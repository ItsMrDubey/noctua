// Track trails: selected-track trail (fading) and all-military-aircraft trails.
import { fetchJson } from "/static/js/api.js";
const C = Cesium;
const SEGMENTS = 6;
const MATS = new Map();

function colorFor(kind, it) {
  if (kind === "aircraft") return C.Color.fromCssColorString(it && it.high_priority ? "#ff4b55" : it && it.is_mil ? "#9ff3ff" : "#c8d4de");
  return C.Color.fromCssColorString(it && it.is_mil ? "#ff4b55" : "#7fa8ff");
}

export class Trails {
  constructor(scene) {
    this.scene = scene;
    this.sel = scene.primitives.add(new C.PolylineCollection());
    this.mil = scene.primitives.add(new C.PolylineCollection());
    this.milOn = false;
    this.selKey = null;
    this.lastSel = 0;
    this.milTimer = null;
  }

  // Draws points ([[lon,lat,altM],...]) as SEGMENTS polylines with rising alpha.
  _draw(col, pts, color, width) {
    if (pts.length < 2) return;
    const per = Math.max(1, Math.ceil((pts.length - 1) / SEGMENTS));
    for (let i = 0, n = 0; i < pts.length - 1; i += per, n++) {
      const seg = pts.slice(i, Math.min(pts.length, i + per + 1));
      if (seg.length < 2) continue;
      const alpha = Math.round(10 * (0.15 + 0.85 * ((n + 1) / Math.ceil((pts.length - 1) / per)))) / 10;
      const mk = color.toCssHexString() + alpha;
      // materials are shared per colour/alpha step so PolylineCollection can batch them
      const material = MATS.get(mk) || MATS.set(mk, C.Material.fromType("Color", { color: color.withAlpha(alpha) })).get(mk);
      col.add({ positions: seg.map((p) => C.Cartesian3.fromDegrees(p[0], p[1], p[2] || 0)), width, material });
    }
  }

  clearSelected() { this.sel.removeAll(); this.selKey = null; this.points = []; this.scene.requestRender(); }

  // Fetch + draw the trail for the selected track; throttled unless force.
  async showSelected(kind, id, it, force) {
    const key = kind + ":" + id;
    const now = Date.now();
    if (!force && key === this.selKey && now - this.lastSel < 8000) return;
    this.selKey = key; this.lastSel = now;
    if (this.onState) this.onState(key, true, null);
    try {
      const d = await fetchJson("/api/tracks/" + encodeURIComponent(id) + "?hours=6");
      if (this.selKey !== key) return; // selection changed while fetching
      const pts = (d.points || []).map((p) => [p.lon, p.lat, kind === "aircraft" ? (p.alt_ft || 0) * 0.3048 : 0]);
      this.sel.removeAll();
      this._draw(this.sel, pts, colorFor(kind, it), 3);
      this.points = d.points || [];
      if (this.onPoints) this.onPoints(key, this.points);
      this.scene.requestRender();
    } catch (e) { if (this.selKey === key && this.onState) this.onState(key, false, String((e && e.message) || e)); /* keep previous trail */ }
  }

  async refreshMil() {
    if (!this.milOn) return;
    try {
      const d = await fetchJson("/api/tracks?kind=air&hours=1&mil_only=true", 30000);
      if (!this.milOn) return;
      this.mil.removeAll();
      const col = C.Color.fromCssColorString("#9ff3ff");
      for (const pts of Object.values(d.tracks || {})) this._draw(this.mil, pts.map((p) => [p[0], p[1], (p[2] || 0) * 0.3048]), col, 1.5);
      this.scene.requestRender();
    } catch (e) { /* retry next tick */ }
  }

  setMil(on) {
    this.milOn = on;
    clearInterval(this.milTimer);
    if (on) { this.refreshMil(); this.milTimer = setInterval(() => this.refreshMil(), 60000); }
    else { this.mil.removeAll(); this.scene.requestRender(); }
  }
}
