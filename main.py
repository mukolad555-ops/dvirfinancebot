import os
import re
import uuid
import logging
import json
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests
import telebot

# ============================================================
# Dvir Finance Bot v5.2 CARD DIRECTIONS
# Telegram + Supabase
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dvir-finance")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
WORKSPACE_CHAT_ID = os.getenv("WORKSPACE_CHAT_ID")
OWNER_TELEGRAM_IDS = {
    int(x.strip()) for x in (os.getenv("OWNER_TELEGRAM_IDS") or "").split(",")
    if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("Не задано TELEGRAM_BOT_TOKEN")
if not SUPABASE_URL:
    raise RuntimeError("Не задано SUPABASE_URL")
if not SUPABASE_KEY:
    raise RuntimeError("Не задано SUPABASE_KEY")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
KYIV = ZoneInfo("Europe/Kyiv")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

CURRENCY_ALIASES = {
    "грн": "UAH", "гр": "UAH", "uah": "UAH", "₴": "UAH", "гривень": "UAH",
    "гривні": "UAH", "гривня": "UAH",
    "дол": "USD", "долар": "USD", "долари": "USD", "доларів": "USD",
    "usd": "USD", "$": "USD",
    "євро": "EUR", "евро": "EUR", "eur": "EUR", "€": "EUR",
}

CURRENCY_SYMBOLS = {"UAH": "грн", "USD": "$", "EUR": "€"}




def workspace_id(message) -> int:
    """Both owners can work from separate private chats in one shared ledger."""
    if WORKSPACE_CHAT_ID and WORKSPACE_CHAT_ID.lstrip("-").isdigit():
        return int(WORKSPACE_CHAT_ID)
    return message.chat.id


def is_authorized(message) -> bool:
    return not OWNER_TELEGRAM_IDS or message.from_user.id in OWNER_TELEGRAM_IDS

# ------------------------ Supabase ------------------------

def sb_request(method: str, table: str, *, params=None, json=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    response = requests.request(
        method,
        url,
        headers=HEADERS,
        params=params,
        json=json,
        timeout=25,
    )
    if not response.ok:
        log.error("Supabase %s %s: %s", method, table, response.text)
        raise RuntimeError(response.text)
    if not response.text:
        return []
    return response.json()


def sb_select(table: str, params: dict):
    return sb_request("GET", table, params=params)


def sb_insert(table: str, payload):
    return sb_request("POST", table, json=payload)


def sb_update(table: str, params: dict, payload: dict):
    return sb_request("PATCH", table, params=params, json=payload)


# ------------------------ Helpers ------------------------

def today_str() -> str:
    return datetime.now(KYIV).date().isoformat()


def user_fields(message):
    user = message.from_user
    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()
    return {
        "telegram_user_id": user.id,
        "telegram_username": user.username,
        "telegram_full_name": full_name or user.username or str(user.id),
        "telegram_message_id": message.message_id,
    }


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def parse_amount(raw: str) -> Decimal:
    cleaned = raw.replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Не вдалося прочитати суму") from exc
    if value <= 0:
        raise ValueError("Сума має бути більшою за нуль")
    return value.quantize(Decimal("0.01"))


def parse_currency(raw: str | None) -> str:
    if not raw:
        return "UAH"
    key = raw.lower().strip().rstrip(".")
    return CURRENCY_ALIASES.get(key, key.upper())


def money(value, currency: str) -> str:
    value = Decimal(str(value or 0))
    number = f"{value:,.2f}".replace(",", " ").replace(".00", "")
    return f"{number} {CURRENCY_SYMBOLS.get(currency, currency)}"


def extract_amount_currency(text: str):
    pattern = (
        r"(?P<amount>\d[\d\s]*(?:[.,]\d{1,2})?)\s*"
        r"(?P<currency>грн|гр|гривень|гривні|гривня|uah|₴|"
        r"доларів|долари|долар|дол|usd|\$|євро|евро|eur|€)?"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Не бачу суму")
    return (
        parse_amount(match.group("amount")),
        parse_currency(match.group("currency")),
        match,
    )


def get_or_create_account(chat_id: int, account_name: str, currency: str, account_type="other"):
    account_name = normalize_text(account_name)
    rows = sb_select(
        "cash_accounts",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "account_name": f"eq.{account_name}",
            "currency": f"eq.{currency}",
            "limit": "1",
        },
    )
    if rows:
        return rows[0]

    created = sb_insert(
        "cash_accounts",
        {
            "telegram_chat_id": chat_id,
            "account_name": account_name,
            "account_type": account_type,
            "currency": currency,
            "is_active": True,
        },
    )
    return created[0]


def account_from_text(text: str, currency: str = "UAH") -> tuple[str, str]:
    """Recognize a person and the exact bank/card destination from natural text."""
    low = text.lower()

    person = None
    if "андр" in low:
        person = "Андрія"
    elif "микол" in low:
        person = "Миколи"

    if person:
        if "півден" in low or "пивден" in low:
            return f"Банк Південний {person}", "bank_account"
        if "приват" in low:
            return f"ПриватБанк {person}", "personal_card"
        if re.search(r"\bфоп\b", low):
            return f"ФОП {person}", "business_account"
        if "карт" in low or "карточ" in low or "рахунок" in low:
            return f"Картка {person}", "personal_card"
        # When a person explicitly receives money, their generic card is safer
        # than silently putting the money into cash.
        return f"Картка {person}", "personal_card"

    # All physical cash currencies live in one logical account: Каса.
    return "Каса", "cash"


def display_account_name(account_name: str | None) -> str:
    """Keep exact bank accounts visible while merging only legacy cash names."""
    name = normalize_text(account_name or "Каса")
    low = name.lower()
    if "півден" in low or "пивден" in low:
        return name
    if "приват" in low:
        return name
    if re.search(r"\bфоп\b", low):
        return name
    if "микол" in low and "карт" in low:
        return "Картка Миколи"
    if "андр" in low and "карт" in low:
        return "Картка Андрія"
    return "Каса"

def default_account_name(text: str, currency: str = "UAH") -> tuple[str, str]:
    return account_from_text(text, currency)

def operation_payload(message, operation_type, amount, currency, description, account_name=None):
    payload = {
        "operation_date": today_str(),
        "operation_type": operation_type,
        "currency": currency,
        "amount": float(amount),
        "operation_group": str(uuid.uuid4()),
        "description": description or None,
        "telegram_chat_id": workspace_id(message),
        "telegram_message_id": message.message_id,
        "is_cancelled": False,
        **user_fields(message),
    }
    if account_name:
        account = get_or_create_account(
            workspace_id(message),
            account_name,
            currency,
            "safe" if account_name == "Сейф" else "other",
        )
        payload["account_id"] = account["id"]
        payload["account_name"] = account_name
    return payload


def sum_rows(rows, field="amount") -> Decimal:
    total = Decimal("0")
    for row in rows:
        total += Decimal(str(row.get(field) or 0))
    return total


# ------------------------ Old cash operations ------------------------

def add_cash_operation(message, operation_type: str, amount: Decimal, currency: str, description: str):
    account_name, _ = default_account_name(description, currency)
    sb_insert(
        "cash_operations",
        operation_payload(
            message,
            operation_type,
            amount,
            currency,
            description,
            account_name=account_name,
        ),
    )


def handle_income_expense(message, text: str) -> bool:
    low = text.lower()

    income_words = ("виручка", "дохід", "доход", "прихід", "приход")
    expense_words = ("витрата", "расход", "видаток", "витрати")

    if low.startswith(income_words):
        amount, currency, match = extract_amount_currency(text)
        description = normalize_text(text[match.end():]) or "Виручка"
        add_cash_operation(message, "income", amount, currency, description)
        bot.reply_to(
            message,
            f"✅ Записано виручку: <b>{money(amount, currency)}</b>\n"
            f"📍 Рахунок: {default_account_name(description, currency)[0]}\n"
            f"👤 Вніс: {user_fields(message)['telegram_full_name']}",
        )
        return True

    if low.startswith(expense_words):
        amount, currency, match = extract_amount_currency(text)
        description = normalize_text(text[match.end():]) or "Витрата"
        add_cash_operation(message, "expense", amount, currency, description)
        bot.reply_to(
            message,
            f"✅ Записано витрату: <b>{money(amount, currency)}</b>\n"
            f"📝 {description}\n"
            f"👤 Вніс: {user_fields(message)['telegram_full_name']}",
        )
        return True

    return False


# ------------------------ Transfers and currency exchange ------------------------

def insert_paired_operations(message, first: dict, second: dict):
    group_id = str(uuid.uuid4())
    common = {
        "operation_date": today_str(),
        "operation_group": group_id,
        "telegram_chat_id": workspace_id(message),
        "telegram_message_id": message.message_id,
        "is_cancelled": False,
        **user_fields(message),
    }
    payloads = []
    for item in (first, second):
        account = get_or_create_account(
            workspace_id(message),
            item["account_name"],
            item["currency"],
            item.get("account_type", "other"),
        )
        payloads.append({
            **common,
            "operation_type": item["operation_type"],
            "currency": item["currency"],
            "amount": float(item["amount"]),
            "description": item.get("description"),
            "account_id": account["id"],
            "account_name": item["account_name"],
        })
    sb_insert("cash_operations", payloads)


def handle_transfer(message, text: str) -> bool:
    low = text.lower()
    transfer_words = ("переклав", "переклали", "переказав", "переказали", "перенесли", "зняли", "поклали")
    if not any(word in low for word in transfer_words):
        return False
    if "помін" in low or "обмін" in low:
        return False

    try:
        amount, currency, match = extract_amount_currency(text)
    except ValueError:
        return False

    before = text[:match.start()]
    after = text[match.end():]

    source_match = re.search(r"(?:з|із|зі|від)\s+(.+?)(?:\s+(?:в|у|на|до)\s+|$)", before, flags=re.IGNORECASE)
    destination_match = re.search(r"(?:в|у|на|до)\s+(.+)$", after, flags=re.IGNORECASE)

    # Common phrasing: "з картки Миколи зняли 10000 грн і поклали в касу"
    if not source_match:
        source_match = re.search(
            r"(?:з|із|зі|від)\s+(.+?)(?:\s+(?:в|у|на|до)\s+|\s+зняли|\s+переклали|\s+переказали|$)",
            text,
            flags=re.IGNORECASE,
        )
    if not destination_match:
        destination_match = re.search(r"(?:в|у|на|до)\s+(.+)$", text, flags=re.IGNORECASE)

    if not source_match or not destination_match:
        return False

    source_name, source_type = account_from_text(source_match.group(1), currency)
    destination_name, destination_type = account_from_text(destination_match.group(1), currency)
    if source_name == destination_name:
        bot.reply_to(message, "⚠️ Рахунок відправника і отримувача однаковий. Операцію не записав.")
        return True

    description = f"Переказ: {source_name} → {destination_name}"
    insert_paired_operations(
        message,
        {
            "operation_type": "transfer_out",
            "currency": currency,
            "amount": amount,
            "account_name": source_name,
            "account_type": source_type,
            "description": description,
        },
        {
            "operation_type": "transfer_in",
            "currency": currency,
            "amount": amount,
            "account_name": destination_name,
            "account_type": destination_type,
            "description": description,
        },
    )
    bot.reply_to(
        message,
        f"✅ Переказ записано\n\n"
        f"➖ {source_name}: {money(amount, currency)}\n"
        f"➕ {destination_name}: {money(amount, currency)}\n\n"
        "Загальна сума грошей не змінилася.",
    )
    return True


def currency_named_after_na(text: str):
    m = re.search(
        r"\bна\s+(?:\d[\d\s]*(?:[.,]\d{1,2})?\s*)?"
        r"(грн|гривні|гривень|uah|₴|долари|доларів|долар|usd|\$|євро|евро|eur|€)",
        text,
        flags=re.IGNORECASE,
    )
    return parse_currency(m.group(1)) if m else None


def handle_exchange(message, text: str) -> bool:
    low = text.lower()
    if not any(word in low for word in ("помін", "обмін", "обмен", "конверт")):
        return False

    amounts = []
    pattern = re.compile(
        r"(?P<amount>\d[\d\s]*(?:[.,]\d{1,2})?)\s*"
        r"(?P<currency>грн|гр|гривень|гривні|гривня|uah|₴|доларів|долари|долар|дол|usd|\$|євро|евро|eur|€)",
        flags=re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        amounts.append((m.start(), parse_amount(m.group("amount")), parse_currency(m.group("currency"))))

    if not amounts:
        return False

    source_amount, source_currency = amounts[0][1], amounts[0][2]
    target_currency = currency_named_after_na(text)
    target_amount = None
    if len(amounts) >= 2 and amounts[1][2] != source_currency:
        target_amount, target_currency = amounts[1][1], amounts[1][2]

    if not target_currency or target_currency == source_currency:
        bot.reply_to(message, "⚠️ Не зрозумів, на яку валюту зробили обмін.")
        return True

    rate_match = re.search(r"(?:курс(?:ом)?|по\s+курсу)\s*[:=-]?\s*(\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    rate = parse_amount(rate_match.group(1)) if rate_match else None

    if target_amount is None:
        if rate is None:
            bot.reply_to(
                message,
                "⚠️ Напиши курс або отриману суму. Наприклад: "
                "<code>поміняли 100000 грн на долари по курсу 41,80</code>",
            )
            return True
        if source_currency == "UAH" and target_currency in ("USD", "EUR"):
            target_amount = (source_amount / rate).quantize(Decimal("0.01"))
        elif target_currency == "UAH" and source_currency in ("USD", "EUR"):
            target_amount = (source_amount * rate).quantize(Decimal("0.01"))
        else:
            # For USD↔EUR the entered rate means target currency per 1 source currency.
            target_amount = (source_amount * rate).quantize(Decimal("0.01"))

    source_name, source_type = account_from_text(text.split("на", 1)[0], source_currency)
    destination_name, destination_type = account_from_text(text.split("на", 1)[-1], target_currency)
    description = (
        f"Обмін {money(source_amount, source_currency)} → "
        f"{money(target_amount, target_currency)}"
        + (f", курс {rate}" if rate else "")
    )
    insert_paired_operations(
        message,
        {
            "operation_type": "exchange_out",
            "currency": source_currency,
            "amount": source_amount,
            "account_name": source_name,
            "account_type": source_type,
            "description": description,
        },
        {
            "operation_type": "exchange_in",
            "currency": target_currency,
            "amount": target_amount,
            "account_name": destination_name,
            "account_type": destination_type,
            "description": description,
        },
    )
    bot.reply_to(
        message,
        f"✅ Обмін записано\n\n"
        f"➖ {source_name}: {money(source_amount, source_currency)}\n"
        f"➕ {destination_name}: {money(target_amount, target_currency)}"
        + (f"\nКурс: {rate}" if rate else ""),
    )
    return True


def handle_external_income(message, text: str) -> bool:
    """Record money received from an outside person/company into an exact account.

    Examples:
    - Андрій отримав від Болени 25000 грн
    - Болена скинула Андрію на ПриватБанк 25000 грн
    - Від Болени прийшло 25000 грн на ФОП Миколи
    """
    low = text.lower()
    incoming_words = (
        "отримав", "отримала", "отримали", "прийшло", "зайшло", "надійшло",
        "скинув", "скинула", "скинули", "перевів", "перевела", "перевели",
        "оплатив", "оплатила", "оплатили",
    )
    if not any(word in low for word in incoming_words):
        return False
    if not ("андр" in low or "микол" in low):
        return False

    try:
        amount, currency, _ = extract_amount_currency(text)
    except ValueError:
        return False

    # Phrases with "з картки ... оплатили" are expenses only when no named
    # recipient (Andrii/Mykola) is receiving the money.
    account_name, account_type = account_from_text(text, currency)
    if account_name == "Каса":
        return False

    payer = None
    patterns = (
        r"(?:від|от)\s+([A-Za-zА-Яа-яІіЇїЄєҐґ0-9_'’.-]+)",
        r"^\s*([A-Za-zА-Яа-яІіЇїЄєҐґ0-9_'’.-]+)\s+(?:скинув|скинула|скинули|перевів|перевела|перевели|оплатив|оплатила|оплатили)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            payer = match.group(1)
            break

    description = f"Надходження{f' від {payer}' if payer else ''} | {normalize_text(text)}"
    sb_insert(
        "cash_operations",
        operation_payload(
            message, "income", amount, currency, description,
            account_name=account_name,
        ),
    )
    bot.reply_to(
        message,
        f"✅ <b>Надходження записано</b>\n"
        f"➕ {money(amount, currency)}\n"
        f"💳 Рахунок: <b>{account_name}</b>"
        + (f"\n🏢 Від: <b>{payer}</b>" if payer else ""),
    )
    return True



def handle_direct_account_flow(message, text: str) -> bool:
    """Handle short, explicit money movements to/from a named card or bank account.

    Examples:
    - на карту Миколи 8000 грн        -> income to Mykola card
    - на картку Андрія 5000 грн       -> income to Andrii card
    - з карти Миколи 3000 грн товар   -> expense from Mykola card
    - із ФОП Миколи 12000 грн податки -> expense from Mykola FOP account

    Transfers between two internal accounts are deliberately left to
    handle_transfer().
    """
    low = normalize_text(text).lower()

    # Must name one of the owners and an account/card destination.
    if not ("микол" in low or "андр" in low):
        return False
    if not re.search(r"карт|карточ|рахунок|приват|фоп|півден|пивден", low):
        return False

    # Do not steal true internal transfers such as
    # "з картки Миколи на картку Андрія 10000 грн".
    has_source = bool(re.search(r"(?:^|\s)(?:з|із|зі|с)\s+", low))
    has_destination = bool(re.search(r"(?:^|\s)(?:на|в|у|до)\s+", low))
    if has_source and has_destination and ("микол" in low and "андр" in low):
        return False

    try:
        amount, currency, match = extract_amount_currency(text)
    except ValueError:
        return False

    account_name, _ = account_from_text(text, currency)
    if account_name == "Каса":
        return False

    incoming = bool(re.search(
        r"(?:^|\s)(?:на|в|у|до)\s+(?:банківськ\w*\s+)?(?:карт\w*|карточ\w*|рахунок|фоп|приватбанк|приват|банк\s+південний|південний)",
        low,
    ))
    outgoing = bool(re.search(
        r"(?:^|\s)(?:з|із|зі|с|від)\s+(?:банківськ\w*\s+)?(?:карт\w*|карточ\w*|рахунку|фоп|приватбанку|привату|банку\s+південний|південного)",
        low,
    ))

    # Natural verbs can make the direction explicit even when the preposition
    # is absent or colloquial.
    if re.search(r"\b(?:отримав|отримала|отримали|прийшло|зайшло|надійшло|поступило)\b", low):
        incoming = True
    if re.search(r"\b(?:зняли|зняв|списали|списав|оплатили|оплатив|заплатили|заплатив|витратили)\b", low):
        outgoing = True

    if incoming == outgoing:
        return False

    description = normalize_text(text[match.end():]) or normalize_text(text)
    operation_type = "income" if incoming else "expense"
    sb_insert(
        "cash_operations",
        operation_payload(
            message, operation_type, amount, currency, description,
            account_name=account_name,
        ),
    )

    sign = "➕" if incoming else "➖"
    action = "Надходження" if incoming else "Витрату"
    bot.reply_to(
        message,
        f"✅ <b>{action} записано</b>\n"
        f"{sign} {money(amount, currency)}\n"
        f"💳 Рахунок: <b>{account_name}</b>"
        + (f"\n📝 {description}" if description and description != normalize_text(text) else ""),
    )
    return True

def handle_natural_expense(message, text: str) -> bool:
    low = text.lower()
    if low.startswith(("витрата", "расход", "видаток", "витрати")):
        return False
    if not re.search(r"\b(оплатили|оплатив|заплатили|заплатив|купили)\b", low):
        return False
    if not re.search(r"карт|каса|сейф|готів", low):
        return False
    try:
        amount, currency, match = extract_amount_currency(text)
    except ValueError:
        return False
    account_name, _ = account_from_text(text, currency)
    description = normalize_text(text) or "Витрата"
    sb_insert(
        "cash_operations",
        operation_payload(message, "expense", amount, currency, description, account_name=account_name),
    )
    bot.reply_to(
        message,
        f"✅ Записано витрату: <b>{money(amount, currency)}</b>\n"
        f"📍 Рахунок: {account_name}\n"
        f"📝 {description}",
    )
    return True



# ------------------------ Evening cash intake ------------------------

AMOUNT_TOKEN_RE = re.compile(
    r"(?P<amount>\d[\d\s]*(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency>грн|гр|гривень|гривні|гривня|uah|₴|доларів|долари|долар|дол|usd|\$|євро|евро|eur|€)",
    flags=re.IGNORECASE,
)


def parse_all_amounts(text: str):
    return [
        (parse_amount(m.group("amount")), parse_currency(m.group("currency")))
        for m in AMOUNT_TOKEN_RE.finditer(text)
    ]


def handle_evening_cash(message, text: str) -> bool:
    """Record multi-currency daily revenue and optional Bazaar sales.

    Supported examples:
    - виручка 27000 грн 1200 $
    - виручка каса 6000 грн 1400 $
    - виручка склад 3000 грн, 100 євро, 27 доларів
    - виручка каса 25000 грн 300 $ 150 € базар 3000 грн

    If the message starts with "виручка" and no source marker is written,
    every amount belongs to the main Cash account. Bazaar is only a source
    label; its amounts are also added to the same Cash balance.
    """
    low = text.lower().strip()
    is_revenue = low.startswith(("виручка", "дохід", "доход", "прихід", "приход"))

    source_pattern = re.compile(
        r"(?:базар(?:[-\s]*каса)?|на\s+базарі|(?:виручка\s+)?склад(?:у|і|ом)?|на\s+складі|зі\s+складу|фастівськ(?:а|ій|ої)?(?:\s+каса|\s+касі|\s+касу)?|фастовськ(?:а|ій|ої)?(?:\s+каса|\s+касі|\s+касу)?|фастівка|(?:(?:загальна|основна)\s+)?кас(?:а|і|у))",
        flags=re.IGNORECASE,
    )
    markers = list(source_pattern.finditer(text))

    # This handler is only for explicit cash/source messages or any
    # multi-currency revenue message. It must not steal ordinary expenses,
    # transfers, debts, or tenant commands.
    if not markers and not is_revenue:
        return False
    if not re.search(r"\d", text):
        return False

    entries = []

    if markers:
        # Amounts before the first marker (usually immediately after the word
        # "виручка") belong to Cash, so short natural phrases are not lost.
        prefix_start = 0
        if is_revenue:
            revenue_word = re.match(r"\s*(?:виручка|дохід|доход|прихід|приход)\b", text, re.IGNORECASE)
            prefix_start = revenue_word.end() if revenue_word else 0
        prefix = text[prefix_start:markers[0].start()]
        for amount, currency in parse_all_amounts(prefix):
            entries.append(("Каса", amount, currency))

        for i, marker in enumerate(markers):
            marker_text = marker.group(0).lower()
            source = "Базар" if "базар" in marker_text else "Каса"
            segment_start = marker.end()
            segment_end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            segment = text[segment_start:segment_end]
            for amount, currency in parse_all_amounts(segment):
                entries.append((source, amount, currency))
    else:
        # "виручка 27000 грн 1200 $ 100 €" — all currencies are Cash.
        revenue_body = re.sub(
            r"^\s*(?:виручка|дохід|доход|прихід|приход)\b[:\s-]*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        for amount, currency in parse_all_amounts(revenue_body):
            entries.append(("Каса", amount, currency))

    if not entries:
        return False

    # Bazaar is a daily sales source, not a separate balance. Every recognized
    # amount is inserted exactly once into the main Cash account.
    for source, amount, currency in entries:
        sb_insert(
            "cash_operations",
            operation_payload(
                message,
                "income",
                amount,
                currency,
                f"Денна каса | {source}",
                account_name="Каса",
            ),
        )

    totals = {}
    for _, amount, currency in entries:
        totals[currency] = totals.get(currency, Decimal("0")) + amount

    lines = ["✅ <b>Касу за день записано</b>", ""]
    for source in ("Каса", "Базар"):
        source_rows = [(a, c) for s0, a, c in entries if s0 == source]
        if source_rows:
            lines.append(f"<b>{source}</b>")
            for amount, currency in source_rows:
                lines.append(f"• {money(amount, currency)}")
            lines.append("")

    lines.append("Додано у <b>касу</b>:")
    for currency in ("UAH", "USD", "EUR"):
        if currency in totals:
            lines.append(f"• {money(totals[currency], currency)}")
    bot.reply_to(message, "\n".join(lines).strip())
    return True


# ------------------------ OpenAI natural-language parser ------------------------

def ai_normalize_command(text: str) -> str | None:
    if not OPENAI_API_KEY:
        return None

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "understood": {"type": "boolean"},
            "canonical_command": {"type": "string"},
        },
        "required": ["understood", "canonical_command"],
    }
    instructions = """
You normalize Ukrainian/Russian bookkeeping messages for DvirFinance.
Return one canonical command only; never invent amounts, names, currencies, accounts, or dates.
Supported canonical forms:
- каса 25000 грн, 300 $, 150 €, базар 3000 грн
- виручка 25000 грн опис
- витрата 1200 грн опис
- з картки Миколи оплатили 1200 грн опис
- переклали 10000 грн з картки Миколи в касу
- поміняли 100000 грн на долари по курсу 41,80
- орендар Вася 12000 грн оренда
- борг Bolena 5000 грн опис
- Bolena оплатила 3000 грн у касу
- Болена скинула Андрію на ПриватБанк 25000 грн
- Від Болени прийшло 25000 грн на ФОП Миколи
- Болена перевела Миколі на Банк Південний 25000 грн
- ревізія 125000 грн у касі
- каса / баланс / сьогодні / борги / орендарі / платежі Ім'я / історія / відміна
Currency symbols are valid: ₴ means UAH, $ means USD, € means EUR.
All authorized owners work in one shared ledger; do not create separate owner cash ledgers.
Bazaar is never a separate balance: its amount is daily sales added immediately to the single general cash account named Каса.
Incoming money to Andrii/Mykola is income, never an expense. Preserve the exact destination account: ПриватБанк, ФОП, Банк Південний, or generic картка. If the message is ambiguous or lacks a required amount/name, set understood=false and canonical_command="".
""".strip()
    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": text,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "finance_command",
                "strict": True,
                "schema": schema,
            }
        },
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=35,
        )
        response.raise_for_status()
        data = response.json()
        output_text = data.get("output_text")
        if not output_text:
            # Defensive extraction for SDK-independent HTTP use.
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        output_text = content.get("text")
                        break
        parsed = json.loads(output_text or "{}")
        command = normalize_text(parsed.get("canonical_command") or "")
        return command if parsed.get("understood") and command else None
    except Exception:
        log.exception("OpenAI parser failed")
        return None

# ------------------------ Revisions ------------------------

def handle_revision(message, text: str) -> bool:
    if not text.lower().startswith(("ревізія", "ревизия")):
        return False

    amount, currency, match = extract_amount_currency(text)
    tail = normalize_text(text[match.end():])
    account_name, account_type = default_account_name(tail, currency)
    account = get_or_create_account(workspace_id(message), account_name, currency, account_type)

    previous = sb_select(
        "cash_revisions",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{workspace_id(message)}",
            "account_id": f"eq.{account['id']}",
            "currency": f"eq.{currency}",
            "order": "revision_date.desc,created_at.desc",
            "limit": "1",
        },
    )
    revision_type = "control" if previous else "opening"

    created = sb_insert(
        "cash_revisions",
        {
            "revision_date": today_str(),
            "telegram_chat_id": workspace_id(message),
            "account_id": account["id"],
            "account_name": account_name,
            "currency": currency,
            "actual_amount": float(amount),
            "calculated_amount": None,
            "difference_amount": None,
            "revision_type": revision_type,
            "description": tail or "Фактичний залишок",
            **{k: v for k, v in user_fields(message).items() if k != "telegram_message_id"},
        },
    )[0]

    label = "Початкову ревізію" if revision_type == "opening" else "Контрольну ревізію"
    bot.reply_to(
        message,
        f"✅ {label} зафіксовано\n\n"
        f"📍 {account_name}\n"
        f"💰 <b>{money(created['actual_amount'], currency)}</b>\n"
        f"📅 {today_str()}\n"
        f"👤 {user_fields(message)['telegram_full_name']}",
    )
    return True


# ------------------------ Debts ------------------------

def create_debt(message, customer_name: str, amount: Decimal, currency: str, description: str):
    payload = {
        "debt_date": today_str(),
        "telegram_chat_id": workspace_id(message),
        "customer_name": normalize_text(customer_name),
        "description": description or None,
        "currency": currency,
        "original_amount": float(amount),
        "paid_amount": 0,
        "outstanding_amount": float(amount),
        "status": "open",
        "is_cancelled": False,
        **user_fields(message),
    }
    return sb_insert("customer_debts", payload)[0]


def handle_debt_create(message, text: str) -> bool:
    low = text.lower()
    if not low.startswith("борг "):
        return False

    # "борг Bolena 25000 грн полікарбонат"
    rest = text[5:].strip()
    amount, currency, match = extract_amount_currency(rest)
    customer = normalize_text(rest[:match.start()])
    description = normalize_text(rest[match.end():])

    if not customer:
        bot.reply_to(message, "Напиши ім’я покупця: <code>борг Bolena 25000 грн</code>")
        return True

    debt = create_debt(message, customer, amount, currency, description)
    bot.reply_to(
        message,
        f"✅ Створено борг\n\n"
        f"👤 <b>{debt['customer_name']}</b>\n"
        f"💰 {money(debt['outstanding_amount'], currency)}\n"
        f"📝 {description or 'Без опису'}\n"
        f"📅 {today_str()}",
    )
    return True


def find_open_debts(chat_id: int, customer_query: str | None = None):
    params = {
        "select": "*",
        "telegram_chat_id": f"eq.{chat_id}",
        "is_cancelled": "eq.false",
        "status": "in.(open,partially_paid)",
        "order": "debt_date.asc,created_at.asc",
    }
    rows = sb_select("customer_debts", params)
    if customer_query:
        query = customer_query.casefold()
        rows = [r for r in rows if query in r["customer_name"].casefold()]
    return rows


def debt_list_text(rows):
    if not rows:
        return "✅ Відкритих боргів немає."

    by_currency = {}
    lines = ["📒 <b>Відкриті борги</b>\n"]
    for row in rows:
        curr = row["currency"]
        amount = Decimal(str(row["outstanding_amount"]))
        by_currency[curr] = by_currency.get(curr, Decimal("0")) + amount
        desc = f" — {row['description']}" if row.get("description") else ""
        lines.append(
            f"• <b>{row['customer_name']}</b>: "
            f"{money(amount, curr)} ({row['debt_date']}){desc}"
        )

    lines.append("\n<b>Разом:</b>")
    for curr, total in by_currency.items():
        lines.append(f"• {money(total, curr)}")
    return "\n".join(lines)


def handle_debt_queries(message, text: str) -> bool:
    low = text.lower().strip()

    if low in ("борги", "хто нам винен", "хто винен"):
        bot.reply_to(message, debt_list_text(find_open_debts(workspace_id(message))))
        return True

    if low.startswith("борг ") and not re.search(r"\d", text):
        customer = normalize_text(text[5:])
        bot.reply_to(
            message,
            debt_list_text(find_open_debts(workspace_id(message), customer)),
        )
        return True

    return False


def handle_debt_payment(message, text: str) -> bool:
    payment_word = re.search(
        r"\b(оплатив|оплатила|оплатили|заплатив|заплатила|погасив|погасила|"
        r"приніс|принесла|закрив|закрила)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not payment_word:
        return False

    customer = normalize_text(text[:payment_word.start()])
    if not customer:
        return False

    tail = text[payment_word.end():]
    try:
        amount, currency, match = extract_amount_currency(tail)
    except ValueError:
        return False

    debts = find_open_debts(workspace_id(message), customer)
    debts = [d for d in debts if d["currency"] == currency]
    if not debts:
        bot.reply_to(
            message,
            f"Не знайшов відкритий борг для <b>{customer}</b> у валюті {currency}.",
        )
        return True

    outstanding_total = sum_rows(debts, "outstanding_amount")
    if amount > outstanding_total:
        over = amount - outstanding_total
        bot.reply_to(
            message,
            f"⚠️ Борг становить {money(outstanding_total, currency)}, "
            f"а вказано {money(amount, currency)}.\n"
            f"Переплата: {money(over, currency)}.\n"
            "Поки операцію не записав.",
        )
        return True

    destination_text = normalize_text(tail[match.end():])
    account_name, account_type = default_account_name(destination_text)
    account = get_or_create_account(workspace_id(message), account_name, currency, account_type)

    remaining_payment = amount
    affected = []

    for debt in debts:
        if remaining_payment <= 0:
            break

        debt_remaining = Decimal(str(debt["outstanding_amount"]))
        part = min(remaining_payment, debt_remaining)
        new_paid = Decimal(str(debt["paid_amount"])) + part
        new_outstanding = debt_remaining - part
        new_status = "closed" if new_outstanding == 0 else "partially_paid"

        sb_insert(
            "debt_payments",
            {
                "payment_date": today_str(),
                "telegram_chat_id": workspace_id(message),
                "debt_id": debt["id"],
                "amount": float(part),
                "currency": currency,
                "destination_account_id": account["id"],
                "destination_account_name": account_name,
                "description": destination_text or f"Оплата боргу {debt['customer_name']}",
                "is_cancelled": False,
                **user_fields(message),
            },
        )

        update_payload = {
            "paid_amount": float(new_paid),
            "outstanding_amount": float(new_outstanding),
            "status": new_status,
            "closed_at": datetime.now(KYIV).isoformat() if new_status == "closed" else None,
        }
        sb_update("customer_debts", {"id": f"eq.{debt['id']}"}, update_payload)
        affected.append((debt["customer_name"], part))
        remaining_payment -= part

    left = outstanding_total - amount
    bot.reply_to(
        message,
        f"✅ Оплату боргу записано\n\n"
        f"👤 {customer}\n"
        f"💰 Отримано: <b>{money(amount, currency)}</b>\n"
        f"📍 Зараховано: {account_name}\n"
        f"📒 Залишок боргу: {money(left, currency)}\n"
        "ℹ️ У виручку повторно не додано.",
    )
    return True


# ------------------------ Tenant payments ------------------------

def tenant_category(text: str) -> str:
    low = text.lower()
    if any(word in low for word in ("комунал", "світло", "электро", "електро", "вода", "газ")):
        return "Комунальні"
    if any(word in low for word in ("оренда", "аренда", "рент")):
        return "Оренда"
    return "Інший платіж"


def handle_tenant_payment(message, text: str) -> bool:
    low = text.lower().strip()
    if not low.startswith(("орендар ", "арендатор ")):
        return False

    prefix_len = len("орендар ") if low.startswith("орендар ") else len("арендатор ")
    rest = text[prefix_len:].strip()
    amount, currency, match = extract_amount_currency(rest)
    tenant_name = normalize_text(rest[:match.start()])
    details = normalize_text(rest[match.end():])

    if not tenant_name:
        bot.reply_to(message, "Напиши ім’я або кличку орендаря. Наприклад: <code>орендар Вася 12000 грн оренда</code>")
        return True

    category = tenant_category(details)
    account_name = "Каса"
    description = f"Орендар: {tenant_name} | {category}"
    if details:
        description += f" | {details}"

    sb_insert(
        "cash_operations",
        operation_payload(
            message,
            "income",
            amount,
            currency,
            description,
            account_name=account_name,
        ),
    )

    bot.reply_to(
        message,
        f"✅ Платіж орендаря записано\n\n"
        f"👤 <b>{tenant_name}</b>\n"
        f"🏷 {category}\n"
        f"💰 <b>{money(amount, currency)}</b>\n"
        f"📍 Зараховано: {account_name}",
    )
    return True


def tenant_payments_text(chat_id: int, tenant_query: str | None = None) -> str:
    rows = sb_select(
        "cash_operations",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "operation_type": "eq.income",
            "description": "like.Орендар:*",
            "is_cancelled": "eq.false",
            "order": "operation_date.desc,created_at.desc",
        },
    )

    if tenant_query:
        q = tenant_query.casefold()
        rows = [r for r in rows if q in (r.get("description") or "").casefold()]

    if not rows:
        return "Платежів орендарів поки немає."

    totals = {}
    lines = ["🏢 <b>Платежі орендарів</b>\n"]
    for row in rows[:20]:
        desc = row.get("description") or ""
        parts = [part.strip() for part in desc.split("|")]
        name = parts[0].replace("Орендар:", "").strip() if parts else "Орендар"
        category = parts[1] if len(parts) > 1 else "Платіж"
        curr = row["currency"]
        amount = Decimal(str(row["amount"]))
        totals[curr] = totals.get(curr, Decimal("0")) + amount
        lines.append(f"• {row['operation_date']} — <b>{name}</b>: {money(amount, curr)} ({category})")

    lines.append("\n<b>Разом за показані платежі:</b>")
    for curr, total in totals.items():
        lines.append(f"• {money(total, curr)}")
    return "\n".join(lines)


def handle_tenant_queries(message, text: str) -> bool:
    low = text.lower().strip()
    if low in ("орендарі", "арендатори", "платежі орендарів", "оплати орендарів"):
        bot.reply_to(message, tenant_payments_text(workspace_id(message)))
        return True

    for prefix in ("платежі ", "оплати "):
        if low.startswith(prefix):
            name = normalize_text(text[len(prefix):])
            if name:
                bot.reply_to(message, tenant_payments_text(workspace_id(message), name))
                return True
    return False


# ------------------------ Daily closing ------------------------

def parse_daily_closing(text: str):
    lines = [normalize_text(line) for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    first = lines[0].lower()
    if not (
        first.startswith("виручка за день")
        or first.startswith("закриття дня")
        or first.startswith("закрытие дня")
    ):
        return None

    total, currency, _ = extract_amount_currency(lines[0])
    allocations = []
    debts = []

    for line in lines[1:]:
        low = line.lower()
        amount, curr, match = extract_amount_currency(line)
        if curr != currency:
            raise ValueError("У закритті дня всі суми мають бути в одній валюті")

        if low.startswith(("у сейф", "в сейф", "сейф")):
            allocations.append(("Сейф", "safe", amount))
        elif "карт" in low:
            # Take words after "картку/карту/картка"
            m = re.search(r"карт(?:ку|ка|у|ці)\s*(.*?)(?=\s+\d|$)", line, re.IGNORECASE)
            owner = normalize_text(m.group(1)) if m and m.group(1) else "Без назви"
            allocations.append((f"Картка {owner}", "personal_card", amount))
        elif low.startswith("борг "):
            before_amount = normalize_text(line[5:match.start()])
            after_amount = normalize_text(line[match.end():])
            customer = before_amount
            if not customer:
                raise ValueError("У рядку боргу не вказано покупця")
            debts.append((customer, amount, after_amount))
        else:
            raise ValueError(f"Не розумію рядок: {line}")

    distributed = sum(a[2] for a in allocations) + sum(d[1] for d in debts)
    return total, currency, allocations, debts, distributed


def handle_daily_closing(message, text: str) -> bool:
    try:
        parsed = parse_daily_closing(text)
    except ValueError as exc:
        bot.reply_to(message, f"⚠️ {exc}")
        return True

    if not parsed:
        return False

    total, currency, allocations, debts, distributed = parsed
    difference = total - distributed

    if difference != 0:
        label = "Не розподілено" if difference > 0 else "Розподілено зайве"
        bot.reply_to(
            message,
            f"⚠️ Баланс не сходиться.\n\n"
            f"Виручка: {money(total, currency)}\n"
            f"Розподілено: {money(distributed, currency)}\n"
            f"{label}: <b>{money(abs(difference), currency)}</b>\n\n"
            "Нічого не записано.",
        )
        return True

    existing = sb_select(
        "daily_closings",
        {
            "select": "id",
            "telegram_chat_id": f"eq.{workspace_id(message)}",
            "closing_date": f"eq.{today_str()}",
            "currency": f"eq.{currency}",
            "is_cancelled": "eq.false",
            "limit": "1",
        },
    )
    if existing:
        bot.reply_to(
            message,
            f"⚠️ Закриття дня за {today_str()} у валюті {currency} вже існує.",
        )
        return True

    safe_total = sum(a[2] for a in allocations if a[1] == "safe")
    card_total = sum(a[2] for a in allocations if a[1] != "safe")
    debt_total = sum(d[1] for d in debts)

    closing = sb_insert(
        "daily_closings",
        {
            "closing_date": today_str(),
            "telegram_chat_id": workspace_id(message),
            "currency": currency,
            "total_revenue": float(total),
            "cash_to_safe": float(safe_total),
            "cash_to_cards": float(card_total),
            "issued_as_debt": float(debt_total),
            "difference_amount": 0,
            "description": "Щоденне закриття",
            "is_cancelled": False,
            **user_fields(message),
        },
    )[0]

    for name, account_type, amount in allocations:
        account = get_or_create_account(workspace_id(message), name, currency, account_type)
        sb_insert(
            "daily_closing_accounts",
            {
                "daily_closing_id": closing["id"],
                "account_id": account["id"],
                "account_name": name,
                "amount": float(amount),
            },
        )

    for customer, amount, description in debts:
        create_debt(message, customer, amount, currency, description)

    lines = [
        "✅ <b>День закрито</b>",
        "",
        f"Загальна виручка: <b>{money(total, currency)}</b>",
        f"У сейф: {money(safe_total, currency)}",
        f"На картки: {money(card_total, currency)}",
        f"Видано товару в борг: {money(debt_total, currency)}",
        "",
        "Баланс зійшовся.",
    ]
    bot.reply_to(message, "\n".join(lines))
    return True


# ------------------------ Cash summary ------------------------

def latest_revisions(chat_id: int):
    rows = sb_select(
        "cash_revisions",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "order": "revision_date.desc,created_at.desc",
        },
    )
    latest = {}
    for row in rows:
        key = (row["account_name"], row["currency"])
        if key not in latest:
            latest[key] = row
    return latest


def cash_summary(chat_id: int) -> str:
    balances = {}

    def add(account_name, currency, amount):
        key = (display_account_name(account_name), currency)
        balances[key] = balances.get(key, Decimal("0")) + Decimal(str(amount or 0))

    revisions = latest_revisions(chat_id)
    revision_dates = {}
    # Several old names may now point to the same logical account. Keep only
    # the newest revision for each displayed account and currency.
    for old_key, row in revisions.items():
        key = (display_account_name(old_key[0]), old_key[1])
        previous_date = revision_dates.get(key)
        if previous_date is None or row["revision_date"] > previous_date:
            balances[key] = Decimal(str(row["actual_amount"]))
            revision_dates[key] = row["revision_date"]

    operations = sb_select(
        "cash_operations",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "order": "operation_date.asc,created_at.asc",
        },
    )
    for row in operations:
        key = (display_account_name(row.get("account_name")), row["currency"])
        rev_date = revision_dates.get(key)
        if rev_date and row["operation_date"] <= rev_date:
            continue
        if row["operation_type"] in ("income", "exchange_in", "transfer_in"):
            sign = Decimal("1")
        elif row["operation_type"] in ("expense", "exchange_out", "transfer_out"):
            sign = Decimal("-1")
        else:
            continue
        add(key[0], key[1], sign * Decimal(str(row["amount"])))

    closings = sb_select(
        "daily_closings",
        {
            "select": "id,closing_date,currency",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "order": "closing_date.asc",
        },
    )
    closing_map = {str(c["id"]): c for c in closings}
    if closing_map:
        closing_accounts = sb_select(
            "daily_closing_accounts",
            {
                "select": "*",
                "daily_closing_id": f"in.({','.join(closing_map.keys())})",
            },
        )
        for row in closing_accounts:
            closing = closing_map.get(str(row["daily_closing_id"]))
            if not closing:
                continue
            key = (row["account_name"], closing["currency"])
            rev_date = revision_dates.get(key)
            if rev_date and closing["closing_date"] <= rev_date:
                continue
            add(key[0], key[1], row["amount"])

    payments = sb_select(
        "debt_payments",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "order": "payment_date.asc",
        },
    )
    for row in payments:
        key = (display_account_name(row.get("destination_account_name") or "Каса"), row["currency"])
        rev_date = revision_dates.get(key)
        if rev_date and row["payment_date"] <= rev_date:
            continue
        add(key[0], key[1], row["amount"])

    debts = find_open_debts(chat_id)
    debt_totals = {}
    for debt in debts:
        curr = debt["currency"]
        debt_totals[curr] = debt_totals.get(curr, Decimal("0")) + Decimal(
            str(debt["outstanding_amount"])
        )

    tenant_rows = sb_select(
        "cash_operations",
        {
            "select": "description",
            "telegram_chat_id": f"eq.{chat_id}",
            "operation_type": "eq.income",
            "description": "like.Орендар:*",
            "is_cancelled": "eq.false",
        },
    )
    tenant_names = set()
    for row in tenant_rows:
        desc = row.get("description") or ""
        first = desc.split("|", 1)[0].replace("Орендар:", "").strip()
        if first:
            tenant_names.add(first.casefold())

    lines = ["💰 <b>Фінансовий стан</b>"]
    standard_accounts = ["Каса", "Картка Миколи", "Картка Андрія"]
    extra_accounts = sorted({account for account, _ in balances if account not in standard_accounts})
    account_order = standard_accounts + extra_accounts

    for account in account_order:
        icon = "💵" if account == "Каса" else "💳"
        lines.append(f"\n{icon} <b>{account}</b>")
        currencies = ("UAH", "USD", "EUR") if account == "Каса" else tuple(
            curr for curr in ("UAH", "USD", "EUR") if (account, curr) in balances
        ) or ("UAH",)
        for curr in currencies:
            lines.append(money(balances.get((account, curr), Decimal("0")), curr))

    lines.append("\n🏢 <b>Орендарі</b>")
    lines.append(f"{len(tenant_names)} орендарів із зафіксованими платежами" if tenant_names else "Платежів ще немає")

    lines.append("\n<b>Нам винні:</b>")
    if debt_totals:
        for curr, amount in debt_totals.items():
            lines.append(f"• {money(amount, curr)}")
    else:
        lines.append("• Боргів немає")

    return "\n".join(lines)


# ------------------------ Reports/history ------------------------

def report_text(chat_id: int):
    rows = sb_select(
        "cash_operations",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "operation_date": f"eq.{today_str()}",
            "order": "created_at.asc",
        },
    )

    totals = {}
    for row in rows:
        curr = row["currency"]
        totals.setdefault(curr, {"income": Decimal("0"), "expense": Decimal("0")})
        if row["operation_type"] == "income":
            totals[curr]["income"] += Decimal(str(row["amount"]))
        elif row["operation_type"] == "expense":
            totals[curr]["expense"] += Decimal(str(row["amount"]))

    closings = sb_select(
        "daily_closings",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "closing_date": f"eq.{today_str()}",
            "is_cancelled": "eq.false",
        },
    )
    for closing in closings:
        curr = closing["currency"]
        totals.setdefault(curr, {"income": Decimal("0"), "expense": Decimal("0")})
        totals[curr]["income"] += Decimal(str(closing["total_revenue"]))

    if not totals:
        return "За сьогодні операцій ще немає."

    lines = [f"📊 <b>Звіт за {today_str()}</b>\n"]
    for curr, data in totals.items():
        lines.append(f"<b>{curr}</b>")
        lines.append(f"Виручка: {money(data['income'], curr)}")
        lines.append(f"Витрати: {money(data['expense'], curr)}")
        lines.append(f"Рух: {money(data['income'] - data['expense'], curr)}\n")
    return "\n".join(lines)


def history_text(chat_id: int):
    rows = sb_select(
        "cash_operations",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "order": "created_at.desc",
            "limit": "10",
        },
    )
    if not rows:
        return "Історія поки порожня."

    lines = ["🕘 <b>Останні операції</b>\n"]
    labels = {
        "income": "Виручка",
        "expense": "Витрата",
        "exchange_in": "Обмін +",
        "exchange_out": "Обмін −",
        "transfer_in": "Переказ +",
        "transfer_out": "Переказ −",
    }
    for row in rows:
        who = row.get("telegram_full_name") or row.get("entered_by") or "невідомо"
        lines.append(
            f"• {row['operation_date']} — {labels.get(row['operation_type'], row['operation_type'])} "
            f"{money(row['amount'], row['currency'])}\n"
            f"  {row.get('description') or ''} · {who}"
        )
    return "\n".join(lines)



def cancel_last_logical_operation(message) -> bool:
    """Cancel the newest logical cash action, not just one database row.

    Transfers and exchanges use operation_group. Multi-currency revenue entered
    in one Telegram message shares telegram_message_id. In both cases every
    related row is cancelled together so balances remain correct.
    """
    chat_id = workspace_id(message)
    rows = sb_select(
        "cash_operations",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    if not rows:
        bot.reply_to(message, "Немає операції для відміни.")
        return True

    last = rows[0]
    if last.get("operation_group"):
        params = {
            "telegram_chat_id": f"eq.{chat_id}",
            "operation_group": f"eq.{last['operation_group']}",
            "is_cancelled": "eq.false",
        }
    elif last.get("telegram_message_id") is not None:
        params = {
            "telegram_chat_id": f"eq.{chat_id}",
            "telegram_message_id": f"eq.{last['telegram_message_id']}",
            "is_cancelled": "eq.false",
        }
    else:
        params = {"id": f"eq.{last['id']}"}

    related = sb_select("cash_operations", {"select": "*", **params})
    now = datetime.now(KYIV).isoformat()
    sb_update(
        "cash_operations",
        params,
        {
            "is_cancelled": True,
            "cancelled_at": now,
            "cancellation_reason": f"Відмінено користувачем {message.from_user.id}",
        },
    )

    totals = {}
    for row in related or [last]:
        sign = Decimal("-1") if row.get("operation_type") in ("expense", "exchange_out", "transfer_out") else Decimal("1")
        curr = row.get("currency", "UAH")
        totals[curr] = totals.get(curr, Decimal("0")) + sign * Decimal(str(row.get("amount") or 0))

    lines = ["✅ <b>Останню операцію відмінено</b>"]
    for curr in ("UAH", "USD", "EUR"):
        if curr in totals:
            lines.append(f"• {money(abs(totals[curr]), curr)}")
    if len(related) > 1:
        lines.append(f"Пов'язаних записів: {len(related)}")
    bot.reply_to(message, "\n".join(lines))
    return True

# ------------------------ Commands ------------------------

HELP_TEXT = """
<b>Dvir Finance v5.2</b>

Основні команди:

<code>ревізія 125000 грн у касі</code>
<code>ревізія 1557 доларів у касі</code>
<code>ревізія 305 євро у касі</code>

<code>виручка 25000 грн</code>
<code>витрата 1200 грн бензин</code>
<code>на картку Миколи 8000 грн</code>
<code>з картки Миколи 10000 грн за товар</code>
<code>на картку Андрія 5000 грн</code>
<code>з картки Андрія 3000 грн доставка</code>

<code>переклали 10000 грн з картки Миколи в касу</code>
<code>поміняли 100000 грн на долари по курсу 41,80</code>
<code>поміняли 5000 доларів на євро по курсу 0,91</code>


<code>орендар Вася 12000 грн оренда</code>
<code>орендар Вася 2500 грн комуналка</code>
<code>орендарі</code>
<code>платежі Вася</code>

<code>борг Bolena 5000 грн полікарбонат</code>
<code>Bolena оплатила 3000 грн у касу</code>
<code>борги</code>
<code>борг Bolena</code>

Вечірня каса одним повідомленням:
<code>каса 25000 грн, базар 3000 грн</code>
<code>каса 25000 грн 500 доларів 200 євро</code>

Базар — лише денний продаж. Його сума одразу додається у загальну касу.

<code>каса</code> або <code>баланс</code>
<code>сьогодні</code> або <code>звіт</code>
<code>історія</code>
<code>відміна</code> — відмінити останню логічну операцію
<code>/id</code>
""".strip()


@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["id"])
def id_handler(message):
    bot.reply_to(
        message,
        f"ID групи/чату: <code>{workspace_id(message)}</code>\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
    )


@bot.message_handler(func=lambda m: True, content_types=["text"])
def text_handler(message):
    if not is_authorized(message):
        bot.reply_to(message, "⛔ Цей користувач не має доступу до спільної каси.")
        return
    dispatch_text(message, message.text.strip(), allow_ai=True)


def dispatch_text(message, text: str, allow_ai: bool = True):
    low = text.lower().strip()

    try:
        if low in ("каса", "баланс", "скільки грошей", "яка фастівська каса", "скільки всього грошей", "що маємо зараз"):
            bot.reply_to(message, cash_summary(workspace_id(message)))
            return

        if low in ("звіт", "отчет", "сьогодні", "сегодня"):
            bot.reply_to(message, report_text(workspace_id(message)))
            return

        if low in ("історія", "история"):
            bot.reply_to(message, history_text(workspace_id(message)))
            return

        if low in ("відміна", "відмінити", "скасувати останню", "видалити останню", "отмена", "отміна"):
            cancel_last_logical_operation(message)
            return

        one_word_hints = {
            "ревізія": "Напиши фактичний залишок, наприклад:\n<code>ревізія 125000 грн 1200 $ 300 € у касі</code>",
            "ревизия": "Напиши фактичний залишок, наприклад:\n<code>ревізія 125000 грн у касі</code>",
            "виручка": "Додай суму, наприклад:\n<code>виручка 27000 грн 1200 $ 100 €</code>",
            "витрата": "Додай суму й опис, наприклад:\n<code>витрата 2500 грн пальне</code>",
            "видаток": "Додай суму й опис, наприклад:\n<code>видаток 2500 грн пальне</code>",
            "обмін": "Напиши обидві валюти та курс, наприклад:\n<code>поміняли 100000 грн на долари по курсу 41,80</code>",
            "борг": "Напиши ім’я, суму й опис, наприклад:\n<code>борг Bolena 5000 грн товар</code>",
            "орендар": "Напиши ім’я, суму й тип платежу, наприклад:\n<code>орендар Вася 12000 грн оренда</code>",
        }
        if low in one_word_hints:
            bot.reply_to(message, one_word_hints[low])
            return

        if handle_exchange(message, text):
            return
        if handle_transfer(message, text):
            return
        if handle_direct_account_flow(message, text):
            return
        if handle_external_income(message, text):
            return
        if handle_evening_cash(message, text):
            return
        if handle_natural_expense(message, text):
            return
        if handle_daily_closing(message, text):
            return
        if handle_revision(message, text):
            return
        if handle_tenant_queries(message, text):
            return
        if handle_tenant_payment(message, text):
            return
        if handle_debt_queries(message, text):
            return
        if handle_debt_create(message, text):
            return
        if handle_debt_payment(message, text):
            return
        if handle_income_expense(message, text):
            return

        if allow_ai:
            canonical = ai_normalize_command(text)
            if canonical and canonical.casefold() != normalize_text(text).casefold():
                log.info("AI normalized: %r -> %r", text, canonical)
                dispatch_text(message, canonical, allow_ai=False)
                return

        bot.reply_to(
            message,
            "Не зовсім зрозумів запис. Дані не записував.\n\n"
            "Напиши, наприклад:\n"
            "<code>каса 25000 грн, базар 3000 грн</code>\n"
            "або натисни /help",
        )

    except Exception as exc:
        log.exception("Помилка обробки повідомлення")
        bot.reply_to(
            message,
            "⚠️ Не вдалося записати операцію. Дані не втрачено. "
            "Спробуй ще раз або надішли скрін помилки.\n\n"
            f"<code>{str(exc)[:300]}</code>",
        )


if __name__ == "__main__":
    log.info("Dvir Finance Bot v5.2 CARD DIRECTIONS запущено")
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        skip_pending=True,
        allowed_updates=["message"],
    )
