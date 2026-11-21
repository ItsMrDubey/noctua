// Fetch helpers and pollers. Never throw out of a poll.
// API token (only when the server runs with OSINT_API_TOKEN): open the UI once as /?token=<token>;
// it is kept in sessionStorage (tab-scoped, gone when the tab closes), stripped from the address bar,
// sent as `Authorization: Bearer` on fetch() and as ?token= on the WebSocket.
// <img> loads cannot send headers: they use short-lived signed URLs from /api/sign (see signedUrl).
const TOKEN_KEY = "osint-token";
export const apiToken = (() => {
  try {
    const u = new URL(location.href), t = u.searchParams.get("token");
    if (t) {
      sessionStorage.setItem(TOKEN_KEY, t);
      u.searchParams.delete("token");
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    }
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch (e) { return ""; }
})();
const authHeaders = () => (apiToken ? { Authorization: "Bearer " + apiToken } : {});

// URL an <img> can load: the plain path without a token, else a signed one (5 min) minted via /api/sign.
export async function signedUrl(path) {
  if (!apiToken) return path;
  const j = await fetchJson("/api/sign?path=" + encodeURIComponent(path), 8000);
  return j.url;
}
export const cacheBust = (u) => u + (u.includes("?") ? "&" : "?") + "t=" + Date.now();
export async function fetchJson(url, timeoutMs = 15000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctl.signal, headers: { Accept: "application/json", ...authHeaders() } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

// Runs fn now and every intervalMs; skips a tick if previous still running.
export function poll(name, intervalMs, fn, onError) {
  let busy = false;
  const tick = async () => {
    if (busy) return;
    busy = true;
    try { await fn(); onError(name, null); }
    catch (e) { onError(name, e && e.name === "AbortError" ? "timeout" : String((e && e.message) || e)); }
    finally { busy = false; }
  };
  tick();
  return setInterval(tick, intervalMs);
}
