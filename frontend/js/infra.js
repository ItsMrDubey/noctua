// Critical infrastructure overlays: submarine cables + landing points, oil/gas pipelines, power
// plants (fuel colour, capacity size), refineries, LNG terminals, large dams, satellite ground
// stations, heliports / small airfields. Self-mounting: builds its own layer-tree section.
// Perf rules: each line layer is ONE Primitive (built once, toggled via .show); point layers are
// loaded once into Point/Billboard collections with GPU-side distance display conditions
// (LOD by camera height); labels come from a small recycled pool for the current view only.
import { fetchJson } from "/static/js/api.js";
import { esc, nf, kv } from "/static/js/ui.js";

const C = Cesium;
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const col = (css, a = 1) => C.Color.fromCssColorString(css).withAlpha(a);
const pos = (lon, lat) => C.Cartesian3.fromDegrees(lon, lat, 0);
// flat [lon, lat, ...] without consecutive duplicates (rounded source data repeats vertices;
// a polyline that collapses to one point makes Cesium's geometry batching throw)
function flatLine(line) {
  const out = [];
  for (const [x, y] of line) if (!out.length || x !== out[out.length - 2] || y !== out[out.length - 1]) out.push(x, y);
  return out.length >= 4 ? out : null;
}

export const FUEL = {
  nuclear: ["Nuclear", "#ff5fd2"], coal: ["Coal", "#9aa3ad"], gas: ["Gas", "#ff9f43"], oil: ["Oil", "#d0743c"],
  hydro: ["Hydro", "#3aa0ff"], wind: ["Wind", "#7ee0c3"], solar: ["Solar", "#ffe14a"], other: ["Other", "#b49cff"],
};
const PIPE = { gas: ["Gas", "#ffd24a"], oil: ["Oil", "#ff6a3d"], other: ["Other", "#9fb3c8"] };
const AIRF = { heliport: ["Heliports", "#9fd3ff"], small_airport: ["Small airfields", "#36c5ff"], seaplane_base: ["Seaplane", "#5ee3ea"] };
const PT = { // billboard layers: [label, colour, glyph svg body]
  refineries: ["Oil refinery", "#ff6a3d", '<path d="M5 17V9h3v8M12 17V5h3v12M3 17h14"/><path d="M13.5 5c0-1.5 1-2 1-3"/>'],
  lng: ["LNG terminal", "#5ee3ea", '<ellipse cx="10" cy="7" rx="6" ry="2.2"/><path d="M4 7v6c0 1.2 2.7 2.2 6 2.2s6-1 6-2.2V7"/>'],
  dams: ["Dam", "#6aa8ff", '<path d="M3 5h14l-3 11H6z"/><path d="M6 9h8M7 12.5h6"/>'],
  ground_stations: ["Satellite ground station", "#c9a7ff", '<path d="M4 13a7 7 0 0 1 9-9z"/><path d="M8.5 8.5l4-4M8 13l-2 4h7"/><circle cx="13.5" cy="3.5" r="1"/>'],
};
const glyph = (body, c) => "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="' + c + '" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + body + "</svg>");
const SUB = { cable: "Submarine cable", landing: "Cable landing point", pipeline: "Pipeline", power: "Power plant", airfield: "Airfield",
  refineries: "Oil refinery", lng: "LNG terminal", dams: "Dam", ground_stations: "Satellite ground station" };
const TINT = { cable: "#36c5ff", landing: "#36c5ff", pipeline: "#ffd24a", power: "#ff9f43", airfield: "#9fd3ff",
  refineries: "#ff6a3d", lng: "#5ee3ea", dams: "#6aa8ff", ground_stations: "#c9a7ff" };

// shared constants (never allocate per item)
const SCALE = new C.NearFarScalar(3e5, 1.0, 2e7, 0.45);
const BB_SCALE = new C.NearFarScalar(5e5, 1.0, 2.5e7, 0.5);
const BLACK = col("#000", 0.85);
const LABEL_OFFSET = new C.Cartesian2(10, 0);
const DDC = { all: new C.DistanceDisplayCondition(0, Number.MAX_VALUE), far: new C.DistanceDisplayCondition(0, 9e6),
  mid: new C.DistanceDisplayCondition(0, 4e6), near: new C.DistanceDisplayCondition(0, 1.5e6), close: new C.DistanceDisplayCondition(0, 5e5) };
// power LOD: capacity -> max camera distance
const powerDdc = (mw) => (mw >= 1500 ? DDC.all : mw >= 500 ? DDC.far : mw >= 100 ? DDC.mid : mw >= 20 ? DDC.near : DDC.close);
const powerPx = (mw) => Math.max(3.5, Math.min(13, 2.2 * Math.log10(Math.max(1, mw || 1)) + 1.5));
const LABEL_MAX_H = 1.5e6, LABEL_CAP = 260, AIRF_MAX_H = 7e5;

export const INFRA_IDS = ["i-cables", "i-landings", "i-pipelines", "i-power", "i-refineries", "i-lng", "i-dams", "i-ground", "i-airfields"];

export class InfraLayers {
  constructor(viewer) {
    this.viewer = viewer; this.scene = viewer.scene;
    this.state = {}; this.loaded = {}; this.prim = {}; this.items = {};
    this.fuels = new Set(Object.keys(FUEL)); this.pipes = new Set(Object.keys(PIPE)); this.airTypes = new Set(["heliport"]);
    this.airMil = false; this.offshoreOnly = false;
    const P = this.scene.primitives;
    this.points = P.add(new C.PointPrimitiveCollection({ blendOption: C.BlendOption.OPAQUE }));
    this.landings = P.add(new C.PointPrimitiveCollection({ blendOption: C.BlendOption.OPAQUE }));
    this.airPts = P.add(new C.PointPrimitiveCollection({ blendOption: C.BlendOption.OPAQUE }));
    this.bb = P.add(new C.BillboardCollection({ scene: this.scene }));
    this.pool = P.add(new C.LabelCollection());
    this.points.show = this.landings.show = false;
    this.airKey = null; this.labelT = 0; this.onCounts = null;
    this.imgs = Object.fromEntries(Object.entries(PT).map(([k, v]) => [k, glyph(v[2], v[1])]));
    viewer.camera.moveEnd.addEventListener(() => { clearTimeout(this.labelT); this.labelT = setTimeout(() => this.viewChanged(), 180); });
  }
  req() { this.scene.requestRender(); }
  kick(ms = 4000) { const end = Date.now() + ms; const t = setInterval(() => { this.req(); if (Date.now() > end) clearInterval(t); }, 200); }
  count(k, n) { this.items[k + "#n"] = n; if (this.onCounts) this.onCounts(k, n); }

  setOn(id, on) {
    this.state[id] = on;
    const done = () => { this.relabel(); this.req(); };
    if (id === "i-cables") return this.ensureCables().then(() => { if (this.prim.cables) this.prim.cables.show = on; done(); });
    if (id === "i-landings") return this.ensureCables().then(() => { this.landings.show = on; done(); });
    if (id === "i-pipelines") return this.ensurePipelines().then(() => { this.applyPipes(); done(); });
    if (id === "i-power") return this.ensurePower().then(() => { this.points.show = on; done(); });
    if (id === "i-airfields") { this.airKey = null; return this.refreshAirfields().then(done); }
    const layer = { "i-refineries": "refineries", "i-lng": "lng", "i-dams": "dams", "i-ground": "ground_stations" }[id];
    if (layer) return this.ensurePoints(layer).then(() => { for (const b of this.items[layer] || []) b._b.show = on; done(); });
    return Promise.resolve();
  }
  setFuels(set) { this.fuels = set; for (const it of this.items.power || []) it._p.show = set.has(it.group); this.relabel(); this.req(); }
  setPipes(set) { this.pipes = set; this.applyPipes(); }
  setOffshore(on) { this.offshoreOnly = on; this.applyPipes(); }
  applyPipes() {
    const on = !!this.state["i-pipelines"];
    for (const k of Object.keys(PIPE)) for (const off of [true, false]) {
      const p = this.prim["pipe:" + k + ":" + off];
      if (p) p.show = on && this.pipes.has(k) && (off || !this.offshoreOnly);
    }
    this.req();
  }
  setAirTypes(set) { this.airTypes = set; this.airKey = null; this.refreshAirfields().then(() => this.relabel()); }
  setAirMil(on) { this.airMil = on; this.airKey = null; this.refreshAirfields().then(() => this.relabel()); }

  // ---- loaders ----
  _once(key, fn) {
    if (!this.loaded[key]) this.loaded[key] = fn().catch((e) => { this.loaded[key] = null; console.warn("infra", key, e); this.count(key, null); });
    return this.loaded[key];
  }
  ensureCables() {
    return this._once("cables", async () => {
      const d = await fetchJson("/api/infra/cables", 30000);
      const inst = [];
      for (const c of d.cables || []) {
        const color = C.ColorGeometryInstanceAttribute.fromColor(col(c.color || "#36c5ff", 0.8));
        const mid = c.lines[0][c.lines[0].length >> 1];
        const id = { ref: true, kind: "infra", sub: "cable", item: { ...c, lines: undefined, lon: mid[0], lat: mid[1], segments: c.lines.length } };
        for (const ln of c.lines) {
          const flat = flatLine(ln);
          if (!flat) continue;
          inst.push(new C.GeometryInstance({ geometry: new C.PolylineGeometry({ positions: C.Cartesian3.fromDegreesArray(flat), width: 1.6, vertexFormat: C.PolylineColorAppearance.VERTEX_FORMAT, arcType: C.ArcType.GEODESIC }),
            attributes: { color }, id }));
        }
      }
      if (inst.length) {
        this.prim.cables = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PolylineColorAppearance({ translucent: true }), asynchronous: true, releaseGeometryInstances: true }));
        this.prim.cables.show = !!this.state["i-cables"];
      }
      const lc = col("#bff3ff", 0.95), out = BLACK;
      this.items.landings = (d.landings || []).map((l) => {
        const p = this.landings.add({ position: pos(l.lon, l.lat), pixelSize: 4.5, color: lc, outlineColor: out, outlineWidth: 1, scaleByDistance: SCALE, distanceDisplayCondition: DDC.far });
        p.id = { ref: true, kind: "infra", sub: "landing", item: l };
        l._p = p;
        return l;
      });
      this.landings.show = !!this.state["i-landings"];
      this.count("cables", (d.cables || []).length); this.count("landings", this.items.landings.length);
      this.kick();
    });
  }
  ensurePipelines() {
    return this._once("pipelines", async () => {
      const d = await fetchJson("/api/infra/pipelines", 60000);
      const groups = {};
      for (const p of d.items || []) {
        const flat = p.line && flatLine(p.line);
        if (!flat) continue;
        const k = (PIPE[p.substance] ? p.substance : "other") + ":" + !!p.offshore;
        const mid = p.line[p.line.length >> 1];
        (groups[k] = groups[k] || []).push(new C.GeometryInstance({ geometry: new C.SimplePolylineGeometry({ positions: C.Cartesian3.fromDegreesArray(flat) }),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(col(PIPE[p.substance] ? PIPE[p.substance][1] : PIPE.other[1], p.offshore ? 0.95 : 0.6)) },
          id: { ref: true, kind: "infra", sub: "pipeline", item: { ...p, line: undefined, lon: mid[0], lat: mid[1], vertices: p.line.length } } }));
      }
      for (const [k, inst] of Object.entries(groups)) {
        this.prim["pipe:" + k] = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }), asynchronous: true, releaseGeometryInstances: true }));
      }
      this.applyPipes();
      this.count("pipelines", (d.items || []).length);
      this.kick();
    });
  }
  ensurePower() {
    return this._once("power", async () => {
      const d = await fetchJson("/api/infra/points/power", 60000);
      const cols = Object.fromEntries(Object.entries(FUEL).map(([k, v]) => [k, col(v[1], 0.95)]));
      this.items.power = d.items || [];
      for (const it of this.items.power) {
        const p = this.points.add({ position: pos(it.lon, it.lat), pixelSize: powerPx(it.mw), color: cols[it.group] || cols.other, outlineColor: BLACK, outlineWidth: 1,
          scaleByDistance: SCALE, distanceDisplayCondition: powerDdc(it.mw || 0), show: this.fuels.has(it.group) });
        p.id = { ref: true, kind: "infra", sub: "power", item: it };
        it._p = p;
      }
      this.count("power", this.items.power.length);
    });
  }
  ensurePoints(layer) {
    return this._once(layer, async () => {
      const d = await fetchJson("/api/infra/points/" + layer, 30000);
      const ddc = layer === "dams" ? DDC.far : DDC.all;
      this.items[layer] = d.items || [];
      for (const it of this.items[layer]) {
        const b = this.bb.add({ position: pos(it.lon, it.lat), image: this.imgs[layer], width: 17, height: 17, scaleByDistance: BB_SCALE, distanceDisplayCondition: ddc, show: false });
        b.id = { ref: true, kind: "infra", sub: layer, item: it };
        it._b = b;
      }
      this.count(layer, this.items[layer].length);
    });
  }
  // heliports / small airfields: viewport-loaded, only when zoomed in
  async refreshAirfields() {
    const h = this.viewer.camera.positionCartographic.height;
    if (!this.state["i-airfields"] || h > AIRF_MAX_H || !this.airTypes.size) {
      if (this.airPts.length) this.airPts.removeAll();
      this.airKey = null; this.items.airfields = [];
      this.count("airfields", this.state["i-airfields"] && h > AIRF_MAX_H ? "zoom" : null);
      this.req();
      return;
    }
    const [w, s, e, n] = this.viewBBox();
    const key = [w, s, e, n].map((x) => x.toFixed(1)).join(",") + "|" + [...this.airTypes].sort() + this.airMil;
    if (key === this.airKey) return;
    this.airKey = key;
    try {
      const d = await fetchJson("/api/infra/airfields?bbox=" + [w, s, e, n].map((x) => x.toFixed(3)).join(",") + "&type=" + [...this.airTypes].join(",") + (this.airMil ? "&military_only=true" : ""), 20000);
      if (key !== this.airKey) return;
      this.airPts.removeAll();
      this.items.airfields = d.items || [];
      for (const a of this.items.airfields) {
        const p = this.airPts.add({ position: pos(a.lon, a.lat), pixelSize: a.type === "heliport" ? 5 : 6, color: col(a.is_military ? "#ff4d4d" : AIRF[a.type][1], 0.95), outlineColor: BLACK, outlineWidth: 1 });
        p.id = { ref: true, kind: "infra", sub: "airfield", item: a };
        a._p = p;
      }
      this.count("airfields", this.items.airfields.length + (d.truncated ? "+" : ""));
      this.req();
    } catch (e) { this.airKey = null; }
  }

  // ---- view-dependent: airfields + label pool ----
  viewBBox() {
    const r = this.viewer.camera.computeViewRectangle(this.scene.globe.ellipsoid);
    if (!r) return [-180, -90, 180, 90];
    const d = C.Math.toDegrees;
    const w = d(r.west), e = d(r.east);
    if ((e >= w ? e - w : e - w + 360) >= 340) return [-180, d(r.south), 180, d(r.north)];
    return [w, d(r.south), e, d(r.north)];
  }
  viewChanged() {
    if (this.state["i-airfields"]) this.refreshAirfields().then(() => this.relabel());
    else this.relabel();
  }
  relabel() {
    const pool = this.pool;
    let used = 0;
    const h = this.viewer.camera.positionCartographic.height;
    if (h < LABEL_MAX_H) {
      const [w, s, e, n] = this.viewBBox();
      const inLon = w <= e ? (lon) => lon >= w && lon <= e : (lon) => lon >= w || lon <= e;
      const inView = (it) => it.lat >= s && it.lat <= n && inLon(it.lon);
      const cands = [];
      const add = (list, pr, color, text, ok) => { for (const it of list || []) if (ok(it) && inView(it)) cands.push([pr, it, color, text(it)]); };
      for (const layer of Object.keys(PT)) {
        const sid = { refineries: "i-refineries", lng: "i-lng", dams: "i-dams", ground_stations: "i-ground" }[layer];
        if (this.state[sid]) add(this.items[layer], 1, PT[layer][1], (it) => it.name, () => true);
      }
      if (this.state["i-power"]) {
        const min = h < 2e5 ? 0 : h < 6e5 ? 100 : 500;
        add(this.items.power, 2, null, (it) => it.name + (it.mw ? " " + nf(it.mw) + " MW" : ""), (it) => (it.mw || 0) >= min && this.fuels.has(it.group));
      }
      if (this.state["i-landings"] && h < 8e5) add(this.items.landings, 3, "#bff3ff", (it) => it.name, () => true);
      if (this.state["i-airfields"] && h < 1.5e5) add(this.items.airfields, 4, "#9fd3ff", (it) => (it.id || "") + " " + it.name, () => true);
      if (cands.length > LABEL_CAP) cands.sort((a, b) => a[0] - b[0] || (b[1].mw || 0) - (a[1].mw || 0));
      for (const [, it, color, text] of cands.slice(0, LABEL_CAP)) {
        let l = used < pool.length ? pool.get(used) : null;
        if (!l) l = pool.add({ font: FONT, scale: 0.82, outlineColor: BLACK, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT, pixelOffset: LABEL_OFFSET });
        if (l._k !== it) {
          const prim = it._b || it._p;
          l._k = it; l.text = String(text || "").toUpperCase().slice(0, 48); l.position = prim.position; l.id = prim.id;
          l.fillColor = col(color || FUEL[it.group][1], 0.9);
        }
        l.show = true;
        used++;
      }
    }
    for (let i = used; i < pool.length; i++) pool.get(i).show = false;
    this.req();
  }
  // named point features for the command palette
  places() {
    const out = [];
    for (const k of [...Object.keys(PT), "power"]) for (const it of this.items[k] || []) if (it.name) out.push({ kind: k, name: it.name, item: it, ref: (it._b || it._p).id });
    return out;
  }
}

const link = (u, t) => (/^https:\/\//i.test(u || "") ? '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">' + esc(t || u) + "</a>" : esc(t || ""));
const pos2 = (it) => nf(it.lat, 4) + ", " + nf(it.lon, 4);
const wd = (q) => (q && /^Q\d+$/.test(q) ? link("https://www.wikidata.org/wiki/" + q, q) : "");

// Inspector content for a picked infrastructure feature: [title, html, eyebrow label, tint]
export function infraDetails(ref) {
  const it = ref.item || {}, sub = ref.sub;
  const label = SUB[sub] || "Infrastructure", tint = TINT[sub] || "#3ddcc2";
  let rows;
  if (sub === "cable") {
    rows = [["Cable", esc(it.name)], ["Segments", nf(it.segments)], ["Details", link("https://www.submarinecablemap.com/submarine-cable/" + encodeURIComponent(it.id || ""), "submarinecablemap.com")],
      ["Source", "TeleGeography (public map; commercial use needs a licence)"]];
  } else if (sub === "landing") {
    rows = [["Landing", esc(it.name)], ["Status", it.tbd ? "TBD" : ""], ["Position", pos2(it)], ["Source", "TeleGeography"]];
  } else if (sub === "pipeline") {
    rows = [["Substance", esc(it.substance)], ["Setting", it.offshore ? "Offshore / submarine" : "Onshore"], ["Operator", esc(it.operator)], ["Diameter", esc(it.diameter)],
      ["Since", esc(it.start_date)], ["OSM", link("https://www.openstreetmap.org/way/" + String(it.id || "").replace(/^osm:w/, ""), it.id)], ["Source", "OpenStreetMap (ODbL)"]];
  } else if (sub === "power") {
    rows = [["Fuel", esc(it.fuel) + (it.other_fuels ? " (+" + esc(it.other_fuels.join(", ")) + ")" : "")], ["Capacity", it.mw == null ? "" : nf(it.mw, 1) + " MW"], ["Country", esc(it.country)],
      ["Owner", esc(it.owner)], ["Commissioned", it.year || ""], ["GPPD id", esc(it.id)], ["Position", pos2(it)], ["Data source", it.url ? link(it.url, it.source || "source") : esc(it.source)],
      ["Licence", "WRI Global Power Plant Database (CC BY 4.0)"]];
  } else if (sub === "airfield") {
    rows = [["Ident", esc(it.id)], ["Type", esc((it.type || "").replace(/_/g, " "))], ["Military", it.is_military ? "Yes" : "No"], ["City", esc(it.municipality)], ["Country", esc(it.country)],
      ["Elevation", it.elevation_ft == null ? "" : nf(it.elevation_ft) + " ft"], ["Position", pos2(it)], ["Source", "OurAirports (public domain)"]];
  } else {
    const osm = String(it.id || "").startsWith("osm:");
    rows = [["Country", esc(it.country)], ["Operator", esc(it.operator)], ["Product", esc(it.product)], ["Height", it.height_m == null ? "" : nf(it.height_m) + " m"], ["Since", esc(it.inception)],
      ["Wikidata", wd(it.wikidata)], ["Position", pos2(it)], ["Source", osm ? "OpenStreetMap (ODbL)" : "Wikidata (CC0)"]];
  }
  return [it.name || label, kv(rows), label, tint];
}

// ---- layer tree section (built here so index.html stays untouched) ----
const row = (id, sw, c, text, extra = "", sub = false, title = "") =>
  '<label class="row' + (sub ? " sub" : "") + '"' + (title ? ' title="' + esc(title) + '"' : "") + '><input type="checkbox" id="' + id + '"' + extra + '><span class="sw ' + sw + '" style="--c:' + c + '"></span>' + text + '<b id="c-' + id + '" class="num"></b></label>';
const chips = (id, map, on) => '<div id="' + id + '" class="chips">' + Object.entries(map).map(([k, [l, c]]) => '<button class="chip' + (on.has(k) ? " on" : "") + '" data-k="' + k + '" style="--c:' + c + ';cursor:pointer">' + l + "</button>").join("") + "</div>";

export function mountInfraTree(layers, tree) {
  const sec = document.createElement("section");
  sec.className = "grp"; sec.dataset.g = "infra";
  sec.innerHTML = '<header><h3>Infrastructure</h3><b id="g-infra-n" class="num">–</b></header>' +
    row("i-cables", "ln", "#36c5ff", "Submarine cables", "", false, "TeleGeography public submarine cable map") +
    row("i-landings", "ring", "#bff3ff", "Cable landings", "", true) +
    row("i-pipelines", "ln", "#ffd24a", "Oil & gas pipelines", "", false, "OpenStreetMap transmission and submarine pipelines") +
    chips("i-pipe-chips", PIPE, layers.pipes) +
    '<label class="row sub"><input type="checkbox" id="i-offshore"><span class="sw ln" style="--c:#ffd24a"></span>Offshore only</label>' +
    row("i-power", "", "#ff9f43", "Power plants", "", false, "WRI Global Power Plant Database; dot size = capacity, smaller plants appear as you zoom in") +
    chips("i-fuel-chips", FUEL, layers.fuels) +
    row("i-refineries", "sq", PT.refineries[1], "Oil refineries") +
    row("i-lng", "sq", PT.lng[1], "LNG terminals") +
    row("i-dams", "sq", PT.dams[1], "Dams ≥ 50 m") +
    row("i-ground", "sq", PT.ground_stations[1], "Sat ground stations") +
    row("i-airfields", "", "#9fd3ff", "Heliports & airfields <small>zoom &lt; 700 km</small>") +
    chips("i-air-chips", AIRF, layers.airTypes) +
    '<label class="row sub"><input type="checkbox" id="i-air-mil"><span class="sw sq" style="--c:#ff4d4d"></span>Military only</label>' +
    '<div class="row-note" id="i-note"></div>';
  const after = document.querySelector('#left .grp[data-g="ref"]');
  if (after) after.after(sec); else document.getElementById("left").appendChild(sec);
  if (tree && tree.closed) sec.classList.toggle("closed", tree.closed.has("infra"));
  sec.querySelector("header").addEventListener("click", () => (tree ? tree.setClosed(sec, !sec.classList.contains("closed")) : sec.classList.toggle("closed")));
  // rail button
  const rb = document.createElement("button");
  rb.className = "rail-btn"; rb.dataset.g = "infra"; rb.title = "Critical infrastructure";
  rb.innerHTML = '<svg viewBox="0 0 20 20"><path d="M2 15c3-4 5-4 8 0s5 4 8 0" /><path d="M6 3v6M14 3v6M4 6h4M12 6h4" /></svg>';
  const refBtn = document.querySelector('.rail-btn[data-g="ref"]');
  if (refBtn) refBtn.after(rb);
  rb.addEventListener("click", () => tree && tree.focusGroup("infra"));
  // wiring
  const $i = (id) => document.getElementById(id);
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("noctua.infra") || "{}") || {}; } catch (e) { saved = {}; }
  const save = () => { try { localStorage.setItem("noctua.infra", JSON.stringify(Object.fromEntries(INFRA_IDS.map((id) => [id, $i(id).checked])))); } catch (e) { /* storage disabled */ } };
  for (const id of INFRA_IDS) {
    const box = $i(id);
    box.checked = !!saved[id];
    box.addEventListener("change", () => { layers.setOn(id, box.checked); save(); });
    if (box.checked) layers.setOn(id, true);
  }
  const chipSet = (id, set, apply) => $i(id).addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    if (b.classList.contains("on")) set.add(b.dataset.k); else set.delete(b.dataset.k);
    apply(set);
  });
  chipSet("i-pipe-chips", layers.pipes, (s) => layers.setPipes(s));
  chipSet("i-fuel-chips", layers.fuels, (s) => layers.setFuels(s));
  chipSet("i-air-chips", layers.airTypes, (s) => layers.setAirTypes(s));
  $i("i-offshore").addEventListener("change", (e) => layers.setOffshore(e.target.checked));
  $i("i-air-mil").addEventListener("change", (e) => layers.setAirMil(e.target.checked));
  const ids = { cables: "i-cables", landings: "i-landings", pipelines: "i-pipelines", power: "i-power", refineries: "i-refineries", lng: "i-lng", dams: "i-dams", ground_stations: "i-ground", airfields: "i-airfields" };
  layers.onCounts = (k, n) => {
    const el = $i("c-" + ids[k]);
    if (!el) return;
    el.textContent = n == null ? "n/a" : n === "zoom" ? "" : typeof n === "number" ? n.toLocaleString() : String(n);
    if (n == null) el.closest("label").title = "Not available: run `osint infra-fetch` to download this dataset";
    let tot = 0;
    for (const key of ["cables", "pipelines", "power", "refineries", "lng", "dams", "ground_stations"]) { const v = layers.items[key + "#n"]; if (typeof v === "number") tot += v; }
    $i("g-infra-n").textContent = tot ? tot.toLocaleString() : "–";
  };
  // dataset availability / staleness note
  fetchJson("/api/infra/summary", 15000).then((s) => {
    const missing = Object.entries(s.layers || {}).filter(([, v]) => !v.available).map(([k]) => k.replace("_", " "));
    $i("i-note").textContent = missing.length ? "Missing: " + missing.join(", ") + " · run osint infra-fetch" : "";
    for (const [k, v] of Object.entries(s.layers || {})) if (k !== "airfields" && v.available && !(k + "#n" in layers.items)) layers.count(k, v.count);
  }).catch(() => { for (const id of INFRA_IDS) { const b = $i(id); b.disabled = true; b.closest("label").title = "Infrastructure layers not supported by this backend version"; } });
  return sec;
}
