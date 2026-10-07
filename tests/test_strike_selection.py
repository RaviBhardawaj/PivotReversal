"""Strike selection rules for live orders. Run: python3 tests/test_strike_selection.py [project dir]"""
import os, sys, importlib.util, types
root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, root)
from brokers.engine import strike_selection_reason as R, DEFAULT_SETTINGS

n = 0
def check(cond, msg):
    global n
    assert cond, msg
    n += 1

S = dict(DEFAULT_SETTINGS)
T = {"Timeframe": "15M Intraday", "Type": "CE", "Symbol": "NIFTY", "Strike": 22600}
I = {"levels": ["R1"], "opt_type": "CE", "symbol": "NIFTY", "strike": 22600}
check(R(S, T, I) == "", "defaults allow everything")
check(R(S, T, dict(I, levels=[])) == "", "all levels on: unknown level allowed")
check(R(dict(S, levels="S1,S2"), T, I) == "level R1 not selected", "level off")
check(R(dict(S, levels="R1,P"), T, I) == "", "level on")
check(R(dict(S, levels="R1,P"), T, dict(I, levels=[])) == "strike level unknown", "unknown level with filter")
check(R(dict(S, levels=""), T, I) == "no strike level selected", "no levels")
check(R(dict(S, levels="PN"), T, dict(I, levels=["P", "PN"])) == "", "multi-level match")
check(R(dict(S, opt_types="PE"), T, I) == "CE not selected", "type off")
check(R(dict(S, opt_types=""), T, I) == "no option type selected", "no types")
check(R(dict(S, strike_tf="BOTH"), T, I) == "strike is not on both 15M and 1H", "needs both TFs")
check(R(dict(S, strike_tf="BOTH"), dict(T, Timeframe="15M / 1H"), I) == "", "both TFs")
check(R(dict(S, symbols="sensex, banknifty"), T, I) == "NIFTY not selected", "symbol off")
check(R(dict(S, symbols="NIFTY"), T, I) == "", "symbol on")
check(R(dict(S, strikes="22650, 22700"), T, I) == "strike 22600 not in your strike list", "strike off")
check(R(dict(S, strikes="22600"), T, dict(I, strike=22600.0)) == "", "strike on (float)")
check(R(dict(S, strikes="22600"), T, dict(I, strike=None)) == "", "falls back to trade strike")

# strike_levels from the main app (pure function, extracted without importing the whole app)
src = open(os.path.join(root, "Options-angel_live-ok.py")).read()
start = src.index("def strike_levels(")
end = src.index("\ndef ", start + 10)
ns = {}
exec(src[start:end], ns)
L = ns["strike_levels"]
f15 = {"CE1": 22700, "CE2": 22750, "PE1": 22500, "PE2": 22450, "P_STRIKE": 22600, "p_strikes": [22550, 22600, 22650]}
f1h = {"CE1": 22800, "CE2": 22900, "PE1": 22400, "PE2": 22300, "P_STRIKE": 22650, "p_strikes": [22600, 22650, 22700]}
check(L(22700, f15, f1h) == {"R1", "PN"}, L(22700, f15, f1h))
check(L(22600, f15, f1h) == {"P", "PN"}, L(22600, f15, f1h))
check(L(22550, f15, {}) == {"PN"}, "PN")
check(L(22450, f15, f1h) == {"S2"}, "S2")
check(L(23000, f15, f1h) == set(), "none")
check(L("22500", f15) == {"S1"}, "string strike")
print(f"ALL STRIKE SELECTION TESTS PASSED ({n} checks)")
