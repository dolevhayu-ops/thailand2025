# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Chatbot: Twilio + Flask + OpenAI (+ Flight Search intents + File Recall)
- /                    : בדיקת חיות
- /health              : בדיקת חיות
- /test/openai         : בדיקת חיבור ל-OpenAI
- /upload              : העלאת קובץ (POST, multipart/form-data)
- /files/<id>          : הגשת קובץ שהועלה (לשיתוף עם WhatsApp)
- /twilio/webhook      : Webhook ל־Twilio WhatsApp (POST)

Env (Render → Environment):
OPENAI_API_KEY           : חובה
OPENAI_MODEL             : ברירת מחדל gpt-4o-mini
SYSTEM_PROMPT            : אופציונלי
VERIFY_TWILIO_SIGNATURE  : 'true' כדי לאמת חתימה (ברירת מחדל: 'false')
TWILIO_AUTH_TOKEN        : חובה אם VERIFY_TWILIO_SIGNATURE=true
LOG_LEVEL                : INFO/DEBUG (ברירת מחדל: INFO)
BASE_PUBLIC_URL          : אופציונלי. אם לא קיים נשתמש ב-request.host_url בזמן ריצה.

Start command (Render):
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2
"""

import os
import re
import time
import uuid
import sqlite3
import logging
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional

from flask import Flask, request, abort, send_file, jsonify, g
from werkzeug.utils import secure_filename
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# OpenAI SDK (v1.x)
from openai import OpenAI
import openai  # for RateLimitError

# ----------------------------------------------------
# קונפיג ולוגים
# ----------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a concise, helpful WhatsApp assistant. "
    "Answer in the user's language. Keep it brief, structured, and practical. "
    "If the message is a command like /help or /reset, follow it."
)
VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL")  # למשל: https://thailand2025.onrender.com

# יצירת לקוח OpenAI (אופציונלי: ORG/PROJECT)
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    logger.error("OPENAI_API_KEY is missing!")
    raise RuntimeError("OPENAI_API_KEY is not set")
client = OpenAI(api_key=api_key)

app = Flask(__name__)

# ----------------------------------------------------
# אחסון קבצים פשוט: תיקייה + SQLite
# ----------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

DB_PATH = os.path.join(BASE_DIR, "data.sqlite3")

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            waid TEXT,
            filename TEXT,
            content_type TEXT,
            path TEXT,
            title TEXT,
            tags TEXT,
            uploaded_at TEXT
        )
        """
    )
    db.commit()

with app.app_context():
    init_db()

# ----------------------------------------------------
# זיכרון שיחה + אנטי-ספאם
# ----------------------------------------------------
chat_histories: Dict[str, List[dict]] = defaultdict(list)
TWILIO_SAFE_CHUNK = 1500
last_user_ts: Dict[str, float] = {}
USER_COOLDOWN_SEC = 1.5

def too_fast(waid: str) -> bool:
    now = time.time()
    last = last_user_ts.get(waid, 0.0)
    if now - last < USER_COOLDOWN_SEC:
        return True
    last_user_ts[waid] = now
    return False

# ----------------------------------------------------
# כלים קטנים
# ----------------------------------------------------
def chunk_text(s: str, size: int = TWILIO_SAFE_CHUNK) -> List[str]:
    s = s or ""
    return [s[i:i+size] for i in range(0, len(s), size)] or [""]

def build_messages(history: List[dict], user_text: str) -> List[dict]:
    trimmed = history[-8:] if len(history) > 8 else history[:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(trimmed)
    messages.append({"role": "user", "content": user_text})
    return messages

def _validated_twilio_request() -> bool:
    if not VERIFY_TWILIO_SIGNATURE:
        return True
    if not TWILIO_AUTH_TOKEN:
        logger.warning("VERIFY_TWILIO_SIGNATURE=true אבל חסר TWILIO_AUTH_TOKEN")
        return False

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    url = request.url
    xf_proto = request.headers.get("X-Forwarded-Proto", "")
    if xf_proto == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    signature = request.headers.get("X-Twilio-Signature", "")
    form = request.form.to_dict(flat=True)
    is_valid = validator.validate(url, form, signature)
    if not is_valid:
        logger.warning("Twilio signature validation FAILED")
    return is_valid

def public_base_url() -> str:
    if BASE_PUBLIC_URL:
        return BASE_PUBLIC_URL.rstrip("/") + "/"
    return request.host_url  # דורש שהשרת יהיה ציבורי

# ----------------------------------------------------
# Intent detection (פשוט, תומך עברית/אנגלית)
# ----------------------------------------------------
FLIGHT_WORDS = [
    "flight", "flights", "טיסה", "טיסות", "כרטיס טיסה", "הזמנת טיסה", "find flight", "book flight",
]
SEND_FILE_WORDS = [
    "send", "שלח", "תשלח", "להחזיר קובץ", "קובץ", "pdf", "הכרטיס", "ticket", "boarding", "כרטיס טיסה",
]

CITY_MAP = {
    # יעדים נפוצים בתאילנד (אפשר להרחיב)
    "בנגקוק": "BKK", "bangkok": "BKK",
    "פוקט": "HKT", "phuket": "HKT",
    "เชียงใหม่": "CNX", "chiang mai": "CNX", "צ'יאנג מאי": "CNX", "צ׳יאנג מאי": "CNX",
    "קוסמוי": "USM", "koh samui": "USM", "סמוי": "USM",
    "קראבי": "KBV", "krabi": "KBV",
    # מוצא נפוץ
    "תל אביב": "TLV", "tel aviv": "TLV", "נתבג": "TLV", "נתב\"ג": "TLV", "israel": "TLV",
}

DATE_PATTERNS = [
    # 2025-09-15
    (re.compile(r"(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})"), "%Y-%m-%d"),
    # 15/09/2025 או 15.09.2025
    (re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})"), "%d-%m-%Y"),
]

def detect_intent(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in FLIGHT_WORDS):
        return "flight_search"
    if any(w in t for w in SEND_FILE_WORDS):
        return "recall_file"
    return "general"

def extract_airports(text: str) -> Dict[str, Optional[str]]:
    t = (text or "").lower()
    origin, dest = None, None
    # חיפוש IATA ישיר (3 אותיות)
    m = re.findall(r"\b([a-z]{3})\b", t)
    if m:
        # אם מופיע אחד → נניח שזה יעד והמקור TLV; אם שניים → הראשון מקור, השני יעד
        if len(m) >= 2:
            origin, dest = m[0].upper(), m[1].upper()
        else:
            origin, dest = "TLV", m[0].upper()
    # מפה של שמות לערי תעופה
    for name, code in CITY_MAP.items():
        if name in t:
            if not origin:
                origin = code
            elif not dest and code != origin:
                dest = code
    # ברירת מחדל מקור TLV אם יש יעד בלבד
    if dest and not origin:
        origin = "TLV"
    return {"origin": origin, "dest": dest}

def extract_dates(text: str) -> Dict[str, Optional[str]]:
    t = text or ""
    # נחפש תאריך יציאה בסיסי
    for rgx, fmt in DATE_PATTERNS:
        m = rgx.search(t)
        if m:
            try:
                if fmt == "%Y-%m-%d":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                dt = datetime(y, mo, d)
                out = dt.strftime("%Y-%m-%d")
                return {"depart": out, "return": None}
            except Exception:
                pass
    return {"depart": None, "return": None}

def build_flight_links(origin: str, dest: str, depart: Optional[str]) -> List[str]:
    links = []
    # Google Flights (הפורמט רופף; אם אין תאריך—נכנסים למסך בחירה)
    if origin and dest and depart:
        g = f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}%20on%20{depart}"
    elif origin and dest:
        g = f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}"
    else:
        g = "https://www.google.com/travel/flights"
    links.append(g)

    # Kayak query
    if origin and dest and depart:
        k = f"https://www.kayak.com/flights/{origin}-{dest}/{depart}?sort=bestflight_a"
    elif origin and dest:
        k = f"https://www.kayak.com/flights/{origin}-{dest}"
    else:
        k = "https://www.kayak.com/flights"
    links.append(k)
    return links

# ----------------------------------------------------
# ראוטים
# ----------------------------------------------------
@app.route("/", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET"])
def health():
    return "Your service is live 🎉", 200

@app.route("/test/openai", methods=["GET"])
def test_openai():
    try:
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0,
            timeout=30,
        )
        txt = (r.choices[0].message.content or "").strip()
        return f"OK: {txt}", 200
    except Exception as e:
        logger.exception("OpenAI test endpoint failed: %s", e)
        return f"OpenAI error: {e}", 500

@app.route("/upload", methods=["POST"])
def upload():
    """
    העלאת קובץ: form-data: file=<file>, waid=<user wa id>, title=<optional>, tags=<optional>
    דוגמה ב-curl:
    curl -F "waid=whatsapp:+9725xxxxxx" -F "title=flight ticket" -F "tags=ticket,flight,pdf" -F "file=@/path/ticket.pdf" https://<app>/upload
    """
    init_db()
    f = request.files.get("file")
    waid = request.form.get("waid") or ""
    title = request.form.get("title") or ""
    tags = request.form.get("tags") or ""
    if not f or not waid:
        return jsonify({"ok": False, "error": "missing file or waid"}), 400

    fname = secure_filename(f.filename or f"upload-{uuid.uuid4().hex}")
    ext = os.path.splitext(fname)[1].lower()
    file_id = uuid.uuid4().hex
    stored_name = f"{file_id}{ext}"
    save_path = os.path.join(STORAGE_DIR, stored_name)
    f.save(save_path)

    content_type = f.mimetype or "application/octet-stream"
    db = get_db()
    db.execute(
        "INSERT INTO files (id, waid, filename, content_type, path, title, tags, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        (file_id, waid, fname, content_type, save_path, title, tags, datetime.utcnow().isoformat()),
    )
    db.commit()

    url = public_base_url() + f"files/{file_id}"
    return jsonify({"ok": True, "file_id": file_id, "url": url})

@app.route("/files/<file_id>", methods=["GET"])
def serve_file(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row:
        abort(404)
    return send_file(row["path"], mimetype=row["content_type"], as_attachment=False, download_name=row["filename"])

def handle_commands(body: str, waid: str) -> Optional[str]:
    cmd = (body or "").strip().lower()
    if cmd in ("/reset", "reset", "/restart"):
        chat_histories.pop(waid, None)
        return "✅ השיחה אופסה. תוכל להתחיל נושא חדש."
    if cmd in ("/help", "help"):
        return (
            "ℹ️ אני יודע: \n"
            "• חיפוש טיסות: כתוב 'תמצא לי טיסה ל…' (אפשר להוסיף תאריך 2025-09-12)\n"
            "• שליחת קובץ שהעלית: כתוב 'שלח לי את הכרטיס' או 'תשלח את ה-PDF'\n"
            "• /reset לאיפוס שיחה"
        )
    return None

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    if not _validated_twilio_request():
        abort(403)

    # שדות מטוויליו
    from_ = request.form.get("From", "")
    waid = request.form.get("WaId", from_)
    body = request.form.get("Body", "") or ""
    num_media = int(request.form.get("NumMedia", "0") or 0)

    latitude = request.form.get("Latitude")
    longitude = request.form.get("Longitude")
    address = request.form.get("Address")
    label = request.form.get("Label")

    resp = MessagingResponse()

    if too_fast(waid):
        resp.message("⏳ מעבד הודעות… שלח שוב בעוד כשנייה.")
        return str(resp)

    # פקודות מהירות
    cmd_reply = handle_commands(body, waid)
    if cmd_reply:
        for chunk in chunk_text(cmd_reply):
            resp.message(chunk)
        return str(resp)

    # מדיה נכנסת — כרגע לא מנתחים (אפשר להרחיב בהמשך)
    if num_media > 0:
        resp.message("📎 קיבלתי קובץ/תמונה. כרגע אני מטפל בטקסט בלבד. לניתוח קובץ העלה דרך /upload.")
        # נמשיך גם עם טקסט אם יש

    # חיבור טקסט עם לוקיישן אם סופק
    user_text = body.strip()
    if latitude and longitude:
        loc = f"[user shared location] lat={latitude}, lon={longitude}"
        user_text = f"{user_text}\n\n{loc}" if user_text else loc

    if not user_text:
        resp.message("👋 שלח לי בקשה, למשל: 'תמצא לי טיסה לפוקט ב-2025-09-12' או '/help'.")
        return str(resp)

    # Intent Routing
    intent = detect_intent(user_text)

    if intent == "flight_search":
        # חילוץ מוצא/יעד/תאריך
        parsed = extract_airports(user_text)
        dates = extract_dates(user_text)
        origin, dest = parsed["origin"], parsed["dest"]
        depart = dates["depart"]

        if not dest:
            resp.message("✈️ כדי שאחפש טיסות — כתוב יעד (למשל: פוקט/צ'יאנג מאי) ואפשר גם תאריך בפורמט YYYY-MM-DD.")
            return str(resp)

        links = build_flight_links(origin, dest, depart)
        origin_txt = origin or "בחר מוצא"
        date_txt = depart or "בחר תאריך"
        msg = (
            f"✈️ חיפוש טיסות {origin_txt} → {dest}\n"
            f"תאריך יציאה: {date_txt}\n"
            f"Google Flights: {links[0]}\n"
            f"Kayak: {links[1]}"
        )
        for ch in chunk_text(msg):
            resp.message(ch)
        return str(resp)

    if intent == "recall_file":
        # שולף את הקובץ האחרון של המשתמש עם תגיות/כותרת שקשורות לכרטיס/טיסה/טיקט
        db = get_db()
        row = db.execute(
            """
            SELECT * FROM files
            WHERE waid=?
              AND (
                    LOWER(IFNULL(tags,'')) LIKE '%ticket%' OR
                    LOWER(IFNULL(tags,'')) LIKE '%flight%' OR
                    LOWER(IFNULL(tags,'')) LIKE '%pdf%' OR
                    LOWER(IFNULL(title,'')) LIKE '%ticket%' OR
                    LOWER(IFNULL(title,'')) LIKE '%flight%' OR
                    LOWER(IFNULL(title,'')) LIKE '%כרטיס%'
                  )
            ORDER BY uploaded_at DESC
            LIMIT 1
            """, (waid,)
        ).fetchone()

        if not row:
            # אם לא נמצא — ננסה את האחרון בכלל של המשתמש
            row = db.execute(
                "SELECT * FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT 1",
                (waid,)
            ).fetchone()

        if not row:
            resp.message("לא מצאתי קובץ שהעלית. תוכל להעלות דרך /upload (waid + file) ולתייג 'ticket,flight,pdf'.")
            return str(resp)

        file_url = public_base_url() + f"files/{row['id']}"
        msg = f"📄 שולח לך את הקובץ האחרון המתאים: {row['filename']}"
        m = resp.message(msg)
        # הוספת מדיה (Twilio שולף את ה-URL מהשרת שלנו)
        m.media(file_url)
        return str(resp)

    # Intent כללי → ננסה GPT, עם Fallback Echo
    history = chat_histories[waid]
    try:
        messages = build_messages(history, user_text)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.4,
            timeout=30,
        )
        answer = (completion.choices[0].message.content or "").strip()
        if not answer:
            answer = "לא הצלחתי לנסח תשובה כרגע."
    except openai.RateLimitError as e:
        logger.exception("OpenAI rate limit / quota error: %s", e)
        answer = (
            "⚠️ כרגע יש מגבלת שימוש מול OpenAI. אפשר לנסות שוב בעוד רגע. "
            f"בינתיים: Echo: {user_text[:280]}"
        )
    except Exception as e:
        logger.exception("OpenAI error while answering WhatsApp: %s", e)
        answer = f"Echo (fallback): {user_text[:300]}"

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        del history[:-20]

    for i, ch in enumerate(chunk_text(answer)):
        if i > 0:
            ch = f"{ch}\n\n({i+1})"
        resp.message(ch)
    return str(resp)

# ----------------------------------------------------
# הרצה מקומית (נוח לפיתוח)
# ----------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
