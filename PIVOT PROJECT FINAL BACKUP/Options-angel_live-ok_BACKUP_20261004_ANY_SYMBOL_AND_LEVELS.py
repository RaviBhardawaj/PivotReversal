"""
Angel One - Live Option Chain with Greeks
Control Panel + Google Sheets Feed
Fixes: expiry sort, max OI highlight, BSE/MCX exchange types
"""

import os, time, pyotp, requests, math, threading, pickle, json, csv, socket
import pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox
from dotenv import load_dotenv
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from datetime import datetime, date, timedelta
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

    rng = high - low
    if rng <= 0:
        # Fallback minimal spread for flat ticks (e.g. single trade)
        rng = max(close * 0.005, 0.05)
        high = close + rng / 2
        low  = close - rng / 2

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
        self.entry_price   = 0.0
        self.entry_time    = ""
        self.sl            = 0.0
        self.targets       = []
        self.signal        = "WAITING"
        self.last_ltp      = 0.0
        self.candle_5m     = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "bucket": -1}
        self.retest_done   = False
        self.retest_low    = 0.0
        self.retest_high   = 0.0
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

    def update_tick(self, ltp: float, pivots: dict, now: datetime, opt_high: float = 0.0, opt_low: float = 0.0):
        if not pivots or ltp <= 0:
            return

        self.last_ltp = ltp

        # ── 5M candle builder from live ticks ──
        bucket = (now.minute // 5) * 5
        c = self.candle_5m
        if c["bucket"] != bucket:
            if c["bucket"] != -1 and c["close"] > 0:
                self._prev_candle = dict(c)
            c["open"]   = ltp
            c["high"]   = max(ltp, opt_high) if opt_high > 0 else ltp
            c["low"]    = min(ltp, opt_low) if opt_low > 0 else ltp
            c["close"]  = ltp
            c["bucket"] = bucket
        else:
            c["high"]  = max(c["high"], ltp)
            c["low"]   = min(c["low"], ltp) if c["low"] > 0 else ltp
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

        # ── Setup state machines and entries ONLY progress during LIVE MARKET hours ──
        if not is_live_market_open(now):
            if self.trade_status == "Waiting" and self.state == "WAITING":
                self.signal = "★ Market Closed — Monitoring Setup"
            return

        # ── Reset after SL/Target so a new sweep can retrigger same day ──
        if self.trade_status in ("SL Hit", "Trailing SL Hit", "Target 5 Hit"):
            if "SL Hit" in self.trade_status:
                self.sl_hit_count += 1
            self.state        = "WAITING"
            self.trade_status = "Waiting"
            self.sl = self.entry_price = 0.0
            self.ll = 0.0   # unlock sweep low so next sweep sets a fresh new LL
            self.hh = 0.0   # unlock sweep high so next sweep sets a fresh new HH
            self.retest_done   = False
            self.retest_low    = 0.0
            self.retest_high   = 0.0
            self.frozen_s2 = self.frozen_s1 = 0.0
            self.frozen_r2 = self.frozen_r1 = 0.0
            self.frozen_pivots = {}
            self.stale_reset   = False
            self.targets = []
            if self.sl_hit_count == 1:
                self.signal = f"★ {self.opt_type} First SL Hit — Awaiting New Sweep & Re-entry with New HH/LL SL"
            else:
                self.signal = "WAITING"

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
                note = f" (S2={s2:.2f}<=0.05)" if use_s1_for_buy else ""
                re_tag = f" [Re-entry New LL:{self.ll:.2f}]" if self.sl_hit_count > 0 else f" [LL:{self.ll:.2f}]"
                self.signal = f"★ {self.opt_type} SWEEP {active_lvl}{note}{re_tag} — Awaiting Reclaim & Candle Close"

            # Step 2: Support Reclaimed — Await Candle Close Confirmation
            elif (self.state == f"SWEEP_{active_lvl}" or (self.state == "WAITING" and valid_low < sup_thresh and ltp < r1)) and ltp >= fs_sup:
                self.state         = f"RECLAIM_{active_lvl}"
                self.trigger_level = active_lvl
                if self.ll == 0:
                    self.ll = round(valid_low if valid_low > 0 else ltp, 2)
                self.signal = f"★ {self.opt_type} RECLAIM {active_lvl} — Awaiting Bullish Candle Close"

            elif self.state == f"RECLAIM_{active_lvl}":
                # Check for completed bullish candle closing above active support level
                candle_ok = False
                wick_retested = False
                if has_closed_candle:
                    c_close = closed["close"]
                    c_open  = closed["open"]
                    c_low   = closed.get("low", c_close)
                    if c_close >= fs_sup and c_close >= c_open:
                        candle_ok = True
                        if c_low <= fs_sup * 1.015:
                            wick_retested = True
                elif ltp >= fs_sup and c["close"] >= fs_sup and c["close"] >= c["open"] and c["low"] <= fs_sup:
                    candle_ok = True
                    wick_retested = True

                if candle_ok:
                    self.state = f"RETEST_{active_lvl}"
                    self.retest_low = round(min(ltp, valid_low), 2)
                    if wick_retested:
                        self.retest_done = True
                        self.state = "TRADING_IN_FAVOR_BUY"
                        self.signal = f"★ {self.opt_type} {active_lvl} Reclaim Candle Closed Bullish (Retest Wick Confirmed) — Watching In-Favour Push"
                    else:
                        self.retest_done = False
                        self.signal = f"★ {self.opt_type} {active_lvl} Reclaim Candle Closed Bullish — Watching Retest"

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
                # Step 4: Price bounces and trades in favour (moves above support level and above retest low)
                start_favor = (ltp > fs_sup and (self.retest_low == 0 or ltp >= self.retest_low + max(fs_sup * 0.002, 0.2)))
                if start_favor:
                    # Step 5: ENTRY IS CONFIRMED!
                    self.state         = "CONFIRMED_BUY"
                    self.trigger_level = active_lvl
                    fp       = self.frozen_pivots if self.frozen_pivots else pivots
                    entry_px = round(fp.get(active_lvl, fs_sup), 2)

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
                re_tag = f" [Re-entry New HH:{self.hh:.2f}]" if self.sl_hit_count > 0 else f" [HH:{self.hh:.2f}]"
                self.signal = f"▼ {self.opt_type} SWEEP R2{re_tag} — Awaiting Rejection & Candle Close"

            # Step 2: R2 Rejected — Await Candle Close Confirmation
            elif (self.state == "SWEEP_R2" or (self.state == "WAITING" and valid_high > r2 and ltp > s1)) and ltp <= fr2:
                self.state         = "RECLAIM_R2"
                self.trigger_level = "R2"
                if self.hh == 0:
                    self.hh = round(valid_high if valid_high > 0 else ltp, 2)
                self.signal = f"▼ {self.opt_type} REJECT R2 — Awaiting Bearish Candle Close"

            elif self.state == "RECLAIM_R2":
                # Check for completed bearish candle closing below R2
                candle_ok = False
                wick_retested = False
                if has_closed_candle:
                    c_close = closed["close"]
                    c_open  = closed["open"]
                    c_high  = closed.get("high", c_close)
                    if c_close <= fr2 and c_close <= c_open:
                        candle_ok = True
                        if c_high >= fr2 * 0.985:
                            wick_retested = True
                elif ltp <= fr2 and c["close"] <= fr2 and c["close"] <= c["open"] and c["high"] >= fr2:
                    candle_ok = True
                    wick_retested = True

                if candle_ok:
                    self.state = "RETEST_R2"
                    self.retest_high = round(max(ltp, valid_high), 2)
                    if wick_retested:
                        self.retest_done = True
                        self.state = "TRADING_IN_FAVOR_SELL"
                        self.signal = f"▼ {self.opt_type} R2 Rejection Candle Closed Bearish (Retest Wick Confirmed) — Watching In-Favour Drop"
                    else:
                        self.retest_done = False
                        self.signal = f"▼ {self.opt_type} R2 Rejection Candle Closed Bearish — Watching Retest"

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
                # Step 4: Price drops and trades in favour (moves below R2 and below retest high)
                start_favor = (ltp < fr2 and (self.retest_high == 0 or ltp <= self.retest_high - max(fr2 * 0.002, 0.2)))
                if start_favor:
                    # Step 5: ENTRY IS CONFIRMED!
                    self.state         = "CONFIRMED_SELL"
                    self.trigger_level = "R2"
                    fp       = self.frozen_pivots if self.frozen_pivots else pivots
                    entry_px = round(fp.get('R2', fr2), 2)

                    # When R2 rejection triggers, HH value is our updated SL (strictly > entry_px)
                    if self.hh > 0 and self.hh > entry_px:
                        effective_sl = round(self.hh, 2)
                    elif valid_high > 0 and valid_high > entry_px:
                        effective_sl = round(valid_high, 2)
                    elif fp.get('R3', r3) > entry_px:
                        effective_sl = round(fp.get('R3', r3), 2)
                    else:
                        effective_sl = round(entry_px * 1.10, 2)

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
                confirmed = (has_closed_candle and
                             closed["close"] <= fr1 and
                             closed["close"] <= closed["open"]) or (not has_closed_candle and ltp <= fr1)
                if confirmed:
                    self.state        = "CONFIRMED_SELL"
                    self.trigger_level = "R1"
                    fp       = self.frozen_pivots if self.frozen_pivots else pivots
                    entry_px = round(fp.get('R1', fr1), 2)
                    if self.hh > 0 and self.hh > entry_px:
                        effective_sl = round(self.hh, 2)
                    elif valid_high > 0 and valid_high > entry_px:
                        effective_sl = round(valid_high, 2)
                    elif fp.get('R2', r2) > entry_px:
                        effective_sl = round(fp.get('R2', r2), 2)
                    else:
                        effective_sl = round(entry_px * 1.10, 2)

                    t1 = round(fp.get('P',  p),  2)
                    t2 = round(fp.get('S1', s1), 2)
                    t3 = round(fp.get('S2', s2), 2)
                    t4 = round(fp.get('S3', s3), 2)
                    t5 = round(fp.get('S3', s3) * 0.95, 2)

                    self.entry_price  = entry_px
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    self.targets      = [t1, t2, t3, t4, t5]
                    mkt_ctx = "Mkt Bearish ↓" if self.opt_type == "CE" else "Mkt Bullish ↑"
                    self.signal       = f"▼ SELL {self.opt_type} [{mkt_ctx} | R1 Reject Confirmed | Entry:{entry_px:.2f} | HH SL:{effective_sl:.2f} | T1:{t1:.1f} T2:{t2:.1f} T3:{t3:.1f} T4:{t4:.1f} T5:{t5:.1f}]"
                    self.trade_status = "Active"

        # ═══════════════════════════════════════════════════════
        # 3. ACTIVE TRADE MONITORING — SL & Target tracking
        # ═══════════════════════════════════════════════════════
        if self.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit", "Target 3 Hit", "Target 4 Hit"):
            # ── Stale-state guard (only applies before targets hit) ──────────────────────────
            if self.trade_status == "Active" and self.entry_price > 0 and ltp > 0:
                stale_buy  = "BUY"  in self.signal and self.entry_price > ltp * 1.5
                stale_sell = "SELL" in self.signal and self.entry_price < ltp * 0.67
                if stale_buy or stale_sell:
                    self.state        = "WAITING"
                    self.trade_status = "Waiting"
                    self.sl = self.entry_price = 0.0
                    self.ll = self.hh = 0.0
                    self.retest_done  = False
                    self.retest_low   = 0.0
                    self.retest_high  = 0.0
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
                        if len(self.targets) > 2 and self.targets[2] > self.sl:
                            self.sl = round(self.targets[2], 2)   # trail SL to T3
                        t5_val = self.targets[4] if len(self.targets) > 4 else round(self.targets[3] * 1.05, 2)
                        self.signal = f"★ BUY {self.opt_type} Target 4 Hit @ {ltp:.2f} | Support Sustained → Aiming T5:{t5_val:.1f} | Trailed SL:{self.sl:.1f}"
                    elif len(self.targets) > 2 and ltp >= self.targets[2]:
                        self.trade_status = "Target 3 Hit"
                        if len(self.targets) > 1 and self.targets[1] > self.sl:
                            self.sl = round(self.targets[1], 2)   # trail SL to T2
                        self.signal = f"★ BUY {self.opt_type} Target 3 Hit @ {ltp:.2f} | Support Sustained → Aiming T4:{self.targets[3]:.1f} | Trailed SL:{self.sl:.1f}"
                    elif len(self.targets) > 1 and ltp >= self.targets[1]:
                        self.trade_status = "Target 2 Hit"
                        if self.targets[0] > self.sl:
                            self.sl = round(self.targets[0], 2)   # trail SL to T1
                        self.signal = f"★ BUY {self.opt_type} Target 2 Hit @ {ltp:.2f} | Support Sustained → Aiming T3:{self.targets[2]:.1f} | Trailed SL:{self.sl:.1f}"
                    elif ltp >= self.targets[0]:
                        self.trade_status = "Target 1 Hit"
                        if self.entry_price > self.sl:
                            self.sl = round(self.entry_price, 2)  # trail SL to Entry (breakeven)
                        self.signal = f"★ BUY {self.opt_type} Target 1 Hit @ {ltp:.2f} | Support Sustained → Aiming T2:{self.targets[1]:.1f} | Trailed SL:{self.sl:.1f}"
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
                        if len(self.targets) > 2 and (self.sl == 0 or self.targets[2] < self.sl):
                            self.sl = round(self.targets[2], 2)
                        t5_val = self.targets[4] if len(self.targets) > 4 else round(self.targets[3] * 0.95, 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 4 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T5:{t5_val:.1f} | Trailed SL:{self.sl:.1f}"
                    elif len(self.targets) > 2 and ltp <= self.targets[2]:
                        self.trade_status = "Target 3 Hit"
                        if len(self.targets) > 1 and (self.sl == 0 or self.targets[1] < self.sl):
                            self.sl = round(self.targets[1], 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 3 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T4:{self.targets[3]:.1f} | Trailed SL:{self.sl:.1f}"
                    elif len(self.targets) > 1 and ltp <= self.targets[1]:
                        self.trade_status = "Target 2 Hit"
                        if self.sl == 0 or self.targets[0] < self.sl:
                            self.sl = round(self.targets[0], 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 2 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T3:{self.targets[2]:.1f} | Trailed SL:{self.sl:.1f}"
                    elif ltp <= self.targets[0]:
                        self.trade_status = "Target 1 Hit"
                        if self.sl == 0 or self.entry_price < self.sl:
                            self.sl = round(self.entry_price, 2)
                        self.signal = f"▼ SELL {self.opt_type} Target 1 Hit @ {ltp:.2f} | Resistance Sustained → Aiming T2:{self.targets[1]:.1f} | Trailed SL:{self.sl:.1f}"


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

    all_4 = sorted(list({s for s in (ce1, ce2, pe1, pe2) if s > 0}))
    all_6 = sorted(list({s for s in (ce1, ce2, ce3, pe1, pe2, pe3) if s > 0}))

    return {
        "timeframe": tf_label,
        "CE1": ce1, "CE2": ce2, "CE3": ce3,
        "PE1": pe1, "PE2": pe2, "PE3": pe3,
        "ce_strikes": [s for s in (ce1, ce2, ce3) if s > 0],
        "pe_strikes": [s for s in (pe1, pe2, pe3) if s > 0],
        "all_4": all_4,
        "all_6": all_6,
        "P": p, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3
    }


def select_intraday_15m_focus_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Selects INTRADAY focus strikes based on 15-Minute Fibonacci Pivots:
      - 2 CE strikes: nearest strikes to 15M R1 and 15M R2 (optional R3)
      - 2 PE strikes: nearest strikes to 15M S1 and 15M S2 (optional S3)
    """
    piv = mtf_pivots.get("15-Min") or mtf_pivots.get("1-Hour") or mtf_pivots.get("Daily", {})
    return select_focus_strikes_from_pivots(piv, step, tf_label="15-Min")


def select_1h_structure_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Selects 1-HOUR STRUCTURE focus strikes based on 1-Hour Fibonacci Pivots:
      - 2 CE strikes: nearest strikes to 1H R1 and 1H R2 (optional R3)
      - 2 PE strikes: nearest strikes to 1H S1 and 1H S2 (optional S3)
    """
    piv = mtf_pivots.get("1-Hour") or mtf_pivots.get("Daily", {})
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


def _safe_float(val, default=0.0):
    """Safely convert a value to float, returning default on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════
# LIVE ORDER MANAGER — Angel One SmartAPI Order Execution
# ═══════════════════════════════════════════════════════════
ORDER_LOG_FILE = os.path.join(BASE_DIR, "live_orders_log.csv")

class LiveOrderManager:
    """
    Manages live order placement via Angel One SmartAPI.

    Modes:
      • PAPER  — no real orders placed, just logs the intent (default, safe)
      • LIVE   — places real BUY/SELL orders via Session.obj.placeOrder()

    Features:
      • Deduplicates: one order per (opt_type, strike) until closed
      • Logs every order attempt to live_orders_log.csv
      • Supports configurable lot size (quantity per trade)
      • Provides exit_order() to square off on SL/Target hit
    """

    def __init__(self):
        self.mode          = "PAPER"        # "PAPER" or "LIVE"
        self.lot_qty       = 1              # number of lots (1 lot = 1 for index options)
        self.active_orders = {}             # (opt_type, strike) -> order_id
        self._init_log()

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
            with open(ORDER_LOG_FILE, 'a', newline='') as f:
                csv.writer(f).writerow([
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    mode, symbol, opt_type, strike, token,
                    action, qty, f"{price:.2f}", order_id, status, remark
                ])
        except Exception as e:
            print(f"[OrderLog] Append error: {e}")

    def place_order(self, symbol: str, opt_type: str, strike: int,
                    token: str, exchange: str, action: str,
                    ltp: float, qty: int = None, expiry: str = "") -> str:
        """
        Place a market BUY or SELL order.
        action: 'BUY' or 'SELL'
        Returns order_id string, or 'PAPER-XXXXX' in paper mode.
        """
        trade_key = (opt_type, strike, expiry) if expiry else (opt_type, strike)
        if trade_key in self.active_orders:
            return self.active_orders[trade_key]   # already in market
        if (opt_type, strike) in self.active_orders and not expiry:
            return self.active_orders[(opt_type, strike)]

        if not is_live_market_open():
            return "MARKET_CLOSED"

        qty = qty or self.lot_qty
        order_id = "PAPER-NONE"

        if self.mode == "LIVE":
            try:
                order_params = {
                    "variety":          "NORMAL",
                    "tradingsymbol":    self._get_trading_symbol(symbol, opt_type, strike, token=token, expiry=expiry),
                    "symboltoken":      token,
                    "transactiontype":  action,         # "BUY" or "SELL"
                    "exchange":         exchange,
                    "ordertype":        "MARKET",
                    "producttype":      "INTRADAY",
                    "duration":         "DAY",
                    "price":            "0",
                    "squareoff":        "0",
                    "stoploss":         "0",
                    "quantity":         str(qty),
                }
                resp = Session.obj.placeOrder(order_params)
                if resp and resp.get("status"):
                    order_id = resp["data"]["orderid"]
                    status   = "PLACED"
                    remark   = f"Filled approx @ {ltp:.2f}"
                    exp_lbl  = f" | Exp:{expiry}" if expiry else ""
                    print(f"✅ [ORDER LIVE] {action} {opt_type} {strike}{exp_lbl} | OrderID: {order_id} | Qty:{qty}")
                    trigger_immediate_alert(
                        f"📤 ORDER PLACED — {action} {opt_type} {strike}{exp_lbl}",
                        f"OrderID:{order_id} | Qty:{qty} | LTP:{ltp:.2f}"
                    )
                else:
                    order_id = "FAILED"
                    status   = "FAILED"
                    remark   = str(resp)
                    print(f"❌ [ORDER LIVE] FAILED {action} {opt_type} {strike}: {resp}")
            except Exception as e:
                order_id = "ERROR"
                status   = "ERROR"
                remark   = str(e)
                print(f"❌ [ORDER LIVE] Exception {action} {opt_type} {strike}: {e}")
        else:
            # PAPER mode
            order_id = f"PAPER-{datetime.now().strftime('%H%M%S%f')[:12]}"
            status   = "PAPER"
            remark   = f"Paper trade @ {ltp:.2f}"
            exp_lbl  = f" | Exp:{expiry}" if expiry else ""
            print(f"📋 [ORDER PAPER] {action} {opt_type} {strike}{exp_lbl} | ID:{order_id} | LTP:{ltp:.2f} | Qty:{qty}")

        self._log_order(self.mode, symbol, opt_type, strike, token,
                        action, qty, ltp, order_id, status, remark)
        self.active_orders[trade_key] = order_id
        return order_id

    def exit_order(self, symbol: str, opt_type: str, strike: int,
                   token: str, exchange: str, original_action: str,
                   ltp: float, reason: str, qty: int = None, expiry: str = "") -> str:
        """
        Square off (exit) an active order.
        original_action: the direction of the ENTRY ('BUY' or 'SELL')
        Exit action is opposite: BUY → SELL to square off.
        """
        trade_key = (opt_type, strike, expiry) if (expiry and (opt_type, strike, expiry) in self.active_orders) else (opt_type, strike)
        entry_order_id = self.active_orders.pop(trade_key, None)
        if not entry_order_id and (opt_type, strike) in self.active_orders:
            entry_order_id = self.active_orders.pop((opt_type, strike), None)
        if not entry_order_id:
            return "NO_ACTIVE_ORDER"

        exit_action = "SELL" if original_action == "BUY" else "BUY"
        qty         = qty or self.lot_qty
        order_id    = "PAPER-EXIT"

        if self.mode == "LIVE":
            try:
                order_params = {
                    "variety":          "NORMAL",
                    "tradingsymbol":    self._get_trading_symbol(symbol, opt_type, strike, token=token, expiry=expiry),
                    "symboltoken":      token,
                    "transactiontype":  exit_action,
                    "exchange":         exchange,
                    "ordertype":        "MARKET",
                    "producttype":      "INTRADAY",
                    "duration":         "DAY",
                    "price":            "0",
                    "squareoff":        "0",
                    "stoploss":         "0",
                    "quantity":         str(qty),
                }
                resp = Session.obj.placeOrder(order_params)
                if resp and resp.get("status"):
                    order_id = resp["data"]["orderid"]
                    status   = f"EXIT-{reason}"
                    remark   = f"Exit @ {ltp:.2f} | Entry OrderID:{entry_order_id}"
                    exp_lbl  = f" | Exp:{expiry}" if expiry else ""
                    print(f"✅ [EXIT LIVE] {exit_action} {opt_type} {strike}{exp_lbl} | {reason} | OrderID:{order_id}")
                    trigger_immediate_alert(
                        f"📥 EXIT — {reason} | {opt_type} {strike}{exp_lbl}",
                        f"Exit@{ltp:.2f} | OrderID:{order_id}"
                    )
                else:
                    order_id = "EXIT-FAILED"
                    status   = "EXIT-FAILED"
                    remark   = str(resp)
                    print(f"❌ [EXIT LIVE] FAILED {exit_action} {opt_type} {strike}: {resp}")
            except Exception as e:
                order_id = "EXIT-ERROR"
                status   = "EXIT-ERROR"
                remark   = str(e)
                print(f"❌ [EXIT LIVE] Exception: {e}")
        else:
            order_id = f"PAPER-EXIT-{datetime.now().strftime('%H%M%S')}"
            status   = f"PAPER-EXIT-{reason}"
            remark   = f"Paper exit @ {ltp:.2f} | Entry:{entry_order_id}"
            exp_lbl  = f" | Exp:{expiry}" if expiry else ""
            print(f"📋 [EXIT PAPER] {exit_action} {opt_type} {strike}{exp_lbl} | {reason} @ {ltp:.2f}")

        self._log_order(self.mode, symbol, opt_type, strike, token,
                        exit_action, qty, ltp, order_id, status, remark)
        return order_id

    def _get_trading_symbol(self, symbol: str, opt_type: str, strike: int, token: str = "", expiry: str = "") -> str:
        """Build the Angel One trading symbol string (e.g. NIFTY24OCT24900CE)."""
        try:
            if Session.scrip_df is not None:
                df = Session.scrip_df
                # 1. Exact match by unique instrument token (bulletproof across all expiries)
                if token:
                    m_tok = df[df["token"].astype(str) == str(token)]
                    if not m_tok.empty:
                        return m_tok.iloc[0]["symbol"]
                # 2. Match by symbol + strike + opt_type + expiry
                mask = (
                    (df["name"] == symbol) &
                    (df["instrumenttype"].isin(["OPTIDX", "OPTSTK"])) &
                    (df["strike"].astype(float) / 100 == strike)
                )
                if expiry:
                    mask = mask & (df["expiry"] == expiry)
                match = df[mask & (df["symbol"].str.endswith(opt_type))]
                if not match.empty:
                    return match.iloc[0]["symbol"]
        except Exception:
            pass
        # Fallback: construct manually
        from datetime import date
        today = date.today()
        exp_str = today.strftime("%d%b%y").upper()
        return f"{symbol}{exp_str}{strike}{opt_type}"

    def set_mode(self, mode: str):
        """Switch between 'PAPER' and 'LIVE'."""
        mode = mode.upper()
        assert mode in ("PAPER", "LIVE"), "Mode must be PAPER or LIVE"
        self.mode = mode
        print(f"🔄 [ORDER MANAGER] Mode set to: {mode}")

    def set_qty(self, qty: int):
        """Set the lot quantity for orders."""
        self.lot_qty = max(1, int(qty))
        print(f"🔄 [ORDER MANAGER] Quantity set to: {self.lot_qty} lot(s)")

    def poll_live_positions(self, forward_tracker, interval_s: int = 30):
        """
        Background thread: polls Angel One open positions every `interval_s` seconds.
        In LIVE mode — if a position that we think is open has been squared off
        externally (manual exit in Angel One app / GTT / broker RMS),
        it will be auto-closed in the ForwardTestTracker as 'Market Closed'.
        Safe in PAPER mode — does nothing.
        """
        import time as _time
        while True:
            _time.sleep(interval_s)
            if self.mode != "LIVE":
                continue
            try:
                resp = Session.obj.position()
                if not resp or not resp.get("status"):
                    continue
                open_positions = {}
                for pos in (resp.get("data") or []):
                    sym  = pos.get("tradingsymbol", "")
                    qty  = int(pos.get("netqty", 0))
                    ltp  = float(pos.get("ltp", 0) or 0)
                    if qty != 0:
                        open_positions[sym] = ltp

                # Check if any of our tracked orders is no longer in open positions
                for (opt_type, strike), order_id in list(self.active_orders.items()):
                    if order_id.startswith("PAPER") or order_id in ("FAILED", "ERROR"):
                        continue
                    # Build the trading symbol to check against
                    trade = forward_tracker.active_trades.get((opt_type, strike))
                    if not trade:
                        continue
                    sym = trade.get('Token', '')   # use token as proxy key if symbol lookup fails
                    # Check if position is gone (net qty = 0 / not in open positions)
                    tsym = self._get_trading_symbol(trade.get('Symbol', ''), opt_type, strike)
                    if tsym not in open_positions:
                        # Position was externally closed — force close in tracker
                        ltp_fallback = trade.get('Current_Price', trade.get('Entry_Price', 0))
                        print(f"[POSITION POLL] {opt_type} {strike} no longer open in Angel One "
                              f"— auto-closing forward test entry @ {ltp_fallback:.2f}")
                        forward_tracker.manual_close(opt_type, strike, ltp_fallback, "Market Closed")
                        self.active_orders.pop((opt_type, strike), None)
            except Exception as e:
                print(f"[POSITION POLL] Error: {e}")


# Singleton order manager instance
live_order_manager = LiveOrderManager()


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
        self._init_file()


    def _init_file(self):
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
                  token: str = "", exchange: str = "NFO", expiry: str = ""):
        """Log a new trade entry with Timeframe, Expiry, and High/Low tracking.
        opt_piv: option Fibonacci pivots at time of entry (used to record S2/R2 entry zone).
        timeframe: '15M Intraday', '1H Structure', '5M Dynamic', etc.
        token: Angel One instrument token (needed for live order execution).
        exchange: exchange code e.g. 'NFO', 'BFO'.
        expiry: contract expiry string e.g. '10OCT2024'.
        """
        trade_key  = (opt_type, strike, expiry) if expiry else (opt_type, strike)
        date_str   = datetime.now().strftime('%Y-%m-%d')
        # Key includes action: BUY and SELL are tracked separately
        # Allows same strike to go BUY→target→SELL→target (bidirectional loop)
        closed_key = (opt_type, strike, expiry, action, date_str) if expiry else (opt_type, strike, action, date_str)

        if not is_live_market_open():
            return None   # Entries strictly allowed during live market hours
        if trade_key in self.active_trades:
            return None   # already tracking this strike — do not duplicate
        if closed_key in self._closed_keys_today:
            return None   # same direction already had SL/target close today — no re-entry

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
        if (opt_type, strike) not in self.active_trades:
            self.active_trades[(opt_type, strike)] = trade
        self._append_row(trade)
        zone_str = f"S2:{entry_s2} < LTP:{entry_price:.2f} < R2:{entry_r2}" if entry_s2 else ""
        exp_lbl  = f" | Exp:{expiry}" if expiry else ""
        print(f"📝 [FWD TEST - {timeframe}] {action} {opt_type} {strike}{exp_lbl} @ {entry_price:.2f} "
              f"| SL:{sl:.1f} | T1:{t1:.1f} T2:{t2:.1f} T3:{t3:.1f} T4:{t4:.1f} T5:{t5:.1f} | {zone_str}")

        # ── Live/Paper Order Placement ─────────────────────────────────
        try:
            order_id = live_order_manager.place_order(
                symbol, opt_type, strike, token, exchange, action, entry_price, expiry=expiry
            )
            trade['Order_ID'] = order_id
        except Exception as oe:
            print(f"[ORDER] place_order error: {oe}")

        return trade

    def update_tick(self, opt_type: str, strike: int, ltp: float, expiry: str = ""):
        """
        Called every tick for every watched strike.
        Updates live PnL, tracks Trade High & Low, calculates Maximum Results (MFE),
        and checks SL / Target exit conditions.
        Returns closed trade dict if trade exited, else None.
        """
        trade_key = (opt_type, strike, expiry) if (expiry and (opt_type, strike, expiry) in self.active_trades) else (opt_type, strike)
        if trade_key not in self.active_trades or ltp <= 0:
            return None

        trade    = self.active_trades[trade_key]
        time_str = datetime.now().strftime('%H:%M:%S')

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
                    if trade.get('Target_3', 0) > trade['SL']:
                        trade['SL'] = trade['Target_3']   # trail SL to T3
                    print(f"🎯 [FWD TEST] T4 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T5:{trade.get('Target_5',0):.1f} | Trailed SL:{trade['SL']:.1f}")
            elif trade.get('Target_3', 0) > 0 and ltp >= trade['Target_3']:
                if target_hit < 3:
                    trade['Target_Hit'] = 3
                    if trade.get('Target_2', 0) > trade['SL']:
                        trade['SL'] = trade['Target_2']   # trail SL to T2
                    print(f"🎯 [FWD TEST] T3 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T4:{trade.get('Target_4',0):.1f} | Trailed SL:{trade['SL']:.1f}")
            elif trade.get('Target_2', 0) > 0 and ltp >= trade['Target_2']:
                if target_hit < 2:
                    trade['Target_Hit'] = 2
                    if trade.get('Target_1', 0) > trade['SL']:
                        trade['SL'] = trade['Target_1']   # trail SL to T1
                    print(f"🎯 [FWD TEST] T2 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T3:{trade.get('Target_3',0):.1f} | Trailed SL:{trade['SL']:.1f}")
            elif trade.get('Target_1', 0) > 0 and ltp >= trade['Target_1']:
                if target_hit < 1:
                    trade['Target_Hit'] = 1
                    if entry > trade['SL']:
                        trade['SL'] = round(entry, 2)     # trail SL to Entry (breakeven)
                    print(f"🎯 [FWD TEST] T1 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{ltp-entry:.2f} | Support Sustained → Next: T2:{trade.get('Target_2',0):.1f} | Trailed SL:{trade['SL']:.1f}")
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
                    if trade['SL'] == 0 or trade.get('Target_3', 0) < trade['SL']:
                        trade['SL'] = trade['Target_3']
                    print(f"🎯 [FWD TEST] T4 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T5:{trade.get('Target_5',0):.1f} | Trailed SL:{trade['SL']:.1f}")
            elif trade.get('Target_3', 0) > 0 and ltp <= trade['Target_3']:
                if target_hit < 3:
                    trade['Target_Hit'] = 3
                    if trade['SL'] == 0 or trade.get('Target_2', 0) < trade['SL']:
                        trade['SL'] = trade['Target_2']
                    print(f"🎯 [FWD TEST] T3 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T4:{trade.get('Target_4',0):.1f} | Trailed SL:{trade['SL']:.1f}")
            elif trade.get('Target_2', 0) > 0 and ltp <= trade['Target_2']:
                if target_hit < 2:
                    trade['Target_Hit'] = 2
                    if trade['SL'] == 0 or trade.get('Target_1', 0) < trade['SL']:
                        trade['SL'] = trade['Target_1']
                    print(f"🎯 [FWD TEST] T2 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T3:{trade.get('Target_3',0):.1f} | Trailed SL:{trade['SL']:.1f}")
            elif trade.get('Target_1', 0) > 0 and ltp <= trade['Target_1']:
                if target_hit < 1:
                    trade['Target_Hit'] = 1
                    if trade['SL'] == 0 or entry < trade['SL']:
                        trade['SL'] = round(entry, 2)
                    print(f"🎯 [FWD TEST] T1 Hit — {trade['Action']} {opt_type} {strike} @ {ltp:.2f} | PnL:+{entry-ltp:.2f} | Resistance Sustained → Next: T2:{trade.get('Target_2',0):.1f} | Trailed SL:{trade['SL']:.1f}")

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

            # ── Live/Paper Exit Order ─────────────────────────────────────
            try:
                live_order_manager.exit_order(
                    trade.get('Symbol', ''),
                    opt_type, strike,
                    trade.get('Token', ''),
                    trade.get('Exchange', 'NFO'),
                    trade['Action'],
                    ltp, exit_reason,
                    expiry=trade.get('Expiry', expiry)
                )
            except Exception as oe:
                print(f"[ORDER] exit_order error: {oe}")
            trade['Live_PnL']    = exit_pnl
            trade['Status']      = 'CLOSED'
            self.active_trades.pop(trade_key, None)
            if (opt_type, strike) in self.active_trades and self.active_trades[(opt_type, strike)] is trade:
                self.active_trades.pop((opt_type, strike), None)

            # If first-time entry SL hit, allow re-entry with new HH/LL SL!
            date_str_close = datetime.now().strftime('%Y-%m-%d')
            exp_val = trade.get('Expiry', '')
            exp_lbl  = f" | Exp:{exp_val}" if exp_val else ""
            close_key = (opt_type, strike, exp_val, trade['Action'], date_str_close) if exp_val else (opt_type, strike, trade['Action'], date_str_close)
            
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
            icon = '✅' if exit_pnl >= 0 else '🛑'
            hl_label = f"High:{trade['Trade_High']:.2f}" if trade['Action'] == 'BUY' else f"Low:{trade['Trade_Low']:.2f}"
            print(f"{icon} [FWD TEST] CLOSED {trade['Action']} {opt_type} {strike}{exp_lbl} "
                  f"| {exit_reason} | PnL:{exit_pnl:+.2f} pts | {hl_label} (Max:{trade['Max_Pts']:+.2f} pts)")
            return trade
        return None

    def get_active_trades(self):
        """Return list of all currently active trades with live PnL."""
        return list(self.active_trades.values())

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

    def get_closed_today(self):
        """Return trades closed in the current session only (no CSV bleed-through)."""
        return list(self._closed_today)

    def manual_close(self, opt_type: str, strike: int, ltp: float,
                     reason: str = "Manual Close", expiry: str = "") -> dict:
        """
        Forcibly close an active tracked trade at the given LTP with a custom reason.
        Useful for:
          - End-of-day square-off ("Market EOD")
          - Manual exit detected from Angel One position API ("Market Closed")
          - Trader-initiated close from GUI ("Manual Close")
        Returns the closed trade dict, or None if no active trade found.
        """
        trade_key = (opt_type, strike, expiry) if (expiry and (opt_type, strike, expiry) in self.active_trades) else (opt_type, strike)
        if trade_key not in self.active_trades:
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

        self.active_trades.pop(trade_key, None)
        if (opt_type, strike) in self.active_trades and self.active_trades[(opt_type, strike)] is trade:
            self.active_trades.pop((opt_type, strike), None)

        date_str_mc = datetime.now().strftime('%Y-%m-%d')
        exp_val = trade.get('Expiry', '')
        if exp_val:
            self._closed_keys_today.add((opt_type, strike, exp_val, trade['Action'], date_str_mc))
        self._closed_keys_today.add((opt_type, strike, trade['Action'], date_str_mc))
        self._closed_today.append(dict(trade))
        self._update_trade_in_file(trade)

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
                        updated_trade['SL'], updated_trade['Target_1'],
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



def get_tv_multi_timeframe_pivots(symbol: str = "NIFTY") -> dict:
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
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Fast path: check local cache first
    cached_pivots = None
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as cf:
                all_cache = json.load(cf)
                s_c = all_cache.get(symbol, {})
                if s_c and "Daily" in s_c:
                    if "15-Min" not in s_c:
                        s_c["15-Min"] = s_c.get("5-Min", s_c.get("Daily", {}))
                    if "1-Hour" not in s_c:
                        s_c["1-Hour"] = s_c.get("Weekly", s_c.get("Daily", {}))
                    cached_pivots = {k: s_c[k] for k in ["Weekly", "Daily", "1-Hour", "15-Min", "5-Min"] if k in s_c}
                    if all(k in s_c for k in ["Weekly", "Daily", "1-Hour", "15-Min", "5-Min"]):
                        return cached_pivots
        except Exception as ce:
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

    candles = []
    for base_url in [
        'https://query2.finance.yahoo.com/v8/finance/chart',
        'https://query1.finance.yahoo.com/v8/finance/chart',
    ]:
        try:
            url = f'{base_url}/{y_sym}?range=2y&interval=1d&includePrePost=false'
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()['chart']['result'][0]
            timestamps = data.get('timestamp', [])
            q = data['indicators']['quote'][0]
            highs  = q.get('high',  [])
            lows   = q.get('low',   [])
            closes = q.get('close', [])
            opens  = q.get('open',  [])

            for i, ts in enumerate(timestamps):
                if ts is None: continue
                try:
                    h, l, c, o = highs[i], lows[i], closes[i], opens[i]
                except IndexError:
                    continue
                if None in (h, l, c, o): continue
                if h <= 0 or l <= 0 or c <= 0: continue
                if h < l: h, l = l, h
                d = datetime.fromtimestamp(ts).date()
                candles.append({
                    "date": d, "open": o, "high": h, "low": l, "close": c
                })
            if candles:
                break
        except Exception as e:
            continue

    if not candles:
        if cached_pivots:
            print(f"Notice: Using cached {symbol} TradingView pivots (Yahoo rate-limited)")
            return cached_pivots
        print("Warning: Could not fetch historical candles for TV pivots.")
        return {}

    now = datetime.now()
    today_date = now.date()
    is_live_today = (now.hour < 15 or (now.hour == 15 and now.minute < 30))

    # Exclude today if market is currently active/open
    completed_daily_candles = candles
    if completed_daily_candles[-1]["date"] == today_date and is_live_today:
        completed_daily_candles = completed_daily_candles[:-1]

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

    # 1. 15-Min & 5-Min charts (TradingView Auto -> Daily Pivot from previous completed trading day)
    c_day = completed_daily_candles[-1]
    piv_15m = _calc_fib(c_day["high"], c_day["low"], c_day["close"])
    piv_5m  = dict(piv_15m)

    # 2. 1-Hour chart (TradingView Auto -> Weekly Pivot from previous completed trading week)
    by_week = {}
    for c in completed_daily_candles:
        iso_wk = c['date'].isocalendar()[:2]
        by_week.setdefault(iso_wk, []).append(c)
    sorted_weeks = sorted(by_week.keys())
    latest_wk = sorted_weeks[-1]
    prev_wk = sorted_weeks[-2] if len(sorted_weeks) >= 2 else latest_wk
    cur_weekday = today_date.weekday()
    # In TradingView: on weekends (Sat/Sun) or Friday after market close or holidays,
    # the completed week is latest_wk; during Mon-Thu in-progress week, it uses prev_wk
    use_wk = latest_wk if (cur_weekday >= 4 or len(by_week[latest_wk]) >= 4) else prev_wk
    wk_candles = by_week[use_wk]
    piv_1h = _calc_fib(
        max(x["high"] for x in wk_candles),
        min(x["low"] for x in wk_candles),
        wk_candles[-1]["close"]
    )

    # 3. Daily (1D) chart (TradingView Auto -> Monthly Pivot from previous completed calendar month)
    by_month = {}
    for c in completed_daily_candles:
        m = (c['date'].year, c['date'].month)
        by_month.setdefault(m, []).append(c)
    sorted_months = sorted(by_month.keys())
    latest_m = sorted_months[-1]
    prev_m = sorted_months[-2] if len(sorted_months) >= 2 else latest_m
    use_m = prev_m if len(by_month[latest_m]) < 18 else latest_m
    m_candles = by_month[use_m]
    piv_daily = _calc_fib(
        max(x["high"] for x in m_candles),
        min(x["low"] for x in m_candles),
        m_candles[-1]["close"]
    )

    # 4. Weekly (1W) chart (TradingView Auto -> Yearly Pivot from previous completed calendar year)
    by_year = {}
    for c in completed_daily_candles:
        y = c['date'].year
        by_year.setdefault(y, []).append(c)
    sorted_years = sorted(by_year.keys())
    latest_y = sorted_years[-1]
    prev_y = sorted_years[-2] if len(sorted_years) >= 2 else latest_y
    use_y = prev_y if len(by_year[latest_y]) < 200 else latest_y
    y_candles = by_year[use_y]
    piv_weekly = _calc_fib(
        max(x["high"] for x in y_candles),
        min(x["low"] for x in y_candles),
        y_candles[-1]["close"]
    )

    results = {
        "Weekly": piv_weekly,
        "Daily":  piv_daily,
        "1-Hour": piv_1h,
        "15-Min": piv_15m,
        "5-Min":  piv_5m,
    }

    for label in ["Weekly", "Daily", "1-Hour", "15-Min", "5-Min"]:
        res = results[label]
        print(f"  [{label}] H:{res['H']} L:{res['L']} C:{res['C']} → "
              f"P:{res['P']} R1:{res['R1']} S1:{res['S1']} R2:{res['R2']} S2:{res['S2']}")

    # Save to persistent cache
    try:
        all_cache = {}
        if os.path.exists(cache_file):
            with open(cache_file, "r") as cf:
                all_cache = json.load(cf)
        all_cache[symbol] = {
            "date": today_str,
            "Weekly": piv_weekly,
            "Daily": piv_daily,
            "1-Hour": piv_1h,
            "15-Min": piv_15m,
            "5-Min": piv_5m,
        }
        with open(cache_file, "w") as cf:
            json.dump(all_cache, cf, indent=2)
    except Exception as ce:
        pass

    return results

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

    # SL is strictly the HH of the day after price reclaim/rejection (MUST be > ltp)
    if hh > 0 and hh > ltp:
        effective_sl = round(hh, 2)
    elif opt_high > 0 and opt_high > ltp:
        effective_sl = round(opt_high, 2)
    elif r3 > ltp:
        effective_sl = round(r3, 2)
    else:
        effective_sl = round(ltp * 1.10, 2)

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
    """Authenticate via OAuth2 and return a gspread client."""
    creds      = None
    token_file = os.path.join(BASE_DIR, "token.pickle")
    creds_file = os.path.join(BASE_DIR, "credentials.json")

    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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
            creds = flow.run_local_server(port=0)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)

    return gspread.authorize(creds)

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
        Load ScripMaster with local disk cache (scrip_cache.pkl).
        Cache is valid for 4 hours — instant restarts, no network wait.
        """
        import pickle as _pkl
        cache_path = os.path.join(BASE_DIR, "scrip_cache.pkl")
        max_age_sec = 4 * 3600   # refresh cache every 4 hours

        # Try loading from cache first
        if os.path.exists(cache_path):
            cache_age = time.time() - os.path.getmtime(cache_path)
            if cache_age < max_age_sec:
                try:
                    with open(cache_path, "rb") as f:
                        cls.scrip_df = _pkl.load(f)
                    print(f"   ScripMaster loaded from cache ({cache_age/60:.1f} min old) — {len(cls.scrip_df)} rows")
                    return
                except Exception as e:
                    print(f"   Cache load failed ({e}), fetching fresh...")

        # Download fresh
        print("   Downloading ScripMaster from AngelBroking (first run or cache expired)...")
        resp = requests.get(SCRIP_URL, timeout=60, stream=True)
        resp.raise_for_status()
        data = resp.json()
        cls.scrip_df = pd.DataFrame(data)

        # Save to cache
        try:
            with open(cache_path, "wb") as f:
                _pkl.dump(cls.scrip_df, f)
            print(f"   ScripMaster cached ({len(cls.scrip_df)} rows) → {cache_path}")
        except Exception as e:
            print(f"   Cache save failed: {e}")

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
        self.strike_pivots      = {}        # {token / (opt_type, strike) -> {P, S1, S2, S3, R1, R2, R3, H, L, C}}
        self.ft_timeframe_filter = "ALL"        # Timeframe filter: ALL, 15M Intraday, 1H Structure, 5M Dynamic
        self.exchange           = "NFO"          # current exchange (set during prepare)
        self._sheet_write_lock  = threading.Lock()  # prevent concurrent sheet writes
        self._ft_b2_cache       = "ALL"            # cached B2 filter from sheet
        self._ft_b2_last_read   = 0.0              # timestamp of last B2 read
        self._last_ft_format_ts = 0.0              # timestamp of last formatting call (quota guard)
        self._last_ft_sync_ts   = 0.0              # timestamp of last forward test sheet write
        self._last_sheet_write_ts = 0.0            # timestamp of last option chain sheet write
        self._quota_backoff_until = 0.0            # 429 quota backoff pause timestamp

    def prepare(self, exchange, symbol, expiries, n_strikes):
        """
        Prepare tokens for one OR multiple expiries.
        expiries: str  (single) or list[str]  (multi-expiry)
        """
        if isinstance(expiries, str):
            expiries = [expiries]

        cfg    = EXCHANGE_CONFIG.get(exchange, EXCHANGE_CONFIG["NFO"])
        step   = get_symbol_step(symbol, exchange)
        extype = cfg["exchange_type"]
        stok, spot_exch_type = get_symbol_spot_token(symbol, exchange)

        seg_candidates = [exchange]
        if exchange == "BFO": seg_candidates = ["BFO", "BSE"]
        if exchange == "MCX": seg_candidates = ["MCX"]

        all_filter_dfs = []
        primary_atm    = None

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

            targets = set(atm + i * step for i in range(-n_strikes, n_strikes + 1))
            # Guarantee ALL 15M and 1H focus strikes (both CE and PE) are always included in targets
            mtf_for_focus = self.mtf_pivots or get_tv_multi_timeframe_pivots(symbol)
            if mtf_for_focus:
                self.mtf_pivots = mtf_for_focus
                f_calc = select_daily_4_focus_strikes(mtf_for_focus, step)
                self.daily_focus = f_calc
                self.focus_15m = f_calc.get("15M", {})
                self.focus_1h = f_calc.get("1H", {})
                for _s in f_calc.get("all_focus", []):
                    if _s > 0: targets.add(_s)
                for _k in ("15M", "1H"):
                    _fo = f_calc.get(_k, {})
                    for _s in _fo.get("ce_strikes", []) + _fo.get("pe_strikes", []):
                        if _s > 0: targets.add(_s)

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

        # Return primary (first) expiry filter_df for backwards compat
        self.exchange = exchange   # store for log_entry calls
        return all_filter_dfs[0], primary_atm, step

    # ── Reversal order mapping helpers ─────────────────────────────────────
    def get_strike_ltp(self, opt_type: str, strike: int) -> float:
        """Live LTP for (opt_type, strike) from the WebSocket feed (nearest expiry first).
        Falls back to the last close in strike_pivots only when no live tick exists."""
        with self.lock:
            metas = [(tok, m) for tok, m in self.token_to_meta.items()
                     if m.get("opt_type") == opt_type and m.get("strike") == strike]
            live = dict(self.live_data)
            sp = self.strike_pivots.get((opt_type, strike)) or {}
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

    def load_or_fetch_strike_pivots(self, symbol: str, option_tokens: list, token_to_meta: dict):
        """
        Loads pre-calculated daily Fibonacci Pivots for all option strikes.
        Matches TradingView 'Pivots Fibonacci Auto' resolution exactly
        using the previous completed trading day's OHLC.
        """
        cache_path = os.path.join(BASE_DIR, "strike_pivot_cache.json")
        loaded_count = 0
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    cdata = json.load(f)
                    strikes_map = cdata.get("strikes", {})
                    for tok, s_info in strikes_map.items():
                        tok_str = str(tok)
                        self.strike_pivots[tok_str] = s_info
                        self.strike_pivots[(s_info["opt_type"], s_info["strike"])] = s_info
                        if "expiry" in s_info:
                            self.strike_pivots[(s_info["opt_type"], s_info["strike"], s_info["expiry"])] = s_info
                        loaded_count += 1
                print(f"[STRIKE PIVOTS] Loaded {loaded_count} strike pivots from {cache_path} (TradingView aligned)")
            except Exception as e:
                print(f"[STRIKE PIVOTS] Cache read error: {e}")

        missing = [t for t in option_tokens if t not in self.strike_pivots]
        if missing:
            print(f"[STRIKE PIVOTS] Notice: {len(missing)} strikes not in cache. Fetching snapshot...")
            missing_expiries = {token_to_meta[t]["expiry"] for t in missing if t in token_to_meta and "expiry" in token_to_meta[t]}
            if not missing_expiries:
                self.fetch_and_map_all_strikes(symbol, force_fetch=False)
            else:
                for exp in missing_expiries:
                    self.fetch_and_map_all_strikes(symbol, expiry=exp, force_fetch=False)

    def fetch_and_map_all_strikes(self, symbol=None, expiry=None, force_fetch=True) -> dict:
        """
        Maps next-day/intraday Fibonacci Pivots for all strikes
        matching TradingView's 'Pivots Fibonacci Auto' indicator exactly:
        P = (H + L + C) / 3, R1-R3, S1-S3.
        Uses Angel One WebSocket SNAP_QUOTE to get completed session OHLC.
        Saves results to strike_pivot_cache.json and updates self.strike_pivots.
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
            target_expiry = expiry or fut_exp.sort_values('exp_dt').iloc[0]['expiry']

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
                target_strike_set.update(focus_calc.get("all_focus_6", focus_calc.get("all_focus", [])))
            except Exception as e_foc:
                print(f"[STRIKE MAPPER] Focus calculation notice: {e_foc}")

            if not target_strike_set:
                target_strike_set = {atm - 2*step, atm - step, atm, atm + step, atm + 2*step}

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
                    'strike': r['strike_int'],
                    'opt_type': 'CE' if r['symbol'].endswith('CE') else 'PE',
                    'expiry': target_expiry
                }

            cache_file = os.path.join(BASE_DIR, "strike_pivot_cache.json")
            if not force_fetch and os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as cf:
                        cd = json.load(cf)
                        if cd.get("symbol") == symbol and cd.get("expiry") == target_expiry:
                            c_strikes = cd.get("strikes", {})
                            if c_strikes and len(c_strikes) >= len(tokens) * 0.8:
                                with self.lock:
                                    for t, s_info in c_strikes.items():
                                        self.strike_pivots[str(t)] = s_info
                                        self.strike_pivots[(s_info["opt_type"], s_info["strike"])] = s_info
                                return c_strikes
                except Exception:
                    pass

            results = {}
            done_event = threading.Event()

            def on_snap_data(wsapp, msg):
                tok = str(msg.get('token', ''))
                if tok in meta_map:
                    results[tok] = msg
                    if len(results) >= len(tokens):
                        done_event.set()

            def on_snap_open(wsapp):
                chunk_size = 50
                token_lists = []
                for i in range(0, len(tokens), chunk_size):
                    token_lists.append({'exchangeType': 2 if exch=="NFO" else 4, 'tokens': tokens[i:i+chunk_size]})
                sws_tmp.subscribe('snap_map', 3, token_lists)

            sws_tmp = SmartWebSocketV2(Session.auth_token, API_KEY, CLIENT_ID, Session.feed_token)
            sws_tmp.on_data = on_snap_data
            sws_tmp.on_open = on_snap_open
            th = threading.Thread(target=sws_tmp.connect, daemon=True)
            th.start()

            done_event.wait(timeout=2.0)
            try:
                sws_tmp.close_connection()
            except Exception:
                pass

            mapped = {}
            for tok, msg in results.items():
                m = meta_map[tok]
                h = msg.get('high_price_of_the_day', 0) / 100
                l = msg.get('low_price_of_the_day', 0) / 100
                c = msg.get('last_traded_price', 0) / 100
                if h <= 0: h = c
                if l <= 0: l = c
                piv = calc_fib_pivots(h, l, c)
                mapped[tok] = {
                    'token': tok,
                    'symbol': m['symbol'],
                    'opt_type': m['opt_type'],
                    'strike': m['strike'],
                    'expiry': m['expiry'],
                    'prev_h': round(h, 2),
                    'prev_l': round(l, 2),
                    'prev_c': round(c, 2),
                    **piv
                }

            nd = date.today() + timedelta(days=1)
            if nd.weekday() == 5: nd += timedelta(days=2)
            if nd.weekday() == 6: nd += timedelta(days=1)
            next_date = nd.strftime('%Y-%m-%d')

            existing_strikes = {}
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as f_ex:
                        existing_strikes = json.load(f_ex).get("strikes", {})
                except Exception:
                    pass
            existing_strikes.update(mapped)

            cache_data = {
                'date': next_date,
                'symbol': symbol,
                'expiry': target_expiry,
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strikes': existing_strikes
            }
            with open(cache_file, "w") as f:
                json.dump(cache_data, f, indent=2)

            with self.lock:
                for tok, s_info in mapped.items():
                    self.strike_pivots[str(tok)] = s_info
                    self.strike_pivots[(s_info["opt_type"], s_info["strike"])] = s_info
                    if "expiry" in s_info:
                        self.strike_pivots[(s_info["opt_type"], s_info["strike"], s_info["expiry"])] = s_info

            print(f"[STRIKE MAPPER] ✅ Mapped {len(mapped)} strikes for {symbol} ({target_expiry}) — 100% TradingView aligned")
            return mapped
        except Exception as ex:
            print(f"[STRIKE MAPPER] Error: {ex}")
            return {}

    # ── WebSocket ──────────────────────────────────────────
    def on_data(self, wsapp, message):
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

            self.live_data[token] = {
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

            # Auto-capture strike pivot if missing (during pre-market, weekend, or post-market)
            if token in self.token_to_meta and token not in self.strike_pivots:
                now_t = datetime.now()
                is_outside_live = (now_t.hour < 9 or (now_t.hour == 9 and now_t.minute < 15) or now_t.hour >= 16 or now_t.weekday() >= 5)
                if is_outside_live and high > 0 and low > 0 and ltp > 0:
                    piv = calc_fib_pivots(high, low, ltp)
                    meta = self.token_to_meta[token]
                    piv_entry = {
                        "token": token,
                        "symbol": meta.get("symbol", ""),
                        "opt_type": meta["opt_type"],
                        "strike": meta["strike"],
                        "expiry": meta.get("expiry", ""),
                        "prev_h": round(high, 2),
                        "prev_l": round(low, 2),
                        "prev_c": round(ltp, 2),
                        **piv
                    }
                    self.strike_pivots[token] = piv_entry
                    self.strike_pivots[(meta["opt_type"], meta["strike"])] = piv_entry

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

    def on_open(self,wsapp):
        self.sws.subscribe("oc001",3,self.token_list)
        print(f"WS subscribed to {sum(len(t['tokens']) for t in self.token_list)} tokens")

    def on_error(self,wsapp,error): print(f"WS error:{error}")
    def on_close(self,wsapp):       print("WS closed")

    def start_ws(self):
        self.sws=SmartWebSocketV2(
            Session.auth_token,API_KEY,CLIENT_ID,Session.feed_token,max_retry_attempt=5
        )
        self.sws.on_open=self.on_open; self.sws.on_data=self.on_data
        self.sws.on_error=self.on_error; self.sws.on_close=self.on_close
        threading.Thread(target=self.sws.connect,daemon=True).start()

    def stop_ws(self):
        try:
            if self.sws: self.sws.close_connection()
        except: pass

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
                                        {"userEnteredValue": "5M Dynamic"},
                                    ],
                                },
                                "inputMessage": "Select Timeframe to filter Forward Test trades (ALL, 15M, 1H, 5M)",
                                "showCustomUi": True,
                                "strict": False,
                            },
                        }
                    }
                    spreadsheet.batch_update({"requests": [req]})
                except Exception as dverr:
                    print(f"Data validation setup note: {dverr}")

                self.forward_test_sheet = ft_ws
                self.sync_forward_test_to_sheet()
                print("✅ Forward Test worksheet ready with Timeframe Filter")
            except Exception as bg_err:
                print(f"[BG Setup Error] {bg_err}")

        threading.Thread(target=_setup_sheet_bg, daemon=True).start()

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
                        elif "5" in b2_u:
                            self._ft_b2_cache = "5M Dynamic"
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
                    return "5M Dynamic"

            def _matches_filter(tf, filt):
                if not filt or filt == "ALL":
                    return True
                tf_u = (tf or "").upper()
                fl_u = filt.upper()
                if "15" in fl_u:
                    return "15M" in tf_u or "15-MIN" in tf_u
                if "1H" in fl_u or "HOUR" in fl_u:
                    return "1H" in tf_u or "HOUR" in tf_u
                if "5" in fl_u:
                    return "5M" in tf_u
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
                    hl_str,       # Trade High (BUY) / Low (SELL)
                    max_str,      # Max Result points
                    t.get('SL', 0),
                    t.get('Target_1', 0),
                    t.get('Target_2', 0),
                    t.get('Target_3', 0),
                    t.get('Target_4', 0),
                    t.get('Target_5', 0),
                    f"🎯 T{t.get('Target_Hit', 0)} Hit" if t.get('Target_Hit', 0) > 0 else '⚡ LIVE',
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
                    hl_str,       # Trade High (BUY) / Low (SELL)
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
            pnl_5m  = sum(_safe_float(r.get('PnL_Pts', 0)) for r in closed_rows_raw if _matches_filter(_get_trade_tf(r), "5M Dynamic"))

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
                'Trade High/Low', 'Max Result (Pts)',
                'SL', 'T1', 'T2', 'T3', 'T4', 'T5',
                'Reason', 'Status', 'Trade_ID'
            ]

            # ── Step 5: Assemble the full grid ──────────────────────────────
            pnl_icon = "🟢" if filt_net_pnl >= 0 else "🔴"
            live_icon = "🟢" if filt_live_pnl >= 0 else "🔴"

            dash_row = [
                f"📊 FORWARD TEST DASHBOARD  |  Filter: {current_filter}  |  "
                f"Net PnL (Closed): {pnl_icon} {net_pnl_all:+.2f} pts  |  "
                f"Max Potential Result: 🔥 +{total_max_pts_all:+.2f} pts  |  "
                f"15M Intraday: {pnl_15m:+.2f} pts  |  "
                f"1H Structure: {pnl_1h:+.2f} pts  |  "
                f"5M Dynamic: {pnl_5m:+.2f} pts  |  "
                f"Updated: {now_str}"
            ] + [''] * 20

            filter_bar = [
                "🎯 TIMEFRAME FILTER:",
                current_filter,
                "👈 Click/Select dropdown in B2 (ALL | 15M Intraday | 1H Structure | 5M Dynamic)",
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
                _market_open = (now_dt.hour == 9 and now_dt.minute == 16)
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
                        if self.daily_focus and self.focus_1h and not _market_open:
                            focus = self.daily_focus
                        else:
                            focus = select_daily_4_focus_strikes(mtf, step)
                        with self.lock:
                            self.mtf_pivots     = mtf
                            self.daily_focus    = focus
                            self.focus_15m      = self.focus_15m or focus.get("15M", {})
                            self.focus_1h       = self.focus_1h or focus.get("1H", {})
                            self._focus_date    = today_date
                            self._alert_strikes = set()
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
                        print(f"[PIVOT] 15M Focus: CE1={f15.get('CE1')} CE2={f15.get('CE2')} | PE1={f15.get('PE1')} PE2={f15.get('PE2')}")
                        print(f"[PIVOT] 1H Focus:  CE1={f1h.get('CE1')} CE2={f1h.get('CE2')} | PE1={f1h.get('PE1')} PE2={f1h.get('PE2')}")
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
                        import json as _jmod, glob as _gmod, os as _omod
                        _plan_files = _gmod.glob(_omod.join(_omod.dirname(_omod.abspath(__file__)),
                                                f"next_day_plan_{today_date.replace('-','')}.json"))
                        if _plan_files:
                            with open(_plan_files[0]) as _pf:
                                _nd_plan_pivots = _jmod.load(_pf).get("pivots", {})
                            print(f"[PLAN] Loaded next-day plan: S2={_nd_plan_pivots.get('S2',0):.0f} "
                                  f"P={_nd_plan_pivots.get('P',0):.0f} R2={_nd_plan_pivots.get('R2',0):.0f}")
                        else:
                            _nd_plan_pivots = {}
                    except Exception:
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

                    if token in snapshot:
                        d         = snapshot[token]
                        ltp       = d.get("ltp", 0.0)
                        opt_high  = d.get("high", ltp)
                        opt_low   = d.get("low", ltp)
                        opt_close = d.get("close", ltp)
                    else:
                        sp = self.strike_pivots.get(token) or self.strike_pivots.get((opt_type, strike))
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
                    strike_piv = self.strike_pivots.get(token) or self.strike_pivots.get((opt_type, strike))
                    if strike_piv:
                        opt_piv = strike_piv
                    else:
                        opt_piv = calc_fib_pivots(opt_high, opt_low, opt_close)
                        if opt_high > 0 and opt_low > 0:
                            # Freeze and lock immediately so strike pivot prices remain completely stable and never drift
                            self.strike_pivots[token] = opt_piv
                            self.strike_pivots[(opt_type, strike)] = opt_piv
                            _exp_m = meta.get("expiry")
                            if _exp_m:
                                self.strike_pivots[(opt_type, strike, _exp_m)] = opt_piv

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
                            self.strategy_states[buy_key] = st_buy
                        st_sell  = self.strategy_states.get(sell_key)
                        if not st_sell:
                            st_sell = StrategyStrikeState(opt_type, strike, expiry=exp, mode="SELL")
                            self.strategy_states[sell_key] = st_sell

                        legacy_key = (opt_type, strike, exp) if exp else (opt_type, strike)
                        self.strategy_states[legacy_key] = st_buy if st_buy.state != "WAITING" else (st_sell if st_sell.state != "WAITING" else st_buy)
                        if (opt_type, strike) not in self.strategy_states:
                            self.strategy_states[(opt_type, strike)] = self.strategy_states[legacy_key]

                    now_tick = datetime.now()
                    st_buy.update_tick(ltp, opt_piv, now_tick, opt_high=opt_high, opt_low=opt_low)
                    st_sell.update_tick(ltp, opt_piv, now_tick, opt_high=opt_high, opt_low=opt_low)

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
                    f15_set = set((self.focus_15m or daily_focus.get("15M", {})).get("all_4", []))
                    f1h_set = set((self.focus_1h or daily_focus.get("1H", {})).get("all_4", []))
                    if strike in f15_set and strike in f1h_set:
                        trade_tf = "15M / 1H"
                    elif strike in f15_set:
                        trade_tf = "15M Intraday"
                    elif strike in f1h_set:
                        trade_tf = "1H Structure"
                    else:
                        trade_tf = "5M Dynamic"

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

                    # Entries & Alerts strictly restricted to LIVE MARKET AND 15M / 1H FOCUS strikes
                    is_15m_focus = (strike in daily_focus.get("all_15m", [])) or (strike in f15_set)
                    is_1h_focus  = (strike in daily_focus.get("all_1h", []))  or (strike in f1h_set)
                    is_focus_strike = (is_15m_focus or is_1h_focus)
                    mkt_open = is_live_market_open(now_tick)

                    # ── Evaluate SELL Entry ──
                    fp_sell = getattr(st_sell, "frozen_pivots", None) or opt_piv
                    if sell_entry["entry"] and mkt_open and is_focus_strike:
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
                            self.alert_mgr.send_alert_once(
                                symbol, opt_type, strike, sig_act, ltp,
                                f"{sell_ctx} | {lvl} Reject | New HH SL:{sell_entry['SL']:.1f}",
                                sell_entry['SL'], sell_entry['T1']
                            )
                            self.forward_tracker.log_entry(
                                symbol, opt_type, strike, "SELL",
                                st_sell.entry_price,          # frozen R2/R1 — NOT ltp
                                st_sell.sl,                   # HH locked at sweep
                                st_sell.targets[0] if len(st_sell.targets) > 0 else sell_entry['T1'],
                                st_sell.targets[1] if len(st_sell.targets) > 1 else sell_entry['T2'],
                                st_sell.targets[2] if len(st_sell.targets) > 2 else sell_entry['T3'],
                                st_sell.targets[3] if len(st_sell.targets) > 3 else sell_entry['T4'],
                                st_sell.targets[4] if len(st_sell.targets) > 4 else sell_entry['T5'],
                                opt_piv=opt_piv, timeframe=trade_tf,
                                token=token, exchange=self.exchange,
                                expiry=exp
                            )

                    # ── Evaluate BUY Entry ──
                    fp_buy = getattr(st_buy, "frozen_pivots", None) or opt_piv
                    if buy_entry["entry"] and mkt_open and is_focus_strike:
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
                            self.alert_mgr.send_alert_once(
                                symbol, opt_type, strike, sig_act, ltp,
                                f"{buy_ctx} | {lvl} Reclaim | New LL SL:{buy_entry['SL']:.1f}",
                                buy_entry['SL'], buy_entry['T1']
                            )
                            self.forward_tracker.log_entry(
                                symbol, opt_type, strike, "BUY",
                                st_buy.entry_price,          # frozen S2/S1 — NOT ltp
                                st_buy.sl,                   # LL locked at sweep
                                st_buy.targets[0] if len(st_buy.targets) > 0 else buy_entry['T1'],
                                st_buy.targets[1] if len(st_buy.targets) > 1 else buy_entry['T2'],
                                st_buy.targets[2] if len(st_buy.targets) > 2 else buy_entry['T3'],
                                st_buy.targets[3] if len(st_buy.targets) > 3 else buy_entry['T4'],
                                st_buy.targets[4] if len(st_buy.targets) > 4 else buy_entry['T5'],
                                opt_piv=opt_piv, timeframe=trade_tf,
                                token=token, exchange=self.exchange,
                                expiry=exp
                            )

                    _s2_raw = opt_piv.get("S2", 0)
                    is_s1_buy = (_s2_raw <= 0.05 or st_buy.trigger_level == "S1")

                    # BUY Signal text:
                    if st_buy.state == "CONFIRMED_BUY" and (st_buy.trade_status == "Active" or "Target" in st_buy.trade_status):
                        lvl = st_buy.trigger_level
                        ep  = st_buy.entry_price
                        h1_tag = f" | {h1_buy_action}" if h1_buy_action else ""
                        tg_tag = f" | {st_buy.trade_status}" if st_buy.trade_status != "Active" else ""
                        signal_buy = f"★ BUY {opt_type} [{buy_ctx} | {lvl} Reclaim | Entry@{lvl}:{ep:.2f} | Trailed SL:{st_buy.sl:.1f}{tg_tag}{h1_tag}]"
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
                            _b_ep  = round(st_buy.entry_price if st_buy.entry_price > 0 else (st_buy.frozen_s1 or opt_piv.get("S1", 0)), 2)
                            signal_buy = f"★ BUY {opt_type} [Entry@S1:{_b_ep:.1f} | LL SL (S2<=0)]"
                        else:
                            _b_lvl = "S2"
                            _b_ep  = round(st_buy.entry_price if st_buy.entry_price > 0 else (st_buy.frozen_s2 or _s2_raw), 2)
                            signal_buy = f"★ BUY {opt_type} [Entry@S2:{_b_ep:.1f} | LL SL]"

                    # SELL Signal text:
                    if st_sell.state == "CONFIRMED_SELL" and (st_sell.trade_status == "Active" or "Target" in st_sell.trade_status):
                        lvl = st_sell.trigger_level
                        ep  = st_sell.entry_price
                        h1_tag = f" | {h1_sell_action}" if h1_sell_action else ""
                        tg_tag = f" | {st_sell.trade_status}" if st_sell.trade_status != "Active" else ""
                        signal_sell = f"▼ SELL {opt_type} [{sell_ctx} | {lvl} Reject | Entry@{lvl}:{ep:.2f} | Trailed SL:{st_sell.sl:.1f}{tg_tag}{h1_tag}]"
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
                    closed_t = self.forward_tracker.update_tick(opt_type, strike, ltp, expiry=exp)
                    if closed_t:
                        self.sync_forward_test_to_sheet()

                    # Stale-reset hook: sync ForwardTestTracker with state machines
                    for _st_inst in (st_buy, st_sell):
                        if _st_inst.stale_reset:
                            _st_inst.stale_reset = False
                            closed_stale = self.forward_tracker.manual_close(
                                opt_type, strike, ltp, reason="Stale Entry Reset", expiry=exp
                            )
                            if closed_stale:
                                _stale_date = datetime.now().strftime('%Y-%m-%d')
                                _stale_key  = (opt_type, strike, exp, closed_stale['Action'], _stale_date) if exp else (opt_type, strike, closed_stale['Action'], _stale_date)
                                self.forward_tracker._closed_keys_today.discard(_stale_key)
                                self.forward_tracker._closed_keys_today.discard((opt_type, strike, closed_stale['Action'], _stale_date))
                                print(f"⚠️  [STALE RESET] {opt_type} {strike}{' ' + exp if exp else ''}: "
                                      f"old entry={closed_stale['Entry_Price']:.2f} cleared | fresh signal will re-form")
                                self.sync_forward_test_to_sheet()

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
                            calc_ep_b = round(st_buy.entry_price if st_buy.entry_price > 0 else (st_buy.frozen_s1 if st_buy.frozen_s1 > 0 else disp_s1_b), 2)
                            zone_text = f"{strike_tag} Buy Zone Above S1 ({calc_ep_b:.1f}) [S2<=0]".strip()
                            plan_buy_zone = f"BUY S1={disp_s1_b:.1f} (S2<=0) | T1:{disp_p_b:.1f} T2:{disp_r1_b:.1f}"
                            tgts_b = list(st_buy.targets) if st_buy.targets else [disp_p_b, disp_r1_b, disp_r2_b, disp_r3_b, round(disp_r3_b * 1.05, 2)]
                        else:
                            calc_ep_b = round(st_buy.entry_price if st_buy.entry_price > 0 else (st_buy.frozen_s2 if st_buy.frozen_s2 > 0 else disp_s2_b), 2)
                            zone_text = f"{strike_tag} Buy Zone Above S2 ({calc_ep_b:.1f})".strip()
                            plan_buy_zone = f"BUY S2={disp_s2_b:.1f} | T1:{disp_s1_b:.1f} P:{disp_p_b:.1f}"
                            tgts_b = list(st_buy.targets) if st_buy.targets else [disp_s1_b, disp_p_b, disp_r1_b, disp_r2_b, disp_r3_b]

                        if st_buy.sl > 0:
                            calc_sl_b = round(st_buy.sl, 2)
                        elif st_buy.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit") or "RECLAIM" in st_buy.state or "CONFIRMED_BUY" in st_buy.state:
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

                        calc_ep_s = round(st_sell.entry_price if st_sell.entry_price > 0 else (disp_r2_s if disp_r2_s > 1.0 else disp_r1_s), 2)
                        if st_sell.sl > 0:
                            calc_sl_s = round(st_sell.sl, 2)
                        elif st_sell.trade_status in ("Active", "Target 1 Hit", "Target 2 Hit") or "REJECT" in st_sell.state or "CONFIRMED_SELL" in st_sell.state:
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
                        if trade_st_row in ("Active", "Target 1 Hit", "Target 2 Hit") or "RECLAIM" in m5_st or "CONFIRMED_BUY" in m5_st:
                            eff_sl = ll_val if (0 < ll_val < planned_ep) else (sl_val if (0 < sl_val < planned_ep) else round(planned_ep * 0.90, 2))
                            sl_str = f"{eff_sl:.2f}"
                        else:
                            sl_str = "-"
                    else:
                        planned_ep = round(w.get("r2", 0) if w.get("r2", 0) > 1.0 else w.get("r1", 0), 2)
                        # SL is calculated AFTER entry: HH when price rejects R2 and candle close gives entry
                        if trade_st_row in ("Active", "Target 1 Hit", "Target 2 Hit") or "REJECT" in m5_st or "CONFIRMED_SELL" in m5_st:
                            eff_sl = hh_val if hh_val > planned_ep else (sl_val if sl_val > planned_ep else round(planned_ep * 1.10, 2))
                            sl_str = f"{eff_sl:.2f}"
                        else:
                            sl_str = "-"

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
                    f"15M FOCUS STRIKES ➜ CE1: {f15.get('CE1','-')}  CE2: {f15.get('CE2','-')} (Resistance) | "
                    f"PE1: {f15.get('PE1','-')}  PE2: {f15.get('PE2','-')} (Support)"
                )

                # ── ROW 3: 1-Hour Timeframe Section Banner ──────────────────
                f1h = self.focus_1h or daily_focus.get("1H", {})
                r3_text = (
                    f"⏳ 1-HOUR TIMEFRAME PIVOTS: P={h1_piv_mtf.get('P','-')} S1={h1_piv_mtf.get('S1','-')} S2={h1_piv_mtf.get('S2','-')} R1={h1_piv_mtf.get('R1','-')} R2={h1_piv_mtf.get('R2','-')}  ||  "
                    f"1H STRUCTURE STRIKES ➜ CE1: {f1h.get('CE1','-')}  CE2: {f1h.get('CE2','-')} (Resistance) | "
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
                        now_t = time.time()
                        if now_t < getattr(self, '_quota_backoff_until', 0.0):
                            return
                        if now_t - getattr(self, '_last_sheet_write_ts', 0.0) < 6.0:
                            return
                        self._last_sheet_write_ts = now_t
                        ws.update(values=grid, range_name=f"A1:X{len(grid)}")
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
                if (_n_active > 0 and loop % 30 == 0) or loop % 100 == 0:
                    self.sync_forward_test_to_sheet()   # throttled non-blocking
                if loop % 20 == 0 and status_cb:
                    status_cb(
                        f"● LIVE | spot:{spot} ({sp_zone}) | {len(all_watch)} active watch | {now_str}"
                    )

                # ── Auto-run eod_planner.py after 15:40 (once per day, non-blocking) ──
                _h15, _m15 = now_dt.hour, now_dt.minute
                _after_close = (_h15 == 15 and _m15 >= 40) or _h15 >= 16
                if _after_close and getattr(self, '_eod_planner_done_date', None) != today_date:
                    self._eod_planner_done_date = today_date
                    try:
                        import subprocess as _sp, os as _os
                        _planner = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'eod_planner.py')
                        _sp.Popen(['python3', _planner],
                                  stdout=open('/tmp/eod_planner_output.log', 'w'),
                                  stderr=_sp.STDOUT)
                        print(f"\n[EOD] ⏰ 15:40+ → Auto-running eod_planner.py in background...")
                        print(f"[EOD] Output → /tmp/eod_planner_output.log\n")
                    except Exception as _ep_err:
                        print(f"[EOD] eod_planner auto-run error: {_ep_err}")

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
                 font=("Consolas", 9, "bold"), padx=12, pady=6,
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
class ControlPanel:
    def __init__(self,root):
        self.root=root
        root.title("Angel One — Live Option Chain & Fibonacci Pivot Reversal")
        root.geometry("1440x950")
        root.minsize(1200, 750)
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
                    font=("Consolas", 9),
                    rowheight=25)
        s.configure("Treeview.Heading",
                    background="#161b22",
                    foreground="#58a6ff",
                    font=("Consolas", 9, "bold"),
                    relief="flat")
        s.map("Treeview", background=[("selected", "#1f6feb")])
        # NOTE: tag_configure is called after self.tree is created in _build_ui

    def _build_ui(self):
        r=self.root
        tk.Label(r,text="FIBONACCI PIVOT OPTION CHAIN & REVERSAL STRATEGY",bg="#0d1117",fg="#58a6ff",
                 font=("Consolas",15,"bold")).pack(pady=(12,2))
        tk.Label(r,text="1H Support/Resistance Strikes  •  5M Liquidity Sweep & Reclaim  •  Live Alerts & Google Sheets",
                 bg="#0d1117",fg="#8b949e",font=("Consolas",8)).pack()

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
        DarkButton(sym_f, text="⚡ Calc Levels", command=self._show_symbol_calculator_window,
                   bg="#1f6feb", fg="#ffffff", activebackground="#388bfd",
                   font=("Consolas", 8, "bold"), padx=6, pady=2).pack(side="left", padx=(6, 0))

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

        # ── Spot & Fibonacci Pivot Monitor Frame ────────────
        sp_frame=tk.Frame(r,bg="#161b22",highlightthickness=1,highlightbackground="#30363d")
        sp_frame.pack(fill="x",padx=16,pady=6)

        tk.Label(sp_frame,text="UNDERLYING, 15M INTRADAY & 1H STRUCTURE STATUS",bg="#161b22",fg="#ffd700",
                 font=("Consolas",8,"bold")).pack(anchor="w",padx=10,pady=(6,2))

        self.spot_lbl=tk.Label(sp_frame,textvariable=self.spot_info,
                               bg="#161b22",fg="#58a6ff",font=("Consolas",10,"bold"),anchor="w")
        self.spot_lbl.pack(fill="x",padx=10,pady=2)

        # ── 15-Minute Intraday Section Display ──────────────
        self.sec_15m_lbl=tk.Label(sp_frame,textvariable=self.sec_15m_info,
                                  bg="#161b22",fg="#38d9a9",font=("Consolas",8,"bold"),anchor="w")
        self.sec_15m_lbl.pack(fill="x",padx=10,pady=1)

        # ── 1-Hour Timeframe Section Display ────────────────
        self.sec_1h_lbl=tk.Label(sp_frame,textvariable=self.sec_1h_info,
                                 bg="#161b22",fg="#ffd43b",font=("Consolas",8,"bold"),anchor="w")
        self.sec_1h_lbl.pack(fill="x",padx=10,pady=1)

        # ── Multi-Timeframe Pivots Display ───────────────────
        self.mtf_info = tk.StringVar(value="MTF Pivots: Waiting for first daily fetch...")

        self.mtf_lbl=tk.Label(sp_frame,textvariable=self.mtf_info,
                               bg="#161b22",fg="#6e7681",font=("Consolas",7),anchor="w",
                               wraplength=850,justify="left")
        self.mtf_lbl.pack(fill="x",padx=10,pady=(2,6))

        # ── Reversal Strategy Watchlist (Live Dashboard from Strategy PDF) ───
        rev_frame=tk.Frame(r,bg="#161b22",highlightthickness=1,highlightbackground="#30363d")
        rev_frame.pack(fill="both",expand=True,padx=16,pady=4)

        rev_hdr=tk.Frame(rev_frame,bg="#161b22")
        rev_hdr.pack(fill="x",padx=10,pady=(4,2))
        tk.Label(rev_hdr,text="LIVE FIBONACCI REVERSAL DASHBOARD (15M Intraday & 1H Structure Sweeps)",bg="#161b22",fg="#3fb950",
                 font=("Consolas",8,"bold")).pack(side="left")

        # Filter buttons
        rf = tk.Frame(rev_frame, bg="#161b22")
        rf.pack(fill="x", padx=8, pady=(0, 4))
        self.filter_var = tk.StringVar(value="FOCUS_ALL")
        for val, txt in [
            ("FOCUS_ALL",    "🎯 Focus (Dual BUY/SELL)"),
            ("BUY_ONLY",     "🟢 Buy Setups (S2/S1)"),
            ("SELL_ONLY",    "🔴 Sell Setups (R2/R1)"),
            ("FOCUS_15M",    "⭐ 15M Focus"),
            ("FOCUS_1H",     "⏳ 1H Focus"),
            ("TODAY_LEVELS", "📅 Today's Levels"),
            ("ALL",          "All Setups"),
        ]:
            tk.Radiobutton(rf, text=txt, value=val, variable=self.filter_var,
                           bg="#161b22", fg="#ffffff", selectcolor="#0d1117",
                           activebackground="#161b22", activeforeground="#58a6ff",
                           highlightbackground="#161b22",
                           font=("Consolas", 9, "bold"), command=self._refresh_display).pack(side="left", padx=4)

        # Expiry filter buttons right in dashboard toolbar
        tk.Label(rf, text=" │  📅 Expiry:", bg="#161b22", fg="#ffd700",
                 font=("Consolas", 9, "bold")).pack(side="left", padx=(8, 3))
        for val, txt, col in [
            ("ALL",     "All",             "#ffffff"),
            ("CURRENT", "⚡ Current Expiry", "#3fb950"),
            ("NEXT",    "⏭ Next Expiry",    "#58a6ff"),
        ]:
            tk.Radiobutton(rf, text=txt, value=val, variable=self.expiry_filter_var,
                           bg="#161b22", fg=col, selectcolor="#0d1117",
                           activebackground="#161b22", activeforeground="#ffd700",
                           highlightbackground="#161b22",
                           font=("Consolas", 9, "bold"), command=self._refresh_display).pack(side="left", padx=3)

        # Treeview for strikes near Support / Resistance
        tree_frame = tk.Frame(rev_frame, bg="#161b22")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        cols = ("Expiry", "Type", "Strike", "Side", "LTP", "OI", "Vol", "Trading Zone", "P", "S1", "S2", "R1", "R2", "5M State", "LL", "HH", "Signal", "SL", "Entry", "T1", "T2", "T3", "Status",
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
            "Status":       {"width": 95,  "anchor": "center", "minwidth": 75},
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

        # ── Google Sheet link & Max OI panel ────────────────
        row_b=tk.Frame(r,bg="#0d1117")
        row_b.pack(fill="x",padx=16,pady=4)

        sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        DarkButton(row_b, text="📊 Open Google Sheet ↗",
                   command=lambda: __import__("webbrowser").open(sheet_url),
                   bg="#1f6feb", fg="#ffffff",
                   activebackground="#388bfd", activeforeground="#ffffff",
                   font=("Consolas", 10, "bold"),
                   padx=14, pady=5).pack(side="left")

        # ── Forward Test Timeframe Filter Toolbar ──────────────
        tk.Label(row_b, text="🎯 FT Filter:", bg="#0d1117", fg="#ffd700",
                 font=("Consolas", 8, "bold")).pack(side="left", padx=(12, 3))
        self.ft_tf_filter_var = tk.StringVar(value="ALL")

        def _on_ft_filter_selected(v):
            feed.ft_timeframe_filter = v
            threading.Thread(target=feed.sync_forward_test_to_sheet, daemon=True).start()

        for val, txt in [
            ("ALL", "ALL"),
            ("15M Intraday", "15M"),
            ("1H Structure", "1H"),
            ("5M Dynamic", "5M")
        ]:
            tk.Radiobutton(row_b, text=txt, value=val, variable=self.ft_tf_filter_var,
                           bg="#0d1117", fg="#ffffff", selectcolor="#161b22",
                           activebackground="#0d1117", activeforeground="#58a6ff",
                           highlightbackground="#0d1117",
                           font=("Consolas", 9, "bold"),
                           command=lambda v=val: _on_ft_filter_selected(v)).pack(side="left", padx=3)

        self.oi_lbl=tk.Label(row_b,textvariable=self.max_oi_info,
                             bg="#0d1117",fg="#ffd700",font=("Consolas",8))
        self.oi_lbl.pack(side="right")

        # ── Live Order Execution Controls ─────────────────────────────────
        order_frame = tk.Frame(r, bg="#0d1117", highlightthickness=1, highlightbackground="#30363d")
        order_frame.pack(fill="x", padx=16, pady=(2, 4))

        tk.Label(order_frame, text="⚡ ORDER EXECUTION:",
                 bg="#0d1117", fg="#ffd700", font=("Consolas", 9, "bold")).pack(side="left", padx=(8,4))

        # Paper / Live mode toggle
        self.order_mode_var = tk.StringVar(value="PAPER")

        def _toggle_order_mode():
            new_mode = self.order_mode_var.get()
            if new_mode == "LIVE":
                # Extra confirmation dialog before going LIVE
                from tkinter import messagebox as mb
                ok = mb.askyesno(
                    "⚠️ LIVE ORDER MODE",
                    "You are about to enable REAL order placement.\n\n"
                    "Orders will be sent to Angel One SmartAPI and executed in the LIVE MARKET.\n\n"
                    "Are you sure you want to switch to LIVE mode?",
                    icon="warning"
                )
                if not ok:
                    self.order_mode_var.set("PAPER")
                    return
            live_order_manager.set_mode(new_mode)
            color = "#da3633" if new_mode == "LIVE" else "#238636"
            self.order_mode_lbl.config(fg=color, text=f"Mode: {new_mode}")
            self._update_order_status(f"Order mode → {new_mode}")

        tk.Radiobutton(order_frame, text="📋 PAPER", value="PAPER",
                       variable=self.order_mode_var,
                       bg="#0d1117", fg="#3fb950", selectcolor="#161b22",
                       activebackground="#0d1117", activeforeground="#3fb950",
                       highlightbackground="#0d1117",
                       font=("Consolas", 9, "bold"),
                       command=_toggle_order_mode).pack(side="left", padx=6)

        tk.Radiobutton(order_frame, text="🔴 LIVE", value="LIVE",
                       variable=self.order_mode_var,
                       bg="#0d1117", fg="#ff7b72", selectcolor="#161b22",
                       activebackground="#0d1117", activeforeground="#ff7b72",
                       highlightbackground="#0d1117",
                       font=("Consolas", 9, "bold"),
                       command=_toggle_order_mode).pack(side="left", padx=6)

        # Quantity (lots) spinbox
        tk.Label(order_frame, text="Qty(lots):",
                 bg="#0d1117", fg="#8b949e", font=("Consolas", 8)).pack(side="left", padx=(10,2))
        self.qty_var = tk.IntVar(value=1)

        def _on_qty_change(*_):
            try:
                q = self.qty_var.get()
                live_order_manager.set_qty(q)
                self._update_order_status(f"Qty set to {q} lot(s)")
            except Exception:
                pass

        qty_spin = tk.Spinbox(order_frame, from_=1, to=50, textvariable=self.qty_var,
                              width=4, bg="#21262d", fg="#f0f6fc", font=("Consolas", 9),
                              insertbackground="#f0f6fc", relief="flat", bd=0,
                              buttonbackground="#30363d", command=_on_qty_change)
        qty_spin.pack(side="left")

        # Order status display
        self.order_status_var = tk.StringVar(value="📋 Paper Mode — no real orders")
        self.order_mode_lbl = tk.Label(order_frame, textvariable=self.order_status_var,
                                       bg="#0d1117", fg="#238636",
                                       font=("Consolas", 8), anchor="w")
        self.order_mode_lbl.pack(side="left", padx=10, fill="x", expand=True)

        # View Orders Log button
        def _open_orders_log():
            import subprocess
            subprocess.Popen(["open", ORDER_LOG_FILE])
        DarkButton(order_frame, text="📂 Orders Log",
                   command=_open_orders_log,
                   bg="#21262d", fg="#79c0ff",
                   activebackground="#30363d", activeforeground="#ffffff",
                   font=("Consolas", 9, "bold"),
                   padx=12, pady=4).pack(side="right", padx=6)

        # ── Strike Level Tools Toolbar ──────────────
        tool_frame = tk.Frame(r, bg="#0d1117")
        tool_frame.pack(fill="x", padx=16, pady=(3, 3))

        DarkButton(tool_frame, text="📐 Map Strike Levels (TV Exact)",
                   command=self._on_map_strike_levels_click,
                   bg="#196c2e", fg="#ffffff",
                   activebackground="#2ea043", activeforeground="#ffffff",
                   font=("Consolas", 9, "bold"),
                   padx=14, pady=6).pack(side="left", padx=(0, 6))

        DarkButton(tool_frame, text="⚡ Gap Check (09:16 AM)",
                   command=self._on_gap_check_click,
                   bg="#6e40c9", fg="#ffffff",
                   activebackground="#8957e5", activeforeground="#ffffff",
                   font=("Consolas", 9, "bold"),
                   padx=14, pady=6).pack(side="left", padx=6)

        DarkButton(tool_frame, text="🎯 Calculate Symbol & Levels",
                   command=self._show_symbol_calculator_window,
                   bg="#0969da", fg="#ffffff",
                   activebackground="#1f6feb", activeforeground="#ffffff",
                   font=("Consolas", 9, "bold"),
                   padx=14, pady=6).pack(side="left", padx=6)

        # ── Start / Stop Buttons ────────────────────────────
        bf=tk.Frame(r,bg="#0d1117")
        bf.pack(fill="x",padx=16,pady=6)
        self.start_btn = DarkButton(bf, text="▶ START SCANNER & FEED", command=self.start_feed,
                                    bg="#238636", fg="#ffffff",
                                    activebackground="#2ea043", activeforeground="#ffffff",
                                    disabledbackground="#21262d", disabledforeground="#8b949e",
                                    font=("Consolas", 11, "bold"), pady=10)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.calc_btn = DarkButton(bf, text="📐 CALC ANY SYMBOL", command=self._show_symbol_calculator_window,
                                   bg="#0969da", fg="#ffffff",
                                   activebackground="#1f6feb", activeforeground="#ffffff",
                                   font=("Consolas", 11, "bold"), pady=10)
        self.calc_btn.pack(side="left", fill="x", expand=True, padx=(3, 3))

        self.stop_btn = DarkButton(bf, text="■ STOP", command=self.stop_feed,
                                   bg="#21262d", fg="#8b949e",
                                   activebackground="#da3633", activeforeground="#ffffff",
                                   disabledbackground="#21262d", disabledforeground="#8b949e",
                                   state="disabled",
                                   font=("Consolas", 11, "bold"), pady=10)
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Status bar
        sb=tk.Frame(r,bg="#161b22",highlightthickness=1,highlightbackground="#30363d")
        sb.pack(fill="x",padx=16,pady=(4,8))
        self.status_lbl=tk.Label(sb,textvariable=self.status_var,
                                  bg="#161b22",fg="#3fb950",
                                  font=("Consolas",8),anchor="w")
        self.status_lbl.pack(fill="x",padx=8,pady=4)

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
        target_sym = (default_sym or self.symbol_var.get() or "NIFTY").strip().upper()
        win = tk.Toplevel(self.root)
        win.title("🎯 Multi-Timeframe Fibonacci Levels & Strategy Calculator (Any Symbol)")
        win.geometry("1140x720")
        win.configure(bg="#0d1117")

        # Top Header Frame
        hdr = tk.Frame(win, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        hdr.pack(fill="x", padx=12, pady=(10, 6))

        tk.Label(hdr, text="⚡ MULTI-TIMEFRAME FIBONACCI PIVOT & LEVEL CALCULATOR",
                 bg="#161b22", fg="#58a6ff", font=("Consolas", 12, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        tk.Label(hdr, text="Calculate 15M Intraday & 1H Structure Levels, Strike Pivots (S3..R3), & Strategy Setups for Any Stock or Index",
                 bg="#161b22", fg="#8b949e", font=("Consolas", 8)).pack(anchor="w", padx=12, pady=(0, 6))

        # Input & Chips Bar
        in_f = tk.Frame(win, bg="#0d1117")
        in_f.pack(fill="x", padx=12, pady=4)

        tk.Label(in_f, text="Symbol:", bg="#0d1117", fg="#f0f6fc", font=("Consolas", 10, "bold")).pack(side="left", padx=(0, 6))
        sym_entry_var = tk.StringVar(value=target_sym)
        sym_ent = ttk.Combobox(in_f, textvariable=sym_entry_var, width=14, font=("Consolas", 10, "bold"))
        sym_ent.pack(side="left", padx=(0, 6))

        calc_status_var = tk.StringVar(value="Ready. Enter any symbol (e.g. RELIANCE, TCS, INFY, CRUDEOIL, NIFTY) and click Calculate.")

        # Quick popular chip buttons
        chip_frame = tk.Frame(in_f, bg="#0d1117")
        chip_frame.pack(side="left", padx=6)
        tk.Label(chip_frame, text="Quick:", bg="#0d1117", fg="#8b949e", font=("Consolas", 8)).pack(side="left", padx=(0, 4))

        def _set_and_calc(s):
            sym_entry_var.set(s)
            _run_calculation()

        for chip in ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK", "CRUDEOIL", "SENSEX"]:
            b = tk.Button(chip_frame, text=chip, bg="#21262d", fg="#79c0ff", activebackground="#30363d", activeforeground="#ffffff",
                          relief="flat", font=("Consolas", 8, "bold"), padx=5, pady=1,
                          command=lambda c=chip: _set_and_calc(c))
            b.pack(side="left", padx=2)

        # Specs / Status Card
        specs_card = tk.Frame(win, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        specs_card.pack(fill="x", padx=12, pady=4)
        specs_var = tk.StringVar(value="Contract Details: -- | Strike Step: -- | Lot Size: -- | Spot: -- | Exchange: --")
        tk.Label(specs_card, textvariable=specs_var, bg="#161b22", fg="#ffd700", font=("Consolas", 9, "bold")).pack(anchor="w", padx=10, pady=5)

        # Multi-timeframe Pivots Display Frame
        piv_card = tk.Frame(win, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        piv_card.pack(fill="x", padx=12, pady=4)
        tk.Label(piv_card, text="📊 MULTI-TIMEFRAME FIBONACCI PIVOTS (TradingView Exact Formula)",
                 bg="#161b22", fg="#58a6ff", font=("Consolas", 8, "bold")).pack(anchor="w", padx=10, pady=(4, 2))
        piv_text_var = tk.StringVar(value="Waiting for calculation...")
        piv_lbl = tk.Label(piv_card, textvariable=piv_text_var, bg="#161b22", fg="#f0f6fc",
                           font=("Consolas", 8), justify="left", anchor="w")
        piv_lbl.pack(fill="x", padx=10, pady=(0, 6))

        # Focus Strikes & Strategy Setups Treeview Frame
        res_frame = tk.Frame(win, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        res_frame.pack(fill="both", expand=True, padx=12, pady=4)

        tree_hdr = tk.Frame(res_frame, bg="#161b22")
        tree_hdr.pack(fill="x", padx=10, pady=(6, 4))
        tk.Label(tree_hdr, text="🎯 15M INTRADAY & 1H STRUCTURE FOCUS STRIKES & EXECUTION SETUPS",
                 bg="#161b22", fg="#3fb950", font=("Consolas", 9, "bold")).pack(side="left")
        tk.Label(tree_hdr, text="Logic: BUY on S2 Reclaim (Targets: S1→P→R1→R2) | SELL on R2 Rejection (Targets: R1→P→S1→S2)",
                 bg="#161b22", fg="#8b949e", font=("Consolas", 8)).pack(side="right")

        cols = ("Timeframe", "Type", "Strike", "Setup / Action", "Planned Entry", "Stop Loss", "Target 1", "Target 2 (P)", "Target 3", "Target 4", "R:R", "S3", "S2", "S1", "Pivot P", "R1", "R2", "R3")
        tree = ttk.Treeview(res_frame, columns=cols, show="headings", height=10)
        col_w = {
            "Timeframe": 105, "Type": 45, "Strike": 65, "Setup / Action": 150,
            "Planned Entry": 90, "Stop Loss": 80, "Target 1": 65, "Target 2 (P)": 80,
            "Target 3": 65, "Target 4": 65, "R:R": 55,
            "S3": 55, "S2": 55, "S1": 55, "Pivot P": 55, "R1": 55, "R2": 55, "R3": 55
        }
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=col_w.get(c, 60), anchor="center")

        t_sy = ttk.Scrollbar(res_frame, orient="vertical", command=tree.yview)
        t_sx = ttk.Scrollbar(res_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=t_sy.set, xscrollcommand=t_sx.set)
        tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        t_sy.pack(side="right", fill="y", pady=4)
        t_sx.pack(side="bottom", fill="x", padx=6)

        tree.tag_configure("buy_row", background="#062b19", foreground="#3bf586")
        tree.tag_configure("sell_row", background="#330810", foreground="#ff6b81")
        tree.tag_configure("info_row", background="#161b22", foreground="#f0f6fc")

        # Bottom Action Bar
        act_f = tk.Frame(win, bg="#0d1117")
        act_f.pack(fill="x", padx=12, pady=(6, 10))

        status_lbl = tk.Label(act_f, textvariable=calc_status_var, bg="#0d1117", fg="#58a6ff",
                              font=("Consolas", 8), anchor="w")
        status_lbl.pack(side="top", fill="x", pady=(0, 4))

        def _apply_symbol_to_dashboard():
            sym = sym_entry_var.get().strip().upper()
            if not sym: return
            exch = get_symbol_exchange(sym)
            self.exchange_var.set(exch)
            syms = list(EXCHANGE_CONFIG.get(exch, {}).get("symbols", []))
            if Session.scrip_df is not None:
                try:
                    df = Session.scrip_df
                    seg_candidates = [exch]
                    if exch == "BFO": seg_candidates = ["BFO", "BSE"]
                    inst = EXCHANGE_CONFIG.get(exch, {}).get("inst_types", ["OPTIDX", "OPTSTK", "OPTFUT"])
                    extra_syms = sorted(df[(df["exch_seg"].isin(seg_candidates)) & (df["instrumenttype"].isin(inst))]["name"].dropna().unique().tolist())
                    for s in extra_syms:
                        if s not in syms: syms.append(s)
                except Exception:
                    pass
            self.sym_cb["values"] = syms
            self.symbol_var.set(sym)
            self._load_expiries()
            exp = self.expiry_var.get()
            self._set_status(f"🚀 Loaded {sym} ({exch}) | Expiry: {exp} — Ready for Scanner", "#3fb950")
            threading.Thread(target=lambda: feed.prepare(sym, exp, self.strikes_var.get()), daemon=True).start()
            messagebox.showinfo("Symbol Loaded", f"✅ Successfully loaded {sym} ({exch}) into main dashboard!\n\nExpiry: {exp}\nStrike Step: {get_symbol_step(sym, exch)}\nLot Size: {get_symbol_lot_size(sym, exch)}")

        def _copy_plan_to_clipboard():
            sym = sym_entry_var.get().strip().upper()
            txt = f"=== FIBONACCI PIVOT TRADING PLAN: {sym} ===\n"
            txt += specs_var.get() + "\n\n"
            txt += piv_text_var.get() + "\n\n"
            txt += "FOCUS STRIKES & EXECUTION SETUPS:\n"
            for item in tree.get_children():
                vals = tree.item(item, "values")
                txt += " | ".join(str(v) for v in vals) + "\n"
            try:
                win.clipboard_clear()
                win.clipboard_append(txt)
                calc_status_var.set("✅ Plan copied to clipboard!")
            except Exception as e:
                calc_status_var.set(f"Clipboard error: {e}")

        DarkButton(act_f, text="🚀 LOAD INTO DASHBOARD & PREPARE FOR TRADE",
                   command=_apply_symbol_to_dashboard,
                   bg="#238636", fg="#ffffff", activebackground="#2ea043",
                   font=("Consolas", 10, "bold"), padx=14, pady=6).pack(side="left", padx=(0, 6))

        DarkButton(act_f, text="📋 Copy Trading Plan",
                   command=_copy_plan_to_clipboard,
                   bg="#21262d", fg="#79c0ff", activebackground="#30363d",
                   font=("Consolas", 9, "bold"), padx=12, pady=6).pack(side="left", padx=6)

        DarkButton(act_f, text="✕ Close", command=win.destroy,
                   bg="#21262d", fg="#8b949e", activebackground="#30363d",
                   font=("Consolas", 9), padx=12, pady=6).pack(side="right")

        def _run_calculation():
            sym = sym_entry_var.get().strip().upper()
            if not sym:
                calc_status_var.set("⚠️ Please enter a symbol first.")
                return

            calc_status_var.set(f"⏳ Calculating MTF pivots and levels for {sym}... Please wait...")

            def _worker():
                try:
                    exch = get_symbol_exchange(sym)
                    step = get_symbol_step(sym, exch)
                    lot = get_symbol_lot_size(sym, exch)
                    mtf_pivots = get_tv_multi_timeframe_pivots(sym)

                    spot = 0.0
                    if sym in feed.spot_price:
                        spot = feed.spot_price[sym]
                    if spot <= 0 and mtf_pivots.get("15-Min"):
                        spot = mtf_pivots["15-Min"].get("P", 0.0)
                    if spot <= 0 and mtf_pivots.get("Daily"):
                        spot = mtf_pivots["Daily"].get("P", 0.0)

                    focus = select_daily_4_focus_strikes(mtf_pivots, step)
                    c_exp = ""
                    if Session.scrip_df is not None:
                        try:
                            df = Session.scrip_df
                            raw_exp = df[(df["name"] == sym) & (df["strike"].astype(float) > 0)]["expiry"].unique()
                            if len(raw_exp) > 0:
                                c_exp = sorted(raw_exp, key=lambda e: datetime.strptime(e, "%d%b%Y") if e else datetime.max)[0]
                        except Exception:
                            pass

                    def _update_gui():
                        specs_text = f"Symbol: {sym} | Exchange: {exch} | Spot: {spot:.2f} | Strike Step: {step} | Lot Size: {lot} | Expiry: {c_exp or 'N/A'}"
                        specs_var.set(specs_text)

                        piv_lines = []
                        for tf_k, tf_name in [("Daily", "Daily"), ("Weekly", "Weekly"), ("15-Min", "15-Min (Intraday)"), ("1-Hour", "1-Hour (Structure)")]:
                            p = mtf_pivots.get(tf_k, {})
                            if p:
                                piv_lines.append(
                                    f"[{tf_name:<18}]  P: {p.get('P',0):>8.2f} | "
                                    f"S1: {p.get('S1',0):>8.2f}  S2: {p.get('S2',0):>8.2f}  S3: {p.get('S3',0):>8.2f} | "
                                    f"R1: {p.get('R1',0):>8.2f}  R2: {p.get('R2',0):>8.2f}  R3: {p.get('R3',0):>8.2f}"
                                )
                        piv_text_var.set("\n".join(piv_lines) if piv_lines else "No pivot data available for this symbol.")

                        for it in tree.get_children():
                            tree.delete(it)

                        f15 = focus.get("15M", {})
                        f1h = focus.get("1H", {})

                        for tf_label, f_dict in [("15M Intraday", f15), ("1H Structure", f1h)]:
                            piv_base = f_dict
                            p = piv_base.get("P", spot)
                            s1, s2, s3 = piv_base.get("S1", 0), piv_base.get("S2", 0), piv_base.get("S3", 0)
                            r1, r2, r3 = piv_base.get("R1", 0), piv_base.get("R2", 0), piv_base.get("R3", 0)

                            for idx, stk in enumerate(f_dict.get("ce_strikes", [])[:2]):
                                tree.insert("", "end", values=(
                                    tf_label, "CE", stk, "BUY (S2 Reclaim)",
                                    f"{s2:.1f}", f"< {s3:.1f}", f"{s1:.1f}", f"{p:.1f}", f"{r1:.1f}", f"{r2:.1f}", "1:2.8",
                                    f"{s3:.1f}", f"{s2:.1f}", f"{s1:.1f}", f"{p:.1f}", f"{r1:.1f}", f"{r2:.1f}", f"{r3:.1f}"
                                ), tags=("buy_row",))
                                tree.insert("", "end", values=(
                                    tf_label, "CE", stk, "SELL (R2 Rejection)",
                                    f"{r2:.1f}", f"> {r3:.1f}", f"{r1:.1f}", f"{p:.1f}", f"{s1:.1f}", f"{s2:.1f}", "1:2.6",
                                    f"{s3:.1f}", f"{s2:.1f}", f"{s1:.1f}", f"{p:.1f}", f"{r1:.1f}", f"{r2:.1f}", f"{r3:.1f}"
                                ), tags=("sell_row",))

                            for idx, stk in enumerate(f_dict.get("pe_strikes", [])[:2]):
                                tree.insert("", "end", values=(
                                    tf_label, "PE", stk, "BUY (S2 Reclaim)",
                                    f"{s2:.1f}", f"< {s3:.1f}", f"{s1:.1f}", f"{p:.1f}", f"{r1:.1f}", f"{r2:.1f}", "1:2.8",
                                    f"{s3:.1f}", f"{s2:.1f}", f"{s1:.1f}", f"{p:.1f}", f"{r1:.1f}", f"{r2:.1f}", f"{r3:.1f}"
                                ), tags=("buy_row",))
                                tree.insert("", "end", values=(
                                    tf_label, "PE", stk, "SELL (R2 Rejection)",
                                    f"{r2:.1f}", f"> {r3:.1f}", f"{r1:.1f}", f"{p:.1f}", f"{s1:.1f}", f"{s2:.1f}", "1:2.6",
                                    f"{s3:.1f}", f"{s2:.1f}", f"{s1:.1f}", f"{p:.1f}", f"{r1:.1f}", f"{r2:.1f}", f"{r3:.1f}"
                                ), tags=("sell_row",))

                        calc_status_var.set(f"✅ Calculation complete for {sym}! Multi-timeframe levels & setups ready.")

                    win.after(0, _update_gui)
                except Exception as ex:
                    win.after(0, lambda: calc_status_var.set(f"❌ Calculation error: {ex}"))

            threading.Thread(target=_worker, daemon=True).start()

        DarkButton(in_f, text="⚡ CALCULATE LEVELS", command=_run_calculation,
                   bg="#1f6feb", fg="#ffffff", activebackground="#388bfd",
                   font=("Consolas", 10, "bold"), padx=12, pady=4).pack(side="left", padx=6)
        sym_ent.bind("<Return>", lambda e: _run_calculation())

        if Session.scrip_df is not None:
            try:
                df = Session.scrip_df
                all_fno = sorted(df[df["instrumenttype"].isin(["OPTIDX", "OPTSTK", "OPTFUT"])]["name"].dropna().unique().tolist())
                sym_ent["values"] = all_fno
            except Exception:
                pass

        win.after(100, _run_calculation)

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

        cache_f = os.path.join(BASE_DIR, "strike_pivot_cache.json")
        cdata = {}
        if os.path.exists(cache_f):
            try:
                with open(cache_f, "r") as f:
                    cdata = json.load(f)
            except Exception:
                pass

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

        dt_str = cdata.get("date", datetime.now().strftime("%Y-%m-%d"))
        tk.Label(top_f, text=f"📐 NEXT-DAY FIBONACCI PIVOT MAP  |  Symbol: {sym}  |  Expiry: {exp}  |  Session: {dt_str}",
                 bg="#161b22", fg="#58a6ff", font=("Consolas", 11, "bold")).pack(anchor="w", padx=10, pady=(6, 2))

        # Index Pivots Reference Display
        piv15 = mtf_all.get("15-Min", {})
        piv1h = mtf_all.get("1-Hour", {})
        idx_txt = (
            f"Index 15M Pivots: P={piv15.get('P', 0):.1f} | R1={piv15.get('R1', 0):.1f} R2={piv15.get('R2', 0):.1f} R3={piv15.get('R3', 0):.1f} | "
            f"S1={piv15.get('S1', 0):.1f} S2={piv15.get('S2', 0):.1f} S3={piv15.get('S3', 0):.1f}\n"
            f"Index 1H Pivots:  P={piv1h.get('P', 0):.1f} | R1={piv1h.get('R1', 0):.1f} R2={piv1h.get('R2', 0):.1f} R3={piv1h.get('R3', 0):.1f} | "
            f"S1={piv1h.get('S1', 0):.1f} S2={piv1h.get('S2', 0):.1f} S3={piv1h.get('S3', 0):.1f}"
        )
        tk.Label(top_f, text=idx_txt, bg="#161b22", fg="#ffd700", font=("Consolas", 8), justify="left").pack(anchor="w", padx=10, pady=2)

        rule_txt = "Trading Logic:  BUY on S2 Reclaim (T1:S1 → T2:P → T3:R1 → T4:R2 | SL=LL of day)  •  SELL on R2 Rejection (T1:R1 → T2:P → T3:S1 → T4:S2 | SL=HH of day)"
        tk.Label(top_f, text=rule_txt, bg="#161b22", fg="#8b949e", font=("Consolas", 8)).pack(anchor="w", padx=10, pady=(2, 6))

        # ── Interactive Filter Toolbar (In-App modal only — No external page) ──
        ctrl_f = tk.Frame(win, bg="#0d1117")
        ctrl_f.pack(fill="x", padx=12, pady=(0, 6))

        modal_filter_var = tk.StringVar(value="15M")
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
                    if s_info.get("opt_type") == opt_type and int(s_info.get("strike", 0)) == int(strike_num):
                        return s_info
                s_info = feed.strike_pivots.get((opt_type, int(strike_num)))
                if s_info: return s_info
                return None

            rows_to_display = []

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
        self.expiry_filter_var.set("NEXT")
        self._refresh_display()

    def _refresh_display(self):
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
                f"⏱️ 15M INTRADAY: P:{p15.get('P','-')} | S1:{p15.get('S1','-')} S2:{p15.get('S2','-')} | R1:{p15.get('R1','-')} R2:{p15.get('R2','-')}  ||  "
                f"15M Strikes ➜ CE@{f15.get('CE1','-')}/{f15.get('CE2','-')}  PE@{f15.get('PE1','-')}/{f15.get('PE2','-')}"
            )
        else:
            self.sec_15m_info.set("⏱️ 15M Intraday: Loading levels...")

        if p1h:
            bias_txt = f" (Bias: {h1b.get('bias','-')})" if h1b else ""
            self.sec_1h_info.set(
                f"⏳ 1H STRUCTURE: P:{p1h.get('P','-')} | S1:{p1h.get('S1','-')} S2:{p1h.get('S2','-')} | R1:{p1h.get('R1','-')} R2:{p1h.get('R2','-')}  ||  "
                f"1H Strikes ➜ CE@{f1h.get('CE1','-')}/{f1h.get('CE2','-')}  PE@{f1h.get('PE1','-')}/{f1h.get('PE2','-')}{bias_txt}"
            )
        else:
            self.sec_1h_info.set("⏳ 1H Structure: Loading levels...")

        # Multi-Timeframe Pivot zones with levels (all timeframes)
        if mtf:
            mtf_lines = []
            for tf in ["Weekly", "Daily", "1-Hour", "15-Min", "5-Min"]:
                tp = mtf.get(tf)
                if tp:
                    zd = get_pivot_zone_detail(spot, tp) if spot > 0 else {"zone": "?"}
                    mtf_lines.append(
                        f"[{tf}] ZONE:{zd['zone']}  P:{tp['P']} S1:{tp['S1']} S2:{tp['S2']} R1:{tp['R1']} R2:{tp['R2']}"
                    )
            self.mtf_info.set("\n".join(mtf_lines) if mtf_lines else "MTF: fetching...")

        filt = self.filter_var.get() if hasattr(self, "filter_var") else "FOCUS_ALL"

        # 15M Focus: exactly 2 CE + 2 PE
        ce_15m = [s for s in f15.get("ce_strikes", [f15.get("CE1"), f15.get("CE2")]) if s and s > 0][:2]
        pe_15m = [s for s in f15.get("pe_strikes", [f15.get("PE1"), f15.get("PE2")]) if s and s > 0][:2]
        focus_15m_strikes = set(ce_15m + pe_15m)

        # 1H Focus: exactly 2 CE + 2 PE
        ce_1h = [s for s in f1h.get("ce_strikes", [f1h.get("CE1"), f1h.get("CE2")]) if s and s > 0][:2]
        pe_1h = [s for s in f1h.get("pe_strikes", [f1h.get("PE1"), f1h.get("PE2")]) if s and s > 0][:2]
        focus_1h_strikes = set(ce_1h + pe_1h)

        all_focus_strikes = focus_15m_strikes | focus_1h_strikes

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
                        self._cached_strikes = json.load(cf).get("strikes", {})
                except Exception:
                    self._cached_strikes = getattr(self, "_cached_strikes", {})
            else:
                self._cached_strikes = {}
            self._cache_by_key = {}
            self._cache_by_type_strike = {}
            for _tok, _si in self._cached_strikes.items():
                _exp = _si.get("expiry", "-")
                self._cache_by_key[(_si.get("opt_type"), _si.get("strike"), _exp)] = _si
                self._cache_by_type_strike.setdefault((_si.get("opt_type"), _si.get("strike")), _si)

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

        needed_focus = []
        for s in ce_15m: needed_focus.append(("CE", s, True, s in ce_1h))
        for s in pe_15m: needed_focus.append(("PE", s, True, s in pe_1h))
        for s in ce_1h:
            if s not in ce_15m: needed_focus.append(("CE", s, False, True))
        for s in pe_1h:
            if s not in pe_15m: needed_focus.append(("PE", s, False, True))

        # ── Ensure all cached strikes (for both current and next expiry) are present in watch ──
        existing_full_keys = {(w.get("type"), w.get("strike"), w.get("expiry", "-"), w.get("side", "BUY")) for w in watch}
        for tok, s_info in cached_strikes.items():
            stk = s_info.get("strike", 0)
            ot = s_info.get("opt_type", "CE")
            exp_val = s_info.get("expiry", "-")
            if (ot, stk, exp_val, "BUY") not in existing_full_keys:
                ltp_val = _live_ltp(ot, stk, exp_val)
                s3_val = s_info.get("S3", 0.0)
                s2_val = s_info.get("S2", 0.0)
                s1_val = s_info.get("S1", 0.0)
                p_val  = s_info.get("P", 0.0)
                r1_val = s_info.get("R1", 0.0)
                r2_val = s_info.get("R2", 0.0)
                r3_val = s_info.get("R3", 0.0)

                is_15m = (ot == "CE" and stk in ce_15m) or (ot == "PE" and stk in pe_15m)
                is_1h  = (ot == "CE" and stk in ce_1h)  or (ot == "PE" and stk in pe_1h)
                strike_tag = "[⭐15M & ⏳1H]" if (is_15m and is_1h) else ("[⭐15M Focus]" if is_15m else ("[⏳1H Focus]" if is_1h else ""))

                entry_lvl_b = "S2" if s2_val > 1.0 else "S1"
                entry_val_b = s2_val if s2_val > 1.0 else s1_val
                tgts_b = [s1_val, p_val, r1_val, r2_val, r3_val] if entry_lvl_b == "S2" else [p_val, r1_val, r2_val, r3_val, r3_val * 1.05]
                sig_b = f"★ BUY ACTIVE [Entry@{entry_lvl_b}:{entry_val_b:.1f} | LL SL]"
                zone_str_b = f"{strike_tag} Buy Zone Above {entry_lvl_b} ({entry_val_b:.1f})".strip()

                entry_lvl_s = "R2" if r2_val > 1.0 else "R1"
                entry_val_s = r2_val if r2_val > 1.0 else r1_val
                tgts_s = [r1_val, p_val, s1_val, s2_val, s3_val] if entry_lvl_s == "R2" else [p_val, s1_val, s2_val, s3_val, s3_val * 0.95]
                sig_s = f"▼ SELL ACTIVE [Entry@{entry_lvl_s}:{entry_val_s:.1f} | HH SL]"
                zone_str_s = f"{strike_tag} Sell Area Below {entry_lvl_s} ({entry_val_s:.1f})".strip()

                watch.append({
                    "side": "BUY", "type": ot, "strike": stk, "ltp": ltp_val,
                    "high": s_info.get("prev_h", ltp_val), "low": s_info.get("prev_l", ltp_val),
                    "oi": 0, "vol": 0, "zone": zone_str_b, "expiry": exp_val,
                    "is_15m": is_15m, "is_1h": is_1h,
                    "s3": s3_val, "s2": s2_val, "s1": s1_val, "p": p_val, "r1": r1_val, "r2": r2_val, "r3": r3_val,
                    "category": "CE_SUP" if ot == "CE" else "PE_SUP",
                    "status": sig_b, "m5_state": "WAITING", "trade_status": "Waiting",
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
                    "oi": 0, "vol": 0, "zone": zone_str_s, "expiry": exp_val,
                    "is_15m": is_15m, "is_1h": is_1h,
                    "s3": s3_val, "s2": s2_val, "s1": s1_val, "p": p_val, "r1": r1_val, "r2": r2_val, "r3": r3_val,
                    "category": "PE_RES" if ot == "PE" else "CE_RES",
                    "status": sig_s, "m5_state": "WAITING", "trade_status": "Waiting",
                    "ll": s_info.get("prev_l", 0.0), "hh": s_info.get("prev_h", 0.0), "sl": 0.0,
                    "entry_price": entry_val_s, "targets": tgts_s, "target": tgts_s[0] if tgts_s else 0.0,
                    "t1": tgts_s[0] if len(tgts_s) > 0 else 0.0, "t2": tgts_s[1] if len(tgts_s) > 1 else 0.0,
                    "t3": tgts_s[2] if len(tgts_s) > 2 else 0.0, "t4": tgts_s[3] if len(tgts_s) > 3 else 0.0,
                    "t5": tgts_s[4] if len(tgts_s) > 4 else 0.0,
                    "plan_s2": s2_val, "plan_s1": s1_val, "plan_p": p_val, "plan_r1": r1_val, "plan_r2": r2_val,
                    "plan_buy_zone": f"BUY {entry_lvl_b}={entry_val_b:.1f} | T1:{tgts_b[0]:.1f} P:{p_val:.1f}",
                    "plan_sell_zone": f"SELL R2={r2_val:.1f} | T1:{r1_val:.1f} P:{p_val:.1f}",
                })
                existing_full_keys.add((ot, stk, exp_val, "BUY"))
                existing_full_keys.add((ot, stk, exp_val, "SELL"))
                existing_keys.add((ot, stk, exp_val))
        else:
            # If watch is populated from feed, check if any focus strikes are missing
            for ot, stk, is_15m, is_1h in needed_focus:
                strike_tag = "[⭐15M & ⏳1H]" if (is_15m and is_1h) else ("[⭐15M Focus]" if is_15m else "[⏳1H Focus]")
                if not any(k[0] == ot and k[1] == stk for k in existing_keys):
                    s_info = feed.strike_pivots.get((ot, stk))
                    if not s_info:
                        for _tok, _si in cached_strikes.items():
                            if _si.get("opt_type") == ot and _si.get("strike") == stk:
                                s_info = _si
                                break
                    if not s_info:
                        ltp_syn = _live_ltp(ot, stk)
                        if ltp_syn > 0:
                            s_info = calc_fib_pivots(ltp_syn * 1.05, ltp_syn * 0.95, ltp_syn)
                            s_info["strike"]   = stk
                            s_info["opt_type"] = ot
                            s_info["prev_c"]   = ltp_syn
                            s_info["prev_h"]   = round(ltp_syn * 1.05, 2)
                            s_info["prev_l"]   = round(ltp_syn * 0.95, 2)
                            feed.strike_pivots[(ot, stk)] = s_info
                    if s_info:
                        ltp_val = _live_ltp(ot, stk)
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
                        sig_b = f"★ BUY ACTIVE [Entry@{entry_lvl_b}:{entry_val_b:.1f} | LL SL]"
                        zone_str_b = f"{strike_tag} Buy Zone Above {entry_lvl_b} ({entry_val_b:.1f})".strip()

                        entry_lvl_s = "R2" if r2_val > 1.0 else "R1"
                        entry_val_s = r2_val if r2_val > 1.0 else r1_val
                        tgts_s = [r1_val, p_val, s1_val, s2_val, s3_val] if entry_lvl_s == "R2" else [p_val, s1_val, s2_val, s3_val, s3_val * 0.95]
                        sig_s = f"▼ SELL ACTIVE [Entry@{entry_lvl_s}:{entry_val_s:.1f} | HH SL]"
                        zone_str_s = f"{strike_tag} Sell Area Below {entry_lvl_s} ({entry_val_s:.1f})".strip()

                        watch.append({
                            "side": "BUY", "type": ot, "strike": stk, "ltp": ltp_val,
                            "high": s_info.get("prev_h", ltp_val), "low": s_info.get("prev_l", ltp_val),
                            "oi": 0, "vol": 0, "zone": zone_str_b, "expiry": s_info.get("expiry", "-"),
                            "is_15m": is_15m, "is_1h": is_1h,
                            "s3": s3_val, "s2": s2_val, "s1": s1_val, "p": p_val, "r1": r1_val, "r2": r2_val, "r3": r3_val,
                            "category": "CE_SUP" if ot == "CE" else "PE_SUP",
                            "status": sig_b, "m5_state": "WAITING", "trade_status": "Waiting",
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
                            "oi": 0, "vol": 0, "zone": zone_str_s, "expiry": s_info.get("expiry", "-"),
                            "is_15m": is_15m, "is_1h": is_1h,
                            "s3": s3_val, "s2": s2_val, "s1": s1_val, "p": p_val, "r1": r1_val, "r2": r2_val, "r3": r3_val,
                            "category": "PE_RES" if ot == "PE" else "CE_RES",
                            "status": sig_s, "m5_state": "WAITING", "trade_status": "Waiting",
                            "ll": s_info.get("prev_l", 0.0), "hh": s_info.get("prev_h", 0.0), "sl": 0.0,
                            "entry_price": entry_val_s, "targets": tgts_s, "target": tgts_s[0] if tgts_s else 0.0,
                            "t1": tgts_s[0] if len(tgts_s) > 0 else 0.0, "t2": tgts_s[1] if len(tgts_s) > 1 else 0.0,
                            "t3": tgts_s[2] if len(tgts_s) > 2 else 0.0, "t4": tgts_s[3] if len(tgts_s) > 3 else 0.0,
                            "t5": tgts_s[4] if len(tgts_s) > 4 else 0.0,
                            "plan_s2": s2_val, "plan_s1": s1_val, "plan_p": p_val, "plan_r1": r1_val, "plan_r2": r2_val,
                            "plan_buy_zone": f"BUY {entry_lvl_b}={entry_val_b:.1f} | T1:{tgts_b[0]:.1f} P:{p_val:.1f}",
                            "plan_sell_zone": f"SELL R2={r2_val:.1f} | T1:{r1_val:.1f} P:{p_val:.1f}",
                        })
        seen = set()
        if filt == "BUY_ONLY":
            display_watch = []
            for w in watch:
                if w.get("side", "BUY") != "BUY": continue
                key = (w.get("type", "CE"), w.get("strike", 0), w.get("expiry", "-"), "BUY")
                if key not in seen:
                    seen.add(key)
                    display_watch.append(w)
        elif filt == "SELL_ONLY":
            display_watch = []
            for w in watch:
                if w.get("side", "BUY") != "SELL": continue
                key = (w.get("type", "CE"), w.get("strike", 0), w.get("expiry", "-"), "SELL")
                if key not in seen:
                    seen.add(key)
                    display_watch.append(w)
        elif filt == "FOCUS_15M":
            display_watch = []
            for w in watch:
                ot = w.get("type", "CE")
                stk = w.get("strike", 0)
                exp = w.get("expiry", "-")
                side = w.get("side", "BUY")
                key = (ot, stk, exp, side)
                if key in seen:
                    continue
                if (ot == "CE" and stk in ce_15m) or (ot == "PE" and stk in pe_15m):
                    seen.add(key)
                    display_watch.append(w)
        elif filt == "FOCUS_1H":
            display_watch = []
            for w in watch:
                ot = w.get("type", "CE")
                stk = w.get("strike", 0)
                exp = w.get("expiry", "-")
                side = w.get("side", "BUY")
                key = (ot, stk, exp, side)
                if key in seen:
                    continue
                if (ot == "CE" and stk in ce_1h) or (ot == "PE" and stk in pe_1h):
                    seen.add(key)
                    display_watch.append(w)
        elif filt in ("FOCUS", "FOCUS_ALL"):
            display_watch = []
            for w in watch:
                ot = w.get("type", "CE")
                stk = w.get("strike", 0)
                exp = w.get("expiry", "-")
                side = w.get("side", "BUY")
                key = (ot, stk, exp, side)
                if key in seen:
                    continue
                if (ot == "CE" and (stk in ce_15m or stk in ce_1h)) or (ot == "PE" and (stk in pe_15m or stk in pe_1h)):
                    seen.add(key)
                    display_watch.append(w)
        elif filt == "CE_SUP":
            display_watch = []
            for w in watch:
                if w.get("type") == "CE":
                    key = ("CE", w.get("strike", 0), w.get("expiry", "-"), w.get("side", "BUY"))
                    if key not in seen:
                        seen.add(key)
                        display_watch.append(w)
        elif filt == "PE_RES":
            display_watch = []
            for w in watch:
                if w.get("type") == "PE":
                    key = ("PE", w.get("strike", 0), w.get("expiry", "-"), w.get("side", "BUY"))
                    if key not in seen:
                        seen.add(key)
                        display_watch.append(w)
        elif filt == "TODAY_LEVELS":
            display_watch = []
            for w in watch:
                ot = w.get("type", "CE")
                stk = w.get("strike", 0)
                exp = w.get("expiry", "-")
                side = w.get("side", "BUY")
                key = (ot, stk, exp, side)
                if key in seen:
                    continue
                if (ot == "CE" and stk in ce_15m) or (ot == "PE" and stk in pe_15m) or w.get("plan_s2", 0) > 0:
                    seen.add(key)
                    display_watch.append(w)
        else:
            display_watch = []
            for w in watch:
                key = (w.get("type", "CE"), w.get("strike", 0), w.get("expiry", "-"), w.get("side", "BUY"))
                if key not in seen:
                    seen.add(key)
                    display_watch.append(w)

        # ── Apply Expiry Filter (ALL / CURRENT / NEXT) ──
        curr_exp = (self.expiry_var.get() or "").strip()
        next_exp = (self.next_exp_var.get() or "").strip()
        exp_filt = self.expiry_filter_var.get() if hasattr(self, "expiry_filter_var") else "ALL"

        if exp_filt == "NEXT":
            if next_exp:
                exp_matched = [w for w in display_watch if w.get("expiry", "").strip() == next_exp]
                if not exp_matched:
                    # Fallback to all strikes for next_exp from watch
                    exp_matched = [w for w in watch if w.get("expiry", "").strip() == next_exp]
                display_watch = exp_matched
            else:
                display_watch = [w for w in display_watch if w.get("expiry", "").strip() != curr_exp]
        elif exp_filt == "CURRENT":
            if curr_exp:
                display_watch = [w for w in display_watch if w.get("expiry", "").strip() == curr_exp]

        # Sort so focus strikes appear first, grouped by strike, with BUY row before SELL row
        display_watch = sorted(display_watch, key=lambda w: (
            0 if ((w.get("type") == "CE" and w.get("strike") in ce_15m) or (w.get("type") == "PE" and w.get("strike") in pe_15m)) else 1,
            0 if ((w.get("type") == "CE" and w.get("strike") in ce_1h) or (w.get("type") == "PE" and w.get("strike") in pe_1h)) else 1,
            w.get("strike", 0),
            0 if w.get("type") == "CE" else 1,
            0 if w.get("side", "BUY") == "BUY" else 1
        ))

        existing_items = self.tree.get_children()
        row_idx = 0

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
                if trade_st in ("Active", "Target 1 Hit", "Target 2 Hit") or "RECLAIM" in m5_t or "CONFIRMED_BUY" in m5_t:
                    eff_sl = ll_v if (0 < ll_v < planned_ep) else (sl_v if (0 < sl_v < planned_ep) else round(planned_ep * 0.90, 2))
                    sl_str = f"{eff_sl:.2f}"
                else:
                    sl_str = "-"
            else:
                planned_ep = round(w.get("r2", 0) if w.get("r2", 0) > 1.0 else w.get("r1", 0), 2)
                lvl_label = "R2"
                s2_note = ""
                is_s1_buy = False
                if trade_st in ("Active", "Target 1 Hit", "Target 2 Hit") or "REJECT" in m5_t or "CONFIRMED_SELL" in m5_t:
                    eff_sl = hh_v if hh_v > planned_ep else (sl_v if sl_v > planned_ep else round(planned_ep * 1.10, 2))
                    sl_str = f"{eff_sl:.2f}"
                else:
                    sl_str = "-"

            # T1 / T2 / T3 from targets list or planned levels
            tgts = w.get("targets", [])
            if len(tgts) > 0 and tgts[0] > 0:
                t1_v = tgts[0]
                t2_v = tgts[1] if len(tgts) > 1 else 0
                t3_v = tgts[2] if len(tgts) > 2 else 0
            elif is_buy and is_s1_buy:
                t1_v = w.get("p", 0)
                t2_v = w.get("r1", 0)
                t3_v = w.get("r2", 0)
            elif is_buy:
                t1_v = w.get("s1", 0)
                t2_v = w.get("p", 0)
                t3_v = w.get("r1", 0)
            else:
                t1_v = w.get("r1", 0)
                t2_v = w.get("p", 0)
                t3_v = w.get("s1", 0)

            t1_str = f"{t1_v:.2f}" if t1_v and t1_v > 0 else "-"
            t2_str = f"{t2_v:.2f}" if t2_v and t2_v > 0 else "-"
            t3_str = f"{t3_v:.2f}" if t3_v and t3_v > 0 else "-"

            is_15m = w.get("is_15m", False) or (opt_t == "CE" and w.get("strike") in ce_15m) or (opt_t == "PE" and w.get("strike") in pe_15m)
            is_1h  = w.get("is_1h", False)  or (opt_t == "CE" and w.get("strike") in ce_1h)  or (opt_t == "PE" and w.get("strike") in pe_1h)
            strike_tag = "[⭐15M & ⏳1H]" if (is_15m and is_1h) else ("[⭐15M Focus]" if is_15m else ("[⏳1H Focus]" if is_1h else ""))

            # Fast-decision signals & statuses for rapid execution
            if trade_st == "Active":
                disp_signal = f"🟢 LIVE LONG @ {ep_v:.2f} | SL: {sl_str} | T1: {t1_str}" if is_buy else f"🔴 LIVE SHORT @ {ep_v:.2f} | SL: {sl_str} | T1: {t1_str}"
            elif "Target 1" in trade_st:
                disp_signal = f"🎯 T1 HIT (+50% Booked) | Trail SL: {sl_str} -> T2: {t2_str}"
            elif "Target 2" in trade_st:
                disp_signal = f"🎯 T2 HIT (+80% Booked) | Runner SL: {sl_str} -> T3: {t3_str}"
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
                row_tag = "ready_buy" if is_buy else "ready_sell"
            else:
                row_tag = "buy_row" if is_buy else "sell_row"

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
                w["strike"],
                side_badge,
                f"{w['ltp']:.2f}",
                oi_disp,
                vol_disp,
                disp_zone,
                f"{w.get('p', 0.0):.2f}",
                f"{w.get('s1', 0.0):.2f}",
                f"{w.get('s2', 0.0):.2f}",
                f"{w.get('r1', 0.0):.2f}",
                f"{w.get('r2', 0.0):.2f}",
                disp_m5,
                ll_str,
                hh_str,
                disp_signal,
                sl_str,
                entry_disp,
                t1_str,
                t2_str,
                t3_str,
                disp_trade_st,
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
        if filt == "TODAY_LEVELS":
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

        # ── EOD auto-planner: after 15:40 launch eod_planner.py once per day ──
        _now_eod = datetime.now()
        _eod_h, _eod_m = _now_eod.hour, _now_eod.minute
        _eod_date = _now_eod.strftime("%Y-%m-%d")
        _after_close_ui = (_eod_h == 15 and _eod_m >= 40) or _eod_h >= 16
        if _after_close_ui and getattr(self, '_ui_eod_planner_done', None) != _eod_date:
            self._ui_eod_planner_done = _eod_date
            try:
                import subprocess as _sp2, os as _os2
                _planner2 = _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)), 'eod_planner.py')
                _sp2.Popen(['python3', _planner2],
                           stdout=open('/tmp/eod_planner_output.log', 'w'),
                           stderr=_sp2.STDOUT)
                print(f"\n[EOD UI] ⏰ {_eod_h}:{_eod_m:02d} → eod_planner.py launched in background")
                print(f"[EOD UI] Output saved to /tmp/eod_planner_output.log\n")
            except Exception as _ep2_err:
                print(f"[EOD UI] planner auto-run error: {_ep2_err}")

        # Reschedule next refresh every 300ms continuously for smooth real-time UI
        self.root.after(300, self._refresh_display)

    def _set_status(self,msg,color="#3fb950"):
        self.status_var.set(msg)
        self.status_lbl.config(fg=color)

    def _update_order_status(self, msg: str):
        """Update the order execution status label in the GUI."""
        try:
            mode = live_order_manager.mode
            color = "#f85149" if mode == "LIVE" else "#3fb950"
            full_msg = f"{'🔴 LIVE' if mode == 'LIVE' else '📋 PAPER'} | {msg}"
            self.order_status_var.set(full_msg)
            self.order_mode_lbl.config(fg=color)
        except Exception:
            pass


    def start_feed(self):
        exch   = self.exchange_var.get()
        symbol = self.symbol_var.get()
        expiry = self.expiry_var.get()
        n      = self.strikes_var.get()

        if not expiry:
            messagebox.showerror("Error", "Please select an expiry"); return

        # Build list of expiries to subscribe
        expiries_to_load = [expiry]
        next_exp = (self.next_exp_var.get() or "").strip()
        if next_exp and next_exp != expiry and (self.use_next_exp.get() or self.expiry_filter_var.get() == "NEXT"):
            expiries_to_load.append(next_exp)

        self._set_status("Connecting & setting up Google Sheet...")
        self.start_btn.config(state="disabled", bg="#21262d", fg="#8b949e")
        feed.running = True
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
                self.root.after(0, lambda: self.stop_btn.config(state="normal", bg="#da3633", fg="#ffffff", activebackground="#f85149"))
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
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.start_btn.config(state="normal", bg="#238636", fg="#ffffff", activebackground="#2ea043"))
                self.root.after(0, lambda: self._set_status(f"Error: {e}", "#f85149"))

        threading.Thread(target=_run, daemon=True).start()

    def stop_feed(self):
        feed.running=False
        feed.stop_ws()
        self.start_btn.config(state="normal", bg="#238636", fg="#ffffff", activebackground="#2ea043")
        self.stop_btn.config(state="disabled", bg="#21262d", fg="#8b949e")
        self._set_status("Stopped — data remains in Google Sheet","#8b949e")

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
if __name__=="__main__":
    print("Logging in to Angel One...")
    Session.login()
    print("Loading ScripMaster (~200k instruments)...")
    Session.load_scrip()

    df=Session.scrip_df
    print(f"   Unique exch_seg values: {df['exch_seg'].unique().tolist()}")

    print("Ready — Fibonacci Pivot Option Chain & Reversal Scanner enabled\n")
    root=tk.Tk()
    app=ControlPanel(root)
    root.mainloop()

