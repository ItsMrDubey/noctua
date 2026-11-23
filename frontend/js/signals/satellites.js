// Satellites: CelesTrak element sets (via /api/signals/satellites) propagated with SGP4 in a web
// worker; one PointPrimitiveCollection for all objects (thousands), positions refreshed every
// 1-3 s. Selecting one draws its orbit ring, ground track and coverage footprint.
import { fetchJson } from "/static/js/api.js";

const C = Cesium;
export const SAT_GROUPS = {
  stations: ["Stations", "#ffffff"], military: ["Military", "#ff4b55"], gnss: ["GNSS", "#4fdf8c"], weather: ["Weather", "#5ec8ff"],
  "last-30-days": ["New 30 d", "#ff9f40"], geo: ["GEO", "#ffd24a"], visual: ["Bright", "#d3dee8"],
  starlink: ["Starlink", "#8fa3b8"], oneweb: ["OneWeb", "#7f8fb0"], kuiper: ["Kuiper", "#c9a7ff"], active: ["All active", "#6f8396"],
};
export const SAT_DEFAULT_ON = ["stations", "military", "gnss", "weather"];
const RE_KM = 6371.0;
const MIN_ELEV = 10; // footprint: ground area that sees the satellite at least this high (deg)
const toRad = Math.PI / 180;
const SCALE = new C.NearFarScalar(1e6, 1.6, 4e7, 0.7);
const colorOf = (groups) => C.Color.fromCssColorString((SAT_GROUPS[Object.keys(SAT_GROUPS).find((g) => groups.includes(g))] || SAT_GROUPS.active)[1]);

// coverage footprint radius (km of ground arc) for an altitude (km) and minimum elevation
export function footprintKm(altKm, minElevDeg = MIN_ELEV) {
  const e = minElevDeg * toRad;
  const lam = Math.acos((RE_KM * Math.cos(e)) / (RE_KM + Math.max(0, altKm))) - e;
  return Math.max(0, lam) * RE_KM;
}

function circle(lat, lon, km, step = 4) {
  const pts = [], r = km / RE_KM, la = lat * toRad, lo = lon * toRad;
  for (let a = 0; a <= 360; a += step) {
    const br = a * toRad;
    const la2 = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(br));
    const lo2 = lo + Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(la2));
    pts.push(((lo2 / toRad + 540) % 360) - 180, la2 / toRad);
  }
  return pts;
}

export class SatelliteLayer {
  constructor(viewer, { onSelect } = {}) {
    this.viewer = viewer; this.scene = viewer.scene; this.onSelect = onSelect;
    this.on = false; this.groups = new Set(SAT_DEFAULT_ON);
    this.items = []; this.fields = []; this.byNorad = new Map();
    this.points = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this.sel = this.scene.primitives.add(new C.PolylineCollection());
    this.labels = this.scene.primitives.add(new C.LabelCollection());
    this.selected = null; this.info = null; this.error = null; this.meta = null;
    this.worker = null; this.timer = 0; this.busy = false; this.seq = 0;
    this._scratch = new C.Cartesian3();
  }
  req() { this.scene.requestRender(); }
  _worker() {
    if (this.worker) return this.worker;
    this.worker = new Worker("/static/js/signals/satworker.js");
    this.worker.onmessage = (e) => this._msg(e.data);
    this.worker.onerror = (e) => { this.error = "worker: " + (e.message || "failed"); if (this.onStatus) this.onStatus(); };
    return this.worker;
  }
  _msg(m) {
    if (m.type === "error") { this.error = m.error; if (this.onStatus) this.onStatus(); return; }
    if (m.type === "loaded") { this.failed = m.failed; this._tick(); return; }
    if (m.type === "pos") {
      this.busy = false;
      const e = m.ecef, n = Math.min(this.items.length, e.length / 3), P = this._scratch;
      for (let i = 0; i < n; i++) {
        const pt = this.items[i]._pt, x = e[i * 3];
        if (!pt) continue;
        if (x !== x) { pt.show = false; continue; } // NaN: decayed / no solution
        P.x = x; P.y = e[i * 3 + 1]; P.z = e[i * 3 + 2];
        pt.position = P;
        pt.show = this.on;
      }
      if (this.selected && this.labels.length) this.labels.get(0).position = this.selected._pt.position;
      this.req();
      return;
    }
    if (m.type === "track" && this.selected && this.items[m.idx] === this.selected) this._drawTrack(m);
  }
  async load() {
    const seq = ++this.seq;
    const g = [...this.groups].join(",") || "none";
    const d = await fetchJson("/api/signals/satellites?groups=" + encodeURIComponent(g), 60000);
    if (seq !== this.seq) return;
    this.meta = d;
    this.fields = d.fields || [];
    this.points.removeAll();
    this.items = (d.items || []).map((row) => {
      const it = { row, norad: row[0], name: row[1], intl: row[2], epoch: row[3], groups: row[row.length - 1] || [] };
      it._pt = this.points.add({ pixelSize: 3.5, color: colorOf(it.groups), outlineColor: C.Color.BLACK.withAlpha(0.6), outlineWidth: 1, scaleByDistance: SCALE, show: false, id: { signal: "sat", norad: it.norad } });
      return it;
    });
    this.byNorad = new Map(this.items.map((it, i) => [it.norad, i]));
    if (this.selected) {
      const i = this.byNorad.get(this.selected.norad);
      this.selected = i == null ? null : this.items[i];
      if (!this.selected) this.clearSelection();
    }
    this._worker().postMessage({ type: "load", items: this.items.map((it) => it.row.slice(0, 13)) });
    if (this.onStatus) this.onStatus();
  }
  _tick() {
    if (!this.on || !this.worker || this.busy || !this.items.length) return;
    this.busy = true;
    this.worker.postMessage({ type: "tick", t: Date.now() });
    if (this.selected) this._requestTrack();
  }
  _loop() {
    clearInterval(this.timer);
    if (!this.on) return;
    const every = this.items.length > 4000 ? 3000 : this.items.length > 1500 ? 2000 : 1000;
    this.timer = setInterval(() => this._tick(), every);
  }
  async setOn(on) {
    this.on = on;
    this.points.show = on; this.sel.show = on; this.labels.show = on;
    if (on) { await this.load(); this._loop(); }
    else { clearInterval(this.timer); this.clearSelection(); }
    this.req();
  }
  async setGroups(set) { this.groups = set; if (this.on) { await this.load(); this._loop(); } }
  get count() { return this.items.length; }
  pick(id) {
    const i = this.byNorad.get(id.norad);
    if (i == null) return false;
    this.select(this.items[i]);
    return true;
  }
  select(it) {
    if (this.selected && this.selected._pt) { this.selected._pt.pixelSize = 3.5; this.selected._pt.color = colorOf(this.selected.groups); }
    this.selected = it; this.info = null;
    it._pt.pixelSize = 9; it._pt.color = C.Color.WHITE;
    this.labels.removeAll();
    this.labels.add({ position: it._pt.position, text: it.name, font: "11px 'JetBrains Mono', Consolas, monospace", fillColor: C.Color.WHITE,
      outlineColor: C.Color.BLACK, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(10, -10), disableDepthTestDistance: 0 });
    this._requestTrack();
    if (this.onSelect) this.onSelect(it);
  }
  _requestTrack() {
    const it = this.selected;
    if (!it || !this.worker) return;
    const idx = this.items.indexOf(it), mm = it.row[4] || 15; // revolutions / day
    const period = 1440 / mm;
    this._trackAt = Date.now();
    this.worker.postMessage({ type: "track", idx, t: Date.now(), minutes: Math.min(period, 1440), steps: period > 600 ? 240 : 180 });
  }
  _drawTrack(m) {
    this.sel.removeAll();
    this.info = m.info;
    if (!m.ecef) return;
    const ring = [], ground = [];
    for (let k = 0; k < m.ecef.length / 3; k++) {
      if (m.ecef[k * 3] === m.ecef[k * 3]) ring.push(new C.Cartesian3(m.ecef[k * 3], m.ecef[k * 3 + 1], m.ecef[k * 3 + 2]));
    }
    // ground track: split where it crosses the antimeridian
    let seg = [];
    for (let k = 0; k < m.geo.length / 3; k++) {
      const lat = m.geo[k * 3], lon = m.geo[k * 3 + 1];
      if (lat !== lat) continue;
      if (seg.length && Math.abs(lon - seg[seg.length - 2]) > 180) { ground.push(seg); seg = []; }
      seg.push(lon, lat);
    }
    if (seg.length) ground.push(seg);
    const col = colorOf(this.selected.groups);
    if (ring.length > 1) this.sel.add({ positions: ring, width: 1.4, material: C.Material.fromType("Color", { color: col.withAlpha(0.75) }) });
    for (const s of ground) if (s.length >= 4) this.sel.add({ positions: C.Cartesian3.fromDegreesArray(s), width: 1.2, material: C.Material.fromType("PolylineDash", { color: col.withAlpha(0.55), dashLength: 10 }) });
    if (m.info) {
      const km = footprintKm(m.info.alt);
      this.footprintKm = km;
      if (km > 1) this.sel.add({ positions: C.Cartesian3.fromDegreesArray(circle(m.info.lat, m.info.lon, Math.min(km, 9000))), width: 1.6, material: C.Material.fromType("Color", { color: C.Color.fromCssColorString("#5ee3ea").withAlpha(0.8) }) });
    }
    this.req();
    if (this.onTrack) this.onTrack(this.selected, m.info);
  }
  clearSelection() {
    if (this.selected && this.selected._pt) { this.selected._pt.pixelSize = 3.5; this.selected._pt.color = colorOf(this.selected.groups); }
    this.selected = null; this.info = null;
    this.sel.removeAll(); this.labels.removeAll();
    this.req();
  }
}
