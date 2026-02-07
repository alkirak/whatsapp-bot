import re
import sqlite3
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ================= НАСТРОЙКИ =================

BOT_TOKEN = "8388581363:AAHzhL7VrXMK4O4y1dAFU-sQXIAzLUvev-o"

TZ = pytz.timezone("Asia/Almaty")

DB_NAME = "reports.db"

BRANCH_CODES = {
    "t": "Turkistan",
    "k": "Kentau",
}

# =============================================


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        branch TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        branch TEXT,
        date TEXT,
        shift TEXT,
        total INTEGER,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


# ---------- HELPERS ----------

def get_branch(chat_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT branch FROM chats WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def save_branch(chat_id, branch):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO chats (chat_id, branch) VALUES (?, ?)",
        (chat_id, branch),
    )
    conn.commit()
    conn.close()


def save_report(chat_id, branch, date, shift, total):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reports (chat_id, branch, date, shift, total, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        branch,
        date,
        shift,
        total,
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()


# ---------- COMMANDS ----------

async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Используй: /bind t или /bind k")
        return

    code = context.args[0].lower()
    if code not in BRANCH_CODES:
        await update.message.reply_text("❌ Неизвестный филиал. Доступно: t, k")
        return

    save_branch(update.effective_chat.id, BRANCH_CODES[code])
    await update.message.reply_text(
        f"✅ Филиал привязан: {BRANCH_CODES[code]} ({code})"
    )


# ---------- REPORT PARSER ----------

def parse_report(text: str):
    date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
    shift_match = re.search(r"(Ночь|День)", text, re.IGNORECASE)
    total_match = re.search(r"Общая касса[:\s]*([\d\s]+)", text)

    if not date_match or not shift_match or not total_match:
        return None

    date = date_match.group(1)
    shift = shift_match.group(1).capitalize()
    total = int(total_match.group(1).replace(" ", ""))

    return date, shift, total


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    branch = get_branch(chat_id)

    if not branch:
        return  # филиал не привязан — молчим

    parsed = parse_report(update.message.text)
    if not parsed:
        return

    date, shift, total = parsed

    save_report(chat_id, branch, date, shift, total)

    await update.message.reply_text(
        f"✅ Отчёт принят\n"
        f"🏢 {branch}\n"
        f"📅 {date}\n"
        f"🕒 {shift}\n"
        f"💰 Оборот: {total:,}".replace(",", " ")
    )


# ---------- WEEKLY REPORT ----------

def send_weekly_reports(app: Application):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    cur.execute("""
        SELECT chat_id, branch, SUM(total)
        FROM reports
        WHERE created_at >= ?
        GROUP BY chat_id, branch
    """, (week_ago,))

    rows = cur.fetchall()
    conn.close()

    for chat_id, branch, total in rows:
        text = (
            f"📊 Недельный отчёт\n"
            f"🏢 {branch}\n"
            f"💰 Итого за неделю: {int(total):,}".replace(",", " ")
        )
        app.bot.send_message(chat_id=chat_id, text=text)


# ---------- MAIN ----------

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("bind", bind))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(
        send_weekly_reports,
        "cron",
        day_of_week="mon",
        hour=11,
        minute=0,
        args=[app],
    )
    scheduler.start()

    print("🤖 Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
