import json
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("dvirfinancebot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TABLE_URL = f"{SUPABASE_URL}/rest/v1/cash_operations"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
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
Я веду внутрішній облік каси й уже розумію звичайні фрази українською та російською.

Можна написати:
• сьогодні заробили 20000 грн і 100 доларів
• заплатили 1500 грн за доставку
• поміняли 10000 грн на 230 доларів
• яка зараз каса
• покажи звіт за сьогодні
• покажи звіт за місяць
• покажи останні операції
• видали останній запис

Старі короткі команди теж працюють:
• виручка
• витрата
• обмін
• каса
• звіт
• звіт місяць
• історія
• видалити останню

Важливо: борги, ревізія сейфа та картки будуть підключені наступним оновленням.
""".strip()


def today_kyiv() -> date:
    return datetime.now(KYIV_TZ).date()


def normalize_number(raw: str) -> Decimal:
    return Decimal(raw.replace(" ", "").replace("\u00a0", "").replace(",", "."))


def find_amounts(text: str) -> List[Tuple[str, Decimal]]:
    found: List[Tuple[str, Decimal]] = []
    lowered = text.lower()

    matches_with_positions = []
    for currency, aliases in CURRENCY_ALIASES.items():
        aliases_pattern = "|".join(
            re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)
        )
        pattern = rf"(\d+(?:[ \u00a0]\d{{3}})*(?:[.,]\d+)?)\s*(?:{aliases_pattern})"
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            try:
                matches_with_positions.append(
                    (match.start(), currency, normalize_number(match.group(1)))
                )
            except InvalidOperation:
                pass

    matches_with_positions.sort(key=lambda item: item[0])
    for _, currency, amount in matches_with_positions:
        found.append((currency, amount))
    return found


def api_request(
    method: str,
    *,
    params: Optional[dict] = None,
    json_data: Optional[dict] = None,
    prefer: Optional[str] = None,
) -> requests.Response:
    headers = dict(HEADERS)
    if prefer:
        headers["Prefer"] = prefer

    response = requests.request(
        method,
        TABLE_URL,
        headers=headers,
        params=params,
        json=json_data,
        timeout=20,
    )
    if not response.ok:
        logger.error("Supabase error %s: %s", response.status_code, response.text)
        response.raise_for_status()
    return response


def save_operation(
    operation_type: str,
    currency: str,
    amount: Decimal,
    description: str,
    update: Update,
) -> None:
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
    api_request("POST", json_data=payload)


def fetch_operations(
    chat_id: int,
    *,
    date_from: Optional[date] = None,
) -> List[dict]:
    params = {
        "select": (
            "id,created_at,operation_date,operation_type,currency,"
            "amount,description,telegram_full_name"
        ),
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
    signs = {
        "income": Decimal("1"),
        "expense": Decimal("-1"),
        "exchange_in": Decimal("1"),
        "exchange_out": Decimal("-1"),
    }

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
    return "\n".join(
        f"• {format_money(currency, values[currency])}"
        for currency in ("UAH", "USD", "EUR")
    )


def extract_openai_text(response_json: dict) -> str:
    direct = response_json.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    for item in response_json.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

    raise RuntimeError("OpenAI не повернув текстову відповідь")


def understand_with_ai(text: str) -> dict:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "income",
                    "expense",
                    "exchange",
                    "cash",
                    "report_today",
                    "report_month",
                    "history",
                    "delete_last",
                    "help",
                    "unsupported_finance",
                    "unknown",
                ],
            },
            "amounts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "currency": {
                            "type": "string",
                            "enum": ["UAH", "USD", "EUR"],
                        },
                        "amount": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["currency", "amount"],
                },
            },
            "from_amount": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "currency": {
                                "type": "string",
                                "enum": ["UAH", "USD", "EUR"],
                            },
                            "amount": {"type": "number", "exclusiveMinimum": 0},
                        },
                        "required": ["currency", "amount"],
                    },
                    {"type": "null"},
                ]
            },
            "to_amount": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "currency": {
                                "type": "string",
                                "enum": ["UAH", "USD", "EUR"],
                            },
                            "amount": {"type": "number", "exclusiveMinimum": 0},
                        },
                        "required": ["currency", "amount"],
                    },
                    {"type": "null"},
                ]
            },
            "description": {"type": "string"},
            "reply": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "action",
            "amounts",
            "from_amount",
            "to_amount",
            "description",
            "reply",
            "confidence",
        ],
    }

    instructions = """
Ти — класифікатор повідомлень для фінансового Telegram-бота.
Користувачі пишуть українською, російською, суржиком і з помилками.

Визнач одну дію:
- income: надходження, виручка, заробили, отримали оплату.
- expense: витрата, заплатили, купили, віддали гроші.
- exchange: обмін однієї валюти на іншу.
- cash: запит про поточну касу або баланс.
- report_today: звіт за сьогодні.
- report_month: звіт за місяць.
- history: останні операції.
- delete_last: видалити або скасувати останній запис.
- help: допомога або що бот уміє.
- unsupported_finance: борг, ревізія, сейф, картка, склад, прайс,
  товарні залишки або інша фінансова дія, яку ця версія ще не записує.
- unknown: нефінансове або незрозуміле повідомлення.

Виправляй очевидні друкарські помилки за змістом.
Наприклад, "викачка 10000 грн 150 доларів" найімовірніше означає
"виручка 10000 грн 150 доларів" і має бути income.

Для income та expense:
- amounts містить усі суми.
- from_amount і to_amount мають бути null.

Для exchange:
- amounts має бути порожнім.
- from_amount — що віддали.
- to_amount — що отримали.

Не вигадуй сум і валют.
Якщо сума не вказана або сенс неоднозначний, action=unknown.
description — короткий зміст повідомлення мовою користувача.
reply — коротке уточнення тільки для unknown або unsupported_finance.
""".strip()

    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": text,
        "reasoning": {"effort": "low"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "financial_intent",
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": 600,
        "store": False,
    }

    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=45,
    )
    if not response.ok:
        logger.error("OpenAI error %s: %s", response.status_code, response.text)
        response.raise_for_status()

    return json.loads(extract_openai_text(response.json()))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT)


async def show_cash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    await update.message.reply_text(
        "💰 Поточна каса:\n" + totals_lines(get_balance(update.effective_chat.id))
    )


async def show_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    month: bool = False,
) -> None:
    if not update.message or not update.effective_chat:
        return

    today = today_kyiv()
    date_from = today.replace(day=1) if month else today
    totals = calculate_totals(
        fetch_operations(update.effective_chat.id, date_from=date_from)
    )
    period = f"за {today.strftime('%m.%Y')}" if month else "за сьогодні"

    await update.message.reply_text(
        f"📊 Звіт {period}\n\n"
        "💵 Виручка:\n"
        + totals_lines(totals["income"])
        + "\n\n"
        "💸 Витрати:\n"
        + totals_lines(totals["expense"])
        + "\n\n"
        "💰 Рух за період:\n"
        + totals_lines(totals["balance"])
    )


async def show_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_chat:
        return

    rows = fetch_operations(update.effective_chat.id)[:10]
    if not rows:
        await update.message.reply_text("Історія поки порожня.")
        return

    lines = ["🧾 Останні операції:"]
    for row in rows:
        label = OPERATION_LABELS.get(
            row["operation_type"],
            row["operation_type"],
        )
        amount = format_money(
            row["currency"],
            Decimal(str(row["amount"])),
        )
        description = (row.get("description") or "").strip()
        author = (row.get("telegram_full_name") or "").strip()

        if len(description) > 45:
            description = description[:42] + "…"

        extra = f"\n  {description}" if description else ""
        if author:
            extra += f"\n  Вніс: {author}"

        lines.append(
            f"• {row['operation_date']} — {label}: {amount}{extra}"
        )

    await update.message.reply_text("\n".join(lines))


async def remove_last(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_chat:
        return

    row = delete_last_operation(update.effective_chat.id)
    if not row:
        await update.message.reply_text("Немає операцій для видалення.")
        return

    label = OPERATION_LABELS.get(
        row["operation_type"],
        row["operation_type"],
    )
    amount = format_money(
        row["currency"],
        Decimal(str(row["amount"])),
    )
    await update.message.reply_text(
        f"🗑 Видалено останню операцію:\n{label} — {amount}"
    )


async def execute_ai_intent(
    intent: dict,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    action = intent["action"]
    confidence = Decimal(str(intent.get("confidence", 0)))

    if confidence < Decimal("0.65") and action not in {
        "cash",
        "report_today",
        "report_month",
        "history",
        "help",
    }:
        await update.message.reply_text(
            "Я не впевнений, що правильно зрозумів. Напиши трохи точніше."
        )
        return

    if action == "cash":
        await show_cash(update, context)
        return

    if action == "report_today":
        await show_report(update, context, month=False)
        return

    if action == "report_month":
        await show_report(update, context, month=True)
        return

    if action == "history":
        await show_history(update, context)
        return

    if action == "delete_last":
        await remove_last(update, context)
        return

    if action == "help":
        await update.message.reply_text(HELP_TEXT)
        return

    if action in {"income", "expense"}:
        amounts = intent.get("amounts") or []
        if not amounts:
            await update.message.reply_text(
                "Я зрозумів тип операції, але не бачу точної суми."
            )
            return

        saved = []
        for item in amounts:
            currency = item["currency"]
            amount = Decimal(str(item["amount"]))
            save_operation(action, currency, amount, text, update)
            saved.append(format_money(currency, amount))

        label = "Виручку" if action == "income" else "Витрату"
        await update.message.reply_text(
            f"✅ {label} записано: " + ", ".join(saved)
        )
        return

    if action == "exchange":
        from_item = intent.get("from_amount")
        to_item = intent.get("to_amount")
        if not from_item or not to_item:
            await update.message.reply_text(
                "Зрозумів, що це обмін, але не бачу обидві суми."
            )
            return

        from_currency = from_item["currency"]
        from_amount = Decimal(str(from_item["amount"]))
        to_currency = to_item["currency"]
        to_amount = Decimal(str(to_item["amount"]))

        save_operation(
            "exchange_out",
            from_currency,
            from_amount,
            text,
            update,
        )
        save_operation(
            "exchange_in",
            to_currency,
            to_amount,
            text,
            update,
        )

        await update.message.reply_text(
            "✅ Обмін записано:\n"
            f"− {format_money(from_currency, from_amount)}\n"
            f"+ {format_money(to_currency, to_amount)}"
        )
        return

    if action == "unsupported_finance":
        reply = intent.get("reply") or (
            "Я зрозумів зміст, але ця функція ще не підключена."
        )
        await update.message.reply_text(
            "🧠 " + reply + "\n\n"
            "Поки нічого не записав, щоб не перекрутити облік."
        )
        return

    reply = intent.get("reply") or "Не зміг надійно зрозуміти повідомлення."
    await update.message.reply_text(reply + "\n\n" + HELP_TEXT)


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    lowered = text.lower()

    try:
        # Швидкі старі команди залишаються без звернення до AI.
        if lowered in {"каса", "баланс", "/cash"}:
            await show_cash(update, context)
            return

        if lowered.startswith(("виручка", "дохід", "прихід")):
            amounts = find_amounts(text)
            if not amounts:
                await update.message.reply_text(
                    "Не бачу суму. Приклад: виручка 20000 грн 100 доларів"
                )
                return
            for currency, amount in amounts:
                save_operation("income", currency, amount, text, update)
            await update.message.reply_text(
                "✅ Виручку записано: "
                + ", ".join(format_money(c, a) for c, a in amounts)
            )
            return

        if lowered.startswith(("витрата", "видаток", "розхід")):
            amounts = find_amounts(text)
            if not amounts:
                await update.message.reply_text(
                    "Не бачу суму. Приклад: витрата 1500 грн доставка"
                )
                return
            for currency, amount in amounts:
                save_operation("expense", currency, amount, text, update)
            await update.message.reply_text(
                "✅ Витрату записано: "
                + ", ".join(format_money(c, a) for c, a in amounts)
            )
            return

        if lowered.startswith(("обмін", "обмен", "поміняли", "поменяли")):
            amounts = find_amounts(text)
            if len(amounts) != 2:
                await update.message.reply_text(
                    "Приклад: обмін 10000 грн на 230 доларів"
                )
                return
            (from_currency, from_amount), (to_currency, to_amount) = amounts
            save_operation(
                "exchange_out",
                from_currency,
                from_amount,
                text,
                update,
            )
            save_operation(
                "exchange_in",
                to_currency,
                to_amount,
                text,
                update,
            )
            await update.message.reply_text(
                "✅ Обмін записано:\n"
                f"− {format_money(from_currency, from_amount)}\n"
                f"+ {format_money(to_currency, to_amount)}"
            )
            return

        if lowered in {"звіт", "звіт сьогодні", "отчет", "отчет сегодня"}:
            await show_report(update, context, month=False)
            return

        if lowered in {
            "звіт місяць",
            "звіт за місяць",
            "отчет месяц",
            "отчет за месяц",
        }:
            await show_report(update, context, month=True)
            return

        if lowered in {
            "історія",
            "история",
            "останні 10",
            "последние 10",
        }:
            await show_history(update, context)
            return

        if lowered in {
            "видалити останню",
            "удалить последнюю",
            "скасувати останню",
            "отменить последнюю",
        }:
            await remove_last(update, context)
            return

        if lowered.startswith(("допомога", "що вмієш", "помощь")):
            await update.message.reply_text(HELP_TEXT)
            return

        # Усе інше розбирає ChatGPT.
        intent = understand_with_ai(text)
        logger.info(
            "AI intent action=%s confidence=%s text=%r",
            intent.get("action"),
            intent.get("confidence"),
            text[:120],
        )
        await execute_ai_intent(intent, text, update, context)

    except requests.HTTPError as exc:
        logger.exception("Помилка зовнішнього API")
        if exc.response is not None and exc.response.status_code == 401:
            await update.message.reply_text(
                "⚠️ OpenAI не прийняв API-ключ. Перевір OPENAI_API_KEY у Railway."
            )
        elif exc.response is not None and exc.response.status_code == 429:
            await update.message.reply_text(
                "⚠️ OpenAI тимчасово обмежив запити або закінчився баланс API."
            )
        else:
            await update.message.reply_text(
                "⚠️ Не вдалося звернутися до AI. Старі команди продовжують працювати."
            )
    except Exception:
        logger.exception("Помилка обробки повідомлення")
        await update.message.reply_text(
            "⚠️ Сталася помилка. Подробиці записані в Railway Deploy Logs."
        )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cash", show_cash))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("DvirFinanceBot AI v1 запущено, модель %s", OPENAI_MODEL)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
