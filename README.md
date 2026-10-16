# Telegram TCP Reachability Monitor

A Render worker Telegram bot for low-rate TCP reachability checks. It does **not**
send UDP floods or invoke an attack API.

## Commands
- `/start`
- `/attack <public-ip> <port> <duration>` — checks TCP connectivity every 5 seconds, max 300 seconds.
- `/status`
- `/help`

## Deploy on Render
1. Push these files to a GitHub repository.
2. In Render, create a new Blueprint from that repository, or create a Background Worker.
3. Add environment variable `TELEGRAM_BOT_TOKEN` with the token from BotFather.
4. Deploy. Keep only one worker instance running to avoid Telegram polling conflicts.

Only monitor hosts and ports you own or are authorized to test.
