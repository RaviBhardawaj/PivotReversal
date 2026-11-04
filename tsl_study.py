"""TSL study: per-trade record of how far each forward-test trade went before it closed.

Data only. Nothing here changes an SL, a target or an order. The live app keeps its fixed SL
(TRAIL_SL = False); this module only watches the same prices and writes down:

  * which targets T1..T5 were reached, at what time and price
  * the best price of the trade (peak for BUY, trough for SELL), when it came, MFE / MAE points
  * the outcome, with "Target then SL" flagged: a target was reached first, then the trade
    came back and closed at the original SL
  * what three candidate trailing-SL techniques WOULD have made on the same ticks
    (shadow exits, never sent to the broker):
        BE@T1     : once T1 is reached, stop moves to the entry price
        STEP      : T1 -> entry, T2 -> T1, T3 -> T2, T4 -> T3
        LOCK50    : once T1 is reached, stop locks 50% of the best move so far

Rows are kept in tsl_study.csv next to forward_test_trades.csv (one row per Trade_ID) and
mirrored to the "TSL Study" sheet tab. Closed trades from before this was added are filled
in from forward_test_trades.csv (no tick times there, so their times are blank and the
shadow results are estimated from the trade's high / low; the Data column says so).

Backfill by hand:  python3 tsl_study.py [forward_test_trades.csv]
"""
import csv
import os
import sys
from datetime import datetime

TECHNIQUES = (
    ("BE", "BE@T1 PnL"),
    ("STEP", "Step Trail PnL"),
    ("LOCK50", "Lock 50% PnL"),
)

HEADERS = ['Trade_ID', 'Date', 'Entry_Time', 'Symbol', 'Type', 'Strike', 'Expiry', 'Action',
           'Entry', 'SL', 'T1', 'T2', 'T3', 'T4', 'T5', 'Max Target',
           'T1 Time', 'T2 Time', 'T3 Time', 'T4 Time', 'T5 Time',
           'Peak Px', 'Peak Time', 'MFE Pts', 'MAE Pts',
           'Exit Time', 'Exit Px', 'Exit Reason', 'Actual PnL', 'Giveback Pts', 'Outcome'] \
          + [h for _, h in TECHNIQUES] + ['Data']

TARGET_THEN_SL = "🎯→🛑 Target then SL"
LIVE_DATA = "live ticks"
HISTORY_DATA = "from trade log (no tick times, TSL estimated)"
REPLAY_DATA = "1-min candles (app restarted)"


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _targets(t):
    """T1..T5; a level on the wrong side of the entry (bad old rows) counts as no target (0)."""
    entry = _f(t.get('Entry_Price'))
    is_buy = t.get('Action') == 'BUY'
    out = []
    for k in range(1, 6):
        lvl = _f(t.get(f'Target_{k}', 0))
        out.append(lvl if lvl > 0 and ((lvl > entry) if is_buy else (lvl < entry)) else 0.0)
    return out


def _entry_sl(t):
    return _f(t.get('Initial_SL', 0)) or _f(t.get('SL', 0))


def _gain(is_buy, entry, px):
    return (px - entry) if is_buy else (entry - px)


def _shadow_stop(name, is_buy, entry, targets, k, peak):
    """Stop price of one technique once k targets are reached (None = still the original SL)."""
    if k < 1:
        return None
    if name == "BE":
        return entry
    if name == "STEP":
        return entry if k == 1 else targets[k - 2]
    if name == "LOCK50":
        return entry + 0.5 * (peak - entry)     # works for both sides: peak is the best price
    return None


def note_tick(trade, ltp, now=None, source=LIVE_DATA, fill_at_stop=False):
    """Called on every price of an open trade, BEFORE its exit check. Records target times,
    the peak and the shadow trailing-SL exits on the trade dict (keys start with 'TSL_').
    fill_at_stop: the price is a candle point reached inside the minute (1-min replay), so a
    shadow stop crossed on the way fills at the stop level, like the replay's real SL."""
    ltp = _f(ltp)
    if ltp <= 0:
        return
    now = now or datetime.now()
    tstr = now.strftime('%H:%M:%S')
    is_buy = trade.get('Action') == 'BUY'
    entry = _f(trade.get('Entry_Price'))
    if entry <= 0:
        return
    targets = _targets(trade)

    if 'TSL_Peak' not in trade:      # a trade opened before this was added: start from its range
        trade['TSL_Peak'] = round(_f(trade.get('Trade_High' if is_buy else 'Trade_Low'), entry) or entry, 2)
        trade['TSL_Peak_Prev'] = trade['TSL_Peak']
    peak = _f(trade.get('TSL_Peak'), entry)
    if _gain(is_buy, entry, ltp) > _gain(is_buy, entry, peak):
        peak = ltp
        trade['TSL_Peak'] = round(ltp, 2)
        trade['TSL_Peak_Time'] = tstr
    else:
        trade.setdefault('TSL_Peak', round(peak, 2))
    worst = _f(trade.get('TSL_Worst'), entry)
    if _gain(is_buy, entry, ltp) < _gain(is_buy, entry, worst):
        trade['TSL_Worst'] = round(ltp, 2)

    # Shadow exits are checked with the stops as they stood before this price moved them,
    # the same order a real stop works in.
    k_before = int(_f(trade.get('TSL_Max_Target', 0)))
    peak_before = _f(trade.get('TSL_Peak_Prev'), entry)
    for name, _ in TECHNIQUES:
        if trade.get(f'TSL_{name}_Exit') not in (None, ''):
            continue
        stop = _shadow_stop(name, is_buy, entry, targets, k_before, peak_before)
        if stop is not None and ((ltp <= stop) if is_buy else (ltp >= stop)):
            trade[f'TSL_{name}_Exit'] = round(stop if fill_at_stop else ltp, 2)
            trade[f'TSL_{name}_Time'] = tstr

    k = k_before
    for i, lvl in enumerate(targets, start=1):
        if lvl > 0 and ((ltp >= lvl) if is_buy else (ltp <= lvl)):
            if not trade.get(f'TSL_T{i}_Time'):
                trade[f'TSL_T{i}_Time'] = tstr
                trade[f'TSL_T{i}_Px'] = round(ltp, 2)
            k = max(k, i)
    trade['TSL_Max_Target'] = k
    trade['TSL_Peak_Prev'] = round(peak, 2)
    trade['TSL_Data'] = source


def _max_target_from_range(t, is_buy, targets):
    best = _f(t.get('Trade_High' if is_buy else 'Trade_Low'))
    k = 0
    for i, lvl in enumerate(targets, start=1):
        if lvl > 0 and best > 0 and ((best >= lvl) if is_buy else (best <= lvl)):
            k = i
    return k


def outcome(reason, k, pnl):
    r = str(reason or '')
    if 'SL Hit' in r:
        return TARGET_THEN_SL if k >= 1 else "🛑 SL (no target)"
    if 'Target 5' in r:
        return "🏁 T5 hit"
    if 'EOD' in r or 'Square-off' in r:
        if k >= 1 and pnl < 0:
            return f"🎯→🔻 T{k} then EOD loss"
        return f"⏰ EOD after T{k}" if k >= 1 else "⏰ EOD (no target)"
    if not r or r == 'ACTIVE':
        return "🟡 Open"
    return f"✋ {r}" + (f" after T{k}" if k >= 1 else "")


def study_row(t):
    """One TSL-study row (list, HEADERS order) for a closed trade dict."""
    is_buy = t.get('Action') == 'BUY'
    entry = _f(t.get('Entry_Price'))
    targets = _targets(t)
    live = bool(t.get('TSL_Data')) and t.get('TSL_Data') != HISTORY_DATA
    exit_px = _f(t.get('Exit_Price'))
    pnl = round(_f(t.get('PnL_Pts')), 2)

    if live:
        k = int(_f(t.get('TSL_Max_Target', 0)))
        peak = _f(t.get('TSL_Peak'), entry)
        worst = _f(t.get('TSL_Worst'), entry)
    else:
        k = _max_target_from_range(t, is_buy, targets)
        peak = _f(t.get('Trade_High' if is_buy else 'Trade_Low'), entry) or entry
        worst = _f(t.get('Trade_Low' if is_buy else 'Trade_High'), entry) or entry
    # the exit price itself can be the best / worst price of the trade
    if exit_px > 0:
        if _gain(is_buy, entry, exit_px) > _gain(is_buy, entry, peak):
            peak = exit_px
        if _gain(is_buy, entry, exit_px) < _gain(is_buy, entry, worst):
            worst = exit_px
    mfe = round(max(0.0, _gain(is_buy, entry, peak)), 2)
    mae = round(min(0.0, _gain(is_buy, entry, worst)), 2)

    shadow = []
    for name, _ in TECHNIQUES:
        if live:
            sx = t.get(f'TSL_{name}_Exit')
            res = _gain(is_buy, entry, _f(sx)) if sx not in (None, '') else pnl
        else:
            # Estimate: the stop for the furthest target reached; if the exit is beyond it,
            # the trade would have closed at that stop instead.
            stop = _shadow_stop(name, is_buy, entry, targets, k, peak)
            if stop is not None and exit_px > 0 and ((exit_px < stop) if is_buy else (exit_px > stop)):
                res = _gain(is_buy, entry, stop)
            else:
                res = pnl
        shadow.append(round(res, 2))

    return [t.get('Trade_ID', ''), t.get('Date', ''), t.get('Entry_Time', ''), t.get('Symbol', ''),
            t.get('Type', ''), t.get('Strike', ''), t.get('Expiry', '') or _expiry(t.get('Trade_ID')),
            t.get('Action', ''), round(entry, 2), round(_entry_sl(t), 2)] \
        + [round(x, 2) for x in targets] + [k] \
        + [t.get(f'TSL_T{i}_Time', '') for i in range(1, 6)] \
        + [round(peak, 2), t.get('TSL_Peak_Time', ''), mfe, mae,
           t.get('Exit_Time', ''), round(exit_px, 2) if exit_px else '', t.get('Exit_Reason', ''),
           pnl, round(mfe - pnl, 2), outcome(t.get('Exit_Reason'), k, pnl)] \
        + shadow + [t.get('TSL_Data') if live else HISTORY_DATA]


def _expiry(trade_id):
    import re
    m = re.search(r"_(\d{2}[A-Z]{3}\d{4})_", str(trade_id or ''))
    return m.group(1) if m else ""


def summary(rows):
    """Totals for the sheet banner: trades, Target-then-SL count / points, actual vs each technique."""
    i_out, i_pnl = HEADERS.index('Outcome'), HEADERS.index('Actual PnL')
    tts = [r for r in rows if r[i_out] == TARGET_THEN_SL]
    out = {"trades": len(rows), "target_then_sl": len(tts),
           "target_then_sl_pts": round(sum(_f(r[i_pnl]) for r in tts), 2),
           "actual": round(sum(_f(r[i_pnl]) for r in rows), 2)}
    for name, h in TECHNIQUES:
        out[name] = round(sum(_f(r[HEADERS.index(h)]) for r in rows), 2)
    return out


def banner(rows):
    s = summary(rows)
    parts = [f"🧪 TSL STUDY (data only, no trailing SL in use) | Trades: {s['trades']}",
             f"{TARGET_THEN_SL}: {s['target_then_sl']} ({s['target_then_sl_pts']:+.2f} pts)",
             f"Actual (fixed SL): {s['actual']:+.2f}"]
    parts += [f"{h.replace(' PnL', '')}: {s[n]:+.2f}" for n, h in TECHNIQUES]
    return "  |  ".join(parts)


class TslJournal:
    """tsl_study.csv: one row per closed trade, upserted by Trade_ID."""

    def __init__(self, filepath):
        self.filepath = filepath

    def _read(self):
        try:
            with open(self.filepath, newline='') as f:
                r = csv.reader(f)
                head = next(r, None)
                if head != HEADERS:
                    return {}
                return {row[0]: row for row in r if row}
        except Exception:
            return {}

    def _write(self, rows):
        tmp = self.filepath + ".tmp"
        with open(tmp, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(HEADERS)
            w.writerows(rows.values())
        os.replace(tmp, self.filepath)

    def record(self, trade):
        """Write (or replace) the row of one closed trade. Never raises."""
        try:
            if str(trade.get('Status', '')).strip() != 'CLOSED' or not trade.get('Trade_ID'):
                return None
            rows = self._read()
            row = study_row(trade)
            old = rows.get(trade['Trade_ID'])
            if old and old[-1] != HISTORY_DATA and row[-1] == HISTORY_DATA:
                return old            # keep the tick-level record over an estimate
            rows[trade['Trade_ID']] = [str(x) for x in row]
            self._write(rows)
            return row
        except Exception as e:
            print(f"[TSL STUDY] record note: {e}")
            return None

    def backfill(self, trade_log):
        """Add every CLOSED trade of forward_test_trades.csv that has no row yet. Returns count."""
        try:
            with open(trade_log, newline='') as f:
                closed = [r for r in csv.DictReader(f) if str(r.get('Status', '')).strip() == 'CLOSED']
        except Exception:
            return 0
        rows = self._read()
        n = 0
        for r in closed:
            if r.get('Trade_ID') and r['Trade_ID'] not in rows:
                rows[r['Trade_ID']] = [str(x) for x in study_row(r)]
                n += 1
        if n:
            try:
                self._write(rows)
            except Exception as e:
                print(f"[TSL STUDY] backfill note: {e}")
                return 0
        return n

    def rows(self):
        """All rows, newest first (Date, Entry_Time)."""
        return sorted(self._read().values(), key=lambda r: (r[1], r[2]), reverse=True)


def journal_path(trade_log):
    return os.path.join(os.path.dirname(os.path.abspath(trade_log)), "tsl_study.csv")


if __name__ == "__main__":
    log = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             "forward_test_trades.csv")
    j = TslJournal(journal_path(log))
    added = j.backfill(log)
    rows = j.rows()
    print(f"{added} trade(s) added from {log} -> {j.filepath}")
    print(banner(rows))
