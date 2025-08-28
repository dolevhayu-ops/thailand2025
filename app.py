# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Chatbot: Twilio + Flask + OpenAI
Capabilities:
- שיחה חכמה (GPT)
- חיפוש טיסות (intent: "flight_search")
- שמירת קבצים מהוואטסאפ אוטומטית (MediaUrl0..) + שליחה חוזרת של קובץ ("recall_file")
- API להעלאת קובץ גם דרך /upload
- הגשת קבצים ציבורית /files/<id> עבור שליחה ב-WhatsApp

Routes:
- /                    : בדיקת חיות
- /health              : בדיקת חיות
- /test/openai         : בדיקת חיבור ל-OpenAI
- /upload              : העלאת קובץ (POST, multipart/form-data)
- /files/<id>          : הגשת קובץ ששמור בשרת
- /twilio/webhook      : Webhook ל־Twilio WhatsApp (POST)

Env (Render → Environment):
OPENAI_API_KEY           : חובה
OPENAI_MODEL             : ברירת מחדל gpt-4o-mini
SYSTEM_PROMPT            : אופציונלי
VERIFY_TWILIO_SIGNATURE  : 'true' כדי לאמת חתימה (ברירת מחדל: 'false')
TWILIO_AUTH_TOKEN        : חובה אם VERIFY_TWILIO_SIGNATURE=true או לשאיבת מדיה מטוויליו
TWILIO_ACCOUNT_SID       : חובה לשאיבת מדיה מטוויליו (Basic Auth)
BASE_PUBLIC_URL          : מומלץ (למשל https://thailand2025.onrender.com)
LOG_LEVEL                : INFO/DEBUG (ברירת מחדל: INFO)

Start command (Render):
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2
"""

import os
import re
import time
import uuid
import sqlite3
import logging
import mimetypes
from datetime import datetime
from urllib.parse import urlparse
from collections import defaultdict
from typing import List, Dict, Optional

import requests
from flask import Flask, request, abort, send_file, jsonify, g
from werkzeug.utils import secure_filename
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

from openai import OpenAI
import openai  # חריגי RateLimit וכו'

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
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL")  # למשל: https://thailand2025.onrender.com

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    logger.error("OPENAI_API_KEY is missing!")
    raise RuntimeError("OPENAI_API_KEY is not set")
client = OpenAI(api_key=api_key)

app = Flask(__name__)

# ----------------------------------------------------
# אחסון קבצים: תיקייה + SQLite
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
# כלים
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
    return request.host_url

def guess_extension(content_type: str, fallback_from_url: str = "") -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type)
        if ext:
            return ext
    # נסה מה-URL
    path = urlparse(fallback_from_url).path
    _, dot, suffix = path.rpartition(".")
    if dot and suffix and len(suffix) <= 5:
        return "." + suffix
    return ".bin"

def save_file_record(waid: str, fname: str, content_type: str, data: bytes, title: str = "", tags: str = "") -> str:
    file_id = uuid.uuid4().hex
    stored_name = file_id + os.path.splitext(fname)[1]
    save_path = os.path.join(STORAGE_DIR, stored_name)
    with open(save_path, "wb") as fp:
        fp.write(data)
    db = get_db()
    db.execute(
        "INSERT INTO files (id, waid, filename, content_type, path, title, tags, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        (file_id, waid, fname, content_type or "application/octet-stream", save_path, title, tags, datetime.utcnow().isoformat()),
    )
    db.commit()
    return file_id

# ----------------------------------------------------
# Intent detection (עברית/אנגלית בסיסי)
# ----------------------------------------------------
FLIGHT_WORDS = [
    "flight", "flights", "טיסה", "טיסות", "כרטיס טיסה", "הזמנת טיסה", "find flight", "book flight",
]
SEND_FILE_WORDS = [
    "send", "שלח", "תשלח", "להחזיר קובץ", "קובץ", "pdf", "הכרטיס", "ticket", "boarding", "כרטיס טיסה",
]

CITY_MAP = {
    "בנגקוק": "BKK", "bangkok": "BKK",
    "פוקט": "HKT", "phuket": "HKT",
    "เชียงใหม่": "CNX", "chiang mai": "CNX", "צ'יאנג מאי": "CNX", "צ׳יאנג מאי": "CNX",
    "קוסמוי": "USM", "koh samui": "USM", "סמוי": "USM",
    "קראבי": "KBV", "krabi": "KBV",
    "תל אביב": "TLV", "tel aviv": "TLV", "נתבג": "TLV", "נתב\"ג": "TLV", "israel": "TLV",
}

DATE_PATTERNS = [
    (re.compile(r"(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})"), "%Y-%m-%d"),
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
    m = re.findall(r"\b([a-z]{3})\b", t)
    if m:
        if len(m) >= 2:
            origin, dest = m[0].upper(), m[1].upper()
        else:
            origin, dest = "TLV", m[0].upper()
    for name, code in CITY_MAP.items():
        if name in t:
            if not origin:
                origin = code
            elif not dest and code != origin:
                dest = code
    if dest and not origin:
        origin = "TLV"
    return {"origin": origin, "dest": dest}

def extract_dates(text: str) -> Dict[str, Optional[str]]:
    t = text or ""
    for rgx, fmt in DATE_PATTERNS:
        m = rgx.search(t)
        if m:
            try:
                if fmt == "%Y-%m-%d":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                dt = datetime(y, mo, d)
                return {"depart": dt.strftime("%Y-%m-%d"), "return": None}
            except Exception:
                pass
    return {"depart": None, "return": None}

def build_flight_links(origin: str, dest: str, depart: Optional[str]) -> List[str]:
    links = []
    if origin and dest and depart:
        g = f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}%20on%20{depart}"
    elif origin and dest:
        g = f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}"
    else:
        g = "https://www.google.com/travel/flights"
    links.append(g)
    if origin and dest and depart:
        k = f"https://www.kayak.com/flights/{origin}-{dest}/{depart}?sort=bestflight_a"
    elif origin and dest:
        k = f"https://www.kayak.com/flights/{origin}-{dest}"
    else:
        k = "https://www.kayak.com/flights"
    links.append(k)
    return links

# ----------------------------------------------------
# Routes
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
    form-data: file=<file>, waid=<user wa id>, title=<optional>, tags=<optional>
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
    if not ext and f.mimetype:
        ext = guess_extension(f.mimetype)
        fname = fname + ext
    file_id = save_file_record(
        waid=waid,
        fname=fname,
        content_type=f.mimetype or "application/octet-stream",
        data=f.read(),
        title=title,
        tags=tags,
    )
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
            "• חיפוש טיסות: 'תמצא לי טיסה ל…' (אפשר גם תאריך 2025-09-12)\n"
            "• שליחת קובץ שהעלית: 'שלח לי את הכרטיס' או 'תשלח את ה-PDF'\n"
            "• שליחת קובץ ישירות פה בוואטסאפ – אני אשמור אוטומטית\n"
            "• /reset לאיפוס שיחה"
        )
    return None

def handle_incoming_media(waid: str, num_media: int, body_text: str) -> List[str]:
    """
    מוריד ושומר את כל המדיה שנשלחה בהודעה זו.
    מחזיר רשימת file_id שנשמרו.
    """
    saved_ids = []
    if not TWILIO_AUTH_TOKEN or not TWILIO_ACCOUNT_SID:
        logger.warning("Media received but TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN missing.")
        return saved_ids

    for i in range(num_media):
        media_url = request.form.get(f"MediaUrl{i}")
        ctype = request.form.get(f"MediaContentType{i}") or "application/octet-stream"
        if not media_url:
            continue
        try:
            # הורדה עם Basic Auth של Twilio
            r = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
            r.raise_for_status()
            # שם קובץ: נגזור מה-URL או לפי content-type
            url_name = os.path.basename(urlparse(media_url).path) or f"media-{uuid.uuid4().hex}"
            ext = os.path.splitext(url_name)[1]
            if not ext:
                ext = guess_extension(ctype, media_url)
                url_name += ext
            fname = secure_filename(url_name)
            title = (body_text or "WhatsApp media").strip()[:80]
            tags = "whatsapp,media,auto"
            file_id = save_file_record(waid, fname, ctype, r.content, title=title, tags=tags)
            saved_ids.append(file_id)
        except Exception as e:
            logger.exception("Failed downloading media from Twilio: %s", e)
    return saved_ids

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    if not _validated_twilio_request():
        abort(403)

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

    # אם יש מדיה – נשמור אותה אוטומטית
    saved_media = []
    if num_media > 0:
        saved_media = handle_incoming_media(waid, num_media, body)
        if saved_media:
            resp.message(f"📎 נשמרו {len(saved_media)} קבצים. כתוב 'שלח לי את הכרטיס' כדי לקבל את האחרון.")
        else:
            resp.message("📎 התקבלה מדיה אך לא הצלחתי לשמור. ודא שהוגדרו TWILIO_ACCOUNT_SID ו-TWILIO_AUTH_TOKEN.")

    # פקודות מהירות
    cmd_reply = handle_commands(body, waid)
    if cmd_reply:
        for chunk in chunk_text(cmd_reply):
            resp.message(chunk)
        return str(resp)

    # חבר טקסט עם לוקיישן אם סופק
    user_text = body.strip()
    if latitude and longitude:
        loc = f"[user shared location] lat={latitude}, lon={longitude}"
        if address or label:
            loc += f" | {label or address}"
        user_text = f"{user_text}\n\n{loc}" if user_text else loc

    if not user_text and saved_media:
        # אם הגיעו רק קבצים בלי טקסט — סיימנו
        return str(resp)
    if not user_text:
        resp.message("👋 שלח לי בקשה, למשל: 'תמצא לי טיסה לפוקט ב-2025-09-12' או '/help'.")
        return str(resp)

    # Intent Routing
    intent = detect_intent(user_text)

    if intent == "flight_search":
        parsed = extract_airports(user_text)
        dates = extract_dates(user_text)
        origin, dest = parsed["origin"], parsed["dest"]
        depart = dates["depart"]
        if not dest:
            resp.message("✈️ כדי שאחפש טיסות — כתוב יעד (למשל: פוקט/צ'יאנג מאי) ואפשר גם תאריך YYYY-MM-DD.")
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
            row = db.execute(
                "SELECT * FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT 1",
                (waid,)
            ).fetchone()
        if not row:
            resp.message("לא מצאתי קובץ שהעלית. שלח קובץ פה בוואטסאפ או העלה דרך /upload.")
            return str(resp)
        file_url = public_base_url() + f"files/{row['id']}"
        msg = f"📄 שולח את: {row['filename']}"
        m = resp.message(msg)
        m.media(file_url)
        return str(resp)

    # שיחה כללית (GPT) עם Fallback
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
        answer = "⚠️ כרגע יש מגבלת שימוש מול OpenAI. נסה שוב מעט מאוחר יותר."
    except Exception as e:
        logger.exception("OpenAI error while answering WhatsApp: %s", e)
        answer = f"Echo (fallback): {user_text[:300]}"

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        del history[:-20]

    for i, ch in enumerate(chunk_text(answer)):
        if i > 1:
            ch = f"{ch}\n\n({i+1})"
        resp.message(ch)
    return str(resp)

# ----------------------------------------------------
# הרצה מקומית
# ----------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
