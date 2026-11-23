// Satellite imagery viewer: every free scene over a site (GET /api/imagery/scenes), rendered
// on demand at native resolution (GET /api/imagery/render), compared and draped on the globe.
//
// Opened from the Intel console ("View images" on the Overhead imaging passes / Satellite
// imagery tiles), the event dossier's imagery section, the before/after frame ("Open in
// viewer") and the globe's right-click menu ("Imagery here"): openImageryViewer({lat, lon,
// label, t, select: {a, b}}). One floating panel; reopening re-targets it.
//
// Layout: header (site, window size, period) · honest source note · time strip (one row per
// sensor, ticks = scenes with cloud as opacity; archive sources as dated chips; predicted
// passes from the Overhead section greyed, future) · stage (A, or A vs B as swipe / side by
// side / blink) with band presets, zoom (wheel) / pan (drag), scale bar and m/px, per-image
// label (sensor, date, native + rendered m/px, cloud scene / site) and attribution always on
// screen · scene list (thumbnails, rendered lazily, newest first, filter by source / cloud).
// Every image is a real scene; renders say how they were stretched. Thumbnails first, the
// full-resolution window replaces them when it arrives.
//
// Esri Wayback releases are shown as Esri's own tiles (browser-side, like the basemap) and
// are not downloadable; everything else downloads as PNG at the rendered resolution.
// Drape: the selected image becomes a Cesium imagery layer clipped to its bounds, just above
// the basemap (and on Google Photorealistic 3D tiles when those are showing), opacity slider.
import { esc, nf } from "/static/js/ui.js";
import { fetchJson, fetchBlob } from "/static/js/api.js";
import { fmtUtc, download } from "/static/js/panels/dossier-kit.js";

const C = window.Cesium;
const KM_OPTS = [1, 2.5, 5, 10, 20];
const DAY_OPTS = [30, 90, 180, 365];
const ROW_ORDER = ["s2", "landsat", "s1", "s1grd"];
const ARCHIVE = ["maxar", "wayback"];
const ROW_LABEL = { s2: "S2 · 10 m", landsat: "LS8/9 · 30 m", s1: "S1 RTC · 10 m", s1grd: "S1 GRD", maxar: "Maxar", wayback: "Wayback" };
const MISSION_ROW = { "Sentinel-2": "s2", Landsat: "landsat", "Sentinel-1": "s1" };
const MAX_TILES = 64;
const PREF_KEY = "noctua.imgview";
const HONEST = "Free sources only. Sub-metre commercial tasking (Maxar / Vantor, Planet, Airbus) is paid and not available here. " +
  "Best free: Maxar Open Data (0.3–0.5 m, only for disasters Maxar released), Esri Wayback (dated sub-metre archive mosaics), " +
  "Sentinel-2 (10 m optical), Sentinel-1 (10 m radar, day / night, through cloud), Landsat 8/9 (30 m).";

let viewer = null;          // Cesium.Viewer, set by installImageryViewer
let panel = null;           // the panel element (created on first open)
let S = null;               // state of the open site
let drape = null;           // { layer, extra: [[collection, layer]], key }
let blinkT = 0;

const pref = (() => { try { return JSON.parse(localStorage.getItem(PREF_KEY) || "{}") || {}; } catch (e) { return {}; } })();
const savePref = () => { try { localStorage.setItem(PREF_KEY, JSON.stringify(pref)); } catch (e) { /* private mode */ } };
const ts = (iso) => Date.parse(iso);
const pct = (v) => (v == null ? "n/a" : nf(v, v < 1 ? 1 : 0) + "%");
const mpx = (v) => (v == null ? "?" : v < 1 ? nf(v, 2) : nf(v, v < 10 ? 1 : 0)) + " m";

// same window maths as osint.imagery.geo.bbox_around (WGS84 metres per degree at the site)
function bboxAround(lat, lon, km) {
  const a = 6378137, f = 1 / 298.257223563, e2 = f * (2 - f), p = (lat * Math.PI) / 180;
  const w = Math.sqrt(1 - e2 * Math.sin(p) ** 2);
  const mx = ((Math.PI / 180) * a * Math.cos(p)) / w, my = ((Math.PI / 180) * a * (1 - e2)) / w ** 3;
  const dx = (km * 500) / Math.max(1, mx), dy = (km * 500) / my;
  return { west: lon - dx, south: lat - dy, east: lon + dx, north: lat + dy };
}
// the preset a scene is shown with: the chosen one when its sensor has it, else its default
const presetFor = (sc, want) => (sc.presets.includes(want) ? want : sc.presets[0]);
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v != null && v !== "")).toString();

// ------------------------------------------------------------------ public API

/** Called once from app.js: the globe (for draping and the right-click menu). */
export function installImageryViewer({ viewer: v }) {
  viewer = v;
  if (!v || !C) return;
  const menu = document.createElement("div");
  menu.className = "iv-ctx glass hidden";
  document.body.appendChild(menu);
  const hide = () => menu.classList.add("hidden");
  const h = new C.ScreenSpaceEventHandler(v.scene.canvas);
  h.setInputAction((e) => {
    const ray = v.camera.getPickRay(e.position);
    const cart = ray && v.scene.globe.pick(ray, v.scene);
    const p = cart ? C.Cartographic.fromCartesian(cart) : null;
    if (!p) { hide(); return; }
    const lat = C.Math.toDegrees(p.latitude), lon = C.Math.toDegrees(p.longitude);
    menu.innerHTML = '<div class="iv-ctx-h">' + lat.toFixed(4) + ", " + lon.toFixed(4) + '</div><button type="button" data-a="img">Imagery here</button>';
    menu.style.left = Math.min(e.position.x + 4, innerWidth - 180) + "px";
    menu.style.top = Math.min(e.position.y + 4, innerHeight - 80) + "px";
    menu.classList.remove("hidden");
    menu.onclick = (ev) => { if (ev.target.closest('[data-a="img"]')) { hide(); openImageryViewer({ lat, lon, label: "Point " + lat.toFixed(3) + ", " + lon.toFixed(3) }); } };
  }, C.ScreenSpaceEventType.RIGHT_CLICK);
  h.setInputAction(hide, C.ScreenSpaceEventType.LEFT_DOWN);
  h.setInputAction(hide, C.ScreenSpaceEventType.WHEEL);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
}

/** Open (or re-target) the viewer on a site. spec: {lat, lon, label, t, select: {a, b}, km, days} */
export function openImageryViewer(spec) {
  if (spec == null || spec.lat == null || spec.lon == null) return;
  ensurePanel();
  stopBlink();
  const km = spec.km || pref.km || 5;
  let days = spec.days || pref.days || 90;
  // make sure pre-selected scenes (e.g. the event's before / after crops) fall in the window
  const sel = spec.select || {};
  const oldest = Math.min(...[sel.aAt, sel.bAt].filter(Boolean).map(ts));
  if (isFinite(oldest)) days = Math.max(days, Math.min(365, Math.ceil((Date.now() - oldest) / 864e5) + 5));
  S = { lat: +spec.lat, lon: +spec.lon, label: spec.label || "", t: spec.t || null, km, days, cat: null, passes: null, a: null, b: null,
    want: { a: sel.a || null, b: sel.b || null, aAt: sel.aAt || null, bAt: sel.bAt || null }, preset: pref.preset || "true", mode: "single", src: new Set(), maxCloud: pref.maxCloud ?? 100,
    renders: new Map(), zoom: 1, tx: 0, ty: 0, presets: {}, req: 0 };
  panel.classList.remove("hidden", "min");
  renderShell();
  load();
}

export const imageryViewerOpen = () => !!(panel && !panel.classList.contains("hidden"));

// ------------------------------------------------------------------ panel shell

function ensurePanel() {
  if (panel) return;
  panel = document.createElement("section");
  panel.id = "iv";
  panel.className = "iv glass hidden";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Satellite imagery viewer");
  document.body.appendChild(panel);
  panel.addEventListener("click", onClick);
  panel.addEventListener("change", onChange);
  panel.addEventListener("input", onInput);
  document.addEventListener("keydown", (e) => {
    if (!imageryViewerOpen() || /INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) return;
    if (e.key === "Escape" && panel.classList.contains("max")) { panel.classList.remove("max"); e.stopImmediatePropagation(); }
  }, true);
}

function renderShell() {
  const opt = (vals, cur, fmt) => vals.map((v) => '<option value="' + v + '"' + (v === cur ? " selected" : "") + ">" + fmt(v) + "</option>").join("");
  panel.innerHTML =
    '<header class="iv-hd"><div class="iv-t"><span class="eyebrow">IMAGERY // SITE</span><b>' + esc(S.label || "Site") + "</b><small>" + S.lat.toFixed(4) + ", " + S.lon.toFixed(4) +
      (S.t ? " · event " + esc(fmtUtc(S.t)) : "") + "</small></div>" +
    '<label title="Side of the square window around the site">Window <select data-f="km">' + opt(KM_OPTS, S.km, (v) => v + " km") + "</select></label>" +
    '<label title="How far back to search the optical / radar catalogues (archives are always listed)">Period <select data-f="days">' + opt(DAY_OPTS, S.days, (v) => v + " d") + "</select></label>" +
    '<span class="iv-sp"></span><button type="button" data-a="fly" title="Fly the globe to the site">Fly to</button>' +
    '<button type="button" data-a="max" title="Maximise / restore">⤢</button><button type="button" data-a="min" title="Minimise (keeps the drape)">–</button><button type="button" data-a="close" title="Close (removes the drape)">×</button></header>' +
    '<div class="iv-body"><div class="iv-honest">' + esc(HONEST) + "</div>" +
    '<div class="iv-strip" data-slot="strip"><div class="dz-empty">Searching catalogues…</div></div>' +
    '<div class="iv-main"><div class="iv-left"><div class="iv-tools" data-slot="tools"></div><div class="iv-stage-box" data-slot="stage"></div></div>' +
    '<aside class="iv-side" data-slot="side"></aside></div>' +
    '<div class="iv-listbar" data-slot="filters"></div><div class="iv-list" data-slot="list"></div></div>';
}
const slot = (n) => panel.querySelector('[data-slot="' + n + '"]');

// ------------------------------------------------------------------ data

async function load() {
  const my = ++S.req;
  const base = { lat: S.lat.toFixed(5), lon: S.lon.toFixed(5), days: S.days };
  // fast catalogues first, the archives (Maxar index walk, Wayback history walk) separately
  const groups = ["s2,landsat,s1", "maxar", "wayback"];
  const merged = { scenes: [], sources: [], presets: {} };
  S.cat = null;
  const pull = (g) => fetchJson("/api/imagery/scenes?" + qs({ ...base, sources: g }), 90000).then((j) => {
    if (my !== S.req) return;
    merged.scenes = merged.scenes.concat(j.scenes || []).sort((x, y) => (x.acquired_at < y.acquired_at ? 1 : -1));
    merged.sources = merged.sources.concat(j.sources || []);
    merged.presets = j.presets || merged.presets;
    merged.window = j.window || merged.window;
    merged.took = Math.max(merged.took || 0, j.took_ms || 0);
    S.cat = merged; S.presets = merged.presets;
    afterCatalogue();
  }, (err) => {
    if (my !== S.req) return;
    merged.sources.push({ id: g, name: g, count: 0, error: "Unavailable: " + (err.message || err) });
    S.cat = merged; afterCatalogue();
  });
  const passes = fetchJson("/api/intel?" + qs({ kind: "point", lat: base.lat, lon: base.lon, t: S.t, sections: "overhead" }), 30000)
    .then((j) => { if (my === S.req) { S.passes = ((j.sections || {}).overhead || {}).passes || []; drawStrip(); } }, () => { if (my === S.req) { S.passes = []; drawStrip(); } });
  await Promise.all([...groups.map(pull), passes]);
  // a requested scene that no catalogue returned: fall back to the default pick
  if (my === S.req && S.cat && !S.a) { S.want.a = null; afterCatalogue(); }
}

function afterCatalogue() {
  const sc = S.cat.scenes;
  // a stored crop's scene by key, else (the catalogue re-issued the product under a new id)
  // the same sensor's scene nearest in time within 2 h
  const byKey = (k, at) => sc.find((s) => s.key === k || (s.alt_keys || []).includes(k)) || (at ? sc.filter((s) => s.source === k.split(":")[0])
    .map((s) => [Math.abs(ts(s.acquired_at) - ts(at)), s]).filter(([d]) => d < 72e5).sort((x, y) => x[0] - y[0]).map((x) => x[1])[0] : null);
  // initial picks: requested scenes, else the newest clear optical scene
  if (!S.a) {
    const want = S.want.a && byKey(S.want.a, S.want.aAt);
    // default: the newest fairly clear Sentinel-2 (10 m), else Landsat, else the newest scene
    const clear = sc.find((s) => s.source === "s2" && (s.cloud_pct ?? 100) <= 20) || sc.find((s) => s.kind === "optical" && (s.cloud_pct ?? 100) <= 20) || sc.find((s) => s.kind === "optical") || sc[0];
    if (want || (!S.want.a && clear)) setA(want || clear, true);
  }
  if (!S.b && S.want.b) { const wb = byKey(S.want.b, S.want.bAt); if (wb) { setB(wb, true); if (S.mode === "single") S.mode = "swipe"; } }
  drawStrip(); drawFilters(); drawList(); drawTools(); drawStage(); drawSide();
}

// render request (cached per key/preset/window/size); resolves {url, bounds, ...} or {error}
function renderOf(sc, preset, size, other) {
  if (sc.renderer === "tiles") return Promise.resolve({ tiles: true });
  const k = [sc.key, preset, S.km, size, other ? other.key : "", S.lat.toFixed(5), S.lon.toFixed(5)].join("|");
  if (!S.renders.has(k)) {
    S.renders.set(k, fetchJson("/api/imagery/render?" + qs({ key: sc.key, preset, lat: S.lat.toFixed(5), lon: S.lon.toFixed(5), km: S.km, size, key2: other ? other.key : null }), 120000)
      .catch((err) => ({ error: "Render failed: " + (err.message || err) })));
  }
  return S.renders.get(k);
}

// ------------------------------------------------------------------ selection

function setA(sc, quiet) { S.a = sc; S.zoom = 1; S.tx = S.ty = 0; if (!quiet) refresh(); }
function setB(sc, quiet) { S.b = sc; if (!quiet) refresh(); }
function refresh() { stopBlink(); drawStrip(); drawList(); drawTools(); drawStage(); drawSide(); }

// ------------------------------------------------------------------ time strip

function drawStrip() {
  const el = slot("strip");
  if (!el || !S.cat) return;
  const t1 = Date.now() + 4 * 864e5, t0 = S.cat.window ? ts(S.cat.window.from) : Date.now() - S.days * 864e5;
  const X = (t) => (((t - t0) / (t1 - t0)) * 100).toFixed(3) + "%";
  const sc = S.cat.scenes;
  const tick = (s) => {
    const sel = S.a && S.a.key === s.key ? " a" : S.b && S.b.key === s.key ? " b" : "";
    const op = s.kind === "optical" && s.cloud_pct != null ? (1 - Math.min(100, s.cloud_pct) / 125).toFixed(2) : 1;
    return '<button type="button" class="iv-tk k-' + s.kind + sel + '" data-key="' + esc(s.key) + '" style="left:' + X(ts(s.acquired_at)) + ";opacity:" + (sel ? 1 : op) + '" title="' +
      esc(tip(s)) + '">' + (sel ? "<i>" + sel.trim().toUpperCase() + "</i>" : "") + "</button>";
  };
  let h = '<div class="iv-axis"><span>' + esc(fmtUtc(new Date(t0).toISOString()).slice(0, 10)) + "</span><span>NOW</span><span>+4 d</span></div>";
  for (const row of ROW_ORDER) {
    const xs = sc.filter((s) => s.source === row);
    const pred = (S.passes || []).filter((p) => MISSION_ROW[p.mission] === row && ts(p.t) > Date.now());
    if (!xs.length && !pred.length && row === "s1grd") continue;
    h += '<div class="iv-row"><span class="iv-rl" title="' + esc(row) + '">' + esc(ROW_LABEL[row]) + " <small>" + xs.length + '</small></span><div class="iv-track">' +
      pred.map((p) => '<i class="iv-pred" style="left:' + X(ts(p.t)) + '" title="' + esc("Predicted " + p.satellite + " pass " + fmtUtc(p.t) + (p.kind === "optical" ? " · forecast cloud " + pct(p.cloud_pct) : " · SAR") + " (geometry only; acquisition not guaranteed)") + '"></i>').join("") +
      xs.map(tick).join("") + '<b class="iv-now" style="left:' + X(Date.now()) + '"></b>' + (S.t ? '<b class="iv-ev" style="left:' + X(ts(S.t)) + '" title="Event ' + esc(fmtUtc(S.t)) + '"></b>' : "") + "</div></div>";
  }
  const arch = sc.filter((s) => ARCHIVE.includes(s.source));
  const pending = ARCHIVE.filter((a) => !S.cat.sources.some((x) => x.id === a));
  h += '<div class="iv-row arch"><span class="iv-rl" title="Dated archives, listed whatever the period">ARCHIVE <small>' + arch.length + '</small></span><div class="iv-chips">' +
    arch.map((s) => '<button type="button" class="iv-chip k-' + s.kind + (S.a && S.a.key === s.key ? " a" : S.b && S.b.key === s.key ? " b" : "") + '" data-key="' + esc(s.key) + '" title="' + esc(tip(s)) + '">' +
      "<b>" + esc(s.source === "maxar" ? "MAXAR" : "WAYBACK") + "</b> " + esc(s.acquired_at.slice(0, 10)) + (s.source === "wayback" && !s.extra.capture_date_known ? " (release; capture n/a)" : "") + " · " + esc(s.platform || "?") + " · " + mpx(s.gsd_m) + "</button>").join("") +
    (pending.length ? '<span class="iv-wait">searching ' + esc(pending.join(", ")) + "…</span>" : !arch.length ? '<span class="iv-wait">no Maxar Open Data event or Wayback history here</span>' : "") + "</div></div>";
  el.innerHTML = h;
}

function tip(s) {
  const x = s.extra || {};
  return [s.name, s.platform, fmtUtc(s.acquired_at), "native " + mpx(s.gsd_m) + "/px", s.cloud_pct != null ? "scene cloud " + pct(s.cloud_pct) : "",
    x.orbit ? x.orbit + " orbit " + (x.rel_orbit ?? "") : "", x.off_nadir_deg != null ? "off-nadir " + nf(x.off_nadir_deg, 1) + "°" : "",
    x.release_date ? "Wayback release " + x.release_date + (x.first_release_date && x.first_release_date !== x.release_date ? " (first in " + x.first_release_date + ")" : "") : "",
    x.provider && s.source === "wayback" ? "provider " + x.provider : "", x.event ? "event " + x.event : "", s.id].filter(Boolean).join(" · ");
}

// ------------------------------------------------------------------ list + filters

function drawFilters() {
  const el = slot("filters");
  if (!el || !S.cat) return;
  const counts = {};
  for (const s of S.cat.scenes) counts[s.source] = (counts[s.source] || 0) + 1;
  const src = S.cat.sources.slice().sort((a, b) => Object.keys(ROW_LABEL).indexOf(a.id) - Object.keys(ROW_LABEL).indexOf(b.id));
  el.innerHTML = '<span class="eyebrow">Scenes</span>' + src.map((s) => '<button type="button" class="iv-src' + (S.src.has(s.id) ? " off" : "") + (s.error ? " err" : "") + '" data-src="' + esc(s.id) + '" title="' +
      esc((s.provider || "") + (s.error ? " · ERROR " + s.error : "") + (s.ms != null ? " · " + s.ms + " ms" : "")) + '">' + esc(ROW_LABEL[s.id] || s.name) + " <b>" + (s.error ? "!" : nf(counts[s.id] || 0)) + "</b></button>").join("") +
    '<label>max cloud <select data-f="cloud">' + [100, 60, 30, 10].map((v) => '<option value="' + v + '"' + (v === S.maxCloud ? " selected" : "") + ">" + (v === 100 ? "any" : "≤" + v + "%") + "</option>").join("") + "</select></label>" +
    '<span class="iv-sp"></span><small>' + nf(S.cat.scenes.length) + " scenes · catalogue " + nf(S.cat.took) + " ms</small>";
}

function visibleScenes() {
  return S.cat.scenes.filter((s) => !S.src.has(s.source) && (s.cloud_pct == null || s.cloud_pct <= S.maxCloud));
}

let thumbIO = null;
function drawList() {
  const el = slot("list");
  if (!el || !S.cat) return;
  const xs = visibleScenes();
  el.innerHTML = xs.length ? xs.map((s) => {
    const sel = S.a && S.a.key === s.key ? "a" : S.b && S.b.key === s.key ? "b" : "";
    return '<div class="iv-card k-' + s.kind + (sel ? " " + sel : "") + '" data-key="' + esc(s.key) + '" title="' + esc(tip(s)) + '"><div class="iv-th" data-thumb="' + esc(s.key) + '"></div>' +
      "<div class=\"iv-cc\"><b>" + esc(s.platform || s.name) + "</b><span>" + esc(fmtUtc(s.acquired_at)) + "</span><span>" + mpx(s.gsd_m) + "/px · " + (s.kind === "sar" ? "radar" : "cloud " + pct(s.cloud_pct)) + "</span>" +
      '<span class="iv-cb"><button type="button" data-set="a" title="Show as image A">A</button><button type="button" data-set="b" title="Compare against this as image B">B</button></span></div>' + (sel ? '<em>' + sel.toUpperCase() + "</em>" : "") + "</div>";
  }).join("") : '<div class="dz-empty">No scenes match the filters</div>';
  if (thumbIO) thumbIO.disconnect();
  thumbIO = new IntersectionObserver((ents) => { for (const e of ents) if (e.isIntersecting) { thumbIO.unobserve(e.target); loadThumb(e.target); } }, { root: el, rootMargin: "120px" });
  el.querySelectorAll("[data-thumb]").forEach((t) => thumbIO.observe(t));
}

// thumbnails render one at a time (they hit the same COGs / tiler as the main image)
let thumbQ = Promise.resolve();
function loadThumb(box) {
  const s = S.cat.scenes.find((x) => x.key === box.dataset.thumb);
  if (!s) return;
  if (s.renderer === "tiles") { box.innerHTML = waybackThumb(s); return; }
  const my = S.req;
  box.classList.add("ld");
  thumbQ = thumbQ.then(() => (my === S.req && box.isConnected ? renderOf(s, s.presets[0], "thumb") : null)).then((r) => {
    if (!r || !box.isConnected) return;
    box.classList.remove("ld");
    box.innerHTML = r.error ? '<span class="iv-terr" title="' + esc(r.error) + '">' + esc(r.error.slice(0, 60)) + "</span>" : '<img alt="" decoding="async" src="' + esc(r.url) + '">' +
      (r.site_cloud_pct != null ? '<small title="Cloud / shadow at the site (scene classification)">site cloud ' + pct(r.site_cloud_pct) + "</small>" : "");
  });
}
function waybackThumb(s) {
  const z = 15, n = 2 ** z, xf = ((S.lon + 180) / 360) * n, la = (S.lat * Math.PI) / 180;
  const yf = ((1 - Math.asinh(Math.tan(la)) / Math.PI) / 2) * n;
  const url = s.extra.tiles.replace("{z}", z).replace("{y}", Math.floor(yf)).replace("{x}", Math.floor(xf));
  return '<img alt="" decoding="async" referrerpolicy="no-referrer" src="' + esc(url) + '" style="object-position:' + ((xf % 1) * 100).toFixed(0) + "% " + ((yf % 1) * 100).toFixed(0) + '%">';
}

// ------------------------------------------------------------------ tools (modes + presets)

function drawTools() {
  const el = slot("tools");
  if (!el || !S.a) { if (el) el.innerHTML = ""; return; }
  const a = S.a, P = S.presets || {};
  const modes = [["single", "A only"], ["swipe", "Swipe"], ["side", "Side by side"], ["blink", "Blink"]];
  const pair = (p) => P[p] && P[p].pair;
  el.innerHTML = '<div class="iv-seg">' + modes.map(([m, l]) => '<button type="button" data-mode="' + m + '" class="' + (S.mode === m ? "on" : "") + '"' + (m !== "single" && !S.b ? " disabled title=\"Pick a B scene (B button on a scene card)\"" : "") + ">" + l + "</button>").join("") + "</div>" +
    '<div class="iv-seg">' + a.presets.map((p) => {
      const need = pair(p) && !(S.b && S.b.source === a.source);
      return '<button type="button" data-preset="' + p + '" class="' + (presetFor(a, S.preset) === p ? "on" : "") + '"' + (need ? ' disabled title="Needs a B scene from the same sensor"' : ' title="' + esc((P[p] || {}).label || p) + '"') + ">" + esc(((P[p] || {}).label || p).split(" (")[0]) + "</button>";
    }).join("") + "</div>";
}

// ------------------------------------------------------------------ stage

function bounds() { return bboxAround(S.lat, S.lon, S.km); }

function drawStage() {
  const box = slot("stage");
  if (!box) return;
  if (!S.a) { box.innerHTML = '<div class="dz-empty">' + (S.cat ? "No scene selected" : "Searching…") + "</div>"; return; }
  const a = S.a, pa = presetFor(a, S.preset), pairA = (S.presets[pa] || {}).pair;
  const side = S.mode === "side" && S.b && !pairA;
  const twoUp = (S.mode === "swipe" || S.mode === "blink") && S.b && !pairA;
  box.innerHTML = side
    ? '<div class="iv-side2">' + frameHtml("A", a) + frameHtml("B", S.b) + "</div>"
    : '<div class="iv-one">' + frameHtml(pairA ? "A×B" : "A", a, twoUp ? S.b : null) + "</div>";
  box.querySelectorAll(".iv-frame").forEach(wireFrame);
  // fill the frames: thumbnail first, then the full-resolution window
  const fill = (frame, sc, layer, other) => {
    const p = presetFor(sc, S.preset);
    const L = frame.querySelector('[data-layer="' + layer + '"]');
    frame._credits = [...new Set([...(frame._credits || []), sc.attribution])];
    if (sc.renderer === "tiles") { L.dataset.tiles = sc.extra.tiles; layoutTiles(frame); label(frame, layer, sc, null); return; }
    const use = (r, full) => {
      if (!L.isConnected) return;
      if (r.error) { if (!L.querySelector("img")) L.innerHTML = '<div class="iv-err">' + esc(r.error) + "</div>"; label(frame, layer, sc, null, r.error); return; }
      const img = new Image();
      img.decoding = "async"; img.alt = ""; img.draggable = false;
      img.onload = () => { if (!L.isConnected) return; L.querySelector("img")?.remove(); L.querySelector(".iv-err")?.remove(); L.prepend(img); L.dataset.full = full ? "1" : ""; L._r = r; label(frame, layer, sc, r); pixelation(frame); };
      img.src = r.url;
    };
    label(frame, layer, sc, null);
    renderOf(sc, p, "thumb", pairA ? other : null).then((r) => { use(r, false); if (!r.error) return renderOf(sc, p, "full", pairA ? other : null).then((f) => use(f, true)); return null; });
  };
  const frames = box.querySelectorAll(".iv-frame");
  fill(frames[0], a, "a", S.b);
  if (side) fill(frames[1], S.b, "a", null);
  else if (twoUp) fill(frames[0], S.b, "b", null);
  if (S.mode === "blink" && twoUp) startBlink(frames[0]);
  applyTransform();
}

function frameHtml(tag, sc, b) {
  return '<div class="iv-frame' + (b ? " cmp mode-" + S.mode : "") + '" data-tag="' + tag + '" tabindex="0" style="--pos:50%"><div class="iv-layers"><div class="iv-l" data-layer="a"></div>' +
    (b ? '<div class="iv-l iv-lb" data-layer="b"></div>' : "") + '<i class="iv-site" title="Site ' + S.lat.toFixed(4) + ", " + S.lon.toFixed(4) + '"></i></div>' +
    (b && S.mode === "swipe" ? '<div class="iv-div"><i></i></div>' : "") +
    '<div class="iv-lab l" data-lab="a"></div>' + (b ? '<div class="iv-lab r" data-lab="b"></div>' : "") +
    '<div class="iv-scale"><i></i><span></span></div><div class="iv-credit" data-credit></div><span class="iv-tag">' + tag + "</span></div>";
}

// image label: sensor · date · native / rendered m/px · cloud (scene / site) · preset; and credit
function label(frame, layer, sc, r, err) {
  const el = frame.querySelector('[data-lab="' + layer + '"]');
  if (!el) return;
  const x = sc.extra || {};
  const who = layer === "b" || frame.dataset.tag === "B" ? "B " : "A ";
  const bits = [who + (sc.platform || sc.name) + " " + (sc.sensor || ""), fmtUtc(sc.acquired_at) + (sc.source === "wayback" && !x.capture_date_known ? " (release)" : ""),
    "native " + mpx(sc.gsd_m) + (r ? " · shown " + mpx(r.m_per_px) + "/px" + (r.size === "thumb" ? " (preview)" : "") : sc.renderer === "tiles" ? " · Esri tiles" : ""),
    sc.kind === "sar" ? (x.calibrated === false ? "radar · GRD DN (uncalibrated)" : "radar γ0") + " · " + (x.orbit || "") : "cloud " + pct(sc.cloud_pct) + (r && r.site_cloud_pct != null ? " scene / " + pct(r.site_cloud_pct) + " site" : sc.cloud_pct != null ? " scene" : ""),
    r ? r.label : sc.renderer === "tiles" ? "Wayback " + (x.release_date || "") + " · " + (x.provider || "") : ""];
  el.innerHTML = bits.filter(Boolean).map((t) => "<span>" + esc(t) + "</span>").join("") + (r && r.note ? '<span class="note">' + esc(r.note) + "</span>" : "") + (err ? '<span class="bad">' + esc(err) + "</span>" : "");
  frame.querySelector("[data-credit]").textContent = (frame._credits || []).join(" · ");
}

// zoom / pan / swipe divider
function wireFrame(frame) {
  let drag = null;
  frame.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = frame.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const z0 = S.zoom, z1 = Math.max(1, Math.min(32, z0 * (e.deltaY < 0 ? 1.25 : 0.8)));
    S.tx = mx - ((mx - S.tx) * z1) / z0; S.ty = my - ((my - S.ty) * z1) / z0; S.zoom = z1;
    clampPan(r); applyTransform(); relayoutTiles();
  }, { passive: false });
  frame.addEventListener("pointerdown", (e) => {
    const r = frame.getBoundingClientRect(), pos = parseFloat(frame.style.getPropertyValue("--pos")) || 50;
    const onDiv = frame.classList.contains("mode-swipe") && Math.abs(e.clientX - r.left - (pos / 100) * r.width) < 14;
    drag = { divider: onDiv, x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty };
    frame.setPointerCapture(e.pointerId);
    if (onDiv) frame.classList.add("drag");
  });
  frame.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const r = frame.getBoundingClientRect();
    if (drag.divider) { frame.style.setProperty("--pos", Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100)) + "%"); return; }
    S.tx = drag.tx + e.clientX - drag.x; S.ty = drag.ty + e.clientY - drag.y; clampPan(r); applyTransform();
  });
  const up = () => { if (drag && !drag.divider) relayoutTiles(); drag = null; frame.classList.remove("drag"); };
  frame.addEventListener("pointerup", up);
  frame.addEventListener("pointercancel", up);
  frame.addEventListener("dblclick", () => { S.zoom = 1; S.tx = S.ty = 0; applyTransform(); relayoutTiles(); });
  frame.addEventListener("keydown", (e) => {
    if (!frame.classList.contains("mode-swipe")) return;
    const cur = parseFloat(frame.style.getPropertyValue("--pos")) || 50;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { frame.style.setProperty("--pos", Math.max(0, Math.min(100, cur + (e.key === "ArrowLeft" ? -5 : 5))) + "%"); e.preventDefault(); e.stopPropagation(); }
  });
}
function clampPan(r) {
  const w = r.width * S.zoom, h = r.height * S.zoom;
  S.tx = Math.min(0, Math.max(r.width - w, S.tx)); S.ty = Math.min(0, Math.max(r.height - h, S.ty));
}
function applyTransform() {
  if (!panel) return;
  panel.querySelectorAll(".iv-frame").forEach((f) => {
    const l = f.querySelector(".iv-layers");
    l.style.transform = "translate(" + S.tx + "px," + S.ty + "px) scale(" + S.zoom + ")";
    l.style.setProperty("--z", S.zoom);
    scaleBar(f); pixelation(f);
  });
}
// true pixels once an image pixel covers more than one screen pixel
function pixelation(f) {
  f.querySelectorAll(".iv-l").forEach((L) => {
    const r = L._r, w = f.clientWidth || 1;
    L.classList.toggle("px", !!(r && (w * S.zoom) / r.px[0] > 1.5));
  });
}
function scaleBar(f) {
  const w = f.clientWidth;
  if (!w) return;
  const mPerPx = (S.km * 1000) / (w * S.zoom);
  const target = mPerPx * 110, p = 10 ** Math.floor(Math.log10(target)), n = [5, 2, 1].map((k) => k * p).find((v) => v <= target) || p;
  const bar = f.querySelector(".iv-scale");
  bar.querySelector("i").style.width = (n / mPerPx).toFixed(1) + "px";
  bar.querySelector("span").textContent = (n >= 1000 ? n / 1000 + " km" : n + " m") + " · screen " + mpx(mPerPx) + "/px";
}

// Wayback: Esri tiles for the visible part of the window at the level matching the zoom
let tileT = 0;
function relayoutTiles() { clearTimeout(tileT); tileT = setTimeout(() => panel && panel.querySelectorAll(".iv-frame").forEach(layoutTiles), 120); }
function layoutTiles(frame) {
  const b = bounds(), w = frame.clientWidth || 600;
  frame.querySelectorAll(".iv-l[data-tiles]").forEach((L) => {
    const tpl = L.dataset.tiles, mPerPx = (S.km * 1000) / (w * S.zoom);
    const lat = (S.lat * Math.PI) / 180, z = Math.max(3, Math.min(19, Math.round(Math.log2((156543.03 * Math.cos(lat)) / mPerPx))));
    const n = 2 ** z, X = (lon) => ((lon + 180) / 360) * n, Y = (la) => ((1 - Math.asinh(Math.tan((la * Math.PI) / 180)) / Math.PI) / 2) * n;
    const LON = (x) => (x / n) * 360 - 180, LAT = (y) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
    // visible fraction of the frame -> lon/lat range
    const f0x = -S.tx / (w * S.zoom), f1x = (w - S.tx) / (w * S.zoom), f0y = -S.ty / (w * S.zoom), f1y = (w - S.ty) / (w * S.zoom);
    const lo0 = b.west + f0x * (b.east - b.west), lo1 = b.west + f1x * (b.east - b.west), la0 = b.north - f0y * (b.north - b.south), la1 = b.north - f1y * (b.north - b.south);
    let x0 = Math.floor(X(lo0)), x1 = Math.floor(X(lo1)), y0 = Math.floor(Y(la0)), y1 = Math.floor(Y(la1));
    while ((x1 - x0 + 1) * (y1 - y0 + 1) > MAX_TILES) { x0++; x1--; y0++; y1--; }
    const fx = (lon) => ((lon - b.west) / (b.east - b.west)) * 100, fy = (la) => ((b.north - la) / (b.north - b.south)) * 100;
    let h = "";
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const l = fx(LON(x)), r = fx(LON(x + 1)), t = fy(LAT(y)), bt = fy(LAT(y + 1));
      h += '<img alt="" draggable="false" decoding="async" referrerpolicy="no-referrer" src="' + esc(tpl.replace("{z}", z).replace("{y}", y).replace("{x}", x)) + '" style="left:' + l.toFixed(3) + "%;top:" + t.toFixed(3) + "%;width:" + (r - l).toFixed(3) + "%;height:" + (bt - t).toFixed(3) + '%">';
    }
    L.innerHTML = h;
    L.dataset.z = z;
  });
}

function startBlink(frame) {
  stopBlink();
  const L = frame.querySelector('[data-layer="b"]');
  if (!L) return;
  blinkT = setInterval(() => { if (!L.isConnected) { stopBlink(); return; } L.classList.toggle("off"); frame.classList.toggle("blink-b", !L.classList.contains("off")); }, 800);
}
function stopBlink() { clearInterval(blinkT); blinkT = 0; }

// ------------------------------------------------------------------ side panel: details + actions

function drawSide() {
  const el = slot("side");
  if (!el) return;
  if (!S.a) { el.innerHTML = ""; return; }
  const det = (tag, s) => {
    if (!s) return '<div class="iv-det none"><b>' + tag + '</b><span>Pick a scene with the B button to compare</span></div>';
    const x = s.extra || {};
    const rows = [["Sensor", (s.platform || "") + " " + s.sensor], ["Source", s.name], ["Acquired", fmtUtc(s.acquired_at) + (s.source === "wayback" && !x.capture_date_known ? " (release date; capture unknown)" : "")],
      ["Native res.", mpx(s.gsd_m) + "/px"], [s.kind === "sar" ? "Mode" : "Cloud (scene)", s.kind === "sar" ? (x.mode || "IW") + " · " + (x.polarizations || []).join("+").toUpperCase() + " · " + (x.orbit || "") + " rel. orbit " + (x.rel_orbit ?? "?") : pct(s.cloud_pct)],
      x.off_nadir_deg != null ? ["Off-nadir", nf(x.off_nadir_deg, 1) + "°"] : null, x.sun_elev_deg != null ? ["Sun elev.", nf(x.sun_elev_deg, 1) + "°"] : null,
      x.release_date ? ["Wayback", "release " + x.release_date + (x.first_release_date !== x.release_date ? " (first " + x.first_release_date + ")" : "") + " · " + (x.provider || "") + (x.product ? " " + x.product : "")] : null,
      x.accuracy_m != null ? ["Accuracy", "±" + nf(x.accuracy_m) + " m CE90"] : null, x.event ? ["Maxar event", x.event_title || x.event] : null,
      x.tile ? ["Tile", x.tile] : null, ["ID", s.id]].filter(Boolean);
    return '<div class="iv-det"><b>' + tag + "</b>" + rows.map(([k, v]) => "<div><span>" + esc(k) + "</span><em>" + esc(v) + "</em></div>").join("") + '<div class="iv-r" data-r="' + tag + '"></div></div>';
  };
  const d = drape && drape.key;
  el.innerHTML = det("A", S.a) + det("B", S.b) +
    '<div class="iv-act"><button type="button" data-a="drape" title="Lay image A on the globe (clipped to the window)">' + (d ? "Re-drape A" : "Drape A on globe") + "</button>" +
    (S.b ? '<button type="button" data-a="drapeb" title="Lay image B on the globe">Drape B</button>' : "") +
    '<label class="iv-op" title="Drape opacity">opacity <input type="range" min="0" max="100" value="' + Math.round((pref.opacity ?? 0.9) * 100) + '" data-f="opacity"></label>' +
    (d ? '<button type="button" data-a="undrape">Remove drape</button>' : "") +
    '<button type="button" data-a="dl" ' + (S.a.renderer === "tiles" ? 'disabled title="Esri Wayback tiles are view-only (Esri terms)"' : 'title="PNG of image A at the rendered resolution"') + ">Download A (PNG)</button>" +
    (S.b ? '<button type="button" data-a="dlb" ' + (S.b.renderer === "tiles" ? 'disabled title="Esri Wayback tiles are view-only (Esri terms)"' : "") + ">Download B (PNG)</button>" : "") + "</div>" +
    '<div class="iv-srcs"><span class="eyebrow">Sources</span>' + (S.cat ? S.cat.sources.map((s) => '<div class="' + (s.error ? "err" : "") + '"><b>' + esc(s.name) + "</b><span>" + (s.error ? esc(s.error) : nf(s.count) + " · " + esc(s.provider || "") + " · " + (s.gsd_m != null ? mpx(s.gsd_m) : "")) + "</span></div>").join("") : "") + "</div>";
}

// ------------------------------------------------------------------ drape

function removeDrape() {
  if (!drape || !viewer) { drape = null; return; }
  viewer.imageryLayers.remove(drape.layer, true);
  for (const [coll, l] of drape.extra) { try { coll.remove(l, true); } catch (e) { /* tileset gone */ } }
  drape = null;
  viewer.scene.requestRender();
}
async function drapeScene(sc) {
  if (!viewer || !C || !sc) return;
  removeDrape();
  const b = bounds(), rect = C.Rectangle.fromDegrees(b.west, b.south, b.east, b.north);
  const credit = new C.Credit(sc.attribution || "");
  let provider;
  if (sc.renderer === "tiles") provider = new C.UrlTemplateImageryProvider({ url: sc.extra.tiles, rectangle: rect, maximumLevel: 19, credit });
  else {
    const r = await renderOf(sc, presetFor(sc, S.preset), "full", (S.presets[presetFor(sc, S.preset)] || {}).pair ? S.b : null);
    if (r.error) return;
    const rb = r.bounds;
    provider = await C.SingleTileImageryProvider.fromUrl(r.url, { rectangle: C.Rectangle.fromDegrees(rb.west, rb.south, rb.east, rb.north), credit });
  }
  const layer = new C.ImageryLayer(provider, { alpha: pref.opacity ?? 0.9 });
  viewer.imageryLayers.add(layer, Math.min(1, viewer.imageryLayers.length));
  const extra = [];
  // Google Photorealistic 3D tiles replace the globe close in: drape onto them too
  const prims = viewer.scene.primitives;
  for (let i = 0; i < prims.length; i++) {
    const p = prims.get(i);
    if (p instanceof C.Cesium3DTileset && p.imageryLayers) {
      const l2 = new C.ImageryLayer(provider, { alpha: pref.opacity ?? 0.9 });
      p.imageryLayers.add(l2); extra.push([p.imageryLayers, l2]);
    }
  }
  drape = { layer, extra, key: sc.key };
  viewer.scene.requestRender();
}
function flySite() {
  if (!viewer || !C) return;
  const b = bounds();
  viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(b.west - (b.east - b.west) * 0.3, b.south - (b.north - b.south) * 0.3, b.east + (b.east - b.west) * 0.3, b.north + (b.north - b.south) * 0.3), duration: 1.4 });
}

async function downloadScene(sc) {
  if (!sc || sc.renderer === "tiles") return;
  const p = presetFor(sc, S.preset), pair = (S.presets[p] || {}).pair;
  const r = await renderOf(sc, p, "full", pair ? (sc === S.a ? S.b : S.a) : null);
  if (r.error) return;
  const png = await fetchJson("/api/imagery/render?" + qs({ key: sc.key, preset: p, lat: S.lat.toFixed(5), lon: S.lon.toFixed(5), km: S.km, size: "full", key2: pair ? (sc === S.a ? S.b : S.a).key : null, fmt: "png" }), 120000);
  if (png.error) return;
  const name = [sc.platform || sc.source, sc.acquired_at.slice(0, 16).replace(/[-:]/g, ""), p, S.lat.toFixed(4) + "N", S.lon.toFixed(4) + "E", S.km + "km"].join("_").replace(/[^A-Za-z0-9_.-]+/g, "") + ".png";
  download(name, await fetchBlob(png.url, 60000));
}

// ------------------------------------------------------------------ events

function onClick(e) {
  const t = e.target;
  const a = t.closest("[data-a]");
  if (a) {
    const act = a.dataset.a;
    if (act === "close") { stopBlink(); removeDrape(); panel.classList.add("hidden"); S && (S.req++); }
    else if (act === "min") panel.classList.toggle("min");
    else if (act === "max") panel.classList.toggle("max");
    else if (act === "fly") flySite();
    else if (act === "drape") drapeScene(S.a).then(drawSide, drawSide);
    else if (act === "drapeb") drapeScene(S.b).then(drawSide, drawSide);
    else if (act === "undrape") { removeDrape(); drawSide(); }
    else if (act === "dl") downloadScene(S.a).catch((err) => console.warn("imagery download", err));
    else if (act === "dlb") downloadScene(S.b).catch((err) => console.warn("imagery download", err));
    if (act === "max" || act === "min") requestAnimationFrame(() => { applyTransform(); relayoutTiles(); });
    return;
  }
  const set = t.closest("[data-set]");
  if (set) {
    const sc = S.cat.scenes.find((s) => s.key === set.closest("[data-key]").dataset.key);
    if (set.dataset.set === "a") setA(sc); else { setB(sc, true); if (S.mode === "single") S.mode = "swipe"; refresh(); }
    return;
  }
  const k = t.closest("[data-key]");
  if (k && S && S.cat) {
    const sc = S.cat.scenes.find((s) => s.key === k.dataset.key);
    if (!sc) return;
    if (e.shiftKey) { setB(sc, true); if (S.mode === "single") S.mode = "swipe"; refresh(); } else setA(sc);
    return;
  }
  const m = t.closest("[data-mode]");
  if (m && !m.disabled) { S.mode = m.dataset.mode; refresh(); return; }
  const p = t.closest("[data-preset]");
  if (p && !p.disabled) { S.preset = p.dataset.preset; pref.preset = S.preset; savePref(); refresh(); return; }
  const src = t.closest("[data-src]");
  if (src) { const id = src.dataset.src; if (S.src.has(id)) S.src.delete(id); else S.src.add(id); drawFilters(); drawList(); }
}
function onChange(e) {
  const f = e.target.dataset.f;
  if (f === "km") { S.km = +e.target.value; pref.km = S.km; savePref(); S.zoom = 1; S.tx = S.ty = 0; refresh(); }
  else if (f === "days") { S.days = +e.target.value; pref.days = S.days; savePref(); S.renders.clear(); const keep = { a: S.a && S.a.key, b: S.b && S.b.key }; S.a = S.b = null; S.want = keep; load(); }
  else if (f === "cloud") { S.maxCloud = +e.target.value; pref.maxCloud = S.maxCloud; savePref(); drawList(); }
}
function onInput(e) {
  if (e.target.dataset.f !== "opacity") return;
  pref.opacity = +e.target.value / 100; savePref();
  if (drape) { drape.layer.alpha = pref.opacity; for (const [, l] of drape.extra) l.alpha = pref.opacity; viewer.scene.requestRender(); }
}
