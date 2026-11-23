import { fetchJson, poll } from "/static/js/api.js";
import { AircraftLayer, ShipLayer, FireLayer, QuakeLayer } from "/static/js/layers.js";
import { TrackList } from "/static/js/tracklist.js";
import { Trails } from "/static/js/trails.js";
import { DensityLayer } from "/static/js/density.js";
import { Timeline } from "/static/js/timeline.js";
import { LiveSocket } from "/static/js/livesocket.js";
import { ReferenceLayers, REF_IDS, BASE_CHIPS, refDetails, airportRunwaysHtml } from "/static/js/reference.js";
import { $, esc } from "/static/js/ui.js";
import { createViewer, setupImagery, applyConfig, preloadBaseTiles } from "/static/js/theme/globe.js";
import { BASE_URL } from "/static/js/layers.js";
import { StatusBar, startClock } from "/static/js/panels/topbar.js";
import { LayerTree } from "/static/js/panels/layertree.js";
import { setupHud, setupFps } from "/static/js/panels/hud.js";
import { Inspector } from "/static/js/panels/inspector.js";
import { AlertCenter } from "/static/js/panels/alerts.js";
import { Palette } from "/static/js/palette.js";
import { Reticle } from "/static/js/reticle.js";
import { EventLayer, EventFeed, STATUSES } from "/static/js/events.js";
import { EventList } from "/static/js/eventlist.js";
import { EVENT_CATS, EVENT_STATUS } from "/static/js/symbology.js";
import { refreshEventStats } from "/static/js/panels/evstats.js";
import { NuclearLayer, NUC_GROUPS, nuclearDetails } from "/static/js/nuclear.js";
import { MovementsLayer, MOV_ROLES, MOV_KINDS, movementDetails } from "/static/js/movements.js";
import { WebcamLayer, renderNearbyCameras } from "/static/js/webcams.js";
import { renderEventImagery } from "/static/js/imagery.js";
import { initLicences } from "/static/js/licences.js";
import { CarrierLayer } from "/static/js/naval.js";
import { HistoricalLayer, historicalDetails } from "/static/js/historical.js";
import { Models3D, VectorLeaders, enableIonExtras, toggleBuildings } from "/static/js/models3d.js";
import { Weather, setCinematic, sunTime } from "/static/js/weather.js";
import { Dossier } from "/static/js/browser.js";
import { installFireScore } from "/static/js/firescore.js";

const errors = {};
let lastStatus = null;

async function loadConfig() {
  try { return await fetchJson("/api/config", 8000); } catch (e) { return { cesium_ion_token: null }; }
}

async function main() {
  initLicences();
  startClock();
  if (typeof Cesium === "undefined") { $("sources").textContent = "CesiumJS failed to load"; return; }
  // base imagery first: warm level 0-2 tiles, build the viewer + base layer before anything
  // else, and let /api/config (ion terrain) arrive whenever it does
  preloadBaseTiles(BASE_URL);
  const viewer = createViewer(null);
  const scene = viewer.scene;
  const redraw = () => scene.requestRender();
  setupImagery(viewer);
  const cfgP = loadConfig();
  cfgP.then((cfg) => applyConfig(viewer, cfg));
  const hud = setupHud(viewer);
  const fps = setupFps(viewer);
  const tree = new LayerTree();
  const statusBar = new StatusBar();

  // ---- Data layers ----
  const air = new AircraftLayer(scene), ships = new ShipLayer(scene), fires = new FireLayer(scene), quakes = new QuakeLayer(scene), events = new EventLayer(scene);
  const all = [air, ships, fires, quakes, events];
  for (const l of all) l.onChange = redraw;
  const byKind = { aircraft: air, ships, fires, quakes, events };

  // ---- 3D level of detail: glTF models near the camera + velocity leader lines ----
  const models = new Models3D(viewer, {
    hideBillboard: (kind, id) => { const p = byKind[kind].map.get(id); if (p) p.show = false; },
    showBillboard: (kind, id) => { const l = byKind[kind], p = l.map.get(id); if (p) p.show = l.filter(l.items.get(id)); },
  });
  const leaders = new VectorLeaders(viewer, { kinematics: models.kinematics });
  // after every aircraft / ship sync (REST, websocket, replay), filter change or layer toggle:
  // Layer.upsert/refilter re-show billboards, update() hides the ones drawn in 3D again
  const sync3d = () => {
    const a = air.visibleItems(), s = ships.visibleItems();
    models.update(a, s);
    leaders.update(a, "aircraft"); leaders.update(s, "ships");
  };
  models.setEnabled($("t-models3d").checked);
  leaders.setHorizonMinutes(+$("t-leader-min").value);
  leaders.setVisible($("t-leaders").checked);
  $("t-models3d").addEventListener("change", (e) => { models.setEnabled(e.target.checked); sync3d(); });
  $("t-leaders").addEventListener("change", (e) => { leaders.setVisible(e.target.checked); sync3d(); });
  $("t-leader-min").addEventListener("change", (e) => leaders.setHorizonMinutes(+e.target.value));
  // OSM Buildings load lazily on first use (needs the Cesium ion token from /api/config)
  let ion = null, ionP = null;
  $("t-buildings").addEventListener("change", async (e) => {
    const box = e.target, on = box.checked;
    if (!ion && on) {
      ionP = ionP || cfgP.then((cfg) => enableIonExtras(viewer, cfg && cfg.cesium_ion_token, { buildings: true }));
      ion = await ionP;
      if (!ion || !ion.tileset) {
        ionP = null; ion = null; box.checked = false;
        box.closest("label").title = "3D buildings need a Cesium ion token (CESIUM_ION_TOKEN) and network access to ion";
        return;
      }
    }
    toggleBuildings(ion, box.checked);
  });
  // ---- weather & lighting ----
  const weather = new Weather(viewer);
  weather.onStatus = (st) => {
    const el = $("wx-clouds-n");
    el.textContent = st.clouds ? String(st.clouds) : "";
    el.closest("label").title = st.error ? "3D clouds: " + st.error : "Volumetric clouds from current cloud cover (Open-Meteo); grid " + (st.step || "-") + " deg";
  };
  $("t-wx-ir").addEventListener("change", (e) => weather.setIR(e.target.checked));
  $("t-wx-radar").addEventListener("change", (e) => weather.setRadar(e.target.checked));
  $("t-wx-clouds").addEventListener("change", (e) => weather.setClouds(e.target.checked));
  const applyCinematic = () => {
    const on = $("t-cinematic").checked;
    setCinematic(viewer, on);
    models.setShadows(on);
    if (ion && ion.tileset) ion.tileset.shadows = on ? Cesium.ShadowMode.ENABLED : Cesium.ShadowMode.DISABLED;
  };
  $("t-cinematic").addEventListener("change", applyCinematic);
  const applySun = () => sunTime(viewer, $("t-sun").value || null);
  $("t-sun").addEventListener("change", applySun);
  viewer.camera.moveEnd.addEventListener(() => { if ($("t-sun").value !== "") applySun(); });

  // entering 2.5D switches the buildings on (once; the user can still turn them off)
  hud.onEnter25 = () => {
    const box = $("t-buildings");
    if (!box.checked) { box.checked = true; box.dispatchEvent(new Event("change")); }
  };

  const applyFilters = () => {
    air.filter = (it) => !$("t-aircraft-hp").checked || !!(it && it.high_priority);
    ships.filter = (it) => !$("t-ships-mil").checked || !!(it && it.is_mil);
    air.refilter(); ships.refilter();
    sync3d();
  };
  applyFilters();
  for (const k of ["aircraft", "ships", "fires", "quakes", "events"]) $("t-" + k).addEventListener("change", (e) => { byKind[k].show = e.target.checked; if (k === "aircraft" || k === "ships") sync3d(); });
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
  const feed = (kind, url, layer) => async (force) => {
    if (replayAt || (force !== true && (kind === "aircraft" || kind === "ships") && wsLive())) return;
    const data = await fetchJson(url);
    const items = Array.isArray(data && data.items) ? data.items : [];
    layer.sync(items);
    if (kind === "aircraft" || kind === "ships") sync3d();
    setCount(kind, items.length, data && data.truncated);
    if (kind === "aircraft") alerts.aircraft(items);
    if (kind === "quakes") alerts.quakes(items);
    if (selected && selected.kind === kind) refreshSelected();
  };
  const trailSelected = (force) => {
    if (!selected || !isTrack(selected.kind) || !trailOn) return;
    trails.showSelected(selected.kind, selected.id, byKind[selected.kind].items.get(selected.id), force);
  };

  // ---- Selection / inspector ----
  const hooks = { nearNuclear: () => null }; // filled once the nuclear layer exists
  let selected = null, following = false, detail = null, trailOn = true, refSel = null;
  const pins = new Set(JSON.parse(localStorage.getItem("noctua.pins") || "[]"));
  const savePins = () => localStorage.setItem("noctua.pins", JSON.stringify([...pins]));
  const isTrack = (k) => k === "aircraft" || k === "ships";
  const fx = { setTarget() {} }; // replaced by the selection reticle when available
  const buttons = () => {
    const t = !!(selected && isTrack(selected.kind));
    inspector.setButtons({ follow: t, following, trail: t, trailOn, pin: t, pinned: t && pins.has(selected.id) });
  };

  // Follow camera. Every rendered frame (preRender, the same instant the 3D models are placed)
  // it re-anchors the camera's reference frame on the track's smoothed, dead-reckoned position
  // and keeps the camera's local offset, so the user can still orbit / zoom around the target
  // (what Cesium's EntityView does, but in lockstep with the model; viewer.trackedEntity never
  // engaged for a graphics-less entity and moved only on data refreshes).
  const follow = { unPre: null, flight: false, enu: new Cesium.Matrix4(), local: new Cesium.Cartesian3(), pos: new Cesium.Cartesian3() };
  const followTarget = (result) => {
    if (!selected) return null;
    const p = models.positionOf(selected.kind, selected.id, result) || byKind[selected.kind].positionOf(selected.id);
    return p ? Cesium.Cartesian3.clone(p, result) : null;
  };
  function lockFollow() {
    const cam = viewer.camera;
    follow.flight = false;
    if (!following || follow.unPre) return;
    const p = followTarget(follow.pos);
    if (!p) { setFollow(false); return; }
    // enter the target's local frame keeping the current camera pose
    Cesium.Transforms.eastNorthUpToFixedFrame(p, undefined, follow.enu);
    const inv = Cesium.Matrix4.inverseTransformation(follow.enu, new Cesium.Matrix4());
    const localPos = Cesium.Matrix4.multiplyByPoint(inv, cam.positionWC, new Cesium.Cartesian3());
    cam.lookAtTransform(follow.enu, localPos);
    follow.unPre = viewer.scene.preRender.addEventListener(() => {
      const t = followTarget(follow.pos);
      if (!t) return; // track gone for now: hold the last frame
      Cesium.Cartesian3.clone(cam.position, follow.local); // offset in the previous local frame
      Cesium.Transforms.eastNorthUpToFixedFrame(t, undefined, follow.enu);
      cam.lookAtTransform(follow.enu, follow.local);
    });
  }
  function unlockFollow() {
    if (follow.unPre) { follow.unPre(); follow.unPre = null; }
    if (follow.flight) { viewer.camera.cancelFlight(); follow.flight = false; }
    viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
  }
  function setFollow(on) {
    const was = following;
    following = !!(on && selected && isTrack(selected.kind));
    if (was && !following) unlockFollow();
    if (following && !was) {
      const st = models.kinematics.get(selected.kind, selected.id);
      const FLY_S = 1.4;
      // fly to where the track will be when the flight lands, then lock on
      const s = st && models.kinematics.predict(st, Date.now() + FLY_S * 1000);
      const dest = s ? Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.altM) : followTarget(new Cesium.Cartesian3());
      if (!dest) following = false;
      else {
        const ship = selected.kind === "ships";
        const range = Math.min(viewer.camera.positionCartographic.height * 0.6, ship ? 1800 : 3200);
        follow.flight = true;
        viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(dest, ship ? 60 : 40), {
          offset: new Cesium.HeadingPitchRange(viewer.camera.heading, Cesium.Math.toRadians(ship ? -28 : -18), Math.max(range, ship ? 600 : 900)),
          duration: FLY_S,
          complete: lockFollow,
          cancel: () => { follow.flight = false; if (following) setFollow(false); },
        });
      }
    }
    models.setPinned(following ? selected.kind : null, following ? selected.id : null);
    models.setFrameSync(following);
    viewer.setContinuous("follow", following);
    buttons();
  }
  const withExtra = (it) => (detail && detail.id === it.id && detail.track ? { ...it, extra: detail.track.extra || {} } : it);
  function refreshSelected() {
    if (!selected) return;
    const it = byKind[selected.kind].items.get(selected.id);
    if (!it) return;
    if (isTrack(selected.kind)) { inspector.track(selected.kind, it, detail && detail.id === selected.id ? detail : null); alerts.squawk(withExtra(it)); }
    else if (selected.kind === "events") inspector.event(it, detail && detail.id === selected.id ? detail : null, hooks.nearNuclear(it));
    else inspector.observation(selected.kind, it);
  }
  async function loadDetail(kind, id) {
    detail = null;
    if (!isTrack(kind) && kind !== "events") return;
    try {
      const d = await fetchJson((kind === "events" ? "/api/events/" : "/api/track/") + encodeURIComponent(id), 8000);
      if (selected && selected.id === id) { detail = d; refreshSelected(); }
    } catch (e) { /* older backend or unknown track: the list item is enough */ }
  }
  // module-rendered selections (webcam, carrier hypothesis) own timers / labels: release them
  const mods = { wc: null, naval: null };
  const releaseModules = () => { if (mods.wc) { mods.wc.closeInspector(); mods.wc.clearSelection(); } };
  let nearFor = null; // event id whose "cameras nearby" strip is showing
  const clearNear = () => { nearFor = null; $("dz-extra").innerHTML = ""; };
  function select(kind, id) {
    setFollow(false);
    releaseModules();
    if (!(kind === "events" && nearFor === id)) clearNear();
    refSel = null;
    selected = { kind, id };
    inspector.points = null;
    refreshSelected();
    buttons();
    loadDetail(kind, id);
    tracklist.setSelected(id);
    evList.setSelected(kind === "events" ? id : null);
    fx.setTarget(() => (selected ? byKind[selected.kind].positionOf(selected.id) : null), kind);
    trails.clearSelected();
    trailSelected(true);
    if (kind === "events" && nearFor !== id) showNearbyCameras(id);
  }
  // "Cameras near this event": rendered once per selection, outside the 5 s-refreshed dossier
  function showNearbyCameras(id) {
    const it = events.items.get(id);
    if (!it) return;
    nearFor = id;
    const root = $("dz-extra");
    root.innerHTML = "";
    const imBox = root.appendChild(document.createElement("div")), box = root.appendChild(document.createElement("div"));
    renderEventImagery(id, imBox).catch(() => {});
    if (it.lat == null || it.precision === "country") return; // a country centroid has no meaningful neighbours
    renderNearbyCameras(it.lat, it.lon, box, { onSelect: (w) => showWebcam(w) })
      .then((items) => { if (nearFor === id && !items.length) box.innerHTML = '<div class="wc-near-h">Cameras near this event</div><div class="wc-loading">None within 25 km</div>'; })
      .catch(() => {});
  }
  function clearSelection() {
    setFollow(false); selected = null; refSel = null; detail = null;
    releaseModules(); clearNear();
    trails.clearSelected(); tracklist.setSelected(null); evList.setSelected(null); fx.setTarget(null);
    inspector.hide();
  }
  const flyTo = (lat, lon, alt) => viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(lon, lat, alt), duration: 1.6 });
  function flyToItem(kind, id) {
    const it = byKind[kind].items.get(id);
    if (it) flyTo(it.lat, it.lon, kind === "aircraft" ? 4e5 : kind === "ships" ? 2e5 : kind === "events" ? (it.precision === "country" ? 3e6 : it.precision === "admin1" ? 1.2e6 : 4e5) : 1.5e6);
  }
  const inspector = new Inspector({
    close: clearSelection,
    fly: () => {
      if (refSel) flyTo(refSel.item.lat, refSel.item.lon, /airport|runway/.test(refSel.kind) ? 8e4 : refSel.kind === "webcam" ? 2.5e4 : refSel.kind === "carrier" ? 2.5e6 : refSel.kind === "corridor" ? 4e6 : refSel.kind === "surge" ? 2.5e6 : 4e5);
      else if (selected) flyToItem(selected.kind, selected.id);
    },
    follow: () => setFollow(!following),
    trail: () => { trailOn = !trailOn; if (trailOn) trailSelected(true); else trails.clearSelected(); buttons(); },
    pin: () => {
      if (!selected) return;
      if (pins.has(selected.id)) pins.delete(selected.id); else pins.add(selected.id);
      savePins(); tracklist.dirty = true; tracklist.render(true); buttons();
    },
  });
  trails.onPoints = (key, pts) => { if (selected && key === selected.kind + ":" + selected.id) inspector.setTrail(pts); };
  installFireScore({ viewer, fires, inspector }); // fire conflict score: style, filter, dossier, front line

  // ---- Alerts ----
  const alerts = new AlertCenter((t) => {
    if (t.kind && byKind[t.kind] && byKind[t.kind].items.has(t.id)) { select(t.kind, t.id); flyToItem(t.kind, t.id); }
    else if (t.lat != null) flyTo(t.lat, t.lon, 1.2e6);
  });
  const renderAlerts = () => {
    const c = $("alerts-count");
    const n = alerts.unread ? String(Math.min(99, alerts.unread)) : "";
    if (c.textContent !== n) c.textContent = n;
    c.classList.toggle("hot", alerts.unread > 0 && alerts.list.slice(0, alerts.unread).some((a) => a.sev === "high"));
    if (tracklist && tracklist.pane === "alerts") $("alerts-pane").innerHTML = alerts.html();
  };
  alerts.onChange = renderAlerts;
  $("alerts-pane").addEventListener("click", (e) => { const li = e.target.closest("li[data-n]"); if (li) alerts.pick(+li.dataset.n); });
  statusBar.onTransition = (n, from, to, s) => alerts.source(n, from, to, s);

  const tracklist = new TrackList(byKind, (kind, id) => { select(kind, id); flyToItem(kind, id); }, {
    pins, onPane: (p) => { if (p === "alerts") { alerts.unread = 0; renderAlerts(); } if (p === "events") evList.render(true); if (p === "moves") { moves.ensurePanel(); renderMoves(); } },
  });
  const evList = new EventList(events, (id) => { select("events", id); flyToItem("events", id); }, () => tracklist.pane === "events" && tracklist.open && !$("tracks").classList.contains("hidden"));
  tracklist.render(true);

  // ---- Picking ----
  const handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  handler.setInputAction((click) => {
    let hits = [];
    try { hits = scene.drillPick(click.position, 8, 14, 14); } catch (e) { hits = []; }
    if (!hits.length) { const p = scene.pick(click.position, 14, 14); if (Cesium.defined(p)) hits = [p]; }
    let best = null, bestD = Infinity, densityHit = null, refHit = null;
    // carrier hypotheses sit on top of everything else, then public webcams
    for (const picked of hits) if (picked && picked.id && (picked.id.navalHyp || picked.id.navalIndicator) && mods.naval.pick(picked)) return;
    for (const picked of hits) if (picked && picked.id && (picked.id.webcam || picked.id.webcamCluster)) { mods.wc.pick(picked); return; }
    for (const picked of hits) if (picked && picked.id && picked.id.histCell) { histLayer.zoomCell(picked.id.histCell); return; }
    for (const picked of hits) {
      if (!picked || !picked.primitive) continue;
      if (picked.id && picked.id.ref) { refHit = refHit || picked.id; continue; }
      if (picked.id && picked.id.h3) { densityHit = densityHit || picked.id; continue; }
      if (picked.id && picked.id.models3d) { // a 3D model stands in for its (hidden) billboard
        if (bestD > 0 && byKind[picked.id.kind] && byKind[picked.id.kind].items.has(picked.id.id)) { bestD = 0; best = { kind: picked.id.kind, id: picked.id.id }; }
        continue;
      }
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

  // ---- Reference geography ----
  const refLayers = new ReferenceLayers(viewer, null);
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
    releaseModules(); clearNear();
    selected = null; detail = null; trails.clearSelected(); tracklist.setSelected(null); evList.setSelected(null);
    refSel = ref;
    const it = ref.item || {};
    if (it.lat != null) { const p = Cesium.Cartesian3.fromDegrees(it.lon, it.lat); fx.setTarget(() => p, "ref"); } else fx.setTarget(null);
    if (ref.kind === "historical") {
      const [ht, hb] = historicalDetails(it);
      inspector.reference(ref, ht, hb); buttons();
      fetchJson("/api/events/" + encodeURIComponent(it.id), 8000).then((d) => {
        if (refSel !== ref) return;
        const [t2, b2] = historicalDetails(it, d); inspector.reference(ref, t2, b2);
      }).catch(() => {});
      return;
    }
    const [title, body] = ref.kind === "nuclear" ? nuclearDetails(it) : MOV_KINDS[ref.kind] ? movementDetails(ref) : refDetails(ref);
    inspector.reference(ref, title, body);
    buttons();
    if (ref.kind === "airport" || ref.kind === "medairport") {
      const rw = await airportRunwaysHtml(it);
      if (refSel === ref && rw) { const [, b2] = refDetails(ref, rw); inspector.reference(ref, title, b2); }
    }
  }

  // ---- Nuclear sites (reference) ----
  const nuclear = new NuclearLayer(viewer);
  hooks.nearNuclear = (it) => nuclear.near(it.lat, it.lon, 50);
  const nucOn = new Set(Object.keys(NUC_GROUPS));
  $("nuc-chips").innerHTML = Object.entries(NUC_GROUPS).map(([k, [l, c]]) => '<button class="chip on" data-k="' + k + '" style="--c:' + c + '">' + l + "</button>").join("");
  $("nuc-chips").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    if (b.classList.contains("on")) nucOn.add(b.dataset.k); else nucOn.delete(b.dataset.k);
    nuclear.setGroups(nucOn);
  });
  nuclear.onLoad = (items) => { $("c-nuclear").textContent = items.length.toLocaleString(); if (selected && selected.kind === "events") refreshSelected(); };
  $("r-nuclear").addEventListener("change", (e) => nuclear.setOn(e.target.checked).then(() => {
    if (nuclear.unavailable) { e.target.checked = false; e.target.closest("label").title = "Nuclear sites not supported by this backend version"; }
  }));
  $("r-nuc-rings").addEventListener("change", (e) => nuclear.setRings(e.target.checked));
  setTimeout(() => nuclear.ensure().catch(() => {}), 4000); // loaded for event proximity tags even when hidden

  // ---- Module selections (webcam / carrier hypothesis) ----
  function showModule(kind, item, eyebrow, tint, render) {
    setFollow(false);
    if (kind === "webcam") mods.wc.closeInspector(); else releaseModules();
    clearNear();
    selected = null; detail = null; trails.clearSelected(); tracklist.setSelected(null); evList.setSelected(null);
    refSel = { kind, item };
    if (item.lat != null) { const p = Cesium.Cartesian3.fromDegrees(item.lon, item.lat); fx.setTarget(() => p, "ref"); } else fx.setTarget(null);
    render(inspector.module(eyebrow, tint));
    buttons();
  }
  function showWebcam(w) {
    if (!w) return;
    showModule("webcam", w, "Public webcam · " + (w.source || "open data"), "#5ee3ea", (el) => mods.wc.renderInspector(w, el));
  }

  // ---- Public webcams (sensors) ----
  const wc = mods.wc = new WebcamLayer(viewer, { onSelect: (w) => showWebcam(w) });
  const wcOn = new Set();
  let wcAll = [];
  const wcCount = () => {
    const n = wcAll.filter((s) => wcOn.has(s.id)).reduce((a, s) => a + (s.count || 0), 0);
    $("c-webcams").textContent = wcAll.length ? n.toLocaleString() : "";
    $("g-sensors-n").textContent = $("s-webcams").checked ? n.toLocaleString() : "–";
  };
  const loadWcSources = () => fetchJson("/api/webcams/sources", 15000).then((j) => {
    wcAll = (j && j.items) || [];
    wcOn.clear();
    for (const s of wcAll) if (s.enabled && s.count) wcOn.add(s.id);
    $("wc-chips").innerHTML = wcAll.map((s) => {
      const ok = !!(s.enabled && s.count);
      const tip = s.name + " · " + (ok ? s.count + " cameras · " + s.licence : s.error || "no cameras");
      return '<button class="chip' + (ok ? " on" : "") + '" data-k="' + esc(s.id) + '" style="--c:#5ee3ea" title="' + esc(tip) + '"' + (ok ? "" : " disabled") + ">" + esc(s.id) + "</button>";
    }).join("");
    wcCount();
  });
  $("wc-chips").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b || b.disabled) return;
    b.classList.toggle("on");
    if (b.classList.contains("on")) wcOn.add(b.dataset.k); else wcOn.delete(b.dataset.k);
    wc.setSources(wcOn.size ? wcOn : ["__none__"]); // an empty set would mean "all sources"
    wcCount();
  });
  $("s-webcams").addEventListener("change", (e) => {
    wc.setVisible(e.target.checked);
    if (!e.target.checked && refSel && refSel.kind === "webcam") clearSelection();
    if (e.target.checked && !wcAll.length) loadWcSources().catch(() => { e.target.closest("label").title = "Webcams not supported by this backend version"; });
    wcCount();
  });
  loadWcSources().catch(() => {});
  viewer.camera.moveEnd.addEventListener(() => { if (wc.visible) wc.refreshForView().catch(() => {}); });

  // ---- Carrier strike group activity (hypotheses, never positions) ----
  const naval = mods.naval = new CarrierLayer(viewer, {
    onSelect: (it) => {
      if (it && it.indicator) showModule("carrier", it.indicator, "Naval indicator · hypothesis input", "#f2ad3d", (el) => naval.renderInspector(it, el));
      else if (it) showModule("carrier", it, "Carrier activity · hypothesis", "#5ee3ea", (el) => naval.renderInspector(it, el));
    },
  });
  const navalCounts = () => {
    const on = $("n-carriers").checked;
    $("c-carriers").textContent = on ? String(naval.hyps.size) : "";
    $("c-indicators").textContent = on ? naval.points.size.toLocaleString() : "";
    $("g-naval-n").textContent = on ? String(naval.hyps.size) : "–";
  };
  const navalRefresh = () => naval.refresh().then(() => { $("n-carriers").closest("label").title = ""; navalCounts(); }, (e) => {
    $("n-carriers").closest("label").title = "Carrier hypotheses unavailable: " + ((e && e.message) || e);
  });
  naval.setIndicatorsVisible($("n-indicators").checked); naval.setSurfaceVisible($("n-surface").checked);
  $("n-carriers").addEventListener("change", (e) => {
    naval.visible = e.target.checked; naval._apply(); // not setVisible(): its refresh swallows errors
    if (e.target.checked) navalRefresh(); else { navalCounts(); if (refSel && refSel.kind === "carrier") clearSelection(); }
  });
  $("n-indicators").addEventListener("change", (e) => { naval.setIndicatorsVisible(e.target.checked); });
  $("n-surface").addEventListener("change", (e) => naval.setSurfaceVisible(e.target.checked));
  setInterval(() => { if (naval.visible) navalRefresh(); }, 10 * 60e3);

  // ---- Military movements ----
  const moves = new MovementsLayer(viewer);
  const roleOn = new Set(Object.keys(MOV_ROLES));
  $("mv-roles").innerHTML = Object.entries(MOV_ROLES).map(([k, [l, c]]) => '<button class="chip on" data-k="' + k + '" style="--c:' + c + '">' + l + "</button>").join("");
  $("mv-roles").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    if (b.classList.contains("on")) roleOn.add(b.dataset.k); else roleOn.delete(b.dataset.k);
    moves.setRoles(roleOn);
  });
  const renderMoves = () => {
    const d = moves.data;
    for (const k of ["corridors", "legs", "surges", "naval"]) { const el = $("c-" + k); if (el) el.textContent = moves.sig[k] ? d[k].length.toLocaleString() : ""; }
    const g = $("g-moves-n"); if (g) g.textContent = moves.sig.corridors || moves.sig.surges ? (d.corridors.length + d.surges.length).toLocaleString() : "–";
    const mc = $("moves-count"), t = d.surges.length ? String(d.surges.length) : "";
    if (mc.textContent !== t) { mc.textContent = t; mc.classList.toggle("hot", !!d.surges.length); }
    if (tracklist.pane === "moves") $("moves-pane").innerHTML = moves.panelHtml();
  };
  moves.onData = renderMoves;
  for (const k of ["corridors", "legs", "surges", "naval"]) $("m-" + k).addEventListener("change", (e) => moves.set(k, e.target.checked));
  $("moves-pane").addEventListener("click", (e) => {
    const r = e.target.closest(".mv-row");
    if (!r) return;
    const ref = moves.pick(r.dataset.k, +r.dataset.i);
    if (!ref) return;
    const it = ref.item;
    if (ref.kind === "corridor" && it.path) { const m = it.path[it.path.length >> 1]; it.lon = m[0]; it.lat = m[1]; }
    showRef(ref);
    if (it.lat != null) flyTo(it.lat, it.lon, ref.kind === "corridor" ? 4e6 : ref.kind === "surge" ? 2.5e6 : 6e5);
  });
  setTimeout(() => { moves.load("surges"); }, 8000); // tab badge for active surges

  // ---- News events: filters, feed, alerts ----
  const catLabel = (k) => EVENT_CATS[k][0].replace(/ \/ /, "/");
  $("ev-cats").innerHTML = Object.keys(EVENT_CATS).map((k) => '<button class="chip on" data-k="' + k + '" style="--c:' + EVENT_CATS[k][1] + '" title="' + EVENT_CATS[k][0] + '">' + catLabel(k) + "</button>").join("");
  $("ev-status").innerHTML = STATUSES.map((k) => '<button class="chip on" data-k="' + k + '">' + EVENT_STATUS[k] + "</button>").join("");
  const chipSet = (box, set) => $(box).addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    b.classList.toggle("on");
    if (b.classList.contains("on")) set.add(b.dataset.k); else set.delete(b.dataset.k);
    events.refilter(); tick_ev_count();
  });
  chipSet("ev-cats", events.cats); chipSet("ev-status", events.statuses);
  $("ev-conf").addEventListener("input", (e) => { events.minConf = +e.target.value; $("ev-conf-v").textContent = events.minConf.toFixed(2); events.refilter(); tick_ev_count(); });
  function tick_ev_count() {
    const n = events.visibleItems().length;
    setCount("events", n);
    const c = $("events-count"), t = n ? String(n > 999 ? "999+" : n) : "";
    if (c.textContent !== t) c.textContent = t;
  }
  const evFeed = new EventFeed(events, (items) => { tick_ev_count(); if (!replayAt) alerts.events([...events.items.values()]); if (selected && selected.kind === "events") refreshSelected(); });

  // ---- Historical conflict events (UCDP), default off ----
  const histLayer = new HistoricalLayer(viewer);
  histLayer.onData = () => {
    $("c-historical").textContent = histLayer.on ? histLayer.visibleN.toLocaleString() : "";
    $("hist-note").textContent = histLayer.note();
  };
  $("t-historical").addEventListener("change", (e) => {
    histLayer.setOn(e.target.checked).then(() => histLayer.onData(), () => { $("hist-note").textContent = histLayer.on ? "Historical events unavailable" : ""; });
    if (!e.target.checked) { histLayer.onData(); if (refSel && refSel.kind === "historical") clearSelection(); }
  });

  // ---- Search field opens the command palette ----
  const sIn = $("search");
  const openPalette = () => { if (ctx.palette) { const v = sIn.value; sIn.value = ""; sIn.blur(); ctx.palette.open(v); } };
  sIn.addEventListener("focus", openPalette);
  sIn.addEventListener("input", openPalette);

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
    if (!has("/api/snapshot")) { disable(["tl-live", "tl-play", "tl-speed", "tl-range", "tl-slider"], "Replay not supported by this backend version"); $("timeline").title = "Replay not supported by this backend version"; }
  }).catch(() => { /* openapi unavailable: leave controls enabled */ });

  // ---- Replay (time slider) ----
  let replayBusy = false, replayPending = null;
  async function renderReplay(at) {
    if (replayBusy) { replayPending = at; return; }
    replayBusy = true;
    try {
      const iso = encodeURIComponent(at.toISOString()), wh = replayWindowH();
      // tracks only exist for days the lake holds; a failed snapshot is "no data", not an error
      const none = { items: [] };
      const [a, sh, fi, qk] = await Promise.all([
        fetchJson("/api/snapshot?kind=air&window_min=10&at=" + iso).catch(() => none),
        fetchJson("/api/snapshot?kind=sea&window_min=30&at=" + iso).catch(() => none),
        fetchJson("/api/layers/fires?hours=" + wh + "&until=" + iso),
        fetchJson("/api/layers/quakes?hours=" + wh + "&until=" + iso),
      ]);
      if (!replayAt) return; // went live meanwhile
      for (const [k, d] of [["aircraft", a], ["ships", sh], ["fires", fi], ["quakes", qk]]) {
        const items = Array.isArray(d && d.items) ? d.items : [];
        byKind[k].sync(items); setCount(k, items.length, d && d.truncated);
      }
      sync3d();
      const noTracks = !(a.items && a.items.length) && !(sh.items && sh.items.length);
      $("tl-note").textContent = (noTracks ? "no track data at this time · " : "") + "fires/quakes: " + (wh >= 48 ? wh / 24 + " d" : wh + " h") + " before cursor";
      if (selected) refreshSelected();
    } catch (e) { onErr("replay", String((e && e.message) || e)); }
    finally {
      replayBusy = false;
      onErr("replay", null);
      if (replayPending && replayAt) { const n = replayPending; replayPending = null; renderReplay(n); }
    }
  }
  // observation window shown around the replay cursor grows with the range
  const replayWindowH = () => (timeline.span <= 7 * 1440 ? 24 : timeline.span <= 30 * 1440 ? 72 : 168);
  const timeline = new Timeline((at) => {
    replayAt = at;
    models.setReplay(!!at);
    events.setReplay(at); tick_ev_count();
    histLayer.setReplay(at, replayWindowH() * 3600e3);
    if (at) renderReplay(at);
    else { replayPending = null; $("tl-note").textContent = ""; ticks.forEach((t) => t(true).catch(() => {})); }
  }, (playing) => viewer.setContinuous("replay", playing), () => {
    histLayer.setSince(new Date(timeline.start())).catch(() => {});
  });
  histLayer.setSince(new Date(timeline.start()));

  // ---- WebSocket live push (falls back to polling while closed) ----
  live = new LiveSocket((m) => {
    if (replayAt || !m) return;
    if (m.type === "snapshot") { air.sync(m.aircraft || []); ships.sync(m.ships || []); }
    else if (m.type === "delta") {
      air.upsert(m.aircraft || []); ships.upsert(m.ships || []);
      const rm = m.removed || [];
      air.removeIds(rm); ships.removeIds(rm);
    } else return;
    sync3d();
    setCount("aircraft", air.items.size); setCount("ships", ships.items.size);
    alerts.aircraft(m.aircraft || []);
    if (selected && isTrack(selected.kind)) { refreshSelected(); trailSelected(false); }
  });

  // ---- Dossier (in-app browser) ----
  const dossier = new Dossier({ flyTo });
  // build the best dossier for the current selection
  async function intel() {
    const open = (spec) => dossier.open(spec);
    if (refSel) {
      const it = refSel.item || {}, k = refSel.kind;
      if (it.wikidata && /^Q\d+$/.test(it.wikidata)) return open({ type: "entity", qid: it.wikidata });
      if (/airport/.test(k) && (it.icao || it.ident)) return open({ type: "resolve", icao: it.icao || it.ident, q: it.name, lat: it.lat, lon: it.lon, label: it.name });
      if (it.name && it.lat != null) return open({ type: "resolve", q: it.name, lat: it.lat, lon: it.lon, label: it.name });
      if (it.lat != null) return open({ type: "nearby", lat: it.lat, lon: it.lon, label: it.name || "" });
      return;
    }
    if (!selected) { dossier.toggle(); return; }
    const it = byKind[selected.kind] && byKind[selected.kind].items.get(selected.id);
    if (!it) return;
    if (selected.kind === "events") {
      const d = detail && detail.id === selected.id ? detail : await fetchJson("/api/events/" + encodeURIComponent(selected.id), 8000).catch(() => null);
      const urls = ((d && d.documents) || []).map((x) => x.url).filter((u) => /^https?:\/\//.test(u || ""));
      for (const u of [...new Set(urls)].slice(0, 3)) dossier.openUrl(u, { focus: false });
      if (it.lat != null && it.precision !== "country") open({ type: "nearby", lat: it.lat, lon: it.lon, label: it.place_name || it.title || "" });
      return;
    }
    if (selected.kind === "ships" && it.name) return open({ type: "resolve", q: it.name, label: it.name });
    if (it.operator) open({ type: "resolve", q: it.operator, label: it.operator });
    if (it.lat != null) open({ type: "nearby", lat: it.lat, lon: it.lon, label: it.name || it.id });
  }
  $("d-intel").addEventListener("click", () => intel().catch((e) => console.warn("intel", e)));
  $("rail-dossier").addEventListener("click", () => dossier.toggle());

  // ---- Keyboard ----
  const ctx = { viewer, scene, air, ships, byKind, refLayers, nuclear, moves, tree, timeline, hud, fps, mods, models, leaders, histLayer, showWebcam, select, flyTo, flyToItem, clearSelection, showRef, inspector, setFollow: () => setFollow(!following) };
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "");
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if (ctx.palette.isOpen) ctx.palette.close(); else ctx.palette.open(); return; }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); ctx.palette.open(); }
    else if (e.key === "Escape") clearSelection();
    else if (e.key === "f" || e.key === "F") fps.toggle();
    else if (e.key === "h" || e.key === "H") hud.home();
    else if (e.key === "t" || e.key === "T") hud.toggleTilt();
    else if (e.key === "i" || e.key === "I") intel().catch((err) => console.warn("intel", err));
    else if (e.key === "b" || e.key === "B") dossier.toggle();
    else if (e.key === "[") tree.toggleCollapsed();
    else if (e.key === "l" || e.key === "L") timeline.goLive();
    else if (e.key === " " && e.target === document.body) { e.preventDefault(); timeline.togglePlay(); }
  });

  // ---- Polling ----
  const ticks = [
    feed("aircraft", "/api/layers/aircraft", air), feed("ships", "/api/layers/ships", ships),
    feed("fires", "/api/layers/fires?hours=24", fires), feed("quakes", "/api/layers/quakes?hours=24", quakes),
  ];
  poll("events", 60000, () => evFeed.tick(), onErr);
  poll("events-stats", 60000, () => refreshEventStats(), onErr);
  poll("status", 5000, async () => { lastStatus = await fetchJson("/api/status", 8000); }, onErr);
  poll("aircraft", 10000, ticks[0], onErr);
  poll("ships", 30000, ticks[1], onErr);
  poll("fires", 300000, ticks[2], onErr);
  poll("quakes", 300000, ticks[3], onErr);
  // refresh relative "last seen" in the inspector
  setInterval(() => { if (selected) { refreshSelected(); if (!replayAt) trailSelected(false); } }, 5000);
  // ---- Selection reticle + high-priority halos ----
  const reticle = new Reticle(scene);
  fx.setTarget = (fn, kind) => reticle.setTarget(fn, kind);
  let haloT = 0;
  const prevAir = air.onChange;
  air.onChange = () => { prevAir && prevAir(); clearTimeout(haloT); haloT = setTimeout(() => reticle.syncHalos(air), 300); };
  ctx.tracklist = tracklist;
  ctx.dossier = dossier;
  ctx.palette = new Palette(ctx);
  window.__osint = ctx;
  return ctx;
}

main().catch((e) => { console.error(e); const s = document.getElementById("sources"); if (s) s.textContent = "UI error: " + e.message; });
