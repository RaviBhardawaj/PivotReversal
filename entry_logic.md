# Pivot Reversal: entry and exit logic (as coded, 8 Oct 2026)

Source: `Options-angel_live-ok.py`. `StrategyStrikeState` runs the state machine, the main feed loop handles entry and alerts, and `ForwardTestTracker` handles exits.

## 1. Which strikes can trade
- Only strikes from the **15M** and **1H** pivots of the index:
  - CE: R1 and R2 strikes.
  - PE: S1 and S2 strikes.
  - P zone: the strike below and above P, traded on both CE and PE.
- A strike that isn't in a 15M or 1H set gets an empty `trade_tf`. It is never entered, recorded or alerted.
- Entries happen only in the live market (09:15–15:30).
- Setups, entries and exits only use **real trades from today** (exchange trade time 09:15 or later) with today's traded high/low. Before the first trade of the day the feed still shows yesterday's close and high/low; those are ignored.

## 2. Option levels for each strike
- Fib pivots come from the option's **previous-day** candle: P, S1–S3, R1–R3.
  - The candle must be of exactly the expected trading day; an older candle is never used.
  - Dhan's daily candle first (Angel as backup). If the daily candle isn't published yet, the day is rebuilt from Dhan's 1-minute candles and replaced by the official candle later.
  - From 15:35 (and at weekends/holidays) every strike shows the **next** session's levels, built from the day that just closed.
- They are fixed for the whole session. New ones are never derived from today's running high/low.
- **Level lock:** a strike keeps the levels it first saw. They change only if the strike's levels actually change.
  - If that happens while a trade is open, the trade keeps its entry, SL and targets until it exits.
  - The new levels apply to the next setup.

## 3. BUY entry (CE or PE), support side
The buy level is S2, or S1 when S2 ≤ 0.05.
1. **Sweep.** LTP trades below the level, or comes within 1% of it.
   - State becomes `SWEEP_S2`, and the lowest price is recorded as **LL**.
   - The levels are frozen at that moment.
2. **Reclaim.** LTP gets back above the level, and the state becomes `RECLAIM_S2`.
3. **Candle close.** A completed 5-minute candle closes green, at or above the level.
   - If its wick touched the level (within 1.5%), the retest counts as done.
4. **Retest.** Otherwise, price has to dip back within 1.5% of the level while holding above LL.
5. **Trading in favour.** Price then pushes above the level, at least 0.2% above the retest low.
   - **Entry is confirmed** and the state becomes `CONFIRMED_BUY`.

When the entry is confirmed:
- **At the entry level only.** The live price must be at the level: S2 (or S1) up to 1% above it (min 0.50). If price has already moved further away, the app does not chase; it waits for price to come back to the level.
- **Entry price** recorded in the forward test is the **live LTP** at that moment (so within 1% of the level).
- **SL** is LL. Fallbacks, in order: the day low, then S3 (or S2 for an S1 entry), then entry minus 10%.
- **Targets:**
  - From S2: S1, P, R1, R2, R3.
  - From S1: P, R1, R2, R3, R3 + 5%.

## 4. SELL entry, resistance side (mirror image)
1. **Sweep.** LTP goes above R2, or comes within 1% of it. The highest price is recorded as **HH**.
2. **Rejection.** LTP drops back below R2.
3. **Candle close.** A completed 5-minute candle closes red, below R2.
4. **Retest** of R2, holding below HH.
5. **Push down.** Price moves lower from there, and the **SELL is confirmed**, only while the price is at R2 or up to 1% below it (no chasing).

When the entry is confirmed:
- **SL** is HH.
- **Targets:** R1, P, S1, S2, S3.

## 5. Exits (forward test and paper orders)
- **SL hit:** the trade closes at LTP.
- **Target ladder:** at T1 the SL moves to entry, at T2 it moves to T1, and so on.
- **3:00 PM:** every open trade closes at LTP.
- **Re-entry:** after the first SL hit on a strike, one re-entry is allowed with a new LL/HH SL. A second SL hit blocks that strike for the day.
- Active trades are no longer force-closed mid-session. The old "stale reset" is off.

## 6. Alerts
- **Entry alerts** (macOS banner and sound) fire only on confirmed entries for 15M/1H strikes in live market, once per signal per day.
- **Exit alerts:**
  - Fire on SL hit, the final target, and the 3 PM close.
  - Only for trades whose timeframe is 15M or 1H.
- Every alert is also written to `alerts_history.csv`.

## Known gaps vs the PDF spec (not changed)
- A sweep is counted when price comes within 1% of S2/R2, not only on a real break through it.
- Targets are a 5-step ladder; the spec says the next pivot only.
