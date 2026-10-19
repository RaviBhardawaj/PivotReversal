"""
Advanced Strategy Configuration and Constants
Strategy: ADVANCED_PIVOT_REVERSAL
"""

import os
from datetime import time as dt_time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Strategy Identification ──────────────────────────────────────────────────
STRATEGY_ID = "ADVANCED_PIVOT_REVERSAL"
ADVANCED_SHEET_TAB_NAME = "Advanced Strategy"
ADVANCED_CSV_FILE = os.path.join(BASE_DIR, "advanced_strategy_trades.csv")
ADVANCED_REJECTIONS_FILE = os.path.join(BASE_DIR, "advanced_strategy_rejections.csv")
ADVANCED_STATE_FILE = os.path.join(BASE_DIR, "advanced_strategy_open.json")
ADVANCED_NEXT_DAY_PLAN_FILE = os.path.join(BASE_DIR, "advanced_strategy_next_day_plan.json")

# ── Entry Filters Configuration ──────────────────────────────────────────────
# Filter A: Weekly and Daily Context
ADVANCED_REJECT_ON_CONTEXT_CONFLICT = os.getenv("ADV_REJECT_ON_CONTEXT_CONFLICT", "true").lower() in ("true", "1", "yes")

# Filter C: 5M Candle Confirmation Strength
ADVANCED_MIN_CANDLE_BODY_PCT = float(os.getenv("ADV_MIN_CANDLE_BODY_PCT", "0.20"))  # body >= 20% of total candle range
ADVANCED_CLOSE_NEAR_EXTREME_PCT = float(os.getenv("ADV_CLOSE_NEAR_EXTREME_PCT", "0.35"))  # opposite wick <= 35% of range

# Filter D: Optional Retest Mode
ADVANCED_REQUIRE_RETEST = os.getenv("ADVANCED_REQUIRE_RETEST", "true").lower() in ("true", "1", "yes")
ADVANCED_RETEST_TOLERANCE_PCT = float(os.getenv("ADV_RETEST_TOLERANCE_PCT", "1.5"))  # within 1.5% of level

# Filter E: Risk / Reward
ADVANCED_MIN_RR_RATIO = float(os.getenv("ADV_MIN_RR_RATIO", "1.5"))  # minimum reward/risk ratio 1.5:1
ADVANCED_MAX_SL_PTS = float(os.getenv("ADV_MAX_SL_PTS", "60.0"))    # maximum SL distance in points
ADVANCED_MIN_SL_PTS = float(os.getenv("ADV_MIN_SL_PTS", "1.0"))     # minimum SL distance in points
ADVANCED_EST_CHARGE_PCT = float(os.getenv("ADV_EST_CHARGE_PCT", "0.05"))  # estimated charges (% of trade turnover)
ADVANCED_EST_FLAT_BROKERAGE = float(os.getenv("ADV_EST_FLAT_BROKERAGE", "40.0")) # Rs 20 buy + Rs 20 sell flat

# Filter F: Option Liquidity & Spread
ADVANCED_MAX_SPREAD_PTS = float(os.getenv("ADV_MAX_SPREAD_PTS", "3.0"))  # max bid/ask spread in pts
ADVANCED_MAX_SPREAD_PCT = float(os.getenv("ADV_MAX_SPREAD_PCT", "3.5"))  # max bid/ask spread in %
ADVANCED_MAX_QUOTE_AGE_S = float(os.getenv("ADV_MAX_QUOTE_AGE_S", "15.0")) # max quote age in seconds

# Filter G & Setup Types
ADVANCED_ALLOW_P_CONTINUATION = os.getenv("ADV_ALLOW_P_CONTINUATION", "true").lower() in ("true", "1", "yes")
ADVANCED_ALLOW_REENTRY = os.getenv("ADV_ALLOW_REENTRY", "true").lower() in ("true", "1", "yes")
ADVANCED_MAX_REENTRIES = int(os.getenv("ADV_MAX_REENTRIES", "1"))
ADVANCED_MAX_ACTIVE_TRADES_TOTAL = int(os.getenv("ADV_MAX_ACTIVE_TRADES_TOTAL", "6"))

# Trading hours & EOD square-off
EOD_SQUARE_OFF_TIME = dt_time(15, 0)

# ── Required 44 Google Sheet Columns ─────────────────────────────────────────
ADVANCED_SHEET_HEADERS = [
    "Date",                     # 1
    "Entry Time",               # 2
    "Exit Time",                # 3
    "Strategy ID",              # 4
    "Trade ID",                 # 5
    "Underlying Symbol",        # 6
    "Underlying Price",         # 7
    "Underlying Zone",          # 8
    "Setup Type",               # 9
    "Entry Reason",             # 10
    "Option Symbol",            # 11
    "Option Type",              # 12
    "Strike",                   # 13
    "Expiry",                   # 14
    "BUY/SELL",                 # 14
    "Quantity",                 # 15
    "Entry Price",              # 16
    "Option P",                 # 17
    "Option R1",                # 18
    "Option R2",                # 19
    "Option S1",                # 20
    "Option S2",                # 21
    "Option R3",                # 22
    "Option S3",                # 23
    "Liquidity LL",             # 24
    "Liquidity HH",             # 25
    "5M Confirmation",          # 26
    "Retest Status",            # 27
    "Stop Loss",                # 28
    "Target 1",                 # 29
    "Target 2",                 # 30
    "Target 3",                 # 31
    "Exit Price",               # 32
    "Exit Reason",              # 33
    "Gross P&L",                # 34
    "Charges",                  # 35
    "Net P&L",                  # 36
    "Points",                   # 37
    "Status",                   # 38
    "Context Filter Result",    # 39
    "Risk/Reward",              # 40
    "Spread Check",             # 41
    "Rejection Reason",         # 42
    "Source Order ID",          # 43
    "Trade Mode (Actual / Shadow)" # 44
]
