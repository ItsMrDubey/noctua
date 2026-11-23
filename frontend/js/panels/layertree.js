// Left rail + layer tree: group collapse (remembered per viewer), rail navigation, a layer
// filter box, an "only active layers" view, preset chips built from existing toggles, and
// throttled count-up layer counts.
//
// Group order (operational hierarchy): Tracks · Events & stories · Sensors · Signals & space ·
// Infrastructure & watchlists · Analysis · Reference · Map & display.
import { $, esc, REDUCED } from "/static/js/ui.js";

const CLOSED_KEY = "noctua.tree.closed.v2"; // v2: groups were regrouped, old keys do not map
const DEFAULT_CLOSED = ["signals", "infra", "analysis", "ref", "map"];
// group keys of the previous layout -> the group that now holds their layers
const ALIAS = { moves: "analysis", naval: "analysis", density: "analysis", imagery: "map", weather: "map" };

// Presets switch ON a sensible set of existing layers (never new data). A preset whose layers are
// all on is shown active; clicking it again switches exactly those layers off. Other layers are
// never touched, so presets combine.
export const PRESETS = [
  { k: "conflict", label: "Conflict watch", title: "Military aircraft, news events + storylines, war-likely fires, front line, bases, GNSS jamming, squawks, surge cells",
    ids: ["t-aircraft", "t-events", "t-stories", "t-fires", "f-mark", "r-frontline", "r-bases", "s-gnss", "s-squawks", "m-surges"] },
  { k: "maritime", label: "Maritime", title: "Ships, naval units, carrier hypotheses, seaports, maritime lines, chokepoints, submarine cables, LNG terminals",
    ids: ["t-ships", "m-naval", "n-carriers", "r-ports", "r-maritime", "r-choke", "i-cables", "i-lng"] },
  { k: "air", label: "Air", title: "Military aircraft, 1 h trails, velocity leaders, airlift corridors, airports, emergency squawks, GNSS interference",
    ids: ["t-aircraft", "t-miltrails", "t-leaders", "m-corridors", "r-airports", "s-squawks", "s-gnss"] },
  { k: "disasters", label: "Disasters", title: "Fires, quakes, news events, IR clouds, precipitation radar, internet outages, radiation",
    ids: ["t-fires", "t-quakes", "t-events", "t-wx-ir", "t-wx-radar", "s-outages", "s-radiation"] },
  { k: "infra", label: "Infrastructure", title: "Cables + landings, pipelines, power plants, refineries, LNG, dams, seaports, internet outages",
    ids: ["i-cables", "i-landings", "i-pipelines", "i-power", "i-refineries", "i-lng", "i-dams", "r-ports", "s-outages"] },
];

const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, " ").trim();

export class LayerTree {
  constructor() {
    this.pending = {}; this.shown = {}; this.trunc = {}; this.extra = {};
    // collapsed sections persist across reloads; first visit keeps the long tail folded
    let closed;
    try { closed = JSON.parse(localStorage.getItem(CLOSED_KEY) || "null"); } catch (e) { closed = null; }
    this.closed = new Set(Array.isArray(closed) ? closed : DEFAULT_CLOSED);
    this.q = ""; this.activeOnly = false;
    for (const g of document.querySelectorAll("#left .grp > header")) this._bindHeader(g.parentElement);
    for (const b of document.querySelectorAll(".rail-btn[data-g]")) {
      b.addEventListener("click", () => this.focusGroup(b.dataset.g));
    }
    $("rail-toggle").addEventListener("click", () => this.toggleCollapsed());
    this._tools();
    // header "on" counts + preset state follow every toggle (also programmatic ones that dispatch change)
    $("left").addEventListener("change", () => this.refresh());
    setInterval(() => this.flush(), 1000);
    this.refresh();
  }
  _bindHeader(sec) {
    const h = sec.querySelector(":scope > header");
    if (!h || h._bound) return;
    h._bound = true;
    sec.classList.toggle("closed", this.closed.has(sec.dataset.g));
    h.tabIndex = 0; h.setAttribute("role", "button");
    h.setAttribute("aria-expanded", String(!sec.classList.contains("closed")));
    if (!h.querySelector(".g-on")) { const s = document.createElement("small"); s.className = "g-on"; h.querySelector("h3").after(s); }
    h.addEventListener("click", () => this.setClosed(sec, !sec.classList.contains("closed")));
    h.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); h.click(); } });
  }
  setClosed(sec, on) {
    sec.classList.toggle("closed", on);
    const h = sec.querySelector(":scope > header");
    if (h) h.setAttribute("aria-expanded", String(!on));
    if (on) this.closed.add(sec.dataset.g); else this.closed.delete(sec.dataset.g);
    try { localStorage.setItem(CLOSED_KEY, JSON.stringify([...this.closed])); } catch (e) { /* storage full / disabled */ }
  }
  toggleCollapsed(force) {
    const on = force != null ? force : !document.body.classList.contains("left-collapsed");
    document.body.classList.toggle("left-collapsed", on);
  }
  focusGroup(g) {
    g = ALIAS[g] || g;
    this.toggleCollapsed(false);
    const sec = document.querySelector('#left .grp[data-g="' + g + '"]');
    if (!sec) return;
    this.setClosed(sec, false);
    sec.scrollIntoView({ block: "start", behavior: REDUCED ? "auto" : "smooth" });
    sec.classList.remove("flash"); void sec.offsetWidth; sec.classList.add("flash");
    for (const b of document.querySelectorAll(".rail-btn[data-g]")) b.classList.toggle("on", b.dataset.g === g);
  }

  // ---- filter box, "Active" view, presets ----
  _tools() {
    const f = $("lt-filter"), act = $("lt-active"), pre = $("lt-presets");
    if (f) {
      f.addEventListener("input", () => { this.q = norm(f.value); this.applyFilter(); });
      f.addEventListener("keydown", (e) => { if (e.key === "Escape" && f.value) { e.stopPropagation(); f.value = ""; this.q = ""; this.applyFilter(); } });
    }
    if (act) act.addEventListener("click", () => { this.activeOnly = !this.activeOnly; act.setAttribute("aria-pressed", String(this.activeOnly)); act.classList.toggle("on", this.activeOnly); this.applyFilter(); });
    if (pre) {
      pre.innerHTML = PRESETS.map((p) => '<button type="button" class="lt-pre" data-k="' + p.k + '" aria-pressed="false" title="' + esc(p.title) + '">' + esc(p.label) + "</button>").join("");
      pre.addEventListener("click", (e) => { const b = e.target.closest(".lt-pre"); if (b) this.applyPreset(b.dataset.k); });
    }
  }
  _boxes(p) { return p.ids.map((id) => $(id)).filter((b) => b && !b.disabled); }
  applyPreset(k) {
    const p = PRESETS.find((x) => x.k === k);
    if (!p) return;
    const boxes = this._boxes(p);
    const allOn = boxes.length && boxes.every((b) => b.checked);
    for (const b of boxes) if (b.checked === allOn) b.click(); // click(): fires the same change handlers as the user
    this.refresh();
  }
  // Blocks: a top-level row (or sub-heading / widget) plus the sub rows, chips and fields after it.
  _blocks(sec) {
    const out = [];
    let cur = null;
    for (const el of sec.children) {
      if (el.tagName === "HEADER" || el.hidden) continue;
      const lead = el.matches("h4.sub-h") || el.matches(".sig-wx") || (el.matches("label.row") && !el.classList.contains("sub"));
      if (lead || !cur) { cur = { lead: el, els: [el], head: el.matches("h4.sub-h") }; out.push(cur); } else cur.els.push(el);
    }
    return out;
  }
  _text(el) { return norm(el.textContent + " " + (el.title || "")); }
  applyFilter() {
    const q = this.q, act = this.activeOnly, filtering = !!(q || act);
    document.getElementById("left").classList.toggle("filtering", filtering);
    let anyShown = false;
    for (const sec of document.querySelectorAll("#left .grp")) {
      const blocks = this._blocks(sec);
      const title = this._text(sec.querySelector(":scope > header h3"));
      let shown = 0, head = null, headShown = false;
      const showHead = () => { if (head) head.lead.classList.toggle("f-hide", !headShown); };
      for (const b of blocks) {
        if (b.head) { showHead(); head = b; headShown = false; continue; }
        let ok = true;
        if (q) ok = title.includes(q) || (head && this._text(head.lead).includes(q)) || b.els.some((el) => this._text(el).includes(q));
        if (ok && act) { const box = b.lead.querySelector && b.lead.querySelector("input[type=checkbox]"); ok = !!(box && box.checked); }
        if (!filtering) ok = true;
        for (const el of b.els) el.classList.toggle("f-hide", !ok);
        if (ok) { shown++; headShown = true; }
      }
      showHead();
      if (!filtering) for (const b of blocks) if (b.head) b.lead.classList.remove("f-hide");
      sec.classList.toggle("f-hide", filtering && !shown);
      // matches show even in folded groups; the remembered fold state comes back when the filter clears
      sec.classList.toggle("closed", !(filtering && shown) && this.closed.has(sec.dataset.g));
      if (shown) anyShown = true;
    }
    const empty = $("lt-empty");
    if (empty) { empty.classList.toggle("hidden", !filtering || anyShown); empty.textContent = act && !q ? "No layers are switched on." : "No layers match."; }
  }
  // header "k on" badges + preset chip state; also re-applies an active filter
  refresh() {
    for (const sec of document.querySelectorAll("#left .grp")) {
      this._bindHeader(sec);
      const boxes = [...sec.querySelectorAll("label.row:not(.sub) > input[type=checkbox]")];
      const on = boxes.filter((b) => b.checked).length;
      const el = sec.querySelector(":scope > header .g-on");
      if (el) { const t = on ? on + " on" : ""; if (el.textContent !== t) el.textContent = t; el.title = on + " of " + boxes.length + " layers on"; }
    }
    for (const b of document.querySelectorAll("#lt-presets .lt-pre")) {
      const p = PRESETS.find((x) => x.k === b.dataset.k), boxes = p ? this._boxes(p) : [];
      const on = boxes.length > 0 && boxes.every((x) => x.checked);
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
    }
    if (this.q || this.activeOnly) this.applyFilter();
  }

  // Counts are buffered and written at most once per second.
  setCount(kind, n, truncated) { this.pending[kind] = n; if (truncated != null) this.trunc[kind] = truncated; }
  /** Extra per-layer counts that feed group totals (e.g. webcams -> Sensors). */
  setExtra(kind, n) { this.extra[kind] = n; }
  flush() {
    let live = 0, any = false;
    for (const k of ["aircraft", "ships", "fires", "quakes", "events"]) {
      const n = k in this.pending ? this.pending[k] : this.shown[k];
      if (k === "aircraft" || k === "ships") { if (n != null) { live += n; any = true; } }
      if (!(k in this.pending)) continue;
      const from = this.shown[k];
      this.shown[k] = n;
      delete this.pending[k];
      const el = $("c-" + k);
      if (!el) continue;
      el.classList.toggle("trunc", !!this.trunc[k]);
      el.title = this.trunc[k] ? "Truncated: the server capped this layer; zoom in or shorten the window" : "";
      this.animate(el, from, n);
      if (k === "events") { const ge = $("g-events-n"); if (ge) ge.textContent = n == null ? "–" : n.toLocaleString(); }
    }
    const g = $("g-live-n");
    if (g && any) { const t = live.toLocaleString(); if (g.textContent !== t) g.textContent = t; }
    // Sensors: visible fires + quakes (+ webcams when on)
    const on = (id) => { const b = $(id); return !!(b && b.checked); };
    const parts = [on("t-fires") ? this.shown.fires : null, on("t-quakes") ? this.shown.quakes : null, on("s-webcams") ? this.extra.webcams : null].filter((v) => v != null);
    const gs = $("g-sensors-n");
    if (gs) { const t = parts.length ? parts.reduce((a, b) => a + b, 0).toLocaleString() : "–"; if (gs.textContent !== t) gs.textContent = t; }
    // Analysis: movement + naval sub-group totals
    const num = (id) => { const el = $(id); const v = el ? parseInt(String(el.textContent).replace(/[^\d]/g, ""), 10) : NaN; return isNaN(v) ? null : v; };
    const an = [num("g-moves-n"), num("g-naval-n")].filter((v) => v != null);
    const ga = $("g-analysis-n");
    if (ga) { const t = an.length ? an.reduce((a, b) => a + b, 0).toLocaleString() : "–"; if (ga.textContent !== t) ga.textContent = t; }
  }
  animate(el, from, to) {
    if (to == null) { el.textContent = "–"; return; }
    if (REDUCED || from == null || from === to || Math.abs(to - from) > 5000) { el.textContent = to.toLocaleString(); return; }
    const t0 = performance.now(), dur = 380;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = Math.round(from + (to - from) * e).toLocaleString();
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
}
