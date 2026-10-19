"""
Advanced Strategy Trade Tracker
Maintains independent trade lifecycle, P&L calculations, and audit history
for STRATEGY_ID = "ADVANCED_PIVOT_REVERSAL".
"""

import os
import csv
import json
import time
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Any, Tuple

from .config import (
    STRATEGY_ID,
    ADVANCED_CSV_FILE,
    ADVANCED_REJECTIONS_FILE,
    ADVANCED_STATE_FILE,
    ADVANCED_SHEET_HEADERS,
    ADVANCED_EST_CHARGE_PCT,
    ADVANCED_EST_FLAT_BROKERAGE,
    EOD_SQUARE_OFF_TIME,
)


def _safe_float(val, default=0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


class AdvancedStrategyTracker:
    """
    Independently tracks trades qualified by the Advanced Version.
    Distinguishes between Actual (real broker filled) and Shadow (simulated) executions.
    Calculates Gross P&L, Estimated Charges, Net P&L, Points, and MFE.
    """

    def __init__(
        self,
        csv_file: str = ADVANCED_CSV_FILE,
        state_file: str = ADVANCED_STATE_FILE,
        rejections_file: str = ADVANCED_REJECTIONS_FILE
    ):
        self.csv_file = csv_file
        self.state_file = state_file
        self.rejections_file = rejections_file

        self.active_trades: Dict[str, Dict[str, Any]] = {}  # trade_id -> trade dict
        self._active_by_key: Dict[Tuple, str] = {}         # (symbol, opt_type, strike, expiry, action) -> trade_id
        self._closed_today: List[Dict[str, Any]] = []
        self._rejections_today: List[Dict[str, Any]] = []
        self._sl_hit_count: Dict[Tuple, int] = {}
        self._closed_keys_today: set = set()

        self._init_csv()
        self._restore_snapshot()

    def _trade_key(self, symbol: str, opt_type: str, strike: int, expiry: str, action: str) -> Tuple:
        return (symbol.upper(), opt_type.upper(), int(strike), str(expiry).upper(), action.upper())

    def _init_csv(self):
        """Initialize CSV files with headers if they don't exist."""
        if not os.path.exists(self.csv_file):
            try:
                with open(self.csv_file, "w", newline="") as f:
                    csv.writer(f).writerow(ADVANCED_SHEET_HEADERS)
            except Exception as e:
                print(f"[ADV TRACKER] CSV init error: {e}")

        if not os.path.exists(self.rejections_file):
            try:
                with open(self.rejections_file, "w", newline="") as f:
                    csv.writer(f).writerow([
                        "Timestamp", "Symbol", "Option_Type", "Strike", "Expiry",
                        "Action", "Setup_Type", "LTP", "Rejection_Reason", "Filter_Details"
                    ])
            except Exception as e:
                print(f"[ADV TRACKER] Rejections CSV init error: {e}")

    def _save_snapshot(self):
        """Atomic snapshot of active trades and daily state for restart resilience."""
        try:
            data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "active": list(self.active_trades.values()),
                "closed_today": self._closed_today,
                "sl_counts": [[list(k), v] for k, v in self._sl_hit_count.items()],
                "closed_keys": [list(k) for k in self._closed_keys_today]
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[ADV TRACKER] Snapshot save error: {e}")

    def _restore_snapshot(self):
        """Restores open trades from today after an app restart."""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            today_str = datetime.now().strftime("%Y-%m-%d")
            if d.get("date") != today_str:
                return

            self._closed_today = d.get("closed_today", [])
            for k_list, count in d.get("sl_counts", []):
                self._sl_hit_count[tuple(k_list)] = count
            for k_list in d.get("closed_keys", []):
                self._closed_keys_today.add(tuple(k_list))

            now_time = datetime.now().time()
            for t in d.get("active", []):
                if t.get("Status") != "ACTIVE":
                    continue
                # If app restarted after 3 PM, close remaining open trades
                if now_time >= EOD_SQUARE_OFF_TIME:
                    t["Exit_Price"] = t.get("Current_Price", t.get("Entry_Price", 0.0))
                    t["Exit_Time"] = "15:00:00"
                    t["Exit_Reason"] = "EOD 3PM Square-off (restart after 3 PM)"
                    t["Status"] = "CLOSED"
                    self._calculate_final_pnl(t)
                    self._closed_today.append(t)
                    self._update_csv(t)
                else:
                    tid = t["Trade_ID"]
                    self.active_trades[tid] = t
                    t_key = self._trade_key(
                        t.get("Underlying_Symbol", ""),
                        t.get("Option_Type", ""),
                        t.get("Strike", 0),
                        t.get("Expiry", ""),
                        t.get("BUY/SELL", "")
                    )
                    self._active_by_key[t_key] = tid
                    print(f"♻️ [ADV TRACKER] Restored active {t.get('BUY/SELL')} {t.get('Option_Type')} {t.get('Strike')} @ {t.get('Entry_Price')}")
        except Exception as e:
            print(f"[ADV TRACKER] Restore snapshot error: {e}")

    def log_rejection(
        self,
        symbol: str,
        opt_type: str,
        strike: int,
        expiry: str,
        action: str,
        setup_type: str,
        ltp: float,
        reason: str,
        details: Dict[str, Any] = None
    ):
        """Record candidate rejection for auditing and dashboard inspection."""
        entry = {
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Symbol": symbol,
            "Option_Type": opt_type,
            "Strike": strike,
            "Expiry": expiry,
            "Action": action,
            "Setup_Type": setup_type,
            "LTP": round(ltp, 2),
            "Rejection_Reason": reason,
            "Filter_Details": json.dumps(details or {}, default=str)
        }
        self._rejections_today.append(entry)
        try:
            with open(self.rejections_file, "a", newline="") as f:
                csv.writer(f).writerow([
                    entry["Timestamp"], entry["Symbol"], entry["Option_Type"],
                    entry["Strike"], entry["Expiry"], entry["Action"],
                    entry["Setup_Type"], entry["LTP"], entry["Rejection_Reason"],
                    entry["Filter_Details"]
                ])
        except Exception as e:
            print(f"[ADV TRACKER] Rejection log write error: {e}")

    def log_qualified_trade(
        self,
        symbol: str,
        spot_px: float,
        spot_zone: str,
        opt_symbol: str,
        opt_type: str,
        strike: int,
        expiry: str,
        action: str,
        setup_type: str,
        entry_px: float,
        sl_px: float,
        t1_px: float,
        t2_px: float = 0.0,
        t3_px: float = 0.0,
        opt_piv: Dict[str, float] = None,
        ll: float = 0.0,
        hh: float = 0.0,
        conf_5m: str = "",
        retest_status: str = "CONFIRMED",
        context_result: str = "",
        risk_reward_str: str = "",
        spread_check_str: str = "",
        source_order_id: str = "",
        trade_mode: str = "Shadow",
        qty: int = 25,
        token: str = "",
        entry_reason: str = ""
    ) -> Optional[Dict[str, Any]]:
        """
        Record a newly qualified Advanced Version trade.
        """
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        t_key = self._trade_key(symbol, opt_type, strike, expiry, action)
        if t_key in self._active_by_key:
            return None  # already active

        piv = opt_piv or {}
        exp_tag = f"_{expiry}" if expiry else ""
        trade_id = f"ADV_{symbol}_{opt_type}_{strike}{exp_tag}_{now.strftime('%Y%m%d_%H%M%S')}"

        if not entry_reason:
            if action == "BUY":
                swp = f"Sweep LL:{ll:.2f}" if ll > 0 else "Support Dip"
                ret = f" | Retest:{retest_status}" if retest_status else ""
                ctx = f" | Context:{context_result}" if context_result else ""
                entry_reason = f"★ {setup_type} [{swp} | Reclaimed @ {entry_px:.2f} | 5M Bullish Candle{ctx}{ret}]"
            else:
                swp = f"Sweep HH:{hh:.2f}" if hh > 0 else "Resistance Test"
                ret = f" | Retest:{retest_status}" if retest_status else ""
                ctx = f" | Context:{context_result}" if context_result else ""
                entry_reason = f"▼ {setup_type} [{swp} | Rejected @ {entry_px:.2f} | 5M Bearish Candle{ctx}{ret}]"

        trade = {
            "Date": date_str,
            "Entry_Time": time_str,
            "Exit_Time": "",
            "Strategy_ID": STRATEGY_ID,
            "Trade_ID": trade_id,
            "Underlying_Symbol": symbol,
            "Underlying_Price": round(spot_px, 2),
            "Underlying_Zone": spot_zone,
            "Setup_Type": setup_type,
            "Entry_Reason": entry_reason,
            "Option_Symbol": opt_symbol or f"{symbol} {strike} {opt_type}",
            "Option_Type": opt_type,
            "Strike": strike,
            "Expiry": expiry,
            "BUY/SELL": action,
            "Quantity": qty,
            "Entry_Price": round(entry_px, 2),
            "Option_P": round(piv.get("P", 0.0), 2),
            "Option_R1": round(piv.get("R1", 0.0), 2),
            "Option_R2": round(piv.get("R2", 0.0), 2),
            "Option_S1": round(piv.get("S1", 0.0), 2),
            "Option_S2": round(piv.get("S2", 0.0), 2),
            "Option_R3": round(piv.get("R3", 0.0), 2),
            "Option_S3": round(piv.get("S3", 0.0), 2),
            "Liquidity_LL": round(ll, 2),
            "Liquidity_HH": round(hh, 2),
            "5M_Confirmation": conf_5m,
            "Retest_Status": retest_status,
            "Stop_Loss": round(sl_px, 2),
            "Target_1": round(t1_px, 2),
            "Target_2": round(t2_px, 2),
            "Target_3": round(t3_px, 2),
            "Exit_Price": "",
            "Exit_Reason": "",
            "Gross_PnL": 0.0,
            "Charges": 0.0,
            "Net_PnL": 0.0,
            "Points": 0.0,
            "Status": "ACTIVE",
            "Context_Filter_Result": context_result,
            "Risk/Reward": risk_reward_str,
            "Spread_Check": spread_check_str,
            "Rejection_Reason": "",
            "Source_Order_ID": source_order_id,
            "Trade_Mode": trade_mode,
            # Internal tracking fields
            "Current_Price": round(entry_px, 2),
            "Trade_High": round(entry_px, 2),
            "Trade_Low": round(entry_px, 2),
            "Max_Pts": 0.0,
            "Token": token,
            "Target_Hit": 0,
        }

        self.active_trades[trade_id] = trade
        self._active_by_key[t_key] = trade_id

        self._append_csv(trade)
        self._save_snapshot()

        print(
            f"🎯 [ADV STRATEGY] QUALIFIED {trade_mode.upper()} {action} {symbol} {strike} {opt_type} @ {entry_px:.2f} "
            f"| SL:{sl_px:.2f} | T1:{t1_px:.2f} | RR:{risk_reward_str} | Setup:{setup_type}"
        )
        return trade

    def update_tick(
        self,
        symbol: str,
        opt_type: str,
        strike: int,
        expiry: str,
        ltp: float,
        broker_forced_exit: Tuple[float, str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Called on every market tick for the strike.
        Updates live P&L, MFE (Max points), and evaluates exits.
        Returns closed trade dict if an exit triggered, otherwise None.
        """
        if ltp <= 0:
            return None

        # Check for matching active trade
        matching_trade = None
        for t in self.active_trades.values():
            if (
                t["Underlying_Symbol"] == symbol
                and t["Option_Type"] == opt_type
                and int(t["Strike"]) == int(strike)
                and (not expiry or not t.get("Expiry") or t.get("Expiry") == expiry)
            ):
                matching_trade = t
                break

        if not matching_trade:
            return None

        t = matching_trade
        action = t["BUY/SELL"]
        entry = _safe_float(t["Entry_Price"])
        sl = _safe_float(t["Stop_Loss"])
        t1 = _safe_float(t["Target_1"])
        t2 = _safe_float(t["Target_2"])
        t3 = _safe_float(t["Target_3"])

        # Track Trade High & Low
        t["Current_Price"] = round(ltp, 2)
        if ltp > _safe_float(t.get("Trade_High", entry)):
            t["Trade_High"] = round(ltp, 2)
        if _safe_float(t.get("Trade_Low", entry)) <= 0 or ltp < _safe_float(t.get("Trade_Low", entry)):
            t["Trade_Low"] = round(ltp, 2)

        # MFE points calculation
        if action == "BUY":
            t["Max_Pts"] = round(max(0.0, _safe_float(t["Trade_High"]) - entry), 2)
            pts = round(ltp - entry, 2)
        else: # SELL
            t["Max_Pts"] = round(max(0.0, entry - _safe_float(t["Trade_Low"])), 2)
            pts = round(entry - ltp, 2)

        t["Points"] = pts
        t["Gross_PnL"] = round(pts * t["Quantity"], 2)

        # Check Exit conditions
        exit_triggered = False
        exit_reason = ""
        exit_px = ltp

        if broker_forced_exit and broker_forced_exit[0] > 0:
            exit_triggered = True
            exit_px = broker_forced_exit[0]
            exit_reason = broker_forced_exit[1]
        elif action == "BUY":
            if sl > 0 and ltp <= sl:
                exit_triggered = True
                ll_v = _safe_float(t.get("Liquidity_LL", 0))
                exit_reason = f"🛑 Stop Loss Hit @ {ltp:.2f} (SL:{sl:.2f} | LL:{ll_v:.2f})"
            elif t3 > 0 and ltp >= t3:
                exit_triggered = True
                exit_reason = f"🎯 Target 3 Hit @ {ltp:.2f} (T3:{t3:.2f})"
            elif t2 > 0 and ltp >= t2 and t.get("Target_Hit", 0) < 2:
                t["Target_Hit"] = 2
            elif t1 > 0 and ltp >= t1 and t.get("Target_Hit", 0) < 1:
                t["Target_Hit"] = 1
        else: # SELL
            if sl > 0 and ltp >= sl:
                exit_triggered = True
                hh_v = _safe_float(t.get("Liquidity_HH", 0))
                exit_reason = f"🛑 Stop Loss Hit @ {ltp:.2f} (SL:{sl:.2f} | HH:{hh_v:.2f})"
            elif t3 > 0 and ltp <= t3:
                exit_triggered = True
                exit_reason = f"🎯 Target 3 Hit @ {ltp:.2f} (T3:{t3:.2f})"
            elif t2 > 0 and ltp <= t2 and t.get("Target_Hit", 0) < 2:
                t["Target_Hit"] = 2
            elif t1 > 0 and ltp <= t1 and t.get("Target_Hit", 0) < 1:
                t["Target_Hit"] = 1

        if exit_triggered:
            return self._close_trade(t, exit_px, exit_reason)

        return None

    def eod_square_off(self, reason: str = "EOD 3PM Square-off @ CMP") -> List[Dict[str, Any]]:
        """Closes all open Advanced Version trades at current market price."""
        closed_list = []
        for tid in list(self.active_trades.keys()):
            t = self.active_trades[tid]
            px = t.get("Current_Price") or t.get("Entry_Price")
            closed = self._close_trade(t, px, reason)
            if closed:
                closed_list.append(closed)
        return closed_list

    def _close_trade(self, trade: Dict[str, Any], exit_px: float, reason: str) -> Dict[str, Any]:
        """Calculates final P&L, marks CLOSED, updates CSV, and removes from active dict."""
        tid = trade["Trade_ID"]
        now = datetime.now()
        trade["Exit_Time"] = now.strftime("%H:%M:%S")
        trade["Exit_Price"] = round(exit_px, 2)
        trade["Exit_Reason"] = reason
        trade["Status"] = "CLOSED"

        self._calculate_final_pnl(trade)

        t_key = self._trade_key(
            trade["Underlying_Symbol"], trade["Option_Type"],
            trade["Strike"], trade["Expiry"], trade["BUY/SELL"]
        )
        self.active_trades.pop(tid, None)
        self._active_by_key.pop(t_key, None)

        if "SL" in reason:
            self._sl_hit_count[t_key] = self._sl_hit_count.get(t_key, 0) + 1
        self._closed_keys_today.add(t_key)
        self._closed_today.append(dict(trade))

        self._update_csv(trade)
        self._save_snapshot()

        icon = "🟢" if trade["Net_PnL"] >= 0 else "🔴"
        print(
            f"{icon} [ADV STRATEGY] CLOSED {trade['BUY/SELL']} {trade['Underlying_Symbol']} {trade['Strike']} {trade['Option_Type']} "
            f"| {reason} @ {exit_px:.2f} | Points: {trade['Points']:+.2f} | Net PnL: Rs {trade['Net_PnL']:+.2f}"
        )
        return trade

    def _calculate_final_pnl(self, trade: Dict[str, Any]):
        """Calculates realized gross P&L, estimated charges, net P&L, and points."""
        entry = _safe_float(trade["Entry_Price"])
        exit_px = _safe_float(trade["Exit_Price"], entry)
        action = trade["BUY/SELL"]
        qty = int(trade.get("Quantity", 25))

        if action == "BUY":
            pts = round(exit_px - entry, 2)
        else:
            pts = round(entry - exit_px, 2)

        gross = round(pts * qty, 2)
        turnover = round((entry + exit_px) * qty, 2)
        charges = round((turnover * ADVANCED_EST_CHARGE_PCT / 100.0) + ADVANCED_EST_FLAT_BROKERAGE, 2)
        net = round(gross - charges, 2)

        trade["Points"] = pts
        trade["Gross_PnL"] = gross
        trade["Charges"] = charges
        trade["Net_PnL"] = net

    def _to_csv_row(self, t: Dict[str, Any]) -> List[Any]:
        """Maps internal dict to 44 Google Sheet / CSV columns."""
        return [
            t.get("Date", ""),
            t.get("Entry_Time", ""),
            t.get("Exit_Time", ""),
            t.get("Strategy_ID", STRATEGY_ID),
            t.get("Trade_ID", ""),
            t.get("Underlying_Symbol", ""),
            t.get("Underlying_Price", ""),
            t.get("Underlying_Zone", ""),
            t.get("Setup_Type", ""),
            t.get("Entry_Reason", ""),
            t.get("Option_Symbol", ""),
            t.get("Option_Type", ""),
            t.get("Strike", ""),
            t.get("Expiry", ""),
            t.get("BUY/SELL", ""),
            t.get("Quantity", 25),
            t.get("Entry_Price", ""),
            t.get("Option_P", ""),
            t.get("Option_R1", ""),
            t.get("Option_R2", ""),
            t.get("Option_S1", ""),
            t.get("Option_S2", ""),
            t.get("Option_R3", ""),
            t.get("Option_S3", ""),
            t.get("Liquidity_LL", ""),
            t.get("Liquidity_HH", ""),
            t.get("5M_Confirmation", ""),
            t.get("Retest_Status", ""),
            t.get("Stop_Loss", ""),
            t.get("Target_1", ""),
            t.get("Target_2", ""),
            t.get("Target_3", ""),
            t.get("Exit_Price", ""),
            t.get("Exit_Reason", ""),
            t.get("Gross_PnL", 0.0),
            t.get("Charges", 0.0),
            t.get("Net_PnL", 0.0),
            t.get("Points", 0.0),
            t.get("Status", ""),
            t.get("Context_Filter_Result", ""),
            t.get("Risk/Reward", ""),
            t.get("Spread_Check", ""),
            t.get("Rejection_Reason", ""),
            t.get("Source_Order_ID", ""),
            t.get("Trade_Mode", "Shadow"),
        ]

    def _append_csv(self, trade: Dict[str, Any]):
        try:
            with open(self.csv_file, "a", newline="") as f:
                csv.writer(f).writerow(self._to_csv_row(trade))
        except Exception as e:
            print(f"[ADV TRACKER] Append CSV error: {e}")

    def _update_csv(self, trade: Dict[str, Any]):
        """Idempotently updates the existing row in CSV by Trade_ID."""
        try:
            if not os.path.exists(self.csv_file):
                return
            with open(self.csv_file, "r", newline="") as f:
                rows = list(csv.reader(f))
            if not rows:
                return

            header = rows[0]
            tid = trade["Trade_ID"]
            found = False
            for i in range(1, len(rows)):
                if len(rows[i]) > 4 and rows[i][4] == tid:
                    rows[i] = [str(x) for x in self._to_csv_row(trade)]
                    found = True
                    break

            if not found:
                rows.append([str(x) for x in self._to_csv_row(trade)])

            with open(self.csv_file, "w", newline="") as f:
                csv.writer(f).writerows(rows)
        except Exception as e:
            print(f"[ADV TRACKER] Update CSV error: {e}")

    def get_active_trades(self) -> List[Dict[str, Any]]:
        return list(self.active_trades.values())

    def get_closed_trades(self) -> List[Dict[str, Any]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        return [t for t in self._closed_today if t.get("Date") == today_str]

    def get_all_closed_trades(self) -> List[Dict[str, Any]]:
        return list(self._closed_today)

    def get_rejections(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._rejections_today[-limit:]

    def get_summary_stats(self) -> Dict[str, Any]:
        """Computes win/loss, points, P&L for Advanced Version."""
        all_closed = self.get_all_closed_trades()
        total_trades = len(all_closed)
        wins = [t for t in all_closed if _safe_float(t.get("Points", 0)) > 0]
        losses = [t for t in all_closed if _safe_float(t.get("Points", 0)) < 0]
        win_rate = round((len(wins) / total_trades * 100.0), 1) if total_trades > 0 else 0.0

        total_pts = round(sum(_safe_float(t.get("Points", 0)) for t in all_closed), 2)
        total_gross = round(sum(_safe_float(t.get("Gross_PnL", 0)) for t in all_closed), 2)
        total_charges = round(sum(_safe_float(t.get("Charges", 0)) for t in all_closed), 2)
        total_net = round(sum(_safe_float(t.get("Net_PnL", 0)) for t in all_closed), 2)

        open_pts = round(sum(_safe_float(t.get("Points", 0)) for t in self.active_trades.values()), 2)
        open_gross = round(sum(_safe_float(t.get("Gross_PnL", 0)) for t in self.active_trades.values()), 2)

        return {
            "strategy_id": STRATEGY_ID,
            "total_trades": total_trades,
            "active_trades": len(self.active_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_points": total_pts,
            "total_gross_pnl": total_gross,
            "total_charges": total_charges,
            "total_net_pnl": total_net,
            "open_points": open_pts,
            "open_gross_pnl": open_gross,
            "rejections_count": len(self._rejections_today)
        }
