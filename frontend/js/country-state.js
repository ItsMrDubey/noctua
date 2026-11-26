// Country context pane state (spec B §3.2): one object per pane, kept in the dossier pane spec
// (the dossier persists specs in localStorage, so it survives reloads and pane history).
// Builds every /api/events and /api/country query of the pane, plus its labels.
// Pure: no imports, no DOM, no Cesium (unit-tested under node: tests/frontend/test_country_state.py).
export const LOCATIONS = ["all", "inside", "abroad"];
export const INVOLVEMENTS = ["all", "domestic", "foreign", "unattributed"];
export const WINDOWS = ["24h", "7d", "30d", "custom"];
export const LIFECYCLES = ["developing", "active", "cooling", "closed"];
export const PAGE = 200;
const HOURS = { "24h": 24, "7d": 168, "30d": 720 };
const CC = /^[A-Z]{2}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const pick = (v, list, d) => (list.includes(v) ? v : d);
const day = (v) => (DAY.test(String(v || "")) ? String(v) : null);

export function normState(spec) {
  const s = spec || {};
  const cc = String(s.cc || "").toUpperCase();
  const win = pick(s.window, WINDOWS, "7d");
  const cp = String(s.counterpart || "").toUpperCase();
  return {
    type: "country", cc,
    location: pick(s.location, LOCATIONS, "all"),
    involvement: pick(s.involvement, INVOLVEMENTS, "all"),
    window: win,
    from: win === "custom" ? day(s.from) : null,
    to: win === "custom" ? day(s.to) : null,
    cats: Array.isArray(s.cats) ? s.cats.filter((c) => typeof c === "string") : null,
    counterpart: CC.test(cp) && cp !== cc ? cp : null,
  };
}

export const resetFilters = (st) => normState({ cc: st.cc });

// CUSTOM needs a start date; until one is picked the 7-day default applies
export function windowRange(st, now = Date.now()) {
  if (st.window === "custom" && st.from) return { since: st.from + "T00:00:00Z", until: st.to ? st.to + "T23:59:59Z" : null };
  return { since: new Date(now - (HOURS[st.window] || HOURS["7d"]) * 3600e3).toISOString(), until: null };
}

// null = no category filter; "none" = every chip off (matches no event)
function catsParam(st, allCats) {
  if (!st.cats) return null;
  if (!st.cats.length) return "none";
  if (allCats.length && allCats.every((c) => st.cats.includes(c))) return null;
  return st.cats.join(",");
}

export function eventsQuery(st, { offset = 0, limit = PAGE, now = Date.now(), allCats = [], ids = false, until = null } = {}) {
  const r = windowRange(st, now), q = new URLSearchParams();
  q.set("country", st.cc);
  q.set("all", "true");
  if (st.location !== "all") q.set("location", st.location);
  if (st.involvement !== "all") q.set("involvement", st.involvement);
  if (st.counterpart) q.set("counterpart", st.counterpart);
  q.set("since", r.since);
  // "Load more" pins until to the first page's effective until so offset paging stays stable
  const u = until || r.until;
  if (u) q.set("until", u);
  const c = catsParam(st, allCats);
  if (c) q.set("category", c);
  q.set("limit", String(limit));
  if (offset) q.set("offset", String(offset));
  if (ids) q.set("ids", "true");
  return q.toString();
}

export function summaryQuery(st, { now = Date.now(), allCats = [] } = {}) {
  const r = windowRange(st, now), q = new URLSearchParams();
  q.set("since", r.since);
  if (r.until) q.set("until", r.until);
  const c = catsParam(st, allCats);
  if (c) q.set("category", c);
  return q.toString();
}

// flip one category chip; null when every category is on again
export function toggleCat(cats, k, allCats) {
  const on = new Set(cats || allCats);
  if (on.has(k)) on.delete(k); else on.add(k);
  return allCats.every((c) => on.has(c)) ? null : allCats.filter((c) => on.has(c));
}

export const ctxLabel = (ctx) => (ctx ? (ctx.location + "·" + (ctx.involvement === "unattributed" ? "unattrib." : ctx.involvement)).toUpperCase() : "");

export const flagEmoji = (cc) => (CC.test(String(cc || "")) ? String.fromCodePoint(...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65)) : "");

// names: optional {ISO2: country name} (from the API) so user-facing text never shows a bare code
const nm = (names, cc) => (names && names[cc]) || cc;
export const pairLabel = (st, names) => (st.counterpart ? nm(names, st.cc) + " ↔ " + nm(names, st.counterpart) : "");

// append a page of events, dropping ids already present
export function mergePage(items, page) {
  const seen = new Set((items || []).map((e) => e.id));
  return (items || []).concat((page || []).filter((e) => !seen.has(e.id) && seen.add(e.id)));
}

// polyline path of daily counts across a w x h box (half-pixel inset so the stroke stays inside)
export function sparkPath(spark, w, h) {
  const v = (spark || []).map((d) => +d.n || 0);
  if (v.length < 2) return "";
  const max = Math.max(1, ...v), f = (x) => +x.toFixed(1);
  return v.map((n, i) => (i ? "L" : "M") + f((i * w) / (v.length - 1)) + "," + f(h - 0.5 - (n / max) * (h - 1))).join("");
}

export function emptyText(st, names) {
  const when = st.window === "custom" ? (st.from ? st.from + " → " + (st.to || "now") : "7D") : st.window.toUpperCase();
  return "No events for " + nm(names, st.cc) + " in " + when + " with these filters";
}

// official threat level (summary.threat) -> 0..4, the --thr-N token tier; null without a rank
export function threatTier(t) {
  if (!t || t.rank == null || !(+t.scale_max > 0)) return null;
  return Math.max(0, Math.min(4, Math.round((+t.rank / +t.scale_max) * 4)));
}

// {ISO2: name} of the summary's country and counterparts, for labels (pairLabel, actor chips)
export function summaryNames(s) {
  const out = {};
  for (const c of s ? [s, ...(s.counterparts || [])] : []) if (CC.test(String(c.cc || "")) && c.name) out[c.cc] = c.name;
  return out;
}
