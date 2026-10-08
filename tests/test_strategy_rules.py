"""The strategy rules from entry_logic.md, checked on the live state machine.
Run: python3 tests/test_strategy_rules.py"""
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

def run(piv, path, mode, ot="CE"):
    """path: [(time, ltp)]; high/low are today's traded range so far."""
    st=app.StrategyStrikeState(ot,22600,expiry="13OCT2026",mode=mode); hi=0; lo=1e9
    for ts,p in path:
        hi=max(hi,p); lo=min(lo,p); FakeDT.v=ts
        st.update_tick(p,piv,ts,opt_high=hi,opt_low=lo)
    return st

PIV=app.calc_fib_pivots(226.05,122.45,132.6)     # 22600 CE 13 OCT, 7 Oct candle
S2,S1,P,R1,R2,R3,S3=(PIV[k] for k in ("S2","S1","P","R1","R2","R3","S3"))
print("levels S3 %.2f S2 %.2f S1 %.2f P %.2f R1 %.2f R2 %.2f R3 %.2f"%(S3,S2,S1,P,R1,R2,R3))
assert (PIV["P"],PIV["R2"],PIV["S2"])==(160.37,224.39,96.34)

# 1. BUY at S2: sweep below S2 -> reclaim -> 5M candle closes green above S2 -> push -> entry at S2
buy=[(at(9,25),S2*1.05),(at(9,30,10),S2*0.97),(at(9,31),S2*0.955),(at(9,33),S2*1.003),(at(9,34),S2*1.008),   # LL = S2*0.955
     (at(9,35),S2*1.004),(at(9,36),S2*1.006)]
st=run(PIV,buy,"BUY")
assert st.state=="CONFIRMED_BUY" and st.trade_status=="Active", (st.state, st.signal)
assert st.entry_price==S2 and st.sl==round(S2*0.955,2)
assert st.targets==[S1,P,R1,R2,R3], st.targets
print("1 BUY S2: entry %.2f SL(LL) %.2f targets %s: OK"%(st.entry_price,st.sl,st.targets))

# 2. no sweep, no trade: price wanders between S1 and R1 all morning
calm=[(at(9,30+i//6, (i%6)*10), P*(0.9+0.02*(i%10))) for i in range(60)]
for mode in ("BUY","SELL"):
    st=run(PIV,calm,mode); assert st.trade_status=="Waiting" and st.state=="WAITING", (mode, st.state)
print("2 no sweep of S2/R2: no trade: OK")

# 3. sweep and reclaim but the 5M candle closes red -> no entry
red=[(at(9,31),S2*1.02),(at(9,32),S2*0.97),(at(9,33),S2*1.01),(at(9,34),S2*0.999),(at(9,35),S2*1.004),(at(9,36),S2*1.006)]
st=run(PIV,red,"BUY")
assert st.trade_status=="Waiting", (st.state, st.signal)
print("3 red 5M candle after the reclaim: no BUY: OK")

# 4. SELL at R2: sweep above R2 -> rejection -> red 5M close -> drop -> entry at R2, SL = HH
sell=[(at(9,25),R2*0.95),(at(9,30,10),R2*1.03),(at(9,31),R2*1.045),(at(9,33),R2*0.997),(at(9,34),R2*0.992),
      (at(9,35),R2*0.996),(at(9,36),R2*0.994)]
st=run(PIV,sell,"SELL")
assert st.state=="CONFIRMED_SELL" and st.entry_price==R2 and st.sl==round(R2*1.045,2), (st.state, st.signal)
assert st.targets==[R1,P,S1,S2,S3], st.targets
print("4 SELL R2: entry %.2f SL(HH) %.2f targets %s: OK"%(st.entry_price,st.sl,st.targets))

# 5. S2 <= 0.05 -> the BUY level is S1, targets P, R1, R2, R3, R3 + 5%
LOW=app.calc_fib_pivots(60.0,6.0,8.0)
assert LOW["S2"]<=0.05
s1=LOW["S1"]
low=[(at(9,25),s1*1.2),(at(9,30,10),s1*0.95),(at(9,31),s1*0.9),(at(9,34),s1*1.01),(at(9,35),s1*1.004),(at(9,36),s1*1.008)]
st=run(LOW,low,"BUY")
assert st.state=="CONFIRMED_BUY" and st.trigger_level=="S1" and st.entry_price==s1, (st.state, st.signal)
assert st.targets==[LOW["P"],LOW["R1"],LOW["R2"],LOW["R3"],round(LOW["R3"]*1.05,2)], st.targets
print("5 S2<=0.05 -> BUY at S1, targets P..R3+5%%: %s: OK"%st.targets)

# 6. no entries outside 09:15-15:30
late=[(datetime(2026,10,9,15,31,i),p) for i,(_,p) in enumerate(buy)]
st=run(PIV,late,"BUY"); assert st.trade_status=="Waiting"
print("6 after 15:30: no setups: OK")
print("STRATEGY RULE TESTS PASSED")
