// Event timeline component (GET /api/events/{id}/timeline, or the "timeline" section of
// /api/intel). Reusable: a storyline view can pass a merged timeline (entries from several
// events, each carrying event_id) to the same renderer.
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
  return (day !== lastDay ? '<li class="tl-day">' + esc(day) + "</li>" : "") +
    '<li class="tl-e k-' + e.kind + (e.sensor ? " s-" + (SENSOR_CLS[e.sensor] || e.sensor) : "") + (hasGeo ? " geo" : "") + '" data-i="' + i + '">' +
    '<time title="' + esc(localHint(t, off) + (e.kind === "occurred" && e.lo ? "\nband " + bandText(e.lo, e.hi) : "")) + '">' + esc(when) + "</time>" +
    '<i class="tl-k" title="' + esc(k[1]) + '">' + k[0] + "</i>" +
    '<div class="tl-m">' + main + '<div class="tl-sub">' + flags + (prec ? '<span class="tl-p">' + esc(prec) + "</span>" : "") +
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
