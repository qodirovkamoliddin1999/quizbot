"""
QUIZ TEST BOT - Telegram Bot
To'liq ishlaydigan versiya
O'rnatish kerak kutubxonalar:
pip install aiogram pandas openpyxl
"""

import asyncio
import logging
from datetime import datetime
import sqlite3
from typing import Optional, Dict, List, Tuple
import re
import os
import tempfile

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    Document
)
import pandas as pd

# ================== KONFIGURATSIYA ==================
BOT_TOKEN = "8457392855:AAH2jg45eBoC9x4S669vm63oWTwa6ITrrBI"  # O'zgartiring!
ADMIN_IDS = [8166749577, 5589604734]  # Admin Telegram ID'larini kiriting (sonlar)
CHANNEL_USERNAME = "@KamoliddinQodirov"  # Kanal username

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================== MA'LUMOTLAR BAZASI ==================
DB_PATH = "quiz_bot.db"


def init_db():
    """Ma'lumotlar bazasini yaratish"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Adminlar
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        full_name TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # O'quvchilar
    c.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        full_name TEXT,
        group_name TEXT,
        phone TEXT,
        subscribed INTEGER DEFAULT 0,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Testlar
    c.execute('''CREATE TABLE IF NOT EXISTS tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        title TEXT,
        correct_keys TEXT,
        question_count INTEGER,
        created_by INTEGER,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Natijalar
    c.execute('''CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        test_id INTEGER,
        correct_count INTEGER,
        total_questions INTEGER,
        user_answers TEXT,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES students(id),
        FOREIGN KEY (test_id) REFERENCES tests(id)
    )''')

    # Sozlamalar (kanal username va h.k.)
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    # Majburiy obuna kanallari ro'yxati
    c.execute('''CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE
    )''')

    # Admin qo'shish
    for admin_id in ADMIN_IDS:
        c.execute('INSERT OR IGNORE INTO admins (tg_id, full_name) VALUES (?, ?)',
                  (admin_id, 'Admin'))

    conn.commit()
    conn.close()


def get_db():
    """Database connection olish"""
    return sqlite3.connect(DB_PATH)


def get_channel_username_from_db(default_value: str) -> str:
    """DB dan kanal username ni olish, yo'q bo'lsa defaultni qaytarish"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('SELECT value FROM settings WHERE key = ?', ('channel_username',))
        row = c.fetchone()
        if row and row[0]:
            return row[0]
        return default_value
    finally:
        conn.close()


def set_channel_username_in_db(value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  ('channel_username', value))
        conn.commit()
    finally:
        conn.close()


def list_required_channels() -> List[Tuple[int, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('SELECT id, username FROM required_channels ORDER BY id ASC')
        return c.fetchall()
    finally:
        conn.close()


def add_required_channel(username: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO required_channels(username) VALUES (?)', (username,))
        conn.commit()
    finally:
        conn.close()


def remove_required_channel(ch_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM required_channels WHERE id = ?', (ch_id,))
        conn.commit()
    finally:
        conn.close()


# ================== FSM STATES ==================
class StudentRegistration(StatesGroup):
    waiting_for_name = State()
    waiting_for_group = State()


class TestCreation(StatesGroup):
    waiting_for_title = State()
    waiting_for_code = State()
    waiting_for_keys = State()
    waiting_for_file = State()


class TestTaking(StatesGroup):
    waiting_for_code = State()
    waiting_for_answers = State()


class InteractiveTest(StatesGroup):
    choosing_test = State()
    in_progress = State()


class AdminSettings(StatesGroup):
    waiting_for_channel = State()
    waiting_for_channel_add = State()


# ================== YORDAMCHI FUNKSIYALAR ==================
async def check_subscription(bot: Bot, user_id: int) -> bool:
    """Foydalanuvchi kanal(lar)ga a'zo ekanligini tekshirish"""
    channels = list_required_channels()
    if not channels:
        # backward compatibility: single setting
        try:
            channel = get_channel_username_from_db(CHANNEL_USERNAME)
            if not channel:
                return True
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            return member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            logger.error(f"Kanal tekshirishda xato: {e}")
            return False
    for _, ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            logger.error(f"Kanal tekshirishda xato ({ch}): {e}")
            return False
    return True


def is_admin(user_id: int) -> bool:
    """Foydalanuvchi admin ekanligini tekshirish"""
    return user_id in ADMIN_IDS


def parse_keys(keys_text: str) -> Dict[int, str]:
    """Kalitlarni parse qilish: '1-A 2-B 3-C' -> {1: 'A', 2: 'B', 3: 'C'}"""
    keys_dict: Dict[int, str] = {}
    pattern = r'(\d+)[\.\-\s]*([A-Da-d])'
    matches = re.findall(pattern, keys_text)
    for num, answer in matches:
        keys_dict[int(num)] = answer.upper()
    return keys_dict


def check_answers(user_answers: str, correct_keys: str) -> Dict:
    """Javoblarni tekshirish"""
    user_dict = parse_keys(user_answers)
    correct_dict = parse_keys(correct_keys)
    results = []
    correct_count = 0

    for num in sorted(correct_dict.keys()):
        user_ans = user_dict.get(num, '')
        is_correct = user_ans == correct_dict[num]
        if is_correct:
            correct_count += 1

        results.append({
            'question': num,
            'user_answer': user_ans,
            'correct_answer': correct_dict[num],
            'is_correct': is_correct
        })

    return {
        'correct_count': correct_count,
        'total': len(correct_dict),
        'details': results
    }


def format_result_message(check_result: Dict) -> str:
    """Natijani chiroyli formatda ko'rsatish"""
    details = check_result['details']
    correct = check_result['correct_count']
    total = check_result['total']
    result_text = f"📊 <b>Test natijalari:</b>\n\n"
    result_text += f"✅ To'g'ri javoblar: <b>{correct}</b> ta\n"
    result_text += f"❌ Noto'g'ri javoblar: <b>{total - correct}</b> ta\n"
    result_text += f"📈 Natija: <b>{correct}/{total}</b>\n\n"
    result_text += f"<b>Batafsil:</b>\n"

    line_count = 0
    for item in details:
        emoji = "✅" if item['is_correct'] else "❌"
        result_text += f"{item['question']}-{emoji} "
        line_count += 1
        if line_count % 10 == 0:
            result_text += "\n"

    return result_text


def build_question_keyboard(q_num: int, total: int, selected: Optional[str], answered_count: int) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    opts = ['A', 'B', 'C', 'D']
    row: List[InlineKeyboardButton] = []
    for o in opts:
        label = f"{o}"
        if selected == o:
            label = f"[{o}]"
        row.append(InlineKeyboardButton(text=label, callback_data=f"choose:{q_num}:{o}"))
    buttons.append(row)
    nav_row: List[InlineKeyboardButton] = []
    nav_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data="nav:prev"))
    nav_row.append(InlineKeyboardButton(text=f"{q_num}/{total}", callback_data="noop"))
    nav_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data="nav:next"))
    buttons.append(nav_row)
    # finish row
    can_finish = answered_count == total and total > 0
    finish_text = "✅ Yakunlash" if can_finish else "⏳ Yakunlash (to'liq emas)"
    finish_cb = "finish" if can_finish else "noop"
    buttons.append([InlineKeyboardButton(text=finish_text, callback_data=finish_cb)])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ================== KLAVIATURALAR ==================
def main_menu_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    """Asosiy menyu klaviaturasi"""
    keyboard: List[List[InlineKeyboardButton]] = []
    if is_admin_user:
        keyboard.extend([
            [InlineKeyboardButton(text="➕ Yangi test yaratish", callback_data="admin_create_test")],
            [InlineKeyboardButton(text="📁 Test faylini yuklash (Excel)", callback_data="admin_upload_test_file")],
            [InlineKeyboardButton(text="📊 Statistika", callback_data="admin_statistics")],
            [InlineKeyboardButton(text="ℹ️ Qo'llanma (test joylash)", callback_data="admin_help")],
            [InlineKeyboardButton(text="🔗 Majburiy obuna kanallari", callback_data="admin_channels")],
            [InlineKeyboardButton(text="🗂 Testlarni boshqarish", callback_data="admin_manage_tests")],
        ])

    keyboard.extend([
        [InlineKeyboardButton(text="📝 Test ishlash", callback_data="student_take_test")],
        [InlineKeyboardButton(text="📖 Mening natijalarim", callback_data="my_results")],
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def subscription_keyboard() -> InlineKeyboardMarkup:
    """Obuna tugmasi"""
    rows: List[List[InlineKeyboardButton]] = []
    channels = list_required_channels()
    if not channels:
        channel = get_channel_username_from_db(CHANNEL_USERNAME)
        if channel:
            rows.append([InlineKeyboardButton(text="📢 Kanalga a'zo bo'lish", url=f"https://t.me/{channel.lstrip('@')}")])
    else:
        for _, ch in channels:
            rows.append([InlineKeyboardButton(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Bekor qilish tugmasi"""
    keyboard = [[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ================== ROUTER ==================
router = Router()


# ================== /START KOMANDASI ==================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Start komandasi"""
    user_id = message.from_user.id
    db = get_db()
    c = db.cursor()
    # Admin tekshirish
    if is_admin(user_id):
        await message.answer(
            f"👋 Assalomu alaykum, Admin!\n\n"
            f"Siz admin sifatida botdan foydalanishingiz mumkin.",
            reply_markup=main_menu_keyboard(is_admin_user=True)
        )
        db.close()
        return

    # O'quvchi ro'yxatdan o'tganligini tekshirish
    c.execute('SELECT * FROM students WHERE tg_id = ?', (user_id,))
    student = c.fetchone()

    if student:
        await message.answer(
            f"👋 Xush kelibsiz, {student[2]}!\n\n"
            f"Botdan foydalanish uchun menyuni tanlang:",
            reply_markup=main_menu_keyboard()
        )
    else:
        await message.answer(
            f"👋 Assalomu alaykum!\n\n"
            f"Bot orqali test ishlash uchun ro'yxatdan o'tishingiz kerak.\n\n"
            f"Iltimos, ism va familiyangizni kiriting:"
        )
        await message.answer("Masalan: <b>Abdullayev Sardor</b>", parse_mode="HTML")
        await state.set_state(StudentRegistration.waiting_for_name)

    db.close()


# ================== RO'YXATDAN O'TISH ==================
@router.message(StudentRegistration.waiting_for_name)
async def register_student_name(message: Message, state: FSMContext):
    """O'quvchini ro'yxatdan o'tkazish - ism olish"""
    user_id = message.from_user.id
    db = get_db()
    c = db.cursor()
    # Allaqachon ro'yxatdan o'tgan bo'lsa
    c.execute('SELECT * FROM students WHERE tg_id = ?', (user_id,))
    if c.fetchone():
        db.close()
        await state.clear()
        await message.answer("Siz allaqachon ro'yxatdan o'tgansiz.", reply_markup=main_menu_keyboard())
        return

    full_name = message.text.strip()

    if len(full_name) < 3:
        await message.answer("❌ Ism juda qisqa. Iltimos, to'liq ism familiyangizni kiriting:")
        db.close()
        return

    # Ism saqlash
    # Endi darhol bazaga yozamiz (guruhsiz)
    try:
        c.execute(
            'INSERT INTO students (tg_id, full_name, group_name) VALUES (?, ?, ?)',
            (user_id, full_name, None)
        )
        db.commit()

        await state.clear()
        await message.answer(
            f"✅ Ro'yxatdan o'tdingiz!\n\n"
            f"👤 Ism: <b>{full_name}</b>\n"
            f"Endi botdan foydalanishingiz mumkin!",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Ro'yxatdan o'tishda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")
    finally:
        db.close()


# ================== INTERAKTIV TEST TANLASH ==================
@router.callback_query(F.data.startswith("select_test:"))
async def select_test(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        # allow all students; only check subscription was done before
        pass
    try:
        test_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Xato test tanlandi.", show_alert=True)
        return

    db = get_db()
    c = db.cursor()
    # get test
    c.execute('SELECT id, code, title, correct_keys, question_count FROM tests WHERE id = ? AND is_active = 1', (test_id,))
    test = c.fetchone()
    if not test:
        db.close()
        await callback.message.edit_text("❌ Test topilmadi.", reply_markup=main_menu_keyboard())
        await callback.answer()


def build_channels_management_kb() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    channels = list_required_channels()
    if not channels:
        rows.append([InlineKeyboardButton(text="— Hozircha kanal yo'q —", callback_data="noop")])
    else:
        for ch_id, ch in channels:
            rows.append([
                InlineKeyboardButton(text=f"{ch}", callback_data="noop"),
                InlineKeyboardButton(text="❌ Olib tashlash", callback_data=f"rm_ch:{ch_id}")
            ])
    rows.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="add_channel")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ================== ADMIN: MAJBURIY KANALLAR ==================
@router.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    kb = build_channels_management_kb()
    await callback.message.edit_text("🔗 Majburiy obuna kanallari", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "add_channel")
async def admin_add_channel_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await callback.message.edit_text(
        "Yangi kanal username yuboring (masalan: @my_channel yoki https://t.me/my_channel)",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(AdminSettings.waiting_for_channel_add)
    await callback.answer()


@router.message(AdminSettings.waiting_for_channel_add)
async def admin_add_channel_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!", reply_markup=main_menu_keyboard())
        await state.clear()
        return
    text = message.text.strip()
    username = text
    if username.startswith('https://t.me/') or username.startswith('http://t.me/'):
        username = '@' + username.split('/t.me/')[-1].split('/')[-1]
    if not username.startswith('@'):
        username = '@' + username.lstrip('@')
    if len(username) < 2 or ' ' in username:
        await message.answer("❌ Noto'g'ri username. Masalan: @my_channel")
        return
    add_required_channel(username)
    await state.clear()
    kb = build_channels_management_kb()
    await message.answer("✅ Kanal qo'shildi", reply_markup=kb)


@router.callback_query(F.data.startswith("rm_ch:"))
async def admin_remove_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    try:
        ch_id = int(callback.data.split(":", 1)[1])
        remove_required_channel(ch_id)
    except Exception:
        pass
    kb = build_channels_management_kb()
    await callback.message.edit_text("🔗 Majburiy obuna kanallari", reply_markup=kb)
    await callback.answer("Olib tashlandi")


def build_tests_management_kb() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    db = get_db()
    c = db.cursor()
    c.execute('SELECT id, title, code, is_active FROM tests ORDER BY created_at DESC LIMIT 50')
    tests = c.fetchall()
    db.close()
    if not tests:
        rows.append([InlineKeyboardButton(text="— Testlar yo'q —", callback_data="noop")])
    else:
        for t_id, title, code, is_active in tests:
            status = "✅" if is_active else "⛔"
            rows.append([
                InlineKeyboardButton(text=f"{status} {title} ({code})", callback_data="noop")
            ])
            rows.append([
                InlineKeyboardButton(text=("Faol qilish" if not is_active else "Faol emas qilish"), callback_data=f"toggle_test:{t_id}"),
                InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del_test:{t_id}")
            ])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ================== ADMIN: TESTLARNI BOSHQARISH ==================
@router.callback_query(F.data == "admin_manage_tests")
async def admin_manage_tests(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    kb = build_tests_management_kb()
    await callback.message.edit_text("🗂 Testlarni boshqarish", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_test:"))
async def admin_toggle_test(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    try:
        t_id = int(callback.data.split(":", 1)[1])
        db = get_db()
        c = db.cursor()
        c.execute('SELECT is_active FROM tests WHERE id = ?', (t_id,))
        row = c.fetchone()
        if row is not None:
            new_val = 0 if row[0] else 1
            c.execute('UPDATE tests SET is_active = ? WHERE id = ?', (new_val, t_id))
            db.commit()
        db.close()
    except Exception:
        pass
    kb = build_tests_management_kb()
    await callback.message.edit_text("🗂 Testlarni boshqarish", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("del_test:"))
async def admin_delete_test(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    try:
        t_id = int(callback.data.split(":", 1)[1])
        db = get_db()
        c = db.cursor()
        # delete results first due to FK
        c.execute('DELETE FROM results WHERE test_id = ?', (t_id,))
        c.execute('DELETE FROM tests WHERE id = ?', (t_id,))
        db.commit()
        db.close()
    except Exception:
        pass
    kb = build_tests_management_kb()
    await callback.message.edit_text("🗂 Testlarni boshqarish", reply_markup=kb)
    await callback.answer("O'chirildi")

# ================== ADMIN: KANALNI SOZLASH ==================
@router.callback_query(F.data == "admin_set_channel")
async def admin_set_channel_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    current = get_channel_username_from_db(CHANNEL_USERNAME)
    await callback.message.edit_text(
        "🔗 Majburiy obuna kanali\n\n"
        f"Hozirgi: <code>{current}</code>\n\n"
        "Yangi kanal username-ni yuboring (masalan: <code>@my_channel</code> yoki https://t.me/my_channel).",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(AdminSettings.waiting_for_channel)
    await callback.answer()


@router.message(AdminSettings.waiting_for_channel)
async def admin_set_channel_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!", reply_markup=main_menu_keyboard())
        await state.clear()
        return
    text = message.text.strip()
    # Normalize to @username
    username = text
    if username.startswith('https://t.me/') or username.startswith('http://t.me/'):
        username = '@' + username.split('/t.me/')[-1].split('/')[-1]
    if not username.startswith('@'):
        username = '@' + username.lstrip('@')
    # Basic validation
    if len(username) < 2 or ' ' in username:
        await message.answer("❌ Noto'g'ri username. Masalan: @my_channel")
        return
    try:
        set_channel_username_in_db(username)
        await message.answer(
            f"✅ Kanal yangilandi: <code>{username}</code>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(is_admin_user=True)
        )
    except Exception as e:
        logger.error(f"Kanalni saqlashda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.")
    finally:
        await state.clear()
        return

    # ensure student exists and not already submitted
    c.execute('SELECT id FROM students WHERE tg_id = ?', (callback.from_user.id,))
    student = c.fetchone()
    if not student:
        db.close()
        await callback.message.edit_text("❌ Iltimos, avval /start orqali ro'yxatdan o'ting.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    student_id = student[0]
    c.execute('SELECT 1 FROM results WHERE user_id = ? AND test_id = ?', (student_id, test_id))
    if c.fetchone():
        db.close()
        await callback.message.edit_text("⚠️ Siz bu testni allaqachon topshirgansiz!", reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    test_id, code, title, correct_keys, question_count = test
    db.close()

    await state.update_data(test_id=test_id, code=code, title=title, correct_keys=correct_keys,
                            question_count=question_count, current_q=1, answers={})
    kb = build_question_keyboard(1, question_count, None, 0)
    await callback.message.edit_text(
        f"📝 <b>{title}</b>\nSavol 1 / {question_count}\nVariantni tanlang:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(InteractiveTest.in_progress)
    await callback.answer()


# ================== INTERAKTIV TEST BOSHQARUVLARI ==================
@router.callback_query(StateFilter(InteractiveTest.in_progress), F.data.startswith("choose:"))
async def choose_answer(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    q_num = int(parts[1])
    opt = parts[2]
    data = await state.get_data()
    question_count = data.get('question_count', 0)
    current_q = data.get('current_q', 1)
    answers: Dict[str, str] = data.get('answers', {})
    answers[str(q_num)] = opt
    await state.update_data(answers=answers)
    answered_count = len(answers)
    # keep current index as-is
    kb = build_question_keyboard(current_q, question_count, answers.get(str(current_q)), answered_count)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Tanlandi")


@router.callback_query(StateFilter(InteractiveTest.in_progress), F.data == "nav:prev")
async def nav_prev(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_q = max(1, int(data.get('current_q', 1)) - 1)
    question_count = data.get('question_count', 0)
    answers: Dict[str, str] = data.get('answers', {})
    await state.update_data(current_q=current_q)
    kb = build_question_keyboard(current_q, question_count, answers.get(str(current_q)), len(answers))
    await callback.message.edit_text(
        f"📝 <b>{data.get('title')}</b>\nSavol {current_q} / {question_count}\nVariantni tanlang:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(StateFilter(InteractiveTest.in_progress), F.data == "nav:next")
async def nav_next(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    question_count = data.get('question_count', 0)
    current_q = min(question_count, int(data.get('current_q', 1)) + 1)
    answers: Dict[str, str] = data.get('answers', {})
    await state.update_data(current_q=current_q)
    kb = build_question_keyboard(current_q, question_count, answers.get(str(current_q)), len(answers))
    await callback.message.edit_text(
        f"📝 <b>{data.get('title')}</b>\nSavol {current_q} / {question_count}\nVariantni tanlang:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(StateFilter(InteractiveTest.in_progress), F.data == "finish")
async def finish_test(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    question_count = data.get('question_count', 0)
    answers: Dict[str, str] = data.get('answers', {})
    if len(answers) != question_count:
        await callback.answer("Barcha savollarga javob bering!", show_alert=True)
        return
    # compose user_answers string
    parts = []
    for i in range(1, question_count + 1):
        parts.append(f"{i}-{answers.get(str(i))}")
    user_answers = " ".join(parts)
    correct_keys = data.get('correct_keys', '')
    check_result = check_answers(user_answers, correct_keys)

    # save result
    db = get_db()
    c = db.cursor()
    c.execute('SELECT id FROM students WHERE tg_id = ?', (callback.from_user.id,))
    row = c.fetchone()
    if not row:
        db.close()
        await callback.message.edit_text("❌ Ro'yxatdan o'tish kerak.")
        await state.clear()
        await callback.answer()
        return
    student_id = row[0]
    c.execute('INSERT INTO results (user_id, test_id, correct_count, total_questions, user_answers) VALUES (?, ?, ?, ?, ?)',
              (student_id, data.get('test_id'), check_result['correct_count'], check_result['total'], user_answers))
    db.commit()
    db.close()

    result_msg = format_result_message(check_result)
    await callback.message.edit_text(result_msg, parse_mode="HTML", reply_markup=main_menu_keyboard())
    await state.clear()
    await callback.answer()


@router.callback_query(StateFilter(InteractiveTest.in_progress), F.data == "noop")
async def noop_cb(callback: CallbackQuery):
    await callback.answer()


@router.message(StudentRegistration.waiting_for_group)
async def register_student_group(message: Message, state: FSMContext):
    """O'quvchini ro'yxatdan o'tkazish - guruh olish"""
    group_name = message.text.strip()
    if group_name.lower() in ['yo\'q', 'yoq', 'kerak emas', '-']:
        group_name = None

    # Ma'lumotlarni olish
    data = await state.get_data()
    full_name = data.get('full_name')

    # Bazaga saqlash
    user_id = message.from_user.id
    db = get_db()
    c = db.cursor()

    try:
        c.execute(
            'INSERT INTO students (tg_id, full_name, group_name) VALUES (?, ?, ?)',
            (user_id, full_name, group_name)
        )
        db.commit()

        await message.answer(
            f"✅ Ro'yxatdan o'tdingiz!\n\n"
            f"👤 Ism: <b>{full_name}</b>\n"
            f"👥 Guruh: <b>{group_name or 'Korsatilmagan'}</b>\n\n"
            f"Endi botdan foydalanishingiz mumkin!",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )

    except Exception as e:
        logger.error(f"Ro'yxatdan o'tishda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")

    finally:
        db.close()
        await state.clear()


# ================== KANAL OBUNA TEKSHIRISH ==================
@router.callback_query(F.data == "check_subscription")
async def check_sub_callback(callback: CallbackQuery):
    """Obunani tekshirish"""
    user_id = callback.from_user.id
    is_subscribed = await check_subscription(callback.bot, user_id)

    if is_subscribed:
        # Bazaga belgilash
        db = get_db()
        c = db.cursor()
        c.execute('UPDATE students SET subscribed = 1 WHERE tg_id = ?', (user_id,))
        db.commit()
        db.close()

        await callback.message.edit_text(
            "✅ Ajoyib! Siz kanalga a'zo bo'lgansiz.\n\n"
            "Endi test ishlashingiz mumkin!",
            reply_markup=main_menu_keyboard()
        )
    else:
        channel = get_channel_username_from_db(CHANNEL_USERNAME)
        await callback.answer(
            "❌ Siz hali kanalga a'zo emassiz!\n"
            f"Iltimos, {channel} kanaliga a'zo bo'ling.",
            show_alert=True
        )


# ================== ADMIN: TEST YARATISH ==================
@router.callback_query(F.data == "admin_create_test")
async def admin_create_test_start(callback: CallbackQuery, state: FSMContext):
    """Test yaratishni boshlash"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await callback.message.edit_text(
        "📝 Yangi test yaratish\n\n"
        "Test nomini kiriting:\n"
        "Masalan: <b>Ona tili 9-sinf</b>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(TestCreation.waiting_for_title)
    await callback.answer()


# ================== ADMIN QO'LLANMA ==================
@router.callback_query(F.data == "admin_help")
async def admin_help(callback: CallbackQuery):
    """Admin uchun test joylash bo'yicha qisqa qo'llanma"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    text = (
        "ℹ️ <b>Qo'llanma: testni qanday joylash</b>\n\n"
        "1) <b>Yangi test yaratish</b> tugmasini bosing.\n"
        "2) <b>Test nomi</b> va <b>test kodi</b> ni kiriting.\n"
        "3) Keyin ikkita usuldan birini tanlang:\n"
        "   • <b>Excel faylni yuklash</b>: .xlsx/.xls fayl yuboring.\n"
        "     - Bitta katakda: 1-A 2-B 3-C ...\n"
        "     - Yoki ustunlar: number | correct (yoki answer)\n"
        "   • <b>Kalit matn sifatida kiritish</b>: \n"
        "     - Format: <code>1-A 2-B 3-C 4-D ...</code>\n\n"
        "4) Saqlangandan so'ng test <b>faol</b> bo'ladi va o'quvchilar\n"
        "   <b>Test ishlash</b> orqali test kodini kiritib ishlashadi.\n\n"
        "💡 Eslatma: test kodini noyob qiling (masalan: OT9-2025)."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard(is_admin_user=True))
    await callback.answer()


@router.message(TestCreation.waiting_for_title)
async def admin_test_title(message: Message, state: FSMContext):
    """Test nomini qabul qilish"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    title = message.text.strip()
    if len(title) < 3:
        await message.answer("❌ Test nomi juda qisqa. Qaytadan kiriting:")
        return

    await state.update_data(title=title)

    await message.answer(
        f"✅ Test nomi: <b>{title}</b>\n\n"
        f"Endi test kodini kiriting:\n"
        f"Masalan: <b>OT9-2025</b>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(TestCreation.waiting_for_code)


@router.message(TestCreation.waiting_for_code)
async def admin_test_code(message: Message, state: FSMContext):
    """Test kodini qabul qilish"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    code = message.text.strip().upper()
    if len(code) < 3:
        await message.answer("❌ Test kodi juda qisqa. Qaytadan kiriting:")
        return

    # Kod mavjudligini tekshirish
    db = get_db()
    c = db.cursor()
    c.execute('SELECT * FROM tests WHERE code = ?', (code,))

    if c.fetchone():
        await message.answer("❌ Bu kod allaqachon ishlatilgan. Boshqa kod kiriting:")
        db.close()
        return

    db.close()

    await state.update_data(code=code)

    # So'rov: fayl yuklash yoki kalit matn orqali
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Excel faylni yuklash", callback_data="admin_upload_file_now")],
        [InlineKeyboardButton(text="✍️ Kalit matn sifatida kiritish", callback_data="admin_enter_keys_now")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]
    ])

    await message.answer(
        f"✅ Test kodi: <b>{code}</b>\n\n"
        f"Endi javoblar kalitini kiriting yoki Excel fayl yuklang.\n\n"
        f"Matn formati: <code>1-A 2-B 3-C 4-D 5-A...</code>",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(F.data == "admin_upload_file_now")
async def admin_choose_file(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await callback.message.edit_text(
        "📁 Iltimos, Excel (.xlsx) faylini yuboring.\n\n"
        "Fayl formatiga mos bo'lishi kerak - yoki bitta hujayrada '1-A 2-B ...' qator, yoki ikkita ustun: 'number' va 'correct' (yoki 'answer').",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(TestCreation.waiting_for_file)
    await callback.answer()


@router.message(TestCreation.waiting_for_file, F.document)
async def admin_receive_file(message: Message, state: FSMContext):
    """Admin yuborgan Excel faylini qabul qilish va parse qilish"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    doc: Document = message.document
    if not doc.file_name.lower().endswith(('.xlsx', '.xls')):
        await message.answer("❌ Iltimos, .xlsx yoki .xls fayl yuboring.")
        return

    # Faylni vaqtinchalik yuklab olish
    tmp_dir = tempfile.gettempdir()
    local_path = os.path.join(tmp_dir, f"test_upload_{message.message_id}_{doc.file_name}")
    await message.bot.download(doc, destination=local_path)

    # Pandas bilan o'qish va parse qilish
    try:
        df = pd.read_excel(local_path, engine='openpyxl')
        # Har xil formatlarni support qilishga harakat qilamiz
        keys_text = ""

        # 1) Agar dfda 'correct' yoki 'answer' ustuni bo'lsa
        if any(col.lower() in ['correct', 'answer', 'javob'] for col in df.columns):
            # topish
            col_candidates = [col for col in df.columns if col.lower() in ['correct', 'answer', 'javob']]
            col = col_candidates[0]
            # Agar number ustuni bo'lsa yoki indexni ishlatamiz
            if any(coln.lower() in ['number', 'n', 'num', 'question'] for coln in df.columns):
                num_col = [c for c in df.columns if c.lower() in ['number', 'n', 'num', 'question']][0]
                for _, row in df.iterrows():
                    keys_text += f"{int(row[num_col])}-{str(row[col]).strip().upper()} "
            else:
                # index bo'yicha 1..n
                for idx, val in enumerate(df[col].tolist(), start=1):
                    keys_text += f"{idx}-{str(val).strip().upper()} "
        else:
            # 2) Agar faylda bitta katakda '1-A 2-B...' kabi butun qator bo'lsa
            found = False
            for col in df.columns:
                for cell in df[col].astype(str).tolist():
                    if re.search(r'(\d+)[\.\-\s]*[A-Da-d]', cell):
                        keys_text += cell.strip() + " "
                        found = True
                if found:
                    break
            if not found:
                # 3) Agar dfda faqat 'A','B','C','D' ustunlari bo'lsa va har qator bir savolga mos keladi
                # biz indeksni raqam sifatida olamiz va ustunlardagi belgilangan ustunni 'correct' deb topamiz
                # bunday fayllar uchun majburiy 'correct' ustuni yo'q ekanligini inobatga oling
                # fallback - treat first column as keys list
                first_col = df.columns[0]
                for idx, cell in enumerate(df[first_col].astype(str).tolist(), start=1):
                    val = cell.strip()
                    if re.match(r'^[A-Da-d]$', val):
                        keys_text += f"{idx}-{val.upper()} "
                    else:
                        # agar bo'sh bo'lsa, o'tkazib yuborish
                        pass

        keys_dict = parse_keys(keys_text)
        if not keys_dict:
            await message.answer("❌ Fayldan javob kalitlari olinmadi. Iltimos, fayl formatini tekshiring.")
            return

        # Saqlash
        data = await state.get_data()
        title = data.get('title')
        code = data.get('code')
        question_count = len(keys_dict)
        db = get_db()
        c = db.cursor()
        c.execute(
            'INSERT INTO tests (code, title, correct_keys, question_count, created_by) VALUES (?, ?, ?, ?, ?)',
            (code, title, keys_text.strip(), question_count, message.from_user.id)
        )
        db.commit()
        db.close()

        await message.answer(
            f"✅ Test muvaffaqiyatli yaratildi (fayldan)!\n\n"
            f"📝 Test nomi: <b>{title}</b>\n"
            f"🔑 Test kodi: <code>{code}</code>\n"
            f"📊 Savollar soni: <b>{question_count}</b> ta\n\n",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(is_admin_user=True)
        )
    except Exception as e:
        logger.exception("Faylni o'qishda xato")
        await message.answer("❌ Faylni o'qishda xato yuz berdi. Iltimos, fayl formatini tekshiring.")
    finally:
        try:
            os.remove(local_path)
        except Exception:
            pass
        await state.clear()


@router.callback_query(F.data == "admin_enter_keys_now")
async def admin_enter_keys_now(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    await callback.message.edit_text(
        "✍️ Iltimos, javoblar kalitini matn sifatida yuboring.\n\n"
        "Format: <code>1-A 2-B 3-C 4-D...</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(TestCreation.waiting_for_keys)
    await callback.answer()


@router.message(TestCreation.waiting_for_keys)
async def admin_test_keys(message: Message, state: FSMContext):
    """Javoblar kalitini qabul qilish"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Siz admin emassiz!", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    keys_text = message.text.strip()
    # Kalitlarni tekshirish
    keys_dict = parse_keys(keys_text)

    if not keys_dict:
        await message.answer(
            "❌ Kalitlar formati noto'g'ri!\n\n"
            "To'g'ri format: <code>1-A 2-B 3-C 4-D...</code>",
            parse_mode="HTML"
        )
        return

    question_count = len(keys_dict)

    # Ma'lumotlarni olish
    data = await state.get_data()
    title = data.get('title')
    code = data.get('code')

    # Bazaga saqlash
    db = get_db()
    c = db.cursor()

    try:
        c.execute(
            'INSERT INTO tests (code, title, correct_keys, question_count, created_by) VALUES (?, ?, ?, ?, ?)',
            (code, title, keys_text, question_count, message.from_user.id)
        )
        db.commit()

        await message.answer(
            f"✅ Test muvaffaqiyatli yaratildi!\n\n"
            f"📝 Test nomi: <b>{title}</b>\n"
            f"🔑 Test kodi: <code>{code}</code>\n"
            f"📊 Savollar soni: <b>{question_count}</b> ta\n\n"
            f"O'quvchilar endi bu testni ishlashlari mumkin!",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(is_admin_user=True)
        )

    except Exception as e:
        logger.error(f"Test saqlashda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")

    finally:
        db.close()
        await state.clear()


# ================== O'QUVCHI: TEST ISHLASH ==================
@router.callback_query(F.data == "student_take_test")
async def student_take_test_start(callback: CallbackQuery, state: FSMContext):
    """Test ishlashni boshlash"""
    user_id = callback.from_user.id
    # Obunani tekshirish
    is_subscribed = await check_subscription(callback.bot, user_id)

    if not is_subscribed:
        channel = get_channel_username_from_db(CHANNEL_USERNAME)
        await callback.message.edit_text(
            f"❌ Test ishlash uchun avval {channel} kanaliga a'zo bo'lishingiz kerak!",
            reply_markup=subscription_keyboard()
        )
        return

    await callback.message.edit_text(
        "📝 Test ishlash\n\nTest kodini kiriting:\nMasalan: <code>OT9-2025</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(TestTaking.waiting_for_code)
    await callback.answer()


@router.message(TestTaking.waiting_for_code)
async def student_test_code(message: Message, state: FSMContext):
    """Test kodini tekshirish"""
    code = message.text.strip().upper()
    # Testni topish
    db = get_db()
    c = db.cursor()
    c.execute('SELECT * FROM tests WHERE code = ? AND is_active = 1', (code,))
    test = c.fetchone()

    if not test:
        await message.answer(
            "❌ Bunday test topilmadi yoki test faol emas.\n\n"
            "Iltimos, to'g'ri test kodini kiriting:",
            reply_markup=cancel_keyboard()
        )
        db.close()
        return

    # test tuple: (id, code, title, correct_keys, question_count, created_by, is_active, created_at)
    test_id = test[0]
    test_code = test[1]
    title = test[2]
    correct_keys = test[3]
    question_count = test[4]

    # Test allaqachon topshirilganligini tekshirish
    c.execute('SELECT * FROM students WHERE tg_id = ?', (message.from_user.id,))
    student = c.fetchone()
    if not student:
        await message.answer("❌ Iltimos, avval /start orqali ro'yxatdan o'ting.", reply_markup=main_menu_keyboard())
        db.close()
        await state.clear()
        return

    student_id = student[0]

    c.execute('SELECT * FROM results WHERE user_id = ? AND test_id = ?', (student_id, test_id))
    if c.fetchone():
        await message.answer(
            "⚠️ Siz bu testni allaqachon topshirgansiz!\n\n"
            "Har bir testni faqat bir marta topshirish mumkin.",
            reply_markup=main_menu_keyboard()
        )
        db.close()
        await state.clear()
        return

    db.close()

    await state.update_data(test_id=test_id, test_code=test_code, title=title,
                           correct_keys=correct_keys, question_count=question_count)

    await message.answer(
        f"✅ Test topildi!\n\n"
        f"📝 Test: <b>{title}</b>\n"
        f"📊 Savollar soni: <b>{question_count}</b> ta\n\n"
        f"Javoblaringizni quyidagi formatda yuboring:\n\n"
        f"<code>1-A 2-B 3-C 4-D 5-A...</code>\n\n"
        f"Masalan:\n"
        f"<code>1-A 2-D 3-C 4-B 5-A 6-C 7-D 8-B 9-A 10-C</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(TestTaking.waiting_for_answers)


@router.message(TestTaking.waiting_for_answers)
async def student_test_answers(message: Message, state: FSMContext):
    """Javoblarni qabul qilish va tekshirish"""
    user_answers = message.text.strip()
    # Javoblarni tekshirish
    data = await state.get_data()
    test_id = data.get('test_id')
    title = data.get('title')
    correct_keys = data.get('correct_keys')
    question_count = data.get('question_count')

    # Parse qilish
    user_dict = parse_keys(user_answers)

    if not user_dict:
        await message.answer(
            "❌ Javoblar formati noto'g'ri!\n\n"
            "To'g'ri format: <code>1-A 2-B 3-C 4-D...</code>",
            parse_mode="HTML"
        )
        return

    if len(user_dict) != question_count:
        await message.answer(
            f"❌ Javoblar soni noto'g'ri!\n\n"
            f"Testda <b>{question_count}</b> ta savol bor, "
            f"siz <b>{len(user_dict)}</b> ta javob yubordingiz.\n\n"
            f"Iltimos, barcha javoblarni yuboring:",
            parse_mode="HTML"
        )
        return

    # Tekshirish
    check_result = check_answers(user_answers, correct_keys)

    # Bazaga saqlash
    db = get_db()
    c = db.cursor()

    c.execute('SELECT id FROM students WHERE tg_id = ?', (message.from_user.id,))
    student_row = c.fetchone()
    if not student_row:
        await message.answer("❌ Ro'yxatdan o'tilishida xato. Iltimos /start ni qayta yuboring.")
        db.close()
        await state.clear()
        return
    student_id = student_row[0]

    try:
        c.execute(
            'INSERT INTO results (user_id, test_id, correct_count, total_questions, user_answers) '
            'VALUES (?, ?, ?, ?, ?)',
            (student_id, test_id, check_result['correct_count'],
             check_result['total'], user_answers)
        )
        db.commit()

        # Natijani ko'rsatish
        result_msg = format_result_message(check_result)

        await message.answer(
            result_msg,
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )

    except Exception as e:
        logger.error(f"Natijani saqlashda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")

    finally:
        db.close()
        await state.clear()


# ================== STATISTIKA ==================
@router.callback_query(F.data == "admin_statistics")
async def admin_show_statistics(callback: CallbackQuery):
    """Statistikani ko'rsatish"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Siz admin emassiz!", show_alert=True)
        return
    db = get_db()
    c = db.cursor()

    # Barcha testlar
    c.execute('SELECT id, title, code FROM tests WHERE is_active = 1')
    tests = c.fetchall()

    if not tests:
        await callback.message.edit_text(
            "📊 Hozircha hech qanday test yaratilmagan.",
            reply_markup=main_menu_keyboard(is_admin_user=True)
        )
        db.close()
        return

    # Har bir test uchun statistika
    stats_text = "📊 <b>STATISTIKA</b>\n\n"

    for test_id, title, code in tests:
        c.execute('''
            SELECT s.full_name, r.correct_count, r.total_questions
            FROM results r
            JOIN students s ON r.user_id = s.id
            WHERE r.test_id = ?
            ORDER BY r.correct_count DESC
        ''', (test_id,))

        results = c.fetchall()

        stats_text += f"📝 <b>{title}</b> (Kod: <code>{code}</code>)\n"

        if not results:
            stats_text += "   └ Hali hech kim topshirmagan\n\n"
        else:
            stats_text += f"   └ Jami: {len(results)} ta o'quvchi\n\n"

            for idx, (name, correct, total) in enumerate(results[:50], 1):
                # Natija faqat to'g'ri javoblar soni bo'lishi kerak — ball kerak emas
                stats_text += f"{idx}. {name} — <b>{correct} ta</b>\n"

            if len(results) > 50:
                stats_text += f"\n   ... va yana {len(results) - 50} ta o'quvchi\n"

            stats_text += "\n"

    db.close()

    await callback.message.edit_text(
        stats_text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_admin_user=True)
    )
    await callback.answer()


# ================== MENING NATIJALARIM ==================
@router.callback_query(F.data == "my_results")
async def show_my_results(callback: CallbackQuery):
    """Foydalanuvchi natijalarini ko'rsatish"""
    user_id = callback.from_user.id
    db = get_db()
    c = db.cursor()

    c.execute('SELECT id FROM students WHERE tg_id = ?', (user_id,))
    student = c.fetchone()

    if not student:
        await callback.answer("❌ Siz ro'yxatdan o'tmagansiz!", show_alert=True)
        db.close()
        return

    student_id = student[0]

    # Natijalarni olish
    c.execute('''
        SELECT t.title, t.code, r.correct_count, r.total_questions, r.submitted_at
        FROM results r
        JOIN tests t ON r.test_id = t.id
        WHERE r.user_id = ?
        ORDER BY r.submitted_at DESC
    ''', (student_id,))

    results = c.fetchall()
    db.close()

    if not results:
        await callback.message.edit_text(
            "📖 Siz hali hech qanday test topshirmagansiz.",
            reply_markup=main_menu_keyboard()
        )
        return

    # Natijalarni ko'rsatish
    results_text = "📖 <b>MENING NATIJALARIM</b>\n\n"

    for idx, (title, code, correct, total, submitted_at) in enumerate(results, 1):
        results_text += f"{idx}. <b>{title}</b>\n"
        results_text += f"   Kod: <code>{code}</code>\n"
        results_text += f"   Natija: <b>{correct} ta</b>\n"
        results_text += f"   Sana: {submitted_at}\n\n"

    await callback.message.edit_text(
        results_text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()


# ================== BEKOR QILISH ==================
@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    """Har qanday amalni bekor qilish"""
    await state.clear()
    is_admin_user = is_admin(callback.from_user.id)

    await callback.message.edit_text(
        "❌ Amal bekor qilindi.",
        reply_markup=main_menu_keyboard(is_admin_user=is_admin_user)
    )
    await callback.answer()


# ================== ASOSIY FUNKSIYA ==================
async def main():
    """Botni ishga tushirish"""
    # Ma'lumotlar bazasini yaratish
    init_db()
    # Bot va Dispatcher
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Routerni ulash
    dp.include_router(router)

    # Botni ishga tushirish
    logger.info("Bot ishga tushdi!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


# ================== ISHGA TUSHIRISH ==================
if __name__ == "__main__":
    asyncio.run(main())
