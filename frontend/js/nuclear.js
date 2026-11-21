// Known nuclear sites (Wikidata CC0 + curated public list): wireframe trefoil billboards coloured
// by type group, status by stroke (operating solid, shut down dimmed, under construction dashed),
// optional 30/100 km rings, lazy label pool (zoomed in only). Loaded once; filters toggle .show.
import { fetchJson } from "/static/js/api.js";
import { esc } from "/static/js/ui.js";

const C = Cesium;
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
export const NUC_GROUPS = {
  power: ["Power", "#ffd24a"], research: ["Research", "#5ee3ea"], fuel_cycle: ["Fuel cycle", "#c9a7ff"],
  weapons: ["Weapons & labs", "#ff4b55"], test: ["Test sites", "#ff8a3d"],
};
const GROUP_OF = { power: "power", research: "research", weapons_lab: "weapons", test_site: "test" };
export const nucGroup = (t) => GROUP_OF[t] || "fuel_cycle";
const TYPE_LABEL = {
  power: "Nuclear power plant", research: "Research reactor / centre", enrichment: "Enrichment", reprocessing: "Reprocessing",
  conversion: "Uranium conversion", fuel_fab: "Fuel fabrication", weapons_lab: "Weapons programme / laboratory", test_site: "Nuclear test site",
  storage: "Storage / waste", heavy_water: "Heavy water", other: "Nuclear facility",
};
const DIM = new Set(["shut_down", "decommissioned", "decommissioning", "closed", "inactive"]);
const styleOf = (st) => (st === "under_construction" ? "dash" : DIM.has(st) ? "dim" : "solid");
const STATUS_LABEL = (s) => String(s || "unknown").replace(/_/g, " ");

// trefoil: three 60° annular blades + hub, outline only
function blade(a0) {
  const r1 = 3.4, r2 = 8.6, c = 10, rad = (d) => (d * Math.PI) / 180;
  const p = (r, a) => (c + r * Math.cos(rad(a))).toFixed(2) + " " + (c - r * Math.sin(rad(a))).toFixed(2);
  return "M" + p(r1, a0) + " L" + p(r2, a0) + " A" + r2 + " " + r2 + " 0 0 0 " + p(r2, a0 + 60) + " L" + p(r1, a0 + 60) + " A" + r1 + " " + r1 + " 0 0 1 " + p(r1, a0) + "Z";
}
const TREFOIL = blade(60) + blade(180) + blade(300);
function glyph(color, style) {
  const dash = style === "dash" ? ' stroke-dasharray="2 1.6"' : "";
  const op = style === "dim" ? ' opacity="0.45"' : "";
  return "data:image/svg+xml;utf8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 20 20" fill="none" stroke="' + color + '" stroke-width="1.25" stroke-linejoin="round"' + op + ">" +
    '<path d="' + TREFOIL + '"' + dash + '/><circle cx="10" cy="10" r="1.5"/>' + (style === "solid" ? '<circle cx="10" cy="10" r="9.4" stroke-width=".6" opacity=".55"/>' : "") + "</svg>");
}
const IMG = {};
for (const [g, [, c]] of Object.entries(NUC_GROUPS)) for (const s of ["solid", "dim", "dash"]) IMG[g + ":" + s] = glyph(c, s);
export const nucGlyphUrl = (it) => IMG[nucGroup(it.type) + ":" + styleOf(it.status)];

const BB_SCALE = new C.NearFarScalar(5e5, 1.0, 2.5e7, 0.55);
const BB_FADE = new C.NearFarScalar(1e6, 1.0, 3e7, 0.7);
const BLACK85 = C.Color.BLACK.withAlpha(0.85);
const LABEL_OFFSET = new C.Cartesian2(12, 0);
const LABEL_MAX_H = 1.8e6, LABEL_CAP = 160;
const RING_KM = [30, 100];

const toRad = Math.PI / 180;
export function distKm(la1, lo1, la2, lo2) {
  const p1 = la1 * toRad, p2 = la2 * toRad, dp = p2 - p1, dl = (lo2 - lo1) * toRad;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(a));
}

export class NuclearLayer {
  constructor(viewer) {
    this.viewer = viewer; this.scene = viewer.scene;
    this.items = []; this.on = false; this.ringsOn = false; this.groups = new Set(Object.keys(NUC_GROUPS));
    this.unavailable = false;
    const P = this.scene.primitives;
    this.bb = P.add(new C.BillboardCollection({ scene: this.scene }));
    this.rings = P.add(new C.PolylineCollection());
    this.labels = P.add(new C.LabelCollection());
    this.rings.show = false;
    this.loading = null;
    this.ringKey = null;
    this.labelT = 0;
    viewer.camera.moveEnd.addEventListener(() => { if (this.on) { clearTimeout(this.labelT); this.labelT = setTimeout(() => this.relabel(), 150); } });
  }
  req() { this.scene.requestRender(); }
  ensure() {
    if (this.loading) return this.loading;
    this.loading = fetchJson("/api/reference/nuclear", 30000).then((d) => {
      this.items = Array.isArray(d && d.items) ? d.items : [];
      for (const it of this.items) {
        const b = this.bb.add({ position: C.Cartesian3.fromDegrees(it.lon, it.lat), image: nucGlyphUrl(it), width: 20, height: 20,
          scaleByDistance: BB_SCALE, translucencyByDistance: BB_FADE, show: false });
        b.id = { ref: true, kind: "nuclear", item: it };
        it._b = b;
      }
      if (this.onLoad) this.onLoad(this.items);
    }).catch((e) => { this.unavailable = true; this.loading = null; console.warn("nuclear sites", e); throw e; });
    return this.loading;
  }
  setOn(on) { this.on = on; return this.ensure().then(() => this.refresh(), () => {}); }
  setGroups(set) { this.groups = set; if (this.items.length) this.refresh(); }
  setRings(on) { this.ringsOn = on; if (this.items.length) this.refresh(); }
  visible(it) { return this.on && this.groups.has(nucGroup(it.type)); }
  refresh() {
    for (const it of this.items) { const s = this.visible(it); if (it._b.show !== s) it._b.show = s; }
    this.buildRings();
    this.relabel();
    this.req();
  }
  // rings: one PolylineCollection rebuilt only when the visible group set changes
  buildRings() {
    const show = this.on && this.ringsOn;
    this.rings.show = show;
    if (!show) return;
    const key = [...this.groups].sort().join(",");
    if (key === this.ringKey) return;
    this.ringKey = key;
    this.rings.removeAll();
    const mats = {};
    for (const it of this.items) {
      const g = nucGroup(it.type);
      if (!this.groups.has(g)) continue;
      for (const km of RING_KM) {
        const mk = g + km;
        const mat = mats[mk] || (mats[mk] = C.Material.fromType("PolylineDash", { color: C.Color.fromCssColorString(NUC_GROUPS[g][1]).withAlpha(km === 30 ? 0.5 : 0.28), dashLength: km === 30 ? 8 : 14 }));
        const pts = [], r = km / 6371, la = it.lat * toRad, lo = it.lon * toRad;
        for (let a = 0; a <= 360; a += 8) {
          const br = a * toRad;
          const la2 = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(br));
          const lo2 = lo + Math.atan2(Math.sin(br) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(la2));
          pts.push(lo2 / toRad, la2 / toRad);
        }
        this.rings.add({ positions: C.Cartesian3.fromDegreesArray(pts), width: 1, material: mat });
      }
    }
  }
  // recycled label pool: visible sites in view, only when zoomed in
  relabel() {
    const pool = this.labels;
    let used = 0;
    const h = this.viewer.camera.positionCartographic.height;
    if (this.on && h < LABEL_MAX_H) {
      const r = this.viewer.camera.computeViewRectangle(this.scene.globe.ellipsoid);
      const d = C.Math.toDegrees;
      const [w, s, e, n] = r ? [d(r.west), d(r.south), d(r.east), d(r.north)] : [-180, -90, 180, 90];
      const inLon = w <= e ? (lon) => lon >= w && lon <= e : (lon) => lon >= w || lon <= e;
      for (const it of this.items) {
        if (used >= LABEL_CAP) break;
        if (!it._b.show || it.lat < s || it.lat > n || !inLon(it.lon)) continue;
        let l = used < pool.length ? pool.get(used) : null;
        if (!l) l = pool.add({ font: FONT, scale: 0.85, outlineColor: BLACK85, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT, pixelOffset: LABEL_OFFSET });
        if (l._k !== it) {
          l._k = it; l.text = String(it.name || "").toUpperCase(); l.position = it._b.position; l.id = it._b.id;
          l.fillColor = C.Color.fromCssColorString(NUC_GROUPS[nucGroup(it.type)][1]).withAlpha(DIM.has(it.status) ? 0.55 : 0.92);
        }
        l.show = true;
        used++;
      }
    }
    for (let i = used; i < pool.length; i++) pool.get(i).show = false;
    this.req();
  }
  // nearest loaded site within km (event tagging; independent of the layer toggle)
  near(lat, lon, km = 50) {
    let best = null, bd = km;
    if (lat == null || lon == null) return null;
    for (const it of this.items) {
      if (Math.abs(it.lat - lat) > 1 || Math.abs(((it.lon - lon + 540) % 360) - 180) > 3) continue;
      const dk = distKm(lat, lon, it.lat, it.lon);
      if (dk <= bd) { bd = dk; best = it; }
    }
    return best ? { site: best, km: bd } : null;
  }
  places() { return this.items.map((it) => ({ kind: "nuclear", name: it.name, item: it, ref: it._b.id })); }
}

const kv = (rows) => '<div class="kv">' + rows.filter((r) => r[1] != null && r[1] !== "").map((r) => "<span>" + esc(r[0]) + "</span><div>" + r[1] + "</div>").join("") + "</div>";
const safeUrl = (u) => (/^https:\/\//i.test(u || "") ? u : null);

export function nuclearDetails(it) {
  const src = safeUrl(it.source_url);
  const wd = it.wikidata && /^Q\d+$/.test(it.wikidata) ? "https://www.wikidata.org/wiki/" + it.wikidata : null;
  const link = (u, t) => '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">' + esc(t) + "</a>";
  return [it.name, kv([
    ["Type", esc(TYPE_LABEL[it.type] || it.type)], ["Status", '<span class="nst nst-' + esc(styleOf(it.status)) + '">' + esc(STATUS_LABEL(it.status)) + "</span>"],
    ["Country", esc(it.country)], ["Operator", esc(it.operator)], ["Capacity", it.capacity_mw == null ? "" : Number(it.capacity_mw).toLocaleString() + " MW"],
    ["Native name", esc(it.name_native)], ["In service", esc(it.service_entry)], ["Closed", esc(it.dissolved)], ["Notes", esc(it.notes)],
    ["Position", Number(it.lat).toFixed(4) + ", " + Number(it.lon).toFixed(4)],
    ["Source", src ? link(src, it.source === "Wikidata" ? "Wikidata " + (it.wikidata || "") : src.replace(/^https:\/\/(en\.)?/, "")) : esc(it.source)],
    ["Wikidata", wd && it.source !== "Wikidata" ? link(wd, it.wikidata) : ""], ["Licence", esc(it.licence)],
  ])];
}
