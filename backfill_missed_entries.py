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
    python3 backfill_missed_entries.py 2026-11-09 auto            # every 15M/1H strike, CE+PE, CURRENT expiry
    python3 backfill_missed_entries.py 2026-11-09 auto --apply    # only (dry run / for real)
    python3 backfill_missed_entries.py 2026-11-09 auto --next     # also the next expiry (only when asked)
"""
import importlib.util, json, os, shutil, sys, tempfile
from datetime import datetime as _real_dt

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
APPLY = "--apply" in sys.argv
WITH_NEXT = "--next" in sys.argv          # next-expiry trades only when asked for; far expiries never
AUTO = len(args) == 2 and args[1].lower() == "auto"
if len(args) < 3 and not AUTO:
    sys.exit(__doc__)
DAY, OPT = args[0], args[1].upper()
STRIKES = [] if AUTO else [int(x) for x in args[2:]]
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

# trade ids already in the CSV / open snapshot: SYMBOL_TYPE_STRIKE_EXPIRY_YYYYMMDD_...
existing_ids = set()
with open(csv_path, newline="") as f:
    for r in _csv.DictReader(f):
        existing_ids.add(str(r.get("Trade_ID", "")))
for t in list(getattr(ft, "active_trades", {}).values()):
    existing_ids.add(str(t.get("Trade_ID", "")))

def tf_of(stk):
    f15 = f1h = set()
    try:
        foc = json.load(open("multi_symbol_levels.json"))[SYMBOL]["focus"]
        f15 = set(foc.get("15M", {}).get("all_4", []) + foc.get("15M", {}).get("all_6", []))
        f1h = set(foc.get("1H", {}).get("all_4", []) + foc.get("1H", {}).get("all_6", []))
    except Exception:
        pass
    if stk in f15 and stk in f1h: return "15M / 1H"
    if stk in f15: return "15M Intraday"
    if stk in f1h: return "1H Structure"
    return ""

def day_expiries(day, expiries, with_next=False):
    """The current expiry on `day` (nearest one not yet expired), plus the next one if asked."""
    d = _real_dt.strptime(day, "%Y-%m-%d").date()
    def _dt(e):
        try:
            return _real_dt.strptime(str(e).upper(), "%d%b%Y").date()
        except ValueError:
            return None
    open_ = sorted({e for e in expiries if _dt(e) and _dt(e) >= d}, key=_dt)
    return open_[:2] if with_next else open_[:1]

if AUTO:
    ALLOWED = day_expiries(DAY, [s.get("expiry") for s in cache.values()], WITH_NEXT)
    print(f"Expiries replayed: {', '.join(ALLOWED) or 'none'}"
          + ("" if WITH_NEXT else "  (current only; add --next for the next expiry)"))
    ft.set_allowed_expiries(ALLOWED or None)
    jobs = sorted({(s.get("opt_type"), int(s["strike"]), s.get("expiry")) for s in cache.values()
                   if s.get("opt_type") in ("CE", "PE") and tf_of(int(s["strike"]))
                   and s.get("expiry") in ALLOWED})
else:
    jobs = [(OPT, stk, EXPIRY) for stk in STRIKES]
print(f"{len(jobs)} contracts to replay")

for OPT, stk, EXPIRY in jobs:
    TF = tf_of(stk) or "15M Intraday"
    key = (DAY, SYMBOL, OPT, str(stk), EXPIRY, "BUY")
    id_prefix = f"{SYMBOL}_{OPT}_{stk}_{EXPIRY}_{DAY.replace('-', '')}"
    if key in existing or any(i.startswith(id_prefix) for i in existing_ids):
        print(f"{EXPIRY} {stk} {OPT}: a trade already exists for {DAY} - skipped")
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
    entry_px = 0.0
    for i, (t, o, h, l, c) in enumerate(bars):
        tt = t.replace(tzinfo=None)
        dh = max(dh, h)
        dl = l if not dl else min(dl, l)
        for px in ((o, l, h, c) if c >= o else (o, h, l, c)):
            st.update_tick(px, piv, tt, opt_high=dh, opt_low=dl)
            entry_px = px
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
    # same as the live app: recorded at the price that traded when the rule fired
    tr = ft.log_entry(SYMBOL, OPT, stk, "BUY", round(entry_px, 2), st.entry_sl,
                      *st.targets[:5], opt_piv=piv, timeframe=TF,
                      token=str(next((k for k, s in cache.items() if s is piv), "")), exchange="NFO",
                      expiry=EXPIRY)
    if not tr:
        print(f"{stk} {OPT}: tracker refused the entry")
        continue
    tr["Order_ID"] = "BACKFILLED: rule replay of the day's 1-min candles"
    ft._save_snapshot()
    print(f"{EXPIRY} {stk} {OPT} [{TF}]: BACKFILLED BUY @ {round(entry_px, 2)} (conf high {st.entry_price}) at {t_entry:%H:%M} | SL {st.entry_sl} | T {st.targets}")

    for (t, o, h, l, c) in bars[entry_i + 1:]:
        FakeClock._now = t.replace(tzinfo=None)
        for px in ((o, l, h, c) if c >= o else (o, h, l, c)):
            ft.update_tick(OPT, stk, px, EXPIRY, SYMBOL)
        if tr.get("Status") != "ACTIVE":
            break
    print(f"    -> now {tr.get('Status')} | last {tr.get('Current_Price')} | hit T{tr.get('Target_Hit')} | "
          f"{tr.get('Exit_Reason')} | PnL {tr.get('PnL_Pts')} pts")

FakeClock._now = None
print("\nDone." + ("" if APPLY else " (dry run - nothing written to the real files)"))
