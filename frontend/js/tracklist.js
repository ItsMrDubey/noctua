// Virtualised, sortable, filterable track table. Renders only the rows in view, and only
// re-sorts when the underlying layer changed and the panel is visible (≤ 1/s).
import { $, esc } from "/static/js/ui.js";

const ROW_H = 24;
const OVERSCAN = 8;
const num = (v, d = 0) => (v == null || isNaN(v) ? "" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
export const COLS = {
  aircraft: { grid: "1.35fr .75fr 1fr .9fr .6fr", cols: [["name", "Callsign"], ["type_code", "Type"], ["reg", "Reg / op"], ["alt_ft", "Alt ft", "r"], ["speed_kts", "Kts", "r"]] },
  ships: { grid: "1.6fr 1fr 1.1fr .6fr", cols: [["name", "Name"], ["type_code", "Type"], ["operator", "Operator"], ["speed_kts", "Kts", "r"]] },
};
const cell = (it, key) => {
  if (key === "name") return it.name || it.id;
  if (key === "reg") return it.reg || it.operator || "";
  if (key === "alt_ft") return num(it.alt_ft);
  if (key === "speed_kts") return num(it.speed_kts);
  return it[key] == null ? "" : it[key];
};

export class TrackList {
  constructor(layers, onPick, opts = {}) {
    this.layers = layers; this.onPick = onPick; this.opts = opts;
    this.tab = "aircraft"; this.sort = { aircraft: ["high_priority", -1], ships: ["name", 1] };
    this.open = true; this.dirty = true; this.rows = []; this.active = -1; this.q = ""; this.selId = null;
    this.body = document.querySelector("#tracks .tk-body");
    this.body.tabIndex = 0;
    this.body.innerHTML = '<div class="vhead"></div><div class="vspace" style="position:relative"></div>';
    this.head = this.body.firstChild; this.space = this.body.lastChild;
    for (const l of Object.values(layers)) {
      const prev = l.onChange;
      l.onChange = () => { if (prev) prev(); if (l === this.layers[this.tab]) this.dirty = true; };
    }
    $("tl-collapse").addEventListener("click", () => { this.open = !this.open; this.dirty = true; this.render(true); });
    for (const b of document.querySelectorAll("#tracks .tab")) b.addEventListener("click", () => this.setTab(b.dataset.tab));
    const filter = $("tk-filter");
    if (filter) filter.addEventListener("input", () => { this.q = filter.value.trim().toLowerCase(); this.dirty = true; this.render(true); });
    this.head.addEventListener("click", (e) => {
      const h = e.target.closest("[data-k]");
      if (!h) return;
      const cur = this.sort[this.tab];
      this.sort[this.tab] = [h.dataset.k, cur[0] === h.dataset.k ? -cur[1] : 1];
      this.dirty = true; this.render(true);
    });
    this.space.addEventListener("click", (e) => {
      const r = e.target.closest(".vrow");
      if (r) { this.active = +r.dataset.i; this.onPick(this.tab, r.dataset.id); this.paint(); }
    });
    this.body.addEventListener("scroll", () => { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this.paint(); }); }, { passive: true });
    this.body.addEventListener("keydown", (e) => this.key(e));
    setInterval(() => this.render(false), 1000);
  }
  setTab(t) { this.tab = t; this.active = -1; this.dirty = true; this.body.scrollTop = 0; this.render(true); }
  setVisible(v) { $("tracks").classList.toggle("hidden", !v); if (v) this.render(true); }
  setSelected(id) { this.selId = id; this.paint(); }
  visible() { return this.open && !$("tracks").classList.contains("hidden") && !$("tracks").classList.contains("off"); }
  key(e) {
    if (!this.rows.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      this.active = Math.max(0, Math.min(this.rows.length - 1, this.active + (e.key === "ArrowDown" ? 1 : -1)));
      const top = this.active * ROW_H, vh = this.body.clientHeight - ROW_H;
      if (top < this.body.scrollTop) this.body.scrollTop = top;
      else if (top > this.body.scrollTop + vh - ROW_H) this.body.scrollTop = top - vh + ROW_H;
      this.paint();
    } else if (e.key === "Enter" && this.active >= 0) {
      e.preventDefault(); this.onPick(this.tab, this.rows[this.active].id);
    }
  }
  render(force) {
    $("tracks").classList.toggle("collapsed", !this.open);
    $("tl-collapse").textContent = this.open ? "–" : "+";
    for (const b of document.querySelectorAll("#tracks .tab")) b.classList.toggle("on", b.dataset.tab === this.tab);
    if (!this.visible() || (!this.dirty && !force)) return;
    this.dirty = false;
    const layer = this.layers[this.tab];
    const [k, dir] = this.sort[this.tab];
    const q = this.q;
    let rows = layer.visibleItems();
    if (q) rows = rows.filter((it) => [it.name, it.id, it.type_code, it.operator, it.reg].some((v) => v != null && String(v).toLowerCase().includes(q)));
    const pins = this.opts.pins;
    rows.sort((a, b) => {
      if (pins) { const pa = pins.has(a.id), pb = pins.has(b.id); if (pa !== pb) return pa ? -1 : 1; }
      const x = a[k], y = b[k];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : (x === y ? 0 : x < y ? -1 : 1)) * dir;
    });
    this.rows = rows;
    const spec = COLS[this.tab];
    const headHtml = spec.cols.map(([key, label, cls]) => '<div data-k="' + key + '"' + (key === k ? ' class="s' + (cls ? " " + cls : "") + '"' : cls ? ' class="' + cls + '"' : "") +
      ' style="' + (cls === "r" ? "text-align:right" : "") + '">' + label + (key === k ? (dir > 0 ? " ▲" : " ▼") : "") + "</div>").join("");
    if (this.head._h !== headHtml) { this.head._h = headHtml; this.head.innerHTML = headHtml; }
    this.head.style.gridTemplateColumns = spec.grid;
    this.space.style.height = rows.length * ROW_H + "px";
    const c = $("tracks-count");
    const txt = rows.length.toLocaleString();
    if (c && c.textContent !== txt) c.textContent = txt;
    this._painted = null;
    this.paint();
  }
  paint() {
    if (!this.visible()) return;
    const spec = COLS[this.tab];
    const top = this.body.scrollTop, h = this.body.clientHeight;
    const a = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
    const b = Math.min(this.rows.length, Math.ceil((top + h) / ROW_H) + OVERSCAN);
    const pins = this.opts.pins;
    let html = "";
    for (let i = a; i < b; i++) {
      const it = this.rows[i];
      const cls = "vrow" + (it.high_priority ? " hp" : "") + (i === this.active ? " act" : "") + (it.id === this.selId ? " sel" : "");
      html += '<div class="' + cls + '" data-i="' + i + '" data-id="' + esc(it.id) + '" style="top:' + i * ROW_H + "px;grid-template-columns:" + spec.grid + '">' +
        spec.cols.map(([key, , c2], j) => "<div" + (c2 ? ' class="' + c2 + '"' : "") + ">" + (j === 0 && pins && pins.has(it.id) ? '<span class="pin">◆</span> ' : "") + esc(cell(it, key)) + "</div>").join("") + "</div>";
    }
    if (html !== this._painted) { this._painted = html; this.space.innerHTML = html; }
  }
}
