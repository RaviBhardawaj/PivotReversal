// Glass dashboard page. All state comes from /api/state (glass_server.py).
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const num = (v, d = 2) => (v || v === 0) && isFinite(v) && Number(v) !== 0 ? Number(v).toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d}) : "--";
let S = null;                 // last state
const hold = {};              // control -> time until which server values must not overwrite local edits
const held = (k) => (hold[k] || 0) > Date.now();
const holdFor = (k, ms = 2500) => { hold[k] = Date.now() + ms; };

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { j = {ok: false, error: `HTTP ${r.status}`}; }
  if (!r.ok && j.ok === undefined) j.ok = false;
  return j;
}

function toast(text, err = false) {
  const t = $("toast");
  t.textContent = text; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 4000);
}
function openModal(id) { $(id).hidden = false; }
function closeModal(id) { $(id).hidden = true; }
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-close]");
  if (c) closeModal(c.closest(".modal").id);
  else if (e.target.classList.contains("modal")) closeModal(e.target.id);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".modal:not([hidden])").forEach((m) => closeModal(m.id)); });

function message(title, text) { $("mm-title").textContent = title; $("mm-text").textContent = text; openModal("m-msg"); }
function confirmBox(title, text, okLabel = "Continue") {
  return new Promise((res) => {
    $("mc-title").textContent = title; $("mc-text").textContent = text; $("mc-ok").textContent = okLabel;
    const m = $("m-confirm");
    const done = (v) => { m.hidden = true; $("mc-ok").onclick = null; m.removeEventListener("click", onBg); res(v); };
    const onBg = (e) => { if (e.target === m || e.target.closest("[data-close]")) done(false); };
    $("mc-ok").onclick = () => done(true);
    m.addEventListener("click", onBg);
    m.hidden = false;
  });
}
async function act(path, body, okMsg) {
  const r = await api(path, body);
  if (r.ok === false && r.error) toast(r.error, true);
  else if (r.message || okMsg) toast(r.message || okMsg);
  poll();
  return r;
}

// ── rendering ────────────────────────────────────────────
function setSelect(el, values, cur) {
  const want = values.join("\u0001") + "\u0002" + cur;
  if (el.dataset.k === want) return;
  el.dataset.k = want;
  const vals = values.slice();
  if (cur && !vals.includes(cur)) vals.unshift(cur);
  el.innerHTML = (vals.length ? "" : '<option value="">--</option>') + vals.map((v) => `<option${v === cur ? " selected" : ""}>${esc(v)}</option>`).join("");
  el.value = cur;
}

function renderControls(c) {
  if (!c) return;
  const busy = (el) => document.activeElement === el;
  if (!held("exchange") && !busy($("c-exchange"))) setSelect($("c-exchange"), c.exchanges || [], c.exchange);
  if (!held("symbol") && !busy($("c-symbol"))) $("c-symbol").value = c.symbol || "";
  const dl = $("c-symbols"), k = (c.symbols || []).join(",");
  if (dl.dataset.k !== k) { dl.dataset.k = k; dl.innerHTML = (c.symbols || []).map((s) => `<option value="${esc(s)}">`).join(""); }
  if (!held("expiry") && !busy($("c-expiry"))) setSelect($("c-expiry"), c.expiries || [], c.expiry);
  if (!held("next") && !busy($("c-next"))) setSelect($("c-next"), [""].concat(c.expiries || []), c.next_expiry || "");
  if (!held("usenext")) $("c-usenext").checked = !!c.use_next;
  if (!held("strikes") && !busy($("c-strikes"))) { $("c-strikes").value = c.strikes; $("c-strikes-val").textContent = c.strikes; }
}

function renderHeader(s) {
  $("pill-market").className = "pill " + (s.market_open ? "on" : "off");
  $("pill-market").lastElementChild.textContent = s.market_open ? "Market open" : "Market closed";
  $("pill-feed").className = "pill " + (s.feed_running ? "on" : "off");
  $("pill-feed").lastElementChild.textContent = s.feed_running ? "Scanner live" : "Scanner stopped";
  $("pill-clock").textContent = (s.now || "--") + " IST";
  const st = s.status || {};
  $("status-text").textContent = String(st.text || "Ready").replace(/^\s*[●•]\s*/, "");
  $("statusbar").style.color = st.color || "";
  $("launcher-banner").hidden = !s.launcher_running;
  $("b-start").disabled = !s.start_enabled;
  $("b-stop").disabled = !s.stop_enabled;
  $("b-stop").classList.toggle("live", !!s.stop_enabled);
}

function dirClass(d) { d = String(d || "").toUpperCase(); return /BULL|BREAKOUT|CE/.test(d) ? "up" : /BEAR|REVERSAL|PE/.test(d) ? "dn" : "neu"; }

function renderUnderlying(s) {
  const u = s.spot || {};
  $("u-title").textContent = (u.symbol || "Underlying") + " spot";
  $("u-price").textContent = u.price ? num(u.price) : "--";
  $("u-dir").textContent = u.direction || "";
  $("u-dir").className = "mono " + dirClass(u.direction);
  $("u-zone").textContent = u.zone || "--";
  $("u-direction").textContent = u.direction || "--";
  $("u-direction").className = dirClass(u.direction);
  $("u-h1").textContent = u.h1_bias ? `${u.h1_bias}${u.h1_zone ? " · " + u.h1_zone : ""}` : "--";
  $("u-h1").className = dirClass(u.h1_bias);
  const fl = (f) => f ? `CE ${f.ce.join(" / ") || "-"} · PE ${f.pe.join(" / ") || "-"} · P ${f.p.join(" / ") || "-"}` : "-";
  $("u-f15").textContent = "⭐ 15M · " + fl((s.focus || {})["15M"]);
  $("u-f1h").textContent = "⏳ 1H · " + fl((s.focus || {})["1H"]);
  $("u-desc").textContent = [u.dir_desc, u.h1_action].filter(Boolean).join(" · ");
  $("u-f15").title = s.sec_15m || ""; $("u-f1h").title = s.sec_1h || "";
}

function renderOrders(o) {
  o = o || {};
  $("o-title").textContent = (o.auto ? "Auto-trade ON · " : "Auto-trade off · ") + (o.broker || "-");
  $("o-trades").textContent = o.trades !== undefined ? `${o.trades} / ${o.max_trades ?? "-"}` : "--";
  const pnl = Number(o.pnl_rs || 0);
  $("o-pnl").textContent = o.pnl_rs !== undefined ? "₹" + pnl.toLocaleString("en-IN", {maximumFractionDigits: 0}) : "--";
  $("o-pnl").className = "mono " + (pnl > 0 ? "up" : pnl < 0 ? "dn" : "");
  $("o-open").textContent = o.open ?? "--";
  $("o-limit").textContent = o.max_loss_rs ? "₹" + Number(o.max_loss_rs).toLocaleString("en-IN") : "--";
  $("o-note").textContent = o.error || (!o.connected ? (o.status || "") : (o.note || "")).slice(0, 160);
  $("o-conn").className = "pill " + (o.connected ? "on" : "off");
  $("o-conn").lastElementChild.textContent = o.connected ? "Broker connected" : "Broker not connected";
  $("b-auto").textContent = "AUTO-TRADE: " + (o.auto ? "ON" : "OFF");
  $("b-auto").classList.toggle("autoon", !!o.auto);
}

function renderPivots(p) {
  const col = {"Weekly": "var(--p)", "Daily": "#93c5fd", "1-Hour": "var(--y)", "15-Min": "#86efac"};
  const cell = (v, cls) => `<td class="${cls}">${v ? num(v) : "--"}</td>`;
  const html = (p || []).map((r) => `<tr><td class="l" style="color:${col[r.tf]}">${esc(r.tf)}</td><td class="l">${esc(r.zone)}</td>` +
    cell(r.S3, "lvl-s") + cell(r.S2, "lvl-s") + cell(r.S1, "lvl-s") + cell(r.P, "lvl-p") + cell(r.R1, "lvl-r") + cell(r.R2, "lvl-r") + cell(r.R3, "lvl-r") + "</tr>").join("");
  if ($("mtf-body").dataset.k !== html) { $("mtf-body").dataset.k = html; $("mtf-body").innerHTML = html; }
}

// ── live trades + watchlist (current expiry only) ─────────
const LV = ["S3", "S2", "S1", "P", "R1", "R2", "R3"], PLAN = ["Plan S2", "Plan P", "Plan R2", "Plan Signal", "vs Plan"];
let localFilters = null;
const pref = (k, d) => { try { const v = localStorage.getItem("glass." + k); return v === null ? d : v; } catch (e) { return d; } };
const setPref = (k, v) => { try { localStorage.setItem("glass." + k, v); } catch (e) {} };
let wSort = pref("sort2", "entry"), wFull = pref("full", "0") === "1";
$("w-sort").value = wSort; $("w-full").checked = wFull;
$("w-sort").addEventListener("change", (e) => { wSort = e.target.value; setPref("sort2", wSort); if (S) renderDash(S); });
$("w-full").addEventListener("change", (e) => { wFull = e.target.checked; setPref("full", wFull ? "1" : "0"); if (S) renderDash(S); });

function filterState(s) {
  if (localFilters && held("filters")) return localFilters;
  localFilters = null;
  return {filters: s.filters || {}, show_plan: !!s.show_plan, expiry_view: s.expiry_view || "CURRENT"};
}

function renderFilters(s) {
  const f = filterState(s);
  const chip = (attrs, label, on, extra = "") => `<button class="chip${on ? " a" : ""}${extra}" ${attrs}>${esc(label)}</button>`;
  let h = "";
  for (const g of s.filter_groups || []) {
    const ticked = new Set(f.filters[g.group] || []);
    h += `<span class="fg"><span class="g">${esc(g.group)}</span>` +
      g.options.map((o) => chip(`data-g="${esc(g.group)}" data-k="${esc(o.key)}"`, o.label, ticked.has(o.key), o.key === "P" ? " p" : "")).join("") + "</span>";
  }
  const ev = f.expiry_view || "CURRENT";
  h += `<span class="fg"><span class="g">Expiry</span>` +
    chip('data-ev="CURRENT"', `Current${s.current_expiry ? " · " + s.current_expiry : ""}`, ev === "CURRENT") +
    chip('data-ev="NEXT"', `Next${s.next_expiry ? " · " + s.next_expiry : ""}`, ev === "NEXT") +
    chip('data-ev="ALL"', "All expiries", ev === "ALL") + "</span>";
  h += chip('data-x="show_plan"', "📅 Plan columns", f.show_plan) + '<button class="chip" data-clear>✖ Clear filters</button>';
  const el = $("d-filters");
  if (el.dataset.k !== h) { el.dataset.k = h; el.innerHTML = h; }
  $("d-info").textContent = s.filter_info || "";
}

$("d-filters").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b || !S) return;
  const f = JSON.parse(JSON.stringify(filterState(S)));
  if (b.hasAttribute("data-clear")) {
    localFilters = {filters: {}, show_plan: false, expiry_view: filterState(S).expiry_view}; holdFor("filters");
    renderFilters(S); renderDash(S); act("/api/filters", {clear: true}); return;
  }
  if (b.dataset.g) {
    const set = new Set(f.filters[b.dataset.g] || []);
    set.has(b.dataset.k) ? set.delete(b.dataset.k) : set.add(b.dataset.k);
    f.filters[b.dataset.g] = [...set];
  } else if (b.dataset.ev) f.expiry_view = b.dataset.ev;
  else if (b.dataset.x) f[b.dataset.x] = !f[b.dataset.x];
  localFilters = f; holdFor("filters");
  renderFilters(S); renderDash(S);
  api("/api/filters", f).then((r) => { if (r.ok === false) toast(r.error || "Filter not applied", true); });
});

// ── row model ──
const fnum = (v) => { const m = String(v ?? "").replace(/,/g, "").match(/-?\d+(?:\.\d+)?/); return m ? parseFloat(m[0]) : 0; };
const f2 = (v) => v > 0 ? v.toFixed(2) : "-";
const strikeNum = (v) => String(v ?? "").replace(/[^\d.]/g, "");

function rowInfo(r) {
  const v = r.v, tag = r.tag || "", status = String(v.Status || "");
  const buy = /BUY/.test(v.Side || "");
  const hits = /T5|Target 5/.test(status) ? 5 : /T4|Target 4/.test(status) ? 4 : /T3|Target 3|All Targets/.test(status) ? 3 : /T2|Target 2/.test(status) ? 2 : /T1|Target 1/.test(status) ? 1 : 0;
  const isSL = tag === "sl" || /SL Hit|SL HIT/.test(status);
  const isExit = /Exit|Closed|Square/i.test(status);
  const live = tag === "active_buy" || tag === "active_sell";
  const trade = live || tag === "target" || isSL || (hits > 0);
  const closed = isSL || isExit || hits >= 3;
  const setup = /ready/.test(tag);
  const entry = fnum(v.Entry), ltp = fnum(v.LTP), sl = fnum(v.SL);
  const tg = ["T1", "T2", "T3", "T4", "T5"].map((t) => fnum(v[t]));
  const move = entry > 0 && ltp > 0 ? (buy ? ltp - entry : entry - ltp) : null;
  const gap = entry > 0 && ltp > 0 ? (ltp - entry) / entry * 100 : null;   // LTP vs planned entry
  const zone = String(v["Trading Zone"] || "");
  return {v, tag, status, buy, hits, isSL, live, trade, closed, setup, entry, ltp, sl, tg, move, gap,
    is15: /15M/.test(zone), is1h: /1H/.test(zone), isP: /🔷/.test(String(v.Strike)) || /🔷P/.test(zone)};
}

function contractCell(i, withSide) {
  const v = i.v;
  return `<span class="stk">${esc(strikeNum(v.Strike))}</span><span class="tag ${v.Type === "PE" ? "pe" : "ce"}">${esc(v.Type)}</span>` +
    (withSide ? ` <span class="tag ${i.buy ? "buy" : "sell"}">${i.buy ? "BUY" : "SELL"}</span>` : "") +
    (i.isP ? `<span class="tag p">P</span>` : "") + (i.is15 ? `<span class="tag tf">15M</span>` : "") + (i.is1h ? `<span class="tag tf">1H</span>` : "") +
    (withSide && v.Expiry ? `<span class="sub">${esc(v.Expiry)}</span>` : "");
}
function statusPill(i) {
  const s = esc(String(i.status).replace(/^[^\w]+/u, "") || "Pending");
  if (i.isSL) return `<span class="st loss">🛑 ${s}</span>`;
  if (i.closed) return `<span class="st win">🏆 ${s}</span>`;
  if (i.live || i.hits) return `<span class="st live${i.buy ? "" : " sell"}">● ${i.hits ? `LIVE · T${i.hits} hit` : "LIVE"}</span>`;
  if (i.setup) return `<span class="st setup">⚡ ${esc(i.v["5M State"] || "Setup")}</span>`;
  return `<span class="st">${esc(i.v["5M State"] || "Waiting")}</span>`;
}
const signed = (x, d = 2) => (x > 0 ? "+" : "") + x.toFixed(d);

// Keyed table sync: rows are created once and only the cells that changed are rewritten,
// so hover, tooltips and checkboxes stay put and the browser does far less work each second.
function syncRows(tbody, items, sig, emptyHTML) {
  if (tbody._sig !== sig) { tbody._sig = sig; tbody._map = new Map(); tbody.innerHTML = ""; }
  const map = tbody._map;
  if (!items.length) {
    if (!tbody._empty || tbody._emptyHTML !== emptyHTML) { map.clear(); tbody.innerHTML = emptyHTML; tbody._empty = true; tbody._emptyHTML = emptyHTML; }
    return;
  }
  if (tbody._empty) { tbody.innerHTML = ""; tbody._empty = false; }
  const seen = new Set();
  items.forEach((it, idx) => {
    seen.add(it.key);
    let e = map.get(it.key);
    if (!e) {
      const tr = document.createElement("tr");
      it.cells.forEach(() => tr.appendChild(document.createElement("td")));
      e = {tr, h: [], c: [], n: []}; map.set(it.key, e);
    }
    if (e.tr.className !== it.cls) e.tr.className = it.cls;
    it.cells.forEach((c, j) => {
      const td = e.tr.children[j];
      if (e.c[j] !== c.c) { e.c[j] = c.c; td.className = c.c || ""; }
      if (e.h[j] !== c.h) {
        e.h[j] = c.h; td.innerHTML = c.h;
        if (c.tick !== undefined && e.n[j] !== undefined && c.tick !== e.n[j]) {
          const up = c.tick > e.n[j], a = td._f === 1 ? 2 : 1; td._f = a;
          td.classList.remove("fu1", "fu2", "fd1", "fd2"); td.classList.add((up ? "fu" : "fd") + a);
        }
        if (c.title !== undefined) td.title = c.title;
      }
      if (c.tick !== undefined) e.n[j] = c.tick;
    });
    if (tbody.children[idx] !== e.tr) tbody.insertBefore(e.tr, tbody.children[idx] || null);
  });
  for (const [k, e] of map) if (!seen.has(k)) { e.tr.remove(); map.delete(k); }
}

function renderLive(s, infos) {
  const trades = infos.filter((i) => i.trade);
  trades.sort((a, b) => (a.closed - b.closed));
  const running = trades.filter((i) => !i.closed);
  $("lt-count").textContent = running.length;
  $("k-live").textContent = running.length;
  $("live-card").classList.toggle("has", running.length > 0);
  const pts = running.reduce((t, i) => t + (i.move || 0), 0);
  $("lt-summary").innerHTML = trades.length ? `${running.length} running · ${trades.length - running.length} closed today` +
    (running.length ? ` · open move <b class="mono ${pts >= 0 ? "up" : "dn"}">${signed(pts)} pts</b>` : "") : "";
  const items = trades.map((i) => {
    const nx = i.closed ? 0 : Math.min(i.hits + 1, 5);
    const nxv = nx ? i.tg[nx - 1] : 0;
    const risk = !i.closed && i.sl > 0 && i.ltp > 0 ? (i.buy ? i.ltp - i.sl : i.sl - i.ltp) : null;
    const pct = i.move !== null && i.entry > 0 ? i.move / i.entry * 100 : null;
    const ladder = `<span class="ladder">${i.tg.map((t, j) => t > 0 ? `<span class="${j < i.hits ? "hit" : j + 1 === nx ? "nx" : ""}">T${j + 1} ${t.toFixed(1)}</span>` : "").join("")}</span>`;
    return {key: i.r.k || i.r.v.Strike + i.r.v.Type + i.r.v.Side, cls: i.closed ? "closed" : "",
      cells: [
        {h: contractCell(i, true), c: "l"},
        {h: statusPill(i), c: "l", title: i.v.Signal || ""},
        {h: f2(i.entry)},
        {h: `<span class="big">${f2(i.ltp)}</span>`, tick: i.ltp},
        {h: i.move === null || i.closed ? "-" : `<span class="big ${i.move >= 0 ? "up" : "dn"}">${signed(i.move)}</span><span class="sub ${i.move >= 0 ? "up" : "dn"}">${signed(pct, 1)}%</span>`},
        {h: esc(i.v.SL || "-")},
        {h: risk === null ? "-" : `<span class="${risk >= 0 ? "" : "dn"}">${risk.toFixed(2)}</span>`},
        {h: nxv > 0 ? `T${nx} <b>${nxv.toFixed(2)}</b>${i.ltp > 0 ? `<span class="sub">${(i.buy ? nxv - i.ltp : i.ltp - nxv).toFixed(2)} away</span>` : ""}` : "-"},
        {h: ladder, c: "l"},
        {h: esc(i.v.Order || "-"), c: "l small"},
      ]};
  });
  syncRows($("lt-body"), items, "lt",
    `<tr class="empty"><td colspan="10">${s.feed_running ? "No live trades yet. Entries appear here the moment a setup triggers." : "Start the live scanner to see live trades"}</td></tr>`);
}

let dashSelectedKeys = new Map();    // row key -> {strike, type, expiry}

function renderDash(s) {
  const cols = s.columns || [];
  const showPlan = filterState(s).show_plan;
  const infos = (s.rows || []).map((r) => Object.assign(rowInfo(r), {r}));
  renderLive(s, infos);
  const ev = filterState(s).expiry_view;
  $("w-exp").textContent = ev === "ALL" ? "all expiries" : ev === "NEXT" ? `${s.next_expiry || "next"} expiry` : `${s.current_expiry || "current"} expiry`;

  // Compact columns by default; "All columns" adds the rest of the launcher's columns.
  const base = [
    ["☑", "c"], ["Strike", "l"], ["Expiry", "l"], ["Side", "l"], ["LTP", ""], ["Setup", "l"], ["Entry", ""], ["SL", ""],
    ["T1", ""], ["T2", ""], ["T3", ""], ["Levels S2 · S1 · P · R1 · R2", ""], ["Order", "l"], ["Signal", "l"],
  ];
  const extra = wFull ? ["Status", "OI", "Vol", "LL", "HH", "S3", "R3", "T4", "T5", "Trading Zone"].filter((c) => cols.includes(c)) : [];
  const plan = showPlan ? PLAN.filter((c) => cols.includes(c)) : [];
  const heads = base.concat(extra.map((c) => [c, /Trading Zone|Status/.test(c) ? "l" : ""]), plan.map((c) => [c, /Signal|vs/.test(c) ? "l" : ""]));
  const sig = heads.map((h) => h[0]).join("|");
  const head = "<tr>" + heads.map(([c, cl], j) => j === 0 ? `<th class="c"><input type="checkbox" id="dash-chk-all" title="Select all"></th>` : `<th class="${cl}">${esc(c)}</th>`).join("") + "</tr>";
  if ($("d-head").dataset.k !== head) { $("d-head").dataset.k = head; $("d-head").innerHTML = head; }

  let list = infos.slice();
  if (wSort === "entry") {
    list = GlassSort.entryFirst(list);
  } else if (wSort === "setup") {
    const rank = (i) => (i.trade && !i.closed ? 0 : i.setup ? 1 : i.trade ? 3 : 2);
    list.sort((a, b) => rank(a) - rank(b));
  } else if (wSort === "near") {
    const d = (i) => (i.gap === null ? 1e9 : Math.abs(i.gap));
    list.sort((a, b) => d(a) - d(b));
  }
  const live = new Set(list.map((i) => i.r.k));
  for (const k of [...dashSelectedKeys.keys()]) if (!live.has(k)) dashSelectedKeys.delete(k);

  const items = list.map((i) => {
    const v = i.v, k = i.r.k;
    const lv = ["S2", "S1", "P", "R1", "R2"].map((c) => { const x = fnum(v[c]); return x > 0 ? x.toFixed(1) : "-"; }).join(" · ");
    const gapTxt = i.gap === null || i.trade ? "" : `<span class="sub${GlassSort.isNear(i) ? " near" : ""}">${signed(i.gap, 1)}% vs entry</span>`;
    const cells = [
      {h: `<input type="checkbox" class="dash-row-chk" data-key="${esc(k)}"${dashSelectedKeys.has(k) ? " checked" : ""}> <button class="del" title="Delete this strike: removed, no new entries today" data-del="${esc(k)}">🗑</button>`, c: "c"},
      {h: contractCell(i, false), c: "l"},
      {h: esc(v.Expiry || "-"), c: "l small"},
      {h: `<span class="tag ${i.buy ? "buy" : "sell"}">${i.buy ? "BUY" : "SELL"}</span>`, c: "l"},
      {h: `<b>${f2(i.ltp)}</b>${gapTxt}`, tick: i.ltp},
      {h: statusPill(i), c: "l"},
      {h: esc(v.Entry || "-")}, {h: esc(v.SL || "-")},
      {h: esc(v.T1 || "-")}, {h: esc(v.T2 || "-")}, {h: esc(v.T3 || "-")},
      {h: `<span class="lvls">${lv}</span>`},
      {h: esc(v.Order || "-"), c: "l small"},
      {h: esc(v.Signal || ""), c: "l sig", title: v.Signal || ""},
    ].concat(extra.map((c) => ({h: esc(v[c] ?? ""), c: /Trading Zone|Status/.test(c) ? "l small" : ""})),
             plan.map((c) => ({h: esc(v[c] ?? ""), c: /Signal|vs/.test(c) ? "l small" : ""})));
    return {key: k, cls: i.tag, cells};
  });
  syncRows($("d-body"), items, sig,
    `<tr class="empty"><td colspan="${heads.length}">${s.feed_running ? "No rows match the filters" : "Start the live scanner to fill the dashboard"}</td></tr>`);
  $("d-maxoi").textContent = s.max_oi || "";

  const allChk = $("dash-chk-all");
  if (allChk) {
    allChk.checked = list.length > 0 && list.every((i) => dashSelectedKeys.has(i.r.k));
    allChk.indeterminate = !allChk.checked && list.some((i) => dashSelectedKeys.has(i.r.k));
  }
  renderBulk(s);
}

function renderBulk(s) {
  const n = dashSelectedKeys.size;
  $("bulk").hidden = n === 0;
  $("dash-del-num").textContent = n;
  $("dash-sel-count").textContent = `${n} selected`;
}

function rowItem(k) {
  const r = ((S && S.rows) || []).find((x) => x.k === k);
  return r ? {strike: strikeNum(r.v.Strike), type: r.v.Type, expiry: r.v.Expiry || (S.current_expiry || "")} : null;
}
async function deleteItems(items, label) {
  if (!items.length) return false;
  const ok = await confirmBox("Delete strike" + (items.length > 1 ? "s" : ""),
    `Delete ${label}?\n\nIt is removed from the dashboard and the strategy takes no new entry on it today (saved, also after a restart). A trade that is already open is not touched.`, "Delete");
  if (!ok) return false;
  const r = await api("/api/strikes/delete-multiple", {strikes: items.map((it) => ({strike: it.strike, opt_type: it.type, expiry: it.expiry}))});
  if (r.ok === false) { toast(r.error || "Delete failed", true); return false; }
  toast(`🗑 ${r.message || "Deleted"}`);
  poll();
  return true;
}

$("d-head").addEventListener("change", (e) => {
  if (e.target.id !== "dash-chk-all" || !S) return;
  for (const r of S.rows || []) {
    if (e.target.checked) { const it = rowItem(r.k); if (it) dashSelectedKeys.set(r.k, it); } else dashSelectedKeys.delete(r.k);
  }
  renderDash(S);
});
$("d-body").addEventListener("change", (e) => {
  const chk = e.target.closest(".dash-row-chk"); if (!chk) return;
  const k = chk.dataset.key;
  if (chk.checked) { const it = rowItem(k); if (it) dashSelectedKeys.set(k, it); } else dashSelectedKeys.delete(k);
  if (S) renderDash(S);
});
$("d-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]"); if (!b) return;
  const it = rowItem(b.dataset.del); if (!it) return;
  if (await deleteItems([it], `${it.strike} ${it.type} ${it.expiry}`)) dashSelectedKeys.delete(b.dataset.del);
});
$("dash-del-selected").addEventListener("click", async () => {
  const items = [...dashSelectedKeys.values()];
  const uniq = [...new Map(items.map((it) => [`${it.strike}|${it.type}|${it.expiry}`, it])).values()];
  if (await deleteItems(uniq, `${uniq.length} selected strike(s)`)) { dashSelectedKeys.clear(); if (S) renderDash(S); }
});
$("dash-sel-clear").addEventListener("click", () => { dashSelectedKeys.clear(); if (S) renderDash(S); });
function renderTools(s) {
  if (!held("source") && document.activeElement !== $("t-source")) setSelect($("t-source"), s.level_sources || [], s.level_source || "");
  $("b-sheet").href = s.sheet_url || "#";
  const opts = [["ALL", "ALL"], ["15M Intraday", "15M"], ["1H Structure", "1H"]];
  const cur = held("ft") ? hold.ftv : s.ft_filter;
  const h = opts.map(([v, t]) => `<button class="chip${v === cur ? " a" : ""}" data-v="${esc(v)}">${t}</button>`).join("");
  if ($("t-ft").dataset.k !== h) { $("t-ft").dataset.k = h; $("t-ft").innerHTML = h; }
}

let lastNote = 0;
function renderNotes(s) {
  for (const n of s.notes || []) if (n.t > lastNote) { lastNote = n.t; toast(n.text, n.kind === "error"); }
}

function render(s) {
  S = s;
  renderHeader(s); renderControls(s.controls); renderUnderlying(s); renderOrders(s.orders);
  renderPivots(s.pivots); renderFilters(s); renderDash(s); renderTools(s); renderNotes(s);
}

let failures = 0;
async function poll() {
  try {
    const s = await api("/api/state");
    if (s.ready === false && !s.controls) { $("status-text").textContent = "Starting up…"; return; }
    failures = 0; render(s);
  } catch (e) {
    if (++failures >= 2) {
      $("status-text").textContent = "Cannot reach the glass dashboard server. Start it with run_glass_dashboard.command.";
      $("statusbar").style.color = "#f87171";
      $("pill-feed").className = "pill off"; $("pill-feed").lastElementChild.textContent = "Server offline";
    }
  }
}
// One request at a time (a slow reply no longer stacks up more requests behind it), and no
// polling while the tab is hidden; it refreshes at once when you come back to it.
let pollTimer = null;
async function pollLoop() {
  clearTimeout(pollTimer);
  if (!document.hidden) await poll();
  pollTimer = setTimeout(pollLoop, 1000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollLoop(); });
pollLoop();

// ── controls ─────────────────────────────────────────────
const sendCtl = (key, body) => { holdFor(key, 4000); return act("/api/controls", body); };
$("c-exchange").addEventListener("change", (e) => sendCtl("exchange", {exchange: e.target.value}));
$("c-symbol").addEventListener("change", (e) => { const v = e.target.value.trim().toUpperCase(); if (v) sendCtl("symbol", {symbol: v}); });
$("c-symbol").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
$("c-expiry").addEventListener("change", (e) => sendCtl("expiry", {expiry: e.target.value}));
$("c-next").addEventListener("change", (e) => sendCtl("next", {next_expiry: e.target.value}));
$("c-usenext").addEventListener("change", (e) => sendCtl("usenext", {use_next: e.target.checked}));
$("c-strikes").addEventListener("input", (e) => { holdFor("strikes"); $("c-strikes-val").textContent = e.target.value; });
$("c-strikes").addEventListener("change", (e) => sendCtl("strikes", {strikes: Number(e.target.value)}));
$("b-start").addEventListener("click", () => { $("b-start").disabled = true; act("/api/start", {}, "Starting the live scanner…"); });
$("b-stop").addEventListener("click", () => act("/api/stop", {}, "Scanner stopped"));

$("b-auto").addEventListener("click", async () => {
  const on = !(S && S.orders && S.orders.auto);
  let r = await api("/api/auto-trade", {on});
  if (r.confirm) {
    if (!(await confirmBox("Turn on LIVE auto-trading?", r.confirm, "Turn on"))) return;
    r = await api("/api/auto-trade", {on: true, confirmed: true});
  }
  if (r.ok === false) message("Auto-trade", r.error || r.message || "Not changed"); else toast(r.message || "Done");
  poll();
});
$("b-kill").addEventListener("click", async () => {
  let r = await api("/api/kill", {});
  if (r.confirm) {
    if (!(await confirmBox("STOP all trading?", r.confirm, "STOP"))) return;
    r = await api("/api/kill", {confirmed: true});
  }
  r.ok === false ? toast(r.error || "Not sent", true) : toast(r.message || "STOP sent");
  poll();
});

// ── tools ────────────────────────────────────────────────
$("b-map").addEventListener("click", () => act("/api/map-levels", {}));
$("t-source").addEventListener("change", (e) => { holdFor("source", 4000); act("/api/level-source", {label: e.target.value}); });
$("b-gap").addEventListener("click", async () => { const r = await api("/api/gap-check", {}); message(r.title || "Gap Check", r.message || r.error || ""); });
$("t-ft").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  hold.ftv = b.dataset.v; holdFor("ft"); renderTools(S || {}); act("/api/ft-filter", {value: b.dataset.v});
});

// ── next-day plan ────────────────────────────────────────
let planMode = "";
async function loadPlan(refresh = false) {
  $("mp-body").innerHTML = `<tr class="empty"><td colspan="22">${refresh ? "Refreshing focus levels…" : "Loading…"}</td></tr>`;
  const q = new URLSearchParams({mode: planMode, s3: $("mp-s3").checked ? "1" : "0", refresh: refresh ? "1" : "0"});
  const r = await api("/api/next-day-plan?" + q);
  if (r.ok === false) { $("mp-body").innerHTML = `<tr class="empty"><td colspan="22">${esc(r.error || "Plan not available")}</td></tr>`; return; }
  planMode = r.mode;
  $("mp-title").textContent = `Next-day plan & strike levels · ${r.symbol}`;
  const p = (o) => `P=${num(o.P, 1)} | R1=${num(o.R1, 1)} R2=${num(o.R2, 1)} R3=${num(o.R3, 1)} | S1=${num(o.S1, 1)} S2=${num(o.S2, 1)} S3=${num(o.S3, 1)}`;
  $("mp-info").innerHTML = `Session ${esc(r.session)} · Status ${esc(r.status)}${r.ref_spot ? " · Ref spot " + num(r.ref_spot) : ""}<br>` +
    `Index 15M pivots: ${p(r.piv15)}<br>Index 1H pivots: ${p(r.piv1h)}` +
    ((r.focus_ce || []).length || (r.focus_pe || []).length ? `<br>Plan focus: CE ${esc((r.focus_ce || []).join(", "))} | PE ${esc((r.focus_pe || []).join(", "))}` : "");
  $("mp-modes").innerHTML = r.modes.map((m) => `<button class="chip${m.key === r.mode ? " a" : ""}" data-m="${m.key}">${esc(m.label)}</button>`).join("");
  $("mp-head").innerHTML = "<tr><th></th>" + r.columns.map((c, i) => `<th class="${i < 3 ? "l" : ""}">${esc(c)}</th>`).join("") + "</tr>";
  $("mp-body").innerHTML = r.rows.length ? r.rows.map((row) => `<tr class="${row.type === "PE" ? "pe" : "ce"}"><td><button class="del" title="Delete this strike" data-plan-del="${esc(strikeNum(row.v.Strike))}|${esc(row.type)}|${esc(row.v.Expiry || "")}">🗑</button></td>` +
    r.columns.map((c, i) => `<td class="${i < 3 ? "l" : ""}">${esc(row.v[c])}</td>`).join("") + "</tr>").join("")
    : `<tr class="empty"><td colspan="${r.columns.length + 1}">No rows for this view yet</td></tr>`;
}
$("b-plan").addEventListener("click", () => { openModal("m-plan"); loadPlan(); });
$("mp-modes").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { planMode = b.dataset.m; loadPlan(); } });
$("mp-s3").addEventListener("change", () => loadPlan());
$("mp-refresh").addEventListener("click", () => loadPlan(true));
$("mp-regen").addEventListener("click", async () => {
  // Rebuild every strike's levels (the plan and orders come from them); deleted strikes stay removed.
  const r = await api("/api/map-levels", {});
  if (r.ok === false) { toast(r.error || "Regenerate failed", true); return; }
  toast("↻ Regenerating strike levels and plan (the status line shows progress)");
  loadPlan(true);
});
$("mp-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-plan-del]"); if (!b) return;
  const [strike, type, expiry] = b.dataset.planDel.split("|");
  if (await deleteItems([{strike, type, expiry}], `${strike} ${type} ${expiry}`)) loadPlan();
});

// ── settings ─────────────────────────────────────────────
let SET = null, setBroker = "", todayTimer = null;
const HINTS = {XTS_ROOT_URL: "blank = Jainam (https://jtrade.jainam.in:5000)", XTS_CLIENT_ID: "only for dealer accounts; blank otherwise",
  DHAN_ACCESS_TOKEN: "optional: made automatically from PIN + TOTP", DHAN_TOTP_SECRET: "Google Authenticator secret (base32)",
  ANGEL_CLIENT_ID: "uses the same login as the price feed"};
const ORDER_ROWS = [
  ["Lots per trade", "num", "lots"], ["Lots for 15M (0 = same)", "num", "lots_15m"], ["Lots for 1H (0 = same)", "num", "lots_1h"],
  ["Entry order", "radio", "entry_type", [["SMART_LIMIT", "Smart limit (bid/ask)"], ["LIMIT", "Limit, then market"], ["MARKET", "Market"]]],
  ["Limit buffer %", "dec", "limit_buffer_pct"], ["Go to market after (s)", "num", "chase_after_s"],
  ["Product", "radio", "product", [["INTRADAY", "Intraday"], ["CARRY", "Carry"]]],
  ["Stop-loss order at broker", "check", "sl_at_broker"], ["SL limit gap %", "dec", "sl_limit_gap_pct"],
  ["Trade timeframes", "tf"], ["Take SELL setups (short)", "check", "take_sell_setups"],
  ["Max trades per day", "num", "max_trades"], ["Max loss per day (₹)", "num", "max_loss_rs"],
];

async function openSettings() {
  openModal("m-settings");
  SET = await api("/api/settings");
  if (SET.ok === false) { $("s-bmsg").textContent = SET.error || "Settings not available"; return; }
  setBroker = SET.broker || "ANGEL";
  drawBrokers(); drawOrders(); drawStrikes();
  $("s-bmsg").textContent = SET.status_msg || "";
}
function drawBrokers() {
  $("s-brokers").innerHTML = Object.entries(SET.brokers).map(([code, b]) =>
    `<label class="toggle"><input type="radio" name="brk" value="${code}"${code === setBroker ? " checked" : ""}> ${esc(b.label)}</label>`).join("");
  const keys = SET.brokers[setBroker].keys;
  $("s-keys").innerHTML = keys.map((k) => {
    const hint = (k.secret && k.saved) ? "saved; type to replace" : (HINTS[k.key] || "");
    return `<label for="k-${k.key}">${k.key}</label><input id="k-${k.key}" data-key="${k.key}" type="${k.secret ? "password" : "text"}" value="${esc(k.value)}" autocomplete="off">` +
      (hint ? `<div class="hint">${esc(hint)}</div>` : "");
  }).join("");
}
$("s-brokers").addEventListener("change", (e) => { setBroker = e.target.value; drawBrokers(); });
const keyBody = () => ({broker: setBroker, keys: Object.fromEntries([...document.querySelectorAll("#s-keys input")].map((i) => [i.dataset.key, i.value]))});
$("s-test").addEventListener("click", async () => { $("s-bmsg").textContent = "Testing connection…"; const r = await api("/api/settings/test", keyBody()); $("s-bmsg").textContent = r.message || r.error || ""; });
$("s-use").addEventListener("click", async () => {
  $("s-bmsg").textContent = "Connecting…";
  const r = await api("/api/settings/use", keyBody());
  $("s-bmsg").textContent = r.message || r.error || "";
  if (r.broker && r.broker !== setBroker) { setBroker = r.broker; drawBrokers(); }
  poll();
});

function drawOrders() {
  const s = SET.settings;
  $("s-orders").innerHTML = ORDER_ROWS.map(([label, kind, key, opts]) => {
    let cell = "";
    if (kind === "num" || kind === "dec") cell = `<input type="number" data-o="${key}" data-kind="${kind}" value="${esc(s[key])}" step="${kind === "dec" ? "0.1" : "1"}" min="0">`;
    else if (kind === "radio") cell = opts.map(([v, t]) => `<label class="inl"><input type="radio" name="o-${key}" data-o="${key}" value="${v}"${s[key] === v ? " checked" : ""}> ${t}</label>`).join("");
    else if (kind === "check") cell = `<label class="inl"><input type="checkbox" data-o="${key}"${s[key] ? " checked" : ""}></label>`;
    else if (kind === "tf") cell = ["trade_15m", "trade_1h"].map((k) => `<label class="inl"><input type="checkbox" data-o="${k}"${s[k] ? " checked" : ""}> ${k === "trade_15m" ? "15M" : "1H"}</label>`).join("");
    return `<label>${esc(label)}</label><div class="cell">${cell}</div>`;
  }).join("");
}
$("s-osave").addEventListener("click", async () => {
  const body = {};
  document.querySelectorAll("#s-orders [data-o]").forEach((i) => {
    const k = i.dataset.o;
    if (i.type === "radio") { if (i.checked) body[k] = i.value; }
    else if (i.type === "checkbox") body[k] = i.checked;
    else body[k] = i.dataset.kind === "dec" ? parseFloat(i.value || "0") : parseInt(i.value || "0", 10);
  });
  const r = await api("/api/settings/save", body);
  $("s-omsg").textContent = r.message || r.error || "";
});

function strikeSel() {
  const lv = [...document.querySelectorAll("#s-strikes [data-lv]:checked")].map((i) => i.dataset.lv);
  const ty = [...document.querySelectorAll("#s-strikes [data-ty]:checked")].map((i) => i.dataset.ty);
  const tf = (document.querySelector("#s-strikes [name=stf]:checked") || {}).value || "ANY";
  return {levels: lv.join(","), opt_types: ty.join(","), strike_tf: tf, symbols: $("s-sym").value.trim(), strikes: $("s-stk").value.trim()};
}
function drawStrikes() {
  const s = SET.settings, on = new Set(SET.levels_on), types = new Set(String(s.opt_types || "").split(",").map((x) => x.trim().toUpperCase()));
  let h = SET.timeframes.map((tf) => `<label>${tf} pivot levels</label><div class="cell">` +
    SET.levels.map((lv) => `<label class="inl"><input type="checkbox" data-lv="${tf}:${lv}"${on.has(tf + ":" + lv) ? " checked" : ""}> ${esc(SET.level_labels[lv])}</label>`).join("") + "</div>").join("");
  h += `<label>Option type</label><div class="cell">` + ["CE", "PE"].map((t) => `<label class="inl"><input type="checkbox" data-ty="${t}"${types.has(t) ? " checked" : ""}> ${t}</label>`).join("") + "</div>";
  const stf = String(s.strike_tf || "ANY").toUpperCase();
  h += `<label>Timeframe match</label><div class="cell">` + [["ANY", "Any 15M or 1H strike"], ["BOTH", "Only strikes on both 15M and 1H"]]
    .map(([v, t]) => `<label class="inl"><input type="radio" name="stf" value="${v}"${stf === v ? " checked" : ""}> ${t}</label>`).join("") + "</div>";
  h += `<label for="s-sym">Symbols</label><div class="cell"><input id="s-sym" class="wide" type="text" value="${esc(s.symbols || "")}"><span class="small">e.g. NIFTY,SENSEX (blank = all)</span></div>`;
  h += `<label for="s-stk">Only these strikes</label><div class="cell"><input id="s-stk" class="wide" type="text" value="${esc(s.strikes || "")}"><span class="small">e.g. 22600,22650 (blank = all)</span></div>`;
  $("s-strikes").innerHTML = h;
  preview();
}
async function preview() {
  const r = await api("/api/settings/strikes-preview", strikeSel());
  if (r.ok === false) { $("s-preview").textContent = r.error || ""; return; }
  const lines = [];
  let any = false;
  for (const g of r.groups) {
    lines.push(`── ${g.tf} strikes ──`);
    if (!g.items.length) lines.push("   (none yet)");
    for (const it of g.items) { any = true; lines.push(`${it.ok ? "✅" : "✖"} ${it.symbol} ${String(it.strike).padStart(7)} ${it.type}  ${it.levels.padEnd(14)} ${it.why || ""}`); }
  }
  $("s-preview").textContent = any ? lines.join("\n") : "Start the feed to see today's strikes.";
}
$("s-strikes").addEventListener("change", preview);
$("s-strikes").addEventListener("input", (e) => { if (e.target.type === "text") { clearTimeout(preview._t); preview._t = setTimeout(preview, 300); } });
$("s-ssave").addEventListener("click", async () => { const r = await api("/api/settings/save", strikeSel()); $("s-smsg").textContent = r.message || r.error || ""; });

async function fillToday() {
  const r = await api("/api/orders/recent");
  const rows = (r.orders || []).map((o) => `<tr><td class="l">${esc(o.time)}</td><td class="l">${esc(o.contract)}</td><td class="l">${esc(o.action)}</td><td>${esc(o.qty)}</td><td>${esc(o.price)}</td><td class="l">${esc(o.status)}</td><td class="l">${esc(o.remark)}</td></tr>`);
  $("s-today").innerHTML = rows.join("") || '<tr class="empty"><td colspan="7">No orders today</td></tr>';
}
$("s-tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab"); if (!b) return;
  document.querySelectorAll("#s-tabs .tab").forEach((t) => t.classList.toggle("a", t === b));
  document.querySelectorAll("#m-settings .tabpane").forEach((p) => (p.hidden = p.dataset.pane !== b.dataset.tab));
  clearInterval(todayTimer);
  if (b.dataset.tab === "today") { fillToday(); todayTimer = setInterval(() => { if ($("m-settings").hidden) clearInterval(todayTimer); else fillToday(); }, 2000); }
  if (b.dataset.tab === "strikes" && SET) preview();
});
$("b-settings").addEventListener("click", openSettings);

// ── mapped strikes & radar ───────────────────────────────
let mappedConfig = null;
let mmSelectedIds = new Set();
let mmCurrentExp = "ALL";

async function loadMappedStrikes() {
  $("mm-tbody").innerHTML = '<tr class="empty"><td colspan="10">Loading mapped strikes…</td></tr>';
  const r = await api("/api/mapped-strikes");
  if (r.ok === false) {
    $("mm-tbody").innerHTML = `<tr class="empty"><td colspan="10">${esc(r.error || "Failed to load")}</td></tr>`;
    return;
  }
  mappedConfig = r.config || {strikes: []};
  
  // Expiry dropdown
  const expList = mappedConfig.all_expiries || [];
  const expOpts = ["ALL", ...expList.filter((e) => e && e !== "ALL")];
  const curExpVal = expOpts.includes(mmCurrentExp) ? mmCurrentExp : "ALL";
  $("mm-exp-filter").innerHTML = expOpts.map((e) => `<option value="${esc(e)}"${e === curExpVal ? " selected" : ""}>${e === "ALL" ? "All Expiries" : esc(e)}</option>`).join("");
  mmCurrentExp = curExpVal;

  // Add card expiry options
  $("mma-expiry").innerHTML = expList.map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("") || '<option value="">(None)</option>';
  if (mappedConfig.current_expiry && expList.includes(mappedConfig.current_expiry)) {
    $("mma-expiry").value = mappedConfig.current_expiry;
  }

  // Symbol default
  if (S && S.spot && S.spot.symbol) {
    $("mma-symbol").value = S.spot.symbol;
  }

  // Restriction mode toggle
  const restr = Boolean(mappedConfig.restrict_to_selected_only);
  $("mm-restricted-toggle").checked = restr;
  $("mm-restricted-label").textContent = restr ? "ON" : "OFF";
  $("mm-restricted-label").className = restr ? "up" : "dn";

  renderMappedTable();
}

function renderMappedTable() {
  if (!mappedConfig) return;
  const strikes = mappedConfig.strikes || [];
  const filtered = strikes.filter((s) => {
    if (mmCurrentExp === "ALL") return true;
    return String(s.expiry || "").toUpperCase() === mmCurrentExp.toUpperCase();
  });

  $("mm-del-exp-name").textContent = mmCurrentExp;
  $("mm-select-all").checked = filtered.length > 0 && filtered.every((s) => mmSelectedIds.has(s.id));

  updateDelSelectedBtn();

  if (!filtered.length) {
    $("mm-tbody").innerHTML = `<tr class="empty"><td colspan="10">No strikes mapped${mmCurrentExp !== "ALL" ? " for expiry " + esc(mmCurrentExp) : ""}. Click ⚡ Import Focus Strikes or ➕ Add Custom Strike.</td></tr>`;
    return;
  }

  $("mm-tbody").innerHTML = filtered.map((s) => {
    const isChecked = mmSelectedIds.has(s.id);
    const ot = esc(s.opt_type || "CE");
    const otClass = ot === "PE" ? "pe" : (ot === "CE" ? "ce" : "buy");
    const ltpStr = s.ltp ? num(s.ltp, 2) : "-";
    return `<tr data-id="${esc(s.id)}" style="${s.enabled ? "" : "opacity:0.55;"}">
      <td style="text-align:center;"><input type="checkbox" class="mm-chk" data-id="${esc(s.id)}"${isChecked ? " checked" : ""}></td>
      <td style="text-align:center;"><label class="toggle" style="margin:0;"><input type="checkbox" class="mm-act" data-id="${esc(s.id)}"${s.enabled ? " checked" : ""}></label></td>
      <td class="l"><b>${esc(s.symbol || "NIFTY")}</b></td>
      <td class="l mono small">${esc(s.expiry || "-")}</td>
      <td><b>${esc(s.strike)}</b></td>
      <td class="l"><span class="tag ${otClass}">${ot}</span></td>
      <td style="text-align:center;"><input type="number" class="mm-lot-inp" data-id="${esc(s.id)}" value="${s.lots || 1}" min="1" max="50" style="width:58px;text-align:center;padding:4px 6px;"></td>
      <td class="mono ${s.ltp ? "accent" : "muted"}">${ltpStr}</td>
      <td class="l small muted" title="${esc(s.notes || "")}">${esc(s.source || "Manual")}: ${esc(s.notes || "-")}</td>
      <td style="text-align:center;"><button class="btn icon stop mm-del-btn" data-id="${esc(s.id)}" title="Delete Strike">🗑️</button></td>
    </tr>`;
  }).join("");
}

function updateDelSelectedBtn() {
  const count = mmSelectedIds.size;
  $("mm-del-count").textContent = count;
  $("mm-del-selected-btn").disabled = count === 0;
  $("mm-sel-info").textContent = `${count} selected`;
}

// ── Event bindings for Mapped Strikes modal ──
$("b-mapped").addEventListener("click", () => {
  openModal("m-mapped");
  mmSelectedIds.clear();
  loadMappedStrikes();
});

$("mm-exp-filter").addEventListener("change", (e) => {
  mmCurrentExp = e.target.value;
  renderMappedTable();
});

$("mm-restricted-toggle").addEventListener("change", async (e) => {
  const on = e.target.checked;
  const r = await api("/api/mapped-strikes/toggle", {enabled: on});
  if (r.ok === false) {
    toast(r.error || "Toggle failed", true);
    e.target.checked = !on;
    return;
  }
  mappedConfig = r.config;
  $("mm-restricted-label").textContent = on ? "ON" : "OFF";
  $("mm-restricted-label").className = on ? "up" : "dn";
  toast(`Restriction mode: ${on ? "ON (Only mapped strikes traded)" : "OFF (All focus strikes traded)"}`);
});

$("mm-toggle-add-btn").addEventListener("click", () => {
  const card = $("mm-add-card");
  card.hidden = !card.hidden;
});

$("mma-cancel-btn").addEventListener("click", () => {
  $("mm-add-card").hidden = true;
});

$("mma-submit-btn").addEventListener("click", async () => {
  const sym = $("mma-symbol").value.trim().toUpperCase() || "NIFTY";
  const exp = $("mma-expiry").value.trim().toUpperCase();
  const stk = parseFloat($("mma-strike").value);
  const ot = $("mma-type").value;
  const lots = parseInt($("mma-lots").value, 10) || 1;
  const note = $("mma-note").value.trim();

  if (!stk || stk <= 0) {
    toast("Please enter a valid strike price", true);
    return;
  }

  const r = await api("/api/mapped-strikes/add", {symbol: sym, expiry: exp, strike: stk, opt_type: ot, lots, note});
  if (r.ok === false) {
    toast(r.error || "Failed to add strike", true);
    return;
  }
  mappedConfig = r.config;
  $("mma-strike").value = "";
  $("mma-note").value = "";
  $("mm-add-card").hidden = true;
  toast(`✅ Added ${sym} ${stk} ${ot} (${exp || "All"})`);
  loadMappedStrikes();
});

$("mm-select-all").addEventListener("change", (e) => {
  const checked = e.target.checked;
  const filtered = (mappedConfig?.strikes || []).filter((s) => {
    if (mmCurrentExp === "ALL") return true;
    return String(s.expiry || "").toUpperCase() === mmCurrentExp.toUpperCase();
  });
  filtered.forEach((s) => {
    if (checked) mmSelectedIds.add(s.id);
    else mmSelectedIds.delete(s.id);
  });
  renderMappedTable();
});

// Delegation for table checkboxes, lot inputs, active toggle & single delete
$("mm-tbody").addEventListener("change", (e) => {
  const chk = e.target.closest(".mm-chk");
  if (chk) {
    const id = chk.dataset.id;
    if (chk.checked) mmSelectedIds.add(id);
    else mmSelectedIds.delete(id);
    updateDelSelectedBtn();
    return;
  }
  const actInp = e.target.closest(".mm-act");
  if (actInp) {
    const id = actInp.dataset.id;
    const st = (mappedConfig?.strikes || []).find((x) => x.id === id);
    if (st) {
      st.enabled = actInp.checked;
      const tr = actInp.closest("tr");
      if (tr) tr.style.opacity = st.enabled ? "1" : "0.55";
    }
    return;
  }
  const lotInp = e.target.closest(".mm-lot-inp");
  if (lotInp) {
    const id = lotInp.dataset.id;
    const val = parseInt(lotInp.value, 10);
    const st = (mappedConfig?.strikes || []).find((x) => x.id === id);
    if (st && val > 0) st.lots = val;
    return;
  }
});

// Single Delete Button (🗑️ in row)
$("mm-tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest(".mm-del-btn");
  if (!btn) return;
  const id = btn.dataset.id;
  const st = (mappedConfig?.strikes || []).find((x) => x.id === id);
  const label = st ? `${st.symbol} ${st.strike} ${st.opt_type} (${st.expiry || "ALL"})` : id;
  if (!(await confirmBox("Delete Strike?", `Remove ${label} from mapped radar?`, "Delete"))) return;
  
  const r = await api("/api/mapped-strikes/delete", {id});
  if (r.ok === false) {
    toast(r.error || "Delete failed", true);
    return;
  }
  mmSelectedIds.delete(id);
  mappedConfig = r.config;
  toast("🗑️ Strike deleted");
  renderMappedTable();
});

// Multiple Delete: Delete Selected
$("mm-del-selected-btn").addEventListener("click", async () => {
  const count = mmSelectedIds.size;
  if (count === 0) return;
  if (!(await confirmBox("Delete Selected Strikes?", `Delete ${count} selected strike(s) from mapped radar?`, `Delete (${count})`))) return;

  const r = await api("/api/mapped-strikes/delete-multiple", {ids: [...mmSelectedIds]});
  if (r.ok === false) {
    toast(r.error || "Failed to delete strikes", true);
    return;
  }
  mmSelectedIds.clear();
  mappedConfig = r.config;
  toast(`🗑️ ${r.message || "Selected strikes deleted"}`);
  renderMappedTable();
});

// Bulk Delete by Expiry: Delete All for Expiry
$("mm-del-expiry-btn").addEventListener("click", async () => {
  if (!(await confirmBox("Delete All for Expiry?", `Delete all mapped strikes for expiry "${mmCurrentExp}"?`, "Delete Expiry"))) return;

  const r = await api("/api/mapped-strikes/delete-expiry", {
    expiry: mmCurrentExp,
    symbol: S?.spot?.symbol || "NIFTY"
  });
  if (r.ok === false) {
    toast(r.error || "Delete failed", true);
    return;
  }
  mmSelectedIds.clear();
  mappedConfig = r.config;
  toast(`🗑️ ${r.message || "Deleted strikes for expiry"}`);
  loadMappedStrikes();
});

// Clear All
$("mm-clear-all-btn").addEventListener("click", async () => {
  if (!(await confirmBox("Clear All Mapped Strikes?", "Are you sure you want to remove ALL mapped strikes across all expiries?", "Clear All"))) return;
  const r = await api("/api/mapped-strikes/clear", {});
  if (r.ok === false) {
    toast(r.error || "Clear failed", true);
    return;
  }
  mmSelectedIds.clear();
  mappedConfig = r.config;
  toast("🗑️ Cleared all mapped strikes");
  loadMappedStrikes();
});

// Import Focus Strikes
$("mm-import-btn").addEventListener("click", async () => {
  const exp = mmCurrentExp === "ALL" ? "" : mmCurrentExp;
  const r = await api("/api/mapped-strikes/import-focus", {
    symbol: S?.spot?.symbol || "NIFTY",
    expiry: exp
  });
  if (r.ok === false) {
    toast(r.error || "Import failed", true);
    return;
  }
  mappedConfig = r.config;
  toast(`⚡ ${r.message || "Imported focus strikes"}`);
  loadMappedStrikes();
});

// Save Lots & Strikes
$("mm-save-btn").addEventListener("click", async () => {
  if (!mappedConfig) return;
  const r = await api("/api/mapped-strikes/save", {
    strikes: mappedConfig.strikes,
    restrict_to_selected_only: mappedConfig.restrict_to_selected_only,
    default_lots: mappedConfig.default_lots
  });
  if (r.ok === false) {
    toast(r.error || "Save failed", true);
    return;
  }
  mappedConfig = r.config;
  toast("💾 Saved mapped strikes and lots to disk");
});
