// views/overview.js — Overview screen (#/): filter bar, stat tiles, daily
// stacked token bars + output line, est-$ trend, model breakdown, sessions table.

import { api } from "../api.js";
import {
  el, legend, statTile, stackedBarChart, hBarChart,
  fmtInt, fmtCompact, fmtUsd, fmtDateTime, fmtDate, shortId,
  emptyState, errorState, loadingState,
} from "../charts.js";

const RANGES = [
  ["24h", 1], ["7d", 7], ["30d", 30], ["all", null],
];

const SERIES = [
  { key: "cache_read_tokens", label: "Cache read", color: "var(--s-cache-read)" },
  { key: "cache_creation_tokens", label: "Cache write", color: "var(--s-cache-write)" },
  { key: "input_tokens", label: "Fresh input", color: "var(--s-fresh)" },
];
const OUTPUT_LINE = { label: "Output", color: "var(--s-output)" };

// Tier-1 quality signals (dv-37e9): stacked "unhappiness events" per day,
// log_* call volume overlaid as the activity line.
const QUALITY_SERIES = [
  { key: "pinned_facts", label: "New pinned facts", color: "var(--s-cache-write)" },
  { key: "complaint_hits", label: "Complaint msgs", color: "#d43d51" },
  { key: "claimed_not_logged", label: "Claimed, not logged", color: "var(--s-output)" },
];
const QUALITY_LINE = { label: "log_* calls", color: "var(--s-fresh)" };

const TABLE_COLUMNS = [
  { key: "session_id", label: "Session", sortable: false },
  { key: "profile", label: "Profile", sortable: false },
  { key: "entrypoint", label: "Entry", sortable: false },
  { key: "last_ts", label: "Last activity", sortable: true },
  { key: "api_call_count", label: "Calls", sortable: true, num: true },
  { key: "output_tokens", label: "Output tok", sortable: true, num: true },
  { key: "cache_read_tokens", label: "Cache read", sortable: true, num: true },
  { key: "est_cost_usd", label: "Est $", sortable: true, num: true },
  { key: "tool_call_count", label: "Tools", sortable: true, num: true },
  { key: "subagent_count", label: "Agents", sortable: true, num: true },
  { key: "turn_count", label: "Turns", sortable: true, num: true },
];

function rangeParams(range) {
  const days = RANGES.find(([name]) => name === range)?.[1];
  if (!days) return {};
  return { from: new Date(Date.now() - days * 86_400_000).toISOString() };
}

export async function renderOverview(view, state) {
  const filterBar = el("div", { class: "filter-bar" });
  const content = el("div");
  view.append(filterBar, content);

  let filterOptions = null;
  let missingMap = null; // session_id -> true when bot session's transcript vanished

  async function loadFilterBar() {
    try {
      // Options are scoped to the selected range, so the dropdowns never
      // offer a user/profile that would show an empty view.
      filterOptions = await api.filters(rangeParams(state.filters.range));
    } catch (e) {
      filterOptions = { users: [], profiles: [] };
    }
    // A previously selected value may fall outside the new range — keep it
    // selectable rather than silently dropping the active filter.
    if (state.filters.user_id !== "" && !(filterOptions.users || []).some((u) => String(u) === state.filters.user_id)) {
      filterOptions.users = [...(filterOptions.users || []), state.filters.user_id];
    }
    if (state.filters.profile !== "" && !(filterOptions.profiles || []).includes(state.filters.profile)) {
      filterOptions.profiles = [...(filterOptions.profiles || []), state.filters.profile];
    }
    buildFilterBar();
  }

  function buildFilterBar() {
    filterBar.textContent = "";
    // date range first — the filter every reader reaches for
    const presets = el("div", { class: "preset-group", role: "group", "aria-label": "Date range" });
    for (const [name] of RANGES) {
      const btn = el("button", {
        type: "button", text: name,
        class: state.filters.range === name ? "active" : "",
        onclick: () => { state.filters.range = name; loadFilterBar(); loadContent(); },
      });
      presets.append(btn);
    }
    filterBar.append(presets);

    const userSel = el("select", {
      "aria-label": "User",
      onchange: () => { state.filters.user_id = userSel.value; loadContent(); },
    });
    userSel.append(el("option", { value: "", text: "All users" }));
    for (const u of filterOptions.users || []) {
      const label = u === "local" ? "Local (this machine)" : "user " + u;
      userSel.append(el("option", { value: u, text: label }));
    }
    userSel.value = state.filters.user_id;
    filterBar.append(userSel);

    const profSel = el("select", {
      "aria-label": "Profile",
      onchange: () => { state.filters.profile = profSel.value; loadContent(); },
    });
    profSel.append(el("option", { value: "", text: "All profiles" }));
    for (const p of filterOptions.profiles || []) {
      profSel.append(el("option", { value: p, text: p }));
    }
    profSel.value = state.filters.profile;
    filterBar.append(profSel);
  }

  function filterParams() {
    const p = { ...rangeParams(state.filters.range) };
    if (state.filters.user_id !== "") p.user_id = state.filters.user_id;
    if (state.filters.profile !== "") p.profile = state.filters.profile;
    return p;
  }

  async function loadContent() {
    content.textContent = "";
    content.append(loadingState());
    const params = filterParams();
    let overview, sessionsPage, quality;
    try {
      [overview, sessionsPage, quality] = await Promise.all([
        api.overview(params),
        api.sessions({ ...params, ...state.sessionsSort, limit: 50 }),
        api.quality(params).catch(() => null), // best-effort: card is skipped
      ]);
    } catch (e) {
      content.textContent = "";
      content.append(errorState("Failed to load overview: " + e.message));
      return;
    }
    // "no transcript" badge data: the sessions list has no per-row transcript
    // presence, so we derive it from reconciliation (bot rows with a vanished
    // transcript file). Best-effort — the badge is skipped if this fetch fails.
    if (missingMap === null) {
      try {
        const rec = await api.reconciliation();
        missingMap = {};
        for (const row of rec.rows || []) {
          if (row.disk && row.disk.present === false) missingMap[row.session_id] = true;
        }
      } catch (e) {
        missingMap = {};
      }
    }
    content.textContent = "";
    renderTiles(content, overview);
    renderDaily(content, overview);
    if (quality) renderQuality(content, quality);
    renderModels(content, overview);
    renderSessions(content, sessionsPage);
  }

  function renderTiles(parent, overview) {
    const t = overview.totals || {};
    const daily = overview.daily || [];
    const tiles = el("div", { class: "tiles" });
    tiles.append(statTile({
      label: "Est. cost", hero: true,
      value: fmtUsd(t.est_cost_usd || 0),
      trend: daily.length > 1
        ? { values: daily.map((d) => d.est_cost_usd || 0), color: "var(--deemph)" }
        : null,
      sub: "est-$ · daily trend",
    }));
    tiles.append(statTile({ label: "Sessions", value: fmtCompact(t.sessions || 0) }));
    tiles.append(statTile({ label: "API calls", value: fmtCompact(t.api_calls || 0) }));
    tiles.append(statTile({
      label: "Output tokens", value: fmtCompact(t.output_tokens || 0),
      sub: fmtCompact(t.input_tokens || 0) + " fresh input",
    }));
    tiles.append(statTile({
      label: "Cache read", value: fmtCompact(t.cache_read_tokens || 0),
      sub: fmtCompact(t.cache_creation_tokens || 0) + " written",
    }));
    tiles.append(statTile({ label: "Tool calls", value: fmtCompact(t.tool_calls || 0) }));
    parent.append(tiles);
  }

  function renderDaily(parent, overview) {
    const card = el("div", { class: "card" });
    card.append(el("h2", { text: "Daily tokens" }));
    card.append(el("div", { class: "card-sub", text: "stacked input composition per day; output overlaid (same axis, tokens)" }));
    const daily = overview.daily || [];
    if (!daily.length) {
      card.append(emptyState("No API calls in this range."));
      parent.append(card);
      return;
    }
    card.append(legend([
      ...SERIES.map((s) => ({ label: s.label, color: s.color, kind: "rect" })),
      { label: OUTPUT_LINE.label, color: OUTPUT_LINE.color, kind: "line" },
    ]));
    const box = el("div", { class: "chart-box" });
    card.append(box);
    parent.append(card);
    stackedBarChart(box, {
      height: 240,
      data: daily.map((d) => ({
        label: fmtDate(d.date),
        parts: SERIES.map((s) => d[s.key] || 0),
      })),
      series: SERIES,
      line: { ...OUTPUT_LINE, values: daily.map((d) => d.output_tokens || 0) },
      extraTip: (i) => [
        { label: "est-$", value: fmtUsd(daily[i].est_cost_usd || 0) },
        { label: "api calls", value: fmtInt(daily[i].api_calls || 0) },
      ],
    });
  }

  function renderQuality(parent, quality) {
    const card = el("div", { class: "card" });
    card.append(el("h2", { text: "Bot quality (Tier-1)" }));
    card.append(el("div", {
      class: "card-sub",
      text: "mechanical signals from bot.db: stacked unhappiness events per day; log_* call volume overlaid (same axis, counts)",
    }));
    const daily = quality.daily || [];
    if (!daily.length) {
      card.append(emptyState("No bot turns in this range."));
      parent.append(card);
      return;
    }
    card.append(legend([
      ...QUALITY_SERIES.map((s) => ({ label: s.label, color: s.color, kind: "rect" })),
      { label: QUALITY_LINE.label, color: QUALITY_LINE.color, kind: "line" },
    ]));
    const box = el("div", { class: "chart-box" });
    card.append(box);
    stackedBarChart(box, {
      height: 200,
      data: daily.map((d) => ({
        label: fmtDate(d.date),
        parts: QUALITY_SERIES.map((s) => d[s.key] || 0),
      })),
      series: QUALITY_SERIES,
      line: { ...QUALITY_LINE, values: daily.map((d) => d.log_calls || 0) },
      extraTip: (i) => [
        { label: "log_* calls", value: fmtInt(daily[i].log_calls || 0) },
        { label: "user msgs", value: fmtInt(daily[i].user_messages || 0) },
      ],
    });
    // Per agent/user breakdown: unhappiness events in range.
    const byAgent = (quality.by_agent || []).map((a) => ({
      ...a,
      events: (a.pinned_facts || 0) + (a.complaint_hits || 0) + (a.claimed_not_logged || 0),
      name: (a.profile || "?") + (a.user_id != null ? " · user " + a.user_id : ""),
    })).filter((a) => a.events > 0);
    if (byAgent.length) {
      card.append(el("div", { class: "card-sub", text: "unhappiness events by agent · user in range" }));
      const abox = el("div", { class: "chart-box" });
      card.append(abox);
      hBarChart(abox, {
        data: byAgent.map((a) => ({ label: a.name, value: a.events })),
        color: "#d43d51",
        format: fmtInt,
        tip: (i) => {
          const a = byAgent[i];
          return [
            { header: a.name },
            { label: "new pinned facts", value: fmtInt(a.pinned_facts || 0) },
            { label: "complaint msgs", value: fmtInt(a.complaint_hits || 0) },
            { label: "claimed, not logged", value: fmtInt(a.claimed_not_logged || 0) },
            { label: "log_* calls", value: fmtInt(a.log_calls || 0) },
            { label: "user msgs", value: fmtInt(a.user_messages || 0) },
          ];
        },
      });
    }
    parent.append(card);
  }

  function renderModels(parent, overview) {
    const card = el("div", { class: "card" });
    card.append(el("h2", { text: "By model" }));
    card.append(el("div", { class: "card-sub", text: "est-$ per model in range" }));
    const byModel = overview.by_model || [];
    if (!byModel.length) {
      card.append(emptyState("No model data in this range."));
      parent.append(card);
      return;
    }
    const box = el("div", { class: "chart-box" });
    card.append(box);
    parent.append(card);
    hBarChart(box, {
      data: byModel.map((m) => ({ label: m.model, value: m.est_cost_usd || 0 })),
      color: "var(--accent)",
      format: fmtUsd,
      tip: (i) => {
        const m = byModel[i];
        return [
          { header: m.model },
          { label: "est-$", value: fmtUsd(m.est_cost_usd || 0) },
          { label: "api calls", value: fmtInt(m.api_calls || 0) },
          { label: "output tokens", value: fmtCompact(m.output_tokens || 0) },
          { label: "cache read", value: fmtCompact(m.cache_read_tokens || 0) },
          { label: "cache write", value: fmtCompact(m.cache_creation_tokens || 0) },
          { label: "fresh input", value: fmtCompact(m.input_tokens || 0) },
        ];
      },
    });
  }

  function renderSessions(parent, page) {
    const card = el("div", { class: "card" });
    const head = el("div", { class: "card-head" });
    head.append(el("h2", { text: "Sessions" }));
    head.append(el("div", { class: "card-sub", text: `${fmtInt(page.total)} in range · click a row to open` }));
    card.append(head);
    if (!page.sessions.length) {
      card.append(emptyState("No sessions match the current filters."));
      parent.append(card);
      return;
    }
    const wrap = el("div", { class: "table-wrap" });
    const table = el("table", { class: "data" });
    const headRow = el("tr");
    for (const col of TABLE_COLUMNS) {
      const th = el("th", {
        class: (col.sortable ? "sortable" : "") + (col.num ? " num" : ""),
      });
      th.append(document.createTextNode(col.label));
      if (col.sortable) {
        if (state.sessionsSort.sort === col.key) {
          th.append(el("span", { class: "sort-arrow", text: state.sessionsSort.order === "desc" ? " ↓" : " ↑" }));
        }
        th.addEventListener("click", () => {
          if (state.sessionsSort.sort === col.key) {
            state.sessionsSort.order = state.sessionsSort.order === "desc" ? "asc" : "desc";
          } else {
            state.sessionsSort = { sort: col.key, order: "desc" };
          }
          loadContent();
        });
      }
      headRow.append(th);
    }
    table.append(el("thead", {}, headRow));
    const tbody = el("tbody");
    for (const s of page.sessions) {
      const tr = el("tr", { class: "clickable", onclick: () => { location.hash = "#/session/" + s.session_id; } });
      const idCell = el("td", {}, el("span", { text: shortId(s.session_id) }));
      if (missingMap && missingMap[s.session_id]) {
        idCell.append(" ", el("span", { class: "badge warn", text: "no transcript" }));
      }
      tr.append(idCell);
      tr.append(el("td", { text: s.profile || "–" }));
      tr.append(el("td", {}, el("span", { class: "badge ep-" + (s.entrypoint || "unknown"), text: s.entrypoint || "?" })));
      tr.append(el("td", { text: fmtDateTime(s.last_ts) }));
      tr.append(el("td", { class: "num", text: fmtInt(s.api_call_count) }));
      tr.append(el("td", { class: "num", text: fmtCompact(s.output_tokens) }));
      tr.append(el("td", { class: "num", text: fmtCompact(s.cache_read_tokens) }));
      tr.append(el("td", { class: "num", text: fmtUsd(s.est_cost_usd) }));
      tr.append(el("td", { class: "num", text: fmtInt(s.tool_call_count) }));
      tr.append(el("td", { class: "num", text: fmtInt(s.subagent_count) }));
      tr.append(el("td", { class: "num", text: fmtInt(s.turn_count) }));
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    card.append(wrap);
    if (page.total > page.sessions.length) {
      card.append(el("div", { class: "table-foot", text: `Showing ${page.sessions.length} of ${fmtInt(page.total)} (sorted server-side)` }));
    }
    parent.append(card);
  }

  await loadFilterBar();
  await loadContent();

  return {
    refresh: async () => { missingMap = null; await loadContent(); },
  };
}
