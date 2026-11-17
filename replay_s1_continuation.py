"""
Replay: what the S1 continuation BUY adds, on real 1-minute option candles (Dhan).

For every contract in strike_pivot_cache.json (both CE and PE, the expiries the app watches) and
every day asked for, it rebuilds that day's strike levels from the previous session's candles,
then runs the day's 1-minute candles through the app's own BUY state machine twice:
  OLD = S1_CONTINUATION_BUY off (waits for S2), NEW = on.
Exits are the app's: fixed SL at the LL, T5, or the 15:00 square-off; one re-entry after an SL.
Read-only: it never touches the trades CSV, the Sheet or the broker.

Run it from the app folder (needs the Dhan login the app uses):
    python3 replay_s1_continuation.py                  # last 5 trading days
    python3 replay_s1_continuation.py 2026-11-12 2026-11-13 2026-11-14
Writes the result to ~/Desktop/replay_s1_continuation.md
"""
import importlib.util, json, os, sys
from datetime import datetime as _real_dt, timedelta, time as dt_time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("opt", os.path.join(HERE, "Options-angel_live-ok.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SYMBOL = os.getenv("BACKFILL_SYMBOL", "NIFTY")
SQUARE_OFF = dt_time(15, 0)


def trading_days_back(n, until=None):
    d = (until or _real_dt.now()).date()
    if _real_dt.now().time() < dt_time(15, 31) and until is None:
        d -= timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def day_bars(exp, stk, opt, day):
    try:
        bars = m.dhan_minute_bars(SYMBOL, exp, stk, opt, day) or []
    except Exception as e:
        print(f"   {exp} {stk} {opt} {day}: no candles ({e})")
        return []
    return [b for b in bars if dt_time(9, 15) <= b[0].replace(tzinfo=None).time() <= dt_time(15, 30)]


def prev_levels(exp, stk, opt, day, cache):
    """Levels for `day` from the previous session that has candles (up to 6 days back)."""
    d = _real_dt.strptime(day, "%Y-%m-%d").date()
    for _ in range(6):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        key = (exp, stk, opt, d.isoformat())
        if key not in cache:
            cache[key] = day_bars(exp, stk, opt, d.isoformat())
        b = cache[key]
        if b:
            return m.calc_fib_pivots(max(x[2] for x in b), min(x[3] for x in b), b[-1][4])
    return None


def simulate(bars, piv, opt, stk, exp, s1_cont):
    """Run one day through the BUY state machine like the live loop; return the trades."""
    m.S1_CONTINUATION_BUY = s1_cont
    st = m.StrategyStrikeState(opt, stk, expiry=exp, mode="BUY")
    trades, tr = [], None
    dh = dl = 0.0
    for (t, o, h, l, c) in bars:
        tt = t.replace(tzinfo=None)
        dh = max(dh, h)
        dl = l if not dl else min(dl, l)
        for px in ((o, l, h, c) if c >= o else (o, h, l, c)):
            if tr is None:
                was_waiting = st.trade_status == "Waiting"
                st.update_tick(px, piv, tt, opt_high=dh, opt_low=dl)
                if was_waiting and st.trade_status == "Active":
                    t1 = st.targets[0] if st.targets else 0.0
                    ok = (tt.time() < SQUARE_OFF and m.buy_entry_ok(px, st.entry_price, st.sl, t1)
                          and piv["S2"] * 0.95 <= px <= piv["R2"] * 1.05 and st.sl < px)
                    if not ok:
                        st.unconfirm_buy()
                        continue
                    tr = {"contract": f"{exp} {stk} {opt}", "level": st.trigger_level, "time": tt.strftime("%H:%M"),
                          "entry": round(px, 2), "sl": st.sl, "targets": list(st.targets), "best": 0}
                continue
            # open trade: fixed SL, T5, 15:00
            for i, tg in enumerate(tr["targets"], 1):
                if px >= tg:
                    tr["best"] = max(tr["best"], i)
            reason = None
            if px <= tr["sl"]:
                reason = "SL"
            elif tr["best"] >= 5:
                reason = "T5"
            elif tt.time() >= SQUARE_OFF:
                reason = "15:00"
            if reason:
                tr.update(exit=round(px, 2), exit_time=tt.strftime("%H:%M"), result=reason,
                          pts=round(px - tr["entry"], 2))
                trades.append(tr)
                tr = None
                if reason != "SL" or st.sl_hit_count >= 1:
                    return trades                      # target / EOD close, or 2nd SL: done for the day
                st.trade_status = "SL Hit Closed"
                st.state = "WAITING"
                st.sl_hit_count += 1
    if tr:
        last = bars[-1][4]
        tr.update(exit=round(last, 2), exit_time=bars[-1][0].strftime("%H:%M"), result="open",
                  pts=round(last - tr["entry"], 2))
        trades.append(tr)
    return trades


def main():
    days = [a for a in sys.argv[1:] if not a.startswith("--")] or trading_days_back(5)
    cache = json.load(open("strike_pivot_cache.json"))["strikes"]
    contracts = sorted({(s.get("expiry"), int(s["strike"]), s.get("opt_type")) for s in cache.values()
                        if s.get("opt_type") in ("CE", "PE")})
    print(f"Days: {', '.join(days)} | {len(contracts)} contracts")
    bar_cache, rows = {}, {"OLD": [], "NEW": []}
    for day in days:
        for exp, stk, opt in contracts:
            try:
                if _real_dt.strptime(exp.upper(), "%d%b%Y").date() < _real_dt.strptime(day, "%Y-%m-%d").date():
                    continue                                  # contract had expired by then
            except ValueError:
                pass
            piv = prev_levels(exp, stk, opt, day, bar_cache)
            if not piv:
                continue
            bars = bar_cache.setdefault((exp, stk, opt, day), day_bars(exp, stk, opt, day))
            if not bars:
                continue
            for tag, flag in (("OLD", False), ("NEW", True)):
                for tr in simulate(bars, piv, opt, stk, exp, flag):
                    tr["day"] = day
                    rows[tag].append(tr)
        print(f"{day}: OLD {sum(1 for r in rows['OLD'] if r['day'] == day)} trades, "
              f"NEW {sum(1 for r in rows['NEW'] if r['day'] == day)} trades")

    def summary(rs):
        return (len(rs), sum(1 for r in rs if r["pts"] > 0), sum(1 for r in rs if r["best"] >= 1),
                sum(1 for r in rs if r["result"] == "SL"), round(sum(r["pts"] for r in rs), 2))

    key = lambda r: (r["day"], r["contract"], r["time"])
    old_keys = {key(r) for r in rows["OLD"]}
    added = [r for r in rows["NEW"] if key(r) not in old_keys]
    s1_new = [r for r in rows["NEW"] if r["level"] == "S1"]
    lines = ["# S1 continuation BUY: replay", "",
             f"Days {', '.join(days)}; {len(contracts)} contracts from strike_pivot_cache.json; 1-minute Dhan candles.",
             "Exits: fixed SL at LL, T5, or 15:00; one re-entry after an SL.", "",
             "| Rule | Trades | Winners | Reached T1 | SL hits | Points |", "|---|---|---|---|---|---|"]
    for tag in ("OLD", "NEW"):
        lines.append("| %s | %d | %d | %d | %d | %.2f |" % ((tag,) + summary(rows[tag])))
    lines.append("| NEW, S1 continuation trades only | %d | %d | %d | %d | %.2f |" % summary(s1_new))
    lines += ["", "## Trades the S1 rule adds or changes", "",
              "| Day | Contract | Level | Entry time | Entry | SL | T1 | Exit | Result | Pts | Best target |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(added, key=key):
        lines.append(f"| {r['day']} | {r['contract']} | {r['level']} | {r['time']} | {r['entry']:.2f} | {r['sl']:.2f} | "
                     f"{r['targets'][0]:.2f} | {r['exit_time']} | {r['result']} | {r['pts']:+.2f} | T{r['best']} |")
    out = "\n".join(lines) + "\n"
    print("\n" + out)
    dest = os.path.join(os.path.expanduser("~"), "Desktop", "replay_s1_continuation.md")
    try:
        open(dest, "w").write(out)
        print("Saved:", dest)
    except OSError as e:
        print("Could not save the report:", e)


if __name__ == "__main__":
    main()
