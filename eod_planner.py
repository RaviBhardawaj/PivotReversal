"""
eod_planner.py  —  STANDALONE next-day plan generator
════════════════════════════════════════════════════════
Run SEPARATELY — does NOT touch Options-angel_live-ok.py at all.

Usage:
    python3 eod_planner.py           # generate tomorrow's plan (run after 15:30)
    python3 eod_planner.py --gap     # detect gap vs yesterday's close (run at 09:16)
    python3 eod_planner.py --show    # show today's saved plan
    python3 eod_planner.py --hold    # recalc SL/targets for overnight hold positions
"""

import json, os, sys, glob, requests
from datetime import datetime, date, timedelta

SYMBOL    = "NIFTY"
STEP      = 50
YAHOO_MAP = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}
GAP_THRESH= 0.5
PLAN_DIR  = os.path.dirname(os.path.abspath(__file__))


def calc_fib_pivots(high, low, close):
    p = (high + low + close) / 3
    r = high - low
    return {
        "P": round(p, 2),
        "S1": round(max(p - 0.382 * r, 0.05), 2),
        "S2": round(max(p - 0.618 * r, 0.05), 2),
        "S3": round(max(p - r, 0.05), 2),
        "R1": round(p + 0.382 * r, 2),
        "R2": round(p + 0.618 * r, 2),
        "R3": round(p + r, 2),
    }

def snap(v): return int(round(v / STEP) * STEP) if v else 0

def fetch_candles(symbol=SYMBOL, days=10):
    y = YAHOO_MAP.get(symbol, f"{symbol}.NS")
    s = requests.Session(); s.headers.update({"User-Agent": "Mozilla/5.0"})
    try: s.get("https://fc.yahoo.com", timeout=3)
    except: pass
    for base in ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com"]:
        try:
            r = s.get(f"{base}/v8/finance/chart/{y}?range={days}d&interval=1d", timeout=10)
            r.raise_for_status()
            d = r.json()["chart"]["result"][0]
            q = d["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(d.get("timestamp", [])):
                try:
                    o,h,l,c = q["open"][i],q["high"][i],q["low"][i],q["close"][i]
                    if None in (o,h,l,c) or h<=0: continue
                    out.append({"date": datetime.fromtimestamp(t).date(), "open":o,"high":h,"low":l,"close":c})
                except: continue
            if out: return out
        except: continue
    return []


# ── GENERATE PLAN ────────────────────────────────────────────────────────────
def generate_plan(symbol=SYMBOL):
    candles = fetch_candles(symbol)
    today   = date.today()
    past    = [c for c in candles if c["date"] < today]
    if not past:
        print("[EOD] No historical data — check internet / Yahoo Finance"); return {}

    prev = past[-1]
    pv   = calc_fib_pivots(prev["high"], prev["low"], prev["close"])

    nd = today + timedelta(days=1)
    if nd.weekday() == 5: nd += timedelta(days=2)
    if nd.weekday() == 6: nd += timedelta(days=1)
    next_date = nd.strftime("%Y-%m-%d")

    s2,s1,p,r1,r2 = pv["S2"],pv["S1"],pv["P"],pv["R1"],pv["R2"]
    if s2 <= 0.05:
        buy_entry_txt = f"S1={s1:.0f} (S2<=0) — candle reclaim above S1 | SL = LL below S1"
        buy_tgts_txt  = f"T1=P({p:.0f})  T2=R1({r1:.0f})  T3=R2({r2:.0f})  T4=R3({pv['R3']:.0f})"
        buy_strikes   = sorted({snap(s1)})
    else:
        buy_entry_txt = f"S2={s2:.0f} — candle reclaim above S2 | SL = LL below S2"
        buy_tgts_txt  = f"T1=S1({s1:.0f})  T2=P({p:.0f})  T3=R1({r1:.0f})  T4=R2({r2:.0f})"
        buy_strikes   = sorted({snap(s2), snap(s1)})
    sell_strikes   = sorted({snap(r1), snap(r2)}, reverse=True)

    # ── Map All Option Strikes to TradingView Pivots ──
    print(f"\n[EOD] Mapping strike Fibonacci pivots for {symbol} (TradingView aligned)...")
    strike_pivots = map_strike_pivots_for_symbol(symbol)

    plan = {
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "for_date":      next_date,
        "symbol":        symbol,
        "prev_ohlc":     {k: round(prev[k],2) for k in ("open","high","low","close")},
        "pivots":        pv,
        "strategy": {
            "BUY_entry":    buy_entry_txt,
            "BUY_targets":  buy_tgts_txt,
            "SELL_entry":   f"R2={r2:.0f} — candle rejection below R2 | SL = HH above R2",
            "SELL_targets": f"T1=R1({r1:.0f})  T2=P({p:.0f})  T3=S1({s1:.0f})  T4=S2({s2:.0f})",
        },
        "focus_strikes": {
            "BUY_CE_from_S2":  buy_strikes,  "BUY_PE_from_S2":  buy_strikes,
            "SELL_CE_from_R2": sell_strikes, "SELL_PE_from_R2": sell_strikes,
        },
        "strike_levels": {
            k: {
                "symbol": v["symbol"], "type": v["opt_type"], "strike": v["strike"],
                "H": v["prev_h"], "L": v["prev_l"], "C": v["prev_c"],
                "S2": v["S2"], "S1": v["S1"], "P": v["P"], "R1": v["R1"], "R2": v["R2"], "R3": v["R3"]
            }
            for k, v in strike_pivots.items()
        },
        "note": "Planned levels from yesterday OHLC. Run --gap at 09:16 to check for gap adjustments."
    }

    fname = os.path.join(PLAN_DIR, f"next_day_plan_{next_date.replace('-','')}.json")
    with open(fname, "w") as f: json.dump(plan, f, indent=2)

    _print_plan(plan)
    print(f"  Saved to: {fname}\n")
    return plan


def map_strike_pivots_for_symbol(symbol=SYMBOL, n_strikes=12):
    """
    Connects to Angel One and fetches previous day completed OHLC
    for all strikes in the trading range, computing exact Fibonacci Pivots
    matching TradingView 'Pivots Fibonacci Auto' indicator.
    """
    try:
        import pyotp, pickle, time, threading
        import pandas as pd
        from dotenv import load_dotenv
        from SmartApi import SmartConnect
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        load_dotenv()
        api_key = os.getenv('ANGEL_API_KEY') or os.getenv('API_KEY')
        client_id = os.getenv('ANGEL_CLIENT_ID') or os.getenv('CLIENT_ID')
        mpin = os.getenv('ANGEL_MPIN') or os.getenv('MPIN')
        totp_secret = os.getenv('ANGEL_TOTP_SECRET') or os.getenv('TOTP_SECRET')

        if not all([api_key, client_id, mpin, totp_secret]):
            print("[STRIKE PLAN] Angel One credentials missing in .env")
            return {}

        cache_scrip = os.path.join(PLAN_DIR, "scrip_cache.pkl")
        if not os.path.exists(cache_scrip):
            print("[STRIKE PLAN] scrip_cache.pkl not found")
            return {}

        with open(cache_scrip, "rb") as f:
            scrip_df = pickle.load(f)

        exch = "BFO" if symbol in ("SENSEX", "BANKEX") else "NFO"
        itype = "OPTIDX"
        opts = scrip_df[(scrip_df['name']==symbol) & (scrip_df['exch_seg']==exch) & (scrip_df['instrumenttype']==itype)].copy()
        opts['exp_dt'] = pd.to_datetime(opts['expiry'], format='%d%b%Y')
        now_ts = pd.Timestamp.now()
        fut_exp = opts[opts['exp_dt'] >= now_ts.floor('D')]
        if fut_exp.empty:
            return {}
        nearest_exp = fut_exp.sort_values('exp_dt').iloc[0]['expiry']

        sub = opts[opts['expiry'] == nearest_exp].copy()
        sub['strike_int'] = (sub['strike'].astype(float) / 100).astype(int)

        candles = fetch_candles(symbol, days=5)
        spot_close = candles[-1]["close"] if candles else 22500
        step = 50 if symbol == "NIFTY" else 100
        atm = int(round(spot_close / step) * step)

        min_s = atm - n_strikes * step
        max_s = atm + n_strikes * step
        focus_df = sub[(sub['strike_int'] >= min_s) & (sub['strike_int'] <= max_s)].copy()

        tokens = focus_df['token'].astype(str).tolist()
        meta_map = {}
        for _, r in focus_df.iterrows():
            meta_map[str(r['token'])] = {
                'token': str(r['token']),
                'symbol': r['symbol'],
                'strike': r['strike_int'],
                'opt_type': 'CE' if r['symbol'].endswith('CE') else 'PE',
                'expiry': nearest_exp
            }

        totp = pyotp.TOTP(totp_secret).now()
        sc = SmartConnect(api_key=api_key)
        sess = sc.generateSession(client_id, mpin, totp)
        if not sess.get("status"):
            print(f"[STRIKE PLAN] Angel login failed: {sess.get('message')}")
            return {}
        # Next session's levels from each contract's official daily candle (the last completed
        # one up to today), the same data TradingView uses. Never from a snapshot / LTP.
        now = datetime.now()
        if now.weekday() < 5 and now.hour * 60 + now.minute < 15 * 60 + 30:
            print("[STRIKE PLAN] Today's candle is not complete before 15:30 — strike levels skipped")
            return {}
        today_s = now.strftime("%Y-%m-%d")
        mapped_pivots, failed = {}, 0
        for tok in tokens:
            m = meta_map[tok]
            rows = []
            for attempt in range(3):
                try:
                    r = sc.getCandleData({"exchange": exch, "symboltoken": tok, "interval": "ONE_DAY",
                                          "fromdate": (now - timedelta(days=20)).strftime("%Y-%m-%d 09:15"),
                                          "todate": now.strftime("%Y-%m-%d %H:%M")})
                    if r and r.get("status") and isinstance(r.get("data"), list):
                        rows = [x for x in r["data"] if str(x[0])[:10] <= today_s and float(x[2]) > 0 and float(x[3]) > 0 and float(x[4]) > 0]
                        break
                except Exception:
                    pass
                time.sleep(1 + attempt)
            time.sleep(0.35)   # SmartAPI historical rate limit
            if not rows:
                failed += 1
                continue
            last = sorted(rows, key=lambda x: str(x[0]))[-1]
            h, l, c = float(last[2]), float(last[3]), float(last[4])
            pv = calc_fib_pivots(h, l, c)
            mapped_pivots[tok] = {
                'token': tok,
                'symbol': m['symbol'],
                'opt_type': m['opt_type'],
                'strike': int(m['strike']),
                'expiry': m['expiry'],
                'candle_date': str(last[0])[:10],
                'prev_h': round(h, 2),
                'prev_l': round(l, 2),
                'prev_c': round(c, 2),
                **pv
            }

        nd = date.today() + timedelta(days=1)
        if nd.weekday() == 5: nd += timedelta(days=2)
        if nd.weekday() == 6: nd += timedelta(days=1)
        next_date = nd.strftime('%Y-%m-%d')

        cache_data = {
            'date': next_date,
            'basis_key': f"{next_date}:next",
            'level_date': next_date,
            'symbol': symbol,
            'expiry': nearest_exp,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'strikes': mapped_pivots
        }
        # Next-session levels have their own file: the live session's strike_pivot_cache.json
        # (what TradingView shows today) is never overwritten from here.
        cache_f = os.path.join(PLAN_DIR, "strike_pivot_cache_next.json")
        with open(cache_f + ".tmp", "w") as f:
            json.dump(cache_data, f, indent=2)
        os.replace(cache_f + ".tmp", cache_f)
        print(f"  [✓] Mapped {len(mapped_pivots)} strikes for {next_date} to strike_pivot_cache_next.json"
              + (f" ({failed} without candle data)" if failed else ""))
        return mapped_pivots
    except Exception as e:
        print(f"[STRIKE PLAN] Error mapping strikes: {e}")
        return {}


def _print_plan(plan):
    pv = plan.get("pivots",{}); st = plan.get("strategy",{})
    fs = plan.get("focus_strikes",{}); ohlc = plan.get("prev_ohlc",{})
    sl = plan.get("strike_levels", {})
    print("\n" + "="*80)
    print(f"  NEXT-DAY PLAN  [{plan.get('symbol')}]  ->  {plan.get('for_date')}")
    print(f"  Generated: {plan.get('generated_at')}")
    print("="*80)
    print(f"  Index OHLC: O:{ohlc.get('open',0):.0f}  H:{ohlc.get('high',0):.0f}  L:{ohlc.get('low',0):.0f}  C:{ohlc.get('close',0):.0f}")
    print(f"  Index Pivots: S3={pv.get('S3',0):.1f}  S2={pv.get('S2',0):.1f}  S1={pv.get('S1',0):.1f}  P={pv.get('P',0):.1f}  R1={pv.get('R1',0):.1f}  R2={pv.get('R2',0):.1f}  R3={pv.get('R3',0):.1f}")
    print()
    print(f"  BUY  -> {st.get('BUY_entry','')}")
    print(f"          {st.get('BUY_targets','')}")
    print(f"  Strikes: {fs.get('BUY_CE_from_S2',[])}")
    print()
    print(f"  SELL -> {st.get('SELL_entry','')}")
    print(f"          {st.get('SELL_targets','')}")
    print(f"  Strikes: {fs.get('SELL_CE_from_R2',[])}")

    if sl:
        print("\n" + "-"*80)
        print("  MAPPED STRIKE FIBONACCI PIVOTS (TradingView Aligned)")
        print("-"*80)
        print(f"  {'Type':<4} {'Strike':<7} {'PrevH':<7} {'PrevL':<7} {'PrevC':<7} {'S2 (Buy)':<9} {'S1':<7} {'Pivot P':<8} {'R1':<7} {'R2 (Sell)':<9} {'R3':<7}")
        print("  " + "-"*76)
        sorted_strikes = sorted(sl.values(), key=lambda x: (x["strike"], x["type"]))
        for s in sorted_strikes:
            # Highlight focus strikes or ATM
            print(f"  {s['type']:<4} {s['strike']:<7} {s['H']:<7.2f} {s['L']:<7.2f} {s['C']:<7.2f} {s['S2']:<9.2f} {s['S1']:<7.2f} {s['P']:<8.2f} {s['R1']:<7.2f} {s['R2']:<9.2f} {s['R3']:<7.2f}")
    print("="*80)


# ── GAP DETECTION ────────────────────────────────────────────────────────────
def detect_gap(symbol=SYMBOL):
    candles = fetch_candles(symbol, days=5)
    today   = date.today()
    today_c = next((c for c in reversed(candles) if c["date"] == today), None)
    prev_c  = next((c for c in reversed(candles) if c["date"] < today),  None)

    if not today_c or not prev_c:
        print("[GAP] Not enough data for gap detection"); return {}

    gap_pct  = round((today_c["open"] - prev_c["close"]) / prev_c["close"] * 100, 2)
    gap_type = "GAP_UP" if gap_pct >= GAP_THRESH else ("GAP_DOWN" if gap_pct <= -GAP_THRESH else "NORMAL")
    icon     = {"GAP_UP": "GAPUP+", "GAP_DOWN": "GAPDN-", "NORMAL": "NORMAL"}[gap_type]

    print(f"\n  [{icon}] {symbol} | Prev Close:{prev_c['close']:.0f} -> Today Open:{today_c['open']:.0f} | Gap:{gap_pct:+.2f}%")
    if gap_type != "NORMAL":
        print(f"  Action: Re-verify planned BUY/SELL zones — gap may shift relevant strikes")
        plan = load_plan()
        if plan:
            pv = plan.get("pivots",{})
            print(f"  Plan Pivots: S2={pv.get('S2',0):.0f}  P={pv.get('P',0):.0f}  R2={pv.get('R2',0):.0f}")
            tip = "Focus SELL from R2 first (market opened higher)" if gap_type=="GAP_UP" else "Focus BUY from S2 first (market opened lower)"
            print(f"  Tip: {tip}")
    else:
        print(f"  Normal open — planned pivot levels are valid as-is")

    return {"type": gap_type, "gap_pct": gap_pct,
            "prev_close": prev_c["close"], "today_open": today_c["open"], "adjust": gap_type != "NORMAL"}


# ── LOAD PLAN ────────────────────────────────────────────────────────────────
def load_plan(for_date=None):
    if for_date:
        matches = glob.glob(os.path.join(PLAN_DIR, f"next_day_plan_{for_date.replace('-','')}.json"))
    else:
        matches = sorted(glob.glob(os.path.join(PLAN_DIR, "next_day_plan_*.json")), reverse=True)
    if matches:
        try:
            with open(matches[0]) as jf: return json.load(jf)
        except: pass
    return {}


def show_plan(for_date=None):
    plan = load_plan(for_date)
    if plan: _print_plan(plan)
    else: print("[PLAN] No saved plan. Run:  python3 eod_planner.py")


# ── HOLD POSITION RECALC ─────────────────────────────────────────────────────
def recalc_hold_positions(positions, symbol=SYMBOL):
    """
    Recalculate SL and targets for carry-forward (overnight hold) positions
    using the next-day pivot plan.

    positions: list of {"action":"BUY"|"SELL", "opt_type":"CE"|"PE",
                         "strike": int, "entry_price": float}
    """
    plan = load_plan()
    if not plan: print("[HOLD] No next-day plan — run eod_planner.py first"); return positions

    pv = plan.get("pivots", {})
    s1,s2,p,r1,r2 = pv.get("S1",0),pv.get("S2",0),pv.get("P",0),pv.get("R1",0),pv.get("R2",0)

    print(f"\n  HOLD RECALC using plan pivots: S2={s2:.0f} S1={s1:.0f} P={p:.0f} R1={r1:.0f} R2={r2:.0f}")
    updated = []
    for pos in positions:
        out = dict(pos)
        if pos.get("action","BUY").upper() == "BUY":
            if s2 <= 0.05:
                r3 = pv.get("R3", round(p + (pv.get("R1", r1) - p) * 2.618, 2))
                out["sl"]=round(max(s1 * 0.9, 0.05), 2); out["t1"]=round(p,2); out["t2"]=round(r1,2); out["t3"]=round(r2,2); out["t4"]=round(r3,2)
            else:
                out["sl"]=round(s2,2); out["t1"]=round(s1,2); out["t2"]=round(p,2); out["t3"]=round(r1,2); out["t4"]=round(r2,2)
        else:
            out["sl"]=round(r2,2); out["t1"]=round(r1,2); out["t2"]=round(p,2); out["t3"]=round(s1,2); out["t4"]=round(s2,2)
        print(f"  {out.get('action')} {out.get('opt_type')} {out.get('strike')} Entry:{pos.get('entry_price',0):.1f} | SL:{out['sl']:.1f} T1:{out['t1']:.1f} T2:{out['t2']:.1f} T3:{out['t3']:.1f} T4:{out['t4']:.1f}")
        updated.append(out)
    return updated


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--gap"  in args: detect_gap(SYMBOL)
    elif "--show" in args: show_plan()
    elif "--hold" in args:
        example = [
            {"action":"BUY",  "opt_type":"CE","strike":22550,"entry_price":95.0},
            {"action":"SELL", "opt_type":"PE","strike":22700,"entry_price":310.0},
        ]
        recalc_hold_positions(example, SYMBOL)
    else:
        generate_plan(SYMBOL)
