/* KalshiTrader dashboard: REST for state, WebSocket for the live stream. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const fmtUsd = (cents) =>
  cents == null ? "—" : (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
const fmtTime = (iso) => new Date(iso).toLocaleTimeString();

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || resp.statusText);
  }
  return resp.json();
}

/* ── Equity chart ─────────────────────────────────────────────────── */

const chart = new Chart($("#equity-chart"), {
  type: "line",
  data: {
    labels: [],
    datasets: [
      {
        label: "Equity",
        data: [],
        borderColor: "#4f8ff7",
        backgroundColor: "rgba(79,143,247,0.12)",
        fill: true,
        tension: 0.25,
        pointRadius: 0,
      },
      {
        label: "Cash",
        data: [],
        borderColor: "#2fbf71",
        borderDash: [4, 4],
        fill: false,
        tension: 0.25,
        pointRadius: 0,
      },
    ],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
      x: { ticks: { color: "#7a8399", maxTicksLimit: 8 }, grid: { color: "#1a2030" } },
      y: {
        ticks: { color: "#7a8399", callback: (v) => "$" + (v / 100).toFixed(2) },
        grid: { color: "#1a2030" },
      },
    },
    plugins: { legend: { labels: { color: "#d6dbe8" } } },
  },
});

async function refreshEquity() {
  const history = await api("/api/equity_history");
  chart.data.labels = history.map((h) => fmtTime(h.time));
  chart.data.datasets[0].data = history.map((h) => h.equity_cents);
  chart.data.datasets[1].data = history.map((h) => h.balance_cents);
  chart.update();
}

/* ── Overview metrics ─────────────────────────────────────────────── */

function renderOverview(o) {
  $("#m-equity").textContent = fmtUsd(o.equity_cents);
  $("#m-balance").textContent = fmtUsd(o.balance_cents);
  $("#m-exposure").textContent = fmtUsd(o.exposure_cents);
  const pnl = (el, cents) => {
    el.textContent = fmtUsd(cents);
    el.className = cents > 0 ? "pos" : cents < 0 ? "neg" : "";
  };
  pnl($("#m-realized"), o.realized_pnl_cents);
  pnl($("#m-unrealized"), o.unrealized_pnl_cents);
  $("#m-orders").textContent = o.orders_placed ?? "—";
  $("#halt-badge").hidden = !o.halted;
  $("#risk-reset").hidden = !o.halted;
}

/* ── Bot controller ───────────────────────────────────────────────── */

async function refreshStatus() {
  const status = await api("/api/status");
  const badge = $("#env-badge");
  badge.textContent = status.env.toUpperCase();
  badge.className = "badge " + status.env;
  $("#halt-badge").hidden = !status.halted;
  $("#risk-reset").hidden = !status.halted;

  const wrap = $("#strategies");
  wrap.innerHTML = "";
  for (const [name, info] of Object.entries(status.strategies)) {
    const row = document.createElement("div");
    row.className = "strategy-row";
    row.innerHTML = `
      <div class="name">${info.label}
        <small>${info.places_orders ? "places orders" : "signal only"}</small>
      </div>
      <span class="state ${info.state}">${info.state}</span>
      <button class="btn" data-cmd="start">▶</button>
      <button class="btn" data-cmd="pause">⏸</button>
      <button class="btn danger" data-cmd="stop">⏹</button>`;
    row.querySelectorAll("button").forEach((btn) =>
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/bot/${name}/${btn.dataset.cmd}`, { method: "POST" });
          await refreshStatus();
        } catch (err) {
          logLine({ level: "error", source: "ui", message: err.message, time: new Date().toISOString() });
        }
      })
    );
    wrap.appendChild(row);
  }

  if (!status.client_ready) {
    logLine({
      level: "error",
      source: "config",
      message: `API client not ready: ${status.client_error || "check .env credentials"}`,
      time: new Date().toISOString(),
    });
  }
}

/* ── Tables ───────────────────────────────────────────────────────── */

async function refreshOrders() {
  const orders = await api("/api/orders");
  $("#orders-table tbody").innerHTML = orders
    .map(
      (o) => `<tr>
        <td>${fmtTime(o.time)}</td><td>${o.strategy}</td><td>${o.ticker}</td>
        <td class="${o.action}">${o.action}</td><td>${o.side}</td>
        <td>${o.count}</td><td>${o.price_cents}c</td><td>${o.status}</td></tr>`
    )
    .join("");
}

async function refreshFills() {
  const fills = await api("/api/fills");
  $("#fills-table tbody").innerHTML = fills
    .map(
      (f) => `<tr>
        <td>${fmtTime(f.time)}</td><td>${f.ticker}</td>
        <td class="${f.action}">${f.action}</td><td>${f.side}</td>
        <td>${f.count}</td><td>${f.price_cents}c</td></tr>`
    )
    .join("");
}

/* ── Settings ─────────────────────────────────────────────────────── */

async function loadSettings() {
  const s = await api("/api/settings");
  const form = $("#settings-form");
  form.env.value = s.env;
  form.scan_interval_seconds.value = s.scan_interval_seconds;
  form.contracts_per_side.value = s.contracts_per_side;
  form.min_profit_cents.value = s.min_profit_cents;
  form.edge_buffer_cents.value = s.edge_buffer_cents;
  form.max_money_working_dollars.value = (s.max_money_working_cents / 100).toFixed(0);
  form.max_contracts_per_order.value = s.max_contracts_per_order;
  form.daily_stop_loss_pct.value = s.daily_stop_loss_pct;
  form.order_ttl_seconds.value = s.order_ttl_seconds;
  form.arb_tickers.value = s.arb_tickers;
  form.arb_series.value = s.arb_series;
  form.fair_values.value = JSON.stringify(s.fair_values);
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const statusEl = $("#settings-status");
  let fairValues;
  try {
    fairValues = form.fair_values.value.trim() ? JSON.parse(form.fair_values.value) : {};
  } catch {
    statusEl.textContent = "invalid fair-values JSON";
    statusEl.style.color = "var(--red)";
    return;
  }
  const patch = {
    env: form.env.value,
    scan_interval_seconds: parseFloat(form.scan_interval_seconds.value),
    contracts_per_side: parseInt(form.contracts_per_side.value),
    min_profit_cents: parseInt(form.min_profit_cents.value),
    edge_buffer_cents: parseInt(form.edge_buffer_cents.value),
    max_money_working_cents: Math.round(parseFloat(form.max_money_working_dollars.value) * 100),
    max_contracts_per_order: parseInt(form.max_contracts_per_order.value),
    daily_stop_loss_pct: parseFloat(form.daily_stop_loss_pct.value),
    order_ttl_seconds: parseInt(form.order_ttl_seconds.value),
    arb_tickers: form.arb_tickers.value,
    arb_series: form.arb_series.value,
    fair_values: fairValues,
  };
  if (patch.env === "live") {
    const ok = confirm(
      "⚠️ Switch to LIVE trading with real money?\n\n" +
        "Make sure you've validated in demo mode first and that Max Allocation is set low."
    );
    if (!ok) return;
    patch.confirm_live = true;
  }
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
    statusEl.textContent = "saved ✓";
    statusEl.style.color = "var(--green)";
    await refreshStatus();
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.style.color = "var(--red)";
  }
});

/* ── Controller footer buttons ────────────────────────────────────── */

$("#cancel-all").addEventListener("click", async () => {
  if (!confirm("Cancel ALL resting orders?")) return;
  const res = await api("/api/orders/cancel_all", { method: "POST" });
  logLine({
    level: "warn", source: "ui",
    message: `cancelled ${res.cancelled} order(s)`,
    time: new Date().toISOString(),
  });
});

$("#risk-reset").addEventListener("click", async () => {
  if (!confirm("Reset the risk circuit breaker and allow trading again?")) return;
  await api("/api/risk/reset", { method: "POST" });
  await refreshStatus();
});

/* ── Activity log + WebSocket ─────────────────────────────────────── */

const MAX_LOG_LINES = 500;

function logLine(entry) {
  const log = $("#activity-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const div = document.createElement("div");
  div.className = `line ${entry.level || "info"}`;
  div.innerHTML = `<span class="t">${fmtTime(entry.time)}</span><span class="src">[${entry.source}]</span>${entry.message}`;
  log.appendChild(div);
  while (log.children.length > MAX_LOG_LINES) log.removeChild(log.firstChild);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $("#conn-badge").textContent = "live";
    $("#conn-badge").className = "badge on";
  };
  ws.onclose = () => {
    $("#conn-badge").textContent = "disconnected";
    $("#conn-badge").className = "badge off";
    setTimeout(connectWs, 3000);
  };
  ws.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    if (event.type === "activity") logLine(event);
    else if (event.type === "overview") {
      renderOverview(event);
      refreshEquity();
    } else if (event.type === "orders_changed") {
      refreshOrders();
      refreshFills();
    }
  };
}

/* ── Boot ─────────────────────────────────────────────────────────── */

(async function init() {
  try {
    const activity = await api("/api/activity");
    activity.forEach(logLine);
    renderOverview(await api("/api/overview"));
    await Promise.all([refreshStatus(), refreshEquity(), refreshOrders(), refreshFills(), loadSettings()]);
  } catch (err) {
    logLine({ level: "error", source: "ui", message: `init failed: ${err.message}`, time: new Date().toISOString() });
  }
  connectWs();
  setInterval(() => {
    refreshOrders().catch(() => {});
    refreshFills().catch(() => {});
  }, 30000);
})();
