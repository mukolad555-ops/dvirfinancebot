import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("dvirfinancebot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TABLE_URL = f"{SUPABASE_URL}/rest/v1/cash_operations"
KYIV_TZ = ZoneInfo("Europe/Kyiv")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

CURRENCY_ALIASES = {
    "UAH": ["грн", "гривень", "гривні", "гривня", "uah", "₴"],
    "USD": ["дол", "долар", "доларів", "долари", "usd", "$"],
    "EUR": ["євро", "евро", "eur", "€"],
}

OPERATION_LABELS = {
    "income": "Виручка",
    "expense": "Витрата",
    "exchange_in": "Обмін: надходження",
    "exchange_out": "Обмін: видача",
}

HELP_TEXT = """
Я веду внутрішній облік каси.

Приклади:
• виручка 20000 грн 100 доларів 50 євро
• витрата 1500 грн доставка
• обмін 10000 грн на 230 доларів
• каса
• звіт
• звіт місяць
• історія
• видалити останню
""".strip()


def today_kyiv() -> date:
    return datetime.now(KYIV_TZ).date()


def normalize_number(raw: str) -> Decimal:
    return Decimal(raw.replace(" ", "").replace("\u00a0", "").replace(",", "."))


def find_amounts(text: str) -> List[Tuple[str, Decimal]]:
    found: List[Tuple[str, Decimal]] = []
    lowered = text.lower()
    for currency, aliases in CURRENCY_ALIASES.items():
        aliases_pattern = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
        pattern = rf"(\d+(?:[ \u00a0]\d{{3}})*(?:[.,]\d+)?)\s*(?:{aliases_pattern})"
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            try:
                found.append((currency, normalize_number(match.group(1))))
            except InvalidOperation:
                pass
    return found


def api_request(method: str, *, params: Optional[dict] = None, json: Optional[dict] = None, prefer: Optional[str] = None) -> requests.Response:
    headers = dict(HEADERS)
    if prefer:
        headers["Prefer"] = prefer
    response = requests.request(method, TABLE_URL, headers=headers, params=params, json=json, timeout=20)
    if not response.ok:
        logger.error("Supabase error %s: %s", response.status_code, response.text)
    response.raise_for_status()
    return response


def save_operation(operation_type: str, currency: str, amount: Decimal, description: str, update: Update) -> None:
    user = update.effective_user
    chat = update.effective_chat
    payload = {
        "operation_date": today_kyiv().isoformat(),
        "operation_type": operation_type,
        "currency": currency,
        "amount": float(amount),
        "description": description[:500],
        "telegram_user_id": user.id if user else None,
        "telegram_username": user.username if user else None,
        "telegram_full_name": user.full_name if user else None,
        "telegram_chat_id": chat.id if chat else None,
    }
    api_request("POST", json=payload)


def fetch_operations(chat_id: int, *, date_from: Optional[date] = None) -> List[dict]:
    params = {
        "select": "id,created_at,operation_date,operation_type,currency,amount,description,telegram_full_name",
        "telegram_chat_id": f"eq.{chat_id}",
        "order": "id.desc",
    }
    if date_from:
        params["operation_date"] = f"gte.{date_from.isoformat()}"
    return api_request("GET", params=params, prefer="count=none").json()


def calculate_totals(rows: List[dict]) -> Dict[str, Dict[str, Decimal]]:
    totals = {
        "income": {"UAH": Decimal("0"), "USD": Decimal("0"), "EUR": Decimal("0")},
        "expense": {"UAH": Decimal("0"), "USD": Decimal("0"), "EUR": Decimal("0")},
        "balance": {"UAH": Decimal("0"), "USD": Decimal("0"), "EUR": Decimal("0")},
    }
    signs = {"income": Decimal("1"), "expense": Decimal("-1"), "exchange_in": Decimal("1"), "exchange_out": Decimal("-1")}
    for row in rows:
        currency = row.get("currency")
        operation_type = row.get("operation_type")
        if currency not in totals["balance"] or operation_type not in signs:
            continue
        amount = Decimal(str(row.get("amount", 0)))
        totals["balance"][currency] += amount * signs[operation_type]
        if operation_type == "income":
            totals["income"][currency] += amount
        elif operation_type == "expense":
            totals["expense"][currency] += amount
    return totals


def get_balance(chat_id: int) -> Dict[str, Decimal]:
    return calculate_totals(fetch_operations(chat_id))["balance"]


def delete_last_operation(chat_id: int) -> Optional[dict]:
    params = {
        "select": "id,operation_type,currency,amount,description",
        "telegram_chat_id": f"eq.{chat_id}",
        "order": "id.desc",
        "limit": "1",
    }
    rows = api_request("GET", params=params, prefer="count=none").json()
    if not rows:
        return None
    row = rows[0]
    api_request("DELETE", params={"id": f"eq.{row['id']}"})
    return row


def format_money(currency: str, amount: Decimal) -> str:
    symbols = {"UAH": "грн", "USD": "$", "EUR": "€"}
    value = f"{amount:,.2f}".replace(",", " ").replace(".00", "")
    return f"{value} {symbols[currency]}"


def totals_lines(values: Dict[str, Decimal]) -> str:
    return "\n".join(f"• {format_money(c, values[c])}" for c in ("UAH", "USD", "EUR"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT)


async def show_cash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    await update.message.reply_text("💰 Поточна каса:\n" + totals_lines(get_balance(update.effective_chat.id)))


async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE, *, month: bool = False) -> None:
    if not update.message or not update.effective_chat:
        return
    today = today_kyiv()
    date_from = today.replace(day=1) if month else today
    totals = calculate_totals(fetch_operations(update.effective_chat.id, date_from=date_from))
    period = f"за {today.strftime('%m.%Y')}" if month else "за сьогодні"
    await update.message.reply_text(
        f"📊 Звіт {period}\n\n"
        "💵 Виручка:\n" + totals_lines(totals["income"]) + "\n\n"
        "💸 Витрати:\n" + totals_lines(totals["expense"]) + "\n\n"
        "💰 Рух за період:\n" + totals_lines(totals["balance"])
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    rows = fetch_operations(update.effective_chat.id)[:10]
    if not rows:
        await update.message.reply_text("Історія поки порожня.")
        return
    lines = ["🧾 Останні операції:"]
    for row in rows:
        label = OPERATION_LABELS.get(row["operation_type"], row["operation_type"])
        amount = format_money(row["currency"], Decimal(str(row["amount"])))
        description = (row.get("description") or "").strip()
        if len(description) > 45:
            description = description[:42] + "…"
        lines.append(f"• {row['operation_date']} — {label}: {amount}" + (f"\n  {description}" if description else ""))
    await update.message.reply_text("\n".join(lines))


async def remove_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    row = delete_last_operation(update.effective_chat.id)
    if not row:
        await update.message.reply_text("Немає операцій для видалення.")
        return
    label = OPERATION_LABELS.get(row["operation_type"], row["operation_type"])
    amount = format_money(row["currency"], Decimal(str(row["amount"])))
    await update.message.reply_text(f"🗑 Видалено останню операцію:\n{label} — {amount}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    lowered = text.lower()
    try:
        if lowered in {"каса", "баланс", "/cash"}:
            await show_cash(update, context)
            return
        if lowered.startswith(("виручка", "дохід", "прихід")):
            amounts = find_amounts(text)
            if not amounts:
                await update.message.reply_text("Не бачу суму. Приклад: виручка 20000 грн 100 доларів")
                return
            for currency, amount in amounts:
                save_operation("income", currency, amount, text, update)
            await update.message.reply_text("✅ Виручку записано: " + ", ".join(format_money(c, a) for c, a in amounts))
            return
        if lowered.startswith(("витрата", "видаток", "розхід")):
            amounts = find_amounts(text)
            if not amounts:
                await update.message.reply_text("Не бачу суму. Приклад: витрата 1500 грн доставка")
                return
            for currency, amount in amounts:
                save_operation("expense", currency, amount, text, update)
            await update.message.reply_text("✅ Витрату записано: " + ", ".join(format_money(c, a) for c, a in amounts))
            return
        if lowered.startswith(("обмін", "обмен", "поміняли", "поменяли")):
            amounts = find_amounts(text)
            if len(amounts) != 2:
                await update.message.reply_text("Приклад: обмін 10000 грн на 230 доларів")
                return
            (from_currency, from_amount), (to_currency, to_amount) = amounts
            save_operation("exchange_out", from_currency, from_amount, text, update)
            save_operation("exchange_in", to_currency, to_amount, text, update)
            await update.message.reply_text("✅ Обмін записано:\n" f"− {format_money(from_currency, from_amount)}\n" f"+ {format_money(to_currency, to_amount)}")
            return
        if lowered in {"звіт", "звіт сьогодні", "отчет", "отчет сегодня"}:
            await show_report(update, context, month=False)
            return
        if lowered in {"звіт місяць", "звіт за місяць", "отчет месяц", "отчет за месяц"}:
            await show_report(update, context, month=True)
            return
        if lowered in {"історія", "история", "останні 10", "последние 10"}:
            await show_history(update, context)
            return
        if lowered in {"видалити останню", "удалить последнюю", "скасувати останню", "отменить последнюю"}:
            await remove_last(update, context)
            return
        if lowered.startswith(("допомога", "що вмієш", "помощь")):
            await update.message.reply_text(HELP_TEXT)
            return
        await update.message.reply_text("Не зрозумів команду.\n\n" + HELP_TEXT)
    except Exception:
        logger.exception("Помилка обробки повідомлення")
        await update.message.reply_text("⚠️ Сталася помилка. Подробиці записані в Railway Deploy Logs.")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cash", show_cash))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("DvirFinanceBot v2 запущено")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
