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

  // 2.5D: oblique "Google Earth" view around the point under the screen centre, with 3D
  // buildings. Toggling again returns to a top-down view of the same point.
  const tiltBtn = $("hud-tilt");
  let onEnter25 = () => {};
  const centerTarget = () => {
    const c = new C.Cartesian2(scene.canvas.clientWidth / 2, scene.canvas.clientHeight / 2);
    const ray = cam.getPickRay(c);
    return (ray && scene.globe.pick(ray, scene)) || cam.pickEllipsoid(c, scene.globe.ellipsoid);
  };
  const is25 = () => scene.mode === C.SceneMode.SCENE3D && cam.pitch > C.Math.toRadians(-70);
  const syncTilt = () => { if (tiltBtn) tiltBtn.classList.toggle("on", is25()); };
  cam.moveEnd.addEventListener(syncTilt);
  const flyAround = (target, pitchDeg, range) => {
    const sphere = new C.BoundingSphere(target, 1);
    viewer.setContinuous("tilt", true);
    cam.flyToBoundingSphere(sphere, {
      offset: new C.HeadingPitchRange(cam.heading, C.Math.toRadians(pitchDeg), range),
      duration: 1.6,
      complete: () => { viewer.setContinuous("tilt", false); syncTilt(); },
      cancel: () => viewer.setContinuous("tilt", false),
    });
  };
  const toggleTilt = () => {
    if (scene.mode !== C.SceneMode.SCENE3D) { // buildings and oblique views need the 3D globe
      scene.morphTo3D(0.6);
      modeBtn.textContent = "2D";
      setTimeout(toggleTilt, 800);
      return;
    }
    const target = centerTarget();
    if (!target) return; // looking at space
    // eye height above the ground being looked at (terrain-aware)
    const eye = Math.max(cam.positionCartographic.height - C.Cartographic.fromCartesian(target).height, 50);
    if (is25()) {
      flyAround(target, -90, Math.min(Math.max(eye * 2, 3000), 2e7));
    } else {
      onEnter25();
      // from orbit drop to city scale; closer in keep roughly the same distance so the
      // framing holds (a valley seen from 25 km stays a valley, a street stays a street)
      const range = eye > 80e3 ? 2500 : Math.max(eye * 1.1, 400);
      flyAround(target, -30, range);
    }
  };
  if (tiltBtn) tiltBtn.addEventListener("click", toggleTilt);
  return { home, toggleMode, toggleTilt, set onEnter25(fn) { onEnter25 = fn; } };
}

// FPS meter: main-thread frames (rAF), scene renders/s, worst render time over the last second,
// plus whatever `extra()` reports (app.js: live websocket cost).
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
        (viewer.scene.requestRenderMode ? "  (on demand)" : "  (continuous)") + (api.extra ? api.extra() : "");
    }
    frames = 0; renders = 0; worst = 0; worstGap = 0;
  }, 1000);
  const toggle = () => { on = !on; el.classList.toggle("hidden", !on); if (on) { last = performance.now(); requestAnimationFrame(loop); } };
  window.__fps = () => ({ on });
  const api = { toggle, extra: null };
  return api;
}
