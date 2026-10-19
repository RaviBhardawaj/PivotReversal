"""
DhanHQ v2 live market data streamer (IPv4-preferred)
----------------------------------------------------
Install:
    py -m pip install --upgrade websocket-client

Run:
    py live_market_data.py

Place dhan_token.json in the same folder.
This script only reads market data; it does not place orders.
"""

import json
import socket
import struct
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

# Prefer IPv4 because the user's network test showed IPv6 connection failures.
_original_getaddrinfo = socket.getaddrinfo

def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(
        host, port, socket.AF_INET, type, proto, flags
    )

socket.getaddrinfo = _ipv4_getaddrinfo

import websocket  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "dhan_token.json"
IST = ZoneInfo("Asia/Kolkata")

# Edit these instrument entries after verifying IDs in Dhan's instrument master.
# Format: (display_name, exchange_segment, security_id)
INSTRUMENTS = [
    ("HDFC Bank", "NSE_EQ", "1333"),
    # ("Reliance Industries", "NSE_EQ", "2885"),
    # ("NIFTY 50", "IDX_I", "13"),
]

SUBSCRIBE_REQUEST_CODE = 15  # Ticker mode
MAX_PER_MESSAGE = 100


def load_credentials():
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Token file not found: {TOKEN_FILE}\n"
            "Place dhan_token.json beside this script."
        )

    with TOKEN_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    client_id = data.get("dhanClientId") or data.get("client_id")
    access_token = data.get("accessToken") or data.get("access_token")

    if not client_id or not access_token:
        raise ValueError(
            "dhan_token.json must contain dhanClientId and accessToken "
            "(or client_id and access_token)."
        )
    return str(client_id), str(access_token)


def build_subscribe_message(instruments):
    return {
        "RequestCode": SUBSCRIBE_REQUEST_CODE,
        "InstrumentCount": len(instruments),
        "InstrumentList": [
            {"ExchangeSegment": segment, "SecurityId": str(security_id)}
            for _, segment, security_id in instruments
        ],
    }


def instrument_name(segment_code, security_id):
    for name, _segment, sid in INSTRUMENTS:
        if str(sid) == str(security_id):
            return name
    return f"segment={segment_code}, security_id={security_id}"


def on_open(ws):
    print("Connected to DhanHQ live market feed (IPv4 preferred).")
    for start in range(0, len(INSTRUMENTS), MAX_PER_MESSAGE):
        batch = INSTRUMENTS[start:start + MAX_PER_MESSAGE]
        ws.send(json.dumps(build_subscribe_message(batch)))
        print(f"Subscription request sent for {len(batch)} instrument(s).")
    print("Waiting for live ticks... Press Ctrl+C to stop.\n")


def on_message(ws, message):
    if not isinstance(message, (bytes, bytearray)):
        print("Server message:", message)
        return

    if len(message) < 8:
        print(f"Ignoring short packet ({len(message)} bytes).")
        return

    try:
        response_code = message[0]
        segment_code = message[3]
        security_id = struct.unpack_from("<I", message, 4)[0]
        name = instrument_name(segment_code, security_id)

        # Ticker packet: 8-byte header + float32 LTP + uint32 last-trade epoch.
        if response_code == 2 and len(message) >= 16:
            ltp = struct.unpack_from("<f", message, 8)[0]
            ltt_epoch = struct.unpack_from("<I", message, 12)[0]
            trade_time = (
                datetime.fromtimestamp(ltt_epoch, IST).strftime("%H:%M:%S")
                if ltt_epoch else "-"
            )
            received_at = datetime.now(IST).strftime("%H:%M:%S")
            print(
                f"{received_at} | {name:<24} | "
                f"LTP ₹{ltp:,.2f} | Last trade: {trade_time}"
            )
        elif response_code == 6 and len(message) >= 12:
            previous_close = struct.unpack_from("<f", message, 8)[0]
            print(f"{name:<24} | Previous close ₹{previous_close:,.2f}")
        else:
            print(
                f"Received packet code={response_code}, "
                f"segment={segment_code}, security_id={security_id}, "
                f"bytes={len(message)}"
            )
    except (struct.error, ValueError, OSError) as exc:
        print(f"Could not decode market packet: {exc}")


def on_error(ws, error):
    print("WebSocket error:", error)


def on_close(ws, status_code, message):
    print(f"WebSocket closed. Code={status_code}, message={message}")


def make_app(url):
    return websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )


def main():
    if not INSTRUMENTS:
        raise ValueError("Add at least one instrument to INSTRUMENTS.")

    client_id, access_token = load_credentials()
    params = urlencode({
        "version": "2",
        "token": access_token,
        "clientId": client_id,
        "authType": "2",
    })
    url = f"wss://api-feed.dhan.co?{params}"

    print("DhanHQ Live Market Data")
    print(f"Configured instruments: {len(INSTRUMENTS)}")
    print("Token loaded locally (not displayed).")
    print("IPv4 preference enabled.")

    while True:
        app = make_app(url)
        try:
            app.run_forever(ping_interval=20, ping_timeout=10)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as exc:
            print("Feed stopped unexpectedly:", exc)

        print("Connection closed. Reconnecting in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as exc:
        print(f"Startup error: {exc}")
