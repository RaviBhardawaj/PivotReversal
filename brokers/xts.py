"""XTS (Symphony Fintech) Interactive API adapter, set up for Jainam by default. Works with any
broker that offers XTS (IIFL, Nirmal Bang, ...): only the root URL differs. Uses plain `requests`."""

import json

import requests

from .base import (BrokerAdapter, BrokerError, OrderStatus, FILLED, PENDING, REJECTED,
                   CANCELLED, tick_round, to_float)

_TYPE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOPLIMIT"}
_PRODUCT = {"INTRADAY": "MIS", "CARRY": "NRML"}
_SEG = {"NFO": "NSEFO", "BFO": "BSEFO"}
JAINAM_ROOT = "https://jtrade.jainam.in:5000"     # Jainam XTS (retail); another XTS broker → set XTS_ROOT_URL


class XTSAdapter(BrokerAdapter):
    name = "XTS"
    env_keys = ["XTS_ROOT_URL", "XTS_API_KEY", "XTS_API_SECRET", "XTS_CLIENT_ID"]
    secret_keys = ["XTS_API_KEY", "XTS_API_SECRET"]

    def __init__(self, creds, log=print):
        super().__init__(creds, log)
        root = str(creds.get("XTS_ROOT_URL", "") or JAINAM_ROOT).strip().rstrip("/")
        if root.endswith("/interactive"):
            root = root[: -len("/interactive")]
        self.root = root
        self.token = ""
        self.user_id = ""
        self.investor = True
        self._http = requests.Session()

    def _client_id(self):
        return self.user_id if self.investor else (self.creds.get("XTS_CLIENT_ID") or self.user_id)

    def _req(self, method, path, body=None, params=None, form=False):
        if not self.root:
            raise BrokerError("XTS root URL missing (ask your broker for the XTS API address)")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = self.token
        try:
            if form:
                r = self._http.request(method, self.root + path, data=body, timeout=10)
            else:
                r = self._http.request(method, self.root + path, headers=headers, params=params,
                                       data=json.dumps(body) if body is not None else None, timeout=10)
        except Exception as e:
            raise BrokerError(f"XTS {method} {path} network error: {type(e).__name__}")
        try:
            d = r.json()
        except Exception:
            raise BrokerError(f"XTS {path} returned non-JSON (HTTP {r.status_code})")
        if d.get("type") == "error" or not (200 <= r.status_code < 300):
            raise BrokerError(f"XTS {path} error: {d.get('description') or d.get('code') or r.status_code}")
        return d.get("result")

    # ── session ─────────────────────────────────────────────
    def login(self):
        if not self.creds.get("XTS_API_KEY") or not self.creds.get("XTS_API_SECRET"):
            raise BrokerError("XTS app key or secret missing")
        res = self._req("POST", "/interactive/user/session",
                        {"appKey": self.creds["XTS_API_KEY"], "secretKey": self.creds["XTS_API_SECRET"],
                         "source": "WEBAPI"}, form=True) or {}
        self.token = res.get("token", "")
        if not self.token:
            raise BrokerError("XTS login returned no token")
        self.user_id = str(res.get("userID", ""))
        self.investor = bool(res.get("isInvestorClient", True))
        if not self.investor and not self.creds.get("XTS_CLIENT_ID"):
            raise BrokerError("This XTS login is a dealer account: set XTS_CLIENT_ID to the trading client code")
        self.connected = True
        self.account = self._client_id()
        return self.account

    def funds(self):
        try:
            res = self._req("GET", "/interactive/user/balance", params={"clientID": self._client_id()}) or {}
            bl = (res.get("BalanceList") or [{}])[0]
            sub = bl.get("limitObject", {}).get("RMSSubLimits", {})
            return to_float(sub.get("netMarginAvailable"), None)
        except Exception:
            return None

    # ── instruments ─────────────────────────────────────────
    def resolve(self, inst):
        # XTS identifies NSE/BSE F&O contracts by the exchange token (the same one Angel uses)
        if not inst.get("token"):
            raise BrokerError(f"No exchange token for {inst.get('tsym')}")
        out = dict(inst)
        out["inst_id"] = str(inst["token"])
        out["segment"] = _SEG.get(inst.get("exchange", "NFO"), "NSEFO")
        return out

    # ── orders ──────────────────────────────────────────────
    def place(self, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        body = {
            "exchangeSegment": inst["segment"],
            "exchangeInstrumentID": int(inst["inst_id"]),
            "productType": _PRODUCT.get(product, "MIS"),
            "orderType": _TYPE[order_type],
            "orderSide": side,
            "timeInForce": "DAY",
            "disclosedQuantity": 0,
            "orderQuantity": int(qty),
            "limitPrice": tick_round(price) if order_type != "MARKET" else 0,
            "stopPrice": tick_round(trigger) if order_type == "SL" else 0,
            "orderUniqueIdentifier": tag or "pivot",
            "clientID": self._client_id(),
        }
        res = self._req("POST", "/interactive/orders", body) or {}
        oid = str(res.get("AppOrderID", ""))
        if not oid:
            raise BrokerError(f"XTS returned no order ID: {res}")
        return oid

    def modify(self, order_id, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        body = {
            "appOrderID": int(order_id),
            "modifiedProductType": _PRODUCT.get(product, "MIS"),
            "modifiedOrderType": _TYPE[order_type],
            "modifiedOrderQuantity": int(qty),
            "modifiedDisclosedQuantity": 0,
            "modifiedLimitPrice": tick_round(price) if order_type != "MARKET" else 0,
            "modifiedStopPrice": tick_round(trigger) if order_type == "SL" else 0,
            "modifiedTimeInForce": "DAY",
            "orderUniqueIdentifier": tag or "pivot",
            "clientID": self._client_id(),
        }
        self._req("PUT", "/interactive/orders", body)

    def cancel(self, order_id, inst=None, tag=""):
        self._req("DELETE", "/interactive/orders",
                  params={"appOrderID": int(order_id), "orderUniqueIdentifier": tag or "pivot",
                          "clientID": self._client_id()})

    def orders(self):
        params = {} if self.investor else {"clientID": self._client_id()}
        res = self._req("GET", "/interactive/orders", params=params) or []
        out = {}
        for o in res:
            st = str(o.get("OrderStatus", ""))
            if st == "Filled":
                s = FILLED
            elif st == "Rejected":
                s = REJECTED
            elif st == "Cancelled":
                s = CANCELLED
            else:
                s = PENDING
            oid = str(o.get("AppOrderID", ""))
            out[oid] = OrderStatus(oid, s, to_float(o.get("OrderAverageTradedPrice")),
                                   o.get("CumulativeQuantity", 0), o.get("CancelRejectReason", ""),
                                   o.get("OrderUniqueIdentifier", ""))
        return out

    def positions(self):
        params = {"dayOrNet": "NetWise"}
        if not self.investor:
            params["clientID"] = self._client_id()
        res = self._req("GET", "/interactive/portfolio/positions", params=params) or {}
        out = {}
        for p in (res.get("positionList") or []):
            q = int(to_float(p.get("Quantity"), 0))
            if q:
                out[str(p.get("ExchangeInstrumentId"))] = q
        return out
