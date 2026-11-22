// 3D level-of-detail for tracks: procedurally generated glTF aircraft/ships (frontend/models,
// built by scripts/gen_models.py) replace the 2D billboards near the camera; motion from
// velocity vectors (dead reckoning, pitch from climb, bank from turn rate); velocity leader
// lines; optional Cesium ion OSM Buildings + World Terrain.
//
// Standalone: nothing imports this yet. The controller (app.js) wires it, see WIRING below.
//
// Axis convention: models are glTF Y-up with the nose/bow on glTF +Z. Cesium (upAxis Y,
// forwardAxis Z) puts the nose on local +X; Transforms.headingPitchRollToFixedFrame with
// heading 0 points local +X east, so heading = toRadians(heading_deg) + HEADING_OFFSET
// makes compass 0 face north and 90 face east.
//
// WIRING (app.js):
//   const models = new Models3D(viewer, {
//     hideBillboard: (kind, id) => { const p = byKind[kind].map.get(id); if (p) p.show = false; },
//     showBillboard: (kind, id) => { const l = byKind[kind], p = l.map.get(id); if (p) p.show = l.filter(l.items.get(id)); },
//   });
//   // after every air/ships sync (REST or websocket), and after refilter():
//   models.update(air.visibleItems(), ships.visibleItems());
//   models.setEnabled(true);
// Layer.upsert() re-shows billboards whose key changed, so call update() after each sync;
// update() re-hides the billboards of everything currently drawn in 3D.

const C = Cesium;

export const HEADING_OFFSET = -Math.PI / 2;
export const MODEL_BASE = "/static/models/";
const KT = 0.514444; // knots -> m/s
const FT = 0.3048;
const G = 9.80665;
const R_E = 6371008.8;
const D2R = Math.PI / 180;
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const wrapPi = (a) => { while (a > Math.PI) a -= 2 * Math.PI; while (a < -Math.PI) a += 2 * Math.PI; return a; };

// ---- role classification (copied from symbology.js so this module stays standalone) ----
const FIGHTER = new Set(["F15", "F16", "F18H", "F18S", "F35", "F22", "F5", "F86", "EUFI", "HAWK", "A10", "B1", "B2", "B52", "SB39", "SA37", "TOR", "AJET", "M339", "M346", "RFAL", "MIR2", "SU", "SU27", "SU30", "SU34", "SU35", "MG29", "MG31", "J10", "J11", "J20", "JF17", "F14", "AV8B", "HARR"]);
const TANKER = new Set(["K35R", "K35E", "KC10", "KC46", "KC2", "KC39", "A332", "A33M", "MRTT", "IL78", "K135", "DC10"]);
const TRANSPORT = new Set(["C17", "C5", "C5M", "C130", "C30J", "C2", "C27J", "C295", "C160", "A400", "CN35", "Y20", "IL76", "AN124", "A124", "AN12", "AN22", "AN26", "AN72", "C212", "DHC6", "CASA", "B748", "B77W", "B77L", "B752", "B737", "A321", "A342", "E135"]);
const ISR = new Set(["E3CF", "E3TF", "E6", "E737", "E7", "R135", "RC35", "P8", "P3", "E2", "U2", "ATLA", "E8", "GLF5", "GLEX", "CL60", "BE20", "B350", "DH8B", "DH8C", "E390", "F900", "LJ45", "E550", "PC12", "D228", "F406"]);
const UAV = new Set(["Q9", "Q4", "Q1", "MQ9", "RQ4", "MQ4", "HRON", "TB2", "UAV", "JPRO"]);
const HELO = new Set(["H60", "H47", "H53", "H53S", "UH1", "UH1Y", "V22", "NH90", "EH10", "LYNX", "PUMA", "TIGR", "H64", "MI8", "MI17", "MI24", "MI28", "KA52", "A139", "A169", "A119", "AS65", "AS32", "B412", "B06", "B429", "EC35", "EC45", "EC55", "EC25", "EC30", "H135", "H145", "H160", "H175", "S92", "S76", "R44", "AS50"]);
const TRAINER = new Set(["T38", "T6", "TEX2", "PC21", "PC7", "PC9", "KT1", "CT4", "G120", "TB30", "G115", "T7", "M326", "L39", "SR20", "C172", "P28A", "GA6C"]);
// model refinements within a role
const MIL_LIFTER = new Set(["C17", "C5", "C5M", "C130", "C30J", "C2", "C27J", "C295", "C160", "A400", "CN35", "Y20", "IL76", "AN124", "A124", "AN12", "AN22", "AN26", "AN72", "C212", "CASA", "KC39"]);
const AWACS = new Set(["E3CF", "E3TF", "E737", "E7", "E2", "A50"]);
const AIRLINER_RE = /^(A3\d|A2\d|A3\d\d|A1[89]N|A20N|A21N|B7\d|B3\d|B38M|B39M|B78X|MD[89]|E1[79]\d|E2\d|E75|E90|CRJ|BCS|C919|SU95|DC|IL96|A30|P8|R135|RC35|E6|K35)/;

export function aircraftRole(it) {
  const t = String((it && it.type_code) || "").toUpperCase();
  const cat = String((it && it.category) || "").toLowerCase();
  if (UAV.has(t) || /uav|drone/.test(cat)) return "uav";
  if (HELO.has(t) || /helo|heli|rotor/.test(cat)) return "helo";
  if (TANKER.has(t) || /tanker/.test(cat) && !TRANSPORT.has(t)) return "tanker";
  if (FIGHTER.has(t) || t.startsWith("SU") || t.startsWith("MIG") || /fighter|attack|strike/.test(cat)) return "fighter";
  if (ISR.has(t) || /isr|surveil|recon|awacs|patrol/.test(cat)) return "isr";
  if (TRANSPORT.has(t) || /transport/.test(cat)) return "transport";
  if (TRAINER.has(t) || /trainer|aerobatic/.test(cat)) return "trainer";
  return "generic";
}

export function aircraftModel(it) {
  const t = String((it && it.type_code) || "").toUpperCase();
  const role = aircraftRole(it);
  if (role === "uav") return "aircraft_uav";
  if (role === "helo") return "aircraft_helo";
  if (role === "fighter") return "aircraft_fighter";
  if (role === "tanker") return it && it.is_mil || /^(K35|KC|IL78|MRTT|A33M|K135)/.test(t) ? "aircraft_tanker" : "aircraft_airliner";
  if (role === "isr") return AWACS.has(t) || /awacs|aew/i.test(String(it && it.category || "")) ? "aircraft_awacs" : AIRLINER_RE.test(t) ? "aircraft_airliner" : "aircraft_generic";
  if (role === "transport") return MIL_LIFTER.has(t) ? "aircraft_transport" : "aircraft_airliner";
  if (role === "generic" && AIRLINER_RE.test(t)) return "aircraft_airliner";
  return "aircraft_generic";
}

export function shipClass(it) {
  if (it && it.is_mil) return "mil";
  const raw = String((it && (it.type_code ?? it.category)) || "").toLowerCase();
  const n = parseInt(raw, 10);
  if (!isNaN(n) && String(n) === raw.trim()) {
    if (n === 35) return "mil";
    if (n === 30) return "fishing";
    if (n === 36 || n === 37) return "pleasure";
    if (n >= 50 && n <= 59 || n === 31 || n === 32 || n === 33) return "service";
    if (n >= 60 && n <= 69) return "passenger";
    if (n >= 70 && n <= 79) return "cargo";
    if (n >= 80 && n <= 89) return "tanker";
    return "other";
  }
  if (/milit|naval|warship|law/.test(raw)) return "mil";
  if (/tank/.test(raw)) return "tanker";
  if (/cargo|container|bulk|carrier/.test(raw)) return "cargo";
  if (/passenger|ferry|cruise/.test(raw)) return "passenger";
  if (/fish/.test(raw)) return "fishing";
  if (/tug|pilot|sar|dredg|service|tow/.test(raw)) return "service";
  if (/sail|pleasure|yacht/.test(raw)) return "pleasure";
  return "other";
}

export function shipModel(it) {
  const cls = shipClass(it);
  if (cls === "mil") {
    const name = String((it && (it.name || it.callsign)) || "");
    const len = +((it && (it.length_m || (it.extra && it.extra.length_m))) || 0);
    return /\bCVN?\b|CVN-?\d|carrier/i.test(name) || len >= 250 ? "ship_carrier" : "ship_destroyer";
  }
  if (cls === "cargo") return "ship_cargo";
  if (cls === "tanker") return "ship_tanker";
  if (cls === "passenger") return "ship_passenger";
  return "ship_small";
}

// ---- vector helpers (usable by the 2D layers too) ----
export function bearingDeg(lat1, lon1, lat2, lon2) {
  const p1 = lat1 * D2R, p2 = lat2 * D2R, dl = (lon2 - lon1) * D2R;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) / D2R + 360) % 360;
}
export function distanceM(lat1, lon1, lat2, lon2) {
  const p1 = lat1 * D2R, p2 = lat2 * D2R, dp = p2 - p1, dl = (lon2 - lon1) * D2R;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R_E * Math.asin(Math.min(1, Math.sqrt(a)));
}
/** Great-circle destination: returns [lat, lon] degrees. */
export function destination(lat, lon, brgDeg, distM) {
  const d = distM / R_E, b = brgDeg * D2R, p1 = lat * D2R, l1 = lon * D2R;
  const sp = Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b);
  const p2 = Math.asin(clamp(sp, -1, 1));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * sp);
  return [p2 / D2R, ((l2 / D2R + 540) % 360) - 180];
}
const altOf = (kind, it) => (kind === "ships" || it.on_ground ? 0 : Math.max(0, (+it.alt_ft || 0) * FT));

/** Per-track motion state from consecutive fixes: heading/pitch/bank, vertical rate,
 *  dead reckoning with a smooth blend toward each new fix. Keys are kind + ":" + id. */
export class Kinematics {
  constructor(opts = {}) {
    this.timeout = { aircraft: opts.aircraftTimeoutS ?? 90, ships: opts.shipTimeoutS ?? 600 };
    this.blendS = opts.blendS ?? 2;
    this.ema = opts.ema ?? 0.35;
    this.fadeS = opts.fadeS ?? 30;
    this.replay = false; // replay snapshots are historical fixes: draw them where they were, never dead-reckon
    this.map = new Map();
  }
  get(kind, id) { return this.map.get(kind + ":" + id); }
  /** Feed the latest snapshot for one kind; tracks absent from it are dropped. */
  ingest(kind, items, now = Date.now()) {
    const seen = new Set();
    for (const it of items) {
      if (!it || it.id == null || it.lat == null || it.lon == null) continue;
      const key = kind + ":" + it.id;
      seen.add(key);
      let st = this.map.get(key);
      const alt = altOf(kind, it);
      let t = Date.parse(it.ts);
      if (!(Math.abs(now - t) < 120e3)) t = now; // missing or skewed clock -> receive time
      if (!st) {
        st = { kind, lat: it.lat, lon: it.lon, alt, t, recv: now, hdg: it.heading_deg ?? null, rawHdg: it.heading_deg, v: (+it.speed_kts || 0) * KT, vs: 0, pitch: 0, roll: 0, off: null };
        this.map.set(key, st);
        continue;
      }
      if (st.lat === it.lat && st.lon === it.lon && st.alt === alt && it.heading_deg === st.rawHdg) { st.v = (+it.speed_kts || 0) * KT; continue; }
      const prev = this.predict(st, now); // where we are drawing it right now
      const dt = (t - st.t) / 1000;
      let hdg = it.heading_deg;
      if (hdg == null) hdg = distanceM(st.lat, st.lon, it.lat, it.lon) > 5 ? bearingDeg(st.lat, st.lon, it.lat, it.lon) : st.hdg;
      const v = (+it.speed_kts || 0) * KT;
      if (dt > 0.5) {
        const air = kind !== "ships" && !it.on_ground;
        st.vs = air ? clamp((alt - st.alt) / dt, -150, 150) : 0;
        const w = st.hdg != null && hdg != null ? wrapPi((hdg - st.hdg) * D2R) / dt : 0;
        const pitch = air && v > 1 ? clamp(Math.atan2(st.vs, v), -20 * D2R, 20 * D2R) : 0;
        const roll = air ? clamp(Math.atan(v * w / G), -35 * D2R, 35 * D2R) : 0;
        st.pitch += this.ema * (pitch - st.pitch);
        st.roll += this.ema * (roll - st.roll);
      }
      Object.assign(st, { lat: it.lat, lon: it.lon, alt, t: Math.max(t, st.t), recv: now, hdg, rawHdg: it.heading_deg, v });
      // blend: keep the drawn position continuous, decay the offset over blendS
      const dr = this.predict(st, now, true);
      let dlon = prev.lon - dr.lon; if (dlon > 180) dlon -= 360; if (dlon < -180) dlon += 360;
      st.off = Math.abs(prev.lat - dr.lat) + Math.abs(dlon) < 0.5 ? { lat: prev.lat - dr.lat, lon: dlon, alt: prev.altM - dr.altM, at: now } : null;
    }
    for (const [k, st] of this.map) if (st.kind === kind && !seen.has(k)) this.map.delete(k);
  }
  /** Drawn state at time now: {lat, lon, altM, heading, pitch, roll, alpha, live}. */
  predict(st, now = Date.now(), raw = false) {
    if (typeof st === "string") st = this.map.get(st);
    if (!st) return null;
    if (this.replay) return { lat: st.lat, lon: st.lon, altM: st.alt, heading: st.hdg ?? 0, pitch: st.pitch, roll: st.roll, alpha: 1, live: false };
    const lim = this.timeout[st.kind] ?? 90;
    const age = Math.max(0, (now - st.t) / 1000);
    const dt = Math.min(age, lim);
    const hdg = st.hdg ?? 0;
    let lat = st.lat, lon = st.lon, altM = st.alt;
    if (st.v > 0.3 && dt > 0) [lat, lon] = destination(st.lat, st.lon, hdg, st.v * dt);
    if (st.vs) altM = Math.max(0, st.alt + st.vs * Math.min(dt, 60));
    let blending = false;
    if (!raw && st.off) {
      const u = (now - st.off.at) / (this.blendS * 1000);
      if (u < 1) {
        const w = 1 - u * u * (3 - 2 * u);
        lat += st.off.lat * w; lon += st.off.lon * w; altM += st.off.alt * w; blending = true;
      } else st.off = null;
    }
    const alpha = age > lim ? Math.max(0.3, 1 - (age - lim) / this.fadeS) : 1;
    return { lat, lon, altM, heading: hdg, pitch: st.pitch, roll: st.roll, alpha, live: (age < lim && st.v > 0.3) || blending || (age > lim && alpha > 0.3) };
  }
}

/** Position of an item after `seconds` along its velocity vector (2D-layer helper). */
export function predictPosition(it, seconds, kind = "aircraft", vsMps = 0) {
  const v = (+it.speed_kts || 0) * KT;
  const [lat, lon] = v > 0 && it.heading_deg != null ? destination(it.lat, it.lon, it.heading_deg, v * seconds) : [it.lat, it.lon];
  return { lat, lon, altM: Math.max(0, altOf(kind, it) + vsMps * seconds) };
}

// ---- Models3D ----
const scratchCarto = new C.Cartographic();
const scratchPos = new C.Cartesian3();
const scratchHpr = new C.HeadingPitchRoll();
const HP_SIL = C.Color.fromCssColorString("#ff4b55");

function eyeHeight(scene) {
  const c = scene.camera.positionCartographic;
  return c ? c.height : Infinity;
}

function padRect(r, f) {
  if (!r) return r;
  const span = r.east >= r.west ? r.east - r.west : r.east - r.west + C.Math.TWO_PI; // antimeridian
  const w = span * f, h = (r.north - r.south) * f;
  const south = Math.max(-C.Math.PI_OVER_TWO, r.south - h), north = Math.min(C.Math.PI_OVER_TWO, r.north + h);
  if (span + 2 * w >= C.Math.TWO_PI) return new C.Rectangle(-C.Math.PI, south, C.Math.PI, north);
  return new C.Rectangle(C.Math.negativePiToPi(r.west - w), south, C.Math.negativePiToPi(r.east + w), north);
}

export class Models3D {
  /**
   * @param viewer Cesium.Viewer
   * @param opts.hideBillboard (kind, id) => void   hide the 2D glyph of a track drawn in 3D
   * @param opts.showBillboard (kind, id) => void   restore it (respecting the layer filter)
   * @param opts.getItems () => ({aircraft, ships})  optional pull source used on camera moves
   * @param opts.maxModels  nearest N tracks in view drawn in 3D (default 250)
   * @param opts.maxAltitudeM  eye altitude below which 3D engages (default 300 km)
   * @param opts.kinematics  shared Kinematics (else one is created; exposed as .kinematics)
   */
  constructor(viewer, opts = {}) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.opts = opts;
    this.hide = opts.hideBillboard || (() => {});
    this.show = opts.showBillboard || (() => {});
    this.getItems = opts.getItems || null;
    this.maxModels = opts.maxModels ?? 250;
    this.maxAlt = opts.maxAltitudeM ?? 300e3;
    this.base = opts.baseUrl || MODEL_BASE;
    this.kinematics = opts.kinematics || new Kinematics(opts);
    this.col = this.scene.primitives.add(new C.PrimitiveCollection({ destroyPrimitives: true }));
    this.entries = new Map(); // key -> {kind, id, name, model, ready, hp, alpha}
    this.items = { aircraft: [], ships: [] };
    this.enabled = false;
    this._raf = 0;
    this._last = 0;
    this._onCam = () => this.refresh();
    this.scene.camera.changed.addEventListener(this._onCam);
    this.scene.camera.moveEnd.addEventListener(this._onCam);
  }

  setEnabled(on) {
    this.enabled = !!on;
    this.refresh();
  }

  /** Feed the current (filtered) items; cheap when disabled (kinematics only). */
  update(aircraftItems, shipItems) {
    const now = Date.now();
    if (aircraftItems) { this.items.aircraft = aircraftItems; this.kinematics.ingest("aircraft", aircraftItems, now); }
    if (shipItems) { this.items.ships = shipItems; this.kinematics.ingest("ships", shipItems, now); }
    this.refresh();
  }

  active() { return this.enabled && eyeHeight(this.scene) < this.maxAlt; }

  /** One timestamp per rendered frame (calls within 8 ms share it), so the follow camera
   * (evaluated in the clock tick) and the model matrices (preRender) use the same instant. */
  frameNow() {
    const t = performance.now();
    if (!(t - this._fnAt < 8)) { this._fnAt = t; this._fn = Date.now(); }
    return this._fn;
  }

  /** Smoothed (dead-reckoned + blended) position of a track as drawn this frame. */
  positionOf(kind, id, result) {
    const s = this.kinematics.predict(this.kinematics.get(kind, id), this.frameNow());
    return s ? C.Cartesian3.fromDegrees(s.lon, s.lat, s.altM, undefined, result) : null;
  }

  /** While following a track: place models on every rendered frame (not the throttled
   * 30 fps loop), so the model and the camera move in lockstep without jitter. */
  /** Keep one track's model regardless of the view footprint (follow target); null clears. */
  setPinned(kind, id) { this.pinned = kind && id != null ? [kind, id] : null; this.refresh(); }

  setFrameSync(on) {
    if (on && !this._unPre) {
      this._unPre = this.scene.preRender.addEventListener(() => {
        if (this.active() && this.entries.size) this._place(this.frameNow());
      });
    } else if (!on && this._unPre) {
      this._unPre();
      this._unPre = null;
      this._kick();
    }
  }

  /** Re-select the 3D set (nearest N in view) and diff models. */
  refresh() {
    if (this.getItems) { const g = this.getItems(); if (g) { this.items.aircraft = g.aircraft || []; this.items.ships = g.ships || []; } }
    const want = new Map();
    if (this.active()) {
      const cam = this.scene.camera;
      // ground footprint of the view, padded: aircraft at altitude seen obliquely project
      // outside it, and a model must not pop in/out at the frame edge
      const rect = padRect(cam.computeViewRectangle(this.scene.globe.ellipsoid), 0.25);
      const eye = cam.positionWC;
      const cand = [];
      for (const kind of ["aircraft", "ships"]) {
        for (const it of this.items[kind]) {
          if (!it || it.lat == null || it.lon == null) continue;
          if (rect && !C.Rectangle.contains(rect, C.Cartographic.fromDegrees(it.lon, it.lat, 0, scratchCarto))) continue;
          const p = C.Cartesian3.fromDegrees(it.lon, it.lat, altOf(kind, it), undefined, scratchPos);
          cand.push([C.Cartesian3.distanceSquared(p, eye), kind, it]);
        }
      }
      cand.sort((a, b) => a[0] - b[0]);
      for (let i = 0; i < cand.length && i < this.maxModels; i++) want.set(cand[i][1] + ":" + cand[i][2].id, cand[i]);
      // the followed track always gets its model, wherever the view footprint falls
      if (this.pinned) {
        const [pk, pid] = this.pinned, key = pk + ":" + pid;
        const it = !want.has(key) && this.items[pk] && this.items[pk].find((x) => x && x.id === pid);
        if (it) want.set(key, [0, pk, it]);
      }
    }
    let changed = false;
    for (const [key, e] of this.entries) {
      if (want.has(key)) continue;
      this._remove(key, e);
      changed = true;
    }
    for (const [key, [, kind, it]] of want) {
      const name = kind === "aircraft" ? aircraftModel(it) : shipModel(it);
      let e = this.entries.get(key);
      if (e && e.name !== name) { this._remove(key, e); e = null; }
      if (!e) { this._create(key, kind, it, name); changed = true; continue; }
      e.it = it;
      if (e.ready) this.hide(kind, it.id); // layers may have re-shown it during sync
      const hp = !!(kind === "aircraft" && it.high_priority);
      if (e.model && hp !== e.hp) { e.hp = hp; e.model.silhouetteSize = hp ? 1.5 : 0; changed = true; }
    }
    this._place(Date.now());
    if (changed || this.entries.size) this.scene.requestRender();
    this._kick();
  }

  _create(key, kind, it, name) {
    const hp = !!(kind === "aircraft" && it.high_priority);
    const e = { kind, id: it.id, it, name, model: null, ready: false, hp, alpha: 1, dead: false };
    this.entries.set(key, e);
    const st = this.kinematics.get(kind, it.id);
    const s = st ? this.kinematics.predict(st) : { lat: it.lat, lon: it.lon, altM: altOf(kind, it), heading: it.heading_deg || 0, pitch: 0, roll: 0 };
    C.Model.fromGltfAsync({
      url: this.base + name + ".glb",
      modelMatrix: this._matrix(s, new C.Matrix4()),
      upAxis: C.Axis.Y, forwardAxis: C.Axis.Z,
      minimumPixelSize: 28, maximumScale: 600,
      silhouetteColor: HP_SIL, silhouetteSize: hp ? 1.5 : 0,
      shadows: C.ShadowMode.DISABLED,
      colorBlendMode: C.ColorBlendMode.MIX, colorBlendAmount: 0,
      id: { kind, id: it.id, models3d: true }, // pickable like the billboard (pick.id.kind / .id)
    }).then((model) => {
      if (e.dead || this.col.isDestroyed()) { model.destroy(); return; }
      e.model = model;
      this.col.add(model);
      model.readyEvent.addEventListener(() => {
        if (e.dead) return;
        e.ready = true;
        this.hide(kind, e.id);
        this.scene.requestRender();
      });
      this.scene.requestRender();
    }).catch((err) => { console.warn("models3d: load failed", name, err); });
  }

  _remove(key, e) {
    e.dead = true;
    if (e.model) this.col.remove(e.model); // destroyPrimitives
    this.entries.delete(key);
    this.show(e.kind, e.id);
  }

  _matrix(s, result) {
    C.Cartesian3.fromDegrees(s.lon, s.lat, s.altM, undefined, scratchPos);
    scratchHpr.heading = (s.heading || 0) * D2R + HEADING_OFFSET;
    scratchHpr.pitch = s.pitch || 0;
    scratchHpr.roll = s.roll || 0;
    return C.Transforms.headingPitchRollToFixedFrame(scratchPos, scratchHpr, C.Ellipsoid.WGS84, undefined, result);
  }

  /** Write every model's matrix (in place); returns true while anything is still moving. */
  _place(now) {
    let moving = false;
    for (const e of this.entries.values()) {
      if (!e.model) continue;
      const s = this.kinematics.predict(this.kinematics.get(e.kind, e.id), now);
      if (!s) continue;
      this._matrix(s, e.model.modelMatrix); // Model diffs modelMatrix each frame: no reload
      if (Math.abs(s.alpha - e.alpha) > 0.01) { e.alpha = s.alpha; e.model.color = C.Color.WHITE.withAlpha(s.alpha); }
      if (s.live) moving = true;
    }
    return moving;
  }

  /** Replay mode: no dead reckoning from arrival time (models would drift away from the snapshot). */
  setReplay(on) {
    if (this.kinematics.replay === !!on) return;
    this.kinematics.replay = !!on;
    this._place(Date.now());
    this.scene.requestRender();
    this._kick();
  }
  // throttled (<= 30 fps) animation loop; runs only while 3D is active and something moves
  _kick() {
    if (this._raf || !this.active() || !this.entries.size) return;
    const step = (ts) => {
      this._raf = 0;
      if (!this.active() || !this.entries.size || this._unPre) return; // frame sync owns placement
      if (ts - this._last >= 33) {
        this._last = ts;
        const moving = this._place(Date.now());
        this.scene.requestRender();
        if (!moving) return; // stop; next update()/camera move restarts it
      }
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this.scene.camera.changed.removeEventListener(this._onCam);
    this.scene.camera.moveEnd.removeEventListener(this._onCam);
    for (const [k, e] of [...this.entries]) this._remove(k, e);
    this.scene.primitives.remove(this.col);
  }
}

// ---- velocity leader lines ----
const LEADER = {
  hp: C.Color.fromCssColorString("#ff4b55").withAlpha(0.6),
  mil: C.Color.fromCssColorString("#ffa040").withAlpha(0.55),
  civ: C.Color.fromCssColorString("#5fd8ff").withAlpha(0.45),
  ship: C.Color.fromCssColorString("#3ddcc2").withAlpha(0.5),
  shipMil: C.Color.fromCssColorString("#ff4b55").withAlpha(0.55),
};

export class VectorLeaders {
  /** @param opts.kinematics shared Kinematics (adds climb from vertical rate); opts.maxTracks (1500) */
  constructor(viewer, opts = {}) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.kin = opts.kinematics || null;
    this.maxTracks = opts.maxTracks ?? 1500;
    this.minutes = 5;
    this.lines = this.scene.primitives.add(new C.PolylineCollection());
    this.ticks = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this.items = { aircraft: [], ships: [] };
    this.byKey = new Map(); // key -> {line, ticks[], sig}
    this._onCam = () => this._rebuild();
    this.scene.camera.moveEnd.addEventListener(this._onCam);
  }
  setHorizonMinutes(m) { this.minutes = [1, 5, 10].includes(+m) ? +m : 5; for (const r of this.byKey.values()) r.sig = ""; this._rebuild(); }
  setVisible(b) { this.lines.show = this.ticks.show = !!b; this.scene.requestRender(); }
  /** items for one kind ("aircraft" | "ships"); diffed by id within that kind. */
  update(items, kind = "aircraft") { this.items[kind] = items || []; this._rebuild(); }

  _color(kind, it) {
    if (kind === "ships") return it.is_mil ? LEADER.shipMil : LEADER.ship;
    return it.high_priority ? LEADER.hp : it.is_mil ? LEADER.mil : LEADER.civ;
  }

  _rebuild() {
    if (!this.lines.show) return;
    const rect = this.scene.camera.computeViewRectangle(this.scene.globe.ellipsoid);
    const center = rect ? C.Rectangle.center(rect) : null;
    const cand = [];
    for (const kind of ["aircraft", "ships"]) {
      for (const it of this.items[kind]) {
        if (!it || it.lat == null || !(+it.speed_kts > 0.5) || it.heading_deg == null) continue;
        const c = C.Cartographic.fromDegrees(it.lon, it.lat, 0, scratchCarto);
        if (rect && !C.Rectangle.contains(rect, c)) continue;
        const d = center ? (c.longitude - center.longitude) ** 2 + (c.latitude - center.latitude) ** 2 : 0;
        cand.push([d, kind, it]);
      }
    }
    if (cand.length > this.maxTracks) { cand.sort((a, b) => a[0] - b[0]); cand.length = this.maxTracks; }
    const keep = new Set();
    for (const [, kind, it] of cand) {
      const key = kind + ":" + it.id;
      keep.add(key);
      const st = this.kin && this.kin.get(kind, it.id);
      const vs = st ? st.vs : 0;
      const sig = it.lat + "|" + it.lon + "|" + it.alt_ft + "|" + it.heading_deg + "|" + it.speed_kts + "|" + (it.high_priority ? 1 : 0) + (it.is_mil ? 1 : 0) + "|" + vs.toFixed(1);
      let r = this.byKey.get(key);
      if (r && r.sig === sig) continue;
      const pts = [];
      for (let m = 0; m <= this.minutes; m++) {
        const p = predictPosition(it, m * 60, kind, vs);
        pts.push(C.Cartesian3.fromDegrees(p.lon, p.lat, p.altM));
      }
      const color = this._color(kind, it);
      if (!r) {
        r = { line: this.lines.add({ positions: pts, width: 1.5, material: C.Material.fromType("Color", { color }) }), ticks: [], sig };
        this.byKey.set(key, r);
      } else {
        r.line.positions = pts;
        r.line.material.uniforms.color = color;
      }
      r.sig = sig;
      while (r.ticks.length > this.minutes) this.ticks.remove(r.ticks.pop());
      for (let m = 1; m <= this.minutes; m++) {
        let t = r.ticks[m - 1];
        if (!t) { t = this.ticks.add({ pixelSize: 3 }); r.ticks.push(t); }
        t.position = pts[m];
        t.color = color;
      }
    }
    for (const [key, r] of this.byKey) {
      if (keep.has(key)) continue;
      this.lines.remove(r.line);
      for (const t of r.ticks) this.ticks.remove(t);
      this.byKey.delete(key);
    }
    this.scene.requestRender();
  }

  destroy() {
    this.scene.camera.moveEnd.removeEventListener(this._onCam);
    this.scene.primitives.remove(this.lines);
    this.scene.primitives.remove(this.ticks);
  }
}

// ---- Cesium ion extras: OSM Buildings + World Terrain ----
/** No-op (returns null) without a token. Returns a handle for toggleBuildings(). */
export async function enableIonExtras(viewer, token, opts = {}) {
  if (!token) return null;
  const scene = viewer.scene;
  C.Ion.defaultAccessToken = token;
  const handle = { scene, tileset: null, terrain: false, on: opts.buildings !== false, _onCam: null };
  try {
    if (!viewer._ionTerrain) { // globe.js applyConfig may already have set it
      viewer._ionTerrain = true;
      scene.setTerrain(C.Terrain.fromWorldTerrain());
    }
    handle.terrain = true;
  } catch (err) { console.warn("models3d: world terrain unavailable", err); }
  try {
    const ts = await C.createOsmBuildingsAsync({ maximumScreenSpaceError: 16 });
    // height-tinted: low-rise slate, mid-rise steel, towers in the accent cyan (reads well
    // in the oblique 2.5D view)
    const h = "Number(${feature['cesium#estimatedHeight']})"; // NaN (no height) compares false
    ts.style = new C.Cesium3DTileStyle({
      color: { conditions: [
        [`${h} >= 150`, "color('#5ee3ea')"],
        [`${h} >= 60`, "color('#8fb8c9')"],
        [`${h} >= 20`, "color('#a3adb8')"],
        ["true", "color('#7d8792')"],
      ] },
    });
    scene.primitives.add(ts);
    handle.tileset = ts;
    // buildings are only legible close in; skip traversal entirely from high up
    const maxH = opts.buildingsMaxAltitudeM ?? 60e3;
    handle._onCam = () => { const v = handle.on && eyeHeight(scene) < maxH; if (ts.show !== v) { ts.show = v; scene.requestRender(); } };
    scene.camera.changed.addEventListener(handle._onCam);
    handle._onCam();
  } catch (err) { console.warn("models3d: OSM buildings unavailable", err); }
  scene.requestRender();
  return handle;
}

export function toggleBuildings(handle, on) {
  if (!handle || !handle.tileset) return;
  handle.on = !!on;
  handle._onCam(); // applies on/off together with the altitude gate and requests a frame
}
