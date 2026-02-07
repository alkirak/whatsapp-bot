import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# =========================
# CONFIG
# =========================

TZ = ZoneInfo(os.getenv("TZ", "Asia/Almaty"))  # GMT+5
DB_PATH = os.getenv("DB_PATH", "data.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set env var BOT_TOKEN in Railway.")

# Привязка чатов -> филиал
CHAT_T = int(os.getenv("CHAT_T", "-5174468450"))  # Turkistan group chat_id
CHAT_K = int(os.getenv("CHAT_K", "-5277664922"))  # Kentau group chat_id

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

        cashbox_total INTEGER,             -- "Общая касса" (услуги+напитки)

        services_total INTEGER,            -- услуги (нал+каспи)
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

    # миграция для старых баз (если таблица была создана без cashbox_total)
    try:
        cur.execute("ALTER TABLE reports ADD COLUMN cashbox_total INTEGER;")
    except sqlite3.OperationalError:
        pass

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

    cashbox_total: Optional[int]      # "Общая касса" (услуги + напитки)

    services_total: Optional[int]     # услуги (нал+каспи)
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
    В одном сообщении может быть 1-2+ отчёта.
    Делим по датам (каждая дата — начало нового отчёта).
    """
    text = text.replace("\r\n", "\n")
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
    # "06.02.2026г. Ночь смена Уля" -> Уля
    for line in block.split("\n"):
        if "смен" in line.lower():
            m = re.search(r"смена\s+([A-Za-zА-Яа-яЁё\-]+)", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


def extract_money_section(block: str, title_words: Tuple[str, ...]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Секции:
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
            m = re.search(r":\s*([0-9 ][0-9 ]*)", ln)
            if m:
                total = norm_int(m.group(1))

            for j in range(i + 1, min(i + 7, len(lines))):
                l2 = lines[j].lower()
                if "нал" in l2:
                    m2 = re.search(r":\s*([0-9 ][0-9 ]*)", lines[j])
                    if m2:
                        cash = norm_int(m2.group(1))
                if "касп" in l2 or "kaspi" in l2:
                    m2 = re.search(r":\s*([0-9 ][0-9 ]*)", lines[j])
                    if m2:
                        kaspi = norm_int(m2.group(1))
            break
    return total, cash, kaspi


def sum_inline_amounts(line: str) -> int:
    nums = re.findall(r"([0-9][0-9 ]{0,})", line)
    s = 0
    for n in nums:
        v = norm_int(n)
        if v is not None:
            s += v
    return s


def extract_ops(block: str) -> Tuple[int, int, int]:
    """
    Изъятие / Закуп / Возврат
    Умеет съедать продолжение строк после заголовка:
    Изъятие: Уля-ЗП-7000
    Алишер-60000(нал.)
    """
    withdraw = purchase = refund = 0

    stop_words = ("остаток", "напит", "общ", "оборот", "касса", "нал:", "касп", "kaspi", "возврат", "закуп", "изъят")
    mode = None  # 'withdraw' | 'purchase' | 'refund' | None

    lines = block.split("\n")
    for ln in lines:
        low = ln.lower().strip()
        if not low:
            mode = None
            continue

        if "изъят" in low:
            mode = "withdraw"
            withdraw += sum_inline_amounts(ln)
            continue
        if "закуп" in low:
            mode = "purchase"
            purchase += sum_inline_amounts(ln)
            continue
        if "возврат" in low:
            mode = "refund"
            refund += sum_inline_amounts(ln)
            continue

        # continuation lines for mode (если строка не начинается с другого раздела)
        if mode:
            # если строка явно новая секция — выходим
            if any(w in low for w in ("остаток", "напит", "общ", "оборот", "касса")) or DATE_RE.search(ln):
                mode = None
                continue
            val = sum_inline_amounts(ln)
            if val > 0:
                if mode == "withdraw":
                    withdraw += val
                elif mode == "purchase":
                    purchase += val
                elif mode == "refund":
                    refund += val

    return withdraw, purchase, refund


def parse_report_block(block: str) -> Optional[ParsedReport]:
    d = detect_date(block)
    sh = detect_shift(block)
    if not d or not sh:
        return None

    staff = extract_staff(block)

    # "Общая касса" — это общая (услуги + напитки)
    cashbox_total, services_cash, services_kaspi = extract_money_section(
        block, ("общая касса", "общая", "оборот")
    )

    services_total = None
    if services_cash is not None and services_kaspi is not None:
        services_total = services_cash + services_kaspi

    drinks_total, drinks_cash, drinks_kaspi = extract_money_section(block, ("напитки",))

    withdraw_total, purchase_total, refund_total = extract_ops(block)

    return ParsedReport(
        report_date=d,
        shift=sh,
        staff=staff,

        cashbox_total=cashbox_total,

        services_total=services_total,
        services_cash=services_cash,
        services_kaspi=services_kaspi,

        drinks_total=drinks_total,
        drinks_cash=drinks_cash,
        drinks_kaspi=drinks_kaspi,

        withdraw_total=withdraw_total,
        purchase_total=purchase_total,
        refund_total=refund_total,
        raw_block=block.strip(),
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
    raw_norm = re.sub(r"\s+", " ", pr.raw_block.strip())
    parts = [
        branch,
        pr.report_date.isoformat(),
        pr.shift,
        str(pr.cashbox_total or ""),
        str(pr.services_cash or ""),
        str(pr.services_kaspi or ""),
        str(pr.drinks_total or ""),
        str(pr.drinks_cash or ""),
        str(pr.drinks_kaspi or ""),
        raw_norm[:200],
    ]
    return "|".join(parts)

# =========================
# VALIDATION / MESSAGES
# =========================

def fmt_money(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ")


def check_mismatches(pr: ParsedReport) -> List[str]:
    warns = []

    # 1) Напитки: total == cash+kaspi
    if pr.drinks_total is not None and pr.drinks_cash is not None and pr.drinks_kaspi is not None:
        if pr.drinks_total != pr.drinks_cash + pr.drinks_kaspi:
            warns.append(
                f"⚠️ *Напитки не сходятся:* {pr.drinks_total} ≠ {pr.drinks_cash}+{pr.drinks_kaspi}"
            )

    # 2) Общая касса: cashbox_total == услуги + напитки
    # услуги = services_cash + services_kaspi
    if pr.cashbox_total is not None and pr.services_total is not None and pr.drinks_total is not None:
        if pr.cashbox_total != pr.services_total + pr.drinks_total:
            warns.append(
                f"⚠️ *Общая касса не сходится:* {pr.cashbox_total} ≠ {pr.services_total}+{pr.drinks_total} (услуги+напитки)"
            )

    return warns


def report_summary_line(branch: str, pr: ParsedReport) -> str:
    dt = pr.report_date.strftime("%d.%m.%Y")
    staff = f" ({pr.staff})" if pr.staff else ""
    return (
        f"*{branch_name(branch)}* | {dt} — {shift_name(pr.shift)}{staff}: "
        f"услуги *{fmt_money(pr.services_total)}*, напитки *{fmt_money(pr.drinks_total)}*, общая *{fmt_money(pr.cashbox_total)}*"
    )

# =========================
# STORE / FETCH
# =========================

def save_report(chat_id: int, branch: str, pr: ParsedReport) -> Tuple[bool, Optional[int]]:
    conn = db()
    cur = conn.cursor()
    fp = make_fingerprint(branch, pr)
    now = datetime.now(TZ).isoformat(timespec="seconds")

    try:
        cur.execute("""
        INSERT INTO reports (
            chat_id, branch, report_date, shift, staff,
            cashbox_total,
            services_total, services_cash, services_kaspi,
            drinks_total, drinks_cash, drinks_kaspi,
            withdraw_total, purchase_total, refund_total,
            raw, fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id, branch, pr.report_date.isoformat(), pr.shift, pr.staff,
            pr.cashbox_total,
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
    ORDER BY report_date ASC,
             CASE shift WHEN 'day' THEN 0 ELSE 1 END ASC,
             created_at ASC
    """, (branch, start.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    conn.close()
    return rows


def fetch_day(branch: str, day: date) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM reports
    WHERE branch = ?
      AND report_date = ?
    ORDER BY CASE shift WHEN 'day' THEN 0 ELSE 1 END ASC, created_at ASC
    """, (branch, day.isoformat()))
    rows = cur.fetchall()
    conn.close()
    return rows

# =========================
# DIGEST
# =========================

def compute_last_week_window(now: datetime) -> Tuple[date, date]:
    """Прошлая неделя (Пн-Вс)."""
    today = now.date()
    weekday = today.weekday()  # Monday=0
    this_monday = today - timedelta(days=weekday)
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    return last_monday, last_sunday


def compute_month_window(mm_yyyy: Optional[str]) -> Tuple[date, date]:
    """Окно месяца. Если None — текущий месяц до сегодня."""
    now = datetime.now(TZ)
    if not mm_yyyy:
        start = date(now.year, now.month, 1)
        end = now.date()
        return start, end

    m = re.match(r"^\s*(\d{2})\.(\d{4})\s*$", mm_yyyy)
    if not m:
        raise ValueError("Format must be MM.YYYY (например 02.2026)")
    mm = int(m.group(1))
    yy = int(m.group(2))
    start = date(yy, mm, 1)

    # конец месяца
    if mm == 12:
        end = date(yy + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(yy, mm + 1, 1) - timedelta(days=1)
    return start, end


def digest_text(branch: str, start: date, end: date, title: str) -> str:
    rows = fetch_range(branch, start, end)
    header = (
        f"📊 *{title}* — *{branch_name(branch)}*\n"
        f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*"
    )
    if not rows:
        return header + "\n\n❌ Нет сохранённых отчётов за период."

    shifts_count = len(rows)

    serv_total = sum((r["services_total"] or 0) for r in rows)
    serv_cash = sum((r["services_cash"] or 0) for r in rows)
    serv_kaspi = sum((r["services_kaspi"] or 0) for r in rows)

    drink_total = sum((r["drinks_total"] or 0) for r in rows)
    drink_cash = sum((r["drinks_cash"] or 0) for r in rows)
    drink_kaspi = sum((r["drinks_kaspi"] or 0) for r in rows)

    cashbox_total = sum((r["cashbox_total"] or 0) for r in rows)

    withdraw_total = sum((r["withdraw_total"] or 0) for r in rows)
    purchase_total = sum((r["purchase_total"] or 0) for r in rows)
    refund_total = sum((r["refund_total"] or 0) for r in rows)

    by_day: Dict[str, Dict[str, int]] = {}
    for r in rows:
        dkey = r["report_date"]  # YYYY-MM-DD
        if dkey not in by_day:
            by_day[dkey] = {"serv": 0, "drink": 0, "total": 0}
        st = r["services_total"] or 0
        dt = r["drinks_total"] or 0
        tt = (r["cashbox_total"] or 0) or (st + dt)
        by_day[dkey]["serv"] += st
        by_day[dkey]["drink"] += dt
        by_day[dkey]["total"] += tt

    # лучший день
    best_day, best_val = None, -1
    for dkey, v in by_day.items():
        if v["total"] > best_val:
            best_val = v["total"]
            best_day = dkey

    lines = [header, ""]
    lines.append(f"✅ Принято смен: *{shifts_count}*")
    lines.append("")
    lines.append("💰 *Итоги*")
    lines.append(f"• Услуги: *{fmt_money(serv_total)}*  (нал {fmt_money(serv_cash)} / каспи {fmt_money(serv_kaspi)})")
    lines.append(f"• Напитки: *{fmt_money(drink_total)}* (нал {fmt_money(drink_cash)} / каспи {fmt_money(drink_kaspi)})")
    lines.append(f"• Общая касса (сумма): *{fmt_money(cashbox_total)}*")
    lines.append("")
    lines.append("🧾 *Операции*")
    lines.append(f"• Изъятие: *{fmt_money(withdraw_total)}*")
    lines.append(f"• Закуп: *{fmt_money(purchase_total)}*")
    lines.append(f"• Возврат: *{fmt_money(refund_total)}*")
    lines.append("")

    if cashbox_total > 0:
        share = int(round((drink_total / cashbox_total) * 100))
        lines.append(f"🥤 Доля напитков: *{share}%*")
        lines.append("")

    if best_day:
        bd = datetime.fromisoformat(best_day).strftime("%d.%m.%Y")
        lines.append(f"🏆 Лучший день: *{bd}* — *{fmt_money(best_val)}*")
        lines.append("")

    lines.append("📅 *По дням* (услуги / напитки / общая касса)")
    for dkey in sorted(by_day.keys()):
        dt_obj = datetime.fromisoformat(dkey)
        dlabel = dt_obj.strftime("%d.%m")
        v = by_day[dkey]
        lines.append(f"• *{dlabel}*: {fmt_money(v['serv'])} / {fmt_money(v['drink'])} / *{fmt_money(v['total'])}*")

    return "\n".join(lines)

# =========================
# COMMANDS
# =========================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "Команды:\n"
        "• /help — помощь\n"
        "• /whoami — твой user_id\n"
        "• /week — последние 7 дней (по этому филиалу)\n"
        "• /day 06.02.2026 — сводка за день\n"
        "• /month 02.2026 — сводка за месяц\n"
        "• /digest_week — дайджест прошлой недели\n"
        "• /digest_month 02.2026 — дайджест месяца\n\n"
        "Как слать отчёт:\n"
        "• В одном сообщении можно 1-2 отчёта\n"
        "• Напитки — отдельным блоком (как сейчас)\n"
        "• Изъятие/Закуп/Возврат можно писать строками ниже заголовка — бот поймёт."
    )
    await update.message.reply_text(txt)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(f"Your user_id: {uid}")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    now = datetime.now(TZ)
    end = now.date()
    start = end - timedelta(days=6)
    msg = digest_text(branch, start, end, title="Сводка за 7 дней")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    if not context.args:
        await update.message.reply_text("Пример: /day 06.02.2026")
        return

    m = DATE_RE.search(" ".join(context.args))
    if not m:
        await update.message.reply_text("Неверный формат даты. Пример: /day 06.02.2026")
        return

    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    rows = fetch_day(branch, d)
    if not rows:
        await update.message.reply_text("Нет отчётов за этот день.")
        return

    serv = sum((r["services_total"] or 0) for r in rows)
    drink = sum((r["drinks_total"] or 0) for r in rows)
    total = sum((r["cashbox_total"] or 0) for r in rows)

    lines = [
        f"📌 *Сводка за {d.strftime('%d.%m.%Y')}* — *{branch_name(branch)}*",
        "",
        f"• Услуги: *{fmt_money(serv)}*",
        f"• Напитки: *{fmt_money(drink)}*",
        f"• Общая касса: *{fmt_money(total)}*",
        "",
        "Смены:"
    ]
    for r in rows:
        lines.append(
            f"• {shift_name(r['shift'])}"
            f"{' (' + r['staff'] + ')' if r['staff'] else ''}: "
            f"услуги {fmt_money(r['services_total'])}, напитки {fmt_money(r['drinks_total'])}, общая {fmt_money(r['cashbox_total'])}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    arg = " ".join(context.args).strip() if context.args else None
    try:
        start, end = compute_month_window(arg)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    msg = digest_text(branch, start, end, title="Сводка за месяц")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_digest_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    start, end = compute_last_week_window(datetime.now(TZ))
    msg = digest_text(branch, start, end, title="Недельный отчёт")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_digest_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    arg = " ".join(context.args).strip() if context.args else None
    try:
        start, end = compute_month_window(arg)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    msg = digest_text(branch, start, end, title="Месячный отчёт")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# =========================
# MESSAGE HANDLER
# =========================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        return  # чужие чаты игнор

    text = update.message.text.strip()
    blocks = split_into_blocks(text)
    if not blocks:
        return

    parsed: List[ParsedReport] = []
    for b in blocks:
        pr = parse_report_block(b)
        if pr:
            parsed.append(pr)

    if not parsed:
        return

    accepted_lines: List[str] = []
    warns_all: List[str] = []
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

    msg_parts: List[str] = []
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
    now = datetime.now(TZ)
    start, end = compute_last_week_window(now)

    for branch, chat_id in [("t", CHAT_T), ("k", CHAT_K)]:
        msg = digest_text(branch, start, end, title="Недельный отчёт")
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"[weekly_digest_job] failed to send to {branch} chat {chat_id}: {e}")

# =========================
# MAIN
# =========================

def main() -> None:
    init_db()

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # timezone для scheduler (если доступен)
    try:
        app.job_queue.scheduler.timezone = TZ
    except Exception:
        pass

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("digest_week", cmd_digest_week))
    app.add_handler(CommandHandler("digest_month", cmd_digest_month))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if ENABLE_WEEKLY_DIGEST:
        # Каждый понедельник 11:00 по Asia/Almaty
        app.job_queue.run_daily(
            weekly_digest_job,
            time=dtime(hour=11, minute=0),
            days=(0,),  # Monday
            name="weekly_digest"
        )

    print("🤖 Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
