"""
Unit and Integration Tests for Advanced Strategy (Strategy B: ADVANCED_PIVOT_REVERSAL)
Run: python3 tests/test_advanced_strategy.py
"""

import sys
import os
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Mock tkinter to allow headless testing
tk = types.ModuleType("tkinter")
class _W:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: None

for n in [
    "Tk", "Label", "Frame", "Button", "StringVar", "BooleanVar", "IntVar",
    "Radiobutton", "Checkbutton", "Toplevel", "Entry", "Canvas", "Scrollbar",
    "Text", "Menu", "OptionMenu", "Spinbox", "PhotoImage", "Listbox"
]:
    setattr(tk, n, _W)
ttk = types.ModuleType("tkinter.ttk")
ttk.Combobox = _W; ttk.Treeview = _W; ttk.Style = _W; ttk.Scrollbar = _W; ttk.Notebook = _W; ttk.Progressbar = _W
mb = types.ModuleType("tkinter.messagebox")
tk.ttk = ttk; tk.messagebox = mb
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb})

# Add project root to path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from advanced_strategy.config import (
    STRATEGY_ID,
    ADVANCED_SHEET_TAB_NAME,
    ADVANCED_SHEET_HEADERS,
    ADVANCED_MIN_RR_RATIO,
    ADVANCED_REQUIRE_RETEST,
)
from advanced_strategy.filters import (
    evaluate_context,
    evaluate_liquidity_sweep,
    evaluate_candle_confirmation,
    evaluate_retest,
    evaluate_risk_reward,
    evaluate_option_liquidity,
    evaluate_no_trade_conditions,
)
from advanced_strategy.tracker import AdvancedStrategyTracker
from advanced_strategy.sheets import AdvancedGoogleSheetsManager
from advanced_strategy.engine import AdvancedStrategyEngine, get_monthly_expiry


class TestAdvancedStrategy(unittest.TestCase):

    def setUp(self):
        self.tmp_csv = os.path.join(ROOT_DIR, "tests", "test_adv_trades.csv")
        self.tmp_state = os.path.join(ROOT_DIR, "tests", "test_adv_state.json")
        self.tmp_rej = os.path.join(ROOT_DIR, "tests", "test_adv_rej.csv")
        for f in (self.tmp_csv, self.tmp_state, self.tmp_rej):
            if os.path.exists(f):
                os.remove(f)

        self.tracker = AdvancedStrategyTracker(
            csv_file=self.tmp_csv,
            state_file=self.tmp_state,
            rejections_file=self.tmp_rej
        )

    def tearDown(self):
        for f in (self.tmp_csv, self.tmp_state, self.tmp_rej):
            if os.path.exists(f):
                os.remove(f)

    # ── 1. Filter A: Context Filter Tests ────────────────────────────────────
    def test_filter_a_context(self):
        daily_pivots = {"P": 25000.0, "S1": 24800.0, "S2": 24600.0, "S3": 24300.0, "R1": 25200.0, "R2": 25400.0, "R3": 25700.0}
        weekly_pivots = {"P": 24900.0, "S1": 24500.0, "S2": 24200.0, "S3": 23900.0, "R1": 25300.0, "R2": 25600.0, "R3": 26000.0}

        # Healthy support reversal: spot near S1
        ok, desc, reason = evaluate_context(24850.0, daily_pivots, weekly_pivots, "CE", "BUY")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

        # Severe breakdown below Daily S3: CE BUY should be rejected
        ok, desc, reason = evaluate_context(24200.0, daily_pivots, weekly_pivots, "CE", "BUY")
        self.assertFalse(ok)
        self.assertIn("breakdown", reason.lower())

        # Severe breakout above Daily R3: PE BUY should be rejected
        ok, desc, reason = evaluate_context(25800.0, daily_pivots, weekly_pivots, "PE", "BUY")
        self.assertFalse(ok)
        self.assertIn("breakout", reason.lower())

    # ── 2. Filter B: Liquidity Sweep Tests ───────────────────────────────────
    def test_filter_b_liquidity_sweep(self):
        # BUY: LL must be < entry and <= level
        ok, reason = evaluate_liquidity_sweep("BUY", "S2", 100.0, ll=95.0, hh=0.0, entry_px=105.0)
        self.assertTrue(ok)

        # BUY with no sweep (LL=0): rejected
        ok, reason = evaluate_liquidity_sweep("BUY", "S2", 100.0, ll=0.0, hh=0.0, entry_px=105.0)
        self.assertFalse(ok)
        self.assertIn("no liquidity sweep", reason.lower())

        # BUY with LL >= entry: rejected
        ok, reason = evaluate_liquidity_sweep("BUY", "S2", 100.0, ll=110.0, hh=0.0, entry_px=105.0)
        self.assertFalse(ok)

        # SELL: HH must be > entry and >= level
        ok, reason = evaluate_liquidity_sweep("SELL", "R2", 200.0, ll=0.0, hh=210.0, entry_px=195.0)
        self.assertTrue(ok)

        # SELL with no sweep (HH=0): rejected
        ok, reason = evaluate_liquidity_sweep("SELL", "R2", 200.0, ll=0.0, hh=0.0, entry_px=195.0)
        self.assertFalse(ok)

    # ── 3. Filter C: 5M Confirmation Candle Tests ────────────────────────────
    def test_filter_c_candle_confirmation(self):
        now_dt = datetime(2026, 10, 9, 10, 0) # bucket 120

        # Unclosed candle (same bucket): rejected
        open_candle = {"open": 100.0, "high": 115.0, "low": 98.0, "close": 112.0, "bucket": 120}
        ok, reason, _ = evaluate_candle_confirmation("BUY", open_candle, now_dt, reclaimed_level_px=100.0)
        self.assertFalse(ok)
        self.assertIn("still open", reason.lower())

        # Closed bullish candle (bucket 119): accepted
        closed_bull = {"open": 101.0, "high": 115.0, "low": 100.0, "close": 113.0, "bucket": 119}
        ok, reason, _ = evaluate_candle_confirmation("BUY", closed_bull, now_dt, reclaimed_level_px=100.0)
        self.assertTrue(ok)

        # Closed bearish candle when evaluating BUY: rejected
        closed_bear = {"open": 113.0, "high": 115.0, "low": 99.0, "close": 101.0, "bucket": 119}
        ok, reason, _ = evaluate_candle_confirmation("BUY", closed_bear, now_dt, reclaimed_level_px=100.0)
        self.assertFalse(ok)
        self.assertIn("bearish", reason.lower())

        # Weak body / long wick (doji): rejected
        doji = {"open": 105.0, "high": 120.0, "low": 90.0, "close": 105.5, "bucket": 119}
        ok, reason, _ = evaluate_candle_confirmation("BUY", doji, now_dt, reclaimed_level_px=100.0)
        self.assertFalse(ok)
        self.assertIn("body", reason.lower())

    # ── 4. Filter D: Retest Mode Tests ───────────────────────────────────────
    def test_filter_d_retest(self):
        # Retest confirmed and held above LL
        ok, status, reason = evaluate_retest(retest_done=True, retest_low=98.0, retest_high=0.0,
                                             level_px=100.0, ll=95.0, hh=0.0, action="BUY")
        self.assertTrue(ok)
        self.assertEqual(status, "RETEST_CONFIRMED")

        # Retest failed (broke below LL)
        ok, status, reason = evaluate_retest(retest_done=True, retest_low=94.0, retest_high=0.0,
                                             level_px=100.0, ll=95.0, hh=0.0, action="BUY")
        self.assertFalse(ok)
        self.assertEqual(status, "RETEST_FAILED")

        # Retest not done yet
        ok, status, reason = evaluate_retest(retest_done=False, retest_low=0.0, retest_high=0.0,
                                             level_px=100.0, ll=95.0, hh=0.0, action="BUY")
        self.assertFalse(ok)
        self.assertEqual(status, "AWAITING_RETEST")

    # ── 5. Filter E: Risk/Reward Tests ───────────────────────────────────────
    def test_filter_e_risk_reward(self):
        # Entry 100, SL 90, T1 125 -> Risk 10, Reward 25, RR 2.5:1 -> PASS
        ok, reason, metrics = evaluate_risk_reward(entry_px=100.0, sl_px=90.0, t1_px=125.0, action="BUY", lot_size=25)
        self.assertTrue(ok)
        self.assertEqual(metrics["rr_ratio"], 2.5)
        self.assertGreater(metrics["net_reward_pts"], 0)

        # Insufficient RR: Entry 100, SL 90, T1 110 -> Risk 10, Reward 10, RR 1.0:1 (< 1.5) -> REJECT
        ok, reason, metrics = evaluate_risk_reward(entry_px=100.0, sl_px=90.0, t1_px=110.0, action="BUY", lot_size=25)
        self.assertFalse(ok)
        self.assertIn("insufficient", reason.lower())

        # Invalid SL on wrong side: SL 105 for BUY 100 -> REJECT
        ok, reason, metrics = evaluate_risk_reward(entry_px=100.0, sl_px=105.0, t1_px=130.0, action="BUY", lot_size=25)
        self.assertFalse(ok)

        # Excessive risk: Risk > 60 pts -> REJECT
        ok, reason, metrics = evaluate_risk_reward(entry_px=100.0, sl_px=30.0, t1_px=220.0, action="BUY", lot_size=25)
        self.assertFalse(ok)
        self.assertIn("excessive", reason.lower())

    # ── 6. Filter F: Liquidity & Spread Tests ────────────────────────────────
    def test_filter_f_liquidity(self):
        # Normal spread: 0.50 pts
        ok, desc, reason = evaluate_option_liquidity(ltp=100.0, bid=99.75, ask=100.25, quote_age_s=1.0)
        self.assertTrue(ok)

        # Wide spread: 5 pts
        ok, desc, reason = evaluate_option_liquidity(ltp=100.0, bid=95.0, ask=100.0, quote_age_s=1.0)
        self.assertFalse(ok)
        self.assertIn("wide", reason.lower())

        # Stale data: age 30s
        ok, desc, reason = evaluate_option_liquidity(ltp=100.0, bid=99.5, ask=100.0, quote_age_s=30.0)
        self.assertFalse(ok)
        self.assertIn("stale", reason.lower())

    # ── 7. Trade Lifecycle & Independent P&L Tracking ────────────────────────
    def test_trade_lifecycle_and_pnl(self):
        opt_piv = {"P": 120.0, "S1": 105.0, "S2": 95.0, "S3": 80.0, "R1": 135.0, "R2": 150.0, "R3": 170.0}

        # Log qualified trade
        trade = self.tracker.log_qualified_trade(
            symbol="NIFTY",
            spot_px=24800.0,
            spot_zone="Near Daily S1-S2 Support",
            opt_symbol="NIFTY 24800 CE",
            opt_type="CE",
            strike=24800,
            expiry="13OCT2026",
            action="BUY",
            setup_type="BUY Reversal",
            entry_px=100.0,
            sl_px=92.0,
            t1_px=120.0,
            t2_px=135.0,
            t3_px=150.0,
            opt_piv=opt_piv,
            ll=92.0,
            hh=0.0,
            conf_5m="Bullish reclaim",
            retest_status="RETEST_CONFIRMED",
            context_result="Bullish support alignment",
            risk_reward_str="1:2.50",
            spread_check_str="Spread 0.50 pts",
            source_order_id="BROKER_ORD_123",
            trade_mode="Actual",
            qty=25,
            token="12345"
        )
        self.assertIsNotNone(trade)
        self.assertEqual(trade["Strategy_ID"], STRATEGY_ID)
        self.assertTrue(trade["Trade_ID"].startswith("ADV_NIFTY_CE_24800"))
        self.assertEqual(len(self.tracker.get_active_trades()), 1)

        # Tick update: price moves to 110 (MFE high=110, live points=+10)
        self.tracker.update_tick("NIFTY", "CE", 24800, "13OCT2026", 110.0)
        active = self.tracker.get_active_trades()[0]
        self.assertEqual(active["Current_Price"], 110.0)
        self.assertEqual(active["Trade_High"], 110.0)
        self.assertEqual(active["Points"], 10.0)
        self.assertEqual(active["Gross_PnL"], 250.0)

        # Tick update: price reaches Target 3 (150.0) -> Exits trade
        closed = self.tracker.update_tick("NIFTY", "CE", 24800, "13OCT2026", 150.0)
        self.assertIsNotNone(closed)
        self.assertEqual(closed["Status"], "CLOSED")
        self.assertIn("Target 3 Hit", closed["Exit_Reason"])
        self.assertEqual(closed["Points"], 50.0)
        self.assertEqual(closed["Gross_PnL"], 1250.0)
        self.assertGreater(closed["Charges"], 0)
        self.assertEqual(closed["Net_PnL"], round(1250.0 - closed["Charges"], 2))

        # Active list is now empty, closed has 1 trade
        self.assertEqual(len(self.tracker.get_active_trades()), 0)
        self.assertEqual(len(self.tracker.get_closed_trades()), 1)

        # Summary statistics calculation
        summary = self.tracker.get_summary_stats()
        self.assertEqual(summary["total_trades"], 1)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["win_rate"], 100.0)
        self.assertEqual(summary["total_points"], 50.0)

    # ── 8. Google Sheets Tab Setup & Idempotency ─────────────────────────────
    def test_sheets_idempotency_and_headers(self):
        mock_spreadsheet = MagicMock()
        mock_ws = MagicMock()
        mock_ws.title = ADVANCED_SHEET_TAB_NAME
        mock_ws.col_count = 44
        mock_ws.row_count = 500
        mock_ws.row_values.return_value = ADVANCED_SHEET_HEADERS
        mock_ws.col_values.return_value = ["Trade ID", "ADV_NIFTY_CE_24800_20261009_093000"]
        mock_spreadsheet.worksheets.return_value = [mock_ws]

        sheets_mgr = AdvancedGoogleSheetsManager()
        ok = sheets_mgr.setup_tab(mock_spreadsheet)
        self.assertTrue(ok)
        self.assertEqual(len(ADVANCED_SHEET_HEADERS), 45)
        self.assertIn("Trade ID", ADVANCED_SHEET_HEADERS)
        self.assertIn("Strategy ID", ADVANCED_SHEET_HEADERS)
        self.assertIn("Trade Mode (Actual / Shadow)", ADVANCED_SHEET_HEADERS)

        # Verified indexed rows
        self.assertIn("ADV_NIFTY_CE_24800_20261009_093000", sheets_mgr._trade_id_to_row)
        self.assertEqual(sheets_mgr._trade_id_to_row["ADV_NIFTY_CE_24800_20261009_093000"], 2)

    # ── 9. Pivot P Continuation Setups ───────────────────────────────────────
    def test_pivot_p_continuation(self):
        engine = AdvancedStrategyEngine()
        engine.tracker = self.tracker

        opt_piv = {"P": 100.0, "S1": 85.0, "S2": 70.0, "R1": 115.0, "R2": 130.0, "R3": 150.0, "S3": 55.0}
        daily_piv = {"P": 25000.0, "S1": 24800.0, "S2": 24600.0, "R1": 25200.0, "R2": 25400.0}

        # 1. Price dips below P (e.g. 96.0) -> sweeps below P (LL=96)
        engine.update_pivot_p_continuation(
            symbol="NIFTY", opt_type="CE", strike=25000, expiry="13OCT2026",
            ltp=96.0, spot_px=25050.0, daily_pivots=daily_piv, weekly_pivots={},
            opt_piv=opt_piv, candle_5m={"bucket": 118, "close": 96.0}
        )
        pst = engine._p_states.get(("NIFTY", "CE", 25000, "13OCT2026"))
        self.assertIsNotNone(pst)
        self.assertEqual(pst["state"], "SWEEP_BELOW_P")
        self.assertEqual(pst["ll"], 96.0)

        # 2. Price reclaims P (ltp=100.5)
        engine.update_pivot_p_continuation(
            symbol="NIFTY", opt_type="CE", strike=25000, expiry="13OCT2026",
            ltp=100.5, spot_px=25050.0, daily_pivots=daily_piv, weekly_pivots={},
            opt_piv=opt_piv, candle_5m={"bucket": 118, "close": 100.5}
        )
        self.assertEqual(pst["state"], "RECLAIM_P")

        # 3. Closed 5M candle closes bullishly above P (open=98, close=104, high=105, low=97, bucket 118)
        now_mock = datetime.now()
        curr_b = (now_mock.hour * 60 + now_mock.minute) // 5
        closed_candle = {"open": 98.0, "high": 102.5, "low": 97.0, "close": 101.5, "bucket": curr_b - 1}

        engine.update_pivot_p_continuation(
            symbol="NIFTY", opt_type="CE", strike=25000, expiry="13OCT2026",
            ltp=101.5, spot_px=25050.0, daily_pivots=daily_piv, weekly_pivots={},
            opt_piv=opt_piv, candle_5m=closed_candle
        )
        # Should have qualified a BUY Continuation at Pivot P trade!
        active = self.tracker.get_active_trades()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["Setup_Type"], "BUY Continuation at Pivot P")
        self.assertEqual(active[0]["Stop_Loss"], 96.0)
        self.assertEqual(active[0]["Target_1"], 115.0)

    # ── 10. Next-Day Plan Auto-Mapping Tests ─────────────────────────────
    def test_next_day_plan_auto_mapping(self):
        engine = AdvancedStrategyEngine()
        strikes_mock = {
            "24800_CE": {
                "symbol": "NIFTY", "opt_type": "CE", "strike": 24800, "expiry": "13OCT2026",
                "S3": 75.0, "S2": 95.0, "S1": 110.0, "P": 130.0, "R1": 150.0, "R2": 165.0, "R3": 185.0
            },
            "24800_PE": {
                "symbol": "NIFTY", "opt_type": "PE", "strike": 24800, "expiry": "13OCT2026",
                "S3": 60.0, "S2": 80.0, "S1": 95.0, "P": 115.0, "R1": 135.0, "R2": 150.0, "R3": 170.0
            }
        }
        daily_focus_mock = {
            "15M": {"all_4": [24800]},
            "1H": {"all_4": [24800]}
        }
        mtf_pivots_mock = {
            "Daily": {"P": 24900.0, "S1": 24800.0, "S2": 24700.0, "R1": 25000.0, "R2": 25100.0},
            "Weekly": {"P": 24850.0, "S1": 24650.0, "R1": 25100.0}
        }

        plan = engine.build_next_day_plan(
            symbol="NIFTY",
            expiry="13OCT2026",
            strikes_dict=strikes_mock,
            mtf_pivots=mtf_pivots_mock,
            daily_focus=daily_focus_mock,
            save_to_file=False
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["strategy_id"], STRATEGY_ID)
        self.assertEqual(plan["symbol"], "NIFTY")
        self.assertGreater(plan["setups_count"], 0)
        self.assertGreater(plan["qualified_count"], 0)

        # Check BUY Reversal setup for 24800 CE
        buy_setup = next((s for s in plan["setups"] if s["strike"] == 24800 and s["type"] == "CE" and "BUY Reversal" in s["setup"]), None)
        self.assertIsNotNone(buy_setup)
        self.assertEqual(buy_setup["planned_ep"], 95.0)
        self.assertEqual(buy_setup["sl"], 75.0)
        self.assertEqual(buy_setup["target_1"], 110.0)
        self.assertEqual(buy_setup["target_2"], 130.0)
        self.assertEqual(buy_setup["advance_status"], "QUALIFIED")

    # ── 11. Monthly Expiry Determination & Anti-Chasing Logic ────────────────
    def test_monthly_expiry_detection(self):
        expiries = ["06OCT2026", "13OCT2026", "20OCT2026", "27OCT2026", "03NOV2026"]
        m_exp = get_monthly_expiry(expiries)
        self.assertEqual(m_exp, "27OCT2026")

    def test_proximity_anti_chasing(self):
        engine = AdvancedStrategyEngine()
        engine.monthly_expiry = "27OCT2026"
        engine.focus_1h_strikes = {23100}

        # Suppose S2 is 31.77, LL is 26.35
        # If LTP ran away to 91.50 (> 8 pts above S2): MUST REJECT as runaway chasing
        candle_5m = {"open": 28.0, "high": 92.0, "low": 26.35, "close": 91.50, "bucket": 100}
        opt_piv = {"S3": 20.0, "S2": 31.77, "S1": 55.0, "P": 110.0, "R1": 150.0, "R2": 210.0, "R3": 250.0}
        
        ok, reason, trade = engine.evaluate_candidate(
            symbol="NIFTY", opt_type="PE", strike=23100, expiry="27OCT2026",
            action="BUY", setup_type="BUY Reversal", ltp=91.50, spot_px=23100.0,
            daily_pivots={"P": 23000.0, "S1": 22900.0, "S2": 22800.0, "R1": 23200.0, "R2": 23300.0},
            weekly_pivots={"P": 23000.0}, opt_piv=opt_piv, ll=26.35, hh=0.0,
            candle_5m=candle_5m, retest_done=True, retest_low=30.0, retest_high=0.0,
            sl_candidate=26.35, t1_candidate=55.0, t2_candidate=110.0
        )
        self.assertFalse(ok)
        self.assertIn("ran away", reason.lower())


if __name__ == "__main__":
    unittest.main()
