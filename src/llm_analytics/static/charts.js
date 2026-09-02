// charts.js — reusable SVG chart helpers (dataviz method: thin marks, 2px
// surface gaps, recessive hairline grid, tooltip layer, text in ink tokens).

const SVG_NS = "http://www.w3.org/2000/svg";

// ---------------------------------------------------------------------------
// DOM builders (textContent only — labels are untrusted data)
// ---------------------------------------------------------------------------

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  for (const child of children) appendChild(node, child);
  return node;
}

export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyAttrs(node, attrs);
  for (const child of children) appendChild(node, child);
  return node;
}

function applyAttrs(node, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === "text") node.textContent = String(v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, String(v));
  }
}

function appendChild(node, child) {
  if (child === undefined || child === null) return;
  if (Array.isArray(child)) { for (const c of child) appendChild(node, c); return; }
  node.append(child.nodeType ? child : document.createTextNode(String(child)));
}

// ---------------------------------------------------------------------------
// Number / date formatting
// ---------------------------------------------------------------------------

export function fmtInt(n) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString("en-US");
}

export function fmtCompact(n) {
  if (n === null || n === undefined) return "–";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return fmtInt(Math.round(n));
}

export function fmtUsd(n) {
  if (n === null || n === undefined) return "–";
  const a = Math.abs(n);
  if (a >= 1e4) return "$" + fmtCompact(n);
  if (a >= 100) return "$" + Math.round(n).toLocaleString("en-US");
  if (a >= 1) return "$" + n.toFixed(2);
  if (a === 0) return "$0";
  return "$" + n.toFixed(3);
}

export function fmtDur(ms) {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1000) return ms + "ms";
  if (ms < 60_000) return (ms / 1000).toFixed(1) + "s";
  return Math.floor(ms / 60_000) + "m " + Math.round((ms % 60_000) / 1000) + "s";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso);
  if (isNaN(d)) return iso;
  return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate();
}

export function fmtDateTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (x) => String(x).padStart(2, "0");
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

export function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

export function shortId(id, n = 8) {
  return id ? String(id).slice(0, n) : "–";
}

// ---------------------------------------------------------------------------
// Scales & ticks
// ---------------------------------------------------------------------------

export function linScale(d0, d1, r0, r1) {
  const dd = d1 - d0 || 1;
  const f = (v) => r0 + ((v - d0) / dd) * (r1 - r0);
  f.invert = (r) => d0 + ((r - r0) / (r1 - r0 || 1)) * dd;
  return f;
}

export function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0];
  const raw = max / count;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = pow;
  for (const m of [1, 2, 5, 10]) {
    if (pow * m >= raw) { step = pow * m; break; }
  }
  const out = [];
  for (let v = 0; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

// ---------------------------------------------------------------------------
// Tooltip singleton
// ---------------------------------------------------------------------------

let tipNode = null;

function tipEl() {
  if (!tipNode) {
    tipNode = el("div", { class: "chart-tip", role: "status" });
    document.body.append(tipNode);
  }
  return tipNode;
}

// rows: [{header}] | [{label, value, swatch?, line?}]  — all via textContent.
export function showTip(clientX, clientY, rows) {
  const tip = tipEl();
  tip.textContent = "";
  for (const row of rows) {
    if (row.header !== undefined) {
      tip.append(el("div", { class: "tip-header", text: row.header }));
      continue;
    }
    const r = el("div", { class: "tip-row" });
    if (row.swatch) r.append(el("span", { class: "tip-swatch", style: `background:${row.swatch}` }));
    else if (row.line) r.append(el("span", { class: "tip-line", style: `background:${row.line}` }));
    r.append(el("span", { class: "tip-value", text: row.value }));
    r.append(el("span", { class: "tip-label", text: row.label }));
    tip.append(r);
  }
  tip.style.display = "block";
  const rect = tip.getBoundingClientRect();
  let x = clientX + 14;
  let y = clientY + 14;
  if (x + rect.width > window.innerWidth - 8) x = clientX - rect.width - 14;
  if (y + rect.height > window.innerHeight - 8) y = clientY - rect.height - 14;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

export function hideTip() {
  if (tipNode) tipNode.style.display = "none";
}

export function attachTip(target, buildRows) {
  target.addEventListener("pointermove", (e) => {
    const rows = buildRows(e);
    if (rows) showTip(e.clientX, e.clientY, rows);
    else hideTip();
  });
  target.addEventListener("pointerleave", hideTip);
}

// ---------------------------------------------------------------------------
// Legend
// ---------------------------------------------------------------------------

// defs: [{label, color, kind: "rect"|"line"|"marker"}]
export function legend(defs) {
  const box = el("div", { class: "legend" });
  for (const d of defs) {
    box.append(
      el("span", { class: "legend-item" },
        el("span", { class: "legend-swatch legend-" + (d.kind || "rect"), style: `background:${d.color}` }),
        el("span", { class: "legend-label", text: d.label })));
  }
  return box;
}

// ---------------------------------------------------------------------------
// Axes (recessive: hairline grid, muted labels)
// ---------------------------------------------------------------------------

export function yAxis(g, yScale, tickValues, x0, x1, format = fmtCompact) {
  for (const t of tickValues) {
    const y = yScale(t);
    g.append(svg("line", { x1: x0, x2: x1, y1: y, y2: y, class: t === 0 ? "axis-base" : "grid-line" }));
    g.append(svg("text", { x: x0 - 6, y: y + 3.5, class: "axis-label", "text-anchor": "end", text: format(t) }));
  }
}

// ---------------------------------------------------------------------------
// Stacked bar chart with optional line overlay (same unit → one axis)
// ---------------------------------------------------------------------------

// opts: { height, data: [{label, parts: [n…], meta?}], series: [{label,color}],
//         line?: {label,color,values:[n…]}, onBarClick?, xLabelEvery? }
export function stackedBarChart(container, opts) {
  const width = Math.max(320, container.clientWidth || 640);
  const height = opts.height || 220;
  const pad = { l: 52, r: 12, t: 10, b: 22 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;
  const data = opts.data;
  const n = data.length;
  const node = svg("svg", { width, height, class: "chart" });
  if (!n) { container.append(node); return node; }

  let maxY = 0;
  for (let i = 0; i < n; i++) {
    const total = data[i].parts.reduce((a, b) => a + (b || 0), 0);
    if (total > maxY) maxY = total;
    if (opts.line && opts.line.values[i] > maxY) maxY = opts.line.values[i];
  }
  const tickVals = niceTicks(maxY);
  maxY = Math.max(maxY, tickVals[tickVals.length - 1]) || 1;
  const y = linScale(0, maxY, pad.t + plotH, pad.t);
  const slot = plotW / n;
  const gap = slot >= 6 ? 2 : slot >= 3 ? 1 : 0;
  const barW = Math.min(24, Math.max(1, slot - gap));
  const xPos = (i) => pad.l + i * slot + (slot - barW) / 2;

  const g = svg("g");
  yAxis(g, y, tickVals, pad.l, pad.l + plotW);
  node.append(g);

  const every = opts.xLabelEvery || Math.max(1, Math.ceil(n / Math.floor(plotW / 56)));
  for (let i = 0; i < n; i++) {
    if (i % every === 0 || n === 1) {
      node.append(svg("text", {
        x: xPos(i) + barW / 2, y: height - 6, class: "axis-label",
        "text-anchor": "middle", text: data[i].label,
      }));
    }
  }

  const segGap = barW >= 5 ? 2 : 0; // surface gap between stacked segments
  for (let i = 0; i < n; i++) {
    const group = svg("g", { class: "bar-group" + (opts.onBarClick ? " clickable" : "") });
    let acc = 0;
    let topIdx = -1;
    for (let s = data[i].parts.length - 1; s >= 0; s--) if (data[i].parts[s] > 0) { topIdx = s; break; }
    for (let s = 0; s < data[i].parts.length; s++) {
      const v = data[i].parts[s] || 0;
      if (v <= 0) continue;
      const y1 = y(acc);
      const y2 = y(acc + v);
      const h = Math.max(0.5, y1 - y2 - (s === topIdx ? 0 : segGap));
      group.append(svg("rect", {
        x: xPos(i), y: y2, width: barW, height: h,
        rx: s === topIdx && barW >= 6 ? 2 : 0,
        fill: opts.series[s].color,
      }));
      acc += v;
    }
    // hit target wider than the mark
    const hit = svg("rect", {
      x: pad.l + i * slot, y: pad.t, width: slot, height: plotH,
      fill: "transparent", class: "bar-hit",
    });
    attachTip(hit, (e) => {
      const rows = [{ header: data[i].label }];
      for (let s = 0; s < opts.series.length; s++) {
        rows.push({ swatch: opts.series[s].color, value: (opts.format || fmtCompact)(data[i].parts[s] || 0), label: opts.series[s].label });
      }
      if (opts.line) rows.push({ line: opts.line.color, value: (opts.format || fmtCompact)(opts.line.values[i] || 0), label: opts.line.label });
      if (opts.extraTip) for (const r of opts.extraTip(i)) rows.push(r);
      return rows;
    });
    if (opts.onBarClick) hit.addEventListener("click", () => opts.onBarClick(i));
    group.append(hit);
    node.append(group);
  }

  if (opts.line) {
    let d = "";
    for (let i = 0; i < n; i++) {
      const px = xPos(i) + barW / 2;
      const py = y(opts.line.values[i] || 0);
      d += (i === 0 ? "M" : "L") + px.toFixed(1) + " " + py.toFixed(1);
    }
    node.append(svg("path", { d, fill: "none", stroke: opts.line.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", class: "no-hit" }));
  }
  container.append(node);
  return node;
}

// ---------------------------------------------------------------------------
// Horizontal bar chart (single measure → single hue, direct end labels)
// ---------------------------------------------------------------------------

// opts: {data: [{label, value, meta?}], color, format, height?, onClick?, tip?}
export function hBarChart(container, opts) {
  const width = Math.max(280, container.clientWidth || 480);
  const rowH = 26;
  const barH = 14;
  const pad = { l: 8, r: 64, t: 4, b: 4 };
  const labelW = Math.min(190, Math.max(90, width * 0.28));
  const data = opts.data;
  const height = pad.t + pad.b + data.length * rowH;
  const node = svg("svg", { width, height, class: "chart" });
  const maxV = Math.max(...data.map((d) => d.value), 0) || 1;
  const x = linScale(0, maxV, pad.l + labelW, width - pad.r);
  const format = opts.format || fmtCompact;
  data.forEach((d, i) => {
    const yTop = pad.t + i * rowH + (rowH - barH) / 2;
    const g = svg("g", { class: opts.onClick ? "clickable" : "" });
    g.append(svg("text", { x: pad.l + labelW - 8, y: yTop + barH - 3, class: "axis-label bar-cat", "text-anchor": "end", text: d.label }));
    g.append(svg("rect", { x: x(0), y: yTop, width: Math.max(1, x(d.value) - x(0)), height: barH, rx: 3, fill: opts.color }));
    g.append(svg("text", { x: x(d.value) + 6, y: yTop + barH - 3, class: "bar-value", text: format(d.value) }));
    const hit = svg("rect", { x: 0, y: pad.t + i * rowH, width, height: rowH, fill: "transparent" });
    if (opts.tip) attachTip(hit, () => opts.tip(i));
    if (opts.onClick) hit.addEventListener("click", () => opts.onClick(i));
    g.append(hit);
    node.append(g);
  });
  container.append(node);
  return node;
}

// ---------------------------------------------------------------------------
// Step line path ("after" steps)
// ---------------------------------------------------------------------------

export function stepPath(points) {
  // points: [[x,y],…]
  if (!points.length) return "";
  let d = "M" + points[0][0].toFixed(1) + " " + points[0][1].toFixed(1);
  for (let i = 1; i < points.length; i++) {
    d += "H" + points[i][0].toFixed(1) + "V" + points[i][1].toFixed(1);
  }
  return d;
}

// ---------------------------------------------------------------------------
// Tiny sparkline (stat-tile trend; no axes)
// ---------------------------------------------------------------------------

export function sparkline(container, values, { width = 120, height = 32, color } = {}) {
  const node = svg("svg", { width, height, class: "chart spark" });
  const n = values.length;
  if (n < 2) { container.append(node); return node; }
  const maxV = Math.max(...values) || 1;
  const x = linScale(0, n - 1, 2, width - 2);
  const y = linScale(0, maxV, height - 2, 2);
  let d = "";
  values.forEach((v, i) => { d += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); });
  node.append(svg("path", { d, fill: "none", stroke: color, "stroke-width": 1.5, "stroke-linejoin": "round" }));
  const last = values[n - 1];
  node.append(svg("circle", { cx: x(n - 1), cy: y(last), r: 2.5, fill: color, class: "spark-dot" }));
  container.append(node);
  return node;
}

// ---------------------------------------------------------------------------
// Sparkline with brush — the minimap/zoom for long call sequences.
// Series are normalized to their own max (navigation aid, not an axis chart).
// ---------------------------------------------------------------------------

// opts: {n, series: [{values, color, label, format}], window: [i0,i1]|null,
//        onWindow(win|null), height?}
export function brushSparkline(container, opts) {
  const width = Math.max(320, container.clientWidth || 640);
  const height = opts.height || 52;
  const pad = { l: 4, r: 4, t: 4, b: 4 };
  const n = opts.n;
  const node = svg("svg", { width, height, class: "chart brush-spark" });
  if (n < 2) { container.append(node); return node; }
  const x = linScale(0, n - 1, pad.l, width - pad.r);
  for (const s of opts.series) {
    const maxV = Math.max(...s.values) || 1;
    const y = linScale(0, maxV, height - pad.b, pad.t);
    let d = "";
    s.values.forEach((v, i) => { d += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); });
    node.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 1.5, class: "no-hit" }));
  }
  const shade = svg("rect", { y: 0, height, fill: "var(--brush-fill)", stroke: "var(--brush-stroke)", "stroke-width": 1, class: "brush-window no-hit" });
  node.append(shade);
  const drawWindow = (win) => {
    if (!win) { shade.setAttribute("width", 0); shade.setAttribute("x", -10); return; }
    const [a, b] = win;
    shade.setAttribute("x", x(a));
    shade.setAttribute("width", Math.max(2, x(b) - x(a)));
  };
  drawWindow(opts.window);

  const idxAt = (clientX) => {
    const rect = node.getBoundingClientRect();
    const i = Math.round(x.invert(clientX - rect.left));
    return Math.max(0, Math.min(n - 1, i));
  };
  let anchor = null;
  node.addEventListener("pointerdown", (e) => {
    anchor = idxAt(e.clientX);
    node.setPointerCapture(e.pointerId);
    drawWindow([anchor, anchor]);
  });
  node.addEventListener("pointermove", (e) => {
    if (anchor === null) {
      const i = idxAt(e.clientX);
      const rows = [{ header: "call " + (i + 1) + " of " + n }];
      for (const s of opts.series) {
        rows.push({ line: s.color, value: (s.format || fmtCompact)(s.values[i]), label: s.label });
      }
      showTip(e.clientX, e.clientY, rows);
      return;
    }
    const i = idxAt(e.clientX);
    drawWindow([Math.min(anchor, i), Math.max(anchor, i)]);
  });
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("pointerup", (e) => {
    if (anchor === null) return;
    const i = idxAt(e.clientX);
    const win = [Math.min(anchor, i), Math.max(anchor, i)];
    anchor = null;
    if (win[1] - win[0] < 2) { drawWindow(opts.window); return; } // treat as click
    opts.onWindow(win);
  });
  node.addEventListener("dblclick", () => opts.onWindow(null));
  container.append(node);
  return node;
}

// ---------------------------------------------------------------------------
// Small UI pieces
// ---------------------------------------------------------------------------

export function statTile({ label, value, sub, trend, hero }) {
  const tile = el("div", { class: "tile" + (hero ? " tile-hero" : "") });
  tile.append(el("div", { class: "tile-label", text: label }));
  tile.append(el("div", { class: "tile-value", text: value }));
  if (sub) tile.append(el("div", { class: "tile-sub", text: sub }));
  if (trend) {
    const t = el("div", { class: "tile-trend" });
    sparkline(t, trend.values, { color: trend.color, width: 132, height: 30 });
    tile.append(t);
  }
  return tile;
}

export function emptyState(msg) {
  return el("div", { class: "empty-state", text: msg });
}

export function errorState(msg) {
  return el("div", { class: "error-state" },
    el("span", { class: "error-icon", text: "!" }),
    el("span", { text: msg }));
}

export function loadingState() {
  return el("div", { class: "empty-state", text: "Loading…" });
}
