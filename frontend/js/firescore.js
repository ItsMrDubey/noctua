// Fire conflict likelihood (scored by the worker's FireScoreJob; /api/layers/fires items carry
// conflict_score 0-100 + conflict_reasons). Self-contained: wraps the FireLayer instance's key /
// apply / sync (no change to layers.js), adds "Fires: All / War-likely" + threshold controls under
// the fires row, a "Front line (DeepStateMap)" reference toggle, and a score section in the
// inspector's fire dossier. Filter "All" with "Mark war-likely" off is the plain FRP rendering.
import { fetchJson } from "/static/js/api.js";
import { $, esc, nf } from "/static/js/ui.js";
import { fmtDateTime } from "/static/js/timefmt.js";

const C = Cesium;
const STORE_KEY = "noctua.fires.score";
const FRONT = C.Color.fromCssColorString("#ff3b3b").withAlpha(0.9);
// threshold -> 100: crimson -> hot magenta (distinct from the orange FRP ramp)
const SCORE_RAMP = Array.from({ length: 11 }, (_, i) => C.Color.fromHsl(0.97 - 0.12 * (i / 10), 1, 0.58, 0.95));
const OUTLINE = C.Color.BLACK.withAlpha(0.75);

// reason code -> [label, detail(features)]
export const FIRE_REASONS = {
  near_frontline: ["Near the Ukraine front line", (f) => f.frontline_km != null && nf(f.frontline_km, 1) + " km from the DeepStateMap line"],
  frontline_zone: ["Front-line zone", (f) => f.frontline_km != null && nf(f.frontline_km, 1) + " km from the DeepStateMap line"],
  ucdp_hist: ["Historical conflict area (UCDP)", (f) => f.ucdp_n != null && nf(f.ucdp_n) + " UCDP GED events within 25 km"],
  ucdp_weak: ["Some conflict history (UCDP)", (f) => f.ucdp_n != null && nf(f.ucdp_n) + " UCDP GED event" + (f.ucdp_n === 1 ? "" : "s") + " within 25 km"],
  conflict_news_5km: ["Conflict news within 5 km", newsDetail],
  conflict_news_city: ["Conflict news in this town", newsDetail],
  conflict_news_25km: ["Conflict news within 25 km", newsDetail],
  night: ["Night-time detection", () => "crop and land-clearing fires burn mostly by day"],
  isolated: ["Isolated site", (f) => f.sites_2km != null && (f.sites_2km ? nf(f.sites_2km) + " other site(s)" : "no other site") + " within 2 km"],
  urban: ["Inside a populated place", (f) => f.place && esc(f.place) + (f.place_km != null ? " (" + nf(f.place_km, 1) + " km from centre)" : "")],
  infrastructure: ["At a facility", (f) => f.facility && esc(f.facility)],
  high_frp: ["High radiative power", () => ""],
  static_anomaly: ["Static site burning far above normal", (f) => f.median_frp != null && "usual median " + nf(f.median_frp, 1) + " MW"],
  large_cluster: ["Part of a large contiguous fire", (f) => f.cluster_n != null && nf(f.cluster_n) + " sites within 3 km"],
  burn_region: ["Regional burning (wildfire / crop season)", (f) => f.region_n != null && nf(f.region_n) + " detections within 25 km"],
  low_conf: ["Low-confidence detection", () => ""],
  static_site: ["Recurring static heat source", (f) => f.static_days != null && "seen on " + f.static_days + " of " + f.history_days + " past days (flare, plant, refinery)"],
  gas_flare: ["Known gas flare (EOG VIIRS Nightfire)", (f) => f.flare_type && esc(f.flare_type)],
  volcano: ["Volcano (Smithsonian GVP)", (f) => f.volcano && esc(f.volcano)],
};
function newsDetail(f) {
  const ev = f.news_event;
  if (!ev) return "";
  return (f.news_km != null ? nf(f.news_km, 1) + " km · " : "") + esc(ev.title || ev.id) + (f.news_n > 1 ? " (+" + (f.news_n - 1) + " more)" : "");
}
export const reasonLabel = (code) => (FIRE_REASONS[code] ? FIRE_REASONS[code][0] : String(code).replace(/_/g, " "));

function load() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null") || {}; } catch (e) { return {}; }
}

export function installFireScore({ viewer, fires, inspector }) {
  const saved = load();
  const st = { mode: saved.mode === "war" ? "war" : "all", thr: Number.isFinite(saved.thr) ? saved.thr : 50, mark: saved.mark !== false };
  const save = () => { try { localStorage.setItem(STORE_KEY, JSON.stringify(st)); } catch (e) { /* storage disabled */ } };
  const score = (it) => (it && it.conflict_score != null ? it.conflict_score : -1);
  const hot = (it) => score(it) >= st.thr;
  const styled = (it) => hot(it) && (st.mode === "war" || st.mark);

  // ---- rendering hooks on the FireLayer instance ----
  const baseKey = fires.key.bind(fires), baseApply = fires.apply.bind(fires), baseSync = fires.sync.bind(fires);
  fires.key = (it) => baseKey(it) + "|" + (styled(it) ? score(it) : "");
  fires.apply = (p, it) => {
    baseApply(p, it);
    if (styled(it)) {
      const t = Math.min(1, Math.max(0, (score(it) - st.thr) / Math.max(1, 100 - st.thr)));
      p.color = SCORE_RAMP[Math.round(t * 10)];
      p.pixelSize = 9 + 4 * t;
      p.outlineColor = OUTLINE; p.outlineWidth = 1;
    } else p.outlineWidth = 0;
  };
  fires.filter = (it) => st.mode === "all" || hot(it);
  fires.sync = (list) => { baseSync(list); count(); };

  // ---- layer tree controls (under the fires row) ----
  const row = $("t-fires").closest("label");
  const box = document.createElement("div");
  box.innerHTML =
    '<label class="row field sub" title="Conflict likelihood of each hotspot (front line, UCDP history, conflict news, pattern; static sites and wildfire fronts demoted)">Show <select id="f-mode" class="mini"><option value="all">All fires</option><option value="war">War-likely</option></select><b id="c-fires-war" class="num" title="Fires at or above the threshold"></b></label>' +
    '<label class="row field sub" title="Score at or above which a fire counts as war-likely">Score ≥ <input type="range" id="f-thr" min="0" max="100" step="5"><output id="f-thr-v" class="num"></output></label>' +
    '<label class="row sub"><input type="checkbox" id="f-mark"><span class="sw" style="--c:#ff3d7f"></span>Mark war-likely</label>';
  row.after(...box.children);
  const mode = $("f-mode"), thr = $("f-thr"), mark = $("f-mark");
  mode.value = st.mode; thr.value = st.thr; mark.checked = st.mark; $("f-thr-v").textContent = st.thr;
  function count() {
    let n = 0;
    for (const it of fires.items.values()) if (hot(it)) n++;
    const el = $("c-fires-war"), t = fires.items.size ? n.toLocaleString() : "";
    if (el.textContent !== t) el.textContent = t;
  }
  function restyle() {
    for (const [id, p] of fires.map) {
      const it = fires.items.get(id), k = fires.key(it);
      if (p._k !== k) { p._k = k; fires.apply(p, it); }
    }
    fires.refilter(); count(); save();
  }
  mode.addEventListener("change", () => { st.mode = mode.value; restyle(); });
  thr.addEventListener("input", () => { st.thr = +thr.value; $("f-thr-v").textContent = st.thr; restyle(); });
  mark.addEventListener("change", () => { st.mark = mark.checked; restyle(); });
  restyle();

  // ---- DeepStateMap front line (reference group) ----
  const front = { prim: null, p: null };
  const fRow = document.createElement("label");
  fRow.className = "row";
  fRow.title = "Ukraine front line derived from DeepStateMap.live occupied / grey-zone areas (refreshed by the worker every 6 h)";
  fRow.innerHTML = '<input type="checkbox" id="r-frontline"><span class="sw ln" style="--c:#ff3b3b"></span>Front line <small>DeepStateMap</small>';
  // reserved slot at the top of the Reference group; older markup: after "Maritime lines"
  const slot = $("slot-frontline");
  if (slot) slot.replaceWith(fRow); else $("r-maritime").closest("label").after(fRow);
  const ensureFront = () => front.p || (front.p = fetchJson("/api/reference/frontline", 30000).then((gj) => {
    const inst = [];
    for (const f of gj.features || []) {
      const props = f.properties || {};
      for (const line of (f.geometry && f.geometry.coordinates) || []) {
        if (line.length < 2) continue;
        inst.push(new C.GeometryInstance({
          geometry: new C.PolylineGeometry({ positions: C.Cartesian3.fromDegreesArray(line.flat()), width: 2.4, vertexFormat: C.PolylineMaterialAppearance.VERTEX_FORMAT }),
          id: { ref: true, kind: "boundary", item: { lat: line[0][1], lon: line[0][0], name: "Front line · DeepStateMap", layer: "DeepStateMap map of " + (props.source_datetime || "?") + " · fetched " + fmtDateTime(props.fetched_at, { zone: true }) } },
        }));
      }
    }
    if (!inst.length) { fRow.title = "No front line cached yet (the worker fetches it from DeepStateMap)"; return; }
    front.prim = viewer.scene.primitives.add(new C.Primitive({
      geometryInstances: inst, asynchronous: true, releaseGeometryInstances: true,
      appearance: new C.PolylineMaterialAppearance({ material: C.Material.fromType("Color", { color: FRONT }) }),
    }));
  }).catch((e) => { front.p = null; console.warn("front line", e); }));
  $("r-frontline").addEventListener("change", async (e) => {
    if (e.target.checked) await ensureFront();
    if (front.prim) front.prim.show = e.target.checked;
    const end = Date.now() + 3000; // async primitive: render a few frames until it is ready
    const t = setInterval(() => { viewer.scene.requestRender(); if (Date.now() > end) clearInterval(t); }, 200);
  });

  // ---- inspector: score + reasons in the fire dossier ----
  const details = new Map(); // fire id -> GET /api/fires/score/{id} (or null when unscored)
  let shown = null;
  const baseObs = inspector.observation.bind(inspector);
  inspector.observation = (kind, it) => {
    baseObs(kind, it);
    shown = kind === "fires" ? it.id : null;
    if (kind !== "fires") return;
    render(it);
    if (it.conflict_score != null && !details.has(it.id)) {
      details.set(it.id, undefined);
      if (details.size > 300) details.delete(details.keys().next().value);
      fetchJson("/api/fires/score/" + encodeURIComponent(it.id), 8000).then((d) => { details.set(it.id, d); if (shown === it.id) render(it); }, () => details.set(it.id, null));
    }
  };
  function render(it) {
    const dz = inspector.dz, head = dz.querySelector(".dz-head");
    if (!head) return;
    let sec = dz.querySelector(".dz-sec.fscore");
    if (!sec) { sec = document.createElement("section"); sec.className = "dz-sec fscore"; head.after(sec); }
    const html = sectionHtml(it, details.get(it.id));
    if (sec.innerHTML !== html) sec.innerHTML = html;
  }
  function sectionHtml(it, d) {
    if (it.conflict_score == null) return "<h3>Conflict likelihood</h3><div class=\"dz-empty\">Not scored yet (the worker scores new detections every few minutes)</div>";
    const s = it.conflict_score, f = (d && d.features) || {}, pts = f.points || {};
    const verdict = s >= st.thr ? "War-likely" : s >= st.thr * 0.6 ? "Possible" : "Unlikely";
    const reasons = (d && d.reasons) || it.conflict_reasons || [];
    const li = reasons.map((code) => {
      const r = FIRE_REASONS[code], v = pts[code];
      const det = r && d ? r[1](f) : "";
      return "<li><b" + (v < 0 ? ' class="neg"' : "") + ">" + (v == null ? "" : (v > 0 ? "+" : "") + nf(v, 0)) + "</b>" + esc(reasonLabel(code)) + (det ? "<em>" + det + "</em>" : "") + "</li>";
    }).join("");
    return "<h3>Conflict likelihood<small>" + verdict + " · threshold " + st.thr + "</small></h3>" +
      '<div class="kv"><span>Score</span><div><div class="cbar" title="' + s + '/100"><i style="width:' + s + '%"></i></div><b class="cnum">' + s + "</b></div></div>" +
      (li ? '<ul class="fs-why">' + li + "</ul>" : '<div class="dz-empty">No conflict indicators</div>') +
      (d && f.static_overridden ? '<div class="dz-empty">Static-site penalty lifted: ' + esc(f.static_overridden.join(", ").replace(/_/g, " ")) + "</div>" : "");
  }
  return { state: st, restyle };
}
