# Twilio Conversations Trip Bot

A simple Flask-based bot that connects to Twilio Conversations and WhatsApp.

## Endpoints
- `/health` → health check
- `/twilio/webhook` → webhook for Twilio to deliver messages

## Run locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Deploy to Render
- Create a new Web Service
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn -b 0.0.0.0:8080 app:app`
