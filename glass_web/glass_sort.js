// Watchlist order "Entry on / near entry first": running trades, then strikes whose LTP is
// within NEAR_PCT of their entry (closest first), then forming setups, then the rest in
// strike order, then trades already closed today. Plain function so tests can run it in Node.
(function (root) {
  "use strict";
  const NEAR_PCT = 5;
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
  const api = {NEAR_PCT, isNear, entryRank, entryFirst};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.GlassSort = api;
})(this);
