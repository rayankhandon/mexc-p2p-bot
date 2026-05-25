import os
import time
import hmac
import hashlib
import json
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

# MEXC sends state as a STRING. Alert only on active states (a new order
# arriving will typically be NOT_PAID or PAID).
ACTIVE_STATES = {"NOT_PAID", "PAID", "WAIT_PROCESS", "PROCESSING"}

# Optional: ignore these — they're old/closed and shouldn't alert
INACTIVE_STATES = {"DONE", "CANCEL", "INVALID", "REFUSE", "TIMEOUT"}

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

def send_telegram_message(text: str, chat_id: str = None, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        if len(text) > 4000:
            text = text[:4000] + "\n\n... (truncated)"

        payload = {
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        response = requests.post(url, json=payload, timeout=15)

        if response.status_code != 200:
            logging.error(f"Telegram error: {response.text}")
            if parse_mode:
                payload.pop("parse_mode", None)
                requests.post(url, json=payload, timeout=15)
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
# FETCH ORDERS
# =========================

def fetch_orders_raw(window_minutes: int = 30):
    """Returns (success, data_or_error_string, http_status)"""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (window_minutes * 60 * 1000)

    params = {
        "timestamp": now_ms,
        "startTime": start_ms,
        "endTime": now_ms,
    }

    query_string = urlencode(params)
    signature = sign_query(query_string)

    url = f"{BASE_URL}{ORDER_ENDPOINT}?{query_string}&signature={signature}"
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

def extract_orders_list(data):
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        for key in ("list", "orders", "records", "items", "rows"):
            v = inner.get(key)
            if isinstance(v, list):
                return v
    return []

def fetch_orders(window_minutes: int = 30):
    success, data, status = fetch_orders_raw(window_minutes)
    if not success:
        raise Exception(f"HTTP {status}: {data}")
    if not isinstance(data, dict):
        raise Exception(f"Unexpected response: {data}")
    if data.get("code") not in (0, 200):
        raise Exception(f"MEXC API error: {data}")
    return extract_orders_list(data)

# =========================
# ORDER PARSING (correct field names based on real MEXC response)
# =========================

def get_order_id(order: dict) -> str:
    return order.get("advOrderNo") or order.get("orderNo") or ""

def get_order_state(order: dict) -> str:
    """State comes as a string like 'PAID', 'TIMEOUT', etc."""
    s = order.get("state")
    return str(s) if s is not None else "UNKNOWN"

def get_buyer_name(order: dict) -> str:
    user_info = order.get("userInfo") or {}
    if isinstance(user_info, dict):
        return user_info.get("nickName") or "Unknown"
    return "Unknown"

def format_timestamp(ms):
    """Convert MEXC's millisecond timestamp to readable time."""
    if not ms:
        return "N/A"
    try:
        return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ms)

def format_order_message(order: dict) -> str:
    order_id = get_order_id(order)
    state_name = get_order_state(order)
    buyer = get_buyer_name(order)

    amount = order.get("amount", "N/A")
    fiat_unit = order.get("fiatUnit", "")
    crypto = order.get("coinName", "")
    crypto_qty = order.get("tradableQuantity", "N/A")
    price = order.get("price", "N/A")
    side = order.get("side", "")
    create_time = format_timestamp(order.get("createTime"))

    # The bot operator's side from MEXC's perspective
    side_emoji = "💰" if side == "SELL" else "🛒"

    return (
        f"🚨 <b>New MEXC P2P Order</b>\n\n"
        f"{side_emoji} Your side: <b>{side}</b>\n"
        f"State: <b>{state_name}</b>\n\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"Buyer: {buyer}\n\n"
        f"💵 Fiat: <b>{amount} {fiat_unit}</b>\n"
        f"🪙 Crypto: <b>{crypto_qty} {crypto}</b>\n"
        f"💱 Price: {price}\n"
        f"🕒 Created: {create_time}\n"
    )

# =========================
# COMMAND HANDLERS
# =========================

def cmd_help(chat_id):
    msg = (
        "<b>🤖 MEXC P2P Bot — Commands</b>\n\n"
        "/status — show bot status\n"
        "/check — test MEXC API (last 30 min)\n"
        "/check_telegram — test Telegram connection\n"
        "/recent — show your last 5 orders\n"
        "/test_alert — simulate an alert with your latest order\n"
        "/raw — show raw MEXC response (debug)\n"
        "/stop — pause polling\n"
        "/resume — resume polling\n"
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
        last_poll.strftime("%Y-%m-%d %H:%M:%S UTC") if last_poll else "never"
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
    send_telegram_message("🔍 Testing MEXC API...", chat_id)
    success, data, status_code = fetch_orders_raw(window_minutes=30)

    if not success:
        send_telegram_message(
            f"❌ Failed\nHTTP: {status_code}\n<code>{str(data)[:300]}</code>",
            chat_id
        )
        return

    if isinstance(data, dict) and data.get("code") in (0, 200):
        orders = extract_orders_list(data)
        send_telegram_message(
            f"<b>✅ MEXC connection OK</b>\n\n"
            f"HTTP: {status_code}\n"
            f"Orders in last 30 min: {len(orders)}",
            chat_id
        )
    else:
        send_telegram_message(
            f"<b>⚠️ MEXC error</b>\n<code>{str(data)[:400]}</code>",
            chat_id
        )

def cmd_recent(chat_id):
    """Show the most recent orders (last 30 days)."""
    try:
        orders = fetch_orders(window_minutes=30 * 24 * 60)
    except Exception as e:
        send_telegram_message(f"❌ Error: {e}", chat_id, parse_mode=None)
        return

    if not orders:
        send_telegram_message("No orders found in last 30 days.", chat_id)
        return

    lines = [f"<b>📋 Last {min(5, len(orders))} orders</b>\n"]
    for i, o in enumerate(orders[:5], 1):
        oid = get_order_id(o)
        st = get_order_state(o)
        amt = o.get("amount", "?")
        cur = o.get("fiatUnit", "")
        coin = o.get("coinName", "")
        buyer = get_buyer_name(o)
        lines.append(
            f"\n<b>{i}.</b> <code>{oid[-8:]}</code>\n"
            f"   State: <b>{st}</b>\n"
            f"   {amt} {cur} → {coin}\n"
            f"   Buyer: {buyer}"
        )

    send_telegram_message("\n".join(lines), chat_id)

def cmd_test_alert(chat_id):
    """Send a real alert formatted from your most recent order — to preview what alerts will look like."""
    try:
        orders = fetch_orders(window_minutes=30 * 24 * 60)
    except Exception as e:
        send_telegram_message(f"❌ Error: {e}", chat_id, parse_mode=None)
        return

    if not orders:
        send_telegram_message("No orders to test with.", chat_id)
        return

    send_telegram_message(
        "🧪 <b>Test alert preview</b>\nThis is what real alerts will look like:\n",
        chat_id
    )
    send_telegram_message(format_order_message(orders[0]), chat_id)

def cmd_raw(chat_id):
    send_telegram_message("🔍 Fetching raw...", chat_id)
    success, data, status_code = fetch_orders_raw(window_minutes=30 * 24 * 60)
    if not success:
        send_telegram_message(f"❌ {data}", chat_id, parse_mode=None)
        return
    try:
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        pretty = str(data)
    send_telegram_message(
        f"RAW (HTTP {status_code}):\n\n{pretty}",
        chat_id,
        parse_mode=None
    )

def cmd_check_telegram(chat_id):
    send_telegram_message("<b>✅ Telegram connection OK</b>", chat_id)

def cmd_stop(chat_id):
    with state_lock:
        if not state["polling_active"]:
            send_telegram_message("⚠️ Already paused.", chat_id)
            return
        state["polling_active"] = False
    send_telegram_message("🔴 <b>Polling paused.</b> Use /resume to restart.", chat_id)

def cmd_resume(chat_id):
    with state_lock:
        if state["polling_active"]:
            send_telegram_message("⚠️ Already active.", chat_id)
            return
        state["polling_active"] = True
    send_telegram_message("🟢 <b>Polling resumed.</b>", chat_id)

COMMANDS = {
    "/help": cmd_help,
    "/start": cmd_help,
    "/status": cmd_status,
    "/check": cmd_check_mexc,
    "/check_mexc": cmd_check_mexc,
    "/check_telegram": cmd_check_telegram,
    "/recent": cmd_recent,
    "/test_alert": cmd_test_alert,
    "/raw": cmd_raw,
    "/stop": cmd_stop,
    "/resume": cmd_resume,
}

# =========================
# TELEGRAM LISTENER
# =========================

def telegram_listener_loop():
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
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                command = text.split()[0].split("@")[0].lower()
                handler = COMMANDS.get(command)

                if handler:
                    logging.info(f"Command: {command}")
                    try:
                        handler(chat_id)
                    except Exception as e:
                        logging.exception(f"Handler error: {e}")
                        send_telegram_message(f"❌ Error: {e}", chat_id, parse_mode=None)
                else:
                    send_telegram_message(
                        "Unknown command. Send /help to see options.",
                        chat_id
                    )

        except Exception as e:
            logging.exception(f"Listener error: {e}")
            time.sleep(5)

# =========================
# POLLING LOOP
# =========================

def polling_loop():
    logging.info("Polling loop started.")

    # Prime existing orders (so we don't alert on already-existing ones at startup)
    try:
        existing = fetch_orders(window_minutes=30)
        with state_lock:
            for o in existing:
                oid = get_order_id(o)
                if oid:
                    state["seen_orders"].add(oid)
            primed = len(state["seen_orders"])
        logging.info(f"Primed {primed} existing orders.")
    except Exception as e:
        logging.exception(f"Initial fetch failed: {e}")

    while True:
        with state_lock:
            active = state["polling_active"]

        if not active:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        try:
            orders = fetch_orders(window_minutes=30)

            with state_lock:
                state["last_poll_time"] = datetime.utcnow()
                state["last_poll_status"] = f"OK ({len(orders)} orders)"

            for order in orders:
                order_state = get_order_state(order)

                # Only alert on active/new states
                if order_state not in ACTIVE_STATES:
                    continue

                oid = get_order_id(order)
                if not oid:
                    continue

                with state_lock:
                    if oid in state["seen_orders"]:
                        continue
                    state["seen_orders"].add(oid)

                message = format_order_message(order)
                logging.info(f"New order: {oid} ({order_state})")
                send_telegram_message(message)

                with state_lock:
                    state["total_alerts_sent"] += 1

            with state_lock:
                if len(state["seen_orders"]) > 500:
                    state["seen_orders"] = set(list(state["seen_orders"])[-250:])

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

    send_telegram_message(
        "✅ <b>MEXC P2P bot started — FINAL VERSION.</b>\n\n"
        "Field names verified against your real orders.\n"
        "Bot will now correctly detect new P2P orders.\n\n"
        "Try /test_alert to preview what alerts look like.\n"
        "Send /help for all commands."
    )

    listener_thread = threading.Thread(target=telegram_listener_loop, daemon=True)
    listener_thread.start()
    polling_loop()

if __name__ == "__main__":
    main()
