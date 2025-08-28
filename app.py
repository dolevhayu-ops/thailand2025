import os
import random
from datetime import datetime, date
from flask import Flask, request, jsonify
from twilio.rest import Client

app = Flask(__name__)

# ==== Environment ====
ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
CONV_SID    = os.environ.get("TWILIO_CONVERSATION_SID", "").strip()

client = Client(ACCOUNT_SID, AUTH_TOKEN)

# ==== Random 'ars' vibe helpers ====
PFX = [
    "יאללה אחי, ", "תקשיב שניה, ", "סמוך עליי, ", "נו באמת... ",
    "חלס, ", "כפרה, ", "שומע? ", "מלך, ", "טיל בליסטי, ", "אחי הקוסם, "
]
ACKS = [
    "הופה! קיבלתי.", "סגור יבחרי.", "עליי!", "אש, נקלט.",
    "חמסה חמסה, טופל.", "נרשם, בוס.", "מוכן יציאה."
]
EMOJIS = ["🔥", "😎", "💪", "🛫", "🧳", "🗓️", "✅", "🫡", "🤙", "🚀"]

def rnd(seq):
    return random.choice(seq)

def flair(text: str) -> str:
    return f"{rnd(PFX)}{text} {rnd(EMOJIS)}"

# ==== Twilio send helper ====
def send_msg(body: str):
    if not (ACCOUNT_SID and AUTH_TOKEN and CONV_SID):
        print("Missing env vars for Twilio; cannot send:", body)
        return
    try:
        # v1 API (avoids deprecation warning)
        client.conversations.v1.conversations(CONV_SID).messages.create(
            author="bot",
            body=body
        )
    except Exception as e:
        print("Failed to send message via Twilio:", e)

# ==== Commands ====
def handle_help():
    body = (
        "אני כאן בשבילך, מאסטר.\n"
        "פקודות זמינות:\n"
        "• HELP – מה אני יודע לעשות\n"
        "• COUNTDOWN YYYY-MM-DD – ספירה אחורה עד תאריך יעד\n"
        "• LIST TRIP – תקציר (דמו בשלב זה)\n\n"
        "תשלח קבצים/סקרינשוטים/לינקים — בבילד הבא אני שומר הכל חכם."
    )
    return flair(body)

def parse_date(s: str):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def handle_countdown(args: str):
    parts = args.strip().split()
    if not parts:
        return flair("נו באמת... תן תאריך ככה: COUNTDOWN 2025-09-12")
    d = parse_date(parts[0])
    if not d:
        return flair("מה זה התאריך הזה אחי? תן בפורמט YYYY-MM-DD, למשל 2025-09-12")
    today = date.today()
    delta = (d - today).days
    if delta > 1:
        return flair(f"נשארו {delta} ימים עד היעד. להדק חגורות!")
    elif delta == 1:
        return flair("נשאר יום אחד! תארוז את הכפכפים 😎")
    elif delta == 0:
        return flair("היום! יאללה לשדה ✈️")
    else:
        return flair(f"עברו {-delta} ימים מהתאריך הזה… איחרת אחי, אבל נזרום בפעם הבאה 😉")

def handle_list_trip():
    body = (
        "בשלב הזה זה דמו — עוד לא שמרתי טיסות/מלונות.\n"
        "תזרוק לי פרטים/קבצים/סקרינשוטים ונארגן הכל בגירסה הבאה."
    )
    return flair(body)

def handle_unknown(text: str):
    body = (
        f"{rnd(ACKS)}\n"
        f"שלחת: “{text}”.\n"
        "לספירה כתוב: COUNTDOWN YYYY-MM-DD, ולעזרה כתוב: HELP."
    )
    return flair(body)

# ==== Routes ====
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    data = request.form.to_dict()
    print("Incoming from Twilio:", data)

    # אל תענה לעצמך (כדי לא ליצור לולאת אקו)
    if (data.get("Author") or "").lower() == "bot":
        return ("OK", 200)

    text = (data.get("Body") or "").strip()
    cmd = text.upper()

    if cmd == "HELP":
        resp = handle_help()
    elif cmd.startswith("COUNTDOWN"):
        resp = handle_countdown(text[len("COUNTDOWN"):])
    elif cmd in ("LIST TRIP", "LIST"):
        resp = handle_list_trip()
    else:
        resp = handle_unknown(text)

    send_msg(resp)
    return ("OK", 200)

# אופציונלי: נקודות לקרון ברנדר (יומי/שבועי)
@app.route("/tasks/daily", methods=["GET", "POST"])
def task_daily():
    send_msg(flair("דוח יומי מזורז בדרך אליך."))
    return ("OK", 200)

@app.route("/tasks/weekly", methods=["GET", "POST"])
def task_weekly():
    send_msg(flair("סקירה שבועית: (דמו) עוד רגע זה נהיה רציני 🍿"))
    return ("OK", 200)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
