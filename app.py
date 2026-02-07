import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

# ======================
# НАСТРОЙКИ
# ======================
BOT_TOKEN = "8388581363:AAHzhL7VrXMK4O4y1dAFU-sQXIAzLUvev-o"

TZ = ZoneInfo("Asia/Almaty")

GROUP_BRANCH = {
    -5174468450: "Polygon Turkistan",
    -5277664922: "Polygon Kentau",
}

REPORT_DAY = 0  # Monday
REPORT_HOUR = 11
REPORT_MINUTE = 0

DB = "reports.db"

# ======================
# DB
# ======================
def db():
    return sqlite3.connect(DB)

def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            branch TEXT,
            date TEXT,
            shift TEXT,
            employee TEXT,
            total INTEGER,
            cash INTEGER,
            kaspi INTEGER,
            drinks INTEGER,
            drinks_cash INTEGER,
            drinks_kaspi INTEGER,
            warnings TEXT
        )
        """)

# ======================
# UTILS
# ======================
def num(x):
    return int(re.sub(r"[^\d]", "", x))

def fmt(n):
    return f"{n:,}".replace(",", " ")

# ======================
# PARSER
# ======================
def parse_report(text: str):
    date_m = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    shift_m = re.search(r"(ночь|день)", text.lower())

    if not date_m or not shift_m:
        return None

    date = date_m.group()
    shift = "Ночь" if "ноч" in shift_m.group() else "День"

    total = cash = kaspi = None
    drinks = dc = dk = None
    warnings = []

    m = re.search(r"Общая касса\s*:\s*([\d\s]+)", text, re.I)
    if m:
        total = num(m.group(1))

    m = re.search(r"Нал\s*:\s*([\d\s]+)", text, re.I)
    if m:
        cash = num(m.group(1))

    m = re.search(r"Каспи\s*:\s*([\d\s]+)", text, re.I)
    if m:
        kaspi = num(m.group(1))

    m = re.search(r"Напитки\s*:\s*([\d\s]+)", text, re.I)
    if m:
        drinks = num(m.group(1))
        sec = text[m.end():]
        m1 = re.search(r"Нал\s*:\s*([\d\s]+)", sec, re.I)
        m2 = re.search(r"Каспи\s*:\s*([\d\s]+)", sec, re.I)
        if m1: dc = num(m1.group(1))
        if m2: dk = num(m2.group(1))

    if total and cash and kaspi and total != cash + kaspi:
        warnings.append("⚠️ Общая касса ≠ Нал + Каспи")

    if drinks and dc and dk and drinks != dc + dk:
        warnings.append("⚠️ Напитки ≠ Нал + Каспи")

    return {
        "date": date,
        "shift": shift,
        "total": total,
        "cash": cash,
        "kaspi": kaspi,
        "drinks": drinks,
        "dc": dc,
        "dk": dk,
        "warnings": "\n".join(warnings)
    }

# ======================
# HANDLERS
# ======================
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_BRANCH:
        return

    parsed = parse_report(update.message.text)
    if not parsed:
        return

    with db() as c:
        c.execute("""
        INSERT INTO reports (ts, branch, date, shift, total, cash, kaspi, drinks, drinks_cash, drinks_kaspi, warnings)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(TZ).isoformat(),
            GROUP_BRANCH[chat_id],
            parsed["date"],
            parsed["shift"],
            parsed["total"],
            parsed["cash"],
            parsed["kaspi"],
            parsed["drinks"],
            parsed["dc"],
            parsed["dk"],
            parsed["warnings"]
        ))

    msg = "✅ Отчёт принят"
    if parsed["warnings"]:
        msg += "\n" + parsed["warnings"] + "\n📌 Напитки указываются отдельно"
    await update.message.reply_text(msg)

# ======================
# WEEKLY REPORT
# ======================
async def weekly_report(app: Application):
    now = datetime.now(TZ)
    start = (now - timedelta(days=7)).strftime("%d.%m.%Y")

    with db() as c:
        rows = c.execute("""
        SELECT branch, SUM(total), SUM(drinks)
        FROM reports
        WHERE date >= ?
        GROUP BY branch
        """, (start,)).fetchall()

    for branch, total, drinks in rows:
        text = (
            f"📊 {branch}\n"
            f"Неделя: последние 7 дней\n\n"
            f"💰 Оборот: {fmt(total or 0)}\n"
            f"🍹 Напитки: {fmt(drinks or 0)}"
        )
        for gid, b in GROUP_BRANCH.items():
            if b == branch:
                await app.bot.send_message(gid, text)

# ======================
# MAIN
# ======================
async def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        weekly_report,
        "cron",
        day_of_week=REPORT_DAY,
        hour=REPORT_HOUR,
        minute=REPORT_MINUTE,
        args=[app]
    )
    scheduler.start()

    print("🤖 Bot started")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
