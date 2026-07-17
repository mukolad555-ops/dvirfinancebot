import logging
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple

from supabase import Client, create_client
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("dvirfinancebot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CURRENCY_ALIASES = {
    "UAH": ["грн", "гривень", "гривні", "гривня", "uah", "₴"],
    "USD": ["дол", "долар", "доларів", "долари", "usd", "$"],
    "EUR": ["євро", "евро", "eur", "€"],
}

HELP_TEXT = """
Я веду внутрішній облік каси.

Приклади повідомлень:
• виручка 20000 грн 100 доларів 50 євро
• витрата 1500 грн доставка
• обмін 10000 грн на 230 доларів
• каса
• звіт сьогодні
• звіт місяць

Усі записи бачать учасники цієї Telegram-групи.
""".strip()


def normalize_number(raw: str) -> Decimal:
    cleaned = raw.replace(" ", "").replace(",", ".")
    return Decimal(cleaned)


def find_amounts(text: str) -> List[Tuple[str, Decimal]]:
    found: List[Tuple[str, Decimal]] = []
    lowered = text.lower()

    for currency, aliases in CURRENCY_ALIASES.items():
        alias_pattern = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        pattern = rf"(\d+(?:[ \u00a0]\d{{3}})*(?:[.,]\d+)?)\s*(?:{alias_pattern})\b"
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            try:
                found.append((currency, normalize_number(match.group(1))))
            except InvalidOperation:
                continue
    return found


def save_operation(
    *,
    operation_type: str,
    currency: str,
    amount: Decimal,
    description: str,
    update: Update,
) -> None:
    user = update.effective_user
    chat = update.effective_chat

    payload = {
        "operation_date": date.today().isoformat(),
        "operation_type": operation_type,
        "currency": currency,
        "amount": float(amount),
        "description": description[:500],
        "telegram_user_id": user.id if user else None,
        "telegram_username": user.username if user else None,
        "telegram_full_name": user.full_name if user else None,
        "telegram_chat_id": chat.id if chat else None,
    }
    supabase.table("cash_operations").insert(payload).execute()


def get_balance(chat_id: int) -> Dict[str, Decimal]:
    response = (
        supabase.table("cash_operations")
        .select("operation_type,currency,amount")
        .eq("telegram_chat_id", chat_id)
        .execute()
    )

    balances = {"UAH": Decimal("0"), "USD": Decimal("0"), "EUR": Decimal("0")}
    signs = {
        "income": Decimal("1"),
        "expense": Decimal("-1"),
        "exchange_in": Decimal("1"),
        "exchange_out": Decimal("-1"),
    }

    for row in response.data or []:
        currency = row.get("currency")
        operation_type = row.get("operation_type")
        if currency in balances and operation_type in signs:
            balances[currency] += Decimal(str(row.get("amount", 0))) * signs[operation_type]
    return balances


def format_money(currency: str, amount: Decimal) -> str:
    symbols = {"UAH": "грн", "USD": "$", "EUR": "€"}
    value = f"{amount:,.2f}".replace(",", " ").replace(".00", "")
    return f"{value} {symbols[currency]}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def show_cash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    balances = get_balance(chat.id)
    message = (
        "💰 Поточна каса:\n"
        f"• {format_money('UAH', balances['UAH'])}\n"
        f"• {format_money('USD', balances['USD'])}\n"
        f"• {format_money('EUR', balances['EUR'])}"
    )
    await update.message.reply_text(message)


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
                await update.message.reply_text(
                    "Не бачу суму. Приклад: виручка 20000 грн 100 доларів 50 євро"
                )
                return

            for currency, amount in amounts:
                save_operation(
                    operation_type="income",
                    currency=currency,
                    amount=amount,
                    description=text,
                    update=update,
                )

            recorded = ", ".join(format_money(c, a) for c, a in amounts)
            await update.message.reply_text(f"✅ Виручку записано: {recorded}")
            return

        if lowered.startswith(("витрата", "видаток", "розхід")):
            amounts = find_amounts(text)
            if not amounts:
                await update.message.reply_text(
                    "Не бачу суму. Приклад: витрата 1500 грн доставка"
                )
                return

            for currency, amount in amounts:
                save_operation(
                    operation_type="expense",
                    currency=currency,
                    amount=amount,
                    description=text,
                    update=update,
                )

            recorded = ", ".join(format_money(c, a) for c, a in amounts)
            await update.message.reply_text(f"✅ Витрату записано: {recorded}")
            return

        if lowered.startswith(("обмін", "обмен", "поміняли", "поменяли")):
            amounts = find_amounts(text)
            if len(amounts) != 2:
                await update.message.reply_text(
                    "Для обміну потрібно дві суми. Приклад: обмін 10000 грн на 230 доларів"
                )
                return

            (from_currency, from_amount), (to_currency, to_amount) = amounts
            save_operation(
                operation_type="exchange_out",
                currency=from_currency,
                amount=from_amount,
                description=text,
                update=update,
            )
            save_operation(
                operation_type="exchange_in",
                currency=to_currency,
                amount=to_amount,
                description=text,
                update=update,
            )

            await update.message.reply_text(
                "✅ Обмін записано:\n"
                f"− {format_money(from_currency, from_amount)}\n"
                f"+ {format_money(to_currency, to_amount)}"
            )
            return

        if lowered.startswith("звіт"):
            await show_cash(update, context)
            return

        if lowered.startswith(("допомога", "що вмієш", "/help")):
            await update.message.reply_text(HELP_TEXT)
            return

    except Exception:
        logger.exception("Помилка під час обробки повідомлення")
        await update.message.reply_text(
            "⚠️ Не вдалося записати операцію. Перевір налаштування Supabase або спробуй ще раз."
        )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cash", show_cash))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("DvirFinanceBot запущено")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
