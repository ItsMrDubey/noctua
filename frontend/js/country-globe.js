// Globe sync for the country pane (spec B §3.3). While a country pane is the active context:
// its outline in --line-2 over a faint (6 %) amber fill, event glyphs outside the pane's
// filtered set dimmed to 25 % (EventLayer.setFocus, never removed), and the camera fitted to
// the country's bbox once per country per session (no re-fit on filter changes; a country
// without a polygon gets no outline and a fly-to on its centroid). deactivate() restores all.
// Colours: --amber, not --acc (--acc turns ion-blue in replay); read once via symbology.js.
import { fetchJson } from "/static/js/api.js";
import { cesiumColor } from "/static/js/symbology.js";
import { fitBox, focusPredicate, outlineRuns } from "/static/js/country-state.js";

const C = window.Cesium;
const FILL_ALPHA = 0.06;
const FLY_S = 1.4;
const CENTROID_ALT = 3e6;

export class CountryGlobe {
  constructor(viewer, events) {
    this.viewer = viewer; this.scene = viewer.scene; this.events = events;
    this.active = null;          // ISO2 of the active country pane, or null
    this.sets = new Map();       // cc -> {ids: Set, truncated} for that pane's filtered set
    this.pendingFit = new Map(); // cc -> summary waiting for its pane to become active
    this.fitted = new Set();     // countries already fitted this session
    this.shapes = new Map();     // cc -> Promise of polygons ([] = no polygon)
    this.fill = null; this.edges = null; this.seq = 0;
    this.lastApplyMs = null;     // time the last focus change took to apply (diagnostics)
  }
  activate(cc) {
    if (!cc || this.active === cc) return;
    this.active = cc;
    this._dim();
    this._outline(cc);
    const s = this.pendingFit.get(cc);
    if (s) this.fit(cc, s);
  }
  deactivate() {
    if (!this.active) return;
    this.active = null; this.seq++;
    this._setFocus(null);
    this._clearOutline();
  }
  // fetchedAt: when the server read the set (ISO or ms); events updated after it stay undimmed
  setIds(cc, ids, truncated = false, fetchedAt = null) { this.sets.set(cc, { ids, truncated: !!truncated, fetchedAt }); if (this.active === cc) this._dim(); }
  fit(cc, summary) {
    if (this.fitted.has(cc) || !summary) return;
    if (this.active !== cc) { this.pendingFit.set(cc, summary); return; }
    this.pendingFit.delete(cc); this.fitted.add(cc);
    // a country without a polygon flies to its centroid (spec §4), so wait for the shape
    this._shape(cc).then((polys) => {
      if (this.active !== cc) { this.fitted.delete(cc); this.pendingFit.set(cc, summary); return; } // switched away meanwhile
      const box = polys.length ? fitBox(summary.bbox) : null;
      if (box) {
        // w > e (antimeridian) is a valid Cesium.Rectangle: the camera spans east across 180
        this.viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(box[0], box[1], box[2], box[3]), duration: FLY_S });
      } else if (Array.isArray(summary.centroid) && summary.centroid.length === 2) {
        this.viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(summary.centroid[1], summary.centroid[0], CENTROID_ALT), duration: FLY_S });
      } else {
        const b = fitBox(summary.bbox);
        if (b) this.viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]), duration: FLY_S });
      }
    });
  }
  _dim() {
    const s = this.active ? this.sets.get(this.active) : null;
    this._setFocus(s ? focusPredicate(s.ids, { truncated: s.truncated, cc: this.active, fetchedAt: s.fetchedAt }) : null);
  }
  _setFocus(pred) {
    const t0 = performance.now();
    this.events.setFocus(pred);
    this.lastApplyMs = performance.now() - t0;
  }
  _shape(cc) {
    let p = this.shapes.get(cc);
    if (!p) {
      p = fetchJson("/api/country/" + encodeURIComponent(cc) + "/shape", 20000)
        .then((r) => (Array.isArray(r && r.polygons) ? r.polygons : []))
        .catch(() => { this.shapes.delete(cc); return []; }); // a failed fetch is retried next time
      this.shapes.set(cc, p);
    }
    return p;
  }
  async _outline(cc) {
    const seq = ++this.seq;
    this._clearOutline();
    const polys = await this._shape(cc);
    if (seq !== this.seq || this.active !== cc || !polys.length) return;
    const line = cesiumColor("--line-2");
    const fill = cesiumColor("--amber", FILL_ALPHA);
    // one Material shared by every polyline of this outline (not one per ring); it lives and dies
    // with this PolylineCollection, which _clearOutline() destroys. Built fresh per outline, never
    // reused across collections; 8 open/close cycles left the primitive count at its baseline
    // with no console errors
    const mat = C.Material.fromType("Color", { color: line });
    const inst = [];
    const edges = new C.PolylineCollection();
    for (const poly of polys) {
      if (!Array.isArray(poly) || !poly.length || !(poly[0].length >= 6)) continue;
      const holes = poly.slice(1).filter((h) => h.length >= 6).map((h) => new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(h)));
      inst.push(new C.GeometryInstance({
        geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(poly[0]), holes), vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT, granularity: C.Math.RADIANS_PER_DEGREE * 2 }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(fill) },
      }));
      for (const ring of poly) for (const run of outlineRuns(ring)) edges.add({ positions: C.Cartesian3.fromDegreesArray(run), width: 1.5, material: mat });
    }
    this.edges = this.scene.primitives.add(edges);
    if (inst.length) this.fill = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false, flat: true }), asynchronous: false, allowPicking: false }));
    this.scene.requestRender();
  }
  // primitives.remove() destroys them (destroyPrimitives defaults to true): no GPU leak per open
  _clearOutline() {
    if (this.fill) { this.scene.primitives.remove(this.fill); this.fill = null; }
    if (this.edges) { this.scene.primitives.remove(this.edges); this.edges = null; }
    this.scene.requestRender();
  }
}
