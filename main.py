
import asyncio
import os
import re
import aiosqlite

from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ = ZoneInfo("Europe/Moscow")
DB = "ltl.db"

if not TOKEN or not ADMIN_ID:
    raise ValueError("Проверь BOT_TOKEN и ADMIN_ID в .env")

bot = Bot(TOKEN)
dp = Dispatcher()

waiting_start_photo = set()
pending_matches = {}


# БАЗА ДАННЫХ

async def fetchone(db, query, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


async def fetchall(db, query, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchall()


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
            "team2": "TEXT"
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

        await db.commit()


# ГЛАВНОЕ МЕНЮ

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚽ ОТПИСЬ МАТЧА")],
            [KeyboardButton(text="📊 ТАБЫ")]
        ],
        resize_keyboard=True
    )


def admin_only(user_id):
    return user_id == ADMIN_ID


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
            photo[0],
            caption=caption,
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            caption,
            reply_markup=main_menu()
        )


@dp.message(Command("setstartphoto"))
async def set_start_photo(message: Message):
    if not admin_only(message.from_user.id):
        return

    waiting_start_photo.add(message.from_user.id)
    await message.answer("Отправьте фотографию главного меню")


# ОТПИСЬ МАТЧА

@dp.message(F.text == "⚽ ОТПИСЬ МАТЧА")
async def match_list(message: Message):
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, name, date, send_time
            FROM matches
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
        buttons.append([
            InlineKeyboardButton(
                text=f"{date} | {time} | {name}",
                callback_data=f"lineup_match_{mid}"
            )
        ])

    await message.answer(
        "⚽ ВЫБЕРИТЕ МАТЧ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("lineup_match_"))
async def select_lineup_match(callback: CallbackQuery):
    match_id = int(callback.data.split("_")[-1])

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT name, team1, team2, photo_id FROM matches WHERE id=?",
            (match_id,)
        )

    if not match:
        await callback.answer("Матч не найден", show_alert=True)
        return

    name, team1, team2, photo_id = match

    if not team1 or not team2:
        await callback.answer(
            "Для этого матча не указаны команды",
            show_alert=True
        )
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=team1,
            callback_data=f"lineup_team_{match_id}_1"
        )],
        [InlineKeyboardButton(
            text=team2,
            callback_data=f"lineup_team_{match_id}_2"
        )]
    ])

    caption = f"⚽ {name}\n\nВЫБЕРИТЕ СВОЮ КОМАНДУ"

    if photo_id:
        await callback.message.answer_photo(
            photo_id,
            caption=caption,
            reply_markup=keyboard
        )
    else:
        await callback.message.answer(
            caption,
            reply_markup=keyboard
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("lineup_team_"))
async def select_lineup_team(callback: CallbackQuery):
    parts = callback.data.split("_")
    match_id = int(parts[2])
    team_number = int(parts[3])

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT team1, team2 FROM matches WHERE id=?",
            (match_id,)
        )

        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return

        team = match[team_number - 1]

        await db.execute(
            """
            INSERT OR REPLACE INTO lineup_sessions
            (user_id, match_id, team)
            VALUES (?, ?, ?)
            """,
            (callback.from_user.id, match_id, team)
        )

        await db.commit()

    await callback.message.answer(
        f"КОМАНДА: {team}\n\n"
        "ОТПРАВЬТЕ СОСТАВ ОДНИМ СООБЩЕНИЕМ"
    )

    await callback.answer()


# СОХРАНЕНИЕ СОСТАВА

async def save_lineup(message, roster_text=None, photo_id=None):
    user_id = message.from_user.id

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db,
            "SELECT match_id, team FROM lineup_sessions WHERE user_id=?",
            (user_id,)
        )

        if not session:
            return False

        match_id, team = session

        match = await fetchone(
            db,
            "SELECT name FROM matches WHERE id=?",
            (match_id,)
        )

        if not match:
            await db.execute(
                "DELETE FROM lineup_sessions WHERE user_id=?",
                (user_id,)
            )
            await db.commit()
            await message.answer("Матч больше не существует")
            return True

        submitted_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

        await db.execute(
            """
            INSERT OR REPLACE INTO lineups
            (user_id, match_id, team, roster_text, photo_id, submitted_at, vip_sent)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                user_id,
                match_id,
                team,
                roster_text,
                photo_id,
                submitted_at
            )
        )

        await db.execute(
            "DELETE FROM lineup_sessions WHERE user_id=?",
            (user_id,)
        )

        await db.commit()

    caption = (
        f"⚽ НОВЫЙ СОСТАВ\n\n"
        f"Матч: {match[0]}\n"
        f"Команда: {team}\n"
        f"Игрок: {user_id}"
    )

    try:
        if photo_id:
            await bot.send_photo(
                ADMIN_ID,
                photo_id,
                caption=caption
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                caption + f"\n\nСостав:\n{roster_text}"
            )
    except Exception as error:
        print("Ошибка отправки админу:", error)

    await message.answer(
        "✅ СОСТАВ ПРИНЯТ\n\n"
        "VIP-Ссылка будет отправлена в назначенное время"
    )

    return True


# ТАБЫ

@dp.message(F.text == "📊 ТАБЫ")
async def tabs_list(message: Message):
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, name FROM matches
            WHERE date >= ?
            ORDER BY date, send_time
            """,
            (today,)
        )

    if not matches:
        await message.answer("На данный момент матчей нет")
        return

    buttons = [
        [InlineKeyboardButton(
            text=name,
            callback_data=f"tabs_{mid}"
        )]
        for mid, name in matches
    ]

    await message.answer(
        "📊 ВЫБЕРИТЕ МАТЧ ДЛЯ ОТПРАВКИ ТАБОВ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("tabs_"))
async def select_tabs(callback: CallbackQuery):
    match_id = int(callback.data.split("_")[1])

    async with aiosqlite.connect(DB) as db:
        match = await fetchone(
            db,
            "SELECT name FROM matches WHERE id=?",
            (match_id,)
        )

        if not match:
            await callback.answer("Матч не найден", show_alert=True)
            return

        await db.execute(
            "DELETE FROM tabs WHERE user_id=? AND match_id=?",
            (callback.from_user.id, match_id)
        )

        await db.execute(
            """
            INSERT OR REPLACE INTO tab_sessions
            (user_id, match_id, score)
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

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db,
            "SELECT match_id, score FROM tab_sessions WHERE user_id=?",
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
            "SELECT file_id FROM tabs WHERE user_id=? AND match_id=?",
            (user_id, match_id)
        )

        if not photos:
            await message.answer("Вы ещё не отправили фотографии табов")
            return

        match = await fetchone(
            db,
            "SELECT name FROM matches WHERE id=?",
            (match_id,)
        )

        await db.execute(
            "DELETE FROM tab_sessions WHERE user_id=?",
            (user_id,)
        )
        await db.commit()

    try:
        await bot.send_message(
            ADMIN_ID,
            f"📊 НОВЫЕ ТАБЫ\n\n"
            f"Матч: {match[0]}\n"
            f"Счёт: {score}\n"
            f"Игрок: {user_id}\n"
            f"Количество фотографий: {len(photos)}"
        )

        for index, photo in enumerate(photos):
            await bot.send_photo(
                ADMIN_ID,
                photo[0],
                caption="📊 ТАБЫ" if index == 0 else None
            )

    except Exception as error:
        print("Ошибка отправки табов админу:", error)
        await message.answer("Не удалось отправить табы админу")
        return

    await message.answer("✅ ТАБЫ И СЧЁТ УСПЕШНО ОТПРАВЛЕНЫ")


# ПРИЁМ ФОТОГРАФИЙ

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

    if await save_lineup(message, photo_id=file_id):
        return

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db,
            "SELECT match_id, score FROM tab_sessions WHERE user_id=?",
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


# ПРИЁМ ТЕКСТА

@dp.message(F.text & ~F.text.startswith("/"))
async def receive_lineup_text(message: Message):

    if message.text in ("⚽ ОТПИСЬ МАТЧА", "📊 ТАБЫ"):
        return

    user_id = message.from_user.id

    async with aiosqlite.connect(DB) as db:
        session = await fetchone(
            db,
            "SELECT match_id, score FROM tab_sessions WHERE user_id=?",
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
                    "Пример:\n"
                    "Севилья 3:2 Боруссия"
                )
                return

            score = message.text.strip()

            await db.execute(
                "UPDATE tab_sessions SET score=? WHERE user_id=?",
                (score, user_id)
            )
            await db.commit()

            await message.answer(
                f"✅ СЧЁТ ПРИНЯТ\n\n"
                f"{score}\n\n"
                "Теперь отправьте фотографии табов"
            )
            return

    await save_lineup(message, roster_text=message.text)


# АДМИН-ПАНЕЛЬ

@dp.message(Command("admin"))
async def admin(message: Message):
    if not admin_only(message.from_user.id):
        return

    await message.answer(
        "⚙️ АДМИН-ПАНЕЛЬ\n\n"
        "/add — добавить матч\n"
        "/list — список матчей\n"
        "/delete ID — удалить матч\n"
        "/setstartphoto — установить фото меню\n\n"
        "Формат добавления:\n"
        "/add 2026-11-12 | 19:30 | Боруссия | Севилья | https://ссылка\n\n"
        "VIP-ссылку можно не указывать"
    )


# ДОБАВЛЕНИЕ МАТЧА

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
                db,
                "SELECT id FROM matches WHERE date=?",
                (date,)
            )

            if len(count) >= 5:
                await message.answer("На эту дату уже добавлено 5 матчей")
                return

        pending_matches[message.from_user.id] = {
            "date": date,
            "time": time,
            "team1": team1,
            "team2": team2,
            "name": f"{team1} — {team2}",
            "link": link
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
                data["date"],
                data["time"],
                data["name"],
                data["link"],
                photo_id,
                data["team1"],
                data["team2"]
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


# СПИСОК МАТЧЕЙ

@dp.message(Command("list"))
async def list_matches(message: Message):
    if not admin_only(message.from_user.id):
        return

    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(
            db,
            """
            SELECT id, date, send_time, name, sent
            FROM matches
            ORDER BY date, send_time
            """
        )

    if not matches:
        await message.answer("Матчей пока нет")
        return

    for mid, date, time, name, sent in matches:
        await message.answer(
            f"#{mid} {name}\n"
            f"📅 {date}\n"
            f"🕒 {time} МСК\n"
            f"Статус: {'Отправлено' if sent else 'Ожидает'}"
        )


# УДАЛЕНИЕ МАТЧА

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
            await db.execute("DELETE FROM lineups WHERE match_id=?", (match_id,))
            await db.execute("DELETE FROM lineup_sessions WHERE match_id=?", (match_id,))
            await db.execute("DELETE FROM tab_sessions WHERE match_id=?", (match_id,))
            await db.commit()

        await message.answer("✅ МАТЧ УДАЛЁН")

    except Exception:
        await message.answer("Используйте /delete ID")


# АВТОМАТИЧЕСКАЯ ОТПРАВКА VIP

async def vip_sender():
    while True:
        now = datetime.now(TZ)
        current = now.strftime("%Y-%m-%d %H:%M")

        async with aiosqlite.connect(DB) as db:
            matches = await fetchall(
                db,
                """
                SELECT id, name, vip_link
                FROM matches
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
                    SELECT id, user_id
                    FROM lineups
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

                    except Exception as error:
                        print("Ошибка отправки VIP:", error)

                await db.execute(
                    "UPDATE matches SET sent=1 WHERE id=?",
                    (match_id,)
                )
                await db.commit()

        await asyncio.sleep(10)


# ЗАПУСК

async def main():
    await init_db()

    sender_task = asyncio.create_task(vip_sender())

    print("LTL BOT запущен")

    try:
        await dp.start_polling(bot)
    finally:
        sender_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
