'use strict';

const STORAGE_KEY = 'vma_dashboard_state_v3';
const INDIA_TIMEZONE = 'Asia/Kolkata';
const MARKET_WINDOW = Object.freeze({
  startMinutes: 9 * 60 + 16,
  endMinutes: 15 * 60 + 30,
});
// EOD auto square-off window: 15:20–15:25 exit trade, 15:25+ stop simulation
const EOD_WINDOW = Object.freeze({
  exitFromMinutes: 15 * 60 + 20,
  exitUntilMinutes: 15 * 60 + 25,
  stopFromMinutes: 15 * 60 + 25,
});
const DEFAULTS = Object.freeze({
  timeframe: '5min',
  shortLen: '5',
  longLen: '9',
  refreshInterval: '10000',
  instrument: 'options',
  sl: '40',
  target: '60',
  trailTrigger: '25',
  trailLock: '15',
  lotSize: '65',
  delta: '0.5',
  minQuality: '2',
  sidewaysFilter: false,
  confirmCandle: false,
});

const state = {
  tf: DEFAULTS.timeframe,
  dualData: null,
  simActive: false,
  simStartTime: null,
  simParams: null,
  simPosition: null,
  simSessionId: null,
  simTrades: [],
  simLastTs: null,
  lastSavedTradeCount: 0,
  pollTimer: null,
  clockTimer: null,
  savedTrades: [],
  manualSessionPause: false,
  historyPage: 1,
  historyPageSize: 15,
  tradesPage: 1,
  tradesPageSize: 10,
  lastTradeType: null,
  // EOD date guards – store the IST date string ('YYYY-MM-DD') when action was last taken
  eodExitDoneDate: null,
  eodStopDoneDate: null,
  // Timestamp of the bar on which the last trade closed — new entries only allowed on LATER bars
  simLastExitTs: null,
};

const els = {};

document.addEventListener('DOMContentLoaded', async () => {
  bindElements();
  bindEvents();
  registerServiceWorker();
  restoreState();
  syncFormToState();
  renderAll();
  startMarketClock();
  await Promise.all([loadDashboard(), loadSavedTrades()]);
  await loadBackendActiveTrade();
  syncMarketSession(true);
});

function bindElements() {
  [
    'liveBadge', 'statusBox', 'calcBtn', 'timeframe', 'shortLen', 'longLen', 'refreshInterval',
    'heroSignal', 'heroTimestamp', 'heroVma', 'heroPosition', 'heroClose', 'heroRsi', 'heroQuality', 'heroSideways',
    'historyMeta', 'historyTable', 'resultMeta', 'resultMetaInline', 'statTrades', 'statWinRate', 'statPnl', 'statBest', 'statWorst', 'statRR',
    'activeTradeCard', 'activeTradeGrid', 'simBtn', 'resetBtn', 'inpInstrument', 'inpSL', 'inpTarget', 'inpTrailTrigger', 'inpTrailLock',
    'inpLotSize', 'inpDelta', 'simShortLen', 'simLongLen', 'inpMinQuality', 'inpSidewaysFilter', 'inpConfirmCandle',
    'savedTradesTable', 'savedTradesMeta', 'marketClock', 'sessionStatus', 'sessionWindow', 'closeActiveTradeBtn',
    'historyPagination', 'historyPrevBtn', 'historyNextBtn', 'historyPageInfo',
    'tradesPagination', 'tradesPrevBtn', 'tradesNextBtn', 'tradesPageInfo'
  ].forEach((id) => {
    els[id] = document.getElementById(id);
  });
}

function bindEvents() {
  els.calcBtn.addEventListener('click', () => loadDashboard(true));
  els.simBtn.addEventListener('click', () => state.simActive ? stopSimulation({ manual: true, reason: 'MANUAL' }) : startSimulation({ manual: true }));
  els.resetBtn.addEventListener('click', resetSimulationForm);
  els.inpInstrument.addEventListener('change', updateInstrumentMode);
  ['shortLen', 'simShortLen'].forEach((id) => els[id].addEventListener('input', syncLengthFields));
  ['longLen', 'simLongLen'].forEach((id) => els[id].addEventListener('input', syncLengthFields));
  ['refreshInterval', 'inpSL', 'inpTarget', 'inpTrailTrigger', 'inpTrailLock', 'inpLotSize', 'inpDelta', 'inpMinQuality', 'inpSidewaysFilter', 'inpConfirmCandle'].forEach((id) => {
    els[id].addEventListener('change', persistState);
  });
  els.timeframe.addEventListener('change', handleTimeframeChange);
  els.closeActiveTradeBtn.addEventListener('click', closeActivePositionManually);
  els.historyPrevBtn.addEventListener('click', () => {
    if (state.historyPage > 1) {
      state.historyPage--;
      renderDashboard();
    }
  });
  els.historyNextBtn.addEventListener('click', () => {
    const allHistory = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history : [];
    const totalPages = Math.ceil(allHistory.length / state.historyPageSize);
    if (state.historyPage < totalPages) {
      state.historyPage++;
      renderDashboard();
    }
  });
  els.tradesPrevBtn.addEventListener('click', () => {
    if (state.tradesPage > 1) {
      state.tradesPage--;
      renderSavedTrades();
    }
  });
  els.tradesNextBtn.addEventListener('click', () => {
    const allRows = Array.isArray(state.savedTrades) ? state.savedTrades : [];
    const completedTrades = allRows.filter((t) => t.entryPrice != null && t.exitPrice != null && t.type);
    const totalPages = Math.ceil(completedTrades.length / state.tradesPageSize);
    if (state.tradesPage < totalPages) {
      state.tradesPage++;
      renderSavedTrades();
    }
  });
}

function syncLengthFields(event) {
  if (event.target.id === 'shortLen') els.simShortLen.value = els.shortLen.value;
  if (event.target.id === 'simShortLen') els.shortLen.value = els.simShortLen.value;
  if (event.target.id === 'longLen') els.simLongLen.value = els.longLen.value;
  if (event.target.id === 'simLongLen') els.longLen.value = els.simLongLen.value;
  persistState();
}

function updateInstrumentMode() {
  const isOptions = els.inpInstrument.value === 'options';
  els.inpDelta.disabled = isOptions;
  persistState();
}

async function handleTimeframeChange() {
  persistState();

  // Always reload the dashboard with the new timeframe so the chart/history updates.
  await loadDashboard(false);

  // If the simulation is running, seamlessly restart it on the new timeframe.
  if (state.simActive) {
    const newTf = els.timeframe.value;

    // Close any open position at last known price before switching.
    if (state.simPosition) {
      closeOpenPosition('TF_SWITCH');
      await persistCompletedTrades();
    }

    // Reset simulation state for the fresh timeframe (keep same risk params).
    state.simLastTs = null;
    state.simTrades = [];
    state.lastSavedTradeCount = 0;
    state.lastTradeType = null;
    state.simPosition = null;
    state.simStartTime = resolveSimulationStartTime(state.simParams);
    state.simSessionId = buildSimulationSessionId(state.simParams);
    state.tf = newTf;

    startPolling();
    renderAll();
    persistState();
    setStatus(`Timeframe switched to ${newTf}. Simulation auto-restarted.`, false, true);
  }
}

async function loadDashboard(showMessage = false) {
  const tf = els.timeframe.value;
  const shortLen = parseInt(els.shortLen.value, 10);
  const longLen = parseInt(els.longLen.value, 10);
  if (!Number.isFinite(shortLen) || !Number.isFinite(longLen) || shortLen >= longLen) {
    setStatus('Short VMA must be smaller than Long VMA.', true);
    return;
  }

  try {
    const params = new URLSearchParams({ tf, short_len: String(shortLen), long_len: String(longLen) });
    const response = await fetch('/api/dual-vma?' + params.toString(), { cache: 'no-store' });
    const json = await response.json();
    if (!json.ok) throw new Error(json.error || 'Unable to load VMA data');
    state.tf = tf;
    state.dualData = json;
    renderDashboard();
    persistState();
    if (showMessage) {
      setStatus('Dashboard refreshed with ' + json.total_bars + ' bars from ' + tf + '.', false, true);
    }
  } catch (error) {
    setStatus(error.message, true);
  }
}

function startSimulation(options = {}) {
  const simParams = readSimulationParams();
  if (!simParams) return false;
  state.manualSessionPause = false;
  state.simActive = true;
  state.simStartTime = resolveSimulationStartTime(simParams);
  state.simSessionId = buildSimulationSessionId(simParams);
  state.simParams = simParams;
  state.simPosition = null;
  state.simTrades = [];
  state.simLastTs = null;
  state.simLastExitTs = null;   // reset: no prior exit in this session
  state.lastTradeType = null;
  state.lastSavedTradeCount = 0;
  renderSimulation();
  setStatus(options.manual ? 'Simulation started manually.' : 'Simulation auto-started for the live market session.', false, true);
  persistState();
  startPolling();
  return true;
}

function stopSimulation(options = {}) {
  state.simActive = false;
  if (options.manual && isMarketOpen()) {
    state.manualSessionPause = true;
  }
  if (!options.manual) {
    state.manualSessionPause = false;
  }
  stopPolling();
  closeOpenPosition(options.reason || 'EOD');
  persistCompletedTrades().catch((error) => setStatus('Trade persistence failed: ' + error.message, true));
  renderSimulation();
  setStatus(resolveStopMessage(options), false, true);
  persistState();
}

function resolveStopMessage(options) {
  if (options.reason === 'MANUAL') return 'Simulation paused manually. Auto-start will resume on the next market session.';
  if (options.reason === 'EOD') return 'Market session closed at 3:30 PM IST. Open trades were closed and saved.';
  return 'Simulation stopped. Trades persisted and UI state saved locally.';
}

function startPolling() {
  stopPolling();
  pollAndProcess();
  state.pollTimer = window.setInterval(pollAndProcess, parseInt(els.refreshInterval.value, 10));
  renderBadge();
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  renderBadge();
}

async function pollAndProcess() {
  if (!state.simActive || !state.simParams) return;
  try {
    const params = new URLSearchParams({
      tf: els.timeframe.value,
      short_len: String(state.simParams.slen),
      long_len: String(state.simParams.llen),
    });
    const response = await fetch('/api/dual-vma?' + params.toString(), { cache: 'no-store' });
    const json = await response.json();
    if (!json.ok) throw new Error(json.error || 'Polling failed');
    state.dualData = json;
    const history = Array.isArray(json.history) ? json.history : [];
    const newBars = history.filter((bar) => {
      const ts = asDate(bar.timestamp);
      const lastTs = asDate(state.simLastTs);
      const startTs = asDate(state.simStartTime);
      return ts && startTs && ts >= startTs && (!lastTs || ts > lastTs);
    });

    for (const bar of newBars) {
      await processBar(bar);
      state.simLastTs = bar.timestamp;
    }

    renderAll();
    await persistCompletedTrades();
    persistState();
  } catch (error) {
    if (error.name === 'AbortError' || error.message.includes('aborted') || error.message.includes('Aborted')) {
      return;
    }
    setStatus('Live polling error: ' + error.message, true);
  }
}

async function processBar(bar) {
  if (state.simPosition) {
    await updateOpenPosition(bar);
    return;
  }

  const signal = getEntrySignal(bar, state.simParams);
  if (!['CE', 'PE'].includes(signal)) return;

  // ── Fresh-signal guard ─────────────────────────────────────────────────────
  // Only enter on bars that arrived STRICTLY AFTER the bar on which the last
  // trade closed. This prevents re-using the same (or earlier) candle's signal
  // immediately after a SL / Target / Manual / EOD square-off.
  if (state.simLastExitTs) {
    const barTs      = asDate(bar.timestamp);
    const lastExitTs = asDate(state.simLastExitTs);
    if (barTs && lastExitTs && barTs <= lastExitTs) return;  // stale bar — skip
  }

  // Alternation Logic: strictly alternate CE and PE trades
  if (state.lastTradeType && state.lastTradeType === signal) return;
  if (state.simParams.sidewaysFilter && bar.is_sideways) return;
  if ((bar.quality || 0) < state.simParams.minQuality) return;

  if (state.simParams.instrument === 'options') {
    const quote = await fetchAngelOptionQuote(signal, bar.close, null);
    const entry = parseFloat(quote.ltp);
    state.simPosition = {
      type: signal,
      instrument: 'options',
      entry: entry,
      entryTs: bar.timestamp,
      initSL: entry - state.simParams.sl,
      curSL: entry - state.simParams.sl,
      tgt: entry + state.simParams.target,
      contract: quote.tradingsymbol,
      tradingsymbol: quote.tradingsymbol,
      symboltoken: quote.symboltoken,
      strike: quote.strike,
      expiry: quote.expiry,
      lastPrice: entry,
      lastSpot: bar.close,
    };
  } else {
    const entry = parseFloat(bar.close);
    const direction = signal === 'CE' ? 1 : -1;
    state.simPosition = {
      type: signal,
      instrument: 'futures',
      entry: entry,
      entryTs: bar.timestamp,
      initSL: entry - (direction * state.simParams.sl),
      curSL: entry - (direction * state.simParams.sl),
      tgt: entry + (direction * state.simParams.target),
      lastPrice: entry,
      lastSpot: entry,
    };
  }
  await syncActiveTrade('ACTIVE');
  persistState();
}

async function updateOpenPosition(bar) {
  const pos = state.simPosition;
  if (!pos) return;

  if (pos.instrument === 'options') {
    const quote = await fetchAngelOptionQuote(pos.type, bar.close, pos);
    const price = parseFloat(quote.ltp);
    pos.lastPrice = price;
    pos.lastSpot = bar.close;
    if (state.simParams.trailTrigger > 0 && price - pos.entry >= state.simParams.trailTrigger) {
      pos.curSL = Math.max(pos.curSL, pos.entry + state.simParams.trailLock);
    }
    if (price <= pos.curSL) return completeTrade(pos.curSL, bar.timestamp, pos.curSL > pos.initSL ? 'TRAILING_SL' : 'SL');
    if (price >= pos.tgt) return completeTrade(pos.tgt, bar.timestamp, 'TARGET');
  } else {
    pos.lastPrice = bar.close;
    pos.lastSpot = bar.close;
    if (pos.type === 'CE') {
      if (state.simParams.trailTrigger > 0 && bar.high - pos.entry >= state.simParams.trailTrigger) {
        pos.curSL = Math.max(pos.curSL, bar.close - state.simParams.trailLock);
      }
      if (bar.low <= pos.curSL) return completeTrade(pos.curSL, bar.timestamp, pos.curSL > pos.initSL ? 'TRAILING_SL' : 'SL');
      if (bar.high >= pos.tgt) return completeTrade(pos.tgt, bar.timestamp, 'TARGET');
    } else {
      if (state.simParams.trailTrigger > 0 && pos.entry - bar.low >= state.simParams.trailTrigger) {
        pos.curSL = Math.min(pos.curSL, bar.close + state.simParams.trailLock);
      }
      if (bar.high >= pos.curSL) return completeTrade(pos.curSL, bar.timestamp, pos.curSL < pos.initSL ? 'TRAILING_SL' : 'SL');
      if (bar.low <= pos.tgt) return completeTrade(pos.tgt, bar.timestamp, 'TARGET');
    }
  }
  await syncActiveTrade('ACTIVE');
}

function closeOpenPosition(reason) {
  const pos = state.simPosition;
  if (!pos) return;
  completeTrade(pos.lastPrice || pos.entry, latestTimestamp(), reason);
}

async function closeActivePositionManually() {
  const pos = state.simPosition;
  if (!pos) return;
  if (confirm('Are you sure you want to square off this active position manually?')) {
    if (!state.simParams) {
      state.simParams = {
        lotSize: parseInt(els.inpLotSize.value, 10) || 65,
        delta: parseFloat(els.inpDelta.value) || 0.5,
        instrument: els.inpInstrument.value || 'options',
      };
    }
    completeTrade(pos.lastPrice || pos.entry, latestTimestamp(), 'MANUAL');
    renderAll();
    await persistCompletedTrades();
    persistState();
    setStatus('Position squared off manually.', false, true);
  }
}

function completeTrade(exitPrice, exitTs, reason) {
  const trade = buildTrade(state.simPosition, exitPrice, exitTs, reason);
  // Record the bar timestamp at which this trade closed so that processBar
  // can refuse entry signals from that same bar or any earlier bar.
  state.simLastExitTs = state.simPosition.entryTs;  // conservative: skip the ENTRY bar too
  // If we have an explicit exitTs that is a valid bar timestamp, prefer it
  const exitBarTs = asDate(exitTs);
  const entryBarTs = asDate(state.simPosition.entryTs);
  if (exitBarTs && entryBarTs && exitBarTs > entryBarTs) {
    state.simLastExitTs = exitTs;
  }
  state.simTrades.push(trade);
  state.lastTradeType = trade.type;
  state.simPosition = null;
  syncActiveTrade('CLOSED', trade).catch((error) => setStatus('Active trade close sync failed: ' + error.message, true));
  persistState();
}

function buildTrade(position, exitPrice, exitTs, reason) {
  const lotSize = state.simParams.lotSize;
  let points;
  if (position.instrument === 'options') {
    points = exitPrice - position.entry;
  } else {
    const direction = position.type === 'CE' ? 1 : -1;
    points = (exitPrice - position.entry) * direction * state.simParams.delta;
  }

  const trailSL = (position.curSL != null && round(position.curSL) !== round(position.initSL))
    ? round(position.curSL)
    : null;

  return {
    type: position.type,
    instrument: position.instrument,
    contract: position.contract || null,
    expiry: position.expiry || null,
    entryTs: position.entryTs,
    entryPrice: round(position.entry),
    exitTs: exitTs,
    exitPrice: round(exitPrice),
    sl: round(position.initSL),
    tgt: round(position.tgt),
    trailSL: trailSL,
    lotSize: lotSize,
    pts: round(points),
    grossPnl: round(points * lotSize),
    reason: reason,
  };
}

async function fetchAngelOptionQuote(side, spot, existingContract) {
  const params = existingContract && existingContract.tradingsymbol && existingContract.symboltoken
    ? new URLSearchParams({ exchange: 'NFO', tradingsymbol: existingContract.tradingsymbol, symboltoken: String(existingContract.symboltoken) })
    : new URLSearchParams({ side, spot: String(spot) });
  const endpoint = existingContract && existingContract.tradingsymbol && existingContract.symboltoken ? '/api/angel/ltp?' : '/api/angel/option-ltp?';
  const response = await fetch(endpoint + params.toString(), { cache: 'no-store' });
  const json = await response.json();
  if (!json.ok) throw new Error(json.error || 'Option quote fetch failed');
  return json;
}

async function persistCompletedTrades() {
  if (state.simTrades.length <= state.lastSavedTradeCount || !state.simParams) return;
  const tradesToSave = state.simTrades.slice(state.lastSavedTradeCount);
  const payload = {
    trades: tradesToSave,
    meta: {
      timeframe: state.tf,
      started_at: state.simStartTime,
      params: state.simParams,
    },
  };

  const response = await fetch('/api/vma-trades', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const json = await response.json();
  if (!json.ok) throw new Error(json.error || 'Trade persistence failed');
  state.lastSavedTradeCount = state.simTrades.length;
  await loadSavedTrades();
}

async function syncActiveTrade(status = 'ACTIVE', trade = null) {
  if (!state.simSessionId || !state.simParams) return;

  const payload = {
    session_id: state.simSessionId,
    status,
    position: state.simPosition,
    trade,
    opened_at: state.simPosition ? state.simPosition.entryTs : null,
    closed_at: trade ? trade.exitTs : null,
    meta: {
      timeframe: state.tf,
      started_at: state.simStartTime,
      params: state.simParams,
    },
  };

  const response = await fetch('/api/vma-active-trade', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const json = await readApiJson(response, 'Active trade sync failed');
  if (!json.ok) throw new Error(json.error || 'Active trade sync failed');
}

async function loadBackendActiveTrade() {
  try {
    const response = await fetch('/api/vma-active-trade', { cache: 'no-store' });
    const json = await readApiJson(response, 'Backend active trade sync failed');
    if (!json.ok || !json.active_trade || !json.active_trade.position) return;

    const active = json.active_trade;
    
    // Check if the restored trade is from today in IST
    const todayStr = new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(new Date());
    const tradeDate = asDate(active.opened_at || (active.position ? active.position.entryTs : null));
    const tradeDateStr = tradeDate ? new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(tradeDate) : '';
    
    if (tradeDateStr !== todayStr) {
      console.log('Ignoring stale backend active trade from previous day:', active.opened_at);
      return;
    }

    if (!state.simPosition) {
      state.simPosition = active.position;
      state.simParams = active.meta && active.meta.params ? active.meta.params : state.simParams;
      state.simStartTime = active.meta && active.meta.started_at ? active.meta.started_at : state.simStartTime;
      state.simSessionId = active.session_id || state.simSessionId;
      state.simActive = true;
      startPolling();
      renderAll();
      persistState();
      setStatus('Backend active simulation trade restored.', false, true);
    }
  } catch (error) {
    setStatus('Backend active trade sync failed: ' + error.message, true);
  }
}

async function readApiJson(response, fallbackMessage) {
  const contentType = response.headers.get('content-type') || '';
  const text = await response.text();

  if (!contentType.includes('application/json')) {
    const preview = text.replace(/\s+/g, ' ').trim().slice(0, 120);
    if (preview.toLowerCase().startsWith('<!doctype') || preview.startsWith('<')) {
      throw new Error(`${fallbackMessage}: backend returned an HTML page for ${response.url}. Restart the Flask/Docker backend so the latest API routes are loaded.`);
    }
    throw new Error(`${fallbackMessage}: backend returned ${response.status || 'non-JSON'} ${preview}`);
  }

  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${fallbackMessage}: invalid JSON response`);
  }
}

async function loadSavedTrades() {
  try {
    const response = await fetch('/api/vma-trades?limit=50', { cache: 'no-store' });
    const json = await response.json();
    if (!json.ok) throw new Error(json.error || 'Unable to fetch saved trades');
    state.savedTrades = json.trades || [];
    els.savedTradesMeta.textContent = (json.count || 0) + ' trades saved';
    if (state.savedTrades.length > 0) {
      const validCompleted = state.savedTrades.filter(t => t.entryPrice != null && t.exitPrice != null && t.type);
      if (validCompleted.length > 0) {
        state.lastTradeType = validCompleted[validCompleted.length - 1].type;
      }
    }
    renderSavedTrades();
  } catch (error) {
    els.savedTradesMeta.textContent = 'Sync failed';
    setStatus('Saved trades sync failed: ' + error.message, true);
  }
}

function buildSimulationSessionId(simParams) {
  const raw = [
    state.simStartTime || new Date().toISOString(),
    els.timeframe.value,
    simParams.instrument,
    simParams.slen,
    simParams.llen,
    Date.now(),
  ].join('|');
  return 'sim-' + hashString(raw);
}

function readSimulationParams() {
  const params = {
    instrument: els.inpInstrument.value,
    sl: parseFloat(els.inpSL.value),
    target: parseFloat(els.inpTarget.value),
    trailTrigger: parseFloat(els.inpTrailTrigger.value || '0'),
    trailLock: parseFloat(els.inpTrailLock.value || '0'),
    lotSize: parseInt(els.inpLotSize.value, 10),
    delta: parseFloat(els.inpDelta.value || '0.5'),
    slen: parseInt(els.simShortLen.value, 10),
    llen: parseInt(els.simLongLen.value, 10),
    minQuality: parseInt(els.inpMinQuality.value, 10),
    sidewaysFilter: els.inpSidewaysFilter.checked,
    confirmCandle: els.inpConfirmCandle.checked,
  };

  if (!Number.isFinite(params.sl) || params.sl <= 0) return setStatus('Stop loss must be greater than 0.', true), null;
  if (!Number.isFinite(params.target) || params.target <= 0) return setStatus('Target must be greater than 0.', true), null;
  if (!Number.isFinite(params.lotSize) || params.lotSize <= 0) return setStatus('Lot size must be greater than 0.', true), null;
  if (!Number.isFinite(params.slen) || !Number.isFinite(params.llen) || params.slen >= params.llen) return setStatus('Simulation short VMA must be smaller than long VMA.', true), null;
  return params;
}

function getDashboardSimParams() {
  return {
    minQuality: parseInt(els.inpMinQuality.value, 10) || 0,
    sidewaysFilter: els.inpSidewaysFilter.checked,
    confirmCandle: els.inpConfirmCandle.checked,
  };
}

function analyzeHistoryDecisions(history, simParams) {
  const bars = Array.isArray(history) ? history : [];
  let lastAcceptedSignal = null;

  return bars.map((bar) => {
    const decision = getBarDecision(bar, simParams, lastAcceptedSignal);
    if (decision.eligible) {
      lastAcceptedSignal = decision.entrySignal;
    }
    return {
      ...bar,
      skipReason: decision.reason,
      entrySignal: decision.entrySignal,
    };
  });
}

function resolveSimulationStartTime(simParams) {
  const history = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history : [];
  const analyzed = analyzeHistoryDecisions(history, simParams);
  const todayStr = new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(new Date());

  // Find the latest eligible bar from TODAY
  const latestEligible = analyzed.slice().reverse().find((bar) => {
    if (bar.skipReason !== 'Eligible') return false;
    const barDate = asDate(bar.timestamp);
    if (!barDate) return false;
    const barDateStr = new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(barDate);
    return barDateStr === todayStr;
  });

  if (latestEligible && latestEligible.timestamp) {
    return latestEligible.timestamp;
  }

  // Fallback to lookback, but clamp it to start of today's market session (09:16 IST)
  const lookbackMs = getSimulationLookbackMs(els.timeframe.value, simParams.confirmCandle);
  const fallbackDate = new Date(Date.now() - lookbackMs);
  
  // Construct today's market start (09:16 IST)
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: INDIA_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  const parts = formatter.formatToParts(new Date());
  const dateMap = parts.reduce((acc, part) => {
    if (part.type !== 'literal') acc[part.type] = part.value;
    return acc;
  }, {});
  const todayMarketStart = new Date(`${dateMap.year}-${dateMap.month}-${dateMap.day}T09:16:00+05:30`);

  // Use the later of fallbackDate or todayMarketStart
  return (fallbackDate > todayMarketStart ? fallbackDate : todayMarketStart).toISOString();
}

function getBarDecision(bar, simParams, lastAcceptedSignal) {
  const entrySignal = getEntrySignal(bar, simParams);
  const hasDirectSignal = bar.signal === 'CE' || bar.signal === 'PE';

  if (entrySignal !== 'CE' && entrySignal !== 'PE') {
    if (simParams.confirmCandle && hasDirectSignal) {
      return { eligible: false, reason: 'Waiting for confirm candle', entrySignal: 'NONE' };
    }
    return { eligible: false, reason: 'No entry signal', entrySignal: 'NONE' };
  }

  if (lastAcceptedSignal && lastAcceptedSignal === entrySignal) {
    return { eligible: false, reason: 'Same side as last trade', entrySignal };
  }

  if (simParams.sidewaysFilter && bar.is_sideways) {
    return { eligible: false, reason: 'Sideways filter blocked', entrySignal };
  }

  if ((bar.quality || 0) < simParams.minQuality) {
    return { eligible: false, reason: `Quality ${bar.quality || 0} below min ${simParams.minQuality}`, entrySignal };
  }

  return { eligible: true, reason: 'Eligible', entrySignal };
}

function getEntrySignal(bar, simParams) {
  const directSignal = bar.signal === 'CE' || bar.signal === 'PE' ? bar.signal : 'NONE';
  const confirmedSignal = bar.confirm_signal === 'CE' || bar.confirm_signal === 'PE' ? bar.confirm_signal : 'NONE';

  if (simParams.confirmCandle) return confirmedSignal;
  if (simParams.minQuality > 0 && confirmedSignal !== 'NONE') return confirmedSignal;
  return directSignal;
}

function renderAll() {
  renderDashboard();
  renderSimulation();
  renderSavedTrades();
  renderBadge();
  renderSessionInfo();
}

function renderDashboard() {
  const current = state.dualData && state.dualData.current ? state.dualData.current : null;
  els.heroSignal.textContent = current ? getEntrySignal(current, getDashboardSimParams()) : '-';
  els.heroTimestamp.textContent = current ? formatDateTime(current.timestamp) : 'Waiting for data';
  els.heroVma.textContent = current ? formatNumber(current.short_vma) + ' / ' + formatNumber(current.long_vma) : '-';
  els.heroPosition.textContent = current ? (current.position || '-') : '-';
  els.heroClose.textContent = current ? 'Rs ' + formatNumber(current.close) : '-';
  els.heroRsi.textContent = current ? 'RSI ' + formatNumber(current.rsi) : '-';
  els.heroQuality.textContent = current ? String(current.quality ?? '-') : '-';
  els.heroSideways.textContent = current ? (current.is_sideways ? 'Sideways' : 'Trending') : '-';
  els.historyMeta.textContent = state.dualData ? state.dualData.total_bars + ' bars loaded' : 'No bars loaded';

  const rawHistory = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history : [];
  const allHistory = analyzeHistoryDecisions(rawHistory, getDashboardSimParams()).slice().reverse();
  const totalItems = allHistory.length;

  if (totalItems > 0) {
    els.historyPagination.style.display = 'flex';
    const totalPages = Math.ceil(totalItems / state.historyPageSize);
    if (state.historyPage > totalPages) state.historyPage = totalPages;
    if (state.historyPage < 1) state.historyPage = 1;

    els.historyPageInfo.textContent = `Page ${state.historyPage} of ${totalPages}`;
    els.historyPrevBtn.disabled = state.historyPage === 1;
    els.historyNextBtn.disabled = state.historyPage === totalPages;

    const startIdx = (state.historyPage - 1) * state.historyPageSize;
    const endIdx = startIdx + state.historyPageSize;
    const rows = allHistory.slice(startIdx, endIdx);

    els.historyTable.innerHTML = rows.map((bar) => `
      <tr>
        <td>${formatDateTime(bar.timestamp)}</td>
        <td>${formatNumber(bar.close)}</td>
        <td>${formatNumber(bar.short_vma)}</td>
        <td>${formatNumber(bar.long_vma)}</td>
        <td>${signalPill(bar.signal)}</td>
        <td>${signalPill(bar.confirm_signal)}</td>
        <td>${bar.quality ?? '-'}</td>
        <td>${escapeHtml(bar.skipReason || '-')}</td>
      </tr>
    `).join('');
  } else {
    els.historyPagination.style.display = 'none';
    els.historyTable.innerHTML = '<tr><td class="empty-row" colspan="8">No history loaded yet.</td></tr>';
  }
}

function renderSimulation() {
  const allRows = Array.isArray(state.savedTrades) ? state.savedTrades : [];
  const trades = allRows.filter((t) => t.entryPrice != null && t.exitPrice != null && t.type);
  const wins = trades.filter((trade) => trade.grossPnl > 0);
  const losses = trades.filter((trade) => trade.grossPnl < 0);
  const pnl = trades.reduce((sum, trade) => sum + trade.grossPnl, 0);
  const best = wins.length ? Math.max(...wins.map((trade) => trade.grossPnl)) : null;
  const worst = losses.length ? Math.min(...losses.map((trade) => trade.grossPnl)) : null;
  const avgW = wins.length ? wins.reduce((sum, trade) => sum + trade.grossPnl, 0) / wins.length : 0;
  const avgL = losses.length ? Math.abs(losses.reduce((sum, trade) => sum + trade.grossPnl, 0) / losses.length) : 0;

  const metaText = state.simParams ? state.simParams.instrument + ' | ' + state.simParams.slen + '/' + state.simParams.llen : 'No simulation yet';
  if (els.resultMeta) els.resultMeta.textContent = metaText;
  if (els.resultMetaInline) els.resultMetaInline.textContent = metaText;
  els.statTrades.textContent = String(trades.length);
  els.statWinRate.textContent = trades.length ? Math.round((wins.length / trades.length) * 100) + '%' : '-';
  els.statPnl.textContent = trades.length ? formatCurrency(pnl) : '-';
  els.statPnl.className = pnl >= 0 ? 'positive' : 'negative';
  els.statBest.textContent = best !== null ? formatCurrency(best) : '-';
  els.statWorst.textContent = worst !== null ? formatCurrency(worst) : '-';
  const rr = avgL > 0 ? (avgW / avgL) : 0;
  els.statRR.textContent = trades.length ? (avgL > 0 ? Math.round((1 / (1 + rr)) * 100) + '%' : '0%') : '-';

  renderActivePosition();
  els.simBtn.textContent = state.simActive ? 'Stop Simulation' : 'Run Simulation';
  els.simBtn.classList.toggle('stop', state.simActive);
}

function renderActivePosition() {
  const pos = state.simPosition;
  const card = els.activeTradeCard;
  if (!pos) {
    if (card) card.style.display = 'none';
    return;
  }
  if (card) card.style.display = 'block';
  const unrealized = (pos.lastPrice - pos.entry) * state.simParams.lotSize;
  els.activeTradeGrid.innerHTML = [
    cell('Type', pos.type),
    cell('Instrument', pos.instrument),
    cell('Entry', formatNumber(pos.entry)),
    cell('Live', formatNumber(pos.lastPrice)),
    cell('SL', formatNumber(pos.curSL)),
    cell('Target', formatNumber(pos.tgt)),
    cell('Contract', pos.contract || '-'),
    cell('Unrealized', formatCurrency(unrealized), unrealized >= 0 ? 'positive' : 'negative'),
  ].join('');
}

function renderSavedTrades() {
  const allRows = Array.isArray(state.savedTrades) ? state.savedTrades.slice().reverse() : [];
  const rows = allRows.filter((t) => t.entryPrice != null && t.exitPrice != null && t.type);

  if (els.savedTradesMeta) els.savedTradesMeta.textContent = rows.length + ' trade' + (rows.length !== 1 ? 's' : '') + ' saved';

  const totalItems = rows.length;
  if (totalItems > 0) {
    els.tradesPagination.style.display = 'flex';
    const totalPages = Math.ceil(totalItems / state.tradesPageSize);
    if (state.tradesPage > totalPages) state.tradesPage = totalPages;
    if (state.tradesPage < 1) state.tradesPage = 1;

    els.tradesPageInfo.textContent = `Page ${state.tradesPage} of ${totalPages}`;
    els.tradesPrevBtn.disabled = state.tradesPage === 1;
    els.tradesNextBtn.disabled = state.tradesPage === totalPages;

    const startIdx = (state.tradesPage - 1) * state.tradesPageSize;
    const endIdx = startIdx + state.tradesPageSize;
    const pageRows = rows.slice(startIdx, endIdx);

    els.savedTradesTable.innerHTML = pageRows.map((trade) => {
      const pnl = trade.grossPnl != null ? trade.grossPnl : 0;
      const trailSL = trade.trailSL != null ? trade.trailSL : null;
      const symbolLabel = formatTradeSymbol(trade);
      trade.contract = symbolLabel;
      return `
      <tr>
        <td class="mono trade-symbol">${escapeHtml(trade.contract || trade.instrument || '—')}</td>
        <td>${signalPill(trade.type)}</td>
        <td class="mono">₹${formatNumber(trade.entryPrice)}</td>
        <td class="mono">₹${formatNumber(trade.exitPrice)}</td>
        <td class="mono sl-col">₹${formatNumber(trade.sl)}</td>
        <td class="mono tgt-col">₹${formatNumber(trade.tgt)}</td>
        <td class="mono">${trade.lotSize || '—'}</td>
        <td class="trail-col">${trailSL != null ? '<span class="trail-check">✓</span><br><span class="mono trail-price">₹' + formatNumber(trailSL) + '</span>' : '<span class="muted-dash">—</span>'}</td>
        <td>${reasonPill(trade.reason)}</td>
        <td class="mono ${pnl >= 0 ? 'positive' : 'negative'}">${pnl >= 0 ? '+' : ''}${formatCurrency(pnl)}</td>
        <td class="mono time-col">${formatTradeTime(trade.entryTs)}</td>
      </tr>`;
    }).join('');
  } else {
    els.tradesPagination.style.display = 'none';
    els.savedTradesTable.innerHTML = '<tr><td class="empty-row" colspan="11">No saved trades yet.</td></tr>';
  }
}

function renderBadge() {
  els.liveBadge.textContent = state.simActive ? 'LIVE' : 'READY';
  els.liveBadge.classList.toggle('live', state.simActive);
  els.liveBadge.classList.toggle('stopped', !state.simActive);
}

function setStatus(message, isError = false, isSuccess = false) {
  if (!message) {
    els.statusBox.style.display = 'none';
    els.statusBox.textContent = '';
    return;
  }
  els.statusBox.textContent = message;
  els.statusBox.className = 'status-card';
  if (isError) els.statusBox.classList.add('error');
  if (isSuccess) els.statusBox.classList.add('success');
  els.statusBox.style.display = 'flex';
}

function persistState() {
  const payload = {
    version: 3,
    tf: els.timeframe.value,
    shortLen: els.shortLen.value,
    longLen: els.longLen.value,
    refreshInterval: els.refreshInterval.value,
    simFields: {
      instrument: els.inpInstrument.value,
      sl: els.inpSL.value,
      target: els.inpTarget.value,
      trailTrigger: els.inpTrailTrigger.value,
      trailLock: els.inpTrailLock.value,
      lotSize: els.inpLotSize.value,
      delta: els.inpDelta.value,
      simShortLen: els.simShortLen.value,
      simLongLen: els.simLongLen.value,
      minQuality: els.inpMinQuality.value,
      sidewaysFilter: els.inpSidewaysFilter.checked,
      confirmCandle: els.inpConfirmCandle.checked,
    },
    runtime: {
      tf: state.tf,
      dualData: state.dualData,
      simActive: state.simActive,
      simStartTime: state.simStartTime,
      simSessionId: state.simSessionId,
      simParams: state.simParams,
      simPosition: state.simPosition,
      simTrades: state.simTrades,
      simLastTs: state.simLastTs,
      lastSavedTradeCount: state.lastSavedTradeCount,
      savedTrades: state.savedTrades,
      manualSessionPause: state.manualSessionPause,
    },
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

function restoreState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      applyDefaultFormValues();
      return;
    }
    const saved = JSON.parse(raw);
    els.shortLen.value = saved.shortLen || DEFAULTS.shortLen;
    els.longLen.value = saved.longLen || DEFAULTS.longLen;
    els.timeframe.value = saved.tf || DEFAULTS.timeframe;
    els.refreshInterval.value = saved.refreshInterval || DEFAULTS.refreshInterval;
    if (saved.simFields) {
      els.inpInstrument.value = saved.simFields.instrument || DEFAULTS.instrument;
      els.inpSL.value = saved.simFields.sl || DEFAULTS.sl;
      els.inpTarget.value = saved.simFields.target || DEFAULTS.target;
      els.inpTrailTrigger.value = saved.simFields.trailTrigger || DEFAULTS.trailTrigger;
      els.inpTrailLock.value = saved.simFields.trailLock || DEFAULTS.trailLock;
      els.inpLotSize.value = saved.simFields.lotSize || DEFAULTS.lotSize;
      els.inpDelta.value = saved.simFields.delta || DEFAULTS.delta;
      els.simShortLen.value = saved.simFields.simShortLen || els.shortLen.value;
      els.simLongLen.value = saved.simFields.simLongLen || els.longLen.value;
      els.inpMinQuality.value = saved.simFields.minQuality || DEFAULTS.minQuality;
      els.inpSidewaysFilter.checked = saved.simFields.sidewaysFilter === true;
      els.inpConfirmCandle.checked = saved.simFields.confirmCandle === true;
    } else {
      applyDefaultFormValues();
    }
    if (saved.runtime) {
      state.tf = saved.runtime.tf || els.timeframe.value;
      state.dualData = saved.runtime.dualData || null;
      state.simParams = saved.runtime.simParams || null;
      state.simSessionId = saved.runtime.simSessionId || null;
      state.simTrades = Array.isArray(saved.runtime.simTrades) ? saved.runtime.simTrades : [];
      state.simLastTs = saved.runtime.simLastTs || null;
      state.lastSavedTradeCount = saved.runtime.lastSavedTradeCount || 0;
      state.savedTrades = Array.isArray(saved.runtime.savedTrades) ? saved.runtime.savedTrades : [];
      state.manualSessionPause = Boolean(saved.runtime.manualSessionPause);

      // Validate runtime properties against today's date in IST
      const todayStr = new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(new Date());
      const savedStartTime = saved.runtime.simStartTime || null;
      let isToday = false;
      if (savedStartTime) {
        const startDate = asDate(savedStartTime);
        const startDateStr = startDate ? new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(startDate) : '';
        isToday = (startDateStr === todayStr);
      }

      if (isToday) {
        state.simActive = saved.runtime.simActive || false;
        state.simStartTime = savedStartTime;
        const pos = saved.runtime.simPosition || null;
        if (pos && pos.entryTs) {
          const posDate = asDate(pos.entryTs);
          const posDateStr = posDate ? new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(posDate) : '';
          state.simPosition = (posDateStr === todayStr) ? pos : null;
        } else {
          state.simPosition = null;
        }
      } else {
        state.simActive = false;
        state.simStartTime = null;
        state.simPosition = null;
      }
    }
  } catch (_) {
    localStorage.removeItem(STORAGE_KEY);
    applyDefaultFormValues();
  }
}

function applyDefaultFormValues() {
  els.timeframe.value = DEFAULTS.timeframe;
  els.refreshInterval.value = DEFAULTS.refreshInterval;
  els.inpInstrument.value = DEFAULTS.instrument;
  els.inpSL.value = DEFAULTS.sl;
  els.inpTarget.value = DEFAULTS.target;
  els.inpTrailTrigger.value = DEFAULTS.trailTrigger;
  els.inpTrailLock.value = DEFAULTS.trailLock;
  els.inpLotSize.value = DEFAULTS.lotSize;
  els.inpDelta.value = DEFAULTS.delta;
  els.shortLen.value = DEFAULTS.shortLen;
  els.longLen.value = DEFAULTS.longLen;
  els.simShortLen.value = DEFAULTS.shortLen;
  els.simLongLen.value = DEFAULTS.longLen;
  els.inpMinQuality.value = DEFAULTS.minQuality;
  els.inpSidewaysFilter.checked = DEFAULTS.sidewaysFilter;
  els.inpConfirmCandle.checked = DEFAULTS.confirmCandle;
}

function syncFormToState() {
  updateInstrumentMode();
  renderBadge();
  renderSessionInfo();
}

function resetSimulationForm() {
  stopPolling();
  state.simActive = false;
  state.simStartTime = null;
  state.simSessionId = null;
  state.simParams = null;
  state.simPosition = null;
  state.simTrades = [];
  state.simLastTs = null;
  state.lastSavedTradeCount = 0;
  state.manualSessionPause = false;
  state.lastTradeType = null;
  applyDefaultFormValues();
  updateInstrumentMode();
  renderSimulation();
  renderSessionInfo();
  persistState();
  setStatus('Simulation form reset with VMA 5/9 and the requested risk defaults.', false, true);
}

// ── EOD Auto Square-Off ─────────────────────────────────────────────────────
// Mirrors handle_eod_auto_controls() from the Python backend exactly:
//   15:20–15:25 IST → force-exit any active position once per day
//   15:25+ IST      → stop the simulation once per day
async function handleEodAutoControls() {
  const parts = getIndiaDateParts();
  const hhmm = Number(parts.hour) * 100 + Number(parts.minute);
  // Build today's IST date string 'YYYY-MM-DD'
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: INDIA_TIMEZONE }).format(new Date());

  // ── 15:20–15:25: force-exit open trade once per day ─────────────────────
  if (hhmm >= 1520 && hhmm < 1525 && state.eodExitDoneDate !== today) {
    state.eodExitDoneDate = today;   // guard first – prevents re-entry even if await takes time
    if (state.simPosition) {
      const reason = 'EOD_AUTO';
      if (!state.simParams) {
        state.simParams = {
          lotSize: parseInt(els.inpLotSize.value, 10) || 65,
          delta: parseFloat(els.inpDelta.value) || 0.5,
          instrument: els.inpInstrument.value || 'options',
        };
      }
      completeTrade(state.simPosition.lastPrice || state.simPosition.entry, latestTimestamp(), reason);
      renderAll();
      await persistCompletedTrades();
      persistState();
      setStatus('⏰ EOD Auto Square-Off: position closed at 15:20 IST.', false, true);
    }
  }

  // ── 15:25+: stop simulation once per day ────────────────────────────────
  if (hhmm >= 1525 && state.eodStopDoneDate !== today) {
    state.eodStopDoneDate = today;   // guard first
    if (state.simActive) {
      stopSimulation({ reason: 'EOD' });
      setStatus('🔴 Simulation auto-stopped at 15:25 IST. Will resume next session.', false, true);
    }
    persistState();
  }
}

function startMarketClock() {
  stopMarketClock();
  renderSessionInfo();
  state.clockTimer = window.setInterval(() => {
    renderSessionInfo();
    syncMarketSession();
    handleEodAutoControls();
  }, 1000);
}

function stopMarketClock() {
  if (state.clockTimer) {
    window.clearInterval(state.clockTimer);
    state.clockTimer = null;
  }
}

function syncMarketSession(isInitial = false) {
  const session = getMarketSessionState();
  if (session.isOpen) {
    if (state.simActive && !state.pollTimer) {
      startPolling();
    } else if (!state.simActive && !state.manualSessionPause) {
      const started = startSimulation({ manual: false });
      if (!started && isInitial) {
        setStatus('Auto-start skipped because the current inputs are invalid.', true);
      }
    }
  } else {
    // Keep simulation and open trades active even after session hours as requested!
    if (state.simActive && !state.pollTimer) {
      startPolling();
    } else if (isInitial) {
      persistState();
    }
  }
  renderSessionInfo(session);
}

function renderSessionInfo(providedSession) {
  const session = providedSession || getMarketSessionState();
  els.marketClock.textContent = session.clockLabel;
  els.sessionStatus.textContent = session.statusLabel;
  els.sessionWindow.textContent = '09:16 - 15:30 IST';
}

function getMarketSessionState() {
  const parts = getIndiaDateParts();
  const hour = Number(parts.hour);
  const minute = Number(parts.minute);
  const totalMinutes = hour * 60 + minute;
  const weekday = parts.weekday;
  const isWeekend = weekday === 'Sat' || weekday === 'Sun';

  let statusLabel = 'Waiting for market open';
  let isOpen = false;

  if (isWeekend) {
    statusLabel = 'Market closed for weekend';
  } else if (totalMinutes < MARKET_WINDOW.startMinutes) {
    statusLabel = 'Auto-start at 09:16 IST';
  } else if (totalMinutes >= MARKET_WINDOW.endMinutes) {
    statusLabel = 'Session closed at 15:30 IST';
  } else {
    isOpen = true;
    statusLabel = state.manualSessionPause ? 'Paused manually until next session' : 'Live auto-run window active';
  }

  return {
    isOpen,
    clockLabel: parts.hour + ':' + parts.minute + ':' + parts.second + ' IST',
    statusLabel,
  };
}

function isMarketOpen() {
  return getMarketSessionState().isOpen;
}

function getIndiaDateParts() {
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: INDIA_TIMEZONE,
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const parts = formatter.formatToParts(new Date());
  return parts.reduce((acc, part) => {
    if (part.type !== 'literal') acc[part.type] = part.value;
    return acc;
  }, {});
}

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/service-worker.js').catch(() => {});
  }
}

function signalPill(value) {
  const normalized = String(value || 'NONE').toLowerCase();
  return '<span class="signal-pill ' + normalized + '">' + escapeHtml(String(value || 'NONE')) + '</span>';
}

function modePill(value) {
  const normalized = String(value || 'MANUAL').toLowerCase();
  return '<span class="mode-pill ' + normalized + '">' + escapeHtml(String(value || 'MANUAL')) + '</span>';
}

function reasonPill(value) {
  const labelMap = {
    'SL':          { label: 'SL_HIT',       cls: 'sl' },
    'TRAILING_SL': { label: 'TRAIL_SL_HIT', cls: 'trailing_sl' },
    'TARGET':      { label: 'TARGET',       cls: 'target' },
    'EOD':         { label: 'EOD',          cls: 'eod' },
    'EOD_AUTO':    { label: 'EOD AUTO',     cls: 'eod' },
    'MANUAL':      { label: 'MANUAL',       cls: 'manual_exit' },
  };
  const entry = labelMap[String(value)] || { label: String(value || 'EOD'), cls: 'eod' };
  return '<span class="reason-pill ' + entry.cls + '">' + escapeHtml(entry.label) + '</span>';
}

function cell(key, value, extraClass = '') {
  return '<div class="active-cell"><span class="key">' + escapeHtml(key) + '</span><span class="value ' + extraClass + '">' + escapeHtml(String(value)) + '</span></div>';
}

function formatCurrency(value) {
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(value || 0);
}

function formatNumber(value) {
  if (value == null || value === '') return '-';
  return Number(value).toFixed(2);
}

function formatDateTime(value) {
  const date = asDate(value);
  return date ? date.toLocaleString() : '-';
}

function formatTradeTime(value) {
  const date = asDate(value);
  if (!date) return '-';
  const day = date.getDate();
  const month = date.toLocaleString('en-IN', { month: 'short', timeZone: 'Asia/Kolkata' });
  const time = date.toLocaleString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' });
  return day + '<br>' + month + '.<br>' + time;
}

function formatTradeSymbol(trade) {
  return trade?.tradingsymbol || trade?.contract || trade?.instrument || '-';
}

function formatOptionExpiryLabel(value) {
  if (!value) return '';

  const date = asDate(value);
  if (date) {
    const day = date.toLocaleString('en-IN', { day: 'numeric', timeZone: INDIA_TIMEZONE });
    const month = date.toLocaleString('en-IN', { month: 'short', timeZone: INDIA_TIMEZONE });
    return `${day} ${month}`;
  }

  const raw = String(value).trim().toUpperCase();
  const match = raw.match(/^(\d{1,2})([A-Z]{3})(\d{2}|\d{4})?$/);
  if (!match) return '';

  const day = String(parseInt(match[1], 10));
  const month = match[2].charAt(0) + match[2].slice(1).toLowerCase();
  return `${day} ${month}`;
}

function extractExpiryFromContract(contract) {
  const raw = String(contract || '').trim().toUpperCase();
  const match = raw.match(/^NIFTY(\d{1,2}[A-Z]{3}\d{2,4})\d+(CE|PE)$/);
  return match ? match[1] : '';
}

function latestTimestamp() {
  const history = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history : [];
  return history.length ? history[history.length - 1].timestamp : new Date().toISOString();
}

function asDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function timeframeToMs(value) {
  const match = String(value || '').trim().match(/^(\d+)\s*(min|m|hour|hr|h|day|d)$/i);
  if (!match) return 60 * 1000;
  const amount = parseInt(match[1], 10);
  const unit = match[2].toLowerCase();
  if (unit === 'day' || unit === 'd') return amount * 24 * 60 * 60 * 1000;
  if (unit === 'hour' || unit === 'hr' || unit === 'h') return amount * 60 * 60 * 1000;
  return amount * 60 * 1000;
}

function getSimulationLookbackMs(timeframe, confirmCandle) {
  const timeframeMs = timeframeToMs(timeframe);
  const barsNeeded = confirmCandle ? 3 : 2;
  return Math.max(90 * 1000, timeframeMs * barsNeeded);
}

function hashString(value) {
  let hash = 0;
  for (let index = 0; index < value.length; index++) {
    hash = ((hash << 5) - hash) + value.charCodeAt(index);
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

function round(value) {
  return Math.round((Number(value) || 0) * 100) / 100;
}

function formatReasonLabel(value) {
  return String(value || '')
    .replaceAll('_', ' ')
    .toLowerCase()
    .replace(/\b[a-z]/g, (char) => char.toUpperCase());
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
