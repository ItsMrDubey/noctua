// COUNTRY CONTEXT pane (spec B §3.2): every event that concerns one country. State lives in the
// pane spec (country-state.js), which the dossier persists, so it survives reloads and pane
// history; the pane refreshes on the live event poll's cadence while it is open.
import { fetchJson } from "/static/js/api.js";
import { esc, nf, stateHtml, settleLoading } from "/static/js/ui.js";
import { EVENT_CATS } from "/static/js/symbology.js";
import { LIFECYCLES, normState, summaryQuery, flagEmoji, sparkPath, threatTier, summaryNames } from "/static/js/country-state.js";

const ICON = '<svg viewBox="0 0 16 16"><path d="M3.5 14.5V2"/><path d="M3.5 2.5h9l-2.2 3 2.2 3h-9"/></svg>';
const REFRESH_MS = 60000; // the live event poll's cadence (app.js)
const SUMMARY_MS = 20000; // summary request timeout
const RETRY_MS = 10000; // a header that never loaded retries sooner than the refresh (busy server at start-up)
let REGION = null;
try { REGION = new Intl.DisplayNames(["en"], { type: "region", fallback: "none" }); } catch { /* old engine: codes stay codes */ }
const NOT_COUNTRIES = new Set(["ZZ", "EU", "EZ", "UN", "QO"]); // CLDR regions that are not countries

// an ISO2 the browser knows as a country (open(cc) refuses the rest; the API 400s on them)
export function knownCountry(cc) {
  const c = String(cc || "").trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(c) || NOT_COUNTRIES.has(c)) return false;
  try { return !REGION || REGION.of(c) !== undefined; } catch { return false; }
}

// country name for an ISO2: the API's names first (summary / counterparts), then the browser's;
// null when it is not an ISO2 the browser knows (actor countries are not always normalised)
function countryName(names, cc) {
  if (!cc) return null;
  if (names && names[cc]) return names[cc];
  if (!/^[A-Z]{2}$/.test(cc)) return null;
  try { return (REGION && REGION.of(cc)) || null; } catch { return null; }
}

class CountryPane {
  constructor(spec, body, hooks) {
    this.st = normState(spec);
    this.body = body;
    this.hooks = hooks;
    this.allCats = Object.keys(EVENT_CATS);
    this.summary = null; this.sumErr = null; this.sumSeq = 0; this.advOpen = false; this.names = {};
    body.innerHTML = '<div class="dk-root cx-pane"><div class="cx-head"></div><div class="cx-filters"></div><div class="cx-cp"></div><div class="cx-list"></div></div>';
    this.root = body.firstElementChild;
    this.root.addEventListener("click", (e) => this.click(e));
  }
  dead() { return this.hooks.stale() || !this.body.isConnected; }
  name(cc) { return countryName(this.names, cc) || cc; }
  title(s) {
    this.hooks.head({ title: (flagEmoji(this.st.cc) + " " + (s ? s.name : this.name(this.st.cc))).trim(), sub: this.st.cc + " · COUNTRY CONTEXT", icon: "country",
      coord: s && s.centroid ? { lat: s.centroid[0], lon: s.centroid[1], alt: 3e6 } : null });
  }
  async start() {
    this.title(null);
    this.renderAll();
    this.timer = setInterval(() => { if (this.dead()) clearInterval(this.timer); else this.load("refresh"); }, REFRESH_MS);
    await this.load("reset");
  }
  load() { return this.loadSummary(); }
  renderAll() { this.paintSummary(); }
  paintSummary() { this.renderHead(); }
  async loadSummary() {
    const seq = ++this.sumSeq;
    if (this.sumErr) { this.sumErr = null; if (!this.summary) this.renderHead(); } // retry: back to loading
    try {
      const s = await fetchJson("/api/country/" + encodeURIComponent(this.st.cc) + "/summary?" + summaryQuery(this.st, { allCats: this.allCats }), SUMMARY_MS);
      if (this.dead() || seq !== this.sumSeq) return;
      this.summary = s; this.sumErr = null; this.names = summaryNames(s);
      this.title(s);
    } catch (err) {
      if (this.dead() || seq !== this.sumSeq) return;
      this.sumErr = err && err.name === "AbortError" ? "No answer within " + SUMMARY_MS / 1000 + " s." : String((err && err.message) || err);
      if (!this.summary) { clearTimeout(this.retryT); this.retryT = setTimeout(() => { if (!this.dead() && !this.summary && seq === this.sumSeq) this.loadSummary(); }, RETRY_MS); }
    }
    this.paintSummary();
  }
  renderHead() {
    const el = this.root.querySelector(".cx-head"), s = this.summary;
    if (this.sumErr) {
      el.innerHTML = '<div class="cx-err">' + stateHtml("err", "Summary unavailable", this.sumErr) + '<button type="button" data-cx="retry">Retry</button></div>';
      return;
    }
    if (!s) {
      el.innerHTML = stateHtml("loading", "Loading " + this.name(this.st.cc) + " summary…");
      settleLoading(el, SUMMARY_MS + 2000, "Summary unavailable"); // backstop: never stuck on "Loading…"
      return;
    }
    const t = s.threat, advs = (t && t.advisories) || [], tier = threatTier(t);
    const threat = t
      ? '<button type="button" class="cx-threat' + (tier == null ? "" : " thr-" + tier) + '" data-cx="adv" aria-expanded="' + this.advOpen + '" title="' + esc(t.as_of ? "As of " + t.as_of : "Official threat level") + '"><b>' + esc(t.level || "No official level") + "</b>" +
        (t.source ? "<small>" + esc(t.source) + "</small>" : "") + '<span class="cx-n">' + nf(advs.length) + " advisor" + (advs.length === 1 ? "y" : "ies") + "</span></button>"
      : '<span class="cx-threat off" title="Terror Watch threat data could not be read">Threat level unavailable</span>';
    const adv = t && this.advOpen ? '<ul class="cx-adv">' + (advs.length ? advs.map((a) => "<li><b>" + esc(a.label || "Level " + a.level) + "</b> " +
      (a.url ? '<a href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">' + esc(a.title || "advisory") + "</a>" : esc(a.title || "")) +
      (a.updated ? " <small>" + esc(a.updated) + "</small>" : "") + (a.terrorism ? ' <span class="cx-terr">terrorism</span>' : "") + "</li>").join("")
      : "<li>No travel advisories for " + esc(s.name || this.name(s.cc)) + ".</li>") + "</ul>" : "";
    const days = s.spark || [], total = days.reduce((a, d) => a + (d.n || 0), 0);
    const range = days.length ? days[0].day + " to " + days[days.length - 1].day : "";
    const spark = '<svg class="cx-spark" viewBox="0 0 120 24" preserveAspectRatio="none" role="img" aria-label="' + esc(nf(total) + " events in the last 30 days") + '"><title>' + esc(nf(total) + " events, " + range) + '</title><path d="' + sparkPath(days, 120, 24) + '"/></svg>';
    const life = '<div class="cx-life">' + LIFECYCLES.map((k) => '<span class="cx-lc lc-' + k + '" title="' + k + '">' + k.slice(0, 3).toUpperCase() + " <b>" + nf((s.lifecycle || {})[k] || 0) + "</b></span>").join("") + "</div>";
    const actors = '<div class="cx-actors">' + (s.top_actors || []).slice(0, 3).map((a) => {
      const where = countryName(this.names, a.country);
      return '<span class="dk-chip" title="' + esc(nf(a.n) + " events" + (where ? " · " + where : "")) + '">' + esc(a.name) + (where && where !== a.name ? " · " + esc(where) : "") + "</span>";
    }).join("") + "</div>";
    el.innerHTML = '<div class="cx-id"><span class="cx-flag" aria-hidden="true">' + flagEmoji(s.cc) + '</span><b class="cx-cc">' + esc(s.cc) + '</b><span class="cx-name">' + esc(s.name) + "</span></div>" + threat + spark + life + actors + adv;
  }
  click(e) {
    const t = e.target.closest("button");
    if (!t || !this.root.contains(t)) return;
    if (t.dataset.cx === "retry") this.loadSummary();
    else if (t.dataset.cx === "adv") { this.advOpen = !this.advOpen; this.renderHead(); }
  }
}

export async function renderCountry(spec, body, hooks) {
  const st = normState(spec);
  if (!knownCountry(st.cc)) {
    hooks.head({ title: "Country", sub: "COUNTRY CONTEXT", icon: "country" });
    body.innerHTML = '<div class="cx-err">' + stateHtml("err", "Not a country code", String(spec.cc == null ? "" : spec.cc)) + "</div>";
    return;
  }
  await new CountryPane(spec, body, hooks).start();
}

export const COUNTRY_PANE = { icon: ICON, key: (s) => "ctry:" + String(s.cc || "").toUpperCase(), render: renderCountry };
