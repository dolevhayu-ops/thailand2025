# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Travel Assistant – Flask + Twilio + OpenAI + SQLite + ICS + Cron + Google Calendar OAuth + Vision

יכולות:
- שיחה חופשית (GPT כשזמין; אחרת Fallback)
- קבלת מדיה בוואטסאפ, שמירה וניתוח (PDF/תמונה) → Flights/Hotels
- שליחה חוזרת של קבצים ("שלח לי את הכרטיס")
- חיפוש טיסות (לינקים)
- פיד ICS אישי: /calendar/<WaId>.ics
- Cron יומי/שבועי לוואטסאפ (תזכורות ודוח)
- Google Calendar OAuth: הוספת אירועים אוטומטית ליומן
- Vision לתמונות: חילוץ פרטי טיסה/מלון מתמונה
- המלצות לפי עיר + קטגוריה

ENV (Render → Environment):
OPENAI_API_KEY
OPENAI_MODEL                (default: gpt-4o-mini)
SYSTEM_PROMPT               (optional)
VERIFY_TWILIO_SIGNATURE     ('false' default)
TWILIO_AUTH_TOKEN           (נדרש לאימות חתימה ולהורדת מדיה)
TWILIO_ACCOUNT_SID          (נדרש להורדת מדיה)
TWILIO_WHATSAPP_FROM        ('whatsapp:+1415...' או מספר וואטסאפ פעיל) או TWILIO_MESSAGING_SERVICE_SID
BASE_PUBLIC_URL             (e.g. https://thailand2025.onrender.com)
CRON_SECRET                 (סיסמה ל-/cron/*)
TZ                          (e.g. Asia/Jerusalem)

# Google OAuth (Calendar)
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_OAUTH_REDIRECT_URI   (e.g. https://<domain>/google/oauth/callback)

Start command (Render):
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2
"""

import os, re, time, uuid, sqlite3, logging, json, mimetypes
from datetime import datetime, timedelta
from urllib.parse import urlparse
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import requests
from flask import Flask, request, abort, send_file, jsonify, g, Response, redirect
from werkzeug.utils import secure_filename
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

# OpenAI
from openai import OpenAI
import openai

# Google Calendar OAuth
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ------------------------- קונפיג ולוגים -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a concise, helpful WhatsApp travel assistant. "
    "Answer in the user's language. Be brief, structured, and practical."
)
VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL")
CRON_SECRET = os.getenv("CRON_SECRET", "changeme")
TZ = os.getenv("TZ", "UTC")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

api_key = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=api_key) if api_key else None

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def tz_now():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(TZ))
        except Exception:
            pass
    return datetime.utcnow()

app = Flask(__name__)

# ------------------------- אחסון/DB -------------------------
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
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript(
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
        );
        CREATE TABLE IF NOT EXISTS flights (
            id TEXT PRIMARY KEY,
            waid TEXT,
            origin TEXT,
            dest TEXT,
            depart_date TEXT,
            depart_time TEXT,
            arrival_date TEXT,
            arrival_time TEXT,
            airline TEXT,
            flight_number TEXT,
            pnr TEXT,
            source_file_id TEXT,
            raw_excerpt TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS hotels (
            id TEXT PRIMARY KEY,
            waid TEXT,
            hotel_name TEXT,
            city TEXT,
            checkin_date TEXT,
            checkout_date TEXT,
            address TEXT,
            source_file_id TEXT,
            raw_excerpt TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS recs (
            id TEXT PRIMARY KEY,
            waid TEXT,
            text TEXT,
            place_name TEXT,
            city_tag TEXT,
            category TEXT,
            lat REAL,
            lon REAL,
            url TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS google_tokens (
            waid TEXT PRIMARY KEY,
            token_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            waid TEXT,
            created_at TEXT
        );
        """
    )
    # שדרוג סכמות ישנות (אם חסר עמודה)
    try:
        db.execute("ALTER TABLE recs ADD COLUMN category TEXT")
    except Exception:
        pass
    db.commit()

with app.app_context():
    init_db()

# ------------------------- כלים -------------------------
TWILIO_SAFE_CHUNK = 1500
chat_histories: Dict[str, List[dict]] = defaultdict(list)

def chunk_text(s: str, n: int = TWILIO_SAFE_CHUNK) -> List[str]:
    s = s or ""
    return [s[i:i+n] for i in range(0, len(s), n)] or [""]

def public_base_url() -> str:
    if BASE_PUBLIC_URL:
        return BASE_PUBLIC_URL.rstrip("/") + "/"
    return request.host_url

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
    return validator.validate(url, form, signature)

def build_messages(history: List[dict], user_text: str) -> List[dict]:
    trimmed = history[-8:] if len(history) > 8 else history[:]
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(trimmed)
    msgs.append({"role": "user", "content": user_text})
    return msgs

def send_whatsapp(to_waid: str, body: str, media_urls: Optional[List[str]] = None):
    if not twilio_client:
        logger.warning("Twilio client not configured; cannot send outbound.")
        return
    kwargs = dict(to=to_waid, body=body)
    if TWILIO_MESSAGING_SERVICE_SID:
        kwargs["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        kwargs["from_"] = TWILIO_WHATSAPP_FROM
    if media_urls:
        kwargs["media_url"] = media_urls
    try:
        twilio_client.messages.create(**kwargs)
    except Exception as e:
        logger.exception("Twilio send failed: %s", e)

# ------------------------- זיהוי תאריכים/שעות/יעדים -------------------------
CITY_MAP = {
    "בנגקוק": "BKK", "bangkok": "BKK",
    "פוקט": "HKT", "phuket": "HKT",
    "chiang mai": "CNX", "צ'יאנג מאי": "CNX", "צ׳יאנג מאי": "CNX", "เชียงใหม่": "CNX",
    "קוסמוי": "USM", "koh samui": "USM", "סמוי": "USM",
    "קראבי": "KBV", "krabi": "KBV",
    "תל אביב": "TLV", "tel aviv": "TLV", "נתבג": "TLV", "נתב\"ג": "TLV", "israel": "TLV",
    "קופנגן": "KOPH", "koh phangan": "KOPH",  # תגית לעיר/אי (לא IATA)
}

DATE_PATTERNS = [
    (re.compile(r"(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})"), "%Y-%m-%d"),
    (re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})"), "%d-%m-%Y"),
]
TIME_RGX = re.compile(r"\b(\d{1,2}):(\d{2})\b")

def parse_dates(text: str) -> List[str]:
    out = []
    for rgx, fmt in DATE_PATTERNS:
        for m in rgx.finditer(text or ""):
            try:
                if fmt == "%Y-%m-%d":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                dt = datetime(y, mo, d)
                out.append(dt.strftime("%Y-%m-%d"))
            except Exception:
                continue
    return list(dict.fromkeys(out))

def parse_times(text: str) -> List[str]:
    res = []
    for m in TIME_RGX.finditer(text or ""):
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            res.append(f"{h:02d}:{mi:02d}")
    return list(dict.fromkeys(res))

def detect_airports(text: str) -> Dict[str, Optional[str]]:
    t = (text or "").lower()
    origin, dest = None, None
    iatas = re.findall(r"\b[A-Z]{3}\b", text or "")
    if len(iatas) >= 2:
        origin, dest = iatas[0], iatas[1]
    else:
        for name, code in CITY_MAP.items():
            if name in t and code in ("BKK","HKT","CNX","USM","KBV","TLV"):
                if not origin: origin = code
                elif not dest and code != origin: dest = code
    if dest and not origin:
        origin = "TLV"
    return {"origin": origin, "dest": dest}

# ------------------------- Vision/AI חילוץ נתונים -------------------------
def ai_extract_booking_from_text(text: str) -> Dict[str, dict]:
    if not openai_client:
        return {}
    prompt = (
        "Extract booking details only if present. Return strict JSON with keys 'flight' and 'hotel'. "
        "flight: {origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr}. "
        "hotel: {hotel_name,city,checkin_date,checkout_date,address}. "
        "Use ISO dates YYYY-MM-DD and HH:MM 24h. Fill only available fields."
    )
    try:
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":prompt},{"role":"user","content":text[:8000]}],
            temperature=0.0, timeout=25,
        )
        content = (r.choices[0].message.content or "").strip()
        s = content
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start:end+1]
        return json.loads(s)
    except openai.RateLimitError:
        return {}
    except Exception:
        return {}

def ai_extract_booking_from_image(image_url: str, hint: str = "") -> Dict[str, dict]:
    if not openai_client:
        return {}
    try:
        messages = [
            {"role":"system","content":"You read images of tickets/hotel confirmations and return strict JSON as specified."},
            {"role":"user","content":[
                {"type":"text","text":
                 ("Extract booking details if present. Return JSON with keys 'flight' and 'hotel' as in: "
                  "flight:{origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr}; "
                  "hotel:{hotel_name,city,checkin_date,checkout_date,address}. "
                  "Dates=YYYY-MM-DD, Times=HH:MM. Fill only existing fields. ") + (hint or "")},
                {"type":"image_url","image_url":{"url": image_url}}
            ]}
        ]
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.0,
            timeout=30,
        )
        content = (r.choices[0].message.content or "").strip()
        s = content
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start:end+1]
        return json.loads(s)
    except openai.RateLimitError:
        return {}
    except Exception:
        return {}

# ------------------------- Calendar (Google) -------------------------
def get_google_flow() -> Optional[Flow]:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI):
        return None
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_OAUTH_REDIRECT_URI]
        }
    }
    flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = GOOGLE_OAUTH_REDIRECT_URI
    return flow

def save_google_token(waid: str, creds: Credentials):
    db = get_db()
    js = creds.to_json()
    now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO google_tokens (waid, token_json, created_at, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(waid) DO UPDATE SET token_json=excluded.token_json, updated_at=excluded.updated_at",
        (waid, js, now, now)
    )
    db.commit()

def load_google_creds(waid: str) -> Optional[Credentials]:
    row = get_db().execute("SELECT token_json FROM google_tokens WHERE waid=?", (waid,)).fetchone()
    if not row:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(row["token_json"]), scopes=GOOGLE_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            save_google_token(waid, creds)
        except Exception as e:
            logger.exception("Google token refresh failed: %s", e)
            return None
    return creds

def add_calendar_event(waid: str, summary: str, description: str, start_iso: str, end_iso: Optional[str] = None, all_day: bool = False):
    creds = load_google_creds(waid)
    if not creds:
        return False
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    if all_day:
        event = {
            "summary": summary,
            "description": description,
            "start": {"date": start_iso},
            "end": {"date": end_iso or start_iso},
        }
    else:
        # start_iso: "YYYY-MM-DDTHH:MM:00"
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso or start_iso},
        }
    try:
        service.events().insert(calendarId="primary", body=event).execute()
        return True
    except Exception as e:
        logger.exception("Google Calendar insert failed: %s", e)
        return False

def to_dt_iso(date_str: str, time_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    if time_str and re.match(r"^\d{2}:\d{2}$", time_str):
        return f"{date_str}T{time_str}:00"
    return f"{date_str}T09:00:00"

# ------------------------- אינדוקס הזמנות לטבלה + הוספה לקלנדר -------------------------
def index_booking_from_text(waid: str, text: str, source_file_id: Optional[str], raw_excerpt: str):
    db = get_db()
    found_dates = parse_dates(text)
    found_times = parse_times(text)
    airports = detect_airports(text)
    flight = None
    if airports["dest"]:
        depart_date = found_dates[0] if found_dates else None
        depart_time = found_times[0] if found_times else None
        flight = {
            "origin": airports["origin"], "dest": airports["dest"],
            "depart_date": depart_date, "depart_time": depart_time,
            "arrival_date": None, "arrival_time": None,
            "airline": None, "flight_number": None, "pnr": None,
        }
    ai = ai_extract_booking_from_text(text) if openai_client else {}
    if ai.get("flight"):
        flight = {**(flight or {}), **{k:v for k,v in ai["flight"].items() if v}}
    if flight and flight.get("dest") and flight.get("depart_date"):
        fid = uuid.uuid4().hex
        db.execute(
            """INSERT INTO flights
               (id,waid,origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr,source_file_id,raw_excerpt,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, waid, flight.get("origin"), flight.get("dest"),
             flight.get("depart_date"), flight.get("depart_time"),
             flight.get("arrival_date"), flight.get("arrival_time"),
             flight.get("airline"), flight.get("flight_number"),
             flight.get("pnr"), source_file_id, raw_excerpt, datetime.utcnow().isoformat())
        )
        db.commit()
        # הוספה לקלנדר אם יש OAuth
        start_iso = to_dt_iso(flight.get("depart_date"), flight.get("depart_time"))
        summary = f"✈️ {flight.get('origin') or ''}→{flight.get('dest') or ''} {flight.get('flight_number') or ''}".strip()
        desc = f"Airline: {flight.get('airline') or ''}\nPNR: {flight.get('pnr') or ''}"
        if start_iso:
            add_calendar_event(waid, summary, desc, start_iso, None, all_day=False)

    # Hotels
    hotel = None
    if re.search(r"\b(hotel|מלון)\b", text, re.I) and len(found_dates) >= 1:
        checkin = found_dates[0]
        checkout = found_dates[1] if len(found_dates) >= 2 else None
        hotel = {"hotel_name": None, "city": None, "checkin_date": checkin, "checkout_date": checkout, "address": None}
    if ai.get("hotel"):
        hotel = {**(hotel or {}), **{k:v for k,v in ai["hotel"].items() if v}}
    if hotel and hotel.get("checkin_date"):
        hid = uuid.uuid4().hex
        db.execute(
            """INSERT INTO hotels
               (id,waid,hotel_name,city,checkin_date,checkout_date,address,source_file_id,raw_excerpt,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (hid, waid, hotel.get("hotel_name"), hotel.get("city"),
             hotel.get("checkin_date"), hotel.get("checkout_date"),
             hotel.get("address"), source_file_id, raw_excerpt, datetime.utcnow().isoformat())
        )
        db.commit()
        # Calendar
        add_calendar_event(
            waid,
            f"🏨 Check-in: {hotel.get('hotel_name') or ''}",
            f"City: {hotel.get('city') or ''}\nAddress: {hotel.get('address') or ''}",
            hotel.get("checkin_date"), hotel.get("checkout_date") or hotel.get("checkin_date"),
            all_day=True
        )

# ------------------------- אחסון קבצים -------------------------
def guess_extension(content_type: str, fallback_from_url: str = "") -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type)
        if ext:
            return ext
    path = urlparse(fallback_from_url).path
    _, dot, suffix = path.rpartition(".")
    if dot and suffix and len(suffix) <= 5:
        return "." + suffix
    return ".bin"

def save_file_record(waid: str, fname: str, content_type: str, data: bytes, title: str = "", tags: str = "") -> str:
    fid = uuid.uuid4().hex
    name = secure_filename(fname) or f"file-{fid}"
    if "." not in name and content_type:
        name += guess_extension(content_type)
    path = os.path.join(STORAGE_DIR, name)
    with open(path, "wb") as fp:
        fp.write(data)
    db = get_db()
    db.execute(
        "INSERT INTO files (id,waid,filename,content_type,path,title,tags,uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        (fid, waid, name, content_type or "application/octet-stream", path, title, tags, datetime.utcnow().isoformat()),
    )
    db.commit()
    # Index content
    try:
        excerpt = f"{title or ''}\n{tags or ''}"
        if (content_type or "").lower().startswith("text/"):
            text = data.decode("utf-8", errors="ignore")
            excerpt += "\n" + text[:4000]
            index_booking_from_text(waid, text, fid, excerpt[:2000])
        elif (content_type or "").lower() in ("application/pdf",) or name.lower().endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(path)
            pages = []
            for p in reader.pages[:6]:
                pages.append(p.extract_text() or "")
            text = "\n".join(pages)
            excerpt += "\n" + text[:4000]
            index_booking_from_text(waid, text, fid, excerpt[:2000])
        elif (content_type or "").lower().startswith("image/"):
            # Vision: נשתמש ב-URL הציבורי שלנו לתמונה ששמרנו
            img_url = public_base_url() + f"files/{fid}"
            ai = ai_extract_booking_from_image(img_url, hint=f"File name: {name}")
            if ai:
                index_booking_from_text(waid, json.dumps(ai), fid, f"vision:{name}")
    except Exception as e:
        logger.exception("Index from file failed: %s", e)
    return fid

def handle_incoming_media(waid: str, num_media: int, body_text: str) -> List[str]:
    saved = []
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        logger.warning("Media received but TWILIO creds missing.")
        return saved
    for i in range(num_media):
        media_url = request.form.get(f"MediaUrl{i}")
        ctype = request.form.get(f"MediaContentType{i}") or "application/octet-stream"
        if not media_url:
            continue
        try:
            r = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
            r.raise_for_status()
            url_name = os.path.basename(urlparse(media_url).path) or f"media-{uuid.uuid4().hex}"
            ext = os.path.splitext(url_name)[1]
            if not ext:
                ext = guess_extension(ctype, media_url)
                url_name += ext
            fid = save_file_record(
                waid, url_name, ctype, r.content,
                title=(body_text or "WhatsApp media")[:80], tags="whatsapp,media"
            )
            saved.append(fid)
        except Exception as e:
            logger.exception("Download media error: %s", e)
    return saved

# ------------------------- המלצות (עיר+קטגוריה) -------------------------
CATEGORY_MAP = {
    "מסעדה": ["מסעדה","restaurant","eat","food"],
    "בר": ["בר","bar","pub","drinks"],
    "קפה": ["קפה","coffee","cafe"],
    "חוף": ["חוף","beach"],
    "אטרקציה": ["אטרקציה","attraction","tour","trip","activity","סדנה","שייט","מפלים","שוק"],
    "ספא": ["ספא","spa","מסאז","massage"],
    "לינה": ["מלון","לינה","hotel","hostel","resort","bungalow"],
    "תחבורה": ["מונית","תחבורה","taxi","bus","ferry","מעבורת","סירה","boat"],
}

def infer_category(text: str) -> Optional[str]:
    t = (text or "").lower()
    for cat, kws in CATEGORY_MAP.items():
        if any(k in t for k in kws):
            return cat
    return "כללי" if text else None

def extract_city_tag(text: str) -> Optional[str]:
    t = (text or "").lower()
    for name in CITY_MAP.keys():
        if name in t:
            return name
    return None

def store_recommendation_if_relevant(waid: str, text: str, lat: Optional[str], lon: Optional[str]) -> None:
    if not text and not (lat and lon):
        return
    url = None
    m = re.search(r"(https?://\S+)", text or "", re.I)
    if m: url = m.group(1)
    city_tag = extract_city_tag(text or "") or None
    category = infer_category(text or "")
    place_name = None
    mq = re.search(r"[?&]q=([^&]+)", url or "")
    if mq:
        place_name = mq.group(1).replace("+"," ").strip()[:120]
    elif text:
        place_name = text.strip()[:120]
    try:
        db = get_db()
        db.execute(
            "INSERT INTO recs (id,waid,text,place_name,city_tag,category,lat,lon,url,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, waid, text or "", place_name, city_tag, category,
             float(lat) if lat else None, float(lon) if lon else None, url, datetime.utcnow().isoformat())
        )
        db.commit()
    except Exception as e:
        logger.exception("Failed to store recommendation: %s", e)

# ------------------------- Intentים -------------------------
FLIGHT_WORDS = ["flight","flights","טיסה","טיסות","כרטיס טיסה","הזמנת טיסה","find flight","book flight"]
RECO_WORDS = ["המלצות","recommendations","places","מה כדאי","לאן ללכת","מסעדות","ברים","חופים","קפה","אטרקציות"]
SEND_FILE_WORDS = ["שלח","תשלח","send","הכרטיס","pdf","כרטיס טיסה","ticket","boarding"]

def detect_intent(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in FLIGHT_WORDS):
        return "flight_search"
    if "ics" in t and "calendar" in t:
        return "calendar_link"
    if any(w in t for w in RECO_WORDS):
        return "recs_query"
    if any(w in t for w in SEND_FILE_WORDS):
        return "recall_file"
    return "general"

def build_flight_links(origin: Optional[str], dest: Optional[str], depart: Optional[str]) -> List[str]:
    if origin and dest and depart:
        g = f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}%20on%20{depart}"
        k = f"https://www.kayak.com/flights/{origin}-{dest}/{depart}?sort=bestflight_a"
    elif origin and dest:
        g = f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}"
        k = f"https://www.kayak.com/flights/{origin}-{dest}"
    else:
        g, k = "https://www.google.com/travel/flights", "https://www.kayak.com/flights"
    return [g, k]

# ------------------------- Routes בסיס -------------------------
@app.route("/", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET"])
def health():
    return "Your service is live 🎉", 200

@app.route("/test/openai", methods=["GET"])
def test_openai():
    if not openai_client:
        return "OpenAI client not configured", 200
    try:
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":"ping"}],
            temperature=0.0, timeout=25,
        )
        return f"OK: {(r.choices[0].message.content or '').strip()}", 200
    except Exception as e:
        logger.exception("OpenAI test endpoint failed: %s", e)
        return f"OpenAI error: {e}", 500

@app.route("/status", methods=["GET"])
def status():
    db = get_db()
    f = db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    fl = db.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
    h = db.execute("SELECT COUNT(*) c FROM hotels").fetchone()["c"]
    r = db.execute("SELECT COUNT(*) c FROM recs").fetchone()["c"]
    gcount = db.execute("SELECT COUNT(*) c FROM google_tokens").fetchone()["c"]
    return jsonify(ok=True, files=f, flights=fl, hotels=h, recs=r, google_tokens=gcount, now=str(tz_now()))

# ------------------------- Upload/Files/ICS -------------------------
@app.route("/upload", methods=["POST"])
def upload():
    init_db()
    f = request.files.get("file")
    waid = request.form.get("waid") or ""
    title = request.form.get("title") or ""
    tags = request.form.get("tags") or ""
    if not f or not waid:
        return jsonify({"ok": False, "error": "missing file or waid"}), 400
    fid = save_file_record(waid, f.filename or f"upload-{uuid.uuid4().hex}", f.mimetype or "application/octet-stream", f.read(), title=title, tags=tags)
    url = public_base_url() + f"files/{fid}"
    return jsonify(ok=True, file_id=fid, url=url)

@app.route("/files/<file_id>", methods=["GET"])
def serve_file(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row: abort(404)
    return send_file(row["path"], mimetype=row["content_type"], as_attachment=False, download_name=row["filename"])

@app.route("/calendar/<path:waid>.ics", methods=["GET"])
def calendar_ics(waid):
    db = get_db()
    flights = db.execute("SELECT * FROM flights WHERE waid=? ORDER BY depart_date", (waid,)).fetchall()
    hotels = db.execute("SELECT * FROM hotels WHERE waid=? ORDER BY checkin_date", (waid,)).fetchall()
    lines = ["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//ThailandBotAI//Travel//EN"]
    def dtstamp(d, t="09:00"):
        return d.replace("-","") + "T" + (t or "09:00").replace(":","") + "00Z"
    for fl in flights:
        start = dtstamp(fl["depart_date"], fl["depart_time"] or "09:00")
        summ = f"Flight {fl['origin'] or ''}->{fl['dest'] or ''} {fl['flight_number'] or ''}".strip()
        desc = f"Airline: {fl['airline'] or ''}\\nPNR: {fl['pnr'] or ''}"
        lines += ["BEGIN:VEVENT", f"UID:{fl['id']}@thailandbot", f"DTSTART:{start}", f"SUMMARY:{summ}", f"DESCRIPTION:{desc}", "END:VEVENT"]
    for ho in hotels:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ho['id']}@thailandbot",
            f"DTSTART;VALUE=DATE:{(ho['checkin_date']).replace('-','')}",
            f"DTEND;VALUE=DATE:{(ho['checkout_date'] or ho['checkin_date']).replace('-','')}",
            f"SUMMARY:Hotel: {ho['hotel_name'] or 'Check-in'}",
            f"DESCRIPTION:City: {ho['city'] or ''}\\nAddress: {ho['address'] or ''}",
            "END:VEVENT"
        ]
    lines.append("END:VCALENDAR")
    ics = "\r\n".join(lines)
    return Response(ics, mimetype="text/calendar")

# ------------------------- Google OAuth -------------------------
@app.route("/google/oauth/start", methods=["GET"])
def google_oauth_start():
    waid = request.args.get("waid")
    if not waid:
        return "Missing waid", 400
    flow = get_google_flow()
    if not flow:
        return "Google OAuth not configured", 500
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    db = get_db()
    db.execute("INSERT INTO oauth_states (state,waid,created_at) VALUES (?,?,?)",
               (state, waid, datetime.utcnow().isoformat()))
    db.commit()
    return redirect(auth_url, code=302)

@app.route("/google/oauth/callback", methods=["GET"])
def google_oauth_callback():
    state = request.args.get("state")
    code = request.args.get("code")
    if not state or not code:
        return "Missing state/code", 400
    row = get_db().execute("SELECT waid FROM oauth_states WHERE state=?", (state,)).fetchone()
    if not row:
        return "Invalid state", 400
    waid = row["waid"]
    flow = get_google_flow()
    if not flow:
        return "Google OAuth not configured", 500
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_google_token(waid, creds)
    return f"Google Calendar connected for {waid}! You can close this tab.", 200

@app.route("/google/status", methods=["GET"])
def google_status():
    waid = request.args.get("waid")
    if not waid:
        return "Missing waid", 400
    ok = load_google_creds(waid) is not None
    return jsonify(ok=ok)

# ------------------------- Twilio Webhook -------------------------
def handle_commands(body: str, waid: str) -> Optional[str]:
    cmd = (body or "").strip().lower()
    if cmd in ("/reset","reset","/restart"):
        chat_histories.pop(waid, None)
        return "✅ השיחה אופסה. תוכל להתחיל נושא חדש."
    if cmd in ("/help","help"):
        base = public_base_url()
        return (
            "ℹ️ אני יודע:\n"
            "• חיפוש טיסות: 'תמצא לי טיסה לפוקט ב-2025-09-12'\n"
            "• שליחת קובץ: שלחו PDF/תמונה – אני אשמור. 'שלח לי את הכרטיס'\n"
            f"• קלנדר: חברו גוגל → {base}google/oauth/start?waid=<WaId>  | ICS: {base}calendar/<WaId>.ics\n"
            "• המלצות: שלחו לינקים/מקומות; שליפה לפי עיר/קטגוריה (למשל: 'המלצות לקופנגן חופים')\n"
            "• /reset לאיפוס שיחה"
        )
    return None

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

    # מדיה נכנסת – שמירה ואינדוקס
    saved_media = []
    if num_media > 0:
        saved_media = handle_incoming_media(waid, num_media, body)
        if saved_media:
            resp.message(f"📎 שמרתי {len(saved_media)} קבצים. כתבו 'שלח לי את הכרטיס' לקבלת האחרון.")

    # המלצות/מיקום – נשמור
    if body or (latitude and longitude):
        store_recommendation_if_relevant(waid, body, latitude, longitude)

    # פקודות
    cmd_reply = handle_commands(body, waid)
    if cmd_reply:
        for ch in chunk_text(cmd_reply): resp.message(ch)
        return str(resp)

    user_text = body.strip()
    if latitude and longitude:
        loc = f"[location] lat={latitude}, lon={longitude} | {label or address or ''}"
        user_text = f"{user_text}\n\n{loc}" if user_text else loc

    if not user_text and saved_media:
        return str(resp)
    if not user_text:
        resp.message("👋 כתבו: 'תמצא טיסה לפוקט' / 'שלח לי את הכרטיס' / '/help'.")
        return str(resp)

    intent = detect_intent(user_text)

    # חיפוש טיסות
    if intent == "flight_search":
        airports = detect_airports(user_text)
        dates = parse_dates(user_text)
        origin, dest = airports["origin"], airports["dest"]
        depart = dates[0] if dates else None
        if not dest:
            resp.message("✈️ ציינו יעד (למשל פוקט) ואפשר תאריך YYYY-MM-DD.")
            return str(resp)
        links = build_flight_links(origin, dest, depart)
        msg = f"✈️ {origin or 'בחר מוצא'} → {dest}\nתאריך יציאה: {depart or 'בחר תאריך'}\nGoogle Flights: {links[0]}\nKayak: {links[1]}"
        for ch in chunk_text(msg): resp.message(ch)
        return str(resp)

    # שליחת קובץ אחרון (כרטיס/טיסה/PDF)
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
            ORDER BY uploaded_at DESC LIMIT 1
            """, (waid,)
        ).fetchone()
        if not row:
            row = db.execute("SELECT * FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT 1", (waid,)).fetchone()
        if not row:
            resp.message("לא מצאתי קובץ. שלחו PDF/תמונה או העלו דרך /upload.")
            return str(resp)
        file_url = public_base_url() + f"files/{row['id']}"
        m = resp.message(f"📄 {row['filename']}")
        m.media(file_url)
        return str(resp)

    # המלצות לפי עיר + קטגוריה
    if intent == "recs_query":
        # דוגמה: "המלצות לקופנגן חופים" → עיר=קופנגן, קטגוריה=חוף
        city = extract_city_tag(user_text)
        cat = infer_category(user_text)
        db = get_db()
        q = "SELECT place_name,url,text,category,city_tag FROM recs WHERE waid=?"
        params: List = [waid]
        if city:
            q += " AND LOWER(IFNULL(city_tag,'')) LIKE ?"
            params.append(f"%{city}%")
        if cat and cat != "כללי":
            q += " AND LOWER(IFNULL(category,'')) LIKE ?"
            params.append(f"%{cat}%")
        q += " ORDER BY created_at DESC LIMIT 12"
        rows = db.execute(q, tuple(params)).fetchall()
        if not rows:
            resp.message("לא מצאתי המלצות תואמות. שלחו לינקים/מקומות ואשמור לפי עיר/קטגוריה.")
            return str(resp)
        lines = [f"⭐ המלצות{(' ל-' + city) if city else ''}{(' – ' + cat) if cat and cat!='כללי' else ''}:"]
        for r in rows:
            title = r["place_name"] or (r["text"][:60] if r["text"] else "מקום")
            if r["url"]:
                lines.append(f"• {title} — {r['url']}")
            else:
                lines.append(f"• {title}")
        for ch in chunk_text("\n".join(lines)): resp.message(ch)
        return str(resp)

    # שיחה כללית (GPT) – עם Fallback אם אין מכסה
    history = chat_histories[waid]
    try:
        if not openai_client:
            raise RuntimeError("OpenAI disabled/not configured")
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=build_messages(history, user_text),
            temperature=0.4, timeout=25,
        )
        answer = (r.choices[0].message.content or "").strip() or "לא הצלחתי לענות כרגע."
    except openai.RateLimitError:
        answer = "⚠️ כרגע חרגתי מהמכסה של OpenAI. נסו שוב מעט מאוחר יותר."
    except Exception as e:
        logger.warning("GPT fallback: %s", e)
        answer = f"Echo: {user_text[:300]}"

    history.append({"role":"user","content":user_text})
    history.append({"role":"assistant","content":answer})
    if len(history) > 20: del history[:-20]
    for ch in chunk_text(answer): resp.message(ch)
    return str(resp)

# ------------------------- Cron -------------------------
def require_cron_secret():
    key = request.args.get("key")
    if key != CRON_SECRET:
        abort(403)

def date_str(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

@app.route("/cron/daily", methods=["POST","GET"])
def cron_daily():
    require_cron_secret()
    now = tz_now()
    tomorrow = now + timedelta(days=1)
    d_str = date_str(tomorrow)
    db = get_db()
    result = defaultdict(list)
    for fl in db.execute("SELECT * FROM flights WHERE depart_date=?", (d_str,)).fetchall():
        t = f"✈️ מחר: {fl['origin'] or ''}→{fl['dest'] or ''} {fl['flight_number'] or ''} בשעה {fl['depart_time'] or 'ללא שעה'}"
        result[fl["waid"]].append(t)
    for ho in db.execute("SELECT * FROM hotels WHERE checkin_date=?", (d_str,)).fetchall():
        t = f"🏨 מחר צ'ק-אין: {ho['hotel_name'] or 'מלון'} בעיר {ho['city'] or ''}"
        result[ho["waid"]].append(t)
    for waid, items in result.items():
        send_whatsapp(waid, "תזכורת למחר:\n" + "\n".join(items))
    return jsonify(ok=True, sent=len(result))

@app.route("/cron/weekly", methods=["POST","GET"])
def cron_weekly():
    require_cron_secret()
    now = tz_now()
    until = now + timedelta(days=7)
    db = get_db()
    waids = [r["waid"] for r in db.execute("SELECT DISTINCT waid FROM files").fetchall()]
    total = 0
    for waid in waids:
        flights = db.execute(
            "SELECT * FROM flights WHERE waid=? AND depart_date BETWEEN ? AND ? ORDER BY depart_date",
            (waid, date_str(now), date_str(until))
        ).fetchall()
        hotels = db.execute(
            "SELECT * FROM hotels WHERE waid=? AND checkin_date BETWEEN ? AND ? ORDER BY checkin_date",
            (waid, date_str(now), date_str(until))
        ).fetchall()
        if not flights and not hotels:
            continue
        lines = ["📅 דו\"ח שבועי:"]
        for fl in flights:
            lines.append(f"- ✈️ {fl['depart_date']} {fl['origin'] or ''}→{fl['dest'] or ''} {fl['flight_number'] or ''} {fl['depart_time'] or ''}")
        for ho in hotels:
            lines.append(f"- 🏨 {ho['checkin_date']} {ho['hotel_name'] or ''} ({ho['city'] or ''})")
        send_whatsapp(waid, "\n".join(lines))
        total += 1
    return jsonify(ok=True, sent=total)

# ------------------------- Main -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
