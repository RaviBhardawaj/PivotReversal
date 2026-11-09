"""Strikes deleted from the dashboard, kept until the user regenerates.

A deleted strike (symbol + expiry + strike + CE/PE) is hidden from the dashboard and its
next-day plan, and the strategy takes no new entry on it, so no paper trade and no broker order
start from it. Trades that are already open are never touched.

Deletes are saved in deleted_strikes.json, so they survive a restart and a page refresh. They
apply to one trading session: during market hours that is today, after the 15:30 close it is
the next trading day. They stay until REGENERATE clears them (or the session they were made for
is over).
"""

import json
import os
import re
import threading
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DELETED_FILE = os.path.join(BASE_DIR, "deleted_strikes.json")

_lock = threading.RLock()
_cache = {"mtime": None, "path": None, "data": None}


def session_date(now: datetime = None) -> str:
    """The trading session a delete is for: today until 15:30 on a weekday, else the next weekday."""
    now = now or datetime.now()
    d = now.date()
    if now.weekday() < 5 and (now.hour, now.minute) < (15, 30):
        return d.isoformat()
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def _norm_strike(v) -> int:
    """25050, "25050.0", "25,050" or a dashboard cell with a marker such as "🔷25050"."""
    m = re.search(r"\d+(?:\.\d+)?", str(v if v is not None else "").replace(",", ""))
    return int(round(float(m.group()))) if m else 0


def key(symbol, expiry, strike, opt_type) -> str:
    return "|".join([str(symbol or "").strip().upper(), str(expiry or "").strip().upper(),
                     str(_norm_strike(strike)), str(opt_type or "").strip().upper()])


def _load() -> dict:
    with _lock:
        try:
            mt = os.path.getmtime(DELETED_FILE)
        except OSError:
            return {"items": []}
        if _cache["path"] == DELETED_FILE and _cache["mtime"] == mt and _cache["data"] is not None:
            return _cache["data"]
        try:
            with open(DELETED_FILE) as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                data = {"items": []}
        except Exception:
            data = {"items": []}
        _cache.update(path=DELETED_FILE, mtime=mt, data=data)
        return data


def _save(data: dict):
    with _lock:
        tmp = DELETED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, DELETED_FILE)
        _cache.update(path=None, mtime=None, data=None)


def active(now: datetime = None) -> list:
    """The deletes that apply to the current session."""
    sess = session_date(now)
    return [it for it in _load().get("items", []) if it.get("session") == sess]


def active_keys(now: datetime = None) -> set:
    return {it["key"] for it in active(now) if it.get("key")}


def is_deleted(symbol, opt_type, strike, expiry, now: datetime = None) -> bool:
    """True when this contract was deleted for the current session. With no expiry given, a
    delete of that strike on any expiry counts."""
    keys = active_keys(now)
    if not keys:
        return False
    if expiry:
        return key(symbol, expiry, strike, opt_type) in keys
    sym, stk, ot = str(symbol or "").strip().upper(), str(_norm_strike(strike)), str(opt_type or "").strip().upper()
    return any(k.split("|")[0] == sym and k.split("|")[2:] == [stk, ot] for k in keys)


def delete(items: list, now: datetime = None) -> dict:
    """Add contracts ({symbol, expiry, strike, type}) to the deleted list for the session."""
    now = now or datetime.now()
    sess = session_date(now)
    with _lock:
        data = _load()
        # drop deletes of sessions that are over, keep the file small
        kept = [it for it in data.get("items", []) if it.get("session", "") >= now.date().isoformat()]
        have = {(it.get("session"), it.get("key")) for it in kept}
        added = 0
        for it in items or []:
            stk = _norm_strike(it.get("strike"))
            ot = str(it.get("type") or it.get("opt_type") or "").strip().upper()
            sym = str(it.get("symbol") or "").strip().upper()
            if not (sym and stk and ot in ("CE", "PE")):
                continue
            k = key(sym, it.get("expiry"), stk, ot)
            if (sess, k) in have:
                continue
            kept.append({"key": k, "symbol": sym, "expiry": str(it.get("expiry") or "").strip().upper(),
                         "strike": stk, "type": ot, "session": sess,
                         "deleted_at": now.strftime("%Y-%m-%d %H:%M:%S")})
            have.add((sess, k))
            added += 1
        _save({"items": kept})
    return {"ok": True, "added": added, "session": sess, "deleted": active(now)}


def restore(keys: list = None, now: datetime = None) -> dict:
    """Bring deleted strikes back: the given keys, or every delete of the session (REGENERATE)."""
    now = now or datetime.now()
    sess = session_date(now)
    with _lock:
        data = _load()
        drop = set(keys or [])
        before = data.get("items", [])
        kept = [it for it in before
                if not (it.get("session") == sess and (not keys or it.get("key") in drop))]
        n = len(before) - len(kept)
        if n:
            _save({"items": kept})
    return {"ok": True, "restored": n, "session": sess, "deleted": active(now)}
