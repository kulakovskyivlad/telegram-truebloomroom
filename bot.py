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

# Если задан ID группы, лото работает только в этой группе.
# Если переменная не задана — лото может работать в любой группе,
# где находится бот.
LOTTERY_CHAT_ID = os.getenv("LOTTERY_CHAT_ID", "").strip()


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


web = Flask(__name__)


# ============================================================
# СОСТОЯНИЕ ЛОТО
# ============================================================

# В памяти храним активные/завершённые лото.
#
# Структура:
# {
#     165: {
#         "number": 165,
#         "numbers": [1, 2, 3, ...],
#         "owners": {
#             1: "Влад",
#             2: "Олена"
#         },
#         "source_message_id": 123,
#         "board_message_id": 124,
#     }
# }
#
# Для текущего теста этого достаточно.
# После перезапуска Render состояние лото сбросится.
lotteries = {}


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES
    )


def get_spreadsheet():
    return gspread.authorize(get_credentials()).open_by_key(SPREADSHEET_ID)


def normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def find_header_row(values, required_columns, max_rows=30):
    required = {normalize(c) for c in required_columns}

    for i, row in enumerate(values[:max_rows]):
        row_headers = {normalize(x) for x in row}

        if required.issubset(row_headers):
            return i

    return None


def load_sheet_rows(
    sheet_name,
    product_column,
    remaining_column,
    category
):
    values = get_spreadsheet().worksheet(sheet_name).get_all_values()

    if not values:
        return []

    header_index = find_header_row(
        values,
        [product_column, remaining_column]
    )

    if header_index is None:
        raise ValueError(
            f'Не найдены колонки на листе "{sheet_name}"'
        )

    headers = [
        normalize(x)
        for x in values[header_index]
    ]

    product_index = headers.index(
        normalize(product_column)
    )

    remaining_index = headers.index(
        normalize(remaining_column)
    )

    rows = []

    for row in values[header_index + 1:]:
        product = (
            row[product_index].strip()
            if product_index < len(row)
            else ""
        )

        if not product:
            continue

        remaining = (
            row[remaining_index].strip()
            if remaining_index < len(row)
            else ""
        )

        rows.append({
            "Товар": product,
            "Осталось": remaining,
            "Источник": category
        })

    return rows


def load_all_rows():
    return (
        load_sheet_rows(
            STOCK_SHEET_NAME,
            "Товар",
            "Осталось",
            "📦 СКЛАД"
        )
        +
        load_sheet_rows(
            FLOWERS_SHEET_NAME,
            "Сорт",
            "Остаток",
            "🌸 ЦВЕТЫ"
        )
    )


# ============================================================
# ПОИСК ОСТАТКОВ
# ============================================================

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
                    (
                        SequenceMatcher(
                            None,
                            word,
                            token
                        ).ratio()
                        for token in product.split()
                    ),
                    default=0
                )

                if best >= 0.75:
                    score += 15

        if score:
            scored.append(
                (score, product, row)
            )

    scored.sort(
        key=lambda x: (-x[0], x[1])
    )

    return [
        item[2]
        for item in scored
    ]


def parse_number(value):
    text = (
        str(value or "")
        .strip()
        .replace(" ", "")
        .replace(",", ".")
    )

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        text
    )

    return float(match.group()) if match else 0


def format_number(value):
    value = float(value)

    if value.is_integer():
        return str(int(value))

    return (
        f"{value:.2f}"
        .rstrip("0")
        .rstrip(".")
    )


def total_for_rows(rows, source=None):
    return sum(
        parse_number(r["Осталось"])
        for r in rows
        if source is None
        or r["Источник"] == source
    )


def format_group(title, rows):

    if not rows:
        return (
            f"{title}\n"
            "Ничего не найдено\n"
            "Итого по найденному: 0 шт."
        )

    lines = [
        title,
        f"Позиций: {len(rows)}",
        ""
    ]

    for row in rows:
        lines.append(
            f'• {row["Товар"]} — '
            f'{row["Осталось"] or "—"} шт.'
        )

    lines += [
        "",
        "Итого по найденному: "
        f'{format_number(total_for_rows(rows))} шт.'
    ]

    return "\n".join(lines)


def format_results(query, all_rows, results):

    stock_total = total_for_rows(
        all_rows,
        "📦 СКЛАД"
    )

    flowers_total = total_for_rows(
        all_rows,
        "🌸 ЦВЕТЫ"
    )

    return "\n".join([
        "📊 ОБЩИЕ ОСТАТКИ",
        "",
        f"📦 Склад: {format_number(stock_total)} шт.",
        f"🌸 Цветы: {format_number(flowers_total)} шт.",
        f"🔢 Всего: {format_number(stock_total + flowers_total)} шт.",
        "",
        f"🔎 Результат поиска: «{query}»",
        "",
        format_group(
            "📦 СКЛАД",
            [
                r for r in results
                if r["Источник"] == "📦 СКЛАД"
            ]
        ),
        "",
        format_group(
            "🌸 ЦВЕТЫ",
            [
                r for r in results
                if r["Источник"] == "🌸 ЦВЕТЫ"
            ]
        )
    ])


# ============================================================
# ЛОТО
# ============================================================

def lottery_chat_allowed(update):
    if not LOTTERY_CHAT_ID:
        return True

    return (
        str(update.effective_chat.id)
        == LOTTERY_CHAT_ID
    )


def is_allowed_user(update):
    user = update.effective_user

    return (
        user is not None
        and user.id in ALLOWED_USER_IDS
    )


def extract_lottery(text):
    """
    Определяем лото, если первая строка сообщения строго:

    Лото №165

    После этого ищем номера.
    Например:

    1 🌸
    2 🌸
    3 🌸
    ...
    11 🌸
    """

    if not text:
        return None

    lines = str(text).splitlines()

    if not lines:
        return None

    first_line = lines[0].strip()

    match = re.match(
        r"^лото\s*№\s*(\d+)\s*$",
        normalize(first_line)
    )

    if not match:
        return None

    lottery_number = int(match.group(1))

    numbers = set()

    for line in lines[1:]:

        # Принимаем варианты:
        #
        # 1
        # 1 🌸
        # 1 — ...
        # 10 🌸
        #
        match_number = re.match(
            r"^\s*(\d{1,3})"
            r"(?:\s|$|[🌸🪷🌿\-—:.)])",
            line
        )

        if match_number:
            numbers.add(
                int(match_number.group(1))
            )

    # Не считаем сообщение лото,
    # если найдено меньше двух номеров.
    if len(numbers) < 2:
        return None

    return (
        lottery_number,
        sorted(numbers)
    )


def format_lottery(lot):

    lines = [
        f'🤖 ЛОТО №{lot["number"]}',
        ""
    ]

    for number in lot["numbers"]:

        owner = lot["owners"].get(number)

        if owner:
            lines.append(
                f"{number} — {owner}"
            )
        else:
            lines.append(
                f"{number} — свободен"
            )

    free_count = sum(
        1
        for number in lot["numbers"]
        if number not in lot["owners"]
    )

    lines.append("")

    if free_count == 0:

        lines.append(
            "🔴 ЛОТО ЗАКРЫТО"
        )

        lines.append(
            "Все номерки заняты."
        )

    else:

        lines.append(
            f"🟢 Свободно: {free_count}"
        )

    return "\n".join(lines)


def parse_reservation(text):
    """
    Разрешён только такой формат:

    лот 165
    номер 4,5,6
    имя Влад

    Допускается изменение регистра и пробелов,
    но обязательно должно быть ровно 3 строки.
    """

    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip()
    ]

    if len(lines) != 3:
        return None

    lottery_match = re.fullmatch(
        r"лот\s+(\d+)",
        normalize(lines[0])
    )

    numbers_match = re.fullmatch(
        r"номер\s+(\d+(?:\s*,\s*\d+)*)",
        normalize(lines[1])
    )

    name_match = re.fullmatch(
        r"имя\s+(.+)",
        lines[2],
        flags=re.IGNORECASE
    )

    if not (
        lottery_match
        and numbers_match
        and name_match
    ):
        return None

    lottery_number = int(
        lottery_match.group(1)
    )

    requested_numbers = [
        int(value.strip())
        for value in
        numbers_match.group(1).split(",")
    ]

    # Дубликаты номеров в одной заявке запрещаем.
    if (
        not requested_numbers
        or
        len(requested_numbers)
        != len(set(requested_numbers))
    ):
        return None

    name = name_match.group(1).strip()

    if not name or len(name) > 100:
        return None

    return (
        lottery_number,
        requested_numbers,
        name
    )


def lottery_free_count(lot):
    return sum(
        1
        for number in lot["numbers"]
        if number not in lot["owners"]
    )


def active_lottery_count():
    count = 0

    for lot in lotteries.values():

        if lottery_free_count(lot) > 0:
            count += 1

    return count


async def create_lottery_from_admin_post(update):

    if not update.message:
        return

    text = (
        update.message.text
        or
        update.message.caption
        or
        ""
    )

    found = extract_lottery(text)

    if not found:
        return

    lottery_number, numbers = found

    # Только администратор группы может создать лото.
    try:

        member = await update.effective_chat.get_member(
            update.effective_user.id
        )

        if member.status not in (
            "administrator",
            "creator"
        ):
            return

    except Exception as exc:

        print(
            f"ADMIN CHECK ERROR: {exc}"
        )

        return

    # Если такое лото уже существует
    # и ещё не заполнено — не создаём второе.
    existing = lotteries.get(
        lottery_number
    )

    if existing:

        if lottery_free_count(existing) > 0:

            return

        # Закрытое лото нельзя открыть повторно
        # тем же номером в рамках текущей сессии.
        await update.message.reply_text(
            f"🔴 Лото №{lottery_number} "
            "уже было закрыто."
        )

        return

    # Одновременно максимум 2 активных лото.
    if active_lottery_count() >= 2:

        await update.message.reply_text(
            "⚠️ Сейчас уже есть два "
            "активных лото.\n"
            "Сначала завершите одно из них."
        )

        return

    lot = {
        "number": lottery_number,
        "numbers": numbers,
        "owners": {},
        "source_message_id": (
            update.message.message_id
        ),
        "board_message_id": None,
    }

    # Бот создаёт своё отдельное табло.
    board = await update.message.reply_text(
        format_lottery(lot)
    )

    lot["board_message_id"] = (
        board.message_id
    )

    lotteries[lottery_number] = lot

    print(
        f"LOTTERY CREATED: "
        f"{lottery_number}, "
        f"numbers={numbers}"
    )


async def handle_lottery_reservation(update):

    if not update.message:
        return

    if not lottery_chat_allowed(update):
        return

    parsed = parse_reservation(
        update.message.text
    )

    # Любой другой текст в группе
    # полностью игнорируем.
    if not parsed:
        return

    (
        lottery_number,
        requested_numbers,
        name
    ) = parsed

    lot = lotteries.get(
        lottery_number
    )

    if not lot:

        await update.message.reply_text(
            f"❌ Лото №{lottery_number} "
            "не найдено или ещё не создано."
        )

        return

    # Если все номера уже заняты,
    # лото закрыто.
    if lottery_free_count(lot) == 0:

        await update.message.reply_text(
            f"🔴 Лото №{lottery_number} "
            "уже закрыто."
        )

        return

    invalid_numbers = [
        number
        for number in requested_numbers
        if number not in lot["numbers"]
    ]

    occupied_numbers = [
        number
        for number in requested_numbers
        if number in lot["owners"]
    ]

    # Если хотя бы одного номера нет
    # в лото — всю заявку отклоняем.
    if invalid_numbers:

        await update.message.reply_text(
            "❌ В заявке есть номера, "
            "которых нет в этом лото: "
            +
            ", ".join(
                map(str, invalid_numbers)
            )
        )

        return

    # Если хотя бы один номер уже занят,
    # НЕ бронируем остальные.
    if occupied_numbers:

        details = ", ".join(
            f'№{number} — '
            f'{lot["owners"][number]}'
            for number in occupied_numbers
        )

        await update.message.reply_text(
            "❌ Эти номерки уже заняты:\n"
            + details
        )

        return

    # Все номера свободны — записываем их.
    for number in requested_numbers:

        lot["owners"][number] = name

    # Обновляем сообщение-табло бота.
    try:

        await update.get_bot().edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=lot["board_message_id"],
            text=format_lottery(lot)
        )

    except Exception as exc:

        print(
            f"LOTTERY BOARD UPDATE ERROR: "
            f"{exc}"
        )

    free_count = lottery_free_count(
        lot
    )

    if free_count == 0:

        await update.message.reply_text(
            f"🔴 Лото №{lottery_number} "
            "закрыто.\n"
            f"Номерки "
            f"{', '.join(map(str, requested_numbers))} "
            f"записаны за {name}.\n"
            "Все номерки заняты."
        )

    else:

        await update.message.reply_text(
            f"✅ Лото №{lottery_number}: "
            f"номерки "
            f"{', '.join(map(str, requested_numbers))} "
            f"записаны за {name}."
        )


# ============================================================
# КОМАНДЫ
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # /start имеет смысл только в личке.
    if (
        update.effective_chat
        and update.effective_chat.type != "private"
    ):
        return

    if not is_allowed_user(update):

        await update.message.reply_text(
            "🔒 У вас нет доступа к этому боту."
        )

        return

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Остатки:\n"
        "«остатки»\n"
        "или\n"
        "«остатки лейка»\n\n"
        "Для лото участник должен писать строго:\n\n"
        "лот 165\n"
        "номер 4,5,6\n"
        "имя Влад"
    )


async def my_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # ID можно запросить в любом чате.
    if update.effective_user:

        await update.message.reply_text(
            f"Ваш Telegram ID: "
            f"{update.effective_user.id}"
        )


# ============================================================
# ОБЩИЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """
    Главное разделение:

    ЛИЧКА:
        только остатки.

    ГРУППА:
        только лото.

    Поэтому запрос «остатки лейка»,
    написанный в группе, никогда не попадёт
    в Google Sheets.
    """

    if not update.message:
        return

    chat = update.effective_chat

    # --------------------------------------------------------
    # ГРУППА
    # --------------------------------------------------------

    if chat and chat.type in (
        "group",
        "supergroup"
    ):

        # В группе только бронирование лото.
        await handle_lottery_reservation(
            update
        )

        return

    # --------------------------------------------------------
    # ЛИЧКА
    # --------------------------------------------------------

    if not chat or chat.type != "private":
        return

    # Только разрешённые пользователи
    # могут пользоваться остатками.
    if not is_allowed_user(update):
        return

    text = normalize(
        update.message.text
    )

    if text == "остатки":

        query = ""

    elif text.startswith("остатки "):

        query = text[
            len("остатки "):
        ].strip()

    else:

        # Сохраняем существующее поведение:
        # можно написать название товара напрямую.
        query = text

    try:

        rows = load_all_rows()

        if not query:

            results = [
                row
                for row in rows
                if parse_number(
                    row["Осталось"]
                ) != 0
            ]

            results.sort(
                key=lambda row: (
                    0
                    if row["Источник"]
                    == "📦 СКЛАД"
                    else 1,
                    normalize(
                        row["Товар"]
                    )
                )
            )

            display_query = (
                "все товары"
            )

        else:

            results = search_products(
                query,
                rows
            )

            display_query = query

        message = format_results(
            display_query,
            rows,
            results
        )

        # Telegram ограничивает сообщение
        # примерно 4096 символами.
        if len(message) <= 4000:

            await update.message.reply_text(
                message
            )

        else:

            current = ""

            for line in message.splitlines():

                if (
                    len(current)
                    + len(line)
                    + 1
                    > 3900
                ):

                    await update.message.reply_text(
                        current
                    )

                    current = ""

                current += line + "\n"

            if current:

                await update.message.reply_text(
                    current
                )

    except Exception as exc:

        print(
            f"ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        await update.message.reply_text(
            "⚠️ Не удалось получить "
            "данные из Google Таблицы."
        )


# ============================================================
# ОБРАБОТКА ПОСТОВ ЛОТО АДМИНИСТРАТОРОВ
# ============================================================

async def handle_admin_lottery_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    chat = update.effective_chat

    # Только группы.
    if not chat or chat.type not in (
        "group",
        "supergroup"
    ):
        return

    if not lottery_chat_allowed(update):
        return

    await create_lottery_from_admin_post(
        update
    )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

def build_application():

    application = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .updater(None)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "id",
            my_id
        )
    )

    # Сначала проверяем сообщения группы
    # на создание нового лото.
    application.add_handler(
        MessageHandler(
            filters.TEXT | filters.CAPTION,
            handle_admin_lottery_message
        ),
        group=0
    )

    # Затем общий обработчик.
    #
    # В группе он занимается только лото.
    # В личке — только остатками.
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        ),
        group=1
    )

    return application


# ============================================================
# WEBHOOK / RENDER
# ============================================================

async def set_webhook():

    application = build_application()

    await application.initialize()

    try:

        await application.bot.set_webhook(
            url=(
                f"{RENDER_EXTERNAL_URL}"
                "/telegram"
            ),
            drop_pending_updates=True
        )

    finally:

        await application.shutdown()


async def process_update(data):

    application = build_application()

    await application.initialize()

    try:

        update = Update.de_json(
            data,
            application.bot
        )

        await application.process_update(
            update
        )

    finally:

        await application.shutdown()


@web.get("/")
def home():

    return (
        "Telegram stock bot is running",
        200
    )


@web.get("/health")
def health():

    return "OK", 200


@web.post("/telegram")
def telegram_webhook():

    data = request.get_json(
        silent=True
    )

    if not data:

        return (
            "Bad Request",
            400
        )

    try:

        asyncio.run(
            process_update(data)
        )

        return "OK", 200

    except Exception as exc:

        print(
            f"WEBHOOK ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

        return (
            "Internal Server Error",
            500
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        set_webhook()
    )

    web.run(
        host="0.0.0.0",
        port=PORT
    )