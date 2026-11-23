// INTEL pane (dossier browser pane type "intel"): an all-source buildup around a selection
// from GET /api/intel. Two requests run in parallel so the pane fills progressively:
// core (assessment, timeline, sources, entities, imagery) and surroundings (related events,
// historical record, sensors, traffic, reference assets). Wikidata / Wikipedia is one
// "Background" card, not the pane. Entities, places and related events pivot the pane
// (Back / Forward work as in any dossier pane; Shift+click opens a new pane).
import { esc, nf } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";
import { eventCat, EVENT_STATUS } from "/static/js/symbology.js";
import { fmtUtc, since, kmText, mgrs, latLonText, sec, wireSections, chip, gauge, bars, copyBtn, wireCopy } from "/static/js/panels/dossier-kit.js";
import { renderTimeline, wireTimeline } from "/static/js/panels/timeline-view.js";
import { imageryHtml, wireImagery } from "/static/js/imagery.js";
import { storylineHtml } from "/static/js/storylines.js";

const CORE = "assessment,timeline,sources,entities,imagery,storyline";
const AROUND = "related,history,sensors,traffic,assets";
const KIND_LABEL = { event: "Event", fire: "Thermal anomaly", quake: "Seismic event", hazard: "Hazard", aircraft: "Aircraft", ship: "Vessel", place: "Place", facility: "Facility", point: "Point", actor: "Actor" };
const LEVEL = { known: ["KNOWN", "ok"], corroborated: ["CORROB", "cor"], unconfirmed: ["UNCONF", "unc"], caveat: ["CAVEAT", "cav"] };
const ASSET = { base: "Base", airport: "Airfield", port: "Port", nuclear: "Nuclear", chokepoint: "Chokepoint" };
const pivot = (spec) => " data-pivot=\"" + esc(JSON.stringify(spec)) + "\"";

export const intelKey = (s) => "i:" + [s.kind, s.id || "", s.q || "", s.lat != null ? (+s.lat).toFixed(3) : "", s.lon != null ? (+s.lon).toFixed(3) : ""].join("|");

function query(spec, sections) {
  const p = new URLSearchParams({ kind: spec.kind, sections });
  for (const k of ["id", "lat", "lon", "t", "q", "label", "radius_km"]) if (spec[k] != null && spec[k] !== "") p.set(k, spec[k]);
  return "/api/intel?" + p.toString();
}

const evRow = (e, extra = "") => "<li" + pivot({ type: "intel", kind: "event", id: e.id, label: e.title }) + ' data-sel="' + esc(e.id) + '" title="Pivot to this event (Shift: new pane)">' +
  '<i class="at">' + esc(eventCat(e.category)[0].split(" ")[0]) + "</i><b>" + esc(e.title || e.id) + "</b><span>" + (e.distance_km != null ? esc(kmText(e.distance_km)) + " " + esc(e.compass || "") + " · " : "") +
  esc(since(e.t_end || e.t_start)) + " ago" + extra + "</span></li>";

function assessment(A) {
  if (!A) return '<div class="dz-empty">Assessment applies to events</div>';
  if (A.error) return '<div class="dz-empty">' + esc(A.error) + "</div>";
  return '<ul class="as">' + A.items.map((x) => '<li class="lv-' + (LEVEL[x.level] || ["", ""])[1] + '"><i>' + esc((LEVEL[x.level] || [x.level])[0]) + "</i><span>" + esc(x.text) + "</span><small>" + esc(x.basis) + "</small></li>").join("") + "</ul>";
}

function related(R) {
  if (!R) return "";
  if (R.error) return '<div class="dz-empty">' + esc(R.error) + "</div>";
  const c = R.counts || {};
  let h = '<div class="ip-stats"><div><b>' + nf(c["24h"]) + "</b><span>24 h</span></div><div><b>" + nf(c["7d"]) + "</b><span>7 d</span></div><div><b>" + nf(c["30d"]) + "</b><span>30 d</span></div>" +
    '<div class="ip-spark" title="Events per day, last 30 days">' + bars(R.sparkline.days, { w: 150, h: 26 }) + "<span>30-day activity" + (R.radius_km ? " · " + nf(R.radius_km) + " km" : "") + "</span></div></div>";
  const list = (title, xs, empty) => '<div class="cx-h">' + esc(title) + " <small>" + xs.length + "</small></div>" + (xs.length ? '<ul class="cx-l ev">' + xs.map((e) => evRow(e, e.actor ? " · " + esc(e.actor) : "")).join("") + "</ul>" : '<div class="dz-empty">' + esc(empty) + "</div>");
  h += list("Nearest", R.nearest || [], "No other events in 30 days");
  if ((R.same_place || []).length) h += list("Same place", R.same_place, "");
  if ((R.thread || []).length) h += list("Same-category thread (±7 d)", R.thread, "");
  if ((R.same_actors || []).length) h += list("Same actors", R.same_actors, "");
  return h;
}

function history(H) {
  if (!H) return "";
  if (H.error) return '<div class="dz-empty">' + esc(H.error) + "</div>";
  if (!H.total) return '<div class="dz-empty">No curated historical events within ' + nf(H.radius_km) + " km</div>";
  const years = Object.entries(H.by_year);
  const max = Math.max(1, ...years.map(([, v]) => v.events));
  return '<div class="ip-hist">' + nf(H.total) + " events · " + nf(H.fatalities_best) + " fatalities (best estimate) within " + nf(H.radius_km) + " km · " + esc((H.datasets || []).join(", ")) + "</div>" +
    '<div class="ip-years">' + years.map(([y, v]) => '<div title="' + esc(y + ": " + v.events + " events, " + v.fatalities + " fatalities") + '"><i style="height:' + Math.max(4, (v.events / max) * 100) + '%"></i><span>' + esc(y.slice(2)) + "</span></div>").join("") + "</div>" +
    '<div class="ip-types">' + Object.entries(H.by_type).map(([k, n]) => chip(k.replace(/_/g, " ") + " " + n, "src")).join("") + "</div>" +
    '<ul class="cx-l">' + (H.recent || []).map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">' + esc(String(x.t_start).slice(0, 4)) + "</i><b>" + esc(x.title) + "</b><span>" + esc(kmText(x.distance_km)) + " " + esc(x.compass) + "</span></li>").join("") + "</ul>";
}

function sensors(S) {
  if (!S) return "";
  if (S.error) return '<div class="dz-empty">' + esc(S.error) + "</div>";
  const f = S.fires || { items: [] }, q = S.quakes || { items: [] }, hz = S.hazards || { items: [] };
  let h = '<div class="ip-win">Window ' + esc(fmtUtc(S.window.from)) + " → " + esc(fmtUtc(S.window.to)) + " · " + nf(S.radius_km) + " km</div>";
  h += '<div class="cx-h">FIRMS hotspots <small>' + f.count + (f.max_conflict_score != null ? " · max conflict score " + f.max_conflict_score : "") + "</small></div>";
  h += f.items.length ? '<table class="ip-t"><thead><tr><th>Time</th><th>Dist</th><th>FRP</th><th>Sat</th><th title="Fire conflict score 0-100">Conflict</th></tr></thead><tbody>' + f.items.slice(0, 25).map((x) =>
    '<tr data-fly="' + x.lat + "," + x.lon + '" title="' + esc((x.conflict_reasons || []).join(", ")) + '"><td>' + esc(fmtUtc(x.ts)) + "</td><td>" + esc(kmText(x.distance_km)) + " " + esc(x.compass) + "</td><td>" + nf(x.frp_mw, 1) + "</td><td>" + esc(x.satellite || "") + "</td><td>" +
    (x.conflict_score != null ? '<span class="cs" style="--v:' + Math.min(100, x.conflict_score) + '">' + x.conflict_score + "</span>" : "–") + "</td></tr>").join("") + "</tbody></table>" : '<div class="dz-empty">None</div>';
  h += '<div class="cx-h">Quakes <small>' + q.count + "</small></div>" + (q.items.length ? '<ul class="cx-l">' + q.items.map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">M' + nf(x.mag, 1) + "</i><b>" + esc(x.place || x.event_type || "") + "</b><span>" + esc(kmText(x.distance_km)) + " · " + esc(fmtUtc(x.ts)) + "</span></li>").join("") + "</ul>" : '<div class="dz-empty">None</div>');
  h += '<div class="cx-h">Hazard alerts (GDACS / EONET) <small>' + hz.count + "</small></div>" + (hz.items.length ? '<ul class="cx-l">' + hz.items.map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">' + esc(x.alert_level || x.source || "") + "</i><b>" + esc(x.name || x.hazard || "") + "</b><span>" + esc(kmText(x.distance_km)) + " · " + esc(fmtUtc(x.ts)) + "</span></li>").join("") + "</ul>" : '<div class="dz-empty">None</div>');
  return h;
}

function trafficRow(x, passed) {
  const flags = (x.flags || []).map((f) => chip(f.label || f.kind, "warn", f.source || "")).join("");
  return "<li" + (x.lat != null ? ' data-fly="' + x.lat + "," + x.lon + '"' : "") + ' class="' + (x.is_mil ? "mil" : "") + '"><i class="at">' + (x.kind === "aircraft" ? "AIR" : "SEA") + "</i><b>" + esc(x.name || x.id) + " " +
    esc(x.type_code || "") + (x.is_mil ? " " + chip("MIL", "mil") : "") + flags + "</b><span>" + (passed ? "closest " + esc(kmText(x.closest_km)) + " at " + esc(fmtUtc(x.ts, { time: true })) : esc(kmText(x.distance_km)) + " " + esc(x.compass || "")) + "</span></li>";
}
function traffic(T) {
  if (!T) return "";
  if (T.error) return '<div class="dz-empty">' + esc(T.error) + "</div>";
  const n = T.now, p = T.passed;
  let h = '<div class="cx-h">Now within ' + nf(T.radius_km) + " km <small>" + n.aircraft + " aircraft · " + n.ships + " ships · " + n.military + " military</small></div>";
  h += n.items.length ? '<ul class="cx-l">' + n.items.map((x) => trafficRow(x, false)).join("") + "</ul>" : '<div class="dz-empty">No live tracks</div>';
  if (p && p.window) {
    h += '<div class="cx-h">Passed near the event <small>' + esc(fmtUtc(p.window.from, { time: true })) + "–" + esc(fmtUtc(p.window.to, { time: true })) + " · " + p.items.length + " tracks · " + p.military + " military</small></div>";
    h += p.items.length ? '<ul class="cx-l">' + p.items.map((x) => trafficRow(x, true)).join("") + "</ul>" : '<div class="dz-empty">No recorded tracks in that window</div>';
  }
  return h;
}

function assets(A) {
  if (!A) return "";
  if (A.error) return '<div class="dz-empty">' + esc(A.error) + "</div>";
  let h = "";
  if (A.frontline) h += '<div class="cx-fl" data-fly="' + A.frontline.lat + "," + A.frontline.lon + '"><span>FRONT LINE</span><b>' + esc(kmText(A.frontline.distance_km)) + " " + esc(A.frontline.compass) + "</b><small>" + esc(A.frontline.source) + "</small></div>";
  h += (A.items || []).length ? '<ul class="cx-l">' + A.items.map((a) => '<li data-fly="' + a.lat + "," + a.lon + '"' + pivot({ type: "intel", kind: "facility", lat: a.lat, lon: a.lon, label: a.name }) + '><i class="at at-' + esc(a.type) + '">' + esc(ASSET[a.type] || a.type) + "</i><b>" + esc(a.name || "–") +
    "</b><span>" + esc(kmText(a.distance_km)) + " " + esc(a.compass || "") + "</span></li>").join("") + "</ul>" : '<div class="dz-empty">No reference assets within ' + nf(A.radius_km) + " km</div>";
  const w = A.webcams || { items: [] };
  if (w.items.length) h += '<div class="cx-h">Webcams <small>' + w.count + "</small></div><ul class=\"cx-l\">" + w.items.map((c) => '<li data-fly="' + c.lat + "," + c.lon + '"><i class="at">CAM</i><b>' + esc(c.name) + "</b><span>" + esc(kmText(c.distance_km)) + "</span></li>").join("") + "</ul>";
  return h;
}

function sources(S, ev) {
  if (!S) return "";
  if (S.error) return '<div class="dz-empty">' + esc(S.error) + "</div>";
  return '<div class="ip-hist">' + S.count + " reports · " + S.outlets + " outlets · " + (S.groups || []).length + " independent texts · languages " + esc((S.languages || []).join(", ") || "–") + "</div>" +
    '<div class="ip-types">' + Object.entries(S.by_kind || {}).map(([k, n]) => chip(k + " " + n, "src")).join("") + "</div>";
}

function entities(E, spec) {
  if (!E) return "";
  if (E.error) return '<div class="dz-empty">' + esc(E.error) + "</div>";
  if (!E.items.length) return '<div class="dz-empty">No named actors or places</div>';
  return '<div class="ip-ents">' + E.items.map((e) => {
    const target = e.type === "actor" ? { type: "intel", kind: "actor", q: e.name, label: e.name } : e.lat != null ? { type: "intel", kind: e.type === "site" ? "facility" : "place", lat: e.lat, lon: e.lon, label: e.name } : { type: "intel", kind: "actor", q: e.name, label: e.name };
    return '<button type="button" class="ip-ent t-' + esc(e.type) + '"' + pivot(target) + ' title="Pivot to ' + esc(e.name) + (e.basis ? " (" + esc(e.basis) + ")" : "") + '"><i>' + esc(e.type.toUpperCase()) + "</i>" + esc(e.name) + (e.count > 1 ? " <small>×" + e.count + "</small>" : "") + "</button>";
  }).join("") + "</div>";
}

// observations / points have no name of their own: use the most frequent nearby place
const OBS = new Set(["fire", "quake", "hazard", "point"]);
async function background(el, spec, subj, hooks, ents) {
  const place = ((ents && ents.items) || []).find((e) => e.type === "place");
  const q = spec.kind === "event" ? subj.place_name : spec.kind === "actor" ? spec.q : OBS.has(spec.kind) ? place && place.name : spec.label;
  try {
    let qid = spec.wikidata && /^Q\d+$/.test(spec.wikidata) ? spec.wikidata : null;
    if (!qid && q) qid = (await hooks.wdResolve({ q, lat: subj.lat ?? null, lon: subj.lon ?? null, icao: spec.icao || null, maxKm: spec.kind === "actor" ? 30 : 60 })).qid;
    if (!qid) { el.innerHTML = '<div class="dz-empty">No Wikidata match for “' + esc(q || "") + "”</div>"; return; }
    const e = await hooks.wdEntity(qid);
    const sum = e.summary && e.summary.extract;
    el.innerHTML = '<div class="ip-bg">' + (e.summary && e.summary.thumb ? '<img alt="" decoding="async" src="' + esc(e.summary.thumb) + '">' : "") + "<div><b>" + esc(e.label) + "</b><small>" + esc(e.description || "") + " · " + esc(qid) + "</small>" +
      (sum ? "<p>" + esc(sum.length > 420 ? sum.slice(0, 420) + "…" : sum) + "</p>" : "") + '<button type="button" data-qid="' + esc(qid) + '">Open Wikidata dossier</button></div></div>';
  } catch (err) {
    el.innerHTML = '<div class="dz-empty">Background unavailable: ' + esc(err.message || err) + "</div>";
  }
}

// render the pane; hooks: {head({title, sub, icon, coord}), stale(), wdResolve, wdEntity, highlight}
export async function renderIntel(spec, body, hooks) {
  const kl = KIND_LABEL[spec.kind] || spec.kind;
  hooks.head({ title: spec.label || kl, sub: "INTEL // " + kl.toUpperCase(), icon: "intel", coord: spec.lat != null ? { lat: spec.lat, lon: spec.lon, alt: 6e4 } : null });
  const slot = (n) => '<div data-slot="' + n + '"><div class="dz-empty">Loading…</div></div>';
  const isEv = spec.kind === "event";
  body.innerHTML = '<div class="dk-root ip">' + '<div data-slot="head"><div class="dz-empty">Building intel picture…</div></div>' +
    '<div class="ip-cols"><div class="ip-col">' +
    sec("ip-story", isEv ? "Storyline" : "Storylines", slot("storyline"), { meta: "linked events · event time" }) +
    (isEv ? sec("ip-assess", "Assessment", slot("assessment")) + sec("ip-imagery", "Satellite imagery", slot("imagery")) + sec("ip-timeline", "Timeline", slot("timeline"), { meta: "UTC · hover for local" }) : "") +
    sec("ip-related", spec.kind === "actor" ? "Events mentioning" : "Related events", slot("related")) +
    (isEv ? sec("ip-sources", "Sources & media", slot("sources")) : "") +
    sec("ip-entities", "Entities · pivot", slot("entities")) +
    '</div><div class="ip-col">' +
    (spec.kind !== "actor" ? sec("ip-sensors", "Sensors", slot("sensors")) + sec("ip-traffic", "Traffic (ADS-B / AIS)", slot("traffic")) + sec("ip-assets", "Reference assets", slot("assets")) + sec("ip-history", "Historical record", slot("history")) : "") +
    sec("ip-bg", "Background (Wikidata / Wikipedia)", slot("background"), { cls: "small" }) + "</div></div></div>";
  const root = body.firstElementChild;
  wireSections(root); wireCopy(root);
  const put = (n, h) => { const el = root.querySelector('[data-slot="' + n + '"]'); if (el) el.innerHTML = h; return el; };
  let subj = null;
  const head = (s) => {
    const conf = s.confidence != null ? gauge(s.confidence) : "";
    const m = s.lat != null ? mgrs(s.lat, s.lon) : null;
    put("head", '<div class="dk-class st-' + esc(String(s.status || "").replace(/[^a-z-]/g, "")) + '"><span>INTEL // ' + esc(kl.toUpperCase()) + "</span><b>" + esc((EVENT_STATUS[s.status] || s.status || "ALL-SOURCE").toUpperCase()) + '</b><span class="dk-r">BUILT ' + esc(fmtUtc(new Date().toISOString(), { time: true })) + "</span></div>" +
      '<header class="dk-head"><div class="dk-ht"><div class="dz-eyebrow">' + esc(s.category ? eventCat(s.category)[0] : kl) + '</div><h2 class="dz-title">' + esc(s.label || spec.label || "") + '</h2><div class="dz-sub">' +
      esc([s.place_name, s.country].filter(Boolean).join(", ") || (s.q ? "name match: " + s.q : "")) + "</div></div>" + conf + "</header>" +
      (s.lat != null ? '<div class="dk-loc"><div><span>POS</span><b>' + esc(latLonText(s.lat, s.lon)) + "</b>" + copyBtn(s.lat.toFixed(5) + ", " + s.lon.toFixed(5)) + "</div>" + (m ? "<div><span>MGRS</span><b>" + esc(m) + "</b>" + copyBtn(m.replace(/ /g, "")) + "</div>" : "") +
        "<div><span>RADIUS</span><b>" + nf(s.radius_km) + " km</b></div></div>" : "") +
      (isEv ? '<div class="dk-tools"><button type="button" data-sel="' + esc(spec.id) + '">Select on globe</button>' + (s.lat != null ? '<button type="button" data-fly="' + s.lat + "," + s.lon + '">Fly to</button>' : "") + "</div>" : ""));
  };
  const fill = (S) => {
    if ("assessment" in S) put("assessment", assessment(S.assessment));
    if ("imagery" in S && S.imagery) { const el = put("imagery", imageryHtml(S.imagery, subj, [])); if (el) wireImagery(el); }
    if ("timeline" in S && S.timeline) { const el = put("timeline", renderTimeline(S.timeline)); if (el) wireTimeline(el, S.timeline, { onHover: (e) => hooks.highlight && hooks.highlight(e ? [{ lat: e.lat, lon: e.lon, label: e.label }] : null) }); }
    if ("related" in S) put("related", related(S.related));
    if ("history" in S) put("history", history(S.history));
    if ("sensors" in S) put("sensors", sensors(S.sensors));
    if ("traffic" in S) put("traffic", traffic(S.traffic));
    if ("assets" in S) put("assets", assets(S.assets));
    if ("sources" in S) put("sources", sources(S.sources));
    if ("entities" in S) put("entities", entities(S.entities, spec));
    if ("storyline" in S) put("storyline", storylineHtml(S.storyline, spec.id));
  };
  const fail = (names, err) => names.split(",").forEach((n) => put(n, '<div class="dz-empty">Unavailable: ' + esc(err.message || err) + "</div>"));
  const core = fetchJson(query(spec, CORE), 20000), around = fetchJson(query(spec, AROUND), 30000);
  let first = true;
  const got = (j) => {
    if (hooks.stale()) return;
    if (first) {
      first = false; subj = j.subject;
      head(subj);
      hooks.head({ title: subj.label || spec.label || kl, sub: "INTEL // " + kl.toUpperCase() + (subj.place_name ? " · " + subj.place_name : ""), icon: "intel", coord: subj.lat != null ? { lat: subj.lat, lon: subj.lon, alt: 6e4 } : null });
    }
    const S = j.sections || {};
    if ("entities" in S) background(root.querySelector('[data-slot="background"]'), spec, subj, hooks, S.entities);
    fill(S);
  };
  await Promise.all([
    core.then(got, (e) => { if (!hooks.stale()) { fail(CORE + ",head", e); if (first) throw e; } }),
    around.then(got, (e) => { if (!hooks.stale()) fail(AROUND, e); }),
  ]);
}
