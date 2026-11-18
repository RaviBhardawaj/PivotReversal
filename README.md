# PivotReversal

Buy at LL, Sell at HH. A live NIFTY / BANKNIFTY / SENSEX option-chain scanner that trades the
Fibonacci pivot reversal setup on 15-minute and 1-hour pivot strikes, with optional live order
placement through Angel One, Dhan or Jainam XTS. You watch and control it in the **Glass dashboard**,
a web page on your own computer at **http://127.0.0.1:8780**.

Runs on macOS, Linux and Windows. Nothing in it depends on a particular Mac or folder.

## Run it (one command)

You need **Python 3.10 or newer** (3.12 recommended) and **git**.

**macOS / Linux**

```bash
git clone https://github.com/RaviBhardawaj/PivotReversal.git && cd PivotReversal && bash run.sh
```

**Windows** (Command Prompt)

```bat
git clone https://github.com/RaviBhardawaj/PivotReversal.git && cd PivotReversal && run.bat
```

The first run creates a private Python environment (`.venv`), installs `requirements.txt`, and
creates `.env` from `.env.example`. Put your Angel One keys in `.env`, run the same command again
(`bash run.sh` or `run.bat` inside the folder), and the dashboard opens in your browser.

Later: `cd PivotReversal && git pull && bash run.sh` (Windows: `run.bat`) gets the newest code and starts it.

### Moving from an old copy of the folder

Your keys and logins are never in git. Copy them from the old folder into the new one once:

```bash
OLD="$HOME/Downloads/Pivot Reversal Strategy"
for f in .env credentials.json token.pickle order_settings.json deleted_strikes.json dhan_token.json; do
  [ -f "$OLD/$f" ] && cp "$OLD/$f" PivotReversal/
done
```

## Configuration

| File | What goes in it | In git? |
|---|---|---|
| `.env` | Angel One keys (feed, levels, orders); optional Dhan and Jainam XTS keys. Template: `.env.example` | No |
| `credentials.json` | Optional: your Google OAuth client for the Google Sheet log. Without it the app runs, just without the sheet | No |
| `token.pickle` | Google login, created on the first sheet login | No |
| `order_settings.json`, `deleted_strikes.json`, `*.csv`, `logs/`, caches | Created by the app while it runs | No |

Environment settings: `GLASS_DASHBOARD_PORT=8790` uses another port, `GLASS_NO_BROWSER=1` stops it
opening the browser (useful on a server; then open the URL yourself).

The dashboard listens on 127.0.0.1 only, so it is reachable from the same computer only.

## Watchlist: fixed strike ladder

The strategy watches different strikes as the price moves, so the list used to reshuffle. The
watchlist now defaults to **Strike ladder (fixed order)**:

- Strikes always stay in strike order, low to high (CE before PE, BUY before SELL). Rows do not jump.
- A cyan **spot line** (e.g. `NIFTY 25,618.00`) sits between the strikes below and above the live
  price and moves as the price moves. Above it CE strikes are in the money, below it PE strikes are.
- Every strike says where it is right now: **ATM**, **1 ITM**, **2 OTM** and so on.
- A strike the strategy just added is marked **NEW** for a minute. A strike it dropped stays for two
  minutes, faded and marked **gone**, instead of vanishing.
- A yellow edge marks a strike whose LTP is within 5% of its entry. Running trades are always in
  the **Live trades** panel above the watchlist.

The previous orders (entry on / near entry first, setups first, closest to entry) are still in the
**Sort** menu.

## Files

| Path | What it is |
|---|---|
| `run.sh`, `run.bat` | Start the Glass dashboard (sets up `.venv` and `.env` on the first run) |
| `glass_server.py`, `glass_dashboard.py`, `headless_tk.py`, `glass_web/` | Glass dashboard: runs the strategy engine with no window and serves the page on port 8780 |
| `Options-angel_live-ok.py` | Strategy engine: Angel One feed, pivot levels, setups, forward test, order bar. Also a Tk desktop launcher when run directly |
| `brokers/` | Order engine and broker adapters (`angel.py`, `dhan.py`, `xts.py`) |
| `eod_planner.py` | End-of-day planner that builds the next day's pivot plan |
| `deleted_strikes.py`, `mapped_strikes_manager.py`, `backfill_missed_entries.py` | Strike deletes, mapped strike levels, restart backfill |
| `test_connections.py`, `live_order_check.py`, `check_levels.py`, `check_sl.py`, `dhan_login.py` | Stand-alone checks and helpers |
| `*.command` | macOS double-click shortcuts for the same scripts |
| `tests/` | Tests (`python3 -m pytest tests` or run a file directly) |
| `entry_logic.md`, `changes_20261007.md`, `Fibonacci_Pivot_Reversal_Strategy.pdf` | Strategy notes and spec |

## Glass dashboard

`bash run.sh` (or `python3 glass_server.py` inside `.venv`) opens **http://127.0.0.1:8780/**.

- It loads the strategy engine (`Options-angel_live-ok.py`) and logs in to Angel One itself. The
  launcher's control logic runs without a window (`headless_tk.py`), so the scanner, the reversal
  rows and their colours, the filters, the next-day plan, Map Strike Levels, Gap Check, AUTO-TRADE,
  ■ STOP and ⚙ Settings (Broker, Orders, Strikes, Today's orders) work exactly as in the launcher.
- Use it **instead of** the Tk launcher (`python3 Options-angel_live-ok.py`), not at the same time:
  both would stream the same feed and write the same sheet and orders. While the launcher is
  running, the page shows a warning and START and AUTO-TRADE are refused.
- Try it without the market: `.venv/bin/python tests/run_glass_demo.py` (made-up NIFTY data, no
  login, no orders).

## Live orders

Orders are sent only when **AUTO-TRADE** is switched ON. Use ⚙ Settings to pick the broker, test the
connection and set lots, order type and daily limits (default: max 6 trades, max ₹5,000 loss per
day, SELL setups off, SL placed at the broker). The Strikes tab picks which strategy strikes get
real orders: pivot levels (R1, R2, S1, S2, P, next to P), CE/PE, 15M/1H match, symbols and an
optional strike list, with a preview of today's strikes. ■ STOP exits every open position.

Before the first live day on a new computer, run `python test_connections.py` (logins only), then
`python live_order_check.py` during market hours. It reads funds, the contract, order book and
positions, and can send one 1-lot NIFTY BUY LIMIT 20% below LTP that it modifies and cancels at once.

## Option strike levels (match TradingView)

Each focus strike's Fibonacci pivots (P, R1–R3, S1–S3) come only from that contract's official
daily candle from Angel One's historical API, the same H/L/C TradingView uses for
**Pivot Points Standard → Type: Fibonacci, Pivots Timeframe: Daily**.

- During a session (and all evening after it) the app shows that session's levels, built from the
  previous day's candle, which is what TradingView draws on the chart. At the weekend it shows
  Friday's.
- After 15:30 the next session's levels are built separately into `strike_pivot_cache_next.json`
  (Next-Day Plan, `eod_planner.py`); they never replace today's levels.
- Every contract keeps its own levels per expiry. A strike with no candle data gets no levels
  (nothing is estimated) and is retried every minute; an expired Angel session is renewed
  automatically.
- On TradingView, compare on a chart of the same contract and expiry, with the timeframe set to
  Daily (on a 1H chart "Auto" switches to weekly pivots). S levels below 0.05 show as 0.05.

## Secrets

`.env`, `credentials.json`, `token.pickle`, `config.json`, Dhan token files, trade CSVs, logs and
caches are in `.gitignore` and must never be committed.
