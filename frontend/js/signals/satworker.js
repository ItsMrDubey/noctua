// Satellite propagation worker (classic script, not bundled): SGP4 for every loaded element set
// off the main thread. satellite.js (MIT) loads from the vendored copy if present, else the pinned
// CDN build (scripts/fetch_satellitejs.sh vendors it).
//   in : {type:"load", items, fields}            -> {type:"loaded", n, failed}
//        {type:"tick", t}                        -> {type:"pos", t, ecef: Float64Array(n*3) m (NaN = no solution)}
//        {type:"track", idx, t, minutes, steps}  -> {type:"track", idx, ecef (orbit ring, m), geo: Float64Array(steps*3)
//                                                   ground track lat, lon, alt km, info}
/* global satellite, importScripts */
const LIBS = ["/static/vendor/satellite/satellite.min.js", "https://cdn.jsdelivr.net/npm/satellite.js@6.0.1/dist/satellite.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/satellite.js/6.0.1/satellite.min.js"];
let libErr = null;
for (const u of LIBS) {
  try { importScripts(u); libErr = null; break; } catch (e) { libErr = String((e && e.message) || e); }
}

const OMM = ["NORAD_CAT_ID", "OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
  "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"];
let recs = [];

function pv(rec, j) {
  try {
    const r = satellite.sgp4(rec, (j - rec.jdsatepoch) * 1440);
    const p = r && r.position;
    return p && typeof p === "object" && isFinite(p.x) ? r : null;
  } catch (e) { return null; }
}

function info(rec, d) {
  const j = satellite.jday(d), gm = satellite.gstime(d), r = pv(rec, j);
  if (!r) return null;
  const g = satellite.eciToGeodetic(r.position, gm), v = r.velocity;
  return { lat: satellite.degreesLat(g.latitude), lon: satellite.degreesLong(g.longitude), alt: g.height,
    speed: Math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z), period: (2 * Math.PI) / rec.no };
}

self.onmessage = (ev) => {
  const m = ev.data;
  if (typeof satellite === "undefined") { self.postMessage({ type: "error", error: "satellite.js unavailable: " + libErr }); return; }
  if (m.type === "load") {
    let failed = 0;
    recs = m.items.map((row) => {
      const o = {};
      OMM.forEach((k, i) => { o[k] = row[i]; });
      try {
        const r = satellite.json2satrec(o);
        if (r.error) { failed++; return null; }
        return r;
      } catch (e) { failed++; return null; }
    });
    self.postMessage({ type: "loaded", n: recs.length, failed });
  } else if (m.type === "tick") {
    const d = new Date(m.t), j = satellite.jday(d), gm = satellite.gstime(d);
    const c = Math.cos(gm), s = Math.sin(gm);
    const out = new Float64Array(recs.length * 3);
    for (let i = 0; i < recs.length; i++) {
      const rec = recs[i], r = rec && pv(rec, j);
      if (!r) { out[i * 3] = out[i * 3 + 1] = out[i * 3 + 2] = NaN; continue; }
      const p = r.position; // ECI (TEME) km -> ECEF m (rotation by GMST, as satellite.eciToEcf)
      out[i * 3] = (p.x * c + p.y * s) * 1000;
      out[i * 3 + 1] = (p.y * c - p.x * s) * 1000;
      out[i * 3 + 2] = p.z * 1000;
    }
    self.postMessage({ type: "pos", t: m.t, ecef: out }, [out.buffer]);
  } else if (m.type === "track") {
    const rec = recs[m.idx];
    if (!rec) { self.postMessage({ type: "track", idx: m.idx, ecef: null }); return; }
    const steps = m.steps || 180, ecef = new Float64Array(steps * 3), geo = new Float64Array(steps * 3);
    // orbit ring: inertial positions over one period rotated by the CURRENT sidereal angle (a closed
    // ellipse through the satellite); ground track: each sample rotated by its own time
    const t0 = m.t - (m.minutes * 60e3) / 2, gm0 = satellite.gstime(new Date(m.t));
    for (let k = 0; k < steps; k++) {
      const d = new Date(t0 + (k / (steps - 1)) * m.minutes * 60e3), j = satellite.jday(d), gm = satellite.gstime(d), r = pv(rec, j);
      if (!r) { ecef[k * 3] = geo[k * 3] = NaN; continue; }
      const e = satellite.eciToEcf(r.position, gm0), g = satellite.eciToGeodetic(r.position, gm);
      ecef[k * 3] = e.x * 1000; ecef[k * 3 + 1] = e.y * 1000; ecef[k * 3 + 2] = e.z * 1000;
      geo[k * 3] = satellite.degreesLat(g.latitude); geo[k * 3 + 1] = satellite.degreesLong(g.longitude); geo[k * 3 + 2] = g.height;
    }
    self.postMessage({ type: "track", idx: m.idx, ecef, geo, info: info(rec, new Date(m.t)) }, [ecef.buffer, geo.buffer]);
  }
};
