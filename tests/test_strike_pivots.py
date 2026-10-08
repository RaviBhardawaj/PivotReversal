"""Option strike pivot levels: built only from each contract's official daily candle, exactly as
TradingView's Pivot Points Standard (Fibonacci, Daily) draws them. Uses a simulated Angel One API.
Run: python3 tests/test_strike_pivots.py"""
import sys, os, json, tempfile, importlib.util
# tkinter is stubbed so the app module loads without a display
import types
# stub tkinter
tk=types.ModuleType("tkinter"); 
class _W: 
    def __init__(s,*a,**k): pass
    def __getattr__(s,n): return lambda *a,**k: None
for n in ["Tk","Label","Frame","Button","StringVar","BooleanVar","IntVar","Radiobutton","Checkbutton","Toplevel","Entry","Canvas","Scrollbar","Text","Menu","OptionMenu","Spinbox","PhotoImage","Listbox"]:
    setattr(tk,n,_W)
ttk=types.ModuleType("tkinter.ttk"); ttk.Combobox=_W; ttk.Treeview=_W; ttk.Style=_W; ttk.Scrollbar=_W; ttk.Notebook=_W; ttk.Progressbar=_W
mb=types.ModuleType("tkinter.messagebox")
tk.ttk=ttk; tk.messagebox=mb
sys.modules.update({"tkinter":tk,"tkinter.ttk":ttk,"tkinter.messagebox":mb})
import pandas as pd
from datetime import datetime
W=tempfile.mkdtemp()
_src=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","Options-angel_live-ok.py")
_spec=importlib.util.spec_from_file_location("app",_src); app=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(app)
app.BASE_DIR=W
app.STRIKE_PIVOT_CACHE=os.path.join(W,"strike_pivot_cache.json")
app.STRIKE_PIVOT_NEXT_CACHE=os.path.join(W,"strike_pivot_cache_next.json")
app._DATA_SETTINGS_FILE=os.path.join(W,"data_settings.json")
app.time.sleep=lambda s: None
class FakeDT(datetime):
    v=datetime(2026,10,8,8,30)
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT

rows=[]; tok=5000; meta={}
for exp in ("13OCT2026","20OCT2026"):
    for k in range(22000,23550,50):
        for ot in ("CE","PE"):
            tok+=1; rows.append({"token":str(tok),"symbol":f"NIFTY{exp[:5]}26{k}{ot}","name":"NIFTY","expiry":exp,"strike":str(k*100),"lotsize":"75","instrumenttype":"OPTIDX","exch_seg":"NFO"})
            meta[str(tok)]=(exp,k,ot)
app.Session.scrip_df=pd.DataFrame(rows)
app.Session.auth_token=app.Session.feed_token="x"
mtf={"15-Min":app.calc_fib_pivots(22821.8,22597.1,22755.75),"1-Hour":app.calc_fib_pivots(23009.3,22417.65,22976.1),"Daily":{"C":22750}}
app.get_tv_multi_timeframe_pivots=lambda sym,force_refresh=False: mtf

DAYS=["2026-10-01","2026-10-05","2026-10-06","2026-10-07","2026-10-08","2026-10-09","2026-10-12"]
def candle(t,d,bump=0):
    exp,k,ot=meta[t]; base=(abs(22750-k)/7+40)*(1.3 if exp=="20OCT2026" else 1)+DAYS.index(d)*3.17+bump
    return [d+"T00:00:00+05:30", round(base,2), round(base*1.21,2), round(base*0.83,2), round(base*1.04,2), 1000]
state={"calls":0,"auth_fail_once":True,"empty":set(),"logins":0,"bump":0}
class Obj:
    def getCandleData(self,p):
        state["calls"]+=1
        if state["auth_fail_once"]:
            state["auth_fail_once"]=False
            return {"status":False,"message":"Invalid API Key","errorcode":"AG8004","data":""}
        t=p["symboltoken"]
        if t in state["empty"]: return {"status":True,"message":"SUCCESS","data":[]}
        today=FakeDT.v.strftime("%Y-%m-%d")
        return {"status":True,"data":[candle(t,d,state["bump"] if d==today else 0) for d in DAYS if d<=today]}
def login():
    state["logins"]+=1; app.Session.obj=Obj()
app.Session.login=classmethod(lambda cls: login())
app.Session.obj=Obj()

def tv(h,l,c):   # TradingView Pivot Points Standard, Fibonacci
    p=(h+l+c)/3; r=h-l
    return {"P":p,"R1":p+.382*r,"R2":p+.618*r,"R3":p+r,"S1":p-.382*r,"S2":p-.618*r,"S3":p-r}
def check(sp,t,day):
    c=candle(t,day); want=tv(c[2],c[3],c[4])
    for k,v in want.items():
        assert abs(sp[k]-max(round(v,2),0.05 if k[0]=="S" else 0))<0.006,(t,k,sp[k],v)
    assert sp["candle_date"]==day

f=app.FeedEngine()
f.spot_price["NIFTY"]=22750
f._start_pivot_watchdog=lambda: None
# A) pre-market 8 Oct: levels of 8 Oct session = 7 Oct candle; first call hits AG8004 -> login again
m=f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
focus=sorted({(s["opt_type"],s["strike"]) for s in m.values()})
print("A focus strikes:",focus,"logins:",state["logins"])
assert state["logins"]==1 and len(m)>=4
for t,s in m.items(): check(s,t,"2026-10-07")
# B) in session 11:00: today's partial candle present + a forced refresh -> unchanged
FakeDT.v=datetime(2026,10,8,11,0); state["bump"]=50
before={t:dict(f.strike_pivots[t]) for t in m}
f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert all(f.strike_pivots[t]==before[t] for t in m); print("B locked in session: OK")
# C) next expiry kept apart; short key stays nearest expiry
m2=f.fetch_and_map_all_strikes("NIFTY","20OCT2026",force_fetch=True)
for t,s in m2.items(): check(s,t,"2026-10-07")
ot,k=focus[0]
a=f.strike_pivots[(ot,k,"13OCT2026")]; b=f.strike_pivots[(ot,k,"20OCT2026")]
assert a["P"]!=b["P"] and f.strike_pivots[(ot,k)] is a; print("C expiries separate:",a["P"],b["P"])
cj=json.load(open(app.STRIKE_PIVOT_CACHE)); assert len(cj["strikes"])==len(m)+len(m2) and cj["basis_key"]=="2026-10-08:session"
# D) after close: next-session file from today's full candle; live levels untouched
FakeDT.v=datetime(2026,10,8,15,45); state["bump"]=0
n=f.fetch_and_map_all_strikes("NIFTY",None,force_fetch=True,mode="next")
for t,s in n.items(): check(s,t,"2026-10-08")
assert all(f.strike_pivots[t]==before[t] for t in m)
nj=json.load(open(app.STRIKE_PIVOT_NEXT_CACHE)); assert nj["date"]=="2026-10-09"
FakeDT.v=datetime(2026,10,8,15,34); assert app.pivot_session_date().isoformat()=="2026-10-08"
FakeDT.v=datetime(2026,10,8,15,45); assert app.pivot_session_date().isoformat()=="2026-10-09"
r=f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
for t,s in r.items(): check(s,t,"2026-10-08")
assert json.load(open(app.STRIKE_PIVOT_CACHE))["basis_key"]=="2026-10-09:session"
print("D after close: dashboard moves to the 9 Oct levels from today's 8 Oct candle; next file for",nj["date"])
# E) Saturday: Monday 12 Oct's levels, built from Fri 9 Oct
FakeDT.v=datetime(2026,10,10,10,0)
assert app.pivot_session_date().isoformat()=="2026-10-12"
f.strike_pivots.clear()
r=f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
for t,s in r.items(): check(s,t,"2026-10-09")
print("E weekend: OK")
# F) a strike without data gets NO levels (nothing made up) and is filled on retry
FakeDT.v=datetime(2026,10,13,8,0); f.strike_pivots.clear()
gone=list(m)[0]; state["empty"]={gone}
r=f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert gone not in r and gone not in f.strike_pivots and gone in f._pivot_jobs[("NIFTY","13OCT2026")]
state["empty"]=set()
r=f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=False)
check(f.strike_pivots[gone],gone,"2026-10-12"); assert not f._pivot_jobs[("NIFTY","13OCT2026")]
print("F missing strike retried: OK")
# G) load_or_fetch on restart uses cache, with exact expiry keys
g=app.FeedEngine(); g._start_pivot_watchdog=lambda: None
calls=state["calls"]
g.load_or_fetch_strike_pivots("NIFTY",list(m),{t:{"expiry":meta[t][0],"strike":meta[t][1],"opt_type":meta[t][2]} for t in m})
assert state["calls"]==calls and all(t in g.strike_pivots for t in m); print("G restart from cache, no refetch: OK")
# H) flat candle = all levels P, as TradingView
p=app.calc_fib_pivots(5,5,5); assert p["R3"]==p["S3"]==p["P"]==5
# I) Dhan is the first source; Angel's candle is used to cross-check it
class FakeDhan:
    def __init__(s, bump=0): s.calls = []; s.bump = bump
    def daily_candles(s, sid, seg, instrument, now=None):
        s.calls.append((sid, seg, instrument))
        today = FakeDT.v.strftime("%Y-%m-%d")
        return [tuple([c[0][:10]] + [x + s.bump for x in c[1:5]]) for c in (candle(sid, d) for d in DAYS if d <= today)]
fd = FakeDhan(); app._DHAN_DATA["ad"] = fd; app._SRC_DOWN.update(Dhan=0.0, Angel=0.0)
assert app.get_level_source() == "DHAN"
FakeDT.v = datetime(2026,10,9,8,0); f.strike_pivots.clear(); calls = state["calls"]
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert len(fd.calls) == len(r) and fd.calls[0][1:] == ("NSE_FNO", "OPTIDX") and state["calls"] - calls == len(r)
for t,s in r.items(): check(s,t,"2026-10-08"); assert s["source"] == "Dhan" and s["cross_check"] == "MATCH"
assert f.level_check_summary == {"MATCH": len(r)}
# the two brokers disagree -> flagged, levels still from the selected source
app._DHAN_DATA["ad"] = FakeDhan(bump=1.0); f.strike_pivots.clear()
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert all(s["cross_check"] == "MISMATCH" and s["source"] == "Dhan" for s in r.values())
print("I Dhan first + Angel cross-check (match and mismatch): OK")
# J) Angel selected: Angel first, Dhan checks it
app._DHAN_DATA["ad"] = FakeDhan(); app.set_level_source("ANGEL"); assert app.get_level_source() == "ANGEL"
f.strike_pivots.clear()
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
for t,s in r.items(): check(s,t,"2026-10-08"); assert s["source"] == "Angel" and s["cross_check"] == "MATCH"
app.set_level_source("DHAN")
print("J Angel selected: OK")
# K) Dhan fails -> Angel fills in; Dhan is not asked again for 2 minutes
class BadDhan:
    n = 0
    def daily_candles(s, *a, **k): BadDhan.n += 1; raise RuntimeError("DH-906 no data plan")
app._DHAN_DATA["ad"] = BadDhan(); app._SRC_DOWN.update(Dhan=0.0, Angel=0.0); f.strike_pivots.clear()
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert r and all(s["source"] == "Angel" and s["cross_check"] == "NOT CHECKED" for s in r.values())
print("K Dhan down, Angel backup: OK (Dhan asked", BadDhan.n, "times)")
# L) the source has not published today's daily candle yet -> rebuilt from 1-minute candles,
#    never an older day's candle; later the official candle replaces it
class LateDhan(FakeDhan):
    def __init__(s, publish=False, holiday=None): super().__init__(); s.publish = publish; s.holiday = holiday or set(); s.min1 = []
    def daily_candles(s, sid, seg, instrument, now=None):
        rows = super().daily_candles(sid, seg, instrument, now)
        return rows if s.publish else [r for r in rows if r[0] != FakeDT.v.strftime("%Y-%m-%d")]
    def intraday_day_ohlc(s, sid, day, seg="NSE_FNO", instrument="OPTIDX"):
        s.min1.append((sid, day))
        if day in s.holiday: return None
        c = candle(sid, day); return (c[1], c[2], c[3], c[4], "15:29")
app._SRC_DOWN.update(Dhan=0.0, Angel=0.0); state["empty"] = set(t for t in meta)   # Angel has nothing
ld = LateDhan(); app._DHAN_DATA["ad"] = ld
FakeDT.v = datetime(2026,10,9,15,40); f.strike_pivots.clear()
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert r and all(s["source"] == "Dhan 1-min" and s["candle_date"] == "2026-10-09" for s in r.values())
for t,s in r.items(): check(s,t,"2026-10-09")
assert all(d == "2026-10-09" for _, d in ld.min1)
ld.publish = True
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert all(s["source"] == "Dhan" for s in r.values())
print("L daily candle late: rebuilt from 1-minute data, then official candle: OK")
# M) a weekday holiday (no trades at all) is skipped to the day before
app._DHAN_DATA["ad"] = LateDhan(holiday={"2026-10-09"}); app._SRC_DOWN.update(Dhan=0.0, Angel=0.0)
FakeDT.v = datetime(2026,10,9,16,0); f.strike_pivots.clear()
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
for t,s in r.items(): check(s,t,"2026-10-08")
print("M holiday: previous trading day's candle: OK")
# N) 1-minute data unreachable -> no levels (retried), never yesterday's candle
class DownMin(LateDhan):
    def intraday_day_ohlc(s, *a, **k): raise RuntimeError("network")
app._DHAN_DATA["ad"] = DownMin(); app._SRC_DOWN.update(Dhan=0.0, Angel=0.0)
f.strike_pivots.clear()
r = f.fetch_and_map_all_strikes("NIFTY","13OCT2026",force_fetch=True)
assert not any(t in f.strike_pivots for t in m) and f._pivot_jobs[("NIFTY","13OCT2026")]
print("N no data: strikes wait, nothing stale used: OK")
state["empty"] = set()
print("STRIKE PIVOT TESTS PASSED")
