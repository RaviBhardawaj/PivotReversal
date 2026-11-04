"""Google Sheets: SSL drops are retried on a fresh connection, requests never overlap on the wire,
and a sheet that can't be set up never stops START. Run: python3 tests/test_sheets_resilience.py"""
import sys, os, types, importlib.util, threading, time
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
import requests
import google.auth.transport.requests as gatr

BAD_MAC = requests.exceptions.SSLError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac")
made=[]; fail_next=[0]; live=[0]; peak=[0]
class FakeSession:
    def __init__(self, auth): self.auth=auth; self.closed=False; made.append(self)
    def close(self): self.closed=True
    def request(self, method, url, **kw):
        assert not self.closed, "used a closed session"
        live[0]+=1; peak[0]=max(peak[0], live[0])
        try:
            time.sleep(0.002) if threading.current_thread() is not threading.main_thread() else None
            if fail_next[0] > 0:
                fail_next[0] -= 1; raise BAD_MAC
            return ("ok", method, url, kw.get("timeout"))
        finally:
            live[0]-=1
gatr.AuthorizedSession = FakeSession
real_sleep=time.sleep
app.time.sleep = lambda s: None

class Shared:                         # gspread's one shared session
    def request(self, *a, **k): raise AssertionError("gspread's own session must not be used")
class HTTP: auth="creds"; session=Shared()
class GC: http_client=HTTP()
gc=GC(); app._make_sheets_session_resilient(gc, "creds")
req=gc.http_client.session.request

# A: a bad-record-mac drop is retried on a brand-new connection, and the timeout is always set
fail_next[0]=2
r=req(method="PUT", url="https://sheets/x", timeout=None)
assert r==("ok","PUT","https://sheets/x",app.SHEETS_TIMEOUT), r
assert len(made)==3 and made[0].closed and made[1].closed and not made[2].closed
assert app.SHEETS_HEALTH["err"]==""
print("A bad record mac: broken connection dropped, retried on a fresh one: OK")

# B: after SHEETS_RETRIES more failures the error is raised and the dashboard alert is set
fail_next[0]=app.SHEETS_RETRIES+1
try: req(method="PUT", url="u"); raise SystemExit("should have raised")
except requests.exceptions.SSLError: pass
assert "SSLError" in app.SHEETS_HEALTH["err"]
req(method="PUT", url="u"); assert app.SHEETS_HEALTH["err"]==""
print("B gives up after", app.SHEETS_RETRIES, "retries, alert set, cleared on next good write: OK")

# C: appends are never retried (Google may already have the rows)
made.clear(); fail_next[0]=1
try: req(method="POST", url="https://sheets/x/values/A1:append"); raise SystemExit("should have raised")
except requests.exceptions.SSLError: pass
assert len(made)==0 and fail_next[0]==0   # no fresh connection opened = no retry
print("C append not retried (no duplicate rows): OK")

# D: many threads writing at once never put two requests on the wire together
app.time.sleep=real_sleep; peak[0]=0; errs=[]
def w():
    try:
        for _ in range(5): req(method="PUT", url="u")
    except Exception as e: errs.append(e)
ts=[threading.Thread(target=w) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
assert not errs and peak[0]==1, (errs, peak[0])
print("D eight threads writing at once: one request on the wire at a time: OK")

# E: a connection that sat idle is replaced before it is used
app.time.sleep=lambda s: None
made.clear(); req(method="PUT", url="u"); assert len(made)==0
_t=app.time.time; app.time.time=lambda: _t()+app.SHEETS_IDLE_SECS+5
req(method="PUT", url="u"); app.time.time=_t
assert len(made)==1
print("E idle connection replaced before the next write: OK")

# F: START keeps going when the sheet can't be set up, and setup retries in the background
class Feed: pass
f=Feed(); f.ws="old"; f.running=True; calls=[]
def setup(sym, strikes):
    calls.append(sym)
    if len(calls)==1: raise requests.exceptions.SSLError("bad record mac")
    f.ws="sheet"
f.setup_sheet=setup
done=threading.Event()
real_thread=app.threading.Thread
class T(real_thread):
    def run(self):
        try: super().run()
        finally: done.set()
app.threading.Thread=T
ok=app.FeedEngine.setup_sheet_safe(f, "NIFTY", [], retry_every=0)
assert ok is False
done.wait(5); app.threading.Thread=real_thread
assert calls==["NIFTY","NIFTY"] and f.ws=="sheet", (calls, f.ws)
print("F sheet setup failure does not stop START; it reconnects in the background: OK")

# G: retry stops once the feed is stopped
f=Feed(); f.ws=None; f.running=False; calls=[]
f.setup_sheet=lambda s,k: (calls.append(s), (_ for _ in ()).throw(RuntimeError("down")))
done.clear(); app.threading.Thread=T
app.FeedEngine.setup_sheet_safe(f, "NIFTY", [], retry_every=0)
done.wait(5); app.threading.Thread=real_thread
assert calls==["NIFTY"], calls
print("G background retry ends when STOP is pressed: OK")
print("SHEETS RESILIENCE TESTS PASSED")
