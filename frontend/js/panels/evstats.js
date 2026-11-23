// GDELT scorecard in the system-status popover: accepted / rejected (breakdown by reason) +
// events by status, from /api/events/stats.
import { $, esc } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";

const nf = (n) => (n == null ? "–" : Number(n).toLocaleString());
const reasons = (g) => {
  const e = Object.entries((g && g.by_reason) || {}).sort((a, b) => b[1] - a[1]);
  return e.length ? e.map(([k, v]) => "<div><span>" + esc(k.replace(/_/g, " ")) + "</span><b>" + nf(v) + "</b></div>").join("") : '<div class="none">none yet</div>';
};

export async function refreshEventStats() {
  const s = await fetchJson("/api/events/stats", 10000);
  const g = s.gdelt || {}, acc = g.accepted || {}, rej = g.rejected || {};
  const el = $("ev-score");
  const st = Object.entries(s.by_status || {});
  const rate = g.acceptance_rate == null ? "–" : (g.acceptance_rate * 100).toFixed(1) + "%";
  const err = s.builder && s.builder.last_error;
  const html = '<div class="sp-kv"><span><i class="dot ' + (err ? "degraded" : "healthy") + '"></i>Accepted / rejected</span><b>' + nf(acc.total) + " / " + nf(rej.total) + "</b></div>" +
    '<div class="sp-kv"><span>Acceptance</span><b>' + rate + "</b></div>" +
    '<details class="sp-more"><summary>Breakdown by reason · events by status</summary>' +
    "<h6>Accepted · " + nf(acc.total) + "</h6>" + reasons(acc) + "<h6>Rejected · " + nf(rej.total) + "</h6>" + reasons(rej) +
    "<h6>Events · " + nf(s.events) + "</h6>" + (st.length ? st.map(([k, v]) => "<div><span>" + esc(k) + "</span><b>" + nf(v) + "</b></div>").join("") : '<div class="none">none yet</div>') +
    "</details>" + (err ? '<div class="err">builder: ' + esc(err) + "</div>" : "");
  if (el._h !== html) {
    const d = el.querySelector("details"), open = !!(d && d.open);
    el._h = html; el.innerHTML = html;
    if (open) el.querySelector("details").open = true;
  }
}
