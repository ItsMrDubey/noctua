// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js), drives
// the globe sync (country-globe.js) from the active country pane and exposes open(cc) for the
// palette, the globe and pivots. Returns { open, globe }.
import { COUNTRY_PANE, knownCountry, setCountrySink } from "/static/js/panels/country-view.js";
import { CountryGlobe } from "/static/js/country-globe.js";
import { globeAction } from "/static/js/country-state.js";

export function installCountry(ctx) {
  const { viewer, dossier, byKind } = ctx;
  const globe = new CountryGlobe(viewer, byKind.events);
  setCountrySink(globe);
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, { country: COUNTRY_PANE });
  // the active country is the focused country pane, kept while it stays open (opening an
  // event's intel pane beside it keeps the context); closing it or minimising / closing the
  // dossier restores the globe. Esc needs no handler of its own: the dossier's Esc minimises
  // (announced here as state "min"), while an Esc that only closes an inner layer (lightbox,
  // zoomed pane, max views) leaves the country context as it is
  window.addEventListener("noctua:dossier", (e) => {
    const a = globeAction(e.detail, globe.active);
    if (a.op === "activate") globe.activate(a.cc);
    else if (a.op === "deactivate") globe.deactivate();
  });
  window.dispatchEvent(new Event("noctua:panels")); // renders country panes restored before this ran
  const open = (cc) => {
    const code = String(cc || "").trim().toUpperCase();
    return knownCountry(code) ? dossier.open({ type: "country", cc: code }) : null;
  };
  return { open, globe };
}
