import os
import re
import json
import asyncio
from difflib import SequenceMatcher

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

STOCK_SHEET_NAME = os.getenv("SHEET_NAME", "Склад")
FLOWERS_SHEET_NAME = os.getenv("FLOWERS_SHEET_NAME", "Цветы")

PORT = int(os.getenv("PORT", "10000"))
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]

# Telegram IDs allowed to use the bot, separated by commas.
# Example: 123456789,987654321
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

web = Flask(__name__)


def get_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES,
    )


def get_spreadsheet():
    client = gspread.authorize(get_credentials())
    return client.open_by_key(SPREADSHEET_ID)


def normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def find_header_row(values, required_columns, max_rows=30):
    required = {normalize(column) for column in required_columns}
    for header_index, raw_header in enumerate(values[:max_rows]):
        headers = {normalize(cell) for cell in raw_header}
        if required.issubset(headers):
            return header_index
    return None


def load_sheet_rows(sheet_name, product_column, remaining_column, category):
    spreadsheet = get_spreadsheet()
    worksheet = spreadsheet.worksheet(sheet_name)
    values = worksheet.get_all_values()

    if not values:
        return []

    header_index = find_header_row(values, [product_column, remaining_column])
    if header_index is None:
        raise ValueError(
            f'На листе "{sheet_name}" не найдены колонки '
            f'"{product_column}" и "{remaining_column}".'
        )

    headers = [normalize(cell) for cell in values[header_index]]
    product_index = headers.index(normalize(product_column))
    remaining_index = headers.index(normalize(remaining_column))

    rows = []
    for raw_row in values[header_index + 1:]:
        product = raw_row[product_index].strip() if product_index < len(raw_row) else ""
        if not product:
            continue
        remaining = raw_row[remaining_index].strip() if remaining_index < len(raw_row) else ""
        rows.append({
            "Товар": product,
            "Осталось": remaining,
            "Источник": category,
        })

    return rows


def load_all_rows():
    return (
        load_sheet_rows(STOCK_SHEET_NAME, "Товар", "Осталось", "📦 СКЛАД")
        + load_sheet_rows(FLOWERS_SHEET_NAME, "Сорт", "Остаток", "🌸 ЦВЕТЫ")
    )


def search_products(query, rows):
    query = normalize(query)
    words = query.split()
    scored = []

    for row in rows:
        product = normalize(row.get("Товар", ""))
        if not product:
            continue

        score = 100 if query in product else 0
        for word in words:
            if word in product:
                score += 30
            else:
                best = max(
                    (SequenceMatcher(None, word, token).ratio()
                     for token in product.split()),
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


def total_for_rows(rows, source=None):
    return sum(
        parse_number(row.get("Осталось", ""))
        for row in rows
        if source is None or row.get("Источник") == source
    )


def format_group(title, rows):
    if not rows:
        return f"{title}\nНичего не найдено\nИтого по найденному: 0 шт."

    total = total_for_rows(rows)
    lines = [title, f"Позиций: {len(rows)}", ""]
    for row in rows:
        product = str(row.get("Товар", "")).strip()
        remaining = str(row.get("Осталось", "")).strip() or "—"
        lines.append(f"• {product} — {remaining} шт.")
    lines.extend(["", f"Итого по найденному: {format_number(total)} шт."])
    return "\n".join(lines)


def format_results(query, all_rows, results):
    stock_total = total_for_rows(all_rows, "📦 СКЛАД")
    flowers_total = total_for_rows(all_rows, "🌸 ЦВЕТЫ")

    stock_results = [r for r in results if r.get("Источник") == "📦 СКЛАД"]
    flower_results = [r for r in results if r.get("Источник") == "🌸 ЦВЕТЫ"]

    return "\n".join([
        "📊 ОБЩИЕ ОСТАТКИ",
        f"📦 Склад: {format_number(stock_total)} шт.",
        f"🌸 Цветы: {format_number(flowers_total)} шт.",
        f"🔢 Всего: {format_number(stock_total + flowers_total)} шт.",
        "",
        f"🔎 Результат поиска: «{query}»",
        "",
        format_group("📦 СКЛАД", stock_results),
        "",
        format_group("🌸 ЦВЕТЫ", flower_results),
    ])


def is_allowed(update):
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


async def deny(update):
    if update.message:
        await update.message.reply_text("🔒 У вас нет доступа к этому боту.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update)
        return

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Напиши:\n"
        "• остатки — все остатки отдельно по складу и цветам\n"
        "• остатки лейка — лейки отдельно по складу и цветам\n"
        "• остатки роза — розы отдельно по складу и цветам"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Anyone may use /id so an administrator can learn a user's numeric ID.
    if update.effective_user:
        await update.message.reply_text(
            f"Ваш Telegram ID: {update.effective_user.id}"
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update)
        return

    text = normalize(update.message.text)
    query = "" if text == "остатки" else (
        text[len("остатки "):].strip() if text.startswith("остатки ") else text
    )

    try:
        rows = load_all_rows()

        if not query:
            results = [
                row for row in rows
                if parse_number(row.get("Осталось", "")) != 0
            ]
            results.sort(key=lambda row: (
                0 if row.get("Источник") == "📦 СКЛАД" else 1,
                normalize(row.get("Товар", "")),
            ))
            display_query = "все товары"
        else:
            results = search_products(query, rows)
            display_query = query

        message = format_results(display_query, rows, results)

        if len(message) <= 4000:
            await update.message.reply_text(message)
        else:
            current = ""
            for line in message.splitlines():
                if len(current) + len(line) + 1 > 3900:
                    await update.message.reply_text(current)
                    current = ""
                current += line + "\n"
            if current:
                await update.message.reply_text(current)

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        await update.message.reply_text(
            '⚠️ Не удалось получить данные из Google Таблицы. '
            'Проверь листы "Склад" и "Цветы".'
        )


def build_application():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    return application


async def set_webhook():
    application = build_application()
    await application.initialize()
    try:
        await application.bot.set_webhook(
            url=f"{RENDER_EXTERNAL_URL}/telegram",
            drop_pending_updates=True,
        )
    finally:
        await application.shutdown()


async def process_update(data):
    application = build_application()
    await application.initialize()
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    finally:
        await application.shutdown()


@web.get("/")
def home():
    return "Telegram stock bot is running", 200


@web.get("/health")
def health():
    return "OK", 200


@web.post("/telegram")
def telegram_webhook():
    data = request.get_json(silent=True)
    if not data:
        return "Bad Request", 400
    try:
        asyncio.run(process_update(data))
        return "OK", 200
    except Exception as exc:
        print(f"WEBHOOK ERROR: {type(exc).__name__}: {exc}")
        return "Internal Server Error", 500


if __name__ == "__main__":
    asyncio.run(set_webhook())
    web.run(host="0.0.0.0", port=PORT)
