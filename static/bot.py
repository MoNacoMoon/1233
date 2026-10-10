"""Daddy Barbershop — Telegram Mini App: бот + REST API + хостинг статики.

Запуск:  python bot.py   (настройки — в .env, см. .env.example)
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time as _time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           MenuButtonWebApp, ReplyKeyboardMarkup, WebAppInfo)
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("barbershop")

# ==============================================================================
# 1. КОНФИГУРАЦИЯ (всё секретное — только в .env)
# ==============================================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")          # https://your-domain.com
STAFF_IDS = [int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip()]
STAFF_CHAT_ID = int(os.getenv("STAFF_CHAT_ID", "0")) or (STAFF_IDS[0] if STAFF_IDS else 0)
GOOGLE_REVIEW_URL = os.getenv("GOOGLE_REVIEW_URL", "")
DB_PATH = os.getenv("DB_PATH", "data/barbershop.db")
PORT = int(os.getenv("PORT", "8080"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Chisinau"))
SEED_DEMO = os.getenv("SEED_DEMO", "0") == "1"

SLOTS = ["10:00", "11:15", "12:30", "13:45", "15:00", "16:15", "17:30", "18:45", "20:00"]
VISIT_MINUTES = 75            # длительность слота — когда считать визит завершённым
BOOKING_HORIZON_DAYS = 14
MIN_LEAD_MINUTES = 30         # нельзя записаться на время, которое начнётся раньше чем через 30 мин
MAX_ACTIVE_PER_USER = 3
REMIND_BEFORE_MIN = 120

MASTERS = {
    "yakov": {"name": "Yakov", "grade": "top"},
    "ruvim": {"name": "Рувим", "grade": "top"},
    "liza": {"name": "Лиза", "grade": "barber"},
}
# «Любой мастер»: распределяем только среди мастеров этого списка, чтобы цена совпадала с показанной (от 170)
ANY_MASTER_POOL = ["liza"]

# id: (название, цена барбера, цена топ-барбера) — сервер сам считает цену, клиенту не верим
SERVICES = {
    "haircut": ("Стрижка", 170, 200),
    "complex": ("Комплекс (Стрижка + Борода)", 290, 330),
    "beard": ("Оформление бороды", 100, 150),
    "shave": ("Бритье", 170, 200),
    "wax": ("Воск (Удаление волос)", 30, 30),
    "vip_complex": ("Стрижка + Борода + Камуфляж", 360, 450),
}
REVIEW_TAGS = {"Стрижка", "Борода", "Сервис", "Скорость"}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db: aiosqlite.Connection = None  # type: ignore


class FeedbackState(StatesGroup):
    waiting_for_complaint = State()


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def now() -> datetime:
    return datetime.now(TZ)


def is_staff(user_id: int) -> bool:
    return user_id in STAFF_IDS


def fmt_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m")


def start_dt(iso_date: str, t: str) -> datetime:
    return datetime.strptime(f"{iso_date} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def parse_ddmm(s: str):
    """'05.10' -> date, в пределах горизонта записи; иначе None."""
    m = re.fullmatch(r"(\d{2})\.(\d{2})", s or "")
    if not m:
        return None
    today = now().date()
    for year in (today.year, today.year + 1):
        try:
            d = date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        if today <= d <= today + timedelta(days=BOOKING_HORIZON_DAYS):
            return d
    return None


def price_for(master_id: str, service_id: str) -> int:
    _, p_barber, p_top = SERVICES[service_id]
    return p_top if MASTERS[master_id]["grade"] == "top" else p_barber


# ==============================================================================
# 2. БАЗА ДАННЫХ (aiosqlite, WAL)
# ==============================================================================
async def init_db():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            user_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, user_name TEXT, phone TEXT, username TEXT,
            barber_id TEXT NOT NULL, service_id TEXT NOT NULL, service_title TEXT, price INTEGER,
            booking_date TEXT NOT NULL, booking_time TEXT NOT NULL,
            status TEXT DEFAULT 'active', review_status TEXT DEFAULT 'pending',
            reminded INTEGER DEFAULT 0, review_sent INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        -- защита от двойной записи на уровне БД
        CREATE UNIQUE INDEX IF NOT EXISTS uq_slot
            ON appointments(barber_id, booking_date, booking_time) WHERE status='active';
        CREATE INDEX IF NOT EXISTS ix_app_user ON appointments(user_id, status);
        CREATE TABLE IF NOT EXISTS internal_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, user_name TEXT, barber_id TEXT, rating INTEGER, tag TEXT, comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS day_off (
            barber_id TEXT NOT NULL, booking_date TEXT NOT NULL, PRIMARY KEY (barber_id, booking_date));
        CREATE TABLE IF NOT EXISTS nfc_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, barber_id TEXT, user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    if SEED_DEMO:
        cur = await db.execute("SELECT COUNT(*) FROM internal_reviews")
        if (await cur.fetchone())[0] == 0:
            await db.execute("INSERT INTO internal_reviews (user_id,user_name,barber_id,rating,tag,comment) "
                             "VALUES (0,'Максим','yakov',5,'Стрижка','Демо-отзыв')")
    await db.commit()


async def get_profile(user_id: int):
    cur = await db.execute("SELECT full_name, phone FROM client_profiles WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return {"full_name": row["full_name"], "phone": row["phone"]} if row else None


async def save_profile(user_id: int, name: str, phone: str):
    await db.execute("""INSERT INTO client_profiles (user_id, full_name, phone) VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, phone=excluded.phone,
        updated_at=CURRENT_TIMESTAMP""", (user_id, name, phone))
    await db.commit()


async def busy_times(master_id: str, iso_date: str) -> list:
    """Занятые/недоступные слоты. Для 'any' — слот занят, только если занят весь пул."""
    pool = ANY_MASTER_POOL if master_id == "any" else [master_id]
    per_master = []
    for m in pool:
        cur = await db.execute("SELECT 1 FROM day_off WHERE barber_id=? AND booking_date=?", (m, iso_date))
        if await cur.fetchone():
            per_master.append(set(SLOTS))
            continue
        cur = await db.execute("SELECT booking_time FROM appointments WHERE barber_id=? AND booking_date=? "
                               "AND status='active'", (m, iso_date))
        per_master.append({r[0] for r in await cur.fetchall()})
    busy = set.intersection(*per_master) if per_master else set(SLOTS)
    limit = now() + timedelta(minutes=MIN_LEAD_MINUTES)
    busy |= {t for t in SLOTS if start_dt(iso_date, t) <= limit}   # прошедшее время
    return sorted(busy)


async def create_booking(user: dict, master_id: str, service_id: str, d: date, t: str, name: str, phone: str):
    """Атомарно занимает слот. UNIQUE-индекс гарантирует, что двое не запишутся на одно время."""
    pool = ANY_MASTER_POOL if master_id == "any" else [master_id]
    iso = d.isoformat()
    for m in pool:
        cur = await db.execute("SELECT 1 FROM day_off WHERE barber_id=? AND booking_date=?", (m, iso))
        if await cur.fetchone():
            continue
        title = SERVICES[service_id][0]
        price = price_for(m, service_id)
        try:
            cur = await db.execute(
                "INSERT INTO appointments (user_id,user_name,phone,username,barber_id,service_id,service_title,"
                "price,booking_date,booking_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user["id"], name, phone, user.get("username"), m, service_id, title, price, iso, t))
            await db.commit()
        except aiosqlite.IntegrityError:
            continue
        return {"id": cur.lastrowid, "barber_id": m, "service_title": title, "price": price,
                "date": iso, "time": t}
    return None


# ==============================================================================
# 3. АВТОРИЗАЦИЯ MINI APP (проверка подписи initData)
# ==============================================================================
def validate_init_data(init_data: str, max_age: int = 86400):
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if _time.time() - int(parsed.get("auth_date", 0)) > max_age:
            return None
        return json.loads(parsed["user"])
    except Exception:
        return None


def auth(request: web.Request) -> dict:
    user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "unauthorized"}),
                                   content_type="application/json")
    return user


_rate: dict = {}


def rate_limit(user_id: int, bucket: str, limit: int = 6, per: int = 60):
    key, t = (user_id, bucket), _time.time()
    hits = [x for x in _rate.get(key, []) if t - x < per]
    if len(hits) >= limit:
        raise web.HTTPTooManyRequests(text=json.dumps({"ok": False, "error": "too_many_requests"}),
                                      content_type="application/json")
    hits.append(t)
    _rate[key] = hits


async def notify_staff(text: str, kb=None):
    if not STAFF_CHAT_ID:
        return
    try:
        await bot.send_message(STAFF_CHAT_ID, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.error("Не удалось отправить в служебный чат: %s", e)


# ==============================================================================
# 4. REST API
# ==============================================================================
async def serve_index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"),
                            headers={"Cache-Control": "no-cache"})


async def api_health(request):
    return web.json_response({"ok": True})


async def api_slots(request):
    master = request.query.get("master", "any")
    d = parse_ddmm(request.query.get("date", ""))
    if (master != "any" and master not in MASTERS) or not d:
        return web.json_response({"busy": SLOTS})
    return web.json_response({"busy": await busy_times(master, d.isoformat())})


async def api_user_profile(request):
    user = auth(request)
    return web.json_response(await get_profile(user["id"]) or {})


async def api_get_reviews(request):
    barber = request.query.get("barber", "")
    cur = await db.execute(
        "SELECT user_name, rating, tag, comment, strftime('%d.%m', created_at) d FROM internal_reviews "
        "WHERE barber_id=? ORDER BY id DESC LIMIT 15", (barber,))
    rows = await cur.fetchall()
    return web.json_response({"reviews": [
        {"author": r["user_name"], "rating": r["rating"], "tag": r["tag"], "text": r["comment"], "date": r["d"]}
        for r in rows]})


async def api_post_review(request):
    user = auth(request)
    rate_limit(user["id"], "review", limit=5, per=600)
    try:
        body = await request.json()
        barber_id = body["barber_id"]
        rating = int(body["rating"])
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    if barber_id not in MASTERS or not 1 <= rating <= 5:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    cur = await db.execute("SELECT 1 FROM internal_reviews WHERE user_id=? AND barber_id=? "
                           "AND created_at > datetime('now','-1 day')", (user["id"], barber_id))
    if await cur.fetchone():
        return web.json_response({"ok": False, "error": "already_reviewed"}, status=409)

    profile = await get_profile(user["id"])
    author = (str(body.get("author") or "").strip()[:40]
              or (profile or {}).get("full_name") or user.get("first_name") or "Гость")
    tag = body.get("tag") if body.get("tag") in REVIEW_TAGS else "Сервис"
    text = str(body.get("text") or "").strip()[:500]
    await db.execute("INSERT INTO internal_reviews (user_id,user_name,barber_id,rating,tag,comment) "
                     "VALUES (?,?,?,?,?,?)", (user["id"], author, barber_id, rating, tag, text))
    await db.commit()

    await notify_staff(
        "🌟 <b>Новый отзыв мастеру (Mini App)</b>\n\n"
        f"💈 {esc(MASTERS[barber_id]['name'])}\n"
        f"⭐ {'★' * rating}{'☆' * (5 - rating)} ({rating}/5) • {esc(tag)}\n"
        f"👤 {esc(author)}\n💬 {esc(text)}")
    # Низкая оценка — отдельный тревожный сигнал управляющему
    if rating <= 3:
        await notify_staff(f"⚠️ <b>Низкая оценка ({rating}/5)</b> мастеру {esc(MASTERS[barber_id]['name'])} — "
                           f"клиент: {esc(author)}. Стоит связаться.")
    return web.json_response({"ok": True, "author": author})


async def api_book(request):
    user = auth(request)
    rate_limit(user["id"], "book", limit=5, per=60)
    try:
        b = await request.json()
        master_id, service_id, t = b["master_id"], b["service_id"], b["time"]
        name = re.sub(r"\s+", " ", str(b["client_name"])).strip()[:60]
        phone_digits = re.sub(r"\D", "", str(b["phone"]))
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    d = parse_ddmm(str(b.get("date", "")))
    if (master_id != "any" and master_id not in MASTERS) or service_id not in SERVICES or t not in SLOTS \
            or not d or len(name) < 2 or not re.fullmatch(r"373\d{8}", phone_digits):
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    phone = f"+373 ({phone_digits[3:6]}) {phone_digits[6:8]}-{phone_digits[8:]}"

    if start_dt(d.isoformat(), t) <= now() + timedelta(minutes=MIN_LEAD_MINUTES):
        return web.json_response({"ok": False, "error": "slot_past"}, status=409)

    cur = await db.execute("SELECT COUNT(*) FROM appointments WHERE user_id=? AND status='active' "
                           "AND booking_date >= ?", (user["id"], now().date().isoformat()))
    if (await cur.fetchone())[0] >= MAX_ACTIVE_PER_USER:
        return web.json_response({"ok": False, "error": "too_many_active"}, status=409)

    booking = await create_booking(user, master_id, service_id, d, t, name, phone)
    if not booking:
        return web.json_response({"ok": False, "error": "slot_taken"}, status=409)

    await save_profile(user["id"], name, phone)
    master_name = MASTERS[booking["barber_id"]]["name"]

    try:
        await bot.send_message(
            user["id"],
            "🔥 <b>Запись подтверждена!</b>\n\n"
            f"💈 <b>Мастер:</b> {esc(master_name)}\n"
            f"✂️ <b>Услуга:</b> {esc(booking['service_title'])} — <b>{booking['price']} руб</b>\n"
            f"📅 <b>Когда:</b> {fmt_date(booking['date'])} в {booking['time']}\n"
            f"📞 <b>Телефон:</b> {esc(phone)}\n\n"
            "📍 ул. 25 Октября, 94. Напомним за 2 часа до визита.\n"
            "Отменить запись: «📋 Мои записи» в меню.", parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось отправить подтверждение клиенту %s: %s", user["id"], e)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отзыв взят на месте", callback_data=f"adm_revdone_{booking['id']}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm_cancel_{booking['id']}")]])
    await notify_staff(
        f"⚡ <b>Новая запись #{booking['id']}</b>\n\n"
        f"💈 {esc(master_name)}{' (авто, «любой мастер»)' if master_id == 'any' else ''}\n"
        f"✂️ {esc(booking['service_title'])} ({booking['price']} руб)\n"
        f"📅 {fmt_date(booking['date'])} в {booking['time']}\n"
        f"👤 {esc(name)}\n📞 <code>{esc(phone)}</code>\n"
        f"💬 {('@' + esc(user['username'])) if user.get('username') else 'без юзернейма'}", kb)

    return web.json_response({"ok": True, "booking": {
        "id": booking["id"], "master": master_name, "service": booking["service_title"],
        "price": booking["price"], "date": fmt_date(booking["date"]), "time": booking["time"]}})


@web.middleware
async def errors_mw(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as e:
        if request.path.startswith("/api/") and e.status >= 400 and not e.text:
            return web.json_response({"ok": False, "error": e.reason}, status=e.status)
        raise
    except Exception:
        log.exception("Ошибка в %s", request.path)
        return web.json_response({"ok": False, "error": "server_error"}, status=500)


# ==============================================================================
# 5. БОТ: МЕНЮ, NFC, ЗАПИСИ КЛИЕНТА
# ==============================================================================
def main_menu():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="💈 Онлайн-запись", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp"))],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="📍 Адрес и контакты")]])


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    args = command.args or ""
    if args.startswith(("nfc_", "review_")):
        barber_id = args.split("_", 1)[1]
        if barber_id in MASTERS:
            await db.execute("INSERT INTO nfc_scans (barber_id, user_id) VALUES (?,?)",
                             (barber_id, message.from_user.id))
            await db.commit()
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="💈 Открыть профиль мастера",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?master={barber_id}"))]])
            await message.answer(
                f"💈 <b>Вы у кресла мастера {esc(MASTERS[barber_id]['name'])}!</b>\n\n"
                "Нажмите кнопку, чтобы посмотреть профиль или оценить визит:", parse_mode="HTML", reply_markup=kb)
            await message.answer("Меню записи всегда под рукой 👇", reply_markup=main_menu())
            return
    await message.answer(
        f"Салют, {esc(message.from_user.first_name)}! 💈\n\nДобро пожаловать в <b>Daddy Barbershop</b> "
        "(г. Тирасполь).\nНажмите «Онлайн-запись», чтобы выбрать мастера и время:",
        parse_mode="HTML", reply_markup=main_menu())


@dp.message(F.text == "📍 Адрес и контакты")
async def cmd_contacts(message: types.Message):
    await message.answer(
        "📍 <b>Daddy Barbershop</b>\nг. Тирасполь, ул. 25 Октября, 94\n\n"
        "⏰ <b>График:</b> 10:00 — 21:00 ежедневно\n"
        "📸 https://www.instagram.com/daddy.barbershop.md/", parse_mode="HTML")


@dp.message(F.text == "📋 Мои записи")
@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    cur = await db.execute(
        "SELECT * FROM appointments WHERE user_id=? AND status='active' AND booking_date>=? "
        "ORDER BY booking_date, booking_time", (message.from_user.id, now().date().isoformat()))
    rows = [r for r in await cur.fetchall() if start_dt(r["booking_date"], r["booking_time"]) > now()]
    if not rows:
        return await message.answer("У вас нет предстоящих записей. Нажмите «💈 Онлайн-запись», чтобы записаться.")
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"usr_cancel_{r['id']}")]])
        await message.answer(
            f"💈 {esc(MASTERS[r['barber_id']]['name'])} • {esc(r['service_title'])} ({r['price']} руб)\n"
            f"📅 {fmt_date(r['booking_date'])} в {r['booking_time']}", parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("usr_cancel_"))
async def usr_cancel(cb: types.CallbackQuery):
    rec_id = int(cb.data.rsplit("_", 1)[1])
    cur = await db.execute("UPDATE appointments SET status='cancelled' WHERE id=? AND user_id=? AND status='active'",
                           (rec_id, cb.from_user.id))
    await db.commit()
    if not cur.rowcount:
        return await cb.answer("Запись уже неактивна", show_alert=True)
    await cb.message.edit_text(f"{cb.message.text}\n\n❌ Запись отменена.")
    await cb.answer("Отменено")
    await notify_staff(f"❌ Клиент {esc(cb.from_user.full_name)} отменил запись #{rec_id}. Слот снова свободен.")


# ==============================================================================
# 6. БОТ: ДЕЙСТВИЯ ПЕРСОНАЛА И ОПРОС ПОСЛЕ ВИЗИТА
# ==============================================================================
@dp.callback_query(F.data.startswith("adm_revdone_"))
async def adm_revdone(cb: types.CallbackQuery):
    if not is_staff(cb.from_user.id):
        return await cb.answer("Только для персонала", show_alert=True)
    rec_id = int(cb.data.rsplit("_", 1)[1])
    await db.execute("UPDATE appointments SET review_status='done' WHERE id=?", (rec_id,))
    await db.commit()
    await cb.message.edit_text(f"{cb.message.html_text}\n\n✅ <b>ОТЗЫВ ВЗЯТ НА МЕСТЕ</b>", parse_mode="HTML")
    await cb.answer("Сохранено")


@dp.callback_query(F.data.startswith("adm_cancel_"))
async def adm_cancel(cb: types.CallbackQuery):
    if not is_staff(cb.from_user.id):
        return await cb.answer("Только для персонала", show_alert=True)
    rec_id = int(cb.data.rsplit("_", 1)[1])
    cur = await db.execute("SELECT * FROM appointments WHERE id=? AND status='active'", (rec_id,))
    r = await cur.fetchone()
    if not r:
        return await cb.answer("Уже неактивна", show_alert=True)
    await db.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (rec_id,))
    await db.commit()
    await cb.message.edit_text(f"{cb.message.html_text}\n\n❌ <b>ОТМЕНЕНО ({esc(cb.from_user.full_name)})</b>",
                               parse_mode="HTML")
    await cb.answer("Запись отменена")
    try:
        await bot.send_message(r["user_id"], f"К сожалению, ваша запись на {fmt_date(r['booking_date'])} в "
                               f"{r['booking_time']} отменена барбершопом. Выберите другое время в «💈 Онлайн-запись».")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("usr_alr_"))
async def usr_already(cb: types.CallbackQuery):
    await db.execute("UPDATE appointments SET review_status='done' WHERE id=? AND user_id=?",
                     (int(cb.data.rsplit("_", 1)[1]), cb.from_user.id))
    await db.commit()
    await cb.message.edit_text("Спасибо за визит! Всегда ждём в Daddy Barbershop 💈🔥")
    await cb.answer()


@dp.callback_query(F.data.startswith("usr_bad_"))
async def usr_bad(cb: types.CallbackQuery, state: FSMContext):
    await db.execute("UPDATE appointments SET review_status='done' WHERE id=? AND user_id=?",
                     (int(cb.data.rsplit("_", 1)[1]), cb.from_user.id))
    await db.commit()
    await state.set_state(FeedbackState.waiting_for_complaint)
    await cb.message.edit_text("Нам жаль, что остались замечания. Напишите, что именно не устроило — "
                               "сообщение увидит лично управляющий:")
    await cb.answer()


@dp.message(FeedbackState.waiting_for_complaint, F.text)
async def handle_complaint(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Спасибо! Мы передали комментарий управляющему.")
    u = message.from_user
    await notify_staff("⚠️ <b>НЕГАТИВНЫЙ ОТЗЫВ</b>\n\n"
                       f"👤 {esc(u.full_name)} ({('@' + esc(u.username)) if u.username else 'без юзернейма'}, "
                       f"id <code>{u.id}</code>)\n💬 <i>{esc(message.text[:1500])}</i>")


# ----- Команды персонала -----
def staff_only(handler):
    async def wrapper(message: types.Message, *a, **kw):
        if not is_staff(message.from_user.id):
            return
        return await handler(message, *a, **kw)
    return wrapper


async def schedule_text(iso: str) -> str:
    cur = await db.execute("SELECT * FROM appointments WHERE booking_date=? AND status='active' "
                           "ORDER BY booking_time, barber_id", (iso,))
    rows = await cur.fetchall()
    if not rows:
        return f"📅 {fmt_date(iso)}: записей нет."
    lines = [f"📅 <b>Записи на {fmt_date(iso)}</b> ({len(rows)})"]
    for r in rows:
        lines.append(f"{r['booking_time']} • {esc(MASTERS[r['barber_id']]['name'])} • {esc(r['user_name'])} "
                     f"<code>{esc(r['phone'])}</code> • {esc(r['service_title'])} {r['price']}₽")
    return "\n".join(lines)


@dp.message(Command("today"))
@staff_only
async def cmd_today(message: types.Message):
    await message.answer(await schedule_text(now().date().isoformat()), parse_mode="HTML")


@dp.message(Command("tomorrow"))
@staff_only
async def cmd_tomorrow(message: types.Message):
    await message.answer(await schedule_text((now().date() + timedelta(days=1)).isoformat()), parse_mode="HTML")


@dp.message(Command("stats"))
@staff_only
async def cmd_stats(message: types.Message):
    lines = ["📊 <b>Статистика за 30 дней</b>\n"]
    for mid, m in MASTERS.items():
        c1 = await (await db.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM appointments WHERE barber_id=? "
                                     "AND status='active' AND booking_date>=date('now','-30 day') "
                                     "AND booking_date<=date('now')", (mid,))).fetchone()
        c2 = await (await db.execute("SELECT COUNT(*), ROUND(AVG(rating),2) FROM internal_reviews WHERE barber_id=? "
                                     "AND created_at>=datetime('now','-30 day')", (mid,))).fetchone()
        c3 = await (await db.execute("SELECT COUNT(*) FROM nfc_scans WHERE barber_id=? "
                                     "AND created_at>=datetime('now','-30 day')", (mid,))).fetchone()
        lines.append(f"💈 <b>{esc(m['name'])}</b>: визитов {c1[0]} ({c1[1]}₽) • отзывов {c2[0]}, "
                     f"средняя {c2[1] or '—'} • NFC-касаний {c3[0]}")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _dayoff(message: types.Message, command: CommandObject, on: bool):
    parts = (command.args or "").split()
    d = parse_ddmm(parts[1]) if len(parts) == 2 else None
    if len(parts) != 2 or parts[0] not in MASTERS or not d:
        return await message.answer(f"Формат: /{'on' if on else 'off'} yakov|ruvim|liza ДД.ММ")
    if on:
        await db.execute("DELETE FROM day_off WHERE barber_id=? AND booking_date=?", (parts[0], d.isoformat()))
    else:
        await db.execute("INSERT OR IGNORE INTO day_off VALUES (?,?)", (parts[0], d.isoformat()))
    await db.commit()
    await message.answer(f"{'✅ Запись открыта' if on else '🚫 Выходной установлен'}: "
                         f"{MASTERS[parts[0]]['name']}, {d.strftime('%d.%m')}")


@dp.message(Command("off"))
@staff_only
async def cmd_off(message: types.Message, command: CommandObject):
    await _dayoff(message, command, False)


@dp.message(Command("on"))
@staff_only
async def cmd_on(message: types.Message, command: CommandObject):
    await _dayoff(message, command, True)


@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"user_id: <code>{message.from_user.id}</code>\nchat_id: <code>{message.chat.id}</code>",
                         parse_mode="HTML")


# ==============================================================================
# 7. ФОНОВЫЙ ПЛАНИРОВЩИК (хранится в БД — переживает перезапуски)
# ==============================================================================
async def scheduler():
    while True:
        try:
            n = now()
            cur = await db.execute("SELECT * FROM appointments WHERE status='active' AND booking_date>=?",
                                   ((n - timedelta(days=2)).date().isoformat(),))
            for r in await cur.fetchall():
                start = start_dt(r["booking_date"], r["booking_time"])
                # напоминание за 2 часа
                if not r["reminded"] and timedelta(minutes=30) < start - n <= timedelta(minutes=REMIND_BEFORE_MIN):
                    await db.execute("UPDATE appointments SET reminded=1 WHERE id=?", (r["id"],))
                    await db.commit()
                    try:
                        await bot.send_message(
                            r["user_id"], f"⏰ Напоминаем: сегодня в <b>{r['booking_time']}</b> вы записаны в "
                            f"Daddy Barbershop к мастеру {esc(MASTERS[r['barber_id']]['name'])}.\n"
                            "📍 ул. 25 Октября, 94", parse_mode="HTML")
                    except Exception:
                        pass
                # опрос об отзыве — через 75 мин ПОСЛЕ начала визита
                if (not r["review_sent"] and r["review_status"] == "pending"
                        and timedelta(minutes=VISIT_MINUTES) <= n - start <= timedelta(hours=24)):
                    await db.execute("UPDATE appointments SET review_sent=1 WHERE id=?", (r["id"],))
                    await db.commit()
                    rows = []
                    if GOOGLE_REVIEW_URL:
                        rows.append([InlineKeyboardButton(text="⭐ Оставить отзыв в Google", url=GOOGLE_REVIEW_URL)])
                    rows += [[InlineKeyboardButton(text="✅ Уже оставил на месте", callback_data=f"usr_alr_{r['id']}")],
                             [InlineKeyboardButton(text="💬 Есть замечания", callback_data=f"usr_bad_{r['id']}")]]
                    try:
                        await bot.send_message(
                            r["user_id"], f"Привет, {esc(r['user_name'])}! 💈\nКак прошёл визит в "
                            "<b>Daddy Barbershop</b>? Если всё понравилось — будем благодарны за 5 звёзд в Google:",
                            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                    except Exception:
                        pass
        except Exception:
            log.exception("Ошибка планировщика")
        await asyncio.sleep(60)


# ==============================================================================
# 8. ЗАПУСК
# ==============================================================================
async def main():
    await init_db()

    app = web.Application(middlewares=[errors_mw], client_max_size=64 * 1024)
    app.router.add_get("/", serve_index)
    app.router.add_get("/webapp", serve_index)
    app.router.add_get("/health", api_health)
    app.router.add_static("/static/", os.path.join(os.path.dirname(__file__), "static"), follow_symlinks=False)
    app.router.add_get("/api/slots", api_slots)
    app.router.add_get("/api/user-profile", api_user_profile)
    app.router.add_get("/api/reviews", api_get_reviews)
    app.router.add_post("/api/reviews", api_post_review)
    app.router.add_post("/api/book", api_book)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Веб-сервер на порту %s", PORT)

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Запись", web_app=WebAppInfo(
            url=f"{WEBAPP_URL}/webapp")))
    except Exception as e:
        log.warning("Menu button: %s", e)

    sched = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        sched.cancel()
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
