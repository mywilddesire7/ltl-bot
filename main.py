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

if not TOKEN:
    raise ValueError("Проверь BOT_TOKEN в .env")

bot = Bot(TOKEN)
dp = Dispatcher()

waiting_start_photo = set()
pending_matches = {}


# ============================================================
# УТИЛИТЫ
# ============================================================

def normalize_name(value: str) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def lineup_opens_at(match_date: str, match_time: str) -> datetime:
    match_dt = datetime.strptime(
        f"{match_date} {match_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)
    return match_dt - timedelta(minutes=LINEUP_OPEN_MINUTES)


def lineup_is_open(match_date: str, match_time: str) -> bool:
    return datetime.now(TZ) >= lineup_opens_at(match_date, match_time)


async def fetchone(db, query, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


async def fetchall(db, query, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchall()


async def resolve_player(db, identifier: str):
    """
    Возвращает (user_id, username|None).
    Поддерживает @username и числовой Telegram ID.
    - @username: ищем в users. Если нет — (None, None).
    - ID: если есть в users — (uid, uname), иначе (uid, None) — добавлять можно.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None, None

    if identifier.startswith("@"):
        uname = identifier[1:].strip()
        if not uname:
            return None, None
        row = await fetchone(
            db,
            "SELECT user_id, username FROM users WHERE LOWER(username)=?",
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


# ============================================================
# МИДЛВАРЬ: сохранение пользователей
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
            await db.execute(
                "ALTER TABLE tabs ADD COLUMN report_id INTEGER"
            )

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
            await db.execute(
                "ALTER TABLE tab_sessions ADD COLUMN score TEXT"
            )

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
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT,
                closed_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                user_id INTEGER,
                text TEXT,
                file_id TEXT,
                file_type TEXT,
                created_at TEXT
            )
        """)

        await db.commit()


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 ТАБЫ"), KeyboardButton(text="⚽ ОТПИСЬ МАТЧА")],
            [KeyboardButton(text="🛟 ПОДДЕРЖКА")],
        ],
        resize_keyboard=True
    )


# ============================================================
# /start и фото меню
# ============================================================

@dp.message(Command("start"))
async def start(message: Message):
    async with aiosqlite.connect(DB) as db:
        photo = await fetchone(
            db,
            "SELECT value FROM settings WHERE key='start_photo'"
        )

    caption = "⚡ LIGHTNING TOUCH LEAGUE\n\nВыберите нужный раздел"

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
# ПОДДЕРЖКА
# ============================================================

async def get_active_ticket(db, user_id):
    return await fetchone(
        db,
        """
        SELECT id FROM support_tickets
        WHERE user_id=? AND status='open'
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,)
    )


async def save_support_message(db, ticket_id, direction, user_id,
                               text=None, file_id=None, file_type="text"):
    await db.execute(
        """
        INSERT INTO support_messages
        (ticket_id, direction, user_id, text, file_id, file_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, direction, user_id, text, file_id, file_type, now_str())
    )
    await db.commit()


async def handle_support_message(message: Message) -> bool:
    user = message.from_user
    if admin_only(user.id):
        return False

    async with aiosqlite.connect(DB) as db:
        ticket = await get_active_ticket(db, user.id)
        if not ticket:
            return False
        ticket_id = ticket[0]

        file_id = None
        file_type = "text"
        text = message.text or message.caption

        if message.photo:
            file_id = message.photo[-1].file_id
            file_type = "photo"
        elif message.video:
            file_id = message.video.file_id
            file_type = "video"
        elif message.document:
            file_id = message.document.file_id
            file_type = "document"
        elif message.text:
            file_type = "text"
        else:
            await message.answer(
                "В поддержку можно отправлять текст, фото, видео или документ."
            )
            return True

        await save_support_message(
            db, ticket_id, "user", user.id, text, file_id, file_type
        )

    header = (
        f"🛟 ОБРАЩЕНИЕ #{ticket_id}\n"
        f"От: @{user.username or 'без username'}\n"
        f"User ID: {user.id}\n\n"
    )

    try:
        if file_type == "photo":
            await bot.send_photo(ADMIN_ID, file_id, caption=header + (text or ""))
        elif file_type == "video":
            await bot.send_video(ADMIN_ID, file_id, caption=header + (text or ""))
        elif file_type == "document":
            await bot.send_document(ADMIN_ID, file_id, caption=header + (text or ""))
        else:
            await bot.send_message(ADMIN_ID, header + (text or ""))
    except Exception as e:
        print("Support forward error:", e)

    await message.answer("✅ Сообщение передано в поддержку")
    return True


@dp.message(F.text == "🛟 ПОДДЕРЖКА")
async def support_button(message: Message):
    user = message.from_user
    async with aiosqlite.connect(DB) as db:
        ticket = await get_active_ticket(db, user.id)
        if ticket:
            await message.answer(
                f"🛟 У вас уже есть открытое обращение #{ticket[0]}.\n\n"
                "Продолжайте писать — сообщения будут переданы администрации.\n"
                "Для завершения: /support_done"
            )
            return

        cursor = await db.execute(
            """
            INSERT INTO support_tickets (user_id, username, status, created_at)
            VALUES (?, ?, 'open', ?)
            """,
            (user.id, user.username or "", now_str())
        )
        await db.commit()
        ticket_id = cursor.lastrowid

    await message.answer(
        f"🛟 ОБРАЩЕНИЕ #{ticket_id} СОЗДАНО\n\n"
        "Опишите вашу проблему текстом, фотографией, видео или документом.\n"
        "Для завершения: /support_done"
    )

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛟 НОВОЕ ОБРАЩЕНИЕ #{ticket_id}\n"
            f"От: @{user.username or 'без username'}\n"
            f"User ID: {user.id}\n"
            f"Время: {now_str()} МСК"
        )
    except Exception as e:
        print("Admin notify error:", e)


@dp.message(Command("support_done"))
async def support_done(message: Message):
    async with aiosqlite.connect(DB) as db:
        ticket = await get_active_ticket(db, message.from_user.id)
        if not ticket:
            await message.answer("У вас нет открытых обращений")
            return
        await db.execute(
            "UPDATE support_tickets SET status='closed', closed_at=? WHERE id=?",
            (now_str(), ticket[0])
        )
        await db.commit()

    await message.answer(f"✅ Обращение #{ticket[0]} закрыто")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛟 Пользователь {message.from_user.id} закрыл обращение #{ticket[0]}"
        )
    except Exception:
        pass


@dp.message(Command("tickets"))
async def tickets_list(message: Message):
    if not admin_only(message.from_user.id):
        return
    async with aiosqlite.connect(DB) as db:
        tickets = await fetchall(
            db,
            """
            SELECT id, user_id, username, created_at
            FROM support_tickets WHERE status='open'
            ORDER BY id
            """
        )
    if not tickets:
        await message.answer("Открытых обращений нет")
        return
    text = "🛟 ОТКРЫТЫЕ ОБРАЩЕНИЯ:\n\n"
    for tid, uid, uname, created in tickets:
        text += f"#{tid} | @{uname or '—'} ({uid}) | {created}\n"
    text += "\n/reply ID текст — ответить\n/close ID — закрыть"
    await message.answer(text)


@dp.message(Command("reply"))
async def reply_ticket(message: Message):
    if not admin_only(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /reply ID текст")
        return
    try:
        ticket_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return
    reply_text = parts[2]

    async with aiosqlite.connect(DB) as db:
        ticket = await fetchone(
            db, "SELECT user_id, status FROM support_tickets WHERE id=?",
            (ticket_id,)
        )
        if not ticket:
            await message.answer("Обращение не найдено")
            return
        user_id, status = ticket
        if status != "open":
            await message.answer("Обращение уже закрыто")
            return
        await save_support_message(
            db, ticket_id, "admin", message.from_user.id, reply_text, None, "text"
        )

    try:
        await bot.send_message(
            user_id,
            f"🛟 ОТВЕТ ПО ОБРАЩЕНИЮ #{ticket_id}\n\n{reply_text}"
        )
    except Exception as e:
        await message.answer(f"Не удалось отправить: {e}")
        return
    await message.answer(f"✅ Ответ отправлен по обращению #{ticket_id}")


@dp.message(Command("close"))
async def close_ticket(message: Message):
    if not admin_only(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /close ID")
        return
    try:
        ticket_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return

    async with aiosqlite.connect(DB) as db:
        ticket = await fetchone(
            db, "SELECT user_id, status FROM support_tickets WHERE id=?",
            (ticket_id,)
        )
        if not ticket:
            await message.answer("Обращение не найдено")
            return
        user_id, status = ticket
        if status != "open":
            await message.answer("Обращение уже закрыто")
            return
        await db.execute(
            "UPDATE support_tickets SET status='closed', closed_at=? WHERE id=?",
            (now_str(), ticket_id)
        )
        await db.commit()

    await message.answer(f"✅ Обращение #{ticket_id} закрыто")
    try:
        await bot.send_message(
            user_id,
            f"🛟 Администратор закрыл ваше обращение #{ticket_id}"
        )
    except Exception:
        pass


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
        note = "\n\n⚠️ Игрок ещё не запускал бота — пусть нажмёт /start, ник подтянется."
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
# ОТПИСЬ МАТЧА (составы, окно -20 минут)
# ============================================================

@dp.message(F.text == "⚽ ОТПИСЬ МАТЧА")
async def match_list(message: Message):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, name, date, send_time FROM matches
            WHERE date >= ?
            ORDER BY date, send_time
            """,
            (today,)
        )
    if not matches:
        await message.answer("На данный момент матчей нет")
        return
    buttons = []
    for mid, name, date, time in matches:
        icon = "✅" if lineup_is_open(date, time) else "🔒"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {date} | {time} | {name}",
            callback_data=f"lineup_match_{mid}"
        )])
    await message.answer(
        "⚽ ВЫБЕРИТЕ МАТЧ\n\n"
        f"🔒 — отпись откроется за {LINEUP_OPEN_MINUTES} минут до начала",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("lineup_match_"))
async def select_lineup_match(callback: CallbackQuery):
    match_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT name, team1, team2, photo_id, date, send_time "
            "FROM matches WHERE id=?",
            (match_id,)
        )
    if not match:
        await callback.answer("Матч не найден", show_alert=True)
        return
    name, team1, team2, photo_id, mdate, mtime = match
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

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=team1, callback_data=f"lineup_team_{match_id}_1"
        )],
        [InlineKeyboardButton(
            text=team2, callback_data=f"lineup_team_{match_id}_2"
        )],
    ])
    caption = f"⚽ {name}\n\nВЫБЕРИТЕ СВОЮ КОМАНДУ"
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
            await callback.answer(
                f"❌ Вы не состоите в клубе '{club[1]}'.\n"
                "Обратитесь к капитану или администратору.",
                show_alert=True
            )
            return

        await db.execute(
            """
            INSERT OR REPLACE INTO lineup_sessions (user_id, match_id, team)
            VALUES (?, ?, ?)
            """,
            (user_id, match_id, team)
        )
        await db.commit()

    await callback.message.answer(
        f"КОМАНДА: {team}\n\nОТПРАВЬТЕ СОСТАВ ОДНИМ СООБЩЕНИЕМ"
    )
    await callback.answer()


# ============================================================
# СОХРАНЕНИЕ СОСТАВА
# ============================================================

async def save_lineup(message, roster_text=None, photo_id=None):
    user_id = message.from_user.id

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
            "SELECT name, date, send_time FROM matches WHERE id=?",
            (match_id,)
        )
        if not match:
            await db.execute(
                "DELETE FROM lineup_sessions WHERE user_id=?", (user_id,)
            )
            await db.commit()
            await message.answer("Матч больше не существует")
            return True

        name, mdate, mtime = match

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

    await message.answer(
        "✅ СОСТАВ ПРИНЯТ\n\n"
        "VIP-Ссылка будет отправлена в назначенное время"
    )
    return True


# ============================================================
# ТАБЫ
# ============================================================

@dp.message(F.text == "📊 ТАБЫ")
async def tabs_list(message: Message):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, name FROM matches
            WHERE date >= ? ORDER BY date, send_time
            """,
            (today,)
        )
    if not matches:
        await message.answer("На данный момент матчей нет")
        return
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"tabs_{mid}")]
        for mid, name in matches
    ]
    await message.answer(
        "📊 ВЫБЕРИТЕ МАТЧ ДЛЯ ОТПРАВКИ ТАБОВ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("tabs_"))
async def select_tabs(callback: CallbackQuery):
    try:
        match_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db, "SELECT name FROM matches WHERE id=?", (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return
        await db.execute(
            "DELETE FROM tabs WHERE user_id=? AND match_id=? AND report_id IS NULL",
            (callback.from_user.id, match_id)
        )
        await db.execute(
            """
            INSERT OR REPLACE INTO tab_sessions (user_id, match_id, score)
            VALUES (?, ?, NULL)
            """,
            (callback.from_user.id, match_id)
        )
        await db.commit()

    await callback.message.answer(
        f"МАТЧ: {match[0]}\n\n"
        "ОТПРАВЬТЕ СЧЁТ МАТЧА И ФОТО ТАБОВ\n\n"
        "Сначала напишите счёт в формате:\n"
        "Севилья 3:2 Боруссия\n\n"
        "Затем отправьте фотографии\n"
        "Когда закончите, напишите /done"
    )
    await callback.answer()


@dp.message(Command("done"))
async def finish_tabs(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db, "SELECT match_id, score FROM tab_sessions WHERE user_id=?",
            (user_id,)
        )
        if not session:
            await message.answer("Сначала выберите матч в разделе ТАБЫ")
            return
        match_id, score = session
        if not score:
            await message.answer("Сначала отправьте счёт матча")
            return
        photos = await fetchall(
            db,
            """
            SELECT file_id FROM tabs
            WHERE user_id=? AND match_id=? AND report_id IS NULL
            """,
            (user_id, match_id)
        )
        if not photos:
            await message.answer("Вы ещё не отправили фотографии табов")
            return
        match = await fetchone(
            db, "SELECT name FROM matches WHERE id=?", (match_id,)
        )

        cursor = await db.execute(
            """
            INSERT INTO tab_reports
            (match_id, user_id, username, score, submitted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (match_id, user_id, username, score, now_str())
        )
        report_id = cursor.lastrowid

        await db.execute(
            """
            UPDATE tabs SET report_id=?
            WHERE user_id=? AND match_id=? AND report_id IS NULL
            """,
            (report_id, user_id, match_id)
        )

        await db.execute(
            "DELETE FROM tab_sessions WHERE user_id=?", (user_id,)
        )
        await db.commit()

    try:
        await bot.send_message(
            ADMIN_ID,
            f"📊 НОВЫЕ ТАБЫ\n\n"
            f"Матч: {match[0]}\n"
            f"Счёт: {score}\n"
            f"Игрок: @{username or user_id} ({user_id})\n"
            f"Отчёт #{report_id}\n"
            f"Количество фотографий: {len(photos)}"
        )
        for index, photo in enumerate(photos):
            await bot.send_photo(
                ADMIN_ID, photo[0],
                caption=f"📊 ТАБЫ (отчёт #{report_id})" if index == 0 else None
            )
    except Exception as error:
        print("Ошибка отправки табов админу:", error)
        await message.answer("Не удалось отправить табы админу")
        return

    await message.answer("✅ ТАБЫ И СЧЁТ УСПЕШНО ОТПРАВЛЕНЫ")


# ============================================================
# ПРИЁМ ФОТОГРАФИЙ
# ============================================================

@dp.message(F.photo)
async def receive_photo(message: Message):
    user_id = message.from_user.id
    file_id = message.photo[-1].file_id

    if admin_only(user_id) and user_id in waiting_start_photo:
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                ("start_photo", file_id)
            )
            await db.commit()
        waiting_start_photo.discard(user_id)
        await message.answer("✅ ФОТО ГЛАВНОГО МЕНЮ СОХРАНЕНО")
        return

    if admin_only(user_id) and user_id in pending_matches:
        await save_pending_match(message, file_id)
        return

    if await handle_support_message(message):
        return

    if await save_lineup(message, photo_id=file_id):
        return

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db, "SELECT match_id, score FROM tab_sessions WHERE user_id=?",
            (user_id,)
        )
        if not session:
            return
        match_id, score = session
        if not score:
            await message.answer("Сначала отправьте счёт матча текстом")
            return
        await db.execute(
            "INSERT INTO tabs (user_id, match_id, file_id) VALUES (?, ?, ?)",
            (user_id, match_id, file_id)
        )
        await db.commit()

    await message.answer(
        "✅ ФОТО ПРИНЯТО\n"
        "Можете отправить следующую фотографию\n"
        "Когда закончите, напишите /done"
    )


@dp.message(F.video | F.document)
async def receive_media(message: Message):
    await handle_support_message(message)


# ============================================================
# ПРИЁМ ТЕКСТА
# ============================================================

@dp.message(F.text & ~F.text.startswith("/"))
async def receive_lineup_text(message: Message):
    if message.text in ("⚽ ОТПИСЬ МАТЧА", "📊 ТАБЫ", "🛟 ПОДДЕРЖКА"):
        return

    if await handle_support_message(message):
        return

    user_id = message.from_user.id

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db, "SELECT match_id, score FROM tab_sessions WHERE user_id=?",
            (user_id,)
        )
        if session:
            match_id, score = session
            if score:
                await message.answer(
                    "Счёт уже принят, теперь отправляйте фотографии табов"
                )
                return
            result = re.fullmatch(
                r"(.+?)\s+(\d+)\s*:\s*(\d+)\s+(.+)",
                message.text.strip()
            )
            if not result:
                await message.answer(
                    "❌ НЕВЕРНЫЙ ФОРМАТ СЧЁТА\n\n"
                    "Пример:\nСевилья 3:2 Боруссия"
                )
                return
            score = message.text.strip()
            await db.execute(
                "UPDATE tab_sessions SET score=? WHERE user_id=?",
                (score, user_id)
            )
            await db.commit()
            await message.answer(
                f"✅ СЧЁТ ПРИНЯТ\n\n{score}\n\n"
                "Теперь отправьте фотографии табов"
            )
            return

    await save_lineup(message, roster_text=message.text)


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

@dp.message(Command("admin"))
async def admin(message: Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer(
        "⚙️ АДМИН-ПАНЕЛЬ\n\n"
        "МАТЧИ:\n"
        "/add — добавить матч\n"
        "/list — список матчей (с кнопками)\n"
        "/match ID — детали матча\n"
        "/delete ID — удалить матч\n"
        "/setstartphoto — установить фото меню\n\n"
        "КЛУБЫ:\n"
        "/addclub Название — создать клуб\n"
        "/clubs — список клубов\n"
        "/setcaptain Клуб @username|ID — назначить капитана\n"
        "/addplayer Клуб @username|ID — добавить игрока\n"
        "/removeplayer Клуб @username|ID — удалить игрока\n"
        "/roster Клуб — состав клуба\n\n"
        "ПОДДЕРЖКА:\n"
        "/tickets — открытые обращения\n"
        "/reply ID текст — ответить\n"
        "/close ID — закрыть\n\n"
        "БЭКАП:\n"
        "/backup — создать копию БД\n\n"
        "Формат добавления:\n"
        "/add 2026-11-12 | 19:30 | Боруссия | Севилья | https://ссылка"
    )


# ============================================================
# ДОБАВЛЕНИЕ МАТЧА
# ============================================================

@dp.message(Command("add"))
async def add_match(message: Message):
    if not admin_only(message.from_user.id):
        return
    try:
        data = message.text.split(maxsplit=1)[1]
        parts = [x.strip() for x in data.split("|")]
        if len(parts) not in (4, 5):
            raise ValueError()
        date, time, team1, team2 = parts[:4]
        link = parts[4] if len(parts) == 5 else ""
        if not team1 or not team2:
            raise ValueError()
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(time, "%H:%M")
        if link and not link.startswith("https://"):
            raise ValueError()

        async with aiosqlite.connect(DB) as db:
            count = await fetchall(
                db, "SELECT id FROM matches WHERE date=?", (date,)
            )
            if len(count) >= 5:
                await message.answer("На эту дату уже добавлено 5 матчей")
                return

        pending_matches[message.from_user.id] = {
            "date": date, "time": time,
            "team1": team1, "team2": team2,
            "name": f"{team1} — {team2}",
            "link": link,
        }
        await message.answer(
            f"МАТЧ: {team1} — {team2}\n"
            f"ДАТА: {date}\n"
            f"ВРЕМЯ: {time} МСК\n\n"
            "Отправьте фотографию матча\n"
            "Или напишите /skip"
        )
    except Exception:
        await message.answer(
            "❌ ОШИБКА ФОРМАТА\n\n"
            "/add 2026-11-12 | 19:30 | Боруссия | Севилья | https://ссылка"
        )


async def save_pending_match(message, photo_id):
    data = pending_matches.pop(message.from_user.id)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO matches
            (date, send_time, name, vip_link, photo_id, team1, team2)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["date"], data["time"], data["name"],
                data["link"], photo_id, data["team1"], data["team2"]
            )
        )
        await db.commit()
    await message.answer(
        f"✅ МАТЧ ДОБАВЛЕН\n\n"
        f"{data['name']}\n"
        f"📅 {data['date']}\n"
        f"🕒 {data['time']} МСК"
    )


@dp.message(Command("skip"))
async def skip_photo(message: Message):
    if not admin_only(message.from_user.id):
        return
    if message.from_user.id not in pending_matches:
        await message.answer("Сначала добавьте матч через /add")
        return
    await save_pending_match(message, None)


# ============================================================
# СПИСОК / ДЕТАЛИ / УДАЛЕНИЕ МАТЧЕЙ
# ============================================================

def match_actions_kb(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📊 Табы", callback_data=f"admin_tabs_{match_id}"
            ),
            InlineKeyboardButton(
                text="⚽ Составы", callback_data=f"admin_lineups_{match_id}"
            ),
        ],
    ])


@dp.message(Command("list"))
async def list_matches(message: Message):
    if not admin_only(message.from_user.id):
        return
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, date, send_time, name, sent FROM matches
            ORDER BY date, send_time
            """
        )
    if not matches:
        await message.answer("Матчей пока нет")
        return
    for mid, date, time, name, sent in matches:
        async with aiosqlite.connect(DB) as db:
            reports_count = await fetchone(
                db,
                "SELECT COUNT(*) FROM tab_reports WHERE match_id=?", (mid,)
            )
            lineups_count = await fetchone(
                db,
                "SELECT COUNT(*) FROM lineups WHERE match_id=?", (mid,)
            )
        await message.answer(
            f"#{mid} {name}\n"
            f"📅 {date}\n"
            f"🕒 {time} МСК\n"
            f"Статус VIP: {'Отправлено' if sent else 'Ожидает'}\n"
            f"📊 Отчётов табов: {reports_count[0]}\n"
            f"⚽ Составов: {lineups_count[0]}",
            reply_markup=match_actions_kb(mid)
        )


@dp.message(Command("match"))
async def match_details(message: Message):
    if not admin_only(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /match ID")
        return
    try:
        match_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            """
            SELECT id, name, date, send_time, team1, team2,
                   vip_link, sent, photo_id
            FROM matches WHERE id=?
            """,
            (match_id,)
        )
        if not match:
            await message.answer("Матч не найден")
            return
        reports_count = await fetchone(
            db,
            "SELECT COUNT(*) FROM tab_reports WHERE match_id=?", (match_id,)
        )
        lineups_count = await fetchone(
            db,
            "SELECT COUNT(*) FROM lineups WHERE match_id=?", (match_id,)
        )

    (mid, name, date, time, team1, team2, link, sent, photo) = match

    opens_at = lineup_opens_at(date, time)
    opens_label = opens_at.strftime("%d.%m.%Y %H:%M") + " МСК"
    status_line = (
        "✅ открыта" if lineup_is_open(date, time)
        else f"🔒 до {opens_label}"
    )

    text = (
        f"⚽ МАТЧ #{mid}\n\n"
        f"Название: {name}\n"
        f"Дата: {date}\n"
        f"Время: {time} МСК\n"
        f"Команды: {team1 or '—'} vs {team2 or '—'}\n"
        f"Отпись: {status_line}\n"
        f"VIP-ссылка: {link or '—'}\n"
        f"Статус VIP: {'Отправлено' if sent else 'Ожидает'}\n"
        f"📊 Отчётов табов: {reports_count[0]}\n"
        f"⚽ Составов: {lineups_count[0]}"
    )

    await message.answer(text, reply_markup=match_actions_kb(mid))


@dp.callback_query(F.data.startswith("admin_tabs_"))
async def admin_view_tabs(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return
    try:
        match_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db, "SELECT name FROM matches WHERE id=?", (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return

        reports = await fetchall(
            db,
            """
            SELECT id, user_id, username, score, submitted_at
            FROM tab_reports WHERE match_id=?
            ORDER BY id
            """,
            (match_id,)
        )

        if not reports:
            await callback.message.answer(
                f"📊 По матчу «{match[0]}» отчётов табов нет"
            )
            await callback.answer()
            return

        await callback.message.answer(
            f"📊 ТАБЫ ПО МАТЧУ «{match[0]}»\n\nОтчётов: {len(reports)}"
        )

        for rid, uid, uname, score, subat in reports:
            await bot.send_message(
                callback.from_user.id,
                f"Отчёт #{rid}\n"
                f"Счёт: {score}\n"
                f"Игрок: @{uname or uid} ({uid})\n"
                f"Время: {subat}"
            )
            photos = await fetchall(
                db,
                "SELECT file_id FROM tabs WHERE report_id=?",
                (rid,)
            )
            for ph in photos:
                try:
                    await bot.send_photo(callback.from_user.id, ph[0])
                except Exception as e:
                    print("Ошибка отправки фото таба:", e)

    await callback.answer()


@dp.callback_query(F.data.startswith("admin_lineups_"))
async def admin_view_lineups(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return
    try:
        match_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db, "SELECT name FROM matches WHERE id=?", (match_id,)
        )
        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return

        lineups = await fetchall(
            db,
            """
            SELECT user_id, team, roster_text, photo_id,
                   submitted_at, vip_sent
            FROM lineups WHERE match_id=?
            ORDER BY id
            """,
            (match_id,)
        )

    if not lineups:
        await callback.message.answer(
            f"⚽ По матчу «{match[0]}» составов нет"
        )
        await callback.answer()
        return

    await callback.message.answer(
        f"⚽ СОСТАВЫ ПО МАТЧУ «{match[0]}»\n\nВсего: {len(lineups)}"
    )

    for uid, team, text, photo, subat, vip in lineups:
        caption = (
            f"Команда: {team}\n"
            f"Игрок: {uid}\n"
            f"Время: {subat}\n"
            f"VIP: {'✅ отправлено' if vip else '❌ не отправлено'}"
        )
        try:
            if photo:
                await bot.send_photo(
                    callback.from_user.id, photo, caption=caption
                )
            else:
                await bot.send_message(
                    callback.from_user.id,
                    caption + f"\n\n{text or '—'}"
                )
        except Exception as e:
            print("Ошибка отправки состава:", e)

    await callback.answer()


@dp.message(Command("delete"))
async def delete_match(message: Message):
    if not admin_only(message.from_user.id):
        return
    try:
        match_id = int(message.text.split()[1])
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM matches WHERE id=?", (match_id,))
            await db.execute("DELETE FROM ready WHERE match_id=?", (match_id,))
            await db.execute("DELETE FROM tabs WHERE match_id=?", (match_id,))
            await db.execute(
                "DELETE FROM tab_reports WHERE match_id=?", (match_id,)
            )
            await db.execute("DELETE FROM lineups WHERE match_id=?", (match_id,))
            await db.execute(
                "DELETE FROM lineup_sessions WHERE match_id=?", (match_id,)
            )
            await db.execute(
                "DELETE FROM tab_sessions WHERE match_id=?", (match_id,)
            )
            await db.commit()
        await message.answer("✅ МАТЧ УДАЛЁН")
    except Exception:
        await message.answer("Используйте /delete ID")


# ============================================================
# РЕЗЕРВНЫЕ КОПИИ
# ============================================================

def _create_backup_sync() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"ltl_{timestamp}.db")

    src = sqlite3.connect(DB)
    dst = sqlite3.connect(path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return path


def _cleanup_backups(keep: int = BACKUPS_KEEP):
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "ltl_*.db")))
    while len(files) > keep:
        try:
            os.remove(files.pop(0))
        except OSError as e:
            print("Backup cleanup error:", e)


async def perform_backup(send_to_admin: bool = True) -> str | None:
    try:
        path = _create_backup_sync()
        _cleanup_backups()
        if send_to_admin:
            try:
                await bot.send_document(
                    ADMIN_ID,
                    FSInputFile(path),
                    caption=(
                        f"💾 Резервная копия БД\n"
                        f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} МСК"
                    )
                )
            except Exception as e:
                print("Send backup error:", e)
        return path
    except Exception as e:
        print("Backup error:", e)
        return None


@dp.message(Command("backup"))
async def backup_command(message: Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer("⏳ Создаю резервную копию...")
    path = await perform_backup(send_to_admin=True)
    if path:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                ("last_backup_date", today)
            )
            await db.commit()
        await message.answer(f"✅ Копия создана: {path}")
    else:
        await message.answer("❌ Не удалось создать копию")


async def backup_scheduler():
    while True:
        try:
            now = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")

            async with aiosqlite.connect(DB) as db:
                row = await fetchone(
                    db,
                    "SELECT value FROM settings WHERE key='last_backup_date'"
                )
            last = row[0] if row else None

            if now.hour >= 3 and last != today:
                print(f"Backup scheduler: creating backup for {today}")
                path = await perform_backup(send_to_admin=True)
                if path:
                    async with aiosqlite.connect(DB) as db:
                        await db.execute(
                            "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                            ("last_backup_date", today)
                        )
                        await db.commit()
        except Exception as e:
            print("Backup scheduler error:", e)

        await asyncio.sleep(60)


# ============================================================
# АВТО-ОТПРАВКА VIP
# ============================================================

async def vip_sender():
    while True:
        try:
            now = datetime.now(TZ)
            current = now.strftime("%Y-%m-%d %H:%M")

            async with aiosqlite.connect(DB) as db:
                matches = await fetchall(
                    db,
                    """
                    SELECT id, name, vip_link FROM matches
                    WHERE date || ' ' || send_time <= ?
                    AND vip_link IS NOT NULL
                    AND vip_link != ''
                    """,
                    (current,)
                )

                for match_id, name, link in matches:
                    users = await fetchall(
                        db,
                        """
                        SELECT id, user_id FROM lineups
                        WHERE match_id=? AND vip_sent=0
                        """,
                        (match_id,)
                    )
                    for lineup_id, user_id in users:
                        try:
                            await bot.send_message(
                                user_id,
                                f"⚽ {name}\n\n"
                                f"Ваша VIP-Ссылка на матч:\n{link}"
                            )
                            await db.execute(
                                "UPDATE lineups SET vip_sent=1 WHERE id=?",
                                (lineup_id,)
                            )
                            await db.commit()
                        except Exception as e:
                            print("VIP send error:", e)

                    await db.execute(
                        "UPDATE matches SET sent=1 WHERE id=?", (match_id,)
                    )
                    await db.commit()
        except Exception as e:
            print("VIP sender loop error:", e)

        await asyncio.sleep(10)


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    await init_db()
    os.makedirs(BACKUP_DIR, exist_ok=True)

    vip_task = asyncio.create_task(vip_sender())
    backup_task = asyncio.create_task(backup_scheduler())

    print("LTL BOT запущен")

    try:
        await dp.start_polling(bot)
    finally:
        vip_task.cancel()
        backup_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
