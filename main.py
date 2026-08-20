import os
import re
import uuid
import logging
import json
import time
from types import SimpleNamespace
from datetime import datetime, date, timedelta
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
VERSION = "DvirFinance 6.2-FINAL-20260820"
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or os.getenv("OPENAI_TOKEN"))
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()
OPENAI_PROJECT = (os.getenv("OPENAI_PROJECT") or os.getenv("OPENAI_PROJECT_ID") or "").strip()
OPENAI_ORG = (os.getenv("OPENAI_ORG") or os.getenv("OPENAI_ORGANIZATION") or "").strip()
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "30"))
AI_MAX_OPTIONS = max(1, min(3, int(os.getenv("AI_MAX_OPTIONS", "3"))))

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
    # Старий STABLE_LOCAL_ONLY більше не вимикає вже підключений ШІ.
    # Якщо ключ є — автоматично працюємо в HYBRID; без ключа — STRICT.
    APP_MODE = "HYBRID" if OPENAI_API_KEY else "STRICT"
if APP_MODE not in {"STRICT", "HYBRID", "AI"}:
    log.warning("Невідомий режим %s; використовую STRICT", APP_MODE)
    APP_MODE = "STRICT"

# Legacy AI_HELP_ENABLED can still forcibly disable AI, but cannot enable it in STRICT.
_legacy_ai_enabled = os.getenv("AI_HELP_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
AI_HELP_ENABLED = APP_MODE in {"HYBRID", "AI"} and _legacy_ai_enabled and bool(OPENAI_API_KEY)
PENDING_AI = {}
PENDING_DIRECT = {}
PENDING_ACTIONS = {}
PENDING_TTL_SECONDS = 900

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



EXPENSE_CATEGORY_RULES = {
    # Specific purpose wins over a document word. Example: "бензин, накладна 428"
    # is Пальне, not Товар. "Накладна 428" alone still falls back to Товар.
    "Зарплата": ("зарплат", "зп", "аванс праців", "оплата праці", "розрахунок праців"),
    "Пальне": ("бензин", "дизел", "соляр", "пальне", "паливо", "заправ"),
    "Доставка": ("достав", "перевез", "нова пошта", "логіст", "транспорт"),
    "Хімія": ("хімія", "химия", "хімікат", "реагент"),
    "Оренда": ("оренд",),
    "Податки": ("подат", "єсв", "єдиний податок", "пдв"),
    "Комунальні": ("комунал", "електро", "світло", "вода", "газ"),
    "Ремонт": ("ремонт", "запчаст", "сервіс"),
    "Реклама": ("реклам", "маркетинг", "таргет"),
    "Товар": ("за товар", "товар", "закуп", "постачаль", "накладн", "прихідн", "приф"),
}

def expense_details(text: str) -> dict:
    low = text.lower()
    category = "Інше"
    for name, words in EXPENSE_CATEGORY_RULES.items():
        if any(word in low for word in words):
            category = name
            break
    invoice = None
    patterns = [
        r"(?:накладн(?:а|ої|ій|ою)?|нк|invoice)\s*[№#:]?\s*([a-zа-яіїєґ0-9\-/]+)",
        r"(?:прихідн(?:а|ої|ій)?|приф(?:і|а)?)\s*[№#:]?\s*([a-zа-яіїєґ0-9\-/]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, low, flags=re.IGNORECASE)
        if m:
            invoice = m.group(1).upper()
            break
    return {"category": category, "invoice": invoice}

def clean_expense_comment(raw_description: str) -> str:
    """Keep the human purpose/comment, but remove technical account and invoice tokens."""
    text = normalize_text(raw_description)
    # Account phrases are accounting metadata, not the spending comment.
    patterns = [
        r"(?:\bз\s+|\bіз\s+|\bзі\s+|\bна\s+|\bз\s+рахунку\s+|\bз\s+карти\s+|\bз\s+картки\s+)?(?:каса|каси|касу|сейф|сейфу|готівка|готівки|готівку|наличка)",
        r"(?:\bз\s+|\bіз\s+|\bзі\s+|\bна\s+)?(?:картка|картки|карту|карта|карти|карточка|карточки|карточку)\s+(?:миколи|микола|андрія|андрій|андрея)",
        r"(?:\bз\s+|\bіз\s+|\bзі\s+|\bна\s+)?(?:південний|південного|южний|южного|южный)\s*(?:банк)?\s*(?:миколи|микола|андрія|андрій|андрея)?",
        r"(?:\bз\s+|\bіз\s+|\bзі\s+|\bна\s+)?(?:приват|приватбанк)\s*(?:миколи|микола|андрія|андрій|андрея)?",
        r"(?:\bз\s+|\bіз\s+|\bзі\s+|\bна\s+)?\bфоп\b\s*(?:миколи|микола|андрія|андрій|андрея)?",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    # Invoice is stored in its own field inside description, so do not duplicate it in comment.
    text = re.sub(
        r"(?:накладн(?:а|ої|ій|ою)?|нк|invoice|прихідн(?:а|ої|ій)?|приф(?:і|а)?)\s*[№#:]?\s*[a-zа-яіїєґ0-9\-/]+",
        " ", text, flags=re.IGNORECASE,
    )
    text = normalize_text(re.sub(r"^[,;:\-]+|[,;:\-]+$", "", text))
    return text


def structured_expense_description(raw_description: str) -> tuple[str, dict]:
    details = expense_details(raw_description)
    comment = clean_expense_comment(raw_description) or details['category']
    details['comment'] = comment
    parts = [f"Категорія: {details['category']}"]
    if details.get("invoice"):
        parts.append(f"Накладна: {details['invoice']}")
    parts.append(f"Коментар: {comment}")
    return " | ".join(parts), details

def income_preview_text(command: str) -> str:
    """Preview every currency in one revenue message before anything is saved."""
    try:
        tail = keyword_tail(command, "виручка")
        amounts = extract_all_amounts_currency(tail)
        account_name, _ = default_account_name(command)
        description = tail
        for _, _, match in reversed(amounts):
            description = description[:match.start()] + " " + description[match.end():]
        description = normalize_text(re.sub(r"^[,;:\-]+|[,;:\-]+$", "", description))
        display_account = "Каса" if account_name == "Сейф" else account_name
        lines = ["🧾 <b>Перевір виручку</b>", ""]
        lines += [f"➕ <b>{money(a, c)}</b>" for a, c, _ in amounts]
        lines.append(f"📍 Зарахувати: <b>{display_account}</b>")
        if description and description.casefold() != "виручка":
            lines.append(f"📝 Джерело/коментар: {description}")
        lines.append("\nНічого ще не записано.")
        return "\n".join(lines)
    except Exception:
        return "🧾 <b>Перевір операцію</b>\n\n" + f"<code>{command}</code>" + "\n\nНічого ще не записано."


def expense_preview_text(command: str) -> str:
    try:
        tail = keyword_tail(command, "витрата")
        amounts = extract_all_amounts_currency(tail)
        account_name, _ = default_account_name(command)
        description = tail
        for _, _, match in reversed(amounts):
            description = description[:match.start()] + " " + description[match.end():]
        description = normalize_text(re.sub(r"^[,;:\-]+|[,;:\-]+$", "", description))
        details = expense_details(description)
        comment = clean_expense_comment(description) or details['category']
        display_account = "Каса" if account_name == "Сейф" else account_name
        lines = ["🧾 <b>Перевір витрату</b>", ""]
        lines += [f"➖ <b>{money(a, c)}</b>" for a,c,_ in amounts]
        lines.append(f"💳 Рахунок: <b>{display_account}</b>")
        lines.append(f"📂 Категорія: <b>{details['category']}</b>")
        if details.get('invoice'):
            lines.append(f"📄 Накладна: <b>{details['invoice']}</b>")
        lines.append(f"📝 Коментар: {comment}")
        lines.append("\nНічого ще не записано.")
        return "\n".join(lines)
    except Exception:
        return "🧾 <b>Перевір операцію</b>\n\n" + f"<code>{command}</code>" + "\n\nНічого ще не записано."

def expense_report_text(chat_id: int, category_filter: str | None = None) -> str:
    rows = sb_select("cash_operations", {
        "select": "created_at,amount,currency,description,account_name",
        "telegram_chat_id": f"eq.{chat_id}",
        "operation_type": "eq.expense",
        "is_cancelled": "eq.false",
        "order": "created_at.desc",
        "limit": "500",
    })
    totals = {}
    recent = []
    for row in rows:
        desc = row.get("description") or ""
        m = re.search(r"Категорія:\s*([^|]+)", desc, flags=re.IGNORECASE)
        cat = normalize_text(m.group(1)) if m else expense_details(desc)["category"]
        if category_filter and category_filter.lower() not in cat.lower():
            continue
        key=(cat,row.get("currency") or "UAH")
        totals[key]=totals.get(key,Decimal("0"))+Decimal(str(row.get("amount") or 0))
        if len(recent)<10:
            inv=re.search(r"Накладна:\s*([^|]+)",desc,flags=re.IGNORECASE)
            cm=re.search(r"Коментар:\s*([^|]+)",desc,flags=re.IGNORECASE)
            comment = normalize_text(cm.group(1)) if cm else clean_expense_comment(desc)
            recent.append((row,cat,normalize_text(inv.group(1)) if inv else None,comment))
    if not totals:
        return "Витрат за цим запитом не знайдено."
    title = f"📊 <b>Витрати — {category_filter}</b>" if category_filter else "📊 <b>Витрати за категоріями</b>"
    lines=[title,""]
    cats=sorted(set(cat for cat,_ in totals))
    for cat in cats:
        vals=[money(totals[(cat,c)],c) for c in ("UAH","USD","EUR") if (cat,c) in totals]
        lines.append(f"• <b>{cat}</b>: " + ", ".join(vals))
    lines.append("\n<b>Останні витрати:</b>")
    for row,cat,inv,comment in recent:
        account = row.get('account_name') or 'Каса'
        detail=f" · накладна {inv}" if inv else ""
        note=f" · {comment}" if comment else ""
        lines.append(f"• {money(row.get('amount'),row.get('currency') or 'UAH')} · {cat}{detail}{note} · {account}")
    return "\n".join(lines)

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
        r"(?P<currency>гривень|гривні|гривня|грн|гр|uah|₴|"
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


def canonical_account_name(raw_name: str) -> str:
    """Normalize common Ukrainian/Russian account-name variants to one canonical name."""
    name = normalize_text(raw_name)
    low = name.lower()
    replacements = {
        "карточка": "Картка",
        "карта": "Картка",
        "картка": "Картка",
        "южний": "Південний",
        "южный": "Південний",
        "південний": "Південний",
        "приватбанк": "Приват",
        "приват": "Приват",
        "фоп": "ФОП",
    }
    owner = None
    if "микол" in low or "никол" in low:
        owner = "Миколи"
    elif "андр" in low:
        owner = "Андрія"

    if any(x in low for x in ("півден", "южн")):
        return f"Південний {owner}" if owner else "Південний"
    if "приват" in low:
        return f"Приват {owner}" if owner else "Приват"
    if "фоп" in low:
        return f"ФОП {owner}" if owner else "ФОП"
    if any(x in low for x in ("картк", "карточ", "карта")):
        return f"Картка {owner}" if owner else "Картка"
    if low in ("сейф", "каса", "готівка", "наличка"):
        return "Сейф"
    return name


def default_account_name(text: str) -> tuple[str, str]:
    low = text.lower()

    # Detect named bank accounts anywhere in the phrase, not only after "на картку".
    bank_patterns = [
        (r"(?:південн(?:ий|ого|ому)|южн(?:ий|ого|ому)|южный)", "bank"),
        (r"приват(?:банк)?", "bank"),
        (r"\bфоп\b", "fop_account"),
        (r"карт(?:а|и|ка|ки|ку|ці|кою|у)|карточ(?:ка|ки|ку|ке|кой)|\bкарта\b", "personal_card"),
    ]
    for pattern, account_type in bank_patterns:
        if re.search(pattern, low, flags=re.IGNORECASE):
            owner = None
            if re.search(r"микол|никол", low):
                owner = "Миколи"
            elif re.search(r"андр", low):
                owner = "Андрія"

            if re.search(r"півден|южн", low):
                return (f"Південний {owner}" if owner else "Південний", account_type)
            if "приват" in low:
                return (f"Приват {owner}" if owner else "Приват", account_type)
            if re.search(r"\bфоп\b", low):
                return (f"ФОП {owner}" if owner else "ФОП", account_type)
            return (f"Картка {owner}" if owner else "Картка", account_type)

    # Physical cash account.
    if "сейф" in low or "кас" in low or "готів" in low or "налич" in low:
        return "Сейф", "safe"

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
        r"(?P<currency>гривень|гривні|гривня|грн|гр|uah|₴|"
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
    expense_meta = None
    if operation_type == "expense":
        description, expense_meta = structured_expense_description(description)

    # Витрата не може створювати від’ємний залишок.
    if operation_type == "expense":
        for amount, currency, _ in amounts:
            available = current_account_balance(message.chat.id, account_name, currency)
            if amount > available:
                display_account = "Каса" if account_name == "Сейф" else account_name
                raise ValueError(
                    f"Недостатньо коштів на рахунку {display_account}. "
                    f"Доступно: {money(available, currency)}; потрібно: {money(amount, currency)}. "
                    "Операцію не записано."
                )

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
    extra = ""
    if operation_type == "expense" and expense_meta:
        extra = f"\n📂 Категорія: {expense_meta['category']}"
        if expense_meta.get('invoice'):
            extra += f"\n📄 Накладна: {expense_meta['invoice']}"
        if expense_meta.get('comment'):
            extra += f"\n📝 Коментар: {expense_meta['comment']}"
    bot.reply_to(
        message,
        f"{title}\n\n{amounts_text}\n"
        f"📍 {account_line}: {display_account}"
        f"{extra}" + (f"\n📝 {description}" if operation_type == "income" else ""),
    )
    return True



def handle_transfer(message, text: str) -> bool:
    """Move money between own accounts without changing the overall total."""
    if first_keyword(text) != "переказ":
        return False

    tail = keyword_tail(text, "переказ")
    amount, currency, match = extract_amount_currency(tail)
    rest = normalize_text(tail[:match.start()] + " " + tail[match.end():])
    m = re.search(r"(?:^|\s)з\s+(.+?)\s+на\s+(.+)$", rest, flags=re.IGNORECASE)
    if not m:
        raise ValueError("Напиши: переказ 5000 грн з Каси на Картку Андрія")

    source_raw, destination_raw = normalize_text(m.group(1)), normalize_text(m.group(2))
    source_name, _ = default_account_name(source_raw)
    destination_name, _ = default_account_name(destination_raw)
    if source_name == destination_name:
        raise ValueError("Рахунок списання і рахунок зарахування мають бути різними")

    available = current_account_balance(message.chat.id, source_name, currency)
    if amount > available:
        source_display = "Каса" if source_name == "Сейф" else source_name
        raise ValueError(
            f"Недостатньо коштів на рахунку {source_display}. "
            f"Доступно: {money(available, currency)}; потрібно: {money(amount, currency)}."
        )

    group = str(uuid.uuid4())
    description = f"Переказ з {'Каса' if source_name == 'Сейф' else source_name} " \
                  f"на {'Каса' if destination_name == 'Сейф' else destination_name}"
    out_payload = operation_payload(message, "transfer_out", amount, currency, description, source_name)
    in_payload = operation_payload(message, "transfer_in", amount, currency, description, destination_name)
    out_payload["operation_group"] = group
    in_payload["operation_group"] = group
    sb_insert("cash_operations", [out_payload, in_payload])

    bot.reply_to(
        message,
        "✅ Переказ записано\n\n"
        f"↘️ {money(amount, currency)} — {'Каса' if source_name == 'Сейф' else source_name}\n"
        f"↗️ {money(amount, currency)} — {'Каса' if destination_name == 'Сейф' else destination_name}",
    )
    return True


# ------------------------ Currency exchange ------------------------

def _currency_word_pattern() -> str:
    return r"гривень|гривні|гривня|грн|гр|uah|₴|доларів|долари|долар|дол|usd|\$|євро|евро|eur|€"


def calculate_exchange_amount(from_amount: Decimal, from_currency: str, to_currency: str, rate: Decimal) -> Decimal:
    """Calculate target amount using the bot's documented rate convention."""
    if rate <= 0:
        raise ValueError("Курс має бути більшим за нуль")
    if from_currency == to_currency:
        raise ValueError("Вихідна й отримана валюти мають бути різними")
    if from_currency == "UAH" and to_currency != "UAH":
        return (from_amount / rate).quantize(Decimal("0.01"))
    return (from_amount * rate).quantize(Decimal("0.01"))


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
        # Напрямок розрахунку залежить від валют:
        # гривні → іноземна валюта: ділимо на курс;
        # іноземна валюта → гривні: множимо на курс;
        # іноземна → іноземна: курс означає кількість цільової валюти
        # за 1 одиницю вихідної, тому множимо.
        to_amount = calculate_exchange_amount(from_amount, from_currency, to_currency, rate)

    if from_currency == to_currency:
        raise ValueError("Вихідна й отримана валюти мають бути різними")

    account_name, account_type = default_account_name(text)
    available = current_account_balance(message.chat.id, account_name, from_currency)
    if from_amount > available:
        display_account = "Каса" if account_name == "Сейф" else account_name
        raise ValueError(
            f"Недостатньо коштів на рахунку {display_account}. "
            f"Доступно: {money(available, from_currency)}; потрібно: {money(from_amount, from_currency)}. "
            "Обмін не записано."
        )

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
    if first_keyword(text) not in ("ревізія", "ревизия", "залишок", "остаток"):
        return False

    amount, currency, match = extract_amount_currency(text)
    tail = normalize_text(text[match.end():])
    account_name, account_type = default_account_name(tail)
    calculated_before = current_account_balance(message.chat.id, account_name, currency)
    difference = amount - calculated_before
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
            "calculated_amount": float(calculated_before),
            "difference_amount": float(difference),
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
        f"Було за обліком: {money(calculated_before, currency)}\n"
        f"Коригування: {money(difference, currency)}\n"
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


def parse_debt_command(text: str):
    """Parse both natural debt orders:
    - борг Болена 5000 грн товар
    - борг 5000 грн Болена товар

    Bank/card words are never required for creating a debt.
    """
    if first_keyword(text) != "борг":
        raise ValueError("Команда має починатися словом борг")
    rest = keyword_tail(text, "борг")
    amount, currency, match = extract_amount_currency(rest)
    before = normalize_text(rest[:match.start()])
    after = normalize_text(rest[match.end():])

    if before:
        customer = before
        description = after
    else:
        # Amount-first form. First token/group after the amount is the customer.
        # Optional description follows it. Names may contain two words, but common
        # expense/account words mark the start of the description.
        tokens = after.split()
        if not tokens:
            customer, description = "", ""
        else:
            description_markers = {
                "товар", "полікарбонат", "підвіконня", "накладна", "згідно",
                "за", "боргом", "рахунок", "замовлення", "доставка"
            }
            split_at = len(tokens)
            for i, token in enumerate(tokens[1:], 1):
                if token.casefold() in description_markers:
                    split_at = i
                    break
            # Default to one-word customer in amount-first shorthand; preserve
            # two-word names when the second word looks like a patronymic/surname.
            if split_at == len(tokens) and len(tokens) > 1:
                second = tokens[1].casefold()
                patronymic_suffixes = ("ович", "евич", "йович", "івна", "ївна", "овна", "енко", "чук", "юк", "ук")
                split_at = 2 if second.endswith(patronymic_suffixes) else 1
            customer = normalize_text(" ".join(tokens[:split_at]))
            description = normalize_text(" ".join(tokens[split_at:]))

    # A debt is a receivable record, not a cash-account operation. Strip accidental
    # account-only tails from the customer only when no real name remains.
    if customer.casefold() in {"каса", "картка", "карта", "південний", "приват", "фоп", "банк"}:
        customer = ""
    return customer, amount, currency, description


def handle_debt_create(message, text: str) -> bool:
    if first_keyword(text) != "борг":
        return False

    customer, amount, currency, description = parse_debt_command(text)

    if not customer:
        bot.reply_to(message, "Напиши ім’я боржника: <code>борг Болена 25000 грн</code>")
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


def _person_name_key(value: str) -> str:
    """Stable comparison key for debtor names without changing stored display text.

    Handles capitalization and common Ukrainian/Russian case endings:
    Наталя/Наталі/Наталю, Болена/Болени/Болену, Боря/борі/борю.
    This is deliberately conservative: it never auto-merges different multi-word names.
    """
    value = normalize_text(value).casefold().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-яіїєґ' -]+", "", value)
    parts = [p for p in value.split() if p]
    result = []
    for part in parts:
        token = part.strip("'-")
        if len(token) >= 5:
            # Longer endings first. Keep at least four letters to avoid overmatching.
            for ending in (
                "ями", "ами", "ові", "еві", "єві", "ого", "ому", "ему",
                "ою", "ею", "єю", "ові", "еві", "ів", "їв",
                "а", "я", "і", "ї", "и", "у", "ю", "е", "є", "о",
            ):
                if token.endswith(ending) and len(token) - len(ending) >= 4:
                    token = token[:-len(ending)]
                    break
        result.append(token)
    return " ".join(result)


def _matched_debt_names(rows, customer_query: str):
    query_text = normalize_text(customer_query)
    query_fold = query_text.casefold()
    query_key = _person_name_key(query_text)

    # 1. Exact text, ignoring case and spaces.
    exact_names = {
        normalize_text(r["customer_name"]).casefold()
        for r in rows
        if normalize_text(r["customer_name"]).casefold() == query_fold
    }
    if exact_names:
        return exact_names

    # 2. Same conservative grammatical key. This merges case forms, not arbitrary people.
    keyed_names = {
        normalize_text(r["customer_name"]).casefold()
        for r in rows
        if _person_name_key(r["customer_name"]) == query_key and query_key
    }
    if keyed_names:
        return keyed_names

    # 3. Unique partial textual match only.
    partial_names = {
        normalize_text(r["customer_name"]).casefold()
        for r in rows
        if query_fold in normalize_text(r["customer_name"]).casefold()
        or normalize_text(r["customer_name"]).casefold() in query_fold
    }
    return partial_names if len(partial_names) == 1 else set()


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
        matched_names = _matched_debt_names(rows, customer_query)
        return [
            r for r in rows
            if normalize_text(r["customer_name"]).casefold() in matched_names
        ]
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
    canonical_customer = normalize_text(debts[0]["customer_name"])
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
        f"👤 {canonical_customer}\n"
        f"➖ Борг зменшено на: <b>{money(amount, currency)}</b>\n"
        f"📍 Гроші зараховано: {display_account}\n"
        f"📒 Залишок боргу: {money(left, currency)}",
    )
    return True



# ------------------------ Hybrid AI helper ------------------------

CANONICAL_KEYWORDS = {"виручка", "витрата", "борг", "повернення", "аванс", "ревізія", "залишок", "обмін", "переказ"}



def local_normalize_command(text: str) -> str:
    """Cheap deterministic normalization before AI: preserve first-word intent and fix common forms."""
    command = normalize_text(text)
    if not command:
        return command
    first, *rest = command.split(" ", 1)
    aliases = {
        "витрати": "витрата",
        "виручки": "виручка",
        "перекинути": "переказ",
        "перекинув": "переказ",
        "перевести": "переказ",
        "перевів": "переказ",
    }
    first = aliases.get(first.lower(), first.lower())
    tail = rest[0] if rest else ""
    tail = re.sub(r"(?<=\d)\s*гр\b", " грн", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\bкарточк(?:а|и|у|ою|е|ой)\b", "картка", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\bкарти\b", "картки", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\bпівденн(?:ій|ьому|ого|им)\b", "Південний", tail, flags=re.IGNORECASE)
    return normalize_text(f"{first} {tail}")


def openai_headers() -> dict:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    if OPENAI_PROJECT:
        headers["OpenAI-Project"] = OPENAI_PROJECT
    if OPENAI_ORG:
        headers["OpenAI-Organization"] = OPENAI_ORG
    return headers


def _openai_response(prompt: str, max_output_tokens: int = 220) -> tuple[str | None, str | None]:
    """Call Responses API with safe model fallback. Returns (text, error)."""
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY не знайдено"
    models = []
    for model in (OPENAI_MODEL, "gpt-5-mini", "gpt-4.1-mini"):
        if model and model not in models:
            models.append(model)
    last_error = None
    for model in models:
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers=openai_headers(),
                json={
                    "model": model,
                    "input": prompt,
                    "store": False,
                    "max_output_tokens": max(64, max_output_tokens),
                    **({"reasoning": {"effort": "minimal"}} if model.startswith("gpt-5") else {}),
                },
                timeout=AI_TIMEOUT_SECONDS,
            )
            if response.ok:
                text = extract_openai_text(response.json()).strip()
                if text:
                    return text, None
                last_error = f"{model}: порожня відповідь"
                continue
            body = response.text[:500]
            last_error = f"{model}: HTTP {response.status_code} {body}"
            # 401/403 — інша модель не допоможе.
            if response.status_code in (401, 403):
                break
        except Exception as exc:
            last_error = f"{model}: {exc}"
    log.warning("OpenAI call failed: %s", last_error)
    return None, last_error


def ai_live_status() -> tuple[bool, str]:
    if not OPENAI_API_KEY:
        return False, "ключ не знайдено"
    text, error = _openai_response('Поверни тільки слово OK без пояснень.', max_output_tokens=128)
    if text and text.strip().upper().startswith("OK"):
        return True, "зв’язок працює"
    return False, error or "немає коректної відповіді"


def explicit_amount_signature(text: str) -> list[tuple[str, str]]:
    """Stable comparison key so AI cannot silently drop a currency from income/expense."""
    try:
        pairs = extract_all_amounts_currency(text)
        return sorted((format(a.normalize(), "f"), c) for a, c, _ in pairs)
    except Exception:
        return []


def ai_suggest_options(text: str) -> list[str]:
    """Return 1-3 safe canonical interpretations. AI never writes; user chooses one."""
    base = local_normalize_command(text)
    expected_amounts = explicit_amount_signature(base) if first_keyword(base) in ("виручка", "витрата") else []
    if not AI_HELP_ENABLED or not OPENAI_API_KEY:
        return [base] if canonical_command_is_safe(base) else []
    prompt = f"""Ти розбираєш бухгалтерське повідомлення DvirFinance. Перше слово задає дію і його не можна змінювати, крім форми «витрати» -> «витрата».
Поверни JSON-масив від 1 до 3 канонічних команд без пояснень.
Ключові слова: виручка, витрата, борг, повернення, аванс, ревізія, залишок, обмін, переказ.
Рахунки: Каса, Картка Миколи, Картка Андрія, Приват Миколи, Приват Андрія, ФОП Миколи, ФОП Андрія, Південний Миколи, Південний Андрія.
«Південний», «Південний банк», «банк Південний» означають один банк. «з карти/картки» означає рахунок списання; «на карту/картку» — рахунок зарахування.
Не вигадуй суму, валюту, власника, боржника чи призначення. Якщо власник/рахунок неоднозначний, дай кілька варіантів. Якщо валюта відсутня — грн.

ЖОРСТКІ ПРАВИЛА ЗА КОМАНДАМИ:
- борг: це запис «нам винні». Потрібні тільки ім'я боржника, сума, валюта й необов'язковий опис. НІКОЛИ не додавай Касу, картку, Південний, Приват або ФОП і не роби варіанти за рахунками. Канонічно: борг Болена 5000 грн товар.
- повернення: ім'я боржника + сума; рахунок потрібен лише як місце фактичного надходження. Без рахунку — в Касу.
- витрата: сума списується з указаного рахунку; без рахунку — Каса.
- виручка/аванс: сума додається на вказаний рахунок; без рахунку — Каса. Якщо у виручці є кілька сум у різних валютах (наприклад 10000 грн 500$ 200€), ОБОВ'ЯЗКОВО збережи ВСІ суми та валюти в одній команді без втрат.
- переказ: обов'язково два різні власні рахунки. Форма: переказ 5000 грн з Картки Андрія на Касу.
- обмін: один рахунок, вихідна валюта, цільова валюта, курс або дві суми.
Для витрати форма: витрата 5000 грн Південний Андрія призначення.
Повідомлення: {base}"""
    try:
        raw_text, api_error = _openai_response(prompt, max_output_tokens=260)
        if not raw_text:
            log.warning("OpenAI options error: %s", api_error)
            return [base] if canonical_command_is_safe(base) else []
        raw = raw_text.strip().strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        data = json.loads(raw)
        if not isinstance(data, list):
            data = [data]
        out=[]
        expected=first_keyword(base)
        for item in data[:AI_MAX_OPTIONS]:
            if not isinstance(item,str):
                continue
            cmd=local_normalize_command(item)
            if expected == "витрата" and first_keyword(cmd) != "витрата":
                continue
            if first_keyword(cmd) != expected:
                continue
            if expected in ("виручка", "витрата") and expected_amounts:
                if explicit_amount_signature(cmd) != expected_amounts:
                    continue
            if expected == "борг":
                # Debt creation must never branch by cash/bank account.
                if any(word in cmd.casefold() for word in ("південний", "картка", "карта", "приват", "фоп", "каса", "банк")):
                    continue
                try:
                    customer, _, _, _ = parse_debt_command(cmd)
                    if not customer:
                        continue
                except Exception:
                    continue
            if canonical_command_is_safe(cmd) and cmd not in out:
                out.append(cmd)
        return out or ([base] if canonical_command_is_safe(base) else [])
    except Exception:
        log.exception("AI options normalization failed")
        return [base] if canonical_command_is_safe(base) else []

def canonical_command_is_safe(command: str) -> bool:
    command = normalize_text(command)
    keyword = first_keyword(command)
    if not command or keyword not in CANONICAL_KEYWORDS:
        return False
    if not re.search(r"\d", command):
        return False
    try:
        extract_amount_currency(command)
        if keyword == "борг":
            customer, _, _, _ = parse_debt_command(command)
            if not customer:
                return False
        elif keyword == "переказ":
            low = command.casefold()
            if " з " not in f" {low} " or " на " not in f" {low} ":
                return False
        elif keyword == "обмін":
            low = command.casefold()
            if " на " not in f" {low} ":
                return False
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

def infer_free_text_transfer(text: str) -> str | None:
    """Deterministically recognize movement between two own accounts.

    Examples: «з картки Миколи 5000 грн в касу»,
    «зняли 5000 з Південного Андрія і поклали в касу».
    Such phrases are transfers, never revenue.
    """
    normalized = normalize_text(text)
    low = normalized.casefold()
    try:
        amount, currency, amount_match = extract_amount_currency(normalized)
    except Exception:
        return None

    account_word = r"(?:кас\w*|сейф\w*|готів\w*|налич\w*|карт\w*|карточ\w*|півден\w*|южн\w*|приват\w*|фоп\w*)"
    # A transfer must explicitly describe both a source and a destination.
    if not re.search(r"\b(?:з|із|зі)\b", low) or not re.search(r"\b(?:в|у|на|до)\b", low):
        return None

    before = normalized[:amount_match.start()]
    after = normalized[amount_match.end():]
    source_fragment = ""
    destination_fragment = ""

    # Common form: «з картки Миколи 5000 грн в касу».
    m_before = re.search(r"\b(?:з|із|зі)\s+(.+)$", before, flags=re.IGNORECASE)
    m_after = re.search(r"\b(?:в|у|на|до)\s+(.+)$", after, flags=re.IGNORECASE)
    if m_before and m_after:
        source_fragment = m_before.group(1)
        destination_fragment = m_after.group(1)
    else:
        # Form: «5000 грн з картки Миколи в касу».
        m = re.search(r"\b(?:з|із|зі)\s+(.+?)\s+(?:в|у|на|до)\s+(.+)$", after, flags=re.IGNORECASE)
        if m:
            source_fragment, destination_fragment = m.group(1), m.group(2)
        else:
            # Form: «зняли 5000 з картки Миколи і поклали в касу».
            m = re.search(r"\b(?:з|із|зі)\s+(.+?)(?:\s+і\s+|\s+та\s+|,\s*)(?:поклал\w*\s+)?(?:в|у|на|до)\s+(.+)$", after, flags=re.IGNORECASE)
            if m:
                source_fragment, destination_fragment = m.group(1), m.group(2)

    if not source_fragment or not destination_fragment:
        return None
    if not re.search(account_word, source_fragment, flags=re.IGNORECASE):
        return None
    if not re.search(account_word, destination_fragment, flags=re.IGNORECASE):
        return None

    source, _ = default_account_name(source_fragment)
    destination, _ = default_account_name(destination_fragment)
    if source == destination:
        return None
    source_display = "Каса" if source == "Сейф" else source
    destination_display = "Каса" if destination == "Сейф" else destination
    return f"переказ {money(amount, currency)} з {source_display} на {destination_display}"


def ai_suggest_canonical(text: str) -> str | None:
    transfer = infer_free_text_transfer(text)
    if transfer:
        return transfer
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
- переказ: переміщення між двома власними рахунками. Фрази «з картки ... в касу», «зняли з картки та поклали в касу» ЗАВЖДИ є переказом, а не виручкою.

Для витрати збережи в команді всі слова про призначення. Виправляй очевидні помилки: грн/гр, карточка/картка, Південний, ФОП.
Розпізнавай категорії: товар, пальне/бензин, зарплата, доставка, хімія, оренда, податки, комунальні, ремонт, реклама.
Обов'язково збережи номер накладної, навіть якщо написано з помилкою: «накладна 428», «згідно накладної №428», «прихідна 428», «прифі 428».

Правила:
1) Поверни тільки один рядок без пояснень.
2) Не вигадуй суму, валюту, ім'я або рахунок.
3) Якщо валюта не вказана, використовуй грн.
4) Якщо для надходження/повернення рахунок не вказаний, використовуй «в касу».
5) Якщо неможливо однозначно зрозуміти, поверни НЕЗРОЗУМІЛО.

Повідомлення: {text}"""
    raw_text, api_error = _openai_response(prompt, max_output_tokens=140)
    if not raw_text:
        log.warning("OpenAI helper error: %s", api_error)
        return None
    suggestion = raw_text.splitlines()[0].strip().strip("`\"")
    if suggestion.upper().startswith("НЕЗРОЗУМІЛО") or not canonical_command_is_safe(suggestion):
        return None
    return suggestion


def cleanup_pending_direct():
    cutoff = time.time() - PENDING_TTL_SECONDS
    for key in list(PENDING_DIRECT):
        if PENDING_DIRECT[key]["created"] < cutoff:
            PENDING_DIRECT.pop(key, None)


def offer_direct_confirmation(message, original_text: str) -> bool:
    """Show one or several interpretations. Nothing is saved before an explicit choice."""
    cleanup_pending_direct()
    options = ai_suggest_options(original_text) if AI_HELP_ENABLED else []
    if not options:
        normalized = local_normalize_command(original_text)
        options = [normalized] if canonical_command_is_safe(normalized) else []
    if not options:
        return False

    token = uuid.uuid4().hex[:12]
    PENDING_DIRECT[token] = {
        "created": time.time(), "chat_id": message.chat.id, "user_id": message.from_user.id,
        "message_id": message.message_id, "options": options, "original": original_text,
        "user": {"id": message.from_user.id, "username": message.from_user.username,
                 "first_name": message.from_user.first_name, "last_name": message.from_user.last_name},
    }
    markup = telebot.types.InlineKeyboardMarkup()
    if len(options) == 1:
        markup.row(
            telebot.types.InlineKeyboardButton("✅ Підтвердити", callback_data=f"direct_pick:{token}:0"),
            telebot.types.InlineKeyboardButton("❌ Скасувати", callback_data=f"direct_no:{token}"),
        )
        suggestion=options[0]
        if first_keyword(suggestion) == "витрата":
            preview = expense_preview_text(suggestion)
        elif first_keyword(suggestion) == "виручка":
            preview = income_preview_text(suggestion)
        else:
            preview = "🧾 <b>Перевір операцію</b>\n\n" + f"<code>{suggestion}</code>" + "\n\nНічого ще не записано."
        bot.reply_to(message, preview + "\n\nПідтвердити цю операцію?", reply_markup=markup)
    else:
        lines=["🤖 <b>Я бачу кілька можливих варіантів</b>", "", "Нічого ще не записано."]
        for idx,cmd in enumerate(options,1):
            lines.append(f"\n<b>{idx}.</b> <code>{cmd}</code>")
            markup.row(telebot.types.InlineKeyboardButton(f"✅ Варіант {idx}", callback_data=f"direct_pick:{token}:{idx-1}"))
        markup.row(telebot.types.InlineKeyboardButton("❌ Скасувати", callback_data=f"direct_no:{token}"))
        bot.reply_to(message, "\n".join(lines), reply_markup=markup)
    return True


@bot.callback_query_handler(func=lambda call: call.data.startswith(("direct_pick:", "direct_no:")))
def direct_confirmation_handler(call):
    cleanup_pending_direct()
    parts = call.data.split(":")
    action, token = parts[0], parts[1]
    option_index = int(parts[2]) if action == "direct_pick" and len(parts) > 2 else None
    pending = PENDING_DIRECT.get(token)
    if not pending or pending["chat_id"] != call.message.chat.id:
        bot.answer_callback_query(call.id, "Підтвердження застаріло")
        return
    if pending["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "Підтвердити може лише автор повідомлення")
        return
    PENDING_DIRECT.pop(token, None)
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    if action == "direct_no":
        bot.answer_callback_query(call.id, "Скасовано. Нічого не записано")
        bot.send_message(call.message.chat.id, "❌ Операцію скасовано. Дані не змінено.")
        return

    u = pending["user"]
    fake_message = SimpleNamespace(
        chat=call.message.chat,
        from_user=SimpleNamespace(**u),
        message_id=pending["message_id"],
        text=pending["options"][option_index],
    )
    try:
        selected = pending["options"][option_index]
        if execute_canonical(fake_message, selected):
            bot.answer_callback_query(call.id, "Операцію записано")
        else:
            bot.answer_callback_query(call.id, "Не вдалося розпізнати операцію")
            bot.send_message(call.message.chat.id, "⚠️ Операцію не записано: не вдалося однозначно розпізнати команду.")
    except Exception as exc:
        log.exception("Direct confirmation failed")
        bot.answer_callback_query(call.id, "Операцію не записано")
        bot.send_message(call.message.chat.id, f"⚠️ Операцію не записано. {str(exc)[:500]}")

def cleanup_pending_ai():
    cutoff = time.time() - PENDING_TTL_SECONDS
    for key in list(PENDING_AI):
        if PENDING_AI[key]["created"] < cutoff:
            PENDING_AI.pop(key, None)

def execute_canonical(message, text: str) -> bool:
    for handler in (
        handle_revision,
        handle_exchange,
        handle_transfer,
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


def calculate_cash_balances(chat_id: int) -> dict:
    """Поточні залишки по кожному рахунку та валюті."""
    balances = {}

    def add(account_name, currency, amount):
        key = (account_name or "Сейф", currency)
        balances[key] = balances.get(key, Decimal("0")) + Decimal(str(amount or 0))

    revisions = latest_revisions(chat_id)
    revision_moments = {}
    for key, row in revisions.items():
        balances[key] = Decimal(str(row["actual_amount"]))
        revision_moments[key] = row.get("created_at") or (str(row["revision_date"]) + "T23:59:59+00:00")

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
        rev_moment = revision_moments.get(key)
        row_moment = row.get("created_at") or (str(row["operation_date"]) + "T00:00:00+00:00")
        if rev_moment and row_moment <= rev_moment:
            continue
        sign = Decimal("1") if row["operation_type"] in ("income", "exchange_in", "transfer_in") else Decimal("-1")
        add(key[0], key[1], sign * Decimal(str(row["amount"])))

    closings = sb_select(
        "daily_closings",
        {
            "select": "id,closing_date,currency,created_at",
            "telegram_chat_id": f"eq.{chat_id}",
            "is_cancelled": "eq.false",
            "order": "closing_date.asc",
        },
    )
    closing_map = {str(c["id"]): c for c in closings}
    if closing_map:
        closing_accounts = sb_select(
            "daily_closing_accounts",
            {"select": "*", "daily_closing_id": f"in.({','.join(closing_map.keys())})"},
        )
        for row in closing_accounts:
            closing = closing_map.get(str(row["daily_closing_id"]))
            if not closing:
                continue
            key = (row["account_name"], closing["currency"])
            rev_moment = revision_moments.get(key)
            closing_moment = closing.get("created_at") or (str(closing["closing_date"]) + "T00:00:00+00:00")
            if rev_moment and closing_moment <= rev_moment:
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
        rev_moment = revision_moments.get(key)
        row_moment = row.get("created_at") or (str(row["payment_date"]) + "T00:00:00+00:00")
        if rev_moment and row_moment <= rev_moment:
            continue
        add(key[0], key[1], row["amount"])

    return balances


def current_account_balance(chat_id: int, account_name: str, currency: str) -> Decimal:
    return calculate_cash_balances(chat_id).get((account_name or "Сейф", currency), Decimal("0"))


def _group_visible_balances(chat_id: int, include_cash: bool) -> dict:
    """Return canonical account -> currency -> non-zero amount."""
    raw = calculate_cash_balances(chat_id)
    grouped = {}
    cash_aliases = {"сейф", "каса", "готівка", "наличка"}
    for (raw_name, currency), amount in raw.items():
        amount = Decimal(str(amount))
        if amount == 0:
            continue
        name = canonical_account_name(raw_name or "Сейф")
        is_cash = name.lower() in cash_aliases or name == "Сейф"
        if not include_cash and is_cash:
            continue
        display = "Каса" if is_cash else name
        grouped.setdefault(display, {})[currency] = grouped.setdefault(display, {}).get(currency, Decimal("0")) + amount
    return grouped



MONTHS_UA = {
    "січня": 1, "січень": 1, "января": 1,
    "лютого": 2, "лютий": 2, "февраля": 2,
    "березня": 3, "березень": 3, "марта": 3,
    "квітня": 4, "квітень": 4, "апреля": 4,
    "травня": 5, "травень": 5, "мая": 5,
    "червня": 6, "червень": 6, "июня": 6,
    "липня": 7, "липень": 7, "июля": 7,
    "серпня": 8, "серпень": 8, "августа": 8,
    "вересня": 9, "вересень": 9, "сентября": 9,
    "жовтня": 10, "жовтень": 10, "октября": 10,
    "листопада": 11, "листопад": 11, "ноября": 11,
    "грудня": 12, "грудень": 12, "декабря": 12,
}


def parse_period_spec(raw: str, now: datetime | None = None) -> tuple[date, date, str]:
    """Parse friendly Ukrainian report periods without involving AI."""
    now = now or datetime.now(KYIV)
    today = now.date()
    text = normalize_text(raw).lower().strip(" ,.;")
    if not text or text in ("місяць", "за місяць", "поточний місяць", "цей місяць"):
        start = today.replace(day=1)
        return start, today, f"{start.strftime('%d.%m.%Y')}–{today.strftime('%d.%m.%Y')}"
    if text in ("сьогодні", "за сьогодні", "сегодня"):
        return today, today, today.strftime("%d.%m.%Y")
    if text in ("вчора", "за вчора", "вчера"):
        d = today - timedelta(days=1)
        return d, d, d.strftime("%d.%m.%Y")
    if text in ("тиждень", "за тиждень", "7 днів", "за 7 днів", "неділя", "неделя"):
        start = today - timedelta(days=6)
        return start, today, f"{start.strftime('%d.%m.%Y')}–{today.strftime('%d.%m.%Y')}"

    # 01.08.2026-15.08.2026 or 01.08-15.08
    m = re.fullmatch(r"(?:з\s*)?(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*(?:-|–|—|по|до)\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
    if m:
        d1, mo1, y1, d2, mo2, y2 = m.groups()
        y1 = int(y1) if y1 else today.year
        y2 = int(y2) if y2 else y1
        if y1 < 100: y1 += 2000
        if y2 < 100: y2 += 2000
        start, end = date(y1, int(mo1), int(d1)), date(y2, int(mo2), int(d2))
        if end < start: raise ValueError("Кінцева дата не може бути раніше початкової")
        return start, end, f"{start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"

    # 01.08.2026 or 01.08
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
    if m:
        d, mo, y = m.groups(); y = int(y) if y else today.year
        if y < 100: y += 2000
        one = date(y, int(mo), int(d))
        return one, one, one.strftime("%d.%m.%Y")

    # 1-15 серпня [2026], з 1 по 15 серпня
    months = "|".join(map(re.escape, MONTHS_UA.keys()))
    m = re.fullmatch(rf"(?:з\s*)?(\d{{1,2}})\s*(?:-|–|—|по|до)\s*(\d{{1,2}})\s+({months})(?:\s+(\d{{4}}))?", text)
    if m:
        d1, d2, month_word, year = m.groups(); year = int(year or today.year)
        month = MONTHS_UA[month_word]
        start, end = date(year, month, int(d1)), date(year, month, int(d2))
        if end < start: raise ValueError("Кінцева дата не може бути раніше початкової")
        return start, end, f"{start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"

    # 1-15 means days of the current month.
    m = re.fullmatch(r"(?:з\s*)?(\d{1,2})\s*(?:-|–|—|по|до)\s*(\d{1,2})", text)
    if m:
        start = date(today.year, today.month, int(m.group(1)))
        end = date(today.year, today.month, int(m.group(2)))
        if end < start: raise ValueError("Кінцева дата не може бути раніше початкової")
        return start, end, f"{start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"

    # 15 серпня [2026]
    m = re.fullmatch(rf"(\d{{1,2}})\s+({months})(?:\s+(\d{{4}}))?", text)
    if m:
        d, month_word, year = m.groups(); year = int(year or today.year)
        one = date(year, MONTHS_UA[month_word], int(d))
        return one, one, one.strftime("%d.%m.%Y")

    raise ValueError("Період не зрозумів. Приклад: базар тиждень; склад місяць; базар 1-15 серпня; склад 01.08-15.08")


def _income_rows_for_period(chat_id: int, start: date, end: date) -> list:
    rows = sb_select("cash_operations", {
        "select": "operation_date,amount,currency,description,account_name,created_at",
        "telegram_chat_id": f"eq.{chat_id}",
        "operation_type": "eq.income",
        "is_cancelled": "eq.false",
        "operation_date": f"gte.{start.isoformat()}",
        "order": "operation_date.asc,created_at.asc",
        "limit": "5000",
    })
    return [r for r in rows if str(r.get("operation_date") or "") <= end.isoformat()]


def source_period_summary(chat_id: int, source: str, period_raw: str = "") -> str:
    """Revenue by Bazar/Sklad for any date range. Source stays analytics, never an account."""
    start, end, label = parse_period_spec(period_raw)
    rows = _income_rows_for_period(chat_id, start, end)
    needle = source.lower()
    matched = [r for r in rows if re.search(rf"(?<![\wа-яіїєґ]){re.escape(needle)}(?![\wа-яіїєґ])", (r.get("description") or "").lower(), flags=re.IGNORECASE)]
    totals, accounts, days = {}, {}, {}
    for row in matched:
        currency = row.get("currency") or "UAH"
        amount = Decimal(str(row.get("amount") or 0))
        totals[currency] = totals.get(currency, Decimal("0")) + amount
        account = canonical_account_name(row.get("account_name") or "Сейф")
        display = "Каса" if account == "Сейф" else account
        accounts.setdefault(display, {})[currency] = accounts.setdefault(display, {}).get(currency, Decimal("0")) + amount
        day = str(row.get("operation_date"))
        days.setdefault(day, {})[currency] = days.setdefault(day, {}).get(currency, Decimal("0")) + amount

    title = source.capitalize()
    if not matched:
        return f"📊 <b>{title} — {label}</b>\n\nВиручки за цей період не записано."
    lines = [f"📊 <b>{title} — {label}</b>", "", "<b>Разом:</b>"]
    for currency in ("UAH", "USD", "EUR"):
        if totals.get(currency): lines.append(f"• {money(totals[currency], currency)}")
    if len(days) > 1:
        lines.append("\n<b>По днях:</b>")
        for day in sorted(days):
            vals = [money(days[day][c], c) for c in ("UAH","USD","EUR") if days[day].get(c)]
            lines.append(f"• {date.fromisoformat(day).strftime('%d.%m')}: " + ", ".join(vals))
    lines.append("\n<b>Куди зараховано:</b>")
    for account in sorted(accounts, key=str.lower):
        vals = [money(accounts[account][c], c) for c in ("UAH","USD","EUR") if accounts[account].get(c)]
        lines.append(f"• {account}: " + ", ".join(vals))
    lines.append("\n<i>Базар і Склад — джерела виручки, а не окремі грошові рахунки.</i>")
    return "\n".join(lines)


def source_month_summary(chat_id: int, source: str) -> str:
    return source_period_summary(chat_id, source, "місяць")


def report_period_text(chat_id: int, period_raw: str = "") -> str:
    start, end, label = parse_period_spec(period_raw or "сьогодні")
    rows = sb_select("cash_operations", {
        "select": "operation_date,operation_type,amount,currency",
        "telegram_chat_id": f"eq.{chat_id}", "is_cancelled": "eq.false",
        "operation_date": f"gte.{start.isoformat()}", "order": "operation_date.asc", "limit": "5000",
    })
    rows = [r for r in rows if str(r.get("operation_date") or "") <= end.isoformat()]
    totals = {}
    for row in rows:
        curr = row.get("currency") or "UAH"
        totals.setdefault(curr, {"income": Decimal("0"), "expense": Decimal("0")})
        if row.get("operation_type") == "income": totals[curr]["income"] += Decimal(str(row.get("amount") or 0))
        elif row.get("operation_type") == "expense": totals[curr]["expense"] += Decimal(str(row.get("amount") or 0))
    if not totals: return f"За період {label} операцій ще немає."
    lines=[f"📊 <b>Звіт — {label}</b>",""]
    for curr in ("UAH","USD","EUR"):
        if curr not in totals: continue
        data=totals[curr]
        lines += [f"<b>{curr}</b>", f"Виручка: {money(data['income'],curr)}", f"Витрати: {money(data['expense'],curr)}", f"Рух: {money(data['income']-data['expense'],curr)}", ""]
    return "\n".join(lines).strip()


def revenue_period_daily_text(chat_id: int, period_raw: str = "тиждень") -> str:
    """Revenue only, grouped by calendar day and currency; includes zero-revenue days."""
    start, end, label = parse_period_spec(period_raw or "тиждень")
    rows = _income_rows_for_period(chat_id, start, end)
    days: dict[str, dict[str, Decimal]] = {}
    totals: dict[str, Decimal] = {}
    for row in rows:
        day = str(row.get("operation_date") or "")
        curr = row.get("currency") or "UAH"
        amount = Decimal(str(row.get("amount") or 0))
        days.setdefault(day, {})[curr] = days.setdefault(day, {}).get(curr, Decimal("0")) + amount
        totals[curr] = totals.get(curr, Decimal("0")) + amount

    lines = [f"📈 <b>Виручка — {label}</b>", "", "<b>По днях:</b>"]
    d = start
    while d <= end:
        key = d.isoformat()
        vals = [money(days.get(key, {}).get(c, Decimal("0")), c) for c in ("UAH", "USD", "EUR") if days.get(key, {}).get(c, Decimal("0")) != 0]
        lines.append(f"• {d.strftime('%d.%m')}: " + (", ".join(vals) if vals else "0 грн"))
        d += timedelta(days=1)

    lines.append("\n<b>Разом:</b>")
    if totals:
        for curr in ("UAH", "USD", "EUR"):
            if totals.get(curr, Decimal("0")) != 0:
                lines.append(f"• {money(totals[curr], curr)}")
    else:
        lines.append("• 0 грн")
    return "\n".join(lines)


def cards_summary(chat_id: int) -> str:
    """Show all bank/card/FOP accounts and only currencies with non-zero balances."""
    grouped = _group_visible_balances(chat_id, include_cash=False)
    if not grouped:
        return "💳 <b>Карти та банківські рахунки</b>\n\nДоступних коштів на банківських рахунках немає."

    lines = ["💳 <b>Карти та банківські рахунки</b>"]
    totals = {}
    for name in sorted(grouped, key=str.lower):
        lines.append(f"\n<b>{name}</b>")
        for currency in ("UAH", "USD", "EUR"):
            amount = grouped[name].get(currency, Decimal("0"))
            if amount != 0:
                lines.append(f"• {money(amount, currency)}")
                totals[currency] = totals.get(currency, Decimal("0")) + amount

    if totals:
        lines.append("\n<b>Разом на всіх банківських рахунках:</b>")
        for currency in ("UAH", "USD", "EUR"):
            amount = totals.get(currency, Decimal("0"))
            if amount != 0:
                lines.append(f"• {money(amount, currency)}")
    return "\n".join(lines)


def cash_summary(chat_id: int) -> str:
    """Full money picture: physical cash plus every bank account, in every non-zero currency."""
    grouped = _group_visible_balances(chat_id, include_cash=True)

    debts = find_open_debts(chat_id)
    debt_totals = {}
    for debt in debts:
        curr = debt["currency"]
        debt_totals[curr] = debt_totals.get(curr, Decimal("0")) + Decimal(str(debt["outstanding_amount"]))

    lines = ["💰 <b>Повний фінансовий стан</b>"]
    if grouped:
        total_all = {}
        for name in sorted(grouped, key=lambda x: (x != "Каса", x.lower())):
            lines.append(f"\n<b>{name}</b>")
            for currency in ("UAH", "USD", "EUR"):
                amount = grouped[name].get(currency, Decimal("0"))
                if amount != 0:
                    lines.append(f"• {money(amount, currency)}")
                    total_all[currency] = total_all.get(currency, Decimal("0")) + amount
        if total_all:
            lines.append("\n<b>Разом доступно на всіх рахунках:</b>")
            for currency in ("UAH", "USD", "EUR"):
                amount = total_all.get(currency, Decimal("0"))
                if amount != 0:
                    lines.append(f"• {money(amount, currency)}")
    else:
        lines.append("\nГрошові залишки ще не внесені.")

    lines.append("\n<b>Нам винні:</b>")
    visible_debts = False
    for curr in ("UAH", "USD", "EUR"):
        amount = debt_totals.get(curr, Decimal("0"))
        if amount != 0:
            lines.append(f"• {money(amount, curr)}")
            visible_debts = True
    if not visible_debts:
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
        "transfer_out": "Переказ −",
        "transfer_in": "Переказ +",
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

<code>залишок 822 долари каса</code>
🎯 встановлює точний фактичний залишок і прибирає старий мінус

<code>ревізія 125000 грн у касі</code>

<code>обмін 10000 грн на долари курс 45</code>
💱 мінус гривні та плюс долари однією операцією
<code>борги</code>
<code>каса</code>
<code>звіт</code>
<code>звіт тиждень</code>
<code>звіт місяць</code>
<code>виручка за тиждень</code> — по днях
<code>виручка 19.08</code> — за конкретний день
<code>виручка 15.08-20.08</code> — по днях за період
<code>базар тиждень</code>
<code>склад місяць</code>
<code>базар 1-15 серпня</code>
<code>витрати</code> — підсумок за категоріями
<code>витрати зарплата</code>
<code>витрати товар</code>
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
        if low in ("ші", "ai", "штучний інтелект", "перевірити ші"):
            ok, detail = ai_live_status()
            key_status = "є" if OPENAI_API_KEY else "немає"
            bot.reply_to(
                message,
                f"🧠 <b>Перевірка ШІ</b>\n"
                f"Ключ: <b>{key_status}</b>\n"
                f"Режим: <b>{APP_MODE}</b>\n"
                f"Модель: <b>{OPENAI_MODEL}</b>\n"
                f"AI_HELP_ENABLED: <b>{AI_HELP_ENABLED}</b>\n"
                f"Статус: <b>{'✅ працює' if ok else '❌ не працює'}</b>\n"
                f"Деталі: <code>{str(detail)[:350]}</code>",
            )
            return

        if low in ("версія", "версия", "version"):
            ai_status = "увімкнено з підтвердженням" if AI_HELP_ENABLED else "вимкнено"
            bot.reply_to(
                message,
                f"✅ <b>{VERSION}</b>\n"
                f"Режим: <b>{APP_MODE}</b>\n"
                "Правило: ключове слово першим\n"
                f"ШІ-помічник: {ai_status}\n"
                f"Модель: <b>{OPENAI_MODEL}</b>\n"
                f"Ключ API: <b>{'знайдено' if OPENAI_API_KEY else 'не знайдено'}</b>\n"
                "ШІ самостійно нічого не записує",
            )
            return

        if low in ("каса", "баланс", "скільки грошей"):
            bot.reply_to(message, cash_summary(message.chat.id))
            return

        if low in ("карта", "карти", "картка", "картки", "банки", "рахунки"):
            bot.reply_to(message, cards_summary(message.chat.id))
            return

        # "виручка" with a period is a read-only report; with an amount it remains a write command.
        revenue_report_match = re.fullmatch(r"виручка(?:\s+за)?\s+(.+)", low)
        if revenue_report_match and not re.search(r"\d\s*(?:грн|гр|uah|₴|\$|usd|€|eur|дол|євро|евро)", low, flags=re.IGNORECASE):
            period_text = revenue_report_match.group(1)
            try:
                parse_period_spec(period_text)
            except Exception:
                pass
            else:
                bot.reply_to(message, revenue_period_daily_text(message.chat.id, period_text))
                return

        source_match = re.fullmatch(r"(базар|склад)(?:\s+(.+))?", low)
        if source_match:
            bot.reply_to(message, source_period_summary(message.chat.id, source_match.group(1), source_match.group(2) or "місяць"))
            return

        report_match = re.fullmatch(r"(?:звіт|отчет)(?:\s+(.+))?", low)
        if report_match:
            period = report_match.group(1) or "сьогодні"
            bot.reply_to(message, report_period_text(message.chat.id, period))
            return

        expense_match = re.fullmatch(r"витрати(?:\s+(.+))?", low)
        if expense_match and not re.search(r"\d", low):
            bot.reply_to(message, expense_report_text(message.chat.id, expense_match.group(1)))
            return

        if finish_reset_if_expected(message, text):
            return

        latest_match = re.fullmatch(r"(?:останні|історія)(?:\s+(\d{1,2}))?", low)
        if latest_match:
            bot.reply_to(message, latest_operations_text(message.chat.id, int(latest_match.group(1) or 10)))
            return

        cancel_match = re.fullmatch(r"(?:скасувати|відміна|відмінити)(?:\s+#?(\d+))?", low)
        if cancel_match:
            request_cancel(message, int(cancel_match.group(1)) if cancel_match.group(1) else None)
            return

        if low == "обнулити":
            request_reset(message)
            return

        if handle_debt_queries(message, text):
            return

        # Усі бухгалтерські записи проходять обов'язковий попередній перегляд.
        write_keywords = {"виручка", "витрата", "витрати", "борг", "повернення", "аванс", "ревізія", "залишок", "обмін", "переказ"}
        if first_keyword(text) in write_keywords:
            if offer_direct_confirmation(message, text):
                return

        # Для фраз із помилками ШІ лише пропонує безпечну канонічну команду.
        if APP_MODE in ("AI", "HYBRID") and offer_ai_suggestion(message, text):
            return

        bot.reply_to(
            message,
            "Не зрозумів перше ключове слово. Дані не записував.\n\n"
            "Використовуй: <b>виручка, витрата, борг, повернення, аванс, залишок, ревізія, обмін, переказ</b>.\n"
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
