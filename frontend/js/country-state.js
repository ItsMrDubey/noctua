// Country context pane state (spec B §3.2): one object per pane, kept in the dossier pane spec
// (the dossier persists specs in localStorage, so it survives reloads and pane history).
// Builds every /api/events and /api/country query of the pane, plus its labels.
// Pure: no imports, no DOM, no Cesium (unit-tested under node: tests/frontend/test_country_state.py).
export const LOCATIONS = ["all", "inside", "abroad"];
export const INVOLVEMENTS = ["all", "domestic", "foreign", "unattributed"];
export const WINDOWS = ["24h", "7d", "30d", "custom"];
export const LIFECYCLES = ["developing", "active", "cooling", "closed"];
export const PAGE = 200;
export const ITEMS_MAX = 2000; // rows a pane keeps across refreshes (rows paged in by "load more" are never trimmed)
export const LIFECYCLE_RANK = { developing: 0, active: 1, cooling: 2, closed: 3 };
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

// a refresh's newest page onto the shown rows: ids not shown yet go on top (in page order),
// shown ones are replaced in place by their fresh copy (lifecycle, badges), the rest stay as they are
export function mergeTop(items, page) {
  const have = new Set((items || []).map((e) => e.id)), fresh = new Map(), top = [];
  for (const e of page || []) {
    if (fresh.has(e.id)) continue;
    fresh.set(e.id, e);
    if (!have.has(e.id)) top.push(e);
  }
  return { items: top.concat((items || []).map((e) => fresh.get(e.id) || e)), added: top.length };
}

// server order of the country list: lifecycle (developing first), then latest update, then id (both desc)
export function listOrder(a, b) {
  const ra = LIFECYCLE_RANK[a.lifecycle] ?? 4, rb = LIFECYCLE_RANK[b.lifecycle] ?? 4;
  if (ra !== rb) return ra - rb;
  const ua = String(a.updated_at || ""), ub = String(b.updated_at || "");
  if (ua !== ub) return ua < ub ? 1 : -1;
  const ia = String(a.id), ib = String(b.id);
  return ia === ib ? 0 : ia < ib ? 1 : -1;
}

const ms = (v) => { const t = Date.parse(v || ""); return Number.isFinite(t) ? t : null; };

// a refresh merged into the shown rows (mergeTop), then put back in server order, rows that
// left the window (updated before `since`, ISO) dropped and the list trimmed to `max` rows.
// added: new rows; trimmed: rows cut by `max` (the lowest in the order)
export function mergeRefresh(items, page, { since = null, max = ITEMS_MAX } = {}) {
  const m = mergeTop(items, page), lo = ms(since);
  let out = lo == null ? m.items : m.items.filter((e) => { const t = ms(e.updated_at); return t == null || t >= lo; });
  out = out.slice().sort(listOrder);
  const trimmed = Math.max(0, out.length - max);
  if (trimmed) out = out.slice(0, max);
  return { items: out, added: m.added, trimmed };
}

// delay before retrying a source after its n-th consecutive failure: 10, 20, 40, then 60 s,
// never shorter than that source's request timeout (a stalled server is not hammered)
export const BACKOFF_MS = [10000, 20000, 40000, 60000];
export function retryDelay(fails, timeoutMs = 0) {
  const n = Math.max(1, Math.floor(+fails || 1));
  return Math.max(BACKOFF_MS[Math.min(n, BACKOFF_MS.length) - 1], +timeoutMs || 0);
}

// sources of a pane due for a request: not in flight, and their next time reached (or forced,
// e.g. the pane just became visible). srcs: {kind: {busy, next}} -> [kind]
export function dueSources(srcs, now, force = false) {
  return Object.keys(srcs || {}).filter((k) => { const s = srcs[k]; return !s.busy && (force || !(now < (s.next || 0))); });
}

// fn delayed until `wait` ms passed without another call (filter clicks / date typing);
// .cancel() drops a pending call, .flush() runs it now
export function debounce(fn, wait) {
  let t = null, args = null;
  const run = () => { t = null; const a = args; args = null; fn(...a); };
  const d = (...a) => { args = a; if (t) clearTimeout(t); t = setTimeout(run, wait); };
  d.cancel = () => { if (t) clearTimeout(t); t = null; args = null; };
  d.flush = () => { if (t) { clearTimeout(t); run(); } };
  d.pending = () => t != null;
  return d;
}

// before migrate_countries has backfilled actor countries (summary.migrated false) abroad,
// involvement and counterparts are partial: the pane says so
export const UNMIGRATED_NOTE = "Actor countries not backfilled yet — abroad, involvement and counterparts are partial (run migrate_countries)";
export const migrationNote = (s) => (s && s.migrated === false ? UNMIGRATED_NOTE : "");
export const counterpartsEmpty = (s) => migrationNote(s) || "No foreign counterparts in this window";

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

// selector that finds a filter control again after a repaint (from its dataset), or null
export function controlSelector(ds) {
  const q = (v) => String(v).replace(/["\\]/g, "");
  if (!ds) return null;
  if (ds.k) return '[data-k="' + q(ds.k) + '"][data-v="' + q(ds.v) + '"]';
  if (ds.cat) return '[data-cat="' + q(ds.cat) + '"]';
  if (ds.date) return '[data-date="' + q(ds.date) + '"]';
  if (ds.cx === "uncp") return '[data-cx="uncp"]';
  if (ds.cp) return '[data-cp="' + q(ds.cp) + '"]';
  return null;
}

// ---- globe sync (country-globe.js) ----
const wrapLon = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
// camera fit box [w, s, e, n] in degrees for a country bbox (w, s, e, n), padded by `pad` of each
// span (at least 0.5 deg). w > e means the box crosses the antimeridian (RU, FJ, US, NZ): the span
// is taken eastwards across it and the padded edges are wrapped into [-180, 180], so the result
// may also have w > e, which is how Cesium.Rectangle spells "crosses the antimeridian". A padded
// span of 360 deg or more becomes the whole longitude range. null for a malformed bbox.
export function fitBox(bbox, pad = 0.08) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every((v) => typeof v === "number" && isFinite(v))) return null;
  const [w, s, e, n] = bbox;
  if (n < s || Math.abs(s) > 90 || Math.abs(n) > 90) return null;
  const spanX = e >= w ? e - w : e - w + 360;
  const px = Math.max(0.5, spanX * pad), py = Math.max(0.5, (n - s) * pad);
  const S = Math.max(-90, s - py), N = Math.min(90, n + py);
  if (spanX + 2 * px >= 360) return [-180, S, 180, N];
  let W = wrapLon(w - px), E = wrapLon(e + px);
  if (E === -180 && W !== -180) E = 180; // an east edge landing exactly on the antimeridian
  return [W, S, E, N];
}

// actor_countries as a list, whether the API sent a JSON string or an array
function actorList(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === "string" && v) { try { const a = JSON.parse(v); return Array.isArray(a) ? a : []; } catch { return []; } }
  return [];
}
// the globe's focus predicate for a country pane: true = drawn normally, false = dimmed. `ids` is
// the pane's filtered set from /api/events?ids=true. When the server capped that set (truncated),
// an event missing from it may still match, so only events that cannot be in any filtered set of
// country `cc` (located elsewhere and without `cc` among their actor countries) are dimmed; an
// event whose country fields are absent counts as unknown and stays undimmed. `fetchedAt` (ms or
// ISO): when the ids set was read; an event updated after it is unknown too (it arrived after the
// set, so it stays undimmed until the next ids refresh decides)
export function focusPredicate(ids, { truncated = false, cc = "", fetchedAt = null } = {}) {
  if (!ids) return null;
  const at = typeof fetchedAt === "number" ? fetchedAt : ms(fetchedAt);
  const fresh = (it) => at != null && (ms(it.updated_at) ?? -Infinity) > at;
  if (!truncated) return (it) => ids.has(it.id) || fresh(it);
  return (it) => {
    if (ids.has(it.id) || fresh(it)) return true;
    if (!("country" in it) && !("actor_countries" in it)) return true; // unknown
    return it.country === cc || actorList(it.actor_countries).includes(cc);
  };
}

// a polygon ring (flat [lon, lat, ...]) as the open polylines of its outline: the ring is closed
// and cut wherever an edge runs along the antimeridian (Natural Earth splits RU / FJ / ... there,
// and that seam is not a border). Each run is a flat [lon, lat, ...] array of >= 2 points.
export function outlineRuns(ring) {
  if (!Array.isArray(ring) || ring.length < 6) return [];
  const pts = [];
  for (let i = 0; i + 1 < ring.length; i += 2) pts.push([ring[i], ring[i + 1]]);
  const a = pts[0], z = pts[pts.length - 1];
  if (a[0] !== z[0] || a[1] !== z[1]) pts.push(a);
  const seam = (p, q) => Math.abs(p[0]) >= 179.9999 && Math.abs(q[0]) >= 179.9999 && Math.sign(p[0]) === Math.sign(q[0]);
  const runs = [];
  let cur = [pts[0]];
  for (let i = 1; i < pts.length; i++) {
    if (seam(pts[i - 1], pts[i])) { if (cur.length > 1) runs.push(cur); cur = [pts[i]]; } else cur.push(pts[i]);
  }
  if (cur.length > 1) runs.push(cur);
  // a closed ring cut somewhere: the last run continues into the first
  if (runs.length > 1) {
    const f = runs[0], l = runs[runs.length - 1];
    if (f[0] === pts[0] && l[l.length - 1] === pts[pts.length - 1] && !seam(pts[pts.length - 2], pts[pts.length - 1])) {
      runs[0] = l.concat(f.slice(1)); runs.pop();
    }
  }
  return runs.map((r) => r.flat());
}

// what the globe sync does on a dossier notification ("noctua:dossier" detail: {state, focused,
// specs}) while `active` (ISO2 or null) is the active country: "deactivate" when the dossier is no
// longer open (closed / minimised, e.g. by Esc) or the active country's pane is gone, "activate"
// (with cc) when a country pane has focus, otherwise "keep" (another pane focused beside it,
// an inner layer such as a zoomed pane or lightbox closed by Esc). `resume`: the country that was
// active when the dossier was minimised; restoring the dossier with another pane focused (the
// event's intel pane beside it) brings that context back while its pane is still open
export function globeAction(detail, active, resume = null) {
  const d = detail || {}, cc = (s) => String((s && s.cc) || "").toUpperCase();
  if (d.state !== "open") return active ? { op: "deactivate" } : { op: "keep" };
  if (d.focused && d.focused.type === "country" && cc(d.focused)) return { op: "activate", cc: cc(d.focused) };
  const has = (code) => (d.specs || []).some((s) => s && s.type === "country" && cc(s) === code);
  if (active && !has(active)) return { op: "deactivate" };
  if (!active && resume && has(resume)) return { op: "activate", cc: resume };
  return { op: "keep" };
}
