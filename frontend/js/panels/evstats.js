// Top-bar scorecard: GDELT accepted / rejected (hover: breakdown by reason) + events by status, from /api/events/stats.
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
  const html = '<i class="dot ' + (err ? "degraded" : "healthy") + '"></i><span class="nm">GDELT</span><span class="sc">' + nf(acc.total) + " accepted / " + nf(rej.total) + " rejected</span>" +
    '<div class="evs-pop" role="tooltip"><h4>GDELT scorecard<small>acceptance ' + rate + "</small></h4>" +
    "<h5>Accepted · " + nf(acc.total) + "</h5>" + reasons(acc) + "<h5>Rejected · " + nf(rej.total) + "</h5>" + reasons(rej) +
    "<h5>Events · " + nf(s.events) + "</h5>" + (st.length ? st.map(([k, v]) => "<div><span>" + esc(k) + "</span><b>" + nf(v) + "</b></div>").join("") : '<div class="none">none yet</div>') +
    (err ? '<div class="err">builder: ' + esc(err) + "</div>" : "") + "</div>";
  if (el._h !== html) { el._h = html; el.innerHTML = html; }
  el.classList.remove("hidden");
}
