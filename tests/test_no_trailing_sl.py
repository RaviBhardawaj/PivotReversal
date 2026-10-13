"""No trailing SL (AJ, 13 Oct): after T1, T2 ... the SL stays the LL (BUY) / HH (SELL) the trade
was entered with. The trade ends only at that SL, T5 or the 3 PM exit.
Run: python3 tests/test_no_trailing_sl.py"""
import sys, os, types, importlib.util
from datetime import datetime, timedelta
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
assert app.TRAIL_SL is False

def tr(action, entry, sl, ts):
    return dict(Action=action, Type="PE", Strike=22400, Entry_Price=entry, SL=sl, Initial_SL=sl,
                **{f"Target_{i+1}": t for i, t in enumerate(ts)}, Target_Hit=0)

# 1. BUY: T1 and T2 hit, price falls back to entry and to T1: still open, SL still the LL
t=tr("BUY",47.0,20.0,[60,80,100,120,140])
for px in (61,81,79,59,47,30):
    closed,why,pnl=app.fwd_exit_check(t,px)
    assert not closed and t["SL"]==20.0, (px,t["SL"],why)
assert t["Target_Hit"]==2
closed,why,pnl=app.fwd_exit_check(t,19.9)
assert closed and why=="SL Hit" and pnl==round(19.9-47.0,2), (why,pnl)
print("1 BUY: after T2 the SL stays the LL 20.00; exit only at the LL: OK")

# 2. SELL: T1 and T2 hit, price comes back above entry: still open, SL still the HH
t=tr("SELL",516.25,538.0,[464.56,363.25,261.94,199.36,98.05])
for px in (460,360,400,516.25,530):
    closed,why,pnl=app.fwd_exit_check(t,px)
    assert not closed and t["SL"]==538.0, (px,t["SL"],why)
closed,why,pnl=app.fwd_exit_check(t,538.0)
assert closed and why=="SL Hit", why
print("2 SELL: after T2 the SL stays the HH 538.00; exit only at the HH: OK")

# 3. T5 still closes the trade
t=tr("BUY",47.0,20.0,[60,80,100,120,140])
closed,why,pnl=app.fwd_exit_check(t,141)
assert closed and why=="Target 5 Hit"
print("3 T5 still exits: OK")

# 4. the live state machine keeps its SL too
st=app.StrategyStrikeState("PE",22400,expiry="13OCT2026",mode="BUY")
st.state="CONFIRMED_BUY"; st.trade_status="Active"; st.entry_price=47.0; st.sl=st.entry_sl=20.0
st.targets=[60,80,100,120,140]; st.signal="★ BUY PE"
st.locked_pivots=None
piv={"S3":5,"S2":20.5,"S1":40,"P":60,"R1":80,"R2":100,"R3":120}
now=datetime(2026,10,13,10,0)
class FakeDT(datetime):
    @classmethod
    def now(cls,tz=None): return now
app.datetime=FakeDT
for i,px in enumerate((61,81,101,60,47,25)):
    st.update_tick(px,piv,now+timedelta(minutes=i))
    assert st.sl==20.0, (px,st.sl,st.trade_status)
assert st.trade_status in ("Target 3 Hit","Target 2 Hit","Target 1 Hit","Active"), st.trade_status
print("4 strategy state: SL stays 20.00 through T1-T3 and the pull-back: OK")
print("NO TRAILING SL TESTS PASSED")
