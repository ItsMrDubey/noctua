// Weather layers.
//
// Satellite clouds (IR): one seamless global layer from five geostationary imagers, each drawn
// only inside its own longitude sector (GOES-West | GOES-East | Meteosat MTG 0 deg | Meteosat
// IODC 45.5E | Himawari; boundaries at the midpoints, cut at +/-70 deg latitude with a soft
// fade). /api/weather/ir resolves each source's newest available frame (GIBS slot probing,
// EUMETSAT capabilities). Tiles are decoded to cloud-top temperature and re-coloured with one
// palette (wxpixels.js): warm ground transparent, cloud fading in with height, cold convective
// tops cyan -> violet -> magenta. A new frame loads invisibly and cross-fades in (no flicker).
//
// Precipitation radar: RainViewer latest frame (zoom <= 7, 512 px tiles to stay well under its
// 100 requests/min limit), re-coloured from dBZ; areas without radar coverage are hatched.
//
// 3D clouds (experimental, off by default): cumulus billboards placed from current low / mid /
// high model cloud cover (Open-Meteo via /api/weather/clouds), sun-lit, fading towards the
// horizon. Positions are illustrative, not observed; Cesium's billboard clouds only look like
// clouds at a few km scale, so the layer draws below 250 km eye altitude.
import { fetchJson } from "/static/js/api.js";
import { fmtTime, utcTitle } from "/static/js/timefmt.js";
import { esc } from "/static/js/ui.js";
import { ageClass, cloudFade, cloudLight, cloudSpecs, gibsTable, greyTable, irPalette, mercRowLats, processIR, processMask, processRadar, radarDecoder, sunElevation } from "/static/js/wxpixels.js";

const C = Cesium;
const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/";
const EUMETSAT_WMS = "https://view.eumetsat.int/geoserver/ows";
const RAINVIEWER = "https://api.rainviewer.com/public/weather-maps.json";
const CLOUD_MAX_EYE = 250e3;
const IR_POLL_MS = 5 * 60e3, RADAR_POLL_MS = 5 * 60e3;
const FADE_MS = 700;

let _gibs = null;
const DECODERS = { gibs: () => (_gibs = _gibs || gibsTable()), mtg: () => greyTable("mtg"), msg: () => greyTable("msg") };
const PALETTES = {};
const palette = (style) => (PALETTES[style] = PALETTES[style] || irPalette(style));

function canvas2d(w, h) {
  if (typeof OffscreenCanvas !== "undefined") {
    const cv = new OffscreenCanvas(w, h);
    return [cv, cv.getContext("2d", { willReadFrequently: true })];
  }
  const cv = document.createElement("canvas");
  cv.width = w; cv.height = h;
  return [cv, cv.getContext("2d", { willReadFrequently: true })];
}

/** Run every tile of ``provider`` through ``fn(pixels, w, h, rowLats, level)`` before Cesium sees it,
 * retrying transient upstream failures (GIBS / EUMETSAT answer the odd tile with a 5xx). */
function processTiles(provider, fn, stats) {
  const orig = provider.requestImage.bind(provider);
  const attempt = (x, y, level, request, n) => {
    const p = orig(x, y, level, request);
    if (!p) return p; // throttled: Cesium asks again later
    return Promise.resolve(p).catch((err) => {
      if (n >= 2) { stats.failed++; throw err; }
      stats.retried++;
      return new Promise((res) => setTimeout(res, 1200 * (n + 1))).then(() => attempt(x, y, level, undefined, n + 1));
    });
  };
  provider.requestImage = (x, y, level, request) => {
    const p = attempt(x, y, level, request, 0);
    if (!p) return p;
    return p.then((img) => {
      stats.loaded++;
      const w = img.width, h = img.height;
      // Cesium hands over ImageBitmaps already flipped (row 0 = south); keep that orientation
      const bitmap = typeof ImageBitmap !== "undefined" && img instanceof ImageBitmap;
      const r = provider.tilingScheme.tileXYToRectangle(x, y, level);
      const [cv, ctx] = canvas2d(w, h);
      ctx.drawImage(img, 0, 0);
      const data = ctx.getImageData(0, 0, w, h);
      fn(data.data, w, h, mercRowLats(r.south, r.north, h, bitmap), level);
      if (bitmap) return createImageBitmap(data, { premultiplyAlpha: "none", colorSpaceConversion: "none" });
      ctx.putImageData(data, 0, 0);
      if (typeof OffscreenCanvas !== "undefined" && cv instanceof OffscreenCanvas) {
        const c2 = document.createElement("canvas");
        c2.width = w; c2.height = h; c2.getContext("2d").drawImage(cv, 0, 0);
        return c2;
      }
      return cv;
    });
  };
  // failures are counted and shown in the layer status line instead of a console line per tile
  provider.errorEvent.addEventListener(() => {});
  return provider;
}

/** Resolve once the globe has no tiles left to load (or after ``timeout`` ms). */
function globeSettled(scene, timeout = 30000) {
  return new Promise((resolve) => {
    const t0 = performance.now();
    const tick = () => {
      scene.requestRender();
      const dt = performance.now() - t0;
      if ((scene.globe.tilesLoaded && dt > 600) || dt > timeout) resolve();
      else setTimeout(tick, 250);
    };
    setTimeout(tick, 250);
  });
}

function crossFade(scene, inc, out, target) {
  return new Promise((resolve) => {
    const t0 = performance.now();
    const step = () => {
      const k = Math.min(1, (performance.now() - t0) / FADE_MS);
      for (const l of inc) l.alpha = Math.max(0.001, target * k);
      for (const l of out) l.alpha = target * (1 - k);
      scene.requestRender();
      if (k < 1) requestAnimationFrame(step); else resolve();
    };
    requestAnimationFrame(step);
  });
}

export class Weather {
  constructor(viewer) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.irOn = false; this.radarOn = false; this.cloudsOn = false;
    this.irStyle = "clouds";
    this.irLayers = new Map(); // source id -> { key, layers: [ImageryLayer], src }
    this.irData = null; this.irError = null;
    this.irStats = { loaded: 0, failed: 0, retried: 0 };
    this.radar = null; this.radarMask = null; this.radarFrame = null; this.radarError = null;
    this.radarStats = { loaded: 0, failed: 0, retried: 0 };
    this._fading = [];
    this.clouds = this.scene.primitives.add(new C.CloudCollection({ noiseDetail: 16.0, show: false }));
    this._timers = {};
    this._reqId = 0;
    this._lastKey = "";
    this._irSeq = 0; this._radarSeq = 0;
    this.status = { clouds: 0, step: null, error: null };
    this.onStatus = () => {};
    this.onLayerStatus = () => {};
    this.scene.camera.moveEnd.addEventListener(() => { if (this.cloudsOn) { this._fadeClouds(); this._loadClouds(); } });
    this.scene.camera.changed.addEventListener(() => this._fadeClouds());
    // weather sits above base imagery / VIIRS / night lights, below the place-label overlay
    // (deferred: the overlay tags its layer right after adding it)
    viewer.imageryLayers.layerAdded.addEventListener((l) => { if (!l._wx && this._all().length) setTimeout(() => this._restack(), 0); });
  }
  _all() {
    const out = [];
    if (this.radarMask) out.push(this.radarMask);
    for (const e of this.irLayers.values()) out.push(...e.layers);
    out.push(...this._fading);
    if (this.radar) out.push(this.radar);
    return out;
  }
  _restack() {
    const L = this.viewer.imageryLayers;
    for (const l of this._all()) if (L.contains(l)) L.raiseToTop(l);
    for (let i = L.length - 1; i >= 0; i--) { const l = L.get(i); if (l && l._overlay === "labels") { L.raiseToTop(l); break; } }
  }
  _add(provider, rect) {
    const l = new C.ImageryLayer(provider, rect ? { rectangle: rect } : {});
    l._wx = true;
    this.viewer.imageryLayers.add(l);
    this._restack();
    return l;
  }
  _layerStatus() { this.onLayerStatus(this); }

  // ---- infrared cloud imagery ----
  setIR(on) {
    this.irOn = !!on;
    clearInterval(this._timers.ir);
    this._irSeq++;
    if (!this.irOn) {
      for (const e of this.irLayers.values()) for (const l of e.layers) this.viewer.imageryLayers.remove(l, true);
      this.irLayers.clear();
      this.scene.requestRender();
      this._layerStatus();
      return;
    }
    this._refreshIR();
    this._timers.ir = setInterval(() => this._refreshIR(), IR_POLL_MS);
  }
  setIRStyle(style) {
    if (style === this.irStyle) return;
    this.irStyle = style;
    if (this.irOn) this._refreshIR(true);
  }
  _irProviders(src) {
    const credit = src.kind === "gibs" ? "NASA EOSDIS GIBS" : "EUMETSAT";
    const limit = src.lat_limit || 70;
    const decode = DECODERS[src.decode] ? DECODERS[src.decode]() : DECODERS.msg();
    const lut = palette(this.irStyle);
    const fn = (px, w, h, rows) => processIR(px, w, h, decode, lut, rows, limit);
    return src.sector.map(([w, e]) => {
      const rect = C.Rectangle.fromDegrees(w, -limit, e, limit);
      let p;
      if (src.kind === "gibs") {
        p = new C.UrlTemplateImageryProvider({
          url: GIBS + src.layer + "/default/" + src.time + "/GoogleMapsCompatible_Level6/{z}/{y}/{x}.png",
          maximumLevel: Math.min(6, src.max_level), rectangle: rect, credit,
        });
      } else {
        // 512 px tiles: a quarter of the requests (the View Service renders each GetMap on demand)
        p = new C.WebMapServiceImageryProvider({
          url: EUMETSAT_WMS, layers: src.layer, rectangle: rect, credit,
          parameters: { format: "image/png", transparent: true, styles: "", time: src.time },
          tilingScheme: new C.WebMercatorTilingScheme(), tileWidth: 512, tileHeight: 512,
          maximumLevel: Math.max(3, src.max_level - 1), enablePickFeatures: false,
        });
      }
      return { provider: processTiles(p, fn, this.irStats), rect };
    });
  }
  async _refreshIR(force = false) {
    const seq = this._irSeq;
    let data;
    try {
      data = await fetchJson("/api/weather/ir", 30000);
      this.irError = null;
    } catch (e) {
      this.irError = "frame lookup failed: " + (e.message || e);
      this._layerStatus();
      return;
    }
    if (seq !== this._irSeq || !this.irOn) return;
    this.irData = data;
    const incoming = [], outgoing = [];
    const first = this.irLayers.size === 0;
    for (const src of data.sources || []) {
      if (!src.time) continue;
      const key = src.layer + "@" + src.time + "#" + this.irStyle;
      const cur = this.irLayers.get(src.id);
      if (cur && cur.key === key && !force) continue;
      const layers = this._irProviders(src).map(({ provider, rect }) => {
        const l = this._add(provider, rect);
        l.alpha = first ? 1 : 0.001;
        return l;
      });
      if (cur) outgoing.push(...cur.layers);
      this.irLayers.set(src.id, { key, layers, src });
      incoming.push(...layers);
    }
    this._layerStatus();
    this.scene.requestRender();
    if (first || !incoming.length) return;
    // the new frame loads invisibly above the old one, then cross-fades in
    this._fading.push(...outgoing);
    await globeSettled(this.scene);
    await crossFade(this.scene, incoming, outgoing, 1);
    for (const l of outgoing) this.viewer.imageryLayers.remove(l, true);
    this._fading = this._fading.filter((l) => !outgoing.includes(l));
    this.scene.requestRender();
  }

  // ---- precipitation radar ----
  async setRadar(on) {
    this.radarOn = !!on;
    clearInterval(this._timers.radar);
    this._radarSeq++;
    if (!this.radarOn) {
      for (const l of [this.radar, this.radarMask]) if (l) this.viewer.imageryLayers.remove(l, true);
      this.radar = null; this.radarMask = null; this.radarFrame = null;
      this.scene.requestRender();
      this._layerStatus();
      return;
    }
    await this._addRadar();
    this._timers.radar = setInterval(() => this._addRadar(), RADAR_POLL_MS);
  }
  async _addRadar() {
    const seq = this._radarSeq;
    let idx;
    try {
      idx = await fetchJson(RAINVIEWER, 10000);
      this.radarError = null;
    } catch (e) {
      this.radarError = "radar index unavailable: " + (e.message || e);
      this._layerStatus();
      return;
    }
    if (seq !== this._radarSeq || !this.radarOn) return;
    const past = (idx.radar && idx.radar.past) || [];
    const last = past[past.length - 1];
    if (!last) { this.radarError = "no radar frames published"; this._layerStatus(); return; }
    const host = idx.host || "https://tilecache.rainviewer.com";
    if (!this.radarMask) {
      // coverage mask (opaque where no radar network reports): drawn as a faint hatch
      const mp = new C.UrlTemplateImageryProvider({
        url: host + "/v2/coverage/0/512/{z}/{x}/{y}/0/0_0.png", tileWidth: 512, tileHeight: 512,
        maximumLevel: 5, credit: "RainViewer",
      });
      this.radarMask = this._add(processTiles(mp, (px, w, h, rows, level) => processMask(px, w, h, level >= 3), this.radarStats));
    }
    if (this.radarFrame && this.radarFrame.path === last.path) return;
    const decode = radarDecoder(), colours = new Map();
    const p = new C.UrlTemplateImageryProvider({
      // 512 px tiles, colour scheme 2 (Universal Blue: the only one left), smoothed, snow
      url: host + last.path + "/512/{z}/{x}/{y}/2/1_1.png", tileWidth: 512, tileHeight: 512,
      maximumLevel: 7, credit: "Weather data by RainViewer",
    });
    const old = this.radar;
    const layer = this._add(processTiles(p, (px) => processRadar(px, decode, colours), this.radarStats));
    layer.alpha = old ? 0.001 : 1;
    this.radar = layer;
    this.radarFrame = { path: last.path, time: new Date(last.time * 1000), frames: past.length };
    this._layerStatus();
    if (old) {
      this._fading.push(old);
      await globeSettled(this.scene);
      await crossFade(this.scene, [layer], [old], 1);
      this.viewer.imageryLayers.remove(old, true);
      this._fading = this._fading.filter((l) => l !== old);
    }
    this.scene.requestRender();
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
    } else this._status(0, null, null);
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
    const c = cam.positionCartographic, clat = d(c.latitude), clon = d(c.longitude), cap = 3;
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
    const now = new Date();
    this._cloudSpecs = cloudSpecs(items, step);
    for (const c of this._cloudSpecs) {
      const light = cloudLight(sunElevation(c.lat, c.lon, now));
      // night clouds: dim blue-grey, not glowing white blobs
      const tint = C.Color.fromBytes(Math.round(150 + 105 * light), Math.round(160 + 95 * light), Math.round(178 + 77 * light), 255);
      coll.add({
        position: C.Cartesian3.fromDegrees(c.lon, c.lat, c.alt),
        scale: new C.Cartesian2(c.w, c.h * 1.6),
        maximumSize: new C.Cartesian3(c.w, c.h * 1.6, c.h * 1.4),
        slice: c.slice, brightness: 0.35 + 0.65 * light, color: tint,
      });
    }
    this._fadeClouds(true);
    this.scene.requestRender();
  }
  // oblique views: far clouds fade out instead of stacking into a white band at the horizon
  _fadeClouds(force) {
    const cam = this.scene.camera, coll = this.clouds;
    if (!this.cloudsOn || !coll.length) return;
    const pos = cam.positionCartographic, eye = pos.height;
    const key = Math.round(C.Math.toDegrees(pos.longitude) * 50) + "," + Math.round(C.Math.toDegrees(pos.latitude) * 50) + "," + Math.round(eye / 2000);
    if (!force && key === this._fadeKey) return;
    this._fadeKey = key;
    const ground = C.Cartesian3.fromRadians(pos.longitude, pos.latitude, 0);
    for (let i = 0; i < coll.length; i++) {
      const cl = coll.get(i);
      const a = cloudFade(C.Cartesian3.distance(ground, cl.position), eye);
      cl.show = a > 0.02;
      if (cl.show && Math.abs(cl.color.alpha - a) > 0.04) cl.color = C.Color.clone(cl.color).withAlpha(a);
    }
  }
  _status(n, step, error) {
    this.status = { clouds: n, step, error: error || null };
    this.onStatus(this.status);
  }
}

// ---- status lines (layer tree "row-note" under each weather toggle) ----
const lonTxt = (v) => Math.abs(v).toFixed(v % 1 ? 1 : 0) + "°" + (v < 0 ? "W" : v > 0 ? "E" : "");
const IR_LEGEND = '<i class="wx-scale" title="Cloud-top temperature: warm ground clear, cloud whiter as it gets colder; cyan → violet → magenta = cold convective tops (-45 to -80 °C)"></i>';

/** HTML for the IR status line: legend + one entry per satellite with its frame time. */
export function irNote(w) {
  if (!w.irOn) return "";
  if (w.irError && !w.irData) return '<span class="wx-err">' + esc(w.irError) + "</span>";
  if (!w.irData) return "resolving latest frames…";
  const now = Date.now();
  const parts = (w.irData.sources || []).map((s) => {
    const age = s.time ? Math.round((now - Date.parse(s.time)) / 60e3) : null;
    const cls = s.time ? ageClass(age, s.step_min) : "bad";
    const sec = s.sector.map(([a, b]) => lonTxt(a) + "–" + lonTxt(b)).join(", ");
    const tip = s.name + " · " + s.band + (s.fallback ? " (fallback: MTG unavailable)" : "") +
      (s.time ? " · frame " + utcTitle(s.time) + " (" + age + " min ago)" : "") + " · sector " + sec +
      (s.error ? " · " + s.error : "");
    return '<b class="wx-src ' + cls + '" title="' + esc(tip) + '">' + esc(s.label) + " " + (s.time ? fmtTime(s.time) : "–") + "</b>";
  });
  const st = w.irStats, fail = st.failed ? ' · <span class="wx-err" title="tiles that still failed after 2 retries">' + st.failed + " tiles failed</span>" : "";
  return IR_LEGEND + parts.join(" ") + fail + (w.irError ? ' <span class="wx-err" title="' + esc(w.irError) + '">(stale)</span>' : "");
}

/** HTML for the radar status line: frame time + coverage hint. */
export function radarNote(w) {
  if (!w.radarOn) return "";
  if (w.radarError && !w.radarFrame) return '<span class="wx-err">' + esc(w.radarError) + "</span>";
  if (!w.radarFrame) return "loading radar index…";
  const f = w.radarFrame, age = Math.round((Date.now() - f.time) / 60e3);
  const st = w.radarStats, fail = st.failed ? ' · <span class="wx-err">' + st.failed + " tiles failed</span>" : "";
  return '<b class="wx-src ' + ageClass(age, 10) + '" title="RainViewer composite, frame ' + esc(utcTitle(f.time)) + " (" + age + ' min ago)">RainViewer ' + fmtTime(f.time) + "</b>" +
    ' <i class="wx-hatch"></i>national radar networks only · hatched = no radar' + fail;
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
