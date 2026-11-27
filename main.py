"""Telegram-бот: склад компьютерных комплектующих.

Версия 0.7. Список изменений по версиям: CHANGELOG.md

Запуск:
    1. Один раз впиши BOT_TOKEN и OWNER_ID в файл .env рядом с main.py.
    2. python main.py
Переменные, заданные через set BOT_TOKEN=..., важнее файла .env.
"""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------
def load_env(path):
    """Читает строки вида КЛЮЧ=значение из файла .env рядом с main.py.
    Переменные, уже заданные в окружении (например через set), важнее файла."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env(Path(__file__).with_name(".env"))

TOKEN = os.getenv("BOT_TOKEN")
# Если задать OWNER_ID, бот будет отвечать только тебе (иначе любой, кто найдёт бота,
# сможет менять твой склад). Свой id можно узнать у бота @userinfobot.
# Несколько человек с общей базой: id через запятую, например 111111111,222222222.
# Первый в списке — владелец: ему приходят ежедневные копии, только он может /restore.
_owners = [x for x in re.split(r"[,;\s]+", os.getenv("OWNER_ID") or "") if x]
if not all(x.isdigit() for x in _owners):
    raise SystemExit("OWNER_ID: только цифры, несколько id — через запятую, например 111111111,222222222.")
ALLOWED_IDS = [int(x) for x in _owners]
OWNER_ID = ALLOWED_IDS[0] if ALLOWED_IDS else 0

# База данных лежит рядом с main.py. На сервере (Railway) задай DATA_DIR=/data —
# папку постоянного диска (Volume), иначе база пропадёт при каждом обновлении.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
DB_PATH = DATA_DIR / "warehouse.db"

# Автобэкап: раз в день бот присылает владельцу файл базы в это время (по TZ_HOURS)
BACKUP_HOUR = 23

VERSION = "0.7.1"

# Часовой пояс для дат продаж и статистики: сдвиг от UTC в часах.
# Тбилиси = 4. Нужен, чтобы на сервере (где часы обычно по UTC) даты не уезжали.
TZ_HOURS = 4

# Валюта для цен. Например "$", "₾" или "" (тогда просто число).
CURRENCY = ""

# Порт-«замок»: не даёт запустить две копии бота одновременно
LOCK_PORT = 47653

# Списки вариантов. Чтобы добавить сокет или тип памяти, просто допиши строку.
SOCKETS = [
    "LGA775", "LGA1156", "LGA1155", "LGA1150", "LGA1151", "LGA1200",
    "LGA1700", "LGA1851", "LGA2011", "LGA2011-3", "LGA2066",
    "AM3", "AM3+", "AM4", "AM5",
]
RAM_TYPES = ["DDR2", "DDR3", "DDR4", "DDR5"]
COUNTS = ["1", "2", "3", "4", "5", "6", "8", "10"]
CORES = ["1", "2", "3", "4", "6", "8", "10", "12", "14", "16", "20", "24", "32"]
# Допустимые значения потоков: из них бот выбирает только подходящие под число ядер
THREAD_VALUES = [1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64]


# ---------------------------------------------------------------------------
# ОПИСАНИЕ КАТЕГОРИЙ И ИХ ХАРАКТЕРИСТИК
# Чтобы добавить новую характеристику, достаточно дописать field(...) в нужную категорию.
# kind: "text" (вводится вручную), "number" (дробное число), "int" (целое),
#       "choice" (выбор кнопками, внизу всегда есть "Другое").
# numeric=True у choice: варианты и «Другое» это числа.
# minimum / maximum: допустимые границы для чисел.
# ---------------------------------------------------------------------------
def field(key, label, kind="text", options=None, unit="", optional=False, hint="",
          numeric=False, minimum=None, maximum=None, integer=False,
          options_fn=None, check=None, hint_fn=None, then=None,
          when=None, unit_fn=None, default=None, hidden=False, reask=False, allow_other=True):
    """options_fn(values) -> список вариантов, зависящий от уже введённого.
    check(value, values) -> текст ошибки или None. then: зависимые поля,
    которые нужно перепроверить, если это поле изменили.
    when(values) -> спрашивать ли это поле вообще (например, тип подсветки — только если она есть).
    unit_fn(values) -> единица, зависящая от других полей (Вт или А у линии 12V).
    default: значение (или функция от values), если поле пропустили.
    hidden: не показывать строкой в карточке. reask: спросить заново, если изменили поле,
    от которого это зависит. allow_other=False: у выбора нет кнопки «Другое»."""
    return {
        "key": key, "label": label, "kind": kind, "options": options or [],
        "unit": unit, "optional": optional, "hint": hint, "numeric": numeric,
        "min": minimum, "max": maximum, "integer": integer,
        "options_fn": options_fn, "check": check, "hint_fn": hint_fn, "then": then or [],
        "when": when, "unit_fn": unit_fn, "default": default, "hidden": hidden,
        "reask": reask, "allow_other": allow_other,
    }


def price_field(optional=False):
    return field("price", "Цена", "number", unit=CURRENCY, minimum=0, optional=optional,
                 hint="Сколько стоила эта позиция целиком, все штуки вместе (если ничего, напиши 0)")


def count_field(label="Количество", options=None, optional=False):
    return field("count", label, "choice", options or COUNTS, unit="шт", numeric=True, integer=True,
                 optional=optional, default=1 if optional else None)


def v12_unit(values):
    return "Вт" if str(values.get("v12_unit", "")).startswith("В ваттах") else "А"


def threads_for(values):
    """Потоков не меньше, чем ядер, и не больше, чем вдвое."""
    cores = values.get("cores")
    if not cores:
        return [str(t) for t in THREAD_VALUES]
    cands = sorted(set(THREAD_VALUES) | {cores, cores * 2})
    return [str(t) for t in cands if cores <= t <= cores * 2]


def threads_check(value, values):
    cores = values.get("cores")
    if cores and not (cores <= value <= cores * 2):
        return f"При {cores} ядрах потоков может быть от {cores} до {cores * 2}."
    return None


CATEGORIES = {
    "mb": {
        "icon": "🔌",
        "title": "Материнская плата",
        "plural": "Материнские платы",
        "add": "Добавить материнскую плату",
        "list_hint": ["socket", "ram_type"],
        "fields": [
            field("name", "Название", hint="Например: ASUS Prime B450M-A"),
            field("socket", "Сокет", "choice", SOCKETS),
            field("ram_slots", "Слотов ОЗУ", "choice", ["1", "2", "4", "6", "8"], unit="шт", numeric=True, integer=True),
            field("ram_type", "Тип ОЗУ", "choice", RAM_TYPES),
            field("m2", "Слотов M.2", "choice", ["0", "1", "2", "3", "4"], unit="шт",
                  numeric=True, integer=True, optional=True,
                  hint="Разъёмы для NVMe-накопителей. Если не знаешь, пропусти"),
            count_field(),
            price_field(),
            field("notes", "Особенности", optional=True,
                  hint="Любые заметки о работе этой платы"),
        ],
    },
    "cpu": {
        "icon": "🧠",
        "title": "Процессор",
        "plural": "Процессоры",
        "add": "Добавить процессор",
        "list_hint": ["socket", "freq"],
        "fields": [
            field("name", "Название", hint="Например: Core i5-4590 или Ryzen 5 3600"),
            field("line", "Линейка", optional=True, hint="Например: Core i5, Ryzen 5, Xeon"),
            field("socket", "Сокет", "choice", SOCKETS),
            field("freq", "Частота", "number", unit="ГГц", hint="Например: 3.4"),
            field("cores", "Ядер", "choice", CORES, numeric=True, integer=True, then=["threads"]),
            field("threads", "Потоков", "choice", numeric=True, integer=True,
                  options_fn=threads_for, check=threads_check,
                  hint_fn=lambda v: f"Ядер: {v['cores']}. Показаны только подходящие варианты." if v.get("cores") else ""),
            count_field(),
            price_field(),
        ],
    },
    "ram": {
        "icon": "💾",
        "title": "Оперативная память",
        "plural": "Оперативная память",
        "add": "Добавить оперативную память",
        "list_hint": ["ram_type", "size"],
        "fields": [
            field("name", "Название", hint="Например: Kingston Fury Beast"),
            field("ram_type", "Тип", "choice", RAM_TYPES),
            field("count", "Количество планок", "choice", ["1", "2", "3", "4", "6", "8"], unit="шт", numeric=True, integer=True),
            field("size", "Объём одной планки", "choice", ["1", "2", "4", "8", "16", "32", "64"], unit="ГБ", numeric=True),
            field("freq", "Частота", "int", unit="МГц", hint="Например: 3200"),
            field("kind", "Назначение", "choice", ["Пользовательская", "Серверная"]),
            price_field(),
        ],
    },
    "ssd": {
        "icon": "⚡",
        "parent": "storage",
        "title": "SSD",
        "plural": "SSD-накопители",
        "add": "Добавить SSD",
        "list_hint": ["size", "interface"],
        "fields": [
            field("name", "Название", hint="Например: Kingston A400"),
            field("size", "Объём", "choice",
                  ["60", "120", "128", "240", "256", "480", "500", "512", "960", "1000", "2000"],
                  unit="ГБ", numeric=True),
            field("interface", "Подключение", "choice", ["SATA", "NVMe"]),
            field("count", "Количество", "choice", COUNTS, unit="шт", numeric=True, integer=True),
            field("health", "Состояние (здоровье)", "number", unit="%", optional=True,
                  minimum=0, maximum=100, hint="Число от 0 до 100, например 87"),
            price_field(),
            field("notes", "Особенности", optional=True, hint="Например: есть битые сектора"),
        ],
    },
    "hdd": {
        "icon": "💿",
        "parent": "storage",
        "title": "HDD",
        "plural": "HDD-накопители",
        "add": "Добавить HDD",
        "list_hint": ["size", "state"],
        "fields": [
            field("name", "Название", hint="Например: WD Blue 1TB"),
            field("size", "Объём", "choice",
                  ["80", "160", "250", "320", "500", "640", "750", "1000", "2000", "3000", "4000"],
                  unit="ГБ", numeric=True),
            field("count", "Количество", "choice", COUNTS, unit="шт", numeric=True, integer=True),
            field("state", "Состояние (как в CrystalDiskInfo)", "choice",
                  ["Хорошо (Good)", "Тревога (Caution)", "Плохо (Bad)", "Неизвестно (Unknown)"]),
            field("health", "Здоровье, %", "number", unit="%", optional=True,
                  minimum=0, maximum=100,
                  hint="Если CrystalDiskInfo показывает процент, например 100"),
            price_field(),
            field("notes", "Особенности", optional=True, hint="Например: щелчки при работе"),
        ],
    },
    "case": {
        "icon": "🗃",
        "title": "Корпус",
        "plural": "Корпуса",
        "add": "Добавить корпус",
        "list_hint": [],
        "fields": [
            field("name", "Название", hint="Например: игровой, со стеклом, Zalman S2 (тип пиши прямо в названии)"),
            count_field(),
            price_field(),
            field("notes", "Особенности", optional=True, hint="Любые заметки"),
        ],
    },
    "fan": {
        "icon": "🌀",
        "title": "Вентилятор",
        "plural": "Вентиляторы",
        "add": "Добавить вентилятор",
        "list_hint": ["size", "count"],
        "fields": [
            field("name", "Название", hint="Например: Deepcool RF120"),
            field("size", "Размер", "choice", ["40", "60", "80", "90", "92", "120", "140", "200"],
                  unit="мм", numeric=True, integer=True),
            field("count", "Количество", "choice", COUNTS, unit="шт", numeric=True, integer=True),
            price_field(),
            field("notes", "Особенности", optional=True, hint="Любые заметки"),
        ],
    },
    "gpu": {
        "icon": "🎮",
        "title": "Видеокарта",
        "plural": "Видеокарты",
        "add": "Добавить видеокарту",
        "list_hint": ["vram", "vram_type"],
        "fields": [
            field("name", "Название", hint="Например: GTX 1660 Super"),
            field("vram", "Объём видеопамяти", "choice",
                  ["1", "2", "3", "4", "6", "8", "10", "11", "12", "16", "24"],
                  unit="ГБ", numeric=True),
            field("vram_type", "Тип видеопамяти", "choice",
                  ["DDR2", "DDR3", "DDR4", "GDDR3", "GDDR5", "GDDR5X", "GDDR6", "GDDR6X", "GDDR7", "HBM2"]),
            count_field(),
            price_field(),
            field("notes", "Особенности работы", optional=True,
                  hint="Например: перегрев, нужно заменить термопасту"),
        ],
    },
    "psu": {
        "icon": "🔋",
        "title": "Блок питания",
        "plural": "Блоки питания",
        "add": "Добавить блок питания",
        "list_hint": ["power"],
        "fields": [
            field("name", "Название", hint="Например: Chieftec GPE-500S"),
            field("power", "Мощность", "choice",
                  ["300", "350", "400", "450", "500", "550", "600", "650", "700", "750", "850", "1000", "1200"],
                  unit="Вт", numeric=True, integer=True),
            field("v12_unit", "Линия 12V: как указать", "choice", ["В ваттах (Вт)", "В амперах (А)"],
                  optional=True, hidden=True, allow_other=False, then=["v12"],
                  hint="Смотри на наклейке блока строку +12V. Можно пропустить"),
            field("v12", "Линия 12V", "number", unit_fn=v12_unit, reask=True,
                  when=lambda v: bool(v.get("v12_unit")),
                  hint_fn=lambda v: "Например: 480" if v12_unit(v) == "Вт" else "Например: 40"),
            count_field(),
            price_field(),
            field("notes", "Особенности", optional=True, hint="Любые заметки"),
        ],
    },
    "cooler": {
        "icon": "❄️",
        "title": "Кулер для процессора",
        "short": "Кулер",
        "plural": "Кулеры для процессора",
        "add": "Добавить кулер",
        "list_hint": ["ctype", "lighting"],
        "fields": [
            field("name", "Название", optional=True,
                  default=lambda v: f"{v.get('ctype', '')} кулер".strip().capitalize(),
                  hint="Например: Deepcool AK400. Можно пропустить: тогда он назовётся по типу, "
                       "например «Башенный кулер»"),
            field("ctype", "Тип", "choice", ["Башенный", "Боксовый"], allow_other=False),
            field("heatpipes", "Медных трубок", "choice", ["0", "1", "2", "3", "4", "5", "6", "8"],
                  unit="шт", numeric=True, integer=True, optional=True),
            field("lighting", "Подсветка", "choice", ["Есть", "Нет"], optional=True,
                  allow_other=False, then=["light_type"]),
            field("light_type", "Тип подсветки", "choice", ["Регулируемая", "Автоматическая"],
                  optional=True, allow_other=False, reask=True,
                  when=lambda v: v.get("lighting") == "Есть"),
            count_field(optional=True),
            price_field(optional=True),
            field("notes", "Особенности", optional=True, hint="Любые заметки"),
        ],
    },
}

# Группы: категория с "parent" показывается не в общем меню, а внутри группы
GROUPS = {
    "storage": {"icon": "🗄", "title": "Накопители", "items": ["ssd", "hdd"]},
}

# Порядок кнопок в меню комплектующих ("group:..." открывает подменю)
MENU = ["mb", "cpu", "cooler", "ram", "group:storage", "gpu", "psu", "case", "fan"]

# Категории, у которых в карточке считается общий объём (количество × объём)
TOTAL_CATS = ("ram", "ssd", "hdd")

# Готовые пункты «Прочих расходов» в сборке: суммы по одному пункту складываются.
# Пункт «Другое» (своё название) добавляется отдельной строкой каждый раз.
# Кулер и блок питания с 0.7 — комплектующие, а не расходы.
EXPENSE_PRESETS = ["Провода"]

# Сборки: порядок деталей. Этих категорий в сборке только по одной штуке.
BUILD_ORDER = ["mb", "cpu", "cooler", "ram", "ssd", "hdd", "gpu", "psu", "case", "fan"]
SINGLE_SLOTS = ("mb", "cpu", "cooler", "psu", "case")


def short_title(cat_key):
    cat = CATEGORIES[cat_key]
    return cat.get("short", cat["title"])


# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (SQLite). Одна таблица: у каждой детали есть id, категория
# и JSON с её характеристиками. Новые поля можно добавлять без переделки базы.
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


PART_COLS = "id, category, data, build_id, sold_price, sold_at, added_at"


def init_db():
    with closing(db()) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS parts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category TEXT NOT NULL, "
            "data TEXT NOT NULL)"
        )
        # build_id: если не пусто, деталь лежит в сборке и в складе её не видно
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(parts)")]
        if "build_id" not in cols:
            conn.execute("ALTER TABLE parts ADD COLUMN build_id INTEGER")
        # sold_price: если не пусто, деталь продана и лежит только в истории
        if "sold_price" not in cols:
            conn.execute("ALTER TABLE parts ADD COLUMN sold_price REAL")
        if "sold_at" not in cols:
            conn.execute("ALTER TABLE parts ADD COLUMN sold_at TEXT")
        # added_at: дата поступления на склад "YYYY-MM-DD" (с 0.7; у старых деталей пусто)
        if "added_at" not in cols:
            conn.execute("ALTER TABLE parts ADD COLUMN added_at TEXT")
        # С 0.7 количество есть у всех категорий: старым записям без него ставим 1
        for row in conn.execute("SELECT id, category, data FROM parts").fetchall():
            cat = CATEGORIES.get(row["category"])
            if not cat or not any(f["key"] == "count" for f in cat["fields"]):
                continue
            data = json.loads(row["data"])
            if not isinstance(data.get("count"), (int, float)):
                data["count"] = 1
                conn.execute("UPDATE parts SET data = ? WHERE id = ?",
                             (json.dumps(data, ensure_ascii=False), row["id"]))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS builds ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'draft')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS expenses ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "build_id INTEGER NOT NULL, "
            "label TEXT NOT NULL, "
            "amount REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL)"
        )
        bcols = [r["name"] for r in conn.execute("PRAGMA table_info(builds)")]
        for col, kind in (("sold_price", "REAL"), ("sold_at", "TEXT"), ("cost", "REAL")):
            if col not in bcols:
                conn.execute(f"ALTER TABLE builds ADD COLUMN {col} {kind}")
        # С 0.7 статусов «готова/черновик» нет: все непроданные сборки просто «open»
        conn.execute("UPDATE builds SET status = 'open' WHERE status != 'sold'")


def add_part(category, data):
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO parts (category, data, added_at) VALUES (?, ?, ?)",
            (category, json.dumps(data, ensure_ascii=False), now_dt().date().isoformat()),
        )
        return cur.lastrowid


def set_added_at(part_id, day):
    """day: 'YYYY-MM-DD' или None (не указана)."""
    with closing(db()) as conn, conn:
        conn.execute("UPDATE parts SET added_at = ? WHERE id = ?", (day, part_id))


def _row_to_part(row):
    return {"id": row["id"], "cat": row["category"], "data": json.loads(row["data"]),
            "build_id": row["build_id"], "sold_price": row["sold_price"], "sold_at": row["sold_at"],
            "added_at": row["added_at"]}


def get_part(part_id):
    with closing(db()) as conn:
        row = conn.execute(f"SELECT {PART_COLS} FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _row_to_part(row) if row else None


def list_parts(category, free_only=True):
    sql = f"SELECT {PART_COLS} FROM parts WHERE category = ?"
    if free_only:
        sql += " AND build_id IS NULL AND sold_price IS NULL"
    with closing(db()) as conn:
        rows = conn.execute(sql, (category,)).fetchall()
    parts = [_row_to_part(r) for r in rows]
    parts.sort(key=lambda p: str(p["data"].get("name", "")).lower())
    return parts


def count_in_builds(category):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM parts WHERE category = ? AND build_id IS NOT NULL "
            "AND (SELECT status FROM builds WHERE builds.id = parts.build_id) != 'sold'", (category,)
        ).fetchone()[0]


def update_part(part_id, data):
    with closing(db()) as conn, conn:
        conn.execute(
            "UPDATE parts SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), part_id),
        )


def delete_part(part_id):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))


# --- сборки ---
def create_build():
    with closing(db()) as conn, conn:
        cur = conn.execute("INSERT INTO builds (name, status) VALUES ('', 'open')")
        build_id = cur.lastrowid
        conn.execute("UPDATE builds SET name = ? WHERE id = ?", (f"Сборка №{build_id}", build_id))
        return build_id


BUILD_COLS = "id, name, status, sold_price, sold_at, cost"


def get_build(build_id):
    with closing(db()) as conn:
        row = conn.execute(f"SELECT {BUILD_COLS} FROM builds WHERE id = ?", (build_id,)).fetchone()
    return dict(row) if row else None


def list_builds():
    """Сборки в работе (проданные живут в истории)."""
    with closing(db()) as conn:
        rows = conn.execute(
            f"SELECT {BUILD_COLS} FROM builds WHERE status != 'sold' ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def list_sold_builds():
    with closing(db()) as conn:
        rows = conn.execute(
            f"SELECT {BUILD_COLS} FROM builds WHERE status = 'sold' ORDER BY sold_at DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def list_sold_parts():
    """Детали, проданные по отдельности (детали из проданных сборок сюда не попадают)."""
    with closing(db()) as conn:
        rows = conn.execute(
            f"SELECT {PART_COLS} FROM parts WHERE sold_price IS NOT NULL AND build_id IS NULL "
            "ORDER BY sold_at DESC, id DESC"
        ).fetchall()
    return [_row_to_part(r) for r in rows]


def now_dt():
    return datetime.now(timezone(timedelta(hours=TZ_HOURS)))


def now_text():
    return now_dt().strftime("%Y-%m-%d %H:%M")


def date_text(stamp):
    """'2026-11-21 11:30' -> '21.11.2026'"""
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "—"


def sell_part(part_id, price):
    """Продаёт свободную деталь. False, если она в сборке, уже продана или удалена."""
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "UPDATE parts SET sold_price = ?, sold_at = ? "
            "WHERE id = ? AND build_id IS NULL AND sold_price IS NULL",
            (price, now_text(), part_id),
        )
        return cur.rowcount == 1


def unsell_part(part_id):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE parts SET sold_price = NULL, sold_at = NULL WHERE id = ?", (part_id,))


def sell_build(build_id, price, cost):
    """Продаёт сборку и запоминает её себестоимость на момент продажи."""
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "UPDATE builds SET status = 'sold', sold_price = ?, sold_at = ?, cost = ? "
            "WHERE id = ? AND status != 'sold'",
            (price, now_text(), cost, build_id),
        )
        return cur.rowcount == 1


def unsell_build(build_id):
    with closing(db()) as conn, conn:
        conn.execute(
            "UPDATE builds SET status = 'open', sold_price = NULL, sold_at = NULL, cost = NULL "
            "WHERE id = ?", (build_id,)
        )


def delete_sold_build(build_id):
    """Стирает проданную сборку из истории вместе с её деталями и расходами."""
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM parts WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM expenses WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))


def rename_build(build_id, name):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE builds SET name = ? WHERE id = ?", (name, build_id))


def build_parts(build_id):
    with closing(db()) as conn:
        rows = conn.execute(f"SELECT {PART_COLS} FROM parts WHERE build_id = ?", (build_id,)).fetchall()
    parts = [_row_to_part(r) for r in rows]
    order = {c: i for i, c in enumerate(BUILD_ORDER)}
    parts.sort(key=lambda p: (order.get(p["cat"], 99), str(p["data"].get("name", "")).lower()))
    return parts


def assign_part(part_id, build_id):
    """Кладёт свободную деталь в сборку. False, если деталь уже занята или удалена."""
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "UPDATE parts SET build_id = ? WHERE id = ? AND build_id IS NULL AND sold_price IS NULL",
            (build_id, part_id)
        )
        return cur.rowcount == 1


def identity(data):
    """Что делает две записи «одной и той же деталью»: всё, кроме количества и цены.
    Дата поступления тоже должна совпадать (это проверяется отдельно, она в своей колонке)."""
    return {k: v for k, v in data.items() if k not in ("count", "price")}


def release_part(part_id):
    """Возвращает деталь на склад. Если на складе уже есть такая же (и с той же датой
    поступления), они сливаются в одну запись (количество и цена складываются).
    Возвращает id записи, где теперь лежит деталь."""
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT id, category, data, added_at FROM parts WHERE id = ?",
                           (part_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE parts SET build_id = NULL WHERE id = ?", (part_id,))
        data = json.loads(row["data"])
        if not isinstance(data.get("count"), (int, float)):
            return part_id
        ident = identity(data)
        others = conn.execute(
            "SELECT id, data FROM parts WHERE category = ? AND id != ? "
            "AND build_id IS NULL AND sold_price IS NULL AND added_at IS ?",
            (row["category"], part_id, row["added_at"]),
        ).fetchall()
        for other in others:
            odata = json.loads(other["data"])
            if not isinstance(odata.get("count"), (int, float)) or identity(odata) != ident:
                continue
            odata["count"] = int(odata["count"]) + int(data["count"])
            p1, p2 = odata.get("price"), data.get("price")
            if p1 is not None or p2 is not None:
                odata["price"] = round((p1 or 0) + (p2 or 0), 2)
            conn.execute("UPDATE parts SET data = ? WHERE id = ?",
                         (json.dumps(odata, ensure_ascii=False), other["id"]))
            conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))
            return other["id"]
        return part_id


def merge_in_build(build_id, part_id):
    """Одинаковые детали внутри одной сборки сливаются в одну строку (×N)."""
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT id, category, data, added_at FROM parts WHERE id = ?",
                           (part_id,)).fetchone()
        if row is None:
            return part_id
        data = json.loads(row["data"])
        if not isinstance(data.get("count"), (int, float)):
            return part_id
        ident = identity(data)
        others = conn.execute(
            "SELECT id, data FROM parts WHERE category = ? AND build_id = ? AND id != ? AND added_at IS ?",
            (row["category"], build_id, part_id, row["added_at"]),
        ).fetchall()
        for other in others:
            odata = json.loads(other["data"])
            if not isinstance(odata.get("count"), (int, float)) or identity(odata) != ident:
                continue
            odata["count"] = int(odata["count"]) + int(data["count"])
            p1, p2 = odata.get("price"), data.get("price")
            if p1 is not None or p2 is not None:
                odata["price"] = round((p1 or 0) + (p2 or 0), 2)
            conn.execute("UPDATE parts SET data = ? WHERE id = ?",
                         (json.dumps(odata, ensure_ascii=False), other["id"]))
            conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))
            return other["id"]
        return part_id


def release_motherboard(build_id, mb_id):
    """Убирает плату из сборки. Процессор и память уходят вместе с ней."""
    for p in build_parts(build_id):
        if p["cat"] in ("cpu", "ram"):
            release_part(p["id"])
    release_part(mb_id)


def split_part(part_id, take):
    """Отделяет от записи `take` штук в новую запись (цена делится по штукам).
    Возвращает id новой записи или None."""
    with closing(db()) as conn, conn:
        row = conn.execute(
            "SELECT category, data, added_at FROM parts WHERE id = ? AND build_id IS NULL AND sold_price IS NULL",
            (part_id,),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["data"])
        count = data.get("count")
        if not isinstance(count, (int, float)) or not (0 < take < count):
            return None
        count, take = int(count), int(take)
        new = dict(data)
        new["count"] = take
        price = data.get("price")
        if price is not None:
            new_price = round(price * take / count, 2)
            new["price"] = new_price
            data["price"] = round(price - new_price, 2)
        data["count"] = count - take
        conn.execute("UPDATE parts SET data = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), part_id))
        cur = conn.execute("INSERT INTO parts (category, data, added_at) VALUES (?, ?, ?)",
                           (row["category"], json.dumps(new, ensure_ascii=False), row["added_at"]))
        return cur.lastrowid


def delete_build(build_id):
    """Удаляет сборку, а все её детали возвращаются на склад (одинаковые сливаются)."""
    for p in build_parts(build_id):
        release_part(p["id"])
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM expenses WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))


# --- прочие расходы сборки и настройки ---
def list_expenses(build_id):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, build_id, label, amount FROM expenses WHERE build_id = ? ORDER BY id",
            (build_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_expense(expense_id):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id, build_id, label, amount FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
    return dict(row) if row else None


def add_expense(build_id, label, amount, accumulate):
    """accumulate=True: если пункт с таким названием уже есть, сумма прибавляется к нему."""
    with closing(db()) as conn, conn:
        if accumulate:
            row = conn.execute(
                "SELECT id, amount FROM expenses WHERE build_id = ? AND label = ?", (build_id, label)
            ).fetchone()
            if row:
                conn.execute("UPDATE expenses SET amount = ? WHERE id = ?",
                             (round(row["amount"] + amount, 2), row["id"]))
                return row["id"]
        cur = conn.execute("INSERT INTO expenses (build_id, label, amount) VALUES (?, ?, ?)",
                           (build_id, label, round(amount, 2)))
        return cur.lastrowid


def set_expense_amount(expense_id, amount):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE expenses SET amount = ? WHERE id = ?", (round(amount, 2), expense_id))


def delete_expense(expense_id):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))


def get_setting_number(key, default=0.0):
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return float(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def get_setting_text(key):
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value))
        )


def delete_setting(key):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --- иконки категорий: свои эмодзи хранятся в settings как icon:<категория> ---
_ICON_CACHE = {}


def icon_info(key):
    """(эмодзи-текст, id своего эмодзи или None) для категории или группы."""
    if key not in _ICON_CACHE:
        default = CATEGORIES[key]["icon"] if key in CATEGORIES else GROUPS[key]["icon"]
        emoji, custom_id = default, None
        raw = get_setting_text(f"icon:{key}")
        if raw:
            try:
                saved = json.loads(raw)
                emoji, custom_id = saved.get("e") or default, saved.get("id")
            except (ValueError, AttributeError):
                pass
        _ICON_CACHE[key] = (emoji, custom_id)
    return _ICON_CACHE[key]


def set_icon(key, emoji, custom_id):
    """emoji=None: вернуть стандартную иконку."""
    if emoji is None:
        delete_setting(f"icon:{key}")
    else:
        set_setting(f"icon:{key}", json.dumps({"e": emoji, "id": custom_id}, ensure_ascii=False))
    _ICON_CACHE.pop(key, None)


def ic(key):
    """Иконка для текста сообщения (HTML). Свой эмодзи виден, если у владельца бота Premium."""
    emoji, custom_id = icon_info(key)
    if custom_id:
        return f'<tg-emoji emoji-id="{esc(custom_id)}">{esc(emoji)}</tg-emoji>'
    return emoji


def icon_btn(key, label, data):
    """Кнопка с иконкой категории: свой эмодзи ставится отдельным полем кнопки."""
    emoji, custom_id = icon_info(key)
    if custom_id:
        return InlineKeyboardButton(text=label, callback_data=data, icon_custom_emoji_id=custom_id)
    return btn(f"{emoji} {label}", data)


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ---------------------------------------------------------------------------
esc = html.escape


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def unit_of(fld, values=None):
    if fld["unit_fn"]:
        return fld["unit_fn"](values or {})
    return fld["unit"]


def plain(fld, value, values=None):
    """Значение для показа без HTML: '3.4 ГГц', 'DDR4' или '—'."""
    if value is None or value == "":
        return "—"
    text = str(value)
    unit = unit_of(fld, values)
    if unit:
        text += " " + unit
    return text


def fmt(fld, value, values=None):
    return esc(plain(fld, value, values))


def field_active(fld, values):
    """Нужно ли это поле при таких значениях (поле с when может быть «выключено»)."""
    return fld["when"] is None or fld["when"](values)


def apply_defaults(cat_key, values):
    """Подставляет значения по умолчанию в пропущенные поля и убирает выключенные поля."""
    out = dict(values)
    for fld in CATEGORIES[cat_key]["fields"]:
        if not field_active(fld, out):
            out.pop(fld["key"], None)
            continue
        if fld["default"] is not None and out.get(fld["key"]) in (None, ""):
            out[fld["key"]] = fld["default"](out) if callable(fld["default"]) else fld["default"]
    return out


def part_count(part):
    count = part["data"].get("count")
    return int(count) if isinstance(count, (int, float)) and count >= 1 else 1


def part_cost(part, take):
    """Себестоимость `take` штук из позиции: пропорциональная доля (так же делит split_part)."""
    price = part_price(part) or 0
    count = part_count(part)
    return price if take >= count else round(price * take / count, 2)


def day_text(stamp):
    """'2026-11-21' или '2026-11-21 11:30' -> '21.11.2026'"""
    try:
        return date.fromisoformat(str(stamp)[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "—"


def parse_day(text):
    """'25.11.2026', '25.11.26', '25.11' или '25/11/2026' -> date. None, если не подошло."""
    text = text.strip().replace("/", ".").replace("-", ".").replace(",", ".")
    today = now_dt().date()
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(f"{text}.{today.year}", "%d.%m.%Y").date()
    except ValueError:
        return None


def get_field(cat_key, key):
    for fld in CATEGORIES[cat_key]["fields"]:
        if fld["key"] == key:
            return fld
    return None


def back_target(cat_key):
    """Куда ведёт кнопка «Назад» из списка категории."""
    parent = CATEGORIES[cat_key].get("parent")
    return f"grp:{parent}" if parent else "parts"


def money(x):
    x = round(x, 2)
    return str(int(x)) if x == int(x) else f"{x:.2f}".rstrip("0")


def money_text(x):
    return (money(x) + " " + CURRENCY).strip()


def profit_text(profit):
    """+30 или -20 (с валютой)."""
    sign = "+" if profit > 0 else ("-" if profit < 0 else "")
    return sign + money_text(abs(profit))


def build_cost(parts):
    return sum(part_price(p) or 0 for p in parts)


def build_costs(build_id, parts=None):
    """Из чего складывается себестоимость сборки."""
    parts = build_parts(build_id) if parts is None else parts
    parts_total = build_cost(parts)
    expenses_total = sum(e["amount"] for e in list_expenses(build_id))
    overhead = get_setting_number("overhead", 0.0) if parts else 0.0
    return {
        "parts": round(parts_total, 2),
        "expenses": round(expenses_total, 2),
        "overhead": round(overhead, 2),
        "total": round(parts_total + expenses_total + overhead, 2),
    }


def sale_lines(cost, sold_price, sold_at):
    """Три строки: себестоимость, цена продажи, прибыль."""
    return [
        f"💰 Себестоимость: {money_text(cost)}",
        f"💵 Продано за: {money_text(sold_price)}  ({date_text(sold_at)})",
        f"📈 Прибыль: <b>{profit_text(sold_price - cost)}</b>",
    ]


def part_price(part):
    value = part["data"].get("price")
    return value if isinstance(value, (int, float)) else None


def part_label(part, with_price=False):
    """Подпись кнопки в списке: название + пара главных характеристик."""
    cat = CATEGORIES[part["cat"]]
    bits = []
    for key in cat["list_hint"]:
        value = part["data"].get(key)
        if value not in (None, ""):
            text = plain(get_field(part["cat"], key), value, part["data"])
            if key == "lighting":  # «Есть» в списке непонятно, пишем словами
                text = "с подсветкой" if value == "Есть" else "без подсветки"
            bits.append(text)
    count = part["data"].get("count")
    if isinstance(count, (int, float)) and count > 1:
        bits.append(f"×{int(count)}")
    if with_price and part_price(part) is not None:
        bits.append(money_text(part_price(part)))
    name = str(part["data"].get("name", "—"))
    return name + (" · " + " · ".join(bits) if bits else "")


def render_card(part):
    cat = CATEGORIES[part["cat"]]
    data = part["data"]
    lines = [f"{ic(part['cat'])} <b>{esc(str(data.get('name', '—')))}</b>", f"<i>{cat['title']}</i>", ""]
    count = part_count(part)
    for fld in cat["fields"][1:]:  # первое поле (название) уже в заголовке
        if fld["hidden"] or not field_active(fld, data):
            continue
        line = f"{fld['label']}: {fmt(fld, data.get(fld['key']), data)}"
        price = data.get(fld["key"])
        if fld["key"] == "price" and count > 1 and isinstance(price, (int, float)):
            line += f" (за штуку: {money_text(price / count)})"
        lines.append(line)
    size = data.get("size")
    if part["cat"] in TOTAL_CATS and data.get("count") and size:
        total = count * size
        total = int(total) if total == int(total) else total
        lines.append(f"Всего: {total} ГБ ({count} × {size} ГБ)")
    added = part.get("added_at")
    lines.append(f"📅 Поступила: {day_text(added)}" if added else "📅 Дата поступления не указана")
    return "\n".join(lines)


def fld_options(fld, values):
    return fld["options_fn"](values) if fld["options_fn"] else fld["options"]


def parse_value(fld, kind, text):
    """Превращает введённый текст в значение. None, если не подошло."""
    text = text.strip()
    if not text:
        return None
    if kind in ("number", "int"):
        try:
            number = float(text.replace(",", "."))
        except ValueError:
            return None
        low, high = fld.get("min"), fld.get("max")
        if low is None:
            if number <= 0:
                return None
        elif number < low:
            return None
        if high is not None and number > high:
            return None
        if kind == "int" and number != int(number):
            return None
        return int(number) if number == int(number) else number
    return text[:500]


TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="[^"]*">(.*?)</tg-emoji>', re.S)


def without_custom_emoji(text, markup):
    """Тот же текст и кнопки, но без своих эмодзи (если Telegram их не принял)."""
    text = TG_EMOJI_RE.sub(r"\1", text)
    if markup is not None:
        rows = [[b.model_copy(update={"icon_custom_emoji_id": None}) for b in row]
                for row in markup.inline_keyboard]
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, markup


def has_custom_emoji(text, markup):
    if "<tg-emoji" in text:
        return True
    return bool(markup) and any(b.icon_custom_emoji_id for row in markup.inline_keyboard for b in row)


async def _send(message, text, markup, edit):
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise
        return message
    return await message.answer(text, reply_markup=markup)


async def show(message, text, markup, edit):
    """edit=True: заменить текст у сообщения бота, иначе отправить новое.
    Возвращает сообщение, в котором теперь этот текст."""
    try:
        return await _send(message, text, markup, edit)
    except TelegramBadRequest:
        if not has_custom_emoji(text, markup):
            raise
        logging.warning("Свои эмодзи не приняты Telegram (нужен Premium у владельца бота), показываю без них")
        text, markup = without_custom_emoji(text, markup)
        return await _send(message, text, markup, edit)


async def remove_keyboard(bot, chat_id, message_id):
    if not message_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------------------
# ЭКРАНЫ (меню)
# ---------------------------------------------------------------------------
HOME_TEXT = f"🖥 <b>Склад комплектующих</b>  <i>v{VERSION}</i>\n\nЧто будем делать?"
PARTS_TEXT = "🧩 <b>Комплектующие</b>\n\nВыбери категорию:"


def main_menu():
    return kb([
        [btn("🧩 Комплектующие", "parts")],
        [btn("🛠 Сборки", "builds")],
        [btn("📜 История и статистика", "hist")],
        [btn("💾 Резервная копия", "bk")],
    ])


def parts_menu():
    rows = []
    for item in MENU:
        if item.startswith("group:"):
            key = item.split(":")[1]
            group = GROUPS[key]
            rows.append([icon_btn(key, group['title'], f"grp:{key}")])
        else:
            cat = CATEGORIES[item]
            rows.append([icon_btn(item, cat['title'], f"cat:{item}")])
    rows.append([btn("🎨 Иконки категорий", "icons")])
    rows.append([btn("⬅️ Назад", "home")])
    return kb(rows)


ICON_KEYS = ["mb", "cpu", "cooler", "ram", "storage", "ssd", "hdd", "gpu", "psu", "case", "fan"]


def icon_title(key):
    return CATEGORIES[key]["title"] if key in CATEGORIES else GROUPS[key]["title"]


async def show_icons(message, edit):
    text = ("🎨 <b>Иконки категорий</b>\n\n"
            "Нажми на категорию и пришли эмодзи: обычный с клавиатуры или свой из набора "
            "(свои эмодзи видны, если у владельца бота есть Telegram Premium).")
    rows = [[icon_btn(k, icon_title(k), f"icset:{k}")] for k in ICON_KEYS]
    rows.append([btn("⬅️ Назад", "parts")])
    await show(message, text, kb(rows), edit)


def group_menu(group_key):
    group = GROUPS[group_key]
    rows = []
    for cat_key in group["items"]:
        cat = CATEGORIES[cat_key]
        rows.append([icon_btn(cat_key, cat['title'], f"cat:{cat_key}")])
    rows.append([btn("⬅️ Назад", "parts")])
    return kb(rows)


async def show_category(message, cat_key, edit):
    cat = CATEGORIES[cat_key]
    parts = list_parts(cat_key)
    text = f"{ic(cat_key)} <b>{cat['plural']}</b>\nВ списке: {len(parts)}"
    in_builds = count_in_builds(cat_key)
    if in_builds:
        text += f"\nВ сборках: {in_builds}"
    if not parts:
        text += "\n\nПока ничего нет."
    rows = [[btn("➕ " + cat["add"], f"add:{cat_key}")]]
    for part in parts:
        rows.append([btn(part_label(part), f"view:{part['id']}")])
    rows.append([btn("⬅️ Назад", back_target(cat_key))])
    await show(message, text, kb(rows), edit)


async def show_card(message, part_id, edit):
    part = get_part(part_id)
    if part is None or part["cat"] not in CATEGORIES:
        await show(message, "Эта деталь не найдена (возможно, уже удалена).",
                   kb([[btn("⬅️ К категориям", "parts")]]), edit)
        return
    if part["sold_price"] is not None:  # проданная деталь: только просмотр
        cost = part_price(part) or 0
        text = render_card(part) + "\n\n" + "\n".join(sale_lines(cost, part["sold_price"], part["sold_at"]))
        if part_price(part) is None:
            text += "\n<i>Цена закупки не была указана, считается 0</i>"
        markup = kb([
            [btn("↩️ Отменить продажу", f"unsell:{part_id}")],
            [btn("🗑 Удалить из истории", f"hdel:{part_id}")],
            [btn("⬅️ К истории", "hp")],
        ])
        await show(message, text, markup, edit)
        return
    if part["build_id"]:
        build = get_build(part["build_id"])
        name = esc(build["name"]) if build else "?"
        sold_build = bool(build and build["status"] == "sold")
        text = render_card(part) + f"\n\n📦 В сборке: <b>{name}</b>"
        if sold_build:
            text += "\nСборка продана, менять её нельзя."
            rows = [[btn("⬅️ К сборке", f"bld:{part['build_id']}")]]
        else:
            text += "\nПока деталь в сборке, менять её нельзя."
            rows = [
                [btn("↩️ Убрать из сборки", f"unbuild:{part_id}")],
                [btn("⬅️ К сборке", f"bld:{part['build_id']}")],
            ]
        await show(message, text, kb(rows), edit)
        return
    rows = [
        [btn("🛠 Начать сборку с этой детали", f"build:{part_id}")],
        [btn("✏️ Изменить", f"edit:{part_id}"), btn("📅 Дата поступления", f"adate:{part_id}")],
    ]
    if part_count(part) > 1:
        rows.append([btn("💵 Продать", f"sell:{part_id}"), btn("✂️ Отделить", f"split:{part_id}")])
        rows.append([btn("🗑 Удалить", f"del:{part_id}")])
    else:
        rows.append([btn("💵 Продать", f"sell:{part_id}"), btn("🗑 Удалить", f"del:{part_id}")])
    rows.append([btn("⬅️ К списку", f"cat:{part['cat']}")])
    await show(message, render_card(part), kb(rows), edit)


# ---------------------------------------------------------------------------
# СБОРКИ
# ---------------------------------------------------------------------------
def build_warnings(parts):
    """Мягкие предупреждения о совместимости. Не запрещают, только подсказывают."""
    by = defaultdict(list)
    for p in parts:
        by[p["cat"]].append(p)
    warns = []
    mb = by["mb"][0] if by["mb"] else None
    cpu = by["cpu"][0] if by["cpu"] else None
    if mb and cpu:
        s_mb, s_cpu = mb["data"].get("socket"), cpu["data"].get("socket")
        if s_mb and s_cpu and s_mb != s_cpu:
            warns.append(f"Сокет процессора ({esc(s_cpu)}) не совпадает с платой ({esc(s_mb)})")
    if mb:
        mb_type = mb["data"].get("ram_type")
        slots = mb["data"].get("ram_slots")
        sticks = 0
        for r in by["ram"]:
            r_type = r["data"].get("ram_type")
            if mb_type and r_type and r_type != mb_type:
                text = f"Тип памяти {esc(r_type)} не совпадает с платой ({esc(mb_type)})"
                if text not in warns:
                    warns.append(text)
            sticks += r["data"].get("count") or 0
        if slots and sticks > slots:
            warns.append(f"Планок памяти {sticks}, а слотов на плате {slots}")
        m2, nvme = m2_slot_info(parts)
        if m2 is not None and nvme > m2:
            warns.append(f"NVMe-накопителей {nvme}, а слотов M.2 на плате {m2}")
    return warns


def ram_slot_info(parts):
    """(слотов на плате или None, занято планок)."""
    mb = next((p for p in parts if p["cat"] == "mb"), None)
    slots = mb["data"].get("ram_slots") if mb else None
    used = sum(int(p["data"].get("count") or 1) for p in parts if p["cat"] == "ram")
    return (int(slots) if isinstance(slots, (int, float)) else None), used


def is_nvme(part):
    return part["cat"] == "ssd" and str(part["data"].get("interface", "")).strip().lower() == "nvme"


def m2_slot_info(parts):
    """(слотов M.2 на плате или None, сколько NVMe уже стоит)."""
    mb = next((p for p in parts if p["cat"] == "mb"), None)
    slots = mb["data"].get("m2") if mb else None
    used = sum(part_count(p) for p in parts if is_nvme(p))
    return (int(slots) if isinstance(slots, (int, float)) else None), used


def _differ(a, b):
    """Оба значения указаны и не совпадают (без учёта регистра и пробелов)."""
    if a in (None, "") or b in (None, ""):
        return False
    return str(a).strip().lower() != str(b).strip().lower()


def incompatibility(part, parts):
    """Почему деталь не подходит к сборке (текст) или None, если подходит.
    Зависимости только у платы: с процессором (сокет), с памятью (тип DDR, число планок)
    и с NVMe-накопителями (слоты M.2). Всё остальное подходит к чему угодно."""
    d = part["data"]
    if part["cat"] == "mb":
        for p in parts:
            pd = p["data"]
            if p["cat"] == "cpu" and _differ(d.get("socket"), pd.get("socket")):
                return f"Сокет платы {d.get('socket')} не подходит к процессору {pd.get('socket')}"
            if p["cat"] == "ram" and _differ(d.get("ram_type"), pd.get("ram_type")):
                return f"Плата под {d.get('ram_type')}, а в сборке память {pd.get('ram_type')}"
        _, sticks = ram_slot_info(parts)
        slots = d.get("ram_slots")
        if isinstance(slots, (int, float)) and sticks > slots:
            return f"В сборке {sticks} планок памяти, а у платы слотов {int(slots)}"
        m2 = d.get("m2")
        _, nvme = m2_slot_info(parts)
        if isinstance(m2, (int, float)) and nvme > m2:
            return f"В сборке NVMe-накопителей {nvme}, а у платы слотов M.2 {int(m2)}"
        return None
    mb = next((p for p in parts if p["cat"] == "mb"), None)
    if mb is None:
        return None
    md = mb["data"]
    if part["cat"] == "cpu" and _differ(d.get("socket"), md.get("socket")):
        return f"Сокет процессора {d.get('socket')}, а у платы {md.get('socket')}"
    if part["cat"] == "ram" and _differ(d.get("ram_type"), md.get("ram_type")):
        return f"Память {d.get('ram_type')}, а плата под {md.get('ram_type')}"
    return None


def max_take(part, parts):
    """(сколько штук этой позиции можно положить в сборку, причина если 0)."""
    count = part_count(part)
    if part["cat"] in SINGLE_SLOTS:
        return 1, None
    if part["cat"] == "ram":
        slots, used = ram_slot_info(parts)
        if slots:
            if used >= slots:
                return 0, f"Свободных слотов памяти нет: занято {used} из {slots}"
            return min(count, slots - used), None
    if is_nvme(part):
        slots, used = m2_slot_info(parts)
        if slots is not None:
            if used >= slots:
                return 0, f"Свободных слотов M.2 нет: занято {used} из {slots}"
            return min(count, slots - used), None
    return count, None


def title_in_build(p):
    """Название детали для строки сборки: имя, объём планки, ×количество."""
    data = p["data"]
    text = str(data.get("name", "—"))
    if p["cat"] == "ram" and data.get("size"):
        text += f" · {data['size']} ГБ"
    count = data.get("count")
    if isinstance(count, (int, float)) and count > 1:
        text += f" ×{int(count)}"
    return text


async def show_builds(message, edit):
    builds = list_builds()
    text = f"🛠 <b>Сборки</b>\nВсего: {len(builds)}"
    if not builds:
        text += "\n\nПока нет ни одной сборки."
    rows = [[btn("➕ Новая сборка", "bnew")]]
    for b in builds:
        total = build_costs(b["id"])["total"]
        rows.append([btn(f"🛠 {b['name']} · {money_text(total)}", f"bld:{b['id']}")])
    overhead = get_setting_number("overhead", 0.0)
    rows.append([btn(f"⚙️ Авто-расходы на сборку: {money_text(overhead)}", "bover")])
    rows.append([btn("⬅️ Назад", "home")])
    await show(message, text, kb(rows), edit)


async def show_build(message, build_id, edit):
    build = get_build(build_id)
    if build is None:
        await show(message, "Сборка не найдена (возможно, уже удалена).",
                   kb([[btn("⬅️ К сборкам", "builds")]]), edit)
        return
    parts = build_parts(build_id)
    sold = build["status"] == "sold"
    lines = [f"🛠 <b>{esc(build['name'])}</b>"]
    if sold:
        lines.append("💵 Продана")
    lines.append("")
    if not parts:
        lines.append("В сборке пока нет деталей.")
    by = defaultdict(list)
    for p in parts:
        by[p["cat"]].append(p)

    no_price = 0
    for cat_key in BUILD_ORDER:
        cat = CATEGORIES[cat_key]
        for p in by[cat_key]:
            price = part_price(p)
            if price is None:
                no_price += 1
                price_text = "цена не указана"
            else:
                price_text = money_text(price)
            lines.append(f"{ic(cat_key)} {esc(title_in_build(p))} — {price_text}")

    if by["ram"]:  # сколько памяти уже набрано и сколько слотов занято
        gb = sum(int(p["data"].get("count") or 1) * (p["data"].get("size") or 0) for p in by["ram"])
        slots, used = ram_slot_info(parts)
        gb_text = int(gb) if gb == int(gb) else gb
        slot_text = f"{used} из {slots}" if slots else f"{used}"
        lines.append(f"🧮 <b>Память: {gb_text} ГБ</b> · слотов занято {slot_text}")

    if not sold and parts:  # только для справки: продать можно и неполную сборку
        missing = []
        for cat_key in ("mb", "cpu", "cooler", "ram", "storage", "gpu", "psu", "case"):
            if cat_key == "storage":
                if not by["ssd"] and not by["hdd"]:
                    missing.append("накопитель")
            elif not by[cat_key]:
                missing.append(short_title(cat_key).lower())
        if missing:
            lines.append(f"<i>Нет в сборке: {', '.join(missing)}</i>")

    costs = build_costs(build_id, parts)
    if sold:
        total = build["cost"] if build["cost"] is not None else costs["total"]
        overhead = round(max(0.0, total - costs["parts"] - costs["expenses"]), 2)
    else:
        total, overhead = costs["total"], costs["overhead"]
    if costs["expenses"] > 0 or overhead > 0:
        lines.append("")
        if costs["expenses"] > 0:
            lines.append(f"💸 Прочие расходы — {money_text(costs['expenses'])}")
        if overhead > 0:
            lines.append(f"🧴 Авто-расходы (термопаста, хомуты и т.п.) — {money_text(overhead)}")

    lines.append("")
    if sold:
        lines += sale_lines(total, build["sold_price"] or 0, build["sold_at"])
    else:
        lines.append(f"💰 <b>Себестоимость: {money_text(total)}</b>")
    if no_price:
        lines.append(f"<i>У деталей без цены ({no_price} шт.) считается 0</i>")
    if not sold:
        for warn in build_warnings(parts):
            lines.append(f"⚠️ {warn}")

    rows = []
    for p in parts:
        cat = CATEGORIES[p["cat"]]
        rows.append([icon_btn(p["cat"], title_in_build(p), f"view:{p['id']}")])
    if sold:
        rows.append([btn("↩️ Отменить продажу", f"bunsell:{build_id}")])
        rows.append([btn("🗑 Удалить из истории", f"bhdel:{build_id}")])
        rows.append([btn("⬅️ К истории сборок", "hb")])
        await show(message, "\n".join(lines), kb(rows), edit)
        return
    add_buttons = []
    for cat_key in BUILD_ORDER:
        if cat_key in SINGLE_SLOTS and by[cat_key]:
            continue
        add_buttons.append(btn(f"➕ {short_title(cat_key)}", f"bpick:{build_id}:{cat_key}"))
    rows += chunk(add_buttons, 2)
    rows.append([btn("💸 Прочие расходы", f"bexp:{build_id}")])
    rows.append([btn("✏️ Название сборки", f"bname:{build_id}")])
    if parts:
        rows.append([btn("💵 Продать сборку", f"bsell:{build_id}")])
    rows.append([btn("🗑 Удалить сборку", f"bdel:{build_id}")])
    rows.append([btn("⬅️ К сборкам", "builds")])
    await show(message, "\n".join(lines), kb(rows), edit)


async def show_expenses(message, build_id, edit):
    build = get_build(build_id)
    if build is None:
        await show(message, "Сборка не найдена.", kb([[btn("⬅️ К сборкам", "builds")]]), edit)
        return
    items = list_expenses(build_id)
    total = sum(e["amount"] for e in items)
    text = f"💸 <b>Прочие расходы</b> · {esc(build['name'])}\nВсего: {money_text(total)}"
    if not items:
        text += "\n\nПока ничего не добавлено."
    text += "\n\n<i>Сумма прибавляется к себестоимости сборки.</i>"
    rows = [[btn(f"{e['label']} — {money_text(e['amount'])}", f"bexpitem:{e['id']}")] for e in items]
    for i, label in enumerate(EXPENSE_PRESETS):
        rows.append([btn(f"➕ {label}", f"bexpadd:{build_id}:{i}")])
    rows.append([btn("➕ Другое (своё название)", f"bexpoth:{build_id}")])
    rows.append([btn("⬅️ К сборке", f"bld:{build_id}")])
    await show(message, text, kb(rows), edit)


async def show_new_build(message, edit):
    """Новая сборка: с какой категории начать (любая деталь, как в конструкторе)."""
    text = ("🛠 <b>Новая сборка</b>\nС какой детали начнём?\n\n"
            "<i>Дальше бот покажет только то, что подходит: плату под выбранную память и процессор, "
            "процессор и память под выбранную плату. Остальное подходит ко всему.</i>")
    buttons = []
    for cat_key in BUILD_ORDER:
        cat = CATEGORIES[cat_key]
        buttons.append(icon_btn(cat_key, f"{short_title(cat_key)} ({len(list_parts(cat_key))})",
                           f"bpick:0:{cat_key}"))
    rows = chunk(buttons, 2)
    rows.append([btn("⬅️ Назад", "builds")])
    await show(message, text, kb(rows), edit)


async def show_pick(message, cat_key, build_id=0):
    """Список свободных деталей категории, подходящих к сборке. build_id=0: новая сборка."""
    cat = CATEGORIES[cat_key]
    free = list_parts(cat_key)
    if not build_id:
        parts = []
        text = f"🛠 <b>Новая сборка · {cat['plural']}</b>\nВыбери деталь, с неё начнётся сборка:"
        prefix, back = "bsel:0:", "bnew"
    else:
        build = get_build(build_id)
        parts = build_parts(build_id)
        text = f"{ic(cat_key)} <b>{cat['plural']}</b>\nВыбери деталь в сборку «{esc(build['name'])}»:"
        prefix, back = f"bsel:{build_id}:", f"bld:{build_id}"
        if cat_key == "ram":
            slots, used = ram_slot_info(parts)
            if slots:
                text += f"\n<i>Слотов памяти занято {used} из {slots}</i>"
        if cat_key == "ssd":
            slots, used = m2_slot_info(parts)
            if slots is not None:
                text += f"\n<i>Слотов M.2 для NVMe занято {used} из {slots}</i>"
    fit = [p for p in free if incompatibility(p, parts) is None]
    hidden = len(free) - len(fit)
    if hidden:
        text += f"\n<i>Не подходят к этой сборке и скрыты: {hidden}</i>"
    if not free:
        text += "\n\nСвободных деталей этой категории нет. Добавь их в разделе комплектующих."
    elif not fit:
        text += "\n\nПодходящих деталей нет."
    rows = [[btn(part_label(p, with_price=True), f"{prefix}{p['id']}")] for p in fit]
    rows.append([btn("⬅️ Назад", back)])
    await show(message, text, kb(rows), True)


async def show_quantity(message, build_id, part, limit):
    """Сколько штук из записи (комплекта) взять в сборку. build_id=0: новая сборка."""
    count = part_count(part)
    cat = part["cat"]
    note = f"\nМожно взять: до {limit} шт." if limit < count else ""
    where = f"в сборку «{esc(get_build(build_id)['name'])}»" if build_id else "в новую сборку"
    text = (f"{ic(cat)} <b>{esc(str(part['data'].get('name', '—')))}</b>\n"
            f"На складе: {count} шт.{note}\n\n"
            f"Сколько взять {where}?\n"
            "<i>Остальное останется на складе.</i>")
    buttons = [btn(str(n), f"bqty:{build_id}:{part['id']}:{n}") for n in range(1, min(limit, 12) + 1)]
    rows = chunk(buttons, 4)
    rows.append([btn("⬅️ Назад", f"bpick:{build_id}:{cat}")])
    await show(message, text, kb(rows), True)


# ---------------------------------------------------------------------------
# ИСТОРИЯ ПРОДАЖ
# ---------------------------------------------------------------------------
HISTORY_LIMIT = 30  # сколько последних продаж показывать кнопками


def history_menu():
    return kb([
        [btn("🧩 История комплектующих", "hp")],
        [btn("🛠 История сборок", "hb")],
        [btn("📊 Статистика", "st")],
        [btn("⬅️ Назад", "home")],
    ])


def history_summary(items):
    """items: список (себестоимость, цена продажи)."""
    cost = sum(c for c, _ in items)
    revenue = sum(s for _, s in items)
    return [
        f"Продано: {len(items)}",
        f"Себестоимость: {money_text(cost)}",
        f"Выручка: {money_text(revenue)}",
        f"Прибыль: <b>{profit_text(revenue - cost)}</b>",
    ]


def history_label(name, cost, sold_price):
    return f"{name} · {money_text(cost)} → {money_text(sold_price)} ({profit_text(sold_price - cost)})"


async def show_history_parts(message, edit):
    parts = list_sold_parts()
    items = [(part_price(p) or 0, p["sold_price"]) for p in parts]
    text = "🧩 <b>История комплектующих</b>\n\n" + "\n".join(history_summary(items))
    text += "\n\n<i>Кнопка: название · себестоимость → цена продажи (прибыль)</i>"
    if not parts:
        text = "🧩 <b>История комплектующих</b>\n\nПока ничего не продано."
    elif len(parts) > HISTORY_LIMIT:
        text += f"\n<i>Показаны последние {HISTORY_LIMIT} из {len(parts)}. В итогах учтены все.</i>"
    rows = []
    for p in parts[:HISTORY_LIMIT]:
        cat = CATEGORIES.get(p["cat"], {"icon": "•"})
        label = history_label(title_in_build(p), part_price(p) or 0, p["sold_price"])
        rows.append([icon_btn(p["cat"], label, f"view:{p['id']}") if p["cat"] in CATEGORIES
                     else btn(label, f"view:{p['id']}")])
    rows.append([btn("⬅️ Назад", "hist")])
    await show(message, text, kb(rows), edit)


async def show_history_builds(message, edit):
    builds = list_sold_builds()
    items = [((b["cost"] or 0), (b["sold_price"] or 0)) for b in builds]
    text = "🛠 <b>История сборок</b>\n\n" + "\n".join(history_summary(items))
    text += "\n\n<i>Кнопка: название · себестоимость → цена продажи (прибыль)</i>"
    if not builds:
        text = "🛠 <b>История сборок</b>\n\nПока ни одна сборка не продана."
    elif len(builds) > HISTORY_LIMIT:
        text += f"\n<i>Показаны последние {HISTORY_LIMIT} из {len(builds)}. В итогах учтены все.</i>"
    rows = []
    for b in builds[:HISTORY_LIMIT]:
        label = history_label(b["name"], b["cost"] or 0, b["sold_price"] or 0)
        rows.append([btn(label, f"bld:{b['id']}")])
    rows.append([btn("⬅️ Назад", "hist")])
    await show(message, text, kb(rows), edit)


# ---------------------------------------------------------------------------
# ПОШАГОВЫЙ ВВОД (FSM). Одно и то же состояние используется и для добавления,
# и для изменения одного поля. Вся «память» диалога лежит в state.get_data():
#   cat, idx (какое поле сейчас), values (уже введённое), single (меняем одно поле?),
#   part_id, other (ввод своего варианта), prompt_id (id сообщения с вопросом)
# ---------------------------------------------------------------------------
class Form(StatesGroup):
    waiting = State()


class BuildName(StatesGroup):
    waiting = State()


class Sale(StatesGroup):
    waiting = State()


class ExpenseForm(StatesGroup):
    waiting = State()


class OverheadForm(StatesGroup):
    waiting = State()


class DateForm(StatesGroup):
    waiting = State()


class IconForm(StatesGroup):
    waiting = State()


SALE_FIELD = field("sale", "Цена продажи", "number", minimum=0)
EXPENSE_FIELD = field("expense", "Сумма", "number")          # строго больше нуля
OVERHEAD_FIELD = field("overhead", "Авто-расходы", "number", minimum=0)


async def ask(message, state, edit):
    """Задаёт вопрос про текущее поле."""
    data = await state.get_data()
    cat = CATEGORIES[data["cat"]]
    fld = cat["fields"][data["idx"]]
    key = fld["key"]
    single = data.get("single", False)
    other = data.get("other", False)

    if single:
        head = f"✏️ <b>Изменение:</b> {fld['label']}"
        current = data["values"].get(key)
        if current not in (None, ""):
            head += f"\nСейчас: {fmt(fld, current, data['values'])}"
    else:
        head = f"{ic(data['cat'])} <b>{cat['add']}</b>\nШаг {data['idx'] + 1} из {len(cat['fields'])}"

    rows = []
    if fld["kind"] == "choice" and not other:
        body = f"Выбери: <b>{fld['label']}</b>"
        if fld["hint"]:
            body += f"\n<i>{esc(fld['hint'])}</i>"
        if fld["hint_fn"]:
            extra = fld["hint_fn"](data["values"])
            if extra:
                body += f"\n<i>{esc(extra)}</i>"
        buttons = []
        unit = unit_of(fld, data["values"])
        for i, option in enumerate(fld_options(fld, data["values"])):
            label = option + (" " + unit if unit else "")
            # В кнопке зашито и имя поля: так старая кнопка не попадёт в чужое поле
            buttons.append(btn(label, f"opt:{key}:{i}"))
        rows += chunk(buttons, 3)
        if fld["allow_other"]:
            rows.append([btn("✏️ Другое", f"opt:{key}:other")])
        if fld["optional"]:
            rows.append([btn("🧹 Очистить" if single else "⏭ Пропустить", f"skip:{key}")])
    else:
        body = f"Введи: <b>{fld['label']}</b>"
        if other:
            body = f"Введи свой вариант: <b>{fld['label']}</b>"
        unit = unit_of(fld, data["values"])
        if fld["kind"] in ("number", "int") or (fld["kind"] == "choice" and fld["numeric"]):
            body += f" (числом{', ' + unit if unit else ''})"
        if fld["hint"]:
            body += f"\n<i>{esc(fld['hint'])}</i>"
        if fld["hint_fn"]:
            extra = fld["hint_fn"](data["values"])
            if extra:
                body += f"\n<i>{esc(extra)}</i>"
        if fld["optional"] and not other:
            rows.append([btn("🧹 Очистить" if single else "⏭ Пропустить", f"skip:{key}")])
    rows.append([btn("❌ Отмена", "cancel")])

    text = head + "\n\n" + body
    sent = await show(message, text, kb(rows), edit)
    await state.update_data(prompt_id=sent.message_id)


async def accept_value(message, state, value, via_callback):
    """Запоминает ответ и переходит к следующему полю или сохраняет всё."""
    data = await state.get_data()
    cat_key = data["cat"]
    fields = CATEGORIES[cat_key]["fields"]
    values = dict(data["values"])
    fld = fields[data["idx"]]
    key = fld["key"]

    if value is None:
        values.pop(key, None)
    else:
        values[key] = value

    if data.get("single"):
        # Если изменили поле, от которого зависят другие (ядра -> потоки, подсветка -> её тип),
        # зависимое поле убираем (если оно больше не нужно) или спрашиваем заново
        for dep_key in fld["then"]:
            dep = get_field(cat_key, dep_key)
            if not field_active(dep, values):
                values.pop(dep_key, None)
                continue
            current = values.get(dep_key)
            broken = current is not None and dep["check"] and dep["check"](current, values)
            if broken or (dep["reask"] and value is not None):
                values.pop(dep_key, None)
                update_part(data["part_id"], apply_defaults(cat_key, values))
                dep_idx = [f["key"] for f in fields].index(dep_key)
                await state.update_data(values=values, idx=dep_idx, other=False)
                await ask(message, state, edit=via_callback)
                return
        update_part(data["part_id"], apply_defaults(cat_key, values))
        await state.clear()
        await show_card(message, data["part_id"], edit=via_callback)
        return

    next_idx = data["idx"] + 1
    while next_idx < len(fields) and not field_active(fields[next_idx], values):
        values.pop(fields[next_idx]["key"], None)  # вопрос не нужен (например, нет подсветки)
        next_idx += 1
    if next_idx >= len(fields):
        part_id = add_part(cat_key, apply_defaults(cat_key, values))
        await state.clear()
        await show_card(message, part_id, edit=via_callback)
        return

    await state.update_data(values=values, idx=next_idx, other=False)
    await ask(message, state, edit=via_callback)


# ---------------------------------------------------------------------------
# ЗАЩИТА ОТ ГОНКИ: действия одного пользователя обрабатываются строго по очереди,
# даже если нажать две кнопки подряд очень быстро.
# ---------------------------------------------------------------------------
class OneAtATime(BaseMiddleware):
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        async with self.locks[user.id]:
            return await handler(event, data)


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ
# ---------------------------------------------------------------------------
dp = Dispatcher()
dp.update.outer_middleware(OneAtATime())

if ALLOWED_IDS:
    dp.message.filter(F.from_user.id.in_(ALLOWED_IDS))
    dp.callback_query.filter(F.from_user.id.in_(ALLOWED_IDS))


# --- навигация -------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HOME_TEXT, reply_markup=main_menu())


@dp.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb.message, HOME_TEXT, main_menu(), True)
    await cb.answer()


@dp.callback_query(F.data == "parts")
async def cb_parts(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb.message, PARTS_TEXT, parts_menu(), True)
    await cb.answer()


@dp.callback_query(F.data == "icons")
async def cb_icons(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_icons(cb.message, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("icset:"))
async def cb_icon_set(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in ICON_KEYS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await state.set_state(IconForm.waiting)
    await state.update_data(icon_key=key, prompt_id=cb.message.message_id)
    default = CATEGORIES[key]["icon"] if key in CATEGORIES else GROUPS[key]["icon"]
    await show(
        cb.message,
        f"🎨 <b>{esc(icon_title(key))}</b>\nСейчас: {ic(key)}\n\n"
        "Пришли одним сообщением один эмодзи для этой категории.",
        kb([[btn(f"↩️ Стандартная ({default})", f"icdef:{key}")], [btn("⬅️ Назад", "icons")]]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("icdef:"))
async def cb_icon_default(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in ICON_KEYS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await state.clear()
    set_icon(key, None, None)
    await cb.answer("Вернул стандартную")
    await show_icons(cb.message, edit=True)


@dp.message(IconForm.waiting, F.text, ~F.text.startswith("/"))
async def on_icon(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    custom = next((e for e in (message.entities or []) if e.type == "custom_emoji"), None)
    if custom is not None:
        emoji, custom_id = custom.extract_from(message.text), custom.custom_emoji_id
    else:
        emoji, custom_id = message.text.strip(), None
        if not emoji or len(emoji) > 8 or any(ch.isalnum() for ch in emoji):
            await message.answer("Пришли только эмодзи, без букв и цифр 🙂")
            return
    set_icon(data["icon_key"], emoji, custom_id)
    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    await state.clear()
    await show_icons(message, edit=False)


@dp.callback_query(F.data.startswith("grp:"))
async def cb_group(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    group_key = cb.data.split(":")[1]
    if group_key not in GROUPS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    group = GROUPS[group_key]
    await show(cb.message, f"{ic(group_key)} <b>{group['title']}</b>\n\nВыбери тип:",
               group_menu(group_key), True)
    await cb.answer()


async def part_in_build_alert(cb, part):
    """True (и сообщение), если деталь в сборке или продана и менять её нельзя."""
    if part and part["sold_price"] is not None:
        await cb.answer("Деталь продана. Сначала отмени продажу в истории.", show_alert=True)
        return True
    if part and part["build_id"]:
        await cb.answer("Деталь в сборке. Сначала убери её из сборки.", show_alert=True)
        return True
    return False


async def build_sold_alert(cb, build):
    """True (и сообщение), если сборка продана и менять её нельзя."""
    if build and build["status"] == "sold":
        await cb.answer("Сборка продана. Сначала отмени продажу в истории.", show_alert=True)
        return True
    return False


# --- сборки ----------------------------------------------------------------
@dp.callback_query(F.data == "builds")
async def cb_builds(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_builds(cb.message, edit=True)
    await cb.answer()


@dp.callback_query(F.data == "bnew")
async def cb_build_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_new_build(cb.message, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("build:"))
async def cb_build_from_card(cb: CallbackQuery):
    """«Начать сборку с этой детали» из карточки: сборка 0 = новая."""
    await add_to_build(cb, 0, int(cb.data.split(":")[1]))


@dp.callback_query(F.data.startswith("bld:"))
async def cb_build_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_build(cb.message, int(cb.data.split(":")[1]), edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("bpick:"))
async def cb_build_pick(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, build_id, cat_key = cb.data.split(":")
    build_id = int(build_id)
    if cat_key not in CATEGORIES:
        await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)
        return
    if build_id:  # 0 = новая сборка, её ещё нет в базе
        build = get_build(build_id)
        if build is None:
            await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)
            return
        if await build_sold_alert(cb, build):
            return
    await show_pick(cb.message, cat_key, build_id)
    await cb.answer()


async def add_to_build(cb, build_id, part_id, take=None):
    """Кладёт деталь в сборку. build_id=0: новая сборка (создаётся, только когда деталь
    действительно кладётся). take: сколько штук взять из комплекта (None: спросить)."""
    part = get_part(part_id)
    build = get_build(build_id) if build_id else None
    if part is None or part["cat"] not in CATEGORIES or (build_id and build is None):
        await cb.answer("Не найдено", show_alert=True)
        return
    if await build_sold_alert(cb, build):
        return
    if part["build_id"] or part["sold_price"] is not None:
        await cb.answer("Эта деталь уже в сборке или продана", show_alert=True)
        return
    parts = build_parts(build_id) if build_id else []
    cat = part["cat"]
    if cat in SINGLE_SLOTS and any(p["cat"] == cat for p in parts):
        await cb.answer(f"В сборке уже есть: {CATEGORIES[cat]['title'].lower()}", show_alert=True)
        return
    reason = incompatibility(part, parts)
    if reason:
        await cb.answer(f"Не подходит: {reason}", show_alert=True)
        return
    limit, reason = max_take(part, parts)
    if limit <= 0:
        await cb.answer(reason, show_alert=True)
        return
    count = part_count(part)
    if take is None:
        if limit > 1:
            await show_quantity(cb.message, build_id, part, limit)
            await cb.answer()
            return
        take = 1
    if not (1 <= take <= limit):
        await cb.answer(f"Можно взять не больше {limit} шт.", show_alert=True)
        return
    if take < count:
        new_id = split_part(part_id, take)
        if new_id is None:
            await cb.answer("Не получилось разделить комплект", show_alert=True)
            return
        part_id = new_id
    created = False
    if not build_id:
        build_id = create_build()
        created = True
    if not assign_part(part_id, build_id):
        if created:
            delete_build(build_id)
        await cb.answer("Не получилось добавить: деталь уже занята", show_alert=True)
        return
    merge_in_build(build_id, part_id)
    await show_build(cb.message, build_id, edit=True)
    await cb.answer("Сборка создана" if created else "Добавлено")


@dp.callback_query(F.data.startswith("bsel:"))
async def cb_build_select(cb: CallbackQuery):
    _, build_id, part_id = cb.data.split(":")
    await add_to_build(cb, int(build_id), int(part_id))


@dp.callback_query(F.data.startswith("bqty:"))
async def cb_build_quantity(cb: CallbackQuery):
    _, build_id, part_id, take = cb.data.split(":")
    await add_to_build(cb, int(build_id), int(part_id), take=int(take))


async def remove_from_build(cb, part_id):
    part = get_part(part_id)
    build_id = part["build_id"]
    if part["cat"] == "mb":
        release_motherboard(build_id, part_id)
        await cb.answer("Плата, процессор и память вернулись на склад")
        await show_pick(cb.message, "mb", build_id)  # пустой слот: выбор новой платы
        return
    release_part(part_id)
    await show_build(cb.message, build_id, edit=True)
    await cb.answer("Деталь вернулась в комплектующие")


@dp.callback_query(F.data.startswith("unbuild:"))
async def cb_unbuild(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or not part["build_id"]:
        await cb.answer("Деталь уже не в сборке", show_alert=True)
        return
    build_id = part["build_id"]
    if await build_sold_alert(cb, get_build(build_id)):
        return
    if part["cat"] == "mb":
        dependents = [p for p in build_parts(build_id) if p["cat"] in ("cpu", "ram")]
        if dependents:  # вместе с платой уйдут процессор и память: спрашиваем один раз
            names = "\n".join(f"• {esc(title_in_build(p))}" for p in dependents)
            await show(
                cb.message,
                f"Убрать материнскую плату «{esc(str(part['data'].get('name', '—')))}» из сборки?\n\n"
                f"Вместе с ней вернутся на склад:\n{names}",
                kb([[btn("↩️ Да, убрать", f"unbmb:{part_id}"), btn("Отмена", f"bld:{build_id}")]]),
                True,
            )
            await cb.answer()
            return
    await remove_from_build(cb, part_id)


@dp.callback_query(F.data.startswith("unbmb:"))
async def cb_unbuild_mb(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or not part["build_id"] or part["cat"] != "mb":
        await cb.answer("Плата уже не в сборке", show_alert=True)
        return
    if await build_sold_alert(cb, get_build(part["build_id"])):
        return
    await remove_from_build(cb, part_id)


@dp.callback_query(F.data.startswith("bdel:"))
async def cb_build_delete(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if await build_sold_alert(cb, build):
        return
    count = len(build_parts(build_id))
    await show(
        cb.message,
        f"Удалить сборку «{esc(build['name'])}»?\n"
        f"Деталей в ней: {count}. Они вернутся в раздел комплектующих.",
        kb([[btn("🗑 Да, удалить", f"bdelok:{build_id}"), btn("Отмена", f"bld:{build_id}")]]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("bdelok:"))
async def cb_build_delete_ok(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    if await build_sold_alert(cb, get_build(build_id)):
        return
    delete_build(build_id)
    await cb.answer("Сборка удалена, детали вернулись")
    await show_builds(cb.message, edit=True)


@dp.callback_query(F.data.startswith("bname:"))
async def cb_build_name(cb: CallbackQuery, state: FSMContext):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if await build_sold_alert(cb, build):
        return
    await state.set_state(BuildName.waiting)
    await state.update_data(build_id=build_id, prompt_id=cb.message.message_id)
    await show(
        cb.message,
        f"✏️ <b>Название сборки</b>\nСейчас: {esc(build['name'])}\n\n"
        "Напиши новое название: любые буквы, цифры и символы.\n"
        "<i>Например: Ультрабюджетный игровой, Офисник i3</i>",
        kb([[btn("❌ Отмена", f"bld:{build_id}")]]),
        True,
    )
    await cb.answer()


@dp.message(BuildName.waiting, F.text, ~F.text.startswith("/"))
async def on_build_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    name = message.text.strip()[:100]
    if not name:
        await message.answer("Название пустое, напиши ещё раз.")
        return
    rename_build(data["build_id"], name)
    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    await state.clear()
    await show_build(message, data["build_id"], edit=False)


# --- продажа ---------------------------------------------------------------
async def ask_sale_price(cb, state, kind, item_id, name, cost, back, take=None):
    await state.set_state(Sale.waiting)
    await state.update_data(kind=kind, item_id=item_id, take=take, cost=cost, back=back,
                            prompt_id=cb.message.message_id)
    if take and take > 1:
        text = (f"💵 <b>Продажа: {esc(name)} ×{take}</b>\n"
                f"Себестоимость {take} шт.: {money_text(cost)}\n\n"
                f"За сколько продал все {take} шт. вместе? Напиши общую сумму.\n<i>Например: 250</i>")
    else:
        text = (f"💵 <b>Продажа: {esc(name)}</b>\n"
                f"Себестоимость: {money_text(cost)}\n\n"
                "За сколько продал? Напиши число.\n<i>Например: 250</i>")
    await show(cb.message, text, kb([[btn("❌ Отмена", back)]]), True)
    await cb.answer()


async def sellable_part(cb, part_id):
    """Свободная деталь для продажи или отделения; иначе сообщение и None."""
    part = get_part(part_id)
    if part is None or part["cat"] not in CATEGORIES:
        await cb.answer("Не найдено", show_alert=True)
        return None
    if await part_in_build_alert(cb, part):
        return None
    return part


@dp.callback_query(F.data.startswith("sell:"))
async def cb_sell_part(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    part_id = int(cb.data.split(":")[1])
    part = await sellable_part(cb, part_id)
    if part is None:
        return
    count = part_count(part)
    name = str(part["data"].get("name", "—"))
    if count == 1:
        await ask_sale_price(cb, state, "part", part_id, name, part_cost(part, 1), f"view:{part_id}", 1)
        return
    buttons = [btn(str(n), f"sellq:{part_id}:{n}") for n in range(1, min(count, 12) + 1)]
    rows = chunk(buttons, 4)
    if count > 12:
        rows.append([btn(f"Все {count} шт.", f"sellq:{part_id}:{count}")])
    rows.append([btn("⬅️ Назад", f"view:{part_id}")])
    await show(cb.message,
               f"💵 <b>Продажа: {esc(name)}</b>\nНа складе: {count} шт.\n\nСколько штук продаёшь?\n"
               "<i>Остальное останется на складе по прежней цене за штуку.</i>",
               kb(rows), True)
    await cb.answer()


@dp.callback_query(F.data.startswith("sellq:"))
async def cb_sell_quantity(cb: CallbackQuery, state: FSMContext):
    _, part_id, take = cb.data.split(":")
    part_id, take = int(part_id), int(take)
    part = await sellable_part(cb, part_id)
    if part is None:
        return
    if not (1 <= take <= part_count(part)):
        await cb.answer("Столько на складе нет", show_alert=True)
        return
    await ask_sale_price(cb, state, "part", part_id, str(part["data"].get("name", "—")),
                         part_cost(part, take), f"view:{part_id}", take)


@dp.callback_query(F.data.startswith("bsell:"))
async def cb_sell_build(cb: CallbackQuery, state: FSMContext):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if await build_sold_alert(cb, build):
        return
    parts = build_parts(build_id)
    if not parts:
        await cb.answer("В сборке нет деталей", show_alert=True)
        return
    await ask_sale_price(cb, state, "build", build_id, build["name"],
                         build_costs(build_id, parts)["total"], f"bld:{build_id}")


async def finish_sale(message, data, price, edit):
    """Записывает продажу. Для части позиции сначала отделяет проданные штуки."""
    if data["kind"] == "part":
        part = get_part(data["item_id"])
        if part is None or part["build_id"] or part["sold_price"] is not None:
            await message.answer("Не получилось: деталь уже в сборке, продана или удалена.")
            return
        count = part_count(part)
        take = data.get("take") or count
        if take > count:
            await message.answer("Не получилось: на складе уже меньше штук, чем ты продаёшь.")
            return
        target = data["item_id"]
        if take < count:
            target = split_part(target, take)
            if target is None:
                await message.answer("Не получилось отделить проданные штуки.")
                return
        if not sell_part(target, price):
            await message.answer("Не получилось: деталь уже в сборке, продана или удалена.")
            return
        await show_card(message, target, edit=edit)
    else:
        build = get_build(data["item_id"])
        if build is None or build["status"] == "sold":
            await message.answer("Не получилось: сборка уже продана или удалена.")
            return
        sell_build(data["item_id"], price, build_costs(data["item_id"])["total"])
        await show_build(message, data["item_id"], edit=edit)


@dp.message(Sale.waiting, F.text, ~F.text.startswith("/"))
async def on_sale_price(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    price = parse_value(SALE_FIELD, "number", message.text)
    if price is None:
        await message.answer("Не понял цену 🤔 Напиши число, например 250 (0 тоже можно).")
        return
    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    cost = data.get("cost") or 0
    if price < cost:  # продажа в минус: сначала предупреждаем
        sent = await message.answer(
            "⚠️ <b>Продажа в минус</b>\n"
            f"Себестоимость: {money_text(cost)}\n"
            f"Цена продажи: {money_text(price)}\n"
            f"Убыток: <b>{money_text(cost - price)}</b>\n\n"
            "Продать всё равно? Или напиши другую цену.",
            reply_markup=kb([[btn("✅ Да, продать", "saleok")], [btn("❌ Отмена", data["back"])]]),
        )
        await state.update_data(pending=price, prompt_id=sent.message_id)
        return
    await state.clear()
    await finish_sale(message, data, price, edit=False)


@dp.callback_query(Sale.waiting, F.data == "saleok")
async def cb_sale_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("pending") is None or cb.message.message_id != data.get("prompt_id"):
        await cb.answer("Эта кнопка устарела")
        return
    await state.clear()
    await cb.answer()
    await finish_sale(cb.message, data, data["pending"], edit=True)


@dp.callback_query(F.data.startswith("unsell:"))
async def cb_unsell_part(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or part["sold_price"] is None:
        await cb.answer("Деталь не в истории", show_alert=True)
        return
    unsell_part(part_id)
    part_id = release_part(part_id)  # вернулась на склад: сливается с такими же
    await cb.answer("Продажа отменена, деталь вернулась в склад")
    await show_card(cb.message, part_id, edit=True)


# --- отделить часть позиции -------------------------------------------------------
@dp.callback_query(F.data.startswith("split:"))
async def cb_split(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    part_id = int(cb.data.split(":")[1])
    part = await sellable_part(cb, part_id)
    if part is None:
        return
    count = part_count(part)
    if count < 2:
        await cb.answer("Здесь всего 1 шт., отделять нечего", show_alert=True)
        return
    buttons = [btn(str(n), f"splitq:{part_id}:{n}") for n in range(1, min(count - 1, 12) + 1)]
    rows = chunk(buttons, 4)
    rows.append([btn("⬅️ Назад", f"view:{part_id}")])
    await show(cb.message,
               f"✂️ <b>Отделить: {esc(str(part['data'].get('name', '—')))}</b>\nНа складе: {count} шт.\n\n"
               "Сколько штук вынести в отдельную запись?\n"
               "<i>Цена поделится по штукам. Пригодится, например, если у одной штуки "
               "другое состояние или заметки.</i>",
               kb(rows), True)
    await cb.answer()


@dp.callback_query(F.data.startswith("splitq:"))
async def cb_split_quantity(cb: CallbackQuery):
    _, part_id, take = cb.data.split(":")
    part = await sellable_part(cb, int(part_id))
    if part is None:
        return
    new_id = split_part(int(part_id), int(take))
    if new_id is None:
        await cb.answer("Не получилось отделить: количество изменилось", show_alert=True)
        return
    await cb.answer(f"Отделено {take} шт. Это новая запись")
    await show_card(cb.message, new_id, edit=True)


# --- дата поступления -------------------------------------------------------------
@dp.callback_query(F.data.startswith("adate:"))
async def cb_added_date(cb: CallbackQuery, state: FSMContext):
    part_id = int(cb.data.split(":")[1])
    part = await sellable_part(cb, part_id)
    if part is None:
        return
    today = now_dt().date()
    current = day_text(part["added_at"]) if part["added_at"] else "не указана"
    await state.set_state(DateForm.waiting)
    await state.update_data(part_id=part_id, prompt_id=cb.message.message_id)
    await show(
        cb.message,
        f"📅 <b>Дата поступления</b> · {esc(str(part['data'].get('name', '—')))}\n"
        f"Сейчас: {current}\n\n"
        "Выбери кнопкой или напиши дату, например <b>25.11.2026</b> или <b>25.11</b>.",
        kb([
            [btn("Сегодня", f"adset:{part_id}:{today.isoformat()}"),
             btn("Вчера", f"adset:{part_id}:{(today - timedelta(days=1)).isoformat()}")],
            [btn("🧹 Не указывать", f"adset:{part_id}:none")],
            [btn("⬅️ Назад", f"view:{part_id}")],
        ]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("adset:"))
async def cb_added_date_set(cb: CallbackQuery, state: FSMContext):
    _, part_id, day = cb.data.split(":")
    part = await sellable_part(cb, int(part_id))
    if part is None:
        return
    await state.clear()
    set_added_at(int(part_id), None if day == "none" else day)
    await cb.answer("Сохранено")
    await show_card(cb.message, int(part_id), edit=True)


@dp.message(DateForm.waiting, F.text, ~F.text.startswith("/"))
async def on_added_date(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    day = parse_day(message.text)
    if day is None or day.year < 2000:
        await message.answer("Не понял дату 🤔 Напиши так: 25.11.2026 или 25.11")
        return
    if day > now_dt().date():
        await message.answer("Эта дата ещё не наступила. Напиши другую.")
        return
    part = get_part(data["part_id"])
    if part is None or part["build_id"] or part["sold_price"] is not None:
        await state.clear()
        await message.answer("Деталь уже в сборке, продана или удалена.")
        return
    set_added_at(data["part_id"], day.isoformat())
    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    await state.clear()
    await show_card(message, data["part_id"], edit=False)


@dp.callback_query(F.data.startswith("bunsell:"))
async def cb_unsell_build(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None or build["status"] != "sold":
        await cb.answer("Сборка не в истории", show_alert=True)
        return
    unsell_build(build_id)
    await cb.answer("Продажа отменена, сборка снова в разделе сборок")
    await show_build(cb.message, build_id, edit=True)


# --- история -----------------------------------------------------------------
@dp.callback_query(F.data == "hist")
async def cb_history(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb.message, "📜 <b>История и статистика</b>\n\nЧто показать?", history_menu(), True)
    await cb.answer()


@dp.callback_query(F.data == "hp")
async def cb_history_parts(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_history_parts(cb.message, edit=True)
    await cb.answer()


@dp.callback_query(F.data == "hb")
async def cb_history_builds(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_history_builds(cb.message, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("hdel:"))
async def cb_history_delete(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or part["sold_price"] is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    name = esc(str(part["data"].get("name", "—")))
    await show(
        cb.message,
        f"Стереть «{name}» из истории?\nДеталь исчезнет совсем, и из итогов тоже. Это нельзя отменить.",
        kb([[btn("🗑 Да, стереть", f"hdelok:{part_id}"), btn("Отмена", f"view:{part_id}")]]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("hdelok:"))
async def cb_history_delete_ok(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or part["sold_price"] is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    delete_part(part_id)
    await cb.answer("Стёрто")
    await show_history_parts(cb.message, edit=True)


@dp.callback_query(F.data.startswith("bhdel:"))
async def cb_history_build_delete(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None or build["status"] != "sold":
        await cb.answer("Не найдено", show_alert=True)
        return
    count = len(build_parts(build_id))
    await show(
        cb.message,
        f"Стереть «{esc(build['name'])}» из истории?\n"
        f"Сборка и все её детали ({count} шт.) исчезнут совсем, и из итогов тоже. "
        "Это нельзя отменить.",
        kb([[btn("🗑 Да, стереть", f"bhdelok:{build_id}"), btn("Отмена", f"bld:{build_id}")]]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("bhdelok:"))
async def cb_history_build_delete_ok(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None or build["status"] != "sold":
        await cb.answer("Не найдено", show_alert=True)
        return
    delete_sold_build(build_id)
    await cb.answer("Стёрто")
    await show_history_builds(cb.message, edit=True)


# --- прочие расходы сборки ---------------------------------------------------
async def expense_prompt(message, state, build_id, text, edit):
    """Задаёт вопрос про расход; запоминает id сообщения, чтобы потом убрать у него кнопки."""
    markup = kb([[btn("❌ Отмена", f"bexp:{build_id}")]])
    if edit:
        await show(message, text, markup, True)
        prompt_id = message.message_id
    else:
        sent = await message.answer(text, reply_markup=markup)
        prompt_id = sent.message_id
    await state.update_data(prompt_id=prompt_id)


async def open_expense_build(cb, build_id):
    """Сборка должна существовать и не быть проданной, иначе False."""
    build = get_build(build_id)
    if build is None:
        await cb.answer("Сборка не найдена", show_alert=True)
        return False
    if await build_sold_alert(cb, build):
        return False
    return True


@dp.callback_query(F.data.startswith("bexp:"))
async def cb_expenses(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    build_id = int(cb.data.split(":")[1])
    if not await open_expense_build(cb, build_id):
        return
    await show_expenses(cb.message, build_id, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("bexpadd:"))
async def cb_expense_add_preset(cb: CallbackQuery, state: FSMContext):
    _, build_id, idx = cb.data.split(":")
    build_id, idx = int(build_id), int(idx)
    if idx >= len(EXPENSE_PRESETS) or not await open_expense_build(cb, build_id):
        return
    label = EXPENSE_PRESETS[idx]
    await state.set_state(ExpenseForm.waiting)
    await state.update_data(mode="preset", build_id=build_id, label=label)
    await expense_prompt(
        cb.message, state, build_id,
        f"💸 <b>{esc(label)}</b>\nНапиши сумму числом. Она прибавится к тому, что уже записано.\n"
        "<i>Например: 10</i>", edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("bexpoth:"))
async def cb_expense_add_other(cb: CallbackQuery, state: FSMContext):
    build_id = int(cb.data.split(":")[1])
    if not await open_expense_build(cb, build_id):
        return
    await state.set_state(ExpenseForm.waiting)
    await state.update_data(mode="other_name", build_id=build_id)
    await expense_prompt(cb.message, state, build_id,
                         "💸 <b>Другой расход</b>\nНапиши название.\n<i>Например: переходник, пыльники</i>",
                         edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("bexpitem:"))
async def cb_expense_item(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    exp = get_expense(int(cb.data.split(":")[1]))
    if exp is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if not await open_expense_build(cb, exp["build_id"]):
        return
    await show(
        cb.message,
        f"💸 <b>{esc(exp['label'])}</b>\nСумма: {money_text(exp['amount'])}",
        kb([
            [btn("✏️ Изменить сумму", f"bexped:{exp['id']}")],
            [btn("🗑 Удалить", f"bexpdel:{exp['id']}")],
            [btn("⬅️ Назад", f"bexp:{exp['build_id']}")],
        ]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("bexped:"))
async def cb_expense_edit(cb: CallbackQuery, state: FSMContext):
    exp = get_expense(int(cb.data.split(":")[1]))
    if exp is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if not await open_expense_build(cb, exp["build_id"]):
        return
    await state.set_state(ExpenseForm.waiting)
    await state.update_data(mode="edit", build_id=exp["build_id"], exp_id=exp["id"])
    await expense_prompt(
        cb.message, state, exp["build_id"],
        f"✏️ <b>{esc(exp['label'])}</b>\nСейчас: {money_text(exp['amount'])}\n"
        "Напиши новую сумму (она заменит старую).", edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("bexpdel:"))
async def cb_expense_delete(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    exp = get_expense(int(cb.data.split(":")[1]))
    if exp is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if not await open_expense_build(cb, exp["build_id"]):
        return
    delete_expense(exp["id"])
    await cb.answer("Удалено")
    await show_expenses(cb.message, exp["build_id"], edit=True)


@dp.message(ExpenseForm.waiting, F.text, ~F.text.startswith("/"))
async def on_expense_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    build = get_build(data["build_id"])
    if build is None or build["status"] == "sold":
        await state.clear()
        await message.answer("Сборка уже недоступна для изменений.")
        return
    mode = data["mode"]
    if mode == "other_name":
        label = message.text.strip()[:60]
        if not label:
            await message.answer("Название пустое, напиши ещё раз.")
            return
        await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
        await state.update_data(mode="other_amount", label=label)
        await expense_prompt(message, state, data["build_id"],
                             f"💸 <b>{esc(label)}</b>\nТеперь сумма числом.\n<i>Например: 15</i>", edit=False)
        return
    amount = parse_value(EXPENSE_FIELD, "number", message.text)
    if amount is None:
        await message.answer("Не понял сумму 🤔 Напиши число больше нуля, например 10.")
        return
    if mode == "preset":
        add_expense(data["build_id"], data["label"], amount, accumulate=True)
    elif mode == "other_amount":
        add_expense(data["build_id"], data["label"], amount, accumulate=False)
    elif mode == "edit":
        if get_expense(data["exp_id"]) is None:
            await state.clear()
            await message.answer("Эта запись уже удалена.")
            return
        set_expense_amount(data["exp_id"], amount)
    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    await state.clear()
    await show_expenses(message, data["build_id"], edit=False)


# --- авто-расходы на каждую сборку --------------------------------------------
@dp.callback_query(F.data == "bover")
async def cb_overhead(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OverheadForm.waiting)
    await state.update_data(prompt_id=cb.message.message_id)
    await show(
        cb.message,
        "⚙️ <b>Авто-расходы на сборку</b>\n"
        f"Сейчас: {money_text(get_setting_number('overhead', 0.0))}\n\n"
        "Эта сумма автоматически прибавляется к себестоимости каждой сборки: термопаста, хомуты "
        "и прочие мелочи, которые не хочется записывать каждый раз.\n"
        "Напиши сумму числом. <b>0</b> отключает авто-расходы.\n"
        "<i>В уже проданных сборках сумма не меняется.</i>",
        kb([[btn("❌ Отмена", "builds")]]),
        True,
    )
    await cb.answer()


@dp.message(OverheadForm.waiting, F.text, ~F.text.startswith("/"))
async def on_overhead(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    value = parse_value(OVERHEAD_FIELD, "number", message.text)
    if value is None:
        await message.answer("Не понял сумму 🤔 Напиши число (0 тоже можно).")
        return
    set_setting("overhead", value)
    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    await state.clear()
    await show_builds(message, edit=False)


# --- статистика ----------------------------------------------------------------
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


def today_local():
    return now_dt().date()


def month_bounds(year, month):
    start = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, nxt - timedelta(days=1)


def week_bounds(monday):
    return monday, monday + timedelta(days=6)


def sales_in(start, end):
    """Продажи за период (включительно): сборки и отдельные детали."""
    a, b = start.isoformat(), end.isoformat()
    with closing(db()) as conn:
        builds = conn.execute(
            "SELECT cost, sold_price FROM builds WHERE status = 'sold' "
            "AND substr(sold_at, 1, 10) BETWEEN ? AND ?", (a, b)).fetchall()
        parts = conn.execute(
            "SELECT data, sold_price FROM parts WHERE sold_price IS NOT NULL AND build_id IS NULL "
            "AND substr(sold_at, 1, 10) BETWEEN ? AND ?", (a, b)).fetchall()
    b_items = [((r["cost"] or 0.0), (r["sold_price"] or 0.0)) for r in builds]
    p_items = []
    for r in parts:
        price = json.loads(r["data"]).get("price")
        p_items.append((price if isinstance(price, (int, float)) else 0.0, r["sold_price"] or 0.0))
    return b_items, p_items


def first_sale_day():
    with closing(db()) as conn:
        r1 = conn.execute("SELECT MIN(substr(sold_at, 1, 10)) FROM builds WHERE status = 'sold'").fetchone()[0]
        r2 = conn.execute("SELECT MIN(substr(sold_at, 1, 10)) FROM parts "
                          "WHERE sold_price IS NOT NULL AND build_id IS NULL").fetchone()[0]
    days = [d for d in (r1, r2) if d]
    return date.fromisoformat(min(days)) if days else None


def stat_block(title, items):
    cost = sum(c for c, _ in items)
    revenue = sum(s for _, s in items)
    return [
        f"{title}: продано {len(items)}",
        f"   Выручка {money_text(revenue)} · себестоимость {money_text(cost)} · "
        f"прибыль <b>{profit_text(revenue - cost)}</b>",
    ]


def period_title(mode, start, end):
    if mode == "m":
        return f"{MONTHS[start.month - 1].capitalize()} {start.year}"
    if mode == "w":
        if start.month == end.month:
            return f"{start.day}–{end.day} {MONTHS_GEN[end.month - 1]} {end.year}"
        return f"{start.day} {MONTHS_GEN[start.month - 1]} – {end.day} {MONTHS_GEN[end.month - 1]} {end.year}"
    return "всё время"


async def show_stats(message, mode, anchor, edit):
    """mode: m (месяц), w (неделя), a (всё время). anchor: дата внутри периода."""
    first = first_sale_day()
    today = today_local()
    if mode == "m":
        start, end = month_bounds(anchor.year, anchor.month)
    elif mode == "w":
        start, end = week_bounds(anchor - timedelta(days=anchor.weekday()))
    else:
        start, end = (first or today), today
    b_items, p_items = sales_in(start, end)
    all_items = b_items + p_items

    lines = [f"📊 <b>Статистика · {period_title(mode, start, end)}</b>", ""]
    if first is None:
        lines.append("Продаж пока нет. Как только продашь деталь или сборку, они появятся здесь.")
    elif not all_items:
        lines.append("За этот период продаж нет.")
    else:
        lines += stat_block("🛠 Сборки", b_items)
        lines += stat_block("🧩 Комплектующие по отдельности", p_items)
        lines.append("")
        cost = sum(c for c, _ in all_items)
        revenue = sum(s for _, s in all_items)
        lines += [
            "<b>Итого</b>",
            f"Выручка: {money_text(revenue)}",
            f"Себестоимость: {money_text(cost)}",
            f"Прибыль: <b>{profit_text(revenue - cost)}</b>",
        ]

    rows = []
    if mode in ("m", "w"):
        nav = []
        if mode == "m":
            prev_anchor = (start - timedelta(days=1))
            next_anchor = end + timedelta(days=1)
        else:
            prev_anchor = start - timedelta(days=7)
            next_anchor = start + timedelta(days=7)
        if first and start > first:
            nav.append(btn("◀", f"st:{mode}:{prev_anchor.isoformat()}"))
        if next_anchor <= today:
            nav.append(btn("▶", f"st:{mode}:{next_anchor.isoformat()}"))
        if nav:
            rows.append(nav)
    switches = []
    if mode != "m":
        switches.append(btn("📅 Месяц", f"st:m:{today.isoformat()}"))
    if mode != "w":
        switches.append(btn("🗓 Неделя", f"st:w:{today.isoformat()}"))
    if mode != "a":
        switches.append(btn("∞ Всё время", f"st:a:{today.isoformat()}"))
    rows.append(switches)
    rows.append([btn("⬅️ Назад", "hist")])
    await show(message, "\n".join(lines), kb(rows), edit)


@dp.callback_query(F.data == "st")
async def cb_stats_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_stats(cb.message, "m", today_local(), edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("st:"))
async def cb_stats(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        _, mode, anchor = cb.data.split(":")
        anchor = date.fromisoformat(anchor)
        if mode not in ("m", "w", "a"):
            raise ValueError
    except ValueError:
        await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)
        return
    await show_stats(cb.message, mode, anchor, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cat_key = cb.data.split(":")[1]
    if cat_key not in CATEGORIES:
        await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)
        return
    await show_category(cb.message, cat_key, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("view:"))
async def cb_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_card(cb.message, int(cb.data.split(":")[1]), edit=True)
    await cb.answer()


# --- удаление --------------------------------------------------------------
@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if await part_in_build_alert(cb, part):
        return
    name = esc(str(part["data"].get("name", "—")))
    await show(
        cb.message,
        f"Удалить «{name}»?\nЭто действие нельзя отменить.",
        kb([[btn("🗑 Да, убрать", f"delok:{part_id}"), btn("Отмена", f"view:{part_id}")]]),
        True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("delok:"))
async def cb_delete_ok(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if await part_in_build_alert(cb, part):
        return
    delete_part(part_id)
    await cb.answer("Удалено")
    if part and part["cat"] in CATEGORIES:
        await show_category(cb.message, part["cat"], edit=True)
    else:
        await show(cb.message, PARTS_TEXT, parts_menu(), True)


# --- добавление и изменение ---------------------------------------------------
@dp.callback_query(F.data.startswith("add:"))
async def cb_add(cb: CallbackQuery, state: FSMContext):
    cat_key = cb.data.split(":")[1]
    if cat_key not in CATEGORIES:
        await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)
        return
    await state.set_state(Form.waiting)
    await state.update_data(cat=cat_key, idx=0, values={}, single=False, part_id=None, other=False)
    await ask(cb.message, state, edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or part["cat"] not in CATEGORIES:
        await cb.answer("Не найдено", show_alert=True)
        return
    if await part_in_build_alert(cb, part):
        return
    rows = []
    for i, fld in enumerate(CATEGORIES[part["cat"]]["fields"]):
        if not field_active(fld, part["data"]):
            continue
        value = plain(fld, part["data"].get(fld["key"]), part["data"])
        rows.append([btn(f"{fld['label']}: {value}", f"ef:{part_id}:{i}")])
    rows.append([btn("⬅️ Назад", f"view:{part_id}")])
    await show(cb.message, "✏️ <b>Что изменить?</b>", kb(rows), True)
    await cb.answer()


@dp.callback_query(F.data.startswith("ef:"))
async def cb_edit_field(cb: CallbackQuery, state: FSMContext):
    _, part_id, idx = cb.data.split(":")
    part = get_part(int(part_id))
    if part is None or part["cat"] not in CATEGORIES:
        await cb.answer("Не найдено", show_alert=True)
        return
    if await part_in_build_alert(cb, part):
        return
    await state.set_state(Form.waiting)
    await state.update_data(
        cat=part["cat"], idx=int(idx), values=dict(part["data"]),
        single=True, part_id=int(part_id), other=False,
    )
    await ask(cb.message, state, edit=True)
    await cb.answer()


# --- ответы внутри диалога ----------------------------------------------------
@dp.callback_query(Form.waiting, F.data.startswith("opt:"))
async def cb_option(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fld = CATEGORIES[data["cat"]]["fields"][data["idx"]]
    _, key, arg = cb.data.split(":")
    # Кнопка должна относиться к текущему вопросу, иначе она устарела
    if key != fld["key"] or cb.message.message_id != data.get("prompt_id"):
        await cb.answer("Эта кнопка устарела")
        return
    if arg == "other":
        await state.update_data(other=True)
        await ask(cb.message, state, edit=True)
        await cb.answer()
        return
    value = fld_options(fld, data["values"])[int(arg)]
    if fld["numeric"]:
        value = float(value)
        value = int(value) if value == int(value) else value
    await cb.answer()
    await accept_value(cb.message, state, value, via_callback=True)


@dp.callback_query(Form.waiting, F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fld = CATEGORIES[data["cat"]]["fields"][data["idx"]]
    key = cb.data.split(":")[1]
    if key != fld["key"] or cb.message.message_id != data.get("prompt_id"):
        await cb.answer("Эта кнопка устарела")
        return
    await cb.answer()
    await accept_value(cb.message, state, None, via_callback=True)


@dp.message(Form.waiting, F.text, ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    fld = CATEGORIES[data["cat"]]["fields"][data["idx"]]
    kind = fld["kind"]
    if kind == "choice":
        if not data.get("other"):
            await message.answer("Выбери вариант кнопкой в сообщении выше 👆")
            return
        if fld["numeric"]:
            kind = "int" if fld["integer"] else "number"
        else:
            kind = "text"

    value = parse_value(fld, kind, message.text)
    if value is None:
        await message.answer("Не понял значение 🤔 Попробуй ещё раз.")
        return
    if fld["check"]:
        error = fld["check"](value, data["values"])
        if error:
            await message.answer(error + " Попробуй ещё раз.")
            return

    await remove_keyboard(bot, message.chat.id, data.get("prompt_id"))
    await accept_value(message, state, value, via_callback=False)


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await cb.answer("Отменено")
    if data.get("part_id"):
        await show_card(cb.message, data["part_id"], edit=True)
    elif data.get("cat"):
        await show_category(cb.message, data["cat"], edit=True)
    else:
        await show(cb.message, HOME_TEXT, main_menu(), True)


# ---------------------------------------------------------------------------
# РЕЗЕРВНАЯ КОПИЯ: файл базы присылается в Telegram и восстанавливается из него
# ---------------------------------------------------------------------------
def db_snapshot():
    """Целостная копия базы (даже если в неё сейчас пишут) в виде байтов."""
    tmp = DATA_DIR / "warehouse.snapshot.db"
    with closing(db()) as src, closing(sqlite3.connect(tmp)) as dst:
        src.backup(dst)
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data


async def send_backup(bot, chat_id, caption):
    name = f"warehouse_{now_dt().strftime('%Y-%m-%d_%H-%M')}.db"
    await bot.send_document(chat_id, BufferedInputFile(db_snapshot(), filename=name), caption=caption)


BACKUP_TEXT = (
    "💾 <b>Резервная копия</b>\n\n"
    "Вся база (детали, сборки, история, настройки) — один файл.\n"
    f"Бот сам присылает его владельцу каждый день в {BACKUP_HOUR}:00.\n\n"
    "<b>Восстановить:</b> пришли сюда файл <code>.db</code> из копии, а в подписи к файлу "
    "напиши <code>/restore</code>. Текущая база перед заменой сохранится рядом."
)


@dp.callback_query(F.data == "bk")
async def cb_backup_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb.message, BACKUP_TEXT,
               kb([[btn("📤 Прислать копию сейчас", "bksend")], [btn("⬅️ Назад", "home")]]), True)
    await cb.answer()


@dp.callback_query(F.data == "bksend")
async def cb_backup_send(cb: CallbackQuery, bot: Bot):
    await cb.answer("Готовлю файл…")
    await send_backup(bot, cb.message.chat.id, "💾 Резервная копия базы")


@dp.message(Command("backup"))
async def cmd_backup(message: Message, bot: Bot):
    await send_backup(bot, message.chat.id, "💾 Резервная копия базы")


@dp.message(F.document, F.caption, F.caption.startswith("/restore"))
async def on_restore(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not OWNER_ID or message.from_user.id != OWNER_ID:
        await message.answer("Восстановить базу может только владелец бота (первый id в OWNER_ID).")
        return
    if message.document.file_size and message.document.file_size > 50 * 1024 * 1024:
        await message.answer("Файл слишком большой для базы склада.")
        return
    incoming = DATA_DIR / "warehouse.incoming.db"
    await bot.download(message.document, destination=incoming)
    try:
        with closing(sqlite3.connect(incoming)) as test:
            tables = {r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            parts = test.execute("SELECT COUNT(*) FROM parts").fetchone()[0] if "parts" in tables else None
    except sqlite3.DatabaseError:
        tables, parts = set(), None
    if parts is None or "builds" not in tables:
        incoming.unlink(missing_ok=True)
        await message.answer("Это не похоже на базу склада 🤔 Пришли файл .db из резервной копии.")
        return
    keep = DATA_DIR / f"warehouse.before-restore_{now_dt().strftime('%Y-%m-%d_%H-%M')}.db"
    keep.write_bytes(db_snapshot())
    with closing(sqlite3.connect(incoming)) as src, closing(db()) as dst:
        src.backup(dst)
    incoming.unlink(missing_ok=True)
    init_db()  # если копия старой версии, добавит новые колонки
    _ICON_CACHE.clear()
    await message.answer(f"✅ База восстановлена: деталей в ней {parts}.\n"
                         f"Прежняя база сохранена в файл {keep.name}.")
    await message.answer(HOME_TEXT, reply_markup=main_menu())


async def daily_backup(bot):
    """Раз в день присылает владельцу файл базы. Не дублирует после перезапуска."""
    while True:
        try:
            now = now_dt()
            today = now.date().isoformat()
            if OWNER_ID and now.hour >= BACKUP_HOUR and get_setting_text("last_backup") != today:
                await send_backup(bot, OWNER_ID, f"💾 Ежедневная копия базы · {day_text(today)}")
                set_setting("last_backup", today)
        except Exception:
            logging.exception("Не получилось отправить ежедневную копию")
        await asyncio.sleep(600)


# Последний обработчик: ловит старые кнопки, которые уже ничего не делают
@dp.callback_query()
async def cb_unknown(cb: CallbackQuery):
    await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)


# ---------------------------------------------------------------------------
def ensure_single_instance():
    """Занимает локальный порт. Если он занят, значит бот уже запущен в другом окне."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        raise SystemExit(
            "Бот уже запущен в другом окне консоли! Две копии бота мешают друг другу.\n"
            "Закрой все окна с ботом (или завершите python.exe в диспетчере задач) "
            "и запусти заново."
        )
    return sock


async def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Открой файл .env рядом с main.py и впиши токен после BOT_TOKEN=")
    # Логи в обычный вывод: Railway красит всё из потока ошибок в красный, даже INFO
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(ALLOWED_IDS) > 1:
        print(f"Доступ: {len(ALLOWED_IDS)} человек, владелец — первый id в OWNER_ID")
    lock = ensure_single_instance()  # переменную не удалять: порт держится, пока она жива
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    if not OWNER_ID:
        print("ВНИМАНИЕ: OWNER_ID не задан, бот отвечает всем, кто его найдёт.")
    print(f"База: {DB_PATH}")
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    backup_task = asyncio.create_task(daily_backup(bot))
    print("Бот запущен. Остановить: Ctrl+C")
    try:
        await dp.start_polling(bot)
    finally:
        backup_task.cancel()
        lock.close()


if __name__ == "__main__":
    asyncio.run(main())
