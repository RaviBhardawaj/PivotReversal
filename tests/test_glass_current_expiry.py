"""Glass dashboard lists the current expiry only and keeps deleted strikes hidden."""
import os
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deleted_strikes  # noqa: E402
import glass_dashboard as gd  # noqa: E402

COLS = ("Expiry", "Type", "Strike", "Side", "LTP", "Status")


class Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class Tree:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, k):
        return COLS

    def get_children(self):
        return list(range(len(self.rows)))

    def item(self, i, what):
        return self.rows[i] if what == "values" else ("buy_row",)


def make_app():
    a = types.SimpleNamespace()
    a.tree = Tree([("17NOV2026", "PE", "23500", "🟢 BUY", "90.00", "⚪ Pending"),
                   ("17NOV2026", "CE", "🔷24000", "🟢 BUY", "150.00", "🟢 LIVE TRADE"),
                   ("24NOV2026", "CE", "24100", "🟢 BUY", "190.00", "⚪ Pending"),
                   ("29DEC2026", "CE", "24500", "🟢 BUY", "90.00", "⚪ Pending")])
    a.dash_filter_vars = {}
    a.status_lbl = types.SimpleNamespace(cget=lambda k: "#fff")
    a.exchange_var, a.symbol_var, a.sym_cb = Var("NFO"), Var("NIFTY"), {"values": ["NIFTY"]}
    a.expiry_var, a.exp_cb, a.next_exp_var = Var("17NOV2026"), {"values": []}, Var("24NOV2026")
    a.use_next_exp, a.strikes_var = Var(True), Var(10)
    a.top_start_btn = a.top_stop_btn = types.SimpleNamespace(_state="normal")
    a.status_var, a.spot_info, a.sec_15m_info, a.sec_1h_info, a.max_oi_info = (Var("") for _ in range(5))
    a.exp_cur_var, a.exp_next_var, a.show_plan_cols_var = Var(False), Var(True), Var(False)
    a.dash_filter_info, a.level_source_var, a.ft_tf_filter_var = Var(""), Var(""), Var("ALL")
    a.refreshed = 0
    a._on_dash_filter_changed = lambda: setattr(a, "refreshed", a.refreshed + 1)
    return a


def make_module():
    feed = types.SimpleNamespace(lock=threading.Lock(), spot_price={}, spot_zone={}, mtf_pivots={}, market_direction={},
                                 h1_bias={}, focus_15m={}, focus_1h={}, daily_focus={}, running=True)
    eng = types.SimpleNamespace(summary=lambda: {}, settings={})
    return types.SimpleNamespace(feed=feed, live_order_manager=types.SimpleNamespace(engine=eng),
                                 is_live_market_open=lambda: True, SPREADSHEET_ID="x", LEVEL_SOURCES={},
                                 DASH_FILTER_GROUPS=[], EXCHANGE_CONFIG={})


class CurrentExpiryOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._file = deleted_strikes.DELETED_FILE
        deleted_strikes.DELETED_FILE = os.path.join(self.tmp, "deleted_strikes.json")
        gd.launcher_pids = lambda: []
        self.app = make_app()
        self.dash = gd.GlassDashboard(self.app, make_module())
        self.dash.snap = self.dash._take_snapshot()

    def tearDown(self):
        deleted_strikes.DELETED_FILE = self._file

    def test_snapshot_forces_current_expiry_view(self):
        self.assertTrue(self.app.exp_cur_var.get())
        self.assertFalse(self.app.exp_next_var.get())
        self.assertEqual(self.app.refreshed, 1)

    def test_rows_are_current_expiry_only_with_keys(self):
        s = self.dash.live_state()
        self.assertEqual(s["current_expiry"], "17NOV2026")
        self.assertEqual([r["v"]["Expiry"] for r in s["rows"]], ["17NOV2026", "17NOV2026"])
        self.assertEqual(s["rows"][1]["k"], "NIFTY|17NOV2026|24000|CE|🟢 BUY")

    def test_expiry_filter_next_and_all(self):
        self.dash.expiry_view = "NEXT"
        self.dash.snap = self.dash._take_snapshot()
        self.assertEqual((self.app.exp_cur_var.get(), self.app.exp_next_var.get()), (False, True))
        s = self.dash.live_state()
        self.assertEqual([r["v"]["Expiry"] for r in s["rows"]], ["24NOV2026"])
        self.assertEqual((s["expiry_view"], s["next_expiry"]), ("NEXT", "24NOV2026"))
        self.dash.expiry_view = "ALL"
        self.dash.snap = self.dash._take_snapshot()
        self.assertEqual((self.app.exp_cur_var.get(), self.app.exp_next_var.get()), (False, False))
        self.assertEqual(len(self.dash.live_state()["rows"]), 4)

    def test_deleted_strike_is_hidden(self):
        deleted_strikes.delete([{"symbol": "NIFTY", "strike": "23500", "type": "PE", "expiry": "17NOV2026"}])
        s = self.dash.live_state()
        self.assertEqual([r["v"]["Strike"] for r in s["rows"]], ["🔷24000"])


if __name__ == "__main__":
    unittest.main()
