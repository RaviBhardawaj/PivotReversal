"""Engine tests against a simulated broker. Run: python3.12 test_engine.py [project dir]"""
import sys, os, time, tempfile, json
from datetime import datetime
import os as _os
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
from brokers import engine as E
from brokers.base import BrokerAdapter, BrokerError, OrderStatus, FILLED, PENDING, REJECTED, CANCELLED


class FakeBroker(BrokerAdapter):
    name = "ANGEL"
    env_keys = []
    def __init__(self, creds, log=print, **kw):
        super().__init__(creds, log)
        self.ltp = {}            # inst_id -> price
        self.book = {}           # oid -> dict
        self.pos = {}            # inst_id -> net qty
        self.n = 0
        self.reject_next = False
        self.fail_after_place = False
        self.calls = []
    def login(self): self.connected = True; return "FAKE1"
    def funds(self): return 100000.0
    def resolve(self, inst):
        o = dict(inst); o["inst_id"] = str(inst["token"]); o["segment"] = "NFO"; return o
    def _fill(self, o, px):
        o["status"] = FILLED; o["avg"] = px; o["filled"] = o["qty"]
        sgn = 1 if o["side"] == "BUY" else -1
        self.pos[o["iid"]] = self.pos.get(o["iid"], 0) + sgn * o["qty"]
    def place(self, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        self.calls.append(("place", side, qty, order_type, price, trigger))
        self.n += 1; oid = f"O{self.n}"
        o = dict(oid=oid, iid=inst["inst_id"], side=side, qty=qty, type=order_type, price=price,
                 trigger=trigger, status=PENDING, avg=0, filled=0, tag=tag, msg="")
        if self.reject_next:
            self.reject_next = False; o["status"] = REJECTED; o["msg"] = "RMS: margin shortfall"
        self.book[oid] = o
        self.match()
        if self.fail_after_place:
            self.fail_after_place = False
            raise BrokerError("timeout")
        return oid
    def modify(self, order_id, inst, side, qty, order_type, price=0.0, trigger=0.0, product="INTRADAY", tag=""):
        self.calls.append(("modify", order_id, order_type, price, trigger))
        o = self.book[order_id]
        if o["status"] != PENDING: raise BrokerError("order not open")
        o.update(type=order_type, price=price, trigger=trigger); self.match()
    def cancel(self, order_id, inst=None, tag=""):
        self.calls.append(("cancel", order_id))
        o = self.book[order_id]
        if o["status"] != PENDING: raise BrokerError("order not open")
        o["status"] = CANCELLED
    def match(self):
        for o in self.book.values():
            if o["status"] != PENDING: continue
            px = self.ltp.get(o["iid"], 0)
            if o["type"] == "MARKET": self._fill(o, px)
            elif o["type"] == "LIMIT":
                if (o["side"] == "BUY" and px <= o["price"]) or (o["side"] == "SELL" and px >= o["price"]): self._fill(o, px)
            elif o["type"] == "SL":
                if (o["side"] == "SELL" and px <= o["trigger"]) or (o["side"] == "BUY" and px >= o["trigger"]): self._fill(o, px)
    def orders(self):
        return {k: OrderStatus(k, o["status"], o["avg"], o["filled"], o["msg"], o["tag"]) for k, o in self.book.items()}
    def positions(self):
        return {k: v for k, v in self.pos.items() if v}


class Clock:
    t = datetime(2026, 10, 8, 10, 0, 0)
    def __call__(self): return self.t

FB = {}
def make(tmp, clock, settings=None):
    class F:
        env_keys = []
        def __new__(cls, creds, log=print, **kw):
            return FB.setdefault("b", FakeBroker(creds, log))
    E.ADAPTERS["ANGEL"] = F
    if settings:
        json.dump(settings, open(os.path.join(tmp, "order_settings.json"), "w"))
    logs, alerts, olog = [], [], []
    eng = E.OrderEngine(tmp, log=logs.append, alert=lambda t, m: alerts.append(t), clock=clock,
                        order_logger=lambda **k: olog.append(k), poll_s=0.05, reconcile_s=0.2)
    eng._logs, eng._alerts, eng._olog = logs, alerts, olog
    return eng

def settle(eng, s=0.4):
    time.sleep(s); eng._jobs.join(); time.sleep(0.15); eng._jobs.join()

def trade(tid, action="BUY", tf="15M Intraday", px=100.0, sl=80.0):
    return {"Trade_ID": tid, "Action": action, "Timeframe": tf, "Entry_Price": px, "SL": sl}

def inst(tok="111", lot=75):
    return {"symbol": "NIFTY", "exchange": "NFO", "token": tok, "tsym": f"NIFTY{tok}CE", "expiry": "13OCT2026",
            "strike": 22600, "opt_type": "CE", "lot_size": lot}

ok = 0
def check(cond, msg):
    global ok
    if not cond:
        raise AssertionError(msg)
    ok += 1

# ── 1. full happy path with chase, SL at broker, trailing, app exit ──
tmp = tempfile.mkdtemp(); clock = Clock(); FB.clear()
eng = make(tmp, clock, {"chase_after_s": 1.5, "lots": 2})
r, m = eng.connect(); check(r, m)
b = FB["b"]
check(eng.enter(trade("T1"), inst()) == "NO ORDER (auto-trading off)", "auto off blocks")
check(eng.set_auto(True)[0] is False, "auto needs confirmation")
eng.set_auto(True, confirmed=True)
b.ltp["111"] = 101.0                          # above limit 100.5 → unfilled
check(eng.enter(trade("T1"), inst()) == "QUEUED", "queued")
settle(eng, 0.2)
p = eng.positions["T1"]
check(p["qty"] == 150 and p["state"] == "OPENING", f"qty lots×lot, pending {p['qty']} {p['state']}")
check(b.calls[0][:5] == ("place", "BUY", 150, "LIMIT", 100.5), f"limit +0.5% {b.calls[0]}")
time.sleep(1.5); settle(eng)
check(any(c[0] == "modify" and c[2] == "MARKET" for c in b.calls), "chased to MARKET")
check(p["state"] == "OPEN" and p["entry_px"] == 101.0, f"filled {p['state']} {p['entry_px']}")
settle(eng)
check(p["sl_oid"] and p["sl_trigger"] == 80.0, f"SL at broker {p['sl_oid']} {p['sl_trigger']}")
slo = b.book[p["sl_oid"]]
check(slo["type"] == "SL" and slo["side"] == "SELL" and abs(slo["price"] - 76.0) < 0.01, f"SL limit 5% below {slo}")
t = trade("T1"); t["SL"] = 101.0; b.ltp["111"] = 120; eng.on_tick(t, 120); settle(eng)
check(b.book[p["sl_oid"]]["trigger"] == 101.0 and p["sl_trigger"] == 101.0, "SL trailed at broker")
eng.on_tick(t, 121); settle(eng)
n_mod = sum(1 for c in b.calls if c[0] == "modify")
eng.on_tick(t, 122); settle(eng)
check(sum(1 for c in b.calls if c[0] == "modify") == n_mod, "no modify when SL unchanged")
b.ltp["111"] = 130
eng.exit(t, "Target 5 Hit", 130); settle(eng)
check(b.book[slo["oid"]]["status"] == CANCELLED, "SL cancelled before exit")
check(p["state"] == "CLOSED" and p["exit_px"] == 130, f"exit filled {p['state']} {p['exit_px']}")
check(abs(eng.realized_rs - (130 - 101) * 150) < 0.01, f"pnl {eng.realized_rs}")
check(eng.pop_forced_close("T1") is None, "no forced close for app exit")
check(b.positions() == {}, "flat at broker")
print("1 ok")

# ── 2. broker SL fills by itself → forced close for the tracker ──
b.ltp["222"] = 50
eng.enter(trade("T2", px=50, sl=40), inst("222")); settle(eng); settle(eng)
p2 = eng.positions["T2"]; check(p2["state"] == "OPEN" and p2["sl_oid"], "T2 open with SL")
b.ltp["222"] = 39; b.match(); settle(eng)
check(p2["state"] == "CLOSED", "closed by broker SL")
fc = eng.pop_forced_close("T2"); check(fc and fc[1] == "SL Hit (broker)" and fc[0] == 39, f"forced {fc}")
eng.exit(trade("T2"), "SL Hit", 39); settle(eng)
check(sum(1 for o in b.book.values() if o["iid"] == "222") == 2, "no extra exit order after broker SL")
print("2 ok")

# ── 3. rejection ──
b.ltp["333"] = 10; b.reject_next = True
eng.enter(trade("T3", px=10, sl=8), inst("333")); settle(eng)
check(eng.positions["T3"]["state"] == "FAILED", "rejected → FAILED")
check(eng.pop_forced_close("T3") == (10.0, "Order Rejected"), "forced reject close")
check(any("REJECTED" in a for a in eng._alerts), "reject alert")
check(eng.order_status_for("T3") == "Rejected", "status label")
print("3 ok")

# ── 4. place raises timeout but order reached broker → no duplicate ──
b.ltp["444"] = 10; b.fail_after_place = True
eng.enter(trade("T4", px=10, sl=8), inst("444")); settle(eng); settle(eng)
p4 = eng.positions["T4"]
check(p4["entry_oid"] and p4["state"] == "OPEN", f"found by tag {p4['state']}")
check(sum(1 for o in b.book.values() if o["iid"] == "444" and o["side"] == "BUY") == 1, "one entry only")
print("4 ok")

# ── 5. limits: SELL skip, timeframe, same contract, max trades ──
eng.enter(trade("T5", action="SELL"), inst("555")); settle(eng)
check("T5" not in eng.positions and "SELL" in eng.order_status_for("T5"), "SELL skipped")
eng.save_settings({"trade_1h": False})
eng.enter(trade("T6", tf="1H Structure"), inst("666")); settle(eng)
check("T6" not in eng.positions, "1H disabled")
eng.enter(trade("T7", px=10), inst("444")); settle(eng)
check("T7" not in eng.positions and "already holding" in eng.order_status_for("T7"), "same contract blocked")
eng.save_settings({"max_trades": 4})       # T1,T2,T4 count (T3 failed)
b.ltp["777"] = 5; eng.enter(trade("T8", px=5, sl=4), inst("777")); settle(eng); settle(eng)
check("T8" in eng.positions, "4th allowed")
b.ltp["888"] = 5; eng.enter(trade("T9", px=5, sl=4), inst("888")); settle(eng)
check("T9" not in eng.positions and "max 4" in eng.order_status_for("T9"), "max trades")
print("5 ok")

# ── 6. kill switch ──
n = eng.kill(); settle(eng); settle(eng)
check(n == 2 and not eng.auto_trade, f"kill {n}")
check(eng.positions["T4"]["state"] == "CLOSED" and eng.positions["T8"]["state"] == "CLOSED", "kill exits")
check(eng.pop_forced_close("T4")[1] == "Kill switch", "kill forced close")
check(b.positions() == {}, "flat after kill")
print("6 ok")

# ── 7. max loss ──
tmp2 = tempfile.mkdtemp(); FB.clear()
eng2 = make(tmp2, clock, {"max_loss_rs": 1000})
eng2.connect(); eng2.set_auto(True, confirmed=True); b2 = FB["b"]
b2.ltp["1"] = 100; eng2.enter(trade("L1", sl=80), inst("1", lot=75)); settle(eng2); settle(eng2)
b2.ltp["1"] = 80; b2.match(); settle(eng2)
check(eng2.realized_rs == -1500, f"loss {eng2.realized_rs}")
b2.ltp["2"] = 100; eng2.enter(trade("L2"), inst("2")); settle(eng2)
check("L2" not in eng2.positions and "max loss" in eng2.order_status_for("L2"), "max loss blocks")
check(any("MAX LOSS" in a for a in eng2._alerts), "max loss alert")
print("7 ok")

# ── 8. restart: open position restored; closed at broker while app was down ──
tmp3 = tempfile.mkdtemp(); FB.clear()
eng3 = make(tmp3, clock)
eng3.connect(); eng3.set_auto(True, confirmed=True); b3 = FB["b"]
b3.ltp["9"] = 100; eng3.enter(trade("R1"), inst("9")); settle(eng3); settle(eng3)
b3.ltp["10"] = 100; eng3.enter(trade("R2"), inst("10")); settle(eng3); settle(eng3)
check(eng3.positions["R1"]["state"] == "OPEN" and eng3.positions["R2"]["state"] == "OPEN", "two open")
# app dies; user exits R2 manually in broker app (SL order cancelled by them too)
b3.book[eng3.positions["R2"]["sl_oid"]]["status"] = CANCELLED
b3.pos["10"] = 0
eng3b = make(tmp3, clock)       # same broker instance via FB
check(eng3b._resume_auto, "auto resume flag")
eng3b.connect(); settle(eng3b)
check(eng3b.auto_trade, "auto resumed after restart")
check(eng3b.positions["R1"]["state"] == "OPEN", "R1 still open")
check(eng3b.positions["R2"]["state"] == "CLOSED", "R2 closed at broker")
check(eng3b.pop_forced_close("R2")[1] == "Closed at broker", "forced closed-at-broker")
eng3b.enter(trade("R1"), inst("9")); settle(eng3b)
check(sum(1 for o in b3.book.values() if o["iid"] == "9" and o["side"] == "BUY") == 1, "no duplicate entry after restart")
print("8 ok")

# ── 9. exit while entry still pending → cancel, nothing sent ──
b3.ltp["11"] = 200
eng3b.save_settings({"chase_after_s": 60})
eng3b.enter(trade("C1", px=100, sl=80), inst("11")); settle(eng3b)
check(eng3b.positions["C1"]["state"] == "OPENING", "pending")
eng3b.exit(trade("C1"), "SL Hit", 200); settle(eng3b)
c1 = eng3b.positions["C1"]
check(c1["state"] == "CLOSED" and c1["filled"] == 0, "cancelled entry")
check(b3.book[c1["entry_oid"]]["status"] == CANCELLED, "broker cancel")
check(not any(o["iid"] == "11" and o["side"] == "SELL" for o in b3.book.values()), "no exit for unfilled")
print("9 ok")

# ── 10. EOD safety net at 15:00 ──
clock.t = datetime(2026, 10, 8, 15, 0, 5); b3.ltp["9"] = 110
time.sleep(0.3); settle(eng3b); settle(eng3b)
check(eng3b.positions["R1"]["state"] == "CLOSED", "EOD engine exit")
check(eng3b.pop_forced_close("R1")[1] == "EOD 3PM (engine)", "EOD forced close")
eng3b.enter(trade("Z1"), inst("12")); settle(eng3b)
check("Z1" not in eng3b.positions and "entry hours" in eng3b.order_status_for("Z1"), "no entries after 3PM")
print("10 ok")

# ── 11. new day starts clean ──
clock.t = datetime(2026, 10, 9, 9, 20)
eng4 = make(tmp3, clock)
check(eng4.positions == {} and not eng4._resume_auto, "new day clean")
print("11 ok")

# ── 12. exit failure retries then alerts ──
tmp5 = tempfile.mkdtemp(); FB.clear(); clock.t = datetime(2026, 10, 8, 11, 0)
eng5 = make(tmp5, clock, {"sl_at_broker": False})
eng5.connect(); eng5.set_auto(True, confirmed=True); b5 = FB["b"]
b5.ltp["5"] = 100; eng5.enter(trade("X1"), inst("5")); settle(eng5); settle(eng5)
orig = b5.place
def bad(*a, **k):
    if a[1] == "SELL": raise BrokerError("exchange down")
    return orig(*a, **k)
b5.place = bad
eng5.exit(trade("X1"), "Target 1 Hit", 100); settle(eng5); settle(eng5); settle(eng5)
check(eng5.positions["X1"]["exit_tries"] == 3 and "EXIT FAILED" in eng5.positions["X1"]["note"], "3 tries")
check(any("EXIT FAILED" in a for a in eng5._alerts), "exit failed alert")
print("12 ok")

print(f"ALL ENGINE TESTS PASSED ({ok} checks)")
