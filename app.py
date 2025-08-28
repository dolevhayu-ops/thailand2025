# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Chatbot on Flask + Twilio + OpenAI (Full)
- /                    : בדיקת חיות
- /health              : בדיקת חיות
- /test/openai         : בדיקת חיבור ל-OpenAI
- /twilio/webhook      : Webhook ל־Twilio WhatsApp (POST)

Env (להגדיר ב-Render → Environment):
- OPENAI_API_KEY              : חובה
- OPENAI_MODEL                : ברירת מחדל: gpt-4o-mini
- OPENAI_ORG                  : אופציונלי
- OPENAI_PROJECT              : אופציונלי (אם key מסוג sk-proj)
- SYSTEM_PROMPT               : אופציונלי
- VERIFY_TWILIO_SIGNATURE     : 'true' כדי לאמת חתימה (ברירת מחדל: 'false')
- TWILIO_AUTH_TOKEN           : חובה אם VERIFY_TWILIO_SIGNATURE=true
- LOG_LEVEL                   : INFO/DEBUG (ברירת מחדל: INFO)

Start command (Render):
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2
"""

import os
import logging
from collections import defaultdict
from typing import List

from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# OpenAI SDK (v1.x)
from openai import OpenAI

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

# יצירת לקוח OpenAI (תומך אופציונלית בארגון/פרויקט)
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    logger.error("OPENAI_API_KEY is missing!")
    raise RuntimeError("OPENAI_API_KEY is not set")
client = OpenAI(
    api_key=api_key,
    organization=os.getenv("OPENAI_ORG") or None,
    project=os.getenv("OPENAI_PROJECT") or None,
)

app = Flask(__name__)

# זיכרון שיחה זמני (ל-PoC). לפרודקשן: Redis/DB
chat_histories = defaultdict(list)  # key = from_waid ; value = [{role, content}]

# מגבלת אורך הודעה בטוח דרך Twilio→WhatsApp (נחתוך ל~1500 תווים)
TWILIO_SAFE_CHUNK = 1500

# ----------------------------------------------------
# עזר: ולידציית בקשות מטוויליו (אופציונלי)
# ----------------------------------------------------
def _validated_twilio_request() -> bool:
    if not VERIFY_TWILIO_SIGNATURE:
        return True
    if not TWILIO_AUTH_TOKEN:
        logger.warning("VERIFY_TWILIO_SIGNATURE=true אבל חסר TWILIO_AUTH_TOKEN")
        return False

    validator = RequestValidator(TWILIO_AUTH_TOKEN)

    # התאמת URL ל-https אם הפרוקסי שינה ל-http
    url = request.url
    xf_proto = request.headers.get("X-Forwarded-Proto", "")
    if xf_proto == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://") :]

    signature = request.headers.get("X-Twilio-Signature", "")
    form = request.form.to_dict(flat=True)

    is_valid = validator.validate(url, form, signature)
    if not is_valid:
        logger.warning("Twilio signature validation FAILED")
    return is_valid

# ----------------------------------------------------
# עזר: חיתוך טקסט להודעות קצרות
# ----------------------------------------------------
def chunk_text(s: str, size: int = TWILIO_SAFE_CHUNK) -> List[str]:
    s = s or ""
    return [s[i : i + size] for i in range(0, len(s), size)] or [""]

# ----------------------------------------------------
# עזר: בניית הודעות למודל
# ----------------------------------------------------
def build_messages(history: List[dict], user_text: str) -> List[dict]:
    trimmed = history[-8:] if len(history) > 8 else history[:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(trimmed)
    messages.append({"role": "user", "content": user_text})
    return messages

# ----------------------------------------------------
# פקודות קצרות
# ----------------------------------------------------
def handle_commands(body: str, waid: str):
    cmd = (body or "").strip().lower()
    if cmd in ("/reset", "reset", "/restart"):
        chat_histories.pop(waid, None)
        return "✅ השיחה אופסה. תוכל להתחיל נושא חדש."
    if cmd in ("/help", "help"):
        return (
            "ℹ️ פקודות שימושיות:\n"
            "• /reset – איפוס היסטוריית השיחה למספר שלך\n"
            "• כתוב כל שאלה/בקשה – אענה בקצרה ובענייניות\n"
            "טיפ: אפשר לבקש תשובה עם רשימות, צעדים, או טבלאות (טקסטואליות)."
        )
    return None

# ----------------------------------------------------
# ראוטים
# ----------------------------------------------------
@app.route("/", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET"])
def health():
    return "Your service is live 🎉", 200

@app.route("/test/openai", methods=["GET"])
def test_openai():
    """בדיקת חיבור ל-OpenAI (GET ידני מהדפדפן/Health probe)."""
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

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    if not _validated_twilio_request():
        abort(403)

    # שדות שימושיים מה-Webhook של Twilio
    from_ = request.form.get("From", "")            # לדוגמה: 'whatsapp:+9725xxxxxxx'
    waid = request.form.get("WaId", from_)          # מזהה וואטסאפ גלובלי (אם קיים)
    body = request.form.get("Body", "") or ""
    num_media = int(request.form.get("NumMedia", "0") or 0)

    # לוקיישן (אם נשלח)
    latitude = request.form.get("Latitude")
    longitude = request.form.get("Longitude")
    address = request.form.get("Address")
    label = request.form.get("Label")

    resp = MessagingResponse()

    # פקודות
    cmd_reply = handle_commands(body, waid)
    if cmd_reply:
        for chunk in chunk_text(cmd_reply):
            resp.message(chunk)
        return str(resp)

    # טקסט משתמש + לוקיישן אם יש
    user_text = body.strip()
    if latitude and longitude:
        location_text = f"[user shared location] lat={latitude}, lon={longitude}"
        if label or address:
            location_text += f" | {label or address}"
        user_text = f"{user_text}\n\n{location_text}" if user_text else location_text

    # מדיה – כרגע מודיעים שטקסט בלבד
    if num_media > 0:
        for chunk in chunk_text("📎 קיבלתי קובץ/תמונה. נכון לעכשיו אני מטפל בטקסט בלבד. ספר לי במילים מה תרצה שאעשה עם המדיה."):
            resp.message(chunk)

    if not user_text:
        resp.message("👋 שלח לי שאלה או בקשה (טקסט). אפשר גם /help לעזרה.")
        return str(resp)

    history = chat_histories[waid]

    # תשובת מודל
    try:
        messages = build_messages(history, user_text)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.4,
            timeout=30,   # הוגדל מ-15 ל-30
        )
        answer = (completion.choices[0].message.content or "").strip()
        if not answer:
            answer = "מצטער, לא הצלחתי לנסח תשובה כרגע."
    except Exception as e:
        # לוג עם פרטי הכשל + fallback Echo כדי שהמשתמש לא יישאר בלי כלום
        logger.exception("OpenAI error while answering WhatsApp: %s", e)
        answer = f"Echo (fallback): {user_text[:300]}"

    # עדכון היסטוריה (user + assistant)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        del history[:-20]

    # שליחת תשובה למשתמש במקטעים קצרים
    chunks = chunk_text(answer, TWILIO_SAFE_CHUNK)
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"{chunk}\n\n({i+1}/{len(chunks)})"
        resp.message(chunk)

    return str(resp)

# ----------------------------------------------------
# הרצה מקומית (נוח לפיתוח)
# ----------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
