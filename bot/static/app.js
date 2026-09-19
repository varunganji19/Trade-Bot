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
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
/* read a CSS custom property off :root — lets the Chart.js canvas follow
   the active theme (light/dark) without rebuilding it */
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
/* Sticky navigation follows the real header height, including wrapped mobile controls. */
function measureHeader() {
  document.documentElement.style.setProperty('--topbar-height', $('.topbar').offsetHeight + 'px');
}
if (typeof ResizeObserver !== 'undefined') new ResizeObserver(measureHeader).observe($('.topbar'));
window.addEventListener('resize', measureHeader);
measureHeader();

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
                     catch (e) { return ''; } };
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
  catch (e) { /* private mode: token just won't persist */ }
  closeDialog('#tokenGate');
  location.reload();   // re-boot the pollers with the header attached
});
$('#tokenGate').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeDialog('#tokenGate');
});

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
  try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) {
    const msg = (data && data.detail) ? data.detail : (r.status + ' ' + r.statusText);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    err.data = data;   // full error body (the market-switch 409 carries
                       // open_positions + requires_confirm the caller reads)
    throw err;
  }
  return data;
}
const jpost = (u, b) => jreq(u, 'POST', b);
const jdel = u => jreq(u, 'DELETE');

/* ===================================================== toasts */
const ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
const ICON_ERR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
function toast(title, msg, ok = true) {
  const d = document.createElement('div');
  d.className = 'toast' + (ok ? '' : ' err');
  d.innerHTML = (ok ? ICON_OK : ICON_ERR) +
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
  $('.engine-pill').classList.remove('warn');
});

$('#tokenCancel').addEventListener('click', () => closeDialog('#tokenGate'));

/* ===================================================== routing */
const VIEWS = ['overview', 'portfolio', 'hft', 'watchlist', 'lab', 'evidence', 'account', 'chat'];
const VIEW_COPY = {
  overview: ['Portfolio overview', 'Your performance, positions and trading activity at a glance.'],
  portfolio: ['Positions & trade history', 'Follow open exposure and inspect the decisions behind each trade.'],
  hft: ['Fast book', 'A separate 5-minute paper account with its own capital and fee tier.'],
  watchlist: ['Your market watchlist', 'Choose the markets and timeframes your paper engine follows.'],
  lab: ['Strategy Lab', 'Test a strategy on historical data before putting it to work.'],
  evidence: ['Research & evidence', 'Check validation results, model quality and rule adherence.'],
  account: ['Paper account', 'Manage your simulated balance and review account transactions.'],
  chat: ['Ask your trading journal', 'Explore your results, strategy decisions and risk rules.']
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
  $('#pageTitle').textContent = VIEW_COPY[name][0];
  $('#pageDescription').textContent = VIEW_COPY[name][1];
  $('#workspaceKicker').textContent = name === 'hft' ? 'HIGH-FREQUENCY PAPER ACCOUNT · IST' : 'STANDARD PAPER ACCOUNT · IST';
  const selected = $('#tab-' + name);
  $('#tabs').scrollLeft = Math.max(0, selected.offsetLeft - $('#tabs').clientWidth / 2 + selected.offsetWidth / 2);
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.title = 'Algo Bot — ' + name[0].toUpperCase() + name.slice(1);
  refreshVisible(name);
  /* smooth ride back to the top when swapping views (auto under reduced motion) */
  if (reduceMotion) window.scrollTo(0, 0);
  else window.scrollTo({top: 0, behavior: 'smooth'});
}
window.addEventListener('hashchange', () => setView(location.hash.slice(1) || 'overview'));
$('#tabs').addEventListener('click', e => { const t = e.target.closest('.tab'); if (t) setView(t.dataset.view); });
$$('[data-goto]').forEach(b => b.addEventListener('click', () => setView(b.dataset.goto)));
$('.skip-link').addEventListener('click', e => { e.preventDefault(); $('#mainContent').focus(); });
/* only #tabs is wired — the duplicated mobile nav is gone from the DOM, and a
   listener on a null element would throw at boot and kill this whole script */

/* first-load skeletons already in the DOM; data replaces them on first poll */
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

/* ===================================================== theme
   data-theme is already on <html> (set pre-paint in <head>) — this
   section only wires the switcher, mirrors state onto the buttons,
   updates the mobile browser chrome color and recolors the chart. */
const THEME_KEY = 'algo-theme';
const THEMES = ['light', 'dark'];   // legacy stored 'black' maps to dark at boot
function applyChartTheme() {
  for (const chart of [equityChart, hftChart, hftPriceChart, labChart]) {
    if (!chart) continue;
    const ds = chart.data.datasets[0];
    ds.borderColor = cssVar('--chart-line');
    ds.backgroundColor = cssVar('--chart-fill');
    const tt = chart.options.plugins.tooltip;
    tt.backgroundColor = cssVar('--color-card');
    tt.borderColor = cssVar('--color-border');
    tt.titleColor = cssVar('--color-foreground');
    tt.bodyColor = cssVar('--color-muted-foreground');
    const tick = cssVar('--color-muted-foreground'), grid = cssVar('--chart-grid');
    ['x', 'y'].forEach(ax => {
      chart.options.scales[ax].ticks.color = tick;
      chart.options.scales[ax].grid.color = grid;
    });
    chart.update('none');
  }
}
function applyTheme(t, persist) {
  if (!THEMES.includes(t)) t = 'light';
  /* the Evidence charts are built once per session — drop them on a theme
     switch so the next visit rebuilds in the new palette (they used to keep
     stale colors until reload) */
  if (evLoaded.v) {
    if (kronosChart) { kronosChart.destroy(); kronosChart = null; }
    if (cvChart) { cvChart.destroy(); cvChart = null; }
    evLoaded.v = false;
  }
  document.documentElement.setAttribute('data-theme', t);
  if (persist) { try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* private mode */ } }
  $$('.theme-switch button').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.theme === t)));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', cssVar('--color-background'));
  applyChartTheme();
}
$$('.theme-switch button').forEach(b =>
  b.addEventListener('click', () => applyTheme(b.dataset.theme, true)));

/* ---- shared render helpers (the Overview/HFT/Lab tabs render the same
   payload shapes; these were four copy-paste blocks each) ---- */
function syncStrategyFilter(sel, strategies) {
  const current = sel.value;
  if (sel.options.length - 1 !== strategies.length ||
      [...sel.options].slice(1).map(o => o.value).join(',') !== strategies.join(',')) {
    sel.innerHTML = '<option value="">all strategies</option>' +
      strategies.map(x => '<option value="' + esc(x) + '">' + esc(x) + '</option>').join('');
    sel.value = current;
  }
}
function renderStrategies(boxSel, listSel, st) {
  /* a book with no voting strategy cannot trade, and that must not look
     like a quiet market */
  if (!st || !st.registered) return;
  const box = $(boxSel);
  box.hidden = false;
  const voting = st.voting || [], silent = st.silent || [];
  /* the gate can be OFF without anything looking wrong — the verdicts file
     lives under the active data directory, so switching directories returns
     it to "everything votes" in silence. Say which state it is in, first. */
  const g = st.gate || {};
  const gateRow = g.state === 'no_evidence'
    ? '<div class="veto-row top"><span class="r">promotion gate: UNMEASURED</span>' +
      '<span class="n" style="font-weight:400">' + esc(g.why || '') + '</span></div>'
    : (g.state === 'active'
        ? '<div class="veto-row"><span class="r" style="color:var(--color-muted-foreground)">' +
          'promotion gate: active</span><span class="n" style="font-weight:400;' +
          'color:var(--color-muted-foreground)">' + esc(g.why || '') +
          ' · ' + esc(g.generated_at || '') + '</span></div>'
        : '');
  const head = voting.length
    ? '<div class="veto-row"><span class="r">voting</span><span class="n">' +
      esc(voting.join(', ')) + '</span></div>'
    : '<div class="veto-row top"><span class="r">NO strategy can trade this book</span>' +
      '<span class="n">0 / ' + st.registered + '</span></div>';
  const rest = silent.map(x =>
    '<div class="veto-row"><span class="r" style="color:var(--color-muted-foreground)">' +
    esc(x.name) + '</span><span class="n" style="font-weight:400;color:var(--color-muted-foreground)">' +
    esc(x.why) + '</span></div>').join('');
  $(listSel).innerHTML = gateRow + head + rest;
}
function renderVetoes(boxSel, sumSel, listSel, v) {
  /* "the engine is running" and "the engine is trading" are different
     claims; this box is the second one. */
  const box = $(boxSel);
  if (!v || !v.attempts) { box.hidden = true; return; }
  box.hidden = false;
  const blocked = v.attempts - v.approved;
  $(sumSel).textContent = v.approved + ' approved / ' + v.attempts + ' attempted';
  const rows = (v.by_reason || []).slice(0, 6);
  $(listSel).innerHTML = rows.length
    ? rows.map((r, i) => '<div class="veto-row' + (i === 0 && !v.approved ? ' top' : '') +
        '"><span class="r">' + esc(r.reason.replace(/_/g, ' ')) + '</span>' +
        '<span class="n">' + r.count + '</span></div>').join('')
    : '<div class="veto-row"><span class="r">no entries blocked</span><span class="n">0</span></div>';
  if (blocked && !v.approved) {
    $(listSel).innerHTML += '<div class="veto-row" style="margin-top:6px"><span class="r" ' +
      'style="color:var(--color-muted-foreground)">every entry so far was refused — ' +
      'the top blocker above is why</span><span class="n"></span></div>';
  }
}
function renderStatCards(el, cards) {
  el.innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + STAT_ICON + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');
}
function renderDecisionRows(el, rows, extraBadge) {
  if (!rows.length) { el.innerHTML = '<div class="empty" style="padding:16px">No decisions journaled yet.</div>'; return; }
  el.innerHTML = rows.map(d => {
    const a = (d.action || '').toLowerCase();
    return '<div class="term-row">' +
      '<span class="term-ts">' + esc(fmtTs(d.ts)) + '</span>' +
      '<div class="term-body"><div class="term-line">' +
      '<span class="tag ' + esc(a === 'hold' ? 'hold' : a) + '">' + esc(d.action) + '</span>' +
      '<span class="term-mkt">' + esc(d.symbol) + ' <span class="tag tf">' + esc(d.timeframe) + '</span>' +
      (extraBadge ? extraBadge(d) : '') + '</span>' +
      '<span class="term-meta">regime ' + esc(d.regime || '—') + ' · conf ' +
        Math.round((d.confidence || 0) * 100) + '% · @ ' + fmtPx(d.price, d.symbol) + '</span>' +
      '</div><div class="term-why">' + esc(d.rationale || '') + '</div></div></div>';
  }).join('');
  el.scrollTop = 0;
}
function renderBars(el, rows) {
  const entries = rows.filter(r => Math.abs(r.pnl || 0) > 0 || r.trades > 0);
  if (!entries.length) {
    el.innerHTML = '<div class="empty" style="padding:16px">No closed trades yet.</div>';
    return;
  }
  const maxAbs = Math.max(...entries.map(r => Math.abs(r.pnl || 0)), 1);
  el.innerHTML = entries.map(r => {
    const pos = (r.pnl || 0) >= 0;
    const w = Math.max(2, Math.abs(r.pnl || 0) / maxAbs * 100);
    const title = r.name + ': ' + r.trades + ' trades' + (r.wins != null ? ', ' + r.wins + ' wins' : '');
    return '<div class="sbar" title="' + esc(title) + '">' +
      '<span class="name">' + esc(r.name) + '</span>' +
      '<span class="track"><span class="fill ' + (pos ? 'pos' : 'neg') +
      '" style="width:' + w.toFixed(1) + '%"></span></span>' +
      '<span class="val ' + (pos ? 'pos' : 'neg') + '">' + fmtPnl(r.pnl) + '</span></div>';
  }).join('');
}

/* ===================================================== overview */
function buildLineChart(canvasSel, label) {
  /* the three equity curves (Overview / HFT / Lab) are the same chart: one
     factory, three calls — colors come from the live theme's CSS variables */
  return new Chart($(canvasSel), {
    type: 'line',
    data: {labels: [], datasets: [{label: label, data: [],
      borderColor: cssVar('--chart-line'), backgroundColor: cssVar('--chart-fill'),
      fill: true, tension: .15, pointRadius: 0, borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : {duration: 250},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: cssVar('--color-card'),
        borderColor: cssVar('--color-border'), borderWidth: 1,
        titleColor: cssVar('--color-foreground'), bodyColor: cssVar('--color-muted-foreground'),
        titleFont: {family: 'Fira Code'}, bodyFont: {family: 'Fira Code'},
        callbacks: {label: c => ' ' + fmt$(c.parsed.y)}}},
      scales: {x: {ticks: {maxTicksLimit: 8, color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10}},
                   grid: {color: cssVar('--chart-grid')}},
               y: {ticks: {color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10},
                           callback: v => '$' + v.toLocaleString()},
                   grid: {color: cssVar('--chart-grid')}}}}
  });
}
let equityChart = null;
function buildEquityChart() {
  equityChart = buildLineChart('#equityChart', 'Equity');
}

const STAT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>';
let statsBusy = false, lastStatsAt = 0, statsFailed = false, engineActionBusy = false;
function updateFreshness() {
  const age = lastStatsAt ? Math.floor((Date.now() - lastStatsAt) / 1000) : 0;
  const stale = statsFailed || (lastStatsAt && age > 15);
  $('#connectionState').dataset.state = stale ? 'error' : lastStatsAt ? 'ok' : 'loading';
  $('#connectionState').textContent = stale ? 'Connection lost · data may be stale'
    : lastStatsAt ? (age < 5 ? 'Updated just now' : 'Updated ' + age + 's ago') : 'Connecting…';
  $('#connectionBanner').hidden = !stale;
  $('#connectionBanner').classList.toggle('show', !!stale);
  if (stale) {
    $('#enginePillText').textContent = 'Engine: status unavailable';
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
  catch (e) { statsFailed = true; updateFreshness(); return; }
  finally { statsBusy = false; }
  lastStatsAt = Date.now(); statsFailed = false; updateFreshness();
  const cards = [
    ['Equity', fmt$(s.current_equity), s.return_pct > 0 ? 'pos' : s.return_pct < 0 ? 'neg' : '',
      'from ' + fmt$(s.start_equity)],
    ['Total P&L', fmtPnl(s.total_pnl), s.total_pnl > 0 ? 'pos' : s.total_pnl < 0 ? 'neg' : '',
      fmtPct(s.return_pct) + ' return'],
    ['Open positions', String((s.open_positions || []).length), '',
      s.engine_running ? 'marked by the engine' : 'saved in your journal'],
    ['Max drawdown', s.max_drawdown_pct ? fmtPct(-Math.abs(s.max_drawdown_pct)) : '0.00%',
      s.max_drawdown_pct ? 'neg' : '', 'largest peak-to-trough decline'],
  ];
  renderStatCards($('#ovStats'), cards);
  const secondary = [
    ['Win rate', s.closed_trades ? (s.win_rate ?? 0) + '%' : '—'],
    ['Profit factor', !s.closed_trades ? '—' : s.profit_factor == null ? '∞' : s.profit_factor],
    ['Closed trades', s.closed_trades ?? 0],
    ['Markets watched', s.watchlist_count ?? 0]
  ];
  $('#overviewSecondary').innerHTML = secondary.map(([label, value]) =>
    '<div class="secondary-metric"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>').join('');

  const demoN = (s.trade_modes || {}).demo || 0;
  const paperN = (s.trade_modes || {}).paper || 0;
  $('#firstRun').hidden = !!(s.engine_running || paperN || demoN || (s.open_positions || []).length);
  $('#ovDemoNote').innerHTML = demoN
    ? '<span class="tag demo">demo</span> ' + demoN + ' seeded backtest-replay trades (mode=demo): ' +
      (paperN
        ? 'badged in the history and excluded from these headline stats, the equity curve, chatbot and shadow answers'
        : 'no paper trades yet — showing the demo record until the engine trades') +
      '. python3 main.py shadow --include-demo audits the replay rows.'
    : '';

  const lifecycle = s.engine_state || (s.engine_running ? 'running' : 'stopped');
  const state = s.engine_running ? (s.health_note ? 'Needs attention' : s.entries_halted ? 'Daily loss halt'
    : s.paused ? 'Entries paused' : 'Running') : lifecycle === 'starting' ? 'Starting…'
    : lifecycle === 'stopping' ? 'Stopping…' : 'Stopped';
  $('#engineDot').className = 'dot ' + (s.engine_running && !s.paused && !s.entries_halted && !s.health_note ? 'on' : '');
  $('#enginePillText').textContent = 'Standard engine: ' + state;
  /* health_note: degraded-but-alive (e.g. an open position behind a dead feed).
     It rode only in /api/engine/status before — nothing rendered it, so the
     pill stayed green on stage while a position sat unguarded. */
  const hb = $('#healthBanner');
  if (s.health_note && s.health_note !== healthDismissedMsg) healthDismissed = false;
  if (s.health_note && !healthDismissed) {
    hb.classList.add('show');
    $('#healthMsg').textContent = s.health_note;
    $('.engine-pill').classList.add('warn');
    $('.engine-pill').title = s.health_note;
  } else {
    hb.classList.remove('show');
    $('.engine-pill').classList.remove('warn');
    $('.engine-pill').title = '';
  }
  if (s.auto_resumed && !autoResumeToasted) {
    autoResumeToasted = true;
    toast('Engine auto-resumed', 'the last session left it running — it is paper-trading now (stop it from the top bar)');
  }
  $('#engineStateText').textContent = state;
  $('#engineStateText').className = 'st ' + (s.engine_running && !s.paused && !s.entries_halted ? 'pos' : '');
  $('#engineStateSub').textContent = (s.cycles ?? 0) + ' cycles · watchlist ' +
    (s.watchlist_count ?? 0) + ' specs';
  renderVetoes('#vetoBox', '#vetoSummary', '#vetoList', s.vetoes);
  renderStrategies('#stratBox', '#stratList', s.strategies);
  const transitioning = lifecycle === 'starting' || lifecycle === 'stopping';
  $('#btnStart').disabled = engineActionBusy || !!s.engine_running || transitioning;
  $('#btnStop').disabled = engineActionBusy || !s.engine_running || transitioning;
  /* the Interval select used to be DISABLED whenever the engine ran, so the
     cadence could only be changed by stopping and restarting the engine (a
     full rebuild: Kronos probe, book lease, position restore). The loop now
     re-reads its interval every cycle, so the control stays live. */
  $('#intervalSel').disabled = transitioning;
  if (document.activeElement !== $('#intervalSel') && s.interval &&
      [...$('#intervalSel').options].some(o => +o.value === s.interval)) {
    $('#intervalSel').value = String(s.interval);
  }

  /* manual pause: banner + button flip. Reported for a stopped engine too —
     the flag is a file that outlives any engine run, and a pause must never
     be lost by stopping/starting the engine. */
  const pb = $('#pauseBanner');
  if (s.paused) {
    pb.classList.add('show');
    $('#pauseNoteBox').textContent = s.paused_note ? ' Note: ' + s.paused_note + '.' : '';
    $('#pauseLabel').textContent = 'Resume trading';
  } else {
    pb.classList.remove('show');
    $('#pauseLabel').textContent = 'Pause trading';
  }

  /* market mode rides the same poll (W1's paused pattern): the banner +
     toggle must follow the PERSISTED mode within one 4s tick — a restart
     can never silently flip markets on the operator. */

  renderPositions(s);
  renderStratBars(s.by_strategy || {});
}

function renderStratBars(by) {
  renderBars($('#stratBars'), Object.entries(by)
    .map(([name, v]) => ({name, pnl: v.pnl, trades: v.trades, wins: v.wins})));
}

let equityHistory = [], equityDays = 0;
function renderEquityHistory() {
  if (!equityChart) return;
  const lastTime = equityHistory.length ? Date.parse(equityHistory[equityHistory.length - 1].ts) : 0;
  const eq = equityDays ? equityHistory.filter(p => Date.parse(p.ts) >= lastTime - equityDays * 86400000) : equityHistory;
  $('#equityEmpty').hidden = eq.length > 0;
  $('#equityChart').hidden = !eq.length;
  equityChart.data.labels = eq.map(p => fmtTs(p.ts));
  equityChart.data.datasets[0].data = eq.map(p => p.equity);
  equityChart.update(reduceMotion ? 'none' : undefined);
  $('#eqRange').textContent = eq.length ? fmtTs(eq[0].ts).slice(0, 5) + ' → ' +
    fmtTs(eq[eq.length - 1].ts).slice(0, 5) + ' · ' + eq.length + ' observations' : 'No recorded equity';
}
$$('.range-switch button').forEach(b => b.addEventListener('click', () => {
  equityDays = Number(b.dataset.days);
  $$('.range-switch button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  renderEquityHistory();
}));
async function refreshEquity() {
  if (!equityChart) return;   // offline: the boot banner already says so
  let eq;
  try { eq = await jget('/api/equity'); } catch (e) { return; }
  equityHistory = eq;
  renderEquityHistory();
}

async function refreshDecisions() {
  let ds;
  try { ds = await jget('/api/decisions?limit=30'); } catch (e) { return; }
  renderDecisionRows($('#decisionFeed'), ds, d => (d.mode === 'demo' ? ' <span class="tag demo">demo</span>' : ''));
}

/* engine controls */
async function startEngine() {
  if (engineActionBusy || statsFailed || !lastStatsAt) return;
  engineActionBusy = true;
  $('#btnStart').disabled = $('#btnStop').disabled = true;
  const interval = parseInt($('#intervalSel').value, 10);
  try {
    const r = await jpost('/api/engine/start', {interval});
    toast('Engine ' + (r.status === 'started' ? 'started' : r.status),
          'cycle interval ' + interval + 's', true);
    addMsg('[engine] ' + r.status + ' — interval ' + interval + 's', 'bot');
  } catch (e) { toastErr('Could not start engine', e); }
  finally { engineActionBusy = false; }
  refreshStats();
}
async function stopEngine() {
  if (engineActionBusy || statsFailed || !lastStatsAt) return;
  engineActionBusy = true;
  $('#btnStart').disabled = $('#btnStop').disabled = true;
  try {
    const r = await jpost('/api/engine/stop', {});
    const stopping = r.status === 'stopping' || r.status === 'starting';
    toast(stopping ? 'Engine is finishing its work' : 'Engine stopped',
      stopping ? 'The current cycle must finish before another start or reset.' : 'Position management is stopped.');
    addMsg('[engine] ' + r.status, 'bot');
  } catch (e) { toastErr('Could not stop engine', e); }
  finally { engineActionBusy = false; }
  refreshStats();
}
$('#intervalSel').addEventListener('change', async () => {
  const interval = parseInt($('#intervalSel').value, 10);
  try {
    const r = await jpost('/api/engine/interval', {interval: interval});
    toast('Cycle interval ' + interval + 's', r.running
      ? 'applies from the next cycle' : 'saved — used when the engine starts');
    addMsg('[engine] interval -> ' + interval + 's', 'bot');
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
  if (!paused) {
    try {
      const r = await jpost('/api/trading/pause', {note: ''});
      toast('Trading paused', r.semantics || 'blocks new entries only');
      addMsg('[engine] trading paused — new entries blocked, open positions still managed', 'bot');
    } catch (e) { toastErr('Could not pause', e); }
  } else {
    try {
      await jpost('/api/trading/resume', {});
      toast('Trading resumed', 'new entries are allowed again (all other risk gates still apply)');
      addMsg('[engine] trading resumed — new entries allowed again', 'bot');
    } catch (e) { toastErr('Could not resume', e); }
  }
  refreshStats();
}
$('#btnPause').addEventListener('click', togglePause);

/* ===================================================== HFT book
   The separate high-frequency paper account: its own stats poll, equity
   chart, ALL-trades history table and decision feed — mode='hft' rows only,
   so the standard book's pages above never mix in HFT records. */
let hftChart = null, hftPriceChart = null;
let hftAutoResumeToasted = false;
let hftDefaultInterval = 10;
let hftMarket = '';
let hftRegisteredStrategies = [];
async function loadHftStrategies() {
  /* one source of truth for "what can run on the fast book": the Lab meta
     endpoint derives it from the strategy registry.
     NOTE: this used to hardcode ['1m']. When the book moved to 5m the lookup
     silently returned undefined and the filter went back to being empty —
     the exact bug it was written to fix. Read EVERY timeframe the book
     registers instead, so the next move cannot break it. */
  try {
    const meta = await jget('/api/lab/meta');
    const byTf = (meta.strategies || {}).hft || {};
    hftRegisteredStrategies = [...new Set([].concat(...Object.values(byTf)))];
    hftRegisteredStrategies = hftRegisteredStrategies.filter(x => x !== 'all' && x !== 'ensemble');
  } catch (e) { /* the filter falls back to strategies seen in trades */ }
}
function buildHftEquityChart() {
  hftChart = buildLineChart('#hftEquityChart', 'HFT equity');
}
/* the 1m price chart: two series (close + EMA20) and a PRICE axis — the
   equity factory formats its axis as dollars, which is wrong for a cross
   like ETH/BTC (0.032). Before this card the HFT page could only show a
   flat equity line, so a running engine looked identical to a dead market. */
function buildHftPriceChart() {
  hftPriceChart = new Chart($('#hftPriceChart'), {
    type: 'line',
    data: {labels: [], datasets: [
      {label: 'close', data: [], borderColor: cssVar('--chart-line'),
       backgroundColor: cssVar('--chart-fill'), fill: true, tension: .15,
       pointRadius: 0, borderWidth: 2},
      {label: 'EMA20', data: [], borderColor: cssVar('--color-blue'),
       fill: false, tension: .15, pointRadius: 0, borderWidth: 1,
       borderDash: [4, 3]}]},
    options: {responsive: true, maintainAspectRatio: false,
      animation: reduceMotion ? false : {duration: 200},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: cssVar('--color-card'),
        borderColor: cssVar('--color-border'), borderWidth: 1,
        titleColor: cssVar('--color-foreground'), bodyColor: cssVar('--color-muted-foreground'),
        titleFont: {family: 'Fira Code'}, bodyFont: {family: 'Fira Code'},
        callbacks: {label: c => ' ' + c.dataset.label + ' ' + fmtPx(c.parsed.y, hftMarket)}}},
      scales: {x: {ticks: {maxTicksLimit: 8, color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10}},
                   grid: {color: cssVar('--chart-grid')}},
               y: {ticks: {color: cssVar('--color-muted-foreground'),
                           font: {family: 'Fira Code', size: 10},
                           callback: v => fmtPx(v, hftMarket)},
                   grid: {color: cssVar('--chart-grid')}}}}
  });
}
async function refreshHftPrice() {
  if (!hftPriceChart) return;
  let d;
  try { d = await jget('/api/hft/candles?limit=180' +
                       (hftMarket ? '&symbol=' + encodeURIComponent(hftMarket) : '')); }
  catch (e) { return; }
  hftMarket = d.symbol || hftMarket;
  const sel = $('#hftMarketSel');
  if (sel.options.length !== (d.markets || []).length) {
    sel.innerHTML = (d.markets || []).map(m =>
      '<option value="' + esc(m) + '">' + esc(m) + '</option>').join('');
  }
  sel.value = hftMarket;
  const bars = d.bars || [];
  $('#hftPriceEmpty').hidden = bars.length > 0;
  if (!bars.length) return;
  const chg = d.change_pct || 0;
  /* the timeframe comes from the API: this label said "1m" long after the
     book moved to 5m — a chart that lies about its own bars */
  $('#hftPriceHint').textContent = bars.length + ' × ' + esc(d.timeframe || '') + ' · ' +
    (chg >= 0 ? '+' : '') + chg.toFixed(2) + '% over the window · last ' +
    fmtPx(bars[bars.length - 1].close, hftMarket);
  hftPriceChart.data.labels = bars.map(b => fmtTs(b.ts));
  hftPriceChart.data.datasets[0].data = bars.map(b => b.close);
  hftPriceChart.data.datasets[1].data = bars.map(b => b.ema20);
  hftPriceChart.update(reduceMotion ? 'none' : undefined);
}
$('#hftMarketSel').addEventListener('change', () => {
  hftMarket = $('#hftMarketSel').value; refreshHftPrice();
});

async function refreshHft() {
  let s;
  try { s = await jget('/api/hft/stats'); } catch (e) { return; }
  hftDefaultInterval = s.interval || hftDefaultInterval;
  /* follow the ENGINE's cadence unless the operator is mid-choice */
  if (document.activeElement !== $('#hftIntervalSel') &&
      [...$('#hftIntervalSel').options].some(o => +o.value === hftDefaultInterval)) {
    $('#hftIntervalSel').value = String(hftDefaultInterval);
  }
  if (!hftRegisteredStrategies.length) loadHftStrategies();
  refreshHftPrice();
  $('#hftFeeHint').textContent = 'fee tier: ' + (s.fee_tier || 'perp') +
    ' · capital ' + fmt$(s.capital) + ' · 5m bars';
  const cards = [
    ['Equity', fmt$(s.broker_equity ?? s.current_equity ?? s.capital),
      (s.broker_equity ?? 0) > s.capital ? 'pos' : (s.broker_equity ?? 0) < s.capital ? 'neg' : '',
      'HFT book · start ' + fmt$(s.capital)],
    ['Total P&L', fmtPnl(s.total_pnl), s.total_pnl > 0 ? 'pos' : s.total_pnl < 0 ? 'neg' : '',
      fmtPct(s.return_pct) + ' return'],
    ['Closed trades', String(s.closed_trades ?? 0), '', 'win rate ' + (s.win_rate ?? 0) + '%'],
    ['Profit factor', s.profit_factor == null ? '∞' : s.profit_factor, '',
      s.profit_factor == null ? 'no losses yet' : 'gross win ÷ loss'],
    ['Max drawdown', fmtPct(s.max_drawdown_pct), 'neg', 'peak-to-trough'],
    ['Open positions', String((s.positions || []).length), '',
      s.engine_running ? 'live marks' : 'from journal'],
    ['Cycles', String(s.cycles ?? 0), '', s.engine_running ? 'running' : 'engine stopped'],
    ['Total fees', fmt$(s.total_fees ?? 0), 'neg', 'the HFT cost autopsy'],
  ];
  renderStatCards($('#hftStats'), cards);

  $('#hftEngineDot').className = 'dot ' + (s.engine_running ? 'on' : 'off');
  $('#hftPillText').textContent = 'hft: ' +
    (s.engine_running ? (s.health_note ? 'degraded' : 'running') : 'stopped');
  $('#hftStartBtn').disabled = !!s.engine_running;
  $('#hftStopBtn').disabled = !s.engine_running;
  const note = $('#hftEngineNote');
  if (s.last_error) { note.hidden = false; note.textContent = 'last error: ' + s.last_error; }
  else if (s.health_note) { note.hidden = false; note.textContent = s.health_note; }
  else note.hidden = true;
  renderVetoes('#hftVetoBox', '#hftVetoSummary', '#hftVetoList', s.vetoes);
  renderStrategies('#hftStratBox', '#hftStratList', s.strategies);
  if (s.auto_resumed && !hftAutoResumeToasted) {
    hftAutoResumeToasted = true;
    toast('HFT book auto-resumed', 'the last session left it running (stop it from the top bar)');
  }

  if (hftChart) {
    let eq;
    try { eq = await jget('/api/hft/equity'); } catch (e) { eq = []; }
    $('#hftEquityEmpty').hidden = eq.length > 0;
    if (eq.length) {
      hftChart.data.labels = eq.map(p => fmtTs(p.ts));
      hftChart.data.datasets[0].data = eq.map(p => p.equity);
      hftChart.update(reduceMotion ? 'none' : undefined);
    }
  }

  /* ALL HFT trades — the one place for the high-frequency history */
  let trades;
  try { trades = await jget('/api/hft/trades?limit=1000'); } catch (e) { trades = []; }
  /* the picker lists every strategy REGISTERED for the fast book, not just the
     ones that happen to appear in the trade history — with an empty history
     (which is the normal state of a fresh book) it used to render a single
     "all strategies" option, so the control looked broken. */
  const sel = $('#hftStratFilter');
  const seen = trades.map(t => t.strategy).filter(Boolean);
  syncStrategyFilter(sel, [...new Set([...hftRegisteredStrategies, ...seen])].sort());
  const filter = sel.value;
  const rows = filter ? trades.filter(t => t.strategy === filter) : trades;
  const tbody = $('#hftTradeTable tbody');
  $('#hftTradeEmpty').hidden = rows.length > 0;
  tbody.innerHTML = rows.map(t => '<tr>' +
    '<td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.opened_ts)) + '</td>' +
    '<td class="mono"><b>' + esc(t.symbol) + '</b> <span class="tag tf">' + esc(t.timeframe || '') + '</span></td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + (t.status === 'CLOSED' ? fmtPnl(t.pnl) : '—') + '</td>' +
    '<td class="mono" style="color:var(--color-blue)">' + esc(t.strategy) + '</td>' +
    '<td><span class="tag ' + (t.status === 'OPEN' ? 'open' : 'hold') + '">' + esc(t.status) + '</span></td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.exit_reason || '—') + '</td></tr>').join('');

  /* decision feed (HOLDs included) */
  let ds;
  try { ds = await jget('/api/hft/decisions?limit=30'); } catch (e) { ds = []; }
  renderDecisionRows($('#hftDecisions'), ds);
}
$('#hftStratFilter').addEventListener('change', refreshHft);
/* cadence is changeable while the book RUNS: both engine loops re-read their
   interval every cycle, so this lands on the next wake instead of needing a
   stop/start (the select used to be a start-time-only value). */
$('#hftIntervalSel').addEventListener('change', async () => {
  const interval = parseInt($('#hftIntervalSel').value, 10);
  hftDefaultInterval = interval;
  try {
    const r = await jpost('/api/hft/engine/interval', {interval: interval});
    toast('HFT cadence ' + interval + 's', r.running
      ? 'applies from the next cycle' : 'saved — used when the book starts');
  } catch (e) { toastErr('Could not change the HFT interval', e); }
});

async function startHftEngine() {
  try {
    const interval = parseInt($('#hftIntervalSel').value, 10) || hftDefaultInterval;
    hftDefaultInterval = interval;
    const r = await jpost('/api/hft/engine/start', {interval: interval});
    toast('HFT engine ' + (r.status === 'started' ? 'started' : r.status),
          'cycle interval ' + interval + 's · 5m bars', r.status !== 'error');
    addMsg('[hft] engine started — interval ' + interval + 's', 'bot');
  } catch (e) { toastErr('Could not start HFT engine', e); }
  refreshHft();
}
async function stopHftEngine() {
  try {
    await jpost('/api/hft/engine/stop', {});
    toast('HFT engine stopped', '', true);
    addMsg('[hft] engine stopped', 'bot');
  } catch (e) { toastErr('Could not stop HFT engine', e); }
  refreshHft();
}
$('#hftStartBtn').addEventListener('click', startHftEngine);
$('#hftStopBtn').addEventListener('click', stopHftEngine);

/* ===================================================== Strategy Lab
   Pick any stock/pair -> apply the strategies registered for it -> backtest
   on real data. Works for BOTH books (standard + HFT) via the book toggle.
   The form options are derived from the server's registry (one source of
   truth), never hand-maintained in JS. */
let labChart = null;
let labMeta = null;
let labPollTimer = null;

function buildLabEquityChart() {
  labChart = buildLineChart('#labEquityChart', 'Lab equity');
}

async function refreshLab() {
  if (!labMeta) {
    try { labMeta = await jget('/api/lab/meta'); } catch (e) { return; }
    labFormRefresh();
  }
  /* while a run is in flight, poll its status (the form's 4s poll is too
     slow for a 10s backtest — poll fast ONLY while running) */
  if (labPollTimer) return;   // the 1.5s run-poller owns status while active
  const st = await jget('/api/lab/status').catch(() => null);
  if (st) labRenderStatus(st);
}

function labFormRefresh() {
  if (!labMeta) return;
  const book = $('#labBook').value, kind = $('#labKind').value;
  const tfs = (labMeta.timeframes[book] || {})[kind] || [];
  const sym = $('#labSymbol').value;
  const tfPrev = $('#labTimeframe').value;
  $('#labTimeframe').innerHTML = tfs.map(t => '<option value="' + t + '">' + t + '</option>').join('');
  $('#labTimeframe').value = tfs.includes(tfPrev) ? tfPrev : tfs[0];
  labStrategyRefresh();
  labDaysRefresh();
  $('#labFeeWrap').hidden = book !== 'hft';
  /* suggestion chips for the picked market */
  const chips = (labMeta.suggestions || {})[kind] || [];
  $('#labChips').innerHTML = chips.map(s =>
    '<button type="button" class="tag tf" style="cursor:pointer;border:1px solid var(--color-border)"' +
    ' data-sym="' + esc(s) + '">' + esc(s) + '</button>').join('');
  if (!sym) $('#labSymbol').value = chips[0] || '';
  $('#labSymbol').placeholder = kind === 'crypto' ? 'BTC/USDT'
    : kind === 'forex' ? 'EURUSD' : 'RELIANCE or ^NSEI';
}

function labStrategyRefresh() {
  const book = $('#labBook').value, tf = $('#labTimeframe').value;
  const strats = (labMeta.strategies[book] || {})[tf] || [];
  const prev = $('#labStrategy').value;
  $('#labStrategy').innerHTML = strats.map(s =>
    '<option value="' + esc(s) + '">' + (s === 'all' ? 'ALL strategies (comparison)' : esc(s)) + '</option>').join('');
  if (strats.includes(prev)) $('#labStrategy').value = prev;
  const cap = ((labMeta.days_cap[book] || {})[$('#labKind').value] || {})[tf];
  const note = [];
  if (cap != null) note.push('history cap ' + cap + 'd');
  if (tf === '1m' && $('#labKind').value !== 'crypto') note.push('yfinance caps 1m at 7d');
  $('#labNote').textContent = note.join(' · ');
}

function labDaysRefresh() {
  const book = $('#labBook').value, kind = $('#labKind').value, tf = $('#labTimeframe').value;
  const cap = ((labMeta.days_cap[book] || {})[kind] || {})[tf] || 365;
  const def = ((labMeta.days_default[book] || {})[kind] || {})[tf] || 180;
  const opts = [1, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650].filter(d => d <= cap);
  if (!opts.includes(def)) opts.push(def);
  opts.sort((a, b) => a - b);
  const prev = parseInt($('#labDays').value, 10);
  $('#labDays').innerHTML = opts.map(d =>
    '<option value="' + d + '"' + (d === (prev || def) ? ' selected' : '') + '>' + d + ' days</option>').join('');
}

async function labRun() {
  const body = {
    book: $('#labBook').value, kind: $('#labKind').value,
    symbol: $('#labSymbol').value.trim(), timeframe: $('#labTimeframe').value,
    strategy: $('#labStrategy').value,
    days: parseInt($('#labDays').value, 10) || 0,
    fee_tier: $('#labBook').value === 'hft' ? $('#labFee').value : null,
  };
  if (!body.symbol) { toastErr('Pick a symbol first', 'type one or tap a suggestion chip'); return; }
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
    toastErr('Lab refused the run', e);
  }
  labRenderStatus(await jget('/api/lab/status').catch(() => ({})));
}

function labRenderStatus(st) {
  const el = $('#labStatus');
  el.hidden = false;
  if (st.status === 'running') {
    el.textContent = '⏳ ' + (st.note || 'running…');
  } else if (st.status === 'error') {
    el.textContent = '✗ ' + (st.error || 'run failed');
  } else if (st.status === 'done') {
    /* hide the status pill ALWAYS; render ONCE per completed run (status
       stays 'done' on every later poll — re-rendering re-fired the
       completion toast and left stale status text visible every 4s tick) */
    el.hidden = true;
    if ($('#labResultTitle').dataset.run !== st.result.generated_at) {
      labRenderResult(st.result);
      $('#labResultTitle').dataset.run = st.result.generated_at;
    }
  }
}

function labRenderResult(r) {
  if (!r) return;
  const s = r.spec;
  $('#labResultCard').hidden = false;
  $('#labResultTitle').textContent = s.symbol + ' ' + s.timeframe + ' · ' + r.stats.strategy;
  $('#labResultMeta').textContent = '[' + s.book + (s.fee_tier ? ' · ' + s.fee_tier : '') +
    '] ' + r.bars + ' bars · ' + r.window.first.slice(0, 10) + ' → ' + r.window.last.slice(0, 10) +
    ' · taker RT ~' + r.taker_round_trip_bps + 'bp';
  const st = r.stats;
  const cards = [
    ['Return', fmtPct(st.return_pct), st.return_pct > 0 ? 'pos' : st.return_pct < 0 ? 'neg' : '', 'full costs'],
    ['Total P&L', fmtPnl(st.total_pnl), st.total_pnl > 0 ? 'pos' : st.total_pnl < 0 ? 'neg' : '', 'on ' + fmt$(10000) + ' basis'],
    ['Trades', String(st.trades), '', 'win rate ' + st.win_rate_pct + '%'],
    ['Profit factor', st.profit_factor == null ? '∞' : st.profit_factor, '', 'gross win ÷ loss'],
    ['Max drawdown', fmtPct(st.max_drawdown_pct), 'neg', 'peak-to-trough'],
    ['Sharpe', st.sharpe == null ? '—' : st.sharpe, '', 'annualized'],
    ['Fees paid', fmt$(st.fees), 'neg', 'the cost autopsy'],
  ];
  renderStatCards($('#labStats'), cards);
  $('#labCurveHint').textContent = r.stats.strategy + ' · ' + r.equity_curve.length + ' pts';
  if (labChart) {
    labChart.data.labels = r.equity_curve.map(p => fmtTs(p.ts));
    labChart.data.datasets[0].data = r.equity_curve.map(p => p.equity);
    labChart.update(reduceMotion ? 'none' : undefined);
  }
  $('#labExits').innerHTML = Object.entries(r.exit_reasons || {}).map(([k, v]) =>
    '<span class="tag tf">' + esc(k) + ' × ' + v + '</span>').join('') ||
    '<span class="hint">no exits</span>';
  /* comparison table (the "apply strategies" plural view) */
  const cmp = r.comparison;
  $('#labCompareCard').hidden = !cmp;
  /* how much each strategy made — bars under the chart (left column);
     single-strategy runs show the one row, comparisons show all, best on top */
  const pnlRows = (cmp && cmp.length ? cmp.slice() : [st]).map(c => ({
    name: c.strategy, pnl: c.total_pnl, trades: c.trades }));
  $('#labPnlCard').hidden = false;
  renderBars($('#labStratBars'), pnlRows.sort((a, b) => b.pnl - a.pnl));
  if (cmp) {
    const sorted = [...cmp].sort((a, b) => b.total_pnl - a.total_pnl);
    $('#labCompareTable tbody').innerHTML = sorted.map(c =>
      '<tr><td class="mono" style="color:var(--color-blue)">' + esc(c.strategy) + '</td>' +
      '<td class="num ' + (c.return_pct > 0 ? 'pos' : c.return_pct < 0 ? 'neg' : '') + '">' + fmtPct(c.return_pct) + '</td>' +
      '<td class="num ' + (c.total_pnl > 0 ? 'pos' : c.total_pnl < 0 ? 'neg' : '') + '">' + fmtPnl(c.total_pnl) + '</td>' +
      '<td class="num">' + c.trades + '</td>' +
      '<td class="num">' + c.win_rate_pct + '%</td>' +
      '<td class="num">' + esc(c.profit_factor == null ? '∞' : c.profit_factor) + '</td>' +
      '<td class="num neg">' + fmtPct(c.max_drawdown_pct) + '</td>' +
      '<td class="num">' + esc(c.sharpe == null ? '—' : c.sharpe) + '</td>' +
      '<td class="num">' + fmt$(c.fees) + '</td></tr>').join('');
  }
  /* trades of the best strategy (last 200) */
  const trades = r.trades || [];
  $('#labTradesCard').hidden = trades.length === 0;
  $('#labTradesHint').textContent = trades.length + ' most recent (of ' + st.trades + ')';
  $('#labTradeTable tbody').innerHTML = trades.slice().reverse().map(t =>
    '<tr><td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.entry_ts)) + '</td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + fmtPnl(t.pnl) + '</td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.exit_reason || '—') + '</td></tr>').join('');
  toast('Lab run complete', r.stats.strategy + ': ' + fmtPct(r.stats.return_pct) +
        ' over ' + st.trades + ' trades', r.stats.return_pct >= 0);
}

$('#labBook').addEventListener('change', labFormRefresh);
$('#labKind').addEventListener('change', () => { labFormRefresh(); });
$('#labTimeframe').addEventListener('change', labStrategyRefresh);
$('#labChips').addEventListener('click', e => {
  const b = e.target.closest('[data-sym]');
  if (b) { $('#labSymbol').value = b.dataset.sym; }
});
$('#labRunBtn').addEventListener('click', labRun);

/* ===================================================== portfolio */
function renderPositions(s) {
  const tbody = $('#posTable tbody');
  const ops = s.open_positions || [];
  $('#liveHint').textContent = s.engine_running
    ? 'engine running — live marks'
    : (ops.length ? 'engine stopped — entry prices from the journal; start the engine for live marks'
                   : 'engine stopped — start it for live marks');
  $('#posEmpty').hidden = ops.length > 0;
  if (!ops.length) { tbody.innerHTML = ''; return; }
  tbody.innerHTML = ops.map(p => {
    const live = s.engine_running && p.live !== false;
    return '<tr>' +
      '<td class="mono"><b>' + esc(p.symbol) + '</b> <span class="tag tf">' + esc(p.timeframe) + '</span></td>' +
      '<td>' + sideTag(p.side) + '</td>' +
      '<td class="num">' + fmtQty(p.qty) + '</td>' +
      '<td class="num">' + fmtPx(p.entry, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.mark, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.stop, p.symbol) + '</td>' +
      '<td class="num">' + fmtPx(p.target, p.symbol) + '</td>' +
      '<td class="mono" style="color:var(--color-blue)">' + esc(p.strategy) + '</td>' +
      '<td class="num">' + (p.bars_held ?? 0) + '</td>' +
      '<td class="num ' + posCls(p.unrealized) + '">' +
        (p.unrealized != null ? fmtPnl(p.unrealized) : (live ? '…' : '—')) + '</td>' +
      '<td>' + (live ? '<button class="btn btn-ghost position-close" title="Review and close position" aria-label="Close ' + esc(p.symbol) + ' ' + esc(p.timeframe) +
        '" data-close="' + esc(p.symbol) + '|' + esc(p.timeframe) + '">' +
        'Close</button>' : '') + '</td></tr>';
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
  const tbody = $('#tradeTable tbody');
  $('#tradeEmpty').hidden = !tradeHistoryLoaded || rows.length > 0;
  $('#tradeEmpty').textContent = tradeHistory.length
    ? 'No trades match your filters. Try another search or clear the filters.'
    : 'No trades recorded yet. Review your watchlist, then start the paper engine.';
  $('#tradeClear').hidden = !(strategy || status || query);
  $('#tradePrev').disabled = tradePage === 0;
  $('#tradeNext').disabled = start + TRADE_PAGE_SIZE >= rows.length;
  if (tradeHistoryLoaded) $('#tradeCount').textContent =
    (rows.length ? (start + 1) + '–' + (start + pageRows.length) : '0') +
    ' of ' + rows.length + ' matching · ' + tradeHistory.length + ' loaded' +
    (tradeHistory.length >= 1000 ? ' (latest 1,000)' : '');
  tbody.innerHTML = pageRows.map(t => '<tr>' +
    '<td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.opened_ts)) + '</td>' +
    '<td class="mono"><b>' + esc(t.symbol) + '</b> <span class="tag tf">' + esc(t.timeframe || '') + '</span>' +
      (t.mode === 'demo' ? ' <span class="tag demo">demo</span>' : '') + '</td>' +
    '<td>' + sideTag(t.side) + '</td>' +
    '<td class="num">' + fmtQty(t.qty) + '</td>' +
    '<td class="num">' + fmtPx(t.entry_price, t.symbol) + '</td>' +
    '<td class="num">' + fmtPx(t.exit_price, t.symbol) + '</td>' +
    '<td class="num ' + posCls(t.pnl) + '">' + (t.status === 'CLOSED' ? fmtPnl(t.pnl) : '—') + '</td>' +
    '<td class="mono" style="color:var(--color-blue)">' + esc(t.strategy) + '</td>' +
    '<td><span class="tag ' + (t.status === 'OPEN' ? 'open' : 'hold') + '">' + esc(t.status) + '</span></td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.exit_reason || '—') + '</td></tr>').join('');
}
async function refreshTrades() {
  let trades;
  try { trades = await jget('/api/trades?limit=1000'); }
  catch (e) {
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
   (validate/shadow), data/kronos_ic.json, data/manifest.json — the tab
   computes nothing from trade data, it presents what the CLI wrote */
let kronosChart = null, cvChart = null;
let EV_REPORTS = [];
/* the shipped empty-state copy, kept so a failure render can be undone */
const KR_EMPTY_HTML = 'no resolved forecasts yet — the ledger fills as ' +
  '<code>main.py kronos</code> forecasts and their horizons resolve';
const CV_EMPTY_HTML = 'no validation reports in this data directory — run ' +
  '<code>make validate</code>';

function evidenceFailed(err) {
  /* the tab used to mark itself loaded BEFORE the request and swallow the
     failure, so one 401 (an unentered token, a rotated one) left every panel
     on "loading…" FOREVER — the tab never retried and never said why. */
  const msg = /401/.test(String(err && err.message))
    ? 'not authorized — enter your dashboard token (the lock in the top bar), then reopen this tab'
    : 'could not load the evidence artifacts: ' + esc(String(err && err.message || err));
  const box = '<div class="empty">' + msg + '</div>';
  $('#evShadow').innerHTML = box;
  $('#evManifest').innerHTML = box;
  $('#krMeta').textContent = '';
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
    evidenceFailed(e);        // NOT marked loaded: the next tab entry retries
    return;
  }
  evLoaded.v = true;          // only a SUCCESSFUL load counts as loaded

  /* --- Kronos rolling IC vs its own hurdle --- */
  const k = ev.kronos || {};
  const S = k.series || [];
  const labels = S.map(p => p.i);
  if (kronosChart) { kronosChart.destroy(); kronosChart = null; }
  $('#krMeta').textContent = k.error ? ('ledger unavailable: ' + k.error)
    : (k.n ? k.n + ' resolved forecasts · pending ' + (k.pending ?? 0) +
        ' · rolling IC ' + (k.ic ?? '—') : '');
  $('#krEmpty').innerHTML = KR_EMPTY_HTML;      // restore after a failure render
  $('#krEmpty').hidden = labels.length > 0;
  if (labels.length) {
    kronosChart = new Chart($('#kronosChart'), {
      type: 'line',
      data: { labels, datasets: [
        { label: 'rolling IC', data: S.map(p => p.ic),
          borderColor: cssVar('--color-blue'), pointRadius: 0, borderWidth: 1.5, tension: 0.25 },
        { label: 'promotion hurdle ' + (k.hurdle ?? 0.02), data: labels.map(() => k.hurdle ?? 0.02),
          borderColor: cssVar('--color-pos'), borderDash: [6, 4], pointRadius: 0, borderWidth: 1 },
        { label: 'demotion floor ' + (k.demote_below ?? 0), data: labels.map(() => k.demote_below ?? 0),
          borderColor: cssVar('--color-neg'), borderDash: [4, 4], pointRadius: 0, borderWidth: 1 }]},
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: cssVar('--color-muted-foreground'),
                                       boxWidth: 10, font: { size: 10 } } } },
        scales: {
          x: { ticks: { color: cssVar('--color-muted-foreground'), maxTicksLimit: 8,
                        font: { size: 10 } }, grid: { color: cssVar('--chart-grid') } },
          y: { ticks: { color: cssVar('--color-muted-foreground'), font: { size: 10 } },
               grid: { color: cssVar('--chart-grid') } } } }
    });
  }

  /* --- validation reports: selector + verdict cards + path chart --- */
  EV_REPORTS = ev.validations || [];
  const sel = $('#evReportSel');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— no report selected —</option>' +
    EV_REPORTS.map((r, i) => '<option value="' + i + '">' +
      esc((r.symbol || '?') + ' ' + (r.timeframe || '') + ' · ' +
          (r.strategy || '?') + ' · ' +
          (r.start ? r.start + '→' + (r.end || 'now') : (r.days || '?') + 'd')) + '</option>').join('');
  sel.value = (cur !== '' && Number(cur) < EV_REPORTS.length) ? cur
                                                             : (EV_REPORTS.length ? '0' : '');
  renderEvidenceCards();
  renderEvidencePaths();

  /* --- shadow adherence --- */
  const sh = ev.shadow;
  if (!sh || !sh.profile) {
    $('#evShadow').innerHTML = '<div class="empty">no shadow report yet — run ' +
      '<code>python3 main.py shadow</code> (writes data/results/shadow_report.json)</div>';
  } else {
    const rows = Object.entries(sh.symbols || {}).map(([name, s]) =>
      '<tr><td class="mono"><b>' + esc(name) + '</b></td>' +
      '<td class="num">' + esc(s.adherence_pct) + '%</td>' +
      '<td class="num">' + esc(s.on_rule) + '</td>' +
      '<td class="num">' + esc(s.late) + '</td>' +
      '<td class="num ' + (s.rule_breaks ? 'neg' : '') + '">' + esc(s.rule_breaks) + '</td>' +
      '<td class="num">' + esc(s.unknown) + '</td></tr>').join('');
    $('#evShadow').innerHTML = '<table><thead><tr><th>market</th><th class="num">on-rule %</th>' +
      '<th class="num">on-rule</th><th class="num">late</th><th class="num">rule breaks</th>' +
      '<th class="num">unknown</th></tr></thead><tbody>' + (rows ||
        '<tr><td colspan="6" class="empty">no auditable trades in the report</td></tr>') +
      '</tbody></table><div class="hint" style="padding:8px 12px">profile over ' +
      esc(sh.profile.n_trades) + ' closed trades · win rate ' + esc(sh.profile.win_rate_pct) +
      '% · blew through stop: ' + esc(sh.profile.n_blew_through_stop) +
      ' · disposition gap ' + esc(sh.profile.disposition_gap_hours) + 'h</div>';
  }

  /* --- pinned-data manifest --- */
  const man = ev.manifest || {};
  const keys = Object.keys(man);
  $('#evManifest').innerHTML = keys.length
    ? '<table><thead><tr><th>market</th><th class="num">bars</th><th>window</th>' +
      '<th>source</th><th>sha256</th><th>fetched</th></tr></thead><tbody>' +
      keys.map(kk => { const m = man[kk];
        return '<tr><td class="mono"><b>' + esc(kk) + '</b></td>' +
          '<td class="num">' + esc(m.bars) + '</td>' +
          '<td class="mono">' + esc(String(m.first_ts).slice(0, 10) + ' → ' +
                                    String(m.last_ts).slice(0, 10)) + '</td>' +
          '<td>' + esc(m.source) + '</td>' +
          '<td class="mono">' + esc(String(m.sha256).slice(0, 12)) + '…</td>' +
          '<td class="mono">' + esc(String(m.fetched_at).slice(0, 10)) + '</td></tr>';
      }).join('') + '</tbody></table>' +
      '<div class="hint" style="padding:8px 12px">fetch with <code>--start/--end</code> for ' +
      'byte-identical pinned windows; checksums make BACKTESTS.md claims checkable</div>'
    : '<div class="empty">no pinned fetches yet — run a backtest with ' +
      '<code>--start YYYY-MM-DD --end YYYY-MM-DD</code></div>';
}

function renderEvidenceCards() {
  const r = EV_REPORTS[$('#evReportSel').value] || null;
  const cards = r ? [
    ['PBO', r.pbo ? r.pbo.pbo : '—',
      r.pbo && r.pbo.pbo >= 0.5 ? 'neg' : (r.pbo && r.pbo.pbo >= 0.35 ? '' : 'pos'),
      r.pbo ? r.pbo.verdict : 'needs a ≥2-strategy family'],
    ['Deflated Sharpe', r.deflated_sharpe ? r.deflated_sharpe.deflated_sharpe : '—',
      r.deflated_sharpe && r.deflated_sharpe.deflated_sharpe >= 0.95 ? 'pos' : '',
      r.deflated_sharpe ? r.deflated_sharpe.verdict : 'pass --trial-sharpes'],
    ['MC terminal p5', r.monte_carlo && r.monte_carlo.n_sims
        ? '$' + r.monte_carlo.terminal_p5.toLocaleString() : '—', 'neg',
      '5th-percentile resampled outcome'],
    ['MinTRL', r.min_trl && r.min_trl.min_bars ? r.min_trl.min_years + 'y' : '—', '',
      r.min_trl && r.min_trl.min_bars
        ? Number(r.min_trl.min_bars).toLocaleString() + ' OOS bars @95%' : 'needs a positive Sharpe'],
    ['Backtest return', r.backtest ? fmtPct(r.backtest.return_pct) : '—',
      r.backtest && r.backtest.return_pct > 0 ? 'pos' : 'neg',
      r.backtest ? (r.backtest.trades + ' trades · sharpe ' + r.backtest.sharpe +
        ' · window ' + (r.start ? r.start + '→' + (r.end || 'now') : (r.days || '?') + 'd')) : ''],
  ] : [
    ['PBO', '—', '', 'run make validate'], ['Deflated Sharpe', '—', '', 'run make validate'],
    ['MC terminal p5', '—', '', 'run make validate'], ['MinTRL', '—', '', 'run make validate'],
    ['Backtest return', '—', '', 'no reports yet'],
  ];
  $('#evCards').innerHTML = cards.map(c =>
    '<div class="stat"><div class="label">' + esc(c[0]) + '</div>' +
    '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
    '<div class="sub">' + esc(c[3]) + '</div></div>').join('');
}

function renderEvidencePaths() {
  if (cvChart) { cvChart.destroy(); cvChart = null; }
  const r = EV_REPORTS[$('#evReportSel').value] || null;
  const paths = r && r.purged_cv ? (r.purged_cv.paths || []) : [];
  $('#cvEmpty').innerHTML = CV_EMPTY_HTML;      // restore after a failure render
  $('#cvEmpty').hidden = paths.some(p => p.trades);
  if (!paths.length) return;
  cvChart = new Chart($('#cvChart'), {
    type: 'bar',
    data: { labels: paths.map((p, i) => 'p' + (i + 1) + (p.trades ? '' : ' ·')),
      datasets: [{ label: 'OOS return %',
        data: paths.map(p => p.trades ? p.return_pct : null),
        backgroundColor: paths.map(p => p.trades
          ? (p.return_pct >= 0 ? cssVar('--color-pos') : cssVar('--color-neg'))
          : cssVar('--color-muted')) }]},
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: cssVar('--color-muted-foreground'), font: { size: 9 } },
                     grid: { display: false } },
                y: { ticks: { color: cssVar('--color-muted-foreground'), font: { size: 10 },
                              callback: v => v + '%' },
                     grid: { color: cssVar('--chart-grid') } } } }
  });
}
$('#evReportSel').addEventListener('change', () => { renderEvidenceCards(); renderEvidencePaths(); });

/* ===================================================== watchlist */
async function refreshWatchlist() {
  let specs;
  try { specs = await jget('/api/watchlist'); } catch (e) { return; }
  $('#wlCount').textContent = specs.length + ' / 12 specs';
  $('#wlEmpty').hidden = specs.length > 0;
  const grid = $('#wlGrid');
  grid.innerHTML = specs.map(s =>
    '<div class="wl-item"><div style="min-width:0">' +
    '<div class="sy">' + esc(s.symbol) + ' <span class="tag tf">' + esc(s.timeframe) + '</span></div>' +
    '<div class="meta"><span class="tag ' + (s.kind === 'crypto' ? 'open' : 'close') + '">' + esc(s.kind) + '</span>' +
    '<span class="tag hold">' + esc(s.strategy) + '</span></div>' +
    (s.display && s.display !== s.symbol ? '<div class="disp">' + esc(s.display) + '</div>' : '') +
    '</div><button class="icon-btn" title="Remove from watchlist" aria-label="Remove ' + esc(s.symbol) + ' ' + esc(s.timeframe) +
    '" data-del="' + esc(s.kind) + '|' + esc(s.symbol) + '|' + esc(s.timeframe) + '">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button></div>').join('');
}
$('#wlGrid').addEventListener('click', async e => {
  const btn = e.target.closest('[data-del]');
  if (!btn) return;
  const [kind, symbol, timeframe] = btn.dataset.del.split('|');
  try {
    const r = await jdel('/api/watchlist/' + kind + '/' + encodeURIComponent(symbol) + '/' + timeframe);
    toast('Removed from watchlist', symbol + ' ' + timeframe + ' — ' + r.count + ' specs remain');
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

const PRESETS = {
  'crypto-majors': () => ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'].flatMap(s =>
    [{kind: 'crypto', symbol: s, timeframe: '1h'},
     {kind: 'crypto', symbol: s, timeframe: '15m'},
     {kind: 'crypto', symbol: s, timeframe: '4h'}]),
  'forex-majors': () => ['EURUSD=X', 'GBPUSD=X', 'USDJPY=X'].map(s =>
    ({kind: 'forex', symbol: s, timeframe: '1h'})),
};
$$('.preset-btn').forEach(b => b.addEventListener('click', () => {
  const p = b.dataset.preset;
  if (p === 'reset-default') {
    const defaults = [['crypto', 'BTC/USDT', '1h', 'Bitcoin'], ['crypto', 'ETH/USDT', '1h', 'Ethereum'],
      ['crypto', 'SOL/USDT', '1h', 'Solana'], ['crypto', 'BTC/USDT', '15m', 'Bitcoin (scalp)'],
      ['crypto', 'ETH/USDT', '15m', 'Ethereum (scalp)'], ['crypto', 'BTC/USDT', '4h', 'Bitcoin (mean-rev)'],
      ['crypto', 'ETH/USDT', '4h', 'Ethereum (mean-rev)'], ['forex', 'EURUSD=X', '1h', 'EUR/USD'],
      ['forex', 'GBPUSD=X', '1h', 'GBP/USD']];
    (async () => {
      try {   // clear then re-add the shipped default list
        let cur = [];
        try { cur = await jget('/api/watchlist'); } catch (e) {}
        for (const s of cur) {
          try { await jdel('/api/watchlist/' + s.kind + '/' + encodeURIComponent(s.symbol) + '/' + s.timeframe); }
          catch (err) { toastErr('Remove failed', err); }
        }
        await addSpecs(defaults.map(d => ({kind: d[0], symbol: d[1], timeframe: d[2], display: d[3]})));
      } catch (err) { toastErr('Preset failed', err); }
    })();
    return;
  }
  addSpecs(PRESETS[p]());
}));

/* ===================================================== account */
async function refreshAccount() {
  let a;
  try { a = await jget('/api/account'); } catch (e) { return; }
  const pnl = a.equity - a.capital;
  const big = $('#balanceBig');
  big.textContent = fmt$(a.equity);
  big.className = 'balance-value ' + posCls(pnl);
  $('#balanceMode').textContent = 'paper mode · ' +
    (a.engine_running ? 'engine running' : 'engine stopped') +
    (pnl ? ' · ' + fmtPnl(pnl) + ' (' + fmtPct(pnl / a.capital * 100) + ')' : '');
  $('#balCash').textContent = fmt$(a.cash);
  const u = $('#balUnreal');
  u.textContent = a.engine_running ? fmtPnl(a.unrealized) : '—';
  u.className = 'v mono ' + posCls(a.unrealized);
  $('#balCapital').textContent = fmt$(a.capital);
  $('#balLast').textContent = a.last_equity ? fmtTs(a.last_equity.ts) : '—';
}

/* deposit/withdrawal ledger — typed rows beat scraping add_equity notes */
async function refreshTransactions() {
  let txs;
  try { txs = await jget('/api/account/transactions'); } catch (e) { return; }
  $('#txnEmpty').hidden = txs.length > 0;
  const tagCls = {deposit: 'long', withdrawal: 'short', reset: 'close'};
  $('#txnTable tbody').innerHTML = txs.map(t => '<tr>' +
    '<td class="mono" style="color:var(--color-muted-foreground)">' + esc(fmtTs(t.ts)) + '</td>' +
    '<td><span class="tag ' + (tagCls[t.kind] || '') + '">' + esc(t.kind) + '</span></td>' +
    '<td class="num ' + (t.kind === 'withdrawal' ? 'neg' : 'pos') + '">' +
      fmtPnl(t.kind === 'withdrawal' ? -t.amount : t.amount) + '</td>' +
    '<td class="num">' + fmt$(t.cash_after) + '</td>' +
    '<td class="num">' + fmt$(t.equity_after) + '</td>' +
    '<td style="color:var(--color-muted-foreground)">' + esc(t.note || '') + '</td></tr>').join('');
}

function parseAmount(v) {
  const n = Number(String(v).replace(/[$,\\s]/g, ''));
  if (!isFinite(n) || isNaN(n) || n <= 0) return null;
  return Math.round(n * 100) / 100;
}
function submitAmount(url, raw, label) {
  const amount = parseAmount(raw);
  if (amount == null) { toast('Invalid amount', 'enter a positive number, e.g. 500', false); return Promise.resolve(false); }
  return jpost(url, {amount}).then(r => {
    toast(label + ' successful', (label === 'Deposit' ? '+' : '-') + fmt$(amount) +
      ' · cash now ' + fmt$(r.cash));
    refreshAccount();
    return true;
  }).catch(e => { toastErr(label + ' failed', e); return false; });
}
$('#depositForm').addEventListener('submit', async e => {
  e.preventDefault();
  const ok = await submitAmount('/api/account/deposit', $('#depositAmt').value, 'Deposit');
  if (ok) $('#depositAmt').value = '';
});
$('#withdrawForm').addEventListener('submit', async e => {
  e.preventDefault();
  const ok = await submitAmount('/api/account/withdraw', $('#withdrawAmt').value, 'Withdrawal');
  if (ok) $('#withdrawAmt').value = '';
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
  if (capital == null) { toast('Invalid capital', 'enter a positive number', false); return; }
  resetModal.dataset.busy = 'true';
  $('#resetGo').disabled = true;
  try {
    const r = await jpost('/api/account/reset', {capital});
    toast('Account reset', 'new capital ' + fmt$(r.capital) +
      (r.backup ? ' · backup ' + r.backup.split('/').pop() : ''));
    resetModal.dataset.busy = 'false';
    closeDialog('#resetModal');
    $('#resetConfirm').value = '';
    refreshAccount(); refreshStats(); refreshEquity();
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
  try { hist = await jget('/api/chat'); } catch (e) { return; }
  hist.forEach(m => addMsg(m.content, m.role === 'user' ? 'user' : 'bot'));
  if (!hist.length) addMsg("Hi! I'm the bot's assistant. Ask me: 'how much did you earn?', " +
    "'which strategy is best?', 'why did you buy BTC?', 'explain the turtle strategy'…", 'bot');
  const log = $('#chatlog');
  log.scrollTop = log.scrollHeight;
}
$('#chatform').addEventListener('submit', async e => {
  e.preventDefault();
  const box = $('#chatbox');
  const msg = box.value.trim();
  if (!msg) return;
  box.value = '';
  addMsg(msg, 'user');
  const typing = document.createElement('div');
  typing.className = 'msg bot typing';
  typing.innerHTML = '<i></i><i></i><i></i>';
  $('#chatlog').appendChild(typing);
  $('#chatlog').scrollTop = 999999;
  try {
    const r = await jpost('/api/chat', {message: msg});
    typing.remove();
    addMsg(r.reply, 'bot');
  } catch (err) {
    typing.remove();
    addMsg('(network error) ' + err.message, 'bot');
  }
});
$('#chatlog').addEventListener('click', () => $('#chatbox').focus());
$$('.quick-chip').forEach(chip => chip.addEventListener('click', () => {
  if (chip.dataset.engine === 'start') { startEngine(); return; }
  if (chip.dataset.engine === 'stop') { stopEngine(); return; }
  const q = chip.dataset.q;
  $('#chatbox').value = q;
  $('#chatform').dispatchEvent(new Event('submit', {cancelable: true}));
}));

/* ===================================================== polling */
let pollTimer = null;
function poll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    const name = document.querySelector('.view.active').id.replace('view-', '');
    refreshVisible(name);
  }, 4000);
}
$('#refreshNow').addEventListener('click', () => {
  evLoaded.v = false;
  chatLoaded.v = false;
  refreshVisible($('.view.active').id.replace('view-', ''));
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshVisible($('.view.active').id.replace('view-', ''));
});
setInterval(updateFreshness, 1000);

/* ===================================================== boot
   Chart.js is VENDORED (served from this app at /chart.umd.min.js, no CDN
   call): on venue/offline WiFi the SPA works regardless. If the vendored
   library somehow fails to load we show a notice and render everything else —
   the equity chart canvas just stays empty instead of killing routing,
   polling and every button listener with a ReferenceError. */
if (typeof Chart === 'undefined') {
  const banner = document.createElement('div');
  banner.className = 'chart-offline';
  banner.textContent = 'Chart.js could not load (offline?) — the equity chart is ' +
    'disabled, everything else works normally.';
  const main = document.querySelector('main') || document.body;
  main.insertBefore(banner, main.firstChild);
} else {
  buildEquityChart();
  buildHftEquityChart();
  buildHftPriceChart();
  buildLabEquityChart();
}
/* sync the switcher + browser chrome with the theme <head> already applied;
   runs after buildEquityChart so applyChartTheme sees a live chart */
applyTheme(document.documentElement.getAttribute('data-theme') || 'light', false);
setView(location.hash.slice(1) || 'overview');
poll();
