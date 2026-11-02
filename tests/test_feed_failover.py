"""Angel One outage: the app keeps trading on Dhan prices, and flaky Sheets writes are retried.
Run: python3 tests/test_feed_failover.py"""
import sys, os, types, importlib.util, time
import requests
tk=types.ModuleType("tkinter")
class _W:
    def __init__(s,*a,**k): pass
    def __getattr__(s,n): return lambda *a,**k: None
for n in ["Tk","Label","Frame","Button","StringVar","BooleanVar","IntVar","Radiobutton","Checkbutton","Toplevel","Entry","Canvas","Scrollbar","Text","Menu","OptionMenu","Spinbox","PhotoImage","Listbox"]:
    setattr(tk,n,_W)
ttk=types.ModuleType("tkinter.ttk"); ttk.Combobox=_W; ttk.Treeview=_W; ttk.Style=_W; ttk.Scrollbar=_W; ttk.Notebook=_W; ttk.Progressbar=_W
mb=types.ModuleType("tkinter.messagebox"); tk.ttk=ttk; tk.messagebox=mb
sys.modules.update({"tkinter":tk,"tkinter.ttk":ttk,"tkinter.messagebox":mb})
_src=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","Options-angel_live-ok.py")
_spec=importlib.util.spec_from_file_location("app",_src); app=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(app)
import tempfile
_T=tempfile.mkdtemp()
# Fed ticks reach the strategy engines: keep their CSV/JSON logs out of the real app folder
_adv=app.advanced_engine.tracker
for _k,_v in list(vars(_adv).items()):
    if _k.endswith("_file") and isinstance(_v,str):
        setattr(_adv,_k,os.path.join(_T,os.path.basename(_v)))
app.trigger_immediate_alert=lambda *a,**k: None
app.subprocess.run=lambda *a,**k: None

# 1. A Dhan quote becomes an Angel-shaped tick (paise, trade time, depth)
q={"last_price":256,"oi":1200,"volume":5000,"last_trade_time":"02/11/2026 10:30:05",
   "ohlc":{"open":240,"close":230.5,"high":260,"low":235},
   "depth":{"buy":[{"price":255.9,"quantity":65}],"sell":[{"price":256.2,"quantity":130},{"price":0,"quantity":0}]}}
t=app.dhan_quote_to_tick("53926",q)
assert t["token"]=="53926" and t["last_traded_price"]==25600 and t["closed_price"]==23050
assert t["best_5_buy_data"][0]["price"]==25590 and len(t["best_5_sell_data"])==1
from datetime import datetime
assert datetime.fromtimestamp(t["last_traded_timestamp"])==datetime(2026,11,2,10,30,5)
print("Dhan quote -> Angel tick: OK")

# 2. Request maps option tokens straight across and the index to Dhan's ID; unknown indices are skipped
f=app.FeedEngine()
f.forward_tracker.filepath=os.path.join(_T,"ft.csv"); f.forward_tracker.state_file=os.path.join(_T,"ft_open.json")
f.token_list=[{"exchangeType":2,"tokens":["53926","53927"]},{"exchangeType":1,"tokens":["99926000"]},
              {"exchangeType":3,"tokens":["500112"]}]
req,back=f._dhan_backup_request()
assert req=={"NSE_FNO":[53926,53927],"IDX_I":[13]}, req
assert back[("IDX_I","13")]=="99926000"
print("Dhan backup request mapping: OK")

# 3. Angel silent in market hours -> Dhan prices flow into live_data; Angel back -> source switches back
class AD:
    calls=0
    def _req(self, m, path, body):
        AD.calls+=1
        return {"data":{"NSE_FNO":{"53926":q,"53927":dict(q,last_price=101)},"IDX_I":{"13":dict(q,last_price=23440.8)}}}
app._dhan_data_login=lambda: AD()
os.environ["FORCE_MARKET_OPEN"]="1"
app.Session.last_error="Angel One server unreachable (internet/DNS problem)"
f.spot_token={"NIFTY":"99926000"}
f.DHAN_TAKEOVER_S=0.1
f.running=True
f._last_angel_tick=time.time()-60
import threading
th=threading.Thread(target=f._dhan_backup_loop,daemon=True); th.start()
time.sleep(2.0)
assert f.data_source=="DHAN" and "ANGEL ONE DOWN" in f.feed_alert, (f.data_source, f.feed_alert)
assert f.live_data["53926"]["ltp"]==256 and f.live_data["53927"]["ltp"]==101
assert f.spot_price["NIFTY"]==23440.8
print("Angel down -> Dhan prices drive the feed:", f.feed_alert)
f.DHAN_TAKEOVER_S=20; f._last_angel_tick=time.time()
time.sleep(2.0)
assert f.data_source=="ANGEL" and f.feed_alert==""
print("Angel back -> Angel takes over again: OK")

# 4. Both down -> loud NONE alert
f.DHAN_TAKEOVER_S=0.1; f._last_angel_tick=time.time()-60
def _boom(): raise RuntimeError("Dhan login failed: DH-901")
app._dhan_data_login=_boom
time.sleep(2.0)
assert f.data_source=="NONE" and "NO LIVE PRICES" in f.feed_alert
f.running=False
print("Angel and Dhan both down -> NO LIVE PRICES alert: OK")

# 5. Sheets: an SSL 'bad record mac' is retried; appends are not (no duplicate rows)
app.time.sleep=lambda s: None
class Sess:
    def __init__(s, fails): s.fails=fails; s.n=0
    def request(s, method, url, **kw):
        s.n+=1
        if s.n<=s.fails: raise requests.exceptions.SSLError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac")
        return "ok"
class GC:
    def __init__(s, sess): s.http_client=types.SimpleNamespace(session=sess)
s=Sess(2); app._retry_sheet_requests(GC(s))
assert s.request(method="PUT", url="https://sheets.googleapis.com/v4/spreadsheets/x/values/A1")=="ok" and s.n==3
s=Sess(1); app._retry_sheet_requests(GC(s))
try:
    s.request(method="POST", url="https://sheets.googleapis.com/v4/spreadsheets/x/values/A1:append"); assert False
except requests.exceptions.SSLError:
    pass
assert s.n==1 and "SSLError" in app.SHEETS_HEALTH["err"]
print("Sheets SSL error retried; append not retried: OK")

# 6. Startup login failure is recorded, not fatal
class SC:
    def __init__(s, api_key): pass
    def generateSession(s,*a):
        raise Exception("HTTPSConnectionPool(host='apiconnect.angelone.in'): Max retries exceeded (Caused by NameResolutionError(\"Failed to resolve\"))")
app.SmartConnect=SC
try:
    app.Session.login(); assert False
except Exception:
    pass
assert app.Session.last_error=="Angel One server unreachable (internet/DNS problem)"
print("Angel login failure recorded for the dashboard: OK")
