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

_TYPE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOP_LOSS"}
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
        self.cache_file = os.path.join(cache_dir, "dhan_scrip_cache.csv")
        self._index = None          # {(UNDERLYING, 'YYYY-MM-DD', strike, 'CE'): (security_id, lot)}
        self._http = requests.Session()

    # ── HTTP ────────────────────────────────────────────────
    def _headers(self):
        if DhanAdapter._shared_token and self.creds.get("DHAN_ACCESS_TOKEN") != DhanAdapter._shared_token:
            self.creds["DHAN_ACCESS_TOKEN"] = DhanAdapter._shared_token
        return {
            "access-token": self.creds.get("DHAN_ACCESS_TOKEN", ""),
            "client-id": self.creds.get("DHAN_CLIENT_ID", ""),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _req(self, method, path, body=None):
        if body is not None:
            body = dict(body, dhanClientId=self.creds.get("DHAN_CLIENT_ID", ""))
        try:
            r = self._http.request(method, API + path, json=body, headers=self._headers(), timeout=10)
        except Exception as e:
            raise BrokerError(f"Dhan {method} {path} network error: {type(e).__name__}")
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {}
        if not (200 <= r.status_code < 300):
            msg = data.get("errorMessage") or data.get("remarks") or r.text[:200]
            code = data.get("errorCode", r.status_code)
            raise BrokerError(f"Dhan {path} error {code}: {msg}")
        return data

    # ── session ─────────────────────────────────────────────
    def _new_token(self):
        """Fresh access token from client ID + PIN + TOTP, so nobody has to paste a token every day."""
        import pyotp
        totp = pyotp.TOTP(str(self.creds["DHAN_TOTP_SECRET"]).strip().upper().replace(" ", "")).now()
        try:
            r = requests.post(AUTH_URL, params={"dhanClientId": self.creds["DHAN_CLIENT_ID"],
                                                "pin": self.creds["DHAN_PIN"], "totp": totp},
                              headers={"Accept": "application/json"}, timeout=20)
            d = r.json()
        except Exception as e:
            raise BrokerError(f"Dhan login (PIN + TOTP) failed: {type(e).__name__}")
        tok = d.get("accessToken") or (d.get("data") or {}).get("accessToken")
        if not tok:
            raise BrokerError(f"Dhan login (PIN + TOTP) failed: {d.get('message') or d.get('errorMessage') or r.status_code}")
        self.creds["DHAN_ACCESS_TOKEN"] = tok
        DhanAdapter._shared_token = tok

    def login(self):
        if not self.creds.get("DHAN_CLIENT_ID"):
            raise BrokerError("Dhan client ID missing")
        can_generate = bool(self.creds.get("DHAN_PIN") and self.creds.get("DHAN_TOTP_SECRET"))
        if not self.creds.get("DHAN_ACCESS_TOKEN") and not can_generate:
            raise BrokerError("Dhan needs either PIN + TOTP secret, or an access token")
        try:
            if not self.creds.get("DHAN_ACCESS_TOKEN"):
                raise BrokerError("no token yet")
            self._req("GET", "/fundlimit")      # cheapest call that proves the token works
        except BrokerError:
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
        try:
            r = self._http.post(API + "/charts/historical", json=body, headers=self._headers(), timeout=15)
        except Exception as e:
            raise BrokerError(f"Dhan historical network error: {type(e).__name__}")
        try:
            d = r.json() if r.content else {}
        except Exception:
            d = {}
        if not (200 <= r.status_code < 300):
            raise BrokerError(f"Dhan historical error {d.get('errorCode', r.status_code)}: "
                              f"{d.get('errorMessage') or d.get('remarks') or r.text[:200]}")
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

    def intraday_day_ohlc(self, security_id, day, segment="NSE_FNO", instrument="OPTIDX"):
        """One session's OHLC rebuilt from Dhan's 1-minute candles (09:15-15:30 IST trades only):
        (open, high, low, close, last_bar_time) or None when there is no data."""
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "interval": "1", "oi": False,
                "fromDate": f"{day} 09:15:00", "toDate": f"{day} 15:31:00"}
        try:
            r = self._http.post(API + "/charts/intraday", json=body, headers=self._headers(), timeout=20)
        except Exception as e:
            raise BrokerError(f"Dhan intraday network error: {type(e).__name__}")
        try:
            d = r.json() if r.content else {}
        except Exception:
            d = {}
        if not (200 <= r.status_code < 300):
            raise BrokerError(f"Dhan intraday error {d.get('errorCode', r.status_code)}: "
                              f"{d.get('errorMessage') or d.get('remarks') or r.text[:200]}")
        ts, o, h, l, c = (d.get(k) or [] for k in ("timestamp", "open", "high", "low", "close"))
        ist = timezone(timedelta(hours=5, minutes=30))
        bars = []
        for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
            try:
                t = datetime.fromtimestamp(float(ts[i]), ist)
            except Exception:
                continue
            if t.strftime("%Y-%m-%d") == day and "09:15" <= t.strftime("%H:%M") <= "15:29":
                bars.append((t, float(o[i]), float(h[i]), float(l[i]), float(c[i])))
        if not bars:
            return None
        bars.sort()
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
        return {
            "transactionType": side,
            "exchangeSegment": inst["segment"],
            "productType": _PRODUCT.get(product, "INTRADAY"),
            "orderType": _TYPE[order_type],
            "validity": "DAY",
            "securityId": str(inst["inst_id"]),
            "quantity": int(qty),
            "disclosedQuantity": 0,
            "price": tick_round(price) if order_type != "MARKET" else 0,
            "triggerPrice": tick_round(trigger) if order_type == "SL" else 0,
            "afterMarketOrder": False,
        }

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
        b = {
            "orderId": str(order_id),
            "orderType": _TYPE[order_type],
            "quantity": int(qty),
            "price": tick_round(price) if order_type != "MARKET" else 0,
            "triggerPrice": tick_round(trigger) if order_type == "SL" else 0,
            "disclosedQuantity": 0,
            "validity": "DAY",
        }
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
