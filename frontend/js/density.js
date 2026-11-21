// H3 density heatmap: hexagon polygons in one Primitive per layer, per-instance colour.
import { fetchJson } from "/static/js/api.js";
const C = Cesium;
// viridis-like perceptual ramp
const RAMP = ["#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c", "#28ae80", "#5ec962", "#addc30", "#fde725"];
const RAMP_C = RAMP.map((h) => C.Color.fromCssColorString(h));

export function ramp(t) {
  const x = Math.min(1, Math.max(0, t)) * (RAMP_C.length - 1);
  const i = Math.min(RAMP_C.length - 2, Math.floor(x));
  return C.Color.lerp(RAMP_C[i], RAMP_C[i + 1], x - i, new C.Color());
}

export class DensityLayer {
  constructor(scene, layer) {
    this.scene = scene; this.layer = layer;
    this.prim = null; this.on = false; this.res = 3; this.timer = null; this.seq = 0;
  }
  _clear() { if (this.prim) { this.scene.primitives.remove(this.prim); this.prim = null; } }
  async load() {
    const seq = ++this.seq;
    try {
      const d = await fetchJson("/api/density?layer=" + this.layer + "&res=" + this.res + "&hours=24", 30000);
      if (seq !== this.seq || !this.on) return;
      const cells = d.cells || [];
      const max = Math.log(1 + Math.max(1, ...cells.map((c) => c.count)));
      const instances = [];
      for (const c of cells) {
        const b = c.boundary || [];
        if (b.length < 3) continue;
        const lons = b.map((p) => p[1]);
        if (Math.max(...lons) - Math.min(...lons) > 180) continue; // antimeridian cell
        instances.push(new C.GeometryInstance({
          geometry: new C.PolygonGeometry({
            polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(b.flatMap((p) => [p[1], p[0]]))),
            vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT,
          }),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(ramp(Math.log(1 + c.count) / max).withAlpha(0.45)) },
          id: { density: this.layer, h3: c.h3, count: c.count },
        }));
      }
      this._clear();
      if (!instances.length) return;
      this.prim = this.scene.primitives.add(new C.Primitive({
        geometryInstances: instances,
        appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false }),
        asynchronous: false,
      }));
      this.scene.requestRender();
    } catch (e) { /* keep previous */ }
  }
  setOn(on) {
    this.on = on;
    clearInterval(this.timer);
    if (on) { this.load(); this.timer = setInterval(() => this.load(), 300000); }
    else { this.seq++; this._clear(); this.scene.requestRender(); }
  }
  setRes(res) { this.res = res; if (this.on) this.load(); }
}
