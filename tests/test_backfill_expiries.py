"""Backfill 'auto' replays the CURRENT expiry only; the next one with --next; far ones never.
Run: python3 tests/test_backfill_expiries.py"""
import ast, os
from datetime import datetime as _real_dt
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backfill_missed_entries.py")).read()
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "day_expiries")
ns = {"_real_dt": _real_dt}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "backfill", "exec"), ns)
day_expiries = ns["day_expiries"]

cache_exps = ["17NOV2026", "09NOV2026", "23NOV2026", "03NOV2026", "09NOV2026", None, ""]
assert day_expiries("2026-11-09", cache_exps) == ["09NOV2026"]                       # expiry day: today's
assert day_expiries("2026-11-09", cache_exps, True) == ["09NOV2026", "17NOV2026"]    # --next
assert day_expiries("2026-11-10", cache_exps) == ["17NOV2026"]                       # 9 Nov has expired
assert "23NOV2026" not in day_expiries("2026-11-09", cache_exps, True)               # far expiry never
assert "and s.get(\"expiry\") in ALLOWED" in src                  # jobs are filtered
print("BACKFILL EXPIRY TESTS PASSED")
