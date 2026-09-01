/* KalshiTrader dashboard: login-gated; REST for state, WebSocket for live stream. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const fmtUsd = (cents) =>
  cents == null ? "—" : (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
// Server timestamps are UTC but arrive without a timezone marker; tag them
// as UTC so they render in the viewer's local time.
const parseTs = (iso) =>
  new Date(typeof iso === "string" && !/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso + "Z" : iso);
const fmtTime = (iso) => parseTs(iso).toLocaleTimeString();
const fmtDateTime = (iso) => {
  const d = parseTs(iso);
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString()}`;
};

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (resp.status === 401) {
    showLogin();
    throw new Error("not authenticated");
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || resp.statusText);
  }
  return resp.json();
}

/* ── Login / setup ────────────────────────────────────────────────── */

let setupMode = false;

function showLogin() {
  $("#login-overlay").hidden = false;
}

async function checkAuth() {
  const status = await fetch("/api/auth/status").then((r) => r.json());
  setupMode = status.setup_required;
  if (setupMode) {
    $("#login-subtitle").textContent = "First run — create a dashboard password (min 8 characters)";
    $("#login-password2").hidden = false;
    $("#login-password").autocomplete = "new-password";
    $("#login-submit").textContent = "Create Password";
  }
  if (!status.authenticated || setupMode) {
    showLogin();
    return false;
  }
  return true;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = $("#login-error");
  errEl.textContent = "";
  const password = $("#login-password").value;
  if (setupMode && password !== $("#login-password2").value) {
    errEl.textContent = "passwords do not match";
    return;
  }
  try {
    const path = setupMode ? "/api/auth/setup" : "/api/auth/login";
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    $("#login-overlay").hidden = true;
    await boot();
  } catch (err) {
    errEl.textContent = err.message;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  location.reload();
});

/* ── Charts ───────────────────────────────────────────────────────── */

// Never let chart setup take down the rest of the dashboard.
function makeChart(canvasSel, config) {
  try {
    return new Chart($(canvasSel), config);
  } catch (err) {
    console.error(`chart init failed for ${canvasSel}:`, err);
    return null;
  }
}

const axisOpts = (moneyAxis = true) => ({
  x: { ticks: { color: "#7a8399", maxTicksLimit: 8 }, grid: { color: "#1a2030" } },
  y: {
    ticks: {
      color: "#7a8399",
      callback: moneyAxis ? (v) => "$" + (v / 100).toFixed(2) : undefined,
    },
    grid: { color: "#1a2030" },
  },
});

const equityChart = makeChart("#equity-chart", {
  type: "line",
  data: {
    labels: [],
    datasets: [
      { label: "Equity", data: [], borderColor: "#4f8ff7", backgroundColor: "rgba(79,143,247,0.12)", fill: true, tension: 0.25, pointRadius: 0 },
      { label: "Cash", data: [], borderColor: "#2fbf71", borderDash: [4, 4], fill: false, tension: 0.25, pointRadius: 0 },
    ],
  },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: axisOpts(),
    plugins: { legend: { labels: { color: "#d6dbe8" } } },
  },
});

const profitChart = makeChart("#profit-chart", {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      label: "Net Profit",
      data: [],
      borderColor: "#2fbf71",
      backgroundColor: "rgba(47,191,113,0.12)",
      fill: true,
      stepped: true,
      pointRadius: 2,
    }],
  },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: axisOpts(),
    plugins: { legend: { display: false } },
  },
});

const tickerChart = makeChart("#ticker-chart", {
  type: "bar",
  data: { labels: [], datasets: [{ label: "Net PnL", data: [], backgroundColor: [] }] },
  options: {
    indexAxis: "y",
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: {
      x: { ticks: { color: "#7a8399", callback: (v) => "$" + (v / 100).toFixed(2) }, grid: { color: "#1a2030" } },
      y: { ticks: { color: "#7a8399", font: { size: 10 } }, grid: { display: false } },
    },
    plugins: { legend: { display: false } },
  },
});

async function refreshEquity() {
  const history = await api("/api/equity_history");
  if (!equityChart) return;
  equityChart.data.labels = history.map((h) => fmtTime(h.time));
  equityChart.data.datasets[0].data = history.map((h) => h.equity_cents);
  equityChart.data.datasets[1].data = history.map((h) => h.balance_cents);
  equityChart.update();
}

/* ── PnL / trades ─────────────────────────────────────────────────── */

async function refreshPnl() {
  const pnl = await api("/api/pnl");

  const netEl = $("#m-net");
  netEl.textContent = fmtUsd(pnl.total_net_cents);
  netEl.className = pnl.total_net_cents > 0 ? "pos" : pnl.total_net_cents < 0 ? "neg" : "";
  $("#m-trades").textContent = pnl.trades.length;

  if (profitChart) {
    profitChart.data.labels = pnl.cumulative.map((p) => fmtDateTime(p.time));
    profitChart.data.datasets[0].data = pnl.cumulative.map((p) => p.net_cents);
    profitChart.update();
  }

  if (tickerChart) {
    const tickers = Object.keys(pnl.by_ticker);
    tickerChart.data.labels = tickers.map((t) => (t.length > 22 ? t.slice(0, 20) + "…" : t));
    tickerChart.data.datasets[0].data = tickers.map((t) => pnl.by_ticker[t]);
    tickerChart.data.datasets[0].backgroundColor = tickers.map((t) =>
      pnl.by_ticker[t] >= 0 ? "rgba(47,191,113,0.7)" : "rgba(229,72,77,0.7)"
    );
    tickerChart.update();
  }

  $("#trades-table tbody").innerHTML = pnl.trades
    .map((t) => {
      const cls = t.net_cents > 0 ? "buy" : t.net_cents < 0 ? "sell" : "";
      return `<tr>
        <td>${fmtDateTime(t.time)}</td><td>${t.ticker}</td><td>${t.side}</td>
        <td>${t.count}</td><td>${fmtUsd(t.cost_cents)}</td><td>${fmtUsd(t.proceeds_cents)}</td>
        <td class="${cls}">${fmtUsd(t.net_cents)}</td><td>${t.kind}</td></tr>`;
    })
    .join("");
}

/* ── Overview metrics ─────────────────────────────────────────────── */

function renderOverview(o) {
  $("#m-equity").textContent = fmtUsd(o.equity_cents);
  $("#m-balance").textContent = fmtUsd(o.balance_cents);
  $("#m-exposure").textContent = fmtUsd(o.exposure_cents);
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
      message: `API client not ready: ${status.client_error || "add credentials in the API Credentials panel"}`,
      time: new Date().toISOString(),
    });
  }
}

/* ── Credentials ──────────────────────────────────────────────────── */

async function refreshCredentials() {
  const creds = await api("/api/credentials");
  const rows = ["demo", "live"].map((env) => {
    const c = creds[env];
    const state = c.configured
      ? `<span class="ok">✓ ${c.key_id_masked} (${c.source})</span>`
      : `<span class="missing">not configured</span>`;
    const active = creds.active_env === env ? " ← active" : "";
    return `<div class="row"><span>${env.toUpperCase()}${active}</span>${state}</div>`;
  });
  $("#cred-status").innerHTML = rows.join("");
}

$("#cred-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) $("#cred-form").private_key_pem.value = await file.text();
});

$("#cred-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const resultEl = $("#cred-result");
  resultEl.textContent = "testing…";
  resultEl.style.color = "var(--muted)";
  try {
    const res = await api("/api/credentials", {
      method: "PUT",
      body: JSON.stringify({
        env: form.env.value,
        key_id: form.key_id.value,
        private_key_pem: form.private_key_pem.value,
      }),
    });
    if (res.connection_ok === false) {
      resultEl.textContent = `saved, but connection failed: ${res.error}`;
      resultEl.style.color = "var(--amber)";
    } else if (res.connection_ok) {
      resultEl.textContent = `connected ✓ balance ${fmtUsd(res.balance_cents)}`;
      resultEl.style.color = "var(--green)";
    } else {
      resultEl.textContent = "saved ✓ (inactive environment, not tested)";
      resultEl.style.color = "var(--green)";
    }
    form.key_id.value = "";
    form.private_key_pem.value = "";
    $("#cred-file").value = "";
    await Promise.all([refreshCredentials(), refreshStatus()]);
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.style.color = "var(--red)";
  }
});

/* ── Tables ───────────────────────────────────────────────────────── */

async function refreshOrders() {
  const orders = await api("/api/orders");
  $("#orders-table tbody").innerHTML = orders
    .map(
      (o) => `<tr>
        <td>${fmtDateTime(o.time)}</td><td>${o.strategy}</td><td>${o.ticker}</td>
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
        <td>${fmtDateTime(f.time)}</td><td>${f.ticker}</td>
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
  form.swing_series.value = s.swing_series;
  form.swing_drop_cents.value = s.swing_drop_cents;
  form.swing_take_profit_cents.value = s.swing_take_profit_cents;
  form.swing_stop_loss_cents.value = s.swing_stop_loss_cents;
  form.swing_max_hold_minutes.value = s.swing_max_hold_minutes;
  form.swing_max_positions.value = s.swing_max_positions;

  // Restore user's Target Series/Tickers from localStorage (persists across reloads)
  try {
    const saved = JSON.parse(localStorage.getItem("kalshi_target_settings") || "{}");
    if (saved.arb_tickers) form.arb_tickers.value = saved.arb_tickers;
    if (saved.arb_series) form.arb_series.value = saved.arb_series;
    if (saved.swing_series) form.swing_series.value = saved.swing_series;
  } catch (e) {
    // Ignore localStorage parse errors
  }
}

// Save Target Series/Tickers to localStorage when settings load
function saveTargetSettingsToStorage(form) {
  try {
    localStorage.setItem("kalshi_target_settings", JSON.stringify({
      arb_tickers: form.arb_tickers.value,
      arb_series: form.arb_series.value,
      swing_series: form.swing_series.value,
    }));
  } catch (e) {
    // localStorage may be full or disabled; fail silently
  }
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
    swing_series: form.swing_series.value,
    swing_drop_cents: parseInt(form.swing_drop_cents.value),
    swing_take_profit_cents: parseInt(form.swing_take_profit_cents.value),
    swing_stop_loss_cents: parseInt(form.swing_stop_loss_cents.value),
    swing_max_hold_minutes: parseInt(form.swing_max_hold_minutes.value),
    swing_max_positions: parseInt(form.swing_max_positions.value),
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
    saveTargetSettingsToStorage(form);
    await Promise.all([refreshStatus(), refreshCredentials()]);
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.style.color = "var(--red)";
  }
});

/* ── Controller footer buttons ────────────────────────────────────── */

$("#manual-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const resultEl = $("#manual-result");
  const body = {
    ticker: form.ticker.value.trim().toUpperCase(),
    side: form.side.value,
    action: form.action.value,
    count: parseInt(form.count.value),
    price_cents: parseInt(form.price_cents.value),
  };
  const cost = ((body.count * body.price_cents) / 100).toFixed(2);
  if (
    !confirm(
      `Place order: ${body.action.toUpperCase()} ${body.count} ${body.side.toUpperCase()} ` +
        `on ${body.ticker} @ ${body.price_cents}c?\n\nMax cost: $${cost}`
    )
  )
    return;
  resultEl.textContent = "placing…";
  resultEl.style.color = "var(--muted)";
  try {
    await api("/api/manual_order", { method: "POST", body: JSON.stringify(body) });
    resultEl.textContent = "order sent ✓ (see Orders table / activity log)";
    resultEl.style.color = "var(--green)";
    refreshOrders();
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.style.color = "var(--red)";
  }
});

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
  ws.onclose = (ev) => {
    $("#conn-badge").textContent = "disconnected";
    $("#conn-badge").className = "badge off";
    if (ev.code !== 4401) setTimeout(connectWs, 3000);
  };
  ws.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    if (event.type === "activity") logLine(event);
    else if (event.type === "overview") {
      renderOverview(event);
      refreshEquity().catch(() => {});
    } else if (event.type === "orders_changed") {
      refreshOrders().catch(() => {});
    } else if (event.type === "trades_changed") {
      refreshFills().catch(() => {});
      refreshPnl().catch(() => {});
    }
  };
}

/* ── Boot ─────────────────────────────────────────────────────────── */

async function boot() {
  try {
    const activity = await api("/api/activity");
    $("#activity-log").innerHTML = "";
    activity.forEach(logLine);
    renderOverview(await api("/api/overview"));
    await Promise.all([
      refreshStatus(), refreshEquity(), refreshOrders(), refreshFills(),
      refreshPnl(), refreshCredentials(), loadSettings(),
    ]);
  } catch (err) {
    if (err.message !== "not authenticated") {
      logLine({ level: "error", source: "ui", message: `init failed: ${err.message}`, time: new Date().toISOString() });
    }
    return;
  }
  connectWs();
  setInterval(() => {
    refreshOrders().catch(() => {});
    refreshFills().catch(() => {});
    refreshPnl().catch(() => {});
  }, 30000);
}

(async function init() {
  if (await checkAuth()) await boot();
})();
