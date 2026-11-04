# Pivot Reversal: entry and exit logic (as coded, 4 Nov 2026)

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
The buy level is S2, or S1 when S2 ≤ 0.05. Rule from AJ, 4 Nov: **close back above S2, retest, then entry above the high of that first close-above candle. A one-way move is ignored.**
1. **Sweep.** Price trades **below** the level. Coming near it without trading below it is not a sweep.
   - State becomes `SWEEP_S2`, and the lowest price is recorded as **LL**.
   - The levels are frozen at that moment.
2. **Reclaim.** LTP gets back above the level, and the state becomes `RECLAIM_S2`.
3. **Close-above candle.** The **first** 5-minute candle that fully **closes** at or above the level, red or green.
   - It must be the sweep candle or a later one. A candle from before the sweep never counts, and a candle that is still forming never counts.
   - Its high is the **confirmation high**.
4. **Retest.** After that candle has closed, price has to pull back below its close and into the level zone (within 1.5% above the level), without trading below the level. The candle's own wick is part of the sweep and never counts as the retest.
5. **Entry: break above the confirmation high.** After the retest, LTP trades above the confirmation candle's high, and the state becomes `CONFIRMED_BUY`.

**One-way moves are ignored:**
- **Up:** if price breaks above the confirmation high *before* the retest, the setup is dropped (signal "continuation … ignored"). A later pull-back does not revive it; only a fresh trade below the level starts a new setup.
- **Down:** if price keeps falling and no candle closes back above the level, there is no entry. Any trade below the level during the retest or before the entry restarts the setup from that sweep: a new close-above candle is needed. The LL stays the lowest price of the whole sweep, so a shallower dip never moves the SL closer.
- After an app restart, a dip below the level that the app did not watch live does not start a setup.

The old behaviour can be switched back in `Options-angel_live-ok.py`: `REQUIRE_LEVEL_BREAK = False` (a near-touch within 1% counts as a sweep) and `CONF_ANY_COLOUR = False` (the candle must close green).

When the entry is confirmed:
- **Entry price** is the confirmation candle's high. The forward test records the live LTP at that moment.
- **No chasing.** The live price must be at most 1% (min 0.50) above the confirmation high. If it has run further, the app waits for price to come back.
- **No room, no trade.** If the confirmation high is already at or above T1, the setup is skipped.
- **SL** is LL. Fallbacks, in order: the day low, then S3 (or S2 for an S1 entry), then entry minus 10%.
- **Targets:**
  - From S2: S1, P, R1, R2, R3.
  - From S1: P, R1, R2, R3, R3 + 5%.

## 4. SELL entry, resistance side (mirror image)
1. **Sweep.** Price trades **above** R2 (a near-touch is not a sweep). The highest price is recorded as **HH**.
2. **Rejection.** LTP drops back below R2.
3. **Close-below candle.** The first fully closed 5-minute candle below R2, red or green, from the sweep candle or later. Its low is the **confirmation low**.
4. **Retest.** After that candle has closed, price pushes back above its close and into the R2 zone (within 1.5% below R2), without trading above R2.
5. **Entry: break below the confirmation low.** After the retest, LTP trades below the confirmation candle's low, and the **SELL is confirmed** at that low. No chasing: at most 1% below it. Skipped if the low is already at or below T1 (R1).
- **One-way moves are ignored:** a break below the confirmation low before the retest drops the setup; a trade above R2 restarts it from that sweep.
- Live SELL orders are on by default, same as BUY (setting "Take SELL setups"). Every setup is recorded in the sheets; one that gets no order shows the reason (Orders sheet row NO ORDER, and the trade's Order_ID).

When the entry is confirmed:
- **SL** is HH: the highest price of the whole sweep, which keeps rising with price through the rejection, candle close and retest until the entry. Never R3 or R2; with no HH above the entry there is no trade.
- **Targets:** R1, P, S1, S2, S3.

## 5. Exits (forward test and paper orders)
- **SL hit:** the trade closes at LTP.
- **No trailing SL (AJ, 13 Oct):** the SL stays the LL (BUY) / HH (SELL) the trade was entered with through every target. A trade ends only at that SL, at T5 or at the 3 PM exit.
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
- Targets are a 5-step ladder; the spec says the next pivot only.
