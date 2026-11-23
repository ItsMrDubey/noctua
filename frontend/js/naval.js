// Carrier strike group ACTIVITY HYPOTHESES (probabilistic, from public signals - never positions).
// Perf rules: primitives only (no Entities); hypotheses/indicators diffed by id; the probability
// surface is one static Primitive rebuilt only when its data changes; requestRender after changes.
// Standalone module: the controller wires it (see naval-report.md).
import { fetchJson } from "/static/js/api.js";
import { esc, ago, agoHtml, nf } from "/static/js/ui.js";

const C = Cesium;
const ION = "#5ee3ea", AMBER = "#f2ad3d";
const FONT = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const DISCLAIMER = "Probabilistic estimate from public signals — not a confirmed position.";
const KIND = {
  sea_terminating_flight: { c: "#ff8a3d", label: "Sea-terminating flight" },
  cvw_aircraft: { c: "#5ee3ea", label: "Carrier air wing type over ocean" },
  support_ship: { c: "#4fdf8c", label: "Fleet support ship loiter" },
  nav_warning: { c: AMBER, label: "NGA navigational warning" },
  traffic_void: { c: "#a99bff", label: "Commercial traffic void" },
  traffic_diversion: { c: "#c7a8ff", label: "Traffic diversion" },
  orbit: { c: "#7fb4ff", label: "Over-ocean orbit (carrier type)" },
  support_orbit: { c: "#5d7fa8", label: "Tanker / patrol orbit (supporting only)" },
  air_track: { c: "#ffd166", label: "Air-track fit (aircraft meeting a moving point)" },
  news: { c: "#9fb0c0", label: "News mention (names only)" },
  usni_prior: { c: "#66798c", label: "USNI tracker prior" },
};
const FIT = "#ffd166";
const ROLE = { launch: "launch", recovery: "recovery", loiter: "helo loiter" };
const kindColor = (k) => C.Color.fromCssColorString((KIND[k] || { c: "#9fb0c0" }).c);
const safeUrl = (u) => (/^(https?:\/\/|\/api\/)/i.test(u || "") ? u : null);
const LABEL_OFFSET = new C.Cartesian2(16, -2);
const R_EARTH = 6371008.8;

// top-down wireframe carrier: hull, angled deck, island
const GLYPH = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40" fill="none" stroke="#ffffff" ' +
  'stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round">' +
  '<path d="M17 3 L23 3 L25 9 L25 33 L22 37 L18 37 L15 33 L15 12 L12 9 L12 6 Z"/>' +
  '<path d="M12.5 7 L22.5 25" stroke-dasharray="2 2"/><path d="M20 26 L20 35" stroke-dasharray="2 2"/>' +
  '<rect x="25.6" y="17" width="3" height="7" rx=".4"/><circle cx="20" cy="20" r="18.5" stroke-opacity=".35" stroke-dasharray="1.5 3"/></svg>');

function circlePositions(lat, lon, radiusKm, n = 72) {
  const out = [], d = (radiusKm * 1000) / R_EARTH;
  const p1 = C.Math.toRadians(lat), l1 = C.Math.toRadians(lon);
  for (let i = 0; i <= n; i++) {
    const b = (i / n) * 2 * Math.PI;
    const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
    const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    out.push(C.Cartesian3.fromRadians(l2, p2, 0));
  }
  return out;
}

const ringPositions = (ring) => C.Cartesian3.fromDegreesArray(ring.flat());

// 2-D uncertainty ellipse (semi-axes km, major-axis azimuth deg from north) as a closed ring
function ellipsePositions(e, n = 72) {
  const out = [], az = C.Math.toRadians(e.orientation_deg || 0);
  const coslat = Math.max(0.05, Math.cos(C.Math.toRadians(e.lat)));
  for (let i = 0; i <= n; i++) {
    const t = (i / n) * 2 * Math.PI, a = e.semi_major_km * Math.cos(t), b = e.semi_minor_km * Math.sin(t);
    const north = a * Math.cos(az) - b * Math.sin(az), east = a * Math.sin(az) + b * Math.cos(az);
    out.push(C.Cartesian3.fromDegrees(e.lon + east / (111.32 * coslat), Math.max(-89.9, Math.min(89.9, e.lat + north / 110.57)), 0));
  }
  return out;
}
const dashed = (color) => C.Material.fromType("PolylineDash", { color, dashLength: 14 });

export class CarrierLayer {
  constructor(viewer, { onSelect, onAircraft, hours = 48 } = {}) {
    this.viewer = viewer;
    this.scene = viewer.scene;
    this.onSelect = onSelect || (() => {});
    // onAircraft(trackId, fix): open the aircraft behind a contributing leg (profile / intel)
    this.onAircraft = onAircraft || null;
    this.fits = new Map();             // fit id -> fit (air-track moving-point fits)
    this.fitSig = "";
    this.hours = hours;
    this.visible = false;
    this.showIndicators = true;
    this.showSurface = true;
    this.kinds = null;                 // Set of indicator kinds to show, or null = all
    this.hyps = new Map();             // id -> {item, bb, label, ring}
    this.points = new Map();           // key -> {pt, item}
    this.warnings = new Map();         // key -> polyline
    this.surfaceSig = "";
    this.surfacePrim = null;
    this.status = null;
    this.selected = null;
    this.bb = this.scene.primitives.add(new C.BillboardCollection({ scene: this.scene }));
    this.labels = this.scene.primitives.add(new C.LabelCollection());
    this.rings = this.scene.primitives.add(new C.PolylineCollection());
    this.warnLines = this.scene.primitives.add(new C.PolylineCollection());
    this.pts = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this.fitLines = this.scene.primitives.add(new C.PolylineCollection());   // tracks + ellipses
    this.legLines = this.scene.primitives.add(new C.PolylineCollection());   // contributing legs
    this.fixPts = this.scene.primitives.add(new C.PointPrimitiveCollection());
    this._apply();
  }

  _apply() {
    const v = this.visible;
    this.bb.show = this.labels.show = this.rings.show = v;
    this.pts.show = this.warnLines.show = v && this.showIndicators;
    this.fitLines.show = this.legLines.show = this.fixPts.show = v && this.showIndicators;
    if (this.surfacePrim) this.surfacePrim.show = v && this.showSurface;
    this.scene.requestRender();
  }

  setVisible(b) {
    this.visible = !!b;
    this._apply();
    if (this.visible) this.refresh().catch(() => {});
  }

  setIndicatorsVisible(b) { this.showIndicators = !!b; this._apply(); }
  setSurfaceVisible(b) { this.showSurface = !!b; this._apply(); }

  // kinds: iterable of indicator kinds or null/empty for all
  setKinds(kinds) {
    const s = kinds && [...kinds].length ? new Set(kinds) : null;
    this.kinds = s;
    for (const { pt, item } of this.points.values()) pt.show = !s || s.has(item.kind);
    for (const pl of this.warnings.values()) pl.show = !s || s.has("nav_warning");
    this.scene.requestRender();
  }

  async refresh() {
    if (!this.visible) return;
    const q = "?hours=" + this.hours;
    const [hyps, surf, ind, fits] = await Promise.all([
      fetchJson("/api/naval/carriers" + q, 60000),
      fetchJson("/api/naval/carrier-surface" + q, 60000),
      fetchJson("/api/naval/indicators" + q, 60000),
      fetchJson("/api/naval/air-tracks" + q, 60000).catch(() => ({ items: [] })), // older backend
    ]);
    this.status = hyps.indicator_status || null;
    this._syncHypotheses(hyps.items || []);
    this._syncSurface(surf.items || []);
    this._syncIndicators(ind.items || []);
    this._syncFits(fits.items || []);
    this.scene.requestRender();
    return hyps.items || [];
  }

  _syncHypotheses(items) {
    const want = new Map(items.map((h) => [h.id, h]));
    for (const [id, e] of this.hyps) {
      if (want.has(id)) continue;
      this.bb.remove(e.bb); this.labels.remove(e.label); this.rings.remove(e.ring);
      this.hyps.delete(id);
    }
    for (const [id, h] of want) {
      const alpha = 0.35 + 0.65 * Math.max(0, Math.min(1, h.confidence));
      const col = C.Color.fromCssColorString(ION).withAlpha(alpha);
      const name = (h.candidate_names && h.candidate_names[0]) ? " · " + h.candidate_names[0].name + "?" : "";
      const text = "CSG? " + h.confidence.toFixed(2) + name;
      const pos = C.Cartesian3.fromDegrees(h.lon, h.lat, 0);
      const e = this.hyps.get(id);
      if (e) {
        const moved = e.item.lat !== h.lat || e.item.lon !== h.lon || e.item.radius_km !== h.radius_km;
        e.bb.position = pos; e.bb.color = col;
        e.label.position = pos; e.label.text = text; e.label.fillColor = col;
        if (moved) e.ring.positions = circlePositions(h.lat, h.lon, h.radius_km);
        e.ring.material = dashed(col.withAlpha(alpha * 0.8));
        e.item = h;
        continue;
      }
      this.hyps.set(id, {
        item: h,
        bb: this.bb.add({ position: pos, image: GLYPH, width: 34, height: 34, color: col,
          disableDepthTestDistance: Number.POSITIVE_INFINITY, id: { navalHyp: id } }),
        label: this.labels.add({ position: pos, text, font: FONT, fillColor: col, outlineColor: C.Color.BLACK,
          outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: LABEL_OFFSET,
          horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER,
          disableDepthTestDistance: Number.POSITIVE_INFINITY, id: { navalHyp: id } }),
        ring: this.rings.add({ positions: circlePositions(h.lat, h.lon, h.radius_km), width: 1.5,
          material: dashed(col.withAlpha(alpha * 0.8)), id: { navalHyp: id } }),
      });
    }
  }

  _syncSurface(cells) {
    const sig = cells.map((c) => c.h3 + ":" + c.p.toFixed(2)).join("|");
    if (sig === this.surfaceSig) return;
    this.surfaceSig = sig;
    if (this.surfacePrim) { this.scene.primitives.remove(this.surfacePrim); this.surfacePrim = null; }
    if (!cells.length) return;
    const base = C.Color.fromCssColorString(ION);
    const instances = cells.map((c) => new C.GeometryInstance({
      geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(ringPositions(c.boundary.slice(0, -1))),
        height: 0, vertexFormat: C.PerInstanceColorAppearance.FLAT_VERTEX_FORMAT }),
      attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(base.withAlpha(0.04 + 0.30 * c.p)) },
      id: { navalCell: c.h3 },
    }));
    this.surfacePrim = this.scene.primitives.add(new C.Primitive({
      geometryInstances: instances, asynchronous: true, releaseGeometryInstances: true,
      appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }),
    }));
    this.surfacePrim.show = this.visible && this.showSurface;
  }

  _syncIndicators(items) {
    const want = new Map();
    const warn = new Map();
    for (const it of items) {
      const key = it.kind + "|" + it.ts + "|" + it.lat + "|" + it.lon;
      want.set(key, it);
      if (it.kind === "nav_warning" && it.polygon) warn.set(key, it);
    }
    for (const [k, e] of this.points) if (!want.has(k)) { this.pts.remove(e.pt); this.points.delete(k); }
    for (const [k, it] of want) {
      if (this.points.has(k)) continue;
      const pt = this.pts.add({ position: C.Cartesian3.fromDegrees(it.lon, it.lat, 0), pixelSize: it.kind === "news" ? 4 : 6,
        color: kindColor(it.kind).withAlpha(it.kind === "news" || it.kind === "usni_prior" ? 0.6 : 0.95),
        outlineColor: C.Color.BLACK.withAlpha(0.7), outlineWidth: 1,
        disableDepthTestDistance: Number.POSITIVE_INFINITY, id: { navalIndicator: it } });
      pt.show = !this.kinds || this.kinds.has(it.kind);
      this.points.set(k, { pt, item: it });
    }
    for (const [k, pl] of this.warnings) if (!warn.has(k)) { this.warnLines.remove(pl); this.warnings.delete(k); }
    for (const [k, it] of warn) {
      if (this.warnings.has(k)) continue;
      const pl = this.warnLines.add({ positions: ringPositions(it.polygon), width: 1.5,
        material: C.Material.fromType("Color", { color: C.Color.fromCssColorString(AMBER).withAlpha(0.85) }),
        id: { navalIndicator: it } });
      pl.show = !this.kinds || this.kinds.has("nav_warning");
      this.warnings.set(k, pl);
    }
  }

  // Air-track fits: fitted path (solid = between first and last fix, dashed = projection to
  // now), the 95 % ellipse at now, and every contributing aircraft leg + its fix. Rebuilt only
  // when the set of fits changes (a few dozen primitives at most).
  _syncFits(items) {
    const sig = items.map((f) => f.id + ":" + f.t_last + ":" + f.n_fixes + ":" + f.ellipse.semi_major_km).join("|");
    if (sig === this.fitSig) return;
    this.fitSig = sig;
    this.fitLines.removeAll(); this.legLines.removeAll(); this.fixPts.removeAll();
    this.fits = new Map(items.map((f) => [f.id, f]));
    const gold = C.Color.fromCssColorString(FIT);
    const legCol = C.Color.fromCssColorString("#ff8a3d").withAlpha(0.7);
    for (const f of items) {
      const weak = f.n_fixes < 2;
      const id = { navalFit: f.id };
      const pts = (f.path || []).map((p) => C.Cartesian3.fromDegrees(p[0], p[1], 0));
      if (pts.length > 1) this.fitLines.add({ positions: pts.slice(0, 2), width: 2.5, id,
        material: C.Material.fromType("Color", { color: gold.withAlpha(0.9) }) });
      if (pts.length > 2) this.fitLines.add({ positions: pts.slice(1), width: 2, id, material: dashed(gold.withAlpha(0.75)) });
      this.fitLines.add({ positions: ellipsePositions(f.ellipse), width: weak ? 1 : 1.6, id,
        material: dashed(gold.withAlpha(weak ? 0.35 : 0.7)) });
      for (const fx of f.fixes || []) {
        const fid = { navalLeg: fx.track_id, fit: f.id, fix: fx };
        if (fx.path && fx.path.length > 1) this.legLines.add({ positions: C.Cartesian3.fromDegreesArray(fx.path.flat()),
          width: 1.4, id: fid, material: C.Material.fromType("Color", { color: legCol }) });
        this.fixPts.add({ position: C.Cartesian3.fromDegrees(fx.lon, fx.lat, 0), pixelSize: 7, color: gold,
          outlineColor: C.Color.BLACK.withAlpha(0.8), outlineWidth: 1.5,
          disableDepthTestDistance: Number.POSITIVE_INFINITY, id: fid });
      }
    }
  }

  // Accepts a Cesium pick result ({primitive, id}) or a primitive. Returns the hypothesis item,
  // {indicator} for an indicator point / warning outline, {fit} for an air-track fit,
  // {aircraft} for a contributing leg (and calls onAircraft), or null.
  pick(p) {
    const ours = (x) => !!(x && (x.navalHyp || x.navalIndicator || x.navalFit || x.navalLeg));
    const id = (p && ours(p.id) ? p.id : p && p.primitive && p.primitive.id) || null;
    if (!ours(id)) return null;
    if (id.navalLeg) {
      if (this.onAircraft) this.onAircraft(id.navalLeg, id.fix);
      return { aircraft: id.navalLeg };
    }
    if (id.navalFit) {
      const f = this.fits.get(id.navalFit);
      if (!f) return null;
      const item = { fit: f };
      this.onSelect(item);
      return item;
    }
    if (id.navalHyp) {
      const e = this.hyps.get(id.navalHyp);
      if (!e) return null;
      this.selected = e.item;
      this.onSelect(e.item);
      return e.item;
    }
    if (id.navalIndicator) {
      const item = { indicator: id.navalIndicator };
      this.onSelect(item);
      return item;
    }
    return null;
  }

  // Renders a hypothesis (or an indicator) into el. Returns a promise.
  async renderInspector(item, el) {
    if (item && item.fit) {
      el.innerHTML = '<div class="nv-insp"><div class="nv-hd">Air-track fit (estimate)</div>' + disclaimerHtml() + fitHtml(item.fit) + "</div>";
      this._wire(el);
      return;
    }
    if (item && item.indicator) { el.innerHTML = indicatorHtml(item.indicator); this._wire(el); return; }
    el.innerHTML = '<div class="nv-insp"><div class="nv-hd">' + esc(item.label || "CSG?") + "</div>" +
      '<div class="nv-loading">Loading evidence…</div>' + disclaimerHtml() + "</div>";
    let full = item;
    try { full = await fetchJson("/api/naval/carriers/" + encodeURIComponent(item.id)); } catch (e) { /* keep slim */ }
    el.innerHTML = hypothesisHtml(full, this.status);
    this._wire(el);
  }

  // aircraft links inside the inspector open the aircraft (profile / intel) via onAircraft
  _wire(el) {
    for (const a of el.querySelectorAll("[data-nv-track]")) {
      a.addEventListener("click", (ev) => {
        if (!this.onAircraft) return;
        ev.preventDefault();
        this.onAircraft(a.dataset.nvTrack, { lat: +a.dataset.lat, lon: +a.dataset.lon, ts: a.dataset.ts, callsign: a.dataset.cs });
      });
    }
  }
}

function aircraftLink(fx) {
  const hex = String(fx.track_id || "").replace(/^air:/, "");
  return '<a href="/api/profile/aircraft/' + esc(encodeURIComponent(hex)) + '" target="_blank" rel="noopener noreferrer" data-nv-track="' +
    esc(fx.track_id) + '" data-lat="' + esc(fx.lat) + '" data-lon="' + esc(fx.lon) + '" data-ts="' + esc(fx.ts) + '" data-cs="' +
    esc(fx.callsign || "") + '">' + esc(fx.callsign || hex) + "</a>";
}

function fitHtml(f) {
  const e = f.ellipse || {};
  const mv = f.velocity_known ? "~" + nf(f.speed_kts) + " kt, heading " + nf(f.heading_deg) + "°" : "unknown (single time / short span)";
  const fixes = (f.fixes || []).map((x) =>
    '<li><i class="nv-dot" style="background:' + FIT + '"></i><div><div class="nv-desc">' + esc(x.description) + "</div>" +
    '<div class="nv-meta">' + esc(ROLE[x.role] || x.role) + " · " + agoHtml(x.ts) + " · w " + nf(x.weight, 2) + " · " + aircraftLink(x) + "</div></div></li>").join("");
  return '<div class="nv-sec">Air-track fit <span>' + esc(f.n_fixes) + " fixes · " + esc((f.aircraft || []).length) + " aircraft</span></div>" +
    '<div class="kv"><span>Motion</span><div>' + mv + "</div>" +
    "<span>Last fix</span><div>" + agoHtml(f.t_last) + " at " + nf(f.last.lat, 2) + ", " + nf(f.last.lon, 2) + "</div>" +
    "<span>Projected now</span><div>" + nf(f.projected.lat, 2) + ", " + nf(f.projected.lon, 2) + "</div>" +
    "<span>95 % ellipse</span><div>" + nf(e.semi_major_km) + " × " + nf(e.semi_minor_km) + " km, axis " + nf(e.orientation_deg) + "°</div>" +
    "<span>Fit RMS</span><div>" + nf(f.rms_km) + " km</div></div>" +
    ((f.notes || []).length ? '<div class="nv-meta">' + f.notes.map(esc).join(" · ") + "</div>" : "") +
    '<div class="nv-meta">Estimate, not a position. The ellipse grows with time since the last fix; V-22 / H-60 also fly from amphibious ships, so a fit means "a flight deck", not necessarily a carrier.</div>' +
    '<ul class="nv-ev">' + fixes + "</ul>";
}

function disclaimerHtml() {
  return '<div class="nv-disclaimer" role="note">' + esc(DISCLAIMER) + "</div>";
}

function refLink(r) {
  const label = r.title || r.id || r.type;
  const url = safeUrl(r.url) || (r.type === "track" ? "/api/track/" + encodeURIComponent(r.id) : null);
  return url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + "</a>"
    : '<span class="nv-ref">' + esc(label) + "</span>";
}

function indicatorHtml(it) {
  const k = KIND[it.kind] || { c: "#9fb0c0", label: it.kind };
  return '<div class="nv-insp"><div class="nv-hd"><i class="nv-dot" style="background:' + k.c + '"></i>' + esc(k.label) + "</div>" +
    '<div class="nv-desc">' + esc(it.description) + "</div>" +
    '<div class="kv"><span>Weight</span><div>' + nf(it.weight, 2) + "</div><span>Position</span><div>" +
    nf(it.lat, 3) + ", " + nf(it.lon, 3) + (it.radius_km ? " (±" + nf(it.radius_km) + " km)" : "") + "</div>" +
    "<span>Seen</span><div>" + agoHtml(it.ts) + "</div></div>" +
    '<div class="nv-refs">' + (it.refs || []).map(refLink).join(" ") + "</div>" +
    (it.detail && it.detail.ellipse ? fitHtml(it.detail) : "") + disclaimerHtml() + "</div>";
}

function hypothesisHtml(h, status) {
  const pct = Math.round((h.confidence || 0) * 100);
  const bd = Object.entries(h.indicator_breakdown || {}).sort((a, b) => b[1] - a[1]);
  const indep = new Set(h.independent_kinds || []);
  const rows = bd.map(([k, v]) => {
    const meta = KIND[k] || { c: "#9fb0c0", label: k };
    return '<div class="nv-kind"><i class="nv-dot" style="background:' + meta.c + '"></i><span>' + esc(meta.label) +
      (indep.has(k) ? ' <em title="counts as an independent signal">indep.</em>' : "") + "</span>" +
      '<div class="nv-mini"><b style="width:' + Math.round(v * 100) + "%;background:" + meta.c + '"></b></div></div>';
  }).join("");
  const names = (h.candidate_names || []).map((c) =>
    '<li><b>' + esc(c.name) + "?</b> <span>" + nf(c.distance_km) + " km</span><div>" + esc(c.basis) +
    (safeUrl(c.url) ? ' · <a href="' + esc(c.url) + '" target="_blank" rel="noopener noreferrer">source</a>' : "") + "</div></li>").join("");
  const ev = (h.evidence || []).map((e) => {
    const meta = KIND[e.kind] || { c: "#9fb0c0", label: e.kind };
    return '<li><i class="nv-dot" style="background:' + meta.c + '"></i><div><div class="nv-desc">' + esc(e.description) +
      '</div><div class="nv-meta">' + esc(meta.label) + " · " + agoHtml(e.ts) + " · " + nf(e.distance_km) + " km from centre · w " +
      nf(e.weight, 2) + "</div>" + '<div class="nv-refs">' + (e.refs || []).slice(0, 3).map(refLink).join(" ") + "</div></div></li>";
  }).join("");
  const missing = status ? Object.entries(status).filter(([, s]) => s.note).map(([k, s]) =>
    "<li>" + esc((KIND[k] || { label: k }).label) + ": " + esc(s.note) + "</li>").join("") : "";
  const hist = (h.history || []).length;
  return '<div class="nv-insp">' +
    '<div class="nv-hd">' + esc(h.label || "CSG?") + (h.stale ? ' <em class="nv-stale">no longer active</em>' : "") + "</div>" +
    disclaimerHtml() +
    '<div class="nv-conf" title="heuristic confidence (needs ≥ 2 independent indicator kinds for > 0.5)">' +
    '<div class="nv-bar"><b style="width:' + pct + '%"></b></div><span>' + pct + "%</span></div>" +
    '<div class="kv"><span>Centre</span><div>' + nf(h.lat, 2) + ", " + nf(h.lon, 2) + "</div>" +
    "<span>Uncertainty</span><div>± " + nf(h.radius_km) + " km" + (h.centre_basis ? ' <span class="nv-meta">(' + esc(h.centre_basis) + ")</span>" : "") + "</div>" +
    "<span>First seen</span><div>" + agoHtml(h.first_seen) + "</div><span>Updated</span><div>" + agoHtml(h.last_updated) + "</div>" +
    (hist ? "<span>History</span><div>" + hist + " pass(es)</div>" : "") + "</div>" +
    '<div class="nv-sec">Indicator breakdown</div>' + rows +
    (h.air_track ? fitHtml(h.air_track) : "") +
    (names ? '<div class="nv-sec">Candidate names <span>candidates, not identifications</span></div><ul class="nv-names">' + names + "</ul>" : "") +
    '<div class="nv-sec">Evidence <span>' + (h.evidence_count || (h.evidence || []).length) + "</span></div>" +
    '<ul class="nv-ev">' + ev + "</ul>" +
    (missing ? '<div class="nv-sec">Signals without data</div><ul class="nv-missing">' + missing + "</ul>" : "") +
    "</div>";
}
