'use strict';
// datafeed.js — DataFeed Bot frontend logic
// Hooks into the registerHandler() plugin API in app.js

// ── State ─────────────────────────────────────────────────────────────────────
const DF = {
  oppCount:      0,
  resolvedCount: 0,
};

// ── DataFeed P&L Chart ────────────────────────────────────────────────────────
let dfChart;
const DFC = {
  data:    [0],
  labels:  [],
  lastPnl: null,
};

function initDFChart() {
  const ctx = document.getElementById('datafeed-pnl-chart');
  if (!ctx) return;
  DFC.labels = [dfFmtTs(Date.now() / 1000)];
  dfChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: DFC.labels,
      datasets: [{
        data:                      DFC.data,
        borderColor:               '#00e87a',
        backgroundColor:           dfGradient,
        borderWidth:               2,
        pointRadius:               0,
        pointHoverRadius:          4,
        pointHoverBackgroundColor: '#00e87a',
        fill:                      true,
        tension:                   0.42,
      }]
    },
    options: {
      responsive:          true,
      maintainAspectRatio: false,
      interaction:         { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#11111f',
          borderColor:     'rgba(255,255,255,0.07)',
          borderWidth:     1,
          padding:         10,
          titleColor:      '#525270',
          bodyColor:       '#00e87a',
          callbacks: {
            title: items => DFC.labels[items[0].dataIndex] || '',
            label: item  => `  P&L  ${ item.parsed.y >= 0 ? '+' : '' }$${ item.parsed.y.toFixed(4) }`,
          }
        }
      },
      scales: {
        x: { display: false },
        y: {
          grid:   { color: 'rgba(255,255,255,0.03)', drawBorder: false },
          border: { display: false },
          ticks: {
            color:  '#525270',
            font:   { family: "'JetBrains Mono', monospace", size: 10 },
            callback:     v => `${ v >= 0 ? '+' : '' }${ v.toFixed(2) }`,
            maxTicksLimit: 5,
          },
        }
      },
      animation: { duration: 250 },
    }
  });
}

function dfGradient(ctx) {
  const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height || 200);
  g.addColorStop(0, 'rgba(0,232,122,0.22)');
  g.addColorStop(1, 'rgba(0,232,122,0.00)');
  return g;
}

function pushDFPnL(totalPnl, ts) {
  if (!dfChart) return;
  const y = parseFloat(totalPnl) || 0;
  if (DFC.lastPnl === y) return;
  DFC.lastPnl = y;

  DFC.data.push(y);
  DFC.labels.push(dfFmtTs(ts || Date.now() / 1000));
  if (DFC.data.length > 300) { DFC.data.shift(); DFC.labels.shift(); }

  const color = y >= 0 ? '#00e87a' : '#ff3361';
  dfChart.data.datasets[0].borderColor = color;
  dfChart.data.datasets[0].data        = DFC.data;
  dfChart.data.labels                  = DFC.labels;
  dfChart.update('none');

  const el = document.getElementById('df-chart-pnl');
  if (el) {
    el.textContent = `${ y >= 0 ? '+' : '' }$${ Math.abs(y).toFixed(4) }`;
    el.className   = `ov-val ${ y >= 0 ? 'g' : 'r' }`;
  }
}

function resetDFChart(ts) {
  DFC.data    = [0];
  DFC.labels  = [dfFmtTs(ts || Date.now() / 1000)];
  DFC.lastPnl = null;
  if (!dfChart) return;
  dfChart.data.datasets[0].data        = DFC.data;
  dfChart.data.datasets[0].borderColor = '#00e87a';
  dfChart.data.labels                  = DFC.labels;
  dfChart.update('none');
  const el = document.getElementById('df-chart-pnl');
  if (el) { el.textContent = '+$0.0000'; el.className = 'ov-val g'; }
}

// ── Snapshot hydration ────────────────────────────────────────────────────────
async function hydrateDFSnapshot() {
  try {
    const resp = await fetch('/api/datafeed/snapshot');
    if (!resp.ok) return;
    const snap = await resp.json();
    if (snap.start_ts) {
      const el = document.getElementById('df-start-ts');
      if (el) el.textContent = dfFmtTs(snap.start_ts);
    }
    if (snap.overview)  renderDFOverview(snap.overview);
    if (snap.positions) renderDFPositions(snap.positions);
  } catch (e) { /* datafeed bot may not be enabled */ }
}

// ── Register event handlers ───────────────────────────────────────────────────
registerHandler('datafeed_start',            d => {
  const el = document.getElementById('df-start-ts');
  if (el) el.textContent = dfFmtTs(d.ts);
  resetDFChart(d.ts);
});
registerHandler('datafeed_overview',         d => renderDFOverview(d));
registerHandler('datafeed_positions',        d => renderDFPositions(d.positions));
registerHandler('datafeed_position_opened',  d => flashDFPositionOpened());
registerHandler('datafeed_position_closed',  d => addDFResolved(d));
registerHandler('datafeed_live_event',       d => addDFLiveEvent(d));
registerHandler('datafeed_opportunity',      d => addDFOpportunity(d));
registerHandler('datafeed_api_status',       d => updateDFApiStatus(d));

// ── Overview panel ────────────────────────────────────────────────────────────
function renderDFOverview(d, ts) {
  dfEl('df-balance',    `$${dfCommas(d.balance_usdc)}`);
  dfEl('df-deployed',   `$${dfCommas(d.total_deployed)}`);
  dfEl('df-slot-badge', `${d.slots_used} / ${d.slots_total} slots`);
  dfEl('df-pos-badge',  `${d.slots_used} open`);
  dfEl('df-slots',      `${d.slots_used} / ${d.slots_total}`);

  dfPnlEl('df-realized',   d.realized_pnl);
  dfPnlEl('df-unrealized', d.unrealized_pnl);
  dfPnlEl('df-total-pnl',  d.total_pnl);

  pushDFPnL(d.total_pnl, ts);
}

function dfPnlEl(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = parseFloat(val) || 0;
  el.textContent = `${v >= 0 ? '+' : ''}$${Math.abs(v).toFixed(4)}`;
  el.className   = `ov-val ${v >= 0 ? 'g' : 'r'}`;
}

// ── Positions table ───────────────────────────────────────────────────────────
function renderDFPositions(positions) {
  const tbody = document.getElementById('df-pos-tbody');
  if (!tbody) return;
  if (!positions || !positions.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No open positions.</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const pnl    = parseFloat(p.unrealized_pnl) || 0;
    const pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pnlStr = `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(4)}`;
    const sideCls = (p.outcome || '').toLowerCase() === 'yes' ? 'side-yes' : 'side-no';
    return `
      <tr>
        <td title="${dfEsc(p.market_question)}">${dfEsc(p.market_question.slice(0, 42))}…</td>
        <td><span class="${sideCls}">${dfEsc(p.outcome)}</span></td>
        <td class="mono-sm">${dfPct(p.entry_price)}</td>
        <td class="mono-sm">${dfPct(p.current_price)}</td>
        <td class="${pnlCls}">${pnlStr}</td>
        <td class="mono-sm">${dfAge(p.age_s)}</td>
        <td class="mono-sm" title="${dfEsc(p.source_event)}">${dfEsc((p.source_event || '').slice(0, 18))}</td>
      </tr>`;
  }).join('');
}

function flashDFPositionOpened() {
  const badge = document.getElementById('df-pos-badge');
  if (badge) { badge.classList.remove('pop'); void badge.offsetWidth; badge.classList.add('pop'); }
}

// ── Resolved trades feed ──────────────────────────────────────────────────────
function addDFResolved(r) {
  DF.resolvedCount++;
  dfEl('df-resolved-badge', `${DF.resolvedCount} closed`);

  const feed = document.getElementById('df-resolved-feed');
  if (!feed) return;
  dfClearEmpty(feed);

  const pnl    = parseFloat(r.pnl_usdc) || 0;
  const result = (r.result || '').toLowerCase();
  const pnlStr = `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(4)}`;

  const card = document.createElement('div');
  card.className = `resolved-card ${result}`;
  card.innerHTML = `
    <div class="res-q">${dfEsc(r.market_question)}</div>
    <div class="res-meta">
      ${dfEsc(r.outcome)} · Entry ${dfPct(r.entry_price)} → Exit ${dfPct(r.exit_price)}
      · ${dfEsc(r.source_event || '')} · ${dfFmtTs(r.resolved_at)}
    </div>
    <div class="res-pnl ${pnl >= 0 ? 'pos' : 'neg'}">${pnlStr}</div>
  `;
  feed.insertBefore(card, feed.firstChild);
  while (feed.children.length > 40) feed.removeChild(feed.lastChild);
}

// ── Live event feed ───────────────────────────────────────────────────────────
function addDFLiveEvent(d) {
  const feed = document.getElementById('df-event-feed');
  if (!feed) return;
  dfClearEmpty(feed);

  const type = d.event_type || '';
  let icon = '●';
  let cls  = '';
  if (type === 'goal')        { icon = '⚽'; cls = 'df-event-goal'; }
  else if (type === 'red_card') { icon = '🟥'; cls = 'df-event-red'; }
  else if (type === 'match_start') { icon = '▶'; cls = 'df-event-start'; }
  else if (type === 'match_end')   { icon = '■'; cls = 'df-event-end'; }

  const score = `${d.home_score}-${d.away_score}`;
  const label = `${dfEsc(d.home_team)} vs ${dfEsc(d.away_team)}`;

  const card = document.createElement('div');
  card.className = 'opp-card';
  card.style.borderLeftColor = cls === 'df-event-goal' ? 'var(--green)'
    : cls === 'df-event-red' ? 'var(--red)' : 'var(--border-bright)';
  card.innerHTML = `
    <div class="card-row">
      <span class="card-q ${cls}">${icon} ${label}</span>
      <span class="card-time">${dfFmtTs(d.detected_at)}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="m-lbl">EVENT</span><span class="m-val ${cls}">${dfEsc(type.replace('_', ' '))}</span></div>
      <div class="metric"><span class="m-lbl">SCORE</span><span class="m-val">${score}</span></div>
      <div class="metric"><span class="m-lbl">MIN</span><span class="m-val">${d.minute || 0}'</span></div>
    </div>
  `;
  feed.insertBefore(card, feed.firstChild);
  while (feed.children.length > 50) feed.removeChild(feed.lastChild);
}

// ── Opportunity feed ──────────────────────────────────────────────────────────
function addDFOpportunity(d) {
  DF.oppCount++;
  const badge = document.getElementById('df-opp-badge');
  if (badge) {
    badge.textContent = `${DF.oppCount} found`;
    badge.classList.remove('pop'); void badge.offsetWidth; badge.classList.add('pop');
  }

  const feed = document.getElementById('df-opp-feed');
  if (!feed) return;
  dfClearEmpty(feed);

  const edge = parseFloat(d.edge_pct) || 0;
  const card = document.createElement('div');
  card.className = 'opp-card';
  card.innerHTML = `
    <div class="card-row">
      <span class="card-q">${dfEsc(d.market_question)}</span>
      <span class="card-time">${dfFmtTs(d.detected_at)}</span>
    </div>
    <div class="metrics">
      <div class="metric"><span class="m-lbl">SIDE</span><span class="m-val ${ d.outcome === 'Yes' ? 'g' : 'r' }">${dfEsc(d.outcome)}</span></div>
      <div class="metric"><span class="m-lbl">FAIR</span><span class="m-val b">${dfPct(d.fair_value)}</span></div>
      <div class="metric"><span class="m-lbl">MKT</span><span class="m-val">${dfPct(d.market_price)}</span></div>
      <div class="metric"><span class="m-lbl">EDGE</span><span class="m-val y">+${edge.toFixed(1)}%</span></div>
      <div class="metric"><span class="m-lbl">TRIGGER</span><span class="m-val">${dfEsc(d.source_event || '')}</span></div>
    </div>
  `;
  feed.insertBefore(card, feed.firstChild);
  while (feed.children.length > 30) feed.removeChild(feed.lastChild);
}

// ── API status indicator ──────────────────────────────────────────────────────
function updateDFApiStatus(d) {
  const badge = document.getElementById('df-api-badge');
  if (!badge) return;
  const remaining = d.calls_remaining || 0;
  const health    = d.health || 'green';
  badge.textContent = `API ${remaining} calls left`;
  badge.style.color = health === 'green' ? 'var(--green)'
    : health === 'yellow' ? 'var(--yellow)'
    : 'var(--red)';
}

// ── Reset button ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-df-reset');
  if (btn) {
    btn.addEventListener('click', async () => {
      if (!confirm('Reset the DataFeed portfolio?')) return;
      await fetch('/api/datafeed/reset', { method: 'POST' });
      DF.oppCount      = 0;
      DF.resolvedCount = 0;
      dfEl('df-opp-badge',      '0 found');
      dfEl('df-resolved-badge', '0 closed');
      const evtFeed = document.getElementById('df-event-feed');
      if (evtFeed) evtFeed.innerHTML = '<div class="empty">Waiting for live soccer events…</div>';
      const oppFeed = document.getElementById('df-opp-feed');
      if (oppFeed) oppFeed.innerHTML = '<div class="empty">No opportunities detected yet.</div>';
      const resFeed = document.getElementById('df-resolved-feed');
      if (resFeed) resFeed.innerHTML = '<div class="empty">No closed positions yet.</div>';
    });
  }

  initDFChart();
  hydrateDFSnapshot();
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function dfEsc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
function dfPct(v)       { return (parseFloat(v) * 100).toFixed(1) + '%'; }
function dfCommas(n)    { return parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function dfClearEmpty(el) { const e = el.querySelector('.empty'); if (e) e.remove(); }
function dfFmtTs(ts)    { return new Date(ts * 1000).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }); }
function dfAge(s) {
  s = Math.floor(parseFloat(s) || 0);
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function dfEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
