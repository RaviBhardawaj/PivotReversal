"""Adapter request/response tests with a fake HTTP layer. Run: python3.12 test_adapters.py [project dir]"""
import sys, json, os, tempfile
import os as _os
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
from brokers.dhan import DhanAdapter
from brokers.xts import XTSAdapter
from brokers.angel import AngelAdapter
from brokers.base import BrokerError, FILLED, PENDING, REJECTED

class Resp:
    def __init__(self, code, data):
        self.status_code = code; self._d = data; self.content = json.dumps(data).encode(); self.text = self.content.decode()
    def json(self): return self._d

class FakeHTTP:
    def __init__(self, routes): self.routes = routes; self.sent = []
    def request(self, method, url, json=None, data=None, headers=None, params=None, timeout=None):
        body = json if json is not None else data
        if isinstance(body, str):
            import json as _j; body = _j.loads(body)
        self.sent.append((method, url, body, headers, params))
        for (m, frag), fn in self.routes.items():
            if m == method and url.endswith(frag):
                return fn(body, params)
        return Resp(404, {"errorMessage": "no route"})

INST = {"symbol": "NIFTY", "exchange": "NFO", "token": "45678", "tsym": "NIFTY13OCT2622600CE",
        "expiry": "13OCT2026", "strike": 22600, "opt_type": "CE", "lot_size": 75}
n = 0
def check(c, m):
    global n
    if not c: raise AssertionError(m)
    n += 1

# ── Dhan ──
tmp = tempfile.mkdtemp()
with open(os.path.join(tmp, "dhan_scrip_cache.csv"), "w") as f:
    f.write("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME\n")
    f.write("NSE,D,45678,OPTIDX,0,NIFTY-Oct2026-22600-CE,75.0,NIFTY 13 OCT 22600 CALL,2026-10-13 14:30:00,22600.00000,CE,5.0000,W,OP,NA,\n")
    f.write("NSE,D,99999,OPTIDX,0,BANKNIFTY-Oct2026-22600-CE,35.0,x,2026-10-13 14:30:00,22600.00000,CE,5.0000,W,OP,NA,\n")
d = DhanAdapter({"DHAN_CLIENT_ID": "1100", "DHAN_ACCESS_TOKEN": "tok"}, log=lambda *a: None, cache_dir=tmp)
d._http = FakeHTTP({
    ("GET", "/fundlimit"): lambda b, p: Resp(200, {"availabelBalance": 52000.5}),
    ("POST", "/orders"): lambda b, p: Resp(200, {"orderId": "D1", "orderStatus": "PENDING"}),
    ("PUT", "/orders/D1"): lambda b, p: Resp(200, {"orderId": "D1"}),
    ("DELETE", "/orders/D1"): lambda b, p: Resp(200, {"orderId": "D1", "orderStatus": "CANCELLED"}),
    ("GET", "/orders"): lambda b, p: Resp(200, [{"orderId": "D1", "orderStatus": "TRADED", "averageTradedPrice": 101.5, "filledQty": 75, "correlationId": "PVX"},
                                                {"orderId": "D2", "orderStatus": "REJECTED", "omsErrorDescription": "margin"}]),
    ("GET", "/positions"): lambda b, p: Resp(200, [{"securityId": "45678", "netQty": 75}, {"securityId": "1", "netQty": 0}]),
})
check(d.login() == "1100", "dhan login"); check(d.funds() == 52000.5, "dhan funds")
h = d._http.sent[0][3]; check(h["access-token"] == "tok" and h["client-id"] == "1100", "dhan headers")
r = d.resolve(INST); check(r["inst_id"] == "45678" and r["segment"] == "NSE_FNO", f"dhan resolve {r}")
check(d.place(r, "BUY", 75, "LIMIT", price=100.52, tag="PVX") == "D1", "dhan place")
b = d._http.sent[-1][2]
check(b["securityId"] == "45678" and b["price"] == 100.5 and b["orderType"] == "LIMIT" and b["productType"] == "INTRADAY"
      and b["correlationId"] == "PVX" and b["dhanClientId"] == "1100" and b["quantity"] == 75, f"dhan body {b}")
d.place(r, "SELL", 75, "SL", price=76, trigger=80)
b = d._http.sent[-1][2]; check(b["orderType"] == "STOP_LOSS" and b["triggerPrice"] == 80 and b["price"] == 76, "dhan SL")
d.modify("D1", r, "SELL", 75, "MARKET"); b = d._http.sent[-1][2]
check(b["orderType"] == "MARKET" and b["price"] == 0 and b["orderId"] == "D1", "dhan modify")
d.cancel("D1")
o = d.orders(); check(o["D1"].status == FILLED and o["D1"].avg_price == 101.5 and o["D1"].tag == "PVX", "dhan orders")
check(o["D2"].status == REJECTED and o["D2"].message == "margin", "dhan reject")
check(d.positions() == {"45678": 75}, "dhan positions")
bad = dict(INST, lot_size=50)
try:
    d.resolve(bad); check(False, "lot mismatch must raise")
except BrokerError:
    check(True, "")
miss = dict(INST, strike=30000, token="777")
check(d.resolve(miss)["inst_id"] == "777", "dhan falls back to exchange token")
d2 = DhanAdapter({"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "bad"}, log=lambda *a: None, cache_dir=tmp)
d2._http = FakeHTTP({("GET", "/fundlimit"): lambda b, p: Resp(401, {"errorCode": "DH-901", "errorMessage": "Invalid token"})})
try:
    d2.login(); check(False, "bad token must fail")
except BrokerError as e:
    check("Invalid token" in str(e) and "bad" not in str(e), f"dhan error text {e}")
# PIN + TOTP login when the saved token is expired
import brokers.dhan as DM
posted=[]
class _R:
    status_code=200
    def json(self): return {"accessToken":"NEWTOKEN","dhanClientId":"1100"}
DM.requests.post=lambda url,params=None,headers=None,timeout=None: (posted.append((url,params)),_R())[1]
d3=DhanAdapter({"DHAN_CLIENT_ID":"1100","DHAN_PIN":"123456","DHAN_TOTP_SECRET":"JBSWY3DPEHPK3PXP","DHAN_ACCESS_TOKEN":"OLD"},log=lambda *a:None,cache_dir=tmp)
calls=[]
def fl(b,p):
    tok=d3._http.sent[-1][3]["access-token"]; calls.append(tok)
    return Resp(200,{"availabelBalance":1}) if tok=="NEWTOKEN" else Resp(401,{"errorCode":"DH-901","errorMessage":"Invalid token"})
d3._http=FakeHTTP({("GET","/fundlimit"):fl})
check(d3.login()=="1100" and calls==["OLD","NEWTOKEN"], f"dhan token refresh {calls}")
check(posted[0][0]=="https://auth.dhan.co/app/generateAccessToken" and posted[0][1]["pin"]=="123456" and len(posted[0][1]["totp"])==6, "dhan auth call")
d4=DhanAdapter({"DHAN_CLIENT_ID":"1100","DHAN_PIN":"1","DHAN_TOTP_SECRET":"JBSWY3DPEHPK3PXP"},log=lambda *a:None,cache_dir=tmp)
d4._http=FakeHTTP({("GET","/fundlimit"):fl})
check(d4.login()=="1100", "dhan login with no saved token")
print("dhan ok")

# ── XTS ──
x = XTSAdapter({"XTS_ROOT_URL": "https://xts.example.com:3000/interactive/", "XTS_API_KEY": "k", "XTS_API_SECRET": "s"}, log=lambda *a: None)
check(x.root == "https://xts.example.com:3000", "xts root normalised")
x._http = FakeHTTP({
    ("POST", "/interactive/user/session"): lambda b, p: Resp(200, {"type": "success", "result": {"token": "T", "userID": "U1", "isInvestorClient": True}}),
    ("GET", "/interactive/user/balance"): lambda b, p: Resp(200, {"type": "success", "result": {"BalanceList": [{"limitObject": {"RMSSubLimits": {"netMarginAvailable": "1234.5"}}}]}}),
    ("POST", "/interactive/orders"): lambda b, p: Resp(200, {"type": "success", "result": {"AppOrderID": 555}}),
    ("PUT", "/interactive/orders"): lambda b, p: Resp(200, {"type": "success", "result": {}}),
    ("DELETE", "/interactive/orders"): lambda b, p: Resp(200, {"type": "success", "result": {}}),
    ("GET", "/interactive/orders"): lambda b, p: Resp(200, {"type": "success", "result": [{"AppOrderID": 555, "OrderStatus": "Filled", "OrderAverageTradedPrice": "1,01.50", "CumulativeQuantity": 75, "OrderUniqueIdentifier": "PVX"}]}),
    ("GET", "/interactive/portfolio/positions"): lambda b, p: Resp(200, {"type": "success", "result": {"positionList": [{"ExchangeInstrumentId": "45678", "Quantity": "-75"}]}}),
})
check(x.login() == "U1" and x.token == "T", "xts login")
check(x._http.sent[0][2] == {"appKey": "k", "secretKey": "s", "source": "WEBAPI"}, "xts login body")
check(x.funds() == 1234.5, "xts funds")
r = x.resolve(INST); check(r["segment"] == "NSEFO" and r["inst_id"] == "45678", "xts resolve")
check(x.place(r, "BUY", 75, "LIMIT", price=100.5, tag="PVX") == "555", "xts place")
b = x._http.sent[-1][2]
check(b["exchangeInstrumentID"] == 45678 and b["productType"] == "MIS" and b["orderSide"] == "BUY" and b["limitPrice"] == 100.5
      and b["clientID"] == "U1" and b["orderUniqueIdentifier"] == "PVX", f"xts body {b}")
check(x._http.sent[-1][3]["Authorization"] == "T", "xts auth header")
x.place(r, "SELL", 75, "SL", price=76, trigger=80); b = x._http.sent[-1][2]
check(b["orderType"] == "STOPLIMIT" and b["stopPrice"] == 80, "xts SL")
x.modify("555", r, "SELL", 75, "MARKET", tag="PVX"); b = x._http.sent[-1][2]
check(b["appOrderID"] == 555 and b["modifiedOrderType"] == "MARKET", "xts modify")
x.cancel("555", tag="PVX"); check(x._http.sent[-1][4]["appOrderID"] == 555, "xts cancel")
o = x.orders(); check(o["555"].status == FILLED and o["555"].filled_qty == 75 and o["555"].tag == "PVX", "xts orders")
check(x.positions() == {"45678": -75}, "xts positions")
check(XTSAdapter({}, log=lambda *a: None).root == "https://jtrade.jainam.in:5000", "jainam default root")
x2 = XTSAdapter({"XTS_ROOT_URL": "https://h", "XTS_API_KEY": "k", "XTS_API_SECRET": "s"}, log=lambda *a: None)
x2._http = FakeHTTP({("POST", "/interactive/user/session"): lambda b, p: Resp(200, {"type": "success", "result": {"token": "T", "userID": "D1", "isInvestorClient": False}})})
try:
    x2.login(); check(False, "dealer without client id must fail")
except BrokerError as e:
    check("XTS_CLIENT_ID" in str(e), "dealer message")
print("xts ok")

# ── Angel ──
class FakeObj:
    def __init__(s): s.sent = []
    def rmsLimit(s): return {"status": True, "data": {"availablecash": "25000.00"}}
    def placeOrderFullResponse(s, p): s.sent.append(("place", dict(p))); return {"status": True, "data": {"orderid": "A1"}}
    def modifyOrder(s, p): s.sent.append(("modify", dict(p))); return {"status": True}
    def cancelOrder(s, oid, v): s.sent.append(("cancel", oid, v)); return {"status": True}
    def orderBook(s): return {"status": True, "data": [{"orderid": "A1", "status": "complete", "averageprice": 101.0, "filledshares": "75", "ordertag": "PVX", "variety": "NORMAL"},
                                                     {"orderid": "A2", "status": "trigger pending", "variety": "STOPLOSS"}]}
    def position(s): return {"status": True, "data": [{"symboltoken": "45678", "netqty": "75"}]}
fo = FakeObj()
a = AngelAdapter({"_session_getter": lambda: fo, "ANGEL_CLIENT_ID": "C1"}, log=lambda *a: None)
check(a.login() == "C1" and a.funds() == 25000.0, "angel login/funds")
r = a.resolve(INST)
a.place(r, "BUY", 75, "LIMIT", price=100.52, tag="PVX"); p = fo.sent[-1][1]
check(p["variety"] == "NORMAL" and p["quantity"] == "75" and p["price"] == "100.5" and p["ordertag"] == "PVX"
      and p["tradingsymbol"] == INST["tsym"] and p["symboltoken"] == "45678", f"angel place {p}")
a.place(r, "SELL", 75, "SL", price=76, trigger=80); p = fo.sent[-1][1]
check(p["variety"] == "STOPLOSS" and p["ordertype"] == "STOPLOSS_LIMIT" and p["triggerprice"] == "80.0", f"angel SL {p}")
a.modify("A1", r, "SELL", 75, "MARKET"); p = fo.sent[-1][1]
check(p["orderid"] == "A1" and p["ordertype"] == "MARKET" and p["variety"] == "STOPLOSS", "angel modify keeps variety")
o = a.orders(); check(o["A1"].status == FILLED and o["A1"].filled_qty == 75 and o["A2"].status == PENDING, "angel orders")
a.cancel("A2"); check(fo.sent[-1] == ("cancel", "A2", "STOPLOSS"), "angel cancel variety")
check(a.positions() == {"45678": 75}, "angel positions")
a2 = AngelAdapter({"_session_getter": lambda: None}, log=lambda *a: None)
try:
    a2.login(); check(False, "no session")
except BrokerError:
    check(True, "")
# stale session: Angel answers AG8004 until the app logs in again
AUTH_ERR = {"status": False, "message": "Invalid API Key", "errorCode": "AG8004", "data": ""}
class StaleObj(FakeObj):
    def rmsLimit(s): return AUTH_ERR
    def placeOrderFullResponse(s, p): return AUTH_ERR
holder = {"obj": StaleObj(), "logins": 0}
def relogin():
    holder["logins"] += 1; holder["obj"] = FakeObj()
a3 = AngelAdapter({"_session_getter": lambda: holder["obj"], "_session_relogin": relogin, "ANGEL_CLIENT_ID": "C1"}, log=lambda *a: None)
check(a3.login() == "C1" and holder["logins"] == 1, "angel logs in again after AG8004")
holder["obj"] = StaleObj(); a3._relogin_at = 0.0     # 5 minutes later
check(a3.place(r, "BUY", 75, "LIMIT", price=100) == "A1" and holder["logins"] == 2
      and len(holder["obj"].sent) == 1, "angel order retried once on the new session")
a4 = AngelAdapter({"_session_getter": lambda: StaleObj(), "_session_relogin": lambda: None}, log=lambda *a: None)
try:
    a4.login(); check(False, "still refused")
except BrokerError as e:
    check("even after a fresh login" in str(e), f"clear message when the key is refused {e}")
holder["obj"] = StaleObj()
try:
    a3.place(r, "BUY", 75, "LIMIT", price=100); check(False, "no second login within 5 minutes")
except BrokerError:
    check(holder["logins"] == 2, "no login flood while Angel refuses the key")
print("angel ok")
print(f"ALL ADAPTER TESTS PASSED ({n} checks)")
