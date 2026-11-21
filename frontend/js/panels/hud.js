// Bottom-right HUD: compass (click = north up), cursor lat/lon, eye altitude, world / 2D-3D;
// plus an FPS / frame-time meter toggled with F.
import { $ } from "/static/js/ui.js";

const C = Cesium;
const fmtDeg = (v, pos, neg) => Math.abs(v).toFixed(4).padStart(8, " ") + "° " + (v >= 0 ? pos : neg);
const fmtAlt = (m) => (m >= 1e6 ? (m / 1e3).toLocaleString(undefined, { maximumFractionDigits: 0 }) + " km" : m >= 1e4 ? (m / 1e3).toFixed(1) + " km" : Math.round(m) + " m");

export function setupHud(viewer) {
  const scene = viewer.scene, cam = viewer.camera;
  const rose = $("compass-rose"), alt = $("hud-alt"), lat = $("hud-lat"), lon = $("hud-lon");
  let lastHead = null, lastAlt = "";
  const onCam = () => {
    const h = Math.round(C.Math.toDegrees(cam.heading) * 2) / 2;
    if (h !== lastHead) { lastHead = h; rose.style.transform = "rotate(" + -h + "deg)"; }
    const a = fmtAlt(cam.positionCartographic.height);
    if (a !== lastAlt) { lastAlt = a; alt.textContent = a; }
  };
  cam.changed.addEventListener(onCam);
  cam.moveEnd.addEventListener(onCam);
  onCam();
  let mv = null, raf = 0;
  const handler = new C.ScreenSpaceEventHandler(scene.canvas);
  handler.setInputAction((e) => {
    mv = e.endPosition;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const p = cam.pickEllipsoid(mv, scene.globe.ellipsoid);
      if (!p) { lat.textContent = "—"; lon.textContent = "—"; return; }
      const c = C.Cartographic.fromCartesian(p);
      lat.textContent = fmtDeg(C.Math.toDegrees(c.latitude), "N", "S");
      lon.textContent = fmtDeg(C.Math.toDegrees(c.longitude), "E", "W");
    });
  }, C.ScreenSpaceEventType.MOUSE_MOVE);

  $("hud-compass").addEventListener("click", () => {
    const c = cam.positionCartographic;
    cam.flyTo({ destination: C.Cartesian3.fromRadians(c.longitude, c.latitude, c.height), orientation: { heading: 0, pitch: cam.pitch, roll: 0 }, duration: 0.6 });
  });
  const home = () => cam.flyTo({ destination: C.Cartesian3.fromDegrees(20, 25, 2.2e7), duration: 1.4 });
  $("hud-home").addEventListener("click", home);
  const modeBtn = $("hud-mode");
  const toggleMode = () => {
    const to2d = scene.mode !== C.SceneMode.SCENE2D;
    if (to2d) scene.morphTo2D(0.8); else scene.morphTo3D(0.8);
    modeBtn.textContent = to2d ? "3D" : "2D";
    viewer.setContinuous("morph", true);
    setTimeout(() => viewer.setContinuous("morph", false), 1200);
  };
  modeBtn.addEventListener("click", toggleMode);
  return { home, toggleMode };
}

// FPS meter: main-thread frames (rAF), scene renders/s, worst render time over the last second.
export function setupFps(viewer) {
  const el = document.createElement("div");
  el.id = "fps";
  el.className = "hidden";
  el.style.cssText = "position:fixed;z-index:40;left:50%;top:66px;transform:translateX(-50%);padding:4px 10px;font:11px var(--f-mono);color:var(--fg);" +
    "background:rgba(3,7,12,.85);border:1px solid var(--line-2);pointer-events:none;white-space:pre";
  document.body.appendChild(el);
  let frames = 0, renders = 0, worst = 0, t0 = 0, on = false, last = performance.now(), worstGap = 0;
  viewer.scene.preRender.addEventListener(() => { t0 = performance.now(); });
  viewer.scene.postRender.addEventListener(() => { renders++; worst = Math.max(worst, performance.now() - t0); });
  const loop = (t) => {
    frames++; worstGap = Math.max(worstGap, t - last); last = t;
    if (on) requestAnimationFrame(loop);
  };
  setInterval(() => {
    if (on) {
      el.textContent = "UI " + frames + " fps · max gap " + worstGap.toFixed(0) + " ms   GLOBE " + renders + " renders/s · max " + worst.toFixed(1) + " ms" +
        (viewer.scene.requestRenderMode ? "  (on demand)" : "  (continuous)");
    }
    frames = 0; renders = 0; worst = 0; worstGap = 0;
  }, 1000);
  const toggle = () => { on = !on; el.classList.toggle("hidden", !on); if (on) { last = performance.now(); requestAnimationFrame(loop); } };
  window.__fps = () => ({ on });
  return { toggle };
}
