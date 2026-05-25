import os
import time
import hmac
import hashlib
import logging
from urllib.parse import urlencode

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
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# TELEGRAM
# =========================

def send_telegram_message(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
        }

        response = requests.post(url, json=payload, timeout=15)

        if response.status_code != 200:
            logging.error(f"Telegram error: {response.text}")

    except Exception as e:
        logging.exception(f"Telegram send failed: {e}")

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

def fetch_orders():
    now_ms = int(time.time() * 1000)

    # last 30 minutes
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

    headers = {
        "X-MEXC-APIKEY": MEXC_API_KEY
    }

    response = requests.get(url, headers=headers, timeout=20)

    response.raise_for_status()

    data = response.json()

    if data.get("code") != 0:
        raise Exception(f"MEXC API error: {data}")

    orders = data.get("data", [])

    return orders

# =========================
# FORMAT MESSAGE
# =========================

def format_order_message(order: dict) -> str:
    state = order.get("state")
    state_name = STATE_NAMES.get(state, str(state))

    adv_order_no = order.get("advOrderNo", "N/A")
    fiat_amount = order.get("fiatAmount", "N/A")
    fiat_currency = order.get("fiatCurrency", "N/A")
    crypto_amount = order.get("cryptoAmount", "N/A")
    crypto_currency = order.get("currency", "N/A")
    price = order.get("price", "N/A")
    buyer_nickname = order.get("nickname", "N/A")

    message = (
        "🚨 New MEXC P2P Order\n\n"
        f"Order ID: {adv_order_no}\n"
        f"Buyer: {buyer_nickname}\n"
        f"State: {state_name}\n\n"
        f"Fiat Amount: {fiat_amount} {fiat_currency}\n"
        f"Crypto Amount: {crypto_amount} {crypto_currency}\n"
        f"Price: {price}\n"
    )

    return message

# =========================
# MAIN LOOP
# =========================

def main():
    if not all([
        MEXC_API_KEY,
        MEXC_SECRET,
        TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID
    ]):
        raise Exception(
            "Missing required environment variables."
        )

    seen_orders = set()

    # =========================
    # PRIME EXISTING ORDERS
    # =========================

    try:
        existing_orders = fetch_orders()

        for order in existing_orders:
            adv_order_no = order.get("advOrderNo")

            if adv_order_no:
                seen_orders.add(adv_order_no)

        logging.info(
            f"Primed {len(seen_orders)} existing orders."
        )

    except Exception as e:
        logging.exception(f"Initial fetch failed: {e}")

    # startup ping
    send_telegram_message("✅ MEXC P2P notification bot started.")

    # =========================
    # POLLING LOOP
    # =========================

    while True:
        try:
            orders = fetch_orders()

            for order in orders:
                state = order.get("state")

                # only active states
                if state not in ACTIVE_STATES:
                    continue

                adv_order_no = order.get("advOrderNo")

                if not adv_order_no:
                    continue

                # skip if already seen
                if adv_order_no in seen_orders:
                    continue

                # mark as seen
                seen_orders.add(adv_order_no)

                # send alert
                message = format_order_message(order)

                logging.info(
                    f"New order detected: {adv_order_no}"
                )

                send_telegram_message(message)

            # trim seen set
            if len(seen_orders) > 500:
                seen_orders = set(list(seen_orders)[-250:])
                logging.info("Trimmed seen_orders set.")

        except Exception as e:
            logging.exception(f"Polling error: {e}")

        time.sleep(POLL_INTERVAL_SEC)

# =========================
# ENTRY
# =========================

if __name__ == "__main__":
    main()
