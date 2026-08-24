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

ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

# Optional test-group restriction. If set, lottery processing is limited to this chat.
LOTTERY_CHAT_ID = os.getenv("LOTTERY_CHAT_ID", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

web = Flask(__name__)

# In-memory lottery state for the test.
# The active lottos are discovered from admin posts and kept until all numbers are taken.
lotteries = {}


def get_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SCOPES
    )


def get_spreadsheet():
    return gspread.authorize(get_credentials()).open_by_key(SPREADSHEET_ID)


def normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def find_header_row(values, required_columns, max_rows=30):
    required = {normalize(c) for c in required_columns}
    for i, row in enumerate(values[:max_rows]):
        if required.issubset({normalize(x) for x in row}):
            return i
    return None


def load_sheet_rows(sheet_name, product_column, remaining_column, category):
    values = get_spreadsheet().worksheet(sheet_name).get_all_values()
    if not values:
        return []

    header_index = find_header_row(values, [product_column, remaining_column])
    if header_index is None:
        raise ValueError(f'Не найдены колонки на листе "{sheet_name}"')

    headers = [normalize(x) for x in values[header_index]]
    pi = headers.index(normalize(product_column))
    ri = headers.index(normalize(remaining_column))

    rows = []
    for row in values[header_index + 1:]:
        product = row[pi].strip() if pi < len(row) else ""
        if not product:
            continue
        remaining = row[ri].strip() if ri < len(row) else ""
        rows.append({"Товар": product, "Осталось": remaining, "Источник": category})
    return rows


def load_all_rows():
    return (
        load_sheet_rows(STOCK_SHEET_NAME, "Товар", "Осталось", "📦 СКЛАД")
        + load_sheet_rows(FLOWERS_SHEET_NAME, "Сорт", "Остаток", "🌸 ЦВЕТЫ")
    )


def search_products(query, rows):
    query = normalize(query)
    scored = []
    for row in rows:
        product = normalize(row["Товар"])
        score = 100 if query in product else 0
        for word in query.split():
            if word in product:
                score += 30
            else:
                best = max(
                    (SequenceMatcher(None, word, token).ratio()
                     for token in product.split()), default=0
                )
                if best >= 0.75:
                    score += 15
        if score:
            scored.append((score, product, row))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in scored]


def parse_number(value):
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else 0


def format_number(value):
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def total_for_rows(rows, source=None):
    return sum(
        parse_number(r["Осталось"])
        for r in rows
        if source is None or r["Источник"] == source
    )


def format_group(title, rows):
    if not rows:
        return f"{title}\nНичего не найдено\nИтого по найденному: 0 шт."
    lines = [title, f"Позиций: {len(rows)}", ""]
    for r in rows:
        lines.append(f'• {r["Товар"]} — {r["Осталось"] or "—"} шт.')
    lines += ["", f'Итого по найденному: {format_number(total_for_rows(rows))} шт.']
    return "\n".join(lines)


def format_results(query, all_rows, results):
    stock = total_for_rows(all_rows, "📦 СКЛАД")
    flowers = total_for_rows(all_rows, "🌸 ЦВЕТЫ")
    return "\n".join([
        "📊 ОБЩИЕ ОСТАТКИ",
        f"📦 Склад: {format_number(stock)} шт.",
        f"🌸 Цветы: {format_number(flowers)} шт.",
        f"🔢 Всего: {format_number(stock + flowers)} шт.",
        "",
        f"🔎 Результат поиска: «{query}»",
        "",
        format_group("📦 СКЛАД", [r for r in results if r["Источник"] == "📦 СКЛАД"]),
        "",
        format_group("🌸 ЦВЕТЫ", [r for r in results if r["Источник"] == "🌸 ЦВЕТЫ"]),
    ])


def lottery_chat_allowed(update):
    if not LOTTERY_CHAT_ID:
        return True
    return str(update.effective_chat.id) == LOTTERY_CHAT_ID


def is_allowed_user(update):
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


def is_admin_message(update):
    # Telegram exposes sender_chat for channel posts. For ordinary group posts,
    # verify the sender's administrator status asynchronously in the handler.
    return bool(update.effective_user)


def extract_lottery(text):
    """
    Detect an admin-created lottery post whose text starts with:
    Лото №165

    Numbers are collected from lines containing a standalone number 1..999,
    optionally followed by any text. This matches the user's numbered list.
    """
    if not text:
        return None

    lines = str(text).splitlines()
    if not lines:
        return None

    first = lines[0].strip()
    match = re.match(r"^лото\s*№\s*(\d+)\s*$", normalize(first))
    if not match:
        return None

    lottery_number = int(match.group(1))
    numbers = set()

    for line in lines[1:]:
        # Accept: "1", "1 🌸 Лисюк", "10 — свободен"
        m = re.match(r"^\s*(\d{1,3})(?:\s|$|[🌸🪷🌿\-—:.)])", line)
        if m:
            numbers.add(int(m.group(1)))

    # Require at least 2 numbers so ordinary "Лото №..." text is not treated as a lottery.
    if len(numbers) < 2:
        return None

    return lottery_number, sorted(numbers)


def format_lottery(lot):
    lines = [f'🤖 ЛОТО №{lot["number"]}', ""]
    for n in lot["numbers"]:
        owner = lot["owners"].get(n)
        lines.append(f"{n} — {owner if owner else 'свободен'}")
    free = sum(1 for n in lot["numbers"] if n not in lot["owners"])
    if free == 0:
        lines += ["", "🔴 ЛОТО ЗАКРЫТО", "Все номерки заняты."]
    else:
        lines += ["", f"🟢 Свободно: {free}"]
    return "\n".join(lines)


def parse_reservation(text):
    """
    Strictly accepts exactly:
    лот 165
    номер 4,5,6
    имя Влад

    Whitespace/case are flexible; the three lines and labels are mandatory.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) != 3:
        return None

    m_lot = re.fullmatch(r"лот\s+(\d+)", normalize(lines[0]))
    m_num = re.fullmatch(r"номер\s+(\d+(?:\s*,\s*\d+)*)", normalize(lines[1]))
    m_name = re.fullmatch(r"имя\s+(.+)", lines[2], flags=re.IGNORECASE)

    if not (m_lot and m_num and m_name):
        return None

    nums = [int(x.strip()) for x in m_num.group(1).split(",")]
    if not nums or len(nums) != len(set(nums)):
        return None

    name = m_name.group(1).strip()
    if not name or len(name) > 100:
        return None

    return int(m_lot.group(1)), nums, name


async def create_lottery_from_admin_post(update):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    found = extract_lottery(text)
    if not found:
        return

    lottery_number, numbers = found

    try:
        member = await update.effective_chat.get_member(update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            return
    except Exception as exc:
        print(f"ADMIN CHECK ERROR: {exc}")
        return

    # A new post with the same number replaces the previous in-memory test state.
    lot = {
        "number": lottery_number,
        "numbers": numbers,
        "owners": {},
        "source_message_id": update.message.message_id,
        "board_message_id": None,
    }

    board = await update.message.reply_text(format_lottery(lot))
    lot["board_message_id"] = board.message_id
    lotteries[lottery_number] = lot

    print(f"LOTTERY CREATED: {lottery_number}, numbers={numbers}")


async def handle_lottery_reservation(update):
    if not update.message or not lottery_chat_allowed(update):
        return

    parsed = parse_reservation(update.message.text)
    if not parsed:
        return

    lottery_number, requested_numbers, name = parsed
    lot = lotteries.get(lottery_number)

    if not lot:
        await update.message.reply_text(
            f"❌ Лото №{lottery_number} не найдено или ещё не создано."
        )
        return

    free_numbers = [n for n in requested_numbers if n in lot["numbers"] and n not in lot["owners"]]
    occupied = [n for n in requested_numbers if n in lot["owners"]]
    invalid = [n for n in requested_numbers if n not in lot["numbers"]]

    # Atomic reservation: if anything is wrong, do not partially book the request.
    if invalid:
        await update.message.reply_text(
            "❌ В заявке есть номера, которых нет в лото: "
            + ", ".join(map(str, invalid))
        )
        return

    if occupied:
        details = ", ".join(
            f'№{n} — {lot["owners"][n]}' for n in occupied
        )
        await update.message.reply_text(
            f"❌ Эти номерки уже заняты: {details}"
        )
        return

    if not free_numbers:
        await update.message.reply_text(
            f"🔴 Лото №{lottery_number} уже закрыто."
        )
        return

    for n in free_numbers:
        lot["owners"][n] = name

    try:
        await update.get_bot().edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=lot["board_message_id"],
            text=format_lottery(lot),
        )
    except Exception as exc:
        # Do not roll back the reservation: the state remains authoritative,
        # and the next valid reservation will retry the board update.
        print(f"LOTTERY BOARD UPDATE ERROR: {exc}")

    free_count = sum(1 for n in lot["numbers"] if n not in lot["owners"])

    if free_count == 0:
        await update.message.reply_text(
            f"🔴 Лото №{lottery_number} закрыто.\n"
            f"Номерки {', '.join(map(str, free_numbers))} записаны за {name}.\n"
            "Все номерки заняты."
        )
    else:
        await update.message.reply_text(
            f"✅ Лото №{lottery_number}: "
            f"номерки {', '.join(map(str, free_numbers))} записаны за {name}."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        await update.message.reply_text("🔒 У вас нет доступа к этому боту.")
        return
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Остатки: «остатки» или «остатки лейка».\n\n"
        "Для лото участник должен писать строго:\n"
        "лот 165\n"
        "номер 4,5,6\n"
        "имя Влад"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # First, try the strict lottery format in the test group.
    await handle_lottery_reservation(update)

    if not is_allowed_user(update):
        return

    text = normalize(update.message.text)
    query = "" if text == "остатки" else (
        text[len("остатки "):].strip() if text.startswith("остатки ") else text
    )

    try:
        rows = load_all_rows()
        if not query:
            results = [r for r in rows if parse_number(r["Осталось"]) != 0]
            results.sort(key=lambda r: (
                0 if r["Источник"] == "📦 СКЛАД" else 1,
                normalize(r["Товар"])
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
        await update.message.reply_text("⚠️ Не удалось получить данные из Google Таблицы.")


async def handle_admin_lottery_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not lottery_chat_allowed(update):
        return
    await create_lottery_from_admin_post(update)


def build_application():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", my_id))

    # Admin lottery posts must be inspected before the generic text handler.
    application.add_handler(
        MessageHandler(filters.TEXT | filters.CAPTION, handle_admin_lottery_message),
        group=0,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
        group=1,
    )
    return application


async def set_webhook():
    app = build_application()
    await app.initialize()
    try:
        await app.bot.set_webhook(
            url=f"{RENDER_EXTERNAL_URL}/telegram",
            drop_pending_updates=True,
        )
    finally:
        await app.shutdown()


async def process_update(data):
    app = build_application()
    await app.initialize()
    try:
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    finally:
        await app.shutdown()


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
