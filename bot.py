#!/usr/bin/env python3
"""
Красивый Telegram-бот учёта зарплаты и долгов
"""

import os
import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ОШИБКА: Не указан BOT_TOKEN. Добавь его в Variables на Railway.")
    exit(1)

# Стартовые данные (то, что уже накопилось до бота)
INITIAL_DEBT = 18500          # твой текущий долг
INITIAL_RECEIVED = 25200          # сколько уже получил из долга

DB_PATH = Path(__file__).parent / "salary.db"

# Состояния
WAITING_CUSTOM_AMOUNT, WAITING_RECEIVE_AMOUNT, WAITING_FIX_RECEIVED, WAITING_FIX_DEBT = range(4)


# ================== БАЗА ДАННЫХ ==================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            debt REAL DEFAULT 0,
            received REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            action TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    return sqlite3.connect(DB_PATH)


def ensure_user(user_id: int, username: str = None, first_name: str = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, debt, received) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, INITIAL_DEBT, INITIAL_RECEIVED),
        )
        if INITIAL_DEBT > 0:
            cur.execute(
                "INSERT INTO history (user_id, amount, action, note) VALUES (?, ?, ?, ?)",
                (user_id, INITIAL_DEBT, "debt_add", "Стартовый долг (до бота)"),
            )
        if INITIAL_RECEIVED > 0:
            cur.execute(
                "INSERT INTO history (user_id, amount, action, note) VALUES (?, ?, ?, ?)",
                (user_id, INITIAL_RECEIVED, "received", "Уже получено до бота"),
            )
        conn.commit()
    conn.close()


def get_balance(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT debt, received FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"debt": row[0], "received": row[1]}
    return {"debt": 0, "received": 0}


def update_balance(user_id: int, debt_delta: float = 0, received_delta: float = 0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users 
        SET debt = MAX(0, debt + ?),
            received = received + ?
        WHERE user_id = ?
        """,
        (debt_delta, received_delta, user_id),
    )
    conn.commit()
    conn.close()


def add_history(user_id: int, amount: float, action: str, note: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO history (user_id, amount, action, note) VALUES (?, ?, ?, ?)",
        (user_id, amount, action, note),
    )
    conn.commit()
    conn.close()


def get_history(user_id: int, limit: int = 12):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT amount, action, note, created_at 
        FROM history 
        WHERE user_id = ? 
        ORDER BY id DESC 
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_last_operation(user_id: int):
    """Возвращает последнюю операцию: (id, amount, action, note)"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, amount, action, note 
        FROM history 
        WHERE user_id = ? 
        ORDER BY id DESC 
        LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def undo_last_operation(user_id: int):
    """Отменяет последнюю операцию. Возвращает (успех, сообщение)"""
    last = get_last_operation(user_id)
    if not last:
        return False, "Нет операций для отмены."

    op_id, amount, action, note = last

    # Не даём отменять стартовые записи
    if note and ("Стартовый" in note or "до бота" in note):
        return False, "Нельзя отменить стартовую запись."

    conn = get_conn()
    cur = conn.cursor()

    if action == "received":
        # Было: received += amount, debt -= reduce
        # Для простоты: уменьшаем received, увеличиваем debt на amount
        cur.execute(
            """
            UPDATE users 
            SET received = MAX(0, received - ?),
                debt = debt + ?
            WHERE user_id = ?
            """,
            (amount, amount, user_id),
        )
    elif action == "debt_add":
        # Было: debt += amount
        cur.execute(
            """
            UPDATE users 
            SET debt = MAX(0, debt - ?)
            WHERE user_id = ?
            """,
            (amount, user_id),
        )
    else:
        conn.close()
        return False, "Эту операцию нельзя отменить."

    # Удаляем запись из истории
    cur.execute("DELETE FROM history WHERE id = ?", (op_id,))
    conn.commit()
    conn.close()

    return True, f"Отменено: {amount:,.0f} лей ({action})"


def set_balance(user_id: int, received: float = None, debt: float = None):
    """Установить точные значения баланса"""
    conn = get_conn()
    cur = conn.cursor()
    if received is not None and debt is not None:
        cur.execute(
            "UPDATE users SET received = ?, debt = ? WHERE user_id = ?",
            (received, debt, user_id),
        )
    elif received is not None:
        cur.execute(
            "UPDATE users SET received = ? WHERE user_id = ?",
            (received, user_id),
        )
    elif debt is not None:
        cur.execute(
            "UPDATE users SET debt = ? WHERE user_id = ?",
            (debt, user_id),
        )
    conn.commit()
    conn.close()


# ================== КРАСИВЫЕ СООБЩЕНИЯ ==================
def format_balance(user_id: int) -> str:
    bal = get_balance(user_id)
    debt = bal["debt"]
    received = bal["received"]

    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>ТВОЙ БАЛАНС</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟢 <b>Получено:</b>   <code>{received:,.0f}</code> лей\n"
        f"🔴 <b>Долг:</b>       <code>{debt:,.0f}</code> лей\n\n"
    )

    if debt > 0:
        text += f"📌 Осталось получить: <b>{debt:,.0f}</b> лей\n"
    else:
        text += "✅ Долгов нет! Всё получено.\n"

    text += "━━━━━━━━━━━━━━━━━━━━"
    return text


def main_keyboard():
    keyboard = [
        [
            KeyboardButton("➕ +700"),
            KeyboardButton("💵 Другая сумма"),
        ],
        [
            KeyboardButton("🟢 Получил деньги"),
            KeyboardButton("🔴 Добавить в долг"),
        ],
        [
            KeyboardButton("📊 Баланс"),
            KeyboardButton("📜 История"),
        ],
        [
            KeyboardButton("↩️ Отменить последнюю"),
            KeyboardButton("✏️ Исправить суммы"),
        ],
        [KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ================== ОБРАБОТЧИКИ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)

    text = (
        f"Привет, <b>{user.first_name}</b>! 👋\n\n"
        "Я красивый бот для учёта твоей зарплаты и долгов.\n\n"
        f"{format_balance(user.id)}\n\n"
        "Выбирай действие кнопками ниже 👇"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    await update.message.reply_text(
        format_balance(user_id),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    history = get_history(user_id)

    if not history:
        await update.message.reply_text(
            "📜 История пока пустая.",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["📜 <b>Последние операции:</b>\n"]
    for amount, action, note, created_at in history:
        dt = created_at[:16].replace("T", " ")
        if action == "received":
            emoji = "🟢"
            sign = "+"
        elif action == "debt_add":
            emoji = "🔴"
            sign = "+"
        elif action == "debt_reduce":
            emoji = "🟢"
            sign = "−"
        else:
            emoji = "⚪"
            sign = ""

        note_str = f" — <i>{note}</i>" if note else ""
        lines.append(f"{emoji} {sign}{amount:,.0f} лей{note_str}\n   <code>{dt}</code>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Как пользоваться ботом</b>\n\n"
        "🟢 <b>Получил деньги</b> — когда тебе реально выплатили\n"
        "   (автоматически гасит долг)\n\n"
        "🔴 <b>Добавить в долг</b> — когда не заплатили\n\n"
        "➕ <b>+700</b> — быстро добавить 700 лей\n"
        "💵 <b>Другая сумма</b> — любая сумма\n\n"
        "📊 <b>Баланс</b> — сколько получил и сколько долг\n"
        "📜 <b>История</b> — все операции\n"
        "↩️ <b>Отменить последнюю</b> — отменить последнюю операцию\n"
        "✏️ <b>Исправить суммы</b> — вручную поставить нужные цифры\n\n"
        "Стартовый долг <b>18 500</b> лей уже загружен.\n"
        "Получено за июнь–июль: <b>25 200</b> лей."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())


# ---------- Быстрое +700 ----------
async def add_700(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    keyboard = [
        [
            InlineKeyboardButton("🟢 Получил 700", callback_data="quick_received_700"),
            InlineKeyboardButton("🔴 В долг 700", callback_data="quick_debt_700"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await update.message.reply_text(
        "➕ <b>+700 лей</b>\n\nКуда записать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------- Другая сумма ----------
async def custom_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💵 Введи сумму цифрами:\n\n"
        "Например: <code>3500</code> или <code>1900</code>\n\n"
        "Чтобы отменить — напиши <b>отмена</b>",
        parse_mode="HTML",
    )
    return WAITING_CUSTOM_AMOUNT


async def custom_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    
    # Возможность отменить
    if text in ("отмена", "cancel", "отменить", "0", "-", "назад"):
        await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
        return ConversationHandler.END

    cleaned = text.replace(" ", "").replace(",", ".")
    try:
        amount = float(cleaned)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно положительное число.\n\n"
            "Или напиши <b>отмена</b>, чтобы выйти.",
            parse_mode="HTML"
        )
        return WAITING_CUSTOM_AMOUNT

    context.user_data["amount"] = amount

    keyboard = [
        [
            InlineKeyboardButton("🟢 Получил", callback_data="custom_received"),
            InlineKeyboardButton("🔴 В долг", callback_data="custom_debt"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await update.message.reply_text(
        f"Сумма: <b>{amount:,.0f}</b> лей\n\nКуда записать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


# ---------- Получил деньги ----------
async def receive_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    bal = get_balance(user_id)

    text = (
        "🟢 <b>Получил деньги</b>\n\n"
        f"Сейчас долг: <b>{bal['debt']:,.0f}</b> лей\n\n"
        "Введи сумму, которую тебе выплатили:\n\n"
        "Чтобы отменить — напиши <b>отмена</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    return WAITING_RECEIVE_AMOUNT


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    
    # Возможность отменить
    if text in ("отмена", "cancel", "отменить", "0", "-", "назад"):
        await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
        return ConversationHandler.END

    cleaned = text.replace(" ", "").replace(",", ".")
    try:
        amount = float(cleaned)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно положительное число.\n\n"
            "Или напиши <b>отмена</b>, чтобы выйти.",
            parse_mode="HTML"
        )
        return WAITING_RECEIVE_AMOUNT

    user_id = update.effective_user.id
    bal = get_balance(user_id)
    current_debt = bal["debt"]

    if current_debt > 0:
        reduce = min(amount, current_debt)
        update_balance(user_id, debt_delta=-reduce, received_delta=amount)
        add_history(user_id, amount, "received", f"Получено (погашено долга {reduce:,.0f})")

        msg = (
            f"🟢 <b>Отлично!</b>\n\n"
            f"Получено: <b>{amount:,.0f}</b> лей\n"
            f"Погашено долга: <b>{reduce:,.0f}</b> лей\n"
        )
        if amount > reduce:
            msg += f"Сверх долга: <b>{amount - reduce:,.0f}</b> лей\n"

        msg += f"\n{format_balance(user_id)}"
    else:
        update_balance(user_id, received_delta=amount)
        add_history(user_id, amount, "received", "Получено")

        msg = (
            f"🟢 <b>Записано!</b>\n\n"
            f"Получено: <b>{amount:,.0f}</b> лей\n\n"
            f"{format_balance(user_id)}"
        )

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_keyboard())
    return ConversationHandler.END


# ---------- Добавить в долг ----------
async def debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔴 <b>Добавить в долг</b>\n\n"
        "Введи сумму, которую не выплатили:\n\n"
        "Чтобы отменить — напиши <b>отмена</b>",
        parse_mode="HTML",
    )
    return WAITING_CUSTOM_AMOUNT


# ---------- Inline кнопки ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "cancel":
        await query.edit_message_text("❌ Отменено")
        return

    if data == "quick_received_700":
        amount = 700
        bal = get_balance(user_id)
        reduce = min(amount, bal["debt"])
        update_balance(user_id, debt_delta=-reduce, received_delta=amount)
        add_history(user_id, amount, "received", "Быстрое +700 (получил)")
        text = (
            f"🟢 Получено <b>700</b> лей\n"
            f"Погашено долга: <b>{reduce:,.0f}</b>\n\n"
            f"{format_balance(user_id)}"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    elif data == "quick_debt_700":
        amount = 700
        update_balance(user_id, debt_delta=amount)
        add_history(user_id, amount, "debt_add", "Быстрое +700 (в долг)")
        text = (
            f"🔴 В долг добавлено <b>700</b> лей\n\n"
            f"{format_balance(user_id)}"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    elif data == "custom_received":
        amount = context.user_data.get("amount", 0)
        bal = get_balance(user_id)
        reduce = min(amount, bal["debt"])
        update_balance(user_id, debt_delta=-reduce, received_delta=amount)
        add_history(user_id, amount, "received", f"Получено (погашено {reduce:,.0f})")
        text = (
            f"🟢 Получено <b>{amount:,.0f}</b> лей\n"
            f"Погашено долга: <b>{reduce:,.0f}</b>\n\n"
            f"{format_balance(user_id)}"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    elif data == "custom_debt":
        amount = context.user_data.get("amount", 0)
        update_balance(user_id, debt_delta=amount)
        add_history(user_id, amount, "debt_add", "Добавлено в долг")
        text = (
            f"🔴 В долг добавлено <b>{amount:,.0f}</b> лей\n\n"
            f"{format_balance(user_id)}"
        )
        await query.edit_message_text(text, parse_mode="HTML")



# ---------- Отменить последнюю ----------
async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    
    success, msg = undo_last_operation(user_id)
    
    if success:
        text = f"↩️ <b>{msg}</b>\n\n{format_balance(user_id)}"
    else:
        text = f"⚠️ {msg}"
    
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())


# ---------- Исправить суммы ----------
async def fix_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    bal = get_balance(user_id)
    
    text = (
        "✏️ <b>Исправить суммы</b>\n\n"
        f"Сейчас:\n"
        f"🟢 Получено: <b>{bal['received']:,.0f}</b>\n"
        f"🔴 Долг: <b>{bal['debt']:,.0f}</b>\n\n"
        "Введи <b>новое значение Получено</b> (или напиши <b>пропустить</b>):"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    return WAITING_FIX_RECEIVED


async def fix_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    
    if text in ("отмена", "cancel", "отменить"):
        await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
        return ConversationHandler.END
    
    if text in ("пропустить", "skip", "-"):
        context.user_data["new_received"] = None
    else:
        try:
            amount = float(text.replace(" ", "").replace(",", "."))
            if amount < 0:
                raise ValueError
            context.user_data["new_received"] = amount
        except ValueError:
            await update.message.reply_text(
                "Нужно число ≥ 0 или <b>пропустить</b> / <b>отмена</b>",
                parse_mode="HTML"
            )
            return WAITING_FIX_RECEIVED
    
    await update.message.reply_text(
        "Теперь введи <b>новое значение Долг</b>\n"
        "(или <b>пропустить</b> / <b>отмена</b>):",
        parse_mode="HTML"
    )
    return WAITING_FIX_DEBT


async def fix_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    
    if text in ("отмена", "cancel", "отменить"):
        await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
        return ConversationHandler.END
    
    new_debt = None
    if text not in ("пропустить", "skip", "-"):
        try:
            amount = float(text.replace(" ", "").replace(",", "."))
            if amount < 0:
                raise ValueError
            new_debt = amount
        except ValueError:
            await update.message.reply_text(
                "Нужно число ≥ 0 или <b>пропустить</b> / <b>отмена</b>",
                parse_mode="HTML"
            )
            return WAITING_FIX_DEBT
    
    user_id = update.effective_user.id
    new_received = context.user_data.get("new_received")
    
    set_balance(user_id, received=new_received, debt=new_debt)
    
    # Записываем в историю
    note_parts = []
    if new_received is not None:
        note_parts.append(f"Получено={new_received:,.0f}")
    if new_debt is not None:
        note_parts.append(f"Долг={new_debt:,.0f}")
    add_history(user_id, 0, "fix", "Исправление: " + ", ".join(note_parts))
    
    await update.message.reply_text(
        f"✅ Суммы исправлены!\n\n{format_balance(user_id)}",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ---------- Текстовые кнопки ----------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "➕ +700":
        await add_700(update, context)
    elif text == "💵 Другая сумма":
        return await custom_amount_start(update, context)
    elif text == "🟢 Получил деньги":
        return await receive_start(update, context)
    elif text == "🔴 Добавить в долг":
        return await debt_start(update, context)
    elif text == "📊 Баланс":
        await show_balance(update, context)
    elif text == "📜 История":
        await show_history(update, context)
    elif text == "ℹ️ Помощь":
        await help_command(update, context)
    elif text == "↩️ Отменить последнюю":
        await undo_last(update, context)
    elif text == "✏️ Исправить суммы":
        return await fix_start(update, context)
    else:
        # Если просто написал число
        cleaned = text.strip().replace(" ", "").replace(",", ".")
        try:
            amount = float(cleaned)
            if amount > 0:
                context.user_data["amount"] = amount
                keyboard = [
                    [
                        InlineKeyboardButton("🟢 Получил", callback_data="custom_received"),
                        InlineKeyboardButton("🔴 В долг", callback_data="custom_debt"),
                    ],
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
                ]
                await update.message.reply_text(
                    f"Сумма: <b>{amount:,.0f}</b> лей\n\nКуда записать?",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return
        except ValueError:
            pass

        await update.message.reply_text(
            "Используй кнопки ниже 👇",
            reply_markup=main_keyboard(),
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ================== ЗАПУСК ==================
def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    if not BOT_TOKEN:
        print("ОШИБКА: Не указан BOT_TOKEN")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💵 Другая сумма$"), custom_amount_start),
            MessageHandler(filters.Regex("^🟢 Получил деньги$"), receive_start),
            MessageHandler(filters.Regex("^🔴 Добавить в долг$"), debt_start),
            MessageHandler(filters.Regex("^✏️ Исправить суммы$"), fix_start),
        ],
        states={
            WAITING_CUSTOM_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_amount_received)
            ],
            WAITING_RECEIVE_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)
            ],
            WAITING_FIX_RECEIVED: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fix_received)
            ],
            WAITING_FIX_DEBT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fix_debt)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", show_balance))
    app.add_handler(CommandHandler("history", show_history))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("✅ Бот запущен и готов к работе!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
