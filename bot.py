import os
import re
import json
from difflib import SequenceMatcher

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.getenv("SHEET_NAME", "Склад")
PORT = int(os.getenv("PORT", "10000"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

app = Flask(__name__)
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()


def get_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES,
    )


def get_worksheet():
    client = gspread.authorize(get_credentials())
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(SHEET_NAME)


def normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_rows():
    worksheet = get_worksheet()
    values = worksheet.get_all_values()

    if not values:
        return []

    for header_index, raw_header in enumerate(values[:30]):
        headers = [normalize(cell) for cell in raw_header]

        if "товар" not in headers or "осталось" not in headers:
            continue

        product_index = headers.index("товар")
        remaining_index = headers.index("осталось")
        rows = []

        for raw_row in values[header_index + 1:]:
            product = raw_row[product_index].strip() if product_index < len(raw_row) else ""
            if not product:
                continue

            remaining = raw_row[remaining_index].strip() if remaining_index < len(raw_row) else ""

            rows.append({
                "Товар": product,
                "Осталось": remaining,
            })

        return rows

    raise ValueError('На листе "Склад" не найдены колонки "Товар" и "Осталось".')


def search_products(query, rows):
    query = normalize(query)
    words = query.split()
    scored = []

    for row in rows:
        product = normalize(row.get("Товар", ""))
        if not product:
            continue

        score = 0

        if query in product:
            score += 100

        for word in words:
            if word in product:
                score += 30
            else:
                best = max(
                    (SequenceMatcher(None, word, token).ratio() for token in product.split()),
                    default=0,
                )
                if best >= 0.75:
                    score += 15

        if score > 0:
            scored.append((score, product, row))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [item[2] for item in scored]


def parse_number(value):
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return 0

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0

    try:
        return float(match.group())
    except ValueError:
        return 0


def format_number(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_results(query, results):
    if not results:
        return (
            f"🔎 По запросу «{query}» ничего не найдено.\n\n"
            "Попробуй, например:\n"
            "остатки лейка\n"
            "остатки ведро"
        )

    total = sum(parse_number(row.get("Осталось", "")) for row in results)

    lines = [
        f"📦 Остатки по запросу «{query}»",
        f"📊 Общее количество: **{format_number(total)} шт.**",
        f"Найдено товаров: {len(results)}",
        "",
    ]

    for row in results:
        product = str(row.get("Товар", "")).strip()
        remaining = str(row.get("Осталось", "")).strip() or "—"
        lines.append(f"• {product} — **{remaining} шт.**")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Я могу показать остатки товаров из Google Таблицы.\n\n"
        "Напиши:\n"
        "• остатки — все товары\n"
        "• остатки лейка — все лейки\n"
        "• остатки ведро — все вёдра"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = normalize(update.message.text)

    if text == "остатки":
        query = ""
    elif text.startswith("остатки "):
        query = text[len("остатки "):].strip()
    else:
        query = text

    try:
        rows = load_rows()

        if not query:
            results = [
                row for row in rows
                if str(row.get("Осталось", "")).strip() not in ("", "0", "0.0")
            ]
            results.sort(key=lambda r: normalize(r.get("Товар", "")))
            display_query = "все товары"
        else:
            results = search_products(query, rows)
            display_query = query

        message = format_results(display_query, results)

        if len(message) <= 4000:
            await update.message.reply_text(message, parse_mode="Markdown")
        else:
            current = ""
            for line in message.splitlines():
                if len(current) + len(line) + 1 > 3900:
                    await update.message.reply_text(current, parse_mode="Markdown")
                    current = ""
                current += line + "\n"
            if current:
                await update.message.reply_text(current, parse_mode="Markdown")

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        await update.message.reply_text(
            "⚠️ Не удалось получить данные из Google Таблицы.\n"
            "Проверь подключение к листу «Склад»."
        )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


@app.get("/")
def home():
    return "Telegram stock bot is running", 200


@app.get("/health")
def health():
    return "OK", 200


@app.post("/telegram")
async def telegram_webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return "OK", 200


@app.before_request
async def initialize():
    if not telegram_app.initialized:
        await telegram_app.initialize()


if __name__ == "__main__":
    import asyncio

    async def setup_webhook():
        await telegram_app.initialize()
        await telegram_app.bot.set_webhook(
            url=os.environ["WEBHOOK_URL"],
            drop_pending_updates=True,
        )

    asyncio.run(setup_webhook())

    # Flask's development server is sufficient for the small Render hobby deployment.
    app.run(host="0.0.0.0", port=PORT)
