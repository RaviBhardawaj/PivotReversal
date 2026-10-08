"""Dashboard TF filter: every strike is in exactly one box (15M only, 1H only, 15M + 1H).
Run: python3 tests/test_tf_filter.py"""
import os, re, ast
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Options-angel_live-ok.py")).read()
ns = {}
for name in ("dash_row_flags", "dash_row_passes"):
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module([node], []), name, "exec"), ns)
f = lambda stk, ot="CE": ns["dash_row_flags"]({"type": ot, "strike": stk, "side": "BUY"},
                                             ce_15m=[22600, 22650], pe_15m=[], ce_1h=[22650, 22700], pe_1h=[],
                                             p_15m=[], p_1h=[])
assert f(22600)["TF"] == {"15M"}
assert f(22700)["TF"] == {"1H"}
assert f(22650)["TF"] == {"BOTH"}
sel = lambda *k: {"TF": set(k)}
P = ns["dash_row_passes"]
assert P(f(22600), sel("15M")) and not P(f(22650), sel("15M")) and not P(f(22700), sel("15M"))
assert P(f(22700), sel("1H")) and not P(f(22650), sel("1H"))
assert P(f(22650), sel("BOTH")) and not P(f(22600), sel("BOTH"))
assert P(f(22650), sel("15M", "BOTH")) and P(f(22600), sel("15M", "BOTH"))
# a CE 15M strike that is a PE 1H strike is "15M + 1H" for both rows, like its trades
g = lambda ot: ns["dash_row_flags"]({"type": ot, "strike": 22500, "side": "SELL"}, ce_15m=[22500], pe_15m=[],
                                    ce_1h=[], pe_1h=[22500], p_15m=[], p_1h=[])
assert g("CE")["TF"] == g("PE")["TF"] == {"BOTH"}
print("TF FILTER TESTS PASSED")
