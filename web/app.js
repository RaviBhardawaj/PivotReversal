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
    plannerExpiry: 'ALL',
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
    plannerData: null,
    otherSymbol: 'BANKNIFTY',
    otherExpiry: 'ALL',
    otherFilter: 'ALL',
    otherData: null,
    mappedConfig: {
      restrict_to_selected_only: false,
      default_lots: 1,
      strikes: []
    }
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

    // Other Symbols Tab
    otherInputSymbol: document.getElementById('other-input-symbol'),
    otherSelExpiry: document.getElementById('other-sel-expiry'),
    btnOtherGenerate: document.getElementById('btn-other-generate'),
    btnOtherStartFeed: document.getElementById('btn-other-start-feed'),
    btnOtherLoadPlan: document.getElementById('btn-other-load-plan'),
    otherPiv15m: document.getElementById('other-piv-15m'),
    otherPiv1h: document.getElementById('other-piv-1h'),
    otherSpotDisplay: document.getElementById('other-spot-display'),
    otherStatusText: document.getElementById('other-status-text'),
    tbodyOtherSymbols: document.getElementById('tbody-other-symbols'),

    // Forward Test & Audit Journal
    tbodyFtActive: document.getElementById('tbody-ft-active'),
    tbodyFtClosed: document.getElementById('tbody-ft-closed'),
    ftActiveBadge: document.getElementById('ft-active-badge'),
    ftClosedBadge: document.getElementById('ft-closed-badge'),
    btnManualSquareoff: document.getElementById('btn-manual-squareoff'),

    // Mapped Strikes & Lots Tab
    tabMappedCount: document.getElementById('tab-mapped-count'),
    toggleRestrictedStrikes: document.getElementById('toggle-restricted-strikes'),
    restrictedModeBadge: document.getElementById('restricted-mode-badge'),
    inputDefaultLots: document.getElementById('input-default-lots'),
    btnImportFocusStrikes: document.getElementById('btn-import-focus-strikes'),
    btnSaveMappedStrikes: document.getElementById('btn-save-mapped-strikes'),
    btnClearMappedStrikes: document.getElementById('btn-clear-mapped-strikes'),
    addStrikeSymbol: document.getElementById('add-strike-symbol'),
    addStrikePrice: document.getElementById('add-strike-price'),
    addStrikeType: document.getElementById('add-strike-type'),
    addStrikeLots: document.getElementById('add-strike-lots'),
    addStrikeNote: document.getElementById('add-strike-note'),
    btnAddMappedStrike: document.getElementById('btn-add-mapped-strike'),
    tbodyMappedStrikes: document.getElementById('tbody-mapped-strikes'),
    mappedStrikesSummaryText: document.getElementById('mapped-strikes-summary-text'),

    // Top Bar Broker, Funds & Kill Switch
    brokerStatusBadge: document.getElementById('broker-status-badge'),
    brokerDot: document.getElementById('broker-dot'),
    brokerNameText: document.getElementById('broker-name-text'),
    brokerFundsText: document.getElementById('broker-funds-text'),
    btnAutoToggle: document.getElementById('btn-auto-toggle'),
    btnKillSwitch: document.getElementById('btn-kill-switch'),

    // Performance & Audit Reports Tab
    tabPerfCount: document.getElementById('tab-perf-count'),
    perfTotalTrades: document.getElementById('perf-total-trades'),
    perfWinrate: document.getElementById('perf-winrate'),
    perfWinLoss: document.getElementById('perf-win-loss'),
    perfNetPnl: document.getElementById('perf-net-pnl'),
    perfGrossPnl: document.getElementById('perf-gross-pnl'),
    perfCharges: document.getElementById('perf-charges'),
    perfAvgWin: document.getElementById('perf-avg-win'),
    perfAvgLoss: document.getElementById('perf-avg-loss'),
    perfClosedBadge: document.getElementById('perf-closed-badge'),
    perfTradesFilter: document.getElementById('perf-trades-filter'),
    tbodyPerfClosed: document.getElementById('tbody-perf-closed'),
    perfBrokerOrdersBadge: document.getElementById('perf-broker-orders-badge'),
    btnRefreshBrokerOrders: document.getElementById('btn-refresh-broker-orders'),
    tbodyPerfBrokerOrders: document.getElementById('tbody-perf-broker-orders'),

    // Mode A Order Execution Modal
    orderModalBackdrop: document.getElementById('order-modal-backdrop'),
    modalCloseBtn: document.getElementById('modal-close-btn'),
    btnCancelModalOrder: document.getElementById('btn-cancel-modal-order'),
    btnConfirmModalOrder: document.getElementById('btn-confirm-modal-order'),
    modalOrderBroker: document.getElementById('modal-order-broker'),
    modalOrderInstrument: document.getElementById('modal-order-instrument'),
    modalOrderAction: document.getElementById('modal-order-action'),
    modalOrderType: document.getElementById('modal-order-type'),
    modalOrderQty: document.getElementById('modal-order-qty'),
    modalOrderPrice: document.getElementById('modal-order-price'),
    modalOrderSl: document.getElementById('modal-order-sl'),
    modalOrderTarget: document.getElementById('modal-order-target'),
    modalValidationBox: document.getElementById('modal-validation-box'),

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
      if (tabId === 'tab-other-symbols' && !state.otherData) calculateOtherSymbols();
      if (tabId === 'tab-performance') loadPerformanceTab();
      if (tabId === 'tab-mapped-strikes') loadMappedStrikesData();
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
      if (data.broker_info) {
        updateBrokerStatus(data.broker_info);
      }
      renderPerformanceTab();
      if (data.mapped_strikes_config) {
        state.mappedConfig = data.mapped_strikes_config;
        updateMappedStrikesSummary(state.mappedConfig);
        if (state.activeTab === 'tab-mapped-strikes') {
          renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
        }
      }
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
      els.execModeBadge.textContent = d.exec_mode === 'LIVE' ? '🔴 LIVE' : '⏸ AUTO-TRADE OFF';
      els.execModeBadge.className = `mode-tag ${d.exec_mode === 'LIVE' ? 'live' : ''}`;
    }
    if (d.feed_running !== undefined && els.btnStartFeed && els.btnStopFeed) {
      state.feedRunning = Boolean(d.feed_running);
      els.btnStartFeed.disabled = Boolean(d.feed_running);
      els.btnStartFeed.textContent = d.feed_running ? '▶ SCANNER RUNNING' : '▶ START SCANNER';
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
    const allDistinctExps = Array.from(new Set(list.map(w => w.expiry).filter(Boolean)));
    if (!state.currentExpiry && allDistinctExps.length > 0) state.currentExpiry = allDistinctExps[0];
    if (!state.nextExpiry && allDistinctExps.length > 1) state.nextExpiry = allDistinctExps[1];

    if (state.expiryFilter === 'CURRENT') {
      const cExp = state.currentExpiry || (allDistinctExps.length > 0 ? allDistinctExps[0] : '');
      if (cExp) {
        filtered = filtered.filter(w => !w.expiry || String(w.expiry).toUpperCase() === String(cExp).toUpperCase());
      }
    } else if (state.expiryFilter === 'NEXT') {
      let nExp = state.nextExpiry;
      if (!nExp && allDistinctExps.length > 1) nExp = allDistinctExps[1];
      if (nExp) {
        filtered = filtered.filter(w => String(w.expiry).toUpperCase() === String(nExp).toUpperCase());
      }
    }

    // Filter by Timeframe & Direction
    if (state.strikeFilter === 'FOCUS') {
      filtered = filtered.filter(w => Boolean(w.is_15m || w.is_1h));
    } else if (state.strikeFilter === '15M') {
      filtered = filtered.filter(w => Boolean(w.is_15m) && !w.is_1h);
    } else if (state.strikeFilter === '1H') {
      filtered = filtered.filter(w => Boolean(w.is_1h) && !w.is_15m);
    } else if (state.strikeFilter === 'BOTH') {
      filtered = filtered.filter(w => Boolean(w.is_15m) && Boolean(w.is_1h));
    } else if (state.strikeFilter === 'BUY_ONLY') {
      filtered = filtered.filter(w => w.side === 'BUY');
    } else if (state.strikeFilter === 'SELL_ONLY') {
      filtered = filtered.filter(w => w.side === 'SELL');
    }

    if (filtered.length === 0) {
      const lbl = state.strikeFilter === '15M' ? '15M only' : (state.strikeFilter === '1H' ? '1H only' : (state.strikeFilter === 'BOTH' ? '15M + 1H' : 'Filter'));
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
          <td style="text-align:center;">
            <button class="btn-order-fast font-bold" data-sym="${state.symbol || 'NIFTY'}" data-strike="${w.strike}" data-type="${w.type}" data-expiry="${w.expiry || ''}" data-side="${w.side || 'BUY'}" data-cmp="${cmpVal}" data-entry="${entryVal}" data-sl="${slNum}" data-t1="${target1Val}" data-bid="${w.bid || 0}" data-ask="${w.ask || 0}">
              ⚡ EXEC
            </button>
          </td>
        </tr>
      `;
    }).join('');

    els.tbodyOptionChain.innerHTML = htmlRows;

    els.tbodyOptionChain.querySelectorAll('.btn-order-fast').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const d = btn.dataset;
        prepareAndOpenOrderModal({
          symbol: d.sym || state.symbol || 'NIFTY',
          strike: parseFloat(d.strike) || 0,
          opt_type: d.type || 'CE',
          expiry: d.expiry || '',
          action: d.side || 'BUY',
          limit_price: parseFloat(d.cmp) || parseFloat(d.entry) || 0,
          bid: parseFloat(d.bid) || 0,
          ask: parseFloat(d.ask) || 0,
          sl: parseFloat(d.sl) || 0,
          t1: parseFloat(d.t1) || 0
        });
      });
    });
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
      // each trade is in exactly one box: 15M only, 1H only, or both ("15M / 1H")
      const tf = String(timeframeStr || '').toUpperCase();
      const f = String(filterVal).toUpperCase();
      const has15 = tf.includes('15M'), has1h = tf.includes('1H');
      if (f === 'BOTH') return has15 && has1h;
      if (f === '15M') return has15 && !has1h;
      if (f === '1H') return has1h && !has15;
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
    const ref = data.reference_levels || {};
    const dFib = ref.daily_fib || data.daily_fib || (data.symbol_pivots && data.symbol_pivots.daily) || {};
    const wFib = ref.weekly_fib || data.weekly_fib || (data.symbol_pivots && data.symbol_pivots.weekly) || {};
    state.plannerData = data;
    const setups = data.setups || [];
    let displaySetups = setups;
    const allPlanExps = Array.from(new Set(setups.map(s => s.expiry).filter(Boolean)));
    const cExp = data.current_expiry || (allPlanExps[0] || '');
    const nExp = data.next_expiry || (allPlanExps.length > 1 ? allPlanExps[1] : '');

    if (state.plannerExpiry === 'CURRENT' && cExp) {
      displaySetups = setups.filter(s => !s.expiry || String(s.expiry).toUpperCase() === String(cExp).toUpperCase());
    } else if (state.plannerExpiry === 'NEXT' && nExp) {
      displaySetups = setups.filter(s => String(s.expiry).toUpperCase() === String(nExp).toUpperCase());
    }

    const setupRowsHtml = (displaySetups && displaySetups.length > 0)
      ? displaySetups.map(s => {
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
              <td style="text-align:center;">
                <button class="btn-order-fast font-bold" data-sym="${sym}" data-strike="${s.strike}" data-type="${s.type}" data-expiry="${s.expiry || ''}" data-side="${s.side || 'BUY'}" data-cmp="${cmpVal}" data-entry="${ep}" data-sl="0" data-t1="${t1}">
                  ⚡ EXEC
                </button>
              </td>
            </tr>
          `;
        }).join('')
      : '<tr><td colspan="16" class="empty-state">No specific setup rows generated for current filter. Refer to strategy rules above.</td></tr>';

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
        ${(dFib && dFib.P) ? `
        <div style="display:flex;align-items:center;gap:6px;font-size:10.5px;color:var(--text-muted);border-left:1px solid var(--border-prominent);padding-left:10px">
          <span style="color:var(--accent-gold);font-weight:700">Ref:</span>
          <span class="font-mono">Daily PP: ₹${Number(dFib.P||0).toFixed(1)} | Wkly PP: ₹${Number((wFib && wFib.P)||0).toFixed(1)}</span>
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
                <th>ACTION</th>
              </tr>
            </thead>
            <tbody>
              ${setupRowsHtml}
            </tbody>
          </table>
        </div>
      </div>
    `;

    els.plannerDeckContainer.querySelectorAll('.btn-order-fast').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const d = btn.dataset;
        prepareAndOpenOrderModal({
          symbol: d.sym || state.symbol || 'NIFTY',
          strike: parseFloat(d.strike) || 0,
          opt_type: d.type || 'CE',
          expiry: d.expiry || '',
          action: d.side || 'BUY',
          limit_price: parseFloat(d.cmp) || parseFloat(d.entry) || 0,
          sl: parseFloat(d.sl) || 0,
          t1: parseFloat(d.t1) || 0
        });
      });
    });
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

  // ── Next-Day Plan Expiry Filter Listeners ──
  document.querySelectorAll('.planner-exp-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.planner-exp-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.plannerExpiry = btn.getAttribute('data-pexp');
      if (state.plannerData) {
        renderPlannerDeck(state.plannerData);
        showToast(`Plan view filtered by ${state.plannerExpiry} Expiry`, 'blue');
      }
    });
  });

  // ── OTHER SYMBOLS SCANNER & REVERSAL STRATEGY LOGIC ──
  async function calculateOtherSymbols(sym = null, exp = null) {
    const targetSym = (sym || (els.otherInputSymbol ? els.otherInputSymbol.value : '') || state.otherSymbol || 'BANKNIFTY').trim().toUpperCase();
    const targetExp = (exp || (els.otherSelExpiry ? els.otherSelExpiry.value : '') || state.otherExpiry || 'ALL').trim().toUpperCase();
    state.otherSymbol = targetSym;
    state.otherExpiry = targetExp;

    if (els.otherStatusText) els.otherStatusText.textContent = `⏳ Computing 15M/1H Reversal Strategy for ${targetSym}...`;
    if (els.btnOtherGenerate) {
      els.btnOtherGenerate.disabled = true;
      els.btnOtherGenerate.textContent = '⏳ COMPUTING...';
    }

    try {
      showToast(`⚡ Calculating 15M/1H Strategy Orders for ${targetSym}...`, 'blue');
      const res = await fetch('/api/other-symbols/calculate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: targetSym, expiry: targetExp })
      });
      const data = await res.json();
      state.otherData = data;

      if (data.success) {
        // Update spot display
        if (els.otherSpotDisplay) els.otherSpotDisplay.textContent = `₹${Number(data.spot || 0).toFixed(2)}`;
        
        // Update MTF Strips
        const pivs = data.symbol_pivots || {};
        const p15 = pivs.p15 || {};
        const p1h = pivs.p1h || {};
        if (els.otherPiv15m && p15.P) {
          els.otherPiv15m.innerHTML = `P: <strong>₹${Number(p15.P).toFixed(1)}</strong> | S1: <strong class="text-green">₹${Number(p15.S1||0).toFixed(1)}</strong> | S2: <strong class="text-green">₹${Number(p15.S2||0).toFixed(1)}</strong> | S3: <strong class="text-green">₹${Number(p15.S3||0).toFixed(1)}</strong> | R1: <strong class="text-red">₹${Number(p15.R1||0).toFixed(1)}</strong> | R2: <strong class="text-red">₹${Number(p15.R2||0).toFixed(1)}</strong> | R3: <strong class="text-red">₹${Number(p15.R3||0).toFixed(1)}</strong>`;
        }
        if (els.otherPiv1h && p1h.P) {
          els.otherPiv1h.innerHTML = `P: <strong>₹${Number(p1h.P).toFixed(1)}</strong> | S1: <strong class="text-green">₹${Number(p1h.S1||0).toFixed(1)}</strong> | S2: <strong class="text-green">₹${Number(p1h.S2||0).toFixed(1)}</strong> | S3: <strong class="text-green">₹${Number(p1h.S3||0).toFixed(1)}</strong> | R1: <strong class="text-red">₹${Number(p1h.R1||0).toFixed(1)}</strong> | R2: <strong class="text-red">₹${Number(p1h.R2||0).toFixed(1)}</strong> | R3: <strong class="text-red">₹${Number(p1h.R3||0).toFixed(1)}</strong>`;
        }

        renderOtherSymbolsTable(data.setups || []);
        if (els.otherStatusText) els.otherStatusText.textContent = `✅ ${targetSym} orders generated (${(data.setups || []).length} setups)`;
        showToast(`✅ ${targetSym} Reversal Strategy orders calculated!`, 'green');
      } else {
        showToast(`Calculation error: ${data.error || 'Failed'}`, 'red');
        if (els.otherStatusText) els.otherStatusText.textContent = `Calculation failed: ${data.error || 'Error'}`;
      }
    } catch (e) {
      showToast(`Calculation error: ${e}`, 'red');
      if (els.otherStatusText) els.otherStatusText.textContent = `Error: ${e}`;
    } finally {
      if (els.btnOtherGenerate) {
        els.btnOtherGenerate.disabled = false;
        els.btnOtherGenerate.textContent = '⚡ GENERATE ORDERS';
      }
    }
  }

  function renderOtherSymbolsTable(setups) {
    if (!els.tbodyOtherSymbols) return;
    const list = setups || (state.otherData ? state.otherData.setups : []);
    if (!list || list.length === 0) {
      els.tbodyOtherSymbols.innerHTML = '<tr><td colspan="15" class="empty-state">No strategy setups found for selected symbol.</td></tr>';
      return;
    }

    let filtered = list;
    // Filter by timeframe / direction
    if (state.otherFilter === '15M') {
      filtered = filtered.filter(s => s.timeframe === '15M');
    } else if (state.otherFilter === '1H') {
      filtered = filtered.filter(s => s.timeframe === '1H');
    } else if (state.otherFilter === 'BUY_ONLY') {
      filtered = filtered.filter(s => s.side === 'BUY');
    } else if (state.otherFilter === 'SELL_ONLY') {
      filtered = filtered.filter(s => s.side === 'SELL');
    }

    // Filter by Expiry
    if (state.otherExpiry === 'CURRENT' && state.otherData) {
      const cExp = state.otherData.current_expiry;
      if (cExp) filtered = filtered.filter(s => String(s.expiry).toUpperCase() === String(cExp).toUpperCase());
    } else if (state.otherExpiry === 'NEXT' && state.otherData) {
      const nExp = state.otherData.next_expiry;
      if (nExp) filtered = filtered.filter(s => String(s.expiry).toUpperCase() === String(nExp).toUpperCase());
    }

    if (filtered.length === 0) {
      els.tbodyOtherSymbols.innerHTML = '<tr><td colspan="15" class="empty-state">No setups matched current filter. Select ALL TIMEFRAMES or ALL EXPIRIES.</td></tr>';
      return;
    }

    const html = filtered.map(s => {
      const isBuy = (s.side === 'BUY');
      const rowClass = isBuy ? 'buy-row' : 'sell-row';
      const tfBadge = s.timeframe === '1H' ? '<span class="badge badge-1h">⏳ 1H</span>' : '<span class="badge badge-15m">⭐ 15M</span>';
      const cmpVal = Number(s.cmp || s.option_price || 0).toFixed(2);
      const epVal = Number(s.entry_price || 0).toFixed(2);
      const t1Val = Number(s.target_1 || 0).toFixed(2);
      const t2Val = (s.target_2) ? `₹${Number(s.target_2).toFixed(2)}` : '-';
      const t3Val = (s.target_3) ? `₹${Number(s.target_3).toFixed(2)}` : '-';
      const t4Val = (s.target_4) ? `₹${Number(s.target_4).toFixed(2)}` : '-';

      return `
        <tr class="${rowClass}">
          <td>${tfBadge}</td>
          <td><strong>${s.expiry || '-'}</strong></td>
          <td><strong class="font-mono">${s.strike}</strong></td>
          <td><strong class="${s.type === 'CE' ? 'text-green' : 'text-gold'}">${s.type}</strong></td>
          <td><strong class="${isBuy ? 'text-green' : 'text-red'}">${s.side}</strong></td>
          <td><strong class="text-cyan font-mono" style="font-size:12px">₹${cmpVal}</strong></td>
          <td><strong class="font-mono">₹${epVal}</strong></td>
          <td><strong class="font-mono text-green">₹${t1Val}</strong></td>
          <td><strong class="font-mono text-green">${t2Val}</strong></td>
          <td><strong class="font-mono text-green">${t3Val}</strong></td>
          <td><strong class="font-mono text-green">${t4Val}</strong></td>
          <td class="text-muted font-mono" title="SL calculated dynamically in live market @ entry confirmation">—</td>
          <td><span class="badge badge-both">${s.rr || '1:2.0'}</span></td>
          <td><span class="badge badge-waiting" style="font-size:10px">${s.signal || s.setup || 'S2 Reclaim BUY'}</span></td>
          <td>
            <button class="btn btn-primary" style="padding:3px 8px;font-size:10px" onclick="window.tradeOtherSymbol('${s.symbol}', '${s.expiry}', ${s.strike}, '${s.type}', '${s.side}', ${s.entry_price})">
              🚀 TRADE
            </button>
          </td>
        </tr>
      `;
    }).join('');

    els.tbodyOtherSymbols.innerHTML = html;
  }

  // Window helper for Paper Trade / Action on Other Symbols
  window.tradeOtherSymbol = function(sym, exp, strike, optType, side, entry) {
    showToast(`Order logged: ${side} ${sym} ${strike} ${optType} (${exp}) @ Planned ₹${entry}`, 'green');
    playAlertSound();
  };

  // Other Symbols Event Listeners
  if (els.btnOtherGenerate) {
    els.btnOtherGenerate.addEventListener('click', () => calculateOtherSymbols());
  }

  if (els.btnOtherStartFeed) {
    els.btnOtherStartFeed.addEventListener('click', async () => {
      const sym = (els.otherInputSymbol ? els.otherInputSymbol.value : '') || state.otherSymbol || 'BANKNIFTY';
      const exp = (els.otherSelExpiry ? els.otherSelExpiry.value : '') || 'ALL';
      try {
        els.btnOtherStartFeed.disabled = true;
        els.btnOtherStartFeed.textContent = '⏳ Starting...';
        const res = await fetch('/api/start-feed', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: sym, expiry: exp, exchange: 'NFO', n_strikes: 8 })
        });
        const data = await res.json();
        els.btnOtherStartFeed.textContent = '▶ STREAM IN SCANNER';
        els.btnOtherStartFeed.disabled = false;
        if (data.success) {
          showToast(`🚀 Live scanner switched to ${sym}!`, 'green');
          // Switch to Live Scanner Tab
          document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
          document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
          const scTab = document.querySelector('[data-tab="tab-scanner"]');
          const scPane = document.getElementById('tab-scanner');
          if (scTab) scTab.classList.add('active');
          if (scPane) scPane.classList.add('active');
          state.activeTab = 'tab-scanner';
          state.symbol = sym;
          if (els.inputSymbol) els.inputSymbol.value = sym;
          loadDashboard(sym);
          loadExpiries(sym);
        } else {
          showToast(`Feed error: ${data.error}`, 'red');
        }
      } catch (e) {
        if (els.btnOtherStartFeed) {
          els.btnOtherStartFeed.textContent = '▶ STREAM IN SCANNER';
          els.btnOtherStartFeed.disabled = false;
        }
        showToast(`Feed error: ${e}`, 'red');
      }
    });
  }

  if (els.btnOtherLoadPlan) {
    els.btnOtherLoadPlan.addEventListener('click', async () => {
      const sym = (els.otherInputSymbol ? els.otherInputSymbol.value : '') || state.otherSymbol || 'BANKNIFTY';
      try {
        showToast(`⏳ Loading ${sym} strategy plan into database...`, 'blue');
        const res = await fetch('/api/other-symbols/load-plan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: sym })
        });
        const data = await res.json();
        if (data.success) {
          showToast(`✅ Strategy plan for ${sym} loaded into database!`, 'green');
          playAlertSound();
        } else {
          showToast(`Load error: ${data.error || 'Failed'}`, 'red');
        }
      } catch (e) {
        showToast(`Load error: ${e}`, 'red');
      }
    });
  }

  document.querySelectorAll('.quick-chips .other-chip').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.quick-chips .other-chip').forEach(c => c.classList.remove('active'));
      b.classList.add('active');
      const s = b.getAttribute('data-sym');
      if (els.otherInputSymbol) els.otherInputSymbol.value = s;
      calculateOtherSymbols(s);
    });
  });

  if (els.otherInputSymbol) {
    els.otherInputSymbol.addEventListener('change', () => {
      const s = els.otherInputSymbol.value.trim().toUpperCase();
      calculateOtherSymbols(s);
    });
  }

  if (els.otherSelExpiry) {
    els.otherSelExpiry.addEventListener('change', () => {
      const exp = els.otherSelExpiry.value;
      state.otherExpiry = exp;
      if (state.otherData) {
        renderOtherSymbolsTable();
      } else {
        calculateOtherSymbols(null, exp);
      }
    });
  }

  document.querySelectorAll('.other-filter-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.other-filter-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.otherFilter = btn.getAttribute('data-other-filter');
      renderOtherSymbolsTable();
    });
  });

  document.querySelectorAll('.other-exp-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.other-exp-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.otherExpiry = btn.getAttribute('data-other-exp');
      renderOtherSymbolsTable();
    });
  });

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
        showToast(`⏳ Initializing live scanner for ${sym}...`, 'blue');
        const res = await fetch('/api/start-feed', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: sym, expiry: exp, exchange: 'NFO', n_strikes: 8 })
        });
        const data = await res.json();
        if (data.success) {
          state.feedRunning = true;
          els.btnStartFeed.textContent = '▶ SCANNER RUNNING';
          els.btnStartFeed.disabled = true;
          if (els.btnStopFeed) els.btnStopFeed.disabled = false;
          showToast(`🚀 Live scanner started for ${sym}`, 'green');
          playAlertSound();
          loadDashboard(sym, exp);
        } else {
          showToast(`Start failed: ${data.error}`, 'red');
          els.btnStartFeed.textContent = '▶ START SCANNER';
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
        els.btnStopFeed.disabled = true;
        await fetch('/api/stop-feed', { method: 'POST' });
        state.feedRunning = false;
        if (els.btnStartFeed) {
          els.btnStartFeed.disabled = false;
          els.btnStartFeed.textContent = '▶ START SCANNER';
        }
        showToast('⏹ Scanner feed stopped', 'gold');
        loadDashboard();
      } catch (e) {
        showToast(`Stop error: ${e}`, 'red');
        els.btnStopFeed.disabled = false;
      }
    });
  }

  // ── Broker Status & Real-time Funds Updater ──
  function updateBrokerStatus(info) {
    if (!info) return;
    if (els.brokerDot) {
      els.brokerDot.className = `broker-dot ${info.connected ? 'online' : 'offline'}`;
    }
    if (els.brokerNameText) {
      els.brokerNameText.textContent = info.broker_name || 'DHAN';
    }
    if (els.brokerFundsText) {
      if (info.connected && info.available_funds !== undefined) {
        els.brokerFundsText.textContent = `Margin: ₹${Number(info.available_funds || 0).toLocaleString('en-IN')}`;
      } else {
        els.brokerFundsText.textContent = info.connected ? 'Active Account' : 'Standby / Ready';
      }
    }
    if (els.btnAutoToggle && info.auto_trade !== undefined) {
      const isAuto = Boolean(info.auto_trade);
      els.btnAutoToggle.textContent = isAuto ? '⚡ AUTO-TRADE: ACTIVE' : '⏸ AUTO-TRADE: OFF';
      els.btnAutoToggle.style.background = isAuto ? 'rgba(16, 185, 129, 0.2)' : 'rgba(100, 116, 139, 0.2)';
      els.btnAutoToggle.style.color = isAuto ? '#10b981' : '#94a3b8';
      els.btnAutoToggle.style.borderColor = isAuto ? '#10b981' : 'rgba(100, 116, 139, 0.4)';
    }
  }

  // ── Performance & Audit Reports Manager ──
  async function loadPerformanceTab() {
    renderPerformanceTab();
    await loadBrokerOrdersLog();
  }

  function renderPerformanceTab() {
    const closed = state.closedTrades || [];
    const total = closed.length;
    let wins = 0;
    let losses = 0;
    let grossPnl = 0;
    let sumWinPts = 0;
    let sumLossPts = 0;

    closed.forEach(t => {
      const pts = Number(t.PnL_Pts || 0);
      const qty = Number(t.Quantity || getContractLotSize(t.Symbol || state.symbol || 'NIFTY'));
      grossPnl += (pts * qty);
      if (pts > 0) {
        wins++;
        sumWinPts += pts;
      } else if (pts < 0) {
        losses++;
        sumLossPts += Math.abs(pts);
      }
    });

    const charges = total * 40.0;
    const netPnl = grossPnl - charges;
    const winRate = total > 0 ? ((wins / total) * 100).toFixed(1) : '0.0';
    const avgWin = wins > 0 ? (sumWinPts / wins).toFixed(2) : '0.00';
    const avgLoss = losses > 0 ? (sumLossPts / losses).toFixed(2) : '0.00';

    if (els.tabPerfCount) els.tabPerfCount.textContent = total;
    if (els.perfTotalTrades) els.perfTotalTrades.textContent = total;
    if (els.perfWinrate) els.perfWinrate.textContent = `${winRate}%`;
    if (els.perfWinLoss) els.perfWinLoss.textContent = `${wins}W / ${losses}L`;
    if (els.perfClosedBadge) els.perfClosedBadge.textContent = total;

    if (els.perfNetPnl) {
      els.perfNetPnl.textContent = `₹${netPnl >= 0 ? '+' : ''}${netPnl.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      els.perfNetPnl.className = `score-val font-mono ${netPnl >= 0 ? 'text-green' : 'text-red'}`;
    }
    if (els.perfGrossPnl) {
      els.perfGrossPnl.textContent = `₹${grossPnl >= 0 ? '+' : ''}${grossPnl.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      els.perfGrossPnl.className = `score-val font-mono ${grossPnl >= 0 ? 'text-green' : 'text-red'}`;
    }
    if (els.perfCharges) {
      els.perfCharges.textContent = `₹${charges.toFixed(2)}`;
    }
    if (els.perfAvgWin) els.perfAvgWin.textContent = `+${avgWin} pts`;
    if (els.perfAvgLoss) els.perfAvgLoss.textContent = `-${avgLoss} pts`;

    // Filter closed trades table
    const q = (els.perfTradesFilter ? els.perfTradesFilter.value.trim().toUpperCase() : '');
    const filteredClosed = q
      ? closed.filter(t => (
          String(t.Symbol || '').toUpperCase().includes(q) ||
          String(t.Strike || '').includes(q) ||
          String(t.Trade_ID || '').toUpperCase().includes(q) ||
          String(t.Type || '').toUpperCase().includes(q)
        ))
      : closed;

    if (els.tbodyPerfClosed) {
      if (filteredClosed.length === 0) {
        els.tbodyPerfClosed.innerHTML = '<tr><td colspan="17" class="empty-state">No closed trades recorded yet for today\'s session.</td></tr>';
      } else {
        els.tbodyPerfClosed.innerHTML = filteredClosed.map(r => {
          const pts = Number(r.PnL_Pts || 0);
          const qty = Number(r.Quantity || getContractLotSize(r.Symbol || 'NIFTY'));
          const gross = pts * qty;
          const trCharges = 40.0;
          const net = gross - trCharges;
          const ptsClass = pts >= 0 ? 'text-green' : 'text-red';
          const netClass = net >= 0 ? 'text-green' : 'text-red';
          const sign = pts >= 0 ? '+' : '';

          return `
            <tr>
              <td>${r.Date || ''}</td>
              <td>${r.Entry_Time || ''}</td>
              <td>${r.Exit_Time || '-'}</td>
              <td class="font-mono text-cyan" style="font-size:11px">${r.Trade_ID || `T-${r.Date}-${r.Strike}`}</td>
              <td><span class="badge ${r.Broker === 'DHAN' ? 'badge-both' : 'badge-silver'}">${r.Broker || 'DHAN (AUTO)'}</span></td>
              <td><strong>${r.Symbol || 'NIFTY'}</strong></td>
              <td><strong class="font-mono">${r.Strike || ''}</strong></td>
              <td><strong class="${r.Type === 'CE' ? 'text-green' : 'text-gold'}">${r.Type || ''}</strong></td>
              <td><strong class="${r.Action === 'BUY' ? 'text-green' : 'text-red'}">${r.Action || 'BUY'}</strong></td>
              <td>${Number(r.Entry_Price || 0).toFixed(2)}</td>
              <td>${Number(r.Exit_Price || 0).toFixed(2)}</td>
              <td class="${ptsClass}"><strong>${sign}${pts.toFixed(2)}</strong></td>
              <td>₹${gross.toFixed(2)}</td>
              <td>₹${trCharges.toFixed(2)}</td>
              <td class="${netClass}"><strong>₹${net.toFixed(2)}</strong></td>
              <td style="font-size:11px">${r.Exit_Reason || 'TARGET / SL'}</td>
              <td><span class="badge ${pts >= 0 ? 'badge-target-hit' : 'badge-sl-hit'}">${pts >= 0 ? 'TARGET HIT' : 'SL HIT'}</span></td>
            </tr>
          `;
        }).join('');
      }
    }
  }

  async function loadBrokerOrdersLog() {
    try {
      const res = await fetch('/api/broker/orders-log');
      if (!res.ok) return;
      const data = await res.json();
      const orders = data.orders || [];
      if (els.perfBrokerOrdersBadge) els.perfBrokerOrdersBadge.textContent = orders.length;

      if (els.tbodyPerfBrokerOrders) {
        if (orders.length === 0) {
          els.tbodyPerfBrokerOrders.innerHTML = '<tr><td colspan="13" class="empty-state">No live broker orders transmitted yet. Orders execute through Mode A (manual review) or Mode B (automated entry).</td></tr>';
        } else {
          els.tbodyPerfBrokerOrders.innerHTML = orders.slice().reverse().map(o => {
            const st = (o.Status || o.status || 'PENDING');
            const isFilled = st.toUpperCase() === 'FILLED' || st.toUpperCase() === 'EXECUTED';
            const isRejected = st.toUpperCase().includes('REJECT');
            const stBadge = isFilled ? 'badge-target-hit' : (isRejected ? 'badge-sl-hit' : 'badge-both');
            const ordMode = o.Mode || o.mode || 'MANUAL';
            const ordId = o.Order_ID || o.order_id || '-';
            const brokerId = o.Broker_Order_ID || o.broker_order_id || ordId;
            const sym = o.Symbol || o.symbol || 'NIFTY';
            const strk = o.Strike || o.strike || '-';
            const optType = o.Type || o.opt_type || '-';
            const act = o.Action || o.action || 'BUY';
            const qty = o.Qty || o.quantity || '-';
            const ordType = o.Order_Type || o.order_type || 'LIMIT';
            const price = Number(o.Price || o.price || 0).toFixed(2);
            const remark = o.Remark || o.message || '-';
            const ts = o.Timestamp || o.timestamp_ist || '-';

            return `
              <tr>
                <td class="font-mono" style="font-size:11px">${ts}</td>
                <td><span class="badge ${ordMode === 'AUTO' ? 'badge-both' : 'badge-silver'}">${ordMode}</span></td>
                <td class="font-mono text-cyan" style="font-size:11px">${ordId}</td>
                <td class="font-mono text-gold" style="font-size:11px">${brokerId}</td>
                <td><strong>${sym}</strong></td>
                <td><strong class="font-mono">${strk}</strong></td>
                <td><strong class="${optType === 'CE' ? 'text-green' : 'text-gold'}">${optType}</strong></td>
                <td><strong class="${act === 'BUY' ? 'text-green' : 'text-red'}">${act}</strong></td>
                <td class="font-mono">${qty}</td>
                <td>${ordType}</td>
                <td class="font-mono">₹${price}</td>
                <td><span class="badge ${stBadge}">${st}</span></td>
                <td style="font-size:11px; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${remark}">${remark}</td>
              </tr>
            `;
          }).join('');
        }
      }
    } catch (err) {
      console.warn('Failed to load broker orders log:', err);
    }
  }

  // ── Mode A Order Execution Modal ──
  state.pendingOrder = null;

  async function prepareAndOpenOrderModal(params) {
    try {
      showToast('Validating contract and security ID...', 'blue');
      const res = await fetch('/api/order/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params)
      });
      const data = await res.json();
      if (data.status !== 'ok' && data.status !== 'valid') {
        showToast(`Order Validation Failed: ${data.message || 'Invalid contract'}`, 'red');
        return;
      }
      const ord = data.order || data;
      state.pendingOrder = ord;

      const brokerName = ord.broker || 'DHAN';
      if (els.modalOrderBroker) els.modalOrderBroker.textContent = `${brokerName} (NSE_FNO)`;
      if (els.modalOrderInstrument) els.modalOrderInstrument.textContent = ord.instrument || `${ord.symbol} ${ord.strike} ${ord.opt_type}`;
      if (els.modalOrderAction) {
        const act = ord.action || 'BUY';
        els.modalOrderAction.textContent = act;
        els.modalOrderAction.className = `val ${act === 'BUY' ? 'text-green' : 'text-red'} font-bold`;
      }
      if (els.modalOrderType) els.modalOrderType.textContent = ord.order_type || 'LIMIT';
      if (els.modalOrderQty) {
        const lots = ord.lots || 1;
        const lotUnit = ord.lot_size || getContractLotSize(ord.symbol || state.symbol || 'NIFTY');
        const qty = ord.quantity || ord.total_qty || (lots * lotUnit);
        els.modalOrderQty.textContent = `${lots} Lot (${qty} Qty)`;
      }
      const ordPrice = ord.price !== undefined ? ord.price : (ord.limit_price || 0);
      if (els.modalOrderPrice) {
        let pTxt = `₹${Number(ordPrice || 0).toFixed(2)}`;
        if (ord.bid || ord.ask) {
          pTxt += ` (Bid: ₹${Number(ord.bid || 0).toFixed(2)} | Ask: ₹${Number(ord.ask || 0).toFixed(2)})`;
        }
        els.modalOrderPrice.textContent = pTxt;
      }
      const ordSl = ord.stop_loss !== undefined ? ord.stop_loss : (ord.sl || 0);
      if (els.modalOrderSl) els.modalOrderSl.textContent = `₹${Number(ordSl || 0).toFixed(2)}`;
      const ordTgt = ord.target !== undefined ? ord.target : (ord.t1 || 0);
      if (els.modalOrderTarget) els.modalOrderTarget.textContent = `₹${Number(ordTgt || 0).toFixed(2)}`;

      const checks = data.checks || [
        `Buy Limit Placed on Bid/Ask Quote: ₹${Number(ordPrice || 0).toFixed(2)}`,
        `Contract Lot Size: 1 lot = ${ord.lot_size || 65} qty (${ord.quantity || 65} units)`,
        `Instrument Tradability & Security ID Verified (${ord.security_id || 'Active NFO'})`,
        `Single Active Trade Limit Respected`
      ];
      if (els.modalValidationBox) {
        els.modalValidationBox.innerHTML = `
          <div style="font-size:10.5px; font-weight:700; color:var(--text-muted); margin-bottom:4px;">PRE-TRADE SAFETY VALIDATIONS:</div>
          ${checks.map(c => `<div class="val-check text-green" style="font-size:11px;">✔ ${c}</div>`).join('')}
        `;
      }

      if (els.btnConfirmModalOrder) {
        els.btnConfirmModalOrder.disabled = false;
        els.btnConfirmModalOrder.textContent = '⚡ TRANSMIT TO DHAN';
      }

      if (els.orderModalBackdrop) {
        els.orderModalBackdrop.style.display = 'flex';
      }
    } catch (err) {
      showToast(`Pre-order error: ${err.message}`, 'red');
    }
  }

  function closeOrderModal() {
    if (els.orderModalBackdrop) {
      els.orderModalBackdrop.style.display = 'none';
    }
    state.pendingOrder = null;
  }

  async function confirmModalOrderExecution() {
    if (!state.pendingOrder) {
      showToast('No active order payload found', 'red');
      return;
    }
    try {
      if (els.btnConfirmModalOrder) {
        els.btnConfirmModalOrder.disabled = true;
        els.btnConfirmModalOrder.textContent = 'TRANSMITTING TO DHAN...';
      }
      const res = await fetch('/api/order/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(state.pendingOrder)
      });
      const data = await res.json();
      if (data.status === 'ok' || data.status === 'success') {
        showToast(`⚡ Order Transmitted: ID ${data.broker_order_id || data.order_id || data.trade_id || 'OK'} (${data.order_status || 'CONFIRMED'})`, 'green');
        playAlertSound();
        closeOrderModal();
        await loadBrokerOrdersLog();
        loadDashboard();
      } else {
        showToast(`Order Rejected: ${data.message || 'Execution error'}`, 'red');
        if (els.btnConfirmModalOrder) {
          els.btnConfirmModalOrder.disabled = false;
          els.btnConfirmModalOrder.textContent = '⚡ RETRY TRANSMIT';
        }
      }
    } catch (err) {
      showToast(`Transmission error: ${err.message}`, 'red');
      if (els.btnConfirmModalOrder) {
        els.btnConfirmModalOrder.disabled = false;
        els.btnConfirmModalOrder.textContent = '⚡ RETRY TRANSMIT';
      }
    }
  }

  // ── Modal & Top Bar Execution Wireup ──
  if (els.modalCloseBtn) els.modalCloseBtn.addEventListener('click', closeOrderModal);
  if (els.btnCancelModalOrder) els.btnCancelModalOrder.addEventListener('click', closeOrderModal);
  if (els.btnConfirmModalOrder) els.btnConfirmModalOrder.addEventListener('click', confirmModalOrderExecution);
  if (els.orderModalBackdrop) {
    els.orderModalBackdrop.addEventListener('click', (e) => {
      if (e.target === els.orderModalBackdrop) closeOrderModal();
    });
  }

  if (els.btnRefreshBrokerOrders) {
    els.btnRefreshBrokerOrders.addEventListener('click', async () => {
      await loadBrokerOrdersLog();
      showToast('🔄 Broker orders log updated', 'blue');
    });
  }

  if (els.perfTradesFilter) {
    els.perfTradesFilter.addEventListener('input', () => {
      renderPerformanceTab();
    });
  }

  if (els.btnKillSwitch) {
    els.btnKillSwitch.addEventListener('click', async () => {
      const ok = confirm("EMERGENCY KILL SWITCH ACTIVATION:\n\n1. Disconnect and turn OFF auto-trading.\n2. Cancel all pending broker orders.\n3. Market square-off all active open positions.\n\nAre you sure you want to proceed?");
      if (!ok) return;
      try {
        const res = await fetch('/api/order/kill', { method: 'POST' });
        const data = await res.json();
        showToast(data.message || 'Emergency Kill Switch Executed', 'red');
        playAlertSound();
        loadDashboard();
        loadBrokerOrdersLog();
      } catch (err) {
        showToast('Kill switch error: ' + err.message, 'red');
      }
    });
  }

  if (els.btnAutoToggle) {
    els.btnAutoToggle.addEventListener('click', async () => {
      try {
        const res = await fetch('/api/order/auto-toggle', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
          const isAuto = Boolean(data.auto_trade);
          els.btnAutoToggle.textContent = isAuto ? '⚡ AUTO-TRADE: ACTIVE' : '⏸ AUTO-TRADE: OFF';
          els.btnAutoToggle.style.background = isAuto ? 'rgba(16, 185, 129, 0.2)' : 'rgba(100, 116, 139, 0.2)';
          els.btnAutoToggle.style.color = isAuto ? '#10b981' : '#94a3b8';
          els.btnAutoToggle.style.borderColor = isAuto ? '#10b981' : 'rgba(100, 116, 139, 0.4)';
          showToast(`Auto-trading mode ${isAuto ? 'ACTIVATED' : 'PAUSED'}`, isAuto ? 'green' : 'blue');
        }
      } catch (err) {
        showToast('Toggle failed', 'red');
      }
    });
  }

  // ── Mapped Strikes & Custom Lots Manager ──
  function getContractLotSize(symbol) {
    const s = (symbol || '').toUpperCase();
    if (s.includes('BANKNIFTY')) return 15;
    if (s.includes('SENSEX')) return 10;
    if (s.includes('FINNIFTY')) return 25;
    if (s.includes('MIDCPNIFTY')) return 50;
    if (s.includes('NIFTY')) return 65;
    return 65; // default NIFTY (1 lot = 65 qty)
  }

  async function loadMappedStrikesData() {
    try {
      const res = await fetch('/api/mapped-strikes');
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.config) {
        state.mappedConfig = data.config;
        updateMappedStrikesSummary(state.mappedConfig);
        renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
      }
    } catch (e) {
      console.warn('Failed to load mapped strikes:', e);
    }
  }

  function updateMappedStrikesSummary(cfg) {
    if (!cfg) return;
    const strikes = cfg.strikes || [];
    const activeCount = strikes.filter(s => s.enabled).length;

    if (els.tabMappedCount) {
      els.tabMappedCount.textContent = activeCount;
    }

    if (els.toggleRestrictedStrikes) {
      els.toggleRestrictedStrikes.checked = Boolean(cfg.restrict_to_selected_only);
    }

    if (els.restrictedModeBadge) {
      if (cfg.restrict_to_selected_only) {
        els.restrictedModeBadge.textContent = '🔒 STRICT: ONLY SELECTED STRIKES';
        els.restrictedModeBadge.style.background = '#e53935';
        els.restrictedModeBadge.style.color = '#ffffff';
        els.restrictedModeBadge.style.boxShadow = '0 0 10px rgba(229, 57, 53, 0.4)';
      } else {
        els.restrictedModeBadge.textContent = 'ALL FOCUS STRIKES (UNRESTRICTED)';
        els.restrictedModeBadge.style.background = '';
        els.restrictedModeBadge.style.color = '';
        els.restrictedModeBadge.style.boxShadow = '';
      }
    }

    if (els.inputDefaultLots && document.activeElement !== els.inputDefaultLots) {
      els.inputDefaultLots.value = cfg.default_lots || 1;
    }

    if (els.mappedStrikesSummaryText) {
      const modeStr = cfg.restrict_to_selected_only ? '🔒 STRICT ONLY' : '🌐 Unrestricted';
      els.mappedStrikesSummaryText.innerHTML = `<strong>${activeCount}</strong> active / <strong>${strikes.length}</strong> total mapped | Logic Mode: <strong>${modeStr}</strong>`;
    }
  }

  function renderMappedStrikesTable(cfg, watchItems) {
    if (!els.tbodyMappedStrikes) return;
    const strikes = (cfg && cfg.strikes) || [];
    if (strikes.length === 0) {
      els.tbodyMappedStrikes.innerHTML = `<tr><td colspan="12" class="empty-state">No strikes mapped yet. Click "⚡ Import Today's 15M & 1H Focus Strikes" or add a custom strike above.</td></tr>`;
      return;
    }

    const quoteMap = {};
    if (watchItems && watchItems.length) {
      watchItems.forEach(w => {
        const key = `${w.strike}_${w.opt_type}`;
        quoteMap[key] = w;
      });
    }

    const rowsHtml = strikes.map(s => {
      const lotUnit = getContractLotSize(s.symbol);
      const totalQty = (parseInt(s.lots, 10) || 1) * lotUnit;

      let ltpDisplay = '<span class="text-silver">-</span>';
      let bidAskDisplay = '<span class="text-silver">-</span>';

      if (s.opt_type === 'BOTH') {
        const ce = quoteMap[`${s.strike}_CE`];
        const pe = quoteMap[`${s.strike}_PE`];
        const ceLtp = ce && ce.ltp ? `CE ₹${Number(ce.ltp).toFixed(1)}` : 'CE -';
        const peLtp = pe && pe.ltp ? `PE ₹${Number(pe.ltp).toFixed(1)}` : 'PE -';
        ltpDisplay = `<div style="font-size:11px;line-height:1.3;"><span class="text-green">${ceLtp}</span><br><span class="text-gold">${peLtp}</span></div>`;

        const ceDepth = ce && (ce.bid || ce.ask) ? `${Number(ce.bid || 0).toFixed(1)} / ${Number(ce.ask || 0).toFixed(1)}` : '-';
        const peDepth = pe && (pe.bid || pe.ask) ? `${Number(pe.bid || 0).toFixed(1)} / ${Number(pe.ask || 0).toFixed(1)}` : '-';
        bidAskDisplay = `<div style="font-size:10px;line-height:1.3;"><span class="text-silver">${ceDepth}</span><br><span class="text-silver">${peDepth}</span></div>`;
      } else {
        const q = quoteMap[`${s.strike}_${s.opt_type}`];
        if (q && q.ltp) {
          ltpDisplay = `<strong class="font-mono ${s.opt_type === 'CE' ? 'text-green' : 'text-gold'}">₹${Number(q.ltp).toFixed(2)}</strong>`;
        }
        if (q && (q.bid || q.ask)) {
          bidAskDisplay = `<span class="font-mono text-silver" style="font-size:11px;">${Number(q.bid || 0).toFixed(1)} / ${Number(q.ask || 0).toFixed(1)}</span>`;
        }
      }

      const optBadgeClass = s.opt_type === 'CE' ? 'badge-ce' : (s.opt_type === 'PE' ? 'badge-pe' : 'badge-both');
      const isRestricted = cfg.restrict_to_selected_only;
      const willExecute = !isRestricted || s.enabled;
      const statusBadge = willExecute
        ? `<span class="badge ${s.enabled ? 'badge-15m' : 'badge-silver'}" style="font-size:10px;">${s.enabled ? '🟢 EVALUATING' : '⚪ IDLE'}</span>`
        : `<span class="badge" style="background:#2a2e39;color:#787b86;font-size:10px;">BLOCKED</span>`;

      return `
        <tr data-strike-id="${s.id}" style="${!s.enabled ? 'opacity:0.6;' : ''}">
          <td style="text-align: center;">
            <input type="checkbox" class="mapped-active-checkbox" data-id="${s.id}" ${s.enabled ? 'checked' : ''} title="Toggle active status">
          </td>
          <td><strong>${s.symbol || 'NIFTY'}</strong></td>
          <td><strong class="font-mono" style="font-size:13px; color:var(--text-bright);">${s.strike}</strong></td>
          <td><span class="badge ${optBadgeClass}">${s.opt_type}</span></td>
          <td><span class="badge badge-1h" style="font-size:10px;">${s.source || 'MANUAL'}</span></td>
          <td>${ltpDisplay}</td>
          <td>${bidAskDisplay}</td>
          <td style="text-align: center;">
            <input type="number" class="table-lot-input" data-id="${s.id}" value="${s.lots || 1}" min="1" max="20" style="width: 54px; text-align: center;">
          </td>
          <td class="font-mono text-cyan" style="font-size:11px;">
            <span class="qty-calc">${totalQty}</span> <span style="font-size:9.5px;color:var(--text-muted);">(${lotUnit}/lot)</span>
          </td>
          <td>${statusBadge}</td>
          <td class="text-silver" style="font-size:11px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${s.notes || ''}">
            ${s.notes || '-'}
          </td>
          <td style="text-align: center;">
            <button class="btn-delete-strike" data-id="${s.id}" title="Remove Strike" style="background:none; border:none; cursor:pointer; font-size:13px; color:#ff5252;">🗑️</button>
          </td>
        </tr>
      `;
    }).join('');

    els.tbodyMappedStrikes.innerHTML = rowsHtml;

    // Attach row events
    els.tbodyMappedStrikes.querySelectorAll('.mapped-active-checkbox').forEach(cb => {
      cb.addEventListener('change', (e) => {
        const id = e.target.getAttribute('data-id');
        const st = state.mappedConfig.strikes.find(x => x.id === id);
        if (st) {
          st.enabled = e.target.checked;
          updateMappedStrikesSummary(state.mappedConfig);
          const tr = e.target.closest('tr');
          if (tr) tr.style.opacity = st.enabled ? '1' : '0.6';
        }
      });
    });

    els.tbodyMappedStrikes.querySelectorAll('.table-lot-input').forEach(inp => {
      inp.addEventListener('input', (e) => {
        const id = e.target.getAttribute('data-id');
        const st = state.mappedConfig.strikes.find(x => x.id === id);
        const val = parseInt(e.target.value, 10);
        if (st && val > 0) {
          st.lots = val;
          const tr = e.target.closest('tr');
          if (tr) {
            const qtySpan = tr.querySelector('.qty-calc');
            if (qtySpan) {
              const unit = getContractLotSize(st.symbol);
              qtySpan.textContent = val * unit;
            }
          }
        }
      });
    });

    els.tbodyMappedStrikes.querySelectorAll('.btn-delete-strike').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const id = e.target.getAttribute('data-id');
        if (!confirm('Remove this strike from mapped radar?')) return;
        try {
          const res = await fetch('/api/mapped-strikes/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ strike_id: id })
          });
          const resp = await res.json();
          if (resp.status === 'success' && resp.config) {
            state.mappedConfig = resp.config;
            updateMappedStrikesSummary(state.mappedConfig);
            renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
            showToast('Strike removed', 'silver');
          }
        } catch (err) {
          showToast('Failed to delete strike', 'red');
        }
      });
    });
  }

  // ── Mapped Strikes Event Listeners ──
  if (els.toggleRestrictedStrikes) {
    els.toggleRestrictedStrikes.addEventListener('change', async (e) => {
      const enabled = e.target.checked;
      try {
        const res = await fetch('/api/mapped-strikes/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled })
        });
        const resp = await res.json();
        if (resp.status === 'success' && resp.config) {
          state.mappedConfig = resp.config;
          updateMappedStrikesSummary(state.mappedConfig);
          renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
          const msg = enabled ? '🔒 Strict Logic Mode: Strategy will ONLY evaluate & trade selected strikes' : '🌐 Unrestricted Mode: Strategy evaluates all focus strikes';
          showToast(msg, enabled ? 'gold' : 'blue');
        }
      } catch (err) {
        showToast('Failed to toggle restriction mode', 'red');
      }
    });
  }

  if (els.btnImportFocusStrikes) {
    els.btnImportFocusStrikes.addEventListener('click', async () => {
      try {
        const lots = parseInt(els.inputDefaultLots ? els.inputDefaultLots.value : 1, 10) || 1;
        const sym = state.symbol || 'NIFTY';
        const res = await fetch('/api/mapped-strikes/import-focus', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: sym, lots })
        });
        const resp = await res.json();
        if (resp.status === 'success' && resp.config) {
          state.mappedConfig = resp.config;
          updateMappedStrikesSummary(state.mappedConfig);
          renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
          showToast(`⚡ Imported ${resp.imported_count || 0} focus strikes for ${sym}`, 'green');
        }
      } catch (err) {
        showToast('Failed to import focus strikes', 'red');
      }
    });
  }

  if (els.btnSaveMappedStrikes) {
    els.btnSaveMappedStrikes.addEventListener('click', async () => {
      try {
        const defLots = parseInt(els.inputDefaultLots ? els.inputDefaultLots.value : 1, 10) || 1;
        state.mappedConfig.default_lots = defLots;
        const res = await fetch('/api/mapped-strikes/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            default_lots: defLots,
            strikes: state.mappedConfig.strikes || []
          })
        });
        const resp = await res.json();
        if (resp.status === 'success' && resp.config) {
          state.mappedConfig = resp.config;
          updateMappedStrikesSummary(state.mappedConfig);
          renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
          showToast('💾 Mapped strikes and lot sizes saved to disk', 'green');
        }
      } catch (err) {
        showToast('Failed to save mapped strikes', 'red');
      }
    });
  }

  if (els.btnClearMappedStrikes) {
    els.btnClearMappedStrikes.addEventListener('click', async () => {
      if (!confirm('Are you sure you want to clear all mapped strikes?')) return;
      try {
        const res = await fetch('/api/mapped-strikes/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' }
        });
        const resp = await res.json();
        if (resp.status === 'success' && resp.config) {
          state.mappedConfig = resp.config;
          updateMappedStrikesSummary(state.mappedConfig);
          renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
          showToast('Cleared mapped strikes list', 'gold');
        }
      } catch (err) {
        showToast('Failed to clear strikes', 'red');
      }
    });
  }

  if (els.btnAddMappedStrike) {
    els.btnAddMappedStrike.addEventListener('click', async () => {
      const sym = (els.addStrikeSymbol ? els.addStrikeSymbol.value : 'NIFTY').trim().toUpperCase();
      const strike = parseFloat(els.addStrikePrice ? els.addStrikePrice.value : 0);
      const optType = els.addStrikeType ? els.addStrikeType.value : 'BOTH';
      const lots = parseInt(els.addStrikeLots ? els.addStrikeLots.value : 1, 10) || 1;
      const note = els.addStrikeNote ? els.addStrikeNote.value.trim() : '';

      if (!strike || strike <= 0) {
        showToast('Please enter a valid strike price', 'red');
        return;
      }

      try {
        const res = await fetch('/api/mapped-strikes/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            symbol: sym,
            strike: strike,
            opt_type: optType,
            lots: lots,
            note: note
          })
        });
        const resp = await res.json();
        if (resp.status === 'success' && resp.config) {
          state.mappedConfig = resp.config;
          updateMappedStrikesSummary(state.mappedConfig);
          renderMappedStrikesTable(state.mappedConfig, state.lastWatchItems);
          if (els.addStrikePrice) els.addStrikePrice.value = '';
          if (els.addStrikeNote) els.addStrikeNote.value = '';
          showToast(`Added ${sym} ${strike} (${optType}) with ${lots} lot(s)`, 'green');
        }
      } catch (err) {
        showToast('Failed to add strike', 'red');
      }
    });
  }

  // ── Bootstrap ──
  loadDashboard('NIFTY');
  loadExpiries('NIFTY');
  loadPlannerDeck('NIFTY');
  loadMappedStrikesData();
  connectWebSocket();
})();
