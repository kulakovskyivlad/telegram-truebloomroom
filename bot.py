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
    Гибкий разбор заявок на лото.

    Поддерживает:

    5
    5 6 7
    5,6,7

    5 Иванов

    5 Аня, 6 Влад, 8 Петя

    лот 165
    5

    лот 165
    5 Аня, 6 Влад

    Также:
    165 5
    165 5,6
    165 5 Аня, 6 Влад

    Если имя не указано:
    name = None

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
    # Ищем "лот 165" или "лот №165"
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
    # Убираем слово "номер"
    #
    # номер 5
    # номер 5,6,7
    # ========================================================

    text = re.sub(
        r"^\s*номер\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if not text:
        return None

    # ========================================================
    # Чисто цифровой вариант:
    #
    # 5
    # 5 6 7
    # 5,6,7
    #
    # Все номера записываются на автора сообщения.
    # ========================================================

    if re.fullmatch(
        r"\d+(?:[\s,\/]+\d+)*",
        text,
    ):
        numbers = [
            int(value)
            for value in re.findall(
                r"\d+",
                text,
            )
        ]

        # Если активно два лото и человек написал:
        #
        # 165 5
        #
        # где 165 — номер лото, а 5 — номерок.
        if (
            lottery_number is None
            and len(active_lottery_numbers) > 1
            and len(numbers) > 1
            and numbers[0]
            in active_lottery_numbers
        ):
            lottery_number = numbers[0]
            numbers = numbers[1:]

        if not numbers:
            return None

        # Один номер нельзя указать дважды.
        if len(numbers) != len(set(numbers)):
            return None

        return {
            "lottery_number": lottery_number,
            "assignments": [
                {
                    "number": number,
                    "name": None,
                }
                for number in numbers
            ],
        }

    # ========================================================
    # Разбираем группы номеров с именами.
    #
    # 5 Аня
    # 6 Влад
    #
    # 5,6 Влад
    #
    # 5 Аня, 6 Влад, 8 Петя
    # ========================================================

    # Сначала разделяем по запятым только там,
    # где после запятой начинается новый номер.
    #
    # Поэтому:
    #
    # 5,6 Влад
    #
    # превращается в одну группу:
    # "5,6 Влад"
    #
    # а:
    #
    # 5 Аня, 6 Влад
    #
    # превращается в две группы.

    # ========================================================
    # Разбираем номера + имя.
    #
    # Поддерживаем:
    #
    # 1 3 Влад
    # 1,3 Влад
    # 1 2 3 Влад
    # 1, 2, 3 Влад
    #
    # А также разные имена:
    #
    # 1 Аня, 2 Влад, 3 Петя
    # ========================================================

    assignments = []

    # --------------------------------------------------------
    # Сначала пробуем разобрать вариант,
    # где одна заявка содержит несколько номеров
    # и одно имя:
    #
    # 1 3 Влад
    # 1,3 Влад
    # 1 2 3 Влад
    # --------------------------------------------------------

    single_name_match = re.fullmatch(
        r"((?:\d+\s*[,/ ]\s*)*\d+)"
        r"\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if single_name_match:

        numbers_text = (
            single_name_match.group(1)
        )

        name = (
            single_name_match.group(2)
            .strip()
        )

        numbers = [
            int(value)
            for value in re.findall(
                r"\d+",
                numbers_text,
            )
        ]

        if not numbers or not name:
            return None

        for number in numbers:
            assignments.append({
                "number": number,
                "name": name,
            })

    else:

        # ----------------------------------------------------
        # Если всё сообщение не является одной группой,
        # разбираем отдельные заявки:
        #
        # 1 Аня, 2 Влад, 3 Петя
        # ----------------------------------------------------

        parts = re.split(
            r",\s*(?=\d+\s)",
            text,
        )

        parts = [
            part.strip()
            for part in parts
            if part.strip()
        ]

        for part in parts:

            # ----------------------------------------------
            # Несколько номеров + имя:
            #
            # 1 2 Влад
            # 1,2 Влад
            # ----------------------------------------------

            match = re.fullmatch(
                r"((?:\d+\s*[,/ ]\s*)*\d+)"
                r"\s+(.+)",
                part,
                flags=re.IGNORECASE,
            )

            if match:

                numbers_text = (
                    match.group(1)
                )

                name = (
                    match.group(2)
                    .strip()
                )

                numbers = [
                    int(value)
                    for value in re.findall(
                        r"\d+",
                        numbers_text,
                    )
                ]

                if not numbers or not name:
                    return None

                for number in numbers:
                    assignments.append({
                        "number": number,
                        "name": name,
                    })

                continue

            # ----------------------------------------------
            # Просто номер:
            #
            # 5
            # ----------------------------------------------

            if re.fullmatch(
                r"\d+",
                part,
            ):
                assignments.append({
                    "number": int(part),
                    "name": None,
                })

                continue

            return None

    if not assignments:
        return None

    # ========================================================
    # Если два лото и написано:
    #
    # 165 5 Аня
    #
    # то первое число считаем номером лото.
    # ========================================================

    if (
        lottery_number is None
        and len(active_lottery_numbers) > 1
        and assignments
        and assignments[0]["number"]
        in active_lottery_numbers
        and len(assignments) > 1
    ):
        lottery_number = assignments[0]["number"]
        assignments = assignments[1:]

    if not assignments:
        return None

    # ========================================================
    # Проверяем, нет ли повторяющихся номерков
    # в одной заявке.
    # ========================================================

    numbers = [
        item["number"]
        for item in assignments
    ]

    if len(numbers) != len(set(numbers)):
        return None

    return {
        "lottery_number": lottery_number,
        "assignments": assignments,
    }	
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

    Сохраняется после перезапуска Render.

    reservation_meta:
    {
        "5": {
            "user_id": 123456,
            "self": true
        },
        "6": {
            "user_id": 123456,
            "self": false
        }
    }
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

            # Новая информация о покупателях.
            # Для старых записей её может не быть.
            reservation_meta = {}

            if "reservation_meta_json" in index:
                meta_index = index[
                    "reservation_meta_json"
                ]

                if meta_index < len(row):
                    raw_meta = row[meta_index].strip()

                    if raw_meta:
                        try:
                            reservation_meta = json.loads(
                                raw_meta
                            )
                        except Exception:
                            reservation_meta = {}

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
                "reservation_meta": reservation_meta,
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
    Понимает:

    5 на Иванович
    №5 на Иванович

    перезапишите меня на Иванович
    поменяйте меня на Иванович
    мои номера на Иванович
    """

    text = str(text or "").strip()

    if not text:
        return None

    # --------------------------------------------------------
    # Конкретный номер:
    #
    # 5 на Иванович
    # №5 на Иванович
    # --------------------------------------------------------

    match = re.fullmatch(
        r"(?:№\s*)?(\d{1,3})\s+на\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return {
            "number": int(
                match.group(1)
            ),
            "name": clean_name(
                match.group(2)
            ),
        }

    # --------------------------------------------------------
    # Все свои номера:
    #
    # перезапишите меня на Иванович
    # поменяйте меня на Иванович
    # мои номера на Иванович
    # --------------------------------------------------------

    match = re.fullmatch(
        r"(?:перезапишите|поменяйте)"
        r"(?:\s+меня)?"
        r"\s+на\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return {
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
            "number": None,
            "name": clean_name(
                match.group(1)
            ),
        }

    return None

async def handle_lottery_reservation(update):
    if (
        not update.message
        or not lottery_chat_allowed(
            update.effective_chat.id
        )
    ):
        return

    # Только групповые чаты.
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    async with LOTTERY_LOCK:

        # ====================================================
        # ПЕРЕИМЕНОВАНИЕ
        # ====================================================

        rename = parse_rename_command(
            text
        )

        if rename:
            active_lotteries = read_lotteries()

            user_id = (
                update.effective_user.id
                if update.effective_user
                else None
            )

            if not user_id:
                return

            # ------------------------------------------------
            # Конкретный номер
            # ------------------------------------------------

            if rename["number"] is not None:
                number = rename["number"]

                matching_lots = [
                    lot
                    for lot in active_lotteries
                    if number in lot["numbers"]
                    and str(
                        lot.get(
                            "reservation_meta",
                            {}
                        ).get(
                            str(number),
                            {}
                        ).get(
                            "user_id",
                            ""
                        )
                    ) == str(user_id)
                ]

                if len(matching_lots) == 0:
                    await update.message.reply_text(
                        f"❌ Номер №{number} "
                        "не найден среди ваших активных заявок."
                    )
                    return

                if len(matching_lots) > 1:
                    numbers = ", ".join(
                        f"№{lot['number']}"
                        for lot in matching_lots
                    )

                    await update.message.reply_text(
                        "⚠️ Этот номер есть в нескольких активных лото: "
                        + numbers
                        + ".\n"
                        "Укажите номер лото."
                    )
                    return

                lot = matching_lots[0]

                lot["owners"][number] = rename["name"]

                meta = lot.get(
                    "reservation_meta",
                    {}
                )

                if str(number) in meta:
                    meta[str(number)]["self"] = (
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
                    f"№{number} теперь записан за "
                    f"{rename['name']}."
                )

                return

            # ------------------------------------------------
            # "мои номера на Иванович"
            # ------------------------------------------------

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
                        ) == str(user_id)
                        and meta.get(
                            "self",
                            False,
                        )
                    ):
                        matching_lots.append(
                            lot
                        )
                        break

            if not matching_lots:
                await update.message.reply_text(
                    "❌ У вас нет активных номерков, "
                    "записанных на ваше имя."
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
                    ) == str(user_id)
                    and meta.get(
                        "self",
                        False,
                    )
                ):
                    lot["owners"][number] = (
                        rename["name"]
                    )

                    changed.append(number)

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