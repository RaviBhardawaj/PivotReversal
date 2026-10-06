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
    return {"P": round(p,2), "S1": round(p-0.382*r,2), "S2": round(p-0.618*r,2),
            "S3": round(p-r,2), "R1": round(p+0.382*r,2), "R2": round(p+0.618*r,2), "R3": round(p+r,2)}

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
    buy_strikes    = sorted({snap(s2), snap(s1)})
    sell_strikes   = sorted({snap(r1), snap(r2)}, reverse=True)

    plan = {
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "for_date":      next_date,
        "symbol":        symbol,
        "prev_ohlc":     {k: round(prev[k],2) for k in ("open","high","low","close")},
        "pivots":        pv,
        "strategy": {
            "BUY_entry":    f"S2={s2:.0f} — candle reclaim above S2 | SL = LL below S2",
            "BUY_targets":  f"T1=S1({s1:.0f})  T2=P({p:.0f})  T3=R1({r1:.0f})  T4=R2({r2:.0f})",
            "SELL_entry":   f"R2={r2:.0f} — candle rejection below R2 | SL = HH above R2",
            "SELL_targets": f"T1=R1({r1:.0f})  T2=P({p:.0f})  T3=S1({s1:.0f})  T4=S2({s2:.0f})",
        },
        "focus_strikes": {
            "BUY_CE_from_S2":  buy_strikes,  "BUY_PE_from_S2":  buy_strikes,
            "SELL_CE_from_R2": sell_strikes, "SELL_PE_from_R2": sell_strikes,
        },
        "note": "Planned levels from yesterday OHLC. Run --gap at 09:16 to check for gap adjustments."
    }

    fname = os.path.join(PLAN_DIR, f"next_day_plan_{next_date.replace('-','')}.json")
    with open(fname, "w") as f: json.dump(plan, f, indent=2)

    _print_plan(plan)
    print(f"  Saved to: {fname}\n")
    return plan


def _print_plan(plan):
    pv = plan.get("pivots",{}); st = plan.get("strategy",{})
    fs = plan.get("focus_strikes",{}); ohlc = plan.get("prev_ohlc",{})
    print("\n" + "="*60)
    print(f"  NEXT-DAY PLAN  [{plan.get('symbol')}]  ->  {plan.get('for_date')}")
    print(f"  Generated: {plan.get('generated_at')}")
    print("="*60)
    print(f"  Prev OHLC: O:{ohlc.get('open',0):.0f}  H:{ohlc.get('high',0):.0f}  L:{ohlc.get('low',0):.0f}  C:{ohlc.get('close',0):.0f}")
    print(f"  Pivots: S3={pv.get('S3',0):.1f}  S2={pv.get('S2',0):.1f}  S1={pv.get('S1',0):.1f}  P={pv.get('P',0):.1f}  R1={pv.get('R1',0):.1f}  R2={pv.get('R2',0):.1f}  R3={pv.get('R3',0):.1f}")
    print()
    print(f"  BUY  -> {st.get('BUY_entry','')}")
    print(f"          {st.get('BUY_targets','')}")
    print(f"  Strikes: {fs.get('BUY_CE_from_S2',[])}")
    print()
    print(f"  SELL -> {st.get('SELL_entry','')}")
    print(f"          {st.get('SELL_targets','')}")
    print(f"  Strikes: {fs.get('SELL_CE_from_R2',[])}")
    print("="*60)


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
    dt = for_date or date.today().strftime("%Y%m%d")
    for f in glob.glob(os.path.join(PLAN_DIR, f"next_day_plan_{dt}.json")):
        try:
            with open(f) as jf: return json.load(jf)
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
