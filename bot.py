# bot.py (PostgreSQL)
import os
import re
import hashlib
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

import psycopg2
import psycopg2.extras

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

TZ = ZoneInfo(os.getenv("TZ", "Asia/Almaty"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set env var BOT_TOKEN in Railway.")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is empty. Add PostgreSQL in Railway and ensure DATABASE_URL exists.")

# Привязка чатов к филиалам:
# t = Turkistan, k = Kentau
CHAT_T = int(os.getenv("CHAT_T", "-5174468450"))
CHAT_K = int(os.getenv("CHAT_K", "-5277664922"))

ENABLE_WEEKLY_DIGEST = os.getenv("ENABLE_WEEKLY_DIGEST", "1") == "1"
WEEKLY_DIGEST_TIME = os.getenv("WEEKLY_DIGEST_TIME", "11:00")  # понедельник 11:00


# =========================
# DB (PostgreSQL)
# =========================

def db() -> psycopg2.extensions.connection:
    """
    Railway Postgres обычно требует SSL. Если в DATABASE_URL нет sslmode,
    добавляем sslmode=require.
    """
    dsn = DATABASE_URL
    if "sslmode=" not in dsn:
        joiner = "&" if "?" in dsn else "?"
        dsn = dsn + f"{joiner}sslmode=require"

    conn = psycopg2.connect(dsn)
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    # Основная таблица отчётов
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        branch TEXT NOT NULL,              -- 't' or 'k'
        report_date DATE NOT NULL,
        shift TEXT NOT NULL,               -- 'day' or 'night'
        staff TEXT,

        cashbox_total INTEGER,
        services_total INTEGER,
        services_cash INTEGER,
        services_kaspi INTEGER,

        drinks_total INTEGER,
        drinks_cash INTEGER,
        drinks_kaspi INTEGER,

        withdraw_total INTEGER DEFAULT 0,
        purchase_total INTEGER DEFAULT 0,
        refund_total INTEGER DEFAULT 0,

        salary_total INTEGER DEFAULT 0,

        raw TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    );
    """)

    # Индексы
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_fingerprint ON reports(fingerprint);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_branch_date ON reports(branch, report_date);")

    # Таблица выплат зарплаты по людям (для корректного “кто сколько получил”)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS salary_payments (
        id SERIAL PRIMARY KEY,
        report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_salary_payments_report_id ON salary_payments(report_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_salary_payments_name ON salary_payments(name);")

    conn.commit()
    cur.close()
    conn.close()


# =========================
# PARSING
# =========================

@dataclass
class ParsedReport:
    report_date: date
    shift: str                 # 'day' | 'night'
    staff: Optional[str]

    cashbox_total: Optional[int]

    services_total: Optional[int]
    services_cash: Optional[int]
    services_kaspi: Optional[int]

    drinks_total: Optional[int]
    drinks_cash: Optional[int]
    drinks_kaspi: Optional[int]

    withdraw_total: int
    purchase_total: int
    refund_total: int

    salary_total: int
    salary_by_staff: Dict[str, int]

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


# ✅ принимает 6.02.2026 и 06.02.2026
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
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
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def split_into_blocks(text: str) -> List[str]:
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
    for line in block.split("\n"):
        if "смен" in line.lower():
            m = re.search(r"смена\s+([A-Za-zА-Яа-яЁё\-]+)", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


def extract_section_with_stop(
    block: str,
    title_predicate,
    stop_markers: Tuple[str, ...],
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    total = cash = kaspi = None

    for i, ln in enumerate(lines):
        low = ln.lower()
        if title_predicate(low):
            m = re.search(r":\s*([0-9 ][0-9 ]*)", ln)
            if m:
                total = norm_int(m.group(1))

            for j in range(i + 1, min(i + 12, len(lines))):
                l2 = lines[j].lower()
                if any(sm in l2 for sm in stop_markers):
                    break

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


def extract_cashbox(block: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    stop = ("услуг", "напит", "изъят", "остаток", "возврат", "закуп", "преми", "зп", "зарплат", "аванс")
    return extract_section_with_stop(
        block,
        lambda low: ("общая касса" in low) or (low.startswith("общая")) or ("оборот" in low),
        stop
    )


def extract_services(block: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    stop = ("напит", "изъят", "остаток", "возврат", "закуп", "преми", "общая касса", "оборот", "зп", "зарплат", "аванс")
    return extract_section_with_stop(
        block,
        lambda low: "услуг" in low,
        stop
    )


def extract_drinks(block: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    stop = ("услуг", "изъят", "остаток", "возврат", "закуп", "преми", "общая касса", "оборот", "зп", "зарплат", "аванс")
    return extract_section_with_stop(
        block,
        lambda low: "напит" in low,
        stop
    )


def sum_inline_amounts(line: str) -> int:
    nums = re.findall(r"([0-9][0-9 ]{0,})", line)
    s = 0
    for n in nums:
        v = norm_int(n)
        if v is not None:
            s += v
    return s


SALARY_KEY_RE = re.compile(r"\b(зп|з/п|зарплат|аванс|преми)\b", re.IGNORECASE)

def parse_salary_lines(block: str, default_staff: Optional[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for ln in block.split("\n"):
        low = ln.lower()
        if not SALARY_KEY_RE.search(low):
            continue

        amount = sum_inline_amounts(ln)
        if amount <= 0:
            continue

        m = re.search(r"(зп|з/п|зарплат[аы]?|аванс|преми[яи]?)\s*[:\-]?\s*([A-Za-zА-Яа-яЁё\-]+)?", ln, re.IGNORECASE)
        name = None
        if m:
            cand = m.group(2)
            if cand and not re.search(r"\d", cand):
                name = cand.strip()

        if not name:
            name = (default_staff or "").strip() or "Без имени"

        out[name] = out.get(name, 0) + amount

    return out


def extract_ops(block: str, default_staff: Optional[str]) -> Tuple[int, int, int, int, Dict[str, int]]:
    withdraw = purchase = refund = 0

    for ln in block.split("\n"):
        low = ln.lower()
        if "изъят" in low:
            withdraw += sum_inline_amounts(ln)
        if "закуп" in low:
            purchase += sum_inline_amounts(ln)
        if "возврат" in low:
            refund += sum_inline_amounts(ln)

    salary_by = parse_salary_lines(block, default_staff)
    salary_total = sum(salary_by.values())

    return withdraw, purchase, refund, salary_total, salary_by


def parse_report_block(block: str) -> Optional[ParsedReport]:
    d = detect_date(block)
    sh = detect_shift(block)
    if not d or not sh:
        return None

    staff = extract_staff(block)

    cashbox_total, cashbox_cash, cashbox_kaspi = extract_cashbox(block)
    services_total, services_cash, services_kaspi = extract_services(block)
    drinks_total, drinks_cash, drinks_kaspi = extract_drinks(block)

    if services_total is None and services_cash is None and services_kaspi is None:
        services_cash = cashbox_cash
        services_kaspi = cashbox_kaspi
        if services_cash is not None and services_kaspi is not None:
            services_total = services_cash + services_kaspi

    withdraw_total, purchase_total, refund_total, salary_total, salary_by_staff = extract_ops(block, staff)

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

        salary_total=salary_total,
        salary_by_staff=salary_by_staff,

        raw_block=block.strip(),
    )


# =========================
# BRANCH helpers
# =========================

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


def fmt_money(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ")


# =========================
# DEDUPE fingerprint
# =========================

def make_fingerprint(branch: str, pr: ParsedReport) -> str:
    raw_norm = re.sub(r"\s+", " ", pr.raw_block.strip())
    base = "|".join([
        branch,
        pr.report_date.isoformat(),
        pr.shift,
        (pr.staff or "").lower(),
        str(pr.cashbox_total or ""),
        str(pr.services_total or ""),
        str(pr.services_cash or ""),
        str(pr.services_kaspi or ""),
        str(pr.drinks_total or ""),
        str(pr.drinks_cash or ""),
        str(pr.drinks_kaspi or ""),
        str(pr.withdraw_total or 0),
        str(pr.purchase_total or 0),
        str(pr.refund_total or 0),
        str(pr.salary_total or 0),
        raw_norm[:600],
    ])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


# =========================
# VALIDATION / MESSAGES
# =========================

def check_mismatches(pr: ParsedReport) -> List[str]:
    warns = []

    if pr.services_total is not None and pr.services_cash is not None and pr.services_kaspi is not None:
        if pr.services_total != pr.services_cash + pr.services_kaspi:
            warns.append(f"⚠️ *Услуги не сходятся:* {pr.services_total} ≠ {pr.services_cash}+{pr.services_kaspi}")

    if pr.drinks_total is not None and pr.drinks_cash is not None and pr.drinks_kaspi is not None:
        if pr.drinks_total != pr.drinks_cash + pr.drinks_kaspi:
            warns.append(f"⚠️ *Напитки не сходятся:* {pr.drinks_total} ≠ {pr.drinks_cash}+{pr.drinks_kaspi}")

    if pr.cashbox_total is not None and pr.services_total is not None and pr.drinks_total is not None:
        if pr.cashbox_total != pr.services_total + pr.drinks_total:
            warns.append(f"⚠️ *Общая касса не сходится:* {pr.cashbox_total} ≠ {pr.services_total}+{pr.drinks_total} (услуги+напитки)")

    return warns


def report_summary_line(branch: str, pr: ParsedReport) -> str:
    dt = pr.report_date.strftime("%d.%m.%Y")
    staff = f" ({pr.staff})" if pr.staff else ""
    salary = f", зп *{fmt_money(pr.salary_total)}*" if pr.salary_total else ""
    return (
        f"*{branch_name(branch)}* | {dt} — {shift_name(pr.shift)}{staff}: "
        f"услуги *{fmt_money(pr.services_total)}*, напитки *{fmt_money(pr.drinks_total)}*, "
        f"общая *{fmt_money(pr.cashbox_total)}*{salary}"
    )


# =========================
# STORE / QUERY (Postgres)
# =========================

def save_salary_payments(conn, report_id: int, salary_by_staff: Dict[str, int]) -> None:
    if not salary_by_staff:
        return
    cur = conn.cursor()
    now = datetime.now(TZ)
    for name, amount in salary_by_staff.items():
        if not name or amount <= 0:
            continue
        cur.execute(
            "INSERT INTO salary_payments (report_id, name, amount, created_at) VALUES (%s, %s, %s, %s)",
            (report_id, name, amount, now)
        )
    cur.close()


def save_report(chat_id: int, branch: str, pr: ParsedReport) -> Tuple[bool, Optional[int]]:
    conn = db()
    cur = conn.cursor()
    fp = make_fingerprint(branch, pr)
    now = datetime.now(TZ)

    try:
        cur.execute("""
        INSERT INTO reports (
            chat_id, branch, report_date, shift, staff,
            cashbox_total,
            services_total, services_cash, services_kaspi,
            drinks_total, drinks_cash, drinks_kaspi,
            withdraw_total, purchase_total, refund_total,
            salary_total,
            raw, fingerprint, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """, (
            chat_id, branch, pr.report_date, pr.shift, pr.staff,
            pr.cashbox_total,
            pr.services_total, pr.services_cash, pr.services_kaspi,
            pr.drinks_total, pr.drinks_cash, pr.drinks_kaspi,
            pr.withdraw_total, pr.purchase_total, pr.refund_total,
            pr.salary_total,
            pr.raw_block, fp, now
        ))
        rid = cur.fetchone()[0]

        save_salary_payments(conn, rid, pr.salary_by_staff)

        conn.commit()
        cur.close()
        conn.close()
        return True, rid

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return False, None
    except Exception:
        conn.rollback()
        cur.close()
        conn.close()
        raise


def fetch_range(branch: str, start: date, end: date) -> List[Dict]:
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT *
    FROM reports
    WHERE branch = %s
      AND report_date >= %s
      AND report_date <= %s
    ORDER BY report_date ASC, shift ASC, created_at ASC
    """, (branch, start, end))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def fetch_day(branch: str, day: date) -> List[Dict]:
    return fetch_range(branch, day, day)


def fetch_salary_breakdown_for_reports(report_ids: List[int]) -> Dict[str, int]:
    if not report_ids:
        return {}
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT name, SUM(amount) AS total
        FROM salary_payments
        WHERE report_id = ANY(%s)
        GROUP BY name
    """, (report_ids,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r["name"]: int(r["total"] or 0) for r in rows}


def period_ops_and_salary(rows: List[Dict]) -> Tuple[int, int, int, int, Dict[str, int]]:
    withdraw = purchase = refund = 0
    report_ids: List[int] = []

    for r in rows:
        withdraw += (r.get("withdraw_total") or 0)
        purchase += (r.get("purchase_total") or 0)
        refund += (r.get("refund_total") or 0)
        report_ids.append(int(r["id"]))

    by_name = fetch_salary_breakdown_for_reports(report_ids)
    salary_total = sum(by_name.values())

    # fallback на случай если по старым данным не было salary_payments
    if not by_name:
        by_name = {}
        salary_total = 0
        for r in rows:
            amt = (r.get("salary_total") or 0)
            salary_total += amt
            name = (r.get("staff") or "").strip() or "Без имени"
            if amt:
                by_name[name] = by_name.get(name, 0) + amt

    return withdraw, purchase, refund, salary_total, by_name


# =========================
# DIGEST
# =========================

def compute_week_window(now: datetime) -> Tuple[date, date]:
    today = now.date()
    weekday = today.weekday()  # Monday=0
    this_monday = today - timedelta(days=weekday)
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    return last_monday, last_sunday


def digest_text(branch: str, start: date, end: date) -> str:
    rows = fetch_range(branch, start, end)
    title = f"📊 *Недельный дайджест* — *{branch_name(branch)}*"
    period = f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*"

    if not rows:
        return f"{title}\n{period}\n\n❌ Нет сохранённых отчётов за период."

    shifts_count = len(rows)

    services_total = sum((r.get("services_total") or 0) for r in rows)
    services_cash = sum((r.get("services_cash") or 0) for r in rows)
    services_kaspi = sum((r.get("services_kaspi") or 0) for r in rows)

    drinks_total = sum((r.get("drinks_total") or 0) for r in rows)
    drinks_cash = sum((r.get("drinks_cash") or 0) for r in rows)
    drinks_kaspi = sum((r.get("drinks_kaspi") or 0) for r in rows)

    withdraw_total = sum((r.get("withdraw_total") or 0) for r in rows)
    purchase_total = sum((r.get("purchase_total") or 0) for r in rows)
    refund_total = sum((r.get("refund_total") or 0) for r in rows)

    grand_total = services_total + drinks_total

    by_day: Dict[str, Dict[str, int]] = {}
    for r in rows:
        dkey = r["report_date"].isoformat()
        st = r.get("services_total") or 0
        dt_ = r.get("drinks_total") or 0
        if dkey not in by_day:
            by_day[dkey] = {"serv": 0, "drink": 0}
        by_day[dkey]["serv"] += st
        by_day[dkey]["drink"] += dt_

    best_day = None
    best_val = -1
    for dkey, vals in by_day.items():
        v = vals["serv"] + vals["drink"]
        if v > best_val:
            best_val = v
            best_day = dkey

    _wd, _pc, _rf, salary_total, salary_by = period_ops_and_salary(rows)

    lines = []
    lines.append(title)
    lines.append(period)
    lines.append("")
    lines.append(f"✅ Принято смен: *{shifts_count}*")
    lines.append("")
    lines.append("💰 *Итого за неделю*")
    lines.append(f"• Услуги: *{fmt_money(services_total)}*  (нал {fmt_money(services_cash)} / каспи {fmt_money(services_kaspi)})")
    lines.append(f"• Напитки: *{fmt_money(drinks_total)}* (нал {fmt_money(drinks_cash)} / каспи {fmt_money(drinks_kaspi)})")
    lines.append(f"• Общий итог (услуги+напитки): *{fmt_money(grand_total)}*")
    lines.append("")

    if grand_total > 0:
        share = int(round((drinks_total / grand_total) * 100))
        lines.append(f"🥤 Доля напитков: *{share}%*")
        lines.append("")

    if best_day:
        bd = datetime.fromisoformat(best_day).strftime("%d.%m.%Y")
        lines.append(f"🏆 Лучший день: *{bd}* — *{fmt_money(best_val)}*")
        lines.append("")

    lines.append("💸 *Зарплаты за неделю*")
    lines.append(f"• Всего: *{fmt_money(salary_total)}*")
    if salary_by:
        for name, val in sorted(salary_by.items()):
            if val:
                lines.append(f"  - {name}: *{fmt_money(val)}*")
    else:
        lines.append("  - —")
    lines.append("")

    lines.append("🧾 *Операции за неделю*")
    lines.append(f"• Изъятие: *{fmt_money(withdraw_total)}*")
    lines.append(f"• Закуп: *{fmt_money(purchase_total)}*")
    lines.append(f"• Возврат: *{fmt_money(refund_total)}*")
    lines.append("")

    lines.append("📅 *По дням* (услуги / напитки / итого)")
    for dkey in sorted(by_day.keys()):
        dt_obj = datetime.fromisoformat(dkey)
        dlabel = dt_obj.strftime("%d.%m")
        v = by_day[dkey]
        lines.append(f"• *{dlabel}*: {fmt_money(v['serv'])} / {fmt_money(v['drink'])} / *{fmt_money(v['serv'] + v['drink'])}*")

    return "\n".join(lines)


# =========================
# COMMANDS
# =========================

def parse_ddmmyyyy(s: str) -> Optional[date]:
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def parse_mmyyyy(s: str) -> Optional[Tuple[int, int]]:
    s = s.strip()
    m = re.fullmatch(r"(\d{2})\.(\d{4})", s)
    if not m:
        return None
    mm, yyyy = int(m.group(1)), int(m.group(2))
    if mm < 1 or mm > 12:
        return None
    return mm, yyyy


def format_salary_block(total: int, by_staff: Dict[str, int]) -> str:
    if total <= 0:
        return "💸 *Зарплата*: *0*\n—"
    lines = [f"💸 *Зарплата*: *{fmt_money(total)}*"]
    for name, val in sorted(by_staff.items()):
        if val:
            lines.append(f"• {name}: *{fmt_money(val)}*")
    return "\n".join(lines)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "Команды:\n"
        "• /help — помощь\n"
        "• /whoami — твой user_id\n"
        "• /week — статистика за последние 7 дней\n"
        "• /day 06.02.2026 — сводка за день\n"
        "• /month 02.2026 — сводка за месяц\n"
        "• /digest_week — вручную отправить недельный дайджест (прошлая неделя)\n\n"
        "Как слать отчёт:\n"
        "— обязательно указывать ДАТУ и СМЕНУ (День/Ночь)\n"
        "— 'Напитки' пишите отдельным блоком, как обычно\n"
        "— можно 2 отчёта одним сообщением (ночь и день)\n\n"
        "Зарплата (любая форма):\n"
        "— ЗП: 10000\n"
        "— Зарплата Уля: 12000\n"
        "— Аванс Ару 5000\n"
        "— Премия: 3000\n"
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
    rows = fetch_range(branch, start, end)

    if not rows:
        await update.message.reply_text("Нет сохранённых отчётов за последние 7 дней.")
        return

    serv = sum((r.get("services_total") or 0) for r in rows)
    drink = sum((r.get("drinks_total") or 0) for r in rows)
    total = serv + drink

    wd, pc, rf, sal, by_staff = period_ops_and_salary(rows)

    msg = (
        f"📈 *Последние 7 дней* — *{branch_name(branch)}*\n"
        f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*\n\n"
        f"• Услуги: *{fmt_money(serv)}*\n"
        f"• Напитки: *{fmt_money(drink)}*\n"
        f"• Итог: *{fmt_money(total)}*\n\n"
        f"{format_salary_block(sal, by_staff)}\n\n"
        f"🧾 *Операции*\n"
        f"• Закуп: *{fmt_money(pc)}*\n"
        f"• Возврат: *{fmt_money(rf)}*\n"
        f"• Изъятие: *{fmt_money(wd)}*"
    )
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

    d = parse_ddmmyyyy(context.args[0])
    if not d:
        await update.message.reply_text("Неверный формат. Пример: /day 06.02.2026")
        return

    rows = fetch_day(branch, d)
    if not rows:
        await update.message.reply_text("Нет отчётов за этот день.")
        return

    serv = sum((r.get("services_total") or 0) for r in rows)
    drink = sum((r.get("drinks_total") or 0) for r in rows)
    total = serv + drink
    shifts = len(rows)

    wd, pc, rf, sal, by_staff = period_ops_and_salary(rows)

    msg = (
        f"📌 *Сводка за {d.strftime('%d.%m.%Y')}* — *{branch_name(branch)}*\n"
        f"✅ Смен: *{shifts}*\n\n"
        f"• Услуги: *{fmt_money(serv)}*\n"
        f"• Напитки: *{fmt_money(drink)}*\n"
        f"• Итог: *{fmt_money(total)}*\n\n"
        f"{format_salary_block(sal, by_staff)}\n\n"
        f"🧾 *Операции*\n"
        f"• Закуп: *{fmt_money(pc)}*\n"
        f"• Возврат: *{fmt_money(rf)}*\n"
        f"• Изъятие: *{fmt_money(wd)}*"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        await update.message.reply_text("Этот чат не привязан к филиалу (t/k).")
        return

    if not context.args:
        await update.message.reply_text("Пример: /month 02.2026")
        return

    parsed = parse_mmyyyy(context.args[0])
    if not parsed:
        await update.message.reply_text("Неверный формат. Пример: /month 02.2026")
        return

    mm, yyyy = parsed
    start = date(yyyy, mm, 1)
    if mm == 12:
        end = date(yyyy + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(yyyy, mm + 1, 1) - timedelta(days=1)

    rows = fetch_range(branch, start, end)
    if not rows:
        await update.message.reply_text("Нет отчётов за этот месяц.")
        return

    serv = sum((r.get("services_total") or 0) for r in rows)
    drink = sum((r.get("drinks_total") or 0) for r in rows)
    total = serv + drink
    shifts = len(rows)

    wd, pc, rf, sal, by_staff = period_ops_and_salary(rows)

    msg = (
        f"🗓️ *Месячная сводка* — *{branch_name(branch)}*\n"
        f"Период: *{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}*\n"
        f"✅ Смен: *{shifts}*\n\n"
        f"• Услуги: *{fmt_money(serv)}*\n"
        f"• Напитки: *{fmt_money(drink)}*\n"
        f"• Итог: *{fmt_money(total)}*\n\n"
        f"{format_salary_block(sal, by_staff)}\n\n"
        f"🧾 *Операции за месяц*\n"
        f"• Закуп: *{fmt_money(pc)}*\n"
        f"• Возврат: *{fmt_money(rf)}*\n"
        f"• Изъятие: *{fmt_money(wd)}*"
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


# =========================
# TEXT HANDLER
# =========================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    branch = branch_from_chat(chat_id)
    if not branch:
        return

    text = update.message.text.strip()
    blocks = split_into_blocks(text)
    if not blocks:
        return

    parsed_reports: List[ParsedReport] = []
    for b in blocks:
        pr = parse_report_block(b)
        if pr:
            parsed_reports.append(pr)

    if not parsed_reports:
        return

    accepted_lines = []
    warns_all = []
    dupes = 0

    for pr in parsed_reports:
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
    now = datetime.now(TZ)
    start, end = compute_week_window(now)

    for branch, chat_id in [("t", CHAT_T), ("k", CHAT_K)]:
        msg = digest_text(branch, start, end)
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

    app.job_queue.scheduler.timezone = TZ

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("digest_week", cmd_digest_week))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if ENABLE_WEEKLY_DIGEST:
        hh, mm = WEEKLY_DIGEST_TIME.split(":")
        app.job_queue.run_daily(
            weekly_digest_job,
            time=dtime(hour=int(hh), minute=int(mm)),
            days=(0,),  # Monday
            name="weekly_digest",
        )

    print("🤖 Bot started (PostgreSQL)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
