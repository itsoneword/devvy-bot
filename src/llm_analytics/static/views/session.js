// views/session.js — Session screen (#/session/<id>): lane timeline with
// per-call stacked bars, tool glyph strips, turn/event annotations, and a
// cumulative sparkline that doubles as the brush/zoom for long sessions.

import { api } from "../api.js";
import {
  el, svg, legend, brushSparkline, attachTip, showTip, hideTip,
  linScale, niceTicks, yAxis,
  fmtInt, fmtCompact, fmtUsd, fmtDateTime, fmtTime, fmtDur, shortId,
  emptyState, errorState, loadingState,
} from "../charts.js";

const GAP_COMPRESS_S = 600;   // wall-clock gaps beyond this are compressed
const BREAK_SPAN_S = 240;     // compressed representation of a long gap
const LANE_LIMIT = 8;         // subagent lanes shown before "+N more"

const STACK = [
  { key: "cache_read_tokens", label: "Cache read", color: "var(--s-cache-read)" },
  { key: "cache_creation_tokens", label: "Cache write", color: "var(--s-cache-write)" },
  { key: "input_tokens", label: "Fresh input", color: "var(--s-fresh)" },
];
const OUTPUT = { label: "Output (cap)", color: "var(--s-output)" };

const EVENT_STYLE = {
  cache_expiry: { color: "var(--st-serious)", label: "cache expiry" },
  partial_cache_hit: { color: "var(--st-warn)", label: "partial cache hit" },
  context_cliff: { color: "var(--st-crit)", label: "context cliff (inferred)" },
};

export async function renderSession(view, state, sessionId) {
  const vs = (state.sessionView[sessionId] ||= {
    mode: "index", window: null, lanesExpanded: false, selectedCall: null,
  });

  const root = el("div");
  view.append(root);

  let data = null; // {detail, timeline, calls (indexed), ct[], breaks[]}

  async function fetchData() {
    const [detail, timeline] = await Promise.all([
      api.session(sessionId),
      api.timeline(sessionId, "*"),
    ]);
    const calls = timeline.calls; // ts-ordered across all lanes
    const times = calls.map((c) => Date.parse(c.ts));
    const ct = new Array(calls.length); // compressed wall-clock coordinate (s)
    const breaks = []; // global indices i where gap (i-1 → i) was compressed
    for (let i = 0; i < calls.length; i++) {
      calls[i]._gi = i;
      if (i === 0) { ct[i] = 0; continue; }
      const dt = (times[i] - times[i - 1]) / 1000;
      if (dt > GAP_COMPRESS_S) { ct[i] = ct[i - 1] + BREAK_SPAN_S; breaks.push(i); }
      else ct[i] = ct[i - 1] + Math.max(dt, 0);
    }
    // cumulative series across ALL lanes (session progress, drives the brush)
    let cumOut = 0, cumCost = 0;
    const cumOutSeries = [], cumCostSeries = [];
    for (const c of calls) {
      cumOut += c.output_tokens || 0;
      cumCost += c.est_cost_usd || 0;
      cumOutSeries.push(cumOut);
      cumCostSeries.push(cumCost);
    }
    data = { detail, timeline, calls, times, ct, breaks, cumOutSeries, cumCostSeries };
  }

  // map an arbitrary timestamp onto the compressed-clock coordinate
  function mapClock(tsMs) {
    const { times, ct } = data;
    if (!times.length) return 0;
    if (tsMs <= times[0]) return ct[0];
    let lo = 0, hi = times.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (times[mid] <= tsMs) lo = mid; else hi = mid - 1;
    }
    if (lo >= times.length - 1) return ct[ct.length - 1] + Math.min((tsMs - times[times.length - 1]) / 1000, BREAK_SPAN_S);
    const span = (times[lo + 1] - times[lo]) / 1000;
    const frac = span > 0 ? (tsMs - times[lo]) / 1000 / span : 0;
    return ct[lo] + frac * (ct[lo + 1] - ct[lo]);
  }

  function render() {
    root.textContent = "";
    const { detail, timeline, calls } = data;
    const s = detail.session;

    renderHeader(root, detail, timeline);

    if (!calls.length) {
      const card = el("div", { class: "card" });
      card.append(el("h2", { text: "Timeline" }));
      card.append(emptyState(detail.missing_transcript
        ? "No API calls indexed — the transcript file is missing (retention)."
        : "No API calls indexed for this session yet."));
      root.append(card);
      return;
    }

    const win = vs.window && vs.window[1] < calls.length ? vs.window : null;
    const [w0, w1] = win || [0, calls.length - 1];

    const card = el("div", { class: "card" });
    const head = el("div", { class: "card-head" });
    head.append(el("h2", { text: "Timeline" }));
    head.append(el("div", { class: "card-sub", text: `${fmtInt(calls.length)} calls (all lanes)` + (timeline.truncated ? " · TRUNCATED at 10k rows" : "") }));
    head.append(el("div", { class: "spacer" }));
    const controls = el("div", { class: "timeline-controls" });
    const modeGroup = el("div", { class: "preset-group", role: "group", "aria-label": "X axis" });
    for (const [mode, label] of [["index", "call index"], ["clock", "wall clock"]]) {
      modeGroup.append(el("button", {
        type: "button", text: label,
        class: vs.mode === mode ? "active" : "",
        onclick: () => { vs.mode = mode; render(); },
      }));
    }
    controls.append(modeGroup);
    if (win) {
      controls.append(el("button", {
        class: "btn small", type: "button",
        text: `zoomed ${w0 + 1}–${w1 + 1} · reset`,
        onclick: () => { vs.window = null; render(); },
      }));
    }
    head.append(controls);
    card.append(head);

    card.append(legend([
      ...STACK.map((d) => ({ label: d.label, color: d.color, kind: "rect" })),
      { label: OUTPUT.label, color: OUTPUT.color, kind: "rect" },
      { label: "tool call", color: "var(--muted)", kind: "rect" },
      { label: "tool error", color: "var(--st-crit)", kind: "rect" },
    ]));

    // ---- lanes ----
    const mainCalls = calls.filter((c) => c.agent_id === null);
    const toolsByLane = groupBy(timeline.tools, (t) => t.agent_id);
    const eventsByLane = groupBy(timeline.events, (e) => e.agent_id);

    const mainLane = el("div", { class: "lane" });
    const mainHeader = el("div", { class: "lane-header" });
    mainHeader.append(el("span", { class: "lane-name", text: "main thread" }));
    mainHeader.append(el("span", { text: `${fmtInt(mainCalls.length)} calls · ${fmtCompact(sum(mainCalls, "output_tokens"))} out · ${fmtUsd(sum(mainCalls, "est_cost_usd"))}` }));
    mainLane.append(mainHeader);
    const mainBox = el("div", { class: "chart-box" });
    mainLane.append(mainBox);
    card.append(mainLane);

    const laneWidth = Math.max(480, (root.clientWidth || view.clientWidth || 1100) - 76);
    drawLane(mainBox, {
      laneCalls: mainCalls, tools: toolsByLane.get(null) || [],
      events: eventsByLane.get(null) || [], turns: timeline.turns,
      width: laneWidth, height: 236, barArea: 178, axis: true, win: [w0, w1],
    });

    // ---- subagent lanes ----
    const subagents = timeline.subagents || [];
    if (subagents.length) {
      const callsByAgent = groupBy(calls, (c) => c.agent_id);
      const shown = vs.lanesExpanded ? subagents : subagents.slice(0, LANE_LIMIT);
      const laneWrap = el("div", { class: "lane-sub" });
      for (const agent of shown) {
        const laneCalls = callsByAgent.get(agent.agent_id) || [];
        const lane = el("div", { class: "lane" });
        const header = el("div", { class: "lane-header" });
        header.append(el("span", { class: "lane-name", text: agent.subagent_type || "subagent" }));
        if (agent.description) header.append(el("span", { text: trim(agent.description, 56) }));
        header.append(el("span", { text: `${fmtInt(agent.api_call_count)} calls · ${fmtCompact(agent.output_tokens)} out · ${fmtUsd(agent.est_cost_usd)}` }));
        if (agent.model_override) header.append(el("span", { class: "badge", text: agent.model_override }));
        if (!agent.spawn_tool_use_id) header.append(el("span", { class: "chip chip-muted", text: "unlinked" }));
        lane.append(header);
        if (laneCalls.length) {
          const box = el("div", { class: "chart-box" });
          lane.append(box);
          drawLane(box, {
            laneCalls, tools: toolsByLane.get(agent.agent_id) || [],
            events: eventsByLane.get(agent.agent_id) || [], turns: [],
            width: laneWidth - 36, height: 62, barArea: 40, axis: false, win: [w0, w1],
          });
        } else {
          lane.append(el("div", { class: "card-sub", text: "no calls in index" }));
        }
        laneWrap.append(lane);
      }
      if (subagents.length > LANE_LIMIT) {
        laneWrap.append(el("button", {
          class: "btn small", type: "button",
          text: vs.lanesExpanded
            ? "Collapse lanes"
            : `+${subagents.length - LANE_LIMIT} more lanes`,
          onclick: () => { vs.lanesExpanded = !vs.lanesExpanded; render(); },
        }));
      }
      card.append(laneWrap);
    }

    // ---- cumulative sparkline / brush ----
    const brushWrap = el("div", { class: "chart-box", style: "margin-top:8px" });
    card.append(el("div", { class: "card-sub", text: "cumulative output + est-$ · drag to zoom the timeline, double-click to reset" }));
    card.append(brushWrap);
    brushSparkline(brushWrap, {
      n: calls.length,
      series: [
        { values: data.cumOutSeries, color: "var(--s-output)", label: "cum output", format: fmtCompact },
        { values: data.cumCostSeries, color: "var(--deemph)", label: "cum est-$", format: fmtUsd },
      ],
      window: win,
      onWindow: (w) => { vs.window = w; render(); },
    });

    root.append(card);

    if (vs.selectedCall) {
      const call = calls.find((c) => c.request_id === vs.selectedCall);
      if (call) renderDetailPanel(call);
      else vs.selectedCall = null;
    }
  }

  // ---- one lane chart -----------------------------------------------------
  function drawLane(container, opts) {
    const { laneCalls, tools, events, turns, width, height, barArea, axis, win } = opts;
    const [w0, w1] = win;
    const padL = axis ? 54 : 8;
    const padR = 10;
    const padT = 8;
    const plotW = width - padL - padR;
    const stripY = padT + barArea + 4;
    const node = svg("svg", { width, height, class: "chart" });
    container.append(node);

    // x scale over the visible window (global index or compressed clock)
    const xOf = vs.mode === "index"
      ? (() => { const sc = linScale(w0, w1, padL, padL + plotW); return (c) => sc(c._gi); })()
      : (() => {
          const sc = linScale(data.ct[w0], data.ct[w1] || data.ct[w0] + 1, padL, padL + plotW);
          return (c) => sc(data.ct[c._gi]);
        })();
    const xOfTs = vs.mode === "index"
      ? (tsMs) => { // interpolate a raw ts into index space
          const { times } = data;
          let lo = 0, hi = times.length - 1;
          while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (times[mid] <= tsMs) lo = mid; else hi = mid - 1; }
          const sc = linScale(w0, w1, padL, padL + plotW);
          if (lo >= times.length - 1) return sc(times.length - 1);
          const span = times[lo + 1] - times[lo];
          return sc(lo + (span > 0 ? (tsMs - times[lo]) / span : 0));
        }
      : (tsMs) => {
          const sc = linScale(data.ct[w0], data.ct[w1] || data.ct[w0] + 1, padL, padL + plotW);
          return sc(mapClock(tsMs));
        };

    const visible = laneCalls.filter((c) => c._gi >= w0 && c._gi <= w1);
    let maxY = 0;
    for (const c of visible) maxY = Math.max(maxY, c.context_provided || 0);
    const tickVals = axis ? niceTicks(maxY || 1, 3) : [0, maxY || 1];
    const yMax = Math.max(maxY, tickVals[tickVals.length - 1]) || 1;
    const y = linScale(0, yMax, padT + barArea, padT);

    if (axis) {
      const g = svg("g");
      yAxis(g, y, tickVals, padL, padL + plotW);
      node.append(g);
    } else {
      node.append(svg("line", { x1: padL, x2: padL + plotW, y1: padT + barArea, y2: padT + barArea, class: "axis-base" }));
    }

    const slots = Math.max(1, w1 - w0);
    const bw = Math.max(1.5, Math.min(14, plotW / (slots + 1) - 1));

    // per-call stacked bars, output as a distinct cap on top
    for (const c of visible) {
      const cx = xOf(c);
      let acc = 0;
      const segGap = bw >= 5 ? 2 : 0;
      const parts = STACK.map((d) => c[d.key] || 0);
      const topIdx = lastPositive(parts);
      for (let sIdx = 0; sIdx < parts.length; sIdx++) {
        const v = parts[sIdx];
        if (v <= 0) continue;
        const y1 = y(acc), y2 = y(acc + v);
        node.append(svg("rect", {
          x: cx - bw / 2, y: y2,
          width: bw, height: Math.max(0.5, y1 - y2 - (sIdx === topIdx ? 0 : segGap)),
          fill: STACK[sIdx].color, class: "no-hit",
        }));
        acc += v;
      }
      // output cap: fixed-position marker at the stack top (value in tooltip;
      // output is orders of magnitude below context, so height is symbolic)
      node.append(svg("rect", {
        x: cx - bw / 2, y: Math.max(padT, y(acc) - 3), width: bw, height: 2.5,
        fill: OUTPUT.color, class: "no-hit",
      }));
    }

    // turn boundary rules (main lane only) with excerpt on hover
    for (const turn of turns) {
      const tx = xOfTs(Date.parse(turn.ts));
      if (tx < padL - 1 || tx > padL + plotW + 1) continue;
      node.append(svg("line", { x1: tx, x2: tx, y1: padT, y2: padT + barArea, class: "grid-line", "stroke-dasharray": "1 3" }));
      const marker = svg("rect", { x: tx - 3, y: padT - 6, width: 6, height: 6, rx: 1, fill: "var(--axis)" });
      attachTip(marker, () => [
        { header: `turn · ${turn.source}` + (turn.user_id ? ` · user ${turn.user_id}` : "") },
        { label: fmtDateTime(turn.ts), value: "" },
        ...(turn.prompt_excerpt ? [{ label: trim(turn.prompt_excerpt, 180), value: "" }] : []),
        ...(turn.cost != null ? [{ label: "bot cost", value: fmtUsd(turn.cost) }] : []),
      ]);
      node.append(marker);
    }

    // derived-event annotation rules (context_cliff labeled "inferred")
    for (const ev of events) {
      const ex = xOfTs(Date.parse(ev.ts));
      if (ex < padL - 1 || ex > padL + plotW + 1) continue;
      const style = EVENT_STYLE[ev.kind] || { color: "var(--muted)", label: ev.kind };
      node.append(svg("line", { x1: ex, x2: ex, y1: padT, y2: padT + barArea, stroke: style.color, "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0.7, class: "no-hit" }));
      const tri = svg("path", { d: `M${ex - 4} ${padT - 1} L${ex + 4} ${padT - 1} L${ex} ${padT + 5} Z`, fill: style.color });
      attachTip(tri, () => {
        const rows = [{ header: style.label }, { label: fmtDateTime(ev.ts), value: "" }];
        if (ev.detail && typeof ev.detail === "object") {
          for (const [k, v] of Object.entries(ev.detail)) rows.push({ label: k, value: fmtCompact(v) });
        }
        return rows;
      });
      node.append(tri);
    }

    // axis-break markers for compressed gaps (wall-clock mode)
    if (vs.mode === "clock") {
      for (const gi of data.breaks) {
        if (gi <= w0 || gi > w1) continue;
        const bx = (xOf(data.calls[gi - 1]) + xOf(data.calls[gi])) / 2;
        for (const off of [-2, 2]) {
          node.append(svg("line", { x1: bx + off - 3, x2: bx + off + 3, y1: padT + barArea + 5, y2: padT + barArea - 5, stroke: "var(--muted)", "stroke-width": 1.5, class: "no-hit" }));
        }
      }
    }

    // bottom axis labels (main lane only)
    if (axis) {
      const nLabels = Math.max(2, Math.floor(plotW / 110));
      for (let k = 0; k <= nLabels; k++) {
        const gi = Math.round(w0 + (k / nLabels) * (w1 - w0));
        const c = data.calls[gi];
        if (!c) continue;
        node.append(svg("text", {
          x: vs.mode === "index" ? linScale(w0, w1, padL, padL + plotW)(gi) : xOf(c),
          y: height - 4, class: "axis-label", "text-anchor": "middle",
          text: vs.mode === "index" ? "#" + (gi + 1) : fmtTime(c.ts),
        }));
      }
    }

    // crosshair overlay: snap to the lane's nearest call (hit target ≫ mark)
    if (visible.length) {
      const cross = svg("line", { y1: padT, y2: padT + barArea, class: "axis-base", opacity: 0, "pointer-events": "none" });
      node.append(cross);
      const overlay = svg("rect", { x: padL, y: padT, width: plotW, height: barArea, fill: "transparent", class: "clickable" });
      const nearest = (clientX) => {
        const rect = node.getBoundingClientRect();
        const px = clientX - rect.left;
        let best = null, bestD = Infinity;
        for (const c of visible) {
          const d = Math.abs(xOf(c) - px);
          if (d < bestD) { bestD = d; best = c; }
        }
        return best;
      };
      overlay.addEventListener("pointermove", (e) => {
        const c = nearest(e.clientX);
        if (!c) { hideTip(); return; }
        const cx = xOf(c);
        cross.setAttribute("x1", cx);
        cross.setAttribute("x2", cx);
        cross.setAttribute("opacity", 0.6);
        showTip(e.clientX, e.clientY, [
          { header: `call #${c._gi + 1} · ${c.model}` },
          { label: fmtDateTime(c.ts), value: "" },
          { swatch: STACK[0].color, value: fmtCompact(c.cache_read_tokens), label: "cache read" },
          { swatch: STACK[1].color, value: fmtCompact(c.cache_creation_tokens), label: "cache write" },
          { swatch: STACK[2].color, value: fmtCompact(c.input_tokens), label: "fresh input" },
          { swatch: OUTPUT.color, value: fmtCompact(c.output_tokens), label: "output" },
          { label: "context provided", value: fmtCompact(c.context_provided) },
          { label: "est-$", value: fmtUsd(c.est_cost_usd) },
          { label: "click for detail", value: "" },
        ]);
      });
      overlay.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
      overlay.addEventListener("click", (e) => {
        const c = nearest(e.clientX);
        if (c) { vs.selectedCall = c.request_id; render(); }
      });
      node.append(overlay);
    }

    // tool glyph strip (appended last → on top; errors visually distinct)
    for (const tool of tools) {
      const tx = xOfTs(Date.parse(tool.ts));
      if (tx < padL - 1 || tx > padL + plotW + 1) continue;
      let w = 4;
      if (vs.mode === "clock" && tool.result_ts) {
        w = Math.max(3, xOfTs(Date.parse(tool.result_ts)) - tx);
      }
      const isErr = tool.is_error === 1;
      const glyph = svg("rect", {
        x: tx - 1.5, y: stripY, width: w, height: 8, rx: 1.5,
        fill: isErr ? "var(--st-crit)" : "var(--muted)",
        opacity: isErr ? 1 : 0.75,
      });
      attachTip(glyph, () => [
        { header: tool.name + (isErr ? " · ERROR" : "") },
        { label: fmtDateTime(tool.ts), value: "" },
        { label: "duration", value: fmtDur(tool.duration_ms) },
        ...(tool.input_summary ? [{ label: trim(tool.input_summary, 160), value: "" }] : []),
      ]);
      node.append(glyph);
    }
  }

  // ---- header -------------------------------------------------------------
  function renderHeader(parent, detail, timeline) {
    const s = detail.session;
    const card = el("div", { class: "card" });
    const head = el("div", { class: "card-head" });
    head.append(el("h2", { text: "Session " + shortId(s.session_id, 12) }));
    head.append(el("span", { class: "badge ep-" + (s.entrypoint || "unknown"), text: s.entrypoint || "?" }));
    if (s.seeded_start) head.append(el("span", { class: "badge", text: "seeded start" }));
    if (detail.missing_transcript) head.append(el("span", { class: "badge warn", text: "no transcript" }));
    head.append(el("div", { class: "spacer" }));
    head.append(el("a", { href: "#/", text: "← overview" }));
    card.append(head);

    const meta = el("div", { class: "session-meta" });
    const metaPairs = [
      ["profile", s.profile], ["user", s.user_id != null ? String(s.user_id) : null],
      ["cwd", s.cwd], ["branch", s.git_branch], ["cli", s.cli_version],
      ["first", fmtDateTime(s.first_ts)], ["last", fmtDateTime(s.last_ts)],
    ];
    for (const [k, v] of metaPairs) {
      if (v) meta.append(el("span", {}, k + " ", el("b", { text: v })));
    }
    card.append(meta);

    const totals = el("div", { class: "session-meta" });
    const totalPairs = [
      ["calls", fmtInt(s.api_call_count)], ["output", fmtCompact(s.output_tokens)],
      ["cache read", fmtCompact(s.cache_read_tokens)], ["cache write", fmtCompact(s.cache_creation_tokens)],
      ["fresh input", fmtCompact(s.input_tokens)], ["est-$", fmtUsd(s.est_cost_usd)],
      ["tools", fmtInt(s.tool_call_count)], ["subagents", fmtInt(s.subagent_count)],
      ["turns", fmtInt(s.turn_count)],
    ];
    for (const [k, v] of totalPairs) totals.append(el("span", {}, k + " ", el("b", { text: v })));
    card.append(totals);

    if (detail.missing_transcript) {
      card.append(el("div", { class: "banner" },
        el("span", { class: "banner-icon", text: "!" }),
        el("span", { text: "Transcript file no longer on disk (retention) — indexed data shown; raw call view unavailable." })));
    }
    parent.append(card);
  }

  // ---- call detail side panel --------------------------------------------
  function renderDetailPanel(call) {
    const panel = el("aside", { class: "detail-panel" });
    const title = el("h3");
    title.append(el("span", { text: "Call #" + (call._gi + 1) }));
    title.append(el("button", { class: "panel-close", type: "button", text: "×", "aria-label": "Close", onclick: () => { vs.selectedCall = null; render(); } }));
    panel.append(title);
    const kv = el("dl", { class: "kv" });
    const rows = [
      ["request_id", call.request_id],
      ["timestamp", call.ts],
      ["model", call.model],
      ["lane", call.agent_id ? "subagent " + shortId(call.agent_id) : "main thread"],
      ["fresh input", fmtInt(call.input_tokens)],
      ["cache read", fmtInt(call.cache_read_tokens)],
      ["cache write", fmtInt(call.cache_creation_tokens)],
      ["· 5m / 1h", fmtInt(call.cache_5m_tokens) + " / " + fmtInt(call.cache_1h_tokens)],
      ["output", fmtInt(call.output_tokens)],
      ["context provided", fmtInt(call.context_provided)],
      ["est-$", fmtUsd(call.est_cost_usd)],
      ["cum output (lane)", fmtInt(call.cum_output_tokens)],
      ["cum est-$ (lane)", fmtUsd(call.cum_est_cost_usd)],
    ];
    for (const [k, v] of rows) {
      kv.append(el("dt", { text: k }), el("dd", { text: v }));
    }
    kv.append(el("dt", { text: "pricing rule" }));
    const dd = el("dd", {});
    dd.append(el("span", {
      class: "badge" + (call.pricing_rule === "default" ? " rule-default" : ""),
      text: call.pricing_rule + (call.pricing_rule === "default" ? " (unknown model)" : ""),
    }));
    kv.append(dd);
    panel.append(kv);

    const rawBox = el("div", { style: "margin-top:12px" });
    const rawBtn = el("button", {
      class: "btn small", type: "button", text: "View raw transcript line",
      onclick: async () => {
        rawBtn.disabled = true;
        rawBox.textContent = "";
        rawBox.append(loadingState());
        try {
          const raw = await api.callRaw(call.request_id);
          rawBox.textContent = "";
          rawBox.append(el("pre", { text: JSON.stringify(raw, null, 2) }));
        } catch (e) {
          rawBox.textContent = "";
          rawBox.append(errorState(e.status === 404
            ? "Raw line unavailable — transcript missing or rotated."
            : "Failed to fetch raw line: " + e.message));
        } finally {
          rawBtn.disabled = false;
        }
      },
    });
    panel.append(rawBtn, rawBox);
    root.append(panel);
  }

  // ---- initial load -------------------------------------------------------
  root.append(loadingState());
  try {
    await fetchData();
  } catch (e) {
    root.textContent = "";
    if (e.status === 404) {
      root.append(el("div", { class: "card" },
        el("h2", { text: "Session not found" }),
        el("div", { class: "card-sub", text: sessionId }),
        errorState("This session is not in the index."),
        el("a", { href: "#/", text: "← back to overview" })));
    } else {
      root.append(errorState("Failed to load session: " + e.message));
    }
    return { refresh: async () => {} };
  }
  render();

  return {
    refresh: async () => { await fetchData(); render(); },
  };
}

// ---- small utils ----------------------------------------------------------

function groupBy(rows, keyFn) {
  const map = new Map();
  for (const row of rows) {
    const k = keyFn(row);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(row);
  }
  return map;
}

function sum(rows, key) {
  let out = 0;
  for (const r of rows) out += r[key] || 0;
  return out;
}

function lastPositive(parts) {
  for (let i = parts.length - 1; i >= 0; i--) if (parts[i] > 0) return i;
  return -1;
}

function trim(text, n) {
  const t = String(text);
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}
