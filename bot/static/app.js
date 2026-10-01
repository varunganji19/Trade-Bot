'use strict';
/* ===================================================== tiny helpers */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = (v, d = 2) => Number(v).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
const fmt$ = v => (v == null || isNaN(v)) ? '—' : '$' + fmtNum(v);
const sign = v => v > 0 ? '+' : '';
// sign() alone must stay ''-for-negatives: fmtPct does NOT wrap in Math.abs,
// so toFixed already emits the minus there ('--3.20%' if sign grew one). fmtPnl
// takes the abs path and prefixes its own '-'.
// z() kills negative zero and float noise: -0.0 showed as "-0.00%" and a
// 1e-16 re-mark showed as "-$0.00" (a red flag on an empty minus)
const z = v => (v === 0 || Math.abs(v) < 0.005) ? 0 : v;
const fmtPnl = v => (v == null || isNaN(v)) ? '—'
  : (v > 0.005 ? '+' : v < -0.005 ? '-' : '') + '$' + fmtNum(Math.abs(v));
const fmtPct = v => (v == null || isNaN(v)) ? '—' : sign(v) + z(v).toFixed(2) + '%';
const isForex = s => String(s).includes('=');
const fmtPx = (v, s) => (v == null || isNaN(v) || !Number(v)) ? '—'
  : fmtNum(v, isForex(s) ? 5 : 2);
const fmtQty = v => (v == null || isNaN(v)) ? '—'
  : Number(v).toLocaleString(undefined, {maximumSignificantDigits: 5});
const posCls = v => z(v) > 0 ? 'pos' : z(v) < 0 ? 'neg' : '';
const tag = (cls, text) => '<span class="tag ' + esc(cls) + '">' + esc(text) + '</span>';
const sideTag = s => tag((s || '').toLowerCase(), String(s).toUpperCase());
const icon = (name, cls = 'ic') => '<svg class="' + cls + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
/* read a CSS custom property off :root — lets the Chart.js canvas follow
   the active theme (light/dark) without rebuilding it */
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* Dialog focus stays inside the active dialog and returns to its trigger. */
let dialogTrigger = null;
function openDialog(id, focusId) {
  dialogTrigger = document.activeElement;
  const modal = $(id);
  modal.classList.add('open');
  (focusId ? $(focusId) : modal.querySelector('button, input')).focus();
}
function closeDialog(id) {
  const modal = $(id);
  if (!modal.classList.contains('open') || modal.dataset.busy === 'true') return;
  modal.classList.remove('open');
  if (dialogTrigger && dialogTrigger.isConnected) dialogTrigger.focus();
  else if (dialogTrigger && dialogTrigger.dataset.close) {
    const replacement = $$('[data-close]').find(b => b.dataset.close === dialogTrigger.dataset.close);
    (replacement || $('#tabs .active')).focus();
  } else $('#tabs .active').focus();
}
document.addEventListener('keydown', e => {
  const modal = $('.modal-overlay.open');
  if (!modal) return;
  if (e.key === 'Escape') { e.preventDefault(); closeDialog('#' + modal.id); }
  if (e.key !== 'Tab') return;
  const items = Array.from(modal.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]'));
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
});
/* journal timestamps are ISO-UTC; the UI reads IST (+05:30 fixed, no DST) —
   shift by 330min and read via getUTC* so the browser's own zone never leaks in.
   Keep in sync with _fmt_ts in bot/chatbot.py (same IST display contract). */
const fmtTs = ts => {
  const d = new Date(ts);
  if (ts == null || isNaN(d.getTime())) return String(ts || '');
  const ist = new Date(d.getTime() + 330 * 60000);
  const p = n => String(n).padStart(2, '0');
  return p(ist.getUTCMonth() + 1) + '-' + p(ist.getUTCDate()) + ' ' +
         p(ist.getUTCHours()) + ':' + p(ist.getUTCMinutes());
};

/* optional bearer token (DASHBOARD_TOKEN): stored once, attached to every
   fetch. A normal browser navigation cannot send headers, so the page shell
   is served unguarded (no secrets in it) and the SPA supplies the header. */
const _tok = () => { try { return localStorage.getItem('algo-token') || ''; }
                     catch { return ''; } };
function askForToken() {
  /* the gate: shown once when the API answers 401 and no token is stored */
  const gate = $('#tokenGate');
  if (gate.classList.contains('open')) return;
  $('#tokenInput').value = _tok();
  openDialog('#tokenGate', '#tokenInput');
}
$('#tokenSave').addEventListener('click', () => {
  const v = $('#tokenInput').value.trim();
  try { v ? localStorage.setItem('algo-token', v) : localStorage.removeItem('algo-token'); }
  catch { /* private mode: token just won't persist */ }
  closeDialog('#tokenGate');
  location.reload();   // re-boot the pollers with the header attached
});
$('#tokenGate').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeDialog('#tokenGate');
});
$('#tokenCancel').addEventListener('click', () => closeDialog('#tokenGate'));

async function jget(u) {
  const t = _tok();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  let r;
  try { r = await fetch(u, {signal: controller.signal,
    headers: t ? {Authorization: 'Bearer ' + t} : {}}); }
  finally { clearTimeout(timer); }
  if (r.status === 401) { askForToken(); throw new Error('token required (401)'); }
  if (!r.ok) throw new Error('GET ' + u);
  return r.json();
}
async function jreq(u, method, body) {
  const h = {'Content-Type': 'application/json'};
  const t = _tok();
  if (t) h.Authorization = 'Bearer ' + t;
  const r = await fetch(u, {method, headers: h,
                            body: body == null ? undefined : JSON.stringify(body)});
  if (r.status === 401) { askForToken(); throw new Error('token required (401)'); }
  let data = {};
  try { data = await r.json(); } catch { /* non-JSON error body */ }
  if (!r.ok) {
    const msg = (data && data.detail) ? data.detail : (r.status + ' ' + r.statusText);
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}
const jpost = (u, b) => jreq(u, 'POST', b);
const jdel = u => jreq(u, 'DELETE');

/* ===================================================== toasts */
function toast(title, msg, ok = true) {
  const d = document.createElement('div');
  d.className = 'toast' + (ok ? '' : ' err');
  d.innerHTML = icon(ok ? 'check' : 'alert') +
    '<div><div class="t-title">' + esc(title) + '</div>' +
    (msg ? '<div class="t-msg">' + esc(msg) + '</div>' : '') + '</div>';
  $('#toasts').appendChild(d);
  setTimeout(() => { d.classList.add('out'); setTimeout(() => d.remove(), 350); }, 4200);
}
const toastErr = (title, e) => toast(title, e && e.message ? e.message : String(e), false);
/* the degraded-engine banner: dismissed by the operator stays dismissed until
   the note CHANGES (a new condition re-shows it) */
let healthDismissed = false, healthDismissedMsg = '';
let autoResumeToasted = false;
$('#healthDismiss').addEventListener('click', () => {
  healthDismissed = true;
  healthDismissedMsg = $('#healthMsg').textContent;
  $('#healthBanner').classList.remove('show');
  $('#enginePill').classList.remove('warn');
});

/* ===================================================== routing */
const VIEWS = ['overview', 'portfolio', 'hft', 'watchlist', 'lab', 'evidence', 'account', 'chat'];
const VIEW_COPY = {
  overview: ['Standard book · paper', 'Overview', 'Performance, positions and the engine at a glance.'],
  portfolio: ['Standard book · paper', 'Portfolio', 'Open exposure and every trade the bot has taken.'],
  hft: ['Fast book · paper · experimental', 'Fast book (experimental)', 'A separate 5-minute account with its own capital and fees. No proven edge.'],
  watchlist: ['Standard book · paper', 'Watchlist', 'The markets and timeframes the engine trades.'],
  lab: ['Research', 'Strategy Lab', 'Backtest any strategy on real data before trusting it.'],
  evidence: ['Research', 'Evidence', 'Validation, forecast quality and rule adherence — measured, not claimed.'],
  account: ['Standard book · paper', 'Paper account', 'Simulated balance, deposits, withdrawals and reset.'],
  chat: ['Research', 'Ask the journal', 'Plain-language answers drawn from the trading journal.']
};
function setView(name) {
  if (!VIEWS.includes(name)) name = 'overview';
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  $$('#tabs .tab').forEach(t => {
    const active = t.dataset.view === name;
    t.classList.toggle('active', active);
    if (active) t.setAttribute('aria-current', 'page');
    else t.removeAttribute('aria-current');
  });
  const [kicker, title, description] = VIEW_COPY[name];
  document.body.dataset.view = name;
  $('#workspaceKicker').textContent = kicker;
  $('#pageTitle').textContent = title;
  $('#pageDescription').textContent = description;
  /* on phones the nav is a horizontal strip: keep the active tab in view */
  const selected = $('#tab-' + name);
  $('#tabs').scrollLeft = Math.max(0, selected.offsetLeft - $('#tabs').clientWidth / 2 + selected.offsetWidth / 2);
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.title = title + ' — Algo';
  refreshVisible(name);
  window.scrollTo({top: 0, behavior: reduceMotion ? 'auto' : 'smooth'});
}
window.addEventListener('hashchange', () => setView(location.hash.slice(1) || 'overview'));
$('#tabs').addEventListener('click', e => { const t = e.target.closest('.tab'); if (t) setView(t.dataset.view); });
$$('[data-goto]').forEach(b => b.addEventListener('click', () => setView(b.dataset.goto)));
$('.skip-link').addEventListener('click', e => { e.preventDefault(); $('#mainContent').focus(); });

/* one-shot view flags — declared before setView() can run them at boot */
const chatLoaded = {v: false}, evLoaded = {v: false};
function refreshVisible(name) {
  refreshStats(); // shared operating state stays current on EVERY section
  if (name === 'overview') { refreshEquity(); refreshDecisions(); }
  else if (name === 'portfolio') { refreshTrades(); }
  else if (name === 'hft') { refreshHft(); }
  else if (name === 'watchlist') refreshWatchlist();
  else if (name === 'lab') { refreshLab(); }
  /* evidence loads ONCE per tab entry, not on every 4s poll: the payload is
     generated artifacts (slow to change) and rebuilding both charts each tick
     churned ~0.7s CPU while the tab was merely open */
  else if (name === 'evidence' && !evLoaded.v) refreshEvidence();
  else if (name === 'account') { refreshAccount(); refreshTransactions(); }
  else if (name === 'chat' && !chatLoaded.v) loadChatHistory();
}

/* ===================================================== charts
   Every chart shares one theme-aware style; colors come from the live CSS
   tokens, so a theme switch recolors canvases without rebuilding them. */
function chartTheme(chart) {
  const tt = chart.options.plugins.tooltip;
  tt.backgroundColor = cssVar('--color-surface');
  tt.borderColor = cssVar('--color-border');
  tt.titleColor = cssVar('--color-text');
  tt.bodyColor = cssVar('--color-muted');
  const legend = chart.options.plugins.legend;
  if (legend.labels) legend.labels.color = cssVar('--color-muted');
  for (const ax of ['x', 'y']) {
    chart.options.scales[ax].ticks.color = cssVar('--color-muted');
    chart.options.scales[ax].grid.color = cssVar('--chart-grid');
  }
}
function chartOptions({yTick, tooltipLabel, legend = false, xGrid = true} = {}) {
  return {
    responsive: true, maintainAspectRatio: false,
    animation: reduceMotion ? false : {duration: 250},
    interaction: {mode: 'index', intersect: false},
    plugins: {
      legend: legend ? {labels: {boxWidth: 10, font: {size: 11}}} : {display: false},
      tooltip: {borderWidth: 1, padding: 10,
                callbacks: tooltipLabel ? {label: tooltipLabel} : {}}},
    scales: {
      x: {ticks: {maxTicksLimit: 6, maxRotation: 0, autoSkipPadding: 18, font: {size: 11}}, grid: {display: xGrid}},
      y: {ticks: {font: {size: 11}, callback: yTick}, grid: {}}}
  };
}
function lineDataset(label, extra = {}) {
  return {label, data: [], borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill'),
          fill: true, tension: .15, pointRadius: 0, borderWidth: 2, ...extra};
}
function buildChart(canvasSel, config) {
  const chart = new Chart($(canvasSel), config);
  chartTheme(chart);
  chart.update('none');
  return chart;
}
/* the three equity curves (Overview / Fast book / Lab) are the same chart */
function buildEquityLine(canvasSel, label) {
  return buildChart(canvasSel, {type: 'line', data: {labels: [], datasets: [lineDataset(label)]},
    options: chartOptions({yTick: v => '$' + v.toLocaleString(), tooltipLabel: c => ' ' + fmt$(c.parsed.y)})});
}
/* an empty chart keeps its layout box (visibility, not display: a canvas
   that is display:none when Chart.js builds it never gets a size) */
const showChart = (sel, on) => $(sel).classList.toggle('is-empty', !on);
function setSeries(chart, labels, ...series) {
  chart.data.labels = labels;
  series.forEach((s, i) => { chart.data.datasets[i].data = s; });
  chart.update(reduceMotion ? 'none' : undefined);
}

let equityChart = null, hftChart = null, hftPriceChart = null, labChart = null;
let kronosChart = null, cvChart = null;

/* ===================================================== theme
   data-theme is already on <html> (set pre-paint in <head>) — this section
   wires the switcher, updates the browser chrome color and recolors charts. */
const THEMES = ['light', 'dark'];
function applyTheme(t, persist) {
  if (!THEMES.includes(t)) t = 'light';
  document.documentElement.setAttribute('data-theme', t);
  if (persist) { try { localStorage.setItem('algo-theme', t); } catch { /* private mode */ } }
  $$('.theme-switch button').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.theme === t)));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', cssVar('--color-background'));
  for (const chart of [equityChart, hftChart, labChart]) {
    if (!chart) continue;
    Object.assign(chart.data.datasets[0], {borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill')});
    chartTheme(chart);
    chart.update('none');
  }
  if (hftPriceChart) {
    Object.assign(hftPriceChart.data.datasets[0], {borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill')});
    hftPriceChart.data.datasets[1].borderColor = cssVar('--color-muted');
    chartTheme(hftPriceChart);
    hftPriceChart.update('none');
  }
  /* the Evidence charts carry per-bar semantic colors: rebuild on next visit */
  if (evLoaded.v) {
    if (kronosChart) { kronosChart.destroy(); kronosChart = null; }
    if (cvChart) { cvChart.destroy(); cvChart = null; }
    evLoaded.v = false;
  }
}
$$('.theme-switch button').forEach(b =>
  b.addEventListener('click', () => applyTheme(b.dataset.theme, true)));

/* ---- shared render helpers (the Overview, Fast book and Lab views render
   the same payload shapes) ---- */
function syncStrategyFilter(sel, strategies) {
  const current = sel.value;
  if (sel.options.length - 1 !== strategies.length ||
      [...sel.options].slice(1).map(o => o.value).join(',') !== strategies.join(',')) {
    sel.innerHTML = '<option value="">All strategies</option>' +
      strategies.map(x => '<option value="' + esc(x) + '">' + esc(x) + '</option>').join('');
    sel.value = current;
  }
}
const vetoRow = (label, value, cls = '') =>
  '<div class="veto-row ' + cls + '"><span class="r">' + label + '</span><span class="n">' + value + '</span></div>';
function renderStrategies(boxSel, listSel, st) {
  /* a book with no voting strategy cannot trade, and that must not look
     like a quiet market */
  if (!st || !st.registered) return;
  $(boxSel).hidden = false;
  const voting = st.voting || [], silent = st.silent || [];
  /* the gate can be OFF without anything looking wrong — the verdicts file
     lives under the active data directory, so switching directories returns
     it to "everything votes" in silence. Say which state it is in, first. */
  const g = st.gate || {};
  const gateRow = g.state === 'no_evidence'
    ? vetoRow('promotion gate: UNMEASURED', '<span class="quiet">' + esc(g.why || '') + '</span>', 'top')
    : (g.state === 'active'
        ? vetoRow('promotion gate: ' + (g.stale ? 'STALE EVIDENCE' : 'active'),
            '<span class="quiet">' + esc(g.why || '') + ' · ' + esc(g.generated_at || '') + '</span>',
            g.stale ? 'top' : '')
        : '');
  /* a vote on probation is a vote without proof; say so next to the name */
  const voters = st.voters || voting.map(n => ({name: n, status: 'promoted', why: ''}));
  const head = voters.length
    ? voters.map(v => v.status === 'promoted'
        ? vetoRow('voting: ' + esc(v.name), '<span class="quiet">' + esc(v.why) + '</span>')
        : vetoRow('voting, unproven: ' + esc(v.name), esc(v.status) + ' — ' + esc(v.why), 'warn')).join('')
    : vetoRow('NO strategy can trade this book', '0 / ' + st.registered, 'top');
  const rest = silent.map(x => vetoRow('<span class="quiet">' + esc(x.name) + '</span>',
    '<span class="quiet">' + esc(x.why) + '</span>')).join('');
  $(listSel).innerHTML = gateRow + head + rest;
}
function renderVetoes(boxSel, sumSel, listSel, v) {
  /* "the engine is running" and "the engine is trading" are different
     claims; this box is the second one. */
  const box = $(boxSel);
  if (!v || !v.attempts) { box.hidden = true; return; }
  box.hidden = false;
  $(sumSel).textContent = v.approved + ' approved / ' + v.attempts + ' attempted';
  const rows = (v.by_reason || []).slice(0, 6);
  let html = rows.length
    ? rows.map((r, i) => vetoRow(esc(r.reason.replace(/_/g, ' ')), r.count,
        i === 0 && !v.approved ? 'top' : '')).join('')
    : vetoRow('no entries blocked', 0);
  if (v.attempts > v.approved && !v.approved) {
    html += vetoRow('<span class="quiet">every entry so far was refused — the top blocker above is why</span>', '');
  }
  $(listSel).innerHTML = html;
}
/* cards: [label, value, valueClass, sub] */
function renderStatCards(el, cards) {
  el.innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');
}
function renderDecisionRows(el, rows, extraBadge) {
  if (!rows.length) { el.innerHTML = '<div class="empty">No decisions journaled yet.</div>'; return; }
  el.innerHTML = rows.map(d => {
    const a = (d.action || '').toLowerCase();
    return '<div class="feed-row">' +
      '<span class="feed-ts">' + esc(fmtTs(d.ts)) + '</span>' +
      '<div><div class="feed-line">' + tag(a, d.action) +
      '<span class="feed-mkt">' + esc(d.symbol) + ' ' + tag('tf', d.timeframe) +
      (extraBadge ? extraBadge(d) : '') + '</span>' +
      '<span class="feed-meta">' + esc(d.regime || '—') + ' · conf ' +
        Math.round((d.confidence || 0) * 100) + '% · @ ' + fmtPx(d.price, d.symbol) + '</span>' +
      '</div><div class="feed-why">' + esc(d.rationale || '') + '</div></div></div>';
  }).join('');
  el.scrollTop = 0;
}
function renderBars(el, rows) {
  const entries = rows.filter(r => Math.abs(r.pnl || 0) > 0 || r.trades > 0);
  if (!entries.length) {
    el.innerHTML = '<div class="empty">No closed trades yet.</div>';
    return;
  }
  const maxAbs = Math.max(...entries.map(r => Math.abs(r.pnl || 0)), 1);
  el.innerHTML = entries.map(r => {
    const cls = (r.pnl || 0) >= 0 ? 'pos' : 'neg';
    const w = Math.max(2, Math.abs(r.pnl || 0) / maxAbs * 100);
    const title = r.name + ': ' + r.trades + ' trades' + (r.wins != null ? ', ' + r.wins + ' wins' : '');
    return '<div class="sbar" title="' + esc(title) + '">' +
      '<span class="name">' + esc(r.name) + '</span>' +
      '<span class="track"><span class="fill ' + cls + '" style="width:' + w.toFixed(1) + '%"></span></span>' +
      '<span class="val ' + cls + '">' + fmtPnl(r.pnl) + '</span></div>';
  }).join('');
}
/* one row shape for both books' trade histories */
function tradeRow(t) {
  return '<tr>' +
    '<td class="muted">' + esc(fmtTs(t.opened_ts)) + '</td>' +
    '<td class="symbol">' + esc(t.symbol) + ' ' + tag('tf', t.timeframe || '') +
      (t.mode === 'demo' ? ' ' + tag('demo', 'demo') : '') + '</td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + (t.status === 'CLOSED' ? fmtPnl(t.pnl) : '—') + '</td>' +
    '<td class="strategy">' + esc(t.strategy) + '</td>' +
    '<td>' + tag(t.status === 'OPEN' ? 'open' : '', t.status) + '</td>' +
    '<td class="muted">' + esc(t.exit_reason || '—') + '</td></tr>';
}

/* ===================================================== overview */
let statsBusy = false, lastStatsAt = 0, statsFailed = false, authNeeded = false, engineActionBusy = false;
function updateFreshness() {
  /* stale means a request FAILED (jget aborts after 10s) — not merely an old
     timestamp: polling pauses while the tab is hidden, which is not an outage */
  const age = lastStatsAt ? Math.floor((Date.now() - lastStatsAt) / 1000) : 0;
  const stale = statsFailed && !authNeeded;
  $('#connectionState').dataset.state = stale || authNeeded ? 'error' : lastStatsAt ? 'ok' : 'loading';
  $('#connectionState').textContent = authNeeded ? 'Token required' : stale ? 'Connection lost'
    : lastStatsAt ? (age < 5 ? 'Updated just now' : 'Updated ' + age + 's ago') : 'Connecting…';
  $('#connectionBanner').hidden = !stale;
  $('#connectionBanner').classList.toggle('show', !!stale);
  if (statsFailed) {
    $('#enginePillText').textContent = 'Engine status unavailable';
    $('#engineDot').className = 'dot';
    $('#btnStart').disabled = true;
    $('#btnStop').disabled = true;
  }
}
async function refreshStats() {
  if (statsBusy) return;
  statsBusy = true;
  let s;
  try { s = await jget('/api/stats'); }
  catch (e) {
    statsFailed = true;
    authNeeded = /401/.test(String(e && e.message));
    updateFreshness();
    return;
  }
  finally { statsBusy = false; }
  lastStatsAt = Date.now(); statsFailed = authNeeded = false; updateFreshness();
  const openN = (s.open_positions || []).length;
  renderStatCards($('#ovStats'), [
    ['Equity', fmt$(s.current_equity), '',
      fmtPct(s.return_pct) + ' return · from ' + fmt$(s.start_equity)],
    ['Realized P&L', fmtPnl(s.total_pnl), posCls(s.total_pnl),
      (s.closed_trades ?? 0) + ' closed trades'],
    ['Open positions', String(openN), '',
      s.engine_running ? 'marked live by the engine' : 'as saved in the journal'],
    ['Max drawdown', s.max_drawdown_pct ? fmtPct(-Math.abs(s.max_drawdown_pct)) : '0.00%',
      s.max_drawdown_pct ? 'neg' : '', 'largest peak-to-trough decline'],
  ]);
  const secondary = [
    ['Win rate', s.closed_trades ? (s.win_rate ?? 0) + '%' : '—'],
    ['Profit factor', !s.closed_trades ? '—' : s.profit_factor == null ? '∞' : s.profit_factor],
    ['Avg win / loss', s.closed_trades ? fmtPnl(s.avg_win) + ' / ' + fmtPnl(s.avg_loss) : '—'],
    ['Markets', s.watchlist_count ?? 0],
  ];
  if (s.net_deposits) secondary.push(['Net deposits', fmtPnl(s.net_deposits)]);
  $('#overviewSecondary').innerHTML = secondary.map(([label, value]) =>
    '<div><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>').join('');

  const demoN = (s.trade_modes || {}).demo || 0;
  const paperN = (s.trade_modes || {}).paper || 0;
  $('#firstRun').hidden = !!(s.engine_running || paperN || openN);
  $('#ovDemoNote').innerHTML = demoN
    ? tag('demo', 'demo') + ' ' + demoN + ' seeded backtest-replay trades are ' +
      'kept in the history (badged) and never counted in these figures.'
    : '';

  const lifecycle = s.engine_state || (s.engine_running ? 'running' : 'stopped');
  const state = s.engine_running ? (s.health_note ? 'Needs attention' : s.entries_halted ? 'Daily loss halt'
    : s.paused ? 'Entries paused' : 'Running') : lifecycle === 'starting' ? 'Starting…'
    : lifecycle === 'stopping' ? 'Stopping…' : 'Stopped';
  const healthy = s.engine_running && !s.paused && !s.entries_halted && !s.health_note;
  $('#engineDot').className = 'dot ' + (healthy ? 'on' : '');
  $('#enginePillText').textContent = 'Engine · ' + state;
  /* health_note: degraded-but-alive (e.g. an open position behind a dead feed).
     It rode only in /api/engine/status before — nothing rendered it, so the
     pill stayed green on stage while a position sat unguarded. */
  const hb = $('#healthBanner');
  if (s.health_note && s.health_note !== healthDismissedMsg) healthDismissed = false;
  const showHealth = !!(s.health_note && !healthDismissed);
  hb.classList.toggle('show', showHealth);
  $('#enginePill').classList.toggle('warn', showHealth);
  $('#enginePill').title = showHealth ? s.health_note : '';
  if (showHealth) $('#healthMsg').textContent = s.health_note;
  if (s.auto_resumed && !autoResumeToasted) {
    autoResumeToasted = true;
    toast('Engine auto-resumed', 'The last session left it running, so it is paper-trading now.');
  }
  $('#engineStateText').textContent = state;
  $('#engineStateText').className = 'status-chip ' + (healthy ? 'on' : s.engine_running ? 'warn' : '');
  $('#engineStateSub').textContent = (s.cycles ?? 0) + ' cycles this run · ' +
    (s.watchlist_count ?? 0) + ' markets watched';
  renderVetoes('#vetoBox', '#vetoSummary', '#vetoList', s.vetoes);
  renderStrategies('#stratBox', '#stratList', s.strategies);
  const transitioning = lifecycle === 'starting' || lifecycle === 'stopping';
  $('#btnStart').disabled = engineActionBusy || !!s.engine_running || transitioning;
  $('#btnStop').disabled = engineActionBusy || !s.engine_running || transitioning;
  /* the Interval select used to be DISABLED whenever the engine ran, so the
     cadence could only be changed by stopping and restarting the engine (a
     full rebuild: book lease, position restore). The loop now re-reads its
     interval every cycle, so the control stays live. */
  $('#intervalSel').disabled = transitioning;
  if (document.activeElement !== $('#intervalSel') && s.interval &&
      [...$('#intervalSel').options].some(o => +o.value === s.interval)) {
    $('#intervalSel').value = String(s.interval);
  }

  /* manual pause: banner + button flip. Reported for a stopped engine too —
     the flag is a file that outlives any engine run, and a pause must never
     be lost by stopping/starting the engine. */
  $('#pauseBanner').classList.toggle('show', !!s.paused);
  $('#pauseNoteBox').textContent = s.paused && s.paused_note ? ' Note: ' + s.paused_note + '.' : '';
  $('#pauseLabel').textContent = s.paused ? 'Resume entries' : 'Pause entries';

  renderPositions(s);
  renderBars($('#stratBars'), Object.entries(s.by_strategy || {})
    .map(([name, v]) => ({name, pnl: v.pnl, trades: v.trades, wins: v.wins})));
}

let equityHistory = [], equityDays = 0;
function renderEquityHistory() {
  if (!equityChart) return;
  const lastTime = equityHistory.length ? Date.parse(equityHistory[equityHistory.length - 1].ts) : 0;
  const eq = equityDays ? equityHistory.filter(p => Date.parse(p.ts) >= lastTime - equityDays * 86400000) : equityHistory;
  const drawable = eq.length > 1;   // one mark is a point, not a curve
  $('#equityEmpty').hidden = drawable;
  showChart('#equityChart', drawable);
  setSeries(equityChart, eq.map(p => fmtTs(p.ts)), eq.map(p => p.equity));
  $('#eqRange').textContent = eq.length ? fmtTs(eq[0].ts).slice(0, 5) + ' → ' +
    fmtTs(eq[eq.length - 1].ts).slice(0, 5) + ' · ' + eq.length + ' marks (IST)' : 'No recorded equity';
}
$$('.range-switch button').forEach(b => b.addEventListener('click', () => {
  equityDays = Number(b.dataset.days);
  $$('.range-switch button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  renderEquityHistory();
}));
async function refreshEquity() {
  if (!equityChart) return;   // offline: the boot banner already says so
  let eq;
  try { eq = await jget('/api/equity'); } catch { return; }
  equityHistory = Array.isArray(eq) ? eq : [];   // an empty book answers {rows: [], demo_only}
  renderEquityHistory();
}

async function refreshDecisions() {
  let ds;
  try { ds = await jget('/api/decisions?limit=30'); } catch { return; }
  renderDecisionRows($('#decisionFeed'), ds, d => (d.mode === 'demo' ? ' ' + tag('demo', 'demo') : ''));
}

/* engine controls */
async function engineAction(url, body, onDone, errTitle) {
  if (engineActionBusy || statsFailed || !lastStatsAt) return;
  engineActionBusy = true;
  $('#btnStart').disabled = $('#btnStop').disabled = true;
  try { onDone(await jpost(url, body)); }
  catch (e) { toastErr(errTitle, e); }
  finally { engineActionBusy = false; }
  refreshStats();
}
function startEngine() {
  const interval = parseInt($('#intervalSel').value, 10);
  engineAction('/api/engine/start', {interval}, r =>
    toast('Engine ' + (r.status === 'started' ? 'started' : r.status), 'Cycle interval ' + interval + ' s'),
  'Could not start the engine');
}
function stopEngine() {
  engineAction('/api/engine/stop', {}, r => {
    const stopping = r.status === 'stopping' || r.status === 'starting';
    toast(stopping ? 'Engine is finishing its cycle' : 'Engine stopped',
      stopping ? 'The current cycle must finish before another start or reset.' : 'Position management is stopped.');
  }, 'Could not stop the engine');
}
$('#intervalSel').addEventListener('change', async () => {
  const interval = parseInt($('#intervalSel').value, 10);
  try {
    const r = await jpost('/api/engine/interval', {interval: interval});
    toast('Cycle interval ' + interval + ' s', r.running
      ? 'Applies from the next cycle.' : 'Saved — used when the engine starts.');
  } catch (e) { toastErr('Could not change the interval', e); }
});
$('#btnStart').addEventListener('click', startEngine);
$('#btnStop').addEventListener('click', stopEngine);

/* manual pause/resume — one button that follows the flag's state. The copy
   must state the semantics every time it fires: entries-only, nothing is
   force-closed (an operator pausing in a panic must know what they did NOT
   just do to their open positions). */
async function togglePause() {
  const paused = $('#pauseBanner').classList.contains('show');
  try {
    if (!paused) {
      const r = await jpost('/api/trading/pause', {note: ''});
      toast('Entries paused', r.semantics || 'New entries are blocked; open positions are still managed.');
    } else {
      await jpost('/api/trading/resume', {});
      toast('Entries resumed', 'New entries are allowed again (all other risk gates still apply).');
    }
  } catch (e) { toastErr(paused ? 'Could not resume' : 'Could not pause', e); }
  refreshStats();
}
$('#btnPause').addEventListener('click', togglePause);

/* ===================================================== fast book
   The separate fast (5m, experimental) paper account: its own stats poll, equity
   chart, ALL-trades history table and decision feed — mode='hft' rows only,
   so the standard book's views never mix in fast-book records. */
let hftAutoResumeToasted = false;
let hftDefaultInterval = 10;
let hftMarket = '';
let hftRegisteredStrategies = [];
async function loadHftStrategies() {
  /* one source of truth for "what can run on the fast book": the Lab meta
     endpoint derives it from the strategy registry.
     NOTE: this used to hardcode one timeframe. When the book moved to 5m the
     lookup silently returned undefined and the filter went back to being
     empty — the exact bug it was written to fix. Read EVERY timeframe the
     book registers instead, so the next move cannot break it. */
  try {
    const meta = await jget('/api/lab/meta');
    const byTf = (meta.strategies || {}).hft || {};
    hftRegisteredStrategies = [...new Set([].concat(...Object.values(byTf)))];
    hftRegisteredStrategies = hftRegisteredStrategies.filter(x => x !== 'all' && x !== 'ensemble');
  } catch { /* the filter falls back to strategies seen in trades */ }
}
/* the price chart: close + EMA20 on a PRICE axis — the equity chart formats
   its axis as dollars, which is wrong for a cross like ETH/BTC (0.032) */
function buildHftPriceChart() {
  hftPriceChart = buildChart('#hftPriceChart', {type: 'line',
    data: {labels: [], datasets: [lineDataset('close'),
      lineDataset('EMA20', {fill: false, borderWidth: 1, borderDash: [4, 3], borderColor: cssVar('--color-muted')})]},
    options: chartOptions({yTick: v => fmtPx(v, hftMarket),
      tooltipLabel: c => ' ' + c.dataset.label + ' ' + fmtPx(c.parsed.y, hftMarket)})});
}
async function refreshHftPrice() {
  if (!hftPriceChart) return;
  let d;
  try { d = await jget('/api/hft/candles?limit=180' +
                       (hftMarket ? '&symbol=' + encodeURIComponent(hftMarket) : '')); }
  catch { d = null; }
  const bars = (d && d.bars) || [];
  $('#hftPriceEmpty').hidden = bars.length > 0;
  showChart('#hftPriceChart', bars.length > 0);
  $('#hftPriceEmpty').textContent = d ? 'No candles yet — the feed is warming up.'
    : 'Price feed unavailable — the exchange could not be reached. Retrying automatically.';
  if (!d) return;
  hftMarket = d.symbol || hftMarket;
  const sel = $('#hftMarketSel');
  if (sel.options.length !== (d.markets || []).length) {
    sel.innerHTML = (d.markets || []).map(m =>
      '<option value="' + esc(m) + '">' + esc(m) + '</option>').join('');
  }
  sel.value = hftMarket;
  if (!bars.length) return;
  const chg = d.change_pct || 0;
  /* the timeframe comes from the API: this label once named a bar size long
     after the book moved on — a chart that lies about its own bars */
  $('#hftPriceHint').textContent = bars.length + ' × ' + (d.timeframe || '') + ' bars · ' +
    (chg >= 0 ? '+' : '') + chg.toFixed(2) + '% over the window · last ' +
    fmtPx(bars[bars.length - 1].close, hftMarket);
  setSeries(hftPriceChart, bars.map(b => fmtTs(b.ts)), bars.map(b => b.close), bars.map(b => b.ema20));
}
$('#hftMarketSel').addEventListener('change', () => {
  hftMarket = $('#hftMarketSel').value; refreshHftPrice();
});

async function refreshHft() {
  let s;
  try { s = await jget('/api/hft/stats'); } catch { return; }
  hftDefaultInterval = s.interval || hftDefaultInterval;
  /* follow the ENGINE's cadence unless the operator is mid-choice */
  if (document.activeElement !== $('#hftIntervalSel') &&
      [...$('#hftIntervalSel').options].some(o => +o.value === hftDefaultInterval)) {
    $('#hftIntervalSel').value = String(hftDefaultInterval);
  }
  if (!hftRegisteredStrategies.length) loadHftStrategies();
  refreshHftPrice();
  $('#hftFeeHint').textContent = 'Fee tier ' + (s.fee_tier || 'perp') + ' · capital ' + fmt$(s.capital);
  const equity = s.broker_equity ?? s.current_equity ?? s.capital;
  renderStatCards($('#hftStats'), [
    ['Equity', fmt$(equity), posCls(equity - s.capital), 'from ' + fmt$(s.capital)],
    ['Realized P&L', fmtPnl(s.total_pnl), posCls(s.total_pnl), fmtPct(s.return_pct) + ' return'],
    ['Closed trades', String(s.closed_trades ?? 0), '', 'win rate ' + (s.win_rate ?? 0) + '%'],
    ['Profit factor', s.profit_factor == null ? '∞' : s.profit_factor, '',
      s.profit_factor == null ? 'no losses yet' : 'gross win ÷ gross loss'],
    ['Max drawdown', fmtPct(s.max_drawdown_pct), s.max_drawdown_pct ? 'neg' : '', 'peak-to-trough'],
    ['Open positions', String((s.positions || []).length), '', s.engine_running ? 'live marks' : 'from journal'],
    ['Cycles', String(s.cycles ?? 0), '', s.engine_running ? 'this run' : 'engine stopped'],
    ['Fees paid', fmt$(s.total_fees ?? 0), s.total_fees ? 'neg' : '', 'the cost of trading fast'],
  ]);

  $('#hftEngineDot').className = 'dot ' + (s.engine_running && !s.health_note ? 'on' : '');
  $('#hftPillText').textContent = s.engine_running ? (s.health_note ? 'Degraded' : 'Running') : 'Stopped';
  $('#hftStartBtn').disabled = !!s.engine_running;
  $('#hftStopBtn').disabled = !s.engine_running;
  const note = $('#hftEngineNote');
  note.hidden = !(s.last_error || s.health_note);
  note.textContent = s.last_error ? 'Last error: ' + s.last_error : (s.health_note || '');
  renderVetoes('#hftVetoBox', '#hftVetoSummary', '#hftVetoList', s.vetoes);
  renderStrategies('#hftStratBox', '#hftStratList', s.strategies);
  if (s.auto_resumed && !hftAutoResumeToasted) {
    hftAutoResumeToasted = true;
    toast('Fast book auto-resumed', 'The last session left it running.');
  }

  if (hftChart) {
    let eq;
    try { eq = await jget('/api/hft/equity'); } catch { eq = []; }
    if (!Array.isArray(eq)) eq = [];
    $('#hftEquityEmpty').hidden = eq.length > 1;
    showChart('#hftEquityChart', eq.length > 1);
    if (eq.length > 1) setSeries(hftChart, eq.map(p => fmtTs(p.ts)), eq.map(p => p.equity));
  }

  /* ALL fast-book trades — the one place for the fast-book history */
  let trades;
  try { trades = await jget('/api/hft/trades?limit=1000'); } catch { trades = []; }
  /* the picker lists every strategy REGISTERED for the fast book, not just the
     ones that happen to appear in the trade history — with an empty history
     (the normal state of a fresh book) it used to render a single option,
     so the control looked broken. */
  const sel = $('#hftStratFilter');
  const seen = trades.map(t => t.strategy).filter(Boolean);
  syncStrategyFilter(sel, [...new Set([...hftRegisteredStrategies, ...seen])].sort());
  const filter = sel.value;
  const rows = filter ? trades.filter(t => t.strategy === filter) : trades;
  $('#hftTradeEmpty').hidden = rows.length > 0;
  $('#hftTradeTable tbody').innerHTML = rows.map(tradeRow).join('');

  let ds;
  try { ds = await jget('/api/hft/decisions?limit=30'); } catch { ds = []; }
  renderDecisionRows($('#hftDecisions'), ds);
}
$('#hftStratFilter').addEventListener('change', refreshHft);
/* cadence is changeable while the book RUNS: both engine loops re-read their
   interval every cycle, so this lands on the next wake */
$('#hftIntervalSel').addEventListener('change', async () => {
  const interval = parseInt($('#hftIntervalSel').value, 10);
  hftDefaultInterval = interval;
  try {
    const r = await jpost('/api/hft/engine/interval', {interval: interval});
    toast('Fast book cadence ' + interval + ' s', r.running
      ? 'Applies from the next cycle.' : 'Saved — used when the book starts.');
  } catch (e) { toastErr('Could not change the interval', e); }
});
$('#hftStartBtn').addEventListener('click', async () => {
  try {
    const interval = parseInt($('#hftIntervalSel').value, 10) || hftDefaultInterval;
    hftDefaultInterval = interval;
    const r = await jpost('/api/hft/engine/start', {interval: interval});
    toast('Fast book ' + (r.status === 'started' ? 'started' : r.status),
          'Cycle interval ' + interval + ' s', r.status !== 'error');
  } catch (e) { toastErr('Could not start the fast book', e); }
  refreshHft();
});
$('#hftStopBtn').addEventListener('click', async () => {
  try {
    await jpost('/api/hft/engine/stop', {});
    toast('Fast book stopped');
  } catch (e) { toastErr('Could not stop the fast book', e); }
  refreshHft();
});

/* ===================================================== Strategy Lab
   Pick a pair -> apply the strategies registered for it -> backtest on real
   data, in BOTH books. The form options are derived from the server's
   registry (one source of truth), never hand-maintained in JS. */
let labMeta = null;
let labPollTimer = null;

async function refreshLab() {
  if (!labMeta) {
    try { labMeta = await jget('/api/lab/meta'); } catch { return; }
    labFormRefresh();
  }
  /* while a run is in flight a 1.5s poller owns the status */
  if (labPollTimer) return;
  const st = await jget('/api/lab/status').catch(() => null);
  if (st) labRenderStatus(st);
}

function labFormRefresh() {
  if (!labMeta) return;
  const book = $('#labBook').value, kind = $('#labKind').value;
  const tfs = (labMeta.timeframes[book] || {})[kind] || [];
  const tfPrev = $('#labTimeframe').value;
  $('#labTimeframe').innerHTML = tfs.map(t => '<option value="' + t + '">' + t + '</option>').join('');
  $('#labTimeframe').value = tfs.includes(tfPrev) ? tfPrev : (tfs.includes('1h') ? '1h' : tfs[0]);
  labStrategyRefresh();
  $('#labFeeWrap').hidden = book !== 'hft';
  /* suggestion chips for the picked market */
  const chips = (labMeta.suggestions || {})[kind] || [];
  $('#labChips').innerHTML = chips.length ? '<span class="hint">Try</span>' + chips.map(s =>
    '<button type="button" class="chip" data-sym="' + esc(s) + '">' + esc(s) + '</button>').join('') : '';
  const sym = $('#labSymbol');
  if (!sym.value || !chips.includes(sym.value) && sym.dataset.auto === '1') {
    sym.value = chips[0] || '';
    sym.dataset.auto = '1';
  }
  sym.placeholder = kind === 'crypto' ? 'BTC/USDT' : 'EURUSD=X';
}

function labStrategyRefresh() {
  const book = $('#labBook').value, tf = $('#labTimeframe').value;
  const registered = (labMeta.strategies[book] || {})[tf] || [];
  /* 'all' runs every registered strategy on one fetched frame (comparison) */
  const strats = registered.length > 1 ? ['all', ...registered] : registered;
  const prev = $('#labStrategy').value;
  $('#labStrategy').innerHTML = strats.map(s =>
    '<option value="' + esc(s) + '">' + (s === 'all' ? 'Compare all' : esc(s)) + '</option>').join('');
  $('#labStrategy').value = strats.includes(prev) ? prev : (strats[0] || '');
  $('#labRunBtn').disabled = !strats.length;
  labDaysRefresh();
}

function labDaysRefresh() {
  const book = $('#labBook').value, kind = $('#labKind').value, tf = $('#labTimeframe').value;
  const cap = ((labMeta.days_cap[book] || {})[kind] || {})[tf] || 365;
  const def = ((labMeta.days_default[book] || {})[kind] || {})[tf] || 180;
  const opts = [7, 14, 30, 60, 90, 180, 365, 730, 1825].filter(d => d <= cap);
  if (!opts.includes(def)) opts.push(def);
  opts.sort((a, b) => a - b);
  const prev = parseInt($('#labDays').value, 10);
  const pick = opts.includes(prev) ? prev : def;
  $('#labDays').innerHTML = opts.map(d =>
    '<option value="' + d + '"' + (d === pick ? ' selected' : '') + '>' + d + ' days</option>').join('');
  $('#labNote').textContent = 'History is capped at ' + cap + ' days for ' + tf + ' ' + kind + '.';
}

async function labRun() {
  const body = {
    book: $('#labBook').value, kind: $('#labKind').value,
    symbol: $('#labSymbol').value.trim(), timeframe: $('#labTimeframe').value,
    strategy: $('#labStrategy').value,
    days: parseInt($('#labDays').value, 10) || 0,
    fee_tier: $('#labBook').value === 'hft' ? $('#labFee').value : null,
  };
  if (!body.symbol) { toast('Pick a symbol first', 'Type one or tap a suggestion.', false); return; }
  $('#labRunBtn').disabled = true;
  try {
    await jpost('/api/lab/run', body);
    if (labPollTimer) clearInterval(labPollTimer);
    labPollTimer = setInterval(async () => {
      const st = await jget('/api/lab/status').catch(() => null);
      if (!st) return;
      labRenderStatus(st);
      if (st.status !== 'running') {
        clearInterval(labPollTimer); labPollTimer = null;
        $('#labRunBtn').disabled = false;
      }
    }, 1500);
  } catch (e) {
    $('#labRunBtn').disabled = false;
    toastErr('The Lab refused the run', e);
  }
  labRenderStatus(await jget('/api/lab/status').catch(() => ({})));
}

function labRenderStatus(st) {
  const el = $('#labStatus');
  el.hidden = !(st.status === 'running' || st.status === 'error');
  el.classList.toggle('err', st.status === 'error');
  if (st.status === 'running') el.textContent = 'Running — ' + (st.note || 'fetching data and replaying bars…');
  else if (st.status === 'error') el.textContent = 'Run failed: ' + (st.error || 'unknown error');
  else if (st.status === 'done' && $('#labResultTitle').dataset.run !== st.result.generated_at) {
    /* render ONCE per completed run: status stays 'done' on every later poll */
    labRenderResult(st.result);
    $('#labResultTitle').dataset.run = st.result.generated_at;
  }
}

function labRenderResult(r) {
  if (!r) return;
  const s = r.spec, st = r.stats;
  $('#labResultCard').hidden = false;
  $('#labResultTitle').textContent = s.symbol + ' · ' + s.timeframe + ' · ' + st.strategy;
  $('#labResultMeta').textContent = (s.book === 'hft' ? 'Fast book' : 'Standard book') +
    (s.fee_tier ? ' · ' + s.fee_tier + ' fees' : '') + ' · ' + r.bars + ' bars · ' +
    r.window.first.slice(0, 10) + ' → ' + r.window.last.slice(0, 10) +
    ' · taker round trip ≈ ' + r.taker_round_trip_bps + ' bp';
  renderStatCards($('#labStats'), [
    ['Return', fmtPct(st.return_pct), posCls(st.return_pct), 'after all costs'],
    ['P&L', fmtPnl(st.total_pnl), posCls(st.total_pnl), 'after all costs'],
    ['Trades', String(st.trades), '', 'win rate ' + st.win_rate_pct + '%'],
    ['Profit factor', st.profit_factor == null ? '∞' : st.profit_factor, '', 'gross win ÷ gross loss'],
    ['Max drawdown', fmtPct(st.max_drawdown_pct), st.max_drawdown_pct ? 'neg' : '', 'peak-to-trough'],
    ['Sharpe', st.sharpe == null ? '—' : st.sharpe, '', 'annualized'],
    ['Fees paid', fmt$(st.fees), st.fees ? 'neg' : '', 'both legs of every trade'],
  ]);
  $('#labCurveHint').textContent = r.equity_curve.length + ' points';
  if (labChart) setSeries(labChart, r.equity_curve.map(p => fmtTs(p.ts)), r.equity_curve.map(p => p.equity));
  $('#labExits').innerHTML = Object.entries(r.exit_reasons || {}).map(([k, v]) =>
    tag('tf', k + ' × ' + v)).join('') || '<span class="hint">No exits</span>';
  /* per-strategy P&L: one row for a single run, every strategy for a comparison */
  const cmp = r.comparison;
  const pnlRows = (cmp && cmp.length ? cmp : [st]).map(c => ({name: c.strategy, pnl: c.total_pnl, trades: c.trades}));
  $('#labPnlCard').hidden = false;
  renderBars($('#labStratBars'), pnlRows.sort((a, b) => b.pnl - a.pnl));
  $('#labCompareCard').hidden = !cmp;
  if (cmp) {
    $('#labCompareTable tbody').innerHTML = [...cmp].sort((a, b) => b.total_pnl - a.total_pnl).map(c =>
      '<tr><td class="strategy">' + esc(c.strategy) + '</td>' +
      '<td class="num ' + posCls(c.return_pct) + '">' + fmtPct(c.return_pct) + '</td>' +
      '<td class="num ' + posCls(c.total_pnl) + '">' + fmtPnl(c.total_pnl) + '</td>' +
      '<td class="num">' + c.trades + '</td>' +
      '<td class="num">' + c.win_rate_pct + '%</td>' +
      '<td class="num">' + esc(c.profit_factor == null ? '∞' : c.profit_factor) + '</td>' +
      '<td class="num ' + posCls(-Math.abs(c.max_drawdown_pct || 0)) + '">' + fmtPct(c.max_drawdown_pct) + '</td>' +
      '<td class="num">' + esc(c.sharpe == null ? '—' : c.sharpe) + '</td>' +
      '<td class="num">' + fmt$(c.fees) + '</td></tr>').join('');
  }
  /* trades of the best strategy (most recent first) */
  const trades = r.trades || [];
  $('#labTradesCard').hidden = trades.length === 0;
  $('#labTradesHint').textContent = trades.length + ' most recent of ' + st.trades;
  $('#labTradeTable tbody').innerHTML = trades.slice().reverse().map(t =>
    '<tr><td class="muted">' + esc(fmtTs(t.entry_ts)) + '</td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + fmtPnl(t.pnl) + '</td>' +
    '<td class="muted">' + esc(t.exit_reason || '—') + '</td></tr>').join('');
  toast('Backtest complete', st.strategy + ': ' + fmtPct(st.return_pct) + ' over ' + st.trades + ' trades',
    st.return_pct >= 0);
}

$('#labBook').addEventListener('change', labFormRefresh);
$('#labKind').addEventListener('change', labFormRefresh);
$('#labTimeframe').addEventListener('change', labStrategyRefresh);
$('#labSymbol').addEventListener('input', () => { $('#labSymbol').dataset.auto = '0'; });
$('#labChips').addEventListener('click', e => {
  const b = e.target.closest('[data-sym]');
  if (b) { $('#labSymbol').value = b.dataset.sym; $('#labSymbol').dataset.auto = '0'; }
});
$('#labRunBtn').addEventListener('click', labRun);

/* ===================================================== portfolio */
function renderPositions(s) {
  const ops = s.open_positions || [];
  $('#liveHint').textContent = s.engine_running ? 'Live marks from the running engine'
    : ops.length ? 'Engine stopped — prices are entry prices from the journal' : '';
  $('#posEmpty').hidden = ops.length > 0;
  $('#posTable tbody').innerHTML = ops.map(p => {
    const live = s.engine_running && p.live !== false;
    return '<tr>' +
      '<td class="symbol">' + esc(p.symbol) + ' ' + tag('tf', p.timeframe) + '</td>' +
      '<td>' + sideTag(p.side) + '</td>' +
      '<td class="num">' + fmtQty(p.qty) + '</td>' +
      '<td class="num">' + fmtPx(p.entry, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.mark, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.stop, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.target, p.symbol) + '</td>' +
      '<td class="strategy">' + esc(p.strategy) + '</td>' +
      '<td class="num">' + (p.bars_held ?? 0) + '</td>' +
      '<td class="num ' + posCls(p.unrealized) + '">' +
        (p.unrealized != null ? fmtPnl(p.unrealized) : (live ? '…' : '—')) + '</td>' +
      '<td>' + (live ? '<button class="btn btn-ghost small" title="Review and close position" aria-label="Close ' +
        esc(p.symbol) + ' ' + esc(p.timeframe) + '" data-close="' + esc(p.symbol) + '|' + esc(p.timeframe) +
        '">Close</button>' : '') + '</td></tr>';
  }).join('');
}
let pendingClose = null;
$('#posTable').addEventListener('click', e => {
  const btn = e.target.closest('[data-close]');
  if (!btn) return;
  const [symbol, timeframe] = btn.dataset.close.split('|');
  pendingClose = {symbol, timeframe};
  $('#closeMessage').textContent = 'Close ' + symbol + ' (' + timeframe + ') at the latest available price?';
  openDialog('#closeModal', '#closeCancel');
});
$('#closeCancel').addEventListener('click', () => closeDialog('#closeModal'));
$('#closeModal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeDialog('#closeModal');
});
$('#closeGo').addEventListener('click', async () => {
  if (!pendingClose || $('#closeGo').disabled) return;
  const {symbol, timeframe} = pendingClose;
  $('#closeGo').disabled = true;
  $('#closeGo').textContent = 'Closing…';
  $('#closeModal').dataset.busy = 'true';
  try {
    const r = await jpost('/api/positions/close', {symbol, timeframe});
    toast('Position closed', symbol + ' ' + timeframe + ' @ ' + fmtPx(r.exit_price, symbol));
    pendingClose = null;
    $('#closeModal').dataset.busy = 'false';
    closeDialog('#closeModal');
  } catch (err) {
    toastErr('Close failed', err);
  } finally {
    $('#closeModal').dataset.busy = 'false';
    $('#closeGo').disabled = false;
    $('#closeGo').textContent = 'Close position';
  }
  refreshStats();
});

let tradeHistory = [], tradePage = 0, tradeHistoryLoaded = false;
const TRADE_PAGE_SIZE = 25;
function renderTradeHistory() {
  const strategy = $('#stratFilter').value;
  const status = $('#tradeStatus').value;
  const query = $('#tradeSearch').value.trim().toLowerCase();
  const rows = tradeHistory.filter(t => (!strategy || t.strategy === strategy) &&
    (!status || t.status === status) && (!query ||
      [t.symbol, t.strategy, t.exit_reason].some(value => String(value || '').toLowerCase().includes(query))));
  tradePage = Math.max(0, Math.min(tradePage, Math.ceil(rows.length / TRADE_PAGE_SIZE) - 1));
  const start = tradePage * TRADE_PAGE_SIZE;
  const pageRows = rows.slice(start, start + TRADE_PAGE_SIZE);
  $('#tradeEmpty').hidden = !tradeHistoryLoaded || rows.length > 0;
  $('#tradeEmpty').textContent = tradeHistory.length
    ? 'No trades match these filters.'
    : 'No trades recorded yet. Review the watchlist, then start the paper engine.';
  $('#tradeClear').hidden = !(strategy || status || query);
  $('#tradePrev').disabled = tradePage === 0;
  $('#tradeNext').disabled = start + TRADE_PAGE_SIZE >= rows.length;
  if (tradeHistoryLoaded) $('#tradeCount').textContent =
    (rows.length ? (start + 1) + '–' + (start + pageRows.length) : '0') +
    ' of ' + rows.length + (rows.length !== tradeHistory.length ? ' matching' : ' trades') +
    (tradeHistory.length >= 1000 ? ' (latest 1,000 loaded)' : '');
  $('#tradeTable tbody').innerHTML = pageRows.map(tradeRow).join('');
}
async function refreshTrades() {
  let trades;
  try { trades = await jget('/api/trades?limit=1000'); }
  catch {
    if (!tradeHistoryLoaded) $('#tradeCount').textContent = 'Unable to load trade history. Retrying automatically…';
    return;
  }
  tradeHistory = trades;
  tradeHistoryLoaded = true;
  const sel = $('#stratFilter');
  // Preserve a selected strategy even if it falls outside the latest 1,000 rows.
  const strategies = [...new Set(trades.map(t => t.strategy).filter(Boolean))];
  if (sel.value && !strategies.includes(sel.value)) strategies.push(sel.value);
  syncStrategyFilter(sel, strategies.sort());
  renderTradeHistory();
}
function filterTradeHistory() { tradePage = 0; renderTradeHistory(); }
$('#tradeSearch').addEventListener('input', filterTradeHistory);
$('#stratFilter').addEventListener('change', filterTradeHistory);
$('#tradeStatus').addEventListener('change', filterTradeHistory);
$('#tradeClear').addEventListener('click', () => {
  $('#tradeSearch').value = '';
  $('#stratFilter').value = '';
  $('#tradeStatus').value = '';
  filterTradeHistory();
  $('#tradeSearch').focus();
});
$('#tradePrev').addEventListener('click', () => { tradePage--; renderTradeHistory(); });
$('#tradeNext').addEventListener('click', () => { tradePage++; renderTradeHistory(); });

/* ===================================================== evidence */
/* read-only render of the GENERATED artifacts: data/results/*.json
   (validate/shadow), data/kronos_ic.json, data/manifest.json — the view
   computes nothing from trade data, it presents what the CLI wrote */
let EV_REPORTS = [];
/* the shipped empty-state copy, kept so a failure render can be undone */
const KR_EMPTY_HTML = 'No resolved forecasts yet — run <code>python3 main.py kronos</code> ' +
  'and let its horizons resolve.';
const CV_EMPTY_HTML = 'No validation reports in this data directory — run <code>make validate</code>.';

function evidenceFailed(err) {
  /* the view used to mark itself loaded BEFORE the request and swallow the
     failure, so one 401 (an unentered token, a rotated one) left every panel
     on "loading…" FOREVER — it never retried and never said why. */
  const msg = /401/.test(String(err && err.message))
    ? 'not authorized — enter your dashboard token, then reopen this view'
    : 'could not load the evidence artifacts: ' + esc(String(err && err.message || err));
  const box = '<div class="empty">' + msg + '</div>';
  $('#evShadow').innerHTML = box;
  $('#evManifest').innerHTML = box;
  $('#krEmpty').hidden = false;
  $('#krEmpty').innerHTML = msg;
  $('#cvEmpty').hidden = false;
  $('#cvEmpty').innerHTML = msg;
  $('#evCards').innerHTML = '';
}

async function refreshEvidence() {
  let ev;
  try {
    ev = await jget('/api/evidence');
  } catch (e) {
    evidenceFailed(e);        // NOT marked loaded: the next visit retries
    return;
  }
  evLoaded.v = true;          // only a SUCCESSFUL load counts as loaded

  /* --- Kronos rolling IC vs its own hurdle --- */
  const k = ev.kronos || {};
  const S = k.series || [];
  const labels = S.map(p => p.i);
  if (kronosChart) { kronosChart.destroy(); kronosChart = null; }
  /* the ledger pools every market and timeframe ever forecast, which can
     manufacture rank correlation, so the pooled IC is shown as context and
     the per-market verdict (main.py kronos) leads */
  const v = k.verdict;
  const verdictTxt = v ? 'Verdict: ' + v.market + ', ' + v.forecasts + ' forecasts, IC ' +
    Number(v.ic).toFixed(3) + ' → ' + (v.promoted ? 'promoted' : 'not promoted') + ' (' + v.date + '). ' : '';
  $('#krMeta').textContent = k.error ? 'Ledger unavailable: ' + k.error
    : k.n ? verdictTxt + 'Chart: ' + k.n + ' resolved forecasts, all markets and horizons pooled · pooled IC ' +
      (k.ic == null ? '—' : Number(k.ic).toFixed(3)) + ' is context, not a verdict (pooling unlike markets inflates rank IC) · ' +
      'offline research only — it does not vote in live trading' + (k.note ? '. Note: ' + k.note : '')
    : 'Rolling rank-IC against its promotion hurdle';
  $('#krEmpty').innerHTML = KR_EMPTY_HTML;      // restore after a failure render
  $('#krEmpty').hidden = labels.length > 0;
  showChart('#kronosChart', labels.length > 0);
  if (labels.length && typeof Chart !== 'undefined') {
    const flat = (v, color, dash, label) => ({label, data: labels.map(() => v), borderColor: color,
      borderDash: dash, pointRadius: 0, borderWidth: 1, fill: false});
    kronosChart = buildChart('#kronosChart', {type: 'line',
      data: {labels, datasets: [
        {label: 'rolling IC', data: S.map(p => p.ic), borderColor: cssVar('--chart-line'),
         pointRadius: 0, borderWidth: 1.75, tension: .25, fill: false},
        flat(k.hurdle ?? 0.02, cssVar('--color-pos'), [6, 4], 'promotion hurdle'),
        flat(k.demote_below ?? 0, cssVar('--color-neg'), [4, 4], 'demotion floor')]},
      options: chartOptions({legend: true, yTick: v => Number(v).toFixed(2)})});
  }

  /* --- validation reports: selector + verdict cards + path chart --- */
  EV_REPORTS = ev.validations || [];
  const sel = $('#evReportSel');
  const cur = sel.value;
  sel.innerHTML = (EV_REPORTS.length ? '' : '<option value="">No reports yet</option>') +
    EV_REPORTS.map((r, i) => '<option value="' + i + '">' +
      esc((r.symbol || '?') + ' ' + (r.timeframe || '') + ' · ' + (r.strategy || '?') + ' · ' +
          (r.start ? r.start + ' → ' + (r.end || 'now') : (r.days || '?') + ' days')) + '</option>').join('');
  sel.value = (cur !== '' && Number(cur) < EV_REPORTS.length) ? cur : (EV_REPORTS.length ? '0' : '');
  renderEvidenceReport();

  /* --- shadow adherence --- */
  const sh = ev.shadow;
  if (!sh || !sh.profile) {
    $('#evShadow').innerHTML = '<div class="empty">No shadow report yet — run ' +
      '<code>python3 main.py shadow</code>.</div>';
  } else {
    const rows = Object.entries(sh.symbols || {}).map(([name, s]) =>
      '<tr><td class="symbol">' + esc(name) + '</td>' +
      '<td class="num">' + esc(s.adherence_pct) + '%</td>' +
      '<td class="num">' + esc(s.on_rule) + '</td>' +
      '<td class="num">' + esc(s.late) + '</td>' +
      '<td class="num ' + (s.rule_breaks ? 'neg' : '') + '">' + esc(s.rule_breaks) + '</td>' +
      '<td class="num">' + esc(s.unknown) + '</td></tr>').join('');
    const p = sh.profile;
    $('#evShadow').innerHTML = '<table><thead><tr><th>Market</th><th class="num">Adherence</th>' +
      '<th class="num">On rule</th><th class="num">Late</th><th class="num">Rule breaks</th>' +
      '<th class="num">Unknown</th></tr></thead><tbody>' + (rows ||
        '<tr><td colspan="6" class="empty">No auditable trades in the report.</td></tr>') +
      '</tbody></table><p class="note">' + esc(p.n_trades) + ' closed trades · win rate ' +
      esc(p.win_rate_pct) + '% · ' + esc(p.n_blew_through_stop) + ' blew through their stop · ' +
      'disposition gap ' + esc(p.disposition_gap_hours) + ' h</p>';
  }

  /* --- pinned-data manifest --- */
  const man = ev.manifest || {};
  const keys = Object.keys(man);
  $('#evManifest').innerHTML = keys.length
    ? '<table><thead><tr><th>Market</th><th class="num">Bars</th><th>Window</th>' +
      '<th>Source</th><th>SHA-256</th><th>Fetched</th></tr></thead><tbody>' +
      keys.map(kk => { const m = man[kk];
        return '<tr><td class="symbol">' + esc(kk) + '</td>' +
          '<td class="num">' + esc(m.bars) + '</td>' +
          '<td class="muted">' + esc(String(m.first_ts).slice(0, 10) + ' → ' + String(m.last_ts).slice(0, 10)) + '</td>' +
          '<td>' + esc(m.source) + '</td>' +
          '<td class="mono muted">' + esc(String(m.sha256).slice(0, 12)) + '…</td>' +
          '<td class="muted">' + esc(String(m.fetched_at).slice(0, 10)) + '</td></tr>';
      }).join('') + '</tbody></table>'
    : '<div class="empty">No pinned fetches yet — run a backtest with ' +
      '<code>--start YYYY-MM-DD --end YYYY-MM-DD</code>.</div>';
}

function renderEvidenceReport() {
  const r = EV_REPORTS[$('#evReportSel').value] || null;
  renderStatCards($('#evCards'), r ? [
    ['PBO', r.pbo ? r.pbo.pbo : '—',
      r.pbo && r.pbo.pbo >= 0.5 ? 'neg' : (r.pbo && r.pbo.pbo < 0.35 ? 'pos' : ''),
      r.pbo ? r.pbo.verdict : 'needs a ≥2-strategy family'],
    ['Deflated Sharpe', r.deflated_sharpe ? r.deflated_sharpe.deflated_sharpe : '—',
      r.deflated_sharpe && r.deflated_sharpe.deflated_sharpe >= 0.95 ? 'pos' : '',
      r.deflated_sharpe ? r.deflated_sharpe.verdict : 'pass --trial-sharpes'],
    ['MC terminal p5', r.monte_carlo && r.monte_carlo.n_sims
        ? '$' + r.monte_carlo.terminal_p5.toLocaleString() : '—', '',
      '5th-percentile resampled outcome'],
    ['MinTRL', r.min_trl && r.min_trl.min_bars ? r.min_trl.min_years + ' y' : '—', '',
      r.min_trl && r.min_trl.min_bars
        ? Number(r.min_trl.min_bars).toLocaleString() + ' OOS bars @ 95%' : 'needs a positive Sharpe'],
    ['Backtest return', r.backtest ? fmtPct(r.backtest.return_pct) : '—',
      r.backtest ? posCls(r.backtest.return_pct) : '',
      r.backtest ? r.backtest.trades + ' trades · Sharpe ' + r.backtest.sharpe : ''],
  ] : [
    ['PBO', '—', '', 'run make validate'], ['Deflated Sharpe', '—', '', 'run make validate'],
    ['MC terminal p5', '—', '', 'run make validate'], ['MinTRL', '—', '', 'run make validate'],
    ['Backtest return', '—', '', 'no reports yet'],
  ]);

  if (cvChart) { cvChart.destroy(); cvChart = null; }
  const paths = r && r.purged_cv ? (r.purged_cv.paths || []) : [];
  $('#cvEmpty').innerHTML = CV_EMPTY_HTML;      // restore after a failure render
  $('#cvEmpty').hidden = paths.some(p => p.trades);
  showChart('#cvChart', paths.length > 0);
  if (!paths.length || typeof Chart === 'undefined') return;
  cvChart = buildChart('#cvChart', {type: 'bar',
    data: {labels: paths.map((p, i) => 'p' + (i + 1) + (p.trades ? '' : ' ·')),
      datasets: [{label: 'OOS return %', borderRadius: 3,
        data: paths.map(p => p.trades ? p.return_pct : null),
        backgroundColor: paths.map(p => p.trades
          ? (p.return_pct >= 0 ? cssVar('--color-pos') : cssVar('--color-neg'))
          : cssVar('--color-border-strong'))}]},
    options: chartOptions({yTick: v => v + '%', tooltipLabel: c => ' ' + fmtPct(c.parsed.y), xGrid: false})});
}
$('#evReportSel').addEventListener('change', renderEvidenceReport);

/* ===================================================== watchlist */
async function refreshWatchlist() {
  let specs;
  try { specs = await jget('/api/watchlist'); } catch { return; }
  $('#wlCount').textContent = specs.length + ' of 12 markets';
  $('#wlEmpty').hidden = specs.length > 0;
  $('#wlGrid').innerHTML = specs.map(s =>
    '<div class="wl-item"><div>' +
    '<div class="sy">' + esc(s.symbol) + ' ' + tag('tf', s.timeframe) + '</div>' +
    (s.display && s.display !== s.symbol ? '<div class="disp">' + esc(s.display) + '</div>' : '') +
    '<div class="meta">' + tag(s.kind, s.kind) + (s.strategies || []).map(n => tag('tf', n)).join('') + '</div>' +
    '</div><button class="icon-btn" title="Remove from watchlist" aria-label="Remove ' + esc(s.symbol) + ' ' +
    esc(s.timeframe) + '" data-del="' + esc(s.kind) + '|' + esc(s.symbol) + '|' + esc(s.timeframe) + '">' +
    icon('trash') + '</button></div>').join('');
}
const deleteSpec = s => jdel('/api/watchlist/' + s.kind + '/' + encodeURIComponent(s.symbol) + '/' + s.timeframe);
$('#wlGrid').addEventListener('click', async e => {
  const btn = e.target.closest('[data-del]');
  if (!btn) return;
  const [kind, symbol, timeframe] = btn.dataset.del.split('|');
  try {
    const r = await deleteSpec({kind, symbol, timeframe});
    toast('Removed from watchlist', symbol + ' ' + timeframe + ' · ' + r.count + ' markets remain');
  } catch (err) { toastErr('Remove failed', err); }
  refreshWatchlist();
});

async function addSpecs(specs) {
  const errors = [];
  let added = 0;
  for (const s of specs) {
    try { await jpost('/api/watchlist', s); added++; }
    catch (err) { errors.push(s.symbol + ' ' + s.timeframe + ': ' + err.message); }
  }
  if (added) toast('Watchlist updated', added + ' market' + (added > 1 ? 's' : '') + ' added');
  errors.forEach(m => toast('Skipped', m, false));
  refreshWatchlist();
}

$('#wlForm').addEventListener('submit', e => {
  e.preventDefault();
  const kind = $('#wlKind').value, symbol = $('#wlSymbol').value.trim(),
        timeframe = $('#wlTf').value, display = $('#wlDisplay').value.trim();
  if (!symbol) { toast('Symbol required', 'e.g. BTC/USDT or EURUSD=X', false); return; }
  addSpecs([{kind, symbol, timeframe, display: display || null}]).then(() => {
    $('#wlSymbol').value = ''; $('#wlDisplay').value = ''; $('#wlSymbol').focus();
  });
});

const spec = (kind, symbol, timeframe, display = null) => ({kind, symbol, timeframe, display});
const PRESETS = {
  'crypto-majors': () => ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'].flatMap(s =>
    ['1h', '15m', '4h'].map(tf => spec('crypto', s, tf))),
  'forex-majors': () => ['EURUSD=X', 'GBPUSD=X', 'USDJPY=X'].map(s => spec('forex', s, '1h')),
};
const DEFAULT_WATCHLIST = [
  spec('crypto', 'BTC/USDT', '1h', 'Bitcoin'), spec('crypto', 'ETH/USDT', '1h', 'Ethereum'),
  spec('crypto', 'SOL/USDT', '1h', 'Solana'), spec('crypto', 'BTC/USDT', '15m', 'Bitcoin (scalp)'),
  spec('crypto', 'ETH/USDT', '15m', 'Ethereum (scalp)'), spec('crypto', 'BTC/USDT', '4h', 'Bitcoin (mean-rev)'),
  spec('crypto', 'ETH/USDT', '4h', 'Ethereum (mean-rev)'), spec('forex', 'EURUSD=X', '1h', 'EUR/USD'),
  spec('forex', 'GBPUSD=X', '1h', 'GBP/USD')];
$$('[data-preset]').forEach(b => b.addEventListener('click', async () => {
  if (b.dataset.preset !== 'reset-default') { addSpecs(PRESETS[b.dataset.preset]()); return; }
  /* replace the list with the shipped default: clear, then re-add */
  let cur;
  try { cur = await jget('/api/watchlist'); } catch (err) { toastErr('Restore failed', err); return; }
  for (const s of cur) {
    try { await deleteSpec(s); } catch (err) { toastErr('Remove failed', err); }
  }
  addSpecs(DEFAULT_WATCHLIST);
}));

/* ===================================================== account */
async function refreshAccount() {
  let a;
  try { a = await jget('/api/account'); } catch { return; }
  const pnl = a.equity - a.capital;
  $('#balanceBig').textContent = fmt$(a.equity);
  $('#balanceMode').textContent = (a.engine_running ? 'Engine running' : 'Engine stopped') +
    (z(pnl) ? ' · ' + fmtPnl(pnl) + ' vs start capital' : '');
  $('#balCash').textContent = fmt$(a.cash);
  const u = $('#balUnreal');
  u.textContent = a.engine_running ? fmtPnl(a.unrealized) : '—';
  u.className = posCls(a.unrealized);
  $('#balCapital').textContent = fmt$(a.capital);
  $('#balLast').textContent = a.last_equity ? fmtTs(a.last_equity.ts) + ' IST' : '—';
}

/* deposit/withdrawal ledger — typed rows beat scraping add_equity notes */
async function refreshTransactions() {
  let txs;
  try { txs = await jget('/api/account/transactions'); } catch { return; }
  $('#txnEmpty').hidden = txs.length > 0;
  $('#txnTable tbody').innerHTML = txs.map(t => '<tr>' +
    '<td class="muted">' + esc(fmtTs(t.ts)) + '</td>' +
    '<td>' + tag(t.kind, t.kind) + '</td>' +
    '<td class="num ' + (t.kind === 'withdrawal' ? 'neg' : 'pos') + '">' +
      fmtPnl(t.kind === 'withdrawal' ? -t.amount : t.amount) + '</td>' +
    '<td class="num">' + fmt$(t.cash_after) + '</td>' +
    '<td class="num">' + fmt$(t.equity_after) + '</td>' +
    '<td class="muted">' + esc(t.note || '') + '</td></tr>').join('');
}

function parseAmount(v) {
  const n = Number(String(v).replace(/[$,\s]/g, ''));
  if (!isFinite(n) || isNaN(n) || n <= 0) return null;
  return Math.round(n * 100) / 100;
}
async function submitAmount(url, input, label) {
  const amount = parseAmount(input.value);
  if (amount == null) { toast('Invalid amount', 'Enter a positive number, e.g. 500', false); return; }
  try {
    const r = await jpost(url, {amount});
    toast(label + ' complete', (label === 'Deposit' ? '+' : '-') + fmt$(amount) + ' · cash now ' + fmt$(r.cash));
    input.value = '';
    refreshAccount(); refreshTransactions();
  } catch (e) { toastErr(label + ' failed', e); }
}
$('#depositForm').addEventListener('submit', e => {
  e.preventDefault(); submitAmount('/api/account/deposit', $('#depositAmt'), 'Deposit');
});
$('#withdrawForm').addEventListener('submit', e => {
  e.preventDefault(); submitAmount('/api/account/withdraw', $('#withdrawAmt'), 'Withdrawal');
});

/* reset modal */
const resetModal = $('#resetModal');
$('#btnResetOpen').addEventListener('click', () => {
  $('#resetConfirm').value = '';
  $('#resetGo').disabled = true;
  $('#resetCapital').value = $('#resetCapital').value || '10000';
  openDialog('#resetModal', '#resetConfirm');
});
$('#resetCancel').addEventListener('click', () => closeDialog('#resetModal'));
resetModal.addEventListener('click', e => { if (e.target === resetModal) closeDialog('#resetModal'); });
$('#resetConfirm').addEventListener('input', e => {
  $('#resetGo').disabled = e.target.value.trim() !== 'RESET';
});
$('#resetGo').addEventListener('click', async () => {
  if (resetModal.dataset.busy === 'true') return;
  const capital = parseAmount($('#resetCapital').value);
  if (capital == null) { toast('Invalid capital', 'Enter a positive number', false); return; }
  resetModal.dataset.busy = 'true';
  $('#resetGo').disabled = true;
  try {
    const r = await jpost('/api/account/reset', {capital});
    toast('Account reset', 'New capital ' + fmt$(r.capital) +
      (r.backup ? ' · backup ' + r.backup.split('/').pop() : ''));
    resetModal.dataset.busy = 'false';
    closeDialog('#resetModal');
    $('#resetConfirm').value = '';
    refreshAccount(); refreshTransactions(); refreshStats(); refreshEquity();
  } catch (e) { toastErr('Reset failed', e); }
  finally {
    resetModal.dataset.busy = 'false';
    $('#resetGo').disabled = $('#resetConfirm').value.trim() !== 'RESET';
  }
});

/* ===================================================== chat */
function addMsg(text, role) {
  const log = $('#chatlog');
  const d = document.createElement('div');
  d.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
async function loadChatHistory() {
  chatLoaded.v = true;
  let hist;
  try { hist = await jget('/api/chat'); } catch { chatLoaded.v = false; return; }
  $('#chatlog').innerHTML = '';
  hist.forEach(m => addMsg(m.content, m.role === 'user' ? 'user' : 'bot'));
  if (!hist.length) addMsg('Ask me about the bot\'s record — what it earned, why it took a trade, ' +
    'which strategy works best, or how the risk rules work. Answers come from the journal, so I cannot invent trades.', 'bot');
}
async function sendChat(msg) {
  addMsg(msg, 'user');
  const typing = document.createElement('div');
  typing.className = 'msg bot typing';
  typing.innerHTML = '<i></i><i></i><i></i>';
  $('#chatlog').appendChild(typing);
  $('#chatlog').scrollTop = $('#chatlog').scrollHeight;
  try {
    const r = await jpost('/api/chat', {message: msg});
    addMsg(r.reply, 'bot');
  } catch (err) {
    addMsg('Could not reach the journal: ' + err.message, 'bot');
  } finally { typing.remove(); }
}
$('#chatform').addEventListener('submit', e => {
  e.preventDefault();
  const msg = $('#chatbox').value.trim();
  if (!msg) return;
  $('#chatbox').value = '';
  sendChat(msg);
});
$$('.quick-chip').forEach(chip => chip.addEventListener('click', () => sendChat(chip.dataset.q)));

/* ===================================================== polling */
function activeView() { return $('.view.active').id.replace('view-', ''); }
setInterval(() => { if (!document.hidden) refreshVisible(activeView()); }, 4000);
setInterval(updateFreshness, 1000);
$('#refreshNow').addEventListener('click', () => {
  evLoaded.v = false;
  refreshVisible(activeView());
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshVisible(activeView());
});

/* ===================================================== boot
   Chart.js is VENDORED (served from this app at /chart.umd.min.js, no CDN
   call): on venue/offline Wi-Fi the SPA works regardless. If the vendored
   library somehow fails to load we say so and render everything else —
   the charts stay empty instead of killing routing, polling and every
   button listener with a ReferenceError. */
if (typeof Chart === 'undefined') {
  const banner = document.createElement('div');
  banner.className = 'banner banner-warn show';
  banner.textContent = 'Charts could not load — everything else works normally.';
  $('main').prepend(banner);
} else {
  Chart.defaults.font.family = cssVar('--font-sans');
  equityChart = buildEquityLine('#equityChart', 'Equity');
  hftChart = buildEquityLine('#hftEquityChart', 'Fast book equity');
  labChart = buildEquityLine('#labEquityChart', 'Backtest equity');
  buildHftPriceChart();
}
/* sync the switcher + browser chrome with the theme <head> already applied */
applyTheme(document.documentElement.getAttribute('data-theme') || 'light', false);
setView(location.hash.slice(1) || 'overview');
