"""Date-wise BUY / SELL points for the 'Daily PnL' sheet tab.
Run: python3 tests/test_daily_pnl.py [forward_test_trades.csv]"""
import sys, os, types, importlib.util, csv
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

R=lambda d,a,p,st="CLOSED": {"Date":d,"Action":a,"PnL_Pts":p,"Status":st}
rows=[R("2026-10-08","BUY",10),R("2026-10-08","BUY",-4),R("2026-10-08","SELL",6.5),
      R("2026-10-09","SELL",-20),R("2026-10-09","BUY",3),R("2026-10-09","BUY",99,"ABANDONED")]
t=app.daily_pnl_table(rows,[{"Date":"2026-10-09","PnL_Pts":5.5}])
H=app.DAILY_PNL_HEADERS; assert all(len(r)==len(H) for r in t)
d9,d8,tot=t
assert d9==["2026-10-09",1,1,0,3.0,1,0,1,-20.0,2,50.0,-17.0,5.5,-4.5], d9      # newest first; ABANDONED ignored
assert d8==["2026-10-08",2,1,1,6.0,1,1,0,6.5,3,66.7,12.5,0.0,12.5], d8
assert tot==["ALL DAYS",3,2,1,9.0,2,1,1,-13.5,5,60.0,-4.5,5.5,-4.5], tot
print("A BUY / SELL points per date, cumulative, totals: OK")

# B. separate BUY and SELL sheets: open trades first (live price), then closed newest first
full=[dict(r,Entry_Price=100,SL=90,Target_1=130,Trade_High=104,Trade_Low=95,Exit_Price=100+r["PnL_Pts"],Entry_Time="10:00:00",Exit_Reason="SL Hit") for r in rows]
live=[{"Date":"2026-10-09","Action":"SELL","Entry_Price":200,"SL":220,"Current_Price":195,"PnL_Pts":5,"Trade_High":201,"Trade_Low":194}]
b=app.side_trades_table(full,"BUY",live); sl=app.side_trades_table(full,"SELL",live)
assert len(b)==3 and all(len(r)==len(app.SIDE_SHEET_HEADERS) for r in b+sl)
assert [r[0] for r in b]==["2026-10-09","2026-10-08","2026-10-08"] and b[0][14]==3 and b[0][10]==4.0
assert sl[0][15]=="ACTIVE" and sl[0][11]==195 and sl[0][10]==6.0 and [r[15] for r in sl]==["ACTIVE","CLOSED","CLOSED"]
print("B BUY and SELL trade tabs: OK")

if len(sys.argv)>1:
    real=list(csv.DictReader(open(sys.argv[1])))
    for r in [H]+app.daily_pnl_table(real): print(r)
print("DAILY PNL TESTS PASSED")
