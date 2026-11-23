// Historical armed-conflict events (UCDP GED backfill, status "historical") as small muted points.
// Density-friendly: one PointPrimitiveCollection of individual points (built once per load), plus a
// grid-aggregated collection shown from high up when the set is large. Replay filters by time.
import { fetchJson } from "/static/js/api.js";
import { esc, nf, kv } from "/static/js/ui.js";

const C = Cesium;
const LIMIT = 10000;            // /api/events max page
const AGG_MIN = 2500;           // aggregate above this many items...
const AGG_EYE_M = 2.5e6;        // ...when the camera is higher than this
const CELL_DEG = 2;
const MUTED = C.Color.fromCssColorString("#8f86c9").withAlpha(0.6);
const MUTED_CELL = C.Color.fromCssColorString("#8f86c9").withAlpha(0.42);
const OUTLINE = C.Color.BLACK.withAlpha(0.5);
const PT_SCALE = new C.NearFarScalar(3e5, 1.4, 1.5e7, 0.8);
const tsOf = (s) => { const t = Date.parse(s); return isNaN(t) ? 0 : t; };

export class HistoricalLayer {
  constructor(viewer) {
    this.viewer = viewer; this.scene = viewer.scene;
    this.pts = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this.agg = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this.pts.show = this.agg.show = false;
    this.on = false; this.items = []; this.since = null; this.capped = false; this.total = null;
    this.replayAt = null; this.windowMs = 0; this.visibleN = 0; this.gen = 0;
    this.onData = null;
    this.scene.camera.moveEnd.addEventListener(() => { if (this.on) this._mode(); });
  }
  setOn(b) {
    this.on = !!b;
    if (!this.on) { this.pts.show = this.agg.show = false; this.scene.requestRender(); return Promise.resolve(); }
    return this.load();
  }
  // start of the timeline range; reloads when on and the range changed
  setSince(d) {
    const changed = !this.since || Math.abs(this.since - d) > 3600e3;
    this.since = d;
    return changed && this.on ? this.load() : Promise.resolve();
  }
  // replay time + window: only events overlapping [at - window, at] are drawn
  setReplay(at, windowMs) {
    this.replayAt = at ? at.getTime() : null; this.windowMs = windowMs || 0;
    if (this.items.length) this._filter();
  }
  async load() {
    const g = ++this.gen;
    const since = this.since ? "&since=" + encodeURIComponent(this.since.toISOString()) : "";
    const [j, st] = await Promise.all([
      fetchJson("/api/events?status=historical&limit=" + LIMIT + since, 60000),
      this.total == null ? fetchJson("/api/events/stats", 20000).catch(() => null) : Promise.resolve(null),
    ]);
    if (g !== this.gen) return;
    if (st && st.by_status) this.total = st.by_status.historical || 0;
    this.items = ((j && j.items) || []).filter((it) => it && it.lat != null && it.lon != null);
    for (const it of this.items) { it._t0 = tsOf(it.t_start); it._t1 = tsOf(it.t_end) || it._t0; }
    this.capped = ((j && j.items) || []).length >= LIMIT;
    this.pts.removeAll();
    for (const it of this.items) {
      this.pts.add({ position: C.Cartesian3.fromDegrees(it.lon, it.lat, 0), pixelSize: 4, color: MUTED, outlineColor: OUTLINE, outlineWidth: 1,
        scaleByDistance: PT_SCALE, id: { ref: true, kind: "historical", item: it } });
    }
    this._filter();
  }
  _in(it) { return this.replayAt == null || (it._t0 <= this.replayAt && it._t1 >= this.replayAt - this.windowMs); }
  _filter() {
    let n = 0;
    for (let i = 0; i < this.pts.length; i++) {
      const p = this.pts.get(i), v = this._in(p.id.item);
      if (p.show !== v) p.show = v;
      if (v) n++;
    }
    this.visibleN = n;
    // aggregate cells over the currently visible items
    this.agg.removeAll();
    if (n > AGG_MIN) {
      const grid = new Map();
      for (const it of this.items) {
        if (!this._in(it)) continue;
        const k = Math.floor((it.lon + 180) / CELL_DEG) + ":" + Math.floor((it.lat + 90) / CELL_DEG);
        let c = grid.get(k);
        if (!c) grid.set(k, (c = { n: 0, lat: 0, lon: 0 }));
        c.n++; c.lat += it.lat; c.lon += it.lon;
      }
      for (const c of grid.values()) {
        const lat = c.lat / c.n, lon = c.lon / c.n;
        this.agg.add({ position: C.Cartesian3.fromDegrees(lon, lat, 0), pixelSize: Math.min(22, 4 + 2.2 * Math.log2(c.n)), color: MUTED_CELL,
          outlineColor: OUTLINE, outlineWidth: 1, id: { histCell: { lat, lon, n: c.n } } });
      }
    }
    this._mode();
    if (this.onData) this.onData();
  }
  _mode() {
    const agg = this.on && this.visibleN > AGG_MIN && this.viewer.camera.positionCartographic.height > AGG_EYE_M;
    this.agg.show = agg; this.pts.show = this.on && !agg;
    this.scene.requestRender();
  }
  zoomCell(c) {
    this.viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(c.lon, c.lat, Math.min(AGG_EYE_M * 0.8, 1.5e6)), duration: 0.9 });
  }
  note() {
    if (!this.on) return "";
    const n = this.items.length;
    if (!n) return this.total === 0 ? "No UCDP events loaded (run the ucdp backfill)" : "No UCDP events in this range";
    const shown = this.replayAt != null ? nf(this.visibleN) + " at replay time · " : "";
    return shown + (this.capped ? "showing " + nf(n) + " most recent of " + (this.total ? nf(this.total) + " archived" : "≥ " + nf(n)) : nf(n) + " in range");
  }
}

// [title, bodyHtml] for the inspector; `detail` is GET /api/events/{id} once loaded
export function historicalDetails(it, detail) {
  const d = detail || it;
  const span = (a, b) => { const x = String(a || "").slice(0, 10), y = String(b || "").slice(0, 10); return esc(x) + (x === y || !y ? "" : " → " + esc(y)); };
  const fields = (d.fields || []).map((f) => [String(f.field).replace(/_/g, " "), esc(f.value)]);
  const body = kv([["Dates", span(it.t_start, it.t_end)], ["Place", esc([d.place_name, d.country].filter(Boolean).join(", "))], ["Precision", esc(it.precision)],
    ["Position", nf(it.lat, 3) + ", " + nf(it.lon, 3)], ...fields, ["ID", esc(it.id)]]) +
    (d.summary_en || d.summary ? '<div class="evsum" style="margin-top:8px">' + esc(d.summary_en || d.summary) + "</div>" : "") +
    '<div class="prov" style="margin-top:8px"><div><i></i>' + (String(it.id).startsWith("acled-")
      ? "Armed Conflict Location &amp; Event Data (ACLED), curated"
      : "Uppsala Conflict Data Program, Georeferenced Event Dataset (curated, historical)") + "</div></div>";
  return [(d.title_en || it.title_en || it.title || d.title) || "Historical conflict event", body];
}
