"""S1 continuation BUY (AJ, 17 Nov): price pulls back only to S1, takes the S1 liquidity,
reclaims and continues up without touching S2 -> the same sweep / close-above candle / retest /
break structure is traded at S1. Run: python3 tests/test_s1_continuation.py"""
import sys, os, types, importlib.util
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
    v=datetime(2026,10,9,9,30)
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT
os.environ.pop("FORCE_MARKET_OPEN",None)
at=lambda h,m,s=0: datetime(2026,10,9,h,m,s)

def run(piv, path, mode="BUY", ot="PE"):
    st=app.StrategyStrikeState(ot,23600,expiry="17NOV2026",mode=mode); hi=0; lo=1e9
    for ts,p in path:
        hi=max(hi,p); lo=min(lo,p); FakeDT.v=ts
        st.update_tick(p,piv,ts,opt_high=hi,opt_low=lo)
    return st

PIV=app.calc_fib_pivots(226.05,122.45,132.6)
S2,S1,P,R1,R2,R3,S3=(PIV[k] for k in ("S2","S1","P","R1","R2","R3","S3"))
print("levels S3 %.2f S2 %.2f S1 %.2f P %.2f R1 %.2f R2 %.2f R3 %.2f"%(S3,S2,S1,P,R1,R2,R3))
assert S1 > S2*1.03

# Opens between S1 and P, pulls back under S1 (S2 untouched), 09:30 candle closes above S1,
# retest into S1, break of the candle high.
OPEN=[(at(9,16),S1*1.08),(at(9,20),S1*1.06),(at(9,25),S1*1.03)]
SWEEP=[(at(9,30,10),S1*0.985),(at(9,31),S1*0.97),(at(9,33),S1*1.004),(at(9,34),S1*1.012)]   # LL = S1*0.97
RETEST=[(at(9,35),S1*1.006),(at(9,36),S1*1.008)]
BREAK=[(at(9,37),S1*1.016)]
CH=round(S1*1.012,2)

# 1. S1 continuation: enters at S1 with SL = LL and the S1 target ladder
st=run(PIV,OPEN+SWEEP+RETEST)
assert st.trade_status=="Waiting" and st.trigger_level=="S1" and st.conf_high==CH, (st.state,st.trigger_level,st.conf_high,st.signal)
st=run(PIV,OPEN+SWEEP+RETEST+BREAK)
assert st.state=="CONFIRMED_BUY" and st.trade_status=="Active" and st.trigger_level=="S1", (st.state,st.signal)
assert st.entry_price==CH and st.sl==round(S1*0.97,2), (st.entry_price,st.sl)
assert st.targets[:4]==[P,R1,R2,R3], st.targets
assert "S1 continuation" in st.signal, st.signal
print("1 S1 pull-back, S2 untouched: BUY @ %.2f SL(LL) %.2f targets %s: OK"%(st.entry_price,st.sl,st.targets))

# 1b. the main loop's entry check accepts it as an S1 reclaim
e=app.check_option_entry(S1*1.016,PIV,"PE",ll=st.ll,state=st.state,level=st.trigger_level)
assert e["entry"] and e["zone"]=="S1 Reclaim", e
print("1b entry check: S1 Reclaim: OK")

# 2. S2 touched before the break -> S1 setup dropped, it becomes an S2 setup (no S1 entry)
st=run(PIV,OPEN+SWEEP+[(at(9,35),S2*1.005)])
assert st.trade_status=="Waiting" and st.trigger_level=="S2" and st.state.startswith("SWEEP_S2"), (st.state,st.trigger_level)
st=run(PIV,OPEN+SWEEP+[(at(9,35),S2*1.005),(at(9,36),S1*1.008),(at(9,37),S1*1.02)])
assert st.trade_status=="Waiting", (st.state,st.signal)
print("2 S2 touched mid-setup: S1 dropped, S2 setup takes over: OK")

# 3. S2 already touched earlier today -> a later S1 pull-back is not traded at S1
EARLY=[(at(9,16),S1*1.05),(at(9,18),S2*1.004),(at(9,20),S1*1.06),(at(9,25),S1*1.05)]
st=run(PIV,EARLY+[(t,p) for t,p in SWEEP+RETEST+BREAK])
assert st.trigger_level!="S1" and not (st.trade_status=="Active" and st.trigger_level=="S1"), (st.state,st.trigger_level,st.signal)
print("3 S2 already touched today: no S1 continuation entry: OK")

# 4. price never traded above S1 (rising from S1-S2 into S1): not a sweep of S1 liquidity
BELOW=[(at(9,16),S1*0.95),(at(9,20),S1*0.96),(at(9,25),S1*0.97)]
st=run(PIV,BELOW+[(at(9,30,10),S1*0.985),(at(9,31),S1*0.975),(at(9,33),S1*1.004),(at(9,34),S1*1.009),(at(9,35),S1*1.006),(at(9,36),S1*1.008),(at(9,37),S1*1.016)])
assert st.trade_status=="Waiting" and st.trigger_level!="S1", (st.state,st.trigger_level,st.signal)
print("4 never above S1 first: no S1 continuation entry: OK")

# 5. switch off -> old behaviour (waits for S2)
app.S1_CONTINUATION_BUY=False
try:
    st=run(PIV,OPEN+SWEEP+RETEST+BREAK)
    assert st.trade_status=="Waiting" and st.state=="WAITING", (st.state,st.signal)
finally:
    app.S1_CONTINUATION_BUY=True
print("5 S1_CONTINUATION_BUY=False: no S1 entry (waits for S2): OK")

# 6. S2 setup itself unchanged: a sweep straight down to S2 is still the S2 setup
st=run(PIV,[(at(9,25),S2*1.05),(at(9,30,10),S2*0.97),(at(9,31),S2*0.955),(at(9,33),S2*1.003),(at(9,34),S2*1.008),(at(9,35),S2*1.004),(at(9,36),S2*1.012)])
assert st.state=="CONFIRMED_BUY" and st.trigger_level=="S2" and st.targets[0]==S1, (st.state,st.trigger_level,st.targets)
print("6 S2 setup unchanged: OK")

# 7. no room: candle high already above P -> no trade
st=run(PIV,[(at(9,16),P*1.05),(at(9,25),S1*1.03),(at(9,30,10),S1*0.99),(at(9,31),S1*0.98),(at(9,32),P*1.01),(at(9,34),S1*1.01),
            (at(9,35),S1*1.005),(at(9,36),P*1.02)])
assert st.trade_status=="Waiting", (st.state,st.signal)
print("7 confirmation high already at P: no S1 continuation trade: OK")

# 8. one-way down after the S1 trade starts is the normal SL; strike state machine does not stack a 2nd S1 entry
st=run(PIV,OPEN+SWEEP+RETEST+BREAK+[(at(9,40),S1*1.05),(at(9,45),S1*0.99),(at(9,50),S1*1.03)])
assert st.entry_count==1, st.entry_count
print("8 one S1 entry per setup (no duplicate while the trade is open): OK")
# 9. after an S1 SL hit: no second S1 entry; the one re-entry comes from a fresh S2 sweep
st=run(PIV,OPEN+SWEEP+RETEST+BREAK)
st.trade_status="SL Hit Closed"; st.state="WAITING"; st.sl_hit_count=1     # what the live loop does on the SL exit
for ts,p in [(at(9,45),S1*1.05),(at(9,50),S1*0.98),(at(9,52),S1*1.01),(at(9,56),S1*1.008),(at(9,58),S1*1.03)]:
    FakeDT.v=ts; st.update_tick(p,PIV,ts,opt_high=S1*1.08,opt_low=min(S1*0.97,p))
assert st.trade_status=="SL Hit Closed" and st.entry_count==1, (st.state,st.trade_status)
FakeDT.v=at(10,5); st.update_tick(S2*1.002,PIV,at(10,5),opt_high=S1*1.08,opt_low=S2*1.002)
assert st.trade_status=="Waiting" and st.state in ("SWEEP_S2","RECLAIM_S2") and st.trigger_level=="S2", (st.state,st.trade_status)
print("9 after an S1 SL: no second S1 entry, re-entry only from a fresh S2 sweep: OK")
print("S1 CONTINUATION TESTS PASSED")
