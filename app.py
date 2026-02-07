from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os, json, re
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
REPORTS_FILE = "reports.json"

# === НАСТРОЙКИ ===
REQUIRE_KNOWN_ADMIN = True  # True = отчёты принимаем только от номеров в ADMIN_BRANCH
TZ_OFFSET_HOURS = 5         # Казахстан часто UTC+5. Если у тебя другой пояс — поменяем.

# Номера админов -> филиалы (Twilio формат: whatsapp:+7XXXXXXXXXX)
ADMIN_BRANCH = {
    "whatsapp:+77070610093": "Polygon Turkistan",
    "whatsapp:+77081474845": "Polygon Kentau",
    "whatsapp:+77089273230": "Polygon Turkistan",  # твой номер (для тестов)
}

# === ХЕЛПЕРЫ ===
def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def today_str():
    return now_local().strftime("%d.%m.%Y")

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

def norm_num(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s))

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
    shift = None
    if shift_raw:
        shift = "Ночь" if "ноч" in shift_raw else "День"

    em = re.search(r"смена\s+([A-Za-zА-Яа-яЁё]+)", block, re.IGNORECASE)
    employee = em.group(1) if em else None

    return date, shift, employee

def split_into_reports(text: str):
    t = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    header_re = re.compile(
        r"(?=(\d{2}\.\d{2}\.\d{4}).{0,60}\b(ночь|ночная|день|дневная)\b)",
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

def parse_one_report(block: str):
    date, shift, employee = extract_shift_header(block)

    total = extract_money_after(["Общая касса", "Касса общая", "Общая"], block)
    cash = extract_money_after(["Нал", "Наличка", "Наличные"], block)
    kaspi = extract_money_after(["Каспи", "Kaspi", "KASPI"], block)

    drinks_total = extract_money_after(["Напитки", "Бар", "Напиток"], block)

    drinks_cash = None
    drinks_kaspi = None
    msec = re.search(
        r"(Напитки|Бар)\s*:\s*[0-9\s]+(.+?)(Изъятие|Остаток|$)",
        block, re.IGNORECASE | re.DOTALL
    )
    if msec:
        sec = msec.group(2)
        m1 = re.search(r"(Нал|Наличка|Наличные)\s*:\s*([0-9\s]+)", sec, re.IGNORECASE)
        m2 = re.search(r"(Каспи|Kaspi|KASPI)\s*:\s*([0-9\s]+)", sec, re.IGNORECASE)
        drinks_cash = norm_num(m1.group(2)) if m1 else None
        drinks_kaspi = norm_num(m2.group(2)) if m2 else None

    withdrawals = []
    w = re.search(r"Изъятие\s*:\s*(.+?)(Остаток|$)", block, re.IGNORECASE | re.DOTALL)
    if w:
        raw = w.group(1).strip()
        raw = " ".join(raw.split())
        parts = re.split(r"\s*,\s*|\n", raw)
        if len(parts) == 1:
            parts = re.split(r"\s+(?=[A-Za-zА-Яа-яЁё]+-)", raw)
        withdrawals = [p.strip() for p in parts if p.strip()]

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
        "withdrawals": withdrawals,
        "remainder": remainder,
        "raw": block
    }
    return data, None

def fmt_money(n):
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")

def validate_report(r):
    warnings = []

    total = r.get("total")
    cash = r.get("cash")
    kaspi = r.get("kaspi")
    drinks_total = r.get("drinks_total")

    # --- вытаскиваем суммы "возврат" из текста изъятий (если есть) ---
    # Примеры: "Возврат-1500(каспи)" / "возврат 1500" / "Возврат: 1500"
    refund_sum = 0
    raw_withdrawals = " ".join(r.get("withdrawals", []) or [])
    for m in re.finditer(r"(возврат)\s*[-: ]\s*([0-9\s]+)", raw_withdrawals, re.IGNORECASE):
        refund_sum += norm_num(m.group(2))

    # 1) Проверка общей кассы
    if total is not None and cash is not None and kaspi is not None and drinks_total is not None:
        base = cash + kaspi + drinks_total

        if total == base:
            pass  # всё ок
        else:
            # если есть возвраты — попробуем объяснить расхождение
            if refund_sum > 0:
                if total == base + refund_sum:
                    warnings.append(
                        f"ℹ️ Общая касса сходится, если 'возврат' учитывать как +{refund_sum}: {total} = {cash}+{kaspi}+{drinks_total}+{refund_sum}"
                    )
                elif total == base - refund_sum:
                    warnings.append(
                        f"ℹ️ Общая касса сходится, если 'возврат' учитывать как -{refund_sum}: {total} = {cash}+{kaspi}+{drinks_total}-{refund_sum}"
                    )
                else:
                    warnings.append(
                        f"⚠️ Общая касса не сходится: {total} ≠ {cash}+{kaspi}+{drinks_total} (нал+каспи+напитки). "
                        f"Возврат найден: {refund_sum}"
                    )
            else:
                warnings.append(
                    f"⚠️ Общая касса не сходится: {total} ≠ {cash}+{kaspi}+{drinks_total} (нал+каспи+напитки)"
                )

    elif total is not None and cash is not None and kaspi is not None:
        # fallback если напитки не указаны
        base = cash + kaspi
        if total != base:
            warnings.append(f"⚠️ Общая касса не сходится: {total} ≠ {cash}+{kaspi}")

    # 2) Проверка напитков по разбиению нал/каспи в секции напитков
    dc = r.get("drinks_cash")
    dk = r.get("drinks_kaspi")
    if drinks_total is not None and dc is not None and dk is not None:
        if drinks_total != (dc + dk):
            warnings.append(f"⚠️ Напитки не сходятся: {drinks_total} ≠ {dc}+{dk}")

    return warnings



def summarize_for_date(reports, date_str):
    rows = [r for r in reports if r.get("date") == date_str and r.get("total") is not None]
    if not rows:
        return f"📊 {date_str}: отчётов нет."

    by = {}
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        sh = r.get("shift") or "?"
        by.setdefault(br, {})
        by[br][sh] = r  # последний по смене

    lines = [f"📊 Сводка за {date_str}"]
    grand_total = 0

    for br in sorted(by.keys()):
        night = by[br].get("Ночь")
        day = by[br].get("День")
        br_sum = 0

        lines.append(f"\n🏢 {br}")
        if night:
            br_sum += night.get("total", 0) or 0
            lines.append(f"  🌙 Ночь: {fmt_money(night.get('total'))}")
        else:
            lines.append("  🌙 Ночь: —")

        if day:
            br_sum += day.get("total", 0) or 0
            lines.append(f"  ☀️ День: {fmt_money(day.get('total'))}")
        else:
            lines.append("  ☀️ День: —")

        lines.append(f"  ✅ Итого филиал: {fmt_money(br_sum)}")
        grand_total += br_sum

    lines.append(f"\n💰 Общий итог: {fmt_money(grand_total)}")
    return "\n".join(lines)

def summarize_month(reports, mm_yyyy: str):
    # mm_yyyy = "02.2026"
    rows = [r for r in reports if r.get("date", "").endswith(mm_yyyy) and r.get("total") is not None]
    if not rows:
        return f"📅 Месяц {mm_yyyy}: отчётов нет."

    by = {}  # branch -> shift -> sum
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        sh = r.get("shift") or "?"
        by.setdefault(br, {"Ночь": 0, "День": 0, "?": 0})
        by[br][sh] = by[br].get(sh, 0) + (r.get("total") or 0)

    lines = [f"📅 Месячный отчёт: {mm_yyyy}"]
    grand_total = 0
    for br in sorted(by.keys()):
        night = by[br].get("Ночь", 0)
        day = by[br].get("День", 0)
        br_sum = night + day + by[br].get("?", 0)

        lines.append(f"\n🏢 {br}")
        lines.append(f"  🌙 Ночь: {fmt_money(night)}")
        lines.append(f"  ☀️ День: {fmt_money(day)}")
        if by[br].get("?", 0):
            lines.append(f"  ❓ Неизв.: {fmt_money(by[br].get('?', 0))}")
        lines.append(f"  ✅ Итого: {fmt_money(br_sum)}")
        grand_total += br_sum

    lines.append(f"\n💰 Общий итог: {fmt_money(grand_total)}")
    return "\n".join(lines)

def summarize_range(reports, d1, d2):
    # inclusive range
    def parse_d(s):
        return datetime.strptime(s, "%d.%m.%Y").date()
    try:
        a = parse_d(d1)
        b = parse_d(d2)
    except Exception:
        return "❌ Формат диапазона: range 01.02.2026 07.02.2026"

    if b < a:
        a, b = b, a

    rows = []
    for r in reports:
        ds = r.get("date")
        if not ds:
            continue
        try:
            rd = parse_d(ds)
        except Exception:
            continue
        if a <= rd <= b and r.get("total") is not None:
            rows.append(r)

    if not rows:
        return f"📆 Диапазон {d1}–{d2}: отчётов нет."

    # Sum totals by branch
    by = {}
    for r in rows:
        br = r.get("branch") or "(филиал не задан)"
        by.setdefault(br, 0)
        by[br] += (r.get("total") or 0)

    lines = [f"📆 Отчёт за период {d1}–{d2} (общая касса)"]
    grand = 0
    for br in sorted(by.keys()):
        lines.append(f"🏢 {br}: {fmt_money(by[br])}")
        grand += by[br]
    lines.append(f"\n💰 Общий итог: {fmt_money(grand)}")
    return "\n".join(lines)

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

# === ROUTES ===
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming = (request.values.get("Body", "") or "").strip()
    sender = request.values.get("From", "")
    branch = ADMIN_BRANCH.get(sender)
    resp = MessagingResponse()

    low = incoming.lower().strip()

    # whoami
    if low == "whoami":
        resp.message(f"From: {sender}\nФилиал: {branch or '(не задан)'}")
        return str(resp)

    # help
    if low in ("help", "помощь"):
        resp.message(
            "Команды:\n"
            "today / сегодня — сводка за сегодня по всем филиалам\n"
            "day 06.02.2026 — сводка за дату\n"
            "month / месяц — отчёт за текущий месяц\n"
            "month 02.2026 — отчёт за месяц\n"
            "range 01.02.2026 07.02.2026 — отчёт за период\n"
            "branch — список филиалов и отчётов за сегодня\n"
            "whoami — показать твой номер как видит Twilio\n\n"
            "Отчёт отправляйте как обычно (можно 2 смены одним сообщением)."
        )
        return str(resp)

    reports = load_reports()

    # today
    if low in ("today", "сегодня"):
        resp.message(summarize_for_date(reports, today_str()))
        return str(resp)

    # day DD.MM.YYYY
    m = re.match(r"^day\s+(\d{2}\.\d{2}\.\d{4})$", low)
    if m:
        resp.message(summarize_for_date(reports, m.group(1)))
        return str(resp)

    # month / месяц or month MM.YYYY
    if low in ("month", "месяц"):
        mm_yyyy = now_local().strftime("%m.%Y")
        resp.message(summarize_month(reports, mm_yyyy))
        return str(resp)

    m = re.match(r"^month\s+(\d{2}\.\d{4})$", low)
    if m:
        resp.message(summarize_month(reports, m.group(1)))
        return str(resp)

    # range DD.MM.YYYY DD.MM.YYYY
    m = re.match(r"^range\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})$", low)
    if m:
        resp.message(summarize_range(reports, m.group(1), m.group(2)))
        return str(resp)

    # branch
    if low == "branch":
        resp.message(list_branches_today(reports))
        return str(resp)

    # === Report intake ===
    if REQUIRE_KNOWN_ADMIN and not branch:
        resp.message("⛔ У вас нет доступа к отправке отчётов. (номер не зарегистрирован)")
        return str(resp)

    blocks = split_into_reports(incoming)
    parsed = []
    for b in blocks:
        data, err = parse_one_report(b)
        if err:
            resp.message("❌ " + err + "\n\nНапиши: help — список команд и пример.")
            return str(resp)
        data["branch"] = branch or "(филиал не задан)"
        parsed.append(data)

    ts = datetime.now(timezone.utc).isoformat()
    for d in parsed:
        d["ts_utc"] = ts
        d["from"] = sender
        reports.append(d)

    save_reports(reports)

    # Reply summary + warnings
    lines = [f"✅ Принял отчёты: {len(parsed)} шт."]
    warnings_all = []
    for i, d in enumerate(parsed, 1):
        lines.append(
            f"{i}) {d.get('branch')} | {d['date']} — {d['shift']}"
            + (f" ({d['employee']})" if d.get("employee") else "")
            + (f": общая {fmt_money(d.get('total'))}" if d.get("total") is not None else "")
            + (f", нал {fmt_money(d.get('cash'))}" if d.get("cash") is not None else "")
            + (f", каспи {fmt_money(d.get('kaspi'))}" if d.get("kaspi") is not None else "")
        )
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
    # позже закроем паролем/токеном
    return {"reports": load_reports()}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
