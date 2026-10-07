"""Check the saved broker logins (Angel One, Dhan, Jainam XTS) without placing any order.
Reads the keys from .env next to this file. Prints only account IDs and available funds.
Run: double-click test_connections.command, or  python3 test_connections.py"""

import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
ENV_FILE = os.path.join(BASE_DIR, ".env")

from dotenv import load_dotenv          # noqa: E402
load_dotenv(ENV_FILE)

from brokers.base import BrokerError    # noqa: E402
from brokers.angel import AngelAdapter  # noqa: E402
from brokers.dhan import DhanAdapter    # noqa: E402
from brokers.xts import XTSAdapter      # noqa: E402


def env(k):
    return (os.getenv(k) or "").strip()


def save_env(key, value):
    """Replace one KEY=VALUE line in .env (keeps every other line as it is)."""
    lines = open(ENV_FILE).read().splitlines() if os.path.exists(ENV_FILE) else []
    lines = [l for l in lines if not re.match(rf"\s*{key}\s*=", l)]
    lines.append(f"{key}={value}")
    with open(ENV_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)


def funds_line(ad):
    try:
        f = ad.funds()
        return f"available funds ₹{f:,.0f}" if f is not None else "funds not reported"
    except Exception as e:
        return f"funds check failed: {e}"


def test_angel():
    missing = [k for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_MPIN", "ANGEL_TOTP_SECRET") if not env(k)]
    if missing:
        return False, "missing in .env: " + ", ".join(missing)
    import pyotp
    from SmartApi import SmartConnect
    obj = SmartConnect(api_key=env("ANGEL_API_KEY"))
    s = obj.generateSession(env("ANGEL_CLIENT_ID"), env("ANGEL_MPIN"), pyotp.TOTP(env("ANGEL_TOTP_SECRET")).now())
    if not s or not s.get("status"):
        return False, f"login refused: {(s or {}).get('message', 'no response')}"
    ad = AngelAdapter({"ANGEL_CLIENT_ID": env("ANGEL_CLIENT_ID"), "_session_getter": lambda: obj})
    acct = ad.login()
    return True, f"login OK ({acct}), {funds_line(ad)}"


def test_dhan():
    creds = {k: env(k) for k in DhanAdapter.env_keys}
    if not creds["DHAN_CLIENT_ID"]:
        return False, "DHAN_CLIENT_ID missing in .env"
    old = creds["DHAN_ACCESS_TOKEN"]
    ad = DhanAdapter(creds, cache_dir=BASE_DIR)
    acct = ad.login()
    msg = f"login OK ({acct}), {funds_line(ad)}"
    if ad.creds.get("DHAN_ACCESS_TOKEN") and ad.creds["DHAN_ACCESS_TOKEN"] != old:
        save_env("DHAN_ACCESS_TOKEN", ad.creds["DHAN_ACCESS_TOKEN"])
        msg += "; new access token saved to .env"
    return True, msg


def test_xts():
    creds = {k: env(k) for k in XTSAdapter.env_keys}
    if not creds["XTS_API_KEY"] or not creds["XTS_API_SECRET"]:
        return None, "skipped (XTS_API_KEY / XTS_API_SECRET not in .env yet)"
    ad = XTSAdapter(creds)
    acct = ad.login()
    return True, f"login OK ({acct}), {funds_line(ad)}"


def main():
    print("\nBroker connection test (no orders are placed)\n")
    results = []
    for name, fn in (("Angel One", test_angel), ("Dhan", test_dhan), ("Jainam XTS", test_xts)):
        try:
            ok, msg = fn()
        except BrokerError as e:
            ok, msg = False, str(e)
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        mark = "✅" if ok else ("⏭" if ok is None else "❌")
        print(f"  {mark} {name:<11} {msg}")
        results.append(ok)
    print()
    return 0 if all(r is not False for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
