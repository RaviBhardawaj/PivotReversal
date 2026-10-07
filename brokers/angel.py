"""Angel One SmartAPI adapter. Reuses the app's existing SmartConnect session (Session.obj)."""

from .base import (BrokerAdapter, BrokerError, OrderStatus, FILLED, PENDING, REJECTED,
                   CANCELLED, tick_round, to_float)

_TYPE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOPLOSS_LIMIT"}
_PRODUCT = {"INTRADAY": "INTRADAY", "CARRY": "CARRYFORWARD"}
# Angel answers these when the session has gone stale (AG8004 "Invalid API Key" overnight)
_AUTH_ERRORS = ("AG8001", "AG8002", "AG8003", "AG8004", "AB1010", "AB1011",
                "INVALID TOKEN", "INVALID API KEY", "TOKEN EXPIRED")


def _is_auth_error(r) -> bool:
    if isinstance(r, dict):
        if r.get("status"):
            return False
        txt = f"{r.get('errorcode') or r.get('errorCode') or ''} {r.get('message', '')}"
    else:
        txt = str(r)
    return any(k in txt.upper() for k in _AUTH_ERRORS)


class AngelAdapter(BrokerAdapter):
    name = "ANGEL"
    env_keys = ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_MPIN", "ANGEL_TOTP_SECRET"]
    secret_keys = ["ANGEL_API_KEY", "ANGEL_MPIN", "ANGEL_TOTP_SECRET"]

    def __init__(self, creds, log=print):
        super().__init__(creds, log)
        self._get_obj = creds.get("_session_getter")      # lambda: Session.obj
        self._relogin = creds.get("_session_relogin")     # logs the app in again (new Session.obj)
        self._variety = {}                                # order_id -> NORMAL / STOPLOSS

    def _obj(self):
        obj = self._get_obj() if self._get_obj else None
        if obj is None:
            raise BrokerError("Angel One is not logged in yet (start the feed first)")
        return obj

    def _call(self, fn):
        """Run fn(SmartConnect). If Angel says the session is no longer valid, log in again once
        and repeat the call with the new session."""
        try:
            r = fn(self._obj())
        except BrokerError:
            raise
        except Exception as e:
            r = e
        if _is_auth_error(r) and self._relogin:
            self.log("[ORDERS] Angel One session expired — logging in again")
            try:
                self._relogin()
            except Exception as e:
                raise BrokerError(f"Angel One login again failed: {e}")
            r = fn(self._obj())
        if isinstance(r, Exception):
            raise r
        return r

    def login(self) -> str:
        r = self._call(lambda o: o.rmsLimit())
        if not r or not r.get("status"):
            msg = (r or {}).get('message', 'no response')
            if _is_auth_error(r):
                msg += (" even after a fresh login. Angel One is refusing this API key: check in the"
                        " SmartAPI dashboard that the app/key is active and that its registered"
                        " static IP is the one this computer uses now")
            raise BrokerError(f"Angel One session check failed: {msg}")
        self.connected = True
        self.account = str(self.creds.get("ANGEL_CLIENT_ID", "") or "Angel One")
        return self.account

    def funds(self):
        r = self._call(lambda o: o.rmsLimit())
        if r and r.get("status") and r.get("data"):
            return to_float(r["data"].get("availablecash"), None)
        return None

    def resolve(self, inst):
        if not inst.get("token") or not inst.get("tsym"):
            raise BrokerError(f"No Angel token/symbol for {inst.get('symbol')} {inst.get('strike')} {inst.get('opt_type')}")
        out = dict(inst)
        out["inst_id"] = str(inst["token"])
        out["segment"] = inst.get("exchange", "NFO")
        return out

    def _params(self, inst, side, qty, order_type, price, trigger, product):
        variety = "STOPLOSS" if order_type == "SL" else "NORMAL"
        p = {
            "variety": variety,
            "tradingsymbol": inst["tsym"],
            "symboltoken": str(inst["inst_id"]),
            "transactiontype": side,
            "exchange": inst["segment"],
            "ordertype": _TYPE[order_type],
            "producttype": _PRODUCT.get(product, "INTRADAY"),
            "duration": "DAY",
            "price": str(tick_round(price)) if order_type != "MARKET" else "0",
            "triggerprice": str(tick_round(trigger)) if order_type == "SL" else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(qty)),
        }
        return variety, p

    def place(self, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        variety, p = self._params(inst, side, qty, order_type, price, trigger, product)
        if tag:
            p["ordertag"] = tag
        obj = self._obj()
        try:
            if not hasattr(obj, "placeOrderFullResponse"):        # older smartapi-python
                oid = obj.placeOrder(p)
                if not oid:
                    raise BrokerError("Angel rejected the order (no order ID returned)")
                self._variety[str(oid)] = variety
                return str(oid)
            r = self._call(lambda o: o.placeOrderFullResponse(p))
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Angel place failed: {e}")
        if isinstance(r, dict) and r.get("status") and r.get("data") and r["data"].get("orderid"):
            oid = str(r["data"]["orderid"])
            self._variety[oid] = variety
            return oid
        msg = r.get("message") if isinstance(r, dict) else r
        raise BrokerError(f"Angel rejected the order: {msg}")

    def modify(self, order_id, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        variety, p = self._params(inst, side, qty, order_type, price, trigger, product)
        p["variety"] = self._variety.get(str(order_id), variety)
        p["orderid"] = str(order_id)
        p.pop("squareoff", None); p.pop("stoploss", None); p.pop("transactiontype", None)
        r = self._call(lambda o: o.modifyOrder(p))
        if not r or not r.get("status"):
            raise BrokerError(f"Angel modify failed: {(r or {}).get('message', r)}")

    def cancel(self, order_id, inst=None, tag=""):
        r = self._call(lambda o: o.cancelOrder(str(order_id), self._variety.get(str(order_id), "NORMAL")))
        if not r or not r.get("status"):
            raise BrokerError(f"Angel cancel failed: {(r or {}).get('message', r)}")

    def orders(self):
        r = self._call(lambda o: o.orderBook())
        if not r or not r.get("status"):
            raise BrokerError(f"Angel order book failed: {(r or {}).get('message', r)}")
        out = {}
        for o in (r.get("data") or []):
            st = str(o.get("status", "")).lower()
            if st == "complete":
                s = FILLED
            elif st == "rejected":
                s = REJECTED
            elif st == "cancelled":
                s = CANCELLED
            else:
                s = PENDING
            oid = str(o.get("orderid", ""))
            if o.get("variety"):
                self._variety.setdefault(oid, o["variety"])
            out[oid] = OrderStatus(oid, s, to_float(o.get("averageprice")), o.get("filledshares", 0),
                                   o.get("text", ""), o.get("ordertag", ""))
        return out

    def positions(self):
        r = self._call(lambda o: o.position())
        if not r or not r.get("status"):
            raise BrokerError(f"Angel positions failed: {(r or {}).get('message', r)}")
        out = {}
        for p in (r.get("data") or []):
            q = int(to_float(p.get("netqty"), 0))
            if q:
                out[str(p.get("symboltoken"))] = q
        return out
