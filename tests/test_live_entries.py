"""Entries only on real trades from today, and only at the entry level.
Replays 8 Oct 2026 09:15: the feed's first ticks still showed 22550 CE (19OCT) at yesterday's
close 235.50 with yesterday's high 335.00, above R2 332.69, and the old code sold at 235.50.
Run: python3 tests/test_live_entries.py"""
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
    v=datetime(2026,10,8,9,15,0)
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT
os.environ.pop("FORCE_MARKET_OPEN",None)

PIV={"S3":157.3,"S2":198.71,"S1":224.29,"P":265.7,"R1":307.11,"R2":332.69,"R3":374.1}
yday=datetime(2026,10,7,15,29,59).timestamp()
def at(h,m,s=0): return datetime(2026,10,8,h,m,s)

# A. trade-time check
FakeDT.v=at(9,15,0)
assert not app.traded_today_tick({"last_traded_timestamp":int(yday)},235.5)
assert app.traded_today_tick({"last_traded_timestamp":int(at(9,15,1).timestamp())},236.0)
assert app.traded_today_tick({"last_traded_timestamp":int(at(9,15,1).timestamp()*1000)},236.0)
assert not app.traded_today_tick({},235.5,235.5)       # no trade time, no move
assert app.traded_today_tick({},236.0,235.5)           # no trade time, price moved
FakeDT.v=at(9,10,0)
assert not app.traded_today_tick({"last_traded_timestamp":int(at(9,10).timestamp())},236.0)
print("A trade-time check: OK")

# B. feed tick parsing keeps today's range apart from yesterday's
class Eng: pass
e=Eng(); import threading; e.lock=threading.Lock(); e.live_data={}; e.spot_token={}
fn=app.FeedEngine._on_data_impl
def tick(ts,ltp,hi,lo,ltt):
    FakeDT.v=ts
    fn(e,None,{"token":"T","last_traded_price":int(round(ltp*100)),"high_price_of_the_day":int(round(hi*100)),
               "low_price_of_the_day":int(round(lo*100)),"closed_price":23550,"open_price_of_the_day":0,
               "last_traded_timestamp":int(ltt.timestamp()) if ltt else 0})
    return e.live_data["T"]
d=tick(at(9,15,0),235.5,335.0,226.6,datetime.fromtimestamp(yday))
assert d["s_live"] is False and d["s_high"]==0, d
d=tick(at(9,15,1),240.0,240.0,238.0,at(9,15,1))
assert d["s_live"] and d["s_high"]==240.0 and d["s_low"]==238.0, d
print("B feed parsing: yesterday's high 335 ignored, today's range 238-240: OK")

# C. replay of the bad open: no setup, no entry
def run(seq, side="SELL"):
    st=app.StrategyStrikeState("CE",22550,expiry="19OCT2026",mode=side)
    for ts,ltp,hi,lo in seq:
        FakeDT.v=ts; st.update_tick(ltp,PIV,ts,opt_high=hi,opt_low=lo)
    return st
seq=[(at(9,15,0,),235.5+i*0.05,240.0,235.0) for i in range(20)]
st=run(seq)
assert st.state=="WAITING" and st.trade_status=="Waiting", st.state
print("C opening ticks below R2: stays WAITING: OK")

# D. a real sweep above R2, a red 5M candle closes below R2, then a break below that candle's low -> SELL
seq=[]; hi=0; lo=999
def add(ts,p):
    global hi,lo
    hi=max(hi,p); lo=min(lo,p); seq.append((ts,p,hi,lo))
for i,p in enumerate([333.0,334.5,336.2,335.0]): add(at(9,20,i),p)     # sweep, HH 336.2
for i,p in enumerate([331.5,330.8]): add(at(9,24,50+i),p)                  # back below R2
for i,p in enumerate([331.0,330.5]): add(at(9,25,i),p)                    # 09:20 candle closed red (low 330.8); break below
st=run(seq)
assert st.state=="CONFIRMED_SELL" and st.trade_status=="Active", (st.state, st.signal)
assert st.entry_price==330.8 and st.sl==336.2, (st.entry_price, st.sl)
assert app.at_entry_price(seq[-1][1], st.entry_price, "SELL")
print("D real R2 sweep -> SELL below the confirmation candle low 330.80, SL HH 336.20: OK")

# E. price already far below the entry level: no chase, wait for the level
seq2=seq[:-2]+[(at(9,25,8),318.0,hi,318.0),(at(9,25,9),317.0,hi,317.0)]
st=run(seq2)
assert st.trade_status=="Waiting" and "come back to entry" in st.signal, (st.state, st.signal)
FakeDT.v=at(9,26); st.update_tick(330.6,PIV,at(9,26),opt_high=hi,opt_low=317.0)
assert st.trade_status=="Active" and st.entry_price==330.8, st.signal
print("E no chase when price ran 15 pts below entry; enters when back at the entry: OK")

# F. BUY side mirror: price far above S2 never buys
st=app.StrategyStrikeState("CE",22550,expiry="19OCT2026",mode="BUY")
for i in range(10):
    FakeDT.v=at(9,15,i); st.update_tick(235.5,PIV,at(9,15,i),opt_high=236,opt_low=235)
assert st.trade_status=="Waiting"
assert not app.at_entry_price(235.5,198.71,"BUY") and app.at_entry_price(199.5,198.71,"BUY")
assert not app.at_entry_price(235.5,332.69,"SELL")
print("F BUY side and entry band: OK")
# G. the SL is shown at every stage after entry (the trailed SL, not the day low)
ds=app.display_sl
for ts in ("Active","Target 1 Hit","Target 2 Hit","Target 3 Hit","Target 4 Hit","SL Hit Closed","Trailing SL Hit Closed"):
    assert ds(ts,"CONFIRMED_BUY",104.0,True,96.0,ll=90.0)=="104.00", ts
    assert ds(ts,"CONFIRMED_SELL",300.0,False,330.0,hh=350.0)=="300.00", ts
assert ds("Waiting","WAITING",0,True,96.0,ll=90.0)=="-"
assert ds("Waiting","RECLAIM_S2",0,True,96.0,ll=90.0)=="90.00"        # setup preview = LL
print("G SL shown after entry at every target and after the exit: OK")
# H: after entry the SL column shows the LL the trade was entered with, trail in brackets (AJ 12 Oct)
assert app.display_sl("Target 1 Hit","CONFIRMED_BUY",47.0,True,47.0,ll=20.0,entry_sl=20.0)=="20.00 (trail 47.00)"
assert app.display_sl("Active","CONFIRMED_BUY",20.0,True,47.0,entry_sl=20.0)=="20.00"
assert app.display_sl("Active","CONFIRMED_SELL",231.0,False,200.0,entry_sl=231.0)=="231.00"
print("H SL column = entry LL/HH, trailed stop in brackets: OK")
print("LIVE ENTRY TESTS PASSED")
