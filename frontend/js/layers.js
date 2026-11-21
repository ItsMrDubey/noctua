// Primitive collections updated incrementally by id.
const C = Cesium;
import { aircraftRole, aircraftGlyph, shipClass, shipGlyph, COLORS } from "/static/js/symbology.js";

const AIR_MIL = C.Color.fromCssColorString(COLORS.air);
const AIR_CIV = C.Color.fromCssColorString(COLORS.airCiv);
const AIR_RED = C.Color.fromCssColorString(COLORS.hp);
const SHIP_BLUE = C.Color.fromCssColorString(COLORS.ship);
const SHIP_RED = C.Color.fromCssColorString(COLORS.shipMil);
const SHIP_SCALE = new C.NearFarScalar(2e5, 1.1, 2e7, 0.55);
const SHIP_FADE = new C.NearFarScalar(2e6, 1.0, 3e7, 0.85);

// shared per-layer constants: never allocate these per item
const AIR_SCALE = new C.NearFarScalar(5e5, 1.2, 2.5e7, 0.75);
const AIR_FADE = new C.NearFarScalar(1e6, 1.0, 3e7, 0.9);
const PT_SCALE = new C.NearFarScalar(1e6, 1.2, 2e7, 1.0);
const PT_FADE = new C.NearFarScalar(2e6, 1.0, 3e7, 0.9);
const Q_SCALE = new C.NearFarScalar(1e6, 1.3, 2e7, 1.0);
const BLACK70 = C.Color.BLACK.withAlpha(0.7);
const QUAKE = C.Color.fromCssColorString(COLORS.quake).withAlpha(0.85);
const FIRE_RAMP = Array.from({ length: 11 }, (_, i) => C.Color.fromHsl(0.09 * (1 - i / 10), 1, 0.5, 0.9));

function pos(lat, lon, altM) { return C.Cartesian3.fromDegrees(lon, lat, altM || 0); }

// Generic incremental sync keyed by id. Subclasses implement make() and apply().
export class Layer {
  constructor(scene, collection) {
    this.col = scene.primitives.add(collection);
    this.map = new Map();
    this.items = new Map();
    this.filter = () => true;
  }
  set show(v) { this.col.show = v; if (this.onChange) this.onChange(); }
  get show() { return this.col.show; }
  upsert(list) {
    const seen = new Set();
    let changed = 0;
    for (const it of list) {
      if (it == null || it.id == null || it.lat == null || it.lon == null) continue;
      seen.add(it.id);
      this.items.set(it.id, it);
      let p = this.map.get(it.id);
      if (!p) { p = this.make(it); p.id = it.id; this.map.set(it.id, p); }
      // only touch the primitive when something visible changed
      const k = this.key(it);
      if (p._k !== k) { p._k = k; this.apply(p, it); changed++; }
      const show = this.filter(it);
      if (p.show !== show) { p.show = show; changed++; }
    }
    if (changed && this.onChange) this.onChange();
    return seen;
  }
  key(it) { return it.lat + "|" + it.lon + "|" + it.alt_ft + "|" + it.heading_deg + "|" + (it.high_priority ? 1 : 0) + (it.is_mil ? 1 : 0); }
  removeIds(ids) {
    for (const id of ids) {
      const p = this.map.get(id);
      if (p) { this.col.remove(p); this.map.delete(id); this.items.delete(id); }
    }
    if (ids.length && this.onChange) this.onChange();
  }
  sync(list) {
    const seen = this.upsert(list);
    this.removeIds([...this.map.keys()].filter((id) => !seen.has(id)));
  }
  refilter() { for (const [id, p] of this.map) p.show = this.filter(this.items.get(id)); if (this.onChange) this.onChange(); }
  visibleItems() { return this.col.show ? [...this.items.values()].filter(this.filter) : []; }
  positionOf(id) { const p = this.map.get(id); return p ? p.position : null; }
}

export class AircraftLayer extends Layer {
  constructor(scene) { super(scene, new C.BillboardCollection({ scene })); this.kind = "aircraft"; }
  make(it) {
    return this.col.add({
      image: aircraftGlyph(aircraftRole(it)), alignedAxis: C.Cartesian3.UNIT_Z, width: 28, height: 28,
      scaleByDistance: AIR_SCALE, translucencyByDistance: AIR_FADE,
    });
  }
  key(it) { return it.lat + "|" + it.lon + "|" + it.alt_ft + "|" + it.heading_deg + "|" + (it.high_priority ? 1 : 0) + (it.is_mil ? 1 : 0) + it.type_code + "|" + it.category; }
  apply(p, it) {
    p.position = pos(it.lat, it.lon, (it.alt_ft || 0) * 0.3048);
    p.rotation = -C.Math.toRadians(it.heading_deg || 0);
    const role = aircraftRole(it);
    if (p._role !== role) { p._role = role; p.image = aircraftGlyph(role); }
    p.color = it.high_priority ? AIR_RED : it.is_mil ? AIR_MIL : AIR_CIV;
    p.width = p.height = it.high_priority ? 34 : role === "helo" || role === "uav" || role === "trainer" ? 24 : 28;
  }
}

class PointLayer extends Layer {
  constructor(scene) { super(scene, new C.PointPrimitiveCollection()); }
  make() { return this.col.add({ position: C.Cartesian3.ZERO, scaleByDistance: PT_SCALE, translucencyByDistance: PT_FADE }); }
}

export class ShipLayer extends Layer {
  constructor(scene) { super(scene, new C.BillboardCollection({ scene })); this.kind = "ships"; }
  make(it) {
    return this.col.add({ image: shipGlyph(shipClass(it)), alignedAxis: C.Cartesian3.UNIT_Z, width: 18, height: 18, scaleByDistance: SHIP_SCALE, translucencyByDistance: SHIP_FADE });
  }
  key(it) { return it.lat + "|" + it.lon + "|" + it.heading_deg + "|" + (it.is_mil ? 1 : 0) + it.type_code; }
  apply(p, it) {
    p.position = pos(it.lat, it.lon, 0);
    p.rotation = -C.Math.toRadians(it.heading_deg || 0);
    const cls = shipClass(it);
    if (p._cls !== cls) { p._cls = cls; p.image = shipGlyph(cls); }
    p.color = it.is_mil ? SHIP_RED : SHIP_BLUE;
    p.width = p.height = it.is_mil ? 22 : 18;
  }
}

export class FireLayer extends PointLayer {
  constructor(scene) { super(scene); this.kind = "fires"; }
  key(it) { return it.lat + "|" + it.lon + "|" + it.frp; }
  apply(p, it) {
    p.position = pos(it.lat, it.lon, 0);
    const t = Math.min(1, Math.max(0, (it.frp || 0) / 100));
    p.color = FIRE_RAMP[Math.round(t * 10)]; // orange -> red
    p.pixelSize = 6 + 3 * t;
  }
}

export class QuakeLayer extends PointLayer {
  constructor(scene) { super(scene); this.kind = "quakes"; }
  key(it) { return it.lat + "|" + it.lon + "|" + it.mag; }
  apply(p, it) {
    p.position = pos(it.lat, it.lon, 0);
    p.color = QUAKE;
    p.outlineColor = C.Color.BLACK;
    p.outlineWidth = 1;
    p.pixelSize = Math.max(7, 3 + (it.mag || 0) * 2.2);
    p.scaleByDistance = Q_SCALE;
  }
}

// ---- Imagery ----
const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/";
export const BASE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
export function makeImagery(kind, date) {
  const credit = "NASA GIBS";
  if (kind === "base") return new C.UrlTemplateImageryProvider({ url: BASE_URL, maximumLevel: 19, credit: "Esri, Maxar, Earthstar Geographics" });
  if (kind === "labels") return new C.UrlTemplateImageryProvider({ url: "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", maximumLevel: 13, credit: "Esri" });
  if (kind === "viirs") return new C.UrlTemplateImageryProvider({ url: GIBS + "VIIRS_SNPP_CorrectedReflectance_TrueColor/default/" + date + "/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg", maximumLevel: 9, credit });
  return new C.UrlTemplateImageryProvider({ url: GIBS + "VIIRS_SNPP_DayNightBand_At_Sensor_Radiance/default/" + date + "/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png", maximumLevel: 8, credit });
}
