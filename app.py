# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Travel Assistant – Flask + Twilio + OpenAI (GPT-5) + SQLite + ICS + Cron + Google Calendar OAuth + Vision

שינויים מרכזיים:
- אין יותר פקודות/רג'אקס קשיחים. כל הניווט והבנה בשפה טבעית נעשים ע"י GPT-5 (nl_route).
- חילוץ פרטי טיסות/מלונות מתוך PDF/תמונה באמצעות GPT-5 (Vision) ושמירה ל-DB + הוספה ליומן (אם קיים OAuth).
- דיסק מתמשך ב-/data (Render) עבור קבצים ו-SQLite.
"""

import os, re, uuid, sqlite3, logging, json, mimetypes, hashlib
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

# ------------------------- לוגים וקונפיג בסיסי -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TZ = os.getenv("TZ", "UTC")

# ------------------------- Twilio -------------------------
VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # whatsapp:+1415...
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")  # MG...

# ------------------------- URLs / CRON -------------------------
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL")
CRON_SECRET = os.getenv("CRON_SECRET", "changeme")

# ------------------------- Flight Watch (Aviationstack) -------------------------
AVIATIONSTACK_KEY = (os.getenv("AVIATIONSTACK_KEY") or "").strip()
AVIATIONSTACK_URL = "http://api.aviationstack.com/v1/flights"
NOTIFY_CC_WAIDS = [x.strip() for x in os.getenv("NOTIFY_CC_WAIDS", "").split(",") if x.strip()]

# ------------------------- Router/aliases -------------------------
DEFAULT_LOOKAHEAD_DAYS = int(os.getenv("DEFAULT_LOOKAHEAD_DAYS", "90"))
CONTACT_ALIASES: Dict[str, str] = {}
for pair in (os.getenv("CONTACT_ALIASES","").split(",") if os.getenv("CONTACT_ALIASES") else []):
    if "=" in pair:
        name, wa = pair.split("=", 1)
        CONTACT_ALIASES[name.strip()] = wa.strip()

# ------------------------- OpenAI Client (GPT-5) -------------------------
api_key = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=api_key) if api_key else None

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
OPENAI_VERBOSITY = os.getenv("OPENAI_VERBOSITY")                 # low|medium|high (אופציונלי)
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT")   # minimal|medium|max (אופציונלי)
DEBUG_OPENAI_ERRORS = os.getenv("DEBUG_OPENAI_ERRORS", "false").lower() == "true"

def _gpt5_extra() -> dict:
    extra = {}
    if OPENAI_VERBOSITY:
        extra["verbosity"] = OPENAI_VERBOSITY
    if OPENAI_REASONING_EFFORT:
        extra["reasoning_effort"] = OPENAI_REASONING_EFFORT
    return extra

def gpt_chat(messages: List[dict], temperature: Optional[float] = None, timeout: int = 25):
    """
    קריאת צ'אט יציבה עם פולבק; מודלי gpt-5 לא תומכים ב-temperature שונה מברירת-המחדל,
    לכן לא נעביר את הפרמטר עבורם.
    """
    if not openai_client:
        raise RuntimeError("OpenAI client not configured")

    extra = _gpt5_extra()
    is_gpt5 = str(OPENAI_MODEL or "").lower().startswith("gpt-5")

    base_kwargs = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "timeout": timeout,
    }
    if (not is_gpt5) and (temperature is not None):
        base_kwargs["temperature"] = temperature

    try:
        return openai_client.chat.completions.create(
            **base_kwargs,
            **({"extra_body": extra} if extra else {}),
        )
    except Exception as e1:
        logger.warning("OpenAI error (with extras): %s", e1)
        try:
            return openai_client.chat.completions.create(**base_kwargs)
        except Exception as e2:
            logger.exception("OpenAI error (clean retry): %s", e2)
            if DEBUG_OPENAI_ERRORS:
                raise
            raise RuntimeError("openai_failed")

# ------------------------- System Prompt + בניית הודעות -------------------------
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a concise, helpful WhatsApp assistant. Answer in the user's language."
)


def list_files_for_waid(waid: str, limit: int = 20, offset: int = 0):
    db = get_db()
    rows = db.execute(
        "SELECT id, filename, content_type, uploaded_at "
        "FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
        (waid, limit, offset)
    ).fetchall()
    total = db.execute("SELECT COUNT(*) AS c FROM files WHERE waid=?", (waid,)).fetchone()["c"]
    return rows, total

def get_file_by_index_or_name(waid: str, index: Optional[int] = None, name: Optional[str] = None):
    db = get_db()
    if name:
        row = db.execute(
            "SELECT * FROM files WHERE waid=? AND LOWER(filename) LIKE ? "
            "ORDER BY uploaded_at DESC LIMIT 1",
            (waid, f"%{name.lower()}%")
        ).fetchone()
    else:
        idx = max(1, int(index or 1))
        row = db.execute(
            "SELECT * FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT 1 OFFSET ?",
            (waid, idx - 1)
        ).fetchone()
    return row




def build_messages(history: List[dict], user_text: str) -> List[dict]:
    sys_prompt = globals().get("SYSTEM_PROMPT") or os.getenv(
        "SYSTEM_PROMPT",
        "You are a concise, helpful WhatsApp assistant. Answer in the user's language."
    )
    trimmed = history[-8:] if len(history) > 8 else history[:]
    msgs = [{"role": "system", "content": sys_prompt}]
    msgs.extend(trimmed)
    msgs.append({"role": "user", "content": user_text})
    return msgs

# ------------------------- עזרי זמן/טלפון -------------------------
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

# ------------------------- Flask & Twilio -------------------------
app = Flask(__name__)

twilio_client: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ------------------------- אחסון ודיסק מתמשך -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.getenv("DATA_ROOT") or "/data"

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

STORAGE_DIR = os.getenv("STORAGE_DIR") or os.path.join(DATA_ROOT, "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

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
    db.executescript("""
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
            passenger_name TEXT,
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
    """)

    # מיגרציות קלות (idempotent)
    try:
        db.execute("ALTER TABLE flights ADD COLUMN passenger_name TEXT")
    except sqlite3.OperationalError:
        pass
    db.commit()

with app.app_context():
    init_db()

# ------------------------- כלי עזר -------------------------
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

# ------------------------- פירוק טקסטים בסיסי -------------------------
CITY_MAP = {
    "בנגקוק": "BKK", "bangkok": "BKK",
    "פוקט": "HKT", "phuket": "HKT",
    "chiang mai": "CNX", "צ'יאנג מאי": "CNX", "צ׳יאנג מאי": "CNX",
    "קוסמוי": "USM", "koh samui": "USM", "סמוי": "USM",
    "קראבי": "KBV", "krabi": "KBV",
    "תל אביב": "TLV", "tel aviv": "TLV", "נתבג": "TLV", "נתב\"ג": "TLV", "israel": "TLV",
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

# ------------------------- Vision/AI חילוץ פרטים -------------------------
def ai_extract_booking_from_text(text: str) -> Dict[str, list]:
    """Return {'flights':[...], 'hotels':[...]} (strict)."""
    if not openai_client:
        return {"flights": [], "hotels": []}
    prompt = (
        "Extract flight and hotel details from booking text.\n"
        "Return STRICT JSON:\n"
        "{ flights: [ {origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr,passengers} ],"
        "  hotels:  [ {hotel_name,city,checkin_date,checkout_date,address} ] }\n"
        "Where 'passengers' is an array of full names (['JOHN DOE','JANE DOE']).\n"
        "Dates in YYYY-MM-DD, times HH:MM 24h. Fill only known fields. If nothing, return empty arrays."
    )
    try:
        r = gpt_chat(
            messages=[{"role":"system","content":prompt},{"role":"user","content":text[:8000]}],
            timeout=25,
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
    except Exception as e:
        logger.warning("ai_extract_booking_from_text failed: %s", e)
        return {"flights": [], "hotels": []}

def ai_extract_booking_from_image(image_url: str, hint: str = "") -> Dict[str, list]:
    if not openai_client:
        return {"flights": [], "hotels": []}
    try:
        messages = [
            {"role":"system","content":
             "You read images of flight tickets and hotel confirmations and return STRICT JSON as: "
             "{ flights:[{origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr,passengers}],"
             "  hotels:[{hotel_name,city,checkin_date,checkout_date,address}] } (YYYY-MM-DD, HH:MM)."},
            {"role":"user","content":[
                {"type":"text","text": (hint or "")},
                {"type":"image_url","image_url":{"url": image_url}}
            ]}
        ]
        r = gpt_chat(messages=messages, timeout=30)
        s = (r.choices[0].message.content or "").strip()
        s = s[s.find("{"):s.rfind("}")+1] if "{" in s and "}" in s else "{}"
        obj = json.loads(s) if s else {}
        return {
            "flights": obj.get("flights") or ([obj.get("flight")] if isinstance(obj.get("flight"), dict) else []) or [],
            "hotels":  obj.get("hotels")  or ([obj.get("hotel")] if isinstance(obj.get("hotel"), dict)  else []) or [],
        }
    except Exception as e:
        logger.warning("ai_extract_booking_from_image failed: %s", e)
        return {"flights": [], "hotels": []}

# ------------------------- Google Calendar -------------------------
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

    # נאיבי (fallback)
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

    # טיסות
    for fl in flights:
        if not fl or not fl.get("dest") or not fl.get("depart_date"): 
            continue

        pax = fl.get("passengers")
        if isinstance(pax, list):
            pax_str = ", ".join([p for p in pax if p])
        elif isinstance(pax, str):
            pax_str = pax
        else:
            pax_str = None

        fid = uuid.uuid4().hex
        db.execute(
            """INSERT INTO flights
               (id,waid,origin,dest,depart_date,depart_time,arrival_date,arrival_time,airline,flight_number,pnr,passenger_name,source_file_id,raw_excerpt,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, waid, fl.get("origin"), fl.get("dest"),
             fl.get("depart_date"), fl.get("depart_time"),
             fl.get("arrival_date"), fl.get("arrival_time"),
             fl.get("airline"), fl.get("flight_number"),
             fl.get("pnr"), pax_str, source_file_id, raw_excerpt, datetime.utcnow().isoformat())
        )
        start_iso = to_dt_iso(fl.get("depart_date"), fl.get("depart_time"))
        if start_iso:
            summary = f"✈️ {fl.get('origin') or ''}→{fl.get('dest') or ''} {fl.get('flight_number') or ''}".strip()
            desc = f"Airline: {fl.get('airline') or ''}\nPNR: {fl.get('pnr') or ''}"
            add_calendar_event(waid, summary, desc, start_iso, None, all_day=False)

    # מלונות
    for ho in hotels:
        if not ho or not ho.get("checkin_date"): 
            continue
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
            max_pages = int(os.getenv("MAX_PDF_PAGES", "8"))
            reader = PdfReader(path)
            pages = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
            text = "\n".join(pages)
            excerpt += "\n" + text[:4000]
            index_booking_from_text(waid, text, fid, excerpt[:2000])
        elif (content_type or "").lower().startswith("image/"):
            img_url = public_base_url() + f"files/{fid}"
            ai = ai_extract_booking_from_image(img_url, hint=f"File name: {name}")
            if ai:
                # נרשום גם את ה-json הגולמי כ-raw_excerpt
                index_booking_from_text(waid, json.dumps(ai, ensure_ascii=False), fid, f"vision:{name}")
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
                ext = guess_extension(ctype, media_url); 
                url_name += ext
            fid = save_file_record(
                waid, url_name, ctype, r.content,
                title=(body_text or "WhatsApp media")[:80], tags="whatsapp,media"
            )
            saved.append(fid)
        except Exception as e:
            logger.exception("Download media error: %s", e)
    return saved

# ------------------------- המלצות (שימור לינקים/מיקומים) -------------------------
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

# ------------------------- Flight Watch (כלי עזר) -------------------------
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

# ------------------------- שאילתות טיסות מה-DB -------------------------
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
    if scope in ("all","כל"):
        return rows
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

# ------------------------- NL Router (GPT-5 בלבד) -------------------------
def nl_route(user_text: str) -> Optional[dict]:
    """
    מפענח בקשת וואטסאפ לטיפוס פעולה ופרמטרים, ללא שום זיהוי ידני.
    מחזיר JSON בסכמה:
      { "type": "<action>", "params": {...} }
    סוגי פעולה:
      - list_user_flights {range_days?:int}
      - list_person_flights {person:string, range_days?:int}
      - subscribe_flight {iata:string, date?:YYYY-MM-DD}
      - cancel_flight {iata?:string}        # ללא iata = בטל הכל
      - flight_status {iata:string}
      - send_last_ticket {}
      - flight_details {scope?: "latest"|"return"|"all"}
      - search_flights {origin?:string, dest:string, depart_date?:YYYY-MM-DD}
      - recs_query {city?:string, category?:string}
      - files_count {}
      - ticket_names {}
      - calendar_link {}
      - general_chat {prompt?:string}
    """
    if not openai_client or not (user_text or "").strip():
        return {"type": "general_chat", "params": {"prompt": user_text or ""}}

    sys = (
        "You are a router for a WhatsApp travel assistant. "
        "Return STRICT JSON only with fields 'type' and 'params'. "
        "If unsure, choose 'general_chat' with {prompt: <original text>}.\n"
        "Dates must be YYYY-MM-DD; IATA flight codes like LY81; Hebrew/English both allowed."
    )

    examples = """
שאלות → תשובות (JSON בלבד):
- "מה הטיסות שלי לשבוע הקרוב?" ->
  {"type":"list_user_flights","params":{"range_days":7}}

- "תן לי רשימה של כל הקבצים ששמרת לי" ->
  {"type":"list_files","params":{"limit":20}}

- "שלח את הקובץ האחרון" ->
  {"type":"send_last_ticket","params":{}}

- "שלח את הקובץ מספר 3" ->
  {"type":"send_file","params":{"index":3}}

- "שלח את הקובץ עם המילה receipt בשם" ->
  {"type":"send_file","params":{"name":"receipt"}}

- "מה הסטטוס של LY81?" ->
  {"type":"flight_status","params":{"iata":"LY81"}}

- "עקוב אחרי טיסה LY81 ב-2025-09-08" ->
  {"type":"subscribe_flight","params":{"iata":"LY81","date":"2025-09-08"}}

- "בטל את כל המעקבים" ->
  {"type":"cancel_flight","params":{}}

- "תן פרטים על הטיסה חזור" ->
  {"type":"flight_details","params":{"scope":"return"}}

- "מצא טיסה מתל אביב לפוקט ב-2025-10-01" ->
  {"type":"search_flights","params":{"origin":"TLV","dest":"HKT","depart_date":"2025-10-01"}}

- "כמה קבצים שמורים יש לך?" ->
  {"type":"files_count","params":{}}

- "תן קישור ליומן" ->
  {"type":"calendar_link","params":{}}

אם לא ברור:
- "מה קורה?" -> {"type":"general_chat","params":{"prompt":"מה קורה?"}}
"""

    try:
        r = gpt_chat(
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": f"Text:\n{user_text}\n\n{examples}\nReturn JSON only."}
            ],
            timeout=20
        )
        s = (r.choices[0].message.content or "").strip()
        start = s.find("{"); end = s.rfind("}")
        if start != -1 and end != -1:
            obj = json.loads(s[start:end+1])
            if isinstance(obj, dict) and obj.get("type"):
                return obj
    except Exception as e:
        logger.warning("nl_route error: %s", e)

    return {"type": "general_chat", "params": {"prompt": user_text or ""}}

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

# ------------------------- Twilio Webhook (GPT-first) -------------------------
def build_flight_links(origin: Optional[str], dest: str, depart: Optional[str]) -> Tuple[str,str]:
    o = (origin or "TLV").upper()
    d = (dest or "").upper()
    dd = (depart or "")
    gf = f"https://www.google.com/travel/flights?q=Flights%20to%20{d}%20from%20{o}" + (f"%20on%20{dd}" if dd else "")
    kdate = dd.replace("-", "") if dd else ""
    ky = f"https://www.kayak.com/flights/{o}-{d}/{kdate}"
    return gf, ky

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    if not _validated_twilio_request():
        abort(403)

    from_ = request.form.get("From", "")
    waid = normalize_waid(request.form.get("WaId", from_) or from_)
    body = request.form.get("Body", "") or ""
    num_media = int(request.form.get("NumMedia", "0") or 0)
    latitude = request.form.get("Latitude")
    longitude = request.form.get("Longitude")
    address = request.form.get("Address")
    label = request.form.get("Label")

    resp = MessagingResponse()
    saved_media: List[str] = []

    # ----- MEDIA (שמירה + אינדוקס + סיכום מיידי) -----
    if num_media > 0:
        saved_media = handle_incoming_media(waid, num_media, body)
        if saved_media:
            try:
                db = get_db()
                rows = db.execute("""
                    SELECT origin,dest,depart_date,depart_time,airline,flight_number,pnr,passenger_name
                    FROM flights WHERE waid=? ORDER BY created_at DESC LIMIT 3
                """, (waid,)).fetchall()

                if rows:
                    latest_pax = next((r["passenger_name"] for r in rows if r["passenger_name"]), None)
                    latest_pnr = next((r["pnr"] for r in rows if r["pnr"]), None)
                    lines = [f"📎 שמרתי {len(saved_media)} קבצים.", "✈️ מצאתי:"]
                    for fl in rows[::-1]:
                        lines.append(
                            f"- {fl['depart_date']} {fl['depart_time'] or ''} {fl['origin'] or ''}→{fl['dest'] or ''} "
                            f"{(fl['flight_number'] or '').strip()} | {fl['airline'] or ''}"
                        )
                    if latest_pnr:
                        lines.append(f"• PNR: {latest_pnr}")
                    if latest_pax:
                        lines.append(f"• נוסעים: {latest_pax}")
                    lines.append("אפשר לבקש: 'תן לי פרטים על הטיסה' / 'סטטוס LY81' / 'שלח את הכרטיס' / 'רשימת קבצים' וכו׳")
                    for ch in chunk_text("\n".join(lines)):
                        resp.message(ch)
                else:
                    resp.message(f"📎 שמרתי {len(saved_media)} קבצים. ניסיתי לחלץ פרטים – אם לא הופיע סיכום, שלחו קובץ אחר או כתבו מה תרצו שאעשה.")
            except Exception as e:
                logger.exception("Post-media summary failed: %s", e)
                resp.message(f"📎 שמרתי {len(saved_media)} קבצים. אפשר לבקש: 'מה הטיסות שלי' או 'שלח לי את הכרטיס'.")
            return str(resp)

    # שמירת לינקים/מיקום כהמלצה (לא עוצר זרימה)
    if body or (latitude and longitude):
        store_recommendation_if_relevant(waid, body, latitude, longitude)

    # ----- ניתוב GPT-5 -----
    nl = nl_route(body)
    t = (nl or {}).get("type") or "general_chat"
    p = (nl or {}).get("params") or {}

    # ===== פעולות =====
    if t == "list_user_flights":
        rows = upcoming_flights_for_waid(waid, int(p.get("range_days", DEFAULT_LOOKAHEAD_DAYS)))
        if not rows:
            resp.message("לא מצאתי טיסות קרובות.")
            return str(resp)
        lines = ["✈️ הטיסות הקרובות שלך:"] + [
            f"- {r['depart_date']} {r['depart_time'] or ''} {r['origin'] or ''}→{r['dest'] or ''} "
            f"{(r['flight_number'] or '').strip()}{(' | ' + r['airline']) if r['airline'] else ''}"
            for r in rows
        ]
        for ch in chunk_text("\n".join(lines)):
            resp.message(ch)
        return str(resp)

    if t == "list_person_flights":
        person = (p.get("person") or "").strip()
        other = CONTACT_ALIASES.get(person)
        if not other:
            resp.message(f"לא מכיר את '{person}'. הוסף אותו ל-CONTACT_ALIASES ב-ENV.")
            return str(resp)
        other_waid = normalize_waid(other)
        rows = upcoming_flights_for_waid(other_waid, int(p.get("range_days", DEFAULT_LOOKAHEAD_DAYS)))
        if not rows:
            resp.message(f"לא מצאתי טיסות קרובות עבור {person}.")
            return str(resp)
        lines = [f"✈️ הטיסות של {person}:"] + [
            f"- {r['depart_date']} {r['depart_time'] or ''} {r['origin'] or ''}→{r['dest'] or ''} "
            f"{(r['flight_number'] or '').strip()}{(' | ' + r['airline']) if r['airline'] else ''}"
            for r in rows
        ]
        for ch in chunk_text("\n".join(lines)):
            resp.message(ch)
        return str(resp)

    if t == "subscribe_flight":
        iata = (p.get("iata") or "").upper()
        date = p.get("date")
        if not iata:
            resp.message("לא הצלחתי להבין את קוד הטיסה (דוגמה: LY81).")
            return str(resp)
        db = get_db()
        db.execute(
            "INSERT INTO flight_watch (waid, flight_iata, flight_date, provider, last_snapshot, last_hash) VALUES (?,?,?,?,?,?)",
            (waid, iata, date, "aviationstack", None, None)
        )
        db.commit()
        resp.message(f"מעולה! עוקב אחרי {iata}" + (f" ({date})" if date else ""))
        return str(resp)

    if t == "cancel_flight":
        iata = (p.get("iata") or "").upper()
        db = get_db()
        if iata:
            db.execute("DELETE FROM flight_watch WHERE waid=? AND flight_iata=?", (waid, iata))
        else:
            db.execute("DELETE FROM flight_watch WHERE waid=?", (waid,))
        n = db.total_changes
        db.commit()
        resp.message("בוטל מעקב" + (f" אחרי {iata}" if iata else " לכל הטיסות") + f" ({n} רשומות).")
        return str(resp)

    if t == "flight_status":
        iata = (p.get("iata") or "").upper()
        if not iata:
            resp.message("צריך מזהה טיסה, למשל: סטטוס LY81")
            return str(resp)
        res = _fw_fetch_aviationstack(iata, None)
        if res.get("error") or not (res.get("data") or []):
            resp.message("לא מצאתי סטטוס לטיסה הזו כרגע.")
            return str(resp)
        snap = _fw_snapshot_from_aviationstack(res["data"][0])
        for ch in chunk_text(_fw_format_message(snap)):
            resp.message(ch)
        return str(resp)

    if t == "send_last_ticket":
        db = get_db()
        row = db.execute("SELECT * FROM files WHERE waid=? ORDER BY uploaded_at DESC LIMIT 1", (waid,)).fetchone()
        if not row:
            resp.message("לא מצאתי קובץ. שלחו PDF/תמונה או העלו דרך /upload.")
            return str(resp)
        file_url = public_base_url() + f"files/{row['id']}"
        m = resp.message(f"📄 {row['filename']}")
        m.media(file_url)
        return str(resp)

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

    if t == "search_flights":
        origin = p.get("origin") or "TLV"
        dest = p.get("dest")
        depart = p.get("depart_date")
        if not dest:
            resp.message("חסר יעד (dest). אפשר לכתוב: 'מצא טיסה TLV→BKK 2025-10-01'.")
            return str(resp)
        links = build_flight_links(origin, dest, depart)
        msg = f"✈️ {origin} → {dest}\nתאריך יציאה: {depart or 'בחר תאריך'}\nGoogle Flights: {links[0]}\nKayak: {links[1]}"
        for ch in chunk_text(msg):
            resp.message(ch)
        return str(resp)

    if t == "recs_query":
        city = p.get("city")
        cat = p.get("category")
        db = get_db()
        q = "SELECT place_name,url,text,category,city_tag FROM recs WHERE waid=?"
        params: List[str] = [waid]
        if city:
            q += " AND LOWER(IFNULL(city_tag,'')) LIKE ?"; params.append(f"%{str(city).lower()}%")
        if cat and cat != "כללי":
            q += " AND LOWER(IFNULL(category,'')) LIKE ?"; params.append(f"%{str(cat).lower()}%")
        q += " ORDER BY created_at DESC LIMIT 12"
        rows = db.execute(q, tuple(params)).fetchall()
        if not rows:
            resp.message("לא מצאתי המלצות תואמות. שלחו לינקים/מקומות ואשמור לפי עיר/קטגוריה.")
            return str(resp)
        lines = [f"⭐ המלצות{(' ל-' + city) if city else ''}{(' – ' + cat) if cat and cat!='כללי' else ''}:"]
        for r in rows:
            title = r["place_name"] or (r["text"][:60] if r["text"] else "מקום")
            lines.append(f"• {title}" + (f" — {r['url']}" if r["url"] else ""))
        for ch in chunk_text("\n".join(lines)):
            resp.message(ch)
        return str(resp)

    if t == "files_count":
        c = get_db().execute("SELECT COUNT(*) AS c FROM files WHERE waid=?", (waid,)).fetchone()["c"]
        resp.message(f"יש לך {c} קבצים שמורים.")
        return str(resp)

    if t == "ticket_names":
        row = get_db().execute(
            "SELECT passenger_name, pnr FROM flights WHERE waid=? AND passenger_name IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (waid,)
        ).fetchone()
        if not row:
            resp.message("לא מצאתי שמות נוסעים מהכרטיסים האחרונים. שלחו את ה-PDF ואחלץ שוב.")
            return str(resp)
        msg = f"👤 נוסעים: {row['passenger_name']}"
        if row["pnr"]: msg += f"\nPNR: {row['pnr']}"
        for ch in chunk_text(msg): 
            resp.message(ch)
        return str(resp)

    if t == "calendar_link":
        ics = public_base_url() + f"calendar/{waid}.ics"
        resp.message(f"📅 ה-ICS האישי שלך: {ics}")
        return str(resp)

    # --- יכולות קבצים חדשות ---
    if t == "list_files":
        limit = min(int(p.get("limit", 20)), 50)
        rows, total = list_files_for_waid(waid, limit=limit, offset=int(p.get("offset", 0) or 0))
        if not rows:
            resp.message("לא שמרתי עדיין קבצים עבורך.")
            return str(resp)
        lines = [f"📁 הקבצים האחרונים ({len(rows)}/{total}):"]
        for i, r in enumerate(rows, 1):
            url = public_base_url() + f"files/{r['id']}"
            lines.append(f"{i}. {r['filename']} — {r['uploaded_at']}\n{url}")
        for ch in chunk_text("\n".join(lines)):
            resp.message(ch)
        return str(resp)

    if t == "send_file":
        row = get_file_by_index_or_name(waid, index=p.get("index"), name=p.get("name"))
        if not row:
            resp.message("לא מצאתי קובץ תואם. נסה לפי מספר ברשימה או חלק מהשם.")
            return str(resp)
        file_url = public_base_url() + f"files/{row['id']}"
        m = resp.message(f"📄 {row['filename']}")
        m.media(file_url)
        return str(resp)

    # ===== ברירת מחדל: שיחה חופשית עם GPT-5 =====
    user_text = (p.get("prompt") if isinstance(p.get("prompt"), str) else body) or body
    history = chat_histories[waid]
    try:
        r = gpt_chat(messages=build_messages(history, user_text), timeout=25)
        answer = (r.choices[0].message.content or "").strip() or "לא הצלחתי לענות כרגע."
    except openai.RateLimitError:
        answer = "⚠️ כרגע חרגתי מהמכסה של OpenAI. נסו שוב מעט מאוחר יותר."
    except Exception as e:
        logger.warning("GPT fallback: %s", e)
        answer = (f"⚠️ OpenAI error: {e}" if DEBUG_OPENAI_ERRORS else f"לא הצלחתי להבין. נסו לנסח אחרת.")

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        del history[:-20]

    for ch in chunk_text(answer):
        resp.message(ch)
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
                recips = [r["waid"]] + [normalize_waid(x.replace("whatsapp:","").lstrip("+")) for x in NOTIFY_CC_WAIDS if x]
                for rcpt in recips:
                    send_whatsapp(rcpt, _fw_format_message(snap))
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
