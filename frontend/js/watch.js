// Watchlist overlay: live tracks matching OpenSanctions (sanctioned / shadow-fleet vessels,
// sanctioned aircraft) or config/watchlist.yaml get a pulsing ring + tag on the globe; new
// matches and "loitering near a cable / pipeline" detections become alerts; the inspector of a
// matched track shows the sanction programme, risk and source link. Matching runs server-side
// (worker job); this module only polls /api/watch/active and /api/watch/alerts.
import { fetchJson } from "/static/js/api.js";
import { esc, kv, agoHtml } from "/static/js/ui.js";

const C = Cesium;
const FONT = "600 10px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const SEV_COLOR = { high: "#ff3b4e", warn: "#ffb020", info: "#5ee3ea" };
const ring = (c) => "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="' + c + '" stroke-width="2">' +
  '<circle cx="24" cy="24" r="17" stroke-dasharray="6 4"/><path d="M24 2v7M24 39v7M2 24h7M39 24h7" stroke-width="2.4"/></svg>');
const RING = Object.fromEntries(Object.entries(SEV_COLOR).map(([k, c]) => [k, ring(c)]));
const KIND = { sea: "ships", air: "aircraft" };
const LABEL_OFFSET = new C.Cartesian2(0, 30);
const RING_SCALE = new C.NearFarScalar(1e6, 1.0, 2e7, 0.5); // smaller rings far out (same fade as the glyphs)
const BLACK = C.Color.BLACK.withAlpha(0.85);
const safe = (u) => (/^https:\/\//i.test(u || "") ? u : null);

export class WatchOverlay {
  constructor(viewer, { byKind, alerts }) {
    this.viewer = viewer; this.scene = viewer.scene; this.byKind = byKind; this.alerts = alerts;
    this.active = new Map(); // track id -> row
    this.recent = new Map(); // track id -> latest proximity/dark alert
    this.on = true; this.lastId = null; this.status = null; this.onChange = null;
    const P = this.scene.primitives;
    this.bb = P.add(new C.BillboardCollection({ scene: this.scene }));
    this.labels = P.add(new C.LabelCollection());
    this.marks = new Map();
    this.t0 = performance.now();
    // gentle pulse: one shared scale animated per frame only while matches are on screen
    this.scene.preRender.addEventListener(() => {
      if (!this.on || !this.marks.size) return;
      const k = 1 + 0.12 * Math.sin((performance.now() - this.t0) / 260);
      for (const m of this.marks.values()) m.b.scale = k;
    });
  }
  setOn(on) { this.on = on; this.bb.show = this.labels.show = on; this.viewer.setContinuous && this.viewer.setContinuous("watch", on && this.marks.size > 0); this.scene.requestRender(); }
  start() {
    let iv = 0;
    const tick = () => this.poll().catch((e) => {
      if (/HTTP 404/.test(String(e && e.message))) { clearInterval(iv); return; } // older backend: no watch routes
      console.warn("watch", e);
    });
    tick();
    iv = setInterval(tick, 20000);
    setInterval(() => this.place(), 2000);
  }
  async poll() {
    const [a, al] = await Promise.all([fetchJson("/api/watch/active", 10000), fetchJson("/api/watch/alerts?since_id=" + (this.lastId || 0), 10000)]);
    this.status = a.status || null;
    this.active = new Map((a.items || []).map((r) => [r.track_id, r]));
    this.sync();
    const items = al.items || []; // newest first
    const first = this.lastId == null;
    if (items.length) this.lastId = Math.max(this.lastId || 0, ...items.map((x) => x.id));
    else if (first) this.lastId = 0;
    for (const x of items) if (x.kind !== "watchlist") this.recent.set(x.track_id, x);
    if (first) {
      const n = this.active.size, hi = [...this.active.values()].filter((r) => r.severity === "high");
      if (n) this.alerts.push({ sev: hi.length ? "high" : "info", title: n + " watchlist track" + (n > 1 ? "s" : "") + " live", sub: [...this.active.values()].slice(0, 4).map((r) => r.name).join(", ") + (n > 4 ? "…" : "") });
      for (const x of items.filter((y) => Date.now() - Date.parse(y.ts) < 15 * 60e3).slice(0, 3).reverse()) this.push(x);
    } else {
      const fresh = items.slice().reverse();
      if (fresh.length > 6) this.alerts.push({ sev: "warn", title: fresh.length + " new watchlist / infrastructure alerts", sub: fresh.slice(0, 4).map((x) => x.title).join(" · ") });
      else for (const x of fresh) this.push(x);
    }
    if (this.onChange) this.onChange(this);
  }
  push(x) {
    const kind = KIND[(x.track_id || "").split(":")[0]];
    this.alerts.push({ sev: x.severity === "info" ? "info" : x.severity === "high" ? "high" : "warn", title: x.title, sub: x.sub,
      target: { kind, id: x.track_id, lat: x.lat, lon: x.lon } });
  }
  // rings follow the live billboard positions (dead-reckoned models keep their billboard anchor)
  sync() {
    for (const [id, m] of this.marks) if (!this.active.has(id)) { this.bb.remove(m.b); this.labels.remove(m.l); this.marks.delete(id); }
    for (const [id, r] of this.active) {
      let m = this.marks.get(id);
      // name-only hits are unverified: never label the hull as sanctioned on the globe
      const weak = r.hits.length && r.hits.every((h) => h.strength === "weak");
      const tag = weak ? "Name match?" : (r.hits[0] && r.hits[0].entry && ((r.hits[0].entry.risk || [])[0] || r.hits[0].entry.label)) || "WATCHLIST";
      if (!m) {
        const b = this.bb.add({ image: RING[r.severity] || RING.warn, width: 44, height: 44, scaleByDistance: RING_SCALE, disableDepthTestDistance: Number.POSITIVE_INFINITY, position: C.Cartesian3.fromDegrees(r.lon, r.lat) });
        const l = this.labels.add({ font: FONT, fillColor: C.Color.fromCssColorString(SEV_COLOR[r.severity] || SEV_COLOR.warn), outlineColor: BLACK, outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: LABEL_OFFSET, horizontalOrigin: C.HorizontalOrigin.CENTER, disableDepthTestDistance: Number.POSITIVE_INFINITY,
          distanceDisplayCondition: new C.DistanceDisplayCondition(0, 8e6), position: b.position });
        m = { b, l };
        this.marks.set(id, m);
      }
      m.b.image = RING[r.severity] || RING.warn;
      m.l.text = String(tag).toUpperCase() + " · " + String(r.name || id).toUpperCase().slice(0, 28);
      // clicking the ring selects the track like clicking its glyph
      m.b.id = m.l.id = { watch: true, kind: KIND[id.split(":")[0]], id };
    }
    this.place();
    this.setOn(this.on);
  }
  place() {
    if (!this.marks.size) return;
    for (const [id, m] of this.marks) {
      const layer = this.byKind[KIND[id.split(":")[0]]];
      const p = layer && layer.positionOf(id);
      if (p && !C.Cartesian3.equals(p, m.b.position)) { m.b.position = p; m.l.position = p; }
    }
    this.scene.requestRender();
  }
  hits(id) { const r = this.active.get(id); return r ? r.hits : null; }
}

// Inspector section for a selected track (rendered into #dz-extra by the app hook).
export function watchHtml(id, overlay) {
  const r = overlay && overlay.active.get(id), prox = overlay && overlay.recent.get(id);
  if (!r && !prox) return "";
  let html = "";
  for (const h of (r && r.hits) || []) {
    const e = h.entry || {}, c = SEV_COLOR[e.severity] || SEV_COLOR.warn, src = safe(e.url);
    const weak = h.strength === "weak", hc = weak ? SEV_COLOR.info : c;
    // a name-only hit is a lead, not a finding: the listed vessel's risk tags read "listed as …"
    const tags = (e.risk || []).map((t) => '<span class="tag' + (weak ? " stale" : "") + '" style="border-color:' + hc + ";color:" + hc + '">' + (weak ? "Listed vessel: " : "") + esc(t) + "</span>").join("") +
      (weak ? '<span class="tag stale" title="Only the vessel name matched; verify IMO / MMSI">Name match only · unverified</span>' : "");
    const doubts = (h.doubts || []).length ? '<ul class="watch-doubts">' + h.doubts.map((d) => "<li>" + esc(d) + "</li>").join("") + "</ul>" : "";
    html += '<section class="dz-sec watch-hit" style="--wc:' + hc + '"><h3>' + (e.source === "watchlist" ? "User watchlist" : weak ? "Possible sanctions name match" : "Sanctions / watchlist match") + "<small>matched on " + esc(String(h.matched_on).toUpperCase()) + "</small></h3>" +
      '<div class="dz-tags">' + tags + "</div>" + doubts + kv([
        ["Listed as", esc(e.name)], ["Label", esc(e.label)], ["IMO", esc(e.imo)], ["MMSI", esc(e.mmsi)], ["Registration", esc((e.regs || []).join(", "))], ["Flag", esc(e.flag ? String(e.flag).toUpperCase() : "")],
        ["Programmes", esc((e.programs || []).join(", "))], ["Lists", esc((e.datasets || []).slice(0, 4).join("; "))], ["Basis", esc(e.sanctions ? String(e.sanctions).slice(0, 320) + (e.sanctions.length > 320 ? "…" : "") : "")],
        ["Note", esc(e.note)], ["Source", src ? '<a href="' + esc(src) + '" target="_blank" rel="noopener noreferrer">OpenSanctions entity</a>' : esc(e.source === "watchlist" ? "config/watchlist.yaml" : "")],
        ["Licence", e.licence ? esc(e.licence) + " · not for commercial use" : ""],
      ]) + "</section>";
  }
  if (prox) {
    html += '<section class="dz-sec watch-hit" style="--wc:' + SEV_COLOR.warn + '"><h3>Infrastructure proximity<small>' + agoHtml(prox.ts) + "</small></h3>" +
      kv([["Alert", esc(prox.title)], ["Reason", esc(prox.sub)], ["Asset", esc(prox.detail && prox.detail.infra && prox.detail.infra.name)],
        ["Distance", prox.detail && prox.detail.km != null ? esc(prox.detail.km) + " km" : ""]]) + "</section>";
  }
  return html;
}
