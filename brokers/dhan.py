"""Dhan (DhanHQ v2 REST) adapter. Uses plain `requests`; no extra package needed."""

import os
import csv
from datetime import datetime, timedelta, timezone

import requests

from .base import (BrokerAdapter, BrokerError, OrderStatus, FILLED, PENDING, REJECTED,
                   CANCELLED, tick_round, to_float)

API = "https://api.dhan.co/v2"
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"   # PIN + TOTP login (same call as dhan_login.py)
SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

_TYPE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"}
_PRODUCT = {"INTRADAY": "INTRADAY", "CARRY": "MARGIN"}
_SEG = {"NFO": "NSE_FNO", "BFO": "BSE_FNO"}


def _col(header, *names):
    """Index of the first matching column name (case-insensitive), or -1."""
    up = [h.strip().upper() for h in header]
    for n in names:
        if n.upper() in up:
            return up.index(n.upper())
    return -1


class DhanAdapter(BrokerAdapter):
    name = "DHAN"
    env_keys = ["DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET", "DHAN_ACCESS_TOKEN"]
    secret_keys = ["DHAN_PIN", "DHAN_TOTP_SECRET", "DHAN_ACCESS_TOKEN"]
    # A new PIN+TOTP token replaces the previous one, so every Dhan adapter in the app
    # (orders and market data) shares the newest token instead of logging each other out.
    _shared_token = ""

    def __init__(self, creds, log=print, cache_dir="."):
        super().__init__(creds, log)
        self.cache_dir = cache_dir
        self.cache_file = os.path.join(cache_dir, "dhan_scrip_cache.csv")
        self.token_file = os.path.join(cache_dir, ".dhan_token")
        self.token_json_file = os.path.join(cache_dir, "dhan_token.json")
        self.config_json_file = os.path.join(cache_dir, "config.json")
        self._index = None          # {(UNDERLYING, 'YYYY-MM-DD', strike, 'CE'): (security_id, lot)}
        self._http = requests.Session()
        
        # High-performance connection pooling for low latency (<50ms execution)
        try:
            adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=1)
            self._http.mount("https://", adapter)
            self._http.mount("http://", adapter)
        except Exception:
            pass

        self._load_config_json_creds()
        self._load_cached_token()

    def _load_config_json_creds(self):
        """Loads client_id, pin, totp_secret from config.json if not in self.creds."""
        if os.path.exists(self.config_json_file):
            try:
                import json
                with open(self.config_json_file, "r") as f:
                    cfg = json.load(f)
                if not self.creds.get("DHAN_CLIENT_ID") and cfg.get("client_id"):
                    self.creds["DHAN_CLIENT_ID"] = str(cfg["client_id"]).strip()
                if not self.creds.get("DHAN_PIN") and cfg.get("pin"):
                    self.creds["DHAN_PIN"] = str(cfg["pin"]).strip()
                if not self.creds.get("DHAN_TOTP_SECRET") and cfg.get("totp_secret"):
                    self.creds["DHAN_TOTP_SECRET"] = str(cfg["totp_secret"]).strip()
                if not self.creds.get("DHAN_ACCESS_TOKEN") and cfg.get("access_token"):
                    self.creds["DHAN_ACCESS_TOKEN"] = str(cfg["access_token"]).strip()
            except Exception:
                pass

    def _load_cached_token(self):
        """Loads valid token from .dhan_token or dhan_token.json or config.json."""
        tok = None
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    tok = f.read().strip()
            except Exception:
                pass
        if not tok and os.path.exists(self.token_json_file):
            try:
                import json
                with open(self.token_json_file, "r") as f:
                    tok = json.load(f).get("accessToken")
            except Exception:
                pass
        if not tok and self.creds.get("DHAN_ACCESS_TOKEN"):
            tok = str(self.creds["DHAN_ACCESS_TOKEN"]).strip()

        if tok:
            DhanAdapter._shared_token = tok
            self.creds["DHAN_ACCESS_TOKEN"] = tok

    def _persist_token(self, token: str):
        """Saves active token to .dhan_token, dhan_token.json, and updates .env so all processes share it."""
        token = (token or "").strip()
        if not token:
            return
        DhanAdapter._shared_token = token
        self.creds["DHAN_ACCESS_TOKEN"] = token
        try:
            with open(self.token_file, "w") as f:
                f.write(token)
        except Exception:
            pass
        try:
            import json
            with open(self.token_json_file, "w") as f:
                json.dump({"accessToken": token}, f, indent=2)
        except Exception:
            pass
        # Also update .env
        env_path = os.path.join(self.cache_dir, ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    content = f.read()
                import re
                if "DHAN_ACCESS_TOKEN=" in content:
                    content = re.sub(r"DHAN_ACCESS_TOKEN=.*", f"DHAN_ACCESS_TOKEN={token}", content)
                else:
                    content += f"\nDHAN_ACCESS_TOKEN={token}\n"
                with open(env_path, "w") as f:
                    f.write(content)
            except Exception:
                pass

    # ── HTTP ────────────────────────────────────────────────
    def _headers(self):
        self._load_cached_token()
        if DhanAdapter._shared_token and self.creds.get("DHAN_ACCESS_TOKEN") != DhanAdapter._shared_token:
            self.creds["DHAN_ACCESS_TOKEN"] = DhanAdapter._shared_token
        return {
            "access-token": self.creds.get("DHAN_ACCESS_TOKEN", ""),
            "client-id": self.creds.get("DHAN_CLIENT_ID", ""),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _req(self, method, path, body=None, retries=3):
        if body is not None:
            body = dict(body, dhanClientId=self.creds.get("DHAN_CLIENT_ID", ""))
        for attempt in range(retries):
            try:
                r = self._http.request(method, API + path, json=body, headers=self._headers(), timeout=12)
            except Exception as e:
                if attempt == retries - 1:
                    raise BrokerError(f"Dhan {method} {path} network error: {type(e).__name__}")
                import time
                time.sleep(1.0)
                continue

            # Auto-refresh token on HTTP 401 Unauthorized
            if r.status_code == 401:
                self.log("[DHAN] Token expired (HTTP 401). Refreshing token...")
                try:
                    self._new_token()
                    # Retry with new token
                    r = self._http.request(method, API + path, json=body, headers=self._headers(), timeout=12)
                except Exception as auth_err:
                    self.log(f"[DHAN] Auto token refresh failed: {auth_err}")

            try:
                data = r.json() if r.content else {}
            except Exception:
                data = {}
            if 200 <= r.status_code < 300:
                return data
            msg = data.get("errorMessage") or data.get("remarks") or r.text[:200]
            code = data.get("errorCode", r.status_code)
            # Throttling handling (DH-904)
            if "DH-904" in str(code) or "DH-904" in str(msg) or "RATE LIMIT" in str(msg).upper():
                import time
                time.sleep(1.5 * (attempt + 1))
                continue
            if attempt == retries - 1:
                raise BrokerError(f"Dhan {path} error {code}: {msg}")
        return {}

    # ── session ─────────────────────────────────────────────
    def _new_token(self):
        """Fresh access token from client ID + PIN + TOTP, with file persistence and retry handling."""
        import pyotp, time
        # First check if another process generated a token within the last 2 minutes
        if os.path.exists(self.token_file):
            try:
                mtime = os.path.getmtime(self.token_file)
                if time.time() - mtime < 120:
                    self._load_cached_token()
                    try:
                        self._req("GET", "/fundlimit")
                        return
                    except Exception:
                        pass
            except Exception:
                pass

        for attempt in range(4):
            totp = pyotp.TOTP(str(self.creds["DHAN_TOTP_SECRET"]).strip().upper().replace(" ", "")).now()
            try:
                r = requests.post(AUTH_URL, params={"dhanClientId": self.creds["DHAN_CLIENT_ID"],
                                                    "pin": self.creds["DHAN_PIN"], "totp": totp},
                                  headers={"Accept": "application/json"}, timeout=20)
                d = r.json()
            except Exception as e:
                if attempt == 3:
                    raise BrokerError(f"Dhan login (PIN + TOTP) failed: {type(e).__name__}")
                time.sleep(2)
                continue

            tok = d.get("accessToken") or (d.get("data") or {}).get("accessToken")
            if tok:
                self._persist_token(tok)
                return
            msg = d.get("message") or d.get("errorMessage") or str(r.status_code)
            if "once every 2 minutes" in str(msg):
                # Another process just generated or rate-limited; wait and reload cached token
                time.sleep(3)
                self._load_cached_token()
                try:
                    self._req("GET", "/fundlimit")
                    return
                except Exception:
                    pass
                if attempt < 3:
                    time.sleep(15)
                    continue
            if attempt == 3:
                raise BrokerError(f"Dhan login (PIN + TOTP) failed: {msg}")

    def login(self):
        if not self.creds.get("DHAN_CLIENT_ID"):
            raise BrokerError("Dhan client ID missing")
        self._load_cached_token()
        can_generate = bool(self.creds.get("DHAN_PIN") and self.creds.get("DHAN_TOTP_SECRET"))
        if not self.creds.get("DHAN_ACCESS_TOKEN") and not can_generate:
            raise BrokerError("Dhan needs either PIN + TOTP secret, or an access token")
        try:
            if not self.creds.get("DHAN_ACCESS_TOKEN"):
                raise BrokerError("no token yet")
            self._req("GET", "/fundlimit")      # cheapest call that proves the token works
        except BrokerError as be:
            if not can_generate:
                raise
            self._new_token()                   # saved token missing or expired → log in again
            self._req("GET", "/fundlimit")
        self.connected = True
        self.account = str(self.creds.get("DHAN_CLIENT_ID"))
        return self.account

    def funds(self):
        d = self._req("GET", "/fundlimit")
        # Dhan spells it 'availabelBalance'; accept the correct spelling too
        return to_float(d.get("availabelBalance", d.get("availableBalance")), None)

    # ── market data ─────────────────────────────────────────
    def daily_candles(self, security_id, segment="NSE_FNO", instrument="OPTIDX", days=20, now=None):
        """Daily candles [(YYYY-MM-DD, open, high, low, close), ...] from Dhan's historical API
        (the exchange's official daily OHLC). Raises BrokerError with Dhan's message on failure."""
        now = now or datetime.now()
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "expiryCode": 0, "oi": False,
                "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
                "toDate": (now + timedelta(days=1)).strftime("%Y-%m-%d")}     # toDate is exclusive
        import time
        for attempt in range(3):
            try:
                r = self._http.post(API + "/charts/historical", json=body, headers=self._headers(), timeout=15)
            except Exception as e:
                if attempt == 2:
                    raise BrokerError(f"Dhan historical network error: {type(e).__name__}")
                time.sleep(1.0)
                continue
            try:
                d = r.json() if r.content else {}
            except Exception:
                d = {}
            if not (200 <= r.status_code < 300):
                msg = d.get("errorMessage") or d.get("remarks") or r.text[:200]
                code = d.get("errorCode", r.status_code)
                if "DH-904" in str(code) or "DH-904" in str(msg):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise BrokerError(f"Dhan historical error {code}: {msg}")
            ts, o, h, l, c = (d.get(k) or [] for k in ("timestamp", "open", "high", "low", "close"))
            ist = timezone(timedelta(hours=5, minutes=30))
            out = []
            for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
                try:
                    day = datetime.fromtimestamp(float(ts[i]), ist).strftime("%Y-%m-%d")
                    out.append((day, float(o[i]), float(h[i]), float(l[i]), float(c[i])))
                except Exception:
                    continue
            return out
        return []

    def intraday_bars(self, security_id, day, segment="NSE_FNO", instrument="OPTIDX"):
        """One session's 1-minute candles [(datetime IST, open, high, low, close), ...],
        09:15-15:30 IST trades inclusive, oldest first. Raises BrokerError with Dhan's message on failure."""
        import time
        # fromDate must be 09:00:00 because Dhan historical/intraday treats fromDate as exclusive (> fromDate)
        # using 09:15:00 would drop the crucial 09:15:00 opening candle!
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "interval": "1", "oi": False,
                "fromDate": f"{day} 09:00:00", "toDate": f"{day} 15:35:00"}
        for attempt in range(3):
            try:
                r = self._http.post(API + "/charts/intraday", json=body, headers=self._headers(), timeout=20)
            except Exception as e:
                if attempt == 2:
                    raise BrokerError(f"Dhan intraday network error: {type(e).__name__}")
                time.sleep(1.0)
                continue
            try:
                d = r.json() if r.content else {}
            except Exception:
                d = {}
            if not (200 <= r.status_code < 300):
                msg = d.get("errorMessage") or d.get("remarks") or r.text[:200]
                code = d.get("errorCode", r.status_code)
                if "DH-904" in str(code) or "DH-904" in str(msg):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise BrokerError(f"Dhan intraday error {code}: {msg}")
            ts, o, h, l, c = (d.get(k) or [] for k in ("timestamp", "open", "high", "low", "close"))
            ist = timezone(timedelta(hours=5, minutes=30))
            bars = []
            for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
                try:
                    t = datetime.fromtimestamp(float(ts[i]), ist)
                except Exception:
                    continue
                # Include full trading session 09:15:00 through 15:30:00
                if t.strftime("%Y-%m-%d") == day and "09:15" <= t.strftime("%H:%M") <= "15:30":
                    bars.append((t, float(o[i]), float(h[i]), float(l[i]), float(c[i])))
            bars.sort()
            return bars
        return []

    def intraday_day_ohlc(self, security_id, day, segment="NSE_FNO", instrument="OPTIDX"):
        """One session's OHLC rebuilt from Dhan's 1-minute candles (09:15-15:30 IST trades only):
        (open, high, low, close, last_bar_time) or None when there is no data."""
        bars = self.intraday_bars(security_id, day, segment, instrument)
        if not bars:
            return None
        return (bars[0][1], max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4],
                bars[-1][0].strftime("%H:%M"))

    # ── instruments ─────────────────────────────────────────
    def _load_index(self):
        if self._index is not None:
            return
        fresh = False
        try:
            st = os.stat(self.cache_file)
            fresh = datetime.fromtimestamp(st.st_mtime).date() == datetime.now().date() and st.st_size > 1000
        except OSError:
            pass
        if not fresh:
            try:
                r = requests.get(SCRIP_URL, timeout=60)
                r.raise_for_status()
                tmp = self.cache_file + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, self.cache_file)
            except Exception as e:
                if not os.path.exists(self.cache_file):
                    self.log(f"[DHAN] Instrument list download failed ({type(e).__name__}); using exchange tokens")
                    self._index = {}
                    return
                self.log("[DHAN] Instrument list download failed; using yesterday's copy")
        idx = {}
        try:
            with open(self.cache_file, newline="", encoding="utf-8", errors="ignore") as f:
                rd = csv.reader(f)
                h = next(rd)
                c_inst = _col(h, "SEM_INSTRUMENT_NAME", "INSTRUMENT")
                c_id   = _col(h, "SEM_SMST_SECURITY_ID", "SECURITY_ID")
                c_sym  = _col(h, "SEM_TRADING_SYMBOL", "SYMBOL_NAME")
                c_und  = _col(h, "UNDERLYING_SYMBOL")
                c_exp  = _col(h, "SEM_EXPIRY_DATE", "SM_EXPIRY_DATE")
                c_stk  = _col(h, "SEM_STRIKE_PRICE", "STRIKE_PRICE")
                c_opt  = _col(h, "SEM_OPTION_TYPE", "OPTION_TYPE")
                c_lot  = _col(h, "SEM_LOT_UNITS", "LOT_SIZE")
                if min(c_inst, c_id, c_exp, c_stk, c_opt, c_lot) < 0 or (c_sym < 0 and c_und < 0):
                    raise ValueError("unexpected columns")
                for row in rd:
                    try:
                        if row[c_inst].strip() != "OPTIDX":
                            continue
                        und = (row[c_und] if c_und >= 0 else row[c_sym].split("-")[0]).strip().upper()
                        key = (und, row[c_exp].strip()[:10], round(float(row[c_stk]), 2), row[c_opt].strip().upper())
                        idx[key] = (row[c_id].strip(), int(float(row[c_lot])))
                    except Exception:
                        continue
        except Exception as e:
            self.log(f"[DHAN] Could not read instrument list ({e}); using exchange tokens")
        self._index = idx
        self.log(f"[DHAN] Instrument list ready: {len(idx)} index options")

    def resolve(self, inst):
        self._load_index()
        out = dict(inst)
        out["segment"] = _SEG.get(inst.get("exchange", "NFO"), "NSE_FNO")
        sid = None
        try:
            exp = datetime.strptime(str(inst.get("expiry", "")).upper(), "%d%b%Y").strftime("%Y-%m-%d")
            hit = self._index.get((str(inst["symbol"]).upper(), exp, round(float(inst["strike"]), 2), inst["opt_type"]))
            if hit:
                sid, lot = hit
                if inst.get("lot_size") and lot and lot != int(inst["lot_size"]):
                    raise BrokerError(f"Dhan lot size {lot} differs from Angel {inst['lot_size']} for {inst.get('tsym')}")
        except BrokerError:
            raise
        except Exception:
            sid = None
        if not sid:
            # Dhan's F&O security ID is the exchange token, which the Angel instrument list already has
            if not inst.get("token"):
                raise BrokerError(f"No Dhan security ID for {inst.get('tsym')}")
            sid = str(inst["token"])
        out["inst_id"] = str(sid)
        return out

    # ── orders ──────────────────────────────────────────────
    def _body(self, inst, side, qty, order_type, price, trigger, product):
        cid = self.creds.get("DHAN_CLIENT_ID", "")
        b = {
            "transactionType": side,
            "exchangeSegment": inst["segment"],
            "productType": _PRODUCT.get(product, "INTRADAY"),
            "orderType": _TYPE[order_type],
            "validity": "DAY",
            "securityId": str(inst["inst_id"]),
            "quantity": int(qty),
            "disclosedQuantity": 0,
            "price": tick_round(price) if order_type not in ("MARKET", "SL-M") else 0,
            "triggerPrice": tick_round(trigger) if order_type in ("SL", "SL-M") else 0,
            "afterMarketOrder": False,
        }
        if cid:
            b["dhanClientId"] = str(cid)
        return b

    def place(self, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        b = self._body(inst, side, qty, order_type, price, trigger, product)
        if tag:
            b["correlationId"] = tag
        d = self._req("POST", "/orders", b)
        oid = str(d.get("orderId", ""))
        if not oid:
            raise BrokerError(f"Dhan returned no order ID: {d}")
        if str(d.get("orderStatus", "")).upper() == "REJECTED":
            raise BrokerError(f"Dhan rejected the order {oid}")
        return oid

    def modify(self, order_id, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        cid = self.creds.get("DHAN_CLIENT_ID", "")
        b = {
            "orderId": str(order_id),
            "orderType": _TYPE[order_type],
            "quantity": int(qty),
            "price": tick_round(price) if order_type != "MARKET" else 0,
            "triggerPrice": tick_round(trigger) if order_type == "SL" else 0,
            "disclosedQuantity": 0,
            "validity": "DAY",
        }
        if cid:
            b["dhanClientId"] = str(cid)
        self._req("PUT", f"/orders/{order_id}", b)

    def cancel(self, order_id, inst=None, tag=""):
        self._req("DELETE", f"/orders/{order_id}")

    def orders(self):
        d = self._req("GET", "/orders")
        rows = d if isinstance(d, list) else (d.get("data") or [])
        out = {}
        for o in rows:
            st = str(o.get("orderStatus", "")).upper()
            if st == "TRADED":
                s = FILLED
            elif st in ("REJECTED", "EXPIRED"):
                s = REJECTED
            elif st == "CANCELLED":
                s = CANCELLED
            else:
                s = PENDING
            oid = str(o.get("orderId", ""))
            out[oid] = OrderStatus(oid, s, to_float(o.get("averageTradedPrice")), o.get("filledQty", 0),
                                   o.get("omsErrorDescription", ""), o.get("correlationId", ""))
        return out

    def positions(self):
        d = self._req("GET", "/positions")
        rows = d if isinstance(d, list) else (d.get("data") or [])
        out = {}
        for p in rows:
            q = int(to_float(p.get("netQty"), 0))
            if q:
                out[str(p.get("securityId"))] = q
        return out
