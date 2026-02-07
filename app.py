from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os, json, re
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
REPORTS_FILE = "reports.json"

# === SETTINGS ===
REQUIRE_KNOWN_ADMIN = True   # only known numbers can submit reports
TZ_OFFSET_HOURS = 5          # Kazakhstan often UTC+5 (change to 6 if needed)

# Admin numbers -> branches (Twilio format: whatsapp:+7XXXXXXXXXX)
ADMIN_BRANCH = {
    "whatsapp:+77070610093": "Polygon Turkistan",
    "whatsapp:+77081474845": "Polygon Kentau",
    "whatsapp:+77089273230": "Polygon Turkistan",  # your number (testing)
}

# Short branch keys (command suffix)
BRANCH_KEY = {
    "t": "Polygon Turkistan",
    "k": "Polygon Kentau",
}

# === TIME ===
def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def today_str():
    return now_local().strftime("%d.%m.%Y")

# === STORAGE ===
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

# === PARSING HELPERS ===
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
    date = dm.group(1) if dm else None

    sm = re.search(r"\b(ночь|ночная|день|дневная)\b", block, re.IGNORECASE)
    shift_raw = sm.group(1).lower() if sm else None
    shift = "Ночь" if shift_raw and "ноч" in shift_raw else ("День" if shift_raw else None)

    em = re.search(r"смена\s+([A-Za-zА-Яа-яЁё]+)", block, re.IGNORECASE)
    employee = em.group(1) if em else None

    return date, shift, employee

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
        end = positions[i+1] if i+1 < len(positions) else len(t)
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
    date, shift, employee = extract_shift_header(block)

    total = extract_money_after(["Общая касса", "Касса общая", "Общая"], block)

    # Total section: first Nal/Kaspi after "Общая касса"
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

    # Sections: withdrawals / refunds / expenses
    withdrawals_lines = parse_line_items_from_section(["Изъятие"], block)
    refunds_lines = parse_line_items_from_section(["Возврат", "Возвраты"], block)
    expenses_lines = parse_line_items_from_section(["Закуп", "Покупка", "Расход"], block)

    # If "Возврат" accidentally written inside "Изъятие"
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

    if not date or not shift:
        return None, "Не смог определить дату или смену (Ночь/День) в одном из отчётов."

    data = {
        "date": date,
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

# === VALIDATION ===
def validate_report(r):
    warnings = []
    total = r.get("total")
    cash = r.get("cash")
    kaspi = r.get("kaspi")

    # Turnover check
    if total is not None and cash is not None and kaspi is not None:
        if total != (cash + kaspi):
            warnings.append(f"⚠️ Общая касса не сходится: {total} ≠ {cash}+{kaspi}")

    # Drinks check
    dt = r.get("drinks_total")
    dc = r.get("drinks_cash")
    dk = r.get("drinks_kaspi")
    if dt is not None and dc is not None and dk is not None:
        if dt != (dc + dk):
            warnings.append(f"⚠️ Напитки не сходятся: {dt} ≠ {dc}+{dk}")

    if dt is not None and total is not None and dt > total:
        warnings.append(f"⚠️ Напитки больше общей кассы: {dt} > {total}")

    return warnings

# === FILTERS ===
def parse_d(s):
    return datetime.strptime(s, "%d.%m.%Y").date()

def filter_by_date(reports, date_str, branch_name=None):
    rows = [r for r in reports if r.get("date") == date_str]
    if branch_name:
        rows = [r for r in rows if (r.get("branch") == branch_name)]
    return rows

def filter_by_month(reports, mm_yyyy, branch_name=None):
    rows = [r for r in reports if r.get("date", "").endswith(mm_yyyy)]
    if branch_name:
        rows = [r for r in rows if (r.get("branch") == branch_name)]
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
        out = [r for r in out if (r.get("branch") == branch_name)]
    return out

# === SUMMARIES ===
def summarize_turnover(rows):
    rows = [r for r in rows if r.get("total") is not None]
    if not rows:
        return None

    by = {}  # branch -> shift -> sum (using last report per shift per day is okay; but for period we sum)
    # For periods we sum totals per report (each report is shift), so sum is fine.
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        sh = r.get("shift") or "?"
        by.setdefault(br, {"Ночь": 0, "День": 0, "?": 0})
        by[br][sh] = by[br].get(sh, 0) + (r.get("total") or 0)

    lines = []
    grand = 0
    for br in sorted(by.keys()):
        night = by[br].get("Ночь", 0)
        day = by[br].get("День", 0)
        unk = by[br].get("?", 0)
        br_sum = night + day + unk

        lines.append(f"\n🏢 {br}")
        lines.append(f"  🌙 Ночь: {fmt_money(night)}")
        lines.append(f"  ☀️ День: {fmt_money(day)}")
        if unk:
            lines.append(f"  ❓ Неизв.: {fmt_money(unk)}")
        lines.append(f"  ✅ Итого: {fmt_money(br_sum)}")
        grand += br_sum

    lines.append(f"\n💰 Общий итог: {fmt_money(grand)}")
    return "\n".join(lines).strip()

def summarize_money_lines(rows, field, title):
    if not rows:
        return None

    by = {}  # branch -> {"sum": int, "lines": []}
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

def list_errors(reports, limit=20, branch_name=None):
    bad = []
    for r in reports:
        if branch_name and r.get("branch") != branch_name:
            continue
        ws = validate_report(r)
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

# === COMMAND PARSER (suffix t/k) ===
def parse_branch_suffix(parts):
    """
    If last token is 't' or 'k' => returns (branch_name, parts_without_suffix)
    """
    if parts and parts[-1].lower() in BRANCH_KEY:
        key = parts[-1].lower()
        return BRANCH_KEY[key], parts[:-1]
    return None, parts

def help_text():
    return (
        "Филиалы (суффикс): t = Polygon Turkistan, k = Polygon Kentau\n\n"
        "Команды:\n"
        "today / сегодня [t|k] — оборот за сегодня\n"
        "day 06.02.2026 [t|k] — оборот за дату\n"
        "month / месяц [t|k] — оборот за текущий месяц\n"
        "month 02.2026 [t|k] — оборот за месяц\n"
        "range 01.02.2026 07.02.2026 [t|k] — оборот за период\n"
        "refunds day 06.02.2026 [t|k] — возвраты\n"
        "expenses month 02.2026 [t|k] — закуп\n"
        "withdrawals day 06.02.2026 [t|k] — изъятие\n"
        "errors [t|k] — последние ошибки\n"
        "branch — сколько отчётов по филиалам сегодня\n"
        "whoami — показать твой номер как видит Twilio\n"
        "help — это сообщение\n\n"
        "Отчёт отправляйте как обычно (можно 2 смены одним сообщением)."
    )

# === ROUTES ===
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming = (request.values.get("Body", "") or "").strip()
    sender = request.values.get("From", "")
    sender_branch = ADMIN_BRANCH.get(sender)
    resp = MessagingResponse()

    reports = load_reports()

    parts = incoming.strip().split()
    branch_filter, parts2 = parse_branch_suffix(parts)
    cmd = (parts2[0].lower() if parts2 else "").strip()

    # whoami
    if incoming.lower().strip() == "whoami":
        resp.message(f"From: {sender}\nФилиал: {sender_branch or '(не задан)'}")
        return str(resp)

    # help
    if incoming.lower().strip() in ("help", "помощь"):
        resp.message(help_text())
        return str(resp)

    # branch list (today counts)
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
        mm_yyyy = now_local().strftime("%m.%Y")
        rows = filter_by_month(reports, mm_yyyy, branch_name=branch_filter)
        header = f"📅 {mm_yyyy} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # month MM.YYYY [t|k]
    if cmd == "month" and len(parts2) == 2 and re.match(r"^\d{2}\.\d{4}$", parts2[1]):
        mm_yyyy = parts2[1]
        rows = filter_by_month(reports, mm_yyyy, branch_name=branch_filter)
        header = f"📅 {mm_yyyy} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # range DD.MM.YYYY DD.MM.YYYY [t|k]
    if cmd == "range" and len(parts2) == 3 and re.match(r"^\d{2}\.\d{2}\.\d{4}$", parts2[1]) and re.match(r"^\d{2}\.\d{2}\.\d{4}$", parts2[2]):
        d1, d2 = parts2[1], parts2[2]
        rows = filter_by_range(reports, d1, d2, branch_name=branch_filter)
        header = f"📆 {d1}–{d2} — оборот" + (f" | {branch_filter}" if branch_filter else "")
        body = summarize_turnover(rows)
        resp.message(header + ("\n\n" + body if body else "\n\nОтчётов нет."))
        return str(resp)

    # refunds/expenses/withdrawals day|month ...
    if cmd in ("refunds", "expenses", "withdrawals") and len(parts2) >= 3:
        period = parts2[1].lower()
        arg = parts2[2]

        if period == "day":
            if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", arg):
                resp.message(f"❌ Формат: {cmd} day 06.02.2026 [t|k]")
                return str(resp)
            rows = filter_by_date(reports, arg, branch_name=branch_filter)
            title = {
                "refunds": f"↩️ Возвраты за {arg}",
                "expenses": f"🧾 Закуп за {arg}",
                "withdrawals": f"💸 Изъятие за {arg}",
            }[cmd]
        elif period == "month":
            if not re.match(r"^\d{2}\.\d{4}$", arg):
                resp.message(f"❌ Формат: {cmd} month 02.2026 [t|k]")
                return str(resp)
            rows = filter_by_month(reports, arg, branch_name=branch_filter)
            title = {
                "refunds": f"↩️ Возвраты за {arg}",
                "expenses": f"🧾 Закуп за {arg}",
                "withdrawals": f"💸 Изъятие за {arg}",
            }[cmd]
        else:
            resp.message(f"❌ Формат: {cmd} day ... или {cmd} month ...")
            return str(resp)

        title += (f" | {branch_filter}" if branch_filter else "")
        field = {"refunds": "refunds", "expenses": "expenses", "withdrawals": "withdrawals"}[cmd]
        body = summarize_money_lines(rows, field, title)
        resp.message(body if body else (title + "\n\nНет данных."))
        return str(resp)

    # === REPORT INTAKE ===
    if REQUIRE_KNOWN_ADMIN and not sender_branch:
        resp.message("⛔ У вас нет доступа к отправке отчётов. (номер не зарегистрирован)")
        return str(resp)

    blocks = split_into_reports(incoming)
    parsed = []
    for b in blocks:
        data, err = parse_one_report(b)
        if err:
            resp.message("❌ " + err + "\n\nНапиши: help — список команд.")
            return str(resp)
        data["branch"] = sender_branch or "(филиал не задан)"
        parsed.append(data)

    ts = datetime.now(timezone.utc).isoformat()
    for d in parsed:
        d["ts_utc"] = ts
        d["from"] = sender
        reports.append(d)

    save_reports(reports)

    lines = [f"✅ Принял отчёты: {len(parsed)} шт."]
    warnings_all = []
    for i, d in enumerate(parsed, 1):
        lines.append(
            f"{i}) {d.get('branch')} | {d['date']} — {d['shift']}"
            + (f" ({d['employee']})" if d.get("employee") else "")
            + (f": оборот {fmt_money(d.get('total'))}" if d.get("total") is not None else "")
        )
        if d.get("refunds_sum"):
            lines.append(f"   ↩️ Возврат: {fmt_money(d.get('refunds_sum'))}")
        if d.get("expenses_sum"):
            lines.append(f"   🧾 Закуп: {fmt_money(d.get('expenses_sum'))}")
        if d.get("withdrawals_sum"):
            lines.append(f"   💸 Изъятие: {fmt_money(d.get('withdrawals_sum'))}")

        warnings_all.extend(validate_report(d))

    if warnings_all:
        lines.append("\n" + "\n".join(warnings_all))

    resp.message("\n".join(lines))
    return str(resp)

@app.route("/")
def health():
    return "OK"

@app.route("/reports", methods=["GET"])
def get_reports():
    # later protect with token
    return {"reports": load_reports()}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
