// Storylines (GET /api/storylines): threads of linked events derived by the worker.
//
// StorylineList  the "Stories" tab of the right panel: active threads, latest activity first,
//                each with its span and "latest event" age by *event* time (best-known
//                occurred / reported time, never the time the system processed it).
// StoryOverlay   the globe layer of one open thread: member events coloured by role, the
//                links between them, sensor hits from the merged timeline, the thread's
//                extent, and playback (setTime): members and hits appear in time order,
//                the most recent ones emphasised.
import { esc, nf } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";
import { fmtDateTime, fmtDate, fmtTime, utcTitle } from "/static/js/timefmt.js";
import { since, distKm } from "/static/js/panels/dossier-kit.js";
import { ROLE, T_BASIS } from "/static/js/panels/timeline-view.js";

const RANGE = { "48h": 48, "7d": 168, "30d": 720 };
const RANGE_KEY = "noctua.stories.range";
const tms = (s) => (s ? Date.parse(s) : NaN);

export const roleChips = (roles) => Object.entries(roles || {}).sort((a, b) => Object.keys(ROLE).indexOf(a[0]) - Object.keys(ROLE).indexOf(b[0]))
  .map(([r, n]) => '<i class="sl-role" style="--rc:' + ((ROLE[r] || [])[1] || "var(--dim)") + '">' + esc((ROLE[r] || [r])[0]) + (n > 1 ? " ×" + n : "") + "</i>").join("");

export const spanText = (s) => {
  // display zone (timefmt.js); same calendar day -> "2026-11-23 09:10 → 14:02"
  if (!s.t_first || !s.t_last) return fmtDateTime(s.t_first || s.t_last);
  return fmtDate(s.t_first) === fmtDate(s.t_last) ? fmtDateTime(s.t_first) + " → " + fmtTime(s.t_last) : fmtDateTime(s.t_first, { short: true }) + " → " + fmtDateTime(s.t_last, { short: true });
};

// The "storyline" section of /api/intel as HTML: the thread an event belongs to (its role, the
// members in time order) or the threads around a point / naming an actor. data-pivot opens
// the storyline pane (Shift: a new pane).
export function storylineHtml(S, selfId) {
  if (!S) return '<div class="dz-empty">No storyline context</div>';
  if (S.error) return '<div class="dz-empty">' + esc(S.error) + "</div>";
  const pv = (id, label) => " data-pivot=\"" + esc(JSON.stringify({ type: "story", id, label })) + "\"";
  if (S.member) {
    const st = S.storyline || {}, m = S.member, role = ROLE[m.role] || [m.role, "var(--dim)"];
    return '<div class="dk-story"' + pv(m.storyline_id, m.title) + ' title="Open the storyline (Shift: new pane)"><span>PART OF STORYLINE</span><b>' + esc(m.title) + "</b>" +
      '<small><i class="sl-role" style="--rc:' + role[1] + '" title="' + esc(m.role_basis || "") + '">' + esc(role[0]) + "</i> " + esc(m.n_events + " events · " + (m.theatre_label || "") + " · " + spanText(m)) + "</small></div>" +
      '<ol class="sl-mini">' + (S.members || []).map((x) => '<li class="' + (x.event_id === selfId ? "self" : "") + '" style="--rc:' + ((ROLE[x.role] || [])[1] || "var(--dim)") + '"' +
        (x.lat != null ? ' data-fly="' + x.lat + "," + x.lon + '"' : "") + '><time title="' + esc((T_BASIS[x.t_basis] || x.t_basis) + '\n' + utcTitle(x.t)) + '">' + esc(fmtDateTime(x.t, { short: true })) + "</time><i>" + esc((ROLE[x.role] || [x.role])[0]) + "</i><span>" + esc(x.title) + "</span></li>").join("") + "</ol>";
  }
  const near = S.nearby || [];
  return near.length ? '<ul class="cx-l">' + near.map((x) => "<li" + pv(x.id, x.title) + ' title="Open the storyline"><i class="at">' + x.n_events + "</i><b>" + esc(x.title) + "</b><span>" + esc(x.theatre_label + " · " + spanText(x)) + "</span></li>").join("") + "</ul>"
    : '<div class="dz-empty">No storylines here in the last 30 days</div>';
}

export class StorylineList {
  // el: list container; tools: toolbar; onOpen(story); onHover(story | null); bbox() -> "w,s,e,n" | null
  constructor({ el, tools, onOpen, onHover, bbox, visible, onCount }) {
    this.el = el; this.tools = tools; this.onOpen = onOpen; this.onHover = onHover || (() => {});
    this.bbox = bbox || (() => null); this.visible = visible || (() => true); this.onCount = onCount || (() => {});
    this.items = []; this.q = ""; this.err = null; this.loading = false; this.job = null;
    let r = "7d";
    try { r = localStorage.getItem(RANGE_KEY) || r; } catch (e) { /* storage disabled */ }
    this.range = RANGE[r] ? r : "7d";
    tools.innerHTML = '<span class="seg">' + Object.keys(RANGE).map((k) => '<button class="tab' + (k === this.range ? " on" : "") + '" data-r="' + k + '" title="Threads with event activity in the last ' + k + '">' + k + "</button>").join("") + "</span>" +
      '<label class="sl-view" title="Only threads whose extent intersects the current view"><input type="checkbox" data-a="view"> in view</label>' +
      '<input type="search" placeholder="Filter title, place, theatre…" autocomplete="off" spellcheck="false" aria-label="Filter storylines">';
    tools.addEventListener("click", (e) => {
      const b = e.target.closest("[data-r]");
      if (!b) return;
      this.range = b.dataset.r;
      try { localStorage.setItem(RANGE_KEY, this.range); } catch (err) { /* ignore */ }
      tools.querySelectorAll("[data-r]").forEach((x) => x.classList.toggle("on", x === b));
      this.refresh();
    });
    tools.querySelector('[data-a="view"]').addEventListener("change", () => this.refresh());
    tools.querySelector('input[type="search"]').addEventListener("input", (e) => { this.q = e.target.value.trim().toLowerCase(); this.render(); });
    el.addEventListener("click", (e) => { const li = e.target.closest("[data-id]"); if (li) this.onOpen(this.items.find((s) => s.id === li.dataset.id)); });
    el.addEventListener("mouseover", (e) => { const li = e.target.closest("[data-id]"); const s = li ? this.items.find((x) => x.id === li.dataset.id) : null; if (s !== this._hov) { this._hov = s; this.onHover(s || null); } });
    el.addEventListener("mouseleave", () => { this._hov = null; this.onHover(null); });
    setInterval(() => { if (this.visible()) this.refresh(); }, 120000);
    setInterval(() => { if (this.visible()) this.render(); }, 30000); // "x ago" labels
  }
  async refresh() {
    if (this.loading) return;
    this.loading = true;
    const p = new URLSearchParams({ hours: String(RANGE[this.range]), limit: "150" });
    const box = this.tools.querySelector('[data-a="view"]').checked ? this.bbox() : null;
    if (box) p.set("bbox", box);
    try {
      const j = await fetchJson("/api/storylines?" + p.toString(), 15000);
      this.items = j.items || []; this.theatres = j.theatres || []; this.job = j.job; this.err = null;
    } catch (e) { this.err = String((e && e.message) || e); }
    this.loading = false;
    this.onCount(this.items.length);
    this.render();
  }
  render() {
    if (!this.visible()) return;
    if (this.err && !this.items.length) { this.el.innerHTML = '<div class="dz-empty">Storylines unavailable: ' + esc(this.err) + "</div>"; return; }
    const q = this.q;
    const list = q ? this.items.filter((s) => (s.title + " " + s.theatre_label + " " + (s.places || []).map((p) => p.name).join(" ")).toLowerCase().includes(q)) : this.items;
    if (!list.length) {
      this.el.innerHTML = '<div class="dz-empty">' + (this.items.length ? "No thread matches the filter" : "No storylines with event activity in the last " + esc(this.range) +
        (this.job && this.job.last_run_at ? " (last linking pass " + esc(since(this.job.last_run_at)) + " ago)" : " yet — the worker links events every few minutes")) + "</div>";
      return;
    }
    const lp = this.job && this.job.last_pass;
    this.el.innerHTML = '<ul class="sl-list">' + list.map((s) => {
      const places = (s.places || []).slice(0, 3).map((p) => p.name).join(" · ");
      return '<li class="sl-row" data-id="' + esc(s.id) + '" tabindex="0">' +
        '<div class="sl-t"><b>' + esc(s.title) + '</b><span class="sl-n" title="' + esc(s.n_news + " news events" + (s.n_sensor ? " + " + s.n_sensor + " sensor events" : "")) + '">' + s.n_events + "</span></div>" +
        '<div class="sl-m"><span class="sl-th">' + esc(s.theatre_label) + "</span>" + (places ? " · " + esc(places) : "") + "</div>" +
        '<div class="sl-s"><span title="' + esc("First: " + (T_BASIS[s.t_first_basis] || s.t_first_basis) + "\n" + utcTitle(s.t_first) + "\nLatest: " + utcTitle(s.t_last)) + '">' + esc(spanText(s)) + "</span>" +
        '<em title="Latest member event time (reported or observed), not processing time">latest event ' + esc(since(s.t_last)) + " ago</em></div>" +
        '<div class="sl-r">' + roleChips(s.roles) + "</div></li>";
    }).join("") + "</ul>" + (lp ? '<div class="sl-foot">Linked by the worker · ' + nf(lp.events) + " events in window · last pass " + esc(since(this.job.last_run_at)) + " ago (" + nf(lp.seconds, 2) + " s)</div>" : "");
  }
}

// ---------------------------------------------------------------- globe overlay
const C = (css, a = 1) => Cesium.Color.fromCssColorString(css).withAlpha(a);
const CORE_KM = 800;

// [west, south, east, north] of the members located within CORE_KM of the representative
// event (city / point precision preferred), else of all located members
export function coreBox(story) {
  const located = (story.members || []).filter((m) => m.lat != null && m.lon != null);
  if (!located.length) return null;
  const fine = located.filter((m) => m.precision !== "country");
  const pool = fine.length ? fine : located;
  const a = story.lat != null ? { lat: story.lat, lon: story.lon } : pool[0];
  const core = pool.filter((m) => distKm(a.lat, a.lon, m.lat, m.lon) <= CORE_KM);
  const pts = core.length ? core : pool;
  return [Math.min(...pts.map((m) => m.lon)), Math.min(...pts.map((m) => m.lat)), Math.max(...pts.map((m) => m.lon)), Math.max(...pts.map((m) => m.lat))];
}

export class StoryOverlay {
  constructor(viewer) {
    this.viewer = viewer;
    this.src = new Cesium.CustomDataSource("storyline");
    viewer.dataSources.add(this.src);
    this.items = []; this.story = null; this.t = null;
  }
  clear() { this.src.entities.removeAll(); this.items = []; this.story = null; this.t = null; this._req(); }
  _req() { this.viewer.scene.requestRender(); }

  // story: /api/storylines/{id}; tl: its merged timeline (sensor hits with coordinates)
  show(story, tl) {
    this.clear();
    this.story = story;
    const ents = this.src.entities, at = {}, stack = {};
    for (const m of story.members || []) {
      if (m.lat == null || m.lon == null) continue;
      // members at the same place stack their labels instead of overprinting
      const sk = m.lat.toFixed(2) + "," + m.lon.toFixed(2), k = (stack[sk] = (stack[sk] || 0) + 1) - 1;
      const col = (ROLE[m.role] || [, "#d3dee8"])[1];
      at[m.event_id] = m;
      const e = ents.add({
        position: Cesium.Cartesian3.fromDegrees(m.lon, m.lat),
        point: { pixelSize: m.role === "origin" ? 13 : 9, color: C(col, 0.95), outlineColor: Cesium.Color.BLACK, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY },
        label: { text: (ROLE[m.role] || [m.role])[0] + " · " + String(m.title || "").slice(0, 46), font: "11px Consolas, monospace", fillColor: C(col), showBackground: true, backgroundColor: C("#05080d", 0.82),
          pixelOffset: new Cesium.Cartesian2(12, -10 - 16 * k), horizontalOrigin: Cesium.HorizontalOrigin.LEFT, disableDepthTestDistance: Number.POSITIVE_INFINITY, show: m.role === "origin" },
        properties: { storyMember: m.event_id },
      });
      this.items.push({ e, t: tms(m.t), kind: "member", role: m.role, size: m.role === "origin" ? 13 : 9, col });
    }
    for (const lk of story.links || []) {
      const a = at[lk.a], b = at[lk.b];
      if (!a || !b || (a.lat === b.lat && a.lon === b.lon)) continue;
      const e = ents.add({ polyline: { positions: Cesium.Cartesian3.fromDegreesArray([a.lon, a.lat, b.lon, b.lat]), width: 1.4, arcType: Cesium.ArcType.GEODESIC,
        material: new Cesium.PolylineDashMaterialProperty({ color: C("#f2ad3d", 0.6), dashLength: 10 }) } });
      this.items.push({ e, t: Math.max(tms(a.t), tms(b.t)), kind: "link" });
    }
    for (const x of (tl && tl.entries) || []) {
      if (x.kind !== "observed" || x.lat == null || x.lon == null) continue;
      const col = x.sensor === "fire" ? "#ff6a1a" : x.sensor === "quake" ? "#ffd24a" : x.sensor === "imagery" ? "#a99bff" : "#9ff3ff";
      const e = ents.add({ position: Cesium.Cartesian3.fromDegrees(x.lon, x.lat), point: { pixelSize: 6, color: C(col, 0.9), outlineColor: Cesium.Color.BLACK, outlineWidth: 1, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
      this.items.push({ e, t: tms(x.t), kind: "sensor", size: 6, col });
    }
    this.box = coreBox(story);
    if (this.box) {
      const [w, s, e, n] = this.box, p = 0.05;
      if (n - s > 0.02 || e - w > 0.02) ents.add({ rectangle: { coordinates: Cesium.Rectangle.fromDegrees(w - p, s - p, e + p, n + p), fill: false, outline: true, outlineColor: C("#f2ad3d", 0.5), height: 0 } });
    }
    this._req();
  }
  // fly to the thread's core extent (members far from the representative event, e.g. a
  // mislocated report, are drawn but do not zoom the camera out to a continent)
  fit() {
    if (!this.box) return;
    const [w, s, e, n] = this.box;
    const dLat = Math.max(0.15, (n - s) * 0.35), dLon = Math.max(0.15, (e - w) * 0.35);
    this.viewer.camera.flyTo({ destination: Cesium.Rectangle.fromDegrees(w - dLon, s - dLat, e + dLon, n + dLat), duration: 1.4 });
  }
  // playback: everything up to t is shown; what happened within `recent` ms of t stands out
  setTime(t, recent = 6 * 36e5) {
    this.t = t;
    for (const it of this.items) {
      const shown = t == null || isNaN(it.t) || it.t <= t;
      it.e.show = shown;
      if (!shown || it.kind === "link") continue;
      const hot = t != null && t - it.t <= recent;
      if (it.e.point) it.e.point.pixelSize = hot ? it.size * 1.7 : it.size;
      if (it.e.label && it.kind === "member") it.e.label.show = t == null ? it.role === "origin" : hot;
    }
    this._req();
  }
}
