"""TSL study (data only): targets reached and when, peak, Target-then-SL flag and shadow
trailing-SL results are recorded per trade; the live SL / exits are unchanged.
Run: python3 tests/test_tsl_study.py"""
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
_root=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..")
sys.path.insert(0,_root)
_spec=importlib.util.spec_from_file_location("app",os.path.join(_root,"Options-angel_live-ok.py"))
app=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(app)
import tsl_study as ts
H=ts.HEADERS
col=lambda row,name: row[H.index(name)]
assert app.TRAIL_SL is False

class FakeDT(datetime):
    v=datetime(2026,11,4,10,0)
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT
os.environ["FORCE_MARKET_OPEN"]="1"

tmp=tempfile.mkdtemp()
log=os.path.join(tmp,"forward_test_trades.csv")
ft=app.ForwardTestTracker(filepath=log); ft.paper_only=True
jr=ts.TslJournal(ts.journal_path(log))

# 1. BUY reaches T1 and T2, then falls back to the original SL: flagged Target then SL,
#    exit unchanged (still the LL), shadow techniques show what a trail would have kept.
tr=ft.log_entry("NIFTY","PE",22400,"BUY",50.0,40.0,60,70,80,90,100,expiry="10NOV2026")
assert tr
t0=datetime(2026,11,4,10,0,0)
path=[55,61,66,71,74,69,63,58,49,39.5]
closed=None
for i,px in enumerate(path):
    FakeDT.v=t0+timedelta(minutes=i+1)
    closed=ft.update_tick("PE",22400,px,expiry="10NOV2026",symbol="NIFTY") or closed
assert closed and closed["Exit_Reason"]=="SL Hit" and closed["SL"]==40.0 and closed["PnL_Pts"]==-10.5, closed
row=[r for r in jr.rows() if r[0]==tr["Trade_ID"]][0]
assert col(row,"Outcome")==ts.TARGET_THEN_SL, row
assert col(row,"Max Target")=="2" and col(row,"T1 Time")=="10:02:00" and col(row,"T2 Time")=="10:04:00", row
assert col(row,"T3 Time")=="" and col(row,"Peak Px")=="74.0" and col(row,"Peak Time")=="10:05:00", row
assert col(row,"MFE Pts")=="24.0" and col(row,"Giveback Pts")=="34.5", row
# BE@T1: stop at 50 -> first price <= 50 is 49 (-1). STEP: after T2 stop at T1=60 -> 58 (+8).
# LOCK50: peak 74 -> stop 62 -> 58 (+8)  (69 > 62.0, 63 > 62.0, 58 <= 62)
assert col(row,"BE@T1 PnL")=="-1.0" and col(row,"Step Trail PnL")=="8.0" and col(row,"Lock 50% PnL")=="8.0", row
assert col(row,"Data")==ts.LIVE_DATA
print("1 BUY T1,T2 then SL: Target then SL flagged with times, peak and shadow TSL results: OK")

# 2. SELL straight to SL with no target: plain SL, shadow results = actual
tr2=ft.log_entry("NIFTY","CE",22600,"SELL",100.0,110.0,90,80,70,60,50,expiry="10NOV2026")
for i,px in enumerate([98,95,104,111]):
    FakeDT.v=datetime(2026,11,4,11,i+1)
    c=ft.update_tick("CE",22600,px,expiry="10NOV2026",symbol="NIFTY")
row=[r for r in jr.rows() if r[0]==tr2["Trade_ID"]][0]
assert c and col(row,"Outcome")=="🛑 SL (no target)" and col(row,"Max Target")=="0", row
assert col(row,"MAE Pts")=="-11.0" and col(row,"MFE Pts")=="5.0", row
assert col(row,"BE@T1 PnL")==col(row,"Actual PnL")=="-11.0", row
print("2 SELL no target then SL: plain SL, shadow = actual: OK")

# 3. SELL T1 then 3 PM square-off in loss: flagged T1 then EOD loss
tr3=ft.log_entry("NIFTY","CE",22700,"SELL",100.0,120.0,90,80,70,60,50,expiry="10NOV2026")
for i,px in enumerate([95,89,97,104]):
    FakeDT.v=datetime(2026,11,4,14,50+i)
    ft.update_tick("CE",22700,px,expiry="10NOV2026",symbol="NIFTY")
FakeDT.v=datetime(2026,11,4,15,0)
ft.eod_square_off()
row=[r for r in jr.rows() if r[0]==tr3["Trade_ID"]][0]
assert col(row,"Outcome")=="🎯→🔻 T1 then EOD loss" and col(row,"Actual PnL")=="-4.0", row
# BE@T1 after T1 (89): stop 100 -> 104 >= 100 -> -4 (same tick); LOCK50 stop 94.5 -> 97 -> +3
assert col(row,"BE@T1 PnL")=="-4.0" and col(row,"Lock 50% PnL")=="3.0", row
print("3 SELL T1 then EOD in loss: flagged, shadow lock50 kept +3: OK")

# 4. Banner totals
b=ts.banner(jr.rows())
assert "Trades: 3" in b and "Target then SL: 1 (-10.50 pts)" in b and "Actual (fixed SL): -25.50" in b, b
print("4 banner totals:", b)

# 5. Backfill from an older trade log without tick data (estimated, times blank)
old=os.path.join(tmp,"old","forward_test_trades.csv"); os.makedirs(os.path.dirname(old))
f=open(old,"w",newline=""); w=csv.writer(f)
w.writerow(['Trade_ID','Date','Entry_Time','Timeframe','Symbol','Type','Strike','Action','Entry_Price','SL',
            'Target_1','Target_2','Target_3','Target_4','Target_5','Trade_High','Trade_Low','Max_Pts',
            'Exit_Price','Exit_Time','Exit_Reason','PnL_Pts','Status'])
w.writerow(['NIFTY_PE_22400_13OCT2026_20261009_101500','2026-10-09','10:15:00','15M Intraday','NIFTY','PE',22400,'BUY',
            50,40,60,70,80,90,100,75,39,25,39.5,'11:00:00','SL Hit',-10.5,'CLOSED'])
w.writerow(['X2','2026-10-09','10:20:00','15M Intraday','NIFTY','PE',22500,'BUY',50,40,60,70,80,90,100,58,50,8,62,'15:00:00','EOD 3PM Square-off @ CMP',12,'CLOSED'])
w.writerow(['X3','2026-10-09','10:30:00','15M Intraday','NIFTY','PE',22600,'BUY',50,40,60,70,80,90,100,50,50,0,'','','ACTIVE',0,'ABANDONED'])
f.close()
oj=ts.TslJournal(ts.journal_path(old))
assert oj.backfill(old)==2 and oj.backfill(old)==0
r1=[r for r in oj.rows() if r[0].startswith("NIFTY_PE_22400")][0]
assert col(r1,"Outcome")==ts.TARGET_THEN_SL and col(r1,"Max Target")=="2" and col(r1,"T1 Time")=="", r1
assert col(r1,"Expiry")=="13OCT2026" and col(r1,"Data")==ts.HISTORY_DATA
assert col(r1,"BE@T1 PnL")=="0.0" and col(r1,"Step Trail PnL")=="10.0" and col(r1,"Lock 50% PnL")=="12.5", r1
r2=[r for r in oj.rows() if r[0]=="X2"][0]
assert col(r2,"Outcome")=="⏰ EOD (no target)" and col(r2,"Max Target")=="0", r2
print("5 backfill from the trade log: Target then SL found, TSL estimated, re-run adds nothing: OK")

# 6. A live record is not replaced by a later estimate of the same trade
n=len(jr.rows()); assert jr.backfill(log)==0 and len(jr.rows())==n
print("6 live rows kept over estimates: OK")

# 7. 1-min replay (app restarted) also records targets, with shadow stops filled at the level
t=dict(Trade_ID="NIFTY_PE_22400_13OCT2026_20261009_112525",Date="2026-10-09",Entry_Time="11:25:25",Symbol="NIFTY",
       Type="PE",Strike=22400,Action="BUY",Entry_Price=50.0,SL=40.0,Initial_SL=40.0,
       Target_1=60,Target_2=70,Target_3=80,Target_4=90,Target_5=100)
t0=datetime(2026,10,9,11,25)
bars=[(t0,50,51,49,50),(t0+timedelta(minutes=1),50,72,49.5,71),(t0+timedelta(minutes=2),71,71,38,39)]
r=app.replay_trade_bars(t,bars)
assert r and r["Exit_Reason"]=="SL Hit" and r["SL"]==40.0
assert r["TSL_T1_Time"]=="11:26:00" and r["TSL_T2_Time"]=="11:26:00" and r["TSL_Max_Target"]==2, r
assert r["TSL_STEP_Exit"]==60 and r["TSL_BE_Exit"]==50 and r["TSL_Data"]==ts.REPLAY_DATA, r
row=ts.study_row(dict(r,Status="CLOSED"))
assert col(row,"Outcome")==ts.TARGET_THEN_SL and col(row,"Step Trail PnL")==10.0, row
print("7 1-min replay records target times and shadow exits at the level: OK")
print("ALL PASS")
