// Reference geography: tactical wireframe overlays (borders, chokepoints, airports, runways, ports, bases).
import { fetchJson } from "/static/js/api.js";
import { esc } from "/static/js/ui.js";

const C = Cesium;
const FONT = "11px 'JetBrains Mono', Consolas, monospace";
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
const pos = (lon, lat, h = 0) => C.Cartesian3.fromDegrees(lon, lat, h);
const nf = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const kv = (rows) => '<div class="kv">' + rows.filter((r) => r[1] != null && r[1] !== "").map((r) => "<span>" + esc(r[0]) + "</span><div>" + r[1] + "</div>").join("") + "</div>";

export const REF_IDS = ["r-airports", "r-airports-med", "r-airports-mil", "r-runways", "r-ports", "r-bases", "r-rings", "r-borders", "r-disputed", "r-maritime", "r-choke", "r-clabels"];

export class ReferenceLayers {
  constructor(viewer, onPick) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.onPick = onPick;
    this.cache = new Map();
    this.state = {};
    this.bases = null;
    const P = this.scene.primitives;
    this.lines = {};
    for (const k of ["land", "disputed", "maritime", "choke", "rings", "runways"]) this.lines[k] = P.add(new C.PolylineCollection());
    this.chokeFill = [];
    this.chokeLabels = P.add(new C.LabelCollection());
    this.countryLabels = P.add(new C.LabelCollection());
    this.pts = {};
    for (const k of ["airports", "ports", "bases"]) {
      this.pts[k] = { map: new Map(), bb: P.add(new C.BillboardCollection({ scene: this.scene })), lb: P.add(new C.LabelCollection()) };
    }
    this.loaded = {};
    this.timer = null;
    viewer.camera.moveEnd.addEventListener(() => this.schedule());
  }

  // ---- toggles ----
  setOn(id, on) {
    this.state[id] = on;
    this.apply(id);
  }
  apply(id) {
    const s = this.state;
    if (id === "r-borders") { this.ensureBoundary("land"); this.lines.land.show = !!s[id]; }
    else if (id === "r-disputed") { this.ensureBoundary("disputed"); this.lines.disputed.show = !!s[id]; }
    else if (id === "r-maritime") { this.ensureBoundary("maritime"); this.lines.maritime.show = !!s[id]; }
    else if (id === "r-choke") { this.ensureChoke(); this.setChokeShow(!!s[id]); }
    else if (id === "r-clabels") { this.ensureCountries(); this.countryLabels.show = !!s[id]; }
    else if (id === "r-bases" || id === "r-rings") { this.ensureBases().then(() => this.refreshBases()); }
    else if (id === "r-runways") { this.lines.runways.show = !!s[id]; this.schedule(50); }
    else this.schedule(50);
  }
  setBaseTypes(set) { this.baseTypes = set; this.ensureBases().then(() => this.refreshBases()); }
  schedule(ms = 400) { clearTimeout(this.timer); this.timer = setTimeout(() => this.refresh().catch(() => {}), ms); }

  // ---- static line layers ----
  async ensureBoundary(name) {
    if (this.loaded[name]) return;
    this.loaded[name] = true;
    try {
      const gj = await fetchJson("/api/reference/boundaries/" + name, 30000);
      const mat = name === "land" ? C.Material.fromType("Color", { color: col(CYAN, 0.55) })
        : C.Material.fromType("PolylineDash", { color: col(name === "disputed" ? AMBER : TEAL, name === "disputed" ? 0.85 : 0.6), dashLength: 14 });
      for (const f of gj.features || []) {
        const g = f.geometry;
        const parts = g.type === "LineString" ? [g.coordinates] : g.coordinates;
        for (const p of parts) {
          if (p.length < 2) continue;
          const l = this.lines[name].add({ positions: C.Cartesian3.fromDegreesArray(p.flat()), width: name === "land" ? 1 : 1.2, material: mat });
          l.id = { ref: true, kind: "boundary", item: { lat: p[0][1], lon: p[0][0], name: f.properties.name || name + " boundary", layer: name } };
        }
      }
    } catch (e) { this.loaded[name] = false; console.warn("boundary", name, e); }
  }
  async ensureChoke() {
    if (this.loaded.choke) return;
    this.loaded.choke = true;
    try {
      const d = await fetchJson("/api/reference/chokepoints", 15000);
      for (const c of d.items || []) {
        const flat = c.ring.flat();
        const l = this.lines.choke.add({ positions: C.Cartesian3.fromDegreesArray(flat), width: 1.6, material: C.Material.fromType("Color", { color: col(AMBER, 0.9) }) });
        l.id = { ref: true, kind: "chokepoint", item: c };
        this.chokeFill.push(this.viewer.entities.add({ show: !!this.state["r-choke"], polygon: { hierarchy: C.Cartesian3.fromDegreesArray(flat), material: col(AMBER, 0.07) } }));
        const lb = this.chokeLabels.add({
          position: pos(c.lon, c.lat), text: c.name.toUpperCase(), font: FONT, scale: 0.9, fillColor: col(AMBER, 0.95), outlineColor: col("#000", 0.8),
          outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -12), distanceDisplayCondition: new C.DistanceDisplayCondition(0, 6e6),
        });
        lb.id = { ref: true, kind: "chokepoint", item: c };
      }
      this.setChokeShow(!!this.state["r-choke"]);
    } catch (e) { this.loaded.choke = false; console.warn("chokepoints", e); }
  }
  setChokeShow(on) {
    this.lines.choke.show = on; this.chokeLabels.show = on;
    for (const e of this.chokeFill) e.show = on;
  }
  async ensureCountries() {
    if (this.loaded.countries) return;
    this.loaded.countries = true;
    try {
      const d = await fetchJson("/api/reference/countries", 15000);
      for (const c of d.items || []) {
        this.countryLabels.add({
          position: pos(c.lon, c.lat), text: c.name.toUpperCase(), font: FONT, scale: 0.85, fillColor: col(CYAN, 0.7), outlineColor: col("#000", 0.7),
          outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE, distanceDisplayCondition: new C.DistanceDisplayCondition(1.5e6, 4e7),
        });
      }
      this.countryLabels.show = !!this.state["r-clabels"];
    } catch (e) { this.loaded.countries = false; console.warn("countries", e); }
  }

  // ---- bases (small curated list: load once, filter client side) ----
  async ensureBases() {
    if (this.bases) return;
    try {
      const d = await fetchJson("/api/reference/bases", 15000);
      this.bases = (d.items || []).map((b, i) => ({ ...b, id: "base:" + i, cls: baseClass(b.type) }));
    } catch (e) { console.warn("bases", e); this.bases = []; }
  }
  refreshBases() {
    const on = !!this.state["r-bases"];
    const types = this.baseTypes || new Set(Object.keys(BASE_COLORS));
    const list = on ? (this.bases || []).filter((b) => types.has(b.cls)) : [];
    this.syncPoints("bases", list, (b) => ({
      lat: b.lat, lon: b.lon, image: ICONS.base(BASE_COLORS[b.cls]), size: 17, label: b.name, labelMax: 1.2e6, color: BASE_COLORS[b.cls], kind: "base",
    }));
    // range rings
    const rl = this.lines.rings;
    rl.removeAll();
    if (this.state["r-rings"] && on) {
      for (const b of list) {
        for (const km of [100, 300, 500]) {
          const pts = [];
          for (let a = 0; a <= 360; a += 6) {
            const r = km / 6371, br = (a * Math.PI) / 180, la = (b.lat * Math.PI) / 180, lo = (b.lon * Math.PI) / 180;
            const la2 = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(br));
            const lo2 = lo + Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(la2));
            pts.push((lo2 * 180) / Math.PI, (la2 * 180) / Math.PI);
          }
          rl.add({ positions: C.Cartesian3.fromDegreesArray(pts), width: 1, material: C.Material.fromType("PolylineDash", { color: col(BASE_COLORS[b.cls], 0.35), dashLength: 10 }) });
        }
      }
    }
  }

  // ---- viewport-driven layers ----
  viewBBox() {
    const r = this.viewer.camera.computeViewRectangle(this.scene.globe.ellipsoid);
    if (!r) return [-180, -90, 180, 90];
    const d = C.Math.toDegrees;
    const pad = 0.1;
    const w = d(r.west), e = d(r.east), s = d(r.south), n = d(r.north);
    const lonSpan = e >= w ? e - w : e - w + 360;
    const latSpan = n - s;
    if (lonSpan >= 340) return [-180, Math.max(-90, s - latSpan * pad), 180, Math.min(90, n + latSpan * pad)];
    const wrap = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
    return [wrap(w - lonSpan * pad), Math.max(-90, s - latSpan * pad), wrap(e + lonSpan * pad), Math.min(90, n + latSpan * pad)];
  }
  async cached(url) {
    if (this.cache.has(url)) return this.cache.get(url);
    const d = await fetchJson(url, 20000);
    this.cache.set(url, d);
    if (this.cache.size > 40) this.cache.delete(this.cache.keys().next().value);
    return d;
  }
  async refresh() {
    const s = this.state;
    const h = this.viewer.camera.positionCartographic.height;
    const bb = this.viewBBox();
    const q = (b) => b.map((x) => Math.round(x * 2) / 2).join(",");
    const key = q([Math.floor(bb[0]), Math.floor(bb[1]), Math.ceil(bb[2]), Math.ceil(bb[3])]);
    const jobs = [];
    if (s["r-airports"]) {
      const med = !!s["r-airports-med"], mil = !!s["r-airports-mil"];
      jobs.push(this.cached("/api/reference/airports?bbox=" + key + "&min_class=" + (med ? "medium" : "large") + "&military_only=" + mil)
        .then((d) => this.syncPoints("airports", d.items || [], (a) => {
          const large = a.type === "large_airport";
          return {
            id: "ap:" + a.ident, lat: a.lat, lon: a.lon, image: a.is_military ? ICONS.milairport(RED) : ICONS.airport(CYAN), size: large ? 15 : 12,
            label: (a.icao || a.ident) + " " + a.name, labelMax: large ? 1.5e6 : 6e5, color: a.is_military ? RED : CYAN, kind: "airport",
          };
        })));
    } else jobs.push(Promise.resolve(this.syncPoints("airports", [], null)));
    if (s["r-ports"]) {
      jobs.push(this.cached("/api/reference/ports?bbox=" + key).then((d) => this.syncPoints("ports", d.items || [], (p) => ({
        id: "pt:" + p.name + p.lat.toFixed(2) + p.lon.toFixed(2), lat: p.lat, lon: p.lon, image: ICONS.port(TEAL), size: 13, label: p.name, labelMax: 8e5, color: TEAL, kind: "port",
      }))));
    } else jobs.push(Promise.resolve(this.syncPoints("ports", [], null)));
    // runways: only when zoomed in
    this.lines.runways.show = !!s["r-runways"];
    if (s["r-runways"] && h < 4e5) {
      let [w, so, e, n] = bb;
      if (e < w || e - w > 4.5 || n - so > 4.5) {
        const c = this.viewer.camera.positionCartographic;
        const lo = C.Math.toDegrees(c.longitude), la = C.Math.toDegrees(c.latitude);
        [w, so, e, n] = [lo - 2, la - 2, lo + 2, la + 2];
      }
      const rk = [w, so, e, n].map((x) => x.toFixed(2)).join(",");
      jobs.push(this.cached("/api/reference/runways?bbox=" + rk).then((d) => this.drawRunways(d.items || [])).catch(() => {}));
    } else if (this.lines.runways.length) this.lines.runways.removeAll();
    await Promise.allSettled(jobs);
  }
  drawRunways(items) {
    const L = this.lines.runways;
    L.removeAll();
    const mat = C.Material.fromType("Color", { color: col(CYAN, 0.95) });
    for (const r of items) {
      const flat = [...r.rect, r.rect[0]].flat();
      const l = L.add({ positions: C.Cartesian3.fromDegreesArray(flat), width: 1.4, material: mat });
      l.id = { ref: true, kind: "runway", item: { ...r, lat: r.rect[0][1], lon: r.rect[0][0] } };
    }
  }

  // Generic point-set sync. make(item) -> {id,lat,lon,image,size,label,labelMax,color,kind}
  syncPoints(layer, items, make) {
    const L = this.pts[layer];
    const seen = new Set();
    if (make) {
      for (const it of items) {
        const m = make(it);
        const id = m.id || it.id;
        seen.add(id);
        if (L.map.has(id)) continue;
        const b = L.bb.add({
          position: pos(m.lon, m.lat), image: m.image, width: m.size, height: m.size,
          scaleByDistance: new C.NearFarScalar(5e5, 1.0, 2.5e7, 0.5), translucencyByDistance: new C.NearFarScalar(1e6, 0.95, 3e7, 0.6),
        });
        const ref = { ref: true, kind: m.kind, item: it };
        b.id = ref;
        const l = L.lb.add({
          position: pos(m.lon, m.lat), text: m.label.toUpperCase(), font: FONT, scale: 0.85, fillColor: col(m.color, 0.9), outlineColor: col("#000", 0.85), outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT, pixelOffset: new C.Cartesian2(10, 0),
          distanceDisplayCondition: new C.DistanceDisplayCondition(0, m.labelMax),
        });
        l.id = ref;
        L.map.set(id, { b, l });
      }
    }
    for (const [id, o] of [...L.map]) {
      if (!seen.has(id)) { L.bb.remove(o.b); L.lb.remove(o.l); L.map.delete(id); }
    }
  }
}

// Details panel content for a picked reference object: [title, html]
export function refDetails(ref, runwaysHtml) {
  const it = ref.item;
  const pos2 = nf(it.lat, 4) + ", " + nf(it.lon, 4);
  if (ref.kind === "airport") {
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
