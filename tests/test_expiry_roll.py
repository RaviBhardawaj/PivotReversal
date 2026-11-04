"""Missed S2 entries on the current expiry (AJ, 4 Nov 2026): the morning instrument list still
had yesterday's 03NOV2026 expiry, the feed watched it in place of 09NOV2026, and the 09NOV
strikes got no ticks until a restart at 11:00, so 23750 PE 09NOV's S2 (113.06) touch, and every
other 09NOV setup before 11:00, was never seen.
Run: python3 tests/test_expiry_roll.py"""
import sys, os, types, importlib.util, threading
from datetime import date
import pandas as pd
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

NOV4 = date(2026, 11, 4)
OPEN = ["09NOV2026", "17NOV2026", "23NOV2026"]      # still open on 4 Nov, nearest first

# 1. yesterday's expiry is replaced by today's current one
assert app.roll_expiries(["03NOV2026", "23NOV2026"], OPEN, NOV4) == ["09NOV2026", "23NOV2026"]
assert app.roll_expiries(["03NOV2026", "09NOV2026", "23NOV2026"], OPEN, NOV4) == ["09NOV2026", "17NOV2026", "23NOV2026"]
assert app.roll_expiries(["03NOV2026"], OPEN, NOV4) == ["09NOV2026"]
print("1 passed expiry 03NOV -> current 09NOV: OK")

# 2. open expiries are left as they are; the expiry day itself is still open
assert app.roll_expiries(["09NOV2026", "23NOV2026"], OPEN, NOV4) == ["09NOV2026", "23NOV2026"]
assert app.roll_expiries(["09NOV2026"], OPEN, date(2026, 11, 9)) == ["09NOV2026"]
assert app.roll_expiries(["03NOV2026"], [], NOV4) == []          # nothing open known: nothing dead watched
print("2 open expiries kept, expiry day still watched: OK")

# 3. prepare() subscribes the 09NOV contracts when asked for 03NOV (the 4 Nov morning start)
rows = []
tok = 1000
for exp in ["03NOV2026"] + OPEN:
    for k in range(23500, 24001, 50):
        for ot in ("CE", "PE"):
            tok += 1
            rows.append(dict(token=str(tok), symbol=f"NIFTY{exp[:5]}{exp[7:]}{k}{ot}", name="NIFTY",
                             expiry=exp, strike=float(k * 100), exch_seg="NFO",
                             instrumenttype="OPTIDX", optiontype=ot))
app.Session.scrip_df = pd.DataFrame(rows)
app.get_tv_multi_timeframe_pivots = lambda *a, **k: {}
app.load_next_day_plan = lambda *a, **k: None
app.get_symbol_spot_token = lambda *a, **k: (None, None)

class _Day(app.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 4, 9, 1)
app.datetime = _Day

feed = app.FeedEngine.__new__(app.FeedEngine)
feed.lock = threading.RLock()
feed.spot_price = {"NIFTY": 23750.0}
feed.daily_focus, feed._focus_date, feed.mtf_pivots = {}, "", {}
feed.next_day_plan = {}
feed.load_or_fetch_strike_pivots = lambda *a, **k: None
feed.prepare("NFO", "NIFTY", ["03NOV2026", "23NOV2026"], 2)
watched = {m["expiry"] for m in feed.token_to_meta.values()}
assert watched == {"09NOV2026", "23NOV2026"}, watched
assert any(m["expiry"] == "09NOV2026" and m["strike"] == 23750 and m["opt_type"] == "PE"
           for m in feed.token_to_meta.values())
assert feed._prep_args[2] == ["09NOV2026", "23NOV2026"]
print("3 prepare(03NOV + 23NOV) on 4 Nov watches 23750 PE 09NOV: OK")

# 4. app kept running from 3 Nov: the new day sees the passed expiry and re-subscribes
feed._prep_args = ("NFO", "NIFTY", ["03NOV2026", "09NOV2026"], 2)
assert feed.loaded_expiry_passed(NOV4) is True
assert feed.loaded_expiry_passed(date(2026, 11, 3)) is False
feed._prep_args = ("NFO", "NIFTY", ["09NOV2026", "23NOV2026"], 2)
assert feed.loaded_expiry_passed(NOV4) is False
print("4 running past the expiry day triggers a re-subscribe: OK")
print("ALL OK")
