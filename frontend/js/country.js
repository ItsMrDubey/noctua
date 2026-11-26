// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js), drives
// the globe sync (country-globe.js) from the active country pane and exposes open(cc) for the
// palette, the globe and pivots. Returns { open, globe }.
import { COUNTRY_PANE, knownCountry, setCountrySink } from "/static/js/panels/country-view.js";
import { CountryGlobe } from "/static/js/country-globe.js";

const ccOf = (s) => String((s && s.cc) || "").toUpperCase();

export function installCountry(ctx) {
  const { viewer, dossier, byKind } = ctx;
  const globe = new CountryGlobe(viewer, byKind.events);
  setCountrySink(globe);
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, { country: COUNTRY_PANE });
  // the active country is the focused country pane, kept while it stays open (opening an
  // event's intel pane beside it keeps the context); closing it, minimising or closing the
  // dossier, or Esc restores the globe
  window.addEventListener("noctua:dossier", (e) => {
    const d = e.detail || {};
    if (d.state !== "open") { globe.deactivate(); return; }
    if (d.focused && d.focused.type === "country") { globe.activate(ccOf(d.focused)); return; }
    if (globe.active && !(d.specs || []).some((s) => s && s.type === "country" && ccOf(s) === globe.active)) globe.deactivate();
  });
  // capture phase: runs even when the dossier's own Esc handler stops propagation
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !/INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) globe.deactivate();
  }, true);
  window.dispatchEvent(new Event("noctua:panels")); // renders country panes restored before this ran
  const open = (cc) => {
    const code = String(cc || "").trim().toUpperCase();
    return knownCountry(code) ? dossier.open({ type: "country", cc: code }) : null;
  };
  return { open, globe };
}
