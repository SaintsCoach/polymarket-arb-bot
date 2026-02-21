'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  oppCount:   0,
  tradeCount: 0,
  startTs:    Date.now(),
  pnlData:    [{ x: 0, y: 0 }],   // [{x: index, y: profit}]
  pnlLabels:  [fmt(Date.now() / 1000)], // tooltip timestamps
};

// ── Chart ─────────────────────────────────────────────────────────────────────
let chart;

function initChart() {
  const ctx = document.getElementById('pnl-chart');
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: S.pnlLabels,
      datasets: [{
        data: S.pnlData.map(p => p.y),
        borderColor:     '#00e87a',
        backgroundColor: createGradient,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: '#00e87a',
        fill: true,
        tension: 0.42,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#11111f',
          borderColor: 'rgba(255,255,255,0.07)',
          borderWidth: 1,
          padding: 10,
          titleColor: '#525270',
          bodyColor: '#00e87a',
          callbacks: {
            title: items => S.pnlLabels[items[0].dataIndex] || '',
            label: item  => `  P&L  ${ item.parsed.y >= 0 ? '+' : '' }${ item.parsed.y.toFixed(4) }`,
          }
        }
      },
      scales: {
        x: { display: false },
        y: {
          grid:   { color: 'rgba(255,255,255,0.03)', drawBorder: false },
          border: { display: false },
          ticks: {
            color: '#525270',
            font:  { family: "'JetBrains Mono', monospace", size: 10 },
            callback: v => `${ v >= 0 ? '+' : '' }${ v.toFixed(2) }`,
            maxTicksLimit: 5,
          },
        }
      },
      animation: { duration: 250 },
    }
  });
}

function createGradient(ctx) {
  const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height || 200);
  g.addColorStop(0, 'rgba(0,232,122,0.22)');
  g.addColorStop(1, 'rgba(0,232,122,0.00)');
  return g;
}

function pushPnL(profit, ts) {
  const y = parseFloat(profit) || 0;
  S.pnlData.push(y);
  S.pnlLabels.push(fmt(ts));
  if (S.pnlData.length > 300) { S.pnlData.shift(); S.pnlLabels.shift(); }

  const color = y >= 0 ? '#00e87a' : '#ff3361';
  chart.data.datasets[0].borderColor = color;
  chart.data.datasets[0].data = S.pnlData;
  chart.data.labels = S.pnlLabels;
  chart.update('none');

  const el = document.getElementById('chart-pnl');
  el.textContent = `${ y >= 0 ? '+' : '' }$${ Math.abs(y).toFixed(4) }`;
  el.className    = `stat-val ${ y >= 0 ? 'g' : 'r' }`;
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${ proto }://${ location.host }/ws`);

  ws.onopen  = () => setOnline(true);
  ws.onclose = () => { setOnline(false); setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = e => {
    try { dispatch(JSON.parse(e.data)); } catch (_) {}
  };
}

function setOnline(on) {
  const pill  = document.getElementById('live-pill');
  const label = document.getElementById('conn-label');
  if (on) { pill.classList.add('online'); label.textContent = 'LIVE'; }
  else    { pill.classList.remove('online'); label.textContent = 'OFFLINE'; }
}

// ── Plugin handler registry (used by mirror.js) ───────────────────────────────
const _extraHandlers = {};
function registerHandler(type, fn) { _extraHandlers[type] = fn; }

// ── Event dispatcher ──────────────────────────────────────────────────────────
function dispatch({ type, data, ts }) {
  switch (type) {
    case 'bot_start':  S.startTs = ts * 1000;          break;
    case 'stats':      renderStats(data);               break;
    case 'scan':       renderScan(data);                break;
    case 'candidates': renderCandidates(data.markets);  break;
    case 'opportunity':addOpp(data, ts);                break;
    case 'trade':      addTrade(data, ts);              break;
    default:
      if (_extraHandlers[type]) _extraHandlers[type](data, ts);
  }
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function renderStats(d) {
  setVal('s-opps',  d.opportunities_seen,   false);
  setVal('s-exec',  d.trades_executed,       false);
  setVal('s-abort', d.trades_aborted,        false);

  if (d.total_profit_usdc !== undefined) {
    const v  = d.total_profit_usdc;
    const el = document.getElementById('s-pnl');
    el.textContent = `${ v >= 0 ? '+' : '-' }$${ Math.abs(v).toFixed(4) }`;
    el.className   = `stat-val ${ v >= 0 ? 'g' : 'r' }`;
  }
  if (d.balance_usdc !== undefined) {
    document.getElementById('s-bal').textContent = `$${ commas(d.balance_usdc) }`;
  }
}

function setVal(id, v, animate = true) {
  if (v === undefined || v === null) return;
  const el = document.getElementById(id);
  const s  = String(v);
  if (el.textContent === s) return;
  el.textContent = s;
  if (animate) { el.classList.remove('pop'); void el.offsetWidth; el.classList.add('pop'); }
}

// ── Scan info ─────────────────────────────────────────────────────────────────
function renderScan({ markets_total, candidates, scan_ms }) {
  document.getElementById('sc-total').textContent = markets_total;
  document.getElementById('sc-cands').textContent = candidates;
  document.getElementById('sc-ms').textContent    = scan_ms;
}

// ── Candidates ────────────────────────────────────────────────────────────────
function renderCandidates(markets) {
  const list = document.getElementById('cand-list');
  list.innerHTML = '';
  if (!markets || !markets.length) {
    list.innerHTML = '<div class="empty">No candidates this cycle</div>';
    return;
  }
  markets.forEach(m => {
    const div = document.createElement('div');
    div.className = 'cand-card';
    div.innerHTML = `
      <div class="cand-q">${ esc(m.question) }</div>
      <div class="cand-est">Combined est: ${ pct(m.combined_est) }</div>
    `;
    list.appendChild(div);
  });
}

// ── Opportunity cards ─────────────────────────────────────────────────────────
function addOpp(d, ts) {
  S.oppCount++;
  document.getElementById('opp-badge').textContent = `${ S.oppCount } found`;
  document.getElementById('s-opps').textContent    = S.oppCount;

  const feed = document.getElementById('opp-feed');
  clearEmpty(feed);

  const c = document.createElement('div');
  c.className = 'opp-card';
  c.innerHTML = `
    <div class="card-row">
      <div class="card-q">${ esc(d.question) }</div>
      <div class="card-time">${ fmt(ts) }</div>
    </div>
    <div class="metrics">
      <div class="metric"><span class="m-lbl">YES ASK</span><span class="m-val b">${ pct(d.yes_ask) }</span></div>
      <div class="metric"><span class="m-lbl">NO ASK</span><span class="m-val b">${ pct(d.no_ask) }</span></div>
      <div class="metric"><span class="m-lbl">COMBINED</span><span class="m-val y">${ d.combined_pct.toFixed(2) }%</span></div>
      <div class="metric"><span class="m-lbl">PROFIT %</span><span class="m-val g">${ d.profit_pct.toFixed(2) }%</span></div>
      <div class="metric"><span class="m-lbl">EST. GAIN</span><span class="m-val g">+$${ d.est_profit_usdc.toFixed(4) }</span></div>
    </div>
  `;
  prepend(feed, c);
}

// ── Trade cards ───────────────────────────────────────────────────────────────
function addTrade(d, ts) {
  S.tradeCount++;
  document.getElementById('trade-badge').textContent = `${ S.tradeCount } trades`;

  const feed = document.getElementById('trade-feed');
  clearEmpty(feed);

  const isOk   = d.outcome === 'SUCCESS';
  const isBad  = d.outcome.startsWith('FAILED');
  const cls    = isOk ? 'ok' : isBad ? 'bad' : 'skip';
  const label  = d.outcome.replace(/_/g, ' ');

  let detail = '';
  if (isOk && d.yes_fill != null) {
    detail = `YES @ ${ pct(d.yes_fill) }  ·  NO @ ${ pct(d.no_fill) }`;
  } else if (d.reason) {
    detail = d.reason.length > 60 ? d.reason.slice(0, 60) + '…' : d.reason;
  }

  const c = document.createElement('div');
  c.className = `trade-card ${ cls }`;
  c.innerHTML = `
    <span class="outcome-tag">${ label }</span>
    <div class="trade-body">
      <div class="trade-q">${ esc(d.question) }</div>
      <div class="trade-detail">${ esc(detail) } · ${ fmt(ts) }</div>
    </div>
    ${ isOk ? `<div class="trade-profit">+$${ d.profit_usdc.toFixed(4) }</div>` : '' }
  `;
  prepend(feed, c);

  if (isOk && d.cumulative_profit != null) {
    pushPnL(d.cumulative_profit, ts);
  }
}

// ── Uptime ticker ─────────────────────────────────────────────────────────────
function startUptime() {
  setInterval(() => {
    const s = Math.floor((Date.now() - S.startTs) / 1000);
    const h = pad(Math.floor(s / 3600));
    const m = pad(Math.floor((s % 3600) / 60));
    const sec = pad(s % 60);
    document.getElementById('s-uptime').textContent = `${ h }:${ m }:${ sec }`;
  }, 1000);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
function fmt(ts) {
  return new Date(ts * 1000).toLocaleTimeString('en-US', {
    hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}
function pct(v)     { return (parseFloat(v) * 100).toFixed(1) + '%'; }
function pad(n)     { return String(n).padStart(2, '0'); }
function commas(n)  { return parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function clearEmpty(el) { const e = el.querySelector('.empty'); if (e) e.remove(); }
function prepend(el, child) {
  el.insertBefore(child, el.firstChild);
  // Keep max 40 cards
  while (el.children.length > 40) el.removeChild(el.lastChild);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initChart();
  connect();
  startUptime();
});
