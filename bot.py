import os
import re
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Твои группы
CHAT_TURKISTAN = -5174468450
CHAT_KENTAU    = -5277664922

CHAT_TO_BRANCH = {
    CHAT_TURKISTAN: "t",
    CHAT_KENTAU: "k",
}

TZ = os.environ.get("TZ", "Asia/Almaty")  # важно для времени 11:00

# Хранилище в памяти (на перезапуске очищается). Потом подключим БД.
REPORTS = defaultdict(list)


# ================== ПАРСИНГ ==================
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
INT_RE = re.compile(r"(\d[\d\s]*)")

def _to_int(s: str) -> int:
    return int(s.replace(" ", ""))

def parse_single_report(block: str):
    """
    Ищем в одном блоке:
    дата: 06.02.2026
    смена: день/ночь
    общая касса: число
    Нал: число
    Каспи: число
    Напитки: число (опционально)
    """
    low = block.lower()

    mdate = DATE_RE.search(block)
    if not mdate:
        return None
    date_str = mdate.group(1)

    shift = None
    if "ноч" in low:
        shift = "ночь"
    elif "день" in low:
        shift = "день"
    else:
        return None

    def grab(label: str):
        # Ищем строку вида "Общая касса: 83450"
        # Берём первое число после двоеточия
        pattern = re.compile(rf"{label}\s*:\s*{INT_RE.pattern}", re.IGNORECASE)
        m = pattern.search(block)
        return _to_int(m.group(1)) if m else None

    total = grab("Общая касса")
    if total is None:
        return None

    cash  = grab("Нал") or 0
    kaspi = grab("Каспи") or 0
    drinks = grab("Напитки") or 0

    return {
        "date": date_str,     # dd.mm.yyyy
        "shift": shift,       # день/ночь
        "total": total,
        "cash": cash,
        "kaspi": kaspi,
        "drinks": drinks,
        "raw": block[:5000],
        "ts": datetime.now().isoformat(timespec="seconds"),
    }

def split_into_reports(text: str):
    """
    Админы могут прислать два отчёта одним сообщением.
    Делим по датам: каждый новый "dd.mm.yyyy" начинает новый блок.
    """
    # Разбиваем так, чтобы дата оставалась в тексте блока
    parts = DATE_RE.split(text)
    if len(parts) < 3:
        r = parse_single_report(text)
        return [r] if r else []

    blocks = []
    # parts: [before, date1, after1, date2, after2, ...]
    before = parts[0]
    for i in range(1, len(parts), 2):
        date = parts[i]
        after = parts[i + 1] if i + 1 < len(parts) else ""
        block = (date + after).strip()
        blocks.append(block)

    parsed = []
    for b in blocks:
        r = parse_single_report(b)
        if r:
            parsed.append(r)
    return parsed


# ================== КОМАНДЫ ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот онлайн. Кидайте отчёты в группу как обычно.")

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id = {update.effective_chat.id}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/whoami — узнать chat_id\n"
        "/week — недельная сводка сейчас\n"
        "/help — помощь\n\n"
        "Отчёты кидайте обычным текстом. Можно 2 отчёта одним сообщением."
    )


# ================== ОБРАБОТКА ОТЧЁТОВ ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    branch = CHAT_TO_BRANCH.get(chat_id)
    if not branch:
        return  # игнорируем другие чаты

    text = update.message.text or ""
    reports = split_into_reports(text)

    if not reports:
        # не спамим ошибками — просто молча игнорируем не-отчёты
        return

    for r in reports:
        r["branch"] = branch
        REPORTS[branch].append(r)

    # Короткий ответ
    lines = [f"✅ Принял отчёты: {len(reports)} шт."]
    for i, r in enumerate(reports, 1):
        lines.append(f"{i}) {branch.upper()} | {r['date']} — {r['shift']} | оборот {r['total']}")
    await update.message.reply_text("\n".join(lines))


# ================== СВОДКИ ==================
def _week_window():
    now = datetime.now()
    start = now - timedelta(days=7)
    return start, now

def build_week_summary(branch: str):
    start, now = _week_window()
    # У нас даты в отчётах как строки — для недельной сводки просто суммируем всё, что накопили
    # (потом улучшим и будем фильтровать по настоящей дате)
    total = sum(r["total"] for r in REPORTS.get(branch, []))
    return total

async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    branch = CHAT_TO_BRANCH.get(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу.")
        return

    t = build_week_summary(branch)
    await update.message.reply_text(f"📊 Недельная сумма для {branch.upper()}: {t}")

async def send_weekly_reports(app: Application):
    # Шлём в оба чата
    for chat_id, branch in CHAT_TO_BRANCH.items():
        total = build_week_summary(branch)
        await app.bot.send_message(chat_id=chat_id, text=f"📊 Недельный отчёт ({branch.upper()}): {total}")

    # очищаем накопленное (недельный отчёт = закрыли неделю)
    REPORTS.clear()


# ================== ЗАПУСК ==================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Нет BOT_TOKEN. Добавь переменную окружения BOT_TOKEN в Railway.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Планировщик: Понедельник 11:00 (Asia/Almaty)
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        lambda: app.create_task(send_weekly_reports(app)),
        CronTrigger(day_of_week="mon", hour=11, minute=0, timezone=TZ),
        id="weekly_report",
        replace_existing=True,
    )
    scheduler.start()

    print("🤖 Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
