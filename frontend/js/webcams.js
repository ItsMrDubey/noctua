// Public webcams layer: ONLY cameras whose owners openly publish them (open-data / operator APIs).
// Perf rules: one BillboardCollection (diffed by id), grid-bucket clusters at world zoom, individual
// cameras below CAM_MAX_H, a single recycled label (hover/selection only), requestRender after changes.
// Standalone module: the controller wires it (see webcams-report.md).
import { fetchJson } from "/static/js/api.js";
import { esc } from "/static/js/ui.js";

const C = Cesium;
const CYAN = "#5ee3ea";
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const CAM_MAX_H = 1.5e6;      // metres of eye altitude below which individual cameras are drawn
const CAM_CAP = 1500;         // hard cap on individual billboards in view
const MIN_REFRESH_S = 10;
const SNAP = (id, t) => "/api/webcams/" + encodeURIComponent(id) + "/snapshot?t=" + (t || Date.now());
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const pos = (lon, lat) => C.Cartesian3.fromDegrees(lon, lat, 0);
const BB_SCALE = new C.NearFarScalar(2e5, 1.0, 1.5e6, 0.7);
const LABEL_OFFSET = new C.Cartesian2(12, 0);

const CAM_SVG = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="' + CYAN +
  '" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5.5" width="11" height="9" rx="1.5"/>' +
  '<path d="M13.5 9l4-2.2v6.4l-4-2.2"/><circle cx="8" cy="10" r="1.6"/></svg>');

const clusterIcons = new Map();
function clusterIcon(n) {
  const text = n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  let url = clusterIcons.get(text);
  if (url) return url;
  const s = 34, cv = document.createElement("canvas");
  cv.width = cv.height = s;
  const g = cv.getContext("2d");
  g.fillStyle = "rgba(7,13,21,0.82)";
  g.strokeStyle = CYAN;
  g.lineWidth = 1.6;
  g.beginPath(); g.arc(s / 2, s / 2, s / 2 - 2, 0, Math.PI * 2); g.fill(); g.stroke();
  g.fillStyle = CYAN;
  g.font = "600 11px 'JetBrains Mono', Consolas, monospace";
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(text, s / 2, s / 2 + 0.5);
  url = cv.toDataURL();
  clusterIcons.set(text, url);
  return url;
}

export class WebcamLayer {
  constructor(viewer, { onSelect } = {}) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.onSelect = onSelect || (() => {});
    this.visible = false;
    this.sources = null;     // Set of source ids, or null = all
    this.items = [];         // slim catalogue (loaded once)
    this.loading = null;
    this.live = new Map();   // id -> {b, item} individual billboards
    this.clusters = [];      // cluster billboards
    this.mode = null;
    this.selected = null;
    this.bb = this.scene.primitives.add(new C.BillboardCollection({ scene: this.scene }));
    this.labels = this.scene.primitives.add(new C.LabelCollection());
    this.label = this.labels.add({
      show: false, font: FONT, fillColor: C.Color.fromCssColorString(CYAN), outlineColor: C.Color.BLACK, outlineWidth: 3,
      style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: LABEL_OFFSET, horizontalOrigin: C.HorizontalOrigin.LEFT,
      verticalOrigin: C.VerticalOrigin.CENTER, disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
    this.bb.show = this.labels.show = false;
    this._stopInspector = null;
  }

  setVisible(v) {
    this.visible = !!v;
    this.bb.show = this.labels.show = this.visible;
    if (!this.visible) { this.closeInspector(); this.label.show = false; }
    this.scene.requestRender();
    if (this.visible) this.refreshForView().catch(() => {});
  }

  // sources: iterable of source ids, or null/empty for all
  setSources(sources) {
    const s = sources && [...sources].length ? new Set(sources) : null;
    this.sources = s;
    this.mode = null;               // force rebuild
    if (this.visible) this.refreshForView().catch(() => {});
  }

  async load() {
    if (this.items.length) return this.items;
    if (!this.loading) {
      this.loading = fetchJson("/api/webcams?limit=20000", 60000)
        .then((j) => { this.items = j.items || []; return this.items; })
        .finally(() => { this.loading = null; });
    }
    return this.loading;
  }

  height() { return this.viewer.camera.positionCartographic.height; }

  _filtered() { return this.sources ? this.items.filter((w) => this.sources.has(w.source)) : this.items; }

  // rect: Cesium.Rectangle (radians) or undefined => computed from the camera (whole globe if not available)
  async refreshForView(rect) {
    if (!this.visible) return;
    await this.load();
    const h = this.height();
    if (h > CAM_MAX_H) return this._showClusters(h);
    const r = rect || this.viewer.camera.computeViewRectangle(this.scene.globe.ellipsoid);
    this._showCameras(r);
  }

  _clearClusters() {
    for (const b of this.clusters) this.bb.remove(b);
    this.clusters = [];
  }

  _clearCameras() {
    for (const { b } of this.live.values()) this.bb.remove(b);
    this.live.clear();
  }

  _showClusters(h) {
    if (this.mode !== "cluster") { this._clearCameras(); this.mode = "cluster"; }
    this._clearClusters();
    const cell = h > 1.2e7 ? 10 : h > 6e6 ? 5 : h > 3e6 ? 2.5 : 1.25;   // degrees
    const grid = new Map();
    for (const w of this._filtered()) {
      const k = Math.floor((w.lon + 180) / cell) + ":" + Math.floor((w.lat + 90) / cell);
      let g = grid.get(k);
      if (!g) grid.set(k, (g = { n: 0, lat: 0, lon: 0 }));
      g.n++; g.lat += w.lat; g.lon += w.lon;
    }
    for (const g of grid.values()) {
      const lat = g.lat / g.n, lon = g.lon / g.n;
      this.clusters.push(this.bb.add({
        position: pos(lon, lat), image: clusterIcon(g.n),
        disableDepthTestDistance: Number.POSITIVE_INFINITY, id: { webcamCluster: { lat, lon, n: g.n } },
      }));
    }
    this.scene.requestRender();
  }

  _showCameras(rect) {
    if (this.mode !== "cams") { this._clearClusters(); this.mode = "cams"; }
    const want = new Map();
    const inRect = (w) => {
      if (!rect) return true;
      const lon = C.Math.toRadians(w.lon), lat = C.Math.toRadians(w.lat);
      const west = rect.west, east = rect.east;
      const okLon = west <= east ? lon >= west && lon <= east : lon >= west || lon <= east;
      return okLon && lat >= rect.south && lat <= rect.north;
    };
    for (const w of this._filtered()) {
      if (want.size >= CAM_CAP) break;
      if (inRect(w)) want.set(w.id, w);
    }
    // diff by id
    for (const [id, e] of this.live) if (!want.has(id)) { this.bb.remove(e.b); this.live.delete(id); }
    for (const [id, w] of want) {
      if (this.live.has(id)) continue;
      const b = this.bb.add({
        position: pos(w.lon, w.lat), image: CAM_SVG, width: 20, height: 20, scaleByDistance: BB_SCALE,
        disableDepthTestDistance: Number.POSITIVE_INFINITY, id: { webcam: w },
      });
      this.live.set(id, { b, item: w });
    }
    this.scene.requestRender();
  }

  // Accepts a Cesium pick result ({primitive, id}) or a primitive. Returns the slim item for a camera,
  // or null (clusters zoom in instead of selecting).
  pick(p) {
    const id = (p && p.id && (p.id.webcam || p.id.webcamCluster) ? p.id : p && p.primitive && p.primitive.id) || (p && p.webcam ? p : null);
    if (!id) return null;
    if (id.webcamCluster) {
      const c = id.webcamCluster;
      this.viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(c.lon, c.lat, Math.max(8e5, this.height() / 3)), duration: 0.8 });
      return null;
    }
    if (id.webcam) {
      this.select(id.webcam);
      return id.webcam;
    }
    return null;
  }

  select(item) {
    this.selected = item;
    this.label.text = item.name;
    this.label.position = pos(item.lon, item.lat);
    this.label.show = true;
    this.scene.requestRender();
    this.onSelect(item);
  }

  clearSelection() {
    this.selected = null;
    this.label.show = false;
    this.scene.requestRender();
  }

  closeInspector() {
    if (this._stopInspector) { this._stopInspector(); this._stopInspector = null; }
  }

  // Renders into containerEl and keeps the snapshot refreshing until the container is detached,
  // renderInspector is called again, or closeInspector() runs. Returns the stop function.
  renderInspector(item, containerEl) {
    this.closeInspector();
    const stop = renderCamera(item, containerEl);
    this._stopInspector = stop;
    return stop;
  }
}

function renderCamera(slim, el) {
  let timer = null, stopped = false, rec = slim;
  const stop = () => { stopped = true; if (timer) clearInterval(timer); timer = null; };
  el.innerHTML = '<div class="wc-insp"><div class="wc-hd">' + esc(slim.name) + '</div><div class="wc-img wc-loading">Loading camera…</div></div>';
  const draw = () => {
    const vid = safeUrl(rec.video_url), emb = safeUrl(rec.embed_url);
    const playable = vid && /\.(mp4|webm)(\?|$)/i.test(vid);
    const row = (k, v) => (v ? "<span>" + esc(k) + "</span><div>" + v + "</div>" : "");
    el.innerHTML =
      '<div class="wc-insp"><div class="wc-hd">' + esc(rec.name) + "</div>" +
      '<div class="wc-img"><img alt="' + esc(rec.name) + '" src="' + SNAP(rec.id) + '"></div>' +
      (playable ? '<video class="wc-video" controls muted playsinline preload="none" src="' + esc(vid) + '"></video>' : "") +
      '<div class="kv">' +
      row("Source", esc(rec.source)) + row("Position", esc(rec.lat.toFixed(4) + ", " + rec.lon.toFixed(4))) +
      row("Country", esc(rec.country)) + row("Refresh", esc(rec.refresh_s + " s")) +
      row("Licence", esc(rec.licence)) + row("Attribution", esc(rec.attribution)) +
      row("Stream", vid && !playable ? '<a href="' + esc(vid) + '" target="_blank" rel="noopener noreferrer">open stream</a>' : "") +
      row("Embed", emb ? '<a href="' + esc(emb) + '" target="_blank" rel="noopener noreferrer">open player</a>' : "") +
      "</div></div>";
    const im = el.querySelector(".wc-img img");
    if (im) {
      im.onerror = () => { im.replaceWith(Object.assign(document.createElement("div"), { className: "wc-loading", textContent: "Snapshot unavailable" })); };
    }
  };
  const tick = () => {
    if (stopped) return;
    if (!el.isConnected) return stop();
    const im = el.querySelector(".wc-img img");
    if (!im) return;
    const next = new Image();   // swap only when the new frame has loaded (no flicker)
    next.onload = () => { if (!stopped && im.isConnected) im.src = next.src; };
    next.src = SNAP(rec.id);
  };
  fetchJson("/api/webcams/" + encodeURIComponent(slim.id)).then((full) => {
    if (stopped) return;
    rec = full;
    draw();
    timer = setInterval(tick, Math.max(MIN_REFRESH_S, rec.refresh_s || 60) * 1000);
  }).catch(() => { if (!stopped) el.innerHTML = '<div class="wc-insp"><div class="wc-loading">Camera record unavailable</div></div>'; });
  return stop;
}

// "Cameras near this event": thumbnails of the nearest published cameras. Returns a promise of the items.
export async function renderNearbyCameras(lat, lon, containerEl, { radiusKm = 25, limit = 6, onSelect } = {}) {
  containerEl.innerHTML = '<div class="wc-near-h">Cameras nearby</div><div class="wc-loading">Searching…</div>';
  let items = [];
  try {
    const j = await fetchJson("/api/webcams/near?lat=" + encodeURIComponent(lat) + "&lon=" + encodeURIComponent(lon) +
      "&radius_km=" + radiusKm + "&limit=" + limit);
    items = j.items || [];
  } catch (e) {
    containerEl.innerHTML = '<div class="wc-near-h">Cameras nearby</div><div class="wc-loading">Unavailable</div>';
    return [];
  }
  if (!items.length) {
    containerEl.innerHTML = '<div class="wc-near-h">Cameras nearby</div><div class="wc-loading">None within ' + radiusKm + " km</div>";
    return items;
  }
  containerEl.innerHTML = '<div class="wc-near-h">Cameras nearby <span>' + items.length + "</span></div>" +
    '<div class="wc-near">' + items.map((w, i) =>
      '<button type="button" class="wc-thumb" data-i="' + i + '" title="' + esc(w.name) + '">' +
      '<img loading="lazy" alt="" src="' + SNAP(w.id) + '"><span>' + esc(w.name) + "</span><em>" + w.distance_km.toFixed(1) + " km</em></button>").join("") +
    "</div>";
  containerEl.querySelectorAll(".wc-thumb img").forEach((im) => { im.onerror = () => im.classList.add("wc-bad"); });
  containerEl.querySelectorAll(".wc-thumb").forEach((btn) => btn.addEventListener("click", () => {
    if (onSelect) onSelect(items[Number(btn.dataset.i)]);
  }));
  return items;
}
