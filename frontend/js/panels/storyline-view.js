// STORYLINE pane (dossier browser pane type "story"): one thread of linked events in the
// dossier style. GET /api/storylines/{id} (members with role and best-known time, links with
// their reasons) and GET /api/storylines/{id}/timeline (the members' own timelines merged).
//
// Every time shown carries its basis: "occurred" = a claimed band from the sources (with its
// precision), "first report" = only an upper bound on when it happened, "sensor" = an
// observation time. Nothing is ordered or placed more precisely than its basis allows.
//
// Globe playback: the scrubber (or Play) moves a time cursor through the thread; members and
// sensor hits appear on the globe in time order (StoryOverlay), the lanes view follows, and
// "Sync replay" also drives the app's own replay timeline (layers as of that time).
import { esc, nf } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";
import { eventCat, EVENT_STATUS } from "/static/js/symbology.js";
import { fmtUtc, localHint, bandText, sec, wireSections, chip, copyBtn, wireCopy, copyText, toast } from "/static/js/panels/dossier-kit.js";
import { renderTimeline, wireTimeline, renderLanes, wireLanes, ROLE, T_BASIS } from "/static/js/panels/timeline-view.js";
import { roleChips, spanText } from "/static/js/storylines.js";

const PREC = { minute: "stated time + zone", time_local: "stated local time ±1 h", part_of_day: "date + part of day", date: "date only", range: "stated range", upper_bound: "upper bound", exact: "timestamp" };
const SPEEDS = [["20", "20 s"], ["40", "40 s"], ["90", "90 s"]];

export const storyKey = (s) => "st:" + s.id;

function timeText(m) {
  if (m.t_basis === "occurred_claims") return "occurred " + (m.t_lo && m.t_hi ? bandText(m.t_lo, m.t_hi) : fmtUtc(m.t)) + " · " + (PREC[m.t_precision] || m.t_precision || "claimed");
  if (m.t_basis === "sensor_observed") return "observed " + fmtUtc(m.t) + (m.t_end && m.t_end !== m.t ? " → " + fmtUtc(m.t_end) : "");
  return "before " + fmtUtc(m.t) + " · first report (no stated time)";
}

function linkFor(m, story, byId) {
  const ls = (story.links || []).filter((l) => l.b === m.event_id || l.a === m.event_id);
  if (!ls.length) return "";
  const best = ls.reduce((a, b) => (b.score > a.score ? b : a));
  const other = byId[best.a === m.event_id ? best.b : best.a] || {};
  return '<div class="slv-why" title="Link score ' + nf(best.score, 2) + '"><span>linked to ' + esc((ROLE[other.role] || [other.role || ""])[0]) + "</span> " +
    best.reasons.filter((r) => r.kind !== "category").map((r) => '<i class="' + (r.points < 0 ? "neg" : "") + '">' + esc(r.text) + "</i>").join("") + "</div>";
}

function memberCards(story) {
  const byId = Object.fromEntries((story.members || []).map((m) => [m.event_id, m]));
  return '<ol class="slv-m">' + (story.members || []).map((m) => {
    const role = ROLE[m.role] || [m.role, "var(--dim)"], cat = eventCat(m.category);
    return '<li data-ev="' + esc(m.event_id) + '" style="--rc:' + role[1] + '"' + (m.lat != null ? ' data-fly="' + m.lat + "," + m.lon + '"' : "") + ">" +
      '<div class="slv-mh"><i class="sl-role" style="--rc:' + role[1] + '" title="' + esc(m.role_basis || "") + '">' + esc(role[0]) + "</i>" +
      '<time title="' + esc((T_BASIS[m.t_basis] || m.t_basis) + "\n" + localHint(m.t, null)) + '">' + esc(timeText(m)) + "</time></div>" +
      "<b>" + esc(m.title || m.event_id) + "</b>" +
      '<div class="slv-ms">' + esc(cat[0]) + (m.place_name ? " · " + esc(m.place_name) + (m.precision ? " (" + esc(m.precision) + ")" : "") : "") +
      (m.status ? " · " + esc(EVENT_STATUS[m.status] || m.status) : "") + (m.origin === "news" ? " · " + nf(m.sources) + " outlets / " + nf(m.mentions) + " reports" : " · " + esc(m.detector || "sensor")) + "</div>" +
      linkFor(m, story, byId) +
      '<div class="slv-ma"><button type="button" data-sel="' + esc(m.event_id) + '" title="Select on the globe">Select</button>' +
      '<button type="button" data-intel="' + esc(m.event_id) + '" title="Open the event intel pane">Intel</button></div></li>';
  }).join("") + "</ol>";
}

function occurredTable(tl) {
  const rows = (tl.occurred_by_event || []).map((o) => {
    const v = o.basis === "claims" ? bandText(o.lo, o.hi) + " · " + (o.qualifier ? o.qualifier + ", " : "") + (PREC[o.precision] || o.precision)
      : o.conflicts && o.conflicts.length ? "sources disagree (" + o.conflicts.length + " conflicting pair" + (o.conflicts.length > 1 ? "s" : "") + ")"
        : o.basis === "before_first_report" ? "before " + fmtUtc(o.hi) + " · no stated time" : "unknown";
    return "<tr" + (o.conflicts && o.conflicts.length ? ' class="cf"' : "") + "><td>" + esc((ROLE[o.event_role] || [o.event_role || ""])[0]) + "</td><td>" + esc(String(o.event_title || "").slice(0, 70)) + "</td><td>" + esc(v) + "</td></tr>";
  }).join("");
  return rows ? '<table class="ip-t slv-occ"><thead><tr><th>Role</th><th>Event</th><th>Occurred (basis)</th></tr></thead><tbody>' + rows + "</tbody></table>" : '<div class="dz-empty">No member timelines</div>';
}

function linksList(story) {
  const byId = Object.fromEntries((story.members || []).map((m) => [m.event_id, m]));
  const name = (id) => { const m = byId[id] || {}; return (ROLE[m.role] || [m.role || ""])[0] + " · " + String(m.title || id).slice(0, 48); };
  return (story.links || []).length ? '<ul class="slv-links">' + story.links.map((l) => '<li><div><b>' + nf(l.score, 2) + "</b> " + esc(name(l.a)) + " ↔ " + esc(name(l.b)) + "</div><small>" +
    l.reasons.map((r) => esc(r.text) + (r.points ? " (" + (r.points > 0 ? "+" : "") + nf(r.points, 2) + ")" : "")).join(" · ") + "</small></li>").join("") + "</ul>" : '<div class="dz-empty">No links</div>';
}

function brief(story, tl) {
  const L = ["## STORYLINE — " + story.title, "**Theatre:** " + story.theatre_label + " · **Span:** " + fmtUtc(story.t_first) + " (" + (T_BASIS[story.t_first_basis] || story.t_first_basis) + ") → " + fmtUtc(story.t_last) +
    " · " + story.n_events + " events", "", "**Events (best-known time, basis)**"];
  for (const m of story.members || []) L.push("- [" + (ROLE[m.role] || [m.role])[0] + "] " + timeText(m) + " — " + m.title + (m.place_name ? " (" + m.place_name + ")" : ""));
  L.push("", "**Links**");
  for (const l of story.links || []) L.push("- " + l.a + " ↔ " + l.b + " (" + nf(l.score, 2) + "): " + l.reasons.map((r) => r.text).join("; "));
  if (tl && tl.conflicts && tl.conflicts.length) L.push("", "**Caveat:** sources disagree on when " + tl.conflicts.length + " member event(s) happened");
  L.push("", "_Storyline " + story.id + " · generated " + fmtUtc(new Date().toISOString()) + " from NOCTUA records_");
  return L.join("\n");
}

// hooks: {head, stale, overlay: {show, fit, setTime, clear}, select(kind, id), intelEvent(id, label), replay(t | null)}
export async function renderStoryline(spec, body, hooks) {
  hooks.head({ title: spec.label || "Storyline", sub: "STORYLINE", icon: "story" });
  body.innerHTML = '<div class="dz-empty">Loading storyline…</div>';
  const [story, tl] = await Promise.all([fetchJson("/api/storylines/" + encodeURIComponent(spec.id), 15000),
    fetchJson("/api/storylines/" + encodeURIComponent(spec.id) + "/timeline", 30000).catch((e) => ({ error: String(e.message || e) }))]);
  if (hooks.stale()) return;
  const coord = story.lat != null ? { lat: story.lat, lon: story.lon, alt: 4e5 } : null;
  hooks.head({ title: story.title, sub: "STORYLINE // " + story.theatre_label + " · " + story.n_events + " events", icon: "story", coord });
  const places = (story.places || []).slice(0, 5).map((p) => p.name).join(" · ");
  const caveat = tl && tl.conflicts && tl.conflicts.length ? '<div class="slv-cav">Sources disagree on when ' + tl.conflicts.length + " member event" + (tl.conflicts.length > 1 ? "s" : "") + " happened — see Occurred per event</div>" : "";
  body.innerHTML = '<div class="dk-root slv">' +
    '<div class="dk-class st-' + esc(String(story.top_status || "").replace(/[^a-z-]/g, "")) + '"><span>STORYLINE // ' + esc(story.theatre_label.toUpperCase()) + "</span><b>" + story.n_events + ' EVENTS</b><span class="dk-r">' + esc(story.id) + "</span></div>" +
    '<header class="dk-head"><div class="dk-ht"><div class="dz-eyebrow">' + (story.categories || []).map((c) => esc(eventCat(c)[0])).join(" · ") + '</div><h2 class="dz-title">' + esc(story.title) + '</h2><div class="dz-sub">' + esc(places) + "</div></div></header>" +
    '<div class="dk-loc slv-span"><div><span>FIRST</span><b title="' + esc(localHint(story.t_first, tl && tl.local_offset_h)) + '">' + esc(fmtUtc(story.t_first)) + "</b><small>" + esc(T_BASIS[story.t_first_basis] || story.t_first_basis) + "</small></div>" +
    '<div><span>LATEST</span><b title="Latest member event time (reported or observed)">' + esc(fmtUtc(story.t_last)) + "</b></div>" +
    "<div><span>EVENTS</span><b>" + story.n_news + " news" + (story.n_sensor ? " + " + story.n_sensor + " sensor" : "") + "</b></div></div>" +
    '<div class="dk-chips">' + roleChips(story.roles) + chip(story.title_basis, "src") + "</div>" + caveat +
    '<div class="dk-tools"><button type="button" data-a="globe" title="Show the thread on the globe and fit its extent">Show on globe</button>' +
    '<button type="button" data-a="brief" title="Copy a Markdown brief of the thread">Brief</button>' + copyBtn(story.id, "copy id") + "</div>" +
    '<div class="slv-play"><button type="button" data-a="play" title="Play the thread on the globe">▶</button><input type="range" min="0" max="1000" value="1000" aria-label="Storyline time">' +
    '<output class="num">—</output><select title="Duration of a full playback">' + SPEEDS.map(([v, l]) => '<option value="' + v + '"' + (v === "40" ? " selected" : "") + ">" + l + "</option>").join("") + "</select>" +
    '<label title="Also move the app replay timeline (all layers as of that time)"><input type="checkbox" data-a="sync"> sync replay</label></div>' +
    sec("slv-lanes", "Timeline lanes", tl && !tl.error ? renderLanes(tl) : '<div class="dz-empty">' + esc((tl && tl.error) || "No timeline") + "</div>", { meta: "best-known times · UTC" }) +
    sec("slv-members", "Events in this thread", memberCards(story), { meta: "by best-known time" }) +
    sec("slv-occ", "Occurred per event", tl && !tl.error ? occurredTable(tl) : "", { meta: "claims never merged across events" }) +
    sec("slv-tl", "Full timeline", tl && !tl.error ? renderTimeline(tl) : "", { meta: "UTC · hover for local" }) +
    sec("slv-links", "Links and reasons", linksList(story), { meta: (story.links || []).length + " links", cls: "small" }) +
    sec("slv-prov", "Provenance", '<div class="prov"><div><i></i>Linked by the worker storyline job (embedding cosine, place, rare names, sensor evidence, time); id stable across passes</div><div><i></i>updated ' +
      esc(fmtUtc(story.updated_at)) + " · span and order by best-known event time, never processing time</div></div>", { cls: "small" }) + "</div>";
  const root = body.firstElementChild;
  wireSections(root); wireCopy(root);

  // ---- playback
  const members = story.members || [];
  // playback covers the thread itself: first member (best-known time) to its last activity
  const times = members.map((m) => Date.parse(m.t)).filter((t) => !isNaN(t));
  const ends = members.map((m) => Date.parse(m.t_end || m.t)).filter((t) => !isNaN(t));
  const lo = Math.min(...times) - 30 * 60e3, hi = Math.max(...ends, ...times, Date.parse(story.t_last) || 0);
  const slider = root.querySelector('.slv-play input[type="range"]'), out = root.querySelector(".slv-play output"), playBtn = root.querySelector('[data-a="play"]');
  const lanes = tl && !tl.error ? wireLanes(root, tl, { members, onPick: (p) => { setT(p.t); if (p.kind === "member" && p.item.lat != null && hooks.flyTo) hooks.flyTo(p.item.lat, p.item.lon, 1.5e5); } }) : null;
  let cur = null, timer = null, lastReplay = 0;
  const cards = [...root.querySelectorAll(".slv-m li")];
  function setT(t, fromPlay) {
    cur = t == null ? null : Math.max(lo, Math.min(hi, t));
    slider.value = cur == null ? 1000 : Math.round(((cur - lo) / Math.max(1, hi - lo)) * 1000);
    out.textContent = cur == null ? "all" : fmtUtc(cur);
    hooks.overlay.setTime(cur, Math.max(36e5, (hi - lo) / 12));
    if (lanes) lanes.setCursor(cur);
    cards.forEach((li, i) => { const t0 = Date.parse(members[i].t); li.classList.toggle("future", cur != null && t0 > cur); li.classList.toggle("now", cur != null && t0 <= cur && cur - t0 <= Math.max(36e5, (hi - lo) / 12)); });
    if (root.querySelector('[data-a="sync"]').checked && hooks.replay && cur != null && (!fromPlay || Date.now() - lastReplay > 1500)) { lastReplay = Date.now(); hooks.replay(cur); }
  }
  const stop = () => { clearInterval(timer); timer = null; playBtn.textContent = "▶"; };
  const play = () => {
    if (timer) return stop();
    if (cur == null || cur >= hi) setT(lo);
    playBtn.textContent = "❚❚";
    const secs = +root.querySelector(".slv-play select").value, step = (hi - lo) / (secs * 10);
    timer = setInterval(() => { if (hooks.stale() || !root.isConnected) return stop(); if (cur >= hi) return stop(); setT(cur + step, true); }, 100);
  };
  slider.addEventListener("input", () => { stop(); setT(+slider.value >= 1000 ? null : lo + (+slider.value / 1000) * (hi - lo)); });
  root.querySelector('[data-a="sync"]').addEventListener("change", (e) => { if (!e.target.checked && hooks.replay) hooks.replay(null); else if (cur != null) setT(cur); });
  root.addEventListener("click", async (e) => {
    const a = e.target.closest("[data-a]");
    if (a && a.dataset.a === "play") return play();
    if (a && a.dataset.a === "globe") { hooks.overlay.show(story, tl); hooks.overlay.fit(); setT(cur); return; }
    if (a && a.dataset.a === "brief") { const ok = await copyText(brief(story, tl)); return toast(a.parentElement, ok ? "Brief copied (Markdown)" : "Copy failed", !ok); }
    const ib = e.target.closest("[data-intel]");
    if (ib) { const m = members.find((x) => x.event_id === ib.dataset.intel); return hooks.intelEvent(ib.dataset.intel, m && m.title); }
    const li = e.target.closest(".slv-m li[data-ev]");
    if (li && !e.target.closest("button")) { const m = members.find((x) => x.event_id === li.dataset.ev); if (m) setT(Date.parse(m.t)); }
  });
  const tlEl = root.querySelector('[data-sec="slv-tl"] .dk-body');
  if (tlEl && tl && !tl.error) wireTimeline(tlEl, tl, { onHover: (x) => hooks.highlight && hooks.highlight(x ? [{ lat: x.lat, lon: x.lon, label: x.label }] : null) });
  hooks.overlay.show(story, tl);
  setT(null);
}
