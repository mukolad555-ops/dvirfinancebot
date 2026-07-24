import os
import re
import uuid
import logging
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests
import telebot

# ============================================================
# Dvir Finance Bot v2.1 Parser
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

VERSION = "DvirFinance 2.1-PARSER-20260724"


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


def extract_all_amounts(text: str):
    """Повертає всі суми з валютами у порядку появи."""
    pattern = re.compile(
        r"(?P<amount>\d[\d\s]*(?:[.,]\d{1,2})?)\s*"
        r"(?P<currency>грн|гривень|гривні|гривня|гр|uah|₴|"
        r"доларів|долари|долар|дол|usd|\$|євро|евро|eur|€)?",
        flags=re.IGNORECASE,
    )
    result = []
    for match in pattern.finditer(text):
        raw_currency = match.group("currency")
        # Голі числа в описі не приймаємо як гроші, крім одного числа у всій фразі.
        if not raw_currency and len(list(re.finditer(r"\d+", text))) > 1:
            continue
        result.append((parse_amount(match.group("amount")), parse_currency(raw_currency), match))
    if not result:
        raise ValueError("Не бачу суму")
    return result


def remove_amounts_and_accounts(text: str) -> str:
    """Залишає людський опис після вилучення сум, валют та службових слів рахунку."""
    cleaned = re.sub(
        r"\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:грн|гривень|гривні|гривня|гр|uah|₴|"
        r"доларів|долари|долар|дол|usd|\$|євро|евро|eur|€)?",
        " ", text, flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:на|у|в|до|з|із)?\s*(?:карт(?:у|ку|а|ка|ці)|карточк(?:у|а|и))\s+"
        r"(?:миколи|миколы|андрія|андрия|андрея)\b",
        " ", cleaned, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:у|в|на)\s+(?:касу|касі|сейф)\b", " ", cleaned, flags=re.IGNORECASE)
    return normalize_text(cleaned).strip(" -,:;")


def canonical_customer_name(value: str) -> str:
    return normalize_text(value).strip(" -,:;")


def find_customer_from_transfer_text(text: str) -> str | None:
    """У фразі 'Болена на карту Миколи 2000 грн' повертає 'Болена'."""
    match = re.search(
        r"^(?P<customer>.+?)\s+(?:скинув|скинула|переказав|переказала|перевів|перевела)?\s*"
        r"(?:на|у|в)\s+(?:карт(?:у|ку|а|ка)|карточк(?:у|а))\b",
        normalize_text(text), flags=re.IGNORECASE,
    )
    if not match:
        return None
    customer = canonical_customer_name(match.group("customer"))
    return customer or None


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


def default_account_name(text: str) -> tuple[str, str]:
    """Визначає рахунок з довільної фрази, не захоплюючи суму в назву."""
    low = normalize_text(text).lower()

    if any(word in low for word in ("каса", "касу", "касі", "сейф", "готів", "налич")):
        return "Сейф", "safe"

    # Спочатку конкретні власники — це найнадійніше для коротких фраз.
    if re.search(r"\b(?:карт(?:а|у|ка|ку|ці|ою)|карточк(?:а|у|и|ою))\s+микол", low):
        return "Картка Миколи", "personal_card"
    if re.search(r"\b(?:карт(?:а|у|ка|ку|ці|ою)|карточк(?:а|у|и|ою))\s+андр", low):
        return "Картка Андрія", "personal_card"

    if "фоп" in low and "микол" in low:
        return "ФОП Миколи", "fop_card"
    if "фоп" in low and "андр" in low:
        return "ФОП Андрія", "fop_card"

    if "півден" in low and "микол" in low:
        return "Банк Південний Миколи", "bank"
    if "півден" in low and "андр" in low:
        return "Банк Південний Андрія", "bank"

    if "приват" in low and "микол" in low:
        return "ПриватБанк Миколи", "bank"
    if "приват" in low and "андр" in low:
        return "ПриватБанк Андрія", "bank"

    # Довільна картка: власник закінчується перед сумою або кінцем рядка.
    card_match = re.search(
        r"(?:на|у|в|до)?\s*(?:особисту\s+|фоп(?:івську)?\s+)?"
        r"(?:карт(?:ку|ці|ка|у)|карточк(?:у|а|и))\s+"
        r"(?P<owner>[а-яіїєґa-z'’.-]+(?:\s+[а-яіїєґa-z'’.-]+){0,2})"
        r"(?=\s+\d|\s*$)",
        low,
        flags=re.IGNORECASE,
    )
    if card_match:
        owner = normalize_text(card_match.group("owner")).title()
        return f"Картка {owner}", "personal_card"

    if "фоп" in low and "карт" in low:
        return "ФОП-картка", "fop_card"

    return "Сейф", "safe"


def operation_payload(message, operation_type, amount, currency, description, account_name=None):
    payload = {
        "operation_date": today_str(),
        "operation_type": operation_type,
        "currency": currency,
        "amount": float(amount),
        "operation_group": str(uuid.uuid4()),
        "description": description or None,
        "telegram_chat_id": message.chat.id,
        "telegram_message_id": message.message_id,
        "is_cancelled": False,
        **user_fields(message),
    }
    if account_name:
        account = get_or_create_account(
            message.chat.id,
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
    account_name, _ = default_account_name(description)
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
    low = normalize_text(text).lower()

    income_words = (
        "виручка", "дохід", "доход", "прихід", "приход", "надходження",
        "отримав", "отримали", "надійшло", "зайшло", "оренда", "орендна плата",
        "склад", "базар", "ринок",
    )
    expense_words = (
        "витрата", "расход", "видаток", "витрати", "оплата", "сплата", "платіж",
        "оплатив", "оплатили", "сплатив", "сплатили", "заплатив", "заплатили",
        "купив", "купили", "зарплата",
    )

    operation_type = None
    source_label = None
    if low.startswith(income_words):
        operation_type = "income"
        if low.startswith(("оренда", "орендна плата")):
            source_label = "Оренда"
        elif low.startswith("склад"):
            source_label = "Склад"
        elif low.startswith(("базар", "ринок")):
            source_label = "Базар"
        else:
            source_label = "Надходження"
    elif low.startswith(expense_words):
        operation_type = "expense"

    if not operation_type:
        return False

    amounts = extract_all_amounts(text)
    account_name, _ = default_account_name(text)
    description = remove_amounts_and_accounts(text)

    # Прибираємо тільки перше службове слово, а решту залишаємо описом.
    first_word_pattern = income_words if operation_type == "income" else expense_words
    for word in sorted(first_word_pattern, key=len, reverse=True):
        if description.lower().startswith(word):
            description = normalize_text(description[len(word):])
            break
    description = description or source_label or ("Витрата" if operation_type == "expense" else "Надходження")

    saved = []
    for amount, currency, _ in amounts:
        add_cash_operation(message, operation_type, amount, currency, f"{source_label + ': ' if source_label else ''}{description}")
        saved.append(money(amount, currency))

    sign_label = "надходження" if operation_type == "income" else "витрату"
    bot.reply_to(
        message,
        f"✅ Записано {sign_label}: <b>{', '.join(saved)}</b>\n"
        f"📍 Рахунок: {account_name}\n"
        f"📝 {description}\n"
        f"👤 Вніс: {user_fields(message)['telegram_full_name']}",
    )
    return True


# ------------------------ Revisions ------------------------

def handle_revision(message, text: str) -> bool:
    if not text.lower().startswith(("ревізія", "ревизия")):
        return False

    amount, currency, match = extract_amount_currency(text)
    tail = normalize_text(text[match.end():])
    account_name, account_type = default_account_name(tail)
    account = get_or_create_account(message.chat.id, account_name, currency, account_type)

    previous = sb_select(
        "cash_revisions",
        {
            "select": "*",
            "telegram_chat_id": f"eq.{message.chat.id}",
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
            "telegram_chat_id": message.chat.id,
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
        "telegram_chat_id": message.chat.id,
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
        bot.reply_to(message, debt_list_text(find_open_debts(message.chat.id)))
        return True

    if low.startswith("борг ") and not re.search(r"\d", text):
        customer = normalize_text(text[5:])
        bot.reply_to(
            message,
            debt_list_text(find_open_debts(message.chat.id, customer)),
        )
        return True

    return False


def handle_debt_payment(message, text: str) -> bool:
    payment_word = re.search(
        r"\b(повернув|повернула|повернули|віддав|віддала|віддали|"
        r"оплатив|оплатила|оплатили|заплатив|заплатила|погасив|погасила|"
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

    debts = find_open_debts(message.chat.id, customer)
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
    account = get_or_create_account(message.chat.id, account_name, currency, account_type)

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
                "telegram_chat_id": message.chat.id,
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


def handle_context_debt_payment(message, text: str) -> bool:
    """Коротка фраза: 'Болена на карту Миколи 2000 грн'.

    Автопогашення виконується лише тоді, коли знайдено відкритий борг цього
    контрагента в тій самій валюті. Інакше обробник нічого не записує.
    """
    customer = find_customer_from_transfer_text(text)
    if not customer:
        return False

    try:
        amount, currency, match = extract_amount_currency(text)
    except ValueError:
        return False

    debts = [d for d in find_open_debts(message.chat.id, customer) if d["currency"] == currency]
    if not debts:
        return False

    # Захист від випадкового списання: ім'я має збігатися повністю хоча б з одним боргом.
    exact = [d for d in debts if d["customer_name"].casefold() == customer.casefold()]
    if exact:
        debts = exact

    outstanding_total = sum_rows(debts, "outstanding_amount")
    if amount > outstanding_total:
        bot.reply_to(
            message,
            f"⚠️ У <b>{customer}</b> борг {money(outstanding_total, currency)}, "
            f"а вказано {money(amount, currency)}. Нічого не записав.",
        )
        return True

    account_name, account_type = default_account_name(text)
    account = get_or_create_account(message.chat.id, account_name, currency, account_type)

    remaining_payment = amount
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
                "telegram_chat_id": message.chat.id,
                "debt_id": debt["id"],
                "amount": float(part),
                "currency": currency,
                "destination_account_id": account["id"],
                "destination_account_name": account_name,
                "description": f"Повернення боргу {debt['customer_name']}",
                "is_cancelled": False,
                **user_fields(message),
            },
        )
        sb_update(
            "customer_debts",
            {"id": f"eq.{debt['id']}"},
            {
                "paid_amount": float(new_paid),
                "outstanding_amount": float(new_outstanding),
                "status": new_status,
                "closed_at": datetime.now(KYIV).isoformat() if new_status == "closed" else None,
            },
        )
        remaining_payment -= part

    left = outstanding_total - amount
    bot.reply_to(
        message,
        f"✅ Повернення боргу записано\n\n"
        f"👤 <b>{customer}</b>\n"
        f"💰 Отримано: <b>{money(amount, currency)}</b>\n"
        f"📍 Зараховано: {account_name}\n"
        f"📒 Залишок боргу: <b>{money(left, currency)}</b>",
    )
    return True


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
            "telegram_chat_id": f"eq.{message.chat.id}",
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
            "telegram_chat_id": message.chat.id,
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
        account = get_or_create_account(message.chat.id, name, currency, account_type)
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
        key = (account_name or "Сейф", currency)
        balances[key] = balances.get(key, Decimal("0")) + Decimal(str(amount or 0))

    revisions = latest_revisions(chat_id)
    revision_dates = {}
    for key, row in revisions.items():
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
        key = (row.get("account_name") or "Сейф", row["currency"])
        rev_date = revision_dates.get(key)
        if rev_date and row["operation_date"] <= rev_date:
            continue
        sign = Decimal("1") if row["operation_type"] in ("income", "exchange_in") else Decimal("-1")
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
        key = (row.get("destination_account_name") or "Сейф", row["currency"])
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

    lines = ["💰 <b>Фінансовий стан</b>\n"]
    if balances:
        lines.append("<b>Доступні гроші:</b>")
        for (name, curr), amount in sorted(balances.items()):
            lines.append(f"• {name}: {money(amount, curr)}")
    else:
        lines.append("Грошові залишки ще не внесені.")

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
    }
    for row in rows:
        who = row.get("telegram_full_name") or row.get("entered_by") or "невідомо"
        lines.append(
            f"• {row['operation_date']} — {labels.get(row['operation_type'], row['operation_type'])} "
            f"{money(row['amount'], row['currency'])}\n"
            f"  {row.get('description') or ''} · {who}"
        )
    return "\n".join(lines)


# ------------------------ Commands ------------------------

HELP_TEXT = """
<b>Dvir Finance v2.1 Parser</b>

Основні команди:

<code>ревізія 125000 грн у сейфі</code>

<code>виручка 25000 грн</code>
<code>витрата 1200 грн бензин</code>

<code>борг Bolena 5000 грн полікарбонат</code>
<code>Bolena оплатила 3000 грн у сейф</code>
<code>борги</code>
<code>борг Bolena</code>

Закриття дня одним повідомленням:
<code>виручка за день 48000 грн
у сейф 35000 грн
на картку Миколи 8000 грн
борг Bolena 5000 грн</code>

<code>каса</code>
<code>звіт</code>
<code>історія</code>
<code>видалити останню</code>
<code>версія</code>
<code>/id</code>
""".strip()


@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["id"])
def id_handler(message):
    bot.reply_to(
        message,
        f"ID групи/чату: <code>{message.chat.id}</code>\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
    )


@bot.message_handler(func=lambda m: True, content_types=["text"])
def text_handler(message):
    text = message.text.strip()
    low = text.lower()

    try:
        if low in ("версія", "версия", "version"):
            bot.reply_to(
                message,
                f"✅ <b>{VERSION}</b>\n"
                "Режим: центральний локальний парсер\n"
                "ШІ для запису: вимкнено",
            )
            return

        if low in ("каса", "баланс", "скільки грошей"):
            bot.reply_to(message, cash_summary(message.chat.id))
            return

        if low in ("звіт", "отчет"):
            bot.reply_to(message, report_text(message.chat.id))
            return

        if low in ("історія", "история"):
            bot.reply_to(message, history_text(message.chat.id))
            return

        if low in ("видалити останню", "удалить последнюю"):
            rows = sb_select(
                "cash_operations",
                {
                    "select": "*",
                    "telegram_chat_id": f"eq.{message.chat.id}",
                    "is_cancelled": "eq.false",
                    "order": "created_at.desc",
                    "limit": "1",
                },
            )
            if not rows:
                bot.reply_to(message, "Немає операції для видалення.")
                return
            row = rows[0]
            sb_update(
                "cash_operations",
                {"id": f"eq.{row['id']}"},
                {
                    "is_cancelled": True,
                    "cancelled_at": datetime.now(KYIV).isoformat(),
                    "cancellation_reason": f"Скасовано користувачем {message.from_user.id}",
                },
            )
            bot.reply_to(
                message,
                f"✅ Останню операцію скасовано: "
                f"{money(row['amount'], row['currency'])}",
            )
            return

        if handle_daily_closing(message, text):
            return
        if handle_revision(message, text):
            return
        if handle_debt_queries(message, text):
            return
        if handle_debt_create(message, text):
            return
        if handle_debt_payment(message, text):
            return
        if handle_context_debt_payment(message, text):
            return
        if handle_income_expense(message, text):
            return

        bot.reply_to(
            message,
            "Не зовсім зрозумів запис.\n\nНатисни /help або напиши, наприклад:\n"
            "<code>витрата 1200 грн бензин</code>",
        )

    except Exception as exc:
        log.exception("Помилка обробки повідомлення")
        bot.reply_to(
            message,
            "⚠️ Не вдалося записати операцію. "
            "Дані не втрачено. Спробуй ще раз або надішли скрін помилки.\n\n"
            f"<code>{str(exc)[:300]}</code>",
        )


if __name__ == "__main__":
    log.info("%s запущено", VERSION)
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        skip_pending=True,
        allowed_updates=["message"],
    )
