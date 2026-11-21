import { fetchJson, poll } from "/static/js/api.js";
import { AircraftLayer, ShipLayer, FireLayer, QuakeLayer } from "/static/js/layers.js";
import { TrackList } from "/static/js/tracklist.js";
import { Trails } from "/static/js/trails.js";
import { DensityLayer } from "/static/js/density.js";
import { Timeline } from "/static/js/timeline.js";
import { LiveSocket } from "/static/js/livesocket.js";
import { ReferenceLayers, REF_IDS, BASE_CHIPS, refDetails, airportRunwaysHtml } from "/static/js/reference.js";
import { $, esc, detailsHtml } from "/static/js/ui.js";
import { createViewer, setupImagery } from "/static/js/theme/globe.js";
import { StatusBar, startClock } from "/static/js/panels/topbar.js";
import { LayerTree } from "/static/js/panels/layertree.js";
import { setupHud, setupFps } from "/static/js/panels/hud.js";

const errors = {};
let lastStatus = null;

async function loadConfig() {
  try { return await fetchJson("/api/config", 8000); } catch (e) { return { cesium_ion_token: null }; }
}

async function main() {
  startClock();
  if (typeof Cesium === "undefined") { $("sources").textContent = "CesiumJS failed to load"; return; }
  const cfg = await loadConfig();
  const viewer = createViewer(cfg);
  const scene = viewer.scene;
  const redraw = () => scene.requestRender();
  setupImagery(viewer);
  const hud = setupHud(viewer);
  const fps = setupFps(viewer);
  const tree = new LayerTree();
  const statusBar = new StatusBar();

  // ---- Data layers ----
  const air = new AircraftLayer(scene), ships = new ShipLayer(scene), fires = new FireLayer(scene), quakes = new QuakeLayer(scene);
  const all = [air, ships, fires, quakes];
  for (const l of all) l.onChange = redraw;
  const byKind = { aircraft: air, ships, fires, quakes };
  const applyFilters = () => {
    air.filter = (it) => !$("t-aircraft-hp").checked || !!(it && it.high_priority);
    ships.filter = (it) => !$("t-ships-mil").checked || !!(it && it.is_mil);
    air.refilter(); ships.refilter();
  };
  applyFilters();
  for (const k of ["aircraft", "ships", "fires", "quakes"]) $("t-" + k).addEventListener("change", (e) => { byKind[k].show = e.target.checked; });
  $("t-aircraft-hp").addEventListener("change", applyFilters);
  $("t-ships-mil").addEventListener("change", applyFilters);

  const setCount = (k, n, truncated) => tree.setCount(k, n, truncated);
  const onErr = (name, err) => {
    if (err) errors[name] = err; else delete errors[name];
    statusBar.render(lastStatus, errors);
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
    setCount(kind, items.length, data && data.truncated);
    if (selected && selected.kind === kind) refreshSelected();
  };
  const trailSelected = (force) => {
    if (!selected || (selected.kind !== "aircraft" && selected.kind !== "ships")) return;
    trails.showSelected(selected.kind, selected.id, byKind[selected.kind].items.get(selected.id), force);
  };

  // ---- Selection / details ----
  let selected = null, following = false, detail = null;
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
    viewer.setContinuous("follow", on && !!selected);
  }
  // Slim list items carry no `extra`; merge it from GET /api/track/{id} when available.
  const withDetail = (it) => {
    if (!detail || !selected || detail.id !== selected.id) return it;
    const tr = detail.track || {};
    return { ...tr, ...it, extra: { ...(tr.extra || {}), ...(it.extra || {}) }, source: detail.position && detail.position.source };
  };
  function refreshSelected() {
    if (!selected) return;
    const it = byKind[selected.kind].items.get(selected.id);
    if (!it) return;
    const [title, body] = detailsHtml(selected.kind, withDetail(it));
    if ($("d-title").textContent !== title) $("d-title").textContent = title;
    if ($("d-body")._h !== body) { $("d-body")._h = body; $("d-body").innerHTML = body; }
    if (following) {
      const p = byKind[selected.kind].positionOf(selected.id);
      if (p) tracker.position = p.clone();
    }
  }
  async function loadDetail(kind, id) {
    detail = null;
    if (kind !== "aircraft" && kind !== "ships") return;
    try {
      const d = await fetchJson("/api/track/" + encodeURIComponent(id), 8000);
      if (selected && selected.id === id) { detail = d; refreshSelected(); }
    } catch (e) { /* older backend or unknown track: list item is enough */ }
  }
  function select(kind, id) {
    setFollow(false);
    refSel = null;
    selected = { kind, id };
    $("right").classList.remove("hidden");
    followBtn.classList.toggle("hidden", kind !== "aircraft" && kind !== "ships");
    refreshSelected();
    loadDetail(kind, id);
    tracklist.setSelected(id);
    trails.clearSelected();
    trailSelected(true);
  }
  function clearSelection() {
    setFollow(false); selected = null; refSel = null; detail = null;
    trails.clearSelected(); tracklist.setSelected(null);
    $("right").classList.add("hidden");
  }
  function flyToItem(kind, id) {
    const it = byKind[kind].items.get(id);
    if (!it) return;
    const alt = (kind === "aircraft" ? 4e5 : kind === "ships" ? 2e5 : 1.5e6);
    viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(it.lon, it.lat, alt), duration: 1.8 });
  }
  $("d-fly").addEventListener("click", () => {
    if (refSel) viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(refSel.item.lon, refSel.item.lat, /airport|runway/.test(refSel.kind) ? 8e4 : 4e5), duration: 1.8 });
    else if (selected) flyToItem(selected.kind, selected.id);
  });
  followBtn.addEventListener("click", () => setFollow(!following));
  $("close-details").addEventListener("click", clearSelection);

  const tracklist = new TrackList(byKind, (kind, id) => { select(kind, id); flyToItem(kind, id); });
  tracklist.render(true);
  const handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  handler.setInputAction((click) => {
    let hits = [];
    try { hits = scene.drillPick(click.position, 8, 14, 14); } catch (e) { hits = []; }
    if (!hits.length) { const p = scene.pick(click.position, 14, 14); if (Cesium.defined(p)) hits = [p]; }
    let best = null, bestD = Infinity, densityHit = null, refHit = null;
    for (const picked of hits) {
      if (!picked || !picked.primitive) continue;
      if (picked.id && picked.id.ref) { refHit = refHit || picked.id; continue; }
      if (picked.id && picked.id.h3) { densityHit = densityHit || picked.id; continue; }
      for (const l of all) {
        if (l.map.get(picked.id) !== picked.primitive) continue;
        const w = Cesium.SceneTransforms.worldToWindowCoordinates(scene, picked.primitive.position);
        const d = w ? Cesium.Cartesian2.distance(w, click.position) : 1e9;
        if (d < bestD) { bestD = d; best = { kind: l.kind, id: picked.id }; }
      }
    }
    if (best) { select(best.kind, best.id); return; }
    if (refHit) { showRef(refHit); return; }
    if (densityHit) {
      const tip = $("pick-tip");
      tip.textContent = densityHit.density + " density: " + densityHit.count.toLocaleString() + " (H3 " + densityHit.h3 + ")";
      tip.classList.remove("hidden");
      clearTimeout(tip._t); tip._t = setTimeout(() => tip.classList.add("hidden"), 5000);
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  viewer.trackedEntityChanged.addEventListener(() => {
    if (following && !viewer.trackedEntity) { following = false; followBtn.textContent = "Follow: off"; followBtn.classList.remove("on"); viewer.setContinuous("follow", false); }
  });

  // ---- Reference geography ----
  const refLayers = new ReferenceLayers(viewer, null);
  let refSel = null;
  const baseOn = new Set(BASE_CHIPS.map((c) => c[0]));
  refLayers.baseTypes = baseOn;
  $("r-chips").innerHTML = BASE_CHIPS.map(([k, c]) => '<button class="chip on" data-k="' + k + '" style="--c:' + c + '">' + k + "</button>").join("");
  $("r-chips").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    if (b.classList.contains("on")) baseOn.add(b.dataset.k); else baseOn.delete(b.dataset.k);
    refLayers.setBaseTypes(baseOn);
  });
  for (const id of REF_IDS) {
    $(id).addEventListener("change", (e) => refLayers.setOn(id, e.target.checked));
    refLayers.state[id] = $(id).checked;
  }
  for (const id of REF_IDS) if ($(id).checked) refLayers.apply(id);
  async function showRef(ref) {
    setFollow(false);
    selected = null; detail = null; trails.clearSelected(); tracklist.setSelected(null);
    refSel = ref;
    $("right").classList.remove("hidden");
    followBtn.classList.add("hidden");
    const [title, body] = refDetails(ref);
    $("d-title").textContent = title;
    $("d-body").innerHTML = body; $("d-body")._h = null;
    if (ref.kind === "airport" || ref.kind === "medairport") {
      const rw = await airportRunwaysHtml(ref.item);
      if (refSel === ref && rw) { const [, b2] = refDetails(ref, rw); $("d-body").innerHTML = b2; }
    }
  }

  // ---- Search ----
  const sIn = $("search"), sOut = $("results");
  const matches = () => {
    const q = sIn.value.trim().toLowerCase();
    if (!q) return [];
    const out = [];
    for (const l of [air, ships]) {
      for (const it of l.visibleItems()) {
        const hay = [it.name, it.id, it.type_code, it.operator, it.reg].map((v) => String(v == null ? "" : v).toLowerCase());
        if (hay.some((h) => h.includes(q))) { out.push({ kind: l.kind, it }); if (out.length >= 50) return out; }
      }
    }
    return out;
  };
  const go = (m) => { select(m.kind, m.it.id); flyToItem(m.kind, m.it.id); sOut.classList.add("hidden"); };
  sIn.addEventListener("input", () => {
    const m = matches().slice(0, 8);
    sOut.innerHTML = m.map((x, i) => '<div data-i="' + i + '">' + esc(x.it.name || x.it.id) + " <small>" + esc(x.kind) + " · " + esc(x.it.type_code || "") + " " + esc(x.it.reg || x.it.operator || "") + "</small></div>").join("");
    sOut.classList.toggle("hidden", !m.length);
    sOut._m = m;
  });
  sOut.addEventListener("click", (e) => { const d = e.target.closest("div[data-i]"); if (d && sOut._m) go(sOut._m[+d.dataset.i]); });
  sIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const m = matches(); if (m.length) go(m[0]); }
    else if (e.key === "Escape") { sOut.classList.add("hidden"); sIn.blur(); }
  });

  // ---- Keyboard ----
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "");
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); sIn.focus(); sIn.select(); return; }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); sIn.focus(); }
    else if (e.key === "Escape") clearSelection();
    else if (e.key === "f" || e.key === "F") fps.toggle();
    else if (e.key === "h" || e.key === "H") hud.home();
    else if (e.key === "[") tree.toggleCollapsed();
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
    if (!has("/api/reference")) { disable([...REF_IDS], "Reference data not supported by this backend version"); for (const id of REF_IDS) refLayers.setOn(id, false); }
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
        byKind[k].sync(items); setCount(k, items.length, d && d.truncated);
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
    document.body.classList.toggle("replay", !!at);
    const pill = $("mode-pill");
    pill.classList.toggle("replay", !!at);
    $("mode-text").textContent = at ? "REPLAY" : "LIVE";
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
  window.__osint = { viewer, air, ships, refLayers };
}

main().catch((e) => { console.error(e); const s = document.getElementById("sources"); if (s) s.textContent = "UI error: " + e.message; });
