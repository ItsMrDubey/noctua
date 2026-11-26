// Intel console tile for sensor-derived events (section "detection" of GET /api/intel): the
// signal itself instead of news-shaped evidence. GNSS interference: detection / peak / now
// readings with their windows and 95% Wilson intervals, the event and neighbouring H3 cells
// drawn with their live level, the % degraded series, history at the location and the
// degraded aircraft with their NIC / NACp. Internet outage: IODA events per datasource with
// their weight. Squawk: the episode (reports, duration, code now) and a pivot to the aircraft.
// Radiation: nearby probes vs their own median ("*" = provisional, short history).
import { esc, nf } from "/static/js/ui.js";
import { fmtUtc, kmText, chip } from "/static/js/panels/dossier-kit.js";

const empty = (t) => '<div class="dz-empty">' + esc(t) + "</div>";
const pct = (v) => (v == null ? "–" : Math.round(v) + "%");
const pivot = (spec) => " data-pivot=\"" + esc(JSON.stringify(spec)) + "\"";
const list = (rows) => '<ul class="cx-l">' + rows.join("") + "</ul>";
function area(vals, { w = 300, h = 40, max = null, cls = "" } = {}) {
  const v = vals.map((x) => (x == null ? 0 : +x));
  if (!v.length) return "";
  const M = max ?? Math.max(1, ...v), dx = w / Math.max(1, v.length - 1);
  const pts = v.map((x, i) => (i * dx).toFixed(1) + "," + (h - (x / M) * (h - 2)).toFixed(1)).join(" ");
  return '<svg class="ic-area ' + cls + '" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true"><polygon points="0,' + h + " " + pts + " " + w + "," + h + '"/><polyline points="' + pts + '"/></svg>';
}

const hhmm = (iso) => (iso ? fmtUtc(iso, { time: true }) : "–");
const SIG_LV = { high: "#ff4b55", medium: "#f2ad3d", low: "#4fdf8c" };
function readingRow(label, r, extra = "") {
  if (!r) return "";
  const v = r.unrecorded ? "reading not recorded" : r.n ? r.bad + " of " + r.n + " aircraft · " + pct(r.pct) : "no aircraft reporting";
  return "<tr><td>" + esc(label) + "</td><td>" + esc(fmtUtc(r.t)) + "</td><td>" + esc((r.window_min || 60) + "-min window") + "</td><td>" + esc(v) + "</td><td>" +
    (r.wilson_lo != null && !r.unrecorded ? esc(nf(r.wilson_lo, 0) + "–" + nf(r.wilson_hi, 0) + "%") : "–") + "</td><td>" + extra + "</td></tr>";
}
// H3 cells (event cells outlined) on a local equirectangular plot, filled by the live level
function cellMap(cells) {
  const pts = cells.flatMap((c) => c.boundary || []);
  if (!pts.length) return "";
  const W = 320, H = 220, pad = 8;
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const [lo, la] of pts) { x0 = Math.min(x0, lo); x1 = Math.max(x1, lo); y0 = Math.min(y0, la); y1 = Math.max(y1, la); }
  const k = Math.cos((((y0 + y1) / 2) * Math.PI) / 180), sx = (x1 - x0) * k || 0.01, sy = y1 - y0 || 0.01, sc = Math.min((W - 2 * pad) / sx, (H - 2 * pad) / sy);
  const X = (lo) => pad + (lo - x0) * k * sc + ((W - 2 * pad) - sx * sc) / 2, Y = (la) => H - pad - (la - y0) * sc - ((H - 2 * pad) - sy * sc) / 2;
  const g = cells.slice().sort((a, b) => (a.role === "event") - (b.role === "event")).map((c) => {
    const col = c.level ? SIG_LV[c.level] : "#5b6573";
    const tip = (c.role === "event" ? "Event cell " : "Neighbour ") + c.h3 + " · " + (c.n ? c.bad + "/" + c.n + " aircraft degraded (" + pct(c.pct) + ") now" : "no live data (fewer than 3 aircraft)");
    return '<polygon points="' + c.boundary.map(([lo, la]) => X(lo).toFixed(1) + "," + Y(la).toFixed(1)).join(" ") + '" style="fill:' + col + ";fill-opacity:" + (c.n ? 0.12 + Math.min(0.5, (c.pct || 0) / 80) : 0.05) +
      ";stroke:" + (c.role === "event" ? "#e8eef6" : col) + ";stroke-width:" + (c.role === "event" ? 1.8 : 0.8) + '" data-fly="' + c.lat + "," + c.lon + '"><title>' + esc(tip) + "</title></polygon>" +
      '<text x="' + X(c.lon).toFixed(1) + '" y="' + (Y(c.lat) + 3).toFixed(1) + '" text-anchor="middle" style="font:11px var(--f-mono);fill:var(--fg-2)">' + (c.n ? esc(pct(c.pct) + " · " + c.n) : "–") + "</text>";
  });
  return '<svg class="ic-cells" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="H3 cells">' + g.join("") + "</svg>";
}
function pctSeries(rows, title) {
  const v = rows.map((r) => (r.pct == null ? 0 : r.pct));
  if (v.length < 2) return "";
  return '<div class="ic-chart" title="' + esc(title) + '">' + area(v, { max: Math.max(20, ...v), cls: "tempo" }) + "<span>" + esc(fmtUtc(rows[0].t)) + "</span><span>" + esc(fmtUtc(rows[rows.length - 1].t)) + "</span></div>";
}
export function detection(X) {
  if (X === null) return empty("Not a sensor-derived event");
  if (!X) return null;
  if (X.error) return empty(X.error);
  const a = X.assessment || {}, R = X.readings || {};
  let h = '<div class="ip-types">' + chip("Sensor-derived · " + X.sensor, "src") + (X.lifecycle ? chip(X.lifecycle, X.lifecycle === "active" ? "warn" : "ok", "Lifecycle from the latest reading") : "") +
    (a.wording ? chip(a.wording + " · conf " + nf(a.confidence, 2), a.wording === "likely" ? "warn" : "src", (a.limits || []).join("; ") || "all 'likely' criteria met") : "") + "</div>";
  const G = X.gnss;
  if (R.detection) {
    h += '<div class="ic-scroll"><table class="ip-t"><thead><tr><th>Reading</th><th>Time (UTC)</th><th>Window</th><th>Degraded</th><th title="95% Wilson interval of the degraded share">95% CI</th><th></th></tr></thead><tbody>' +
      readingRow("At detection", R.detection) + (R.peak && R.peak.t !== R.detection.t ? readingRow(R.peak.legacy ? "Last hot" : "Peak", R.peak) : "") +
      (R.latest && R.latest.t !== (R.peak || R.detection).t ? readingRow("Now", R.latest, esc(X.lifecycle || "")) : "") + "</tbody></table></div>";
    if (a.windows != null) h += '<div class="ip-hist">Degraded in ' + nf(a.windows) + " non-overlapping 60-min window(s); best window " + nf(a.bad) + "/" + nf(a.n) + " aircraft (95% lower bound " + nf(a.wilson_lo, 0) + "%)." + (a.wording !== "likely" ? " Not 'likely': " + esc((a.limits || []).join("; ")) + "." : "") + "</div>";
  }
  if (G) {
    h += '<div class="cx-h">Cells <small>' + esc(G.cell_size) + " · live " + esc(G.live_updated ? fmtUtc(new Date(G.live_updated * 1000).toISOString(), { time: true }) : "–") + ", last " + nf(G.live_window_min) + " min</small></div>" + cellMap(G.cells || []) +
      '<div class="ic-legend"><i style="background:#ff4b55"></i>high <i style="background:#f2ad3d"></i>medium <i style="background:#4fdf8c"></i>low <i style="background:#5b6573"></i>no data · white outline = event cell</div>';
    if ((G.series || []).length > 1) h += '<div class="cx-h">% degraded, event cells <small>10-min readings since detection</small></div>' + pctSeries(G.series, "Share of aircraft with degraded NIC/NACp in the event cells (60-min windows)");
    const hist = G.history || {};
    if (hist.error) h += empty("History unavailable: " + hist.error);
    else {
      if ((hist.series || []).length > 1) h += '<div class="cx-h">Hourly, last 72 h <small>closed hourly buckets</small></div>' + pctSeries(hist.series, hist.basis || "");
      h += '<div class="ip-hist' + (G.chronic ? " hot" : "") + '" title="' + esc(hist.basis || "") + '">History here: ' + (hist.hours_observed ? nf(hist.hours_degraded) + " of " + nf(hist.hours_observed) + " observed hours degraded since " + esc(fmtUtc(hist.first_hour)) : "no hourly history recorded yet") +
        " · " + nf((G.prior_events || []).length) + " earlier GNSS event(s) within 2 cells, 30 d" + (G.chronic ? " · CHRONIC interference area" : "") + "</div>";
    }
    const ac = G.aircraft || [];
    h += '<div class="cx-h">Degraded aircraft <small>' + (ac.length ? "at " + esc(hhmm(G.aircraft_at)) + " · NIC < " + nf((G.thresholds || {}).nic_min) + " or NACp < " + nf((G.thresholds || {}).nacp_min) + " or GPS lost" : "") + "</small></div>" +
      (ac.length ? '<div class="ic-scroll"><table class="ip-t"><thead><tr><th>ICAO hex</th><th>NIC</th><th>NACp</th><th>GPS lost</th><th>Cell</th></tr></thead><tbody>' + ac.map((x) =>
        "<tr" + pivot({ type: "intel", kind: "aircraft", id: "air:" + x.hex, label: x.hex.toUpperCase() }) + "><td>" + esc(x.hex.toUpperCase()) + "</td><td>" + esc(x.nic ?? "–") + "</td><td>" + esc(x.nac_p ?? "–") + "</td><td>" + (x.gps_lost ? "yes" : "no") + "</td><td>" + esc((x.cell || "").slice(0, 7)) + "</td></tr>").join("") + "</tbody></table></div>"
        : empty("Aircraft list not recorded for this detection (stored before per-aircraft NIC/NACp was kept)"));
  }
  const O = X.outage;
  if (O) {
    const st = O.country || {};
    h += '<div class="cx-h" title="' + esc(O.basis) + '">IODA ' + esc(O.cc || "") + " <small>" + esc(st.label || "") + "</small></div>" +
      (O.events.length ? '<div class="ic-scroll"><table class="ip-t"><thead><tr><th>Datasource</th><th>Weight</th><th>Score</th><th>From</th><th>Until</th></tr></thead><tbody>' + O.events.map((e) =>
        "<tr><td>" + esc(e.label + " (" + e.datasource + ")") + "</td><td>" + nf(e.weight, 2) + "</td><td>" + nf(e.score) + "</td><td>" + esc(fmtUtc(e.from)) + "</td><td>" + esc(e.ongoing ? "ongoing" : fmtUtc(e.until)) + "</td></tr>").join("") + "</tbody></table></div>" : empty("No IODA events for " + (O.cc || "") + " in the live window"));
  }
  const Q = X.squawk;
  if (Q) {
    const e = Q.episode, d = X.details || {};
    h += '<div class="ic-kv"><div><b>' + esc(d.code || (e && e.code) || "–") + "</b><span>" + esc((e && e.label) || "squawk") + "</span></div><div><b>" + nf(d.reports || (e && e.reports)) + "</b><span>reports</span></div><div><b>" + esc(d.duration_s != null ? Math.round(d.duration_s / 60) + " min" : "–") + "</b><span>" + esc(hhmm(d.first_seen) + " → " + hhmm(d.last_seen)) + "</span></div><div><b>" + esc(d.squawk_now || (e && e.squawk) || "–") + "</b><span>transponder code now</span></div></div>" +
      '<button type="button" class="ic-showall"' + pivot({ type: "intel", kind: "aircraft", id: Q.track_id, label: d.flight || Q.track_id }) + ">Open the aircraft (flight, profile)</button>";
  }
  const RA = X.radiation;
  if (RA) {
    const d = X.details || {}, b = d.baseline || [];
    if (d.value != null) h += '<div class="ip-hist">At detection ' + esc(fmtUtc(d.ts)) + ": " + nf(d.value, 3) + " " + esc(d.unit || "µSv/h") + (b[0] != null ? " vs median " + nf(b[0], 3) + " over " + nf(b[1]) + " readings (" + nf(d.value / b[0], 1) + "×) · " + esc(d.status || "") : "") + "</div>";
  }
  if (RA) h += '<div class="cx-h">Probes ≤100 km, now <small>' + RA.stations.length + "</small></div>" + list(RA.stations.map((s) => "<li><i class=\"at\">" + esc(s.status + (s.provisional ? "*" : "")) + "</i><b>" + esc(s.name || s.id) + "</b><span>" + nf(s.value, 3) + " " + esc(s.unit || "") + (s.baseline != null ? " · median " + nf(s.baseline, 3) + " (" + s.baseline_n + ")" : "") + " · " + esc(kmText(s.distance_km)) + "</span></li>"));
  return h;
}
