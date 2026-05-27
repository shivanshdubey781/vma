'use strict';

const STORAGE_KEY = 'vma_dashboard_state_v2';
const ASSET_CACHE_VERSION = '2026-05-27-1';
const state = {
  tf: '5min',
  dualData: null,
  simActive: false,
  simStartTime: null,
  simParams: null,
  simPosition: null,
  simTrades: [],
  simLastTs: null,
  lastSavedTradeCount: 0,
  pollTimer: null,
  savedTrades: [],
};

const els = {};

document.addEventListener('DOMContentLoaded', async () => {
  bindElements();
  bindEvents();
  registerServiceWorker();
  restoreState();
  syncFormToState();
  renderAll();
  await Promise.all([loadDashboard(), loadSavedTrades()]);
  if (state.simActive) {
    startPolling();
  }
});

function bindElements() {
  [
    'liveBadge', 'statusBox', 'calcBtn', 'timeframe', 'shortLen', 'longLen', 'refreshInterval',
    'heroSignal', 'heroTimestamp', 'heroVma', 'heroPosition', 'heroClose', 'heroRsi', 'heroQuality', 'heroSideways',
    'historyMeta', 'historyTable', 'resultMeta', 'statTrades', 'statWinRate', 'statPnl', 'statBest', 'statWorst', 'statRR',
    'activeTradeGrid', 'simBtn', 'resetBtn', 'inpInstrument', 'inpSL', 'inpTarget', 'inpTrailTrigger', 'inpTrailLock',
    'inpLotSize', 'inpDelta', 'simShortLen', 'simLongLen', 'inpMinQuality', 'inpSidewaysFilter', 'inpConfirmCandle',
    'tradesTable', 'savedTradesTable', 'savedTradesMeta'
  ].forEach((id) => {
    els[id] = document.getElementById(id);
  });
}

function bindEvents() {
  els.calcBtn.addEventListener('click', () => loadDashboard(true));
  els.simBtn.addEventListener('click', () => state.simActive ? stopSimulation() : startSimulation());
  els.resetBtn.addEventListener('click', resetSimulationForm);
  els.inpInstrument.addEventListener('change', updateInstrumentMode);
  ['shortLen', 'simShortLen'].forEach((id) => els[id].addEventListener('input', syncLengthFields));
  ['longLen', 'simLongLen'].forEach((id) => els[id].addEventListener('input', syncLengthFields));
  ['timeframe', 'refreshInterval', 'inpSL', 'inpTarget', 'inpTrailTrigger', 'inpTrailLock', 'inpLotSize', 'inpDelta', 'inpMinQuality', 'inpSidewaysFilter', 'inpConfirmCandle'].forEach((id) => {
    els[id].addEventListener('change', persistState);
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

function startSimulation() {
  const simParams = readSimulationParams();
  if (!simParams) return;
  state.simActive = true;
  state.simStartTime = new Date(Date.now() - 90 * 1000).toISOString();
  state.simParams = simParams;
  state.simPosition = null;
  state.simTrades = [];
  state.simLastTs = null;
  state.lastSavedTradeCount = 0;
  renderSimulation();
  setStatus('Simulation started. Watching fresh crossover bars.', false, true);
  persistState();
  startPolling();
}

function stopSimulation() {
  state.simActive = false;
  stopPolling();
  closeOpenPosition('EOD');
  persistCompletedTrades();
  renderSimulation();
  setStatus('Simulation stopped. Trades persisted and UI state saved locally.', false, true);
  persistState();
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
    setStatus('Live polling error: ' + error.message, true);
  }
}

async function processBar(bar) {
  if (state.simPosition) {
    await updateOpenPosition(bar);
    return;
  }

  const signal = state.simParams.confirmCandle ? bar.confirm_signal : bar.signal;
  if (!['CE', 'PE'].includes(signal)) return;
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
}

function closeOpenPosition(reason) {
  const pos = state.simPosition;
  if (!pos) return;
  completeTrade(pos.lastPrice || pos.entry, latestTimestamp(), reason);
}

function completeTrade(exitPrice, exitTs, reason) {
  const trade = buildTrade(state.simPosition, exitPrice, exitTs, reason);
  state.simTrades.push(trade);
  state.simPosition = null;
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

  return {
    type: position.type,
    instrument: position.instrument,
    contract: position.contract || null,
    entryTs: position.entryTs,
    entryPrice: round(position.entry),
    exitTs: exitTs,
    exitPrice: round(exitPrice),
    sl: round(position.initSL),
    tgt: round(position.tgt),
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

async function loadSavedTrades() {
  try {
    const response = await fetch('/api/vma-trades?limit=25', { cache: 'no-store' });
    const json = await response.json();
    if (!json.ok) throw new Error(json.error || 'Unable to fetch saved trades');
    state.savedTrades = json.trades || [];
    els.savedTradesMeta.textContent = (json.count || 0) + ' trades in MongoDB';
    renderSavedTrades();
  } catch (error) {
    els.savedTradesMeta.textContent = 'Sync failed';
    setStatus('Saved trades sync failed: ' + error.message, true);
  }
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

function renderAll() {
  renderDashboard();
  renderSimulation();
  renderSavedTrades();
  renderBadge();
}

function renderDashboard() {
  const current = state.dualData && state.dualData.current ? state.dualData.current : null;
  els.heroSignal.textContent = current ? (current.signal || current.confirm_signal || 'NONE') : '-';
  els.heroTimestamp.textContent = current ? formatDateTime(current.timestamp) : 'Waiting for data';
  els.heroVma.textContent = current ? formatNumber(current.short_vma) + ' / ' + formatNumber(current.long_vma) : '-';
  els.heroPosition.textContent = current ? (current.position || '-') : '-';
  els.heroClose.textContent = current ? 'Rs ' + formatNumber(current.close) : '-';
  els.heroRsi.textContent = current ? 'RSI ' + formatNumber(current.rsi) : '-';
  els.heroQuality.textContent = current ? String(current.quality ?? '-') : '-';
  els.heroSideways.textContent = current ? (current.is_sideways ? 'Sideways' : 'Trending') : '-';
  els.historyMeta.textContent = state.dualData ? state.dualData.total_bars + ' bars loaded' : 'No bars loaded';

  const rows = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history.slice(-20).reverse() : [];
  els.historyTable.innerHTML = rows.length ? rows.map((bar) => `
    <tr>
      <td>${formatDateTime(bar.timestamp)}</td>
      <td>${formatNumber(bar.close)}</td>
      <td>${formatNumber(bar.short_vma)}</td>
      <td>${formatNumber(bar.long_vma)}</td>
      <td>${signalPill(bar.signal)}</td>
      <td>${signalPill(bar.confirm_signal)}</td>
      <td>${bar.quality ?? '-'}</td>
    </tr>
  `).join('') : '<tr><td class="empty-row" colspan="7">No history loaded yet.</td></tr>';
}

function renderSimulation() {
  const trades = state.simTrades;
  const wins = trades.filter((trade) => trade.grossPnl > 0);
  const losses = trades.filter((trade) => trade.grossPnl < 0);
  const pnl = trades.reduce((sum, trade) => sum + trade.grossPnl, 0);
  const best = wins.length ? Math.max(...wins.map((trade) => trade.grossPnl)) : null;
  const worst = losses.length ? Math.min(...losses.map((trade) => trade.grossPnl)) : null;
  const avgW = wins.length ? wins.reduce((sum, trade) => sum + trade.grossPnl, 0) / wins.length : 0;
  const avgL = losses.length ? Math.abs(losses.reduce((sum, trade) => sum + trade.grossPnl, 0) / losses.length) : 0;

  els.resultMeta.textContent = state.simParams ? state.simParams.instrument + ' | ' + state.simParams.slen + '/' + state.simParams.llen : 'No simulation yet';
  els.statTrades.textContent = String(trades.length);
  els.statWinRate.textContent = trades.length ? Math.round((wins.length / trades.length) * 100) + '%' : '-';
  els.statPnl.textContent = trades.length ? formatCurrency(pnl) : '-';
  els.statPnl.className = pnl >= 0 ? 'positive' : 'negative';
  els.statBest.textContent = best !== null ? formatCurrency(best) : '-';
  els.statWorst.textContent = worst !== null ? formatCurrency(worst) : '-';
  els.statRR.textContent = avgL > 0 ? (avgW / avgL).toFixed(2) : '-';

  els.tradesTable.innerHTML = trades.length ? trades.map((trade, index) => `
    <tr>
      <td>${index + 1}</td>
      <td>${signalPill(trade.type)}</td>
      <td>${formatDateTime(trade.entryTs)}</td>
      <td>${formatNumber(trade.entryPrice)}</td>
      <td>${formatNumber(trade.exitPrice)}</td>
      <td>${formatNumber(trade.sl)}</td>
      <td>${formatNumber(trade.tgt)}</td>
      <td>${trade.lotSize}</td>
      <td class="${trade.grossPnl >= 0 ? 'positive' : 'negative'}">${formatCurrency(trade.grossPnl)}</td>
      <td>${reasonPill(trade.reason)}</td>
    </tr>
  `).join('') : '<tr><td class="empty-row" colspan="10">No completed trades in the current browser session.</td></tr>';

  renderActivePosition();
  els.simBtn.textContent = state.simActive ? 'Stop Simulation' : 'Run Simulation';
  els.simBtn.classList.toggle('stop', state.simActive);
}

function renderActivePosition() {
  const pos = state.simPosition;
  if (!pos) {
    els.activeTradeGrid.innerHTML = '<div class="active-cell"><span class="key">State</span><span class="value">No active position</span></div>';
    return;
  }

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
  const rows = Array.isArray(state.savedTrades) ? state.savedTrades.slice().reverse().slice(0, 10) : [];
  els.savedTradesTable.innerHTML = rows.length ? rows.map((trade) => `
    <tr>
      <td>${formatDateTime(trade.exitTs || trade.saved_at)}</td>
      <td>${signalPill(trade.type || 'NONE')}</td>
      <td>${trade.reason ? reasonPill(trade.reason) : '-'}</td>
      <td class="${(trade.grossPnl || 0) >= 0 ? 'positive' : 'negative'}">${trade.grossPnl != null ? formatCurrency(trade.grossPnl) : '-'}</td>
    </tr>
  `).join('') : '<tr><td class="empty-row" colspan="4">No saved trades found yet.</td></tr>';
}

function renderBadge() {
  els.liveBadge.textContent = state.simActive ? 'LIVE' : 'READY';
  els.liveBadge.classList.toggle('live', state.simActive);
  els.liveBadge.classList.toggle('stopped', !state.simActive);
}

function setStatus(message, isError = false, isSuccess = false) {
  els.statusBox.textContent = message;
  els.statusBox.className = 'status-card';
  if (isError) els.statusBox.classList.add('error');
  if (isSuccess) els.statusBox.classList.add('success');
}

function persistState() {
  const payload = {
    version: 2,
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
      simParams: state.simParams,
      simPosition: state.simPosition,
      simTrades: state.simTrades,
      simLastTs: state.simLastTs,
      lastSavedTradeCount: state.lastSavedTradeCount,
      savedTrades: state.savedTrades,
    },
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

function restoreState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.shortLen) els.shortLen.value = saved.shortLen;
    if (saved.longLen) els.longLen.value = saved.longLen;
    if (saved.tf) els.timeframe.value = saved.tf;
    if (saved.refreshInterval) els.refreshInterval.value = saved.refreshInterval;
    if (saved.simFields) {
      els.inpInstrument.value = saved.simFields.instrument || 'options';
      els.inpSL.value = saved.simFields.sl || '20';
      els.inpTarget.value = saved.simFields.target || '40';
      els.inpTrailTrigger.value = saved.simFields.trailTrigger || '15';
      els.inpTrailLock.value = saved.simFields.trailLock || '10';
      els.inpLotSize.value = saved.simFields.lotSize || '65';
      els.inpDelta.value = saved.simFields.delta || '0.5';
      els.simShortLen.value = saved.simFields.simShortLen || els.shortLen.value;
      els.simLongLen.value = saved.simFields.simLongLen || els.longLen.value;
      els.inpMinQuality.value = saved.simFields.minQuality || '2';
      els.inpSidewaysFilter.checked = saved.simFields.sidewaysFilter !== false;
      els.inpConfirmCandle.checked = saved.simFields.confirmCandle !== false;
    }
    if (saved.runtime) {
      state.tf = saved.runtime.tf || els.timeframe.value;
      state.dualData = saved.runtime.dualData || null;
      state.simActive = saved.runtime.simActive || false;
      state.simStartTime = saved.runtime.simStartTime || null;
      state.simParams = saved.runtime.simParams || null;
      state.simPosition = saved.runtime.simPosition || null;
      state.simTrades = Array.isArray(saved.runtime.simTrades) ? saved.runtime.simTrades : [];
      state.simLastTs = saved.runtime.simLastTs || null;
      state.lastSavedTradeCount = saved.runtime.lastSavedTradeCount || 0;
      state.savedTrades = Array.isArray(saved.runtime.savedTrades) ? saved.runtime.savedTrades : [];
    }
  } catch (_) {
    localStorage.removeItem(STORAGE_KEY);
  }
}

function syncFormToState() {
  updateInstrumentMode();
  renderBadge();
}

function resetSimulationForm() {
  stopPolling();
  state.simActive = false;
  state.simStartTime = null;
  state.simParams = null;
  state.simPosition = null;
  state.simTrades = [];
  state.simLastTs = null;
  state.lastSavedTradeCount = 0;
  els.inpInstrument.value = 'options';
  els.inpSL.value = '20';
  els.inpTarget.value = '40';
  els.inpTrailTrigger.value = '15';
  els.inpTrailLock.value = '10';
  els.inpLotSize.value = '65';
  els.inpDelta.value = '0.5';
  els.shortLen.value = '9';
  els.longLen.value = '21';
  els.simShortLen.value = '9';
  els.simLongLen.value = '21';
  els.inpMinQuality.value = '2';
  els.inpSidewaysFilter.checked = true;
  els.inpConfirmCandle.checked = true;
  updateInstrumentMode();
  renderSimulation();
  persistState();
  setStatus('Simulation form reset. Cached browser state updated.', false, true);
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

function reasonPill(value) {
  const normalized = String(value || 'EOD').toLowerCase();
  return '<span class="reason-pill ' + normalized + '">' + escapeHtml(String(value || 'EOD')) + '</span>';
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

function latestTimestamp() {
  const history = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history : [];
  return history.length ? history[history.length - 1].timestamp : new Date().toISOString();
}

function asDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function round(value) {
  return Math.round((Number(value) || 0) * 100) / 100;
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
