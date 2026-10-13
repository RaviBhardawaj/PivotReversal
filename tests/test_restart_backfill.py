"""Trades left ABANDONED by an app restart are finished from that day's 1-minute candles.
Run: python3 tests/test_restart_backfill.py"""
import sys, os, types, importlib.util, tempfile, csv
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
app.TRAIL_SL=True   # these cases cover the trailing-SL path (off in the app since 13 Oct)

def bars(day, start, ohlc):
    """ohlc: list of (o,h,l,c), one per minute from start 'HH:MM'."""
    t0=datetime.strptime(f"{day} {start}","%Y-%m-%d %H:%M")
    return [(t0+timedelta(minutes=i),)+tuple(x) for i,x in enumerate(ohlc)]

def sell(**k):
    t=dict(Trade_ID="NIFTY_CE_22150_13OCT2026_20261009_112525",Date="2026-10-09",Entry_Time="11:25:25",
           Symbol="NIFTY",Type="CE",Strike=22150,Action="SELL",Entry_Price=410.0,SL=430.0,
           Target_1=373.97,Target_2=340.0,Target_3=300.0,Target_4=260.0,Target_5=220.0)
    t.update(k); return t

# A: SELL hits T1, SL trails to entry, then price comes back: trailing SL at entry (0 pts)
t=sell()
b=bars("2026-10-09","11:25",[(410,411,409,410),(408,409,395,396),(396,397,372,374),(380,412,379,411)])
r=app.replay_trade_bars(t,b)
assert r["Target_Hit"]==1 and r["Exit_Reason"]=="Trailing SL Hit" and r["Exit_Price"]==410.0 and r["PnL_Pts"]==0.0, r
assert r["Exit_Time"]=="11:28:00" and r["Max_Pts"]==38.0, r
print("A SELL T1 hit, SL trails to entry, exit at entry: OK")

# B: SL crossed inside a candle fills at the SL level; the entry minute itself is ignored
t=sell()
b=bars("2026-10-09","11:25",[(410,450,400,410),(412,436,405,433)])
r=app.replay_trade_bars(t,b)
assert r["Exit_Reason"]=="SL Hit" and r["Exit_Price"]==430.0 and r["PnL_Pts"]==-20.0 and r["Exit_Time"]=="11:26:00", r
print("B SL inside a candle fills at the SL, entry minute skipped: OK")

# C: gap past the SL at the candle open fills at the open
t=sell()
b=bars("2026-10-09","11:26",[(440,445,438,441)])
r=app.replay_trade_bars(t,b); assert r["Exit_Price"]==440.0 and r["PnL_Pts"]==-30.0, r
print("C gap past SL fills at the open: OK")

# D: target ladder up to T3, then 15:00 square-off at the 15:00 open
t=sell()
ohlc=[(410,411,409,410)]+[(400,401,299,301)]+[(301,305,298,302)]*5
b=bars("2026-10-09","14:52",ohlc)+bars("2026-10-09","15:00",[(295,296,290,291)])
r=app.replay_trade_bars(t,b)
assert r["Target_Hit"]==3 and r["SL"]==340.0 and r["Exit_Reason"]=="EOD 3PM Square-off @ CMP" and r["Exit_Price"]==295 and r["Exit_Time"]=="15:00:00", r
print("D T3 hit (SL trailed to T2), squared off at 15:00: OK")

# E: BUY T5 closes at the T5 level
t=dict(sell(),Type="PE",Action="BUY",Entry_Price=100.0,SL=90.0,Target_1=110.0,Target_2=120.0,Target_3=130.0,Target_4=140.0,Target_5=150.0)
b=bars("2026-10-09","11:26",[(101,160,100,158)])
r=app.replay_trade_bars(t,b); assert r["Exit_Reason"]=="Target 5 Hit" and r["Exit_Price"]==150.0 and r["PnL_Pts"]==50.0, r
print("E BUY Target 5 fills at T5: OK")

# F: candles stop before the exit and before 14:59 → still open (None)
t=sell(); b=bars("2026-10-09","11:26",[(405,415,400,410)]*30)
assert app.replay_trade_bars(t,b) is None
print("F incomplete day data leaves the trade open: OK")

# G: backfill rewrites ABANDONED rows in the CSV; today's rows wait until after 15:00
D=tempfile.mkdtemp(); F=os.path.join(D,"forward_test_trades.csv")
hdr=['Trade_ID','Date','Entry_Time','Timeframe','Symbol','Type','Strike','Action','Entry_Price','SL','Target_1','Target_2','Target_3','Target_4','Target_5','Trade_High','Trade_Low','Max_Pts','Exit_Price','Exit_Time','Exit_Reason','PnL_Pts','Status']
def row(tid,date,tm,typ,stk,act,ep,sl,t1,t2,t3,t4,t5,status="ABANDONED",reason="App restarted - not tracked"):
    return [tid,date,tm,"1H Structure","NIFTY",typ,stk,act,ep,sl,t1,t2,t3,t4,t5,ep,ep,0.0,"","",reason,0.0,status]
with open(F,"w",newline="") as f:
    w=csv.writer(f); w.writerow(hdr)
    w.writerow(row("NIFTY_CE_22150_13OCT2026_20261009_112525","2026-10-09","11:25:25","CE",22150,"SELL",410.0,430.0,373.97,340.0,300.0,260.0,220.0))
    w.writerow(row("NIFTY_PE_22300_19OCT2026_20261009_112519","2026-10-09","11:25:19","PE",22300,"BUY",103.0,96.25,136.08,150,160,170,180))
    w.writerow(row("NIFTY_PE_22000_13OCT2026_20261009_100000","2026-10-09","10:00:00","PE",22000,"BUY",50.0,45.0,60,70,80,90,100,status="CLOSED",reason="SL Hit"))
calls=[]
def fetch(sym,exp,stk,typ,day):
    calls.append((sym,exp,stk,typ,day))
    if typ=="CE":
        return bars(day,"11:25",[(410,411,409,410),(408,409,372,373),(373,374,340,341),(345,412,344,410)])
    raise RuntimeError("no data")
class FakeDT(datetime):
    v=datetime(2026,10,9,14,30)
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT
tr=app.ForwardTestTracker(F); tr.paper_only=True
assert tr.backfill_abandoned(fetch,now=datetime(2026,10,9,14,30))==[] and calls==[]
done=tr.backfill_abandoned(fetch,now=datetime(2026,10,9,15,5))
assert len(done)==1 and ("NIFTY","13OCT2026",22150,"CE","2026-10-09") in calls, calls
rows={r["Trade_ID"]:r for r in csv.DictReader(open(F))}
ce=rows["NIFTY_CE_22150_13OCT2026_20261009_112525"]
assert ce["Status"]=="CLOSED" and ce["Exit_Reason"]=="Trailing SL Hit"+app.RESTART_FILL_NOTE, ce
assert float(ce["PnL_Pts"])==36.03 and float(ce["Max_Pts"])==70.0 and float(ce["SL"])==430.0 and ce["Exit_Time"]=="11:28:00", ce
assert rows["NIFTY_PE_22300_19OCT2026_20261009_112519"]["Status"]=="ABANDONED"
assert rows["NIFTY_PE_22000_13OCT2026_20261009_100000"]["Status"]=="CLOSED"
assert len(tr.get_closed_trades_from_file())==2
print("G ABANDONED CE SELL closed in the CSV after 15:00 (T2 hit, trailed SL); no-data row left; today waits: OK")
print("ALL PASS")
