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
LOTTERY_TOPIC_ID = os.getenv("LOTTERY_TOPIC_ID", "").strip()

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
    Если листа ещё нет — создаёт его.

    Дополнительно используется колонка reservation_meta_json:
    в ней сохраняем Telegram ID человека, который сделал заявку,
    и информацию о том, был ли номер записан на самого покупателя.
    """

    spreadsheet = get_spreadsheet()

    try:
        worksheet = spreadsheet.worksheet(LOTTERY_STATE_SHEET)

    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=LOTTERY_STATE_SHEET,
            rows=100,
            cols=9,
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
            "reservation_meta_json",
        ])

        return worksheet

    # Если лист уже существует со старой структурой,
    # добавляем недостающую колонку.
    if worksheet.col_count < 9:
        worksheet.add_cols(9 - worksheet.col_count)

    values = worksheet.get_all_values()

    if not values:
        worksheet.update(
            "A1:I1",
            [[
                "lot_number",
                "chat_id",
                "source_message_id",
                "board_message_id",
                "active",
                "slots_json",
                "updated_at",
                "source_text",
                "reservation_meta_json",
            ]],
        )

    else:
        headers = [
            normalize(x)
            for x in values[0]
        ]

        if "reservation_meta_json" not in headers:
            worksheet.update(
                "I1",
                [["reservation_meta_json"]],
            )

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

def lottery_topic_allowed(update):
    if not update.message:
        return False

    if not LOTTERY_TOPIC_ID:
        return True

    return str(
        update.message.message_thread_id
    ) == LOTTERY_TOPIC_ID

def is_allowed_user(update):
    user = update.effective_user

    return (
        user is not None
        and user.id in ALLOWED_USER_IDS
    )


def extract_lottery(text):
    """
    Создание нового лото.

    Поддерживается только новый формат:

    Лото №165

    Количество номерков: 10

    Бот создаёт номера от 1 до 10.
    """

    if not text:
        return None

    lines = [
        line.strip()
        for line in str(text).splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    # ========================================================
    # ПЕРВАЯ СТРОКА — НОМЕР ЛОТО
    # ========================================================

    first_line = lines[0]

    lottery_match = re.fullmatch(
        r"лото\s*№\s*(\d+)",
        normalize(first_line),
        flags=re.IGNORECASE,
    )

    if not lottery_match:
        return None

    lottery_number = int(
        lottery_match.group(1)
    )

    # ========================================================
    # ИЩЕМ КОЛИЧЕСТВО НОМЕРКОВ
    # ========================================================

    numbers_count = None

    for line in lines[1:]:

        match = re.fullmatch(
            r"кількість\s+спроб\s*:\s*(\d+)",
            normalize(line),
            flags=re.IGNORECASE,
        )

        if match:
            numbers_count = int(
                match.group(1)
            )
            break

    # ========================================================
    # ПРОВЕРЯЕМ КОЛИЧЕСТВО
    # ========================================================

    if numbers_count is None:
        return None

    if numbers_count < 1:
        return None

    if numbers_count > 1000:
        return None

    # ========================================================
    # СОЗДАЁМ НОМЕРКИ 1 ... N
    # ========================================================

    numbers = list(
        range(
            1,
            numbers_count + 1,
        )
    )

    return (
        lottery_number,
        numbers,
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


def get_user_display_name(user):
    """
    Имя человека из Telegram.
    """

    if not user:
        return "Пользователь"

    parts = []

    if user.first_name:
        parts.append(
            user.first_name.strip()
        )

    if user.last_name:
        parts.append(
            user.last_name.strip()
        )

    if parts:
        return " ".join(parts)

    if user.username:
        return user.username

    return f"Пользователь {user.id}"


def clean_name(value):
    """
    Очищает имя от лишних знаков.
    """

    value = str(value or "").strip()

    value = re.sub(
        r"^[,;:.\-—–]+",
        "",
        value,
    )

    value = re.sub(
        r"[,;:.\-—–]+$",
        "",
        value,
    )

    return value.strip()


RESERVATION_STOP_WORDS = {
    "лот",
    "номер",
    "номера",
    "номерок",
    "номерки",
    "возьму",
    "беру",
    "хочу",
    "забронируйте",
    "забронировать",
    "запишите",
    "записать",
    "мне",
    "пожалуйста",
}


def parse_reservation(
    text,
    active_lottery_numbers=None,
):
    """
    Разбор заявок на лото.

    Поддерживает:

    ТОЛЬКО НОМЕРА:
        5
        5 7 10
        5,7,10
        5/7/10

    ОДИН ЧЕЛОВЕК:
        5 Влад
        5 7 10 Влад
        5,7,10 Влад
        5/7/10 Влад

        Влад 5
        Влад 5 7 10
        Влад 5,7,10
        Влад 5/7/10

    НЕСКОЛЬКО ЛЮДЕЙ:
        1 Влад 2 Полина 3 Настя
        1,2 Влад 3 Полина
        1/2 Влад 3/4 Полина

        Влад 1 Полина 2 Настя 3
        Влад 1,2 Полина 3,4 Настя 5
        Влад 1/2 Полина 3/4 Настя 5

    Номер лото:
        165 1 2 3
        165 Влад 1 2 3

    Возвращает:

        {
            "lottery_number": ...,
            "assignments": [
                {
                    "number": 1,
                    "name": "Влад"
                },
                ...
            ]
        }

    Если имя не указано:
        "name": None

    Тогда handle_lottery_reservation()
    использует имя Telegram-пользователя.
    """

    text = str(text or "").strip()

    if not text:
        return None

    active_lottery_numbers = [
        int(number)
        for number in (
            active_lottery_numbers or []
        )
    ]

    lottery_number = None

    # ========================================================
    # ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
    # ========================================================

    def clean_name(name):
        """
        Убирает лишние разделители
        вокруг имени.

        Например:

        Полина,  -> Полина
        /Влад/   -> Влад
        Настя.   -> Настя
        """

        return (
            str(name or "")
            .strip()
            .strip(",./")
            .strip()
        )

    def make_assignments(numbers, name=None):
        """
        Превращает:

        [1, 2, 3] + Влад

        в:

        1 -> Влад
        2 -> Влад
        3 -> Влад
        """

        cleaned_name = (
            clean_name(name)
            if name is not None
            else None
        )

        return [
            {
                "number": int(number),
                "name": cleaned_name,
            }
            for number in numbers
        ]

    def extract_numbers(value):
        """
        Извлекает номера из строки.

        1 2 3
        1,2,3
        1/2/3

        -> [1, 2, 3]
        """

        return [
            int(number)
            for number in re.findall(
                r"\d+",
                str(value or ""),
            )
        ]

    def valid_numbers(numbers):
        """
        Номера должны существовать
        и не повторяться.
        """

        return (
            bool(numbers)
            and len(numbers)
            == len(set(numbers))
        )

    def looks_like_lottery_number(number):
        return (
            number in active_lottery_numbers
        )

    # ========================================================
    # УБИРАЕМ "ЛОТ 165"
    #
    # Например:
    #
    # лот 165
    # 1 2 3 Влад
    # ========================================================

    lottery_match = re.search(
        r"\bлот\s*№?\s*(\d+)\b",
        text,
        flags=re.IGNORECASE,
    )

    if lottery_match:

        lottery_number = int(
            lottery_match.group(1)
        )

        text = (
            text[:lottery_match.start()]
            + " "
            + text[lottery_match.end():]
        ).strip()

    # ========================================================
    # УБИРАЕМ "НОМЕР" / "НОМЕРКИ"
    #
    # номер 1 2 3
    # номерки 1/2/3
    # ========================================================

    text = re.sub(
        r"^\s*номер(?:ки)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if not text:
        return None

    # ========================================================
    # ПОДГОТОВКА
    #
    # Запятые и "/" превращаем в пробел.
    #
    # 1,2/3
    #
    # становится:
    #
    # 1 2 3
    #
    # При этом имена вроде:
    #
    # Полина,
    #
    # потом очищаются через clean_name().
    # ========================================================

    normalized_text = re.sub(
        r"[,/]+",
        " ",
        text,
    )

    tokens = [
        token.strip()
        for token in normalized_text.split()
        if token.strip()
    ]

    if not tokens:
        return None

    # ========================================================
    # ВАРИАНТ 1
    #
    # ТОЛЬКО НОМЕРА
    #
    # 1 2 3
    # 1,2,3
    # 1/2/3
    # ========================================================

    if all(
        token.isdigit()
        for token in tokens
    ):

        numbers = [
            int(token)
            for token in tokens
        ]

        # ----------------------------------------------------
        # Если активны два лото:
        #
        # 165 1 2 3
        #
        # первое число может быть номером лото.
        # ----------------------------------------------------

        if (
            lottery_number is None
            and len(active_lottery_numbers) > 1
            and len(numbers) > 1
            and looks_like_lottery_number(
                numbers[0]
            )
        ):
            lottery_number = numbers[0]
            numbers = numbers[1:]

        if not valid_numbers(numbers):
            return None

        return {
            "lottery_number": lottery_number,
            "assignments": make_assignments(
                numbers,
                None,
            ),
        }

    # ========================================================
    # ВАРИАНТ 2
    #
    # НЕСКОЛЬКО ЛЮДЕЙ:
    #
    # НОМЕР -> ИМЯ
    #
    # 1 Влад 2 Полина 3 Настя
    #
    # 1 2 Влад 3 Полина
    #
    # ========================================================

    assignments = []

    i = 0

    while i < len(tokens):

        # ----------------------------------------------------
        # Должен начинаться номер.
        # ----------------------------------------------------

        if not tokens[i].isdigit():
            assignments = []
            break

        number = int(
            tokens[i]
        )

        i += 1

        # ----------------------------------------------------
        # После номера собираем имя
        # до следующего номера.
        # ----------------------------------------------------

        name_parts = []

        while (
            i < len(tokens)
            and not tokens[i].isdigit()
        ):
            name_parts.append(
                tokens[i]
            )
            i += 1

        if not name_parts:
            assignments = []
            break

        name = clean_name(
            " ".join(name_parts)
        )

        if not name:
            assignments = []
            break

        assignments.append({
            "number": number,
            "name": name,
        })

    # --------------------------------------------------------
    # Если получили несколько отдельных
    # номер -> имя, возвращаем их.
    #
    # ВАЖНО:
    #
    # 1 Влад 2 Полина 3 Настя
    #
    # будет:
    #
    # 1 Влад
    # 2 Полина
    # 3 Настя
    # --------------------------------------------------------

    if assignments:

        numbers = [
            item["number"]
            for item in assignments
        ]

        if valid_numbers(numbers):

            # ------------------------------------------------
            # Возможный номер лото в начале:
            #
            # 165 1 Влад 2 Полина
            #
            # ------------------------------------------------

            if (
                lottery_number is None
                and len(active_lottery_numbers) > 1
                and len(assignments) > 1
                and looks_like_lottery_number(
                    assignments[0]["number"]
                )
            ):
                lottery_number = (
                    assignments[0]["number"]
                )

                assignments = (
                    assignments[1:]
                )

            if assignments:

                return {
                    "lottery_number": lottery_number,
                    "assignments": assignments,
                }

    # ========================================================
    # ВАРИАНТ 3
    #
    # ИМЯ -> НОМЕРА
    #
    # Влад 1 2 3
    #
    # Влад 1 Полина 2 Настя 3
    #
    # ========================================================

    assignments = []

    current_name_parts = []

    i = 0

    while i < len(tokens):

        # ----------------------------------------------------
        # Если встретили число:
        #
        # всё, что накопили до него — имя.
        # ----------------------------------------------------

        if tokens[i].isdigit():

            if not current_name_parts:
                assignments = []
                break

            name = clean_name(
                " ".join(
                    current_name_parts
                )
            )

            if not name:
                assignments = []
                break

            # ------------------------------------------------
            # Это первый номер после имени.
            # ------------------------------------------------

            number = int(
                tokens[i]
            )

            assignments.append({
                "number": number,
                "name": name,
            })

            i += 1

            # ------------------------------------------------
            # После номера могут сразу идти
            # дополнительные номера того же человека.
            #
            # Влад 1 2 3 Полина 4
            #
            # ------------------------------------------------

            while (
                i < len(tokens)
                and tokens[i].isdigit()
            ):
                assignments.append({
                    "number": int(
                        tokens[i]
                    ),
                    "name": name,
                })

                i += 1

            # ------------------------------------------------
            # Начинается новая группа имени.
            # ------------------------------------------------

            current_name_parts = []

        else:

            current_name_parts.append(
                tokens[i]
            )

            i += 1

    if assignments:

        numbers = [
            item["number"]
            for item in assignments
        ]

        if valid_numbers(numbers):

            # ------------------------------------------------
            # Возможный номер лото:
            #
            # Влад 165 1 2
            #
            # Но только если активны два лото.
            # ------------------------------------------------

            if (
                lottery_number is None
                and len(active_lottery_numbers) > 1
                and assignments[0]["number"]
                in active_lottery_numbers
            ):
                lottery_number = (
                    assignments[0]["number"]
                )

                assignments = (
                    assignments[1:]
                )

            if assignments:

                return {
                    "lottery_number": lottery_number,
                    "assignments": assignments,
                }

    # ========================================================
    # ВАРИАНТ 4
    #
    # ОДИН ЧЕЛОВЕК:
    #
    # 1 2 3 Влад
    #
    # Этот вариант не прошёл выше,
    # потому что начинается с цифр,
    # но там нет номера перед каждым именем.
    # ========================================================

    # --------------------------------------------------------
    # Ищем последовательность:
    #
    # НОМЕРА + ИМЯ
    #
    # --------------------------------------------------------

    match = re.fullmatch(
        r"(\d+(?:\s+\d+)*)\s+(.+)",
        normalized_text,
        flags=re.IGNORECASE,
    )

    if match:

        numbers = extract_numbers(
            match.group(1)
        )

        name = clean_name(
            match.group(2)
        )

        if (
            name
            and valid_numbers(numbers)
        ):

            if (
                lottery_number is None
                and len(active_lottery_numbers) > 1
                and len(numbers) > 1
                and looks_like_lottery_number(
                    numbers[0]
                )
            ):
                lottery_number = numbers[0]
                numbers = numbers[1:]

            if valid_numbers(numbers):

                return {
                    "lottery_number": lottery_number,
                    "assignments": make_assignments(
                        numbers,
                        name,
                    ),
                }

    # ========================================================
    # ВАРИАНТ 5
    #
    # ОДИН ЧЕЛОВЕК:
    #
    # Влад 1,2,3
    #
    # ========================================================

    match = re.fullmatch(
        r"(.+?)\s+(\d+(?:\s+\d+)*)",
        normalized_text,
        flags=re.IGNORECASE,
    )

    if match:

        name = clean_name(
            match.group(1)
        )

        numbers = extract_numbers(
            match.group(2)
        )

        if (
            name
            and valid_numbers(numbers)
        ):

            if (
                lottery_number is None
                and len(active_lottery_numbers) > 1
                and len(numbers) > 1
                and looks_like_lottery_number(
                    numbers[0]
                )
            ):
                lottery_number = numbers[0]
                numbers = numbers[1:]

            if valid_numbers(numbers):

                return {
                    "lottery_number": lottery_number,
                    "assignments": make_assignments(
                        numbers,
                        name,
                    ),
                }

    # ========================================================
    # НИЧЕГО НЕ РАСПОЗНАЛИ
    # ========================================================

    return None	
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

    Восстанавливает:
    - номера лото;
    - имена владельцев;
    - Telegram user_id;
    - признак self для каждого номера.

    Благодаря этому бот после перезапуска
    понимает, какие номера принадлежат
    конкретному пользователю.
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

            # =================================================
            # ПРОВЕРЯЕМ, АКТИВНО ЛИ ЛОТО
            # =================================================

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

            # =================================================
            # ЗАГРУЖАЕМ НОМЕРА И ИМЕНА
            # =================================================

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

            # =================================================
            # ЗАГРУЖАЕМ RESERVATION META
            # =================================================

            reservation_meta = {}

            if "reservation_meta_json" in index:

                meta_index = index[
                    "reservation_meta_json"
                ]

                meta_text = (
                    row[meta_index]
                    if meta_index < len(row)
                    else ""
                )

                if meta_text:

                    try:
                        reservation_meta = json.loads(
                            meta_text
                        )

                    except Exception as exc:
                        print(
                            "RESERVATION META "
                            f"JSON ERROR: {exc}"
                        )

            # =================================================
            # СОЗДАЁМ ОБЪЕКТ ЛОТО
            # =================================================

            result.append({

                "number": int(
                    row[
                        index["lot_number"]
                    ]
                ),

                "chat_id": int(
                    row[
                        index["chat_id"]
                    ]
                ),

                "source_message_id": int(
                    row[
                        index["source_message_id"]
                    ]
                ),

                "board_message_id": int(
                    row[
                        index["board_message_id"]
                    ]
                ),

                "numbers": sorted(
                    numbers
                ),

                "owners": owners,

                "reservation_meta": (
                    reservation_meta
                ),
            })

        except Exception as exc:

            print(
                "LOTTERY STATE ERROR: "
                f"{type(exc).__name__}: {exc}"
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
            "reservation_meta_json",
        ])

        values = worksheet.get_all_values()

    headers = [
        normalize(x)
        for x in values[0]
    ]

    # На случай старой версии листа.
    if "reservation_meta_json" not in headers:
        worksheet.update(
            "I1",
            [["reservation_meta_json"]],
        )

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

    reservation_meta_json = json.dumps(
        lot.get(
            "reservation_meta",
            {},
        ),
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
        reservation_meta_json,
    ]

    if target_row:
        worksheet.update(
            f"A{target_row}:I{target_row}",
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

def parse_rename_command(text):
    """
    Понимает переименование:

    2 на Антон
    №2 на Антон

    2 10 на Антон
    2,10 на Антон
    2/10 на Антон
    №2 №10 на Антон

    поменяйте 2 на Антон
    поменяйте 2 10 на Антон

    поменяйте на Антон
    перезапишите на Антон
    мои номера на Антон

    Важно:
    2 10
    НЕ является переименованием.
    """

    text = str(text or "").strip()

    if not text:
        return None

    # --------------------------------------------------------
    # НОРМАЛИЗУЕМ ПРОБЕЛЫ
    # --------------------------------------------------------

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    # --------------------------------------------------------
    # ПЕРЕИМЕНОВАНИЕ КОНКРЕТНЫХ НОМЕРОВ
    #
    # 2 на Антон
    # 2 10 на Антон
    # 2,10 на Антон
    # 2/10 на Антон
    # №2 №10 на Антон
    #
    # Также:
    #
    # поменяйте 2 10 на Антон
    # перезапишите 2 на Антон
    # --------------------------------------------------------

    match = re.fullmatch(
        r"(?:(?:поменяйте|перезапишите)\s+)?"
        r"((?:№\s*)?\d{1,3}"
        r"(?:\s*[,/]\s*|\s+)"
        r"(?:№\s*)?\d{1,3})+"
        r"\s+на\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        numbers_text = match.group(1)
        name = clean_name(
            match.group(2)
        )

        numbers = [
            int(number)
            for number in re.findall(
                r"\d{1,3}",
                numbers_text,
            )
        ]

        if numbers and name:
            return {
                "numbers": numbers,
                "number": None,
                "name": name,
            }

    # --------------------------------------------------------
    # ОДИН КОНКРЕТНЫЙ НОМЕР
    #
    # 2 на Антон
    # №2 на Антон
    #
    # поменяйте 2 на Антон
    # перезапишите 2 на Антон
    # --------------------------------------------------------

    match = re.fullmatch(
        r"(?:(?:поменяйте|перезапишите)\s+)?"
        r"(?:№\s*)?(\d{1,3})"
        r"\s+на\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return {
            "numbers": [
                int(match.group(1))
            ],
            "number": int(
                match.group(1)
            ),
            "name": clean_name(
                match.group(2)
            ),
        }

    # --------------------------------------------------------
    # ВСЕ СВОИ НОМЕРА
    #
    # поменяйте на Антон
    # перезапишите на Антон
    # поменяйте меня на Антон
    # перезапишите меня на Антон
    # мои номера на Антон
    # --------------------------------------------------------

    match = re.fullmatch(
        r"(?:поменяйте|перезапишите)"
        r"(?:\s+меня)?"
        r"\s+на\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return {
            "numbers": None,
            "number": None,
            "name": clean_name(
                match.group(1)
            ),
        }

    match = re.fullmatch(
        r"мои\s+номера\s+на\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return {
            "numbers": None,
            "number": None,
            "name": clean_name(
                match.group(1)
            ),
        }

    return None

async def handle_lottery_reservation(update):
    if not update.message:
        return
    if not lottery_chat_allowed(
        update.effective_chat.id
    ):
        return

    if not lottery_topic_allowed(update):
        return

    text = (
        update.message.text
        or update.message.caption
        or ""
    )

    # ========================================================
    # НЕ ОБРАБАТЫВАЕМ СООБЩЕНИЕ, КОТОРОЕ СОЗДАЁТ НОВОЕ ЛОТО
    #
    # Это объявление администратора.
    # Оно должно обрабатываться только
    # create_lottery_from_admin_post().
    # ========================================================

    if extract_lottery(text):
        return

    async with LOTTERY_LOCK:

        # ====================================================
        # ПЕРЕИМЕНОВАНИЕ
        # ====================================================

        rename = parse_rename_command(text)

        if rename:

            active_lotteries = read_lotteries()

            user_id = (
                update.effective_user.id
                if update.effective_user
                else None
            )

            if not user_id:
                return

            # =================================================
            # ПРОВЕРЯЕМ, ЯВЛЯЕТСЯ ЛИ АВТОР СООБЩЕНИЯ АДМИНОМ
            # =================================================

            is_admin = False

            if update.effective_chat and update.effective_user:

                try:
                    member = await update.effective_chat.get_member(
                        update.effective_user.id
                    )

                    is_admin = member.status in (
                        "administrator",
                        "creator",
                    )

                except Exception as exc:
                    print(
                        f"ADMIN CHECK ERROR: {exc}"
                    )

            # =================================================
            # ОПРЕДЕЛЯЕМ НОМЕРА ДЛЯ ПЕРЕИМЕНОВАНИЯ
            # =================================================

            rename_numbers = rename.get(
                "numbers"
            )

            # Совместимость со старым форматом
            # одного номера.

            if rename_numbers is None:

                if rename.get("number") is not None:

                    rename_numbers = [
                        rename["number"]
                    ]

            # =================================================
            # КОНКРЕТНЫЕ НОМЕРА
            #
            # 4 на Влад
            # 2 10 на Антон
            # 2,10 на Антон
            # 2/10 на Антон
            # =================================================

            if rename_numbers is not None:

                # Убираем дубликаты,
                # сохраняя порядок.

                rename_numbers = list(
                    dict.fromkeys(
                        rename_numbers
                    )
                )

                # =================================================
                # АДМИН
                #
                # Администратор может менять чужие номерки.
                #
                # Но только уже занятые номерки.
                # Свободный номер через "4 на Влад"
                # не бронируем.
                # =================================================

                if is_admin:

                    matching_lots = []

                    for lot in active_lotteries:

                        found_numbers = []

                        for number in rename_numbers:

                            if number not in lot["numbers"]:
                                continue

                            # Номер должен быть уже занят.
                            if number not in lot["owners"]:
                                continue

                            found_numbers.append(
                                number
                            )

                        if found_numbers:

                            matching_lots.append(
                                (
                                    lot,
                                    found_numbers,
                                )
                            )

                    # ---------------------------------------------
                    # Ни один номер не найден среди занятых
                    # ---------------------------------------------

                    if not matching_lots:

                        await update.message.reply_text(
                            "❌ Указанные номерки "
                            "не найдены среди занятых."
                        )

                        return

                    # ---------------------------------------------
                    # Если номера находятся в разных лото
                    # ---------------------------------------------

                    unique_lots = []

                    for lot, found_numbers in matching_lots:

                        if lot not in unique_lots:
                            unique_lots.append(
                                lot
                            )

                    if len(unique_lots) > 1:

                        numbers = ", ".join(
                            f"№{lot['number']}"
                            for lot in unique_lots
                        )

                        await update.message.reply_text(
                            "⚠️ Указанные номерки "
                            "находятся в нескольких "
                            "активных лото: "
                            + numbers
                            + ".\n"
                            "Укажите номер лото."
                        )

                        return

                    # ---------------------------------------------
                    # Одно лото
                    # ---------------------------------------------

                    lot = unique_lots[0]

                    changed = []
                    not_found = []

                    for number in rename_numbers:

                        # Номер отсутствует в этом лото.
                        if number not in lot["numbers"]:
                            not_found.append(
                                number
                            )
                            continue

                        # Номер свободен.
                        if number not in lot["owners"]:
                            not_found.append(
                                number
                            )
                            continue

                        # Меняем только имя.
                        lot["owners"][number] = (
                            rename["name"]
                        )

                        # ВАЖНО:
                        # reservation_meta НЕ меняем.
                        #
                        # user_id остаётся прежним.
                        # То есть сохраняется информация,
                        # кто фактически забронировал номер.

                        changed.append(
                            number
                        )

                    if not changed:

                        await update.message.reply_text(
                            "❌ Не удалось изменить "
                            "указанные номерки."
                        )

                        return

                    # ---------------------------------------------
                    # Сохраняем лото
                    # ---------------------------------------------

                    save_lottery(
                        lot,
                        active=True,
                    )

                    # ---------------------------------------------
                    # Обновляем табло
                    # ---------------------------------------------

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

                    response = (
                        f"✅ Лото №{lot['number']}: "
                        f"администратор изменил "
                        f"номера "
                        f"{', '.join(map(str, changed))} "
                        f"на "
                        f"{rename['name']}."
                    )

                    if not_found:

                        response += (
                            "\n\n⚠️ Не изменены: "
                            + ", ".join(
                                map(
                                    str,
                                    not_found,
                                )
                            )
                        )

                    await update.message.reply_text(
                        response
                    )

                    return

                # =================================================
                # ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ
                #
                # Здесь оставляем прежнее правило:
                # менять можно только свои номера.
                # =================================================

                matching_lots = []

                for lot in active_lotteries:

                    owned_numbers = []

                    for number in rename_numbers:

                        if number not in lot["numbers"]:
                            continue

                        meta = lot.get(
                            "reservation_meta",
                            {}
                        ).get(
                            str(number),
                            {}
                        )

                        if (
                            str(
                                meta.get(
                                    "user_id",
                                    ""
                                )
                            )
                            == str(user_id)
                        ):

                            owned_numbers.append(
                                number
                            )

                    if owned_numbers:

                        matching_lots.append(
                            (
                                lot,
                                owned_numbers,
                            )
                        )

                # ---------------------------------------------
                # Ни один указанный номер
                # не принадлежит пользователю.
                #
                # Это может быть обычная заявка:
                #
                # 3 Антон
                #
                # Поэтому если "на" нет,
                # передаём дальше в бронирование.
                # ---------------------------------------------

                if not matching_lots:

                    if " на " in (
                        f" {text.lower()} "
                    ):
                        pass

                    else:
                        return

                else:

                    # -----------------------------------------
                    # Номера находятся в нескольких лото
                    # -----------------------------------------

                    unique_lots = []

                    for lot, owned_numbers in matching_lots:

                        if lot not in unique_lots:

                            unique_lots.append(
                                lot
                            )

                    if len(unique_lots) > 1:

                        numbers = ", ".join(
                            f"№{lot['number']}"
                            for lot in unique_lots
                        )

                        await update.message.reply_text(
                            "⚠️ Указанные вами номера "
                            "есть в нескольких активных лото: "
                            + numbers
                            + ".\n"
                            "Укажите номер лото."
                        )

                        return

                    # -----------------------------------------
                    # Нашли одно лото
                    # -----------------------------------------

                    lot = unique_lots[0]

                    owned_numbers = []

                    for number in rename_numbers:

                        if number not in lot["numbers"]:
                            continue

                        meta = lot.get(
                            "reservation_meta",
                            {}
                        ).get(
                            str(number),
                            {}
                        )

                        if (
                            str(
                                meta.get(
                                    "user_id",
                                    ""
                                )
                            )
                            == str(user_id)
                        ):

                            owned_numbers.append(
                                number
                            )

                    # -----------------------------------------
                    # Переименовываем
                    # -----------------------------------------

                    for number in owned_numbers:

                        lot["owners"][number] = (
                            rename["name"]
                        )

                        meta = lot.get(
                            "reservation_meta",
                            {}
                        )

                        if str(number) in meta:

                            meta[
                                str(number)
                            ]["self"] = (
                                normalize(
                                    rename["name"]
                                )
                                ==
                                normalize(
                                    get_user_display_name(
                                        update.effective_user
                                    )
                                )
                            )

                        lot["reservation_meta"] = meta

                    save_lottery(
                        lot,
                        active=True,
                    )

                    # -----------------------------------------
                    # Обновляем табло
                    # -----------------------------------------

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

                    await update.message.reply_text(
                        f"✅ Лото №{lot['number']}: "
                        f"номера "
                        f"{', '.join(map(str, owned_numbers))} "
                        f"теперь записаны за "
                        f"{rename['name']}."
                    )

                    return

            # =================================================
            # ВСЕ СВОИ НОМЕРА
            #
            # поменяйте на Антон
            # перезапишите меня на Антон
            # мои номера на Антон
            # =================================================

            else:

                matching_lots = []

                for lot in active_lotteries:

                    for number in lot["numbers"]:

                        meta = lot.get(
                            "reservation_meta",
                            {}
                        ).get(
                            str(number),
                            {}
                        )

                        if (
                            str(
                                meta.get(
                                    "user_id",
                                    ""
                                )
                            )
                            == str(user_id)
                        ):

                            matching_lots.append(
                                lot
                            )

                            break

                if not matching_lots:

                    await update.message.reply_text(
                        "❌ У вас нет активных номерков, "
                        "записанных за вами."
                    )

                    return

                if len(matching_lots) > 1:

                    numbers = ", ".join(
                        f"№{lot['number']}"
                        for lot in matching_lots
                    )

                    await update.message.reply_text(
                        "⚠️ У вас есть номера "
                        "в нескольких активных лото: "
                        + numbers
                        + ".\n"
                        "Для изменения нескольких лото "
                        "лучше укажите конкретный номер."
                    )

                    return

                lot = matching_lots[0]

                changed = []

                for number in lot["numbers"]:

                    meta = lot.get(
                        "reservation_meta",
                        {}
                    ).get(
                        str(number),
                        {}
                    )

                    if (
                        str(
                            meta.get(
                                "user_id",
                                ""
                            )
                        )
                        == str(user_id)
                    ):

                        lot["owners"][number] = (
                            rename["name"]
                        )

                        if str(number) in meta:

                            meta[
                                str(number)
                            ]["self"] = (
                                normalize(
                                    rename["name"]
                                )
                                ==
                                normalize(
                                    get_user_display_name(
                                        update.effective_user
                                    )
                                )
                            )

                        changed.append(
                            number
                        )

                lot["reservation_meta"] = meta

                save_lottery(
                    lot,
                    active=True,
                )

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

                await update.message.reply_text(
                    f"✅ Лото №{lot['number']}: "
                    f"ваши номера "
                    f"{', '.join(map(str, changed))} "
                    f"теперь записаны за "
                    f"{rename['name']}."
                )

                return

        # ====================================================
        # БРОНИРОВАНИЕ
        # ====================================================

        active_lotteries = read_lotteries()

        active_numbers = [
            lot["number"]
            for lot in active_lotteries
        ]

        parsed = parse_reservation(
            text,
            active_lottery_numbers=active_numbers,
        )

        if not parsed:
            # Не похожее на заявку сообщение —
            # бот молчит.
            return

        lottery_number = parsed[
            "lottery_number"
        ]

        assignments = parsed[
            "assignments"
        ]

        # ----------------------------------------------------
        # Определяем лото
        # ----------------------------------------------------

        if lottery_number is None:

            if len(active_lotteries) == 0:
                return

            if len(active_lotteries) > 1:
                available = ", ".join(
                    f"№{lot['number']}"
                    for lot in active_lotteries
                )

                await update.message.reply_text(
                    "⚠️ Сейчас активно два лото: "
                    + available
                    + ".\n"
                    "Укажите номер лото, например:\n"
                    "165 4,5"
                )

                return

            lottery_number = (
                active_lotteries[0]["number"]
            )

        # ----------------------------------------------------
        # Ищем лото
        # ----------------------------------------------------

        lot = find_lottery(
            lottery_number
        )

        if not lot:
            await update.message.reply_text(
                f"❌ Лото №{lottery_number} "
                "не найдено или уже закрыто."
            )
            return

        # ----------------------------------------------------
        # Имя автора сообщения
        # ----------------------------------------------------

        telegram_name = get_user_display_name(
            update.effective_user
        )

        user_id = (
            update.effective_user.id
            if update.effective_user
            else None
        )

        if not user_id:
            return

        # ----------------------------------------------------
        # Проверяем и записываем каждый номер
        # отдельно.
        #
        # Это позволяет делать ЧАСТИЧНУЮ бронь.
        # ----------------------------------------------------

        booked = []
        occupied = []
        invalid = []

        reservation_meta = lot.get(
            "reservation_meta",
            {}
        )

        for assignment in assignments:

            number = assignment[
                "number"
            ]

            requested_name = (
                assignment["name"]
                or telegram_name
            )

            if number not in lot["numbers"]:
                invalid.append(number)
                continue

            if number in lot["owners"]:
                occupied.append({
                    "number": number,
                    "owner": lot["owners"][number],
                })
                continue

            lot["owners"][number] = (
                requested_name
            )

            is_self = (
                assignment["name"] is None
                or
                normalize(
                    requested_name
                )
                ==
                normalize(
                    telegram_name
                )
            )

            reservation_meta[
                str(number)
            ] = {
                "user_id": user_id,
                "self": is_self,
            }

            booked.append({
                "number": number,
                "name": requested_name,
            })

        lot["reservation_meta"] = (
            reservation_meta
        )

        # ----------------------------------------------------
        # Если ничего не записали — состояние
        # сохранять не нужно.
        # ----------------------------------------------------

        if not booked:

            parts = []

            if occupied:
                parts.append(
                    "❌ Уже заняты:\n"
                    +
                    "\n".join(
                        f"№{item['number']} — "
                        f"{item['owner']}"
                        for item in occupied
                    )
                )

            if invalid:
                parts.append(
                    "❌ В этом лото нет номерков: "
                    +
                    ", ".join(
                        map(str, invalid)
                    )
                )

            if parts:
                await update.message.reply_text(
                    "\n\n".join(parts)
                )

            return

        # ----------------------------------------------------
        # Проверяем, закрылось ли лото.
        # ----------------------------------------------------

        free_count = sum(
            1
            for number in lot["numbers"]
            if number not in lot["owners"]
        )

        is_closed = (
            free_count == 0
        )

        # Сохраняем состояние.
        save_lottery(
            lot,
            active=not is_closed,
        )

        # ----------------------------------------------------
        # Обновляем табло.
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Формируем ответ.
        # ----------------------------------------------------

        response = [
            f"✅ Лото №{lottery_number}"
        ]

        response.append(
            "Записано:"
        )

        response.append(
            "\n".join(
                f"№{item['number']} — "
                f"{item['name']}"
                for item in booked
            )
        )

        if occupied:
            response.append(
                "❌ Уже заняты:"
            )

            response.append(
                "\n".join(
                    f"№{item['number']} — "
                    f"{item['owner']}"
                    for item in occupied
                )
            )

        if invalid:
            response.append(
                "❌ Нет в этом лото: "
                +
                ", ".join(
                    map(str, invalid)
                )
            )

        if is_closed:
            response.append(
                "🔴 ЛОТО ЗАКРЫТО — "
                "все номерки заняты."
            )

        else:
            response.append(
                f"🟢 Свободно: {free_count}"
            )

        await update.message.reply_text(
            "\n\n".join(response)
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
    "Привет! 👋\n\n"
    "Остатки: «остатки» "
    "или «остатки лейка».\n\n"
    "Лото можно бронировать обычным сообщением:\n"
    "5\n"
    "5,6,8\n"
    "Иванов 5,6\n"
    "5 Аня, 6 Влад, 8 Петя\n\n"
    "Если активно два лото, укажите номер:\n"
    "165 5,6\n\n"
    "Можно изменить имя:\n"
    "5 на Иванович"
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

    if not lottery_topic_allowed(update):
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