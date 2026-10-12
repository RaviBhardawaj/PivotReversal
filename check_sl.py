"""Checks each of today's trades' SL against the rule: SL = LL of the sweep the entry came from
(HH for a SELL). Uses Dhan 1-minute candles. Places no orders.
Writes sl_check_<date>.csv next to this file. Run: python3 check_sl.py [YYYY-MM-DD]"""
import csv, json, os, re, sys
from datetime import datetime
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
load_dotenv(os.path.join(HERE, ".env"))
from brokers.dhan import DhanAdapter

day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
trades = [r for r in csv.DictReader(open(os.path.join(HERE, "forward_test_trades.csv"))) if r["Date"] == day]
if not trades:
    sys.exit(f"No trades on {day}")
levels = {}
try:
    for v in json.load(open(os.path.join(HERE, "strike_pivot_cache.json")))["strikes"].values():
        levels[(v["opt_type"], int(v["strike"]), v["expiry"])] = v
except Exception as e:
    print(f"levels file unreadable: {e}")

ad = DhanAdapter({k: (os.getenv(k, "") or "").strip() for k in DhanAdapter.env_keys}, log=lambda *a: None, cache_dir=HERE)
ad.login()
out, cache = [], {}
for t in trades:
    exp = (re.search(r"_(\d{2}[A-Z]{3}\d{4})_", t["Trade_ID"]) or [None, ""])[1]
    typ, stk, act = t["Type"], int(float(t["Strike"])), t["Action"]
    key = (typ, stk, exp)
    if key not in cache:
        inst = ad.resolve({"symbol": t["Symbol"], "expiry": exp, "strike": stk, "opt_type": typ,
                           "exchange": "BFO" if t["Symbol"] in ("SENSEX", "BANKEX") else "NFO",
                           "token": (levels.get(key) or {}).get("token", ""), "tsym": f"{typ} {stk} {exp}"})
        cache[key] = ad.intraday_bars(inst["inst_id"], day, inst["segment"])
    bars = [b for b in cache[key] if b[0].strftime("%H:%M:%S") <= t["Entry_Time"]]
    lv = levels.get(key, {})
    if act == "BUY":
        level = lv.get("S1", 0) if lv.get("S2", 0) <= 0.05 else lv.get("S2", 0)
        day_x = min((b[3] for b in bars), default=0)
        # the last stretch of candles that traded below the level before the entry = the sweep
        i = len(bars) - 1
        while i >= 0 and bars[i][3] > level: i -= 1
        j = i
        while j >= 0 and bars[j][3] <= level * 1.015: j -= 1
        seg = bars[j + 1:i + 1]
        rule = min((b[3] for b in seg), default=0)
        when = min(seg, key=lambda b: b[3])[0].strftime("%H:%M") if seg else ""
    else:
        level = lv.get("R2", 0)
        day_x = max((b[2] for b in bars), default=0)
        i = len(bars) - 1
        while i >= 0 and bars[i][2] < level: i -= 1
        j = i
        while j >= 0 and bars[j][2] >= level * 0.985: j -= 1
        seg = bars[j + 1:i + 1]
        rule = max((b[2] for b in seg), default=0)
        when = max(seg, key=lambda b: b[2])[0].strftime("%H:%M") if seg else ""
    sl = float(t["SL"])
    ok = rule > 0 and abs(sl - rule) <= max(0.1, rule * 0.01)
    row = {"Entry_Time": t["Entry_Time"], "Trade": f"{act} {typ} {stk} {exp}", "Entry": t["Entry_Price"],
           "Level": round(level, 2), "SL_used": sl, "Sweep_LL_HH": round(rule, 2), "Sweep_time": when,
           "Day_low_high_before_entry": round(day_x, 2), "Match": "OK" if ok else "DIFFERENT"}
    out.append(row)
    print(f"{row['Entry_Time']} {row['Trade']:<24} entry {row['Entry']:>7} level {row['Level']:>7} "
          f"SL {sl:>7} | sweep {'LL' if act=='BUY' else 'HH'} {row['Sweep_LL_HH']:>7} at {when:<5} | "
          f"day {'low' if act=='BUY' else 'high'} {row['Day_low_high_before_entry']:>7} | {row['Match']}")
path = os.path.join(HERE, f"sl_check_{day}.csv")
with open(path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
print(f"\nSaved {path}")
