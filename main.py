import os
import re
import uuid
import logging
import json
import time
from types import SimpleNamespace
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests
import telebot

# ============================================================
# Dvir Finance Bot v3.1 — mode-based parser + safe AI confirmation
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
VERSION = "DvirFinance 4.2-INTEGRATED-CORE-20260724"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

# New single mode switch. Preferred Railway variable:
# DVIRFINANCE_MODE=STRICT | HYBRID | AI
# MODE is accepted as a shorter alias.
# Legacy STABLE_LOCAL_ONLY remains supported only when no new mode is set.
LEGACY_STABLE_LOCAL_ONLY = os.getenv("STABLE_LOCAL_ONLY", "false").lower() in (
    "1", "true", "yes", "on"
)
_mode_raw = (os.getenv("DVIRFINANCE_MODE") or os.getenv("MODE") or "").strip().upper()
if _mode_raw:
    APP_MODE = _mode_raw
else:
    APP_MODE = "STRICT" if LEGACY_STABLE_LOCAL_ONLY else "HYBRID"
if APP_MODE not in {"STRICT", "HYBRID", "AI"}:
    log.warning("Невідомий режим %s; використовую STRICT", APP_MODE)
    APP_MODE = "STRICT"

# Legacy AI_HELP_ENABLED can still forcibly disable AI, but cannot enable it in STRICT.
_legacy_ai_enabled = os.getenv("AI_HELP_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
AI_HELP_ENABLED = APP_MODE in {"HYBRID", "AI"} and _legacy_ai_enabled and bool(OPENAI_API_KEY)
PENDING_AI = {}
PENDING_ACTIONS = {}
PENDING_TTL_SECONDS = 900

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

CURRENCY_ALIASES = {
    "грн": "UAH", "uah": "UAH", "₴": "UAH", "гривень": "UAH",
    "гривні": "UAH", "гривня": "UAH",
    "дол": "USD", "долар": "USD", "долари": "USD", "доларів": "USD",
    "usd": "USD", "$": "USD",
    "євро": "EUR", "евро": "EUR", "eur": "EUR", "€": "EUR",
}

CURRENCY_SYMBOLS = {"UAH": "грн", "USD": "$", "EUR": "€"}


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


def sb_rpc(function_name: str, payload: dict):
    """Call a Supabase PostgreSQL function. Critical accounting changes live in DB transactions."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"
    response = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    if not response.ok:
        log.error("Supabase RPC %s: %s", function_name, response.text)
        raise RuntimeError(response.text)
    return response.json() if response.text else None


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
        r"(?P<currency>грн|гривень|гривні|гривня|uah|₴|"
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


def default_account_name(text: str) -> tuple[str, str]:
    low = text.lower()
    # У базі історично рахунок готівки має назву "Сейф".
    # Слова "каса", "готівка", "наличка" ведуть на той самий рахунок,
    # щоб не розділити старий і новий баланс на два рахунки.
    if (
        "сейф" in low
        or "кас" in low
        or "готів" in low
        or "налич" in low
    ):
        return "Сейф", "safe"

    card_match = re.search(
        r"(?:на|у|в)\s+(?:особисту\s+|фоп(?:івську)?\s+)?карт(?:ку|у|ці|ка)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if card_match:
        owner = normalize_text(card_match.group(1))
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


# ------------------------ Parser dictionaries ------------------------

DEBT_RETURN_RE = re.compile(
    r"\b(повернен(?:ня|о)?|повернув|повернула|повернули|"
    r"віддав|віддала|віддали|погасив|погасила|погасили|"
    r"закрив|закрила|закрили|приніс|принесла|принесли|"
    r"оплатив|оплатила|оплатили|заплатив|заплатила|заплатили)\b",
    flags=re.IGNORECASE,
)


def contains_debt_return_marker(text: str) -> bool:
    return bool(DEBT_RETURN_RE.search(text))


def first_keyword(text: str) -> str:
    """Перше слово є командою операції."""
    normalized = normalize_text(text).lower()
    return normalized.split(" ", 1)[0] if normalized else ""


def keyword_tail(text: str, keyword: str) -> str:
    normalized = normalize_text(text)
    if normalized.lower() == keyword.lower():
        return ""
    return normalized[len(keyword):].strip()


def description_around_amount(tail: str, match, fallback: str) -> str:
    before = normalize_text(tail[:match.start()])
    after = normalize_text(tail[match.end():])
    description = normalize_text(f"{before} {after}")
    return re.sub(r"^[-—:;, ]+|[-—:;, ]+$", "", description) or fallback


def handle_advance(message, text: str) -> bool:
    """Ключове слово «аванс» завжди означає надходження (+)."""
    if first_keyword(text) != "аванс":
        return False

    tail = keyword_tail(text, "аванс")
    amount, currency, match = extract_amount_currency(tail)
    description = description_around_amount(tail, match, "Аванс")
    account_name, _ = default_account_name(text)

    add_cash_operation(
        message, "income", amount, currency, description, account_name=account_name
    )
    display_account = "Каса" if account_name == "Сейф" else account_name
    bot.reply_to(
        message,
        f"✅ Аванс записано\n\n"
        f"➕ <b>{money(amount, currency)}</b>\n"
        f"📍 Зараховано: {display_account}\n"
        f"📝 {description}",
    )
    return True


# ------------------------ Old cash operations ------------------------

def add_cash_operation(
    message,
    operation_type: str,
    amount: Decimal,
    currency: str,
    description: str,
    account_name: str | None = None,
):
    if not account_name:
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


def extract_all_amounts_currency(text: str):
    """Read every explicit amount+currency pair, including compact input: 300$,250€."""
    pattern = re.compile(
        r"(?P<amount>\d[\d\s]*(?:[.,]\d{1,2})?)\s*"
        r"(?P<currency>грн|гривень|гривні|гривня|uah|₴|"
        r"доларів|долари|долар|дол|usd|\$|євро|евро|eur|€)",
        flags=re.IGNORECASE,
    )
    result = []
    for match in pattern.finditer(text):
        result.append((parse_amount(match.group("amount")), parse_currency(match.group("currency")), match))
    if not result:
        # Keep the established default: a number without currency means UAH.
        amount, currency, match = extract_amount_currency(text)
        result.append((amount, currency, match))
    return result


def handle_income_expense(message, text: str) -> bool:
    keyword = first_keyword(text)
    if keyword not in ("виручка", "витрата"):
        return False

    tail = keyword_tail(text, keyword)
    amounts = extract_all_amounts_currency(tail)
    account_name, _ = default_account_name(text)
    operation_type = "income" if keyword == "виручка" else "expense"
    operation_group = str(uuid.uuid4())

    # Remove all parsed sums from the note so a second currency is never silently left as text.
    description = tail
    for _, _, match in reversed(amounts):
        description = description[:match.start()] + " " + description[match.end():]
    description = normalize_text(re.sub(r"^[,;:\-]+|[,;:\-]+$", "", description))
    if not description:
        description = "Виручка" if keyword == "виручка" else "Витрата"

    payloads = []
    for amount, currency, _ in amounts:
        payload = operation_payload(
            message, operation_type, amount, currency, description, account_name=account_name
        )
        payload["operation_group"] = operation_group
        payloads.append(payload)

    # PostgREST inserts the whole JSON array in one database transaction.
    sb_insert("cash_operations", payloads)

    display_account = "Каса" if account_name == "Сейф" else account_name
    sign = "➕" if keyword == "виручка" else "➖"
    amounts_text = "\n".join(
        f"{sign} <b>{money(amount, currency)}</b>" for amount, currency, _ in amounts
    )
    title = "✅ Виручку записано" if keyword == "виручка" else "✅ Витрату записано"
    account_line = "Зараховано" if keyword == "виручка" else "Списано"
    bot.reply_to(
        message,
        f"{title}\n\n{amounts_text}\n"
        f"📍 {account_line}: {display_account}\n"
        f"📝 {description}",
    )
    return True


# ------------------------ Currency exchange ------------------------

def _currency_word_pattern() -> str:
    return r"грн|гривень|гривні|гривня|uah|₴|доларів|долари|долар|дол|usd|\$|євро|евро|eur|€"


def handle_exchange(message, text: str) -> bool:
    """Обмін є однією атомарною операцією: мінус вихідної валюти, плюс отриманої."""
    if first_keyword(text) != "обмін":
        return False

    tail = keyword_tail(text, "обмін")
    from_amount, from_currency, first_match = extract_amount_currency(tail)
    after = tail[first_match.end():].strip()

    # Варіант 1: обмін 10000 грн на 220 доларів
    explicit_to = re.search(
        rf"(?:^|\s)на\s+(?P<amount>\d[\d\s]*(?:[.,]\d{{1,2}})?)\s*"
        rf"(?P<currency>{_currency_word_pattern()})",
        after,
        flags=re.IGNORECASE,
    )

    rate_match = re.search(
        r"(?:курс|по)\s*(?:по\s*)?(?P<rate>\d[\d\s]*(?:[.,]\d{1,6})?)",
        after,
        flags=re.IGNORECASE,
    )

    if explicit_to:
        to_amount = parse_amount(explicit_to.group("amount"))
        to_currency = parse_currency(explicit_to.group("currency"))
        rate = (from_amount / to_amount).quantize(Decimal("0.000001"))
    else:
        target_match = re.search(
            rf"(?:^|\s)на\s+(?P<currency>{_currency_word_pattern()})",
            after,
            flags=re.IGNORECASE,
        )
        if not target_match:
            raise ValueError("Вкажи, на яку валюту міняємо. Наприклад: обмін 10000 грн на долари курс 45")
        if not rate_match:
            raise ValueError("Вкажи курс. Наприклад: обмін 10000 грн на долари курс 45")
        to_currency = parse_currency(target_match.group("currency"))
        rate = parse_amount(rate_match.group("rate"))
        # Курс розуміємо як кількість вихідної валюти за 1 одиницю отриманої.
        to_amount = (from_amount / rate).quantize(Decimal("0.01"))

    if from_currency == to_currency:
        raise ValueError("Вихідна й отримана валюти мають бути різними")

    account_name, account_type = default_account_name(text)
    from_account = get_or_create_account(message.chat.id, account_name, from_currency, account_type)
    to_account = get_or_create_account(message.chat.id, account_name, to_currency, account_type)

    result = sb_rpc("dvirfinance_exchange", {
        "p_chat_id": message.chat.id,
        "p_user_id": message.from_user.id,
        "p_username": message.from_user.username,
        "p_full_name": user_fields(message)["telegram_full_name"],
        "p_message_id": message.message_id,
        "p_operation_date": today_str(),
        "p_from_amount": float(from_amount),
        "p_from_currency": from_currency,
        "p_to_amount": float(to_amount),
        "p_to_currency": to_currency,
        "p_rate": float(rate),
        "p_from_account_id": from_account["id"],
        "p_from_account_name": account_name,
        "p_to_account_id": to_account["id"],
        "p_to_account_name": account_name,
        "p_description": normalize_text(text),
    })
    operation_id = result.get("operation_id") if isinstance(result, dict) else None
    display_account = "Каса" if account_name == "Сейф" else account_name
    bot.reply_to(
        message,
        "✅ <b>Обмін записано</b>"
        f"{f' · #{operation_id}' if operation_id else ''}\n\n"
        f"➖ {money(from_amount, from_currency)}\n"
        f"➕ <b>{money(to_amount, to_currency)}</b>\n"
        f"Курс: {rate.normalize()}\n"
        f"📍 Рахунок: {display_account}",
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
    if first_keyword(text) != "борг":
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
        query = normalize_text(customer_query).casefold()
        exact = [r for r in rows if normalize_text(r["customer_name"]).casefold() == query]
        if exact:
            return exact
        # Частковий пошук дозволяємо лише коли він веде до одного унікального імені.
        partial = [r for r in rows if query in normalize_text(r["customer_name"]).casefold()]
        names = {normalize_text(r["customer_name"]).casefold() for r in partial}
        return partial if len(names) == 1 else []
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
    """Повернення боргу. Перше слово обов'язково «повернення».

    Формат:
      повернення Болена 2000 грн в касу
      повернення Болена 2000 грн на картку Миколи
    """
    if first_keyword(text) != "повернення":
        return False

    rest = keyword_tail(text, "повернення")
    # Дозволяємо необов'язкове друге слово «борг/боргу».
    rest = re.sub(r"^(?:борг|боргу)\s+", "", rest, flags=re.IGNORECASE)

    amount, currency, amount_match = extract_amount_currency(rest)
    customer = normalize_text(rest[:amount_match.start()])
    customer = re.sub(r"\b(?:борг|боргу)\b", " ", customer, flags=re.IGNORECASE)
    customer = normalize_text(customer)
    if not customer:
        bot.reply_to(
            message,
            "Напиши ім’я: <code>повернення Болена 2000 грн в касу</code>",
        )
        return True

    debts = [
        d for d in find_open_debts(message.chat.id, customer)
        if d["currency"] == currency
    ]
    if not debts:
        bot.reply_to(
            message,
            f"Не знайшов відкритий борг для <b>{customer}</b> у валюті {currency}.",
        )
        return True

    outstanding_total = sum_rows(debts, "outstanding_amount")
    if amount > outstanding_total:
        bot.reply_to(
            message,
            f"⚠️ Борг становить {money(outstanding_total, currency)}, "
            f"а вказано {money(amount, currency)}.\nНічого не записано.",
        )
        return True

    account_name, account_type = default_account_name(text)
    account = get_or_create_account(message.chat.id, account_name, currency, account_type)
    destination_text = normalize_text(rest[amount_match.end():])

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
                "description": destination_text or f"Повернення боргу {debt['customer_name']}",
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
    display_account = "Каса" if account_name == "Сейф" else account_name
    bot.reply_to(
        message,
        f"✅ Повернення боргу записано\n\n"
        f"👤 {customer}\n"
        f"➖ Борг зменшено на: <b>{money(amount, currency)}</b>\n"
        f"📍 Гроші зараховано: {display_account}\n"
        f"📒 Залишок боргу: {money(left, currency)}",
    )
    return True



# ------------------------ Hybrid AI helper ------------------------

CANONICAL_KEYWORDS = {"виручка", "витрата", "борг", "повернення", "аванс", "ревізія", "обмін"}

def canonical_command_is_safe(command: str) -> bool:
    command = normalize_text(command)
    if not command or first_keyword(command) not in CANONICAL_KEYWORDS:
        return False
    if not re.search(r"\d", command):
        return False
    try:
        extract_amount_currency(command)
    except Exception:
        return False
    return len(command) <= 240

def extract_openai_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n".join(parts).strip()

def ai_suggest_canonical(text: str) -> str | None:
    if not AI_HELP_ENABLED or not OPENAI_API_KEY:
        return None
    prompt = f"""Перетвори повідомлення користувача на ОДНУ канонічну команду DvirFinance.
Дозволені перші слова і значення:
- виручка: плюс у касу/вказаний рахунок
- витрата: мінус із каси/вказаного рахунку
- борг: створити борг клієнта
- повернення: зменшити борг клієнта і зарахувати гроші у касу/на рахунок
- аванс: отриманий аванс, плюс у касу/на рахунок
- ревізія: фактичний залишок
- обмін: обмін однієї валюти на іншу з указаним курсом або двома сумами

Правила:
1) Поверни тільки один рядок без пояснень.
2) Не вигадуй суму, валюту, ім'я або рахунок.
3) Якщо валюта не вказана, використовуй грн.
4) Якщо для надходження/повернення рахунок не вказаний, використовуй «в касу».
5) Якщо неможливо однозначно зрозуміти, поверни НЕЗРОЗУМІЛО.

Повідомлення: {text}"""
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": OPENAI_MODEL, "input": prompt, "store": False, "max_output_tokens": 100},
        timeout=20,
    )
    if not response.ok:
        log.warning("OpenAI helper error: %s", response.text[:500])
        return None
    suggestion = extract_openai_text(response.json()).splitlines()[0].strip().strip("`\"")
    if suggestion.upper().startswith("НЕЗРОЗУМІЛО") or not canonical_command_is_safe(suggestion):
        return None
    return suggestion

def cleanup_pending_ai():
    cutoff = time.time() - PENDING_TTL_SECONDS
    for key in list(PENDING_AI):
        if PENDING_AI[key]["created"] < cutoff:
            PENDING_AI.pop(key, None)

def execute_canonical(message, text: str) -> bool:
    for handler in (
        handle_revision,
        handle_exchange,
        handle_debt_payment,
        handle_debt_create,
        handle_advance,
        handle_income_expense,
    ):
        if handler(message, text):
            return True
    return False

def offer_ai_suggestion(message, original_text: str) -> bool:
    suggestion = ai_suggest_canonical(original_text)
    if not suggestion:
        return False
    cleanup_pending_ai()
    token = uuid.uuid4().hex[:12]
    PENDING_AI[token] = {
        "created": time.time(),
        "chat_id": message.chat.id,
        "user_id": message.from_user.id,
        "message_id": message.message_id,
        "suggestion": suggestion,
        "original": original_text,
        "user": {
            "id": message.from_user.id,
            "username": message.from_user.username,
            "first_name": message.from_user.first_name,
            "last_name": message.from_user.last_name,
        },
    }
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Так, записати", callback_data=f"ai_yes:{token}"),
        telebot.types.InlineKeyboardButton("❌ Ні", callback_data=f"ai_no:{token}"),
    )
    bot.reply_to(
        message,
        "🤖 Я не записував операцію. Можливо, ти мав на увазі:\n\n"
        f"<code>{suggestion}</code>\n\nПідтвердити запис?",
        reply_markup=markup,
    )
    return True

@bot.callback_query_handler(func=lambda call: call.data.startswith(("ai_yes:", "ai_no:")))
def ai_confirmation_handler(call):
    cleanup_pending_ai()
    action, token = call.data.split(":", 1)
    pending = PENDING_AI.get(token)
    if not pending or pending["chat_id"] != call.message.chat.id:
        bot.answer_callback_query(call.id, "Пропозиція вже застаріла")
        return
    if pending["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "Підтвердити може лише автор повідомлення")
        return
    PENDING_AI.pop(token, None)
    if action == "ai_no":
        bot.answer_callback_query(call.id, "Скасовано")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    u = pending["user"]
    fake_message = SimpleNamespace(
        chat=call.message.chat,
        from_user=SimpleNamespace(**u),
        message_id=pending["message_id"],
        text=pending["suggestion"],
    )
    try:
        if execute_canonical(fake_message, pending["suggestion"]):
            bot.answer_callback_query(call.id, "Записано")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        else:
            bot.answer_callback_query(call.id, "Не вдалося виконати")
    except Exception as exc:
        log.exception("AI confirmation failed")
        bot.answer_callback_query(call.id, "Помилка запису")

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
            "is_cancelled": "eq.false",
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



# ------------------------ Reliable accounting core ------------------------

def operation_label(row: dict) -> str:
    labels = {
        "cash_operation": "Грошова операція",
        "debt": "Борг",
        "debt_payment": "Повернення боргу",
        "revision": "Ревізія",
        "transfer": "Переказ",
        "daily_closing": "Закриття дня",
        "reset": "Обнулення",
        "exchange": "Обмін",
    }
    return labels.get(row.get("entity_type"), row.get("entity_type") or "Операція")


def latest_operations_text(chat_id: int, limit: int = 10) -> str:
    limit = max(1, min(int(limit), 30))
    rows = sb_select(
        "accounting_operations",
        {
            "select": "id,created_at,entity_type,summary,is_cancelled,cancelled_at",
            "telegram_chat_id": f"eq.{chat_id}",
            "order": "id.desc",
            "limit": str(limit),
        },
    )
    if not rows:
        return "Історія операцій поки порожня."
    lines = ["🕘 <b>Останні операції</b>\n"]
    for row in rows:
        summary = row.get("summary") or {}
        amount = summary.get("amount")
        currency = summary.get("currency") or "UAH"
        description = summary.get("description") or summary.get("customer_name") or ""
        account = summary.get("account_name") or summary.get("destination_account_name") or ""
        if row.get("entity_type") == "exchange":
            description = (
                f"{money(summary.get('from_amount'), summary.get('from_currency') or 'UAH')} → "
                f"{money(summary.get('to_amount'), summary.get('to_currency') or 'USD')} · "
                f"курс {summary.get('rate')}"
            )
        created = str(row.get("created_at") or "").replace("T", " ")[:16]
        status = " — <b>СКАСОВАНО</b>" if row.get("is_cancelled") else ""
        detail = operation_label(row)
        if amount is not None:
            detail += f" · {money(amount, currency)}"
        if description:
            detail += f" · {description}"
        if account:
            detail += f" → {'Каса' if account == 'Сейф' else account}"
        lines.append(f"<b>#{row['id']}</b>{status}\n{created} · {detail}")
    return "\n\n".join(lines)


def find_operation(chat_id: int, operation_id: int | None = None):
    params = {
        "select": "id,created_at,entity_type,summary,is_cancelled,cancelled_at",
        "telegram_chat_id": f"eq.{chat_id}",
        "order": "id.desc",
        "limit": "1",
    }
    if operation_id is not None:
        params["id"] = f"eq.{operation_id}"
    else:
        params["is_cancelled"] = "eq.false"
    rows = sb_select("accounting_operations", params)
    return rows[0] if rows else None


def cleanup_pending_actions():
    cutoff = time.time() - PENDING_TTL_SECONDS
    for key in list(PENDING_ACTIONS):
        if PENDING_ACTIONS[key].get("created", 0) < cutoff:
            PENDING_ACTIONS.pop(key, None)


def request_cancel(message, operation_id: int | None = None):
    row = find_operation(message.chat.id, operation_id)
    if not row:
        bot.reply_to(message, "Не знайшов активну операцію для скасування.")
        return
    if row.get("is_cancelled"):
        bot.reply_to(message, f"Операція #{row['id']} вже скасована.")
        return
    token = uuid.uuid4().hex[:12]
    cleanup_pending_actions()
    PENDING_ACTIONS[token] = {
        "kind": "cancel",
        "created": time.time(),
        "chat_id": message.chat.id,
        "user_id": message.from_user.id,
        "operation_id": row["id"],
    }
    summary = row.get("summary") or {}
    detail = operation_label(row)
    if summary.get("amount") is not None:
        detail += f" — {money(summary['amount'], summary.get('currency') or 'UAH')}"
    if summary.get("description") or summary.get("customer_name"):
        detail += f"\n{summary.get('description') or summary.get('customer_name')}"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Підтвердити скасування", callback_data=f"cancel_yes:{token}"),
        telebot.types.InlineKeyboardButton("❌ Відміна", callback_data=f"cancel_no:{token}"),
    )
    bot.reply_to(message, f"Скасувати операцію <b>#{row['id']}</b>?\n\n{detail}\n\nСтан бази буде повернуто так, ніби операції не було.", reply_markup=markup)


def request_reset(message):
    token = uuid.uuid4().hex[:12]
    cleanup_pending_actions()
    PENDING_ACTIONS[token] = {
        "kind": "reset_stage1",
        "created": time.time(),
        "chat_id": message.chat.id,
        "user_id": message.from_user.id,
    }
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("⚠️ Так, продовжити", callback_data=f"reset_yes:{token}"),
        telebot.types.InlineKeyboardButton("❌ Відміна", callback_data=f"reset_no:{token}"),
    )
    bot.reply_to(
        message,
        "⚠️ <b>Обнулити облік?</b>\n\nБудуть обнулені рахунки, активні борги, аванси й перекази. Історія залишиться.\n\nЦе перше підтвердження.",
        reply_markup=markup,
    )


def finish_reset_if_expected(message, text: str) -> bool:
    cleanup_pending_actions()
    candidates = [
        (token, data) for token, data in PENDING_ACTIONS.items()
        if data.get("kind") == "reset_stage2"
        and data.get("chat_id") == message.chat.id
        and data.get("user_id") == message.from_user.id
    ]
    if not candidates:
        return False
    token, pending = max(candidates, key=lambda item: item[1]["created"])
    if text != "ОБНУЛИТИ":
        bot.reply_to(message, "Для другого підтвердження потрібно написати точно: <code>ОБНУЛИТИ</code>")
        return True
    result = sb_rpc("dvirfinance_reset", {
        "p_chat_id": message.chat.id,
        "p_user_id": message.from_user.id,
        "p_reason": "Подвійне підтвердження в Telegram",
    })
    PENDING_ACTIONS.pop(token, None)
    op_id = result.get("operation_id") if isinstance(result, dict) else None
    bot.reply_to(message, f"✅ Облік обнулено. Історія збережена.{f' Операція #{op_id}.' if op_id else ''}")
    return True


@bot.callback_query_handler(func=lambda call: call.data.startswith(("cancel_yes:", "cancel_no:", "reset_yes:", "reset_no:")))
def accounting_confirmation_handler(call):
    cleanup_pending_actions()
    action, token = call.data.split(":", 1)
    pending = PENDING_ACTIONS.get(token)
    if not pending or pending.get("chat_id") != call.message.chat.id:
        bot.answer_callback_query(call.id, "Підтвердження застаріло")
        return
    if pending.get("user_id") != call.from_user.id:
        bot.answer_callback_query(call.id, "Підтвердити може лише автор команди")
        return
    if action in ("cancel_no", "reset_no"):
        PENDING_ACTIONS.pop(token, None)
        bot.answer_callback_query(call.id, "Скасовано")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    if action == "cancel_yes":
        try:
            result = sb_rpc("dvirfinance_cancel_operation", {
                "p_chat_id": call.message.chat.id,
                "p_operation_id": pending["operation_id"],
                "p_user_id": call.from_user.id,
                "p_reason": "Скасовано через Telegram",
            })
            PENDING_ACTIONS.pop(token, None)
            bot.answer_callback_query(call.id, "Операцію скасовано")
            bot.edit_message_text(
                f"✅ Операцію <b>#{pending['operation_id']}</b> скасовано.\nСтан бази відновлено.",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception as exc:
            log.exception("Cancellation failed")
            bot.answer_callback_query(call.id, "Скасування не виконано")
            bot.send_message(call.message.chat.id, f"⚠️ Операцію не скасовано. База не змінена.\n<code>{str(exc)[:300]}</code>")
        return
    if action == "reset_yes":
        pending["kind"] = "reset_stage2"
        pending["created"] = time.time()
        bot.answer_callback_query(call.id, "Перше підтвердження прийнято")
        bot.edit_message_text(
            "⚠️ <b>ОСТАТОЧНЕ ПІДТВЕРДЖЕННЯ</b>\n\nНапишіть точно великими літерами:\n<code>ОБНУЛИТИ</code>\n\nКоманда діє 15 хвилин.",
            call.message.chat.id,
            call.message.message_id,
        )


# ------------------------ Commands ------------------------

HELP_TEXT = """
<b>DvirFinance — правило першого слова</b>

Перше слово завжди визначає операцію:

<code>виручка 25000 грн склад</code>
➕ гроші в касу

<code>витрата 1200 грн бензин</code>
➖ гроші з каси

<code>борг Болена 7000 грн товар</code>
📒 створює борг

<code>повернення Болена 2000 грн в касу</code>
📒 зменшує борг і додає гроші в касу

<code>повернення Болена 2000 грн на картку Миколи</code>
📒 зменшує борг і додає гроші на картку

<code>аванс 1000$ натяжні потолки</code>
➕ отриманий аванс у касу

<code>ревізія 125000 грн у касі</code>

<code>обмін 10000 грн на долари курс 45</code>
💱 мінус гривні та плюс долари однією операцією
<code>борги</code>
<code>каса</code>
<code>звіт</code>
<code>останні</code>
<code>скасувати</code>
<code>скасувати #127</code>
<code>обнулити</code>
<code>версія</code>
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
            ai_status = "увімкнено з підтвердженням" if AI_HELP_ENABLED else "вимкнено"
            bot.reply_to(
                message,
                f"✅ <b>{VERSION}</b>\n"
                f"Режим: <b>{APP_MODE}</b>\n"
                "Правило: ключове слово першим\n"
                f"ШІ-помічник: {ai_status}\n"
                "ШІ самостійно нічого не записує",
            )
            return

        if low in ("каса", "баланс", "скільки грошей"):
            bot.reply_to(message, cash_summary(message.chat.id))
            return

        if low in ("звіт", "отчет"):
            bot.reply_to(message, report_text(message.chat.id))
            return

        if finish_reset_if_expected(message, text):
            return

        latest_match = re.fullmatch(r"(?:останні|історія)(?:\s+(\d{1,2}))?", low)
        if latest_match:
            bot.reply_to(message, latest_operations_text(message.chat.id, int(latest_match.group(1) or 10)))
            return

        cancel_match = re.fullmatch(r"скасувати(?:\s+#?(\d+))?", low)
        if cancel_match:
            request_cancel(message, int(cancel_match.group(1)) if cancel_match.group(1) else None)
            return

        if low == "обнулити":
            request_reset(message)
            return

        if handle_revision(message, text):
            return
        if handle_debt_queries(message, text):
            return
        # STRICT: тільки локальні перевірені правила.
        # HYBRID: спочатку локальні правила, потім AI-підказка з підтвердженням.
        # AI: спочатку AI-підказка з підтвердженням; якщо AI не допоміг — локальні правила.
        if APP_MODE == "AI":
            if offer_ai_suggestion(message, text):
                return
            if execute_canonical(message, text):
                return
        else:
            if execute_canonical(message, text):
                return
            if APP_MODE == "HYBRID" and offer_ai_suggestion(message, text):
                return

        bot.reply_to(
            message,
            "Не зрозумів перше ключове слово. Дані не записував.\n\n"
            "Використовуй: <b>виручка, витрата, борг, повернення, аванс, ревізія, обмін</b>.\n"
            "Приклад: <code>повернення Болена 2000 грн в касу</code>",
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
        allowed_updates=["message", "callback_query"],
    )
