// Globe look + render budget: darkened imagery, atmosphere, graticule, day/night, overlays,
// request-render mode with an explicit "continuous" override (follow / replay play).
import { $ } from "/static/js/ui.js";
import { makeImagery } from "/static/js/layers.js";

const C = Cesium;

export function createViewer(cfg) {
  const opts = {
    baseLayer: false, geocoder: false, baseLayerPicker: false, timeline: false, animation: false,
    homeButton: false, navigationHelpButton: false, sceneModePicker: false, fullscreenButton: false, infoBox: false, selectionIndicator: false,
    requestRenderMode: true, maximumRenderTimeChange: Infinity,
  };
  // base imagery must not queue behind the default 6-requests-per-server throttle
  C.RequestScheduler.requestsByServer["server.arcgisonline.com:443"] = 18;
  const viewer = new C.Viewer("globe", opts);
  if (cfg) applyConfig(viewer, cfg);
  const scene = viewer.scene, globe = scene.globe;
  scene.requestRenderMode = true;
  scene.maximumRenderTimeChange = Infinity;
  // render budget
  scene.fxaa = false;
  if (scene.postProcessStages && scene.postProcessStages.fxaa) scene.postProcessStages.fxaa.enabled = false;
  globe.maximumScreenSpaceError = 2.5;
  globe.tileCacheSize = 300;
  const dpr = window.devicePixelRatio || 1;
  if (dpr > 1.5) viewer.resolutionScale = Math.min(1, 1.5 / dpr);
  // look: deep night-ocean base, atmosphere on
  globe.baseColor = C.Color.fromCssColorString("#07111c");
  globe.enableLighting = false;
  globe.showGroundAtmosphere = true;
  if (scene.skyAtmosphere) { scene.skyAtmosphere.show = true; scene.skyAtmosphere.brightnessShift = -0.25; scene.skyAtmosphere.saturationShift = -0.3; }
  scene.backgroundColor = C.Color.fromCssColorString("#03060a");
  if (scene.sun) scene.sun.show = false;
  if (scene.moon) scene.moon.show = false;
  viewer.camera.percentageChanged = 0.02;
  viewer.camera.setView({ destination: C.Cartesian3.fromDegrees(20, 25, 2.2e7) });

  // continuous-render override, reference counted by reason
  const reasons = new Set();
  viewer.setContinuous = (reason, on) => {
    if (on) reasons.add(reason); else reasons.delete(reason);
    scene.requestRenderMode = reasons.size === 0;
    scene.requestRender();
  };
  // cheap safety net: async textures/primitives that don't request a frame themselves
  setInterval(() => scene.requestRender(), 2000);
  return viewer;
}

// Terrain arrives after /api/config; the viewer (and its imagery) never waits for it.
export function applyConfig(viewer, cfg) {
  if (!cfg || !cfg.cesium_ion_token || viewer._ionTerrain) return;
  viewer._ionTerrain = true;
  C.Ion.defaultAccessToken = cfg.cesium_ion_token;
  viewer.scene.setTerrain(C.Terrain.fromWorldTerrain());
  viewer.scene.requestRender();
}

// Warm the browser cache with the base imagery's levels 0-2 (21 tiles) so the first frames
// of the globe are textured instead of black while Cesium schedules its own requests.
export function preloadBaseTiles(template) {
  const imgs = [];
  for (let z = 0; z <= 2; z++) {
    const n = 1 << z;
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
      const im = new Image();
      im.crossOrigin = "anonymous";
      im.src = template.replace("{z}", z).replace("{y}", y).replace("{x}", x);
      imgs.push(im);
    }
  }
  return imgs;
}

export function setupImagery(viewer) {
  const scene = viewer.scene, layers = viewer.imageryLayers;
  // base layer goes in first (index 0), before any data layer or overlay
  const base = layers.addImageryProvider(makeImagery("base"), 0);
  // darken + desaturate so data pops (kept mild: never dim the base to near-black)
  base.brightness = 0.78; base.contrast = 1.1; base.saturation = 0.6; base.gamma = 1.0;
  // keep requesting frames until the first base tiles have arrived (requestRenderMode would
  // otherwise only repaint on the 2 s safety net)
  const t0 = performance.now();
  const kick = () => {
    scene.requestRender();
    if (!scene.globe.tilesLoaded && performance.now() - t0 < 20000) setTimeout(kick, 150);
  };
  kick();
  const today = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  const dateIn = $("gibs-date");
  dateIn.value = today;
  dateIn.max = new Date().toISOString().slice(0, 10);
  const overlay = { labels: null, viirs: null, night: null };
  function setOverlay(name, on) {
    if (overlay[name]) { layers.remove(overlay[name], true); overlay[name] = null; }
    if (on) {
      const l = layers.addImageryProvider(makeImagery(name, dateIn.value || today));
      l.alpha = name === "night" ? 0.8 : name === "labels" ? 0.85 : 1;
      if (name === "viirs") { l.brightness = 0.8; l.saturation = 0.7; }
      overlay[name] = l;
      if (name !== "labels" && overlay.labels) layers.raiseToTop(overlay.labels);
    }
    scene.requestRender();
  }
  for (const n of ["labels", "viirs", "night"]) $("t-" + n).addEventListener("change", (e) => setOverlay(n, e.target.checked));
  dateIn.addEventListener("change", () => { for (const n of ["viirs", "night"]) if ($("t-" + n).checked) setOverlay(n, true); });

  // graticule: one static primitive, built on first use
  let grid = null;
  const gridOn = (on) => {
    if (on && !grid) {
      const inst = [];
      for (let lat = -75; lat <= 75; lat += 15) {
        const pts = [];
        for (let lon = -180; lon <= 180; lon += 5) pts.push(lon, lat);
        inst.push(new C.GeometryInstance({ geometry: new C.SimplePolylineGeometry({ positions: C.Cartesian3.fromDegreesArray(pts) }),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString(lat === 0 ? "#5ee3ea" : "#5ee3ea").withAlpha(lat === 0 ? 0.32 : 0.14)) } }));
      }
      for (let lon = -180; lon < 180; lon += 15) {
        const pts = [];
        for (let lat = -88; lat <= 88; lat += 4) pts.push(lon, lat);
        inst.push(new C.GeometryInstance({ geometry: new C.SimplePolylineGeometry({ positions: C.Cartesian3.fromDegreesArray(pts) }),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString("#5ee3ea").withAlpha(lon === 0 ? 0.3 : 0.12)) } }));
      }
      grid = scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }), asynchronous: false }));
    }
    if (grid) grid.show = on;
    scene.requestRender();
  };
  $("t-grid").addEventListener("change", (e) => gridOn(e.target.checked));
  $("t-daynight").addEventListener("change", (e) => {
    scene.globe.enableLighting = e.target.checked;
    scene.globe.dynamicAtmosphereLighting = e.target.checked;
    scene.requestRender();
  });
}
