"""
Angel One - Live Option Chain with Greeks
Control Panel + Google Sheets Feed
Fixes: expiry sort, max OI highlight, BSE/MCX exchange types
"""

import os, time, pyotp, requests, math, threading, pickle, json, csv
import pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox
from dotenv import load_dotenv
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from datetime import datetime
import gspread
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

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
        "inst_types":    ["OPTIDX"],
    },
    "BFO": {
        "symbols":       ["SENSEX", "BANKEX"],
        "exchange_type":  7,
        "spot_exchange":  3,
        "steps":         {"SENSEX":100,"BANKEX":100},
        "spot_tokens":   {"SENSEX":"1","BANKEX":"500112"},
        "inst_types":    ["OPTIDX"],
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
    s1 = p - 0.382 * rng
    r2 = p + 0.618 * rng
    s2 = p - 0.618 * rng
    r3 = p + rng
    s3 = p - rng

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
    Tracks state machine and liquidity sweeps for an option strike based on PDF strategy:
      - 5M candle OHLC tracking
      - BUY: Sweep below S2 -> Liquidity Low (LL) -> Reclaim support -> 5M candle close -> BUY ENTRY
      - SELL: Sweep above R2 -> Liquidity High (HH) -> Reject resistance -> 5M candle close -> SELL ENTRY
      - SL: LL (BUY) or HH (SELL), Targets: Next Pivot Levels
    """
    def __init__(self, opt_type: str, strike: int):
        self.opt_type      = opt_type
        self.strike        = strike
        self.state         = "WAITING"        # WAITING, SWEEP_S1, RECLAIM_S1, SWEEP_S2, RECLAIM_S2, CONFIRMED_BUY, SWEEP_R1, RECLAIM_R1, SWEEP_R2, RECLAIM_R2, CONFIRMED_SELL
        self.trigger_level = "S2"             # "S1", "S2", "R1", "R2"
        self.trade_status  = "Waiting"        # Waiting, Active, Target 1 Hit, Target 2 Hit, SL Hit
        self.ll            = 0.0              # Liquidity Low formed during sweep below S1/S2
        self.hh            = 0.0              # Liquidity High formed during sweep above R1/R2
        self.entry_price   = 0.0
        self.entry_time    = ""
        self.sl            = 0.0
        self.targets       = []
        self.signal        = "WAITING"
        self.last_ltp      = 0.0
        self.candle_5m     = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "bucket": -1}

    def update_tick(self, ltp: float, pivots: dict, now: datetime):
        if not pivots or ltp <= 0:
            return

        self.last_ltp = ltp

        # ── 5M candle builder from live ticks ──
        bucket = (now.minute // 5) * 5
        c = self.candle_5m
        if c["bucket"] != bucket:
            # New 5-minute bar — previous candle is now closed; use that close to confirm
            self._prev_candle = dict(c)   # snapshot of just-closed candle
            c["open"]   = ltp
            c["high"]   = ltp
            c["low"]    = ltp
            c["close"]  = ltp
            c["bucket"] = bucket
        else:
            c["high"]  = max(c["high"], ltp)
            c["low"]   = min(c["low"], ltp)
            c["close"] = ltp

        # Use the most-recently *closed* candle for confirmation (more reliable than live close)
        closed = getattr(self, "_prev_candle", None) or c

        s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]
        r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
        p          = pivots["P"]

        # ── Reset after SL so a new sweep can retrigger same day ──
        if self.trade_status in ("SL Hit", "Target 2 Hit"):
            self.state        = "WAITING"
            self.trade_status = "Waiting"
            self.ll = self.hh = self.sl = self.entry_price = 0.0
            self.targets = []
            self.signal  = "WAITING"

        # ═══════════════════════════════════════════════════════
        # 1. BUY SETUP — S1 / S2 Reclaim with Liquidity Sweep
        #    - S2 Reclaim: price drops below S2 -> records LL -> reclaims S2 -> bullish 5M close
        #    - S1 Reclaim: price drops below S1 (above S2 without touch) -> records LL -> reclaims S1 -> bullish 5M close
        # ═══════════════════════════════════════════════════════
        if self.trade_status == "Waiting" and not self.state.endswith("SELL") and "R1" not in self.state and "R2" not in self.state:

            # S2 takes priority (deep sweep)
            if ltp < s2:
                self.state         = "SWEEP_S2"
                self.trigger_level = "S2"
                self.ll            = ltp if self.ll == 0.0 else min(self.ll, ltp)

            elif self.state == "SWEEP_S2" and ltp >= s2:
                self.state = "RECLAIM_S2"

            elif self.state == "RECLAIM_S2":
                confirmed = (closed["close"] > s2 and closed["close"] >= closed["open"])
                if confirmed:
                    self.state        = "CONFIRMED_BUY"
                    self.trigger_level = "S2"
                    effective_sl      = round(self.ll if self.ll > 0 else (s3 if s3 < s2 else s2 * 0.98), 2)
                    self.entry_price  = round(ltp, 2)
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    if ltp < s1:
                        self.targets = [s1, p, r1, r2]
                    elif ltp < p:
                        self.targets = [p, r1, r2, r3]
                    else:
                        self.targets = [r1, r2, r3]
                    mkt_ctx = "Mkt Bullish ↑" if self.opt_type == "CE" else "⚡ Mkt Reversal ↑"
                    self.signal       = f"★ BUY {self.opt_type} [{mkt_ctx} | S2 Reclaim | SL:{effective_sl:.1f}]"
                    self.trade_status = "Active"

            # S1 Sweep & Reclaim (price dips below S1, but stays above S2 without touching S2)
            elif ltp < s1 and ltp >= s2:
                if self.state not in ("SWEEP_S2", "RECLAIM_S2", "CONFIRMED_BUY"):
                    self.state         = "SWEEP_S1"
                    self.trigger_level = "S1"
                    self.ll            = ltp if self.ll == 0.0 else min(self.ll, ltp)

            elif self.state == "SWEEP_S1" and ltp >= s1:
                self.state = "RECLAIM_S1"

            elif self.state == "RECLAIM_S1":
                confirmed = (closed["close"] > s1 and closed["close"] >= closed["open"])
                if confirmed:
                    self.state        = "CONFIRMED_BUY"
                    self.trigger_level = "S1"
                    effective_sl      = round(self.ll if self.ll > 0 else (s2 if s2 < s1 else s1 * 0.98), 2)
                    self.entry_price  = round(ltp, 2)
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    if ltp < p:
                        self.targets = [p, r1, r2, r3]
                    else:
                        self.targets = [r1, r2, r3]
                    mkt_ctx = "Mkt Bullish ↑" if self.opt_type == "CE" else "⚡ Mkt Reversal ↑"
                    self.signal       = f"★ BUY {self.opt_type} [{mkt_ctx} | S1 Reclaim | SL:{effective_sl:.1f}]"
                    self.trade_status = "Active"

        # ═══════════════════════════════════════════════════════
        # 2. SELL SETUP — R1 / R2 Rejection with Liquidity Sweep
        #    - R2 Rejection: price pushes above R2 -> records HH -> falls below R2 -> bearish 5M close
        #    - R1 Rejection: price pushes above R1 (below R2 without touch) -> records HH -> falls below R1 -> bearish 5M close
        # ═══════════════════════════════════════════════════════
        if self.trade_status == "Waiting" and not self.state.endswith("BUY") and "S1" not in self.state and "S2" not in self.state:

            # R2 takes priority (high sweep)
            if ltp > r2:
                self.state         = "SWEEP_R2"
                self.trigger_level = "R2"
                self.hh            = ltp if self.hh == 0.0 else max(self.hh, ltp)

            elif self.state == "SWEEP_R2" and ltp <= r2:
                self.state = "RECLAIM_R2"

            elif self.state == "RECLAIM_R2":
                confirmed = (closed["close"] < r2 and closed["close"] <= closed["open"])
                if confirmed:
                    self.state        = "CONFIRMED_SELL"
                    self.trigger_level = "R2"
                    effective_sl      = round(self.hh if self.hh > 0 else (r3 if r3 > r2 else r2 * 1.02), 2)
                    self.entry_price  = round(ltp, 2)
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    if ltp > r1:
                        self.targets = [r1, p, s1, s2]
                    elif ltp > p:
                        self.targets = [p, s1, s2, s3]
                    else:
                        self.targets = [s1, s2, s3]
                    mkt_ctx = "Mkt Bearish ↓" if self.opt_type == "PE" else "⚡ Mkt Reversal ↓"
                    self.signal       = f"▼ SELL {self.opt_type} [{mkt_ctx} | R2 Reject | SL:{effective_sl:.1f}]"
                    self.trade_status = "Active"

            # R1 Sweep & Rejection (price pushes above R1, but stays below R2 without touching R2)
            elif ltp > r1 and ltp <= r2:
                if self.state not in ("SWEEP_R2", "RECLAIM_R2", "CONFIRMED_SELL"):
                    self.state         = "SWEEP_R1"
                    self.trigger_level = "R1"
                    self.hh            = ltp if self.hh == 0.0 else max(self.hh, ltp)

            elif self.state == "SWEEP_R1" and ltp <= r1:
                self.state = "RECLAIM_R1"

            elif self.state == "RECLAIM_R1":
                confirmed = (closed["close"] < r1 and closed["close"] <= closed["open"])
                if confirmed:
                    self.state        = "CONFIRMED_SELL"
                    self.trigger_level = "R1"
                    effective_sl      = round(self.hh if self.hh > 0 else (r2 if r2 > r1 else r1 * 1.02), 2)
                    self.entry_price  = round(ltp, 2)
                    self.entry_time   = now.strftime("%H:%M:%S")
                    self.sl           = effective_sl
                    if ltp > p:
                        self.targets = [p, s1, s2, s3]
                    else:
                        self.targets = [s1, s2, s3]
                    mkt_ctx = "Mkt Bearish ↓" if self.opt_type == "PE" else "⚡ Mkt Reversal ↓"
                    self.signal       = f"▼ SELL {self.opt_type} [{mkt_ctx} | R1 Reject | SL:{effective_sl:.1f}]"
                    self.trade_status = "Active"

        # ═══════════════════════════════════════════════════════
        # 3. ACTIVE TRADE MONITORING — SL & Target tracking
        # ═══════════════════════════════════════════════════════
        if self.trade_status == "Active":
            if "BUY" in self.signal:
                if ltp <= self.sl:
                    self.trade_status = "SL Hit"
                elif self.targets and ltp >= self.targets[0]:
                    self.trade_status = "Target 1 Hit"
                    if len(self.targets) > 1 and ltp >= self.targets[1]:
                        self.trade_status = "Target 2 Hit"
            elif "SELL" in self.signal:
                if ltp >= self.sl:
                    self.trade_status = "SL Hit"
                elif self.targets and ltp <= self.targets[0]:
                    self.trade_status = "Target 1 Hit"
                    if len(self.targets) > 1 and ltp <= self.targets[1]:
                        self.trade_status = "Target 2 Hit"


def select_focus_strikes_from_pivots(pivots: dict, step: int = 50, tf_label: str = "15-Min") -> dict:
    """
    Selects 4 focus strikes from given Fibonacci Pivots (PDF Section 3 & 4):
      - 2 CE strikes: nearest strikes to R1 and R2 (Resistance strikes for Call)
      - 2 PE strikes: nearest strikes to S1 and S2 (Support strikes for Put)
    """
    if not pivots:
        return {}
    r1 = pivots.get("R1", 0)
    r2 = pivots.get("R2", 0)
    s1 = pivots.get("S1", 0)
    s2 = pivots.get("S2", 0)
    p  = pivots.get("P", 0)

    def snap(val): return int(round(val / step) * step) if val else 0

    ce1, ce2 = snap(r1), snap(r2)
    pe1, pe2 = snap(s1), snap(s2)

    if ce2 == ce1 and ce1 > 0: ce2 = ce1 + step
    if pe2 == pe1 and pe1 > 0: pe2 = max(pe1 - step, step)

    return {
        "timeframe": tf_label,
        "CE1": ce1, "CE2": ce2,
        "PE1": pe1, "PE2": pe2,
        "all_4": sorted(list({ce1, ce2, pe1, pe2})),
        "P": p, "R1": r1, "R2": r2, "S1": s1, "S2": s2
    }


def select_intraday_15m_focus_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Selects INTRADAY focus strikes based on 15-Minute Fibonacci Pivots:
      - 2 CE strikes: nearest strikes to 15M R1 and 15M R2
      - 2 PE strikes: nearest strikes to 15M S1 and 15M S2
    """
    piv = mtf_pivots.get("15-Min") or mtf_pivots.get("1-Hour") or mtf_pivots.get("Daily", {})
    return select_focus_strikes_from_pivots(piv, step, tf_label="15-Min")


def select_1h_structure_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Selects 1-HOUR STRUCTURE focus strikes based on 1-Hour Fibonacci Pivots:
      - 2 CE strikes: nearest strikes to 1H R1 and 1H R2
      - 2 PE strikes: nearest strikes to 1H S1 and 1H S2
    """
    piv = mtf_pivots.get("1-Hour") or mtf_pivots.get("Daily", {})
    return select_focus_strikes_from_pivots(piv, step, tf_label="1-Hour")


def select_daily_4_focus_strikes(mtf_pivots: dict, step: int = 50) -> dict:
    """
    Intraday strike selection using 15-Minute timeframe as primary focus for intraday trades,
    while also computing 1-Hour structure strikes for overall market context.
    """
    f15 = select_intraday_15m_focus_strikes(mtf_pivots, step)
    f1h = select_1h_structure_strikes(mtf_pivots, step)

    res = dict(f15)
    res["15M"] = f15
    res["1H"]  = f1h
    res["all_15m"] = f15.get("all_4", [])
    res["all_1h"]  = f1h.get("all_4", [])
    res["all_focus"] = sorted(list(set(f15.get("all_4", []) + f1h.get("all_4", []))))
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
    Triggers an instant macOS desktop banner notification and plays an audible system sound.
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
                f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'
            ], capture_output=True)
            if sound:
                subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], capture_output=True)
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


class ForwardTestTracker:
    """
    Logs every forward test trade entry & exit to forward_test_trades.csv.
    Tracks live PnL, Target hits, and Stop Loss hits.
    """
    def __init__(self, filepath=FORWARD_TEST_FILE):
        self.filepath = filepath
        self.active_trades = {} # (opt_type, strike) -> trade dict
        self._init_file()

    def _init_file(self):
        if not os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        'Trade_ID', 'Date', 'Entry_Time', 'Symbol', 'Type', 'Strike', 'Action',
                        'Entry_Price', 'SL', 'Target_1', 'Target_2',
                        'Exit_Price', 'Exit_Time', 'Exit_Reason', 'PnL_Pts', 'Status'
                    ])
            except Exception as e:
                print(f"Forward test init error: {e}")

    def log_entry(self, symbol: str, opt_type: str, strike: int, action: str, entry_price: float, sl: float, t1: float, t2: float):
        trade_key = (opt_type, strike)
        if trade_key in self.active_trades:
            return None

        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')
        trade_id = f"{symbol}_{opt_type}_{strike}_{now.strftime('%Y%m%d_%H%M%S')}"

        trade = {
            'Trade_ID': trade_id,
            'Date': date_str,
            'Entry_Time': time_str,
            'Symbol': symbol,
            'Type': opt_type,
            'Strike': strike,
            'Action': action,
            'Entry_Price': round(entry_price, 2),
            'SL': round(sl, 2),
            'Target_1': round(t1, 2),
            'Target_2': round(t2, 2),
            'Exit_Price': '',
            'Exit_Time': '',
            'Exit_Reason': 'ACTIVE',
            'PnL_Pts': 0.0,
            'Status': 'ACTIVE'
        }
        self.active_trades[trade_key] = trade
        self._append_row(trade)
        print(f"📝 [FORWARD TEST] New Trade Logged: {action} {opt_type} {strike} @ {entry_price:.2f} | SL:{sl:.1f} | T1:{t1:.1f}")
        return trade

    def update_tick(self, opt_type: str, strike: int, ltp: float):
        trade_key = (opt_type, strike)
        if trade_key not in self.active_trades or ltp <= 0:
            return None

        trade = self.active_trades[trade_key]
        now = datetime.now()
        time_str = now.strftime('%H:%M:%S')
        closed = False

        if trade['Action'] == 'BUY':
            if trade['SL'] > 0 and ltp <= trade['SL']:
                trade['Exit_Price'] = round(ltp, 2)
                trade['Exit_Time'] = time_str
                trade['Exit_Reason'] = 'SL Hit'
                trade['PnL_Pts'] = round(ltp - trade['Entry_Price'], 2)
                trade['Status'] = 'CLOSED'
                closed = True
            elif trade['Target_1'] > 0 and ltp >= trade['Target_1']:
                trade['Exit_Price'] = round(ltp, 2)
                trade['Exit_Time'] = time_str
                trade['Exit_Reason'] = 'Target 1 Hit'
                trade['PnL_Pts'] = round(ltp - trade['Entry_Price'], 2)
                trade['Status'] = 'CLOSED'
                closed = True
        elif trade['Action'] == 'SELL':
            if trade['SL'] > 0 and ltp >= trade['SL']:
                trade['Exit_Price'] = round(ltp, 2)
                trade['Exit_Time'] = time_str
                trade['Exit_Reason'] = 'SL Hit'
                trade['PnL_Pts'] = round(trade['Entry_Price'] - ltp, 2)
                trade['Status'] = 'CLOSED'
                closed = True
            elif trade['Target_1'] > 0 and ltp <= trade['Target_1']:
                trade['Exit_Price'] = round(ltp, 2)
                trade['Exit_Time'] = time_str
                trade['Exit_Reason'] = 'Target 1 Hit'
                trade['PnL_Pts'] = round(trade['Entry_Price'] - ltp, 2)
                trade['Status'] = 'CLOSED'
                closed = True

        if closed:
            del self.active_trades[trade_key]
            self._update_trade_in_file(trade)
            print(f"🏁 [FORWARD TEST] Trade Closed: {trade['Action']} {trade['Type']} {trade['Strike']} | Reason:{trade['Exit_Reason']} | PnL:{trade['PnL_Pts']} pts")
            return trade
        return None

    def _append_row(self, trade):
        try:
            with open(self.filepath, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade['Trade_ID'], trade['Date'], trade['Entry_Time'], trade['Symbol'],
                    trade['Type'], trade['Strike'], trade['Action'], trade['Entry_Price'],
                    trade['SL'], trade['Target_1'], trade['Target_2'], trade['Exit_Price'],
                    trade['Exit_Time'], trade['Exit_Reason'], trade['PnL_Pts'], trade['Status']
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
                    rows[i] = [
                        updated_trade['Trade_ID'], updated_trade['Date'], updated_trade['Entry_Time'], updated_trade['Symbol'],
                        updated_trade['Type'], updated_trade['Strike'], updated_trade['Action'], updated_trade['Entry_Price'],
                        updated_trade['SL'], updated_trade['Target_1'], updated_trade['Target_2'], updated_trade['Exit_Price'],
                        updated_trade['Exit_Time'], updated_trade['Exit_Reason'], updated_trade['PnL_Pts'], updated_trade['Status']
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
                    if s_c.get("date") == today_str and all(k in s_c for k in ["Weekly", "Daily", "1-Hour", "15-Min", "5-Min"]):
                        print(f"Loaded {symbol} TradingView pivots from local cache ({today_str}):")
                        for label in ["Weekly", "Daily", "1-Hour", "15-Min", "5-Min"]:
                            res = cached_pivots[label]
                            print(f"  [{label}] P:{res['P']} R1:{res['R1']} S1:{res['S1']} R2:{res['R2']} S2:{res['S2']}")
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
    }.get(symbol, '%5ENSEI')

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
        return {
            "H": round(h, 2), "L": round(l, 2), "C": round(c, 2),
            "P":  round(p, 2),
            "R1": round(p + 0.382 * rng, 2),
            "S1": round(p - 0.382 * rng, 2),
            "R2": round(p + 0.618 * rng, 2),
            "S2": round(p - 0.618 * rng, 2),
            "R3": round(p + 1.000 * rng, 2),
            "S3": round(p - 1.000 * rng, 2),
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
                       ll: float = 0.0, state: str = "WAITING", level: str = "S2") -> dict:
    """
    BUY entry — triggers ONLY after the state machine confirms a support reclaim (S1 or S2).
    """
    if not pivots or ltp <= 0 or state != "CONFIRMED_BUY":
        return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "SL": 0}

    s1, s2, s3 = pivots["S1"], pivots["S2"], pivots["S3"]
    p,  r1, r2  = pivots["P"],  pivots["R1"], pivots["R2"]

    if level == "S1":
        if ltp < s1:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "SL": 0}
        zone = "S1 Reclaim"
        effective_sl = round(ll if ll > 0 else (s2 if s2 < s1 else s1 * 0.98), 2)
        if ltp < p:
            t1, t2, t3 = p, r1, r2
        else:
            t1, t2, t3 = r1, r2, pivots.get("R3", r2 * 1.01)
    else:
        if ltp < s2:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "SL": 0}
        zone = "S2 Reclaim"
        tol = max(s2 * 0.02, 1.0)
        effective_sl = round(ll if ll > 0 else (s3 if s3 < s2 else s2 - tol), 2)
        if ltp < s1:
            t1, t2, t3 = s1, p, r1
        elif ltp < p:
            t1, t2, t3 = p, r1, r2
        else:
            t1, t2, t3 = r1, r2, pivots.get("R3", r2 * 1.01)

    label  = "CE" if opt_type == "CE" else "PE"
    detail = f"BUY {label} @ {ltp:.2f} [{zone} | LL SL:{effective_sl:.1f}] → T1:{t1} T2:{t2} T3:{t3}"
    return {
        "entry": True, "zone": zone, "detail": detail,
        "T1": t1, "T2": t2, "T3": t3, "SL": effective_sl
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
                            hh: float = 0.0, state: str = "WAITING", level: str = "R2") -> dict:
    """
    SELL entry — triggers ONLY after the state machine confirms a resistance rejection (R1 or R2).
    """
    if not pivots or ltp <= 0 or state != "CONFIRMED_SELL":
        return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "SL": 0}

    r1, r2, r3 = pivots["R1"], pivots["R2"], pivots["R3"]
    p,  s1, s2  = pivots["P"],  pivots["S1"], pivots["S2"]

    if level == "R1":
        if ltp > r1:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "SL": 0}
        zone = "R1 Rejection"
        effective_sl = round(hh if hh > 0 else (r2 if r2 > r1 else r1 * 1.02), 2)
        if ltp > p:
            t1, t2, t3 = p, s1, s2
        else:
            t1, t2, t3 = s1, s2, pivots.get("S3", s2 * 0.99)
    else:
        if ltp > r2:
            return {"entry": False, "zone": "", "detail": "", "T1": 0, "T2": 0, "T3": 0, "SL": 0}
        zone = "R2 Rejection"
        tol = max(r2 * 0.02, 1.0)
        effective_sl = round(hh if hh > 0 else (r3 if r3 > r2 else r2 + tol), 2)
        if ltp > r1:
            t1, t2, t3 = r1, p, s1
        elif ltp > p:
            t1, t2, t3 = p, s1, s2
        else:
            t1, t2, t3 = s1, s2, pivots.get("S3", s2 * 0.99)

    label  = "PE" if opt_type == "PE" else "CE"
    detail = f"SELL {label} @ {ltp:.2f} [{zone} | HH SL:{effective_sl:.1f}] → T1:{t1} T2:{t2} T3:{t3}"
    return {
        "entry": True, "zone": zone, "detail": detail,
        "T1": t1, "T2": t2, "T3": t3, "SL": effective_sl
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
        cls.scrip_df=pd.DataFrame(requests.get(SCRIP_URL,timeout=30).json())

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

    def prepare(self, exchange, symbol, expiries, n_strikes):
        """
        Prepare tokens for one OR multiple expiries.
        expiries: str  (single) or list[str]  (multi-expiry)
        """
        if isinstance(expiries, str):
            expiries = [expiries]

        df   = Session.scrip_df
        cfg  = EXCHANGE_CONFIG[exchange]
        step = cfg["steps"].get(symbol, 50)
        extype = cfg["exchange_type"]

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

            # ATM from live spot (only computed once — same for all expiries of same symbol)
            stok = cfg["spot_tokens"].get(symbol, "")
            atm = all_s[len(all_s) // 2]
            live_spot = self.spot_price.get(symbol, 0)
            if live_spot > 0:
                atm = int(round(live_spot / step) * step)
            if primary_atm is None:
                primary_atm = atm

            targets   = set(atm + i * step for i in range(-n_strikes, n_strikes + 1))
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
            }
            for _, r in combined.iterrows()
        }

        option_tokens = combined["token"].astype(str).tolist()

        self.spot_token = {symbol: stok} if stok else {}
        self.token_list = [{"exchangeType": extype, "tokens": option_tokens}]
        if stok:
            self.token_list.append({
                "exchangeType": cfg["spot_exchange"],
                "tokens": [stok]
            })

        # Return primary (first) expiry filter_df for backwards compat
        return all_filter_dfs[0], primary_atm, step

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
        """Set up Google Sheet with 28 columns including all Fibonacci Pivots (S3 to R3) on both CE and PE."""
        gc          = get_gspread_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        ws          = spreadsheet.get_worksheet(0)
        ws.clear()

        # Rename sheet tab
        try:
            spreadsheet.batch_update({"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": ws.id, "title": f"{symbol} Scanner"},
                "fields": "title"
            }}]})
            ws = spreadsheet.get_worksheet(0)
        except Exception as e:
            print(f"Sheet rename note: {e}")

        self.ws = ws

        col_headers = [
            "Expiry", "Type", "Strike", "LTP", "Trading Zone", "Pivot P", "S1", "S2", "R1", "R2",
            "5M State", "LL / HH", "Signal", "SL", "T1", "T2", "T3", "Status", "Updated"
        ]

        row1 = [f"⚡ FIBONACCI PIVOT SCANNER | {symbol} SPOT: Connecting..."] + [""] * 18
        row2 = [f"⏱️ 15-MIN INTRADAY SECTION: Initializing 15-Minute Fibonacci Pivots & Strike Selection..."] + [""] * 18
        row3 = [f"⏳ 1-HOUR TIMEFRAME SECTION: Initializing 1-Hour Fibonacci Pivots & Structure Strikes..."] + [""] * 18
        row4 = ["🚨 REVERSAL STRATEGY: Initializing S2 Reclaim & R2 Rejection Strategy..."] + [""] * 18
        row5 = [""] * 19
        row6_ce_hdr  = ["📗 CALL (CE) SECTION — BUY ZONE ▲"] + [""] * 18
        row7_ce_cols = col_headers

        all_rows = [row1, row2, row3, row4, row5, row6, row7, row8_ce_hdr, row9_ce_cols]
        ws.update(values=all_rows, range_name=f"A1:S{len(all_rows)}")

        # ── Header & column formatting ──────────────
        try:
            from gspread_formatting import format_cell_range, CellFormat, Color, TextFormat

            # Row 1: Spot & Pivot Banner
            format_cell_range(ws, "A1:S1", CellFormat(
                backgroundColor=Color(0.05, 0.07, 0.11),
                textFormat=TextFormat(foregroundColor=Color(1, 0.84, 0), bold=True, fontSize=11),
                horizontalAlignment="LEFT"
            ))

            # Row 2: 15-Min Intraday Section Banner (Cyan/Teal)
            format_cell_range(ws, "A2:S2", CellFormat(
                backgroundColor=Color(0.04, 0.18, 0.20),
                textFormat=TextFormat(foregroundColor=Color(0.3, 0.9, 0.8), bold=True, fontSize=10),
                horizontalAlignment="LEFT"
            ))

            # Row 3: 1-Hour Timeframe Section Banner (Indigo/Purple)
            format_cell_range(ws, "A3:S3", CellFormat(
                backgroundColor=Color(0.12, 0.08, 0.24),
                textFormat=TextFormat(foregroundColor=Color(0.8, 0.6, 1.0), bold=True, fontSize=10),
                horizontalAlignment="LEFT"
            ))

            # Row 4: Daily Market Structure Banner (Sky Blue)
            format_cell_range(ws, "A4:S4", CellFormat(
                backgroundColor=Color(0.06, 0.14, 0.24),
                textFormat=TextFormat(foregroundColor=Color(0.48, 0.75, 1.0), bold=True, fontSize=10),
                horizontalAlignment="LEFT"
            ))

            # Row 5: Weekly Market Structure Banner (Violet)
            format_cell_range(ws, "A5:S5", CellFormat(
                backgroundColor=Color(0.16, 0.08, 0.22),
                textFormat=TextFormat(foregroundColor=Color(0.82, 0.66, 1.0), bold=True, fontSize=10),
                horizontalAlignment="LEFT"
            ))

            # Row 6: Strategy Alert Banner
            format_cell_range(ws, "A6:S6", CellFormat(
                backgroundColor=Color(0.09, 0.11, 0.15),
                textFormat=TextFormat(foregroundColor=Color(0.35, 0.65, 1.0), bold=True, fontSize=10),
                horizontalAlignment="LEFT"
            ))

            # Row 8: CE Section Banner
            format_cell_range(ws, "A8:S8", CellFormat(
                backgroundColor=Color(0.05, 0.22, 0.10),
                textFormat=TextFormat(foregroundColor=Color(0.4, 1.0, 0.5), bold=True, fontSize=10),
                horizontalAlignment="LEFT"
            ))

            # Row 9: CE Column Headers
            format_cell_range(ws, "A9:S9", CellFormat(
                backgroundColor=Color(0.13, 0.17, 0.23),
                textFormat=TextFormat(foregroundColor=Color(1, 1, 1), bold=True),
                horizontalAlignment="CENTER"
            ))

            print("Sheet formatted with split CE/PE Scanner layout (19 cols A-S) successfully")
        except Exception as e:
            print(f"Formatting note: {e}")

        # ── Setup "Forward Test" sheet tab ──────────
        try:
            ft_ws = None
            for s in spreadsheet.worksheets():
                if s.title == "Forward Test":
                    ft_ws = s
                    break
            if not ft_ws:
                ft_ws = spreadsheet.add_worksheet(title="Forward Test", rows=200, cols=16)
                ft_ws.update(values=[[
                    'Trade_ID', 'Date', 'Entry_Time', 'Symbol', 'Type', 'Strike', 'Action',
                    'Entry_Price', 'SL', 'Target_1', 'Target_2',
                    'Exit_Price', 'Exit_Time', 'Exit_Reason', 'PnL_Pts', 'Status'
                ]], range_name="A1:P1")
            self.forward_test_sheet = ft_ws
            self.sync_forward_test_to_sheet()
            print("Forward Test worksheet ready")
        except Exception as fte:
            print(f"Forward Test sheet tab note: {fte}")

    def sync_forward_test_to_sheet(self):
        """Syncs all records from forward_test_trades.csv to the Forward Test Google Sheet tab."""
        if not self.forward_test_sheet or not os.path.exists(FORWARD_TEST_FILE):
            return
        try:
            with open(FORWARD_TEST_FILE, 'r', newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)
            if rows:
                self.forward_test_sheet.update(values=rows, range_name=f"A1:P{len(rows)}")
        except Exception as e:
            pass

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
        today_focus_date = None     # reset focus strikes each new day

        while self.running:
            try:
                with self.lock:
                    snapshot = dict(self.live_data)
                    spot     = self.spot_price.get(symbol, 0)
                    sp_piv   = self.spot_pivots.get(symbol)
                    sp_zone  = self.spot_zone.get(symbol, "Monitoring")

                # ── Daily 4-strike focus selection (PDF Section 3 & 4) ──────────
                today_date = datetime.now().strftime("%Y-%m-%d")
                if today_date != today_focus_date:
                    try:
                        print("Fetching multi-timeframe pivots from Yahoo Finance (TradingView alignment)...")
                        mtf = get_tv_multi_timeframe_pivots(symbol)
                        cfg  = EXCHANGE_CONFIG.get(next(k for k,v in EXCHANGE_CONFIG.items() if symbol in v["symbols"]), {})
                        step = cfg.get("steps", {}).get(symbol, 50)
                        focus = select_daily_4_focus_strikes(mtf, step)
                        with self.lock:
                            self.mtf_pivots     = mtf
                            self.daily_focus    = focus
                            self.focus_15m      = focus.get("15M", {})
                            self.focus_1h       = focus.get("1H", {})
                            self._focus_date    = today_date
                            self._alert_strikes = set()
                        today_focus_date = today_date
                        f15 = focus.get("15M", {})
                        f1h = focus.get("1H", {})
                        print(f"15M Intraday Focus: CE1={f15.get('CE1')} CE2={f15.get('CE2')} | PE1={f15.get('PE1')} PE2={f15.get('PE2')}")
                        print(f"1H Structure Focus: CE1={f1h.get('CE1')} CE2={f1h.get('CE2')} | PE1={f1h.get('PE1')} PE2={f1h.get('PE2')}")
                        focus_msg = (
                            f"15M Focus: CE {f15.get('CE1')},{f15.get('CE2')} | PE {f15.get('PE1')},{f15.get('PE2')} -- "
                            f"1H: CE {f1h.get('CE1')},{f1h.get('CE2')} | PE {f1h.get('PE1')},{f1h.get('PE2')}"
                        )
                        trigger_immediate_alert(f"📌 {symbol} 15M & 1H Focus Strikes", focus_msg, sound=False)
                    except Exception as fe:
                        print(f"Daily focus error: {fe}")

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

                # Fill grid from live snapshot
                for token, meta in self.token_to_meta.items():
                    if token not in snapshot: continue
                    d        = snapshot[token]
                    ltp      = d["ltp"]
                    strike   = meta["strike"]
                    opt_type = meta["opt_type"]
                    idx      = strike_to_idx.get(strike)
                    if idx is None: continue

                    iv = compute_iv(ltp, S, strike, T, risk_free, opt_type)
                    g  = bs_greeks(S, strike, T, risk_free, iv, opt_type)

                    # Option Fibonacci Pivots (P, S1-S3, R1-R3)
                    opt_high  = d.get("high", ltp)
                    opt_low   = d.get("low", ltp)
                    opt_close = d.get("close", ltp)
                    if opt_high <= 0 or opt_low <= 0 or opt_high == opt_low:
                        opt_open  = d.get("open", ltp)
                        spread    = max(ltp * 0.05, 0.5)
                        opt_high  = max(ltp, opt_open) + spread * 0.1
                        opt_low   = min(ltp, opt_open) - spread * 0.1
                        opt_close = ltp
                    opt_piv   = calc_fib_pivots(opt_high, opt_low, opt_close)

                    # Level label for display column
                    level_info  = check_option_levels(ltp, opt_piv, opt_type)
                    signal_text = level_info["signal"]

                    # ── State Machine tracking (PDF Section 7, 8, 9, 10) ──
                    with self.lock:
                        st_state = self.strategy_states.get((opt_type, strike))
                        if not st_state:
                            st_state = StrategyStrikeState(opt_type, strike)
                            self.strategy_states[(opt_type, strike)] = st_state

                    st_state.update_tick(ltp, opt_piv, datetime.now())

                    # Check support reclaim (BUY) and resistance rejection (SELL) entries
                    # Both functions gate on CONFIRMED_BUY / CONFIRMED_SELL from the state machine
                    buy_entry     = check_option_entry(ltp, opt_piv, opt_type,
                                                       ll=st_state.ll, state=st_state.state,
                                                       level=st_state.trigger_level)
                    sell_entry    = check_option_sell_entry(ltp, opt_piv, opt_type,
                                                            hh=st_state.hh, state=st_state.state,
                                                            level=st_state.trigger_level)
                    detailed_zone = get_detailed_strike_zone(ltp, opt_piv)

                    # Market context tags for all 4 directions
                    sell_ctx = "Mkt Bearish ↓" if opt_type == "PE" else "⚡ Mkt Reversal ↓"
                    buy_ctx  = "Mkt Bullish ↑" if opt_type == "CE" else "⚡ Mkt Reversal ↑"

                    if sell_entry["entry"]:
                        lvl = st_state.trigger_level
                        signal_text = f"▼ SELL {opt_type} [{sell_ctx} | {lvl} Reject | HH SL:{sell_entry['SL']:.1f}]"
                        active_alerts.append(
                            f"SELL {opt_type} {strike} @ {ltp:.2f} | {sell_ctx} | {lvl} Reject → T1:{sell_entry['T1']} T2:{sell_entry['T2']} T3:{sell_entry['T3']} | SL:{sell_entry['SL']}"
                        )
                        self.alert_mgr.send_alert_once(
                            symbol, opt_type, strike, f"SELL {opt_type}", ltp,
                            f"{sell_ctx} | {lvl} Reject | HH SL:{sell_entry['SL']:.1f}",
                            sell_entry['SL'], sell_entry['T1']
                        )
                        self.forward_tracker.log_entry(
                            symbol, opt_type, strike, "SELL", ltp,
                            sell_entry['SL'], sell_entry['T1'], sell_entry['T2']
                        )
                    elif buy_entry["entry"]:
                        lvl = st_state.trigger_level
                        signal_text = f"★ BUY {opt_type} [{buy_ctx} | {lvl} Reclaim | LL SL:{buy_entry['SL']:.1f}]"
                        active_alerts.append(
                            f"BUY {opt_type} {strike} @ {ltp:.2f} | {buy_ctx} | {lvl} Reclaim → T1:{buy_entry['T1']} T2:{buy_entry['T2']} T3:{buy_entry['T3']} | SL:{buy_entry['SL']}"
                        )
                        self.alert_mgr.send_alert_once(
                            symbol, opt_type, strike, f"BUY {opt_type}", ltp,
                            f"{buy_ctx} | {lvl} Reclaim | LL SL:{buy_entry['SL']:.1f}",
                            buy_entry['SL'], buy_entry['T1']
                        )
                        self.forward_tracker.log_entry(
                            symbol, opt_type, strike, "BUY", ltp, buy_entry['SL'], buy_entry['T1'], buy_entry['T2']
                        )

                    # ── State machine overrides: show market context for all 4 trade directions ──
                    mkt_buy_ctx  = "Mkt Bullish ↑" if opt_type == "CE" else "⚡ Mkt Reversal ↑"
                    mkt_sell_ctx = "Mkt Bearish ↓" if opt_type == "PE" else "⚡ Mkt Reversal ↓"

                    if st_state.state == "CONFIRMED_SELL" and st_state.trade_status in ("Active", "Target 1 Hit"):
                        lvl = st_state.trigger_level
                        signal_text = (
                            f"▼ SELL {opt_type} [{mkt_sell_ctx} | {lvl} Reject | HH SL:{st_state.sl:.1f}]"
                        )
                        self.forward_tracker.log_entry(
                            symbol, opt_type, strike, "SELL", ltp, st_state.sl,
                            st_state.targets[0] if st_state.targets else 0.0,
                            st_state.targets[1] if len(st_state.targets) > 1 else 0.0
                        )
                    elif st_state.state == "CONFIRMED_BUY" and st_state.trade_status in ("Active", "Target 1 Hit"):
                        lvl = st_state.trigger_level
                        signal_text = (
                            f"★ BUY {opt_type} [{mkt_buy_ctx} | {lvl} Reclaim | LL SL:{st_state.sl:.1f}]"
                        )
                        self.forward_tracker.log_entry(
                            symbol, opt_type, strike, "BUY", ltp, st_state.sl,
                            st_state.targets[0] if st_state.targets else 0.0,
                            st_state.targets[1] if len(st_state.targets) > 1 else 0.0
                        )
                    elif st_state.state == "SWEEP_R2":
                        signal_text = f"▼ {opt_type} SWEEP R2 [{mkt_sell_ctx}] HH:{st_state.hh:.1f} — Watching for rejection"
                    elif st_state.state == "RECLAIM_R2":
                        signal_text = f"▼ {opt_type} RECLAIM R2 [{mkt_sell_ctx}] — Awaiting bearish 5M candle"
                    elif st_state.state == "SWEEP_R1":
                        signal_text = f"▼ {opt_type} SWEEP R1 [{mkt_sell_ctx}] HH:{st_state.hh:.1f} — Watching for R1 rejection"
                    elif st_state.state == "RECLAIM_R1":
                        signal_text = f"▼ {opt_type} RECLAIM R1 [{mkt_sell_ctx}] — Awaiting bearish 5M candle"
                    elif st_state.state == "SWEEP_S2":
                        signal_text = f"★ {opt_type} SWEEP S2 [{mkt_buy_ctx}] LL:{st_state.ll:.1f} — Watching for reclaim"
                    elif st_state.state == "RECLAIM_S2":
                        signal_text = f"★ {opt_type} RECLAIM S2 [{mkt_buy_ctx}] — Awaiting bullish 5M candle"
                    elif st_state.state == "SWEEP_S1":
                        signal_text = f"★ {opt_type} SWEEP S1 [{mkt_buy_ctx}] LL:{st_state.ll:.1f} — Watching for S1 reclaim"
                    elif st_state.state == "RECLAIM_S1":
                        signal_text = f"★ {opt_type} RECLAIM S1 [{mkt_buy_ctx}] — Awaiting bullish 5M candle"

                    # Update forward test tick tracking for target/SL exit
                    closed_t = self.forward_tracker.update_tick(opt_type, strike, ltp)
                    if closed_t:
                        self.sync_forward_test_to_sheet()

                    # Always add to watch list if option has pivots and valid LTP
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

                        zone_display = f"{strike_tag} {detailed_zone}".strip()
                        # OI for this specific token (no cross-expiry overwrite)
                        tok_oi  = d.get("oi",  0)
                        tok_vol = d.get("vol", 0)
                        watch_item = {
                            "type":         opt_type,
                            "strike":       strike,
                            "ltp":          ltp,
                            "oi":           tok_oi,
                            "vol":          tok_vol,
                            "zone":         zone_display,
                            "expiry":       tok_meta.get("expiry", "-"),
                            "is_15m":       is_15m_focus,
                            "is_1h":        is_1h_focus,
                            "s3":           opt_piv["S3"],
                            "s2":           opt_piv["S2"],
                            "s1":           opt_piv["S1"],
                            "p":            opt_piv["P"],
                            "r1":           opt_piv["R1"],
                            "r2":           opt_piv["R2"],
                            "r3":           opt_piv["R3"],
                            "category":     level_info["category"],
                            "status":       signal_text,
                            "m5_state":     st_state.state,
                            "trade_status": st_state.trade_status,
                            "ll":           st_state.ll,
                            "hh":           st_state.hh,
                            "sl":           st_state.sl,
                            "targets":      list(st_state.targets),
                            "target":       st_state.targets[0] if st_state.targets else 0.0
                        }
                        if level_info["is_match"] or st_state.state != "WAITING" or strike in focus_all_4:
                            all_watch.append(watch_item)
                            if "SUP" in level_info["category"] or st_state.state.endswith("_S2"):
                                ce_sup_watch.append(watch_item)
                            if "RES" in level_info["category"] or st_state.state.endswith("_R2"):
                                pe_res_watch.append(watch_item)

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
                    "Expiry", "Type", "Strike", "LTP", "OI", "Volume", "Trading Zone", "Pivot P", "S1", "S2", "R1", "R2",
                    "5M State", "LL / HH", "Signal", "SL", "T1", "T2", "Status", "Updated"
                ]

                def _make_row(w):
                    # LL or HH — whichever is relevant (SL seed from sweep)
                    ll_hh_str = (
                        f"LL:{w.get('ll', 0):.2f}" if w.get("ll", 0) > 0
                        else (f"HH:{w.get('hh', 0):.2f}" if w.get("hh", 0) > 0 else "-")
                    )

                    # SL (from confirmed entry, or ‘-’ while still sweeping)
                    sl_val = w.get("sl", 0)
                    sl_str = f"{sl_val:.2f}" if sl_val > 0 else "-"

                    # Targets T1 / T2 from the full target ladder
                    tgts   = w.get("targets", [])
                    t1_str = f"{tgts[0]:.2f}" if len(tgts) > 0 and tgts[0] > 0 else "-"
                    t2_str = f"{tgts[1]:.2f}" if len(tgts) > 1 and tgts[1] > 0 else "-"

                    # Format OI and Volume in K / L for readability
                    oi_val  = w.get("oi",  0)
                    vol_val = w.get("vol", 0)
                    oi_str  = (f"{oi_val/100000:.1f}L"  if oi_val  >= 100000 else
                               (f"{oi_val/1000:.1f}K"   if oi_val  >= 1000   else str(oi_val)))
                    vol_str = (f"{vol_val/100000:.1f}L" if vol_val >= 100000 else
                               (f"{vol_val/1000:.1f}K"  if vol_val >= 1000   else str(vol_val)))

                    return [
                        w.get("expiry", "-"),
                        w["type"],
                        w["strike"],
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
                        ll_hh_str,
                        w.get("status", "-"),
                        sl_str,   # SL column
                        t1_str,   # T1
                        t2_str,   # T2
                        w.get("trade_status", "Waiting"),
                        now_str
                    ]

                # Separate CE and PE watch items, each sorted: focus first, then by expiry, strike asc
                ce_items = sorted(
                    [w for w in all_watch if w["type"] == "CE"],
                    key=lambda w: (
                        w.get("expiry", ""),
                        0 if w["strike"] in all_focus_set else 1,
                        w["strike"]
                    )
                )
                pe_items = sorted(
                    [w for w in all_watch if w["type"] == "PE"],
                    key=lambda w: (
                        w.get("expiry", ""),
                        0 if w["strike"] in all_focus_set else 1,
                        w["strike"]
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
                            divrow = [f"── Expiry: {exp} ──"] + [""] * 19
                            rows.append(divrow)
                        rows.append(_make_row(w))
                    return rows

                ce_rows = _rows_with_expiry_dividers(ce_items)
                pe_rows = _rows_with_expiry_dividers(pe_items)

                # Pad each section  (now 21 columns: added OI + Volume)
                min_rows = max(len(sorted_strikes), 1)
                ce_rows += [[""] * 21 for _ in range(max(0, min_rows - len(ce_rows)))]
                pe_rows += [[""] * 21 for _ in range(max(0, min_rows - len(pe_rows)))]

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
                ce_banner = [f"📗 CALL (CE) — BUY ZONE ▲  |  {len(ce_items)} active  |  15M Focus: {f15.get('CE1','-')}, {f15.get('CE2','-')}  |  1H Focus: {f1h.get('CE1','-')}, {f1h.get('CE2','-')}"] + [""] * 20
                pe_banner = [f"📕 PUT (PE)  — SELL ZONE ▼  |  {len(pe_items)} active  |  15M Focus: {f15.get('PE1','-')}, {f15.get('PE2','-')}  |  1H Focus: {f1h.get('PE1','-')}, {f1h.get('PE2','-')}"] + [""] * 20
                divider   = ["─" * 20] + [""] * 20

                full_grid = (
                    [[r1_text] + [""] * 20,
                     [r2_text] + [""] * 20,
                     [r3_text] + [""] * 20,
                     [r4_text] + [""] * 20,
                     [oi_header_text] + [""] * 20,
                     [""] * 21,
                     ce_banner,
                     col_headers]
                    + ce_rows
                    + [divider, pe_banner, col_headers]
                    + pe_rows
                )

                # Single atomic batch write — 21 cols A to U
                self.ws.update(values=full_grid, range_name=f"A1:U{len(full_grid)}")

                loop += 1

                # ── Apply conditional row colours (rate-limited to every 20 loops ≈ 40 s) ──
                if loop % 20 == 0:
                    try:
                        from gspread_formatting import format_cell_range, CellFormat, Color, TextFormat
                        ce_start_row = 9   # +1 because we added OI summary row
                        pe_start_row = ce_start_row + len(ce_rows) + 3

                        def _color_section(start, rows_data, is_ce):
                            for i, row in enumerate(rows_data):
                                if not any(row):
                                    continue
                                sheet_row = start + i
                                # Columns shifted: signal now at index 14 (was 12), zone at index 6 (was 4)
                                status = row[14] if len(row) > 14 else ""
                                zone   = row[6]  if len(row) > 6  else ""
                                if "BUY" in status and ("★" in status or "CONFIRMED" in status):
                                    bg = Color(0.04, 0.28, 0.09)
                                elif "SELL" in status and ("▼" in status or "CONFIRMED" in status):
                                    bg = Color(0.28, 0.04, 0.04)
                                elif "Buy Area" in zone:
                                    bg = Color(0.06, 0.18, 0.08)
                                elif "Sell Area" in zone:
                                    bg = Color(0.18, 0.06, 0.06)
                                elif row[0] and "Expiry" in str(row[0]):   # expiry divider row
                                    bg = Color(0.10, 0.10, 0.18)
                                elif is_ce:
                                    bg = Color(0.07, 0.11, 0.08)
                                else:
                                    bg = Color(0.11, 0.07, 0.08)
                                format_cell_range(self.ws, f"A{sheet_row}:U{sheet_row}",
                                    CellFormat(backgroundColor=bg,
                                               textFormat=TextFormat(foregroundColor=Color(0.9, 0.9, 0.9))))

                        _color_section(ce_start_row, ce_rows, is_ce=True)
                        _color_section(pe_start_row, pe_rows, is_ce=False)

                        format_cell_range(self.ws, "A7:U7", CellFormat(
                            backgroundColor=Color(0.05, 0.22, 0.10),
                            textFormat=TextFormat(foregroundColor=Color(0.4, 1.0, 0.5), bold=True)))
                        pe_banner_row = ce_start_row + len(ce_rows) + 1
                        format_cell_range(self.ws, f"A{pe_banner_row}:U{pe_banner_row}", CellFormat(
                            backgroundColor=Color(0.22, 0.05, 0.08),
                            textFormat=TextFormat(foregroundColor=Color(1.0, 0.4, 0.4), bold=True)))
                    except Exception:
                        pass

                if loop % 10 == 0:
                    self.sync_forward_test_to_sheet()
                if loop % 5 == 0 and status_cb:
                    status_cb(
                        f"● LIVE | spot:{spot} ({sp_zone}) | {len(all_watch)} active watch | {now_str}"
                    )

                time.sleep(2)

            except Exception as e:
                if not self.running: break
                print(f"Sheet loop error: {e}")
                time.sleep(5)

feed = FeedEngine()

# ═══════════════════════════════════════════════════════════
# CONTROL PANEL
# ═══════════════════════════════════════════════════════════
class ControlPanel:
    def __init__(self,root):
        self.root=root
        root.title("Angel One — Live Option Chain & Fibonacci Pivot Reversal")
        root.geometry("1120x900")
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

        # Style Treeview
        s.configure("Treeview",
                    background="#161b22",
                    foreground="#f0f6fc",
                    fieldbackground="#161b22",
                    font=("Consolas", 8),
                    rowheight=22)
        s.configure("Treeview.Heading",
                    background="#21262d",
                    foreground="#58a6ff",
                    font=("Consolas", 8, "bold"))
        s.map("Treeview", background=[("selected", "#1f6feb")])

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

        lbl(frame,"Symbol",1)
        self.sym_cb=cb(frame,self.symbol_var,[],1,self._load_expiries)

        lbl(frame, "Expiry (Current)", 2)
        self.exp_cb = cb(frame, self.expiry_var, [], 2)

        # ── Next Expiry row ─────────────────────────────────
        lbl(frame, "Next Expiry", 3)
        nf = tk.Frame(frame, bg="#161b22")
        nf.grid(row=3, column=1, padx=10, pady=6, sticky="ew")
        self.next_exp_cb = ttk.Combobox(
            nf, textvariable=self.next_exp_var, values=[],
            state="readonly", width=18, font=("Consolas", 10))
        self.next_exp_cb.pack(side="left", fill="x", expand=True)
        tk.Checkbutton(
            nf, text="Enable", variable=self.use_next_exp,
            bg="#161b22", fg="#58a6ff", selectcolor="#21262d",
            activebackground="#161b22", activeforeground="#58a6ff",
            font=("Consolas", 9)
        ).pack(side="left", padx=6)

        lbl(frame, "Strikes (ATM +/-)", 4)
        sf = tk.Frame(frame, bg="#161b22")
        sf.grid(row=4, column=1, padx=10, pady=6, sticky="ew")
        self.slbl = tk.Label(sf, text="10", bg="#161b22", fg="#58a6ff",
                             font=("Consolas", 10, "bold"), width=3)
        self.slbl.pack(side="left")
        tk.Scale(sf, from_=3, to=20, orient="horizontal", variable=self.strikes_var,
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
        rf=tk.Frame(rev_frame,bg="#161b22")
        rf.pack(fill="x",padx=8,pady=(0,4))
        self.filter_var=tk.StringVar(value="ALL")
        for val,txt in [
            ("ALL", "All Setups"),
            ("FOCUS_15M", "⭐ 15M Intraday Focus"),
            ("FOCUS_1H", "⏳ 1H Structure Focus"),
            ("FOCUS_ALL", "All Focus"),
            ("CE_SUP", "CE Support (S1/S2)"),
            ("PE_RES", "PE Resistance (R1/R2)")
        ]:
            tk.Radiobutton(rf,text=txt,value=val,variable=self.filter_var,
                           bg="#161b22",fg="#f0f6fc",selectcolor="#21262d",
                           activebackground="#161b22",activeforeground="#58a6ff",
                           font=("Consolas",8),command=self._refresh_display).pack(side="left",padx=4)

        # Treeview for strikes near Support / Resistance
        tree_frame=tk.Frame(rev_frame,bg="#161b22")
        tree_frame.pack(fill="both",expand=True,padx=8,pady=(0,6))

        cols = ("Expiry", "Type", "Strike", "LTP", "OI", "Vol", "Trading Zone", "P", "S1", "S2", "R1", "R2", "5M State", "LL/HH", "Signal", "SL", "Target", "Status")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=7)
        col_widths = {
            "Expiry": 75, "Type": 40, "Strike": 60, "LTP": 55, "OI": 65, "Vol": 60, "Trading Zone": 155,
            "P": 50, "S1": 50, "S2": 50, "R1": 50, "R2": 50,
            "5M State": 90, "LL/HH": 65, "Signal": 120, "SL": 50, "Target": 60, "Status": 70
        }
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=col_widths.get(c, 60), anchor="center")

        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # ── Google Sheet link & Max OI panel ────────────────
        row_b=tk.Frame(r,bg="#0d1117")
        row_b.pack(fill="x",padx=16,pady=4)

        sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        tk.Button(row_b,text="📊 Open Google Sheet ->",
                  command=lambda: __import__("webbrowser").open(sheet_url),
                  bg="#21262d",fg="#58a6ff",font=("Consolas",9,"bold"),
                  relief="flat",bd=0,cursor="hand2",padx=10,pady=4).pack(side="left")

        self.oi_lbl=tk.Label(row_b,textvariable=self.max_oi_info,
                             bg="#0d1117",fg="#ffd700",font=("Consolas",8))
        self.oi_lbl.pack(side="right")

        # ── Start / Stop Buttons ────────────────────────────
        bf=tk.Frame(r,bg="#0d1117")
        bf.pack(fill="x",padx=16,pady=6)
        self.start_btn=tk.Button(bf,text="START SCANNER & FEED",command=self.start_feed,
            bg="#238636",fg="white",font=("Consolas",11,"bold"),
            relief="flat",cursor="hand2",pady=8,bd=0,
            activebackground="#2ea043",activeforeground="white")
        self.start_btn.pack(side="left",fill="x",expand=True,padx=(0,4))

        self.stop_btn=tk.Button(bf,text="STOP",command=self.stop_feed,
            bg="#6e2424",fg="#ff8080",font=("Consolas",11,"bold"),
            relief="flat",cursor="hand2",pady=8,bd=0,state="disabled",
            activebackground="#da3633",activeforeground="white")
        self.stop_btn.pack(side="left",fill="x",expand=True,padx=(4,0))

        # Status bar
        sb=tk.Frame(r,bg="#161b22",highlightthickness=1,highlightbackground="#30363d")
        sb.pack(fill="x",padx=16,pady=(4,8))
        self.status_lbl=tk.Label(sb,textvariable=self.status_var,
                                  bg="#161b22",fg="#3fb950",
                                  font=("Consolas",8),anchor="w")
        self.status_lbl.pack(fill="x",padx=8,pady=4)

    def _on_exchange_change(self):
        exch=self.exchange_var.get()
        syms=EXCHANGE_CONFIG[exch]["symbols"]
        self.sym_cb["values"]=syms
        self.symbol_var.set(syms[0])
        self._load_expiries()

    def _load_expiries(self):
        if Session.scrip_df is None: return
        exch = self.exchange_var.get()
        sym  = self.symbol_var.get()
        df   = Session.scrip_df

        seg_candidates = [exch]
        if exch == "BFO": seg_candidates = ["BFO", "BSE"]

        inst = EXCHANGE_CONFIG[exch]["inst_types"]
        raw  = df[
            (df["name"] == sym) &
            (df["exch_seg"].isin(seg_candidates)) &
            (df["instrumenttype"].isin(inst))
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
            else:
                self.next_exp_var.set("")
        self._set_status(f"● {len(expiries)} expiries for {sym}")
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
        f15 = feed.focus_15m or focus.get("15M", {})
        f1h = feed.focus_1h or focus.get("1H", {})
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

        # Update Strategy Dashboard Treeview with Filter
        for item in self.tree.get_children():
            self.tree.delete(item)

        filt = self.filter_var.get() if hasattr(self, "filter_var") else "ALL"
        focus_15m_strikes = set(f15.get("all_4", []))
        focus_1h_strikes  = set(f1h.get("all_4", []))
        all_focus_strikes = focus_15m_strikes | focus_1h_strikes

        if filt == "FOCUS_15M":
            display_watch = [w for w in watch if w.get("strike") in focus_15m_strikes]
        elif filt == "FOCUS_1H":
            display_watch = [w for w in watch if w.get("strike") in focus_1h_strikes]
        elif filt in ("FOCUS", "FOCUS_ALL"):
            display_watch = [w for w in watch if w.get("strike") in all_focus_strikes]
        elif filt == "CE_SUP":
            display_watch = [w for w in watch if w.get("type") == "CE"]
        elif filt == "PE_RES":
            display_watch = [w for w in watch if w.get("type") == "PE"]
        else:
            display_watch = watch

        # Sort so 15M/1H Focus strikes appear at the very top
        display_watch = sorted(display_watch, key=lambda w: (
            0 if w.get("strike") in focus_15m_strikes else (1 if w.get("strike") in focus_1h_strikes else 2),
            w.get("strike", 0)
        ))

        for w in display_watch[:35]:
            ll_hh_str = f"LL:{w.get('ll', 0):.1f}" if w.get("ll", 0) > 0 else (f"HH:{w.get('hh', 0):.1f}" if w.get("hh", 0) > 0 else "-")
            sl_str    = f"{w.get('sl', 0):.1f}" if w.get("sl", 0) > 0 else "-"
            tgt_str   = f"{w.get('target', 0):.1f}" if w.get("target", 0) > 0 else "-"

            # Format OI and Volume
            oi_v  = w.get("oi",  0)
            vol_v = w.get("vol", 0)
            oi_disp  = f"{oi_v/100000:.1f}L"  if oi_v  >= 100000 else (f"{oi_v/1000:.1f}K"  if oi_v  >= 1000 else str(oi_v))
            vol_disp = f"{vol_v/100000:.1f}L" if vol_v >= 100000 else (f"{vol_v/1000:.1f}K" if vol_v >= 1000 else str(vol_v))

            self.tree.insert("", "end", values=(
                w.get("expiry", "-"),
                w["type"],
                w["strike"],
                f"{w['ltp']:.2f}",
                oi_disp,
                vol_disp,
                w.get("zone", "-"),
                f"{w.get('p', 0.0):.2f}",
                f"{w.get('s1', 0.0):.2f}",
                f"{w.get('s2', 0.0):.2f}",
                f"{w.get('r1', 0.0):.2f}",
                f"{w.get('r2', 0.0):.2f}",
                w.get("m5_state", "WAITING"),
                ll_hh_str,
                w.get("status", "-"),
                sl_str,
                tgt_str,
                w.get("trade_status", "Waiting")
            ))

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

        if feed.running:
            self.root.after(2000, self._refresh_display)

    def _set_status(self,msg,color="#3fb950"):
        self.status_var.set(msg)
        self.status_lbl.config(fg=color)

    def start_feed(self):
        exch   = self.exchange_var.get()
        symbol = self.symbol_var.get()
        expiry = self.expiry_var.get()
        n      = self.strikes_var.get()

        if not expiry:
            messagebox.showerror("Error", "Please select an expiry"); return

        # Build list of expiries to subscribe
        expiries_to_load = [expiry]
        if self.use_next_exp.get():
            next_exp = self.next_exp_var.get()
            if next_exp and next_exp != expiry:
                expiries_to_load.append(next_exp)

        self._set_status("Connecting & setting up Google Sheet...")
        self.start_btn.config(state="disabled")

        def _run():
            try:
                filter_df, atm, step = feed.prepare(exch, symbol, expiries_to_load, n)
                strikes = sorted(set(filter_df["strike"].astype(float).div(100).astype(int)))
                feed.setup_sheet(symbol, strikes)
                feed.start_ws()
                time.sleep(3)
                exp_label = " + ".join(expiries_to_load)
                self.root.after(0, lambda: self._set_status(
                    f"● LIVE | {symbol} | {exp_label} | ATM~{atm} | +/-{n} strikes"))
                self.root.after(0, lambda: self.stop_btn.config(state="normal"))
                self.root.after(1000, self._refresh_display)
                feed.update_sheet_loop(
                    symbol, strikes,
                    status_cb=lambda m: self.root.after(0, lambda: self._set_status(m))
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.start_btn.config(state="normal"))
                self.root.after(0, lambda: self._set_status(f"Error: {e}", "#f85149"))

        threading.Thread(target=_run, daemon=True).start()

    def stop_feed(self):
        feed.running=False
        feed.stop_ws()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
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

