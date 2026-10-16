import os
import asyncio
import logging
import ipaddress
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MAX_DURATION = 300
CHECK_INTERVAL = 5
IST = ZoneInfo("Asia/Kolkata")
active_checks = {}
completed_jobs = {}

MENU = ReplyKeyboardMarkup(
    [["/attack", "/status"], ["/help", "/start"]],
    resize_keyboard=True,
)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"Telegram Bot is running" if self.path == "/" else b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"Not Found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        logging.info("HTTP health check: " + fmt, *args)

def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def now_ist():
    return datetime.now(IST)

def fmt_time(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S IST")

def valid_public_ip(host):
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False

def server_number_for_key(key):
    # Dashboard slots are display slots, not separate remote servers.
    # Assign each active monitoring task to the first free display slot.
    used = {r.get("server") for r in active_checks.values() if r.get("server")}
    for number in range(1, 6):
        if number not in used:
            return number
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 TCP Monitor Bot\n\n"
        "Commands:\n"
        "/attack <ip> <port> <duration> [DM|GROUP]\n"
        "/status — refresh the server dashboard\n"
        "/help — usage\n\n"
        "This bot checks TCP reachability only. It does not send attack traffic.",
        reply_markup=MENU,
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage:\n"
        "/attack 1.1.1.1 443 60\n"
        "/attack 1.1.1.1 443 60 DM\n\n"
        "Duration: 1–300 seconds. Checks occur every 5 seconds. "
        "Only monitor systems you own or are authorized to test.",
        reply_markup=MENU,
    )

async def check_port(host, port, timeout=3.0):
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

def build_status_text():
    now = time.time()
    lines = ["🖥️ SERVER STATUS", ""]
    by_server = {r["server"]: r for r in active_checks.values() if r.get("server")}
    for server in range(1, 6):
        record = by_server.get(server)
        if record:
            remaining = max(0, int(record["started"] + record["duration"] - now))
            lines.extend([
                f"Server {server}: 🔴 BUSY",
                f"├── Target: {record['host']}:{record['port']} ({record['label']})",
                f"└── Time Left: {remaining}s",
                "",
            ])
        else:
            lines.extend([f"Server {server}: 🟢 FREE", ""])
    return "\n".join(lines).rstrip()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_status_text(), reply_markup=MENU)

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) not in (3, 4):
        await update.message.reply_text(
            "Usage: /attack <public-ip> <port> <duration> [DM|GROUP]\n"
            "Example: /attack 1.1.1.1 443 60 DM",
            reply_markup=MENU,
        )
        return

    host, port_s, duration_s = context.args[:3]
    label = context.args[3].upper() if len(context.args) == 4 else "DM"
    if label not in ("DM", "GROUP"):
        await update.message.reply_text("Optional label must be DM or GROUP.")
        return
    if not valid_public_ip(host):
        await update.message.reply_text("Please provide a valid public IP address.")
        return

    try:
        port, duration = int(port_s), int(duration_s)
    except ValueError:
        await update.message.reply_text("Port and duration must be numbers.")
        return
    if not 1 <= port <= 65535:
        await update.message.reply_text("Port must be 1–65535.")
        return
    if not 1 <= duration <= MAX_DURATION:
        await update.message.reply_text("Duration must be between 1 and 300 seconds.")
        return

    key = (update.effective_chat.id, host, port)
    if key in active_checks:
        await update.message.reply_text("A monitor for this target is already running.")
        return

    server = server_number_for_key(key)
    if server is None:
        await update.message.reply_text("All five monitoring slots are busy. Try again later.")
        return

    started = time.time()
    record = {
        "host": host, "port": port, "duration": duration, "label": label,
        "started": started, "last_result": "Waiting for first check",
        "checks": 0, "online": 0, "offline": 0,
        "chat_id": update.effective_chat.id, "message_id": None, "server": server,
    }
    active_checks[key] = record

    start_text = (
        "💀 MONITOR STARTED 💀\n\n"
        f"Target: {host}:{port}\n"
        f"Duration: {duration}s\n"
        "Method: TCP\n"
        f"Server: {server}\n\n"
        f"📅 Started: {fmt_time(started)}\n"
        f"⏰ Ends: {fmt_time(started + duration)}"
    )
    msg = await update.message.reply_text(start_text, reply_markup=MENU)
    record["message_id"] = msg.message_id
    asyncio.create_task(run_monitor(key, context.application))

async def run_monitor(key, app):
    record = active_checks[key]
    end_at = record["started"] + record["duration"]
    try:
        while time.time() < end_at:
            ok = await check_port(record["host"], record["port"])
            record["checks"] += 1
            record["online" if ok else "offline"] += 1
            record["last_result"] = "🟢 Reachable" if ok else "🔴 Not reachable"

            elapsed = int(time.time() - record["started"])
            remaining = max(0, int(end_at - time.time()))
            status_text = (
                "📊 TCP MONITOR STATUS\n\n"
                f"Target: {record['host']}:{record['port']}\n"
                f"State: {record['last_result']}\n"
                f"Server: {record['server']}\n"
                f"Elapsed: {elapsed}s / {record['duration']}s\n"
                f"Remaining: {remaining}s\n"
                f"Checks: {record['checks']} | Reachable: {record['online']} | Failed: {record['offline']}\n\n"
                "Refresh interval: 5 seconds"
            )
            try:
                await app.bot.edit_message_text(
                    chat_id=record["chat_id"], message_id=record["message_id"], text=status_text
                )
            except Exception:
                logging.exception("Could not update monitor message")

            sleep_for = min(CHECK_INTERVAL, max(0, end_at - time.time()))
            if sleep_for:
                await asyncio.sleep(sleep_for)

        completed = time.time()
        completion_text = (
            "✅ MONITOR COMPLETE ✅\n\n"
            f"Target: {record['host']}:{record['port']}\n"
            f"Duration: {record['duration']}s\n"
            "Method: TCP\n"
            f"Server: {record['server']}\n\n"
            f"📅 Started: {fmt_time(record['started'])}\n"
            f"📅 Completed: {fmt_time(completed)}\n\n"
            f"Checks: {record['checks']} | Reachable: {record['online']} | Failed: {record['offline']}"
        )
        try:
            await app.bot.edit_message_text(
                chat_id=record["chat_id"], message_id=record["message_id"], text=completion_text
            )
        except Exception:
            logging.exception("Could not update completion message")
        completed_jobs[key] = {**record, "completed": completed}
    finally:
        active_checks.pop(key, None)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Show main menu"),
        BotCommand("attack", "Start a TCP reachability monitor"),
        BotCommand("status", "Show ongoing monitoring jobs"),
        BotCommand("help", "Show usage"),
    ])

def main():
    Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("status", status_command))
    app.run_polling()

if __name__ == "__main__":
    main()
