// COUNTRY CONTEXT pane (spec B §3.2): every event that concerns one country. State lives in the
// pane spec (country-state.js), which the dossier persists, so it survives reloads and pane
// history; the pane refreshes on the live event poll's cadence while it is open.
import { fetchJson } from "/static/js/api.js";
import { esc, nf, stateHtml, settleLoading } from "/static/js/ui.js";
import { EVENT_CATS } from "/static/js/symbology.js";
import { controlSelector, LOCATIONS, INVOLVEMENTS, WINDOWS, LIFECYCLES, normState, summaryQuery, toggleCat, flagEmoji, sparkPath, threatTier, summaryNames, pairLabel } from "/static/js/country-state.js";

const ICON = '<svg viewBox="0 0 16 16"><path d="M3.5 14.5V2"/><path d="M3.5 2.5h9l-2.2 3 2.2 3h-9"/></svg>';
const REFRESH_MS = 60000; // the live event poll's cadence (app.js)
const SUMMARY_MS = 20000; // summary request timeout
const RETRY_MS = 10000; // a failed load retries sooner than the refresh (busy server at start-up)
const TICK_MS = 2000; // visibility / due check; nothing is fetched while the pane is hidden
let REGION = null;
try { REGION = new Intl.DisplayNames(["en"], { type: "region", fallback: "none" }); } catch { /* old engine: codes stay codes */ }
// Windows has no flag glyphs: a regional-indicator pair renders as the two letters, so skip it there
const FLAGS = typeof navigator === "undefined" || !/Windows/i.test(navigator.userAgent || "");
const flagOf = (cc) => (FLAGS ? flagEmoji(cc) : "");
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

const GROUP = { location: "Location", involvement: "Involvement", window: "Time window" };
class CountryPane {
  constructor(spec, body, hooks) {
    this.st = normState(spec);
    this.body = body;
    this.hooks = hooks;
    this.allCats = Object.keys(EVENT_CATS);
    this.summary = null; this.sumErr = null; this.sumSeq = 0; this.loadingN = 0; this.advOpen = false; this.names = {};
    body.innerHTML = '<div class="dk-root cx-pane"><div class="cx-head"></div><div class="cx-filters"></div><div class="cx-cp"></div><div class="cx-list"></div></div>';
    this.root = body.firstElementChild;
    this.root.addEventListener("click", (e) => this.click(e));
    this.root.addEventListener("change", (e) => this.change(e));
  }
  dead() { return this.hooks.stale() || !this.body.isConnected; }
  // minimised / closed dossier or inactive tab: display:none ancestors give offsetParent null
  hidden() { return this.body.offsetParent === null; }
  name(cc) { return countryName(this.names, cc) || cc; }
  title(s) {
    this.hooks.head({ title: (flagOf(this.st.cc) + " " + (s ? s.name : this.name(this.st.cc))).trim(), sub: this.st.cc + " · COUNTRY CONTEXT", icon: "country",
      coord: s && s.centroid ? { lat: s.centroid[0], lon: s.centroid[1], alt: 3e6 } : null });
  }
  async start() {
    this.title(null);
    this.renderAll();
    this.wasHidden = this.hidden();
    this.timer = setInterval(() => this.tick(), TICK_MS);
    if (!this.wasHidden) await this.load("reset");
  }
  tick() {
    if (this.dead()) { clearInterval(this.timer); return; } // disposed / replaced: stop the timer now
    if (this.hidden()) { this.wasHidden = true; return; }
    const due = this.lastTry + (this.sumErr ? RETRY_MS : REFRESH_MS);
    if (this.wasHidden || !(Date.now() < due)) { this.wasHidden = false; this.load("refresh"); }
  }
  load() { return this.loadSummary(); }
  renderAll() { this.paintSummary(); }
  paintSummary() {
    this.renderHead();
    if (!(this.filtersBusy() && this.countsOnly())) this.renderFilters(); // focus inside the bar: only the numbers change
    this.root.querySelector(".cx-filters").classList.toggle("cx-loading", this.loadingN > 0);
  }
  // keyboard focus / an open date picker live inside .cx-filters: never rebuild it from under them on a refresh
  filtersBusy() { const f = this.root.querySelector(".cx-filters"); return !!(f && f.contains(document.activeElement)); }
  countsOnly() {
    const c = this.summary && this.summary.counts, bs = this.root.querySelectorAll(".cx-seg button b");
    if (!c || bs.length !== 7) return false; // structure differs (counts just arrived): full repaint
    const tot = c.inside + c.abroad, v = [tot, c.inside, c.abroad, tot, c.domestic, c.foreign, c.unattributed];
    bs.forEach((b, i) => { b.textContent = nf(v[i]); });
    return true;
  }
  // any filter change: new state into the pane spec (persisted), redraw, reload (the seq guard drops superseded loads)
  set(next) {
    this.st = normState(next);
    this.hooks.setSpec(this.st);
    this.renderHead(); this.renderFilters(); // state changed: always a full repaint (focus is restored)
    this.load("reset");
  }
  renderFilters() {
    const act = document.activeElement, host = this.root.querySelector(".cx-filters");
    const sel = host && act && host.contains(act) ? controlSelector(act.dataset) : null;
    this.paintFilters();
    if (sel) { const el = host.querySelector(sel); if (el) el.focus(); } // innerHTML rebuild drops focus: find the same control again
  }
  paintFilters() {
    const st = this.st, c = (this.summary && this.summary.counts) || null, tot = c ? c.inside + c.abroad : null;
    const seg = (key, values, labels, counts) => '<span class="seg cx-seg" role="group" aria-label="' + GROUP[key] + '">' + values.map((v, i) =>
      '<button type="button" data-k="' + key + '" data-v="' + v + '" class="tab' + (st[key] === v ? " on" : "") + '" aria-pressed="' + (st[key] === v) + '">' + labels[i] +
      (counts && counts[v] != null ? " <b>" + nf(counts[v]) + "</b>" : "") + "</button>").join("") + "</span>";
    const loc = seg("location", LOCATIONS, ["ALL", "INSIDE", "ABROAD"], c && { all: tot, inside: c.inside, abroad: c.abroad });
    const inv = seg("involvement", INVOLVEMENTS, ["ALL", "DOMESTIC", "FOREIGN", "UNATTRIB."], c && { all: tot, domestic: c.domestic, foreign: c.foreign, unattributed: c.unattributed });
    const win = seg("window", WINDOWS, ["24H", "7D", "30D", "CUSTOM"], null);
    const dates = st.window === "custom" ? '<span class="cx-dates"><input type="date" data-date="from" value="' + esc(st.from || "") + '" aria-label="From (UTC)"><span aria-hidden="true">→</span><input type="date" data-date="to" value="' + esc(st.to || "") + '" aria-label="To (UTC)"></span>' : "";
    const pair = st.counterpart ? '<button type="button" class="cx-pair" data-cx="uncp" title="Remove the counterpart filter">' + esc(pairLabel(st, this.names)) + ' <span aria-hidden="true">×</span></button>' : "";
    const on = (k) => !st.cats || st.cats.includes(k);
    const cats = '<div class="chips cx-cats" role="group" aria-label="Categories">' + this.allCats.map((k) => '<button type="button" class="chip' + (on(k) ? " on" : "") + '" data-cat="' + k + '" aria-pressed="' + on(k) + '" style="--c:' + EVENT_CATS[k][1] + '" title="' + esc(EVENT_CATS[k][0]) + '">' + esc(EVENT_CATS[k][0]) + "</button>").join("") + "</div>";
    this.root.querySelector(".cx-filters").innerHTML = '<div class="cx-frow">' + loc + inv + '</div><div class="cx-frow">' + win + dates + pair + "</div>" + cats;
  }
  change(e) {
    const d = e.target.closest("[data-date]");
    if (d) this.set({ ...this.st, window: "custom", [d.dataset.date]: d.value || null });
  }
  async loadSummary() {
    const seq = ++this.sumSeq;
    this.loadingN = 1; this.root.querySelector(".cx-filters").classList.add("cx-loading"); // stale counts read as dimmed until this lands
    this.lastTry = Date.now();
    if (this.sumErr) { this.sumErr = null; if (!this.summary) this.renderHead(); } // retry: back to loading
    try {
      const s = await fetchJson("/api/country/" + encodeURIComponent(this.st.cc) + "/summary?" + summaryQuery(this.st, { allCats: this.allCats }), SUMMARY_MS);
      if (this.dead() || seq !== this.sumSeq) return;
      this.loadingN = 0;
      this.summary = s; this.sumErr = null; this.names = summaryNames(s);
      this.title(s);
    } catch (err) {
      if (this.dead() || seq !== this.sumSeq) return;
      this.loadingN = 0;
      this.sumErr = err && err.name === "AbortError" ? "No answer within " + SUMMARY_MS / 1000 + " s." : String((err && err.message) || err);
    }
    this.paintSummary();
  }
  renderHead() {
    const el = this.root.querySelector(".cx-head"), s = this.summary;
    if (this.sumErr && !s) {
      el.innerHTML = '<div class="cx-err">' + stateHtml("err", "Summary unavailable", this.sumErr) + '<button type="button" data-cx="retry">Retry</button></div>';
      return;
    }
    if (!s) {
      el.innerHTML = stateHtml("loading", "Loading " + this.name(this.st.cc) + " summary…");
      settleLoading(el, SUMMARY_MS + 2000, "Summary unavailable"); // backstop: never stuck on "Loading…"
      return;
    }
    const stale = this.sumErr ? '<div class="cx-stale" role="status">Showing last data · retrying</div>' : "";
    const t = s.threat, advs = (t && t.advisories) || [], tier = threatTier(t);
    const threat = t
      ? '<button type="button" class="cx-threat' + (tier == null ? "" : " thr-" + tier) + '" data-cx="adv" aria-expanded="' + this.advOpen + '" title="' + esc(t.as_of ? "As of " + t.as_of : "Official threat level") + '"><b>' + esc(t.level || "No official level") + "</b>" +
        (t.source ? "<small>" + esc(t.source) + "</small>" : "") + '<span class="cx-n">' + nf(advs.length) + " advisor" + (advs.length === 1 ? "y" : "ies") + "</span></button>"
      : '<span class="cx-threat off" title="Terror Watch threat data could not be read">Threat level unavailable</span>';
    const adv = t && this.advOpen ? '<ul class="cx-adv">' + (advs.length ? advs.map((a) => "<li><b>" + esc(a.label || "Level " + a.level) + "</b> " +
      (/^https?:\/\//i.test(a.url || "") ? '<a href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">' + esc(a.title || "advisory") + "</a>" : esc(a.title || "")) +
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
    el.innerHTML = '<div class="cx-id">' + (flagOf(s.cc) ? '<span class="cx-flag" aria-hidden="true">' + flagOf(s.cc) + "</span>" : "") + '<b class="cx-cc">' + esc(s.cc) + '</b><span class="cx-sep" aria-hidden="true">·</span><span class="cx-name">' + esc(s.name) + "</span></div>" + stale + threat + spark + life + actors + adv;
  }
  click(e) {
    const t = e.target.closest("button");
    if (!t || !this.root.contains(t)) return;
    const st = this.st, a = t.dataset.cx;
    if (a === "retry") this.loadSummary();
    else if (a === "adv") { this.advOpen = !this.advOpen; this.renderHead(); }
    else if (a === "uncp") this.set({ ...st, counterpart: null });
    else if (t.dataset.k) this.set({ ...st, [t.dataset.k]: t.dataset.v });
    else if (t.dataset.cat) this.set({ ...st, cats: toggleCat(st.cats, t.dataset.cat, this.allCats) });
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
