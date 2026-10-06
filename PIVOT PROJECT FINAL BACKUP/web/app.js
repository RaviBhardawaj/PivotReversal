/**
 * Quantum Terminal — 15M & 1H Intraday Strategy Engine
 * Live Web Client & WebSocket Handler (Production Clean)
 */

(function () {
  'use strict';

  // ── Application State ──
  const state = {
    ws: null,
    wsConnected: false,
    activeTab: 'tab-scanner',
    strikeFilter: 'FOCUS',
    expiryFilter: 'CURRENT',
    currentExpiry: '',
    nextExpiry: '',
    ftFilter: 'ALL',
    audioEnabled: true,
    feedRunning: false,
    symbol: 'NIFTY',
    expiry: '',
    spotPrice: 0,
    spotZone: '',
    activeTrades: [],
    closedTrades: [],
    scorecard: null,
    lastWatchItems: [],
    lastMaxOi: '',
    plannerData: null
  };

  // ── DOM References ──
  const els = {
    // Header & Status
    marketStatusPill: document.getElementById('market-status-pill'),
    marketStatusText: document.getElementById('market-status-text'),
    currentSymLabel: document.getElementById('current-sym-label'),
    spotPriceVal: document.getElementById('spot-price-val'),
    spotZoneBadge: document.getElementById('spot-zone-badge'),
    liveIstClock: document.getElementById('live-ist-clock'),
    timer3pm: document.getElementById('timer-3pm'),
    timer345pm: document.getElementById('timer-345pm'),
    execModeBadge: document.getElementById('execution-mode-badge'),
    audioToggleBtn: document.getElementById('audio-toggle-btn'),

    // Performance Strip (4 clean institutional metrics)
    scNetPts: document.getElementById('sc-net-pts'),
    scTradesCount: document.getElementById('sc-trades-count'),
    scWinRate: document.getElementById('sc-win-rate'),
    scWinLoss: document.getElementById('sc-win-loss'),
    scLivePts: document.getElementById('sc-live-pts'),
    scActiveTradesCount: document.getElementById('sc-active-trades-count'),
    scTotalNet: document.getElementById('sc-total-net'),

    // Reference Levels & Stored Plan Ribbon
    dailyFibText: document.getElementById('daily-fib-text'),
    weeklyFibText: document.getElementById('weekly-fib-text'),
    activePlanBanner: document.getElementById('active-plan-banner'),
    activePlanDetails: document.getElementById('active-plan-details'),
    activePlanTag: document.getElementById('active-plan-tag'),

    // Scanner
    inputSymbol: document.getElementById('input-symbol'),
    selExpiry: document.getElementById('sel-expiry'),
    btnStartFeed: document.getElementById('btn-start-feed'),
    btnStopFeed: document.getElementById('btn-stop-feed'),
    pivPill15m: document.getElementById('piv-pill-15m'),
    pivPill1h: document.getElementById('piv-pill-1h'),
    tbodyOptionChain: document.getElementById('tbody-option-chain'),
    maxOiText: document.getElementById('max-oi-text'),
    tabActiveCount: document.getElementById('tab-active-count'),

    // Planner (Next-Day 15M + 1H Intraday)
    btnApplyPlanToScanner: document.getElementById('btn-apply-plan-to-scanner'),
    btnTriggerPlanner: document.getElementById('btn-trigger-planner'),
    plannerDeckContainer: document.getElementById('planner-deck-container'),

    // Forward Test & Audit Journal
    tbodyFtActive: document.getElementById('tbody-ft-active'),
    tbodyFtClosed: document.getElementById('tbody-ft-closed'),
    ftActiveBadge: document.getElementById('ft-active-badge'),
    ftClosedBadge: document.getElementById('ft-closed-badge'),
    btnManualSquareoff: document.getElementById('btn-manual-squareoff'),

    // Toast Container
    toastContainer: document.getElementById('toast-container')
  };

  // ── Web Audio Synth (Subtle Alert) ──
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  function playAlertSound() {
    if (!state.audioEnabled) return;
    try {
      if (audioCtx.state === 'suspended') audioCtx.resume();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(659.25, audioCtx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(880, audioCtx.currentTime + 0.12);
      gain.gain.setValueAtTime(0.06, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + 0.31);
    } catch (e) {
      console.warn('Audio play error:', e);
    }
  }

  // ── Toast Notifications ──
  function showToast(msg, type = 'blue') {
    if (!els.toastContainer) return;
    const t = document.createElement('div');
    t.className = `toast toast-${type}`;
    t.textContent = msg;
    els.toastContainer.appendChild(t);
    setTimeout(() => {
      t.style.opacity = '0';
      t.style.transform = 'translateY(10px)';
      setTimeout(() => t.remove(), 250);
    }, 4000);
  }

  // ── Live IST Time Clock & Timers ──
  function updateLiveClockAndTimers() {
    const now = new Date();
    const istTimeStr = now.toLocaleTimeString('en-IN', {
      timeZone: 'Asia/Kolkata',
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
    if (els.liveIstClock) els.liveIstClock.textContent = istTimeStr;

    function fmtDiff(targetHour, targetMin) {
      const tgt = new Date(now.getFullYear(), now.getMonth(), now.getDate(), targetHour, targetMin, 0);
      const diff = tgt - now;
      if (diff <= 0) return 'Passed @ ' + String(targetHour).padStart(2, '0') + ':' + String(targetMin).padStart(2, '0');
      const hrs = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      const secs = Math.floor((diff % 60000) / 1000);
      return `${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }
    if (els.timer3pm) els.timer3pm.textContent = fmtDiff(15, 0);
    if (els.timer345pm) els.timer345pm.textContent = fmtDiff(15, 45);
  }
  setInterval(updateLiveClockAndTimers, 1000);
  updateLiveClockAndTimers();

  // ── Tab Switching ──
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const tabId = btn.getAttribute('data-tab');
      state.activeTab = tabId;
      const targetPane = document.getElementById(tabId);
      if (targetPane) targetPane.classList.add('active');
      if (tabId === 'tab-planner') loadPlannerDeck(state.symbol);
    });
  });

  // ── Audio Toggle ──
  if (els.audioToggleBtn) {
    els.audioToggleBtn.addEventListener('click', () => {
      state.audioEnabled = !state.audioEnabled;
      els.audioToggleBtn.textContent = state.audioEnabled ? '🔔 AUDIO' : '🔕 MUTED';
      els.audioToggleBtn.classList.toggle('active', state.audioEnabled);
      showToast(state.audioEnabled ? 'Audio alerts enabled' : 'Audio alerts muted', 'blue');
    });
  }

  // ── WebSocket Connection ──
  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/live`;
    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
      state.wsConnected = true;
      if (els.marketStatusPill) {
        els.marketStatusPill.className = 'status-pill online';
      }
      if (els.marketStatusText) {
        els.marketStatusText.textContent = 'WEBSOCKET LIVE';
      }
      showToast('Connected to Quantum Terminal Live Feed', 'green');
    };

    state.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleWsMessage(data);
      } catch (err) {
        console.error('WS parse error:', err);
      }
    };

    state.ws.onclose = () => {
      state.wsConnected = false;
      if (els.marketStatusPill) {
        els.marketStatusPill.className = 'status-pill offline';
      }
      if (els.marketStatusText) {
        els.marketStatusText.textContent = 'DISCONNECTED — RECONNECTING...';
      }
      setTimeout(connectWebSocket, 2000);
    };

    state.ws.onerror = () => {
      state.ws.close();
    };
  }

  // ── REST Snapshot Loader ──
  async function loadDashboard(sym = null, exp = null) {
    try {
      const targetSym = sym || state.symbol || 'NIFTY';
      const targetExp = exp || state.expiryFilter || 'CURRENT';
      const url = `/api/dashboard?symbol=${encodeURIComponent(targetSym)}&expiry=${encodeURIComponent(targetExp)}`;
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      if (data) {
        handleWsMessage(data);
      }
    } catch (e) {
      console.warn('Dashboard snapshot load failed:', e);
    }
  }

  // ── Handle Incoming WebSocket & Snapshot Messages ──
  function handleWsMessage(data) {
    if (data.type === 'TICK_UPDATE') {
      updateHeaderAndSpot(data);
      updateScorecard(data.scorecard);
      if (data.current_expiry) state.currentExpiry = data.current_expiry;
      if (data.next_expiry) state.nextExpiry = data.next_expiry;
      if (data.active_expiry) {
        state.expiry = data.active_expiry;
        if (els.selExpiry && els.selExpiry.value !== data.active_expiry) {
          for (let i = 0; i < els.selExpiry.options.length; i++) {
            if (els.selExpiry.options[i].value === data.active_expiry) {
              els.selExpiry.selectedIndex = i;
              break;
            }
          }
        }
      }
      if (data.watch_items && data.watch_items.length) {
        state.lastWatchItems = data.watch_items;
      }
      if (data.max_oi) {
        state.lastMaxOi = data.max_oi;
      }
      updateOptionChainTable(state.lastWatchItems, state.lastMaxOi);
      updateForwardTestTables(data.active_trades || [], data.closed_trades || []);
    } else if (data.type === 'TRADE_ALERT') {
      playAlertSound();
      showToast(data.message, 'gold');
    }
  }

  // ── Update Header, Spot & 15M/1H Structure Banner ──
  function updateHeaderAndSpot(d) {
    if (d.spot !== undefined && els.spotPriceVal) {
      if (els.currentSymLabel) {
        els.currentSymLabel.textContent = `${d.symbol || 'NIFTY'} SPOT`;
      }
      els.spotPriceVal.textContent = Number(d.spot).toFixed(2);
      if (els.spotZoneBadge) {
        els.spotZoneBadge.textContent = d.spot_zone || 'NEUTRAL';
      }
    }
    if (d.exec_mode && els.execModeBadge) {
      els.execModeBadge.textContent = d.exec_mode === 'LIVE' ? '🔴 LIVE' : '📋 PAPER';
      els.execModeBadge.className = `mode-tag ${d.exec_mode === 'LIVE' ? 'live' : ''}`;
    }
    if (d.feed_running !== undefined && els.btnStartFeed && els.btnStopFeed) {
      state.feedRunning = d.feed_running;
      els.btnStartFeed.disabled = d.feed_running;
      els.btnStopFeed.disabled = !d.feed_running;
    }

    // Update 15M vs 1H Intraday Market Structure Banner
    if (d.symbol_pivots) {
      if (els.pivPill15m && d.symbol_pivots.p15) {
        const p = d.symbol_pivots.p15;
        els.pivPill15m.innerHTML = `P: <strong>₹${Number(p.P).toFixed(1)}</strong> | S1: <strong>₹${Number(p.S1).toFixed(1)}</strong> | S2: <strong>₹${Number(p.S2).toFixed(1)}</strong> | R1: <strong>₹${Number(p.R1).toFixed(1)}</strong> | R2: <strong>₹${Number(p.R2).toFixed(1)}</strong>`;
      }
      if (els.pivPill1h && d.symbol_pivots.p1h) {
        const p = d.symbol_pivots.p1h;
        els.pivPill1h.innerHTML = `P: <strong>₹${Number(p.P).toFixed(1)}</strong> | S1: <strong>₹${Number(p.S1).toFixed(1)}</strong> | S2: <strong>₹${Number(p.S2).toFixed(1)}</strong> | R1: <strong>₹${Number(p.R1).toFixed(1)}</strong> | R2: <strong>₹${Number(p.R2).toFixed(1)}</strong>`;
      }
    }

    // Update Reference Levels: Daily & Weekly Pivot Fib (Reference Only)
    const ref = d.reference_levels || {};
    const dFib = ref.daily_fib || (d.symbol_pivots && d.symbol_pivots.daily);
    if (els.dailyFibText && dFib && dFib.P) {
      els.dailyFibText.innerHTML = `PP: <strong>₹${Number(dFib.P||0).toFixed(1)}</strong> | S1: <strong>₹${Number(dFib.S1||0).toFixed(1)}</strong> | S2: <strong>₹${Number(dFib.S2||0).toFixed(1)}</strong> | S3: <strong>₹${Number(dFib.S3||0).toFixed(1)}</strong> | R1: <strong>₹${Number(dFib.R1||0).toFixed(1)}</strong> | R2: <strong>₹${Number(dFib.R2||0).toFixed(1)}</strong> | R3: <strong>₹${Number(dFib.R3||0).toFixed(1)}</strong>`;
    }
    const wFib = ref.weekly_fib || (d.symbol_pivots && d.symbol_pivots.weekly);
    if (els.weeklyFibText && wFib && wFib.P) {
      els.weeklyFibText.innerHTML = `PP: <strong>₹${Number(wFib.P||0).toFixed(1)}</strong> | S1: <strong>₹${Number(wFib.S1||0).toFixed(1)}</strong> | S2: <strong>₹${Number(wFib.S2||0).toFixed(1)}</strong> | S3: <strong>₹${Number(wFib.S3||0).toFixed(1)}</strong> | R1: <strong>₹${Number(wFib.R1||0).toFixed(1)}</strong> | R2: <strong>₹${Number(wFib.R2||0).toFixed(1)}</strong> | R3: <strong>₹${Number(wFib.R3||0).toFixed(1)}</strong>`;
    }

    // Update Active Stored Next-Day Plan Ribbon
    if (d.active_plan) {
      const ap = d.active_plan;
      if (els.activePlanDetails) {
        if (ap.target_trading_date) {
          els.activePlanDetails.textContent = `Target Trading Session: ${ap.target_trading_date} | Stored Plan Strikes: ${ap.setups_count || 0} setups active in Live Scanner | Generated: ${ap.generated_at || 'Auto-EOD'}`;
        } else {
          els.activePlanDetails.textContent = `No next-day plan saved yet. Run Next-Day Plan after market close.`;
        }
      }
      if (els.activePlanTag) {
        if (ap.target_trading_date) {
          els.activePlanTag.textContent = `ACTIVE SESSION: ${ap.target_trading_date}`;
          els.activePlanTag.className = 'plan-session-tag active';
        } else {
          els.activePlanTag.textContent = 'AWAITING PLAN';
          els.activePlanTag.className = 'plan-session-tag';
        }
      }
    }
  }

  // ── Update Performance Strip (4 Metric Strip) ──
  function updateScorecard(sc) {
    if (!sc) return;
    const net = Number(sc.net_pts || 0);
    if (els.scNetPts) {
      els.scNetPts.textContent = `${net >= 0 ? '+' : ''}${net.toFixed(2)} pts`;
      els.scNetPts.className = `perf-value ${net >= 0 ? 'text-green' : 'text-red'}`;
    }
    if (els.scTradesCount) {
      els.scTradesCount.textContent = `${sc.trades || 0} Closed Trades`;
    }
    if (els.scWinRate) {
      els.scWinRate.textContent = `${Number(sc.win_rate || 0).toFixed(1)}%`;
    }
    if (els.scWinLoss) {
      els.scWinLoss.textContent = `${sc.wins || 0} W | ${sc.losses || 0} L`;
    }
    const live = Number(sc.live_pts || 0);
    if (els.scLivePts) {
      els.scLivePts.textContent = `${live >= 0 ? '+' : ''}${live.toFixed(2)} pts`;
      els.scLivePts.className = `perf-value ${live >= 0 ? 'text-green' : 'text-red'}`;
    }
    const totalNet = Number(sc.net_incl_live || 0);
    if (els.scTotalNet) {
      els.scTotalNet.textContent = `${totalNet >= 0 ? '+' : ''}${totalNet.toFixed(2)} pts`;
      els.scTotalNet.className = `perf-value ${totalNet >= 0 ? 'text-green' : 'text-red'}`;
    }
  }

  // ── Update Option Chain Table (14 Clean Columns) ──
  function updateOptionChainTable(items, maxOi) {
    if (maxOi && els.maxOiText) els.maxOiText.textContent = `OI Activity: ${maxOi}`;
    const list = (items && items.length) ? items : state.lastWatchItems;
    if (!list || list.length === 0) return;

    // Filter by Expiry
    let filtered = list;
    if (state.expiryFilter === 'CURRENT' && state.currentExpiry) {
      filtered = filtered.filter(w => !w.expiry || w.expiry === state.currentExpiry);
    } else if (state.expiryFilter === 'NEXT' && state.nextExpiry) {
      filtered = filtered.filter(w => w.expiry === state.nextExpiry);
    }

    // Filter by Timeframe & Direction
    if (state.strikeFilter === 'FOCUS') {
      filtered = filtered.filter(w => Boolean(w.is_15m || w.is_1h));
    } else if (state.strikeFilter === '15M') {
      filtered = filtered.filter(w => Boolean(w.is_15m));
    } else if (state.strikeFilter === '1H') {
      filtered = filtered.filter(w => Boolean(w.is_1h));
    } else if (state.strikeFilter === 'BUY_ONLY') {
      filtered = filtered.filter(w => w.side === 'BUY');
    } else if (state.strikeFilter === 'SELL_ONLY') {
      filtered = filtered.filter(w => w.side === 'SELL');
    }

    if (filtered.length === 0) {
      const lbl = state.strikeFilter === '15M' ? '15M Intraday' : (state.strikeFilter === '1H' ? '1H Structure' : 'Filter');
      els.tbodyOptionChain.innerHTML = `<tr><td colspan="14" class="empty-state">No strikes currently matched for <strong>${state.symbol}</strong> in <strong>${lbl}</strong>. Click Start Scanner or select ALL strikes to inspect full ladder.</td></tr>`;
      return;
    }

    const htmlRows = filtered.map(w => {
      const isBuy = (w.side === 'BUY');
      const is15m = Boolean(w.is_15m);
      const is1h = Boolean(w.is_1h);
      
      const tfBadge = (is15m && is1h)
        ? '<span class="badge badge-both">15M+1H</span>'
        : (is15m ? '<span class="badge badge-15m">⭐ 15M</span>'
        : (is1h ? '<span class="badge badge-1h">⏳ 1H</span>' : '<span class="badge">LADDER</span>'));

      const cmpVal = Number(w.ltp || 0).toFixed(2);
      const entryNum = Number(w.planned_ep || w.entry_price || w.ltp || 0);
      const entryVal = entryNum.toFixed(2);

      // Strict Target 1 != Entry enforcement:
      let target1Num = Number(w.t1 || 0);
      if (target1Num === entryNum || target1Num <= 0) {
        const optP = Number(w.p || 0);
        const optR1 = Number(w.r1 || 0);
        const optS1 = Number(w.s1 || 0);
        if (isBuy) {
          target1Num = (optP > entryNum) ? optP : ((optR1 > entryNum) ? optR1 : entryNum * 1.25);
        } else {
          target1Num = (optP < entryNum && optP > 0) ? optP : ((optS1 < entryNum && optS1 > 0) ? optS1 : entryNum * 0.80);
        }
      }
      const target1Val = target1Num.toFixed(2);

      // Target 2
      let target2Num = Number(w.t2 || 0);
      if (target2Num <= 0 || (isBuy && target2Num <= target1Num) || (!isBuy && target2Num >= target1Num)) {
        target2Num = isBuy ? (target1Num * 1.2) : (target1Num * 0.8);
      }
      const target2Display = target2Num > 0 ? `₹${target2Num.toFixed(2)}` : '-';

      // Target 3
      let target3Num = Number(w.t3 || 0);
      if (target3Num <= 0 || (isBuy && target3Num <= target2Num) || (!isBuy && target3Num >= target2Num)) {
        target3Num = isBuy ? (target2Num * 1.2) : (target2Num * 0.8);
      }
      const target3Display = target3Num > 0 ? `₹${target3Num.toFixed(2)}` : '-';

      // Target 4
      let target4Num = Number(w.t4 || 0);
      if (target4Num <= 0 || (isBuy && target4Num <= target3Num) || (!isBuy && target4Num >= target3Num)) {
        target4Num = isBuy ? (target3Num * 1.2) : (target3Num * 0.8);
      }
      const target4Display = target4Num > 0 ? `₹${target4Num.toFixed(2)}` : '-';

      // Stop Loss (SL is BLANK until entry confirmed; updated dynamically in live market)
      const slNum = Number(w.sl || (isBuy ? w.ll : w.hh) || 0);
      const isConfirmed = Boolean(
        w.is_entry_confirmed ||
        w.entry_status === 'TRIGGERED' ||
        (w.trade_status && !['WAITING FOR ENTRY', 'WAITING', 'MONITORING', 'Waiting'].includes(w.trade_status))
      );
      const slDisplay = (isConfirmed && slNum > 0)
        ? `<span class="text-gold font-bold font-mono">🛡️ ₹${slNum.toFixed(2)}</span>`
        : `<span class="text-muted font-mono" title="Calculated dynamically in live market at entry confirmation">—</span>`;

      // R:R
      let rrVal = w.rr || '1:2.0';
      if (slNum > 0 && entryNum > 0 && Math.abs(entryNum - slNum) > 0.1) {
        const risk = Math.abs(entryNum - slNum);
        const reward = Math.abs(target1Num - entryNum);
        rrVal = `1:${(reward / risk).toFixed(1)}`;
      }

      const rowClass = isBuy ? 'buy-row' : 'sell-row';
      const m5Status = w.m5_state || w.status || 'Watching CMP';

      // Trade execution status badge (including Target 4 & Target 3 Hit)
      let badgeClass = 'badge-waiting';
      let stText = w.trade_status || 'WAITING FOR ENTRY';
      const stUpper = stText.toUpperCase();
      if (stUpper.includes('TARGET 4') || stUpper.includes('T4')) {
        badgeClass = 'badge-target-hit';
        stText = '🏆 TARGET 4 HIT';
      } else if (stUpper.includes('TARGET 3') || stUpper.includes('T3')) {
        badgeClass = 'badge-target-hit';
        stText = '🎯 TARGET 3 HIT';
      } else if (stUpper.includes('TARGET 2') || stUpper.includes('T2')) {
        badgeClass = 'badge-target-hit';
        stText = '🎯 TARGET 2 HIT';
      } else if (stUpper.includes('TARGET') || stUpper.includes('T1')) {
        badgeClass = 'badge-target-hit';
        stText = '🎯 TARGET 1 HIT';
      } else if (stUpper.includes('STOP') || stUpper.includes('SL')) {
        badgeClass = 'badge-sl-hit';
        stText = '🛑 STOP LOSS HIT';
      } else if (stUpper.includes('TRIGGERED')) {
        badgeClass = 'badge-triggered';
        stText = '⚡ ENTRY TRIGGERED';
      } else {
        badgeClass = 'badge-waiting';
        stText = '⏳ WAITING FOR ENTRY';
      }

      return `
        <tr class="${rowClass}">
          <td>${tfBadge}</td>
          <td><strong>${w.expiry || '-'}</strong></td>
          <td><strong class="font-mono">${w.strike}</strong></td>
          <td><strong class="${w.type === 'CE' ? 'text-green' : 'text-gold'}">${w.type}</strong></td>
          <td><strong class="${isBuy ? 'text-green' : 'text-red'}">${w.side}</strong></td>
          <td><strong class="text-cyan font-mono" style="font-size:13px">₹${cmpVal}</strong></td>
          <td><strong class="font-mono">₹${entryVal}</strong></td>
          <td><strong class="text-green font-mono">₹${target1Val}</strong></td>
          <td><strong class="text-green font-mono">${target2Display}</strong></td>
          <td><strong class="text-green font-mono">${target3Display}</strong></td>
          <td><strong class="text-green font-mono">${target4Display}</strong></td>
          <td>${slDisplay}</td>
          <td><span class="badge badge-both">${rrVal}</span></td>
          <td style="font-size:11px">${m5Status}</td>
          <td><span class="${badgeClass}">${stText}</span></td>
        </tr>
      `;
    }).join('');

    els.tbodyOptionChain.innerHTML = htmlRows;
  }

  // ── Strike Filter Buttons ──
  document.querySelectorAll('.filter-cluster:not(.expiry-cluster) .filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-cluster:not(.expiry-cluster) .filter-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.strikeFilter = btn.getAttribute('data-filter');
      updateOptionChainTable(state.lastWatchItems, state.lastMaxOi);
    });
  });

  // ── Expiry View Buttons (CURRENT / NEXT / ALL) ──
  document.querySelectorAll('.expiry-cluster .expiry-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.expiry-cluster .expiry-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const expChoice = btn.getAttribute('data-expiry');
      state.expiryFilter = expChoice;
      loadDashboard(state.symbol, expChoice);
      showToast(`Switched to ${expChoice} EXPIRY view`, 'blue');
    });
  });

  // ── Forward Test Filters ──
  document.querySelectorAll('[data-ft-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-ft-filter]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.ftFilter = btn.getAttribute('data-ft-filter');
      updateForwardTestTables(state.activeTrades, state.closedTrades);
    });
  });

  // ── Render Forward Test & Audit Log ──
  function updateForwardTestTables(active, closed) {
    state.activeTrades = active;
    state.closedTrades = closed;

    if (els.tabActiveCount) els.tabActiveCount.textContent = active.length;
    if (els.ftActiveBadge) els.ftActiveBadge.textContent = active.length;
    if (els.ftClosedBadge) els.ftClosedBadge.textContent = closed.length;
    if (els.scActiveTradesCount) els.scActiveTradesCount.textContent = `${active.length} Active Positions`;

    function matchTf(timeframeStr, filterVal) {
      if (!filterVal || filterVal === 'ALL') return true;
      const tf = String(timeframeStr || '').toUpperCase();
      const f = String(filterVal).toUpperCase();
      return tf.includes(f);
    }

    // Active Positions Table (15 columns)
    const filtActive = active.filter(t => matchTf(t.Timeframe, state.ftFilter));
    if (filtActive.length === 0) {
      els.tbodyFtActive.innerHTML = '<tr><td colspan="15" class="empty-state">No open positions currently active. Tracking live market data — entries execute at CMP tick upon signal confirmation.</td></tr>';
    } else {
      els.tbodyFtActive.innerHTML = filtActive.map(t => {
        const pnl = Number(t.PnL_Pts || t.Live_PnL || 0);
        const pnlClass = pnl >= 0 ? 'text-green' : 'text-red';
        const pnlSign = pnl >= 0 ? '+' : '';
        return `
          <tr class="${t.Action === 'BUY' ? 'buy-row' : 'sell-row'}">
            <td>${t.Date || ''}</td>
            <td>${t.Entry_Time || ''}</td>
            <td><span class="badge ${t.Timeframe === '1H' ? 'badge-1h' : 'badge-15m'}">${t.Timeframe || '15M'}</span></td>
            <td><strong>${t.Symbol || ''}</strong></td>
            <td><strong class="${t.Type === 'CE' ? 'text-green' : 'text-gold'}">${t.Type || ''}</strong></td>
            <td><strong class="font-mono">${t.Strike || ''}</strong></td>
            <td><strong class="${t.Action === 'BUY' ? 'text-green' : 'text-red'}">${t.Action || ''}</strong></td>
            <td>${Number(t.Entry_Price || 0).toFixed(2)}</td>
            <td><strong class="text-cyan font-mono">${Number(t.Current_Price || t.Entry_Price || 0).toFixed(2)}</strong></td>
            <td class="${pnlClass}"><strong>${pnlSign}${pnl.toFixed(2)} pts</strong></td>
            <td>${Number(t.SL || 0).toFixed(2)}</td>
            <td>${Number(t.Target_1 || 0).toFixed(2)}</td>
            <td>${Number(t.Target_2 || 0).toFixed(2)}</td>
            <td><span class="badge badge-both">ACTIVE</span></td>
            <td>
              <button class="btn btn-secondary" style="padding:2px 8px;font-size:10px" onclick="window.closeTrade('${t.Type}','${t.Strike}','${t.Expiry||''}','${t.Symbol||''}')">
                Exit @ CMP
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    // Closed Trades Audit Log (13 columns)
    const filtClosed = closed.filter(t => matchTf(t.Timeframe, state.ftFilter));
    if (filtClosed.length === 0) {
      els.tbodyFtClosed.innerHTML = '<tr><td colspan="13" class="empty-state">No closed trades in today\'s live market session yet. Trades record here upon target hit, SL hit, or 3:00 PM square-off.</td></tr>';
    } else {
      els.tbodyFtClosed.innerHTML = filtClosed.map(r => {
        const pnl = Number(r.PnL_Pts || 0);
        const cum = Number(r.Cum_Pts || 0);
        const pnlClass = pnl >= 0 ? 'text-green' : 'text-red';
        const cumClass = cum >= 0 ? 'text-green' : 'text-red';
        const pnlSign = pnl >= 0 ? '+' : '';
        const cumSign = cum >= 0 ? '+' : '';
        return `
          <tr>
            <td>${r.Date || ''}</td>
            <td>${r.Entry_Time || ''}</td>
            <td><span class="badge ${r.Timeframe === '1H' ? 'badge-1h' : 'badge-15m'}">${r.Timeframe || '15M'}</span></td>
            <td><strong>${r.Symbol || ''}</strong></td>
            <td><strong class="${r.Type === 'CE' ? 'text-green' : 'text-gold'}">${r.Type || ''}</strong></td>
            <td><strong class="font-mono">${r.Strike || ''}</strong></td>
            <td><strong class="${r.Action === 'BUY' ? 'text-green' : 'text-red'}">${r.Action || ''}</strong></td>
            <td>${Number(r.Entry_Price || 0).toFixed(2)}</td>
            <td>${Number(r.Exit_Price || 0).toFixed(2)}</td>
            <td class="${pnlClass}"><strong>${pnlSign}${pnl.toFixed(2)} pts</strong></td>
            <td class="${cumClass}"><strong>${cumSign}${cum.toFixed(2)} pts</strong></td>
            <td style="font-size:11px">${r.Exit_Reason || ''}</td>
            <td>${pnl >= 0 ? '✅ TARGET HIT' : '🛑 SL HIT'}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // ── Manual 3:00 PM Square-off ──
  if (els.btnManualSquareoff) {
    els.btnManualSquareoff.addEventListener('click', async () => {
      if (!confirm('Close ALL active intraday trades at current CMP?')) return;
      try {
        const res = await fetch('/api/trades/square-off', { method: 'POST' });
        const data = await res.json();
        showToast(`⏰ Square-off complete: ${data.closed_count} trade(s) closed @ CMP | Net: ${data.net_pts >= 0 ? '+' : ''}${data.net_pts} pts`, 'gold');
      } catch (e) {
        showToast(`Square-off error: ${e}`, 'red');
      }
    });
  }

  window.closeTrade = async function (type, strike, expiry, symbol) {
    try {
      const res = await fetch('/api/trades/manual-close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, strike: Number(strike), expiry, symbol })
      });
      const data = await res.json();
      showToast(`Trade closed: ${type} ${strike} @ CMP`, 'green');
    } catch (e) {
      showToast(`Close error: ${e}`, 'red');
    }
  };

  // ── Next-Day 15M + 1H Intraday Strategy Deck ──
  async function loadPlannerDeck(sym = null) {
    const targetSym = (sym || state.symbol || 'NIFTY').trim().toUpperCase();
    if (!els.plannerDeckContainer) return;
    els.plannerDeckContainer.innerHTML = `
      <div class="empty-state">
        ⏳ Calculating Next-Day 15M & 1H Intraday Strategy Deck for <strong>${targetSym}</strong>...
      </div>
    `;

    try {
      const res = await fetch(`/api/planner?symbol=${encodeURIComponent(targetSym)}`);
      const data = await res.json();
      if (!data.success || !data.levels_15m) {
        els.plannerDeckContainer.innerHTML = `
          <div class="empty-state">
            No next-day plan generated yet. Click <strong>⚡ RE-CALCULATE 15M/1H PLAN</strong> to compute.
          </div>
        `;
        return;
      }
      renderPlannerDeck(data);
    } catch (e) {
      els.plannerDeckContainer.innerHTML = `<div class="empty-state text-red">Error loading planner deck: ${e}</div>`;
    }
  }

  function renderPlannerDeck(data) {
    const sym = data.symbol || 'NIFTY';
    const genAt = data.generated_at || '-';
    const targetDate = data.target_trading_date || 'Next Trading Day';
    const p15 = data.levels_15m || {};
    const p1h = data.levels_1h || {};
    const sup = data.support || {};
    const res = data.resistance || {};
    const setups = data.setups || [];
    const refLevels = data.reference_levels || {};
    const dFib = refLevels.daily_fib || {};
    const wFib = refLevels.weekly_fib || {};

    const setupRowsHtml = (setups && setups.length > 0)
      ? setups.map(s => {
          const isBuy = (s.side === 'BUY');
          const rowClass = isBuy ? 'buy-row' : 'sell-row';
          const cmpVal = Number(s.live_cmp || s.ref_option_price || 0).toFixed(2);
          const ep = Number(s.entry_price || s.entry || 0).toFixed(2);
          const t1 = Number(s.target_1 || s.target || s.t1 || 0).toFixed(2);
          const t2 = (s.target_2 || s.t2) ? `₹${Number(s.target_2 || s.t2).toFixed(2)}` : '-';
          const t3 = (s.target_3 || s.t3) ? `₹${Number(s.target_3 || s.t3).toFixed(2)}` : '-';
          const t4 = (s.target_4 || s.t4) ? `₹${Number(s.target_4 || s.t4).toFixed(2)}` : '-';
          const isTgtMismatch = (t1 !== ep);

          return `
            <tr class="${rowClass}">
              <td><span class="badge ${s.timeframe && s.timeframe.includes('1H') ? 'badge-1h' : 'badge-15m'}">${s.timeframe || '15M'}</span></td>
              <td><strong>${s.expiry || '-'}</strong></td>
              <td><strong class="font-mono">${s.strike}</strong></td>
              <td><strong class="${s.type === 'CE' ? 'text-green' : 'text-gold'}">${s.type}</strong></td>
              <td style="font-size:11px" class="font-mono">${s.option_symbol || '-'}</td>
              <td><strong class="text-cyan font-mono" style="font-size:12px">₹${cmpVal}</strong></td>
              <td><strong class="${isBuy ? 'text-green' : 'text-red'}">${s.setup || s.side}</strong></td>
              <td><strong class="font-mono">₹${ep}</strong></td>
              <td><strong class="font-mono text-green ${isTgtMismatch ? '' : 'text-red'}">₹${t1}</strong></td>
              <td><strong class="font-mono text-green">${t2}</strong></td>
              <td><strong class="font-mono text-green">${t3}</strong></td>
              <td><strong class="font-mono text-green">${t4}</strong></td>
              <td class="text-muted font-mono" title="Calculated dynamically in live market at entry confirmation">—</td>
              <td><span class="badge badge-both">${s.rr || '1:2.0'}</span></td>
              <td><span class="badge badge-waiting">${s.strategy_status || 'WAITING FOR ENTRY'}</span></td>
            </tr>
          `;
        }).join('')
      : '<tr><td colspan="15" class="empty-state">No specific setup rows generated. Refer to strategy rules above.</td></tr>';

    els.plannerDeckContainer.innerHTML = `
      <!-- Method & Target Trading Session Ribbon -->
      <div style="background:var(--bg-card);border:1px solid var(--border-color);border-radius:var(--radius-sm);padding:10px 14px;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span style="color:var(--accent-cyan);font-weight:800;font-size:12px">TARGET TRADING SESSION:</span>
          <span style="background:rgba(56,189,248,0.18);color:var(--accent-cyan);padding:2px 8px;border-radius:4px;font-weight:800;font-family:monospace;font-size:12px">${targetDate}</span>
          <span style="background:rgba(16,185,129,0.15);color:var(--accent-green);padding:2px 8px;border-radius:4px;font-size:10.5px;font-weight:700">💾 PERSISTED IN DATABASE (next_day_plans.json)</span>
          <span style="color:var(--text-muted);font-size:11px">Setups: <strong class="text-cyan">${setups.length}</strong></span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <button class="btn btn-success font-bold" onclick="window.applyPlanToLiveScanner()" style="padding:4px 12px;font-size:11px">
            📥 LOAD PLAN INTO LIVE SCANNER
          </button>
          <span style="font-size:10.5px;color:var(--text-dim)">Computed: ${genAt}</span>
        </div>
      </div>

      <!-- Professional Compact MTF & Reference Strip (Takes only 45px instead of giant cards) -->
      <div style="background:var(--bg-card);border:1px solid var(--border-prominent);border-radius:var(--radius-sm);padding:8px 12px;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:11px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="badge badge-15m" style="font-size:9.5px">⭐ 15M INTRADAY</span>
          <span class="font-mono" style="color:var(--text-main)">P: <strong>₹${Number(p15.P||0).toFixed(1)}</strong> | S1: <strong class="text-green">₹${Number(p15.S1||0).toFixed(1)}</strong> | S2: <strong class="text-green">₹${Number(p15.S2||0).toFixed(1)}</strong> | R1: <strong class="text-red">₹${Number(p15.R1||0).toFixed(1)}</strong> | R2: <strong class="text-red">₹${Number(p15.R2||0).toFixed(1)}</strong></span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="badge badge-1h" style="font-size:9.5px">⏳ 1H STRUCTURE</span>
          <span class="font-mono" style="color:var(--text-main)">P: <strong>₹${Number(p1h.P||0).toFixed(1)}</strong> | S1: <strong class="text-green">₹${Number(p1h.S1||0).toFixed(1)}</strong> | S2: <strong class="text-green">₹${Number(p1h.S2||0).toFixed(1)}</strong> | R1: <strong class="text-red">₹${Number(p1h.R1||0).toFixed(1)}</strong> | R2: <strong class="text-red">₹${Number(p1h.R2||0).toFixed(1)}</strong></span>
        </div>
        ${dFib.P ? `
        <div style="display:flex;align-items:center;gap:6px;font-size:10.5px;color:var(--text-muted);border-left:1px solid var(--border-prominent);padding-left:10px">
          <span style="color:var(--accent-gold);font-weight:700">Ref:</span>
          <span class="font-mono">Daily PP: ₹${Number(dFib.P||0).toFixed(1)} | Wkly PP: ₹${Number(wFib.P||0).toFixed(1)}</span>
        </div>
        ` : ''}
      </div>

      <!-- Setups Table (Immediate Primary Focus) -->
      <div class="planner-setups-section">
        <div class="panel-header" style="margin-bottom:8px;display:flex;align-items:center;justify-content:space-between">
          <div>
            <h3 style="font-size:13px;margin:0">NEXT-DAY CALCULATED STRIKE SETUPS</h3>
            <span class="panel-sub" style="font-size:11px">Planned setups for ${targetDate} — SL calculated dynamically in live market @ entry</span>
          </div>
          <span class="badge badge-both" style="font-size:10px">Target ≠ Entry (Next Pivot: P / R1)</span>
        </div>
        <div class="terminal-table-container">
          <table class="terminal-table">
            <thead>
              <tr>
                <th>TIMEFRAME</th>
                <th>EXPIRY</th>
                <th>STRIKE</th>
                <th>CE / PE</th>
                <th>OPTION CONTRACT</th>
                <th>LIVE CMP (₹)</th>
                <th>SETUP / ACTION</th>
                <th>ENTRY LEVEL (₹)</th>
                <th>TARGET 1 (₹)</th>
                <th>TARGET 2 (₹)</th>
                <th>TARGET 3 (₹)</th>
                <th>TARGET 4 (₹)</th>
                <th>STOP LOSS (₹)</th>
                <th>R:R</th>
                <th>STRATEGY STATUS</th>
              </tr>
            </thead>
            <tbody>
              ${setupRowsHtml}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  // ── Global Apply Plan into Live Scanner ──
  window.applyPlanToLiveScanner = async function () {
    try {
      showToast(`⏳ Loading ${state.symbol} plan into Live Strategy Scanner...`, 'blue');
      const res = await fetch('/api/planner/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: state.symbol })
      });
      const data = await res.json();
      if (data.success) {
        showToast(`✅ ${data.message || 'Next-Day Plan loaded into Live Scanner!'}`, 'green');
        playAlertSound();

        // Switch Tab to Live Scanner
        document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
        const scTab = document.querySelector('[data-tab="tab-scanner"]');
        const scPane = document.getElementById('tab-scanner');
        if (scTab) scTab.classList.add('active');
        if (scPane) scPane.classList.add('active');
        state.activeTab = 'tab-scanner';

        // Reload fresh snapshot with stored plan
        loadDashboard(state.symbol, state.expiryFilter);
      } else {
        showToast(`Apply error: ${data.error || 'Failed'}`, 'red');
      }
    } catch (e) {
      showToast(`Apply error: ${e}`, 'red');
    }
  };

  if (els.btnApplyPlanToScanner) {
    els.btnApplyPlanToScanner.addEventListener('click', window.applyPlanToLiveScanner);
  }

  // ── Planner Re-calculation Trigger ──
  if (els.btnTriggerPlanner) {
    els.btnTriggerPlanner.addEventListener('click', async () => {
      try {
        showToast('⏳ Re-calculating Next-Day 15M + 1H Plan...', 'blue');
        const res = await fetch('/api/planner/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: state.symbol })
        });
        const data = await res.json();
        if (data.success) {
          showToast(`✅ Next-Day 15M/1H Plan re-calculated for ${state.symbol}!`, 'green');
          renderPlannerDeck(data);
        } else {
          showToast(`Planner error: ${data.error || 'Failed'}`, 'red');
        }
      } catch (e) {
        showToast(`Planner error: ${e}`, 'red');
      }
    });
  }

  // ── Expiries & Feed Start/Stop ──
  async function loadExpiries(sym) {
    try {
      const res = await fetch(`/api/expiries?symbol=${encodeURIComponent(sym)}`);
      const data = await res.json();
      if (els.selExpiry) {
        els.selExpiry.innerHTML = '';
        (data.expiries || []).forEach(exp => {
          const opt = document.createElement('option');
          opt.value = exp;
          opt.textContent = exp;
          els.selExpiry.appendChild(opt);
        });
        if (state.expiry && (data.expiries || []).includes(state.expiry)) {
          els.selExpiry.value = state.expiry;
        }
      }
    } catch (e) {
      console.warn('Expiries load failed:', e);
    }
  }

  if (els.selExpiry) {
    els.selExpiry.addEventListener('change', () => {
      const exp = els.selExpiry.value;
      if (exp) {
        state.expiry = exp;
        loadDashboard(state.symbol, exp);
        showToast(`Switched contract expiry to ${exp}`, 'blue');
      }
    });
  }

  if (els.inputSymbol) {
    els.inputSymbol.addEventListener('change', () => {
      const sym = els.inputSymbol.value.trim().toUpperCase();
      state.symbol = sym;
      loadExpiries(sym);
      loadDashboard(sym);
      loadPlannerDeck(sym);
    });
  }

  document.querySelectorAll('.quick-chips .chip-btn').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.quick-chips .chip-btn').forEach(c => c.classList.remove('active'));
      b.classList.add('active');
      const s = b.getAttribute('data-sym');
      if (els.inputSymbol) els.inputSymbol.value = s;
      state.symbol = s;
      loadExpiries(s);
      loadDashboard(s);
      loadPlannerDeck(s);
      showToast(`Switched symbol to ${s}`, 'blue');
    });
  });

  if (els.btnStartFeed) {
    els.btnStartFeed.addEventListener('click', async () => {
      const sym = els.inputSymbol ? els.inputSymbol.value.trim().toUpperCase() : 'NIFTY';
      const exp = els.selExpiry ? els.selExpiry.value : '';

      try {
        els.btnStartFeed.disabled = true;
        els.btnStartFeed.textContent = '⏳ Starting...';
        const res = await fetch('/api/start-feed', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: sym, expiry: exp, exchange: 'NFO', n_strikes: 8 })
        });
        const data = await res.json();
        els.btnStartFeed.textContent = '▶ START SCANNER';
        if (data.success) {
          showToast(`🚀 Live scanner started for ${sym}`, 'green');
          if (els.btnStopFeed) els.btnStopFeed.disabled = false;
          loadDashboard(sym);
        } else {
          showToast(`Start failed: ${data.error}`, 'red');
          els.btnStartFeed.disabled = false;
        }
      } catch (e) {
        els.btnStartFeed.textContent = '▶ START SCANNER';
        showToast(`Start error: ${e}`, 'red');
        els.btnStartFeed.disabled = false;
      }
    });
  }

  if (els.btnStopFeed) {
    els.btnStopFeed.addEventListener('click', async () => {
      try {
        await fetch('/api/stop-feed', { method: 'POST' });
        showToast('⏹ Scanner feed stopped', 'gold');
        loadDashboard();
      } catch (e) {
        showToast(`Stop error: ${e}`, 'red');
      }
    });
  }

  // ── Bootstrap ──
  loadDashboard('NIFTY');
  loadExpiries('NIFTY');
  loadPlannerDeck('NIFTY');
  connectWebSocket();
})();
