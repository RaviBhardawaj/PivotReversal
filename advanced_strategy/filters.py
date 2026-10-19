"""
Advanced Strategy Filters (Filters A through G)
Implements all qualification criteria specified for ADVANCED_PIVOT_REVERSAL.
"""

from typing import Dict, Any, Tuple, Optional
from datetime import datetime

from .config import (
    ADVANCED_REJECT_ON_CONTEXT_CONFLICT,
    ADVANCED_MIN_CANDLE_BODY_PCT,
    ADVANCED_CLOSE_NEAR_EXTREME_PCT,
    ADVANCED_REQUIRE_RETEST,
    ADVANCED_RETEST_TOLERANCE_PCT,
    ADVANCED_MIN_RR_RATIO,
    ADVANCED_MAX_SL_PTS,
    ADVANCED_MIN_SL_PTS,
    ADVANCED_MAX_SPREAD_PTS,
    ADVANCED_MAX_SPREAD_PCT,
    ADVANCED_MAX_QUOTE_AGE_S,
    ADVANCED_EST_CHARGE_PCT,
    ADVANCED_EST_FLAT_BROKERAGE,
    ADVANCED_MAX_REENTRIES,
)


def evaluate_context(
    spot: float,
    daily_pivots: Dict[str, float],
    weekly_pivots: Dict[str, float],
    opt_type: str,
    action: str,
    setup_type: str = "REVERSAL"
) -> Tuple[bool, str, str]:
    """
    Filter A: Weekly and Daily context evaluation.
    Returns: (passed, context_description, rejection_reason)
    """
    if spot <= 0 or not daily_pivots:
        # If spot or daily pivots unavailable, permit with neutral note
        return True, "NEUTRAL (Context Data Incomplete)", ""

    d_p = daily_pivots.get("P", 0.0)
    d_s1 = daily_pivots.get("S1", 0.0)
    d_s2 = daily_pivots.get("S2", 0.0)
    d_s3 = daily_pivots.get("S3", 0.0)
    d_r1 = daily_pivots.get("R1", 0.0)
    d_r2 = daily_pivots.get("R2", 0.0)
    d_r3 = daily_pivots.get("R3", 0.0)

    w_p = weekly_pivots.get("P", 0.0) if weekly_pivots else 0.0
    w_s3 = weekly_pivots.get("S3", 0.0) if weekly_pivots else 0.0
    w_r3 = weekly_pivots.get("R3", 0.0) if weekly_pivots else 0.0

    # Determine underlying market location
    if spot >= d_r2:
        loc = "At Daily Resistance (R2+)"
        mkt_bias = "BULLISH_EXTREME"
    elif spot >= d_r1:
        loc = "Above Daily R1"
        mkt_bias = "BULLISH"
    elif spot >= d_p:
        loc = "Between Daily P & R1"
        mkt_bias = "MILD_BULLISH"
    elif spot >= d_s1:
        loc = "Between Daily S1 & P"
        mkt_bias = "MILD_BEARISH"
    elif spot >= d_s2:
        loc = "Near Daily S1-S2 Support"
        mkt_bias = "BEARISH_EXTREME"
    else:
        loc = "Below Daily S2/S3"
        mkt_bias = "BREAKDOWN"

    # Context alignment check:
    # 1. Buying CE:
    #    - Reversal at support: underlying has pulled back to support (S1/S2/P) or holding P.
    #    - Conflict if market has broken down below Daily S3 or Weekly S3 with runaway momentum.
    # 2. Buying PE:
    #    - Reversal at resistance: underlying has rallied into resistance (R1/R2/P).
    #    - Conflict if market is breaking out above Daily R3 or Weekly R3 with runaway momentum.
    # 3. Selling CE:
    #    - Selling resistance: underlying near resistance.
    # 4. Selling PE:
    #    - Selling support: underlying near support.

    conflict = False
    reason = ""

    if action == "BUY":
        if opt_type == "CE":
            # Bullish option trade
            if d_s3 > 0 and spot < d_s3:
                conflict = True
                reason = f"Spot {spot:.1f} below Daily S3 ({d_s3:.1f}) - breakdown in underlying"
            elif w_s3 > 0 and spot < w_s3:
                conflict = True
                reason = f"Spot {spot:.1f} below Weekly S3 ({w_s3:.1f}) - multi-day breakdown"
        else: # PE
            # Bearish option trade (profit when market drops)
            if d_r3 > 0 and spot > d_r3:
                conflict = True
                reason = f"Spot {spot:.1f} above Daily R3 ({d_r3:.1f}) - runaway breakout in underlying"
            elif w_r3 > 0 and spot > w_r3:
                conflict = True
                reason = f"Spot {spot:.1f} above Weekly R3 ({w_r3:.1f}) - multi-day breakout"
    else: # SELL
        if opt_type == "CE":
            # Short CE (bearish trade)
            if d_r3 > 0 and spot > d_r3:
                conflict = True
                reason = f"Spot {spot:.1f} above Daily R3 ({d_r3:.1f}) - dangerous to short CE"
        else: # PE
            # Short PE (bullish trade)
            if d_s3 > 0 and spot < d_s3:
                conflict = True
                reason = f"Spot {spot:.1f} below Daily S3 ({d_s3:.1f}) - dangerous to short PE"

    ctx_desc = f"{loc} | Bias:{mkt_bias}"
    if conflict:
        ctx_desc += " [CONTEXT CONFLICT]"
        if ADVANCED_REJECT_ON_CONTEXT_CONFLICT:
            return False, ctx_desc, reason

    return True, ctx_desc, ""


def evaluate_liquidity_sweep(
    action: str,
    level_name: str,
    level_px: float,
    ll: float,
    hh: float,
    entry_px: float
) -> Tuple[bool, str]:
    """
    Filter B: Liquidity sweep and reclaim/rejection.
    A touch alone is not enough; LL/HH must be recorded before entry.
    """
    if action == "BUY":
        if ll <= 0:
            return False, "No liquidity sweep below support recorded (LL = 0)"
        if level_px > 0 and ll > level_px * 1.001:
            return False, f"LL ({ll:.2f}) is above support level {level_name} ({level_px:.2f}) - no real sweep"
        if ll >= entry_px:
            return False, f"Invalid LL ({ll:.2f}) >= entry price ({entry_px:.2f})"
        return True, ""
    else: # SELL
        if hh <= 0:
            return False, "No liquidity sweep above resistance recorded (HH = 0)"
        if level_px > 0 and hh < level_px * 0.999:
            return False, f"HH ({hh:.2f}) is below resistance level {level_name} ({level_px:.2f}) - no real sweep"
        if hh <= entry_px:
            return False, f"Invalid HH ({hh:.2f}) <= entry price ({entry_px:.2f})"
        return True, ""


def evaluate_candle_confirmation(
    action: str,
    candle_5m: Dict[str, Any],
    now_tick: datetime,
    reclaimed_level_px: float
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Filter C: Completed 5M candle confirmation.
    Only evaluates after relevant 5M candle has fully closed.
    Validates candle strength (body size and close near extreme).
    """
    if not candle_5m:
        return False, "Missing 5M candle data", {}

    c_open = candle_5m.get("open", 0.0)
    c_high = candle_5m.get("high", 0.0)
    c_low = candle_5m.get("low", 0.0)
    c_close = candle_5m.get("close", 0.0)
    c_bucket = candle_5m.get("bucket", -1)

    if c_close <= 0 or c_high <= 0 or c_open <= 0 or c_bucket == -1:
        return False, "Incomplete or unformed 5M candle", {}

    # Verify candle is CLOSED:
    # A candle bucket is (hour * 60 + minute) // 5.
    # Current tick bucket must be strictly greater than the confirmation candle bucket.
    current_bucket = (now_tick.hour * 60 + now_tick.minute) // 5
    if current_bucket == c_bucket:
        return False, f"Current 5M candle (bucket {c_bucket}) is still open; confirmation requires closed candle", {}

    rng = round(c_high - c_low, 2)
    if rng <= 0.01:
        return False, "Candle range too narrow for meaningful confirmation", {}

    body = round(abs(c_close - c_open), 2)
    body_pct = round(body / rng, 3)

    metrics = {
        "open": c_open, "high": c_high, "low": c_low, "close": c_close,
        "range": rng, "body": body, "body_pct": body_pct, "bucket": c_bucket
    }

    if action == "BUY":
        # 1. Bullish close: close > open
        if c_close < c_open:
            return False, f"5M candle closed bearish ({c_close:.2f} < {c_open:.2f}) - reclaim rejected", metrics
        # 2. Level reclaimed: close at or above reclaimed level
        if reclaimed_level_px > 0 and c_close < reclaimed_level_px * 0.998:
            return False, f"5M close {c_close:.2f} is below support {reclaimed_level_px:.2f}", metrics
        # 3. Meaningful candle body
        if body_pct < ADVANCED_MIN_CANDLE_BODY_PCT:
            return False, f"Candle body ({body_pct*100:.1f}%) < minimum {ADVANCED_MIN_CANDLE_BODY_PCT*100:.0f}%", metrics
        # 4. Close near high (upper wick <= ADVANCED_CLOSE_NEAR_EXTREME_PCT)
        upper_wick = c_high - c_close
        upper_wick_pct = round(upper_wick / rng, 3)
        metrics["wick_pct"] = upper_wick_pct
        if upper_wick_pct > ADVANCED_CLOSE_NEAR_EXTREME_PCT:
            return False, f"Upper wick ({upper_wick_pct*100:.1f}%) > maximum {ADVANCED_CLOSE_NEAR_EXTREME_PCT*100:.0f}% (close not near high)", metrics

        return True, f"Bullish reclaim closed @ {c_close:.2f} (Body {body_pct*100:.0f}%, UpperWick {upper_wick_pct*100:.0f}%)", metrics

    else: # SELL
        # 1. Bearish close: close < open
        if c_close > c_open:
            return False, f"5M candle closed bullish ({c_close:.2f} > {c_open:.2f}) - rejection failed", metrics
        # 2. Level rejected: close at or below rejected level
        if reclaimed_level_px > 0 and c_close > reclaimed_level_px * 1.002:
            return False, f"5M close {c_close:.2f} is above resistance {reclaimed_level_px:.2f}", metrics
        # 3. Meaningful candle body
        if body_pct < ADVANCED_MIN_CANDLE_BODY_PCT:
            return False, f"Candle body ({body_pct*100:.1f}%) < minimum {ADVANCED_MIN_CANDLE_BODY_PCT*100:.0f}%", metrics
        # 4. Close near low (lower wick <= ADVANCED_CLOSE_NEAR_EXTREME_PCT)
        lower_wick = c_close - c_low
        lower_wick_pct = round(lower_wick / rng, 3)
        metrics["wick_pct"] = lower_wick_pct
        if lower_wick_pct > ADVANCED_CLOSE_NEAR_EXTREME_PCT:
            return False, f"Lower wick ({lower_wick_pct*100:.1f}%) > maximum {ADVANCED_CLOSE_NEAR_EXTREME_PCT*100:.0f}% (close not near low)", metrics

        return True, f"Bearish rejection closed @ {c_close:.2f} (Body {body_pct*100:.0f}%, LowerWick {lower_wick_pct*100:.0f}%)", metrics


def evaluate_retest(
    retest_done: bool,
    retest_low: float,
    retest_high: float,
    level_px: float,
    ll: float,
    hh: float,
    action: str
) -> Tuple[bool, str, str]:
    """
    Filter D: Optional retest mode.
    Returns: (passed, retest_status_label, rejection_reason)
    """
    if not ADVANCED_REQUIRE_RETEST:
        return True, "SKIPPED_BY_CONFIG", ""

    if not retest_done:
        return False, "AWAITING_RETEST", "Retest of pivot level has not occurred yet"

    # Retest confirmed: verify it held in expected direction
    if action == "BUY":
        if ll > 0 and retest_low < ll:
            return False, "RETEST_FAILED", f"Retest broke below LL ({retest_low:.2f} < {ll:.2f})"
        return True, "RETEST_CONFIRMED", ""
    else: # SELL
        if hh > 0 and retest_high > hh:
            return False, "RETEST_FAILED", f"Retest pushed above HH ({retest_high:.2f} > {hh:.2f})"
        return True, "RETEST_CONFIRMED", ""


def evaluate_risk_reward(
    entry_px: float,
    sl_px: float,
    t1_px: float,
    action: str,
    lot_size: int = 25
) -> Tuple[bool, str, Dict[str, float]]:
    """
    Filter E: Risk/reward check and transaction charge estimation.
    Returns: (passed, rejection_reason, metrics_dict)
    """
    if entry_px <= 0:
        return False, f"Invalid entry price: {entry_px:.2f}", {}

    if action == "BUY":
        if sl_px <= 0 or sl_px >= entry_px:
            return False, f"Invalid BUY SL {sl_px:.2f} (must be strictly 0 < SL < Entry {entry_px:.2f})", {}
        if t1_px <= entry_px:
            return False, f"Invalid Target 1 {t1_px:.2f} (must be > Entry {entry_px:.2f})", {}
        risk_pts = round(entry_px - sl_px, 2)
        reward_pts = round(t1_px - entry_px, 2)
    else: # SELL
        if sl_px <= entry_px:
            return False, f"Invalid SELL SL {sl_px:.2f} (must be strictly SL > Entry {entry_px:.2f})", {}
        if t1_px <= 0 or t1_px >= entry_px:
            return False, f"Invalid Target 1 {t1_px:.2f} (must be strictly 0 < Target 1 < Entry {entry_px:.2f})", {}
        risk_pts = round(sl_px - entry_px, 2)
        reward_pts = round(entry_px - t1_px, 2)

    if risk_pts < ADVANCED_MIN_SL_PTS:
        return False, f"Risk distance too small ({risk_pts:.2f} pts < min {ADVANCED_MIN_SL_PTS})", {}

    if risk_pts > ADVANCED_MAX_SL_PTS:
        return False, f"Risk distance excessive ({risk_pts:.2f} pts > max {ADVANCED_MAX_SL_PTS} pts)", {}

    rr_ratio = round(reward_pts / risk_pts, 2)
    if rr_ratio < ADVANCED_MIN_RR_RATIO:
        return False, f"Insufficient Reward/Risk ratio ({rr_ratio:.2f} < min {ADVANCED_MIN_RR_RATIO})", {
            "risk_pts": risk_pts, "reward_pts": reward_pts, "rr_ratio": rr_ratio
        }

    # Estimated transaction charges & breakeven points
    turnover = round(entry_px * lot_size, 2)
    est_charges = round((turnover * ADVANCED_EST_CHARGE_PCT / 100.0) + ADVANCED_EST_FLAT_BROKERAGE, 2)
    charges_per_unit = round(est_charges / lot_size, 2)
    net_reward_pts = round(reward_pts - charges_per_unit, 2)

    if net_reward_pts <= 0:
        return False, f"Reward after charges is non-positive (net reward {net_reward_pts:.2f} pts)", {}

    metrics = {
        "risk_pts": risk_pts,
        "reward_pts": reward_pts,
        "rr_ratio": rr_ratio,
        "est_charges": est_charges,
        "charges_per_unit": charges_per_unit,
        "net_reward_pts": net_reward_pts
    }
    return True, "", metrics


def evaluate_option_liquidity(
    ltp: float,
    bid: float = 0.0,
    ask: float = 0.0,
    quote_age_s: float = 0.0
) -> Tuple[bool, str, str]:
    """
    Filter F: Option liquidity and price quality check.
    Returns: (passed, spread_description, rejection_reason)
    """
    if ltp <= 0:
        return False, "NO_DATA", "LTP is 0 or negative"

    if quote_age_s > ADVANCED_MAX_QUOTE_AGE_S:
        return False, "STALE_DATA", f"Quote is stale ({quote_age_s:.1f}s > {ADVANCED_MAX_QUOTE_AGE_S}s)"

    if bid > 0 and ask > 0:
        if ask < bid:
            return False, "CROSSED_MARKET", f"Ask ({ask:.2f}) < Bid ({bid:.2f})"
        spread_pts = round(ask - bid, 2)
        spread_pct = round((spread_pts / bid) * 100.0, 2) if bid > 0 else 0.0
        spread_desc = f"Spread {spread_pts:.2f} pts ({spread_pct:.1f}%)"

        if spread_pts > ADVANCED_MAX_SPREAD_PTS and spread_pct > ADVANCED_MAX_SPREAD_PCT:
            return False, spread_desc, f"Spread too wide ({spread_pts:.2f} pts / {spread_pct:.1f}%)"
        return True, spread_desc, ""

    return True, "LTP Valid (Depth N/A)", ""


def evaluate_no_trade_conditions(
    is_active_already: bool,
    reentry_count: int,
    price_oscillating_p: bool = False
) -> Tuple[bool, str]:
    """
    Filter G: General no-trade conditions.
    """
    if is_active_already:
        return False, "Equivalent Advanced Strategy trade is already active on this strike"

    if reentry_count > ADVANCED_MAX_REENTRIES:
        return False, f"Max re-entries ({ADVANCED_MAX_REENTRIES}) reached for this strike today"

    if price_oscillating_p:
        return False, "Price repeatedly crossing Pivot P without directional confirmation"

    return True, ""
