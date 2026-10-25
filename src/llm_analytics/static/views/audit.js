// views/audit.js — Harness Audit screen (#/audit): reconciliation of bot.db
// vs disk transcripts, and the chain explorer replaying the bot's window
// counter against real per-call context.

import { api } from "../api.js";
import {
  el, svg, legend, statTile, attachTip, showTip, hideTip,
  linScale, niceTicks, yAxis, stepPath,
  fmtInt, fmtCompact, fmtUsd, fmtDateTime, shortId,
  emptyState, errorState, loadingState,
} from "../charts.js";

const CONTEXT_LINE = { label: "context provided (real) — what rotation reads", color: "var(--s-cache-read)" };
const SESSION_LINE = { label: "new session", color: "var(--ink-2)" };

export async function renderAudit(view, state) {
  const av = state.auditView;
  const root = el("div");
  view.append(root);

  const reconSection = el("div");
  const chainSection = el("div");
  // chain explorer first — it is the reason to open this screen; reconciliation
  // is the reference table you scroll down to.
  root.append(chainSection, reconSection);

  // ------------------------------------------------------------------
  // Reconciliation
  // ------------------------------------------------------------------
  async function loadReconciliation() {
    reconSection.textContent = "";
    reconSection.append(loadingState());
    let rec;
    try {
      rec = await api.reconciliation();
    } catch (e) {
      reconSection.textContent = "";
      reconSection.append(errorState("Failed to load reconciliation: " + e.message));
      return;
    }
    reconSection.textContent = "";
    const sm = rec.summary || {};
    const tiles = el("div", { class: "tiles" });
    tiles.append(statTile({ label: "Bot sessions", value: fmtInt(sm.bot_sessions) }));
    tiles.append(statTile({ label: "With transcript", value: fmtInt(sm.with_transcript) }));
    tiles.append(statTile({ label: "Missing transcript", value: fmtInt(sm.missing_transcript) }));
    tiles.append(statTile({ label: "Orphan transcripts", value: fmtInt(sm.orphan_transcripts) }));
    tiles.append(statTile({ label: "Flagged", value: fmtInt(sm.flagged) }));
    tiles.append(statTile({
      label: "Bot cost vs est-$",
      value: fmtUsd(sm.bot_cost_total) + " / " + fmtUsd(sm.disk_est_cost_total),
      sub: "Δ " + fmtUsd((sm.disk_est_cost_total || 0) - (sm.bot_cost_total || 0)),
    }));
    reconSection.append(tiles);

    const card = el("div", { class: "card" });
    card.append(el("h2", { text: "Reconciliation — bot.db vs disk transcripts" }));
    card.append(el("div", { class: "card-sub", text: "one row per bot session · retention-expired rows muted · click a row with a transcript to open the session" }));
    const rows = rec.rows || [];
    if (!rows.length) {
      card.append(emptyState("No bot sessions mirrored yet."));
    } else {
      const wrap = el("div", { class: "table-wrap" });
      const table = el("table", { class: "data" });
      const thead = el("thead", {}, el("tr", {},
        el("th", { text: "Session" }),
        el("th", { text: "User" }),
        el("th", { text: "Profile" }),
        el("th", { text: "Created" }),
        el("th", { class: "num", text: "Bot msgs" }),
        el("th", { class: "num", text: "Disk calls" }),
        el("th", { class: "num", text: "Disk turns" }),
        el("th", { class: "num", text: "Bot $" }),
        el("th", { class: "num", text: "Est $" }),
        el("th", { class: "num", text: "Δ $" }),
        el("th", { text: "Flags" })));
      table.append(thead);
      const tbody = el("tbody");
      for (const row of rows) {
        const retention = row.flags.includes("missing_transcript_retention");
        const canOpen = row.disk && row.disk.present;
        const tr = el("tr", { class: (retention ? "muted " : "") + (canOpen ? "clickable" : "") });
        if (canOpen) tr.addEventListener("click", () => { location.hash = "#/session/" + row.session_id; });
        tr.append(el("td", { text: shortId(row.session_id) }));
        tr.append(el("td", { text: String(row.user_id) }));
        tr.append(el("td", { text: row.profile || "–" }));
        tr.append(el("td", { text: fmtDateTime(row.bot.created_at) }));
        tr.append(el("td", { class: "num", text: fmtInt(row.bot.message_count) }));
        tr.append(el("td", { class: "num", text: row.disk.api_calls != null ? fmtInt(row.disk.api_calls) : "–" }));
        tr.append(el("td", { class: "num", text: fmtInt(row.disk.user_turns) }));
        tr.append(el("td", { class: "num", text: fmtUsd(row.bot.total_cost) }));
        tr.append(el("td", { class: "num", text: row.disk.est_cost != null ? fmtUsd(row.disk.est_cost) : "–" }));
        const delta = row.disk.est_cost != null && row.bot.total_cost != null
          ? row.disk.est_cost - row.bot.total_cost : null;
        tr.append(el("td", { class: "num", text: delta != null ? fmtUsd(delta) : "–" }));
        const flagCell = el("td");
        for (const flag of row.flags) {
          flagCell.append(el("span", {
            class: "chip" + (flag === "missing_transcript_retention" ? " chip-muted" : ""),
            text: flag,
          }));
        }
        tr.append(flagCell);
        tbody.append(tr);
      }
      table.append(tbody);
      wrap.append(table);
      card.append(wrap);
    }
    if ((rec.orphans || []).length) {
      const foot = el("div", { class: "table-foot" });
      foot.append(el("span", { text: `${rec.orphans.length} orphan sdk-py transcripts (no bot.db row):` }));
      for (const o of rec.orphans.slice(0, 8)) {
        foot.append(el("a", { href: "#/session/" + o.session_id, text: shortId(o.session_id) }));
      }
      if (rec.orphans.length > 8) foot.append(el("span", { text: `+${rec.orphans.length - 8} more` }));
      card.append(foot);
    }
    reconSection.append(card);
  }

  // ------------------------------------------------------------------
  // Chain explorer
  // ------------------------------------------------------------------
  let chainsData = null;

  // Date range of the chain view (default: last 7 days). Sent as since/until
  // to /api/chains and /api/chains/detail, which filter server-side.
  const PRESETS = [["24h", 1], ["7d", 7], ["30d", 30], ["all", 0]];
  const isoDay = (d) => d.toISOString().slice(0, 10);
  function chainRange() {
    if (av.preset === "custom") return { since: av.from, until: av.to };
    const days = (PRESETS.find((p) => p[0] === av.preset) || ["", 0])[1];
    if (!days) return {};
    // 24h = since this moment yesterday; days are whole UTC days otherwise
    if (days === 1) return { since: new Date(Date.now() - 864e5).toISOString() };
    return { since: isoDay(new Date(Date.now() - (days - 1) * 864e5)) };
  }

  async function loadChains() {
    chainSection.textContent = "";
    chainSection.append(loadingState());
    try {
      chainsData = (await api.chains()).chains || [];
    } catch (e) {
      chainSection.textContent = "";
      chainSection.append(errorState("Failed to load chains: " + e.message));
      return;
    }
    chainSection.textContent = "";
    const card = el("div", { class: "card" });
    card.append(el("h2", { text: "Chain explorer — window rotation audit" }));
    card.append(el("div", { class: "card-sub", text: "real per-call context vs the bot's replayed window counter, per (user, working_dir) chain · click the chart or a session row to open that session" }));

    if (!chainsData.length) {
      card.append(emptyState("No chains — bot.db mirror is empty."));
      chainSection.append(card);
      return;
    }

    // default selection: first chain that has indexed sessions
    if (av.user_id === null || !chainsData.some((c) => c.user_id === av.user_id && c.working_dir === av.working_dir)) {
      const preferred = chainsData.find((c) => c.sessions.some((s) => s.in_index)) || chainsData[0];
      av.user_id = preferred.user_id;
      av.working_dir = preferred.working_dir;
    }

    const selects = el("div", { class: "chain-select" });
    const users = [...new Set(chainsData.map((c) => c.user_id))];
    const userSel = el("select", { "aria-label": "User" });
    for (const u of users) userSel.append(el("option", { value: u, text: "user " + u }));
    userSel.value = String(av.user_id);
    const dirSel = el("select", { "aria-label": "Working dir" });
    const fillDirs = () => {
      dirSel.textContent = "";
      const dirs = chainsData.filter((c) => c.user_id === Number(userSel.value));
      for (const c of dirs) {
        dirSel.append(el("option", { value: c.working_dir, text: `${c.working_dir} (${c.sessions.length} sessions)` }));
      }
      if (dirs.some((c) => c.working_dir === av.working_dir)) dirSel.value = av.working_dir;
    };
    fillDirs();
    userSel.addEventListener("change", () => {
      av.user_id = Number(userSel.value);
      fillDirs();
      av.working_dir = dirSel.value;
      loadChainDetail(card);
    });
    dirSel.addEventListener("change", () => {
      av.working_dir = dirSel.value;
      loadChainDetail(card);
    });
    selects.append(el("label", { text: "Chain:" }), userSel, dirSel);
    card.append(selects);
    card.append(rangeBar(card));
    card.append(el("div", { class: "chain-body" }));
    chainSection.append(card);
    await loadChainDetail(card);
  }

  function rangeBar(card) {
    const bar = el("div", { class: "chain-select" });
    bar.append(el("label", { text: "Period:" }));
    const fromIn = el("input", { type: "date", "aria-label": "From" });
    const toIn = el("input", { type: "date", "aria-label": "To" });
    const buttons = [];
    const sync = () => {
      for (const [b, name] of buttons) b.classList.toggle("active", av.preset === name);
      const r = chainRange();
      fromIn.value = av.preset === "custom" ? av.from : (r.since || "").slice(0, 10);
      toIn.value = av.preset === "custom" ? av.to : "";
    };
    for (const [name] of PRESETS) {
      const b = el("button", { class: "btn", type: "button", text: name });
      b.addEventListener("click", () => { av.preset = name; sync(); loadChainDetail(card); });
      buttons.push([b, name]);
      bar.append(b);
    }
    const custom = () => {
      av.preset = "custom"; av.from = fromIn.value; av.to = toIn.value;
      sync(); loadChainDetail(card);
    };
    fromIn.addEventListener("change", custom);
    toIn.addEventListener("change", custom);
    bar.append(el("label", { text: "from" }), fromIn, el("label", { text: "to" }), toIn);
    const reload = el("button", { class: "btn", type: "button", text: "↻ refresh", title: "Reload chain data (no auto-refresh here)" });
    reload.addEventListener("click", () => loadChainDetail(card));
    bar.append(reload);
    sync();
    return bar;
  }

  async function loadChainDetail(card) {
    const body = card.querySelector(".chain-body");
    body.textContent = "";
    body.append(loadingState());
    const range = chainRange();
    let chain;
    let detail;
    try {
      const ranged = (await api.chains(range)).chains || [];
      chain = ranged.find((c) => c.user_id === av.user_id && c.working_dir === av.working_dir);
      detail = await api.chainDetail({ user_id: av.user_id, working_dir: av.working_dir, ...range });
    } catch (e) {
      body.textContent = "";
      body.append(errorState("Failed to load chain detail: " + e.message));
      return;
    }
    body.textContent = "";

    const series = detail.series || [];
    const ann = detail.annotations || {};
    const rotations = chain ? chain.rotations_observed : 0;

    // current window state + per-window chips
    if (chain && chain.current_window) {
      const cw = chain.current_window;
      const chips = el("div", { class: "window-chips" });
      chips.append(el("span", {}, "current window ", el("b", { text: "#" + cw.window_id })));
      chips.append(el("span", {}, "counter ", el("b", { text: fmtCompact(cw.cumulative_tokens) })));
      chips.append(el("span", {}, "backend ", el("b", { text: cw.active_backend || "–" })));
      chips.append(el("span", {}, "updated ", el("b", { text: fmtDateTime(cw.updated_at) })));
      for (const w of chain.windows || []) {
        chips.append(el("span", { class: "badge", text: `w${w.window_id}: ${fmtInt(w.message_count)} msgs · ${fmtUsd(w.total_cost)}` }));
      }
      body.append(chips);
    }

    const hasSessions = chain && (chain.sessions || []).length > 0;

    if (!series.length) {
      body.append(emptyState("No indexed API calls for this chain in the selected period (or transcripts missing / never indexed)."));
      // still list the sessions — this is exactly when you want to inspect them
      if (hasSessions) body.append(chainSessionTable(chain));
      return;
    }

    // rotation banner. Rotation reads the real context level (dv-9f31), so the
    // threshold is quoted against the context peak. The legacy flow counter is
    // no longer drawn: it confused readers and nothing decides on it (25.10).
    const contextPeak = Math.max(...series.map((p) => p.context_provided));
    const threshold = ann.refresh_threshold || 220000;
    const counterNote = `context peak ${fmtCompact(contextPeak)} of ${fmtCompact(threshold)}`;
    if (rotations === 0) {
      body.append(el("div", { class: "banner" },
        el("span", { class: "banner-icon", text: "!" }),
        el("span", { text: "No rotation observed — " + counterNote })));
    } else {
      body.append(el("div", { class: "banner info" },
        el("span", { class: "banner-icon", text: "i" }),
        el("span", { text: `${rotations} rotation${rotations === 1 ? "" : "s"} observed · ${counterNote}` })));
    }

    body.append(legend([
      { label: CONTEXT_LINE.label, color: CONTEXT_LINE.color, kind: "line" },
      { label: SESSION_LINE.label, color: SESSION_LINE.color, kind: "line" },
      { label: "cache expiry", color: "var(--st-serious)", kind: "marker" },
      { label: "rotation", color: "var(--st-crit)", kind: "line" },
    ]));

    const box = el("div", { class: "chart-box" });
    body.append(box);
    drawChainChart(box, series, ann, chain);

    body.append(growthSection(series));

    if (hasSessions) body.append(chainSessionTable(chain));
  }

  // "What grew the context" (dv-aeca): pick a window, see where its tokens
  // went by category and by tool, plus the single largest items and every
  // call's step. Measured per-step growth; only the split inside a step is
  // estimated (by size), see src/llm_analytics/growth.py.
  const CATEGORY_LABEL = {
    base_system: "system prompt (persona, CLI)", base_tools: "tool schemas",
    base_reminder: "CLAUDE.md / skills / env reminders", base_unknown: "system+tools (not logged)",
    seed: "window seed", turn_prefix: "per-turn prefix (time, family)",
    user_text: "user text", reminder: "harness reminders", tool_result: "tool results",
    subagent_result: "sub-agent reports", tool_use: "tool call inputs",
    assistant_text: "assistant text", thinking: "thinking (hidden)",
    unattributed: "unattributed", context_shrink: "context shrank",
  };

  function growthSection(series) {
    const wrap = el("div", { class: "chain-sessions" });
    wrap.append(el("h3", { text: "What grew the context" }));
    const windows = [...new Set(series.map((p) => p.window_id).filter((w) => w != null))].sort((a, b) => a - b);
    if (!windows.length) {
      wrap.append(emptyState("No window ids for this chain."));
      return wrap;
    }
    const sel = el("select", { "aria-label": "Window" });
    for (const w of windows) sel.append(el("option", { value: w, text: "window " + w }));
    // keep the picked window across reloads; fall back to the newest
    sel.value = String(windows.includes(av.window_id) ? av.window_id : windows[windows.length - 1]);
    const out = el("div");
    wrap.append(el("div", { class: "card-sub", text: "per window: tokens by category and tool (measured step growth, split by size inside a step)" }), sel, out);
    const load = async () => {
      out.textContent = "";
      out.append(loadingState());
      let data;
      try {
        data = await api.chainWindow({ user_id: av.user_id, working_dir: av.working_dir, window_id: sel.value });
      } catch (e) {
        out.textContent = "";
        out.append(errorState("Failed to load window breakdown: " + e.message));
        return;
      }
      out.textContent = "";
      if (!data.indexed_sizes) {
        out.append(el("div", { class: "banner" }, el("span", { class: "banner-icon", text: "!" }),
          el("span", { text: "No size data for this window yet — its transcripts predate the re-index or are gone." })));
      }
      out.append(growthTables(data));
    };
    sel.addEventListener("change", () => { av.window_id = Number(sel.value); load(); });
    load();
    return wrap;
  }

  function sizeTable(headers, rows) {
    const wrap = el("div", { class: "table-wrap" });
    const table = el("table", { class: "data" });
    table.append(el("thead", {}, el("tr", {}, ...headers.map((h, i) => el("th", { class: i ? "num" : "", text: h })))));
    const tbody = el("tbody");
    for (const r of rows) tbody.append(el("tr", {}, ...r.map((c, i) => el("td", { class: i ? "num" : "", text: String(c) }))));
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  function growthTables(data) {
    const box = el("div");
    const total = data.categories.reduce((a, c) => a + c.tokens, 0) || 1;
    box.append(el("h4", { text: `${fmtCompact(total)} tokens over ${data.calls} calls` }));
    box.append(sizeTable(["Category", "Tokens", "Share"],
      data.categories.map((c) => [CATEGORY_LABEL[c.category] || c.category, fmtInt(c.tokens),
        (100 * c.tokens / total).toFixed(1) + "%"])));
    box.append(el("h4", { text: "Tool results by tool" }));
    box.append(sizeTable(["Tool", "Calls", "Tokens", "p95", "max", "Chars"],
      data.tools.map((t) => [t.name, t.calls, fmtInt(t.tokens), fmtInt(t.p95), fmtInt(t.max), fmtInt(t.chars)])));
    box.append(el("h4", { text: "Largest single items" }));
    box.append(sizeTable(["Item", "Call", "Time", "Tokens", "Chars"],
      data.top_items.map((e) => [`${CATEGORY_LABEL[e.category] || e.category}: ${e.name}`, "#" + e.call,
        fmtDateTime(e.ts), fmtInt(e.tokens), fmtInt(e.chars)])));
    box.append(el("h4", { text: "Every step (context growth per call)" }));
    const stepRows = data.steps.map((st, i) => {
      const top = [...st.entries].sort((a, b) => b.tokens - a.tokens).slice(0, 3)
        .map((e) => `${e.name} ${fmtCompact(e.tokens)}`).join(", ");
      return ["#" + (i + 1) + " " + fmtDateTime(st.ts), st.delta == null ? "start" : "+" + fmtInt(st.delta), fmtInt(st.context), top];
    });
    const stepTable = sizeTable(["Call", "Growth", "Context", "Biggest pieces"], stepRows);
    stepTable.querySelectorAll("td:last-child").forEach((td) => { td.classList.remove("num"); });
    box.append(stepTable);
    return box;
  }

  // Sessions of the selected chain, newest first — the chart bands are precise
  // but hard to aim at, so the same navigation is offered as a plain table.
  function chainSessionTable(chain) {
    const wrapCard = el("div", { class: "chain-sessions" });
    wrapCard.append(el("h3", { text: "Sessions in this chain" }));
    wrapCard.append(el("div", { class: "card-sub", text: "newest first · click a row to open the session" }));
    const wrap = el("div", { class: "table-wrap" });
    const table = el("table", { class: "data" });
    table.append(el("thead", {}, el("tr", {},
      el("th", { text: "Session" }),
      el("th", { text: "Created" }),
      el("th", { text: "Windows" }),
      el("th", { class: "num", text: "Bot msgs" }),
      el("th", { class: "num", text: "Calls" }),
      el("th", { class: "num", text: "Est $" }),
      el("th", { text: "" }))));
    const tbody = el("tbody");
    for (const s of [...chain.sessions].reverse()) {
      const tr = el("tr", { class: "clickable", onclick: () => { location.hash = "#/session/" + s.session_id; } });
      tr.append(el("td", { text: shortId(s.session_id) }));
      tr.append(el("td", { text: fmtDateTime(s.created_at) }));
      tr.append(el("td", {
        text: s.min_window == null ? "–"
          : (s.min_window === s.max_window ? "w" + s.min_window : `w${s.min_window}–w${s.max_window}`),
      }));
      tr.append(el("td", { class: "num", text: fmtInt(s.message_count) }));
      tr.append(el("td", { class: "num", text: s.api_call_count != null ? fmtInt(s.api_call_count) : "–" }));
      tr.append(el("td", { class: "num", text: s.est_cost_usd != null ? fmtUsd(s.est_cost_usd) : "–" }));
      const marks = el("td");
      if (!s.in_index) marks.append(el("span", { class: "badge warn", text: "not indexed" }));
      if (s.seeded_start) marks.append(el("span", { class: "badge", text: "seeded" }));
      if (s.rotated_into) marks.append(el("span", { class: "badge", text: "rotated →" }));
      tr.append(marks);
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    wrapCard.append(wrap);
    return wrapCard;
  }

  function drawChainChart(container, series, ann, chain) {
    const width = Math.max(560, container.clientWidth || 900);
    const height = 320;
    const pad = { l: 58, r: 14, t: 26, b: 24 };
    const plotW = width - pad.l - pad.r;
    const plotH = height - pad.t - pad.b;
    const node = svg("svg", { width, height, class: "chart" });
    container.append(node);

    const n = series.length;
    const refresh = ann.refresh_threshold || 220000;
    let maxY = refresh * 1.08;
    for (const p of series) maxY = Math.max(maxY, p.context_provided);
    const x = linScale(0, Math.max(1, n - 1), pad.l, pad.l + plotW);
    const y = linScale(0, maxY, pad.t + plotH, pad.t);

    // session background bands (decoration only — the crosshair overlay on top
    // owns hit-testing, including click → session screen)
    const seededMap = new Map((chain ? chain.sessions : []).map((s) => [s.session_id, s]));
    let bandStart = 0;
    let bandIdx = 0;
    for (let i = 1; i <= n; i++) {
      if (i === n || series[i].session_id !== series[bandStart].session_id) {
        const sid = series[bandStart].session_id;
        const x0 = bandStart === 0 ? pad.l : (x(bandStart - 1) + x(bandStart)) / 2;
        const x1 = i === n ? pad.l + plotW : (x(i - 1) + x(i)) / 2;
        const band = svg("rect", {
          x: x0, y: pad.t, width: Math.max(1, x1 - x0), height: plotH,
          fill: bandIdx % 2 === 0 ? "var(--band-a)" : "var(--band-b)",
          class: "no-hit",
        });
        const info = seededMap.get(sid);
        node.append(band);
        // session-start rule + first_call_context label + seeded marker. Every
        // new session gets a visible boundary (a /new with no rotation looked
        // identical to no event at all); a rotation draws its red rule on top.
        if (bandStart > 0) {
          node.append(svg("line", {
            x1: x0, x2: x0, y1: pad.t, y2: pad.t + plotH,
            stroke: SESSION_LINE.color, "stroke-width": 1.5, "stroke-dasharray": "4 4",
            class: "no-hit",
          }));
          node.append(svg("path", {
            d: `M${x0} ${pad.t + plotH} l-4 7 l8 0 Z`,
            fill: SESSION_LINE.color, class: "no-hit",
          }));
        }
        const fcc = info && info.first_call_context != null ? info.first_call_context : series[bandStart].context_provided;
        if (x1 - x0 > 72) { // suppress the label on narrow bands (collisions)
          node.append(svg("text", {
            x: x0 + 3, y: pad.t - 14, class: "axis-label",
            text: shortId(sid, 6) + " · " + fmtCompact(fcc),
          }));
        }
        if (info && info.seeded_start) {
          const dot = svg("circle", { cx: x0 + 3, cy: pad.t - 6, r: 3, fill: "var(--s-fresh)" });
          attachTip(dot, () => [{ header: "seeded start" }, { label: "transcript begins with <session_notes>/<recent_conversation>", value: "" }]);
          node.append(dot);
        }
        bandStart = i;
        bandIdx++;
      }
    }

    // grid + y axis
    const g = svg("g", { class: "no-hit" });
    yAxis(g, y, niceTicks(maxY, 4), pad.l, pad.l + plotW);
    node.append(g);

    // labeled reference lines at eval/rotation thresholds
    // thresholds apply to the real context level, i.e. the blue line, and come
    // from the server (which reads the bot's own settings) — never hardcoded
    for (const [value, label] of [
      [ann.eval_threshold || 120000, "eval " + fmtCompact(ann.eval_threshold || 120000) + " (context)"],
      [refresh, "rotation " + fmtCompact(refresh) + " (context)"],
    ]) {
      const ry = y(value);
      node.append(svg("line", { x1: pad.l, x2: pad.l + plotW, y1: ry, y2: ry, stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "5 4", class: "no-hit" }));
      node.append(svg("text", { x: pad.l + plotW - 4, y: ry - 4, class: "axis-label", "text-anchor": "end", text: label }));
    }

    // rotation markers where window_id increments between consecutive points
    // (null window_id = session without bot messages → unknown, not window 0)
    for (let i = 1; i < n; i++) {
      const wPrev = series[i - 1].window_id;
      const wCur = series[i].window_id;
      if (wPrev != null && wCur != null && wCur > wPrev) {
        const rx = (x(i - 1) + x(i)) / 2;
        node.append(svg("line", { x1: rx, x2: rx, y1: pad.t, y2: pad.t + plotH, stroke: "var(--st-crit)", "stroke-width": 2, class: "no-hit" }));
        const flag = svg("path", { d: `M${rx} ${pad.t} l8 4 l-8 4 Z`, fill: "var(--st-crit)" });
        attachTip(flag, () => [
          { header: "window rotation" },
          { label: `window ${series[i - 1].window_id || 0} → ${series[i].window_id || 0}`, value: "" },
          { label: fmtDateTime(series[i].ts), value: "" },
        ]);
        node.append(flag);
      }
    }

    // step line: real context provided per call
    const ctxPts = series.map((p, i) => [x(i), y(p.context_provided)]);
    node.append(svg("path", { d: stepPath(ctxPts), fill: "none", stroke: CONTEXT_LINE.color, "stroke-width": 2, "stroke-linejoin": "round", class: "no-hit" }));

    // cache-expiry markers on the context line (icon + tooltip, not color-alone)
    for (let i = 0; i < n; i++) {
      if (!series[i].cache_expiry) continue;
      const mk = svg("circle", { cx: x(i), cy: y(series[i].context_provided), r: 4.5, fill: "var(--st-serious)", stroke: "var(--surface)", "stroke-width": 2 });
      attachTip(mk, () => [
        { header: "cache expiry" },
        { label: fmtDateTime(series[i].ts), value: "" },
        { label: "context re-written after idle gap", value: "" },
      ]);
      node.append(mk);
    }

    // x labels (call sequence)
    const nLabels = Math.max(2, Math.floor(plotW / 120));
    for (let k = 0; k <= nLabels; k++) {
      const i = Math.round((k / nLabels) * (n - 1));
      node.append(svg("text", { x: x(i), y: height - 6, class: "axis-label", "text-anchor": "middle", text: fmtDateTime(series[i].ts) }));
    }

    // crosshair overlay listing both series at the nearest call
    const cross = svg("line", { y1: pad.t, y2: pad.t + plotH, class: "axis-base", opacity: 0, "pointer-events": "none" });
    node.append(cross);
    // The overlay covers the whole plot, so it — not the session bands beneath
    // it — receives every pointer event. Navigation therefore lives here.
    const overlay = svg("rect", { x: pad.l, y: pad.t, width: plotW, height: plotH, fill: "transparent", class: "clickable" });
    const pointAt = (clientX) => {
      const rect = node.getBoundingClientRect();
      return Math.max(0, Math.min(n - 1, Math.round(x.invert(clientX - rect.left))));
    };
    overlay.addEventListener("click", (e) => {
      hideTip();
      location.hash = "#/session/" + series[pointAt(e.clientX)].session_id;
    });
    overlay.addEventListener("pointermove", (e) => {
      const i = pointAt(e.clientX);
      const p = series[i];
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      cross.setAttribute("opacity", 0.6);
      const info = seededMap.get(p.session_id);
      showTip(e.clientX, e.clientY, [
        { header: `call ${i + 1} of ${n} · window ${p.window_id ?? "?"}` },
        { label: fmtDateTime(p.ts), value: "" },
        { label: "session " + shortId(p.session_id), value: "" },
        ...(info ? [
          { label: "seeded start", value: info.seeded_start ? "yes" : "no" },
          { label: "bot msgs", value: fmtInt(info.message_count) },
        ] : []),
        { line: CONTEXT_LINE.color, value: fmtCompact(p.context_provided), label: "context provided" },
        ...(p.delta != null ? [{ label: "context growth since previous call", value: "+" + fmtInt(p.delta) }] : []),
        ...(p.tools || []).map((t) => ({ label: "result: " + t[0] + " (" + fmtInt(t[1]) + " chars)", value: "~" + fmtCompact(t[2]) })),
        ...(p.first_of_session ? [{ label: "first call of session", value: "" }] : []),
        ...(p.cache_expiry ? [{ label: "cache expiry", value: "" }] : []),
        { label: "click to open this session", value: "" },
      ]);
    });
    overlay.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
    node.append(overlay);
  }

  await Promise.all([loadReconciliation(), loadChains()]);

  return {
    // Poll refreshes only the reconciliation card: rebuilding the chain
    // explorer reset the window pick and scroll every poll (owner, 17.10). The
    // chain explorer reloads on its own ↻ button or a filter change.
    refresh: async () => { await loadReconciliation(); },
  };
}
