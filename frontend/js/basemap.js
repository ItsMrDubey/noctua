// Basemap source and look (Map & display > Imagery): Esri World Imagery (default), Bing Maps
// Aerial or Google Photorealistic 3D Tiles (both through Cesium ion; need the ion token), and
// "Dim for data" (darkened, desaturated base so data layers pop). Choices are remembered per
// viewer. Photorealistic tiles are drawn only close in (below PHOTO_MAX_EYE): they replace the
// globe surface there and carry their own terrain and buildings, so OSM Buildings are switched
// off while they show.
import { makeImagery } from "/static/js/layers.js";

const C = Cesium;
const KEY = "noctua.basemap";
const PHOTO_MAX_EYE = 60e3;
const BING_AERIAL = 2; // Cesium ion asset ids
const DIM = { brightness: 0.55, contrast: 1.15, saturation: 0.35, hue: 0.03 };
const FULL = { brightness: 1.0, contrast: 1.0, saturation: 1.0, hue: 0 };
const LABEL = { esri: "Esri World Imagery", bing: "Bing Maps Aerial", photo: "Google Photorealistic 3D" };

/** Mounts the controls under the Imagery heading. ``cfgP`` resolves to /api/config;
 * ``onPhoto(on)`` is told when photorealistic tiles take over the surface. */
export function installBasemap({ viewer, cfgP, onPhoto = () => {} }) {
  const scene = viewer.scene, layers = viewer.imageryLayers;
  const st = { src: "esri", dim: false };
  try { Object.assign(st, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) { /* storage disabled */ }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* ignore */ } };

  const head = [...document.querySelectorAll('[data-g="map"] h4.sub-h')].find((h) => /imagery/i.test(h.textContent));
  const box = document.createElement("div");
  box.innerHTML =
    '<label class="row field" title="Base imagery. Bing and Google need the Cesium ion token; Google 3D shows below ' + PHOTO_MAX_EYE / 1e3 + ' km eye altitude">Basemap ' +
    '<select id="bm-src" class="mini"><option value="esri">' + LABEL.esri + '</option><option value="bing">' + LABEL.bing + " (ion)</option>" +
    '<option value="photo">' + LABEL.photo + " (ion)</option></select></label>" +
    '<label class="row sub" title="Darken and desaturate the base imagery so data layers stand out"><input type="checkbox" id="bm-dim"><span class="sw half" style="--c:#7d8792"></span>Dim for data</label>' +
    '<div id="bm-status" class="row sub muted" hidden></div>';
  if (head) head.after(...box.children);
  const sel = document.getElementById("bm-src"), dim = document.getElementById("bm-dim"), msg = document.getElementById("bm-status");
  const status = (t) => { if (!msg) return; msg.hidden = !t; msg.textContent = t || ""; };

  let photo = null, photoP = null, photoActive = false, req = 0;
  const look = () => { const l = layers.length ? layers.get(0) : null; if (l) Object.assign(l, st.dim ? DIM : FULL); scene.requestRender(); };

  function photoGate() {
    const on = !!photo && st.src === "photo" && scene.camera.positionCartographic.height < PHOTO_MAX_EYE;
    if (photo) photo.show = on;
    scene.globe.show = !on;
    if (on !== photoActive) { photoActive = on; onPhoto(on); }
    scene.requestRender();
  }
  scene.camera.changed.addEventListener(photoGate);

  async function token() {
    const cfg = await cfgP.catch(() => null);
    const t = cfg && cfg.cesium_ion_token;
    if (t) C.Ion.defaultAccessToken = t;
    return t;
  }

  async function apply(src) {
    const id = ++req;
    status("");
    if (src !== "esri" && !(await token())) { status("Cesium ion token not configured: using " + LABEL.esri); src = "esri"; }
    let provider;
    try {
      provider = src === "bing" ? await C.IonImageryProvider.fromAssetId(BING_AERIAL) : makeImagery("base");
    } catch (err) {
      status(LABEL[src] + " unavailable (" + (err.message || err) + "): using " + LABEL.esri);
      src = "esri"; provider = makeImagery("base");
    }
    if (id !== req) return;
    const old = layers.length ? layers.get(0) : null;
    layers.addImageryProvider(provider, 0);
    if (old) layers.remove(old, true);
    look();
    if (src === "photo" && !photo) {
      try {
        photoP = photoP || C.createGooglePhotorealistic3DTileset();
        photo = await photoP;
        if (id !== req) return;
        photo.show = false;
        scene.primitives.add(photo);
      } catch (err) {
        photoP = null;
        status(LABEL.photo + " unavailable (" + (err.message || err) + ")");
      }
    }
    if (photo && src !== "photo") photo.show = false;
    if (src === "photo" && photo) status("3D tiles below " + PHOTO_MAX_EYE / 1e3 + " km eye altitude · usage counts against the ion plan");
    st.src = src; sel.value = src; save();
    photoGate();
  }

  sel.value = st.src; dim.checked = !!st.dim;
  sel.addEventListener("change", () => apply(sel.value));
  dim.addEventListener("change", () => { st.dim = dim.checked; save(); look(); });
  // the default base layer is already in place; only switch when another source is chosen
  if (st.src !== "esri") apply(st.src); else look();
  return { apply, get source() { return st.src; } };
}
