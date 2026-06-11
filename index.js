'use strict';

// ─── Storage key bumped to v4 so stale localStorage is auto-cleared ───────────
const STORAGE_KEY    = 'vma_dashboard_state_v4';
const INDIA_TIMEZONE = 'Asia/Kolkata';
const MARKET_WINDOW  = Object.freeze({ startMinutes: 9 * 60 + 16, endMinutes: 15 * 60 + 30 });
const DEFAULTS = Object.freeze({
  timeframe: '5min', shortLen: '5', longLen: '9', refreshInterval: '10000',
  instrument: 'options', sl: '40', target: '60', trailTrigger: '25',
  trailLock: '15', lotSize: '65', delta: '0.5', minQuality: '2',
  sidewaysFilter: false, confirmCandle: false,
});

// ─── Display-only state ───────────────────────────────────────────────────────
// sim is the server snapshot polled from GET /api/sim-state.
// Nothing simulation-related is computed in the browser anymore.
const state = {
  tf:             DEFAULTS.timeframe,
  dualData:       null,   // from /api/dual-vma  (chart / history)
  sim:            null,   // from /api/sim-state  (single source of truth)
  savedTrades:    [],     // from /api/vma-trades
  pollTimer:      null,
  clockTimer:     null,
  historyPage:    1,
  historyPageSize: 15,
  tradesPage:     1,
  tradesPageSize: 10,
};

const els = {};

// ─────────────────────────────────────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  bindElements();
  bindEvents();
  registerServiceWorker();
  restoreFormState();
  syncFormToState();
  renderAll();
  startMarketClock();
  await Promise.all([loadDashboard(), loadSavedTrades()]);
  // Sync server sim state and start polling immediately
  await pollSimState();
  startPolling();
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
    'tradesPagination', 'tradesPrevBtn', 'tradesNextBtn', 'tradesPageInfo',
  ].forEach((id) => { els[id] = document.getElementById(id); });
}

function bindEvents() {
  els.calcBtn.addEventListener('click', () => loadDashboard(true));

  // Start / Stop — send command to SERVER, not local state
  els.simBtn.addEventListener('click', () => {
    const sim = state.sim;
    if (sim && sim.active) {
      sendSimControl('stop');
    } else {
      sendSimControl('start');
    }
  });

  els.resetBtn.addEventListener('click', resetSimulationForm);
  els.inpInstrument.addEventListener('change', updateInstrumentMode);
  ['shortLen', 'simShortLen'].forEach((id) => els[id].addEventListener('input', syncLengthFields));
  ['longLen',  'simLongLen' ].forEach((id) => els[id].addEventListener('input', syncLengthFields));
  ['refreshInterval', 'inpSL', 'inpTarget', 'inpTrailTrigger', 'inpTrailLock',
   'inpLotSize', 'inpDelta', 'inpMinQuality', 'inpSidewaysFilter', 'inpConfirmCandle',
  ].forEach((id) => { els[id].addEventListener('change', persistFormState); });
  els.timeframe.addEventListener('change', handleTimeframeChange);
  els.closeActiveTradeBtn.addEventListener('click', closeActivePositionManually);

  els.historyPrevBtn.addEventListener('click', () => { if (state.historyPage > 1) { state.historyPage--; renderDashboard(); } });
  els.historyNextBtn.addEventListener('click', () => {
    const total = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history.length : 0;
    if (state.historyPage < Math.ceil(total / state.historyPageSize)) { state.historyPage++; renderDashboard(); }
  });
  els.tradesPrevBtn.addEventListener('click', () => { if (state.tradesPage > 1) { state.tradesPage--; renderSavedTrades(); } });
  els.tradesNextBtn.addEventListener('click', () => {
    const rows = Array.isArray(state.savedTrades) ? state.savedTrades.filter((t) => t.entryPrice != null && t.exitPrice != null && t.type) : [];
    if (state.tradesPage < Math.ceil(rows.length / state.tradesPageSize)) { state.tradesPage++; renderSavedTrades(); }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// SERVER-SIDE SIM CONTROL — POST /api/sim-control
// ─────────────────────────────────────────────────────────────────────────────
async function sendSimControl(action) {
  try {
    const body = { action };
    if (action === 'start') {
      const params = readSimulationParams();
      if (!params) return;   // validation failed, message already set
      body.params      = params;
      body.tf          = els.timeframe.value;
      body.refresh_ms  = parseInt(els.refreshInterval.value, 10) || 10000;
    }
    if (action === 'tf_switch') {
      body.tf = els.timeframe.value;
    }

    const resp = await fetch('/api/sim-control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const json = await resp.json();
    if (!json.ok) throw new Error(json.error || 'Control failed');

    // Immediately refresh state from server so UI updates without waiting for next poll
    await pollSimState();
    persistFormState();
    renderAll();
  } catch (err) {
    setStatus('Sim control error: ' + err.message, true);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// POLLING — GET /api/sim-state every N ms (replaces old pollAndProcess)
// ─────────────────────────────────────────────────────────────────────────────
async function pollSimState() {
  try {
    const resp = await fetch('/api/sim-state', { cache: 'no-store' });
    const json = await resp.json();
    if (!json.ok) throw new Error(json.error || 'sim-state fetch failed');
    state.sim = json.sim || null;

    // Also refresh dashboard VMA data so chart stays up to date
    if (state.sim && state.sim.tf) {
      const tf = state.sim.tf;
      if (tf !== els.timeframe.value) {
        els.timeframe.value = tf;
        state.tf = tf;
      }
    }

    // Refresh saved trades whenever a position just closed
    const simTrades = (state.sim && state.sim.trades) ? state.sim.trades : [];
    if (simTrades.length !== (state._lastSimTradeCount || 0)) {
      state._lastSimTradeCount = simTrades.length;
      await loadSavedTrades();
    }

    renderAll();
    if (state.sim && state.sim.status_msg) {
      setStatus(state.sim.status_msg, false, false);
    }
  } catch (err) {
    setStatus('Sim state poll error: ' + err.message, true);
  }
}

function startPolling() {
  stopPolling();
  const interval = parseInt(els.refreshInterval.value, 10) || 10000;
  state.pollTimer = window.setInterval(async () => {
    await pollSimState();
    // Also re-fetch dual-vma data for dashboard chart
    await loadDashboard(false);
  }, interval);
  renderBadge();
}

function stopPolling() {
  if (state.pollTimer) { window.clearInterval(state.pollTimer); state.pollTimer = null; }
  renderBadge();
}

// ─────────────────────────────────────────────────────────────────────────────
// Dashboard (VMA chart) — unchanged from original
// ─────────────────────────────────────────────────────────────────────────────
async function loadDashboard(showMessage = false) {
  const tf       = els.timeframe.value;
  const shortLen = parseInt(els.shortLen.value, 10);
  const longLen  = parseInt(els.longLen.value, 10);
  if (!Number.isFinite(shortLen) || !Number.isFinite(longLen) || shortLen >= longLen) {
    setStatus('Short VMA must be smaller than Long VMA.', true);
    return;
  }
  try {
    const params   = new URLSearchParams({ tf, short_len: String(shortLen), long_len: String(longLen) });
    const response = await fetch('/api/dual-vma?' + params.toString(), { cache: 'no-store' });
    const json     = await response.json();
    if (!json.ok) throw new Error(json.error || 'Unable to load VMA data');
    state.tf       = tf;
    state.dualData = json;
    renderDashboard();
    persistFormState();
    if (showMessage) setStatus('Dashboard refreshed with ' + json.total_bars + ' bars from ' + tf + '.', false, true);
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function loadSavedTrades() {
  try {
    const response = await fetch('/api/vma-trades?limit=50', { cache: 'no-store' });
    const json     = await response.json();
    if (!json.ok) throw new Error(json.error || 'Unable to fetch saved trades');
    state.savedTrades = json.trades || [];
    if (els.savedTradesMeta) els.savedTradesMeta.textContent = (json.count || 0) + ' trades saved';
    renderSavedTrades();
  } catch (error) {
    if (els.savedTradesMeta) els.savedTradesMeta.textContent = 'Sync failed';
    setStatus('Saved trades sync failed: ' + error.message, true);
  }
}

// Square-off: send command to server
async function closeActivePositionManually() {
  const sim = state.sim;
  if (!sim || !sim.position) return;
  if (!confirm('Are you sure you want to square off this active position manually?')) return;
  await sendSimControl('squareoff');
  await loadSavedTrades();
  setStatus('Position squared off manually.', false, true);
}

// ─────────────────────────────────────────────────────────────────────────────
// Timeframe change — tell server to switch + restart sim
// ─────────────────────────────────────────────────────────────────────────────
async function handleTimeframeChange() {
  persistFormState();
  await loadDashboard(false);
  const sim = state.sim;
  if (sim && sim.active) {
    await sendSimControl('tf_switch');
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Market session auto-start (UI only trigger — server is always the authority)
// ─────────────────────────────────────────────────────────────────────────────
function syncMarketSession(isInitial = false) {
  const session = getMarketSessionState();
  // If server sim is not active and market is open and we're not paused, auto-start
  const sim = state.sim;
  const serverActive = sim && sim.active;
  const manualPause  = sim && sim.manual_pause;
  if (session.isOpen && !serverActive && !manualPause) {
    sendSimControl('start').catch(() => {});
  }
  renderSessionInfo(session);
}

// ─────────────────────────────────────────────────────────────────────────────
// Rendering
// ─────────────────────────────────────────────────────────────────────────────
function renderAll() {
  renderDashboard();
  renderSimulation();
  renderSavedTrades();
  renderBadge();
  renderSessionInfo();
}

function renderDashboard() {
  const current = state.dualData && state.dualData.current ? state.dualData.current : null;
  const simParams = getDisplaySimParams();
  els.heroSignal.textContent    = current ? getEntrySignal(current, simParams) : '-';
  els.heroTimestamp.textContent = current ? formatDateTime(current.timestamp) : 'Waiting for data';
  els.heroVma.textContent       = current ? formatNumber(current.short_vma) + ' / ' + formatNumber(current.long_vma) : '-';
  els.heroPosition.textContent  = current ? (current.position || '-') : '-';
  els.heroClose.textContent     = current ? 'Rs ' + formatNumber(current.close) : '-';
  els.heroRsi.textContent       = current ? 'RSI ' + formatNumber(current.rsi) : '-';
  els.heroQuality.textContent   = current ? String(current.quality ?? '-') : '-';
  els.heroSideways.textContent  = current ? (current.is_sideways ? 'Sideways' : 'Trending') : '-';
  els.historyMeta.textContent   = state.dualData ? state.dualData.total_bars + ' bars loaded' : 'No bars loaded';

  const rawHistory = state.dualData && Array.isArray(state.dualData.history) ? state.dualData.history : [];
  const allHistory = analyzeHistoryDecisions(rawHistory, simParams).slice().reverse();
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
    const rows = allHistory.slice(startIdx, startIdx + state.historyPageSize);
    els.historyTable.innerHTML = rows.map((bar) => `
      <tr>
        <td>${formatDateTime(bar.timestamp)}</td>
        <td>${formatNumber(bar.close)}</td>
        <td>${formatNumber(bar.short_vma)}</td>
        <td>${formatNumber(bar.long_vma)}</td>
        <td>${signalPill(bar.signal)}</td>
        <td>${signalPill(bar.confirm_signal)}</td>
        <td>${bar.quality ?? '-'}</td>
        <td>${bar.is_sideways ? '<span class="sideways-pill">SW</span>' : '<span class="trending-pill">TR</span>'}</td>
        <td>${bar.skipReason || '-'}</td>
      </tr>`).join('');
  } else {
    if (els.historyPagination) els.historyPagination.style.display = 'none';
    els.historyTable.innerHTML = '<tr><td class="empty-row" colspan="9">No history yet.</td></tr>';
  }
}

function renderSimulation() {
  const sim    = state.sim;
  const active = sim && sim.active;
  const params = (sim && sim.params) ? sim.params : null;

  // Stats from saved trades
  const allRows = Array.isArray(state.savedTrades) ? state.savedTrades : [];
  const trades  = allRows.filter((t) => t.entryPrice != null && t.exitPrice != null && t.type);
  const wins    = trades.filter((t) => t.grossPnl > 0);
  const losses  = trades.filter((t) => t.grossPnl < 0);
  const pnl     = trades.reduce((s, t) => s + t.grossPnl, 0);
  const best    = wins.length   ? Math.max(...wins.map((t) => t.grossPnl))   : null;
  const worst   = losses.length ? Math.min(...losses.map((t) => t.grossPnl)) : null;
  const avgW    = wins.length   ? wins.reduce((s, t) => s + t.grossPnl, 0) / wins.length : 0;
  const avgL    = losses.length ? Math.abs(losses.reduce((s, t) => s + t.grossPnl, 0) / losses.length) : 0;

  const metaText = params ? (params.instrument || 'options') + ' | ' + (params.slen || 5) + '/' + (params.llen || 9) : 'No simulation yet';
  if (els.resultMeta)       els.resultMeta.textContent       = metaText;
  if (els.resultMetaInline) els.resultMetaInline.textContent = metaText;
  els.statTrades.textContent   = String(trades.length);
  els.statWinRate.textContent  = trades.length ? Math.round((wins.length / trades.length) * 100) + '%' : '-';
  els.statPnl.textContent      = trades.length ? formatCurrency(pnl) : '-';
  els.statPnl.className        = pnl >= 0 ? 'positive' : 'negative';
  els.statBest.textContent     = best  !== null ? formatCurrency(best)  : '-';
  els.statWorst.textContent    = worst !== null ? formatCurrency(worst) : '-';
  const rr = avgL > 0 ? (avgW / avgL) : 0;
  els.statRR.textContent       = trades.length ? (avgL > 0 ? Math.round((1 / (1 + rr)) * 100) + '%' : '0%') : '-';

  renderActivePosition();
  els.simBtn.textContent = active ? 'Stop Simulation' : 'Run Simulation';
  els.simBtn.classList.toggle('stop', active);
}

function renderActivePosition() {
  // Read position from SERVER state, not browser state
  const sim = state.sim;
  const pos  = sim && sim.position ? sim.position : null;
  const card = els.activeTradeCard;
  if (!pos) { if (card) card.style.display = 'none'; return; }
  if (card) card.style.display = 'block';
  const entry = parseFloat(pos.entry || 0);
  const last  = parseFloat(pos.last_price || entry);
  const lotSize = parseInt((sim.params && sim.params.lotSize) || pos.lot_size || 65, 10);
  const unrealized = (last - entry) * lotSize;
  els.activeTradeGrid.innerHTML = [
    cell('Type',       pos.type),
    cell('Instrument', pos.instrument),
    cell('Entry',      formatNumber(entry)),
    cell('Live',       formatNumber(last)),
    cell('SL',         formatNumber(pos.cur_sl)),
    cell('Target',     formatNumber(pos.tgt)),
    cell('Contract',   pos.contract || '-'),
    cell('Unrealized', formatCurrency(unrealized), unrealized >= 0 ? 'positive' : 'negative'),
  ].join('');
}

function renderSavedTrades() {
  const allRows = Array.isArray(state.savedTrades) ? state.savedTrades.slice().reverse() : [];
  const rows    = allRows.filter((t) => t.entryPrice != null && t.exitPrice != null && t.type);
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
    const startIdx  = (state.tradesPage - 1) * state.tradesPageSize;
    const pageRows  = rows.slice(startIdx, startIdx + state.tradesPageSize);
    els.savedTradesTable.innerHTML = pageRows.map((trade) => {
      const pnl       = trade.grossPnl != null ? trade.grossPnl : 0;
      const trailSL   = trade.trailSL  != null ? trade.trailSL  : null;
      const symbolLabel = formatTradeSymbol(trade);
      return `
      <tr>
        <td class="mono trade-symbol">${escapeHtml(symbolLabel || trade.instrument || '—')}</td>
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
  const active = state.sim && state.sim.active;
  els.liveBadge.textContent = active ? 'LIVE' : 'READY';
  els.liveBadge.classList.toggle('live',    active);
  els.liveBadge.classList.toggle('stopped', !active);
}

function setStatus(message, isError = false, isSuccess = false) {
  if (!message) { els.statusBox.style.display = 'none'; els.statusBox.textContent = ''; return; }
  els.statusBox.textContent = message;
  els.statusBox.className   = 'status-card';
  if (isError)   els.statusBox.classList.add('error');
  if (isSuccess) els.statusBox.classList.add('success');
  els.statusBox.style.display = 'flex';
}

// ─────────────────────────────────────────────────────────────────────────────
// Form param helpers (read-only display params from form fields)
// ─────────────────────────────────────────────────────────────────────────────
function readSimulationParams() {
  const params = {
    instrument:    els.inpInstrument.value,
    sl:            parseFloat(els.inpSL.value),
    target:        parseFloat(els.inpTarget.value),
    trailTrigger:  parseFloat(els.inpTrailTrigger.value || '0'),
    trailLock:     parseFloat(els.inpTrailLock.value    || '0'),
    lotSize:       parseInt(els.inpLotSize.value, 10),
    delta:         parseFloat(els.inpDelta.value || '0.5'),
    slen:          parseInt(els.simShortLen.value, 10),
    llen:          parseInt(els.simLongLen.value,  10),
    minQuality:    parseInt(els.inpMinQuality.value, 10),
    sidewaysFilter: els.inpSidewaysFilter.checked,
    confirmCandle:  els.inpConfirmCandle.checked,
  };
  if (!Number.isFinite(params.sl)      || params.sl     <= 0) return setStatus('Stop loss must be > 0.',   true), null;
  if (!Number.isFinite(params.target)  || params.target <= 0) return setStatus('Target must be > 0.',      true), null;
  if (!Number.isFinite(params.lotSize) || params.lotSize<= 0) return setStatus('Lot size must be > 0.',    true), null;
  if (!Number.isFinite(params.slen)    || !Number.isFinite(params.llen) || params.slen >= params.llen)
    return setStatus('Short VMA must be smaller than Long VMA.', true), null;
  return params;
}

function getDisplaySimParams() {
  // For dashboard signal display — read from server sim params if available, else form
  const sim = state.sim;
  if (sim && sim.params && Object.keys(sim.params).length > 0) return sim.params;
  return {
    minQuality:    parseInt(els.inpMinQuality.value, 10) || 0,
    sidewaysFilter: els.inpSidewaysFilter.checked,
    confirmCandle:  els.inpConfirmCandle.checked,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Signal helpers (pure display — used for history table coloring only)
// ─────────────────────────────────────────────────────────────────────────────
function getEntrySignal(bar, simParams) {
  const signal  = bar.signal         === 'CE' || bar.signal         === 'PE' ? bar.signal         : 'NONE';
  const confirm = bar.confirm_signal === 'CE' || bar.confirm_signal === 'PE' ? bar.confirm_signal : 'NONE';
  if (simParams.confirmCandle) return confirm;
  if ((simParams.minQuality || 0) > 0 && confirm !== 'NONE') return confirm;
  return signal;
}

function analyzeHistoryDecisions(history, simParams) {
  const bars = Array.isArray(history) ? history : [];
  let lastAccepted = null;
  return bars.map((bar) => {
    const decision = getBarDecision(bar, simParams, lastAccepted);
    if (decision.eligible) lastAccepted = decision.entrySignal;
    return { ...bar, skipReason: decision.reason, entrySignal: decision.entrySignal };
  });
}

function getBarDecision(bar, simParams, lastAccepted) {
  const entrySignal   = getEntrySignal(bar, simParams);
  const hasDirectSig  = bar.signal === 'CE' || bar.signal === 'PE';
  if (entrySignal !== 'CE' && entrySignal !== 'PE') {
    if (simParams.confirmCandle && hasDirectSig) return { eligible: false, reason: 'Waiting for confirm candle', entrySignal: 'NONE' };
    return { eligible: false, reason: 'No entry signal', entrySignal: 'NONE' };
  }
  if (lastAccepted && lastAccepted === entrySignal)  return { eligible: false, reason: 'Same side as last trade', entrySignal };
  if (simParams.sidewaysFilter && bar.is_sideways)   return { eligible: false, reason: 'Sideways filter blocked', entrySignal };
  if ((bar.quality || 0) < (simParams.minQuality || 0)) return { eligible: false, reason: `Quality ${bar.quality||0} below min ${simParams.minQuality}`, entrySignal };
  return { eligible: true, reason: 'Eligible', entrySignal };
}

// ─────────────────────────────────────────────────────────────────────────────
// Form state persistence (only UI/form fields, not simulation runtime)
// ─────────────────────────────────────────────────────────────────────────────
function persistFormState() {
  const payload = {
    version: 4,
    tf:              els.timeframe.value,
    shortLen:        els.shortLen.value,
    longLen:         els.longLen.value,
    refreshInterval: els.refreshInterval.value,
    simFields: {
      instrument:    els.inpInstrument.value,
      sl:            els.inpSL.value,
      target:        els.inpTarget.value,
      trailTrigger:  els.inpTrailTrigger.value,
      trailLock:     els.inpTrailLock.value,
      lotSize:       els.inpLotSize.value,
      delta:         els.inpDelta.value,
      simShortLen:   els.simShortLen.value,
      simLongLen:    els.simLongLen.value,
      minQuality:    els.inpMinQuality.value,
      sidewaysFilter: els.inpSidewaysFilter.checked,
      confirmCandle:  els.inpConfirmCandle.checked,
    },
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

function restoreFormState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) { applyDefaultFormValues(); return; }
    const saved = JSON.parse(raw);
    // Clear old format (v3) automatically
    if (!saved.version || saved.version < 4) { applyDefaultFormValues(); return; }
    els.shortLen.value        = saved.shortLen        || DEFAULTS.shortLen;
    els.longLen.value         = saved.longLen         || DEFAULTS.longLen;
    els.timeframe.value       = saved.tf              || DEFAULTS.timeframe;
    els.refreshInterval.value = saved.refreshInterval || DEFAULTS.refreshInterval;
    if (saved.simFields) {
      els.inpInstrument.value       = saved.simFields.instrument    || DEFAULTS.instrument;
      els.inpSL.value               = saved.simFields.sl            || DEFAULTS.sl;
      els.inpTarget.value           = saved.simFields.target        || DEFAULTS.target;
      els.inpTrailTrigger.value     = saved.simFields.trailTrigger  || DEFAULTS.trailTrigger;
      els.inpTrailLock.value        = saved.simFields.trailLock     || DEFAULTS.trailLock;
      els.inpLotSize.value          = saved.simFields.lotSize        || DEFAULTS.lotSize;
      els.inpDelta.value            = saved.simFields.delta          || DEFAULTS.delta;
      els.simShortLen.value         = saved.simFields.simShortLen   || els.shortLen.value;
      els.simLongLen.value          = saved.simFields.simLongLen    || els.longLen.value;
      els.inpMinQuality.value       = saved.simFields.minQuality    || DEFAULTS.minQuality;
      els.inpSidewaysFilter.checked = saved.simFields.sidewaysFilter === true;
      els.inpConfirmCandle.checked  = saved.simFields.confirmCandle  === true;
    } else {
      applyDefaultFormValues();
    }
  } catch (_) {
    localStorage.removeItem(STORAGE_KEY);
    applyDefaultFormValues();
  }
}

function applyDefaultFormValues() {
  els.timeframe.value       = DEFAULTS.timeframe;
  els.refreshInterval.value = DEFAULTS.refreshInterval;
  els.inpInstrument.value   = DEFAULTS.instrument;
  els.inpSL.value           = DEFAULTS.sl;
  els.inpTarget.value       = DEFAULTS.target;
  els.inpTrailTrigger.value = DEFAULTS.trailTrigger;
  els.inpTrailLock.value    = DEFAULTS.trailLock;
  els.inpLotSize.value      = DEFAULTS.lotSize;
  els.inpDelta.value        = DEFAULTS.delta;
  els.shortLen.value        = DEFAULTS.shortLen;
  els.longLen.value         = DEFAULTS.longLen;
  els.simShortLen.value     = DEFAULTS.shortLen;
  els.simLongLen.value      = DEFAULTS.longLen;
  els.inpMinQuality.value   = DEFAULTS.minQuality;
  els.inpSidewaysFilter.checked = DEFAULTS.sidewaysFilter;
  els.inpConfirmCandle.checked  = DEFAULTS.confirmCandle;
}

function syncFormToState() { updateInstrumentMode(); renderBadge(); renderSessionInfo(); }
function syncLengthFields(event) {
  if (event.target.id === 'shortLen')    els.simShortLen.value = els.shortLen.value;
  if (event.target.id === 'simShortLen') els.shortLen.value    = els.simShortLen.value;
  if (event.target.id === 'longLen')     els.simLongLen.value  = els.longLen.value;
  if (event.target.id === 'simLongLen')  els.longLen.value     = els.simLongLen.value;
  persistFormState();
}
function updateInstrumentMode() {
  const isOptions = els.inpInstrument.value === 'options';
  els.inpDelta.disabled = isOptions;
  persistFormState();
}

function resetSimulationForm() {
  stopPolling();
  sendSimControl('stop').catch(() => {});
  applyDefaultFormValues();
  updateInstrumentMode();
  state.sim = null;
  renderSimulation();
  renderSessionInfo();
  persistFormState();
  setStatus('Simulation form reset with VMA 5/9 and the requested risk defaults.', false, true);
}

// ─────────────────────────────────────────────────────────────────────────────
// Market clock + session
// ─────────────────────────────────────────────────────────────────────────────
function startMarketClock() {
  stopMarketClock();
  renderSessionInfo();
  state.clockTimer = window.setInterval(() => {
    renderSessionInfo();
    syncMarketSession();
  }, 1000);
}
function stopMarketClock() {
  if (state.clockTimer) { window.clearInterval(state.clockTimer); state.clockTimer = null; }
}

function renderSessionInfo(providedSession) {
  const session = providedSession || getMarketSessionState();
  els.marketClock.textContent   = session.clockLabel;
  els.sessionStatus.textContent = session.statusLabel;
  els.sessionWindow.textContent = '09:16 - 15:30 IST';
}

function getMarketSessionState() {
  const parts       = getIndiaDateParts();
  const totalMin    = Number(parts.hour) * 60 + Number(parts.minute);
  const isWeekend   = parts.weekday === 'Sat' || parts.weekday === 'Sun';
  let statusLabel   = 'Waiting for market open';
  let isOpen        = false;
  const sim         = state.sim;
  const manualPause = sim && sim.manual_pause;
  if (isWeekend) { statusLabel = 'Market closed for weekend'; }
  else if (totalMin < MARKET_WINDOW.startMinutes) { statusLabel = 'Auto-start at 09:16 IST'; }
  else if (totalMin >= MARKET_WINDOW.endMinutes)  { statusLabel = 'Session closed at 15:30 IST'; }
  else { isOpen = true; statusLabel = manualPause ? 'Paused manually until next session' : 'Live auto-run window active'; }
  return { isOpen, clockLabel: parts.hour + ':' + parts.minute + ':' + parts.second + ' IST', statusLabel };
}

function isMarketOpen() { return getMarketSessionState().isOpen; }

function getIndiaDateParts() {
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: INDIA_TIMEZONE, weekday: 'short',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  return formatter.formatToParts(new Date()).reduce((acc, part) => {
    if (part.type !== 'literal') acc[part.type] = part.value;
    return acc;
  }, {});
}

// ─────────────────────────────────────────────────────────────────────────────
// Misc helpers
// ─────────────────────────────────────────────────────────────────────────────
function registerServiceWorker() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => {});
}

function signalPill(value) {
  const n = String(value || 'NONE').toLowerCase();
  return '<span class="signal-pill ' + n + '">' + escapeHtml(String(value || 'NONE')) + '</span>';
}
function reasonPill(value) {
  const m = { 'SL': { label: 'SL_HIT', cls: 'sl' }, 'TRAILING_SL': { label: 'TRAIL_SL_HIT', cls: 'trailing_sl' }, 'TARGET': { label: 'TARGET', cls: 'target' }, 'EOD': { label: 'EOD', cls: 'eod' }, 'EOD_AUTO': { label: 'EOD AUTO', cls: 'eod' }, 'MANUAL': { label: 'MANUAL', cls: 'manual_exit' }, 'TF_SWITCH': { label: 'TF SWITCH', cls: 'eod' } };
  const e = m[String(value)] || { label: String(value || 'EOD'), cls: 'eod' };
  return '<span class="reason-pill ' + e.cls + '">' + escapeHtml(e.label) + '</span>';
}
function cell(key, value, extraClass = '') {
  return '<div class="active-cell"><span class="key">' + escapeHtml(key) + '</span><span class="value ' + extraClass + '">' + escapeHtml(String(value)) + '</span></div>';
}
function formatCurrency(value)  { return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(value || 0); }
function formatNumber(value)    { if (value == null || value === '') return '-'; return Number(value).toFixed(2); }
function formatDateTime(value)  { const d = asDate(value); return d ? d.toLocaleString() : '-'; }
function formatTradeTime(value) {
  const d = asDate(value); if (!d) return '-';
  const day   = d.getDate();
  const month = d.toLocaleString('en-IN', { month: 'short', timeZone: 'Asia/Kolkata' });
  const time  = d.toLocaleString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' });
  return day + '<br>' + month + '.<br>' + time;
}
function formatTradeSymbol(trade) { return trade?.tradingsymbol || trade?.contract || trade?.instrument || '-'; }
function asDate(value)           { if (!value) return null; const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
function round(value)            { return Math.round((Number(value) || 0) * 100) / 100; }
function escapeHtml(value) {
  return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
}
