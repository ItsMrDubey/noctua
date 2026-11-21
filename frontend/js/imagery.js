// Satellite before/after viewer for an event (GET /api/imagery/{event_id}): two thumbnails, acquisition
// dates and the required credit (e.g. "Contains modified Copernicus Sentinel data 2026").
import { esc } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";

export async function renderEventImagery(eventId, el) {
  el.innerHTML = "";
  let j;
  try { j = await fetchJson("/api/imagery/" + encodeURIComponent(eventId), 8000); } catch (e) { return false; }
  const ok = (j.assets || []).filter((a) => a.status === "ok" && a.url);
  if (!ok.length) return false;
  const by = Object.fromEntries(ok.map((a) => [a.role, a]));
  const fig = (role, label) => {
    const a = by[role];
    if (!a) return "";
    const d = String(a.acquired_at || "").slice(0, 10);
    return '<figure><a href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer"><img loading="lazy" alt="' + esc(label) + ' satellite image" src="' + esc(a.url) + '"></a>' +
      "<figcaption>" + esc(label) + (d ? " · " + esc(d) : "") + (a.cloud_pct != null ? " · cloud " + Math.round(a.cloud_pct) + "%" : "") + "</figcaption></figure>";
  };
  const credit = (j.attributions || []).join(" · ");
  el.innerHTML = '<div class="wc-near-h">Satellite imagery</div><div class="im-ba">' + fig("before", "Before") + fig("after", "After") + "</div>" +
    (credit ? '<div class="im-credit">' + esc(credit) + "</div>" : "");
  return true;
}
