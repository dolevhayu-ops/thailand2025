# app.py
# -*- coding: utf-8 -*-
"""
WhatsApp Chatbot on Flask + Twilio + OpenAI
- /           : בריאות/בדיקה
- /health     : בריאות/בדיקה
- /twilio/webhook : Webhook ל־Twilio WhatsApp (POST)

דרישות סביבתיות (Environment Variables):
- OPENAI_API_KEY              : חובה (מפתח OpenAI)
- OPENAI_MODEL                : אופציונלי (ברירת מחדל: gpt-4o-mini)
- SYSTEM_PROMPT               : אופציונלי (הנחיית מערכת לבוט)
- VERIFY_TWILIO_SIGNATURE     : 'true' כדי לאכוף ולידציית חתימה (ברירת מחדל: 'false')
- TWILIO_AUTH_TOKEN           : חובה אם VERIFY_TWILIO_SIGNATURE=true
- LOG_LEVEL                   : אופציונלי (INFO/DEBUG)

טיפים ל-Render:
- Start Command: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2
"""

import os
import logging
from collections import defaultdict
from typing import List

from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# OpenAI SDK (גרסת ה-SDK המודרנית)
from openai import OpenAI

# ----------------------------------------------------
# קונפיגורציה ולוגים
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

# יוזמה של לקוח OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
if not client.api_key:
    logger.error("OPENAI_API_KEY is missing!")
    raise RuntimeError("OPENAI_API_KEY is not set")

app = Flask(__name__)

# זיכרון שיחה בזיכרון (ל־PoC). לפרודקשן מומלץ Redis/DB.
chat_histories = defaultdict(list)  # key = from_waid ; value = list of dicts [{role, content}]

# מגבלת אורך הודעה בווטסאפ דרך טוויליו – כדי להיות סופר־זהירים נחתוך ל־1500 תווים
# (Twilio ממליצים עד ~1600 תווים לכל הודעה; וואטסאפ עצמו תומך עד 4096) :contentReference[oaicite:0]{index=0}
TWILIO_SAFE_CHUNK = 1500


# ----------------------------------------------------
# עזר: ולידציית בקשות מטוויליו (אופציונלי)
# ----------------------------------------------------
def _validated_twilio_request() -> bool:
    """Validate X-Twilio-Signature header (אם הופעל)."""
    if not VERIFY_TWILIO_SIGNATURE:
        return True
    if not TWILIO_AUTH_TOKEN:
        logger.warning("VERIFY_TWILIO_SIGNATURE=true אבל חסר TWILIO_AUTH_TOKEN")
        return False

    validator = RequestValidator(TWILIO_AUTH_TOKEN)

    # Render ופרוקסי לעתים משנים את הפרוטוקול ל-http; משחזרים ל-https אם התקבל header תואם
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
# עזר: חיתוך הודעה ארוכה למקטעים
# ----------------------------------------------------
def chunk_text(s: str, size: int = TWILIO_SAFE_CHUNK) -> List[str]:
    s = s or ""
    return [s[i : i + size] for i in range(0, len(s), size)] or [""]


# ----------------------------------------------------
# עזר: בניית הודעה ל-OpenAI מתוך היסטוריה + טקסט אחרון
# ----------------------------------------------------
def build_messages(history: List[dict], user_text: str) -> List[dict]:
    # מגבילים היסטוריה לעומק 8-10 פריטים כדי לשמור על מהירות ועלויות
    trimmed = history[-8:] if len(history) > 8 else history[:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(trimmed)
    messages.append({"role": "user", "content": user_text})
    return messages


# ----------------------------------------------------
# עזר: מענה מקומי לפקודות
# ----------------------------------------------------
def handle_commands(body: str, waid: str):
    cmd = body.strip().lower()
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


@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    if not _validated_twilio_request():
        abort(403)

    # פרמטרים שמעניינים אותנו מה-webhook של טוויליו
    from_ = request.form.get("From", "")          # דוגמה: 'whatsapp:+9725xxxxxxxx'
    waid = request.form.get("WaId", from_)        # מזהה ווטסאפ גלובלי (לפעמים מגיע בנפרד) :contentReference[oaicite:1]{index=1}
    body = request.form.get("Body", "") or ""
    num_media = int(request.form.get("NumMedia", "0") or 0)

    # לוקיישן (אם נשלח): Latitude / Longitude / Address / Label :contentReference[oaicite:2]{index=2}
    latitude = request.form.get("Latitude")
    longitude = request.form.get("Longitude")
    address = request.form.get("Address")
    label = request.form.get("Label")

    # פקודות מהירות
    cmd_reply = handle_commands(body, waid)
    resp = MessagingResponse()

    if cmd_reply:
        for chunk in chunk_text(cmd_reply):
            resp.message(chunk)
        return str(resp)

    # אם יש לוקיישן – ננסח טקסט עזר ונוסיף לשיחה
    location_text = None
    if latitude and longitude:
        location_text = f"[user shared location] lat={latitude}, lon={longitude}"
        if label or address:
            location_text += f" | {label or address}"

    # אם קיבלנו מדיה – מודיעים למשתמש שהבוט עובד כרגע טקסטואלית
    if num_media > 0:
        # אפשר בהמשך להוריד את המדיה עם קרדנצ'יאלס של Twilio ולשלוח לניתוח חיצוני
        media_msg = (
            "📎 קיבלתי קובץ/תמונה. נכון לעכשיו אני מטפל בטקסט בלבד. "
            "ספר לי במילים מה תרצה שאעשה עם המדיה."
        )
        for chunk in chunk_text(media_msg):
            resp.message(chunk)
        # ממשיכים גם לנתח את הטקסט שצורף (Body) אם יש

    user_text = body.strip()
    if location_text:
        user_text = f"{user_text}\n\n{location_text}" if user_text else location_text

    if not user_text:
        resp.message("👋 שלח לי שאלה או בקשה (טקסט). אפשר גם /help לעזרה.")
        return str(resp)

    # מוסיפים להיסטוריה ושולחים ל-OpenAI
    history = chat_histories[waid]
    try:
        messages = build_messages(history, user_text)

        # שימוש ב-Chat Completions (נתמך ומומלץ לשימושי צ'אט רגילים) :contentReference[oaicite:3]{index=3}
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.4,
            timeout=15,  # שניות
        )
        answer = completion.choices[0].message.content.strip() if completion and completion.choices else "מצטער, לא הצלחתי לנסח תשובה כרגע."
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        answer = "❗ אירעה שגיאה זמנית בעיבוד הבקשה. נסה שוב עוד רגע."

    # עדכון היסטוריה (שומרים גם את מסקנת המודל)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    # שמירה על גודל סביר
    if len(history) > 20:
        del history[:-20]

    # חיתוך תשובה להודעות קצרות
    chunks = chunk_text(answer, TWILIO_SAFE_CHUNK)
    for i, chunk in enumerate(chunks):
        # שורת סטטוס קטנה אם יש פיצול
        if len(chunks) > 1:
            chunk = f"{chunk}\n\n({i+1}/{len(chunks)})"
        resp.message(chunk)

    return str(resp)


# ----------------------------------------------------
# הרצה מקומית (לנוחות)
# ----------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
