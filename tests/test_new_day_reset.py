"""New day while the app keeps running (AJ, 27 Oct): yesterday's closed trade on a strike
(e.g. 22700 PE EOD 3PM square-off) must not show on that strike the next morning.
Run: python3 tests/test_new_day_reset.py"""
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
import tempfile

tmp = tempfile.mkdtemp()
tracker = app.ForwardTestTracker(filepath=os.path.join(tmp, "fwd.csv"))
y, today = "2026-10-26", "2026-10-27"
tracker._closed_today = [dict(Date=y, Trade_ID="old", Action="BUY", Type="PE", Strike=22700, Expiry="27OCT2026",
                              Exit_Reason="EOD 3PM Square-off @ CMP"),
                         dict(Date=today, Trade_ID="new", Action="BUY", Type="CE", Strike=22800, Expiry="27OCT2026")]
tracker._closed_keys_today = {("PE", 22700, "BUY", y), ("CE", 22800, "BUY", today)}
tracker._sl_hit_count = {("PE", 22700, "BUY", y): 1}
tracker._seed_closed_cache = list(tracker._closed_today)

feed = app.FeedEngine.__new__(app.FeedEngine)
import threading
feed.lock = threading.RLock()
feed.forward_tracker = tracker
done = app.StrategyStrikeState("PE", 22700, expiry="27OCT2026", mode="BUY")
done.seed_from_closed(tracker._closed_today[0])
live = app.StrategyStrikeState("CE", 23000, expiry="27OCT2026", mode="BUY")
live.trade_status = "Active"
feed.strategy_states = {("PE", 22700, "27OCT2026", "BUY"): done, ("PE", 22700, "27OCT2026"): done,
                        ("CE", 23000, "27OCT2026", "BUY"): live}

# 1. first loop of the process: nothing cleared
assert feed.roll_day(y) is False and len(feed.strategy_states) == 3
assert feed.roll_day(y) is False
print("1 same day: strike states kept: OK")

# 2. date changes while running: yesterday's closed strike state and cache are gone, open trade kept
assert feed.roll_day(today) is True
assert ("PE", 22700, "27OCT2026", "BUY") not in feed.strategy_states
assert ("PE", 22700, "27OCT2026") not in feed.strategy_states
assert feed.strategy_states[("CE", 23000, "27OCT2026", "BUY")] is live
assert [c["Trade_ID"] for c in tracker._closed_today] == ["new"]
assert tracker._seed_closed_cache is None
assert tracker._closed_keys_today == {("CE", 22800, "BUY", today)} and tracker._sl_hit_count == {}
print("2 new day: 22700 PE EOD square-off cleared, open trade kept: OK")

# 3. a fresh state for 22700 PE today shows no trade
st = app.StrategyStrikeState("PE", 22700, expiry="27OCT2026", mode="BUY")
tracker.seed_state(st, "NIFTY")
assert st.trade_status != done.trade_status and st.entry_price == 0, (st.trade_status, st.entry_price)
print("3 22700 PE starts today with no entry/SL from yesterday: OK")
