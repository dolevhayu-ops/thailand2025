
# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Travel Assistant – Flask + Twilio + OpenAI + SQLite + ICS + Cron + Google Calendar OAuth + Vision

יכולות עיקריות:
- שיחה חופשית (GPT + NL Router): “מה הטיסות שלי?”, “תן לי פרטים על הטיסה”, “סטטוס LY81”, “עקוב אחרי טיסה LY81 2025-09-08”, “בטל LY81”, “שלח לי את הכרטיס”, “מה הטיסות של דולב” וכן הלאה.
- קבלת מדיה (PDF/תמונה) בוואטסאפ → חילוץ ריבוי טיסות/מלונות + סיכום מיידי
- פיד ICS אישי: /calendar/<WaId>.ics
- Cron יומי/שבועי (תזכורות/דו״ח) + Flight Watch (בדיקת שינויים והתראות)
- Google Calendar OAuth: הוספת אירועים אוטומטית ליומן
- Flight Watch חינמי (Aviationstack) + התראות לשולח ולנמענים ב־NOTIFY_CC_WAIDS
"""

import os, re, uuid, sqlite3, logging, json, mimetypes, hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse
from collections import defaultdict
from typing import List, Dict, Optional

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
    "You are a concise, helpful WhatsApp assistant. Answer in the user's language."
)
VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # whatsapp:+1415...
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # MG...

BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL")
CRON_SECRET = os.getenv("CRON_SECRET", "changeme")
TZ = os.getenv("TZ", "UTC")

# === FLIGHT WATCH – config ===
AVIATIONSTACK_KEY = (os.getenv("AVIATIONSTACK_KEY") or "").strip()
AVIATIONSTACK_URL = "http://api.aviationstack.com/v1/flights"
NOTIFY_CC_WAIDS = [x.strip() for x in os.getenv("NOTIFY_CC_WAIDS", "").split(",") if x.strip()]

# === NL Router / Aliases ===
DEFAULT_LOOKAHEAD_DAYS = int(os.getenv("DEFAULT_LOOKAHEAD_DAYS", "90"))
CONTACT_ALIASES: Dict[str, str] = {}
for pair in (os.getenv("CONTACT_ALIASES","").split(",") if os.getenv("CONTACT_ALIASES") else []):
    if "=" in pair:
        name, wa = pair.split("=", 1)
        CONTACT_ALIASES[name.strip()] = wa.strip()

# OpenAI client
api_key = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=api_key) if api_key else None

# Twilio client
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

def normalize_waid(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    s = s.strip()
    if s.startswith("whatsapp:"):
        s = s[len("whatsapp:"):]
    s = s.lstrip("+")
    return s

app = Flask(__name__)

# ------------------------- אחסון/DB -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# נתיב קבוע על דיסק מתמשך (ב-Render הגדֵר Persistent Disk שממופה ל-/data)
DATA_ROOT = os.getenv("DATA_ROOT") or "/data"

# ודא שהדיסק קיים וכתוב; אם לא — עצור עם שגיאה ברורה
if not os.path.isdir(DATA_ROOT):
    raise RuntimeError("Persistent disk is not mounted at /data. In Render, add a Disk and mount it to /data.")
try:
    os.makedirs(DATA_ROOT, exist_ok=True)
    _test_path = os.path.join(DATA_ROOT, ".writetest")
    with open(_test_path, "w", encoding="utf-8") as _f:
        _f.write("ok")
    os.remove(_test_path)
except Exception as e:
    raise RuntimeError(f"Cannot write to persistent data root ({DATA_ROOT}). Details: {e}")

# תיקיית קבצים נשמרת בדיסק המתמשך
STORAGE_DIR = os.path.join(DATA_ROOT, "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

# קובץ ה-SQLite יישב בדיסק המתמשך
DB_PATH = os.getenv("DB_PATH") or os.path.join(DATA_ROOT, "data.sqlite3")




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
        PRAGMA journal_mode=WAL;
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
        -- === FLIGHT WATCH ===
        CREATE TABLE IF NOT EXISTS flight_watch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            waid TEXT NOT NULL,
            flight_iata TEXT NOT NULL,
            flight_date TEXT,
            provider TEXT DEFAULT 'aviationstack',
            last_snapshot TEXT,
            last_hash TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
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
    to_waid_norm = "whatsapp:+" + normalize_waid(to_waid)
    kwargs = dict(to=to_waid_norm, body=body)
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
    "קופנגן": "KOPH", "koh phangan": "KOPH",
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
                dt_ = datetime(y, mo, d)
                out.append(dt_.strftime("%Y-%m-%d"))
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
def ai_extract_booking_from_text(text: str) -> Dict[str, list]:
    """Return {'flights':[...], 'hotels':[...]} (strict)."""
    if not openai_client:
        return {"flights": [], "hotels": []}
    prompt = (
        "Extract flight and hotel details from booking text.\n"
        "Return STRICT JSON: { flights: [ {origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr} ],"
        "  hotels: [ {hotel_name,city,checkin_date,checkout_date,address} ] }.\n"
        "Dates in YYYY-MM-DD, times HH:MM 24h. Fill only known fields. If nothing, return empty arrays."
    )
    try:
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":prompt},{"role":"user","content":text[:8000]}],
            temperature=0.0, timeout=25,
        )
        s = (r.choices[0].message.content or "").strip()
        s = s[s.find("{"):s.rfind("}")+1] if "{" in s and "}" in s else "{}"
        obj = json.loads(s) if s else {}
        if "flights" not in obj:
            f = obj.get("flight")
            obj["flights"] = [f] if isinstance(f, dict) else []
        if "hotels" not in obj:
            h = obj.get("hotel")
            obj["hotels"] = [h] if isinstance(h, dict) else []
        return {"flights": obj.get("flights") or [], "hotels": obj.get("hotels") or []}
    except Exception:
        return {"flights": [], "hotels": []}

def ai_extract_booking_from_image(image_url: str, hint: str = "") -> Dict[str, list]:
    if not openai_client:
        return {"flights": [], "hotels": []}
    try:
        messages = [
            {"role":"system","content":
             "You read images of tickets/hotel confirmations and return STRICT JSON as: "
             "{ flights:[{origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr}],"
             "  hotels:[{hotel_name,city,checkin_date,checkout_date,address}] } (YYYY-MM-DD, HH:MM)."},
            {"role":"user","content":[
                {"type":"text","text": (hint or "")},
                {"type":"image_url","image_url":{"url": image_url}}
            ]}
        ]
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, temperature=0.0, timeout=30,
        )
        s = (r.choices[0].message.content or "").strip()
        s = s[s.find("{"):s.rfind("}")+1] if "{" in s and "}" in s else "{}"
        obj = json.loads(s) if s else {}
        return {
            "flights": obj.get("flights") or ([obj.get("flight")] if isinstance(obj.get("flight"), dict) else []) or [],
            "hotels":  obj.get("hotels")  or ([obj.get("hotel")] if isinstance(obj.get("hotel"), dict)  else []) or [],
        }
    except Exception:
        return {"flights": [], "hotels": []}

# ------------------------- Calendar (Google) -------------------------
def get_google_flow() -> Optional[Flow]:
    cid = os.getenv("GOOGLE_CLIENT_ID"); cs = os.getenv("GOOGLE_CLIENT_SECRET"); red = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
    if not (cid and cs and red): return None
    client_config = {"web":{"client_id":cid,"client_secret":cs,"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","redirect_uris":[red]}}
    flow = Flow.from_client_config(client_config, scopes=["https://www.googleapis.com/auth/calendar"])
    flow.redirect_uri = red
    return flow

def save_google_token(waid: str, creds: Credentials):
    db = get_db()
    js = creds.to_json(); now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO google_tokens (waid, token_json, created_at, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(waid) DO UPDATE SET token_json=excluded.token_json, updated_at=excluded.updated_at",
        (waid, js, now, now)
    ); db.commit()

def load_google_creds(waid: str) -> Optional[Credentials]:
    row = get_db().execute("SELECT token_json FROM google_tokens WHERE waid=?", (waid,)).fetchone()
    if not row: return None
    creds = Credentials.from_authorized_user_info(json.loads(row["token_json"]), scopes=["https://www.googleapis.com/auth/calendar"])
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest()); save_google_token(waid, creds)
        except Exception as e:
            logger.exception("Google token refresh failed: %s", e); return None
    return creds

def add_calendar_event(waid: str, summary: str, description: str, start_iso: str, end_iso: Optional[str] = None, all_day: bool = False):
    creds = load_google_creds(waid)
    if not creds: return False
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    if all_day:
        event = {"summary":summary,"description":description,"start":{"date":start_iso},"end":{"date": end_iso or start_iso}}
    else:
        event = {"summary":summary,"description":description,"start":{"dateTime":start_iso},"end":{"dateTime": end_iso or start_iso}}
    try:
        service.events().insert(calendarId="primary", body=event).execute(); return True
    except Exception as e:
        logger.exception("Google Calendar insert failed: %s", e); return False

def to_dt_iso(date_str: str, time_str: Optional[str]) -> Optional[str]:
    if not date_str: return None
    if time_str and re.match(r"^\d{2}:\d{2}$", time_str): return f"{date_str}T{time_str}:00"
    return f"{date_str}T09:00:00"

# ------------------------- אינדוקס הזמנות (ריבוי טיסות) -------------------------
def index_booking_from_text(waid: str, text: str, source_file_id: Optional[str], raw_excerpt: str):
    db = get_db()

    # נאיבי (fallback) – טיסה אחת
    naive_flight = None
    found_dates = parse_dates(text); found_times = parse_times(text); airports = detect_airports(text)
    if airports["dest"]:
        naive_flight = {
            "origin": airports["origin"], "dest": airports["dest"],
            "depart_date": (found_dates[0] if found_dates else None),
            "depart_time": (found_times[0] if found_times else None),
            "arrival_date": None, "arrival_time": None,
            "airline": None, "flight_number": None, "pnr": None,
        }

    ai = ai_extract_booking_from_text(text) if openai_client else {"flights": [], "hotels": []}
    flights = ai.get("flights") or []
    hotels = ai.get("hotels") or []

    if not flights and naive_flight and naive_flight.get("dest") and naive_flight.get("depart_date"):
        flights = [naive_flight]

    # שמירת כל הטיסות
    for fl in flights:
        if not fl or not fl.get("dest") or not fl.get("depart_date"): continue
        fid = uuid.uuid4().hex
        db.execute(
            """INSERT INTO flights
               (id,waid,origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr,source_file_id,raw_excerpt,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, waid, fl.get("origin"), fl.get("dest"),
             fl.get("depart_date"), fl.get("depart_time"),
             fl.get("arrival_date"), fl.get("arrival_time"),
             fl.get("airline"), fl.get("flight_number"),
             fl.get("pnr"), source_file_id, raw_excerpt, datetime.utcnow().isoformat())
        )
        start_iso = to_dt_iso(fl.get("depart_date"), fl.get("depart_time"))
        if start_iso:
            summary = f"✈️ {fl.get('origin') or ''}→{fl.get('dest') or ''} {fl.get('flight_number') or ''}".strip()
            desc = f"Airline: {fl.get('airline') or ''}\nPNR: {fl.get('pnr') or ''}"
            add_calendar_event(waid, summary, desc, start_iso, None, all_day=False)

    # שמירת כל המלונות
    for ho in hotels:
        if not ho or not ho.get("checkin_date"): continue
        hid = uuid.uuid4().hex
        db.execute(
            """INSERT INTO hotels
               (id,waid,hotel_name,city,checkin_date,checkout_date,address,source_file_id,raw_excerpt,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (hid, waid, ho.get("hotel_name"), ho.get("city"),
             ho.get("checkin_date"), ho.get("checkout_date"),
             ho.get("address"), source_file_id, raw_excerpt, datetime.utcnow().isoformat())
        )
        add_calendar_event(
            waid,
            f"🏨 Check-in: {ho.get('hotel_name') or ''}",
            f"City: {ho.get('city') or ''}\nAddress: {ho.get('address') or ''}",
            ho.get("checkin_date"), ho.get("checkout_date") or ho.get("checkin_date"),
            all_day=True
        )
    db.commit()

# ------------------------- אחסון קבצים -------------------------
def guess_extension(content_type: str, fallback_from_url: str = "") -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type)
        if ext: return ext
    path = urlparse(fallback_from_url).path
    _, dot, suffix = path.rpartition(".")
    if dot and suffix and len(suffix) <= 5: return "." + suffix
    return ".bin"

def save_file_record(waid: str, fname: str, content_type: str, data: bytes, title: str = "", tags: str = "") -> str:
    fid = uuid.uuid4().hex
    name = secure_filename(fname) or f"file-{fid}"
    if "." not in name and content_type: name += guess_extension(content_type)
    path = os.path.join(STORAGE_DIR, name)
    with open(path, "wb") as fp: fp.write(data)
    db = get_db()
    db.execute(
        "INSERT INTO files (id,waid,filename,content_type,path,title,tags,uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        (fid, waid, name, content_type or "application/octet-stream", path, title, tags, datetime.utcnow().isoformat()),
    ); db.commit()
    # אינדוקס תוכן
    try:
        excerpt = f"{title or ''}\n{tags or ''}"
        if (content_type or "").lower().startswith("text/"):
            text = data.decode("utf-8", errors="ignore")
            excerpt += "\n" + text[:4000]
            index_booking_from_text(waid, text, fid, excerpt[:2000])
        elif (content_type or "").lower() in ("application/pdf",) or name.lower().endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(path)
            pages = [(p.extract_text() or "") for p in reader.pages[:6]]
            text = "\n".join(pages)
            excerpt += "\n" + text[:4000]
            index_booking_from_text(waid, text, fid, excerpt[:2000])
        elif (content_type or "").lower().startswith("image/"):
            img_url = public_base_url() + f"files/{fid}"
            ai = ai_extract_booking_from_image(img_url, hint=f"File name: {name}")
            if ai: index_booking_from_text(waid, json.dumps(ai), fid, f"vision:{name}")
    except Exception as e:
        logger.exception("Index from file failed: %s", e)
    return fid

def handle_incoming_media(waid: str, num_media: int, body_text: str) -> List[str]:
    saved = []
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        logger.warning("Media received but TWILIO creds missing."); return saved
    for i in range(num_media):
        media_url = request.form.get(f"MediaUrl{i}")
        ctype = request.form.get(f"MediaContentType{i}") or "application/octet-stream"
        if not media_url: continue
        try:
            r = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
            r.raise_for_status()
            url_name = os.path.basename(urlparse(media_url).path) or f"media-{uuid.uuid4().hex}"
            ext = os.path.splitext(url_name)[1]
            if not ext: ext = guess_extension(ctype, media_url); url_name += ext
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
        if any(k in t for k in kws): return cat
    return "כללי" if text else None

def extract_city_tag(text: str) -> Optional[str]:
    t = (text or "").lower()
    for name in CITY_MAP.keys():
        if name in t: return name
    return None

def store_recommendation_if_relevant(waid: str, text: str, lat: Optional[str], lon: Optional[str]) -> None:
    if not text and not (lat and lon): return
    url = None; m = re.search(r"(https?://\S+)", text or "", re.I)
    if m: url = m.group(1)
    city_tag = extract_city_tag(text or "") or None
    category = infer_category(text or "")
    place_name = None; mq = re.search(r"[?&]q=([^&]+)", url or "")
    if mq: place_name = mq.group(1).replace("+"," ").strip()[:120]
    elif text: place_name = text.strip()[:120]
    try:
        db = get_db()
        db.execute(
            "INSERT INTO recs (id,waid,text,place_name,city_tag,category,lat,lon,url,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, waid, text or "", place_name, city_tag, category,
             float(lat) if lat else None, float(lon) if lon else None, url, datetime.utcnow().isoformat())
        ); db.commit()
    except Exception as e:
        logger.exception("Failed to store recommendation: %s", e)

# ------------------------- Intentים (קיצורי דרך – fallback) -------------------------
FLIGHT_WORDS = ["flight","flights","טיסה","טיסות","כרטיס טיסה","הזמנת טיסה","find flight","book flight"]
RECO_WORDS = ["המלצות","recommendations","places","מה כדאי","לאן ללכת","מסעדות","ברים","חופים","קפה","אטרקציות"]
SEND_FILE_WORDS = ["שלח","תשלח","send","הכרטיס","pdf","כרטיס טיסה","ticket","boarding"]
MY_FLIGHT_WORDS = ["מה הטיסה שלי", "מתי הטיסה שלי", "הטיסה שלי", "פרטי הטיסה", "flight details", "my flight"]

def detect_intent(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in MY_FLIGHT_WORDS): return "my_flight"
    if any(w in t for w in FLIGHT_WORDS): return "flight_search"
    if "ics" in t and "calendar" in t: return "calendar_link"
    if any(w in t for w in RECO_WORDS): return "recs_query"
    if any(w in t for w in SEND_FILE_WORDS): return "recall_file"
    # ✨ חדש: אם יש גם "פרטים" וגם "טיסה" → flight_details
    if "פרטים" in t and "טיסה" in t:
        return "flight_details"
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

# ------------------------- === FLIGHT WATCH === core -------------------------
IATA_RE = re.compile(r"\b([A-Z]{2}\d{1,4})\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

def _fw_parse_track(text: str):
    t = (text or "").strip().lower()
    if "עקוב" not in t and "track" not in t: return None, None, False
    m = IATA_RE.search(text.upper())
    if not m: return None, None, True
    flight = m.group(1).upper()
    dm = DATE_RE.search(text); date_str = dm.group(1) if dm else None
    return flight, date_str, True

def _fw_parse_untrack(text: str):
    t = (text or "").lower()
    if not any(k in t for k in ["בטל","unsubscribe","untrack","הסר"]): return None
    m = IATA_RE.search(text.upper())
    return m.group(1).upper() if m else "__ALL__"

def _fw_is_list(text: str):
    return any(k in (text or "").lower() for k in ["רשימה", "list flights", "list"])

def _fw_send_to_all(primary_waid: str, body: str):
    recips = [primary_waid] + [normalize_waid(r.replace("whatsapp:","").lstrip("+")) for r in NOTIFY_CC_WAIDS if r]
    for r in recips:
        send_whatsapp(r, body)

def _fw_fmt_time_both(iso_ts: str) -> str:
    if not iso_ts: return "-"
    try:
        t_utc = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return iso_ts
    s_utc = t_utc.strftime("%Y-%m-%d %H:%M UTC")
    if ZoneInfo:
        try:
            t_loc = t_utc.astimezone(ZoneInfo(TZ))
            s_loc = t_loc.strftime(f"%Y-%m-%d %H:%M {TZ}")
            return f"{s_utc} | {s_loc}"
        except Exception: pass
    return s_utc

def _fw_snapshot_from_aviationstack(rec: dict):
    def safe(*keys, default=None):
        cur = rec
        for k in keys:
            if not isinstance(cur, dict): return default
            cur = cur.get(k)
        return cur
    status = safe("flight_status")
    dep = {"airport":safe("departure","airport"),"scheduled":safe("departure","scheduled"),"estimated":safe("departure","estimated"),"actual":safe("departure","actual"),"terminal":safe("departure","terminal"),"gate":safe("departure","gate")}
    arr = {"airport":safe("arrival","airport"),"scheduled":safe("arrival","scheduled"),"estimated":safe("arrival","estimated"),"actual":safe("arrival","actual"),"terminal":safe("arrival","terminal"),"gate":safe("arrival","gate"),"baggage":safe("arrival","baggage")}
    flight = {"iata":safe("flight","iata"),"icao":safe("flight","icao"),"number":safe("flight","number")}
    airline = safe("airline","name")
    return {"status": status, "airline": airline, "flight": flight, "departure": dep, "arrival": arr}

def _fw_snapshot_hash(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode("utf-8")).hexdigest()

def _fw_format_message(snap: dict) -> str:
    f = snap.get("flight", {}) or {}; dep = snap.get("departure", {}) or {}; arr = snap.get("arrival", {}) or {}
    lines = [
        f"✈️ עדכון טיסה {f.get('iata') or f.get('number','')}",
        f"סטטוס: {snap.get('status','-')} | חברת תעופה: {snap.get('airline','-')}",
        f"יציאה: {dep.get('airport','-')} טרמ' {dep.get('terminal','-')} שער {dep.get('gate','-')}",
        f"זמני יציאה: מתוכנן {_fw_fmt_time_both(dep.get('scheduled'))} | משוער {_fw_fmt_time_both(dep.get('estimated'))} | בפועל {_fw_fmt_time_both(dep.get('actual'))}",
        f"הגעה: {arr.get('airport','-')} טרמ' {arr.get('terminal','-')} שער {arr.get('gate','-')} (מסוע {arr.get('baggage','-')})",
        f"זמני הגעה: מתוכנן {_fw_fmt_time_both(arr.get('scheduled'))} | משוער {_fw_fmt_time_both(arr.get('estimated'))} | בפועל {_fw_fmt_time_both(arr.get('actual'))}",
    ]; return "\n".join(lines)

def _fw_fetch_aviationstack(flight_iata: str, flight_date: Optional[str]):
    if not AVIATIONSTACK_KEY: return {"error": "Missing AVIATIONSTACK_KEY"}
    params = {"access_key": AVIATIONSTACK_KEY, "flight_iata": flight_iata}
    if flight_date: params["flight_date"] = flight_date
    r = requests.get(AVIATIONSTACK_URL, params=params, timeout=25)
    if r.status_code != 200:
        return {"error": f"aviationstack HTTP {r.status_code}", "body": r.text}
    try: data = r.json()
    except Exception as e: return {"error": f"aviationstack JSON parse: {e}", "body": r.text}
    return {"data": data.get("data", [])}

# ------------------------- עזר לטיסות קרובות + פרטים -------------------------
def upcoming_flights_for_waid(waid: str, days_ahead: int = DEFAULT_LOOKAHEAD_DAYS, limit: int = 3):
    db = get_db()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    until = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT origin,dest,depart_date,depart_time,airline,flight_number,pnr,arrival_date,arrival_time
        FROM flights
        WHERE waid=? AND depart_date BETWEEN ? AND ?
        ORDER BY depart_date ASC, IFNULL(depart_time,'23:59') ASC
        LIMIT ?
    """, (waid, today, until, limit)).fetchall()
    return rows

def pick_flights_for_details(waid: str, scope: str = "latest"):
    db = get_db()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr
        FROM flights
        WHERE waid=? AND depart_date >= ?
        ORDER BY depart_date ASC, IFNULL(depart_time,'23:59') ASC
        LIMIT 5
    """, (waid, today)).fetchall()
    if not rows:
        return []
    scope = (scope or "latest").lower()
    if scope in ("latest","next","קרובה","קרוב"):
        return [rows[0]]
    if scope in ("return","חזור","חזרה"):
        return rows[-1:] if len(rows) > 1 else [rows[0]]
    return rows[:2]

def format_flight_details(rows):
    if not rows:
        return "לא מצאתי טיסות קרובות. שלחו PDF/תמונה של הכרטיס או כתבו 'מה הטיסות שלי'."
    lines = []
    for r in rows:
        lines += [
            "✈️ פרטי טיסה:",
            f"- תאריך/שעה: {r['depart_date']} {r['depart_time'] or ''}".strip(),
            f"- מסלול: {r['origin'] or ''} → {r['dest'] or ''}",
            f"- חברת תעופה: {r['airline'] or '-'}",
            f"- מספר טיסה: {r['flight_number'] or '-'}",
            f"- PNR: {r['pnr'] or '-'}",
            ""
        ]
    return "\n".join(lines).strip()

# ------------------------- NL Router (שפה טבעית → פעולה) -------------------------
def nl_route(user_text: str) -> Optional[dict]:
    if not openai_client or not user_text.strip():
        return None

    sys = (
        "Turn a WhatsApp travel request into STRICT JSON.\n"
        "Schema: {type: enum['list_user_flights','list_person_flights','subscribe_flight',"
        "'cancel_flight','flight_status','send_last_ticket','flight_details','none'], params: object}\n"
        "Return JSON only."
    )

usr = f"Text: {user_text}\n" + (
    "Examples:\n"
    "- 'מה הטיסות שלי?' -> {\"type\":\"list_user_flights\",\"params\":{\"range_days\":30}}\n"
    "- 'מה הטיסות של דולב לשבוע הקרוב' -> {\"type\":\"list_person_flights\",\"params\":{\"person\":\"דולב\",\"range_days\":7}}\n"
    "- 'עקוב אחרי טיסה LY81 ב-2025-09-08' -> {\"type\":\"subscribe_flight\",\"params\":{\"iata\":\"LY81\",\"date\":\"2025-09-08\"}}\n"
    "- 'בטל LY81' -> {\"type\":\"cancel_flight\",\"params\":{\"iata\":\"LY81\"}}\n"
    "- 'סטטוס LY81' -> {\"type\":\"flight_status\",\"params\":{\"iata\":\"LY81\"}}\n"
    "- 'שלח לי את הכרטיס' -> {\"type\":\"send_last_ticket\",\"params\":{}}\n"
    "- 'תן לי פרטים על הטיסה' -> {\"type\":\"flight_details\",\"params\":{\"scope\":\"latest\"}}\n"
    "- 'מה הפרטים של הטיסה חזור' -> {\"type\":\"flight_details\",\"params\":{\"scope\":\"return\"}}\n"
    "- 'פרטים על הטיסה חזור' -> {\"type\":\"flight_details\",\"params\":{\"scope\":\"return\"}}\n"
    "- 'מה ה-PNR שלי?' -> {\"type\":\"flight_details\",\"params\":{\"scope\":\"latest\"}}\n"
)







Ask ChatGPT


    try:
        r = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            timeout=12,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": usr},
            ],
        )
        s = (r.choices[0].message.content or "").strip()
        s = s[s.find("{"):s.rfind("}")+1] if "{" in s and "}" in s else s
        obj = json.loads(s)
        if isinstance(obj, dict) and obj.get("type"):
            return obj
    except Exception as e:
        logger.warning("nl_route failed: %s", e)
        return None

    return None

# ------------------------- Routes בסיס -------------------------
@app.route("/", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET"])
def health():
    return "Your service is live 🎉", 200

@app.route("/status", methods=["GET"])
def status():
    db = get_db()
    f = db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    fl = db.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
    h = db.execute("SELECT COUNT(*) c FROM hotels").fetchone()["c"]
    r = db.execute("SELECT COUNT(*) c FROM recs").fetchone()["c"]
    gcount = db.execute("SELECT COUNT(*) c FROM google_tokens").fetchone()["c"]
    fw = db.execute("SELECT COUNT(*) c FROM flight_watch").fetchone()["c"]
    return jsonify(ok=True, files=f, flights=fl, hotels=h, recs=r, google_tokens=gcount, flight_watch=fw, now=str(tz_now()))

# ------------------------- Upload/Files/ICS -------------------------
@app.route("/upload", methods=["POST"])
def upload():
    init_db()
    f = request.files.get("file")
    waid = normalize_waid(request.form.get("waid") or "")
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
    waid = normalize_waid(waid)
    db = get_db()
    flights = db.execute("SELECT * FROM flights WHERE waid=? ORDER BY depart_date", (waid,)).fetchall()
    hotels = db.execute("SELECT * FROM hotels WHERE waid=? ORDER BY checkin_date", (waid,)).fetchall()
    lines = ["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//ThailandBotAI//Travel//EN"]
    def dtstamp(d, t="09:00"): return d.replace("-","") + "T" + (t or "09:00").replace(":","") + "00Z"
    for fl in flights:
        start = dtstamp(fl["depart_date"], fl["depart_time"] or "09:00")
        summ = f"Flight {fl['origin'] or ''}->{fl['dest'] or ''} {fl['flight_number'] or ''}".strip()
        desc = f"Airline: {fl['airline'] or ''}\\nPNR: {fl['pnr'] or ''}"
        lines += ["BEGIN:VEVENT", f"UID:{fl['id']}@thailandbot", f"DTSTART:{start}", f"SUMMARY:{summ}", f"DESCRIPTION:{desc}", "END:VEVENT"]
    for ho in hotels:
        lines += ["BEGIN:VEVENT", f"UID:{ho['id']}@thailandbot",
                  f"DTSTART;VALUE=DATE:{(ho['checkin_date']).replace('-','')}",
                  f"DTEND;VALUE=DATE:{(ho['checkout_date'] or ho['checkin_date']).replace('-','')}",
                  f"SUMMARY:Hotel: {ho['hotel_name'] or 'Check-in'}",
                  f"DESCRIPTION:City: {ho['city'] or ''}\\nAddress: {ho['address'] or ''}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    ics = "\r\n".join(lines); return Response(ics, mimetype="text/calendar")

# ------------------------- Google OAuth -------------------------
@app.route("/google/oauth/start", methods=["GET"])
def google_oauth_start():
    waid = normalize_waid(request.args.get("waid"))
    if not waid: return "Missing waid", 400
    flow = get_google_flow()
    if not flow: return "Google OAuth not configured", 500
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    db = get_db()
    db.execute("INSERT INTO oauth_states (state,waid,created_at) VALUES (?,?,?)", (state, waid, datetime.utcnow().isoformat())); db.commit()
    return redirect(auth_url, code=302)

@app.route("/google/oauth/callback", methods=["GET"])
def google_oauth_callback():
    state = request.args.get("state"); code = request.args.get("code")
    if not state or not code: return "Missing state/code", 400
    row = get_db().execute("SELECT waid FROM oauth_states WHERE state=?", (state,)).fetchone()
    if not row: return "Invalid state", 400
    waid = row["waid"]; flow = get_google_flow()
    if not flow: return "Google OAuth not configured", 500
    flow.fetch_token(authorization_response=request.url); creds = flow.credentials; save_google_token(waid, creds)
    return f"Google Calendar connected for {waid}! You can close this tab.", 200

@app.route("/google/status", methods=["GET"])
def google_status():
    waid = normalize_waid(request.args.get("waid"))
    if not waid: return "Missing waid", 400
    ok = load_google_creds(waid) is not None
    return jsonify(ok=ok)

# ------------------------- Twilio Webhook -------------------------
def handle_commands(body: str, waid: str) -> Optional[str]:
    cmd = (body or "").strip().lower()
    if cmd in ("/reset","reset","/restart"):
        chat_histories.pop(waid, None); return "✅ השיחה אופסה. תוכל להתחיל נושא חדש."
    if cmd in ("/help","help"):
        base = public_base_url()
        return ("ℹ️ אני יודע:\n"
                "• 'מה הטיסות שלי' / 'מה הטיסות של דולב/עודד'\n"
                "• 'תן לי פרטים על הטיסה' / 'מה הפרטים של הטיסה חזור'\n"
                "• חיפוש טיסות: 'תמצא טיסה TLV→BKK 2025-09-12'\n"
                f"• גוגל קלנדר: {base}google/oauth/start?waid=<WaId>  | ICS: {base}calendar/<WaId>.ics\n"
                "• 'שלח לי את הכרטיס' להחזרת הקובץ האחרון\n"
                "• מעקב טיסות: 'עקוב אחרי טיסה LY7 2025-09-25' | 'בטל LY7' | 'רשימה'\n"
                "• /reset לאיפוס שיחה")
    return None

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    if not _validated_twilio_request(): abort(403)
    from_ = request.form.get("From", "")
    waid = normalize_waid(request.form.get("WaId", from_) or from_)
    body = request.form.get("Body", "") or ""
    num_media = int(request.form.get("NumMedia", "0") or 0)
    latitude = request.form.get("Latitude"); longitude = request.form.get("Longitude")
    address = request.form.get("Address"); label = request.form.get("Label")
    resp = MessagingResponse()

    # מדיה נכנסת – שמירה, אינדוקס + סיכום מיידי
    saved_media = []
    if num_media > 0:
        saved_media = handle_incoming_media(waid, num_media, body)
        if saved_media:
            resp.message(f"📎 שמרתי {len(saved_media)} קבצים.")
            try:
                db = get_db()
                rows = db.execute("""
                    SELECT origin,dest,depart_date,depart_time,airline,flight_number,pnr
                    FROM flights WHERE waid=? ORDER BY created_at DESC LIMIT 2
                """, (waid,)).fetchall()
                if rows:
                    lines = ["✈️ מצאתי:"]
                    for fl in rows[::-1]:
                        lines.append(
                            f"- {fl['depart_date']} {fl['depart_time'] or ''} "
                            f"{fl['origin'] or ''}→{fl['dest'] or ''} "
                            f"{(fl['flight_number'] or '').strip()} | {fl['airline'] or ''} | PNR: {fl['pnr'] or '-'}"
                        )
                    lines.append("אפשר לכתוב: 'תן לי פרטים על הטיסה' / 'עקוב אחרי טיסה <IATA> <תאריך>'")
                    for ch in chunk_text("\n".join(lines)): resp.message(ch)
                else:
                    resp.message("ניסיתי לחלץ פרטים. אם לא הופיע סיכום, שלחו קובץ אחר או כתבו 'מה הטיסות שלי'.")
            except Exception as e:
                logger.exception("Post-media summary failed: %s", e)
                resp.message("שמרתי את הקובץ. אפשר לכתוב: 'מה הטיסות שלי' או 'שלח לי את הכרטיס'.")
            return str(resp)

    # המלצות/מיקום – נשמור
    if body or (latitude and longitude): store_recommendation_if_relevant(waid, body, latitude, longitude)

    # פקודות טכניות
    cmd_reply = handle_commands(body, waid)
    if cmd_reply:
        for ch in chunk_text(cmd_reply): resp.message(ch)
        return str(resp)

    # --- Natural-language router ---
    nl = nl_route(body)
    if nl and nl.get("type") != "none":
        t = nl["type"]; p = nl.get("params") or {}
        if t == "list_user_flights":
            rows = upcoming_flights_for_waid(waid, int(p.get("range_days", DEFAULT_LOOKAHEAD_DAYS)))
            if not rows: resp.message("לא מצאתי טיסות קרובות."); return str(resp)
            lines = ["✈️ הטיסות הקרובות שלך:"] + [
                f"- {r['depart_date']} {r['depart_time'] or ''} {r['origin'] or ''}→{r['dest'] or ''} {(r['flight_number'] or '').strip()}{(' | ' + r['airline']) if r['airline'] else ''}"
                for r in rows
            ]
            for ch in chunk_text("\n".join(lines)): resp.message(ch)
            return str(resp)

        if t == "list_person_flights":
            person = (p.get("person") or "").strip()
            other = CONTACT_ALIASES.get(person)
            if not other:
                resp.message(f"לא מכיר את '{person}'. הוסף ל-ENV CONTACT_ALIASES."); return str(resp)
            other_waid = normalize_waid(other)
            rows = upcoming_flights_for_waid(other_waid, int(p.get("range_days", DEFAULT_LOOKAHEAD_DAYS)))
            if not rows: resp.message(f"לא מצאתי טיסות קרובות עבור {person}."); return str(resp)
            lines = [f"✈️ הטיסות של {person}:"] + [
                f"- {r['depart_date']} {r['depart_time'] or ''} {r['origin'] or ''}→{r['dest'] or ''} {(r['flight_number'] or '').strip()}{(' | ' + r['airline']) if r['airline'] else ''}"
                for r in rows
            ]
            for ch in chunk_text("\n".join(lines)): resp.message(ch)
            return str(resp)

        if t == "subscribe_flight":
            iata = (p.get("iata") or "").upper(); date = p.get("date")
            if not iata:
                resp.message("לא הצלחתי להבין את הטיסה. נסו למשל: LY81 2025-09-08."); return str(resp)
            db = get_db()
            db.execute("INSERT INTO flight_watch (waid, flight_iata, flight_date, provider, last_snapshot, last_hash) VALUES (?,?,?,?,?,?)",
                       (waid, iata, date, "aviationstack", None, None)); db.commit()
            resp.message(f"מעולה! עוקב אחרי {iata}" + (f" ({date})" if date else "")); return str(resp)

        if t == "cancel_flight":
            iata = (p.get("iata") or "").upper(); db = get_db()
            if iata: db.execute("DELETE FROM flight_watch WHERE waid=? AND flight_iata=?", (waid, iata))
            else: db.execute("DELETE FROM flight_watch WHERE waid=?", (waid,))
            n = db.total_changes; db.commit()
            resp.message("בוטל מעקב" + (f" אחרי {iata}" if iata else " לכל הטיסות") + f" ({n} רשומות)."); return str(resp)

        if t == "flight_status":
            iata = (p.get("iata") or "").upper()
            if not iata: resp.message("צריך מזהה טיסה, למשל: סטטוס LY81"); return str(resp)
            res = _fw_fetch_aviationstack(iata, None)
            if res.get("error") or not (res.get("data") or []): resp.message("לא מצאתי סטטוס לטיסה הזו כרגע."); return str(resp)
            snap = _fw_snapshot_from_aviationstack(res["data"][0])
            for ch in chunk_text(_fw_format_message(snap)): resp.message(ch)
            return str(resp)

        if t == "send_last_ticket":
            db = get_db()
            row = db.execute("SELECT * FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT 1", (waid,)).fetchone()
            if not row: resp.message("לא מצאתי קובץ. שלחו PDF/תמונה או העלו דרך /upload."); return str(resp)
            file_url = public_base_url() + f"files/{row['id']}"; m = resp.message(f"📄 {row['filename']}"); m.media(file_url); return str(resp)

        if t == "flight_details":
            scope = (p.get("scope") or "latest")
            rows = pick_flights_for_details(waid, scope)
            msg = format_flight_details(rows)
            if rows:
                ics = public_base_url() + f"calendar/{waid}.ics"
                first_num = (rows[0]['flight_number'] or "").strip()
                first_date = rows[0]['depart_date']
                extra = f"\n📅 ICS: {ics}"
                if first_num and first_date:
                    extra += f"\n🔔 מעקב: כתבו 'עקוב אחרי טיסה {first_num} {first_date}'"
                msg += extra
            for ch in chunk_text(msg):
                resp.message(ch)
            return str(resp)

    # === FLIGHT WATCH – פקודות קשיחות (fallback) ===
    track_iata, track_date, is_track_cmd = _fw_parse_track(body)
    if is_track_cmd:
        if not track_iata: resp.message("צריך מזהה טיסה, למשל: 'עקוב אחרי טיסה LY7 2025-09-25'."); return str(resp)
        db = get_db()
        db.execute("INSERT INTO flight_watch (waid, flight_iata, flight_date, provider, last_snapshot, last_hash) VALUES (?,?,?,?,?,?)",
                   (waid, track_iata, track_date, "aviationstack", None, None)); db.commit()
        resp.message(f"מעולה! עוקב אחרי הטיסה {track_iata}" + (f" לתאריך {track_date}" if track_date else "") + ". אעדכן כשיהיו שינויים.")
        return str(resp)

    to_untrack = _fw_parse_untrack(body)
    if to_untrack:
        db = get_db()
        if to_untrack == "__ALL__": db.execute("DELETE FROM flight_watch WHERE waid=?", (waid,))
        else: db.execute("DELETE FROM flight_watch WHERE waid=? AND flight_iata=?", (waid, to_untrack))
        n = db.total_changes; db.commit()
        resp.message("בוטל מעקב" + (f" אחרי {to_untrack}" if to_untrack != "__ALL__" else " לכל הטיסות") + f" ({n} רשומות)."); return str(resp)

    if any(k in body.lower() for k in ["רשימה", "list flights", "list"]):
        db = get_db()
        rows = db.execute("SELECT id, flight_iata, flight_date, created_at FROM flight_watch WHERE waid=? ORDER BY id DESC", (waid,)).fetchall()
        if not rows: resp.message("אין מנויים פעילים כרגע."); return str(resp)
        lines = [f"✈️ רשימת מנויים ({len(rows)}):"] + [
            f"#{r['id']} {r['flight_iata']}" + (f" {r['flight_date']}" if r['flight_date'] else "") + f" (מ־{r['created_at']})"
            for r in rows
        ]
        for ch in chunk_text("\n".join(lines)): resp.message(ch); return str(resp)

    # ---- יתר היכולות (intent ישן + GPT כללי) ----
    user_text = body.strip()
    if latitude and longitude:
        loc = f"[location] lat={latitude}, lon={longitude} | {label or address or ''}"
        user_text = f"{user_text}\n\n{loc}" if user_text else loc

    if not user_text and saved_media: return str(resp)
    if not user_text:
        resp.message("👋 כתבו: 'מה הטיסות שלי' / 'תן לי פרטים על הטיסה' / 'סטטוס LY81' / 'שלח לי את הכרטיס' / '/help'.")
        return str(resp)

    intent = detect_intent(user_text)

    # "מה הטיסות שלי" – הקרובות
    if intent == "my_flight":
        rows = upcoming_flights_for_waid(waid, days_ahead=DEFAULT_LOOKAHEAD_DAYS)
        if not rows: resp.message("לא מצאתי טיסות קרובות. שלחו PDF/תמונה של הכרטיס או טקסט עם הפרטים."); return str(resp)
        lines = ["✈️ הטיסות הקרובות שלך:"] + [
            f"- {r['depart_date']} {r['depart_time'] or ''} {r['origin'] or ''}→{r['dest'] or ''} {(r['flight_number'] or '').strip()}{(' | ' + r['airline']) if r['airline'] else ''}"
            for r in rows
        ]
        for ch in chunk_text("\n".join(lines)): resp.message(ch); return str(resp)

    # חיפוש טיסות (קישורים)
    if intent == "flight_search":
        airports = detect_airports(user_text); dates = parse_dates(user_text)
        origin, dest = airports["origin"], airports["dest"]; depart = dates[0] if dates else None
        if not dest:
            resp.message("✈️ ציינו יעד (למשל פוקט) ואפשר תאריך YYYY-MM-DD."); return str(resp)
        links = build_flight_links(origin, dest, depart)
        msg = f"✈️ {origin or 'בחר מוצא'} → {dest}\nתאריך יציאה: {depart or 'בחר תאריך'}\nGoogle Flights: {links[0]}\nKayak: {links[1]}"
        for ch in chunk_text(msg): resp.message(ch)
        return str(resp)

    # שליחת קובץ אחרון (כרטיס/טיסה/PDF)
    if intent == "recall_file":
        db = get_db()
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
        city = extract_city_tag(user_text); cat = infer_category(user_text)
        db = get_db()
        q = "SELECT place_name,url,text,category,city_tag FROM recs WHERE waid=?"
        params: List = [waid]
        if city:
            q += " AND LOWER(IFNULL(city_tag,'')) LIKE ?"; params.append(f"%{city}%")
        if cat and cat != "כללי":
            q += " AND LOWER(IFNULL(category,'')) LIKE ?"; params.append(f"%{cat}%")
        q += " ORDER BY created_at DESC LIMIT 12"
        rows = db.execute(q, tuple(params)).fetchall()
        if not rows:
            resp.message("לא מצאתי המלצות תואמות. שלחו לינקים/מקומות ואשמור לפי עיר/קטגוריה.")
            return str(resp)
        lines = [f"⭐ המלצות{(' ל-' + city) if city else ''}{(' – ' + cat) if cat and cat!='כללי' else ''}:"]
        for r in rows:
            title = r["place_name"] or (r["text"][:60] if r["text"] else "מקום")
            if r["url"]: lines.append(f"• {title} — {r['url']}")
            else: lines.append(f"• {title}")
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
        lines = ["🗓️ השבוע הקרוב:"]
        for fl in flights:
            lines.append(f"• ✈️ {fl['depart_date']} {fl['depart_time'] or ''} {fl['origin'] or ''}→{fl['dest'] or ''} {fl['flight_number'] or ''}".strip())
        for ho in hotels:
            lines.append(f"• 🏨 {ho['checkin_date']} צ'ק-אין: {ho['hotel_name'] or ''} ({ho['city'] or ''})")
        send_whatsapp(waid, "\n".join(lines))
        total += 1
    return jsonify(ok=True, sent=total)

@app.route("/cron/flightwatch", methods=["POST","GET"])
def cron_flightwatch():
    require_cron_secret()
    db = get_db()
    rows = db.execute("SELECT id, waid, flight_iata, flight_date FROM flight_watch ORDER BY id DESC").fetchall()
    updated = 0; errors = 0
    for r in rows:
        iata = r["flight_iata"]; fdate = r["flight_date"]
        try:
            res = _fw_fetch_aviationstack(iata, fdate)
            if res.get("error"):
                errors += 1
                continue
            data = res.get("data") or []
            if not data:
                continue
            snap = _fw_snapshot_from_aviationstack(data[0])
            s_hash = _fw_snapshot_hash(snap)
            row2 = db.execute("SELECT last_hash FROM flight_watch WHERE id=?", (r["id"],)).fetchone()
            prev_hash = (row2["last_hash"] if row2 else None)
            if s_hash != prev_hash:
                # שינוי התגלה → שליחת הודעה ועדכון
                _fw_send_to_all(r["waid"], _fw_format_message(snap))
                db.execute("UPDATE flight_watch SET last_snapshot=?, last_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (json.dumps(snap, ensure_ascii=False), s_hash, r["id"]))
                db.commit()
                updated += 1
        except Exception as e:
            logger.exception("flightwatch error for %s: %s", iata, e)
            errors += 1
    return jsonify(ok=True, updated=updated, errors=errors, total=len(rows))

# ------------------------- Run -------------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

