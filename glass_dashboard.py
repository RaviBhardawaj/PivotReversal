#!/usr/bin/env python3
"""
Glass Dashboard bridge: serves the glass web page (glass_web/) and connects it to the
launcher's control logic (ControlPanel in Options-angel_live-ok.py).

glass_server.py runs that ControlPanel without a window (headless_tk.py) and calls start()
here. The page shows the same feed, strategy state, reversal rows and order engine the launcher
would, and its buttons call the same handlers, so nothing here re-implements the strategy.

The server binds to 127.0.0.1 only and uses the standard library only (no new packages).
The existing web_server.py dashboard and the Tk launcher are separate and are not touched.
"""

import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import deleted_strikes   # dashboard deletes, kept on disk until restored

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "glass_web")
DEFAULT_PORT = 8780
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon"}

NEXT_DAY_MODES = [
    ("PLAN", "📋 Next-day plan setups"),
    ("15M", "⭐ 15M intraday (2 CE + 2 PE)"),
    ("1H", "⏳ 1H structure (2 CE + 2 PE)"),
    ("BOTH", "🎯 Both 15M & 1H focus"),
    ("ALL", "All mapped strikes"),
]
NEXT_DAY_COLS = ["Focus Group", "Action / Setup", "Type", "Strike", "Expiry", "LTP",
                 "Planned Entry", "Planned SL", "LL Price", "HH Price", "T1", "T2 (Pivot P)", "T3", "T4", "R:R",
                 "S3", "S2", "S1", "P", "R1", "R2", "R3"]


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt(v):
    v = _f(v)
    return f"{v:.2f}" if v else "-"


_LAUNCHER = {"at": 0.0, "pids": []}


def launcher_pids():
    """PIDs of a running Tk launcher (Options-angel_live-ok.py), checked at most every 5 seconds."""
    if time.time() - _LAUNCHER["at"] < 5:
        return _LAUNCHER["pids"]
    pids = []
    try:
        out = subprocess.run(["pgrep", "-f", "Options-angel_live-ok.py"], capture_output=True, text=True, timeout=3).stdout
        me = os.getpid()
        pids = [int(x) for x in out.split() if x.strip().isdigit() and int(x) != me]
    except Exception:
        pass
    _LAUNCHER.update(at=time.time(), pids=pids)
    return pids


LAUNCHER_MSG = ("The Python launcher (Options-angel_live-ok.py) is running. Close it first: running both "
                "would stream the same feed twice and write the same sheet and orders twice.")


class GlassDashboard:
    """Bridges the HTTP server and the launcher's ControlPanel (`app`) and module (`m`)."""

    def __init__(self, app, m):
        self.app = app
        self.m = m
        self.snap = {"ready": False}
        self.snap_lock = threading.Lock()
        self.notes = []            # messages for the web page (gap check, saves, errors)
        self.notes_lock = threading.Lock()

    # ── Tk thread helpers ───────────────────────────────────
    def on_tk(self, fn, timeout=8.0):
        """Run fn on the Tk main thread and return its result (Tk is not thread-safe)."""
        ev, box = threading.Event(), {}

        def run():
            try:
                box["r"] = fn()
            except Exception as e:      # handed back to the HTTP thread
                box["e"] = e
            finally:
                ev.set()
        self.app.root.after(0, run)    # ControlPanel routes off-thread after() through its queue
        if not ev.wait(timeout):
            raise TimeoutError("The launcher window is busy; try again")
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def note(self, text, kind="info"):
        with self.notes_lock:
            self.notes.append({"t": time.time(), "text": text, "kind": kind})
            del self.notes[:-30]

    # ── snapshot of the Tk window (taken on the Tk thread) ──
    def start_snapshots(self):
        def tick():
            try:
                s = self._take_snapshot()
                with self.snap_lock:
                    self.snap = s
            except Exception as e:
                if time.time() - getattr(self, "_snap_err_at", 0) > 30:
                    self._snap_err_at = time.time()
                    print(f"[GLASS] snapshot note: {e}")
            finally:
                self.app._tk_after(500, tick)
        self.app._tk_after(500, tick)

    def _take_snapshot(self):
        a, m = self.app, self.m
        tree = a.tree
        cols = list(tree["columns"])
        rows = []
        for iid in tree.get_children():
            vals = tree.item(iid, "values")
            tags = tree.item(iid, "tags")
            rows.append({"v": dict(zip(cols, [str(x) for x in vals])), "tag": (tags[0] if tags else "")})
        filters = {}
        for (grp, key), var in a.dash_filter_vars.items():
            filters.setdefault(grp, [])
            if var.get():
                filters[grp].append(key)
        try:
            status_color = a.status_lbl.cget("fg")
        except Exception:
            status_color = "#3fb950"
        return {
            "ready": True,
            "controls": {
                "exchange": a.exchange_var.get(),
                "exchanges": list(m.EXCHANGE_CONFIG.keys()),
                "symbol": a.symbol_var.get(),
                "symbols": list(a.sym_cb["values"] or []),
                "expiry": a.expiry_var.get(),
                "expiries": list(a.exp_cb["values"] or []),
                "next_expiry": a.next_exp_var.get(),
                "use_next": bool(a.use_next_exp.get()),
                "strikes": int(a.strikes_var.get()),
            },
            "start_enabled": getattr(a.top_start_btn, "_state", "normal") == "normal",
            "stop_enabled": getattr(a.top_stop_btn, "_state", "disabled") == "normal",
            "status": {"text": a.status_var.get(), "color": str(status_color)},
            "spot_info": a.spot_info.get(),
            "sec_15m": a.sec_15m_info.get(),
            "sec_1h": a.sec_1h_info.get(),
            "max_oi": a.max_oi_info.get(),
            "filters": filters,
            "exp_cur": bool(a.exp_cur_var.get()),
            "exp_next": bool(a.exp_next_var.get()),
            "show_plan": bool(a.show_plan_cols_var.get()),
            "filter_info": a.dash_filter_info.get(),
            "columns": cols,
            "rows": rows,
            "level_source": a.level_source_var.get(),
            "ft_filter": a.ft_tf_filter_var.get(),
            "excluded_strikes": sorted(list(getattr(m.feed, "excluded_strikes", set()))),
            "taken_at": time.time(),
        }

    # ── live data read straight from the feed and order engine ──
    def live_state(self):
        m = self.m
        feed = m.feed
        with self.snap_lock:
            snap = dict(self.snap)
        sym = (snap.get("controls") or {}).get("symbol") or "NIFTY"
        with feed.lock:
            spot = _f(feed.spot_price.get(sym, 0))
            zone = feed.spot_zone.get(sym, "")
            mtf = dict(feed.mtf_pivots or {})
            mkt_dir = dict(feed.market_direction.get(sym, {}) or {})
            h1b = dict(feed.h1_bias.get(sym, {}) or {})
        f15 = dict(feed.focus_15m or (feed.daily_focus or {}).get("15M", {}) or {})
        f1h = dict(feed.focus_1h or (feed.daily_focus or {}).get("1H", {}) or {})
        pivots = []
        for tf in ("Weekly", "Daily", "1-Hour", "15-Min"):
            tp = mtf.get(tf) or {}
            row = {"tf": tf, "zone": "--"}
            if tp:
                try:
                    row["zone"] = m.get_pivot_zone_detail(spot, tp)["zone"] if spot > 0 else "--"
                except Exception:
                    pass
                for k in ("S3", "S2", "S1", "P", "R1", "R2", "R3"):
                    row[k] = _f(tp.get(k))
            pivots.append(row)

        def focus(f):
            p = f.get("p_strikes") or ([f["P_STRIKE"]] if f.get("P_STRIKE") else [])
            return {"ce": [x for x in (f.get("CE1"), f.get("CE2")) if x], "pe": [x for x in (f.get("PE1"), f.get("PE2")) if x],
                    "p": list(p)}
        try:
            eng = m.live_order_manager.engine
            orders = eng.summary()
            orders["max_trades"] = eng.settings.get("max_trades")
            orders["max_loss_rs"] = eng.settings.get("max_loss_rs")
            orders["lots"] = eng.settings.get("lots")
        except Exception as e:
            orders = {"error": str(e)}
        with self.notes_lock:
            notes = list(self.notes)
        # Deleted strikes are hidden here (and get no new entries in ForwardTestTracker.log_entry)
        dkeys = deleted_strikes.active_keys()
        snap["rows"] = [r for r in (snap.get("rows") or [])
                        if deleted_strikes.key(sym, (r.get("v") or {}).get("Expiry"), (r.get("v") or {}).get("Strike"),
                                               (r.get("v") or {}).get("Type")) not in dkeys]
        snap.update({
            "now": datetime.now().strftime("%H:%M:%S"),
            "market_open": bool(m.is_live_market_open()),
            "feed_running": bool(getattr(feed, "running", False)),
            "launcher_running": bool(launcher_pids()),
            "spot": {"symbol": sym, "price": spot, "zone": zone, "direction": mkt_dir.get("direction", ""),
                     "dir_color": mkt_dir.get("color", ""), "dir_desc": mkt_dir.get("desc", ""),
                     "h1_bias": h1b.get("bias", ""), "h1_zone": h1b.get("zone", ""), "h1_action": h1b.get("action", "")},
            "pivots": pivots,
            "focus": {"15M": focus(f15), "1H": focus(f1h)},
            "orders": orders,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{m.SPREADSHEET_ID}/edit",
            "level_sources": list(m.LEVEL_SOURCES.values()),
            "filter_groups": [{"group": g, "options": [{"key": k, "label": t} for k, t in opts]}
                              for g, opts in m.DASH_FILTER_GROUPS],
            "excluded_strikes": self._excluded_list(),
            "deleted_strikes": deleted_strikes.active(),
            "notes": notes,
        })
        return snap

    # ── actions: each calls the launcher's own handler on the Tk thread ──
    def set_controls(self, body):
        a = self.app

        def run():
            if "exchange" in body and body["exchange"] != a.exchange_var.get():
                a.exchange_var.set(body["exchange"])
                a._on_exchange_change()
            if "symbol" in body and str(body["symbol"]).strip().upper() != a.symbol_var.get():
                a.symbol_var.set(str(body["symbol"]).strip().upper())
                a._on_symbol_entered()
            if "expiry" in body and body["expiry"] != a.expiry_var.get():
                a.expiry_var.set(body["expiry"])
                a._on_expiry_selected()
            if "next_expiry" in body and body["next_expiry"] != a.next_exp_var.get():
                a.next_exp_var.set(body["next_expiry"])
                a._on_next_expiry_selected()
            if "use_next" in body:
                a.use_next_exp.set(bool(body["use_next"]))
                a._on_use_next_exp_toggled()   # updates which expiries may get orders
            if "strikes" in body:
                n = max(3, min(50, int(body["strikes"])))
                a.strikes_var.set(n)
                a.slbl.config(text=str(n))
        self.on_tk(run, timeout=30)
        return {"ok": True}

    def start(self):
        a = self.app

        def run():
            if launcher_pids():
                return {"ok": False, "error": LAUNCHER_MSG}
            if not a.expiry_var.get():
                return {"ok": False, "error": "Please select an expiry"}
            if getattr(a.top_start_btn, "_state", "normal") != "normal":
                return {"ok": False, "error": "The scanner is already starting or running"}
            a.start_feed()
            return {"ok": True}
        return self.on_tk(run)

    def stop(self):
        def run():
            self.app.stop_feed()
            return {"ok": True}
        return self.on_tk(run)

    def set_filters(self, body):
        a = self.app

        def run():
            if body.get("clear"):
                a._clear_dash_filters()
                return {"ok": True}
            for g in body.get("filters", {}) or {}:
                ticked = set(body["filters"][g] or [])
                for (grp, key), var in a.dash_filter_vars.items():
                    if grp == g:
                        var.set(key in ticked)
            if "exp_cur" in body:
                a.exp_cur_var.set(bool(body["exp_cur"]))
            if "exp_next" in body:
                a.exp_next_var.set(bool(body["exp_next"]))
            if "show_plan" in body:
                a.show_plan_cols_var.set(bool(body["show_plan"]))
            a._on_dash_filter_changed()
            return {"ok": True}
        return self.on_tk(run)

    def auto_trade(self, body):
        eng = self.m.live_order_manager.engine
        if not body.get("on"):
            ok, msg = eng.set_auto(False)
            return {"ok": ok, "message": msg}
        if launcher_pids():
            return {"ok": False, "error": LAUNCHER_MSG}
        if not eng.connected:
            return {"ok": False, "error": f"{eng.status_msg}\n\nOpen ⚙ Settings → Broker to connect."}
        if not body.get("confirmed"):
            s = eng.settings
            tfs = " and ".join(t for t, on in (("15M", s["trade_15m"]), ("1H", s["trade_1h"])) if on) or "no"
            return {"ok": False, "confirm": (
                f"Real orders will be sent to {self.m.BROKER_LABELS.get(eng.broker_name, eng.broker_name)} "
                f"for every {tfs} entry.\n\nLots per trade: {s['lots']}   Max trades/day: {s['max_trades']}   "
                f"Max loss/day: ₹{s['max_loss_rs']:,}\nSELL setups: {'traded' if s['take_sell_setups'] else 'skipped'}   "
                f"Stop-loss at broker: {'yes' if s['sl_at_broker'] else 'no'}\n\nTurn on auto-trading?")}
        ok, msg = eng.set_auto(True, confirmed=True)
        return {"ok": ok, "message": msg}

    def kill(self, body):
        eng = self.m.live_order_manager.engine
        n = sum(1 for p in list(eng.positions.values()) if p["state"] in ("OPENING", "OPEN"))
        if not body.get("confirmed"):
            return {"ok": False, "confirm": (f"Auto-trading goes OFF, pending orders are cancelled and "
                                             f"{n} open position(s) are exited at market.\n\nContinue?")}
        eng.kill()
        return {"ok": True, "message": "STOP sent: auto-trading off, open positions exiting"}

    def level_source(self, body):
        a = self.app
        label = body.get("label", "")
        if label not in self.m.LEVEL_SOURCES.values():
            return {"ok": False, "error": "Unknown levels source"}

        def run():
            a.level_source_var.set(label)
            a._on_level_source_change()
        self.on_tk(run)
        return {"ok": True}

    def map_levels(self):
        self.on_tk(self.app._on_map_strike_levels_click)
        return {"ok": True, "message": "Mapping strike levels; the status line shows progress"}

    def ft_filter(self, body):
        v = body.get("value", "ALL")
        if v not in ("ALL", "15M Intraday", "1H Structure"):
            return {"ok": False, "error": "Unknown filter"}
        self.on_tk(lambda: self.app.ft_tf_filter_var.set(v))
        feed = self.m.feed
        feed.ft_timeframe_filter = v
        threading.Thread(target=feed.sync_forward_test_to_sheet, daemon=True).start()
        return {"ok": True}

    def gap_check(self):
        sym = self.snap.get("controls", {}).get("symbol") or "NIFTY"
        try:
            from eod_planner import detect_gap
            res = detect_gap(sym)
        except Exception as e:
            return {"ok": True, "title": "Gap Check", "message": f"Gap check info: {e}"}
        if not res:
            return {"ok": True, "title": f"{sym} Gap Detection", "message": "No gap data yet (available after 09:15)."}
        gt, gp = res.get("type", "NORMAL"), _f(res.get("gap_pct"))
        msg = f"{sym} Open Gap: {gp:+.2f}% ({gt})\n\n"
        if gt == "GAP_UP":
            msg += "Market opened higher -> Watch for R2 Rejection (SELL setups) first."
        elif gt == "GAP_DOWN":
            msg += "Market opened lower -> Watch for S2 Reclaim (BUY setups) first."
        else:
            msg += "Normal open -> All planned pivot levels are valid as-is."
        return {"ok": True, "title": f"{sym} Gap Detection", "message": msg, "gap": res}

    # ── next-day plan (same sources and rows as the launcher's Next-Day Plan window) ──
    def next_day_plan(self, mode="", inc_s3=True, refresh=False):
        m, feed = self.m, self.m.feed
        c = self.snap.get("controls", {})
        sym, exp = c.get("symbol") or "NIFTY", c.get("expiry") or ""
        cfg = m.EXCHANGE_CONFIG.get(next((k for k, v in m.EXCHANGE_CONFIG.items() if sym in v["symbols"]), "NFO"), {})
        step = cfg.get("steps", {}).get(sym, 50)
        mtf_all = feed.mtf_pivots or m.get_tv_multi_timeframe_pivots(sym)
        focus = feed.daily_focus or m.select_daily_4_focus_strikes(mtf_all, step)
        f15, f1h = focus.get("15M", {}), focus.get("1H", {})
        nd_plan = m.load_next_day_plan(sym) or {}
        if refresh:
            strikes_dict = feed.fetch_and_map_all_strikes(sym, exp, force_fetch=True) or {}
            cdata = {}
        else:
            cdata = m.read_json_file(m.STRIKE_PIVOT_NEXT_CACHE)
            if not (cdata.get("symbol") == sym and cdata.get("date", "") > datetime.now().strftime("%Y-%m-%d")):
                cdata = m.read_json_file(m.STRIKE_PIVOT_CACHE)
            strikes_dict = cdata.get("strikes", {}) or {}
            if not strikes_dict:
                strikes_dict = feed.fetch_and_map_all_strikes(sym, exp, force_fetch=False) or {}
        mode = mode or ("PLAN" if nd_plan.get("setups") else "15M")

        def strike_data(ot, stk):
            for s in strikes_dict.values():
                if (s.get("opt_type") == ot and int(_f(s.get("strike"))) == int(_f(stk))
                        and (not exp or s.get("expiry") == exp)):
                    return s
            return None

        def ltp_str(ot, stk):
            try:
                v = feed.get_strike_ltp(ot, int(_f(stk)), exp)
                if v <= 0:
                    sd = strike_data(ot, stk)
                    if sd:
                        v = _f(sd.get("prev_c") or sd.get("close") or sd.get("P"))
                return f"{v:.2f}" if v > 0 else "-"
            except Exception:
                return "-"

        def lv(sd):
            return [_fmt(sd.get(k)) if sd else "-" for k in ("S3", "S2", "S1", "P", "R1", "R2", "R3")]

        rows = []

        dkeys = deleted_strikes.active_keys()

        def add(vals, ot):
            _v = dict(zip(NEXT_DAY_COLS, [str(x) for x in vals]))
            if deleted_strikes.key(sym, _v.get("Expiry"), _v.get("Strike"), ot) in dkeys:
                return                          # deleted on the dashboard
            rows.append({"v": dict(zip(NEXT_DAY_COLS, [str(x) for x in vals])), "type": ot})

        def plan_setup_row(fg, stp, sd, ll, hh):
            ot = stp.get("type", "CE")
            add([fg, stp.get("setup") or f"{stp.get('side')} ({stp.get('calculated_level', '')})", ot,
                 stp.get("strike", 0), str(stp.get("expiry") or exp or ""), ltp_str(ot, stp.get("strike", 0)),
                 _fmt(stp.get("planned_ep") or stp.get("entry_price")), _fmt(stp.get("sl")), _fmt(ll), _fmt(hh),
                 _fmt(stp.get("target_1")), _fmt(stp.get("target_2")), _fmt(stp.get("target_3")),
                 _fmt(stp.get("target_4")), str(stp.get("rr", "-"))] + lv(sd), ot)

        if mode == "PLAN" and nd_plan.get("setups"):
            for stp in nd_plan.get("setups", []):
                sl = _f(stp.get("sl"))
                plan_setup_row(f"Plan ({stp.get('timeframe', 'Intraday')})", stp,
                               strike_data(stp.get("type", "CE"), stp.get("strike", 0)),
                               sl if stp.get("side") == "BUY" else 0, sl if stp.get("side") == "SELL" else 0)
        else:
            wanted = []
            if mode in ("15M", "BOTH"):
                wanted.append(("15M Intraday", f15))
            if mode in ("1H", "BOTH"):
                wanted.append(("1H Structure", f1h))
            if mode == "ALL":
                both = set(f15.get("all_4", [])) & set(f1h.get("all_4", []))
                items = sorted(strikes_dict.values(), key=lambda x: (_f(x.get("strike")), x.get("opt_type", "")))
                for it in items:
                    stk = it.get("strike", 0)
                    fg = ("⭐15M & ⏳1H" if stk in both else "⭐15M Intraday" if stk in set(f15.get("all_4", []))
                          else "⏳1H Structure" if stk in set(f1h.get("all_4", [])) else "All Mapped")
                    wanted.append((fg, {"_one": (it.get("opt_type", "CE"), stk)}))
            seen = set()
            for grp, f in wanted:
                if "_one" in f:
                    combos = [f["_one"]]
                else:
                    keys = ["CE1", "CE2"] + (["CE3"] if inc_s3 else []) + ["PE1", "PE2"] + (["PE3"] if inc_s3 else [])
                    combos = [(k[:2], f.get(k)) for k in keys if f.get(k)]
                for ot, stk in combos:
                    for role in ("★ BUY (S2 Reclaim)", "▼ SELL (R2 Reject)"):
                        if (grp, role, ot, stk) in seen:
                            continue
                        seen.add((grp, role, ot, stk))
                        sd = strike_data(ot, stk)
                        if not sd:
                            add([grp, role, ot, stk, exp, ltp_str(ot, stk)] + ["-"] * 9 + ["-"] * 7, ot)
                            continue
                        s3, s2, s1, p, r1, r2, r3 = (_f(sd.get(k)) for k in ("S3", "S2", "S1", "P", "R1", "R2", "R3"))
                        if "BUY" in role:
                            entry, sl = s2, (s3 if s3 > 0 else round(s2 * 0.90, 2))
                            t = (s1, p, r1, r2)
                            rr = f"{((t[1] - entry) / max(entry - sl, 0.5)):.1f}:1" if entry > sl else "-"
                        else:
                            entry, sl = r2, (r3 if r3 > 0 else round(r2 * 1.10, 2))
                            t = (r1, p, s1, s2)
                            rr = f"{((entry - t[1]) / max(sl - entry, 0.5)):.1f}:1" if sl > entry else "-"
                        add([grp, role, ot, stk, sd.get("expiry") or exp, ltp_str(ot, stk), _fmt(entry), _fmt(sl),
                             "-", "-"] + [_fmt(x) for x in t] + [rr] + lv(sd), ot)

        piv15 = nd_plan.get("levels_15m") or mtf_all.get("15-Min", {}) or {}
        piv1h = nd_plan.get("levels_1h") or mtf_all.get("1-Hour", {}) or {}
        return {
            "ok": True, "symbol": sym, "mode": mode, "modes": [{"key": k, "label": t} for k, t in NEXT_DAY_MODES],
            "session": nd_plan.get("target_trading_date") or nd_plan.get("generated_date") or cdata.get("date", datetime.now().strftime("%Y-%m-%d")),
            "status": nd_plan.get("status", "ACTIVE") if nd_plan else "LIVE CALCULATED",
            "ref_spot": _f(nd_plan.get("spot")),
            "piv15": {k: _f(piv15.get(k)) for k in ("S3", "S2", "S1", "P", "R1", "R2", "R3")},
            "piv1h": {k: _f(piv1h.get(k)) for k in ("S3", "S2", "S1", "P", "R1", "R2", "R3")},
            "focus_ce": (nd_plan.get("resistance") or {}).get("focus_ce", []),
            "focus_pe": (nd_plan.get("support") or {}).get("focus_pe", []),
            "columns": NEXT_DAY_COLS, "rows": rows,
        }

    # ── settings (same four tabs as the launcher's ⚙ Settings window) ──
    def settings(self):
        m = self.m
        eng = m.live_order_manager.engine
        brokers = {}
        for code, cls in m.ADAPTERS.items():
            keys = []
            for k in cls.env_keys:
                secret = k in cls.secret_keys
                cur = os.getenv(k, "") or ""
                keys.append({"key": k, "secret": secret, "saved": bool(cur), "value": "" if secret else cur})
            brokers[code] = {"label": m.BROKER_LABELS.get(code, code), "keys": keys}
        s = dict(eng.settings)
        return {"ok": True, "broker": eng.broker_name or s.get("broker"), "brokers": brokers,
                "status_msg": eng.status_msg, "connected": eng.connected, "auto": eng.auto_trade,
                "settings": s, "timeframes": list(m.TIMEFRAMES), "levels": list(m.ALL_LEVELS),
                "level_labels": dict(m.LEVEL_LABELS),
                "levels_on": sorted(m.tf_levels(x for x in str(s.get("levels", "")).split(",")))}

    def save_keys(self, body):
        m = self.m
        cls = m.ADAPTERS.get(body.get("broker", ""))
        if not cls:
            return {"ok": False, "error": "Unknown broker"}
        upd = {}
        for k in cls.env_keys:
            v = str((body.get("keys") or {}).get(k, "") or "").strip()
            if v or k not in cls.secret_keys:     # a blank secret keeps the saved one
                upd[k] = v
        m._write_env(upd)
        return {"ok": True}

    def broker_test(self, body):
        self.save_keys(body)
        eng = self.m.live_order_manager.engine
        sample = None
        try:
            sample = self.on_tk(self.app._sample_order_instrument)
        except Exception:
            pass
        ok, msg = eng.test_connection(body.get("broker"), sample)
        return {"ok": ok, "message": ("✅ " if ok else "❌ ") + msg}

    def broker_use(self, body):
        eng = self.m.live_order_manager.engine
        if eng.auto_trade:
            return {"ok": False, "message": "Turn auto-trading off before changing broker."}
        self.save_keys(body)
        ok, msg = eng.connect(body.get("broker"))
        return {"ok": ok, "message": ("✅ " if ok else "❌ ") + msg, "broker": eng.broker_name}

    def save_settings(self, body):
        allowed = {"lots", "lots_15m", "lots_1h", "entry_type", "limit_buffer_pct", "chase_after_s", "product",
                   "sl_at_broker", "sl_limit_gap_pct", "take_sell_setups", "trade_15m", "trade_1h",
                   "max_trades", "max_loss_rs", "levels", "opt_types", "strike_tf", "symbols", "strikes"}
        new = {k: v for k, v in (body or {}).items() if k in allowed}
        try:
            self.m.live_order_manager.engine.save_settings(new)
        except Exception as e:
            return {"ok": False, "message": f"❌ {e}"}
        return {"ok": True, "message": "✅ Saved. Applies to the next entry."}

    def strikes_preview(self, sel):
        """Today's strategy strikes and whether each gets real orders (the Strikes tab preview)."""
        m, feed = self.m, self.m.feed
        df = getattr(feed, "daily_focus", None) or {}
        focus = {"15M": feed.focus_15m or df.get("15M", {}) or {}, "1H": feed.focus_1h or df.get("1H", {}) or {}}
        sym = self.snap.get("controls", {}).get("symbol", "") or ""
        both = set(focus["15M"].get("all_4", [])) & set(focus["1H"].get("all_4", []))
        out = []
        for tf in m.TIMEFRAMES:
            f = focus[tf]
            items = []
            for stk in sorted(set(f.get("all_4", []))):
                lvs = m.strike_levels(stk, f)
                types = ("CE", "PE") if (lvs & {"P", "PN"}) or not lvs else \
                    tuple(t for t in ("CE", "PE") if (t == "CE" and lvs & {"R1", "R2"}) or (t == "PE" and lvs & {"S1", "S2"}))
                trade_tf = "15M / 1H" if stk in both else ("15M Intraday" if tf == "15M" else "1H Structure")
                for t in types:
                    why = m.strike_selection_reason(sel, {"Timeframe": trade_tf, "Type": t, "Symbol": sym, "Strike": stk},
                                                    {"levels": [f"{tf}:{x}" for x in lvs], "opt_type": t, "symbol": sym, "strike": stk})
                    items.append({"symbol": sym, "strike": stk, "type": t,
                                  "levels": "/".join(m.LEVEL_LABELS[x] for x in m.ALL_LEVELS if x in lvs) or "-",
                                  "ok": not why, "why": why})
            out.append({"tf": tf, "items": items})
        return {"ok": True, "groups": out}

    def recent_orders(self):
        eng = self.m.live_order_manager.engine
        return {"ok": True, "orders": list(reversed(list(eng.recent)[-20:]))}

    # ── mapped strikes management with expiry selection and single/multiple delete ──
    def mapped_strikes_get(self):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            cfg = mapped_strikes_mgr.get_config()
            snap_ctrl = self.snap.get("controls", {})
            cur_exp = snap_ctrl.get("expiry") or ""
            next_exp = snap_ctrl.get("next_expiry") or ""
            all_exp = list(snap_ctrl.get("expiries") or [])
            exp_set = set(cfg.get("expiries", []))
            if cur_exp: exp_set.add(cur_exp)
            if next_exp: exp_set.add(next_exp)
            for e in all_exp:
                if e: exp_set.add(e)
            cfg["all_expiries"] = sorted(list(exp_set))
            cfg["current_expiry"] = cur_exp
            cfg["next_expiry"] = next_exp
            # Attach live prices if available from feed
            feed = self.m.feed
            for s in cfg.get("strikes", []):
                try:
                    ot = s.get("opt_type", "CE")
                    stk = int(float(s.get("strike", 0)))
                    exp = s.get("expiry") or cur_exp
                    s["ltp"] = feed.get_strike_ltp(ot, stk, exp) if feed else 0.0
                except Exception:
                    s["ltp"] = 0.0
            return {"ok": True, "config": cfg}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_add(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            sym = body.get("symbol") or self.snap.get("controls", {}).get("symbol") or "NIFTY"
            strike = float(body.get("strike", 0))
            ot = body.get("opt_type", "BOTH")
            lots = int(body.get("lots", 1))
            exp = body.get("expiry") or ""
            note = body.get("note") or ""
            source = body.get("source") or "Manual"
            item = mapped_strikes_mgr.add_strike(sym, strike, ot, lots, source=source, notes=note, expiry=exp)
            return {"ok": True, "item": item, "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_delete(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            item_id = body.get("id") or body.get("strike_id") or ""
            ok = mapped_strikes_mgr.delete_strike(item_id)
            return {"ok": ok, "message": "Strike deleted" if ok else "Strike not found", "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_delete_multiple(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            ids = body.get("ids") or []
            if not isinstance(ids, list):
                ids = [ids]
            count = mapped_strikes_mgr.delete_strikes(ids)
            return {"ok": True, "count": count, "message": f"Deleted {count} strike(s)", "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_delete_expiry(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            exp = body.get("expiry") or ""
            sym = body.get("symbol") or None
            count = mapped_strikes_mgr.delete_strikes_by_expiry(exp, sym)
            return {"ok": True, "count": count, "message": f"Deleted {count} strike(s) for expiry {exp}", "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_clear(self):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            count = mapped_strikes_mgr.clear_strikes()
            return {"ok": True, "count": count, "message": f"Cleared {count} strike(s)", "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_toggle(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            enabled = bool(body.get("enabled", False))
            val = mapped_strikes_mgr.set_restricted_mode(enabled)
            return {"ok": True, "restricted_mode": val, "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_save(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            if "strikes" in body:
                mapped_strikes_mgr.data["strikes"] = body["strikes"]
            if "restrict_to_selected_only" in body:
                mapped_strikes_mgr.set_restricted_mode(bool(body["restrict_to_selected_only"]))
            if "default_lots" in body:
                mapped_strikes_mgr.set_default_lots(int(body["default_lots"]))
            mapped_strikes_mgr.save()
            return {"ok": True, "message": "Saved mapped strikes configuration", "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def mapped_strikes_import_focus(self, body):
        try:
            from mapped_strikes_manager import mapped_strikes_mgr
            feed = self.m.feed
            c = self.snap.get("controls", {})
            sym = body.get("symbol") or c.get("symbol") or "NIFTY"
            exp = body.get("expiry") or c.get("expiry") or ""
            lots = int(body.get("lots") or mapped_strikes_mgr.data.get("default_lots", 1))
            f15 = feed.focus_15m or (feed.daily_focus or {}).get("15M", {}) or {}
            f1h = feed.focus_1h or (feed.daily_focus or {}).get("1H", {}) or {}
            count = mapped_strikes_mgr.import_from_focus(f15, f1h, symbol=sym, default_lots=lots, expiry=exp)
            return {"ok": True, "imported_count": count, "message": f"Imported {count} focus strike(s) for {exp or 'all'}", "config": mapped_strikes_mgr.get_config()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── main live dashboard strikes: delete (kept until restored) ──
    # Deletes are saved in deleted_strikes.json by deleted_strikes.py, so they survive the
    # snapshot refresh, a page reload and an app restart. A deleted strike is hidden from the
    # dashboard and the next-day plan, and the strategy takes no new entry on it. Open trades
    # are not touched. ↺ Restore (one or all) brings strikes back.
    @staticmethod
    def _excluded_list():
        return [f"{d['symbol']}|{d['strike']}|{d['type']}|{d['expiry']}" for d in deleted_strikes.active()]

    def _sym(self, body=None):
        return ((body or {}).get("symbol") or (self.snap.get("controls") or {}).get("symbol") or "NIFTY")

    def _done(self, msg, **extra):
        try:
            self.on_tk(self.app._refresh_display)
        except Exception:
            pass
        print(f"[DELETED STRIKES] {msg}")
        return {"ok": True, "message": msg, "excluded": self._excluded_list(), **extra}

    def strikes_delete(self, body):
        sym, stk, ot, exp = self._sym(body), body.get("strike", 0), body.get("opt_type") or body.get("type", ""), body.get("expiry", "")
        r = deleted_strikes.delete([{"symbol": sym, "strike": stk, "type": ot, "expiry": exp}])
        if not r["added"] and not deleted_strikes.is_deleted(sym, ot, stk, exp):
            return {"ok": False, "error": f"Could not delete {stk} {ot}: strike or type missing"}
        return self._done(f"Deleted strike {deleted_strikes._norm_strike(stk)} {ot} ({exp or 'ALL'})",
                          key=deleted_strikes.key(sym, exp, stk, ot))

    def strikes_delete_multiple(self, body):
        items = [{"symbol": s.get("symbol") or self._sym(), "strike": s.get("strike", 0),
                  "type": s.get("opt_type") or s.get("type", ""), "expiry": s.get("expiry", "")}
                 for s in body.get("strikes", [])]
        r = deleted_strikes.delete(items)
        return self._done(f"Deleted {r['added']} strike(s)", count=r["added"])

    def strikes_delete_expiry(self, body):
        exp = str(body.get("expiry", "")).strip().upper()
        if not exp or exp == "ALL":
            return {"ok": False, "error": "Please select a specific expiry date to delete"}
        sym = self._sym(body)
        items = [{"symbol": sym, "strike": (r.get("v") or {}).get("Strike"), "type": (r.get("v") or {}).get("Type"),
                  "expiry": exp}
                 for r in self.live_state().get("rows", [])
                 if str((r.get("v") or {}).get("Expiry", "")).strip().upper() == exp]
        r = deleted_strikes.delete(items)
        return self._done(f"Deleted {r['added']} strike(s) of expiry {exp}", count=r["added"])

    def strikes_restore(self, body):
        if body.get("all"):
            r = deleted_strikes.restore(None)
            return self._done(f"Restored all {r['restored']} deleted strike(s)", restored=r["restored"])
        key = body.get("key", "")
        if "|" in key:                         # the page's own "SYM|STRIKE|TYPE|EXPIRY" key
            sym, stk, ot, exp = (key.split("|") + ["", "", "", ""])[:4]
            key = deleted_strikes.key(sym, exp, stk, ot)
        elif not key:
            key = deleted_strikes.key(self._sym(body), body.get("expiry", ""), body.get("strike", 0),
                                      body.get("opt_type") or body.get("type", ""))
        r = deleted_strikes.restore([key])
        return self._done(f"Restored {r['restored']} strike(s)", restored=r["restored"])


def _make_handler(dash):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GlassDashboard/1.0"

        def log_message(self, *a):      # keep the launcher's terminal quiet
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local_only(self):
            # Browsers send Origin on cross-site POSTs; only accept our own page.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                self._send(403, {"ok": False, "error": "Forbidden"})
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/state":
                    return self._send(200, dash.live_state())
                if u.path == "/api/next-day-plan":
                    return self._send(200, dash.next_day_plan(q.get("mode", ""), q.get("s3", "1") == "1",
                                                              q.get("refresh") == "1"))
                if u.path == "/api/settings":
                    return self._send(200, dash.settings())
                if u.path == "/api/mapped-strikes":
                    return self._send(200, dash.mapped_strikes_get())
                if u.path == "/api/orders/recent":
                    return self._send(200, dash.recent_orders())
                if u.path == "/api/health":
                    return self._send(200, {"ok": True})
                return self._static(u.path)
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"ok": False, "error": str(e)})

        def do_POST(self):
            if not self._local_only():
                return
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                return self._send(400, {"ok": False, "error": "Bad JSON"})
            routes = {
                "/api/controls": lambda: dash.set_controls(body),
                "/api/start": dash.start,
                "/api/stop": dash.stop,
                "/api/filters": lambda: dash.set_filters(body),
                "/api/auto-trade": lambda: dash.auto_trade(body),
                "/api/kill": lambda: dash.kill(body),
                "/api/level-source": lambda: dash.level_source(body),
                "/api/map-levels": dash.map_levels,
                "/api/ft-filter": lambda: dash.ft_filter(body),
                "/api/gap-check": dash.gap_check,
                "/api/settings/keys": lambda: dash.save_keys(body),
                "/api/settings/test": lambda: dash.broker_test(body),
                "/api/settings/use": lambda: dash.broker_use(body),
                "/api/settings/save": lambda: dash.save_settings(body),
                "/api/settings/strikes-preview": lambda: dash.strikes_preview(body),
                "/api/mapped-strikes/add": lambda: dash.mapped_strikes_add(body),
                "/api/mapped-strikes/delete": lambda: dash.mapped_strikes_delete(body),
                "/api/mapped-strikes/delete-multiple": lambda: dash.mapped_strikes_delete_multiple(body),
                "/api/mapped-strikes/delete-expiry": lambda: dash.mapped_strikes_delete_expiry(body),
                "/api/mapped-strikes/clear": dash.mapped_strikes_clear,
                "/api/mapped-strikes/toggle": lambda: dash.mapped_strikes_toggle(body),
                "/api/mapped-strikes/save": lambda: dash.mapped_strikes_save(body),
                "/api/mapped-strikes/import-focus": lambda: dash.mapped_strikes_import_focus(body),
                "/api/strikes/delete": lambda: dash.strikes_delete(body),
                "/api/strikes/delete-multiple": lambda: dash.strikes_delete_multiple(body),
                "/api/strikes/delete-expiry": lambda: dash.strikes_delete_expiry(body),
                "/api/strikes/restore": lambda: dash.strikes_restore(body),
            }
            fn = routes.get(u.path)
            if not fn:
                return self._send(404, {"ok": False, "error": "Not found"})
            try:
                return self._send(200, fn())
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"ok": False, "error": str(e)})

        def _static(self, path):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
                return self._send(404, {"ok": False, "error": "Not found"})
            with open(full, "rb") as f:
                data = f.read()
            return self._send(200, data, STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
    return Handler


def start(app, module, port=None):
    """Serve the glass dashboard for a ControlPanel. Returns the server, or None when off or the port is busy."""
    if port is None:
        try:
            port = int(os.getenv("GLASS_DASHBOARD_PORT", DEFAULT_PORT))
        except ValueError:
            port = DEFAULT_PORT
    if not port:
        print("[GLASS] Glass dashboard turned off (GLASS_DASHBOARD_PORT=0)")
        return None
    dash = GlassDashboard(app, module)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(dash))
    except OSError as e:
        print(f"[GLASS] ⚠️ Port {port} is busy ({e}). Is the glass dashboard already running? "
              f"Set GLASS_DASHBOARD_PORT to use another port.")
        return None
    srv.daemon_threads = True
    srv.dash = dash
    dash.start_snapshots()
    threading.Thread(target=srv.serve_forever, name="glass-dashboard", daemon=True).start()
    print(f"[GLASS] ✨ Glass dashboard: http://127.0.0.1:{port}/")
    return srv
