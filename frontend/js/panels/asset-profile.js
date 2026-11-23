// ASSET PROFILE: who / what a tracked aircraft or vessel is, why it is listed, who owns it and
// what it has been doing (GET /api/profile/{aircraft|vessel}/{id}). Every value shows its source;
// unknowns say what was checked. Sections are ordered by the backend (what is notable for this
// asset first); sections with nothing notable collapse to a one-line summary (click to expand).
//
// Contract (used by the inspector and the Intel console tile):
//   renderAssetProfile(container, kind, id, opts) -> Promise<profile | null>
//     container  element the card is drawn into (owned by the card until replaced)
//     kind       "aircraft" | "vessel"
//     id         ICAO hex / MMSI without the "air:" / "sea:" prefix ("reg:RA-85041" and
//                "imo:9336414" also work)
//     opts       { compact?: bool  (inspector density: fewer highlights / records),
//                  trackId?, label?  (Intel console metadata, informational),
//                  onLoad?(profile)  (after every (re)load) }
//     Re-polls while third-party lookups are pending (server answers from cache only) and stops
//     when the container leaves the document.
//   assetProfileHtml(kind, id, opts) -> string   synchronous card HTML from the client cache for
//     string-rendered hosts (inspector trackExtra hook); starts / refreshes the load and calls
//     the `rerender` hook when new data arrives. opts.fallback() -> HTML shown until loaded.
//   setAssetProfileHooks({ pivot(spec), flyTo(lat, lon, alt), rerender() })
//     pivot: open an entity in the Intel pane ({type:"intel", kind:"actor", q, label});
//     without it entity names open their OpenSanctions page (in-app browser for target=_blank).
// The module also registers itself as window.__noctuaPanels.assetProfile (the bundler has no
// dynamic import(), so the Intel console picks it up from there) and fires "noctua:panels".
import { esc } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";

const TTL_MS = 60000;
const PENDING_RETRY_MS = 1500;
const MAX_PENDING_TRIES = 12;
const SEV_LABEL = { high: "HIGH", warn: "WATCH", info: "NOTE" };
const STATUS_LABEL = { ok: "ok", not_found: "not found", rate_limited: "rate-limited", pending: "loading…", error: "error", skipped: "skipped" };
const CONTEXT_LABEL = { sanctioned: "Sanctioned asset", listed: "Watchlisted", emergency: "Emergency", military: "Military", civil: "Civil", plain: "Vessel" };
const FIELD_URL = /^https:\/\//i;

const cache = new Map(); // "kind:id" -> { data, at, loading, err, tries, timer }
const open = new Set(); // "kind:id:section" expanded by the user (collapsed sections)
const closed = new Set(); // "kind:id:section" collapsed by the user (notable sections)
let hooks = { pivot: null, flyTo: null, rerender: null };

export function setAssetProfileHooks(h) { hooks = { ...hooks, ...h }; }
const rerenderHook = () => hooks.rerender && hooks.rerender(); // one subscriber for all string renders

const keyOf = (kind, id) => kind + ":" + String(id).replace(/^(air|sea|aircraft|ship|vessel):/, "");
const safeUrl = (u) => (FIELD_URL.test(u || "") ? u : null);
const ext = (u, label) => (safeUrl(u) ? '<a href="' + esc(u) + '" target="_blank" rel="noopener">' + esc(label) + "</a>" : esc(label));
const utc = (ts) => { const t = Date.parse(ts); return isNaN(t) ? esc(ts || "") : new Date(t).toISOString().replace("T", " ").slice(0, 16) + "Z"; };
const ago = (ts) => {
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago";
};

// ------------------------------------------------------------------ loading

function load(kind, id, onUpdate) {
  const k = keyOf(kind, id);
  let c = cache.get(k);
  if (!c) { c = { data: null, at: 0, loading: false, err: null, tries: 0, subs: new Set() }; cache.set(k, c); }
  if (onUpdate) c.subs.add(onUpdate);
  const fresh = c.data && Date.now() - c.at < TTL_MS && !c.data.pending;
  if (c.loading || fresh || c.timer) return c;
  c.loading = true;
  const path = "/api/profile/" + (kind === "aircraft" ? "aircraft/" : "vessel/") + encodeURIComponent(k.slice(k.indexOf(":") + 1));
  fetchJson(path, 20000).then((d) => { c.data = d; c.err = null; }, (e) => { c.err = String((e && e.message) || e); })
    .finally(() => {
      c.loading = false; c.at = Date.now();
      if (c.data && c.data.pending && c.tries < MAX_PENDING_TRIES) {
        c.tries++;
        c.timer = setTimeout(() => { c.timer = null; c.at = 0; load(kind, id); }, PENDING_RETRY_MS);
      } else if (c.data && !c.data.pending) c.tries = 0;
      for (const f of [...c.subs]) { try { f(c); } catch (err) { console.warn("asset profile", err); } }
    });
  return c;
}

export function assetProfileHtml(kind, id, opts = {}) {
  const c = load(kind, id, rerenderHook);
  if (!c.data) return c.err ? '<div class="ap ap-err">Asset profile unavailable: ' + esc(c.err) + "</div>" + (opts.fallback ? opts.fallback() : "")
    : '<div class="ap ap-loading"><i></i>Building asset profile…</div>' + (opts.fallback ? opts.fallback() : "");
  return card(kind, keyOf(kind, id), c.data, { compact: opts.compact !== false, ...opts });
}

export function renderAssetProfile(container, kind, id, opts = {}) {
  kind = kind === "ship" ? "vessel" : kind;
  const k = keyOf(kind, id);
  container.dataset.apKey = k;
  let first = true;
  return new Promise((resolve) => {
    const draw = (c) => {
      if (!container.isConnected && !first) { c.subs.delete(draw); return; }
      if (container.dataset.apKey !== k) { c.subs.delete(draw); return; }
      first = false;
      container.innerHTML = c.data ? card(kind, k, c.data, { compact: !!opts.compact, ...opts })
        : c.err ? '<div class="ap ap-err">Asset profile unavailable: ' + esc(c.err) + "</div>" : '<div class="ap ap-loading"><i></i>Building asset profile…</div>';
      container._apRedraw = () => draw(c);
      if (c.data && opts.onLoad) opts.onLoad(c.data);
      if (c.data || c.err) resolve(c.data || null);
    };
    const c = load(kind, id, draw);
    draw(c);
  });
}

// ------------------------------------------------------------------ rendering

function srcChip(p, s) {
  const src = (p.sources || {})[s] || {};
  return '<sup class="ap-src" title="' + esc((src.name || s) + (src.licence ? " · " + src.licence : "") + (src.fetched_at ? " · fetched " + src.fetched_at : "")) + '">' + esc(s) + "</sup>";
}

function fieldsHtml(p, fields) {
  if (!fields || !fields.length) return "";
  return '<div class="ap-kv">' + fields.map((f) => {
    let v = typeof f.value === "object" ? JSON.stringify(f.value) : String(f.value);
    v = /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v) ? utc(v) + ' <span class="ap-dim">(' + ago(v) + ")</span>" : esc(v);
    if (safeUrl(f.url)) v = '<a href="' + esc(f.url) + '" target="_blank" rel="noopener">' + v + "</a>";
    return "<span>" + esc(f.label) + "</span><div" + (f.value === "unknown" ? ' class="ap-unknown"' : "") + ">" + v + srcChip(p, f.source) +
      (f.note ? '<small class="ap-note">' + esc(f.note) + "</small>" : "") + "</div>";
  }).join("") + "</div>";
}

function sparkSvg(vals, label, unit) {
  const pts = (vals || []).map((v, i) => [i, v]).filter(([, v]) => v != null && !isNaN(v));
  if (pts.length < 2) return "";
  const ys = pts.map(([, v]) => v), min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1, n = vals.length - 1 || 1;
  const xy = pts.map(([i, v]) => ((i / n) * 100).toFixed(1) + "," + (20 - ((v - min) / span) * 18).toFixed(1)).join(" ");
  return '<div class="ap-spark"><span>' + esc(label) + '</span><svg viewBox="0 0 100 22" preserveAspectRatio="none"><polygon points="0,22 ' + xy + ' 100,22"/><polyline points="' + xy + '"/></svg><b>' +
    esc(Math.round(min).toLocaleString()) + "–" + esc(Math.round(max).toLocaleString()) + " " + esc(unit) + "</b></div>";
}

function tags(list, cls) { return (list || []).map((t) => '<span class="ap-tag ' + (cls || "") + '">' + esc(t) + "</span>").join(""); }

function partyHtml(p, pt, opts) {
  const sanc = pt.sanctioned;
  const pivot = '<button class="ap-link" data-ap-pivot="' + esc(pt.name || pt.id) + '" data-ap-url="' + esc(pt.url || "") + '" title="Pivot: open this entity in the Intel pane">' + esc(pt.name || pt.id) + "</button>";
  let h = '<div class="ap-party' + (sanc ? " sanc" : "") + '">' + pivot +
    (pt.schema ? ' <span class="ap-dim">' + esc(pt.schema) + "</span>" : "") +
    (pt.countries && pt.countries.length ? ' <span class="ap-cc">' + esc(pt.countries.join(", ").toUpperCase()) + "</span>" : "") +
    ' <a class="ap-os" href="' + esc(pt.url) + '" target="_blank" rel="noopener" title="OpenSanctions entity page">OpenSanctions ↗</a>';
  const risk = (pt.risk || []).filter((r) => r !== "Sanctioned");
  if (sanc || risk.length) h += '<div class="ap-tags">' + (sanc ? '<span class="ap-tag high">Sanctioned' + (pt.programs && pt.programs.length ? ": " + esc(pt.programs.slice(0, 6).join(", ")) : "") + "</span>" : "") + tags(risk, "warn") + "</div>";
  if (!opts.compact && pt.sanctions && pt.sanctions.length) h += recordsHtml(pt.sanctions.slice(0, 4), true);
  if (!opts.compact && (pt.imo_company || pt.address)) h += '<div class="ap-dim ap-small">' + esc([pt.imo_company && "IMO company " + pt.imo_company, pt.address].filter(Boolean).join(" · ")) + "</div>";
  return h + "</div>";
}

function recordsHtml(recs, small) {
  return '<ul class="ap-recs' + (small ? " small" : "") + '">' + recs.map((r) =>
    "<li><b>" + esc(r.authority || "Authority not stated") + "</b>" + (r.program_id || r.program ? ' <span class="ap-prog">' + esc(r.program_id || r.program) + "</span>" : "") +
    '<span class="ap-dates">' + esc([r.start && "since " + r.start, r.end && "until " + r.end].filter(Boolean).join(" · ") || "date not stated") + "</span>" +
    (r.reason ? '<div class="ap-reason">' + esc(r.reason) + "</div>" : "") +
    (r.program && r.program_id && r.program !== r.program_id ? '<div class="ap-dim ap-small">' + esc(r.program) + "</div>" : "") +
    ((r.source_urls || []).length || r.program_url ? '<div class="ap-srcs">' + [...(r.source_urls || []).slice(0, 2), r.program_url].filter(safeUrl).map((u, i) => ext(u, i === 0 ? "source" : "source " + (i + 1))).join(" · ") + "</div>" : "") +
    "</li>").join("") + "</ul>";
}

const RENDER = {
  identity: (p, s) => fieldsHtml(p, s.fields),
  sanctions: (p, s, o) => {
    let h = "";
    for (const e of s.entries || []) {
      const recs = o.compact ? (e.records || []).slice(0, 3) : e.records || [];
      h += '<div class="ap-entry"><div class="ap-entry-h">' + ext(e.url, e.name) + ' <span class="ap-dim">matched on ' + esc(String(e.matched_on).toUpperCase()) + "</span></div>" +
        '<div class="ap-tags">' + tags(e.risk, (e.topics || []).includes("sanction") ? "high" : "warn") + tags(e.programs, "prog") + "</div>" +
        (recs.length ? recordsHtml(recs) : "") + ((e.records || []).length > recs.length ? '<div class="ap-dim ap-small">+' + ((e.records || []).length - recs.length) + " more record(s) — open the console tile for all</div>" : "") +
        (e.narrative ? '<details class="ap-narr"' + (o.compact ? "" : " open") + "><summary>Listing narrative" + srcChip(p, "opensanctions") + "</summary><p>" + esc(e.narrative) + "</p></details>" : "") +
        '<div class="ap-srcs">' + (e.source_urls || []).slice(0, o.compact ? 2 : 6).filter(safeUrl).map((u) => ext(u, new URL(u).hostname.replace(/^www\./, ""))).join(" · ") + "</div></div>";
    }
    if ((s.lists || []).length) h += '<div class="ap-lists">' + s.lists.map((l) => '<div><span class="ap-tag ' + (["sanction", "mare.shadow"].includes(l.topic) ? "high" : "warn") + '">' + esc(l.label) + "</span> " + esc(l.datasets.join(", ")) + srcChip(p, "os_maritime") + "</div>").join("") + "</div>";
    for (const u of s.user || []) h += '<div class="ap-user">User watchlist: <b>' + esc(u.label || u.name) + "</b>" + (u.note ? " — " + esc(u.note) : "") + "</div>";
    for (const w of s.weak || []) h += '<div class="ap-weak"><b>Possible name match, unverified:</b> ' + ext(w.url, w.name) + " " + tags(w.risk, "") +
      (w.doubts && w.doubts.length ? '<ul class="ap-doubts">' + w.doubts.map((d) => "<li>" + esc(d) + "</li>").join("") + "</ul>" : "") + "</div>";
    if (!s.listed && !(s.weak || []).length) h += '<div class="ap-dim">Not listed. Checked: ' + esc((s.checked || []).join("; ")) + "</div>";
    return h;
  },
  ownership: (p, s, o) => {
    let h = "";
    for (const g of s.groups || []) {
      const roles = g.roles.map((r) => '<span class="ap-role">' + esc(r.role) + (r.start || r.end ? ' <i>' + esc([r.start, r.end].filter(Boolean).join(" → ")) + "</i>" : "") + "</span>").join("");
      h += '<div class="ap-own d' + g.depth + (g.current ? "" : " past") + '">' + (g.depth > 1 ? '<span class="ap-up">↳ parent</span>' : "") + '<div class="ap-roles">' + roles + "</div>" + partyHtml(p, g.party, o) + "</div>";
    }
    if ((s.groups || []).length) h += '<div class="ap-dim ap-small">Relationships as stated by the listing authorities' + srcChip(p, "opensanctions") + "</div>";
    h += fieldsHtml(p, s.fields);
    if (!(s.groups || []).length && !(s.fields || []).length) h += '<div class="ap-dim">Unknown. Checked: ' + esc((s.checked || []).join("; ")) + "</div>";
    return h;
  },
  live: (p, s, o) => {
    const keep = o.compact ? (f) => /Route|Airline|Destination|Last port/.test(f.label) : () => true;
    let h = fieldsHtml(p, (s.fields || []).filter(keep));
    if (s.route && s.route.caveat) h += '<div class="ap-dim ap-small">' + esc(s.route.caveat) + "</div>";
    return h || '<div class="ap-dim">' + esc(s.summary) + "</div>";
  },
  history: (p, s) => {
    const t = s.track;
    let h = "";
    if (t) {
      h += fieldsHtml(p, [{ label: "Window", value: s.window, source: "lake" }, { label: "First / last fix", value: utc(t.first) + " → " + utc(t.last), source: "lake" },
        { label: "Fixes · distance", value: t.fixes + " · " + t.distance_km.toLocaleString() + " km", source: "lake" },
        t.max_alt_ft != null ? { label: "Max altitude", value: t.max_alt_ft.toLocaleString() + " ft", source: "lake" } : null,
        t.mean_speed_kts != null ? { label: "Mean speed", value: t.mean_speed_kts + " kt", source: "lake" } : null].filter(Boolean));
      h += sparkSvg(t.spark && t.spark.alt_ft, "ALT", "ft") + sparkSvg(t.spark && t.spark.speed_kts, "SPEED", "kt");
    }
    if ((s.countries || []).length) h += '<div class="ap-countries">' + s.countries.map((c) => '<span class="ap-tag">' + esc(c.name) + "</span>").join("") + "</div>";
    if (s.seen) h += '<div class="ap-dim ap-small">Seen on ' + s.seen.days + " day(s) of the last " + s.seen.window_days + (s.seen.coverage_since ? " · our position history starts " + esc(s.seen.coverage_since) : "") + "</div>";
    for (const st of s.stops || []) h += '<div class="ap-small">Stationary ' + utc(st.from) + " → " + utc(st.to) + (st.port ? " near " + esc(st.port) + " (" + st.port_km + " km)" : " (no known port within 15 km)") + "</div>";
    return h || '<div class="ap-dim">' + esc(s.summary) + "</div>";
  },
  activity: (p, s) => {
    if (!(s.items || []).length) return '<div class="ap-dim">' + esc(s.summary) + "</div>";
    return '<ul class="ap-items">' + s.items.map((it) => '<li class="' + esc(it.severity) + '">' + esc(it.text) + srcChip(p, it.source) +
      (it.lat != null ? ' <button class="ap-fly" data-ap-fly="' + it.lat + "," + it.lon + '" title="Fly to">◎</button>' : "") + "</li>").join("") + "</ul>";
  },
};

function photoHtml(p) {
  const ph = p.sections.photo && p.sections.photo.photo;
  if (!ph || !safeUrl(ph.image) || !safeUrl(ph.link)) return "";
  // Planespotters terms: image URL unchanged, loaded by this browser; the image links to the
  // photo page (no nofollow); photographer credit visible next to it. Commons: author + licence.
  const credit = ph.source === "planespotters" ? "© " + esc(ph.credit) + ' · <a href="' + esc(ph.link) + '" target="_blank" rel="noopener">Planespotters.net</a>'
    : esc(ph.credit) + (ph.licence ? " · " + ext(ph.licence_url, ph.licence) : "") + ' · <a href="' + esc(ph.link) + '" target="_blank" rel="noopener">Wikimedia Commons</a>' + (ph.date ? " · " + esc(String(ph.date).slice(0, 10)) : "");
  return '<figure class="ap-photo"><a href="' + esc(ph.link) + '" target="_blank" rel="noopener"><img src="' + esc(ph.image) + '" alt="' + esc(ph.title || p.title) + '" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></a>' +
    "<figcaption>" + credit + (ph.title && ph.source === "commons" ? '<span class="ap-dim"> · ' + esc(ph.title) + "</span>" : "") + "</figcaption></figure>";
}

function card(kind, k, p, opts) {
  const compact = !!opts.compact;
  const hl = (p.highlights || []).slice(0, compact ? 4 : 12);
  let h = '<div class="ap ' + (compact ? "compact" : "full") + " ctx-" + esc(p.context) + '" data-ap="' + esc(k) + '">';
  h += photoHtml(p);
  h += '<div class="ap-head"><div class="ap-eyebrow">' + esc(CONTEXT_LABEL[p.context] || p.kind) + " · Asset profile" + (p.pending ? ' <span class="ap-pend">updating…</span>' : "") + "</div>" +
    (compact ? "" : '<h2 class="ap-title">' + esc(p.title) + "</h2>") + '<div class="ap-sub">' + esc(p.subtitle || "") + "</div></div>";
  if (hl.length) h += '<ul class="ap-hl">' + hl.map((x) => '<li class="' + esc(x.severity) + '"><b>' + esc(SEV_LABEL[x.severity] || "") + "</b>" + esc(x.text) + srcChip(p, x.source) + "</li>").join("") +
    ((p.highlights || []).length > hl.length ? '<li class="more">+' + ((p.highlights || []).length - hl.length) + " more</li>" : "") + "</ul>";
  for (const sid of p.order || []) {
    const s = p.sections[sid];
    if (!s || sid === "photo" || !RENDER[sid]) continue;
    const sk = k + ":" + sid;
    const expanded = (s.notable && !closed.has(sk)) || open.has(sk);
    h += '<section class="ap-sec sev-' + esc(s.severity) + (expanded ? " open" : " shut") + '" data-sec="' + esc(sid) + '"><h3 data-ap-toggle="' + esc(sk) + '" data-notable="' + (s.notable ? 1 : 0) + '">' +
      '<span class="ap-chev">' + (expanded ? "▾" : "▸") + "</span>" + esc(s.title) + (expanded ? "" : '<small class="ap-sum">' + esc(s.summary || "") + "</small>") + "</h3>" +
      (expanded ? '<div class="ap-body">' + RENDER[sid](p, s, opts) + "</div>" : "") + "</section>";
  }
  const srcs = Object.entries(p.sources || {});
  h += '<details class="ap-foot"' + (compact ? "" : " open") + "><summary>Sources & freshness <small>" + srcs.filter(([, v]) => v.status === "ok").length + "/" + srcs.length + " ok · built " + esc(ago(p.generated_at)) + "</small></summary><ul>" +
    srcs.map(([id, v]) => '<li class="st-' + esc(v.status) + '"><b>' + esc(id) + "</b> " + (safeUrl(v.url) ? ext(v.url, v.name) : esc(v.name)) + ' <span class="ap-st">' + esc(STATUS_LABEL[v.status] || v.status) + (v.stale ? " (stale)" : "") + "</span>" +
      (v.fetched_at ? ' <span class="ap-dim">' + esc(ago(v.fetched_at)) + "</span>" : "") + (v.licence ? '<div class="ap-dim ap-small">' + esc(v.licence) + (v.attribution ? " · " + esc(v.attribution) : "") + "</div>" : "") +
      (v.error ? '<div class="ap-dim ap-small">' + esc(v.error) + "</div>" : "") + "</li>").join("") + "</ul></details>";
  return h + "</div>";
}

// ------------------------------------------------------------------ interaction (delegated)

if (typeof document !== "undefined") {
  document.addEventListener("click", (e) => {
    const t = e.target.closest && e.target.closest("[data-ap-toggle],[data-ap-pivot],[data-ap-fly]");
    if (!t || !t.closest(".ap")) return;
    if (t.dataset.apToggle) {
      const sk = t.dataset.apToggle, notable = t.dataset.notable === "1", isOpen = t.parentElement.classList.contains("open");
      if (isOpen) { open.delete(sk); if (notable) closed.add(sk); } else { closed.delete(sk); if (!notable) open.add(sk); }
      const host = t.closest("[data-ap-key]");
      if (host && host._apRedraw) host._apRedraw(); else if (hooks.rerender) hooks.rerender();
    } else if (t.dataset.apPivot) {
      e.preventDefault();
      const name = t.dataset.apPivot, url = t.dataset.apUrl;
      if (hooks.pivot) hooks.pivot({ type: "intel", kind: "actor", q: name, label: name, url });
      else if (safeUrl(url)) window.open(url, "_blank", "noopener");
    } else if (t.dataset.apFly && hooks.flyTo) {
      const [lat, lon] = t.dataset.apFly.split(",").map(Number);
      hooks.flyTo(lat, lon, 2.5e5);
    }
  });
}

if (typeof window !== "undefined") {
  (window.__noctuaPanels ||= {}).assetProfile = renderAssetProfile;
  window.dispatchEvent(new Event("noctua:panels"));
}
