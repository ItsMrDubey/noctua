// Selection reticle (animated corner brackets + expanding pulse ring) and pulsing halos
// under high-priority aircraft. Animates at a capped tick rate in request-render mode.
import { REDUCED } from "/static/js/ui.js";

const C = Cesium;
const svg = (body) => "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="square">' + body + "</svg>");
const BRACKETS = svg('<path d="M4 18V4h14M46 4h14v14M60 46v14H46M18 60H4V46"/><path d="M32 2v6M32 56v6M2 32h6M56 32h6" stroke-width="1.4"/>');
const RING = svg('<circle cx="32" cy="32" r="28" stroke-width="2"/>');
const TICK_MS = 70; // ~14 Hz: smooth enough for a pulse, cheap in request-render mode
const ION = C.Color.fromCssColorString("#5ee3ea"), AMBER = C.Color.fromCssColorString("#f2ad3d"), TEAL = C.Color.fromCssColorString("#3ddcc2");
const HALO = C.Color.fromCssColorString("#ff4b55");

export class Reticle {
  constructor(scene) {
    this.scene = scene;
    this.bb = scene.primitives.add(new C.BillboardCollection({ scene }));
    const common = { show: false, disableDepthTestDistance: Number.POSITIVE_INFINITY, position: C.Cartesian3.ZERO };
    this.br = this.bb.add({ ...common, image: BRACKETS, width: 46, height: 46 });
    this.ring = this.bb.add({ ...common, image: RING, width: 46, height: 46 });
    this.br.id = this.ring.id = { fx: true };
    this.halos = scene.primitives.add(new C.PointPrimitiveCollection());
    this.haloMap = new Map();
    this.target = null; this.kind = null;
    this.t0 = performance.now();
    this.timer = setInterval(() => this.tick(), TICK_MS);
  }
  setTarget(fn, kind) {
    this.target = fn; this.kind = kind;
    this.t0 = performance.now();
    if (!fn) { this.br.show = this.ring.show = false; this.scene.requestRender(); }
    else this.tick(true);
  }
  // keep one halo per visible high-priority aircraft
  syncHalos(layer) {
    const want = new Set();
    if (layer.show) {
      for (const [id, it] of layer.items) {
        if (!it.high_priority) continue;
        const p = layer.map.get(id);
        if (!p || !p.show) continue;
        want.add(id);
        let h = this.haloMap.get(id);
        if (!h) {
          h = this.halos.add({ color: HALO.withAlpha(0.35), outlineColor: HALO.withAlpha(0.8), outlineWidth: 1, pixelSize: 16,
            scaleByDistance: new C.NearFarScalar(1e6, 1.0, 2.5e7, 0.7) });
          h.id = { fx: true };
          this.haloMap.set(id, h);
        }
        if (h._p !== p.position) { h.position = p.position; h._p = p.position; }
      }
    }
    for (const [id, h] of this.haloMap) if (!want.has(id)) { this.halos.remove(h); this.haloMap.delete(id); }
    this.scene.requestRender();
  }
  tick(force) {
    if (document.hidden && !force) return;
    const anim = !REDUCED;
    const t = (performance.now() - this.t0) / 1000;
    let dirty = !!force;
    if (this.target) {
      const p = this.target();
      if (p) {
        const replay = document.body.classList.contains("replay");
        const col = replay ? AMBER : this.kind === "ref" ? TEAL : ION;
        this.br.position = p; this.ring.position = p;
        this.br.show = true; this.br.color = col;
        // brackets snap in, then breathe slightly; ring expands + fades every 1.6 s
        const snap = Math.min(1, t / 0.18);
        this.br.scale = anim ? (1.6 - 0.6 * snap) + 0.05 * Math.sin(t * 3.2) : 1;
        if (anim) {
          const ph = (t % 1.6) / 1.6;
          this.ring.show = true; this.ring.scale = 0.5 + ph * 1.3; this.ring.color = col.withAlpha(0.8 * (1 - ph));
        } else this.ring.show = false;
        dirty = true;
      } else if (this.br.show) { this.br.show = this.ring.show = false; dirty = true; }
    }
    if (this.haloMap.size && anim) {
      const ph = (t % 1.4) / 1.4;
      const size = 14 + ph * 20, a = 0.45 * (1 - ph);
      const fill = HALO.withAlpha(a * 0.5), line = HALO.withAlpha(a * 1.6);
      for (const h of this.haloMap.values()) { h.pixelSize = size; h.color = fill; h.outlineColor = line; }
      dirty = true;
    }
    if (dirty) this.scene.requestRender();
  }
}
