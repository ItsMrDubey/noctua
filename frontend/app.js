import { fetchJson, poll } from "/static/js/api.js";
import { AircraftLayer, ShipLayer, FireLayer, QuakeLayer, makeImagery } from "/static/js/layers.js";
import { TrackList } from "/static/js/tracklist.js";
import { Trails } from "/static/js/trails.js";
import { DensityLayer } from "/static/js/density.js";
import { Timeline } from "/static/js/timeline.js";
import { LiveSocket } from "/static/js/livesocket.js";
import { $, esc, renderStatus, startClock, detailsHtml } from "/static/js/ui.js";

const errors = {};
let lastStatus = null;

async function loadConfig() {
  try { return await fetchJson("/api/config", 8000); } catch (e) { return { cesium_ion_token: null }; }
}

async function main() {
  startClock();
  if (typeof Cesium === "undefined") { $("sources").textContent = "CesiumJS failed to load (CDN unreachable)"; return; }
  const cfg = await loadConfig();
  const opts = {
    baseLayer: false, geocoder: false, baseLayerPicker: false, timeline: false, animation: false,
    homeButton: false, navigationHelpButton: false, sceneModePicker: true, fullscreenButton: false, infoBox: false, selectionIndicator: false,
  };
  if (cfg && cfg.cesium_ion_token) {
    Cesium.Ion.defaultAccessToken = cfg.cesium_ion_token;
    opts.terrain = Cesium.Terrain.fromWorldTerrain();
  }
  const viewer = new Cesium.Viewer("globe", opts);
  const scene = viewer.scene;
  scene.globe.baseColor = Cesium.Color.fromCssColorString("#0a1220");
  scene.globe.enableLighting = false;
  viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(20, 25, 2.2e7) });

  // ---- Imagery ----
  const layers = viewer.imageryLayers;
  layers.addImageryProvider(makeImagery("base"));
  const today = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  const dateIn = $("gibs-date");
  dateIn.value = today;
  dateIn.max = new Date().toISOString().slice(0, 10);
  const overlay = { labels: null, viirs: null, night: null };
  const kindOf = { labels: "labels", viirs: "viirs", night: "night" };
  function setOverlay(name, on) {
    if (overlay[name]) { layers.remove(overlay[name], true); overlay[name] = null; }
    if (on) {
      const l = layers.addImageryProvider(makeImagery(kindOf[name], dateIn.value || today));
      l.alpha = name === "night" ? 0.8 : 1;
      overlay[name] = l;
      // keep labels on top
      if (name !== "labels" && overlay.labels) layers.raiseToTop(overlay.labels);
    }
  }
  for (const n of ["labels", "viirs", "night"]) $("t-" + n).addEventListener("change", (e) => setOverlay(n, e.target.checked));
  dateIn.addEventListener("change", () => {
    for (const n of ["viirs", "night"]) if ($("t-" + n).checked) setOverlay(n, true);
  });

  // ---- Data layers ----
  const air = new AircraftLayer(scene), ships = new ShipLayer(scene), fires = new FireLayer(scene), quakes = new QuakeLayer(scene);
  const all = [air, ships, fires, quakes];
  const byKind = { aircraft: air, ships, fires, quakes };
  const applyFilters = () => {
    air.filter = (it) => !$("t-aircraft-hp").checked || !!(it && it.high_priority);
    ships.filter = (it) => !$("t-ships-mil").checked || !!(it && it.is_mil);
    air.refilter(); ships.refilter();
  };
  applyFilters();
  for (const k of ["aircraft", "ships", "fires", "quakes"]) {
    $("t-" + k).addEventListener("change", (e) => { byKind[k].show = e.target.checked; });
  }
  $("t-aircraft-hp").addEventListener("change", applyFilters);
  $("t-ships-mil").addEventListener("change", applyFilters);

  const setCount = (k, n) => { $("c-" + k).textContent = n == null ? "–" : n.toLocaleString(); };
  const onErr = (name, err) => {
    if (err) errors[name] = err; else delete errors[name];
    renderStatus(lastStatus, errors);
  };
  let replayAt = null, live = null;
  const trails = new Trails(scene);
  const dens = { aircraft: new DensityLayer(scene, "aircraft"), ships: new DensityLayer(scene, "ships"), fires: new DensityLayer(scene, "fires"), quakes: new DensityLayer(scene, "quakes") };
  for (const k of Object.keys(dens)) $("d-" + k).addEventListener("change", (e) => dens[k].setOn(e.target.checked));
  $("d-res").addEventListener("change", (e) => { for (const d of Object.values(dens)) d.setRes(+e.target.value); });
  $("t-miltrails").addEventListener("change", (e) => trails.setMil(e.target.checked));
  const wsLive = () => !!(live && live.isOpen());
  const feed = (kind, url, layer) => async () => {
    if (replayAt || ((kind === "aircraft" || kind === "ships") && wsLive())) return;
    const data = await fetchJson(url);
    const items = Array.isArray(data && data.items) ? data.items : [];
    layer.sync(items);
    setCount(kind, items.length);
    if (selected && selected.kind === kind) refreshSelected();
  };
  const trailSelected = (force) => {
    if (!selected || (selected.kind !== "aircraft" && selected.kind !== "ships")) return;
    trails.showSelected(selected.kind, selected.id, byKind[selected.kind].items.get(selected.id), force);
  };

  // ---- Selection / details ----
  let selected = null, following = false;
  const tracker = viewer.entities.add({ position: Cesium.Cartesian3.fromDegrees(0, 0, 0) });
  const followBtn = $("d-follow");

  function setFollow(on) {
    following = on;
    followBtn.textContent = "Follow: " + (on ? "on" : "off");
    followBtn.classList.toggle("on", on);
    if (on && selected) {
      const p = byKind[selected.kind].positionOf(selected.id);
      if (p) { tracker.position = p.clone(); viewer.trackedEntity = tracker; }
    } else {
      viewer.trackedEntity = undefined;
    }
  }
  function refreshSelected() {
    const it = byKind[selected.kind].items.get(selected.id);
    if (!it) return;
    const [title, body] = detailsHtml(selected.kind, it);
    $("d-title").textContent = title;
    $("d-body").innerHTML = body;
    if (following && selected.kind === "aircraft") {
      const p = air.positionOf(selected.id);
      if (p) tracker.position = p.clone();
    }
  }
  function select(kind, id) {
    setFollow(false);
    selected = { kind, id };
    $("right").classList.remove("hidden");
    tracklist.setVisible(false);
    followBtn.classList.toggle("hidden", kind !== "aircraft");
    refreshSelected();
    trails.clearSelected();
    trailSelected(true);
  }
  function flyToItem(kind, id) {
    const it = byKind[kind].items.get(id);
    if (!it) return;
    const alt = (kind === "aircraft" ? 4e5 : kind === "ships" ? 2e5 : 1.5e6);
    viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(it.lon, it.lat, alt), duration: 1.8 });
  }
  $("d-fly").addEventListener("click", () => selected && flyToItem(selected.kind, selected.id));
  followBtn.addEventListener("click", () => setFollow(!following));
  $("close-details").addEventListener("click", () => { setFollow(false); selected = null; trails.clearSelected(); $("right").classList.add("hidden"); tracklist.setVisible(true); tracklist.render(true); });

  const tracklist = new TrackList(byKind, (kind, id) => { select(kind, id); flyToItem(kind, id); });
  tracklist.render(true);
  const handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  handler.setInputAction((click) => {
    let hits = [];
    try { hits = scene.drillPick(click.position, 25, 16, 16); } catch (e) { hits = []; }
    if (!hits.length) { const p = scene.pick(click.position, 16, 16); if (Cesium.defined(p)) hits = [p]; }
    let best = null, bestD = Infinity, densityHit = null;
    for (const picked of hits) {
      if (!picked || !picked.primitive) continue;
      if (picked.id && picked.id.h3) { densityHit = densityHit || picked.id; continue; }
      for (const l of all) {
        if (l.map.get(picked.id) !== picked.primitive) continue;
        const w = Cesium.SceneTransforms.worldToWindowCoordinates(scene, picked.primitive.position);
        const d = w ? Cesium.Cartesian2.distance(w, click.position) : 1e9;
        if (d < bestD) { bestD = d; best = { kind: l.kind, id: picked.id }; }
      }
    }
    if (best) { select(best.kind, best.id); return; }
    if (densityHit) {
      const tip = $("pick-tip");
      tip.textContent = densityHit.density + " density: " + densityHit.count.toLocaleString() + " (H3 " + densityHit.h3 + ")";
      tip.classList.remove("hidden");
      clearTimeout(tip._t); tip._t = setTimeout(() => tip.classList.add("hidden"), 5000);
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  // user dragging away from a tracked entity cancels follow
  viewer.trackedEntityChanged.addEventListener(() => {
    if (following && !viewer.trackedEntity) { following = false; followBtn.textContent = "Follow: off"; followBtn.classList.remove("on"); }
  });

  // ---- Search ----
  const sIn = $("search"), sOut = $("results");
  const matches = () => {
    const q = sIn.value.trim().toLowerCase();
    if (!q) return [];
    const out = [];
    for (const l of [air, ships]) {
      for (const it of l.visibleItems()) {
        const hay = [it.name, it.id, it.type_code, it.operator].map((v) => String(v == null ? "" : v).toLowerCase());
        if (hay.some((h) => h.includes(q))) { out.push({ kind: l.kind, it }); if (out.length >= 50) return out; }
      }
    }
    return out;
  };
  const go = (m) => { select(m.kind, m.it.id); flyToItem(m.kind, m.it.id); sOut.classList.add("hidden"); };
  sIn.addEventListener("input", () => {
    const m = matches().slice(0, 8);
    sOut.innerHTML = m.map((x, i) => '<div data-i="' + i + '">' + esc(x.it.name || x.it.id) + " <small>" + esc(x.kind) + " · " + esc(x.it.type_code || "") + " " + esc(x.it.operator || "") + "</small></div>").join("");
    sOut.classList.toggle("hidden", !m.length);
    sOut._m = m;
  });
  sOut.addEventListener("click", (e) => { const d = e.target.closest("div[data-i]"); if (d && sOut._m) go(sOut._m[+d.dataset.i]); });
  sIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const m = matches(); if (m.length) go(m[0]); }
    else if (e.key === "Escape") sOut.classList.add("hidden");
  });

  // ---- Feature detection: older backends lack the v2 routes; disable those controls ----
  const disable = (ids, why) => {
    for (const id of ids) { const el = $(id); if (!el) continue; el.disabled = true; (el.closest("label") || el).title = why; }
  };
  fetchJson("/openapi.json", 8000).then((spec) => {
    const paths = Object.keys((spec && spec.paths) || {});
    if (!paths.length) return;
    const has = (p) => paths.some((x) => x.startsWith(p));
    if (!has("/api/density")) disable(["d-aircraft", "d-ships", "d-fires", "d-quakes", "d-res"], "Not supported by this backend version");
    if (!has("/api/tracks")) disable(["t-miltrails"], "Not supported by this backend version");
    if (!has("/api/snapshot")) { disable(["tl-live", "tl-play", "tl-speed", "tl-slider"], "Replay not supported by this backend version"); $("timeline").title = "Replay not supported by this backend version"; }
  }).catch(() => { /* openapi unavailable: leave controls enabled */ });

  // ---- Replay (time slider) ----
  let replayBusy = false, replayPending = null;
  async function renderReplay(at) {
    if (replayBusy) { replayPending = at; return; }
    replayBusy = true;
    try {
      const iso = encodeURIComponent(at.toISOString());
      const [a, sh, fi, qk] = await Promise.all([
        fetchJson("/api/snapshot?kind=air&window_min=10&at=" + iso),
        fetchJson("/api/snapshot?kind=sea&window_min=30&at=" + iso),
        fetchJson("/api/layers/fires?hours=24&until=" + iso),
        fetchJson("/api/layers/quakes?hours=24&until=" + iso),
      ]);
      if (!replayAt) return; // went live meanwhile
      for (const [k, d] of [["aircraft", a], ["ships", sh], ["fires", fi], ["quakes", qk]]) {
        const items = Array.isArray(d && d.items) ? d.items : [];
        byKind[k].sync(items); setCount(k, items.length);
      }
      if (selected) refreshSelected();
    } catch (e) { onErr("replay", String((e && e.message) || e)); }
    finally {
      replayBusy = false;
      onErr("replay", null);
      if (replayPending && replayAt) { const n = replayPending; replayPending = null; renderReplay(n); }
    }
  }
  new Timeline((at) => {
    replayAt = at;
    if (at) renderReplay(at);
    else { replayPending = null; ticks.forEach((t) => t().catch(() => {})); }
  });

  // ---- WebSocket live push (falls back to polling while closed) ----
  live = new LiveSocket((m) => {
    if (replayAt || !m) return;
    if (m.type === "snapshot") { air.sync(m.aircraft || []); ships.sync(m.ships || []); }
    else if (m.type === "delta") {
      air.upsert(m.aircraft || []); ships.upsert(m.ships || []);
      const rm = m.removed || [];
      air.removeIds(rm); ships.removeIds(rm);
    } else return;
    setCount("aircraft", air.items.size); setCount("ships", ships.items.size);
    if (selected && (selected.kind === "aircraft" || selected.kind === "ships")) { refreshSelected(); trailSelected(false); }
  });

  // ---- Polling ----
  const ticks = [
    feed("aircraft", "/api/layers/aircraft", air), feed("ships", "/api/layers/ships", ships),
    feed("fires", "/api/layers/fires?hours=24", fires), feed("quakes", "/api/layers/quakes?hours=24", quakes),
  ];
  poll("status", 5000, async () => { lastStatus = await fetchJson("/api/status", 8000); }, onErr);
  poll("aircraft", 10000, ticks[0], onErr);
  poll("ships", 30000, ticks[1], onErr);
  poll("fires", 300000, ticks[2], onErr);
  poll("quakes", 300000, ticks[3], onErr);
  // refresh relative "last seen" in the details panel
  setInterval(() => { if (selected) { refreshSelected(); if (!replayAt) trailSelected(false); } }, 5000);
}

main().catch((e) => { console.error(e); const s = document.getElementById("sources"); if (s) s.textContent = "UI error: " + e.message; });
