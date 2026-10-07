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
max ₹5,000 loss per day, SELL setups off, SL placed at the broker). ■ STOP exits every open position.

Secrets (`.env`, `credentials.json`, `token.pickle`, `config.json`, Dhan files) are listed in
`.gitignore` and must never be committed.
