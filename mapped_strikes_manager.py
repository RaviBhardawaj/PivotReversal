"""
Mapped Strikes & Lot Size Manager
Handles user-selected strikes, custom lot sizing per strike, and logic restriction.
Persists state in mapped_strikes_config.json.
"""

import os
import json
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional

DEFAULT_CONFIG_FILE = "mapped_strikes_config.json"


class MappedStrikesManager:
    """
    Manages selection of mapped strikes and custom lot sizes.
    When 'restrict_to_selected_only' is enabled, the strategy logic,
    alerts, and order engine will operate strictly on enabled strikes.
    """

    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir
        self.config_path = os.path.join(base_dir, DEFAULT_CONFIG_FILE)
        self.lock = threading.RLock()
        self.data: Dict[str, Any] = {
            "restrict_to_selected_only": False,
            "default_lots": 1,
            "strikes": [],
            "updated_at": datetime.now().isoformat()
        }
        self.load()

    def load(self) -> Dict[str, Any]:
        with self.lock:
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    if isinstance(saved, dict):
                        self.data["restrict_to_selected_only"] = bool(saved.get("restrict_to_selected_only", False))
                        self.data["default_lots"] = max(1, int(saved.get("default_lots", 1)))
                        self.data["strikes"] = saved.get("strikes", []) or []
                        self.data["updated_at"] = saved.get("updated_at", datetime.now().isoformat())
                except Exception as e:
                    print(f"[MAPPED STRIKES] Could not load {self.config_path} ({e}); using defaults")
            return self.data

    def save(self) -> bool:
        with self.lock:
            self.data["updated_at"] = datetime.now().isoformat()
            tmp_path = self.config_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2)
                os.replace(tmp_path, self.config_path)
                return True
            except Exception as e:
                print(f"[MAPPED STRIKES] Could not save config: {e}")
                return False

    def is_restricted_mode(self) -> bool:
        with self.lock:
            return bool(self.data.get("restrict_to_selected_only", False))

    def set_restricted_mode(self, enabled: bool) -> bool:
        with self.lock:
            self.data["restrict_to_selected_only"] = bool(enabled)
            self.save()
            return self.data["restrict_to_selected_only"]

    def set_default_lots(self, lots: int) -> int:
        with self.lock:
            self.data["default_lots"] = max(1, int(lots))
            self.save()
            return self.data["default_lots"]

    def get_config(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "restrict_to_selected_only": self.data.get("restrict_to_selected_only", False),
                "default_lots": self.data.get("default_lots", 1),
                "strikes": list(self.data.get("strikes", [])),
                "updated_at": self.data.get("updated_at", "")
            }

    @staticmethod
    def make_id(symbol: str, strike: float, opt_type: str) -> str:
        sym = str(symbol or "NIFTY").strip().upper()
        ot = str(opt_type or "BOTH").strip().upper()
        try:
            stk = f"{float(strike):g}"
        except (ValueError, TypeError):
            stk = str(strike)
        return f"{sym}_{stk}_{ot}"

    def add_strike(
        self,
        symbol: str,
        strike: float,
        opt_type: str = "BOTH",
        lots: int = 1,
        source: str = "Manual",
        notes: str = "",
        enabled: bool = True
    ) -> Dict[str, Any]:
        with self.lock:
            sym = str(symbol or "NIFTY").strip().upper()
            ot = str(opt_type or "BOTH").strip().upper()
            if ot not in ("CE", "PE", "BOTH"):
                ot = "BOTH"
            try:
                stk_val = round(float(strike), 2)
            except (ValueError, TypeError):
                stk_val = 0.0

            if stk_val <= 0:
                raise ValueError("Invalid strike value")

            item_id = self.make_id(sym, stk_val, ot)
            existing = next((s for s in self.data["strikes"] if s.get("id") == item_id), None)
            
            if existing:
                existing["lots"] = max(1, int(lots))
                existing["enabled"] = bool(enabled)
                if source:
                    existing["source"] = source
                if notes:
                    existing["notes"] = notes
                item = existing
            else:
                item = {
                    "id": item_id,
                    "symbol": sym,
                    "strike": stk_val,
                    "opt_type": ot,
                    "lots": max(1, int(lots)),
                    "enabled": bool(enabled),
                    "source": source or "Manual",
                    "notes": notes or f"Mapped {sym} {stk_val:g} {ot}",
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                self.data["strikes"].append(item)

            self.data["strikes"].sort(key=lambda x: (x.get("symbol", ""), float(x.get("strike", 0)), x.get("opt_type", "")))
            self.save()
            return item

    def update_strike(self, item_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.lock:
            for item in self.data["strikes"]:
                if item.get("id") == item_id:
                    if "lots" in updates:
                        item["lots"] = max(1, int(updates["lots"]))
                    if "enabled" in updates:
                        item["enabled"] = bool(updates["enabled"])
                    if "notes" in updates:
                        item["notes"] = str(updates["notes"])
                    if "source" in updates:
                        item["source"] = str(updates["source"])
                    self.save()
                    return item
            return None

    def delete_strike(self, item_id: str) -> bool:
        with self.lock:
            initial_len = len(self.data["strikes"])
            self.data["strikes"] = [s for s in self.data["strikes"] if s.get("id") != item_id]
            if len(self.data["strikes"]) < initial_len:
                self.save()
                return True
            return False

    def clear_strikes(self) -> int:
        with self.lock:
            count = len(self.data["strikes"])
            self.data["strikes"] = []
            self.save()
            return count

    def is_strike_active(self, symbol: str, strike: float, opt_type: str = "") -> bool:
        """
        Checks whether logic/trading is active for this strike.
        If restricted mode is OFF, returns True.
        If restricted mode is ON, returns True only if explicitly in enabled strikes.
        """
        with self.lock:
            if not self.data.get("restrict_to_selected_only", False):
                return True
            
            sym = str(symbol or "").strip().upper()
            ot = str(opt_type or "").strip().upper()
            try:
                stk_val = round(float(strike), 2)
            except (ValueError, TypeError):
                return False

            for item in self.data.get("strikes", []):
                if not item.get("enabled", True):
                    continue
                if sym and item.get("symbol", "").upper() != sym:
                    continue
                try:
                    i_stk = round(float(item.get("strike", 0)), 2)
                except (ValueError, TypeError):
                    continue
                if i_stk != stk_val:
                    continue
                
                i_ot = str(item.get("opt_type", "BOTH")).upper()
                if not ot or i_ot == "BOTH" or i_ot == ot:
                    return True

            return False

    def get_lots_for(self, symbol: str, strike: float, opt_type: str = "", fallback_lots: int = 1) -> int:
        """
        Returns custom configured lot size for this specific strike,
        or fallback_lots if not specifically configured.
        """
        with self.lock:
            sym = str(symbol or "").strip().upper()
            ot = str(opt_type or "").strip().upper()
            try:
                stk_val = round(float(strike), 2)
            except (ValueError, TypeError):
                return fallback_lots

            for item in self.data.get("strikes", []):
                if sym and item.get("symbol", "").upper() != sym:
                    continue
                try:
                    i_stk = round(float(item.get("strike", 0)), 2)
                except (ValueError, TypeError):
                    continue
                if i_stk != stk_val:
                    continue

                i_ot = str(item.get("opt_type", "BOTH")).upper()
                if not ot or i_ot == "BOTH" or i_ot == ot:
                    return max(1, int(item.get("lots", fallback_lots)))

            return fallback_lots

    def import_from_focus(self, focus_15m: dict, focus_1h: dict, symbol: str = "NIFTY", default_lots: int = 1) -> int:
        """
        Auto-populates mapped strikes from today's calculated 15M & 1H focus levels.
        """
        with self.lock:
            count = 0
            sym = str(symbol or "NIFTY").strip().upper()
            lots = max(1, int(default_lots or self.data.get("default_lots", 1)))

            # 1. 15M levels
            if focus_15m:
                ce1, ce2 = focus_15m.get("CE1"), focus_15m.get("CE2")
                pe1, pe2 = focus_15m.get("PE1"), focus_15m.get("PE2")
                p_stk = focus_15m.get("P_STRIKE")

                if ce1:
                    self.add_strike(sym, ce1, "CE", lots, source="15M:R1", notes="15M Intraday R1 Reclaim")
                    count += 1
                if ce2:
                    self.add_strike(sym, ce2, "CE", lots, source="15M:R2", notes="15M Intraday R2 Reclaim")
                    count += 1
                if pe1:
                    self.add_strike(sym, pe1, "PE", lots, source="15M:S1", notes="15M Intraday S1 Reclaim")
                    count += 1
                if pe2:
                    self.add_strike(sym, pe2, "PE", lots, source="15M:S2", notes="15M Intraday S2 Reclaim")
                    count += 1
                if p_stk:
                    self.add_strike(sym, p_stk, "BOTH", lots, source="15M:P", notes="15M Intraday Pivot Zone")
                    count += 1

            # 2. 1H levels
            if focus_1h:
                h_ce1, h_ce2 = focus_1h.get("CE1"), focus_1h.get("CE2")
                h_pe1, h_pe2 = focus_1h.get("PE1"), focus_1h.get("PE2")
                h_p = focus_1h.get("P_STRIKE")

                if h_ce1:
                    self.add_strike(sym, h_ce1, "CE", lots, source="1H:R1", notes="1H Structure R1 Reclaim")
                    count += 1
                if h_ce2:
                    self.add_strike(sym, h_ce2, "CE", lots, source="1H:R2", notes="1H Structure R2 Reclaim")
                    count += 1
                if h_pe1:
                    self.add_strike(sym, h_pe1, "PE", lots, source="1H:S1", notes="1H Structure S1 Reclaim")
                    count += 1
                if h_pe2:
                    self.add_strike(sym, h_pe2, "PE", lots, source="1H:S2", notes="1H Structure S2 Reclaim")
                    count += 1
                if h_p:
                    self.add_strike(sym, h_p, "BOTH", lots, source="1H:P", notes="1H Structure Pivot Zone")
                    count += 1

            return count


# Singleton instance
mapped_strikes_mgr = MappedStrikesManager()
