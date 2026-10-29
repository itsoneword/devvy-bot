// ui_harness.mjs — drive the real LLM Analytics frontend (static/app.js) in
// node against a live API server, with a minimal DOM shim (no jsdom here).
//
// Usage: node ui_harness.mjs <spec.json>
// spec = { base: "http://127.0.0.1:PORT/", static_dir: "/abs/static", out,
//          actions: [ {op, ...}, ... ] }
// Writes one JSON object to spec.out: { snapshots: [{label, tree, requests}], errors }.
//
// Ops (each waits until every fetch has settled):
//   click_button {text}           click the <button> whose text equals `text`
//   expand {label, until}         in the "source" card: if `until` is not yet
//                                 visible (text or title/aria-label), click
//                                 the row, or the one control in the row,
//                                 holding `label`
//   toggle_archive                click the archive toggle (checkbox/button
//                                 whose label, text, aria-label or title
//                                 matches /archiv|архив/i)
//   select_source {match}         in a filter <select> pick the option whose
//                                 value (else text) contains `match`
//   snapshot {label}              serialize the view + the request log
//
// Visibility (computed by the Python side from the tree): an element is
// hidden when it has the `hidden` attribute/property, style display:none,
// or sits inside a closed <details> outside its <summary>.

import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
const errors = [];

// ---------------------------------------------------------------------------
// DOM shim
// ---------------------------------------------------------------------------

class ShimEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = init.bubbles !== false;
    this.detail = init.detail;
    this.target = null;
    this.currentTarget = null;
    this.defaultPrevented = false;
    this._stop = false;
    this.key = init.key;
    this.clientX = 0;
    this.clientY = 0;
  }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; }
  preventDefault() { this.defaultPrevented = true; }
}

class ShimNode {
  constructor() {
    this.childNodes = [];
    this.parentNode = null;
    this._listeners = {};
  }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const s = this.parentNode.childNodes;
    return s[s.indexOf(this) + 1] || null;
  }
  get previousSibling() {
    if (!this.parentNode) return null;
    const s = this.parentNode.childNodes;
    return s[s.indexOf(this) - 1] || null;
  }
  get nextElementSibling() {
    let n = this.nextSibling;
    while (n && n.nodeType !== 1) n = n.nextSibling;
    return n;
  }
  get previousElementSibling() {
    let n = this.previousSibling;
    while (n && n.nodeType !== 1) n = n.previousSibling;
    return n;
  }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === document;
  }
  _norm(nodes) {
    const out = [];
    for (const n of nodes) {
      if (n === null || n === undefined) continue;
      if (typeof n === "object" && n.nodeType === 11) out.push(...n.childNodes.slice());
      else if (typeof n === "object" && n.nodeType) out.push(n);
      else out.push(new ShimText(String(n)));
    }
    return out;
  }
  _insertAt(index, nodes) {
    for (const n of this._norm(nodes)) {
      if (n.parentNode) {
        const s = n.parentNode.childNodes;
        const i = s.indexOf(n);
        if (n.parentNode === this && i < index) index -= 1;
        s.splice(i, 1);
      }
      n.parentNode = this;
      this.childNodes.splice(index, 0, n);
      index += 1;
    }
  }
  append(...nodes) { this._insertAt(this.childNodes.length, nodes); }
  prepend(...nodes) { this._insertAt(0, nodes); }
  appendChild(n) { this.append(n); return n; }
  insertBefore(n, ref) {
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    this._insertAt(i < 0 ? this.childNodes.length : i, [n]);
    return n;
  }
  removeChild(n) {
    const i = this.childNodes.indexOf(n);
    if (i >= 0) { this.childNodes.splice(i, 1); n.parentNode = null; }
    return n;
  }
  replaceChild(n, old) { this.insertBefore(n, old); this.removeChild(old); return old; }
  replaceChildren(...nodes) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(...nodes) {
    const p = this.parentNode;
    if (!p) return;
    const i = p.childNodes.indexOf(this);
    p.removeChild(this);
    p._insertAt(i, nodes);
  }
  before(...nodes) { const p = this.parentNode; if (p) p._insertAt(p.childNodes.indexOf(this), nodes); }
  after(...nodes) { const p = this.parentNode; if (p) p._insertAt(p.childNodes.indexOf(this) + 1, nodes); }
  contains(n) {
    while (n) { if (n === this) return true; n = n.parentNode; }
    return false;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    if (v !== null && v !== undefined && String(v) !== "") this.append(new ShimText(String(v)));
  }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    const l = this._listeners[type];
    if (l) this._listeners[type] = l.filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    if (!(ev instanceof ShimEvent)) {
      const e2 = new ShimEvent(ev.type, { bubbles: ev.bubbles });
      ev = e2;
    }
    ev.target = this;
    let node = this;
    while (node) {
      ev.currentTarget = node;
      const fns = (node._listeners[ev.type] || []).slice();
      const prop = node["on" + ev.type];
      if (typeof prop === "function" && !fns.includes(prop)) fns.push(prop);
      for (const fn of fns) {
        try {
          const r = typeof fn === "function" ? fn.call(node, ev) : fn.handleEvent(ev);
          if (r && typeof r.catch === "function") r.catch((e) => errors.push("handler: " + (e && e.stack || e)));
        } catch (e) {
          errors.push("handler: " + (e && e.stack || e));
        }
      }
      if (ev._stop || !ev.bubbles) break;
      node = node.parentNode;
    }
    return !ev.defaultPrevented;
  }
  hasClickHandler() {
    return (this._listeners.click || []).length > 0 || typeof this.onclick === "function";
  }
  cloneNode(deep) {
    let c;
    if (this.nodeType === 3) return new ShimText(this.data);
    if (this.nodeType === 11) c = new ShimFragment();
    else {
      c = new ShimElement(this.tagName.toLowerCase(), this.namespaceURI);
      for (const [k, v] of this._attrs) c._attrs.set(k, v);
      Object.assign(c._style, this._style);
    }
    if (deep) for (const ch of this.childNodes) c.append(ch.cloneNode(true));
    return c;
  }
}

class ShimText extends ShimNode {
  constructor(data) { super(); this.nodeType = 3; this.data = data; this.nodeName = "#text"; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  get nodeValue() { return this.data; }
}

class ShimFragment extends ShimNode {
  constructor() { super(); this.nodeType = 11; this.nodeName = "#document-fragment"; }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
}

function makeStyle() {
  const s = {};
  return new Proxy(s, {
    get(t, k) {
      if (k === "setProperty") return (p, v) => { t[p] = String(v); };
      if (k === "removeProperty") return (p) => { delete t[p]; };
      if (k === "getPropertyValue") return (p) => t[p] || "";
      if (k === "cssText") return Object.entries(t).map(([a, b]) => `${a}: ${b}`).join("; ");
      return t[k] ?? "";
    },
    set(t, k, v) {
      if (k === "cssText") {
        for (const key of Object.keys(t)) delete t[key];
        for (const part of String(v).split(";")) {
          const [a, ...b] = part.split(":");
          if (a && a.trim()) t[a.trim()] = b.join(":").trim();
        }
        return true;
      }
      t[k] = v === null || v === undefined ? "" : String(v);
      return true;
    },
  });
}

const REFLECTED_BOOL = ["hidden", "open", "disabled", "selected", "multiple", "required", "readonly"];

class ShimElement extends ShimNode {
  constructor(tag, ns) {
    super();
    this.nodeType = 1;
    this.namespaceURI = ns || "http://www.w3.org/1999/xhtml";
    this.tagName = ns && ns.includes("svg") ? tag : tag.toUpperCase();
    this.localName = tag.toLowerCase();
    this.nodeName = this.tagName;
    this._attrs = new Map();
    this._style = makeStyle();
    this._value = undefined;
    this._checked = undefined;
    const self = this;
    this.classList = {
      _list() { return (self.getAttribute("class") || "").split(/\s+/).filter(Boolean); },
      add(...c) { const l = this._list(); for (const x of c) if (!l.includes(x)) l.push(x); self.setAttribute("class", l.join(" ")); },
      remove(...c) { self.setAttribute("class", this._list().filter((x) => !c.includes(x)).join(" ")); },
      contains(c) { return this._list().includes(c); },
      toggle(c, force) {
        const has = this.contains(c);
        const want = force === undefined ? !has : !!force;
        if (want && !has) this.add(c);
        if (!want && has) this.remove(c);
        return want;
      },
      replace(a, b) { if (this.contains(a)) { this.remove(a); this.add(b); } },
      get length() { return this._list().length; },
      forEach(fn) { this._list().forEach(fn); },
      [Symbol.iterator]() { return this._list()[Symbol.iterator](); },
    };
    this.dataset = new Proxy({}, {
      get(_, k) {
        if (typeof k !== "string") return undefined;
        return self.getAttribute("data-" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())) ?? undefined;
      },
      set(_, k, v) { self.setAttribute("data-" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()), v); return true; },
      deleteProperty(_, k) { self.removeAttribute("data-" + k); return true; },
    });
  }
  get style() { return this._style; }
  set style(v) { this._style.cssText = v; }
  setAttribute(k, v) {
    k = String(k);
    if (k === "style") { this._style.cssText = String(v); return; }
    this._attrs.set(k, String(v));
  }
  getAttribute(k) {
    if (k === "style") { const t = this._style.cssText; return t || null; }
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  hasAttribute(k) { return this._attrs.has(k); }
  removeAttribute(k) { this._attrs.delete(k); }
  toggleAttribute(k, force) {
    const want = force === undefined ? !this.hasAttribute(k) : !!force;
    if (want) this.setAttribute(k, ""); else this.removeAttribute(k);
    return want;
  }
  get id() { return this.getAttribute("id") || ""; }
  set id(v) { this.setAttribute("id", v); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get title() { return this.getAttribute("title") || ""; }
  set title(v) { this.setAttribute("title", v); }
  get type() { return (this.getAttribute("type") || (this.localName === "button" ? "submit" : "text")).toLowerCase(); }
  set type(v) { this.setAttribute("type", v); }
  get name() { return this.getAttribute("name") || ""; }
  set name(v) { this.setAttribute("name", v); }
  get htmlFor() { return this.getAttribute("for") || ""; }
  set htmlFor(v) { this.setAttribute("for", v); }
  get href() { return this.getAttribute("href") || ""; }
  set href(v) { this.setAttribute("href", v); }
  get label() { return this.getAttribute("label") || this.textContent; }
  set label(v) { this.setAttribute("label", v); }
  get innerHTML() { return this.textContent; }
  set innerHTML(v) { this.textContent = String(v).replace(/<[^>]*>/g, ""); }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get options() { return queryAll(this, "option"); }
  get selectedOptions() { return this.options.filter((o) => o.selected); }
  get selectedIndex() {
    const opts = this.options;
    const i = opts.findIndex((o) => o.selected);
    return i >= 0 ? i : opts.length ? 0 : -1;
  }
  set selectedIndex(i) { this.options.forEach((o, j) => { o.selected = j === i; }); }
  get value() {
    if (this.localName === "select") {
      const opts = this.options;
      const o = opts.find((x) => x.selected) || opts[0];
      return o ? o.value : "";
    }
    if (this.localName === "option") {
      return this.hasAttribute("value") ? this.getAttribute("value") : this.textContent;
    }
    if (this._value !== undefined) return this._value;
    return this.getAttribute("value") ?? (this.type === "checkbox" ? "on" : "");
  }
  set value(v) {
    v = v === null || v === undefined ? "" : String(v);
    if (this.localName === "select") {
      let hit = false;
      for (const o of this.options) {
        o.selected = !hit && o.value === v;
        if (o.selected) hit = true;
      }
      return;
    }
    if (this.localName === "option") { this.setAttribute("value", v); return; }
    this._value = v;
  }
  get checked() { return this._checked !== undefined ? this._checked : this.hasAttribute("checked"); }
  set checked(v) { this._checked = !!v; }
  get selected() { return this._selected !== undefined ? this._selected : this.hasAttribute("selected"); }
  set selected(v) {
    this._selected = !!v;
    if (v && this.localName === "option") {
      const sel = this.closest("select");
      if (sel) for (const o of sel.options) if (o !== this) o._selected = false;
    }
  }
  get offsetWidth() { return 800; }
  get offsetHeight() { return 300; }
  get clientWidth() { return 800; }
  get clientHeight() { return 300; }
  get scrollWidth() { return 800; }
  get scrollHeight() { return 300; }
  getBoundingClientRect() { return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 300, width: 800, height: 300 }; }
  getBBox() { return { x: 0, y: 0, width: 50, height: 12 }; }
  getComputedTextLength() { return 50; }
  getTotalLength() { return 100; }
  focus() {}
  blur() {}
  select() {}
  scrollIntoView() {}
  scrollTo() {}
  setPointerCapture() {}
  releasePointerCapture() {}
  showModal() { this.open = true; }
  show() { this.open = true; }
  close() { this.open = false; }
  click() {
    if (this.disabled) return;
    if (this.localName === "input" && (this.type === "checkbox" || this.type === "radio")) {
      this.checked = this.type === "radio" ? true : !this.checked;
      this.dispatchEvent(new ShimEvent("click"));
      this.dispatchEvent(new ShimEvent("input"));
      this.dispatchEvent(new ShimEvent("change"));
      return;
    }
    const ev = new ShimEvent("click");
    const ok = this.dispatchEvent(ev);
    if (!ok) return;
    if (this.localName === "summary" && this.parentElement && this.parentElement.localName === "details") {
      const d = this.parentElement;
      d.open = !d.open;
      d.dispatchEvent(new ShimEvent("toggle", { bubbles: false }));
    } else if (this.localName === "label") {
      const ctl = this.htmlFor ? document.getElementById(this.htmlFor) : queryAll(this, "input,select,button")[0];
      if (ctl && ctl !== this) ctl.click();
    }
  }
  matches(sel) { return matchesSelector(this, sel); }
  closest(sel) {
    let n = this;
    while (n && n.nodeType === 1) { if (matchesSelector(n, sel)) return n; n = n.parentNode; }
    return null;
  }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
  getElementsByTagName(t) { return queryAll(this, t); }
  getElementsByClassName(c) { return queryAll(this, "." + c); }
  insertAdjacentElement(pos, n) {
    if (pos === "beforeend") this.append(n);
    else if (pos === "afterbegin") this.prepend(n);
    else if (pos === "beforebegin") this.before(n);
    else this.after(n);
    return n;
  }
  insertAdjacentText(pos, t) { this.insertAdjacentElement(pos, new ShimText(t)); }
}
for (const b of REFLECTED_BOOL) {
  if (b === "selected") continue;
  Object.defineProperty(ShimElement.prototype, b, {
    get() { return this.hasAttribute(b); },
    set(v) { if (v) this.setAttribute(b, ""); else this.removeAttribute(b); },
  });
}

function descendants(root) {
  const out = [];
  const walk = (n) => { for (const c of n.childNodes) if (c.nodeType === 1) { out.push(c); walk(c); } };
  walk(root);
  return out;
}

function matchesSimple(elm, simple) {
  const re = /([#.]?[^#.\[\]:]+)|(\[[^\]]+\])|(:[a-z-]+(\([^)]*\))?)/gi;
  const parts = simple.match(re) || [];
  for (const p of parts) {
    if (p === "*") continue;
    if (p.startsWith("#")) { if (elm.id !== p.slice(1)) return false; }
    else if (p.startsWith(".")) { if (!elm.classList.contains(p.slice(1))) return false; }
    else if (p.startsWith("[")) {
      const m = p.slice(1, -1).match(/^([^~|^$*=]+)(?:([~|^$*]?=)["']?(.*?)["']?)?$/);
      if (!m) return false;
      const v = elm.getAttribute(m[1].trim());
      if (v === null) return false;
      if (m[2] === "=" && v !== m[3]) return false;
      if (m[2] === "*=" && !v.includes(m[3])) return false;
      if (m[2] === "^=" && !v.startsWith(m[3])) return false;
      if (m[2] === "$=" && !v.endsWith(m[3])) return false;
      if (m[2] === "~=" && !v.split(/\s+/).includes(m[3])) return false;
    } else if (p.startsWith(":")) {
      if (p === ":checked" && !elm.checked) return false;
      if (p === ":first-child" && elm.parentNode && elm.parentNode.children[0] !== elm) return false;
      if (p.startsWith(":not(")) { if (matchesSelector(elm, p.slice(5, -1))) return false; }
    } else if (elm.localName !== p.toLowerCase()) return false;
  }
  return true;
}

function matchesSelector(elm, sel) {
  return sel.split(",").some((alt) => {
    const chain = alt.trim().replace(/\s*>\s*/g, " ").split(/\s+/).filter(Boolean);
    if (!chain.length) return false;
    if (!matchesSimple(elm, chain[chain.length - 1])) return false;
    let node = elm.parentNode;
    for (let i = chain.length - 2; i >= 0; i--) {
      while (node && node.nodeType === 1 && !matchesSimple(node, chain[i])) node = node.parentNode;
      if (!node || node.nodeType !== 1) return false;
      node = node.parentNode;
    }
    return true;
  });
}

function queryAll(root, sel) { return descendants(root).filter((e) => matchesSelector(e, sel)); }

class ShimDocument extends ShimNode {
  constructor() {
    super();
    this.nodeType = 9;
    this.documentElement = new ShimElement("html");
    this.head = new ShimElement("head");
    this.body = new ShimElement("body");
    this.documentElement.append(this.head, this.body);
    this.append(this.documentElement);
    this.hidden = false;
    this.visibilityState = "visible";
    this.activeElement = this.body;
  }
  createElement(tag) { return new ShimElement(tag); }
  createElementNS(ns, tag) { return new ShimElement(tag, ns); }
  createTextNode(t) { return new ShimText(String(t)); }
  createDocumentFragment() { return new ShimFragment(); }
  createComment() { return new ShimText(""); }
  getElementById(id) { return descendants(this).find((e) => e.id === id) || null; }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
}

const document = new ShimDocument();
globalThis.document = document;
globalThis.window = globalThis;
globalThis.Node = ShimNode;
globalThis.Element = ShimElement;
globalThis.HTMLElement = ShimElement;
globalThis.SVGElement = ShimElement;
globalThis.Text = ShimText;
globalThis.Event = ShimEvent;
globalThis.CustomEvent = ShimEvent;
globalThis.innerWidth = 1280;
globalThis.innerHeight = 900;
globalThis.devicePixelRatio = 1;
globalThis.location = { hash: "", href: spec.base, pathname: "/", search: "", origin: spec.base.replace(/\/$/, ""), reload() {} };
globalThis.history = { pushState() {}, replaceState() {}, back() {} };
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.sessionStorage = globalThis.localStorage;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.getComputedStyle = (e) => e.style;
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.MutationObserver = class { observe() {} disconnect() {} takeRecords() { return []; } };
globalThis.alert = () => {};
globalThis.confirm = () => true;
const winListeners = {};
globalThis.addEventListener = (t, fn) => { (winListeners[t] ||= []).push(fn); };
globalThis.removeEventListener = () => {};

// Page skeleton (index.html).
const main = document.createElement("main");
main.id = "view";
const health = document.createElement("span");
health.id = "health";
document.body.append(health, main);

// ---------------------------------------------------------------------------
// fetch: relative URLs resolve against the live server; every call is logged
// ---------------------------------------------------------------------------

const realFetch = globalThis.fetch;
const requests = [];
let inflight = 0;
globalThis.fetch = async (url, opts) => {
  const abs = new URL(String(url), spec.base);
  inflight += 1;
  const rec = { path: abs.pathname.replace(/^\//, ""), params: Object.fromEntries(abs.searchParams), status: null, json: null };
  requests.push(rec);
  try {
    const res = await realFetch(abs, opts);
    const text = await res.text();
    rec.status = res.status;
    try { rec.json = JSON.parse(text); } catch { rec.json = null; }
    return {
      ok: res.ok, status: res.status, statusText: res.statusText,
      json: async () => JSON.parse(text), text: async () => text,
    };
  } finally {
    setTimeout(() => { inflight -= 1; }, 0);
  }
};

process.on("unhandledRejection", (e) => errors.push("unhandled: " + (e && e.stack || e)));
const origConsoleError = console.error;
console.error = (...a) => errors.push("console.error: " + a.map(String).join(" "));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function idle() {
  let quiet = 0;
  for (let i = 0; i < 400 && quiet < 6; i++) {
    await sleep(15);
    quiet = inflight === 0 ? quiet + 1 : 0;
  }
}

// ---------------------------------------------------------------------------
// Visibility + lookup helpers (mirror the Python side)
// ---------------------------------------------------------------------------

function selfHidden(e) {
  if (e.hasAttribute("hidden")) return true;
  const d = String(e.style.display || "").trim();
  if (d === "none") return true;
  return false;
}
function isVisible(e) {
  let n = e;
  let child = null;
  while (n && n.nodeType === 1) {
    if (selfHidden(n)) return false;
    if (n.localName === "details" && !n.open && child && child.localName !== "summary") return false;
    child = n;
    n = n.parentNode;
  }
  return true;
}
function visibleText(root) {
  const parts = [];
  const walk = (n, child) => {
    for (const c of n.childNodes) {
      if (c.nodeType === 3) parts.push(c.data);
      else if (c.nodeType === 1) {
        if (selfHidden(c)) continue;
        if (n.nodeType === 1 && n.localName === "details" && !n.open && c.localName !== "summary") continue;
        walk(c);
      }
    }
  };
  walk(root);
  return parts.join(" ");
}
function visibleTextAndTitles(root) {
  const parts = [visibleText(root)];
  for (const e of descendants(root)) {
    if (!isVisible(e)) continue;
    for (const k of ["title", "aria-label"]) if (e.hasAttribute(k)) parts.push(e.getAttribute(k));
  }
  return parts.join(" ");
}
function sourceCard() {
  for (const card of queryAll(main, ".card")) {
    const h = queryAll(card, "h2,h3")[0];
    if (h && /source|источник/i.test(h.textContent)) return card;
  }
  return null;
}
function clickable(e) {
  return e.hasClickHandler() || ["summary", "button", "a"].includes(e.localName) ||
    (e.localName === "input" && ["checkbox", "radio", "button"].includes(e.type));
}

async function expand(label, until) {
  const card = sourceCard();
  if (!card) { errors.push("expand: no source card"); return; }
  const low = (s) => s.toLowerCase();
  if (until && low(visibleTextAndTitles(card)).includes(low(until))) return; // already open
  const holders = descendants(card).filter((e) => isVisible(e) && low(visibleText(e)).includes(low(label)));
  if (!holders.length) { errors.push("expand: no visible row for " + label); return; }
  holders.sort((a, b) => visibleText(a).length - visibleText(b).length);
  let node = holders[0];
  // Walk up from the label to its row: the first ancestor that is itself
  // clickable, or holds exactly one visible control. A level with several
  // controls is past the row (it holds other rows) — stop there.
  while (node && node !== card.parentNode) {
    if (clickable(node)) { node.click(); await idle(); return; }
    const inner = descendants(node).filter((e) => isVisible(e) && clickable(e));
    if (inner.length === 1) { inner[0].click(); await idle(); return; }
    if (inner.length > 1) break;
    node = node.parentNode;
  }
  errors.push("expand: nothing clickable for " + label);
}

async function toggleArchive() {
  const re = /archiv|архив/i;
  const boxes = queryAll(main, "input").filter((i) => i.type === "checkbox" && isVisible(i));
  for (const box of boxes) {
    const lbl = box.closest("label") || (box.id && document.querySelector(`label[for=${box.id}]`));
    const texts = [
      lbl ? lbl.textContent : "", box.parentElement ? box.parentElement.textContent : "",
      box.getAttribute("aria-label") || "", box.title, box.name, box.id,
    ];
    if (texts.some((t) => re.test(t))) { box.click(); await idle(); return; }
  }
  const cands = descendants(main).filter((e) => isVisible(e) && clickable(e) &&
    (re.test(e.textContent) || re.test(e.getAttribute("aria-label") || "") || re.test(e.title)));
  if (cands.length) {
    cands.sort((a, b) => a.textContent.length - b.textContent.length);
    cands[0].click();
    await idle();
    return;
  }
  errors.push("toggle_archive: no archive toggle found");
}

async function selectSource(match) {
  const low = match.toLowerCase();
  for (const sel of queryAll(main, "select")) {
    const opts = sel.options.filter((o) => !o.disabled);
    let hit = opts.filter((o) => o.value.toLowerCase().includes(low));
    if (!hit.length) hit = opts.filter((o) => o.textContent.toLowerCase().includes(low));
    if (!hit.length) continue;
    hit.sort((a, b) => a.value.length - b.value.length);
    sel.value = hit[0].value;
    sel.dispatchEvent(new ShimEvent("input"));
    sel.dispatchEvent(new ShimEvent("change"));
    await idle();
    return;
  }
  errors.push("select_source: no option matching " + match);
}

async function clickButton(text) {
  const b = queryAll(main, "button").find((x) => x.textContent.trim() === text);
  if (!b) { errors.push("click_button: none " + text); return; }
  b.click();
  await idle();
}

function serialize(n) {
  if (n.nodeType === 3) return n.data;
  const attrs = {};
  for (const [k, v] of n._attrs) attrs[k] = v;
  const style = n.getAttribute("style");
  if (style) attrs.style = style;
  const out = { t: n.localName, a: attrs, c: n.childNodes.map(serialize) };
  if (n.localName === "details") out.open = !!n.open;
  if (n.localName === "select") out.value = n.value;
  if (n.localName === "option") out.selected = n.selected;
  if (n.localName === "input") out.checked = n.checked;
  if (n.hasAttribute("hidden")) out.hidden = true;
  return out;
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

const snapshots = [];
try {
  await import(pathToFileURL(path.join(spec.static_dir, "app.js")).href);
  await idle();
  for (const act of spec.actions) {
    if (act.op === "click_button") await clickButton(act.text);
    else if (act.op === "expand") await expand(act.label, act.until);
    else if (act.op === "toggle_archive") await toggleArchive();
    else if (act.op === "select_source") await selectSource(act.match);
    else if (act.op === "snapshot") {
      snapshots.push({ label: act.label, tree: serialize(main), requests: requests.slice() });
    } else errors.push("unknown op " + act.op);
  }
} catch (e) {
  errors.push("harness: " + (e && e.stack || e));
}
writeFileSync(spec.out, JSON.stringify({ snapshots, errors }));
process.exit(0);
