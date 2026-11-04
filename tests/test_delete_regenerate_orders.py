"""Delete and regenerate generated orders: the NIFTY next-day plan (next_day_plans.json) and the
daily F&O orders (fno_orders.json + "F&O Orders" sheet tab).
Run: python3 tests/test_delete_regenerate_orders.py"""
import sys, os, types, tempfile, importlib.util, json, asyncio
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
_spec=importlib.util.spec_from_file_location("web_server",os.path.join(_root,"web_server.py"))
web=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(web)
fp=web.fno_planner
run=asyncio.run

W=tempfile.mkdtemp()
web.PLANS_FILE=os.path.join(W,"next_day_plans.json")
fp.FNO_ORDERS_FILE=os.path.join(W,"fno_orders.json")
web.get_tv_multi_timeframe_pivots=lambda sym,force_refresh=False: {}
web.get_next_trading_date=lambda *a,**k: "2026-11-05"
CUR, NXT = "09NOV2026", "17NOV2026"
web.feed.upcoming_expiries=lambda sym,n=2,from_day=None: [CUR,NXT][:n]
web.feed.forward_tracker.set_allowed_expiries(None)
web.feed.get_strike_ltp=lambda *a,**k: 0.0

gen_calls=[]
def payload(requested_sym=None, requested_exp=None, bypass_stored_plan=False):
    gen_calls.append(requested_exp)
    return {"spot":23750.0,"current_expiry":CUR,"next_expiry":NXT,
            "watch_items":[{"expiry":requested_exp,"strike":stk,"type":ot,"side":"BUY","is_15m":True,
                            "planned_ep":100.0,"sl":90.0,"t1":120.0,"t2":140.0,"ltp":101.0}
                           for stk,ot in ((23700,"PE"),(23800,"CE"))]}
web.get_dashboard_payload=payload

# A. NIFTY plan: delete one order, then the rest of an expiry; nothing else changes
plan=web.generate_and_store_next_day_plan("NIFTY",force_refresh=True)
ids=[s["setup_id"] for s in plan["setups"]]
assert len(ids)==4, ids
one=ids[0]
r=run(web.delete_planner_orders(web.DeleteOrdersRequest(symbol="NIFTY",setup_ids=[one])))
assert r["success"] and r["deleted"]==1 and r["remaining"]==3, r
left=[s["setup_id"] for s in web.get_stored_plan("NIFTY")["setups"]]
assert one not in left and len(left)==3
r=run(web.delete_planner_orders(web.DeleteOrdersRequest(symbol="NIFTY",setup_ids=["nope"])))
assert r["success"] and r["deleted"]==0
r=run(web.delete_planner_orders(web.DeleteOrdersRequest(symbol="NIFTY",setup_ids=[])))
assert not r["success"]                                           # an empty selection never means "all"
r=run(web.delete_planner_orders(web.DeleteOrdersRequest(symbol="NIFTY",expiry=NXT)))
assert r["deleted"]==2 and {s["expiry"] for s in web.get_stored_plan("NIFTY")["setups"]}=={CUR}, r
print("A NIFTY plan: delete one order / one expiry: OK")

# B. deleting every order keeps the plan empty (no silent auto-regenerate) until REGENERATE
r=run(web.delete_planner_orders(web.DeleteOrdersRequest(symbol="NIFTY")))
assert r["remaining"]==0
stored=web.get_stored_plan("NIFTY")
assert stored["user_cleared"] and stored["setups"]==[]
gen_calls.clear()
p=run(web.get_planner_deck("NIFTY"))
assert p["setups"]==[] and p["user_cleared"] and gen_calls==[], gen_calls
assert web.generate_and_store_next_day_plan("NIFTY",force_refresh=False)["setups"]==[] and gen_calls==[]
a=run(web.apply_planner_to_live_scanner({"symbol":"NIFTY"}))
assert not a["success"] and "REGENERATE" in a["error"] and gen_calls==[]
p=run(web.trigger_eod_planner({"symbol":"NIFTY"}))            # REGENERATE
assert len(p["setups"])==4 and not p.get("user_cleared") and gen_calls
assert len(web.get_stored_plan("NIFTY")["setups"])==4
print("B delete all stays empty; REGENERATE rebuilds 4 orders: OK")

# C. F&O orders: delete one / a symbol's expiry; sheet rows of exactly those orders removed
def setup(sym,exp,stk,ot,side,tf="15M"):
    return {"setup_id":f"{sym}_2026-11-05_{stk}_{ot}_{side}_{tf}_{exp}","symbol":sym,"expiry":exp,"timeframe":tf,
            "strike":stk,"type":ot,"side":side,"option_symbol":f"{sym}{exp}{stk}{ot}","entry_price":50.0,
            "target_1":60,"target_2":70,"target_3":80,"target_4":90,"sl":40,"rr":"1:1","ref_close":55,
            "candle_date":"2026-11-04","source":"Dhan","setup":"x"}
res=[{"symbol":"BANKNIFTY","expiries":[CUR],"setups":[setup("BANKNIFTY",CUR,52000,"CE","BUY"),setup("BANKNIFTY",CUR,52000,"CE","SELL"),
                                                     setup("BANKNIFTY",NXT,51900,"PE","BUY","1H")]},
     {"symbol":"SENSEX","expiries":[CUR],"setups":[setup("SENSEX",CUR,80000,"PE","BUY")]}]
fp.save_run("2026-11-05","2026-11-04 15:46:00","CURRENT",res)

class WS:
    id=7; title=fp.SHEET_TAB
    def __init__(s,rows): s.rows=[list(fp.SHEET_HEADERS)]+[list(map(str,r)) for r in rows]; s.batches=0; s.spreadsheet=s
    def get_all_values(s): return [list(r) for r in s.rows]
    def row_values(s,i): return s.rows[i-1] if len(s.rows)>=i else []
    def update(s,values,range_name): s.rows[0]=values[0]
    def append_rows(s,rows,value_input_option=None): s.rows+= [list(map(str,r)) for r in rows]
    def batch_update(s,body):
        s.batches+=1
        for q in body["requests"]:                                # bottom block first, like the API
            rg=q["deleteDimension"]["range"]; del s.rows[rg["startIndex"]:rg["endIndex"]]
old=["2026-11-04","x","BANKNIFTY",CUR,"15M","52000","CE","BUY"]+[""]*12      # an older plan date
rows=fp.sheet_rows("2026-11-05","2026-11-04 15:46:00",res)
rows[1][0]="05/11/2026"                                            # sheet shows its own date format
ws=WS([old]+rows)
eng=types.SimpleNamespace(SPREADSHEET_ID="s",get_gspread_client=lambda: types.SimpleNamespace(
    open_by_key=lambda k: types.SimpleNamespace(worksheets=lambda: [ws])))

d=fp.delete_orders(None,[res[0]["setups"][1]["setup_id"]])          # BANKNIFTY 52000 CE SELL
assert d["deleted"]==1 and d["plan_date"]=="2026-11-05"
assert "1 rows removed" in fp.remove_from_sheet(eng,d["keys"]), ws.rows
assert len(ws.rows)==1+1+3 and not any(r[7]=="SELL" for r in ws.rows[1:]) and ws.rows[1]==list(map(str,old))
d=fp.delete_orders("2026-11-05",None,"BANKNIFTY",NXT)
assert d["deleted"]==1 and fp.remove_from_sheet(eng,d["keys"]).startswith("1 rows")
v=fp.flat_orders()
assert [s["setup_id"] for s in v["setups"]]==[res[0]["setups"][0]["setup_id"],res[1]["setups"][0]["setup_id"]]
assert v["summary"]["BANKNIFTY"]["orders"]==1
assert not fp.delete_orders(None,[])["success"]                     # empty selection never deletes all
print("C F&O delete one / symbol+expiry, sheet rows removed (other dates kept): OK")

# D. F&O regenerate of one symbol replaces its rows in the sheet (no duplicates), others kept
new=[{"symbol":"SENSEX","expiries":[CUR],"setups":[setup("SENSEX",CUR,80100,"PE","BUY"),setup("SENSEX",CUR,80200,"CE","SELL")]}]
fp.save_run("2026-11-05","2026-11-04 16:10:00","CURRENT",new)
note=fp.push_to_sheet(eng,fp.sheet_rows("2026-11-05","2026-11-04 16:10:00",new),replace={("2026-11-05","SENSEX")})
assert "2 rows added" in note and "1 old rows replaced" in note, note
sx=[r for r in ws.rows[1:] if r[2]=="SENSEX"]
assert sorted(r[5] for r in sx)==["80100","80200"], sx
assert sum(r[2]=="BANKNIFTY" for r in ws.rows[1:])==2              # old-date row + remaining 52000 BUY
assert len(fp.flat_orders(symbol="SENSEX")["setups"])==2 and len(fp.flat_orders(symbol="BANKNIFTY")["setups"])==1
print("D regenerate one symbol replaces its sheet rows: OK")

# E. web endpoint: blocked while a run is going; otherwise deletes and clears the sheet in background
fp.status["running"]=True
assert not run(web.delete_fno_orders(web.DeleteOrdersRequest(symbol="SENSEX")))["success"]
fp.status["running"]=False
calls=[]
fp.remove_from_sheet=lambda e,keys: calls.append(keys) or "ok"
r=run(web.delete_fno_orders(web.DeleteOrdersRequest(symbol="SENSEX")))
assert r["success"] and r["deleted"]==2 and "keys" not in r, r
import time; time.sleep(0.2)
assert len(calls)==1 and len(calls[0])==2
assert fp.flat_orders(symbol="SENSEX")["setups"]==[] and len(fp.flat_orders()["setups"])==1
print("E delete endpoint: OK")
print("DELETE / REGENERATE TESTS PASSED")
