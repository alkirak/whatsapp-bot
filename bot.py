import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# =========================
# CONFIG
# =========================

TZ = ZoneInfo(os.getenv("TZ", "Asia/Almaty"))  # GMT+5 (Казахстан обычно Asia/Almaty)
DB_PATH = os.getenv("DB_PATH", "data.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set env var BOT_TOKEN in Railway.")

# Группы (chat_id) — можно держать в env, но по умолчанию ставлю твои
CHAT_T = int(os.getenv("CHAT_T", "-5174468450"))  # Turkistan
CHAT_K = int(os.getenv("CHAT_K", "-5277664922"))  # Kentau

# Включить/выключить авто-дайджест (на всякий)
ENABLE_WEEKLY_DIGEST = os.getenv("ENABLE_WEEKLY_DIGEST", "1") == "1"

# =========================
# DB
# =========================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        branch TEXT NOT NULL,              -- 't' or 'k'
        report_date TEXT NOT NULL,         -- YYYY-MM-DD
        shift TEXT NOT NULL,               -- 'day' or 'night'
        staff TEXT,
        services_total INTEGER,
        services_cash INTEGER,
        services_kaspi INTEGER,
        drinks_total INTEGER,
        drinks_cash INTEGER,
        drinks_kaspi INTEGER,
        withdraw_total INTEGER DEFAULT 0,
        purchase_total INTEGER DEFAULT 0,
        refund_total INTEGER DEFAULT 0,
        raw TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_fingerprint ON reports(fingerprint);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_branch_date ON reports(branch, report_date);")
    conn.commit()
    conn.close()

# =========================
# PARSING
# =========================

@dataclass
class ParsedReport:
    report_date: date
    shift: str                 # 'day' | 'night'
    staff: Optional[str]
    services_total: Optional[int]
    services_cash: Optional[int]
    services_kaspi: Optional[int]
    drinks_total: Optional[int]
    drinks_cash: Optional[int]
    drinks_kaspi: Optional[int]
    withdraw_total: int
    purchase_total: int
    refund_total: int
    raw_block: str

def norm_int(s: str) -> Optional[int]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    s = s.replace(" ", "")
    s = re.sub(r"[^\d\-]", "", s)
    if s == "" or s == "-":
        return None
    try:
        return int(s)
    except Exception:
        return None

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
SHIFT_RE = re.compile(r"\b(ночь|ночная|ноч)\b|\b(день|дневная|днев)\b", re.IGNORECASE)

def detect_shift(text: str) -> Optional[str]:
    m = SHIFT_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return "night"
    if m.group(2):
        return "day"
    return None

def detect_date(text: str) -> Optional[date]:
    m = DATE_RE.search(text)
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return date(yyyy, mm, dd)

def split_into_blocks(text: str) -> List[str]:
    """
    В одном сообщении может быть 1-2 отчёта.
    Делим по вхождениям даты (каждая дата обычно начало отчёта).
    """
    text = text.replace("\r\n", "\n")
    # Найдем позиции всех дат
    positions = [m.start() for m in DATE_RE.finditer(text)]
    if not positions:
        return []
    positions.append(len(text))
    blocks = []
    for i in range(len(positions) - 1):
        start = positions[i]
        end = positions[i + 1]
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks

def extract_staff(block: str) -> Optional[str]:
    # Пример: "06.02.2026г. Ночь смена Уля" или "День смена Ару"
    # Берем последнее слово в строке с "смена"
    for line in block.split("\n"):
        if "смен" in line.lower():
            # вытащим после "смена"
            m = re.search(r"смена\s+([A-Za-zА-Яа-яЁё\-]+)", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None

def extract_money_section(block: str, title_words: Tuple[str, ...]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Для секций типа:
      Общая касса: 83450
      Нал: 14030
      Каспи: 63970
    или
      Напитки: 5450
      Нал: 1600
      Каспи: 3850
    """
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    total = cash = kaspi = None
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(w in low for w in title_words):
            # total in same line "xxx: 123"
            m = re.search(r":\s*([0-9 ][0-9 ]*)", ln)
            if m:
                total = norm_int(m.group(1))
            # scan next ~3 lines for "нал" and "каспи"
            for j in range(i + 1, min(i + 6, len(lines))):
                l2 = lines[j].lower()
                if "нал" in l2:
                    m2 = re.search(r":\s*([0-9 ][0-9 ]*)", lines[j])
                    if m2:
                        cash = norm_int(m2.group(1))
                if "касп" in l2:  # каспи / kaspi
                    m2 = re.search(r":\s*([0-9 ][0-9 ]*)", lines[j])
                    if m2:
                        kaspi = norm_int(m2.group(1))
            break
    return total, cash, kaspi

def sum_inline_amounts(line: str) -> int:
    # Забираем все числа (с пробелами) и суммируем
    nums = re.findall(r"([0-9][0-9 ]{0,})", line)
    s = 0
    for n in nums:
        v = norm_int(n)
        if v is not None:
            s += v
    return s

def extract_ops(block: str) -> Tuple[int, int, int]:
    """
    Изъятие, Закуп, Возврат - просто суммы чисел в строках.
    """
    withdraw = purchase = refund = 0
    for ln in block.split("\n"):
        low = ln.lower()
        if "изъят" in low:
            withdraw += sum_inline_amounts(ln)
        if "закуп" in low:
            purchase += sum_inline_amounts(ln)
        if "возврат" in low:
            refund += sum_inline_amounts(ln)
    return withdraw, purchase, refund

def parse_report_block(block: str) -> Optional[ParsedReport]:
    d = detect_date(block)
    sh = detect_shift(block)
    if not d or not sh:
        return None

    staff = extract_staff(block)

    services_total, services_cash, services_kaspi = extract_money_section(
        block, ("общая касса", "общая", "оборот")
    )
    drinks_total, drinks_cash, drinks_kaspi = extract_money_section(
        block, ("напитки",)
    )

    withdraw_total, purchase_total, refund_total = extract_ops(block)

    return ParsedReport(
        report_date=d,
        shift=sh,
        staff=staff,
        services_total=services_total,
        services_cash=services_cash,
        services_kaspi=services_kaspi,
        drinks_total=drinks_total,
        drinks_cash=drinks_cash,
        drinks_kaspi=drinks_kaspi,
        withdraw_total=withdraw_total,
        purchase_total=purchase_total,
        refund_total=refund_total,
        raw_block=block.strip()
    )

def branch_from_chat(chat_id: int) -> Optional[str]:
    if chat_id == CHAT_T:
        return "t"
    if chat_id == CHAT_K:
        return "k"
    return None

def branch_name(b: str) -> str:
    return "Turkistan" if b == "t" else "Kentau"

def shift_name(shift: str) -> str:
    return "День" if shift == "day" else "Ночь"

def make_fingerprint(branch: str, pr: ParsedReport) -> str:
    # Защита от дублей: ключ = филиал + дата + смена + основные цифры
    # raw тоже добавляем, но нормализуем пробелы
    raw_norm = re.sub(r"\s+", " ", pr.raw_block.strip())
    parts = [
        branch,
        pr.report_date.isoformat(),
        pr.shift,
        str(pr.services_total or ""),
        str(pr.services_cash or ""),
        str(pr.services_kaspi or ""),
        str(pr.drinks_total or ""),
        str(pr.drinks_cash or ""),
        str(pr.drinks_kaspi or ""),
        raw_norm[:200],  # достаточно для уникальности
    ]
    return "|".join(parts)

# =========================
# VALIDATION / MESSAGES
# =========================

def check_mismatches(pr: ParsedReport) -> List[str]:
    warns = []

    # 1) Услуги: общая касса должна сходиться с нал+каспи (если все поля есть)
    if pr.services_total is not None and pr.services_cash is not None and pr.services_kaspi is not None:
        if pr.services_total != pr.services_cash + pr.services_kaspi:
            warns.append(
                f"⚠️ *Услуги не сходятся:* {pr.services_total} ≠ {pr.services_cash}+{pr.services_kaspi}"
            )

    # 2) Напитки: если указали напитки и их нал/каспи, проверим
    if pr.drinks_total is not None:
        if pr.drinks_cash is not None and pr.drinks_kaspi is not None:
            if pr.drinks_total != pr.drinks_cash + pr.drinks_kaspi:
                warns.append(
                    f"⚠️ *Напитки не сходятся:* {pr.drinks_total} ≠ {pr.drinks_cash}+{pr.drinks_kaspi}"
                )

    return warns

def fmt_money(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ")

def report_summary_line(branch: str, pr: ParsedReport) -> str:
    dt = pr.report_date.strftime("%d.%m.%Y")
    staff = f" ({pr.staff})" if pr.staff else ""
    s_total = fmt_money(pr.services_total)
    d_total = fmt_money(pr.drinks_total)
    return f"*{branch_name(branch)}* | {dt} — {shift_name(pr.shift)}{staff}: услуги {s_total}, напитки {d_total}"

# =========================
# STORE
# =========================

def save_report(chat_id: int, branch: str, pr: ParsedReport) -> Tuple[bool, Optional[int]]:
    """
    returns: (saved?, report_id)
    """
    conn = db()
    cur = conn.cursor()
    fp = make_fingerprint(branch, pr)
    now = datetime.now(TZ).isoformat(timespec="seconds")

    try:
        cur.execute("""
        INSERT INTO reports (
            chat_id, branch, report_date, shift, staff,
            services_total, services_cash, services_kaspi,
            drinks_total, drinks_cash, drinks_kaspi,
            withdraw_total, purchase_total, refund_total,
            raw, fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id, branch, pr.report_date.isoformat(), pr.shift, pr.staff,
            pr.services_total, pr.services_cash, pr.services_kaspi,
            pr.drinks_total, pr.drinks_cash, pr.drinks_kaspi,
            pr.withdraw_total, pr.purchase_total, pr.refund_total,
            pr.raw_block, fp, now
        ))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return True, rid
    except sqlite3.IntegrityError:
        conn.close()
        return False, None

def fetch_range(branch: str, start: date, end: date) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM reports
    WHERE branch = ?
      AND report_date >= ?
      AND report_date <= ?
    ORDER BY report_date ASC, shift ASC, created_at ASC
    """, (branch, start.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    conn.close()
    return rows

# =========================
# DIGEST
# =========================

def compute_week_window(now: datetime) -> Tuple[date, date]:
    """
    Дайджест по прошлой неделе (Пн-Вс).
    В понедельник 11:00 считаем прошлый Пн..Вс.
    """
    today = now.date()
    # Monday=0..Sunday=6
    weekday = today.weekday()
    # текущий понедельник
    this_monday = today - timedelta(days=weekday)
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    return last_monday, last_sunday

def digest_text(branch: str, start: date, end: date) -> str:
    rows = fetch_range(branch, start, end)
    if not rows:
        return (
            f"📊 *Недельный отчёт* — *{branch_name(branch)}*\n"
            f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*\n\n"
            f"❌ Нет сохранённых отчётов за период."
        )

    # totals
    serv_total = serv_cash = serv_kaspi = 0
    drink_total = drink_cash = drink_kaspi = 0
    shifts_count = 0

    # by day
    by_day: Dict[str, Dict[str, int]] = {}
    # shift keys: day/night

    for r in rows:
        shifts_count += 1
        st = r["services_total"] or 0
        sc = r["services_cash"] or 0
        sk = r["services_kaspi"] or 0
        dt = r["drinks_total"] or 0
        dc = r["drinks_cash"] or 0
        dk = r["drinks_kaspi"] or 0

        serv_total += st
        serv_cash += sc
        serv_kaspi += sk
        drink_total += dt
        drink_cash += dc
        drink_kaspi += dk

        dkey = r["report_date"]  # YYYY-MM-DD
        if dkey not in by_day:
            by_day[dkey] = {"day": 0, "night": 0, "serv": 0, "drink": 0}
        by_day[dkey][r["shift"]] += (st + dt)
        by_day[dkey]["serv"] += st
        by_day[dkey]["drink"] += dt

    grand_total = serv_total + drink_total

    # best day
    best_day = None
    best_val = -1
    for dkey, vals in by_day.items():
        v = vals["serv"] + vals["drink"]
        if v > best_val:
            best_val = v
            best_day = dkey

    lines = []
    lines.append(f"📊 *Недельный отчёт* — *{branch_name(branch)}*")
    lines.append(f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*")
    lines.append("")
    lines.append(f"✅ Принято смен: *{shifts_count}*")
    lines.append("")
    lines.append("💰 *Итого за неделю*")
    lines.append(f"• Услуги: *{fmt_money(serv_total)}*  (нал {fmt_money(serv_cash)} / каспи {fmt_money(serv_kaspi)})")
    lines.append(f"• Напитки: *{fmt_money(drink_total)}* (нал {fmt_money(drink_cash)} / каспи {fmt_money(drink_kaspi)})")
    lines.append(f"• Общий итог: *{fmt_money(grand_total)}*")
    lines.append("")

    # share drinks
    if grand_total > 0:
        share = int(round((drink_total / grand_total) * 100))
        lines.append(f"🥤 Доля напитков: *{share}%*")
        lines.append("")

    if best_day:
        bd = datetime.fromisoformat(best_day).strftime("%d.%m.%Y")
        lines.append(f"🏆 Лучший день: *{bd}* — *{fmt_money(best_val)}*")
        lines.append("")

    # daily table-ish
    lines.append("📅 *По дням* (услуги / напитки / итог)")
    for dkey in sorted(by_day.keys()):
        dt_obj = datetime.fromisoformat(dkey)
        dlabel = dt_obj.strftime("%d.%m")
        v = by_day[dkey]
        lines.append(f"• *{dlabel}*: {fmt_money(v['serv'])} / {fmt_money(v['drink'])} / *{fmt_money(v['serv'] + v['drink'])}*")

    return "\n".join(lines)

# =========================
# HANDLERS
# =========================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "Команды:\n"
        "• /help — помощь\n"
        "• /whoami — твой user_id\n"
        "• /week — показать текущую неделю (последние 7 дней)\n"
        "• /digest_week — вручную отправить недельный дайджест (прошлая неделя)\n\n"
        "Отчёт можно слать как обычно. Напитки — отдельно блоком."
    )
    await update.message.reply_text(txt)

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(f"Your user_id: {uid}")

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # последние 7 дней по этому чату/филиалу
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    now = datetime.now(TZ)
    end = now.date()
    start = end - timedelta(days=6)

    rows = fetch_range(branch, start, end)
    if not rows:
        await update.message.reply_text("Нет сохранённых отчётов за последние 7 дней.")
        return

    # кратко
    serv = sum((r["services_total"] or 0) for r in rows)
    drink = sum((r["drinks_total"] or 0) for r in rows)
    total = serv + drink

    msg = (
        f"📈 *Последние 7 дней* — *{branch_name(branch)}*\n"
        f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*\n\n"
        f"• Услуги: *{fmt_money(serv)}*\n"
        f"• Напитки: *{fmt_money(drink)}*\n"
        f"• Итог: *{fmt_money(total)}*"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_digest_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    start, end = compute_week_window(datetime.now(TZ))
    msg = digest_text(branch, start, end)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        # чужие чаты игнорируем
        return

    text = update.message.text.strip()
    blocks = split_into_blocks(text)
    if not blocks:
        return  # не похоже на отчёт

    parsed: List[ParsedReport] = []
    for b in blocks:
        pr = parse_report_block(b)
        if pr:
            parsed.append(pr)

    if not parsed:
        return

    accepted_lines = []
    warns_all = []
    dupes = 0

    for pr in parsed:
        saved, _rid = save_report(chat_id, branch, pr)
        if not saved:
            dupes += 1
            continue

        accepted_lines.append("✅ " + report_summary_line(branch, pr))
        warns_all.extend(check_mismatches(pr))

    if not accepted_lines and dupes > 0:
        await update.message.reply_text("♻️ Этот отчёт уже был принят (дубль).")
        return

    msg_parts = []
    if accepted_lines:
        msg_parts.append(f"✅ Принял отчёты: *{len(accepted_lines)} шт.*")
        msg_parts.append("\n".join(accepted_lines))

    if dupes:
        msg_parts.append(f"♻️ Дублей пропущено: *{dupes}*")

    if warns_all:
        msg_parts.append("")
        msg_parts.append("\n".join(warns_all))

    await update.message.reply_text("\n\n".join(msg_parts), parse_mode=ParseMode.MARKDOWN)

# =========================
# WEEKLY JOB
# =========================

async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Отправляем дайджест в оба филиала
    now = datetime.now(TZ)
    start, end = compute_week_window(now)

    for branch, chat_id in [("t", CHAT_T), ("k", CHAT_K)]:
        msg = digest_text(branch, start, end)
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            # лог в stdout
            print(f"[weekly_digest_job] failed to send to {branch} chat {chat_id}: {e}")

# =========================
# MAIN
# =========================

def main() -> None:
    init_db()

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .timezone(TZ)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("digest_week", cmd_digest_week))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if ENABLE_WEEKLY_DIGEST:
        # Каждый понедельник 11:00 по TZ
        app.job_queue.run_daily(
            weekly_digest_job,
            time=datetime.strptime("11:00", "%H:%M").time(),
            days=(0,),  # Monday
            name="weekly_digest"
        )

    print("🤖 Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
