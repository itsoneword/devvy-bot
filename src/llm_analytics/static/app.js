// app.js — hash router + 45s poll refresh. All URLs relative (path-prefix safe).

import { api } from "./api.js";
import { renderOverview } from "./views/overview.js";
import { renderSession } from "./views/session.js";
import { renderAudit } from "./views/audit.js";

const POLL_MS = 45_000;

// Shared cross-view state (filters survive navigation within the page life).
const appState = {
  filters: { user_id: "", profile: "", range: "7d" }, // range: 24h|7d|30d|all
  sessionsSort: { sort: "last_ts", order: "desc" },
  sessionView: {}, // per-session UI state, keyed by session id
  auditView: { user_id: null, working_dir: null, preset: "7d", from: "", to: "", window_id: null },
};

let currentView = null; // {refresh()} returned by the active view renderer

function parseRoute() {
  const hash = location.hash.replace(/^#/, "") || "/";
  if (hash === "/" || hash === "") return { name: "overview" };
  const sessionMatch = hash.match(/^\/session\/([^/?]+)/);
  if (sessionMatch) return { name: "session", id: decodeURIComponent(sessionMatch[1]) };
  if (hash.startsWith("/audit")) return { name: "audit" };
  return { name: "overview" };
}

function markNav(routeName) {
  for (const link of document.querySelectorAll(".topnav a")) {
    link.classList.toggle("active", link.dataset.route === routeName);
  }
}

async function route() {
  const view = document.getElementById("view");
  const r = parseRoute();
  markNav(r.name === "session" ? "overview" : r.name);
  currentView = null;
  view.textContent = "";
  try {
    if (r.name === "session") {
      currentView = await renderSession(view, appState, r.id);
    } else if (r.name === "audit") {
      currentView = await renderAudit(view, appState);
    } else {
      currentView = await renderOverview(view, appState);
    }
  } catch (e) {
    // Views handle their own fetch errors; this catches render bugs/unexpected.
    view.textContent = "";
    const err = document.createElement("div");
    err.className = "error-state";
    err.textContent = "Failed to render view: " + e.message;
    view.append(err);
  }
}

let lastIndexState = null; // "files:sessions:api_calls" from the last health poll

async function refreshHealth() {
  const node = document.getElementById("health");
  try {
    const h = await api.health();
    node.textContent =
      `${h.sessions ?? 0} sessions · ${(h.api_calls ?? 0).toLocaleString("en-US")} calls · ` +
      `last scan ${h.last_scan_at ? h.last_scan_at.slice(11, 19) + "Z" : "never"}`;
    return `${h.files}:${h.sessions}:${h.api_calls}`;
  } catch (e) {
    node.textContent = "api unreachable";
    return null;
  }
}

async function poll() {
  if (document.hidden) return;
  const indexState = await refreshHealth();
  // Re-render only when the index actually changed — avoids a visible blink
  // on every poll. No dimming: the stale frame stays until the new one lands.
  if (indexState !== null && indexState === lastIndexState) return;
  if (indexState !== null) lastIndexState = indexState;
  if (currentView && typeof currentView.refresh === "function") {
    try {
      await currentView.refresh();
    } catch (e) {
      // keep previous render on poll failure
    }
  }
}

window.addEventListener("hashchange", route);
route();
refreshHealth();
setInterval(poll, POLL_MS);
