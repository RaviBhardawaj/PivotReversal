"""Dhan chart requests are spaced out and a refused ("too many requests") call is retried,
so a strike never falls back to a weaker candle because of a burst.
Run: python3 tests/test_dhan_rate_limit.py"""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from brokers import dhan
from brokers.dhan import DhanAdapter
from brokers.base import BrokerError

clock = {"t": 1000.0, "sleeps": [], "sent": []}
dhan.time.time = lambda: clock["t"]
def _sleep(s):
    clock["sleeps"].append(round(s, 2)); clock["t"] += s
dhan.time.sleep = _sleep

class R:
    def __init__(s, code, body): s.status_code, s._b = code, body; s.content = json.dumps(body).encode(); s.text = json.dumps(body)
    def json(s): return s._b
OK = {"timestamp": [1760000000], "open": [10], "high": [12], "low": [9], "close": [11]}
class Http:
    def __init__(s, script): s.script = list(script)
    def post(s, url, json=None, headers=None, timeout=None):
        clock["sent"].append(clock["t"]); return s.script.pop(0)

ad = DhanAdapter({"DHAN_CLIENT_ID": "x", "DHAN_ACCESS_TOKEN": "y"}, log=lambda *a: None)
# 1. refused twice (DH-904 / HTTP 429), then answered: the candles come back
ad._http = Http([R(429, {"errorCode": "DH-904", "errorMessage": "Too many requests"}),
                 R(400, {"errorCode": "DH-904", "errorMessage": "Too many requests"}), R(200, OK)])
rows = ad.daily_candles("123")
assert rows and rows[0][4] == 11.0, rows
print("1 rate-limited twice, then answered: candles returned: OK")
# 2. a real error (bad security id) is not retried
ad._http = Http([R(400, {"errorCode": "DH-905", "errorMessage": "Invalid security id"}), R(200, OK)])
try:
    ad.daily_candles("999"); raise SystemExit("expected an error")
except BrokerError as e:
    assert "DH-905" in str(e) and len(ad._http.script) == 1, e
print("2 a real error is raised at once, not retried: OK")
# 3. a burst of calls is spaced out by at least CHART_GAP
clock["sent"].clear()
ad._http = Http([R(200, OK) for _ in range(5)])
for i in range(5):
    ad.intraday_bars("1", "2025-10-09")
gaps = [b - a for a, b in zip(clock["sent"], clock["sent"][1:])]
assert all(g >= DhanAdapter.CHART_GAP - 1e-9 for g in gaps), gaps
print("3 five calls in a row are spaced %.2fs apart: OK" % min(gaps))
# 4. still refused after every try: the error is raised (the strike is retried later)
ad._http = Http([R(429, {"errorCode": "DH-904", "errorMessage": "Too many requests"})] * DhanAdapter.CHART_TRIES)
try:
    ad.daily_candles("123"); raise SystemExit("expected an error")
except BrokerError as e:
    assert "DH-904" in str(e)
print("4 refused on every try: error raised after %d tries: OK" % DhanAdapter.CHART_TRIES)
print("DHAN RATE LIMIT TESTS PASSED")
