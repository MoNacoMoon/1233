import asyncio
import html
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# ==============================================================================
# 1. КОНФИГУРАЦИЯ
# ==============================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8928081500:AAFhxVBjkp4GLOIpzn7R4i4PtF0xkJ0Oqk0")
STAFF_IDS = [int(x.strip()) for x in os.environ.get("STAFF_IDS", "5027569984").split(",") if x.strip()]
BARBERSHOP_GROUP_ID = int(os.environ.get("BARBERSHOP_GROUP_ID", "-1004483657150"))

WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://whisking-egomaniac-canning.ngrok-free.dev").rstrip("/")
GOOGLE_REVIEW_URL = "https://search.google.com/local/writereview?placeid=ChIJaTnhLewDyUARJBmIh1gWXuc"
GOOGLE_MAPS_URL = "https://www.google.com/maps/search/?api=1&query=Daddy+Barbershop&query_place_id=ChIJaTnhLewDyUARJBmIh1gWXuc"
ALTEGIO_URL = "https://n807184.alteg.io/"
INSTAGRAM_URL = "https://www.instagram.com/daddy.barbershop.md/"

DEFAULT_MASTER_ID = "yakov"
DEFAULT_MASTER_NAME = "Yakov"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class FeedbackState(StatesGroup):
    waiting_for_complaint = State()
    waiting_for_block_time = State()


# ==============================================================================
# 2. БАЗА ДАННЫХ SQLITE (barbershop.db)
# ==============================================================================
DB_PATH = os.environ.get("DB_PATH", "/data/barbershop.db" if os.path.exists("/data") else "barbershop.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Таблица постоянных профилей клиентов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица записей на стрижку
    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            phone TEXT,
            barber TEXT,
            service TEXT,
            booking_date TEXT,
            booking_time TEXT,
            status TEXT DEFAULT 'active',
            review_status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            extra_services TEXT DEFAULT '',
            total_price INTEGER DEFAULT 0,
            reminded_2h INTEGER DEFAULT 0,
            reminded_21d INTEGER DEFAULT 0,
            confirmed INTEGER DEFAULT 0
        )
    """)

    # Таблица внутренних отзывов по мастерам
    cur.execute("""
        CREATE TABLE IF NOT EXISTS internal_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            barber_id TEXT,
            rating INTEGER,
            tag TEXT,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица жалоб и негативных отзывов (<3 звезд, видны только админу)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            client_name TEXT,
            phone TEXT,
            barber_id TEXT,
            rating INTEGER,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица заблокированных слотов (перерывы)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blocked_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barber TEXT,
            booking_date TEXT,
            booking_time TEXT,
            reason TEXT DEFAULT 'Перерыв',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица галереи работ из Instagram (@daddy.barbershop.md)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instagram_works (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_url TEXT NOT NULL,
            caption TEXT,
            tag TEXT DEFAULT 'Стрижка',
            barber_name TEXT DEFAULT 'Yakov',
            instagram_url TEXT DEFAULT 'https://www.instagram.com/daddy.barbershop.md/',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Начальные работы из Instagram
    cur.execute("SELECT COUNT(*) FROM instagram_works")
    if cur.fetchone()[0] == 0:
        cur.executemany("""
            INSERT INTO instagram_works (image_url, caption, tag, barber_name, instagram_url)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("/static/img/portfolio/work1.jpg", "Низкий Fade с четкой геометрией", "Fade", "Yakov", INSTAGRAM_URL),
            ("/static/img/portfolio/work2.jpg", "Моделирование бороды опасной бритвой", "Борода", "Yakov", INSTAGRAM_URL),
            ("/static/img/portfolio/work3.jpg", "Текстурный Кроп и укладка матовой пастой", "Стрижка", "Yakov", INSTAGRAM_URL),
            ("/static/img/portfolio/work4.jpg", "Низкий Taper Fade и оформление контуров", "Fade", "Daddy Team", INSTAGRAM_URL),
            ("/static/img/portfolio/work5.jpg", "Классический Помпадур с пробором", "Классика", "Daddy Team", INSTAGRAM_URL),
            ("/static/img/portfolio/work6.jpg", "Фирменное бритье с распариванием", "Бритье", "Daddy Team", INSTAGRAM_URL)
        ])

    # Стартовые отзывы
    cur.execute("SELECT COUNT(*) FROM internal_reviews")
    if cur.fetchone()[0] == 0:
        cur.executemany("""
            INSERT INTO internal_reviews (user_id, user_name, barber_id, rating, tag, comment)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            (0, "Максим", "yakov", 5, "Стрижка", "Яков сделал идеальный переход, борода на высшем уровне. Топовый мастер!"),
            (0, "Дмитрий", "yakov", 5, "Борода", "Четкие контуры опасной бритвой. Лучший сервис в городе."),
            (0, "Александр", "yakov", 5, "Скорость", "Быстро, уверенно, идеальная форма. Стригусь только у Якова."),
            (0, "Игорь", "yakov", 5, "Сервис", "Вкусный кофе, крутая атмосфера и мастерство на высоте."),
            (0, "Артем", "yakov", 5, "Стрижка", "Переход безупречный. Смело рекомендую всем знакомым.")
        ])

    conn.commit()
    conn.close()


def save_or_update_client_profile(user_id: int, name: str, phone: str):
    if not user_id:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO client_profiles (user_id, full_name, phone, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name = excluded.full_name,
            phone = excluded.phone,
            updated_at = CURRENT_TIMESTAMP
    """, (user_id, name, phone))
    conn.commit()
    conn.close()


def get_client_profile(user_id: int):
    if not user_id:
        return None
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT full_name, phone FROM client_profiles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM appointments WHERE user_id = ? AND status = 'active'", (user_id,))
    visits_count = cur.fetchone()[0]
    conn.close()
    if row:
        return {"full_name": row[0], "phone": row[1], "visits_count": visits_count}
    return {"visits_count": visits_count}


def save_appointment(user_id, name, phone, barber, service, date, time, extra_services="", total_price=0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO appointments (
            user_id, user_name, phone, barber, service, booking_date, booking_time,
            extra_services, total_price, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
    """, (user_id, name, phone, barber, service, date, time, extra_services, total_price))
    conn.commit()
    rec_id = cur.lastrowid
    conn.close()
    return rec_id


def get_booked_times(barber: str, date_str: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if barber == "Любой мастер":
        cur.execute("""
            SELECT booking_time FROM appointments 
            WHERE booking_date = ? AND status = 'active'
        """, (date_str,))
    else:
        cur.execute("""
            SELECT booking_time FROM appointments 
            WHERE barber = ? AND booking_date = ? AND status = 'active'
        """, (barber, date_str))
    busy_app = [r[0] for r in cur.fetchall()]

    cur.execute("""
        SELECT booking_time FROM blocked_slots
        WHERE (barber = ? OR barber = 'all') AND booking_date = ?
    """, (barber, date_str))
    busy_blocked = [r[0] for r in cur.fetchall()]

    conn.close()
    return list(set(busy_app + busy_blocked))


def get_user_active_bookings(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, barber, service, booking_date, booking_time, extra_services, total_price, status
        FROM appointments
        WHERE user_id = ? AND status = 'active'
        ORDER BY id DESC LIMIT 3
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "barber": r[1],
            "service": r[2],
            "date": r[3],
            "time": r[4],
            "extra_services": r[5] or "",
            "total_price": r[6] or 0,
            "status": r[7]
        }
        for r in rows
    ]


def cancel_booking_in_db(record_id: int, user_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if user_id:
        cur.execute("SELECT id, barber, booking_date, booking_time, user_name FROM appointments WHERE id = ? AND user_id = ?", (record_id, user_id))
    else:
        cur.execute("SELECT id, barber, booking_date, booking_time, user_name FROM appointments WHERE id = ?", (record_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    cur.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return {"id": row[0], "barber": row[1], "date": row[2], "time": row[3], "user_name": row[4]}


def mark_review_done(record_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE appointments SET review_status = 'done' WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()


def is_review_needed(record_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT status, review_status FROM appointments WHERE id = ?", (record_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] == 'active' and row[1] == 'pending')


def save_master_rating(user_id, user_name, barber_id, rating, tag=None, comment=None):
    profile = get_client_profile(user_id) if user_id else None
    actual_name = profile["full_name"] if (profile and profile.get("full_name")) else user_name

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO internal_reviews (user_id, user_name, barber_id, rating, tag, comment)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, actual_name, barber_id, rating, tag, comment))
    conn.commit()
    conn.close()
    return actual_name


def get_master_reviews_db(barber_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_name, rating, tag, comment, strftime('%d.%m', created_at)
        FROM internal_reviews
        WHERE barber_id = ? AND rating >= 3
        ORDER BY id DESC LIMIT 15
    """, (barber_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"author": r[0], "rating": r[1], "tag": r[2], "text": r[3], "date": r[4]} for r in rows]


# ==============================================================================
# 3. МЕНЮ И КОМАНДЫ TELEGRAM
# ==============================================================================
def get_main_menu():
    kb = [
        [KeyboardButton(text="💈 Запись к Yakov (Mini App)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?master=yakov"))],
        [KeyboardButton(text="📍 Адрес и контакты"), KeyboardButton(text="👤 Мой визит")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    args = command.args

    # Поддержка NFC-меток у кресла мастера: /start nfc_yakov
    if args and (args.startswith("nfc_") or args.startswith("review_")):
        barber_id = args.replace("nfc_", "").replace("review_", "")
        url = f"{WEBAPP_URL}/webapp?master={barber_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💈 Открыть профиль и отзывы", web_app=WebAppInfo(url=url))]
        ])
        return await message.answer(
            f"💈 <b>Вы у кресла топ-барбера {DEFAULT_MASTER_NAME}!</b>\n\n"
            "Нажмите кнопку ниже, чтобы посмотреть работы, прайс или оставить отзыв:",
            reply_markup=kb,
            parse_mode="HTML"
        )

    await message.answer(
        f"Салют, {message.from_user.first_name}! 💈\n\n"
        "Добро пожаловать в <b>Daddy Barbershop</b> (г. Тирасполь).\n"
        f"⚡ <b>Открыта эксклюзивная онлайн-запись к топ-барберу {DEFAULT_MASTER_NAME}</b>.\n\n"
        "Нажмите кнопку ниже, чтобы выбрать удобное время в Telegram Mini App:",
        reply_markup=get_main_menu(),
        parse_mode="HTML"
    )


@dp.message(F.text == "📍 Адрес и контакты")
async def cmd_contacts(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺️ Маршрут в Google Maps", url=GOOGLE_MAPS_URL)],
        [InlineKeyboardButton(text="💈 Записаться в Mini App", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?master=yakov"))]
    ])
    await message.answer(
        "📍 <b>Daddy Barbershop</b>\n"
        "г. Тирасполь, ул. 25 Октября, 94\n\n"
        "⏰ <b>График работы:</b> 10:00 — 21:00 ежедневно\n"
        f"💈 <b>Топ-барбер салона:</b> {DEFAULT_MASTER_NAME} (@_yakov_barber_)\n"
        "📸 <b>Instagram салона:</b> https://www.instagram.com/daddy.barbershop.md/\n"
        "🔗 <b>Запись к остальным мастерам (Altegio):</b> https://n807184.alteg.io/",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.message(F.text == "👤 Мой визит")
async def cmd_my_visit(message: types.Message):
    bookings = get_user_active_bookings(message.from_user.id)
    if not bookings:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💈 Записаться к {DEFAULT_MASTER_NAME}", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?master=yakov"))]
        ])
        return await message.answer(
            "У вас пока нет активных записей на стрижку.\n\nХотите выбрать время?",
            reply_markup=kb
        )

    b = bookings[0]
    extra_info = f"\n➕ <b>Доп. услуги:</b> {b['extra_services']}" if b['extra_services'] else ""
    price_info = f"\n💰 <b>Итого:</b> {b['total_price']} руб" if b['total_price'] else ""

    text = (
        "🎫 <b>ВАШ АКТИВНЫЙ ВИЗИТ:</b>\n\n"
        f"💈 <b>Мастер:</b> {b['barber']}\n"
        f"✂️ <b>Услуга:</b> {b['service']}{extra_info}{price_info}\n"
        f"📅 <b>Дата и время:</b> {b['date']} в {b['time']}\n"
        "📍 <b>Адрес:</b> г. Тирасполь, ул. 25 Октября, 94\n\n"
        "Ждём вас!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"usr_cancel_{b['id']}")],
        [InlineKeyboardButton(text="📱 Открыть в Mini App", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?tab=profile"))]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ==============================================================================
# 4. ДОБАВЛЕНИЕ НОВЫХ РАБОТ ИЗ INSTAGRAM ЧЕРЕЗ БОТА (/add_work ИЛИ ФОТО)
# ==============================================================================
def is_admin(user_id: int) -> bool:
    return user_id in STAFF_IDS


@dp.message(Command("add_work"))
async def cmd_add_work(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id) and message.chat.id != BARBERSHOP_GROUP_ID:
        return await message.answer("🔒 Доступ только для сотрудников.")
    args = (command.args or "").strip()
    if not args:
        return await message.answer(
            "ℹ️ <b>Формат команды:</b>\n"
            "<code>/add_work URL_картинки Описание</code>\n\n"
            "<i>Или просто отправьте сюда фото стрижки с подписью — бот сразу опубликует его в Mini App!</i>",
            parse_mode="HTML"
        )
    parts = args.split(" ", 1)
    img_url = parts[0]
    caption = parts[1] if len(parts) > 1 else "Новая работа мастера"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO instagram_works (image_url, caption, tag, barber_name, instagram_url)
        VALUES (?, ?, ?, ?, ?)
    """, (img_url, caption, "Стрижка", DEFAULT_MASTER_NAME, INSTAGRAM_URL))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Работа успешно добавлена в галерею Mini App!\n📸 <i>{caption}</i>", parse_mode="HTML")


@dp.message(F.photo)
async def handle_staff_photo(message: types.Message):
    """Сотрудник или мастер может просто прислать фото в чат бота, и оно появится в Mini App!"""
    if not is_admin(message.from_user.id) and message.chat.id != BARBERSHOP_GROUP_ID:
        return

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    filename = f"insta_{int(datetime.now().timestamp())}.jpg"
    local_dir = os.path.join(os.path.dirname(__file__), "static", "img", "portfolio")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, filename)

    await bot.download_file(file_info.file_path, local_path)
    caption = message.caption or "Новая работа из Instagram"
    tag = "Fade" if "fade" in caption.lower() else ("Борода" if "бород" in caption.lower() else "Стрижка")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO instagram_works (image_url, caption, tag, barber_name, instagram_url)
        VALUES (?, ?, ?, ?, ?)
    """, (f"/static/img/portfolio/{filename}", caption, tag, DEFAULT_MASTER_NAME, INSTAGRAM_URL))
    conn.commit()
    conn.close()

    await message.answer(
        f"🔥 <b>Фотография моментально добавлена в ленту работ Mini App!</b>\n\n"
        f"🏷️ Категория: {tag}\n"
        f"💬 Описание: <i>{caption}</i>\n"
        "Клиенты уже видят её в приложении 📸",
        parse_mode="HTML"
    )


# ==============================================================================
# 5. ПАНЕЛЬ УПРАВЛЕНИЯ /admin
# ==============================================================================
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id) and message.chat.id != BARBERSHOP_GROUP_ID:
        return await message.answer("🔒 Доступ только для сотрудников и мастера.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Расписание на сегодня", callback_data="adm_schedule_today")],
        [InlineKeyboardButton(text="💰 Выручка за сегодня", callback_data="adm_revenue_today")],
        [InlineKeyboardButton(text="🔒 Закрыть слот (Перерыв)", callback_data="adm_block_slot")],
        [InlineKeyboardButton(text="📊 Статистика записей Yakov", callback_data="adm_pilot_stats")]
    ])
    await message.answer("⚡ <b>Панель управления Daddy Barbershop</b>\nВыберите действие:", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_schedule_today")
async def adm_schedule_today_handler(callback: types.CallbackQuery):
    today_str = datetime.now().strftime("%d.%m")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, booking_time, user_name, phone, service, total_price, extra_services
        FROM appointments
        WHERE booking_date = ? AND barber = ? AND status = 'active'
        ORDER BY booking_time ASC
    """, (today_str, DEFAULT_MASTER_NAME))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text(f"📋 На сегодня ({today_str}) записей к {DEFAULT_MASTER_NAME} пока нет.")
        return await callback.answer()

    text = f"📋 <b>Расписание на сегодня ({today_str}) • Мастер {DEFAULT_MASTER_NAME}:</b>\n\n"
    for r in rows:
        extras = f" (+{r[6]})" if r[6] else ""
        price = f" — <b>{r[5]} руб</b>" if r[5] else ""
        text += (
            f"⏰ <b>{r[1]}</b> — {r[2]} (<code>{r[3]}</code>)\n"
            f"   ✂️ {r[4]}{extras}{price}\n\n"
        )

    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm_revenue_today")
async def adm_revenue_today_handler(callback: types.CallbackQuery):
    today_str = datetime.now().strftime("%d.%m")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), SUM(total_price)
        FROM appointments
        WHERE booking_date = ? AND barber = ? AND status = 'active'
    """, (today_str, DEFAULT_MASTER_NAME))
    count, total = cur.fetchone()
    conn.close()

    total = total or 0
    text = (
        f"💰 <b>Касса на сегодня ({today_str}) • {DEFAULT_MASTER_NAME}:</b>\n\n"
        f"Записей: <b>{count}</b>\n"
        f"Ожидаемая выручка: <b>{total} руб</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm_pilot_stats")
async def adm_pilot_stats_handler(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(total_price) FROM appointments WHERE barber = ? AND status = 'active'", (DEFAULT_MASTER_NAME,))
    active_count, active_total = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM appointments WHERE barber = ? AND extra_services != '' AND extra_services IS NOT NULL", (DEFAULT_MASTER_NAME,))
    upsell_count = cur.fetchone()[0]
    conn.close()

    active_total = active_total or 0
    avg_check = round(active_total / active_count) if active_count else 0
    upsell_percent = round((upsell_count / active_count) * 100) if active_count else 0

    text = (
        f"📊 <b>Статистика записей мастера {DEFAULT_MASTER_NAME}:</b>\n\n"
        f"Всего активных записей: <b>{active_count}</b>\n"
        f"Общая выручка: <b>{active_total} руб</b>\n"
        f"Средний чек: <b>{avg_check} руб</b>\n"
        f"Записей с допродажами (Upsell): <b>{upsell_count} ({upsell_percent}%)</b>\n\n"
        "<i>Mini App экономит время администратора и увеличивает средний чек!</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm_block_slot")
async def adm_block_slot_handler(callback: types.CallbackQuery):
    today_str = datetime.now().strftime("%d.%m")
    slots = ["10:00", "11:15", "12:30", "13:45", "15:00", "16:15", "17:30", "18:45", "20:00"]
    buttons = []
    for s in slots:
        buttons.append([InlineKeyboardButton(text=f"🔒 Блок {s} (сегодня)", callback_data=f"do_block_{s}_{today_str}")])
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="adm_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("Выберите слот на сегодня для закрытия перерывом:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("do_block_"))
async def do_block_slot_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    slot_time = parts[2]
    slot_date = parts[3]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO blocked_slots (barber, booking_date, booking_time, reason) VALUES (?, ?, ?, ?)", (DEFAULT_MASTER_NAME, slot_date, slot_time, "Перерыв мастера"))
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"✅ Слот <b>{slot_time}</b> на дату {slot_date} успешно закрыт!", parse_mode="HTML")
    await callback.answer("Слот заблокирован!")


# ==============================================================================
# 6. ПРИЕМ БРОНИРОВАНИЯ ИЗ MINI APP
# ==============================================================================
@dp.message(F.web_app_data)
async def handle_webapp_data(message: types.Message):
    data = json.loads(message.web_app_data.data)
    user_id = data.get("user_id") or message.from_user.id
    client_name = data.get("client_name") or message.from_user.full_name
    phone = data.get("phone", "Не указан")
    barber = data.get("barber") or DEFAULT_MASTER_NAME
    service = data.get("service", "Стрижка")
    price_str = str(data.get("price", "200 руб"))
    date = data.get("date")
    time = data.get("time")
    extra_services = data.get("extra_services", "")
    total_price = int(data.get("total_price", 0)) or int("".join(filter(str.isdigit, price_str)) or 200)

    # Закрепляем имя и телефон в базе
    save_or_update_client_profile(user_id, client_name, phone)

    # Сохраняем в таблицу записей
    rec_id = save_appointment(
        user_id=user_id,
        name=client_name,
        phone=phone,
        barber=barber,
        service=service,
        date=date,
        time=time,
        extra_services=extra_services,
        total_price=total_price
    )

    # Подтверждение для клиента
    extra_line = f"\n➕ <b>Доп. услуги:</b> {extra_services}" if extra_services else ""
    client_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺️ Маршрут в Google Maps", url=GOOGLE_MAPS_URL)],
        [InlineKeyboardButton(text="📱 Открыть билет в Mini App", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?tab=profile"))]
    ])
    await message.answer(
        "🔥 <b>Запись успешно подтверждена!</b>\n\n"
        f"💈 <b>Мастер:</b> {barber}\n"
        f"✂️ <b>Услуга:</b> {service}{extra_line}\n"
        f"💰 <b>Сумма к оплате:</b> <b>{total_price} руб</b>\n"
        f"📅 <b>Время:</b> {date} в {time}\n"
        f"👤 <b>Клиент:</b> {client_name}\n"
        f"📞 <b>Телефон:</b> {phone}\n\n"
        "📍 Ждём вас по адресу: ул. 25 Октября, 94!\n"
        "<i>Напоминание о визите придет за 2 часа прямо сюда в Telegram.</i>",
        parse_mode="HTML",
        reply_markup=client_kb
    )

    # Карточка администратору / в группу салона
    staff_card = (
        f"⚡ <b>НОВАЯ ЗАПИСЬ #{rec_id} (DADDY BARBERSHOP)</b>\n\n"
        f"💈 <b>Мастер:</b> {barber}\n"
        f"✂️ <b>Услуга:</b> {service}\n"
        f"➕ <b>Доп. услуги:</b> {extra_services or 'Нет'}\n"
        f"💰 <b>Итого чек:</b> <b>{total_price} руб</b>\n"
        f"📅 <b>Время:</b> {date} в {time}\n"
        f"👤 <b>Клиент:</b> {client_name}\n"
        f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
        f"💬 <b>Телеграм:</b> @{message.from_user.username or 'нет юзернейма'}"
    )
    staff_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отзыв взят на месте", callback_data=f"adm_revdone_{rec_id}")]
    ])

    target = BARBERSHOP_GROUP_ID if BARBERSHOP_GROUP_ID else STAFF_IDS[0]
    await bot.send_message(chat_id=target, text=staff_card, parse_mode="HTML", reply_markup=staff_kb)

    # Запуск опроса через 1.5 часа
    asyncio.create_task(schedule_review_poll(rec_id, message.from_user.id, client_name, delay_seconds=5400))


@dp.callback_query(F.data.startswith("adm_revdone_"))
async def admin_review_clicked(callback: types.CallbackQuery):
    rec_id = int(callback.data.replace("adm_revdone_", ""))
    mark_review_done(rec_id)
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>ОТЗЫВ ВЗЯТ НА МЕСТЕ (NFC)</b>", parse_mode="HTML")
    await callback.answer("Отметка сохранена!")


@dp.callback_query(F.data.startswith("usr_confirm_"))
async def user_confirm_appointment(callback: types.CallbackQuery):
    rec_id = int(callback.data.replace("usr_confirm_", ""))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE appointments SET confirmed = 1 WHERE id = ?", (rec_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text("✅ <b>Отлично, ждём вас точно ко времени!</b> 💈 До встречи в Daddy Barbershop!", parse_mode="HTML")
    await callback.answer("Визит подтвержден!")


@dp.callback_query(F.data.startswith("usr_cancel_"))
async def user_cancel_appointment(callback: types.CallbackQuery):
    rec_id = int(callback.data.replace("usr_cancel_", ""))
    cancelled_data = cancel_booking_in_db(rec_id, callback.from_user.id)
    if cancelled_data:
        await callback.message.edit_text("Запись отменена. Слот освобожден для других клиентов 🤝", parse_mode="HTML")
        alert = (
            f"⚠️ <b>КЛИЕНТ ОТМЕНИЛ ЗАПИСЬ #{rec_id}</b>\n\n"
            f"👤 Клиент: {cancelled_data['user_name']}\n"
            f"💈 Мастер: {cancelled_data['barber']}\n"
            f"📅 Время: {cancelled_data['date']} в {cancelled_data['time']}\n"
            "<i>Слот снова свободен для бронирования.</i>"
        )
        target = BARBERSHOP_GROUP_ID if BARBERSHOP_GROUP_ID else STAFF_IDS[0]
        await bot.send_message(chat_id=target, text=alert, parse_mode="HTML")
    else:
        await callback.message.edit_text("Запись уже была изменена или отменена ранее.")
    await callback.answer()


async def schedule_review_poll(record_id: int, user_id: int, user_name: str, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    if not is_review_needed(record_id):
        return

    text = (
        f"Привет, {user_name}! 💈\n"
        "Ты недавно был на стрижке в <b>Daddy Barbershop</b>. Как оценишь визит?\n\n"
        "Если уже приложил NFC-карту на месте — спасибо от души! 🤝\n"
        "Если не успел — будем благодарны за 5 звёзд на картах Google:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Оставить отзыв в Google (5 звёзд)", url=GOOGLE_REVIEW_URL)],
        [InlineKeyboardButton(text="✅ Уже оставил на месте", callback_data=f"usr_alr_{record_id}")],
        [InlineKeyboardButton(text="💬 Есть замечания по сервису", callback_data=f"usr_bad_{record_id}")]
    ])
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка отправки фонового опроса: {e}")


@dp.callback_query(F.data.startswith("usr_alr_"))
async def user_review_already(callback: types.CallbackQuery):
    rec_id = int(callback.data.replace("usr_alr_", ""))
    mark_review_done(rec_id)
    await callback.message.edit_text("Спасибо за визит! Всегда ждем в Daddy Barbershop 💈🔥")
    await callback.answer()


@dp.callback_query(F.data.startswith("usr_bad_"))
async def user_bad_feedback(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackState.waiting_for_complaint)
    await callback.message.edit_text(
        "Нам искренне жаль, что возникли замечания.\n\n"
        "Напишите прямо сюда, что именно вас расстроило? Сообщение увидит лично управляющий:"
    )
    await callback.answer()


@dp.message(FeedbackState.waiting_for_complaint)
async def handle_complaint(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Спасибо! Мы передали ваш комментарий управляющему для решения вопроса.")

    alert = (
        "⚠️ <b>НЕГАТИВНЫЙ ОТЗЫВ ОТ КЛИЕНТА!</b>\n\n"
        f"👤 <b>Клиент:</b> {message.from_user.full_name} (@{message.from_user.username or 'нет'})\n"
        f"💬 <b>Текст:</b>\n<i>{message.text}</i>"
    )
    target = BARBERSHOP_GROUP_ID if BARBERSHOP_GROUP_ID else STAFF_IDS[0]
    await bot.send_message(chat_id=target, text=alert, parse_mode="HTML")


# ==============================================================================
# 7. ФОНОВЫЙ ПЛАНИРОВЩИК (НАПОМИНАНИЯ ЗА 2 ЧАСА И ЧЕРЕЗ 21 ДЕНЬ)
# ==============================================================================
async def background_scheduler():
    while True:
        try:
            now_dt = datetime.now()
            today_str = now_dt.strftime("%d.%m")

            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            # 1. Напоминания за 2 часа до визита
            cur.execute("""
                SELECT id, user_id, user_name, barber, booking_time, service
                FROM appointments
                WHERE booking_date = ? AND status = 'active' AND reminded_2h = 0
            """, (today_str,))
            today_rows = cur.fetchall()

            for app_id, u_id, u_name, barber, b_time, srv in today_rows:
                try:
                    time_parts = b_time.split(":")
                    app_time = now_dt.replace(hour=int(time_parts[0]), minute=int(time_parts[1]), second=0)
                    time_diff = (app_time - now_dt).total_seconds() / 60.0

                    if 0 < time_diff <= 130:
                        cur.execute("UPDATE appointments SET reminded_2h = 1 WHERE id = ?", (app_id,))
                        conn.commit()

                        text = (
                            f"Привет, {u_name}! 💈\n"
                            f"Напоминаем: сегодня в <b>{b_time}</b> ждём тебя в <b>Daddy Barbershop</b> "
                            f"к мастеру <b>{barber}</b>!\n\n"
                            "📍 г. Тирасполь, ул. 25 Октября, 94\n"
                            "Подтвердите, пожалуйста, ваш визит:"
                        )
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Буду точно", callback_data=f"usr_confirm_{app_id}")],
                            [InlineKeyboardButton(text="❌ Не смогу прийти", callback_data=f"usr_cancel_{app_id}")]
                        ])
                        await bot.send_message(chat_id=u_id, text=text, parse_mode="HTML", reply_markup=kb)
                except Exception as ex:
                    logging.error(f"Error sending 2h reminder for app {app_id}: {ex}")

            # 2. Напоминание через 21 день ("Твоя стрижка требует обновления")
            cur.execute("""
                SELECT id, user_id, user_name, barber, created_at
                FROM appointments
                WHERE status = 'active' AND reminded_21d = 0
            """)
            past_rows = cur.fetchall()

            for app_id, u_id, u_name, barber, created_at_str in past_rows:
                try:
                    created_dt = datetime.strptime(created_at_str[:19], "%Y-%m-%d %H:%M:%S")
                    if (now_dt - created_dt).days >= 21:
                        cur.execute("UPDATE appointments SET reminded_21d = 1 WHERE id = ?", (app_id,))
                        conn.commit()

                        retention_text = (
                            f"Салют, {u_name}! 💈\n\n"
                            f"Прошло ровно 3 недели с твоего визита к мастеру <b>{barber}</b>.\n"
                            "<b>Твоя стрижка требует обновления ✂️</b>\n\n"
                            "Забронировать удобное время на ближайшие дни?"
                        )
                        retention_kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text=f"💈 Записаться к {barber}",
                                web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?master=yakov")
                            )]
                        ])
                        await bot.send_message(chat_id=u_id, text=retention_text, parse_mode="HTML", reply_markup=retention_kb)
                except Exception as ex:
                    logging.error(f"Error sending 21d retention reminder for app {app_id}: {ex}")

            conn.close()
        except Exception as e:
            logging.error(f"Error in background scheduler: {e}")

        await asyncio.sleep(60)


# ==============================================================================
# 8. REST API ЭНДПОИНТЫ ДЛЯ MINI APP
# ==============================================================================
async def serve_index(request):
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    return web.FileResponse(path)


async def api_get_busy_slots(request):
    barber = request.query.get("barber", DEFAULT_MASTER_NAME)
    date = request.query.get("date", "")
    busy = get_booked_times(barber, date)
    return web.json_response({"busy": busy})


async def api_get_user_profile(request):
    user_id_str = request.query.get("user_id")
    if not user_id_str:
        return web.json_response({})
    try:
        user_id = int(user_id_str)
        profile = get_client_profile(user_id)
        return web.json_response(profile or {})
    except ValueError:
        return web.json_response({})


async def api_get_my_bookings(request):
    user_id_str = request.query.get("user_id")
    if not user_id_str:
        return web.json_response({"bookings": []})
    try:
        user_id = int(user_id_str)
        bookings = get_user_active_bookings(user_id)
        return web.json_response({"bookings": bookings})
    except ValueError:
        return web.json_response({"bookings": []})


async def api_cancel_booking(request):
    try:
        data = await request.json()
        app_id = int(data.get("appointment_id", 0))
        user_id = int(data.get("user_id", 0))
        cancelled = cancel_booking_in_db(app_id, user_id)
        if cancelled:
            alert = (
                f"⚠️ <b>ОТМЕНА ЗАПИСИ ИЗ MINI APP #{app_id}</b>\n\n"
                f"👤 Клиент: {cancelled['user_name']}\n"
                f"💈 Мастер: {cancelled['barber']}\n"
                f"📅 Время: {cancelled['date']} в {cancelled['time']}\n"
                "<i>Слот снова свободен.</i>"
            )
            target = BARBERSHOP_GROUP_ID if BARBERSHOP_GROUP_ID else STAFF_IDS[0]
            await bot.send_message(chat_id=target, text=alert, parse_mode="HTML")
            return web.json_response({"status": "ok"})
        return web.json_response({"status": "error", "message": "Запись не найдена"}, status=404)
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)


async def api_get_portfolio(request):
    """Возвращает живую ленту работ мастеров из Instagram."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, image_url, caption, tag, barber_name, instagram_url FROM instagram_works ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    works = [
        {
            "id": r[0],
            "image_url": r[1],
            "caption": r[2],
            "tag": r[3],
            "barber": r[4],
            "instagram_url": r[5]
        }
        for r in rows
    ]
    return web.json_response({"works": works})


async def api_get_reviews(request):
    barber_id = request.query.get("barber", DEFAULT_MASTER_ID)
    reviews = get_master_reviews_db(barber_id)
    return web.json_response({"reviews": reviews})


async def api_post_review(request):
    try:
        body = await request.json()
        user_id = body.get("user_id", 0)
        author = body.get("author", "Клиент")
        phone = body.get("phone", "")
        barber_id = body.get("barber_id", DEFAULT_MASTER_ID)
        rating = int(body.get("rating", 5))
        tag = body.get("tag", "Стрижка")
        text = body.get("text", "")
        is_complaint = body.get("is_complaint", False) or (rating < 3)

        # ЕСЛИ ОЦЕНКА МЕНЬШЕ 3 ЗВЕЗД ИЛИ ЭТО ЖАЛОБА — НЕ ПУБЛИКУЕМ НА СТЕНУ!
        if is_complaint or rating < 3:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO complaints (user_id, client_name, phone, barber_id, rating, comment)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, author, phone, barber_id, rating, text))
            conn.commit()
            conn.close()

            alert = (
                "⚠️ <b>НЕГАТИВНЫЙ ОТЗЫВ / ПРЕТЕНЗИЯ В MINI APP!</b>\n\n"
                f"💈 <b>Барбер:</b> {barber_id.upper()}\n"
                f"⭐ <b>Оценка:</b> {'★' * rating}{'☆' * (5 - rating)} ({rating}/5)\n"
                f"👤 <b>Клиент:</b> {author}\n"
                f"📞 <b>Телефон:</b> <code>{phone or 'Не указан'}</code>\n"
                f"💬 <b>Что произошло:</b>\n<i>{text}</i>\n\n"
                "🔒 <i>(Отзыв СКРЫТ от стены приложения и отправлен только руководству салона)</i>"
            )
            target = BARBERSHOP_GROUP_ID if BARBERSHOP_GROUP_ID else STAFF_IDS[0]
            try:
                await bot.send_message(chat_id=target, text=alert, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Error sending complaint telegram alert: {e}")
                if STAFF_IDS and target != STAFF_IDS[0]:
                    try:
                        await bot.send_message(chat_id=STAFF_IDS[0], text=alert, parse_mode="HTML")
                    except Exception:
                        pass

            return web.json_response({"status": "complaint_handled", "author": author})

        # Положительные отзывы (3, 4, 5 звезд) — публикуются на стене мастера
        final_author_name = save_master_rating(
            user_id=user_id,
            user_name=author,
            barber_id=barber_id,
            rating=rating,
            tag=tag,
            comment=text
        )

        alert = (
            "🌟 <b>НОВЫЙ ОТЗЫВ МАСТЕРУ В MINI APP!</b>\n\n"
            f"💈 <b>Барбер:</b> {barber_id.upper()}\n"
            f"⭐ <b>Оценка:</b> {'★' * rating}{'☆' * (5 - rating)} ({rating}/5)\n"
            f"🏷️ <b>Категория:</b> {tag}\n"
            f"👤 <b>Клиент:</b> {final_author_name}\n"
            f"💬 <b>Отзыв:</b> {text}\n"
            "<i>(Опубликован в приложении)</i>"
        )
        target = BARBERSHOP_GROUP_ID if BARBERSHOP_GROUP_ID else STAFF_IDS[0]
        try:
            await bot.send_message(chat_id=target, text=alert, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Error sending review telegram alert: {e}")

        return web.json_response({"status": "ok", "author": final_author_name})
    except Exception as ex:
        logging.error(f"Error in api_post_review: {ex}")
        return web.json_response({"status": "error", "message": str(ex)}, status=500)


async def api_serve_calendar_ics(request):
    """Генерирует стандартный .ics файл для мгновенного добавления в Apple / Google календарь."""
    service = request.query.get("service", "Стрижка")
    barber = request.query.get("barber", "Yakov")
    date_str = request.query.get("date", "")
    time_str = request.query.get("time", "15:00")
    price = request.query.get("price", "200 руб")

    now_dt = datetime.now()
    year = now_dt.year
    try:
        day, month = [int(x) for x in date_str.split(".")]
        hour, minute = [int(x) for x in time_str.split(":")]
        dt_start = datetime(year, month, day, hour, minute)
        if dt_start < now_dt - timedelta(days=30):
            dt_start = datetime(year + 1, month, day, hour, minute)
        dt_end = dt_start + timedelta(minutes=75)
        dt_start_str = dt_start.strftime("%Y%m%dT%H%M%S")
        dt_end_str = dt_end.strftime("%Y%m%dT%H%M%S")
    except Exception:
        dt_start_str = (now_dt + timedelta(hours=2)).strftime("%Y%m%dT%H%M%S")
        dt_end_str = (now_dt + timedelta(hours=3, minutes=15)).strftime("%Y%m%dT%H%M%S")

    ics_content = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Daddy Barbershop//Calendar//RU\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:daddy-{int(now_dt.timestamp())}@daddybarbershop.md\r\n"
        f"DTSTAMP:{now_dt.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTSTART:{dt_start_str}\r\n"
        f"DTEND:{dt_end_str}\r\n"
        f"SUMMARY:Daddy Barbershop: {service} ({barber})\r\n"
        f"DESCRIPTION:Запись в Daddy Barbershop.\\nМастер: {barber}\\nУслуга: {service}\\nСумма: {price}\\nАдрес: ул. 25 Октября, 94\\nЖдём вас!\r\n"
        "LOCATION:Тирасполь, ул. 25 Октября, 94\r\n"
        "STATUS:CONFIRMED\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT120M\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Напоминание: через 2 часа визит в Daddy Barbershop\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    return web.Response(
        body=ics_content.encode("utf-8"),
        content_type="text/calendar",
        charset="utf-8",
        headers={
            "Content-Disposition": f'inline; filename="daddy_visit_{date_str}.ics"',
            "Cache-Control": "no-cache"
        }
    )


# ==============================================================================
# 9. ЗАПУСК ПРИЛОЖЕНИЯ
# ==============================================================================
async def main():
    init_db()

    app = web.Application()
    app.router.add_get("/webapp", serve_index)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.router.add_static("/static/", path=static_dir, name="static")

    # API
    app.router.add_get("/api/slots", api_get_busy_slots)
    app.router.add_get("/api/user-profile", api_get_user_profile)
    app.router.add_get("/api/my-bookings", api_get_my_bookings)
    app.router.add_post("/api/cancel-booking", api_cancel_booking)
    app.router.add_get("/api/portfolio", api_get_portfolio)
    app.router.add_get("/api/reviews", api_get_reviews)
    app.router.add_post("/api/reviews", api_post_review)
    app.router.add_get("/api/calendar.ics", api_serve_calendar_ics)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"[*] Веб-сервер запущен на порту {port}")

    # Запуск фонового планировщика
    asyncio.create_task(background_scheduler())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
