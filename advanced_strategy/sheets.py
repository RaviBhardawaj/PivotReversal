"""
Google Sheets Integration for Advanced Strategy
Manages the dedicated 'Advanced Strategy' tab with 44 required columns,
idempotent row tracking, batch updates, and quota backoff handling.
"""

import time
import threading
from typing import Dict, List, Any, Optional

from .config import (
    ADVANCED_SHEET_TAB_NAME,
    ADVANCED_SHEET_HEADERS,
)


def col_to_letter(col_idx: int) -> str:
    """Converts 1-based column index to Excel column letter (e.g. 1->A, 26->Z, 27->AA, 45->AS)."""
    result = ""
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


class AdvancedGoogleSheetsManager:
    """
    Manages synchronization of Advanced Strategy trades to the 'Advanced Strategy' Google Sheet tab.
    Ensures idempotent updates, avoiding duplicate rows on live price ticks or re-entries.
    """

    def __init__(self, spreadsheet_getter=None):
        self.spreadsheet_getter = spreadsheet_getter
        self.ws = None
        self._trade_id_to_row: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._last_sync_ts = 0.0
        self._quota_backoff_until = 0.0
        self._is_syncing = False

    def setup_tab(self, spreadsheet) -> bool:
        """
        Creates or retrieves the 'Advanced Strategy' tab without touching other tabs.
        Initializes headers and styling if newly created.
        """
        if not spreadsheet:
            return False

        try:
            ws = None
            for s in spreadsheet.worksheets():
                if s.title == ADVANCED_SHEET_TAB_NAME:
                    ws = s
                    break

            if not ws:
                ws = spreadsheet.add_worksheet(
                    title=ADVANCED_SHEET_TAB_NAME,
                    rows=1000,
                    cols=len(ADVANCED_SHEET_HEADERS)
                )
                print(f"[ADV SHEET] Created new worksheet '{ADVANCED_SHEET_TAB_NAME}'")
            elif ws.col_count < len(ADVANCED_SHEET_HEADERS):
                try:
                    ws.add_cols(len(ADVANCED_SHEET_HEADERS) - ws.col_count)
                except Exception:
                    pass

            self.ws = ws

            # Check if row 1 headers need to be written
            existing_headers = []
            try:
                existing_headers = ws.row_values(1)
            except Exception:
                pass

            end_col = col_to_letter(len(ADVANCED_SHEET_HEADERS))
            if not existing_headers or existing_headers != ADVANCED_SHEET_HEADERS:
                ws.update(values=[ADVANCED_SHEET_HEADERS], range_name=f"A1:{end_col}1")
                # Format headers
                try:
                    from gspread_formatting import format_cell_ranges, CellFormat, Color, TextFormat, set_frozen
                    set_frozen(ws, rows=1)
                    header_format = CellFormat(
                        backgroundColor=Color(0.08, 0.15, 0.25),  # Deep Navy
                        textFormat=TextFormat(foregroundColor=Color(1, 0.85, 0.2), bold=True, fontSize=10),
                        horizontalAlignment="CENTER"
                    )
                    format_cell_ranges(ws, [(f"A1:{end_col}1", header_format)])
                except Exception as fe:
                    print(f"[ADV SHEET] Formatting note: {fe}")

            # Index existing rows by Trade ID (Column E = index 5)
            self._index_existing_rows()
            print(f"✅ [ADV SHEET] Tab '{ADVANCED_SHEET_TAB_NAME}' ready ({len(self._trade_id_to_row)} trades indexed)")
            return True

        except Exception as e:
            print(f"[ADV SHEET] Tab setup error: {e}")
            return False

    def _index_existing_rows(self):
        """Builds in-memory index of Trade_ID -> sheet row number to guarantee idempotency."""
        if not self.ws:
            return
        try:
            # Column E is Trade ID (index 5)
            col_e = self.ws.col_values(5)
            self._trade_id_to_row.clear()
            for idx, val in enumerate(col_e, start=1):
                val_str = str(val).strip()
                if val_str and val_str != "Trade ID" and val_str.startswith("ADV_"):
                    self._trade_id_to_row[val_str] = idx
        except Exception as e:
            print(f"[ADV SHEET] Indexing error: {e}")

    def sync_trades_async(self, trades_to_sync: List[Dict[str, Any]]):
        """Launches background thread for non-blocking sheet synchronization."""
        if not self.ws or not trades_to_sync:
            return

        now_ts = time.time()
        if now_ts < self._quota_backoff_until:
            return  # in rate limit backoff

        if self._is_syncing:
            return  # prior sync still running

        threading.Thread(
            target=self._sync_trades_bg,
            args=(list(trades_to_sync),),
            daemon=True
        ).start()

    def _sync_trades_bg(self, trades: List[Dict[str, Any]]):
        if not self._lock.acquire(blocking=False):
            return

        self._is_syncing = True
        try:
            self._do_sync(trades)
        finally:
            self._is_syncing = False
            self._lock.release()

    def _do_sync(self, trades: List[Dict[str, Any]]):
        """Batch-updates or appends trade rows to the sheet."""
        if not self.ws or not trades:
            return

        try:
            rows_to_update = []  # [(range_name, [values])]
            rows_to_append = []  # [[values]]

            # Find current sheet length
            next_row = max(self._trade_id_to_row.values()) + 1 if self._trade_id_to_row else 2

            end_col = col_to_letter(len(ADVANCED_SHEET_HEADERS))
            for t in trades:
                tid = t.get("Trade_ID", "")
                if not tid:
                    continue

                row_vals = self._format_trade_row(t)
                if tid in self._trade_id_to_row:
                    row_num = self._trade_id_to_row[tid]
                    range_name = f"A{row_num}:{end_col}{row_num}"
                    rows_to_update.append({"range": range_name, "values": [row_vals]})
                else:
                    self._trade_id_to_row[tid] = next_row
                    rows_to_append.append(row_vals)
                    next_row += 1

            # Make sure worksheet has enough rows
            if next_row > self.ws.row_count:
                try:
                    self.ws.add_rows(next_row + 100 - self.ws.row_count)
                except Exception:
                    pass

            # 1. Update existing rows (batch)
            if rows_to_update:
                self.ws.batch_update(rows_to_update)

            # 2. Append new rows (batch)
            if rows_to_append:
                start_row = next_row - len(rows_to_append)
                end_row = next_row - 1
                append_range = f"A{start_row}:{end_col}{end_row}"
                self.ws.update(values=rows_to_append, range_name=append_range)

            self._last_sync_ts = time.time()

        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "quota" in err_str:
                self._quota_backoff_until = time.time() + 60.0  # backoff 60s
                print(f"[ADV SHEET] Google Sheets API 429 quota reached - backing off for 60s")
            else:
                print(f"[ADV SHEET] Sync update error: {e}")

    @staticmethod
    def _format_trade_row(t: Dict[str, Any]) -> List[Any]:
        """Converts trade dict into 45 column values."""
        pts = t.get("Points", 0.0)
        net_pnl = t.get("Net_PnL", 0.0)
        status = t.get("Status", "ACTIVE")

        return [
            t.get("Date", ""),
            t.get("Entry_Time", ""),
            t.get("Exit_Time", ""),
            t.get("Strategy_ID", "ADVANCED_PIVOT_REVERSAL"),
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
            net_pnl,
            pts,
            status,
            t.get("Context_Filter_Result", ""),
            t.get("Risk/Reward", ""),
            t.get("Spread_Check", ""),
            t.get("Rejection_Reason", ""),
            t.get("Source_Order_ID", ""),
            t.get("Trade_Mode", "Shadow"),
        ]
