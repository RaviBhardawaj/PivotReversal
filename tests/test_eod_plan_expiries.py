"""The 15:45 next-day plan ("auto generate orders") holds CURRENT-expiry setups only. Next-expiry
ones only when asked for (planner generate with NEXT selected), a far expiry never. On an expiry
day "current" is the expiry still open on the plan's session, never today's expiring one.
Run: python3 tests/test_eod_plan_expiries.py"""
import sys, os, types, tempfile, importlib.util
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

W=tempfile.mkdtemp()
web.PLANS_FILE=os.path.join(W,"next_day_plans.json")
web.get_tv_multi_timeframe_pivots=lambda sym,force_refresh=False: {}
web.get_next_trading_date=lambda *a,**k: "2026-10-14"

TODAY, CUR, NXT = "13OCT2026", "20OCT2026", "27OCT2026"   # 13 Oct is an expiry day
FAR="03NOV2026"
web.feed.upcoming_expiries=lambda sym,n=2,from_day=None: [CUR,NXT,FAR][:n]   # open on 14 Oct

asked=[]
def payload(requested_sym=None, requested_exp=None, bypass_stored_plan=False):
    asked.append(requested_exp)
    exp={"CURRENT":TODAY,"NEXT":CUR}.get(requested_exp,requested_exp)
    return {"spot":22750.0,"current_expiry":TODAY,"next_expiry":CUR,
            "watch_items":[{"expiry":exp,"strike":22700,"type":"PE","side":"BUY","is_15m":True,
                            "planned_ep":100.0,"sl":90.0,"t1":120.0,"t2":140.0,"ltp":101.0}]}
web.get_dashboard_payload=payload
def plan_exps(include_next=False):
    return sorted({s["expiry"] for s in web.generate_and_store_next_day_plan("NIFTY",force_refresh=True,include_next=include_next)["setups"]})

# A. daily (default): current expiry only, whatever the Next Expiry box says
for gate in (None, [CUR], [CUR, NXT]):
    web.feed.forward_tracker.set_allowed_expiries(gate)
    assert web.plan_expiries("NIFTY","2026-10-14")==[CUR]
    asked.clear(); got=plan_exps()
    assert got==[CUR] and asked==[CUR], (gate,got,asked)
print("A daily plan: only current expiry (%s) orders: OK"%CUR)

# B. asked for the next expiry: current + next, never the far one
assert web.plan_expiries("NIFTY","2026-10-14",include_next=True)==[CUR,NXT]
asked.clear(); got=plan_exps(include_next=True)
assert got==sorted([CUR,NXT]) and asked==[CUR,NXT], (got,asked)
print("B next expiry on request: current + next, no far expiry: OK")

# C. expiries unknown (no instrument list): falls back to CURRENT/NEXT, still current only when unticked
web.feed.upcoming_expiries=lambda sym,n=2,from_day=None: []
MAP={"CURRENT":CUR,"NEXT":NXT}
def payload2(requested_sym=None, requested_exp=None, bypass_stored_plan=False):
    asked.append(requested_exp)
    return {"spot":22750.0,"current_expiry":CUR,"next_expiry":NXT,
            "watch_items":[{"expiry":MAP[requested_exp],"strike":22700,"type":"PE","side":"BUY","is_15m":True,
                            "planned_ep":100.0,"sl":90.0,"t1":120.0,"t2":140.0}]}
web.get_dashboard_payload=payload2
asked.clear(); got=plan_exps()
assert asked==["CURRENT"] and got==[CUR], (got,asked)
asked.clear(); got=plan_exps(include_next=True)
assert asked==["CURRENT","NEXT"] and got==sorted([CUR,NXT]), (got,asked)
print("C fallback without the instrument list: OK")

# D. an expiry that has passed is never planned (today's expiring contracts on an expiry day)
web.get_dashboard_payload=payload          # CURRENT -> 13OCT, which expires before 14 Oct
asked.clear(); got=plan_exps()
assert got==[], (got,asked)                # fallback "CURRENT" is today's expiring contract
web.feed.upcoming_expiries=lambda sym,n=2,from_day=None: [CUR,NXT,FAR][:n]
asked.clear(); got=plan_exps()
assert got==[CUR], (got,asked)
print("D expired expiry (%s) left out of the plan: OK"%TODAY)

# E. stored plans: orders of an expiry that has passed are purged from next_day_plans.json
import json
from datetime import datetime, date
class FakeDT(datetime):
    @classmethod
    def now(cls,tz=None): return cls(2026,11,4,9,20)
web.engine.datetime=FakeDT
assert web.engine.expiry_is_past("03NOV2026") and not web.engine.expiry_is_past("04NOV2026")
assert not web.engine.expiry_is_past("") and not web.engine.expiry_is_past(None)
json.dump({"NIFTY_2026-11-04":{"symbol":"NIFTY","target_trading_date":"2026-11-04","setups":[
    {"expiry":"03NOV2026","strike":24000,"type":"CE"},{"expiry":"10NOV2026","strike":24000,"type":"CE"}]}},
    open(web.PLANS_FILE,"w"))
p=web.get_stored_plan("NIFTY","2026-11-04")
assert [x["expiry"] for x in p["setups"]]==["10NOV2026"], p["setups"]
on_disk=json.load(open(web.PLANS_FILE))["NIFTY_2026-11-04"]["setups"]
assert [x["expiry"] for x in on_disk]==["10NOV2026"], on_disk
print("E 3 Nov orders purged from the stored plan (shown and on disk): OK")

# F. strike level cache entries of a past expiry are dropped
lv={"1":{"expiry":"03NOV2026"},"2":{"expiry":"10NOV2026"},"3":{"expiry":""}}
assert set(web.engine.drop_expired(lv))=={"2","3"}
print("F expired strike levels dropped: OK")
print("EOD PLAN EXPIRY TESTS PASSED")
