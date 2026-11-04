"""Run the real Tk launcher window with made-up NIFTY data (no Angel login, no orders) and the
glass dashboard on http://127.0.0.1:8780, so the dashboard can be tried without the market.
Needs a display (on Linux: xvfb-run). Run: python3 tests/run_glass_demo.py"""
import os, sys, time, threading, importlib.util
import pandas as pd

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BASE)
os.environ.setdefault("GLASS_DASHBOARD_PORT", "8780")
spec = importlib.util.spec_from_file_location("launcher", os.path.join(BASE, "Options-angel_live-ok.py"))
m = importlib.util.module_from_spec(spec); sys.modules["launcher"] = m; spec.loader.exec_module(m)

EXP, NXT = "06NOV2026", "13NOV2026"
rows = []
for e in (EXP, NXT):
    for k in range(25000, 26300, 50):
        for ot in ("CE", "PE"):
            rows.append({"name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": e,
                         "strike": str(k * 100), "token": f"{e}{k}{ot}", "symbol": f"NIFTY{e}{k}{ot}"})
m.Session.scrip_df = pd.DataFrame(rows)

feed = m.feed
mtf = {
    "Weekly": m.calc_fib_pivots(25900, 25270, 25580), "Daily": m.calc_fib_pivots(25790, 25430, 25600),
    "1-Hour": m.calc_fib_pivots(25660, 25540, 25590), "15-Min": m.calc_fib_pivots(25640, 25590, 25612),
}
focus = m.select_daily_4_focus_strikes(mtf, 50)
spot = 25612.40
with feed.lock:
    feed.spot_price["NIFTY"] = spot
    feed.spot_zone["NIFTY"] = m.get_pivot_zone(spot, mtf["Daily"])
    feed.market_direction["NIFTY"] = m.get_market_direction(spot, mtf["Daily"])
    feed.h1_bias["NIFTY"] = m.get_1h_trade_bias(spot, mtf["1-Hour"])
    feed.mtf_pivots = mtf
    feed.daily_focus = focus
    feed.focus_15m, feed.focus_1h = focus["15M"], focus["1H"]
    for e in (EXP, NXT):
        for k in sorted(set(focus["15M"]["all_4"]) | set(focus["1H"]["all_4"])):
            for ot in ("CE", "PE"):
                base = max(5.0, (k - spot if ot == "PE" else spot - k) + 90 + (20 if e == NXT else 0))
                pv = m.calc_fib_pivots(base * 1.35, base * 0.7, base)
                pv.update({"prev_c": base, "prev_h": base * 1.35, "prev_l": base * 0.7, "oi": 4_000_000, "vol": 15_000_000,
                           "opt_type": ot, "strike": k, "expiry": e})
                feed.strike_pivots[(ot, k, e)] = pv
    f15 = focus["15M"]
    pe = f15["PE1"]
    feed.s1_s2_watchlist = [
        {"side": "BUY", "type": "PE", "strike": pe, "expiry": EXP, "ltp": 92.4, "oi": 4820000, "vol": 19000000,
         "s3": 60, "s2": 84.7, "s1": 99.15, "p": 117.6, "r1": 136, "r2": 150.5, "r3": 170,
         "m5_state": "TRADING_IN_FAVOR_BUY", "trade_status": "Target 1 Hit", "status": "S2 reclaim + retest high break",
         "ll": 81.1, "hh": 96.35, "sl": 86.2, "entry_price": 86.2, "targets": [99.15, 117.6, 136, 150.5, 170],
         "plan_s2": 84.7, "plan_s1": 99.15, "plan_p": 117.6, "plan_r2": 150.5},
        {"side": "BUY", "type": "CE", "strike": f15["CE1"], "expiry": EXP, "ltp": 67.9, "oi": 7210000, "vol": 31000000,
         "s3": 30, "s2": 51.3, "s1": 62.8, "p": 81.4, "r1": 100, "r2": 111.5, "r3": 130,
         "m5_state": "RETEST_S2", "trade_status": "Pending", "status": "🔄 Retest S2: waiting break of 62.10",
         "ll": 48.55, "hh": 62.1, "sl": 0, "entry_price": 0, "targets": [],
         "plan_s2": 51.3, "plan_s1": 62.8, "plan_p": 81.4, "plan_r2": 111.5},
    ]

# a pretend feed so START / STOP can be tried
def _prepare(exch, sym, exps, n):
    return pd.DataFrame({"strike": [str(k * 100) for k in range(25400, 25850, 50)]}), 25600, 50
def _loop(sym, strikes, status_cb=None):
    while feed.running:
        with feed.lock:
            feed.spot_price["NIFTY"] = round(feed.spot_price["NIFTY"] + 0.35, 2)
        time.sleep(1)
feed.prepare, feed.setup_sheet, feed.start_ws, feed.stop_ws = _prepare, (lambda *a, **k: None), (lambda: None), (lambda: None)
feed.update_sheet_loop = _loop
feed.fetch_and_map_all_strikes = lambda *a, **k: {f"{ot}{k}{e}": v for (ot, k, e), v in feed.strike_pivots.items()}
feed.sync_forward_test_to_sheet = lambda *a, **k: None
m.live_order_manager.engine.connect = lambda *a, **k: (False, "Demo: no broker")

import tkinter as tk
root = tk.Tk()
app = m.ControlPanel(root)
app.expiry_var.set(EXP); app.next_exp_var.set(NXT)
import glass_dashboard
srv = glass_dashboard.start(app, m)
if os.getenv("GLASS_DEMO_SECONDS"):
    root.after(int(os.environ["GLASS_DEMO_SECONDS"]) * 1000, root.destroy)
root.mainloop()
