// Context inspector: object dossier (glyph + threat colour header, tags, profile sparklines,
// key/value telemetry, provenance) and actions (Fly to, Follow, Trail, Pin).
import { $, esc, nf, agoHtml, kv, stateHtml } from "/static/js/ui.js";
import { fmtTime, tzLabel, utcTitle } from "/static/js/timefmt.js";
import { uiGlyph, threatColor, aircraftRole, ROLE_LABEL, shipClass, SHIP_LABEL } from "/static/js/symbology.js";
import { EventDossier } from "/static/js/panels/event-dossier.js";

const EMERG = { 7500: "Hijack", 7600: "Radio failure", 7700: "Emergency" };
const tsMs = (ts) => (ts == null ? NaN : typeof ts === "number" ? (ts < 1e11 ? ts * 1000 : ts) : Date.parse(ts));
const extras = (ex, skip) => Object.entries(ex || {}).filter(([k, v]) => !skip.includes(k) && v != null && v !== "").map(([k, v]) => [k.replace(/_/g, " "), esc(typeof v === "object" ? JSON.stringify(v) : v)]);

// Which profile placeholder to show: data wins; otherwise in-flight is "loading" (only while a trail
// request is outstanding), then error, then an answered-but-short trail is "empty", else not requested.
export function trailState({ inFlight, pts, error }) {
  if (pts && pts.length > 1) return "ready";
  if (inFlight) return "loading";
  if (error) return "error";
  return pts ? "empty" : "off";
}

function spark(label, pts, fmt) {
  const vals = pts.filter((v) => v != null && !isNaN(v));
  if (vals.length < 2) return "";
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const n = pts.length - 1;
  const xy = [];
  pts.forEach((v, i) => { if (v != null && !isNaN(v)) xy.push(((i / n) * 100).toFixed(2) + "," + (20 - ((v - min) / span) * 18).toFixed(2)); });
  const lastXY = xy[xy.length - 1].split(",");
  return '<div class="spark"><span>' + label + '</span><svg viewBox="0 0 100 22" preserveAspectRatio="none"><polygon points="0,22 ' + xy.join(" ") + ' 100,22"/><polyline points="' + xy.join(" ") +
    '"/><circle cx="' + lastXY[0] + '" cy="' + lastXY[1] + '" r="1.6"/></svg><b title="min ' + fmt(min) + " / max " + fmt(max) + '">' + fmt(vals[vals.length - 1]) + "</b></div>";
}

export class Inspector {
  constructor(actions) {
    this.el = $("right"); this.dz = $("dz"); this.a = actions;
    this.cur = null; this.points = null; this.trailReq = { inFlight: false, error: null };
    $("close-details").addEventListener("click", () => actions.close());
    $("d-fly").addEventListener("click", () => actions.fly());
    $("d-follow").addEventListener("click", () => actions.follow());
    $("d-trail").addEventListener("click", () => actions.trail());
    $("d-pin").addEventListener("click", () => actions.pin());
    this._ev = null;
    // event dossier hooks: camera, globe highlight, selection, intel pane, in-app browser
    this.evd = new EventDossier({
      flyTo: (lat, lon, alt) => actions.flyToPoint && actions.flyToPoint(lat, lon, alt),
      highlight: (pts) => actions.highlight && actions.highlight(pts),
      select: (kind, id) => actions.select && actions.select(kind, id),
      intel: () => actions.intel && actions.intel(),
      cameras: (el) => actions.cameras && actions.cameras(el),
      story: (id, label) => actions.story && actions.story(id, label),
      rerender: () => { if (this._ev) this.event(...this._ev); },
    });
  }
  get open() { return !this.el.classList.contains("hidden"); }
  hide() { this.el.classList.add("hidden"); this.cur = null; this.points = null; this.trailReq = { inFlight: false, error: null }; this._h = null; this._ev = null; this.evd.forget(); }
  _show(html, threat) {
    if (this._ev) { this._ev = null; this.evd.forget(); }
    this.el.classList.remove("hidden", "ev");
    this.el.style.setProperty("--threat", threat);
    if (html !== this._h) { this._h = html; this.dz.innerHTML = html; }
  }
  setButtons({ follow, following, trail, trailOn, pin, pinned }) {
    const f = $("d-follow"), t = $("d-trail"), p = $("d-pin");
    f.disabled = !follow; f.classList.toggle("on", !!following); f.textContent = following ? "Following" : "Follow";
    t.disabled = !trail; t.classList.toggle("on", !!trailOn);
    p.disabled = !pin; p.classList.toggle("on", !!pinned); p.textContent = pinned ? "Pinned" : "Pin";
  }
  setTrailState(inFlight, error) { this.trailReq = { inFlight, error: error || null }; if (this.cur) this.track(this.cur.kind, this.cur.it, this.cur.detail); }
  setTrail(points) { this.points = points; this.trailReq = { inFlight: false, error: null }; if (this.cur) this.track(this.cur.kind, this.cur.it, this.cur.detail); }

  _head(kind, it, eyebrow, title, sub) {
    const c = threatColor(kind, it);
    return '<header class="dz-head" style="--threat:' + c + '"><div class="dz-glyph" style="color:' + c + '">' + uiGlyph(kind, it) + "</div><div>" +
      '<div class="dz-eyebrow">' + esc(eyebrow) + '</div><h2 class="dz-title">' + esc(title) + '</h2><div class="dz-sub">' + esc(sub) + "</div></div></header>";
  }

  // aircraft / ships: `detail` is GET /api/track/{id} (or null)
  track(kind, it, detail) {
    this.cur = { kind, it, detail };
    const tr = (detail && detail.track) || {};
    const ex = { ...(tr.extra || {}), ...(it.extra || {}) };
    const air = kind === "aircraft";
    const role = air ? ROLE_LABEL[aircraftRole(it)] : SHIP_LABEL[shipClass(it)];
    const eyebrow = [air ? "Aircraft" : "Vessel", role, it.high_priority ? "High priority" : ""].filter(Boolean).join(" · ");
    const sub = [it.type_code, it.operator || it.category, it.reg || tr.reg].filter(Boolean).join(" · ") || it.id;
    const age = (Date.now() - tsMs(it.ts)) / 1000;
    const sq = ex.squawk && EMERG[ex.squawk];
    const tags = [it.high_priority ? '<span class="tag hp">High priority</span>' : "", it.is_mil ? '<span class="tag mil">Military</span>' : '<span class="tag">Civil</span>',
      sq ? '<span class="tag hp">Squawk ' + esc(ex.squawk) + " " + sq + "</span>" : "", age > 180 ? '<span class="tag stale">Stale ' + Math.round(age / 60) + " min</span>" : ""].join("");
    let prof = "";
    const pts = this.points || [];
    const tstate = trailState({ ...(this.trailReq || {}), pts: this.points });
    if (tstate === "ready") {
      prof = (air ? spark("ALT", pts.map((p) => p.alt_ft), (v) => nf(v) + " ft") : "") + spark("SPEED", pts.map((p) => p.speed_kts), (v) => nf(v) + " kt");
      const t0 = tsMs(pts[0].ts);
      prof = '<section class="dz-sec"><h3>Profile<small>' + pts.length + " fixes · since " + (isNaN(t0) ? "–" : '<span title="' + esc(utcTitle(t0)) + '">' + fmtTime(t0) + " " + esc(tzLabel(t0).split(" ")[0]) + "</span>") + "</small></h3>" + (prof || '<div class="dz-empty">No altitude / speed samples</div>') + "</section>";
    } else {
      const st = tstate === "loading" ? stateHtml("loading", "Loading trail…") : tstate === "error" ? stateHtml("err", "Trail unavailable", this.trailReq.error || "request failed")
        : tstate === "empty" ? stateHtml("empty", "No trail yet", "Stationary or newly tracked; fewer than two fixes in the last 6 h.") : stateHtml("empty", "Trail off", "Enable Trail to load the track history.");
      prof = '<section class="dz-sec"><h3>Profile</h3>' + st + "</section>";
    }
    const tele = air
      ? [["Altitude", it.alt_ft == null ? "–" : nf(it.alt_ft) + " ft · " + nf(it.alt_ft * 0.3048) + " m"], ["Speed", it.speed_kts == null ? "–" : nf(it.speed_kts) + " kt"],
        ["Heading", it.heading_deg == null ? "–" : nf(it.heading_deg) + "°"], ["Squawk", esc(ex.squawk)], ["Position", nf(it.lat, 4) + ", " + nf(it.lon, 4)], ["Last fix", agoHtml(it.ts)]]
      : [["Speed", it.speed_kts == null ? "–" : nf(it.speed_kts, 1) + " kt"], ["Heading", it.heading_deg == null ? "–" : nf(it.heading_deg) + "°"],
        ["Destination", esc(ex.destination)], ["Position", nf(it.lat, 4) + ", " + nf(it.lon, 4)], ["Last fix", agoHtml(it.ts)]];
    const ident = air
      ? [["Callsign", esc(it.name)], ["ICAO hex", esc(tr.ident || String(it.id).replace(/^air:/, ""))], ["Registration", esc(it.reg || tr.reg)], ["Type", esc(it.type_code)],
        ["Model", esc(ex.desc)], ["Operator", esc(it.operator)], ["Category", esc(it.category)], ["Role", esc(ex.role)], ...extras(ex, ["squawk", "desc", "role"])]
      : [["Name", esc(it.name)], ["MMSI / ID", esc(it.id)], ["IMO", esc(ex.imo)], ["Callsign", esc(ex.callsign)], ["Type", esc(it.type_code)], ["Operator", esc(it.operator)],
        ["Category", esc(it.category)], ...extras(ex, ["destination", "imo", "callsign"])];
    const pos = detail && detail.position;
    const prov = [pos && pos.source ? ["Feed", pos.source] : null, tr.first_seen ? ["First seen", agoHtml(tr.first_seen)] : null, tr.last_seen ? ["Track updated", agoHtml(tr.last_seen)] : null,
      ["Record", detail ? "Full record · /api/track" : "List item (full record unavailable)"]].filter(Boolean);
    const html = this._head(kind, it, eyebrow, it.name || it.id, sub) + '<div class="dz-tags">' + tags + "</div>" + (this.trackExtra ? this.trackExtra(kind, it) : "") + prof +
      '<section class="dz-sec"><h3>Telemetry</h3>' + kv(tele) + '</section><section class="dz-sec"><h3>Identity</h3>' + kv(ident) + "</section>" +
      '<section class="dz-sec"><h3>Provenance</h3><div class="prov">' + prov.map(([k, v]) => "<div><i></i>" + esc(k) + ": " + v + "</div>").join("") + "</div></section>";
    this._show(html, threatColor(kind, it));
  }

  // news / sensor event: `it` is the slim list item, `detail` is GET /api/events/{id} (or null
  // while loading). Rendered by EventDossier (panels/event-dossier.js), which owns its DOM and
  // only rebuilds when the record changes.
  // nearNuc: {site, km} of the closest known nuclear site within 50 km (client-side), or null
  event(it, detail, nearNuc, detailErr) {
    this.cur = null;
    this._ev = [it, detail, nearNuc, detailErr];
    this.el.classList.remove("hidden");
    this.el.classList.add("ev");
    this.el.style.setProperty("--threat", threatColor("events", it));
    this._h = null; // the dossier owns #dz: the next non-event render must write it
    if (this.evd.render(this.dz, it, detail, nearNuc, detailErr)) this.el.scrollTop = this._evScrollFor === it.id ? this.el.scrollTop : 0;
    this._evScrollFor = it.id;
  }

  observation(kind, it) {
    this.cur = null;
    let html;
    if (kind === "fires") {
      html = this._head(kind, it, "Thermal anomaly · VIIRS / MODIS", "Fire detection", nf(it.frp, 1) + " MW radiative power") + '<section class="dz-sec"><h3>Detection</h3>' +
        kv([["FRP", it.frp == null ? "–" : nf(it.frp, 1) + " MW"], ["Confidence", esc(it.confidence)], ["Satellite", esc(it.satellite)], ["Day / night", it.daynight === "D" ? "Day" : it.daynight === "N" ? "Night" : esc(it.daynight)],
          ["Position", nf(it.lat, 4) + ", " + nf(it.lon, 4)], ["Detected", agoHtml(it.ts)], ["ID", esc(it.id)]]) + '</section><section class="dz-sec"><h3>Provenance</h3><div class="prov"><div><i></i>NASA FIRMS</div></div></section>';
    } else {
      const url = /^https?:\/\//.test(it.url || "") ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">USGS event page</a>' : "";
      html = this._head(kind, it, "Seismic event" + (it.mag >= 5 ? " · Significant" : ""), "M " + nf(it.mag, 1) + " " + (it.place || "Earthquake"), (it.event_type || "earthquake") + " · depth " + nf(it.depth_km, 1) + " km") +
        '<section class="dz-sec"><h3>Event</h3>' + kv([["Magnitude", nf(it.mag, 1)], ["Place", esc(it.place)], ["Depth", it.depth_km == null ? "–" : nf(it.depth_km, 1) + " km"], ["Type", esc(it.event_type)],
          ["Position", nf(it.lat, 4) + ", " + nf(it.lon, 4)], ["Time", agoHtml(it.ts)], ["ID", esc(it.id)]]) + '</section><section class="dz-sec"><h3>Provenance</h3><div class="prov"><div><i></i>USGS ' + (url || "") + "</div></div></section>";
    }
    this._show(html, threatColor(kind, it));
  }

  // Self-rendering modules (webcams, carrier hypotheses): returns the container they draw into.
  module(eyebrow, tint) {
    this.cur = null;
    this._show('<div class="dz-mod-head">' + esc(eyebrow) + '</div><div class="dz-mod"></div>', tint);
    this._h = null; // the module mutates the DOM; never skip the next render
    return this.dz.querySelector(".dz-mod");
  }

  reference(ref, title, bodyHtml) {
    this.cur = null;
    const label = { airport: "Airport", medairport: "Airport", runway: "Runway", port: "Seaport", base: "Military installation", chokepoint: "Maritime chokepoint", boundary: "Boundary",
      nuclear: "Nuclear site", historical: "Historical conflict · UCDP GED", corridor: "Military movements · corridor", leg: "Military movements · flight leg", surge: "Military movements · surge", naval: "Military movements · naval unit" }[ref.kind] || ref.label || "Reference";
    const it = ref.item || {};
    const mov = /^(corridor|leg|surge|naval)$/.test(ref.kind);
    const tint = ref.tint || (ref.kind === "nuclear" ? "#ffd24a" : ref.kind === "historical" ? "#8f86c9" : ref.kind === "surge" ? "#f2ad3d" : ref.kind === "naval" ? "#ff4b55" : mov ? "#36c5ff" : "#3ddcc2");
    const head = this._head("ref", it, (mov ? "" : "Reference · ") + label, title, it.lat != null ? nf(it.lat, 3) + ", " + nf(it.lon, 3) : "").replace(/--threat:[^;"]*/, "--threat:" + tint);
    const html = head + '<section class="dz-sec">' + bodyHtml + "</section>";
    this._show(html, tint);
  }
}
