// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js), drives
// the globe sync (country-globe.js) from the active country pane, opens a country on Alt+click
// on the globe (with a one-time hint) and exposes open(cc) for the palette and pivots.
// Returns { open, globe }.
import { fetchJson } from "/static/js/api.js";
import { $ } from "/static/js/ui.js";
import { COUNTRY_PANE, knownCountry, setCountrySink } from "/static/js/panels/country-view.js";
import { CountryGlobe } from "/static/js/country-globe.js";
import { globeAction } from "/static/js/country-state.js";
import { HINT_TEXT, OCEAN_TEXT, LOOKUP_FAIL_TEXT, hintDue, markHintSeen, hintTrigger, lookupGate, atUrl, atOutcome } from "/static/js/country-entry.js";

// brief, non-blocking note in the shared #pick-tip (pointer-events: none)
function tip(text, ms = 3000) {
  const el = $("pick-tip");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), ms);
}

// localStorage, or null where touching it throws (blocked site data)
function store() {
  try { return window.localStorage; } catch (e) { return null; }
}

// the one-time Alt+click hint: a dismissible status note with a real button (Enter / Space / Esc)
function showHint() {
  if ($("cx-hint")) return;
  const el = document.createElement("div");
  el.id = "cx-hint";
  el.className = "cx-hint glass";
  el.setAttribute("role", "status");
  el.innerHTML = '<span class="cx-hint-t"></span><button type="button" class="cx-hint-x" aria-label="Dismiss tip">Got it</button>';
  el.querySelector(".cx-hint-t").textContent = HINT_TEXT;
  const close = () => { clearTimeout(el._t); el.remove(); };
  el.querySelector(".cx-hint-x").addEventListener("click", close);
  el.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
  document.body.appendChild(el);
  el._t = setTimeout(close, 12000);
}

export function installCountry(ctx) {
  const { viewer, dossier, byKind } = ctx;
  const C = window.Cesium;
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

  // Alt + click on land: the country under the cursor (server-side point-in-polygon). Cesium
  // keys actions by modifier, so app.js's plain LEFT_CLICK (selection) does not fire on an
  // Alt+click and this one does not fire on a plain click; a drag never makes a click.
  const canvas = viewer.scene.canvas;
  const gate = lookupGate(800); // a double Alt+click makes one lookup, one pane
  const handler = new C.ScreenSpaceEventHandler(canvas);
  handler.setInputAction(async (click) => {
    const cart = viewer.camera.pickEllipsoid(click.position, viewer.scene.globe.ellipsoid);
    if (!cart) return; // sky / space
    const g = C.Cartographic.fromCartesian(cart);
    const url = atUrl(C.Math.toDegrees(g.latitude), C.Math.toDegrees(g.longitude));
    if (!url || !gate.begin(Date.now())) return;
    let out = "fail";
    try {
      out = atOutcome(200, await fetchJson(url, 8000));
    } catch (err) {
      const m = /HTTP (\d+)/.exec(String((err && err.message) || ""));
      out = atOutcome(m ? +m[1] : 0, null);
    } finally {
      gate.end();
    }
    if (out === "ocean") tip(OCEAN_TEXT);
    else if (out === "fail") tip(LOOKUP_FAIL_TEXT);
    else if (!open(out.cc)) tip("No country context for " + (out.name || out.cc));
  }, C.ScreenSpaceEventType.LEFT_CLICK, C.KeyboardEventModifier.ALT);
  // Windows: releasing Alt can focus the browser's menu bar. Swallow only the Alt keyup that
  // ends an Alt+press on the globe; every other Alt use (Alt+Left/Right pane history) is untouched.
  let altOnGlobe = false, downAt = null;
  canvas.addEventListener("pointerdown", (e) => { altOnGlobe = e.altKey && e.button === 0; downAt = [e.clientX, e.clientY]; });
  window.addEventListener("keyup", (e) => {
    if (e.key === "Alt" && altOnGlobe) { altOnGlobe = false; e.preventDefault(); }
  });
  // the hint: shown once per browser, on the first plain click on the globe
  if (hintDue(store())) {
    const hint = (e) => {
      // a click, not the end of a camera drag
      if (!hintTrigger(e) || !downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
      canvas.removeEventListener("pointerup", hint);
      if (!hintDue(store())) return; // seen in another tab meanwhile
      markHintSeen(store());
      showHint();
    };
    canvas.addEventListener("pointerup", hint);
  }
  return { open, globe };
}
