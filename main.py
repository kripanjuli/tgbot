import os
import asyncio
import logging
import socket
import ipaddress
import time
from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MAX_DURATION = 300
CHECK_INTERVAL = 5
active_checks = {}

MENU = ReplyKeyboardMarkup(
    [["/attack", "/status"], ["/help", "/start"]],
    resize_keyboard=True
)

def valid_target(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        # Reject unspecified, multicast, loopback, and link-local targets.
        return not (ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 Host Monitor Bot\n\n"
        "Commands:\n"
        "/attack <ip> <port> <duration> — run a low-rate TCP reachability check\n"
        "/status — show active checks (refreshes every 5 seconds)\n"
        "/help — usage\n\n"
        "This bot checks TCP connectivity only; it does not send attack traffic.",
        reply_markup=MENU,
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage:\n"
        "/attack 1.1.1.1 443 60\n\n"
        "Duration must be 1–300 seconds. Checks run at most once every 5 seconds "
        "and only test whether the specified TCP port accepts a connection.\n"
        "Use only against systems you own or are authorized to test.",
        reply_markup=MENU,
    )

async def check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage: /attack <ip> <port> <duration>\nExample: /attack 1.1.1.1 443 60",
            reply_markup=MENU,
        )
        return

    host, port_s, duration_s = context.args
    if not valid_target(host):
        await update.message.reply_text("Please provide a valid public IP address.")
        return

    try:
        port = int(port_s)
        duration = int(duration_s)
        if not 1 <= port <= 65535:
            raise ValueError
        if not 1 <= duration <= MAX_DURATION:
            await update.message.reply_text("Duration must be between 1 and 300 seconds.")
            return
    except ValueError:
        await update.message.reply_text("Port must be 1–65535 and duration 1–300 seconds.")
        return

    key = (update.effective_chat.id, host, port)
    if key in active_checks:
        await update.message.reply_text("A check for this host and port is already running.")
        return

    record = {
        "host": host, "port": port, "duration": duration,
        "started": time.time(), "last_result": "Waiting for first check",
        "checks": 0, "online": 0, "offline": 0,
        "chat_id": update.effective_chat.id, "message_id": None,
    }
    active_checks[key] = record
    msg = await update.message.reply_text("🔎 Starting TCP reachability monitor…", reply_markup=MENU)
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
            text = (
                "📊 TCP Reachability Status\n"
                f"Target: `{record['host']}:{record['port']}`\n"
                f"State: {record['last_result']}\n"
                f"Elapsed: {elapsed}s / {record['duration']}s\n"
                f"Remaining: {remaining}s\n"
                f"Checks: {record['checks']} | Reachable: {record['online']} | Failed: {record['offline']}\n\n"
                "Refresh interval: 5 seconds"
            )
            try:
                await app.bot.edit_message_text(
                    chat_id=record["chat_id"], message_id=record["message_id"],
                    text=text, parse_mode="Markdown"
                )
            except Exception:
                pass
            await asyncio.sleep(min(CHECK_INTERVAL, max(0, end_at - time.time())))
        try:
            await app.bot.edit_message_text(
                chat_id=record["chat_id"], message_id=record["message_id"],
                text=(
                    "✅ Monitor finished\n"
                    f"Target: {record['host']}:{record['port']}\n"
                    f"Checks: {record['checks']} | Reachable: {record['online']} | Failed: {record['offline']}"
                )
            )
        except Exception:
            pass
    finally:
        active_checks.pop(key, None)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_checks:
        await update.message.reply_text("No checks are currently running.", reply_markup=MENU)
        return
    lines = ["📊 Active monitors:"]
    now = time.time()
    for r in active_checks.values():
        elapsed = int(now - r["started"])
        remaining = max(0, r["duration"] - elapsed)
        lines.append(
            f"• {r['host']}:{r['port']} — {r['last_result']} "
            f"({remaining}s left; {r['checks']} checks)"
        )
    await update.message.reply_text("\n".join(lines), reply_markup=MENU)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Show main menu"),
        BotCommand("attack", "Start a TCP reachability check"),
        BotCommand("status", "Show ongoing checks"),
        BotCommand("help", "Show usage"),
    ])

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("status", status_command))
    app.run_polling()

if __name__ == "__main__":
    main()
