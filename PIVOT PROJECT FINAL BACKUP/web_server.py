#!/usr/bin/env python3
"""
Quantum Terminal — FastAPI WebSocket & REST Web Server
Serves real-time streaming dashboard for Live Option Chain & Fibonacci Reversal Strategy
Continuous Daily Trading Cycle Engine (Next-Day Plan -> Live Scanner -> Execution -> Store Plan)
"""

import os
import sys
import time
import json
import csv
import asyncio
import threading
from datetime import datetime, timedelta, time as dt_time
from typing import List, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Dynamic Import from Options-angel_live-ok.py ──────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_PATH = os.path.join(BASE_DIR, "Options-angel_live-ok.py")

import importlib.util
spec = importlib.util.spec_from_file_location("engine_mod", ENGINE_PATH)
engine = importlib.util.module_from_spec(spec)
sys.modules["engine_mod"] = engine
spec.loader.exec_module(engine)

# Re-export core references
Session = engine.Session
feed = engine.feed
live_order_manager = engine.live_order_manager
FORWARD_TEST_FILE = engine.FORWARD_TEST_FILE
MULTI_LEVELS_FILE = engine.MULTI_LEVELS_FILE
SPREADSHEET_ID = engine.SPREADSHEET_ID
EOD_SQUARE_OFF_TIME = engine.EOD_SQUARE_OFF_TIME
NEXT_DAY_LEVELS_TIME = engine.NEXT_DAY_LEVELS_TIME
compute_points_summary = engine.compute_points_summary
get_tv_multi_timeframe_pivots = engine.get_tv_multi_timeframe_pivots
select_intraday_15m_focus_strikes = engine.select_intraday_15m_focus_strikes
select_1h_structure_strikes = engine.select_1h_structure_strikes
get_symbol_exchange = engine.get_symbol_exchange
get_symbol_step = engine.get_symbol_step
get_symbol_lot_size = engine.get_symbol_lot_size
EXCHANGE_CONFIG = engine.EXCHANGE_CONFIG
_safe_float = engine._safe_float

app = FastAPI(title="Quantum Terminal — Live Option Chain Web Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = os.path.join(BASE_DIR, "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# ── WebSocket Connection Manager ──────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.append(connection)
        for dc in dead_connections:
            self.disconnect(dc)

manager = ConnectionManager()

# ── Pydantic Request Models ────────────────────────────────────
class StartFeedRequest(BaseModel):
    symbol: str = "NIFTY"
    expiry: str = ""
    exchange: str = "NFO"
    n_strikes: int = 8

class ManualCloseRequest(BaseModel):
    type: str
    strike: int
    expiry: str = ""
    symbol: str = ""

# ── Continuous Next-Day Plan Storage & Date Functions ───────────
PLANS_FILE = os.path.join(BASE_DIR, "next_day_plans.json")
ALERT_TRACKER = set()

def get_next_trading_date(base_date=None, after_market_close=True):
    """
    Returns YYYY-MM-DD for the next actual trading session.
    Skips Saturday (weekday 5) and Sunday (weekday 6).
    """
    if base_date is None:
        now_dt = datetime.now()
        base_date = now_dt.date()
        if now_dt.weekday() < 5 and (now_dt.hour < 15 or (now_dt.hour == 15 and now_dt.minute < 30)):
            after_market_close = False
            
    target = base_date + timedelta(days=1) if after_market_close else base_date
    while target.weekday() >= 5:  # Skip Saturday (5) & Sunday (6)
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%d")

def get_current_trading_date():
    """Returns the trading date for the current/most recent session."""
    now_dt = datetime.now()
    d = now_dt.date()
    if d.weekday() == 5:
        d -= timedelta(days=1)
    elif d.weekday() == 6:
        d -= timedelta(days=2)
    return d.strftime("%Y-%m-%d")

def load_all_plans() -> dict:
    if os.path.exists(PLANS_FILE):
        try:
            with open(PLANS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_all_plans(plans_dict: dict):
    try:
        with open(PLANS_FILE, "w") as f:
            json.dump(plans_dict, f, indent=2)
    except Exception as e:
        print(f"[PLANS SAVE ERROR] {e}")

def get_stored_plan(symbol="NIFTY", target_date=None) -> dict:
    plans = load_all_plans()
    sym = symbol.strip().upper()
    if target_date:
        plan_key = f"{sym}_{target_date}"
        if plan_key in plans:
            return plans[plan_key]
    matches = [p for k, p in plans.items() if p.get("symbol") == sym]
    if matches:
        matches.sort(key=lambda x: x.get("target_trading_date", ""), reverse=True)
        return matches[0]
    return None

def store_next_day_plan(plan_data: dict):
    plans = load_all_plans()
    sym = plan_data.get("symbol", "NIFTY")
    target_date = plan_data.get("target_trading_date")
    plan_key = f"{sym}_{target_date}"
    plans[plan_key] = plan_data
    save_all_plans(plans)
    return plan_data

def generate_and_store_next_day_plan(sym: str = "NIFTY", force_refresh: bool = False) -> dict:
    sym = (sym or "NIFTY").strip().upper()
    target_date = get_next_trading_date()

    if not force_refresh:
        existing = get_stored_plan(sym, target_date)
        if existing and existing.get("setups"):
            return existing

    exch = get_symbol_exchange(sym)
    step = get_symbol_step(sym, exch)

    # 1. Multi-timeframe pivots strictly separated: 15-Min and 1-Hour ONLY (NO Daily Pivot for strategy)
    mtf = get_tv_multi_timeframe_pivots(sym, force_refresh=force_refresh)
    piv_15m = mtf.get("15-Min", {})
    piv_1h  = mtf.get("1-Hour", {})
    piv_daily = mtf.get("Daily", {})
    piv_weekly = mtf.get("Weekly", {})

    def _clean_lvl(d):
        p = _safe_float(d.get("P", 0))
        return {
            "P": p,
            "S1": _safe_float(d.get("S1", p - step)),
            "S2": _safe_float(d.get("S2", p - 2 * step)),
            "S3": _safe_float(d.get("S3", p - 3 * step)),
            "R1": _safe_float(d.get("R1", p + step)),
            "R2": _safe_float(d.get("R2", p + 2 * step)),
            "R3": _safe_float(d.get("R3", p + 3 * step)),
        }

    lvl_15m = _clean_lvl(piv_15m)
    lvl_1h  = _clean_lvl(piv_1h)

    # 2. Strike focus from 15M and 1H
    f15 = select_intraday_15m_focus_strikes(mtf, step)
    f1h = select_1h_structure_strikes(mtf, step)

    # 3. Live Option Chain watch items
    payload = get_dashboard_payload(requested_sym=sym, requested_exp="CURRENT", bypass_stored_plan=True)
    dash_watch = payload.get("watch_items", [])
    spot = payload.get("spot", 0.0)
    c_exp = payload.get("active_expiry") or payload.get("current_expiry") or "06OCT2026"

    setups = []
    seen_setups = set()
    for w in dash_watch:
        is_buy = (w.get("side") == "BUY")
        is_15m = bool(w.get("is_15m"))
        is_1h  = bool(w.get("is_1h"))
        if not (is_15m or is_1h):
            continue

        stk = int(w.get("strike", 0))
        ot = w.get("type", "CE")
        side = w.get("side", "BUY")
        s_key = (stk, ot, side)
        if s_key in seen_setups:
            continue
        seen_setups.add(s_key)

        tf_label = "15M & 1H" if (is_15m and is_1h) else ("15M" if is_15m else "1H")
        setup_nm = "★ BUY (S2/S1 Reclaim)" if is_buy else "▼ SELL (R2/R1 Reject)"
        ep = _safe_float(w.get("planned_ep", w.get("entry_price", 0)))
        tgt1 = _safe_float(w.get("t1", 0))
        tgt2 = _safe_float(w.get("t2", 0))
        tgt3 = _safe_float(w.get("t3", 0))
        tgt4 = _safe_float(w.get("t4", 0))
        sl = _safe_float(w.get("sl", 0))

        # Strictly enforce Target 1 != Entry
        if is_buy and tgt1 <= ep:
            tgt1 = round(ep * 1.25, 2)
        elif not is_buy and tgt1 >= ep:
            tgt1 = round(ep * 0.80, 2)

        if tgt2 <= tgt1 and is_buy:
            tgt2 = round(tgt1 * 1.20, 2)
        elif tgt2 >= tgt1 and not is_buy:
            tgt2 = round(tgt1 * 0.80, 2)

        if tgt3 <= tgt2 and is_buy:
            tgt3 = round(tgt2 * 1.20, 2)
        elif (tgt3 >= tgt2 or tgt3 <= 0) and not is_buy:
            tgt3 = round(tgt2 * 0.80, 2)

        if tgt4 <= tgt3 and is_buy:
            tgt4 = round(tgt3 * 1.20, 2)
        elif (tgt4 >= tgt3 or tgt4 <= 0) and not is_buy:
            tgt4 = round(tgt3 * 0.80, 2)

        rr = "-"
        if is_buy and ep > sl and sl > 0:
            rr = f"{((tgt1 - ep) / max(ep - sl, 0.5)):.1f}:1"
        elif not is_buy and sl > ep and ep > 0:
            rr = f"{((ep - tgt1) / max(sl - ep, 0.5)):.1f}:1"

        lvl_desc = "S1/S2 Reclaim" if is_buy else "R1/R2 Reject"
        entry_cond = f"Candle reclaim above ₹{ep:.1f} with 5M confirmation" if is_buy else f"Candle rejection below ₹{ep:.1f} with 5M confirmation"

        setups.append({
            "setup_id": f"{sym}_{target_date}_{stk}_{ot}_{side}",
            "timeframe": tf_label,
            "symbol": sym,
            "expiry": w.get("expiry", c_exp),
            "strike": stk,
            "type": ot,
            "side": side,
            "option_symbol": w.get("option_symbol", f"{sym}{w.get('expiry', c_exp)}{stk}{ot}"),
            "token": str(w.get("token", "")),
            "calculated_level": lvl_desc,
            "entry_condition": entry_cond,
            "entry_price": ep,
            "planned_ep": ep,
            "target_1": tgt1,
            "target_2": tgt2,
            "target_3": tgt3,
            "target_4": tgt4,
            "sl": sl,
            "ref_option_price": _safe_float(w.get("ltp", 0)),
            "rr": rr,
            "setup": setup_nm,
            "strategy_status": "WAITING FOR ENTRY",
            "entry_status": "WAITING",
            "target_status": "NOT_ACTIVE",
            "sl_status": "NOT_ACTIVE"
        })

    plan_data = {
        "success": True,
        "plan_id": f"{sym}_{target_date}",
        "symbol": sym,
        "generated_date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "target_trading_date": target_date,
        "status": "STORED & READY FOR NEXT SESSION",
        "methodology": "15M + 1H Intraday Strategy Engine (Zero Daily Pivot Dependency)",
        "spot": spot,
        "levels_15m": lvl_15m,
        "levels_1h": lvl_1h,
        "support": {
            "15M": f"S1: {lvl_15m['S1']:.1f} | S2: {lvl_15m['S2']:.1f}",
            "1H": f"S1: {lvl_1h['S1']:.1f} | S2: {lvl_1h['S2']:.1f}",
            "focus_pe": f15.get("pe_strikes", [])
        },
        "resistance": {
            "15M": f"R1: {lvl_15m['R1']:.1f} | R2: {lvl_15m['R2']:.1f}",
            "1H": f"R1: {lvl_1h['R1']:.1f} | R2: {lvl_1h['R2']:.1f}",
            "focus_ce": f15.get("ce_strikes", [])
        },
        "reference_levels": {
            "daily_fib": piv_daily,
            "weekly_fib": piv_weekly
        },
        "setups": setups
    }

    store_next_day_plan(plan_data)
    print(f"[NEXT-DAY PLAN] ✅ Stored {len(setups)} setups for {sym} for target session {target_date}")
    return plan_data

# ── Helper Data Builders ───────────────────────────────────────
def get_dashboard_payload(requested_sym: str = None, requested_exp: str = None, bypass_stored_plan: bool = False):
    """Assembles fresh real-time payload matching the desktop terminal."""
    today_date = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%H:%M:%S")

    # Spot Price & Symbol Resolution
    feed_sym = getattr(feed, "symbol", "NIFTY") or "NIFTY"
    current_sym = (requested_sym or feed_sym).strip().upper()
    spot = feed.spot_price.get(current_sym, 0.0)
    sp_zone = feed.spot_zone.get(current_sym, "NEUTRAL")

    exch = get_symbol_exchange(current_sym)
    step = get_symbol_step(current_sym, exch)

    # MTF Pivots & Focus strikes for requested symbol
    if current_sym == feed_sym and feed.mtf_pivots:
        mtf = feed.mtf_pivots
    else:
        mtf = get_tv_multi_timeframe_pivots(current_sym)

    # Fallback spot from MTF pivots if live spot not yet received
    if spot <= 0 and mtf:
        if mtf.get("15-Min"):
            spot = mtf["15-Min"].get("P", 0.0)
        elif mtf.get("Daily"):
            spot = mtf["Daily"].get("P", 0.0)
    if spot <= 0:
        spot = 22400.0 if current_sym == "NIFTY" else (54000.0 if current_sym == "BANKNIFTY" else 1200.0)

    # 15M Focus Strikes & 15M Data Levels
    f15 = select_intraday_15m_focus_strikes(mtf, step) if mtf else {}
    ce_15m = set(f15.get("ce_strikes", []))
    pe_15m = set(f15.get("pe_strikes", []))
    p15_v  = _safe_float(f15.get("P", spot))
    s1_15_v = _safe_float(f15.get("S1", round(p15_v - step, 2)))
    s2_15_v = _safe_float(f15.get("S2", round(p15_v - 2 * step, 2)))
    s3_15_v = _safe_float(f15.get("S3", round(p15_v - 3 * step, 2)))
    r1_15_v = _safe_float(f15.get("R1", round(p15_v + step, 2)))
    r2_15_v = _safe_float(f15.get("R2", round(p15_v + 2 * step, 2)))
    r3_15_v = _safe_float(f15.get("R3", round(p15_v + 3 * step, 2)))

    # 1H Structure Strikes & 1H Data Levels
    f1h = select_1h_structure_strikes(mtf, step) if mtf else {}
    ce_1h = set(f1h.get("ce_strikes", []))
    pe_1h = set(f1h.get("pe_strikes", []))
    p1h_v  = _safe_float(f1h.get("P", spot))
    s1_1h_v = _safe_float(f1h.get("S1", round(p1h_v - step, 2)))
    s2_1h_v = _safe_float(f1h.get("S2", round(p1h_v - 2 * step, 2)))
    s3_1h_v = _safe_float(f1h.get("S3", round(p1h_v - 3 * step, 2)))
    r1_1h_v = _safe_float(f1h.get("R1", round(p1h_v + step, 2)))
    r2_1h_v = _safe_float(f1h.get("R2", round(p1h_v + 2 * step, 2)))
    r3_1h_v = _safe_float(f1h.get("R3", round(p1h_v + 3 * step, 2)))

    # Check live watchlist
    with feed.lock:
        if current_sym == feed_sym and getattr(feed, "s1_s2_watchlist", None):
            live_all_watch = list(feed.s1_s2_watchlist)
        else:
            live_all_watch = []
        snap = dict(feed.live_data)
        meta = dict(feed.token_to_meta)

    # Expiry resolution (Current and Next Expiry support)
    expiries_list = []
    if Session.scrip_df is not None:
        try:
            df = Session.scrip_df
            raw_exp = df[(df["name"] == current_sym) & (df["strike"].astype(float) > 0)]["expiry"].unique()
            if len(raw_exp) > 0:
                today_d = datetime.now().date()
                all_sorted = sorted(raw_exp, key=lambda e: datetime.strptime(e, "%d%b%Y") if e else datetime.max)
                fut_sorted = [e for e in all_sorted if datetime.strptime(e, "%d%b%Y").date() >= today_d]
                expiries_list = fut_sorted if fut_sorted else all_sorted
        except Exception:
            pass

    curr_exp = expiries_list[0] if len(expiries_list) > 0 else (getattr(feed, 'expiry', '') or "06OCT2026")
    next_exp = expiries_list[1] if len(expiries_list) > 1 else (getattr(feed, 'next_expiry', '') or "13OCT2026")

    req_e_upper = (requested_exp or getattr(feed, 'target_expiry', '') or getattr(feed, 'expiry', '') or "").strip().upper()
    if req_e_upper == "NEXT":
        active_exp = next_exp if next_exp else curr_exp
        target_expiries = [active_exp]
    elif req_e_upper == "ALL":
        active_exp = "ALL"
        target_expiries = [e for e in [curr_exp, next_exp] if e]
        if not target_expiries:
            target_expiries = ["06OCT2026", "13OCT2026"]
    elif req_e_upper == "CURRENT":
        active_exp = curr_exp
        target_expiries = [curr_exp]
    elif req_e_upper and req_e_upper in expiries_list:
        active_exp = req_e_upper
        target_expiries = [active_exp]
    else:
        active_exp = curr_exp
        target_expiries = [curr_exp]

    # Load option pivot cache
    cache_file = os.path.join(BASE_DIR, "strike_pivot_cache.json")
    opt_piv_cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as cf:
                c_json = json.load(cf).get("strikes", {})
                for t_id, s_info in c_json.items():
                    ot_k = s_info.get("opt_type", "CE")
                    stk_k = int(s_info.get("strike", 0))
                    exp_k = s_info.get("expiry", "")
                    opt_piv_cache[(ot_k, stk_k, exp_k)] = s_info
                    if (ot_k, stk_k) not in opt_piv_cache:
                        opt_piv_cache[(ot_k, stk_k)] = s_info
        except Exception:
            pass

    # Forward Test Trades — Strictly Today's Live Market Session
    active_trades = feed.forward_tracker.get_active_trades()
    closed_trades_raw = []
    if os.path.exists(FORWARD_TEST_FILE):
        try:
            with open(FORWARD_TEST_FILE, "r", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r.get("Status", "").strip() == "CLOSED":
                        if r.get("Date", "").strip() == today_date:
                            closed_trades_raw.append(r)
        except Exception as _fe:
            print(f"[CSV READ ERROR] {_fe}")

    cum_pts = 0.0
    closed_with_cum = []
    for r in closed_trades_raw:
        p = _safe_float(r.get("PnL_Pts", 0))
        cum_pts += p
        rc = dict(r)
        rc["Cum_Pts"] = round(cum_pts, 2)
        closed_with_cum.append(rc)

    scorecard = compute_points_summary(closed_trades_raw, active_trades)

    # Check stored Next-Day Plan for active trading session
    active_plan = None
    if not bypass_stored_plan:
        active_plan = get_stored_plan(current_sym)
        if not active_plan or not active_plan.get("setups"):
            try:
                active_plan = generate_and_store_next_day_plan(current_sym, force_refresh=False)
            except Exception:
                active_plan = None

    watch_items = []

    # If stored Next-Day Plan is present, load the exact planned strikes & contracts!
    if active_plan and active_plan.get("setups") and not bypass_stored_plan and req_e_upper != "ALL":
        for s in active_plan.get("setups", []):
            stk = int(s.get("strike", 0))
            ot = s.get("type", "CE")
            side = s.get("side", "BUY")
            exp_item = s.get("expiry", curr_exp)
            opt_tok = str(s.get("token", ""))
            opt_sym = s.get("option_symbol", f"{current_sym}{exp_item}{stk}{ot}")

            # Live LTP resolution
            ltp = 0.0
            if opt_tok and opt_tok in snap:
                ltp = _safe_float(snap[opt_tok].get("ltp", 0))
            if ltp <= 0:
                ltp = feed.get_strike_ltp(ot, stk)
            if ltp <= 0:
                ltp = _safe_float(s.get("ref_option_price", 0))
            if ltp <= 0:
                dist = (spot - stk) if ot == "CE" else (stk - spot)
                ltp = max(round(max(0.0, dist) + step * 0.4, 2), 4.0)

            c_info = opt_piv_cache.get((ot, stk, exp_item)) or opt_piv_cache.get((ot, stk), {})
            opt_p = _safe_float(c_info.get("P", 0)) or _safe_float(s.get("target_1", 0))
            opt_s1 = _safe_float(c_info.get("S1", 0)) or round(opt_p * 0.75, 2)
            opt_s2 = _safe_float(c_info.get("S2", 0)) or round(opt_p * 0.50, 2)
            opt_r1 = _safe_float(c_info.get("R1", 0)) or round(opt_p * 1.25, 2)
            opt_r2 = _safe_float(c_info.get("R2", 0)) or round(opt_p * 1.50, 2)

            planned_ep = _safe_float(s.get("entry_price", 0))
            tgt1 = _safe_float(s.get("target_1", 0))
            tgt2 = _safe_float(s.get("target_2", 0))
            tgt3 = _safe_float(s.get("target_3", 0))
            tgt4 = _safe_float(s.get("target_4", 0))
            sl_val = _safe_float(s.get("sl", 0))

            if tgt4 <= 0:
                tgt4 = round(tgt3 * 1.20, 2) if side == "BUY" else round(tgt3 * 0.80, 2)

            # Strict guarantee: Target 1 != Entry
            if side == "BUY" and tgt1 <= planned_ep:
                tgt1 = round(planned_ep * 1.25, 2)
            elif side == "SELL" and tgt1 >= planned_ep:
                tgt1 = round(planned_ep * 0.80, 2)

            # Check active open trade in forward tracker
            matched_trade = next(
                (t for t in active_trades if t.get("Type") == ot and int(t.get("Strike", 0)) == stk and t.get("Action") == side),
                None
            )
            is_active = (matched_trade is not None)

            # Check live strategy state if available
            st_inst = None
            if hasattr(feed, "strategy_states") and feed.strategy_states:
                st_inst = feed.strategy_states.get((ot, stk, exp_item, side)) or feed.strategy_states.get((ot, stk, side))

            # ── When entry is confirmed as per logic: update SL, Entry, and Targets dynamically ──
            if is_active and matched_trade:
                tr_st = "ENTRY TRIGGERED"
                entry_st = "TRIGGERED"
                tgt_st = "ACTIVE"
                sl_st = "ACTIVE"

                if matched_trade.get("Entry_Price") and _safe_float(matched_trade.get("Entry_Price")) > 0:
                    planned_ep = round(_safe_float(matched_trade.get("Entry_Price")), 2)

                # SL dynamically updated from confirmed active trade (which trails per strategy logic)
                if matched_trade.get("SL") and _safe_float(matched_trade.get("SL")) > 0:
                    sl_val = round(_safe_float(matched_trade.get("SL")), 2)
                elif st_inst and getattr(st_inst, "sl", 0) > 0:
                    sl_val = round(_safe_float(st_inst.sl), 2)

                if matched_trade.get("Target_1") and _safe_float(matched_trade.get("Target_1")) > 0:
                    tgt1 = _safe_float(matched_trade.get("Target_1"))
                if matched_trade.get("Target_2") and _safe_float(matched_trade.get("Target_2")) > 0:
                    tgt2 = _safe_float(matched_trade.get("Target_2"))
                if matched_trade.get("Target_3") and _safe_float(matched_trade.get("Target_3")) > 0:
                    tgt3 = _safe_float(matched_trade.get("Target_3"))
                if matched_trade.get("Target_4") and _safe_float(matched_trade.get("Target_4")) > 0:
                    tgt4 = _safe_float(matched_trade.get("Target_4"))

                # Real-time target hit & SL hit checks with Target 3 & Target 4
                if side == "BUY":
                    if tgt4 > 0 and ltp >= tgt4:
                        tr_st = "TARGET 4 HIT"
                    elif tgt3 > 0 and ltp >= tgt3:
                        tr_st = "TARGET 3 HIT"
                    elif tgt2 > 0 and ltp >= tgt2:
                        tr_st = "TARGET 2 HIT"
                    elif tgt1 > 0 and ltp >= tgt1:
                        tr_st = "TARGET 1 HIT"
                    elif sl_val > 0 and ltp <= sl_val:
                        tr_st = "STOP LOSS HIT"
                else: # SELL
                    if tgt4 > 0 and ltp <= tgt4:
                        tr_st = "TARGET 4 HIT"
                    elif tgt3 > 0 and ltp <= tgt3:
                        tr_st = "TARGET 3 HIT"
                    elif tgt2 > 0 and ltp <= tgt2:
                        tr_st = "TARGET 2 HIT"
                    elif tgt1 > 0 and ltp <= tgt1:
                        tr_st = "TARGET 1 HIT"
                    elif sl_val > 0 and ltp >= sl_val:
                        tr_st = "STOP LOSS HIT"
            elif st_inst and getattr(st_inst, "state", "") in ("CONFIRMED_BUY", "CONFIRMED_SELL", "TRADING_IN_FAVOR_BUY", "TRADING_IN_FAVOR_SELL"):
                tr_st = "ENTRY TRIGGERED"
                entry_st = "TRIGGERED"
                tgt_st = "ACTIVE"
                sl_st = "ACTIVE"
                if getattr(st_inst, "sl", 0) > 0:
                    sl_val = round(_safe_float(st_inst.sl), 2)
                if getattr(st_inst, "entry_price", 0) > 0:
                    planned_ep = round(_safe_float(st_inst.entry_price), 2)
                if getattr(st_inst, "targets", None) and len(st_inst.targets) >= 3:
                    tgt1 = _safe_float(st_inst.targets[0])
                    tgt2 = _safe_float(st_inst.targets[1])
                    tgt3 = _safe_float(st_inst.targets[2])
            else:
                tr_st = "WAITING FOR ENTRY"
                entry_st = "WAITING"
                tgt_st = "NOT_ACTIVE"
                sl_st = "NOT_ACTIVE"

            # Deduplicated notification tracking
            if tr_st != "WAITING FOR ENTRY":
                alert_key = (current_sym, stk, ot, side, tr_st)
                if alert_key not in ALERT_TRACKER:
                    ALERT_TRACKER.add(alert_key)
                    alert_msg = f"{tr_st}: {current_sym} {stk} {ot} @ CMP ₹{ltp:.2f} (Planned Entry: ₹{planned_ep:.2f})"
                    try:
                        asyncio.create_task(manager.broadcast({
                            "type": "TRADE_ALERT",
                            "status": tr_st,
                            "symbol": current_sym,
                            "strike": stk,
                            "type": ot,
                            "message": alert_msg
                        }))
                    except Exception:
                        pass

            tf_label = s.get("timeframe", "15M")
            is_15m = bool("15M" in tf_label)
            is_1h  = bool("1H" in tf_label)

            watch_items.append({
                "expiry": exp_item,
                "option_symbol": opt_sym,
                "token": opt_tok,
                "type": ot,
                "strike": stk,
                "side": side,
                "ltp": round(ltp, 2),
                "entry_price": planned_ep,
                "planned_ep": planned_ep,
                "oi": 0,
                "vol": 0,
                "zone": f"[{tf_label}] {s.get('calculated_level', '')}",
                "p": round(opt_p, 2),
                "s1": round(opt_s1, 2),
                "s2": round(opt_s2, 2),
                "r1": round(opt_r1, 2),
                "r2": round(opt_r2, 2),
                "m5_state": "⚡ Real-time Stream",
                "ll": round(sl_val, 2),
                "hh": round(tgt2, 2),
                "status": f"{s.get('setup')} | Entry: ₹{planned_ep:.2f} | T1: ₹{tgt1:.2f}",
                "sl": round(sl_val, 2),
                "t1": round(tgt1, 2),
                "t2": round(tgt2, 2),
                "t3": round(tgt3, 2),
                "t4": round(tgt4, 2),
                "trade_status": tr_st,
                "entry_status": entry_st,
                "target_status": tgt_st,
                "sl_status": sl_st,
                "is_entry_confirmed": bool(is_active or (st_inst and getattr(st_inst, "state", "") in ("CONFIRMED_BUY", "CONFIRMED_SELL", "TRADING_IN_FAVOR_BUY", "TRADING_IN_FAVOR_SELL"))),
                "is_15m": is_15m,
                "is_1h": is_1h,
                "rr": s.get("rr", "-"),
                "sym_p_15m": p15_v,
                "sym_s1_15m": s1_15_v,
                "sym_s2_15m": s2_15_v,
                "sym_r1_15m": r1_15_v,
                "sym_r2_15m": r2_15_v,
                "sym_p_1h": p1h_v,
                "sym_s1_1h": s1_1h_v,
                "sym_s2_1h": s2_1h_v,
                "sym_r1_1h": r1_1h_v,
                "sym_r2_1h": r2_1h_v,
            })

    # Fallback: Dynamic Focus strikes mapper (used if bypass_stored_plan=True or ALL expiries requested or no plan)
    if len(watch_items) == 0:
        needed_pe_15m = sorted([s for s in pe_15m if s and s > 0], reverse=True)[:2]
        needed_ce_15m = sorted([s for s in ce_15m if s and s > 0])[:2]
        needed_pe_1h  = sorted([s for s in pe_1h if s and s > 0 and s not in needed_pe_15m], reverse=True)[:2]
        needed_ce_1h  = sorted([s for s in ce_1h if s and s > 0 and s not in needed_ce_15m])[:2]
        strike_order = [("PE", s) for s in needed_pe_15m] + [("CE", s) for s in needed_ce_15m] + [("PE", s) for s in needed_pe_1h] + [("CE", s) for s in needed_ce_1h]

        live_map = {}
        for lw in live_all_watch:
            k = (lw.get("type", "CE"), lw.get("strike", 0), lw.get("side", "BUY"))
            live_map[k] = lw

        for exp_item in target_expiries:
            for ot, stk in strike_order:
                is_15m = bool((ot == "CE" and stk in ce_15m) or (ot == "PE" and stk in pe_15m))
                is_1h  = bool((ot == "CE" and stk in ce_1h)  or (ot == "PE" and stk in pe_1h))
                strike_tag = "[⭐15M & ⏳1H]" if (is_15m and is_1h) else ("[⭐15M Focus]" if is_15m else ("[⏳1H Focus]" if is_1h else ""))

                c_info = opt_piv_cache.get((ot, stk, exp_item)) or opt_piv_cache.get((ot, stk), {})
                opt_sym = f"{current_sym}{exp_item}{stk}{ot}" if exp_item else f"{current_sym} {stk} {ot}"
                opt_tok = ""
                if Session.scrip_df is not None and exp_item:
                    try:
                        df = Session.scrip_df
                        sub = df[(df["name"] == current_sym) & (df["expiry"] == exp_item) & (df["symbol"].str.endswith(ot))]
                        sub_stk = sub[(sub["strike"].astype(float) / 100.0).round().astype(int) == stk]
                        if not sub_stk.empty:
                            opt_tok = str(sub_stk.iloc[0]["token"])
                            opt_sym = str(sub_stk.iloc[0]["symbol"])
                    except Exception:
                        pass

                dist = (spot - stk) if ot == "CE" else (stk - spot)
                time_mult = 0.6 if (exp_item == next_exp and next_exp != curr_exp) else 0.4
                fallback_ltp = max(round(max(0.0, dist) + step * time_mult, 2), 4.0)

                for side in ["BUY", "SELL"]:
                    existing = live_map.get((ot, stk, side))
                    ltp = 0.0
                    if opt_tok and opt_tok in snap:
                        ltp = _safe_float(snap[opt_tok].get("ltp", 0))
                    if ltp <= 0 and existing and existing.get("expiry") == exp_item:
                        ltp = _safe_float(existing.get("ltp", 0))
                    if ltp <= 0:
                        ltp = _safe_float(c_info.get("prev_c", 0)) or fallback_ltp

                    oi = int(existing.get("oi", 0)) if existing else 0
                    vol = int(existing.get("vol", 0)) if existing else 0
                    m5_st = existing.get("m5_state", "⏳ Waiting") if existing else "⏳ Waiting"
                    tr_st = existing.get("trade_status", "Pending") if existing else "Pending"

                    ll = _safe_float(c_info.get("prev_l", 0) or (existing.get("ll", 0) if existing else 0))
                    if ll <= 0: ll = round(ltp * 0.88, 2)
                    hh = _safe_float(c_info.get("prev_h", 0) or (existing.get("hh", 0) if existing else 0))
                    if hh <= 0: hh = round(ltp * 1.15, 2)

                    opt_p = _safe_float(c_info.get("P", 0) or (existing.get("p", 0) if existing else 0))
                    opt_s1 = _safe_float(c_info.get("S1", 0) or (existing.get("s1", 0) if existing else 0))
                    opt_s2 = _safe_float(c_info.get("S2", 0) or (existing.get("s2", 0) if existing else 0))
                    opt_r1 = _safe_float(c_info.get("R1", 0) or (existing.get("r1", 0) if existing else 0))
                    opt_r2 = _safe_float(c_info.get("R2", 0) or (existing.get("r2", 0) if existing else 0))

                    if opt_p <= 0:
                        opt_p = round(max(ltp * 1.05, 5.0), 2)
                        opt_s1 = round(max(opt_p * 0.75, 2.0), 2)
                        opt_s2 = round(max(opt_p * 0.50, 0.05), 2)
                        opt_r1 = round(opt_p * 1.25, 2)
                        opt_r2 = round(opt_p * 1.50, 2)

                    opt_sl = _safe_float(existing.get("sl", 0) if existing else 0)
                    opt_s3 = _safe_float(c_info.get("S3", 0) or (existing.get("s3", 0) if existing else 0))
                    opt_r3 = _safe_float(c_info.get("R3", 0) or (existing.get("r3", 0) if existing else 0))

                    if side == "BUY":
                        if opt_s2 > 0.05:
                            planned_ep = round(opt_s2, 2)
                            lvl_nm = "S2"
                            t1_val = opt_s1 if opt_s1 > planned_ep else opt_p
                            t2_val = opt_p if opt_p > t1_val else opt_r1
                            t3_val = opt_r1 if opt_r1 > t2_val else opt_r2
                        else:
                            planned_ep = round(opt_s1 if opt_s1 > 0.05 else max(opt_p * 0.70, 2.0), 2)
                            lvl_nm = "S1"
                            t1_val = opt_p if opt_p > planned_ep else round(planned_ep * 1.25, 2)
                            t2_val = opt_r1 if opt_r1 > t1_val else round(t1_val * 1.20, 2)
                            t3_val = opt_r2 if opt_r2 > t2_val else round(t2_val * 1.20, 2)

                        if t1_val <= planned_ep:
                            t1_val = round(max(opt_p, planned_ep * 1.25), 2)
                        if t2_val <= t1_val:
                            t2_val = round(max(opt_r1, t1_val * 1.20), 2)
                        if t3_val <= t2_val:
                            t3_val = round(max(opt_r2, t2_val * 1.20), 2)

                        t4_val = opt_r3 if opt_r3 > t3_val else round(t3_val * 1.20, 2)
                        if t4_val <= t3_val:
                            t4_val = round(t3_val * 1.20, 2)

                        sl_cand = opt_sl if opt_sl > 0 else ll
                        if 0 < sl_cand < planned_ep:
                            sl_val = sl_cand
                        elif 0 < opt_s3 < planned_ep:
                            sl_val = opt_s3
                        else:
                            sl_val = round(planned_ep * 0.88, 2)

                        zone_txt = f"{strike_tag} Buy Zone".strip()
                        sig_status = f"★ Plan BUY @ {lvl_nm}:{planned_ep:.1f} | LL SL (Waiting)" if tr_st != "Active" else f"★ BUY @ CMP:{ltp:.2f} | LL SL:{sl_val:.1f}"
                    else:
                        if opt_r2 > 1.0:
                            planned_ep = round(opt_r2, 2)
                            lvl_nm = "R2"
                            t1_val = opt_r1 if opt_r1 < planned_ep else opt_p
                            t2_val = opt_p if opt_p < t1_val else opt_s1
                            t3_val = opt_s1 if opt_s1 < t2_val else opt_s2
                        else:
                            planned_ep = round(opt_r1 if opt_r1 > 1.0 else max(opt_p * 1.25, 5.0), 2)
                            lvl_nm = "R1"
                            t1_val = opt_p if opt_p < planned_ep else round(planned_ep * 0.80, 2)
                            t2_val = opt_s1 if opt_s1 < t1_val else round(t1_val * 0.80, 2)
                            t3_val = opt_s2 if opt_s2 < t2_val else round(t2_val * 0.80, 2)

                        if t1_val >= planned_ep:
                            t1_val = round(min(opt_p, planned_ep * 0.80), 2)
                        if t2_val >= t1_val:
                            t2_val = round(min(opt_s1, t1_val * 0.80), 2)
                        if t3_val >= t2_val:
                            t3_val = round(min(opt_s2, t2_val * 0.80), 2)

                        t4_val = opt_s3 if 0 < opt_s3 < t3_val else round(t3_val * 0.80, 2)
                        if t4_val >= t3_val or t4_val <= 0:
                            t4_val = round(t3_val * 0.80, 2)

                        sl_cand = opt_sl if opt_sl > 0 else hh
                        if sl_cand > planned_ep:
                            sl_val = sl_cand
                        elif opt_r3 > planned_ep:
                            sl_val = opt_r3
                        else:
                            sl_val = round(planned_ep * 1.15, 2)

                        zone_txt = f"{strike_tag} Sell Zone".strip()
                        sig_status = f"▼ Plan SELL @ {lvl_nm}:{planned_ep:.1f} | HH SL (Waiting)" if tr_st != "Active" else f"▼ SELL @ CMP:{ltp:.2f} | HH SL:{sl_val:.1f}"

                    watch_items.append({
                        "expiry": exp_item,
                        "option_symbol": opt_sym,
                        "token": opt_tok,
                        "type": ot,
                        "strike": stk,
                        "side": side,
                        "ltp": round(ltp, 2),
                        "entry_price": planned_ep,
                        "planned_ep": planned_ep,
                        "oi": oi,
                        "vol": vol,
                        "zone": zone_txt,
                        "p": opt_p,
                        "s1": opt_s1,
                        "s2": opt_s2,
                        "r1": opt_r1,
                        "r2": opt_r2,
                        "m5_state": m5_st,
                        "ll": ll,
                        "hh": hh,
                        "status": sig_status,
                        "sl": round(sl_val, 2),
                        "t1": round(t1_val, 2),
                        "t2": round(t2_val, 2),
                        "t3": round(t3_val, 2),
                        "t4": round(t4_val, 2),
                        "trade_status": tr_st,
                        "is_entry_confirmed": bool(tr_st == "Active"),
                        "is_15m": is_15m,
                        "is_1h": is_1h,
                        "sym_p_15m": p15_v,
                        "sym_s1_15m": s1_15_v,
                        "sym_s2_15m": s2_15_v,
                        "sym_r1_15m": r1_15_v,
                        "sym_r2_15m": r2_15_v,
                        "sym_p_1h": p1h_v,
                        "sym_s1_1h": s1_1h_v,
                        "sym_s2_1h": s2_1h_v,
                        "sym_r1_1h": r1_1h_v,
                        "sym_r2_1h": r2_1h_v,
                    })

    # Per-symbol breakdown
    sym_breakdown = {}
    for r in closed_trades_raw:
        s = r.get("Symbol", current_sym)
        sym_breakdown[s] = sym_breakdown.get(s, 0.0) + _safe_float(r.get("PnL_Pts", 0))
    for t in active_trades:
        s = t.get("Symbol", current_sym)
        sym_breakdown[s] = sym_breakdown.get(s, 0.0) + _safe_float(t.get("PnL_Pts", t.get("Live_PnL", 0)))

    # Max OI text
    max_oi_text = ""
    if snap and meta:
        oi_per_exp = {}
        for token, m in meta.items():
            if token not in snap: continue
            oi_v = snap[token]["oi"]
            s = m["strike"]
            exp = m.get("expiry", "-")
            ot = m["opt_type"]
            oi_per_exp.setdefault(exp, {}).setdefault(s, {"CE": 0, "PE": 0})
            oi_per_exp[exp][s][ot] = oi_v

        parts = []
        for exp_k in sorted(oi_per_exp):
            exp_data = oi_per_exp[exp_k]
            mc_s = max(exp_data, key=lambda s: exp_data[s].get("CE", 0), default=None)
            mp_s = max(exp_data, key=lambda s: exp_data[s].get("PE", 0), default=None)
            mc_v = exp_data[mc_s]["CE"] if mc_s else 0
            mp_v = exp_data[mp_s]["PE"] if mp_s else 0
            mc_f = f"{mc_v/100000:.1f}L" if mc_v >= 100000 else f"{mc_v/1000:.0f}K"
            mp_f = f"{mp_v/100000:.1f}L" if mp_v >= 100000 else f"{mp_v/1000:.0f}K"
            parts.append(f"{exp_k} ➜ CE MaxOI@{mc_s}({mc_f}) | PE MaxOI@{mp_s}({mp_f})")
        max_oi_text = "  ||  ".join(parts)

    return {
        "type": "TICK_UPDATE",
        "symbol": current_sym,
        "spot": spot,
        "spot_zone": sp_zone,
        "symbol_pivots": {
            "p15": {"P": p15_v, "S1": s1_15_v, "S2": s2_15_v, "S3": s3_15_v, "R1": r1_15_v, "R2": r2_15_v, "R3": r3_15_v},
            "p1h": {"P": p1h_v, "S1": s1_1h_v, "S2": s2_1h_v, "S3": s3_1h_v, "R1": r1_1h_v, "R2": r2_1h_v, "R3": r3_1h_v},
            "daily": mtf.get("Daily", {}),
            "weekly": mtf.get("Weekly", {}),
            "ce_15m": sorted(list(ce_15m)),
            "pe_15m": sorted(list(pe_15m)),
            "ce_1h": sorted(list(ce_1h)),
            "pe_1h": sorted(list(pe_1h)),
        },
        "reference_levels": {
            "daily_fib": mtf.get("Daily", {}),
            "weekly_fib": mtf.get("Weekly", {}),
        },
        "active_plan": {
            "plan_id": active_plan.get("plan_id", "") if active_plan else "",
            "target_trading_date": active_plan.get("target_trading_date", "") if active_plan else "",
            "generated_at": active_plan.get("generated_at", "") if active_plan else "",
            "status": active_plan.get("status", "NO_STORED_PLAN") if active_plan else "NO_STORED_PLAN",
            "setups_count": len(active_plan.get("setups", [])) if active_plan else 0,
        },
        "feed_running": feed.running,
        "exec_mode": live_order_manager.mode,
        "timestamp": now_time,
        "scorecard": scorecard,
        "sym_breakdown": sym_breakdown,
        "watch_items": watch_items,
        "max_oi": max_oi_text,
        "current_expiry": curr_exp,
        "next_expiry": next_exp,
        "active_expiry": active_exp,
        "expiries": expiries_list,
        "active_trades": active_trades,
        "closed_trades": list(reversed(closed_with_cum))
    }

# ── Background Broadcast Worker ────────────────────────────────
async def broadcast_loop():
    """Broadcasts 100-200ms real-time state to connected WebSocket clients."""
    while True:
        try:
            if manager.active_connections:
                req_sym = getattr(feed, 'symbol', 'NIFTY')
                req_exp = getattr(feed, 'target_expiry', None) or getattr(feed, 'expiry', None)
                payload = get_dashboard_payload(requested_sym=req_sym, requested_exp=req_exp)
                await manager.broadcast(payload)
        except Exception as e:
            print(f"[WS BROADCAST] error: {e}")
        await asyncio.sleep(0.1)   # 100ms ultra-fast real-time tick interval

# ── Background EOD Scheduler (3:00 PM Square-off & 3:45 PM Next-Day) ──
def background_eod_scheduler():
    while True:
        try:
            now_dt = datetime.now()
            today_date = now_dt.strftime("%Y-%m-%d")
            is_weekday = now_dt.weekday() < 5

            # 1. 3:00 PM Intraday Square-off at CMP
            if (is_weekday and now_dt.time() >= EOD_SQUARE_OFF_TIME and
                    getattr(feed, '_web_eod_sq_date', None) != today_date):
                feed._web_eod_sq_date = today_date
                try:
                    sq = feed.forward_tracker.eod_square_off("EOD 3PM Square-off @ CMP")
                    if sq:
                        print(f"\n[WEB EOD 3PM] ⏰ 15:00:00 → Squared off {len(sq)} open trade(s) at CMP")
                        feed.sync_forward_test_to_sheet()
                        feed.sync_multi_symbol_sheet()
                except Exception as sq_err:
                    print(f"[WEB EOD 3PM] error: {sq_err}")

            # 2. 3:45 PM Next-Day Level Calculation & EOD Planner
            if (is_weekday and now_dt.time() >= NEXT_DAY_LEVELS_TIME and
                    getattr(feed, '_web_eod_plan_date', None) != today_date):
                feed._web_eod_plan_date = today_date
                try:
                    sym = feed.symbol or "NIFTY"
                    print(f"\n[WEB EOD 3:45PM] ⏰ 15:45:00 → Calculating next-day levels for {sym}...")
                    mtf = get_tv_multi_timeframe_pivots(sym, force_refresh=True)
                    if mtf:
                        with feed.lock:
                            feed.mtf_pivots = mtf
                        feed.fetch_and_map_all_strikes(sym, None, force_fetch=True)
                    feed.sync_multi_symbol_sheet()
                    print(f"[WEB EOD 3:45PM] ✅ Next-day levels cached & synced to Google Sheets.")
                    
                    # Store Next-Day Plan directly in persistent JSON
                    plan_data = generate_and_store_next_day_plan(sym, force_refresh=True)
                    print(f"[WEB EOD 3:45PM] ⏰ Next-Day Plan generated & stored for target date: {plan_data.get('target_trading_date')}\n")
                except Exception as nd_err:
                    print(f"[WEB EOD 3:45PM] next-day level calc error: {nd_err}")

        except Exception as e:
            print(f"[EOD SCHEDULER] error: {e}")
        time.sleep(1.0)

# ── Startup Event ──────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    print("\n" + "═"*60)
    print("🚀 QUANTUM TERMINAL — WEB DASHBOARD BACKEND INITIALIZING")
    print("═"*60)

    # Angel One credentials check
    c_id = getattr(engine, "CLIENT_ID", "")
    mpin = getattr(engine, "MPIN", "")
    if not c_id or not mpin:
        print("[STARTUP NOTICE] SmartAPI credentials missing in environment. Running in Offline Paper mode.")
    else:
        def _login_bg():
            try:
                print("[STARTUP] Logging in to Angel One...")
                Session.login()
                print("[STARTUP] Loading ScripMaster...")
                Session.load_scrip()
                print(f"[STARTUP] ✅ Loaded {len(Session.scrip_df)} instruments into memory")
            except Exception as ex:
                print(f"[STARTUP LOGIN ERROR] {ex}")
        threading.Thread(target=_login_bg, daemon=True).start()

    # Pre-generate or load next-day plan on startup
    try:
        generate_and_store_next_day_plan("NIFTY", force_refresh=False)
    except Exception as _p_err:
        print(f"[STARTUP PLAN INIT NOTICE] {_p_err}")

    # Start WebSocket broadcast background task
    asyncio.create_task(broadcast_loop())

    # Start EOD timer scheduler in separate thread
    threading.Thread(target=background_eod_scheduler, daemon=True).start()
    print("✅ Web Server & Schedulers Online — Open http://127.0.0.1:8055 in your browser\n")

# ── ROUTES ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = os.path.join(WEB_DIR, "index.html")
    with open(index_file, "r") as f:
        return f.read()

@app.get("/api/dashboard")
async def get_dashboard(symbol: str = None, expiry: str = None):
    """Instant HTTP snapshot of the entire dashboard."""
    if symbol:
        feed.symbol = symbol.strip().upper()
    if expiry:
        req_u = expiry.strip().upper()
        feed.target_expiry = req_u
        if req_u not in ["NEXT", "CURRENT"]:
            feed.expiry = req_u
    return get_dashboard_payload(requested_sym=symbol or getattr(feed, 'symbol', 'NIFTY'),
                                 requested_exp=expiry or getattr(feed, 'target_expiry', None))

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Immediate welcome packet
        await websocket.send_json(get_dashboard_payload())
        while True:
            # Keep alive and receive any client ping
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

@app.get("/api/expiries")
async def get_expiries(symbol: str = "NIFTY"):
    symbol = symbol.strip().upper()
    expiries = []
    if Session.scrip_df is not None:
        try:
            df = Session.scrip_df
            raw_exp = df[(df["name"] == symbol) & (df["strike"].astype(float) > 0)]["expiry"].unique()
            if len(raw_exp) > 0:
                today_d = datetime.now().date()
                all_sorted = sorted(raw_exp, key=lambda e: datetime.strptime(e, "%d%b%Y") if e else datetime.max)
                fut_sorted = [e for e in all_sorted if datetime.strptime(e, "%d%b%Y").date() >= today_d]
                expiries = fut_sorted if fut_sorted else all_sorted
        except Exception:
            pass
    return {"symbol": symbol, "expiries": list(expiries)}

@app.post("/api/start-feed")
async def start_feed(req: StartFeedRequest):
    sym = (req.symbol or getattr(feed, "symbol", "NIFTY") or "NIFTY").strip().upper()
    req_exp = (req.expiry or "").strip().upper()
    exch = (req.exchange or "NFO").strip().upper()
    n_strikes = int(req.n_strikes or 8)

    # 1. Resolve available expiries from scrip_df
    expiries_list = []
    if Session.scrip_df is not None:
        try:
            df = Session.scrip_df
            raw = df[(df["name"] == sym) & (df["strike"].astype(float) > 0)]["expiry"].unique()
            if len(raw) > 0:
                today_d = datetime.now().date()
                all_s = sorted(raw, key=lambda e: datetime.strptime(e, "%d%b%Y") if e else datetime.max)
                fut_s = [e for e in all_s if datetime.strptime(e, "%d%b%Y").date() >= today_d]
                expiries_list = fut_s if fut_s else all_s
        except Exception:
            pass

    curr_exp = expiries_list[0] if expiries_list else (getattr(feed, "expiry", "") or "06OCT2026")
    next_exp = expiries_list[1] if len(expiries_list) > 1 else curr_exp

    if not req_exp or req_exp in ["CURRENT", ""]:
        target_exp = curr_exp
        exp_list = [curr_exp]
    elif req_exp == "NEXT":
        target_exp = next_exp
        exp_list = [next_exp]
    elif req_exp == "ALL":
        target_exp = "ALL"
        exp_list = [e for e in [curr_exp, next_exp] if e]
    elif req_exp in expiries_list:
        target_exp = req_exp
        exp_list = [req_exp]
    else:
        target_exp = curr_exp
        exp_list = [curr_exp]

    feed.symbol = sym
    feed.expiry = curr_exp
    feed.target_expiry = target_exp
    if next_exp:
        feed.next_expiry = next_exp

    try:
        def _worker():
            try:
                feed.running = True
                print(f"[WEB FEED] Preparing tokens for {exch}/{sym}/{exp_list}...")
                strikes_list = []
                try:
                    filter_df, atm, step = feed.prepare(exch, sym, exp_list, n_strikes)
                    if filter_df is not None and not filter_df.empty:
                        strikes_list = sorted(set(filter_df["strike"].astype(float).div(100).astype(int)))
                except Exception as prep_err:
                    print(f"[WEB FEED] prepare notice: {prep_err}")

                if not strikes_list and hasattr(feed, 'all_strikes') and feed.all_strikes:
                    strikes_list = sorted(list(set(feed.all_strikes)))
                if not strikes_list:
                    strikes_list = [22400]

                try:
                    feed.fetch_and_map_all_strikes(symbol=sym, expiry=(target_exp if target_exp != "ALL" else None), force_fetch=True)
                except Exception as map_err:
                    print(f"[WEB FEED] strike map notice: {map_err}")

                try:
                    feed.setup_sheet(sym, strikes_list)
                except Exception as sh_err:
                    print(f"[WEB FEED] Google sheet setup notice: {sh_err}")

                try:
                    feed.start_ws()
                except Exception as ws_err:
                    print(f"[WEB FEED] WebSocket start notice: {ws_err}")

                time.sleep(0.5)

                try:
                    feed.update_sheet_loop(sym, strikes_list)
                except Exception as loop_err:
                    print(f"[WEB FEED] Sheet loop notice: {loop_err}")

                # Keep thread alive to sustain feed
                while feed.running:
                    time.sleep(1.0)
            except Exception as ex:
                feed.running = False
                print(f"[WEB FEED ERROR] {ex}")
                import traceback
                traceback.print_exc()

        threading.Thread(target=_worker, daemon=True).start()
        return {"success": True, "message": f"Feed started for {sym} {target_exp}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/stop-feed")
async def stop_feed():
    feed.running = False
    feed.stop_ws()
    return {"success": True, "message": "Live feed stopped"}

@app.post("/api/trades/square-off")
async def square_off_all():
    """Manual trigger for 3:00 PM Square-off at CMP."""
    try:
        closed = feed.forward_tracker.eod_square_off("EOD 3PM Square-off @ CMP")
        net = sum(_safe_float(c.get("PnL_Pts", 0)) for c in closed)
        feed.sync_forward_test_to_sheet()
        feed.sync_multi_symbol_sheet()
        return {"success": True, "closed_count": len(closed), "net_pts": round(net, 2)}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/trades/manual-close")
async def manual_close_trade(req: ManualCloseRequest):
    try:
        ltp = feed.get_strike_ltp(req.type, req.strike)
        res = feed.forward_tracker.manual_close(
            opt_type=req.type, strike=req.strike, ltp=ltp,
            reason="Manual Web Exit", expiry=req.expiry, symbol=req.symbol
        )
        feed.sync_forward_test_to_sheet()
        feed.sync_multi_symbol_sheet()
        return {"success": True, "trade": res}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/planner")
async def get_planner_deck(symbol: str = "NIFTY", force_refresh: bool = False):
    """
    Next-Day Strategy Deck computed strictly from 15-Minute and 1-Hour Intraday levels.
    Zero Daily Pivot dependency. Stores plan for the next actual trading session.
    """
    sym = (symbol or "NIFTY").strip().upper()
    try:
        plan = get_stored_plan(sym)
        if not plan or force_refresh or not plan.get("setups"):
            plan = generate_and_store_next_day_plan(sym, force_refresh=force_refresh)

        # Enrich each setup with latest live LTP from snapshot
        snap = dict(feed.live_data)
        for s in plan.get("setups", []):
            tok = str(s.get("token", ""))
            ltp = 0.0
            if tok and tok in snap:
                ltp = _safe_float(snap[tok].get("ltp", 0))
            if ltp <= 0:
                ltp = feed.get_strike_ltp(s.get("type", "CE"), int(s.get("strike", 0)))
            if ltp <= 0:
                ltp = _safe_float(s.get("ref_option_price", 0))
            s["live_cmp"] = round(ltp, 2)

        return plan
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/planner/generate")
async def trigger_eod_planner(req: dict = None):
    """Force re-computes and stores next-day plan for the target trading session."""
    try:
        sym = "NIFTY"
        if req and isinstance(req, dict):
            sym = req.get("symbol", "NIFTY")
        plan = generate_and_store_next_day_plan(sym, force_refresh=True)

        snap = dict(feed.live_data)
        for s in plan.get("setups", []):
            tok = str(s.get("token", ""))
            ltp = 0.0
            if tok and tok in snap:
                ltp = _safe_float(snap[tok].get("ltp", 0))
            if ltp <= 0:
                ltp = feed.get_strike_ltp(s.get("type", "CE"), int(s.get("strike", 0)))
            if ltp <= 0:
                ltp = _safe_float(s.get("ref_option_price", 0))
            s["live_cmp"] = round(ltp, 2)

        return plan
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/planner/apply")
async def apply_planner_to_live_scanner(req: dict = None):
    """Explicitly applies/activates stored next-day plan into Live Scanner."""
    try:
        sym = "NIFTY"
        if req and isinstance(req, dict):
            sym = req.get("symbol", "NIFTY").strip().upper()
        plan = get_stored_plan(sym)
        if not plan or not plan.get("setups"):
            plan = generate_and_store_next_day_plan(sym, force_refresh=True)
        return {
            "success": True,
            "symbol": sym,
            "target_trading_date": plan.get("target_trading_date"),
            "setups_count": len(plan.get("setups", [])),
            "message": f"Next-Day Plan for {sym} ({plan.get('target_trading_date')}) active in Live Scanner"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8055))
    uvicorn.run("web_server:app", host="0.0.0.0", port=port, reload=False)
