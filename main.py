import os
import time
import hmac
import hashlib
import logging
import threading
from urllib.parse import urlencode
from datetime import datetime

import requests

# =========================
# CONFIG
# =========================

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "15"))

BASE_URL = "https://api.mexc.com"
ORDER_ENDPOINT = "/api/v3/fiat/merchant/order/pagination"

ACTIVE_STATES = {0, 1, 2, 3}

STATE_NAMES = {
    0: "NOT_PAID",
    1: "PAID",
    2: "WAIT_PROCESS",
    3: "PROCESSING",
    4: "DONE",
    5: "CANCEL",
    6: "INVALID",
    7: "REFUSE",
    8: "TIMEOUT",
}

# =========================
# GLOBAL STATE
# =========================

state = {
    "polling_active": True,
    "started_at": datetime.utcnow(),
    "seen_orders": set(),
    "last_poll_time": None,
    "last_poll_status": "not yet polled",
    "total_alerts_sent": 0,
    "last_telegram_update_id": 0,
}

state_lock = threading.Lock()

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# TELEGRAM SEND
# =========================

def send_telegram_message(text: str, chat_id: str = None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        payload = {
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }

        response = requests.post(url, json=payload, timeout=15)

        if response.status_code != 200:
            logging.error(f"Telegram error: {response.text}")
            return False

        return True

    except Exception as e:
        logging.exception(f"Telegram send failed: {e}")
        return False

# =========================
# MEXC SIGNATURE
# =========================

def sign_query(query_string: str) -> str:
    return hmac.new(
        MEXC_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

# =========================
# FETCH ORDERS (raw)
# =========================

def fetch_orders_raw():
    """Returns (success, data_or_error_string, http_status)"""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (30 * 60 * 1000)

    params = {
        "timestamp": now_ms,
        "startTime": start_ms,
        "endTime": now_ms,
    }

    query_string = urlencode(params)
    signature = sign_query(query_string)

    url = (
        f"{BASE_URL}{ORDER_ENDPOINT}"
        f"?{query_string}&signature={signature}"
    )

    headers = {"X-MEXC-APIKEY": MEXC_API_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=20)
        status_code = response.status_code

        try:
            data = response.json()
        except Exception:
            return False, response.text[:500], status_code

        return True, data, status_code

    except Exception as e:
        return False, str(e), 0

def fetch_orders():
    """Returns list of orders or raises Exception."""
    success, data, status = fetch_orders_raw()

    if not success:
        raise Exception(f"HTTP {status}: {data}")

    if not isinstance(data, dict):
        raise Exception(f"Unexpected response: {data}")

    if data.get("code") != 0 and data.get("code") != 200:
        raise Exception(f"MEXC API error: {data}")

    orders = data.get("data", [])

    if isinstance(orders, dict):
        # Sometimes data is wrapped; try common keys
        orders = orders.get("list") or orders.get("orders") or []

    if not isinstance(orders, list):
        orders = []

    return orders

# =========================
# FORMAT ORDER MESSAGE
# =========================

def format_order_message(order: dict) -> str:
    s = order.get("state")
    state_name = STATE_NAMES.get(s, str(s))

    adv_order_no = order.get("advOrderNo", "N/A")
    fiat_amount = order.get("fiatAmount", "N/A")
    fiat_currency = order.get("fiatCurrency", "N/A")
    crypto_amount = order.get("cryptoAmount", "N/A")
    crypto_currency = order.get("currency", "N/A")
    price = order.get("price", "N/A")
    buyer_nickname = order.get("nickname", "N/A")

    return (
        "🚨 <b>New MEXC P2P Order</b>\n\n"
        f"Order ID: <code>{adv_order_no}</code>\n"
        f"Buyer: {buyer_nickname}\n"
        f"State: <b>{state_name}</b>\n\n"
        f"Fiat: {fiat_amount} {fiat_currency}\n"
        f"Crypto: {crypto_amount} {crypto_currency}\n"
        f"Price: {price}\n"
    )

# =========================
# COMMAND HANDLERS
# =========================

def cmd_help(chat_id):
    msg = (
        "<b>🤖 MEXC P2P Bot — Commands</b>\n\n"
        "/status — show bot status\n"
        "/check — test MEXC API connection\n"
        "/check_telegram — test Telegram connection\n"
        "/stop — pause order polling\n"
        "/resume — resume order polling\n"
        "/help — show this menu"
    )
    send_telegram_message(msg, chat_id)

def cmd_status(chat_id):
    with state_lock:
        polling = state["polling_active"]
        started = state["started_at"]
        last_poll = state["last_poll_time"]
        last_status = state["last_poll_status"]
        seen = len(state["seen_orders"])
        alerts = state["total_alerts_sent"]

    uptime = datetime.utcnow() - started
    uptime_str = str(uptime).split(".")[0]

    last_poll_str = (
        last_poll.strftime("%Y-%m-%d %H:%M:%S UTC")
        if last_poll else "never"
    )

    msg = (
        "<b>📊 Bot Status</b>\n\n"
        f"Polling: {'🟢 ACTIVE' if polling else '🔴 PAUSED'}\n"
        f"Uptime: {uptime_str}\n"
        f"Poll interval: {POLL_INTERVAL_SEC}s\n"
        f"Last poll: {last_poll_str}\n"
        f"Last poll result: {last_status}\n"
        f"Orders tracked: {seen}\n"
        f"Alerts sent: {alerts}\n"
    )
    send_telegram_message(msg, chat_id)

def cmd_check_mexc(chat_id):
    send_telegram_message("🔍 Testing MEXC API connection...", chat_id)

    success, data, status_code = fetch_orders_raw()

    if not success:
        msg = (
            "<b>❌ MEXC connection failed</b>\n\n"
            f"HTTP status: {status_code}\n"
            f"Error: <code>{str(data)[:300]}</code>\n\n"
            "Check your MEXC_API_KEY and MEXC_SECRET."
        )
        send_telegram_message(msg, chat_id)
        return

    if isinstance(data, dict):
        code = data.get("code")
        message = data.get("msg") or data.get("message") or ""
        orders_field = data.get("data")

        if code == 0 or code == 200:
            count = 0
            if isinstance(orders_field, list):
                count = len(orders_field)
            elif isinstance(orders_field, dict):
                lst = orders_field.get("list") or orders_field.get("orders") or []
                count = len(lst) if isinstance(lst, list) else 0

            msg = (
                "<b>✅ MEXC connection OK</b>\n\n"
                f"HTTP: {status_code}\n"
                f"API code: {code}\n"
                f"Orders in last 30 min: {count}\n\n"
                "Your API key and secret are working."
            )
        else:
            msg = (
                "<b>⚠️ MEXC responded with an error</b>\n\n"
                f"HTTP: {status_code}\n"
                f"API code: {code}\n"
                f"Message: <code>{message}</code>\n\n"
                f"Raw: <code>{str(data)[:300]}</code>\n\n"
                "The credentials may be wrong, or this endpoint "
                "may not be available for your account."
            )
    else:
        msg = (
            "<b>⚠️ Unexpected response from MEXC</b>\n\n"
            f"HTTP: {status_code}\n"
            f"Body: <code>{str(data)[:300]}</code>"
        )

    send_telegram_message(msg, chat_id)

def cmd_check_telegram(chat_id):
    ok = send_telegram_message(
        "<b>✅ Telegram connection OK</b>\n\n"
        "If you're reading this, your bot token and chat ID are correct.",
        chat_id
    )
    if not ok:
        logging.error("Telegram self-check failed.")

def cmd_stop(chat_id):
    with state_lock:
        if not state["polling_active"]:
            send_telegram_message("⚠️ Polling is already paused.", chat_id)
            return
        state["polling_active"] = False

    send_telegram_message(
        "🔴 <b>Polling paused.</b>\n\n"
        "Bot is still alive but will not send order alerts.\n"
        "Use /resume to start again.",
        chat_id
    )

def cmd_resume(chat_id):
    with state_lock:
        if state["polling_active"]:
            send_telegram_message("⚠️ Polling is already active.", chat_id)
            return
        state["polling_active"] = True

    send_telegram_message(
        "🟢 <b>Polling resumed.</b>\n\n"
        "Bot will now alert on new orders.",
        chat_id
    )

COMMANDS = {
    "/help": cmd_help,
    "/start": cmd_help,
    "/status": cmd_status,
    "/check": cmd_check_mexc,
    "/check_mexc": cmd_check_mexc,
    "/check_telegram": cmd_check_telegram,
    "/stop": cmd_stop,
    "/resume": cmd_resume,
    "/start_polling": cmd_resume,
}

# =========================
# TELEGRAM LISTENER (long polling)
# =========================

def telegram_listener_loop():
    """Listens for incoming Telegram commands in a background thread."""
    logging.info("Telegram listener started.")

    while True:
        try:
            with state_lock:
                offset = state["last_telegram_update_id"] + 1

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 25, "offset": offset}

            response = requests.get(url, params=params, timeout=35)

            if response.status_code != 200:
                logging.error(f"getUpdates error: {response.text}")
                time.sleep(5)
                continue

            data = response.json()

            if not data.get("ok"):
                logging.error(f"getUpdates not ok: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                update_id = update.get("update_id", 0)

                with state_lock:
                    if update_id > state["last_telegram_update_id"]:
                        state["last_telegram_update_id"] = update_id

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = (message.get("text") or "").strip()

                if not text or not chat_id:
                    continue

                # Only accept commands from the configured chat
                if chat_id != str(TELEGRAM_CHAT_ID):
                    logging.warning(
                        f"Ignored message from unauthorized chat {chat_id}"
                    )
                    continue

                # Extract command (strip @botname if present)
                command = text.split()[0].split("@")[0].lower()

                handler = COMMANDS.get(command)
                if handler:
                    logging.info(f"Command received: {command}")
                    try:
                        handler(chat_id)
                    except Exception as e:
                        logging.exception(f"Command handler error: {e}")
                        send_telegram_message(
                            f"❌ Error running {command}: {e}", chat_id
                        )
                else:
                    send_telegram_message(
                        f"Unknown command: <code>{command}</code>\n"
                        "Send /help to see available commands.",
                        chat_id
                    )

        except Exception as e:
            logging.exception(f"Telegram listener error: {e}")
            time.sleep(5)

# =========================
# POLLING LOOP
# =========================

def polling_loop():
    """Fetches MEXC orders and sends alerts."""
    logging.info("Polling loop started.")

    # Prime existing orders
    try:
        existing_orders = fetch_orders()
        with state_lock:
            for order in existing_orders:
                adv_order_no = order.get("advOrderNo")
                if adv_order_no:
                    state["seen_orders"].add(adv_order_no)
            primed_count = len(state["seen_orders"])
        logging.info(f"Primed {primed_count} existing orders.")
    except Exception as e:
        logging.exception(f"Initial fetch failed: {e}")

    while True:
        with state_lock:
            active = state["polling_active"]

        if not active:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        try:
            orders = fetch_orders()

            with state_lock:
                state["last_poll_time"] = datetime.utcnow()
                state["last_poll_status"] = f"OK ({len(orders)} orders)"

            for order in orders:
                s = order.get("state")
                if s not in ACTIVE_STATES:
                    continue

                adv_order_no = order.get("advOrderNo")
                if not adv_order_no:
                    continue

                with state_lock:
                    if adv_order_no in state["seen_orders"]:
                        continue
                    state["seen_orders"].add(adv_order_no)

                message = format_order_message(order)
                logging.info(f"New order detected: {adv_order_no}")
                send_telegram_message(message)

                with state_lock:
                    state["total_alerts_sent"] += 1

            # Trim seen set
            with state_lock:
                if len(state["seen_orders"]) > 500:
                    state["seen_orders"] = set(
                        list(state["seen_orders"])[-250:]
                    )
                    logging.info("Trimmed seen_orders set.")

        except Exception as e:
            logging.exception(f"Polling error: {e}")
            with state_lock:
                state["last_poll_time"] = datetime.utcnow()
                state["last_poll_status"] = f"ERROR: {str(e)[:100]}"

        time.sleep(POLL_INTERVAL_SEC)

# =========================
# ENTRY
# =========================

def main():
    if not all([MEXC_API_KEY, MEXC_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
        raise Exception("Missing required environment variables.")

    # Startup ping with command hint
    send_telegram_message(
        "✅ <b>MEXC P2P notification bot started.</b>\n\n"
        "Send /help to see available commands."
    )

    # Start Telegram listener in background thread
    listener_thread = threading.Thread(
        target=telegram_listener_loop, daemon=True
    )
    listener_thread.start()

    # Run polling loop in main thread (so the container stays alive)
    polling_loop()

if __name__ == "__main__":
    main()
