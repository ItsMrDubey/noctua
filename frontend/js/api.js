// Fetch helpers and pollers. Never throw out of a poll.
export async function fetchJson(url, timeoutMs = 15000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctl.signal, headers: { Accept: "application/json" } });
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
