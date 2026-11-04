"""Daily orders for the F&O symbols other than NIFTY (fno_planner.py).
Run: python3 tests/test_fno_orders.py"""
import os, sys, json, tempfile, types
from datetime import datetime, date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pandas as pd
import fno_planner as fp

tmp = tempfile.mkdtemp()
fp.FNO_ORDERS_FILE = os.path.join(tmp, "fno_orders.json")
fp.DATA_SETTINGS_FILE = os.path.join(tmp, "data_settings.json")
fp.BASE_DIR = tmp
os.environ.pop("FNO_PLAN_SYMBOLS", None)

CUR, NXT, FAR = "06NOV2026", "13NOV2026", "20NOV2026"

# A. expiry choice: current only by default
assert fp.pick_expiries([CUR, NXT, FAR]) == [CUR]
assert fp.pick_expiries([CUR, NXT, FAR], "current") == [CUR]
assert fp.pick_expiries([CUR, NXT, FAR], "NEXT") == [NXT]
assert fp.pick_expiries([CUR, NXT, FAR], "ALL") == [CUR, NXT]
assert fp.pick_expiries([], "ALL") == []
print("A current expiry by default, next/all only when asked: OK")

# B. plan date: next weekday after the close, today before it
assert fp.plan_date(datetime(2026, 11, 4, 15, 45)) == date(2026, 11, 5)     # Wed after close
assert fp.plan_date(datetime(2026, 11, 4, 10, 0)) == date(2026, 11, 4)      # Wed in session
assert fp.plan_date(datetime(2026, 11, 6, 15, 45)) == date(2026, 11, 9)     # Fri -> Mon
print("B plan date: OK")

# C. symbol list: default, settings file, ALL, never NIFTY
assert fp.configured_symbols() == fp.DEFAULT_SYMBOLS and "NIFTY" not in fp.DEFAULT_SYMBOLS
json.dump({"fno_plan_symbols": ["banknifty", "NIFTY", "SBIN", "SBIN"]}, open(fp.DATA_SETTINGS_FILE, "w"))
assert fp.configured_symbols() == ["BANKNIFTY", "SBIN"]
rows = []
for name, seg, it in (("NIFTY", "NFO", "OPTIDX"), ("BANKNIFTY", "NFO", "OPTIDX"), ("SENSEX", "BFO", "OPTIDX"),
                      ("SBIN", "NFO", "OPTSTK"), ("SBIN", "NFO", "FUTSTK"), ("CRUDEOIL", "MCX", "OPTFUT")):
    rows.append({"name": name, "exch_seg": seg, "instrumenttype": it})
json.dump({"fno_plan_symbols": "ALL"}, open(fp.DATA_SETTINGS_FILE, "w"))
assert fp.configured_symbols(pd.DataFrame(rows)) == ["BANKNIFTY", "SBIN", "SENSEX"]
os.remove(fp.DATA_SETTINGS_FILE)
print("C symbol list: OK")

# D. order maths (same as the Other Symbols tab)
lv = {"P": 100.0, "S1": 80.0, "S2": 68.0, "S3": 48.0, "R1": 120.0, "R2": 132.0, "R3": 152.0}
b = fp.order_from_levels(lv, "BUY")
assert (b["entry_price"], b["target_1"], b["target_2"], b["target_3"], b["target_4"], b["sl"]) == (68.0, 80.0, 100.0, 120.0, 132.0, 48.0), b
assert b["rr"] == "0.6:1"
s = fp.order_from_levels(lv, "SELL")
assert (s["entry_price"], s["target_1"], s["target_2"], s["target_3"], s["target_4"], s["sl"]) == (132.0, 120.0, 100.0, 80.0, 68.0, 152.0), s
floor = fp.order_from_levels({**lv, "S2": 0.05, "S3": 0.05}, "BUY")
assert floor["entry_price"] == 80.0 and floor["sl"] == round(80 * 0.88, 2)      # S1 when S2 is at the floor
assert fp.order_from_levels({"P": 0}, "BUY") is None
print("D entry / targets / SL from the contract's own levels: OK")

# E. a full run with a fake app: current expiry only, candles per contract, one sheet append
def scrip():
    out = []
    for sym, exch, base in (("BANKNIFTY", "NFO", 52000), ("SBIN", "NFO", 800)):
        step = 100 if sym == "BANKNIFTY" else 10
        for exp in (CUR, NXT):
            for k in range(-6, 7):
                stk = base + k * step
                for ot in ("CE", "PE"):
                    out.append({"name": sym, "exch_seg": exch, "expiry": exp, "strike": stk * 100.0,
                                "symbol": f"{sym}{exp}{stk}{ot}", "token": f"{sym}-{exp}-{stk}-{ot}",
                                "instrumenttype": "OPTIDX" if sym == "BANKNIFTY" else "OPTSTK"})
    return pd.DataFrame(out)

candle_calls = []
def official_strike_candle(exch, tok, itype, mode, now):
    candle_calls.append((tok, mode))
    if tok.endswith("NOCANDLE"):
        return None, "no candle", None
    return ("2026-11-04", 0, 150.0, 90.0, 120.0), "Dhan", None

def mtf_for(sym):
    base, step = (52000, 100) if sym == "BANKNIFTY" else (800, 10)
    piv = {"P": base, "S1": base - step, "S2": base - 2 * step, "S3": base - 3 * step,
           "R1": base + step, "R2": base + 2 * step, "R3": base + 3 * step, "C": base}
    return {"15-Min": piv, "1-Hour": piv, "Daily": piv}

def focus(m, step):
    p = m.get("15-Min", {})
    s = lambda v: int(round(v / step) * step)
    return {"ce_strikes": [s(p["R1"]), s(p["R2"])], "pe_strikes": [s(p["S1"]), s(p["S2"])],
            "P_STRIKE": s(p["P"]), "p_strikes": [s(p["P"])]}

class Sheet:
    def __init__(self): self.appends = []; self.header = []; self.title = fp.SHEET_TAB
    def row_values(self, i): return self.header
    def update(self, values, range_name): self.header = values[0]
    def append_rows(self, rows, value_input_option=None): self.appends.append(rows)
ws = Sheet()
class Book:
    def worksheets(self): return []
    def add_worksheet(self, title, rows, cols): assert title == "F&O Orders"; return ws

eng = types.SimpleNamespace(
    Session=types.SimpleNamespace(scrip_df=scrip()),
    feed=types.SimpleNamespace(upcoming_expiries=lambda sym, n, from_day=None: [CUR, NXT][:n]),
    get_symbol_exchange=lambda s: "NFO",
    get_symbol_step=lambda s, e: 100 if s == "BANKNIFTY" else 10,
    get_symbol_lot_size=lambda s, e: 30,
    get_tv_multi_timeframe_pivots=lambda s, force_refresh=False: mtf_for(s),
    select_intraday_15m_focus_strikes=focus,
    select_1h_structure_strikes=focus,
    official_strike_candle=official_strike_candle,
    calc_fib_pivots=(lambda h, l, c: {"P": round((h + l + c) / 3, 2), "S1": round((h + l + c) / 3 - 0.382 * (h - l), 2),
                          "S2": round((h + l + c) / 3 - 0.618 * (h - l), 2), "S3": round((h + l + c) / 3 - (h - l), 2),
                          "R1": round((h + l + c) / 3 + 0.382 * (h - l), 2), "R2": round((h + l + c) / 3 + 0.618 * (h - l), 2),
                          "R3": round((h + l + c) / 3 + (h - l), 2)}),
    get_gspread_client=lambda: types.SimpleNamespace(open_by_key=lambda k: Book()),
    SPREADSHEET_ID="sheet",
)

now = datetime(2026, 11, 4, 15, 46)
res = fp.generate(eng, ["BANKNIFTY", "SBIN", "NIFTY"], now=now, pause=0)
assert res["success"], res
assert set(res["symbols"]) == {"BANKNIFTY", "SBIN"}                 # NIFTY is never in this run
stored = fp.load_orders()["2026-11-05"]["symbols"]
for sym in ("BANKNIFTY", "SBIN"):
    exps = {x["expiry"] for x in stored[sym]["setups"]}
    assert exps == {CUR}, (sym, exps)                                 # current expiry only
    assert stored[sym]["setups"], sym
assert all(m == "next" for _, m in candle_calls)                    # after the close: next session's candle
assert len(candle_calls) == len(set(t for t, _ in candle_calls))    # each contract's candle fetched once
assert len(ws.appends) == 1 and ws.header == fp.SHEET_HEADERS        # one batched append
assert len(ws.appends[0]) == res["orders"]
print(f"E run: {res['orders']} current-expiry orders, {len(candle_calls)} candles, 1 sheet append: OK")

# F. NEXT on demand adds next-expiry orders, dashboard filter by symbol
res2 = fp.generate(eng, ["SBIN"], expiry_mode="NEXT", now=now, pause=0, sheet=False)
assert {x["expiry"] for x in fp.load_orders()["2026-11-05"]["symbols"]["SBIN"]["setups"]} == {NXT}
assert fp.load_orders()["2026-11-05"]["symbols"]["BANKNIFTY"]["setups"]   # other symbols kept
v = fp.flat_orders(symbol="BANKNIFTY")
assert v["plan_date"] == "2026-11-05" and v["symbols"] == ["BANKNIFTY", "SBIN"]
assert v["setups"] and all(x["symbol"] == "BANKNIFTY" for x in v["setups"])
assert len(fp.flat_orders()["setups"]) == len(v["setups"]) + len(fp.flat_orders(symbol="SBIN")["setups"])
print("F next expiry on demand + symbol filter: OK")

# G. the 15:45 run happens once per plan date even with two apps running
assert fp.claim_auto_run("2026-11-05") is True
assert fp.claim_auto_run("2026-11-05") is False
assert fp.claim_auto_run("2026-11-06") is True
assert not os.path.exists(os.path.join(tmp, ".fno_auto_2026-11-05"))     # old marker tidied
print("G one auto run per day: OK")

# H. sheet write retries when the connection drops, then gives up without raising
fp.time.sleep = lambda s: None
fails = {"n": 0}
def flaky():
    fails["n"] += 1
    if fails["n"] < 3:
        raise ConnectionError("SSL: UNEXPECTED_EOF")
    return types.SimpleNamespace(open_by_key=lambda k: Book())
eng.get_gspread_client = flaky
ws.appends.clear()
assert "rows added" in fp.push_to_sheet(eng, [["x"] * 20])
assert fails["n"] == 3 and len(ws.appends) == 1
eng.get_gspread_client = lambda: (_ for _ in ()).throw(ConnectionError("down"))
assert fp.push_to_sheet(eng, [["x"]]).startswith("sheet not updated")
print("H sheet retry: OK")

# I. add / remove symbols of the daily list from the dashboard; other settings kept
json.dump({"level_source": "ANGEL"}, open(fp.DATA_SETTINGS_FILE, "w"))
df = pd.DataFrame(rows)
r = fp.edit_symbols("add", "sbin", df)
assert r["success"] and r["symbols"] == fp.DEFAULT_SYMBOLS + ["SBIN"], r
assert json.load(open(fp.DATA_SETTINGS_FILE))["level_source"] == "ANGEL"
assert not fp.edit_symbols("add", "SBIN", df)["success"]           # already there
assert not fp.edit_symbols("add", "NIFTY", df)["success"]          # NIFTY has its own plan
assert not fp.edit_symbols("add", "NOSUCH", df)["success"]         # no options
r = fp.edit_symbols("remove", "FINNIFTY", df)
assert r["success"] and "FINNIFTY" not in fp.configured_symbols(df) and "SBIN" in fp.configured_symbols(df)
assert not fp.edit_symbols("remove", "FINNIFTY", df)["success"]
assert fp.edit_symbols("reset", "", df)["symbols"] == fp.DEFAULT_SYMBOLS
print("I add / remove symbols: OK")
print("F&O ORDERS TESTS PASSED")
