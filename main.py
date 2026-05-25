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

def send_telegram_message(text: str, chat_id: str = None, parse_mode: str = "HTML"):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        # Telegram limit is 4096 chars; trim if needed
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
            # Retry without parse_mode in case HTML breaks
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
# FETCH ORDERS (raw) — configurable window
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

def extract_orders_list(data):
    """Try every reasonable shape to find the list of orders inside the response."""
    if not isinstance(data, dict):
        return []

    inner = data.get("data")

    if isinstance(inner, list):
        return inner

    if isinstance(inner, dict):
        # Common keys for paginated lists
        for key in ("list", "orders", "records", "items", "rows", "content"):
            v = inner.get(key)
            if isinstance(v, list):
                return v

    return []

def fetch_orders(window_minutes: int = 30):
    """Returns list of orders or raises Exception."""
    success, data, status = fetch_orders_raw(window_minutes)

    if not success:
        raise Exception(f"HTTP {status}: {data}")

    if not isinstance(data, dict):
        raise Exception(f"Unexpected response: {data}")

    if data.get("code") != 0 and data.get("code") != 200:
        raise Exception(f"MEXC API error: {data}")

    return extract_orders_list(data)

# =========================
# FORMAT ORDER MESSAGE
# =========================

def safe_get(order, *keys, default="N/A"):
    """Try multiple possible keys (some APIs use camelCase, snake_case, etc)."""
    for k in keys:
        if k in order and order[k] not in (None, ""):
            return order[k]
    return default

def format_order_message(order: dict) -> str:
    s = safe_get(order, "state", "status", "orderState", default=None)
    state_name = STATE_NAMES.get(s, str(s)) if s is not None else "UNKNOWN"

    order_id = safe_get(order, "advOrderNo", "orderNo", "orderId", "id")
    fiat_amount = safe_get(order, "fiatAmount", "amount", "totalAmount")
    fiat_currency = safe_get(order, "fiatCurrency", "currency", "fiat")
    crypto_amount = safe_get(order, "cryptoAmount", "quantity", "tradeAmount")
    crypto_currency = safe_get(order, "currency", "coin", "asset", "cryptoCurrency")
    price = safe_get(order, "price", "unitPrice")
    buyer = safe_get(order, "nickname", "buyerNickname", "counterpartyName", "userName")

    return (
        "🚨 <b>New MEXC P2P Order</b>\n\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"Buyer: {buyer}\n"
        f"State: <b>{state_name}</b>\n\n"
        f"Fiat: {fiat_amount} {fiat_currency}\n"
        f"Crypto: {crypto_amount} {crypto_currency}\n"
        f"Price: {price}\n"
    )

def get_order_id(order):
    """Get a unique identifier for an order across possible field name variations."""
    return safe_get(
        order, "advOrderNo", "orderNo", "orderId", "id", "tradeNo",
        default=None
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
        "/test_history — fetch last 30 days of orders\n"
        "/raw — show RAW MEXC response (for debugging)\n"
        "/fields — show field names in your orders\n"
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
    send_telegram_message("🔍 Testing MEXC API (last 30 min)...", chat_id)

    success, data, status_code = fetch_orders_raw(window_minutes=30)

    if not success:
        msg = (
            "<b>❌ MEXC connection failed</b>\n\n"
            f"HTTP status: {status_code}\n"
            f"Error: <code>{str(data)[:300]}</code>"
        )
        send_telegram_message(msg, chat_id)
        return

    if isinstance(data, dict):
        code = data.get("code")
        orders = extract_orders_list(data)

        if code == 0 or code == 200:
            msg = (
                "<b>✅ MEXC connection OK</b>\n\n"
                f"HTTP: {status_code}\n"
                f"API code: {code}\n"
                f"Orders in last 30 min: {len(orders)}\n\n"
                "Your API key and secret are working."
            )
        else:
            msg = (
                "<b>⚠️ MEXC error</b>\n\n"
                f"HTTP: {status_code}\n"
                f"Code: {code}\n"
                f"Raw: <code>{str(data)[:400]}</code>"
            )
    else:
        msg = f"<b>⚠️ Unexpected response</b>\n<code>{str(data)[:400]}</code>"

    send_telegram_message(msg, chat_id)

def cmd_test_history(chat_id):
    """Fetch last 30 days of orders to verify the endpoint returns real data."""
    send_telegram_message(
        "🔍 Fetching last 30 days of orders from MEXC...\n"
        "(your dashboard shows 3 completed orders in this window)",
        chat_id
    )

    success, data, status_code = fetch_orders_raw(window_minutes=30 * 24 * 60)

    if not success:
        send_telegram_message(
            f"❌ Request failed\nHTTP: {status_code}\n<code>{str(data)[:400]}</code>",
            chat_id
        )
        return

    if not isinstance(data, dict):
        send_telegram_message(f"⚠️ Unexpected response: <code>{str(data)[:400]}</code>", chat_id)
        return

    code = data.get("code")
    orders = extract_orders_list(data)

    summary = (
        f"<b>📈 30-day history result</b>\n\n"
        f"HTTP: {status_code}\n"
        f"API code: {code}\n"
        f"Orders returned: <b>{len(orders)}</b>\n\n"
    )

    if len(orders) == 0:
        summary += (
            "❌ 0 orders returned.\n\n"
            "Your dashboard shows 3 completed orders in 30 days, "
            "but this endpoint returned none. This means either:\n"
            "• The endpoint is wrong\n"
            "• It needs different parameters\n"
            "• It requires merchant-API-key permissions\n\n"
            "Use /raw to see the exact response."
        )
        send_telegram_message(summary, chat_id)
        return

    # Show a summary of each order
    summary += "✅ Endpoint returns real data!\n\nOrder breakdown:\n"

    for i, o in enumerate(orders[:5], 1):
        s = safe_get(o, "state", "status", "orderState", default=None)
        state_name = STATE_NAMES.get(s, str(s)) if s is not None else "?"
        oid = get_order_id(o)
        fiat = safe_get(o, "fiatAmount", "amount")
        cur = safe_get(o, "fiatCurrency", "currency", "fiat")
        summary += f"\n{i}. <code>{oid}</code> — {state_name} — {fiat} {cur}"

    if len(orders) > 5:
        summary += f"\n\n...and {len(orders) - 5} more"

    send_telegram_message(summary, chat_id)

def cmd_raw(chat_id):
    """Show the raw JSON response from MEXC for debugging."""
    send_telegram_message("🔍 Fetching raw response...", chat_id)

    success, data, status_code = fetch_orders_raw(window_minutes=30 * 24 * 60)

    if not success:
        send_telegram_message(
            f"❌ Request failed\nHTTP: {status_code}\n<code>{str(data)[:1000]}</code>",
            chat_id
        )
        return

    try:
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        pretty = str(data)

    # Send without parse_mode to avoid HTML issues with raw JSON
    msg = f"RAW response (HTTP {status_code}):\n\n{pretty}"
    send_telegram_message(msg, chat_id, parse_mode=None)

def cmd_fields(chat_id):
    """Show the field names present in your actual orders."""
    success, data, status_code = fetch_orders_raw(window_minutes=30 * 24 * 60)

    if not success:
        send_telegram_message(f"❌ Request failed: {data}", chat_id)
        return

    orders = extract_orders_list(data)

    if not orders:
        send_telegram_message(
            "No orders returned — can't analyze fields. Try /raw to see the full response.",
            chat_id
        )
        return

    first = orders[0]
    if not isinstance(first, dict):
        send_telegram_message(f"Unexpected order shape: <code>{str(first)[:300]}</code>", chat_id)
        return

    field_lines = []
    for k, v in first.items():
        v_str = str(v)
        if len(v_str) > 60:
            v_str = v_str[:60] + "..."
        field_lines.append(f"• <b>{k}</b>: <code>{v_str}</code>")

    msg = (
        "<b>📋 Fields in your first order</b>\n\n"
        + "\n".join(field_lines)
        + f"\n\n<i>Total orders found: {len(orders)}</i>"
    )
    send_telegram_message(msg, chat_id)

def cmd_check_telegram(chat_id):
    send_telegram_message(
        "<b>✅ Telegram connection OK</b>",
        chat_id
    )

def cmd_stop(chat_id):
    with state_lock:
        if not state["polling_active"]:
            send_telegram_message("⚠️ Polling is already paused.", chat_id)
            return
        state["polling_active"] = False
    send_telegram_message("🔴 <b>Polling paused.</b> Use /resume to restart.", chat_id)

def cmd_resume(chat_id):
    with state_lock:
        if state["polling_active"]:
            send_telegram_message("⚠️ Polling is already active.", chat_id)
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
    "/test_history": cmd_test_history,
    "/raw": cmd_raw,
    "/fields": cmd_fields,
    "/stop": cmd_stop,
    "/resume": cmd_resume,
    "/start_polling": cmd_resume,
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
                    logging.warning(f"Ignored message from chat {chat_id}")
                    continue

                command = text.split()[0].split("@")[0].lower()
                handler = COMMANDS.get(command)

                if handler:
                    logging.info(f"Command received: {command}")
                    try:
                        handler(chat_id)
                    except Exception as e:
                        logging.exception(f"Command handler error: {e}")
                        send_telegram_message(f"❌ Error: {e}", chat_id, parse_mode=None)
                else:
                    send_telegram_message(
                        f"Unknown command. Send /help to see options.",
                        chat_id
                    )

        except Exception as e:
            logging.exception(f"Telegram listener error: {e}")
            time.sleep(5)

# =========================
# POLLING LOOP
# =========================

def polling_loop():
    logging.info("Polling loop started.")

    try:
        existing_orders = fetch_orders(window_minutes=30)
        with state_lock:
            for order in existing_orders:
                oid = get_order_id(order)
                if oid:
                    state["seen_orders"].add(oid)
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
            orders = fetch_orders(window_minutes=30)

            with state_lock:
                state["last_poll_time"] = datetime.utcnow()
                state["last_poll_status"] = f"OK ({len(orders)} orders)"

            for order in orders:
                s = safe_get(order, "state", "status", "orderState", default=None)

                if s not in ACTIVE_STATES:
                    continue

                oid = get_order_id(order)
                if not oid:
                    continue

                with state_lock:
                    if oid in state["seen_orders"]:
                        continue
                    state["seen_orders"].add(oid)

                message = format_order_message(order)
                logging.info(f"New order detected: {oid}")
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
        "✅ <b>MEXC P2P bot started (diagnostic build).</b>\n\n"
        "New commands available:\n"
        "/test_history — check 30-day history\n"
        "/raw — see raw MEXC response\n"
        "/fields — see field names\n\n"
        "Send /help for full list."
    )

    listener_thread = threading.Thread(target=telegram_listener_loop, daemon=True)
    listener_thread.start()

    polling_loop()

if __name__ == "__main__":
    main()
