// Weather tile pixel processing (pure: no Cesium / DOM, unit-tested under node).
//
// Infrared: every source is decoded to cloud-top brightness temperature (deg C) and re-coloured
// with ONE palette, so the five geostationary sectors read as one seamless layer:
//   - NASA GIBS band 13 (GOES, Himawari) ships colour-mapped PNGs (GIBS colormap
//     "Clean_Longwave_Infrared_Window_Band": rainbow below -19 C, grey ramp above); the map is
//     inverted exactly.
//   - EUMETSAT View Service ships grey PNGs: MTG-I FCI IR10.5 white -80 C .. black +30 C
//     (its legend), MSG SEVIRI IR10.8 ~+30 .. -67 C (calibrated against MTG on overlapping disks).
// Warm ground and sea become transparent; cloud fades in as it gets colder; cold convective
// tops go cyan -> violet -> magenta. Rows fade out towards the latitude cut (geostationary
// imagery is unusable near the poles).
//
// Radar: RainViewer's only remaining colour scheme (Universal Blue) is decoded back to dBZ
// (rain / snow) and re-coloured with a palette that keeps light echoes subtle on a dark globe.

// ---- brightness temperature index: i in 0..255 <-> T = T0 + i * TSTEP ----
export const T0 = -100, TSTEP = 0.625; // -100 .. +59.4 C
export const tIndex = (t) => Math.max(0, Math.min(255, Math.round((t - T0) / TSTEP)));
export const tOf = (i) => T0 + i * TSTEP;

// GIBS colour entries below -19 C (rgb hex, upper bin edge C); bins are 1 C (0.5 C from -31).
// Above -19.1 C the colormap is a grey ramp: 197 at -18.85 C down to 1 at +57 C.
const GIBS_COLD = "ffffff-91.1 7f007f-90.1 8c0d87-89.1 99198e-88.1 a52696-87.1 b2339d-86.1 bf40a5-85.1 cc4cad-84.1 d959b4-83.1 e566bc-82.1 f272c3-81.1 ff7fcb-80.1 " +
  "e6e6e6-79.1 cccccc-78.1 b1b1b1-77.1 9b9b9b-76.1 818181-75.1 666666-74.1 4c4c4c-73.1 363636-72.1 1b1b1b-71.1 050505-70.1 " +
  "1a0000-69.1 330000-68.1 4d0000-67.1 660000-66.1 800000-65.1 990000-64.1 b30000-63.1 cc0000-62.1 e60000-61.1 ff0000-60.1 " +
  "ff1a00-59.1 ff3300-58.1 ff4d00-57.1 ff6600-56.1 ff8000-55.1 ff9900-54.1 ffb300-53.1 ffcc00-52.1 ffe600-51.1 ffff00-50.1 " +
  "e6ff00-49.1 ccff00-48.1 b3ff00-47.1 99ff00-46.1 80ff00-45.1 66ff00-44.1 4dff00-43.1 33ff00-42.1 1aff00-41.1 00ff00-40.1 " +
  "00ea0a-39.1 00d413-38.1 00bf1d-37.1 00aa26-36.1 009530-35.1 00803a-34.1 006a43-33.1 00554d-32.1 004056-31.1 002a60-30.6 " +
  "001569-30.1 000073-29.6 00007d-29.1 000d7a-28.6 001a81-28.1 002688-27.6 00338f-27.1 004096-26.6 004c9d-26.1 0059a4-25.6 " +
  "0066ab-25.1 0073b2-24.6 0080b9-24.1 008cc0-23.6 0099c7-23.1 00a6ce-22.6 00b2d5-22.1 00bfdc-21.6 00cce3-21.1 00d9ea-20.6 " +
  "00e6f1-20.1 00f2f8-19.6 00ffff-19.1";

/** packed rgb (r<<16|g<<8|b) -> T index, for GIBS band-13 tiles. Greys resolve to the warm ramp
 * (cold greys -80..-70 C share values with it; warm pixels are far more common). */
export function gibsTable() {
  const m = new Map();
  for (const tok of GIBS_COLD.split(" ")) {
    const hex = tok.slice(0, 6), hi = parseFloat(tok.slice(6));
    m.set(parseInt(hex, 16), tIndex(hi - 0.5));
  }
  for (let g = 1; g <= 197; g++) m.set(g * 65793, tIndex(-18.85 + (197 - g) * (75.85 / 196)));
  return m;
}

/** grey level (0..255, white = cold) -> T index for EUMETSAT grey products. */
export function greyTable(kind) {
  const out = new Uint8Array(256);
  for (let g = 0; g < 256; g++) {
    const t = kind === "mtg" ? 30 - g * (110 / 255) : 30 - g * 0.38;
    out[g] = tIndex(t);
  }
  return out;
}

// ---- IR palette ----
const lerp = (a, b, t) => a + (b - a) * t;
const smooth = (t) => t * t * (3 - 2 * t);
const hex = (h) => [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];

// cold-top enhancement stops (T C, colour, alpha); shared by both styles
const COLD = [
  [-38, "eef5fa", 0.74], [-46, "9fe2ff", 0.8], [-53, "3fb8ff", 0.85], [-59, "4f73ff", 0.88],
  [-65, "a24dff", 0.9], [-71, "ff4fd8", 0.92], [-80, "ffd9f3", 0.95],
];
export const IR_STYLES = {
  // every cloud: warm ground transparent, cloud fades in with height, cold tops enhanced
  clouds: { warm: 6, mid: -22, midA: 0.45, coldA: 0.68, base: "d3e3f1" },
  // convective / storm tops only: nothing warmer than about -32 C
  storms: { warm: -30, mid: -36, midA: 0.55, coldA: 0.8, base: "eef5fa" },
};

/** RGBA (0..255) for cloud-top temperature ``t`` (C) in palette ``style``. */
export function irColour(t, style = "clouds") {
  const s = IR_STYLES[style] || IR_STYLES.clouds;
  if (t >= s.warm) return [0, 0, 0, 0];
  if (t > s.mid) { // fade in: barely-there low cloud -> clear mid cloud
    const k = smooth((s.warm - t) / (s.warm - s.mid));
    const c = hex(s.base);
    return [c[0], c[1], c[2], Math.round(255 * s.midA * k)];
  }
  if (t > COLD[0][0]) { // mid -> cold anvil: brighten towards white
    const k = (s.mid - t) / (s.mid - COLD[0][0]);
    const a = hex(s.base), b = hex(COLD[0][1]);
    return [Math.round(lerp(a[0], b[0], k)), Math.round(lerp(a[1], b[1], k)), Math.round(lerp(a[2], b[2], k)),
      Math.round(255 * lerp(s.midA, s.coldA, k))];
  }
  for (let i = 1; i < COLD.length; i++) {
    const [t1, c1, a1] = COLD[i - 1], [t2, c2, a2] = COLD[i];
    if (t > t2 || i === COLD.length - 1) {
      const k = Math.max(0, Math.min(1, (t1 - t) / (t1 - t2)));
      const a = hex(c1), b = hex(c2);
      const al = Math.max(s.coldA, lerp(a1, a2, k));
      return [Math.round(lerp(a[0], b[0], k)), Math.round(lerp(a[1], b[1], k)), Math.round(lerp(a[2], b[2], k)), Math.round(255 * al)];
    }
  }
  return [255, 255, 255, 255];
}

/** 256-entry RGBA lookup over T index. */
export function irPalette(style = "clouds") {
  const lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) lut.set(irColour(tOf(i), style), i * 4);
  return lut;
}

// ---- geometry: latitude of each pixel row of a web-mercator tile ----
/** Latitudes (deg) of the ``h`` pixel-row centres of a web-mercator tile spanning
 * [south, north] (radians). ``bottomUp``: row 0 is the southern edge (flipped bitmaps). */
export function mercRowLats(southRad, northRad, h, bottomUp = false) {
  const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + lat / 2));
  const yN = merc(northRad), yS = merc(southRad), out = new Float32Array(h);
  for (let j = 0; j < h; j++) {
    const f = (j + 0.5) / h, y = bottomUp ? yS + (yN - yS) * f : yN - (yN - yS) * f;
    out[j] = (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
  }
  return out;
}

/** 0..1 opacity factor at latitude ``lat``: 1 inside, fading to 0 at +/-``limit``. */
export function latFade(lat, limit = 70, width = 8) {
  const d = limit - Math.abs(lat);
  return d <= 0 ? 0 : d >= width ? 1 : smooth(d / width);
}

/** Re-colour an RGBA tile in place. ``decode``: Map (packed rgb -> T index, GIBS) or Uint8Array
 * (grey -> T index). Pixels that do not decode (off-disk, no data) become transparent. */
export function processIR(px, w, h, decode, lut, rowLats, limit = 70) {
  const isMap = decode instanceof Map;
  for (let j = 0; j < h; j++) {
    const fade = rowLats ? latFade(rowLats[j], limit) : 1;
    let o = j * w * 4;
    for (let i = 0; i < w; i++, o += 4) {
      if (px[o + 3] < 128 || fade === 0) { px[o + 3] = 0; continue; }
      let ti;
      if (isMap) {
        ti = decode.get((px[o] << 16) | (px[o + 1] << 8) | px[o + 2]);
        if (ti === undefined) { px[o + 3] = 0; continue; }
      } else ti = decode[px[o]];
      const q = ti * 4;
      px[o] = lut[q]; px[o + 1] = lut[q + 1]; px[o + 2] = lut[q + 2];
      px[o + 3] = lut[q + 3] * fade;
    }
  }
  return px;
}

// ---- radar ----
// Universal Blue anchors (dBZ, RRGGBBAA); linear between anchors reproduces RainViewer's table
const UB_RAIN = "-10:63615914 5:92887164 10:cec08796 14:ded097be 15:88ddeeff 20:00a3e0ff 25:0077aaff 30:005588ff 34:004768ff " +
  "35:ffee00ff 40:ffaa00ff 44:ff8100ff 45:ff4400ff 50:c10000ff 54:5d0000ff 55:ffaaffff 60:ff77ffff 64:ff4effff 65:ffffffff 74:ffffffff";
const UB_SNOW = "-9:ceffff0c 0:c7ffff7f 10:bfffffff 20:7fbfffff 30:4f8fffff 40:2f6fffff 55:003fffff 75:0000ffff";
const parseStops = (s) => s.split(" ").map((t) => { const [d, c] = t.split(":"); return [+d, [0, 2, 4, 6].map((k) => parseInt(c.slice(k, k + 2), 16))]; });

/** Reference colours [{dbz, snow, rgba}] at 1 dBZ steps (what RainViewer draws). */
export function ubTable() {
  const out = [];
  for (const [snow, stops] of [[false, parseStops(UB_RAIN)], [true, parseStops(UB_SNOW)]]) {
    for (let i = 1; i < stops.length; i++) {
      const [d1, c1] = stops[i - 1], [d2, c2] = stops[i];
      // a step change (e.g. 14 -> 15 dBZ) has adjacent anchors: no interpolation across it
      for (let d = d1; d < d2 || (i === stops.length - 1 && d <= d2); d++) {
        const k = d2 === d1 ? 0 : (d - d1) / (d2 - d1);
        const step = d2 - d1 === 1;
        out.push({ dbz: d, snow, rgba: c1.map((v, j) => Math.round(step ? v : lerp(v, c2[j], k))) });
      }
    }
  }
  return out;
}

const RAIN = [ // dBZ, colour, alpha
  [8, "2bb3a6", 0], [12, "2bb3a6", 0.42], [18, "2fd39a", 0.62], [25, "52c95a", 0.74], [32, "d8e04a", 0.82],
  [38, "ffc23d", 0.88], [44, "ff7a2e", 0.9], [50, "ff3b3b", 0.93], [56, "e0157a", 0.95], [62, "ff4fd8", 0.96], [68, "ffffff", 0.97],
];
const SNOW = [[4, "9db4ff", 0], [8, "9db4ff", 0.42], [18, "b5c4ff", 0.62], [30, "d6dcff", 0.8], [45, "f1f0ff", 0.9]]; // periwinkle: not mistaken for cloud

/** Display RGBA for ``dbz`` (rain or snow). Drizzle / clutter below ~8 dBZ is not drawn. */
export function radarColour(dbz, snow = false) {
  const st = snow ? SNOW : RAIN;
  if (dbz <= st[0][0]) return [0, 0, 0, 0];
  for (let i = 1; i < st.length; i++) {
    const [d1, c1, a1] = st[i - 1], [d2, c2, a2] = st[i];
    if (dbz <= d2 || i === st.length - 1) {
      const k = Math.max(0, Math.min(1, (dbz - d1) / (d2 - d1)));
      const a = hex(c1), b = hex(c2);
      return [Math.round(lerp(a[0], b[0], k)), Math.round(lerp(a[1], b[1], k)), Math.round(lerp(a[2], b[2], k)), Math.round(255 * lerp(a1, a2, k))];
    }
  }
  return [255, 255, 255, 255];
}

/** Decoder: RainViewer RGBA -> {dbz, snow} by nearest reference colour (memoised; smoothed tiles
 * contain blends of neighbouring colours). */
export function radarDecoder() {
  const ref = ubTable(), memo = new Map();
  return (r, g, b, a) => {
    const key = ((r << 24) | (g << 16) | (b << 8) | a) >>> 0;
    let hit = memo.get(key);
    if (hit) return hit;
    let best = null, bd = Infinity;
    for (const e of ref) {
      const c = e.rgba, d = (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2 + 0.5 * (c[3] - a) ** 2;
      if (d < bd) { bd = d; best = e; }
    }
    hit = { dbz: best.dbz, snow: best.snow };
    if (memo.size < 200000) memo.set(key, hit);
    return hit;
  };
}

/** Re-colour a RainViewer tile in place. */
export function processRadar(px, decode, colourMemo = new Map()) {
  for (let o = 0; o < px.length; o += 4) {
    const a = px[o + 3];
    if (a === 0) continue;
    const d = decode(px[o], px[o + 1], px[o + 2], a);
    const k = (d.snow ? 1000 : 0) + d.dbz;
    let c = colourMemo.get(k);
    if (!c) { c = radarColour(d.dbz, d.snow); colourMemo.set(k, c); }
    px[o] = c[0]; px[o + 1] = c[1]; px[o + 2] = c[2]; px[o + 3] = c[3];
  }
  return px;
}

/** RainViewer coverage mask (opaque = no radar) -> faint diagonal hatch (``hatch``) or, zoomed
 * out where a hatch would turn into moire, a uniform faint veil; covered areas stay clear. */
export function processMask(px, w, h, hatch = true, period = 7) {
  for (let j = 0; j < h; j++) {
    for (let i = 0; i < w; i++) {
      const o = (j * w + i) * 4;
      if (px[o + 3] < 128) { px[o + 3] = 0; continue; }
      const line = hatch && (i + j) % period === 0;
      px[o] = line ? 150 : 8; px[o + 1] = line ? 170 : 12; px[o + 2] = line ? 190 : 18;
      px[o + 3] = line ? 40 : hatch ? 24 : 40;
    }
  }
  return px;
}

/** "09:40" style labels are formatted by the caller; this is the per-source age class. */
export function ageClass(ageMin, stepMin = 10) {
  if (ageMin == null) return "bad";
  return ageMin <= 3 * stepMin + 30 ? "ok" : ageMin <= 120 ? "warn" : "bad";
}

// ---- 3D clouds: layout from model cloud cover, sun-lit brightness ----
// Per grid node and level: cumulus billboards scattered over the node's cell, count from the
// cover, deterministic jitter so a refresh does not reshuffle them. Cesium's cloud billboards
// are made for cumulus a few km across seen from close by; scaled up to deck size they turn
// into speckled balls, so sizes stay <= 9 km and the layer only draws close in.
export const CLOUD_LEVELS = [
  // key, base altitude m, width as a fraction of the cell, height/width, clouds per cell at 100 %
  { key: "low", alt: 1500, fw: 0.3, fh: 0.4, n: 22, slice: 0.38 },
  { key: "mid", alt: 4600, fw: 0.36, fh: 0.3, n: 12, slice: 0.45 },
  { key: "high", alt: 9800, fw: 0.5, fh: 0.12, n: 5, slice: 0.62 },
];
const jitter = (seed) => { const x = Math.sin(seed * 12.9898) * 43758.5453; return x - Math.floor(x); };

/** Cloud billboards [{lon, lat, alt, w, h, slice, level}] for grid ``items`` at ``step`` deg. */
export function cloudSpecs(items, step, max = 1200) {
  const out = [];
  const cell = step * 111e3;
  for (const L of CLOUD_LEVELS) {
    for (const it of items) {
      const cover = +it[L.key] || 0;
      if (cover < 15) continue;
      const count = Math.max(1, Math.round((cover / 100) * L.n));
      for (let i = 0; i < count; i++) {
        if (out.length >= max) return out;
        const seed = it.lat * 1e3 + it.lon * 7.13 + L.alt + i * 17.7;
        const k = 0.75 + jitter(seed + 2) * 0.5;
        const w = Math.min(9e3, cell * L.fw * k * (0.6 + 0.4 * cover / 100));
        out.push({
          lat: it.lat + (jitter(seed) - 0.5) * step, lon: it.lon + (jitter(seed + 1) - 0.5) * step,
          alt: L.alt + (jitter(seed + 4) - 0.5) * 500, w, h: Math.max(400, Math.min(L.key === "high" ? 1200 : 3500, w * L.fh)),
          slice: L.slice + (jitter(seed + 5) - 0.5) * 0.12, level: L.key, cover,
        });
      }
    }
  }
  return out;
}

/** Solar elevation (deg) at lat/lon (deg) for a Date: enough for lighting (+/- 1 deg). */
export function sunElevation(lat, lon, date) {
  const d = date.getTime() / 86400e3 - 10957.5; // days since J2000
  const g = (357.529 + 0.98560028 * d) * Math.PI / 180;
  const q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * Math.PI / 180;
  const e = (23.439 - 0.00000036 * d) * Math.PI / 180;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L));
  const dec = Math.asin(Math.sin(e) * Math.sin(L));
  const gmst = (18.697374558 + 24.06570982441908 * d) % 24;
  const ha = ((gmst * 15 + lon) * Math.PI / 180) - ra;
  const la = lat * Math.PI / 180;
  return Math.asin(Math.sin(la) * Math.sin(dec) + Math.cos(la) * Math.cos(dec) * Math.cos(ha)) * 180 / Math.PI;
}

/** Cloud brightness for a solar elevation: full by day, dimming through twilight, dark at night. */
export function cloudLight(elev) {
  if (elev >= 8) return 1;
  if (elev <= -8) return 0.16;
  return 0.16 + 0.84 * smooth((elev + 8) / 16);
}

/** 0..1 opacity of a cloud ``dist`` metres from the camera's ground point at eye height ``eye``:
 * near clouds solid, the oblique horizon fades out instead of piling up. */
export function cloudFade(dist, eye) {
  const r1 = Math.max(60e3, eye * 1.6), r2 = Math.max(120e3, eye * 3.2);
  return dist <= r1 ? 1 : dist >= r2 ? 0 : 1 - smooth((dist - r1) / (r2 - r1));
}
