// Event timeline component (GET /api/events/{id}/timeline, or the "timeline" section of
// /api/intel). Reusable: a storyline passes its merged timeline (GET /api/storylines/{id}/
// timeline: entries from several events, each carrying event_id / event_title / event_role)
// to the same renderer, and its multi-lane view (renderLanes / wireLanes below).
//
// Three kinds of time are drawn apart and labelled: OCCURRED (claimed, as a band with its
// precision; conflicting claims side by side), REPORTED (publication times) and OBSERVED
// (sensor scan / origin / satellite pass times). Milestones (first report, first sensor,
// first imagery, status changes, ingest) are flagged. All times UTC; hover shows site-local
// and your local time; T± is relative to the first report.
import { esc } from "/static/js/ui.js";
import { fmtUtc, localHint, relMin, bandText } from "/static/js/panels/dossier-kit.js";

const KIND = { occurred: ["OCC", "Occurred (claimed)"], reported: ["RPT", "Reported"], observed: ["OBS", "Observed by sensor"], milestone: ["SYS", "System / status"] };
const FLAG = { first_report: "First report", first_sensor: "First sensor detection", first_imagery: "First post-event imagery", conflict: "Conflicting claim", future: "Scheduled / future" };
const PREC = {
  minute: "stated time + zone", time_local: "stated time, zone assumed local ±1 h", part_of_day: "date + part of day", date: "date only", range: "stated range",
  exact: "timestamp", scan: "satellite scan time", origin: "origin time", pass: "satellite pass", recorded: "system clock", upper_bound: "upper bound only",
};
const SENSOR_CLS = { fire: "fire", quake: "quake", hazard: "hazard", aircraft: "air", ship: "sea", imagery: "img" };
const tms = (s) => (s ? Date.parse(s) : NaN);
const entryT = (e) => tms(e.t || e.lo);

function occurredHead(tl) {
  const o = tl.occurred || {}, off = tl.local_offset_h;
  if (o.conflicts && o.conflicts.length) {
    const claims = (tl.entries || []).filter((e) => e.kind === "occurred" && (e.flags || []).includes("conflict"));
    return '<div class="tl-occ conflict"><b>OCCURRED · sources disagree</b><div class="tl-claims">' + claims.map((c) =>
      '<div title="' + esc(localHint(c.lo, off)) + '"><span>' + esc(bandText(c.lo, c.hi)) + "</span><small>" + esc(c.source || "") + " · “" + esc(c.phrase) + "”</small></div>").join("") + "</div></div>";
  }
  if (o.basis === "claims") {
    return '<div class="tl-occ" title="' + esc(localHint(o.lo, off)) + '"><b>OCCURRED</b><span>' + esc(bandText(o.lo, o.hi)) + "</span><small>" +
      esc([o.qualifier, PREC[o.precision] || o.precision, o.tz_basis === "longitude" ? "local zone by longitude" : "", o.n_claims > 1 ? o.n_claims + " agreeing claims" : "1 claim"].filter(Boolean).join(" · ")) + "</small></div>";
  }
  if (o.basis === "before_first_report") return '<div class="tl-occ weak"><b>OCCURRED</b><span>before ' + esc(fmtUtc(o.hi)) + "</span><small>no stated time · upper bound = first report</small></div>";
  return '<div class="tl-occ weak"><b>OCCURRED</b><span>unknown</span></div>';
}

// overview strip: claimed bands, report ticks, sensor dots, milestone diamonds
function strip(tl) {
  const first = tms((tl.anchor || {}).first_report);
  const es = (tl.entries || []).filter((e) => !isNaN(entryT(e)) && !(e.milestone === "status" && e.baseline));
  const inScale = es.filter((e) => isNaN(first) || entryT(e) >= first - 3 * 86400e3);
  if (inScale.length < 2) return "";
  let lo = Math.min(...inScale.map((e) => entryT(e))), hi = Math.max(...inScale.map((e) => (e.hi ? tms(e.hi) : entryT(e))));
  const pad = Math.max(60e3 * 5, (hi - lo) * 0.04); lo -= pad; hi += pad;
  const W = 300, x = (t) => (((Math.max(lo, Math.min(hi, t)) - lo) / (hi - lo)) * W).toFixed(1);
  let svg = "";
  const occ = inScale.filter((e) => e.kind === "occurred" && !e.future);
  occ.forEach((e, i) => { svg += '<rect class="occ' + ((e.flags || []).includes("conflict") ? " cf" : "") + '" x="' + x(tms(e.lo)) + '" y="' + (3 + i * 4) + '" width="' + Math.max(1.5, x(tms(e.hi)) - x(tms(e.lo))).toFixed(1) + '" height="5"><title>' + esc("Occurred (claimed): " + bandText(e.lo, e.hi)) + "</title></rect>"; });
  for (const e of inScale) {
    const t = entryT(e), cx = x(t);
    if (e.kind === "reported") svg += '<line class="rpt" x1="' + cx + '" x2="' + cx + '" y1="14" y2="26"><title>' + esc("Reported " + fmtUtc(e.t) + " · " + (e.source || "")) + "</title></line>";
    else if (e.kind === "observed") svg += '<circle class="obs ' + (SENSOR_CLS[e.sensor] || "") + '" cx="' + cx + '" cy="20" r="3"><title>' + esc((e.label || "") + " " + fmtUtc(e.t)) + "</title></circle>";
    else if (e.kind === "milestone") svg += '<path class="ms" d="M' + cx + " 28 l3 3 l-3 3 l-3 -3z" + '"><title>' + esc((e.label || "") + " " + fmtUtc(e.t)) + "</title></path>";
  }
  if (!isNaN(first)) svg += '<line class="t0" x1="' + x(first) + '" x2="' + x(first) + '" y1="0" y2="36"/>';
  return '<div class="tl-strip"><svg viewBox="0 0 ' + W + ' 36" preserveAspectRatio="none" aria-hidden="true">' + svg + '</svg><div class="tl-axis"><span>' + esc(fmtUtc(lo)) +
    "</span><span>T0 first report</span><span>" + esc(fmtUtc(hi)) + "</span></div></div>";
}

function row(e, i, tl, lastDay) {
  const off = tl.local_offset_h, k = KIND[e.kind] || ["·", e.kind];
  const t = e.t || e.lo, day = t ? fmtUtc(t).slice(0, 10) : "";
  const when = e.kind === "occurred" ? (e.lo ? fmtUtc(e.lo, { time: true }) : "?") : fmtUtc(t, { time: true });
  const flags = (e.flags || []).map((f) => '<em class="tl-f ' + f + '">' + esc(FLAG[f] || f) + "</em>").join("");
  let main = "<b>" + esc(e.kind === "reported" ? e.source || "Reported" : e.label || k[1]) + "</b>";
  if (e.kind === "reported") main += e.title ? '<span class="tl-d">' + esc(e.title) + "</span>" : "";
  else if (e.kind === "occurred") main += '<span class="tl-d">“' + esc(e.phrase) + "” — " + esc(e.source || "") + ((e.also || []).length ? " +" + e.also.length + " agreeing" : "") + "</span>";
  else if (e.detail) main += '<span class="tl-d">' + esc(e.detail) + "</span>";
  const prec = (e.kind === "occurred" && e.lo ? "band " + bandText(e.lo, e.hi) + " · " : "") + (PREC[e.precision] || e.precision || "");
  const hasGeo = e.lat != null && e.lon != null;
  // storyline timelines: which member event the row belongs to (and its role)
  const ev = e.event_id && e.event_title ? '<em class="tl-ev" style="--rc:' + ((ROLE[e.event_role] || [])[1] || "var(--dim)") + '" title="' + esc((ROLE[e.event_role] || [e.event_role || ""])[0] + " · " + e.event_title) + '" data-ev="' + esc(e.event_id) + '">' +
    esc((ROLE[e.event_role] || [e.event_role || ""])[0]) + " · " + esc(String(e.event_title).slice(0, 42)) + "</em>" : "";
  return (day !== lastDay ? '<li class="tl-day">' + esc(day) + "</li>" : "") +
    '<li class="tl-e k-' + e.kind + (e.sensor ? " s-" + (SENSOR_CLS[e.sensor] || e.sensor) : "") + (hasGeo ? " geo" : "") + '" data-i="' + i + '">' +
    '<time title="' + esc(localHint(t, off) + (e.kind === "occurred" && e.lo ? "\nband " + bandText(e.lo, e.hi) : "")) + '">' + esc(when) + "</time>" +
    '<i class="tl-k" title="' + esc(k[1]) + '">' + k[0] + "</i>" +
    '<div class="tl-m">' + main + '<div class="tl-sub">' + ev + flags + (prec ? '<span class="tl-p">' + esc(prec) + "</span>" : "") +
    (e.t_rel_min != null ? '<span class="tl-r">' + esc(relMin(e.t_rel_min)) + "</span>" : "") +
    (e.url && /^https?:/i.test(e.url) ? '<a href="' + esc(e.url) + '" target="_blank" rel="noopener noreferrer">open</a>' : "") + "</div></div></li>";
}

export function renderTimeline(tl) {
  if (!tl) return '<div class="dz-empty">No timeline</div>';
  if (tl.error) return '<div class="dz-empty">Timeline unavailable: ' + esc(tl.error) + "</div>";
  const es = tl.entries || [];
  let last = "", rows = "";
  es.forEach((e, i) => { rows += row(e, i, tl, last); last = (e.t || e.lo) ? fmtUtc(e.t || e.lo).slice(0, 10) : last; });
  const hasStatus = es.some((e) => e.milestone === "status");
  const note = hasStatus && tl.status_history_recorded ? "" : '<div class="tl-note">Status changes are recorded from this build on; earlier transitions of this event were not stored.</div>';
  const unplaced = (tl.unplaced || []).length ? '<div class="tl-unp"><b>Stated, not placeable</b>' + tl.unplaced.map((u) => "<div>“" + esc(u.phrase) + "” — " + esc(u.source || "") + "</div>").join("") + "</div>" : "";
  return '<div class="tl">' + occurredHead(tl) + strip(tl) + '<ol class="tl-list">' + rows + "</ol>" + unplaced + note + "</div>";
}

// hover -> onHover(entry | null) (globe highlight for geolocated rows)
export function wireTimeline(root, tl, { onHover } = {}) {
  const list = root.querySelector(".tl-list");
  if (!list || !tl) return;
  let cur = null;
  list.addEventListener("mouseover", (e) => {
    const li = e.target.closest(".tl-e");
    const ent = li ? tl.entries[+li.dataset.i] : null;
    if (ent === cur) return;
    cur = ent;
    if (onHover) onHover(ent && ent.lat != null ? ent : null);
  });
  list.addEventListener("mouseleave", () => { cur = null; if (onHover) onHover(null); });
}

// ---------------------------------------------------------------- multi-lane view
// Zoomable multi-lane view of a merged (storyline) timeline: one lane per kind of time
// (members' best-known times, occurred bands, reports, sensors, imagery, status), the same
// entries and precision labels as the list above. Wheel zooms around the pointer, drag pans,
// double-click fits; a cursor line follows the globe playback. Members are drawn as their
// precision band (t_lo → t_hi) when their time is a claimed band, else as a point.
export const ROLE = { origin: ["ORIGIN", "#ff5a5a"], development: ["DEV", "#f2ad3d"], follow_up: ["FOLLOW-UP", "#ff8a3d"], reaction: ["REACTION", "#5ee3ea"], sensor: ["SENSOR", "#a99bff"] };
export const T_BASIS = { occurred_claims: "occurred (claimed band)", first_report: "first report (upper bound)", sensor_observed: "sensor observation" };
const LANES = [["events", "Events"], ["occurred", "Occurred"], ["reports", "Reports"], ["sensors", "Sensors"], ["imagery", "Imagery"], ["status", "Status"]];
const STEPS = [60e3 * 15, 36e5, 36e5 * 3, 36e5 * 6, 36e5 * 12, 864e5, 864e5 * 2, 864e5 * 7];
const LABEL_W = 64, LANE_H = 20;

export function renderLanes(tl) {
  if (!tl || tl.error) return "";
  return '<div class="tll" aria-label="Storyline timeline lanes: wheel to zoom, drag to pan, double-click to fit"><div class="tll-svg"></div>' +
    '<div class="tll-foot"><span class="tll-range"></span><span>wheel zoom · drag pan · dbl-click fit · UTC</span></div></div>';
}

// onPick({kind: "member" | "entry" | "time", item, t}) on click; returns {setCursor(ms | null), fit(), range()}
export function wireLanes(root, tl, { members = [], onPick } = {}) {
  const none = { setCursor() {}, fit() {}, range: () => null };
  const box = root.querySelector(".tll"), host = root.querySelector(".tll-svg");
  if (!box || !host || !tl) return none;
  const marks = [];
  for (const m of members) {
    const t = tms(m.t);
    if (isNaN(t)) continue;
    const band = m.t_basis === "occurred_claims" && m.t_lo && m.t_hi;
    marks.push({ lane: "events", lo: band ? tms(m.t_lo) : t, hi: band ? tms(m.t_hi) : t, t, kind: "member", item: m, cls: "m-" + (m.role || ""),
      tip: (ROLE[m.role] || [m.role])[0] + " · " + (m.title || m.event_id) + "\n" + fmtUtc(m.t) + " · " + (T_BASIS[m.t_basis] || m.t_basis) + (m.t_precision && band ? " · " + (PREC[m.t_precision] || m.t_precision) : "") });
  }
  for (const e of tl.entries || []) {
    const lane = e.lane || (e.kind === "occurred" ? "occurred" : e.kind === "reported" ? "reports" : e.kind === "observed" ? (e.sensor === "imagery" ? "imagery" : "sensors") : e.milestone === "status" ? "status" : null);
    if (!lane || !LANES.some((l) => l[0] === lane)) continue;
    const t = entryT(e);
    if (isNaN(t) || (e.milestone === "status" && e.baseline)) continue;
    const occ = e.kind === "occurred";
    marks.push({ lane, lo: occ ? tms(e.lo) : t, hi: occ ? tms(e.hi) : t, t, kind: "entry", item: e,
      cls: (e.sensor ? "s-" + (SENSOR_CLS[e.sensor] || e.sensor) : "") + ((e.flags || []).includes("conflict") ? " cf" : ""),
      tip: (e.kind === "reported" ? "Reported · " + (e.source || "") : e.label || e.kind) + "\n" + (occ ? bandText(e.lo, e.hi) : fmtUtc(e.t)) + (e.event_title ? "\n" + e.event_title : "") + (e.detail ? "\n" + e.detail : "") });
  }
  if (!marks.length) { host.innerHTML = '<div class="dz-empty">No placeable times</div>'; return none; }
  const all = [Math.min(...marks.map((m) => m.lo)), Math.max(...marks.map((m) => m.hi))];
  // the default view frames the members (± 2 days of their evidence), not e.g. a baseline
  // image weeks before; zoom out / pan for the rest
  const mem = marks.filter((m) => m.kind === "member"), near = 2 * 864e5;
  const focus = mem.length ? marks.filter((m) => m.hi >= Math.min(...mem.map((x) => x.lo)) - near && m.lo <= Math.max(...mem.map((x) => x.hi)) + near) : marks;
  const fit0 = [Math.min(...focus.map((m) => m.lo)), Math.max(...focus.map((m) => m.hi))];
  const pad0 = Math.max(30 * 60e3, (fit0[1] - fit0[0]) * 0.04);
  let v0 = fit0[0] - pad0, v1 = fit0[1] + pad0, cursor = null;
  const W = () => Math.max(220, host.clientWidth || 600);
  const x = (t, w) => LABEL_W + ((t - v0) / (v1 - v0)) * (w - LABEL_W - 8);
  const draw = () => {
    const w = W(), H = LANES.length * LANE_H + 18;
    const span = v1 - v0, step = STEPS.find((s) => span / s <= 8) || 864e5 * 14;
    let s = '<svg width="' + w + '" height="' + H + '" viewBox="0 0 ' + w + " " + H + '">';
    LANES.forEach(([, label], i) => { s += '<rect class="ln-bg' + (i % 2 ? " odd" : "") + '" x="0" y="' + i * LANE_H + '" width="' + w + '" height="' + LANE_H + '"/><text class="ln-l" x="4" y="' + (i * LANE_H + 13) + '">' + label + "</text>"; });
    for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) {
      const px = x(t, w).toFixed(1), d = new Date(t).toISOString();
      s += '<line class="ln-tick" x1="' + px + '" x2="' + px + '" y1="0" y2="' + (H - 14) + '"/><text class="ln-tl" x="' + px + '" y="' + (H - 3) + '">' + (step >= 864e5 ? d.slice(5, 10) : d.slice(5, 10) + " " + d.slice(11, 16)) + "</text>";
    }
    marks.forEach((m, i) => {
      if (m.hi < v0 || m.lo > v1) return;
      const li = LANES.findIndex((l) => l[0] === m.lane), y = li * LANE_H, cy = y + LANE_H / 2;
      const fut = cursor != null && m.t > cursor ? " future" : "";
      const a = Math.max(LABEL_W, x(m.lo, w)), b = Math.min(w - 4, x(m.hi, w)), px = x(m.t, w).toFixed(1);
      const title = "<title>" + esc(m.tip) + "</title>", di = '" data-i="' + i + '"';
      if (b - a > 3) s += '<rect class="mk band ' + m.cls + fut + di + ' x="' + a.toFixed(1) + '" y="' + (y + 5) + '" width="' + (b - a).toFixed(1) + '" height="' + (LANE_H - 10) + '">' + title + "</rect>";
      if (m.lane === "reports") s += '<line class="mk rpt' + fut + di + ' x1="' + px + '" x2="' + px + '" y1="' + (y + 4) + '" y2="' + (y + LANE_H - 4) + '">' + title + "</line>";
      else if (m.lane === "status") s += '<path class="mk ms' + fut + di + ' d="M' + px + " " + (cy - 5) + ' l5 5 l-5 5 l-5 -5z">' + title + "</path>";
      else if (m.lane === "imagery") s += '<rect class="mk img' + fut + di + ' x="' + (px - 4) + '" y="' + (cy - 4) + '" width="8" height="8">' + title + "</rect>";
      else if (m.lane === "events" || b - a <= 3) s += '<circle class="mk ' + m.cls + fut + di + ' cx="' + px + '" cy="' + cy + '" r="' + (m.lane === "events" ? 5 : 3.5) + '">' + title + "</circle>";
    });
    if (cursor != null && cursor >= v0 && cursor <= v1) s += '<line class="ln-cur" x1="' + x(cursor, w).toFixed(1) + '" x2="' + x(cursor, w).toFixed(1) + '" y1="0" y2="' + (H - 14) + '"/>';
    host.innerHTML = s + "</svg>";
    const r = box.querySelector(".tll-range");
    if (r) r.textContent = fmtUtc(v0) + " → " + fmtUtc(v1);
  };
  const tAt = (clientX) => { const rc = host.getBoundingClientRect(), w = W(); return v0 + ((clientX - rc.left - LABEL_W) / (w - LABEL_W - 8)) * (v1 - v0); };
  host.addEventListener("wheel", (e) => {
    e.preventDefault();
    const t = tAt(e.clientX), f = (t - v0) / (v1 - v0);
    const span = Math.max(10 * 60e3, Math.min((all[1] - all[0]) * 6 + 864e5, (v1 - v0) * (e.deltaY > 0 ? 1.25 : 0.8)));
    v0 = t - f * span; v1 = v0 + span; draw();
  }, { passive: false });
  let drag = null;
  host.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, v0, v1, moved: false, target: e.target }; });
  host.addEventListener("pointermove", (e) => {
    if (!drag || !(e.buttons & 1)) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 3) drag.moved = true;
    if (!drag.moved) return;
    const dt = (dx / (W() - LABEL_W - 8)) * (drag.v1 - drag.v0);
    v0 = drag.v0 - dt; v1 = drag.v1 - dt; draw();
  });
  host.addEventListener("pointerup", (e) => {
    const d = drag; drag = null;
    if (!d || d.moved || !onPick) return;
    const mk = d.target && d.target.closest && d.target.closest(".mk"), m = mk ? marks[+mk.dataset.i] : null;
    onPick(m ? { kind: m.kind, item: m.item, t: m.t } : { kind: "time", t: tAt(e.clientX) });
  });
  const fit = () => { v0 = fit0[0] - pad0; v1 = fit0[1] + pad0; draw(); };
  host.addEventListener("dblclick", fit);
  if (typeof ResizeObserver === "function") new ResizeObserver(() => draw()).observe(host);
  draw();
  return {
    setCursor(t) {
      cursor = t == null ? null : +t;
      if (cursor != null && (cursor < v0 || cursor > v1)) { const span = v1 - v0; v0 = cursor - span / 2; v1 = v0 + span; }
      draw();
    },
    fit, range: () => [all[0], all[1]],
  };
}
