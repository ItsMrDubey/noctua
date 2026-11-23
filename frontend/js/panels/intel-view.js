// INTEL console (dossier browser pane type "intel"): an all-source fusion picture around a
// selection from GET /api/intel. Three requests run in parallel so the console fills
// progressively: core (assessment, timeline, sources, entities, imagery, storyline),
// surroundings (related events, historical record, sensors, traffic, reference assets) and
// site context (environment, overhead passes, signals, exposure, 24 h activity, country,
// attention). Wikidata / Wikipedia is one "Background" tile; its English article also feeds
// the Wikipedia pageviews in the Attention tile (GET /api/intel/pageviews, cached).
//
// Layout: a persistent header (classification bar, subject, position, a metrics strip of
// real numbers) over a responsive grid of tiles (container query on the pane: 1 column in
// a narrow dock, up to 4 when maximised). Tiles fold, go double-width or fill the console;
// fold / wide state is remembered per viewer. Tile bodies render when they scroll into view
// (data is fetched up front; long lists render 12 rows and a "show all" button).
// Keys while the console is focused: 1-9 jump to a tile, P pivot (entities), C copy a
// plain-text brief, M toggle console (maximised) / docked. Every number shown comes from a
// section payload; estimates say so in their tooltip.
import { esc, nf } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";
import { eventCat, EVENT_STATUS } from "/static/js/symbology.js";
import { fmtUtc, since, kmText, mgrs, latLonText, chip, gauge, bars, copyBtn, wireCopy, copyText, toast } from "/static/js/panels/dossier-kit.js";
import { renderTimeline, wireTimeline } from "/static/js/panels/timeline-view.js";
import { imageryHtml, wireImagery } from "/static/js/imagery.js";
import { storylineHtml } from "/static/js/storylines.js";

const CORE = "assessment,timeline,sources,entities,imagery,storyline";
const AROUND = "related,history,sensors,traffic,assets";
const CONTEXT = "environment,overhead,signals,exposure,activity,country,attention";
const KIND_LABEL = { event: "Event", fire: "Thermal anomaly", quake: "Seismic event", hazard: "Hazard", aircraft: "Aircraft", ship: "Vessel", place: "Place", facility: "Facility", point: "Point", actor: "Actor" };
const LEVEL = { known: ["KNOWN", "ok"], corroborated: ["CORROB", "cor"], unconfirmed: ["UNCONF", "unc"], caveat: ["CAVEAT", "cav"] };
const ASSET = { base: "Base", airport: "Airfield", port: "Port", nuclear: "Nuclear", chokepoint: "Chokepoint" };
const pivot = (spec) => " data-pivot=\"" + esc(JSON.stringify(spec)) + "\"";
const LAYOUT_KEY = "noctua.intel.layout.v1";
const CONSOLE_KEY = "noctua.intel.console";
const LIST_ROWS = 12;

export const intelKey = (s) => "i:" + [s.kind, s.id || "", s.q || "", s.lat != null ? (+s.lat).toFixed(3) : "", s.lon != null ? (+s.lon).toFixed(3) : ""].join("|");

function query(spec, sections) {
  const p = new URLSearchParams({ kind: spec.kind, sections });
  for (const k of ["id", "lat", "lon", "t", "q", "label", "radius_km"]) if (spec[k] != null && spec[k] !== "") p.set(k, spec[k]);
  return "/api/intel?" + p.toString();
}

// ---- small formatters ----
const empty = (t) => '<div class="dz-empty">' + esc(t) + "</div>";
const errBox = (x) => (x && x.error ? empty(x.error) : null);
const pct = (v) => (v == null ? "–" : Math.round(v) + "%");
const ts = (iso) => (iso == null ? NaN : typeof iso === "number" ? iso * 1000 : Date.parse(/Z|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z"));
// "in 3 h 12 min" / "2 h ago" relative to now
function rel(iso) {
  const t = ts(iso);
  if (isNaN(t)) return "–";
  const m = Math.round((t - Date.now()) / 60000), a = Math.abs(m);
  const txt = a < 60 ? a + " min" : a < 2880 ? Math.floor(a / 60) + " h" + (a % 60 ? " " + (a % 60) + " min" : "") : Math.round(a / 1440) + " d";
  return m >= 0 ? "in " + txt : txt + " ago";
}
// "1h 07m" / "-3h 10m" / "2d 4h" (metrics strip)
function relShort(iso) {
  const t = ts(iso);
  if (isNaN(t)) return "–";
  const m = Math.round((t - Date.now()) / 60000), a = Math.abs(m), sg = m < 0 ? "−" : "";
  return sg + (a < 60 ? a + "m" : a < 2880 ? Math.floor(a / 60) + "h " + String(a % 60).padStart(2, "0") + "m" : Math.floor(a / 1440) + "d " + Math.floor((a % 1440) / 60) + "h");
}
const isAir = (k) => k === "aircraft" || k === "air";
// "show all" for long lists: first LIST_ROWS rows, the rest behind a button
function list(rows, cls = "cx-l") {
  if (rows.length <= LIST_ROWS) return '<ul class="' + cls + '">' + rows.join("") + "</ul>";
  return '<ul class="' + cls + '">' + rows.slice(0, LIST_ROWS).join("") + '</ul><ul class="' + cls + ' ic-more" hidden>' + rows.slice(LIST_ROWS).join("") + '</ul><button type="button" class="ic-showall" data-more="1">Show all ' + rows.length + "</button>";
}

// ---- tiny charts (SVG strings; viewBox scaled by CSS) ----
function area(vals, { w = 300, h = 40, max = null, cls = "" } = {}) {
  const v = vals.map((x) => (x == null ? 0 : +x));
  if (!v.length) return "";
  const M = max ?? Math.max(1, ...v), dx = w / Math.max(1, v.length - 1);
  const pts = v.map((x, i) => (i * dx).toFixed(1) + "," + (h - (x / M) * (h - 2)).toFixed(1)).join(" ");
  return '<svg class="ic-area ' + cls + '" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true"><polygon points="0,' + h + " " + pts + " " + w + "," + h + '"/><polyline points="' + pts + '"/></svg>';
}
const spark = (vals) => (vals && vals.length ? bars(vals, { w: 60, h: 14 }) : "");

// ------------------------------------------------------------------ section renderers

function assessment(A) {
  if (!A) return empty("Assessment applies to events");
  return errBox(A) || '<ul class="as">' + A.items.map((x) => '<li class="lv-' + (LEVEL[x.level] || ["", ""])[1] + '"><i>' + esc((LEVEL[x.level] || [x.level])[0]) + "</i><span>" + esc(x.text) + "</span><small>" + esc(x.basis) + "</small></li>").join("") + "</ul>";
}

const evRow = (e, extra = "") => "<li" + pivot({ type: "intel", kind: "event", id: e.id, label: e.title }) + ' data-sel="' + esc(e.id) + '" title="Pivot to this event (Shift: new pane)">' +
  '<i class="at" style="color:' + eventCat(e.category)[1] + '">' + esc(eventCat(e.category)[0].split(" ")[0]) + "</i><b>" + esc(e.title || e.id) + "</b><span>" + (e.distance_km != null ? esc(kmText(e.distance_km)) + " " + esc(e.compass || "") + " · " : "") +
  esc(since(e.t_end || e.t_start)) + " ago" + extra + "</span></li>";

function related(R) {
  if (!R) return "";
  if (R.error) return empty(R.error);
  const c = R.counts || {};
  let h = '<div class="ip-stats"><div><b>' + nf(c["24h"]) + "</b><span>24 h</span></div><div><b>" + nf(c["7d"]) + "</b><span>7 d</span></div><div><b>" + nf(c["30d"]) + "</b><span>30 d</span></div>" +
    '<div class="ip-spark" title="Events per day, last 30 days">' + bars(R.sparkline.days, { w: 150, h: 26 }) + "<span>30-day activity" + (R.radius_km ? " · " + nf(R.radius_km) + " km" : "") + "</span></div></div>";
  const sub = (title, xs, emptyText) => '<div class="cx-h">' + esc(title) + " <small>" + xs.length + "</small></div>" + (xs.length ? list(xs.map((e) => evRow(e, e.actor ? " · " + esc(e.actor) : "")), "cx-l ev") : empty(emptyText));
  h += sub("Nearest", R.nearest || [], "No other events in 30 days");
  if ((R.same_place || []).length) h += sub("Same place", R.same_place, "");
  if ((R.thread || []).length) h += sub("Same-category thread (±7 d)", R.thread, "");
  if ((R.same_actors || []).length) h += sub("Same actors", R.same_actors, "");
  return h;
}

function history(H) {
  if (!H) return "";
  if (H.error) return empty(H.error);
  if (!H.total) return empty("No curated historical events within " + nf(H.radius_km) + " km");
  const years = Object.entries(H.by_year);
  const max = Math.max(1, ...years.map(([, v]) => v.events));
  return '<div class="ip-hist">' + nf(H.total) + " events · " + nf(H.fatalities_best) + " fatalities (best estimate) within " + nf(H.radius_km) + " km · " + esc((H.datasets || []).join(", ")) + "</div>" +
    '<div class="ip-years">' + years.map(([y, v]) => '<div title="' + esc(y + ": " + v.events + " events, " + v.fatalities + " fatalities") + '"><i style="height:' + Math.max(4, (v.events / max) * 100) + '%"></i><span>' + esc(y.slice(2)) + "</span></div>").join("") + "</div>" +
    '<div class="ip-types">' + Object.entries(H.by_type).map(([k, n]) => chip(k.replace(/_/g, " ") + " " + n, "src")).join("") + "</div>" +
    list((H.recent || []).map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">' + esc(String(x.t_start).slice(0, 4)) + "</i><b>" + esc(x.title) + "</b><span>" + esc(kmText(x.distance_km)) + " " + esc(x.compass) + "</span></li>"));
}

function sensors(S) {
  if (!S) return "";
  if (S.error) return empty(S.error);
  const f = S.fires || { items: [] }, q = S.quakes || { items: [] }, hz = S.hazards || { items: [] };
  let h = '<div class="ip-win">Window ' + esc(fmtUtc(S.window.from)) + " → " + esc(fmtUtc(S.window.to)) + " · " + nf(S.radius_km) + " km</div>";
  h += '<div class="cx-h">FIRMS hotspots <small>' + f.count + (f.max_conflict_score != null ? " · max conflict score " + f.max_conflict_score : "") + (f.max_frp_mw != null ? " · max FRP " + nf(f.max_frp_mw, 1) + " MW" : "") + "</small></div>";
  h += f.items.length ? '<div class="ic-scroll"><table class="ip-t"><thead><tr><th>Time</th><th>Dist</th><th>FRP</th><th>Sat</th><th title="Fire conflict score 0-100">Conflict</th></tr></thead><tbody>' + f.items.slice(0, 40).map((x) =>
    '<tr data-fly="' + x.lat + "," + x.lon + '" title="' + esc((x.conflict_reasons || []).join(", ")) + '"><td>' + esc(fmtUtc(x.ts)) + "</td><td>" + esc(kmText(x.distance_km)) + " " + esc(x.compass) + "</td><td>" + nf(x.frp_mw, 1) + "</td><td>" + esc(x.satellite || "") + "</td><td>" +
    (x.conflict_score != null ? '<span class="cs" style="--v:' + Math.min(100, x.conflict_score) + '">' + x.conflict_score + "</span>" : "–") + "</td></tr>").join("") + "</tbody></table></div>" : empty("None");
  h += '<div class="cx-h">Quakes <small>' + q.count + "</small></div>" + (q.items.length ? list(q.items.map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">M' + nf(x.mag, 1) + "</i><b>" + esc(x.place || x.event_type || "") + "</b><span>" + esc(kmText(x.distance_km)) + " · " + esc(fmtUtc(x.ts)) + "</span></li>")) : empty("None"));
  h += '<div class="cx-h">Hazard alerts (GDACS / EONET) <small>' + hz.count + "</small></div>" + (hz.items.length ? list(hz.items.map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">' + esc(x.alert_level || x.source || "") + "</i><b>" + esc(x.name || x.hazard || "") + "</b><span>" + esc(kmText(x.distance_km)) + " · " + esc(fmtUtc(x.ts)) + "</span></li>")) : empty("None"));
  return h;
}

function trackRow(x, how) {
  const flags = (x.flags || []).map((f) => chip(f.label || f.kind, f.severity === "info" ? "src" : "warn", f.source || "")).join("");
  const right = how === "passed" ? "closest " + esc(kmText(x.closest_km)) + " at " + esc(fmtUtc(x.ts, { time: true }))
    : how === "day" ? "closest " + esc(kmText(x.closest_km)) + " · " + esc(fmtUtc(x.last, { time: true })) + (x.max_gap_min >= 60 ? " · gap " + x.max_gap_min + " min" : "")
      : esc(kmText(x.distance_km)) + " " + esc(x.compass || "");
  return "<li" + (x.lat != null ? ' data-fly="' + x.lat + "," + x.lon + '"' : "") + ' class="' + (x.is_mil ? "mil" : "") + '"><i class="at">' + (isAir(x.kind) ? "AIR" : "SEA") + "</i><b>" + esc(x.name || x.id) + " " +
    esc(x.type_code || "") + (x.is_mil ? " " + chip("MIL", "mil") : "") + flags + "</b><span>" + right + "</span></li>";
}
function traffic(T, A) {
  let h = "";
  if (A && !A.error) {
    const s = A.series || {};
    h += '<div class="ic-kv"><div><b>' + nf(A.aircraft) + "</b><span>aircraft 24 h</span></div><div><b>" + nf(A.ships) + "</b><span>ships 24 h</span></div><div class=\"" + (A.military ? "hot" : "") + '"><b>' + nf(A.military) + "</b><span>military</span></div><div class=\"" + (A.flagged ? "hot" : "") + '"><b>' + nf(A.flagged) + "</b><span>watchlisted</span></div></div>" +
      '<div class="ic-chart" title="Distinct tracks per hour within ' + nf(A.radius_km) + ' km, last 24 h (aircraft cyan, ships blue)">' + area(s.aircraft || [], { cls: "air" }) + area(s.ship || [], { cls: "sea" }) + "<span>" + esc(fmtUtc(s.start, { time: true })) + "</span><span>now</span></div>";
    if ((A.notable || []).length) h += '<div class="cx-h">Notable, last 24 h <small>' + A.notable.length + "</small></div>" + list(A.notable.map((x) => trackRow(x, "day")));
    if ((A.gaps || []).length) h += '<div class="cx-h" title="' + esc(A.basis) + '">AIS reporting gaps ≥ 60 min <small>' + A.gaps.length + "</small></div>" + list(A.gaps.map((x) => trackRow(x, "day")));
    if ((A.watch_alerts || []).length) h += '<div class="cx-h">Watch alerts in the area <small>' + A.watch_alerts.length + "</small></div>" + list(A.watch_alerts.map((a) => '<li data-fly="' + a.lat + "," + a.lon + '"><i class="at">' + esc(a.kind) + "</i><b>" + esc(a.title) + "</b><span>" + esc(fmtUtc(a.ts, { time: true })) + "</span></li>"));
  } else if (A && A.error) h += empty("24 h history: " + A.error);
  if (T && !T.error) {
    const n = T.now, p = T.passed;
    h += '<div class="cx-h">Now within ' + nf(T.radius_km) + " km <small>" + n.aircraft + " aircraft · " + n.ships + " ships · " + n.military + " military</small></div>";
    h += n.items.length ? list(n.items.map((x) => trackRow(x, "now"))) : empty("No live tracks");
    if (p && p.window) {
      h += '<div class="cx-h">Passed near the event <small>' + esc(fmtUtc(p.window.from, { time: true })) + "–" + esc(fmtUtc(p.window.to, { time: true })) + " · " + p.items.length + " tracks · " + p.military + " military</small></div>";
      h += p.items.length ? list(p.items.map((x) => trackRow(x, "passed"))) : empty("No recorded tracks in that window");
    }
  } else if (T && T.error) h += empty(T.error);
  return h || empty("Loading…");
}

function assets(A) {
  if (!A) return "";
  if (A.error) return empty(A.error);
  let h = "";
  if (A.frontline) h += '<div class="cx-fl" data-fly="' + A.frontline.lat + "," + A.frontline.lon + '"><span>FRONT LINE</span><b>' + esc(kmText(A.frontline.distance_km)) + " " + esc(A.frontline.compass) + "</b><small>" + esc(A.frontline.source) + (A.frontline.source_datetime ? " · " + esc(fmtUtc(A.frontline.source_datetime)) : "") + "</small></div>";
  h += (A.items || []).length ? list(A.items.map((a) => '<li data-fly="' + a.lat + "," + a.lon + '"' + pivot({ type: "intel", kind: "facility", lat: a.lat, lon: a.lon, label: a.name }) + '><i class="at at-' + esc(a.type) + '">' + esc(ASSET[a.type] || a.type) + "</i><b>" + esc(a.name || "–") +
    (a.is_military ? " " + chip("MIL", "mil") : "") + "</b><span>" + esc(kmText(a.distance_km)) + " " + esc(a.compass || "") + "</span></li>")) : empty("No reference assets within " + nf(A.radius_km) + " km");
  const w = A.webcams || { items: [] };
  if (w.items.length) h += '<div class="cx-h">Webcams <small>' + w.count + "</small></div>" + list(w.items.map((c) => '<li data-fly="' + c.lat + "," + c.lon + '"><i class="at">CAM</i><b>' + esc(c.name) + "</b><span>" + esc(kmText(c.distance_km)) + "</span></li>"));
  return h;
}

function sources(S) {
  if (!S) return "";
  if (S.error) return empty(S.error);
  const langs = Object.entries(S.by_lang || {}), max = Math.max(1, ...langs.map(([, n]) => n));
  return '<div class="ic-kv"><div><b>' + nf(S.count) + "</b><span>reports</span></div><div><b>" + nf(S.outlets) + "</b><span>outlets</span></div><div><b>" + nf((S.groups || []).length) + '</b><span>independent texts</span></div><div><b>' + nf((S.languages || []).length) + "</b><span>languages</span></div></div>" +
    (S.hourly && S.hourly.length > 1 ? '<div class="ic-chart" title="Reports per hour since the first report">' + area(S.hourly, { cls: "src" }) + "<span>" + esc(fmtUtc(S.first_at)) + "</span><span>" + esc(fmtUtc(S.last_at)) + "</span></div>" : "") +
    '<div class="ic-bars">' + langs.map(([k, n]) => '<div title="' + esc(k + ": " + n + " reports") + '"><span>' + esc(k.toUpperCase()) + '</span><i style="width:' + ((n / max) * 100).toFixed(1) + '%"></i><b>' + n + "</b></div>").join("") + "</div>" +
    '<div class="ip-types">' + Object.entries(S.by_kind || {}).map(([k, n]) => chip(k + " " + n, "src")).join("") + "</div>";
}

function entities(E) {
  if (!E) return "";
  if (E.error) return empty(E.error);
  if (!E.items.length) return empty("No named actors or places");
  return '<div class="ip-ents">' + E.items.map((e) => {
    const target = e.type === "actor" ? { type: "intel", kind: "actor", q: e.name, label: e.name } : e.lat != null ? { type: "intel", kind: e.type === "site" ? "facility" : "place", lat: e.lat, lon: e.lon, label: e.name } : { type: "intel", kind: "actor", q: e.name, label: e.name };
    return '<button type="button" class="ip-ent t-' + esc(e.type) + '"' + pivot(target) + ' title="Pivot to ' + esc(e.name) + (e.basis ? " (" + esc(e.basis) + ")" : "") + '"><i>' + esc(e.type.toUpperCase()) + "</i>" + esc(e.name) + (e.count > 1 ? " <small>×" + e.count + "</small>" : "") + "</button>";
  }).join("") + "</div>";
}

// ---- local conditions ----
function lightCard(title, L) {
  if (!L) return "";
  const m = L.moon || {};
  return '<div class="ic-light ph-' + esc(L.phase.replace(/ .*/, "")) + '"><span>' + esc(title) + " · " + esc(fmtUtc(L.t)) + "</span><b>" + esc(L.phase.toUpperCase()) + "</b>" +
    "<small>sun " + nf(L.sun_elev_deg, 1) + "° el · az " + nf(L.sun_az_deg) + "°</small><small>moon " + Math.round((m.illumination || 0) * 100) + "% lit (" + esc(m.phase || "") + "), " + (m.up ? "up " + nf(m.elev_deg) + "°" : "below horizon") + "</small></div>";
}
function environment(E) {
  if (!E) return "";
  if (E.error) return empty(E.error);
  const sn = E.sun_next || {}, W = E.weather || {};
  let h = '<div class="ic-lights">' + lightCard("At event", E.light.event) + lightCard("Now", E.light.now) + "</div>";
  h += '<div class="ic-sun">' + [["sunrise", "Sunrise"], ["sunset", "Sunset"], ["civil_dawn", "Civil dawn"], ["civil_dusk", "Civil dusk"]].map(([k, l]) => "<div><span>" + l + "</span><b>" + esc(sn[k] ? fmtUtc(sn[k], { time: true }) : "–") + "</b><small>" + esc(sn[k] ? rel(sn[k]) : "") + "</small></div>").join("") + "</div>";
  if (W.error) return h + empty(W.error);
  const c = W.current || {}, ae = W.at_event;
  h += '<div class="ic-wx"><div class="ic-wx-now"><span>NOW · ' + esc(c.label || "") + "</span><b>" + nf(c.temperature_2m, 1) + "°C</b><small>cloud " + pct(c.cloud_cover) + " · wind " + nf(c.wind_speed_10m, 1) + " kn from " + nf(c.wind_direction_10m) + "° (gust " + nf(c.wind_gusts_10m, 1) + ")" +
    " · vis " + (c.visibility != null ? nf(c.visibility / 1000, 1) + " km" : "–") + " · RH " + pct(c.relative_humidity_2m) + "</small>" +
    (W.downwind ? '<small class="ic-dw" title="Smoke / fire / plume drift direction from the current 10 m wind">drift toward ' + esc(W.downwind) + ' <i style="transform:rotate(' + W.downwind_deg + 'deg)">↑</i></small>' : "") + "</div>" +
    (ae ? '<div class="ic-wx-ev"><span>AT EVENT HOUR · ' + esc(fmtUtc(ae.time)) + "</span><small>cloud " + pct(ae.cloud_cover) + " (low " + pct(ae.cloud_cover_low) + ") · wind " + nf(ae.wind_speed_10m, 1) + " kn from " + nf(ae.wind_direction_10m) + "° · precip " + nf(ae.precipitation, 1) + " mm · vis " + (ae.visibility != null ? nf(ae.visibility / 1000, 1) + " km" : "–") + "</small></div>" : "") + "</div>";
  const s = W.series || { time: [] };
  if (s.time.length > 2) {
    const nowI = s.time.findIndex((t) => ts(t) > Date.now());
    h += '<div class="ic-chart tall" title="Cloud cover % (area) and wind kn (line), past 24 h → next 48 h">' + area(s.cloud_cover, { max: 100, cls: "cloud" }) + area(s.wind_speed_10m, { cls: "wind" }) +
      (nowI > 0 ? '<i class="ic-now" style="left:' + ((nowI / (s.time.length - 1)) * 100).toFixed(1) + '%"></i>' : "") + "<span>" + esc(fmtUtc(s.time[0])) + "</span><span>" + esc(fmtUtc(s.time[s.time.length - 1])) + "</span></div>";
    const n = W.next48 || {};
    h += '<div class="ip-hist">Next 48 h: cloud ' + pct(n.cloud_min) + "–" + pct(n.cloud_max) + " (mean " + pct(n.cloud_mean) + ") · precip " + nf(n.precip_mm, 1) + " mm · max wind " + nf(n.wind_max_kn, 1) + " kn</div>";
  }
  return h + '<div class="ic-basis">' + esc(W.source || "") + " · fetched " + esc(fmtUtc(W.fetched_at, { time: true })) + " · grid " + esc((W.grid || []).join(", ")) + " · " + esc(E.basis) + "</div>";
}

// ---- overhead passes ----
function passRow(p) {
  const geom = p.kind === "optical" ? "off-nadir " + nf(p.off_nadir_deg, 1) + "°" : "incidence " + nf(p.incidence_deg, 1) + "° (right)";
  return '<tr class="k-' + p.kind + (ts(p.t) < Date.now() ? " past" : "") + '" title="' + esc(p.mission + " " + p.sensor + " · " + p.res + " · " + p.node + " · " + nf(p.cross_km, 1) + " km from ground track · sun " + nf(p.sun_elev_deg, 1) + "° · elements " + nf(p.element_age_h, 1) + " h old · archive: " + p.archive) + '">' +
    "<td>" + esc(fmtUtc(p.t)) + '<small> ±' + (p.time_unc_s || 1) + " s</small></td><td>" + esc(p.satellite) + "</td><td>" + (p.kind === "optical" ? "OPT" : "SAR") + "</td><td>" + esc(geom) + "</td><td>" + (p.kind === "optical" ? pct(p.cloud_pct) : "n/a") + "</td><td>" + esc(rel(p.t)) + "</td></tr>";
}
function passCard(label, p, note = "") {
  if (!p) return '<div class="ic-pass none"><span>' + esc(label) + "</span><b>none in window</b></div>";
  return '<div class="ic-pass k-' + p.kind + '"><span>' + esc(label) + "</span><b>" + esc(fmtUtc(p.t)) + "</b><small>" + esc(p.satellite) + " · " + esc(rel(p.t)) + (p.kind === "optical" ? " · cloud " + pct(p.cloud_pct) : "") + note + "</small></div>";
}
function overhead(O) {
  if (!O) return "";
  if (O.error) return empty(O.error);
  const op = O.by_kind.optical || {}, sar = O.by_kind.sar || {};
  const t0 = ts(O.window.from), t1 = ts(O.window.to), X = (t) => (((ts(t) - t0) / (t1 - t0)) * 100).toFixed(2) + "%";
  let h = '<div class="ic-passes">' + passCard("Next optical", op.next) + passCard("Next clear optical (<30% cloud)", op.next_clear) + passCard("Next SAR", sar.next);
  if (O.event_t) h += passCard("First optical after event", op.after_event) + passCard("First SAR after event", sar.after_event) + passCard("Last optical before", op.before_event);
  h += "</div>";
  h += '<div class="ic-strip" title="Passes over the site: optical (top), SAR (bottom); opacity = forecast clear sky">' +
    O.passes.map((p) => '<i class="k-' + p.kind + '" style="left:' + X(p.t) + ";opacity:" + (p.kind === "optical" && p.cloud_pct != null ? (1 - p.cloud_pct / 130).toFixed(2) : 1) + '" title="' + esc(fmtUtc(p.t) + " " + p.satellite + (p.kind === "optical" ? " · cloud " + pct(p.cloud_pct) : "")) + '"></i>').join("") +
    '<b class="now" style="left:' + X(new Date().toISOString()) + '"><em>NOW</em></b>' + (O.event_t ? '<b class="ev" style="left:' + X(O.event_t) + '"><em>EVENT</em></b>' : "") +
    "<span>" + esc(fmtUtc(O.window.from)) + "</span><span>" + esc(fmtUtc(O.window.to)) + "</span></div>";
  h += O.passes.length ? '<div class="ic-scroll"><table class="ip-t ic-pt"><thead><tr><th>Time (UTC)</th><th>Satellite</th><th>Type</th><th>Geometry</th><th>Cloud</th><th></th></tr></thead><tbody>' + O.passes.map(passRow).join("") + "</tbody></table></div>" : empty("No covering passes in the window");
  return h + '<div class="ic-basis">' + esc(O.basis) + " Elements up to " + nf(O.max_element_age_h, 1) + " h old.</div>";
}

// ---- signals at the site ----
function signals(G) {
  if (!G) return "";
  if (G.error) return empty(G.error);
  const g = G.gnss || {}, o = G.outage, r = G.radiation || {}, s = G.space || {};
  const cell = g.site, worst = g.worst_150km;
  let h = '<div class="ic-sig">';
  h += '<div class="' + (cell && cell.level !== "low" ? "lv-" + cell.level : "") + '" title="' + esc(g.basis || "") + '"><span>GNSS AT SITE</span><b>' + (cell ? pct(cell.pct) + " degraded" : "no data") + "</b><small>" + (cell ? esc(cell.level) + " · " + cell.bad + "/" + cell.n + " aircraft · cell " + esc(kmText(cell.distance_km)) + " " + esc(cell.compass) : "no ADS-B integrity reports within 120 km in the last " + nf(g.window_min) + " min") + "</small></div>";
  h += '<div class="' + (worst && worst.level !== "low" ? "lv-" + worst.level : "") + '"><span>WORST ≤150 KM</span><b>' + (worst ? pct(worst.pct) : "–") + "</b><small>" + (worst ? esc(worst.level) + " · " + esc(kmText(worst.distance_km)) + " " + esc(worst.compass) + " · " + worst.n + " aircraft" : "no cells") + "</small></div>";
  if (o) {
    const c = o.country;
    h += '<div class="' + (c && c.ongoing ? "lv-" + (c.level || "medium") : "") + '" title="' + esc(o.basis) + '"><span>INTERNET · ' + esc(o.cc) + "</span><b>" + (c ? (c.ongoing ? "OUTAGE " : "recent ") + esc((c.level || "").toUpperCase()) : "no outage") + "</b><small>" + (c ? esc((c.datasources || []).join(", ")) + " · score " + nf(c.score) + " · since " + esc(fmtUtc(c.from)) : "IODA, last " + nf(o.window_h) + " h") + "</small></div>";
  }
  const kp = s.kp_now ? s.kp_now[1] : null, sc = s.scales || {};
  h += '<div class="' + (kp >= 5 ? "lv-high" : kp >= 4 ? "lv-medium" : "") + '" title="NOAA SWPC; Kp ≥ 5 = geomagnetic storm (GNSS / HF degradation at high latitudes)"><span>SPACE WEATHER</span><b>Kp ' + nf(kp, 1) + "</b><small>G" + (sc.G ?? "–") + " S" + (sc.S ?? "–") + " R" + (sc.R ?? "–") + (s.solar_wind ? " · wind " + nf(s.solar_wind.speed_kms) + " km/s · Bz " + nf(s.solar_wind.bz_nt) + " nT" : "") + "</small></div>";
  h += "</div>";
  h += '<div class="cx-h" title="' + esc(r.basis || "") + '">Radiation probes ≤250 km <small>' + nf(r.count_250km) + "</small></div>" + ((r.stations || []).length ? list(r.stations.map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">' + esc(x.status || "") + "</i><b>" + esc(x.name || x.id) + "</b><span>" + nf(x.value, 3) + " " + esc(x.unit || "") + (x.baseline != null ? " (median " + nf(x.baseline, 3) + ")" : "") + " · " + esc(kmText(x.distance_km)) + "</span></li>")) : empty("No public probes nearby (BfS covers Germany; Safecast is sparse)"));
  const sq = (G.squawks || {}).items || [];
  if (sq.length) h += '<div class="cx-h">Emergency squawks ≤300 km, 24 h <small>' + sq.length + "</small></div>" + list(sq.map((x) => '<li data-fly="' + x.lat + "," + x.lon + '"><i class="at">' + esc(x.code) + "</i><b>" + esc((x.flight || x.reg || "") + " " + (x.type || "")) + "</b><span>" + esc(x.label) + " · " + esc(kmText(x.distance_km)) + "</span></li>"));
  if ((g.cells || []).length) h += '<div class="cx-h">GNSS cells ≤300 km <small>' + g.cells.length + "</small></div>" + list(g.cells.map((c) => '<li data-fly="' + c.lat + "," + c.lon + '" class="' + (c.level === "high" ? "mil" : "") + '"><i class="at">' + esc(c.level) + "</i><b>" + pct(c.pct) + " degraded · " + c.bad + "/" + c.n + " aircraft</b><span>" + esc(kmText(c.distance_km)) + " " + esc(c.compass) + "</span></li>"));
  return h;
}

// ---- infrastructure + population ----
const FUEL_C = { nuclear: "#ffd24a", coal: "#9a8f86", gas: "#5ee3ea", oil: "#d9b27c", hydro: "#7fa8ff", wind: "#9ff3ff", solar: "#f2ad3d", other: "#a99bff" };
function exposure(X) {
  if (!X) return "";
  if (X.error) return empty(X.error);
  const P = X.power || { items: [] }, pop = X.population;
  let h = "";
  if (pop) {
    const r = pop.rings || {};
    h += '<div class="ic-kv" title="' + esc(pop.basis) + '">' + ["10", "25", "50"].map((k) => "<div><b>" + nf(r[k]) + "</b><span>people ≤" + k + " km</span></div>").join("") + "<div><b>" + nf(pop.places) + "</b><span>settlements" + (pop.folded_districts ? " (+" + pop.folded_districts + " districts)" : "") + "</span></div></div>";
  }
  const fuels = Object.entries(P.mw_by_fuel || {}), tot = Math.max(1, P.mw_total || 0);
  h += '<div class="cx-h">Power ≤' + nf(X.radius_km) + " km <small>" + nf(P.count) + " plants · " + nf(P.mw_total) + " MW</small></div>" +
    (fuels.length ? '<div class="ic-stack">' + fuels.map(([f, mw]) => '<i style="flex:' + (mw / tot).toFixed(3) + ";background:" + (FUEL_C[f] || FUEL_C.other) + '" title="' + esc(f + ": " + nf(mw) + " MW") + '"></i>').join("") + "</div>" : "") +
    (P.items.length ? list(P.items.map((p) => '<li data-fly="' + p.lat + "," + p.lon + '"><i class="at" style="color:' + (FUEL_C[p.group] || FUEL_C.other) + '">' + esc(p.fuel) + "</i><b>" + esc(p.name) + "</b><span>" + nf(p.mw) + " MW · " + esc(kmText(p.distance_km)) + " " + esc(p.compass) + "</span></li>")) : "");
  const pts = [["refineries", "Refinery"], ["lng", "LNG"], ["dams", "Dam"], ["ground_stations", "Ground stn"], ["landings", "Cable landing"]];
  const rows = [];
  for (const [k, l] of pts) for (const it of (X[k] || {}).items || []) rows.push('<li data-fly="' + it.lat + "," + it.lon + '"><i class="at">' + esc(l) + "</i><b>" + esc(it.name || "–") + "</b><span>" + esc(kmText(it.distance_km)) + " " + esc(it.compass) + "</span></li>");
  for (const p of (X.pipelines || {}).items || []) rows.push("<li><i class=\"at\">Pipeline</i><b>" + esc(p.name || "unnamed " + (p.substance || "")) + (p.offshore ? " " + chip("offshore", "src") : "") + "</b><span>" + esc(p.substance || "") + " · " + esc(kmText(p.distance_km)) + "</span></li>");
  for (const c of (X.cables || {}).items || []) rows.push("<li><i class=\"at\">Cable</i><b>" + esc(c.name) + "</b><span>" + esc(kmText(c.distance_km)) + "</span></li>");
  for (const a of (X.airfields || {}).items || []) rows.push('<li data-fly="' + a.lat + "," + a.lon + '" class="' + (a.is_military ? "mil" : "") + '"><i class="at">' + esc(a.type.replace(/_/g, " ")) + "</i><b>" + esc(a.name) + (a.is_military ? " " + chip("MIL", "mil") : "") + "</b><span>" + esc(kmText(a.distance_km)) + " " + esc(a.compass) + "</span></li>");
  const cnt = (k) => ((X[k] || {}).count || 0);
  h += '<div class="cx-h">Critical infrastructure ≤' + nf(X.radius_km) + " km <small>" + [["refineries", "ref"], ["lng", "LNG"], ["dams", "dam"], ["pipelines", "pipe"], ["cables", "cable"], ["landings", "landing"], ["airfields", "airfield"]].map(([k, l]) => cnt(k) + " " + l).join(" · ") + "</small></div>" +
    (rows.length ? list(rows) : empty("None in the loaded datasets"));
  if (pop && pop.items.length) h += '<div class="cx-h">Largest settlements <small>GeoNames</small></div>' + list(pop.items.map((p) => '<li data-fly="' + p.lat + "," + p.lon + '"' + pivot({ type: "intel", kind: "place", lat: p.lat, lon: p.lon, label: p.name }) + '><i class="at">' + nf(p.pop) + "</i><b>" + esc(p.name) + "</b><span>" + esc(kmText(p.distance_km)) + " " + esc(p.compass) + "</span></li>"));
  return h + (pop ? '<div class="ic-basis">' + esc(pop.basis) + "</div>" : "");
}

// ---- country context ----
function country(C) {
  if (!C) return "";
  if (C.error) return empty(C.error);
  const H = C.history || {}, T = C.tempo || {};
  const months = Object.entries(H.months || {}), max = Math.max(1, ...months.map(([, v]) => v.events));
  let h = '<div class="ic-kv"><div><b>' + nf(T.last7) + "</b><span>events, last 7 d</span></div><div class=\"" + (T.ratio >= 2 ? "hot" : "") + '" title="' + esc(T.basis || "") + '"><b>' + (T.ratio != null ? "×" + nf(T.ratio, 2) : "–") + "</b><span>vs baseline" + (T.ratio == null ? " (platform data since " + esc(T.first_day || "–") + ")" : "") + "</span></div><div><b>" + nf(H.total) + "</b><span>UCDP events " + esc(months.length ? months[0][0].slice(0, 4) : "") + "</span></div><div><b>" + nf(H.fatalities_best) + "</b><span>UCDP fatalities</span></div></div>";
  h += '<div class="ic-chart" title="Platform events in ' + esc(C.name || C.cc) + ' per day, last 37 days">' + area(T.days || [], { cls: "tempo" }) + "<span>−37 d</span><span>today</span></div>";
  const cats = Object.entries(T.categories_7d || {});
  if (cats.length) h += '<div class="ip-types">' + cats.map(([k, n]) => chip(eventCat(k)[0] + " " + n, "src")).join("") + "</div>";
  if (months.length) {
    h += '<div class="cx-h" title="' + esc(H.basis || "") + '">UCDP GED by month <small>' + esc((H.names || []).join(" / ")) + "</small></div>" +
      '<div class="ip-years">' + months.map(([m, v]) => '<div title="' + esc(m + ": " + v.events + " events, " + v.fatalities + " fatalities (best)") + '"><i style="height:' + Math.max(4, (v.events / max) * 100) + '%"></i><span>' + esc(m.slice(5)) + "</span></div>").join("") + "</div>" +
      '<div class="ip-types">' + Object.entries(H.by_type || {}).map(([k, n]) => chip(k + " " + n, "src")).join("") + "</div>";
  } else h += empty("No curated UCDP events for " + (C.name || C.cc));
  if ((C.storylines || []).length) h += '<div class="cx-h">Active storylines in the theatre (≤400 km, 7 d) <small>' + C.storylines.length + "</small></div>" +
    list(C.storylines.map((s) => "<li" + pivot({ type: "story", id: s.id, label: s.title }) + ' title="Open the storyline"><i class="at">' + s.n_events + "</i><b>" + esc(s.title) + "</b><span>" + esc((s.theatre_label || "") + " · " + since(s.t_last) + " ago") + "</span></li>"));
  return h;
}

// ---- attention ----
function attention(N, PV) {
  let h = "";
  if (N && !N.error) {
    h += '<div class="ic-kv"><div><b>' + nf(N.last3_per_day, 1) + "</b><span>reports/day, last 3 d</span></div><div><b>" + nf(N.documents.reduce((a, b) => a + b, 0)) + "</b><span>reports, 30 d ≤" + nf(N.radius_km) + " km</span></div></div>" +
      '<div class="ic-chart" title="' + esc(N.basis) + '">' + area(N.documents, { cls: "src" }) + "<span>" + esc(N.days[0]) + "</span><span>" + esc(N.days[N.days.length - 1]) + "</span></div>";
  } else if (N && N.error) h += empty(N.error);
  if (PV === undefined) h += '<div class="cx-h">Wikipedia pageviews</div>' + empty("Waiting for the Background article…");
  else if (PV && PV.error) h += '<div class="cx-h">Wikipedia pageviews</div>' + empty(PV.error);
  else if (PV && PV.missing) h += '<div class="cx-h">Wikipedia pageviews</div>' + empty("No pageview data for " + PV.title);
  else if (PV) {
    h += '<div class="cx-h">Wikipedia pageviews · ' + esc(PV.title) + ' <small>' + esc(PV.project) + "</small></div>" +
      '<div class="ic-kv"><div class="' + (PV.ratio >= 2 ? "hot" : "") + '"><b>' + (PV.ratio != null ? "×" + nf(PV.ratio, 2) : "–") + "</b><span>last 3 d vs 60-d median</span></div><div><b>" + nf(PV.last3_per_day) + "</b><span>views/day, last 3 d</span></div><div><b>" + nf(PV.baseline_median) + "</b><span>median/day</span></div><div><b>" + nf(PV.peak) + "</b><span>peak day</span></div></div>" +
      '<div class="ic-chart" title="Daily user pageviews (Wikimedia REST; one-day lag)">' + area(PV.views, { cls: "pv" }) + "<span>" + esc(PV.days[0]) + "</span><span>" + esc(PV.days[PV.days.length - 1]) + "</span></div>" +
      '<div class="ic-basis">' + esc(PV.source) + " · fetched " + esc(fmtUtc(PV.fetched_at, { time: true })) + "</div>";
  } else if (PV === null) h += '<div class="cx-h">Wikipedia pageviews</div>' + empty("No English Wikipedia article for this subject");
  return h;
}

// ---- locator: local azimuthal-equidistant plot of everything with a position ----
const LOC_KM = 50;
function locator(subj, D) {
  if (!subj || subj.lat == null) return empty("No position");
  const W = 320, R = 150, k = Math.cos((subj.lat * Math.PI) / 180);
  const P = (la, lo) => { const x = ((lo - subj.lon) * 111.32 * k) / LOC_KM, y = ((la - subj.lat) * 110.57) / LOC_KM; return [W / 2 + x * R, W / 2 - y * R, Math.hypot(x, y)]; };
  const g = [];
  const dot = (la, lo, cls, title, extra = "", r = 3) => { if (la == null) return; const [x, y, d] = P(la, lo); if (d > 1.02) return; g.push('<circle class="' + cls + '" cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + r + '" data-fly="' + la + "," + lo + '"' + extra + "><title>" + esc(title) + "</title></circle>"); };
  for (const kmr of [10, 25, 50]) g.push('<circle class="ring" cx="' + W / 2 + '" cy="' + W / 2 + '" r="' + ((kmr / LOC_KM) * R).toFixed(1) + '"/><text class="rl" x="' + (W / 2 + 3) + '" y="' + (W / 2 - (kmr / LOC_KM) * R + 9).toFixed(1) + '">' + kmr + " km</text>");
  g.push('<path class="ax" d="M' + W / 2 + " 6V" + (W - 6) + "M6 " + W / 2 + "H" + (W - 6) + '"/><text class="rl n" x="' + (W / 2 + 3) + '" y="12">N</text>');
  const pop = (D.exposure || {}).population;
  const boxes = []; // placed town labels (largest first): a label that would overlap one is dropped
  for (const p of (pop && pop.items) || []) {
    const [x, y, d] = P(p.lat, p.lon);
    if (d > 1) continue;
    g.push('<circle class="town" cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + Math.min(9, 1.5 + Math.sqrt(p.pop) / 120).toFixed(1) + '"><title>' + esc(p.name + " · pop " + nf(p.pop)) + "</title></circle>");
    const bx = [x + 5, y - 6, x + 5 + p.name.length * 4.6, y + 4];
    if (boxes.some((b) => bx[0] < b[2] && bx[2] > b[0] && bx[1] < b[3] && bx[3] > b[1])) continue;
    boxes.push(bx);
    g.push('<text class="tl" x="' + (x + 5).toFixed(1) + '" y="' + (y + 3).toFixed(1) + '">' + esc(p.name) + "</text>");
  }
  for (const a of (D.assets || {}).items || []) dot(a.lat, a.lon, "as as-" + a.type, (ASSET[a.type] || a.type) + ": " + a.name, "", 3.5);
  const X = D.exposure || {};
  for (const p of (X.power || {}).items || []) dot(p.lat, p.lon, "inf pw", "Power " + p.fuel + " " + nf(p.mw) + " MW: " + p.name, ' style="fill:' + (FUEL_C[p.group] || FUEL_C.other) + '"', 2.6 + Math.min(4, Math.sqrt(p.mw || 0) / 12));
  for (const key of ["refineries", "lng", "dams", "landings", "ground_stations"]) for (const p of (X[key] || {}).items || []) dot(p.lat, p.lon, "inf", key + ": " + (p.name || ""), "", 2.6);
  for (const f of ((D.sensors || {}).fires || {}).items || []) dot(f.lat, f.lon, "fire", "FIRMS " + fmtUtc(f.ts) + " FRP " + nf(f.frp_mw, 1) + " MW", "", 2.2);
  for (const e of (D.related || {}).nearest || []) dot(e.lat, e.lon, "ev", e.title, ' style="fill:' + eventCat(e.category)[1] + '"', 3);
  for (const c of ((D.signals || {}).gnss || {}).cells || []) dot(c.lat, c.lon, "gn lv-" + c.level, "GNSS cell " + c.level + " " + pct(c.pct), "", 5);
  const fl = (D.assets || {}).frontline;
  if (fl) {
    const [x, y, d] = P(fl.lat, fl.lon);
    if (d <= 1) g.push('<circle class="fl" cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="4" data-fly="' + fl.lat + "," + fl.lon + '"><title>Nearest front line point ' + esc(kmText(fl.distance_km)) + "</title></circle>");
    else { const a = Math.atan2(y - W / 2, x - W / 2); g.push('<path class="fl" d="M' + (W / 2 + Math.cos(a) * (R + 2)).toFixed(1) + " " + (W / 2 + Math.sin(a) * (R + 2)).toFixed(1) + "l" + (Math.cos(a + 2.6) * 8).toFixed(1) + " " + (Math.sin(a + 2.6) * 8).toFixed(1) + "M" + (W / 2 + Math.cos(a) * (R + 2)).toFixed(1) + " " + (W / 2 + Math.sin(a) * (R + 2)).toFixed(1) + "l" + (Math.cos(a - 2.6) * 8).toFixed(1) + " " + (Math.sin(a - 2.6) * 8).toFixed(1) + '"><title>Front line ' + esc(kmText(fl.distance_km)) + " " + esc(fl.compass) + "</title></path>"); }
  }
  const wx = ((D.environment || {}).weather || {});
  if (wx.downwind_deg != null) { const a = ((wx.downwind_deg - 90) * Math.PI) / 180, L = 26; g.push('<g class="wind"><path d="M' + W / 2 + " " + W / 2 + "l" + (Math.cos(a) * L).toFixed(1) + " " + (Math.sin(a) * L).toFixed(1) + '"/><title>Wind drift toward ' + esc(wx.downwind) + " (" + nf((wx.current || {}).wind_speed_10m, 1) + " kn)</title></g>"); }
  g.push('<path class="me" d="M' + W / 2 + " " + (W / 2 - 7) + "L" + (W / 2 + 6) + " " + (W / 2 + 5) + "L" + (W / 2 - 6) + " " + (W / 2 + 5) + 'Z"><title>Subject</title></path>');
  return '<svg class="ic-loc" viewBox="0 0 ' + W + " " + W + '" role="img" aria-label="Locator">' + g.join("") + "</svg>" +
    '<div class="ic-legend"><i class="ev"></i>events <i class="fire"></i>FIRMS <i class="as"></i>assets <i class="inf"></i>infrastructure <i class="town"></i>settlements <i class="fl"></i>front line <i class="gn"></i>GNSS cell</div>';
}

// ---- background (Wikidata) ----
const OBS = new Set(["fire", "quake", "hazard", "point"]);
async function background(spec, subj, hooks, ents) {
  const place = ((ents && ents.items) || []).find((e) => e.type === "place");
  const q = spec.kind === "event" ? subj.place_name : spec.kind === "actor" ? spec.q : OBS.has(spec.kind) ? place && place.name : spec.label;
  let qid = spec.wikidata && /^Q\d+$/.test(spec.wikidata) ? spec.wikidata : null;
  if (!qid && q) qid = (await hooks.wdResolve({ q, lat: subj.lat ?? null, lon: subj.lon ?? null, icao: spec.icao || null, maxKm: spec.kind === "actor" ? 30 : 60 })).qid;
  if (!qid) return { html: empty("No Wikidata match for “" + (q || "") + "”"), enwiki: null };
  const e = await hooks.wdEntity(qid);
  const sum = e.summary && e.summary.extract;
  return { enwiki: e.enwiki || null, html: '<div class="ip-bg">' + (e.summary && e.summary.thumb ? '<img alt="" decoding="async" src="' + esc(e.summary.thumb) + '">' : "") + "<div><b>" + esc(e.label) + "</b><small>" + esc(e.description || "") + " · " + esc(qid) + "</small>" +
    (sum ? "<p>" + esc(sum.length > 520 ? sum.slice(0, 520) + "…" : sum) + "</p>" : "") + '<button type="button" data-qid="' + esc(qid) + '">Open Wikidata dossier</button></div></div>' };
}

// ------------------------------------------------------------------ metrics strip

function metrics(subj, D, isEv) {
  const S = D.sources, se = D.sensors, as = D.assets, A = D.activity, G = D.signals, O = D.overhead, E = D.environment, X = D.exposure, C = D.country;
  const cells = [];
  const m = (key, label, val, sub, title, tone = "") => cells.push('<div class="ic-m ' + tone + '" data-m="' + key + '" title="' + esc(title) + '"><span>' + esc(label) + "</span><b>" + val + "</b><small>" + sub + "</small></div>");
  const wait = (x) => x === undefined;
  const nd = '<i class="ld">…</i>';
  if (isEv) {
    m("rep", "Reports", S ? nf(S.count) : nd, S ? nf(S.outlets) + " outlets · " + nf((S.languages || []).length) + " lang" : "", "Documents attached to the event (sources section)");
    m("first", "First report", S && S.first_at ? esc(since(S.first_at)) : subj.t_start ? esc(since(subj.t_start)) : nd, "ago · " + esc(fmtUtc(S && S.first_at || subj.t_start, { time: true })), "Earliest publication time among the reports");
  }
  if (se !== undefined) {
    const f = (se && se.fires) || {}, q = (se && se.quakes) || {}, hz = (se && se.hazards) || {};
    m("sens", "Sensor hits", se && !se.error ? nf((f.count || 0) + (q.count || 0) + (hz.count || 0)) : "–", se && !se.error ? "FIRMS " + nf(f.count) + " · quake " + nf(q.count) + " · hazard " + nf(hz.count) : "", "FIRMS / quake / hazard observations in the sensor window and radius", f.count ? "warm" : "");
  } else m("sens", "Sensor hits", nd, "", "");
  const fl = as && as.frontline;
  // the loaded line is DeepStateMap's (Ukraine): beyond 500 km it says nothing about the site
  m("front", "Front line", wait(as) ? nd : fl ? (fl.distance_km > 500 ? "> 500 km" : esc(kmText(fl.distance_km))) : "–", fl ? esc(kmText(fl.distance_km)) + " " + esc(fl.compass) + " · " + esc(fl.source) : as ? "no line loaded" : "", "Distance to the nearest point of the loaded front line (" + (fl ? fl.source : "") + ")", fl && fl.distance_km < 30 ? "hot" : fl && fl.distance_km < 100 ? "warm" : "");
  const milA = as && !as.error ? (as.items || []).filter((a) => a.type === "base" || a.is_military).length : null;
  const milF = X && !X.error && X.airfields ? X.airfields.military : 0;
  m("mil", "Mil. sites ≤100 km", wait(as) ? nd : nf((milA || 0) + (milF || 0)), "bases / mil. airfields", "Military bases (reference) plus military-flagged airfields (OurAirports) near the site", milA ? "warm" : "");
  m("traf", "Air / sea 24 h", wait(A) ? nd : A && !A.error ? nf(A.aircraft) + " / " + nf(A.ships) : "–", A && !A.error ? nf(A.military) + " military" : "", "Distinct tracks recorded within the radius in the last 24 h (platform collectors)");
  m("watch", "Watchlisted", wait(A) ? nd : A && !A.error ? nf(A.flagged + (A.watch_alerts || []).length) : "–", A && !A.error ? nf(A.flagged) + " tracks · " + nf((A.watch_alerts || []).length) + " alerts" : "", "Tracks matching OpenSanctions / user watchlists and watch alerts in the area, 24 h", A && (A.flagged || (A.watch_alerts || []).length) ? "hot" : "");
  const cell = G && G.gnss && G.gnss.site;
  m("gnss", "GNSS degraded", wait(G) ? nd : cell ? pct(cell.pct) : "no data", cell ? esc(cell.level) + " · " + cell.n + " aircraft" : "no ADS-B reports", "Share of aircraft in the site's H3 cell reporting degraded navigation integrity (last hour)", cell && cell.level === "high" ? "hot" : cell && cell.level === "medium" ? "warm" : "");
  const oc = G && G.outage;
  m("net", "Internet", wait(G) ? nd : oc ? (oc.country ? (oc.country.ongoing ? "OUTAGE" : "recent") : "normal") : "–", oc ? esc(oc.cc) + (oc.country ? " · " + esc(oc.country.level) : " · IODA 24 h") : "", "IODA country outage status, last 24 h", oc && oc.country && oc.country.ongoing && oc.country.level !== "low" ? "hot" : "");
  const op = O && O.by_kind && O.by_kind.optical && O.by_kind.optical.next, sar = O && O.by_kind && O.by_kind.sar && O.by_kind.sar.next;
  m("opt", "Next optical pass", wait(O) ? nd : op ? esc(relShort(op.t)) : "–", op ? esc(op.satellite) + " · cloud " + pct(op.cloud_pct) : O && O.error ? "unavailable" : "none in 4 d", "Next Sentinel-2 / Landsat pass whose swath covers the site in daylight (geometry; acquisition depends on the mission plan)");
  m("sar", "Next SAR pass", wait(O) ? nd : sar ? esc(relShort(sar.t)) : "–", sar ? esc(sar.satellite) + " · " + nf(sar.incidence_deg, 0) + "° inc." : "", "Next Sentinel-1 IW pass with the site in the right-looking swath");
  const L = E && E.light && (E.light.event || E.light.now);
  m("light", E && E.light && E.light.event ? "Light at event" : "Light now", wait(E) ? nd : L ? esc(L.phase) : "–", L ? "sun " + nf(L.sun_elev_deg, 0) + "° · moon " + Math.round(L.moon.illumination * 100) + "%" : "", "Computed sun elevation / twilight phase and moon illumination");
  const pop = X && X.population;
  m("pop", "Pop. ≤25 km", wait(X) ? nd : pop ? nf(pop.rings["25"]) : "–", pop ? nf(pop.places) + " settlements" : "", pop ? pop.basis : "");
  m("pow", "Power ≤50 km", wait(X) ? nd : X && X.power ? nf(X.power.mw_total) + " MW" : "–", X && X.power ? nf(X.power.count) + " plants" : "", "Installed capacity of power plants within 50 km (Global Power Plant Database)");
  const T = C && C.tempo;
  m("tempo", "Country tempo 7 d", wait(C) ? nd : T ? nf(T.last7) : "–", T ? (T.ratio != null ? "×" + nf(T.ratio, 2) + " vs baseline" : esc(C.cc) + " · no baseline yet") : "", T ? T.basis : "", T && T.ratio >= 2 ? "hot" : "");
  const sw = G && G.space;
  m("kp", "Kp index", wait(G) ? nd : sw && sw.kp_now ? nf(sw.kp_now[1], 1) : "–", sw && sw.scales ? "G" + sw.scales.G + " S" + sw.scales.S + " R" + sw.scales.R : "", "Planetary K-index now (NOAA SWPC)", sw && sw.kp_now && sw.kp_now[1] >= 5 ? "hot" : "");
  return cells.join("");
}

// plain-text brief from whatever has loaded (C key / button)
function briefText(spec, subj, D) {
  const L = [], S = D.sources, se = D.sensors, O = D.overhead, E = D.environment, G = D.signals, X = D.exposure, A = D.activity, C = D.country;
  L.push("INTEL BRIEF // " + (KIND_LABEL[spec.kind] || spec.kind).toUpperCase() + " // built " + new Date().toISOString().slice(0, 16).replace("T", " ") + "Z");
  L.push((subj.label || spec.label || "") + (subj.place_name ? " — " + subj.place_name + (subj.country ? ", " + subj.country : "") : ""));
  if (subj.lat != null) L.push("Position " + latLonText(subj.lat, subj.lon) + (mgrs(subj.lat, subj.lon) ? " (MGRS " + mgrs(subj.lat, subj.lon) + ")" : ""));
  if (subj.status) L.push("Status " + subj.status + ", confidence " + (+subj.confidence || 0).toFixed(2));
  for (const x of ((D.assessment || {}).items || [])) L.push("  [" + (LEVEL[x.level] || [x.level])[0] + "] " + x.text + " (" + x.basis + ")");
  if (S && !S.error) L.push("Reporting: " + S.count + " reports, " + S.outlets + " outlets, " + (S.groups || []).length + " independent texts, languages " + (S.languages || []).join("/") + "; first " + fmtUtc(S.first_at));
  if (se && !se.error) L.push("Sensors (" + fmtUtc(se.window.from) + " → " + fmtUtc(se.window.to) + "): " + se.fires.count + " FIRMS hotspots, " + se.quakes.count + " quakes, " + se.hazards.count + " hazard alerts within " + se.radius_km + " km");
  if (E && !E.error) {
    const l = E.light.event || E.light.now;
    L.push("Light " + (E.light.event ? "at event" : "now") + ": " + l.phase + ", sun " + l.sun_elev_deg + "°, moon " + Math.round(l.moon.illumination * 100) + "% " + (l.moon.up ? "up" : "down"));
    const c = (E.weather || {}).current;
    if (c) L.push("Weather now (Open-Meteo): " + (c.label || "") + ", " + c.temperature_2m + "°C, cloud " + c.cloud_cover + "%, wind " + c.wind_speed_10m + " kn from " + c.wind_direction_10m + "°");
  }
  if (O && !O.error) {
    const b = O.by_kind;
    const f = (p) => (p ? fmtUtc(p.t) + " " + p.satellite + (p.kind === "optical" ? " (cloud " + pct(p.cloud_pct) + ")" : "") : "none");
    L.push("Overhead (geometry, SGP4): next optical " + f(b.optical.next) + "; next SAR " + f(b.sar.next) + (O.event_t ? "; first optical after event " + f(b.optical.after_event) + "; first SAR after event " + f(b.sar.after_event) : ""));
  }
  if (G && !G.error) {
    const c = G.gnss && G.gnss.site;
    L.push("Signals: GNSS " + (c ? c.pct + "% degraded (" + c.level + ")" : "no data") + "; internet " + (G.outage ? (G.outage.country ? G.outage.country.level + (G.outage.country.ongoing ? " outage ongoing" : "") : "no outage") : "–") + "; Kp " + (G.space && G.space.kp_now ? G.space.kp_now[1] : "–"));
  }
  if (A && !A.error) L.push("Traffic 24 h ≤" + A.radius_km + " km: " + A.aircraft + " aircraft, " + A.ships + " ships, " + A.military + " military, " + A.flagged + " watchlisted");
  if (D.assets && D.assets.frontline) L.push("Front line " + kmText(D.assets.frontline.distance_km) + " " + D.assets.frontline.compass + " (" + D.assets.frontline.source + ")");
  if (X && !X.error) L.push("Exposure ≤50 km: " + (X.power ? X.power.mw_total + " MW power (" + X.power.count + " plants)" : "") + (X.population ? "; ~" + nf(X.population.rings["25"]) + " people ≤25 km (settlement-based)" : ""));
  if (C && !C.error) L.push("Country " + (C.name || C.cc) + ": " + C.tempo.last7 + " platform events last 7 d" + (C.tempo.ratio != null ? " (×" + C.tempo.ratio + " vs baseline)" : "") + "; UCDP " + C.history.total + " events / " + C.history.fatalities_best + " fatalities");
  return L.join("\n");
}

// ------------------------------------------------------------------ console

// [id, title, needs (sections that fill it), applies(kind), wide by default]
const TILES = [
  ["assess", "Assessment", ["assessment"], (k) => k === "event", false],
  ["loc", "Locator", ["related", "assets", "exposure", "sensors", "environment", "signals"], (k) => k !== "actor", false],
  ["time", "Timeline", ["timeline"], (k) => k === "event", true],
  ["over", "Overhead imaging passes", ["overhead"], (k) => k !== "actor", true],
  ["img", "Satellite imagery", ["imagery"], (k) => k === "event", true],
  ["env", "Local conditions", ["environment"], (k) => k !== "actor", false],
  ["sens", "Sensor evidence", ["sensors"], (k) => k !== "actor", false],
  ["story", "Storyline", ["storyline"], () => true, false],
  ["rel", "Related events", ["related"], () => true, false],
  ["src", "Sources & languages", ["sources"], (k) => k === "event", false],
  ["traf", "Air & maritime", ["traffic", "activity"], (k) => k !== "actor", false],
  ["sig", "Signals at the site", ["signals"], (k) => k !== "actor", false],
  ["assets", "Military & reference assets", ["assets"], (k) => k !== "actor", false],
  ["expo", "Infrastructure & population", ["exposure"], (k) => k !== "actor", false],
  ["ctry", "Country context", ["country"], (k) => k !== "actor", false],
  ["attn", "Attention", ["attention"], (k) => k !== "actor", false],
  ["ents", "Entities · pivot", ["entities"], () => true, false],
  ["hist", "Historical record (local)", ["history"], (k) => k !== "actor", false],
  ["bg", "Background (Wikidata / Wikipedia)", [], () => true, false],
];

let layout = { fold: {}, wide: {} };
try { layout = { fold: {}, wide: {}, ...(JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}") || {}) }; } catch (e) { /* storage disabled */ }
const saveLayout = () => { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (e) { /* private mode */ } };
const consolePref = () => { try { return localStorage.getItem(CONSOLE_KEY) !== "0"; } catch (e) { return true; } };
const setConsolePref = (on) => { try { localStorage.setItem(CONSOLE_KEY, on ? "1" : "0"); } catch (e) { /* private mode */ } };

function tileShell(t, i, kind) {
  const [id, title, , , wideDef] = t;
  const wide = layout.wide[id] ?? wideDef, fold = !!layout.fold[id];
  return '<section class="ic-tile' + (wide ? " w2" : "") + (fold ? " fold" : "") + '" data-tile="' + id + '" tabindex="-1">' +
    '<header class="ic-th"><b class="ic-n">' + (i < 9 ? i + 1 : "") + '</b><span class="ic-tt">' + esc(id === "rel" && kind === "actor" ? "Events mentioning" : id === "story" && kind !== "event" ? "Storylines" : title) + '</span><em class="ic-badge"></em><span class="ic-sp"></span>' +
    '<span class="ic-tb"><button type="button" data-t="wide" title="Double width">⇔</button><button type="button" data-t="max" title="Fill the console (Esc)">⤢</button><button type="button" data-t="fold" title="Fold / unfold">' + (fold ? "+" : "–") + "</button></span></header>" +
    '<div class="ic-body"><div class="dz-empty">Loading…</div></div></section>';
}

// render the pane; hooks: {head, stale, wdResolve, wdEntity, highlight, console(on), consoleOn(), focused()}
export async function renderIntel(spec, body, hooks) {
  const kl = KIND_LABEL[spec.kind] || spec.kind;
  const isEv = spec.kind === "event";
  hooks.head({ title: spec.label || kl, sub: "INTEL // " + kl.toUpperCase(), icon: "intel", coord: spec.lat != null ? { lat: spec.lat, lon: spec.lon, alt: 6e4 } : null });
  if (hooks.console && consolePref()) hooks.console(true);
  const tiles = TILES.filter((t) => t[3](spec.kind));
  body.innerHTML = '<div class="dk-root ip ic">' + '<div class="ic-hd"><div data-slot="head"><div class="dz-empty">Building intel picture…</div></div><div class="ic-metrics"></div></div>' +
    '<div class="ic-grid">' + tiles.map((t, i) => tileShell(t, i, spec.kind)).join("") + "</div></div>";
  const root = body.firstElementChild;
  wireCopy(root);
  const D = {};            // section name -> payload (undefined = not loaded yet)
  let subj = null, PV, bgHtml = null;
  const tileEl = (id) => root.querySelector('[data-tile="' + id + '"]');

  // ---- tile rendering: lazy (when visible and unfolded), idempotent per data version ----
  const RENDER = {
    assess: () => assessment(D.assessment),
    loc: () => locator(subj, D),
    time: () => (D.timeline ? renderTimeline(D.timeline) : D.timeline === null ? empty("No timeline") : null),
    over: () => overhead(D.overhead),
    img: () => (D.imagery ? imageryHtml(D.imagery, subj, []) : D.imagery === null ? empty("No imagery") : null),
    env: () => environment(D.environment),
    sens: () => sensors(D.sensors),
    story: () => (D.storyline !== undefined ? storylineHtml(D.storyline, spec.id) : null),
    rel: () => related(D.related),
    src: () => sources(D.sources),
    traf: () => (D.traffic !== undefined || D.activity !== undefined ? traffic(D.traffic, D.activity) : null),
    sig: () => signals(D.signals),
    assets: () => assets(D.assets),
    expo: () => exposure(D.exposure),
    ctry: () => country(D.country),
    attn: () => (D.attention !== undefined || PV !== undefined ? attention(D.attention, PV) : null),
    ents: () => entities(D.entities),
    hist: () => history(D.history),
    bg: () => bgHtml,
  };
  const WIRE = {
    time: (el) => D.timeline && wireTimeline(el, D.timeline, { onHover: (e) => hooks.highlight && hooks.highlight(e ? [{ lat: e.lat, lon: e.lon, label: e.label }] : null) }),
    img: (el) => D.imagery && wireImagery(el),
  };
  // header badge + sparkline per tile from the data
  const BADGE = {
    assess: () => D.assessment && D.assessment.items && D.assessment.items.length,
    over: () => D.overhead && D.overhead.passes && D.overhead.passes.length + " passes",
    sens: () => D.sensors && D.sensors.fires && (D.sensors.fires.count + D.sensors.quakes.count + D.sensors.hazards.count),
    rel: () => D.related && D.related.total,
    src: () => D.sources && D.sources.count,
    traf: () => D.activity && !D.activity.error && (D.activity.aircraft + D.activity.ships) + " / 24 h",
    assets: () => D.assets && D.assets.items && D.assets.items.length,
    expo: () => D.exposure && D.exposure.power && D.exposure.power.mw_total ? nf(D.exposure.power.mw_total) + " MW" : null,
    ctry: () => D.country && D.country.name,
    hist: () => D.history && D.history.total,
    ents: () => D.entities && D.entities.items && D.entities.items.length,
  };
  const SPARK = {
    rel: () => D.related && D.related.sparkline && spark(D.related.sparkline.days),
    src: () => D.sources && D.sources.hourly && spark(D.sources.hourly.slice(-30)),
    traf: () => D.activity && D.activity.series && spark(D.activity.series.aircraft.map((v, i) => v + (D.activity.series.ship[i] || 0))),
    ctry: () => D.country && D.country.tempo && spark(D.country.tempo.days.slice(-30)),
    attn: () => (PV && PV.views ? spark(PV.views.slice(-30)) : D.attention && D.attention.documents ? spark(D.attention.documents) : null),
  };
  const dirty = new Set(tiles.map((t) => t[0]));
  const visible = new Set();
  const draw = (id) => {
    const el = tileEl(id);
    if (!el || el.classList.contains("fold") || !visible.has(id) || !dirty.has(id)) return;
    const h = RENDER[id]();
    if (h == null) return;
    dirty.delete(id);
    const b = el.querySelector(".ic-body");
    b.innerHTML = h;
    if (WIRE[id]) WIRE[id](b);
  };
  const io = new IntersectionObserver((ents) => {
    for (const e of ents) { const id = e.target.dataset.tile; if (e.isIntersecting) { visible.add(id); draw(id); } else visible.delete(id); }
  }, { root: null, rootMargin: "300px 0px" });
  root.querySelectorAll(".ic-tile").forEach((t) => io.observe(t));
  // masonry row spans (see .ic-grid in dossier.css); observer callbacks are batched per frame
  const GAP = 8, ROW = 4;
  const ro = new ResizeObserver((ents) => {
    for (const e of ents) {
      const span = "span " + Math.max(1, Math.ceil((e.target.offsetHeight + GAP) / (ROW + GAP)));
      if (e.target.style.gridRowEnd !== span) e.target.style.gridRowEnd = span;
    }
  });
  root.querySelectorAll(".ic-tile").forEach((t) => ro.observe(t));
  let raf = 0;
  const refreshHead = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      if (!root.isConnected) return;
      root.querySelector(".ic-metrics").innerHTML = metrics(subj || {}, D, isEv);
      for (const [id] of tiles) {
        const el = tileEl(id);
        if (!el) continue;
        const b = BADGE[id] && BADGE[id](), s = SPARK[id] && SPARK[id]();
        el.querySelector(".ic-badge").textContent = b == null || b === false ? "" : b;
        el.querySelector(".ic-sp").innerHTML = s || "";
      }
    });
  };
  const touch = (names) => {
    for (const [id, , needs] of tiles) if (needs.some((n) => names.includes(n))) { dirty.add(id); draw(id); }
    refreshHead();
  };

  // ---- header ----
  const head = (s) => {
    const conf = s.confidence != null ? gauge(s.confidence) : "";
    const m = s.lat != null ? mgrs(s.lat, s.lon) : null;
    const on = hooks.consoleOn ? hooks.consoleOn() : false;
    root.querySelector('[data-slot="head"]').innerHTML = '<div class="dk-class st-' + esc(String(s.status || "").replace(/[^a-z-]/g, "")) + '"><span>INTEL // ' + esc(kl.toUpperCase()) + "</span><b>" + esc((EVENT_STATUS[s.status] || s.status || "ALL-SOURCE").toUpperCase()) + "</b><span>FUSION CONSOLE · " + tiles.length + ' TILES</span><span class="dk-r">BUILT ' + esc(fmtUtc(new Date().toISOString(), { time: true })) + "</span></div>" +
      '<header class="dk-head ic-head"><div class="dk-ht"><div class="dz-eyebrow">' + esc(s.category ? eventCat(s.category)[0] : kl) + '</div><h2 class="dz-title">' + esc(s.label || spec.label || "") + '</h2><div class="dz-sub">' +
      esc([s.place_name, s.country || s.cc].filter(Boolean).join(", ") || (s.q ? "name match: " + s.q : "")) + (s.t_start ? " · first report " + esc(fmtUtc(s.t_start)) : "") + "</div></div>" +
      (s.lat != null ? '<div class="dk-loc ic-loc-b"><div><span>POS</span><b>' + esc(latLonText(s.lat, s.lon)) + "</b>" + copyBtn(s.lat.toFixed(5) + ", " + s.lon.toFixed(5)) + "</div>" + (m ? "<div><span>MGRS</span><b>" + esc(m) + "</b>" + copyBtn(m.replace(/ /g, "")) + "</div>" : "") +
        "<div><span>RADIUS</span><b>" + nf(s.radius_km) + " km</b></div></div>" : "") + conf + "</header>" +
      '<div class="dk-tools ic-tools">' + (isEv ? '<button type="button" data-sel="' + esc(spec.id) + '">Select on globe</button>' : "") + (s.lat != null ? '<button type="button" data-fly="' + s.lat + "," + s.lon + '">Fly to</button>' : "") +
      '<button type="button" data-ic="brief" title="Copy a plain-text brief of everything loaded">Copy brief <kbd>C</kbd></button><button type="button" data-ic="pivot" title="Jump to the pivot entities">Pivot <kbd>P</kbd></button>' +
      (hooks.console ? '<button type="button" data-ic="console" title="Maximised console / docked pane">' + (on ? "Dock pane" : "Console") + " <kbd>M</kbd></button>" : "") +
      '<span class="ic-keys"><kbd>1</kbd>–<kbd>9</kbd> tiles</span></div>';
  };

  // ---- tile chrome + console actions ----
  const focusTile = (id) => { const el = tileEl(id); if (!el) return; el.scrollIntoView({ block: "nearest", behavior: "smooth" }); el.focus({ preventScroll: true }); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); };
  const maxTile = (el) => {
    const on = !el.classList.contains("max");
    root.querySelectorAll(".ic-tile.max").forEach((x) => x.classList.remove("max"));
    root.classList.toggle("has-max", on);
    if (on) { el.classList.add("max"); visible.add(el.dataset.tile); draw(el.dataset.tile); }
  };
  const brief = async () => { const ok = await copyText(briefText(spec, subj || {}, D)); toast(root.querySelector(".ic-hd"), ok ? "Brief copied" : "Copy failed", !ok); };
  const toggleConsole = () => { const on = !(hooks.consoleOn && hooks.consoleOn()); setConsolePref(on); hooks.console(on); if (subj) head(subj); };
  root.addEventListener("click", (e) => {
    const more = e.target.closest("[data-more]");
    if (more) { const h = more.previousElementSibling; if (h) h.hidden = false; more.remove(); return; }
    const ic = e.target.closest("[data-ic]");
    if (ic) { if (ic.dataset.ic === "brief") brief(); else if (ic.dataset.ic === "pivot") focusTile("ents"); else if (ic.dataset.ic === "console") toggleConsole(); return; }
    const b = e.target.closest(".ic-tb button");
    if (!b) return;
    const el = b.closest(".ic-tile"), id = el.dataset.tile;
    if (b.dataset.t === "fold") { const f = el.classList.toggle("fold"); b.textContent = f ? "+" : "–"; layout.fold[id] = f || undefined; saveLayout(); if (!f) draw(id); }
    else if (b.dataset.t === "wide") { const w = el.classList.toggle("w2"); layout.wide[id] = w; saveLayout(); }
    else if (b.dataset.t === "max") maxTile(el);
  });
  root.addEventListener("dblclick", (e) => { const h = e.target.closest(".ic-th"); if (h && !e.target.closest("button")) maxTile(h.closest(".ic-tile")); });
  const onKey = (e) => {
    if (!root.isConnected) { document.removeEventListener("keydown", onKey, true); io.disconnect(); ro.disconnect(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey || /INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) return;
    if (!root.offsetParent || (hooks.focused && !hooks.focused())) return;
    if (e.key === "Escape" && root.classList.contains("has-max")) { maxTile(root.querySelector(".ic-tile.max")); e.stopImmediatePropagation(); e.preventDefault(); return; }
    let hit = true;
    if (/^[1-9]$/.test(e.key) && tiles[+e.key - 1]) focusTile(tiles[+e.key - 1][0]);
    else if (e.key === "p" || e.key === "P") focusTile("ents");
    else if (e.key === "c" || e.key === "C") brief();
    else if ((e.key === "m" || e.key === "M") && hooks.console) toggleConsole();
    else hit = false;
    if (hit) { e.stopPropagation(); e.preventDefault(); }
  };
  document.addEventListener("keydown", onKey, true);

  // ---- data ----
  const fail = (names, err) => { for (const n of names.split(",")) if (D[n] === undefined) D[n] = { error: "Unavailable: " + (err.message || err) }; touch(names.split(",")); };
  let first = true;
  const got = (j) => {
    if (hooks.stale()) return;
    if (first) {
      first = false; subj = j.subject;
      head(subj);
      hooks.head({ title: subj.label || spec.label || kl, sub: "INTEL // " + kl.toUpperCase() + (subj.place_name ? " · " + subj.place_name : ""), icon: "intel", coord: subj.lat != null ? { lat: subj.lat, lon: subj.lon, alt: 6e4 } : null });
    }
    const S = j.sections || {};
    Object.assign(D, S);
    if ("entities" in S) {
      background(spec, subj, hooks, S.entities).then((r) => {
        if (hooks.stale()) return;
        bgHtml = r.html; touch(["__bg"]); dirty.add("bg"); draw("bg");
        if (!r.enwiki) { PV = null; dirty.add("attn"); draw("attn"); refreshHead(); return; }
        fetchJson("/api/intel/pageviews?" + new URLSearchParams({ title: r.enwiki }), 15000)
          .then((pv) => { PV = pv; }, (err) => { PV = { error: "Pageviews unavailable: " + (err.message || err) }; })
          .then(() => { if (!hooks.stale()) { dirty.add("attn"); draw("attn"); refreshHead(); } });
      }, (err) => { bgHtml = empty("Background unavailable: " + (err.message || err)); PV = null; dirty.add("bg"); dirty.add("attn"); draw("bg"); draw("attn"); });
    }
    touch(Object.keys(S));
  };
  const reqs = [[CORE, 20000], [AROUND, 30000]];
  if (spec.kind !== "actor") reqs.push([CONTEXT, 30000]);
  const ps = reqs.map(([sec, ms]) => fetchJson(query(spec, sec), ms));
  refreshHead();
  await Promise.all(ps.map((p, i) => p.then(got, (e) => {
    if (hooks.stale()) return;
    fail(reqs[i][0], e);
    if (i === 0 && first) throw e;
  })));
}
