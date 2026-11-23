"""Every 15M/1H focus strike gets a dashboard row even when its levels have not loaded yet
(shown as "Levels pending", no made-up levels), its levels are fetched straight away, and a
strike_pivot_cache.json from another session never stands in for today's levels.
Run: python3 tests/test_focus_rows_without_levels.py"""
import os, sys, json, tempfile, importlib.util
import pandas as pd

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BASE)
import headless_tk
tk = headless_tk.install()
spec = importlib.util.spec_from_file_location("launcher", os.path.join(BASE, "Options-angel_live-ok.py"))
m = importlib.util.module_from_spec(spec); sys.modules["launcher"] = m; spec.loader.exec_module(m)

def check(ok, msg):
    print(("OK   " if ok else "FAIL ") + msg)
    if not ok:
        sys.exit(1)

EXP = "23NOV2026"
tmp = tempfile.mkdtemp()
m.BASE_DIR = tmp                        # dashboard reads BASE_DIR/strike_pivot_cache.json
feed = m.feed
mtf = {"Weekly": m.calc_fib_pivots(25900, 25270, 25580), "Daily": m.calc_fib_pivots(25790, 25430, 25600),
       "1-Hour": m.calc_fib_pivots(26006, 25136, 25571), "15-Min": m.calc_fib_pivots(25940, 25410, 25676)}
focus = m.select_daily_4_focus_strikes(mtf, 50)
f15, f1h = focus["15M"], focus["1H"]
all_focus = sorted(set(f15["all_4"]) | set(f1h["all_4"]))
have = {f15["PE1"]}                     # only one strike has its levels loaded
with feed.lock:
    feed.spot_price["NIFTY"] = 25929.0
    feed.mtf_pivots, feed.daily_focus = mtf, focus
    feed.focus_15m, feed.focus_1h = f15, f1h
    feed.strike_pivots.clear()
    for k in have:
        for ot in ("CE", "PE"):
            pv = m.calc_fib_pivots(120, 60, 90)
            pv.update({"prev_c": 90, "prev_h": 120, "prev_l": 60, "opt_type": ot, "strike": k, "expiry": EXP})
            feed.strike_pivots[(ot, k, EXP)] = pv
    feed.s1_s2_watchlist = []

# a stale cache file (another session) holding levels for every focus strike
stale = {"date": "2026-11-20", "expiry": EXP, "strikes": {
    f"{ot}{k}": {"opt_type": ot, "strike": k, "expiry": EXP, "P": 50, "S1": 40, "S2": 30, "S3": 20,
                 "R1": 60, "R2": 70, "R3": 80, "prev_c": 50} for k in all_focus for ot in ("CE", "PE")}}
json.dump(stale, open(os.path.join(tmp, "strike_pivot_cache.json"), "w"))

fetched = []
feed.fetch_and_map_all_strikes = lambda sym, expiry=None, **k: fetched.append((sym, expiry)) or {}

root = tk.Tk()
app = m.ControlPanel(root)
app.symbol_var.set("NIFTY")
app.expiry_var.set(EXP); app.next_exp_var.set("01DEC2026")
app._refresh_display_body()

cols = list(app.tree["columns"])
rows = [dict(zip(cols, app.tree.item(i, "values"))) for i in app.tree.get_children()]
strikes_shown = {(r[cols[1]], str(r[cols[2]]).replace("🔷", "")) for r in rows}
print("rows:", len(rows), sorted(strikes_shown))
for k in f15["ce_strikes"][:2]:
    check(("CE", str(k)) in strikes_shown, f"15M CE strike {k} listed")
for k in f15["pe_strikes"][:2]:
    check(("PE", str(k)) in strikes_shown, f"15M PE strike {k} listed")
for k in f1h["ce_strikes"][:2]:
    check(("CE", str(k)) in strikes_shown, f"1H CE strike {k} listed")
for k in f1h["pe_strikes"][:2]:
    check(("PE", str(k)) in strikes_shown, f"1H PE strike {k} listed")
for k in f15["p_strikes"] + f1h["p_strikes"]:
    check(("CE", str(k)) in strikes_shown and ("PE", str(k)) in strikes_shown, f"P strike {k} listed on CE and PE")
pend = [r for r in rows if "Levels pending" in " ".join(str(v) for v in r.values())]
check(len(pend) > 0, f"strikes without levels are marked Levels pending ({len(pend)} rows)")
check(all(r.get("S1", "-") in ("-",) for r in pend), "pending rows show no levels (stale cache not used)")
loaded = [r for r in rows if str(r[cols[2]]).replace("🔷", "") == str(f15["PE1"])]
check(loaded and all("Levels pending" not in " ".join(str(v) for v in r.values()) for r in loaded),
      "strike with levels shows them normally")
check(("NIFTY", EXP) in fetched, "missing levels are fetched straight away")
n = len(fetched)
app._refresh_display_body()
check(len(fetched) == n, "fetch is not repeated on every refresh (once a minute)")
print("FOCUS ROWS WITHOUT LEVELS TESTS PASSED")
