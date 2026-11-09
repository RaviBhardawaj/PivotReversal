"""Strikes deleted on the dashboard stay deleted (file, restart, refresh) until REGENERATE, and
the strategy takes no new entry on them. Run: python3 tests/test_deleted_strikes.py"""
import os, sys, tempfile, importlib, importlib.util, json
from datetime import datetime

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BASE)
import deleted_strikes as ds
W = tempfile.mkdtemp()
ds.DELETED_FILE = os.path.join(W, "deleted_strikes.json")

MON_10 = datetime(2026, 11, 9, 10, 0)      # Monday, in session
MON_16 = datetime(2026, 11, 9, 16, 0)      # after the close -> Tuesday's session
FRI_16 = datetime(2026, 11, 13, 16, 0)
assert ds.session_date(MON_10) == "2026-11-09" and ds.session_date(MON_16) == "2026-11-10"
assert ds.session_date(FRI_16) == "2026-11-16"
print("A session date: OK")

r = ds.delete([{"symbol": "nifty", "expiry": "09nov2026", "strike": "25050", "type": "ce"},
               {"symbol": "NIFTY", "expiry": "09NOV2026", "strike": "🔷25050", "type": "CE"},     # same one
               {"symbol": "NIFTY", "expiry": "", "strike": 0, "type": "CE"}], now=MON_10)        # invalid
assert r["added"] == 1 and len(r["deleted"]) == 1, r
assert ds.is_deleted("NIFTY", "CE", 25050, "09NOV2026", now=MON_10)
assert ds.is_deleted("NIFTY", "CE", 25050, "", now=MON_10)            # no expiry given: any expiry
assert not ds.is_deleted("NIFTY", "PE", 25050, "09NOV2026", now=MON_10)
assert not ds.is_deleted("NIFTY", "CE", 25050, "17NOV2026", now=MON_10)
assert not ds.is_deleted("BANKNIFTY", "CE", 25050, "09NOV2026", now=MON_10)
print("B delete one contract only: OK")

# survives a restart: a fresh import reads the same file
importlib.reload(ds); ds.DELETED_FILE = os.path.join(W, "deleted_strikes.json")
assert ds.is_deleted("NIFTY", "CE", 25050, "09NOV2026", now=MON_10)
assert json.load(open(ds.DELETED_FILE))["items"][0]["session"] == "2026-11-09"
print("C kept on disk across a restart: OK")

# a later session does not inherit it
assert not ds.is_deleted("NIFTY", "CE", 25050, "09NOV2026", now=MON_16)
print("D only for the session it was made for: OK")

ds.delete([{"symbol": "NIFTY", "expiry": "09NOV2026", "strike": 24650, "type": "PE"}], now=MON_10)
k = ds.key("NIFTY", "09NOV2026", 24650, "PE")
r = ds.restore([k], now=MON_10)
assert r["restored"] == 1 and not ds.is_deleted("NIFTY", "PE", 24650, "09NOV2026", now=MON_10)
assert ds.is_deleted("NIFTY", "CE", 25050, "09NOV2026", now=MON_10)
r = ds.restore(None, now=MON_10)                                      # REGENERATE
assert r["restored"] == 1 and ds.active(MON_10) == []
print("E restore one / regenerate restores all: OK")

# F. the strategy takes no new entry on a deleted strike
import headless_tk
headless_tk.install()
spec = importlib.util.spec_from_file_location("launcher", os.path.join(BASE, "Options-angel_live-ok.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.deleted_strikes.DELETED_FILE = ds.DELETED_FILE
class Clock(datetime):
    @classmethod
    def now(cls, tz=None): return MON_10
m.datetime = Clock
m.is_live_market_open = lambda *a, **k: True
ft = m.ForwardTestTracker(filepath=os.path.join(W, "ft.csv"))
ft._sync_open_to_disk = lambda *a, **k: None
ds.delete([{"symbol": "NIFTY", "expiry": "09NOV2026", "strike": 25050, "type": "CE"}], now=MON_10)
args = dict(entry_price=28.3, sl=14.7, t1=49.6, t2=105.8, timeframe="15M Intraday", expiry="09NOV2026")
assert ft.log_entry("NIFTY", "CE", 25050, "BUY", **args) is None
assert ft.log_entry("NIFTY", "CE", 25100, "BUY", **args) is not None   # other strikes still trade
ds.restore(None, now=MON_10)
assert ft.log_entry("NIFTY", "CE", 25050, "BUY", **args) is not None
print("F no entry on a deleted strike, entries again after regenerate: OK")
print("DELETED STRIKE TESTS PASSED")
