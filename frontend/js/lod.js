// Globe level-of-detail rules and the pure decluttering logic behind declutter.js: height bands,
// stable geographic aggregation grids, per-layer priorities, the on-screen marker budget and
// screen-space label placement. No Cesium and no DOM here (unit-tested under node:
// tests/frontend/test_lod.py), so every rule is one table or function in this file.

// ---- camera height bands (metres above the ellipsoid) ----
export const BANDS = [["world", 7e6], ["continent", 3e6], ["region", 1.5e6], ["area", 6e5], ["local", 0]];
export function bandOf(h) {
  for (const [name, min] of BANDS) if (h >= min) return name;
  return "local";
}
// on-screen budget for individually drawn markers (track glyphs, event glyphs, war-likely fires)
// per band, filled by priority; aggregate cells and cluster badges stand for everything else
export const BUDGET = { world: 450, continent: 900, region: 1600, area: 3500, local: Infinity };
// a layer with at least one candidate on screen keeps this share of the budget (so 3,000
// war-likely fires cannot starve the news events, and vice versa)
export const MIN_SHARE = 0.15;

// Per-layer rules. aggregateAbove: camera height above which the layer is drawn as aggregate
// cells / clusters plus only its notable items; below it every item is drawn. cellPx: target
// on-screen size of one aggregation cell.
export const LOD = {
  fires: { aggregateAbove: 1.5e6, cellPx: 22 },
  ships: { aggregateAbove: 8e5, cellPx: 22 },
  aircraft: { aggregateAbove: 2.5e6, cellPx: 48 },
  events: { aggregateAbove: 0, cellPx: 52 }, // screen clustering at every height (stacked city-centre reports)
  quakes: { aggregateAbove: 3e6, minMag: 4.5 }, // far out only M4.5+ (smaller ones are still listed)
  radiation: { normalBelow: 1.5e6 }, // stations reading normal only when zoomed in; anomalies always
};

// Infrastructure / reference point LOD: [minimum size, max camera distance in metres].
// Power plants by capacity (MW); ports by WPI harbour size; airports by class.
export const POWER_LOD = [[2000, Infinity], [1000, 9e6], [300, 3.5e6], [100, 1.8e6], [20, 8e5], [0, 3e5]];
export function powerMaxDist(mw) {
  for (const [min, d] of POWER_LOD) if ((mw || 0) >= min) return d;
  return 3e5;
}
export const PORT_LOD = { large: 1.2e7, medium: 4e6, small: 1.5e6, very_small: 6e5 };
export const portMaxDist = (size) => PORT_LOD[size] || 6e5;
export function airportMaxDist(a) {
  if (a.type === "large_airport") return 9e6;
  if (a.is_military) return 3.5e6;
  return 1.5e6;
}
// fixed infra billboards (no size attribute): refineries / LNG / dams / ground stations
export const INFRA_POINT_LOD = { refineries: 6e6, lng: 9e6, dams: 3e6, ground_stations: 9e6, landings: 2.5e6 };
// label budget per band (all label layers together, after overlap removal)
export const LABEL_CAP = { world: 12, continent: 30, region: 60, area: 120, local: 200 };
// top-N news event titles drawn as labels per band
export const EVENT_LABELS = { world: 3, continent: 8, region: 14, area: 20, local: 28 };

// ---- geometry ----
// metres per screen pixel at the view centre (perspective camera looking down)
export function metersPerPixel(h, fovY, canvasH) {
  return (2 * Math.max(1, h) * Math.tan((fovY || 1.0472) / 2)) / Math.max(1, canvasH || 900);
}
// grid step in degrees for ~px-pixel cells at height h, snapped to a power of two so the grid
// is identical for every camera position within a zoom step (no re-binning while panning)
export function gridStep(h, px, fovY, canvasH) {
  const deg = (px * metersPerPixel(h, fovY, canvasH)) / 111320;
  const k = Math.round(Math.log2(Math.max(1e-6, deg)));
  return 2 ** Math.max(-10, Math.min(4, k));
}
// roughly equal-area cell id: rows of `step` degrees, columns widened by 1/cos(latitude)
export function cellOf(lat, lon, step) {
  const row = Math.floor((Math.min(89.999, Math.max(-90, lat)) + 90) / step);
  const mid = -90 + (row + 0.5) * step;
  const lonStep = step / Math.max(0.15, Math.cos((mid * Math.PI) / 180));
  const col = Math.floor((((lon + 180) % 360) + 360) % 360 / lonStep);
  return row * 100000 + col;
}

// Aggregate items into grid cells. opts.weight(it) -> number (e.g. FRP), opts.prio(it) -> number
// (the highest-priority member becomes the cell's representative), opts.members keeps the ids.
// Returns [{ key, n, lat, lon (mean), w, s, e, nn (bounds), sum, max, top, topPrio, ids }].
export function aggregate(items, step, opts = {}) {
  const cells = new Map();
  const weight = opts.weight, prio = opts.prio;
  for (const it of items) {
    if (!it || it.lat == null || it.lon == null) continue;
    const key = cellOf(it.lat, it.lon, step);
    let c = cells.get(key);
    if (!c) {
      c = { key, n: 0, lat: 0, lon: 0, w: it.lon, s: it.lat, e: it.lon, nn: it.lat, sum: 0, max: -Infinity, top: null, topPrio: -Infinity, ids: opts.members ? [] : null };
      cells.set(key, c);
    }
    c.n++;
    c.lat += it.lat; c.lon += it.lon;
    if (it.lon < c.w) c.w = it.lon; if (it.lon > c.e) c.e = it.lon;
    if (it.lat < c.s) c.s = it.lat; if (it.lat > c.nn) c.nn = it.lat;
    if (weight) { const v = weight(it) || 0; c.sum += v; if (v > c.max) c.max = v; }
    if (prio) { const p = prio(it); if (p > c.topPrio) { c.topPrio = p; c.top = it; } }
    if (c.ids) c.ids.push(it.id);
  }
  const out = [];
  for (const c of cells.values()) { c.lat /= c.n; c.lon /= c.n; if (!weight) c.max = 0; out.push(c); }
  return out;
}

// Greedy merge of clusters whose centres are closer than minDeg (cells next to each other on a
// grid line): bigger / higher-priority clusters absorb smaller ones. For the few hundred
// clusters of the glyph layers only (O(k^2)).
export function mergeClose(cells, minDeg) {
  const sorted = cells.slice().sort((a, b) => b.topPrio - a.topPrio || b.n - a.n);
  const out = [];
  for (const c of sorted) {
    let host = null;
    for (const o of out) {
      const k = Math.cos(((o.lat + c.lat) / 2) * Math.PI / 180);
      const d = Math.hypot(o.lat - c.lat, (o.lon - c.lon) * k);
      if (d < minDeg) { host = o; break; }
    }
    if (!host) { out.push({ ...c, ids: c.ids ? c.ids.slice() : null }); continue; }
    const n = host.n + c.n;
    host.lat = (host.lat * host.n + c.lat * c.n) / n; host.lon = (host.lon * host.n + c.lon * c.n) / n;
    host.n = n; host.sum += c.sum; host.max = Math.max(host.max, c.max);
    host.w = Math.min(host.w, c.w); host.e = Math.max(host.e, c.e); host.s = Math.min(host.s, c.s); host.nn = Math.max(host.nn, c.nn);
    if (host.ids && c.ids) host.ids.push(...c.ids);
    if (c.topPrio > host.topPrio) { host.topPrio = c.topPrio; host.top = c.top; }
  }
  return out;
}

// ---- priorities (higher = more important; Infinity = always drawn, never budgeted) ----
// sensor-detected: one sensor saw it (no independent corroboration) - ranked with official
export const STATUS_RANK = { "sensor-corroborated": 4, official: 3, "sensor-detected": 3, "multi-source": 2, reported: 1 };
export const CAT_SEVERITY = { armed_conflict: 9, air_activity: 7, maritime_security: 6, cyber_outage: 5, disaster: 5, civil_unrest: 4, infrastructure: 3, political: 2, economic: 1 };
const clamp01 = (x) => Math.min(1, Math.max(0, +x || 0));

// news event: corroboration status first, then confidence, severity of the category and recency
// by EVENT time (_t0 = t_start in ms; not the time the system processed it)
export function eventPriority(it, now) {
  const st = STATUS_RANK[it.status] || 1, sev = CAT_SEVERITY[it.category] || 2;
  const ageH = it._t0 ? Math.max(0, (now - it._t0) / 36e5) : 96;
  return 10 + st * 10 + clamp01(it.confidence) * 10 + sev * 1.2 + Math.exp(-ageH / 24) * 8 - (it.precision === "country" ? 8 : 0);
}
// the most severe category among a cluster's members (badge colour)
export function topCategory(cats) {
  let best = null, bs = -1;
  for (const c of cats) { const s = CAT_SEVERITY[c] ?? 0; if (s > bs) { bs = s; best = c; } }
  return best;
}

// Theatres where a fast-moving vessel is worth showing at world view: [name, west, south, east, north]
export const HOT_ZONES = [
  ["Black Sea / Azov", 27, 40.5, 42, 47.5], ["Eastern Mediterranean", 28, 30, 37, 37.5], ["Red Sea / Gulf of Aden", 32, 11, 52, 30],
  ["Persian Gulf / Hormuz", 47, 23, 60, 31], ["Taiwan Strait", 117, 21, 123, 27], ["South China Sea", 105, 3, 121, 22],
  ["Baltic", 9, 53, 31, 66], ["Korean Peninsula", 123, 33, 132, 40.5],
];
export function inHotZone(lat, lon) {
  for (const z of HOT_ZONES) if (lon >= z[1] && lon <= z[3] && lat >= z[2] && lat <= z[4]) return z[0];
  return null;
}
// ship drawn individually when zoomed out: watchlist / sanctioned / loitering-dark alert
// (watched = true) always, military, fast movers inside a conflict theatre; 0 = aggregate only
export function shipPriority(it, watched) {
  if (watched) return Infinity;
  if (it.is_mil) return 55;
  const fast = (it.speed_kts || 0) >= 25 && (it.speed_kts || 0) < 60; // > 60 kts is bad data
  if (fast && inHotZone(it.lat, it.lon)) return 42;
  return 0;
}
export function aircraftPriority(it, watched) {
  if (watched || it.high_priority) return Infinity;
  return it.is_mil ? 50 : 22;
}
// fire drawn individually while zoomed out: at or above the war-likely score threshold
export function firePriority(it, thr) {
  const s = it.conflict_score;
  if (s == null || s < thr) return 0;
  return 30 + s * 0.35 + Math.min(6, Math.log10(1 + (it.frp || 0)) * 2);
}

// Fill the on-screen budget. cands: [{ layer, id, prio }] (on-screen candidates). Prio Infinity
// is always taken and does not consume budget. Every layer with candidates first gets up to
// minShare * budget of its own best, the rest goes to the best remaining overall.
// Returns Map(layer -> Set(id)).
export function allocate(cands, budget, minShare = MIN_SHARE) {
  const out = new Map(), by = new Map();
  const take = (c) => { let s = out.get(c.layer); if (!s) out.set(c.layer, (s = new Set())); s.add(c.id); };
  let rest = [];
  for (const c of cands) {
    if (c.prio === Infinity) { take(c); continue; }
    if (!(c.prio > 0)) continue;
    let l = by.get(c.layer); if (!l) by.set(c.layer, (l = []));
    l.push(c);
  }
  if (budget === Infinity) { for (const l of by.values()) for (const c of l) take(c); return out; }
  let left = Math.max(0, budget | 0);
  const share = Math.floor(budget * minShare);
  for (const l of by.values()) {
    l.sort((a, b) => b.prio - a.prio);
    const n = Math.min(l.length, share, left);
    for (let i = 0; i < n; i++) take(l[i]);
    left -= n;
    rest = rest.concat(l.slice(n));
  }
  if (left > 0 && rest.length) {
    rest.sort((a, b) => b.prio - a.prio);
    for (let i = 0; i < Math.min(left, rest.length); i++) take(rest[i]);
  }
  return out;
}

// Screen-space label placement without overlap. labels: [{ x, y, w, h, prio }] (x, y = top-left
// in pixels); fixed: boxes no label may cover (the glyphs they annotate). Greedy by priority
// over a coarse bucket grid; returns the accepted label indices.
export function placeLabels(labels, cap = Infinity, pad = 2, bucket = 64, fixed = []) {
  const order = labels.map((l, i) => i).sort((a, b) => labels[b].prio - labels[a].prio);
  const grid = new Map(), placed = [], boxes = [];
  const keys = (b) => {
    const out = [];
    for (let gx = Math.floor(b.x0 / bucket); gx <= Math.floor(b.x1 / bucket); gx++)
      for (let gy = Math.floor(b.y0 / bucket); gy <= Math.floor(b.y1 / bucket); gy++) out.push(gx + ":" + gy);
    return out;
  };
  for (const f of fixed) {
    const b = { x0: f.x, y0: f.y, x1: f.x + f.w, y1: f.y + f.h }, bi = boxes.push(b) - 1;
    for (const k of keys(b)) { let a = grid.get(k); if (!a) grid.set(k, (a = [])); a.push(bi); }
  }
  for (const i of order) {
    if (placed.length >= cap) break;
    const l = labels[i];
    const b = { x0: l.x - pad, y0: l.y - pad, x1: l.x + l.w + pad, y1: l.y + l.h + pad };
    const ks = keys(b);
    let hit = false;
    for (const k of ks) {
      for (const j of grid.get(k) || []) { const o = boxes[j]; if (b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0) { hit = true; break; } }
      if (hit) break;
    }
    if (hit) continue;
    const bi = boxes.push(b) - 1;
    for (const k of ks) { let a = grid.get(k); if (!a) grid.set(k, (a = [])); a.push(bi); }
    placed.push(i);
  }
  return placed;
}

// Positions (pixel offsets) of n markers fanned out around a shared point ("spiderfy"): one
// ring of up to 10, then wider rings.
export function spiderOffsets(n, r0 = 30, gap = 24) {
  const out = [];
  let ring = 0, i = 0;
  while (i < n) {
    const r = r0 + ring * gap, cap = Math.max(6, Math.floor((2 * Math.PI * r) / 26)), k = Math.min(cap, n - i);
    for (let j = 0; j < k; j++) { const a = -Math.PI / 2 + (2 * Math.PI * j) / k + ring * 0.3; out.push([Math.round(r * Math.cos(a)), Math.round(r * Math.sin(a))]); }
    i += k; ring++;
  }
  return out;
}
