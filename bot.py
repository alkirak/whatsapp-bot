import os
import re
import datetime
from collections import defaultdict

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN") or "PASTE_TOKEN_HERE"

BRANCHES = {
    -5174468450: "t",  # Turkistan
    -5277664922: "k",  # Kentau
}

REPORTS = defaultdict(list)

# ================== ПАРСИНГ ==================

def parse_report(text: str):
    date = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    shift = "ночь" if "ноч" in text.lower() else "день" if "день" in text.lower() else None

    total = re.search(r"Общая касса:\s*([\d\s]+)", text)
    cash = re.search(r"Нал:\s*([\d\s]+)", text)
    kaspi = re.search(r"Каспи:\s*([\d\s]+)", text)

    if not (date and shift and total):
        return None

    def num(x):
        return int(x.replace(" ", "")) if x else 0

    return {
        "date": date.group(),
        "shift": shift,
        "total": num(total.group(1)),
        "cash": num(cash.group(1)) if cash else 0,
        "kaspi": num(kaspi.group(1)) if kaspi else 0,
    }

# ================== ХЕНДЛЕРЫ ==================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    branch = BRANCHES.get(chat_id)

    if not branch:
        return

    parsed = parse_report(update.message.text)
    if not parsed:
        return

    REPORTS[branch].append(parsed)

    await update.message.reply_text(
        f"✅ Отчёт принят\n"
        f"Филиал: {branch}\n"
        f"{parsed['date']} — {parsed['shift'].capitalize()}\n"
        f"Оборот: {parsed['total']}"
    )

async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 Недельный отчёт\n"]

    for branch, reports in REPORTS.items():
        total = sum(r["total"] for r in reports)
        lines.append(f"🏢 {branch.upper()}: {total}")

    text = "\n".join(lines)

    for chat_id in BRANCHES:
        await context.bot.send_message(chat_id=chat_id, text=text)

    REPORTS.clear()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот запущен и принимает отчёты")

# ================== ЗАПУСК ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.job_queue.run_weekly(
        weekly_report,
        time=datetime.time(hour=11, minute=0),
        days=(0,)  # понедельник
    )

    print("🤖 Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
