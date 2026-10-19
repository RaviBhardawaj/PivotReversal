"""
Backfill classic-scanner BUY entries that the live scanner missed on a given day.

It replays the day's real 1-minute option candles (Dhan) through the SAME state machine the live
scanner uses (StrategyStrikeState, BUY mode, locked strike pivots from strike_pivot_cache.json), and when
the rule fires it records the trade through ForwardTestTracker (same CSV / open-snapshot the sheet is
built from), then replays the candles after the entry through the tracker's own update_tick so SL / targets /
exits are handled by the normal code.

IMPORTANT: run it while the scanner app is STOPPED (the running app rewrites the open-trades snapshot),
then start the app again. Without --apply it works on a scratch copy and changes nothing.

    python3 backfill_missed_entries.py 2026-10-19 PE 23100 23050 23000            # dry run
    python3 backfill_missed_entries.py 2026-10-19 PE 23100 23050 23000 --apply    # write for real
"""
import importlib.util, json, os, shutil, sys, tempfile
from datetime import datetime as _real_dt

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
APPLY = "--apply" in sys.argv
if len(args) < 3:
    sys.exit(__doc__)
DAY, OPT, STRIKES = args[0], args[1].upper(), [int(x) for x in args[2:]]
SYMBOL = os.getenv("BACKFILL_SYMBOL", "NIFTY")
EXPIRY = os.getenv("BACKFILL_EXPIRY", "19OCT2026")

spec = importlib.util.spec_from_file_location("opt", os.path.join(HERE, "Options-angel_live-ok.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeClock(_real_dt):
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now or _real_dt.now(tz)


m.datetime = FakeClock                     # the tracker stamps entry / exit times from this clock

if APPLY:
    csv_path, state_path = m.FORWARD_TEST_FILE, os.path.splitext(m.FORWARD_TEST_FILE)[0] + "_open.json"
else:
    tmp = tempfile.mkdtemp(prefix="backfill_dry_")
    csv_path = os.path.join(tmp, "forward_test_trades.csv")
    for src in (m.FORWARD_TEST_FILE, os.path.splitext(m.FORWARD_TEST_FILE)[0] + "_open.json"):
        if os.path.exists(src):
            shutil.copy(src, os.path.join(tmp, os.path.basename(src)))
    state_path = os.path.splitext(csv_path)[0] + "_open.json"
    print(f"[dry run] working on a scratch copy in {tmp}")

ft = m.ForwardTestTracker(csv_path)
ft.paper_only = True                       # backfill never sends a broker order

cache = json.load(open("strike_pivot_cache.json"))["strikes"]
existing = set()
with open(csv_path, newline="") as f:
    import csv as _csv
    for r in _csv.DictReader(f):
        existing.add((r.get("Date"), r.get("Symbol"), r.get("Type"), str(r.get("Strike")), r.get("Expiry"), r.get("Action")))

for stk in STRIKES:
    key = (DAY, SYMBOL, OPT, str(stk), EXPIRY, "BUY")
    if key in existing:
        print(f"{stk} {OPT}: a BUY trade already exists for {DAY} - skipped")
        continue
    piv = next((s for s in cache.values()
                if s.get("opt_type") == OPT and int(s["strike"]) == stk and s.get("expiry") == EXPIRY), None)
    if not piv:
        print(f"{stk} {OPT}: no strike levels in strike_pivot_cache.json - skipped")
        continue
    bars = m.dhan_minute_bars(SYMBOL, EXPIRY, stk, OPT, DAY)
    if not bars:
        print(f"{stk} {OPT}: no 1-min candles from Dhan - skipped")
        continue

    st = m.StrategyStrikeState(OPT, stk, expiry=EXPIRY, mode="BUY")
    dh = dl = 0.0
    entry_i = None
    for i, (t, o, h, l, c) in enumerate(bars):
        tt = t.replace(tzinfo=None)
        dh = max(dh, h)
        dl = l if not dl else min(dl, l)
        for px in (o, l, h, c):
            st.update_tick(px, piv, tt, opt_high=dh, opt_low=dl)
            if st.state == "CONFIRMED_BUY":
                break
        if st.state == "CONFIRMED_BUY":
            entry_i = i
            break
    if entry_i is None:
        print(f"{stk} {OPT}: rule never fired on {DAY} - no entry")
        continue

    t_entry = bars[entry_i][0].replace(tzinfo=None)
    FakeClock._now = t_entry
    tr = ft.log_entry(SYMBOL, OPT, stk, "BUY", st.entry_price, st.entry_sl,
                      *st.targets[:5], opt_piv=piv, timeframe="15M Intraday",
                      token=str(next((k for k, s in cache.items() if s is piv), "")), exchange="NFO",
                      expiry=EXPIRY)
    if not tr:
        print(f"{stk} {OPT}: tracker refused the entry")
        continue
    tr["Order_ID"] = "BACKFILLED: rule replay of the day's 1-min candles"
    ft._save_snapshot()
    print(f"{stk} {OPT}: BACKFILLED BUY @ {st.entry_price} at {t_entry:%H:%M} | SL {st.entry_sl} | T {st.targets}")

    for (t, o, h, l, c) in bars[entry_i + 1:]:
        FakeClock._now = t.replace(tzinfo=None)
        for px in (o, l, h, c):
            ft.update_tick(OPT, stk, px, EXPIRY, SYMBOL)
        if tr.get("Status") != "ACTIVE":
            break
    print(f"    -> now {tr.get('Status')} | last {tr.get('Current_Price')} | hit T{tr.get('Target_Hit')} | "
          f"{tr.get('Exit_Reason')} | PnL {tr.get('PnL_Pts')} pts")

FakeClock._now = None
print("\nDone." + ("" if APPLY else " (dry run - nothing written to the real files)"))
