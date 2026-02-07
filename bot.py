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
CHAT_TURKISTAN = int(os.environ.get("CHAT_TURKISTAN", "-5174468450"))
CHAT_KENTAU    = int(os.environ.get("CHAT_KENTAU", "-5277664922"))

CHAT_TO_BRANCH = {
    CHAT_TURKISTAN: "t",
    CHAT_KENTAU: "k",
}

BRANCH_NAME = {"t": "T", "k": "K"}

TZ = os.environ.get("TZ", "Asia/Almaty")  # важно для времени 11:00

# Хранилище в памяти (на перезапуске очищается).
REPORTS = defaultdict(list)

# Set для дублей: (branch, date, shift, total)
SEEN = set()


# ================== УТИЛИТЫ ==================
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
INT_RE = re.compile(r"(\d[\d\s]*)")


def _to_int(s: str) -> int:
    return int(s.replace(" ", ""))


def _fmt(n: int) -> str:
    # 83450 -> "83 450"
    return f"{n:,}".replace(",", " ")


def _parse_ddmmyyyy(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(date_str, "%d.%m.%Y")
    except Exception:
        return None


def _parse_mm_yyyy(s: str) -> tuple[int, int] | None:
    # "02.2026"
    m = re.match(r"^\s*(\d{2})\.(\d{4})\s*$", s)
    if not m:
        return None
    mm = int(m.group(1))
    yy = int(m.group(2))
    if not (1 <= mm <= 12):
        return None
    return mm, yy


# ================== ПАРСИНГ ОТЧЁТОВ ==================
def parse_single_report(block: str):
    """
    Ищем в одном блоке:
    дата: 06.02.2026
    смена: день/ночь
    общая касса: число (УСЛУГИ)
    Нал: число
    Каспи: число
    Напитки: число (опционально, отдельно)
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
        pattern = re.compile(rf"{label}\s*:\s*{INT_RE.pattern}", re.IGNORECASE)
        m = pattern.search(block)
        return _to_int(m.group(1)) if m else None

    # Общая касса = услуги (как договорились)
    total_services = grab("Общая касса")
    if total_services is None:
        return None

    cash_services = grab("Нал") or 0
    kaspi_services = grab("Каспи") or 0

    # Напитки отдельно
    drinks_total = grab("Напитки") or 0

    return {
        "date": date_str,            # dd.mm.yyyy
        "shift": shift,              # день/ночь
        "services_total": total_services,
        "services_cash": cash_services,
        "services_kaspi": kaspi_services,
        "drinks_total": drinks_total,
        "raw": block[:5000],
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def split_into_reports(text: str):
    """
    Админы могут прислать два отчёта одним сообщением.
    Делим по датам: каждый новый "dd.mm.yyyy" начинает новый блок.
    """
    parts = DATE_RE.split(text)
    if len(parts) < 3:
        r = parse_single_report(text)
        return [r] if r else []

    blocks = []
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
        "/last — последний принятый отчёт\n"
        "/day 06.02.2026 — сводка за день\n"
        "/week — недельная сводка (последние 7 дней по датам отчётов)\n"
        "/month 02.2026 — сводка за месяц\n"
        "/stats — статистика (сколько отчётов, за какие даты)\n"
        "/help — помощь\n\n"
        "Важно: 'Общая касса' = услуги. 'Напитки' — отдельно."
    )


# ================== ПРИЁМ ОТЧЁТОВ ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    branch = CHAT_TO_BRANCH.get(chat_id)
    if not branch:
        return  # игнор других чатов

    text = update.message.text or ""
    reports = split_into_reports(text)
    if not reports:
        return

    accepted = []
    duplicates = 0

    for r in reports:
        key = (branch, r["date"], r["shift"], r["services_total"])
        if key in SEEN:
            duplicates += 1
            continue

        SEEN.add(key)
        r["branch"] = branch
        REPORTS[branch].append(r)
        accepted.append(r)

    if not accepted and duplicates > 0:
        await update.message.reply_text("⚠️ Похоже это дубликат (уже был принят).")
        return

    lines = [f"✅ Принял отчёты: {len(accepted)} шт."]
    if duplicates:
        lines.append(f"♻️ Дубликаты пропущены: {duplicates}")

    for i, r in enumerate(accepted, 1):
        s = f"{i}) {BRANCH_NAME[branch]} | {r['date']} — {r['shift']} | 🎮 услуги {_fmt(r['services_total'])}"
        if r["drinks_total"]:
            s += f" | 🥤 напитки {_fmt(r['drinks_total'])}"
        lines.append(s)

    await update.message.reply_text("\n".join(lines))


# ================== СВОДКИ / АГРЕГАЦИИ ==================
def _get_branch_from_chat(chat_id: int) -> str | None:
    return CHAT_TO_BRANCH.get(chat_id)


def _reports_for_branch(branch: str):
    return REPORTS.get(branch, [])


def _group_by_date_and_shift(reports: list[dict]):
    # {date: {shift: {"services": sum, "drinks": sum}}}
    out = defaultdict(lambda: defaultdict(lambda: {"services": 0, "drinks": 0}))
    for r in reports:
        out[r["date"]][r["shift"]]["services"] += r["services_total"]
        out[r["date"]][r["shift"]]["drinks"] += r["drinks_total"]
    return out


def build_day_summary(branch: str, date_str: str):
    reps = [r for r in _reports_for_branch(branch) if r["date"] == date_str]
    if not reps:
        return None

    by = _group_by_date_and_shift(reps)[date_str]
    s_night = by.get("ночь", {"services": 0, "drinks": 0})
    s_day   = by.get("день", {"services": 0, "drinks": 0})

    total_services = s_night["services"] + s_day["services"]
    total_drinks   = s_night["drinks"] + s_day["drinks"]

    return {
        "date": date_str,
        "night": s_night,
        "day": s_day,
        "services_total": total_services,
        "drinks_total": total_drinks,
    }


def build_week_summary(branch: str):
    """
    Берём последние 7 дней по календарю относительно СЕЙЧАС,
    но фильтруем отчёты по их dd.mm.yyyy.
    """
    now = datetime.now()
    start = now - timedelta(days=7)

    reps = []
    for r in _reports_for_branch(branch):
        d = _parse_ddmmyyyy(r["date"])
        if d and start.date() <= d.date() <= now.date():
            reps.append(r)

    if not reps:
        return None

    by = _group_by_date_and_shift(reps)

    # сортируем даты
    dates_sorted = sorted(by.keys(), key=lambda x: (_parse_ddmmyyyy(x) or datetime.min))
    lines = []

    week_services = 0
    week_drinks = 0

    for ds in dates_sorted:
        dayinfo = build_day_summary(branch, ds)
        if not dayinfo:
            continue

        n = dayinfo["night"]["services"]
        d = dayinfo["day"]["services"]
        nd = dayinfo["night"]["drinks"]
        dd = dayinfo["day"]["drinks"]

        week_services += dayinfo["services_total"]
        week_drinks += dayinfo["drinks_total"]

        lines.append(f"📅 {ds}")
        if n or nd:
            line = f"  🌙 Ночь: 🎮 {_fmt(n)}"
            if nd:
                line += f" | 🥤 {_fmt(nd)}"
            lines.append(line)
        if d or dd:
            line = f"  ☀️ День: 🎮 {_fmt(d)}"
            if dd:
                line += f" | 🥤 {_fmt(dd)}"
            lines.append(line)
        lines.append("")

    return {
        "branch": branch,
        "lines": lines,
        "services_total": week_services,
        "drinks_total": week_drinks,
    }


def build_month_summary(branch: str, mm: int, yy: int):
    reps = []
    for r in _reports_for_branch(branch):
        d = _parse_ddmmyyyy(r["date"])
        if d and d.month == mm and d.year == yy:
            reps.append(r)

    if not reps:
        return None

    by = _group_by_date_and_shift(reps)
    dates_sorted = sorted(by.keys(), key=lambda x: (_parse_ddmmyyyy(x) or datetime.min))

    services = 0
    drinks = 0
    for ds in dates_sorted:
        dayinfo = build_day_summary(branch, ds)
        if not dayinfo:
            continue
        services += dayinfo["services_total"]
        drinks += dayinfo["drinks_total"]

    return {
        "branch": branch,
        "mm": mm,
        "yy": yy,
        "services_total": services,
        "drinks_total": drinks,
        "days": len(dates_sorted),
        "reports": len(reps),
    }


# ================== КОМАНДЫ СВОДОК ==================
async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = _get_branch_from_chat(update.effective_chat.id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу.")
        return

    reps = _reports_for_branch(branch)
    if not reps:
        await update.message.reply_text("Пока нет принятых отчётов.")
        return

    r = reps[-1]
    msg = (
        f"🧾 Последний отчёт ({BRANCH_NAME[branch]})\n"
        f"{r['date']} — {r['shift']}\n"
        f"🎮 Услуги: {_fmt(r['services_total'])}\n"
    )
    if r["drinks_total"]:
        msg += f"🥤 Напитки: {_fmt(r['drinks_total'])}\n"
    await update.message.reply_text(msg)


async def day_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = _get_branch_from_chat(update.effective_chat.id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу.")
        return

    if not context.args:
        await update.message.reply_text("Формат: /day 06.02.2026")
        return

    date_str = context.args[0].strip()
    if not _parse_ddmmyyyy(date_str):
        await update.message.reply_text("Дата должна быть в формате: 06.02.2026")
        return

    s = build_day_summary(branch, date_str)
    if not s:
        await update.message.reply_text("Нет отчётов за эту дату.")
        return

    lines = [f"📊 Сводка за {s['date']} ({BRANCH_NAME[branch]})", ""]
    if s["night"]["services"] or s["night"]["drinks"]:
        line = f"🌙 Ночь: 🎮 {_fmt(s['night']['services'])}"
        if s["night"]["drinks"]:
            line += f" | 🥤 {_fmt(s['night']['drinks'])}"
        lines.append(line)
    if s["day"]["services"] or s["day"]["drinks"]:
        line = f"☀️ День: 🎮 {_fmt(s['day']['services'])}"
        if s["day"]["drinks"]:
            line += f" | 🥤 {_fmt(s['day']['drinks'])}"
        lines.append(line)

    lines.append("")
    lines.append(f"✅ Итого услуги: {_fmt(s['services_total'])}")
    if s["drinks_total"]:
        lines.append(f"✅ Итого напитки: {_fmt(s['drinks_total'])}")

    await update.message.reply_text("\n".join(lines))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = _get_branch_from_chat(update.effective_chat.id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу.")
        return

    s = build_week_summary(branch)
    if not s:
        await update.message.reply_text("За последние 7 дней нет отчётов.")
        return

    header = f"📅 Недельный отчёт ({BRANCH_NAME[branch]})\n"
    footer = (
        f"💰 Итого неделя:\n"
        f"🎮 Услуги: {_fmt(s['services_total'])}\n"
    )
    if s["drinks_total"]:
        footer += f"🥤 Напитки: {_fmt(s['drinks_total'])}\n"

    text = header + "\n".join(s["lines"]) + footer
    await update.message.reply_text(text)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = _get_branch_from_chat(update.effective_chat.id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу.")
        return

    if not context.args:
        await update.message.reply_text("Формат: /month 02.2026")
        return

    parsed = _parse_mm_yyyy(context.args[0])
    if not parsed:
        await update.message.reply_text("Формат: /month 02.2026")
        return

    mm, yy = parsed
    s = build_month_summary(branch, mm, yy)
    if not s:
        await update.message.reply_text("Нет отчётов за этот месяц.")
        return

    lines = [
        f"📆 Месячная сводка ({BRANCH_NAME[branch]}) — {mm:02d}.{yy}",
        f"Дней с отчётами: {s['days']}",
        f"Отчётов: {s['reports']}",
        "",
        f"🎮 Услуги: {_fmt(s['services_total'])}",
    ]
    if s["drinks_total"]:
        lines.append(f"🥤 Напитки: {_fmt(s['drinks_total'])}")

    await update.message.reply_text("\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = _get_branch_from_chat(update.effective_chat.id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу.")
        return

    reps = _reports_for_branch(branch)
    if not reps:
        await update.message.reply_text("Пока нет отчётов.")
        return

    dates = sorted({r["date"] for r in reps}, key=lambda x: (_parse_ddmmyyyy(x) or datetime.min))
    await update.message.reply_text(
        f"📌 Статистика ({BRANCH_NAME[branch]})\n"
        f"Отчётов: {len(reps)}\n"
        f"Дат: {len(dates)}\n"
        f"Диапазон: {dates[0]} — {dates[-1]}"
    )


# ================== ПЛАНОВЫЙ НЕДЕЛЬНЫЙ ОТЧЁТ ==================
async def send_weekly_reports(app: Application):
    # Для каждого филиала — в свой чат
    for chat_id, branch in CHAT_TO_BRANCH.items():
        s = build_week_summary(branch)
        if not s:
            await app.bot.send_message(chat_id=chat_id, text="📅 Недельный отчёт: за последние 7 дней нет данных.")
            continue

        header = f"📅 Недельный отчёт ({BRANCH_NAME[branch]})\n"
        footer = (
            f"💰 Итого неделя:\n"
            f"🎮 Услуги: {_fmt(s['services_total'])}\n"
        )
        if s["drinks_total"]:
            footer += f"🥤 Напитки: {_fmt(s['drinks_total'])}\n"

        text = header + "\n".join(s["lines"]) + footer
        await app.bot.send_message(chat_id=chat_id, text=text)


# ================== ЗАПУСК ==================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Нет BOT_TOKEN. Добавь переменную окружения BOT_TOKEN в Railway.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("last", last_cmd))
    app.add_handler(CommandHandler("day", day_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

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
