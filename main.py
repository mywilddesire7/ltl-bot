import asyncio
import glob
import os
import re
import sqlite3

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "2046978907"))
TZ = ZoneInfo("Europe/Moscow")
DB = "ltl.db"
BACKUP_DIR = "backups"
BACKUPS_KEEP = 7
LINEUP_OPEN_MINUTES = 20
VIP_SEND_BEFORE_MINUTES = 5
REMINDER_MINUTES = 5
MAX_MATCHES_PER_DAY = 5

VIP_LINKS = [
    "https://www.roblox.com/share?code=657ef0c3653a7441921a1f1569d92d2b&type=Server",
    "https://www.roblox.com/share?code=fef5add89a022542808e5f31189e9448&type=Server",
    "https://www.roblox.com/share?code=6717806bff919c4da9739feccd71bec2&type=Server",
]
VIP_LABELS = ["VIP1", "VIP2", "VIP3"]

if not TOKEN:
    raise ValueError("Проверь BOT_TOKEN в .env")

bot = Bot(TOKEN)
dp = Dispatcher()

waiting_start_photo = set()
pending_matches = {}
EXTRA_ADMINS: set[int] = set()


# ============================================================
# УТИЛИТЫ
# ============================================================

def normalize_name(value: str) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in EXTRA_ADMINS


def root_admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_date_input(text: str):
    text = (text or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def format_date_display(db_date: str) -> str:
    if not db_date:
        return "—"
    try:
        return datetime.strptime(db_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return db_date


def match_datetime(match_date: str, match_time: str) -> datetime:
    return datetime.strptime(
        f"{match_date} {match_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)


def lineup_opens_at(match_date: str, match_time: str) -> datetime:
    return match_datetime(match_date, match_time) - timedelta(
        minutes=LINEUP_OPEN_MINUTES
    )


def lineup_is_open(match_date: str, match_time: str) -> bool:
    return datetime.now(TZ) >= lineup_opens_at(match_date, match_time)


def match_has_started(match_date: str, match_time: str) -> bool:
    return datetime.now(TZ) >= match_datetime(match_date, match_time)


def vip_send_time(match_date: str, match_time: str) -> datetime:
    return match_datetime(match_date, match_time) - timedelta(
        minutes=VIP_SEND_BEFORE_MINUTES
    )


def vip_label(link: str) -> str:
    if not link:
        return "—"
    try:
        return VIP_LABELS[VIP_LINKS.index(link)]
    except ValueError:
        return "VIP"


async def get_next_vip(db) -> str:
    row = await fetchone(db, "SELECT value FROM settings WHERE key='vip_counter'")
    counter = int(row[0]) if row else 0
    link = VIP_LINKS[counter % len(VIP_LINKS)]
    await db.execute(
        "INSERT OR REPLACE INTO settings VALUES (?, ?)",
        ("vip_counter", str(counter + 1))
    )
    await db.commit()
    return link


async def fetchone(db, query, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


async def fetchall(db, query, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchall()


async def resolve_player(db, identifier: str):
    identifier = (identifier or "").strip()
    if not identifier:
        return None, None
    if identifier.startswith("@"):
        uname = identifier[1:].strip()
        if not uname:
            return None, None
        row = await fetchone(
            db, "SELECT user_id, username FROM users WHERE LOWER(username)=?",
            (uname.lower(),)
        )
        if not row:
            return None, None
        return row[0], (row[1] or uname)
    try:
        uid = int(identifier)
    except ValueError:
        return None, None
    if uid <= 0:
        return None, None
    row = await fetchone(
        db, "SELECT user_id, username FROM users WHERE user_id=?", (uid,)
    )
    if row:
        return row[0], row[1]
    return uid, None


def parse_schedule_text(text: str):
    """Парсит блок с матчами. Возвращает [(team1, team2, 'HH:MM'), ...]."""
    results = []
    pending = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        time_match = re.search(r"(\d{1,2}):(\d{2})", line)

        if "⚽" in line and not time_match:
            clean = line.replace("⚽", "").replace("\ufe0f", "").strip()
            if "—" in clean:
                parts = clean.split("—", 1)
            elif "–" in clean:
                parts = clean.split("–", 1)
            elif " - " in clean:
                parts = clean.split(" - ", 1)
            else:
                continue
            if len(parts) == 2:
                t1, t2 = parts[0].strip(), parts[1].strip()
                if t1 and t2:
                    pending = (t1, t2)
            continue

        if time_match and pending:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            if 0 <= h <= 23 and 0 <= m <= 59:
                results.append((pending[0], pending[1], f"{h:02d}:{m:02d}"))
            pending = None

    return results


# ============================================================
# СТАТУС ОТПИСИ
# ============================================================

async def team_status(db, match_id, team):
    if not team:
        return False
    row = await fetchone(
        db,
        "SELECT COUNT(*) FROM lineups WHERE match_id=? AND team=?",
        (match_id, team)
    )
    return bool(row and row[0] > 0)


def status_icon(done: bool) -> str:
    return "✅" if done else "⏳"


async def notify_opponent_team(db, match_id, opponent_team, match_name, own_team):
    club = await fetchone(
        db, "SELECT id, name FROM clubs WHERE name_lower=?",
        (normalize_name(opponent_team),)
    )
    if not club:
        return
    members = await fetchall(
        db, "SELECT user_id FROM club_members WHERE club_id=?", (club[0],)
    )
    for (uid,) in members:
        try:
            await bot.send_message(
                uid,
                f"⚽ {match_name}\n\n"
                f"✅ Команда «{own_team}» уже отписалась.\n"
                f"⏳ Ваша команда «{opponent_team}» ещё нет.\n\n"
                f"Не забудьте отправить состав до начала матча!"
            )
        except Exception as e:
            print(f"Notify team error ({uid}):", e)


# ============================================================
# ОЧИСТКА СЕССИЙ (фикс бага с переключением разделов)
# ============================================================

async def clear_tab_session(user_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM tab_sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def clear_lineup_session(user_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM lineup_sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def clear_both_sessions(user_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM tab_sessions WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM lineup_sessions WHERE user_id=?", (user_id,))
        await db.commit()


# ============================================================
# МИДЛВАРЬ
# ============================================================

class UserSaverMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            try:
                async with aiosqlite.connect(DB) as db:
                    await db.execute(
                        """
                        INSERT INTO users (user_id, username, first_name)
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            username=excluded.username,
                            first_name=excluded.first_name
                        """,
                        (user.id, user.username, user.first_name or "")
                    )
                    await db.commit()
            except Exception as e:
                print("UserSaverMiddleware error:", e)
        return await handler(event, data)


dp.message.outer_middleware(UserSaverMiddleware())
dp.callback_query.outer_middleware(UserSaverMiddleware())


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

async def init_db():
    async with aiosqlite.connect(DB) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                vip_link TEXT,
                send_time TEXT,
                date TEXT
            )
        """)

        columns = await fetchall(db, "PRAGMA table_info(matches)")
        existing = [c[1] for c in columns]
        migrations = {
            "sent": "INTEGER DEFAULT 0",
            "photo_id": "TEXT",
            "team1": "TEXT",
            "team2": "TEXT",
            "reminder_sent": "INTEGER DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in existing:
                await db.execute(
                    f"ALTER TABLE matches ADD COLUMN {column} {definition}"
                )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ready (
                user_id INTEGER,
                match_id INTEGER,
                UNIQUE(user_id, match_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tabs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                match_id INTEGER,
                file_id TEXT
            )
        """)

        columns = await fetchall(db, "PRAGMA table_info(tabs)")
        existing = [c[1] for c in columns]
        if "report_id" not in existing:
            await db.execute("ALTER TABLE tabs ADD COLUMN report_id INTEGER")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tab_sessions (
                user_id INTEGER PRIMARY KEY,
                match_id INTEGER,
                score TEXT
            )
        """)

        columns = await fetchall(db, "PRAGMA table_info(tab_sessions)")
        existing = [c[1] for c in columns]
        if "score" not in existing:
            await db.execute("ALTER TABLE tab_sessions ADD COLUMN score TEXT")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS lineup_sessions (
                user_id INTEGER PRIMARY KEY,
                match_id INTEGER,
                team TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS lineups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                match_id INTEGER,
                team TEXT,
                roster_text TEXT,
                photo_id TEXT,
                submitted_at TEXT,
                vip_sent INTEGER DEFAULT 0,
                UNIQUE(user_id, match_id, team)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tab_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                user_id INTEGER,
                username TEXT,
                score TEXT,
                submitted_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS clubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_lower TEXT NOT NULL UNIQUE,
                captain_id INTEGER,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS club_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                club_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                added_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS club_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                club_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                decided_at TEXT,
                decided_by INTEGER
            )
        """)

        await db.commit()


async def load_admins():
    global EXTRA_ADMINS
    async with aiosqlite.connect(DB) as db:
        rows = await fetchall(db, "SELECT user_id FROM admins")
        EXTRA_ADMINS = {r[0] for r in rows}


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 ТАБЫ"), KeyboardButton(text="⚽ ОТПИСЬ МАТЧА")],
            [KeyboardButton(text="📅 РАСПИСАНИЕ"), KeyboardButton(text="📋 СТАТУС МАТЧЕЙ")],
        ],
        resize_keyboard=True
    )


# ============================================================
# /start
# ============================================================

@dp.message(Command("start"))
async def start(message: Message):
    # сбрасываем возможные «зависшие» сессии
    await clear_both_sessions(message.from_user.id)

    async with aiosqlite.connect(DB) as db:
        photo = await fetchone(
            db, "SELECT value FROM settings WHERE key='start_photo'"
        )

    caption = (
        "⚡ LIGHTNING TOUCH LEAGUE\n\n"
        "Кнопки меню:\n"
        "📊 ТАБЫ — счёт и фото табов\n"
        "⚽ ОТПИСЬ МАТЧА — состав на матч\n"
        "📅 РАСПИСАНИЕ — матчи на сегодня\n"
        "📋 СТАТУС МАТЧЕЙ — кто уже отписался\n\n"
        "Команды:\n"
        "/today — расписание на сегодня\n"
        "/day ДД.ММ.ГГГГ — расписание на день\n"
        "/myclub — мой клуб (для капитана)\n"
        "/join Клуб — отправить заявку в клуб"
    )

    if photo:
        await message.answer_photo(
            photo[0], caption=caption, reply_markup=main_menu()
        )
    else:
        await message.answer(caption, reply_markup=main_menu())


@dp.message(Command("setstartphoto"))
async def set_start_photo(message: Message):
    if not admin_only(message.from_user.id):
        return
    waiting_start_photo.add(message.from_user.id)
    await message.answer("Отправьте фотографию главного меню")


# ============================================================
# РАСПИСАНИЕ (кнопка)
# ============================================================

@dp.message(F.text == "📅 РАСПИСАНИЕ")
async def schedule_button(message: Message):
    await clear_both_sessions(message.from_user.id)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    await show_schedule_for_date(message, today)


async def show_schedule_for_date(message: Message, date_db: str):
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, name, date, send_time, team1, team2, vip_link
            FROM matches
            WHERE date = ?
            ORDER BY send_time
            """,
            (date_db,)
        )
        if not matches:
            await message.answer(
                f"📅 На {format_date_display(date_db)} матчей нет"
            )
            return

        await message.answer(
            f"📅 РАСПИСАНИЕ НА {format_date_display(date_db)}\n"
            f"Всего: {len(matches)}"
        )

        for mid, name, date, time, team1, team2, vip_link in matches:
            s1 = status_icon(await team_status(db, mid, team1))
            s2 = status_icon(await team_status(db, mid, team2))

            if match_has_started(date, time):
                window = "🏁 матч начался"
            elif lineup_is_open(date, time):
                window = "🔓 отпись открыта"
            else:
                opens = lineup_opens_at(date, time)
                window = f"🔒 отпись с {opens.strftime('%H:%M')} МСК"

            vip_tag = vip_label(vip_link) if vip_link else "—"

            if match_has_started(date, time):
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="👁 Показать составы",
                        callback_data=f"view_lineups_{mid}"
                    )]
                ])
            else:
                kb = match_actions_kb(mid)

            await message.answer(
                f"⚽ {name}\n"
                f"🕒 {time} МСК | 🎟 {vip_tag}\n"
                f"{window}\n\n"
                f"Отпись: {s1} {team1 or '—'} | {s2} {team2 or '—'}",
                reply_markup=kb
            )


@dp.message(Command("today"))
async def today_matches(message: Message):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    await show_schedule_for_date(message, today)


@dp.message(Command("day"))
async def day_matches(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /day ДД.ММ.ГГГГ\n"
            "Например: /day 14.11.2026"
        )
        return
    date_db = parse_date_input(parts[1].strip())
    if not date_db:
        await message.answer("❌ Неверный формат. Используйте ДД.ММ.ГГГГ")
        return
    await show_schedule_for_date(message, date_db)


# ============================================================
# КЛУБЫ
# ============================================================

@dp.message(Command("addclub"))
async def add_club(message: Message):
    if not admin_only(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /addclub Название клуба")
        return
    name = parts[1].strip()
    name_lower = normalize_name(name)
    if not name_lower:
        await message.answer("Название клуба не может быть пустым")
        return

    async with aiosqlite.connect(DB) as db:
        existing = await fetchone(
            db, "SELECT id, name FROM clubs WHERE name_lower=?", (name_lower,)
        )
        if existing:
            await message.answer(
                f"Клуб '{existing[1]}' уже существует (id={existing[0]})"
            )
            return
        cursor = await db.execute(
            "INSERT INTO clubs (name, name_lower, created_at) VALUES (?, ?, ?)",
            (name, name_lower, now_str())
        )
        await db.commit()
        club_id = cursor.lastrowid

    await message.answer(f"✅ Клуб '{name}' создан (id={club_id})")


@dp.message(Command("clubs"))
async def list_clubs(message: Message):
    if not admin_only(message.from_user.id):
        return
    async with aiosqlite.connect(DB) as db:
        clubs = await fetchall(
            db,
            """
            SELECT c.id, c.name, c.captain_id, u.username
            FROM clubs c
            LEFT JOIN users u ON u.user_id = c.captain_id
            ORDER BY c.name
            """
        )
        if not clubs:
            await message.answer("Клубов нет. Создайте: /addclub Название")
            return
        for cid, name, captain_id, cap_uname in clubs:
            count = await fetchone(
                db, "SELECT COUNT(*) FROM club_members WHERE club_id=?", (cid,)
            )
            if captain_id:
                cap_label = f"@{cap_uname}" if cap_uname else f"ID:{captain_id}"
            else:
                cap_label = "—"
            await message.answer(
                f"#{cid} {name}\nКапитан: {cap_label}\nИгроков: {count[0]}"
            )


@dp.message(Command("setcaptain"))
async def set_captain(message: Message):
    if not admin_only(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование: /setcaptain Название клуба @username|ID"
        )
        return
    identifier = parts[-1]
    club_name = " ".join(parts[1:-1]).strip()
    if not club_name:
        await message.answer(
            "Использование: /setcaptain Название клуба @username|ID"
        )
        return

    async with aiosqlite.connect(DB) as db:
        club = await fetchone(
            db, "SELECT id, name FROM clubs WHERE name_lower=?",
            (normalize_name(club_name),)
        )
        if not club:
            await message.answer(f"Клуб '{club_name}' не найден")
            return

        user_id, uname = await resolve_player(db, identifier)
        if user_id is None:
            await message.answer(
                f"❌ Пользователь {identifier} не найден в базе.\n"
                "Если это @username — сначала пусть откроет бота и нажмёт /start.\n"
                "Либо укажите Telegram ID (число)."
            )
            return

        display = f"@{uname}" if uname else f"ID:{user_id}"

        await db.execute(
            "UPDATE clubs SET captain_id=NULL WHERE captain_id=?", (user_id,)
        )
        await db.execute(
            "DELETE FROM club_members WHERE user_id=?", (user_id,)
        )
        await db.execute(
            "UPDATE clubs SET captain_id=? WHERE id=?", (user_id, club[0])
        )
        await db.execute(
            """
            INSERT INTO club_members (club_id, user_id, username, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (club[0], user_id, uname, now_str())
        )
        await db.commit()

    note = ""
    if not uname:
        note = "\n\n⚠️ Игрок ещё не запускал бота — пусть нажмёт /start."
    await message.answer(
        f"✅ {display} назначен капитаном клуба '{club[1]}' и добавлен в состав{note}"
    )


async def _add_player_impl(message: Message, club_id: int, club_name: str,
                            identifier: str):
    async with aiosqlite.connect(DB) as db:
        player_id, player_username = await resolve_player(db, identifier)
        if player_id is None:
            await message.answer(
                f"❌ Пользователь {identifier} не найден в базе.\n"
                "Если это @username — сначала пусть откроет бота и нажмёт /start.\n"
                "Либо укажите Telegram ID (число)."
            )
            return

        display = f"@{player_username}" if player_username else f"ID:{player_id}"

        existing = await fetchone(
            db,
            """
            SELECT c.name FROM club_members m
            JOIN clubs c ON c.id=m.club_id
            WHERE m.user_id=?
            """,
            (player_id,)
        )
        if existing:
            await message.answer(
                f"❌ {display} уже состоит в клубе '{existing[0]}'"
            )
            return

        await db.execute(
            """
            INSERT INTO club_members (club_id, user_id, username, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (club_id, player_id, player_username, now_str())
        )
        await db.commit()

    note = ""
    if not player_username:
        note = "\n\n⚠️ Игрок ещё не запускал бота — пусть нажмёт /start."
    await message.answer(
        f"✅ {display} добавлен в клуб '{club_name}'{note}"
    )


@dp.message(Command("addplayer"))
async def add_player(message: Message):
    user_id = message.from_user.id
    is_admin = admin_only(user_id)
    parts = message.text.split()

    if is_admin:
        if len(parts) < 3:
            await message.answer(
                "Использование: /addplayer Название клуба @username|ID"
            )
            return
        identifier = parts[-1]
        club_name = " ".join(parts[1:-1]).strip()
        if not club_name:
            await message.answer(
                "Использование: /addplayer Название клуба @username|ID"
            )
            return
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(
                db, "SELECT id, name FROM clubs WHERE name_lower=?",
                (normalize_name(club_name),)
            )
        if not club:
            await message.answer(f"Клуб '{club_name}' не найден")
            return
        await _add_player_impl(message, club[0], club[1], identifier)
        return

    if len(parts) != 2:
        await message.answer("Использование: /addplayer @username|ID")
        return
    identifier = parts[1]
    if not identifier:
        await message.answer("Укажите @username или ID")
        return

    async with aiosqlite.connect(DB) as db:
        club = await fetchone(
            db, "SELECT id, name FROM clubs WHERE captain_id=?", (user_id,)
        )
    if not club:
        await message.answer("Вы не являетесь капитаном клуба")
        return
    await _add_player_impl(message, club[0], club[1], identifier)


async def _remove_player_impl(message: Message, club_id: int, club_name: str,
                               identifier: str):
    async with aiosqlite.connect(DB) as db:
        player_id, player_username = await resolve_player(db, identifier)
        if player_id is None:
            await message.answer(
                f"❌ Пользователь {identifier} не найден.\n"
                "Если это @username — сначала /start. Либо укажите ID."
            )
            return

        display = f"@{player_username}" if player_username else f"ID:{player_id}"

        member = await fetchone(
            db,
            "SELECT id FROM club_members WHERE club_id=? AND user_id=?",
            (club_id, player_id)
        )
        if not member:
            await message.answer(
                f"❌ {display} не состоит в клубе '{club_name}'"
            )
            return

        club_row = await fetchone(
            db, "SELECT captain_id FROM clubs WHERE id=?", (club_id,)
        )
        if club_row and club_row[0] == player_id:
            await message.answer(
                "❌ Нельзя удалить капитана. Сначала назначьте нового."
            )
            return

        await db.execute(
            "DELETE FROM club_members WHERE club_id=? AND user_id=?",
            (club_id, player_id)
        )
        await db.commit()

    await message.answer(
        f"✅ {display} удалён из клуба '{club_name}'"
    )


@dp.message(Command("removeplayer"))
async def remove_player(message: Message):
    user_id = message.from_user.id
    is_admin = admin_only(user_id)
    parts = message.text.split()

    if is_admin:
        if len(parts) < 3:
            await message.answer(
                "Использование: /removeplayer Название клуба @username|ID"
            )
            return
        identifier = parts[-1]
        club_name = " ".join(parts[1:-1]).strip()
        if not club_name:
            await message.answer(
                "Использование: /removeplayer Название клуба @username|ID"
            )
            return
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(
                db, "SELECT id, name FROM clubs WHERE name_lower=?",
                (normalize_name(club_name),)
            )
        if not club:
            await message.answer(f"Клуб '{club_name}' не найден")
            return
        await _remove_player_impl(message, club[0], club[1], identifier)
        return

    if len(parts) != 2:
        await message.answer("Использование: /removeplayer @username|ID")
        return
    identifier = parts[1]
    if not identifier:
        await message.answer("Укажите @username или ID")
        return

    async with aiosqlite.connect(DB) as db:
        club = await fetchone(
            db, "SELECT id, name FROM clubs WHERE captain_id=?", (user_id,)
        )
    if not club:
        await message.answer("Вы не являетесь капитаном клуба")
        return
    await _remove_player_impl(message, club[0], club[1], identifier)


@dp.message(Command("roster"))
async def roster(message: Message):
    user_id = message.from_user.id
    is_admin = admin_only(user_id)
    parts = message.text.split(maxsplit=1)

    if is_admin:
        if len(parts) < 2:
            await message.answer("Использование: /roster Название клуба")
            return
        club_name = parts[1].strip()
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(
                db,
                "SELECT id, name, captain_id FROM clubs WHERE name_lower=?",
                (normalize_name(club_name),)
            )
        if not club:
            await message.answer(f"Клуб '{club_name}' не найден")
            return
    else:
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(
                db,
                "SELECT id, name, captain_id FROM clubs WHERE captain_id=?",
                (user_id,)
            )
        if not club:
            await message.answer("Вы не являетесь капитаном клуба")
            return

    async with aiosqlite.connect(DB) as db:
        members = await fetchall(
            db,
            """
            SELECT m.user_id, COALESCE(u.username, m.username)
            FROM club_members m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.club_id=?
            ORDER BY m.added_at
            """,
            (club[0],)
        )

    text = f"👥 СОСТАВ КЛУБА '{club[1]}'\n\n"
    if not members:
        text += "Игроков нет"
    else:
        for i, (mid, muname) in enumerate(members, 1):
            mark = " 👑" if mid == club[2] else ""
            label = f"@{muname}" if muname else f"ID:{mid}"
            text += f"{i}. {label}{mark}\n"
    await message.answer(text)


@dp.message(Command("myclub"))
async def my_club(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(
            db, "SELECT id, name, captain_id FROM clubs WHERE captain_id=?",
            (user_id,)
        )
        if not club:
            await message.answer("Вы не являетесь капитаном клуба")
            return
        members = await fetchall(
            db,
            """
            SELECT m.user_id, COALESCE(u.username, m.username)
            FROM club_members m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.club_id=?
            """,
            (club[0],)
        )

    text = f"👑 КЛУБ '{club[1]}'\n\nИгроков: {len(members)}\n\n"
    for i, (mid, muname) in enumerate(members, 1):
        mark = " 👑" if mid == club[2] else ""
        label = f"@{muname}" if muname else f"ID:{mid}"
        text += f"{i}. {label}{mark}\n"
    await message.answer(text)


# ============================================================
# ЗАЯВКИ В КЛУБ
# ============================================================

async def notify_request_reviewers(db, req_id, user_id, username, club_id,
                                    club_name, captain_id):
    reviewers = set(EXTRA_ADMINS) | {ADMIN_ID}
    if captain_id:
        reviewers.add(captain_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_req_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_req_{req_id}"),
        ]
    ])
    text = (
        f"📨 ЗАЯВКА В КЛУБ «{club_name}»\n\n"
        f"От: @{username or user_id} ({user_id})\n"
        f"Заявка #{req_id}"
    )
    for uid in reviewers:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            print(f"Notify reviewer error ({uid}):", e)


@dp.callback_query(F.data.startswith("request_join_"))
async def handle_request_join(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 5:
        await callback.answer("Ошибка", show_alert=True)
        return
    try:
        match_id = int(parts[2])
        team_num = int(parts[3])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return

    user_id = callback.from_user.id
    username = callback.from_user.username or ""

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db, "SELECT team1, team2 FROM matches WHERE id=?", (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return
        team = match[0] if team_num == 1 else match[1]
        if not team:
            await callback.answer("Команда не найдена", show_alert=True)
            return

        club = await fetchone(
            db, "SELECT id, name, captain_id FROM clubs WHERE name_lower=?",
            (normalize_name(team),)
        )
        if not club:
            await callback.answer("Клуб не создан", show_alert=True)
            return

        member = await fetchone(
            db, "SELECT id FROM club_members WHERE club_id=? AND user_id=?",
            (club[0], user_id)
        )
        if member:
            await callback.answer("Вы уже в этом клубе", show_alert=True)
            return

        existing = await fetchone(
            db, "SELECT id FROM club_requests WHERE user_id=? AND club_id=? AND status='pending'",
            (user_id, club[0])
        )
        if existing:
            await callback.answer(
                f"Заявка уже отправлена (№{existing[0]}), ожидайте",
                show_alert=True
            )
            return

        cursor = await db.execute(
            "INSERT INTO club_requests (user_id, club_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (user_id, club[0], now_str())
        )
        req_id = cursor.lastrowid
        await db.commit()

        await notify_request_reviewers(
            db, req_id, user_id, username, club[0], club[1], club[2]
        )

    await callback.message.answer(
        f"✅ Заявка в клуб «{club[1]}» отправлена (№{req_id}).\n"
        "Капитан и администраторы получили уведомление.\n"
        "Как только заявку примут, вы сможете отписаться за этот клуб."
    )
    await callback.answer("Заявка отправлена")


@dp.callback_query(F.data.startswith("accept_req_"))
async def accept_request(callback: CallbackQuery):
    try:
        req_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    uid = callback.from_user.id

    async with aiosqlite.connect(DB) as db:
        req = await fetchone(
            db,
            """
            SELECT r.user_id, r.club_id, r.status, c.name, c.captain_id,
                   COALESCE(u.username, '')
            FROM club_requests r
            JOIN clubs c ON c.id = r.club_id
            LEFT JOIN users u ON u.user_id = r.user_id
            WHERE r.id=?
            """,
            (req_id,)
        )
        if not req:
            await callback.answer("Заявка не найдена", show_alert=True)
            return

        applicant_id, club_id, status, club_name, captain_id, applicant_uname = req

        if not (admin_only(uid) or uid == captain_id):
            await callback.answer("Нет прав обрабатывать эту заявку", show_alert=True)
            return

        if status != "pending":
            await callback.answer(f"Заявка уже обработана ({status})", show_alert=True)
            return

        member = await fetchone(
            db, "SELECT id FROM club_members WHERE club_id=? AND user_id=?",
            (club_id, applicant_id)
        )
        if not member:
            await db.execute(
                "INSERT INTO club_members (club_id, user_id, username, added_at) VALUES (?, ?, ?, ?)",
                (club_id, applicant_id, applicant_uname or None, now_str())
            )

        await db.execute(
            "UPDATE club_requests SET status='accepted', decided_at=?, decided_by=? WHERE id=?",
            (now_str(), uid, req_id)
        )
        await db.commit()

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n✅ Заявка принята",
            reply_markup=None
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            applicant_id,
            f"✅ Ваша заявка в клуб «{club_name}» принята!\n"
            "Теперь вы можете отправить состав за этот клуб."
        )
    except Exception as e:
        print("Notify applicant error:", e)

    await callback.answer("Принято")


@dp.callback_query(F.data.startswith("reject_req_"))
async def reject_request(callback: CallbackQuery):
    try:
        req_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    uid = callback.from_user.id

    async with aiosqlite.connect(DB) as db:
        req = await fetchone(
            db,
            """
            SELECT r.user_id, r.club_id, r.status, c.name, c.captain_id
            FROM club_requests r
            JOIN clubs c ON c.id = r.club_id
            WHERE r.id=?
            """,
            (req_id,)
        )
        if not req:
            await callback.answer("Заявка не найдена", show_alert=True)
            return

        applicant_id, club_id, status, club_name, captain_id = req

        if not (admin_only(uid) or uid == captain_id):
            await callback.answer("Нет прав обрабатывать эту заявку", show_alert=True)
            return

        if status != "pending":
            await callback.answer(f"Заявка уже обработана ({status})", show_alert=True)
            return

        await db.execute(
            "UPDATE club_requests SET status='rejected', decided_at=?, decided_by=? WHERE id=?",
            (now_str(), uid, req_id)
        )
        await db.commit()

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n❌ Заявка отклонена",
            reply_markup=None
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            applicant_id,
            f"❌ Ваша заявка в клуб «{club_name}» отклонена."
        )
    except Exception as e:
        print("Notify applicant error:", e)

    await callback.answer("Отклонено")


@dp.message(Command("join"))
async def join_club_request(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /join Название клуба")
        return

    club_name = parts[1].strip()
    user_id = message.from_user.id
    username = message.from_user.username or ""

    async with aiosqlite.connect(DB) as db:
        club = await fetchone(
            db, "SELECT id, name, captain_id FROM clubs WHERE name_lower=?",
            (normalize_name(club_name),)
        )
        if not club:
            await message.answer(f"❌ Клуб «{club_name}» не найден")
            return

        member = await fetchone(
            db, "SELECT id FROM club_members WHERE club_id=? AND user_id=?",
            (club[0], user_id)
        )
        if member:
            await message.answer(f"✅ Вы уже состоите в клубе «{club[1]}»")
            return

        existing = await fetchone(
            db, "SELECT id FROM club_requests WHERE user_id=? AND club_id=? AND status='pending'",
            (user_id, club[0])
        )
        if existing:
            await message.answer(
                f"⏳ Заявка в клуб «{club[1]}» уже отправлена (№{existing[0]})"
            )
            return

        cursor = await db.execute(
            "INSERT INTO club_requests (user_id, club_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (user_id, club[0], now_str())
        )
        req_id = cursor.lastrowid
        await db.commit()

        await notify_request_reviewers(
            db, req_id, user_id, username, club[0], club[1], club[2]
        )

    await message.answer(
        f"✅ Заявка в клуб «{club[1]}» отправлена (№{req_id})"
    )


@dp.message(Command("requests"))
async def show_requests(message: Message):
    user_id = message.from_user.id
    is_admin = admin_only(user_id)

    async with aiosqlite.connect(DB) as db:
        if is_admin:
            rows = await fetchall(
                db,
                """
                SELECT r.id, r.user_id, r.club_id, c.name,
                       COALESCE(u.username, '')
                FROM club_requests r
                JOIN clubs c ON c.id = r.club_id
                LEFT JOIN users u ON u.user_id = r.user_id
                WHERE r.status='pending'
                ORDER BY r.id
                """
            )
        else:
            captains = await fetchall(
                db, "SELECT id FROM clubs WHERE captain_id=?", (user_id,)
            )
            if not captains:
                await message.answer(
                    "Вы не капитан клуба и не админ — заявки недоступны"
                )
                return
            club_ids = [c[0] for c in captains]
            placeholders = ",".join("?" for _ in club_ids)
            rows = await fetchall(
                db,
                f"""
                SELECT r.id, r.user_id, r.club_id, c.name,
                       COALESCE(u.username, '')
                FROM club_requests r
                JOIN clubs c ON c.id = r.club_id
                LEFT JOIN users u ON u.user_id = r.user_id
                WHERE r.status='pending' AND r.club_id IN ({placeholders})
                ORDER BY r.id
                """,
                tuple(club_ids)
            )

    if not rows:
        await message.answer("📨 Заявок нет")
        return

    await message.answer(f"📨 ЗАЯВКИ В КЛУБЫ ({len(rows)}):")
    for rid, uid, cid, cname, uname in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_req_{rid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_req_{rid}"),
            ]
        ])
        await message.answer(
            f"📨 #{rid} → клуб «{cname}»\n"
            f"От: @{uname or uid} ({uid})",
            reply_markup=kb
        )


# ============================================================
# ОТПИСЬ МАТЧА
# ============================================================

@dp.message(F.text == "⚽ ОТПИСЬ МАТЧА")
async def match_list(message: Message):
    await clear_tab_session(message.from_user.id)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, name, date, send_time, team1, team2, vip_link FROM matches
            WHERE date >= ?
            ORDER BY date, send_time
            """,
            (today,)
        )
        if not matches:
            await message.answer("На данный момент матчей нет")
            return

        buttons = []
        for mid, name, date, time, team1, team2, vip_link in matches:
            pretty = format_date_display(date)
            if match_has_started(date, time):
                buttons.append([InlineKeyboardButton(
                    text=f"👁 {pretty} {time} | {team1} — {team2}",
                    callback_data=f"view_lineups_{mid}"
                )])
            else:
                icon = "✅" if lineup_is_open(date, time) else "🔒"
                s1 = status_icon(await team_status(db, mid, team1))
                s2 = status_icon(await team_status(db, mid, team2))
                tag = vip_label(vip_link) if vip_link else ""
                tag_part = f" {tag}" if tag else ""
                buttons.append([InlineKeyboardButton(
                    text=f"{icon} {pretty} {time}{tag_part} | {team1} {s1} — {s2} {team2}",
                    callback_data=f"lineup_match_{mid}"
                )])

    await message.answer(
        "⚽ ВЫБЕРИТЕ МАТЧ\n\n"
        f"🔒 — отпись откроется за {LINEUP_OPEN_MINUTES} минут до начала\n"
        "✅ — отписалась, ⏳ — нет, 👁 — начался, VIP1/2/3 — метка VIP",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("lineup_match_"))
async def select_lineup_match(callback: CallbackQuery):
    await clear_tab_session(callback.from_user.id)
    match_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT name, team1, team2, photo_id, date, send_time, vip_link "
            "FROM matches WHERE id=?",
            (match_id,)
        )
    if not match:
        await callback.answer("Матч не найден", show_alert=True)
        return
    name, team1, team2, photo_id, mdate, mtime, vip_link = match
    if not team1 or not team2:
        await callback.answer(
            "Для этого матча не указаны команды", show_alert=True
        )
        return

    if not lineup_is_open(mdate, mtime):
        opens = lineup_opens_at(mdate, mtime)
        await callback.answer(
            f"⏳ Отпись на матч ещё закрыта.\n\n"
            f"Откроется {opens.strftime('%d.%m.%Y %H:%M')} МСК\n"
            f"(за {LINEUP_OPEN_MINUTES} минут до начала)",
            show_alert=True
        )
        return

    async with aiosqlite.connect(DB) as db:
        s1 = status_icon(await team_status(db, match_id, team1))
        s2 = status_icon(await team_status(db, match_id, team2))

    tag = vip_label(vip_link) if vip_link else ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{s1} {team1}", callback_data=f"lineup_team_{match_id}_1"
        )],
        [InlineKeyboardButton(
            text=f"{s2} {team2}", callback_data=f"lineup_team_{match_id}_2"
        )],
    ])
    vip_line = f"🎟 {tag}\n" if tag else ""
    caption = (
        f"⚽ {name}\n"
        f"📅 {format_date_display(mdate)} {mtime} МСК\n"
        f"{vip_line}\n"
        f"СТАТУС ОТПИСИ:\n"
        f"{s1} {team1}\n"
        f"{s2} {team2}\n\n"
        f"ВЫБЕРИТЕ СВОЮ КОМАНДУ"
    )
    if photo_id:
        await callback.message.answer_photo(
            photo_id, caption=caption, reply_markup=keyboard
        )
    else:
        await callback.message.answer(caption, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("lineup_team_"))
async def select_lineup_team(callback: CallbackQuery):
    parts = callback.data.split("_")
    match_id = int(parts[2])
    team_number = int(parts[3])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT team1, team2, date, send_time FROM matches WHERE id=?",
            (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return
        team1, team2, mdate, mtime = match
        team = team1 if team_number == 1 else team2
        if not team:
            await callback.answer("Команда не указана", show_alert=True)
            return

        if not lineup_is_open(mdate, mtime):
            opens = lineup_opens_at(mdate, mtime)
            await callback.answer(
                f"⏳ Отпись ещё закрыта. Откроется "
                f"{opens.strftime('%d.%m.%Y %H:%M')} МСК",
                show_alert=True
            )
            return

        club = await fetchone(
            db, "SELECT id, name FROM clubs WHERE name_lower=?",
            (normalize_name(team),)
        )
        if not club:
            await callback.answer(
                f"❌ Клуб для команды '{team}' не создан.\n"
                "Обратитесь к администратору.",
                show_alert=True
            )
            return

        member = await fetchone(
            db,
            "SELECT id FROM club_members WHERE club_id=? AND user_id=?",
            (club[0], user_id)
        )
        if not member:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"📨 Заявка в клуб «{club[1]}»",
                    callback_data=f"request_join_{match_id}_{team_number}"
                )]
            ])
            await callback.message.answer(
                f"❌ Вы не состоите в клубе «{club[1]}».\n\n"
                "Хотите отправить заявку на вступление? "
                "Капитан или администратор её рассмотрит.",
                reply_markup=kb
            )
            await callback.answer()
            return

        # сбрасываем сессию табов (если была) перед стартом состава
        await db.execute(
            "DELETE FROM tab_sessions WHERE user_id=?", (user_id,)
        )
        await db.execute(
            "INSERT OR REPLACE INTO lineup_sessions (user_id, match_id, team) VALUES (?, ?, ?)",
            (user_id, match_id, team)
        )
        await db.commit()

    await callback.message.answer(
        f"КОМАНДА: {team}\n\n"
        "ОТПРАВЬТЕ СОСТАВ ОДНИМ СООБЩЕНИЕМ"
    )
    await callback.answer()


# ============================================================
# ПРОСМОТР СОСТАВОВ
# ============================================================

@dp.callback_query(F.data.startswith("view_lineups_"))
async def view_lineups_public(callback: CallbackQuery):
    try:
        match_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT name, date, send_time, team1, team2 FROM matches WHERE id=?",
            (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return
        name, mdate, mtime, team1, team2 = match

        if not match_has_started(mdate, mtime):
            await callback.answer(
                "Составы откроются после начала матча", show_alert=True
            )
            return

        lineups = await fetchall(
            db,
            """
            SELECT team, roster_text, photo_id, user_id, submitted_at
            FROM lineups WHERE match_id=?
            ORDER BY team, id
            """,
            (match_id,)
        )

    await callback.message.answer(
        f"👁 СОСТАВЫ МАТЧА\n\n"
        f"⚽ {name}\n"
        f"📅 {format_date_display(mdate)} {mtime} МСК"
    )

    if not lineups:
        await callback.message.answer("❌ По этому матчу составы не отправлены")
        await callback.answer()
        return

    for team in (team1, team2):
        team_lineups = [l for l in lineups if l[0] == team]
        if not team_lineups:
            await callback.message.answer(f"❌ {team}: состав не отправлен")
            continue
        await callback.message.answer(
            f"✅ {team}: {len(team_lineups)} состав(ов)"
        )
        for _, roster, photo, uid, subat in team_lineups:
            caption = (
                f"Команда: {team}\n"
                f"Игрок: {uid}\n"
                f"Отправлен: {subat}"
            )
            try:
                if photo:
                    await bot.send_photo(
                        callback.from_user.id, photo, caption=caption
                    )
                else:
                    await bot.send_message(
                        callback.from_user.id,
                        caption + f"\n\n{roster or '—'}"
                    )
            except Exception as e:
                print("Ошибка отправки состава:", e)

    await callback.answer()


# ============================================================
# СОХРАНЕНИЕ СОСТАВА
# ============================================================

async def save_lineup(message, roster_text=None, photo_id=None):
    user_id = message.from_user.id
    notify_info = None

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db, "SELECT match_id, team FROM lineup_sessions WHERE user_id=?",
            (user_id,)
        )
        if not session:
            return False
        match_id, team = session

        match = await fetchone(
            db,
            "SELECT name, date, send_time, team1, team2 FROM matches WHERE id=?",
            (match_id,)
        )
        if not match:
            await db.execute(
                "DELETE FROM lineup_sessions WHERE user_id=?", (user_id,)
            )
            await db.commit()
            await message.answer("Матч больше не существует")
            return True

        name, mdate, mtime, team1, team2 = match

        if not lineup_is_open(mdate, mtime):
            await db.execute(
                "DELETE FROM lineup_sessions WHERE user_id=?", (user_id,)
            )
            await db.commit()
            opens = lineup_opens_at(mdate, mtime)
            await message.answer(
                f"⏳ Отпись на матч «{name}» ещё закрыта.\n"
                f"Откроется {opens.strftime('%d.%m.%Y %H:%M')} МСК."
            )
            return True

        if match_has_started(mdate, mtime):
            await db.execute(
                "DELETE FROM lineup_sessions WHERE user_id=?", (user_id,)
            )
            await db.commit()
            await message.answer(f"⏳ Матч «{name}» уже начался. Отпись закрыта.")
            return True

        prev = await fetchone(
            db,
            "SELECT COUNT(*) FROM lineups WHERE match_id=? AND team=?",
            (match_id, team)
        )
        is_first_for_team = (prev[0] == 0)

        opponent_team = team2 if team == team1 else team1
        opponent_done = await team_status(db, match_id, opponent_team)

        submitted_at = now_str()

        await db.execute(
            """
            INSERT OR REPLACE INTO lineups
            (user_id, match_id, team, roster_text, photo_id, submitted_at, vip_sent)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (user_id, match_id, team, roster_text, photo_id, submitted_at)
        )
        await db.execute(
            "DELETE FROM lineup_sessions WHERE user_id=?", (user_id,)
        )
        await db.commit()

        if is_first_for_team and opponent_team:
            notify_info = (opponent_team, name, team)

    caption = (
        f"⚽ НОВЫЙ СОСТАВ\n\n"
        f"Матч: {name}\n"
        f"Команда: {team}\n"
        f"Игрок: {user_id}"
    )
    try:
        if photo_id:
            await bot.send_photo(ADMIN_ID, photo_id, caption=caption)
        else:
            await bot.send_message(
                ADMIN_ID, caption + f"\n\nСостав:\n{roster_text}"
            )
    except Exception as error:
        print("Ошибка отправки админу:", error)

    if notify_info:
        opponent_team, match_name, own_team = notify_info
        try:
            async with aiosqlite.connect(DB) as db:
                await notify
