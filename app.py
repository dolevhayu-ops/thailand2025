from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/twilio/webhook", methods=["POST"])
def twilio_webhook():
    # כאן ייכנס הלוגיקה של ניתוח הודעות בהמשך
    data = request.form.to_dict()
    print("Incoming from Twilio:", data)
    return "OK", 200

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
