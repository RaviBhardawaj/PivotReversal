"""Pre-live check of order placement with a real broker, using the same adapters the app trades with.

Without options it only reads: login, funds, contract lookup, order book and positions.
With --place BROKER (ANGEL, DHAN or XTS) it also sends ONE test order that cannot fill:
a 1-lot NIFTY ATM CE BUY LIMIT priced 20% below LTP. It then lowers the price (modify), cancels
the order, and confirms that no position is left. If the order fills anyway, it is sold at market
at once.

Run during market hours (09:15-15:25 IST, Mon-Fri):
    double-click live_order_check.command, or
    python3 live_order_check.py [--place ANGEL|DHAN|XTS]
"""

import os
import sys
import time
import pickle
from datetime import datetime, time as dtime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv                       # noqa: E402
load_dotenv(os.path.join(BASE_DIR, ".env"))

import requests                                      # noqa: E402
from brokers import ADAPTERS, BROKER_LABELS          # noqa: E402
from brokers.base import FILLED, PENDING, REJECTED, CANCELLED, tick_round   # noqa: E402

SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_SPOT = ("NSE", "Nifty 50", "99926000")
TEST_DISCOUNT = 0.80          # test BUY limit = 80% of LTP, so it rests unfilled
problems = []


def env(k):
    return (os.getenv(k) or "").strip()


def say(ok, text):
    print(f"  {'✅' if ok else '❌'} {text}")
    if not ok:
        problems.append(text)


def market_open(now=None):
    now = now or datetime.now()
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 25)


# ── Angel session: market data for every broker, and the Angel adapter's session ──
def angel_session():
    import pyotp
    from SmartApi import SmartConnect
    obj = SmartConnect(api_key=env("ANGEL_API_KEY"))
    s = obj.generateSession(env("ANGEL_CLIENT_ID"), env("ANGEL_MPIN"), pyotp.TOTP(env("ANGEL_TOTP_SECRET")).now())
    if not s or not s.get("status"):
        raise RuntimeError(f"Angel One login refused: {(s or {}).get('message', 'no response')}")
    return obj


def ltp(obj, exch, tsym, token):
    r = obj.ltpData(exch, tsym, str(token))
    if not r or not r.get("status") or not r.get("data"):
        raise RuntimeError(f"no LTP for {tsym}: {(r or {}).get('message', r)}")
    return float(r["data"]["ltp"])


def scrip_rows():
    """Angel instrument list: today's app cache if present, otherwise a fresh download."""
    path = os.path.join(BASE_DIR, "scrip_cache.pkl")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                df = pickle.load(f)
            return df.to_dict("records") if hasattr(df, "to_dict") else list(df)
        except Exception:
            pass
    return requests.get(SCRIP_URL, timeout=90).json()


def nifty_atm_ce(obj):
    spot = ltp(obj, *NIFTY_SPOT)
    atm = round(spot / 50) * 50
    today = datetime.now().date()
    best = None
    for r in scrip_rows():
        if r.get("exch_seg") != "NFO" or r.get("name") != "NIFTY" or r.get("instrumenttype") != "OPTIDX":
            continue
        if not str(r.get("symbol", "")).endswith("CE"):
            continue
        try:
            if abs(float(r["strike"]) / 100 - atm) > 0.01:
                continue
            exp = datetime.strptime(str(r["expiry"]).upper(), "%d%b%Y").date()
        except Exception:
            continue
        if exp >= today and (best is None or exp < best[0]):
            best = (exp, r)
    if not best:
        raise RuntimeError(f"NIFTY {atm} CE not found in the instrument list")
    r = best[1]
    inst = {"symbol": "NIFTY", "exchange": "NFO", "token": str(r["token"]), "tsym": str(r["symbol"]),
            "expiry": str(r["expiry"]).upper(), "strike": float(atm), "opt_type": "CE",
            "lot_size": int(float(r["lotsize"]))}
    return spot, inst


def make_adapter(name, obj):
    cls = ADAPTERS[name]
    creds = {k: env(k) for k in cls.env_keys}
    if name == "ANGEL":
        creds["_session_getter"] = lambda: obj
    kw = {"cache_dir": BASE_DIR} if name == "DHAN" else {}
    return cls(creds, log=lambda m: print("     " + m), **kw)


def has_keys(name):
    need = {"ANGEL": ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_MPIN", "ANGEL_TOTP_SECRET"],
            "DHAN": ["DHAN_CLIENT_ID"], "XTS": ["XTS_API_KEY", "XTS_API_SECRET"]}[name]
    return all(env(k) for k in need)


def wait_status(ad, oid, want, secs=10):
    st = None
    for _ in range(int(secs * 2)):
        st = ad.orders().get(str(oid))
        if st and st.status in want:
            return st
        time.sleep(0.5)
    return st


# ── read-only checks ──
def read_checks(name, obj, inst):
    label = BROKER_LABELS[name]
    print(f"\n{label}")
    ad = make_adapter(name, obj)
    try:
        say(True, f"login OK ({ad.login()})")
    except Exception as e:
        say(False, f"{label} login failed: {e}")
        return None, None
    try:
        f = ad.funds()
        print(f"  ℹ️  available funds: {'₹{:,.0f}'.format(f) if f is not None else 'not reported'}")
    except Exception as e:
        print(f"  ⚠️  funds not readable: {e}")
    try:
        r = ad.resolve(inst)
        say(True, f"contract {inst['tsym']} → {label} ID {r['inst_id']}, lot {inst['lot_size']}")
    except Exception as e:
        say(False, f"{label} contract lookup failed: {e}")
        return ad, None
    try:
        say(True, f"order book readable ({len(ad.orders())} orders today)")
    except Exception as e:
        say(False, f"{label} order book failed: {e}")
    try:
        pos = ad.positions()
        say(True, f"positions readable ({len(pos)} open)")
    except Exception as e:
        say(False, f"{label} positions failed: {e}")
    return ad, r


# ── one real test order ──
def order_check(name, ad, r, obj):
    label = BROKER_LABELS[name]
    qty = r["lot_size"]
    opt_ltp = ltp(obj, "NFO", r["tsym"], r["token"])
    px = max(0.05, tick_round(opt_ltp * TEST_DISCOUNT))
    px2 = max(0.05, tick_round(px - 0.5))
    tag = "PVTEST" + datetime.now().strftime("%H%M%S")
    print(f"\n{label}: test order BUY {qty} {r['tsym']} LIMIT ₹{px} (LTP ₹{opt_ltp})")
    try:
        oid = ad.place(r, "BUY", qty, "LIMIT", px, product="INTRADAY", tag=tag)
    except Exception as e:
        say(False, f"{label} place failed: {e}")
        return
    say(True, f"placed, order ID {oid}")
    try:
        st = wait_status(ad, oid, (PENDING, FILLED, REJECTED, CANCELLED), 6)
        if st is None:
            say(False, f"{label} order {oid} not found in the order book")
        elif st.status == REJECTED:
            say(False, f"{label} rejected the test order: {st.message}")
            return
        elif st.status == FILLED:
            raise RuntimeError("filled")
        else:
            say(True, "order is open at the broker (not filled)")
        ad.modify(oid, r, "BUY", qty, "LIMIT", px2, product="INTRADAY", tag=tag)
        say(True, f"modified price to ₹{px2}")
        st = wait_status(ad, oid, (PENDING, FILLED), 4)
        if st and st.status == FILLED:
            raise RuntimeError("filled")
        ad.cancel(oid, r, tag=tag)
        st = wait_status(ad, oid, (CANCELLED, FILLED), 10)
        if st and st.status == FILLED:
            raise RuntimeError("filled")
        say(bool(st and st.status == CANCELLED),
            "cancelled and confirmed" if st and st.status == CANCELLED else f"{label} cancel not confirmed (status {st.status if st else '?'})")
    except Exception as e:
        if str(e) != "filled":
            say(False, f"{label} order check error: {e} (check order {oid} in the broker app)")
            return
        print("  ⚠️  the test order filled; selling it at market now")
        try:
            xid = ad.place(r, "SELL", qty, "MARKET", product="INTRADAY", tag=tag + "X")
            xs = wait_status(ad, xid, (FILLED, REJECTED), 10)
            say(bool(xs and xs.status == FILLED), f"exit order {xid}: {xs.status if xs else 'unknown'}")
        except Exception as e2:
            say(False, f"{label} EXIT FAILED: {e2} — square off {r['tsym']} in the broker app NOW")
    try:
        left = ad.positions().get(str(r["inst_id"]), 0)
        say(left == 0, "no position left" if left == 0 else f"{label} position left: {left} — square it off")
    except Exception as e:
        say(False, f"{label} positions check failed: {e}")


def main():
    args = [a.upper() for a in sys.argv[1:]]
    place = args[1] if len(args) > 1 and args[0] == "--PLACE" else None
    if place and place not in ADAPTERS:
        print(f"Unknown broker {place}; use ANGEL, DHAN or XTS")
        return 2
    print("\nPre-live order check" + (f" — with a test order on {BROKER_LABELS[place]}" if place else " (read-only)"))
    try:
        obj = angel_session()
        spot, inst = nifty_atm_ce(obj)
        print(f"  ℹ️  NIFTY {spot:,.2f} → test contract {inst['tsym']} (lot {inst['lot_size']})")
    except Exception as e:
        say(False, f"market data via Angel One failed: {e}")
        return 1
    names = [place] if place else [n for n in ("ANGEL", "DHAN", "XTS") if has_keys(n)]
    for name in ("ANGEL", "DHAN", "XTS"):
        if name not in names:
            if not place:
                print(f"\n{BROKER_LABELS[name]}\n  ⏭  skipped (no keys in .env)")
            continue
        ad, r = read_checks(name, obj, inst)
        if place and ad and r:
            if not market_open():
                say(False, "test order skipped: market is closed (run it between 09:15 and 15:25 IST)")
            else:
                order_check(name, ad, r, obj)
    print("\n" + ("ALL CHECKS PASSED" if not problems else f"{len(problems)} PROBLEM(S):\n  - " + "\n  - ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
