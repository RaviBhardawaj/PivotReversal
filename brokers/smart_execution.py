"""
Smart Bid/Ask Execution Engine
Calculates optimal limit prices from live L1 market depth (Bid/Ask)
to guarantee best execution with minimal slippage for option buying.
"""

from typing import Tuple, Dict, Any, Optional
from datetime import datetime


def tick_round(val: float, tick: float = 0.05) -> float:
    """Rounds price to nearest valid exchange tick (0.05)."""
    if val is None or val <= 0:
        return 0.0
    return round(round(float(val) / tick) * tick, 2)


class SmartPriceCalculator:
    """
    Computes optimal Limit Entry and Exit prices using live L1 Bid/Ask depth.
    Avoids aggressive MARKET orders while ensuring rapid fills.
    """

    def __init__(
        self,
        tight_spread_threshold_pts: float = 0.50,
        tight_spread_threshold_pct: float = 1.0,
        chase_timeout_seconds: float = 3.0,
        max_marketable_buffer_pct: float = 0.5
    ):
        self.tight_spread_threshold_pts = tight_spread_threshold_pts
        self.tight_spread_threshold_pct = tight_spread_threshold_pct
        self.chase_timeout_seconds = chase_timeout_seconds
        self.max_marketable_buffer_pct = max_marketable_buffer_pct

    def compute_buy_entry_price(
        self,
        ltp: float,
        bid: float = 0.0,
        ask: float = 0.0,
        signal_ep: float = 0.0
    ) -> Tuple[str, float, str]:
        """
        Calculates best entry price for Option Buying.
        Returns: (order_type, limit_price, execution_rationale)
        """
        ltp = float(ltp or 0.0)
        bid = float(bid or 0.0)
        ask = float(ask or 0.0)
        ref_px = ltp if ltp > 0 else (signal_ep if signal_ep > 0 else ask)

        # 1. Fallback when depth is unavailable or zero
        if ask <= 0.0 or bid <= 0.0:
            buffered_px = tick_round(ref_px * (1.0 + self.max_marketable_buffer_pct / 100.0))
            return "LIMIT", buffered_px, f"Depth unavailable: Limit @ LTP+{self.max_marketable_buffer_pct}% ({buffered_px:.2f})"

        spread_pts = round(ask - bid, 2)
        spread_pct = round((spread_pts / ask * 100.0), 2) if ask > 0 else 0.0

        # 2. Tight Spread: Hit Ask directly for instantaneous fill
        if spread_pts <= self.tight_spread_threshold_pts or spread_pct <= self.tight_spread_threshold_pct:
            return "LIMIT", tick_round(ask), f"Tight spread ({spread_pts:.2f} pts): Limit @ Best Ask ({ask:.2f})"

        # 3. Wide Spread: Quote near midpoint / above best bid to save slippage
        # Price at midpoint, rounded to tick
        midpoint = (bid + ask) / 2.0
        smart_px = tick_round(min(ask, max(bid + 0.05, midpoint)))
        return "LIMIT", smart_px, f"Wide spread ({spread_pts:.2f} pts): Smart Midpoint Limit ({smart_px:.2f}) [Bid:{bid:.2f} Ask:{ask:.2f}]"

    def compute_sell_exit_price(
        self,
        ltp: float,
        bid: float = 0.0,
        ask: float = 0.0
    ) -> Tuple[str, float, str]:
        """
        Calculates best exit price for selling the bought option.
        Returns: (order_type, limit_price, execution_rationale)
        """
        ltp = float(ltp or 0.0)
        bid = float(bid or 0.0)
        ask = float(ask or 0.0)

        if bid > 0.0:
            return "LIMIT", tick_round(bid), f"Limit @ Best Bid ({bid:.2f}) for instant exit fill"

        ref_px = ltp if ltp > 0 else ask
        buffered_px = tick_round(ref_px * (1.0 - self.max_marketable_buffer_pct / 100.0))
        return "LIMIT", buffered_px, f"Bid unavailable: Limit @ LTP-{self.max_marketable_buffer_pct}% ({buffered_px:.2f})"

    def compute_chase_modification(
        self,
        side: str,
        current_limit_px: float,
        bid: float = 0.0,
        ask: float = 0.0,
        ltp: float = 0.0
    ) -> Tuple[float, str]:
        """
        Called when limit order has not filled after chase_timeout_seconds.
        Steps price towards current best quote to guarantee fill.
        """
        bid = float(bid or 0.0)
        ask = float(ask or 0.0)
        ltp = float(ltp or 0.0)

        if side.upper() == "BUY":
            target_px = ask if ask > 0 else tick_round(ltp * 1.005)
            if target_px > current_limit_px:
                return tick_round(target_px), f"Chase BUY timeout: stepping to current Ask ({target_px:.2f})"
        else: # SELL
            target_px = bid if bid > 0 else tick_round(ltp * 0.995)
            if target_px < current_limit_px and target_px > 0:
                return tick_round(target_px), f"Chase SELL timeout: stepping to current Bid ({target_px:.2f})"

        return current_limit_px, "Price unchanged"


# Global instance
smart_execution = SmartPriceCalculator()
