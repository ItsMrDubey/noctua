// Settings popover (gear in the command bar). Time display: Local (auto-detected zone) / UTC /
// any IANA zone from a searchable list. Applies live through timefmt.js (no reload).
import { $, esc } from "/static/js/ui.js";
import { getTz, setTz, onTzChange, localTz, tzLabel, listZones, fmtDateTime } from "/static/js/timefmt.js";
import { bindPopover } from "/static/js/panels/topbar.js";

const MAX_LIST = 60;

export function installSettings() {
  const btn = $("cfg-btn"), pop = $("cfg-pop");
  if (!btn || !pop) return null;
  const zones = listZones();
  pop.innerHTML =
    '<div class="sp-head"><h4>Settings</h4><button id="cfg-close" class="ghost" type="button" aria-label="Close settings (Esc)">&times;</button></div>' +
    '<section class="sp-sec"><h5>Time display<small id="cfg-now"></small></h5>' +
    '<div class="cfg-opts" role="radiogroup" aria-label="Time display">' +
    '<label class="row"><input type="radio" name="cfg-tz" value="local"><span>Local <small id="cfg-local"></small></span></label>' +
    '<label class="row"><input type="radio" name="cfg-tz" value="UTC"><span>UTC</span></label>' +
    '<label class="row"><input type="radio" name="cfg-tz" value="zone"><span>Time zone <small id="cfg-zone-cur"></small></span></label>' +
    "</div>" +
    '<input id="cfg-zone-q" type="search" placeholder="Search time zones (e.g. London, Tokyo)" autocomplete="off" spellcheck="false" aria-label="Search time zones" aria-controls="cfg-zones">' +
    '<div id="cfg-zones" class="cfg-zones" role="listbox" aria-label="Time zones"></div>' +
    '<div class="row-note">Times show in this zone; hover any time for the exact UTC instant. Relative ages ("3m ago") are unaffected.</div>' +
    "</section>";
  const q = $("cfg-zone-q"), list = $("cfg-zones");
  let zone = getTz() !== "local" && getTz() !== "UTC" ? getTz() : null;
  const renderList = () => {
    const s = q.value.trim().toLowerCase().replace(/\s+/g, "_");
    const hits = zones.filter((z) => !s || z.toLowerCase().includes(s)).slice(0, MAX_LIST);
    const cur = getTz();
    list.innerHTML = hits.map((z) => '<button type="button" role="option" class="cfg-z' + (z === cur ? " on" : "") + '" aria-selected="' + (z === cur) + '" data-z="' + esc(z) + '"><span>' + esc(z.replace(/_/g, " ")) + '</span><small>' + esc(tzLabel(null, z)) + "</small></button>").join("") ||
      '<div class="none">No zone matches.</div>';
  };
  const sync = () => {
    const tz = getTz();
    const mode = tz === "local" || tz === "UTC" ? tz : "zone";
    for (const r of pop.querySelectorAll('input[name="cfg-tz"]')) r.checked = r.value === mode;
    $("cfg-local").textContent = localTz().replace(/_/g, " ") + " · " + tzLabel(null, "local");
    $("cfg-zone-cur").textContent = mode === "zone" ? tz.replace(/_/g, " ") + " · " + tzLabel(null, tz) : zone ? zone.replace(/_/g, " ") : "";
    $("cfg-now").textContent = "now " + fmtDateTime(Date.now(), { zone: true });
    if (!pop.classList.contains("hidden")) renderList();
  };
  pop.addEventListener("change", (e) => {
    const r = e.target.closest('input[name="cfg-tz"]');
    if (!r) return;
    if (r.value === "zone") { if (zone) setTz(zone); else q.focus(); } else setTz(r.value);
  });
  q.addEventListener("input", renderList);
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const b = list.querySelector(".cfg-z"); if (b) b.click(); }
    else if (e.key === "ArrowDown") { const b = list.querySelector(".cfg-z"); if (b) { e.preventDefault(); b.focus(); } }
  });
  list.addEventListener("click", (e) => { const b = e.target.closest(".cfg-z"); if (b) { zone = b.dataset.z; setTz(zone); } });
  list.addEventListener("keydown", (e) => {
    const b = e.target.closest(".cfg-z");
    if (!b) return;
    if (e.key === "ArrowDown" && b.nextElementSibling) { e.preventDefault(); b.nextElementSibling.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); (b.previousElementSibling || q).focus(); }
  });
  const ctl = bindPopover(btn, pop, { onOpen: sync });
  $("cfg-close").addEventListener("click", () => ctl.close(true));
  onTzChange(sync);
  sync();
  return ctl;
}
