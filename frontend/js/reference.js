// Reference geography: tactical wireframe overlays (borders, chokepoints, airports, runways, ports, bases).
// Perf rules: static line layers are ONE Primitive each (built once, toggled via .show); point sets are
// loaded once and diffed by id; labels come from a small recycled pool covering only the current view.
import { fetchJson } from "/static/js/api.js";
import { esc } from "/static/js/ui.js";

const C = Cesium;
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const CYAN = "#36c5ff", AMBER = "#ffb020", TEAL = "#3ddcc2", RED = "#ff4d4d";
const BASE_COLORS = { airbase: "#ff5a5a", naval: "#4aa3ff", radar: "#c9a7ff", missile: "#ffb020", other: "#9fb3c8" };
const BASE_CLASS = { airbase: "airbase", naval_base: "naval", port: "naval", radar: "radar", air_defense: "radar", missile_site: "missile" };
const baseClass = (t) => BASE_CLASS[t] || "other";
const col = (css, a = 1) => C.Color.fromCssColorString(css).withAlpha(a);
const svg = (body, c) => "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="' + c + '" stroke-width="1.6" stroke-linecap="round">' + body + "</svg>");
const ICONS = {
  airport: (c) => svg('<rect x="4" y="4" width="12" height="12" rx="1"/><path d="M10 6.5v7"/>', c),
  milairport: (c) => svg('<rect x="4" y="4" width="12" height="12" rx="1"/><path d="M6.5 6.5l7 7M13.5 6.5l-7 7"/>', c),
  port: (c) => svg('<circle cx="10" cy="5.5" r="1.8"/><path d="M10 7.5v8.5M6.5 10h7M4.5 12.5c.8 3 3 4 5.5 4s4.7-1 5.5-4"/>', c),
  base: (c) => svg('<path d="M10 3l7 7-7 7-7-7z"/><circle cx="10" cy="10" r="1.2"/>', c),
};
const IMG = { airport: ICONS.airport(CYAN), milairport: ICONS.milairport(RED), port: ICONS.port(TEAL) };
const BASE_IMG = Object.fromEntries(Object.entries(BASE_COLORS).map(([k, c]) => [k, ICONS.base(c)]));
const pos = (lon, lat, h = 0) => C.Cartesian3.fromDegrees(lon, lat, h);
const nf = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const kv = (rows) => '<div class="kv">' + rows.filter((r) => r[1] != null && r[1] !== "").map((r) => "<span>" + esc(r[0]) + "</span><div>" + r[1] + "</div>").join("") + "</div>";

// shared constants (never allocate per item)
const BB_SCALE = new C.NearFarScalar(5e5, 1.0, 2.5e7, 0.5);
const BB_FADE = new C.NearFarScalar(1e6, 0.95, 3e7, 0.6);
const BLACK85 = col("#000", 0.85);
const LABEL_OFFSET = new C.Cartesian2(10, 0);
const LABEL_MAX_H = 1.6e6;   // labels only below this camera height
const LABEL_CAP = 300;
const MED_MAX_H = 2.5e6;     // medium airports only below this camera height
const PRIORITY = { base: 0, airport: 1, port: 2, medairport: 3 };

export const REF_IDS = ["r-airports", "r-airports-med", "r-airports-mil", "r-runways", "r-ports", "r-bases", "r-rings", "r-borders", "r-disputed", "r-maritime", "r-choke", "r-clabels"];

export class ReferenceLayers {
  constructor(viewer, onPick) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.onPick = onPick;
    this.state = {};
    this.bases = null;
    this.chokeItems = [];
    this.countryItems = [];
    this.static = {};   // name -> Primitive (built once)
    this.loaded = {};
    const P = this.scene.primitives;
    this.rings = P.add(new C.PolylineCollection());
    this.runways = P.add(new C.PolylineCollection());
    this.chokeLabels = P.add(new C.LabelCollection());
    this.countryLabels = P.add(new C.LabelCollection());
    this.bb = P.add(new C.BillboardCollection({ scene: this.scene }));
    this.pool = P.add(new C.LabelCollection());
    this.poolUsed = 0;
    this.pts = new Map(); // key -> {b, kind, item, label, color, layer}
    this.timer = null;
    this.medKey = null;
    viewer.camera.moveEnd.addEventListener(() => this.schedule());
  }
  req() { this.scene.requestRender(); }
  // async primitives become ready a few frames later; nudge the renderer meanwhile
  kick(ms = 4000) {
    const end = Date.now() + ms;
    const t = setInterval(() => { this.req(); if (Date.now() > end) clearInterval(t); }, 200);
  }

  // ---- toggles ----
  setOn(id, on) { this.state[id] = on; this.apply(id); }
  apply(id) {
    const s = this.state;
    if (id === "r-borders") this.ensureBoundary("land").then(() => this.showStatic("land", !!s[id]));
    else if (id === "r-disputed") this.ensureBoundary("disputed").then(() => this.showStatic("disputed", !!s[id]));
    else if (id === "r-maritime") this.ensureBoundary("maritime").then(() => this.showStatic("maritime", !!s[id]));
    else if (id === "r-choke") this.ensureChoke().then(() => this.setChokeShow(!!s[id]));
    else if (id === "r-clabels") this.ensureCountries().then(() => { this.countryLabels.show = !!s[id]; this.req(); });
    else if (id === "r-bases" || id === "r-rings") this.ensureBases().then(() => this.refreshBases());
    else if (id === "r-airports" || id === "r-airports-mil") (s["r-airports"] ? this.ensureAirports() : Promise.resolve()).then(() => { this.refreshAirports(); this.schedule(50); });
    else if (id === "r-ports") (s[id] ? this.ensurePorts() : Promise.resolve()).then(() => { this.refreshPorts(); this.schedule(50); });
    else this.schedule(50);
  }
  setBaseTypes(set) { this.baseTypes = set; this.ensureBases().then(() => this.refreshBases()); }
  schedule(ms = 300) { clearTimeout(this.timer); this.timer = setTimeout(() => this.refresh().catch((e) => console.warn("reference refresh", e)), ms); }
  showStatic(name, on) { const p = this.static[name]; if (p) { p.show = on; this.req(); } }

  // ---- static line layers: one Primitive each ----
  async ensureBoundary(name) {
    if (this.loaded[name]) return this.loaded[name];
    this.loaded[name] = (async () => {
      try {
        const gj = await fetchJson("/api/reference/boundaries/" + name, 30000);
        const land = name === "land";
        const inst = [];
        for (const f of gj.features || []) {
          const g = f.geometry;
          const parts = g.type === "LineString" ? [g.coordinates] : g.coordinates;
          for (const p of parts) {
            if (p.length < 2) continue;
            const positions = C.Cartesian3.fromDegreesArray(p.flat());
            const id = { ref: true, kind: "boundary", item: { lat: p[0][1], lon: p[0][0], name: (f.properties && f.properties.name) || name + " boundary", layer: name } };
            inst.push(new C.GeometryInstance({
              geometry: land ? new C.SimplePolylineGeometry({ positions }) : new C.PolylineGeometry({ positions, width: 1.3, vertexFormat: C.PolylineMaterialAppearance.VERTEX_FORMAT }),
              attributes: land ? { color: C.ColorGeometryInstanceAttribute.fromColor(col(CYAN, 0.5)) } : undefined,
              id,
            }));
          }
        }
        if (!inst.length) return;
        const appearance = land ? new C.PerInstanceColorAppearance({ flat: true, translucent: true })
          : new C.PolylineMaterialAppearance({ material: C.Material.fromType("PolylineDash", { color: col(name === "disputed" ? AMBER : TEAL, name === "disputed" ? 0.85 : 0.6), dashLength: 14 }) });
        const prim = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance, asynchronous: true, releaseGeometryInstances: true }));
        prim.show = !!this.state[{ land: "r-borders", disputed: "r-disputed", maritime: "r-maritime" }[name]];
        this.static[name] = prim;
        this.kick();
      } catch (e) { this.loaded[name] = null; console.warn("boundary", name, e); }
    })();
    return this.loaded[name];
  }
  async ensureChoke() {
    if (this.loaded.choke) return this.loaded.choke;
    this.loaded.choke = (async () => {
      try {
        const d = await fetchJson("/api/reference/chokepoints", 15000);
        const lines = [], fills = [];
        for (const c of d.items || []) {
          const positions = C.Cartesian3.fromDegreesArray(c.ring.flat());
          const id = { ref: true, kind: "chokepoint", item: c };
          lines.push(new C.GeometryInstance({ geometry: new C.PolylineGeometry({ positions, width: 1.6, vertexFormat: C.PolylineMaterialAppearance.VERTEX_FORMAT }), id }));
          fills.push(new C.GeometryInstance({ geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(positions), vertexFormat: C.PerInstanceColorAppearance.FLAT_VERTEX_FORMAT }),
            attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(col(AMBER, 0.07)) }, id }));
          const lb = this.chokeLabels.add({
            position: pos(c.lon, c.lat), text: c.name.toUpperCase(), font: FONT, scale: 0.9, fillColor: col(AMBER, 0.95), outlineColor: BLACK85,
            outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -12), distanceDisplayCondition: new C.DistanceDisplayCondition(0, 6e6),
          });
          lb.id = id;
          this.chokeItems.push(c);
        }
        if (lines.length) {
          this.static.chokeLines = this.scene.primitives.add(new C.Primitive({ geometryInstances: lines,
            appearance: new C.PolylineMaterialAppearance({ material: C.Material.fromType("Color", { color: col(AMBER, 0.9) }) }), asynchronous: true }));
          this.static.chokeFill = this.scene.primitives.add(new C.Primitive({ geometryInstances: fills,
            appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }), asynchronous: true }));
        }
        this.setChokeShow(!!this.state["r-choke"]);
        this.kick();
      } catch (e) { this.loaded.choke = null; console.warn("chokepoints", e); }
    })();
    return this.loaded.choke;
  }
  setChokeShow(on) {
    this.chokeLabels.show = on;
    for (const k of ["chokeLines", "chokeFill"]) if (this.static[k]) this.static[k].show = on;
    this.req();
  }
  async ensureCountries() {
    if (this.loaded.countries) return this.loaded.countries;
    this.loaded.countries = (async () => {
      try {
        const d = await fetchJson("/api/reference/countries", 15000);
        const fill = col(CYAN, 0.7), outline = col("#000", 0.7), ddc = new C.DistanceDisplayCondition(1.5e6, 4e7);
        for (const c of d.items || []) {
          this.countryItems.push(c);
          this.countryLabels.add({ position: pos(c.lon, c.lat), text: c.name.toUpperCase(), font: FONT, scale: 0.85, fillColor: fill, outlineColor: outline,
            outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE, distanceDisplayCondition: ddc });
        }
        this.countryLabels.show = !!this.state["r-clabels"];
        this.req();
      } catch (e) { this.loaded.countries = null; console.warn("countries", e); }
    })();
    return this.loaded.countries;
  }

  // ---- point sets (loaded once, billboards created once, shown/hidden by filters) ----
  _point(key, kind, item, image, size, label, color) {
    let o = this.pts.get(key);
    if (!o) {
      const ref = { ref: true, kind, item };
      const b = this.bb.add({ position: pos(item.lon, item.lat), image, width: size, height: size, scaleByDistance: BB_SCALE, translucencyByDistance: BB_FADE, show: false });
      b.id = ref;
      o = { b, kind, item, label: label.toUpperCase(), color, ref, pr: PRIORITY[kind] ?? 4 };
      this.pts.set(key, o);
    }
    return o;
  }
  _showWhere(prefix, pred) {
    for (const [k, o] of this.pts) if (k.startsWith(prefix)) o.b.show = pred(o);
    this.req();
  }
  async ensureAirports() {
    if (this.loaded.airports) return this.loaded.airports;
    this.loaded.airports = fetchJson("/api/reference/airports?bbox=-180,-90,180,90&min_class=large&military_only=false", 30000).then((d) => {
      for (const a of d.items || []) {
        const large = a.type === "large_airport";
        this._point("ap:" + a.ident, "airport", a, a.is_military ? IMG.milairport : IMG.airport, large ? 15 : 12, (a.icao || a.ident) + " " + a.name, a.is_military ? RED : CYAN);
      }
    }).catch((e) => { this.loaded.airports = null; console.warn("airports", e); });
    return this.loaded.airports;
  }
  refreshAirports() {
    const on = !!this.state["r-airports"], mil = !!this.state["r-airports-mil"];
    this._showWhere("ap:", (o) => on && (!mil || o.item.is_military));
  }
  async ensurePorts() {
    if (this.loaded.ports) return this.loaded.ports;
    this.loaded.ports = fetchJson("/api/reference/ports?bbox=-180,-90,180,90", 30000).then((d) => {
      for (const p of d.items || []) this._point("pt:" + p.name + p.lat.toFixed(2) + p.lon.toFixed(2), "port", p, IMG.port, 13, p.name, TEAL);
    }).catch((e) => { this.loaded.ports = null; console.warn("ports", e); });
    return this.loaded.ports;
  }
  refreshPorts() { const on = !!this.state["r-ports"]; this._showWhere("pt:", () => on); }
  async ensureBases() {
    if (this.bases) return;
    try {
      const d = await fetchJson("/api/reference/bases", 15000);
      this.bases = (d.items || []).map((b, i) => ({ ...b, id: "base:" + i, cls: baseClass(b.type) }));
      for (const b of this.bases) this._point(b.id, "base", b, BASE_IMG[b.cls], 17, b.name, BASE_COLORS[b.cls]);
    } catch (e) { console.warn("bases", e); this.bases = []; }
  }
  refreshBases() {
    const on = !!this.state["r-bases"];
    const types = this.baseTypes || new Set(Object.keys(BASE_COLORS));
    this._showWhere("base:", (o) => on && types.has(o.item.cls));
    const rl = this.rings;
    rl.removeAll();
    if (this.state["r-rings"] && on) {
      const mats = {};
      for (const b of this.bases || []) {
        if (!types.has(b.cls)) continue;
        const mat = mats[b.cls] || (mats[b.cls] = C.Material.fromType("PolylineDash", { color: col(BASE_COLORS[b.cls], 0.35), dashLength: 10 }));
        for (const km of [100, 300, 500]) {
          const pts = [];
          for (let a = 0; a <= 360; a += 6) {
            const r = km / 6371, br = (a * Math.PI) / 180, la = (b.lat * Math.PI) / 180, lo = (b.lon * Math.PI) / 180;
            const la2 = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(br));
            const lo2 = lo + Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(la2));
            pts.push((lo2 * 180) / Math.PI, (la2 * 180) / Math.PI);
          }
          rl.add({ positions: C.Cartesian3.fromDegreesArray(pts), width: 1, material: mat });
        }
      }
    }
    this.schedule(50);
    this.req();
  }

  // ---- viewport-driven: medium airports, runways, label pool ----
  viewBBox() {
    const r = this.viewer.camera.computeViewRectangle(this.scene.globe.ellipsoid);
    if (!r) return [-180, -90, 180, 90];
    const d = C.Math.toDegrees;
    const w = d(r.west), e = d(r.east), s = d(r.south), n = d(r.north);
    const lonSpan = e >= w ? e - w : e - w + 360;
    if (lonSpan >= 340) return [-180, s, 180, n];
    return [w, s, e, n];
  }
  async refresh() {
    const s = this.state;
    const h = this.viewer.camera.positionCartographic.height;
    const bb = this.viewBBox();
    // medium airports: viewport-loaded only when zoomed in
    if (s["r-airports"] && s["r-airports-med"] && h < MED_MAX_H) {
      const key = bb.map((x) => Math.round(x)).join(",");
      if (key !== this.medKey) {
        this.medKey = key;
        try {
          const d = await fetchJson("/api/reference/airports?bbox=" + key + "&min_class=medium&military_only=false", 20000);
          for (const a of d.items || []) {
            if (a.type !== "medium_airport" || this.pts.has("ap:" + a.ident)) continue;
            this._point("md:" + a.ident, "medairport", a, a.is_military ? IMG.milairport : IMG.airport, 12, (a.icao || a.ident) + " " + a.name, a.is_military ? RED : CYAN);
          }
        } catch (e) { this.medKey = null; }
      }
      const mil = !!s["r-airports-mil"];
      this._showWhere("md:", (o) => !mil || o.item.is_military);
    } else this._showWhere("md:", () => false);
    // runways: only when zoomed right in
    this.runways.show = !!s["r-runways"];
    if (s["r-runways"] && h < 4e5) {
      let [w, so, e, n] = bb;
      if (e < w || e - w > 4.5 || n - so > 4.5) {
        const c = this.viewer.camera.positionCartographic;
        const lo = C.Math.toDegrees(c.longitude), la = C.Math.toDegrees(c.latitude);
        [w, so, e, n] = [lo - 2, la - 2, lo + 2, la + 2];
      }
      const rk = [w, so, e, n].map((x) => x.toFixed(1)).join(",");
      if (rk !== this.rwKey) {
        this.rwKey = rk;
        try { this.drawRunways((await fetchJson("/api/reference/runways?bbox=" + rk, 20000)).items || []); } catch (e) { this.rwKey = null; }
      }
    } else if (this.runways.length) { this.runways.removeAll(); this.rwKey = null; }
    this.relabel(h, bb);
    this.req();
  }
  drawRunways(items) {
    const L = this.runways;
    L.removeAll();
    const mat = C.Material.fromType("Color", { color: col(CYAN, 0.95) });
    for (const r of items) {
      const l = L.add({ positions: C.Cartesian3.fromDegreesArray([...r.rect, r.rect[0]].flat()), width: 1.4, material: mat });
      l.id = { ref: true, kind: "runway", item: { ...r, lat: r.rect[0][1], lon: r.rect[0][0] } };
    }
  }
  // Recycled label pool: only visible points inside the view, only when zoomed in, capped.
  relabel(h, bb) {
    const pool = this.pool;
    let used = 0;
    if (h < LABEL_MAX_H) {
      const [w, s, e, n] = bb;
      const inLon = w <= e ? (lon) => lon >= w && lon <= e : (lon) => lon >= w || lon <= e;
      const cands = [];
      for (const o of this.pts.values()) {
        if (!o.b.show) continue;
        const it = o.item;
        if (it.lat < s || it.lat > n || !inLon(it.lon)) continue;
        cands.push(o);
      }
      if (cands.length > LABEL_CAP) cands.sort((a, b) => a.pr - b.pr);
      for (const o of cands.slice(0, LABEL_CAP)) {
        let l = used < pool.length ? pool.get(used) : null;
        if (!l) l = pool.add({ font: FONT, scale: 0.85, outlineColor: BLACK85, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT, pixelOffset: LABEL_OFFSET });
        if (l._k !== o) { l._k = o; l.text = o.label; l.position = o.b.position; l.fillColor = col(o.color, 0.9); l.id = o.ref; }
        l.show = true;
        used++;
      }
    }
    for (let i = used; i < pool.length; i++) pool.get(i).show = false;
    this.poolUsed = used;
  }

  // places for the command palette
  places() {
    const out = [];
    for (const o of this.pts.values()) out.push({ kind: o.kind, name: o.label, item: o.item, ref: o.ref });
    for (const c of this.chokeItems) out.push({ kind: "chokepoint", name: c.name, item: c, ref: { ref: true, kind: "chokepoint", item: c } });
    for (const c of this.countryItems) out.push({ kind: "country", name: c.name, item: c, ref: null });
    return out;
  }
  ensureAll() { this.ensureBases(); this.ensureChoke(); this.ensureCountries(); this.ensureAirports(); this.ensurePorts(); }
}

// Details panel content for a picked reference object: [title, html]
export function refDetails(ref, runwaysHtml) {
  const it = ref.item;
  const pos2 = nf(it.lat, 4) + ", " + nf(it.lon, 4);
  if (ref.kind === "airport" || ref.kind === "medairport") {
    return [it.name, kv([["ICAO", esc(it.icao || it.ident)], ["IATA", esc(it.iata)], ["Type", esc((it.type || "").replace("_", " "))], ["Military", it.is_military ? "Yes" : "No"],
      ["Elevation", it.elevation_ft == null ? "" : nf(it.elevation_ft) + " ft (" + nf(it.elevation_ft * 0.3048) + " m)"], ["City", esc(it.municipality)], ["Country", esc(it.iso_country)],
      ["Runways", runwaysHtml || (it.runway_count ? nf(it.runway_count) : "–")], ["Position", pos2]])];
  }
  if (ref.kind === "runway") {
    return ["Runway " + (it.le || "") + "/" + (it.he || ""), kv([["Airport", esc(it.airport)], ["Length", it.length_ft == null ? "" : nf(it.length_ft) + " ft (" + nf(it.length_ft * 0.3048) + " m)"],
      ["Width", it.width_ft == null ? "" : nf(it.width_ft) + " ft"], ["Surface", esc(it.surface)]])];
  }
  if (ref.kind === "port") {
    return [it.name, kv([["Country", esc(it.country)], ["Harbour size", esc(it.harbor_size)], ["Harbour type", esc(it.harbor_type)], ["Source", esc(it.source)], ["Position", pos2]])];
  }
  if (ref.kind === "base") {
    return [it.name, kv([["Type", esc((it.type || "").replace("_", " "))], ["Operator", esc(it.operator)], ["Country", esc(it.country)], ["Notes", esc(it.notes)], ["Position", pos2]])];
  }
  if (ref.kind === "chokepoint") return [it.name, kv([["Chokepoint", esc(it.note)], ["Position", pos2]])];
  return [it.name || "Boundary", kv([["Layer", esc(it.layer)]])];
}

export async function airportRunwaysHtml(it) {
  try {
    const d = await fetchJson("/api/reference/runways?bbox=" + [it.lon - 0.08, it.lat - 0.08, it.lon + 0.08, it.lat + 0.08].map((x) => x.toFixed(4)).join(","), 8000);
    const rows = (d.items || []).filter((r) => r.airport === it.ident);
    if (!rows.length) return "–";
    return rows.map((r) => esc((r.le || "?") + "/" + (r.he || "?")) + " " + nf(r.length_ft) + " ft " + esc(r.surface || "")).join("<br>");
  } catch (e) { return null; }
}

export const BASE_CHIPS = Object.keys(BASE_COLORS).map((k) => [k, BASE_COLORS[k]]);
