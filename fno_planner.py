"""Daily orders for the F&O symbols other than NIFTY.

At 15:45 (or from the dashboard's GENERATE button) every symbol in the list gets the same
15M + 1H reversal orders the Other Symbols tab computes: focus strikes from the symbol's 15M and
1H Fibonacci pivots, and each option contract's entry / targets / SL from its own official daily
candle (the same candle the NIFTY strike levels use). Nothing is estimated: a contract without a
candle gets no order.

By default only the CURRENT expiry (the nearest one still open on the plan's session) is
generated. NEXT or ALL is generated only when asked for.

Orders are saved to fno_orders.json (dashboard) and appended to the "F&O Orders" tab of the
Google Sheet in one batched write per run (small requests, for slow connections).

The symbol list: data_settings.json "fno_plan_symbols" (a list, or "ALL" for every F&O stock and
index), else FNO_PLAN_SYMBOLS in .env (comma separated or ALL), else DEFAULT_SYMBOLS.
"""

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, time as dt_time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FNO_ORDERS_FILE = os.path.join(BASE_DIR, "fno_orders.json")
DATA_SETTINGS_FILE = os.path.join(BASE_DIR, "data_settings.json")
SHEET_TAB = "F&O Orders"
KEEP_DAYS = 10

DEFAULT_SYMBOLS = ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]   # indices

SHEET_HEADERS = ["Plan Date", "Generated At", "Symbol", "Expiry", "Timeframe", "Strike", "Type",
                 "Side", "Option Symbol", "Entry", "T1", "T2", "T3", "T4", "SL", "R:R",
                 "Ref Close", "Candle Date", "Source", "Setup"]

BUY_SETUP = "★ BUY (S2/S1 Reclaim)"
SELL_SETUP = "▼ SELL (R2/R1 Reject)"

_run_lock = threading.Lock()
status = {"running": False, "done": 0, "total": 0, "symbol": "", "message": "Idle", "started_at": ""}


# ── Symbol list ────────────────────────────────────────────────────────────
def _read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def all_fno_symbols(scrip_df) -> list:
    """Every symbol with options on NFO / BFO in the instrument list, NIFTY left out."""
    if scrip_df is None:
        return list(DEFAULT_SYMBOLS)
    o = scrip_df[scrip_df["exch_seg"].isin(["NFO", "BFO"])
                 & scrip_df["instrumenttype"].isin(["OPTIDX", "OPTSTK"])]
    return sorted({str(n).upper() for n in o["name"].dropna().unique()} - {"NIFTY", ""})


def configured_symbols(scrip_df=None) -> list:
    """The symbols the daily run covers (see the module notes). NIFTY is never in it."""
    raw = _read_json(DATA_SETTINGS_FILE).get("fno_plan_symbols")
    if raw is None:
        raw = (os.getenv("FNO_PLAN_SYMBOLS", "") or "").strip() or None
    if isinstance(raw, str):
        if raw.strip().upper() == "ALL":
            return all_fno_symbols(scrip_df)
        raw = [s for s in raw.split(",")]
    syms = [str(s).strip().upper() for s in (raw or DEFAULT_SYMBOLS) if str(s).strip()]
    out = []
    for s in syms:
        if s != "NIFTY" and s not in out:
            out.append(s)
    return out


def has_options(scrip_df, sym: str) -> bool:
    """True when the instrument list has NFO/BFO options of sym (True when it is not loaded)."""
    if scrip_df is None:
        return True
    m = scrip_df[(scrip_df["name"] == sym) & scrip_df["exch_seg"].isin(["NFO", "BFO"])
                 & scrip_df["instrumenttype"].isin(["OPTIDX", "OPTSTK"])]
    return not m.empty


def edit_symbols(action: str, symbol: str, scrip_df=None) -> dict:
    """Add or remove one symbol of the daily list (saved in data_settings.json, other settings
    kept). Returns {"success", "symbols", "error"}."""
    sym = (symbol or "").strip().upper()
    current = configured_symbols(scrip_df)
    action = (action or "").strip().lower()
    if action == "reset":
        new = list(DEFAULT_SYMBOLS)
    elif not sym:
        return {"success": False, "symbols": current, "error": "Enter a symbol"}
    elif action == "add":
        if sym == "NIFTY":
            return {"success": False, "symbols": current, "error": "NIFTY has its own next-day plan"}
        if sym in current:
            return {"success": False, "symbols": current, "error": f"{sym} is already in the list"}
        if not has_options(scrip_df, sym):
            return {"success": False, "symbols": current, "error": f"{sym} has no F&O options"}
        new = current + [sym]
    elif action == "remove":
        if sym not in current:
            return {"success": False, "symbols": current, "error": f"{sym} is not in the list"}
        new = [x for x in current if x != sym]
    else:
        return {"success": False, "symbols": current, "error": f"unknown action {action}"}
    d = _read_json(DATA_SETTINGS_FILE)
    d["fno_plan_symbols"] = new
    tmp = DATA_SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, DATA_SETTINGS_FILE)
    return {"success": True, "symbols": new, "error": ""}


# ── Dates and expiries ─────────────────────────────────────────────────────
def after_close(now: datetime) -> bool:
    return now.weekday() >= 5 or now.time() >= dt_time(15, 30)


def plan_date(now: datetime = None) -> date:
    """The session the orders are for: the next weekday after the close, today before it."""
    now = now or datetime.now()
    d = now.date()
    if after_close(now):
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def pick_expiries(upcoming: list, expiry_mode: str = "CURRENT") -> list:
    """CURRENT (default) -> nearest only, NEXT -> the one after it, ALL -> both."""
    upcoming = [e for e in (upcoming or []) if e]
    if not upcoming:
        return []
    mode = (expiry_mode or "CURRENT").strip().upper()
    if mode == "NEXT":
        return upcoming[1:2]
    if mode == "ALL":
        return upcoming[:2]
    if mode in {e.upper() for e in upcoming}:
        return [e for e in upcoming if e.upper() == mode]
    return upcoming[:1]


# ── Order maths (shared with the Other Symbols tab) ────────────────────────
def focus_combos(f15: dict, f1h: dict) -> list:
    """(timeframe, option type, strikes, side, setup) the strategy trades for one expiry:
    PE at S1/S2 strikes, CE at R1/R2 strikes, and the P strike(s) on both CE and PE."""
    combos = []
    for tf, fo in (("15M", f15 or {}), ("1H", f1h or {})):
        pe, ce = list(fo.get("pe_strikes", []))[:2], list(fo.get("ce_strikes", []))[:2]
        combos += [(tf, "PE", pe, "BUY", BUY_SETUP), (tf, "CE", ce, "BUY", BUY_SETUP),
                   (tf, "CE", ce, "SELL", SELL_SETUP), (tf, "PE", pe, "SELL", SELL_SETUP)]
    for tf, fo in (("15M", f15 or {}), ("1H", f1h or {})):
        ps = list(fo.get("p_strikes") or ([fo["P_STRIKE"]] if fo.get("P_STRIKE") else []))
        for ot in ("CE", "PE"):
            combos.append((tf, ot, ps, "BUY", BUY_SETUP))
            combos.append((tf, ot, ps, "SELL", SELL_SETUP))
    return combos


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def order_from_levels(levels: dict, side: str, ltp: float = 0.0) -> dict:
    """Entry, T1-T4, SL and R:R of one contract from its own Fibonacci levels.
    BUY enters at S2 (S1 when S2 is at the 0.05 floor), SELL at R2. None without levels."""
    p, s1, s2, s3 = (_f(levels.get(k)) for k in ("P", "S1", "S2", "S3"))
    r1, r2, r3 = (_f(levels.get(k)) for k in ("R1", "R2", "R3"))
    if p <= 0:
        return None
    if side == "BUY":
        ep = s2 if s2 > 0.05 else s1
        if ep <= 0.05:
            ep = round(ltp * 0.95, 2)
        t1 = s1 if s1 > ep else p
        if t1 <= ep: t1 = round(ep * 1.25, 2)
        t2 = p if p > t1 else r1
        if t2 <= t1: t2 = round(t1 * 1.20, 2)
        t3 = r1 if r1 > t2 else r2
        if t3 <= t2: t3 = round(t2 * 1.20, 2)
        t4 = r2 if r2 > t3 else r3
        if t4 <= t3: t4 = round(t3 * 1.20, 2)
        sl = s3 if s3 > 0.05 else round(ep * 0.88, 2)
        rr = f"{((t1 - ep) / max(ep - sl, 0.5)):.1f}:1" if ep > sl else "1:2.0"
        sig = f"Reversal BUY @ S2:{ep:.2f} | Reclaim S2 | T1:{t1:.2f} T2:{t2:.2f}"
    else:
        ep = r2 if r2 > 0.05 else r1
        if ep <= 0.05:
            ep = round(ltp * 1.05, 2)
        t1 = r1 if 0 < r1 < ep else p
        if t1 >= ep: t1 = round(ep * 0.80, 2)
        t2 = p if 0 < p < t1 else s1
        if t2 >= t1: t2 = round(t1 * 0.80, 2)
        t3 = s1 if 0 < s1 < t2 else s2
        if t3 >= t2: t3 = round(t2 * 0.80, 2)
        t4 = s2 if 0 < s2 < t3 else s3
        if t4 >= t3: t4 = round(t3 * 0.80, 2)
        sl = r3 if r3 > 0.05 else round(ep * 1.12, 2)
        rr = f"{((ep - t1) / max(sl - ep, 0.5)):.1f}:1" if sl > ep else "1:2.0"
        sig = f"Reversal SELL @ R2:{ep:.2f} | Rejection R2 | T1:{t1:.2f} T2:{t2:.2f}"
    return {"entry_price": round(ep, 2), "target_1": round(t1, 2), "target_2": round(t2, 2),
            "target_3": round(t3, 2), "target_4": round(t4, 2), "sl": round(sl, 2),
            "rr": rr, "signal": sig}


# ── One symbol ─────────────────────────────────────────────────────────────
def _contracts(df, sym, exch, expiry):
    """{(strike, 'CE'|'PE'): row} of the symbol's options of one expiry."""
    sub = df[(df["name"] == sym) & (df["exch_seg"] == exch) & (df["expiry"] == expiry)
             & (df["instrumenttype"].isin(["OPTIDX", "OPTSTK"]))]
    out = {}
    for _, r in sub.iterrows():
        s = str(r["symbol"])
        ot = "CE" if s.endswith("CE") else ("PE" if s.endswith("PE") else "")
        if not ot:
            continue
        try:
            stk = int(round(float(r["strike"]) / 100.0))
        except (TypeError, ValueError):
            continue
        out[(stk, ot)] = r
    return out


def build_symbol_orders(eng, sym: str, expiry_mode: str = "CURRENT", now: datetime = None,
                        pause: float = 0.35) -> dict:
    """Orders of one symbol for the plan's session. eng is the loaded app module."""
    now = now or datetime.now()
    sym = sym.strip().upper()
    target = plan_date(now)
    exch = eng.get_symbol_exchange(sym)
    step = eng.get_symbol_step(sym, exch)
    df = eng.Session.scrip_df
    res = {"symbol": sym, "exchange": exch, "step": step, "plan_date": target.isoformat(),
           "expiries": [], "setups": [], "missing": [], "error": ""}
    if df is None:
        res["error"] = "instrument list not loaded"
        return res
    exps = pick_expiries(eng.feed.upcoming_expiries(sym, 2, from_day=target), expiry_mode)
    res["expiries"] = exps
    if not exps:
        res["error"] = "no open expiry"
        return res
    mtf = eng.get_tv_multi_timeframe_pivots(sym, force_refresh=False) or {}
    if not mtf.get("15-Min") and not mtf.get("1-Hour"):
        res["error"] = "no 15M/1H pivots"
        return res
    f15 = eng.select_intraday_15m_focus_strikes(mtf, step)
    f1h = eng.select_1h_structure_strikes(mtf, step)
    res["spot"] = _f((mtf.get("15-Min") or {}).get("C") or (mtf.get("Daily") or {}).get("C"))
    res["lot_size"] = eng.get_symbol_lot_size(sym, exch)
    mode = "next" if after_close(now) else "session"
    combos = focus_combos(f15, f1h)

    for exp in exps:
        contracts = _contracts(df, sym, exch, exp)
        levels = {}                     # (strike, type) -> levels dict, fetched once per contract
        seen = set()
        for tf, ot, strikes, side, setup in combos:
            for stk in strikes:
                if not stk or stk <= 0 or (tf, stk, ot, side) in seen:
                    continue
                seen.add((tf, stk, ot, side))
                row = contracts.get((int(stk), ot))
                if row is None:
                    res["missing"].append(f"{stk}{ot} {exp}: no contract")
                    continue
                key = (int(stk), ot)
                if key not in levels:
                    levels[key] = None
                    tok = str(row["token"])
                    itype = "OPTSTK" if str(row.get("instrumenttype", "")) == "OPTSTK" else "OPTIDX"
                    try:
                        cndl, src, _day = eng.official_strike_candle(exch, tok, itype, mode, now)
                    except Exception as e:
                        cndl, src = None, str(e)
                    if cndl:
                        c_date, _o, h, l, c = cndl
                        piv = eng.calc_fib_pivots(h, l, c)
                        if piv:
                            levels[key] = {**piv, "prev_c": round(c, 2), "candle_date": str(c_date),
                                           "source": src, "token": tok, "option_symbol": str(row["symbol"])}
                    if levels[key] is None:
                        res["missing"].append(f"{stk}{ot} {exp}: {src}")
                    if pause:
                        time.sleep(pause)
                lv = levels[key]
                if not lv:
                    continue
                o = order_from_levels(lv, side, lv["prev_c"])
                if not o:
                    continue
                res["setups"].append({
                    "setup_id": f"{sym}_{target.isoformat()}_{stk}_{ot}_{side}_{tf}_{exp}",
                    "symbol": sym, "expiry": exp, "timeframe": tf, "strike": int(stk), "type": ot,
                    "side": side, "option_symbol": lv["option_symbol"], "token": lv["token"],
                    "ref_close": lv["prev_c"], "candle_date": lv["candle_date"], "source": lv["source"],
                    "setup": setup, "status": "WAITING FOR ENTRY", **o})
    return res


# ── Storage ────────────────────────────────────────────────────────────────
def load_orders() -> dict:
    return _read_json(FNO_ORDERS_FILE)


def save_run(day: str, generated_at: str, expiry_mode: str, results: list):
    """Merge one run into fno_orders.json under its plan date (a symbol's newer run replaces its
    older one) and keep the last KEEP_DAYS dates."""
    data = load_orders()
    entry = data.get(day) or {"symbols": {}}
    entry["generated_at"] = generated_at
    entry["expiry_mode"] = expiry_mode
    for r in results:
        entry["symbols"][r["symbol"]] = {**r, "generated_at": generated_at}
    data[day] = entry
    for old in sorted(data)[:-KEEP_DAYS]:
        data.pop(old, None)
    tmp = FNO_ORDERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, FNO_ORDERS_FILE)


def sheet_rows(day: str, generated_at: str, results: list) -> list:
    rows = []
    for r in results:
        for s in r.get("setups", []):
            rows.append([day, generated_at, s["symbol"], s["expiry"], s["timeframe"], s["strike"],
                         s["type"], s["side"], s["option_symbol"], s["entry_price"], s["target_1"],
                         s["target_2"], s["target_3"], s["target_4"], s["sl"], s["rr"],
                         s["ref_close"], s["candle_date"], s["source"], s["setup"]])
    return rows


def push_to_sheet(eng, rows: list, retries: int = 3) -> str:
    """Append the run's rows to the "F&O Orders" tab: one append call (chunks of 500 rows),
    retried with a backoff when the connection drops. Returns a short result note."""
    if not rows:
        return "no rows"
    last = ""
    for attempt in range(retries):
        try:
            gc = eng.get_gspread_client()
            sh = gc.open_by_key(eng.SPREADSHEET_ID)
            ws = next((w for w in sh.worksheets() if w.title == SHEET_TAB), None)
            if ws is None:
                ws = sh.add_worksheet(title=SHEET_TAB, rows=max(1000, len(rows) + 10), cols=len(SHEET_HEADERS))
                ws.update(values=[SHEET_HEADERS], range_name="A1")
            elif (ws.row_values(1) or [])[:1] != SHEET_HEADERS[:1]:
                ws.update(values=[SHEET_HEADERS], range_name="A1")
            for i in range(0, len(rows), 500):
                ws.append_rows(rows[i:i + 500], value_input_option="USER_ENTERED")
            return f"{len(rows)} rows added to '{SHEET_TAB}'"
        except Exception as e:
            last = str(e)[:160]
            print(f"[F&O ORDERS] Sheet write failed (try {attempt + 1}/{retries}): {last}")
            time.sleep(min(60, 5 * (2 ** attempt)) if "429" not in last else 60)
    return f"sheet not updated: {last}"


# ── A full run ─────────────────────────────────────────────────────────────
def generate(eng, symbols: list = None, expiry_mode: str = "CURRENT", sheet: bool = True,
             now: datetime = None, pause: float = 0.35) -> dict:
    """Build and store the orders of every symbol, then write them to the sheet once.
    Only one run at a time; a second call while one runs returns at once."""
    if not _run_lock.acquire(blocking=False):
        return {"success": False, "error": "a run is already in progress", "status": dict(status)}
    try:
        now = now or datetime.now()
        syms = [s.strip().upper() for s in (symbols or configured_symbols(eng.Session.scrip_df))
                if s and s.strip() and s.strip().upper() != "NIFTY"]
        expiry_mode = (expiry_mode or "CURRENT").strip().upper()
        day = plan_date(now).isoformat()
        gen_at = now.strftime("%Y-%m-%d %H:%M:%S")
        status.update(running=True, done=0, total=len(syms), symbol="", started_at=gen_at,
                      message=f"Generating {expiry_mode.lower()} expiry orders for {len(syms)} symbols")
        print(f"[F&O ORDERS] {status['message']} (plan date {day})")
        results = []
        for i, sym in enumerate(syms):
            status.update(symbol=sym, done=i)
            try:
                r = build_symbol_orders(eng, sym, expiry_mode, now=now, pause=pause)
            except Exception as e:
                r = {"symbol": sym, "plan_date": day, "expiries": [], "setups": [], "missing": [],
                     "error": str(e)[:200]}
            results.append(r)
            print(f"[F&O ORDERS] {sym}: {len(r['setups'])} orders {','.join(r.get('expiries') or [])}"
                  + (f" ({r['error']})" if r.get("error") else ""))
        save_run(day, gen_at, expiry_mode, results)
        rows = sheet_rows(day, gen_at, results)
        note = push_to_sheet(eng, rows) if sheet else "sheet skipped"
        msg = f"{len(rows)} orders for {len(syms)} symbols ({day}); {note}"
        status.update(running=False, done=len(syms), symbol="", message=msg)
        print(f"[F&O ORDERS] ✅ {msg}")
        return {"success": True, "plan_date": day, "orders": len(rows), "sheet": note,
                "symbols": {r["symbol"]: len(r["setups"]) for r in results}}
    except Exception as e:
        status.update(running=False, message=f"Run failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        _run_lock.release()


def claim_auto_run(day: str) -> bool:
    """True once per plan date across every process (desktop app and web server both have a
    15:45 timer): an exclusive marker file decides which one runs."""
    marker = os.path.join(BASE_DIR, f".fno_auto_{day}")
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, datetime.now().isoformat().encode())
        os.close(fd)
    except FileExistsError:
        return False
    except OSError:
        return True
    for f in os.listdir(BASE_DIR):          # tidy old markers
        if f.startswith(".fno_auto_") and f < f".fno_auto_{day}":
            try:
                os.remove(os.path.join(BASE_DIR, f))
            except OSError:
                pass
    return True


def start_daily_run(eng, now: datetime = None) -> bool:
    """15:45 hook: current-expiry orders for the configured symbols, in the background."""
    day = plan_date(now or datetime.now()).isoformat()
    if not claim_auto_run(day):
        return False
    threading.Thread(target=generate, args=(eng,), kwargs={"expiry_mode": "CURRENT"},
                     daemon=True, name="fno-orders").start()
    return True


def flat_orders(day: str = None, symbol: str = None) -> dict:
    """Stored orders of one plan date (latest by default), optionally of one symbol."""
    data = load_orders()
    if not data:
        return {"plan_date": "", "dates": [], "symbols": [], "setups": [], "summary": {}}
    day = day if day in data else sorted(data)[-1]
    entry = data[day]
    syms = sorted(entry.get("symbols", {}))
    setups, summary = [], {}
    for s in syms:
        r = entry["symbols"][s]
        summary[s] = {"orders": len(r.get("setups", [])), "expiries": r.get("expiries", []),
                      "error": r.get("error", ""), "missing": len(r.get("missing", [])),
                      "generated_at": r.get("generated_at", "")}
        if not symbol or symbol.upper() in ("ALL", s):
            setups.extend(r.get("setups", []))
    return {"plan_date": day, "dates": sorted(data, reverse=True), "symbols": syms,
            "generated_at": entry.get("generated_at", ""), "expiry_mode": entry.get("expiry_mode", ""),
            "setups": setups, "summary": summary}
