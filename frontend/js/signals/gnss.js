// GNSS interference map (gpsjam.org style): H3 cells coloured by the share of aircraft reporting
// degraded GPS navigation accuracy. One batched Primitive for the fills, one PolylineCollection for
// the outlines of elevated cells and the (dashed) watch-region coverage circles.
import { fetchJson } from "/static/js/api.js";

const C = Cesium;
export const GNSS_LEVELS = { low: ["Normal < 2 %", "#3ddc84", 0.16], medium: ["Elevated 2-10 %", "#ffd24a", 0.36], high: ["Jamming > 10 %", "#ff4b55", 0.55] };
const toRad = Math.PI / 180;

function ring(lat, lon, km, step = 6) {
  const pts = [], r = km / 6371, la = lat * toRad, lo = lon * toRad;
  for (let a = 0; a <= 360; a += step) {
    const br = a * toRad;
    const la2 = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(br));
    const lo2 = lo + Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(la2));
    pts.push(lo2 / toRad, la2 / toRad);
  }
  return pts;
}

export class GnssLayer {
  constructor(scene) {
    this.scene = scene; this.on = false; this.hours = 1; this.regionsOn = true;
    this.prim = null; this.cells = []; this.byCell = new Map(); this.meta = {};
    this.lines = scene.primitives.add(new C.PolylineCollection());
    this.regionLines = scene.primitives.add(new C.PolylineCollection());
    this.timer = 0; this.seq = 0;
  }
  req() { this.scene.requestRender(); }
  async load() {
    const seq = ++this.seq;
    const d = await fetchJson("/api/signals/gnss?hours=" + this.hours, 30000);
    if (seq !== this.seq || !this.on) return;
    this.meta = d;
    this.cells = d.cells || [];
    this.byCell = new Map(this.cells.map((c) => [c.h3, c]));
    const inst = [];
    this.lines.removeAll();
    for (const c of this.cells) {
      const b = c.b || [];
      if (b.length < 6) continue;
      let mn = 999, mx = -999;
      for (let i = 0; i < b.length; i += 2) { mn = Math.min(mn, b[i]); mx = Math.max(mx, b[i]); }
      if (mx - mn > 180) continue; // antimeridian cell
      const [, col, a] = GNSS_LEVELS[c.level] || GNSS_LEVELS.low;
      const color = C.Color.fromCssColorString(col);
      // fewer aircraft -> fainter (a cell of 3 is weaker evidence than one of 30)
      const alpha = a * (0.55 + 0.45 * Math.min(1, c.n / 15));
      inst.push(new C.GeometryInstance({
        geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(b)), vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(color.withAlpha(alpha)) },
        id: { signal: "gnss", cell: c.h3 },
      }));
      if (c.level !== "low") this.lines.add({ positions: C.Cartesian3.fromDegreesArray([...b, b[0], b[1]]), width: c.level === "high" ? 1.6 : 1, material: C.Material.fromType("Color", { color: color.withAlpha(0.9) }) });
    }
    if (this.prim) { this.scene.primitives.remove(this.prim); this.prim = null; }
    if (inst.length) {
      this.prim = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false }), asynchronous: false }));
    }
    this.regionLines.removeAll();
    const rc = C.Color.fromCssColorString("#66798c").withAlpha(0.55);
    for (const r of d.regions || []) {
      this.regionLines.add({ positions: C.Cartesian3.fromDegreesArray(ring(r.lat, r.lon, (r.dist_nm || 250) * 1.852)), width: 1, material: C.Material.fromType("PolylineDash", { color: rc, dashLength: 12 }) });
    }
    this._show();
    if (this.onData) this.onData(this);
  }
  _show() {
    if (this.prim) this.prim.show = this.on;
    this.lines.show = this.on;
    this.regionLines.show = this.on && this.regionsOn;
    this.req();
  }
  setOn(on) {
    this.on = on;
    clearInterval(this.timer);
    if (on) { this.load().catch((e) => { if (this.onError) this.onError(e); }); this.timer = setInterval(() => this.load().catch(() => {}), 120000); }
    else this.seq++;
    this._show();
  }
  setHours(h) { this.hours = h; if (this.on) this.load().catch(() => {}); }
  setRegions(on) { this.regionsOn = on; this._show(); }
  get hot() { return this.cells.filter((c) => c.level === "high").length; }
}
