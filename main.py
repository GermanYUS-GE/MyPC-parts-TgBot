"""Telegram-бот: склад компьютерных комплектующих.

Запуск (Windows, cmd, КАЖДУЮ команду отдельно):
    cd папка_с_ботом
    set BOT_TOKEN=токен_от_BotFather
    set OWNER_ID=твой_telegram_id        (необязательно, но желательно)
    python main.py
"""
import asyncio
import html
import json
import os
import socket
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
# Если задать OWNER_ID, бот будет отвечать только тебе (иначе любой, кто найдёт бота,
# сможет менять твой склад). Свой id можно узнать у бота @userinfobot.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# База данных лежит рядом с main.py
DB_PATH = Path(__file__).with_name("warehouse.db")

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
          options_fn=None, check=None, hint_fn=None, then=None):
    """options_fn(values) -> список вариантов, зависящий от уже введённого.
    check(value, values) -> текст ошибки или None. then: зависимые поля,
    которые нужно перепроверить, если это поле изменили."""
    return {
        "key": key, "label": label, "kind": kind, "options": options or [],
        "unit": unit, "optional": optional, "hint": hint, "numeric": numeric,
        "min": minimum, "max": maximum, "integer": integer,
        "options_fn": options_fn, "check": check, "hint_fn": hint_fn, "then": then or [],
    }


def price_field():
    return field("price", "Цена", "number", unit=CURRENCY, minimum=0,
                 hint="Сколько стоила эта позиция целиком (если ничего, напиши 0)")


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
            price_field(),
            field("notes", "Особенности работы", optional=True,
                  hint="Например: перегрев, нужно заменить термопасту"),
        ],
    },
}

# Группы: категория с "parent" показывается не в общем меню, а внутри группы
GROUPS = {
    "storage": {"icon": "🗄", "title": "Накопители", "items": ["ssd", "hdd"]},
}

# Порядок кнопок в меню комплектующих ("group:..." открывает подменю)
MENU = ["mb", "cpu", "ram", "group:storage", "gpu"]

# Категории, у которых в карточке считается общий объём (количество × объём)
TOTAL_CATS = ("ram", "ssd", "hdd")

# Сборки: порядок деталей. Материнка и процессор в сборке только по одной.
BUILD_ORDER = ["mb", "cpu", "ram", "ssd", "hdd", "gpu"]
SINGLE_SLOTS = ("mb", "cpu")


# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (SQLite). Одна таблица: у каждой детали есть id, категория
# и JSON с её характеристиками. Новые поля можно добавлять без переделки базы.
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


PART_COLS = "id, category, data, build_id"


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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS builds ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'draft')"
        )


def add_part(category, data):
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO parts (category, data) VALUES (?, ?)",
            (category, json.dumps(data, ensure_ascii=False)),
        )
        return cur.lastrowid


def _row_to_part(row):
    return {"id": row["id"], "cat": row["category"], "data": json.loads(row["data"]),
            "build_id": row["build_id"]}


def get_part(part_id):
    with closing(db()) as conn:
        row = conn.execute(f"SELECT {PART_COLS} FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _row_to_part(row) if row else None


def list_parts(category, free_only=True):
    sql = f"SELECT {PART_COLS} FROM parts WHERE category = ?"
    if free_only:
        sql += " AND build_id IS NULL"
    with closing(db()) as conn:
        rows = conn.execute(sql, (category,)).fetchall()
    parts = [_row_to_part(r) for r in rows]
    parts.sort(key=lambda p: str(p["data"].get("name", "")).lower())
    return parts


def count_in_builds(category):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM parts WHERE category = ? AND build_id IS NOT NULL", (category,)
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
        cur = conn.execute("INSERT INTO builds (name, status) VALUES ('', 'draft')")
        build_id = cur.lastrowid
        conn.execute("UPDATE builds SET name = ? WHERE id = ?", (f"Сборка №{build_id}", build_id))
        return build_id


def get_build(build_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT id, name, status FROM builds WHERE id = ?", (build_id,)).fetchone()
    return dict(row) if row else None


def list_builds():
    with closing(db()) as conn:
        rows = conn.execute("SELECT id, name, status FROM builds ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def rename_build(build_id, name):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE builds SET name = ? WHERE id = ?", (name, build_id))


def set_build_status(build_id, status):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE builds SET status = ? WHERE id = ?", (status, build_id))


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
            "UPDATE parts SET build_id = ? WHERE id = ? AND build_id IS NULL", (build_id, part_id)
        )
        return cur.rowcount == 1


def release_part(part_id):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE parts SET build_id = NULL WHERE id = ?", (part_id,))


def delete_build(build_id):
    """Удаляет сборку, а все её детали возвращаются на склад."""
    with closing(db()) as conn, conn:
        conn.execute("UPDATE parts SET build_id = NULL WHERE build_id = ?", (build_id,))
        conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))


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


def plain(fld, value):
    """Значение для показа без HTML: '3.4 ГГц', 'DDR4' или '—'."""
    if value is None or value == "":
        return "—"
    text = str(value)
    if fld["unit"]:
        text += " " + fld["unit"]
    return text


def fmt(fld, value):
    return esc(plain(fld, value))


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
            bits.append(plain(get_field(part["cat"], key), value))
    if with_price and part_price(part) is not None:
        bits.append(money_text(part_price(part)))
    name = str(part["data"].get("name", "—"))
    return name + (" · " + " · ".join(bits) if bits else "")


def render_card(part):
    cat = CATEGORIES[part["cat"]]
    data = part["data"]
    lines = [f"{cat['icon']} <b>{esc(str(data.get('name', '—')))}</b>", f"<i>{cat['title']}</i>", ""]
    for fld in cat["fields"][1:]:  # первое поле (название) уже в заголовке
        lines.append(f"{fld['label']}: {fmt(fld, data.get(fld['key']))}")
    count, size = data.get("count"), data.get("size")
    if part["cat"] in TOTAL_CATS and count and size:
        total = count * size
        total = int(total) if total == int(total) else total
        lines.append(f"Всего: {total} ГБ ({count} × {size} ГБ)")
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


async def show(message, text, markup, edit):
    """edit=True: заменить текст у сообщения бота, иначе отправить новое."""
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise
    else:
        await message.answer(text, reply_markup=markup)


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
HOME_TEXT = "🖥 <b>Склад комплектующих</b>\n\nЧто будем делать?"
PARTS_TEXT = "🧩 <b>Комплектующие</b>\n\nВыбери категорию:"


def main_menu():
    return kb([
        [btn("🧩 Добавить или изменить комплектующие", "parts")],
        [btn("🛠 Добавить или изменить сборки", "builds")],
    ])


def parts_menu():
    rows = []
    for item in MENU:
        if item.startswith("group:"):
            key = item.split(":")[1]
            group = GROUPS[key]
            rows.append([btn(f"{group['icon']} {group['title']}", f"grp:{key}")])
        else:
            cat = CATEGORIES[item]
            rows.append([btn(f"{cat['icon']} {cat['title']}", f"cat:{item}")])
    rows.append([btn("⬅️ Назад", "home")])
    return kb(rows)


def group_menu(group_key):
    group = GROUPS[group_key]
    rows = []
    for cat_key in group["items"]:
        cat = CATEGORIES[cat_key]
        rows.append([btn(f"{cat['icon']} {cat['title']}", f"cat:{cat_key}")])
    rows.append([btn("⬅️ Назад", "parts")])
    return kb(rows)


async def show_category(message, cat_key, edit):
    cat = CATEGORIES[cat_key]
    parts = list_parts(cat_key)
    text = f"{cat['icon']} <b>{cat['plural']}</b>\nВ списке: {len(parts)}"
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
    if part["build_id"]:
        build = get_build(part["build_id"])
        name = esc(build["name"]) if build else "?"
        text = render_card(part) + f"\n\n📦 В сборке: <b>{name}</b>\nПока деталь в сборке, менять её нельзя."
        markup = kb([
            [btn("↩️ Убрать из сборки", f"unbuild:{part_id}")],
            [btn("⬅️ К сборке", f"bld:{part['build_id']}")],
        ])
        await show(message, text, markup, edit)
        return
    rows = []
    if part["cat"] == "mb":
        rows.append([btn("🛠 Начать сборку", f"build:{part_id}")])
    rows += [
        [btn("✏️ Изменить", f"edit:{part_id}")],
        [btn("🗑 Убрать из списка", f"del:{part_id}")],
        [btn("⬅️ К списку", f"cat:{part['cat']}")],
    ]
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
    return warns


async def show_builds(message, edit):
    builds = list_builds()
    text = f"🛠 <b>Сборки</b>\nВсего: {len(builds)}"
    if not builds:
        text += "\n\nПока нет ни одной сборки."
    rows = [[btn("➕ Новая сборка (начать с материнской платы)", "bnew")]]
    for b in builds:
        total = sum(part_price(p) or 0 for p in build_parts(b["id"]))
        icon = "🟢" if b["status"] == "ready" else "🟡"
        rows.append([btn(f"{icon} {b['name']} · {money_text(total)}", f"bld:{b['id']}")])
    rows.append([btn("⬅️ Назад", "home")])
    await show(message, text, kb(rows), edit)


async def show_build(message, build_id, edit):
    build = get_build(build_id)
    if build is None:
        await show(message, "Сборка не найдена (возможно, уже удалена).",
                   kb([[btn("⬅️ К сборкам", "builds")]]), edit)
        return
    parts = build_parts(build_id)
    ready = build["status"] == "ready"
    lines = [f"🛠 <b>{esc(build['name'])}</b>", "🟢 Готова" if ready else "🟡 Черновик", ""]
    by = defaultdict(list)
    for p in parts:
        by[p["cat"]].append(p)

    total, no_price = 0, 0
    for cat_key in BUILD_ORDER:
        cat = CATEGORIES[cat_key]
        for p in by[cat_key]:
            price = part_price(p)
            if price is None:
                no_price += 1
                price_text = "цена не указана"
            else:
                total += price
                price_text = money_text(price)
            lines.append(f"{cat['icon']} {esc(str(p['data'].get('name', '—')))} — {price_text}")
    for cat_key in ("mb", "cpu", "ram", "gpu"):
        if not by[cat_key]:
            cat = CATEGORIES[cat_key]
            lines.append(f"{cat['icon']} {cat['title']}: —")
    if not by["ssd"] and not by["hdd"]:
        lines.append("🗄 Накопитель: —")

    lines += ["", f"💰 <b>Себестоимость: {money_text(total)}</b>"]
    if no_price:
        lines.append(f"<i>У деталей без цены ({no_price} шт.) считается 0</i>")
    for warn in build_warnings(parts):
        lines.append(f"⚠️ {warn}")

    rows = []
    for p in parts:
        cat = CATEGORIES[p["cat"]]
        rows.append([btn(f"{cat['icon']} {p['data'].get('name', '—')}", f"view:{p['id']}")])
    add_buttons = []
    for cat_key in BUILD_ORDER:
        if cat_key in SINGLE_SLOTS and by[cat_key]:
            continue
        cat = CATEGORIES[cat_key]
        add_buttons.append(btn(f"➕ {cat['title']}", f"bpick:{build_id}:{cat_key}"))
    rows += chunk(add_buttons, 2)
    rows.append([btn("✏️ Название сборки", f"bname:{build_id}")])
    if not ready and parts:
        rows.append([btn("✅ Сборка готова", f"bready:{build_id}")])
    rows.append([btn("🗑 Удалить сборку", f"bdel:{build_id}")])
    rows.append([btn("⬅️ К сборкам", "builds")])
    await show(message, "\n".join(lines), kb(rows), edit)


async def show_pick(message, cat_key, build_id=None):
    """Список свободных деталей категории. build_id=None: выбор материнской платы для новой сборки."""
    cat = CATEGORIES[cat_key]
    free = list_parts(cat_key)
    if build_id is None:
        text = "🛠 <b>Новая сборка</b>\nВыбери материнскую плату, с неё всё начнётся:"
        prefix, back = "bnewsel:", "builds"
    else:
        build = get_build(build_id)
        text = f"{cat['icon']} <b>{cat['plural']}</b>\nВыбери деталь в сборку «{esc(build['name'])}»:"
        prefix, back = f"bsel:{build_id}:", f"bld:{build_id}"
    if not free:
        text += f"\n\nСвободных деталей этой категории нет. Добавь их в разделе комплектующих."
    rows = [[btn(part_label(p, with_price=True), f"{prefix}{p['id']}")] for p in free]
    rows.append([btn("⬅️ Назад", back)])
    await show(message, text, kb(rows), True)


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
            head += f"\nСейчас: {fmt(fld, current)}"
    else:
        head = f"{cat['icon']} <b>{cat['add']}</b>\nШаг {data['idx'] + 1} из {len(cat['fields'])}"

    rows = []
    if fld["kind"] == "choice" and not other:
        body = f"Выбери: <b>{fld['label']}</b>"
        if fld["hint_fn"]:
            extra = fld["hint_fn"](data["values"])
            if extra:
                body += f"\n<i>{esc(extra)}</i>"
        buttons = []
        for i, option in enumerate(fld_options(fld, data["values"])):
            label = option + (" " + fld["unit"] if fld["unit"] else "")
            # В кнопке зашито и имя поля: так старая кнопка не попадёт в чужое поле
            buttons.append(btn(label, f"opt:{key}:{i}"))
        rows += chunk(buttons, 3)
        rows.append([btn("✏️ Другое", f"opt:{key}:other")])
    else:
        body = f"Введи: <b>{fld['label']}</b>"
        if other:
            body = f"Введи свой вариант: <b>{fld['label']}</b>"
        if fld["kind"] in ("number", "int") or (fld["kind"] == "choice" and fld["numeric"]):
            body += " (числом)"
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
    if edit:
        await show(message, text, kb(rows), True)
        prompt_id = message.message_id
    else:
        sent = await message.answer(text, reply_markup=kb(rows))
        prompt_id = sent.message_id
    await state.update_data(prompt_id=prompt_id)


async def accept_value(message, state, value, via_callback):
    """Запоминает ответ и переходит к следующему полю или сохраняет всё."""
    data = await state.get_data()
    cat_key = data["cat"]
    fields = CATEGORIES[cat_key]["fields"]
    values = dict(data["values"])
    key = fields[data["idx"]]["key"]

    if value is None:
        values.pop(key, None)
    else:
        values[key] = value

    if data.get("single"):
        update_part(data["part_id"], values)
        # Если изменили поле, от которого зависят другие (ядра -> потоки), перепроверяем их
        for dep_key in fields[data["idx"]]["then"]:
            dep = get_field(cat_key, dep_key)
            current = values.get(dep_key)
            if current is not None and dep["check"] and dep["check"](current, values):
                values.pop(dep_key)
                update_part(data["part_id"], values)
                dep_idx = [f["key"] for f in fields].index(dep_key)
                await state.update_data(values=values, idx=dep_idx, other=False)
                await ask(message, state, edit=via_callback)
                return
        await state.clear()
        await show_card(message, data["part_id"], edit=via_callback)
        return

    next_idx = data["idx"] + 1
    if next_idx >= len(fields):
        part_id = add_part(cat_key, values)
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

if OWNER_ID:
    dp.message.filter(F.from_user.id == OWNER_ID)
    dp.callback_query.filter(F.from_user.id == OWNER_ID)


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


@dp.callback_query(F.data.startswith("grp:"))
async def cb_group(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    group_key = cb.data.split(":")[1]
    if group_key not in GROUPS:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    group = GROUPS[group_key]
    await show(cb.message, f"{group['icon']} <b>{group['title']}</b>\n\nВыбери тип:",
               group_menu(group_key), True)
    await cb.answer()


async def part_in_build_alert(cb, part):
    """True (и сообщение), если деталь лежит в сборке и менять её нельзя."""
    if part and part["build_id"]:
        await cb.answer("Деталь в сборке. Сначала убери её из сборки.", show_alert=True)
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
    await show_pick(cb.message, "mb")
    await cb.answer()


async def start_build_with(cb, part_id):
    part = get_part(part_id)
    if part is None or part["cat"] != "mb":
        await cb.answer("Материнская плата не найдена", show_alert=True)
        return
    if part["build_id"]:
        await cb.answer("Эта плата уже в сборке", show_alert=True)
        return
    build_id = create_build()
    assign_part(part_id, build_id)
    await show_build(cb.message, build_id, edit=True)
    await cb.answer("Сборка создана")


@dp.callback_query(F.data.startswith("bnewsel:"))
async def cb_build_new_select(cb: CallbackQuery):
    await start_build_with(cb, int(cb.data.split(":")[1]))


@dp.callback_query(F.data.startswith("build:"))
async def cb_build_from_card(cb: CallbackQuery):
    await start_build_with(cb, int(cb.data.split(":")[1]))


@dp.callback_query(F.data.startswith("bld:"))
async def cb_build_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_build(cb.message, int(cb.data.split(":")[1]), edit=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("bpick:"))
async def cb_build_pick(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, build_id, cat_key = cb.data.split(":")
    if get_build(int(build_id)) is None or cat_key not in CATEGORIES:
        await cb.answer("Кнопка устарела. Нажми /start", show_alert=True)
        return
    await show_pick(cb.message, cat_key, int(build_id))
    await cb.answer()


@dp.callback_query(F.data.startswith("bsel:"))
async def cb_build_select(cb: CallbackQuery):
    _, build_id, part_id = cb.data.split(":")
    build_id, part_id = int(build_id), int(part_id)
    part = get_part(part_id)
    if get_build(build_id) is None or part is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    if part["build_id"]:
        await cb.answer("Эта деталь уже в сборке", show_alert=True)
        return
    if part["cat"] in SINGLE_SLOTS and any(p["cat"] == part["cat"] for p in build_parts(build_id)):
        await cb.answer(f"В сборке уже есть: {CATEGORIES[part['cat']]['title'].lower()}", show_alert=True)
        return
    assign_part(part_id, build_id)
    await show_build(cb.message, build_id, edit=True)
    await cb.answer("Добавлено")


@dp.callback_query(F.data.startswith("unbuild:"))
async def cb_unbuild(cb: CallbackQuery):
    part_id = int(cb.data.split(":")[1])
    part = get_part(part_id)
    if part is None or not part["build_id"]:
        await cb.answer("Деталь уже не в сборке", show_alert=True)
        return
    build_id = part["build_id"]
    release_part(part_id)
    await show_build(cb.message, build_id, edit=True)
    await cb.answer("Деталь вернулась в комплектующие")


@dp.callback_query(F.data.startswith("bready:"))
async def cb_build_ready(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    if get_build(build_id) is None:
        await cb.answer("Сборка не найдена", show_alert=True)
        return
    set_build_status(build_id, "ready")
    await show_build(cb.message, build_id, edit=True)
    await cb.answer("Сборка готова ✅")


@dp.callback_query(F.data.startswith("bdel:"))
async def cb_build_delete(cb: CallbackQuery):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None:
        await cb.answer("Не найдено", show_alert=True)
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
    delete_build(int(cb.data.split(":")[1]))
    await cb.answer("Сборка удалена, детали вернулись")
    await show_builds(cb.message, edit=True)


@dp.callback_query(F.data.startswith("bname:"))
async def cb_build_name(cb: CallbackQuery, state: FSMContext):
    build_id = int(cb.data.split(":")[1])
    build = get_build(build_id)
    if build is None:
        await cb.answer("Не найдено", show_alert=True)
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
        f"Убрать из списка «{name}»?\nЭто действие нельзя отменить.",
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
        rows.append([btn(f"{fld['label']}: {plain(fld, part['data'].get(fld['key']))}", f"ef:{part_id}:{i}")])
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
        raise SystemExit("Не задан BOT_TOKEN. Выполни: set BOT_TOKEN=твой_токен")
    lock = ensure_single_instance()  # переменную не удалять: порт держится, пока она жива
    init_db()
    if not OWNER_ID:
        print("ВНИМАНИЕ: OWNER_ID не задан, бот отвечает всем, кто его найдёт.")
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    print("Бот запущен. Остановить: Ctrl+C")
    try:
        await dp.start_polling(bot)
    finally:
        lock.close()


if __name__ == "__main__":
    asyncio.run(main())
