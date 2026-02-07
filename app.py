from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import os, json, re, csv, io
from datetime import datetime, timezone, timedelta, date

app = Flask(__name__)
REPORTS_FILE = "reports.json"

# =========================
# SETTINGS
# =========================
REQUIRE_KNOWN_ADMIN = True
TZ_OFFSET_HOURS = 5  # change if needed

# REQUIRED for export links:
# Set in Railway Variables:
# BASE_URL = https://<your-app>.up.railway.app
# EXPORT_TOKEN = some-long-secret
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
EXPORT_TOKEN = os.environ.get("EXPORT_TOKEN", "")

ADMIN_BRANCH = {
    "whatsapp:+77070610093": "Polygon Turkistan",
    "whatsapp:+77081474845": "Polygon Kentau",
    "whatsapp:+77089273230": "Polygon Turkistan",  # your number (testing)
}

BRANCH_KEY = {
    "t": "Polygon Turkistan",
    "k": "Polygon Kentau",
}

# =========================
# TIME
# =========================
def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def today_str():
    return now_local().strftime("%d.%m.%Y")

def this_month_str():
    return now_local().strftime("%m.%Y")

def parse_d(s: str) -> date:
    return datetime.strptime(s, "%d.%m.%Y").date()

# =========================
# STORAGE
# =========================
def load_reports():
    if not os.path.exists(REPORTS_FILE):
        return []
    try:
        with open(REPORTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_reports(reports):
    with open(REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)

# =========================
# PARSING
# =========================
def norm_num(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s))

def fmt_money(n):
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")

def extract_money_after(labels, text: str):
    for label in labels:
        m = re.search(rf"{label}\s*:\s*([0-9\s]+)", text, re.IGNORECASE)
        if m:
            return norm_num(m.group(1))
    return None

def extract_shift_header(block: str):
    dm = re.search(r"(\d{2}\.\d{2}\.\d{4})", block)
    d = dm.group(1) if dm else None

    sm = re.search(r"\b(ночь|ночная|день|дневная)\b", block, re.IGNORECASE)
    shift_raw = sm.group(1).lower() if sm else None
    shift = "Ночь" if shift_raw and "ноч" in shift_raw else ("День" if shift_raw else None)

    em = re.search(r"смена\s+([A-Za-zА-Яа-яЁё]+)", block, re.IGNORECASE)
    employee = em.group(1) if em else None
    return d, shift, employee

def split_into_reports(text: str):
    t = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    header_re = re.compile(
        r"(?=(\d{2}\.\d{2}\.\d{4}).{0,120}\b(ночь|ночная|день|дневная)\b)",
        re.IGNORECASE | re.DOTALL
    )
    positions = [m.start() for m in header_re.finditer(t)]
    if len(positions) <= 1:
        return [t]

    blocks = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(t)
        block = t[pos:end].strip()
        if block:
            blocks.append(block)
    return blocks

def parse_line_items_from_section(label_variants, block: str):
    lbl = "|".join([re.escape(x) for x in label_variants])
    m = re.search(
        rf"({lbl})\s*:\s*(.+?)(Остаток|Изъятие|Закуп|Возврат|Напитки|Бар|$)",
        block, re.IGNORECASE | re.DOTALL
    )
    if not m:
        return []
    raw = m.group(2).strip()
    lines = [x.strip() for x in raw.replace("\r", "").split("\n") if x.strip()]
    if not lines and raw:
        lines = [" ".join(raw.split())]
    return lines

def sum_amounts_in_lines(lines):
    total = 0
    for ln in lines:
        for m in re.finditer(r"([0-9][0-9\s]{0,})", ln):
            total += norm_num(m.group(1))
    return total

def parse_one_report(block: str):
    d, shift, employee = extract_shift_header(block)

    total = extract_money_after(["Общая касса", "Касса общая", "Общая"], block)

    # Total section: Nal/Kaspi near "Общая касса"
    cash = None
    kaspi = None
    m_total_sec = re.search(
        r"(Общая касса|Касса общая|Общая)\s*:\s*[0-9\s]+(.+?)(Напитки|Бар|Изъятие|Закуп|Возврат|Остаток|$)",
        block, re.IGNORECASE | re.DOTALL
    )
    if m_total_sec:
        sec = m_total_sec.group(2)
        m1 = re.search(r"(Нал|Наличка|Наличные)\s*:\s*([0-9\s]+)", sec, re.IGNORECASE)
        m2 = re.search(r"(Каспи|Kaspi|KASPI)\s*:\s*([0-9\s]+)", sec, re.IGNORECASE)
        cash = norm_num(m1.group(2)) if m1 else None
        kaspi = norm_num(m2.group(2)) if m2 else None
    else:
        cash = extract_money_after(["Нал", "Наличка", "Наличные"], block)
        kaspi = extract_money_after(["Каспи", "Kaspi", "KASPI"], block)

    # Drinks
    drinks_total = extract_money_after(["Напитки", "Бар", "Напиток"], block)
    drinks_cash = None
    drinks_kaspi = None
    m_drinks = re.search(
        r"(Напитки|Бар)\s*:\s*([0-9\s]+)(.+?)(Изъятие|Закуп|Возврат|Остаток|$)",
        block, re.IGNORECASE | re.DOTALL
    )
    if m_drinks:
        sec = m_drinks.group(3)
        m1 = re.search(r"(Нал|Наличка|Наличные)\s*:\s*([0-9\s]+)", sec, re.IGNORECASE)
        m2 = re.search(r"(Каспи|Kaspi|KASPI)\s*:\s*([0-9\s]+)", sec, re.IGNORECASE)
        drinks_cash = norm_num(m1.group(2)) if m1 else None
        drinks_kaspi = norm_num(m2.group(2)) if m2 else None

    withdrawals_lines = parse_line_items_from_section(["Изъятие"], block)
    refunds_lines = parse_line_items_from_section(["Возврат", "Возвраты"], block)
    expenses_lines = parse_line_items_from_section(["Закуп", "Покупка", "Расход"], block)

    # if "возврат" mistakenly inside withdrawals
    if withdrawals_lines and not refunds_lines:
        cand = [ln for ln in withdrawals_lines if re.search(r"\bвозврат\b", ln, re.IGNORECASE)]
        if cand:
            refunds_lines = cand

    refunds_sum = sum_amounts_in_lines(refunds_lines) if refunds_lines else 0
    expenses_sum = sum_amounts_in_lines(expenses_lines) if expenses_lines else 0
    withdrawals_sum = sum_amounts_in_lines(withdrawals_lines) if withdrawals_lines else 0

    remainder = None
    r = re.search(r"Остаток\s*:\s*(.+)$", block, re.IGNORECASE | re.DOTALL)
    if r:
        remainder = " ".join(r.group(1).strip().split())

    if not d or not shift:
        return None, "Не смог определить дату или смену (Ночь/День). Напиши: help"

    data = {
        "date": d,
        "shift": shift,
        "employee": employee,
        "total": total,
        "cash": cash,
        "kaspi": kaspi,
        "drinks_total": drinks_total,
        "drinks_cash": drinks_cash,
        "drinks_kaspi": drinks_kaspi,
        "withdrawals": withdrawals_lines,
        "withdrawals_sum": withdrawals_sum,
        "refunds": refunds_lines,
        "refunds_sum": refunds_sum,
        "expenses": expenses_lines,
        "expenses_sum": expenses_sum,
        "remainder": remainder,
        "raw": block,
    }
    return data, None

# =========================
# VALIDATION + SMART HINTS
# =========================
def validate_report(r):
    warnings = []
    hint = None

    total = r.get("total")
    cash = r.get("cash")
    kaspi = r.get("kaspi")

    dt = r.get("drinks_total")
    dc = r.get("drinks_cash")
    dk = r.get("drinks_kaspi")

    # Turnover check
    if total is not None and cash is not None and kaspi is not None:
        if total != (cash + kaspi):
            warnings.append(f"⚠️ Общая касса не сходится: {total} ≠ {cash}+{kaspi}")
            # smart hint: maybe drinks were included into total
            if dt is not None and (cash + kaspi + dt) == total:
                hint = "ℹ️ Похоже, вы включили НАПИТКИ в «Общую кассу». Напитки отправляйте ОТДЕЛЬНО."

    # Drinks check
    if dt is not None and dc is not None and dk is not None:
        if dt != (dc + dk):
            warnings.append(f"⚠️ Напитки не сходятся: {dt} ≠ {dc}+{dk}")
            if not hint:
                hint = "ℹ️ Напитки должны сходиться: Нал(напитки)+Каспи(напитки) = Напитки."

    if dt is not None and total is not None and dt > total:
        warnings.append(f"⚠️ Напитки больше общей кассы: {dt} > {total}")

    return warnings, hint

# =========================
# FILTERS
# =========================
def filter_by_date(reports, d, branch_name=None):
    rows = [r for r in reports if r.get("date") == d]
    if branch_name:
        rows = [r for r in rows if r.get("branch") == branch_name]
    return rows

def filter_by_month(reports, mm_yyyy, branch_name=None):
    rows = [r for r in reports if r.get("date", "").endswith(mm_yyyy)]
    if branch_name:
        rows = [r for r in rows if r.get("branch") == branch_name]
    return rows

def filter_by_range(reports, d1, d2, branch_name=None):
    a = parse_d(d1)
    b = parse_d(d2)
    if b < a:
        a, b = b, a
    out = []
    for r in reports:
        ds = r.get("date")
        if not ds:
            continue
        try:
            rd = parse_d(ds)
        except Exception:
            continue
        if a <= rd <= b:
            out.append(r)
    if branch_name:
        out = [r for r in out if r.get("branch") == branch_name]
    return out

def filter_last_days(reports, days: int, branch_name=None):
    end = now_local().date()
    start = end - timedelta(days=days - 1)
    out = []
    for r in reports:
        ds = r.get("date")
        if not ds:
            continue
        try:
            rd = parse_d(ds)
        except Exception:
            continue
        if start <= rd <= end:
            out.append(r)
    if branch_name:
        out = [r for r in out if r.get("branch") == branch_name]
    return out

# =========================
# SUMMARIES
# =========================
def summarize_turnover(rows):
    rows = [r for r in rows if r.get("total") is not None]
    if not rows:
        return None

    by = {}
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        sh = r.get("shift") or "?"
        by.setdefault(br, {"Ночь": 0, "День": 0, "?": 0})
        by[br][sh] = by[br].get(sh, 0) + (r.get("total") or 0)

    lines = []
    grand = 0
    for br in sorted(by.keys()):
        night = by[br].get("Ночь", 0)
        dayv = by[br].get("День", 0)
        unk = by[br].get("?", 0)
        br_sum = night + dayv + unk

        lines.append(f"\n🏢 {br}")
        lines.append(f"  🌙 Ночь: {fmt_money(night)}")
        lines.append(f"  ☀️ День: {fmt_money(dayv)}")
        if unk:
            lines.append(f"  ❓ Неизв.: {fmt_money(unk)}")
        lines.append(f"  ✅ Итого: {fmt_money(br_sum)}")
        grand += br_sum

    lines.append(f"\n💰 Общий итог: {fmt_money(grand)}")
    return "\n".join(lines).strip()

def summarize_bar(rows):
    rows = [r for r in rows if r.get("drinks_total") is not None]
    if not rows:
        return None
    by = {}
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        by.setdefault(br, 0)
        by[br] += (r.get("drinks_total") or 0)
    lines = ["🥤 Напитки (оборот бара):"]
    grand = 0
    for br in sorted(by.keys()):
        lines.append(f"- {br}: {fmt_money(by[br])}")
        grand += by[br]
    lines.append(f"\nИтого: {fmt_money(grand)}")
    return "\n".join(lines).strip()

def summarize_money_lines(rows, field, title):
    if not rows:
        return None
    by = {}
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        by.setdefault(br, {"sum": 0, "lines": []})

        lines = r.get(field) or []
        by[br]["lines"].extend([f"{r.get('date','?')} {r.get('shift','?')}: {ln}" for ln in lines])

        if field == "refunds":
            by[br]["sum"] += (r.get("refunds_sum") or 0)
        elif field == "expenses":
            by[br]["sum"] += (r.get("expenses_sum") or 0)
        elif field == "withdrawals":
            by[br]["sum"] += (r.get("withdrawals_sum") or 0)

    out = [title]
    grand = 0
    for br in sorted(by.keys()):
        out.append(f"\n🏢 {br}: {fmt_money(by[br]['sum'])}")
        grand += by[br]["sum"]
        tail = by[br]["lines"][-8:]
        for ln in tail:
            out.append(f"- {ln}")
        if len(by[br]["lines"]) > 8:
            out.append(f"... ещё {len(by[br]['lines'])-8} строк")

    out.append(f"\nИтого: {fmt_money(grand)}")
    return "\n".join(out).strip()

def list_errors(reports, limit=20, branch_name=None):
    bad = []
    for r in reports:
        if branch_name and r.get("branch") != branch_name:
            continue
        ws, _ = validate_report(r)
        if ws:
            bad.append((r, ws))
    if not bad:
        return "✅ Ошибок в отчётах не найдено."
    bad = bad[-limit:]
    lines = [f"⚠️ Последние ошибки (до {limit}):"]
    for r, ws in bad:
        lines.append(f"\n{r.get('branch','?')} | {r.get('date','?')} — {r.get('shift','?')} ({r.get('employee','')})")
        for w in ws:
            lines.append(f"- {w}")
    return "\n".join(lines).strip()

def list_branches_today(reports):
    d = today_str()
    rows = [r for r in reports if r.get("date") == d]
    counts = {}
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        counts[br] = counts.get(br, 0) + 1
    lines = [f"🏢 Филиалы сегодня ({d}):"]
    if not counts:
        lines.append("— отчётов нет")
        return "\n".join(lines)
    for br in sorted(counts.keys()):
        lines.append(f"- {br}: {counts[br]} отч.")
    return "\n".join(lines)

def compare_branches(rows, a, b):
    a_sum = 0
    b_sum = 0
    for r in rows:
        if r.get("total") is None:
            continue
        if r.get("branch") == a:
            a_sum += r.get("total") or 0
        elif r.get("branch") == b:
            b_sum += r.get("total") or 0
    diff = a_sum - b_sum
    lines = [
        "📊 Сравнение филиалов (оборот):",
        f"- {a}: {fmt_money(a_sum)}",
        f"- {b}: {fmt_money(b_sum)}",
        f"\nРазница: {fmt_money(diff)} ({'+' if diff >= 0 else ''}{diff})"
    ]
    return "\n".join(lines)

def rating_month(reports, mm_yyyy, branch_name=None):
    rows = filter_by_month(reports, mm_yyyy, branch_name=branch_name)
    if not rows:
        return "Нет данных."
    by_emp = {}
    for r in rows:
        emp = r.get("employee") or "(без имени)"
        by_emp.setdefault(emp, {"turnover": 0, "reports": 0, "errors": 0})
        by_emp[emp]["reports"] += 1
        if r.get("total") is not None:
            by_emp[emp]["turnover"] += r.get("total") or 0
        ws, _ = validate_report(r)
        if ws:
            by_emp[emp]["errors"] += 1

    ranked = sorted(by_emp.items(), key=lambda kv: (kv[1]["turnover"], -kv[1]["errors"]), reverse=True)
    lines = [f"🏆 Рейтинг админов за {mm_yyyy}" + (f" | {branch_name}" if branch_name else "")]
    for i, (emp, st) in enumerate(ranked[:12], 1):
        lines.append(f"{i}) {emp}: оборот {fmt_money(st['turnover'])}, отчётов {st['reports']}, ошибок {st['errors']}")
    return "\n".join(lines)

def top_days(reports, days=30, branch_name=None):
    rows = filter_last_days(reports, days, branch_name=branch_name)
    if not rows:
        return "Нет данных."
    by_day = {}
    for r in rows:
        if r.get("total") is None:
            continue
        d = r.get("date")
        by_day.setdefault(d, 0)
        by_day[d] += r.get("total") or 0
    ranked = sorted(by_day.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"📈 Топ дней за последние {days} дней" + (f" | {branch_name}" if branch_name else "")]
    for i, (d, v) in enumerate(ranked[:10], 1):
        lines.append(f"{i}) {d}: {fmt_money(v)}")
    return "\n".join(lines)

# =========================
# COMMAND PARSER (suffix t/k)
# =========================
def parse_branch_suffix(parts):
    if parts and parts[-1].lower() in BRANCH_KEY:
        key = parts[-1].lower()
        return BRANCH_KEY[key], parts[:-1]
    return None, parts

def rules_text():
    return (
        "📌 Правила отчёта:\n"
        "1) «Общая касса» = ТОЛЬКО услуги\n"
        "   Общая касса = Нал + Каспи\n"
        "2) «Напитки» = отдельно (бар)\n"
        "   Напитки = Нал(напитки) + Каспи(напитки)\n"
        "3) «Изъятие», «Закуп», «Возврат» — отдельные строки, на оборот не влияют.\n"
    )

def help_text():
    return (
        "Филиалы (суффикс): t = Polygon Turkistan, k = Polygon Kentau\n\n"
        "Команды (можно добавить t/k в конце):\n"
        "today / сегодня [t|k] — оборот за сегодня\n"
        "day 06.02.2026 [t|k] — оборот за дату\n"
        "month / месяц [t|k] — оборот за текущий месяц\n"
        "month 02.2026 [t|k] — оборот за месяц\n"
        "range 01.02.2026 07.02.2026 [t|k] — оборот за период\n"
        "week [t|k] — оборот за 7 дней\n"
        "bar day 06.02.2026 [t|k] — напитки за дату\n"
        "bar month 02.2026 [t|k] — напитки за месяц\n"
        "compare 02.2026 — сравнить t vs k за месяц\n"
        "rating 02.2026 [t|k] — рейтинг админов (оборот/ошибки)\n"
        "topdays 30 [t|k] — топ дней за N дней\n"
        "refunds day 06.02.2026 [t|k] — возвраты\n"
        "expenses month 02.2026 [t|k] — закуп\n"
        "withdrawals day 06.02.2026 [t|k] — изъятие\n"
        "export month 02.2026 [t|k] — CSV для Excel (ссылка)\n"
        "errors [t|k] — последние ошибки\n"
        "branch — сколько отчётов по филиалам сегодня\n"
        "rules — правила отчёта\n"
        "whoami — показать твой номер как видит Twilio\n"
        "help — это сообщение\n"
    )

# =========================
# EXPORT HELPERS
# =========================
def require_export_auth(token: str) -> bool:
    return bool(EXPORT_TOKEN) and token == EXPORT_TOKEN

def reports_to_csv(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ts_utc", "from", "branch", "date", "shift", "employee",
        "total", "cash", "kaspi",
        "drinks_total", "drinks_cash", "drinks_kaspi",
        "refunds_sum", "expenses_sum", "withdrawals_sum",
        "remainder"
    ])
    for r in rows:
        writer.writerow([
            r.get("ts_utc",""),
            r.get("from",""),
            r.get("branch",""),
            r.get("date",""),
            r.get("shift",""),
            r.get("employee",""),
            r.get("total",""),
            r.get("cash",""),
            r.get("kaspi",""),
            r.get("drinks_total",""),
            r.get("drinks_cash",""),
            r.get("drinks_kaspi",""),
            r.get("refunds_sum",""),
            r.get("expenses_sum",""),
            r.get("withdrawals_sum",""),
            r.get("remainder",""),
        ])
    return output.getvalue()

def make_export_link(kind, value, branch_key=None):
    # kind: month/day/range
    if not BASE_URL or not EXPORT_TOKEN:
        return None
    params = [f"token={EXPORT_TOKEN}", f"kind={kind}", f"value={value}"]
    if branch_key:
        params.append(f"branch={branch_key}")
    return f"{BASE_URL}/export?" + "&".join(params)

# =========================
# ROUTES
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming = (request.values.get("Body", "") or "").strip()
    sender = request.values.get("From", "")
    sender_branch = ADMIN_BRANCH.get(sender)
    resp = MessagingResponse()
    reports = load_reports()

    parts = incoming.split()
    branch_filter, parts2 = parse_branch_suffix(parts)
    cmd = (parts2[0].lower() if parts2 else "")

    # whoami
    if incoming.lower().strip() == "whoami":
        resp.message(f"From: {sender}\nФилиал: {sender_branch or '(не задан)'}")
        return str(resp)

    # help / rules
    if incoming.lower().strip() in ("help", "помощь"):
        resp.message(help_text())
        return str(resp)
    if incoming.lower().strip() == "rules":
        resp.message(rules_text())
        return str(resp)

    # branch
    if cmd == "branch" and len(parts2) == 1:
        resp.message(list_branches_today(reports))
        return str(resp)

    # errors [t|k]
    if cmd == "errors" and len(parts2) == 1:
        resp.message(list_errors(reports, branch_name=branch_filter))
        return str(resp)

    # today / сегодня [t|k]
    if cmd in ("today", "сегодня") and len(parts2) == 1:
        d = today_str()
        rows = filter_by_date(reports, d, branch_name=branch_filter)
        header = f"📊 Сегодня ({d}) — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # day DD.MM.YYYY [t|k]
    if cmd == "day" and len(parts2) == 2 and re.match(r"^\d{2}\.\d{2}\.\d{4}$", parts2[1]):
        d = parts2[1]
        rows = filter_by_date(reports, d, branch_name=branch_filter)
        header = f"📊 {d} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # month / месяц [t|k]
    if cmd in ("month", "месяц") and len(parts2) == 1:
        mm = this_month_str()
        rows = filter_by_month(reports, mm, branch_name=branch_filter)
        header = f"📅 {mm} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # month MM.YYYY [t|k]
    if cmd == "month" and len(parts2) == 2 and re.match(r"^\d{2}\.\d{4}$", parts2[1]):
        mm = parts2[1]
        rows = filter_by_month(reports, mm, branch_name=branch_filter)
        header = f"📅 {mm} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # range d1 d2 [t|k]
    if cmd == "range" and len(parts2) == 3 and re.match(r"^\d{2}\.\d{2}\.\d{4}$", parts2[1]) and re.match(r"^\d{2}\.\d{2}\.\d{4}$", parts2[2]):
        d1, d2 = parts2[1], parts2[2]
        rows = filter_by_range(reports, d1, d2, branch_name=branch_filter)
        header = f"📆 {d1}–{d2} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # week [t|k]  (7 days)
    if cmd == "week" and len(parts2) == 1:
        rows = filter_last_days(reports, 7, branch_name=branch_filter)
        header = f"📅 Последние 7 дней — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # bar day/date or bar month/mm
    if cmd == "bar" and len(parts2) >= 3:
        period = parts2[1].lower()
        arg = parts2[2]
        if period == "day" and re.match(r"^\d{2}\.\d{2}\.\d{4}$", arg):
            rows = filter_by_date(reports, arg, branch_name=branch_filter)
            header = f"🥤 Напитки за {arg}" + (f" | {branch_filter}" if branch_filter else "")
            body = summarize_bar(rows)
            resp.message(header + ("\n\n" + body if body else "\n\nНет данных по напиткам."))
            return str(resp)
        if period == "month" and re.match(r"^\d{2}\.\d{4}$", arg):
            rows = filter_by_month(reports, arg, branch_name=branch_filter)
            header = f"🥤 Напитки за {arg}" + (f" | {branch_filter}" if branch_filter else "")
            body = summarize_bar(rows)
            resp.message(header + ("\n\n" + body if body else "\n\nНет данных по напиткам."))
            return str(resp)
        resp.message("❌ Формат: bar day 06.02.2026 [t|k] или bar month 02.2026 [t|k]")
        return str(resp)

    # compare MM.YYYY  (t vs k)
    if cmd == "compare" and len(parts2) == 2 and re.match(r"^\d{2}\.\d{4}$", parts2[1]):
        mm = parts2[1]
        rows = filter_by_month(reports, mm)
        resp.message(compare_branches(rows, BRANCH_KEY["t"], BRANCH_KEY["k"]))
        return str(resp)

    # rating MM.YYYY [t|k]
    if cmd == "rating" and len(parts2) == 2 and re.match(r"^\d{2}\.\d{4}$", parts2[1]):
        mm = parts2[1]
        resp.message(rating_month(reports, mm, branch_name=branch_filter))
        return str(resp)

    # topdays N [t|k]
    if cmd == "topdays" and len(parts2) == 2 and re.match(r"^\d+$", parts2[1]):
        n = int(parts2[1])
        n = max(7, min(120, n))
        resp.message(top_days(reports, days=n, branch_name=branch_filter))
        return str(resp)

    # refunds/expenses/withdrawals day|month
    if cmd in ("refunds", "expenses", "withdrawals") and len(parts2) >= 3:
        period = parts2[1].lower()
        arg = parts2[2]
        if period == "day":
            if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", arg):
                resp.message(f"❌ Формат: {cmd} day 06.02.2026 [t|k]")
                return str(resp)
            rows = filter_by_date(reports, arg, branch_name=branch_filter)
        elif period == "month":
            if not re.match(r"^\d{2}\.\d{4}$", arg):
                resp.message(f"❌ Формат: {cmd} month 02.2026 [t|k]")
                return str(resp)
            rows = filter_by_month(reports, arg, branch_name=branch_filter)
        else:
            resp.message(f"❌ Формат: {cmd} day ... или {cmd} month ...")
            return str(resp)

        title = {
            "refunds": f"↩️ Возвраты за {arg}",
            "expenses": f"🧾 Закуп за {arg}",
            "withdrawals": f"💸 Изъятие за {arg}",
        }[cmd]
        title += (f" | {branch_filter}" if branch_filter else "")
        field = {"refunds": "refunds", "expenses": "expenses", "withdrawals": "withdrawals"}[cmd]
        body = summarize_money_lines(rows, field, title)
        resp.message(body if body else (title + "\n\nНет данных."))
        return str(resp)

    # export month/mm or export day/date (returns link)
    if cmd == "export" and len(parts2) >= 3:
        period = parts2[1].lower()
        arg = parts2[2]
        branch_key = None
        if branch_filter:
            # reverse map
            for k, v in BRANCH_KEY.items():
                if v == branch_filter:
                    branch_key = k
                    break

        if period == "month" and re.match(r"^\d{2}\.\d{4}$", arg):
            link = make_export_link("month", arg, branch_key=branch_key)
            if not link:
                resp.message("❌ Для export нужно установить BASE_URL и EXPORT_TOKEN в Railway Variables.")
                return str(resp)
            resp.message(f"📁 CSV за {arg}" + (f" | {branch_filter}" if branch_filter else "") + f"\n{link}")
            return str(resp)

        if period == "day" and re.match(r"^\d{2}\.\d{2}\.\d{4}$", arg):
            link = make_export_link("day", arg, branch_key=branch_key)
            if not link:
                resp.message("❌ Для export нужно установить BASE_URL и EXPORT_TOKEN в Railway Variables.")
                return str(resp)
            resp.message(f"📁 CSV за {arg}" + (f" | {branch_filter}" if branch_filter else "") + f"\n{link}")
            return str(resp)

        if period == "range" and len(parts2) >= 4 and re.match(r"^\d{2}\.\d{2}\.\d{4}$", arg) and re.match(r"^\d{2}\.\d{2}\.\d{4}$", parts2[3]):
            value = f"{arg},{parts2[3]}"
            link = make_export_link("range", value, branch_key=branch_key)
            if not link:
                resp.message("❌ Для export нужно установить BASE_URL и EXPORT_TOKEN в Railway Variables.")
                return str(resp)
            resp.message(f"📁 CSV период {arg}–{parts2[3]}" + (f" | {branch_filter}" if branch_filter else "") + f"\n{link}")
            return str(resp)

        resp.message("❌ Формат: export month 02.2026 [t|k] | export day 06.02.2026 [t|k] | export range d1 d2 [t|k]")
        return str(resp)

    # =========================
    # REPORT INTAKE
    # =========================
    if REQUIRE_KNOWN_ADMIN and not sender_branch:
        resp.message("⛔ У вас нет доступа к отправке отчётов. (номер не зарегистрирован)")
        return str(resp)

    blocks = split_into_reports(incoming)
    parsed = []
    for b in blocks:
        data, err = parse_one_report(b)
        if err:
            resp.message("❌ " + err)
            return str(resp)
        data["branch"] = sender_branch or "(филиал не задан)"
        parsed.append(data)

    ts = datetime.now(timezone.utc).isoformat()
    for dct in parsed:
        dct["ts_utc"] = ts
        dct["from"] = sender
        reports.append(dct)

    save_reports(reports)

    lines = [f"✅ Принял отчёты: {len(parsed)} шт."]
    hints = []
    warnings_all = []
    for i, dct in enumerate(parsed, 1):
        lines.append(
            f"{i}) {dct.get('branch')} | {dct['date']} — {dct['shift']}"
            + (f" ({dct['employee']})" if dct.get("employee") else "")
            + (f": оборот {fmt_money(dct.get('total'))}" if dct.get("total") is not None else "")
        )
        if dct.get("drinks_total") is not None:
            lines.append(f"   🥤 Напитки: {fmt_money(dct.get('drinks_total'))}")
        if dct.get("refunds_sum"):
            lines.append(f"   ↩️ Возврат: {fmt_money(dct.get('refunds_sum'))}")
        if dct.get("expenses_sum"):
            lines.append(f"   🧾 Закуп: {fmt_money(dct.get('expenses_sum'))}")
        if dct.get("withdrawals_sum"):
            lines.append(f"   💸 Изъятие: {fmt_money(dct.get('withdrawals_sum'))}")

        ws, hint = validate_report(dct)
        warnings_all.extend(ws)
        if hint:
            hints.append(hint)

    if warnings_all:
        lines.append("\n" + "\n".join(warnings_all))
    if hints:
        # show unique hints
        uniq = []
        for h in hints:
            if h not in uniq:
                uniq.append(h)
        lines.append("\n" + "\n".join(uniq))
        lines.append("\n" + "📌 Напоминание: «Общая касса» = услуги. «Напитки» — отдельно.")

    resp.message("\n".join(lines))
    return str(resp)

@app.route("/")
def health():
    return "OK"

@app.route("/export", methods=["GET"])
def export():
    token = request.args.get("token", "")
    if not require_export_auth(token):
        return Response("Forbidden", status=403)

    kind = request.args.get("kind", "")
    value = request.args.get("value", "")
    branch_key = request.args.get("branch", "")
    branch_name = BRANCH_KEY.get(branch_key) if branch_key else None

    reports = load_reports()

    if kind == "month" and re.match(r"^\d{2}\.\d{4}$", value):
        rows = filter_by_month(reports, value, branch_name=branch_name)
        name = f"reports_{value.replace('.','_')}"
    elif kind == "day" and re.match(r"^\d{2}\.\d{2}\.\d{4}$", value):
        rows = filter_by_date(reports, value, branch_name=branch_name)
        name = f"reports_{value.replace('.','_')}"
    elif kind == "range":
        # value like d1,d2
        if "," not in value:
            return Response("Bad Request", status=400)
        d1, d2 = value.split(",", 1)
        if not (re.match(r"^\d{2}\.\d{2}\.\d{4}$", d1) and re.match(r"^\d{2}\.\d{2}\.\d{4}$", d2)):
            return Response("Bad Request", status=400)
        rows = filter_by_range(reports, d1, d2, branch_name=branch_name)
        name = f"reports_{d1.replace('.','_')}_{d2.replace('.','_')}"
    else:
        return Response("Bad Request", status=400)

    csv_text = reports_to_csv(rows)
    filename = name + (f"_{branch_key}" if branch_key else "") + ".csv"
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.route("/reports", methods=["GET"])
def get_reports():
    return {"reports": load_reports()}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
