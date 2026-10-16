# Telegram Host Monitor (Render Web Service)

This bot runs as a Render Web Service with a small HTTP health endpoint at `/` and `/health`.
It provides low-rate TCP reachability monitoring, not UDP attack traffic.

## Commands
- `/start`
- `/attack <public-ip> <port> <duration>` — TCP connectivity checks every 5 seconds, duration 1–300 seconds.
- `/status`
- `/help`

## Deploy
1. Upload these files to the root of a GitHub repository.
2. Create a Render Web Service from the repository, or use the included Blueprint.
3. Set `TELEGRAM_BOT_TOKEN` in Render Environment settings.
4. Build command: `pip install -r requirements.txt`
5. Start command: `python main.py`

Render supplies the `PORT` environment variable. The health server binds to `0.0.0.0:$PORT`.
Use only on systems you own or are authorized to test.
