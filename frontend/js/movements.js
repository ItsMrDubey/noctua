// Military movements: airlift corridors (flowing arcs), recent legs, surge cells, naval units.
// Perf: one collection per sub-layer, rebuilt only when the fetched data or the role filter
// changes (signature diff); the flow/pulse animation is a ~10 fps uniform update that only runs
// while an animated sub-layer is visible and the page is visible (requestRenderMode stays on).
import { fetchJson } from "/static/js/api.js";
import { esc } from "/static/js/ui.js";

const C = Cesium;
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
export const MOV_ROLES = {
  airlift: ["Airlift", "#36c5ff"], tanker: ["Tanker", "#4fdf8c"], isr: ["ISR", "#c9a7ff"],
  fighter: ["Fighter", "#ff4b55"], helo: ["Helo", "#ffd24a"], other: ["Other", "#9fb0c0"],
};
const AMBER = "#f2ad3d", NAVAL = "#ff4b55";
const roleColor = (r) => (MOV_ROLES[r] || MOV_ROLES.other)[1];
const col = (css, a = 1) => C.Color.fromCssColorString(css).withAlpha(a);
const POLL_MS = 120000, TICK_MS = 100;
const BLACK85 = C.Color.BLACK.withAlpha(0.85);

// flowing dash: bright pulses travel origin -> destination along st.s, soft edge across st.t
const FLOW_SRC = `
czm_material czm_getMaterial(czm_materialInput materialInput) {
  czm_material m = czm_getDefaultMaterial(materialInput);
  vec2 st = materialInput.st;
  float t = fract(st.s * repeat - offset);
  float pulse = smoothstep(0.0, 0.25, t) * (1.0 - smoothstep(0.35, 0.55, t));
  float edge = 1.0 - abs(st.t - 0.5) * 2.0;
  m.diffuse = color.rgb;
  m.emission = color.rgb * (0.25 + 0.75 * pulse);
  m.alpha = color.a * (0.22 + 0.78 * pulse) * pow(edge, 0.7);
  return m;
}`;
const flowMaterial = (css) => new C.Material({ fabric: { type: "NoctuaFlow_" + css.slice(1), uniforms: { color: col(css, 0.95), offset: 0, repeat: 5 }, source: FLOW_SRC }, translucent: true });

const SHIP_IMG = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 20 20" fill="none" stroke="' + NAVAL + '" stroke-width="1.4" stroke-linejoin="round">' +
  '<path d="M10 1.8l3.6 4.4v10.6l-3.6 1.6-3.6-1.6V6.2z"/><path d="M8.2 8.5h3.6M10 6v6"/></svg>');
const SHIP_SCALE = new C.NearFarScalar(2e5, 1.1, 2e7, 0.6);

function arcPositions(path) {
  // lift the great circle into an arc proportional to its length (capped)
  const n = path.length;
  if (n < 2) return [];
  const [lo0, la0] = path[0], [lo1, la1] = path[n - 1];
  const r = Math.PI / 180;
  const dk = 12742 * Math.asin(Math.sqrt(Math.sin(((la1 - la0) * r) / 2) ** 2 + Math.cos(la0 * r) * Math.cos(la1 * r) * Math.sin(((lo1 - lo0) * r) / 2) ** 2));
  const peak = Math.min(4e5, Math.max(1.5e4, dk * 60));
  const out = [];
  for (let i = 0; i < n; i++) out.push(path[i][0], path[i][1], Math.sin((Math.PI * i) / (n - 1)) * peak);
  return C.Cartesian3.fromDegreesArrayHeights(out);
}

export class MovementsLayer {
  constructor(viewer) {
    this.viewer = viewer; this.scene = viewer.scene;
    this.on = { corridors: false, legs: false, surges: false, naval: false };
    this.roles = new Set(Object.keys(MOV_ROLES));
    this.data = { corridors: [], legs: [], surges: [], naval: [] };
    this.meta = { surges: null };
    this.sig = {};
    this.unavailable = false;
    const P = this.scene.primitives;
    this.cCol = P.add(new C.PolylineCollection());
    this.lCol = P.add(new C.PolylineCollection());
    this.sCol = P.add(new C.PolylineCollection());
    this.sLab = P.add(new C.LabelCollection());
    this.nTrk = P.add(new C.PolylineCollection());
    this.nBb = P.add(new C.BillboardCollection({ scene: this.scene }));
    this.flow = {}; this.legMat = {};
    for (const r of Object.keys(MOV_ROLES)) {
      this.flow[r] = flowMaterial(roleColor(r));
      this.legMat[r] = C.Material.fromType("Color", { color: col(roleColor(r), 0.5) });
    }
    this.surgeMat = C.Material.fromType("Color", { color: col(AMBER, 0.9) });
    this.navalMat = C.Material.fromType("PolylineDash", { color: col(NAVAL, 0.6), dashLength: 10 });
    this.phase = 0; this.timer = null; this.pollT = null; this.loading = false;
    this.onData = null;
    this._apply();
    document.addEventListener("visibilitychange", () => this._anim());
  }
  req() { this.scene.requestRender(); }
  any() { return Object.values(this.on).some(Boolean); }
  set(name, on) {
    this.on[name] = on;
    this._apply();
    if (on) this.load(name); else this._anim();
    if (this.any() && !this.pollT) this.pollT = setInterval(() => this.loadAll(), POLL_MS);
    if (!this.any() && this.pollT) { clearInterval(this.pollT); this.pollT = null; }
  }
  setRoles(set) { this.roles = set; this.sig.corridors = this.sig.legs = null; this.build("corridors"); this.build("legs"); this.req(); if (this.onData) this.onData(); }
  _apply() {
    this.cCol.show = this.on.corridors; this.lCol.show = this.on.legs;
    this.sCol.show = this.sLab.show = this.on.surges; this.nTrk.show = this.nBb.show = this.on.naval;
    this.req();
  }
  async loadAll() { for (const k of Object.keys(this.on)) if (this.on[k]) await this.load(k); }
  // panel data is wanted even with the map layers off
  async load(name) {
    const url = { corridors: "/api/movements/corridors?hours=168", legs: "/api/movements/legs?hours=24", surges: "/api/movements/surges?hours=24", naval: "/api/movements/naval?hours=24" }[name];
    try {
      const d = await fetchJson(url, 60000);
      this.unavailable = false;
      this.data[name] = Array.isArray(d && d.items) ? d.items : [];
      if (name === "surges") this.meta.surges = d;
      this.build(name);
      if (this.onData) this.onData(name);
    } catch (e) {
      this.unavailable = true;
      if (this.onData) this.onData(name, e);
    }
  }
  visibleRole(it) { return this.roles.has(MOV_ROLES[it.role] ? it.role : "other"); }
  build(name) {
    const items = this.data[name] || [];
    const sig = name + items.length + "|" + [...this.roles].join(",") + "|" + items.map((x) => x.id + ":" + (x.count || x.current || x.ts || x.t_end || "")).join(",");
    if (this.sig[name] === sig) return;
    this.sig[name] = sig;
    if (name === "corridors") {
      this.cCol.removeAll();
      for (const c of items) {
        if (!this.visibleRole(c) || !c.path || c.path.length < 2) continue;
        const mid = c.path[c.path.length >> 1];
        c.lon = mid[0]; c.lat = mid[1];
        const p = this.cCol.add({ positions: arcPositions(c.path), width: Math.min(9, 2 + 2.2 * Math.log2(1 + c.count)), material: this.flow[MOV_ROLES[c.role] ? c.role : "other"] });
        p.id = { ref: true, kind: "corridor", item: c };
      }
    } else if (name === "legs") {
      this.lCol.removeAll();
      for (const lg of items) {
        if (!this.visibleRole(lg) || !lg.path || lg.path.length < 2) continue;
        const last = lg.path[lg.path.length - 1];
        lg.lon = last[0]; lg.lat = last[1];
        const p = this.lCol.add({ positions: C.Cartesian3.fromDegreesArray(lg.path.flat()), width: 1.3, material: this.legMat[MOV_ROLES[lg.role] ? lg.role : "other"] });
        p.id = { ref: true, kind: "leg", item: lg };
      }
    } else if (name === "surges") {
      this.sCol.removeAll(); this.sLab.removeAll();
      for (const s of items) {
        const ring = [...s.boundary, s.boundary[0]].flat();
        const p = this.sCol.add({ positions: C.Cartesian3.fromDegreesArray(ring), width: 2, material: this.surgeMat });
        p.id = { ref: true, kind: "surge", item: s };
        const l = this.sLab.add({ position: C.Cartesian3.fromDegrees(s.lon, s.lat), text: surgeText(s), font: FONT, fillColor: col(AMBER, 0.95),
          outlineColor: BLACK85, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.CENTER,
          distanceDisplayCondition: new C.DistanceDisplayCondition(0, 1.4e7) });
        l.id = p.id;
      }
    } else if (name === "naval") {
      this.nTrk.removeAll(); this.nBb.removeAll();
      for (const n of items) {
        if (n.track && n.track.length > 1) this.nTrk.add({ positions: C.Cartesian3.fromDegreesArray(n.track.flat()), width: 1.4, material: this.navalMat });
        const b = this.nBb.add({ position: C.Cartesian3.fromDegrees(n.lon, n.lat), image: SHIP_IMG, width: 22, height: 22, alignedAxis: C.Cartesian3.UNIT_Z,
          rotation: -C.Math.toRadians(n.heading_deg || 0), scaleByDistance: SHIP_SCALE });
        b.id = { ref: true, kind: "naval", item: n };
      }
    }
    this._anim();
    this.req();
  }
  // ~10 fps uniform animation, only while something animated is on screen
  _anim() {
    const want = !document.hidden && ((this.on.corridors && this.cCol.length > 0) || (this.on.surges && this.sCol.length > 0));
    if (want && !this.timer) {
      this.timer = setInterval(() => {
        this.phase = (this.phase + TICK_MS / 1000) % 1000;
        const off = (this.phase * 0.35) % 1;
        for (const m of Object.values(this.flow)) m.uniforms.offset = off;
        this.surgeMat.uniforms.color.alpha = 0.45 + 0.5 * (0.5 + 0.5 * Math.sin(this.phase * 4));
        this.req();
      }, TICK_MS);
    } else if (!want && this.timer) { clearInterval(this.timer); this.timer = null; }
  }
  // right-panel content: top corridors + active surges + naval
  panelHtml() {
    if (this.unavailable && !this.data.corridors.length) return '<div class="mv-empty">Movements API unavailable on this backend.</div>';
    const cors = this.data.corridors.filter((c) => this.visibleRole(c)).slice(0, 12);
    const surges = this.data.surges;
    const sm = this.meta.surges;
    let h = '<h4 class="mv-h">Top corridors <small>7 d · ' + this.data.corridors.length + "</small></h4>";
    h += cors.length ? cors.map((c, i) => '<div class="mv-row" data-k="corridor" data-i="' + this.data.corridors.indexOf(c) + '"><i style="--c:' + roleColor(c.role) + '"></i><span>' +
      esc(apName(c.origin)) + " → " + esc(apName(c.dest)) + "</span><em>" + esc((MOV_ROLES[c.role] || MOV_ROLES.other)[0]) + "</em><b>" + c.count + "</b></div>").join("") : '<div class="mv-empty">No resolved corridors yet (needs legs with known origin and destination).</div>';
    h += '<h4 class="mv-h">Active surges <small>24 h</small></h4>';
    if (surges.length) h += surges.map((s, i) => '<div class="mv-row" data-k="surge" data-i="' + i + '"><i class="hx" style="--c:' + AMBER + '"></i><span>' + esc(s.cell) + "</span><em>z " + Number(s.z).toFixed(1) + "</em><b>" + esc(surgeText(s)) + "</b></div>").join("");
    else h += '<div class="mv-empty">' + (sm && sm.baseline_insufficient ? "Baseline still building (" + Number(sm.history_days || 0).toFixed(1) + " d of history; flags need ≥ 2 d)." : "No surge cells flagged.") + "</div>";
    h += '<h4 class="mv-h">Naval units <small>' + this.data.naval.length + "</small></h4>";
    h += this.data.naval.length ? this.data.naval.slice(0, 20).map((n, i) => '<div class="mv-row" data-k="naval" data-i="' + i + '"><i style="--c:' + NAVAL + '"></i><span>' + esc(n.name || n.id) + "</span><em>" + esc(n.match === "ais_type" ? "AIS mil" : "name") + "</em><b></b></div>").join("")
      : '<div class="mv-empty">No military vessels (needs AIS data).</div>';
    return h;
  }
  pick(kind, i) { const arr = { corridor: this.data.corridors, surge: this.data.surges, naval: this.data.naval }[kind]; const it = arr && arr[i]; return it ? { ref: true, kind, item: it } : null; }
  ensurePanel() { for (const k of ["corridors", "surges", "naval"]) if (!this.data[k].length && !this.sig[k]) this.load(k); }
}

const apName = (a) => (a ? a.icao || a.iata || a.ident || a.name : "unknown");
export function surgeText(s) { return s.ratio != null ? Number(s.ratio).toFixed(1) + "× baseline" : "z " + Number(s.z).toFixed(1); }

const kv = (rows) => '<div class="kv">' + rows.filter((r) => r[1] != null && r[1] !== "").map((r) => "<span>" + esc(r[0]) + "</span><div>" + r[1] + "</div>").join("") + "</div>";
const t16 = (s) => esc(String(s || "").replace("T", " ").slice(0, 16)) + (s ? " UTC" : "");
const apFull = (a, how) => (a ? esc((a.icao || a.ident) + " · " + (a.name || "")) + (how && how !== "ground" ? " <small>(" + esc(how) + ")</small>" : "") : "<small>unknown</small>");

export const MOV_KINDS = { corridor: "Corridor", leg: "Flight leg", surge: "Activity surge", naval: "Naval unit" };
export function movementDetails(ref) {
  const it = ref.item || {};
  const role = (MOV_ROLES[it.role] || MOV_ROLES.other)[0];
  if (ref.kind === "corridor") {
    return [apName(it.origin) + " → " + apName(it.dest), kv([["Origin", apFull(it.origin)], ["Destination", apFull(it.dest)], ["Role", esc(role)],
      ["Flights", esc(it.count) + " legs · " + esc(it.aircraft) + " aircraft (7 d)"], ["Last seen", t16(it.last_seen)], ["Callsigns", esc((it.callsigns || []).join(", "))],
      ["Types", esc((it.types || []).join(", "))]])];
  }
  if (ref.kind === "leg") {
    return [(it.callsign || it.track_id) + " · " + apName(it.origin) + " → " + apName(it.dest), kv([["Callsign", esc(it.callsign)], ["Type", esc(it.type_code)], ["Role", esc(role)],
      ["Origin", apFull(it.origin, it.origin_how)], ["Destination", apFull(it.dest, it.dest_how)], ["Status", esc(String(it.status || "").replace("_", " "))],
      ["Departed", t16(it.t_start)], ["Last fix", t16(it.t_end)], ["Max altitude", it.max_alt_ft == null ? "" : Number(it.max_alt_ft).toLocaleString() + " ft"], ["Fixes", esc(it.n_fixes)]])];
  }
  if (ref.kind === "surge") {
    return ["Surge " + surgeText(it), kv([["H3 cell", esc(it.cell)], ["Aircraft", esc(it.current) + " distinct military aircraft"], ["Baseline", esc(it.baseline_mean) + " ± " + esc(it.baseline_std) + " (" + esc(it.baseline_n) + " days, same 6 h slot)"],
      ["z-score", esc(it.z)], ["Window", t16(it.bucket_start) + " + " + esc(it.bucket_hours || 6) + " h"], ["Callsigns", esc((it.callsigns || []).join(", "))]])];
  }
  return [it.name || it.id, kv([["Name", esc(it.name)], ["MMSI / ID", esc(it.id)], ["Matched by", it.match === "ais_type" ? "AIS ship type 35 (military)" : "Hull prefix in name"],
    ["Operator", esc(it.operator)], ["Speed", it.speed_kts == null ? "" : Number(it.speed_kts).toFixed(1) + " kt"], ["Last fix", t16(it.ts)],
    ["Position", Number(it.lat).toFixed(4) + ", " + Number(it.lon).toFixed(4)]])];
}
