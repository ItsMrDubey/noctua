// Right-panel EVENTS tab: virtualised list (updated_at desc), filter box, click to select + fly.
import { $, esc } from "/static/js/ui.js";
import { uiGlyph, threatColor, eventCat, EVENT_STATUS } from "/static/js/symbology.js";

const ROW_H = 42, OVERSCAN = 6;
export const relTime = (ts) => {
  const t = typeof ts === "number" ? ts : Date.parse(ts);
  if (isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 60 ? "now" : s < 3600 ? Math.floor(s / 60) + "m" : s < 86400 ? Math.floor(s / 3600) + "h" : Math.floor(s / 86400) + "d";
};
export const statusPill = (st) => '<span class="pill st-' + esc(String(st).replace(/[^a-z-]/g, "")) + '">' + esc(EVENT_STATUS[st] || st || "?") + "</span>";

export class EventList {
  constructor(layer, onPick, isActive) {
    this.layer = layer; this.onPick = onPick; this.isActive = isActive;
    this.rows = []; this.dirty = true; this.q = ""; this.selId = null; this.active = -1;
    this.body = document.querySelector('#tracks .tk-body[data-pane="events"]');
    this.body.tabIndex = 0;
    this.body.innerHTML = '<div class="vspace" style="position:relative"></div>';
    this.space = this.body.firstChild;
    const f = $("ev-filter");
    f.addEventListener("input", () => { this.q = f.value.trim().toLowerCase(); this.dirty = true; this.render(true); });
    this.space.addEventListener("click", (e) => {
      const r = e.target.closest(".erow");
      if (r) { this.active = +r.dataset.i; this.onPick(r.dataset.id); this.paint(); }
    });
    this.body.addEventListener("scroll", () => { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this.paint(); }); }, { passive: true });
    this.body.addEventListener("keydown", (e) => this.key(e));
    const prev = layer.onChange;
    layer.onChange = () => { if (prev) prev(); this.dirty = true; };
    setInterval(() => { this._tick = (this._tick || 0) + 1; if (this._tick % 30 === 0) this._painted = null; this.render(false); }, 1000);
  }
  setSelected(id) { this.selId = id; this.paint(); }
  key(e) {
    if (!this.rows.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      this.active = Math.max(0, Math.min(this.rows.length - 1, this.active + (e.key === "ArrowDown" ? 1 : -1)));
      const top = this.active * ROW_H, vh = this.body.clientHeight;
      if (top < this.body.scrollTop) this.body.scrollTop = top;
      else if (top + ROW_H > this.body.scrollTop + vh) this.body.scrollTop = top + ROW_H - vh;
      this.paint();
    } else if (e.key === "Enter" && this.active >= 0) { e.preventDefault(); this.onPick(this.rows[this.active].id); }
  }
  render(force) {
    if (!this.isActive() || (!this.dirty && !force)) return;
    this.dirty = false;
    const q = this.q;
    let rows = this.layer.visibleItems();
    if (q) rows = rows.filter((it) => [it.title, it.category, it.status, it.precision, it.place_name].some((v) => v != null && String(v).toLowerCase().includes(q)));
    rows.sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0));
    this.rows = rows;
    this.space.style.height = rows.length * ROW_H + "px";
    const n = $("ev-count"), txt = rows.length.toLocaleString() + " events";
    if (n && n.textContent !== txt) n.textContent = txt;
    this._painted = null;
    this.paint();
  }
  paint() {
    if (!this.isActive()) return;
    const top = this.body.scrollTop, h = this.body.clientHeight;
    const a = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN), b = Math.min(this.rows.length, Math.ceil((top + h) / ROW_H) + OVERSCAN);
    let html = "";
    if (!this.rows.length) html = '<div class="empty" style="position:absolute;left:0;right:0"><b>No events</b>' + (this.layer.items.size ? "Nothing matches the current filters." : "Waiting for the event builder. Fused events appear here as outlets report them.") + "</div>";
    for (let i = a; i < b; i++) {
      const it = this.rows[i];
      const cls = "erow" + (i === this.active ? " act" : "") + (it.id === this.selId ? " sel" : "");
      const c = threatColor("events", it);
      html += '<div class="' + cls + '" data-i="' + i + '" data-id="' + esc(it.id) + '" style="top:' + i * ROW_H + 'px;--c:' + c + '" title="' + esc(eventCat(it.category)[0]) + '">' +
        '<div class="g" style="color:' + c + '">' + uiGlyph("events", it) + '</div><div class="t">' + esc(it.title || it.id) + "</div>" +
        '<div class="m">' + statusPill(it.status) + "<span>" + (it.source_diversity || 0) + " outlet" + (it.source_diversity === 1 ? "" : "s") + "</span><span>" + esc(it.place_name || it.precision || "") + "</span><time>" + relTime(it.updated_at) + "</time></div></div>";
    }
    if (html !== this._painted) { this._painted = html; this.space.innerHTML = html; }
  }
}
