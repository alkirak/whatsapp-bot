from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os, json, re
from datetime import datetime, timezone

app = Flask(__name__)
REPORTS_FILE = "reports.json"

# Номера админов -> филиалы (Twilio формат: whatsapp:+7XXXXXXXXXX)
ADMIN_BRANCH = {
    "whatsapp:+77070610093": "Polygon Turkistan",
    "whatsapp:+77081474845": "Polygon Kentau",
    "whatsapp:+77089273230": "Polygon Turkistan",
}

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
    # "63 970" -> 63970
    return int(re.sub(r"[^\d]", "", s))

def extract_money_after(labels, text: str):
    """
    labels: list[str] possible label spellings
    Finds "label: 12345"
    """
    for label in labels:
        m = re.search(rf"{label}\s*:\s*([0-9\s]+)", text, re.IGNORECASE)
        if m:
            return norm_num(m.group(1))
    return None

def extract_shift_header(block: str):
    """
    Examples:
    "06.02.2026г. Ночь смена Уля"
    "06.02.2026 День смена Ару"
    """
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
    """
    Splits message into multiple report blocks by finding repeated headers.
    """
    t = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    header_re = re.compile(
        r"(?=(\d{2}\.\d{2}\.\d{4}).{0,40}\b(ночь|ночная|день|дневная)\b)",
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
    cash = extract_money_after(["Нал", "Наличка", "Наличные"], block)  # первое "Нал" = общая касса нал
    kaspi = extract_money_after(["Каспи", "Kaspi", "KASPI"], block)     # первое "Каспи" = общая касса каспи

    drinks_total = extract_money_after(["Напитки", "Бар", "Напиток"], block)

    # Drinks cash/kaspi only within drinks section
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

    # Withdrawals
    withdrawals = []
    w = re.search(r"Изъятие\s*:\s*(.+?)(Остаток|$)", block, re.IGNORECASE | re.DOTALL)
    if w:
        raw = w.group(1).strip()
        raw = " ".join(raw.split())
        parts = re.split(r"\s*,\s*|\n", raw)
        if len(parts) == 1:
            parts = re.split(r"\s+(?=[A-Za-zА-Яа-яЁё]+-)", raw)
        withdrawals = [p.strip() for p in parts if p.strip()]

    # Remainder
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

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming = (request.values.get("Body", "") or "").strip()
    sender = request.values.get("From", "")  # e.g. 'whatsapp:+7707...'
    branch = ADMIN_BRANCH.get(sender)

    resp = MessagingResponse()

    # Команда, чтобы увидеть как Twilio видит номер
    if incoming.lower().strip() == "whoami":
        resp.message(f"From: {sender}\nФилиал: {branch or '(не задан)'}")
        return str(resp)

    if incoming.lower().strip() in ("help", "помощь"):
        resp.message(
            "Отправляйте отчёт как обычно, можно даже два отчёта одним сообщением.\n\n"
            "Пример:\n"
            "06.02.2026г. Ночь смена Уля\n"
            "Общая касса: 83450\nНал: 14030\nКаспи: 63970\n\n"
            "Напитки: 5450\nНал: 1600\nКаспи: 3850\n\n"
            "Изъятие: Уля-ЗП-7000\nАлишер-60000(нал.)\n"
            "Остаток: 5500+6480(мел.)"
        )
        return str(resp)

    blocks = split_into_reports(incoming)
    parsed = []
    for b in blocks:
        data, err = parse_one_report(b)
        if err:
            resp.message("❌ " + err + "\n\nНапиши: help — покажу пример.")
            return str(resp)
        data["branch"] = branch
        parsed.append(data)

    reports = load_reports()
    ts = datetime.now(timezone.utc).isoformat()
    for d in parsed:
        d["ts_utc"] = ts
        d["from"] = sender
        reports.append(d)
    save_reports(reports)

    lines = [f"✅ Принял отчёты: {len(parsed)} шт."]
    for i, d in enumerate(parsed, 1):
        lines.append(
            f"{i}) {d.get('branch','(филиал не задан)')} | {d['date']} — {d['shift']}"
            + (f" ({d['employee']})" if d.get("employee") else "")
            + (f": общая {d['total']}" if d.get("total") is not None else "")
            + (f", нал {d['cash']}" if d.get("cash") is not None else "")
            + (f", каспи {d['kaspi']}" if d.get("kaspi") is not None else "")
        )

    resp.message("\n".join(lines))
    return str(resp)

@app.route("/")
def health():
    return "OK"

@app.route("/reports", methods=["GET"])
def get_reports():
    return {"reports": load_reports()}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

