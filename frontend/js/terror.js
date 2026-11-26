// TERROR WATCH: terrorism incidents, groups, designations, official threat levels and
// indicators & warnings (I&W). Read-only views of /api/terror/* (the worker's TerrorJob does
// the fetching, LLM extraction, classification and indicator maths).
//
// Accuracy rules this module keeps on screen:
// * every incident shows its status WITH its basis (confirmed = >= 2 independent outlets;
//   reported = single origin, unconfirmed; possible = criteria not established) and its
//   attribution level with the verbatim quote it rests on; name-only group matches are
//   labelled "lead", never "attributed";
// * times are EVENT times (occurrence if stated, else first report) via timefmt.js; follow-up
//   coverage of an older attack is flagged, never shown as new;
// * locations carry their precision and basis; country-level incidents are not drawn as points;
// * indicators are compared with stated baselines ("no baseline yet" when data is short) and
//   are labelled as indicators, not predictions.
import { fetchJson, poll } from "/static/js/api.js";
import { $, esc, nf, stateHtml } from "/static/js/ui.js";
import { timeHtml, fmtDateTime } from "/static/js/timefmt.js";
import { gridStep, aggregate, mergeClose, bandOf } from "/static/js/lod.js";
import { sec, wireSections, chip } from "/static/js/panels/dossier-kit.js";
import { cssColor } from "/static/js/symbology.js";

const C = window.Cesium;
export const TW_STATUS = { confirmed: cssColor("--red"), reported: cssColor("--thr-3"), possible: cssColor("--violet") || "#c792ea" };
export const TW_STATUS_TEXT = { confirmed: "Confirmed", reported: "Reported (unconfirmed)", possible: "Possible", excluded: "Excluded", context: "Context", pending: "Pending" };
export const TW_ATTR_TEXT = { claimed: "Claimed", attributed_by_authorities: "Attributed by authorities", suspected: "Suspected (media)", unknown: "Unknown" };
const ATTR_RING = { claimed: "#ffffff", attributed_by_authorities: cssColor("--ion"), suspected: "#c9d2dc", unknown: "#2a3440" };
const CLASS_TEXT = { attack: "Attack", plot: "Plot / foiled plot", arrest: "Arrest", prosecution: "Prosecution", threat: "Threat / hoax", ct_operation: "CT operation" };
const LIST_TEXT = { us_fto: "US FTO", us_sdgt: "US SDGT", un_1267: "UN 1267", un_1988: "UN 1988", uk_proscribed: "UK proscribed", eu_terr: "EU terrorist list", eu_isil_aq: "EU ISIL/AQ" };
const PREC_TEXT = { point: "exact site", city: "city", admin1: "province / region", country: "country only", unknown: "unknown" };
const ICON = '<svg viewBox="0 0 16 16"><path d="M8 1.8l6.2 11.4H1.8z"/><path d="M8 6v3.6M8 11.3v.2"/></svg>';

// ------------------------------------------------------------------ small helpers

const when = (ts) => (ts ? timeHtml(ts, { short: true }) : "–");
const statusChip = (s) => '<span class="tw-st tw-st-' + esc(s) + '">' + esc(TW_STATUS_TEXT[s] || s) + "</span>";
const p = (x) => (x == null ? "–" : x < 0.001 ? "<0.001" : nf(x, 3));
function verdictChip(v) {
  const cls = v === "above baseline" ? "warn" : v === "below baseline" ? "ok" : "";
  return chip(v || "n/a", cls);
}
function killedText(k) {
  if (!k || k.low == null) return "not stated";
  return (k.low === k.high ? nf(k.low) : nf(k.low) + "–" + nf(k.high)) + (k.disagreement ? " (sources disagree)" : "");
}
function groupLink(g) {
  if (!g || !g.group_id) return "";
  const lead = g.kind === "lead";
  return '<a href="#" class="tw-g' + (lead ? " lead" : "") + '" data-pivot=\'' + esc(JSON.stringify({ type: "terror-group", id: g.group_id })) + "' title=\"" + esc(g.basis || "") + '">' +
    esc(g.group_name || g.group_id) + "</a>" + (lead ? ' <em class="tw-lead" title="' + esc(g.basis || "") + '">lead, not attribution</em>' : "");
}
function pivot(spec, label, cls = "") {
  return '<a href="#" class="' + cls + '" data-pivot=\'' + esc(JSON.stringify(spec)) + "'>" + label + "</a>";
}
const intelSpec = (i) => ({ type: "intel", kind: "event", id: i.event_id, label: i.title || i.event_id });

// ------------------------------------------------------------------ globe layer

// Incidents (located at point / city / province precision only), coloured by status, ringed by
// attribution level, sized by the highest reported death toll. Clustered on screen with the
// shared LOD grid (lod.js aggregate + mergeClose) so a city with many reports is one badge.
export class TerrorLayer {
  constructor(viewer) {
    this.viewer = viewer; this.scene = viewer.scene;
    this.pts = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this.labels = this.scene.primitives.add(new C.LabelCollection());
    this.on = true; this.items = []; this.replayAt = null;
    this.filter = { statuses: new Set(["confirmed", "reported"]), classes: new Set(["attack", "plot", "arrest", "prosecution", "threat"]), days: 30 };
    this.onData = null;
    this.scene.camera.moveEnd.addEventListener(() => { if (this.on) this.draw(); });
  }
  async load() {
    const st = [...this.filter.statuses].join(",") || "confirmed";
    const j = await fetchJson("/api/terror/incidents?status=" + st + "&days=" + this.filter.days + "&follow_up=true&limit=5000", 30000);
    this.items = ((j && j.items) || []).filter((i) => i.lat != null && i.lon != null && ["point", "city", "admin1"].includes(i.precision));
    this.total = (j && j.count) || 0;
    for (const it of this.items) it._t = Date.parse(it.sort_time || it.first_reported_at || "") || 0;
    this.draw();
    if (this.onData) this.onData(this);
  }
  visible() {
    return this.items.filter((i) => this.filter.classes.has(i.klass || "attack") && (!this.replayAt || i._t <= this.replayAt));
  }
  setOn(b) { this.on = !!b; this.pts.show = this.labels.show = this.on; if (this.on) this.draw(); this.scene.requestRender(); }
  setReplay(at) { this.replayAt = at ? at.getTime() : null; this.draw(); }
  draw() {
    this.pts.removeAll(); this.labels.removeAll();
    if (!this.on) return;
    const h = this.scene.camera.positionCartographic.height;
    const step = gridStep(h, 46, this.scene.camera.frustum.fovy, this.scene.canvas.clientHeight);
    const prio = (i) => ({ confirmed: 3, reported: 2, possible: 1 }[i.status] || 0) * 1000 + Math.min(999, ((i.killed || {}).high || 0));
    const cells = mergeClose(aggregate(this.visible(), step, { prio, members: true }), step * 0.7);
    const byId = new Map(this.items.map((i) => [i.id, i]));
    for (const c of cells) {
      const it = c.top;
      const k = (it.killed || {}).high || 0;
      const size = 7 + Math.min(9, Math.log2(1 + k) * 2);
      const pos = C.Cartesian3.fromDegrees(c.n > 1 ? c.lon : it.lon, c.n > 1 ? c.lat : it.lat, 0);
      this.pts.add({ position: pos, pixelSize: c.n > 1 ? Math.min(26, 12 + Math.log2(c.n) * 3) : size,
        color: C.Color.fromCssColorString(TW_STATUS[it.status] || TW_STATUS.possible).withAlpha(it.status === "possible" ? 0.55 : 0.92),
        outlineColor: C.Color.fromCssColorString(c.n > 1 ? cssColor("--void") : ATTR_RING[(it.attribution_level || "unknown")] || "#2a3440"), outlineWidth: c.n > 1 ? 1.5 : 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        id: c.n > 1 ? { terror: true, cluster: c.ids.map((x) => byId.get(x)).filter(Boolean), lat: c.lat, lon: c.lon, w: c.w, s: c.s, e: c.e, nn: c.nn } : { terror: true, incident: it } });
      if (c.n > 1) this.labels.add({ position: pos, text: String(c.n), font: "600 11px Inter, sans-serif", fillColor: C.Color.WHITE,
        horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.CENTER, disableDepthTestDistance: Number.POSITIVE_INFINITY });
    }
    this.band = bandOf(h);
    this.scene.requestRender();
  }
}

// ------------------------------------------------------------------ incident card (intel tile)

export function incidentHtml(i, { compact = false } = {}) {
  if (!i) return '<div class="dz-empty">Not a Terror Watch incident.</div>';
  const a = i.attribution || {}, def = i.definition || {}, loc = i.location || {}, cas = i.casualties || {};
  const crit = def.criteria || {};
  const critRow = (k, label) => '<span class="tw-crit ' + (crit[k] === true ? "y" : crit[k] === false ? "n" : "u") + '">' + label + "</span>";
  let h = '<div class="tw-card"><div class="tw-row">' + statusChip(i.status) + chip(CLASS_TEXT[i.klass] || i.klass || "–") +
    (i.attack_type && i.attack_type !== "unknown" ? chip(i.attack_type_label || i.attack_type) : "") +
    (i.target_type && i.target_type !== "unknown" ? chip("target: " + (i.target_type_label || i.target_type)) : "") +
    (i.follow_up ? chip("follow-up coverage", "warn", "Later coverage of an earlier incident; not new") : "") + "</div>";
  h += '<p class="tw-basis"><b>Status basis</b> ' + esc(i.status_basis || i.excluded_reason || "–") + "</p>";
  if (i.excluded_reason) h += '<p class="tw-basis"><b>Excluded</b> ' + esc(i.excluded_reason) + "</p>";
  h += '<div class="tw-attr"><div class="tw-attr-h">ATTRIBUTION <b>' + esc(TW_ATTR_TEXT[a.level] || a.level || "Unknown") + "</b>" + (a.by ? " · by " + esc(a.by) : "") + (a.channel ? " · via " + esc(a.channel) : "") + "</div>" +
    (a.quote ? '<blockquote>"' + esc(a.quote) + '"</blockquote>' : '<div class="tw-muted">' + esc(a.basis || "no claim or attribution stated") + "</div>") +
    (i.perpetrator && i.perpetrator.name ? '<div class="tw-muted">Perpetrator (as stated): ' + esc(i.perpetrator.name) + " · " + esc((i.perpetrator.type || "").replace(/_/g, " ")) + "</div>" : "") +
    (i.group ? '<div>Group: ' + groupLink(i.group) + ' <small class="tw-muted">' + esc(i.group.basis || "") + "</small></div>" : "") + "</div>";
  h += '<div class="tw-kv"><span>Occurred</span><div>' + (i.occurred_at ? when(i.occurred_at) : "not stated") + ' <small class="tw-muted">' + esc(i.occurred_basis || "") + "</small></div>" +
    "<span>First report</span><div>" + when(i.first_reported_at) + "</div>" +
    "<span>Location</span><div>" + esc([i.place_name, i.country_name || i.country].filter(Boolean).join(", ") || "unknown") + " · " + esc(PREC_TEXT[i.precision] || i.precision || "") + ' <small class="tw-muted">' + esc(loc.basis || "") + "</small></div>" +
    "<span>Killed</span><div>" + killedText((cas.killed) || {}) + "</div><span>Injured</span><div>" + killedText((cas.injured) || {}) + "</div></div>";
  if (!compact) {
    h += '<div class="tw-crits" title="Working definition (GTD-aligned): sub-national perpetrator + at least 2 of C1-C3">' +
      critRow("C1_goal", "C1 political / religious / social goal") + critRow("C2_audience", "C2 wider audience") + critRow("C3_outside_warfare", "C3 outside legitimate warfare") +
      '<span class="tw-muted">' + esc(def.basis || "") + "</span></div>";
    if ((i.rules || []).length) h += '<div class="tw-muted tw-rules">rules: ' + esc(i.rules.join(", ")) + "</div>";
    if (i.merged_from && i.merged_from.length) h += '<div class="tw-muted">merged duplicates: ' + i.merged_from.map((m) => esc(m.basis)).join("; ") + "</div>";
  }
  return h + "</div>";
}

export function casualtiesHtml(i) {
  const cas = (i && i.casualties) || {};
  const src = cas.sources || [];
  const o = (i && i.outlets) || {};
  let h = "";
  if (src.length) {
    h += '<table class="tw-tab"><thead><tr><th>Source</th><th>Killed</th><th>Injured</th><th>Basis</th></tr></thead><tbody>' +
      src.map((s) => "<tr><td>" + esc(s.source) + "</td><td>" + (s.killed == null ? "–" : nf(s.killed) + (s.killed_max ? "–" + nf(s.killed_max) : "")) + "</td><td>" + (s.injured == null ? "–" : nf(s.injured) + (s.injured_max ? "–" + nf(s.injured_max) : "")) + "</td><td>" +
        (s.quote ? '<small title="' + esc(s.quote) + '">"' + esc(String(s.quote).slice(0, 90)) + '"</small>' : '<small class="tw-muted">no verbatim figure</small>') + "</td></tr>").join("") + "</tbody></table>" +
      ((cas.killed || {}).disagreement || (cas.injured || {}).disagreement ? '<div class="tw-warn">Sources disagree: the range is shown, never an average.</div>' : "");
  } else h += '<div class="dz-empty">No casualty figures stated.</div>';
  h += '<div class="tw-sub">Independent origins (' + nf(o.n || 0) + ")" + (o.telegram_only ? " · Telegram only" : "") + "</div>";
  h += '<ul class="tw-orig">' + (o.origins || []).map((x) => "<li><b>" + esc(x.outlet) + "</b> " + nf(x.docs) + " doc(s) · " + esc((x.languages || []).join(", ")) + " · first " + when(x.first) + "</li>").join("") + "</ul>";
  if (i && i.languages) h += '<div class="tw-muted">Languages: ' + esc(i.languages.join(", ")) + "</div>";
  return h;
}

function nearbyHtml(T) {
  if (!T) return null;
  let h = "";
  const c = T.country;
  if (c) {
    const pz = c.posture || {};
    h += '<div class="tw-sub">' + esc(c.name || c.cc) + " posture " + (pz.available ? "<b>" + pz.index + "</b> (band " + pz.band[0] + "–" + pz.band[1] + ")" : "n/a") +
      " · " + pivot({ type: "terror-country", cc: c.cc }, "country board") + "</div>";
    for (const lv of c.official_levels || []) h += '<div class="tw-muted">' + esc(lv.authority) + ": <b>" + esc(lv.level) + "</b>" + (lv.scope && lv.scope !== "national" ? " (" + esc(lv.scope) + ")" : "") + "</div>";
    if (c.advisory) h += '<div class="tw-muted">US advisory: Level ' + esc(c.advisory.level) + (c.advisory.terrorism ? " (names terrorism)" : "") + "</div>";
  }
  const n = T.nearby;
  if (n) {
    h += '<div class="tw-sub">Incidents within ' + nf(n.radius_km) + " km, 365 d: " + nf(n.count) + "</div>";
    h += '<ul class="tw-list">' + (n.items || []).map((x) => "<li>" + statusChip(x.status) + " " + pivot({ type: "intel", kind: "event", id: x.event_id, label: x.title }, esc(x.title || x.id)) +
      ' <small class="tw-muted">' + nf(x.distance_km) + " km · " + when(x.sort_time) + "</small></li>").join("") + "</ul>";
  }
  return h || '<div class="dz-empty">No Terror Watch context here.</div>';
}

// intel console tiles (registered on window.__noctuaPanels.intelTiles, read by intel-view.js)
export const INTEL_TILES = {
  tw: { title: "Terror Watch · classification & attribution", sections: ["terror"], wide: true, kinds: ["event"],
    render: (D) => (D.terror === undefined ? null : incidentHtml(D.terror && D.terror.incident)),
    notes: (D) => {
      const i = D.terror && D.terror.incident;
      if (D.terror === undefined) return null;
      if (!i || i.status === "excluded") return { quiet: i ? "excluded: " + (i.excluded_reason || "") : "not a terrorism incident under the working definition" };
      if (i.status === "confirmed") return { level: "alert", text: "Terror Watch: confirmed " + (CLASS_TEXT[i.klass] || "incident").toLowerCase() };
      if (i.status === "reported") return { level: "warn", text: "Terror Watch: reported (unconfirmed) " + (CLASS_TEXT[i.klass] || "incident").toLowerCase() };
      if (i.status === "possible") return { level: "warn", text: "Terror Watch: possible " + (CLASS_TEXT[i.klass] || "incident").toLowerCase() + " (criteria not established)" };
      return null;
    },
    badge: (D) => D.terror && D.terror.incident && TW_STATUS_TEXT[D.terror.incident.status] },
  twcas: { title: "Casualties & sources (Terror Watch)", sections: ["terror"], wide: false, kinds: ["event"],
    render: (D) => (D.terror === undefined ? null : D.terror && D.terror.incident ? casualtiesHtml(D.terror.incident) : '<div class="dz-empty">Not a Terror Watch incident.</div>'),
    notes: (D) => (D.terror !== undefined && !(D.terror && D.terror.incident && D.terror.incident.status !== "excluded") ? { quiet: "no incident" } : null) },
  twnear: { title: "Terror Watch · nearby incidents & country posture", sections: ["terror"], wide: false, kinds: ["event", "place", "point", "facility", "fire", "quake"],
    render: (D) => (D.terror === undefined ? null : nearbyHtml(D.terror)),
    notes: (D) => (D.terror === null ? { quiet: "no Terror Watch context" } : null) },
};

// ------------------------------------------------------------------ dossier panes

function indicatorRow(label, t, unit = "events") {
  if (!t) return "";
  if (!t.available) return '<tr><td>' + esc(label) + '</td><td colspan="4">' + chip(t.verdict || "no baseline yet") + ' <small class="tw-muted">' + esc(t.basis || "") + "</small></td></tr>";
  return "<tr><td>" + esc(label) + "</td><td>" + nf(t.observed) + " " + unit + '<br><small class="tw-muted">' + esc(t.recent_window || (t.window_days + " d")) + "</small></td><td>" + nf(t.expected, 1) +
    '<br><small class="tw-muted">baseline ' + esc(t.baseline_window || (t.baseline_days + " d")) + "</small></td><td>" + verdictChip(t.verdict) + "</td><td><small>p↑ " + p(t.p_above) + " · p↓ " + p(t.p_below) + "<br>" + esc(t.model || "") + "</small></td></tr>";
}

function postureHtml(pz) {
  if (!pz || !pz.available) return '<div class="dz-empty">Posture index unavailable: ' + esc((pz && pz.basis) || "no component") + "</div>";
  const comps = Object.entries(pz.components || {}).map(([k, c]) => "<tr><td>" + esc(k.replace(/_/g, " ")) + "</td><td>" + nf(c.value, 2) + "</td><td>" + nf((pz.weights || {})[k], 2) + "</td><td><small>" + esc(c.basis) + "</small></td></tr>").join("");
  return '<div class="tw-posture"><div class="tw-pz"><b>' + pz.index + "</b><span>/100</span></div><div><div class=\"tw-band\"><i style=\"left:" + pz.band[0] + "%;width:" + Math.max(1, pz.band[1] - pz.band[0]) + '%"></i><em style="left:' + pz.index + '%"></em></div>' +
    '<small class="tw-muted">uncertainty band ' + pz.band[0] + "–" + pz.band[1] + " · coverage " + nf(pz.coverage * 100) + "% of weights · " + esc(pz.label || "") + "</small></div></div>" +
    '<table class="tw-tab"><thead><tr><th>Component</th><th>Value</th><th>Weight</th><th>Basis</th></tr></thead><tbody>' + comps + "</tbody></table>" +
    '<details class="tw-formula"><summary>Formula</summary><ul>' + Object.entries(pz.formula || {}).map(([k, v]) => "<li><b>" + esc(k) + "</b> " + esc(v) + "</li>").join("") + "</ul></details>";
}

function levelsHtml(levels, adv) {
  let h = "";
  for (const lv of levels || []) {
    h += '<div class="tw-level"><b>' + esc(lv.level) + "</b>" + (lv.rank ? ' <small class="tw-muted">' + lv.rank + "/" + lv.scale_max + "</small>" : "") + " · " + esc(lv.authority) + (lv.scope && lv.scope !== "national" ? " (" + esc(lv.scope) + ")" : "") +
      '<div class="tw-muted">as of ' + esc(lv.as_of || "not stated by the source") + " · fetched " + when(lv.fetched_at) + ' · <a href="' + esc(lv.source_url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">source</a></div>' +
      (lv.quote ? '<blockquote>"' + esc(lv.quote) + '"</blockquote>' : "") + (lv.note ? '<div class="tw-muted">' + esc(lv.note) + "</div>" : "") + "</div>";
  }
  if (adv) h += '<div class="tw-level"><b>US State Dept: Level ' + esc(adv.level) + "</b> " + esc(adv.label || "") + (adv.terrorism ? " " + chip("names terrorism", "warn") : "") +
    '<div class="tw-muted">updated ' + when(adv.updated) + ' · <a href="' + esc(adv.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">advisory</a></div>' + (adv.quote ? '<blockquote>"' + esc(adv.quote) + '"</blockquote>' : "") + "</div>";
  return h || '<div class="dz-empty">No official national level published for this country in the monitored sources.</div>';
}

function incidentRows(items) {
  if (!items || !items.length) return '<div class="dz-empty">No incidents.</div>';
  return '<ul class="tw-list">' + items.map((i) => "<li>" + statusChip(i.status) + " " + pivot(intelSpec(i), esc(i.title || i.id)) + '<br><small class="tw-muted">' +
    esc(CLASS_TEXT[i.klass] || i.klass || "") + " · " + (i.occurred_at ? "occurred " + when(i.occurred_at) : "first report " + when(i.first_reported_at)) + (i.follow_up ? " · follow-up" : "") +
    " · " + esc(i.place_name || i.country || "") + " · killed " + killedText(i.killed) + " · " + esc(TW_ATTR_TEXT[i.attribution_level] || "") + (i.group ? " · " + esc(i.group.group_name || "") + (i.group.kind === "lead" ? " (lead)" : "") : "") + "</small></li>").join("") + "</ul>";
}

function desigRows(items) {
  if (!items || !items.length) return '<div class="dz-empty">None in the window.</div>';
  return '<ul class="tw-list">' + items.map((d) => "<li>" + chip(LIST_TEXT[d.list_id] || d.list_id) + " <b>" + esc(d.change) + "</b> " + (d.group_id ? pivot({ type: "terror-group", id: d.group_id }, esc(d.name)) : esc(d.name)) +
    ' <small class="tw-muted">' + esc(d.date) + " · " + esc(d.date_basis || "") + "</small></li>").join("") + "</ul>";
}

function seriesSpark(series) {
  const vals = Object.values(series || {});
  if (!vals.length) return "";
  const max = Math.max(1, ...vals), w = 220, h = 34, bw = w / vals.length;
  return '<svg class="tw-spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' + vals.map((v, i) => '<rect x="' + (i * bw + 0.5) + '" y="' + (h - (v / max) * h) + '" width="' + Math.max(1, bw - 1) + '" height="' + (v / max) * h + '"><title>' + esc(Object.keys(series)[i]) + ": " + v + "</title></rect>").join("") + "</svg>";
}

function indicatorsTable(ind, opts = {}) {
  return '<table class="tw-tab tw-ind"><thead><tr><th>Indicator</th><th>Observed</th><th>Expected</th><th>Verdict</th><th>Test</th></tr></thead><tbody>' +
    indicatorRow("UCDP one-sided violence by designated groups (events)", ind.ucdp_events) + indicatorRow("…deaths", ind.ucdp_deaths, "deaths") +
    indicatorRow("News: unique attacks (30 d)", ind.news_incident_tempo, "incidents") + indicatorRow("News: plots / arrests / prosecutions (30 d)", ind.news_plot_arrest_tempo, "items") + "</tbody></table>" +
    (ind.ucdp_events && ind.ucdp_events.series ? '<div class="tw-muted">UCDP monthly events, last 24 months ' + seriesSpark(ind.ucdp_events.series) + "</div>" : "") +
    (ind.ucdp_events && ind.ucdp_events.caveat ? '<div class="tw-caveat">' + esc(ind.ucdp_events.caveat) + " " + esc(ind.ucdp_events.latest_month_note || "") + "</div>" : "") +
    (opts.extra || "");
}

function baseRatesHtml(r) {
  if (!r || !r.available) return '<div class="dz-empty">' + esc((r && r.basis) || "no base rate") + "</div>";
  return '<table class="tw-tab"><thead><tr><th>Year</th><th>Events</th><th>Deaths</th></tr></thead><tbody>' + r.years.map((y) => "<tr><td>" + y.year + "</td><td>" + nf(y.events) + "</td><td>" + nf(y.deaths) + "</td></tr>").join("") +
    "</tbody></table><div class=\"tw-muted\">mean " + nf(r.mean_events, 1) + " events / " + nf(r.mean_deaths, 1) + " deaths per year · " + esc(r.basis) + "</div><div class=\"tw-caveat\">" + esc(r.caveat) + "</div>";
}

function calendarHtml(cal) {
  if (!cal || !cal.length) return '<div class="dz-empty">No listed dates in the next 45 days.</div>';
  return '<ul class="tw-list">' + cal.map((c) => "<li><b>" + esc(c.on) + "</b> (in " + c.in_days + " d) " + esc(c.label) + "</li>").join("") + '</ul><div class="tw-caveat">Context only: an anniversary is not an indicator that an attack will happen.</div>';
}

async function renderCountry(spec, body, hooks) {
  const cc = String(spec.cc || "").toUpperCase();
  hooks.head({ title: "Terror Watch · " + cc, sub: "COUNTRY BOARD", icon: "terror" });
  const b = await fetchJson("/api/terror/board/" + encodeURIComponent(cc), 30000);
  if (hooks.stale()) return;
  hooks.head({ title: "Terror Watch · " + (b.name || cc), sub: "COUNTRY BOARD // INDICATORS, NOT PREDICTIONS", icon: "terror" });
  const ind = b.indicators || {};
  let us = "";
  if (b.us) {
    const d = b.us.doj || {}, n = b.us.ntas;
    us = sec("tw-us", "US posture", '<div class="tw-kv"><span>DHS NTAS</span><div>' + (n ? "<b>" + esc(n.level) + "</b> " + '<small class="tw-muted">' + esc(n.note || "") + "</small>" + (n.quote ? '<blockquote>"' + esc(n.quote) + '"</blockquote>' : "") : "unavailable (source not reachable)") + "</div>" +
      "<span>DOJ terrorism cases</span><div>" + nf(d.cases_30d) + " releases in 30 d (" + nf(d.prosecutions_30d) + " prosecutions, " + nf(d.arrests_plots_30d) + " arrests/plots) · " + esc(d.baseline || "") + '<br><small class="tw-muted">' + esc(d.basis || "") + " · collected over " + nf(d.coverage_days) + " d</small></div></div>" +
      '<div class="tw-sub">Latest DOJ releases</div><ul class="tw-list">' + (d.recent || []).map((r) => '<li><a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">' + esc(r.title) + "</a> <small class=\"tw-muted\">" + esc(r.kind) + " · " + when(r.published_at) + "</small></li>").join("") + "</ul>" +
      '<div class="tw-sub">US designations (FTO, last 365 d, official dates)</div>' + desigRows(((b.us.designations_365d || {}).fto) || []), { meta: "NTAS · DOJ · FTO / SDGT" });
  }
  body.innerHTML = '<div class="dk-root tw-pane">' +
    '<div class="tw-disclaimer">Indicators &amp; warnings with stated baselines, not predictions. Definition ' + esc(b.definition_version || "") + " · " + pivot({ type: "terror-def" }, "definition &amp; sources") + "</div>" +
    sec("tw-posture", "Threat posture (composite index)", postureHtml(b.posture), { meta: "documented formula" }) +
    sec("tw-levels", "Official threat levels & advisories", levelsHtml(b.official_levels, b.advisory)) + us +
    sec("tw-ind", "Indicators vs baselines", indicatorsTable(ind)) +
    sec("tw-base", "Base rates (UCDP proxy, 5 complete years)", baseRatesHtml(ind.base_rates)) +
    sec("tw-recent", "Recent incidents (event time)", incidentRows(b.recent_incidents), { meta: nf((b.counts || {}).attacks_30d) + " attacks in 30 d · " + nf((b.counts || {}).possible) + " possible" }) +
    sec("tw-groups", "Groups active here (data-derived)", (b.groups || []).length ? '<div class="tw-chips">' + b.groups.map((g) => pivot({ type: "terror-group", id: g.id }, esc(g.name), "dk-chip")).join("") + "</div>" : '<div class="dz-empty">No mapped group activity in 24 months.</div>') +
    sec("tw-desig", "Designations touching these groups (90 d)", desigRows((ind.designations_90d || {}).items)) +
    sec("tw-cal", "Significant dates (context)", calendarHtml(b.calendar)) +
    '<div class="tw-muted tw-foot">computed ' + when(b.computed_at) + "</div></div>";
  wireSections(body.firstElementChild);
}

async function renderGlobal(spec, body, hooks) {
  hooks.head({ title: "Terror Watch", sub: "GLOBAL BOARD", icon: "terror" });
  const [b, st] = await Promise.all([fetchJson("/api/terror/board/global", 30000), fetchJson("/api/terror/status", 15000).catch(() => null)]);
  if (hooks.stale()) return;
  const ind = b.indicators || {};
  const hot = (b.hotspots_ucdp || []).map((h) => "<tr><td>" + pivot({ type: "terror-country", cc: h.country }, esc(h.name)) + "</td><td>" + nf(h.events) + "</td><td>" + nf(h.deaths) + "</td><td>" + verdictChip(h.tempo_verdict) + "</td><td><small>" +
    h.groups.map((g) => esc(g.name)).join(", ") + "</small></td></tr>").join("");
  const news = (b.news_attacks_30d_by_country || []).map((c) => "<li>" + pivot({ type: "terror-country", cc: c.country }, esc(c.name)) + " " + nf(c.attacks) + "</li>").join("");
  const levels = (b.levels || []).map((lv) => "<tr><td>" + pivot({ type: "terror-country", cc: lv.country }, esc(lv.country)) + "</td><td><b>" + esc(lv.level) + "</b>" + (lv.scope && lv.scope !== "national" ? '<br><small class="tw-muted">' + esc(lv.scope) + "</small>" : "") + "</td><td>" + esc(lv.authority) + "</td><td>" + esc(lv.as_of || "–") + "</td></tr>").join("");
  const srcs = st && st.sources ? st.sources.map((s) => '<span class="tw-src ' + (s.ok ? "ok" : "bad") + '" title="' + esc(s.error || "") + '">' + esc(s.source_id) + "</span>").join("") : "";
  body.innerHTML = '<div class="dk-root tw-pane"><div class="tw-disclaimer">Indicators &amp; warnings, not predictions · ' + pivot({ type: "terror-def" }, "definition, sources &amp; evaluation") + " · " + pivot({ type: "terror-groups" }, "groups") + " · " + pivot({ type: "terror-country", cc: "US" }, "US posture") + "</div>" +
    sec("tw-g-ind", "Global indicators vs baselines", indicatorsTable(ind)) +
    sec("tw-hot", "Hotspots: designated-group one-sided violence (UCDP, last 3 months)", '<table class="tw-tab"><thead><tr><th>Country</th><th>Events</th><th>Deaths</th><th>vs 12-mo baseline</th><th>Groups</th></tr></thead><tbody>' + hot + "</tbody></table>" +
      '<div class="tw-caveat">' + esc(((ind.ucdp_events || {}).caveat) || "") + "</div>") +
    sec("tw-newsc", "News-derived attacks by country (30 d, unique incidents)", news ? '<ul class="tw-cols">' + news + "</ul>" : '<div class="dz-empty">None yet.</div>', { meta: "news collection: " + nf(b.news_coverage_days, 1) + " d" }) +
    sec("tw-g-lv", "Official national threat levels", '<table class="tw-tab"><thead><tr><th>Country</th><th>Level</th><th>Authority</th><th>As of</th></tr></thead><tbody>' + levels + "</tbody></table>") +
    sec("tw-g-des", "New designations / delistings (90 d)", desigRows((ind.designations_90d || {}).items)) +
    sec("tw-g-cal", "Significant dates (context)", calendarHtml(b.calendar)) +
    sec("tw-g-src", "Source health", '<div class="tw-chips">' + srcs + "</div>") +
    '<div class="tw-muted tw-foot">computed ' + when(b.computed_at) + "</div></div>";
  wireSections(body.firstElementChild);
}

async function renderGroup(spec, body, hooks) {
  hooks.head({ title: "Group", sub: "TERROR WATCH // GROUP", icon: "terror" });
  const g = await fetchJson("/api/terror/groups/" + encodeURIComponent(spec.id), 30000);
  if (hooks.stale()) return;
  const G = g.group;
  hooks.head({ title: G.name, sub: "TERROR WATCH // GROUP · " + (G.category_label || ""), icon: "terror" });
  const lists = Object.entries(g.designations || {}).map(([lid, d]) => "<tr><td>" + esc(LIST_TEXT[lid] || lid) + "</td><td>" + (d.status === "listed" ? chip("listed", "warn") : chip(d.status)) + "</td><td><small>" +
    d.entries.filter((e) => !e.kind || e.kind === "organisation").slice(0, 4).map((e) => esc(e.name) + (e.listed_on ? " (" + esc(e.listed_on) + ")" : "") + (e.delisted_on ? " delisted " + esc(e.delisted_on) : "")).join("; ") +
    (d.persons_or_assets ? " · +" + nf(d.persons_or_assets) + " linked persons/assets" : "") + '</small></td><td><small class="tw-muted">' + esc((d.entries[0] || {}).group_basis || "") + "</small></td></tr>").join("");
  const areas = (g.areas_of_operation || []).map((a) => "<tr><td>" + pivot({ type: "terror-country", cc: a.country }, esc(a.name)) + "</td><td>" + nf(a.ucdp_events) + "</td><td>" + nf(a.ucdp_deaths) + "</td><td>" + nf(a.incidents) + "</td></tr>").join("");
  const fam = (g.family || []).filter((f) => f.id !== G.id).map((f) => pivot({ type: "terror-group", id: f.id }, esc(f.name), "dk-chip")).join("");
  const docs = (g.docs || []).map((d) => '<li><a href="' + esc(d.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">' + esc(d.title) + '</a> <small class="tw-muted">' + esc(d.source) + " · " + when(d.published_at || d.first_seen) + "</small></li>").join("");
  body.innerHTML = '<div class="dk-root tw-pane">' +
    '<div class="tw-disclaimer">Identity from the curated KB; designation status from the official lists; areas and tempo derived from data. ' + (G.note ? esc(G.note) : "") + "</div>" +
    sec("tw-gid", "Identity", '<div class="tw-kv"><span>Category</span><div>' + esc(G.category_label) + "</div><span>Aliases</span><div><small>" + esc((G.aliases || []).join(" · ")) + "</small></div>" +
      ((G.weak_aliases || []).length ? "<span>Ambiguous short names</span><div><small>" + esc(G.weak_aliases.join(" · ")) + " (leads only)</small></div>" : "") +
      (g.parent ? "<span>Parent</span><div>" + pivot({ type: "terror-group", id: g.parent.id }, esc(g.parent.name)) + "</div>" : "") + (fam ? "<span>Affiliates</span><div>" + fam + "</div>" : "") + "</div>") +
    sec("tw-gdes", "Designation status by jurisdiction", lists ? '<table class="tw-tab"><thead><tr><th>List</th><th>Status</th><th>Entries</th><th>Match basis</th></tr></thead><tbody>' + lists + "</tbody></table>" : '<div class="dz-empty">No entry on the monitored lists matches this group by exact name.</div>', { meta: esc(g.designation_basis || "") }) +
    sec("tw-garea", "Areas of operation (data-derived, 24 months)", areas ? '<table class="tw-tab"><thead><tr><th>Country</th><th>UCDP events</th><th>UCDP deaths</th><th>Linked incidents</th></tr></thead><tbody>' + areas + "</tbody></table>" : '<div class="dz-empty">No attributed activity in the data.</div>', { meta: esc(g.areas_basis || "") }) +
    sec("tw-gtempo", "Activity tempo (UCDP)", indicatorsTable({ ucdp_events: g.ucdp_tempo })) +
    sec("tw-ginc", "Incidents (event time)", incidentRows(g.incidents), { meta: "confirmed " + nf((g.incident_counts || {}).confirmed) + " · reported " + nf((g.incident_counts || {}).reported) + " · possible " + nf((g.incident_counts || {}).possible) }) +
    sec("tw-gdocs", "Official documents mentioning the group", docs ? '<ul class="tw-list">' + docs + "</ul>" : '<div class="dz-empty">None.</div>') + "</div>";
  wireSections(body.firstElementChild);
}

async function renderGroups(spec, body, hooks) {
  hooks.head({ title: "Groups", sub: "TERROR WATCH // GROUPS", icon: "terror" });
  const j = await fetchJson("/api/terror/groups", 30000);
  if (hooks.stale()) return;
  const rows = (j.items || []).map((g) => "<tr><td>" + pivot({ type: "terror-group", id: g.id }, esc(g.name)) + (g.auto ? ' <small class="tw-muted">(from list)</small>' : "") + "</td><td><small>" + esc(g.category_label) + "</small></td><td>" + g.lists.map((l) => chip(LIST_TEXT[l] || l)).join(" ") + "</td><td>" + nf(g.incidents_365d) + "</td></tr>").join("");
  body.innerHTML = '<div class="dk-root tw-pane">' + sec("tw-gl", "Groups knowledge base", '<table class="tw-tab"><thead><tr><th>Group</th><th>Category</th><th>Listed on</th><th>Linked incidents 365 d</th></tr></thead><tbody>' + rows + "</tbody></table>", { meta: nf(j.count) + " groups" }) + "</div>";
  wireSections(body.firstElementChild);
}

async function renderDef(spec, body, hooks) {
  hooks.head({ title: "Definition & sources", sub: "TERROR WATCH // METHOD", icon: "terror" });
  const [d, s, ev] = await Promise.all([fetchJson("/api/terror/definition", 15000), fetchJson("/api/terror/sources", 15000), fetchJson("/api/terror/eval", 15000).catch(() => null)]);
  if (hooks.stale()) return;
  const crit = (d.criteria.required || []).map((c) => "<li>" + esc(c.text) + "</li>").join("") + "<li>and at least two of:<ul>" + (d.criteria.at_least_two_of || []).map((c) => "<li>" + esc(c.text) + "</li>").join("") + "</ul></li>";
  const rules = (d.operational_rules || []).map((r) => "<li>" + esc(r) + "</li>").join("");
  const kvList = (o) => '<div class="tw-kv">' + Object.entries(o || {}).map(([k, v]) => "<span>" + esc(k) + "</span><div>" + esc(v) + "</div>").join("") + "</div>";
  const srcs = (s.sources || []).map((x) => "<tr><td>" + esc(x.name) + (x.enabled ? "" : ' <small class="tw-muted">(not used)</small>') + "</td><td>" + (x.ok == null ? "–" : x.ok ? chip("ok", "ok") : chip("error", "warn", x.error || "")) + "</td><td><small>" + esc(x.licence) + "</small></td><td>" + esc(x.commercial) + "</td><td><small>" + esc(x.note || "") + "</small>" + (x.last_ok ? '<br><small class="tw-muted">last ok ' + when(x.last_ok) + "</small>" : "") + "</td></tr>").join("");
  let evh = '<div class="dz-empty">No evaluation report yet (osint terror-eval).</div>';
  if (ev && ev.overall) {
    const o = ev.overall, row = (k, l) => { const m = o[k] || {}; return "<tr><td>" + esc(l) + "</td><td>" + (m.value == null ? "–" : nf(m.value, 3)) + "</td><td>" + nf(m.k) + "/" + nf(m.n) + "</td><td>" + (m.ci95 ? m.ci95.join("–") : "–") + "</td></tr>"; };
    evh = '<table class="tw-tab"><thead><tr><th>Metric</th><th>Value</th><th>k/n</th><th>95% CI</th></tr></thead><tbody>' + row("attack_precision", "Attack precision (reported + confirmed)") + row("attack_recall", "Attack recall (incl. possible)") +
      row("confirmed_precision", "Confirmed precision") + row("other_class_precision", "Plot / arrest / threat precision") + row("attribution_accuracy", "Attribution-level accuracy") + row("group_link_accuracy", "Group link accuracy") +
      row("country_accuracy", "Country accuracy") + row("coordinate_accuracy", "Coordinate accuracy") + row("duplicate_rate", "Duplicate rate") + "</tbody></table>" +
      '<div class="tw-muted">' + nf(ev.n_items) + " hand-labelled items (" + Object.entries(ev.pools || {}).map(([k, v]) => k + " " + v).join(", ") + ") · generated " + when(ev.generated_at) + "</div>";
  }
  body.innerHTML = '<div class="dk-root tw-pane">' +
    sec("tw-def", "Working definition (" + esc(d.version) + ")", '<p class="tw-muted">' + esc(d.basis) + "</p><ol>" + crit + "</ol>") +
    sec("tw-rules", "Operational rules", "<ul>" + rules + "</ul>") +
    sec("tw-status", "Incident status", kvList(d.statuses)) + sec("tw-attrl", "Attribution levels", kvList(d.attribution_levels)) +
    sec("tw-eval", "Evaluation (hand-labelled sample)", evh) +
    sec("tw-srcs", "Sources, licences & health", '<table class="tw-tab"><thead><tr><th>Source</th><th>State</th><th>Licence</th><th>Commercial</th><th>Notes</th></tr></thead><tbody>' + srcs + "</tbody></table>") +
    '<div class="tw-caveat">' + esc(d.sources_note) + " " + esc(d.disclaimer) + "</div></div>";
  wireSections(body.firstElementChild);
}

export const TERROR_PANES = {
  terror: { icon: ICON, key: () => "tw:global", render: renderGlobal },
  "terror-country": { icon: ICON, key: (s) => "tw:c:" + s.cc, render: renderCountry },
  "terror-group": { icon: ICON, key: (s) => "tw:g:" + s.id, render: renderGroup },
  "terror-groups": { icon: ICON, key: () => "tw:groups", render: renderGroups },
  "terror-def": { icon: ICON, key: () => "tw:def", render: renderDef },
};

// ------------------------------------------------------------------ right-panel feed

const FEED_TYPES = [["incident", "Incidents"], ["designation", "Designations"], ["level_change", "Levels"], ["doc", "Justice / LE"]];
const NAV = [[{ type: "terror" }, "Global board"], [{ type: "terror-country", cc: "US" }, "US posture"], [{ type: "terror-groups" }, "Groups"], [{ type: "terror-def" }, "Definition"]];

export class TerrorPanel {
  constructor({ el, tools, open, flyTo, onCount }) {
    this.el = el; this.tools = tools; this.open = open; this.flyTo = flyTo; this.onCount = onCount;
    this.types = new Set(["incident", "designation", "level_change", "doc"]);
    this.statuses = new Set(["confirmed", "reported", "possible"]);
    this.days = 30; this.cc = ""; this.items = []; this.global = null;
    tools.innerHTML = '<span class="seg tw-types">' + FEED_TYPES.map(([k, l]) => '<button class="on" data-ty="' + k + '">' + l + "</button>").join("") + "</span>" +
      '<span class="seg tw-sts">' + ["confirmed", "reported", "possible"].map((s) => '<button class="on" data-st="' + s + '" title="' + esc(TW_STATUS_TEXT[s]) + '">' + s.slice(0, 4) + "</button>").join("") + "</span>" +
      '<select id="tw-days" class="mini" title="Window (event time)"><option value="7">7 d</option><option value="30" selected>30 d</option><option value="90">90 d</option><option value="365">1 y</option></select>' +
      '<input id="tw-cc" type="search" placeholder="Country (ISO2)" maxlength="2" spellcheck="false" aria-label="Country filter">';
    tools.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.ty) { b.classList.toggle("on"); if (b.classList.contains("on")) this.types.add(b.dataset.ty); else this.types.delete(b.dataset.ty); this.render(); }
      if (b.dataset.st) { b.classList.toggle("on"); if (b.classList.contains("on")) this.statuses.add(b.dataset.st); else this.statuses.delete(b.dataset.st); this.render(); }
    });
    tools.querySelector("#tw-days").addEventListener("change", (e) => { this.days = +e.target.value; this.refresh(); });
    tools.querySelector("#tw-cc").addEventListener("input", (e) => { const v = e.target.value.trim().toUpperCase(); if (v.length === 0 || v.length === 2) { this.cc = v; this.refresh(); } });
    el.addEventListener("click", (e) => {
      const a = e.target.closest("[data-open]");
      if (a) { e.preventDefault(); let spec; try { spec = JSON.parse(a.dataset.open); } catch (err) { return; } this.open(spec); return; }
      const f = e.target.closest("[data-fly]");
      if (f) { const [la, lo] = f.dataset.fly.split(",").map(Number); this.flyTo(la, lo, 6e5); }
    });
  }
  async refresh() {
    const qs = "days=" + this.days + (this.cc ? "&country=" + this.cc : "") + "&limit=500";
    let feed, g;
    try {
      [feed, g] = await Promise.all([fetchJson("/api/terror/feed?" + qs, 30000), this.global ? Promise.resolve(this.global) : fetchJson("/api/terror/board/global", 30000).catch(() => null)]);
    } catch (e) {
      const msg = String((e && e.message) || e);
      if (!this.items.length) { this.el.innerHTML = stateHtml("err", "Terror Watch feed unavailable", msg); return; }
      // already rendered: keep the data, show a one-line non-destructive note above it
      const note = this.el.querySelector(".tw-stale") || this.el.insertAdjacentElement("afterbegin", Object.assign(document.createElement("div"), { className: "tw-stale" }));
      note.innerHTML = stateHtml("err", "Showing last data", "Refresh failed: " + msg);
      return;
    }
    this.items = (feed && feed.items) || []; this.global = g;
    this.render();
  }
  render() {
    const vis = this.items.filter((x) => this.types.has(x.type === "advisory_change" ? "level_change" : x.type) && (x.type !== "incident" || this.statuses.has(x.status)));
    const n = this.items.filter((x) => x.type === "incident" && x.status !== "possible" && !x.follow_up).length;
    if (this.onCount) this.onCount(n);
    const g = this.global || {}, ind = g.indicators || {}, t = ind.ucdp_events || {}, nt = ind.news_incident_tempo || {};
    const head = '<div class="tw-head"><div><b>Terror Watch</b> <small class="tw-muted">indicators &amp; warnings, not predictions</small></div>' +
      '<div class="tw-head-row"><span>Global UCDP tempo ' + verdictChip(t.verdict || (t.available === false ? "no data" : "–")) + '</span><span>News tempo ' + chip(nt.verdict || "–") + "</span></div>" +
      '<div class="tw-head-row">' + NAV.map(([spec, label]) => '<button type="button" class="ghost" data-open=\'' + esc(JSON.stringify(spec)) + "'>" + esc(label) + "</button>").join("") + "</div></div>";
    const rows = vis.map((x) => this.row(x)).join("");
    this.el.innerHTML = head + (rows ? '<ul class="tw-feed">' + rows + "</ul>" : stateHtml("empty", "No items in window", "Nothing matches these filters in the selected window."));
  }
  row(x) {
    if (x.type === "incident") {
      const spec = intelSpec(x);
      return '<li class="tw-fi"><i class="tw-dot" style="--c:' + (TW_STATUS[x.status] || "#888") + '"></i><div><a href="#" data-open=\'' + esc(JSON.stringify(spec)) + "'>" + esc(x.title || x.id) + "</a>" +
        '<small class="tw-muted">' + esc(TW_STATUS_TEXT[x.status] || x.status) + " · " + esc(CLASS_TEXT[x.klass] || x.klass || "") + " · " + (x.occurred_at ? "occurred " + when(x.occurred_at) : "first report " + when(x.first_reported_at)) + (x.follow_up ? " · follow-up" : "") +
        " · " + esc(x.place_name || x.country || "?") + " (" + esc(PREC_TEXT[x.precision] || "") + ")" + " · " + esc(TW_ATTR_TEXT[x.attribution_level] || "") + (x.group ? " · " + esc(x.group.group_name || "") + (x.group.kind === "lead" ? " (lead)" : "") : "") + "</small></div>" +
        (x.lat != null && ["point", "city", "admin1"].includes(x.precision) ? '<button type="button" class="ghost" data-fly="' + x.lat + "," + x.lon + '" title="Fly to">⌖</button>' : "") + "</li>";
    }
    if (x.type === "designation") {
      const spec = x.group_id ? { type: "terror-group", id: x.group_id } : { type: "terror-groups" };
      return '<li class="tw-fi"><i class="tw-dot sq" style="--c:var(--amber)"></i><div><a href="#" data-open=\'' + esc(JSON.stringify(spec)) + "'>" + esc((LIST_TEXT[x.list_id] || x.list_id) + " · " + x.change + ": " + x.name) + "</a>" +
        '<small class="tw-muted">' + esc(x.date) + " · " + esc(x.date_basis || "") + "</small></div></li>";
    }
    if (x.type === "level_change" || x.type === "advisory_change") {
      return '<li class="tw-fi"><i class="tw-dot sq" style="--c:var(--ion)"></i><div><a href="#" data-open=\'' + esc(JSON.stringify({ type: "terror-country", cc: x.country })) + "'>" + esc(x.country + " · " + (x.authority || "US advisory") + ": " + (x.prev_level ?? "?") + " → " + (x.level ?? "?")) + "</a>" +
        '<small class="tw-muted">detected ' + when(x.observed_at) + (x.as_of ? " · as of " + esc(x.as_of) : "") + "</small></div></li>";
    }
    return '<li class="tw-fi"><i class="tw-dot sq" style="--c:var(--fg-2)"></i><div><a href="' + esc(x.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">' + esc(x.title) + "</a>" +
      '<small class="tw-muted">' + esc(x.source) + " · " + esc(x.kind || "") + " · " + when(x.published_at || x.first_seen) + " (" + esc(x.date_basis || "") + ")" + (x.group_ids && x.group_ids.length ? " · mentions " + esc(x.group_ids.join(", ")) : "") + "</small></div></li>";
  }
}

// ------------------------------------------------------------------ install

// Wires the layer, the left-tree rows (#tw-*), the right-panel tab (#terror-pane) and registers
// the dossier panes and intel tiles. Returns { layer, panel, pick(id), refresh(), setReplay(at) }.
export function installTerror(ctx) {
  const { viewer, dossier, flyTo } = ctx;
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, TERROR_PANES);
  window.__noctuaPanels = window.__noctuaPanels || {};
  window.__noctuaPanels.intelTiles = Object.assign(window.__noctuaPanels.intelTiles || {}, INTEL_TILES);
  window.dispatchEvent(new Event("noctua:panels"));
  const layer = new TerrorLayer(viewer);
  const open = (spec) => dossier.open(spec);
  const panel = new TerrorPanel({ el: $("terror-pane"), tools: document.querySelector('#tracks .tk-tools[data-pane="terror"]'), open, flyTo,
    onCount: (n) => { const c = $("terror-count"); const t = n ? String(n) : ""; if (c && c.textContent !== t) c.textContent = t; } });
  const count = () => { const el = $("c-terror"), g = $("g-terror-n"); const t = layer.on ? nf(layer.visible().length) : ""; if (el) el.textContent = t; if (g) g.textContent = layer.on ? t : "–"; };
  layer.onData = count;
  const tog = $("tw-incidents");
  if (tog) { layer.on = tog.checked; tog.addEventListener("change", () => { layer.setOn(tog.checked); if (tog.checked) layer.load().catch(() => {}); count(); }); }
  const chips = $("tw-chips");
  if (chips) {
    chips.innerHTML = ["confirmed", "reported", "possible"].map((s) => '<button class="chip' + (layer.filter.statuses.has(s) ? " on" : "") + '" data-st="' + s + '" style="--c:' + TW_STATUS[s] + '">' + s + "</button>").join("") +
      [["attack", "attacks"], ["plot", "plots"], ["arrest", "arrests"], ["threat", "threats"]].map(([k, l]) => '<button class="chip on" data-k="' + k + '" style="--c:var(--fg-2)">' + l + "</button>").join("");
    chips.addEventListener("click", (e) => {
      const b = e.target.closest(".chip");
      if (!b) return;
      b.classList.toggle("on");
      const on = b.classList.contains("on");
      if (b.dataset.st) { if (on) layer.filter.statuses.add(b.dataset.st); else layer.filter.statuses.delete(b.dataset.st); layer.load().catch(() => {}); }
      if (b.dataset.k) { const ks = b.dataset.k === "arrest" ? ["arrest", "prosecution"] : [b.dataset.k]; for (const k of ks) { if (on) layer.filter.classes.add(k); else layer.filter.classes.delete(k); } layer.draw(); count(); }
    });
  }
  const days = $("tw-days-l");
  if (days) days.addEventListener("change", (e) => { layer.filter.days = +e.target.value; layer.load().catch(() => {}); });
  const openBtn = $("tw-open");
  if (openBtn) openBtn.addEventListener("click", () => open({ type: "terror" }));
  poll("terror", 300000, async () => { if (layer.on) await layer.load(); count(); if (ctx.tracklist && ctx.tracklist.pane === "terror") await panel.refresh(); }, (n, e) => { if (e) console.warn("terror", e); });
  setTimeout(() => panel.refresh().catch(() => {}), 7000); // tab badge
  const pick = (id) => {
    if (id.incident) { open(intelSpec(id.incident)); return true; }
    if (id.cluster) {
      const items = id.cluster;
      if (items.length <= 6 || viewer.scene.camera.positionCartographic.height < 4e5) {
        const tip = $("pick-tip");
        if (tip) {
          tip.innerHTML = "<b>" + items.length + " incidents</b><br>" + items.slice(0, 8).map((i) => esc((TW_STATUS_TEXT[i.status] || i.status) + ": " + (i.title || "").slice(0, 70))).join("<br>");
          tip.classList.remove("hidden"); clearTimeout(tip._t); tip._t = setTimeout(() => tip.classList.add("hidden"), 6000);
        }
        open(intelSpec(items[0]));
      } else {
        viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(id.w - 0.5, id.s - 0.5, id.e + 0.5, id.nn + 0.5), duration: 1.2 });
      }
      return true;
    }
    return false;
  };
  return { layer, panel, pick, refresh: () => panel.refresh(), setReplay: (at) => { layer.setReplay(at); count(); }, fmt: fmtDateTime };
}
