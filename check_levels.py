"""Checks every strike level the app is using against the raw trades.

For each strike in strike_pivot_cache.json it fetches the same day's candle again:
  - Dhan daily candle (what the app uses)
  - the day rebuilt from Dhan's 1-minute candles (09:15-15:30 trades)
  - Angel daily candle (when the Angel key works)
and recomputes the Fibonacci pivots. Writes level_check_report.csv. Places no orders,
prints no keys. Run: python3 check_levels.py   (or double-click check_levels.command)"""
import os, sys, csv, json, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))
from brokers.dhan import DhanAdapter, _SEG

TOL = 0.05


def fib(h, l, c):
    p = (h + l + c) / 3.0
    r = h - l
    lv = {"P": p, "R1": p + 0.382 * r, "R2": p + 0.618 * r, "R3": p + r,
          "S1": p - 0.382 * r, "S2": p - 0.618 * r, "S3": p - r}
    return {k: round(max(v, 0.05) if k.startswith("S") else v, 2) for k, v in lv.items()}


def main():
    cache = json.load(open(os.path.join(HERE, "strike_pivot_cache.json")))
    rows = list((cache.get("strikes") or {}).values())
    print(f"Levels in use: {cache.get('basis_key')}  ({len(rows)} strikes)")
    creds = {k: (os.getenv(k, "") or "").strip() for k in DhanAdapter.env_keys}
    dh = DhanAdapter(creds, log=lambda *a: None, cache_dir=HERE)
    dh.login()
    angel = None
    try:
        from SmartApi import SmartConnect
        import pyotp
        a = SmartConnect(api_key=os.getenv("ANGEL_API_KEY") or os.getenv("API_KEY"))
        r = a.generateSession(os.getenv("ANGEL_CLIENT_ID") or os.getenv("CLIENT_ID"),
                              os.getenv("ANGEL_MPIN") or os.getenv("MPIN") or os.getenv("PASSWORD"),
                              pyotp.TOTP(os.getenv("ANGEL_TOTP_SECRET") or os.getenv("TOTP_SECRET") or "").now())
        if r and r.get("status"):
            angel = a
    except Exception:
        angel = None
    out, bad = [], []
    for i, s in enumerate(sorted(rows, key=lambda x: (x.get("expiry", ""), x.get("strike", 0), x.get("opt_type", "")))):
        day, tok = s.get("candle_date"), s.get("token")
        name = f"{s.get('strike')} {s.get('opt_type')} {s.get('expiry')}"
        rec = {"strike": s.get("strike"), "type": s.get("opt_type"), "expiry": s.get("expiry"),
               "token": tok, "candle_date": day, "app_H": s.get("prev_h"), "app_L": s.get("prev_l"),
               "app_C": s.get("prev_c")}
        for k in ("S3", "S2", "S1", "P", "R1", "R2", "R3"):
            rec["app_" + k] = s.get(k)
        try:
            d = [x for x in dh.daily_candles(tok, _SEG.get("NFO"), "OPTIDX") if x[0] == day]
            if d:
                rec.update(dhan_H=d[0][2], dhan_L=d[0][3], dhan_C=d[0][4])
        except Exception as e:
            rec["note"] = f"dhan daily: {e}"
        time.sleep(0.25)
        try:
            m = dh.intraday_day_ohlc(tok, day)
            if m:
                rec.update(min1_H=m[1], min1_L=m[2], min1_C=m[3], min1_last=m[4])
        except Exception as e:
            rec["note"] = (rec.get("note", "") + f" 1min: {e}").strip()
        time.sleep(0.25)
        if angel:
            try:
                g = angel.getCandleData({"exchange": "NFO", "symboltoken": tok, "interval": "ONE_DAY",
                                         "fromdate": f"{day} 09:00", "todate": f"{day} 15:30"})
                dd = [x for x in (g.get("data") or []) if str(x[0])[:10] == day]
                if dd:
                    rec.update(angel_H=dd[0][2], angel_L=dd[0][3], angel_C=dd[0][4])
            except Exception:
                pass
            time.sleep(0.4)
        diffs = []
        for src in ("dhan", "min1", "angel"):
            if rec.get(src + "_H") is None:
                continue
            for k in ("H", "L", "C"):
                if abs(float(rec[src + "_" + k]) - float(rec["app_" + k] or 0)) > TOL:
                    diffs.append(f"{src} {k} {rec[src + '_' + k]} vs app {rec['app_' + k]}")
        if rec.get("min1_H") is not None:
            for k, v in fib(rec["min1_H"], rec["min1_L"], rec["min1_C"]).items():
                rec["from1min_" + k] = v
        rec["result"] = "MATCH" if not diffs else "DIFFERENT"
        rec["detail"] = "; ".join(diffs)
        if diffs:
            bad.append(f"  {name}: " + "; ".join(diffs))
        out.append(rec)
        print(f"  [{i + 1}/{len(rows)}] {name}: {rec['result']}")
    keys = []
    for r in out:
        for k in r:
            if k not in keys:
                keys.append(k)
    path = os.path.join(HERE, "level_check_report.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(out)
    print()
    print(f"{len(out) - len(bad)} of {len(out)} strikes match. Report: level_check_report.csv")
    if bad:
        print("Different:")
        print("\n".join(bad))


if __name__ == "__main__":
    main()
