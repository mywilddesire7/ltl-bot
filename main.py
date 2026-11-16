import asyncio, glob, os, re, sqlite3, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
try:
    sys.stdout.reconfigure(line_buffering=True); sys.stderr.reconfigure(line_buffering=True)
except Exception: pass
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
VIP_LINKS = [
    "https://www.roblox.com/share?code=657ef0c3653a7441921a1f1569d92d2b&type=Server",
    "https://www.roblox.com/share?code=fef5add89a022542808e5f31189e9448&type=Server",
    "https://www.roblox.com/share?code=6717806bff919c4da9739feccd71bec2&type=Server",
]
VIP_LABELS = ["VIP1","VIP2","VIP3"]
if not TOKEN: raise ValueError("Нет BOT_TOKEN")
bot = Bot(TOKEN)
dp = Dispatcher()
waiting_start_photo = set()
pending_matches = {}
schedule_pending = {}
EXTRA_ADMINS = set()

def normalize_name(v): return " ".join((v or "").strip().lower().split()) if v else ""
def admin_only(u): return u == ADMIN_ID or u in EXTRA_ADMINS
def root_admin_only(u): return u == ADMIN_ID
def now_str(): return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
def parse_date_input(t):
    for f in ("%d.%m.%Y","%Y-%m-%d"):
        try: return datetime.strptime((t or "").strip(), f).strftime("%Y-%m-%d")
        except (ValueError, AttributeError): pass
    return None
def format_date_display(d):
    if not d: return "—"
    try: return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (ValueError, TypeError): return d
def match_dt(d,t): return datetime.strptime(f"{d} {t}","%Y-%m-%d %H:%M").replace(tzinfo=TZ)
def lineup_opens_at(d,t): return match_dt(d,t) - timedelta(minutes=LINEUP_OPEN_MINUTES)
def lineup_is_open(d,t): return datetime.now(TZ) >= lineup_opens_at(d,t)
def match_has_started(d,t): return datetime.now(TZ) >= match_dt(d,t)
def vip_send_time(d,t): return match_dt(d,t) - timedelta(minutes=VIP_SEND_BEFORE_MINUTES)
def vip_label(link):
    if not link: return "—"
    try: return VIP_LABELS[VIP_LINKS.index(link)]
    except ValueError: return "VIP"
async def fetchone(db,q,p=()):
    async with db.execute(q,p) as c: return await c.fetchone()
async def fetchall(db,q,p=()):
    async with db.execute(q,p) as c: return await c.fetchall()
async def get_next_vip(db):
    row = await fetchone(db,"SELECT value FROM settings WHERE key='vip_counter'")
    cnt = int(row[0]) if row and row[0] else 0
    link = VIP_LINKS[cnt % len(VIP_LINKS)]
    await db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",("vip_counter",str(cnt+1)))
    await db.commit(); return link
async def resolve_player(db,ident):
    ident = (ident or "").strip()
    if not ident: return None,None
    if ident.startswith("@"):
        u = ident[1:].strip()
        if not u: return None,None
        row = await fetchone(db,"SELECT user_id, username FROM users WHERE LOWER(username)=?",(u.lower(),))
        return (row[0],row[1] or u) if row else (None,None)
    try: uid = int(ident)
    except ValueError: return None,None
    if uid <= 0: return None,None
    row = await fetchone(db,"SELECT user_id, username FROM users WHERE user_id=?",(uid,))
    return (row[0],row[1]) if row else (uid,None)
def parse_schedule_text(text):
    results, pending = [], None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line: continue
        tm = re.search(r"(\d{1,2}):(\d{2})", line)
        if "⚽" in line and not tm:
            clean = line.replace("⚽","").replace("\ufe0f","").strip()
            sep = None
            for s in (" — "," – "," - ","—","–"):
                if s in clean: sep = s; break
            if not sep: continue
            t1,t2 = clean.split(sep,1); t1,t2 = t1.strip(), t2.strip()
            if t1 and t2: pending = (t1,t2)
            continue
        if tm and pending:
            h,m = int(tm.group(1)), int(tm.group(2))
            if 0 <= h <= 23 and 0 <= m <= 59: results.append((pending[0],pending[1],f"{h:02d}:{m:02d}"))
            pending = None
    return results
async def team_status(db,mid,team):
    if not team: return False
    n = normalize_name(team)
    if not n: return False
    row = await fetchone(db,"SELECT COUNT(*) FROM lineups WHERE match_id=? AND team_norm=?",(mid,n))
    if row and row[0] > 0: return True
    row = await fetchone(db,"SELECT COUNT(*) FROM lineups WHERE match_id=? AND LOWER(TRIM(team))=?",(mid,n))
    return bool(row and row[0] > 0)
def status_icon(done): return "✅" if done else "⏳"
async def notify_opponent_team(db,mid,opp_team,match_name,own_team):
    club = await fetchone(db,"SELECT id FROM clubs WHERE name_lower=?",(normalize_name(opp_team),))
    if not club: return
    if await team_status(db,mid,opp_team): return
    for (uid,) in await fetchall(db,"SELECT user_id FROM club_members WHERE club_id=?",(club[0],)):
        try: await bot.send_message(uid, f"⚽ {match_name}\n\n✅ Команда «{own_team}» уже отписалась.\n⏳ Ваша команда «{opp_team}» ещё нет.\n\nНе забудьте отправить состав!")
        except Exception as e: print(f"notify ({uid}):",e,flush=True)
async def clear_sessions(uid,what="both"):
    async with aiosqlite.connect(DB) as db:
        if what in ("both","tabs"): await db.execute("DELETE FROM tab_sessions WHERE user_id=?",(uid,))
        if what in ("both","lineup"): await db.execute("DELETE FROM lineup_sessions WHERE user_id=?",(uid,))
        await db.commit()

class UserSaverMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event,"from_user",None)
        if user is not None:
            try:
                async with aiosqlite.connect(DB) as db:
                    await db.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",(user.id,user.username,user.first_name or ""))
                    await db.commit()
            except Exception as e: print("UserSaver:",e,flush=True)
        return await handler(event,data)
dp.message.outer_middleware(UserSaverMiddleware())
dp.callback_query.outer_middleware(UserSaverMiddleware())

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS matches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, vip_link TEXT, send_time TEXT, date TEXT)")
        cols = [c[1] for c in await fetchall(db,"PRAGMA table_info(matches)")]
        for c,d in {"sent":"INTEGER DEFAULT 0","photo_id":"TEXT","team1":"TEXT","team2":"TEXT","reminder_sent":"INTEGER DEFAULT 0"}.items():
            if c not in cols: await db.execute(f"ALTER TABLE matches ADD COLUMN {c} {d}")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS ready (user_id INTEGER, match_id INTEGER, UNIQUE(user_id,match_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS tabs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, match_id INTEGER, file_id TEXT)")
        cols = [c[1] for c in await fetchall(db,"PRAGMA table_info(tabs)")]
        if "report_id" not in cols: await db.execute("ALTER TABLE tabs ADD COLUMN report_id INTEGER")
        await db.execute("CREATE TABLE IF NOT EXISTS tab_sessions (user_id INTEGER PRIMARY KEY, match_id INTEGER, score TEXT)")
        cols = [c[1] for c in await fetchall(db,"PRAGMA table_info(tab_sessions)")]
        if "score" not in cols: await db.execute("ALTER TABLE tab_sessions ADD COLUMN score TEXT")
        await db.execute("CREATE TABLE IF NOT EXISTS lineup_sessions (user_id INTEGER PRIMARY KEY, match_id INTEGER, team TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS lineups (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, match_id INTEGER, team TEXT, team_norm TEXT, roster_text TEXT, photo_id TEXT, submitted_at TEXT, vip_sent INTEGER DEFAULT 0, UNIQUE(user_id,match_id,team))")
        cols = [c[1] for c in await fetchall(db,"PRAGMA table_info(lineups)")]
        if "team_norm" not in cols: await db.execute("ALTER TABLE lineups ADD COLUMN team_norm TEXT")
        await db.execute("UPDATE lineups SET team_norm=LOWER(TRIM(team)) WHERE team_norm IS NULL")
        await db.execute("CREATE TABLE IF NOT EXISTS tab_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, user_id INTEGER, username TEXT, score TEXT, submitted_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS clubs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, name_lower TEXT NOT NULL UNIQUE, captain_id INTEGER, created_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS club_members (id INTEGER PRIMARY KEY AUTOINCREMENT, club_id INTEGER NOT NULL, user_id INTEGER NOT NULL UNIQUE, username TEXT, added_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, username TEXT, added_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS club_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, club_id INTEGER NOT NULL, status TEXT DEFAULT 'pending', created_at TEXT, decided_at TEXT, decided_by INTEGER)")
        await db.commit()

async def load_admins():
    global EXTRA_ADMINS
    async with aiosqlite.connect(DB) as db:
        rows = await fetchall(db,"SELECT user_id FROM admins")
        EXTRA_ADMINS = {r[0] for r in rows}

def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 ТАБЫ"), KeyboardButton(text="⚽ ОТПИСЬ МАТЧА")],
        [KeyboardButton(text="📅 РАСПИСАНИЕ"), KeyboardButton(text="📋 СТАТУС МАТЧЕЙ")],
    ], resize_keyboard=True)

@dp.message(Command("start"))
async def start(message: Message):
    await clear_sessions(message.from_user.id,"both")
    async with aiosqlite.connect(DB) as db:
        photo = await fetchone(db,"SELECT value FROM settings WHERE key='start_photo'")
    cap = "⚡ LIGHTNING TOUCH LEAGUE\n\n📊 ТАБЫ — счёт и фото\n⚽ ОТПИСЬ МАТЧА — состав\n📅 РАСПИСАНИЕ — матчи сегодня\n📋 СТАТУС — кто отписался\n\n/today — сегодня\n/day ДД.ММ.ГГГГ — на день\n/join Клуб — заявка в клуб"
    if photo: await message.answer_photo(photo[0],caption=cap,reply_markup=main_menu())
    else: await message.answer(cap,reply_markup=main_menu())

@dp.message(Command("setstartphoto"))
async def set_start_photo(message: Message):
    if not admin_only(message.from_user.id): return
    waiting_start_photo.add(message.from_user.id)
    await message.answer("Отправьте фото главного меню")

@dp.callback_query(F.data=="noop")
async def noop_cb(callback: CallbackQuery): await callback.answer()

def schedule_match_kb(mid,started,open_now,is_admin):
    rows = []
    if started or open_now: rows.append([InlineKeyboardButton(text="⚽ Отписаться",callback_data=f"lineup_match_{mid}")])
    if started: rows.append([InlineKeyboardButton(text="👁 Составы",callback_data=f"view_lineups_{mid}")])
    if is_admin: rows.append([InlineKeyboardButton(text="📊 Табы",callback_data=f"admin_tabs_{mid}"),InlineKeyboardButton(text="⚙️ Составы",callback_data=f"admin_lineups_{mid}")])
    if not rows: rows = [[InlineKeyboardButton(text="🔒 Ещё закрыто",callback_data="noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_schedule_for_date(message, date_db):
    uid = message.from_user.id; is_admin = admin_only(uid)
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(db,"SELECT id,name,date,send_time,team1,team2,vip_link FROM matches WHERE date=? ORDER BY send_time",(date_db,))
        if not matches: await message.answer(f"📅 На {format_date_display(date_db)} матчей нет"); return
        await message.answer(f"📅 РАСПИСАНИЕ НА {format_date_display(date_db)}\nВсего: {len(matches)}")
        for mid,name,date,time,t1,t2,vip in matches:
            s1 = status_icon(await team_status(db,mid,t1)); s2 = status_icon(await team_status(db,mid,t2))
            started = match_has_started(date,time); open_now = lineup_is_open(date,time)
            if started: window = "🏁 матч начался (отпись открыта)"
            elif open_now: window = "🔓 отпись открыта"
            else: window = f"🔒 отпись с {lineup_opens_at(date,time).strftime('%H:%M')} МСК"
            tag = vip_label(vip) if vip else "—"
            kb = schedule_match_kb(mid,started,open_now,is_admin)
            await message.answer(f"⚽ МАТЧ #{mid}\n{name}\n🕒 {time} МСК | 🎟 {tag}\n{window}\n\nОтпись: {s1} {t1 or '—'} | {s2} {t2 or '—'}",reply_markup=kb)

@dp.message(F.text=="📅 РАСПИСАНИЕ")
async def schedule_button(message: Message):
    await clear_sessions(message.from_user.id,"both")
    await show_schedule_for_date(message, datetime.now(TZ).strftime("%Y-%m-%d"))

@dp.message(Command("today"))
async def today_matches(message: Message):
    await show_schedule_for_date(message, datetime.now(TZ).strftime("%Y-%m-%d"))

@dp.message(Command("day"))
async def day_matches(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2: await message.answer("Использование: /day ДД.ММ.ГГГГ"); return
    d = parse_date_input(parts[1])
    if not d: await message.answer("❌ Формат: /day ДД.ММ.ГГГГ"); return
    await show_schedule_for_date(message,d)

@dp.message(Command("addclub"))
async def add_club(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip(): await message.answer("Использование: /addclub Название"); return
    name = parts[1].strip(); nl = normalize_name(name)
    if not nl: return
    async with aiosqlite.connect(DB) as db:
        ex = await fetchone(db,"SELECT id, name FROM clubs WHERE name_lower=?",(nl,))
        if ex: await message.answer(f"'{ex[1]}' уже есть (id={ex[0]})"); return
        cur = await db.execute("INSERT INTO clubs (name, name_lower, created_at) VALUES (?, ?, ?)",(name,nl,now_str()))
        await db.commit(); await message.answer(f"✅ '{name}' (id={cur.lastrowid})")

@dp.message(Command("clubs"))
async def list_clubs(message: Message):
    if not admin_only(message.from_user.id): return
    async with aiosqlite.connect(DB) as db:
        clubs = await fetchall(db,"SELECT c.id, c.name, c.captain_id, u.username FROM clubs c LEFT JOIN users u ON u.user_id=c.captain_id ORDER BY c.name")
        if not clubs: await message.answer("Клубов нет. /addclub Название"); return
        for cid,name,cap_id,cap_un in clubs:
            cnt = await fetchone(db,"SELECT COUNT(*) FROM club_members WHERE club_id=?",(cid,))
            cap = f"@{cap_un}" if cap_un else (f"ID:{cap_id}" if cap_id else "—")
            await message.answer(f"#{cid} {name}\nКапитан: {cap}\nИгроков: {cnt[0]}")

async def _set_captain_impl(message, club_id, club_name, ident):
    async with aiosqlite.connect(DB) as db:
        uid,un = await resolve_player(db,ident)
        if uid is None: await message.answer(f"❌ {ident} не найден. /start или ID."); return
        disp = f"@{un}" if un else f"ID:{uid}"
        await db.execute("UPDATE clubs SET captain_id=NULL WHERE captain_id=?",(uid,))
        await db.execute("DELETE FROM club_members WHERE user_id=?",(uid,))
        await db.execute("UPDATE clubs SET captain_id=? WHERE id=?",(uid,club_id))
        await db.execute("INSERT INTO club_members (club_id, user_id, username, added_at) VALUES (?, ?, ?, ?)",(club_id,uid,un,now_str()))
        await db.commit()
    note = "" if un else "\n⚠️ /start"
    await message.answer(f"✅ {disp} — капитан '{club_name}'{note}")

@dp.message(Command("setcaptain"))
async def set_captain(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 3: await message.answer("Использование: /setcaptain Клуб @username|ID"); return
    ident = parts[-1]; club_name = " ".join(parts[1:-1]).strip()
    if not club_name: return
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(db,"SELECT id, name FROM clubs WHERE name_lower=?",(normalize_name(club_name),))
    if not club: await message.answer(f"'{club_name}' не найден"); return
    await _set_captain_impl(message,club[0],club[1],ident)

async def _add_player_impl(message, club_id, club_name, ident):
    async with aiosqlite.connect(DB) as db:
        pid,pun = await resolve_player(db,ident)
        if pid is None: await message.answer(f"❌ {ident} не найден. /start или ID."); return
        disp = f"@{pun}" if pun else f"ID:{pid}"
        ex = await fetchone(db,"SELECT c.name FROM club_members m JOIN clubs c ON c.id=m.club_id WHERE m.user_id=?",(pid,))
        if ex: await message.answer(f"❌ {disp} уже в '{ex[0]}'"); return
        await db.execute("INSERT INTO club_members (club_id, user_id, username, added_at) VALUES (?, ?, ?, ?)",(club_id,pid,pun,now_str()))
        await db.commit()
    note = "" if pun else "\n⚠️ /start"
    await message.answer(f"✅ {disp} в '{club_name}'{note}")

@dp.message(Command("addplayer"))
async def add_player(message: Message):
    uid = message.from_user.id; parts = message.text.split()
    if admin_only(uid):
        if len(parts) < 3: await message.answer("Использование: /addplayer Клуб @username|ID"); return
        ident = parts[-1]; club_name = " ".join(parts[1:-1]).strip()
        if not club_name: return
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(db,"SELECT id, name FROM clubs WHERE name_lower=?",(normalize_name(club_name),))
        if not club: await message.answer(f"'{club_name}' не найден"); return
        await _add_player_impl(message,club[0],club[1],ident); return
    if len(parts) != 2: await message.answer("Использование: /addplayer @username|ID"); return
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(db,"SELECT id, name FROM clubs WHERE captain_id=?",(uid,))
    if not club: await message.answer("Вы не капитан"); return
    await _add_player_impl(message,club[0],club[1],parts[1])

async def _remove_player_impl(message, club_id, club_name, ident):
    async with aiosqlite.connect(DB) as db:
        pid,pun = await resolve_player(db,ident)
        if pid is None: await message.answer(f"❌ {ident} не найден"); return
        disp = f"@{pun}" if pun else f"ID:{pid}"
        m = await fetchone(db,"SELECT id FROM club_members WHERE club_id=? AND user_id=?",(club_id,pid))
        if not m: await message.answer(f"❌ {disp} не в '{club_name}'"); return
        cap = await fetchone(db,"SELECT captain_id FROM clubs WHERE id=?",(club_id,))
        if cap and cap[0] == pid: await message.answer("❌ Нельзя удалить капитана"); return
        await db.execute("DELETE FROM club_members WHERE club_id=? AND user_id=?",(club_id,pid))
        await db.commit()
    await message.answer(f"✅ {disp} удалён из '{club_name}'")

@dp.message(Command("removeplayer"))
async def remove_player(message: Message):
    uid = message.from_user.id; parts = message.text.split()
    if admin_only(uid):
        if len(parts) < 3: await message.answer("Использование: /removeplayer Клуб @username|ID"); return
        ident = parts[-1]; club_name = " ".join(parts[1:-1]).strip()
        if not club_name: return
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(db,"SELECT id, name FROM clubs WHERE name_lower=?",(normalize_name(club_name),))
        if not club: await message.answer(f"'{club_name}' не найден"); return
        await _remove_player_impl(message,club[0],club[1],ident); return
    if len(parts) != 2: await message.answer("Использование: /removeplayer @username|ID"); return
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(db,"SELECT id, name FROM clubs WHERE captain_id=?",(uid,))
    if not club: await message.answer("Вы не капитан"); return
    await _remove_player_impl(message,club[0],club[1],parts[1])

async def _show_roster(message, club_id, club_name, cap_id):
    async with aiosqlite.connect(DB) as db:
        members = await fetchall(db,"SELECT m.user_id, COALESCE(u.username, m.username) FROM club_members m LEFT JOIN users u ON u.user_id=m.user_id WHERE m.club_id=? ORDER BY m.added_at",(club_id,))
    text = f"👥 СОСТАВ '{club_name}'\n\n"
    if not members: text += "Пусто"
    else:
        for i,(mid,mun) in enumerate(members,1):
            mark = " 👑" if mid == cap_id else ""
            label = f"@{mun}" if mun else f"ID:{mid}"
            text += f"{i}. {label}{mark}\n"
    await message.answer(text)

@dp.message(Command("roster"))
async def roster(message: Message):
    uid = message.from_user.id; parts = message.text.split(maxsplit=1)
    if admin_only(uid):
        if len(parts) < 2: await message.answer("Использование: /roster Клуб"); return
        async with aiosqlite.connect(DB) as db:
            club = await fetchone(db,"SELECT id, name, captain_id FROM clubs WHERE name_lower=?",(normalize_name(parts[1]),))
        if not club: await message.answer("Клуб не найден"); return
        await _show_roster(message,club[0],club[1],club[2]); return
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(db,"SELECT id, name, captain_id FROM clubs WHERE captain_id=?",(uid,))
    if not club: await message.answer("Вы не капитан"); return
    await _show_roster(message,club[0],club[1],club[2])

@dp.message(Command("myclub"))
async def my_club(message: Message):
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(db,"SELECT id, name, captain_id FROM clubs WHERE captain_id=?",(message.from_user.id,))
    if not club: await message.answer("Вы не капитан"); return
    await _show_roster(message,club[0],club[1],club[2])

async def notify_reviewers(req_id, uid, uname, cid, cname, cap_id):
    revs = set(EXTRA_ADMINS) | {ADMIN_ID}
    if cap_id: revs.add(cap_id)
    old_name = None
    async with aiosqlite.connect(DB) as db:
        old = await fetchone(db,"SELECT c.name FROM club_members m JOIN clubs c ON c.id=m.club_id WHERE m.user_id=?",(uid,))
        if old: old_name = old[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять",callback_data=f"accept_req_{req_id}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"reject_req_{req_id}")]])
    if old_name and old_name != cname: header = f"📨 ЗАЯВКА (ПЕРЕВОД) В «{cname}»"; note = f"\n🔄 Переход из «{old_name}»"
    else: header = f"📨 ЗАЯВКА В «{cname}»"; note = ""
    text = f"{header}\n\nОт: @{uname or uid} ({uid}){note}\nЗаявка #{req_id}"
    for r in revs:
        try: await bot.send_message(r,text,reply_markup=kb)
        except Exception as e: print(f"notify_rev ({r}):",e,flush=True)

@dp.callback_query(F.data.startswith("request_join_"))
async def handle_request_join(callback: CallbackQuery):
    parts = callback.data.split("_")
    try: mid,tn = int(parts[2]), int(parts[3])
    except (ValueError, IndexError): await callback.answer("Ошибка",show_alert=True); return
    uid = callback.from_user.id; uname = callback.from_user.username or ""
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT team1, team2 FROM matches WHERE id=?",(mid,))
        if not m: await callback.answer("Матч не найден",show_alert=True); return
        team = m[0] if tn == 1 else m[1]
        club = await fetchone(db,"SELECT id, name, captain_id FROM clubs WHERE name_lower=?",(normalize_name(team),))
        if not club: await callback.answer("Клуб не создан",show_alert=True); return
        if await fetchone(db,"SELECT id FROM club_members WHERE club_id=? AND user_id=?",(club[0],uid)): await callback.answer("Вы уже в клубе",show_alert=True); return
        ex = await fetchone(db,"SELECT id FROM club_requests WHERE user_id=? AND club_id=? AND status='pending'",(uid,club[0]))
        if ex: await callback.answer(f"Заявка уже есть (№{ex[0]})",show_alert=True); return
        cur = await db.execute("INSERT INTO club_requests (user_id, club_id, status, created_at) VALUES (?, ?, 'pending', ?)",(uid,club[0],now_str()))
        rid = cur.lastrowid; await db.commit()
    await notify_reviewers(rid,uid,uname,club[0],club[1],club[2])
    await callback.message.answer(f"✅ Заявка в «{club[1]}» отправлена (№{rid})")
    await callback.answer("Отправлено")

@dp.callback_query(F.data.startswith("accept_req_"))
async def accept_request(callback: CallbackQuery):
    try: rid = int(callback.data.split("_")[-1])
    except ValueError: await callback.answer("Ошибка",show_alert=True); return
    uid = callback.from_user.id; old_name = None; new_name = None; applicant_id = None; applicant_uname = ""; old_cap_id = None
    try:
        async with aiosqlite.connect(DB) as db:
            req = await fetchone(db,"SELECT r.user_id, r.club_id, r.status, c.name, c.captain_id, COALESCE(u.username,'') FROM club_requests r JOIN clubs c ON c.id=r.club_id LEFT JOIN users u ON u.user_id=r.user_id WHERE r.id=?",(rid,))
            if not req: await callback.answer("Заявка не найдена",show_alert=True); return
            applicant_id, cid, st, new_name, cap, applicant_uname = req
            if not (admin_only(uid) or uid == cap): await callback.answer("Нет прав",show_alert=True); return
            if st != "pending": await callback.answer(f"Уже обработана ({st})",show_alert=True); return
            old = await fetchone(db,"SELECT c.id, c.name, c.captain_id FROM club_members m JOIN clubs c ON c.id=m.club_id WHERE m.user_id=?",(applicant_id,))
            if old:
                old_cid, old_name, old_cap_id = old
                if old_cid == cid:
                    await db.execute("UPDATE club_requests SET status='accepted', decided_at=?, decided_by=? WHERE id=?",(now_str(),uid,rid))
                    await db.commit(); await callback.answer("Игрок уже в этом клубе"); return
                if old_cap_id == applicant_id: await callback.answer("Игрок — капитан старого клуба. Сначала /setcaptain другого.",show_alert=True); return
                await db.execute("DELETE FROM club_members WHERE user_id=?",(applicant_id,))
            await db.execute("INSERT INTO club_members (club_id, user_id, username, added_at) VALUES (?, ?, ?, ?)",(cid,applicant_id,applicant_uname or None,now_str()))
            await db.execute("UPDATE club_requests SET status='accepted', decided_at=?, decided_by=? WHERE id=?",(now_str(),uid,rid))
            await db.commit()
    except Exception as e: print("accept_request:",e,flush=True); await callback.answer(f"Ошибка: {e}",show_alert=True); return
    try:
        prefix = f"\n\n🔄 Перевод из «{old_name}» принят" if old_name else "\n\n✅ Заявка принята"
        await callback.message.edit_text((callback.message.text or "")+prefix,reply_markup=None)
    except Exception: pass
    try:
        msg = (f"✅ Вы переведены из «{old_name}» в «{new_name}»!" if old_name else f"✅ Ваша заявка в «{new_name}» принята!")
        await bot.send_message(applicant_id,msg+"\nТеперь можете отписаться за этот клуб.")
    except Exception as e: print("notify_app:",e,flush=True)
    if old_cap_id and old_cap_id != applicant_id and old_cap_id != uid:
        try: await bot.send_message(old_cap_id,f"📤 Игрок @{applicant_uname or applicant_id} ({applicant_id}) перешёл из «{old_name}» в «{new_name}».")
        except Exception as e: print("notify_old_cap:",e,flush=True)
    await callback.answer("Перевод принят" if old_name else "Принято")

@dp.callback_query(F.data.startswith("reject_req_"))
async def reject_request(callback: CallbackQuery):
    try: rid = int(callback.data.split("_")[-1])
    except ValueError: await callback.answer("Ошибка",show_alert=True); return
    uid = callback.from_user.id
    try:
        async with aiosqlite.connect(DB) as db:
            req = await fetchone(db,"SELECT r.user_id, r.club_id, r.status, c.name, c.captain_id FROM club_requests r JOIN clubs c ON c.id=r.club_id WHERE r.id=?",(rid,))
            if not req: await callback.answer("Нет заявки",show_alert=True); return
            aid, cid, st, cname, cap = req
            if not (admin_only(uid) or uid == cap): await callback.answer("Нет прав",show_alert=True); return
            if st != "pending": await callback.answer(f"Уже {st}",show_alert=True); return
            await db.execute("UPDATE club_requests SET status='rejected', decided_at=?, decided_by=? WHERE id=?",(now_str(),uid,rid))
            await db.commit()
    except Exception as e: print("reject:",e,flush=True); await callback.answer(f"Ошибка: {e}",show_alert=True); return
    try: await callback.message.edit_text((callback.message.text or "")+"\n\n❌ Отклонена",reply_markup=None)
    except Exception: pass
    try: await bot.send_message(aid,f"❌ Заявка в «{cname}» отклонена")
    except Exception as e: print("notify_app:",e,flush=True)
    await callback.answer("Отклонено")

@dp.message(Command("join"))
async def join_club_request(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2: await message.answer("Использование: /join Клуб"); return
    cname_in = parts[1].strip(); uid = message.from_user.id; uname = message.from_user.username or ""
    async with aiosqlite.connect(DB) as db:
        club = await fetchone(db,"SELECT id, name, captain_id FROM clubs WHERE name_lower=?",(normalize_name(cname_in),))
        if not club: await message.answer(f"❌ '{cname_in}' не найден"); return
        if await fetchone(db,"SELECT id FROM club_members WHERE club_id=? AND user_id=?",(club[0],uid)): await message.answer(f"✅ Вы в «{club[1]}»"); return
        ex = await fetchone(db,"SELECT id FROM club_requests WHERE user_id=? AND club_id=? AND status='pending'",(uid,club[0]))
        if ex: await message.answer(f"⏳ Заявка уже (№{ex[0]})"); return
        cur = await db.execute("INSERT INTO club_requests (user_id, club_id, status, created_at) VALUES (?, ?, 'pending', ?)",(uid,club[0],now_str()))
        rid = cur.lastrowid; await db.commit()
    await notify_reviewers(rid,uid,uname,club[0],club[1],club[2])
    await message.answer(f"✅ Заявка в «{club[1]}» (№{rid})")

@dp.message(Command("requests"))
async def show_requests(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB) as db:
        if admin_only(uid):
            rows = await fetchall(db,"SELECT r.id, r.user_id, c.name, COALESCE(u.username,'') FROM club_requests r JOIN clubs c ON c.id=r.club_id LEFT JOIN users u ON u.user_id=r.user_id WHERE r.status='pending' ORDER BY r.id")
        else:
            caps = await fetchall(db,"SELECT id FROM clubs WHERE captain_id=?",(uid,))
            if not caps: await message.answer("Вы не капитан"); return
            ids = [c[0] for c in caps]; ph = ",".join("?"*len(ids))
            rows = await fetchall(db,f"SELECT r.id, r.user_id, c.name, COALESCE(u.username,'') FROM club_requests r JOIN clubs c ON c.id=r.club_id LEFT JOIN users u ON u.user_id=r.user_id WHERE r.status='pending' AND r.club_id IN ({ph}) ORDER BY r.id",tuple(ids))
    if not rows: await message.answer("📨 Заявок нет"); return
    await message.answer(f"📨 ЗАЯВКИ ({len(rows)}):")
    for rid,r_uid,cname,uname in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять",callback_data=f"accept_req_{rid}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"reject_req_{rid}")]])
        await message.answer(f"📨 #{rid} → «{cname}»\nОт: @{uname or r_uid} ({r_uid})",reply_markup=kb)

@dp.message(F.text=="⚽ ОТПИСЬ МАТЧА")
async def match_list(message: Message):
    await clear_sessions(message.from_user.id,"tabs")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(db,"SELECT id,name,date,send_time,team1,team2,vip_link FROM matches WHERE date>=? ORDER BY date, send_time",(today,))
        if not matches: await message.answer("Матчей нет"); return
        today_list = [m for m in matches if m[2] == today]
        future_list = [m for m in matches if m[2] != today]
        btns = []
        if today_list:
            btns.append([InlineKeyboardButton(text="━━━ СЕГОДНЯ ━━━",callback_data="noop")])
            for mid,name,date,time,t1,t2,vip in today_list:
                s1 = status_icon(await team_status(db,mid,t1)); s2 = status_icon(await team_status(db,mid,t2))
                tag = vip_label(vip) if vip else ""; tagp = f" {tag}" if tag else ""
                ic = "🏁" if match_has_started(date,time) else ("🔓" if lineup_is_open(date,time) else "🔒")
                btns.append([InlineKeyboardButton(text=f"{ic} #{mid} {time}{tagp} | {t1} {s1} — {s2} {t2}",callback_data=f"lineup_match_{mid}")])
        if future_list:
            btns.append([InlineKeyboardButton(text="━━━ БУДУЩИЕ ━━━",callback_data="noop")])
            for mid,name,date,time,t1,t2,vip in future_list:
                pr = format_date_display(date); s1 = status_icon(await team_status(db,mid,t1)); s2 = status_icon(await team_status(db,mid,t2))
                tag = vip_label(vip) if vip else ""; tagp = f" {tag}" if tag else ""
                ic = "🔓" if lineup_is_open(date,time) else "🔒"
                btns.append([InlineKeyboardButton(text=f"{ic} #{mid} {pr} {time}{tagp} | {t1} {s1} — {s2} {t2}",callback_data=f"lineup_match_{mid}")])
    await message.answer("⚽ ВЫБЕРИТЕ МАТЧ\n\n🔒/🔓/🏁 — закрыто/открыто/начался",reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("lineup_match_"))
async def select_lineup_match(callback: CallbackQuery):
    await clear_sessions(callback.from_user.id,"tabs")
    mid = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name, team1, team2, photo_id, date, send_time, vip_link FROM matches WHERE id=?",(mid,))
    if not m: await callback.answer("Не найден",show_alert=True); return
    name,t1,t2,ph,md,mt,vip = m
    if not t1 or not t2: await callback.answer("Команды не указаны",show_alert=True); return
    if not lineup_is_open(md,mt):
        opens = lineup_opens_at(md,mt); await callback.answer(f"⏳ Отпись откроется {opens.strftime('%d.%m.%Y %H:%M')} МСК (за {LINEUP_OPEN_MINUTES}мин)",show_alert=True); return
    async with aiosqlite.connect(DB) as db:
        s1 = status_icon(await team_status(db,mid,t1)); s2 = status_icon(await team_status(db,mid,t2))
    tag = vip_label(vip) if vip else ""
    rows = [[InlineKeyboardButton(text=f"{s1} {t1}",callback_data=f"lineup_team_{mid}_1")],[InlineKeyboardButton(text=f"{s2} {t2}",callback_data=f"lineup_team_{mid}_2")]]
    started = match_has_started(md,mt)
    if started: rows.append([InlineKeyboardButton(text="👁 Посмотреть составы",callback_data=f"view_lineups_{mid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    vline = f"🎟 {tag}\n" if tag else ""
    started_line = "🏁 Матч начался — отпись всё ещё открыта\n" if started else ""
    cap = f"⚽ МАТЧ #{mid} | {name}\n📅 {format_date_display(md)} {mt} МСК\n{vline}{started_line}\nСТАТУС:\n{s1} {t1}\n{s2} {t2}\n\nВЫБЕРИТЕ КОМАНДУ"
    if ph: await callback.message.answer_photo(ph,caption=cap,reply_markup=kb)
    else: await callback.message.answer(cap,reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("lineup_team_"))
async def select_lineup_team(callback: CallbackQuery):
    parts = callback.data.split("_"); mid,tn = int(parts[2]), int(parts[3]); uid = callback.from_user.id
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT team1, team2, date, send_time FROM matches WHERE id=?",(mid,))
        if not m: await callback.answer("Не найден",show_alert=True); return
        t1,t2,md,mt = m; team = t1 if tn == 1 else t2
        if not team: await callback.answer("Команда не указана",show_alert=True); return
        if not lineup_is_open(md,mt): await callback.answer("Отпись ещё закрыта",show_alert=True); return
        club = await fetchone(db,"SELECT id, name FROM clubs WHERE name_lower=?",(normalize_name(team),))
        if not club: await callback.answer(f"❌ Клуб «{team}» не создан",show_alert=True); return
        member = await fetchone(db,"SELECT id FROM club_members WHERE club_id=? AND user_id=?",(club[0],uid))
        if not member:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"📨 Заявка в «{club[1]}»",callback_data=f"request_join_{mid}_{tn}")]])
            await callback.message.answer(f"❌ Вы не в клубе «{club[1]}».\n\nОтправить заявку на вступление?",reply_markup=kb)
            await callback.answer(); return
        await db.execute("DELETE FROM tab_sessions WHERE user_id=?",(uid,))
        await db.execute("INSERT OR REPLACE INTO lineup_sessions (user_id, match_id, team) VALUES (?, ?, ?)",(uid,mid,team))
        await db.commit()
    await callback.message.answer(f"КОМАНДА: {team}\n\nОТПРАВЬТЕ СОСТАВ ОДНИМ СООБЩЕНИЕМ")
    await callback.answer()

@dp.callback_query(F.data.startswith("view_lineups_"))
async def view_lineups_public(callback: CallbackQuery):
    try: mid = int(callback.data.split("_")[-1])
    except ValueError: await callback.answer("Ошибка",show_alert=True); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name, date, send_time, team1, team2 FROM matches WHERE id=?",(mid,))
        if not m: await callback.answer("Не найден",show_alert=True); return
        name,md,mt,t1,t2 = m
        if not match_has_started(md,mt): await callback.answer("Составы после старта",show_alert=True); return
        lineups = await fetchall(db,"SELECT team, roster_text, photo_id, user_id, submitted_at FROM lineups WHERE match_id=? ORDER BY team, id",(mid,))
    await callback.message.answer(f"👁 СОСТАВЫ #{mid}\n\n⚽ {name}\n📅 {format_date_display(md)} {mt} МСК")
    if not lineups: await callback.message.answer("❌ Составов нет"); await callback.answer(); return
    for team in (t1,t2):
        tl = [l for l in lineups if normalize_name(l[0]) == normalize_name(team)]
        if not tl: await callback.message.answer(f"❌ {team}: нет"); continue
        await callback.message.answer(f"✅ {team}: {len(tl)}")
        for _,roster,ph,uid,sub in tl:
            cap = f"Команда: {team}\nИгрок: {uid}\nОтправлен: {sub}"
            try:
                if ph: await bot.send_photo(callback.from_user.id,ph,caption=cap)
                else: await bot.send_message(callback.from_user.id,cap+f"\n\n{roster or '—'}")
            except Exception as e: print("view:",e,flush=True)
    await callback.answer()

async def save_lineup(message, roster_text=None, photo_id=None):
    uid = message.from_user.id; notify_info = None
    async with aiosqlite.connect(DB) as db:
        s = await fetchone(db,"SELECT match_id, team FROM lineup_sessions WHERE user_id=?",(uid,))
        if not s: return False
        mid,team = s
        m = await fetchone(db,"SELECT name, date, send_time, team1, team2 FROM matches WHERE id=?",(mid,))
        if not m:
            await db.execute("DELETE FROM lineup_sessions WHERE user_id=?",(uid,)); await db.commit()
            await message.answer("Матч удалён"); return True
        name,md,mt,t1,t2 = m
        if not lineup_is_open(md,mt):
            await db.execute("DELETE FROM lineup_sessions WHERE user_id=?",(uid,)); await db.commit()
            opens = lineup_opens_at(md,mt)
            await message.answer(f"⏳ Отпись на «{name}» ещё закрыта. Откроется {opens.strftime('%d.%m.%Y %H:%M')} МСК"); return True
        prev = await fetchone(db,"SELECT COUNT(*) FROM lineups WHERE match_id=? AND team_norm=?",(mid,normalize_name(team)))
        first_for_team = (prev[0] == 0)
        opp = t2 if normalize_name(team) == normalize_name(t1) else t1
        opp_done = await team_status(db,mid,opp)
        sub = now_str()
        await db.execute("INSERT OR REPLACE INTO lineups (user_id, match_id, team, team_norm, roster_text, photo_id, submitted_at, vip_sent) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",(uid,mid,team,normalize_name(team),roster_text,photo_id,sub))
        await db.execute("DELETE FROM lineup_sessions WHERE user_id=?",(uid,)); await db.commit()
        if first_for_team and opp: notify_info = (opp,name,team)
    cap = f"⚽ НОВЫЙ СОСТАВ\n\nМатч: {name}\nКоманда: {team}\nИгрок: {uid}"
    try:
        if photo_id: await bot.send_photo(ADMIN_ID,photo_id,caption=cap)
        else: await bot.send_message(ADMIN_ID,cap+f"\n\nСостав:\n{roster_text}")
    except Exception as e: print("send_admin:",e,flush=True)
    if notify_info:
        opp,mn,own = notify_info
        try:
            async with aiosqlite.connect(DB) as db: await notify_opponent_team(db,mid,opp,mn,own)
        except Exception as e: print("notify:",e,flush=True)
    so_line = f"✅ Соперник «{opp}» уже отписался" if opp_done else f"⏳ Соперник «{opp}» пока не отписался\n(актуальный статус: 📋 СТАТУС МАТЧЕЙ)"
    await message.answer(f"✅ СОСТАВ ПРИНЯТ\n\nВаша команда «{team}»: ✅\n{so_line}\n\nVIP за {VIP_SEND_BEFORE_MINUTES} минут до матча")
    return True

@dp.message(F.text=="📋 СТАТУС МАТЧЕЙ")
async def status_overview(message: Message):
    await clear_sessions(message.from_user.id,"both")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(db,"SELECT id,name,date,send_time,team1,team2 FROM matches WHERE date>=? ORDER BY date, send_time",(today,))
        if not matches: await message.answer("Матчей нет"); return
        for mid,name,date,time,t1,t2 in matches:
            s1 = status_icon(await team_status(db,mid,t1)); s2 = status_icon(await team_status(db,mid,t2))
            pr = format_date_display(date)
            if match_has_started(date,time):
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👁 Составы",callback_data=f"view_lineups_{mid}")]])
                await message.answer(f"⚽ #{mid} {name}\n📅 {pr} {time} МСК\n🏁 начался\n\n{s1} {t1}\n{s2} {t2}",reply_markup=kb)
            else:
                lock = "🔓 открыта" if lineup_is_open(date,time) else f"🔒 с {lineup_opens_at(date,time).strftime('%d.%m.%Y %H:%M')} МСК"
                await message.answer(f"⚽ #{mid} {name}\n📅 {pr} {time} МСК\n{lock}\n\n{s1} {t1}\n{s2} {t2}")

@dp.message(F.text=="📊 ТАБЫ")
async def tabs_list(message: Message):
    await clear_sessions(message.from_user.id,"lineup")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(db,"SELECT id, name FROM matches WHERE date>=? ORDER BY date, send_time",(today,))
    if not matches: await message.answer("Матчей нет"); return
    btns = [[InlineKeyboardButton(text=f"#{m} {n}",callback_data=f"tabs_{m}")] for m,n in matches]
    await message.answer("📊 ВЫБЕРИТЕ МАТЧ ДЛЯ ТАБОВ",reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("tabs_"))
async def select_tabs(callback: CallbackQuery):
    await clear_sessions(callback.from_user.id,"lineup")
    try: mid = int(callback.data.split("_")[1])
    except (IndexError, ValueError): await callback.answer("Ошибка",show_alert=True); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name FROM matches WHERE id=?",(mid,))
        if not m: await callback.answer("Не найден",show_alert=True); return
        await db.execute("DELETE FROM tabs WHERE user_id=? AND match_id=? AND report_id IS NULL",(callback.from_user.id,mid))
        await db.execute("INSERT OR REPLACE INTO tab_sessions (user_id, match_id, score) VALUES (?, ?, NULL)",(callback.from_user.id,mid))
        await db.commit()
    await callback.message.answer(f"МАТЧ #{mid}: {m[0]}\n\nОТПРАВЬТЕ СЧЁТ И ФОТО\n\nСчёт: Севилья 3:2 Боруссия\n\nЗатем фото\n\n/done — завершить")
    await callback.answer()

@dp.message(Command("done"))
async def finish_tabs(message: Message):
    uid = message.from_user.id; uname = message.from_user.username or ""
    async with aiosqlite.connect(DB) as db:
        s = await fetchone(db,"SELECT match_id, score FROM tab_sessions WHERE user_id=?",(uid,))
        if not s: await message.answer("Сначала выберите матч в ТАБАХ"); return
        mid,score = s
        if not score: await message.answer("Сначала счёт"); return
        photos = await fetchall(db,"SELECT file_id FROM tabs WHERE user_id=? AND match_id=? AND report_id IS NULL",(uid,mid))
        if not photos: await message.answer("Нет фото табов"); return
        m = await fetchone(db,"SELECT name FROM matches WHERE id=?",(mid,))
        cur = await db.execute("INSERT INTO tab_reports (match_id, user_id, username, score, submitted_at) VALUES (?, ?, ?, ?, ?)",(mid,uid,uname,score,now_str()))
        rid = cur.lastrowid
        await db.execute("UPDATE tabs SET report_id=? WHERE user_id=? AND match_id=? AND report_id IS NULL",(rid,uid,mid))
        await db.execute("DELETE FROM tab_sessions WHERE user_id=?",(uid,)); await db.commit()
    try:
        await bot.send_message(ADMIN_ID,f"📊 ТАБЫ #{mid}\n\nМатч: {m[0]}\nСчёт: {score}\nИгрок: @{uname or uid} ({uid})\nОтчёт #{rid}\nФото: {len(photos)}")
        for i,ph in enumerate(photos): await bot.send_photo(ADMIN_ID,ph[0],caption=f"📊 #{rid}" if i == 0 else None)
    except Exception as e: print("send_tabs:",e,flush=True); await message.answer("Не удалось отправить"); return
    await message.answer("✅ ТАБЫ И СЧЁТ ОТПРАВЛЕНЫ")

@dp.message(F.photo)
async def receive_photo(message: Message):
    uid = message.from_user.id; fid = message.photo[-1].file_id
    if admin_only(uid) and uid in waiting_start_photo:
        async with aiosqlite.connect(DB) as db:
            await db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",("start_photo",fid)); await db.commit()
        waiting_start_photo.discard(uid); await message.answer("✅ ФОТО МЕНЮ СОХРАНЕНО"); return
    if admin_only(uid) and uid in pending_matches: await save_pending_match(message,fid); return
    if await save_lineup(message,photo_id=fid): return
    async with aiosqlite.connect(DB) as db:
        s = await fetchone(db,"SELECT match_id, score FROM tab_sessions WHERE user_id=?",(uid,))
        if not s: return
        mid,score = s
        if not score: await message.answer("Сначала счёт текстом"); return
        await db.execute("INSERT INTO tabs (user_id, match_id, file_id) VALUES (?, ?, ?)",(uid,mid,fid)); await db.commit()
    await message.answer("✅ ФОТО ПРИНЯТО\nЕщё или /done")

@dp.message(F.text & ~F.text.startswith("/"))
async def receive_text(message: Message):
    if message.text in ("⚽ ОТПИСЬ МАТЧА","📊 ТАБЫ","📅 РАСПИСАНИЕ","📋 СТАТУС МАТЧЕЙ"): return
    uid = message.from_user.id
    if uid in schedule_pending:
        date_db = schedule_pending.pop(uid); await process_schedule_text(message,date_db,message.text); return
    async with aiosqlite.connect(DB) as db:
        s = await fetchone(db,"SELECT match_id, score FROM tab_sessions WHERE user_id=?",(uid,))
        if s:
            mid,score = s
            if score: await message.answer("Счёт уже принят, шлите фото /done"); return
            m = re.fullmatch(r"(.+?)\s+(\d+)\s*:\s*(\d+)\s+(.+)",message.text.strip())
            if not m: await message.answer("❌ Формат: Севилья 3:2 Боруссия"); return
            sc = message.text.strip()
            await db.execute("UPDATE tab_sessions SET score=? WHERE user_id=?",(sc,uid)); await db.commit()
            await message.answer(f"✅ СЧЁТ: {sc}\nТеперь фото табов, потом /done"); return
    await save_lineup(message,roster_text=message.text)

async def process_schedule_text(message, date_db, text):
    matches = parse_schedule_text(text)
    if not matches:
        await message.answer("❌ Не найдено матчей.\n\nФормат:\n⚽ Байер — Марсель ⚽\n19:00 МСК\n\n⚽ Севилья — Аталанта ⚽\n20:00 МСК\n\nVIP: 1→VIP1, 2→VIP2, 3→VIP3, 4→VIP1, 5→VIP2."); return
    async with aiosqlite.connect(DB) as db:
        exist = await fetchone(db,"SELECT COUNT(*) FROM matches WHERE date=?",(date_db,))
        if exist[0] + len(matches) > 5:
            await message.answer(f"❌ На {format_date_display(date_db)} уже {exist[0]}, ещё {len(matches)} — превысит лимит 5"); return
    added = []
    async with aiosqlite.connect(DB) as db:
        for t1,t2,tm in matches:
            vip = await get_next_vip(db); label = vip_label(vip)
            await db.execute("INSERT INTO matches (date, send_time, name, vip_link, team1, team2) VALUES (?, ?, ?, ?, ?, ?)",(date_db,tm,f"{t1} — {t2}",vip,t1,t2))
            added.append((t1,t2,tm,label))
        await db.commit()
    lines = [f"✅ ДОБАВЛЕНО {len(added)} матчей на {format_date_display(date_db)}:"]
    for i,(t1,t2,tm,lab) in enumerate(added,1): lines.append(f"{i}. {t1} — {t2}, {tm} МСК, 🎟 {lab}")
    await message.answer("\n".join(lines))

@dp.message(Command("addschedule"))
async def addschedule_cmd(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2: await message.answer("Использование:\n/addschedule ДД.ММ.ГГГГ\nзатем пришлите блок с матчами"); return
    date_db = parse_date_input(parts[1].split()[0])
    if not date_db: await message.answer("❌ Формат: /addschedule 14.11.2026"); return
    schedule_pending[message.from_user.id] = date_db
    await message.answer(f"📅 Дата: {format_date_display(date_db)}\n\nПришлите блок текстом:\n⚽ Байер — Марсель ⚽\n19:00 МСК\n\n⚽ Севилья — Аталанта ⚽\n20:00 МСК\n\nVIP: 1→VIP1, 2→VIP2, 3→VIP3, 4→VIP1, 5→VIP2.")

@dp.message(Command("admin"))
async def admin(message: Message):
    if not admin_only(message.from_user.id): return
    await message.answer("⚙️ АДМИН-ПАНЕЛЬ\n\nМАТЧИ:\n/add ДД.ММ.ГГГГ | HH:MM | T1 | T2 | https://vip\n/addschedule ДД.ММ.ГГГГ — расписание текстом\n/list — все матчи\n/today — сегодня\n/day ДД.ММ.ГГГГ — на день\n/match ID — детали\n/setmatchtime ID ДД.ММ.ГГГГ HH:MM — изменить время\n/setvip ID https://... — VIP\n/clearvip ID — убрать VIP\n/delete ID — удалить\n/setstartphoto — фото меню\n\nКЛУБЫ:\n/addclub Название\n/clubs\n/setcaptain Клуб @username|ID\n/addplayer Клуб @username|ID\n/removeplayer Клуб @username|ID\n/roster Клуб\n\nЗАЯВКИ:\n/requests\n\nПОЛЬЗОВАТЕЛИ:\n/users\n\nАДМИНЫ:\n/admins\n/addadmin @username|ID\n/removeadmin @username|ID\n\nБЭКАП:\n/backup")

@dp.message(Command("users"))
async def list_users(message: Message):
    if not admin_only(message.from_user.id): return
    async with aiosqlite.connect(DB) as db:
        total = await fetchone(db,"SELECT COUNT(*) FROM users")
        users = await fetchall(db,"SELECT user_id, username, first_name FROM users ORDER BY user_id")
    if not users: await message.answer("Никто не взаимодействовал"); return
    chunk = f"👥 ПОЛЬЗОВАТЕЛИ ({total[0]})\n\n"
    for uid,un,fn in users:
        label = f"@{un}" if un else (fn or "—"); line = f"{uid} — {label}\n"
        if len(chunk) + len(line) > 3800: await message.answer(chunk); chunk = ""
        chunk += line
    if chunk.strip(): await message.answer(chunk)

@dp.message(Command("addadmin"))
async def add_admin(message: Message):
    if not root_admin_only(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2: await message.answer("Использование: /addadmin @username|ID"); return
    ident = parts[1].strip()
    async with aiosqlite.connect(DB) as db:
        uid,un = await resolve_player(db,ident)
        if uid is None: await message.answer("❌ Не найден. /start или ID."); return
        if uid == ADMIN_ID: await message.answer("Это главный админ"); return
        if await fetchone(db,"SELECT user_id FROM admins WHERE user_id=?",(uid,)): await message.answer(f"ID:{uid} уже админ"); return
        await db.execute("INSERT INTO admins (user_id, username, added_at) VALUES (?, ?, ?)",(uid,un,now_str())); await db.commit()
    EXTRA_ADMINS.add(uid); disp = f"@{un}" if un else f"ID:{uid}"
    await message.answer(f"✅ {disp} теперь админ")

@dp.message(Command("removeadmin"))
async def remove_admin(message: Message):
    if not root_admin_only(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2: await message.answer("Использование: /removeadmin @username|ID"); return
    ident = parts[1].strip()
    async with aiosqlite.connect(DB) as db:
        uid,un = await resolve_player(db,ident)
        if uid is None: await message.answer("❌ Не найден"); return
        if uid == ADMIN_ID: await message.answer("Главного нельзя"); return
        if not await fetchone(db,"SELECT user_id FROM admins WHERE user_id=?",(uid,)): await message.answer(f"ID:{uid} не админ"); return
        await db.execute("DELETE FROM admins WHERE user_id=?",(uid,)); await db.commit()
    EXTRA_ADMINS.discard(uid); disp = f"@{un}" if un else f"ID:{uid}"
    await message.answer(f"✅ {disp} больше не админ")

@dp.message(Command("admins"))
async def list_admins(message: Message):
    if not admin_only(message.from_user.id): return
    async with aiosqlite.connect(DB) as db:
        rows = await fetchall(db,"SELECT a.user_id, COALESCE(u.username, a.username) FROM admins a LEFT JOIN users u ON u.user_id=a.user_id ORDER BY a.added_at")
    text = f"👑 ГЛАВНЫЙ: {ADMIN_ID}\n\n"
    if rows:
        text += "ДОП:\n"
        for uid,un in rows: text += f"• @{un} ({uid})\n" if un else f"• ID:{uid}\n"
    else: text += "Доп. админов нет"
    await message.answer(text)

@dp.message(Command("add"))
async def add_match(message: Message):
    if not admin_only(message.from_user.id): return
    try:
        data = message.text.split(maxsplit=1)[1]
        parts = [x.strip() for x in data.split("|")]
        if len(parts) not in (4,5): raise ValueError()
        date_in,time,t1,t2 = parts[:4]; link = parts[4] if len(parts) == 5 else ""
        if not t1 or not t2: raise ValueError()
        date_db = parse_date_input(date_in)
        if not date_db: raise ValueError()
        datetime.strptime(time,"%H:%M")
        if link and not link.startswith("https://"): raise ValueError()
        async with aiosqlite.connect(DB) as db:
            cnt = await fetchall(db,"SELECT id FROM matches WHERE date=?",(date_db,))
            if len(cnt) >= 5: await message.answer("На дату уже 5 матчей"); return
        pending_matches[message.from_user.id] = {"date":date_db,"time":time,"team1":t1,"team2":t2,"name":f"{t1} — {t2}","link":link}
        await message.answer(f"МАТЧ: {t1} — {t2}\nДАТА: {format_date_display(date_db)}\nВРЕМЯ: {time} МСК\n\nФото матча или /skip")
    except Exception: await message.answer("❌ Формат:\n/add 12.11.2026 | 19:30 | Боруссия | Севилья | https://vip")

async def save_pending_match(message, photo_id):
    data = pending_matches.pop(message.from_user.id)
    async with aiosqlite.connect(DB) as db:
        if not data["link"]: data["link"] = await get_next_vip(db)
        await db.execute("INSERT INTO matches (date, send_time, name, vip_link, photo_id, team1, team2) VALUES (?, ?, ?, ?, ?, ?, ?)",(data["date"],data["time"],data["name"],data["link"],photo_id,data["team1"],data["team2"])); await db.commit()
    tag = vip_label(data["link"])
    await message.answer(f"✅ МАТЧ ДОБАВЛЕН\n{data['name']}\n📅 {format_date_display(data['date'])}\n🕒 {data['time']} МСК\n🎟 {tag}\nVIP за {VIP_SEND_BEFORE_MINUTES} мин до матча")

@dp.message(Command("skip"))
async def skip_photo(message: Message):
    if not admin_only(message.from_user.id): return
    if message.from_user.id not in pending_matches: await message.answer("Сначала /add"); return
    await save_pending_match(message,None)

@dp.message(Command("setmatchtime"))
async def set_match_time(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 4: await message.answer("Использование: /setmatchtime ID ДД.ММ.ГГГГ HH:MM\nПример: /setmatchtime 5 14.11.2026 21:30"); return
    try: mid = int(parts[1])
    except ValueError: await message.answer("ID должен быть числом"); return
    date_db = parse_date_input(parts[2])
    if not date_db: await message.answer("Дата: ДД.ММ.ГГГГ"); return
    try: datetime.strptime(parts[3],"%H:%M")
    except ValueError: await message.answer("Время: HH:MM"); return
    new_time = parts[3]
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name, date, send_time FROM matches WHERE id=?",(mid,))
        if not m: await message.answer("Матч не найден"); return
        name,old_date,old_time = m
        if old_date != date_db:
            cnt = await fetchone(db,"SELECT COUNT(*) FROM matches WHERE date=? AND id!=?",(date_db,mid))
            if cnt[0] >= 5: await message.answer(f"На {format_date_display(date_db)} уже 5 матчей"); return
        await db.execute("UPDATE matches SET date=?, send_time=?, sent=0, reminder_sent=0 WHERE id=?",(date_db,new_time,mid))
        await db.execute("UPDATE lineups SET vip_sent=0 WHERE match_id=?",(mid,)); await db.commit()
    opens = lineup_opens_at(date_db,new_time).strftime("%d.%m.%Y %H:%M")
    vip_at = vip_send_time(date_db,new_time).strftime("%d.%m.%Y %H:%M")
    await message.answer(f"✅ ВРЕМЯ ИЗМЕНЕНО\n\nМатч #{mid} {name}\nБыло: {format_date_display(old_date)} {old_time} МСК\nСтало: {format_date_display(date_db)} {new_time} МСК\n\n🔓 Отпись с {opens} МСК\n🎟 VIP уйдёт в {vip_at} МСК\nФлаги VIP и напоминаний сброшены")

@dp.message(Command("setvip"))
async def set_vip(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3: await message.answer("Использование: /setvip ID https://...\n/clearvip ID"); return
    try: mid = int(parts[1])
    except ValueError: await message.answer("ID должен быть числом"); return
    link = parts[2].strip()
    if not link.startswith("https://"): await message.answer("https://"); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name, vip_link, date, send_time FROM matches WHERE id=?",(mid,))
        if not m: await message.answer("Матч не найден"); return
        name,old,md,mt = m
        await db.execute("UPDATE matches SET vip_link=?, sent=0 WHERE id=?",(link,mid))
        await db.execute("UPDATE lineups SET vip_sent=0 WHERE match_id=?",(mid,)); await db.commit()
        rec = await fetchone(db,"SELECT COUNT(*) FROM lineups WHERE match_id=?",(mid,))
    tag = vip_label(link); note = f"\n\nБыло: {old}" if old else ""
    if match_has_started(md,mt): timing = "⏱ Матч начался — уйдёт в ближайшие секунды"
    else: timing = f"⏱ Уйдёт в {vip_send_time(md,mt).strftime('%d.%m.%Y %H:%M')} МСК (за {VIP_SEND_BEFORE_MINUTES}мин)"
    await message.answer(f"✅ VIP ОБНОВЛЕН\n\nМатч #{mid} {name}\n🎟 {tag}\n{link}{note}\n\nПолучателей: {rec[0]}\nФлаги сброшены\n{timing}")

@dp.message(Command("clearvip"))
async def clear_vip(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("Использование: /clearvip ID"); return
    try: mid = int(parts[1])
    except ValueError: await message.answer("ID — число"); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name, vip_link FROM matches WHERE id=?",(mid,))
        if not m: await message.answer("Матч не найден"); return
        name,old = m
        if not old: await message.answer(f"У #{mid} и так нет VIP"); return
        await db.execute("UPDATE matches SET vip_link='' WHERE id=?",(mid,)); await db.commit()
    await message.answer(f"✅ VIP удалён\nМатч #{mid} {name}")

@dp.message(Command("list"))
async def list_matches(message: Message):
    if not admin_only(message.from_user.id): return
    async with aiosqlite.connect(DB) as db:
        matches = await fetchall(db,"SELECT id, date, send_time, name, sent, team1, team2, vip_link FROM matches ORDER BY date, send_time")
    if not matches: await message.answer("Матчей нет"); return
    for mid,date,time,name,sent,t1,t2,vip in matches:
        async with aiosqlite.connect(DB) as db:
            rc = await fetchone(db,"SELECT COUNT(*) FROM tab_reports WHERE match_id=?",(mid,))
            lc = await fetchone(db,"SELECT COUNT(*) FROM lineups WHERE match_id=?",(mid,))
            s1 = status_icon(await team_status(db,mid,t1)); s2 = status_icon(await team_status(db,mid,t2))
        tag = vip_label(vip) if vip else "—"
        await message.answer(f"#{mid} {name}\n📅 {format_date_display(date)}\n🕒 {time} МСК | 🎟 {tag}\nОтпись: {s1} {t1 or '—'} | {s2} {t2 or '—'}\nVIP: {'Отправлено' if sent else 'Ожидает'}\n📊 Табов: {rc[0]} | ⚽ Составов: {lc[0]}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Табы",callback_data=f"admin_tabs_{mid}"),InlineKeyboardButton(text="⚽ Составы",callback_data=f"admin_lineups_{mid}")]]))

@dp.message(Command("match"))
async def match_details(message: Message):
    if not admin_only(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("Использование: /match ID"); return
    try: mid = int(parts[1])
    except ValueError: await message.answer("ID — число"); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT id, name, date, send_time, team1, team2, vip_link, sent, photo_id FROM matches WHERE id=?",(mid,))
        if not m: await message.answer("Матч не найден"); return
        rc = await fetchone(db,"SELECT COUNT(*) FROM tab_reports WHERE match_id=?",(mid,))
        lc = await fetchone(db,"SELECT COUNT(*) FROM lineups WHERE match_id=?",(mid,))
        s1 = status_icon(await team_status(db,mid,m[4])); s2 = status_icon(await team_status(db,mid,m[5]))
    mid_,name,date,time,t1,t2,link,sent,photo = m
    opens = lineup_opens_at(date,time).strftime("%d.%m.%Y %H:%M")+" МСК"
    if match_has_started(date,time): sl = "🏁 начался"
    elif lineup_is_open(date,time): sl = "✅ открыта"
    else: sl = f"🔒 до {opens}"
    tag = vip_label(link) if link else "—"
    vip_at = vip_send_time(date,time).strftime("%d.%m.%Y %H:%M")+" МСК"
    text = f"⚽ МАТЧ #{mid_}\n\nНазвание: {name}\nДата: {format_date_display(date)}\nВремя: {time} МСК\nКоманды: {t1 or '—'} vs {t2 or '—'}\nОтпись: {s1} {t1 or '—'} | {s2} {t2 or '—'}\nОкно отписи: {sl}\nVIP: {tag}\nVIP-ссылка: {link or '—'}\nVIP-отправка: {vip_at}\nСтатус VIP: {'Отправлено' if sent else 'Ожидает'}\n📊 Табов: {rc[0]} | ⚽ Составов: {lc[0]}"
    await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Табы",callback_data=f"admin_tabs_{mid_}"),InlineKeyboardButton(text="⚽ Составы",callback_data=f"admin_lineups_{mid_}")]]))

@dp.callback_query(F.data.startswith("admin_tabs_"))
async def admin_view_tabs(callback: CallbackQuery):
    if not admin_only(callback.from_user.id): await callback.answer("Только админ",show_alert=True); return
    try: mid = int(callback.data.split("_")[-1])
    except ValueError: await callback.answer("Ошибка",show_alert=True); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name FROM matches WHERE id=?",(mid,))
        if not m: await callback.answer("Не найден",show_alert=True); return
        reports = await fetchall(db,"SELECT id, user_id, username, score, submitted_at FROM tab_reports WHERE match_id=? ORDER BY id",(mid,))
        if not reports: await callback.message.answer(f"📊 По «{m[0]}» нет отчётов"); await callback.answer(); return
        await callback.message.answer(f"📊 ТАБЫ «{m[0]}» — {len(reports)}")
        for rid,uid,un,sc,sb in reports:
            await bot.send_message(callback.from_user.id,f"Отчёт #{rid}\nСчёт: {sc}\nИгрок: @{un or uid} ({uid})\nВремя: {sb}")
            photos = await fetchall(db,"SELECT file_id FROM tabs WHERE report_id=?",(rid,))
            for ph in photos:
                try: await bot.send_photo(callback.from_user.id,ph[0])
                except Exception as e: print("tabs_view:",e,flush=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_lineups_"))
async def admin_view_lineups(callback: CallbackQuery):
    if not admin_only(callback.from_user.id): await callback.answer("Только админ",show_alert=True); return
    try: mid = int(callback.data.split("_")[-1])
    except ValueError: await callback.answer("Ошибка",show_alert=True); return
    async with aiosqlite.connect(DB) as db:
        m = await fetchone(db,"SELECT name FROM matches WHERE id=?",(mid,))
        if not m: await callback.answer("Не найден",show_alert=True); return
        lineups = await fetchall(db,"SELECT user_id, team, roster_text, photo_id, submitted_at, vip_sent FROM lineups WHERE match_id=? ORDER BY id",(mid,))
    if not lineups: await callback.message.answer(f"⚽ По «{m[0]}» нет составов"); await callback.answer(); return
    await callback.message.answer(f"⚽ СОСТАВЫ «{m[0]}» — {len(lineups)}")
    for uid,team,txt,ph,sb,vip in lineups:
        cap = f"Команда: {team}\nИгрок: {uid}\nВремя: {sb}\nVIP: {'✅' if vip else '❌'}"
        try:
            if ph: await bot.send_photo(callback.from_user.id,ph,caption=cap)
            else: await bot.send_message(callback.from_user.id,cap+f"\n\n{txt or '—'}")
        except Exception as e: print("lineups_view:",e,flush=True)
    await callback.answer()

@dp.message(Command("delete"))
async def delete_match(message: Message):
    if not admin_only(message.from_user.id): return
    try:
        mid = int(message.text.split()[1])
        async with aiosqlite.connect(DB) as db:
            for t in ("matches","ready","tabs","tab_reports","lineups","lineup_sessions","tab_sessions"):
                col = "id" if t == "matches" else "match_id"
                await db.execute(f"DELETE FROM {t} WHERE {col}=?",(mid,))
            await db.commit()
        await message.answer("✅ МАТЧ УДАЛЁН")
    except Exception: await message.answer("Использование: /delete ID")

def _backup_sync():
    os.makedirs(BACKUP_DIR,exist_ok=True)
    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S"); path = os.path.join(BACKUP_DIR,f"ltl_{ts}.db")
    src = sqlite3.connect(DB); dst = sqlite3.connect(path)
    try:
        with dst: src.backup(dst)
    finally: src.close(); dst.close()
    return path

def _cleanup(keep=BACKUPS_KEEP):
    files = sorted(glob.glob(os.path.join(BACKUP_DIR,"ltl_*.db")))
    while len(files) > keep:
        try: os.remove(files.pop(0))
        except OSError as e: print("cleanup:",e,flush=True)

async def perform_backup(send=True):
    try:
        path = _backup_sync(); _cleanup()
        if send:
            try: await bot.send_document(ADMIN_ID,FSInputFile(path),caption=f"💾 Бэкап {datetime.now(TZ).strftime('%d.%m.%Y %H:%M:%S')} МСК")
            except Exception as e: print("send_backup:",e,flush=True)
        return path
    except Exception as e: print("backup:",e,flush=True); return None

@dp.message(Command("backup"))
async def backup_cmd(message: Message):
    if not admin_only(message.from_user.id): return
    await message.answer("⏳ Создаю...")
    p = await perform_backup(True)
    if p:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB) as db:
            await db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",("last_backup_date",today)); await db.commit()
        await message.answer(f"✅ {p}")
    else: await message.answer("❌ Не удалось")

async def backup_scheduler():
    while True:
        try:
            now = datetime.now(TZ); today = now.strftime("%Y-%m-%d")
            async with aiosqlite.connect(DB) as db:
                r = await fetchone(db,"SELECT value FROM settings WHERE key='last_backup_date'")
            last = r[0] if r else None
            if now.hour >= 3 and last != today:
                p = await perform_backup(True)
                if p:
                    async with aiosqlite.connect(DB) as db:
                        await db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",("last_backup_date",today)); await db.commit()
        except Exception as e: print("backup_sched:",e,flush=True)
        await asyncio.sleep(60)

async def match_reminder_scheduler():
    while True:
        try:
            now = datetime.now(TZ)
            async with aiosqlite.connect(DB) as db:
                matches = await fetchall(db,"SELECT id, name, date, send_time, team1, team2 FROM matches WHERE reminder_sent=0")
                for mid,name,date,time,t1,t2 in matches:
                    try: mdt = match_dt(date,time)
                    except ValueError: continue
                    delta = mdt - now
                    if timedelta(0) < delta <= timedelta(minutes=REMINDER_MINUTES):
                        s1 = await team_status(db,mid,t1); s2 = await team_status(db,mid,t2)
                        if not s1 and not s2:
                            ml = max(1,int(delta.total_seconds() // 60))
                            try: await bot.send_message(ADMIN_ID,f"⚠️ «{name}» через {ml} мин (в {time} МСК)\n📅 {format_date_display(date)}\n\n❌ {t1}\n❌ {t2}\nНикто не отписался!")
                            except Exception as e: print("remind:",e,flush=True)
                        await db.execute("UPDATE matches SET reminder_sent=1 WHERE id=?",(mid,)); await db.commit()
        except Exception as e: print("remind_loop:",e,flush=True)
        await asyncio.sleep(60)

async def vip_sender():
    while True:
        try:
            now = datetime.now(TZ)
            async with aiosqlite.connect(DB) as db:
                matches = await fetchall(db,"SELECT id, name, vip_link, date, send_time FROM matches WHERE vip_link IS NOT NULL AND vip_link != ''")
                for mid,name,link,md,mt in matches:
                    try: send_at = vip_send_time(md,mt)
                    except ValueError: continue
                    if now < send_at: continue
                    users = await fetchall(db,"SELECT id, user_id FROM lineups WHERE match_id=? AND vip_sent=0",(mid,))
                    if not users: continue
                    for lid,uid in users:
                        try:
                            await bot.send_message(uid,f"⚽ {name}\n\nVIP-ссылка на матч:\n{link}")
                            await db.execute("UPDATE lineups SET vip_sent=1 WHERE id=?",(lid,)); await db.commit()
                        except Exception as e: print("vip_send:",e,flush=True)
                    await db.execute("UPDATE matches SET sent=1 WHERE id=?",(mid,)); await db.commit()
        except Exception as e: print("vip_loop:",e,flush=True)
        await asyncio.sleep(10)

async def main():
    print("LTL: init_db...",flush=True)
    await init_db()
    print("LTL: init_db OK",flush=True)
    await load_admins()
    print(f"LTL: admins loaded extra={len(EXTRA_ADMINS)}",flush=True)
    os.makedirs(BACKUP_DIR,exist_ok=True)
    print("LTL: backups dir OK",flush=True)
    t1 = asyncio.create_task(vip_sender())
    t2 = asyncio.create_task(backup_scheduler())
    t3 = asyncio.create_task(match_reminder_scheduler())
    print("LTL BOT запущен",flush=True)
    try:
        await dp.start_polling(bot)
    except Exception:
        import traceback; traceback.print_exc(); raise
    finally:
        t1.cancel(); t2.cancel(); t3.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception:
        import traceback; traceback.print_exc(); raise
