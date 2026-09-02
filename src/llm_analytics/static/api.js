// api.js — fetch wrapper. RELATIVE urls only: the app is served behind an
// nginx path prefix, so no URL here may start with "/".

export class ApiError extends Error {
  constructor(status, detail) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function getJSON(path, params) {
  let url = path; // relative — resolves under the current path prefix
  if (params) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    const s = qs.toString();
    if (s) url += "?" + s;
  }
  let res;
  try {
    res = await fetch(url, { headers: { Accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, "network error: " + e.message);
  }
  if (!res.ok) {
    let detail = res.statusText || String(res.status);
    try {
      const body = await res.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (e) { /* non-JSON error body */ }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const api = {
  health: () => getJSON("api/health"),
  filters: (params) => getJSON("api/filters", params),
  overview: (p) => getJSON("api/overview", p),
  quality: (p) => getJSON("api/quality", p),
  sessions: (p) => getJSON("api/sessions", p),
  session: (id) => getJSON("api/sessions/" + encodeURIComponent(id)),
  timeline: (id, agentId) =>
    getJSON("api/sessions/" + encodeURIComponent(id) + "/timeline", { agent_id: agentId }),
  call: (id) => getJSON("api/calls/" + encodeURIComponent(id)),
  callRaw: (id) => getJSON("api/calls/" + encodeURIComponent(id) + "/raw"),
  reconciliation: (p) => getJSON("api/reconciliation", p),
  chains: () => getJSON("api/chains"),
  chainDetail: (p) => getJSON("api/chains/detail", p),
};
