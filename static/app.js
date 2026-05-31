/* Token-Monitor dashboard logic. */
(function () {
  "use strict";

  const COLORS = ["#d97757", "#58a6ff", "#56d364", "#bc8cff", "#e3b341", "#79c0ff", "#f85149", "#a371f7"];

  const state = {
    range: "7d",
    provider: "",
    charts: {},
  };

  const PROVIDER_LABELS = {
    "anthropic": "Claude Code",
    "openai": "OpenAI Codex",
  };
  const PROVIDER_COLORS = {
    "anthropic": "#d97757",
    "openai": "#10a37f",
  };

  function providerLabel(p) {
    return PROVIDER_LABELS[p] || p || "—";
  }

  function pquery() {
    return state.provider ? `&provider=${encodeURIComponent(state.provider)}` : "";
  }

  // ---------- helpers ----------
  function $(sel) { return document.querySelector(sel); }
  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    Object.assign(node, props);
    for (const child of children) {
      if (typeof child === "string") node.appendChild(document.createTextNode(child));
      else if (child) node.appendChild(child);
    }
    return node;
  }
  function fmtTokens(n) {
    if (!n) return "0";
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }
  function fmtUsd(n) {
    if (!n && n !== 0) return "—";
    if (n >= 1000) return "$" + n.toFixed(0);
    if (n >= 100) return "$" + n.toFixed(2);
    return "$" + n.toFixed(4);
  }
  function fmtPct(n) { return (n * 100).toFixed(1) + "%"; }
  function fmtDate(s) {
    if (!s) return "—";
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    return d.toLocaleString();
  }
  async function jget(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  // ---------- KPIs ----------
  function renderKpis(s) {
    const providers = s.providers || [];
    const claudeCost = (providers.find(p => p.provider === "anthropic") || {}).cost_usd || 0;
    const codexCost = (providers.find(p => p.provider === "openai") || {}).cost_usd || 0;

    const cards = [
      ["Gesamtkosten",      fmtUsd(s.total_cost_usd), `${s.requests || 0} Requests`],
      ["Claude Code",       fmtUsd(claudeCost), `${(providers.find(p => p.provider === "anthropic") || {}).requests || 0} Requests`],
      ["OpenAI Codex",      fmtUsd(codexCost), `${(providers.find(p => p.provider === "openai") || {}).requests || 0} Requests`],
      ["Gesamt-Tokens",     fmtTokens(s.total_tokens), `${fmtTokens(s.input_tokens)} in / ${fmtTokens(s.output_tokens)} out`],
      ["Cache-Hit-Rate",    fmtPct(s.cache_hit_rate || 0), `${fmtTokens(s.cache_read_tokens)} aus Cache`],
      ["Sessions",          String(s.sessions || 0), `Ø ${fmtUsd(s.avg_cost_per_session || 0)} / Session`],
      ["Top-Modell",        s.top_model || "—", "höchste Kosten"],
      ["Cache-Creation",    fmtTokens(s.cache_creation_tokens), "Cache-Aufbau"],
    ];
    const grid = $("#kpi-grid");
    grid.innerHTML = "";
    for (const [label, value, sub] of cards) {
      const card = el("div", { className: "kpi-card" }, [
        el("div", { className: "label", textContent: label }),
        el("div", { className: "value", textContent: value }),
        el("div", { className: "sub", textContent: sub }),
      ]);
      grid.appendChild(card);
    }
  }

  // ---------- Charts ----------
  function destroyChart(key) {
    if (state.charts[key]) {
      state.charts[key].destroy();
      delete state.charts[key];
    }
  }

  function renderTokensChart(data) {
    destroyChart("tokens");
    const ctx = $("#chart-tokens").getContext("2d");
    state.charts.tokens = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: data.series.map((s, i) => ({
          label: s.name,
          data: s.data,
          backgroundColor: COLORS[i % COLORS.length],
          stack: "tokens",
        })),
      },
      options: chartOpts({ stacked: true }),
    });
  }

  function renderCostChart(data) {
    destroyChart("cost");
    const ctx = $("#chart-cost").getContext("2d");
    state.charts.cost = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [{
          label: "Kosten ($)",
          data: data.series[0].data,
          backgroundColor: COLORS[0],
        }],
      },
      options: chartOpts({ valueFmt: (v) => "$" + v.toFixed(2) }),
    });
  }

  function renderProvidersChart(data) {
    destroyChart("providers");
    const ctx = $("#chart-providers").getContext("2d");
    state.charts.providers = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: data.series.map((s) => ({
          label: providerLabel(s.name),
          data: s.data,
          backgroundColor: PROVIDER_COLORS[s.name] || "#888",
          stack: "providers",
        })),
      },
      options: chartOpts({
        stacked: true,
        valueFmt: (v) => "$" + Number(v).toFixed(2),
      }),
    });
  }

  function renderModelsChart(rows) {
    destroyChart("models");
    const ctx = $("#chart-models").getContext("2d");
    state.charts.models = new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: rows.map(r => r.label),
        datasets: [{
          data: rows.map(r => r.cost_usd),
          backgroundColor: rows.map((_, i) => COLORS[i % COLORS.length]),
          borderColor: "#0d1117",
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "right", labels: { color: "#c9d1d9", boxWidth: 12 } },
          tooltip: {
            callbacks: {
              label: (c) => `${c.label}: $${(c.parsed || 0).toFixed(4)}`,
            },
          },
        },
      },
    });
  }

  function renderToolsChart(rows) {
    destroyChart("tools");
    const top = rows.slice(0, 12);
    const ctx = $("#chart-tools").getContext("2d");
    state.charts.tools = new Chart(ctx, {
      type: "bar",
      data: {
        labels: top.map(r => r.tool_name),
        datasets: [{
          label: "Aufrufe",
          data: top.map(r => r.calls),
          backgroundColor: COLORS[1],
        }],
      },
      options: chartOpts({ indexAxis: "y" }),
    });
  }

  function chartOpts(extra = {}) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: extra.indexAxis || "x",
      plugins: {
        legend: { labels: { color: "#c9d1d9" } },
        tooltip: {
          callbacks: extra.valueFmt ? {
            label: (c) => `${c.dataset.label}: ${extra.valueFmt(c.parsed.y ?? c.parsed)}`,
          } : undefined,
        },
      },
      scales: {
        x: {
          stacked: !!extra.stacked,
          grid: { color: "#30363d" },
          ticks: { color: "#8b949e" },
        },
        y: {
          stacked: !!extra.stacked,
          grid: { color: "#30363d" },
          ticks: { color: "#8b949e" },
        },
      },
    };
  }

  // ---------- Heatmap ----------
  function renderHeatmap(rows) {
    const container = $("#heatmap");
    container.innerHTML = "";
    const days = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"];
    const max = Math.max(1, ...rows.map(r => r.tokens || 0));
    const matrix = {};
    for (const r of rows) {
      if (!matrix[r.dow]) matrix[r.dow] = {};
      matrix[r.dow][r.hour] = r.tokens || 0;
    }
    // header row
    container.appendChild(el("span", { className: "hm-label", textContent: "" }));
    for (let h = 0; h < 24; h++) {
      container.appendChild(el("span", {
        className: "hm-col",
        textContent: h % 3 === 0 ? String(h) : "",
      }));
    }
    for (let d = 0; d < 7; d++) {
      container.appendChild(el("span", { className: "hm-label", textContent: days[d].slice(0, 2) }));
      for (let h = 0; h < 24; h++) {
        const v = (matrix[d] && matrix[d][h]) || 0;
        const intensity = v / max;
        const cell = el("span", {
          className: "hm-cell",
          title: `${days[d]} ${h}:00 — ${fmtTokens(v)} Tokens`,
        });
        if (v > 0) {
          cell.style.background = `rgba(217, 119, 87, ${0.15 + 0.85 * intensity})`;
        }
        container.appendChild(cell);
      }
    }
  }

  // ---------- Recommendations ----------
  function renderRecommendations(items) {
    const container = $("#recommendations");
    container.innerHTML = "";
    if (!items.length) {
      container.appendChild(el("p", { className: "muted", textContent: "Aktuell keine Empfehlungen — Token-Verbrauch wirkt sauber. 🎉" }));
      return;
    }
    for (const r of items) {
      const head = el("div", { className: "rec-head" }, [
        el("span", { className: "rec-title", textContent: r.title }),
        r.est_monthly_savings_usd > 0
          ? el("span", { className: "rec-savings", textContent: `~${fmtUsd(r.est_monthly_savings_usd)} / Monat sparen` })
          : null,
      ]);
      const body = el("div", { className: "rec-body", textContent: r.body });
      container.appendChild(el("div", { className: `rec ${r.severity}` }, [head, body]));
    }
  }

  // ---------- Sessions ----------
  function renderSessions(rows) {
    const tbody = $("#sessions-table tbody");
    tbody.innerHTML = "";
    for (const r of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", { textContent: fmtDate(r.last_seen_at) }));
      const pBadge = el("span", { className: "badge", textContent: providerLabel(r.provider) });
      pBadge.style.background = (PROVIDER_COLORS[r.provider] || "#888") + "33";
      pBadge.style.color = PROVIDER_COLORS[r.provider] || "#aaa";
      tr.appendChild(el("td", {}, [pBadge]));
      tr.appendChild(el("td", { textContent: r.project_path || "—" }));
      tr.appendChild(el("td", { textContent: (r.session_id || "").slice(0, 8) }));
      tr.appendChild(el("td", { className: "num", textContent: String(r.requests || 0) }));
      tr.appendChild(el("td", { className: "num", textContent: fmtTokens(r.tokens) }));
      tr.appendChild(el("td", { className: "num", textContent: fmtUsd(r.cost_usd) }));
      tr.appendChild(el("td", { textContent: r.models || "—" }));
      tr.addEventListener("click", () => openSession(r.session_id));
      tbody.appendChild(tr);
    }
  }

  // ---------- Breakdown ----------
  async function renderBreakdown() {
    const dim = $("#breakdown-dim").value;
    const data = await jget(`/api/breakdown?range=${state.range}&dim=${dim}&limit=30${pquery()}`);
    const tbody = $("#breakdown-table tbody");
    tbody.innerHTML = "";
    for (const r of data.rows) {
      const tr = el("tr");
      const label = dim === "provider" ? providerLabel(r.label) : r.label;
      tr.appendChild(el("td", { textContent: label }));
      tr.appendChild(el("td", { className: "num", textContent: String(r.requests) }));
      tr.appendChild(el("td", { className: "num", textContent: fmtTokens(r.tokens) }));
      tr.appendChild(el("td", { className: "num", textContent: fmtUsd(r.cost_usd) }));
      tbody.appendChild(tr);
    }
  }

  // ---------- Session modal ----------
  async function openSession(id) {
    if (!id) return;
    const modal = $("#session-modal");
    const body = $("#modal-body");
    body.innerHTML = "<p class='muted'>Lade…</p>";
    modal.classList.remove("hidden");
    try {
      const data = await jget(`/api/session/${encodeURIComponent(id)}`);
      const m = data.meta || {};
      const tokens = (m.input_tokens || 0) + (m.output_tokens || 0) + (m.cache_read_tokens || 0) + (m.cache_creation_tokens || 0);
      const summary = el("div", {}, [
        el("h2", { textContent: "Session " + (id || "").slice(0, 16) }),
        el("p", { className: "muted",
          textContent: `${m.project_path || "(kein Pfad)"} · ${fmtDate(m.started_at)} → ${fmtDate(m.last_seen_at)}` }),
        el("p", { textContent: `${m.requests || 0} Requests · ${fmtTokens(tokens)} Tokens · ${fmtUsd(m.cost_usd || 0)}` }),
      ]);

      const toolsTable = el("table");
      toolsTable.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { textContent: "Tool" }), el("th", { className: "num", textContent: "Aufrufe" }),
      ])]));
      const ttbody = el("tbody");
      for (const t of (data.tools || [])) {
        ttbody.appendChild(el("tr", {}, [
          el("td", { textContent: t.tool_name }),
          el("td", { className: "num", textContent: String(t.calls) }),
        ]));
      }
      toolsTable.appendChild(ttbody);

      const reqTable = el("table");
      reqTable.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { textContent: "Zeit" }), el("th", { textContent: "Modell" }),
        el("th", { className: "num", textContent: "In" }), el("th", { className: "num", textContent: "Out" }),
        el("th", { className: "num", textContent: "Cache R" }), el("th", { className: "num", textContent: "Cache C" }),
        el("th", { className: "num", textContent: "Cost" }), el("th", { textContent: "Quelle" }),
      ])]));
      const rtbody = el("tbody");
      for (const r of (data.requests || [])) {
        rtbody.appendChild(el("tr", {}, [
          el("td", { textContent: fmtDate(r.timestamp) }),
          el("td", { textContent: r.model }),
          el("td", { className: "num", textContent: fmtTokens(r.input_tokens) }),
          el("td", { className: "num", textContent: fmtTokens(r.output_tokens) }),
          el("td", { className: "num", textContent: fmtTokens(r.cache_read_tokens) }),
          el("td", { className: "num", textContent: fmtTokens(r.cache_creation_tokens) }),
          el("td", { className: "num", textContent: fmtUsd(r.cost_usd) }),
          el("td", { textContent: r.source }),
        ]));
      }
      reqTable.appendChild(rtbody);

      body.innerHTML = "";
      body.appendChild(summary);
      body.appendChild(el("h3", { textContent: "Tools" }));
      body.appendChild(toolsTable);
      body.appendChild(el("h3", { textContent: "Requests" }));
      body.appendChild(reqTable);
    } catch (err) {
      body.innerHTML = "";
      body.appendChild(el("p", { textContent: "Fehler: " + err.message }));
    }
  }

  // ---------- Health ----------
  async function refreshHealth() {
    try {
      const h = await jget("/api/health");
      $("#status-led").classList.remove("err", "warn");
      const lines = [
        `DB: ${(h.db_size_bytes / 1024).toFixed(1)} KB`,
        `Requests: ${h.api_request_count}`,
        `Letztes Event: ${h.latest_event_ts ? fmtDate(h.latest_event_ts) : "—"}`,
        `Letzter JSONL-Scan: ${h.last_jsonl_scan_at ? fmtDate(h.last_jsonl_scan_at) : "—"}`,
      ];
      $("#health-info").textContent = lines.join(" · ");
    } catch (err) {
      $("#status-led").classList.add("err");
      $("#health-info").textContent = "Health check fehlgeschlagen: " + err.message;
    }
  }

  // ---------- Refresh ----------
  async function refreshAll() {
    state.range = $("#range").value;
    state.provider = $("#provider").value || "";
    const p = pquery();
    try {
      const [summary, timeseriesTokens, timeseriesCost, providersTS, models, tools, recs, sessions, heatmap] = await Promise.all([
        jget(`/api/summary?range=${state.range}${p}`),
        jget(`/api/timeseries?range=${state.range}&metric=tokens&bucket=day${p}`),
        jget(`/api/timeseries?range=${state.range}&metric=cost&bucket=day${p}`),
        jget(`/api/timeseries?range=${state.range}&metric=cost&bucket=day&groupBy=provider${p}`),
        jget(`/api/breakdown?range=${state.range}&dim=model&limit=10${p}`),
        jget(`/api/tools?range=${state.range}&limit=20${p}`),
        jget(`/api/recommendations?range=${state.range}${p}`),
        jget(`/api/sessions?range=${state.range}&limit=100${p}`),
        jget(`/api/heatmap?range=${state.range === "24h" ? "7d" : state.range}${p}`),
      ]);
      renderKpis(summary);
      renderTokensChart(timeseriesTokens);
      renderCostChart(timeseriesCost);
      renderProvidersChart(providersTS);
      renderModelsChart(models.rows);
      renderToolsChart(tools.rows);
      renderRecommendations(recs.items);
      renderSessions(sessions.rows);
      renderHeatmap(heatmap.rows);
      await renderBreakdown();
      await refreshHealth();
    } catch (err) {
      console.error(err);
      $("#status-led").classList.add("err");
    }
  }

  // ---------- Boot ----------
  document.addEventListener("DOMContentLoaded", () => {
    $("#range").addEventListener("change", refreshAll);
    $("#provider").addEventListener("change", refreshAll);
    $("#refresh").addEventListener("click", refreshAll);
    $("#breakdown-dim").addEventListener("change", renderBreakdown);
    $("#modal-close").addEventListener("click", () => $("#session-modal").classList.add("hidden"));
    $("#session-modal").addEventListener("click", (e) => {
      if (e.target === $("#session-modal")) $("#session-modal").classList.add("hidden");
    });
    refreshAll();
    setInterval(refreshAll, 30_000);
  });
})();
