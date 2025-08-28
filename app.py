import os
import re
import random
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify
from twilio.rest import Client

app = Flask(__name__)

# ===== Env & Twilio client =====
ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
CONV_SID = os.environ.get("TWILIO_CONVERSATION_SID", "").strip()
TZ = os.environ.get("TZ", "Asia/Jerusalem")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

# ===== Helper: send message into conversation =====
def send_msg(body: str):
    if not (ACCOUNT_SID and AUTH_TOKEN and CONV_SID):
        print("Missing Twilio env vars, cannot send:", body)
        return
    client.conversations.conversations(CONV_SID).messages.create(
        author="bot",
        body=body
    )

# ===== “ערס vibes” snippets (randomized) =====
PFX = [
    "יאללה אחי, ", "תקשיב שניה, ", "סמוך עליי, ",
    "נו באמת... ", "חלס, ", "כפרה, ", "שומע? ",
    "מלך, ", "טיל בליסטי, ", "אחי הקוסם, "
]

ACKS = [
    "הופה! קיבלתי.", "סגור יבחרי.", "עליי!", "אש, נקלט.",
    "חמסה חמסה, טופל.", "נרשם, בוס.", "מוכן יציאה."
]

EMOJIS = ["🔥", "😎", "💪", "🛫", "🧳", "🗓️", "✅", "🫡", "🤙", "🚀"]

def rnd(seq):  # random choice safe
    return random.choice(seq)

def flair(text):
    # Random prefix + emoji sprinkle
    return f"{rnd(PFX)}{text} {rnd(EMOJIS)}"

# ===== Command handlers =====
def handle_help():
    body = (
        "אני פה בשבילך, מאסטר.\n"
        "פקודות זמינות:\n"
        "• HELP – מה אני יודע לעשות\n"
        "• COUNTDOWN YYYY-MM-DD – ספירה אחורה עד תאריך היעד (למשל: COUNTDOWN 2025-09-12)\n"
        "• LIST TRIP – תקציר טיסות/מלונות (בינתיים דמו)\n"
        "\n"
        "תשלח קבצים/סקרינשוטים/לינקים, נבנה מזה טיול פצצה 💣"
    )
    return flair(body)

def parse_date_yyyy_mm_dd(s: str):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def handle_countdown(args: str):
    # Expect: YYYY-MM-DD
    parts = args.strip().split()
    if not parts:
        return flair("נו באמת... תן תאריך ככה: COUNTDOWN 2025-09-12")

    d = parse_date_yyyy_mm_dd(parts[0])
    if not d:
        return flair("מה זה התאריך הזה אחי? תן בפורמט YYYY-MM-DD, למשל 2025-09-12")

    today = date.today()
    delta = (d - today).days
    if delta > 1:
        return flair(f"נשארו {delta} ימים עד היעד. להדק חגורות, יש המראה!")
    elif delta == 1:
        return flair("נשאר יום אחד! תארוז את הכפכפים 😎")
    elif delta == 0:
        return flair("היום! תזיז את עצמך לשדה ✈️")
    else:
        return flair(f"עברו {-delta} ימים מהתאריך הזה… איחרת אחי, אבל זורם איתך בפעם הבאה 😉")

def handle_list_trip():
    # Placeholder until DB/Parser added
    body = (
        "בשלב הזה אני עוד מסדר את הזכרון…\n"
        "כרגע אין רשומות של טיסות/מלונות שמורות אצלי.\n"
        "תזרוק לי פרטים/קבצים/סקרינשוטים — ואני אשמור לך הכל מסודר בבילד הבא."
    )
    return flair(body)

def handle_unknown(user_text: str):
    body = (
        f"{rnd(ACKS)}\n"
        f"שלחת: “{user_text}”.\n"
        "אם אתה רוצה ספירה – כתוב: COUNTDOWN YYYY-MM-DD.\n"
        "ואם בא לך עזרה – כתוב: HELP."
    )
    return flair(body)

# ===== Flask routes =====
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    # Twilio Conversations posts application/x-www-form-urlencoded
    data = request.form.to_dict()
    print("Incoming from Twilio:", data)

    text = (data.get("Body") or "").strip()
    cmd = text.upper()

    # Simple command parsing
    if cmd == "HELP":
        resp = handle_help()
    elif cmd.startswith("COUNTDOWN"):
        resp = handle_countdown(text[len("COUNTDOWN"):])
    elif cmd == "LIST TRIP" or cmd == "LIST":
        resp = handle_list_trip()
    else:
        resp = handle_unknown(text)

    try:
        send_msg(resp)
    except Exception as e:
        # Log but still 200 to prevent Twilio retries storm
        print("Failed to send message:", e)

    return ("OK", 200)

# ===== optional: cron-friendly endpoints for Render =====
@app.route("/tasks/daily", methods=["POST", "GET"])
def task_daily():
    # Example daily note (at 09:00 Asia/Jerusalem via Render Cron Job)
    try:
        send_msg(flair("דוח יומי מזורז: בודק ספירה, תאריכים ועדכונים."))
    except Exception as e:
        print("Daily task send failed:", e)
    return ("OK", 200)

@app.route("/tasks/weekly", methods=["POST", "GET"])
def task_weekly():
    # Example weekly summary placeholder
    try:
        send_msg(flair("סקירה שבועית בדרך לנסיך: (דמו) עוד רגע זה נהיה רציני 🍿"))
    except Exception as e:
        print("Weekly task send failed:", e)
    return ("OK", 200)

if __name__ == "__main__":
    # Render binds a PORT env var; default to 8080 locally
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
