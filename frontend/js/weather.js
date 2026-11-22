// Weather: geostationary infrared cloud imagery (NASA GIBS: GOES-East, GOES-West, Himawari;
// refreshed every 10 min), precipitation radar (RainViewer), and volumetric 3D clouds placed
// from current low / mid / high cloud cover (Open-Meteo via /api/weather/clouds).
import { fetchJson } from "/static/js/api.js";

const C = Cesium;
const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/";
const IR_LAYERS = ["GOES-East_ABI_Band13_Clean_Infrared", "GOES-West_ABI_Band13_Clean_Infrared", "Himawari_AHI_Band13_Clean_Infrared"];
const IR_LAG_MIN = 30; // geostationary imagery reaches GIBS ~20-30 min after the scan
const CLOUD_MAX_EYE = 450e3;

/** Latest 10-minute slot that GIBS should already have, as an ISO timestamp. */
export function irSlot(now = Date.now()) {
  const t = new Date(now - IR_LAG_MIN * 60e3);
  t.setUTCMinutes(Math.floor(t.getUTCMinutes() / 10) * 10, 0, 0);
  return t.toISOString().replace(".000Z", "Z");
}

// deterministic jitter so a refresh does not reshuffle the clouds
function rand(seed) {
  let x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

const LEVELS = [
  // key, altitude (m), cloud size (m) at 0.25 deg grid, max clouds per node, brightness
  { key: "low", alt: 1600, w: 9000, h: 3200, n: 12, b: 1.0 },
  { key: "mid", alt: 4800, w: 13000, h: 3600, n: 8, b: 0.95 },
  { key: "high", alt: 9800, w: 20000, h: 1600, n: 4, b: 0.9 },
];

export class Weather {
  constructor(viewer) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.ir = [];
    this.radar = null;
    this.irOn = false; this.radarOn = false; this.cloudsOn = false;
    this.clouds = this.scene.primitives.add(new C.CloudCollection({ noiseDetail: 16.0, show: false }));
    this._timers = {};
    this._reqId = 0;
    this._lastKey = "";
    this.status = { clouds: 0, step: null, error: null };
    this.onStatus = () => {};
    this.scene.camera.moveEnd.addEventListener(() => { if (this.cloudsOn) this._loadClouds(); });
  }

  // ---- infrared cloud imagery ----
  setIR(on) {
    this.irOn = !!on;
    this._dropIR();
    clearInterval(this._timers.ir);
    if (!this.irOn) { this.scene.requestRender(); return; }
    this._addIR();
    this._timers.ir = setInterval(() => { this._dropIR(); this._addIR(); }, 10 * 60e3);
  }
  _addIR() {
    const slot = irSlot();
    const layers = this.viewer.imageryLayers;
    for (const name of IR_LAYERS) {
      const l = layers.addImageryProvider(new C.UrlTemplateImageryProvider({
        url: GIBS + name + "/default/" + slot + "/GoogleMapsCompatible_Level6/{z}/{y}/{x}.png",
        maximumLevel: 6, credit: "NASA GIBS (GOES / Himawari ABI band 13)",
      }));
      // band 13 (10.3 um): cold cloud tops bright, warm ground dark -> key the ground out
      l.colorToAlpha = C.Color.BLACK;
      l.colorToAlphaThreshold = 0.42;
      l.alpha = 0.8; l.brightness = 1.25; l.contrast = 1.3;
      this.ir.push(l);
    }
    this.slot = slot;
    this.scene.requestRender();
  }
  _dropIR() {
    for (const l of this.ir) this.viewer.imageryLayers.remove(l, true);
    this.ir = [];
  }

  // ---- precipitation radar ----
  async setRadar(on) {
    this.radarOn = !!on;
    clearInterval(this._timers.radar);
    if (this.radar) { this.viewer.imageryLayers.remove(this.radar, true); this.radar = null; }
    if (!this.radarOn) { this.scene.requestRender(); return; }
    await this._addRadar();
    this._timers.radar = setInterval(() => this._addRadar(), 10 * 60e3);
  }
  async _addRadar() {
    try {
      const idx = await fetchJson("https://api.rainviewer.com/public/weather-maps.json", 10000);
      const past = (idx.radar && idx.radar.past) || [];
      const last = past[past.length - 1];
      if (!last || !this.radarOn) return;
      const layer = this.viewer.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({
        url: idx.host + last.path + "/256/{z}/{x}/{y}/4/1_1.png", maximumLevel: 7, credit: "Radar: RainViewer",
      }));
      layer.alpha = 0.75;
      if (this.radar) this.viewer.imageryLayers.remove(this.radar, true);
      this.radar = layer;
      this.radarTime = new Date(last.time * 1000);
      this.scene.requestRender();
    } catch (e) { console.warn("weather: radar index unavailable", e); }
  }

  // ---- volumetric clouds ----
  setClouds(on) {
    this.cloudsOn = !!on;
    this.clouds.show = this.cloudsOn;
    clearInterval(this._timers.clouds);
    if (this.cloudsOn) {
      this._lastKey = "";
      this._loadClouds();
      this._timers.clouds = setInterval(() => { this._lastKey = ""; this._loadClouds(); }, 15 * 60e3);
    }
    this.scene.requestRender();
  }
  async _loadClouds() {
    const cam = this.scene.camera;
    if (cam.positionCartographic.height > CLOUD_MAX_EYE) {
      this.clouds.removeAll(); this._lastKey = "";
      this._status(0, null, "zoom in below " + CLOUD_MAX_EYE / 1000 + " km for 3D clouds");
      return;
    }
    const r = cam.computeViewRectangle(this.scene.globe.ellipsoid);
    if (!r) return;
    const d = C.Math.toDegrees;
    let [w, s, e, n] = [d(r.west), d(r.south), d(r.east), d(r.north)];
    // oblique views see to the horizon: cap the box around the point under the camera
    const c = cam.positionCartographic, clat = d(c.latitude), clon = d(c.longitude), cap = 9;
    w = Math.max(w, clon - cap); e = Math.min(e, clon + cap); s = Math.max(s, clat - cap); n = Math.min(n, clat + cap);
    if (e <= w || n <= s) return;
    const bbox = [w, s, e, n].map((v) => v.toFixed(2)).join(",");
    const key = [w, s, e, n].map((v) => Math.round(v * 4) / 4).join(",");
    if (key === this._lastKey) return;
    const id = ++this._reqId;
    let res;
    try { res = await fetchJson("/api/weather/clouds?bbox=" + bbox, 15000); } catch (err) { this._status(this.clouds.length, null, String(err.message || err)); return; }
    if (id !== this._reqId || !this.cloudsOn) return;
    this._lastKey = key;
    this._build(res.items || [], res.step || 0.25);
    this._status(this.clouds.length, res.step, res.error);
  }
  _build(items, step) {
    const coll = this.clouds;
    coll.removeAll();
    const scale = Math.sqrt(step / 0.25); // coarser grid -> fewer, larger clouds
    const span = step; // spread clouds over the node's cell
    const MAX = 2500;
    for (const it of items) {
      for (let li = 0; li < LEVELS.length; li++) {
        const L = LEVELS[li], cover = +it[L.key] || 0;
        if (cover < 8) continue;
        const count = Math.round((cover / 100) * L.n);
        for (let i = 0; i < count && coll.length < MAX; i++) {
          const seed = it.lat * 1e3 + it.lon * 7.13 + li * 101 + i * 17.7;
          const lat = it.lat + (rand(seed) - 0.5) * span, lon = it.lon + (rand(seed + 1) - 0.5) * span;
          const k = 0.7 + rand(seed + 2) * 0.6;
          const w = L.w * scale * k, h = L.h * scale * (0.8 + rand(seed + 3) * 0.4);
          coll.add({
            position: C.Cartesian3.fromDegrees(lon, lat, L.alt + (rand(seed + 4) - 0.5) * 600),
            scale: new C.Cartesian2(w, h),
            maximumSize: new C.Cartesian3(w * 0.5, h * 0.55, Math.min(w, h) * 0.45),
            slice: L.key === "high" ? 0.62 : 0.32 + rand(seed + 5) * 0.2,
            brightness: L.b * (cover > 85 && L.key !== "high" ? 0.82 : 1.0),
          });
        }
      }
    }
    this.scene.requestRender();
  }
  _status(n, step, error) {
    this.status = { clouds: n, step, error: error || null };
    this.onStatus(this.status);
  }
}

// ---- cinematic lighting ----
// Real-time "RTX-like" look within WebGL limits: soft sun shadows (models, buildings,
// terrain) and bloom, sun-lit globe and atmosphere.
export function setCinematic(viewer, on) {
  const s = viewer.scene;
  // HDR stays off: on this class of GPU Cesium's HDR tone mapping blew translucent overlays
  // into saturated blotches and crushed terrain shadows to black
  s.highDynamicRange = false;
  viewer.shadows = !!on;
  const sm = s.shadowMap;
  if (sm) { sm.softShadows = true; sm.size = 2048; sm.darkness = 0.55; sm.maximumDistance = 25000; }
  viewer.terrainShadows = on ? C.ShadowMode.RECEIVE_ONLY : C.ShadowMode.DISABLED;
  // no ambient occlusion: Cesium's AO stage combined with bloom renders a black frame (and AO
  // darkens HDR scenes); shadows + HDR + bloom give the depth cue without it
  s.postProcessStages.ambientOcclusion.enabled = false;
  const bloom = s.postProcessStages.bloom;
  bloom.enabled = !!on;
  if (on) Object.assign(bloom.uniforms, { glowOnly: false, contrast: 119, brightness: -0.45, delta: 0.9, sigma: 3.0, stepSize: 4.0 });
  s.requestRender();
}

/** Local solar hour at which the sun reaches ``elevDeg`` (morning or afternoon side) at
 * ``latDeg`` on ``date``; null if it never does (polar day/night). Noon is 12. */
export function solarHourForElevation(latDeg, date, elevDeg, afternoon) {
  const start = Date.UTC(date.getUTCFullYear(), 0, 0);
  const doy = (date.getTime() - start) / 86400e3;
  const decl = -23.44 * Math.cos((2 * Math.PI / 365) * (doy + 10)) * Math.PI / 180;
  const lat = latDeg * Math.PI / 180, el = elevDeg * Math.PI / 180;
  const cosH = (Math.sin(el) - Math.sin(lat) * Math.sin(decl)) / (Math.cos(lat) * Math.cos(decl));
  if (cosH < -1 || cosH > 1) return null;
  const h = Math.acos(cosH) * 12 / Math.PI; // hours from solar noon
  return afternoon ? 12 + h : 12 - h;
}

// sun presets as elevations, so "golden hour" is golden at any latitude / season
export const SUN_PRESETS = {
  morning: { elev: 18, pm: false },
  noon: { elev: null },
  golden: { elev: 6, pm: true },
  dusk: { elev: -3, pm: true },
};

/** Set the lighting sun for the camera's location: preset key, or "" / null = real time. */
export function sunTime(viewer, preset) {
  const clock = viewer.clock;
  if (!preset || !SUN_PRESETS[preset]) { clock.currentTime = C.JulianDate.now(); viewer.scene.requestRender(); return; }
  const c = viewer.camera.positionCartographic;
  const lon = C.Math.toDegrees(c.longitude), lat = C.Math.toDegrees(c.latitude);
  const now = new Date(), p = SUN_PRESETS[preset];
  let hour = p.elev == null ? 12 : solarHourForElevation(lat, now, p.elev, p.pm);
  if (hour == null) hour = 12; // sun never reaches that elevation today: use noon
  const d = new Date(now);
  d.setUTCHours(0, 0, 0, 0);
  d.setTime(d.getTime() + (hour - lon / 15) * 3600e3);
  clock.currentTime = C.JulianDate.fromDate(d);
  viewer.scene.requestRender();
}
