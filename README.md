# PivotReversal

Buy at LL, Sell at HH. A live NIFTY / BANKNIFTY / SENSEX option-chain dashboard that trades the
Fibonacci pivot reversal setup on 15-minute and 1-hour pivot strikes, with optional live order
placement through Angel One, Dhan or Jainam XTS.

## Files

| Path | What it is |
|---|---|
| `Options-angel_live-ok.py` | Main app: Angel One feed, pivot levels, setups, forward test, Tk dashboard, order bar |
| `brokers/` | Order engine and broker adapters (`angel.py`, `dhan.py`, `xts.py`) |
| `eod_planner.py` | End-of-day planner that builds the next day's pivot plan |
| `web_server.py`, `web/` | Local web dashboard (bound to localhost; it never places orders) |
| `glass_dashboard.py`, `glass_web/` | Glass dashboard: browser view of the running launcher on http://127.0.0.1:8780 |
| `run_option_chain.command`, `run.sh`, `run_web.sh` | Launchers (macOS / shell) |
| `LiveOptionChain.app` | macOS launcher app |
| `dhan_login.py` | Stand-alone Dhan PIN + TOTP login helper |
| `entry_logic.md`, `changes_20261007.md` | Strategy notes and change log |
| `Fibonacci_Pivot_Reversal_Strategy.pdf` | Strategy spec |
| `PIVOT PROJECT FINAL BACKUP/` | Older snapshot uploaded on 2026-10-06 |

## Setup

1. Python 3.11 or 3.12 with `smartapi-python`, `pyotp`, `requests`, `pandas`, `gspread`, `google-auth-oauthlib`.
2. Copy `.env.example` to `.env` and fill in your keys.
3. For the Google Sheet log, put your own `credentials.json` (Google OAuth client) next to the app;
   `token.pickle` is created on first login.
4. Start with `run_option_chain.command` (macOS) or `python3 Options-angel_live-ok.py`.

## Live orders

Orders are sent only when **AUTO-TRADE** is switched ON in the dashboard's order bar. Use ⚙ Settings to
pick the broker, test the connection and set lots, order type and daily limits (default: max 6 trades,
max ₹5,000 loss per day, SELL setups off, SL placed at the broker). The Strikes tab picks which
strategy strikes get real orders: pivot levels (R1, R2, S1, S2, P, next to P), CE/PE, 15M/1H match,
symbols and an optional strike list, with a preview of today's strikes. ■ STOP exits every open position.

Before the first live day, double-click `test_connections.command` (logins only), then
`live_order_check.command` during market hours. It reads funds, the contract, order book and positions,
and can send one 1-lot NIFTY BUY LIMIT 20% below LTP that it modifies and cancels at once.

Secrets (`.env`, `credentials.json`, `token.pickle`, `config.json`, Dhan files) are listed in
`.gitignore` and must never be committed.

## Option strike levels (match TradingView)

Each focus strike's Fibonacci pivots (P, R1–R3, S1–S3) come only from that contract's official
daily candle from Angel One's historical API, the same H/L/C TradingView uses for
**Pivot Points Standard → Type: Fibonacci, Pivots Timeframe: Daily**.

- During a session (and all evening after it) the app shows that session's levels, built from the
  previous day's candle, which is what TradingView draws on the chart. At the weekend it shows
  Friday's.
- After 15:30 the next session's levels are built separately into `strike_pivot_cache_next.json`
  (Next-Day Plan window, `eod_planner.py`); they never replace today's levels.
- Every contract keeps its own levels per expiry. A strike with no candle data gets no levels
  (nothing is estimated) and is retried every minute; an expired Angel session is renewed
  automatically.
- On TradingView, compare on a chart of the same contract and expiry, with the timeframe set to
  Daily (on a 1H chart "Auto" switches to weekly pivots). S levels below 0.05 show as 0.05.
- Test: `python3 tests/test_strike_pivots.py`

## Glass dashboard (browser view of the launcher)

When `Options-angel_live-ok.py` starts, it also serves a glass-style copy of its window on
**http://127.0.0.1:8780/**. It runs inside the launcher, so it shows the same feed, reversal rows,
filters and order engine, and its buttons (START / STOP, filters, AUTO-TRADE, ■ STOP, ⚙ Settings,
Next-Day Plan, Map Strike Levels, Gap Check, FT filter) do exactly what the Tk buttons do. Changing a
filter or the symbol in the browser changes it in the Tk window too. It never starts a second feed or
order engine, and the existing `web_server.py` dashboard is not affected.

- Only reachable from this Mac (bound to 127.0.0.1). Standard library only, no new packages.
- `GLASS_DASHBOARD_PORT=8790` picks another port; `GLASS_DASHBOARD_PORT=0` turns it off.
- Try it without the market: `python3 tests/run_glass_demo.py` (made-up NIFTY data, no login, no orders).
