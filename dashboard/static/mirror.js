'use strict';
// mirror.js — Mirror Bot frontend logic
// Hooks into the registerHandler() plugin API in app.js

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${tab}`).classList.add('active');
  });
});

// ── State ─────────────────────────────────────────────────────────────────────
const M = {
  resolvedCount: 0,
  addresses: {},      // address → data dict
};

// ── Mirror P&L Chart ──────────────────────────────────────────────────────────
let mirrorChart;
const MC = {
  data:    [0],
  labels:  [],
  lastPnl: null,
};

function initMirrorChart() {
  const ctx = document.getElementById('mirror-pnl-chart');
  if (!ctx) return;
  MC.labels = [fmtTs(Date.now() / 1000)];
  mirrorChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: MC.labels,
      datasets: [{
        data: MC.data,
        borderColor:     '#00e87a',
        backgroundColor: mirrorGradient,
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
            title: items => MC.labels[items[0].dataIndex] || '',
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

function mirrorGradient(ctx) {
  const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height || 200);
  g.addColorStop(0, 'rgba(0,232,122,0.22)');
  g.addColorStop(1, 'rgba(0,232,122,0.00)');
  return g;
}

function pushMirrorPnL(totalPnl, ts) {
  if (!mirrorChart) return;
  const y = parseFloat(totalPnl) || 0;
  if (MC.lastPnl === y) return;
  MC.lastPnl = y;

  MC.data.push(y);
  MC.labels.push(fmtTs(ts || Date.now() / 1000));
  if (MC.data.length > 300) { MC.data.shift(); MC.labels.shift(); }

  const color = y >= 0 ? '#00e87a' : '#ff3361';
  mirrorChart.data.datasets[0].borderColor = color;
  mirrorChart.data.datasets[0].data        = MC.data;
  mirrorChart.data.labels                  = MC.labels;
  mirrorChart.update('none');

  const el = document.getElementById('mirror-chart-pnl');
  if (el) {
    el.textContent = `${ y >= 0 ? '+' : '' }$${ Math.abs(y).toFixed(4) }`;
    el.className   = `ov-val ${ y >= 0 ? 'g' : 'r' }`;
  }
}

function resetMirrorChart(ts) {
  MC.data    = [0];
  MC.labels  = [fmtTs(ts || Date.now() / 1000)];
  MC.lastPnl = null;
  if (!mirrorChart) return;
  mirrorChart.data.datasets[0].data        = MC.data;
  mirrorChart.data.datasets[0].borderColor = '#00e87a';
  mirrorChart.data.labels                  = MC.labels;
  mirrorChart.update('none');
  const el = document.getElementById('mirror-chart-pnl');
  if (el) { el.textContent = '+$0.0000'; el.className = 'ov-val g'; }
}

// ── Register event handlers ───────────────────────────────────────────────────
registerHandler('mirror_overview',        renderOverview);
registerHandler('mirror_positions',       d => renderPositions(d.positions));
registerHandler('mirror_queue',           d => renderQueue(d.queue));
registerHandler('mirror_position_opened', d => flashPositionOpened(d));
registerHandler('mirror_position_closed', d => addResolved(d));
registerHandler('mirror_address_status',  renderAddressCard);
registerHandler('mirror_addresses',       d => renderAllAddresses(d.addresses));
registerHandler('mirror_api_event',       handleApiEvent);
registerHandler('mirror_poll_debug',      handlePollDebug);
registerHandler('mirror_bot_start',       d => {
  const el = document.getElementById('m-start-ts');
  if (el) el.textContent = fmtTs(d.ts);
  resetMirrorChart(d.ts);
});

// ── Overview panel ────────────────────────────────────────────────────────────
function renderOverview(d, ts) {
  mirrorEl('m-balance',   `$${commas2(d.balance_usdc)}`);
  mirrorEl('m-deployed',  `$${commas2(d.total_deployed)}`);
  mirrorEl('m-slot-badge', `${d.slots_used} / ${d.slots_total} slots`);
  mirrorEl('m-queue-cnt', d.queue_size);
  mirrorEl('m-pos-badge', `${d.slots_used} open`, 'm-pos-badge');
  mirrorEl('m-queue-badge', `${d.queue_size} queued`);

  setPnlEl('m-realized',   d.realized_pnl);
  setPnlEl('m-unrealized', d.unrealized_pnl);
  setPnlEl('m-total-pnl',  d.total_pnl);

  pushMirrorPnL(d.total_pnl, ts);
}

function setPnlEl(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = parseFloat(val) || 0;
  el.textContent = `${v >= 0 ? '+' : ''}$${Math.abs(v).toFixed(4)}`;
  el.className   = `ov-val ${v >= 0 ? 'g' : 'r'}`;
}

// ── Positions table ───────────────────────────────────────────────────────────
function renderPositions(positions) {
  const tbody = document.getElementById('pos-tbody');
  if (!positions || !positions.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No open positions.</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const pnl = parseFloat(p.unrealized_pnl) || 0;
    const pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pnlStr = `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(4)}`;
    const sideCls = (p.outcome || '').toLowerCase() === 'yes' ? 'side-yes' : 'side-no';
    const age = fmtAge(p.age_s);
    return `
      <tr>
        <td title="${esc2(p.market_question)}">${esc2(p.market_question.slice(0,45))}…</td>
        <td><span class="${sideCls}">${esc2(p.outcome)}</span></td>
        <td class="mono-sm">${pct2(p.entry_price)}</td>
        <td class="mono-sm">${pct2(p.current_price)}</td>
        <td class="${pnlCls}">${pnlStr}</td>
        <td class="mono-sm">${age}</td>
        <td class="mono-sm">${esc2(p.triggered_by)}</td>
      </tr>`;
  }).join('');
}

// ── Queue table ───────────────────────────────────────────────────────────────
function renderQueue(queue) {
  const tbody = document.getElementById('queue-tbody');
  if (!queue || !queue.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Queue is empty.</td></tr>';
    return;
  }
  tbody.innerHTML = queue.map(q => {
    const sideCls = (q.outcome || '').toLowerCase() === 'yes' ? 'side-yes' : 'side-no';
    return `
      <tr>
        <td title="${esc2(q.market_question)}">${esc2(q.market_question.slice(0,42))}…</td>
        <td><span class="${sideCls}">${esc2(q.outcome)}</span></td>
        <td class="mono-sm">${pct2(q.entry_price)}</td>
        <td class="mono-sm">${esc2(q.triggered_by)}</td>
        <td class="mono-sm">${fmtTs(q.queued_at)}</td>
      </tr>`;
  }).join('');
}

// ── Resolved trades feed ──────────────────────────────────────────────────────
function addResolved(r) {
  M.resolvedCount++;
  document.getElementById('m-resolved-badge').textContent = `${M.resolvedCount} closed`;

  const feed = document.getElementById('m-resolved-feed');
  clearEmpty2(feed);

  const pnl    = parseFloat(r.pnl_usdc) || 0;
  const result = (r.result || '').toLowerCase();   // win | loss | push
  const pnlStr = `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(4)}`;

  const card = document.createElement('div');
  card.className = `resolved-card ${result}`;
  card.innerHTML = `
    <div class="res-q">${esc2(r.market_question)}</div>
    <div class="res-meta">
      ${esc2(r.outcome)} · Entry ${pct2(r.entry_price)} → Exit ${pct2(r.exit_price)}
      · ${esc2(r.triggered_by)} · ${fmtTs(r.resolved_at)}
    </div>
    <div class="res-pnl ${pnl >= 0 ? 'pos' : 'neg'}">${pnlStr}</div>
  `;
  feed.insertBefore(card, feed.firstChild);
  while (feed.children.length > 40) feed.removeChild(feed.lastChild);
}

// ── Address cards ─────────────────────────────────────────────────────────────
function renderAddressCard(a) {
  M.addresses[a.address] = a;
  rebuildAddressList();
}

function renderAllAddresses(addresses) {
  M.addresses = {};
  addresses.forEach(a => { M.addresses[a.address] = a; });
  rebuildAddressList();
}

function rebuildAddressList() {
  const list = document.getElementById('addr-list');
  const entries = Object.values(M.addresses);
  if (!entries.length) {
    list.innerHTML = '<div class="empty">No addresses configured.</div>';
    return;
  }
  list.innerHTML = '';
  entries.forEach(a => {
    const health = a.health || 'ok';
    const stats  = a.stats || {};
    const pnl    = parseFloat(stats.total_pnl_usdc) || 0;
    const pnlStr = `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}`;
    const pnlCls = pnl >= 0 ? '' : 'style="color:var(--red)"';
    const enabled = a.enabled !== false;

    const card = document.createElement('div');
    card.className = `addr-card ${health}`;
    card.innerHTML = `
      <div class="addr-row1">
        <span class="addr-nick">${esc2(a.nickname)}</span>
        <span class="health-dot ${health}" title="${health}"></span>
      </div>
      <div class="addr-hex">${a.address.slice(0, 12)}…${a.address.slice(-6)}</div>
      <div class="addr-stats">
        <span class="addr-stat">Trades: <span>${stats.trades_mirrored || 0}</span></span>
        <span class="addr-stat">W/L: <span>${stats.wins || 0}/${stats.losses || 0}</span></span>
        <span class="addr-stat">PnL: <span ${pnlCls}>${pnlStr}</span></span>
        <span class="addr-stat">Fails: <span>${a.consecutive_failures || 0}</span></span>
      </div>
      <div class="addr-actions">
        <button class="btn-sm" onclick="toggleAddr('${esc2(a.address)}', ${!enabled})">
          ${enabled ? 'Pause' : 'Resume'}
        </button>
        <button class="btn-sm danger" onclick="removeAddr('${esc2(a.address)}')">Remove</button>
      </div>
    `;
    list.appendChild(card);
  });
}

function flashPositionOpened(p) {
  // Brief badge flash when a position is newly opened
  const badge = document.getElementById('m-pos-badge');
  if (badge) { badge.classList.remove('pop'); void badge.offsetWidth; badge.classList.add('pop'); }
}

// ── API event handler (retry / rate limit / poll error) ───────────────────────
function handleApiEvent(d) {
  // Could show toast notifications; for now just log quietly.
  // console.debug('[mirror api]', d);
}

// ── Poll debug panel ──────────────────────────────────────────────────────────
function handlePollDebug(d) {
  const log = document.getElementById('debug-log');
  if (!log) return;

  const statusCls = d.new_count > 0 ? 'de-new' : d.initialized ? 'de-ok' : 'de-warn';
  const statusTxt = !d.initialized
    ? `BASELINE (${d.fetched} positions captured)`
    : d.new_count > 0
      ? `${d.new_count} NEW  |  ${d.closed_count} closed  |  ${d.fetched} active`
      : `no change  |  ${d.fetched} active`;

  let inner = `
    <span class="de-time">${fmtTs(d.ts)}</span>
    <span class="de-addr">  ${esc2(d.nickname)}</span>
    <span class="${statusCls}">  ${statusTxt}</span>`;

  if (d.opened && d.opened.length) {
    d.opened.forEach(p => {
      inner += `<span class="de-item de-new">↑ ${esc2(p.title)}  @${p.price != null ? (p.price*100).toFixed(1)+'%' : '?'}</span>`;
    });
  }
  if (d.closed && d.closed.length) {
    d.closed.forEach(p => {
      inner += `<span class="de-item de-warn">↓ ${esc2(p.title)}</span>`;
    });
  }

  const entry = document.createElement('div');
  entry.className = 'debug-entry';
  entry.innerHTML = inner;
  log.insertBefore(entry, log.firstChild);
  while (log.children.length > 50) log.removeChild(log.lastChild);
}

// ── Debug panel toggle ────────────────────────────────────────────────────────
document.getElementById('debug-toggle').addEventListener('click', () => {
  document.getElementById('debug-panel').classList.toggle('open');
});
document.getElementById('debug-close').addEventListener('click', () => {
  document.getElementById('debug-panel').classList.remove('open');
});

// ── Reset button ──────────────────────────────────────────────────────────────
document.getElementById('btn-reset').addEventListener('click', async () => {
  if (!confirm('Reset the mirror portfolio and take a fresh snapshot?')) return;
  await fetch('/api/mirror/reset', { method: 'POST' });
});

// ── Address management buttons ────────────────────────────────────────────────
document.getElementById('btn-add-addr').addEventListener('click', () => {
  document.getElementById('add-form').style.display = 'flex';
  document.getElementById('btn-add-addr').style.display = 'none';
});
document.getElementById('btn-cancel-addr').addEventListener('click', () => {
  document.getElementById('add-form').style.display = 'none';
  document.getElementById('btn-add-addr').style.display = '';
  document.getElementById('f-addr').value = '';
  document.getElementById('f-nick').value = '';
});
document.getElementById('btn-submit-addr').addEventListener('click', async () => {
  const address  = document.getElementById('f-addr').value.trim();
  const nickname = document.getElementById('f-nick').value.trim() || address.slice(0, 8);
  if (!address) return;
  try {
    const resp = await fetch('/api/mirror/addresses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ address, nickname }),
    });
    if (resp.ok) {
      document.getElementById('btn-cancel-addr').click();
    } else {
      alert('Failed to add address: ' + resp.status);
    }
  } catch (e) { alert('Network error: ' + e.message); }
});

window.toggleAddr = async (address, enabled) => {
  await fetch(`/api/mirror/addresses/${encodeURIComponent(address)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
};

window.removeAddr = async (address) => {
  if (!confirm(`Remove ${address.slice(0, 12)}…?`)) return;
  await fetch(`/api/mirror/addresses/${encodeURIComponent(address)}`, { method: 'DELETE' });
};

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', initMirrorChart);

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc2(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
function pct2(v)       { return (parseFloat(v) * 100).toFixed(1) + '%'; }
function commas2(n)    { return parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function clearEmpty2(el){ const e = el.querySelector('.empty'); if (e) e.remove(); }
function fmtTs(ts)     { return new Date(ts * 1000).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }); }
function fmtAge(s)     {
  s = Math.floor(parseFloat(s) || 0);
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m`;
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}
function mirrorEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
