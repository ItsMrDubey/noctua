// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js) and
// exposes open(cc) for the palette, the globe and pivots. Returns { open }.
import { COUNTRY_PANE, knownCountry } from "/static/js/panels/country-view.js";

export function installCountry(ctx) {
  const { dossier } = ctx;
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, { country: COUNTRY_PANE });
  window.dispatchEvent(new Event("noctua:panels")); // renders country panes restored before this ran
  const open = (cc) => {
    const code = String(cc || "").trim().toUpperCase();
    return knownCountry(code) ? dossier.open({ type: "country", cc: code }) : null;
  };
  return { open };
}
