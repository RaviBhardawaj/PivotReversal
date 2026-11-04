// Glass dashboard: browser view of the running launcher. All state comes from /api/state.
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
  $("status-text").textContent = st.text || "Ready";
  $("statusbar").style.color = st.color || "";
  $("b-start").disabled = !s.start_enabled;
  $("b-stop").disabled = !s.stop_enabled;
  $("b-stop").classList.toggle("live", !!s.stop_enabled);
}

function dirClass(d) { d = String(d || "").toUpperCase(); return /BULL|BREAKOUT|CE/.test(d) ? "up" : /BEAR|REVERSAL|PE/.test(d) ? "dn" : "neu"; }

function renderUnderlying(s) {
  const u = s.spot || {};
  $("u-title").textContent = "Underlying · " + (u.symbol || "");
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
  $("o-title").textContent = "Auto-trade · " + (o.broker || "-");
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

// reversal dashboard
const LV = ["S3", "S2", "S1", "P", "R1", "R2", "R3"], PLAN = ["Plan S2", "Plan P", "Plan R2", "Plan Signal", "vs Plan"];
const LEFT = new Set(["Expiry", "Type", "Side", "Trading Zone", "5M State", "Signal", "Status", "Order", "Plan Signal", "vs Plan"]);
let localFilters = null;

function filterState(s) {
  if (localFilters && held("filters")) return localFilters;
  localFilters = null;
  return {filters: s.filters || {}, exp_cur: !!s.exp_cur, exp_next: !!s.exp_next, show_plan: !!s.show_plan};
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
  h += `<span class="fg"><span class="g">📅 Expiry</span>${chip('data-x="exp_cur"', "⚡ Current", f.exp_cur)}${chip('data-x="exp_next"', "⏭ Next", f.exp_next)}</span>`;
  h += chip('data-x="show_plan"', "📅 Show plan columns", f.show_plan) + '<button class="chip" data-clear>✖ Clear filters</button>';
  const el = $("d-filters");
  if (el.dataset.k !== h) { el.dataset.k = h; el.innerHTML = h; }
  $("d-info").textContent = s.filter_info || "";
}

$("d-filters").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b || !S) return;
  const f = JSON.parse(JSON.stringify(filterState(S)));
  if (b.hasAttribute("data-clear")) {
    localFilters = {filters: {}, exp_cur: false, exp_next: false, show_plan: false}; holdFor("filters");
    renderFilters(S); renderDash(S); act("/api/filters", {clear: true}); return;
  }
  if (b.dataset.g) {
    const set = new Set(f.filters[b.dataset.g] || []);
    set.has(b.dataset.k) ? set.delete(b.dataset.k) : set.add(b.dataset.k);
    f.filters[b.dataset.g] = [...set];
  } else if (b.dataset.x) f[b.dataset.x] = !f[b.dataset.x];
  localFilters = f; holdFor("filters");
  renderFilters(S); renderDash(S);
  api("/api/filters", f).then((r) => { if (r.ok === false) toast(r.error || "Filter not applied", true); });
});

function cellHTML(c, v) {
  if (c === "Type") return `<span class="tag ${v === "PE" ? "pe" : "ce"}">${esc(v)}</span>`;
  if (c === "Side") { const buy = /BUY/.test(v); return `<span class="tag ${buy ? "buy" : "sell"}">${buy ? "BUY" : "SELL"}</span>`; }
  return esc(v);
}

function renderDash(s) {
  const cols = s.columns || [];
  const showPlan = filterState(s).show_plan;
  const disp = cols.filter((c) => !LV.includes(c) && !PLAN.includes(c)).concat(LV.filter((c) => cols.includes(c)), showPlan ? PLAN.filter((c) => cols.includes(c)) : []);
  const head = "<tr>" + disp.map((c) => `<th class="${LEFT.has(c) ? "l" : ""}">${esc(c)}</th>`).join("") + "</tr>";
  if ($("d-head").dataset.k !== head) { $("d-head").dataset.k = head; $("d-head").innerHTML = head; }
  const rows = s.rows || [];
  const body = rows.length ? rows.map((r) => `<tr class="${esc(r.tag)}">` + disp.map((c) => `<td class="${LEFT.has(c) ? "l" : ""}">${cellHTML(c, r.v[c] ?? "")}</td>`).join("") + "</tr>").join("")
    : `<tr class="empty"><td colspan="${disp.length || 1}">${s.feed_running ? "No rows match the filters" : "Start the live scanner to fill the dashboard"}</td></tr>`;
  if ($("d-body").dataset.k !== body) { $("d-body").dataset.k = body; $("d-body").innerHTML = body; }
  $("d-maxoi").textContent = s.max_oi || "";
}

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
    if (s.ready === false && !s.controls) { $("status-text").textContent = "Waiting for the launcher window…"; return; }
    failures = 0; render(s);
  } catch (e) {
    if (++failures >= 2) {
      $("status-text").textContent = "Cannot reach the launcher. Is Options-angel_live-ok.py running?";
      $("statusbar").style.color = "#f87171";
      $("pill-feed").className = "pill off"; $("pill-feed").lastElementChild.textContent = "Launcher offline";
    }
  }
}
setInterval(poll, 1000);
poll();

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
  $("mp-head").innerHTML = "<tr>" + r.columns.map((c, i) => `<th class="${i < 3 ? "l" : ""}">${esc(c)}</th>`).join("") + "</tr>";
  $("mp-body").innerHTML = r.rows.length ? r.rows.map((row) => `<tr class="${row.type === "PE" ? "pe" : "ce"}">` +
    r.columns.map((c, i) => `<td class="${i < 3 ? "l" : ""}">${esc(row.v[c])}</td>`).join("") + "</tr>").join("")
    : `<tr class="empty"><td colspan="${r.columns.length}">No rows for this view yet</td></tr>`;
}
$("b-plan").addEventListener("click", () => { openModal("m-plan"); loadPlan(); });
$("mp-modes").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { planMode = b.dataset.m; loadPlan(); } });
$("mp-s3").addEventListener("change", () => loadPlan());
$("mp-refresh").addEventListener("click", () => loadPlan(true));

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
