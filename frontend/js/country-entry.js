// Country context entry points (spec B §3.1): the pure parts of the Alt+click lookup, the one-time
// hint and the "open beside" pivot rule. No imports, no DOM, no Cesium, so it runs under node
// (tests/frontend/test_country_entry.py). country.js and browser.js wire them to the page.
export const HINT_KEY = "noctua.country.althint";
export const HINT_TEXT = "Tip: Alt + click a country on the globe to open its context";
export const OCEAN_TEXT = "No country here";
export const LOOKUP_FAIL_TEXT = "Country lookup unavailable, try again";

// true when the one-time hint is still due; storage that is missing or throws (private mode,
// blocked site data) counts as seen, so the hint never nags on every click
export function hintDue(storage) {
  try { return !!storage && storage.getItem(HINT_KEY) !== "1"; } catch (e) { return false; }
}
export function markHintSeen(storage) {
  try { if (storage) storage.setItem(HINT_KEY, "1"); return true; } catch (e) { return false; }
}

// a plain primary-button release on the globe triggers the hint (Alt+click is the gesture itself)
export const hintTrigger = (e) => !!e && e.button === 0 && !e.altKey && !e.ctrlKey && !e.shiftKey && !e.metaKey;

// one lookup at a time, and none within `ms` of the last start: a double Alt+click opens one pane
export function lookupGate(ms = 800) {
  let busy = false, last = -Infinity;
  return {
    begin(now) { if (busy || now - last < ms) return false; busy = true; last = now; return true; },
    end() { busy = false; },
    get busy() { return busy; },
  };
}

// /api/country/at query for a picked point (degrees); null for a non-finite point
export function atUrl(lat, lon) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const la = Math.max(-90, Math.min(90, lat));
  const lo = ((((lon + 180) % 360) + 360) % 360) - 180; // wrap into [-180, 180)
  return "/api/country/at?lat=" + la.toFixed(4) + "&lon=" + lo.toFixed(4);
}

// outcome of a lookup: { cc } for a country, "ocean" for 404 no_country, "fail" otherwise
export function atOutcome(status, body) {
  if (status === 200 && body && /^[A-Za-z]{2}$/.test(String(body.cc || ""))) return { cc: String(body.cc).toUpperCase(), name: body.name || "" };
  if (status === 404) return "ocean";
  return "fail";
}

// a pivot opens in a new pane with Shift / Ctrl, or when the link asks for it (data-newpane="1");
// otherwise it navigates the pane it sits in
export const wantsNewPane = (e, dataset) => !!((e && (e.shiftKey || e.ctrlKey)) || (dataset && dataset.newpane === "1"));
