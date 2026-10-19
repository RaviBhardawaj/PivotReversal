"""
order.py — Dhan API v2 order placement with stop-loss support.
Includes sample orders for testing. Auto-refreshes token on 401.
"""

import json
import os
import sys
from pathlib import Path

import pyotp
import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
TOKEN_FILE = BASE_DIR / "dhan_token.json"
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
API_BASE = "https://api.dhan.co/v2"

REQUIRED_KEYS = ["client_id", "pin", "app_id", "app_secret", "totp_secret"]
PROMPTS = {
    "client_id":   "Dhan Client ID",
    "pin":         "Dhan PIN",
    "app_id":      "Dhan App ID",
    "app_secret":  "Dhan App Secret",
    "totp_secret": "Google Authenticator TOTP secret (base32, no spaces)",
}


# ----------------------------------------------------------------------
# Config & token
# ----------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    changed = False
    for k in REQUIRED_KEYS:
        if k not in cfg:
            cfg[k] = ""
            changed = True
    if changed or not CONFIG_FILE.exists():
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def prompt_missing(cfg: dict) -> dict:
    changed = False
    for k in REQUIRED_KEYS:
        if not cfg.get(k):
            v = input(f"Enter {PROMPTS[k]}: ").strip()
            if v:
                cfg[k] = v
                changed = True
    if changed:
        save_config(cfg)
        print(f"[+] Credentials saved to {CONFIG_FILE}")
    return cfg


def read_cached_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        with TOKEN_FILE.open("r", encoding="utf-8") as f:
            return json.load(f).get("accessToken")
    except Exception:
        return None


def save_token(data: dict) -> None:
    with TOKEN_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def generate_token(cfg: dict) -> str:
    totp_secret = cfg["totp_secret"].strip().upper().replace(" ", "")
    params = {
        "dhanClientId": cfg["client_id"],
        "pin": cfg["pin"],
        "totp": pyotp.TOTP(totp_secret).now(),
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    print("[*] Requesting fresh access token...")
    r = requests.post(AUTH_URL, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        raise RuntimeError(f"Token request failed ({r.status_code}): {body}")
    data = r.json()
    if "accessToken" not in data:
        raise RuntimeError(f"Unexpected token response: {data}")
    save_token(data)
    print("[+] New access token saved.")
    return data["accessToken"]


def get_token(cfg: dict) -> str:
    cached = read_cached_token()
    return cached if cached else generate_token(cfg)


# ----------------------------------------------------------------------
# API wrappers with auto-refresh
# ----------------------------------------------------------------------
def api_post(cfg: dict, holder: dict, path: str, payload: dict) -> dict:
    url = f"{API_BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": holder["token"],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code == 401:
        print("[!] Token expired. Refreshing...")
        holder["token"] = generate_token(cfg)
        headers["access-token"] = holder["token"]
        r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def api_put(cfg: dict, holder: dict, path: str, payload: dict) -> dict:
    url = f"{API_BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": holder["token"],
    }
    r = requests.put(url, headers=headers, json=payload, timeout=30)
    if r.status_code == 401:
        holder["token"] = generate_token(cfg)
        headers["access-token"] = holder["token"]
        r = requests.put(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def api_delete(cfg: dict, holder: dict, path: str) -> dict:
    url = f"{API_BASE}{path}"
    headers = {"Accept": "application/json", "access-token": holder["token"]}
    r = requests.delete(url, headers=headers, timeout=30)
    if r.status_code == 401:
        holder["token"] = generate_token(cfg)
        headers["access-token"] = holder["token"]
        r = requests.delete(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------
# Dynamic payload builder (omits unused optional fields)
# ----------------------------------------------------------------------
def build_order_payload(
    cfg: dict,
    security_id: str,
    segment: str,
    txn_type: str,
    qty: int,
    product_type: str,
    order_type: str,
    price: float | None = None,
    trigger_price: float | None = None,
    disclosed_qty: int | None = None,
    correlation_id: str | None = None,
    after_market: bool = False,
    amo_time: str | None = None,
    bo_profit: float | None = None,
    bo_sl: float | None = None,
) -> dict:
    """Build a minimal, valid order payload. Only include fields that are actually used."""
    payload = {
        "dhanClientId": cfg["client_id"],
        "transactionType": txn_type,
        "exchangeSegment": segment,
        "productType": product_type,
        "orderType": order_type,
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": qty,
    }

    if order_type == "LIMIT":
        if price is None:
            raise ValueError("price is required for LIMIT orders")
        payload["price"] = float(price)

    if trigger_price is not None:
        payload["triggerPrice"] = float(trigger_price)

    if disclosed_qty is not None and disclosed_qty > 0:
        payload["disclosedQuantity"] = int(disclosed_qty)

    if correlation_id:
        payload["correlationId"] = correlation_id

    if after_market:
        payload["afterMarketOrder"] = True
        if amo_time:
            payload["amoTime"] = amo_time

    if bo_profit is not None:
        payload["boProfitValue"] = float(bo_profit)
    if bo_sl is not None:
        payload["boStopLossValue"] = float(bo_sl)

    return payload


# ----------------------------------------------------------------------
# Order placement
# ----------------------------------------------------------------------
def place_order(cfg: dict, holder: dict, payload: dict) -> dict:
    print("[*] Placing order...")
    result = api_post(cfg, holder, "/orders", payload)
    print(f"[+] Order response: {result}")
    return result


def place_super_order(
    cfg: dict,
    holder: dict,
    security_id: str,
    segment: str,
    txn_type: str,
    qty: int,
    entry_price: float,
    target_price: float,
    stop_loss_price: float,
    trailing_jump: float = 0,
    product_type: str = "INTRADAY",
) -> dict:
    payload = {
        "dhanClientId": cfg["client_id"],
        "transactionType": txn_type,
        "exchangeSegment": segment,
        "productType": product_type,
        "orderType": "LIMIT",
        "securityId": str(security_id),
        "quantity": qty,
        "price": entry_price,
        "targetPrice": target_price,
        "stopLossPrice": stop_loss_price,
        "trailingJump": trailing_jump,
    }
    print("[*] Placing Super Order...")
    result = api_post(cfg, holder, "/super/orders", payload)
    print(f"[+] Super Order response: {result}")
    return result


# ----------------------------------------------------------------------
# Modify & cancel
# ----------------------------------------------------------------------
def modify_order(cfg: dict, holder: dict, order_id: str, updates: dict) -> dict:
    print(f"[*] Modifying order {order_id}...")
    result = api_put(cfg, holder, f"/orders/{order_id}", updates)
    print(f"[+] Modify response: {result}")
    return result


def cancel_order(cfg: dict, holder: dict, order_id: str) -> dict:
    print(f"[*] Cancelling order {order_id}...")
    result = api_delete(cfg, holder, f"/orders/{order_id}")
    print(f"[+] Cancel response: {result}")
    return result


# ----------------------------------------------------------------------
# SAMPLE ORDERS — run these first to validate your setup
# ----------------------------------------------------------------------
def sample_market_order(cfg: dict, holder: dict) -> None:
    """Sample: BUY 1 share MARKET, CNC. Replace securityId with your stock."""
    print("\n--- SAMPLE: MARKET order (BUY 1 share, CNC) ---")
    payload = build_order_payload(
        cfg=cfg,
        security_id="1660",        # ← change to your stock's securityId
        segment="NSE_EQ",
        txn_type="BUY",
        qty=1,
        product_type="CNC",
        order_type="MARKET",
    )
    place_order(cfg, holder, payload)


def sample_limit_order(cfg: dict, holder: dict) -> None:
    """Sample: BUY 1 share LIMIT at ₹255, CNC."""
    print("\n--- SAMPLE: LIMIT order (BUY 1 share @ ₹255, CNC) ---")
    payload = build_order_payload(
        cfg=cfg,
        security_id="1660",
        segment="NSE_EQ",
        txn_type="BUY",
        qty=1,
        product_type="CNC",
        order_type="LIMIT",
        price=255.0,
    )
    place_order(cfg, holder, payload)


def sample_sl_market_order(cfg: dict, holder: dict) -> None:
    """Sample: SELL 1 share STOP_LOSS_MARKET (SL-M) at trigger ₹240."""
    print("\n--- SAMPLE: SL-M order (SELL 1 share, trigger ₹240) ---")
    payload = build_order_payload(
        cfg=cfg,
        security_id="1660",
        segment="NSE_EQ",
        txn_type="SELL",
        qty=1,
        product_type="CNC",
        order_type="STOP_LOSS_MARKET",
        trigger_price=240.0,
    )
    place_order(cfg, holder, payload)


def sample_sl_limit_order(cfg: dict, holder: dict) -> None:
    """Sample: SELL 1 share STOP_LOSS (SL-L) at trigger ₹240, limit ₹239."""
    print("\n--- SAMPLE: SL-L order (SELL 1 share, trigger ₹240, limit ₹239) ---")
    payload = build_order_payload(
        cfg=cfg,
        security_id="1660",
        segment="NSE_EQ",
        txn_type="SELL",
        qty=1,
        product_type="CNC",
        order_type="STOP_LOSS",
        trigger_price=240.0,
        price=239.0,
    )
    place_order(cfg, holder, payload)


def sample_super_order(cfg: dict, holder: dict) -> None:
    """Sample: Super Order — BUY @ ₹255, target ₹265, SL ₹248."""
    print("\n--- SAMPLE: Super Order (BUY @ ₹255, target ₹265, SL ₹248) ---")
    place_super_order(
        cfg=cfg,
        holder=holder,
        security_id="1660",
        segment="NSE_EQ",
        txn_type="BUY",
        qty=1,
        entry_price=255.0,
        target_price=265.0,
        stop_loss_price=248.0,
        trailing_jump=0,
        product_type="CNC",
    )


def run_sample_orders(cfg: dict, holder: dict) -> None:
    """Run all sample orders in sequence (for testing)."""
    print("\n=== RUNNING SAMPLE ORDERS ===")
    print("These use securityId='1660'. Change it to a valid stock ID first.")
    confirm = input("Proceed? (y/N): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    sample_market_order(cfg, holder)
    sample_limit_order(cfg, holder)
    sample_sl_market_order(cfg, holder)
    sample_sl_limit_order(cfg, holder)
    sample_super_order(cfg, holder)


# ----------------------------------------------------------------------
# Interactive menu
# ----------------------------------------------------------------------
def prompt_order_details() -> dict:
    print("\n--- Order details ---")
    sec_id = input("Security ID: ").strip()
    seg = input("Exchange segment (NSE_EQ / NSE_FNO / IDX_I): ").strip().upper() or "NSE_EQ"
    txn = input("Transaction type (BUY / SELL): ").strip().upper() or "BUY"
    qty = int(input("Quantity: ").strip())
    prod = input("Product type (INTRADAY / CNC / MARGIN): ").strip().upper() or "INTRADAY"
    return {"security_id": sec_id, "segment": seg, "txn_type": txn,
            "qty": qty, "product_type": prod}


def main() -> None:
    print("=== Dhan Order Placement ===")
    cfg = load_config()
    cfg = prompt_missing(cfg)
    if not all(cfg.get(k) for k in REQUIRED_KEYS):
        sys.exit("[-] Missing required credentials in config.json.")

    holder = {"token": get_token(cfg)}

    print("\nChoose action:")
    print("  0) RUN SAMPLE ORDERS (test your setup)")
    print("  1) Place regular MARKET / LIMIT order")
    print("  2) Place STOP_LOSS (SL-L) order")
    print("  3) Place STOP_LOSS_MARKET (SL-M) order")
    print("  4) Place Super Order (entry + target + SL)")
    print("  5) Modify a pending order")
    print("  6) Cancel a pending order")
    choice = input("Enter 0–6: ").strip()

    if choice == "0":
        run_sample_orders(cfg, holder)

    elif choice == "1":
        d = prompt_order_details()
        otype = input("Order type (MARKET / LIMIT): ").strip().upper() or "MARKET"
        price = None
        if otype == "LIMIT":
            price = float(input("Limit price: ").strip())
        payload = build_order_payload(
            cfg=cfg, security_id=d["security_id"], segment=d["segment"],
            txn_type=d["txn_type"], qty=d["qty"], product_type=d["product_type"],
            order_type=otype, price=price,
        )
        place_order(cfg, holder, payload)

    elif choice == "2":
        d = prompt_order_details()
        trigger = float(input("Trigger price: ").strip())
        limit_price = float(input("Limit price: ").strip())
        payload = build_order_payload(
            cfg=cfg, security_id=d["security_id"], segment=d["segment"],
            txn_type=d["txn_type"], qty=d["qty"], product_type=d["product_type"],
            order_type="STOP_LOSS", trigger_price=trigger, price=limit_price,
        )
        place_order(cfg, holder, payload)

    elif choice == "3":
        d = prompt_order_details()
        trigger = float(input("Trigger price: ").strip())
        payload = build_order_payload(
            cfg=cfg, security_id=d["security_id"], segment=d["segment"],
            txn_type=d["txn_type"], qty=d["qty"], product_type=d["product_type"],
            order_type="STOP_LOSS_MARKET", trigger_price=trigger,
        )
        place_order(cfg, holder, payload)

    elif choice == "4":
        d = prompt_order_details()
        entry = float(input("Entry price: ").strip())
        target = float(input("Target price: ").strip())
        sl = float(input("Stop-loss price: ").strip())
        trail = float(input("Trailing jump (0 to disable): ").strip() or 0)
        place_super_order(
            cfg=cfg, holder=holder, security_id=d["security_id"],
            segment=d["segment"], txn_type=d["txn_type"], qty=d["qty"],
            entry_price=entry, target_price=target, stop_loss_price=sl,
            trailing_jump=trail, product_type=d["product_type"],
        )

    elif choice == "5":
        oid = input("Order ID to modify: ").strip()
        print("Leave blank to keep unchanged.")
        updates = {}
        p = input("New price: ").strip()
        if p:
            updates["price"] = float(p)
        q = input("New quantity: ").strip()
        if q:
            updates["quantity"] = int(q)
        ot = input("New order type: ").strip().upper()
        if ot:
            updates["orderType"] = ot
        v = input("New validity: ").strip().upper()
        if v:
            updates["validity"] = v
        if updates:
            modify_order(cfg, holder, oid, updates)
        else:
            print("Nothing to modify.")

    elif choice == "6":
        oid = input("Order ID to cancel: ").strip()
        cancel_order(cfg, holder, oid)

    else:
        sys.exit("Invalid choice.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"\n[-] HTTP {e.response.status_code}: {e.response.text}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
    except Exception as e:
        print(f"\n[-] Error: {e}")
        sys.exit(1)