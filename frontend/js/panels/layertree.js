// Left rail + layer tree: group collapse, rail navigation, throttled count-up layer counts.
import { $, REDUCED } from "/static/js/ui.js";

const CLOSED_KEY = "noctua.tree.closed";
const DEFAULT_CLOSED = ["moves", "naval", "sensors", "density", "imagery"];

export class LayerTree {
  constructor() {
    this.pending = {}; this.shown = {}; this.trunc = {};
    // collapsed sections persist across reloads; first visit keeps the long tail folded
    let closed;
    try { closed = JSON.parse(localStorage.getItem(CLOSED_KEY) || "null"); } catch (e) { closed = null; }
    this.closed = new Set(Array.isArray(closed) ? closed : DEFAULT_CLOSED);
    for (const g of document.querySelectorAll("#left .grp > header")) {
      const sec = g.parentElement;
      sec.classList.toggle("closed", this.closed.has(sec.dataset.g));
      g.addEventListener("click", () => this.setClosed(sec, !sec.classList.contains("closed")));
    }
    for (const b of document.querySelectorAll(".rail-btn[data-g]")) {
      b.addEventListener("click", () => this.focusGroup(b.dataset.g));
    }
    $("rail-toggle").addEventListener("click", () => this.toggleCollapsed());
    setInterval(() => this.flush(), 1000);
  }
  setClosed(sec, on) {
    sec.classList.toggle("closed", on);
    if (on) this.closed.add(sec.dataset.g); else this.closed.delete(sec.dataset.g);
    try { localStorage.setItem(CLOSED_KEY, JSON.stringify([...this.closed])); } catch (e) { /* storage full / disabled */ }
  }
  toggleCollapsed(force) {
    const on = force != null ? force : !document.body.classList.contains("left-collapsed");
    document.body.classList.toggle("left-collapsed", on);
  }
  focusGroup(g) {
    this.toggleCollapsed(false);
    const sec = document.querySelector('#left .grp[data-g="' + g + '"]');
    if (!sec) return;
    this.setClosed(sec, false);
    sec.scrollIntoView({ block: "start", behavior: REDUCED ? "auto" : "smooth" });
    sec.classList.remove("flash"); void sec.offsetWidth; sec.classList.add("flash");
    for (const b of document.querySelectorAll(".rail-btn[data-g]")) b.classList.toggle("on", b.dataset.g === g);
  }
  // Counts are buffered and written at most once per second.
  setCount(kind, n, truncated) { this.pending[kind] = n; if (truncated != null) this.trunc[kind] = truncated; }
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
