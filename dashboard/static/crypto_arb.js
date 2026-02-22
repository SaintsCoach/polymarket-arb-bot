/* ── Crypto Arb Bot dashboard module ──────────────────────────────────────── */
"use strict";

(function () {

  // ── Chart ─────────────────────────────────────────────────────────────────
  let pnlChart = null;

  function initChart() {
    const ctx = document.getElementById("ca-pnl-chart");
    if (!ctx || pnlChart) return;
    pnlChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          label: "P&L (USDC)",
          data: [],
          borderColor: "#00e87a",
          backgroundColor: "rgba(0,232,122,0.08)",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: {
            grid:  { color: "rgba(255,255,255,0.04)" },
            ticks: { color: "#525270", font: { size: 10 } },
          }
        }
      }
    });
  }

  function pushPnl(history) {
    if (!pnlChart) initChart();
    if (!pnlChart) return;
    pnlChart.data.labels  = history.map(p => new Date(p.ts * 1000).toLocaleTimeString());
    pnlChart.data.datasets[0].data = history.map(p => p.pnl);
    pnlChart.update("none");
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);

  function fmt(n, dec = 2) {
    if (n == null) return "—";
    return Number(n).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }

  function fmtPnl(n) {
    const s = (n >= 0 ? "+" : "") + fmt(n, 4);
    return `<span style="color:${n >= 0 ? "var(--green)" : "var(--red)"}">${s}</span>`;
  }

  function fmtNet(n) {
    const col = n >= 0.5 ? "var(--green)" : n >= 0 ? "var(--yellow)" : "var(--red)";
    return `<span style="color:${col}">${(n >= 0 ? "+" : "")}${fmt(n, 3)}%</span>`;
  }

  function timeStr(ts) {
    return new Date(ts * 1000).toLocaleTimeString();
  }

  function sinceStr(ts) {
    if (!ts) return "—";
    const d = Math.floor(Date.now() / 1000 - ts);
    const h = String(Math.floor(d / 3600)).padStart(2, "0");
    const m = String(Math.floor((d % 3600) / 60)).padStart(2, "0");
    const s = String(d % 60).padStart(2, "0");
    return `${h}:${m}:${s} ago`;
  }

  // ── Overview ──────────────────────────────────────────────────────────────
  function updateOverview(d) {
    if (!d) return;
    const pnl = d.realized_pnl || 0;
    $("ca-balance").textContent  = "$" + fmt(d.balance);
    $("ca-pnl").innerHTML        = fmtPnl(pnl);
    $("ca-scans").textContent    = d.scan_count  || 0;
    $("ca-opps").textContent     = d.opp_count   || 0;
    $("ca-trades").textContent   = d.trade_count || 0;
    $("ca-pairs").textContent    = d.pair_count  || "—";
    if (d.start_ts) $("ca-since").textContent = sinceStr(d.start_ts);
  }

  // ── Exchange health ───────────────────────────────────────────────────────
  function updateHealth(d) {
    if (!d) return;
    setHealth("cb", d.coinbase);
    setHealth("kr", d.kraken);
  }

  function setHealth(key, ok) {
    const dot = $(`ca-health-${key}-dot`);
    const lbl = $(`ca-health-${key}-lbl`);
    if (!dot) return;
    dot.style.background = ok ? "var(--green)" : "var(--red)";
    lbl.textContent      = ok ? "ONLINE" : "OFFLINE";
    lbl.style.color      = ok ? "var(--green)" : "var(--red)";
  }

  // ── Top pairs by opportunity count ────────────────────────────────────────
  function updateTopPairs(pairs) {
    const el = $("ca-top-pairs");
    if (!el || !pairs) return;
    const max = pairs.length ? pairs[0].count : 1;
    el.innerHTML = pairs.map(p => `
      <div style="padding:5px 12px">
        <div style="display:flex;justify-content:space-between;margin-bottom:3px">
          <span style="font-family:var(--mono);font-size:12px">${p.sym}</span>
          <span style="color:var(--yellow);font-size:11px">${p.count} opp${p.count !== 1 ? "s" : ""}</span>
        </div>
        <div style="height:3px;background:var(--bg3);border-radius:2px">
          <div style="height:3px;background:var(--yellow);border-radius:2px;width:${Math.round(p.count / max * 100)}%"></div>
        </div>
      </div>`).join("");
  }

  // ── Quality pairs panel (top 10 by raw/fee ratio) ─────────────────────────
  function updateQualityPairs(pairs, scanCount) {
    const el = $("ca-quality-pairs");
    if (!el || !pairs) return;
    const hdr = $("ca-quality-scan-lbl");
    if (hdr) hdr.textContent = `scan #${scanCount}`;

    const best = pairs.length ? pairs[0].quality : 1;
    el.innerHTML = pairs.map(p => {
      // quality bar: green at ≥1.0 (spread ≥ fee), yellow 0.5-1.0, red <0.5
      const pct = Math.min(100, Math.round((p.quality / Math.max(best, 0.001)) * 100));
      const barCol = p.quality >= 1.0 ? "var(--green)" : p.quality >= 0.5 ? "var(--yellow)" : "var(--red)";
      const netCol = p.net_pct >= 0 ? "var(--green)" : p.net_pct >= -0.3 ? "var(--yellow)" : "var(--muted)";
      const dir = `${p.buy_ex.slice(0,2).toUpperCase()}→${p.sell_ex.slice(0,2).toUpperCase()}`;
      return `
      <div style="padding:5px 12px">
        <div style="display:flex;justify-content:space-between;margin-bottom:2px;gap:6px">
          <span style="font-family:var(--mono);font-size:12px;flex:1">${p.sym}</span>
          <span style="font-size:10px;color:var(--muted)">${dir}</span>
          <span style="font-size:11px;color:${barCol};min-width:38px;text-align:right">Q ${fmt(p.quality,3)}</span>
          <span style="font-size:11px;color:${netCol};min-width:52px;text-align:right">${(p.net_pct>=0?"+":"")}${fmt(p.net_pct,3)}%</span>
        </div>
        <div style="height:3px;background:var(--bg3);border-radius:2px">
          <div style="height:3px;background:${barCol};border-radius:2px;width:${pct}%"></div>
        </div>
      </div>`;
    }).join("");
  }

  // ── Scan result feed ──────────────────────────────────────────────────────
  function updateScanFeed(pairs, scanCount, totalPairs) {
    const tbody = $("ca-scan-tbody");
    const meta  = $("ca-scan-meta");
    if (!tbody) return;
    if (meta) meta.textContent = `scan #${scanCount} · ${totalPairs} pairs · sorted by quality`;

    // Already sorted by quality descending from the bot; show top 15
    const top = (pairs || []).slice(0, 15);
    tbody.innerHTML = top.map(p => {
      const netCol  = p.net_pct >= 0 ? "var(--green)" : p.net_pct >= -0.3 ? "var(--yellow)" : "var(--muted)";
      const qualCol = (p.quality||0) >= 1.0 ? "var(--green)" : (p.quality||0) >= 0.5 ? "var(--yellow)" : "var(--muted)";
      const dir = `${(p.buy_ex||"?").slice(0,2).toUpperCase()}→${(p.sell_ex||"?").slice(0,2).toUpperCase()}`;
      return `<tr>
        <td style="font-family:var(--mono)">${p.sym}</td>
        <td style="font-size:10px;color:var(--muted)">${dir}</td>
        <td style="color:var(--yellow)">+${fmt(p.raw_pct, 3)}%</td>
        <td style="color:var(--muted)">${fmt(p.fee_pct, 3)}%</td>
        <td style="color:${netCol}">${(p.net_pct >= 0 ? "+" : "")}${fmt(p.net_pct, 3)}%</td>
        <td style="color:${qualCol};font-weight:600">${fmt(p.quality||0, 3)}</td>
      </tr>`;
    }).join("");
  }

  // ── Opportunity card ──────────────────────────────────────────────────────
  function prependOpportunity(opp) {
    const feed = $("ca-opp-feed");
    if (!feed) return;
    const cls = opp.net_pct >= 0.5 ? "ca-opp-profit" : opp.net_pct >= 0 ? "ca-opp-marginal" : "ca-opp-loss";
    const card = document.createElement("div");
    card.className = `ca-opp-card ${cls}`;
    card.innerHTML = `
      <div class="ca-opp-header">
        <span class="ca-opp-sym">${opp.sym}</span>
        <span class="ca-opp-net" style="color:${opp.net_pct >= 0 ? "var(--green)" : "var(--red)"}">
          NET ${opp.net_pct >= 0 ? "+" : ""}${fmt(opp.net_pct, 3)}%
        </span>
        <span class="ca-opp-time">${timeStr(opp.ts)}</span>
      </div>
      <div class="ca-opp-body">
        <span>BUY <b>${opp.buy_ex.toUpperCase()}</b> @ ${fmt(opp.buy_ask, 6)}</span>
        <span>SELL <b>${opp.sell_ex.toUpperCase()}</b> @ ${fmt(opp.sell_bid, 6)}</span>
        <span>RAW <b>${fmt(opp.raw_pct, 3)}%</b></span>
        <span>FEES <b>${fmt(opp.fee_pct, 3)}%</b></span>
        <span>SLIP <b>${fmt(opp.slip_pct, 3)}%</b></span>
        <span>EST <b style="color:${opp.est_usd >= 0 ? "var(--green)" : "var(--red)"}">$${fmt(opp.est_usd, 4)}</b></span>
      </div>`;
    feed.prepend(card);
    // Keep last 100
    while (feed.children.length > 100) feed.removeChild(feed.lastChild);

    // Update counter
    const lbl = $("ca-opp-count-lbl");
    if (lbl) lbl.textContent = feed.children.length + " detected";
  }

  function renderOpportunities(opps) {
    const feed = $("ca-opp-feed");
    if (!feed || !opps) return;
    feed.innerHTML = "";
    opps.slice().reverse().forEach(prependOpportunity);
    const lbl = $("ca-opp-count-lbl");
    if (lbl) lbl.textContent = opps.length + " detected";
  }

  // ── Trade log ─────────────────────────────────────────────────────────────
  function prependTrade(t) {
    const tbody = $("ca-trade-tbody");
    if (!tbody) return;
    const row = document.createElement("tr");
    row.innerHTML = `
      <td style="color:var(--muted);font-size:11px">${timeStr(t.ts)}</td>
      <td style="font-family:var(--mono)">${t.sym}</td>
      <td style="font-size:11px">${t.buy_ex.toUpperCase()}</td>
      <td style="font-size:11px">${t.sell_ex.toUpperCase()}</td>
      <td>${fmtNet(t.net_pct)}</td>
      <td>${fmtPnl(t.pnl_usdc)}</td>`;
    tbody.prepend(row);
    while (tbody.children.length > 200) tbody.removeChild(tbody.lastChild);
  }

  function renderTrades(trades) {
    const tbody = $("ca-trade-tbody");
    if (!tbody || !trades) return;
    tbody.innerHTML = "";
    trades.slice().reverse().forEach(prependTrade);
  }

  // ── Snapshot hydration ────────────────────────────────────────────────────
  function hydrate() {
    fetch("/api/crypto_arb/snapshot")
      .then(r => r.json())
      .then(snap => {
        updateOverview(snap.overview);
        updateHealth(snap.exchange_health);
        updateTopPairs(snap.top_pairs);
        renderOpportunities(snap.opportunities);
        renderTrades(snap.trades);
        if (snap.pnl_history && snap.pnl_history.length) pushPnl(snap.pnl_history);
        if (snap.scan_pairs && snap.scan_pairs.length) {
          updateScanFeed(snap.scan_pairs, snap.overview.scan_count, snap.overview.pair_count);
          updateQualityPairs(snap.scan_pairs.slice(0, 10), snap.overview.scan_count);
        }
      })
      .catch(() => {});
  }

  // ── WebSocket event handlers ──────────────────────────────────────────────
  function handleEvent(type, data) {
    switch (type) {
      case "arb_start":
        $("ca-since") && ($("ca-since").textContent = sinceStr(data.ts));
        break;
      case "arb_overview":
        updateOverview(data);
        break;
      case "arb_exchange_health":
        updateHealth(data);
        break;
      case "arb_scan_result":
        updateScanFeed(data.pairs, data.scan_count, data.total_pairs);
        break;
      case "arb_quality_pairs":
        updateQualityPairs(data.pairs, data.scan_count);
        break;
      case "arb_opportunity":
        prependOpportunity(data);
        break;
      case "arb_opportunities":
        renderOpportunities(data.opportunities);
        break;
      case "arb_trade":
        prependTrade(data);
        break;
      case "arb_trades":
        renderTrades(data.trades);
        break;
      case "arb_top_pairs":
        updateTopPairs(data.pairs);
        break;
      case "arb_pnl":
        pushPnl(data.history);
        break;
    }
  }

  // ── Reset ─────────────────────────────────────────────────────────────────
  function init() {
    initChart();
    hydrate();

    const resetBtn = $("ca-reset-btn");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        if (!confirm("Reset Crypto Arb portfolio?")) return;
        fetch("/api/crypto_arb/reset", { method: "POST" })
          .then(() => {
            $("ca-opp-feed").innerHTML = "";
            $("ca-trade-tbody").innerHTML = "";
            $("ca-scan-tbody").innerHTML = "";
            $("ca-top-pairs").innerHTML = "";
            if (pnlChart) { pnlChart.data.labels = []; pnlChart.data.datasets[0].data = []; pnlChart.update(); }
          });
      });
    }
  }

  // ── Register with global WS dispatcher ───────────────────────────────────
  window.__cryptoArbHandleEvent = handleEvent;
  window.__cryptoArbInit        = init;

})();

// Hook into the main app.js WS dispatcher (called after app.js loads)
document.addEventListener("DOMContentLoaded", () => {
  if (window.__cryptoArbInit) window.__cryptoArbInit();

  // Register all arb_* event types with the shared dispatcher
  const ARB_EVENTS = [
    "arb_start", "arb_overview", "arb_exchange_health",
    "arb_scan_result", "arb_opportunity", "arb_opportunities",
    "arb_trade", "arb_trades", "arb_top_pairs", "arb_pnl",
    "arb_quality_pairs",
  ];
  if (typeof registerHandler !== "undefined") {
    ARB_EVENTS.forEach(t => registerHandler(t, d => window.__cryptoArbHandleEvent(t, d)));
  }
});
