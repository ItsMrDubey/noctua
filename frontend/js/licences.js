// Footer "Attributions & licences" panel: the data-source register from /api/sources/licences.
import { $, esc } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";

const CLS = { ok: "OK", review: "Review for commercial use", no: "Non-commercial only" };
let loaded = null;

function render(j) {
  const rows = (j.sources || []).map((s) =>
    '<li class="lic-' + esc(s.commercial) + '"><b>' + esc(s.name) + '</b><em title="Commercial use">' + esc(CLS[s.commercial] || s.commercial) + "</em>" +
    '<div>' + esc(s.licence) + (s.share_alike ? " · share-alike" : "") + "</div>" +
    '<div class="lic-at">' + esc(s.attribution) + "</div>" +
    (s.notes ? '<div class="lic-n">' + esc(s.notes) + "</div>" : "") +
    (/^https:\/\//.test(s.url || "") ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">terms</a>' : "") + "</li>").join("");
  return '<header><h2>Attributions &amp; licences</h2><button type="button" id="lic-close" aria-label="Close">×</button></header><ul>' + rows + "</ul>";
}

export function initLicences() {
  const panel = $("lic-panel"), open = $("lic-open");
  const close = () => panel.classList.add("hidden");
  open.addEventListener("click", async () => {
    panel.classList.remove("hidden");
    if (!loaded) {
      panel.innerHTML = '<div class="lic-load">Loading…</div>';
      try { loaded = render(await fetchJson("/api/sources/licences", 10000)); } catch (e) { panel.innerHTML = '<div class="lic-load">Unavailable</div>'; return; }
    }
    panel.innerHTML = loaded;
    $("lic-close").addEventListener("click", close);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}
