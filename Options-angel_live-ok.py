"""
Angel One - Live Option Chain with Greeks
Control Panel + Google Sheets Feed
Fixes: expiry sort, max OI highlight, BSE/MCX exchange types
"""

import os, re, sys, time, pyotp, requests, math, threading, pickle, json, csv, socket, functools
import pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox
from dotenv import load_dotenv
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from datetime import datetime, date, timedelta
from datetime import time as dt_time
import gspread
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# Force IPv4 socket resolution to eliminate 24s IPv6 fallback latency on macOS
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        res = _orig_getaddrinfo(host, port, family, type, proto, flags)
        ipv4 = [r for r in res if r[0] == socket.AF_INET]
        return ipv4 if ipv4 else res
    except Exception:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _ipv4_getaddrinfo

load_dotenv()
API_KEY     = os.getenv("ANGEL_API_KEY")
CLIENT_ID   = os.getenv("ANGEL_CLIENT_ID")
MPIN        = os.getenv("ANGEL_MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

SCRIP_URL      = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SPREADSHEET_ID = "1TNuwFBf1xtTq7xyu_d32MPoWhSp7HzMdacNSnFV6i2U"
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from brokers import (OrderEngine, ADAPTERS, BROKER_LABELS, ALL_LEVELS, LEVEL_LABELS,   # live orders
                     TIMEFRAMES, tf_levels, strike_selection_reason)

# ── Exchange config ───────────────────────────────────────
EXCHANGE_CONFIG = {
    "NFO": {
        "symbols":       ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"],
        "exchange_type":  2,
        "spot_exchange":  1,
        "steps":         {"NIFTY":50,"BANKNIFTY":100,"FINNIFTY":50,"MIDCPNIFTY":25},
        "spot_tokens":   {"NIFTY":"99926000","BANKNIFTY":"99926009",
                          "FINNIFTY":"99926037","MIDCPNIFTY":"99926074"},
        "inst_types":    ["OPTIDX", "OPTSTK"],
    },
    "BFO": {
        "symbols":       ["SENSEX", "BANKEX"],
        "exchange_type":  7,
        "spot_exchange":  3,
        "steps":         {"SENSEX":100,"BANKEX":100},
        "spot_tokens":   {"SENSEX":"1","BANKEX":"500112"},
        "inst_types":    ["OPTIDX", "OPTSTK"],
    },
    "MCX": {
        "symbols":       ["CRUDEOIL","GOLD","SILVER","NATURALGAS"],
        "exchange_type":  5,
        "spot_exchange":  5,
        "steps":         {"CRUDEOIL":100,"GOLD":100,"SILVER":1000,"NATURALGAS":10},
        "spot_tokens":   {},
        "inst_types":    ["OPTFUT"],
    },
}

def get_symbol_exchange(symbol: str) -> str:
    """Determine exchange segment for any given symbol (NFO, BFO, or MCX)."""
    sym = (symbol or "").strip().upper()
    if sym in ("SENSEX", "BANKEX"):
        return "BFO"
    if sym in ("CRUDEOIL", "GOLD", "SILVER", "NATURALGAS"):
        return "MCX"
    if Session.scrip_df is not None:
        df = Session.scrip_df
        m = df[df["name"] == sym]
        if not m.empty:
            for seg in ["NFO", "BFO", "MCX"]:
                if (m["exch_seg"] == seg).any():
                    return seg
    return "NFO"

def get_symbol_step(symbol: str, exchange: str = "NFO") -> int:
    """Get or auto-detect strike step for any index or stock."""
    sym = (symbol or "").strip().upper()
    cfg = EXCHANGE_CONFIG.get(exchange, {})
    if sym in cfg.get("steps", {}):
        return cfg["steps"][sym]
    if Session.scrip_df is not None:
        df = Session.scrip_df
        sub = df[(df["name"] == sym) & (df["exch_seg"].isin([exchange, "NFO", "BFO", "MCX"])) & (df["strike"].astype(float) > 0)]
        if not sub.empty:
            strikes = sorted(set(sub["strike"].astype(float).div(100).astype(int)))
            diffs = [s2 - s1 for s1, s2 in zip(strikes[:-1], strikes[1:]) if s2 - s1 > 0]
            if diffs:
                from collections import Counter
                return Counter(diffs).most_common(1)[0][0]
    return 50

def get_symbol_lot_size(symbol: str, exchange: str = "NFO") -> int:
    """Retrieve official contract lot size for any symbol."""
    sym = (symbol or "").strip().upper()
    if Session.scrip_df is not None:
        df = Session.scrip_df
        sub = df[(df["name"] == sym) & (df["exch_seg"].isin([exchange, "NFO", "BFO", "MCX"]))]
        if not sub.empty and "lotsize" in sub.columns:
            try:
                ls = int(sub.iloc[0]["lotsize"])
                if ls > 0: return ls
            except Exception:
                pass
    defaults = {"NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25, "MIDCPNIFTY": 50, "SENSEX": 10, "BANKEX": 15}
    return defaults.get(sym, 1)

def get_symbol_spot_token(symbol: str, exchange: str = "NFO") -> tuple:
    """Get (spot_token, spot_exchange_type) for any index or equity stock."""
    sym = (symbol or "").strip().upper()
    cfg = EXCHANGE_CONFIG.get(exchange, {})
    if sym in cfg.get("spot_tokens", {}):
        return str(cfg["spot_tokens"][sym]), cfg.get("spot_exchange", 1)
    if Session.scrip_df is not None:
        df = Session.scrip_df
        eq_seg = "BSE" if exchange == "BFO" else ("MCX" if exchange == "MCX" else "NSE")
        eq_match = df[(df["name"] == sym) & (df["exch_seg"] == eq_seg)]
        if not eq_match.empty:
            spot_exch = 3 if eq_seg == "BSE" else (5 if eq_seg == "MCX" else 1)
            return str(eq_match.iloc[0]["token"]), spot_exch
    return "", cfg.get("spot_exchange", 1)

# ═══════════════════════════════════════════════════════════
# GREEKS  (Black-Scholes)
# ═══════════════════════════════════════════════════════════
def norm_cdf(x):
    return 0.5*(1+math.erf(x/math.sqrt(2)))

def norm_pdf(x):
    return math.exp(-0.5*x*x)/math.sqrt(2*math.pi)

def compute_iv(ltp, S, K, T, r, opt_type, tol=1e-5, max_iter=200):
    if T<=0 or ltp<=0 or S<=0: return 0.0
    sigma = 0.3
    for _ in range(max_iter):
        try:
            d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
            d2 = d1 - sigma*math.sqrt(T)
            if opt_type=="CE":
                price = S*norm_cdf(d1) - K*math.exp(-r*T)*norm_cdf(d2)
            else:
                price = K*math.exp(-r*T)*norm_cdf(-d2) - S*norm_cdf(-d1)
            vega = S*norm_pdf(d1)*math.sqrt(T)
            diff = price - ltp
            if abs(diff)<tol: break
            if vega<1e-10:    break
            sigma -= diff/vega
            if sigma<=0: sigma=0.001
        except: break
    return max(0.0, min(sigma, 5.0))

def bs_greeks(S, K, T, r, sigma, opt_type):
    if T<=0 or sigma<=0 or S<=0:
        return dict(delta=0,gamma=0,theta=0,vega=0)
    try:
        d1=(math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
        d2=d1-sigma*math.sqrt(T)
        nd1=norm_pdf(d1)
        gamma = nd1/(S*sigma*math.sqrt(T))
        vega  = S*nd1*math.sqrt(T)/100
        if opt_type=="CE":
            delta = norm_cdf(d1)
            theta = ((-S*nd1*sigma)/(2*math.sqrt(T)) - r*K*math.exp(-r*T)*norm_cdf(d2))/365
        else:
            delta = norm_cdf(d1)-1
            theta = ((-S*nd1*sigma)/(2*math.sqrt(T)) + r*K*math.exp(-r*T)*norm_cdf(-d2))/365
        return dict(delta=round(delta,4),gamma=round(gamma,6),
                    theta=round(theta,2),vega=round(vega,2))
    except:
        return dict(delta=0,gamma=0,theta=0,vega=0)

ENTRY_BAND_PCT = 0.01   # a trade is taken only within 1% of its entry level (min 0.50)


OPEN_TRADE_STATUSES = ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit")


def display_sl(trade_status: str, m5_state: str, sl: float, is_buy: bool, planned_ep: float,
               ll: float = 0.0, hh: float = 0.0, entry_sl: float = 0.0) -> str:
    """SL text for the dashboard and sheet. Once a trade is entered its SL is the LL (HH for a
    SELL) it was entered with, at every target and after the exit; when the SL has trailed
    (T1 -> entry, T2 -> T1 ...) the trailed stop follows in brackets. Before entry: a preview."""
    ts, m5 = str(trade_status or ""), str(m5_state or "")
    entered = ts in OPEN_TRADE_STATUSES or "CONFIRMED" in m5 or ("Hit" in ts) or ("Closed" in ts)
    if entered and (entry_sl > 0 or sl > 0):
        base = entry_sl if entry_sl > 0 else sl
        if sl > 0 and abs(sl - base) >= 0.01:
            return f"{base:.2f} (trail {sl:.2f})"
        return f"{base:.2f}"
    if entered or ("RECLAIM" in m5 if is_buy else "REJECT" in m5):
        if is_buy:
            eff = ll if (0 < ll < planned_ep) else (sl if (0 < sl < planned_ep) else round(planned_ep * 0.90, 2))
        else:
            eff = hh if hh > planned_ep else (sl if sl > planned_ep else round(planned_ep * 1.10, 2))
        return f"{eff:.2f}" if eff > 0 else "-"
    return "-"


def at_entry_price(ltp: float, entry_px: float, side: str) -> bool:
    """True when the live price is at the planned entry level: a SELL at or just under its
    level, a BUY at or just over it. Never chase a price that has already run away."""
    if ltp <= 0 or entry_px <= 0:
        return False
    band = max(entry_px * ENTRY_BAND_PCT, 0.5)
    if side == "SELL":
        return entry_px - band <= ltp <= entry_px + 0.05
    return entry_px - 0.05 <= ltp <= entry_px + band


def traded_today_tick(message: dict, ltp: float, prev_ltp: float = 0.0, now: datetime = None) -> bool:
    """True when this feed tick is a real trade from today's session (09:15 IST onwards).
    Before the day's first trade the feed repeats yesterday's close and high/low."""
    now = now or datetime.now()
    if not is_live_market_open(now) or ltp <= 0:
        return False
    ltt = message.get("last_traded_timestamp") or 0
    try:
        ltt = float(ltt)
    except (TypeError, ValueError):
        ltt = 0
    if ltt > 0:
        if ltt > 1e12:            # milliseconds
            ltt /= 1000.0
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        return datetime.fromtimestamp(ltt) >= start
    # no trade time in this feed mode: only a price that moved during market hours counts
    return prev_ltp > 0 and abs(ltp - prev_ltp) > 1e-9


def is_live_market_open(dt: datetime = None) -> bool:
    """
    Checks if Indian Equity/Derivatives market (NSE/NFO) is currently in regular live trading hours:
    Monday through Friday, 09:15:00 to 15:30:00 IST.
    Supports FORCE_MARKET_OPEN=1 environment variable for testing/override.
    """
    if os.getenv("FORCE_MARKET_OPEN") == "1":
        return True
    if dt is None:
        dt = datetime.now()
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    from datetime import time as dt_time
    cur_t = dt.time()
    return dt_time(9, 15, 0) <= cur_t <= dt_time(15, 30, 0)

# ═══════════════════════════════════════════════════════════
# FIBONACCI PIVOTS & REVERSAL STRATEGY LOGIC
# ═══════════════════════════════════════════════════════════
def calc_fib_pivots(high: float, low: float, close: float) -> dict:
    """
    Exact Fibonacci Pivot formula:
        P  = (H + L + C) / 3
        R1 = P + 0.382 * (H - L)
        S1 = P - 0.382 * (H - L)
        R2 = P + 0.618 * (H - L)
        S2 = P - 0.618 * (H - L)
        R3 = P + (H - L)
        S3 = P - (H - L)
    """
    if high <= 0 or low <= 0 or close <= 0:
        return None
    if high < low:
        high, low = low, high

    rng = high - low   # a flat candle gives all levels = P, as on TradingView

    p  = (high + low + close) / 3.0
    r1 = p + 0.382 * rng
    s1 = max(p - 0.382 * rng, 0.05)
    r2 = p + 0.618 * rng
    s2 = max(p - 0.618 * rng, 0.05)
    r3 = p + rng
    s3 = max(p - rng, 0.05)

    return {
        "P":  round(p, 2),
        "R1": round(r1, 2), "R2": round(r2, 2), "R3": round(r3, 2),
        "S1": round(s1, 2), "S2": round(s2, 2), "S3": round(s3, 2),
    }

def get_pivot_zone(price: float, pivots: dict) -> str:
    """Classifies price into Fibonacci Pivot zones (terse label for display)."""
    if not pivots or price <= 0:
        return "N/A"
    p  = pivots["P"]
    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
    s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]
    near_band = max(p * 0.0015, 0.5)
    if abs(price - p) <= near_band: return "Near P"
    if s2 <= price <= s1:           return "In S1–S2"
    if r1 <= price <= r2:           return "In R1–R2"
    if price > r3:                  return "> R3"
    if r2 < price <= r3:            return "R2–R3"
    if p  < price < r1:             return "P–R1"
    if s1 < price < p:              return "S1–P"
    if s3 <= price < s2:            return "S2–S3"
    if price < s3:                  return "< S3"
    return "Normal"


def get_detailed_strike_zone(price: float, pivots: dict) -> str:
    """
    Returns an intuitive, detailed zone label showing where price is trading relative
    to Support, Resistance, or Pivot levels (works offline or live).
    """
    if not pivots or price <= 0:
        return "N/A"
    p  = pivots["P"]
    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
    s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]

    tol_s1 = max(s1 * 0.015, 0.5)
    tol_s2 = max(s2 * 0.015, 0.5)
    tol_r1 = max(r1 * 0.015, 0.5)
    tol_r2 = max(r2 * 0.015, 0.5)
    tol_p  = max(p  * 0.015, 0.5)

    if s2 <= price <= s1:
        return f"Buy Area (Above S2: {s2:.1f}–{s1:.1f})"
    if r1 <= price <= r2:
        return f"Sell Area (Below R2: {r1:.1f}–{r2:.1f})"
    if abs(price - s2) <= tol_s2:
        return f"Buy Area (Near S2: {s2:.1f})"
    if abs(price - s1) <= tol_s1:
        return f"Buy Area (Near S1: {s1:.1f})"
    if abs(price - r2) <= tol_r2:
        return f"Sell Area (Near R2: {r2:.1f})"
    if abs(price - r1) <= tol_r1:
        return f"Sell Area (Near R1: {r1:.1f})"
    if abs(price - p) <= tol_p:
        return f"Near Pivot P ({p:.1f})"
    if p < price < r1:
        return f"Mid P–R1 ({p:.1f}–{r1:.1f})"
    if s1 < price < p:
        return f"Mid S1–P ({s1:.1f}–{p:.1f})"
    if price > r3:
        return f"Breakout > R3 ({r3:.1f})"
    if r2 < price <= r3:
        return f"High R2–R3 ({r2:.1f}–{r3:.1f})"
    if s3 <= price < s2:
        return f"Deep S2–S3 ({s3:.1f}–{s2:.1f})"
    if price < s3:
        return f"Breakdown < S3 ({s3:.1f})"
    return f"Trading @ {price:.1f}"


def get_pivot_zone_detail(price: float, pivots: dict) -> dict:
    """
    Returns a rich zone dict with:
      zone      : short label (same as get_pivot_zone)
      above_p   : True if price > P
      in_support: True if In S1-S2 or near S1/S2
      in_resist : True if In R1-R2 or near R1/R2
      nearest_sup: closest support level and distance
      nearest_res: closest resistance level and distance
    """
    if not pivots or price <= 0:
        return {"zone": "N/A", "above_p": None, "in_support": False,
                "in_resist": False, "nearest_sup": None, "nearest_res": None}
    p  = pivots["P"]
    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
    s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]
    zone = get_pivot_zone(price, pivots)
    tol  = max(p * 0.005, 2.0)   # 0.5% tolerance for "near" check
    in_support = zone in ("In S1–S2",) or abs(price - s1) <= tol or abs(price - s2) <= tol
    in_resist  = zone in ("In R1–R2",) or abs(price - r1) <= tol or abs(price - r2) <= tol
    # nearest support below price
    sups = [(s1, "S1"), (s2, "S2"), (s3, "S3")]
    nearest_sup = min(sups, key=lambda x: abs(price - x[0]))
    ress = [(r1, "R1"), (r2, "R2"), (r3, "R3")]
    nearest_res = min(ress, key=lambda x: abs(price - x[0]))
    return {
        "zone":        zone,
        "above_p":     price > p,
        "in_support":  in_support,
        "in_resist":   in_resist,
        "nearest_sup": {"level": nearest_sup[1], "price": nearest_sup[0], "dist": round(price - nearest_sup[0], 2)},
        "nearest_res": {"level": nearest_res[1], "price": nearest_res[0], "dist": round(nearest_res[0] - price, 2)},
    }


def get_market_direction(spot: float, daily_pivots: dict, prev_spot: float = 0) -> dict:
    """
    Market direction engine based on Daily Pivot P.

    Rules:
      - BULLISH  : spot > Daily P  (price is above pivot, upward bias)
      - BEARISH  : spot < Daily P  (price is below pivot, downward bias)
      - REVERSAL_AT_P : spot was above P but crossed below P with significant move (>= 0.15%)
      - NEAR_P        : spot within ±0.15% of P — decision zone, wait for candle confirmation
    Returns dict with 'direction', 'bias' (CE/PE/NEUTRAL), 'color', and description.
    """
    if not daily_pivots or spot <= 0:
        return {"direction": "LOADING", "bias": "NEUTRAL", "color": "#8b949e", "desc": "Waiting for data"}

    p = daily_pivots["P"]
    band = max(p * 0.0015, 0.5)   # 0.15% near-P band

    if abs(spot - p) <= band:
        return {"direction": "NEAR P", "bias": "NEUTRAL", "color": "#ffd700",
                "desc": f"Decision zone — wait for breakout above/below P ({p})"}

    if spot > p:
        # Was it a reversal from below? (prev_spot < p and now above)
        if prev_spot > 0 and prev_spot < p:
            return {"direction": "BREAKOUT ↑ P", "bias": "CE", "color": "#3fb950",
                    "desc": f"Price crossed above P ({p}) — BULLISH breakout, look for CE"}
        return {"direction": "BULLISH", "bias": "CE", "color": "#3fb950",
                "desc": f"Above Daily P ({p}) — Bullish bias, trade CE on dips to S1/S2"}
    else:
        # Was it a reversal from above?
        if prev_spot > 0 and prev_spot > p:
            return {"direction": "REVERSAL ↓ P", "bias": "PE", "color": "#f85149",
                    "desc": f"Price broke below P ({p}) — BEARISH reversal, look for PE"}
        return {"direction": "BEARISH", "bias": "PE", "color": "#f85149",
                "desc": f"Below Daily P ({p}) — Bearish bias, trade PE on rallies to R1/R2"}


def get_1h_trade_bias(spot: float, h1_pivots: dict) -> dict:
    """
    Reads 1H pivot zone to decide intraday trade direction.

    Logic:
      - Near/In S1-S2 on 1H → look for CE (buy calls from option S1/S2, target option R1/R2)
      - Near/In R1-R2 on 1H → look for PE (buy puts from option S1/S2, target option R1/R2)
      - Near P on 1H        → NEUTRAL (wait)
    Returns dict with 'bias' (CE/PE/NEUTRAL), 'zone', 'action', 'color'.
    """
    if not h1_pivots or spot <= 0:
        return {"bias": "NEUTRAL", "zone": "N/A", "action": "Wait for data", "color": "#8b949e"}

    zd = get_pivot_zone_detail(spot, h1_pivots)
    zone = zd["zone"]
    p    = h1_pivots["P"]
    s1   = h1_pivots["S1"]
    s2   = h1_pivots["S2"]
    r1   = h1_pivots["R1"]
    r2   = h1_pivots["R2"]

    if zd["in_support"]:
        return {"bias": "CE", "zone": zone, "color": "#3fb950",
                "action": f"1H near Support ({s1}/{s2}) — Buy CE @ option S1/S2, target option R1/R2"}
    if zd["in_resist"]:
        return {"bias": "PE", "zone": zone, "color": "#f85149",
                "action": f"1H near Resistance ({r1}/{r2}) — Buy PE @ option S1/S2, target option R1/R2"}
    if zone in ("P–R1", "Near P"):
        return {"bias": "NEUTRAL", "zone": zone, "color": "#ffd700",
                "action": f"1H near Pivot ({p}) — Decision zone, wait for directional move"}
    if zone in ("S1–P", "S2–S3", "< S3"):
        return {"bias": "CE", "zone": zone, "color": "#58a6ff",
                "action": f"1H below P but not at support — CE bias, wait for S1/S2 touch"}
    if zone in ("R2–R3", "> R3"):
        return {"bias": "PE", "zone": zone, "color": "#ff7b72",
                "action": f"1H above R2 — Extended, PE bias, wait for R1/R2 reaction"}
    return {"bias": "NEUTRAL", "zone": zone, "color": "#8b949e",
            "action": "Monitor — no clear S/R touch"}


# SELL setup steps after a sweep, before the entry: the HH keeps tracking the high here
SELL_SETUP_STATES = ("SWEEP_R2", "RECLAIM_R2", "RETEST_R2", "TRADING_IN_FAVOR_SELL", "SWEEP_R1", "RECLAIM_R1")


class StrategyStrikeState:
    """
    Tracks state machine and liquidity sweeps for an option strike:
      - 5M candle OHLC tracking
      - BUY (S2 Buyer Area):
          1. Sweep/Approach below S2 -> records Liquidity Low (LL)
          2. Candle close confirmation above S2 -> bullish candle close
          3. Price retest: dips back towards S2 holding above LL
          4. Trading in favor: bounces from retest and pushes higher
          5. ENTRY CONFIRMED -> BUY ENTRY with SL = LL, Targets = S1, P, R1, R2, R3
      - SELL (R2 Seller Area):
          1. Sweep/Approach above R2 -> records Liquidity High (HH)
          2. Candle close confirmation below R2 -> bearish candle close
          3. Price retest: pushes back towards R2 holding below HH
          4. Trading in favor: drops from retest and pushes lower
          5. ENTRY CONFIRMED -> SELL ENTRY with SL = HH, Targets = R1, P, S1, S2, S3
    """
    def __init__(self, opt_type: str, strike: int, expiry: str = "", mode: str = "BOTH"):
        self.opt_type      = opt_type
        self.strike        = strike
        self.expiry        = expiry
        self.mode          = mode.upper()     # "BUY", "SELL", or "BOTH"
        self.state         = "WAITING"        # WAITING, SWEEP_S2, RECLAIM_S2, RETEST_S2, TRADING_IN_FAVOR_BUY, CONFIRMED_BUY, SWEEP_R2, RECLAIM_R2, RETEST_R2, TRADING_IN_FAVOR_SELL, CONFIRMED_SELL
        self.trigger_level = "S2"             # "S1", "S2", "R1", "R2"
        self.trade_status  = "Waiting"        # Waiting, Active, Target 1 Hit, Target 2 Hit, Target 3 Hit, Target 4 Hit, SL Hit
        self.ll            = 0.0              # Liquidity Low locked during sweep (used as BUY SL)
        self.hh            = 0.0              # Liquidity High locked during sweep (used as SELL SL)
        self.day_low       = 0.0              # Running day low of the option (for LL display column)
        self.day_high      = 0.0              # Running day high of the option (for HH display column)
        self.post_entry_hh = 0.0              # Highest High reached AFTER entry (preserved for sheet/tracker)
        self.post_entry_ll = 0.0              # Lowest Low reached AFTER entry (preserved for sheet/tracker)
        self.exit_price    = 0.0
        self.exit_time     = ""
        self.exit_reason   = ""
        self.entry_price   = 0.0
        self.entry_time    = ""
        self.sl            = 0.0
        self.entry_sl      = 0.0              # SL at entry (the LL / HH), kept when the SL trails
        self.targets       = []
        self.signal        = "WAITING"
        self.last_ltp      = 0.0
        self.candle_5m     = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "bucket": -1}
        self.retest_done   = False
        self.retest_low    = 0.0
        self.retest_high   = 0.0
        # Confirmation candle: the first 5M candle that CLOSES back above S2 (below R2) after the sweep.
        # Entry is a break above its high (BUY) / below its low (SELL), after the retest.
        self.sweep_bucket  = -1
        self.conf_high     = 0.0
        self.conf_low      = 0.0
        # Frozen pivot levels — locked at the moment the sweep begins
        # These do NOT drift as opt_piv recalculates each tick from changing opt_high/low
        self.frozen_s2     = 0.0   # S2 at time of SWEEP_S2 detection
        self.frozen_s1     = 0.0   # S1 at time of SWEEP_S1 detection
        self.frozen_r2     = 0.0   # R2 at time of SWEEP_R2 detection
        self.frozen_r1     = 0.0   # R1 at time of SWEEP_R1 detection
        self.frozen_pivots = {}    # Snapshot of pivots dict at time of sweep
        self.stale_reset   = False  # Set True when stale guard fires — main loop must close tracker
        self.entry_count   = 0      # Total entries taken on this strike
        self.sl_hit_count  = 0      # Number of times SL has been hit on this strike

    _LOCK_KEYS = ("P", "S1", "S2", "S3", "R1", "R2", "R3")

    def seed_from_trade(self, trade: dict, sl_hits: int = 0):
        """After an app restart: carry on with a trade the forward test restored, instead
        of starting this strike from WAITING (which could stack a second entry on it)."""
        side = trade.get("Action", "")
        self.state         = f"CONFIRMED_{side}"
        hit = int(_safe_float(trade.get("Target_Hit", 0)))
        self.trade_status  = f"Target {hit} Hit" if 1 <= hit <= 4 else "Active"
        self.entry_price   = round(_safe_float(trade.get("Entry_Price", 0)), 2)
        self.entry_time    = str(trade.get("Entry_Time", ""))
        self.sl            = round(_safe_float(trade.get("SL", 0)), 2)
        self.entry_sl      = round(_safe_float(trade.get("Initial_SL", 0)), 2) or self.sl
        self.targets       = [round(_safe_float(trade.get(f"Target_{i}", 0)), 2) for i in range(1, 6)]
        self.entry_count   = 1 + sl_hits
        self.sl_hit_count  = sl_hits
        if side == "BUY":
            self.ll = self.entry_sl
            self.trigger_level = "S2"
        else:
            self.hh = self.entry_sl
            self.trigger_level = "R2"
        self.signal = f"♻️ {side} {self.opt_type} restored after restart | Entry:{self.entry_price:.2f} | SL:{self.sl:.2f}"

    def seed_from_closed(self, trade: dict, sl_hits: int = 0, entries: int = 1):
        """After an app restart: show the strike's last trade of the day (entry, SL, exit) as it
        was shown before the restart. The strike waits for a fresh sweep for any new entry."""
        self.state         = "WAITING"
        self.trade_status  = f"{trade.get('Exit_Reason', 'Exit')} Closed"
        self.entry_price   = round(_safe_float(trade.get("Entry_Price", 0)), 2)
        self.entry_time    = str(trade.get("Entry_Time", ""))
        self.entry_sl      = round(_safe_float(trade.get("Initial_SL", 0) or trade.get("SL", 0)), 2)
        self.sl            = self.entry_sl
        self.targets       = [round(_safe_float(trade.get(f"Target_{i}", 0)), 2) for i in range(1, 6)]
        self.entry_count   = entries
        self.sl_hit_count  = sl_hits
        side = trade.get("Action", "")
        self.signal = (f"{'★' if side == 'BUY' else '▼'} {side} {self.opt_type} {self.trade_status} "
                       f"@ {_safe_float(trade.get('Exit_Price', 0)):.2f} | Entry:{self.entry_price:.2f} | SL:{self.entry_sl:.2f}")

    def update_tick(self, ltp: float, pivots: dict, now: datetime, opt_high: float = 0.0, opt_low: float = 0.0):
        """
        Level lock: the strike's pivot levels (and so planned entry / SL / targets) are locked
        the first time they are seen. They only change when the strike's levels themselves
        change (e.g. a new session's levels are mapped). A trade already running keeps the
        entry, SL and targets it was entered with until it exits; the new levels apply to the
        next setup after that.
        """
        if not pivots or ltp <= 0:
            return
        sig = tuple(round(float(pivots.get(k, 0) or 0), 2) for k in self._LOCK_KEYS)
        locked = getattr(self, "locked_pivots", None)
        if not locked:
            self.locked_pivots = dict(pivots)
            self._locked_sig = sig
        elif sig != getattr(self, "_locked_sig", None):
            in_trade = self.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit")
            if not in_trade:
                print(f"[LEVELS] {self.opt_type} {getattr(self, 'strike', '')} {self.mode}: levels changed {self._locked_sig} -> {sig} — new plan")
                self.locked_pivots = dict(pivots)
                self._locked_sig = sig
                if self.state not in ("WAITING",) and "Closed" not in self.trade_status:
                    # a half-formed setup on the old levels is no longer valid
                    self.state = "WAITING"
                    self.trigger_level = "S2" if self.mode != "SELL" else "R2"
                    self.ll = self.hh = 0.0
                    self.retest_done = False
                    self.retest_low = self.retest_high = 0.0
                    self.conf_high = self.conf_low = 0.0
                    self.sweep_bucket = -1
                    self.frozen_s2 = self.frozen_s1 = 0.0
                    self.frozen_r2 = self.frozen_r1 = 0.0
                    self.frozen_pivots = {}
            # in a trade: keep tracking on the locked levels; switch after the exit
        return self._update_tick_locked(ltp, self.locked_pivots, now, opt_high, opt_low)

    def _update_tick_locked(self, ltp: float, pivots: dict, now: datetime, opt_high: float = 0.0, opt_low: float = 0.0):
        if not pivots or ltp <= 0:
            return

        self.last_ltp = ltp

        # ── 5M candle builder from live ticks ──
        # The candle gets the day's high/low only when it moves while this candle is open
        # (a trade between two ticks); an older day high is not this candle's high.
        prev_dh, prev_dl = getattr(self, "_seen_day_high", 0.0), getattr(self, "_seen_day_low", 0.0)
        first_seen = prev_dh <= 0 and prev_dl <= 0
        opening_candle = (now.hour == 9 and 15 <= now.minute < 20)
        new_hi = opt_high if (opt_high > 0 and ((prev_dh > 0 and opt_high > prev_dh) or (first_seen and opening_candle))) else 0.0
        new_lo = opt_low if (opt_low > 0 and ((prev_dl > 0 and opt_low < prev_dl) or (first_seen and opening_candle))) else 0.0
        if opt_high > 0: self._seen_day_high = max(prev_dh, opt_high)
        if opt_low > 0:  self._seen_day_low = min(prev_dl, opt_low) if prev_dl > 0 else opt_low
        bucket = (now.hour * 60 + now.minute) // 5
        c = self.candle_5m
        if c["bucket"] != bucket:
            if c["bucket"] != -1 and c["close"] > 0:
                self._prev_candle = dict(c)
            c["open"]   = ltp
            c["high"]   = max(ltp, new_hi)
            c["low"]    = min(ltp, new_lo) if new_lo > 0 else ltp
            c["close"]  = ltp
            c["bucket"] = bucket
        else:
            c["high"]  = max(c["high"], ltp, new_hi)
            c["low"]   = min([x for x in (c["low"], ltp, new_lo) if x > 0])
            c["close"] = ltp

        closed = getattr(self, "_prev_candle", None)
        has_closed_candle = (closed is not None and closed.get("close", 0) > 0)

        s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]
        r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
        p          = pivots["P"]

        # Accurate day/session Low and High reference
        valid_low  = opt_low if opt_low > 0 else ltp
        valid_high = opt_high if opt_high > 0 else ltp

        # ── Always track the running day low/high for display in LL/HH columns ──
        if valid_low > 0:
            self.day_low  = round(valid_low, 2)   if self.day_low  == 0 else round(min(self.day_low,  valid_low),  2)
        if valid_high > 0:
            self.day_high = round(valid_high, 2)  if self.day_high == 0 else round(max(self.day_high, valid_high), 2)

        # ── Track post-entry peak High (BUY) & peak Low (SELL) for GSheet & tracker ──
        if self.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit") or "Closed" in self.trade_status:
            if self.post_entry_hh > 0 and ltp > self.post_entry_hh:
                self.post_entry_hh = round(ltp, 2)
            if self.post_entry_ll > 0 and ltp < self.post_entry_ll:
                self.post_entry_ll = round(ltp, 2)

        # ── Setup state machines and entries ONLY progress during LIVE MARKET hours ──
        if not is_live_market_open(now):
            if self.trade_status == "Waiting" and self.state == "WAITING":
                self.signal = "★ Market Closed — Monitoring Setup"
            return

        # ── Do NOT reset immediately after SL/Target; only reset when a fresh new sweep begins ──
        if "Closed" in self.trade_status:
            if self.mode in ("BUY", "BOTH"):
                use_s1 = (s2 <= 0.05 or s2 == 0)
                sup_lvl = s1 if use_s1 else s2
                if ltp <= sup_lvl * 1.005:  # fresh new sweep has started below support
                    self.state         = f"SWEEP_{'S1' if use_s1 else 'S2'}"
                    self.trigger_level = "S1" if use_s1 else "S2"
                    self.ll            = round(ltp, 2)
                    self.trade_status  = "Waiting"
                    self.sl = self.entry_price = self.entry_sl = 0.0
                    self.retest_done   = False
                    self.retest_low    = self.retest_high = 0.0
                    self.conf_high = self.conf_low = 0.0
                    self.sweep_bucket = -1
                    self.frozen_s2 = self.frozen_s1 = 0.0
                    self.frozen_r2 = self.frozen_r1 = 0.0
                    self.frozen_pivots = {}
                    self.targets       = []
                    self.signal        = f"★ {self.opt_type} Fresh SWEEP after SL — Awaiting Reclaim & New LL SL"
            if self.mode in ("SELL", "BOTH"):
                if ltp >= r2 * 0.995:  # fresh new sweep has started above resistance
                    self.state         = "SWEEP_R2"
                    self.trigger_level = "R2"
                    self.hh            = round(ltp, 2)
                    self.trade_status  = "Waiting"
                    self.sl = self.entry_price = self.entry_sl = 0.0
                    self.retest_done   = False
                    self.retest_low    = self.retest_high = 0.0
                    self.conf_high = self.conf_low = 0.0
                    self.sweep_bucket = -1
                    self.frozen_s2 = self.frozen_s1 = 0.0
                    self.frozen_r2 = self.frozen_r1 = 0.0
                    self.frozen_pivots = {}
                    self.targets       = []
                    self.signal        = f"▼ {self.opt_type} Fresh SWEEP R2 after SL — Awaiting Rejection & New HH SL"

        # ═══════════════════════════════════════════════════════
        # 1. BUY SETUP — S2 Buyer Area with Proper Candle Close, Retest & Trading in Favour
        # ═══════════════════════════════════════════════════════
        if self.mode in ("BUY", "BOTH"):
            if self.mode == "BUY":
                can_buy = (self.trade_status == "Waiting" and 
                           (ltp < r1 or self.state.startswith("SWEEP_S") or self.state.startswith("RECLAIM_S") or "RETEST_S" in self.state or "FAVOR_BUY" in self.state))
            else:
                can_buy = (self.trade_status == "Waiting" and 
                           not self.state.endswith("SELL") and 
                           "R1" not in self.state and 
                           "R2" not in self.state and 
                           (ltp < r1 or self.state.startswith("SWEEP_S") or self.state.startswith("RECLAIM_S") or "RETEST_S" in self.state or "FAVOR_BUY" in self.state))
        else:
            can_buy = False

        if can_buy:
            # S1 entry mode if S2 <= 0.05 or unpopulated; otherwise S2 entry mode
            use_s1_for_buy = (s2 <= 0.05 or s2 == 0)
            active_lvl = "S1" if use_s1_for_buy else "S2"
            fs_sup = (self.frozen_s1 if self.frozen_s1 > 0 else s1) if use_s1_for_buy else (self.frozen_s2 if self.frozen_s2 > 0 else s2)
            sup_thresh = s1 if use_s1_for_buy else s2

            # Cascade: if S2 > 0.05 and price does NOT sustain S1 (breaks below S1), it sweeps down into S2
            # If price also does NOT sustain S2 (breaks below S2), it cascades into S3 as extreme support
            if not use_s1_for_buy and ltp < s1 and self.state == "WAITING" and ltp > s2 * 1.01:
                self.signal = f"★ {self.opt_type} S1 did not sustain (LTP < {s1:.2f}) — Falling to S2 Buyer Area ({s2:.2f})"
            elif not use_s1_for_buy and s3 > 0.05 and ltp < s2 and self.state == "WAITING" and ltp > s3 * 1.01:
                self.signal = f"★ {self.opt_type} S2 did not sustain (LTP < {s2:.2f}) — Falling to S3 Extreme Support Area ({s3:.2f})"

            # Step 1: Sweep / Approach below Support (Records NEW LL)
            cur_sweep_low = c["low"] if (c.get("low", 0) > 0 and c["low"] < ltp) else ltp
            if ltp < fs_sup or (self.state == "WAITING" and (cur_sweep_low <= sup_thresh or ltp <= sup_thresh * 1.01)):
                self.state         = f"SWEEP_{active_lvl}"
                self.trigger_level = active_lvl
                sweep_ll = round(min(cur_sweep_low, ltp), 2)
                self.ll  = sweep_ll if self.ll == 0 else round(min(self.ll, sweep_ll), 2)
                if (active_lvl == "S2" and self.frozen_s2 == 0.0) or (active_lvl == "S1" and self.frozen_s1 == 0.0):
                    if active_lvl == "S2": self.frozen_s2 = round(s2, 2)
                    else:                  self.frozen_s1 = round(s1, 2)
                    self.frozen_pivots = {k: (round(v, 2) if isinstance(v, (int, float)) else v) for k, v in pivots.items()}
                self.retest_done = False
                # the reclaim candle must close AFTER this sweep; any older confirmation is void
                self.sweep_bucket = c["bucket"]
                self.conf_high = self.conf_low = 0.0
                note = f" (S2={s2:.2f}<=0.05)" if use_s1_for_buy else ""
                re_tag = f" [Re-entry New LL:{self.ll:.2f}]" if self.sl_hit_count > 0 else f" [LL:{self.ll:.2f}]"
                self.signal = f"★ {self.opt_type} SWEEP {active_lvl}{note}{re_tag} — Awaiting Reclaim & Candle Close"

            # Step 2: Support Reclaimed — Await Candle Close Confirmation
            elif (self.state == f"SWEEP_{active_lvl}" or (self.state == "WAITING" and valid_low < sup_thresh and ltp < r1)) and ltp >= fs_sup:
                self.state         = f"RECLAIM_{active_lvl}"
                self.trigger_level = active_lvl
                if self.ll == 0:
                    self.ll = round(valid_low if valid_low > 0 else ltp, 2)
                if self.sweep_bucket < 0:
                    self.sweep_bucket = c["bucket"]
                self.signal = f"★ {self.opt_type} RECLAIM {active_lvl} — Awaiting Bullish Candle Close"

            elif self.state == f"RECLAIM_{active_lvl}":
                # Confirmation candle: a 5M candle that has fully CLOSED, opened in or after the
                # sweep candle, and closed green above the support level. A candle still forming
                # never counts.
                candle_ok = False
                wick_retested = False
                if has_closed_candle and closed.get("bucket", -1) >= self.sweep_bucket:
                    c_close = closed["close"]
                    c_open  = closed["open"]
                    c_low   = closed.get("low", c_close)
                    if c_close >= fs_sup and c_close >= c_open:
                        candle_ok = True
                        if c_low <= fs_sup * 1.015:
                            wick_retested = True

                if candle_ok:
                    self.conf_high = round(closed.get("high", closed["close"]), 2)
                    self.conf_low  = round(closed.get("low", closed["close"]), 2)
                    self.state = f"RETEST_{active_lvl}"
                    self.retest_low = round(min(ltp, valid_low), 2)
                    if wick_retested:
                        self.retest_done = True
                        self.state = "TRADING_IN_FAVOR_BUY"
                        self.signal = f"★ {self.opt_type} {active_lvl} Confirmation Candle Closed Bullish (Retest Wick) — Buy Above {self.conf_high:.2f}"
                    else:
                        self.retest_done = False
                        self.signal = f"★ {self.opt_type} {active_lvl} Confirmation Candle Closed Bullish (High {self.conf_high:.2f}) — Watching Retest"

            elif self.state == f"RETEST_{active_lvl}":
                # Step 3: Retest Support level (price dips towards level holding above LL)
                if ltp <= fs_sup * 1.015:
                    self.retest_done = True
                    self.retest_low = round(min(self.retest_low if self.retest_low > 0 else ltp, ltp), 2)
                    self.state = "TRADING_IN_FAVOR_BUY"
                    self.signal = f"★ {self.opt_type} {active_lvl} Retested @ {self.retest_low:.2f} — Watching In-Favour Push"
                elif ltp < self.ll:
                    self.ll = round(ltp, 2)

            elif self.state == "TRADING_IN_FAVOR_BUY":
                # Step 4: after the retest, price breaks ABOVE the confirmation candle's high -> BUY.
                # Entry price = the confirmation candle high. No chasing: only within the entry
                # band just above it.
                _fp0 = self.frozen_pivots if self.frozen_pivots else pivots
                _ep0 = round(self.conf_high, 2)
                start_favor = (self.conf_high > 0 and ltp > fs_sup and ltp > self.conf_high)
                _t1 = round(_fp0.get('P', p) if active_lvl == "S1" else _fp0.get('S1', s1), 2)
                if start_favor and _ep0 >= _t1:
                    self.signal = f"★ {self.opt_type} {active_lvl} Confirmation high {_ep0:.2f} is at/above T1 {_t1:.2f} — no room, no trade"
                    start_favor = False
                elif start_favor and not at_entry_price(ltp, _ep0, "BUY"):
                    # price has run away from the entry: no chasing; wait for it to come back
                    self.signal = f"★ {self.opt_type} {active_lvl} Broke confirmation high — waiting for price to come back to entry {_ep0:.2f} (LTP {ltp:.2f})"
                    start_favor = False
                elif not start_favor and self.conf_high > 0:
                    self.signal = f"★ {self.opt_type} {active_lvl} Retested — Buy Above Confirmation High {self.conf_high:.2f}"
                if start_favor:
                    # Step 5: ENTRY IS CONFIRMED!
                    self.state         = "CONFIRMED_BUY"
                    self.trigger_level = active_lvl
                    fp       = self.frozen_pivots if self.frozen_pivots else pivots
                    entry_px = _ep0

                    # When support reclaim triggers, LL value is our updated SL (strictly < entry_px, NEVER 0.05)
                    if self.ll > 0 and self.ll < entry_px:
                        effective_sl = round(self.ll, 2)
                    elif valid_low > 0 and valid_low < entry_px:
                        effective_sl = round(valid_low, 2)
                    elif active_lvl == "S2" and fp.get('S3', s3) > 0 and fp.get('S3', s3) < entry_px:
                        effective_sl = round(fp.get('S3', s3), 2)
                    elif active_lvl == "S1" and fp.get('S2', s2) > 0 and fp.get('S2', s2) < entry_px:
                        effective_sl = round(fp.get('S2', s2), 2)
                    else:
                        effective_sl = round(entry_px * 0.90, 2) if entry_px > 0 else round(ltp * 0.90, 2)

                    # Target ladders:
                    if active_lvl == "S1":
                        # Entry near S1 (S2<=0.05) -> T1:P -> T2:R1 -> T3:R2 -> T4:R3 -> T5:R3*1.05
                        t1 = round(fp.get('P',  p),  2)
                        t2 = round(fp.get('R1', r1), 2)
                        t3 = round(fp.get('R2', r2), 2)
                        t4 = round(fp.get('R3', r3), 2)
                        t5 = round(fp.get('R3', r3) * 1.05, 2)
                    else:
                        # Entry near S2 -> T1:S1 -> T2:P -> T3:R1 -> T4:R2 -> T5:R3
                        t1 = round(fp.get('S1', s1), 2)
                        t2 = round(fp.get('P',  p),  2)
                        t3 = round(fp.get('R1', r1), 2)
                        t4 = round(fp.get('R2', r2), 2)
                        t5 = round(fp.get('R3', r3), 2)

                    self.entry_count += 1
                    self.entry_price  = entry_px
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    self.entry_sl     = effective_sl
                    self.targets      = [t1, t2, t3, t4, t5]
                    mkt_ctx = "Mkt Bullish ↑" if self.opt_type == "CE" else "Mkt Bearish ↓"
                    s2_tag = " (S2<=0.05)" if use_s1_for_buy else ""
                    re_tag = " [RE-ENTRY with New LL SL]" if self.sl_hit_count > 0 else ""
                    self.signal       = f"★ BUY {self.opt_type}{re_tag} [{mkt_ctx} | {active_lvl} Reclaim Confirmed{s2_tag} | Entry:{entry_px:.2f} | New LL SL:{effective_sl:.2f} | T1:{t1:.1f} T2:{t2:.1f} T3:{t3:.1f} T4:{t4:.1f} T5:{t5:.1f}]"
                    self.trade_status = "Active"

        # ═══════════════════════════════════════════════════════
        # 2. SELL SETUP — R2 Seller Area with Proper Candle Close, Retest & Trading in Favour
        # ═══════════════════════════════════════════════════════
        if self.mode in ("SELL", "BOTH"):
            if self.mode == "SELL":
                can_sell = (self.trade_status == "Waiting" and 
                            (ltp > s1 or self.state.startswith("SWEEP_R") or self.state.startswith("RECLAIM_R") or "RETEST_R" in self.state or "FAVOR_SELL" in self.state))
            else:
                can_sell = (self.trade_status == "Waiting" and 
                            not self.state.endswith("BUY") and 
                            "S1" not in self.state and 
                            "S2" not in self.state and 
                            (ltp > s1 or self.state.startswith("SWEEP_R") or self.state.startswith("RECLAIM_R") or "RETEST_R" in self.state or "FAVOR_SELL" in self.state))
        else:
            can_sell = False

        if can_sell:
            fr2 = self.frozen_r2 if self.frozen_r2 > 0 else r2

            # HH = the highest price of the whole sweep. Once a sweep is on, it keeps rising
            # with price through the rejection, candle close, retest and in-favour steps until
            # the entry (a sweep that only came near R2 can still make its high afterwards).
            if self.hh > 0 and self.state in SELL_SETUP_STATES:
                self.hh = round(max(self.hh, ltp, c["high"]), 2)

            # Step 1: Sweep / Push above R2 (Records NEW HH)
            cur_sweep_high = c["high"] if (c.get("high", 0) > 0 and c["high"] > ltp) else ltp
            if ltp > fr2 or (self.state == "WAITING" and (cur_sweep_high >= r2 or ltp >= r2 * 0.99)):
                self.state         = "SWEEP_R2"
                self.trigger_level = "R2"
                sweep_hh = round(max(cur_sweep_high, ltp), 2)
                self.hh  = sweep_hh if self.hh == 0 else round(max(self.hh, sweep_hh), 2)
                if self.frozen_r2 == 0.0:
                    self.frozen_r2 = round(r2, 2)
                    self.frozen_pivots = {k: (round(v, 2) if isinstance(v, (int, float)) else v) for k, v in pivots.items()}
                self.retest_done = False
                # the rejection candle must close AFTER this sweep; any older confirmation is void
                self.sweep_bucket = c["bucket"]
                self.conf_high = self.conf_low = 0.0
                re_tag = f" [Re-entry New HH:{self.hh:.2f}]" if self.sl_hit_count > 0 else f" [HH:{self.hh:.2f}]"
                self.signal = f"▼ {self.opt_type} SWEEP R2{re_tag} — Awaiting Rejection & Candle Close"

            # Step 2: R2 Rejected — Await Candle Close Confirmation
            elif (self.state == "SWEEP_R2" or (self.state == "WAITING" and valid_high > r2 and ltp > s1)) and ltp <= fr2:
                self.state         = "RECLAIM_R2"
                self.trigger_level = "R2"
                if self.hh == 0:
                    self.hh = round(valid_high if valid_high > 0 else ltp, 2)
                if self.sweep_bucket < 0:
                    self.sweep_bucket = c["bucket"]
                self.signal = f"▼ {self.opt_type} REJECT R2 — Awaiting Bearish Candle Close"

            elif self.state == "RECLAIM_R2":
                # Confirmation candle: a fully CLOSED 5M candle, opened in or after the sweep
                # candle, that closed red below R2. A candle still forming never counts.
                candle_ok = False
                wick_retested = False
                if has_closed_candle and closed.get("bucket", -1) >= self.sweep_bucket:
                    c_close = closed["close"]
                    c_open  = closed["open"]
                    c_high  = closed.get("high", c_close)
                    if c_close <= fr2 and c_close <= c_open:
                        candle_ok = True
                        if c_high >= fr2 * 0.985:
                            wick_retested = True

                if candle_ok:
                    self.conf_high = round(closed.get("high", closed["close"]), 2)
                    self.conf_low  = round(closed.get("low", closed["close"]), 2)
                    self.hh = round(max(self.hh, self.conf_high), 2)
                    self.state = "RETEST_R2"
                    self.retest_high = round(max(ltp, valid_high), 2)
                    if wick_retested:
                        self.retest_done = True
                        self.state = "TRADING_IN_FAVOR_SELL"
                        self.signal = f"▼ {self.opt_type} R2 Confirmation Candle Closed Bearish (Retest Wick) — Sell Below {self.conf_low:.2f}"
                    else:
                        self.retest_done = False
                        self.signal = f"▼ {self.opt_type} R2 Confirmation Candle Closed Bearish (Low {self.conf_low:.2f}) — Watching Retest"

            elif self.state == "RETEST_R2":
                # Step 3: Retest R2 level (price bounces towards R2 holding below HH)
                if ltp >= fr2 * 0.985:
                    self.retest_done = True
                    self.retest_high = round(max(self.retest_high if self.retest_high > 0 else ltp, ltp), 2)
                    self.state = "TRADING_IN_FAVOR_SELL"
                    self.signal = f"▼ {self.opt_type} R2 Retested @ {self.retest_high:.2f} — Watching In-Favour Drop"
                elif ltp > self.hh:
                    self.hh = round(ltp, 2)

            elif self.state == "TRADING_IN_FAVOR_SELL":
                # Step 4: after the retest, price breaks BELOW the confirmation candle's low -> SELL.
                # Entry price = the confirmation candle low. No chasing: only within the entry
                # band just below it.
                _fp0 = self.frozen_pivots if self.frozen_pivots else pivots
                _ep0 = round(self.conf_low, 2)
                start_favor = (self.conf_low > 0 and ltp < fr2 and ltp < self.conf_low)
                _t1 = round(_fp0.get('R1', r1), 2)
                if start_favor and _ep0 <= _t1:
                    self.signal = f"▼ {self.opt_type} R2 Confirmation low {_ep0:.2f} is at/below T1 {_t1:.2f} — no room, no trade"
                    start_favor = False
                elif start_favor and not at_entry_price(ltp, _ep0, "SELL"):
                    # price has run away from the entry: no chasing; wait for it to come back
                    self.signal = f"▼ {self.opt_type} R2 Broke confirmation low — waiting for price to come back to entry {_ep0:.2f} (LTP {ltp:.2f})"
                    start_favor = False
                elif not start_favor and self.conf_low > 0:
                    self.signal = f"▼ {self.opt_type} R2 Retested — Sell Below Confirmation Low {self.conf_low:.2f}"
                if start_favor:
                    # Step 5: ENTRY IS CONFIRMED!
                    self.state         = "CONFIRMED_SELL"
                    self.trigger_level = "R2"
                    fp       = self.frozen_pivots if self.frozen_pivots else pivots
                    entry_px = _ep0

                    # SL is the sweep HH, never R3 or a % of entry (strictly > entry_px)
                    effective_sl = round(max(self.hh, self.conf_high), 2)
                if start_favor and effective_sl <= entry_px:
                    self.signal = f"▼ {self.opt_type} R2 No HH above entry {entry_px:.2f} — no trade"
                    self.state = "TRADING_IN_FAVOR_SELL"
                    start_favor = False
                if start_favor:
                    # 5-target ladder (descending): R1 → P → S1 → S2 → S3
                    t1 = round(fp.get('R1', r1), 2)
                    t2 = round(fp.get('P',  p),  2)
                    t3 = round(fp.get('S1', s1), 2)
                    t4 = round(fp.get('S2', s2), 2)
                    t5 = round(fp.get('S3', s3), 2)

                    self.entry_count += 1
                    self.entry_price  = entry_px
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    self.entry_sl     = effective_sl
                    self.targets      = [t1, t2, t3, t4, t5]
                    mkt_ctx = "Mkt Bearish ↓" if self.opt_type == "CE" else "Mkt Bullish ↑"
                    re_tag = " [RE-ENTRY with New HH SL]" if self.sl_hit_count > 0 else ""
                    self.signal       = f"▼ SELL {self.opt_type}{re_tag} [{mkt_ctx} | R2 Rejection Confirmed | Entry:{entry_px:.2f} | New HH SL:{effective_sl:.2f} | T1:{t1:.1f} T2:{t2:.1f} T3:{t3:.1f} T4:{t4:.1f} T5:{t5:.1f}]"
                    self.trade_status = "Active"

            # R1 Sweep & Rejection — SECONDARY setup
            elif self.state == "SWEEP_R1" and ltp <= r1:
                self.state = "RECLAIM_R1"
                if self.hh == 0:
                    self.hh = round(valid_high if valid_high > 0 else ltp, 2)

            elif self.state == "RECLAIM_R1":
                fr1 = self.frozen_r1 if self.frozen_r1 > 0 else r1
                # same rule as R2: a closed red candle below R1, then a break below its low
                if self.conf_low <= 0 and (has_closed_candle and
                        closed.get("bucket", -1) >= self.sweep_bucket and
                        closed["close"] <= fr1 and closed["close"] <= closed["open"]):
                    self.conf_high = round(closed.get("high", closed["close"]), 2)
                    self.conf_low  = round(closed.get("low", closed["close"]), 2)
                confirmed = self.conf_low > 0 and ltp < self.conf_low
                if confirmed and not at_entry_price(ltp, self.conf_low, "SELL"):
                    confirmed = False
                if confirmed:
                    self.state        = "CONFIRMED_SELL"
                    self.trigger_level = "R1"
                    fp       = self.frozen_pivots if self.frozen_pivots else pivots
                    entry_px = round(self.conf_low, 2)
                    # SL is the sweep HH, never R2 or a % of entry (strictly > entry_px)
                    effective_sl = round(max(self.hh, self.conf_high), 2)
                    if effective_sl <= entry_px:
                        self.state = "RECLAIM_R1"
                        self.signal = f"▼ {self.opt_type} R1 No HH above entry {entry_px:.2f} — no trade"
                        confirmed = False
                if confirmed:
                    t1 = round(fp.get('P',  p),  2)
                    t2 = round(fp.get('S1', s1), 2)
                    t3 = round(fp.get('S2', s2), 2)
                    t4 = round(fp.get('S3', s3), 2)
                    t5 = round(fp.get('S3', s3) * 0.95, 2)

                    self.entry_price  = entry_px
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    self.entry_sl     = effective_sl
                    self.targets      = [t1, t2, t3, t4, t5]
                    mkt_ctx = "Mkt Bearish ↓" if self.opt_type == "CE" else "Mkt Bullish ↑"
                    self.signal       = f"▼ SELL {self.opt_type} [{mkt_ctx} | R1 Reject Confirmed | Entry:{entry_px:.2f} | HH SL:{effective_sl:.2f} | T1:{t1:.1f} T2:{t2:.1f} T3:{t3:.1f} T4:{t4:.1f} T5:{t5:.1f}]"
                    self.trade_status = "Active"

        # ═══════════════════════════════════════════════════════
        # 3. ACTIVE TRADE MONITORING — SL & Target tracking
        # ═══════════════════════════════════════════════════════
        if self.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit"):
            # ── Stale-state guard (only applies before targets hit) ──────────────────────────
            # Disabled: an active trade is never reset mid-session; it runs until its own SL / target / 3 PM exit.
            if False and self.trade_status == "Active" and self.entry_price > 0 and ltp > 0:
                stale_buy  = "BUY"  in self.signal and self.entry_price > ltp * 1.5
                stale_sell = "SELL" in self.signal and self.entry_price < ltp * 0.67
                if stale_buy or stale_sell:
                    self.state        = "WAITING"
                    self.trade_status = "Waiting"
                    self.sl = self.entry_price = self.entry_sl = 0.0
                    self.ll = self.hh = 0.0
                    self.retest_done  = False
                    self.retest_low   = 0.0
                    self.retest_high  = 0.0
                    self.conf_high = self.conf_low = 0.0
                    self.sweep_bucket = -1
                    self.targets      = []
                    self.signal       = "WAITING"
                    self.frozen_s2    = self.frozen_s1 = 0.0
                    self.frozen_r2    = self.frozen_r1 = 0.0
                    self.frozen_pivots = {}
                    self.stale_reset  = True   # signals main loop to force-close tracker

        if self.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit"):
            if "BUY" in self.signal:
                if self.sl > 0 and ltp <= self.sl:
                    self.trade_status = "Trailing SL Hit" if (self.entry_price > 0 and self.sl >= self.entry_price) else "SL Hit"
                    self.signal = f"★ BUY {self.opt_type} {self.trade_status} @ {ltp:.2f} [Secured/Exited]"
                elif self.targets:
                    # Support & Target progression:
                    # When a target level is reached, it becomes the new support!
                    # If price sustains above it, we hold and advance to the NEXT target:
                    if len(self.targets) > 4 and ltp >= self.targets[4]:
                        self.trade_status = "Target 5 Hit"
                        self.signal = f"★ BUY {self.opt_type} All Targets Hit! Final T5:{self.targets[4]:.1f} @ {ltp:.2f}"
                    elif len(self.targets) > 3 and ltp >= self.targets[3]:
                        self.trade_status = "Target 4 Hit"
                        if TRAIL_SL and (len(self.targets) > 2 and self.targets[2] > self.sl):
                            self.sl = round(self.targets[2], 2)   # trail SL to T3
                        t5_val = self.targets[4] if len(self.targets) > 4 else round(self.targets[3] * 1.05, 2)
                        self.signal = f"★ BUY {self.opt_type} Target 4 Hit @ {ltp:.2f} | Support Sustained → Aiming T5:{t5_val:.1f} | SL:{self.sl:.1f}"
                    elif len(self.targets) > 2 and ltp >= self.targets[2]:
                        self.trade_status = "Target 3 Hit"
                        if TRAIL_SL and (len(self.targets) > 1 and self.targets[1] > self.sl):
                            self.sl = round(self.targets[1], 2)   # trail SL to T2
                        self.signal = f"★ BUY {self.opt_type} Target 3 Hit @ {ltp:.2f} | Support Sustained → Aiming T4:{self.targets[3]:.1f} | SL:{self.sl:.1f}"
                    elif len(self.targets) > 1 and ltp >= self.targets[1]:
                        self.trade_status = "Target 2 Hit"
                        if TRAIL_SL and (self.targets[0] > self.sl):
                            self.sl = round(self.targets[0], 2)   # trail SL to T1
                        self.signal = f"★ BUY {self.opt_type} Target 2 Hit @ {ltp:.2f} | Support Sustained → Aiming T3:{self.targets[2]:.1f} | SL:{self.sl:.1f}"
                    elif ltp >= self.targets[0]:
                        self.trade_status = "Target 1 Hit"
                        if TRAIL_SL and (self.entry_price > self.sl):
                            self.sl = round(self.entry_price, 2)  # trail SL to Entry (breakeven)
                        self.signal = f"★ BUY {self.opt_type} Target 1 Hit @ {ltp:.2f} | Support Sustained → Aiming T2:{self.targets[1]:.1f} | SL:{self.sl:.1f}"
            elif "SELL" in self.signal:
                if self.sl > 0 and ltp >= self.sl:
                    self.trade_status = "Trailing SL Hit" if (self.entry_price > 0 and self.sl <= self.entry_price) else "SL Hit"
                    self.signal = f"▼ SELL {self.opt_type} {self.trade_status} @ {ltp:.2f} [Secured/Exited]"
                elif self.targets:
                    if len(self.targets) > 4 and ltp <= self.targets[4]:
                        self.trade_status = "Target 5 Hit"
                        self.signal = f"▼ SELL {self.opt_type} All Targets Hit! Final T5:{self.targets[4]:.1f} @ {ltp:.2f}"
                    elif len(self.targets) > 3 and ltp <= self.targets[3]:
                        self.trade_status = "Target 4 Hit"
                        if TRAIL_SL and (len(self.targets) > 2 and (self.sl == 0 or self.targets[2] < self.sl)):
                            self.sl = round(self.targets[2], 2)
                        t5_val = self.targets[4] if len(self.targets) > 4 else round(self.targets[3] * 0.95, 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 4 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T5:{t5_val:.1f} | SL:{self.sl:.1f}"
                    elif len(self.targets) > 2 and ltp <= self.targets[2]:
                        self.trade_status = "Target 3 Hit"
                        if TRAIL_SL and (len(self.targets) > 1 and (self.sl == 0 or self.targets[1] < self.sl)):
                            self.sl = round(self.targets[1], 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 3 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T4:{self.targets[3]:.1f} | SL:{self.sl:.1f}"
                    elif len(self.targets) > 1 and ltp <= self.targets[1]:
                        self.trade_status = "Target 2 Hit"
                        if TRAIL_SL and (self.sl == 0 or self.targets[0] < self.sl):
                            self.sl = round(self.targets[0], 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 2 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T3:{self.targets[2]:.1f} | SL:{self.sl:.1f}"
                    elif ltp <= self.targets[0]:
                        self.trade_status = "Target 1 Hit"
                        if TRAIL_SL and (self.sl == 0 or self.entry_price < self.sl):
                            self.sl = round(self.entry_price, 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 1 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T2:{self.targets[1]:.1f} | SL:{self.sl:.1f}"


def select_focus_strikes_from_pivots(pivots: dict, step: int = 50, tf_label: str = "15-Min") -> dict:
    """
    Selects 4 to 6 focus strikes from given Fibonacci Pivots (PDF Section 3 & 4):
      - 2 or 3 CE strikes: nearest strikes to R1 and R2 (and R3 for extreme expansion)
      - 2 or 3 PE strikes: nearest strikes to S1 and S2 (and S3 for extreme expansion)
    """
    if not pivots:
        return {}
    r1 = pivots.get("R1", 0)
    r2 = pivots.get("R2", 0)
    r3 = pivots.get("R3", 0)
    s1 = pivots.get("S1", 0)
    s2 = pivots.get("S2", 0)
    s3 = pivots.get("S3", 0)
    p  = pivots.get("P", 0)

    def snap(val): return int(round(val / step) * step) if val else 0

    ce1, ce2 = snap(r1), snap(r2)
    ce3 = snap(r3) if r3 else 0
    pe1, pe2 = snap(s1), snap(s2)
    pe3 = snap(s3) if s3 else 0

    if ce2 == ce1 and ce1 > 0: ce2 = ce1 + step
    if pe2 == pe1 and pe1 > 0: pe2 = max(pe1 - step, step)
    if ce3 == ce2 and ce2 > 0: ce3 = ce2 + step
    if pe3 == pe2 and pe2 > 0: pe3 = max(pe2 - step, step)

    # Pivot (P) strike — traded on BOTH CE and PE alongside the R1/R2 (CE) and S1/S2 (PE) strikes
    p_stk = snap(p)
    # Strikes either side of P (e.g. P=22622 → 22600 / 22650). If P sits exactly on a strike,
    # use one strike below and one above. Both are traded on CE and PE like P_STRIKE.
    if p:
        p_lo = int(math.floor(p / step) * step)
        p_hi = int(math.ceil(p / step) * step)
        if p_lo == p_hi:
            p_lo, p_hi = p_lo - step, p_hi + step
    else:
        p_lo = p_hi = 0
    p_strikes = sorted({x for x in (p_lo, p_stk, p_hi) if x > 0})

    # "all_4" = the traded/subscribed set: R1, R2, P zone, S1, S2 strikes (name kept for compatibility)
    all_4 = sorted(list({s for s in (ce1, ce2, pe1, pe2, *p_strikes) if s > 0}))
    all_6 = sorted(list({s for s in (ce1, ce2, ce3, pe1, pe2, pe3, *p_strikes) if s > 0}))

    return {
        "timeframe": tf_label,
        "CE1": ce1, "CE2": ce2, "CE3": ce3,
        "PE1": pe1, "PE2": pe2, "PE3": pe3,
        "P_STRIKE": p_stk,
        "P_LOW": p_lo, "P_HIGH": p_hi,
        "p_strikes": p_strikes,
        "ce_strikes": [s for s in (ce1, ce2, ce3) if s > 0],
        "pe_strikes": [s for s in (pe1, pe2, pe3) if s > 0],
        "all_4": all_4,
        "all_6": all_6,
        "P": p, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3
    }


def strike_levels(strike, *focus_dicts) -> set:
    """Pivot levels a strike stands for in the given 15M/1H focus dicts:
    R1/R2 (CE1/CE2), S1/S2 (PE1/PE2), P (strike nearest P) and PN (the strikes next to P)."""
    out = set()
    try:
        stk = int(round(float(strike)))
    except (TypeError, ValueError):
        return out
    for f in focus_dicts:
        if not f:
            continue
        for key, lv in (("CE1", "R1"), ("CE2", "R2"), ("PE1", "S1"), ("PE2", "S2"), ("P_STRIKE", "P")):
            if f.get(key) and int(f[key]) == stk:
                out.add(lv)
        if stk in set(f.get("p_strikes") or []) and stk != f.get("P_STRIKE"):
            out.add("PN")
    return out


def strike_levels_tf(strike, f15: dict, f1h: dict) -> set:
    """Levels qualified by timeframe so 15M and 1H strikes never mix, e.g. {'15M:R1', '1H:PN'}."""
    return {f"15M:{x}" for x in strike_levels(strike, f15)} | {f"1H:{x}" for x in strike_levels(strike, f1h)}


def select_intraday_15m_focus_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Selects INTRADAY focus strikes based strictly on 15-Minute Fibonacci Pivots:
      - 2 CE strikes: nearest strikes to 15M R1 and 15M R2
      - 2 PE strikes: nearest strikes to 15M S1 and 15M S2
    """
    piv = mtf_pivots.get("15-Min", {})
    return select_focus_strikes_from_pivots(piv, step, tf_label="15-Min")


def select_1h_structure_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Selects 1-HOUR STRUCTURE focus strikes based strictly on 1-Hour Fibonacci Pivots:
      - 2 CE strikes: nearest strikes to 1H R1 and 1H R2
      - 2 PE strikes: nearest strikes to 1H S1 and 1H S2
    """
    piv = mtf_pivots.get("1-Hour", {})
    return select_focus_strikes_from_pivots(piv, step, tf_label="1-Hour")


def select_daily_4_focus_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Intraday strike selection using 15-Minute timeframe as primary focus for intraday trades (2 CE + 2 PE),
    while also computing 1-Hour structure strikes for overall market context (2 CE + 2 PE),
    with optional S3/R3 strikes (up to 6 strikes).
    """
    f15 = select_intraday_15m_focus_strikes(mtf_pivots, step)
    f1h = select_1h_structure_strikes(mtf_pivots, step)

    res = dict(f15)
    res["15M"] = f15
    res["1H"]  = f1h
    res["all_15m"] = f15.get("all_4", [])
    res["all_1h"]  = f1h.get("all_4", [])
    res["all_15m_6"] = f15.get("all_6", f15.get("all_4", []))
    res["all_1h_6"]  = f1h.get("all_6", f1h.get("all_4", []))
    res["all_focus"] = sorted(list(set(f15.get("all_4", []) + f1h.get("all_4", []))))
    res["all_focus_6"] = sorted(list(set(res["all_15m_6"] + res["all_1h_6"])))
    return res


def select_strikes_by_1h_bias(mtf_pivots: dict, h1_bias: str, step: int = 50) -> dict:
    """Backward compatibility wrapper — delegates to select_daily_4_focus_strikes."""
    res = select_daily_4_focus_strikes(mtf_pivots, step)
    res["bias"] = h1_bias
    return res


def check_option_levels(ltp: float, pivots: dict, opt_type: str = "CE") -> dict:
    """
    Checks whether an option premium is trading between or near its Support (S1/S2)
    or Resistance (R1/R2) levels.
    - CE: Support (S1–S2 / near S1 / near S2) for dip buy / reversal.
    - PE: Resistance (R1–R2 / near R1 / near R2) as requested ("put near resistance"), plus support.
    """
    if not pivots or ltp <= 0:
        return {"signal": "", "zone": "NORMAL", "is_match": False, "category": ""}

    p  = pivots["P"]
    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
    s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]

    tol_s1 = max(s1 * 0.02, 1.0)
    tol_s2 = max(s2 * 0.02, 1.0)
    tol_r1 = max(r1 * 0.02, 1.0)
    tol_r2 = max(r2 * 0.02, 1.0)
    tol_p  = max(p  * 0.015, 0.5)

    # 1. In Resistance zone / Below R2 -> Direct SELL area (below R2)
    if (r1 <= ltp <= r2) or (p <= ltp <= r2):
        return {
            "signal": f"▼ SELL ACTIVE [Below R2 | HH SL]",
            "zone": "BELOW_R2",
            "is_match": True,
            "category": "PE_RES" if opt_type=="PE" else "CE_RES",
            "detail": f"Sell Area Below R2 ({r1}–{r2})"
        }

    # 2. In Support zone / Above S2 -> Direct BUY area (above S2)
    if (s2 <= ltp <= s1) or (s2 <= ltp <= p):
        return {
            "signal": f"★ BUY ACTIVE [Above S2 | LL SL]",
            "zone": "ABOVE_S2",
            "is_match": True,
            "category": "CE_SUP" if opt_type=="CE" else "PE_SUP",
            "detail": f"Buy Area Above S2 ({s2}–{s1})"
        }

    # 3. Near Resistance -> SELL trigger zone
    if abs(ltp - r2) <= tol_r2:
        return {
            "signal": f"▼ SELL ACTIVE [Near R2 | HH SL]",
            "zone": "NEAR_R2",
            "is_match": True,
            "category": "PE_RES" if opt_type=="PE" else "CE_RES",
            "detail": f"Sell Area Near R2 ({r2})"
        }
    if abs(ltp - r1) <= tol_r1:
        return {
            "signal": f"▼ SELL ACTIVE [Near R1 | HH SL]",
            "zone": "NEAR_R1",
            "is_match": True,
            "category": "PE_RES" if opt_type=="PE" else "CE_RES",
            "detail": f"Sell Area Near R1 ({r1})"
        }

    # 4. Near Support -> BUY trigger zone
    if abs(ltp - s2) <= tol_s2:
        return {
            "signal": f"★ BUY ACTIVE [Near S2 | LL SL]",
            "zone": "NEAR_S2",
            "is_match": True,
            "category": "CE_SUP" if opt_type=="CE" else "PE_SUP",
            "detail": f"Buy Area Near S2 ({s2})"
        }
    if abs(ltp - s1) <= tol_s1:
        return {
            "signal": f"★ BUY ACTIVE [Near S1 | LL SL]",
            "zone": "NEAR_S1",
            "is_match": True,
            "category": "CE_SUP" if opt_type=="CE" else "PE_SUP",
            "detail": f"Buy Area Near S1 ({s1})"
        }

    # 5. Near Pivot P
    if abs(ltp - p) <= tol_p:
        return {"signal": "Near P", "zone": "NEAR_P", "is_match": False, "category": "PIVOT", "detail": f"Near Pivot ({p})"}

    if ltp > r2: return {"signal": "> R2", "zone": "ABOVE_R2", "is_match": False, "category": "", "detail": "Above R2"}
    if ltp < s2: return {"signal": "< S2", "zone": "BELOW_S2", "is_match": False, "category": "", "detail": "Below S2"}

    return {"signal": "", "zone": "NORMAL", "is_match": False, "category": "", "detail": ""}

# ═══════════════════════════════════════════════════════════
# TRADINGVIEW MULTI-TIMEFRAME PIVOTS & NOTIFICATION SYSTEM
# ═══════════════════════════════════════════════════════════
import subprocess

ALERT_LOG_FILE    = os.path.join(BASE_DIR, "alerts_history.csv")
FORWARD_TEST_FILE = os.path.join(BASE_DIR, "forward_test_trades.csv")

# ── Intraday EOD rules ─────────────────────────────────────
EOD_SQUARE_OFF_TIME  = dt_time(15, 0, 0)    # close all open intraday trades at CMP
# Trailing SL (T1 -> entry, T2 -> T1 ...) is OFF (AJ, 13 Oct): every trade keeps the SL it was
# entered with, the sweep LL for BUY and the HH for SELL, until SL, T5 or the 3 PM exit.
TRAIL_SL = False
NEXT_DAY_LEVELS_TIME = dt_time(15, 45, 0)   # compute next-day levels after close

# ── Multi-symbol forward test (other symbols from calculator) ──
MULTI_FT_FILE          = os.path.join(BASE_DIR, "multi_symbol_forward_test.csv")
MULTI_WATCHLIST_FILE   = os.path.join(BASE_DIR, "multi_symbol_watchlist.json")
MULTI_LEVELS_FILE      = os.path.join(BASE_DIR, "multi_symbol_levels.json")
MULTI_SHEET_META_FILE  = os.path.join(BASE_DIR, "multi_symbol_sheet.json")

_alert_history = {}

def trigger_immediate_alert(title: str, message: str, sound: bool = True):
    """
    Triggers an instant macOS desktop banner notification and plays a gentle low-volume beep.
    Rate-limited to once every 45s per message to prevent audio spam.
    """
    now = time.time()
    if now - _alert_history.get(message, 0) < 45:
        return
    _alert_history[message] = now

    def _notify():
        try:
            safe_title = title.replace('"', '\\"')
            safe_msg   = message.replace('"', '\\"')
            # Native macOS Notification banner
            subprocess.run([
                "osascript", "-e",
                f'display notification "{safe_msg}" with title "{safe_title}"'
            ], capture_output=True)
            if sound:
                # Gentle low-volume beep on macOS (-v 0.15 gives a pleasant, quiet alert)
                subprocess.run(["afplay", "-v", "0.15", "/System/Library/Sounds/Tink.aiff"], capture_output=True)
        except Exception as e:
            print(f"Desktop alert error: {e}")

    threading.Thread(target=_notify, daemon=True).start()


class AlertManager:
    """
    Deduplicates alerts so each setup per strike is sent ONCE per day.
    Saves all alerts to alerts_history.csv for auditing.
    """
    def __init__(self, filepath=ALERT_LOG_FILE):
        self.filepath = filepath
        self.sent_today = set()
        self._init_file()

    def _init_file(self):
        if not os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(['Timestamp', 'Date', 'Symbol', 'Type', 'Strike', 'Signal', 'Price', 'Zone', 'SL', 'Target'])
            except Exception as e:
                print(f"Alert log init error: {e}")

    def send_alert_once(self, symbol: str, opt_type: str, strike: int, signal: str, price: float, zone: str, sl: float, target: float):
        # Alerts ONLY active during live market hours
        if not is_live_market_open():
            return False

        date_str = datetime.now().strftime('%Y-%m-%d')
        alert_key = (date_str, symbol, opt_type, strike, signal)
        if alert_key in self.sent_today:
            return False

        self.sent_today.add(alert_key)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            with open(self.filepath, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([now_str, date_str, symbol, opt_type, strike, signal, f"{price:.2f}", zone, f"{sl:.2f}", f"{target:.2f}"])
        except Exception as e:
            print(f"Alert append error: {e}")

        # Trigger desktop banner and audible notification
        icon = "🚨" if "BUY" in signal else "🔴"
        trigger_immediate_alert(
            f"{icon} {signal} — {symbol} {strike}",
            f"{opt_type} {strike} @ {price:.2f} | {zone} | SL:{sl:.1f} | T1:{target:.1f}"
        )
        return True

    def send_exit_alert(self, trade: dict) -> bool:
        """Exit alert (SL / target / 3 PM close) — only for 15M and 1H trades."""
        tf = str(trade.get('Timeframe', '') or '')
        if not ('15M' in tf or '1H' in tf):
            return False
        reason = str(trade.get('Exit_Reason', '') or 'Exit')
        sig = f"EXIT {trade.get('Action', '')} {trade.get('Type', '')} #{trade.get('Trade_ID', '')} {reason}"
        date_str = datetime.now().strftime('%Y-%m-%d')
        key = (date_str, trade.get('Symbol', ''), trade.get('Type', ''), trade.get('Strike', 0), sig)
        if key in self.sent_today:
            return False
        self.sent_today.add(key)
        px  = _safe_float(trade.get('Exit_Price', 0))
        pnl = _safe_float(trade.get('PnL_Pts', 0))
        try:
            with open(self.filepath, 'a', newline='') as f:
                csv.writer(f).writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), date_str,
                                        trade.get('Symbol', ''), trade.get('Type', ''), trade.get('Strike', 0),
                                        sig, f"{px:.2f}", tf, f"{_safe_float(trade.get('SL', 0)):.2f}", f"{pnl:+.2f}"])
        except Exception as e:
            print(f"Alert append error: {e}")
        icon = "✅" if pnl >= 0 else "🛑"
        trigger_immediate_alert(
            f"{icon} EXIT {trade.get('Action', '')} {trade.get('Type', '')} {trade.get('Strike', '')} — {reason}",
            f"{tf} | Exit @ {px:.2f} | PnL {pnl:+.2f} pts"
        )
        return True


def _safe_float(val, default=0.0):
    """Safely convert a value to float, returning default on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════
# LIVE ORDER MANAGER — real orders via Angel One / Dhan / XTS (brokers/)
# ═══════════════════════════════════════════════════════════
ORDER_LOG_FILE = os.path.join(BASE_DIR, "live_orders_log.csv")

def _write_env(updates: dict):
    """Set KEY=VALUE lines in the .env file next to the app (keeps every other line) and in os.environ.
    Values are never printed or logged."""
    path = os.path.join(BASE_DIR, ".env")
    lines = []
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    done = set()
    for i, ln in enumerate(lines):
        k = ln.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    for k, val in updates.items():
        if k not in done:
            lines.append(f"{k}={val}")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)
    for k, val in updates.items():
        os.environ[k] = val


class LiveOrderManager:
    """
    Bridge between the forward-test tracker and the broker order engine (brokers/engine.py).

    There is no paper order mode: when auto-trading is ON, every 15M/1H entry the tracker
    records is sent to the connected broker (Angel One, Dhan or XTS) as a real order, with a
    stop-loss order at the broker, and every exit (SL, target, 3 PM, manual) is sent too.
    When auto-trading is OFF nothing is sent; the forward test in the sheet keeps running.
    Every order is logged to live_orders_log.csv and the "Orders Log" sheet tab.
    """

    def __init__(self):
        self.sheet_queue   = []             # order rows waiting to be appended to the "Orders Log" tab
        self.sheet_lock    = threading.Lock()
        self._init_log()
        self.engine = OrderEngine(
            BASE_DIR,
            log=print,
            alert=lambda t, m: trigger_immediate_alert(t, m),
            order_logger=lambda **k: self._log_order(**k),
            market_open=is_live_market_open,
            session_getter=lambda: Session.obj,
            session_relogin=lambda: Session.login(),
            env_reader=lambda k: os.getenv(k, "") or "",
        )

    @property
    def mode(self):
        return "LIVE" if self.engine.auto_trade else "OFF"

    def _init_log(self):
        if not os.path.exists(ORDER_LOG_FILE):
            try:
                with open(ORDER_LOG_FILE, 'w', newline='') as f:
                    csv.writer(f).writerow([
                        'Timestamp', 'Mode', 'Symbol', 'Type', 'Strike',
                        'Token', 'Action', 'Qty', 'Price', 'Order_ID',
                        'Status', 'Remark'
                    ])
            except Exception as e:
                print(f"[OrderLog] Init error: {e}")

    def _log_order(self, mode, symbol, opt_type, strike, token, action,
                   qty, price, order_id, status, remark=""):
        try:
            row = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                mode, symbol, opt_type, strike, token,
                action, qty, f"{price:.2f}", order_id, status, remark
            ]
            with open(ORDER_LOG_FILE, 'a', newline='') as f:
                csv.writer(f).writerow(row)
            with self.sheet_lock:
                self.sheet_queue.append([str(v) for v in row])
        except Exception as e:
            print(f"[OrderLog] Append error: {e}")

    @staticmethod
    def instrument_for(trade: dict) -> dict:
        """Contract details for a trade, from the Angel instrument list (exact token match)."""
        token = str(trade.get('Token', '') or '')
        inst = {
            "symbol": trade.get('Symbol', ''), "exchange": trade.get('Exchange', 'NFO') or 'NFO',
            "token": token, "tsym": "", "expiry": trade.get('Expiry', ''),
            "strike": trade.get('Strike'), "opt_type": trade.get('Type', ''), "lot_size": 0,
        }
        try:
            df = Session.scrip_df
            if df is not None and token:
                m = df[df["token"].astype(str) == token]
                if not m.empty:
                    row = m.iloc[0]
                    inst["tsym"] = str(row.get("symbol", ""))
                    inst["lot_size"] = int(float(row.get("lotsize", 0) or 0))
                    inst["exchange"] = str(row.get("exch_seg", inst["exchange"]) or inst["exchange"])
                    if not inst["expiry"]:
                        inst["expiry"] = str(row.get("expiry", ""))
        except Exception as e:
            print(f"[ORDERS] Instrument lookup error for token {token}: {e}")
        return inst

    def place_order(self, trade: dict, levels=None) -> str:
        """Send the entry for a newly recorded forward-test trade (only when auto-trading is ON).
        levels: the strike's pivot levels (R1/R2/S1/S2/P/PN), checked against the strike selection."""
        if not self.engine.auto_trade:
            return "No order: auto-trading off"
        inst = self.instrument_for(trade)
        inst["levels"] = sorted(levels or [])
        return self.engine.enter(trade, inst)

    def exit_order(self, trade: dict, reason: str, ltp: float) -> str:
        """Exit the broker position behind a forward-test trade. Safe to call more than once."""
        return self.engine.exit(trade, reason, ltp)

    def on_tick(self, trade: dict, ltp: float):
        self.engine.on_tick(trade, ltp)

    def pop_forced_close(self, trade_id):
        return self.engine.pop_forced_close(trade_id)

    def order_ref(self, trade_id):
        return self.engine.order_ref(trade_id)


# Singleton order manager instance
live_order_manager = LiveOrderManager()


def compute_points_summary(closed_rows, active_trades=None) -> dict:
    """
    +/- points calculation across trades.
    closed_rows: list of dicts with 'PnL_Pts'; active_trades: list of dicts with live 'PnL_Pts'.
    """
    pnls   = [_safe_float(r.get('PnL_Pts', 0)) for r in (closed_rows or [])]
    plus   = round(sum(p for p in pnls if p > 0), 2)
    minus  = round(sum(p for p in pnls if p < 0), 2)
    wins   = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    live   = round(sum(_safe_float(t.get('PnL_Pts', t.get('Live_PnL', 0))) for t in (active_trades or [])), 2)
    net    = round(plus + minus, 2)
    n      = len(pnls)
    return {
        "trades": n, "wins": wins, "losses": losses,
        "plus_pts": plus, "minus_pts": minus, "net_pts": net,
        "live_pts": live, "net_incl_live": round(net + live, 2),
        "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
        "avg_pts": round(net / n, 2) if n else 0.0,
    }


DAILY_PNL_HEADERS = ['Date', 'BUY Trades', 'BUY Wins', 'BUY Losses', 'BUY PnL (Pts)',
                     'SELL Trades', 'SELL Wins', 'SELL Losses', 'SELL PnL (Pts)',
                     'Total Trades', 'Win %', 'NET PnL (Pts)', 'Open Live (Pts)', 'Cumulative NET (Pts)']


def daily_pnl_table(closed_rows, active_trades=None) -> list:
    """Date-wise points: BUY and SELL separately, from every CLOSED forward-test trade.
    Newest date first, with a running cumulative NET and an ALL-DAYS total row at the end."""
    days = {}
    for r in closed_rows or []:
        if str(r.get('Status', '')).strip() != 'CLOSED':
            continue
        d = str(r.get('Date', '')).strip()
        if not d:
            continue
        side = 'SELL' if str(r.get('Action', '')).upper() == 'SELL' else 'BUY'
        pnl = _safe_float(r.get('PnL_Pts', 0))
        x = days.setdefault(d, {'BUY': [0, 0, 0, 0.0], 'SELL': [0, 0, 0, 0.0], 'live': 0.0})
        x[side][0] += 1
        x[side][1] += 1 if pnl > 0 else 0
        x[side][2] += 1 if pnl < 0 else 0
        x[side][3] += pnl
    for t in active_trades or []:
        d = str(t.get('Date', '')).strip()
        if d:
            x = days.setdefault(d, {'BUY': [0, 0, 0, 0.0], 'SELL': [0, 0, 0, 0.0], 'live': 0.0})
            x['live'] += _safe_float(t.get('PnL_Pts', t.get('Live_PnL', 0)))
    cum, out = 0.0, []
    tot = {'BUY': [0, 0, 0, 0.0], 'SELL': [0, 0, 0, 0.0], 'live': 0.0}
    for d in sorted(days):
        x = days[d]
        b, sl = x['BUY'], x['SELL']
        n, w = b[0] + sl[0], b[1] + sl[1]
        net = round(b[3] + sl[3], 2)
        cum = round(cum + net, 2)
        out.append([d, b[0], b[1], b[2], round(b[3], 2), sl[0], sl[1], sl[2], round(sl[3], 2),
                    n, round(100.0 * w / n, 1) if n else 0.0, net, round(x['live'], 2), cum])
        for k in ('BUY', 'SELL'):
            tot[k] = [tot[k][i] + x[k][i] for i in range(4)]
        tot['live'] += x['live']
    out.reverse()
    b, sl = tot['BUY'], tot['SELL']
    n, w = b[0] + sl[0], b[1] + sl[1]
    out.append(['ALL DAYS', b[0], b[1], b[2], round(b[3], 2), sl[0], sl[1], sl[2], round(sl[3], 2),
                n, round(100.0 * w / n, 1) if n else 0.0, round(b[3] + sl[3], 2), round(tot['live'], 2), cum])
    return out


SIDE_SHEET_HEADERS = ['Date', 'Entry_Time', 'Timeframe', 'Symbol', 'Type', 'Strike', 'Entry', 'SL', 'T1',
                      'Risk (Pts)', 'Best Move For Us (Pts)', 'Exit / 3PM LTP', 'Exit_Time', 'Exit Reason',
                      'PnL (Pts)', 'Status']


def side_trades_table(rows, side: str, active_trades=None) -> list:
    """All BUY (or SELL) forward-test trades, newest first: open ones first (live price), then
    closed ones. Trades still open at 3 PM are closed at the 3 PM LTP and show it here."""
    out = []
    def _row(r, live=False):
        e, sl = _safe_float(r.get('Entry_Price', 0)), _safe_float(r.get('Initial_SL') or r.get('SL', 0))
        hi, lo = _safe_float(r.get('Trade_High', e)), _safe_float(r.get('Trade_Low', e))
        best = (hi - e) if side == 'BUY' else (e - lo)
        px = _safe_float(r.get('Current_Price', e)) if live else _safe_float(r.get('Exit_Price', 0))
        return [r.get('Date', ''), r.get('Entry_Time', ''), r.get('Timeframe', ''), r.get('Symbol', ''),
                r.get('Type', ''), r.get('Strike', ''), round(e, 2), round(sl, 2),
                round(_safe_float(r.get('Target_1', 0)), 2), round(abs(e - sl), 2), round(max(0.0, best), 2),
                round(px, 2), '' if live else r.get('Exit_Time', ''),
                'OPEN (live)' if live else r.get('Exit_Reason', ''),
                round(_safe_float(r.get('PnL_Pts', r.get('Live_PnL', 0))), 2), 'ACTIVE' if live else 'CLOSED']
    for t in active_trades or []:
        if str(t.get('Action', '')).upper() == side:
            out.append(_row(t, live=True))
    closed = [r for r in rows or [] if str(r.get('Status', '')).strip() == 'CLOSED'
              and str(r.get('Action', '')).upper() == side]
    closed.sort(key=lambda r: (str(r.get('Date', '')), str(r.get('Entry_Time', ''))), reverse=True)
    out += [_row(r) for r in closed]
    return out


def format_points_bar(title: str, s: dict) -> str:
    """One-line +/- points banner for the top of a sheet."""
    return (f"{title}  |  Trades: {s['trades']}  |  ✅ Wins: {s['wins']} (+{s['plus_pts']:.2f} pts)  |  "
            f"🛑 Losses: {s['losses']} ({s['minus_pts']:.2f} pts)  |  "
            f"{'🟢' if s['net_pts'] >= 0 else '🔴'} NET: {s['net_pts']:+.2f} pts  |  "
            f"Live Open: {s['live_pts']:+.2f}  |  Net incl. Live: {s['net_incl_live']:+.2f}  |  "
            f"Win%: {s['win_rate']:.1f}  |  Avg/Trade: {s['avg_pts']:+.2f}")


def fwd_exit_check(trade, ltp, opt_type=None, strike=None):
    """SL / target ladder for one price of an open forward-test trade (the live tick rules).
    Moves Target_Hit and trails the SL in place. Returns (closed, exit_reason, exit_pnl)."""
    opt_type = trade.get('Type') if opt_type is None else opt_type
    strike = trade.get('Strike') if strike is None else strike
    entry = trade['Entry_Price']
    target_hit = trade.get('Target_Hit', 0)
    closed, exit_reason, exit_pnl = False, '', 0.0
    if trade['Action'] == 'BUY':
        if trade['SL'] > 0 and ltp <= trade['SL']:
            closed       = True
            exit_reason  = 'Trailing SL Hit' if trade['SL'] >= entry else 'SL Hit'
            exit_pnl     = round(ltp - entry, 2)
        elif trade.get('Target_5', 0) > 0 and ltp >= trade['Target_5']:
            closed       = True
            exit_reason  = 'Target 5 Hit'
            exit_pnl     = round(ltp - entry, 2)
            trade['Target_Hit'] = 5
        elif trade.get('Target_4', 0) > 0 and ltp >= trade['Target_4']:
            if target_hit < 4:
                trade['Target_Hit'] = 4
                if TRAIL_SL and (trade.get('Target_3', 0) > trade['SL']):
                    trade['SL'] = trade['Target_3']   # trail SL to T3
                print(f"🎯 [FWD TEST] T4 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T5:{trade.get('Target_5',0):.1f} | SL:{trade['SL']:.1f}")
        elif trade.get('Target_3', 0) > 0 and ltp >= trade['Target_3']:
            if target_hit < 3:
                trade['Target_Hit'] = 3
                if TRAIL_SL and (trade.get('Target_2', 0) > trade['SL']):
                    trade['SL'] = trade['Target_2']   # trail SL to T2
                print(f"🎯 [FWD TEST] T3 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T4:{trade.get('Target_4',0):.1f} | SL:{trade['SL']:.1f}")
        elif trade.get('Target_2', 0) > 0 and ltp >= trade['Target_2']:
            if target_hit < 2:
                trade['Target_Hit'] = 2
                if TRAIL_SL and (trade.get('Target_1', 0) > trade['SL']):
                    trade['SL'] = trade['Target_1']   # trail SL to T1
                print(f"🎯 [FWD TEST] T2 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T3:{trade.get('Target_3',0):.1f} | SL:{trade['SL']:.1f}")
        elif trade.get('Target_1', 0) > 0 and ltp >= trade['Target_1']:
            if target_hit < 1:
                trade['Target_Hit'] = 1
                if TRAIL_SL and (entry > trade['SL']):
                    trade['SL'] = round(entry, 2)     # trail SL to Entry (breakeven)
                print(f"🎯 [FWD TEST] T1 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T2:{trade.get('Target_2',0):.1f} | SL:{trade['SL']:.1f}")
    else:   # SELL
        if trade['SL'] > 0 and ltp >= trade['SL']:
            closed       = True
            exit_reason  = 'Trailing SL Hit' if trade['SL'] <= entry else 'SL Hit'
            exit_pnl     = round(entry - ltp, 2)
        elif trade.get('Target_5', 0) > 0 and ltp <= trade['Target_5']:
            closed       = True
            exit_reason  = 'Target 5 Hit'
            exit_pnl     = round(entry - ltp, 2)
            trade['Target_Hit'] = 5
        elif trade.get('Target_4', 0) > 0 and ltp <= trade['Target_4']:
            if target_hit < 4:
                trade['Target_Hit'] = 4
                if TRAIL_SL and (trade['SL'] == 0 or trade.get('Target_3', 0) < trade['SL']):
                    trade['SL'] = trade['Target_3']
                print(f"🎯 [FWD TEST] T4 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T5:{trade.get('Target_5',0):.1f} | SL:{trade['SL']:.1f}")
        elif trade.get('Target_3', 0) > 0 and ltp <= trade['Target_3']:
            if target_hit < 3:
                trade['Target_Hit'] = 3
                if TRAIL_SL and (trade['SL'] == 0 or trade.get('Target_2', 0) < trade['SL']):
                    trade['SL'] = trade['Target_2']
                print(f"🎯 [FWD TEST] T3 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T4:{trade.get('Target_4',0):.1f} | SL:{trade['SL']:.1f}")
        elif trade.get('Target_2', 0) > 0 and ltp <= trade['Target_2']:
            if target_hit < 2:
                trade['Target_Hit'] = 2
                if TRAIL_SL and (trade['SL'] == 0 or trade.get('Target_1', 0) < trade['SL']):
                    trade['SL'] = trade['Target_1']
                print(f"🎯 [FWD TEST] T2 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T3:{trade.get('Target_3',0):.1f} | SL:{trade['SL']:.1f}")
        elif trade.get('Target_1', 0) > 0 and ltp <= trade['Target_1']:
            if target_hit < 1:
                trade['Target_Hit'] = 1
                if TRAIL_SL and (trade['SL'] == 0 or entry < trade['SL']):
                    trade['SL'] = round(entry, 2)
                print(f"🎯 [FWD TEST] T1 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T2:{trade.get('Target_2',0):.1f} | SL:{trade['SL']:.1f}")
    return closed, exit_reason, exit_pnl


RESTART_FILL_NOTE = " (from 1-min data: app restarted)"


def replay_trade_bars(trade, bars, eod=EOD_SQUARE_OFF_TIME):
    """Finish a trade the app stopped watching (it was restarted) from that day's 1-minute
    candles, with the same SL / target / trailing rules as live ticks. Each candle is walked
    open -> low -> high -> close when green, open -> high -> low -> close when red. Fills at the
    SL / T5 level itself unless the candle opened past it. Square-off at 15:00 as live.
    bars: [(datetime, o, h, l, c), ...]. Returns the closed trade, or None while it is still open."""
    entry = trade['Entry_Price']
    try:
        start = datetime.strptime(f"{trade['Date']} {trade['Entry_Time']}", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    start = start.replace(second=0)
    is_buy = trade['Action'] == 'BUY'
    trade.setdefault('Trade_High', entry)
    trade.setdefault('Trade_Low', entry)
    trade.setdefault('Target_Hit', 0)
    last_px, done = None, None
    for t, o, h, l, c in bars:
        tn = t.replace(tzinfo=None)
        if tn <= start:
            continue                      # the entry minute itself: the part after entry is unknown
        if tn.time() >= eod:
            done = (o, '15:00:00', 'EOD 3PM Square-off @ CMP', None)
            break
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        for i, px in enumerate(path):
            trade['Trade_High'] = round(max(trade['Trade_High'], px), 2)
            trade['Trade_Low'] = round(min(trade['Trade_Low'], px) if trade['Trade_Low'] else px, 2)
            sl_before, t5 = trade['SL'], trade.get('Target_5', 0)
            closed, reason, _ = fwd_exit_check(trade, px)
            if closed:
                fill = px
                if i > 0:                 # crossed inside the candle: filled at the level
                    fill = sl_before if 'SL' in reason else (t5 or px)
                done = (fill, tn.strftime('%H:%M:00'), reason, px)
                break
        if done:
            break
        last_px = c
    if not done:
        if last_px is None or not bars or bars[-1][0].replace(tzinfo=None).time() < dt_time(14, 59):
            return None                   # the day's candles do not reach the exit yet
        done = (last_px, '15:00:00', 'EOD 3PM Square-off @ CMP', None)
    fill, when, reason, _ = done
    trade['Trade_High'] = round(max(trade['Trade_High'], fill), 2)
    trade['Trade_Low'] = round(min(trade['Trade_Low'], fill), 2)
    pnl = round((fill - entry) if is_buy else (entry - fill), 2)
    trade['Max_Pts'] = round(max(0.0, (trade['Trade_High'] - entry) if is_buy else (entry - trade['Trade_Low'])), 2)
    trade.update({'Exit_Price': round(fill, 2), 'Exit_Time': when, 'Exit_Reason': reason,
                  'PnL_Pts': pnl, 'Live_PnL': pnl, 'Status': 'CLOSED'})
    return trade


def trade_expiry(trade_id):
    """Contract expiry ('13OCT2026') from a forward-test Trade_ID, or ''."""
    m = re.search(r"_(\d{2}[A-Z]{3}\d{4})_", str(trade_id))
    return m.group(1) if m else ""


class ForwardTestTracker:
    """
    Logs every forward test trade entry & exit to forward_test_trades.csv.
    Tracks live PnL every tick — SELL PnL = Entry - Current, BUY PnL = Current - Entry.
    Active trades show real-time PnL on every sheet sync.
    Integrates with LiveOrderManager for optional live order execution.
    """
    def __init__(self, filepath=FORWARD_TEST_FILE):
        self.filepath      = filepath
        self.active_trades = {}   # (opt_type, strike) -> trade dict
        self._closed_today = []   # cache closed trades for dashboard
        # Per-day guard: once a trade is CLOSED for a key, block re-entry same day
        # Prevents infinite buy→SL hit→re-buy loop caused by stale pivot drift
        self._closed_keys_today = set()  # {(opt_type, strike, action, date_str)} — action-aware
        self._sl_hit_count      = {}     # {(opt_type, strike, action, date_str) -> int}
        self.paper_only         = False  # True => never route orders to LiveOrderManager
        # Open trades and today's re-entry guards are saved here, so an app restart
        # (internet drop, crash, Mac sleep) picks up the same trades and keeps watching them.
        self.state_file         = os.path.splitext(filepath)[0] + "_open.json"
        self._snap_ts           = 0.0
        self._seed_closed_cache = None
        self._restored_ids      = self._restore_snapshot()
        self._init_file()

    @staticmethod
    def _trade_keys(t):
        sym, ot, stk, exp = t.get('Symbol', ''), t.get('Type', ''), t.get('Strike'), t.get('Expiry', '')
        main = (sym, ot, stk, exp) if (sym and exp) else ((ot, stk, exp) if exp else (ot, stk))
        keys = [main]
        if exp:
            keys.append((ot, stk, exp))
        keys.append((ot, stk))
        return keys

    def _save_snapshot(self, force=True):
        """Write open trades + today's guards to disk (atomic). Throttled unless force."""
        if not force and time.time() - self._snap_ts < 3.0:
            return
        self._snap_ts = time.time()
        try:
            data = {"date": datetime.now().strftime('%Y-%m-%d'),
                    "active": self.get_active_trades(),
                    "closed_keys": [list(k) for k in self._closed_keys_today],
                    "sl_counts": [[list(k), n] for k, n in self._sl_hit_count.items()],
                    "closed_today": self._closed_today}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[FWD TEST] Could not save open trades: {e}")

    def _restore_snapshot(self):
        """Reload today's open trades after a restart. Returns the restored Trade_IDs."""
        try:
            with open(self.state_file) as f:
                d = json.load(f)
        except Exception:
            return set()
        now = datetime.now()
        if d.get("date") != now.strftime('%Y-%m-%d') or now.time() >= EOD_SQUARE_OFF_TIME:
            # The app was not running at 3 PM: close those trades at the last price seen,
            # so every entry has an exit and the day's points add up.
            for t in d.get("active", []):
                if t.get('Status') != 'ACTIVE':
                    continue
                e = _safe_float(t.get('Entry_Price', 0))
                px = _safe_float(t.get('Current_Price', 0)) or e
                pnl = round((px - e) if t.get('Action') == 'BUY' else (e - px), 2)
                t.update({'Exit_Price': round(px, 2), 'Exit_Time': '15:00:00', 'PnL_Pts': pnl, 'Live_PnL': pnl,
                          'Exit_Reason': 'EOD 3PM Square-off @ last LTP (app off at 3 PM)', 'Status': 'CLOSED'})
                self._update_trade_in_file(t)
                print(f"⏰ [FWD TEST] {t.get('Action')} {t.get('Type')} {t.get('Strike')} closed at last LTP "
                      f"{px:.2f} (app was off at 3 PM) | PnL:{pnl:+.2f}")
            if d.get("date") == now.strftime('%Y-%m-%d'):
                self._closed_today = list(d.get("closed_today", []))
            try:
                os.remove(self.state_file)
            except OSError:
                pass
            return set()
        tup = lambda k: tuple(int(x) if isinstance(x, float) and x.is_integer() else x for x in k)
        self._closed_keys_today = {tup(k) for k in d.get("closed_keys", [])}
        self._sl_hit_count = {tup(k): int(n) for k, n in d.get("sl_counts", [])}
        self._closed_today = list(d.get("closed_today", []))
        ids = set()
        try:
            with open(self.filepath, 'r', newline='') as f:
                entry_sl = {r.get('Trade_ID'): _safe_float(r.get('SL', 0)) for r in csv.DictReader(f)}
        except Exception:
            entry_sl = {}
        for t in d.get("active", []):
            if t.get('Status') != 'ACTIVE':
                continue
            if not t.get('Initial_SL'):
                t['Initial_SL'] = entry_sl.get(t.get('Trade_ID')) or t.get('SL', 0)
            if not TRAIL_SL and _safe_float(t.get('Initial_SL', 0)) > 0:
                t['SL'] = _safe_float(t['Initial_SL'])   # a stop trailed before the switch goes back to the LL / HH
            for k in self._trade_keys(t):
                if k not in self.active_trades:
                    self.active_trades[k] = t
            ids.add(t.get('Trade_ID'))
            print(f"♻️  [FWD TEST] Restored open {t.get('Action')} {t.get('Type')} {t.get('Strike')} "
                  f"{t.get('Expiry', '')} @ {t.get('Entry_Price')} | SL:{t.get('SL')} — watching again")
        return ids


    def _init_file(self):
        # Rows still ACTIVE in the file were left by an earlier run: they are no longer
        # tracked in memory, so mark them ABANDONED instead of leaving phantom open trades.
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', newline='') as f:
                    rows = list(csv.DictReader(f))
                    fields = list(rows[0].keys()) if rows else None
                restored = getattr(self, "_restored_ids", set())
                if fields and any(r.get('Status') == 'ACTIVE' and r.get('Trade_ID') not in restored for r in rows):
                    for r in rows:
                        if r.get('Status') == 'ACTIVE' and r.get('Trade_ID') not in restored:
                            r['Status'] = 'ABANDONED'
                            r['Exit_Reason'] = r.get('Exit_Reason') or 'App restarted - not tracked'
                    with open(self.filepath, 'w', newline='') as f:
                        w = csv.DictWriter(f, fieldnames=fields)
                        w.writeheader(); w.writerows(rows)
            except Exception as e:
                print(f"Forward test cleanup note: {e}")
        if not os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        'Trade_ID', 'Date', 'Entry_Time', 'Timeframe', 'Symbol', 'Type', 'Strike', 'Action',
                        'Entry_Price', 'SL', 'Target_1', 'Target_2', 'Target_3', 'Target_4', 'Target_5',
                        'Trade_High', 'Trade_Low', 'Max_Pts',
                        'Exit_Price', 'Exit_Time', 'Exit_Reason', 'PnL_Pts', 'Status'
                    ])
            except Exception as e:
                print(f"Forward test init error: {e}")

    def log_entry(self, symbol: str, opt_type: str, strike: int, action: str,
                  entry_price: float, sl: float, t1: float, t2: float,
                  t3: float = 0.0, t4: float = 0.0, t5: float = 0.0,
                  opt_piv: dict = None, timeframe: str = "15M Intraday",
                  token: str = "", exchange: str = "NFO", expiry: str = "", levels=None):
        """Log a new trade entry with Timeframe, Expiry, and High/Low tracking.
        opt_piv: option Fibonacci pivots at time of entry (used to record S2/R2 entry zone).
        timeframe: '15M Intraday', '1H Structure' or '15M / 1H'.
        token: Angel One instrument token (needed for live order execution).
        exchange: exchange code e.g. 'NFO', 'BFO'.
        expiry: contract expiry string e.g. '10OCT2024'.
        """
        trade_key  = (symbol, opt_type, strike, expiry) if (symbol and expiry) else ((opt_type, strike, expiry) if expiry else (opt_type, strike))
        date_str   = datetime.now().strftime('%Y-%m-%d')
        # Key includes action: BUY and SELL are tracked separately
        # Allows same strike to go BUY→target→SELL→target (bidirectional loop)
        closed_key = (symbol, opt_type, strike, expiry, action, date_str) if (symbol and expiry) else ((opt_type, strike, expiry, action, date_str) if expiry else (opt_type, strike, action, date_str))

        if not is_live_market_open():
            return None   # Entries strictly allowed during live market hours
        if datetime.now().time() >= EOD_SQUARE_OFF_TIME:
            return None   # No fresh intraday entries after 3:00 PM square-off
        if trade_key in self.active_trades:
            return None   # already tracking this strike — do not duplicate
        if closed_key in self._closed_keys_today:
            return None   # same direction already had SL/target close today — no re-entry
        # A BUY needs its SL below the entry, a SELL above it — otherwise the trade would
        # close on the very next tick (stale/trailed SL carried into a new entry).
        if sl and entry_price and ((action == "BUY" and sl >= entry_price) or (action == "SELL" and sl <= entry_price)):
            print(f"⚠️  [FWD TEST] Skipped {action} {opt_type} {strike}: SL {sl:.2f} on the wrong side of entry {entry_price:.2f}")
            return None

        now      = datetime.now()
        exp_tag  = f"_{expiry}" if expiry else ""
        trade_id = f"{symbol}_{opt_type}_{strike}{exp_tag}_{now.strftime('%Y%m%d_%H%M%S')}"

        # Record the S2 and R2 values at entry so sheet can show the valid zone
        entry_s2 = round(opt_piv.get('S2', 0), 2) if opt_piv else 0
        entry_r2 = round(opt_piv.get('R2', 0), 2) if opt_piv else 0
        entry_p  = round(opt_piv.get('P',  0), 2) if opt_piv else 0

        trade = {
            'Trade_ID':     trade_id,
            'Date':         now.strftime('%Y-%m-%d'),
            'Entry_Time':   now.strftime('%H:%M:%S'),
            'Timeframe':    timeframe,
            'Symbol':       symbol,
            'Type':         opt_type,
            'Strike':       strike,
            'Expiry':       expiry,
            'Action':       action,          # 'BUY' or 'SELL'
            'Entry_Price':  round(entry_price, 2),
            'SL':           round(sl, 2),
            'Initial_SL':   round(sl, 2),   # the LL / HH at entry; 'SL' trails at the targets
            'Target_1':     round(t1, 2),
            'Target_2':     round(t2, 2),
            'Target_3':     round(t3, 2),
            'Target_4':     round(t4, 2),
            'Target_5':     round(t5, 2),
            'Trade_High':   round(entry_price, 2),  # peak price reached during trade
            'Trade_Low':    round(entry_price, 2),  # lowest price reached during trade
            'Max_Pts':      0.0,                   # maximum profit points reached
            'Entry_S2':     entry_s2,        # option S2 at entry time
            'Entry_R2':     entry_r2,        # option R2 at entry time
            'Entry_P':      entry_p,         # option P at entry time
            'Token':        token,           # instrument token for order placement
            'Exchange':     exchange,        # exchange code
            'Order_ID':     '',              # filled by live_order_manager
            'Exit_Price':   '',
            'Exit_Time':    '',
            'Exit_Reason':  'ACTIVE',
            'PnL_Pts':      0.0,             # updated every tick
            'Live_PnL':     0.0,             # same — live mirror
            'Current_Price': round(entry_price, 2),  # starts at entry, updated every tick
            'Target_Hit':   0,               # 0=none, 1=T1 hit, 2=T2, 3=T3, 4=T4
            'Status':       'ACTIVE',
        }
        self.active_trades[trade_key] = trade
        if (opt_type, strike, expiry) not in self.active_trades and expiry:
            self.active_trades[(opt_type, strike, expiry)] = trade
        if (opt_type, strike) not in self.active_trades:
            self.active_trades[(opt_type, strike)] = trade
        self._append_row(trade)
        self._save_snapshot()
        zone_str = f"S2:{entry_s2} < LTP:{entry_price:.2f} < R2:{entry_r2}" if entry_s2 else ""
        exp_lbl  = f" | Exp:{expiry}" if expiry else ""
        print(f"📝 [FWD TEST - {timeframe}] {action} {opt_type} {strike}{exp_lbl} @ {entry_price:.2f} "
              f"| SL:{sl:.1f} | T1:{t1:.1f} T2:{t2:.1f} T3:{t3:.1f} T4:{t4:.1f} T5:{t5:.1f} | {zone_str}")

        # ── Live/Paper Order Placement ─────────────────────────────────
        if not self.paper_only:
            try:
                trade['Order_ID'] = live_order_manager.place_order(trade, levels)
            except Exception as oe:
                print(f"[ORDER] place_order error: {oe}")

        return trade

    def update_tick(self, opt_type: str, strike: int, ltp: float, expiry: str = "", symbol: str = ""):
        """
        Called every tick for every watched strike.
        Updates live PnL, tracks Trade High & Low, calculates Maximum Results (MFE),
        and checks SL / Target exit conditions.
        Returns closed trade dict if trade exited, else None.
        """
        trade_key = None
        if symbol and expiry and (symbol, opt_type, strike, expiry) in self.active_trades:
            trade_key = (symbol, opt_type, strike, expiry)
        elif expiry and (opt_type, strike, expiry) in self.active_trades:
            trade_key = (opt_type, strike, expiry)
        elif symbol and (symbol, opt_type, strike) in self.active_trades:
            trade_key = (symbol, opt_type, strike)
        elif (opt_type, strike) in self.active_trades:
            trade_key = (opt_type, strike)

        if not trade_key or ltp <= 0:
            return None
        # A tick from ANOTHER expiry of the same strike must never update this trade.
        # (Before: the (type, strike) alias matched e.g. 20OCT CE 23600 @7.30 against a
        #  13OCT CE 23600 trade @1.90 → fake instant "Target 5 Hit" / "SL Hit" exits.)
        _t_exp = self.active_trades[trade_key].get('Expiry', '')
        if expiry and _t_exp and _t_exp != expiry:
            return None

        trade    = self.active_trades[trade_key]
        time_str = datetime.now().strftime('%H:%M:%S')
        if not self.paper_only and trade.get('Order_ID') == "QUEUED":
            _ref = live_order_manager.order_ref(trade.get('Trade_ID'))
            if _ref:
                trade['Order_ID'] = _ref     # broker order id, or why no order was sent

        # A close that already happened at the broker (its SL order filled, the entry was
        # rejected, kill switch, or the position was closed in the broker app) ends this trade now.
        _forced = live_order_manager.pop_forced_close(trade.get('Trade_ID')) if not self.paper_only else None
        if _forced and _forced[0] > 0:
            ltp = _forced[0]          # the broker's fill price

        # ── Track High and Low of trade ──
        entry = trade['Entry_Price']
        cur_high = trade.get('Trade_High', entry)
        cur_low  = trade.get('Trade_Low', entry)
        if ltp > cur_high:
            trade['Trade_High'] = round(ltp, 2)
        if cur_low == 0 or ltp < cur_low:
            trade['Trade_Low'] = round(ltp, 2)

        # ── Calculate Maximum Results (MFE) ──
        # For BUY: high of the trade gives the maximum result!
        # For SELL: low of the trade gives the maximum result!
        if trade['Action'] == 'BUY':
            trade['Max_Pts'] = round(max(0.0, trade['Trade_High'] - entry), 2)
        else:
            trade['Max_Pts'] = round(max(0.0, entry - trade['Trade_Low']), 2)

        # ── Live PnL — computed correctly for BUY and SELL ──────────
        if trade['Action'] == 'BUY':
            live_pnl = round(ltp - entry, 2)
        else:   # SELL: profit when price falls
            live_pnl = round(entry - ltp, 2)

        trade['Live_PnL']      = live_pnl
        trade['PnL_Pts']       = live_pnl
        trade['Current_Price'] = round(ltp, 2)
        closed = False

        # ── Exit checks — cascade through targets T1 → T2 → T3 → T4 → T5 ──
        target_hit = trade.get('Target_Hit', 0)   # how many targets already hit
        if _forced:
            closed       = True
            exit_reason  = _forced[1]
            if exit_reason == "SL Hit (broker)":
                _trailed = (trade['SL'] >= entry) if trade['Action'] == 'BUY' else (0 < trade['SL'] <= entry)
                exit_reason = ('Trailing SL Hit' if _trailed else 'SL Hit') + ' (broker)'
            exit_pnl     = live_pnl
        else:
            closed, exit_reason, exit_pnl = fwd_exit_check(trade, ltp, opt_type, strike)

        if not closed and not self.paper_only:
            live_order_manager.on_tick(trade, ltp)    # moves the broker SL order when the SL trails
        if not closed:
            self._save_snapshot(force=trade.get('Target_Hit', 0) != target_hit)

        if closed:
            # Ensure final tick high/low are captured
            if ltp > trade.get('Trade_High', entry):
                trade['Trade_High'] = round(ltp, 2)
            if trade.get('Trade_Low', entry) == 0 or ltp < trade.get('Trade_Low', entry):
                trade['Trade_Low'] = round(ltp, 2)
            if trade['Action'] == 'BUY':
                trade['Max_Pts'] = round(max(0.0, trade['Trade_High'] - entry), 2)
            else:
                trade['Max_Pts'] = round(max(0.0, entry - trade['Trade_Low']), 2)

            trade['Exit_Price']  = round(ltp, 2)
            trade['Exit_Time']   = time_str
            trade['Exit_Reason'] = exit_reason
            trade['PnL_Pts']     = exit_pnl

            # ── Broker exit order ─────────────────────────────────────────
            if not self.paper_only:
                try:
                    live_order_manager.exit_order(trade, exit_reason, ltp)
                except Exception as oe:
                    print(f"[ORDER] exit_order error: {oe}")
            trade['Live_PnL']    = exit_pnl
            trade['Status']      = 'CLOSED'
            for k in list(self.active_trades.keys()):
                if self.active_trades[k] is trade or self.active_trades[k].get('Trade_ID') == trade.get('Trade_ID'):
                    self.active_trades.pop(k, None)

            # If first-time entry SL hit, allow re-entry with new HH/LL SL!
            date_str_close = datetime.now().strftime('%Y-%m-%d')
            exp_val = trade.get('Expiry', '')
            exp_lbl  = f" | Exp:{exp_val}" if exp_val else ""
            close_key = (trade.get('Symbol', ''), opt_type, strike, exp_val, trade['Action'], date_str_close) if exp_val else (trade.get('Symbol', ''), opt_type, strike, trade['Action'], date_str_close)
            
            is_sl_exit = ("SL Hit" in exit_reason)
            if is_sl_exit:
                self._sl_hit_count[close_key] = self._sl_hit_count.get(close_key, 0) + 1
                if self._sl_hit_count[close_key] < 2:
                    # First time SL hit: DO NOT block re-entry — allow taking entry again with new HH/LL SL
                    print(f"🔄 [FWD TEST] First SL Hit for {trade['Action']} {opt_type} {strike}{exp_lbl} — Re-entry ALLOWED when price confirms entry again with new HH/LL SL")
                else:
                    # Re-entry also hit SL: now block for remainder of session
                    if exp_val:
                        self._closed_keys_today.add(close_key)
                    self._closed_keys_today.add((opt_type, strike, trade['Action'], date_str_close))
            else:
                if exp_val:
                    self._closed_keys_today.add(close_key)
                self._closed_keys_today.add((opt_type, strike, trade['Action'], date_str_close))
            self._closed_today.append(dict(trade))
            self._update_trade_in_file(trade)
            self._save_snapshot()
            icon = '✅' if exit_pnl >= 0 else '🛑'
            hl_label = f"High:{trade['Trade_High']:.2f}" if trade['Action'] == 'BUY' else f"Low:{trade['Trade_Low']:.2f}"
            print(f"{icon} [FWD TEST] CLOSED {trade['Action']} {opt_type} {strike}{exp_lbl} "
                  f"| {exit_reason} | PnL:{exit_pnl:+.2f} pts | {hl_label} (Max:{trade['Max_Pts']:+.2f} pts)")
            return trade
        return None

    def _today_closed(self):
        """Today's closed trades: the trade log plus this session's closes (by Trade_ID)."""
        today = datetime.now().strftime('%Y-%m-%d')
        out = {}
        try:
            with open(self.filepath, 'r', newline='') as f:
                for r in csv.DictReader(f):
                    if r.get('Date') == today and r.get('Status', '').strip() == 'CLOSED':
                        r = dict(r)
                        r['Expiry'] = trade_expiry(r.get('Trade_ID'))
                        out[r.get('Trade_ID')] = r
        except Exception:
            pass
        for c in self._closed_today:
            if c.get('Date', today) == today:
                out[c.get('Trade_ID')] = c
        return list(out.values())

    def seed_state(self, st, symbol=""):
        """A new strike state picks up today's trade on that strike after a restart (same strike,
        side, expiry): an open trade carries on; otherwise the last closed one is shown with its
        entry, SL and exit, and the strike waits for a fresh sweep, as before the restart."""
        side = "BUY" if st.mode == "BUY" else "SELL"
        same = lambda t: (t.get('Action') == side and t.get('Type') == st.opt_type
                          and int(_safe_float(t.get('Strike', 0))) == int(st.strike)
                          and (t.get('Expiry', '') or '') == (st.expiry or '')
                          and (not symbol or not t.get('Symbol') or t.get('Symbol') == symbol))
        if not self._restored_ids:
            if self._seed_closed_cache is None:
                self._seed_closed_cache = self._today_closed()
            done = sorted((c for c in self._seed_closed_cache if same(c)),
                          key=lambda c: (str(c.get('Exit_Time', '')), str(c.get('Entry_Time', ''))))
            if done:
                hits = sum(1 for c in done if 'SL Hit' in str(c.get('Exit_Reason', '')))
                st.seed_from_closed(done[-1], sl_hits=hits, entries=len(done))
            return
        side = "BUY" if st.mode == "BUY" else "SELL"
        for t in self.get_active_trades():
            if (t.get('Trade_ID') in self._restored_ids and t.get('Action') == side
                    and t.get('Type') == st.opt_type and int(_safe_float(t.get('Strike', 0))) == int(st.strike)
                    and (t.get('Expiry', '') or '') == (st.expiry or '')
                    and (not symbol or not t.get('Symbol') or t.get('Symbol') == symbol)):
                hits = sum(1 for c in self._closed_today
                           if c.get('Type') == st.opt_type and int(_safe_float(c.get('Strike', 0))) == int(st.strike)
                           and c.get('Action') == side and (c.get('Expiry', '') or '') == (st.expiry or '')
                           and 'SL Hit' in str(c.get('Exit_Reason', '')))
                st.seed_from_trade(t, sl_hits=hits)
                return
        ids, self._restored_ids = self._restored_ids, set()
        try:
            self.seed_state(st, symbol)            # no open trade on this strike: show its last close
        finally:
            self._restored_ids = ids

    def get_active_trades(self):
        """Return list of all currently active trades with live PnL (de-duplicated)."""
        seen, out = set(), []
        for t in self.active_trades.values():
            if id(t) not in seen:
                seen.add(id(t))
                out.append(t)
        return out

    def eod_square_off(self, reason: str = "EOD 3PM Square-off @ CMP") -> list:
        """
        Close every open trade at its current market price (CMP) — used at 3:00 PM
        for intraday trades whose SL/Target was not hit. Routes exit orders to
        LiveOrderManager unless paper_only. Returns list of closed trade dicts.
        """
        closed = []
        for t in self.get_active_trades():
            cmp_px = _safe_float(t.get('Current_Price', 0)) or _safe_float(t.get('Entry_Price', 0))
            opt_type, strike, exp = t.get('Type'), t.get('Strike'), t.get('Expiry', '')
            sym = t.get('Symbol', '')
            res = self.manual_close(opt_type, strike, cmp_px, reason=reason, expiry=exp, symbol=sym)
            if res:
                closed.append(res)
        if closed:
            net = sum(_safe_float(c.get('PnL_Pts', 0)) for c in closed)
            print(f"⏰ [EOD 3PM] Squared off {len(closed)} open trade(s) @ CMP | Net: {net:+.2f} pts")
        return closed

    def get_closed_trades_from_file(self):
        """Read all CLOSED trades from CSV (full audit log — NOT used for dashboard)."""
        closed = []
        try:
            if not os.path.exists(self.filepath):
                return closed
            with open(self.filepath, 'r', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('Status', '').strip() == 'CLOSED':
                        closed.append(row)
        except Exception as e:
            print(f"Read closed trades error: {e}")
        return closed

    def backfill_abandoned(self, fetch_bars, now=None):
        """Rows marked ABANDONED (the app was restarted while they were open) are finished
        from that day's 1-minute candles, so their SL / target / 3 PM exit reaches the sheet.
        Today's rows wait until after 15:00. fetch_bars(symbol, expiry, strike, opt_type, day).
        Returns the trades that were closed."""
        now = now or datetime.now()
        today = now.strftime('%Y-%m-%d')
        try:
            with open(self.filepath, 'r', newline='') as f:
                rows = [r for r in csv.DictReader(f) if r.get('Status', '').strip() == 'ABANDONED']
        except Exception:
            return []
        done = []
        for r in rows:
            day = r.get('Date', '')
            if day > today or (day == today and now.time() < dt_time(15, 1)):
                continue
            exp = trade_expiry(r.get('Trade_ID'))
            if not exp:
                continue
            t = dict(r)
            for k in ('Entry_Price', 'SL', 'Target_1', 'Target_2', 'Target_3', 'Target_4', 'Target_5'):
                t[k] = _safe_float(r.get(k, 0))
            t['Strike'] = int(_safe_float(r.get('Strike', 0)))
            t.update({'Trade_High': t['Entry_Price'], 'Trade_Low': t['Entry_Price'], 'Target_Hit': 0, 'Initial_SL': t['SL']})
            try:
                bars = fetch_bars(t.get('Symbol', ''), exp, t['Strike'], t.get('Type', ''), day)
            except Exception as e:
                print(f"[FWD TEST] 1-min data for {t.get('Type')} {t['Strike']} {exp} unavailable: {e}")
                continue
            res = replay_trade_bars(t, bars or [])
            if not res:
                continue
            res['Exit_Reason'] += RESTART_FILL_NOTE
            self._update_trade_in_file(res)
            done.append(res)
            print(f"♻️  [FWD TEST] {res['Action']} {res['Type']} {res['Strike']} {exp} ({day} {res['Entry_Time']}) "
                  f"finished from 1-min data: {res['Exit_Reason']} @ {res['Exit_Price']} | PnL:{res['PnL_Pts']:+.2f}")
        return done

    def get_closed_today(self):
        """Return trades closed in the current session only (no CSV bleed-through)."""
        return list(self._closed_today)

    def manual_close(self, opt_type: str, strike: int, ltp: float,
                     reason: str = "Manual Close", expiry: str = "", symbol: str = "") -> dict:
        """
        Forcibly close an active tracked trade at the given LTP with a custom reason.
        Useful for:
          - End-of-day square-off ("Market EOD")
          - Manual exit detected from Angel One position API ("Market Closed")
          - Trader-initiated close from GUI ("Manual Close")
        Returns the closed trade dict, or None if no active trade found.
        """
        trade_key = None
        if symbol and expiry and (symbol, opt_type, strike, expiry) in self.active_trades:
            trade_key = (symbol, opt_type, strike, expiry)
        elif symbol and (symbol, opt_type, strike) in self.active_trades:
            trade_key = (symbol, opt_type, strike)
        elif expiry and (opt_type, strike, expiry) in self.active_trades:
            trade_key = (opt_type, strike, expiry)
        elif (opt_type, strike) in self.active_trades:
            trade_key = (opt_type, strike)

        if not trade_key:
            return None

        trade = self.active_trades[trade_key]
        time_str = datetime.now().strftime('%H:%M:%S')
        entry    = trade['Entry_Price']

        # Update running high/low
        if ltp > trade.get('Trade_High', entry):
            trade['Trade_High'] = round(ltp, 2)
        if trade.get('Trade_Low', entry) == 0 or ltp < trade.get('Trade_Low', entry):
            trade['Trade_Low'] = round(ltp, 2)

        if trade['Action'] == 'BUY':
            exit_pnl = round(ltp - entry, 2)
            trade['Max_Pts'] = round(max(0.0, trade['Trade_High'] - entry), 2)
        else:
            exit_pnl = round(entry - ltp, 2)
            trade['Max_Pts'] = round(max(0.0, entry - trade['Trade_Low']), 2)

        trade['Exit_Price']  = round(ltp, 2)
        trade['Exit_Time']   = time_str
        trade['Exit_Reason'] = reason
        trade['PnL_Pts']     = exit_pnl
        trade['Live_PnL']    = exit_pnl
        trade['Status']      = 'CLOSED'

        # Broker exit (no-op if the broker position is already closed or was never opened)
        if not self.paper_only:
            try:
                live_order_manager.exit_order(trade, reason, ltp)
            except Exception as oe:
                print(f"[ORDER] exit_order error: {oe}")

        for k in list(self.active_trades.keys()):
            if self.active_trades[k] is trade or self.active_trades[k].get('Trade_ID') == trade.get('Trade_ID'):
                self.active_trades.pop(k, None)

        date_str_mc = datetime.now().strftime('%Y-%m-%d')
        exp_val = trade.get('Expiry', '')
        if exp_val:
            self._closed_keys_today.add((trade.get('Symbol', ''), opt_type, strike, exp_val, trade['Action'], date_str_mc))
        self._closed_keys_today.add((trade.get('Symbol', ''), opt_type, strike, trade['Action'], date_str_mc))
        self._closed_today.append(dict(trade))
        self._update_trade_in_file(trade)
        self._save_snapshot()

        icon = '✅' if exit_pnl >= 0 else '🛑'
        exp_lbl = f" | Exp:{exp_val}" if exp_val else ""
        print(f"{icon} [FWD TEST] MANUAL CLOSE {trade['Action']} {opt_type} {strike}{exp_lbl} "
              f"@ {ltp:.2f} | {reason} | PnL:{exit_pnl:+.2f} pts")
        return trade

    def _append_row(self, trade):
        try:
            with open(self.filepath, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade['Trade_ID'], trade['Date'], trade['Entry_Time'],
                    trade.get('Timeframe', '15M Intraday'),
                    trade['Symbol'], trade['Type'], trade['Strike'],
                    trade['Action'], trade['Entry_Price'],
                    trade['SL'], trade['Target_1'], trade['Target_2'],
                    trade.get('Target_3', 0), trade.get('Target_4', 0), trade.get('Target_5', 0),
                    trade.get('Trade_High', trade['Entry_Price']),
                    trade.get('Trade_Low', trade['Entry_Price']),
                    trade.get('Max_Pts', 0.0),
                    trade.get('Exit_Price', ''), trade.get('Exit_Time', ''),
                    trade.get('Exit_Reason', 'ACTIVE'),
                    trade.get('PnL_Pts', 0.0), trade.get('Status', 'ACTIVE')
                ])
        except Exception as e:
            print(f"Forward test append error: {e}")

    def _update_trade_in_file(self, updated_trade):
        try:
            rows = []
            if os.path.exists(self.filepath):
                with open(self.filepath, 'r', newline='') as f:
                    reader = csv.reader(f)
                    rows = list(reader)

            for i, row in enumerate(rows):
                if row and row[0] == updated_trade['Trade_ID']:
                    tf = updated_trade.get('Timeframe', '15M Intraday')
                    rows[i] = [
                        updated_trade['Trade_ID'], updated_trade['Date'],
                        updated_trade['Entry_Time'], tf,
                        updated_trade['Symbol'],
                        updated_trade['Type'], updated_trade['Strike'],
                        updated_trade['Action'], updated_trade['Entry_Price'],
                        updated_trade.get('Initial_SL') or updated_trade['SL'], updated_trade['Target_1'],
                        updated_trade['Target_2'],
                        updated_trade.get('Target_3', 0),
                        updated_trade.get('Target_4', 0),
                        updated_trade.get('Target_5', 0),
                        updated_trade.get('Trade_High', updated_trade['Entry_Price']),
                        updated_trade.get('Trade_Low', updated_trade['Entry_Price']),
                        updated_trade.get('Max_Pts', 0.0),
                        updated_trade.get('Exit_Price', ''),
                        updated_trade.get('Exit_Time', ''),
                        updated_trade.get('Exit_Reason', 'ACTIVE'),
                        updated_trade.get('PnL_Pts', 0.0),
                        updated_trade.get('Status', 'CLOSED')
                    ]
                    break

            with open(self.filepath, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerows(rows)
        except Exception as e:
            print(f"Forward test update error: {e}")



def get_tv_multi_timeframe_pivots(symbol: str = "NIFTY", force_refresh: bool = False) -> dict:
    """
    Calculates Fibonacci Pivot levels matching TradingView's built-in
    'Pivots Fibonacci Auto' indicator exactly:

    In TradingView 'Pivots Auto' resolution mapping:
      • 5-Min chart  → Auto uses DAILY pivot (previous completed trading day OHLC)
      • 1-Hour chart → Auto uses WEEKLY pivot (previous completed trading week OHLC)
      • Daily (1D)   → Auto uses MONTHLY pivot (previous completed calendar month OHLC)
      • Weekly (1W)  → Auto uses YEARLY pivot (previous completed calendar year OHLC)

    Fibonacci Pivot Formula:
      P  = (High + Low + Close) / 3
      R1 = P + 0.382 * (High - Low)    S1 = P - 0.382 * (High - Low)
      R2 = P + 0.618 * (High - Low)    S2 = P - 0.618 * (High - Low)
      R3 = P + 1.000 * (High - Low)    S3 = P - 1.000 * (High - Low)
    """
    cache_file = os.path.join(BASE_DIR, "tv_pivot_cache.json")
    _now = datetime.now()
    today_str = _now.strftime("%Y-%m-%d")
    # Levels for a trading day are built ONLY from sessions that closed before it.
    # Before 15:30 on a weekday today's (live) bars are excluded, so the levels are
    # identical whenever the app is started that day and never move during the session.
    # After 15:30 (or on weekends) today's completed session is included → next-day levels.
    _after_close = _now.weekday() >= 5 or _now.time() >= dt_time(15, 30)
    _basis = "post_close" if _after_close else "pre_close"
    _in_session = (not _after_close) and _now.time() >= dt_time(9, 15)

    # Fast path: cached levels are reused only if they were built today on the same basis
    cached_pivots = None
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as cf:
                all_cache = json.load(cf)
                s_c = all_cache.get(symbol, {})
                if s_c and "15-Min" in s_c and "1-Hour" in s_c:
                    cached_pivots = {k: s_c[k] for k in ["Weekly", "Daily", "1-Hour", "15-Min"] if k in s_c}
                    fresh = (s_c.get("date") == today_str and s_c.get("basis") == _basis)
                    # During market hours locked levels are never recalculated, even on force_refresh
                    if fresh and (_in_session or not force_refresh):
                        return cached_pivots
        except Exception:
            pass

    y_sym = {
        'NIFTY':      '%5ENSEI',
        'BANKNIFTY':  '%5ENSEBANK',
        'FINNIFTY':   'NIFTY_FIN_SERVICE.NS',
        'MIDCPNIFTY': 'NIFTY_MID_SELECT.NS',
        'SENSEX':     '%5EBSESN',
        'BANKEX':     '%5EBSEBK',
        'CRUDEOIL':   'CL=F',
        'GOLD':       'GC=F',
        'SILVER':     'SI=F',
        'NATURALGAS': 'NG=F',
    }.get(symbol, f"{symbol}.NS")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    })
    try:
        session.get('https://fc.yahoo.com', timeout=5)
    except Exception:
        pass

    def _calc_fib(h, l, c):
        p = (h + l + c) / 3.0
        rng = h - l
        if rng <= 0: rng = max(c * 0.001, 0.01)
        s1 = max(p - 0.382 * rng, 0.05)
        s2 = max(p - 0.618 * rng, 0.05)
        s3 = max(p - 1.000 * rng, 0.05)
        return {
            "H": round(h, 2), "L": round(l, 2), "C": round(c, 2),
            "P":  round(p, 2),
            "R1": round(p + 0.382 * rng, 2),
            "S1": round(s1, 2),
            "R2": round(p + 0.618 * rng, 2),
            "S2": round(s2, 2),
            "R3": round(p + 1.000 * rng, 2),
            "S3": round(s3, 2),
        }

    def _completed_bars(chart):
        """Aligned H/L/C lists of bars from completed sessions only (see basis rule above)."""
        ts = chart.get('timestamp', []) or []
        q  = chart['indicators']['quote'][0]
        hs, ls, cs = q.get('high', []), q.get('low', []), q.get('close', [])
        H, L, C = [], [], []
        for i, t in enumerate(ts):
            if i >= len(hs) or i >= len(ls) or i >= len(cs):
                break
            h, l, c = hs[i], ls[i], cs[i]
            if not t or h is None or l is None or c is None or h <= 0 or l <= 0 or c <= 0:
                continue
            if not _after_close and datetime.fromtimestamp(t).date() >= _now.date():
                continue   # bar belongs to today's live session — excluded
            H.append(h); L.append(l); C.append(c)
        return H, L, C

    piv_15m = {}
    piv_1h  = {}
    piv_daily = {}
    piv_weekly = {}

    for base_url in [
        'https://query2.finance.yahoo.com/v8/finance/chart',
        'https://query1.finance.yahoo.com/v8/finance/chart',
    ]:
        # 1. 15-Minute Data -> strictly 15-minute candles
        try:
            url15 = f'{base_url}/{y_sym}?range=5d&interval=15m&includePrePost=false'
            r15 = session.get(url15, timeout=8)
            if r15.ok:
                d15 = r15.json()['chart']['result'][0]
                h15, l15, c15 = _completed_bars(d15)
                if h15 and l15 and c15:
                    n15 = min(25, len(h15))
                    piv_15m = _calc_fib(max(h15[-n15:]), min(l15[-n15:]), c15[-1])
        except Exception:
            pass

        # 2. 1-Hour Data -> strictly 1-hour candles
        try:
            url60 = f'{base_url}/{y_sym}?range=1mo&interval=60m&includePrePost=false'
            r60 = session.get(url60, timeout=8)
            if r60.ok:
                d60 = r60.json()['chart']['result'][0]
                h60, l60, c60 = _completed_bars(d60)
                if h60 and l60 and c60:
                    n60 = min(35, len(h60))
                    piv_1h = _calc_fib(max(h60[-n60:]), min(l60[-n60:]), c60[-1])
        except Exception:
            pass

        # 3. Daily & Weekly Data -> daily candles
        try:
            urld = f'{base_url}/{y_sym}?range=2y&interval=1d&includePrePost=false'
            rd = session.get(urld, timeout=10)
            if rd.ok:
                dd = rd.json()['chart']['result'][0]
                qd = dd['indicators']['quote'][0]
                ts = dd.get('timestamp', [])
                hd = qd.get('high', [])
                ld = qd.get('low', [])
                cd = qd.get('close', [])
                daily_c = []
                for i, t in enumerate(ts):
                    if t and i < len(hd) and hd[i] and ld[i] and cd[i]:
                        daily_c.append({"date": datetime.fromtimestamp(t).date(), "high": hd[i], "low": ld[i], "close": cd[i]})
                if daily_c:
                    today_date = datetime.now().date()
                    comp_d = [x for x in daily_c if x["date"] < today_date or _after_close]
                    if comp_d:
                        piv_daily = _calc_fib(comp_d[-1]["high"], comp_d[-1]["low"], comp_d[-1]["close"])
                        # Weekly from completed week
                        by_wk = {}
                        for x in comp_d:
                            by_wk.setdefault(x['date'].isocalendar()[:2], []).append(x)
                        wk_keys = sorted(by_wk.keys())
                        cur_wk = today_date.isocalendar()[:2]
                        week_done = today_date.weekday() >= 5 or (today_date.weekday() == 4 and _after_close)
                        done_keys = [k for k in wk_keys if k < cur_wk or (k == cur_wk and week_done)]
                        use_w = done_keys[-1] if done_keys else wk_keys[-1]
                        wkc = by_wk[use_w]
                        piv_weekly = _calc_fib(max(x["high"] for x in wkc), min(x["low"] for x in wkc), wkc[-1]["close"])
        except Exception:
            pass

        if piv_15m and piv_1h:
            break

    # If fetch failed or partial, fallback to cached pivots for this symbol
    if not piv_15m and cached_pivots and "15-Min" in cached_pivots:
        piv_15m = cached_pivots["15-Min"]
    if not piv_1h and cached_pivots and "1-Hour" in cached_pivots:
        piv_1h = cached_pivots["1-Hour"]
    if not piv_daily and cached_pivots and "Daily" in cached_pivots:
        piv_daily = cached_pivots["Daily"]
    if not piv_weekly and cached_pivots and "Weekly" in cached_pivots:
        piv_weekly = cached_pivots["Weekly"]

    if not piv_15m or not piv_1h:
        if cached_pivots:
            return cached_pivots
        return {}

    # Only 15M and 1H drive trades; Weekly/Daily are kept for context display
    results = {
        "Weekly": piv_weekly or piv_1h,
        "Daily":  piv_daily or piv_15m,
        "1-Hour": piv_1h,
        "15-Min": piv_15m,
    }

    # Save to persistent cache
    try:
        all_cache = {}
        if os.path.exists(cache_file):
            with open(cache_file, "r") as cf:
                all_cache = json.load(cf)
        all_cache[symbol] = {
            "date": today_str,
            "basis": _basis,
            "Weekly": results["Weekly"],
            "Daily": results["Daily"],
            "1-Hour": results["1-Hour"],
            "15-Min": results["15-Min"],
        }
        with open(cache_file, "w") as cf:
            json.dump(all_cache, cf, indent=2)
    except Exception:
        pass

    return results


def calculate_symbol_option_pipeline(symbol: str, exchange: str = None) -> dict:
    """
    Executes the complete end-to-end pipeline for any symbol:
    Symbol -> Market Data -> Correct Timeframe -> Pivot/Level Calculation ->
    Support/Resistance -> Filter -> Strike Selection -> Option Symbol Mapping ->
    Option Price -> Trade Calculation -> Signal/Status.
    """
    sym = (symbol or "NIFTY").strip().upper()
    exch = exchange or get_symbol_exchange(sym)
    step = get_symbol_step(sym, exch)
    lot = get_symbol_lot_size(sym, exch)

    # 1. MTF Pivots from Yahoo Finance or Cache (strictly separated per timeframe)
    mtf = get_tv_multi_timeframe_pivots(sym)
    piv_15m = mtf.get("15-Min", {})
    piv_1h  = mtf.get("1-Hour", {})

    # Spot price
    spot = feed.spot_price.get(sym, 0.0)
    if spot <= 0:
        spot = piv_15m.get("C", 0.0) or piv_1h.get("C", 0.0) or piv_15m.get("P", 0.0) or piv_1h.get("P", 0.0)

    # 2. Strict timeframe strike selection (15M and 1H completely separated)
    f15 = select_intraday_15m_focus_strikes(mtf, step)
    f1h = select_1h_structure_strikes(mtf, step)

    # 3. Active Expiry for this symbol
    c_exp = ""
    df = Session.scrip_df
    if df is not None:
        try:
            m = df[(df["name"] == sym) & (df["strike"].astype(float) > 0)]
            if not m.empty:
                exp_list = sorted(
                    m["expiry"].dropna().unique(),
                    key=lambda e: datetime.strptime(e, "%d%b%Y") if e else datetime.max
                )
                today_d = datetime.now().date()
                fut_exps = [e for e in exp_list if datetime.strptime(e, "%d%b%Y").date() >= today_d]
                c_exp = fut_exps[0] if fut_exps else (exp_list[0] if exp_list else "")
        except Exception:
            pass

    # 4. Option Contract Mapping & Price Retrieval
    def _map_option(opt_type: str, strike: int):
        opt_sym = f"{sym}{c_exp}{strike}{opt_type}" if c_exp else f"{sym} {strike} {opt_type}"
        token = ""
        lot_sz = lot
        if df is not None and c_exp:
            try:
                sub = df[(df["name"] == sym) & (df["expiry"] == c_exp) & (df["symbol"].str.endswith(opt_type))]
                sub_stk = sub[(sub["strike"].astype(float) / 100.0).round().astype(int) == strike]
                if not sub_stk.empty:
                    row0 = sub_stk.iloc[0]
                    token = str(row0["token"])
                    opt_sym = str(row0["symbol"])
                    lot_sz = int(row0.get("lotsize", lot))
            except Exception:
                pass

        # Option Price
        ltp = 0.0
        if token and token in feed.live_data:
            ltp = _safe_float(feed.live_data[token].get("ltp", 0))
        if ltp <= 0:
            sp = feed.strike_pivots.get((opt_type, strike, c_exp)) or feed.strike_pivots.get((opt_type, strike)) or feed.strike_pivots.get(token)
            if sp:
                ltp = _safe_float(sp.get("prev_c", 0))
        if ltp <= 0:
            # Theoretical option price
            intrinsic = max(spot - strike, 0.0) if opt_type == "CE" else max(strike - spot, 0.0)
            time_val = max(step * 0.40, 4.0)
            ltp = round(intrinsic + time_val, 2)

        # Option Contract Fibonacci Pivots (TradingView Aligned)
        sp = feed.strike_pivots.get(token) or feed.strike_pivots.get((opt_type, strike, c_exp)) or {}

        return opt_sym, token, lot_sz, ltp, sp

    # 5. Build Setups / Other Symbols Rows
    # Required columns: Symbol | Timeframe | Level | Support | Resistance | Selected Strike | CE/PE | Option Price | Signal | Status
    rows = []

    # 1-Hour Timeframe Setups
    # 1H Support -> PE strikes
    for stk in f1h.get("pe_strikes", [])[:2]:
        opt_sym, tok, lsz, opt_px, sp = _map_option("PE", stk)
        s2_o = _safe_float(sp.get("S2", 0))
        s1_o = _safe_float(sp.get("S1", 0))
        p_o  = _safe_float(sp.get("P", 0))
        s3_o = _safe_float(sp.get("S3", 0))
        r1_o = _safe_float(sp.get("R1", 0))
        r2_o = _safe_float(sp.get("R2", 0))
        opt_entry = s2_o if s2_o > 0.05 else (s1_o if s1_o > 0.05 else round(opt_px * 0.95, 2))
        opt_target = p_o if p_o > 0 else round(opt_entry * 1.25, 2)
        opt_sl = s3_o if s3_o > 0.05 else round(opt_entry * 0.90, 2)
        sig = f"Plan BUY @ S2:{opt_entry:.2f} | T:{opt_target:.2f} | SL:{opt_sl:.2f}"
        rows.append({
            "symbol": sym,
            "timeframe": "1H",
            "level": f"P:{p_o:.1f} (S2:{s2_o:.1f} R2:{r2_o:.1f})",
            "support": f"S1:{s1_o:.1f} S2:{s2_o:.1f}",
            "resistance": f"R1:{r1_o:.1f} R2:{r2_o:.1f}",
            "selected_strike": stk,
            "type": "PE",
            "option_symbol": opt_sym,
            "option_price": f"{opt_px:.2f}",
            "current_ltp": f"{spot:.2f}",
            "spot": spot,
            "token": tok,
            "pnl": "+0.00",
            "signal": sig,
            "status": "Pending",
            "entry": f"{opt_entry:.2f}",
            "target": f"{opt_target:.2f}",
            "sl": f"{opt_sl:.2f}",
            "expiry": c_exp
        })

    # 1H Resistance -> CE strikes
    for stk in f1h.get("ce_strikes", [])[:2]:
        opt_sym, tok, lsz, opt_px, sp = _map_option("CE", stk)
        s2_o = _safe_float(sp.get("S2", 0))
        s1_o = _safe_float(sp.get("S1", 0))
        p_o  = _safe_float(sp.get("P", 0))
        s3_o = _safe_float(sp.get("S3", 0))
        r1_o = _safe_float(sp.get("R1", 0))
        r2_o = _safe_float(sp.get("R2", 0))
        opt_entry = s2_o if s2_o > 0.05 else (s1_o if s1_o > 0.05 else round(opt_px * 0.95, 2))
        opt_target = p_o if p_o > 0 else round(opt_entry * 1.25, 2)
        opt_sl = s3_o if s3_o > 0.05 else round(opt_entry * 0.90, 2)
        sig = f"Plan BUY @ S2:{opt_entry:.2f} | T:{opt_target:.2f} | SL:{opt_sl:.2f}"
        rows.append({
            "symbol": sym,
            "timeframe": "1H",
            "level": f"P:{p_o:.1f} (S2:{s2_o:.1f} R2:{r2_o:.1f})",
            "support": f"S1:{s1_o:.1f} S2:{s2_o:.1f}",
            "resistance": f"R1:{r1_o:.1f} R2:{r2_o:.1f}",
            "selected_strike": stk,
            "type": "CE",
            "option_symbol": opt_sym,
            "option_price": f"{opt_px:.2f}",
            "current_ltp": f"{spot:.2f}",
            "spot": spot,
            "token": tok,
            "pnl": "+0.00",
            "signal": sig,
            "status": "Pending",
            "entry": f"{opt_entry:.2f}",
            "target": f"{opt_target:.2f}",
            "sl": f"{opt_sl:.2f}",
            "expiry": c_exp
        })

    # 15-Minute Timeframe Setups
    # 15M Support -> PE strikes
    for stk in f15.get("pe_strikes", [])[:2]:
        opt_sym, tok, lsz, opt_px, sp = _map_option("PE", stk)
        s2_o = _safe_float(sp.get("S2", 0))
        s1_o = _safe_float(sp.get("S1", 0))
        p_o  = _safe_float(sp.get("P", 0))
        s3_o = _safe_float(sp.get("S3", 0))
        r1_o = _safe_float(sp.get("R1", 0))
        r2_o = _safe_float(sp.get("R2", 0))
        opt_entry = s2_o if s2_o > 0.05 else (s1_o if s1_o > 0.05 else round(opt_px * 0.95, 2))
        opt_target = p_o if p_o > 0 else round(opt_entry * 1.25, 2)
        opt_sl = s3_o if s3_o > 0.05 else round(opt_entry * 0.90, 2)
        sig = f"Plan BUY @ S2:{opt_entry:.2f} | T:{opt_target:.2f} | SL:{opt_sl:.2f}"
        rows.append({
            "symbol": sym,
            "timeframe": "15M",
            "level": f"P:{p_o:.1f} (S2:{s2_o:.1f} R2:{r2_o:.1f})",
            "support": f"S1:{s1_o:.1f} S2:{s2_o:.1f}",
            "resistance": f"R1:{r1_o:.1f} R2:{r2_o:.1f}",
            "selected_strike": stk,
            "type": "PE",
            "option_symbol": opt_sym,
            "option_price": f"{opt_px:.2f}",
            "current_ltp": f"{spot:.2f}",
            "spot": spot,
            "token": tok,
            "pnl": "+0.00",
            "signal": sig,
            "status": "Pending",
            "entry": f"{opt_entry:.2f}",
            "target": f"{opt_target:.2f}",
            "sl": f"{opt_sl:.2f}",
            "expiry": c_exp
        })

    # 15M Resistance -> CE strikes
    for stk in f15.get("ce_strikes", [])[:2]:
        opt_sym, tok, lsz, opt_px, sp = _map_option("CE", stk)
        s2_o = _safe_float(sp.get("S2", 0))
        s1_o = _safe_float(sp.get("S1", 0))
        p_o  = _safe_float(sp.get("P", 0))
        s3_o = _safe_float(sp.get("S3", 0))
        r1_o = _safe_float(sp.get("R1", 0))
        r2_o = _safe_float(sp.get("R2", 0))
        opt_entry = s2_o if s2_o > 0.05 else (s1_o if s1_o > 0.05 else round(opt_px * 0.95, 2))
        opt_target = p_o if p_o > 0 else round(opt_entry * 1.25, 2)
        opt_sl = s3_o if s3_o > 0.05 else round(opt_entry * 0.90, 2)
        sig = f"Plan BUY @ S2:{opt_entry:.2f} | T:{opt_target:.2f} | SL:{opt_sl:.2f}"
        rows.append({
            "symbol": sym,
            "timeframe": "15M",
            "level": f"P:{p_o:.1f} (S2:{s2_o:.1f} R2:{r2_o:.1f})",
            "support": f"S1:{s1_o:.1f} S2:{s2_o:.1f}",
            "resistance": f"R1:{r1_o:.1f} R2:{r2_o:.1f}",
            "selected_strike": stk,
            "type": "CE",
            "option_symbol": opt_sym,
            "option_price": f"{opt_px:.2f}",
            "current_ltp": f"{spot:.2f}",
            "spot": spot,
            "token": tok,
            "pnl": "+0.00",
            "signal": sig,
            "status": "Pending",
            "entry": f"{opt_entry:.2f}",
            "target": f"{opt_target:.2f}",
            "sl": f"{opt_sl:.2f}",
            "expiry": c_exp
        })

    return {
        "success": True,
        "symbol": sym,
        "exchange": exch,
        "spot": spot,
        "step": step,
        "lot_size": lot,
        "expiry": c_exp,
        "mtf_pivots": mtf,
        "focus_15m": f15,
        "focus_1h": f1h,
        "rows": rows
    }

def check_option_entry(ltp: float, pivots: dict, opt_type: str = "CE",
                       ll: float = 0.0, state: str = "WAITING", level: str = "S2",
                       opt_low: float = 0.0) -> dict:
    """
    BUY entry — triggers for CALL (bullish) or PUT (reversal) after support reclaim (S1 or S2).
    SL is strictly the Liquidity Low (LL) of the strike.
    """
    if not pivots or ltp <= 0 or state != "CONFIRMED_BUY":
        return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}

    s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]
    p,  r1, r2  = pivots["P"],  pivots["R1"], pivots["R2"]
    r3          = pivots.get("R3", round(r2 + (r2 - r1), 2))

    # SL is strictly the Liquidity Low (LL) formed during sweep below support (MUST be < ltp)
    if ll > 0 and ll < ltp:
        effective_sl = round(ll, 2)
    elif opt_low > 0 and opt_low < ltp:
        effective_sl = round(opt_low, 2)
    elif s3 > 0 and s3 < ltp:
        effective_sl = round(s3, 2)
    else:
        effective_sl = round(ltp * 0.90, 2) if ltp > 0 else 0.05

    # BUY target ladder always goes UP through pivot levels:
    # Entry near S2 → T1:S1 → T2:P → T3:R1 → T4:R2 → T5:R3
    # Entry near S1 → T1:P  → T2:R1 → T3:R2 → T4:R3 → T5:R3*ext
    if level == "S1":
        if ltp < s1:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}
        zone = "S1 Reclaim"
        # Cascade upward from current LTP position
        levels_up = [l for l in [p, r1, r2, r3] if l > ltp]
        t1 = levels_up[0] if len(levels_up) > 0 else r1
        t2 = levels_up[1] if len(levels_up) > 1 else r2
        t3 = levels_up[2] if len(levels_up) > 2 else r3
        t4 = levels_up[3] if len(levels_up) > 3 else round(r3 * 1.02, 2)
        t5 = round(r3 * 1.05, 2)
    else:
        if ltp < s2:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}
        zone = "S2 Reclaim"
        # Full ladder: S1 → P → R1 → R2 → R3
        levels_up = [l for l in [s1, p, r1, r2, r3] if l > ltp]
        t1 = levels_up[0] if len(levels_up) > 0 else s1
        t2 = levels_up[1] if len(levels_up) > 1 else p
        t3 = levels_up[2] if len(levels_up) > 2 else r1
        t4 = levels_up[3] if len(levels_up) > 3 else r2
        t5 = levels_up[4] if len(levels_up) > 4 else r3

    label  = "CE" if opt_type == "CE" else "PE"
    detail = (f"BUY {label} @ {ltp:.2f} [{zone} | LL SL:{effective_sl:.2f}] "
              f"→ T1:{t1} T2:{t2} T3:{t3} T4:{t4} T5:{t5}")
    return {
        "entry": True, "zone": zone, "detail": detail,
        "T1": t1, "T2": t2, "T3": t3, "T4": t4, "T5": t5, "SL": effective_sl
    }


def check_option_support_touch(ltp: float, pivots: dict) -> dict:
    """Legacy compatibility wrapper — use check_option_entry for new code."""
    if not pivots or ltp <= 0:
        return {"touched": False, "level": "", "detail": ""}
    s1, s2 = pivots["S1"], pivots["S2"]
    tol1 = max(s1 * 0.02, 1.0)
    tol2 = max(s2 * 0.02, 1.0)
    if s2 <= ltp <= s1:
        return {"touched": True, "level": "IN_S1_S2", "detail": f"In Support ({s2}–{s1})"}
    if abs(ltp - s1) <= tol1:
        return {"touched": True, "level": "TOUCH_S1", "detail": f"Near S1 ({s1})"}
    if abs(ltp - s2) <= tol2:
        return {"touched": True, "level": "TOUCH_S2", "detail": f"Near S2 ({s2})"}
    return {"touched": False, "level": "", "detail": ""}


def check_option_sell_entry(ltp: float, pivots: dict, opt_type: str = "PE",
                            hh: float = 0.0, state: str = "WAITING", level: str = "R2",
                            opt_high: float = 0.0) -> dict:
    """
    SELL entry — triggers for PUT (bullish) or CALL (reversal) after resistance rejection (R1 or R2).
    SL is strictly the Liquidity High (HH) of the strike.
    """
    if not pivots or ltp <= 0 or state != "CONFIRMED_SELL":
        return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}

    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
    p,  s1, s2  = pivots["P"],  pivots["S1"], pivots["S2"]
    s3          = pivots.get("S3", max(round(s2 - (s1 - s2), 2), 0.05))

    # SL is strictly the sweep HH (MUST be > ltp); never R3 or a % of the price
    if hh > 0 and hh > ltp:
        effective_sl = round(hh, 2)
    elif opt_high > 0 and opt_high > ltp:
        effective_sl = round(opt_high, 2)
    else:
        return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}

    # SELL target ladder always goes DOWN through pivot levels:
    # Entry near R2 → T1:R1 → T2:P → T3:S1 → T4:S2 → T5:S3
    # Entry near R1 → T1:P  → T2:S1 → T3:S2 → T4:S3 → T5:S3*ext
    if level == "R1":
        if ltp > r1:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}
        zone = "R1 Rejection"
        # Cascade downward from current LTP position
        levels_dn = [l for l in [p, s1, s2, s3] if l < ltp]
        t1 = levels_dn[0] if len(levels_dn) > 0 else p
        t2 = levels_dn[1] if len(levels_dn) > 1 else s1
        t3 = levels_dn[2] if len(levels_dn) > 2 else s2
        t4 = levels_dn[3] if len(levels_dn) > 3 else s3
        t5 = max(round(s3 * 0.95, 2), 0.05)
    else:
        if ltp > r2:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "T4": 0, "T5": 0, "SL": 0}
        zone = "R2 Rejection"
        # Full ladder: R1 → P → S1 → S2 → S3
        levels_dn = [l for l in [r1, p, s1, s2, s3] if l < ltp]
        t1 = levels_dn[0] if len(levels_dn) > 0 else r1
        t2 = levels_dn[1] if len(levels_dn) > 1 else p
        t3 = levels_dn[2] if len(levels_dn) > 2 else s1
        t4 = levels_dn[3] if len(levels_dn) > 3 else s2
        t5 = levels_dn[4] if len(levels_dn) > 4 else s3

    label  = "PE" if opt_type == "PE" else "CE"
    detail = (f"SELL {label} @ {ltp:.2f} [{zone} | HH SL:{effective_sl:.2f}] "
              f"→ T1:{t1} T2:{t2} T3:{t3} T4:{t4} T5:{t5}")
    return {
        "entry": True, "zone": zone, "detail": detail,
        "T1": t1, "T2": t2, "T3": t3, "T4": t4, "T5": t5, "SL": effective_sl
    }
def _patched_on_close(self, wsapp, close_status_code=None, close_msg=None):
    if self.on_close: self.on_close(wsapp)
def _patched_on_error(self, wsapp, error=None):
    if self.on_error and error: self.on_error(wsapp, error)
SmartWebSocketV2._on_close = _patched_on_close
SmartWebSocketV2._on_error = _patched_on_error

# ═══════════════════════════════════════════════════════════
# GOOGLE SHEETS AUTH
# ═══════════════════════════════════════════════════════════
def get_gspread_client():
    """Authenticate with Google and return a gspread client.
    A service-account key (service_account.json, or GOOGLE_SERVICE_ACCOUNT_FILE in .env) never
    expires and is used when present. Otherwise the OAuth login in token.pickle is used; when
    Google refuses it (expired or revoked), the old file is kept as token.pickle.expired and the
    browser login opens again by itself, so the app never stops on a dead token."""
    creds      = None
    token_file = os.path.join(BASE_DIR, "token.pickle")
    creds_file = os.path.join(BASE_DIR, "credentials.json")
    sa_file    = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "") or "").strip() or os.path.join(BASE_DIR, "service_account.json")
    if not os.path.isabs(sa_file):
        sa_file = os.path.join(BASE_DIR, sa_file)

    if os.path.exists(sa_file):
        from google.oauth2.service_account import Credentials as _SACreds
        creds = _SACreds.from_service_account_file(sa_file, scopes=SCOPES)
        print(f"[GOOGLE] Using service account {getattr(creds, 'service_account_email', '')} (no expiry)")
    else:
        if os.path.exists(token_file):
            try:
                with open(token_file, "rb") as f:
                    creds = pickle.load(f)
            except Exception as e:
                print(f"[GOOGLE] Saved login unreadable ({type(e).__name__}); logging in again")
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"[GOOGLE] Saved login refused ({str(e)[:80]}); opening the Google login again")
                    try:
                        os.replace(token_file, token_file + ".expired")
                    except OSError:
                        pass
                    creds = None
            if not creds or not creds.valid:
                if not os.path.exists(creds_file):
                    raise FileNotFoundError(
                        "credentials.json not found!\n\n"
                        "Steps to create it:\n"
                        "1. Go to https://console.cloud.google.com/\n"
                        "2. Create/select a project\n"
                        "3. Enable 'Google Sheets API'\n"
                        "4. Go to APIs & Services -> Credentials\n"
                        "5. Create Credentials -> OAuth 2.0 Client ID -> Desktop App\n"
                        "6. Download JSON and save as:\n"
                        f"   {creds_file}"
                    )
                flow  = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
                # access_type=offline + consent: Google always returns a fresh refresh token
                creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
                print("[GOOGLE] New Google login saved")
            tmp = token_file + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(creds, f)
            os.replace(tmp, token_file)

    gc = gspread.authorize(creds)
    # A Sheets call without a timeout can hang the feed loop forever — cap every request
    try:
        gc.set_timeout(20)
    except Exception:
        try:
            gc.session.request = functools.partial(gc.session.request, timeout=20)
        except Exception:
            pass
    return gc

# ═══════════════════════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════════════════════
class Session:
    obj=auth_token=feed_token=scrip_df=None

    @classmethod
    def login(cls):
        totp=pyotp.TOTP(TOTP_SECRET).now()
        cls.obj=SmartConnect(api_key=API_KEY)
        s=cls.obj.generateSession(CLIENT_ID,MPIN,totp)
        if not s["status"]: raise Exception(s["message"])
        cls.auth_token=s["data"]["jwtToken"]
        cls.feed_token=cls.obj.getfeedToken()

    @classmethod
    def load_scrip(cls):
        """
        Load the Angel One ScripMaster (instrument list) with a local cache (scrip_cache.pkl).

        Why it is needed: the WebSocket subscribes by numeric token, and the token of each
        calculated strike (plus expiries and lot sizes) can only be looked up in this list.

        To keep startup light:
          • the cache is reused for the whole trading day (one download per day, refreshed
            after 08:30 when Angel publishes the new list) — crash/auto-restarts load it in <1s;
          • only what the app uses is kept: NFO/BFO/MCX contracts that have not expired and
            NSE/BSE rows (index and stock spot tokens). Currency/commodity-derivative segments
            and expired contracts are dropped.
          • if the download fails, the last cache is used instead of stopping the app.
        """
        import pickle as _pkl
        cache_path = os.path.join(BASE_DIR, "scrip_cache.pkl")
        now = datetime.now()
        refresh_after = now.replace(hour=8, minute=30, second=0, microsecond=0)

        cached = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    cached = _pkl.load(f)
            except Exception as e:
                print(f"   Cache load failed ({e}), fetching fresh...")
            if cached is not None:
                made = datetime.fromtimestamp(os.path.getmtime(cache_path))
                fresh = made.date() == now.date() and (made >= refresh_after or now < refresh_after)
                if fresh:
                    cls.scrip_df = cached
                    print(f"   ScripMaster loaded from today's cache ({len(cached)} instruments)")
                    return
                if (now - made).days < 4:
                    # A recent list has every contract the app trades: start with it at once and
                    # fetch today's list in the background (it is large; slow internet would hold
                    # the start for minutes).
                    cls.scrip_df = cached
                    print(f"   ScripMaster: using the {made:%d %b} list ({len(cached)} instruments); "
                          f"today's list downloads in the background")
                    threading.Thread(target=cls._download_scrip, args=(cache_path, now, cached), daemon=True).start()
                    return
        cls._download_scrip(cache_path, now, cached)

    @classmethod
    def _download_scrip(cls, cache_path, now, cached=None):
        import pickle as _pkl
        print("   Downloading ScripMaster from Angel One (once per day)...")
        try:
            resp = requests.get(SCRIP_URL, timeout=60, stream=True)
            resp.raise_for_status()
            df = pd.DataFrame(resp.json())
        except Exception as e:
            if cached is not None:
                print(f"   Download failed ({e}) — using the previous cache ({len(cached)} instruments)")
                cls.scrip_df = cached
                return
            raise

        try:
            exp = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")
            keep = df["exch_seg"].isin(["NFO", "BFO", "MCX", "NSE", "BSE"]) & (
                exp.isna() | (exp >= pd.Timestamp(now.date())))
            slim = df[keep].reset_index(drop=True)
            print(f"   ScripMaster: kept {len(slim)} of {len(df)} instruments (live F&O + spot)")
            df = slim
        except Exception as e:
            print(f"   ScripMaster slimming skipped: {e}")
        cls.scrip_df = df

        try:
            with open(cache_path + ".tmp", "wb") as f:
                _pkl.dump(df, f)
            os.replace(cache_path + ".tmp", cache_path)
            print(f"   ScripMaster cached → {cache_path}")
        except Exception as e:
            print(f"   Cache save failed: {e}")

# ═══════════════════════════════════════════════════════════
# OPTION STRIKE PIVOT DATA (official daily candles)
# ═══════════════════════════════════════════════════════════
STRIKE_PIVOT_CACHE = os.path.join(BASE_DIR, "strike_pivot_cache.json")             # levels TradingView shows now
STRIKE_PIVOT_NEXT_CACHE = os.path.join(BASE_DIR, "strike_pivot_cache_next.json")   # next session, built after the close

_ANGEL_AUTH_ERRORS = ("AG8001", "AG8002", "AG8003", "AG8004", "AB1010", "AB1011",
                      "INVALID TOKEN", "INVALID API KEY", "TOKEN EXPIRED")
_ANGEL_RELOGIN_AT = 0.0      # when the app last logged in again because Angel refused the session
_ANGEL_RATE_ERRORS = ("AB1004", "AB1019", "EXCEEDING ACCESS RATE", "TOO MANY REQUESTS")


LEVELS_ROLL_TIME = dt_time(15, 35)   # after this the app shows the next session's levels


def pivot_session_date(now: datetime = None) -> date:
    """The session the strike levels are for. During the trading day (until 15:35 on a weekday)
    that is today; after the close, at the weekend or on a holiday it is the next weekday, so
    every strike already shows the levels built from the day that just closed."""
    now = now or datetime.now()
    d = now.date()
    if d.weekday() < 5 and now.time() < LEVELS_ROLL_TIME:
        return d
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def expected_candle_day(mode: str = "session", now: datetime = None) -> date:
    """The trading day whose candle the levels must come from: the weekday before the session
    (pivot_session_date) for "session"; today (last weekday at the weekend) for "next"."""
    now = now or datetime.now()
    if mode == "next":
        d = now.date()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    return prev_weekday(pivot_session_date(now))


def pick_pivot_candle(rows, mode: str = "session", now: datetime = None, day: date = None):
    """From daily candles [(YYYY-MM-DD, o, h, l, c), ...] pick the candle of exactly the expected
    trading day (expected_candle_day). An older candle is never used in its place: when the
    source has not published that day yet the result is None and the caller rebuilds the day
    from 1-minute candles."""
    now = now or datetime.now()
    if mode == "next" and now.weekday() < 5 and now.time() < dt_time(15, 30):
        return None
    want = (day or expected_candle_day(mode, now)).isoformat()
    rows = [r for r in rows if r[0] == want and r[2] > 0 and r[3] > 0 and r[4] > 0]
    return rows[-1] if rows else None


def angel_daily_candles(exch: str, token: str, days: int = 20, now: datetime = None) -> list:
    """Daily candles [(YYYY-MM-DD, open, high, low, close), ...] of one contract from Angel One's
    historical API. When Angel refuses the session (it goes stale overnight and answers
    AG8004 'Invalid API Key') it logs in again once; on the rate limit it waits and retries.
    Raises with Angel's message when no data comes back."""
    now = now or datetime.now()
    params = {"exchange": exch, "symboltoken": str(token), "interval": "ONE_DAY",
              "fromdate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
              "todate": now.strftime("%Y-%m-%d %H:%M")}
    relogged, last = False, ""
    for attempt in range(5):
        try:
            if Session.obj is None:
                Session.login()
            r = Session.obj.getCandleData(dict(params))
        except Exception as e:
            r = {"status": False, "message": str(e)}
        if isinstance(r, dict) and r.get("status") and isinstance(r.get("data"), list):
            out = []
            for x in r["data"]:
                try:
                    out.append((str(x[0])[:10], float(x[1]), float(x[2]), float(x[3]), float(x[4])))
                except Exception:
                    pass
            return out
        if isinstance(r, dict):
            last = f"{r.get('errorcode') or r.get('errorCode') or ''} {r.get('message', '')}".strip()
        else:
            last = str(r)[:120]
        up = last.upper()
        if any(k in up for k in _ANGEL_AUTH_ERRORS):
            global _ANGEL_RELOGIN_AT
            # A fresh login a moment ago did not help: Angel is refusing the key itself, so
            # don't log in again for every strike (that only floods Angel).
            if relogged or time.time() - _ANGEL_RELOGIN_AT < 300:
                last += " (still refused after a fresh login: check the API key / static IP in the SmartAPI dashboard)"
                break
            relogged = True
            _ANGEL_RELOGIN_AT = time.time()
            try:
                Session.login()
                print("[STRIKE MAPPER] Angel session refreshed (it had expired)")
            except Exception as le:
                last += f" (login again failed: {le})"
                break
            continue
        if any(k in up for k in _ANGEL_RATE_ERRORS) or attempt < 2:
            time.sleep(1.0 + attempt)
            continue
        break
    raise RuntimeError(last or "no candle data from Angel One")


_DHAN_DATA = {"ad": None, "fail_at": 0.0, "why": ""}


def dhan_daily_candles(exch: str, token: str, instrument: str = "OPTIDX", now: datetime = None) -> list:
    """Daily candles of one contract from Dhan's historical API (same official exchange OHLC).
    Dhan's F&O security ID is the exchange token, the same number Angel's instrument list has.
    Logs in with the Dhan keys in .env (PIN + TOTP makes a fresh token); a failed login is not
    retried for 5 minutes."""
    from brokers.dhan import _SEG
    st = _DHAN_DATA
    _dhan_data_login()
    try:
        return st["ad"].daily_candles(token, _SEG.get(exch, "NSE_FNO"), instrument, now=now)
    except Exception as e:
        if any(k in str(e).upper() for k in ("DH-901", "DH-902", " 401", "INVALID", "EXPIRED")):
            st["ad"] = None          # token no longer valid: log in again on the next call
        raise


def _dhan_data_login():
    """The shared Dhan market-data session (logs in with the Dhan keys in .env)."""
    from brokers.dhan import DhanAdapter
    st = _DHAN_DATA
    if st["ad"] is None:
        if time.time() - st["fail_at"] < 300:
            raise RuntimeError(st["why"])
        creds = {k: (os.getenv(k, "") or "").strip() for k in DhanAdapter.env_keys}
        if not creds.get("DHAN_CLIENT_ID"):
            st["fail_at"], st["why"] = time.time(), "no Dhan keys in .env"
            raise RuntimeError(st["why"])
        ad = DhanAdapter(creds, log=print, cache_dir=BASE_DIR)
        try:
            ad.login()
        except Exception as e:
            st["fail_at"], st["why"] = time.time(), f"Dhan login failed: {e}"
            raise RuntimeError(st["why"])
        st["ad"] = ad
    return st["ad"]


def dhan_minute_bars(symbol, expiry, strike, opt_type, day):
    """1-minute candles of one option contract for one day, from Dhan."""
    ad = _dhan_data_login()
    exch = "BFO" if str(symbol).upper() in ("SENSEX", "BANKEX") else "NFO"
    inst = ad.resolve({"symbol": symbol, "expiry": expiry, "strike": strike, "opt_type": opt_type,
                       "exchange": exch, "tsym": f"{symbol} {expiry} {strike} {opt_type}"})
    try:
        return ad.intraday_bars(inst["inst_id"], day, inst["segment"])
    except Exception as e:
        if any(k in str(e).upper() for k in ("DH-901", "DH-902", " 401", "INVALID", "EXPIRED")):
            _DHAN_DATA["ad"] = None
        raise


def dhan_intraday_day(exch: str, token: str, day: date, instrument: str = "OPTIDX"):
    """One trading day's candle rebuilt from Dhan's 1-minute candles (09:15-15:30 trades):
    (YYYY-MM-DD, o, h, l, c), or None when the contract did not trade that day."""
    from brokers.dhan import _SEG
    dhan_daily_candles(exch, token, instrument)          # makes sure the Dhan login is up
    m = _DHAN_DATA["ad"].intraday_day_ohlc(token, day.isoformat(), _SEG.get(exch, "NSE_FNO"), instrument)
    if not m:
        return None
    return (day.isoformat(), m[0], m[1], m[2], m[3])


def official_strike_candle(exch: str, token: str, instrument: str, mode: str, now: datetime):
    """The candle a strike's levels are built from, always of the expected trading day:
    the official daily candle when the source has it, otherwise the same day rebuilt from
    Dhan's 1-minute candles (the daily candle is often published late in the evening).
    A weekday with no trades at all (exchange holiday) is skipped back to the day before.
    Returns (candle, source, day) or (None, reason, day)."""
    day = expected_candle_day(mode, now)
    rows, src = [], ""
    try:
        rows, src = strike_daily_candles(exch, token, instrument, now=now)
    except Exception as e:
        src = f"daily candles unavailable ({e})"
    for _ in range(6):
        c = pick_pivot_candle(rows, mode, now, day=day)
        if c:
            return c, src, day
        if mode == "next" and now.weekday() < 5 and now.time() < dt_time(15, 30):
            return None, "today's candle is not complete yet", day
        try:
            c = dhan_intraday_day(exch, token, day, instrument)
        except Exception as e:
            return None, f"no candle for {day} yet ({e})", day
        if c:
            return c, "Dhan 1-min", day
        day = prev_weekday(day)                       # no trades that day: market holiday
    return None, "no candle found", day


LEVEL_SOURCES = {"DHAN": "Dhan (Angel backup)", "ANGEL": "Angel (Dhan backup)"}
_DATA_SETTINGS_FILE = os.path.join(BASE_DIR, "data_settings.json")
_SRC_DOWN = {"Dhan": 0.0, "Angel": 0.0}       # when a source last failed (skip it for 2 minutes)


def get_level_source() -> str:
    """Which broker's daily candles the strike levels come from first: DHAN (default) or ANGEL."""
    v = str(read_json_file(_DATA_SETTINGS_FILE).get("level_source") or os.getenv("LEVELS_SOURCE") or "DHAN").upper()
    return v if v in LEVEL_SOURCES else "DHAN"


def set_level_source(v: str):
    d = read_json_file(_DATA_SETTINGS_FILE)
    d["level_source"] = v if v in LEVEL_SOURCES else "DHAN"
    write_json_file(_DATA_SETTINGS_FILE, d)


def _source_fns(exch, token, instrument, now):
    return {"Dhan": lambda: dhan_daily_candles(exch, token, instrument, now=now),
            "Angel": lambda: angel_daily_candles(exch, token, now=now)}


def strike_daily_candles(exch: str, token: str, instrument: str = "OPTIDX", now: datetime = None):
    """Official daily candles of one option from the selected source first (get_level_source),
    the other broker as the backup. Returns (rows, source). Raises with both reasons."""
    first = "Dhan" if get_level_source() == "DHAN" else "Angel"
    order = [first, "Angel" if first == "Dhan" else "Dhan"]
    fns = _source_fns(exch, token, instrument, now)
    errs = []
    for i, name in enumerate(order):
        if time.time() - _SRC_DOWN[name] < 120:
            errs.append(f"{name}: skipped (failed in the last 2 minutes)")
            continue
        try:
            rows = fns[name]()
            if rows:
                return rows, name
            errs.append(f"{name}: no candles")
        except Exception as e:
            _SRC_DOWN[name] = time.time()
            errs.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errs))


def cross_check_candle(exch: str, token: str, instrument: str, used: str, cndl, mode: str, now: datetime):
    """Compare the candle the levels were built from with the other broker's candle for the same
    day. Returns ("MATCH" | "MISMATCH" | "NOT CHECKED", detail)."""
    other = "Angel" if used == "Dhan" else "Dhan"
    if time.time() - _SRC_DOWN[other] < 120:
        return "NOT CHECKED", f"{other} unavailable"
    try:
        rows = _source_fns(exch, token, instrument, now)[other]()
    except Exception as e:
        _SRC_DOWN[other] = time.time()
        return "NOT CHECKED", f"{other}: {e}"
    o = next((r for r in rows if r[0] == cndl[0]), None)
    if not o:
        return "NOT CHECKED", f"{other} has no candle for {cndl[0]}"
    diff = max(abs(o[2] - cndl[2]), abs(o[3] - cndl[3]), abs(o[4] - cndl[4]))
    if diff <= 0.05 + 1e-9:
        return "MATCH", ""
    return "MISMATCH", (f"{used} H/L/C {cndl[2]}/{cndl[3]}/{cndl[4]} vs {other} {o[2]}/{o[3]}/{o[4]}")


def read_json_file(path: str) -> dict:
    try:
        with open(path, "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_json_file(path: str, data: dict):
    """Write through a temp file so a reader never sees half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_next_day_plan(symbol: str) -> dict:
    """Load next-day plan for a symbol from next_day_plans.json or next_day_plan_*.json."""
    try:
        nd_file = os.path.join(BASE_DIR, "next_day_plans.json")
        if os.path.exists(nd_file):
            with open(nd_file, "r") as f:
                all_plans = json.load(f)
            # 1. Exact match on symbol attribute
            for k, plan in all_plans.items():
                if plan.get("symbol") == symbol:
                    return plan
            # 2. Key prefix match (e.g. NIFTY_2026-10-05)
            for k, plan in all_plans.items():
                if k == symbol or k.startswith(f"{symbol}_"):
                    return plan
        # Fallback to single-symbol files next_day_plan_*.json
        import glob
        pattern = os.path.join(BASE_DIR, "next_day_plan_*.json")
        for fpath in sorted(glob.glob(pattern), reverse=True):
            if os.path.basename(fpath) == "next_day_plans.json":
                continue
            with open(fpath, "r") as f:
                data = json.load(f)
            if data.get("symbol") == symbol:
                return data
            if "pivots" in data and not symbol:
                return data
    except Exception as e:
        print(f"[PLAN] Error loading next day plan for {symbol}: {e}")
    return {}


# ═══════════════════════════════════════════════════════════
# FEED ENGINE
# ═══════════════════════════════════════════════════════════
class FeedEngine:
    def __init__(self):
        self.live_data          = {}
        self.token_to_meta      = {}
        self.spot_price         = {}
        self.spot_prev_price    = {}     # previous tick to detect P-cross
        self.spot_token         = {}
        self.spot_ohlc          = {}
        self.spot_pivots        = {}
        self.spot_zone          = {}
        self.market_direction   = {}     # {symbol -> direction dict}
        self.h1_bias            = {}     # {symbol -> 1H bias dict}
        self.reversal_alerts    = []
        self.s1_s2_watchlist    = []
        self.ce_sup_watchlist   = []
        self.pe_res_watchlist   = []
        self.daily_focus        = {}        # primary focus strikes (15M intraday)
        self.focus_15m          = {}        # 15-Min Intraday focus strikes
        self.focus_1h           = {}        # 1-Hour Structure focus strikes
        self.nearby_pivot_strikes = set()   # nearby strikes of all CE and PE pivots
        self.next_day_plan      = {}        # {symbol -> plan dict}
        self.spot_candles       = {}        # {sym -> {'15m': candle, '1h': candle}}
        self.mtf_pivots         = {}        # {Weekly/Daily/1-Hour/15-Min/5-Min -> pivot dict}
        self._focus_date        = None      # track when focus strikes were last set
        self._alert_strikes     = set()     # rate-limit touch alerts per strike+level
        self.lock               = threading.Lock()
        self.sws                = None
        self.token_list         = []
        self.ws                 = None   # gspread Worksheet
        self.running            = False
        self.oi_data            = {}
        self.strategy_states    = {}        # (opt_type, strike) -> StrategyStrikeState
        self.alert_mgr          = AlertManager()
        self.forward_tracker    = ForwardTestTracker()
        self.forward_test_sheet = None
        self.orders_sheet       = None             # "Orders Log" tab (mirror of live_orders_log.csv)
        self._last_orders_sync_ts = 0.0
        self._orders_sync_busy  = False
        self.multi_symbol_sheet = None
        self.calculated_symbols_cache = {}
        self._load_calculated_symbols_cache()
        self.strike_pivots      = {}        # {token / (opt_type, strike) -> {P, S1, S2, S3, R1, R2, R3, H, L, C}}
        self.ft_timeframe_filter = "ALL"        # Timeframe filter: ALL, 15M Intraday, 1H Structure
        self.exchange           = "NFO"          # current exchange (set during prepare)
        self._sheet_write_lock  = threading.Lock()  # prevent concurrent sheet writes
        self._ft_b2_cache       = "ALL"            # cached B2 filter from sheet
        self._ft_b2_last_read   = 0.0              # timestamp of last B2 read
        self._last_ft_format_ts = 0.0              # timestamp of last formatting call (quota guard)
        self._last_ft_sync_ts   = 0.0              # timestamp of last forward test sheet write
        self._last_multi_sync_ts = 0.0             # timestamp of last multi-symbol sheet write
        self._last_sheet_write_ts = 0.0            # timestamp of last option chain sheet write
        self._quota_backoff_until = 0.0            # 429 quota backoff pause timestamp
        self.status_cb          = None             # UI status bar notification callback
        self._last_tick_time    = time.time()      # timestamp of last received live tick
        self._is_connected      = False            # WebSocket connection state
        self._ws_watchdog_thread = None            # background health monitor thread
        self._last_reconnect_time = 0.0            # rate-limit reconnects
        self._ws_reconnect_lock = threading.Lock() # lock for clean reconnects

    def prepare(self, exchange, symbol, expiries, n_strikes):
        """
        Prepare tokens for one OR multiple expiries.
        expiries: str  (single) or list[str]  (multi-expiry)
        """
        if isinstance(expiries, str):
            expiries = [expiries]
        self._prep_args = (exchange, symbol, list(expiries), n_strikes)

        cfg    = EXCHANGE_CONFIG.get(exchange, EXCHANGE_CONFIG["NFO"])
        step   = get_symbol_step(symbol, exchange)
        extype = cfg["exchange_type"]
        stok, spot_exch_type = get_symbol_spot_token(symbol, exchange)

        seg_candidates = [exchange]
        if exchange == "BFO": seg_candidates = ["BFO", "BSE"]
        if exchange == "MCX": seg_candidates = ["MCX"]

        all_filter_dfs = []
        primary_atm    = None

        df = Session.scrip_df
        if df is None:
            Session.load_scrip()
            df = Session.scrip_df

        for expiry in expiries:
            base = df[
                (df["name"] == symbol) &
                (df["exch_seg"].isin(seg_candidates)) &
                (df["instrumenttype"].isin(cfg["inst_types"])) &
                (df["expiry"] == expiry)
            ].copy()

            if base.empty:
                base = df[
                    (df["name"] == symbol) &
                    (df["exch_seg"].isin(seg_candidates)) &
                    (df["expiry"] == expiry)
                ].copy()

            if base.empty:
                print(f"[WARN] No data for {symbol}/{exchange}/{expiry} — skipping")
                continue

            base["strike_int"] = base["strike"].astype(float).div(100).astype(int)
            base["opt_type"]   = base["symbol"].str.strip().str[-2:].str.upper()
            bad = ~base["opt_type"].isin(["CE", "PE"])
            if bad.any():
                base.loc[bad, "opt_type"] = (
                    base.loc[bad, "optiontype"].str.strip().str.upper().str[-2:]
                )

            all_s = sorted(base["strike_int"].unique())
            if not step or step <= 0:
                diffs = [s2 - s1 for s1, s2 in zip(all_s[:-1], all_s[1:]) if s2 - s1 > 0]
                if diffs:
                    from collections import Counter
                    step = Counter(diffs).most_common(1)[0][0]
                else:
                    step = 50

            # ATM from live spot (only computed once — same for all expiries of same symbol)
            atm = all_s[len(all_s) // 2]
            live_spot = self.spot_price.get(symbol, 0)
            if live_spot > 0:
                atm = int(round(live_spot / step) * step)
            else:
                cached_tv = get_tv_multi_timeframe_pivots(symbol)
                p_ref = (
                    cached_tv.get("15-Min", {}).get("P")
                    or cached_tv.get("Daily", {}).get("P")
                    or cached_tv.get("15-Min", {}).get("C")
                )
                if p_ref and p_ref > 0:
                    atm = int(round(p_ref / step) * step)
            if primary_atm is None:
                primary_atm = atm

            # WebSocket instruments = ONLY the strikes calculated from 15M and 1H pivot levels
            # (15M R1/R2 + S1/S2 and 1H R1/R2 + S1/S2 snapped to the strike step).
            # No ATM band, no Daily/Weekly/plan "nearby" strikes.
            targets = set()
            mtf_for_focus = get_tv_multi_timeframe_pivots(symbol)
            if mtf_for_focus:
                self.mtf_pivots = mtf_for_focus
                # Same day + same symbol: keep the strikes already chosen (levels frozen for the session)
                if (self.daily_focus and self._focus_date == datetime.now().strftime("%Y-%m-%d")
                        and getattr(self, "_focus_symbol", None) == symbol):
                    f_calc = self.daily_focus
                else:
                    f_calc = select_daily_4_focus_strikes(mtf_for_focus, step)
                self.daily_focus = f_calc
                self.focus_15m = f_calc.get("15M", {})
                self.focus_1h = f_calc.get("1H", {})
                self._focus_date = datetime.now().strftime("%Y-%m-%d")
                self._focus_symbol = symbol
                for _s in f_calc.get("all_focus", []):
                    if _s > 0: targets.add(_s)
            if not targets:
                print(f"[WARN] No 15M/1H pivots for {symbol} — falling back to ATM ±{n_strikes} strikes")
                targets = set(atm + i * step for i in range(-n_strikes, n_strikes + 1))
            print(f"[WS] {symbol} {expiry}: subscribing pivot strikes only → {sorted(targets)}")

            nd_plan = load_next_day_plan(symbol)
            if nd_plan:
                self.next_day_plan[symbol] = nd_plan
            self.nearby_pivot_strikes = set()

            filter_df = base[
                base["strike_int"].isin(targets) &
                base["opt_type"].isin(["CE", "PE"])
            ].copy()
            filter_df["expiry_tag"] = expiry   # carry expiry through

            if not filter_df.empty:
                all_filter_dfs.append(filter_df)

        if not all_filter_dfs:
            raise ValueError(
                f"No strikes found for {symbol}/{exchange} in expiries: {expiries}"
            )

        combined = pd.concat(all_filter_dfs, ignore_index=True)

        # Build token_to_meta (includes expiry tag per token)
        self.token_to_meta = {
            str(r["token"]): {
                "symbol":   symbol,
                "strike":   r["strike_int"],
                "opt_type": r["opt_type"],
                "expiry":   r["expiry_tag"],
                "token":    str(r["token"]),   # store token for order placement
            }
            for _, r in combined.iterrows()
        }

        option_tokens = combined["token"].astype(str).tolist()

        self.spot_token = {symbol: stok} if stok else {}
        self.token_list = [{"exchangeType": extype, "tokens": option_tokens}]
        if stok:
            self.token_list.append({
                "exchangeType": spot_exch_type,
                "tokens": [stok]
            })

        # Load TradingView aligned daily Fibonacci pivots for option strikes
        self.load_or_fetch_strike_pivots(symbol, option_tokens, self.token_to_meta)

        # Record symbol into Multi-Symbol Tracker tab
        if self.mtf_pivots and self.daily_focus:
            try:
                self.record_calculated_symbol(
                    sym=symbol,
                    exch=exchange,
                    spot=self.spot_price.get(symbol, 0.0),
                    step=step,
                    lot=get_symbol_lot_size(symbol, exchange),
                    expiry=expiries[0] if expiries else "",
                    mtf_pivots=self.mtf_pivots,
                    focus=self.daily_focus
                )
            except Exception as _rc_err:
                print(f"[PREPARE] record_calculated_symbol error: {_rc_err}")

        # Return primary (first) expiry filter_df for backwards compat
        self.exchange = exchange   # store for log_entry calls
        return all_filter_dfs[0], primary_atm, step

    # ── Reversal order mapping helpers ─────────────────────────────────────
    def get_strike_ltp(self, opt_type: str, strike: int, expiry: str = None) -> float:
        """Live LTP for (opt_type, strike) from the WebSocket feed (the given expiry, else nearest
        expiry first). Falls back to the last close in strike_pivots only when no live tick exists."""
        with self.lock:
            metas = [(tok, m) for tok, m in self.token_to_meta.items()
                     if m.get("opt_type") == opt_type and m.get("strike") == strike
                     and (not expiry or m.get("expiry") == expiry)]
            live = dict(self.live_data)
            sp = ((self.strike_pivots.get((opt_type, strike, expiry)) if expiry else None)
                  or self.strike_pivots.get((opt_type, strike)) or {})
        # nearest expiry first
        def _exp_key(item):
            try:
                return datetime.strptime(item[1].get("expiry", ""), "%d%b%Y")
            except Exception:
                return datetime.max
        for tok, _m in sorted(metas, key=_exp_key):
            v = live.get(tok, {}).get("ltp", 0) or 0
            if v > 0:
                return float(v)
        return float(sp.get("prev_c", 0) or 0)

    def compute_ce_on_support(self, ce_strikes: list) -> bool:
        """
        Reversal pairing rule for one timeframe's 2 CE + 2 PE strikes:
          • CE on support (CE LTP at/below its own Fib P, heading to S2)
                → BUY CE from S2  +  SELL PE from R2
          • CE at resistance (CE LTP above its own Fib P, heading to R2)
                → SELL CE from R2 +  BUY PE from S2
        A confirmed / in-progress CE trade always wins over the LTP check.
        """
        if not ce_strikes:
            return True
        with self.lock:
            states = {s: self.strategy_states.get(("CE", s)) for s in ce_strikes}
            pivs   = {s: (self.strike_pivots.get(("CE", s)) or {}) for s in ce_strikes}
        # 1. Active CE trade decides the side
        for s in ce_strikes:
            st = states.get(s)
            if not st:
                continue
            state = getattr(st, "state", "WAITING") or "WAITING"
            if state in ("CONFIRMED_BUY",) or state.startswith("RECLAIM_S") or state.startswith("SWEEP_S"):
                return True
            if state in ("CONFIRMED_SELL",) or state.startswith("RECLAIM_R") or state.startswith("SWEEP_R"):
                return False
        # 2. Otherwise compare CE LTP with its own pivot P
        votes_sup, votes_res = 0, 0
        for s in ce_strikes:
            p = float(pivs[s].get("P", 0) or 0)
            ltp = self.get_strike_ltp("CE", s)
            if p <= 0 or ltp <= 0:
                continue
            if ltp <= p:
                votes_sup += 1
            else:
                votes_res += 1
        return votes_sup >= votes_res

    def _nearest_expiry(self, expiries) -> str:
        """Nearest not-yet-expired expiry (DDMONYYYY) of the given list."""
        today = datetime.now().date()
        best = None
        for e in set(expiries or []):
            try:
                d = datetime.strptime(str(e).upper(), "%d%b%Y").date()
            except Exception:
                continue
            if d >= today and (best is None or d < best[0]):
                best = (d, e)
        return best[1] if best else ""

    def _store_strike_pivot(self, s_info: dict, nearest_expiry: str):
        """Index one strike's levels by token and (type, strike, expiry). The short (type, strike)
        key is kept for the nearest expiry only, so one expiry's levels never show under another."""
        self.strike_pivots[str(s_info["token"])] = s_info
        if s_info.get("expiry"):
            self.strike_pivots[(s_info["opt_type"], s_info["strike"], s_info["expiry"])] = s_info
        if not s_info.get("expiry") or not nearest_expiry or s_info["expiry"] == nearest_expiry:
            self.strike_pivots[(s_info["opt_type"], s_info["strike"])] = s_info

    def load_or_fetch_strike_pivots(self, symbol: str, option_tokens: list, token_to_meta: dict):
        """
        Loads the daily Fibonacci pivots of the option strikes for the session TradingView is
        showing (see pivot_session_date), built from the previous day's official daily candle.
        Anything missing is fetched, and a background check keeps retrying until every
        focus strike has its levels and rebuilds them when the session date changes.
        """
        basis_key = f"{pivot_session_date().isoformat()}:session"
        nearest = self._nearest_expiry(m.get("expiry") for m in token_to_meta.values())
        cdata = read_json_file(STRIKE_PIVOT_CACHE)
        strikes_map = cdata.get("strikes", {}) if (cdata.get("basis_key") == basis_key
                                                    and cdata.get("symbol") == symbol) else {}
        if cdata and not strikes_map:
            print("[STRIKE PIVOTS] Cache is from another session — rebuilding from the official daily candles")
        with self.lock:
            for s_info in strikes_map.values():
                self._store_strike_pivot(s_info, nearest)
            self._pivot_basis = basis_key
        print(f"[STRIKE PIVOTS] Loaded {len(strikes_map)} strike pivots for {basis_key[:10]} from {STRIKE_PIVOT_CACHE}")

        missing = [t for t in option_tokens if t not in self.strike_pivots]
        if missing:
            missing_expiries = {token_to_meta[t]["expiry"] for t in missing if t in token_to_meta and token_to_meta[t].get("expiry")}
            for exp in (sorted(missing_expiries) or [None]):
                self.fetch_and_map_all_strikes(symbol, expiry=exp, force_fetch=False)
        self._start_pivot_watchdog()

    def _start_pivot_watchdog(self):
        """Every minute: refetch focus strikes that still have no levels (Angel refused or had no
        data), and rebuild all levels when a new session starts while the app keeps running."""
        if getattr(self, "_pivot_watchdog_on", False):
            return
        self._pivot_watchdog_on = True

        def _loop():
            while True:
                time.sleep(60)
                try:
                    now = datetime.now()
                    in_session = is_live_market_open(now) and now.time() >= dt_time(9, 15)
                    basis_key = f"{pivot_session_date(now).isoformat()}:session"
                    jobs = dict(getattr(self, "_pivot_jobs", {}))   # (symbol, expiry) -> missing tokens
                    if basis_key != getattr(self, "_pivot_basis", basis_key) and not in_session:
                        print(f"[STRIKE PIVOTS] New session {basis_key[:10]} — rebuilding strike levels")
                        with self.lock:
                            self.strike_pivots.clear()
                            self._pivot_basis = basis_key
                        for (sym, exp) in jobs:
                            self.fetch_and_map_all_strikes(sym, exp, force_fetch=True)
                        continue
                    if not in_session and time.time() - getattr(self, "_last_1min_refresh", 0) > 900:
                        with self.lock:
                            rebuilt = any(isinstance(v, dict) and v.get("source") == "Dhan 1-min"
                                          for v in self.strike_pivots.values())
                        if rebuilt:
                            self._last_1min_refresh = time.time()
                            print("[STRIKE PIVOTS] Checking whether the official daily candle is out for levels built from 1-minute candles")
                            for (sym, exp) in jobs:
                                self.fetch_and_map_all_strikes(sym, exp, force_fetch=True)
                            continue
                    for (sym, exp), miss in jobs.items():
                        if miss:
                            print(f"[STRIKE PIVOTS] Retrying {len(miss)} strike(s) without levels ({sym} {exp})")
                            self.fetch_and_map_all_strikes(sym, exp, force_fetch=False)
                except Exception as e:
                    print(f"[STRIKE PIVOTS] watchdog notice: {e}")

        threading.Thread(target=_loop, daemon=True, name="pivot-watchdog").start()

    def upcoming_expiries(self, symbol: str, n: int = 2, from_day: date = None) -> list:
        """The first n option expiries of the symbol that are still open on from_day (default today),
        nearest first, as the instrument list writes them (e.g. '13OCT2026')."""
        df = Session.scrip_df
        if df is None:
            return []
        exch = get_symbol_exchange(symbol)
        o = df[(df['name'] == symbol) & (df['exch_seg'] == exch)
               & (df['instrumenttype'].isin(['OPTIDX', 'OPTSTK']))]
        day = pd.Timestamp(from_day or datetime.now().date())
        exps = {}
        for e in o['expiry'].dropna().unique():
            try:
                d = pd.to_datetime(e, format='%d%b%Y')
            except Exception:
                continue
            if d >= day:
                exps[e] = d
        return sorted(exps, key=exps.get)[:n]

    def build_next_day_levels(self, symbol: str):
        """After the close (15:45): the next session's strike levels, built from today's candle,
        for the current AND the next expiry, then eod_planner.py, which takes its strike levels
        from them. Runs once a day, whichever of the two 15:45 timers gets here first."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            if getattr(self, "_next_levels_date", None) == today:
                return
            self._next_levels_date = today
        try:
            print(f"\n[EOD] ⏰ 15:45 → Calculating NEXT-DAY levels for {symbol}...")
            _mtf = get_tv_multi_timeframe_pivots(symbol, force_refresh=True)
            if _mtf:
                with self.lock:
                    self.mtf_pivots = _mtf
            nd = datetime.now().date() + timedelta(days=1)
            while nd.weekday() >= 5:
                nd += timedelta(days=1)
            exps = self.upcoming_expiries(symbol, 2, from_day=nd) or [None]
            for exp in exps:
                _mapped = self.fetch_and_map_all_strikes(symbol, exp, force_fetch=True, mode="next")
                print(f"[EOD] ✅ Next-day strike levels {symbol} {exp or 'nearest'}: {len(_mapped or {})} strikes "
                      f"→ strike_pivot_cache_next.json")
        except Exception as _nd_err:
            print(f"[EOD] next-day level calc error: {_nd_err}")
        try:
            import subprocess as _sp
            _planner = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'eod_planner.py')
            _sp.Popen([sys.executable or 'python3', _planner], cwd=os.path.dirname(_planner),
                      stdout=open(os.path.join(BASE_DIR, 'eod_planner_output.log'), 'w'), stderr=_sp.STDOUT)
            print("[EOD] eod_planner.py started (output: eod_planner_output.log)\n")
        except Exception as _ep_err:
            print(f"[EOD] eod_planner auto-run error: {_ep_err}")

    def fetch_and_map_all_strikes(self, symbol=None, expiry=None, force_fetch=True, mode="session") -> dict:
        """
        Fibonacci pivots (P, R1-R3, S1-S3) of the focus option strikes, exactly as TradingView's
        'Pivot Points Standard' (Type Fibonacci, timeframe Daily) draws them:
        P = (H + L + C) / 3 of the previous day's official daily candle (Dhan historical API,
        Angel One's as the backup).

        mode="session": levels of the session TradingView shows now (pivot_session_date). Saved to
                        strike_pivot_cache.json and used by the dashboard, alerts and orders.
                        During market hours they are locked; only missing strikes are filled.
        mode="next":    after the close, the next session's levels from today's completed candle.
                        Saved to strike_pivot_cache_next.json for planning; the live levels are
                        not touched.
        No level is ever made up: a strike without an official candle gets no levels and is
        retried every minute.
        """
        symbol = symbol or "NIFTY"
        try:
            if not Session.auth_token or not Session.feed_token:
                Session.login()
            if Session.scrip_df is None:
                Session.load_scrip()
            df = Session.scrip_df
            if df is None:
                return {}

            exch = get_symbol_exchange(symbol)
            opts = df[(df['name'] == symbol) & (df['exch_seg'] == exch) & (df['instrumenttype'].isin(['OPTIDX', 'OPTSTK', 'OPTFUT']))].copy()
            if opts.empty:
                opts = df[(df['name'] == symbol) & (df['strike'].astype(float) > 0)].copy()
                if not opts.empty:
                    exch = opts.iloc[0]['exch_seg']
            opts['exp_dt'] = pd.to_datetime(opts['expiry'], format='%d%b%Y')
            now_ts = pd.Timestamp.now()
            fut_exp = opts[opts['exp_dt'] >= now_ts.floor('D')]
            if fut_exp.empty:
                fut_exp = opts
            if fut_exp.empty:
                return {}
            nearest_expiry = fut_exp.sort_values('exp_dt').iloc[0]['expiry']
            target_expiry = expiry or nearest_expiry

            sub = opts[opts['expiry'] == target_expiry].copy()
            sub['strike_int'] = (sub['strike'].astype(float) / 100).astype(int)

            # Determine ATM strike
            spot_val = self.spot_price.get(symbol, 0)
            if spot_val <= 0:
                mtf = self.mtf_pivots.get("Daily", {})
                spot_val = mtf.get("C", 0)
            step = get_symbol_step(symbol, exch)
            if spot_val <= 0:
                u_s = sorted(sub['strike_int'].unique())
                spot_val = u_s[len(u_s)//2] if u_s else 1000
            atm = int(round(spot_val / step) * step)

            # Only calculate strikes which are required as per timeframe levels (4 to max 6 strikes)
            target_strike_set = set()
            try:
                mtf_all = self.mtf_pivots or get_tv_multi_timeframe_pivots(symbol)
                focus_calc = select_daily_4_focus_strikes(mtf_all, step)
                target_strike_set.update(focus_calc.get("all_focus", []))   # only traded 15M/1H strikes
            except Exception as e_foc:
                print(f"[STRIKE MAPPER] Focus calculation notice: {e_foc}")

            if not target_strike_set:
                target_strike_set = {atm - 2*step, atm - step, atm, atm + step, atm + 2*step}
            # every strike on the dashboard for this expiry gets its levels too (also after the
            # close, when the 15M/1H focus moves to the next day's strikes)
            for _m in list(getattr(self, "token_to_meta", {}).values()):
                if _m.get("expiry") == target_expiry and _m.get("strike"):
                    target_strike_set.add(int(_m["strike"]))

            focus_df = sub[sub['strike_int'].isin(target_strike_set)].copy()
            if focus_df.empty:
                min_s = atm - 4 * step
                max_s = atm + 4 * step
                focus_df = sub[(sub['strike_int'] >= min_s) & (sub['strike_int'] <= max_s)].copy()
            tokens = focus_df['token'].astype(str).tolist()

            meta_map = {}
            for _, r in focus_df.iterrows():
                meta_map[str(r['token'])] = {
                    'token': str(r['token']),
                    'symbol': r['symbol'],
                    'strike': int(r['strike_int']),
                    'opt_type': 'CE' if r['symbol'].endswith('CE') else 'PE',
                    'expiry': target_expiry,
                    'itype': 'OPTSTK' if str(r.get('instrumenttype', '')) == 'OPTSTK' else 'OPTIDX',
                }

            _now = datetime.now()
            _after_close = _now.weekday() >= 5 or _now.time() >= dt_time(15, 30)
            _in_session = (not _after_close) and _now.time() >= dt_time(9, 15)
            if mode == "next":
                if not _after_close:
                    print("[STRIKE MAPPER] Next-session levels are built after 15:30 (today's candle is not complete yet)")
                    return {}
                cache_file = STRIKE_PIVOT_NEXT_CACHE
                nd = _now.date() + timedelta(days=1)
                while nd.weekday() >= 5:
                    nd += timedelta(days=1)
                level_date = nd
            else:
                cache_file = STRIKE_PIVOT_CACHE
                level_date = pivot_session_date(_now)
            basis_key = f"{level_date.isoformat()}:{mode}"

            cd = read_json_file(cache_file)
            cached = cd.get("strikes", {}) if (cd.get("basis_key") == basis_key and cd.get("symbol") == symbol) else {}

            # Reuse a complete set: always during market hours (levels are locked), otherwise
            # unless a fresh fetch was asked for.
            if cached and all(t in cached for t in tokens) and (_in_session or not force_fetch):
                if mode == "session":
                    with self.lock:
                        for t in tokens:
                            if _in_session and t in self.strike_pivots:
                                continue
                            self._store_strike_pivot(cached[t], nearest_expiry)
                        self._pivot_basis = basis_key
                self._pivot_jobs = {**getattr(self, "_pivot_jobs", {}), (symbol, target_expiry): set()} if mode == "session" else getattr(self, "_pivot_jobs", {})
                return {t: cached[t] for t in tokens}

            if _in_session and mode == "session":
                # Locked strikes stay as they are; only compute what is missing
                fetch_tokens = [t for t in tokens if t not in self.strike_pivots and t not in cached]
                for t in tokens:
                    if t in cached and t not in self.strike_pivots:
                        with self.lock:
                            self._store_strike_pivot(cached[t], nearest_expiry)
            elif not force_fetch:
                fetch_tokens = [t for t in tokens if t not in cached]
            else:
                fetch_tokens = list(tokens)

            mapped, failed, checks = {}, {}, {}
            for tok in fetch_tokens:
                m = meta_map[tok]
                try:
                    cndl, src, _cday = official_strike_candle(exch, tok, m.get('itype', 'OPTIDX'), mode, _now)
                    if not cndl:
                        failed[tok] = src
                    else:
                        c_date, _o, h, l, c = cndl
                        chk, chk_note = (cross_check_candle(exch, tok, m.get('itype', 'OPTIDX'), src, cndl, mode, _now)
                                         if src in ("Dhan", "Angel") else ("NOT CHECKED", "rebuilt from 1-minute candles"))
                        if chk == "MISMATCH":
                            print(f"[STRIKE MAPPER] ⚠️ {m['strike']} {m['opt_type']} {target_expiry} candle {c_date}: "
                                  f"Dhan and Angel differ ({chk_note}) — levels use {src}")
                        checks[chk] = checks.get(chk, 0) + 1
                        piv = calc_fib_pivots(h, l, c)
                        if not piv:
                            failed[tok] = f"bad candle {c_date} H={h} L={l} C={c}"
                        else:
                            mapped[tok] = {
                                'token': tok,
                                'symbol': m['symbol'],
                                'opt_type': m['opt_type'],
                                'strike': m['strike'],
                                'expiry': m['expiry'],
                                'candle_date': c_date,          # day whose H/L/C built these levels
                                'source': src,                  # Dhan or Angel
                                'cross_check': chk,             # other broker's candle: MATCH / MISMATCH / NOT CHECKED
                                'level_date': level_date.isoformat(),
                                'prev_h': round(h, 2),
                                'prev_l': round(l, 2),
                                'prev_c': round(c, 2),
                                **piv
                            }
                except Exception as he:
                    failed[tok] = str(he)
                time.sleep(0.35)   # SmartAPI historical rate limit (~3 req/s)

            for tok, why in failed.items():
                m = meta_map[tok]
                print(f"[STRIKE MAPPER] ⚠️ {m['strike']} {m['opt_type']} {target_expiry}: no levels ({why}) — will retry")

            # Merge with what is already saved for this session (all expiries are kept, by token)
            cd = read_json_file(cache_file)
            existing = cd.get("strikes", {}) if (cd.get("basis_key") == basis_key and cd.get("symbol") == symbol) else {}
            existing.update(mapped)
            write_json_file(cache_file, {
                'date': level_date.isoformat(),
                'basis_key': basis_key,
                'symbol': symbol,
                'expiry': nearest_expiry,
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strikes': existing,
            })

            if mode == "session":
                with self.lock:
                    for tok, s_info in mapped.items():
                        if _in_session and str(tok) in self.strike_pivots:
                            continue   # locked for the session
                        self._store_strike_pivot(s_info, nearest_expiry)
                    self._pivot_basis = basis_key
                    still = {t for t in tokens if t not in self.strike_pivots}
                jobs = dict(getattr(self, "_pivot_jobs", {}))
                jobs[(symbol, target_expiry)] = still
                self._pivot_jobs = jobs

            label = "next session" if mode == "next" else "session"
            print(f"[STRIKE MAPPER] ✅ {len(mapped)} strike(s) mapped for {symbol} {target_expiry} "
                  f"({label} {level_date.isoformat()}, {len(failed)} without data)"
                  + (f" | Dhan vs Angel check: {checks.get('MATCH', 0)} match, {checks.get('MISMATCH', 0)} differ, "
                     f"{checks.get('NOT CHECKED', 0)} not checked" if checks else ""))
            self.level_check_summary = dict(checks)
            return {t: existing[t] for t in tokens if t in existing}
        except Exception as ex:
            print(f"[STRIKE MAPPER] Error: {ex}")
            return {}

    # ── WebSocket ──────────────────────────────────────────
    def on_data(self, wsapp, message):
        # Never let one bad tick raise into the websocket thread (that closes the socket)
        try:
            self._on_data_impl(wsapp, message)
        except Exception as e:
            now = time.time()
            if now - getattr(self, "_last_tick_err", 0) > 30:
                self._last_tick_err = now
                print(f"[WS] tick processing error (feed continues): {e}")

    def _on_data_impl(self, wsapp, message):
        self._last_tick_time = time.time()
        self._is_connected = True
        if not isinstance(message,dict): return
        token=str(message.get("token",""))
        if not token: return
        ltp    = message.get("last_traded_price",       0)/100
        oi     = message.get("open_interest",            0)
        vol    = message.get("volume_trade_for_the_day", 0)
        close  = message.get("closed_price",             0)/100
        high   = message.get("high_price_of_the_day",    0)/100
        low    = message.get("low_price_of_the_day",     0)/100
        open_p = message.get("open_price_of_the_day",    0)/100
        chg    = round(((ltp-close)/close*100),2) if close else 0

        now_str = datetime.now().strftime("%H:%M:%S")

        with self.lock:
            curr = self.live_data.get(token, {})
            # Rolling fallback in case exchange high/low is not yet published
            r_high  = max(curr.get("high", ltp), ltp, high)
            r_low_c = curr.get("low", ltp)
            r_low   = min(filter(lambda x: x>0, [r_low_c, ltp, low if low>0 else ltp])) if ltp>0 else 0
            r_close = close if close>0 else curr.get("close", ltp)
            r_open  = open_p if open_p>0 else curr.get("open", ltp)

            # Today's traded range, used by the setups. Before the first trade of the day the
            # feed still carries yesterday's close and high/low; those must never count as a
            # sweep or an entry. A tick counts only when the exchange says its last trade was
            # today after 09:15 (or, if the feed gives no trade time, when the price has moved
            # during market hours).
            today_s = datetime.now().strftime("%Y-%m-%d")
            if curr.get("s_date") != today_s:
                s_live, s_high, s_low = False, 0.0, 0.0
            else:
                s_live, s_high, s_low = curr.get("s_live", False), curr.get("s_high", 0.0), curr.get("s_low", 0.0)
            traded_now = traded_today_tick(message, ltp, curr.get("ltp", 0) if curr.get("s_date") == today_s else 0)
            if traded_now and ltp > 0:
                hi_c = [ltp] + ([high] if high > 0 else []) + ([s_high] if s_high > 0 else [])
                lo_c = [ltp] + ([low] if low > 0 else []) + ([s_low] if s_low > 0 else [])
                s_high, s_low, s_live = max(hi_c), min(lo_c), True
            elif s_live and ltp > 0:
                s_high, s_low = max(s_high, ltp), min(s_low, ltp) if s_low > 0 else ltp

            self.live_data[token] = {
                "s_date": today_s, "s_live": s_live, "s_high": s_high, "s_low": s_low,
                "ltp":   ltp,
                "oi":    oi,
                "vol":   vol,
                "chg":   chg,
                "high":  r_high,
                "low":   r_low,
                "close": r_close,
                "open":  r_open,
                "time":  now_str
            }

            # Strike levels come only from the official daily candles (fetch_and_map_all_strikes),
            # never from ticks.

            for sym,stok in self.spot_token.items():
                if token==stok:
                    prev = self.spot_price.get(sym, 0)
                    if prev > 0:
                        self.spot_prev_price[sym] = prev
                    self.spot_price[sym] = ltp
                    self.spot_ohlc[sym]  = {"high": r_high, "low": r_low, "close": r_close, "open": r_open}
                    sp_piv = calc_fib_pivots(r_high, r_low, r_close)
                    if sp_piv:
                        self.spot_pivots[sym] = sp_piv
                        self.spot_zone[sym]   = get_pivot_zone(ltp, sp_piv)

                    # ── Live 15M & 1H timeframe candle tracking ──────────
                    now_dt = datetime.now()
                    b_15m = (now_dt.hour, now_dt.minute // 15)
                    b_1h  = now_dt.hour

                    sc = self.spot_candles.setdefault(sym, {
                        "15m": {"bucket": b_15m, "open": ltp, "high": ltp, "low": ltp, "close": ltp},
                        "1h":  {"bucket": b_1h,  "open": ltp, "high": ltp, "low": ltp, "close": ltp}
                    })

                    c15 = sc["15m"]
                    if c15["bucket"] != b_15m:
                        if c15["high"] > 0 and c15["low"] > 0:
                            # 15M candle completed: store in candle history for liquidity sweep checks
                            sc.setdefault("history_15m", []).append({
                                "bucket": c15["bucket"],
                                "open": round(c15["open"], 2),
                                "high": round(c15["high"], 2),
                                "low": round(c15["low"], 2),
                                "close": round(c15["close"], 2),
                            })
                            # Keep last 50 completed candles
                            if len(sc["history_15m"]) > 50:
                                sc["history_15m"] = sc["history_15m"][-50:]
                        c15["open"] = c15["high"] = c15["low"] = c15["close"] = ltp
                        c15["bucket"] = b_15m
                    else:
                        c15["high"]  = max(c15["high"], ltp)
                        c15["low"]   = min(c15["low"], ltp) if c15["low"] > 0 else ltp
                        c15["close"] = ltp

                    c1h = sc["1h"]
                    if c1h["bucket"] != b_1h:
                        if c1h["high"] > 0 and c1h["low"] > 0:
                            # 1H candle completed: store in candle history
                            sc.setdefault("history_1h", []).append({
                                "bucket": c1h["bucket"],
                                "open": round(c1h["open"], 2),
                                "high": round(c1h["high"], 2),
                                "low": round(c1h["low"], 2),
                                "close": round(c1h["close"], 2),
                            })
                            if len(sc["history_1h"]) > 24:
                                sc["history_1h"] = sc["history_1h"][-24:]
                        c1h["open"] = c1h["high"] = c1h["low"] = c1h["close"] = ltp
                        c1h["bucket"] = b_1h
                    else:
                        c1h["high"]  = max(c1h["high"], ltp)
                        c1h["low"]   = min(c1h["low"], ltp) if c1h["low"] > 0 else ltp
                        c1h["close"] = ltp

    def on_open(self, wsapp):
        self._is_connected = True
        self._last_tick_time = time.time()
        print(f"[WS] ✅ WebSocket connected! Subscribing tokens in batches...")
        total_tokens = 0
        for item in self.token_list:
            tokens = item.get("tokens", [])
            extype = item.get("exchangeType", 2)
            total_tokens += len(tokens)
            chunk_size = 50
            for i in range(0, len(tokens), chunk_size):
                chunk = tokens[i:i + chunk_size]
                try:
                    self.sws.subscribe("oc001", 3, [{"exchangeType": extype, "tokens": chunk}])
                    time.sleep(0.05)
                except Exception as e:
                    print(f"[WS] Subscription error for chunk: {e}")
        print(f"[WS] ✅ Subscribed to {total_tokens} tokens across {len(self.token_list)} exchange types.")
        if self.status_cb:
            try: self.status_cb(f"● LIVE WS Connected ({total_tokens} tokens)", "#3fb950")
            except Exception: pass

    def on_error(self, wsapp, error, gen=None):
        print(f"[WS] ⚠️ Error: {error}")
        self._is_connected = False
        self._schedule_reconnect(reason=f"error: {error}", gen=gen)

    def on_close(self, wsapp, gen=None):
        print("[WS] ⚠️ Connection closed.")
        self._is_connected = False
        self._schedule_reconnect(reason="socket closed", gen=gen)

    def _ws_session_open(self, dt: datetime = None) -> bool:
        """True while the exchange of the running feed is in session (watchdog only forces reconnects then)."""
        dt = dt or datetime.now()
        if os.getenv("FORCE_MARKET_OPEN") == "1":
            return True
        if dt.weekday() >= 5:
            return False
        if getattr(self, "exchange", "NFO") == "MCX":
            return dt_time(9, 0) <= dt.time() <= dt_time(23, 30)
        return dt_time(9, 15) <= dt.time() <= dt_time(15, 30)

    def _new_ws(self):
        """
        Build a SmartWebSocketV2 whose callbacks only act while it is the CURRENT socket.
        Late close/error events from a socket we already replaced are ignored, so they can
        no longer tear down the healthy replacement (the old reconnect storm).
        """
        self._ws_gen = getattr(self, "_ws_gen", 0) + 1
        gen = self._ws_gen
        sws = SmartWebSocketV2(
            Session.auth_token, API_KEY, CLIENT_ID, Session.feed_token, max_retry_attempt=0
        )

        def _cur():
            return gen == self._ws_gen and self.running

        def _open(wsapp):
            if _cur():
                self._ws_fail_count = 0
                self.on_open(wsapp)

        def _data(wsapp, message):
            if gen == self._ws_gen:
                self.on_data(wsapp, message)

        def _err(wsapp, error=None, *a):
            if _cur():
                self.on_error(wsapp, error, gen=gen)

        def _close(wsapp, *a):
            if _cur():
                self.on_close(wsapp, gen=gen)

        sws.on_open, sws.on_data, sws.on_error, sws.on_close = _open, _data, _err, _close

        def _run():
            try:
                sws.connect()
            except Exception as e:
                print(f"[WS] connect() raised: {e}")
            # run_forever returned: if this is still the live socket, the link is gone
            if _cur():
                self._is_connected = False
                self._schedule_reconnect(reason="connect loop ended", gen=gen)

        threading.Thread(target=_run, daemon=True).start()
        return sws

    def _schedule_reconnect(self, reason="", gen=None):
        if not self.running:
            return
        lk = self.__dict__.setdefault("_sched_lock", threading.Lock())
        with lk:
            # An event from a socket that was already replaced (gen is stale) must not
            # start another reconnect — checked under the lock to avoid a check-then-act race.
            if gen is not None and gen != getattr(self, "_ws_gen", 0):
                return
            if getattr(self, "_reconnect_pending", False):
                return   # one reconnect at a time; duplicate error+close events collapse here
            self._reconnect_pending = True
        threading.Thread(target=self._reconnect_ws, args=(reason,), daemon=True).start()

    def _reconnect_ws(self, reason=""):
        try:
            with self._ws_reconnect_lock:
                if not self.running:
                    return
                # Back-off: 2s, 4s, 8s ... capped at 30s
                fails = getattr(self, "_ws_fail_count", 0)
                delay = min(2 ** (fails + 1), 30)
                since = time.time() - self._last_reconnect_time
                if since < delay:
                    time.sleep(delay - since)
                if not self.running:
                    return
                self._last_reconnect_time = time.time()
                self._ws_fail_count = fails + 1
                print(f"[WS] 🔄 Auto-reconnecting WebSocket ({reason}) — attempt {self._ws_fail_count}...")
                if self.status_cb:
                    try: self.status_cb(f"🔄 Reconnecting WS ({reason})...", "#f0883e")
                    except Exception: pass

                old = self.sws
                self._ws_gen = getattr(self, "_ws_gen", 0) + 1   # invalidate old socket's callbacks first
                try:
                    if old:
                        old.close_connection()
                except Exception:
                    pass

                # Every 3rd failed attempt, refresh the login: an expired JWT/feed token
                # makes every reconnect fail until the session is renewed.
                if self._ws_fail_count % 3 == 0:
                    try:
                        print("[WS] 🔑 Refreshing Angel One session before reconnect...")
                        Session.login()
                    except Exception as le:
                        print(f"[WS] Session refresh failed: {le}")

                self._last_tick_time = time.time()
                try:
                    self.sws = self._new_ws()
                    print("[WS] 🚀 Reconnect thread spawned.")
                except Exception as e:
                    print(f"[WS] Reconnection attempt failed: {e}")
        finally:
            self._reconnect_pending = False

    def _ws_watchdog_loop(self):
        while self.running:
            time.sleep(5)
            if not self.running:
                break
            if not self.token_list:
                continue
            now = time.time()
            no_tick = now - self._last_tick_time
            last_pong = getattr(self.sws, "last_pong_timestamp", None) if self.sws else None
            pong_age = (now - last_pong) if last_pong else None
            if self._ws_session_open():
                # In session the spot index ticks every second: 30s of silence = dead feed
                if no_tick > 30.0:
                    print(f"[WS WATCHDOG] ⚠️ No ticks for {int(no_tick)}s in market hours. Reconnecting...")
                    self._schedule_reconnect(reason="watchdog stalled ticks")
            else:
                # Outside session no ticks is normal; only reconnect if heartbeat is dead too
                if pong_age is not None and pong_age > 60.0 and no_tick > 60.0:
                    print(f"[WS WATCHDOG] ⚠️ Heartbeat lost ({int(pong_age)}s). Reconnecting...")
                    self._schedule_reconnect(reason="watchdog heartbeat lost")

    def start_ws(self):
        self._last_tick_time = time.time()
        self._ws_fail_count = 0
        self._reconnect_pending = False
        self.sws = self._new_ws()
        if not self._ws_watchdog_thread or not self._ws_watchdog_thread.is_alive():
            self._ws_watchdog_thread = threading.Thread(target=self._ws_watchdog_loop, daemon=True)
            self._ws_watchdog_thread.start()

    def stop_ws(self):
        self.running = False
        self._ws_gen = getattr(self, "_ws_gen", 0) + 1
        try:
            if self.sws: self.sws.close_connection()
        except Exception: pass


    # ── Google Sheets ──────────────────────────────────────
    def setup_sheet(self, symbol, strikes):
        """Set up Google Sheet with 21 columns including all Fibonacci Pivots on both CE and PE."""
        gc          = get_gspread_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        ws          = spreadsheet.get_worksheet(0)

        # Rename sheet tab if needed
        try:
            if ws.title != f"{symbol} Scanner":
                spreadsheet.batch_update({"requests": [{"updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "title": f"{symbol} Scanner"},
                    "fields": "title"
                }}]})
        except Exception as e:
            print(f"Sheet rename note: {e}")

        self.ws = ws

        # One-time fresh start: wipe every tab once (old forward-test / order data was inaccurate).
        _reset_marker = os.path.join(BASE_DIR, ".sheet_reset_done")
        _reset_tag = "fresh_20261007"
        try:
            _done = open(_reset_marker).read().strip() if os.path.exists(_reset_marker) else ""
        except Exception:
            _done = ""
        if _done != _reset_tag:
            try:
                for _s in spreadsheet.worksheets():
                    _s.clear()
                with open(_reset_marker, "w") as _f:
                    _f.write(_reset_tag)
                print("[Sheet] All tabs cleared for a fresh start")
            except Exception as _rst_err:
                print(f"[Sheet] fresh-start clear failed: {_rst_err}")

        col_headers = [
            "Expiry", "Type", "Strike", "LTP", "OI", "Volume", "Trading Zone", "Pivot P", "S1", "S2", "R1", "R2",
            "5M State", "LL / HH", "Signal", "SL", "T1", "T2", "T3", "Status", "Updated"
        ]

        row1 = [f"⚡ FIBONACCI PIVOT OPTION SCANNER | {symbol} | Connecting to Angel One WebSocket..."] + [""] * 21
        row2 = [f"⏱️ 15-MIN INTRADAY: Initializing Fibonacci Pivot levels & Strike Selection..."] + [""] * 21
        row3 = [f"⏳ 1-HOUR STRUCTURE: Initializing 1H Fibonacci Pivots & Structure Strikes..."] + [""] * 21
        row4 = ["🚨 REVERSAL STRATEGY: S2 Reclaim (BUY) & R2 Rejection (SELL) — Waiting for first data..."] + [""] * 21
        row5 = ["📊 OI SUMMARY: Waiting for data..."] + [""] * 21
        row6 = [""] * 22
        row7_ce_hdr  = ["📗 CALL (CE) SECTION — BUY ZONE ▲"] + [""] * 21
        row8_ce_cols = col_headers

        all_rows = [row1, row2, row3, row4, row5, row6, row7_ce_hdr, row8_ce_cols]
        ws.update(values=all_rows, range_name=f"A1:W{len(all_rows)}")

        # ── Background thread for formatting & Forward Test tab so START is instant ──
        def _setup_sheet_bg():
            try:
                from gspread_formatting import format_cell_ranges, CellFormat, Color, TextFormat, set_frozen
                banner_formats = [
                    ("A1:W1", CellFormat(backgroundColor=Color(0.05, 0.07, 0.11), textFormat=TextFormat(foregroundColor=Color(1, 0.84, 0), bold=True, fontSize=12), horizontalAlignment="LEFT")),
                    ("A2:W2", CellFormat(backgroundColor=Color(0.04, 0.18, 0.20), textFormat=TextFormat(foregroundColor=Color(0.3, 0.9, 0.8), bold=True, fontSize=10), horizontalAlignment="LEFT")),
                    ("A3:W3", CellFormat(backgroundColor=Color(0.12, 0.08, 0.24), textFormat=TextFormat(foregroundColor=Color(0.8, 0.6, 1.0), bold=True, fontSize=10), horizontalAlignment="LEFT")),
                    ("A4:W4", CellFormat(backgroundColor=Color(0.06, 0.14, 0.24), textFormat=TextFormat(foregroundColor=Color(0.48, 0.75, 1.0), bold=True, fontSize=10), horizontalAlignment="LEFT")),
                    ("A5:W5", CellFormat(backgroundColor=Color(0.08, 0.12, 0.08), textFormat=TextFormat(foregroundColor=Color(0.7, 1.0, 0.7), bold=True, fontSize=10), horizontalAlignment="LEFT")),
                    ("A7:W7", CellFormat(backgroundColor=Color(0.05, 0.22, 0.10), textFormat=TextFormat(foregroundColor=Color(0.4, 1.0, 0.5), bold=True, fontSize=10), horizontalAlignment="LEFT")),
                    ("A8:W8", CellFormat(backgroundColor=Color(0.13, 0.17, 0.23), textFormat=TextFormat(foregroundColor=Color(1, 1, 1), bold=True), horizontalAlignment="CENTER")),
                ]
                format_cell_ranges(ws, banner_formats)
                print("[BG] Header formatting applied (1 batch request)")

                # Forward Test sheet setup
                ft_ws = None
                for s in spreadsheet.worksheets():
                    if s.title == "Forward Test":
                        ft_ws = s
                        break
                if not ft_ws:
                    ft_ws = spreadsheet.add_worksheet(title="Forward Test", rows=350, cols=20)
                elif ft_ws.col_count < 20:
                    try:
                        ft_ws.add_cols(20 - ft_ws.col_count)
                    except Exception:
                        pass

                try:
                    set_frozen(ft_ws, rows=2)
                except Exception:
                    pass

                # Dropdown validation on B2
                try:
                    req = {
                        "setDataValidation": {
                            "range": {"sheetId": ft_ws.id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 2},
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": "ALL"},
                                        {"userEnteredValue": "15M Intraday"},
                                        {"userEnteredValue": "1H Structure"},
                                    ],
                                },
                                "inputMessage": "Select Timeframe to filter Forward Test trades (ALL, 15M, 1H)",
                                "showCustomUi": True,
                                "strict": False,
                            },
                        }
                    }
                    spreadsheet.batch_update({"requests": [req]})
                except Exception as dverr:
                    print(f"Data validation setup note: {dverr}")

                self.forward_test_sheet = ft_ws
                try:
                    dp_ws = None
                    for s in spreadsheet.worksheets():
                        if s.title == "Daily PnL":
                            dp_ws = s
                            break
                    if not dp_ws:
                        dp_ws = spreadsheet.add_worksheet(title="Daily PnL", rows=400, cols=len(DAILY_PNL_HEADERS))
                    self.daily_pnl_sheet = dp_ws
                    self.side_sheets = {}
                    for _side in ("BUY", "SELL"):
                        _title = f"{_side} Trades"
                        _ws = next((x for x in spreadsheet.worksheets() if x.title == _title), None)
                        if not _ws:
                            _ws = spreadsheet.add_worksheet(title=_title, rows=600, cols=len(SIDE_SHEET_HEADERS))
                        self.side_sheets[_side] = _ws
                except Exception as dperr:
                    print(f"[DAILY PNL] sheet setup note: {dperr}")
                self.sync_forward_test_to_sheet()
                self.backfill_abandoned_async()

                # Orders Log tab: mirror of live_orders_log.csv (rewritten from the CSV at start, then appended live)
                try:
                    ord_ws = None
                    for s in spreadsheet.worksheets():
                        if s.title == "Orders Log":
                            ord_ws = s
                            break
                    if not ord_ws:
                        ord_ws = spreadsheet.add_worksheet(title="Orders Log", rows=1000, cols=12)
                    rows = []
                    try:
                        with open(ORDER_LOG_FILE, newline='') as f:
                            rows = [r for r in csv.reader(f) if r]
                    except Exception:
                        pass
                    if not rows:
                        rows = [['Timestamp', 'Mode', 'Symbol', 'Type', 'Strike', 'Token', 'Action',
                                 'Qty', 'Price', 'Order_ID', 'Status', 'Remark']]
                    with live_order_manager.sheet_lock:
                        live_order_manager.sheet_queue.clear()   # CSV already holds every queued row
                    ord_ws.clear()
                    if ord_ws.row_count < len(rows) + 200:
                        ord_ws.add_rows(len(rows) + 200 - ord_ws.row_count)
                    ord_ws.update(values=rows, range_name="A1")
                    try:
                        set_frozen(ord_ws, rows=1)
                        format_cell_ranges(ord_ws, [("A1:L1", CellFormat(
                            backgroundColor=Color(0.13, 0.17, 0.23),
                            textFormat=TextFormat(foregroundColor=Color(1, 1, 1), bold=True),
                            horizontalAlignment="CENTER"))])
                    except Exception:
                        pass
                    self.orders_sheet = ord_ws
                    print(f"✅ Orders Log worksheet ready ({len(rows) - 1} rows)")
                except Exception as ord_err:
                    print(f"[Orders Log] setup error: {ord_err}")
                print("✅ Forward Test worksheet ready with Timeframe Filter")

                # Multi-Symbol Tracker sheet setup removed as requested
                self.multi_symbol_sheet = None
            except Exception as bg_err:
                print(f"[BG Setup Error] {bg_err}")

        threading.Thread(target=_setup_sheet_bg, daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────
    # MULTI-SYMBOL TRACKER & FORWARD TESTING ENGINE
    # ────────────────────────────────────────────────────────────────────────
    def _load_calculated_symbols_cache(self):
        try:
            if os.path.exists(MULTI_LEVELS_FILE):
                with open(MULTI_LEVELS_FILE, "r") as f:
                    self.calculated_symbols_cache = json.load(f)
        except Exception:
            self.calculated_symbols_cache = {}

    def _save_calculated_symbols_cache(self):
        try:
            with open(MULTI_LEVELS_FILE, "w") as f:
                json.dump(self.calculated_symbols_cache, f, indent=2)
        except Exception:
            pass

    def record_calculated_symbol(self, sym: str, exch: str, spot: float, step: int, lot: int,
                                 expiry: str, mtf_pivots: dict, focus: dict, pipeline_rows: list = None):
        """Records any calculated symbol and its levels into cache and syncs to Multi-Symbol Tracker sheet."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.calculated_symbols_cache[sym] = {
            "symbol": sym,
            "exchange": exch,
            "spot": spot,
            "step": step,
            "lot_size": lot,
            "expiry": expiry,
            "calculated_at": now_str,
            "mtf_pivots": mtf_pivots,
            "focus": focus,
            "pipeline_rows": pipeline_rows or []
        }
        self._save_calculated_symbols_cache()
        threading.Thread(target=self.sync_multi_symbol_sheet, daemon=True).start()

    def sync_multi_symbol_sheet(self):
        """Multi-Symbol sheet removed — no-op."""
        return

    def _sync_multi_sheet_bg(self):
        return

    def _do_sync_multi_symbol_sheet(self):
        """Multi-Symbol sheet removed — no-op."""
        return

    # ────────────────────────────────────────────────────────────────────────
    # FORWARD TEST DASHBOARD — Google Sheets redesign with TIMEFRAME FILTER
    # Layout:
    #   Row 1   : Dashboard summary bar + Multi-timeframe PnL breakdown
    #   Row 2   : 🎯 Interactive TIMEFRAME FILTER toolbar (cell B2 dropdown)
    #   Row 3   : 🟡 ACTIVE TRADES section header (filtered)
    #   Row 4   : Column headers (including Timeframe)
    #   Row 5..n: Active trade rows (live PnL injected from memory)
    #   Row n+1 : Blank spacer
    #   Row n+2 : ✅ CLOSED TRADES section header (filtered)
    #   Row n+3 : Column headers
    #   Row n+4+: Closed rows (newest first, PnL color-coded)
    # ────────────────────────────────────────────────────────────────────────
    def sync_orders_to_sheet(self):
        """Append queued order-log rows to the "Orders Log" tab (background, throttled to 5s)."""
        if not self.orders_sheet or self._orders_sync_busy:
            return
        now_ts = time.time()
        if now_ts < getattr(self, '_quota_backoff_until', 0.0) or now_ts - self._last_orders_sync_ts < 5.0:
            return
        with live_order_manager.sheet_lock:
            if not live_order_manager.sheet_queue:
                return
            rows = list(live_order_manager.sheet_queue)
            live_order_manager.sheet_queue.clear()
        self._last_orders_sync_ts = now_ts
        self._orders_sync_busy = True

        def _bg():
            try:
                self.orders_sheet.append_rows(rows, value_input_option="USER_ENTERED")
            except Exception as e:
                print(f"[Orders Log] append failed, will retry: {e}")
                with live_order_manager.sheet_lock:
                    live_order_manager.sheet_queue[:0] = rows
                if "429" in str(e):
                    self._quota_backoff_until = time.time() + 60
            finally:
                self._orders_sync_busy = False
        threading.Thread(target=_bg, daemon=True).start()

    def backfill_abandoned_async(self, delay=0):
        """Finish trades left ABANDONED by an app restart from Dhan 1-minute candles (background)."""
        if getattr(self, "_backfill_running", False):
            return
        self._backfill_running = True

        def _run():
            try:
                if delay:
                    time.sleep(delay)
                done = self.forward_tracker.backfill_abandoned(dhan_minute_bars)
                if done:
                    self._last_ft_sync_ts = 0.0
                    self.sync_forward_test_to_sheet()
            except Exception as e:
                print(f"[FWD TEST] restart backfill note: {e}")
            finally:
                self._backfill_running = False
        threading.Thread(target=_run, daemon=True).start()

    def sync_forward_test_to_sheet(self):
        """
        Syncs forward test trades to the dashboard sheet with TIMEFRAME FILTER.
        Runs in a background thread — never blocks the main tick loop.
        Throttled to at most once per 8s to prevent Google Sheets 429 quota exhaustion.
        """
        now_ts = time.time()
        if now_ts < getattr(self, '_quota_backoff_until', 0.0):
            return
        if now_ts - getattr(self, '_last_ft_sync_ts', 0.0) < 8.0:
            return
        self._last_ft_sync_ts = now_ts
        if not self.forward_test_sheet:
            return
        # Run in background thread to not block the 1s tick
        threading.Thread(target=self._sync_ft_sheet_bg, daemon=True).start()

    def _sync_ft_sheet_bg(self):
        """Background worker for sync_forward_test_to_sheet."""
        if not self._sheet_write_lock.acquire(blocking=False):
            return   # already a sheet write in progress — skip this cycle
        try:
            self._do_sync_ft_sheet()
        finally:
            self._sheet_write_lock.release()

    def _sync_daily_pnl_sheet(self, active_mem=None):
        """Date-wise BUY / SELL points on the 'Daily PnL' tab (every closed trade, all days).
        Rewritten when a trade closes, otherwise at most once a minute (open live points)."""
        ws = getattr(self, "daily_pnl_sheet", None)
        if not ws:
            return
        try:
            rows = []
            if os.path.exists(FORWARD_TEST_FILE):
                with open(FORWARD_TEST_FILE, 'r', newline='') as f:
                    rows = [r for r in csv.DictReader(f) if str(r.get('Status', '')).strip() == 'CLOSED']
            sig = len(rows)
            if sig == getattr(self, "_dp_last_sig", None) and time.time() - getattr(self, "_dp_last_ts", 0) < 60:
                return
            table = daily_pnl_table(rows, active_mem)
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            grid = ([[f"📅 DATE-WISE PnL (POINTS) — BUY vs SELL — closed forward-test trades — Updated: {now_str}"]
                     + [''] * (len(DAILY_PNL_HEADERS) - 1), DAILY_PNL_HEADERS] + table)
            if ws.row_count < len(grid) + 10:
                ws.add_rows(len(grid) + 10 - ws.row_count)
            ws.update(values=grid, range_name="A1")
            prev = getattr(self, "_dp_last_len", None)
            if prev is not None and prev > len(grid):
                ws.batch_clear([f"A{len(grid) + 1}:N{prev}"])
            self._dp_last_len = len(grid)
            for _side, _ws in (getattr(self, "side_sheets", None) or {}).items():
                _t = side_trades_table(rows, _side, active_mem)
                _pts = compute_points_summary([{"PnL_Pts": r[14]} for r in _t if r[15] == 'CLOSED'],
                                              [{"PnL_Pts": r[14]} for r in _t if r[15] == 'ACTIVE'])
                _g = ([[format_points_bar(f"{'🟢' if _side == 'BUY' else '🔴'} {_side} TRADES — ALL DAYS", _pts)
                        + f"  |  Updated: {now_str}"] + [''] * (len(SIDE_SHEET_HEADERS) - 1),
                       SIDE_SHEET_HEADERS] + _t)
                if _ws.row_count < len(_g) + 10:
                    _ws.add_rows(len(_g) + 10 - _ws.row_count)
                _ws.update(values=_g, range_name="A1")
                _prev = getattr(self, f"_side_last_len_{_side}", None)
                if _prev is not None and _prev > len(_g):
                    _ws.batch_clear([f"A{len(_g) + 1}:P{_prev}"])
                setattr(self, f"_side_last_len_{_side}", len(_g))
            self._dp_last_sig, self._dp_last_ts = sig, time.time()
        except Exception as e:
            print(f"[DAILY PNL] sheet update note: {e}")

    def _do_sync_ft_sheet(self):
        """Actual sheet sync logic (runs in background thread)."""
        if not self.forward_test_sheet:
            return
        try:
            # ── Step 0: Read B2 filter — cached for 30s to avoid extra API call ──
            now_ts = time.time()
            if now_ts - self._ft_b2_last_read > 30:
                try:
                    b2_val = self.forward_test_sheet.acell('B2').value
                    if b2_val:
                        b2_u = b2_val.strip().upper()
                        if "15" in b2_u:
                            self._ft_b2_cache = "15M Intraday"
                        elif "1H" in b2_u or "HOUR" in b2_u:
                            self._ft_b2_cache = "1H Structure"
                        elif "ALL" in b2_u:
                            self._ft_b2_cache = "ALL"
                    self._ft_b2_last_read = now_ts
                except Exception:
                    pass

            # Use GUI filter if set, otherwise use cached sheet B2 value
            current_filter = self.ft_timeframe_filter or self._ft_b2_cache or "ALL"

            # Focus strikes sets for classifying timeframes
            with self.lock:
                f15 = self.focus_15m or self.daily_focus.get("15M", {})
                f1h = self.focus_1h or self.daily_focus.get("1H", {})
            focus_15m_strikes = set(f15.get("all_4", []))
            focus_1h_strikes  = set(f1h.get("all_4", []))

            def _get_trade_tf(item):
                tf = item.get('Timeframe', '').strip()
                if tf:
                    return tf
                try:
                    stk = int(float(item.get('Strike', 0)))
                except:
                    stk = 0
                if stk in focus_15m_strikes and stk in focus_1h_strikes:
                    return "15M / 1H"
                elif stk in focus_15m_strikes:
                    return "15M Intraday"
                elif stk in focus_1h_strikes:
                    return "1H Structure"
                else:
                    return "Other"

            def _matches_filter(tf, filt):
                if not filt or filt == "ALL":
                    return True
                tf_u = (tf or "").upper()
                fl_u = filt.upper()
                if "15" in fl_u:
                    return "15M" in tf_u or "15-MIN" in tf_u
                if "1H" in fl_u or "HOUR" in fl_u:
                    return "1H" in tf_u or "HOUR" in tf_u
                return True

            # ── Step 1: Get live active trades from memory (100% fresh PnL) ──
            active_mem = self.forward_tracker.get_active_trades()  # list of dicts

            # ── Step 2: Read CSV to get CLOSED rows — TODAY ONLY ─────────────
            # Only show today's closed trades to avoid showing historical paper
            # simulation data from previous runs (avoids 800+ phantom trades).
            closed_rows_raw = []
            today_date_str = datetime.now().strftime('%Y-%m-%d')
            if os.path.exists(FORWARD_TEST_FILE):
                try:
                    with open(FORWARD_TEST_FILE, 'r', newline='') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            status = row.get('Status', '').strip()
                            row_date = row.get('Date', '').strip()
                            if status == 'CLOSED' and row_date == today_date_str:
                                closed_rows_raw.append(row)
                except Exception as csv_err:
                    print(f"CSV read error: {csv_err}")

            now_str = datetime.now().strftime('%H:%M:%S')

            def _fmt_pnl(pnl_val, suffix=" pts"):
                if pnl_val is None:
                    return "—"
                try:
                    v = float(pnl_val)
                    em = "🟢" if v >= 0 else "🔴"
                    sign = "+" if v >= 0 else ""
                    return f"{em} {sign}{v:.2f}{suffix}"
                except:
                    return str(pnl_val)

            def _active_row(t):
                live_pnl = t.get('PnL_Pts', t.get('Live_PnL', 0.0))
                cur_px   = t.get('Current_Price', t.get('Entry_Price', 0))
                tf       = _get_trade_tf(t)
                action   = t.get('Action', 'BUY')
                entry    = _safe_float(t.get('Entry_Price', 0))
                t_high   = _safe_float(t.get('Trade_High', entry))
                t_low    = _safe_float(t.get('Trade_Low', entry))

                if action == 'BUY':
                    hl_str  = f"H: {t_high:.2f}" if t_high > 0 else "-"
                    max_pts = round(max(0.0, t_high - entry), 2)
                else:
                    hl_str  = f"L: {t_low:.2f}" if t_low > 0 else "-"
                    max_pts = round(max(0.0, entry - t_low), 2)
                hh_str = f"{t_high:.2f}" if t_high > 0 else "-"
                ll_str = f"{t_low:.2f}" if t_low > 0 else "-"
                max_str = f"🔥 +{max_pts:.2f}" if max_pts > 0 else "0.00"

                return [
                    t.get('Date', ''),
                    t.get('Entry_Time', ''),
                    tf,
                    t.get('Symbol', ''),
                    t.get('Type', ''),
                    t.get('Strike', ''),
                    action,
                    entry,
                    round(float(cur_px), 2) if cur_px else '',
                    _fmt_pnl(live_pnl),
                    hh_str,       # Highest price since entry (HH after entry)
                    ll_str,       # Lowest price since entry (LL after entry)
                    max_str,      # Max Result points
                    (t.get('Initial_SL') or t.get('SL', 0)),
                    t.get('Target_1', 0),
                    t.get('Target_2', 0),
                    t.get('Target_3', 0),
                    t.get('Target_4', 0),
                    t.get('Target_5', 0),
                    (f"🎯 T{t.get('Target_Hit', 0)} Hit — SL {t.get('SL', 0)}" if t.get('Target_Hit', 0) > 0
                     else f"⚡ LIVE — SL {t.get('SL', 0)} / T1 {t.get('Target_1', 0)}"),
                    '🟡 ACTIVE',
                    t.get('Trade_ID', ''),
                ]

            def _closed_row(r):
                try:
                    pnl_val = float(r.get('PnL_Pts', 0))
                except:
                    pnl_val = 0.0
                exit_reason = r.get('Exit_Reason', '')
                win_icon = '✅' if pnl_val >= 0 else '🛑'
                tf = _get_trade_tf(r)
                action = r.get('Action', 'BUY')
                entry  = _safe_float(r.get('Entry_Price', 0))
                exit_p = _safe_float(r.get('Exit_Price', entry))

                t_high = _safe_float(r.get('Trade_High', 0))
                t_low  = _safe_float(r.get('Trade_Low', 0))

                if action == 'BUY':
                    if t_high == 0:
                        t_high = max(entry, exit_p, _safe_float(r.get('Target_1', 0)) if 'Target' in exit_reason else entry)
                    hl_str  = f"H: {t_high:.2f}"
                    max_pts = round(max(0.0, t_high - entry, pnl_val), 2)
                else:
                    if t_low == 0:
                        t_low = min(entry, exit_p, _safe_float(r.get('Target_1', entry)) if 'Target' in exit_reason else entry)
                    hl_str  = f"L: {t_low:.2f}"
                    max_pts = round(max(0.0, entry - t_low, pnl_val), 2)

                max_str = f"🔥 +{max_pts:.2f}" if max_pts > 0 else "0.00"
                hh_c = t_high if t_high > 0 else max(entry, exit_p)
                ll_c = t_low if t_low > 0 else min(entry, exit_p)

                return [
                    r.get('Date', ''),
                    r.get('Entry_Time', ''),
                    tf,
                    r.get('Symbol', ''),
                    r.get('Type', ''),
                    r.get('Strike', ''),
                    action,
                    entry,
                    r.get('Exit_Price', ''),
                    _fmt_pnl(pnl_val),
                    f"{hh_c:.2f}",   # HH after entry
                    f"{ll_c:.2f}",   # LL after entry
                    max_str,      # Max Result points
                    r.get('SL', 0),
                    r.get('Target_1', 0),
                    r.get('Target_2', 0),
                    r.get('Target_3', 0),
                    r.get('Target_4', 0),
                    r.get('Target_5', 0),
                    exit_reason,
                    f"{win_icon} CLOSED",
                    r.get('Trade_ID', ''),
                ]

            all_active_rows = [_active_row(t) for t in active_mem]
            all_closed_rows = [_closed_row(r) for r in reversed(closed_rows_raw)]  # newest first

            # ── Filter rows by selected Timeframe ───────────────────────────
            active_sheet_rows = [r for r in all_active_rows if _matches_filter(r[2], current_filter)]
            closed_sheet_rows = [r for r in all_closed_rows if _matches_filter(r[2], current_filter)]

            # ── Step 4: Statistics Breakdown by Timeframe & Max Results ─────
            def _calc_max_pts(row):
                act = row.get('Action', 'BUY')
                ent = _safe_float(row.get('Entry_Price', 0))
                th  = _safe_float(row.get('Trade_High', 0))
                tl  = _safe_float(row.get('Trade_Low', 0))
                pnl = _safe_float(row.get('PnL_Pts', 0))
                if act == 'BUY':
                    return max(0.0, th - ent, pnl) if th > 0 else max(0.0, pnl)
                else:
                    return max(0.0, ent - tl, pnl) if tl > 0 else max(0.0, pnl)

            net_pnl_all       = sum(_safe_float(r.get('PnL_Pts', 0)) for r in closed_rows_raw)
            total_max_pts_all = sum(_calc_max_pts(r) for r in closed_rows_raw)
            live_unreal_all   = sum(t.get('PnL_Pts', t.get('Live_PnL', 0.0)) for t in active_mem)

            pnl_15m = sum(_safe_float(r.get('PnL_Pts', 0)) for r in closed_rows_raw if _matches_filter(_get_trade_tf(r), "15M Intraday"))
            pnl_1h  = sum(_safe_float(r.get('PnL_Pts', 0)) for r in closed_rows_raw if _matches_filter(_get_trade_tf(r), "1H Structure"))

            filt_closed_cnt = len(closed_sheet_rows)
            filt_active_cnt = len(active_sheet_rows)
            filt_wins       = sum(1 for r in closed_sheet_rows if str(r[9]).startswith('🟢'))
            filt_losses     = filt_closed_cnt - filt_wins
            filt_net_pnl    = sum(_safe_float(r.get('PnL_Pts', 0)) for r in closed_rows_raw if _matches_filter(_get_trade_tf(r), current_filter))
            filt_max_pts    = sum(_calc_max_pts(r) for r in closed_rows_raw if _matches_filter(_get_trade_tf(r), current_filter))
            filt_live_pnl   = sum(t.get('PnL_Pts', t.get('Live_PnL', 0.0)) for t in active_mem if _matches_filter(_get_trade_tf(t), current_filter))

            col_headers = [
                'Date', 'Entry_Time', 'Timeframe', 'Symbol', 'Type', 'Strike', 'Action',
                'Entry_Price', 'Exit/Current_Px', 'PnL_Pts',
                'HH After Entry', 'LL After Entry', 'Max Result (Pts)',
                'SL', 'T1', 'T2', 'T3', 'T4', 'T5',
                'Reason', 'Status', 'Trade_ID'
            ]

            # ── Step 5: Assemble the full grid ──────────────────────────────
            pnl_icon = "🟢" if filt_net_pnl >= 0 else "🔴"
            live_icon = "🟢" if filt_live_pnl >= 0 else "🔴"

            pts_today = compute_points_summary(closed_rows_raw, active_mem)
            dash_row = [
                format_points_bar("📊 FORWARD TEST (TODAY) ± POINTS", pts_today) + "  |  "
                f"Max Potential: 🔥 {total_max_pts_all:+.2f} pts  |  "
                f"15M: {pnl_15m:+.2f}  |  1H: {pnl_1h:+.2f}  |  "
                f"Filter: {current_filter}  |  Updated: {now_str}"
            ] + [''] * 20

            filter_bar = [
                "🎯 TIMEFRAME FILTER:",
                current_filter,
                "👈 Click/Select dropdown in B2 (ALL | 15M Intraday | 1H Structure)",
                f"Showing: {filt_active_cnt} Active, {filt_closed_cnt} Closed",
                f"Wins: {filt_wins}  Losses: {filt_losses}",
                f"Filtered Closed PnL: {pnl_icon} {filt_net_pnl:+.2f} pts",
                f"Filtered Max Result: 🔥 +{filt_max_pts:+.2f} pts",
                f"Filtered Live: {live_icon} {filt_live_pnl:+.2f} pts"
            ] + [''] * 10

            active_hdr = [f"🟡 ACTIVE TRADES ({filt_active_cnt}) — [Timeframe: {current_filter}] — Live PnL updates every ~6s"] + [''] * 20
            closed_hdr = [f"✅ CLOSED TRADES ({filt_closed_cnt}) — [Timeframe: {current_filter}] — Newest First"] + [''] * 20

            grid = (
                [dash_row, filter_bar]
                + [active_hdr]
                + [col_headers]
                + (active_sheet_rows if active_sheet_rows else [['— No active trades matching timeframe filter —'] + [''] * 20])
                + [[''] * 21]
                + [closed_hdr]
                + [col_headers]
                + (closed_sheet_rows if closed_sheet_rows else [['— No closed trades matching timeframe filter —'] + [''] * 20])
            )

            n_cols = 23
            col_letter = chr(ord('A') + n_cols - 1)   # 'W'
            self.forward_test_sheet.update(
                values=grid,
                range_name=f"A1:{col_letter}{len(grid)}"
            )
            # Wipe any rows left below the grid (old/deleted trades must not linger in the sheet)
            _prev_len = getattr(self, "_ft_last_grid_len", None)
            if _prev_len is None or _prev_len > len(grid):
                try:
                    _last_row = max(self.forward_test_sheet.row_count, len(grid) + 1)
                    self.forward_test_sheet.batch_clear([f"A{len(grid) + 1}:{col_letter}{_last_row}"])
                except Exception as _clr_err:
                    print(f"[FT SHEET] clear-below note: {_clr_err}")
            self._ft_last_grid_len = len(grid)
            self._sync_daily_pnl_sheet(active_mem)

            # ── Step 6: Apply colour formatting (atomic batch call to preserve quota) ──
            try:
                from gspread_formatting import format_cell_ranges, CellFormat, Color, TextFormat

                # Only format once per filter selection AND at most every 300 seconds
                # to avoid exceeding Google Sheets 429 quota (write limit: 60 req/min)
                _last_fmt_filter = getattr(self, '_last_ft_formatted_filter', None)
                _last_fmt_ts     = getattr(self, '_last_ft_format_ts', 0)
                _fmt_stale       = (time.time() - _last_fmt_ts) > 300
                if _last_fmt_filter != current_filter or _fmt_stale:
                    formats = [
                        (f"A1:{col_letter}1", CellFormat(
                            backgroundColor=Color(0.04, 0.08, 0.16),
                            textFormat=TextFormat(foregroundColor=Color(1.0, 0.84, 0.0),
                                                  bold=True, fontSize=10)
                        )),
                        (f"A2:{col_letter}2", CellFormat(
                            backgroundColor=Color(0.06, 0.12, 0.20),
                            textFormat=TextFormat(foregroundColor=Color(0.65, 0.85, 1.0), fontSize=9)
                        )),
                        ("A2:A2", CellFormat(
                            backgroundColor=Color(0.06, 0.12, 0.20),
                            textFormat=TextFormat(foregroundColor=Color(1.0, 0.84, 0.0), bold=True, fontSize=9)
                        )),
                        ("B2:B2", CellFormat(
                            backgroundColor=Color(0.12, 0.25, 0.40),
                            textFormat=TextFormat(foregroundColor=Color(0.2, 1.0, 0.6), bold=True, fontSize=10)
                        )),
                        (f"A3:{col_letter}3", CellFormat(
                            backgroundColor=Color(0.07, 0.20, 0.09),
                            textFormat=TextFormat(foregroundColor=Color(0.4, 1.0, 0.5), bold=True)
                        )),
                        (f"A4:{col_letter}4", CellFormat(
                            backgroundColor=Color(0.10, 0.16, 0.10),
                            textFormat=TextFormat(foregroundColor=Color(0.8, 1.0, 0.8), bold=True)
                        )),
                    ]

                    # Add closed headers
                    closed_hdr_r = 5 + len(active_sheet_rows) + 2
                    closed_col_r = closed_hdr_r + 1
                    formats.append((f"A{closed_hdr_r}:{col_letter}{closed_hdr_r}", CellFormat(
                        backgroundColor=Color(0.16, 0.08, 0.04),
                        textFormat=TextFormat(foregroundColor=Color(1.0, 0.75, 0.3), bold=True)
                    )))
                    formats.append((f"A{closed_col_r}:{col_letter}{closed_col_r}", CellFormat(
                        backgroundColor=Color(0.14, 0.10, 0.06),
                        textFormat=TextFormat(foregroundColor=Color(1.0, 0.9, 0.6), bold=True)
                    )))

                    # Send all formats in 1 single API call
                    format_cell_ranges(self.forward_test_sheet, formats)
                    self._last_ft_formatted_filter = current_filter
                    self._last_ft_format_ts        = time.time()

            except Exception as fmt_err:
                print(f"FT sheet format error: {fmt_err}")

        except Exception as e:
            if "429" in str(e) or "Quota" in str(e):
                self._quota_backoff_until = time.time() + 60.0
                print("[SHEET] Google Sheets 429 quota limit hit — pausing sheet updates for 60s (Local GUI & trading run unaffected)")
            else:
                print(f"Forward test sync error: {e}")

    def update_sheet_loop(self, symbol, strikes, status_cb=None, risk_free=0.065):
        """Continuously push live option chain and Fibonacci Pivot data to Google Sheets."""
        sorted_strikes = sorted(strikes)
        n              = len(sorted_strikes)
        strike_to_idx  = {s: i for i, s in enumerate(sorted_strikes)}

        # Grid row indices for strikes (0-based inside strike_rows list)
        # Column mapping (28 columns total):
        # 0: CE Signal, 1: CE S3, 2: CE S2, 3: CE S1, 4: CE Pivot, 5: CE R1, 6: CE R2, 7: CE R3, 8: CE LTP, 9: CE OI, 10: CE Vol, 11: CE IV, 12: CE Delta
        # 13: STRIKE
        # 14: PE Delta, 15: PE IV, 16: PE Vol, 17: PE OI, 18: PE LTP, 19: PE R3, 20: PE R2, 21: PE R1, 22: PE Pivot, 23: PE S1, 24: PE S2, 25: PE S3, 26: PE Signal
        # 27: Updated

        self.running = True
        loop = 0
        today_focus_date        = None
        # ── Next-day plan: loaded from eod_planner.py JSON (display-only, no logic) ──
        _nd_plan_date  = None
        _nd_plan_pivots = {}    # {"S2":x,"S1":x,"P":x,"R1":x,"R2":x} from saved plan   # reset focus strikes each new day
        _mkt_open_refresh_date  = None   # track if 09:16 AM refresh already done today

        while self.running:
            try:
                with self.lock:
                    snapshot = dict(self.live_data)
                    spot     = self.spot_price.get(symbol, 0)
                    sp_piv   = self.spot_pivots.get(symbol)
                    sp_zone  = self.spot_zone.get(symbol, "Monitoring")

                # ── Daily 4-strike focus selection (PDF Section 3 & 4) ──────────
                # Refreshes on:
                #   (a) New calendar date (first loop of the day / app restart)
                #   (b) 09:16 AM sharp — ensures fresh previous-day OHLC after market open
                now_dt       = datetime.now()
                today_date   = now_dt.strftime("%Y-%m-%d")
                _market_open = False   # levels are locked for the session — no 09:16 re-calculation
                _need_refresh = (
                    today_date != today_focus_date                           # new day
                    or (_market_open and _mkt_open_refresh_date != today_date)  # 09:16 AM trigger
                )

                if _need_refresh:
                    try:
                        print(f"[PIVOT] Fetching multi-timeframe pivots @ {now_dt.strftime('%H:%M:%S')} (TradingView alignment)...")
                        mtf = get_tv_multi_timeframe_pivots(symbol)
                        step = get_symbol_step(symbol, getattr(self, "exchange", "NFO"))
                        # Retain existing focus if already computed for today (stops 1H strikes from shifting)
                        same_day = (self.daily_focus and self._focus_date == today_date
                                    and getattr(self, "_focus_symbol", symbol) == symbol)
                        if same_day:
                            focus = self.daily_focus
                        else:
                            focus = select_daily_4_focus_strikes(mtf, step)
                        with self.lock:
                            self.mtf_pivots     = mtf
                            self.daily_focus    = focus
                            self.focus_15m      = focus.get("15M", {})
                            self.focus_1h       = focus.get("1H", {})
                            self._focus_date    = today_date
                            self._focus_symbol  = symbol
                            self._alert_strikes = set()
                        # New day while running: subscribe the new pivot strikes
                        new_set = {x for x in focus.get("all_focus", []) if x > 0}
                        subscribed = {m["strike"] for m in self.token_to_meta.values()}
                        if (not same_day and new_set and not new_set.issubset(subscribed)
                                and getattr(self, "_prep_args", None)):
                            try:
                                print(f"[PIVOT] New session strikes {sorted(new_set)} — re-subscribing WebSocket")
                                self.prepare(*self._prep_args)
                                self._schedule_reconnect(reason="new session pivot strikes")
                            except Exception as rp_err:
                                print(f"[PIVOT] Re-subscribe error: {rp_err}")
                        today_focus_date       = today_date
                        if _market_open:
                            _mkt_open_refresh_date = today_date
                            print(f"[PIVOT] ✅ 09:16 AM market-open pivot refresh complete for {symbol}")
                            trigger_immediate_alert(
                                f"📐 {symbol} Pivots Refreshed @ 09:16 AM",
                                f"New pivots applied: P={mtf.get('15-Min', {}).get('P','-')} | R1={mtf.get('15-Min', {}).get('R1','-')} R2={mtf.get('15-Min', {}).get('R2','-')} | S1={mtf.get('15-Min', {}).get('S1','-')} S2={mtf.get('15-Min', {}).get('S2','-')}",
                                sound=True
                            )
                        f15 = focus.get("15M", {})
                        f1h = focus.get("1H", {})
                        print(f"[PIVOT] 15M Focus: CE1={f15.get('CE1')} CE2={f15.get('CE2')} | P={'/'.join(str(x) for x in f15.get('p_strikes', [])) or f15.get('P_STRIKE')} | PE1={f15.get('PE1')} PE2={f15.get('PE2')}")
                        print(f"[PIVOT] 1H Focus:  CE1={f1h.get('CE1')} CE2={f1h.get('CE2')} | P={'/'.join(str(x) for x in f1h.get('p_strikes', [])) or f1h.get('P_STRIKE')} | PE1={f1h.get('PE1')} PE2={f1h.get('PE2')}")
                        focus_msg = (
                            f"15M Focus: CE {f15.get('CE1')},{f15.get('CE2')} | PE {f15.get('PE1')},{f15.get('PE2')} -- "
                            f"1H: CE {f1h.get('CE1')},{f1h.get('CE2')} | PE {f1h.get('PE1')},{f1h.get('PE2')}"
                        )
                        if not _market_open and is_live_market_open(now_dt):  # avoid double alert at 09:16 and only in live market
                            trigger_immediate_alert(f"📌 {symbol} 15M & 1H Focus Strikes", focus_msg, sound=False)
                    except Exception as fe:
                        print(f"[PIVOT] Daily focus error: {fe}")

                # ── Load next-day plan once per day (display-only filter column) ──
                if today_date != _nd_plan_date:
                    _nd_plan_date = today_date
                    try:
                        nd_p = load_next_day_plan(symbol)
                        if nd_p:
                            self.next_day_plan[symbol] = nd_p
                            _nd_plan_pivots = nd_p.get("levels_15m") or nd_p.get("pivots") or {}
                            print(f"[PLAN] ✅ Loaded next-day plan for {symbol}: S2={_nd_plan_pivots.get('S2',0):.1f} "
                                  f"P={_nd_plan_pivots.get('P',0):.1f} R2={_nd_plan_pivots.get('R2',0):.1f} ({len(nd_p.get('setups',[]))} setups)")
                        else:
                            _nd_plan_pivots = {}
                    except Exception as _p_err:
                        print(f"[PLAN] Plan loading note: {_p_err}")
                        _nd_plan_pivots = {}

                with self.lock:
                    daily_focus  = dict(self.daily_focus)
                    focus_15m    = dict(self.focus_15m or daily_focus.get("15M", {}))
                    focus_1h     = dict(self.focus_1h or daily_focus.get("1H", {}))
                    focus_all_4  = set(daily_focus.get("all_focus", daily_focus.get("all_4", [])))
                    mtf          = dict(self.mtf_pivots)
                    prev_spot    = self.spot_prev_price.get(symbol, 0)

                # ── Market Direction (Daily P rule) & 15M / 1H Zones ──────────────────────────
                daily_piv_mtf = mtf.get("Daily", {})
                h1_piv_mtf    = mtf.get("1-Hour", {})
                piv_15m_mtf   = mtf.get("15-Min", {})
                mkt_dir  = get_market_direction(spot, piv_15m_mtf or daily_piv_mtf, prev_spot)
                h1_trade = get_1h_trade_bias(spot, h1_piv_mtf)
                z15m     = get_pivot_zone_detail(spot, piv_15m_mtf).get("zone", "Monitoring") if piv_15m_mtf and spot > 0 else "Monitoring"
                z1h      = get_pivot_zone_detail(spot, h1_piv_mtf).get("zone", "Monitoring") if h1_piv_mtf and spot > 0 else "Monitoring"

                with self.lock:
                    self.market_direction[symbol] = mkt_dir
                    self.h1_bias[symbol]          = h1_trade

                # Reversal pairing per timeframe (2 CE + 2 PE each):
                #   CE on support  -> BUY CE @S2  + SELL PE @R2
                #   CE at resist.  -> SELL CE @R2 + BUY PE @S2
                _ce15 = [s for s in focus_15m.get("ce_strikes", [focus_15m.get("CE1"), focus_15m.get("CE2")]) if s][:2]
                _pe15 = [s for s in focus_15m.get("pe_strikes", [focus_15m.get("PE1"), focus_15m.get("PE2")]) if s][:2]
                _ce1h = [s for s in focus_1h.get("ce_strikes", [focus_1h.get("CE1"), focus_1h.get("CE2")]) if s][:2]
                _pe1h = [s for s in focus_1h.get("pe_strikes", [focus_1h.get("PE1"), focus_1h.get("PE2")]) if s][:2]
                ce_sup_15m = self.compute_ce_on_support(_ce15)
                ce_sup_1h  = self.compute_ce_on_support(_ce1h) if _ce1h else ce_sup_15m

                def _ce_on_support_for(ot, stk):
                    if (ot == "CE" and stk in _ce15) or (ot == "PE" and stk in _pe15):
                        return ce_sup_15m
                    if (ot == "CE" and stk in _ce1h) or (ot == "PE" and stk in _pe1h):
                        return ce_sup_1h
                    return ce_sup_15m
                ce_on_support = ce_sup_15m

                # ── MTF zone map (all timeframes) ─────────────────────────
                mtf_zones = {}
                for tf, tp in mtf.items():
                    mtf_zones[tf] = get_pivot_zone_detail(spot, tp)

                # Time to expiry (years)
                try:
                    exp_str = list(self.token_to_meta.values())[0]["expiry"]
                    exp_dt  = datetime.strptime(exp_str, "%d%b%Y").replace(hour=15, minute=30)
                    T       = max((exp_dt - datetime.now()).total_seconds() / (365.25*24*3600), 1e-6)
                except:
                    T = 30/365

                # OI tracking — key on (strike, expiry) to avoid dual-expiry overwrite
                oi_by_strike_exp = {}   # {(strike, expiry): {"CE":oi, "PE":oi}}
                for token, meta in self.token_to_meta.items():
                    if token not in snapshot: continue
                    s   = meta["strike"]
                    ot  = meta["opt_type"]
                    exp = meta.get("expiry", "-")
                    key = (s, exp)
                    oi_by_strike_exp.setdefault(key, {"CE": 0, "PE": 0})
                    oi_by_strike_exp[key][ot] = snapshot[token]["oi"]

                # Collapse to plain {strike: {CE/PE}} per expiry for max-OI header
                # (use primary expiry = first expiry loaded)
                oi_by_strike = {}   # legacy: needed for _refresh_display compat
                oi_by_exp    = {}   # {expiry: {strike: {CE, PE}}}
                for (s, exp), vals in oi_by_strike_exp.items():
                    oi_by_strike.setdefault(s, {"CE": 0, "PE": 0})
                    # accumulate — CE/PE from the expiry with higher OI wins
                    for ot in ("CE", "PE"):
                        if vals[ot] > oi_by_strike[s][ot]:
                            oi_by_strike[s][ot] = vals[ot]
                    oi_by_exp.setdefault(exp, {})
                    oi_by_exp[exp].setdefault(s, {"CE": 0, "PE": 0})
                    for ot in ("CE", "PE"):
                        oi_by_exp[exp][s][ot] = max(oi_by_exp[exp][s][ot], vals[ot])

                max_ce_oi = max((v["CE"] for v in oi_by_strike.values()), default=0)
                max_pe_oi = max((v["PE"] for v in oi_by_strike.values()), default=0)
                self.oi_data = {"max_ce_oi": max_ce_oi, "max_pe_oi": max_pe_oi,
                                "oi_by_strike": oi_by_strike,
                                "oi_by_exp": oi_by_exp}

                S       = spot if spot > 0 else (min(strikes)+max(strikes))//2
                now_str = datetime.now().strftime("%H:%M:%S")

                active_alerts = []
                all_watch     = []
                ce_sup_watch  = []
                pe_res_watch  = []

                # Fill grid from live snapshot (or strike_pivots cache when market is closed)
                for token, meta in self.token_to_meta.items():
                    strike   = meta["strike"]
                    opt_type = meta["opt_type"]
                    idx      = strike_to_idx.get(strike)

                    d = snapshot.get(token, {})
                    if token in snapshot:
                        ltp       = d.get("ltp", 0.0)
                        opt_high  = d.get("high", ltp)
                        opt_low   = d.get("low", ltp)
                        opt_close = d.get("close", ltp)
                    else:
                        sp = self.strike_pivots.get(token)
                        if sp and sp.get("prev_c", 0) > 0:
                            ltp       = sp.get("prev_c", 0.0)
                            opt_high  = sp.get("prev_h", ltp)
                            opt_low   = sp.get("prev_l", ltp)
                            opt_close = ltp
                        else:
                            continue

                    if opt_high <= 0: opt_high = ltp
                    if opt_low <= 0:  opt_low  = ltp
                    if opt_close <= 0: opt_close = ltp

                    # Option Fibonacci Pivots (P, S1-S3, R1-R3) — TradingView Aligned
                    strike_piv = (self.strike_pivots.get(token)
                                  or (self.strike_pivots.get((opt_type, strike, meta.get("expiry"))) if meta.get("expiry") else None))
                    if not strike_piv:
                        # No official-candle levels for this strike yet: never make them up from
                        # live prices; the strike shows once the mapper has filled it.
                        continue
                    opt_piv = strike_piv

                    # Level label for display column
                    level_info  = check_option_levels(ltp, opt_piv, opt_type)
                    signal_text = level_info["signal"]

                    # ── Dual State Machine tracking (BUY on S2/S1, SELL on R2/R1) ──
                    exp = meta.get("expiry", "")
                    with self.lock:
                        buy_key  = (opt_type, strike, exp, "BUY") if exp else (opt_type, strike, "BUY")
                        sell_key = (opt_type, strike, exp, "SELL") if exp else (opt_type, strike, "SELL")
                        st_buy   = self.strategy_states.get(buy_key)
                        if not st_buy:
                            st_buy = StrategyStrikeState(opt_type, strike, expiry=exp, mode="BUY")
                            self.forward_tracker.seed_state(st_buy, symbol)
                            self.strategy_states[buy_key] = st_buy
                        st_sell  = self.strategy_states.get(sell_key)
                        if not st_sell:
                            st_sell = StrategyStrikeState(opt_type, strike, expiry=exp, mode="SELL")
                            self.forward_tracker.seed_state(st_sell, symbol)
                            self.strategy_states[sell_key] = st_sell

                        legacy_key = (opt_type, strike, exp) if exp else (opt_type, strike)
                        self.strategy_states[legacy_key] = st_buy if st_buy.state != "WAITING" else (st_sell if st_sell.state != "WAITING" else st_buy)
                        if (opt_type, strike) not in self.strategy_states:
                            self.strategy_states[(opt_type, strike)] = self.strategy_states[legacy_key]

                    now_tick = datetime.now()
                    # Setups move only on real trades from today's session, with today's
                    # traded high/low. Yesterday's close/high (what the feed shows before the
                    # first trade, or the saved candle) must never look like a sweep.
                    live_tick = bool(token in snapshot and d.get("s_live")
                                     and d.get("s_date") == now_tick.strftime("%Y-%m-%d"))
                    if live_tick:
                        sm_high = d.get("s_high", 0) or ltp
                        sm_low  = d.get("s_low", 0) or ltp
                        st_buy.update_tick(ltp, opt_piv, now_tick, opt_high=sm_high, opt_low=sm_low)
                        st_sell.update_tick(ltp, opt_piv, now_tick, opt_high=sm_high, opt_low=sm_low)

                    # Check support reclaim (BUY) and resistance rejection (SELL) entries
                    buy_entry     = check_option_entry(ltp, opt_piv, opt_type,
                                                       ll=st_buy.ll, state=st_buy.state,
                                                       level=st_buy.trigger_level,
                                                       opt_low=opt_low)
                    sell_entry    = check_option_sell_entry(ltp, opt_piv, opt_type,
                                                            hh=st_sell.hh, state=st_sell.state,
                                                            level=st_sell.trigger_level,
                                                            opt_high=opt_high)
                    detailed_zone = get_detailed_strike_zone(ltp, opt_piv)

                    # Market context tags for all 4 directions
                    sell_ctx = "Mkt Bullish ↑" if opt_type == "PE" else "Mkt Bearish ↓"
                    buy_ctx  = "Mkt Bullish ↑" if opt_type == "CE" else "Mkt Bearish ↓"

                    # Determine timeframe category for this strike setup
                    f15_focus = self.focus_15m or daily_focus.get("15M", {})
                    f1h_focus = self.focus_1h or daily_focus.get("1H", {})
                    f15_set = set(f15_focus.get("all_4", []))
                    f1h_set = set(f1h_focus.get("all_4", []))
                    if strike in f15_set and strike in f1h_set:
                        trade_tf = "15M / 1H"
                    elif strike in f15_set:
                        trade_tf = "15M Intraday"
                    elif strike in f1h_set:
                        trade_tf = "1H Structure"
                    else:
                        trade_tf = ""   # not a 15M/1H pivot strike → never traded

                    # 1H Structure confluence for active trades
                    h1_buy_action = ""
                    h1_sell_action = ""
                    if h1_trade:
                        h1_bias_dir = h1_trade.get("bias", "NEUTRAL")
                        if opt_type == "CE":
                            h1_buy_action = "🟢 1H Buy Zone: Hold for Big Targets (T3-T5)" if h1_bias_dir == "CE" else ("⚠️ 1H Resistance: Safe Exit / Book Profit" if h1_bias_dir == "PE" else "")
                            h1_sell_action = "🔴 1H Sell Zone: Hold for Big Targets (T3-T5)" if h1_bias_dir == "PE" else ("⚠️ 1H Support: Safe Exit / Book Profit" if h1_bias_dir == "CE" else "")
                        elif opt_type == "PE":
                            h1_buy_action = "🔴 1H Sell Zone: Hold for Big Targets (T3-T5)" if h1_bias_dir == "PE" else ("⚠️ 1H Support: Safe Exit / Book Profit" if h1_bias_dir == "CE" else "")
                            h1_sell_action = "🟢 1H Buy Zone: Hold for Big Targets (T3-T5)" if h1_bias_dir == "CE" else ("⚠️ 1H Resistance: Safe Exit / Book Profit" if h1_bias_dir == "PE" else "")

                    # Entries & Alerts strictly restricted to LIVE MARKET, 15M / 1H FOCUS strikes & nearby pivot strikes
                    is_15m_focus = (strike in daily_focus.get("all_15m", [])) or (strike in f15_set)
                    is_1h_focus  = (strike in daily_focus.get("all_1h", []))  or (strike in f1h_set)
                    # Only 15M and 1H setups are traded / recorded
                    is_focus_strike = bool(trade_tf) and (is_15m_focus or is_1h_focus)
                    mkt_open = is_live_market_open(now_tick)

                    # ── Evaluate SELL Entry ──
                    fp_sell = getattr(st_sell, "frozen_pivots", None) or opt_piv
                    if (sell_entry["entry"] and mkt_open and is_focus_strike and live_tick
                            and at_entry_price(ltp, st_sell.entry_price, "SELL")):
                        in_sell_zone = (fp_sell["S2"] * 0.95 <= ltp <= fp_sell["R2"] * 1.05)
                        if in_sell_zone:
                            lvl = st_sell.trigger_level
                            exp_tag = f" {exp}" if exp else ""
                            re_lbl = f" [RE-ENTRY #{st_sell.entry_count} New HH SL]" if st_sell.sl_hit_count > 0 else ""
                            sig_act = f"SELL {opt_type}{exp_tag}{re_lbl}"
                            active_alerts.append(
                                f"{sig_act} @ {ltp:.2f} | {sell_ctx} | {lvl} Reject "
                                f"→ T1:{sell_entry['T1']} T2:{sell_entry['T2']} T3:{sell_entry['T3']} T4:{sell_entry['T4']} T5:{sell_entry['T5']} | SL:{sell_entry['SL']} "
                                f"| Zone: Above S2({fp_sell['S2']:.1f}) Below R2({fp_sell['R2']:.1f})"
                            )
                            self._last_ft_sync_ts = 0.0   # push the new entry to the sheet right away
                            _new_trade = self.forward_tracker.log_entry(
                                symbol, opt_type, strike, "SELL",
                                round(ltp, 2),          # Live market data entry price (LTP)
                                st_sell.sl,                   # HH locked at sweep
                                st_sell.targets[0] if len(st_sell.targets) > 0 else sell_entry['T1'],
                                st_sell.targets[1] if len(st_sell.targets) > 1 else sell_entry['T2'],
                                st_sell.targets[2] if len(st_sell.targets) > 2 else sell_entry['T3'],
                                st_sell.targets[3] if len(st_sell.targets) > 3 else sell_entry['T4'],
                                st_sell.targets[4] if len(st_sell.targets) > 4 else sell_entry['T5'],
                                opt_piv=opt_piv, timeframe=trade_tf,
                                token=token, exchange=self.exchange,
                                expiry=exp, levels=strike_levels_tf(strike, f15_focus, f1h_focus)
                            )
                            if _new_trade:   # alert only when a trade was actually recorded
                                self.alert_mgr.send_alert_once(
                                    symbol, opt_type, strike, sig_act, ltp,
                                    f"{sell_ctx} | {lvl} Reject | New HH SL:{sell_entry['SL']:.1f}",
                                    sell_entry['SL'], sell_entry['T1']
                                )

                    # ── Evaluate BUY Entry ──
                    fp_buy = getattr(st_buy, "frozen_pivots", None) or opt_piv
                    if (buy_entry["entry"] and mkt_open and is_focus_strike and live_tick
                            and at_entry_price(ltp, st_buy.entry_price, "BUY")):
                        in_buy_zone = (fp_buy["S2"] * 0.95 <= ltp <= fp_buy["R2"] * 1.05)
                        if in_buy_zone:
                            lvl = st_buy.trigger_level
                            re_lbl = f" [RE-ENTRY #{st_buy.entry_count} New LL SL]" if st_buy.sl_hit_count > 0 else ""
                            exp_tag = f" {exp}" if exp else ""
                            sig_act = f"BUY {opt_type}{exp_tag}{re_lbl}"
                            active_alerts.append(
                                f"{sig_act} @ {ltp:.2f} | {buy_ctx} | {lvl} Reclaim "
                                f"→ T1:{buy_entry['T1']} T2:{buy_entry['T2']} T3:{buy_entry['T3']} T4:{buy_entry['T4']} T5:{buy_entry['T5']} | SL:{buy_entry['SL']} "
                                f"| Zone: Above S2({fp_buy['S2']:.1f}) Below R2({fp_buy['R2']:.1f})"
                            )
                            self._last_ft_sync_ts = 0.0   # push the new entry to the sheet right away
                            _new_trade = self.forward_tracker.log_entry(
                                symbol, opt_type, strike, "BUY",
                                round(ltp, 2),          # Live market data entry price (LTP)
                                st_buy.sl,                   # LL locked at sweep
                                st_buy.targets[0] if len(st_buy.targets) > 0 else buy_entry['T1'],
                                st_buy.targets[1] if len(st_buy.targets) > 1 else buy_entry['T2'],
                                st_buy.targets[2] if len(st_buy.targets) > 2 else buy_entry['T3'],
                                st_buy.targets[3] if len(st_buy.targets) > 3 else buy_entry['T4'],
                                st_buy.targets[4] if len(st_buy.targets) > 4 else buy_entry['T5'],
                                opt_piv=opt_piv, timeframe=trade_tf,
                                token=token, exchange=self.exchange,
                                expiry=exp, levels=strike_levels_tf(strike, f15_focus, f1h_focus)
                            )
                            if _new_trade:   # alert only when a trade was actually recorded
                                self.alert_mgr.send_alert_once(
                                    symbol, opt_type, strike, sig_act, ltp,
                                    f"{buy_ctx} | {lvl} Reclaim | New LL SL:{buy_entry['SL']:.1f}",
                                    buy_entry['SL'], buy_entry['T1']
                                )

                    _s2_raw = opt_piv.get("S2", 0)
                    is_s1_buy = (_s2_raw <= 0.05 or st_buy.trigger_level == "S1")

                    # BUY Signal text:
                    if st_buy.state == "CONFIRMED_BUY" and (st_buy.trade_status == "Active" or "Target" in st_buy.trade_status):
                        lvl = st_buy.trigger_level
                        ep  = st_buy.entry_price
                        h1_tag = f" | {h1_buy_action}" if h1_buy_action else ""
                        tg_tag = f" | {st_buy.trade_status}" if st_buy.trade_status != "Active" else ""
                        signal_buy = f"★ BUY {opt_type} [{buy_ctx} | {lvl} Reclaim | Entry@{lvl}:{ep:.2f} | SL:{st_buy.sl:.1f}{tg_tag}{h1_tag}]"
                    elif st_buy.state == "TRADING_IN_FAVOR_BUY":
                        _lvl = "S1" if is_s1_buy else "S2"
                        signal_buy = f"★ {opt_type} {_lvl} Push In Favour [{buy_ctx}] — Confirming Buy Entry"
                    elif st_buy.state.startswith("RETEST_S"):
                        _lvl = "S1" if ("S1" in st_buy.state or is_s1_buy) else "S2"
                        signal_buy = f"★ {opt_type} RETEST {_lvl} [{buy_ctx}] LL:{st_buy.ll:.1f} — Watching Retest Hold"
                    elif st_buy.state.startswith("SWEEP_S"):
                        _lvl = "S1" if ("S1" in st_buy.state or is_s1_buy) else "S2"
                        signal_buy = f"★ {opt_type} SWEEP {_lvl} [{buy_ctx}] LL:{st_buy.ll:.1f} — Watching for reclaim"
                    elif st_buy.state.startswith("RECLAIM_S"):
                        _lvl = "S1" if ("S1" in st_buy.state or is_s1_buy) else "S2"
                        signal_buy = f"★ {opt_type} RECLAIM {_lvl} [{buy_ctx}] — Awaiting bullish candle close"
                    else:
                        if is_s1_buy:
                            _b_lvl = "S1"
                            _b_ep  = round(st_buy.entry_price if st_buy.entry_price > 0 else st_buy.conf_high if st_buy.conf_high > 0 else (st_buy.frozen_s1 or opt_piv.get("S1", 0)), 2)
                            signal_buy = f"★ BUY {opt_type} [Entry@S1:{_b_ep:.1f} | LL SL (S2<=0)]"
                        else:
                            _b_lvl = "S2"
                            _b_ep  = round(st_buy.entry_price if st_buy.entry_price > 0 else st_buy.conf_high if st_buy.conf_high > 0 else (st_buy.frozen_s2 or _s2_raw), 2)
                            signal_buy = f"★ BUY {opt_type} [Entry@S2:{_b_ep:.1f} | LL SL]"

                    # SELL Signal text:
                    if st_sell.state == "CONFIRMED_SELL" and (st_sell.trade_status == "Active" or "Target" in st_sell.trade_status):
                        lvl = st_sell.trigger_level
                        ep  = st_sell.entry_price
                        h1_tag = f" | {h1_sell_action}" if h1_sell_action else ""
                        tg_tag = f" | {st_sell.trade_status}" if st_sell.trade_status != "Active" else ""
                        signal_sell = f"▼ SELL {opt_type} [{sell_ctx} | {lvl} Reject | Entry@{lvl}:{ep:.2f} | SL:{st_sell.sl:.1f}{tg_tag}{h1_tag}]"
                    elif st_sell.state == "TRADING_IN_FAVOR_SELL":
                        signal_sell = f"▼ {opt_type} R2 Drop In Favour [{sell_ctx}] — Confirming Sell Entry"
                    elif st_sell.state == "RETEST_R2":
                        signal_sell = f"▼ {opt_type} RETEST R2 [{sell_ctx}] HH:{st_sell.hh:.1f} — Watching Retest Hold"
                    elif st_sell.state == "SWEEP_R2":
                        signal_sell = f"▼ {opt_type} SWEEP R2 [{sell_ctx}] HH:{st_sell.hh:.1f} — Watching for rejection"
                    elif st_sell.state == "RECLAIM_R2":
                        signal_sell = f"▼ {opt_type} REJECT R2 [{sell_ctx}] — Awaiting bearish candle close"
                    else:
                        _s_ep = round(st_sell.frozen_r2 or opt_piv.get("R2", 0) or opt_piv.get("R1", 0), 2)
                        _s_lvl = "R2" if (st_sell.frozen_r2 or opt_piv.get("R2", 0)) > 1.0 else "R1"
                        signal_sell = f"▼ SELL {opt_type} [Entry@{_s_lvl}:{_s_ep:.1f} | HH SL]"

                    # Update forward test tick tracking for target/SL exit
                    # (only real trades from today can hit a target or SL)
                    closed_t = (self.forward_tracker.update_tick(opt_type, strike, ltp, expiry=exp, symbol=symbol)
                                if live_tick else None)
                    if closed_t:
                        self.alert_mgr.send_exit_alert(closed_t)
                        # Keep the setup state machine in step with the tracker: the trade is over,
                        # so the strike waits for a FRESH sweep before any new entry (no instant re-entry
                        # on the same confirmation with a stale, already-trailed SL).
                        _st_done = st_buy if closed_t.get("Action") == "BUY" else st_sell
                        _st_done.trade_status = f"{closed_t.get('Exit_Reason', 'Exit')} Closed"
                        _st_done.state = "WAITING"
                        if "SL" in str(closed_t.get("Exit_Reason", "")):
                            _st_done.sl_hit_count += 1   # labels the next entry as a re-entry (and alerts it)
                        self._last_ft_sync_ts = 0.0   # record SL/target exit immediately
                        self.sync_forward_test_to_sheet()
                        self.sync_multi_symbol_sheet()

                    # Stale-reset hook: sync ForwardTestTracker with state machines
                    for _st_inst in (st_buy, st_sell):
                        if _st_inst.stale_reset:
                            _st_inst.stale_reset = False
                            closed_stale = self.forward_tracker.manual_close(
                                opt_type, strike, ltp, reason="Stale Entry Reset", expiry=exp, symbol=symbol
                            )
                            if closed_stale:
                                _stale_date = datetime.now().strftime('%Y-%m-%d')
                                _stale_key  = (symbol, opt_type, strike, exp, closed_stale['Action'], _stale_date) if exp else (symbol, opt_type, strike, closed_stale['Action'], _stale_date)
                                self.forward_tracker._closed_keys_today.discard(_stale_key)
                                self.forward_tracker._closed_keys_today.discard((opt_type, strike, exp, closed_stale['Action'], _stale_date))
                                self.forward_tracker._closed_keys_today.discard((opt_type, strike, closed_stale['Action'], _stale_date))
                                print(f"⚠️  [STALE RESET] {symbol} {opt_type} {strike}{' ' + exp if exp else ''}: "
                                      f"old entry={closed_stale['Entry_Price']:.2f} cleared | fresh signal will re-form")
                                self.sync_forward_test_to_sheet()
                                self.sync_multi_symbol_sheet()

                    # ── Assemble Paired Watch Items (BUY + SELL) ──
                    if opt_piv and ltp > 0:
                        tok_meta = self.token_to_meta.get(token, {})
                        is_15m_focus = (strike in daily_focus.get("all_15m", []))
                        is_1h_focus  = (strike in daily_focus.get("all_1h", []))
                        strike_tag = ""
                        if is_15m_focus and is_1h_focus:
                            strike_tag = "[⭐15M & ⏳1H]"
                        elif is_15m_focus:
                            strike_tag = "[⭐15M Focus]"
                        elif is_1h_focus:
                            strike_tag = "[⏳1H Focus]"

                        tok_oi  = d.get("oi",  0)
                        tok_vol = d.get("vol", 0)
                        eff_ll  = (st_buy.day_low or st_sell.day_low) if (st_buy.day_low or st_sell.day_low) > 0 else round(opt_low if opt_low > 0 else ltp, 2)
                        eff_hh  = (st_buy.day_high or st_sell.day_high) if (st_buy.day_high or st_sell.day_high) > 0 else round(opt_high if opt_high > 0 else ltp, 2)

                        # BUY Watch Item (S2 or S1 Buyer Area)
                        disp_s3_b = round(max(fp_buy.get('S3', opt_piv['S3']), 0.05), 2)
                        disp_s2_b = round(max(fp_buy.get('S2', opt_piv['S2']), 0.05), 2)
                        disp_s1_b = round(max(fp_buy.get('S1', opt_piv['S1']), 0.05), 2)
                        disp_p_b  = round(fp_buy.get('P',  opt_piv['P']),  2)
                        disp_r1_b = round(fp_buy.get('R1', opt_piv['R1']), 2)
                        disp_r2_b = round(fp_buy.get('R2', opt_piv['R2']), 2)
                        disp_r3_b = round(fp_buy.get('R3', opt_piv['R3']), 2)

                        is_s1_buy = (opt_piv.get('S2', 0) <= 0.05 or disp_s2_b <= 0.05 or st_buy.trigger_level == "S1")
                        if is_s1_buy:
                            calc_ep_b = round(st_buy.entry_price if st_buy.entry_price > 0 else st_buy.conf_high if st_buy.conf_high > 0 else (st_buy.frozen_s1 if st_buy.frozen_s1 > 0 else disp_s1_b), 2)
                            zone_text = f"{strike_tag} Buy Zone Above S1 ({calc_ep_b:.1f}) [S2<=0]".strip()
                            plan_buy_zone = f"BUY S1={disp_s1_b:.1f} (S2<=0) | T1:{disp_p_b:.1f} T2:{disp_r1_b:.1f}"
                            tgts_b = list(st_buy.targets) if st_buy.targets else [disp_p_b, disp_r1_b, disp_r2_b, disp_r3_b, round(disp_r3_b * 1.05, 2)]
                        else:
                            calc_ep_b = round(st_buy.entry_price if st_buy.entry_price > 0 else st_buy.conf_high if st_buy.conf_high > 0 else (st_buy.frozen_s2 if st_buy.frozen_s2 > 0 else disp_s2_b), 2)
                            zone_text = f"{strike_tag} Buy Zone Above S2 ({calc_ep_b:.1f})".strip()
                            plan_buy_zone = f"BUY S2={disp_s2_b:.1f} | T1:{disp_s1_b:.1f} P:{disp_p_b:.1f}"
                            tgts_b = list(st_buy.targets) if st_buy.targets else [disp_s1_b, disp_p_b, disp_r1_b, disp_r2_b, disp_r3_b]

                        if st_buy.sl > 0:
                            calc_sl_b = round(st_buy.sl, 2)
                        elif st_buy.trade_status in OPEN_TRADE_STATUSES or "RECLAIM" in st_buy.state or "CONFIRMED_BUY" in st_buy.state:
                            calc_sl_b = eff_ll if (0 < eff_ll < calc_ep_b) else round(calc_ep_b * 0.90, 2)
                        else:
                            calc_sl_b = 0.0

                        watch_buy = {
                            "side":         "BUY",
                            "type":         opt_type,
                            "strike":       strike,
                            "ltp":          ltp,
                            "high":         opt_high,
                            "low":          opt_low,
                            "oi":           tok_oi,
                            "vol":          tok_vol,
                            "zone":         zone_text,
                            "expiry":       tok_meta.get("expiry", "-"),
                            "is_15m":       is_15m_focus,
                            "is_1h":        is_1h_focus,
                            "is_nearby":    False,
                            "s3":           disp_s3_b,
                            "s2":           disp_s2_b,
                            "s1":           disp_s1_b,
                            "p":            disp_p_b,
                            "r1":           disp_r1_b,
                            "r2":           disp_r2_b,
                            "r3":           disp_r3_b,
                            "category":     "CE_SUP" if opt_type == "CE" else "PE_SUP",
                            "status":       signal_buy,
                            "m5_state":     st_buy.state,
                            "trade_status": st_buy.trade_status,
                            "ll":           eff_ll,
                            "hh":           eff_hh,
                            "sl":           calc_sl_b,
                            "entry_sl":     round(st_buy.entry_sl, 2),
                            "entry_price":  calc_ep_b,
                            "targets":      tgts_b,
                            "target":       tgts_b[0] if tgts_b else 0.0,
                            "t1":           round(tgts_b[0], 2) if len(tgts_b) > 0 else 0.0,
                            "t2":           round(tgts_b[1], 2) if len(tgts_b) > 1 else 0.0,
                            "t3":           round(tgts_b[2], 2) if len(tgts_b) > 2 else 0.0,
                            "t4":           round(tgts_b[3], 2) if len(tgts_b) > 3 else 0.0,
                            "plan_s2":      round(max(opt_piv.get("S2", 0), 0.05), 2),
                            "plan_s1":      round(max(opt_piv.get("S1", 0), 0.05), 2),
                            "plan_p":       round(opt_piv.get("P",  0), 2),
                            "plan_r1":      round(opt_piv.get("R1", 0), 2),
                            "plan_r2":      round(opt_piv.get("R2", 0), 2),
                            "plan_buy_zone":  plan_buy_zone,
                            "plan_sell_zone": f"SELL R2={opt_piv.get('R2',0):.1f} | T1:{opt_piv.get('R1',0):.1f} P:{opt_piv.get('P',0):.1f}",
                        }

                        # SELL Watch Item (R2 Seller Area)
                        disp_s3_s = round(fp_sell.get('S3', opt_piv['S3']), 2)
                        disp_s2_s = round(fp_sell.get('S2', opt_piv['S2']), 2)
                        disp_s1_s = round(fp_sell.get('S1', opt_piv['S1']), 2)
                        disp_p_s  = round(fp_sell.get('P',  opt_piv['P']),  2)
                        disp_r1_s = round(fp_sell.get('R1', opt_piv['R1']), 2)
                        disp_r2_s = round(fp_sell.get('R2', opt_piv['R2']), 2)
                        disp_r3_s = round(fp_sell.get('R3', opt_piv['R3']), 2)

                        calc_ep_s = round(st_sell.entry_price if st_sell.entry_price > 0 else st_sell.conf_low if st_sell.conf_low > 0 else (disp_r2_s if disp_r2_s > 1.0 else disp_r1_s), 2)
                        if st_sell.sl > 0:
                            calc_sl_s = round(st_sell.sl, 2)
                        elif st_sell.trade_status in OPEN_TRADE_STATUSES or "REJECT" in st_sell.state or "CONFIRMED_SELL" in st_sell.state:
                            calc_sl_s = eff_hh if (eff_hh > calc_ep_s) else round(calc_ep_s * 1.10, 2)
                        else:
                            calc_sl_s = 0.0

                        tgts_s = list(st_sell.targets) if st_sell.targets else [disp_r1_s, disp_p_s, disp_s1_s, disp_s2_s, disp_s3_s]

                        watch_sell = {
                            "side":         "SELL",
                            "type":         opt_type,
                            "strike":       strike,
                            "ltp":          ltp,
                            "high":         opt_high,
                            "low":          opt_low,
                            "oi":           tok_oi,
                            "vol":          tok_vol,
                            "zone":         f"{strike_tag} Sell Area Below R2 ({calc_ep_s:.1f})".strip(),
                            "expiry":       tok_meta.get("expiry", "-"),
                            "is_15m":       is_15m_focus,
                            "is_1h":        is_1h_focus,
                            "is_nearby":    False,
                            "s3":           disp_s3_s,
                            "s2":           disp_s2_s,
                            "s1":           disp_s1_s,
                            "p":            disp_p_s,
                            "r1":           disp_r1_s,
                            "r2":           disp_r2_s,
                            "r3":           disp_r3_s,
                            "category":     "PE_RES" if opt_type == "PE" else "CE_RES",
                            "status":       signal_sell,
                            "m5_state":     st_sell.state,
                            "trade_status": st_sell.trade_status,
                            "ll":           eff_ll,
                            "hh":           eff_hh,
                            "sl":           calc_sl_s,
                            "entry_sl":     round(st_sell.entry_sl, 2),
                            "entry_price":  calc_ep_s,
                            "targets":      tgts_s,
                            "target":       tgts_s[0] if tgts_s else 0.0,
                            "t1":           round(tgts_s[0], 2) if len(tgts_s) > 0 else 0.0,
                            "t2":           round(tgts_s[1], 2) if len(tgts_s) > 1 else 0.0,
                            "t3":           round(tgts_s[2], 2) if len(tgts_s) > 2 else 0.0,
                            "t4":           round(tgts_s[3], 2) if len(tgts_s) > 3 else 0.0,
                            "plan_s2":      round(opt_piv.get("S2", 0), 2),
                            "plan_s1":      round(opt_piv.get("S1", 0), 2),
                            "plan_p":       round(opt_piv.get("P",  0), 2),
                            "plan_r1":      round(opt_piv.get("R1", 0), 2),
                            "plan_r2":      round(opt_piv.get("R2", 0), 2),
                            "plan_buy_zone":  f"BUY S2={opt_piv.get('S2',0):.1f} | T1:{opt_piv.get('S1',0):.1f} P:{opt_piv.get('P',0):.1f}",
                            "plan_sell_zone": f"SELL R2={opt_piv.get('R2',0):.1f} | T1:{opt_piv.get('R1',0):.1f} P:{opt_piv.get('P',0):.1f}",
                        }

                        all_watch.append(watch_buy)
                        all_watch.append(watch_sell)
                        pe_res_watch.append(watch_sell)

                # Update live watchlist for UI
                with self.lock:
                    self.reversal_alerts  = active_alerts
                    self.s1_s2_watchlist  = all_watch
                    self.ce_sup_watchlist = ce_sup_watch
                    self.pe_res_watchlist = pe_res_watch

                # ── Construct Google Sheet Scanner rows — split CE / PE ────
                today_str     = datetime.now().strftime("%d-%b-%Y %H:%M")
                all_focus_set = set(daily_focus.get("all_focus", daily_focus.get("all_4", [])))

                col_headers = [
                    "Expiry", "Type", "Strike", "Side", "LTP", "OI", "Volume", "Trading Zone", "Pivot P", "S1", "S2", "R1", "R2",
                    "5M State", "LL", "HH", "Signal", "SL", "Entry", "T1", "T2", "T3", "Status", "Updated"
                ]

                def _make_row(w):
                    # LL = day's running lowest low; HH = day's running highest high
                    # These come from st_state.day_low/day_high and must NOT fall back to opt_high/low
                    ll_val  = w.get("ll", 0)   # day_low: strict running min
                    hh_val  = w.get("hh", 0)   # day_high: strict running max
                    opt_t   = w.get("type", "CE")
                    sig_txt = w.get("status", "")
                    m5_st   = w.get("m5_state", "")
                    zone_t  = w.get("zone", "")
                    cat_t   = w.get("category", "")
                    sl_val  = w.get("sl", 0)

                    # Determine setup side: BUY (from S2) or SELL (from R2) — decided once in the
                    # reversal pairing step and stored on the watch item as "side".
                    is_buy = (w.get("side", "BUY") == "BUY")
                    side_badge = "🟢 BUY" if is_buy else "🔴 SELL"

                    ll_str = f"{ll_val:.2f}" if ll_val > 0 else "-"
                    hh_str = f"{hh_val:.2f}" if hh_val > 0 else "-"

                    trade_st_row = w.get("trade_status", "Waiting")
                    s2_raw = w.get("s2", 0)
                    s1_raw = w.get("s1", 0)
                    is_s1_buy = (s2_raw <= 0.05 or "S1" in m5_st or "S1" in sig_txt)

                    if is_buy:
                        planned_ep = round(s1_raw if is_s1_buy else s2_raw, 2)
                        # SL is calculated AFTER entry: LL when price reclaims S1/S2 and candle close gives entry
                        sl_str = display_sl(trade_st_row, m5_st, sl_val, True, planned_ep, ll=ll_val, entry_sl=w.get("entry_sl", 0))
                    else:
                        planned_ep = round(w.get("r2", 0) if w.get("r2", 0) > 1.0 else w.get("r1", 0), 2)
                        # SL is calculated AFTER entry: HH when price rejects R2 and candle close gives entry
                        sl_str = display_sl(trade_st_row, m5_st, sl_val, False, planned_ep, hh=hh_val, entry_sl=w.get("entry_sl", 0))

                    # Targets T1 / T2 / T3 from the full target ladder
                    tgts = w.get("targets", [])
                    if len(tgts) > 0 and tgts[0] > 0:
                        t1_str = f"{tgts[0]:.2f}"
                        t2_str = f"{tgts[1]:.2f}" if len(tgts) > 1 and tgts[1] > 0 else "-"
                        t3_str = f"{tgts[2]:.2f}" if len(tgts) > 2 and tgts[2] > 0 else "-"
                    elif is_buy and is_s1_buy:
                        t1_str = f"{w.get('p', 0):.2f}"
                        t2_str = f"{w.get('r1', 0):.2f}"
                        t3_str = f"{w.get('r2', 0):.2f}"
                    elif is_buy:
                        t1_str = f"{w.get('s1', 0):.2f}"
                        t2_str = f"{w.get('p', 0):.2f}"
                        t3_str = f"{w.get('r1', 0):.2f}"
                    else:
                        t1_str = f"{w.get('r1', 0):.2f}"
                        t2_str = f"{w.get('p', 0):.2f}"
                        t3_str = f"{w.get('s1', 0):.2f}"

                    # Format OI and Volume in K / L for readability
                    oi_val  = w.get("oi",  0)
                    vol_val = w.get("vol", 0)
                    oi_str  = (f"{oi_val/100000:.1f}L"  if oi_val  >= 100000 else
                               (f"{oi_val/1000:.1f}K"   if oi_val  >= 1000   else str(oi_val)))
                    vol_str = (f"{vol_val/100000:.1f}L" if vol_val >= 100000 else
                               (f"{vol_val/1000:.1f}K"  if vol_val >= 1000   else str(vol_val)))

                    ep_val = w.get("entry_price", 0)
                    entry_str = f"{ep_val:.2f}" if ep_val and ep_val > 0 else "-"

                    return [
                        w.get("expiry", "-"),
                        w["type"],
                        w["strike"],
                        side_badge,
                        round(w["ltp"], 2),
                        oi_str,
                        vol_str,
                        w.get("zone", "-"),
                        round(w.get("p",  0.0), 2),
                        round(w.get("s1", 0.0), 2),
                        round(w.get("s2", 0.0), 2),
                        round(w.get("r1", 0.0), 2),
                        round(w.get("r2", 0.0), 2),
                        w.get("m5_state", "WAITING"),
                        ll_str,   # LL of the day
                        hh_str,   # HH of the day
                        w.get("status", "-"),
                        sl_str,   # SL column
                        entry_str,  # Entry Price
                        t1_str,   # T1
                        t2_str,   # T2
                        t3_str,   # T3
                        w.get("trade_status", "Waiting"),
                        now_str
                    ]

                # Separate CE and PE watch items, each sorted: expiry, focus first, strike asc, side BUY before SELL
                ce_items = sorted(
                    [w for w in all_watch if w["type"] == "CE"],
                    key=lambda w: (
                        w.get("expiry", ""),
                        0 if w["strike"] in all_focus_set else 1,
                        w["strike"],
                        0 if w.get("side") == "BUY" else 1
                    )
                )
                pe_items = sorted(
                    [w for w in all_watch if w["type"] == "PE"],
                    key=lambda w: (
                        w.get("expiry", ""),
                        0 if w["strike"] in all_focus_set else 1,
                        w["strike"],
                        0 if w.get("side") == "BUY" else 1
                    )
                )

                def _rows_with_expiry_dividers(items):
                    """Insert a 1-row expiry header between expiry groups."""
                    rows  = []
                    cur_exp = None
                    for w in items:
                        exp = w.get("expiry", "-")
                        if exp != cur_exp:
                            cur_exp = exp
                            divrow = [f"── Expiry: {exp} ──"] + [""] * 23
                            rows.append(divrow)
                        rows.append(_make_row(w))
                    return rows

                ce_rows = _rows_with_expiry_dividers(ce_items)
                pe_rows = _rows_with_expiry_dividers(pe_items)

                # Pad each section (24 columns: includes Side & Entry Price)
                min_rows = max(len(sorted_strikes), 1)
                ce_rows += [[""] * 24 for _ in range(max(0, min_rows - len(ce_rows)))]
                pe_rows += [[""] * 24 for _ in range(max(0, min_rows - len(pe_rows)))]

                # ── ROW 1: Spot + Market Direction + Live Time ─────────────
                r1_text = f"⚡ {symbol} SPOT: {spot:.2f}  |  Market: {mkt_dir.get('direction','-')}  |  15M Zone: {z15m}  |  1H Zone: {z1h}  |  Live: {now_str}"

                # ── ROW 2: 15-Minute Intraday Section Banner ────────────────
                f15 = self.focus_15m or daily_focus.get("15M", {})
                r2_text = (
                    f"⏱️ 15-MIN INTRADAY PIVOTS: P={piv_15m_mtf.get('P','-')} S1={piv_15m_mtf.get('S1','-')} S2={piv_15m_mtf.get('S2','-')} R1={piv_15m_mtf.get('R1','-')} R2={piv_15m_mtf.get('R2','-')}  ||  "
                    f"15M FOCUS STRIKES ➜ CE1: {f15.get('CE1','-')}  CE2: {f15.get('CE2','-')} (Resistance) | P: {'/'.join(str(x) for x in f15.get('p_strikes', [])) or f15.get('P_STRIKE','-')} (CE+PE) | "
                    f"PE1: {f15.get('PE1','-')}  PE2: {f15.get('PE2','-')} (Support)"
                )

                # ── ROW 3: 1-Hour Timeframe Section Banner ──────────────────
                f1h = self.focus_1h or daily_focus.get("1H", {})
                r3_text = (
                    f"⏳ 1-HOUR TIMEFRAME PIVOTS: P={h1_piv_mtf.get('P','-')} S1={h1_piv_mtf.get('S1','-')} S2={h1_piv_mtf.get('S2','-')} R1={h1_piv_mtf.get('R1','-')} R2={h1_piv_mtf.get('R2','-')}  ||  "
                    f"1H STRUCTURE STRIKES ➜ CE1: {f1h.get('CE1','-')}  CE2: {f1h.get('CE2','-')} (Resistance) | P: {'/'.join(str(x) for x in f1h.get('p_strikes', [])) or f1h.get('P_STRIKE','-')} (CE+PE) | "
                    f"PE1: {f1h.get('PE1','-')}  PE2: {f1h.get('PE2','-')} (Support)"
                )

                # ── ROW 4: Strategy Alerts Banner ───────────────────────────
                if active_alerts:
                    r4_text = f"🚨 ENTRY ALERTS ({len(active_alerts)}): " + "  |  ".join(active_alerts[:3])
                else:
                    r4_text = f"REVERSAL STRATEGY: S2 Reclaim (BUY) & R2 Rejection (SELL) Active across All Timeframes"

                # ── Max OI per expiry header lines ────────────────────────────
                oi_by_exp_snap = self.oi_data.get("oi_by_exp", {})
                oi_header_parts = []
                for exp_k, exp_oi in sorted(oi_by_exp_snap.items()):
                    if not exp_oi: continue
                    mc_s = max(exp_oi, key=lambda s: exp_oi[s].get("CE", 0), default=None)
                    mp_s = max(exp_oi, key=lambda s: exp_oi[s].get("PE", 0), default=None)
                    mc_v = exp_oi[mc_s]["CE"] if mc_s else 0
                    mp_v = exp_oi[mp_s]["PE"] if mp_s else 0
                    mc_fmt = f"{mc_v/100000:.1f}L" if mc_v >= 100000 else f"{mc_v/1000:.0f}K"
                    mp_fmt = f"{mp_v/100000:.1f}L" if mp_v >= 100000 else f"{mp_v/1000:.0f}K"
                    oi_header_parts.append(f"{exp_k}: CE MaxOI@{mc_s}({mc_fmt})  PE MaxOI@{mp_s}({mp_fmt})")
                oi_header_text = "📊 OI SUMMARY: " + "  ||  ".join(oi_header_parts) if oi_header_parts else "📊 OI SUMMARY: Waiting for data..."

                # ── Section banners ──────────────────────────────────────────
                ce_banner = [f"📗 CALL (CE) — BUY & SELL DUAL SCANNER  |  {len(ce_items)} active  |  15M Focus: {f15.get('CE1','-')}, {f15.get('CE2','-')}  |  1H Focus: {f1h.get('CE1','-')}, {f1h.get('CE2','-')}"] + [""] * 23
                pe_banner = [f"📕 PUT (PE)  — BUY & SELL DUAL SCANNER  |  {len(pe_items)} active  |  15M Focus: {f15.get('PE1','-')}, {f15.get('PE2','-')}  |  1H Focus: {f1h.get('PE1','-')}, {f1h.get('PE2','-')}"] + [""] * 23
                divider   = ["─" * 20] + [""] * 23

                full_grid = (
                    [[r1_text] + [""] * 23,
                     [r2_text] + [""] * 23,
                     [r3_text] + [""] * 23,
                     [r4_text] + [""] * 23,
                     [oi_header_text] + [""] * 23,
                     [""] * 24,
                     ce_banner,
                     col_headers]
                    + ce_rows
                    + [divider, pe_banner, col_headers]
                    + pe_rows
                )

                # ── Non-blocking sheet write in background thread (throttled to 6s) ─────────────
                _grid_to_write = full_grid   # capture for closure
                _ws             = self.ws
                def _write_grid(grid=_grid_to_write, ws=_ws):
                    try:
                        if ws is None:
                            return
                        now_t = time.time()
                        if now_t < getattr(self, '_quota_backoff_until', 0.0):
                            return
                        if now_t - getattr(self, '_last_sheet_write_ts', 0.0) < 6.0:
                            return
                        self._last_sheet_write_ts = now_t
                        ws.update(grid)
                    except Exception as ex:
                        if "429" in str(ex) or "Quota" in str(ex):
                            self._quota_backoff_until = time.time() + 60.0
                            print("[SHEET] Google Sheets 429 quota reached — pausing sheet updates for 60s")
                        else:
                            print(f"Sheet write error: {ex}")
                threading.Thread(target=_write_grid, daemon=True).start()

                loop += 1

                # ── Apply conditional banner colours (only on loop 1 to preserve API quota) ──
                if loop == 1:
                    try:
                        from gspread_formatting import format_cell_range, CellFormat, Color, TextFormat
                        ce_start_row = 9
                        format_cell_range(self.ws, "A7:X7", CellFormat(
                            backgroundColor=Color(0.05, 0.22, 0.10),
                            textFormat=TextFormat(foregroundColor=Color(0.4, 1.0, 0.5), bold=True)))
                        pe_banner_row = ce_start_row + len(ce_rows) + 1
                        format_cell_range(self.ws, f"A{pe_banner_row}:X{pe_banner_row}", CellFormat(
                            backgroundColor=Color(0.22, 0.05, 0.08),
                            textFormat=TextFormat(foregroundColor=Color(1.0, 0.4, 0.4), bold=True)))
                    except Exception:
                        pass

                # Sync Forward Test in background (throttled)
                _n_active = len(self.forward_tracker.active_trades)
                if _n_active != getattr(self, "_ft_last_n_active", 0):
                    self._ft_last_n_active = _n_active
                    self._last_ft_sync_ts = 0.0          # new entry / exit → write sheet now
                    self.sync_forward_test_to_sheet()
                elif (_n_active > 0 and loop % 30 == 0) or loop % 100 == 0:
                    self.sync_forward_test_to_sheet()   # throttled non-blocking
                self.sync_orders_to_sheet()             # push new paper/live order rows
                if loop % 20 == 0 and status_cb:
                    status_cb(
                        f"● LIVE | spot:{spot} ({sp_zone}) | {len(all_watch)} active watch | {now_str}"
                    )

                # ── 3:00 PM intraday square-off: close all open trades at CMP ──
                _is_weekday = now_dt.weekday() < 5
                if (_is_weekday and EOD_SQUARE_OFF_TIME <= now_dt.time() <= dt_time(15, 30)
                        and getattr(self, '_eod_sq_done_date', None) != today_date):
                    self._eod_sq_done_date = today_date
                    try:
                        _sq = self.forward_tracker.eod_square_off("EOD 3PM Square-off @ CMP")
                        for _c in _sq:
                            self.alert_mgr.send_exit_alert(_c)
                        if status_cb:
                            status_cb(f"⏰ 3:00 PM square-off — {len(_sq)} trade(s) closed at CMP")
                        self._last_ft_sync_ts = 0.0
                        self.sync_forward_test_to_sheet()
                    except Exception as _sq_err:
                        print(f"[EOD 3PM] square-off error: {_sq_err}")
                    self.backfill_abandoned_async(delay=120)   # 1-min data up to 15:00 is in by then

                # ── 3:45 PM: next-day levels + eod_planner.py (once per day, non-blocking) ──
                _after_close = _is_weekday and now_dt.time() >= NEXT_DAY_LEVELS_TIME
                if _after_close and getattr(self, '_eod_planner_done_date', None) != today_date:
                    self._eod_planner_done_date = today_date

                    threading.Thread(target=self.build_next_day_levels, args=(symbol,), daemon=True).start()

                time.sleep(0.1)   # 100ms ultra-fast tick processing — maximum speed

            except Exception as e:
                if not self.running: break
                import traceback
                err_str = str(e)
                print(f"Sheet loop error: {err_str}")
                traceback.print_exc()
                # Exponential backoff for Google Sheets 429 quota exhaustion
                if "429" in err_str or "Quota" in err_str:
                    time.sleep(15)   # reduced from 30s
                else:
                    time.sleep(2)    # reduced from 5s

feed = FeedEngine()

# Modern fixed-width font with high legibility on macOS / Windows
GUI_FONT = "Menlo" if sys.platform == "darwin" else "Consolas"

# ═══════════════════════════════════════════════════════════
# DARK THEME BUTTON (macOS & Linux high-contrast custom widget)
# ═══════════════════════════════════════════════════════════
class DarkButton(tk.Label):
    """
    High-contrast dark-theme button for macOS & Linux Tkinter.
    Avoids macOS native Aqua engine rendering buttons as white/light-gray pills
    which drowns out white typography. Renders sharp, rich colors and crisp fonts.
    """
    def __init__(self, parent, text="", command=None, bg="#21262d", fg="#ffffff",
                 activebackground=None, activeforeground="#ffffff",
                 disabledbackground="#21262d", disabledforeground="#8b949e",
                 font=(GUI_FONT, 9, "bold"), padx=12, pady=6,
                 relief="flat", cursor="hand2", **kwargs):
        kwargs.pop("highlightbackground", None)
        kwargs.pop("bd", None)
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font,
                         padx=padx, pady=pady, cursor=cursor, relief=relief, **kwargs)
        self.command = command
        self._bg = bg
        self._fg = fg
        self._active_bg = activebackground or bg
        self._active_fg = activeforeground or fg
        self._disabled_bg = disabledbackground
        self._disabled_fg = disabledforeground
        self._state = kwargs.get("state", "normal")
        if self._state == "disabled":
            super().configure(bg=self._disabled_bg, fg=self._disabled_fg, cursor="arrow")

        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_click(self, event=None):
        if self._state == "normal" and self.command:
            self.command()

    def _on_enter(self, event=None):
        if self._state == "normal":
            super().configure(bg=self._active_bg, fg=self._active_fg)

    def _on_leave(self, event=None):
        if self._state == "normal":
            super().configure(bg=self._bg, fg=self._fg)

    def config(self, **kwargs):
        kwargs.pop("highlightbackground", None)
        kwargs.pop("bd", None)
        if "bg" in kwargs:
            self._bg = kwargs["bg"]
        if "fg" in kwargs:
            self._fg = kwargs["fg"]
        if "activebackground" in kwargs:
            self._active_bg = kwargs.pop("activebackground")
        if "activeforeground" in kwargs:
            self._active_fg = kwargs.pop("activeforeground")

        if "state" in kwargs:
            self._state = kwargs.pop("state")
            if self._state == "disabled":
                cur_bg = kwargs.pop("bg", self._disabled_bg)
                cur_fg = kwargs.pop("fg", self._disabled_fg)
                super().configure(cursor="arrow", bg=cur_bg, fg=cur_fg)
            else:
                cur_bg = kwargs.pop("bg", self._bg)
                cur_fg = kwargs.pop("fg", self._fg)
                super().configure(cursor="hand2", bg=cur_bg, fg=cur_fg)
        super().config(**kwargs)

    configure = config

# ═══════════════════════════════════════════════════════════
# CONTROL PANEL
# ═══════════════════════════════════════════════════════════
# Dashboard multi-select filter groups: within a group ticks are OR'ed, groups are AND'ed.
# A group with nothing ticked does not filter.
DASH_FILTER_GROUPS = [
    ("Side",  [("BUY", "🟢 Buy"), ("SELL", "🔴 Sell")]),
    ("Type",  [("CE", "CE"), ("PE", "PE")]),
    ("TF",    [("15M", "⭐ 15M only"), ("1H", "⏳ 1H only"), ("BOTH", "⭐⏳ 15M + 1H")]),
    ("Level", [("RS", "R/S strikes"), ("P", "🔷 P strike"), ("PN", "Next to P")]),
    ("State", [("WAIT", "Waiting"), ("SETUP", "⚡ Setup forming"), ("TRADE", "🟢 In trade"), ("DONE", "Closed")]),
]


def dash_row_flags(w: dict, ce_15m, pe_15m, ce_1h, pe_1h, p_15m, p_1h, p_exact=None) -> dict:
    """Classify one dashboard row for the filters and colouring.
    Level: "P" = the strike nearest a 15M/1H pivot P (coloured 🔷), "PN" = the strike next to it
    (traded on CE+PE but not coloured), "RS" = an R1/R2 (CE) or S1/S2 (PE) strike."""
    ot  = w.get("type", "CE")
    stk = w.get("strike", 0)
    rs_15 = (ot == "CE" and stk in ce_15m) or (ot == "PE" and stk in pe_15m)
    rs_1h = (ot == "CE" and stk in ce_1h) or (ot == "PE" and stk in pe_1h)
    p15, p1h = stk in p_15m, stk in p_1h
    in15 = stk in set(ce_15m) | set(pe_15m) | set(p_15m)
    in1h = stk in set(ce_1h) | set(pe_1h) | set(p_1h)
    if p_exact is None:
        p_exact = set(p_15m) | set(p_1h)
    is_exact_p = stk in p_exact
    ts = str(w.get("trade_status", "") or "")
    m5 = str(w.get("m5_state", "") or "")
    if ts in ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit"):
        state = "TRADE"
    elif "Target" in ts or "SL" in ts or "Exit" in ts or "Closed" in ts:
        state = "DONE"
    elif any(k in m5 for k in ("CONFIRMED", "RECLAIM", "REJECT", "TRADING_IN_FAVOR", "SWEEP", "RETEST")):
        state = "SETUP"
    else:
        state = "WAIT"
    return {
        "Side": {w.get("side", "BUY")},
        "Type": {ot},
        # each strike sits in exactly one TF box: 15M only, 1H only, or both (never mixed in).
        # A strike counts for a timeframe when it is on that timeframe's plan for CE or PE,
        # the same rule the trades use ("15M Intraday" / "1H Structure" / "15M / 1H").
        "TF": ({"BOTH"} if (in15 and in1h) else {"15M"} if in15 else {"1H"} if in1h else set()),
        "Level": ({"RS"} if (rs_15 or rs_1h) else set())
                 | ({"P"} if is_exact_p else set())
                 | ({"PN"} if ((p15 or p1h) and not is_exact_p) else set()),
        "State": {state},
        "is_p": is_exact_p,
        "is_15m": in15,
        "is_1h": in1h,
        "on_plan": rs_15 or p15 or rs_1h or p1h,     # this CE/PE row is on a 15M or 1H plan
    }


def dash_row_passes(flags: dict, selected: dict) -> bool:
    """selected: {group: set(ticked keys)}. Empty group = no filtering on that group."""
    for grp, ticked in selected.items():
        if ticked and not (flags.get(grp, set()) & ticked):
            return False
    return True


class ControlPanel:
    def __init__(self,root):
        self.root=root
        # Tk is not thread-safe: background threads (feed loop, WS callbacks) call
        # root.after(...). Route those through a queue that the Tk main thread drains,
        # so the macOS dashboard can't freeze or crash from cross-thread Tk calls.
        import queue as _queue
        self._ui_q = _queue.Queue()
        self._tk_after = root.after
        _main_thread = threading.main_thread()
        def _safe_after(ms, func=None, *args):
            if threading.current_thread() is _main_thread:
                return self._tk_after(ms, func, *args)
            self._ui_q.put((ms, func, args))
            return None
        root.after = _safe_after
        def _pump():
            try:
                for _ in range(500):
                    ms, func, args = self._ui_q.get_nowait()
                    self._tk_after(ms, func, *args)
            except _queue.Empty:
                pass
            except Exception as _pe:
                print(f"[UI] queue pump note: {_pe}")
            self._last_ui_pump = time.time()
            self._tk_after(50, _pump)
        self._last_ui_pump = time.time()
        self._tk_after(50, _pump)
        def _ui_hang_watchdog():
            # If the Tk main thread is stuck for 2 minutes the window is dead: exit with
            # code 3 so run_option_chain.command restarts the app (and the feed) cleanly.
            while True:
                time.sleep(10)
                if time.time() - self._last_ui_pump > 120:
                    print("[UI] ❌ Dashboard frozen for 120s — exiting for auto-restart")
                    os._exit(3)
        threading.Thread(target=_ui_hang_watchdog, daemon=True).start()
        root.title("Angel One — Live Option Chain & Fibonacci Pivot Reversal")
        root.geometry("1440x820")
        root.minsize(1200, 680)
        root.configure(bg="#0d1117")
        root.resizable(True,True)

        self.exchange_var  = tk.StringVar(value="NFO")
        self.symbol_var    = tk.StringVar(value="NIFTY")
        self.expiry_var    = tk.StringVar()
        self.next_exp_var  = tk.StringVar()          # next expiry
        self.use_next_exp  = tk.BooleanVar(value=True)  # enable by default
        self.strikes_var   = tk.IntVar(value=10)
        self.status_var    = tk.StringVar(value="Ready")
        self.max_oi_info   = tk.StringVar(value="")
        self.spot_info     = tk.StringVar(value="Spot: -- | Zone: --")
        self.pivots_info   = tk.StringVar(value="Pivots: Initializing...")
        self.sec_15m_info  = tk.StringVar(value="⏱️ 15M Intraday: Loading levels & strikes...")
        self.sec_1h_info   = tk.StringVar(value="⏳ 1H Structure: Loading levels & strikes...")
        self.expiry_filter_var = tk.StringVar(value="ALL")  # "ALL", "CURRENT", or "NEXT"

        self._style_ttk()
        self._build_ui()
        self._on_exchange_change()

    def _style_ttk(self):
        s=ttk.Style(self.root)
        s.theme_use("clam")
        for w in ["TCombobox"]:
            s.configure(w,fieldbackground="#21262d",background="#21262d",
                        foreground="#f0f6fc",selectbackground="#388bfd",
                        selectforeground="#fff",bordercolor="#30363d",
                        lightcolor="#30363d",darkcolor="#30363d",arrowcolor="#8b949e")
            s.map(w,fieldbackground=[("readonly","#21262d")],
                  foreground=[("readonly","#f0f6fc")])

        # Style Treeview with proper line spacing and modern colors
        s.configure("Treeview",
                    background="#0d1117",
                    foreground="#f0f6fc",
                    fieldbackground="#0d1117",
                    font=(GUI_FONT, 9),
                    rowheight=25)
        s.configure("Treeview.Heading",
                    background="#161b22",
                    foreground="#58a6ff",
                    font=(GUI_FONT, 9, "bold"),
                    relief="flat")
        s.map("Treeview", background=[("selected", "#1f6feb")])
        # NOTE: tag_configure is called after self.tree is created in _build_ui

    def _build_ui(self):
        r=self.root
        tk.Label(r,text="FIBONACCI PIVOT OPTION CHAIN & REVERSAL STRATEGY",bg="#0d1117",fg="#58a6ff",
                 font=(GUI_FONT,15,"bold")).pack(pady=(12,2))
        tk.Label(r,text="1H Support/Resistance Strikes  •  5M Liquidity Sweep & Reclaim  •  Live Alerts & Google Sheets",
                 bg="#0d1117",fg="#8b949e",font=(GUI_FONT,9)).pack()

        sep=tk.Frame(r,bg="#21262d",height=1)
        sep.pack(fill="x",padx=16,pady=8)

        # ── Controls Frame ──────────────────────────────────
        frame=tk.Frame(r,bg="#161b22",highlightthickness=1,highlightbackground="#30363d")
        frame.pack(fill="x",padx=16,pady=2)
        frame.columnconfigure(1,weight=1)

        def lbl(p,t,row,col=0,**kw):
            tk.Label(p,text=t,bg="#161b22",fg="#8b949e",
                     font=("Consolas",9),anchor="w",**kw).grid(
                         row=row,column=col,padx=10,pady=6,sticky="w")

        def cb(p,var,vals,row,cmd=None):
            c=ttk.Combobox(p,textvariable=var,values=vals,
                           state="readonly",width=22,font=("Consolas",10))
            if cmd: c.bind("<<ComboboxSelected>>",lambda e:cmd())
            c.grid(row=row,column=1,padx=10,pady=6,sticky="ew")
            return c

        lbl(frame,"Exchange",0)
        cb(frame,self.exchange_var,list(EXCHANGE_CONFIG.keys()),0,self._on_exchange_change)

        lbl(frame, "Symbol", 1)
        sym_f = tk.Frame(frame, bg="#161b22")
        sym_f.grid(row=1, column=1, padx=10, pady=6, sticky="ew")
        self.sym_cb = ttk.Combobox(sym_f, textvariable=self.symbol_var, values=[],
                                   width=14, font=("Consolas", 10))
        self.sym_cb.pack(side="left", fill="x", expand=True)
        self.sym_cb.bind("<<ComboboxSelected>>", lambda e: self._load_expiries())
        self.sym_cb.bind("<Return>", lambda e: self._on_symbol_entered())

        lbl(frame, "Expiry (Current)", 2)
        self.exp_cb = cb(frame, self.expiry_var, [], 2, cmd=self._on_expiry_selected)

        # ── Next Expiry row ─────────────────────────────────
        lbl(frame, "Next Expiry", 3)
        nf = tk.Frame(frame, bg="#161b22")
        nf.grid(row=3, column=1, padx=10, pady=6, sticky="ew")
        self.next_exp_cb = ttk.Combobox(
            nf, textvariable=self.next_exp_var, values=[],
            state="readonly", width=18, font=("Consolas", 10))
        self.next_exp_cb.bind("<<ComboboxSelected>>", lambda e: self._on_next_expiry_selected())
        self.next_exp_cb.pack(side="left", fill="x", expand=True)
        tk.Checkbutton(
            nf, text="Enable", variable=self.use_next_exp,
            bg="#161b22", fg="#58a6ff", selectcolor="#21262d",
            activebackground="#161b22", activeforeground="#58a6ff",
            font=("Consolas", 9), command=self._refresh_display
        ).pack(side="left", padx=6)

        lbl(frame, "Strikes (ATM +/-)", 4)
        sf = tk.Frame(frame, bg="#161b22")
        sf.grid(row=4, column=1, padx=10, pady=6, sticky="ew")
        self.slbl = tk.Label(sf, text="10", bg="#161b22", fg="#58a6ff",
                             font=("Consolas", 10, "bold"), width=3)
        self.slbl.pack(side="left")
        tk.Scale(sf, from_=3, to=50, orient="horizontal", variable=self.strikes_var,
                 bg="#161b22", fg="#8b949e", troughcolor="#21262d",
                 highlightthickness=0, bd=0, showvalue=False,
                 command=lambda v: self.slbl.config(text=str(int(float(v))))).pack(
                     side="left", fill="x", expand=True)

        # ── Top Feed / Live Scanner Control Unit ────────────
        top_ctrl = tk.Frame(frame, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        top_ctrl.grid(row=0, column=2, rowspan=5, padx=(20, 10), pady=6, sticky="nsew")

        tk.Label(top_ctrl, text="⚡ LIVE SCANNER CONTROL", bg="#161b22", fg="#ffd700",
                 font=(GUI_FONT, 9, "bold")).pack(padx=12, pady=(8, 4), anchor="w")

        self.top_start_btn = DarkButton(top_ctrl, text="▶ START LIVE SCANNER", command=self.start_feed,
                                        bg="#238636", fg="#ffffff", activebackground="#2ea043",
                                        font=(GUI_FONT, 10, "bold"), padx=14, pady=7)
        self.top_start_btn.pack(fill="x", padx=10, pady=2)

        self.top_stop_btn = DarkButton(top_ctrl, text="⏹ STOP", command=self.stop_feed,
                                       bg="#21262d", fg="#8b949e", activebackground="#da3633",
                                       disabledbackground="#21262d", disabledforeground="#8b949e",
                                       state="disabled", font=(GUI_FONT, 10, "bold"), padx=14, pady=7)
        self.top_stop_btn.pack(fill="x", padx=10, pady=2)

        # ── Spot & Fibonacci Pivot Monitor Frame ────────────
        sp_frame = tk.Frame(r, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        sp_frame.pack(side="top", fill="x", padx=16, pady=2)

        tk.Label(sp_frame, text="⚡ UNDERLYING, 15M INTRADAY & 1H STRUCTURE STATUS", bg="#161b22", fg="#ffd700",
                 font=(GUI_FONT, 9, "bold")).pack(anchor="w", padx=10, pady=(3, 1))

        self.spot_lbl = tk.Label(sp_frame, textvariable=self.spot_info,
                                 bg="#161b22", fg="#58a6ff", font=(GUI_FONT, 10, "bold"), anchor="w")
        self.spot_lbl.pack(fill="x", padx=10, pady=1)

        # ── 15-Minute Intraday Section Display ──────────────
        self.sec_15m_lbl = tk.Label(sp_frame, textvariable=self.sec_15m_info,
                                    bg="#161b22", fg="#38d9a9", font=(GUI_FONT, 9, "bold"), anchor="w")
        self.sec_15m_lbl.pack(fill="x", padx=10, pady=1)

        # ── 1-Hour Timeframe Section Display ────────────────
        self.sec_1h_lbl = tk.Label(sp_frame, textvariable=self.sec_1h_info,
                                   bg="#161b22", fg="#ffd43b", font=(GUI_FONT, 9, "bold"), anchor="w")
        self.sec_1h_lbl.pack(fill="x", padx=10, pady=1)

        # ── Multi-Timeframe Fibonacci Pivot Levels (Weekly, Daily, 1 Hr, 15 Min, 5 Min with S1-S3 & R1-R3) ──
        mtf_card = tk.Frame(sp_frame, bg="#0d1117", highlightthickness=1, highlightbackground="#30363d")
        mtf_card.pack(fill="x", padx=8, pady=(2, 3))

        self.mtf_vars = {
            "Weekly":  tk.StringVar(value="[WEEKLY]   ZONE: --        │ P:      -- │ S1:      --  S2:      --  S3:      -- │ R1:      --  R2:      --  R3:      --"),
            "Daily":   tk.StringVar(value="[DAILY]    ZONE: --        │ P:      -- │ S1:      --  S2:      --  S3:      -- │ R1:      --  R2:      --  R3:      --"),
            "1-Hour":  tk.StringVar(value="[1-HOUR]   ZONE: --        │ P:      -- │ S1:      --  S2:      --  S3:      -- │ R1:      --  R2:      --  R3:      --"),
            "15-Min":  tk.StringVar(value="[15-MIN]   ZONE: --        │ P:      -- │ S1:      --  S2:      --  S3:      -- │ R1:      --  R2:      --  R3:      --"),
        }

        tf_palette = [
            ("Weekly", "#d2a8ff"),  # Vibrant lavender / purple
            ("Daily",  "#79c0ff"),  # Bright sky blue
            ("1-Hour", "#ffd700"),  # Electric gold
            ("15-Min", "#7ee787"),  # Vibrant spring mint
        ]

        self.mtf_lbls = {}
        for tf, col in tf_palette:
            lbl = tk.Label(mtf_card, textvariable=self.mtf_vars[tf],
                           bg="#0d1117", fg=col, font=(GUI_FONT, 9, "bold"),
                           anchor="w", justify="left")
            lbl.pack(fill="x", padx=8, pady=0)
            self.mtf_lbls[tf] = lbl

        self.mtf_info = tk.StringVar(value="")

        # ── Reversal Strategy Watchlist (Live Dashboard from Strategy PDF) ───
        rev_frame=tk.Frame(r,bg="#161b22",highlightthickness=1,highlightbackground="#30363d")
        # NOTE: packed with side='top', expand=True AFTER bottom pinned controls

        rev_hdr=tk.Frame(rev_frame,bg="#161b22")
        rev_hdr.pack(fill="x",padx=10,pady=(4,2))
        tk.Label(rev_hdr,text="LIVE FIBONACCI REVERSAL DASHBOARD (15M Intraday & 1H Structure Sweeps)",bg="#161b22",fg="#3fb950",
                 font=("Consolas",8,"bold")).pack(side="left")

        # Filter buttons
        rf = tk.Frame(rev_frame, bg="#161b22")
        rf.pack(fill="x", padx=8, pady=(0, 4))
        # Multi-select filters: tick any combination. Within a group ticks are OR'ed, groups are AND'ed;
        # a group with nothing ticked shows everything for that group.
        self.dash_filter_vars = {}
        _cb_kw = dict(bg="#161b22", fg="#ffffff", selectcolor="#0d1117",
                      activebackground="#161b22", activeforeground="#58a6ff",
                      highlightthickness=0, bd=0, font=("Consolas", 9, "bold"),
                      command=self._on_dash_filter_changed)
        rf2 = tk.Frame(rev_frame, bg="#161b22")
        rf2.pack(fill="x", padx=8, pady=(0, 4))
        _rows = [rf, rf, rf, rf2, rf2]
        for (grp, opts), parent in zip(DASH_FILTER_GROUPS, _rows):
            tk.Label(parent, text=f" {grp}:", bg="#161b22", fg="#8b949e",
                     font=("Consolas", 9, "bold")).pack(side="left", padx=(6, 1))
            for key, txt in opts:
                v = tk.BooleanVar(value=False)
                self.dash_filter_vars[(grp, key)] = v
                fg = "#7fd4ff" if key == "P" else _cb_kw["fg"]
                tk.Checkbutton(parent, text=txt, variable=v, **{**_cb_kw, "fg": fg}).pack(side="left", padx=2)
            tk.Label(parent, text="│", bg="#161b22", fg="#30363d").pack(side="left", padx=2)

        # Expiry: tick Current and/or Next (none ticked = both)
        tk.Label(rf, text=" 📅 Expiry:", bg="#161b22", fg="#ffd700",
                 font=("Consolas", 9, "bold")).pack(side="left", padx=(6, 1))
        self.exp_cur_var  = tk.BooleanVar(value=False)
        self.exp_next_var = tk.BooleanVar(value=False)
        tk.Checkbutton(rf, text="⚡ Current", variable=self.exp_cur_var, **{**_cb_kw, "fg": "#3fb950"}).pack(side="left", padx=2)
        tk.Checkbutton(rf, text="⏭ Next", variable=self.exp_next_var, **{**_cb_kw, "fg": "#58a6ff"}).pack(side="left", padx=2)

        self.show_plan_cols_var = tk.BooleanVar(value=False)
        tk.Checkbutton(rf2, text="📅 Show plan columns", variable=self.show_plan_cols_var, **_cb_kw).pack(side="left", padx=(6, 2))
        tk.Button(rf2, text="✖ Clear filters", command=self._clear_dash_filters,
                  bg="#21262d", fg="#c9d1d9", activebackground="#30363d", activeforeground="#ffffff",
                  relief="flat", font=("Consolas", 9, "bold"), padx=6).pack(side="left", padx=6)
        self.dash_filter_info = tk.StringVar(value="")
        tk.Label(rf2, textvariable=self.dash_filter_info, bg="#161b22", fg="#8b949e",
                 font=("Consolas", 9)).pack(side="left", padx=6)

        # Treeview for strikes near Support / Resistance
        tree_frame = tk.Frame(rev_frame, bg="#161b22")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        cols = ("Expiry", "Type", "Strike", "Side", "LTP", "OI", "Vol", "Trading Zone", "S3", "S2", "S1", "P", "R1", "R2", "R3", "5M State", "LL", "HH", "Signal", "SL", "Entry", "T1", "T2", "T3", "T4", "T5", "Status", "Order",
                "Plan S2", "Plan P", "Plan R2", "Plan Signal", "vs Plan")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=10)

        col_configs = {
            "Expiry":       {"width": 80,  "anchor": "center", "minwidth": 60},
            "Type":         {"width": 45,  "anchor": "center", "minwidth": 40},
            "Strike":       {"width": 70,  "anchor": "center", "minwidth": 60},
            "Side":         {"width": 80,  "anchor": "center", "minwidth": 70},
            "LTP":          {"width": 68,  "anchor": "e",      "minwidth": 55},
            "OI":           {"width": 62,  "anchor": "e",      "minwidth": 50},
            "Vol":          {"width": 62,  "anchor": "e",      "minwidth": 50},
            "Trading Zone": {"width": 180, "anchor": "w",      "minwidth": 120},
            "S3":           {"width": 55,  "anchor": "e",      "minwidth": 45},
            "R3":           {"width": 55,  "anchor": "e",      "minwidth": 45},
            "P":            {"width": 55,  "anchor": "e",      "minwidth": 45},
            "S1":           {"width": 55,  "anchor": "e",      "minwidth": 45},
            "S2":           {"width": 55,  "anchor": "e",      "minwidth": 45},
            "R1":           {"width": 55,  "anchor": "e",      "minwidth": 45},
            "R2":           {"width": 55,  "anchor": "e",      "minwidth": 45},
            "5M State":     {"width": 130, "anchor": "center", "minwidth": 100},
            "LL":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "HH":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "Signal":       {"width": 250, "anchor": "w",      "minwidth": 160},
            "SL":           {"width": 65,  "anchor": "e",      "minwidth": 50},
            "Entry":        {"width": 65,  "anchor": "e",      "minwidth": 50},
            "T1":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "T2":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "T3":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "T4":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "T5":           {"width": 58,  "anchor": "e",      "minwidth": 45},
            "Status":       {"width": 95,  "anchor": "center", "minwidth": 75},
            "Order":        {"width": 80,  "anchor": "center", "minwidth": 60},   # broker order: Pending / Filled / Closed / Rejected
            # Today's Levels plan columns (shown only in TODAY_LEVELS filter)
            "Plan S2":      {"width": 65,  "anchor": "e",      "minwidth": 50},
            "Plan P":       {"width": 65,  "anchor": "e",      "minwidth": 50},
            "Plan R2":      {"width": 65,  "anchor": "e",      "minwidth": 50},
            "Plan Signal":  {"width": 160, "anchor": "w",      "minwidth": 110},
            "vs Plan":      {"width": 90,  "anchor": "center", "minwidth": 70},
        }
        for c in cols:
            cfg_c = col_configs.get(c, {"width": 65, "anchor": "center", "minwidth": 50})
            self.tree.heading(c, text=c)
            self.tree.column(c, width=cfg_c["width"], anchor=cfg_c["anchor"], minwidth=cfg_c["minwidth"])

        # Pivot levels S3..R3 shown on the right, after Status / Order (row values keep their order)
        _lv = ("S3", "S2", "S1", "P", "R1", "R2", "R3")
        _plan = ("Plan S2", "Plan P", "Plan R2", "Plan Signal", "vs Plan")
        self.tree["displaycolumns"] = ([c for c in cols if c not in _lv and c not in _plan]
                                       + list(_lv) + list(_plan))

        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        # Row colour tags: professional high-contrast dark theme for rapid trader scanning
        self.tree.tag_configure("buy_row",     background="#091811", foreground="#c0e8cf")  # Muted forest green
        self.tree.tag_configure("sell_row",    background="#1a0b11", foreground="#f4c0c8")  # Muted plum red
        self.tree.tag_configure("ready_buy",   background="#062b19", foreground="#3bf586")  # Bright neon green setup ready
        self.tree.tag_configure("ready_sell",  background="#330810", foreground="#ff6b81")  # Bright neon red setup ready
        self.tree.tag_configure("active_buy",  background="#084224", foreground="#ffffff")  # High contrast in-trade BUY
        self.tree.tag_configure("active_sell", background="#540c17", foreground="#ffffff")  # High contrast in-trade SELL
        self.tree.tag_configure("target",      background="#382903", foreground="#ffd700")  # Gold target hit
        self.tree.tag_configure("sl",          background="#380e0e", foreground="#ff7070")  # Maroon SL hit
        # P-level strikes: blue / violet family so they stand out from the R/S strikes
        self.tree.tag_configure("p_buy_row",   background="#0b1f3a", foreground="#9fd3ff")
        self.tree.tag_configure("p_sell_row",  background="#22123a", foreground="#d6b8ff")
        self.tree.tag_configure("p_ready_buy", background="#0d3263", foreground="#4fc3ff")
        self.tree.tag_configure("p_ready_sell",background="#3a1666", foreground="#c792ff")

        # ── PINNED BOTTOM FRAMES (Anchored with side='bottom' so NEVER pushed off screen) ──
        # 1. Status bar (absolute bottom)
        sb = tk.Frame(r, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        sb.pack(side="bottom", fill="x", padx=16, pady=(2, 6))
        self.status_lbl = tk.Label(sb, textvariable=self.status_var,
                                  bg="#161b22", fg="#3fb950",
                                  font=(GUI_FONT, 9), anchor="w")
        self.status_lbl.pack(fill="x", padx=8, pady=3)

        # 2. Main Live Scanner Start / Stop Action Buttons (PROMINENT, PINNED AT BOTTOM)
        bf = tk.Frame(r, bg="#0d1117")
        bf.pack(side="bottom", fill="x", padx=16, pady=4)
        self.start_btn = DarkButton(bf, text="▶ START LIVE SCANNER & FEED", command=self.start_feed,
                                    bg="#238636", fg="#ffffff",
                                    activebackground="#2ea043", activeforeground="#ffffff",
                                    disabledbackground="#21262d", disabledforeground="#8b949e",
                                    font=(GUI_FONT, 12, "bold"), pady=8)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.stop_btn = DarkButton(bf, text="⏹ STOP", command=self.stop_feed,
                                   bg="#21262d", fg="#8b949e",
                                   activebackground="#da3633", activeforeground="#ffffff",
                                   disabledbackground="#21262d", disabledforeground="#8b949e",
                                   state="disabled",
                                   font=(GUI_FONT, 12, "bold"), pady=8)
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Register button pairs so both top and bottom toggle together
        self._start_buttons = [self.start_btn, getattr(self, "top_start_btn", self.start_btn)]
        self._stop_buttons  = [self.stop_btn, getattr(self, "top_stop_btn", self.stop_btn)]

        # 3. Strike tools toolbar
        tool_frame = tk.Frame(r, bg="#0d1117")
        tool_frame.pack(side="bottom", fill="x", padx=16, pady=2)

        DarkButton(tool_frame, text="📋 Next-Day Plan & Focus Strikes",
                   command=self._show_next_day_plan_window,
                   bg="#1f6feb", fg="#ffffff",
                   activebackground="#388bfd", activeforeground="#ffffff",
                   font=(GUI_FONT, 9, "bold"),
                   padx=14, pady=5).pack(side="left", padx=(0, 6))

        DarkButton(tool_frame, text="📐 Map Strike Levels (TV Exact)",
                   command=self._on_map_strike_levels_click,
                   bg="#196c2e", fg="#ffffff",
                   activebackground="#2ea043", activeforeground="#ffffff",
                   font=(GUI_FONT, 9, "bold"),
                   padx=14, pady=5).pack(side="left", padx=6)

        tk.Label(tool_frame, text="Levels from:", bg="#0d1117", fg="#8b949e",
                 font=(GUI_FONT, 9)).pack(side="left", padx=(6, 2))
        self.level_source_var = tk.StringVar(value=LEVEL_SOURCES[get_level_source()])
        _src_cb = ttk.Combobox(tool_frame, textvariable=self.level_source_var, state="readonly",
                               values=list(LEVEL_SOURCES.values()), width=19)
        _src_cb.pack(side="left", padx=(0, 6))
        _src_cb.bind("<<ComboboxSelected>>", lambda e: self._on_level_source_change())

        DarkButton(tool_frame, text="⚡ Gap Check (09:16 AM)",
                   command=self._on_gap_check_click,
                   bg="#6e40c9", fg="#ffffff",
                   activebackground="#8957e5", activeforeground="#ffffff",
                   font=(GUI_FONT, 9, "bold"),
                   padx=14, pady=5).pack(side="left", padx=6)

        # 4. Live order bar (slim). All order settings live in the ⚙ Settings window.
        order_frame = tk.Frame(r, bg="#0d1117", highlightthickness=1, highlightbackground="#30363d")
        order_frame.pack(side="bottom", fill="x", padx=16, pady=2)
        self._build_order_bar(order_frame)

        # 5. Google Sheet link & Forward Test filter toolbar
        row_b = tk.Frame(r, bg="#0d1117")
        row_b.pack(side="bottom", fill="x", padx=16, pady=2)

        sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        DarkButton(row_b, text="📊 Open Google Sheet ↗",
                   command=lambda: __import__("webbrowser").open(sheet_url),
                   bg="#1f6feb", fg="#ffffff",
                   activebackground="#388bfd", activeforeground="#ffffff",
                   font=(GUI_FONT, 9, "bold"),
                   padx=12, pady=4).pack(side="left")

        tk.Label(row_b, text="🎯 FT Filter:", bg="#0d1117", fg="#ffd700",
                 font=(GUI_FONT, 8, "bold")).pack(side="left", padx=(12, 3))
        self.ft_tf_filter_var = tk.StringVar(value="ALL")

        def _on_ft_filter_selected(v):
            feed.ft_timeframe_filter = v
            threading.Thread(target=feed.sync_forward_test_to_sheet, daemon=True).start()

        for val, txt in [
            ("ALL", "ALL"),
            ("15M Intraday", "15M"),
            ("1H Structure", "1H"),
        ]:
            tk.Radiobutton(row_b, text=txt, value=val, variable=self.ft_tf_filter_var,
                           bg="#0d1117", fg="#ffffff", selectcolor="#161b22",
                           activebackground="#0d1117", activeforeground="#58a6ff",
                           highlightbackground="#0d1117",
                           font=(GUI_FONT, 9, "bold"),
                           command=lambda v=val: _on_ft_filter_selected(v)).pack(side="left", padx=3)

        self.oi_lbl = tk.Label(row_b, textvariable=self.max_oi_info,
                               bg="#0d1117", fg="#ffd700", font=(GUI_FONT, 8))
        self.oi_lbl.pack(side="right")

        # 6. Finally, pack rev_frame to fill remaining space in the middle
        rev_frame.pack(side="top", fill="both", expand=True, padx=16, pady=4)

    def _on_exchange_change(self):
        exch=self.exchange_var.get()
        syms=list(EXCHANGE_CONFIG.get(exch, {}).get("symbols", []))
        if Session.scrip_df is not None:
            try:
                df = Session.scrip_df
                seg_candidates = [exch]
                if exch == "BFO": seg_candidates = ["BFO", "BSE"]
                inst = EXCHANGE_CONFIG.get(exch, {}).get("inst_types", ["OPTIDX", "OPTSTK", "OPTFUT"])
                extra_syms = sorted(df[(df["exch_seg"].isin(seg_candidates)) & (df["instrumenttype"].isin(inst))]["name"].dropna().unique().tolist())
                for s in extra_syms:
                    if s not in syms:
                        syms.append(s)
            except Exception:
                pass
        self.sym_cb["values"]=syms
        if syms:
            self.symbol_var.set(syms[0])
        self._load_expiries()

    def _on_symbol_entered(self):
        sym = (self.symbol_var.get() or "").strip().upper()
        if not sym:
            return
        self.symbol_var.set(sym)
        detected_exch = get_symbol_exchange(sym)
        if detected_exch and self.exchange_var.get() != detected_exch:
            self.exchange_var.set(detected_exch)
            syms = list(EXCHANGE_CONFIG.get(detected_exch, {}).get("symbols", []))
            if Session.scrip_df is not None:
                try:
                    df = Session.scrip_df
                    seg_candidates = [detected_exch]
                    if detected_exch == "BFO": seg_candidates = ["BFO", "BSE"]
                    inst = EXCHANGE_CONFIG.get(detected_exch, {}).get("inst_types", ["OPTIDX", "OPTSTK", "OPTFUT"])
                    extra_syms = sorted(df[(df["exch_seg"].isin(seg_candidates)) & (df["instrumenttype"].isin(inst))]["name"].dropna().unique().tolist())
                    for s in extra_syms:
                        if s not in syms:
                            syms.append(s)
                except Exception:
                    pass
            self.sym_cb["values"] = syms
        self._load_expiries()

    def _show_symbol_calculator_window(self, default_sym=None):
        """Symbol calculator window removed as requested."""
        pass

    def _on_level_source_change(self):
        label = self.level_source_var.get()
        key = next((k for k, v in LEVEL_SOURCES.items() if v == label), "DHAN")
        set_level_source(key)
        if is_live_market_open():
            # levels are locked during the session; the new source is used from the next session
            self._set_status(f"Levels source: {label}. Today's levels stay locked; used from the next session.", "#ffd700")
        else:
            self._set_status(f"Levels source: {label}. Rebuilding strike levels...", "#58a6ff")
            self._on_map_strike_levels_click()

    def _on_map_strike_levels_click(self):
        sym = self.symbol_var.get()
        exp = self.expiry_var.get()
        self._set_status(f"Mapping strike Fibonacci pivots for {sym} ({exp})...", "#58a6ff")
        def _run_map():
            mapped = feed.fetch_and_map_all_strikes(sym, exp, force_fetch=True)
            if mapped:
                self.root.after(0, lambda: self._set_status(f"✅ Mapped {len(mapped)} strikes for {sym} — 100% TradingView Aligned", "#3fb950"))
                self.root.after(0, self._refresh_display)
            else:
                self.root.after(0, lambda: self._set_status("⚠️ Strike mapping completed from local cache", "#ffd700"))
        threading.Thread(target=_run_map, daemon=True).start()

    def _show_next_day_plan_window(self):
        sym = self.symbol_var.get()
        exp = self.expiry_var.get()

        cfg = EXCHANGE_CONFIG.get(next((k for k,v in EXCHANGE_CONFIG.items() if sym in v["symbols"]), "NFO"), {})
        step = cfg.get("steps", {}).get(sym, 50)
        mtf_all = feed.mtf_pivots or get_tv_multi_timeframe_pivots(sym)
        focus = feed.daily_focus or select_daily_4_focus_strikes(mtf_all, step)
        f15 = focus.get("15M", {})
        f1h = focus.get("1H", {})

        nd_plan = load_next_day_plan(sym)

        # After the close the next session's levels (built from today's candle) are shown;
        # otherwise the levels of the session in progress.
        cdata = read_json_file(STRIKE_PIVOT_NEXT_CACHE)
        if not (cdata.get("symbol") == sym and cdata.get("date", "") > datetime.now().strftime("%Y-%m-%d")):
            cdata = read_json_file(STRIKE_PIVOT_CACHE)
        strikes_dict = cdata.get("strikes", {})
        if not strikes_dict:
            self._set_status("Generating strike level map...", "#58a6ff")
            strikes_dict = feed.fetch_and_map_all_strikes(sym, exp, force_fetch=False)

        win = tk.Toplevel(self.root)
        win.title(f"Next-Day Trading Plan & Strike Levels ({sym}) — Intraday & Structure Focus (TV Exact)")
        win.geometry("1060x650")
        win.configure(bg="#0d1117")

        top_f = tk.Frame(win, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        top_f.pack(fill="x", padx=12, pady=(10, 6))

        dt_str = nd_plan.get("target_trading_date") or nd_plan.get("generated_date") or cdata.get("date", datetime.now().strftime("%Y-%m-%d"))
        plan_st = nd_plan.get("status", "ACTIVE") if nd_plan else "LIVE CALCULATED"
        plan_spot = nd_plan.get("spot", 0.0)
        spot_str = f" | Ref Spot: {plan_spot:.2f}" if plan_spot > 0 else ""
        tk.Label(top_f, text=f"📐 NEXT-DAY FIBONACCI PIVOT MAP  |  Symbol: {sym}  |  Session: {dt_str}  |  Status: {plan_st}{spot_str}",
                 bg="#161b22", fg="#58a6ff", font=("Consolas", 11, "bold")).pack(anchor="w", padx=10, pady=(6, 2))

        # Index Pivots Reference Display
        piv15 = (nd_plan.get("levels_15m") if nd_plan else None) or mtf_all.get("15-Min", {})
        piv1h = (nd_plan.get("levels_1h") if nd_plan else None) or mtf_all.get("1-Hour", {})
        f_pe = nd_plan.get("support", {}).get("focus_pe", []) if nd_plan else []
        f_ce = nd_plan.get("resistance", {}).get("focus_ce", []) if nd_plan else []
        focus_plan_txt = f"  •  Plan Focus: CE {f_ce} | PE {f_pe}" if (f_ce or f_pe) else ""
        idx_txt = (
            f"Index 15M Pivots: P={piv15.get('P', 0):.1f} | R1={piv15.get('R1', 0):.1f} R2={piv15.get('R2', 0):.1f} R3={piv15.get('R3', 0):.1f} | "
            f"S1={piv15.get('S1', 0):.1f} S2={piv15.get('S2', 0):.1f} S3={piv15.get('S3', 0):.1f}\n"
            f"Index 1H Pivots:  P={piv1h.get('P', 0):.1f} | R1={piv1h.get('R1', 0):.1f} R2={piv1h.get('R2', 0):.1f} R3={piv1h.get('R3', 0):.1f} | "
            f"S1={piv1h.get('S1', 0):.1f} S2={piv1h.get('S2', 0):.1f} S3={piv1h.get('S3', 0):.1f}{focus_plan_txt}"
        )
        tk.Label(top_f, text=idx_txt, bg="#161b22", fg="#ffd700", font=("Consolas", 8), justify="left").pack(anchor="w", padx=10, pady=2)

        rule_txt = "Trading Logic:  BUY on S2 Reclaim (T1:S1 → T2:P → T3:R1 → T4:R2 | SL=LL of day)  •  SELL on R2 Rejection (T1:R1 → T2:P → T3:S1 → T4:S2 | SL=HH of day)"
        tk.Label(top_f, text=rule_txt, bg="#161b22", fg="#8b949e", font=("Consolas", 8)).pack(anchor="w", padx=10, pady=(2, 6))

        # ── Interactive Filter Toolbar (In-App modal only — No external page) ──
        ctrl_f = tk.Frame(win, bg="#0d1117")
        ctrl_f.pack(fill="x", padx=12, pady=(0, 6))

        modal_filter_var = tk.StringVar(value="PLAN" if (nd_plan and nd_plan.get("setups")) else "15M")
        include_s3_var   = tk.BooleanVar(value=True)

        tree_f = tk.Frame(win, bg="#161b22")
        tree_f.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        cols = (
            "Focus Group", "Action / Setup", "Type", "Strike",
            "Planned Entry", "Planned SL", "T1", "T2 (Pivot P)", "T3", "T4", "R:R",
            "S3", "S2", "S1", "P", "R1", "R2", "R3"
        )
        tw = ttk.Treeview(tree_f, columns=cols, show="headings", height=18)
        col_w = {
            "Focus Group": 110, "Action / Setup": 140, "Type": 45, "Strike": 60,
            "Planned Entry": 85, "Planned SL": 75, "T1": 60, "T2 (Pivot P)": 80,
            "T3": 60, "T4": 60, "R:R": 55,
            "S3": 55, "S2": 55, "S1": 55, "P": 55,
            "R1": 55, "R2": 55, "R3": 55
        }
        for c in cols:
            tw.heading(c, text=c)
            tw.column(c, width=col_w.get(c, 60), anchor="center")

        scroll = ttk.Scrollbar(tree_f, orient="vertical", command=tw.yview)
        h_scroll = ttk.Scrollbar(tree_f, orient="horizontal", command=tw.xview)
        tw.configure(yscrollcommand=scroll.set, xscrollcommand=h_scroll.set)
        tw.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        h_scroll.pack(side="bottom", fill="x")

        tw.tag_configure("ce", foreground="#3fb950")
        tw.tag_configure("pe", foreground="#f85149")

        def populate_tree():
            for item in tw.get_children():
                tw.delete(item)

            mode = modal_filter_var.get()
            inc_s3 = include_s3_var.get()

            def get_strike_data(opt_type, strike_num):
                for tok, s_info in strikes_dict.items():
                    if (s_info.get("opt_type") == opt_type and int(s_info.get("strike", 0)) == int(strike_num)
                            and (not exp or s_info.get("expiry") == exp)):
                        return s_info
                return None

            rows_to_display = []

            # Next-Day Plan Setups
            if mode == "PLAN" and nd_plan and nd_plan.get("setups"):
                for stp in nd_plan.get("setups", []):
                    ot = stp.get("type", "CE")
                    tag = "ce" if ot == "CE" else "pe"
                    stk = stp.get("strike", 0)
                    fg = f"Plan ({stp.get('timeframe', 'Intraday')})"
                    setup_lbl = stp.get("setup") or f"{stp.get('side')} ({stp.get('calculated_level', '')})"
                    ep_v = float(stp.get("planned_ep") or stp.get("entry_price") or 0.0)
                    sl_v = float(stp.get("sl") or 0.0)
                    t1_v = float(stp.get("target_1") or 0.0)
                    t2_v = float(stp.get("target_2") or 0.0)
                    t3_v = float(stp.get("target_3") or 0.0)
                    t4_v = float(stp.get("target_4") or 0.0)
                    rr_v = str(stp.get("rr", "-"))
                    sd = get_strike_data(ot, stk)
                    if sd:
                        s3 = sd.get("S3", 0); s2 = sd.get("S2", 0); s1 = sd.get("S1", 0)
                        p = sd.get("P", 0); r1 = sd.get("R1", 0); r2 = sd.get("R2", 0); r3 = sd.get("R3", 0)
                        tw.insert("", "end", values=(
                            fg, setup_lbl, ot, stk,
                            f"{ep_v:.2f}", f"{sl_v:.2f}", f"{t1_v:.2f}", f"{t2_v:.2f}", f"{t3_v:.2f}", f"{t4_v:.2f}", rr_v,
                            f"{s3:.2f}", f"{s2:.2f}", f"{s1:.2f}", f"{p:.2f}", f"{r1:.2f}", f"{r2:.2f}", f"{r3:.2f}"
                        ), tags=(tag,))
                    else:
                        tw.insert("", "end", values=(
                            fg, setup_lbl, ot, stk,
                            f"{ep_v:.2f}", f"{sl_v:.2f}", f"{t1_v:.2f}", f"{t2_v:.2f}", f"{t3_v:.2f}", f"{t4_v:.2f}", rr_v,
                            "-", "-", "-", "-", "-", "-", "-"
                        ), tags=(tag,))
                return

            # 15M Intraday strikes: 2 CE + 2 PE (or up to 6 with S3/R3)
            if mode in ("15M", "BOTH"):
                if f15.get("CE1"):
                    rows_to_display.append(("15M Intraday", "★ BUY (S2 Reclaim)", "CE", f15.get("CE1")))
                    rows_to_display.append(("15M Intraday", "▼ SELL (R2 Reject)", "CE", f15.get("CE1")))
                if f15.get("CE2"):
                    rows_to_display.append(("15M Intraday", "★ BUY (S2 Reclaim)", "CE", f15.get("CE2")))
                    rows_to_display.append(("15M Intraday", "▼ SELL (R2 Reject)", "CE", f15.get("CE2")))
                if inc_s3 and f15.get("CE3"):
                    rows_to_display.append(("15M Intraday", "★ BUY (S2 Reclaim)", "CE", f15.get("CE3")))
                    rows_to_display.append(("15M Intraday", "▼ SELL (R2 Reject)", "CE", f15.get("CE3")))

                if f15.get("PE1"):
                    rows_to_display.append(("15M Intraday", "★ BUY (S2 Reclaim)", "PE", f15.get("PE1")))
                    rows_to_display.append(("15M Intraday", "▼ SELL (R2 Reject)", "PE", f15.get("PE1")))
                if f15.get("PE2"):
                    rows_to_display.append(("15M Intraday", "★ BUY (S2 Reclaim)", "PE", f15.get("PE2")))
                    rows_to_display.append(("15M Intraday", "▼ SELL (R2 Reject)", "PE", f15.get("PE2")))
                if inc_s3 and f15.get("PE3"):
                    rows_to_display.append(("15M Intraday", "★ BUY (S2 Reclaim)", "PE", f15.get("PE3")))
                    rows_to_display.append(("15M Intraday", "▼ SELL (R2 Reject)", "PE", f15.get("PE3")))

            # 1H Structure strikes: 2 CE + 2 PE (or up to 6 with S3/R3)
            if mode in ("1H", "BOTH"):
                if f1h.get("CE1"):
                    rows_to_display.append(("1H Structure", "★ BUY (S2 Reclaim)", "CE", f1h.get("CE1")))
                    rows_to_display.append(("1H Structure", "▼ SELL (R2 Reject)", "CE", f1h.get("CE1")))
                if f1h.get("CE2"):
                    rows_to_display.append(("1H Structure", "★ BUY (S2 Reclaim)", "CE", f1h.get("CE2")))
                    rows_to_display.append(("1H Structure", "▼ SELL (R2 Reject)", "CE", f1h.get("CE2")))
                if inc_s3 and f1h.get("CE3"):
                    rows_to_display.append(("1H Structure", "★ BUY (S2 Reclaim)", "CE", f1h.get("CE3")))
                    rows_to_display.append(("1H Structure", "▼ SELL (R2 Reject)", "CE", f1h.get("CE3")))

                if f1h.get("PE1"):
                    rows_to_display.append(("1H Structure", "★ BUY (S2 Reclaim)", "PE", f1h.get("PE1")))
                    rows_to_display.append(("1H Structure", "▼ SELL (R2 Reject)", "PE", f1h.get("PE1")))
                if f1h.get("PE2"):
                    rows_to_display.append(("1H Structure", "★ BUY (S2 Reclaim)", "PE", f1h.get("PE2")))
                    rows_to_display.append(("1H Structure", "▼ SELL (R2 Reject)", "PE", f1h.get("PE2")))
                if inc_s3 and f1h.get("PE3"):
                    rows_to_display.append(("1H Structure", "★ BUY (S2 Reclaim)", "PE", f1h.get("PE3")))
                    rows_to_display.append(("1H Structure", "▼ SELL (R2 Reject)", "PE", f1h.get("PE3")))

            if mode == "ALL":
                sorted_items = sorted(strikes_dict.values(), key=lambda x: (x.get("strike", 0), x.get("opt_type", "")))
                for item in sorted_items:
                    ot = item.get("opt_type", "CE")
                    tag = "ce" if ot == "CE" else "pe"
                    stk = item.get("strike", 0)
                    fg = "All Mapped"
                    if stk in set(f15.get("all_4", [])) and stk in set(f1h.get("all_4", [])):
                        fg = "⭐15M & ⏳1H"
                    elif stk in set(f15.get("all_4", [])):
                        fg = "⭐15M Intraday"
                    elif stk in set(f1h.get("all_4", [])):
                        fg = "⏳1H Structure"

                    s3 = item.get("S3", 0)
                    s2 = item.get("S2", 0)
                    s1 = item.get("S1", 0)
                    p  = item.get("P", 0)
                    r1 = item.get("R1", 0)
                    r2 = item.get("R2", 0)
                    r3 = item.get("R3", 0)

                    # BUY from S2 (Buyers Area)
                    entry_b = s2
                    sl_b = s3 if s3 > 0 else round(s2 * 0.90, 2)
                    t1_b, t2_b, t3_b, t4_b = s1, p, r1, r2
                    rr_b = f"{((t2_b - entry_b) / max(entry_b - sl_b, 0.5)):.1f}:1" if entry_b > sl_b else "-"
                    tw.insert("", "end", values=(
                        fg, "★ BUY (S2 Reclaim)", ot, stk,
                        f"{entry_b:.2f}", f"{sl_b:.2f}", f"{t1_b:.2f}", f"{t2_b:.2f}", f"{t3_b:.2f}", f"{t4_b:.2f}", rr_b,
                        f"{s3:.2f}", f"{s2:.2f}", f"{s1:.2f}", f"{p:.2f}", f"{r1:.2f}", f"{r2:.2f}", f"{r3:.2f}"
                    ), tags=(tag,))

                    # SELL from R2 (Sellers Area)
                    entry_s = r2
                    sl_s = r3 if r3 > 0 else round(r2 * 1.10, 2)
                    t1_s, t2_s, t3_s, t4_s = r1, p, s1, s2
                    rr_s = f"{((entry_s - t2_s) / max(sl_s - entry_s, 0.5)):.1f}:1" if sl_s > entry_s else "-"
                    tw.insert("", "end", values=(
                        fg, "▼ SELL (R2 Reject)", ot, stk,
                        f"{entry_s:.2f}", f"{sl_s:.2f}", f"{t1_s:.2f}", f"{t2_s:.2f}", f"{t3_s:.2f}", f"{t4_s:.2f}", rr_s,
                        f"{s3:.2f}", f"{s2:.2f}", f"{s1:.2f}", f"{p:.2f}", f"{r1:.2f}", f"{r2:.2f}", f"{r3:.2f}"
                    ), tags=(tag,))
                return

            # Insert focus rows
            seen = set()
            for grp, role, ot, stk in rows_to_display:
                if (grp, role, ot, stk) in seen: continue
                seen.add((grp, role, ot, stk))
                sd = get_strike_data(ot, stk)
                tag = "ce" if ot == "CE" else "pe"
                if sd:
                    s3 = sd.get("S3", 0)
                    s2 = sd.get("S2", 0)
                    s1 = sd.get("S1", 0)
                    p  = sd.get("P", 0)
                    r1 = sd.get("R1", 0)
                    r2 = sd.get("R2", 0)
                    r3 = sd.get("R3", 0)

                    if "BUY" in role:
                        entry = s2
                        sl = s3 if s3 > 0 else round(s2 * 0.90, 2)
                        t1, t2, t3, t4 = s1, p, r1, r2
                        rr = f"{((t2 - entry) / max(entry - sl, 0.5)):.1f}:1" if entry > sl else "-"
                    else:
                        entry = r2
                        sl = r3 if r3 > 0 else round(r2 * 1.10, 2)
                        t1, t2, t3, t4 = r1, p, s1, s2
                        rr = f"{((entry - t2) / max(sl - entry, 0.5)):.1f}:1" if sl > entry else "-"

                    tw.insert("", "end", values=(
                        grp,
                        role,
                        ot,
                        stk,
                        f"{entry:.2f}",
                        f"{sl:.2f}",
                        f"{t1:.2f}",
                        f"{t2:.2f}",
                        f"{t3:.2f}",
                        f"{t4:.2f}",
                        rr,
                        f"{s3:.2f}",
                        f"{s2:.2f}",
                        f"{s1:.2f}",
                        f"{p:.2f}",
                        f"{r1:.2f}",
                        f"{r2:.2f}",
                        f"{r3:.2f}",
                    ), tags=(tag,))
                else:
                    tw.insert("", "end", values=(
                        grp, role, ot, stk,
                        "-", "-", "-", "-", "-", "-", "-",
                        "-", "-", "-", "-", "-", "-", "-"
                    ), tags=(tag,))

        # Pack radio buttons and checkboxes
        for val, txt in [
            ("PLAN", "📋 Next-Day Plan Setups"),
            ("15M", "⭐ 15M Intraday (2 CE + 2 PE)"),
            ("1H", "⏳ 1H Structure (2 CE + 2 PE)"),
            ("BOTH", "🎯 Both 15M & 1H Focus"),
            ("ALL", "All Mapped Strikes"),
        ]:
            tk.Radiobutton(ctrl_f, text=txt, value=val, variable=modal_filter_var,
                           bg="#0d1117", fg="#f0f6fc", selectcolor="#21262d",
                           activebackground="#0d1117", activeforeground="#58a6ff",
                           font=("Consolas", 8, "bold"), command=populate_tree).pack(side="left", padx=4)

        tk.Checkbutton(ctrl_f, text="Include S3 / R3 (Max 6 strikes)", variable=include_s3_var,
                       bg="#0d1117", fg="#ffd700", selectcolor="#21262d",
                       activebackground="#0d1117", activeforeground="#ffd700",
                       font=("Consolas", 8), command=populate_tree).pack(side="left", padx=10)

        def _refresh_in_modal():
            self._set_status(f"Mapping focus strikes for {sym}...", "#58a6ff")
            def _map_job():
                nonlocal strikes_dict
                strikes_dict = feed.fetch_and_map_all_strikes(sym, exp, force_fetch=True)
                win.after(0, populate_tree)
                self.root.after(0, lambda: self._set_status("✅ Focus strike levels refreshed", "#3fb950"))
            threading.Thread(target=_map_job, daemon=True).start()

        DarkButton(ctrl_f, text="🔄 Refresh Focus Levels", command=_refresh_in_modal,
                   bg="#1f6feb", fg="#ffffff",
                   font=("Consolas", 9, "bold"),
                   padx=12, pady=5,
                   activebackground="#388bfd", activeforeground="#ffffff").pack(side="right", padx=4)

        populate_tree()

    def _on_gap_check_click(self):
        sym = self.symbol_var.get()
        try:
            from eod_planner import detect_gap
            res = detect_gap(sym)
            if res:
                gt = res.get("type", "NORMAL")
                gp = res.get("gap_pct", 0.0)
                msg = f"{sym} Open Gap: {gp:+.2f}% ({gt})\n\n"
                if gt == "GAP_UP":
                    msg += "Market opened higher -> Watch for R2 Rejection (SELL setups) first."
                elif gt == "GAP_DOWN":
                    msg += "Market opened lower -> Watch for S2 Reclaim (BUY setups) first."
                else:
                    msg += "Normal open -> All planned pivot levels are valid as-is."
                messagebox.showinfo(f"{sym} Gap Detection", msg)
        except Exception as e:
            messagebox.showinfo("Gap Check", f"Gap check info: {e}")

    def _load_expiries(self):
        if Session.scrip_df is None: return
        exch = self.exchange_var.get()
        sym  = (self.symbol_var.get() or "").strip().upper()
        if not sym: return
        self.symbol_var.set(sym)
        df   = Session.scrip_df

        # Auto-detect exchange if current exchange doesn't have this symbol
        detected_exch = get_symbol_exchange(sym)
        if detected_exch != exch and exch not in ("BFO", "MCX"):
            self.exchange_var.set(detected_exch)
            exch = detected_exch

        seg_candidates = [exch]
        if exch == "BFO": seg_candidates = ["BFO", "BSE"]

        inst = EXCHANGE_CONFIG.get(exch, {}).get("inst_types", ["OPTIDX", "OPTSTK", "OPTFUT"])
        raw  = df[
            (df["name"] == sym) &
            (df["exch_seg"].isin(seg_candidates)) &
            (df["instrumenttype"].isin(inst))
        ]["expiry"].unique()

        if len(raw) == 0:
            raw = df[
                (df["name"] == sym) &
                (df["strike"].astype(float) > 0)
            ]["expiry"].unique()

        def parse_exp(e):
            try: return datetime.strptime(e, "%d%b%Y")
            except: return datetime.max

        expiries = sorted(raw, key=parse_exp)

        self.exp_cb["values"] = expiries
        self.next_exp_cb["values"] = expiries
        if expiries:
            self.expiry_var.set(expiries[0])
            # Auto-select next expiry if available
            if len(expiries) > 1:
                self.next_exp_var.set(expiries[1])
                self.use_next_exp.set(True)
            else:
                self.next_exp_var.set("")
        self._set_status(f"● {len(expiries)} expiries for {sym}")
        self._refresh_display()

    def _on_expiry_selected(self):
        self._refresh_display()

    def _on_next_expiry_selected(self):
        self.use_next_exp.set(True)
        if hasattr(self, "exp_next_var"):
            self.exp_cur_var.set(False)
            self.exp_next_var.set(True)
        self._sync_expiry_filter_var()
        self._refresh_display()

    def _sync_expiry_filter_var(self):
        """Keep the legacy ALL/CURRENT/NEXT value in step with the expiry tick boxes."""
        if not hasattr(self, "exp_cur_var"):
            return
        cur, nxt = self.exp_cur_var.get(), self.exp_next_var.get()
        self.expiry_filter_var.set("CURRENT" if (cur and not nxt) else ("NEXT" if (nxt and not cur) else "ALL"))

    def _dash_selected_filters(self) -> dict:
        sel = {}
        for (grp, key), v in getattr(self, "dash_filter_vars", {}).items():
            sel.setdefault(grp, set())
            try:
                if v.get():
                    sel[grp].add(key)
            except Exception:
                pass
        return sel

    def _on_dash_filter_changed(self):
        self._sync_expiry_filter_var()
        self._refresh_display()

    def _clear_dash_filters(self):
        for v in getattr(self, "dash_filter_vars", {}).values():
            v.set(False)
        for name in ("exp_cur_var", "exp_next_var", "show_plan_cols_var"):
            if hasattr(self, name):
                getattr(self, name).set(False)
        self._on_dash_filter_changed()

    def _refresh_display(self):
        """
        Request a UI refresh. Exactly ONE self-rescheduling refresh chain ever runs.
        (Before: every status update started a NEW 300ms chain, so after an hour
        thousands of chains flooded Tk and the dashboard froze / stopped opening.)
        """
        if getattr(self, "_refresh_chain_running", False):
            return
        self._refresh_chain_running = True
        self._refresh_tick()

    def _refresh_tick(self):
        try:
            self._refresh_display_body()
        except Exception as _re:
            # A display error must never kill the refresh loop
            now = time.time()
            if now - getattr(self, "_last_refresh_err", 0) > 30:
                self._last_refresh_err = now
                import traceback
                print(f"[UI] refresh error (dashboard keeps running): {_re}")
                traceback.print_exc()
        finally:
            self._tk_after(300, self._refresh_tick)

    def _refresh_display_body(self):
        sym = self.symbol_var.get()
        with feed.lock:
            snap    = dict(feed.live_data)
            meta    = dict(feed.token_to_meta)
            spot    = feed.spot_price.get(sym, 0)
            pivots  = feed.spot_pivots.get(sym)
            zone    = feed.spot_zone.get(sym, "Monitoring")
            watch   = list(feed.s1_s2_watchlist)
            alerts  = list(feed.reversal_alerts)
            mtf     = dict(feed.mtf_pivots)
            focus   = dict(feed.daily_focus)
            mkt_dir = dict(feed.market_direction.get(sym, {}))
            h1b     = dict(feed.h1_bias.get(sym, {}))

        # Update Spot + Market Direction info
        if spot > 0:
            dir_label = mkt_dir.get("direction", "...")
            dir_color = mkt_dir.get("color", "#ffd700")
            self.spot_info.set(f"{sym} @ {spot:.2f}  |  {dir_label}  |  Zone: {zone}")
            self.spot_lbl.config(fg=dir_color)
        else:
            self.spot_info.set(f"{sym} Spot: -- | Direction: Initializing...")

        # 15M Intraday and 1H Structure displays
        cfg = EXCHANGE_CONFIG.get(next((k for k,v in EXCHANGE_CONFIG.items() if sym in v["symbols"]), "NFO"), {})
        step = cfg.get("steps", {}).get(sym, 50)
        mtf = mtf or feed.mtf_pivots or get_tv_multi_timeframe_pivots(sym)
        focus_calc = focus or feed.daily_focus or select_daily_4_focus_strikes(mtf, step)
        f15 = feed.focus_15m or focus_calc.get("15M", {})
        f1h = feed.focus_1h  or focus_calc.get("1H", {})
        p15 = mtf.get("15-Min", {})
        p1h = mtf.get("1-Hour", {})

        if p15:
            self.sec_15m_info.set(
                f"⏱️ 15M INTRADAY: P:{p15.get('P','-')} | S1:{p15.get('S1','-')} S2:{p15.get('S2','-')} S3:{p15.get('S3','-')} | R1:{p15.get('R1','-')} R2:{p15.get('R2','-')} R3:{p15.get('R3','-')}  ||  "
                f"15M Strikes ➜ CE@{f15.get('CE1','-')}/{f15.get('CE2','-')}  P@{'/'.join(str(x) for x in f15.get('p_strikes', [])) or f15.get('P_STRIKE','-')} (CE+PE)  PE@{f15.get('PE1','-')}/{f15.get('PE2','-')}"
            )
        else:
            self.sec_15m_info.set("⏱️ 15M Intraday: Loading levels...")

        if p1h:
            bias_txt = f" (Bias: {h1b.get('bias','-')})" if h1b else ""
            self.sec_1h_info.set(
                f"⏳ 1H STRUCTURE: P:{p1h.get('P','-')} | S1:{p1h.get('S1','-')} S2:{p1h.get('S2','-')} S3:{p1h.get('S3','-')} | R1:{p1h.get('R1','-')} R2:{p1h.get('R2','-')} R3:{p1h.get('R3','-')}  ||  "
                f"1H Strikes ➜ CE@{f1h.get('CE1','-')}/{f1h.get('CE2','-')}  P@{'/'.join(str(x) for x in f1h.get('p_strikes', [])) or f1h.get('P_STRIKE','-')} (CE+PE)  PE@{f1h.get('PE1','-')}/{f1h.get('PE2','-')}{bias_txt}"
            )
        else:
            self.sec_1h_info.set("⏳ 1H Structure: Loading levels...")

        # Multi-Timeframe Pivot zones with levels (all timeframes)
        if mtf:
            tf_tags = {
                "Weekly": "[WEEKLY] ",
                "Daily":  "[DAILY]  ",
                "1-Hour": "[1-HOUR] ",
                "15-Min": "[15-MIN] ",
            }
            mtf_lines = []
            for tf in ["Weekly", "Daily", "1-Hour", "15-Min"]:
                tp = mtf.get(tf)
                tag = tf_tags.get(tf, f"[{tf:<7}] ")
                if tp:
                    zd = get_pivot_zone_detail(spot, tp) if spot > 0 else {"zone": "--"}
                    zone_str = f"ZONE: {zd['zone']:<11}"

                    def _fmt(val):
                        try:
                            return f"{float(val):>8.2f}"
                        except Exception:
                            return f"{str(val):>8}"

                    p_val  = _fmt(tp.get('P', 0))
                    s1_val = _fmt(tp.get('S1', 0))
                    s2_val = _fmt(tp.get('S2', 0))
                    s3_val = _fmt(tp.get('S3', 0))
                    r1_val = _fmt(tp.get('R1', 0))
                    r2_val = _fmt(tp.get('R2', 0))
                    r3_val = _fmt(tp.get('R3', 0))

                    line = (
                        f"{tag} {zone_str} │ "
                        f"P: {p_val} │ "
                        f"S1: {s1_val}  S2: {s2_val}  S3: {s3_val} │ "
                        f"R1: {r1_val}  R2: {r2_val}  R3: {r3_val}"
                    )
                    if hasattr(self, "mtf_vars") and tf in self.mtf_vars:
                        self.mtf_vars[tf].set(line)
                    mtf_lines.append(line)
                else:
                    if hasattr(self, "mtf_vars") and tf in self.mtf_vars:
                        self.mtf_vars[tf].set(f"{tag} ZONE: --        │ P:      -- │ S1:      --  S2:      --  S3:      -- │ R1:      --  R2:      --  R3:      --")
            self.mtf_info.set("\n".join(mtf_lines) if mtf_lines else "MTF: fetching...")

        sel_filters = self._dash_selected_filters() if hasattr(self, "dash_filter_vars") else {}

        # 15M Focus: exactly 2 CE + 2 PE
        ce_15m = [s for s in f15.get("ce_strikes", [f15.get("CE1"), f15.get("CE2")]) if s and s > 0][:2]
        pe_15m = [s for s in f15.get("pe_strikes", [f15.get("PE1"), f15.get("PE2")]) if s and s > 0][:2]
        focus_15m_strikes = set(ce_15m + pe_15m + list(f15.get("p_strikes") or ([f15["P_STRIKE"]] if f15.get("P_STRIKE") else [])))

        # 1H Focus: exactly 2 CE + 2 PE
        ce_1h = [s for s in f1h.get("ce_strikes", [f1h.get("CE1"), f1h.get("CE2")]) if s and s > 0][:2]
        pe_1h = [s for s in f1h.get("pe_strikes", [f1h.get("PE1"), f1h.get("PE2")]) if s and s > 0][:2]
        focus_1h_strikes = set(ce_1h + pe_1h + list(f1h.get("p_strikes") or ([f1h["P_STRIKE"]] if f1h.get("P_STRIKE") else [])))

        all_focus_strikes = focus_15m_strikes | focus_1h_strikes
        p_15m = set(f15.get("p_strikes") or ([f15["P_STRIKE"]] if f15.get("P_STRIKE") else []))
        p_1h  = set(f1h.get("p_strikes") or ([f1h["P_STRIKE"]] if f1h.get("P_STRIKE") else []))
        # Only the strike nearest each P is coloured / marked 🔷 (its neighbours trade but look normal)
        p_exact = {x for x in (f15.get("P_STRIKE"), f1h.get("P_STRIKE")) if x}

        # ── Ensure all focus strikes (both CE and PE for 15M and 1H) are always present in watch ──
        existing_keys = {(w.get("type"), w.get("strike"), w.get("expiry", "-")) for w in watch}
        cache_file = os.path.join(BASE_DIR, "strike_pivot_cache.json")
        try:
            mtime = os.path.getmtime(cache_file) if os.path.exists(cache_file) else 0
        except OSError:
            mtime = 0
        if not hasattr(self, "_cached_strikes") or getattr(self, "_cached_strikes_mtime", -1) != mtime:
            self._cached_strikes_mtime = mtime
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as cf:
                        _cj = json.load(cf)
                        _ok = _cj.get("basis_key") == f"{pivot_session_date().isoformat()}:session"
                        self._cached_strikes = _cj.get("strikes", {}) if _ok else {}
                        self._cached_nearest_exp = _cj.get("expiry", "")
                except Exception:
                    self._cached_strikes = getattr(self, "_cached_strikes", {})
            else:
                self._cached_strikes = {}
            self._cache_by_key = {}
            self._cache_by_type_strike = {}
            for _tok, _si in self._cached_strikes.items():
                _exp = _si.get("expiry", "-")
                self._cache_by_key[(_si.get("opt_type"), _si.get("strike"), _exp)] = _si
                if _exp == getattr(self, "_cached_nearest_exp", _exp):   # short key = nearest expiry only
                    self._cache_by_type_strike[(_si.get("opt_type"), _si.get("strike"))] = _si

        cached_strikes = getattr(self, "_cached_strikes", {})
        _cache_by_key  = getattr(self, "_cache_by_key", {})
        _cache_by_ts   = getattr(self, "_cache_by_type_strike", {})

        def _live_ltp(ot, stk, exp=None):
            v = feed.get_strike_ltp(ot, stk)
            if v > 0:
                return v
            if exp:
                ci = _cache_by_key.get((ot, stk, exp))
                if ci:
                    return float(ci.get("prev_c", 0) or 0)
            return float((_cache_by_ts.get((ot, stk)) or {}).get("prev_c", 0) or 0)

        # ── Reversal pairing per timeframe ──
        #   CE on support (CE LTP <= its P) -> BUY CE @S2  + SELL PE @R2
        #   CE at resistance (CE LTP > P)   -> SELL CE @R2 + BUY PE @S2
        def _ce_sup(ce_list):
            if not ce_list:
                return True
            with feed.lock:
                _sts = {s: feed.strategy_states.get(("CE", s)) for s in ce_list}
            for s in ce_list:
                st = _sts.get(s)
                stv = (getattr(st, "state", "") or "") if st else ""
                if stv == "CONFIRMED_BUY" or stv.startswith("RECLAIM_S"):
                    return True
                if stv == "CONFIRMED_SELL" or stv.startswith("RECLAIM_R"):
                    return False
            sup = res = 0
            for s in ce_list:
                piv = feed.strike_pivots.get(("CE", s)) or _cache_by_ts.get(("CE", s)) or {}
                p = float(piv.get("P", 0) or 0)
                l = _live_ltp("CE", s)
                if p <= 0 or l <= 0:
                    continue
                if l <= p: sup += 1
                else:      res += 1
            return sup >= res

        ce_sup_15m = _ce_sup(ce_15m)
        ce_sup_1h  = _ce_sup(ce_1h) if ce_1h else ce_sup_15m

        def _cos_for(ot, stk):
            if (ot == "CE" and stk in ce_15m) or (ot == "PE" and stk in pe_15m):
                return ce_sup_15m
            if (ot == "CE" and stk in ce_1h) or (ot == "PE" and stk in pe_1h):
                return ce_sup_1h
            return ce_sup_15m

        ce_on_support = ce_sup_15m

        nearby_set = getattr(feed, "nearby_pivot_strikes", set())
        needed_focus = []
        for s in ce_15m: needed_focus.append(("CE", s, True, s in ce_1h, False))
        for s in pe_15m: needed_focus.append(("PE", s, True, s in pe_1h, False))
        for s in ce_1h:
            if s not in ce_15m: needed_focus.append(("CE", s, False, True, False))
        for s in pe_1h:
            if s not in pe_15m: needed_focus.append(("PE", s, False, True, False))
        for s in sorted(nearby_set):
            if s not in ce_15m and s not in ce_1h:
                needed_focus.append(("CE", s, False, False, True))
            if s not in pe_15m and s not in pe_1h:
                needed_focus.append(("PE", s, False, False, True))
        # P-level strikes are traded on both CE and PE
        _have = {(ot, stk) for ot, stk, _, _, _ in needed_focus}
        for s in sorted(p_15m | p_1h):
            for _ot in ("CE", "PE"):
                if (_ot, s) not in _have:
                    needed_focus.append((_ot, s, s in p_15m, s in p_1h, False))

        # ── Ensure only strategy-required focus strikes are present in watch ──
        needed_focus_set = {(ot, stk) for ot, stk, _, _, _ in needed_focus}
        watch = [w for w in watch if (w.get("type"), w.get("strike")) in needed_focus_set]
        existing_keys = {(w.get("type"), w.get("strike"), w.get("expiry", "-")) for w in watch}

        curr_exp = (self.expiry_var.get() or "").strip()
        next_exp = (self.next_exp_var.get() or "").strip()
        exp_filt = self.expiry_filter_var.get() if hasattr(self, "expiry_filter_var") else "ALL"

        if exp_filt == "NEXT" and next_exp:
            target_expiries = [next_exp]
        elif exp_filt == "CURRENT" and curr_exp:
            target_expiries = [curr_exp]
        else:
            target_expiries = [e for e in [curr_exp, next_exp] if e]
            if not target_expiries:
                target_expiries = [curr_exp or "-"]

        for tgt_exp in target_expiries:
            for ot, stk, is_15m, is_1h, is_nearby in needed_focus:
                strike_tag = "[⭐15M & ⏳1H]" if (is_15m and is_1h) else ("[⭐15M Focus]" if is_15m else ("[⏳1H Focus]" if is_1h else ("[📌Nearby Pivot]" if is_nearby else "")))
                if (ot, stk, tgt_exp) not in existing_keys:
                    # Levels of this exact contract only (never another expiry's, never estimated)
                    s_info = feed.strike_pivots.get((ot, stk, tgt_exp)) or _cache_by_key.get((ot, stk, tgt_exp))
                    if s_info:
                        exp_val = tgt_exp
                        ltp_val = _live_ltp(ot, stk, exp_val)
                        s3_val = s_info.get("S3", 0.0)
                        s2_val = s_info.get("S2", 0.0)
                        s1_val = s_info.get("S1", 0.0)
                        p_val  = s_info.get("P", 0.0)
                        r1_val = s_info.get("R1", 0.0)
                        r2_val = s_info.get("R2", 0.0)
                        r3_val = s_info.get("R3", 0.0)

                        entry_lvl_b = "S2" if s2_val > 1.0 else "S1"
                        entry_val_b = s2_val if s2_val > 1.0 else s1_val
                        tgts_b = [s1_val, p_val, r1_val, r2_val, r3_val] if entry_lvl_b == "S2" else [p_val, r1_val, r2_val, r3_val, r3_val * 1.05]
                        sig_b = f"★ Plan BUY @ {entry_lvl_b}:{entry_val_b:.1f} | LL SL (Waiting)"
                        zone_str_b = f"{strike_tag} Buy Zone".strip()

                        entry_lvl_s = "R2" if r2_val > 1.0 else "R1"
                        entry_val_s = r2_val if r2_val > 1.0 else r1_val
                        tgts_s = [r1_val, p_val, s1_val, s2_val, s3_val] if entry_lvl_s == "R2" else [p_val, s1_val, s2_val, s3_val, s3_val * 0.95]
                        sig_s = f"▼ Plan SELL @ {entry_lvl_s}:{entry_val_s:.1f} | HH SL (Waiting)"
                        zone_str_s = f"{strike_tag} Sell Area".strip()

                        watch.append({
                            "side": "BUY", "type": ot, "strike": stk, "ltp": ltp_val,
                            "high": s_info.get("prev_h", ltp_val), "low": s_info.get("prev_l", ltp_val),
                            "oi": s_info.get("oi", 0), "vol": s_info.get("vol", 0), "zone": zone_str_b, "expiry": exp_val,
                            "is_15m": is_15m, "is_1h": is_1h, "is_nearby": is_nearby,
                            "s3": s3_val, "s2": s2_val, "s1": s1_val, "p": p_val, "r1": r1_val, "r2": r2_val, "r3": r3_val,
                            "category": "CE_SUP" if ot == "CE" else "PE_SUP",
                            "status": sig_b, "m5_state": "⏳ Waiting", "trade_status": "Pending",
                            "ll": s_info.get("prev_l", 0.0), "hh": s_info.get("prev_h", 0.0), "sl": 0.0,
                            "entry_price": entry_val_b, "targets": tgts_b, "target": tgts_b[0] if tgts_b else 0.0,
                            "t1": tgts_b[0] if len(tgts_b) > 0 else 0.0, "t2": tgts_b[1] if len(tgts_b) > 1 else 0.0,
                            "t3": tgts_b[2] if len(tgts_b) > 2 else 0.0, "t4": tgts_b[3] if len(tgts_b) > 3 else 0.0,
                            "t5": tgts_b[4] if len(tgts_b) > 4 else 0.0,
                            "plan_s2": s2_val, "plan_s1": s1_val, "plan_p": p_val, "plan_r1": r1_val, "plan_r2": r2_val,
                            "plan_buy_zone": f"BUY {entry_lvl_b}={entry_val_b:.1f} | T1:{tgts_b[0]:.1f} P:{p_val:.1f}",
                            "plan_sell_zone": f"SELL R2={r2_val:.1f} | T1:{r1_val:.1f} P:{p_val:.1f}",
                        })
                        watch.append({
                            "side": "SELL", "type": ot, "strike": stk, "ltp": ltp_val,
                            "high": s_info.get("prev_h", ltp_val), "low": s_info.get("prev_l", ltp_val),
                            "oi": s_info.get("oi", 0), "vol": s_info.get("vol", 0), "zone": zone_str_s, "expiry": exp_val,
                            "is_15m": is_15m, "is_1h": is_1h, "is_nearby": is_nearby,
                            "s3": s3_val, "s2": s2_val, "s1": s1_val, "p": p_val, "r1": r1_val, "r2": r2_val, "r3": r3_val,
                            "category": "PE_RES" if ot == "PE" else "CE_RES",
                            "status": sig_s, "m5_state": "⏳ Waiting", "trade_status": "Pending",
                            "ll": s_info.get("prev_l", 0.0), "hh": s_info.get("prev_h", 0.0), "sl": 0.0,
                            "entry_price": entry_val_s, "targets": tgts_s, "target": tgts_s[0] if tgts_s else 0.0,
                            "t1": tgts_s[0] if len(tgts_s) > 0 else 0.0, "t2": tgts_s[1] if len(tgts_s) > 1 else 0.0,
                            "t3": tgts_s[2] if len(tgts_s) > 2 else 0.0, "t4": tgts_s[3] if len(tgts_s) > 3 else 0.0,
                            "t5": tgts_s[4] if len(tgts_s) > 4 else 0.0,
                            "plan_s2": s2_val, "plan_s1": s1_val, "plan_p": p_val, "plan_r1": r1_val, "plan_r2": r2_val,
                            "plan_buy_zone": f"BUY {entry_lvl_b}={entry_val_b:.1f} | T1:{tgts_b[0]:.1f} P:{p_val:.1f}",
                            "plan_sell_zone": f"SELL R2={r2_val:.1f} | T1:{r1_val:.1f} P:{p_val:.1f}",
                        })
                        existing_keys.add((ot, stk, tgt_exp))
        # ── Multi-select filters (side / type / timeframe / level / state) + expiry ──
        seen = set()
        display_watch = []
        _row_flags = {}
        _exp_cur = getattr(self, "exp_cur_var", None)
        _exp_nxt = getattr(self, "exp_next_var", None)
        _want_cur = bool(_exp_cur.get()) if _exp_cur else False
        _want_nxt = bool(_exp_nxt.get()) if _exp_nxt else False
        _allowed_exp = None
        if _want_cur != _want_nxt:          # exactly one ticked
            _e = curr_exp if _want_cur else next_exp
            _allowed_exp = {_e} if _e else None
        for w in watch:
            key = (w.get("type", "CE"), w.get("strike", 0), w.get("expiry", "-"), w.get("side", "BUY"))
            if key in seen:
                continue
            if _allowed_exp is not None and (w.get("expiry", "") or "").strip() not in _allowed_exp:
                continue
            fl = dash_row_flags(w, ce_15m, pe_15m, ce_1h, pe_1h, p_15m, p_1h, p_exact)
            if not (fl["on_plan"] or w.get("strike") in nearby_set):
                continue
            if not dash_row_passes(fl, sel_filters):
                continue
            seen.add(key)
            _row_flags[key] = fl
            display_watch.append(w)
        if hasattr(self, "dash_filter_info"):
            _n_tick = sum(len(v) for v in sel_filters.values()) + int(_want_cur) + int(_want_nxt)
            self.dash_filter_info.set(f"{len(display_watch)} rows" + (f" · {_n_tick} filter(s) on" if _n_tick else " · no filters"))

        # Sort matching reference screenshot: PE descending first, CE ascending next; BUY before SELL
        def _watch_sort_key(w):
            ot = w.get("type", "CE")
            stk = w.get("strike", 0)
            side = w.get("side", "BUY")
            type_order = 0 if ot == "PE" else 1
            strike_order = -stk if ot == "PE" else stk
            side_order = 0 if side == "BUY" else 1
            return (type_order, strike_order, side_order)

        display_watch = sorted(display_watch, key=_watch_sort_key)

        existing_items = self.tree.get_children()
        row_idx = 0

        # Broker order state per contract and side (latest order of the day wins)
        _order_map = {}
        try:
            _eng = live_order_manager.engine
            for _p in sorted(list(_eng.positions.values()), key=lambda q: q.get("placed_at", 0)):
                _i = _p["inst"]
                _lbl = _eng.order_status_for(_p["trade_id"])
                _order_map[(_i.get("opt_type"), str(_i.get("strike")), str(_i.get("expiry", "") or ""), _p["side"])] = _lbl
        except Exception:
            pass
        for w in display_watch[:100]:
            ll_v   = w.get("ll", 0) or w.get("low", 0)
            hh_v   = w.get("hh", 0) or w.get("high", 0)
            opt_t  = w.get("type", "CE")
            sig_t  = w.get("status", "")
            m5_t   = w.get("m5_state", "")
            zone_t = w.get("zone", "")
            sl_v   = w.get("sl", 0)

            is_buy = (w.get("side", "BUY") == "BUY")
            side_badge = "🟢 BUY" if is_buy else "🔴 SELL"

            ll_str = f"{ll_v:.2f}" if ll_v > 0 else "-"
            hh_str = f"{hh_v:.2f}" if hh_v > 0 else "-"

            trade_st = w.get("trade_status", "Waiting")
            ep_v   = w.get("entry_price", 0)

            if is_buy:
                s2_raw = w.get("s2", 0)
                s1_raw = w.get("s1", 0)
                is_s1_buy = (s2_raw <= 0.05 or "S1" in m5_t or "S1" in sig_t)
                planned_ep = round(s1_raw if is_s1_buy else s2_raw, 2)
                lvl_label = "S1" if is_s1_buy else "S2"
                s2_note = " [S2<=0]" if is_s1_buy else ""
                sl_str = display_sl(trade_st, m5_t, sl_v, True, planned_ep, ll=ll_v, entry_sl=w.get("entry_sl", 0))
            else:
                planned_ep = round(w.get("r2", 0) if w.get("r2", 0) > 1.0 else w.get("r1", 0), 2)
                lvl_label = "R2"
                s2_note = ""
                is_s1_buy = False
                sl_str = display_sl(trade_st, m5_t, sl_v, False, planned_ep, hh=hh_v, entry_sl=w.get("entry_sl", 0))

            # T1 / T2 / T3 / T4 from targets list or planned levels
            tgts = w.get("targets", [])
            if len(tgts) > 0 and tgts[0] > 0:
                t1_v = tgts[0]
                t2_v = tgts[1] if len(tgts) > 1 else 0
                t3_v = tgts[2] if len(tgts) > 2 else 0
                t4_v = tgts[3] if len(tgts) > 3 else 0
                t5_v = tgts[4] if len(tgts) > 4 else 0
            elif is_buy and is_s1_buy:
                # same ladder as the strategy: S1 entry -> P, R1, R2, R3, R3 x 1.05
                t1_v = w.get("p", 0)
                t2_v = w.get("r1", 0)
                t3_v = w.get("r2", 0)
                t4_v = w.get("r3", 0) or round(t3_v * 1.25, 2)
                t5_v = round(w.get("r3", 0) * 1.05, 2) if w.get("r3", 0) else 0
            elif is_buy:
                # S2 entry -> S1, P, R1, R2, R3
                t1_v = w.get("s1", 0)
                t2_v = w.get("p", 0)
                t3_v = w.get("r1", 0)
                t4_v = w.get("r2", 0) or round(t3_v * 1.25, 2)
                t5_v = w.get("r3", 0)
            else:
                # R2 entry -> R1, P, S1, S2, S3
                t1_v = w.get("r1", 0)
                t2_v = w.get("p", 0)
                t3_v = w.get("s1", 0)
                t4_v = w.get("s2", 0) or round(t3_v * 0.80, 2)
                t5_v = w.get("s3", 0)

            t1_str = f"{t1_v:.2f}" if t1_v and t1_v > 0 else "-"
            t2_str = f"{t2_v:.2f}" if t2_v and t2_v > 0 else "-"
            t3_str = f"{t3_v:.2f}" if t3_v and t3_v > 0 else "-"
            t4_str = f"{t4_v:.2f}" if t4_v and t4_v > 0 else "-"
            t5_str = f"{t5_v:.2f}" if t5_v and t5_v > 0 else "-"

            _fl = _row_flags.get((opt_t, w.get("strike", 0), w.get("expiry", "-"), w.get("side", "BUY"))) \
                  or dash_row_flags(w, ce_15m, pe_15m, ce_1h, pe_1h, p_15m, p_1h, p_exact)
            is_15m = _fl["is_15m"]
            is_1h  = _fl["is_1h"]
            is_p_row = _fl["is_p"]
            strike_tag = "[⭐15M & ⏳1H]" if (is_15m and is_1h) else ("[⭐15M Focus]" if is_15m else ("[⏳1H Focus]" if is_1h else ""))
            if is_p_row:
                strike_tag = f"[🔷P] {strike_tag}".strip()

            # Fast-decision signals & statuses for rapid execution
            if trade_st == "Active":
                disp_signal = f"🟢 LIVE LONG @ {ep_v:.2f} | SL: {sl_str} | T1: {t1_str}" if is_buy else f"🔴 LIVE SHORT @ {ep_v:.2f} | SL: {sl_str} | T1: {t1_str}"
            elif "Target 1" in trade_st:
                disp_signal = f"🎯 T1 HIT (+50% Booked) | SL: {sl_str} -> T2: {t2_str}"
            elif "Target 2" in trade_st:
                disp_signal = f"🎯 T2 HIT (+80% Booked) | SL: {sl_str} -> T3: {t3_str}"
            elif "Target 3" in trade_st:
                disp_signal = "🏆 T3 HIT — All Targets Hit"
            elif "SL" in trade_st:
                disp_signal = f"🛑 SL HIT ({sl_str}) — Trade Closed"
            elif sig_t and any(k in m5_t for k in ("SWEEP", "RECLAIM", "REJECT", "RETEST", "FAVOR", "CONFIRMED")):
                disp_signal = sig_t
            elif is_buy:
                disp_signal = f"★ Plan BUY @ {lvl_label}:{planned_ep:.1f} | LL SL (Watch {lvl_label} Sweep{s2_note})"
            else:
                disp_signal = f"▼ Plan SELL @ R2:{planned_ep:.1f} | HH SL (Watch R2 Sweep)"

            if is_buy:
                disp_zone = f"{strike_tag} Buy Zone Above {lvl_label} ({planned_ep:.1f}){s2_note}".strip()
            else:
                disp_zone = f"{strike_tag} Sell Area Below R2 ({planned_ep:.1f})".strip()

            # Clean readable 5M State & Trade Status icons
            m5_icons = {
                "WAITING":               "⏳ Waiting",
                "SWEEP_S1":              "🌊 Swept S1",
                "SWEEP_S2":              "🌊 Swept S2",
                "SWEEP_R2":              "🌊 Swept R2",
                "RECLAIM_S1":            "⚡ Close > S1",
                "RECLAIM_S2":            "⚡ Close > S2",
                "RECLAIM_R2":            "⚡ Close < R2",
                "RETEST_S1":             "🔄 Retest S1",
                "RETEST_S2":             "🔄 Retest S2",
                "RETEST_R2":             "🔄 Retest R2",
                "TRADING_IN_FAVOR_BUY":  "🚀 Push Up",
                "TRADING_IN_FAVOR_SELL": "🔻 Push Down",
                "CONFIRMED_BUY":         "✅ Confirmed Buy",
                "CONFIRMED_SELL":        "✅ Confirmed Sell",
            }
            disp_m5 = m5_icons.get(m5_t, m5_t)

            trade_st_map = {
                "Waiting":      "⚪ Pending",
                "Active":       "🟢 LIVE TRADE",
                "Target 1 Hit": "🎯 T1 Hit",
                "Target 2 Hit": "🎯 T2 Hit",
                "Target 3 Hit": "🏆 T3 Hit",
                "SL Hit":       "🛑 SL Hit",
            }
            disp_trade_st = trade_st_map.get(trade_st, trade_st)

            # Format OI and Volume
            oi_v  = w.get("oi",  0)
            vol_v = w.get("vol", 0)
            oi_disp  = f"{oi_v/100000:.1f}L"  if oi_v  >= 100000 else (f"{oi_v/1000:.1f}K"  if oi_v  >= 1000 else str(oi_v))
            vol_disp = f"{vol_v/100000:.1f}L" if vol_v >= 100000 else (f"{vol_v/1000:.1f}K" if vol_v >= 1000 else str(vol_v))

            # Row tag for high-contrast color coding
            if trade_st == "Active":
                row_tag = "active_buy" if is_buy else "active_sell"
            elif "Target" in trade_st:
                row_tag = "target"
            elif "SL" in trade_st:
                row_tag = "sl"
            elif any(k in m5_t for k in ("CONFIRMED", "RECLAIM", "REJECT", "TRADING_IN_FAVOR", "SWEEP")):
                row_tag = ("p_ready_buy" if is_buy else "p_ready_sell") if is_p_row else ("ready_buy" if is_buy else "ready_sell")
            else:
                row_tag = ("p_buy_row" if is_buy else "p_sell_row") if is_p_row else ("buy_row" if is_buy else "sell_row")

            entry_disp = f"{ep_v:.2f}" if ep_v and ep_v > 0 else (f"{planned_ep:.2f}" if planned_ep > 0 else "-")

            # Build plan columns
            plan_s2_raw = float(w.get("plan_s2", 0) or 0.0)
            plan_s2_v = max(plan_s2_raw, 0.05) if plan_s2_raw != 0 else 0.0
            plan_p_v  = float(w.get("plan_p",  0) or 0.0)
            plan_r2_v = float(w.get("plan_r2", 0) or 0.0)
            ltp_v     = float(w.get("ltp", 0.0) or 0.0)
            plan_s2_str = f"{plan_s2_v:.2f}" if plan_s2_v > 0.05 else ("0.05 (<=0)" if plan_s2_v > 0 else "-")
            plan_p_str  = f"{plan_p_v:.2f}"  if plan_p_v  != 0 else "-"
            plan_r2_str = f"{plan_r2_v:.2f}" if plan_r2_v != 0 else "-"
            plan_signal_str = (w.get("plan_buy_zone", "") if is_buy else w.get("plan_sell_zone", "")) or "-"
            if is_buy and is_s1_buy:
                plan_buy_ref = float(w.get("plan_s1", 0) or 0.0)
                if plan_buy_ref > 0 and ltp_v > 0:
                    vs_plan = "BUY (<S1)" if ltp_v <= plan_buy_ref else f"Near S1 (+{ltp_v - plan_buy_ref:.1f})"
                else:
                    vs_plan = "-"
            elif plan_s2_v != 0 and plan_r2_v != 0 and ltp_v > 0:
                if ltp_v <= plan_s2_v:
                    vs_plan = f"BUY (<S2)"
                elif ltp_v >= plan_r2_v:
                    vs_plan = f"SELL (>R2)"
                else:
                    gap_to_s2 = round(ltp_v - plan_s2_v, 1)
                    gap_to_r2 = round(plan_r2_v - ltp_v, 1)
                    vs_plan = f"Mid (+{gap_to_s2}/-{gap_to_r2})"
            else:
                vs_plan = "-"

            row_vals = (
                w.get("expiry", "-"),
                w["type"],
                f"🔷{w['strike']}" if is_p_row else w["strike"],
                side_badge,
                f"{w['ltp']:.2f}",
                oi_disp,
                vol_disp,
                disp_zone,
                f"{w.get('s3', 0.0):.2f}",
                f"{w.get('s2', 0.0):.2f}",
                f"{w.get('s1', 0.0):.2f}",
                f"{w.get('p', 0.0):.2f}",
                f"{w.get('r1', 0.0):.2f}",
                f"{w.get('r2', 0.0):.2f}",
                f"{w.get('r3', 0.0):.2f}",
                disp_m5,
                ll_str,
                hh_str,
                disp_signal,
                sl_str,
                entry_disp,
                t1_str,
                t2_str,
                t3_str,
                t4_str,
                t5_str,
                disp_trade_st,
                _order_map.get((w["type"], str(w["strike"]), str(w.get("expiry", "") or ""), w.get("side", ""))) or "",
                # Today's Levels plan columns (always present for Treeview consistency)
                plan_s2_str,
                plan_p_str,
                plan_r2_str,
                plan_signal_str,
                vs_plan,
            )

            if row_idx < len(existing_items):
                item_id = existing_items[row_idx]
                self.tree.item(item_id, values=row_vals, tags=(row_tag,))
            else:
                self.tree.insert("", "end", values=row_vals, tags=(row_tag,))
            row_idx += 1

        # Prune any excess rows from previous frame
        if row_idx < len(existing_items):
            for extra_id in existing_items[row_idx:]:
                self.tree.delete(extra_id)

        # Show/hide Today's Levels plan columns based on active filter
        _plan_cols   = ("Plan S2", "Plan P", "Plan R2", "Plan Signal", "vs Plan")
        _plan_widths = {"Plan S2": 65, "Plan P": 65, "Plan R2": 65, "Plan Signal": 160, "vs Plan": 90}
        _show_plan = bool(self.show_plan_cols_var.get()) if hasattr(self, "show_plan_cols_var") else False
        if _show_plan:
            for c in _plan_cols:
                self.tree.column(c, width=_plan_widths[c], minwidth=50)
                self.tree.heading(c, text=c)
        else:
            for c in _plan_cols:
                self.tree.column(c, width=0, minwidth=0)
                self.tree.heading(c, text="")

        # Max OI — keyed by (strike, expiry) to avoid dual-expiry overwrite
        if snap and meta:
            # Build {expiry: {strike: {CE, PE}}}
            oi_per_exp = {}
            for token, m in meta.items():
                if token not in snap: continue
                oi_v = snap[token]["oi"]
                s    = m["strike"]
                exp  = m.get("expiry", "-")
                ot   = m["opt_type"]
                oi_per_exp.setdefault(exp, {})
                oi_per_exp[exp].setdefault(s, {"CE": 0, "PE": 0})
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
            self.max_oi_info.set("  ||  ".join(parts) if parts else "")

        # ── 3:00 PM Square-off & 3:45 PM Next-Day Level Calculation ──
        _now_eod = datetime.now()
        _is_weekday = _now_eod.weekday() < 5
        _eod_date = _now_eod.strftime("%Y-%m-%d")

        # 1. 3:00 PM: Square off all open intraday trades at CMP if SL/Targets not hit
        if _is_weekday and _now_eod.time() >= EOD_SQUARE_OFF_TIME and getattr(self, '_ui_eod_sq_done', None) != _eod_date:
            self._ui_eod_sq_done = _eod_date
            try:
                _sq = feed.forward_tracker.eod_square_off("EOD 3PM Square-off @ CMP")
                if _sq:
                    self._set_status(f"⏰ 3:00 PM Square-off: {len(_sq)} trade(s) closed at CMP", "#ffd700")
                    feed.sync_forward_test_to_sheet()
                    feed.sync_multi_symbol_sheet()
                    print(f"\n[EOD UI] ⏰ 15:00:00 → Squared off {len(_sq)} open trade(s) at CMP")
            except Exception as _sq_err:
                print(f"[EOD UI 3PM] Square-off error: {_sq_err}")

        # 2. 3:45 PM: Calculate next-day levels + run eod_planner.py
        _after_close_ui = _is_weekday and _now_eod.time() >= NEXT_DAY_LEVELS_TIME
        if _after_close_ui and getattr(self, '_ui_eod_planner_done', None) != _eod_date:
            self._ui_eod_planner_done = _eod_date

            def _calc_next_day_bg():
                try:
                    curr_sym = self.symbol_var.get().strip().upper() or "NIFTY"
                    feed.build_next_day_levels(curr_sym)
                    feed.sync_multi_symbol_sheet()
                    print(f"[EOD UI] ✅ Next-day levels calculated & synced to Multi-Symbol Tracker sheet.")
                except Exception as _nd_err:
                    print(f"[EOD UI] Next-day calculation error: {_nd_err}")

            threading.Thread(target=_calc_next_day_bg, daemon=True).start()

        # (rescheduling is done by _refresh_tick — single chain)

    def _set_status(self,msg,color="#3fb950"):
        self.status_var.set(msg)
        self.status_lbl.config(fg=color)

    # ═══════════════════════════════════════════════════════
    # LIVE ORDERS — slim bar + Settings window
    # ═══════════════════════════════════════════════════════
    def _build_order_bar(self, bar):
        eng = live_order_manager.engine
        lbl = dict(bg="#0d1117", font=(GUI_FONT, 9, "bold"))
        self.ob_dot = tk.Label(bar, text="●", fg="#f85149", **lbl)
        self.ob_dot.pack(side="left", padx=(8, 2))
        self.ob_broker = tk.Label(bar, text=BROKER_LABELS.get(eng.settings["broker"], "-"), fg="#c9d1d9", **lbl)
        self.ob_broker.pack(side="left", padx=(0, 10))
        self.ob_auto = DarkButton(bar, text="AUTO-TRADE: OFF", command=self._toggle_auto_trade,
                                  bg="#21262d", fg="#8b949e", activebackground="#30363d",
                                  font=(GUI_FONT, 9, "bold"), padx=10, pady=3)
        self.ob_auto.pack(side="left", padx=4)
        self.ob_stats = tk.Label(bar, text="", fg="#8b949e", bg="#0d1117", font=(GUI_FONT, 9), anchor="w")
        self.ob_stats.pack(side="left", padx=10, fill="x", expand=True)
        DarkButton(bar, text="■ STOP", command=self._kill_switch, bg="#8b1a1a", fg="#ffffff",
                   activebackground="#da3633", font=(GUI_FONT, 9, "bold"), padx=10, pady=3).pack(side="right", padx=(4, 8))
        DarkButton(bar, text="⚙ Settings", command=self._open_order_settings, bg="#21262d", fg="#79c0ff",
                   activebackground="#30363d", font=(GUI_FONT, 9, "bold"), padx=10, pady=3).pack(side="right", padx=4)
        self.order_status_var = tk.StringVar(value="")      # kept for older callers
        self._order_bar_tick()
        threading.Thread(target=self._auto_connect_broker, daemon=True).start()

    def _auto_connect_broker(self):
        ok, msg = live_order_manager.engine.connect()
        print(f"[ORDERS] {'✅' if ok else '⚠️'} {msg}")

    def _order_bar_tick(self):
        try:
            s = live_order_manager.engine.summary()
            self.ob_dot.config(fg="#3fb950" if s["connected"] else "#f85149")
            self.ob_broker.config(text=s["broker"])
            if s["auto"]:
                self.ob_auto.config(text="AUTO-TRADE: ON", bg="#da3633", fg="#ffffff")
            else:
                self.ob_auto.config(text="AUTO-TRADE: OFF", bg="#21262d", fg="#8b949e")
            pnl = s["pnl_rs"]
            txt = f"Trades {s['trades']}  │  Open {s['open']}  │  P&L ₹{pnl:,.0f}"
            if not s["connected"]:
                txt += f"  │  {s['status'][:70]}"
            elif s["note"]:
                txt += f"  │  {s['note']}"
            self.ob_stats.config(text=txt, fg="#3fb950" if pnl > 0 else ("#f85149" if pnl < 0 else "#8b949e"))
        except Exception:
            pass
        self.root.after(1000, self._order_bar_tick)

    def _toggle_auto_trade(self):
        eng = live_order_manager.engine
        if eng.auto_trade:
            eng.set_auto(False)
            return
        if not eng.connected:
            messagebox.showwarning("Broker not connected",
                                   f"{eng.status_msg}\n\nOpen ⚙ Settings → Broker to connect.")
            return
        s = eng.settings
        lots = s["lots"]
        tfs = " and ".join(t for t, on in (("15M", s["trade_15m"]), ("1H", s["trade_1h"])) if on) or "no"
        ok = messagebox.askyesno(
            "Turn on LIVE auto-trading?",
            f"Real orders will be sent to {BROKER_LABELS.get(eng.broker_name, eng.broker_name)} "
            f"for every {tfs} entry.\n\n"
            f"Lots per trade: {lots}   Max trades/day: {s['max_trades']}   Max loss/day: ₹{s['max_loss_rs']:,}\n"
            f"SELL setups: {'traded' if s['take_sell_setups'] else 'skipped'}   "
            f"Stop-loss at broker: {'yes' if s['sl_at_broker'] else 'no'}\n\nTurn on auto-trading?",
            icon="warning")
        if ok:
            eng.set_auto(True, confirmed=True)

    def _kill_switch(self):
        eng = live_order_manager.engine
        n = sum(1 for p in list(eng.positions.values()) if p["state"] in ("OPENING", "OPEN"))
        if not messagebox.askyesno("STOP all trading?",
                                   f"Auto-trading goes OFF, pending orders are cancelled and "
                                   f"{n} open position(s) are exited at market.\n\nContinue?", icon="warning"):
            return
        eng.kill()

    def _open_order_settings(self):
        if getattr(self, "_ord_win", None) is not None and self._ord_win.winfo_exists():
            self._ord_win.lift()
            return
        eng = live_order_manager.engine
        w = tk.Toplevel(self.root)
        self._ord_win = w
        w.title("Order Settings")
        w.configure(bg="#0d1117")
        w.geometry("820x620")
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        L = dict(bg="#0d1117", fg="#c9d1d9", font=(GUI_FONT, 9))
        E = dict(bg="#21262d", fg="#f0f6fc", insertbackground="#f0f6fc", relief="flat", font=(GUI_FONT, 9))
        C = dict(bg="#0d1117", fg="#c9d1d9", selectcolor="#161b22", activebackground="#0d1117",
                 activeforeground="#58a6ff", font=(GUI_FONT, 9))

        # ── Broker tab ──
        tb = tk.Frame(nb, bg="#0d1117"); nb.add(tb, text=" Broker ")
        brk_var = tk.StringVar(value=eng.broker_name or eng.settings["broker"])
        row = tk.Frame(tb, bg="#0d1117"); row.pack(fill="x", padx=10, pady=(12, 6))
        tk.Label(row, text="Broker:", **L).pack(side="left")
        keys_frame = tk.Frame(tb, bg="#0d1117"); keys_frame.pack(fill="x", padx=10, pady=4)
        entries = {}
        hints = {
            "XTS_ROOT_URL": "blank = Jainam (https://jtrade.jainam.in:5000)",
            "XTS_CLIENT_ID": "only for dealer accounts; blank otherwise",
            "DHAN_ACCESS_TOKEN": "optional: made automatically from PIN + TOTP",
            "DHAN_TOTP_SECRET": "Google Authenticator secret (base32)",
            "ANGEL_CLIENT_ID": "uses the same login as the price feed",
        }

        def draw_keys(*_):
            for c in keys_frame.winfo_children():
                c.destroy()
            entries.clear()
            cls = ADAPTERS[brk_var.get()]
            for i, k in enumerate(cls.env_keys):
                tk.Label(keys_frame, text=k, anchor="w", width=20, **L).grid(row=2 * i, column=0, sticky="w", pady=(6, 0))
                secret = k in cls.secret_keys
                e = tk.Entry(keys_frame, width=38, show="•" if secret else "", **E)
                e.grid(row=2 * i, column=1, sticky="we", pady=(6, 0))
                cur = os.getenv(k, "")
                if cur and not secret:
                    e.insert(0, cur)
                entries[k] = (e, secret, bool(cur))
                hint = ("saved; type to replace" if (secret and cur) else "") or hints.get(k, "")
                if hint:
                    tk.Label(keys_frame, text=hint, anchor="w", bg="#0d1117", fg="#6e7681",
                             font=(GUI_FONT, 8)).grid(row=2 * i + 1, column=1, sticky="w")

        for code in ("ANGEL", "DHAN", "XTS"):
            tk.Radiobutton(row, text=BROKER_LABELS[code], value=code, variable=brk_var,
                           command=draw_keys, **C).pack(side="left", padx=6)
        draw_keys()
        msg_var = tk.StringVar(value=eng.status_msg)
        tk.Label(tb, textvariable=msg_var, justify="left", anchor="w", wraplength=520,
                 bg="#0d1117", fg="#ffd700", font=(GUI_FONT, 9)).pack(fill="x", padx=10, pady=8)

        def save_keys():
            upd = {}
            for k, (e, secret, had) in entries.items():
                v = e.get().strip()
                if v or not secret:
                    upd[k] = v
            _write_env(upd)
            return upd

        def run_bg(fn, done):
            def job():
                res = fn()
                self.root.after(0, lambda: done(*res))
            threading.Thread(target=job, daemon=True).start()

        def test():
            save_keys(); msg_var.set("Testing connection…")
            sample = self._sample_order_instrument()
            run_bg(lambda: eng.test_connection(brk_var.get(), sample),
                   lambda ok, m: msg_var.set(("✅ " if ok else "❌ ") + m))

        def use():
            if eng.auto_trade:
                messagebox.showinfo("Auto-trading is ON", "Turn auto-trading off before changing broker.", parent=w)
                return
            save_keys(); msg_var.set("Connecting…")
            run_bg(lambda: eng.connect(brk_var.get()), lambda ok, m: msg_var.set(("✅ " if ok else "❌ ") + m))

        btns = tk.Frame(tb, bg="#0d1117"); btns.pack(fill="x", padx=10, pady=4)
        DarkButton(btns, text="Test connection", command=test, bg="#1f6feb", padx=12, pady=4).pack(side="left")
        DarkButton(btns, text="Use this broker", command=use, bg="#196c2e", padx=12, pady=4).pack(side="left", padx=8)
        tk.Label(tb, text="Keys are saved only in the .env file next to the app.", anchor="w",
                 bg="#0d1117", fg="#6e7681", font=(GUI_FONT, 8)).pack(fill="x", padx=10, pady=(10, 0))

        # ── Orders tab ──
        to = tk.Frame(nb, bg="#0d1117"); nb.add(to, text=" Orders ")
        s = eng.settings
        v = {
            "lots": tk.IntVar(value=s["lots"]), "lots_15m": tk.IntVar(value=s["lots_15m"]),
            "lots_1h": tk.IntVar(value=s["lots_1h"]), "entry_type": tk.StringVar(value=s["entry_type"]),
            "limit_buffer_pct": tk.DoubleVar(value=s["limit_buffer_pct"]),
            "chase_after_s": tk.IntVar(value=s["chase_after_s"]), "product": tk.StringVar(value=s["product"]),
            "sl_at_broker": tk.BooleanVar(value=s["sl_at_broker"]),
            "sl_limit_gap_pct": tk.DoubleVar(value=s["sl_limit_gap_pct"]),
            "take_sell_setups": tk.BooleanVar(value=s["take_sell_setups"]),
            "trade_15m": tk.BooleanVar(value=s["trade_15m"]), "trade_1h": tk.BooleanVar(value=s["trade_1h"]),
            "max_trades": tk.IntVar(value=s["max_trades"]), "max_loss_rs": tk.IntVar(value=s["max_loss_rs"]),
        }
        g = tk.Frame(to, bg="#0d1117"); g.pack(fill="x", padx=12, pady=10)
        rows = [
            ("Lots per trade", "spin", "lots", (1, s["max_lots"])),
            ("Lots for 15M (0 = same)", "spin", "lots_15m", (0, s["max_lots"])),
            ("Lots for 1H (0 = same)", "spin", "lots_1h", (0, s["max_lots"])),
            ("Entry order", "radio", "entry_type", (("LIMIT", "Limit, then market"), ("MARKET", "Market"))),
            ("Limit buffer %", "entry", "limit_buffer_pct", None),
            ("Go to market after (s)", "spin", "chase_after_s", (3, 60)),
            ("Product", "radio", "product", (("INTRADAY", "Intraday"), ("CARRY", "Carry"))),
            ("Stop-loss order at broker", "check", "sl_at_broker", None),
            ("SL limit gap %", "entry", "sl_limit_gap_pct", None),
            ("Trade timeframes", "tf", None, None),
            ("Take SELL setups (short)", "check", "take_sell_setups", None),
            ("Max trades per day", "spin", "max_trades", (1, 50)),
            ("Max loss per day (₹)", "entry", "max_loss_rs", None),
        ]
        for i, (text, kind, key, opt) in enumerate(rows):
            tk.Label(g, text=text, anchor="w", width=26, **L).grid(row=i, column=0, sticky="w", pady=3)
            cell = tk.Frame(g, bg="#0d1117"); cell.grid(row=i, column=1, sticky="w")
            if kind == "spin":
                tk.Spinbox(cell, from_=opt[0], to=opt[1], textvariable=v[key], width=6,
                           buttonbackground="#30363d", **E).pack(side="left")
            elif kind == "entry":
                tk.Entry(cell, textvariable=v[key], width=8, **E).pack(side="left")
            elif kind == "radio":
                for val, t in opt:
                    tk.Radiobutton(cell, text=t, value=val, variable=v[key], **C).pack(side="left", padx=(0, 8))
            elif kind == "check":
                tk.Checkbutton(cell, variable=v[key], **C).pack(side="left")
            elif kind == "tf":
                tk.Checkbutton(cell, text="15M", variable=v["trade_15m"], **C).pack(side="left")
                tk.Checkbutton(cell, text="1H", variable=v["trade_1h"], **C).pack(side="left", padx=8)
        save_msg = tk.StringVar(value="")

        def save_orders():
            try:
                eng.save_settings({k: var.get() for k, var in v.items()})
                save_msg.set("✅ Saved. Applies to the next entry.")
            except Exception as e:
                save_msg.set(f"❌ {e}")
        sb = tk.Frame(to, bg="#0d1117"); sb.pack(fill="x", padx=12)
        DarkButton(sb, text="Save", command=save_orders, bg="#196c2e", padx=16, pady=4).pack(side="left")
        tk.Label(sb, textvariable=save_msg, bg="#0d1117", fg="#3fb950", font=(GUI_FONT, 9)).pack(side="left", padx=10)

        # ── Strikes tab: which of the strategy's strikes get real orders (15M and 1H kept apart) ──
        tk_ = tk.Frame(nb, bg="#0d1117"); nb.add(tk_, text=" Strikes ")
        lv_on = tf_levels(x for x in str(s.get("levels", "")).split(","))
        ty_on = {x.strip().upper() for x in str(s.get("opt_types", "")).split(",") if x.strip()}
        lv_vars = {(tf, lv): tk.BooleanVar(value=f"{tf}:{lv}" in lv_on) for tf in TIMEFRAMES for lv in ALL_LEVELS}
        ty_vars = {t: tk.BooleanVar(value=t in ty_on) for t in ("CE", "PE")}
        stf_var = tk.StringVar(value=str(s.get("strike_tf", "ANY")).upper())
        sym_var = tk.StringVar(value=s.get("symbols", ""))
        stk_var = tk.StringVar(value=s.get("strikes", ""))
        gs = tk.Frame(tk_, bg="#0d1117"); gs.pack(fill="x", padx=12, pady=8)

        def srow(i, text):
            tk.Label(gs, text=text, anchor="w", width=22, **L).grid(row=i, column=0, sticky="nw", pady=3)
            c = tk.Frame(gs, bg="#0d1117"); c.grid(row=i, column=1, sticky="w", pady=3)
            return c
        for i, tf in enumerate(TIMEFRAMES):
            c = srow(i, f"{tf} pivot levels")
            for lv in ALL_LEVELS:
                tk.Checkbutton(c, text=LEVEL_LABELS[lv], variable=lv_vars[(tf, lv)], command=lambda: preview(), **C).pack(side="left", padx=(0, 6))
        c = srow(2, "Option type")
        for t in ("CE", "PE"):
            tk.Checkbutton(c, text=t, variable=ty_vars[t], command=lambda: preview(), **C).pack(side="left", padx=(0, 6))
        c = srow(3, "Timeframe match")
        for val, t in (("ANY", "Any 15M or 1H strike"), ("BOTH", "Only strikes on both 15M and 1H")):
            tk.Radiobutton(c, text=t, value=val, variable=stf_var, command=lambda: preview(), **C).pack(side="left", padx=(0, 8))
        c = srow(4, "Symbols")
        tk.Entry(c, textvariable=sym_var, width=30, **E).pack(side="left")
        tk.Label(c, text="e.g. NIFTY,SENSEX  (blank = all)", bg="#0d1117", fg="#6e7681", font=(GUI_FONT, 8)).pack(side="left", padx=6)
        c = srow(5, "Only these strikes")
        tk.Entry(c, textvariable=stk_var, width=30, **E).pack(side="left")
        tk.Label(c, text="e.g. 22600,22650  (blank = all)", bg="#0d1117", fg="#6e7681", font=(GUI_FONT, 8)).pack(side="left", padx=6)
        tk.Label(tk_, text="Today's strategy strikes by timeframe (✅ gets real orders, ✖ forward test only):", anchor="w",
                 bg="#0d1117", fg="#8b949e", font=(GUI_FONT, 9)).pack(fill="x", padx=12, pady=(4, 2))
        prev = tk.Text(tk_, height=12, bg="#161b22", fg="#c9d1d9", relief="flat", font=("Menlo", 9))
        prev.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        def strike_sel():
            return {"levels": ",".join(f"{tf}:{lv}" for tf in TIMEFRAMES for lv in ALL_LEVELS if lv_vars[(tf, lv)].get()),
                    "opt_types": ",".join(t for t in ("CE", "PE") if ty_vars[t].get()),
                    "strike_tf": stf_var.get(), "symbols": sym_var.get().strip(), "strikes": stk_var.get().strip()}

        def preview(*_):
            sel = strike_sel()
            df = getattr(self, "daily_focus", None) or {}
            focus = {"15M": self.focus_15m or df.get("15M", {}) or {}, "1H": self.focus_1h or df.get("1H", {}) or {}}
            sym = (self.symbol_var.get() if hasattr(self, "symbol_var") else "") or ""
            both = set(focus["15M"].get("all_4", [])) & set(focus["1H"].get("all_4", []))
            lines = []
            for tf in TIMEFRAMES:
                f = focus[tf]
                lines.append(f"── {tf} strikes ──")
                for stk in sorted(set(f.get("all_4", []))):
                    lvs = strike_levels(stk, f)
                    types = ("CE", "PE") if (lvs & {"P", "PN"}) or not lvs else \
                            tuple(t for t in ("CE", "PE") if (t == "CE" and lvs & {"R1", "R2"}) or (t == "PE" and lvs & {"S1", "S2"}))
                    trade_tf = "15M / 1H" if stk in both else ("15M Intraday" if tf == "15M" else "1H Structure")
                    for t in types:
                        why = strike_selection_reason(sel, {"Timeframe": trade_tf, "Type": t, "Symbol": sym, "Strike": stk},
                                                      {"levels": [f"{tf}:{x}" for x in lvs], "opt_type": t, "symbol": sym, "strike": stk})
                        lv_txt = "/".join(LEVEL_LABELS[x] for x in ALL_LEVELS if x in lvs) or "-"
                        lines.append(f"{'✅' if not why else '✖'} {sym} {stk:>7} {t}  {lv_txt:<14} {why}")
                if len(lines) and lines[-1].startswith("──"):
                    lines.append("   (none yet)")
            prev.config(state="normal"); prev.delete("1.0", "end")
            has = any(not x.startswith(("──", "   (")) for x in lines)
            prev.insert("end", "\n".join(lines) if has else "Start the feed to see today's strikes.")
            prev.config(state="disabled")
        for var in (sym_var, stk_var):
            var.trace_add("write", preview)
        preview()
        strike_msg = tk.StringVar(value="")

        def save_strikes():
            try:
                eng.save_settings(strike_sel())
                strike_msg.set("✅ Saved. Applies to the next entry.")
            except Exception as e:
                strike_msg.set(f"❌ {e}")
        sb2 = tk.Frame(tk_, bg="#0d1117"); sb2.pack(fill="x", padx=12, pady=(0, 8))
        DarkButton(sb2, text="Save", command=save_strikes, bg="#196c2e", padx=16, pady=4).pack(side="left")
        tk.Label(sb2, text="A strike on both timeframes gets its order if either timeframe's levels allow it.",
                 bg="#0d1117", fg="#6e7681", font=(GUI_FONT, 8)).pack(side="left", padx=10)
        tk.Label(sb2, textvariable=strike_msg, bg="#0d1117", fg="#3fb950", font=(GUI_FONT, 9)).pack(side="left", padx=4)

        # ── Today's orders tab ──
        tt = tk.Frame(nb, bg="#0d1117"); nb.add(tt, text=" Today's orders ")
        cols = ("time", "contract", "action", "qty", "price", "status", "remark")
        tv = ttk.Treeview(tt, columns=cols, show="headings", height=16)
        for c, wdt in zip(cols, (72, 200, 56, 50, 70, 104, 200)):
            tv.heading(c, text=c.title()); tv.column(c, width=wdt, anchor="w")
        tv.pack(fill="both", expand=True, padx=6, pady=6)

        def fill():
            if not w.winfo_exists():
                return
            tv.delete(*tv.get_children())
            for o in reversed(eng.recent[-20:]):
                tv.insert("", "end", values=tuple(o.get(c, "") for c in cols))
            w.after(2000, fill)
        fill()

    def _sample_order_instrument(self):
        """One contract the app is watching, used by Test connection to check the instrument lookup."""
        try:
            for tok, m in list(getattr(feed, "token_to_meta", {}).items())[:200]:
                if m.get("strike") and m.get("opt_type") in ("CE", "PE"):
                    inst = LiveOrderManager.instrument_for({"Token": tok, "Symbol": m.get("symbol", ""),
                                                            "Strike": m["strike"], "Type": m["opt_type"]})
                    if inst.get("tsym"):
                        return inst
        except Exception:
            pass
        return None

    def _update_order_status(self, msg: str):
        """Older callers: show an order message in the status line."""
        try:
            self._set_status(msg, "#ffd700")
        except Exception:
            pass


    def start_feed(self):
        exch   = self.exchange_var.get()
        symbol = self.symbol_var.get()
        expiry = self.expiry_var.get()
        n      = self.strikes_var.get()

        if not expiry:
            messagebox.showerror("Error", "Please select an expiry"); return
        try:   # remembered so an auto-restart resumes the same symbol
            with open(os.path.join(BASE_DIR, "last_session.json"), "w") as _lf:
                json.dump({"exchange": exch, "symbol": symbol}, _lf)
        except Exception:
            pass

        # Build list of expiries to subscribe
        expiries_to_load = [expiry]
        next_exp = (self.next_exp_var.get() or "").strip()
        if next_exp and next_exp != expiry and (self.use_next_exp.get() or self.expiry_filter_var.get() == "NEXT"):
            expiries_to_load.append(next_exp)

        self._set_status("Connecting & setting up Google Sheet...")
        for b in getattr(self, "_start_buttons", [self.start_btn]):
            b.config(state="disabled", bg="#21262d", fg="#8b949e")
        feed.running = True
        feed.status_cb = lambda m, col=None: self.root.after(0, lambda: self._set_status(m, col or "#3fb950"))
        self.root.after(300, self._refresh_display)

        def _run():
            try:
                print(f"[START] Preparing tokens for {exch}/{symbol}/{expiries_to_load}...")
                filter_df, atm, step = feed.prepare(exch, symbol, expiries_to_load, n)
                strikes = sorted(set(filter_df["strike"].astype(float).div(100).astype(int)))
                print(f"[START] Got {len(strikes)} strikes, ATM~{atm}")

                print("[START] Setting up Google Sheet...")
                feed.setup_sheet(symbol, strikes)
                print("[START] Sheet setup done, starting WebSocket...")

                feed.start_ws()
                print("[START] WebSocket connecting...")
                time.sleep(1)

                exp_label = " + ".join(expiries_to_load)
                print(f"[START] LIVE — {symbol} | {exp_label} | {len(strikes)} strikes")
                self.root.after(0, lambda: self._set_status(
                    f"● LIVE | {symbol} | {exp_label} | ATM~{atm} | +/-{n} strikes"))
                def _enable_stop():
                    for b in getattr(self, "_stop_buttons", [self.stop_btn]):
                        b.config(state="normal", bg="#da3633", fg="#ffffff", activebackground="#f85149")
                self.root.after(0, _enable_stop)
                self.root.after(200, self._refresh_display)
                feed.update_sheet_loop(
                    symbol, strikes,
                    status_cb=lambda m: (
                        self.root.after(0, lambda: self._set_status(m)),
                        self.root.after(0, self._refresh_display)
                    )
                )
            except Exception as e:
                feed.running = False
                import traceback
                print(f"[START ERROR] {e}")
                traceback.print_exc()
                _err = str(e)   # `e` is unbound once the except block ends; capture it for the callbacks
                self.root.after(0, lambda: messagebox.showerror("Error", _err))
                def _enable_start_err():
                    for b in getattr(self, "_start_buttons", [self.start_btn]):
                        b.config(state="normal", bg="#238636", fg="#ffffff", activebackground="#2ea043")
                self.root.after(0, _enable_start_err)
                self.root.after(0, lambda: self._set_status(f"Error: {_err}", "#f85149"))

        threading.Thread(target=_run, daemon=True).start()

    def stop_feed(self):
        feed.running=False
        feed.stop_ws()
        for b in getattr(self, "_start_buttons", [self.start_btn]):
            b.config(state="normal", bg="#238636", fg="#ffffff", activebackground="#2ea043")
        for b in getattr(self, "_stop_buttons", [self.stop_btn]):
            b.config(state="disabled", bg="#21262d", fg="#8b949e")
        self._set_status("Stopped — data remains in Google Sheet","#8b949e")

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
if __name__=="__main__":
    print("Logging in to Angel One...")
    Session.login()
    print("Loading instrument list (cached once per day)...")
    Session.load_scrip()

    df=Session.scrip_df
    print(f"   Unique exch_seg values: {df['exch_seg'].unique().tolist()}")

    print("Ready — Fibonacci Pivot Option Chain & Reversal Scanner enabled\n")
    root=tk.Tk()
    app=ControlPanel(root)
    if os.getenv("LOC_AUTOSTART") == "1":
        # Relaunched by the launcher after a crash/freeze: start the feed again by itself
        _t0 = time.time()
        try:
            with open(os.path.join(BASE_DIR, "last_session.json")) as _lf:
                _last = json.load(_lf)
            app.exchange_var.set(_last.get("exchange", "NFO"))
            app.symbol_var.set(_last.get("symbol", "NIFTY"))
            app.expiry_var.set("")
            app._load_expiries()
        except Exception:
            pass
        def _auto_start():
            if app.expiry_var.get() or time.time() - _t0 > 90:
                print("[AUTO] Restarting live feed after relaunch...")
                app.start_feed()
            else:
                root.after(2000, _auto_start)
        root.after(5000, _auto_start)
    root.mainloop()

