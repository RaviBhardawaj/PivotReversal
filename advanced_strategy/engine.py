"""
Advanced Strategy Engine
Core orchestrator for ADVANCED_PIVOT_REVERSAL:
Evaluates candidates against Filters A-G, manages trade tracking,
and synchronizes with Google Sheets. Sends NO alerts or notifications.
"""

import os
import json
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional, Tuple

from .config import (
    STRATEGY_ID,
    ADVANCED_SHEET_TAB_NAME,
    ADVANCED_REQUIRE_RETEST,
    ADVANCED_ALLOW_P_CONTINUATION,
    ADVANCED_NEXT_DAY_PLAN_FILE,
    ADVANCED_MIN_RR_RATIO,
    ADVANCED_REJECT_ON_CONTEXT_CONFLICT,
    BASE_DIR,
)
from .filters import (
    evaluate_context,
    evaluate_liquidity_sweep,
    evaluate_candle_confirmation,
    evaluate_retest,
    evaluate_risk_reward,
    evaluate_option_liquidity,
    evaluate_no_trade_conditions,
)
from .tracker import AdvancedStrategyTracker
from .sheets import AdvancedGoogleSheetsManager


def snap_strike(val: float, step: int = 50) -> int:
    return int(round(val / step) * step) if val else 0


def get_symbol_step(symbol: str) -> int:
    s = (symbol or "").upper()
    if "BANK" in s or "SENSEX" in s:
        return 100
    if "MIDCP" in s:
        return 25
    return 50


def extract_focus_strikes_from_pivots(pivots: Dict[str, float], step: int = 50) -> Dict[str, Any]:
    if not pivots:
        return {}
    r1 = float(pivots.get("R1", 0))
    r2 = float(pivots.get("R2", 0))
    r3 = float(pivots.get("R3", 0))
    s1 = float(pivots.get("S1", 0))
    s2 = float(pivots.get("S2", 0))
    s3 = float(pivots.get("S3", 0))
    p  = float(pivots.get("P", 0))

    ce1, ce2 = snap_strike(r1, step), snap_strike(r2, step)
    ce3 = snap_strike(r3, step) if r3 else 0
    pe1, pe2 = snap_strike(s1, step), snap_strike(s2, step)
    pe3 = snap_strike(s3, step) if s3 else 0

    if ce2 == ce1 and ce1 > 0:
        ce2 = ce1 + step
    if pe2 == pe1 and pe1 > 0:
        pe2 = max(pe1 - step, step)
    if ce3 == ce2 and ce2 > 0:
        ce3 = ce2 + step
    if pe3 == pe2 and pe2 > 0:
        pe3 = max(pe2 - step, step)

    p_stk = snap_strike(p, step)
    if p > 0:
        p_lo = int(int(p // step) * step)
        p_hi = int(int((p + step - 1) // step) * step)
        if p_lo == p_hi:
            p_lo, p_hi = p_lo - step, p_hi + step
    else:
        p_lo = p_hi = 0
    p_strikes = sorted({x for x in (p_lo, p_stk, p_hi) if x > 0})

    ce_strikes = [s for s in (ce1, ce2, ce3) if s > 0]
    pe_strikes = [s for s in (pe1, pe2, pe3) if s > 0]
    all_4 = sorted(list({s for s in (ce1, ce2, pe1, pe2, *p_strikes) if s > 0}))
    all_6 = sorted(list({s for s in (ce1, ce2, ce3, pe1, pe2, pe3, *p_strikes) if s > 0}))

    return {
        "CE1": ce1, "CE2": ce2, "CE3": ce3,
        "PE1": pe1, "PE2": pe2, "PE3": pe3,
        "P_STRIKE": p_stk,
        "p_strikes": p_strikes,
        "ce_strikes": ce_strikes,
        "pe_strikes": pe_strikes,
        "all_4": all_4,
        "all_6": all_6,
    }


class AdvancedStrategyEngine:
    """
    Independent engine for the Advanced Fibonacci Pivot Reversal Strategy.
    Runs alongside the original strategy without altering its behavior or placing duplicate orders.
    """

    def __init__(self):
        self.tracker = AdvancedStrategyTracker()
        self.sheets_mgr = AdvancedGoogleSheetsManager()
        self._last_sheet_sync_ts = 0.0
        self._p_states: Dict[Tuple, Dict[str, Any]] = {} # (symbol, opt_type, strike, exp) -> state dict
        self.next_day_plan: Dict[str, Any] = self._load_persisted_plan()

    def _load_persisted_plan(self) -> Dict[str, Any]:
        try:
            if os.path.exists(ADVANCED_NEXT_DAY_PLAN_FILE):
                with open(ADVANCED_NEXT_DAY_PLAN_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def setup_sheet(self, spreadsheet) -> bool:
        """Called during sheet setup to ensure 'Advanced Strategy' tab exists."""
        return self.sheets_mgr.setup_tab(spreadsheet)

    def _get_or_create_p_state(self, symbol: str, opt_type: str, strike: int, exp: str) -> Dict[str, Any]:
        key = (symbol, opt_type, strike, exp)
        if key not in self._p_states:
            self._p_states[key] = {
                "state": "IDLE",
                "ll": 0.0,
                "hh": 0.0,
                "retest_done": False,
                "retest_low": 0.0,
                "retest_high": 0.0,
                "last_bucket": -1,
            }
        return self._p_states[key]

    def update_pivot_p_continuation(
        self,
        symbol: str,
        opt_type: str,
        strike: int,
        expiry: str,
        ltp: float,
        spot_px: float,
        daily_pivots: Dict[str, float],
        weekly_pivots: Dict[str, float],
        opt_piv: Dict[str, float],
        candle_5m: Dict[str, Any],
        bid: float = 0.0,
        ask: float = 0.0,
        quote_age_s: float = 0.0,
        lot_size: int = 25,
        token: str = ""
    ):
        """
        Tracks and evaluates Section 5 C & D:
        BUY Continuation at Pivot P & SELL Continuation at Pivot P.
        """
        if not ADVANCED_ALLOW_P_CONTINUATION or not opt_piv or ltp <= 0:
            return

        p = opt_piv.get("P", 0.0)
        s1 = opt_piv.get("S1", 0.0)
        s2 = opt_piv.get("S2", 0.0)
        r1 = opt_piv.get("R1", 0.0)
        r2 = opt_piv.get("R2", 0.0)
        r3 = opt_piv.get("R3", round(r2 + (r2 - r1), 2))
        s3 = opt_piv.get("S3", round(max(s2 - (s1 - s2), 0.05), 2))

        if p <= 0:
            return

        pst = self._get_or_create_p_state(symbol, opt_type, strike, expiry)
        st = pst["state"]

        # BUY Continuation: Price approaches P from below, sweeps below P (LL), reclaims P,
        # closed 5M candle confirms bullish above P, Target 1 = R1, Target 2 = R2
        if ltp < p and ltp > s2:  # option has NOT reached S2
            if st in ("IDLE", "REJECT_P"):
                pst["state"] = "SWEEP_BELOW_P"
                pst["ll"] = ltp
                pst["retest_done"] = False
            elif st == "SWEEP_BELOW_P":
                pst["ll"] = min(pst["ll"], ltp)

        elif ltp >= p and st == "SWEEP_BELOW_P":
            # Reclaimed P
            pst["state"] = "RECLAIM_P"
            # Wick into P tolerance counts as retest
            if abs(ltp - p) / p <= 0.015:
                pst["retest_done"] = True
                pst["retest_low"] = ltp

        elif st == "RECLAIM_P":
            if abs(ltp - p) / p <= 0.015:
                pst["retest_done"] = True
                pst["retest_low"] = min(pst.get("retest_low", ltp) or ltp, ltp)

            # Check for confirmed entry on 5M close above P
            c_bucket = candle_5m.get("bucket", -1)
            c_close = candle_5m.get("close", 0.0)
            now = datetime.now()
            curr_b = (now.hour * 60 + now.minute) // 5

            if curr_b > c_bucket and c_close > p and pst["ll"] > 0:
                # Trigger candidate evaluation
                sl_c = pst["ll"]
                t1_c = r1
                t2_c = r2
                t3_c = r3
                qual, reason, trade = self.evaluate_candidate(
                    symbol=symbol,
                    opt_type=opt_type,
                    strike=strike,
                    expiry=expiry,
                    action="BUY",
                    setup_type="BUY Continuation at Pivot P",
                    ltp=ltp,
                    spot_px=spot_px,
                    daily_pivots=daily_pivots,
                    weekly_pivots=weekly_pivots,
                    opt_piv=opt_piv,
                    ll=pst["ll"],
                    hh=0.0,
                    candle_5m=candle_5m,
                    retest_done=pst["retest_done"],
                    retest_low=pst.get("retest_low", ltp),
                    retest_high=0.0,
                    sl_candidate=sl_c,
                    t1_candidate=t1_c,
                    t2_candidate=t2_c,
                    t3_candidate=t3_c,
                    bid=bid,
                    ask=ask,
                    quote_age_s=quote_age_s,
                    source_order_id="",
                    trade_mode="Shadow",
                    lot_size=lot_size,
                    token=token
                )
                pst["state"] = "IDLE"  # Reset state after evaluation

        # SELL Continuation: Price approaches P from above, sweeps above P (HH), rejects P,
        # closed 5M candle confirms bearish below P, Target 1 = S1, Target 2 = S2
        if ltp > p and ltp < r2:  # option has NOT reached R2
            if st in ("IDLE", "RECLAIM_P"):
                pst["state"] = "SWEEP_ABOVE_P"
                pst["hh"] = ltp
                pst["retest_done"] = False
            elif st == "SWEEP_ABOVE_P":
                pst["hh"] = max(pst["hh"], ltp)

        elif ltp <= p and st == "SWEEP_ABOVE_P":
            # Rejected P
            pst["state"] = "REJECT_P"
            if abs(ltp - p) / p <= 0.015:
                pst["retest_done"] = True
                pst["retest_high"] = ltp

        elif st == "REJECT_P":
            if abs(ltp - p) / p <= 0.015:
                pst["retest_done"] = True
                pst["retest_high"] = max(pst.get("retest_high", ltp) or ltp, ltp)

            c_bucket = candle_5m.get("bucket", -1)
            c_close = candle_5m.get("close", 0.0)
            now = datetime.now()
            curr_b = (now.hour * 60 + now.minute) // 5

            if curr_b > c_bucket and c_close < p and pst["hh"] > 0:
                sl_c = pst["hh"]
                t1_c = s1
                t2_c = s2
                t3_c = s3
                qual, reason, trade = self.evaluate_candidate(
                    symbol=symbol,
                    opt_type=opt_type,
                    strike=strike,
                    expiry=expiry,
                    action="SELL",
                    setup_type="SELL Continuation at Pivot P",
                    ltp=ltp,
                    spot_px=spot_px,
                    daily_pivots=daily_pivots,
                    weekly_pivots=weekly_pivots,
                    opt_piv=opt_piv,
                    ll=0.0,
                    hh=pst["hh"],
                    candle_5m=candle_5m,
                    retest_done=pst["retest_done"],
                    retest_low=0.0,
                    retest_high=pst.get("retest_high", ltp),
                    sl_candidate=sl_c,
                    t1_candidate=t1_c,
                    t2_candidate=t2_c,
                    t3_candidate=t3_c,
                    bid=bid,
                    ask=ask,
                    quote_age_s=quote_age_s,
                    source_order_id="",
                    trade_mode="Shadow",
                    lot_size=lot_size,
                    token=token
                )
                pst["state"] = "IDLE"  # Reset state after evaluation

    def evaluate_candidate(
        self,
        symbol: str,
        opt_type: str,
        strike: int,
        expiry: str,
        action: str,
        setup_type: str,
        ltp: float,
        spot_px: float,
        daily_pivots: Dict[str, float],
        weekly_pivots: Dict[str, float],
        opt_piv: Dict[str, float],
        ll: float,
        hh: float,
        candle_5m: Dict[str, Any],
        retest_done: bool,
        retest_low: float,
        retest_high: float,
        sl_candidate: float,
        t1_candidate: float,
        t2_candidate: float = 0.0,
        t3_candidate: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
        quote_age_s: float = 0.0,
        source_order_id: str = "",
        trade_mode: str = "Shadow",
        lot_size: int = 25,
        token: str = ""
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Runs comprehensive evaluation against Filters A through G.
        If qualified, records trade and syncs to sheet.
        If rejected, logs rejection reason.
        Returns: (is_qualified, reason_or_status, trade_dict)
        """
        now = datetime.now()

        # Check if Pivot P continuation is disabled
        if "CONTINUATION" in setup_type.upper() and not ADVANCED_ALLOW_P_CONTINUATION:
            reason = "Pivot P Continuation setup disabled in configuration"
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason)
            return False, reason, None

        # Filter G: No-trade conditions (duplicate check, etc.)
        t_key = self.tracker._trade_key(symbol, opt_type, strike, expiry, action)
        is_active = t_key in self.tracker._active_by_key
        reentry_count = self.tracker._sl_hit_count.get(t_key, 0)
        pass_g, reason_g = evaluate_no_trade_conditions(is_active, reentry_count)
        if not pass_g:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason_g)
            return False, reason_g, None

        # Filter F: Option liquidity and price quality
        pass_f, spread_desc, reason_f = evaluate_option_liquidity(ltp, bid, ask, quote_age_s)
        if not pass_f:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason_f)
            return False, reason_f, None

        # Filter A: Weekly and Daily context
        pass_a, ctx_desc, reason_a = evaluate_context(spot_px, daily_pivots, weekly_pivots, opt_type, action, setup_type)
        if not pass_a:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason_a, {"context": ctx_desc})
            return False, reason_a, None

        # Filter B: Liquidity sweep and reclaim/rejection
        level_name = "P" if "CONTINUATION" in setup_type.upper() else ("S2" if action == "BUY" else "R2")
        level_px = opt_piv.get(level_name, 0.0) if opt_piv else 0.0
        pass_b, reason_b = evaluate_liquidity_sweep(action, level_name, level_px, ll, hh, ltp)
        if not pass_b:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason_b, {"LL": ll, "HH": hh})
            return False, reason_b, None

        # Filter C: Completed 5M candle confirmation
        pass_c, conf_desc, c_metrics = evaluate_candle_confirmation(action, candle_5m, now, level_px)
        if not pass_c:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, conf_desc, c_metrics)
            return False, conf_desc, None

        # Filter D: Retest mode
        pass_d, retest_status, reason_d = evaluate_retest(retest_done, retest_low, retest_high, level_px, ll, hh, action)
        if not pass_d:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason_d, {"status": retest_status})
            return False, reason_d, None

        # Filter E: Risk / Reward check
        pass_e, reason_e, rr_metrics = evaluate_risk_reward(ltp, sl_candidate, t1_candidate, action, lot_size)
        if not pass_e:
            self.tracker.log_rejection(symbol, opt_type, strike, expiry, action, setup_type, ltp, reason_e, rr_metrics)
            return False, reason_e, None

        # ALL FILTERS PASSED -> QUALIFY TRADE
        rr_str = f"1:{rr_metrics.get('rr_ratio', 0.0):.2f}"
        spot_zone = ctx_desc

        if action == "BUY":
            swp = f"Sweep LL:{ll:.2f}" if ll > 0 else "Support Dip"
            ret = f" | Retest:{retest_status}" if retest_status else ""
            ctx = f" | Context:{ctx_desc}" if ctx_desc else ""
            entry_reason_str = f"★ {setup_type} [{swp} | Reclaimed @ {ltp:.2f} | 5M Bullish Candle Closed | SL:{sl_candidate:.2f} | T1:{t1_candidate:.2f}{ctx}{ret}]"
        else:
            swp = f"Sweep HH:{hh:.2f}" if hh > 0 else "Resistance Test"
            ret = f" | Retest:{retest_status}" if retest_status else ""
            ctx = f" | Context:{ctx_desc}" if ctx_desc else ""
            entry_reason_str = f"▼ {setup_type} [{swp} | Rejected @ {ltp:.2f} | 5M Bearish Candle Closed | SL:{sl_candidate:.2f} | T1:{t1_candidate:.2f}{ctx}{ret}]"

        trade = self.tracker.log_qualified_trade(
            symbol=symbol,
            spot_px=spot_px,
            spot_zone=spot_zone,
            opt_symbol=f"{symbol} {strike} {opt_type}",
            opt_type=opt_type,
            strike=strike,
            expiry=expiry,
            action=action,
            setup_type=setup_type,
            entry_px=ltp,
            sl_px=sl_candidate,
            t1_px=t1_candidate,
            t2_px=t2_candidate,
            t3_px=t3_candidate,
            opt_piv=opt_piv,
            ll=ll,
            hh=hh,
            conf_5m=conf_desc,
            retest_status=retest_status,
            context_result=ctx_desc,
            risk_reward_str=rr_str,
            spread_check_str=spread_desc,
            source_order_id=source_order_id,
            trade_mode=trade_mode,
            qty=lot_size,
            token=token,
            entry_reason=entry_reason_str
        )

        if trade:
            # Sync to Google Sheets tab (asynchronous)
            self.sheets_mgr.sync_trades_async([trade])

        return True, "QUALIFIED", trade

    def on_tick(
        self,
        symbol: str,
        opt_type: str,
        strike: int,
        expiry: str,
        ltp: float,
        broker_forced_exit: Tuple[float, str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Called on every market tick.
        Updates active trades and checks exit conditions.
        """
        closed_trade = self.tracker.update_tick(symbol, opt_type, strike, expiry, ltp, broker_forced_exit)
        if closed_trade:
            # Sync closed trade immediately to sheet
            self.sheets_mgr.sync_trades_async([closed_trade])
            return closed_trade

        # Periodic sync of active trades (throttled to at most once per 8 seconds)
        now_ts = time.time()
        if now_ts - self._last_sheet_sync_ts >= 8.0:
            active = self.tracker.get_active_trades()
            if active:
                self.sheets_mgr.sync_trades_async(active)
            self._last_sheet_sync_ts = now_ts

        return None

    def eod_square_off(self, reason: str = "EOD 3PM Square-off @ CMP") -> List[Dict[str, Any]]:
        """Closes all active Advanced Strategy trades at 3:00 PM."""
        closed = self.tracker.eod_square_off(reason)
        if closed:
            self.sheets_mgr.sync_trades_async(closed)
        return closed

    def get_summary_stats(self) -> Dict[str, Any]:
        return self.tracker.get_summary_stats()

    def get_active_trades(self) -> List[Dict[str, Any]]:
        return self.tracker.get_active_trades()

    def get_closed_trades(self) -> List[Dict[str, Any]]:
        return self.tracker.get_closed_trades()

    def get_rejections(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.tracker.get_rejections(limit)

    def build_next_day_plan(
        self,
        symbol: str = "NIFTY",
        expiry: Optional[str] = None,
        strikes_dict: Optional[Dict[str, Any]] = None,
        mtf_pivots: Optional[Dict[str, Any]] = None,
        daily_focus: Optional[Dict[str, Any]] = None,
        mode: str = "session"
    ) -> Dict[str, Any]:
        """
        Auto-generates Next-Day Levels and Qualifying Plan Setups for the Advanced Strategy,
        applying the exact same logic and rules design:
        - 1H Support/Resistance & 15M Intraday strike selection
        - S2 Buy Reversals (with liquidity sweep LL, targets S1->P->R1->R2, and R:R check >= 1.5)
        - R2 Sell Reversals (with liquidity sweep HH, targets R1->P->S1->S2, and R:R check >= 1.5)
        - Pivot P Continuations (Buy -> targets R1, R2; Sell -> targets S1, S2, R:R >= 1.5)
        - Weekly and Daily Context filters & Retest confirmation requirements.
        Saves to advanced_strategy_next_day_plan.json.
        """
        if not strikes_dict:
            for fn in ["strike_pivot_cache_next.json", "strike_pivot_cache.json"]:
                fpath = os.path.join(BASE_DIR, fn)
                if os.path.exists(fpath):
                    try:
                        with open(fpath, "r") as f:
                            d = json.load(f)
                            if d.get("symbol") == symbol or not symbol:
                                strikes_dict = d.get("strikes", {})
                                if strikes_dict:
                                    break
                    except Exception:
                        pass
        if not strikes_dict:
            return self.next_day_plan or {}

        today = date.today()
        nd = today + timedelta(days=1)
        while nd.weekday() >= 5:
            nd += timedelta(days=1)
        next_date_str = nd.strftime("%Y-%m-%d")

        step = get_symbol_step(symbol)

        f15 = (daily_focus or {}).get("15M", {})
        f1h = (daily_focus or {}).get("1H", {})
        if not f15 and mtf_pivots:
            p15 = mtf_pivots.get("15-Min") or mtf_pivots.get("15M") or {}
            f15 = extract_focus_strikes_from_pivots(p15, step)
        if not f1h and mtf_pivots:
            p1h = mtf_pivots.get("1-Hour") or mtf_pivots.get("1H") or {}
            f1h = extract_focus_strikes_from_pivots(p1h, step)

        f15_ce = set(f15.get("ce_strikes") or [s for s in (f15.get("CE1"), f15.get("CE2"), f15.get("CE3")) if s])
        f15_pe = set(f15.get("pe_strikes") or [s for s in (f15.get("PE1"), f15.get("PE2"), f15.get("PE3")) if s])
        f15_p  = set(f15.get("p_strikes") or ([f15.get("P_STRIKE")] if f15.get("P_STRIKE") else []))
        f15_all = set(f15.get("all_6") or f15.get("all_4") or (list(f15_ce | f15_pe | f15_p)))
        if f15_all and not f15_ce and not f15_pe:
            f15_ce = set(f15_all)
            f15_pe = set(f15_all)

        f1h_ce = set(f1h.get("ce_strikes") or [s for s in (f1h.get("CE1"), f1h.get("CE2"), f1h.get("CE3")) if s])
        f1h_pe = set(f1h.get("pe_strikes") or [s for s in (f1h.get("PE1"), f1h.get("PE2"), f1h.get("PE3")) if s])
        f1h_p  = set(f1h.get("p_strikes") or ([f1h.get("P_STRIKE")] if f1h.get("P_STRIKE") else []))
        f1h_all = set(f1h.get("all_6") or f1h.get("all_4") or (list(f1h_ce | f1h_pe | f1h_p)))
        if f1h_all and not f1h_ce and not f1h_pe:
            f1h_ce = set(f1h_all)
            f1h_pe = set(f1h_all)

        weekly_piv = (mtf_pivots or {}).get("Weekly", {})
        daily_piv = (mtf_pivots or {}).get("Daily", {})

        f_day = extract_focus_strikes_from_pivots(daily_piv, step) if daily_piv else {}
        f_day_ce = set(f_day.get("ce_strikes") or [])
        f_day_pe = set(f_day.get("pe_strikes") or [])
        f_day_p  = set(f_day.get("p_strikes") or [])
        f_day_all = set(f_day.get("all_6") or f_day.get("all_4") or [])

        # Allowed strikes: STRICTLY those coming from 15M, 1H, or Daily Fibonacci pivot levels
        all_ce_strikes = f15_ce | f1h_ce | f_day_ce | f15_p | f1h_p | f_day_p
        all_pe_strikes = f15_pe | f1h_pe | f_day_pe | f15_p | f1h_p | f_day_p
        all_p_strikes  = f15_p | f1h_p | f_day_p
        all_allowed_strikes = (
            f15_all | f1h_all | f_day_all
            | set((daily_focus or {}).get("all_focus_6", []))
            | set((daily_focus or {}).get("all_focus", []))
        )

        setups = []
        for tok, s_info in strikes_dict.items():
            ot = s_info.get("opt_type", "CE")
            stk = int(s_info.get("strike", 0))
            if stk <= 0:
                continue
            if expiry and s_info.get("expiry") and s_info.get("expiry") != expiry:
                continue

            # STRICT ADVANCE FILTER: Keep strikes IDENTICAL to 15M, 1H & Daily Pivot levels
            if all_allowed_strikes:
                if stk not in all_allowed_strikes:
                    continue
                # If specific CE/PE roles are established, ensure option type matches (R1/R2 -> CE, S1/S2 -> PE, P -> Both)
                if ot == "CE" and all_ce_strikes and stk not in all_ce_strikes:
                    continue
                if ot == "PE" and all_pe_strikes and stk not in all_pe_strikes:
                    continue

            s3 = float(s_info.get("S3", 0.0))
            s2 = float(s_info.get("S2", 0.0))
            s1 = float(s_info.get("S1", 0.0))
            p  = float(s_info.get("P", 0.0))
            r1 = float(s_info.get("R1", 0.0))
            r2 = float(s_info.get("R2", 0.0))
            r3 = float(s_info.get("R3", 0.0))

            if p <= 0:
                continue

            # Focus group label strictly based on Fibonacci pivot timeframe
            if stk in f15_all and stk in f1h_all:
                fg = "⭐15M & ⏳1H"
            elif stk in f15_all:
                fg = "⭐15M Intraday"
            elif stk in f1h_all:
                fg = "⏳1H Structure"
            elif stk in f_day_all:
                fg = "📅Daily Structure"
            else:
                fg = "⭐Pivot Focus"

            s_exp = str(s_info.get("expiry") or expiry or "")
            und_spot = float(daily_piv.get("close") or daily_piv.get("C") or daily_piv.get("P") or 0.0)

            # 1. BUY Reversal at S2
            buy_ep = s2 if s2 > 0.05 else s1
            buy_sl = s3 if s3 > 0 else round(buy_ep * 0.90, 2)
            buy_ll = buy_sl
            buy_hh = 0.0
            buy_t1 = s1 if s2 > 0.05 else p
            buy_t2 = p
            buy_t3 = r1
            buy_t4 = r2
            buy_risk = max(buy_ep - buy_sl, 0.5)
            buy_reward = max(buy_t2 - buy_ep, 0.0)
            buy_rr = round(buy_reward / buy_risk, 2)

            ctx_buy_ok, ctx_buy_desc, _ = evaluate_context(
                spot=und_spot if und_spot > 0 else buy_ep,
                daily_pivots=daily_piv,
                weekly_pivots=weekly_piv,
                opt_type=ot,
                action="BUY",
                setup_type="REVERSAL"
            )
            buy_qual = (buy_rr >= ADVANCED_MIN_RR_RATIO) and (ctx_buy_ok or not ADVANCED_REJECT_ON_CONTEXT_CONFLICT)
            buy_status = "QUALIFIED" if buy_qual else f"REJECTED ({'Low R:R' if buy_rr < ADVANCED_MIN_RR_RATIO else 'Context Conflict'})"
            buy_reason = f"★ BUY Reversal [S2 Reclaim @ {buy_ep:.2f} | Sweep LL: {buy_ll:.2f} | T1: {buy_t1:.2f} | T2: {buy_t2:.2f}]"

            strike_ltp = round(float(s_info.get("ltp") or s_info.get("prev_c") or s_info.get("close") or s_info.get("last_price") or s_info.get("P", 0.0) or 0.0), 2)

            setups.append({
                "focus_group": fg,
                "setup": "★ BUY Reversal (S2 Reclaim)",
                "action": "BUY",
                "type": ot,
                "strike": stk,
                "expiry": s_exp,
                "ltp": strike_ltp,
                "planned_ep": buy_ep,
                "sl": buy_sl,
                "ll_price": round(buy_ll, 2),
                "hh_price": round(buy_hh, 2),
                "target_1": buy_t1,
                "target_2": buy_t2,
                "target_3": buy_t3,
                "target_4": buy_t4,
                "rr": f"1:{buy_rr:.2f}",
                "rr_val": buy_rr,
                "context_result": ctx_buy_desc,
                "retest_status": "Requires 5M Reclaim + Retest" if ADVANCED_REQUIRE_RETEST else "5M Reclaim Candle",
                "advance_status": buy_status,
                "entry_reason": buy_reason,
                "pivots": {"S3": s3, "S2": s2, "S1": s1, "P": p, "R1": r1, "R2": r2, "R3": r3}
            })

            # 2. SELL Reversal at R2
            sell_ep = r2
            sell_sl = r3 if r3 > 0 else round(sell_ep * 1.10, 2)
            sell_hh = sell_sl
            sell_ll = 0.0
            sell_t1 = r1
            sell_t2 = p
            sell_t3 = s1
            sell_t4 = s2
            sell_risk = max(sell_sl - sell_ep, 0.5)
            sell_reward = max(sell_ep - sell_t2, 0.0)
            sell_rr = round(sell_reward / sell_risk, 2)

            ctx_sell_ok, ctx_sell_desc, _ = evaluate_context(
                spot=und_spot if und_spot > 0 else sell_ep,
                daily_pivots=daily_piv,
                weekly_pivots=weekly_piv,
                opt_type=ot,
                action="SELL",
                setup_type="REVERSAL"
            )
            sell_qual = (sell_rr >= ADVANCED_MIN_RR_RATIO) and (ctx_sell_ok or not ADVANCED_REJECT_ON_CONTEXT_CONFLICT)
            sell_status = "QUALIFIED" if sell_qual else f"REJECTED ({'Low R:R' if sell_rr < ADVANCED_MIN_RR_RATIO else 'Context Conflict'})"
            sell_reason = f"▼ SELL Reversal [R2 Reject @ {sell_ep:.2f} | Sweep HH: {sell_hh:.2f} | T1: {sell_t1:.2f} | T2: {sell_t2:.2f}]"

            setups.append({
                "focus_group": fg,
                "setup": "▼ SELL Reversal (R2 Reject)",
                "action": "SELL",
                "type": ot,
                "strike": stk,
                "expiry": s_exp,
                "ltp": strike_ltp,
                "planned_ep": sell_ep,
                "sl": sell_sl,
                "ll_price": round(sell_ll, 2),
                "hh_price": round(sell_hh, 2),
                "target_1": sell_t1,
                "target_2": sell_t2,
                "target_3": sell_t3,
                "target_4": sell_t4,
                "rr": f"1:{sell_rr:.2f}",
                "rr_val": sell_rr,
                "context_result": ctx_sell_desc,
                "retest_status": "Requires 5M Rejection + Retest" if ADVANCED_REQUIRE_RETEST else "5M Rejection Candle",
                "advance_status": sell_status,
                "entry_reason": sell_reason,
                "pivots": {"S3": s3, "S2": s2, "S1": s1, "P": p, "R1": r1, "R2": r2, "R3": r3}
            })

            # 3. Pivot P Continuation Setups (ONLY for strikes in the Pivot P zone)
            is_p_zone_strike = (stk in all_p_strikes) or (not all_p_strikes and abs(stk - p) <= step)
            if ADVANCED_ALLOW_P_CONTINUATION and is_p_zone_strike:
                p_buy_ep = p
                p_buy_sl = s1
                p_buy_ll = p_buy_sl
                p_buy_hh = 0.0
                p_buy_risk = max(p_buy_ep - p_buy_sl, 0.5)
                p_buy_rew = max(r1 - p_buy_ep, 0.0)
                p_buy_rr = round(p_buy_rew / p_buy_risk, 2)
                p_buy_status = "QUALIFIED" if p_buy_rr >= ADVANCED_MIN_RR_RATIO else "REJECTED (Low R:R)"
                p_buy_reason = f"▲ BUY Continuation at Pivot P [P Reclaim @ {p_buy_ep:.2f} | Sweep LL: {p_buy_ll:.2f} | T1: {r1:.2f} | T2: {r2:.2f}]"
                setups.append({
                    "focus_group": fg,
                    "setup": "▲ BUY Continuation at Pivot P",
                    "action": "BUY",
                    "type": ot,
                    "strike": stk,
                    "expiry": s_exp,
                    "ltp": strike_ltp,
                    "planned_ep": p_buy_ep,
                    "sl": p_buy_sl,
                    "ll_price": round(p_buy_ll, 2),
                    "hh_price": round(p_buy_hh, 2),
                    "target_1": r1,
                    "target_2": r2,
                    "target_3": r3,
                    "target_4": 0.0,
                    "rr": f"1:{p_buy_rr:.2f}",
                    "rr_val": p_buy_rr,
                    "context_result": "Pivot P Continuation (Support Area Reclaim)",
                    "retest_status": "Sweep below P + 5M Bullish Reclaim",
                    "advance_status": p_buy_status,
                    "entry_reason": p_buy_reason,
                    "pivots": {"S3": s3, "S2": s2, "S1": s1, "P": p, "R1": r1, "R2": r2, "R3": r3}
                })

                p_sell_ep = p
                p_sell_sl = r1
                p_sell_hh = p_sell_sl
                p_sell_ll = 0.0
                p_sell_risk = max(p_sell_sl - p_sell_ep, 0.5)
                p_sell_rew = max(p_sell_ep - s1, 0.0)
                p_sell_rr = round(p_sell_rew / p_sell_risk, 2)
                p_sell_status = "QUALIFIED" if p_sell_rr >= ADVANCED_MIN_RR_RATIO else "REJECTED (Low R:R)"
                p_sell_reason = f"▼ SELL Continuation at Pivot P [P Reject @ {p_sell_ep:.2f} | Sweep HH: {p_sell_hh:.2f} | T1: {s1:.2f} | T2: {s2:.2f}]"
                setups.append({
                    "focus_group": fg,
                    "setup": "▼ SELL Continuation at Pivot P",
                    "action": "SELL",
                    "type": ot,
                    "strike": stk,
                    "expiry": s_exp,
                    "ltp": strike_ltp,
                    "planned_ep": p_sell_ep,
                    "sl": p_sell_sl,
                    "ll_price": round(p_sell_ll, 2),
                    "hh_price": round(p_sell_hh, 2),
                    "target_1": s1,
                    "target_2": s2,
                    "target_3": s3,
                    "target_4": 0.0,
                    "rr": f"1:{p_sell_rr:.2f}",
                    "rr_val": p_sell_rr,
                    "context_result": "Pivot P Continuation (Resistance Area Reject)",
                    "retest_status": "Sweep above P + 5M Bearish Reject",
                    "advance_status": p_sell_status,
                    "entry_reason": p_sell_reason,
                    "pivots": {"S3": s3, "S2": s2, "S1": s1, "P": p, "R1": r1, "R2": r2, "R3": r3}
                })

        plan = {
            "strategy_id": STRATEGY_ID,
            "symbol": symbol,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "target_session_date": next_date_str,
            "total_mapped_strikes": len(strikes_dict),
            "setups_count": len(setups),
            "qualified_count": sum(1 for s in setups if s["advance_status"] == "QUALIFIED"),
            "setups": setups
        }

        self.next_day_plan = plan
        try:
            with open(ADVANCED_NEXT_DAY_PLAN_FILE, "w") as f:
                json.dump(plan, f, indent=2)
            print(f"🎯 [ADV STRATEGY] Next-Day Plan saved: {len(setups)} setups ({plan['qualified_count']} qualified) → {ADVANCED_NEXT_DAY_PLAN_FILE}")
        except Exception as e:
            print(f"[ADV STRATEGY] Error saving next day plan: {e}")

        return plan

    def get_next_day_plan(self) -> Dict[str, Any]:
        return self.next_day_plan or {}


# Singleton instance
advanced_engine = AdvancedStrategyEngine()
