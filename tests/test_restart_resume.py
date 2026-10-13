"""Open trades survive an app restart (internet drop, crash, Mac sleep) and keep being watched.
Run: python3 tests/test_restart_resume.py"""
import sys, os, types, importlib.util, tempfile, csv, json
from datetime import datetime
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
class FakeDT(datetime):
    v=datetime(2026,10,9,11,25,0)
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT
app.TRAIL_SL=True   # these cases cover the trailing-SL path (off in the app since 13 Oct)
os.environ.pop("FORCE_MARKET_OPEN",None)

D=tempfile.mkdtemp(); F=os.path.join(D,"forward_test_trades.csv")
def tracker():
    t=app.ForwardTestTracker(F); t.paper_only=True; return t
def rows(): return {r["Trade_ID"]:r for r in csv.DictReader(open(F))}

# morning: PE 22300 BUY, SL hit; then the re-entry; plus CE 22350 SELL
a=tracker()
t1=a.log_entry("NIFTY","PE",22300,"BUY",103.0,96.25,136.08,193.0,250.0,300.0,350.0,timeframe="15M Intraday",expiry="19OCT2026")
assert t1
FakeDT.v=datetime(2026,10,9,11,26); assert a.update_tick("PE",22300,95.0,expiry="19OCT2026",symbol="NIFTY")["Exit_Reason"]=="SL Hit"
FakeDT.v=datetime(2026,10,9,11,31)
t2=a.log_entry("NIFTY","PE",22300,"BUY",104.0,94.0,136.08,193.0,250.0,300.0,350.0,timeframe="15M Intraday",expiry="19OCT2026")
t3=a.log_entry("NIFTY","CE",22350,"SELL",332.35,348.55,302.16,233.25,180.0,120.0,80.0,timeframe="15M / 1H",expiry="19OCT2026")
assert t2 and t3
FakeDT.v=datetime(2026,10,9,11,33); a.update_tick("PE",22300,137.0,expiry="19OCT2026",symbol="NIFTY")   # T1: SL -> entry
assert t2["Target_Hit"]==1 and t2["SL"]==104.0
print("A morning trades taken; re-entry at T1 with SL trailed to entry: OK")

# internet drops, app restarts at 11:35
FakeDT.v=datetime(2026,10,9,11,35,31)
b=tracker()
act={t["Trade_ID"]:t for t in b.get_active_trades()}
assert set(act)=={t2["Trade_ID"],t3["Trade_ID"]}, act.keys()
assert act[t2["Trade_ID"]]["SL"]==104.0 and act[t2["Trade_ID"]]["Target_Hit"]==1
r=rows(); assert r[t2["Trade_ID"]]["Status"]=="ACTIVE" and r[t3["Trade_ID"]]["Status"]=="ACTIVE", r
assert r[t1["Trade_ID"]]["Status"]=="CLOSED"
print("B after restart: both open trades restored (trailed SL kept), sheet rows still ACTIVE: OK")

# the strike's state machine picks the trade up instead of starting from WAITING
st=app.StrategyStrikeState("PE",22300,expiry="19OCT2026",mode="BUY"); b.seed_state(st,"NIFTY")
assert st.state=="CONFIRMED_BUY" and st.trade_status=="Target 1 Hit" and st.entry_price==104.0 and st.sl==104.0 and st.sl_hit_count==1, (st.state,st.trade_status)
st2=app.StrategyStrikeState("PE",22300,expiry="19OCT2026",mode="SELL"); b.seed_state(st2,"NIFTY"); assert st2.state=="WAITING"
assert act[t2["Trade_ID"]]["Initial_SL"]==94.0 and st.entry_sl==94.0 and st.sl==104.0, (act[t2["Trade_ID"]].get("Initial_SL"), st.entry_sl)
print("C strategy state resumes CONFIRMED_BUY / Target 1 Hit, re-entry count kept: OK")

# no duplicate entry on the restored strike; the SL hit closes it after the restart
FakeDT.v=datetime(2026,10,9,11,40)
assert b.log_entry("NIFTY","PE",22300,"BUY",105.0,99.0,136.08,193.0,timeframe="15M Intraday",expiry="19OCT2026") is None
c=b.update_tick("PE",22300,103.5,expiry="19OCT2026",symbol="NIFTY")
assert c and c["Exit_Reason"]=="Trailing SL Hit", c
assert rows()[t2["Trade_ID"]]["Status"]=="CLOSED"
print("D no duplicate entry; trailing SL hit after the restart closes it in the sheet: OK")

# restart again: the SELL is still there; the guard counts survive too
FakeDT.v=datetime(2026,10,9,11,45)
c2=tracker(); assert [t["Trade_ID"] for t in c2.get_active_trades()]==[t3["Trade_ID"]]
assert len(c2.get_closed_today())==2
print("E second restart: remaining SELL restored, today's closed trades kept: OK")

# next morning: yesterday's leftovers are not restored
FakeDT.v=datetime(2026,10,12,9,20)
d=tracker(); r3=rows()[t3["Trade_ID"]]
assert d.get_active_trades()==[] and r3["Status"]=="CLOSED" and r3["Exit_Time"]=="15:00:00" and "last LTP" in r3["Exit_Reason"], r3
assert float(r3["Exit_Price"])==332.35 and float(r3["PnL_Pts"])==0.0
print("F app off at 3 PM: the open SELL is closed at its last LTP, so the day adds up: OK")
# G: today's closed trades still show on their strikes after a restart (AJ 12 Oct)
FakeDT.v=datetime(2026,10,9,14,0)
D2=tempfile.mkdtemp(); F2=os.path.join(D2,"forward_test_trades.csv")
g=app.ForwardTestTracker(F2); g.paper_only=True
tg=g.log_entry("NIFTY","CE",22400,"BUY",84.7,76.3,120.22,184.42,248.61,280.0,300.0,timeframe="15M Intraday",expiry="13OCT2026")
FakeDT.v=datetime(2026,10,9,14,5); assert g.update_tick("CE",22400,75.85,expiry="13OCT2026",symbol="NIFTY")["Exit_Reason"]=="SL Hit"
FakeDT.v=datetime(2026,10,9,14,10)
h=app.ForwardTestTracker(F2); h.paper_only=True
sg=app.StrategyStrikeState("CE",22400,expiry="13OCT2026",mode="BUY"); h.seed_state(sg,"NIFTY")
assert sg.trade_status=="SL Hit Closed" and sg.entry_price==84.7 and sg.entry_sl==76.3 and sg.sl_hit_count==1 and sg.state=="WAITING", (sg.trade_status, sg.entry_price, sg.entry_sl)
assert app.display_sl(sg.trade_status, sg.state, sg.sl, True, 84.7, entry_sl=sg.entry_sl)=="76.30"
sg2=app.StrategyStrikeState("CE",22400,expiry="13OCT2026",mode="SELL"); h.seed_state(sg2,"NIFTY"); assert sg2.trade_status=="Waiting"
assert h.log_entry("NIFTY","CE",22400,"BUY",85.0,77.0,120.22,184.42,timeframe="15M Intraday",expiry="13OCT2026") is not None   # one re-entry still allowed
print("G restart: today's closed CE 22400 BUY shows again (entry 84.70, SL 76.30, SL Hit Closed): OK")
print("RESTART RESUME TESTS PASSED")
