import asyncio
import html
import logging
import os
import re
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ==================== КОНФИГУРАЦИЯ ====================
TOKEN = os.getenv("BOT_TOKEN", "8833391875:AAE7qPO7vg8wtywiXImiIlghGrDMxV1wSaE")

# Список ID администраторов (можно перечислить через запятую в env или прямо здесь)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "8239312897,2028135610").split(",") if x.strip()]

SUPPORT_USERNAME = "FreelancerFinance"

CHANNEL_USERNAME = "@Finance_by_Freelance_channel"
CHANNEL_URL = "https://t.me/Finance_by_Freelance_channel"

MIN_WITHDRAW = 10.0  # минимальная сумма вывода
REFERRAL_BONUS = 15.0  # бонус рефереру за активного реферала

DB_NAME = "bot_database.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
router = Router()


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        language TEXT DEFAULT 'ru',
        balance REAL DEFAULT 0.0,
        total_withdrawn REAL DEFAULT 0.0,
        referrer_id INTEGER,
        referral_count INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    )
    """)

    # Инициализируем администраторов по умолчанию, если таблица пуста
    cursor.execute("SELECT COUNT(*) FROM admins")
    if cursor.fetchone()[0] == 0:
        for a_id in ADMIN_IDS:
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (a_id,))

    migrations = [
        ("username", "ALTER TABLE users ADD COLUMN username TEXT"),
        ("language", "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'ru'"),
        ("balance", "ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0.0"),
        ("total_withdrawn", "ALTER TABLE users ADD COLUMN total_withdrawn REAL DEFAULT 0.0"),
        ("referrer_id", "ALTER TABLE users ADD COLUMN referrer_id INTEGER"),
        ("referral_count", "ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0"),
    ]
    for _, ddl in migrations:
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER,
        title TEXT,
        limit_users INTEGER,
        time_limit INTEGER,
        reward REAL DEFAULT 0.0,
        link TEXT,
        instruction_text TEXT,
        instruction_photo TEXT,
        FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE CASCADE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        task_id INTEGER,
        status TEXT,
        deadline INTEGER,
        link TEXT,
        screenshot TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
    )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tasks_status ON user_tasks(status, deadline)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tasks_user ON user_tasks(user_id, task_id)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        details TEXT,
        amount REAL DEFAULT 0.0,
        status TEXT DEFAULT 'pending'
    )
    """)

    conn.commit()
    conn.close()


def db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def is_admin(user_id: int) -> bool:
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


async def notify_all_admins(bot: Bot, text: str, reply_markup=None, photo=None):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins")
    admins = cursor.fetchall()
    conn.close()

    for (adm_id,) in admins:
        try:
            if photo:
                await bot.send_photo(chat_id=adm_id, photo=photo, caption=text, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=adm_id, text=text, reply_markup=reply_markup)
        except Exception:
            logging.exception("Не удалось отправить уведомление админу %s", adm_id)


def get_user_lang(user_id):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 'ru'


def set_user_lang(user_id, lang):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (user_id, language) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET language = ?",
        (user_id, lang, lang)
    )
    conn.commit()
    conn.close()


def get_user_balance(user_id):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT balance, total_withdrawn FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return (row[0], row[1]) if row else (0.0, 0.0)


def update_user_balance(user_id, amount):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
        (user_id, amount, amount)
    )
    conn.commit()
    conn.close()


def add_total_withdrawn(user_id, amount):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET total_withdrawn = total_withdrawn + ? WHERE user_id = ?",
        (amount, user_id)
    )
    conn.commit()
    conn.close()


def get_user_rating(user_id):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM user_tasks WHERE user_id = ? AND status = 'completed'", (user_id,))
    completed = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM user_tasks WHERE user_id = ? AND status = 'rejected'", (user_id,))
    rejected = cursor.fetchone()[0]
    conn.close()

    total_reviewed = completed + rejected
    if total_reviewed == 0:
        return 10.0, completed, rejected

    rating = (completed / total_reviewed) * 10.0
    return round(rating, 1), completed, rejected


def get_user_profile_info(user_id):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT username, referral_count FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, 0


# ==================== СЛОВАРЬ ЯЗЫКОВ ====================
LANGS = {
    "ru": {
        "welcome": "👋 Добро пожаловать в главного помощника!\n\n👇 Выберите нужный раздел в меню ниже:",
        "tasks": "📋 Задания",
        "balance": "💰 Баланс",
        "profile": "👤 Профиль",
        "contact": "💬 Поддержка",
        "admin": "⚙️ Админ-панель",
        "change_lang": "🌐 Сменить язык",
        "back": "◀️ Назад",
        "sub_required": "📢 Требуется подписка!\n\nДля использования бота необходимо подписаться на наш канал.\n\n👇 Пожалуйста, подпишитесь, а затем нажмите кнопку проверки ниже.",
        "sub_btn": "📢 Подписаться на канал",
        "check_sub_btn": "✅ Проверить подписку",
        "not_subscribed": "❌ Вы еще не подписались на канал!",
        "no_cats": "📂 Пока нет доступных категорий.",
        "select_cat": "📂 Выберите категорию заданий:",
        "no_tasks_in_cat": "📭 В этой категории пока нет заданий.",
        "task_desc": "📌 Задание №{t_id}\n\n📝 Описание: {title}\n🎁 Награда: {reward} руб.\n⏱ Время на выполнение: {t_limit} мин.",
        "accept": "✅ Принять",
        "reject": "❌ Отказаться",
        "task_accepted": "🚀 Вы приняли задание №{task_id}!\n⏳ У вас есть **{time_limit} минут**.\n\n📥 **Шаг 1 из 2:** Отправьте ссылку на выполненное задание:",
        "step2": "📸 Шаг 2 из 2: Отправьте скриншот отчета:",
        "report_sent": "🎉 Отчет успешно отправлен на модерацию администратору! Ожидайте проверки.",
        "limit_reached": "⚠️ К сожалению, лимит мест на это задание исчерпан.",
        "task_not_found": "⚠️ Задание не найдено или больше недоступно.",
        "already_accepted": "⚠️ Вы уже выполняете это задание. Дождитесь проверки или истечения времени.",
        "already_completed": "⚠️ Вы уже выполнили это задание раньше!",
        "invalid_link": "❌ Это не похоже на ссылку. Отправьте ссылку, начинающуюся с `http://` или `https://`",
        "send_photo_please": "📸 Пожалуйста, отправьте скриншот именно **фотографией** (не файлом):",
        "balance_text": "💰 Ваш кошелек:\n\n💳 Текущий баланс: {balance} руб.\n💸 Всего выведено: {total_withdrawn} руб.",
        "profile_text": "👤 Ваш профиль:\n\n🏷 Пользователь: \n {username} \n\n👥 Приглашено рефералов: {ref_count}\n🔗 Ваша реферальная ссылка:\n {ref_link} \n\n⭐ Рейтинг: {rating} / 10",
        "withdraw": "💸 Вывести средства",
        "withdraw_info": "💳 Вывод средств\n\n💡 Минимальная сумма для вывода: 10 рублей.\n\n✍️ Введите ваши реквизиты (ФИО, банк, номер телефона/карты):",
        "min_withdraw_error": "⚠️ Недостаточно средств для вывода.**\nМинимальная сумма: 10 руб.",
        "withdraw_sent": "📤 Заявка на вывод успешно отправлена администратору! Ожидайте выплаты.",
        "contact_admin": "💬 Связаться с администратором: https://t.me/{support}",
        "referral_bonus_msg": "🎉 Реферальный бонус!\n\nПо вашей реферальной ссылке зарегистрировался новый пользователь и подписался на канал.\n💰 Вам начислено 15 рублей!",
        "admin_panel": "⚙️ Панель администратора:",
        "adm_add_cat": "➕ Создать категорию",
        "adm_list_cats": "🗑 Список категорий (Удаление)",
        "adm_add_task": "➕ Создать задание",
        "adm_list_tasks": "🗑 Список всех заданий (Удаление)",
        "adm_withdrawals": "💸 Запросы на вывод",
        "input_cat_name": "✍️ Введите название новой категории:",
        "cat_created": "✅ Категория '{cat_name}' успешно создана!",
        "cat_exists": "⚠️ Категория с таким названием уже существует.",
        "no_cats_admin": "⚠️ Сначала создайте хотя бы одну категорию!",
        "select_cat_task": "📂 Выберите категорию для задания:",
        "input_task_title": "✍️ Введите описание/текст задания:",
        "input_task_link": "🔗 Отправьте ссылку на задание:",
        "input_task_instruction": "📝 Отправьте инструкцию текстом **ИЛИ** прикрепите фото с инструкцией:",
        "input_task_limit": "👥 Сколько людей могут выполнить это задание? (укажите число):",
        "input_task_time": "⏱ Укажите время на выполнение в минутах:",
        "input_task_reward": "💰 Укажите выплату за задание в рублях (например, `15` или `10.5`):",
        "task_created": "🎉 Задание успешно создано!",
        "task_list_empty": "📭 Список заданий пуст.",
        "task_deleted": "🗑 Задание удалено!",
        "approved": "✅ Ваше задание №{task_id} подтверждено!\n💰 На ваш баланс зачислено {reward} руб.",
        "rejected_msg": "❌ Ваше выполнение задания №{task_id} было отклонено администратором.",
        "expired_msg": "⏳ Время на выполнение задания №{task_id} истекло. Задание автоматически отменено.",
        "select_lang": "🌐 Выберите язык / Choose language / Виберіть мову / Тілді таңдаңыз:",
        "already_processed": "⚠️ Этот отчет уже обработан.",
        "internal_error": "⚠️ Произошла внутренняя ошибка. Попробуйте позже или напишите администратору."
    },
    "en": {
        "welcome": "👋 Welcome!\n\n👇 Please select a section from the menu below:",
        "tasks": "📋 Tasks",
        "balance": "💰 Balance",
        "profile": "👤 Profile",
        "contact": "💬 Support",
        "admin": "⚙️ Admin Panel",
        "change_lang": "🌐 Change Language",
        "back": "◀️ Back",
        "sub_required": "📢 **Subscription Required!**\n\nYou must subscribe to our channel to use the bot.\n\n👇 Please subscribe and then click the check button.",
        "sub_btn": "📢 Subscribe to Channel",
        "check_sub_btn": "✅ Check Subscription",
        "not_subscribed": "❌ You are not subscribed to the channel yet!",
        "no_cats": "📂 No categories available yet.",
        "select_cat": "📂 **Select a task category:**",
        "no_tasks_in_cat": "📭 There are no tasks in this category yet.",
        "task_desc": "📌 **Task #{t_id}**\n\n📝 **Description:** {title}\n🎁 **Reward:** {reward} RUB\n⏱ **Time limit:** {t_limit} minutes",
        "accept": "✅ Accept",
        "reject": "❌ Decline",
        "task_accepted": "🚀 **You accepted task #{task_id}!**\n⏳ You have **{time_limit} minutes**.\n\n📥 **Step 1 of 2:** Send the link to the completed task:",
        "step2": "📸 **Step 2 of 2:** Send the screenshot:",
        "report_sent": "🎉 **Report successfully sent to the administrator!**",
        "limit_reached": "⚠️ Sorry, the participant limit has been reached.",
        "task_not_found": "⚠️ Task not found.",
        "already_accepted": "⚠️ You are already performing this task. Wait for review or expiry.",
        "already_completed": "⚠️ You have already completed this task!",
        "invalid_link": "❌ That doesn't look like a link. Send a link starting with `http://` or `https://`",
        "send_photo_please": "📸 Please send the screenshot as a **photo** (not a file):",
        "balance_text": "💰 **Your Balance:**\n\n💳 Current Balance: **{balance} RUB**\n💸 Total withdrawn: **{total_withdrawn} RUB**",
        "profile_text": "👤 **Your Profile:**\n\n🏷 Username: `{username}`\n👥 Referrals: **{ref_count}**\n🔗 Ref link:\n`{ref_link}`\n\n📊 **Tasks Stats:**\n✅ Completed: {completed}\n❌ Rejected: {rejected}\n⭐ Rating: {rating} / 10",
        "withdraw": "💸 Withdraw funds",
        "withdraw_info": "💳 **Withdrawal**\n\n💡 Minimum withdrawal amount: **10 RUB**.\n\n✍️ Enter your details (Full name, bank, phone/card number):",
        "min_withdraw_error": "⚠️ **Insufficient funds.** Minimum is **10 RUB**.",
        "withdraw_sent": "📤 **Withdrawal request sent to administrator!**",
        "contact_admin": "💬 Contact administrator: https://t.me/{support}",
        "referral_bonus_msg": "🎉 **Referral Bonus!**\n\nA new user registered via your referral link and subscribed to the channel.\n💰 You have been credited **15 RUB**!",
        "admin_panel": "⚙️ **Admin Panel:**",
        "adm_add_cat": "➕ Create Category",
        "adm_list_cats": "🗑 Categories List (Delete)",
        "adm_add_task": "➕ Create Task",
        "adm_list_tasks": "🗑 Task List (Delete)",
        "adm_withdrawals": "💸 Withdrawal Requests",
        "input_cat_name": "✍️ Enter category name:",
        "cat_created": "✅ Category **'{cat_name}'** created!",
        "cat_exists": "⚠️ Category exists.",
        "no_cats_admin": "⚠️ Create a category first!",
        "select_cat_task": "📂 Select category:",
        "input_task_title": "✍️ Enter task description:",
        "input_task_link": "🔗 Send task link:",
        "input_task_instruction": "📝 Send text instruction OR photo:",
        "input_task_limit": "👥 Limit of users:",
        "input_task_time": "⏱ Time limit in minutes:",
        "input_task_reward": "💰 Reward in rubles:",
        "task_created": "🎉 **Task created!**",
        "task_list_empty": "📭 Empty list.",
        "task_deleted": "🗑 Task deleted!",
        "approved": "✅ **Task #{task_id} approved!**\n💰 Added **{reward} RUB** to balance.",
        "rejected_msg": "❌ **Task #{task_id} rejected.**",
        "expired_msg": "⏳ **Task #{task_id} expired.**",
        "select_lang": "🌐 Choose language:",
        "already_processed": "⚠️ This report has already been processed.",
        "internal_error": "⚠️ Internal error. Try again later or contact the administrator."
    },
    "uk": {
        "welcome": "👋 **Вітаємо!**\n\n👇 Виберіть потрібний розділ у меню нижче:",
        "tasks": "📋 Завдання",
        "balance": "💰 Баланс",
        "profile": "👤 Профіль",
        "contact": "💬 Підтримка",
        "admin": "⚙️ Адмін-панель",
        "change_lang": "🌐 Змінити мову",
        "back": "◀️ Назад",
        "sub_required": "📢 **Потрібна підписка!**\n\nЩоб користуватися ботом, підпишіться на наш канал.\n\n👇 Підпишіться, а потім натисніть кнопку перевірки нижче.",
        "sub_btn": "📢 Підписатися на канал",
        "check_sub_btn": "✅ Перевірити підписку",
        "not_subscribed": "❌ Ви ще не підписалися на канал!",
        "no_cats": "📂 Поки немає доступних категорій.",
        "select_cat": "📂 **Виберіть категорію завдань:**",
        "no_tasks_in_cat": "📭 У цій категорії поки немає завдань.",
        "task_desc": "📌 **Завдання №{t_id}**\n\n📝 **Опис:** {title}\n🎁 **Нагорода:** {reward} руб.\n⏱ **Час:** {t_limit} хв.",
        "accept": "✅ Прийняти",
        "reject": "❌ Відмовитися",
        "task_accepted": "🚀 **Ви прийняли завдання №{task_id}!**\n⏳ У вас є **{time_limit} хв**.\n\n📥 **Крок 1:** Надішліть посилання:",
        "step2": "📸 **Крок 2:** Надішліть скріншот:",
        "report_sent": "🎉 **Звіт надіслано на модерацію!**",
        "limit_reached": "⚠️ Ліміт вичерпано.",
        "task_not_found": "⚠️ Завдання не знайдено.",
        "already_accepted": "⚠️ Ви вже виконуєте це завдання.",
        "already_completed": "⚠️ Ви вже виконали це завдання раніше!",
        "invalid_link": "❌ Це не схоже на посилання. Надішліть посилання, що починається з `http://` або `https://`",
        "send_photo_please": "📸 Надішліть скріншот саме **фотографією** (не файлом):",
        "balance_text": "💰 **Ваш баланс:**\n\n💳 Поточний баланс: **{balance} руб.**\n💸 Виведено: **{total_withdrawn} руб.**",
        "profile_text": "👤 **Ваш профіль:**\n\n🏷 Юзернейм: `{username}`\n👥 Рефералів: **{ref_count}**\n🔗 Реф. посилання:\n`{ref_link}`\n\n📊 **Статистика:**\n✅ Виконано: {completed}\n❌ Відхилено: {rejected}\n⭐ Рейтинг: {rating} / 10",
        "withdraw": "💸 Вивести кошти",
        "withdraw_info": "💳 **Виведення коштів**\n\n💡 Мін. сума виведення: **10 рублів**.\n\n✍️ Введіть ваші реквізити (ПІБ, банк, телефон):",
        "min_withdraw_error": "⚠️ **Недостатньо коштів.** Мінімум: **10 руб.**",
        "withdraw_sent": "📤 **Заявку надіслано адміністратору!**",
        "contact_admin": "💬 Зв'язатися з адміністратором: https://t.me/{support}",
        "referral_bonus_msg": "🎉 **Реферальний бонус!**\n\nЗа вашим реферальним посиланням зареєструвався новий користувач.\n💰 Вам нараховано **15 рублів**!",
        "admin_panel": "⚙️ **Адмін-панель:**",
        "adm_add_cat": "➕ Створити категорію",
        "adm_list_cats": "🗑 Список категорій",
        "adm_add_task": "➕ Створити завдання",
        "adm_list_tasks": "🗑 Список завдань",
        "adm_withdrawals": "💸 Виплати",
        "input_cat_name": "✍️ Введіть назву категорії:",
        "cat_created": "✅ Категорію створено!",
        "cat_exists": "⚠️ Вже існує.",
        "no_cats_admin": "⚠️ Створіть категорію!",
        "select_cat_task": "📂 Виберіть категорію:",
        "input_task_title": "✍️ Опис завдання:",
        "input_task_link": "🔗 Посилання:",
        "input_task_instruction": "📝 Інструкція (текст або фото):",
        "input_task_limit": "👥 Ліміт людей:",
        "input_task_time": "⏱ Час (хв):",
        "input_task_reward": "💰 Виплата (руб):",
        "task_created": "🎉 **Створено!**",
        "task_list_empty": "📭 Порожньо.",
        "task_deleted": "🗑 Видалено!",
        "approved": "✅ **Підтверджено!** +**{reward} руб.**",
        "rejected_msg": "❌ Відхилено.",
        "expired_msg": "⏳ Час вийшов.",
        "select_lang": "🌐 Виберіть мову:",
        "already_processed": "⚠️ Цей звіт вже оброблено.",
        "internal_error": "⚠️ Внутрішня помилка. Спробуйте пізніше."
    },
    "kk": {
        "welcome": "👋 **Қош келдіңіз!**\n\n👇 Төмендегі мәзірден қажетті бөлімді таңдаңыз:",
        "tasks": "📋 Тапсырмалар",
        "balance": "💰 Баланс",
        "profile": "👤 Профиль",
        "contact": "💬 Қолдау",
        "admin": "⚙️ Әкімші панелі",
        "change_lang": "🌐 Тілді өзгерту",
        "back": "◀️ Артқа",
        "sub_required": "📢 **Арнаға жазылу қажет!**\n\nБотты қолдану үшін арнамызға тіркеліңіз.\n\n👇 Жазылып, содан кейін тексеру түймесін басыңыз.",
        "sub_btn": "📢 Арнаға жазылу",
        "check_sub_btn": "✅ Жазылуды тексеру",
        "not_subscribed": "❌ Сіз әлі арнаға жазылмадыңыз!",
        "no_cats": "📂 Санаттар жоқ.",
        "select_cat": "📂 **Санатты таңдаңыз:**",
        "no_tasks_in_cat": "📭 Тапсырмалар жоқ.",
        "task_desc": "📌 **Тапсырма №{t_id}**\n\n📝 **Сипаттама:** {title}\n🎁 **Сыйлық:** {reward} руб.\n⏱ **Уақыт:** {t_limit} мин.",
        "accept": "✅ Қабылдау",
        "reject": "❌ Бас тарту",
        "task_accepted": "🚀 **Тапсырма қабылданды!**\n\n📥 **1-қадам:** Сілтеме жіберіңіз:",
        "step2": "📸 **2-қадам:** Скриншот жіберіңіз:",
        "report_sent": "🎉 **Есеп сәтті жіберілді!**",
        "limit_reached": "⚠️ Лимит бітті.",
        "task_not_found": "⚠️ Табылмады.",
        "already_accepted": "⚠️ Сіз бұл тапсырманы орындап жатырсыз.",
        "already_completed": "⚠️ Сіз бұл тапсырманы бұрын орындап қойғансыз!",
        "invalid_link": "❌ Бұл сілтемеге ұқсамайды. `http://` немесе `https://` басталатын сілтеме жіберіңіз",
        "send_photo_please": "📸 Скриншотты міндетті түрде **фото ретінде** жіберіңіз (файл емес):",
        "balance_text": "💰 **Сіздің балансыңыз:**\n\n💳 Ағымдағы баланс: **{balance} руб.**\n💸 Шығарылды: **{total_withdrawn} руб.**",
        "profile_text": "👤 **Сіздің профиліңіз:**\n\n🏷 Юзернейм: `{username}`\n👥 Рефералдар: **{ref_count}**\n🔗 Реф. сілтеме:\n`{ref_link}`\n\n📊 **Статистика:**\n✅ Орындалды: {completed}\n❌ Қабылданбады: {rejected}\n⭐ Рейтинг: {rating} / 10",
        "withdraw": "💸 Ақша шығару",
        "withdraw_info": "💳 **Ақша шығару**\n\n💡 Минималды сома: **10 рубль**.\n\n✍️ Деректеріңізді жазыңыз (Аты-жөні, банк, телефон):",
        "min_withdraw_error": "⚠️ **Баланс жеткіліксіз.** Минималды: **10 руб.**",
        "withdraw_sent": "📤 **Өтінім әкімшіге жіберілді!**",
        "contact_admin": "💬 Әкімшімен байланысу: https://t.me/{support}",
        "referral_bonus_msg": "🎉 **Рефералдық бонус!**\n\nСіздің сілтемеңіз бойынша жаңа қолданушы тіркелді.\n💰 Сізге **15 рубль** есептелді!",
        "admin_panel": "⚙️ **Әкімші панелі:**",
        "adm_add_cat": "➕ Санат құру",
        "adm_list_cats": "🗑 Санаттар тізімі",
        "adm_add_task": "➕ Тапсырма құру",
        "adm_list_tasks": "🗑 Тізім",
        "adm_withdrawals": "💸 Шығару сұраулары",
        "input_cat_name": "✍️ Атауын енгізіңіз:",
        "cat_created": "✅ Құрылды!",
        "cat_exists": "⚠️ Бар.",
        "no_cats_admin": "⚠️ Санат құрыңыз!",
        "select_cat_task": "📂 Санатты таңдаңыз:",
        "input_task_title": "✍️ Сипаттама:",
        "input_task_link": "🔗 Сілтеме:",
        "input_task_instruction": "📝 Нұсқаулық (мәтін/фото):",
        "input_task_limit": "👥 Адам саны:",
        "input_task_time": "⏱ Уақыт (мин):",
        "input_task_reward": "💰 Сыйақы (руб):",
        "task_created": "🎉 **Құрылды!**",
        "task_list_empty": "📭 Бос.",
        "task_deleted": "🗑 Жойылды!",
        "approved": "✅ **Расталды!** +**{reward} руб.**",
        "rejected_msg": "❌ Қабылданбады.",
        "expired_msg": "⏳ Уақыт бітті.",
        "select_lang": "🌐 Тілді таңдаңыз:",
        "already_processed": "⚠️ Бұл есеп әлдеқашан өңделген.",
        "internal_error": "⚠️ Ішкі қате. Кейінірек көріңіз."
    }
}

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE_RE = re.compile(r"`([^`]+)`")


def _md_to_html(text: str) -> str:
    text = _BOLD_RE.sub(r"**\1**", text)
    text = _CODE_RE.sub(r"`\1`", text)
    return text


def t(user_id, key, **kwargs):
    lang = get_user_lang(user_id)
    text = LANGS.get(lang, LANGS["ru"]).get(key, LANGS["ru"].get(key, key))
    return _md_to_html(text.format(**kwargs))


def esc(value):
    return html.escape(str(value)) if value is not None else ""


async def check_user_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        if member.status in ("creator", "administrator", "member"):
            return True
        if member.status == "restricted" and getattr(member, "is_member", False):
            return True
        return False
    except Exception as e:
        logging.warning("get_chat_member не сработал для %s: %s", user_id, e)
        return False


# ==================== СОСТОЯНИЯ FSM ====================
class AdminTaskState(StatesGroup):
    waiting_for_title = State()
    waiting_for_link = State()
    waiting_for_instruction = State()
    waiting_for_limit = State()
    waiting_for_time = State()
    waiting_for_reward = State()


class AdminCategoryState(StatesGroup):
    waiting_for_name = State()


class UserTaskState(StatesGroup):
    waiting_for_link = State()
    waiting_for_screenshot = State()


class UserWithdrawState(StatesGroup):
    waiting_for_details = State()


class AdminWithdrawScreenshotState(StatesGroup):
    waiting_for_screenshot = State()


# ==================== КЛАВИАТУРЫ ====================
def get_main_menu_keyboard(user_id):
    keyboard = [
        [KeyboardButton(text=t(user_id, "tasks")), KeyboardButton(text=t(user_id, "balance"))],
        [KeyboardButton(text=t(user_id, "profile")), KeyboardButton(text=t(user_id, "change_lang"))],
        [KeyboardButton(text=t(user_id, "contact"))]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton(text=t(user_id, "admin"))])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_sub_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "sub_btn"), url=CHANNEL_URL)],
        [InlineKeyboardButton(text=t(user_id, "check_sub_btn"), callback_data="check_subscription")]
    ])


# ==================== ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ====================
@router.error()
async def global_error_handler(event: ErrorEvent):
    logging.exception("Ошибка при обработке апдейта: %s", event.exception)
    update = event.update
    try:
        if update.message and update.message.from_user:
            await update.message.answer(t(update.message.from_user.id, "internal_error"))
        elif update.callback_query and update.callback_query.from_user:
            await update.callback_query.answer("⚠️ Ошибка обработки. Попробуйте позже.", show_alert=True)
    except Exception:
        pass
    return True


# ==================== ОБРАБОТЧИКИ КОМАНД ====================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, command: CommandObject, bot: Bot):
    await state.clear()
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else f"id{user_id}"

    args = command.args
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if not row:
        ref_id = None
        if args and args.isdigit():
            potential_ref = int(args)
            if potential_ref != user_id:
                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (potential_ref,))
                if cursor.fetchone():
                    ref_id = potential_ref

        cursor.execute(
            "INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
            (user_id, username, ref_id)
        )
        conn.commit()
    else:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()
    conn.close()

    is_subscribed = await check_user_subscription(bot, user_id)
    if not is_subscribed:
        await message.answer(t(user_id, "sub_required"), reply_markup=get_sub_keyboard(user_id))
        return

    await message.answer(t(user_id, "welcome"), reply_markup=get_main_menu_keyboard(user_id))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(t(message.from_user.id, "welcome"), reply_markup=get_main_menu_keyboard(message.from_user.id))


@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = callback.from_user.id
    is_subscribed = await check_user_subscription(bot, user_id)

    if not is_subscribed:
        await callback.answer(t(user_id, "not_subscribed"), show_alert=True)
        return

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    referrer_id = row[0] if row else None

    if referrer_id:
        cursor.execute("UPDATE users SET referrer_id = NULL WHERE user_id = ?", (user_id,))
        cursor.execute(
            "UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?",
            (REFERRAL_BONUS, referrer_id)
        )
        conn.commit()
        try:
            await bot.send_message(referrer_id, t(referrer_id, "referral_bonus_msg"))
        except Exception:
            pass
    conn.close()

    await callback.answer("OK")
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(t(user_id, "welcome"), reply_markup=get_main_menu_keyboard(user_id))


# ==================== FSM: СОЗДАНИЕ КАТЕГОРИИ ====================
@router.message(AdminCategoryState.waiting_for_name)
async def adm_save_cat(message: Message, state: FSMContext):
    user_id = message.from_user.id
    cat_name = (message.text or "").strip()

    if not cat_name:
        await message.answer("⚠️ Название категории не может быть пустым. Введите текст:")
        return

    conn = db_connect()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
        conn.commit()
        await message.answer(t(user_id, "cat_created", cat_name=esc(cat_name)),
                             reply_markup=get_main_menu_keyboard(user_id))
    except sqlite3.IntegrityError:
        await message.answer(t(user_id, "cat_exists"), reply_markup=get_main_menu_keyboard(user_id))
    except Exception as e:
        logging.exception("Ошибка создания категории")
        await message.answer(f"⚠️ Произошла ошибка при создании категории: {e}")
    finally:
        conn.close()
        await state.clear()


# ==================== FSM: СОЗДАНИЕ ЗАДАНИЯ ====================
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import logging


# Убедитесь, что у вас подключены функции: is_admin(user_id), db_connect(), t(user_id, key), get_main_menu_keyboard(user_id)
# и класс состояний AdminTaskState со всеми необходимыми шагами.

@router.message(AdminTaskState.waiting_for_title)
async def adm_task_title(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    text = message.text.strip() if message.text else ""
    if not text:
        await message.answer("⚠️ Название задания не может быть пустым. Введите название:")
        return

    await state.update_data(title=text)
    await state.set_state(AdminTaskState.waiting_for_link)
    await message.answer(t(user_id, "input_task_link"))


@router.message(AdminTaskState.waiting_for_link)
async def adm_task_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    text = message.text.strip() if message.text else ""
    if not text:
        await message.answer("⚠️ Ссылка не может быть пустой. Введите ссылку:")
        return

    await state.update_data(link=text)
    await state.set_state(AdminTaskState.waiting_for_instruction)
    await message.answer(t(user_id, "input_task_instruction"))


@router.message(AdminTaskState.waiting_for_instruction)
async def adm_task_instruction(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    if message.photo:
        instruction_photo = message.photo[-1].file_id
        instruction_text = message.caption if message.caption else ""
    elif message.text:
        instruction_text = message.text.strip()
        instruction_photo = None
    else:
        await message.answer("⚠️ Пожалуйста, отправьте текстовую инструкцию или фотографию с описанием:")
        return

    await state.update_data(
        instruction_text=instruction_text,
        instruction_photo=instruction_photo
    )
    await state.set_state(AdminTaskState.waiting_for_limit)

    try:
        msg_text = t(user_id, "input_task_limit")
    except Exception:
        msg_text = "Введите лимит пользователей для этого задания (целое число):"
    await message.answer(msg_text)


@router.message(AdminTaskState.waiting_for_limit)
async def adm_task_limit(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    text = message.text.strip() if message.text else ""
    if not text.isdigit():
        await message.answer("⚠️ Пожалуйста, введите целое число (например, 100):")
        return

    limit_users = int(text)
    if limit_users <= 0:
        await message.answer("⚠️ Лимит пользователей должен быть больше 0:")
        return

    await state.update_data(limit_users=limit_users)
    await state.set_state(AdminTaskState.waiting_for_time)

    try:
        msg_text = t(user_id, "input_task_time")
    except Exception:
        msg_text = "Введите время на выполнение задания в минутах (целое число):"
    await message.answer(msg_text)


@router.message(AdminTaskState.waiting_for_time)
async def adm_task_time(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    text = message.text.strip() if message.text else ""
    if not text.isdigit():
        await message.answer("⚠️ Пожалуйста, введите целое число минут (например, 30):")
        return

    time_limit = int(text)
    if time_limit <= 0:
        await message.answer("⚠️ Время выполнения должно быть больше 0 минут:")
        return

    await state.update_data(time_limit=time_limit)
    await state.set_state(AdminTaskState.waiting_for_reward)

    try:
        msg_text = t(user_id, "input_task_reward")
    except Exception:
        msg_text = "Введите награду за выполнение задания (число, например 15 или 10.5):"
    await message.answer(msg_text)


@router.message(AdminTaskState.waiting_for_reward)
async def adm_task_reward(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    try:
        reward_text = message.text.strip().replace(",", ".") if message.text else ""
        reward = round(float(reward_text), 2)
        if reward < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("⚠️ Пожалуйста, введите корректное число для награды (например, 15 или 10.5):")
        return

    data = await state.get_data()

    # Проверка, чтобы все ключи на месте
    required_keys = ["category_id", "title", "link", "limit_users", "time_limit"]
    missing_keys = [k for k in required_keys if k not in data]

    if missing_keys:
        await message.answer(
            f"⚠️ Ошибка: утеряны данные сессии ({', '.join(missing_keys)}). Пожалуйста, начните создание задания заново из меню.")
        await state.clear()
        return

    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO tasks 
               (category_id, title, limit_users, time_limit, reward, link, instruction_text, instruction_photo) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("category_id"),
                data.get("title"),
                data.get("limit_users"),
                data.get("time_limit"),
                reward,
                data.get("link"),
                data.get("instruction_text"),
                data.get("instruction_photo")
            )
        )
        conn.commit()
    except Exception as e:
        logging.exception("Критическая ошибка при записи задания в БД: %s", e)
        await message.answer(f"⚠️ Внутренняя ошибка базы данных: {e}")
        return
    finally:
        if 'conn' in locals():
            conn.close()

    await state.clear()

    try:
        success_msg = t(user_id, "task_created")
    except Exception:
        success_msg = "✅ Задание успешно создано!"

    await message.answer(success_msg, reply_markup=get_main_menu_keyboard(user_id))
# ==================== FSM: ВЫПОЛНЕНИЕ ЗАДАНИЯ ====================
@router.message(UserTaskState.waiting_for_link)
async def process_task_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    link = (message.text or "").strip()
    if not ("http://" in link or "https://" in link):
        await message.answer(t(user_id, "invalid_link"))
        return
    await state.update_data(link=link)
    await state.set_state(UserTaskState.waiting_for_screenshot)
    await message.answer(t(user_id, "step2"))


@router.message(UserTaskState.waiting_for_screenshot, F.photo)
async def process_task_screenshot(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    photo_file_id = message.photo[-1].file_id
    data = await state.get_data()
    user_task_id = data.get("user_task_id")
    task_id = data.get("task_id")
    link = data.get("link")

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM user_tasks WHERE id = ?", (user_task_id,))
    row = cursor.fetchone()
    if not row or row[0] != "active":
        conn.close()
        await state.clear()
        await message.answer(t(user_id, "already_processed"), reply_markup=get_main_menu_keyboard(user_id))
        return

    cursor.execute("UPDATE user_tasks SET link = ?, screenshot = ? WHERE id = ?", (link, photo_file_id, user_task_id))
    conn.commit()
    cursor.execute("SELECT username, user_id FROM user_tasks WHERE id = ?", (user_task_id,))
    row = cursor.fetchone()
    conn.close()

    await state.clear()
    await message.answer(t(user_id, "report_sent"), reply_markup=get_main_menu_keyboard(user_id))

    if not row:
        return
    username, u_id = row
    rating, completed, rejected = get_user_rating(u_id)

    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_approve_{user_task_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject_{user_task_id}")
        ]
    ])

    await notify_all_admins(
        bot=bot,
        text=f"📋 **Новый отчет по заданию!**\n\n"
             f"📌 Номер задания: #{task_id}\n"
             f"👤 Пользователь: {esc(username)} (ID: `{u_id}`)\n"
             f"⭐ Рейтинг: {rating} / 10 (✅ Выполнено: {completed} / ❌ Отклонено: {rejected})\n"
             f"🔗 Ссылка: {esc(link)}",
        reply_markup=admin_keyboard,
        photo=photo_file_id
    )


@router.message(UserTaskState.waiting_for_screenshot)
async def process_task_screenshot_wrong(message: Message, state: FSMContext):
    await message.answer(t(message.from_user.id, "send_photo_please"))


# ==================== FSM: ВЫВОД СРЕДСТВ ====================
@router.message(UserWithdrawState.waiting_for_details)
async def process_withdraw_details(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    details = (message.text or "").strip()
    if not details:
        await message.answer("⚠️ Реквизиты не могут быть пустыми. Введите текстом:")
        return

    username = f"@{message.from_user.username}" if message.from_user.username else f"id{user_id}"
    balance, _ = get_user_balance(user_id)

    if balance < MIN_WITHDRAW:
        await message.answer(t(user_id, "min_withdraw_error"), reply_markup=get_main_menu_keyboard(user_id))
        await state.clear()
        return

    withdraw_amount = round(balance, 2)
    update_user_balance(user_id, -withdraw_amount)

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO withdrawals (user_id, username, details, amount, status) VALUES (?, ?, ?, ?, 'pending')",
        (user_id, username, f"Реквизиты: {details}", withdraw_amount)
    )
    wd_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer(t(user_id, "withdraw_sent"), reply_markup=get_main_menu_keyboard(user_id))

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Выплачено (скрин)", callback_data=f"adm_wd_pay_{wd_id}_{user_id}")],
        [InlineKeyboardButton(text="❌ Отклонить",
                              callback_data=f"adm_wd_reject_{wd_id}_{user_id}_{withdraw_amount:.2f}")]
    ])

    await notify_all_admins(
        bot=bot,
        text=f"💸 **Новый запрос на вывод средств!**\n\n"
             f"👤 Пользователь: {esc(username)} (ID: `{user_id}`)\n"
             f"💰 Сумма: **{withdraw_amount} руб.**\n"
             f"📋 {esc(details)}",
        reply_markup=admin_kb
    )


@router.message(AdminWithdrawScreenshotState.waiting_for_screenshot, F.photo)
async def adm_wd_pay_send(message: Message, state: FSMContext, bot: Bot):
    photo_file_id = message.photo[-1].file_id
    data = await state.get_data()
    wd_id = data.get("wd_id")
    user_id = data.get("user_id")

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT status, amount FROM withdrawals WHERE id = ?", (wd_id,))
    wd_row = cursor.fetchone()

    if not wd_row or wd_row[0] != "pending":
        conn.close()
        await state.clear()
        await message.answer("⚠️ Этот запрос уже обработан.")
        return

    amount = wd_row[1]
    cursor.execute("UPDATE withdrawals SET status = 'completed' WHERE id = ?", (wd_id,))
    conn.commit()
    conn.close()

    add_total_withdrawn(user_id, amount)
    await state.clear()
    await message.answer("✅ Скриншот успешно отправлен пользователю, выплата отмечена как завершенная.")

    try:
        await bot.send_photo(
            chat_id=user_id,
            photo=photo_file_id,
            caption="🎉 **Ваш запрос на вывод средств успешно выполнен!** Администратор прикрепил подтверждение перевода:"
        )
    except Exception:
        logging.exception("Не удалось отправить скриншот выплаты пользователю")


@router.message(AdminWithdrawScreenshotState.waiting_for_screenshot)
async def adm_wd_pay_wrong(message: Message, state: FSMContext):
    await message.answer("⚠️ Отправьте скриншот именно **фотографией** (не файлом):")


# ==================== МОДЕРАЦИЯ ЗАДАНИЙ ====================
@router.callback_query(F.data.startswith("adm_approve_"))
async def adm_approve(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора.", show_alert=True)
        return

    user_task_id = int(callback.data.split("_")[2])
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT status, user_id, task_id FROM user_tasks WHERE id = ?", (user_task_id,))
    row = cursor.fetchone()
    if not row or row[0] != "active":
        conn.close()
        await callback.answer("⚠️ Этот отчет уже обработан.", show_alert=True)
        return

    _, u_id, task_id = row
    cursor.execute("UPDATE user_tasks SET status = 'completed' WHERE id = ? AND status = 'active'", (user_task_id,))
    cursor.execute("SELECT reward FROM tasks WHERE id = ?", (task_id,))
    task_rew = cursor.fetchone()
    reward = task_rew[0] if task_rew else 0.0
    conn.commit()
    conn.close()

    update_user_balance(u_id, reward)

    try:
        await bot.send_message(u_id, t(u_id, "approved", task_id=task_id, reward=reward))
    except Exception:
        pass

    await _mark_callback_message(callback, f"✅ ПОДТВЕРЖДЕНО (@{callback.from_user.username or callback.from_user.id})")
    await callback.answer("Задание подтверждено, награда зачислена.", show_alert=True)


@router.callback_query(F.data.startswith("adm_reject_"))
async def adm_reject(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора.", show_alert=True)
        return

    user_task_id = int(callback.data.split("_")[2])
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT status, user_id, task_id FROM user_tasks WHERE id = ?", (user_task_id,))
    row = cursor.fetchone()
    if not row or row[0] != "active":
        conn.close()
        await callback.answer("⚠️ Этот отчет уже обработан.", show_alert=True)
        return

    _, u_id, task_id = row
    cursor.execute("UPDATE user_tasks SET status = 'rejected' WHERE id = ? AND status = 'active'", (user_task_id,))
    conn.commit()
    conn.close()

    try:
        await bot.send_message(u_id, t(u_id, "rejected_msg", task_id=task_id))
    except Exception:
        pass

    await _mark_callback_message(callback, f"❌ ОТКЛОНЕНО (@{callback.from_user.username or callback.from_user.id})")
    await callback.answer("Задание отклонено.", show_alert=True)


async def _mark_callback_message(callback: CallbackQuery, status_text: str):
    try:
        if callback.message.caption:
            await callback.message.edit_caption(caption=callback.message.caption + f"\n\n[СТАТУС: {status_text}]")
        else:
            await callback.message.edit_text(callback.message.text + f"\n\n[СТАТУС: {status_text}]")
    except Exception:
        pass


# ==================== ОБЩИЙ ОБРАБОТЧИК ТЕКСТОВЫХ КНОПОК ====================
@router.message(F.text)
async def handle_reply_buttons(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    text = message.text

    if not await check_user_subscription(bot, user_id):
        await message.answer(t(user_id, "sub_required"), reply_markup=get_sub_keyboard(user_id))
        return

    if any(text == LANGS[lang]["tasks"] for lang in LANGS):
        await state.clear()
        await show_tasks_categories(message, user_id)
        return

    if any(text == LANGS[lang]["balance"] for lang in LANGS):
        await state.clear()
        await show_balance(message, user_id)
        return

    if any(text == LANGS[lang]["profile"] for lang in LANGS):
        await state.clear()
        await show_profile(message, user_id, bot)
        return

    if any(text == LANGS[lang]["change_lang"] for lang in LANGS):
        await state.clear()
        await change_language_handler(message, user_id)
        return

    if any(text == LANGS[lang]["contact"] for lang in LANGS):
        await message.answer(t(user_id, "contact_admin", support=SUPPORT_USERNAME),
                             reply_markup=get_main_menu_keyboard(user_id))
        return

    if is_admin(user_id) and any(text == LANGS[lang]["admin"] for lang in LANGS):
        await state.clear()
        await show_admin_panel(message, user_id)
        return


async def change_language_handler(message: Message, user_id: int):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="set_lang_uk")],
        [InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data="set_lang_kk")]
    ])
    await message.answer(t(user_id, "select_lang"), reply_markup=keyboard)


@router.callback_query(F.data.startswith("set_lang_"))
async def set_language_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    lang = callback.data.split("_")[2]
    if lang not in LANGS:
        await callback.answer("Unknown language", show_alert=True)
        return
    set_user_lang(user_id, lang)

    await callback.answer("OK")
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(t(user_id, "welcome"), reply_markup=get_main_menu_keyboard(user_id))


async def show_balance(message: Message, user_id: int):
    balance, total_withdrawn = get_user_balance(user_id)
    balance = round(balance, 2)
    total_withdrawn = round(total_withdrawn, 2)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "withdraw"), callback_data="user_withdraw_start")]
    ])
    await message.answer(t(user_id, "balance_text", balance=balance, total_withdrawn=total_withdrawn),
                         reply_markup=keyboard)


async def show_profile(message: Message, user_id: int, bot: Bot):
    rating, completed, rejected = get_user_rating(user_id)
    username, ref_count = get_user_profile_info(user_id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    display_username = username if username else f"id{user_id}"

    await message.answer(t(
        user_id, "profile_text",
        username=esc(display_username),
        ref_count=ref_count,
        ref_link=ref_link,
        completed=completed,
        rejected=rejected,
        rating=rating
    ))


@router.callback_query(F.data == "user_withdraw_start")
async def user_withdraw_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    if not await check_user_subscription(bot, user_id):
        await callback.answer("❌", show_alert=True)
        return

    balance, _ = get_user_balance(user_id)
    if balance < MIN_WITHDRAW:
        await callback.answer(t(user_id, "min_withdraw_error"), show_alert=True)
        return

    await state.clear()
    await callback.answer()
    await state.set_state(UserWithdrawState.waiting_for_details)
    await callback.message.answer(t(user_id, "withdraw_info"))


# ==================== ЗАДАНИЯ (ПОЛЬЗОВАТЕЛЬ) ====================
async def show_tasks_categories(message: Message, user_id: int):
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM categories")
    categories = cursor.fetchall()
    conn.close()

    if not categories:
        await message.answer(t(user_id, "no_cats"))
        return

    keyboard = [[InlineKeyboardButton(text=f"📁 {cat_name}", callback_data=f"user_cat_{cat_id}")] for cat_id, cat_name in
                categories]
    await message.answer(t(user_id, "select_cat"), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("user_cat_"))
async def show_category_tasks(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    cat_id = int(callback.data.split("_")[2])

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, limit_users, time_limit, reward FROM tasks WHERE category_id = ?", (cat_id,))
    tasks = cursor.fetchall()

    if not tasks:
        conn.close()
        try:
            await callback.message.edit_text(t(user_id, "no_tasks_in_cat"))
        except Exception:
            await callback.message.answer(t(user_id, "no_tasks_in_cat"))
        return

    cursor.execute("SELECT task_id FROM user_tasks WHERE user_id = ? AND status IN ('active', 'completed')", (user_id,))
    excluded_task_ids = {row[0] for row in cursor.fetchall()}

    placeholders = ",".join("?" * len(tasks))
    cursor.execute(
        f"SELECT task_id, COUNT(*) FROM user_tasks WHERE status IN ('active', 'completed') AND task_id IN ({placeholders}) GROUP BY task_id",
        tuple(t[0] for t in tasks)
    )
    counts = dict(cursor.fetchall())
    conn.close()

    keyboard = []
    for t_id, title, limit, t_limit, reward in tasks:
        if t_id in excluded_task_ids:
            continue
        if counts.get(t_id, 0) < limit:
            short_title = title[:15] + "..." if len(title) > 15 else title
            keyboard.append([InlineKeyboardButton(text=f"📌 #{t_id}: {short_title} (+{reward} руб.)",
                                                  callback_data=f"view_task_{t_id}")])

    keyboard.append([InlineKeyboardButton(text=t(user_id, "back"), callback_data="user_tasks_menu")])

    if len(keyboard) <= 1:
        text_resp = t(user_id, "no_tasks_in_cat")
    else:
        text_resp = t(user_id, "select_cat")

    try:
        await callback.message.edit_text(text_resp, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except Exception:
        await callback.message.answer(text_resp, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data == "user_tasks_menu")
async def user_tasks_menu_callback(callback: CallbackQuery):
    await callback.answer()
    await show_tasks_categories(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("view_task_"))
async def view_single_task(callback: CallbackQuery):
    user_id = callback.from_user.id
    task_id = int(callback.data.split("_")[2])

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM user_tasks WHERE user_id = ? AND task_id = ? AND status IN ('active', 'completed')",
                   (user_id, task_id))
    if cursor.fetchone():
        conn.close()
        await callback.answer(t(user_id, "already_completed"), show_alert=True)
        return

    cursor.execute(
        "SELECT id, title, time_limit, reward, link, instruction_text, instruction_photo FROM tasks WHERE id = ?",
        (task_id,))
    task = cursor.fetchone()
    conn.close()

    if not task:
        await callback.answer(t(user_id, "task_not_found"), show_alert=True)
        return

    t_id, title, t_limit, reward, link, inst_text, inst_photo = task
    text = t(user_id, "task_desc", t_id=t_id, title=esc(title), reward=reward, t_limit=t_limit)
    if link:
        text += f"\n\n🔗 **Ссылка:** {esc(link)}"
    if inst_text:
        text += f"\n\n📝 **Инструкция:**\n{esc(inst_t
                                              )}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t(user_id, "accept"), callback_data=f"accept_task_{t_id}"),
            InlineKeyboardButton(text=t(user_id, "reject"), callback_data="user_tasks_menu")
        ]
    ])

    try:
        if inst_photo:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(photo=inst_photo, caption=text, reply_markup=keyboard)
        else:
            await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("accept_task_"))
async def accept_task(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    if not await check_user_subscription(bot, user_id):
        await callback.answer("❌", show_alert=True)
        return

    task_id = int(callback.data.split("_")[2])
    username = f"@{callback.from_user.username}" if callback.from_user.username else f"id{user_id}"

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, status FROM user_tasks WHERE user_id = ? AND task_id = ? AND status IN ('active', 'completed')",
        (user_id, task_id))
    existing_task = cursor.fetchone()
    if existing_task:
        conn.close()
        await callback.answer(t(user_id, "already_completed"), show_alert=True)
        return

    cursor.execute("SELECT time_limit, limit_users FROM tasks WHERE id = ?", (task_id,))
    task_info = cursor.fetchone()

    if not task_info:
        conn.close()
        await callback.answer(t(user_id, "task_not_found"), show_alert=True)
        return

    time_limit_min, limit_users = task_info

    cursor.execute("SELECT COUNT(*) FROM user_tasks WHERE task_id = ? AND status IN ('active', 'completed')",
                   (task_id,))
    active_count = cursor.fetchone()[0]

    if active_count >= limit_users:
        conn.close()
        await callback.answer(t(user_id, "limit_reached"), show_alert=True)
        return

    deadline = int(time.time()) + (time_limit_min * 60)
    cursor.execute(
        "INSERT INTO user_tasks (user_id, username, task_id, status, deadline) VALUES (?, ?, ?, 'active', ?)",
        (user_id, username, task_id, deadline)
    )
    user_task_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await state.update_data(user_task_id=user_task_id, task_id=task_id)
    await state.set_state(UserTaskState.waiting_for_link)

    await callback.answer("OK")
    await callback.message.answer(t(user_id, "task_accepted", task_id=task_id, time_limit=time_limit_min))


# ==================== АДМИН-ПАНЕЛЬ ====================
async def show_admin_panel(message: Message, user_id: int):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "adm_add_cat"), callback_data="adm_add_cat")],
        [InlineKeyboardButton(text=t(user_id, "adm_list_cats"), callback_data="adm_list_cats")],
        [InlineKeyboardButton(text=t(user_id, "adm_add_task"), callback_data="adm_add_task")],
        [InlineKeyboardButton(text=t(user_id, "adm_list_tasks"), callback_data="adm_list_tasks")],
        [InlineKeyboardButton(text=t(user_id, "adm_withdrawals"), callback_data="adm_list_withdrawals")]
    ])
    await message.answer(t(user_id, "admin_panel"), reply_markup=keyboard)


@router.callback_query(F.data == "adm_add_cat")
async def adm_add_cat_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.set_state(AdminCategoryState.waiting_for_name)
    await callback.message.answer(t(callback.from_user.id, "input_cat_name"))


@router.callback_query(F.data == "adm_list_cats")
async def adm_list_cats(callback: CallbackQuery, answered: bool = False):
    if not is_admin(callback.from_user.id):
        if not answered:
            await callback.answer("❌ Нет прав.", show_alert=True)
        return
    if not answered:
        await callback.answer()
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM categories")
    categories = cursor.fetchall()
    conn.close()

    if not categories:
        await callback.message.answer("📭 Список категорий пуст.")
        return

    keyboard = [[InlineKeyboardButton(text=f"📁 {name}", callback_data=f"adm_del_cat_{c_id}")] for c_id, name in
                categories]
    await callback.message.answer("📂 **Список категорий (нажмите для удаления со всеми заданиями):**",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("adm_del_cat_"))
async def adm_delete_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    cat_id = int(callback.data.split("_")[3])
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()

    await callback.answer("🗑 Категория и все привязанные задания удалены!", show_alert=True)
    await adm_list_cats(callback, answered=True)


@router.callback_query(F.data == "adm_add_task")
async def adm_add_task_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM categories")
    categories = cursor.fetchall()
    conn.close()

    if not categories:
        await callback.message.answer(t(user_id, "no_cats_admin"))
        return

    keyboard = [[InlineKeyboardButton(text=f"📁 {name}", callback_data=f"adm_task_cat_{c_id}")] for c_id, name in
                categories]
    await callback.message.answer(t(user_id, "select_cat_task"),
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("adm_task_cat_"))
async def adm_task_cat_chosen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    user_id = callback.from_user.id
    cat_id = int(callback.data.split("_")[3])
    await state.update_data(category_id=cat_id)
    await state.set_state(AdminTaskState.waiting_for_title)
    await callback.message.answer(t(user_id, "input_task_title"))


@router.callback_query(F.data == "adm_list_tasks")
async def adm_list_tasks(callback: CallbackQuery, answered: bool = False):
    if not is_admin(callback.from_user.id):
        if not answered:
            await callback.answer("❌ Нет прав.", show_alert=True)
        return
    if not answered:
        await callback.answer()
    user_id = callback.from_user.id
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title FROM tasks")
    tasks = cursor.fetchall()
    conn.close()

    if not tasks:
        await callback.message.answer(t(user_id, "task_list_empty"))
        return

    keyboard = [[InlineKeyboardButton(text=f"📌 #{t_id}: {title[:20]}...", callback_data=f"adm_del_task_{t_id}")]
                for t_id, title in tasks]
    await callback.message.answer("📋 **Список всех заданий (нажмите для удаления):**",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("adm_del_task_"))
async def adm_delete_task(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    task_id = int(callback.data.split("_")[3])
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    await callback.answer("🗑 Задание удалено!", show_alert=True)
    await adm_list_tasks(callback, answered=True)


# ==================== ВЫПЛАТЫ (АДМИН) ====================
@router.callback_query(F.data == "adm_list_withdrawals")
async def adm_list_withdrawals(callback: CallbackQuery, answered: bool = False):
    if not is_admin(callback.from_user.id):
        if not answered:
            await callback.answer("❌ Нет прав.", show_alert=True)
        return
    if not answered:
        await callback.answer()
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, amount FROM withdrawals WHERE status = 'pending'")
    withdrawals = cursor.fetchall()
    conn.close()

    if not withdrawals:
        await callback.message.answer("📭 Нет активных запросов на вывод.")
        return

    keyboard = [[InlineKeyboardButton(text=f"💳 Вывод #{wd_id} ({username}) — {amount} руб.",
                                      callback_data=f"adm_view_wd_{wd_id}")] for wd_id, username, amount in withdrawals]
    await callback.message.answer("💸 **Запросы на вывод средств:**",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("adm_view_wd_"))
async def adm_view_withdrawal(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    wd_id = int(callback.data.split("_")[3])
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, details, amount FROM withdrawals WHERE id = ?", (wd_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("⚠️ Запрос не найден.", show_alert=True)
        return

    await callback.answer()
    user_id, username, details, amount = row
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Выплачено (отправить скрин)", callback_data=f"adm_wd_pay_{wd_id}_{user_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_wd_reject_{wd_id}_{user_id}_{amount:.2f}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_list_withdrawals")]
    ])
    await callback.message.answer(
        f"💳 **Запрос на вывод #{wd_id}**\n\n"
        f"👤 Пользователь: {esc(username)} (ID: `{user_id}`)\n"
        f"💰 Сумма: **{amount} руб.**\n"
        f"📋 {esc(details)}",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("adm_wd_reject_"))
async def adm_wd_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    parts = callback.data.split("_")
    wd_id = int(parts[3])
    user_id = int(parts[4])
    amount = float(parts[5])

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM withdrawals WHERE id = ?", (wd_id,))
    row = cursor.fetchone()
    if not row or row[0] != "pending":
        conn.close()
        await callback.answer("⚠️ Этот запрос уже обработан.", show_alert=True)
        return

    cursor.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (wd_id,))
    conn.commit()
    conn.close()

    update_user_balance(user_id, amount)

    await callback.answer("✅ Запрос отклонен, средства возвращены пользователю.", show_alert=True)
    await adm_list_withdrawals(callback, answered=True)


@router.callback_query(F.data.startswith("adm_wd_pay_"))
async def adm_wd_pay_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 5:
        await callback.answer("⚠️ Ошибка данных.", show_alert=True)
        return
    wd_id = int(parts[3])
    user_id = int(parts[4])

    await state.clear()
    await state.update_data(wd_id=wd_id, user_id=user_id)
    await state.set_state(AdminWithdrawScreenshotState.waiting_for_screenshot)
    await callback.message.answer("📸 Отправьте скриншот выплаты (подтверждение перевода):")


# ==================== ФОНОВАЯ ПРОВЕРКА ИСТЕКШИХ ЗАДАНИЙ ====================
async def check_expired_tasks(bot: Bot):
    while True:
        await asyncio.sleep(30)
        current_time = int(time.time())

        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, task_id FROM user_tasks WHERE status = 'active' AND deadline < ?",
            (current_time,)
        )
        expired_tasks = cursor.fetchall()

        for ut_id, u_id, task_id in expired_tasks:
            cursor.execute("UPDATE user_tasks SET status = 'expired' WHERE id = ? AND status = 'active'", (ut_id,))
            try:
                await bot.send_message(u_id, t(u_id, "expired_msg", task_id=task_id))
            except Exception:
                pass

        conn.commit()
        conn.close()


# ==================== ЗАПУСК ====================
async def main():
    bot = Bot(token=TOKEN, default_parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    init_db()
    asyncio.create_task(check_expired_tasks(bot))

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())