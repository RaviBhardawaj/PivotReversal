"""Next-session levels after the close: built from TODAY's candle for the current AND the next
expiry, and eod_planner.py takes its plan from them.
Run: python3 tests/test_next_day_levels.py"""
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

state["auth_fail_once"]=False
app.dhan_daily_candles=lambda exch,tok,inst="OPTIDX",now=None: [tuple([r[0][:10]]+r[1:5]) for r in [candle(tok,d) for d in DAYS if d<FakeDT.v.strftime("%Y-%m-%d")]]
def intr(exch,tok,day,inst="OPTIDX"):
    c=candle(tok,day.isoformat()); return (day.isoformat(),c[1],c[2],c[3],c[4])
app.dhan_intraday_day=intr
launched=[]
import subprocess
subprocess.Popen=lambda *a,**k: launched.append(a[0])
FakeDT.v=datetime(2026,10,12,15,45)
f=app.FeedEngine(); f.spot_price["NIFTY"]=22750; f._start_pivot_watchdog=lambda: None

# A) the expiries still open on the next session, nearest first
assert f.upcoming_expiries("NIFTY",2)==["13OCT2026","20OCT2026"], f.upcoming_expiries("NIFTY",2)
from datetime import date
assert f.upcoming_expiries("NIFTY",2,from_day=date(2026,10,14))==["20OCT2026"]
print("A expiries for the next session: current and next: OK")

# B) 15:45: next-day levels for BOTH expiries, from today's (12 Oct) candle, then the planner
f.build_next_day_levels("NIFTY")
d=json.load(open(app.STRIKE_PIVOT_NEXT_CACHE))
import collections
by=collections.Counter(v["expiry"] for v in d["strikes"].values())
assert d["basis_key"]=="2026-10-13:next" and set(by)=={"13OCT2026","20OCT2026"} and min(by.values())>0, (d["basis_key"],by)
for t,s in d["strikes"].items(): check(s,t,"2026-10-12")
assert len(launched)==1 and launched[0][-1].endswith("eod_planner.py"), launched
print("B next-day levels for 13OCT and 20OCT from the 12 Oct candle, planner started once:", dict(by), ": OK")
# C) the second 15:45 timer the same day does nothing
f.build_next_day_levels("NIFTY"); assert len(launched)==1
print("C runs once a day: OK")

# D) eod_planner: plan built from TODAY's candle after the close, strike levels from the app's file
_ps=importlib.util.spec_from_file_location("planner",os.path.join(os.path.dirname(_src),"eod_planner.py"))
pl=importlib.util.module_from_spec(_ps); _ps.loader.exec_module(pl)
pl.PLAN_DIR=W
class PDT(datetime):
    @classmethod
    def now(cls,tz=None): return FakeDT.v
class PD(date):
    @classmethod
    def today(cls): return FakeDT.v.date()
pl.datetime=PDT; pl.date=PD
pl.fetch_candles=lambda symbol="NIFTY",days=10: [
    {"date":date(2026,10,9),"open":22599.05,"high":22580.75,"low":22294.75,"close":22520.45},
    {"date":date(2026,10,12),"open":22400.0,"high":22651.1,"low":22304.45,"close":22363.35}]
plan=pl.generate_plan("NIFTY")
assert plan["for_date"]=="2026-10-13" and plan["prev_ohlc"]["close"]==22363.35, plan["prev_ohlc"]
assert plan["pivots"]==pl.calc_fib_pivots(22651.1,22304.45,22363.35)
exps=collections.Counter(v["expiry"] for v in plan["strike_levels"].values())
assert set(exps)=={"13OCT2026","20OCT2026"}, exps
print("D plan for 13 Oct from the 12 Oct candle, %d strike levels from the app (both expiries): OK"%len(plan["strike_levels"]))
# E) before the close the plan still uses the last completed day
FakeDT.v=datetime(2026,10,12,14,0)
plan=pl.generate_plan("NIFTY")
assert plan["prev_ohlc"]["close"]==22520.45, plan["prev_ohlc"]
print("E before 15:30 the last completed day (9 Oct) is used: OK")
print("NEXT-DAY LEVEL TESTS PASSED")
