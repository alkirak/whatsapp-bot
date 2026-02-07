import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# ========= НАСТРОЙКИ =========

BOT_TOKEN = "8388581363:AAHzhL7VrXMK4O4y1dAFU-sQXIAzLUvev-o"

BRANCHES = {
    "t": -5174468450,   # Turkistan
    "k": -5277664922,   # Kentau
}

REPORT_DAY = 0   # Понедельник (0 = Monday)
REPORT_HOUR = 11
REPORT_MINUTE = 0

# ========= ЛОГИ =========

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ========= ХРАНИЛИЩЕ =========
# (в будущем можно заменить на БД)

reports = []


# ========= ПАРСИНГ ОТЧЁТА =========

def parse_report(text: str):
    data = {
        "date": None,
        "shift": None,
        "total": None,
        "branch": None,
    }

    lines = text.lower().splitlines()

    for line in lines:
        if "день" in line:
            data["shift"] = "День"
        if "ночь" in line:
            data["shift"] = "Ночь"

        if "общая касса" in line:
            digits = "".join(c for c in line if c.isdigit())
            if digits:
                data["total"] = int(digits)

        for key in BRANCHES:
            if key in line:
                data["branch"] = key

        for part in line.split():
            if "." in part and len(part) == 10:
                data["date"] = part

    return data


# ========= ОБРАБОТКА СООБЩЕНИЙ =========

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.message.chat_id

    report = parse_report(text)

    if not report["date"] or not report["shift"] or not report["total"]:
        return

    report["chat_id"] = chat_id
    reports.append(report)

    await update.message.reply_text(
        f"✅ Отчёт принят\n"
        f"📅 {report['date']}\n"
        f"🕒 {report['shift']}\n"
        f"💰 {report['total']}"
    )


# ========= КОМАНДЫ =========

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Chat ID: {update.message.chat_id}"
    )


async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    start = now - timedelta(days=7)

    summary = {}

    for r in reports:
        try:
            d = datetime.strptime(r["date"], "%d.%m.%Y")
        except:
            continue

        if d >= start:
            b = r["branch"] or "unknown"
            summary[b] = summary.get(b, 0) + (r["total"] or 0)

    text = "📊 *Недельный отчёт*\n\n"

    if not summary:
        text += "Нет данных"
    else:
        for b, total in summary.items():
            name = "Turkistan" if b == "t" else "Kentau"
            text += f"🏢 {name}: {total}\n"

    for chat_id in BRANCHES.values():
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown"
        )


# ========= ЗАПУСК =========

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(
        weekly_report,
        time=datetime.now().replace(
            hour=REPORT_HOUR,
            minute=REPORT_MINUTE,
            second=0,
        ).time(),
        days=(REPORT_DAY,)
    )

    print("🤖 Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
