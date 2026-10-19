"""
Order engine: turns the strategy's forward-test entries and exits into real broker orders.

Flow for one trade
  enter()   → checks → LIMIT entry (LTP + buffer) → after N s unfilled, modified to MARKET
  filled    → stop-loss order placed at the broker (SL-limit at the trade's SL)
  on_tick() → trade's trailed SL changed → broker SL order modified
  exit()    → SL order cancelled → marketable LIMIT exit → after N s unfilled, MARKET
  broker SL fills by itself / position closed in the broker app → the trade is closed in the
            forward test on its next tick (pop_forced_close)

Every broker call runs on one worker thread (never on the tick thread), one call at a time.
State is saved to order_state.json so an app restart picks up open positions.
"""

import os
import json
import queue
import threading
import time
from datetime import datetime, time as dt_time

from .base import (BrokerError, FILLED, PENDING, REJECTED, CANCELLED, tick_round)
from .angel import AngelAdapter
from .dhan import DhanAdapter
from .xts import XTSAdapter

ADAPTERS = {"ANGEL": AngelAdapter, "DHAN": DhanAdapter, "XTS": XTSAdapter}
BROKER_LABELS = {"ANGEL": "Angel One", "DHAN": "Dhan", "XTS": "Jainam XTS"}

DEFAULT_SETTINGS = {
    "broker":            "ANGEL",
    "lots":              1,        # lots per trade
    "lots_15m":          0,        # 0 = use "lots"
    "lots_1h":           0,        # 0 = use "lots"
    "max_lots":          20,       # hard cap per order
    "entry_type":        "LIMIT",  # LIMIT (LTP + buffer, then MARKET) or MARKET
    "limit_buffer_pct":  0.5,
    "chase_after_s":     10,       # unfilled LIMIT → modified to MARKET after this many seconds
    "product":           "INTRADAY",
    "sl_at_broker":      True,     # place a stop-loss order at the broker
    "sl_limit_gap_pct":  5.0,      # SL-limit price this far beyond the trigger, so it fills on a gap
    "take_sell_setups":  True,     # SELL setups short an option (needs margin); on (AJ, 8 Oct)
    "trade_15m":         True,
    "trade_1h":          True,
    "max_trades":        6,
    "max_loss_rs":       5000,
    # strike selection: which of the strategy's strikes get real orders
    # pivot levels per timeframe, e.g. "15M:R1,1H:P" (PN = strikes next to P; a bare "R1" means both)
    "levels":            "15M:R1,15M:R2,15M:S1,15M:S2,15M:P,15M:PN,1H:R1,1H:R2,1H:S1,1H:S2,1H:P,1H:PN",
    "opt_types":         "CE,PE",
    "strike_tf":         "ANY",    # ANY = a 15M or 1H strike; BOTH = must be a strike on both
    "symbols":           "",       # e.g. "NIFTY,SENSEX"; blank = all
    "strikes":           "",       # e.g. "22600,22650"; blank = all
}
ALL_LEVELS = ("R1", "R2", "S1", "S2", "P", "PN")
LEVEL_LABELS = {"R1": "R1", "R2": "R2", "S1": "S1", "S2": "S2", "P": "P", "PN": "Next to P"}
TIMEFRAMES = ("15M", "1H")
ALL_TF_LEVELS = tuple(f"{tf}:{lv}" for tf in TIMEFRAMES for lv in ALL_LEVELS)


def tf_levels(values):
    """Levels qualified by timeframe: 'R1' (old form, no timeframe) counts for both 15M and 1H."""
    out = set()
    for x in values:
        x = str(x).strip().upper()
        if not x:
            continue
        out |= {x} if ":" in x else {f"{tf}:{x}" for tf in TIMEFRAMES}
    return out


def tf_level_label(x):
    tf, _, lv = x.partition(":")
    return f"{tf} {LEVEL_LABELS.get(lv, lv)}"


def csv_set(v):
    """'a, b,c' -> {'A','B','C'}"""
    return {x.strip().upper() for x in str(v or "").replace(";", ",").split(",") if x.strip()}


def strike_selection_reason(s, trade, inst):
    """Why the strike selection in settings s rules this entry out ('' = allowed)."""
    lv_on = tf_levels(csv_set(s.get("levels", "")))
    lv = tf_levels(inst.get("levels") or [])
    if lv_on != set(ALL_TF_LEVELS):
        if not lv_on:
            return "no strike level selected"
        if not lv:
            return "strike level unknown"
        if not (lv & lv_on):
            return f"level {' / '.join(tf_level_label(x) for x in ALL_TF_LEVELS if x in lv)} not selected"
    types = csv_set(s.get("opt_types", ""))
    ot = str(inst.get("opt_type") or trade.get("Type") or "").upper()
    if not types:
        return "no option type selected"
    if ot not in types:
        return f"{ot} not selected"
    if str(s.get("strike_tf", "ANY")).upper() == "BOTH":
        tf = str(trade.get("Timeframe", ""))
        if not ("15M" in tf and "1H" in tf):
            return "strike is not on both 15M and 1H"
    syms = csv_set(s.get("symbols", ""))
    sym = str(inst.get("symbol") or trade.get("Symbol") or "").upper()
    if syms and sym not in syms:
        return f"{sym} not selected"
    only = set()
    for x in csv_set(s.get("strikes", "")):
        try:
            only.add(round(float(x), 2))
        except ValueError:
            pass
    if only:
        try:
            stk = round(float(inst.get("strike") or trade.get("Strike")), 2)
        except (TypeError, ValueError):
            return "strike unknown"
        if stk not in only:
            return f"strike {stk:g} not in your strike list"
    return ""

ENTRY_CUTOFF = dt_time(15, 0)   # no new entries from 3:00 PM (the strategy squares off then)
OPEN_STATES = ("OPENING", "OPEN", "CLOSING")


def _now():
    return datetime.now()


class OrderEngine:
    def __init__(self, base_dir, log=print, alert=None, order_logger=None,
                 market_open=None, session_getter=None, env_reader=None, session_relogin=None, clock=_now,
                 poll_s=1.0, reconcile_s=30.0, start_threads=True):
        self.base_dir = base_dir
        self.log = log
        self.alert = alert or (lambda t, m: None)
        self.order_logger = order_logger or (lambda **k: None)
        self.market_open = market_open or (lambda: True)
        self.session_getter = session_getter
        self.session_relogin = session_relogin
        self.env_reader = env_reader or (lambda k: os.getenv(k, ""))
        self.clock = clock
        self.poll_s = poll_s
        self.reconcile_s = reconcile_s

        self.settings_file = os.path.join(base_dir, "order_settings.json")
        self.state_file = os.path.join(base_dir, "order_state.json")
        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()

        self.broker = None
        self.broker_name = ""
        self.connected = False
        self.status_msg = "Not connected"
        self.auto_trade = False
        self.positions = {}          # trade_id -> position dict
        self.realized_rs = 0.0
        self.recent = []             # last orders for the Settings window
        self._forced = {}            # trade_id -> (price, reason) for the forward test
        self._flock = threading.Lock()
        self.skipped = {}            # trade_id -> why no order was sent
        self._resume_auto = False
        self._seq = 0
        self._lock = threading.RLock()
        self._jobs = queue.Queue()
        self._last_reconcile = 0.0
        self._missing = {}           # trade_id -> consecutive reconcile misses
        self._limit_note = ""
        self._load_state()
        if start_threads:
            threading.Thread(target=self._worker, name="order-worker", daemon=True).start()
            threading.Thread(target=self._monitor, name="order-monitor", daemon=True).start()

    # ════════════════════════════════════════════════════════
    # settings and state
    # ════════════════════════════════════════════════════════
    def _load_settings(self):
        try:
            with open(self.settings_file) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in DEFAULT_SETTINGS:
                    self.settings[k] = v
            if not saved.get("sell_default_v2"):
                # SELL setups used to be off by default; from 8 Oct they are traded like BUY setups
                self.settings["take_sell_setups"] = True
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"[ORDERS] Settings file unreadable ({e}); using defaults")

    def save_settings(self, new: dict):
        with self._lock:
            for k, v in new.items():
                if k in DEFAULT_SETTINGS:
                    self.settings[k] = type(DEFAULT_SETTINGS[k])(v)
            self.settings["lots"] = max(1, min(int(self.settings["lots"]), int(self.settings["max_lots"])))
            tmp = self.settings_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(self.settings, sell_default_v2=True), f, indent=2)
            os.replace(tmp, self.settings_file)

    def _today(self):
        return self.clock().strftime("%Y-%m-%d")

    def _save_state(self):
        try:
            data = {"date": self._today(), "broker": self.broker_name, "auto_trade": self.auto_trade,
                    "realized_rs": round(self.realized_rs, 2), "positions": self.positions}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, default=str)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"[ORDERS] Could not save order state: {e}")

    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
        except Exception:
            return
        if d.get("date") != self._today():
            return                                  # a new day starts clean
        self.positions = d.get("positions", {}) or {}
        self.realized_rs = float(d.get("realized_rs", 0) or 0)
        self._resume_auto = bool(d.get("auto_trade"))   # crash restart during the day
        if self.positions:
            self.log(f"[ORDERS] Restored {len(self.positions)} order record(s) from today")

    # ════════════════════════════════════════════════════════
    # connection
    # ════════════════════════════════════════════════════════
    def creds_for(self, name):
        cls = ADAPTERS[name]
        creds = {k: self.env_reader(k) for k in cls.env_keys}
        if name == "ANGEL":
            creds["_session_getter"] = self.session_getter
            creds["_session_relogin"] = self.session_relogin
        return creds

    def has_creds(self, name):
        """Check if credentials are configured for the specified broker."""
        name = (name or "").upper()
        if name == "DHAN":
            cid = self.env_reader("DHAN_CLIENT_ID")
            tok = self.env_reader("DHAN_ACCESS_TOKEN")
            pin = self.env_reader("DHAN_PIN")
            totp = self.env_reader("DHAN_TOTP_SECRET")
            token_file = os.path.join(self.base_dir, ".dhan_token")
            return bool(cid and (tok or (pin and totp) or os.path.exists(token_file)))
        elif name == "ANGEL":
            return bool(self.env_reader("ANGEL_CLIENT_ID") or (self.session_getter and self.session_getter()))
        elif name == "XTS":
            return bool(self.env_reader("XTS_API_KEY") and self.env_reader("XTS_API_SECRET"))
        return False

    def connect(self, name=None, allow_fallback=True):
        """Log in to the chosen broker. If the chosen broker (e.g. Angel) fails,
        automatically falls back to the secondary broker (e.g. Dhan) if configured.
        Returns (ok, message). Safe to call again."""
        target = (name or self.settings.get("broker", "ANGEL")).upper()
        if target not in ADAPTERS:
            return False, f"Unknown broker {target}"
        with self._lock:
            if any(p["state"] in OPEN_STATES and p.get("broker") != target for p in self.positions.values()):
                return False, "Close today's open positions before switching broker"

        def _do_login(b_name):
            kw = {"cache_dir": self.base_dir} if b_name == "DHAN" else {}
            ad = ADAPTERS[b_name](self.creds_for(b_name), log=self.log, **kw)
            acct = ad.login()
            return ad, acct

        primary_err = None
        adapter, account = None, ""
        try:
            adapter, account = _do_login(target)
        except (BrokerError, Exception) as e:
            primary_err = str(e)
            fallback_target = None
            if allow_fallback:
                if target == "ANGEL" and self.has_creds("DHAN"):
                    fallback_target = "DHAN"
                elif target == "DHAN" and self.has_creds("ANGEL"):
                    fallback_target = "ANGEL"

            if fallback_target:
                self.log(f"[ORDERS] ⚠️ {BROKER_LABELS.get(target, target)} login failed ({primary_err}). Automatically falling back to {BROKER_LABELS.get(fallback_target, fallback_target)}...")
                try:
                    adapter, account = _do_login(fallback_target)
                    target = fallback_target
                    self.log(f"[ORDERS] ✅ Fallback to {BROKER_LABELS.get(target, target)} successful ({account})")
                except Exception as fb_err:
                    err_msg = f"{BROKER_LABELS.get(target, target)} failed ({primary_err}); Fallback {BROKER_LABELS.get(fallback_target, fallback_target)} also failed ({fb_err})"
                    with self._lock:
                        self.connected = False
                        self.status_msg = err_msg
                    return False, err_msg
            else:
                with self._lock:
                    self.connected = False
                    self.status_msg = primary_err
                return False, primary_err

        with self._lock:
            self.broker, self.broker_name, self.connected = adapter, target, True
            fallback_note = f" (fallback from {BROKER_LABELS.get('ANGEL' if target == 'DHAN' else 'DHAN', 'primary')})" if primary_err else ""
            self.status_msg = f"Connected: {BROKER_LABELS[target]} {account}{fallback_note}"
            if self.settings.get("broker") != target:
                self.save_settings({"broker": target})
        self.log(f"[ORDERS] {self.status_msg}")
        self._sync_with_broker(startup=True)
        if self._resume_auto:
            self._resume_auto = False
            self.set_auto(True, confirmed=True)
            self.log("[ORDERS] Auto-trading resumed after restart")
        return True, self.status_msg

    def test_connection(self, name, sample_inst=None):
        """Log in with a broker without switching to it: login, funds, and one instrument lookup."""
        try:
            kw = {"cache_dir": self.base_dir} if name == "DHAN" else {}
            ad = ADAPTERS[name](self.creds_for(name), log=self.log, **kw)
            account = ad.login()
            lines = [f"Login OK ({account})"]
            f = ad.funds()
            lines.append(f"Available funds: ₹{f:,.0f}" if f is not None else "Funds: not reported")
            if sample_inst:
                r = ad.resolve(sample_inst)
                lines.append(f"Contract {sample_inst.get('tsym')} → ID {r['inst_id']}")
            return True, "\n".join(lines)
        except Exception as e:
            return False, str(e)

    def set_auto(self, on, confirmed=False):
        if on and not self.connected:
            return False, "Connect a broker first"
        if on and not confirmed:
            return False, "Needs confirmation"
        self.auto_trade = bool(on)
        self._jobs.put(("save", None))
        self.log(f"[ORDERS] Auto-trading {'ON' if on else 'OFF'}")
        return True, "Auto-trading ON" if on else "Auto-trading OFF"

    # ════════════════════════════════════════════════════════
    # called by the strategy
    # ════════════════════════════════════════════════════════
    def _lots_for(self, timeframe):
        s = self.settings
        lots = s["lots"]
        if "15M" in timeframe and s["lots_15m"]:
            lots = s["lots_15m"]
        elif "1H" in timeframe and s["lots_1h"]:
            lots = s["lots_1h"]
        return max(1, min(int(lots), int(s["max_lots"])))

    def _check_entry(self, trade, inst):
        s = self.settings
        tf = str(trade.get("Timeframe", ""))
        if not self.auto_trade:
            return "auto-trading off"
        if not self.connected:
            return "broker not connected"
        if not (("15M" in tf and s["trade_15m"]) or ("1H" in tf and s["trade_1h"])):
            return f"timeframe {tf or '-'} not enabled"
        if trade.get("Action") == "SELL" and not s["take_sell_setups"]:
            return "SELL setups are off"
        why = strike_selection_reason(s, trade, inst)
        if why:
            return why
        if not self.market_open() or self.clock().time() >= ENTRY_CUTOFF:
            return "outside entry hours"
        if not inst.get("lot_size") or int(inst["lot_size"]) <= 0:
            return "lot size unknown"
        active = [p for p in self.positions.values() if p["date"] == self._today() and p["state"] != "FAILED"]
        if len(active) >= int(s["max_trades"]):
            return f"max {s['max_trades']} trades for today reached"
        if self.realized_rs <= -abs(float(s["max_loss_rs"])):
            return f"max loss ₹{s['max_loss_rs']} for today reached"
        tok = str(inst.get("token"))
        if any(p["state"] in OPEN_STATES and str(p["inst"].get("token")) == tok for p in self.positions.values()):
            return "already holding this contract"
        return ""

    def enter(self, trade, inst):
        """
        trade: the forward-test trade dict. inst: {symbol, exchange, token, tsym, expiry, strike, opt_type, lot_size}.
        Returns a short status for trade['Order_ID'].
        """
        if not self.auto_trade:
            return "NO ORDER (auto-trading off)"
        self._jobs.put(("new", (dict(trade), dict(inst))))
        return "QUEUED"

    def _job_new(self, arg):
        trade, inst = arg
        tid = trade.get("Trade_ID")
        if tid in self.positions:
            return
        why = self._check_entry(trade, inst)
        if why:
            self.skipped[tid] = why
            self.log(f"[ORDERS] No order for {inst.get('tsym')}: {why}")
            # every setup is recorded: the skipped ones go to the order log / sheet with the reason
            self._record({"inst": inst}, trade.get("Action", ""), 0, trade.get("Entry_Price", 0),
                         "-", "NO ORDER", f"{why} | {trade.get('Timeframe', '')}")
            return
        if True:
            lots = self._lots_for(str(trade.get("Timeframe", "")))
            pos = {
                "trade_id": tid, "date": self._today(), "broker": self.broker_name,
                "inst": dict(inst), "side": trade["Action"], "qty": lots * int(inst["lot_size"]),
                "lots": lots, "timeframe": trade.get("Timeframe", ""),
                "signal_px": float(trade.get("Entry_Price", 0)), "sl_want": float(trade.get("SL", 0) or 0),
                "state": "OPENING", "entry_oid": "", "entry_tag": self._tag("E"), "entry_px": 0.0,
                "filled": 0, "placed_at": time.time(), "chased": False,
                "sl_oid": "", "sl_tag": "", "sl_trigger": 0.0,
                "exit_oid": "", "exit_tag": "", "exit_px": 0.0, "exit_reason": "", "exit_tries": 0,
                "exit_placed_at": 0.0, "exit_chased": False, "note": "",
            }
            self.positions[tid] = pos
        self._job_enter(pos)

    def on_tick(self, trade, ltp=0.0):
        """Cheap; called every tick for an open trade. Moves the broker SL when the strategy trails it."""
        tid = trade.get("Trade_ID")
        pos = self.positions.get(tid)
        if not pos or pos["state"] != "OPEN":
            return
        pos["last_ltp"] = float(ltp or 0)
        want = float(trade.get("SL", 0) or 0)
        if want and abs(want - pos["sl_want"]) >= 0.05:
            pos["sl_want"] = want
            self._jobs.put(("sl_sync", tid))

    def exit(self, trade, reason, ltp=0.0):
        tid = trade.get("Trade_ID")
        pos = self.positions.get(tid)
        if not pos and tid not in self._pending_new():
            return "NO_ACTIVE_ORDER"
        self._jobs.put(("exit_req", (tid, reason, float(ltp or 0))))
        return "EXIT QUEUED"

    def _pending_new(self):
        return {a[0].get("Trade_ID") for j, a in list(self._jobs.queue) if j == "new"}

    def _job_exit_req(self, arg):
        tid, reason, ltp = arg
        pos = self.positions.get(tid)
        if not pos or pos["state"] not in ("OPENING", "OPEN"):
            return
        pos["exit_reason"] = reason
        if ltp:
            pos["last_ltp"] = ltp
        pos["state"] = "CLOSING"
        self._job_exit(pos)

    def pop_forced_close(self, trade_id):
        """A close that happened at the broker (SL filled, rejected entry, kill switch). Used once."""
        with self._flock:
            return self._forced.pop(trade_id, None)

    def kill(self):
        """Stop everything: auto-trading off, pending orders cancelled, open positions exited."""
        self.auto_trade = False
        n = sum(1 for p in list(self.positions.values()) if p["state"] in ("OPENING", "OPEN"))
        self._jobs.put(("kill", None))
        self.alert("🛑 KILL SWITCH", f"Auto-trading off; exiting {n} position(s)")
        return n

    def _job_kill(self, _):
        for pos in list(self.positions.values()):
            if pos["state"] in ("OPENING", "OPEN"):
                pos["exit_reason"] = "Kill switch"
                pos["state"] = "CLOSING"
                try:
                    self._job_exit(pos)
                except Exception as e:
                    self.log(f"[ORDERS] Kill exit failed for {pos['trade_id']}: {e}")

    def summary(self):
        try:
            today = [p for p in list(self.positions.values()) if p["date"] == self._today()]
        except RuntimeError:
            today = []
        if True:
            return {
                "broker": BROKER_LABELS.get(self.broker_name or self.settings["broker"], "-"),
                "connected": self.connected,
                "auto": self.auto_trade,
                "trades": sum(1 for p in today if p["state"] != "FAILED"),
                "open": sum(1 for p in today if p["state"] in OPEN_STATES),
                "pnl_rs": round(self.realized_rs + self._open_pnl(), 2),
                "status": self.status_msg,
                "note": self._limit_note,
            }

    def _open_pnl(self):
        tot = 0.0
        for p in list(self.positions.values()):
            if p["state"] == "OPEN" and p.get("last_ltp") and p["entry_px"]:
                sgn = 1 if p["side"] == "BUY" else -1
                tot += sgn * (p["last_ltp"] - p["entry_px"]) * p["filled"]
        return tot

    def order_ref(self, trade_id):
        """What the forward-test sheet shows in Order_ID: the broker order id, or why there is none."""
        p = self.positions.get(trade_id)
        if p:
            return p.get("entry_oid") or ""
        why = self.skipped.get(trade_id)
        return f"No order: {why}" if why else ""

    def order_status_for(self, trade_id):
        p = self.positions.get(trade_id)
        if not p:
            why = self.skipped.get(trade_id)
            return f"No order: {why}" if why else ""
        return {"OPENING": "Pending", "OPEN": "Filled", "CLOSING": "Exiting",
                "CLOSED": "Closed", "FAILED": "Rejected"}.get(p["state"], p["state"])

    # ════════════════════════════════════════════════════════
    # internals
    # ════════════════════════════════════════════════════════
    def _tag(self, leg):
        self._seq = (self._seq + 1) % 100
        return f"PV{self.clock().strftime('%y%m%d%H%M%S')}{self._seq:02d}{leg}"   # 17 chars

    def _record(self, pos, action, qty, price, oid, status, remark=""):
        inst = pos["inst"]
        row = {"time": self.clock().strftime("%H:%M:%S"), "contract": inst.get("tsym", ""),
               "action": action, "qty": qty, "price": round(float(price or 0), 2),
               "order_id": oid, "status": status, "remark": remark}
        self.recent.append(row)
        del self.recent[:-50]
        try:
            self.order_logger(mode=f"LIVE-{self.broker_name}", symbol=inst.get("symbol", ""),
                              opt_type=inst.get("opt_type", ""), strike=inst.get("strike", ""),
                              token=inst.get("token", ""), action=action, qty=qty, price=float(price or 0),
                              order_id=oid, status=status, remark=remark)
        except Exception as e:
            self.log(f"[ORDERS] Order log write failed: {e}")

    def _force_close(self, pos, px, reason):
        with self._flock:
            self._forced[pos["trade_id"]] = (round(float(px or 0), 2), reason)

    def _book_pnl(self, pos):
        if pos["entry_px"] and pos["exit_px"] and pos["filled"]:
            sgn = 1 if pos["side"] == "BUY" else -1
            self.realized_rs += sgn * (pos["exit_px"] - pos["entry_px"]) * pos["filled"]
            lim = abs(float(self.settings["max_loss_rs"]))
            if self.realized_rs <= -lim and not self._limit_note:
                self._limit_note = f"Max loss ₹{lim:,.0f} hit: no new entries today"
                self.alert("⚠️ MAX LOSS HIT", self._limit_note)

    def _worker(self):
        while True:
            job, arg = self._jobs.get()
            try:
                with self._lock:
                    if self.broker is None and job != "save":
                        continue
                    if job == "save":
                        pass
                    elif job in ("new", "exit_req", "kill"):
                        getattr(self, f"_job_{job}")(arg)
                    else:
                        pos = self.positions.get(arg)
                        if pos is None:
                            continue
                        getattr(self, f"_job_{job}")(pos)
                    self._save_state()
            except Exception as e:
                self.log(f"[ORDERS] {job} failed: {e}")
            finally:
                self._jobs.task_done()

    # ── jobs (run on the worker thread, under the lock) ─────
    def _job_enter(self, pos):
        if pos["state"] != "OPENING" or pos["entry_oid"]:
            return
        b = self.broker
        try:
            pos["inst"] = b.resolve(pos["inst"])
        except Exception as e:
            return self._entry_failed(pos, str(e))
        s = self.settings
        px = pos["signal_px"]
        if s["entry_type"] == "LIMIT":
            buf = 1 + float(s["limit_buffer_pct"]) / 100 if pos["side"] == "BUY" else 1 - float(s["limit_buffer_pct"]) / 100
            otype, price = "LIMIT", tick_round(px * buf)
        else:
            otype, price = "MARKET", 0.0
        try:
            oid = b.place(pos["inst"], pos["side"], pos["qty"], otype, price=price,
                          product=s["product"], tag=pos["entry_tag"])
        except Exception as e:
            oid = self._find_by_tag(pos["entry_tag"])     # did it reach the broker despite the error?
            if not oid:
                return self._entry_failed(pos, str(e))
        pos["entry_oid"] = oid
        pos["placed_at"] = time.time()
        self._record(pos, pos["side"], pos["qty"], price or px, oid, "PLACED",
                     f"{otype} {pos['lots']} lot(s) | {pos['timeframe']}")
        self.log(f"✅ [ORDER] {pos['side']} {pos['inst'].get('tsym')} x{pos['qty']} {otype} {price or ''} → {oid}")

    def _entry_failed(self, pos, msg):
        pos["state"] = "FAILED"
        pos["note"] = msg
        self._record(pos, pos["side"], pos["qty"], pos["signal_px"], pos["entry_oid"] or "-", "REJECTED", msg)
        self._force_close(pos, pos["signal_px"], "Order Rejected")
        self.alert(f"❌ ORDER REJECTED {pos['inst'].get('tsym')}", msg[:120])
        self.log(f"❌ [ORDER] Entry rejected {pos['inst'].get('tsym')}: {msg}")

    def _find_by_tag(self, tag):
        try:
            for oid, st in self.broker.orders().items():
                if st.tag == tag:
                    return oid
        except Exception:
            pass
        return ""

    def _sl_order_px(self, pos, trigger):
        gap = float(self.settings["sl_limit_gap_pct"]) / 100
        return tick_round(trigger * (1 - gap) if pos["side"] == "BUY" else trigger * (1 + gap))

    def _job_place_sl(self, pos):
        if pos["state"] != "OPEN" or pos["sl_oid"] or not self.settings["sl_at_broker"]:
            return
        trig = tick_round(pos["sl_want"])
        if trig <= 0:
            return
        ltp = pos.get("last_ltp") or pos["entry_px"]
        if (pos["side"] == "BUY" and trig >= ltp) or (pos["side"] == "SELL" and trig <= ltp):
            pos["note"] = "SL already crossed; app exits on the next tick"
            return
        exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
        pos["sl_tag"] = self._tag("S")
        try:
            oid = self.broker.place(pos["inst"], exit_side, pos["filled"], "SL", price=self._sl_order_px(pos, trig),
                                    trigger=trig, product=self.settings["product"], tag=pos["sl_tag"])
        except Exception as e:
            pos["note"] = f"SL order failed ({e}); app watches the SL"
            self.alert(f"⚠️ SL ORDER FAILED {pos['inst'].get('tsym')}", "The app will exit on its own SL check")
            self.log(f"⚠️ [ORDER] SL order failed {pos['inst'].get('tsym')}: {e}")
            return
        pos["sl_oid"], pos["sl_trigger"] = oid, trig
        self._record(pos, exit_side, pos["filled"], trig, oid, "SL PLACED", f"trigger {trig}")

    def _job_sl_sync(self, pos):
        if pos["state"] != "OPEN":
            return
        if not pos["sl_oid"]:
            return self._job_place_sl(pos)
        trig = tick_round(pos["sl_want"])
        if abs(trig - pos["sl_trigger"]) < 0.05:
            return
        exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
        try:
            self.broker.modify(pos["sl_oid"], pos["inst"], exit_side, pos["filled"], "SL",
                               price=self._sl_order_px(pos, trig), trigger=trig,
                               product=self.settings["product"], tag=pos["sl_tag"])
            pos["sl_trigger"] = trig
            self._record(pos, exit_side, pos["filled"], trig, pos["sl_oid"], "SL MOVED", f"trigger {trig}")
        except Exception as e:
            pos["note"] = f"SL move failed ({e}); app watches the SL"
            self.log(f"⚠️ [ORDER] SL move failed {pos['inst'].get('tsym')}: {e}")

    def _job_exit(self, pos):
        if pos["state"] != "CLOSING" or pos["exit_oid"]:
            return
        b = self.broker
        book = self._safe_orders()
        # 1. entry still pending → cancel it; exit only what was filled
        if pos["entry_oid"] and pos["filled"] < pos["qty"]:
            st = book.get(pos["entry_oid"])
            if st and st.status == PENDING:
                try:
                    b.cancel(pos["entry_oid"], pos["inst"], pos["entry_tag"])
                except Exception:
                    pass
                book = self._safe_orders()
                st = book.get(pos["entry_oid"])
            if st:
                self._apply_entry_status(pos, st, from_exit=True)
        if not pos["filled"]:
            pos["state"] = "CLOSED"
            self._record(pos, pos["side"], pos["qty"], 0, pos["entry_oid"] or "-", "CANCELLED",
                         f"entry cancelled ({pos['exit_reason']})")
            return
        # 2. broker SL already filled? then nothing to send
        if pos["sl_oid"]:
            st = book.get(pos["sl_oid"])
            if st and st.status == PENDING:
                try:
                    b.cancel(pos["sl_oid"], pos["inst"], pos["sl_tag"])
                except Exception:
                    pass
                st = self._safe_orders().get(pos["sl_oid"])
            if st and st.status == FILLED:
                return self._closed_by_sl(pos, st)
            pos["sl_oid"] = ""
        self._send_exit(pos)

    def _send_exit(self, pos):
        exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
        ltp = pos.get("last_ltp") or pos["entry_px"] or pos["signal_px"]
        buf = float(self.settings["limit_buffer_pct"]) * 4 / 100          # marketable limit: 4× the entry buffer
        price = tick_round(ltp * (1 - buf) if exit_side == "SELL" else ltp * (1 + buf))
        otype = "LIMIT"
        if pos["exit_reason"] == "Kill switch" or pos["exit_tries"] >= 1:
            otype, price = "MARKET", 0.0      # STOP, or a retry after a failed exit: go straight to market
        pos["exit_tag"] = self._tag("X")
        pos["exit_tries"] += 1
        try:
            oid = self.broker.place(pos["inst"], exit_side, pos["filled"], otype, price=price,
                                    product=self.settings["product"], tag=pos["exit_tag"])
        except Exception as e:
            oid = self._find_by_tag(pos["exit_tag"])
            if not oid:
                self._record(pos, exit_side, pos["filled"], price, "-", "EXIT FAILED", str(e))
                if pos["exit_tries"] < 3:
                    self._jobs.put(("exit", pos["trade_id"]))
                else:
                    pos["note"] = "EXIT FAILED: close this position in the broker app"
                    self.alert(f"🚨 EXIT FAILED {pos['inst'].get('tsym')}", "Close it manually in the broker app")
                return
        pos["exit_oid"], pos["exit_placed_at"], pos["exit_chased"] = oid, time.time(), False
        self._record(pos, exit_side, pos["filled"], price or ltp, oid, "EXIT PLACED", f"{otype} | {pos['exit_reason']}")

    def _job_chase(self, pos):
        """Turn a still-open LIMIT entry or exit into a MARKET order."""
        if pos["state"] == "OPENING" and pos["entry_oid"]:
            oid, side, qty, tag = pos["entry_oid"], pos["side"], pos["qty"], pos["entry_tag"]
        elif pos["state"] == "CLOSING" and pos["exit_oid"]:
            oid, side, qty, tag = pos["exit_oid"], ("SELL" if pos["side"] == "BUY" else "BUY"), pos["filled"], pos["exit_tag"]
        else:
            return
        try:
            self.broker.modify(oid, pos["inst"], side, qty, "MARKET", product=self.settings["product"], tag=tag)
            self._record(pos, side, qty, 0, oid, "MODIFIED", "unfilled LIMIT → MARKET")
        except Exception as e:
            self.log(f"[ORDERS] Could not convert {oid} to MARKET: {e}")

    # ── monitor (status polling and reconciliation) ─────────
    def _safe_orders(self):
        try:
            return self.broker.orders() if self.broker else {}
        except Exception as e:
            self.log(f"[ORDERS] Order book read failed: {e}")
            return {}

    def _apply_entry_status(self, pos, st, from_exit=False):
        if st.status == FILLED or (st.status in (CANCELLED, REJECTED) and st.filled_qty > 0):
            pos["filled"] = st.filled_qty or pos["qty"]
            pos["entry_px"] = st.avg_price or pos["signal_px"]
            if not from_exit and pos["state"] == "OPENING":
                pos["state"] = "OPEN"
                self._record(pos, pos["side"], pos["filled"], pos["entry_px"], pos["entry_oid"], "FILLED")
                self._jobs.put(("place_sl", pos["trade_id"]))
        elif st.status in (CANCELLED, REJECTED) and not from_exit:
            self._entry_failed(pos, st.message or st.status.lower())

    def _closed_by_sl(self, pos, st):
        pos["exit_oid"] = pos["sl_oid"]
        pos["exit_px"] = st.avg_price or pos["sl_trigger"]
        pos["state"] = "CLOSED"
        self._book_pnl(pos)
        self._record(pos, "SELL" if pos["side"] == "BUY" else "BUY", pos["filled"], pos["exit_px"],
                     pos["sl_oid"], "SL FILLED", pos.get("exit_reason") or "broker stop-loss")
        if not pos.get("exit_reason"):
            self._force_close(pos, pos["exit_px"], "SL Hit (broker)")

    def poll_once(self):
        with self._lock:
            live = [p for p in self.positions.values() if p["state"] in OPEN_STATES]
            if not live or self.broker is None:
                return
            book = self._safe_orders()
            now = time.time()
            for pos in live:
                if pos["state"] == "OPENING" and pos["entry_oid"]:
                    st = book.get(pos["entry_oid"])
                    if st:
                        self._apply_entry_status(pos, st)
                    if pos["state"] == "OPENING" and not pos["chased"] and self.settings["entry_type"] == "LIMIT" \
                            and now - pos["placed_at"] >= float(self.settings["chase_after_s"]):
                        pos["chased"] = True
                        self._jobs.put(("chase", pos["trade_id"]))
                elif pos["state"] == "OPEN" and pos["sl_oid"]:
                    st = book.get(pos["sl_oid"])
                    if st and st.status == FILLED:
                        self._closed_by_sl(pos, st)
                    elif st and st.status in (CANCELLED, REJECTED):
                        pos["sl_oid"] = ""
                        pos["note"] = f"Broker SL {st.status.lower()} ({st.message}); app watches the SL"
                        self.log(f"⚠️ [ORDER] {pos['note']}")
                elif pos["state"] == "CLOSING" and pos["exit_oid"]:
                    st = book.get(pos["exit_oid"])
                    if st and st.status == FILLED:
                        pos["exit_px"] = st.avg_price or pos.get("last_ltp", 0)
                        pos["state"] = "CLOSED"
                        self._book_pnl(pos)
                        self._record(pos, "SELL" if pos["side"] == "BUY" else "BUY", pos["filled"],
                                     pos["exit_px"], pos["exit_oid"], "EXIT FILLED", pos["exit_reason"])
                        if pos["exit_reason"] in ("Kill switch", "EOD 3PM (engine)"):
                            self._force_close(pos, pos["exit_px"], pos["exit_reason"])
                    elif st and st.status in (CANCELLED, REJECTED):
                        self._record(pos, "EXIT", pos["filled"], 0, pos["exit_oid"], "EXIT REJECTED", st.message)
                        pos["exit_oid"] = ""
                        if pos["exit_tries"] < 3:
                            self._jobs.put(("exit", pos["trade_id"]))
                        else:
                            pos["note"] = "EXIT FAILED: close this position in the broker app"
                            self.alert(f"🚨 EXIT FAILED {pos['inst'].get('tsym')}", "Close it manually in the broker app")
                    elif st and not pos["exit_chased"] and now - pos["exit_placed_at"] >= float(self.settings["chase_after_s"]):
                        pos["exit_chased"] = True
                        self._jobs.put(("chase", pos["trade_id"]))
            # safety net: 3 PM square-off of anything the strategy did not close
            if self.clock().time() >= ENTRY_CUTOFF:
                for pos in live:
                    if pos["state"] in ("OPENING", "OPEN"):
                        pos["exit_reason"] = "EOD 3PM (engine)"
                        pos["state"] = "CLOSING"
                        self._jobs.put(("exit", pos["trade_id"]))
            self._save_state()

    def _sync_with_broker(self, startup=False):
        """Compare our open positions with the broker's. Two misses in a row = closed outside the app."""
        with self._lock:
            live = [p for p in self.positions.values() if p["state"] == "OPEN"]
            if not live or self.broker is None:
                return
            try:
                held = self.broker.positions()
            except Exception as e:
                self.log(f"[ORDERS] Position check failed: {e}")
                return
            for pos in live:
                iid = str(pos["inst"].get("inst_id", pos["inst"].get("token")))
                if held.get(iid):
                    self._missing.pop(pos["trade_id"], None)
                    continue
                n = self._missing.get(pos["trade_id"], 0) + 1
                self._missing[pos["trade_id"]] = n
                if n >= 2 or startup:
                    if pos["sl_oid"]:
                        st = self._safe_orders().get(pos["sl_oid"])
                        if st and st.status == FILLED:
                            self._closed_by_sl(pos, st)
                            continue
                        try:
                            self.broker.cancel(pos["sl_oid"], pos["inst"], pos["sl_tag"])
                        except Exception:
                            pass
                    pos["state"] = "CLOSED"
                    pos["exit_px"] = pos.get("last_ltp") or pos["entry_px"]
                    self._book_pnl(pos)
                    self._record(pos, "-", pos["filled"], pos["exit_px"], "-", "CLOSED AT BROKER",
                                 "position no longer open at the broker")
                    self._force_close(pos, pos["exit_px"], "Closed at broker")
            self._save_state()

    def _monitor(self):
        while True:
            time.sleep(self.poll_s)
            try:
                self.poll_once()
                if time.time() - self._last_reconcile >= self.reconcile_s:
                    self._last_reconcile = time.time()
                    self._sync_with_broker()
            except Exception as e:
                self.log(f"[ORDERS] Monitor error: {e}")
