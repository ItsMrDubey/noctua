// Satellite before/after viewer for an event (payload of GET /api/imagery/{event_id}, also
// the "imagery" section of /api/intel).
//
// Both images present: a swipe-compare frame (drag the divider or use the arrow keys).
// One image: that frame plus the real state of the other (awaiting a clear pass with the
// latest pass date and cloud cover, next check, projected next Sentinel-2 pass, failure
// text). Overlays are placed from the crop bounds: the event location, the crop centre when
// it was moved to the impact site / FIRMS hotspot hull, and evidence points. Click a frame
// for a full-size lightbox. Images load eagerly (the old loading="lazy" inside the inspector's
// own scroll container left them unrequested until scrolled into view, showing only the alt
// text) into fixed-aspect boxes, so nothing shifts while they decode.
import { esc, nf } from "/static/js/ui.js";
import { fmtUtc, since } from "/static/js/panels/dossier-kit.js";

const BASIS = { impact_site: "impact site", firms_hull: "FIRMS hotspot hull", event: "event location" };
const ROLE = { before: "Before", after: "After" };

// fraction position of a point inside a crop's bounds (null when outside / no bounds)
function frac(bounds, lat, lon) {
  if (!bounds || bounds.west == null || lat == null) return null;
  const x = (lon - bounds.west) / (bounds.east - bounds.west), y = (bounds.north - lat) / (bounds.north - bounds.south);
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}
function overlays(a, ev, points) {
  const b = a && a.bounds;
  if (!b) return "";
  let h = "";
  const evp = ev && ev.lat != null ? frac(b, ev.lat, ev.lon) : b.marker;
  if (evp) h += '<i class="im-mk ev" style="left:' + (evp.x * 100).toFixed(2) + "%;top:" + (evp.y * 100).toFixed(2) + '%" title="Reported event location"></i>';
  const tg = b.target;
  if (tg && tg.basis && tg.basis !== "event") {
    const p = frac(b, tg.lat, tg.lon);
    if (p) h += '<i class="im-mk tg" style="left:' + (p.x * 100).toFixed(2) + "%;top:" + (p.y * 100).toFixed(2) + '%" title="Crop centre: ' + esc(BASIS[tg.basis] || tg.basis) + '"></i>';
  }
  for (const p of points || []) {
    const f = frac(b, p.lat, p.lon);
    if (f) h += '<i class="im-mk pt" style="left:' + (f.x * 100).toFixed(2) + "%;top:" + (f.y * 100).toFixed(2) + '%" title="' + esc(p.label || "") + '"></i>';
  }
  return h;
}
const caption = (a) => [ROLE[a.role], fmtUtc(a.acquired_at).slice(0, 16), a.cloud_pct != null ? "cloud " + nf(a.cloud_pct, a.cloud_pct < 1 ? 1 : 0) + "%" : "",
  a.collection, a.kind === "preview" ? "preview (low-res)" : a.bounds && a.bounds.gsd ? a.bounds.gsd + " m/px" : ""].filter(Boolean).join(" · ");
const ratio = (a) => (a && a.bounds && a.bounds.px ? a.bounds.px[0] + " / " + a.bounds.px[1] : "1 / 1");

function statusCard(role, a, watch) {
  const w = watch || {};
  if (!a) return '<div class="im-st"><b>' + ROLE[role] + "</b><span>No imagery job for this event yet (eligible events are processed every few minutes)</span></div>";
  if (a.status === "awaiting_after") {
    const lines = [];
    lines.push(a.acquired_at ? "Latest pass " + fmtUtc(a.acquired_at).slice(0, 10) + " · cloud " + nf(a.cloud_pct) + "% > " + nf(w.max_cloud_pct) + "% limit" : "No post-event pass yet");
    if (w.next_pass_est) lines.push('Next Sentinel-2 pass ≈ <b title="' + esc(w.next_pass_basis || "") + '">' + esc(fmtUtc(w.next_pass_est).slice(0, 10)) + "</b> (est.)");
    if (a.next_try_at) lines.push("Next check " + esc(fmtUtc(a.next_try_at)) + (a.checked_at ? " · last " + esc(since(a.checked_at)) + " ago" : ""));
    if (w.watch_until) lines.push("Watch ends " + esc(fmtUtc(w.watch_until).slice(0, 10)));
    return '<div class="im-st wait"><b>' + ROLE[role] + " · awaiting clear pass</b>" + lines.map((l) => "<span>" + l + "</span>").join("") + "</div>";
  }
  if (a.status === "no_scene") return '<div class="im-st"><b>' + ROLE[role] + " · none</b><span>" + esc(a.error || "No cloud-free scene in the lookback window") + "</span></div>";
  if (a.status === "failed") return '<div class="im-st bad"><b>' + ROLE[role] + " · failed (" + nf(a.attempts) + " attempts)</b><span>" + esc(a.error || "") + "</span>" +
    (a.next_try_at ? "<span>Retry " + esc(fmtUtc(a.next_try_at)) + "</span>" : "") + "</div>";
  return '<div class="im-st"><b>' + ROLE[role] + "</b><span>" + esc(a.status) + "</span></div>";
}

// `points`: [{lat, lon, label}] evidence to overlay; `ev`: {lat, lon}
export function imageryHtml(j, ev, points) {
  if (!j) return '<div class="dz-empty">Loading imagery…</div>';
  if (j.error) return '<div class="dz-empty">Imagery unavailable: ' + esc(j.error) + "</div>";
  const by = Object.fromEntries((j.assets || []).map((a) => [a.role, a]));
  const ok = (r) => by[r] && by[r].status === "ok" && by[r].url;
  const credit = (j.attributions || []).join(" · ");
  let h = "";
  if (ok("before") && ok("after")) {
    const b = by.before, a = by.after;
    h = '<div class="im-frame im-cmp" tabindex="0" role="slider" aria-label="Before / after divider" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50" style="aspect-ratio:' + ratio(b) + ';--pos:50%">' +
      '<img class="im-a" decoding="async" alt="" src="' + esc(a.url) + '"><div class="im-b"><img decoding="async" alt="" src="' + esc(b.url) + '"></div>' + overlays(b, ev, points) +
      '<div class="im-div"><i></i></div><span class="im-lab l">BEFORE ' + esc(fmtUtc(b.acquired_at).slice(0, 10)) + '</span><span class="im-lab r">AFTER ' + esc(fmtUtc(a.acquired_at).slice(0, 10)) + "</span>" +
      '<button type="button" class="im-zoom" title="Enlarge">⤢</button></div>' +
      '<div class="im-cap">' + esc(caption(b)) + "</div><div class=\"im-cap\">" + esc(caption(a)) + (a.change_score != null ? " · change " + nf(a.change_score * 100, 1) + "%" : "") + "</div>";
  } else {
    const have = ok("before") ? "before" : ok("after") ? "after" : null;
    if (have) {
      const a = by[have];
      h = '<div class="im-frame" style="aspect-ratio:' + ratio(a) + '"><img decoding="async" alt="" src="' + esc(a.url) + '">' + overlays(a, ev, points) +
        '<span class="im-lab l">' + ROLE[have].toUpperCase() + " " + esc(fmtUtc(a.acquired_at).slice(0, 10)) + '</span><button type="button" class="im-zoom" title="Enlarge">⤢</button></div>' +
        '<div class="im-cap">' + esc(caption(a)) + "</div>";
    }
    h += statusCard(have === "after" ? "before" : "after", by[have === "after" ? "before" : "after"], j.watch);
  }
  const tgt = (by.before || by.after || {}).target;
  const note = tgt && tgt.basis !== "event" ? '<div class="im-note">Crop centred on the ' + esc(BASIS[tgt.basis] || tgt.basis) + " (◇), not the reported location (◎)</div>" : "";
  const missing = points && points.length && (ok("before") || ok("after")) ? points.filter((p) => !frac((by.before || by.after).bounds, p.lat, p.lon)).length : 0;
  return h + note + (missing ? '<div class="im-note">' + missing + " evidence point" + (missing === 1 ? " lies" : "s lie") + " outside this crop</div>" : "") +
    (credit ? '<div class="im-credit">' + esc(credit) + "</div>" : "");
}

function setPos(frame, pct) {
  const p = Math.max(0, Math.min(100, pct));
  frame.style.setProperty("--pos", p + "%");
  frame.setAttribute("aria-valuenow", String(Math.round(p)));
}
function dragCompare(frame) {
  const move = (e) => { const r = frame.getBoundingClientRect(); setPos(frame, ((e.clientX - r.left) / r.width) * 100); };
  frame.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".im-zoom")) return;
    frame.setPointerCapture(e.pointerId); move(e);
    frame._drag = true;
  });
  frame.addEventListener("pointermove", (e) => { if (frame._drag) move(e); });
  frame.addEventListener("pointerup", () => { frame._drag = false; });
  frame.addEventListener("keydown", (e) => {
    const cur = parseFloat(frame.style.getPropertyValue("--pos")) || 50;
    if (e.key === "ArrowLeft") { setPos(frame, cur - 5); e.preventDefault(); e.stopPropagation(); }
    else if (e.key === "ArrowRight") { setPos(frame, cur + 5); e.preventDefault(); e.stopPropagation(); }
  });
}

function lightbox(frame) {
  const lb = document.createElement("div");
  lb.className = "im-lb";
  lb.setAttribute("role", "dialog");
  lb.setAttribute("aria-label", "Satellite imagery");
  const big = frame.cloneNode(true);
  big.classList.add("big");
  big.querySelector(".im-zoom")?.remove();
  lb.innerHTML = '<button type="button" class="im-x" title="Close (Esc)">×</button>';
  lb.appendChild(big);
  const cap = frame.parentElement.querySelectorAll(".im-cap, .im-credit");
  cap.forEach((c) => lb.appendChild(c.cloneNode(true)));
  document.body.appendChild(lb);
  if (big.classList.contains("im-cmp")) dragCompare(big);
  const close = () => { lb.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); } };
  document.addEventListener("keydown", onKey, true);
  lb.addEventListener("click", (e) => { if (e.target === lb || e.target.closest(".im-x")) close(); });
  big.focus();
}

export function wireImagery(root) {
  root.querySelectorAll(".im-cmp").forEach(dragCompare);
  root.addEventListener("click", (e) => {
    const z = e.target.closest(".im-zoom") || (e.target.closest(".im-frame:not(.im-cmp)") && !e.target.closest(".im-lb"));
    if (!z) return;
    const frame = e.target.closest(".im-frame");
    if (frame) lightbox(frame);
  });
  // a failed image says why instead of leaving its alt text behind
  root.addEventListener("error", async (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement) || !img.closest(".im-frame")) return;
    let why = "network error";
    try { const r = await fetch(img.src, { method: "HEAD" }); why = "HTTP " + r.status; } catch (err) { /* keep */ }
    const fr = img.closest(".im-frame");
    fr.classList.add("im-bad");
    fr.setAttribute("data-err", "Image failed to load (" + why + ")");
  }, true);
}

// standalone use (kept for callers of the old API): render + wire into `el`
export async function renderEventImagery(eventId, el, fetchJson) {
  const j = await fetchJson("/api/imagery/" + encodeURIComponent(eventId), 8000);
  el.innerHTML = imageryHtml(j);
  wireImagery(el);
  return (j.assets || []).length > 0;
}
