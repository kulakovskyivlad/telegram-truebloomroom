import os
import re
import json
import asyncio
from datetime import datetime, timezone
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

# Отдельный лист для постоянного хранения состояния лото.
LOTTERY_STATE_SHEET = os.getenv("LOTTERY_STATE_SHEET", "Лото_бот")

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


# Боту нужна запись в Google Таблицу для сохранения состояния лото.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


web = Flask(__name__)

# Защищаем одновременные изменения лото.
LOTTERY_LOCK = asyncio.Lock()


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES,
    )


def get_spreadsheet():
    return gspread.authorize(get_credentials()).open_by_key(SPREADSHEET_ID)


def get_lottery_worksheet():
    """
    Возвращает лист для хранения состояния лото.
    Если листа ещё нет — создаёт его автоматически.
    """

    spreadsheet = get_spreadsheet()

    try:
        return spreadsheet.worksheet(LOTTERY_STATE_SHEET)

    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=LOTTERY_STATE_SHEET,
            rows=100,
            cols=8,
        )

        worksheet.append_row([
            "lot_number",
            "chat_id",
            "source_message_id",
            "board_message_id",
            "active",
            "slots_json",
            "updated_at",
            "source_text",
        ])

        return worksheet


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def find_header_row(values, required_columns, max_rows=30):
    required = {normalize(c) for c in required_columns}

    for i, row in enumerate(values[:max_rows]):
        if required.issubset({normalize(x) for x in row}):
            return i

    return None


# ============================================================
# GOOGLE SHEETS — ОСТАТКИ
# ============================================================

def load_sheet_rows(
    sheet_name,
    product_column,
    remaining_column,
    category,
):
    values = get_spreadsheet().worksheet(sheet_name).get_all_values()

    if not values:
        return []

    header_index = find_header_row(
        values,
        [product_column, remaining_column],
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
            "Источник": category,
        })

    return rows


def load_all_rows():
    return (
        load_sheet_rows(
            STOCK_SHEET_NAME,
            "Товар",
            "Осталось",
            "📦 СКЛАД",
        )
        +
        load_sheet_rows(
            FLOWERS_SHEET_NAME,
            "Сорт",
            "Остаток",
            "🌸 ЦВЕТЫ",
        )
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
                    (
                        SequenceMatcher(
                            None,
                            word,
                            token,
                        ).ratio()
                        for token in product.split()
                    ),
                    default=0,
                )

                if best >= 0.75:
                    score += 15

        if score:
            scored.append(
                (
                    score,
                    product,
                    row,
                )
            )

    scored.sort(
        key=lambda x: (-x[0], x[1])
    )

    return [
        x[2]
        for x in scored
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
        text,
    )

    if not match:
        return 0

    return float(match.group())


def format_number(value):
    if float(value).is_integer():
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
        "",
    ]

    for r in rows:
        lines.append(
            f'• {r["Товар"]} — '
            f'{r["Осталось"] or "—"} шт.'
        )

    lines += [
        "",
        f"Итого по найденному: "
        f"{format_number(total_for_rows(rows))} шт.",
    ]

    return "\n".join(lines)


def format_results(
    query,
    all_rows,
    results,
):
    stock = total_for_rows(
        all_rows,
        "📦 СКЛАД",
    )

    flowers = total_for_rows(
        all_rows,
        "🌸 ЦВЕТЫ",
    )

    return "\n".join([
        "📊 ОБЩИЕ ОСТАТКИ",
        f"📦 Склад: {format_number(stock)} шт.",
        f"🌸 Цветы: {format_number(flowers)} шт.",
        f"🔢 Всего: {format_number(stock + flowers)} шт.",
        "",
        f"🔎 Результат поиска: «{query}»",
        "",
        format_group(
            "📦 СКЛАД",
            [
                r
                for r in results
                if r["Источник"] == "📦 СКЛАД"
            ],
        ),
        "",
        format_group(
            "🌸 ЦВЕТЫ",
            [
                r
                for r in results
                if r["Источник"] == "🌸 ЦВЕТЫ"
            ],
        ),
    ])


# ============================================================
# ЛОТО — ПАРСИНГ
# ============================================================

def lottery_chat_allowed(chat_id):
    if not LOTTERY_CHAT_ID:
        return True

    return str(chat_id) == LOTTERY_CHAT_ID


def is_allowed_user(update):
    user = update.effective_user

    return (
        user is not None
        and user.id in ALLOWED_USER_IDS
    )


def extract_lottery(text):
    """
    Ищем пост администратора, который начинается:

    Лото №165

    Ниже ищем строки вида:

    1 🌸 Лисюк
    2 🌸 Лисюк
    3 🌸 Кузьменко

    и т.д.
    """

    if not text:
        return None

    lines = str(text).splitlines()

    if not lines:
        return None

    first = lines[0].strip()

    match = re.match(
        r"^лото\s*№\s*(\d+)\s*$",
        normalize(first),
    )

    if not match:
        return None

    lottery_number = int(match.group(1))

    numbers = set()

    for line in lines[1:]:
        m = re.match(
            r"^\s*(\d{1,3})"
            r"(?:\s|$|[🌸🪷🌿\-—:.)])",
            line,
        )

        if m:
            numbers.add(
                int(m.group(1))
            )

    # Нормальное лото должно содержать минимум 2 номерка.
    if len(numbers) < 2:
        return None

    return (
        lottery_number,
        sorted(numbers),
    )


def format_lottery(lot):
    lines = [
        f'🤖 ЛОТО №{lot["number"]}',
        "",
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

    free = sum(
        1
        for number in lot["numbers"]
        if number not in lot["owners"]
    )

    if free == 0:
        lines += [
            "",
            "🔴 ЛОТО ЗАКРЫТО",
            "Все номерки заняты.",
        ]
    else:
        lines += [
            "",
            f"🟢 Свободно: {free}",
        ]

    return "\n".join(lines)


def parse_reservation(text):
    """
    Принимаются только 3 строки:

    лот 165
    номер 4,5,6
    имя Влад

    Также:

    лот 165
    номер 4 5 6
    имя Влад

    И смешанный вариант:

    номер 4, 5 6

    Другой текст бот игнорирует.
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
        normalize(lines[0]),
    )

    numbers_match = re.fullmatch(
        r"номер\s+(\d+(?:\s*[, ]\s*\d+)*)",
        normalize(lines[1]),
    )

    name_match = re.fullmatch(
        r"имя\s+(.+)",
        lines[2],
        flags=re.IGNORECASE,
    )

    if not (
        lottery_match
        and numbers_match
        and name_match
    ):
        return None

    requested_numbers = [
        int(value)
        for value in re.split(
            r"[,\s]+",
            numbers_match.group(1).strip(),
        )
        if value
    ]

    if (
        not requested_numbers
        or len(requested_numbers)
        != len(set(requested_numbers))
    ):
        return None

    name = name_match.group(1).strip()

    if not name or len(name) > 100:
        return None

    return (
        int(lottery_match.group(1)),
        requested_numbers,
        name,
    )


# ============================================================
# ЛОТО — СОХРАНЕНИЕ В GOOGLE SHEETS
# ============================================================

def ensure_lottery_sheet():
    """
    Создаёт вкладку Лото_бот, если её ещё нет.
    """

    return get_lottery_worksheet()


def read_lotteries():
    """
    Загружает все активные лото из Google Таблицы.

    Благодаря этому состояние не теряется после:
    - перезапуска Render;
    - нового deploy;
    - изменения bot.py.
    """

    worksheet = ensure_lottery_sheet()

    values = worksheet.get_all_values()

    if len(values) <= 1:
        return []

    headers = [
        normalize(x)
        for x in values[0]
    ]

    index = {
        name: i
        for i, name in enumerate(headers)
    }

    result = []

    for row in values[1:]:
        if not row:
            continue

        try:
            active_index = index["active"]

            active_value = (
                row[active_index]
                if active_index < len(row)
                else ""
            )

            active = (
                str(active_value)
                .strip()
                .lower()
                == "true"
            )

            if not active:
                continue

            slots_json = row[
                index["slots_json"]
            ]

            slots_data = json.loads(
                slots_json
            )

            numbers = [
                int(number)
                for number in slots_data.keys()
            ]

            owners = {}

            for number, owner in slots_data.items():
                if owner:
                    owners[int(number)] = str(owner)

            result.append({
                "number": int(
                    row[index["lot_number"]]
                ),
                "chat_id": int(
                    row[index["chat_id"]]
                ),
                "source_message_id": int(
                    row[index["source_message_id"]]
                ),
                "board_message_id": int(
                    row[index["board_message_id"]]
                ),
                "numbers": sorted(numbers),
                "owners": owners,
            })

        except Exception as exc:
            print(
                f"LOTTERY STATE ERROR: {exc}"
            )

    return result


def find_lottery(lottery_number):
    """
    Ищет активное лото непосредственно
    в сохранённом состоянии.
    """

    lotteries = read_lotteries()

    for lot in lotteries:
        if lot["number"] == lottery_number:
            return lot

    return None


def save_lottery(lot, active=True):
    """
    Создаёт или обновляет запись лото
    в листе Лото_бот.
    """

    worksheet = ensure_lottery_sheet()

    values = worksheet.get_all_values()

    if not values:
        worksheet.append_row([
            "lot_number",
            "chat_id",
            "source_message_id",
            "board_message_id",
            "active",
            "slots_json",
            "updated_at",
            "source_text",
        ])

        values = worksheet.get_all_values()

    headers = [
        normalize(x)
        for x in values[0]
    ]

    columns = {
        name: i + 1
        for i, name in enumerate(headers)
    }

    target_row = None

    for row_number, row in enumerate(
        values[1:],
        start=2,
    ):
        if (
            len(row)
            > columns["lot_number"] - 1
        ):
            existing_number = str(
                row[
                    columns["lot_number"] - 1
                ]
            ).strip()

            if existing_number == str(
                lot["number"]
            ):
                target_row = row_number
                break

    slots_json = json.dumps(
        {
            str(number): lot["owners"].get(
                number,
                "",
            )
            for number in lot["numbers"]
        },
        ensure_ascii=False,
    )

    row_values = [
        lot["number"],
        lot["chat_id"],
        lot["source_message_id"],
        lot["board_message_id"],
        "TRUE" if active else "FALSE",
        slots_json,
        datetime.now(
            timezone.utc
        ).isoformat(),
        lot.get(
            "source_text",
            "",
        ),
    ]

    if target_row:
        worksheet.update(
            f"A{target_row}:H{target_row}",
            [row_values],
        )
    else:
        worksheet.append_row(
            row_values
        )


def close_lottery(lot):
    """
    Помечает лото закрытым.
    """

    save_lottery(
        lot,
        active=False,
    )


# ============================================================
# ЛОТО — СОЗДАНИЕ
# ============================================================

async def create_lottery_from_admin_post(update):
    if not update.message:
        return

    text = (
        update.message.text
        or update.message.caption
        or ""
    )

    found = extract_lottery(text)

    if not found:
        return

    lottery_number, numbers = found

    # Проверяем, что пост написал администратор.
    if not update.effective_user:
        return

    try:
        member = await update.effective_chat.get_member(
            update.effective_user.id
        )

        if member.status not in (
            "administrator",
            "creator",
        ):
            return

    except Exception as exc:
        print(
            f"ADMIN CHECK ERROR: {exc}"
        )
        return

    # Если такое активное лото уже существует,
    # не создаём вторую копию.
    existing = find_lottery(
        lottery_number
    )

    if existing:
        print(
            f"LOTTERY {lottery_number} "
            "ALREADY EXISTS"
        )
        return

    # Не больше двух активных лото.
    active_lotteries = read_lotteries()

    if len(active_lotteries) >= 2:
        print(
            "LOTTERY LIMIT: already 2 active lotteries"
        )

        await update.message.reply_text(
            "⚠️ Сейчас уже есть 2 активных лото."
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
        "chat_id": (
            update.effective_chat.id
        ),
        "source_text": text,
    }

    # Создаём сообщение-табло.
    board = await update.message.reply_text(
        format_lottery(lot)
    )

    lot["board_message_id"] = (
        board.message_id
    )

    # Сразу сохраняем в Google Таблицу.
    save_lottery(
        lot,
        active=True,
    )

    print(
        f"LOTTERY CREATED: "
        f"{lottery_number}, "
        f"numbers={numbers}"
    )


# ============================================================
# ЛОТО — БРОНИРОВАНИЕ
# ============================================================

async def handle_lottery_reservation(update):
    if (
        not update.message
        or not lottery_chat_allowed(
            update.effective_chat.id
        )
    ):
        return

    # Запрос должен быть именно в группе.
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    parsed = parse_reservation(
        update.message.text
    )

    # Если человек написал не по формату —
    # бот ничего не делает.
    if not parsed:
        return

    lottery_number, requested_numbers, name = parsed

    async with LOTTERY_LOCK:

        lot = find_lottery(
            lottery_number
        )

        if not lot:
            await update.message.reply_text(
                f"❌ Лото №{lottery_number} "
                "не найдено или уже закрыто."
            )
            return

        # Проверяем номера.
        invalid = [
            number
            for number in requested_numbers
            if number not in lot["numbers"]
        ]

        if invalid:
            await update.message.reply_text(
                "❌ В этом лото нет номерков: "
                + ", ".join(
                    map(str, invalid)
                )
            )
            return

        occupied = [
            number
            for number in requested_numbers
            if number in lot["owners"]
        ]

        if occupied:
            details = ", ".join(
                f"№{number} — "
                f"{lot['owners'][number]}"
                for number in occupied
            )

            await update.message.reply_text(
                "❌ Эти номерки уже заняты:\n"
                + details
            )

            return

        # Все номера свободны.
        for number in requested_numbers:
            lot["owners"][number] = name

        free_count = sum(
            1
            for number in lot["numbers"]
            if number not in lot["owners"]
        )

        # Если все заняты — лото закрываем.
        is_closed = (
            free_count == 0
        )

        # Сначала сохраняем состояние.
        save_lottery(
            lot,
            active=not is_closed,
        )

        # Обновляем табло.
        try:
            await update.get_bot().edit_message_text(
                chat_id=lot["chat_id"],
                message_id=lot["board_message_id"],
                text=format_lottery(lot),
            )

        except Exception as exc:
            print(
                f"LOTTERY BOARD UPDATE ERROR: "
                f"{exc}"
            )

        if is_closed:
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
# ОСНОВНЫЕ КОМАНДЫ
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed_user(update):
        await update.message.reply_text(
            "🔒 У вас нет доступа к этому боту."
        )
        return

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Остатки: «остатки» "
        "или «остатки лейка».\n\n"
        "Для лото участник должен писать строго:\n"
        "лот 165\n"
        "номер 4,5,6\n"
        "имя Влад\n\n"
        "Также можно писать номерки через пробел:\n"
        "номер 4 5 6"
    )


async def my_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user:
        await update.message.reply_text(
            f"Ваш Telegram ID: "
            f"{update.effective_user.id}"
        )


# ============================================================
# СООБЩЕНИЯ
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    # Сначала проверяем бронь лото.
    await handle_lottery_reservation(
        update
    )

    if not is_allowed_user(update):
        return

    # Запросы остатков разрешены только
    # в личном чате с ботом.
    if update.effective_chat.type != "private":
        return

    text = normalize(
        update.message.text
    )

    query = (
        ""
        if text == "остатки"
        else (
            text[len("остатки "):].strip()
            if text.startswith("остатки ")
            else text
        )
    )

    try:
        rows = load_all_rows()

        if not query:
            results = [
                r
                for r in rows
                if parse_number(
                    r["Осталось"]
                ) != 0
            ]

            results.sort(
                key=lambda r: (
                    0
                    if r["Источник"]
                    == "📦 СКЛАД"
                    else 1,
                    normalize(
                        r["Товар"]
                    ),
                )
            )

            display_query = "все товары"

        else:
            results = search_products(
                query,
                rows,
            )

            display_query = query

        message = format_results(
            display_query,
            rows,
            results,
        )

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
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        await update.message.reply_text(
            "⚠️ Не удалось получить "
            "данные из Google Таблицы."
        )


# ============================================================
# АДМИНСКИЙ ПОСТ ЛОТО
# ============================================================

async def handle_admin_lottery_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not lottery_chat_allowed(
        update.effective_chat.id
    ):
        return

    # Только групповые чаты.
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    await create_lottery_from_admin_post(
        update
    )


# ============================================================
# APPLICATION
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
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "id",
            my_id,
        )
    )

    # Сначала проверяем админские посты с лото.
    application.add_handler(
        MessageHandler(
            filters.TEXT | filters.CAPTION,
            handle_admin_lottery_message,
        ),
        group=0,
    )

    # Затем обычные сообщения.
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        ),
        group=1,
    )

    return application


# ============================================================
# WEBHOOK
# ============================================================

async def set_webhook():
    app = build_application()

    await app.initialize()

    try:
        await app.bot.set_webhook(
            url=(
                f"{RENDER_EXTERNAL_URL}"
                "/telegram"
            ),
            drop_pending_updates=True,
        )

    finally:
        await app.shutdown()


async def process_update(data):
    app = build_application()

    await app.initialize()

    try:
        update = Update.de_json(
            data,
            app.bot,
        )

        await app.process_update(
            update
        )

    finally:
        await app.shutdown()


@web.get("/")
def home():
    return (
        "Telegram stock bot is running",
        200,
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
            400,
        )

    try:
        asyncio.run(
            process_update(data)
        )

        return "OK", 200

    except Exception as exc:
        print(
            f"WEBHOOK ERROR: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return (
            "Internal Server Error",
            500,
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
        port=PORT,
    )