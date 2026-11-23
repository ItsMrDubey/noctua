// Signal overlays: emergency squawks (billboards + labels), internet outages (country polygons),
// radiation dose rate (points), OVATION aurora (one translucent imagery layer). Each one is a
// small batched primitive set with its own refresh timer while switched on.
import { fetchJson } from "/static/js/api.js";
import { LOD } from "/static/js/lod.js";

const C = Cesium;
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const svgUrl = (svg) => "data:image/svg+xml;utf8," + encodeURIComponent(svg);

class Timed {
  constructor(scene, every) { this.scene = scene; this.every = every; this.on = false; this.timer = 0; this.seq = 0; this.items = []; }
  req() { this.scene.requestRender(); }
  setOn(on) {
    this.on = on;
    clearInterval(this.timer);
    if (on) { this.refresh(); this.timer = setInterval(() => this.refresh(), this.every); } else this.seq++;
    this._show();
  }
  refresh() { return this.load().catch((e) => { if (this.onError) this.onError(e); }); }
}

// ---- emergency squawks ----
export const SQK_COLOR = { 7500: "#ff2bd6", 7600: "#ffb020", 7700: "#ff3b3b" };
const sqColor = (code) => SQK_COLOR[code] || "#ff8a3d";
const sqIcon = (col, active) => svgUrl('<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 26 26" fill="none" stroke="' + col + '" stroke-width="1.6"' + (active ? "" : ' opacity=".5"') + ">" +
  '<path d="M13 2l11 11-11 11L2 13z"/><path d="M13 8v6M13 17v1.2"/>' + (active ? '<circle cx="13" cy="13" r="12.2" stroke-width=".7" opacity=".6"/>' : "") + "</svg>");

export class SquawkLayer extends Timed {
  constructor(scene) {
    super(scene, 60000);
    this.bb = scene.primitives.add(new C.BillboardCollection({ scene }));
    this.lb = scene.primitives.add(new C.LabelCollection());
    this.byId = new Map();
  }
  async load() {
    const seq = ++this.seq;
    const d = await fetchJson("/api/signals/squawks", 15000);
    if (seq !== this.seq) return;
    this.items = (d.items || []).filter((e) => e.lat != null);
    this.byId = new Map(this.items.map((e) => [e.id, e]));
    this.bb.removeAll(); this.lb.removeAll();
    for (const e of this.items) {
      const pos = C.Cartesian3.fromDegrees(e.lon, e.lat, Math.max(0, (e.alt_ft || 0) * 0.3048));
      this.bb.add({ position: pos, image: sqIcon(sqColor(e.code), e.active), width: 24, height: 24, id: { signal: "squawk", id: e.id }, disableDepthTestDistance: 2e6 });
      this.lb.add({ position: pos, text: e.code + " " + (e.flight || e.reg || e.hex.toUpperCase()), font: FONT, fillColor: C.Color.fromCssColorString(sqColor(e.code)),
        outlineColor: C.Color.BLACK, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(15, 0), horizontalOrigin: C.HorizontalOrigin.LEFT,
        translucencyByDistance: new C.NearFarScalar(2e6, 1, 1.2e7, e.active ? 0.8 : 0), disableDepthTestDistance: 2e6 });
    }
    this._show();
    if (this.onData) this.onData(this);
  }
  _show() { this.bb.show = this.on; this.lb.show = this.on; this.req(); }
  get active() { return this.items.filter((e) => e.active).length; }
}

// ---- internet outages (IODA) ----
export const OUTAGE_COLORS = { high: "#ff3fd5", medium: "#c86bff", low: "#7c6bff" };
export class OutageLayer extends Timed {
  constructor(scene) {
    super(scene, 600000);
    this.prim = null; this.byCc = new Map();
    this.lb = scene.primitives.add(new C.LabelCollection());
    this.edges = scene.primitives.add(new C.PolylineCollection());
  }
  async load() {
    const seq = ++this.seq;
    const d = await fetchJson("/api/signals/outages?shapes=1", 30000);
    if (seq !== this.seq) return;
    this.items = d.countries || [];
    this.events = d.events || [];
    this.byCc = new Map(this.items.map((c) => [c.cc, c]));
    const inst = [];
    this.lb.removeAll(); this.edges.removeAll();
    for (const c of this.items) {
      const col = C.Color.fromCssColorString(OUTAGE_COLORS[c.level] || OUTAGE_COLORS.low);
      // telescope-only (merit-nt) detections are weak evidence: drawn faintly
      const weakOnly = c.datasources.every((s) => s === "merit-nt");
      const alpha = (c.ongoing ? 0.42 : 0.2) * (weakOnly ? 0.45 : 1);
      for (const poly of ((d.shapes || {})[c.cc] || [])) {
        if (!poly.length || poly[0].length < 6) continue;
        const holes = poly.slice(1).filter((h) => h.length >= 6).map((h) => new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(h)));
        inst.push(new C.GeometryInstance({
          geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(poly[0]), holes), vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT, granularity: C.Math.RADIANS_PER_DEGREE * 2 }),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(col.withAlpha(alpha)) },
          id: { signal: "outage", cc: c.cc },
        }));
        if (!weakOnly) this.edges.add({ positions: C.Cartesian3.fromDegreesArray([...poly[0], poly[0][0], poly[0][1]]), width: 1.2, material: C.Material.fromType("Color", { color: col.withAlpha(c.ongoing ? 0.9 : 0.5) }) });
      }
      if (!weakOnly && c.lat != null) {
        this.lb.add({ position: C.Cartesian3.fromDegrees(c.lon, c.lat), text: "NET " + (c.ongoing ? "DOWN " : "") + c.cc, font: FONT, fillColor: col, outlineColor: C.Color.BLACK, outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE, translucencyByDistance: new C.NearFarScalar(3e6, 1, 2.5e7, 0.6), id: { signal: "outage", cc: c.cc } });
      }
    }
    if (this.prim) { this.scene.primitives.remove(this.prim); this.prim = null; }
    if (inst.length) this.prim = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false }), asynchronous: false }));
    this._show();
    if (this.onData) this.onData(this);
  }
  _show() { if (this.prim) this.prim.show = this.on; this.lb.show = this.on; this.edges.show = this.on; this.req(); }
  get ongoing() { return this.items.filter((c) => c.ongoing && !c.datasources.every((s) => s === "merit-nt")).length; }
}

// ---- radiation dose rate ----
export const RAD_STOPS = [[0.0, "#3ddc84"], [0.1, "#3ddc84"], [0.15, "#c9e84a"], [0.25, "#ffd24a"], [0.5, "#ff8a3d"], [1.0, "#ff4b55"]];
export function radColor(v) {
  let lo = RAD_STOPS[0];
  for (const s of RAD_STOPS) { if (v >= s[0]) lo = s; }
  return lo[1];
}
// stations reading normal only when zoomed in (lod.js); elevated / alert ones at every height
const RAD_NORMAL_DDC = new C.DistanceDisplayCondition(0, LOD.radiation.normalBelow);
const RAD_SCALE = new C.NearFarScalar(3e5, 1.4, 2e7, 0.6);
export class RadiationLayer extends Timed {
  constructor(scene) {
    super(scene, 900000);
    this.pts = scene.primitives.add(new C.PointPrimitiveCollection());
  }
  async load() {
    const seq = ++this.seq;
    const d = await fetchJson("/api/signals/radiation", 30000);
    if (seq !== this.seq) return;
    this.items = d.items || [];
    this.networks = d.networks || {};
    this.pts.removeAll();
    this.items.forEach((s, i) => {
      const hot = s.status === "elevated" || s.status === "alert";
      this.pts.add({ position: C.Cartesian3.fromDegrees(s.lon, s.lat), pixelSize: hot ? 10 : s.source === "safecast" ? 5 : 4, color: C.Color.fromCssColorString(radColor(s.value)).withAlpha(0.9),
        outlineColor: hot ? C.Color.fromCssColorString("#ff4b55") : C.Color.BLACK.withAlpha(0.5), outlineWidth: hot ? 3 : 1, scaleByDistance: RAD_SCALE, distanceDisplayCondition: hot ? undefined : RAD_NORMAL_DDC, id: { signal: "rad", i } });
    });
    this._show();
    if (this.onData) this.onData(this);
  }
  _show() { this.pts.show = this.on; this.req(); }
  get elevated() { return this.items.filter((s) => s.status === "elevated" || s.status === "alert").length; }
}

// ---- aurora (OVATION nowcast, 1-degree probability grid) ----
export class AuroraLayer extends Timed {
  constructor(viewer) {
    super(viewer.scene, 1800000);
    this.viewer = viewer; this.layer = null; this.meta = null;
  }
  async load() {
    const seq = ++this.seq;
    const d = await fetchJson("/api/signals/aurora", 30000);
    if (seq !== this.seq || !d || !d.b64) return;
    this.meta = d;
    const raw = atob(d.b64), w = d.w, h = d.h;
    const cv = document.createElement("canvas");
    cv.width = w; cv.height = h;
    const ctx = cv.getContext("2d"), img = ctx.createImageData(w, h);
    for (let y = 0; y < h; y++) {
      const gy = h - 1 - y; // canvas top = north (lat 90); grid row 0 = lat -90
      for (let x = 0; x < w; x++) {
        const gx = (x + 180) % 360; // canvas x 0 = lon -180; grid column 0 = lon 0
        const v = raw.charCodeAt(gy * w + gx), o = (y * w + x) * 4;
        if (v < 8) continue; // below ~8 % the nowcast is background noise (and smears at the pole)
        const t = Math.min(1, (v - 8) / 50);
        img.data[o] = Math.round(40 + 215 * Math.max(0, t - 0.5) * 2);    // green -> yellow -> red
        img.data[o + 1] = Math.round(255 - 120 * Math.max(0, t - 0.7) / 0.3);
        img.data[o + 2] = Math.round(120 * (1 - t));
        img.data[o + 3] = Math.round(70 + 160 * t);
      }
    }
    ctx.putImageData(img, 0, 0);
    const provider = await C.SingleTileImageryProvider.fromUrl(cv.toDataURL("image/png"), { rectangle: C.Rectangle.fromDegrees(-180, -90, 180, 90) });
    if (seq !== this.seq) return;
    const old = this.layer;
    this.layer = this.viewer.imageryLayers.addImageryProvider(provider);
    this.layer.alpha = 0.85;
    this.layer.show = this.on;
    if (old) this.viewer.imageryLayers.remove(old, true);
    this.req();
    if (this.onData) this.onData(this);
  }
  _show() { if (this.layer) this.layer.show = this.on; this.req(); }
}
