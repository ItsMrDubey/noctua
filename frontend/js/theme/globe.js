// Globe look + render budget: darkened imagery, atmosphere, graticule, day/night, overlays,
// request-render mode with an explicit "continuous" override (follow / replay play).
import { $ } from "/static/js/ui.js";
import { makeImagery } from "/static/js/layers.js";

const C = Cesium;

// WebGL can fail to initialise when the browser has exhausted its GPU contexts (many 3D tabs)
// or its GPU process crashed. Retry once with the most compatible context settings before
// giving up with actionable guidance.
const COMPAT_CONTEXT = {
  requestWebgl1: true,
  webgl: { powerPreference: "default", failIfMajorPerformanceCaveat: false, antialias: false },
};

function constructViewer(opts) {
  try {
    return new C.Viewer("globe", opts);
  } catch (first) {
    console.warn("Viewer init failed, retrying with compatible WebGL settings:", first);
    const el = document.getElementById("globe");
    if (el) el.innerHTML = "";
    try {
      return new C.Viewer("globe", { ...opts, contextOptions: COMPAT_CONTEXT });
    } catch (second) {
      showGpuHelp(String(second && second.message ? second.message : second));
      throw second;
    }
  }
}

function showGpuHelp(detail) {
  const box = document.createElement("div");
  box.className = "gpu-help";
  box.style.cssText = "position:fixed;inset:0;display:grid;place-items:center;z-index:9999;" +
    "background:rgba(3,6,10,.92);color:#cfe8ff;font:14px/1.5 'Segoe UI',sans-serif";
  const msg = document.createElement("div");
  msg.style.cssText = "max-width:560px;padding:24px;border:1px solid #1f3a52;border-radius:6px";
  msg.innerHTML = "<b>3D globe could not start (WebGL)</b><br><br>" +
    "Usually the browser has run out of GPU contexts or its GPU process crashed.<br>" +
    "1. Close other tabs showing 3D maps or globes.<br>" +
    "2. Restart the browser (or open <code>chrome://gpu</code> to check WebGL status).<br>" +
    "3. Make sure hardware acceleration is enabled in browser settings.<br><br>" +
    "<span style='opacity:.7'>Detail: </span>";
  const d = document.createElement("span");
  d.style.opacity = ".7";
  d.textContent = detail;  // never innerHTML untrusted text
  msg.appendChild(d);
  const btn = document.createElement("button");
  btn.textContent = "Retry";
  btn.style.cssText = "margin-top:16px";
  btn.onclick = () => location.reload();
  msg.appendChild(document.createElement("br"));
  msg.appendChild(btn);
  box.appendChild(msg);
  document.body.appendChild(box);
}

// Cesium cannot rebuild its GPU resources after a lost context, so recover by reloading
// once the browser restores the context (or after a short delay if it never does).
// Reloads are rate-limited: browsers (Firefox especially) block WebGL for the session after
// repeated context losses, so a tight lose -> reload -> lose loop would end in a dead globe.
const RELOAD_KEY = "noctua.glReloads";
function reloadAllowed() {
  const now = Date.now();
  let hist = [];
  try { hist = JSON.parse(sessionStorage.getItem(RELOAD_KEY) || "[]"); } catch { /* ignore */ }
  hist = hist.filter((t) => now - t < 10 * 60000);
  if (hist.length >= 2) return false;
  hist.push(now);
  try { sessionStorage.setItem(RELOAD_KEY, JSON.stringify(hist)); } catch { /* ignore */ }
  return true;
}

function watchContextLoss(viewer) {
  const canvas = viewer.scene.canvas;
  let pending = null;
  const recover = () => {
    if (pending) return;
    if (reloadAllowed()) { pending = setTimeout(() => location.reload(), 1500); return; }
    showGpuHelp("WebGL context was lost repeatedly (GPU reset or low memory). " +
      "Close other GPU-heavy tabs/apps, then press Retry.");
  };
  canvas.addEventListener("webglcontextlost", (e) => {
    e.preventDefault();
    console.warn("WebGL context lost; waiting for restore");
    setTimeout(recover, 5000);
  }, false);
  canvas.addEventListener("webglcontextrestored", recover, false);
}

export function createViewer(cfg) {
  const opts = {
    baseLayer: false, geocoder: false, baseLayerPicker: false, timeline: false, animation: false,
    homeButton: false, navigationHelpButton: false, sceneModePicker: false, fullscreenButton: false, infoBox: false, selectionIndicator: false,
    requestRenderMode: true, maximumRenderTimeChange: Infinity,
  };
  // base imagery must not queue behind the default 6-requests-per-server throttle
  C.RequestScheduler.requestsByServer["server.arcgisonline.com:443"] = 18;
  const viewer = constructViewer(opts);
  watchContextLoss(viewer);
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
