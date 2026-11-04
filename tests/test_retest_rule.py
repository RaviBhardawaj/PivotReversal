"""S2/R2 retest entry rule (AJ, 4 Nov 2026): close back above S2 -> retest after that close ->
entry above the first close-above candle high (R2 mirror). One-way continuation is ignored.
Run: python3 tests/test_retest_rule.py"""
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

PIV=app.calc_fib_pivots(226.05,122.45,132.6)
S2,S1,P,R1,R2,R3,S3=(PIV[k] for k in ("S2","S1","P","R1","R2","R3","S3"))
B=lambda f: round(S2*f,2)
R=lambda f: round(R2*f,2)

# 1. full rule: break below S2 -> 09:30 candle closes back above S2 (high 1.012) -> 09:35 pull-back
#    to S2 -> break above 1.012 -> BUY at 1.012, SL = the sweep low
ok=[(at(9,29),B(1.04)),(at(9,30,10),B(0.97)),(at(9,31),B(0.95)),(at(9,33),B(1.012)),(at(9,34,50),B(1.010)),
    (at(9,35,10),B(1.011)),(at(9,36),B(1.004)),(at(9,37),B(1.009)),(at(9,38),B(1.013))]
st=run(PIV,ok[:6],"BUY")
assert st.state=="RETEST_S2" and st.conf_high==B(1.012) and not st.retest_done, (st.state, st.signal)
st=run(PIV,ok[:7],"BUY")
assert st.state=="TRADING_IN_FAVOR_BUY" and st.retest_done, (st.state, st.signal)
st=run(PIV,ok,"BUY")
assert st.trade_status=="Active" and st.entry_price==B(1.012) and st.sl==B(0.95), (st.state, st.entry_price, st.sl)
print("1 break < S2, close above, retest, break of the close-above candle high -> BUY @ %.2f SL %.2f: OK"%(st.entry_price,st.sl))

# 2. the close-above candle's own wick is the sweep, not the retest: straight up after it -> ignored
#    (the old rule took this as 'retest wick' and bought above 1.012)
up=[(at(9,29),B(1.04)),(at(9,30,10),B(0.97)),(at(9,31),B(0.95)),(at(9,33),B(1.012)),(at(9,34,50),B(1.010)),
    (at(9,35,10),B(1.011)),(at(9,36),B(1.02)),(at(9,37),B(1.03))]
st=run(PIV,up[:7],"BUY")
assert st.trade_status=="Waiting" and st.state=="WAITING" and "continuation" in st.signal, (st.state, st.signal)
st=run(PIV,up,"BUY")
assert st.trade_status=="Waiting" and st.state=="WAITING", (st.state, st.signal)
# and a later dip to S2 + break of the old high is still no trade: it needs a new break below S2
st=run(PIV,up+[(at(9,45),B(1.005)),(at(9,46),B(1.02))],"BUY")
assert st.trade_status=="Waiting", st.signal
print("2 no retest after the close, price runs on: continuation ignored: OK")

# 3. one-way move down: breaks S2 and never closes back above it -> no entry
down=[(at(9,29),B(1.02)),(at(9,30,10),B(0.99))]+[(at(9,31+i),B(0.985-0.003*i)) for i in range(12)]
st=run(PIV,down,"BUY")
assert st.trade_status=="Waiting" and st.conf_high==0, (st.state, st.signal)
print("3 continuation below S2 with no close back above: no entry: OK")

# 4. near-touch of S2 within 1% (never below) is a sweep (AJ, 4 Nov) -> same close / retest / break rule
near=[(at(9,29),B(1.03)),(at(9,30,10),B(1.006)),(at(9,31),B(1.002)),(at(9,34),B(1.01)),(at(9,35,10),B(1.004)),
      (at(9,36),B(1.008)),(at(9,37),B(1.012))]
st=run(PIV,near,"BUY")
assert st.trade_status=="Active" and st.entry_price==B(1.01) and st.sl==B(1.006), (st.state, st.sl, st.signal)
# with REQUIRE_LEVEL_BREAK on, the same path is no setup
app.REQUIRE_LEVEL_BREAK=True
try: st=run(PIV,near,"BUY")
finally: app.REQUIRE_LEVEL_BREAK=False
assert st.state=="WAITING" and st.trade_status=="Waiting", (st.state, st.signal)
print("4 near-touch of S2 within 1%%: BUY @ %.2f after close/retest/break (off with REQUIRE_LEVEL_BREAK): OK"%B(1.01))

# 5. the first candle that closes above S2 counts even if it is red
red=[(at(9,29),B(1.04)),(at(9,30,5),B(1.03)),(at(9,31),B(0.96)),(at(9,33),B(1.035)),(at(9,34,50),B(1.01)),
     (at(9,35,10),B(1.006)),(at(9,36),B(1.036))]
st=run(PIV,red,"BUY")
assert st.trade_status=="Active" and st.entry_price==B(1.035), (st.state, st.signal)
print("5 red candle closing above S2 is the close-above candle -> BUY @ %.2f: OK"%st.entry_price)

# 6. the retest trades back below S2: that is a new sweep; the next close above S2 is the new candle
re=[(at(9,29),B(1.04)),(at(9,30,10),B(0.97)),(at(9,33),B(1.012)),(at(9,34,50),B(1.010)),
    (at(9,35,10),B(0.99)),(at(9,37),B(1.02)),(at(9,39,50),B(1.015)),
    (at(9,40,10),B(1.008)),(at(9,41),B(1.021))]
st=run(PIV,re,"BUY")
assert st.trade_status=="Active" and st.entry_price==B(1.02) and st.sl==B(0.97), (st.state, st.entry_price, st.sl)
print("6 retest broke S2 again: new close-above candle (high %.2f) used, SL stays the sweep low: OK"%st.entry_price)

# 7. SELL mirror: break above R2 -> close back below -> retest -> break below that candle's low
sell=[(at(9,29),R(0.96)),(at(9,30,10),R(1.03)),(at(9,31),R(1.05)),(at(9,33),R(0.988)),(at(9,34,50),R(0.99)),
      (at(9,35,10),R(0.989)),(at(9,36),R(0.996)),(at(9,37),R(0.99)),(at(9,38),R(0.987))]
st=run(PIV,sell,"SELL")
assert st.trade_status=="Active" and st.entry_price==R(0.988) and st.sl==R(1.05), (st.state, st.entry_price, st.sl)
print("7 SELL R2: close below, retest, break of its low -> SELL @ %.2f SL %.2f: OK"%(st.entry_price,st.sl))

# 8. SELL continuation: closes back below R2, then drops through that candle's low with no retest
sdown=sell[:6]+[(at(9,36),R(0.98)),(at(9,37),R(0.97))]
st=run(PIV,sdown[:7],"SELL")
assert st.trade_status=="Waiting" and st.state=="WAITING" and "continuation" in st.signal, (st.state, st.signal)
print("8 SELL continuation below the close-below candle low without a retest: ignored: OK")

# 9. SELL near-touch of R2 within 1% is a sweep too
snear=[(t,round(R2*(2-p/S2),2)) for t,p in near]
st=run(PIV,snear,"SELL")
assert st.trade_status=="Active" and st.entry_price==min(p for _,p in snear[2:5]), (st.state, st.entry_price, st.signal)
print("9 SELL near-touch of R2 within 1%%: SELL @ %.2f: OK"%st.entry_price)
# 10. a day low under S2 the app never watched (e.g. before a restart) does not start a setup
st=app.StrategyStrikeState("CE",22600,expiry="13OCT2026",mode="BUY")
for ts,p in [(at(11,0),B(1.05)),(at(11,4),B(1.04)),(at(11,5,10),B(1.045)),(at(11,10,10),B(1.042))]:
    FakeDT.v=ts; st.update_tick(p,PIV,ts,opt_high=B(1.10),opt_low=B(0.90))
assert st.state=="WAITING", (st.state, st.signal)
print("10 unwatched dip below S2 (day low only): no setup: OK")
print("RETEST RULE TESTS PASSED")
