// Watchlist ordering helpers. Plain functions so tests can run them in Node.
//
// "ladder" (default): a fixed strike ladder. Rows never jump around as the price moves:
// strikes stay in strike order (CE before PE, BUY before SELL), a spot line sits between the
// strikes below and above the live price, and each row says where it is vs the price
// (ATM / 1 ITM / 2 OTM ...). When the strategy adds a strike it is marked NEW for a minute;
// when a strike drops out of the focus list it stays a little while, faded, instead of
// vanishing without a trace.
//
// "entry": running trades, then strikes whose LTP is within NEAR_PCT of their entry (closest
// first), then forming setups, then the rest in strike order, then trades closed today.
(function (root) {
  "use strict";
  const NEAR_PCT = 5;
  const NEW_MS = 60 * 1000;       // a strike just added to the list shows "NEW" this long
  const GONE_MS = 2 * 60 * 1000;  // a strike that left the list stays (faded) this long

  const isNear = (i) => !i.trade && i.gap !== null && i.gap !== undefined && Math.abs(i.gap) <= NEAR_PCT;
  function entryRank(i) {
    if (i.trade && !i.closed) return 0;
    if (isNear(i)) return 1;
    if (i.setup) return 2;
    if (i.trade) return 4;
    return 3;
  }
  function entryFirst(list) {
    return list.map((i, n) => ({i, n, r: entryRank(i)}))
      .sort((a, b) => a.r - b.r || (a.r === 1 ? Math.abs(a.i.gap) - Math.abs(b.i.gap) : 0) || a.n - b.n)
      .map((x) => x.i);
  }

  // ── strike ladder ──
  const strikeOf = (i) => { const s = parseFloat(i.strike); return isFinite(s) ? s : Infinity; };
  const typeRank = (i) => (i.type === "PE" ? 1 : 0);
  function ladder(list) {
    return list.map((i, n) => ({i, n}))
      .sort((a, b) => strikeOf(a.i) - strikeOf(b.i) || typeRank(a.i) - typeRank(b.i) ||
        (b.i.buy ? 1 : 0) - (a.i.buy ? 1 : 0) || a.n - b.n)
      .map((x) => x.i);
  }
  // Strike step of the chain (smallest gap between distinct strikes), 0 if unknown.
  function strikeStep(strikes) {
    const s = [...new Set(strikes.filter((x) => isFinite(x) && x > 0))].sort((a, b) => a - b);
    let step = 0;
    for (let j = 1; j < s.length; j++) { const d = s[j] - s[j - 1]; if (d > 0 && (!step || d < step)) step = d; }
    return step;
  }
  // Where a strike is vs the price: {label: "ATM" | "2 ITM" | "1 OTM", cls: "atm"|"itm"|"otm"}.
  // ATM is the strike nearest the spot; others count strike steps away from it.
  function moneyness(type, strike, spot, step) {
    if (!(spot > 0) || !isFinite(strike) || !(step > 0)) return null;
    const atm = Math.round(spot / step) * step;
    const n = Math.round((strike - atm) / step);
    if (n === 0) return {label: "ATM", cls: "atm", n: 0};
    const itm = type === "PE" ? n > 0 : n < 0;
    return {label: `${Math.abs(n)} ${itm ? "ITM" : "OTM"}`, cls: itm ? "itm" : "otm", n};
  }
  // Index in a ladder-ordered list where the spot line goes (first row above the spot).
  function spotSlot(ordered, spot) {
    if (!(spot > 0)) return -1;
    const j = ordered.findIndex((i) => strikeOf(i) > spot);
    return j === -1 ? ordered.length : j;
  }

  // Remembers which rows were on screen: rows first seen in the last NEW_MS are "fresh", rows
  // that left within GONE_MS come back as ghosts. A context change (symbol, expiry, filters)
  // starts over, so a filter click never shows a wall of NEW / faded rows.
  function makeTracker(opts) {
    const newMs = (opts && opts.newMs) || NEW_MS, goneMs = (opts && opts.goneMs) || GONE_MS;
    let ctx = null;
    const seen = new Map();   // key -> {first, last, item}
    return {
      step(items, now, context) {
        const reset = context !== ctx;
        if (reset) { ctx = context; seen.clear(); }
        const fresh = new Set(), here = new Set();
        for (const it of items) {
          here.add(it.key);
          const e = seen.get(it.key);
          if (!e) seen.set(it.key, {first: reset ? -Infinity : now, last: now, item: it});
          else { e.last = now; e.item = it; }
          const f = seen.get(it.key).first;
          if (now - f < newMs) fresh.add(it.key);
        }
        const ghosts = [];
        for (const [k, e] of seen) {
          if (here.has(k)) continue;
          if (now - e.last > goneMs) { seen.delete(k); continue; }
          e.first = now;            // if it comes back it is new again
          ghosts.push(e.item);
        }
        return {fresh, ghosts};
      },
      // drop rows for good (e.g. strikes the user deleted): a key list or a test on the item
      forget(what) {
        for (const [k, e] of seen) if (typeof what === "function" ? what(e.item) : what.includes(k)) seen.delete(k);
      },
    };
  }

  const api = {NEAR_PCT, NEW_MS, GONE_MS, isNear, entryRank, entryFirst, ladder, strikeStep, moneyness, spotSlot, makeTracker};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.GlassSort = api;
})(this);
