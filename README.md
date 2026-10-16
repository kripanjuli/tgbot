# Telegram TCP Monitor — Render Web Service

Provides an HTTP health endpoint (`/`, `/health`) and Telegram commands:
- `/start`
- `/attack <public-ip> <port> <duration> [DM|GROUP]`
- `/status`
- `/help`

Jobs perform TCP reachability checks every 5 seconds for 1–300 seconds. The five numbered
slots are local dashboard slots for active checks, not separate physical servers. This
project does not invoke UDP attack APIs or send attack traffic.

Deploy as a Render Web Service with build command `pip install -r requirements.txt`
and start command `python main.py`. Set `TELEGRAM_BOT_TOKEN` in Render Environment.
