// DOSSIER: in-app browser workspace with auto-tiling panes.
//
// Pane types
//   entity  - Wikidata item: hero photo, key facts, Wikipedia summary, Commons photos
//             (justified gallery + lightbox with author / licence), live Wikipedia article
//   url     - any web page: live view when the site allows embedding, else reader view
//             (/api/browse/read: server-side fetch + main-text extraction)
//   nearby  - Wikipedia articles around a point (thumbnails, distance, fly-to)
//   search  - candidate Wikidata items for a name
//   intel   - all-source intel buildup around a selection (panels/intel-view.js, /api/intel);
//             Wikidata / Wikipedia is its "Background" card. Entities pivot within the pane.
//   story   - a storyline: linked events of one thread, lanes timeline, globe playback
//             (panels/storyline-view.js, /api/storylines/{id})
//
// Wikidata / Wikipedia / Commons are queried straight from the app (CORS, origin=*): this is
// user-driven browsing. Links clicked anywhere in the app (target=_blank) open here; Ctrl- or
// middle-click opens the system browser instead.
//
// Layout is a tiling-WM style master/stack: 1 = full, 2 = split, 3 = master + 2 stacked,
// 4 = 2x2, 5 = master + 2x2, 6 = 3x2, then 3 or 4 columns. Drag a pane header onto another
// pane to swap them; the star promotes a pane to master; zoom shows one pane alone.
import { esc } from "/static/js/ui.js";
import { fmtDateTime, utcTitle } from "/static/js/timefmt.js";
import { fetchJson } from "/static/js/api.js";
import { renderIntel, intelKey } from "/static/js/panels/intel-view.js";
import { renderStoryline, storyKey } from "/static/js/panels/storyline-view.js";

const WD = "https://www.wikidata.org/w/api.php";
const COMMONS = "https://commons.wikimedia.org/w/api.php";
const WP = "https://en.wikipedia.org/w/api.php";
const WP_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/";
const QID = /^Q[1-9]\d{0,11}$/;
const MAX_PANES = 12;
const STORE_KEY = "noctua.dossier.v1";
const WIKIMEDIA_HOST = /(^|\.)(wikipedia|wikimedia|wikidata|wikivoyage|wiktionary|wikinews|wikisource|wikiquote)\.org$/i;

const FACTS = [
  ["P31", "Instance of"], ["P279", "Subclass of"], ["P17", "Country"], ["P131", "Located in"], ["P706", "Terrain feature"],
  ["P137", "Operator"], ["P127", "Owned by"], ["P749", "Parent organisation"], ["P241", "Military branch"], ["P1416", "Affiliation"],
  ["P361", "Part of"], ["P176", "Manufacturer"], ["P289", "Vessel class"], ["P8047", "Country of registry"], ["P571", "Inception"],
  ["P1619", "Opened"], ["P729", "Service entry"], ["P730", "Service retirement"], ["P576", "Dissolved"], ["P36", "Capital"],
  ["P35", "Head of state"], ["P6", "Head of government"], ["P1082", "Population"], ["P2046", "Area"], ["P2044", "Elevation"],
  ["P2048", "Height"], ["P2043", "Length"], ["P2067", "Mass"], ["P2109", "Nameplate capacity"], ["P516", "Powered by"],
  ["P239", "ICAO"], ["P238", "IATA"], ["P458", "IMO"], ["P587", "MMSI"], ["P1435", "Heritage designation"], ["P856", "Website"],
];
const UNITS = { Q11573: "m", Q828224: "km", Q712226: "km²", Q11570: "kg", Q191118: "t", Q3200: "ft", Q174728: "cm", Q6574: "MW", Q3332099: "MW", Q35852: "ha", Q11229: "%", Q25235: "ft" };

const ICON = {
  entity: '<svg viewBox="0 0 16 16"><path d="M2.5 3.5h11v9h-11z"/><path d="M5 6.5h6M5 9h4"/></svg>',
  url: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5"/><path d="M2.5 8h11M8 2.5c-2 2-2 9 0 11M8 2.5c2 2 2 9 0 11"/></svg>',
  nearby: '<svg viewBox="0 0 16 16"><path d="M8 14s4.5-4.2 4.5-7.5a4.5 4.5 0 0 0-9 0C3.5 9.8 8 14 8 14z"/><circle cx="8" cy="6.5" r="1.6"/></svg>',
  search: '<svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2l3.3 3.3"/></svg>',
  intel: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="2.2"/><path d="M8 1v3M8 12v3M1 8h3M12 8h3"/></svg>',
  story: '<svg viewBox="0 0 16 16"><circle cx="3" cy="11" r="1.6"/><circle cx="8" cy="5" r="1.6"/><circle cx="13" cy="9" r="1.6"/><path d="M4.2 9.8L6.9 6.3M9.4 5.8l2.3 2.2"/></svg>',
};
const B = {
  back: '<svg viewBox="0 0 16 16"><path d="M10 3.5L5.5 8l4.5 4.5"/></svg>',
  fwd: '<svg viewBox="0 0 16 16"><path d="M6 3.5L10.5 8 6 12.5"/></svg>',
  fly: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="2"/><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3"/></svg>',
  star: '<svg viewBox="0 0 16 16"><path d="M8 2l1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3l-3.7 2 .8-4.1L2 6.3l4.2-.5z"/></svg>',
  zoom: '<svg viewBox="0 0 16 16"><path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9 7M2.5 13.5L7 9"/></svg>',
  ext: '<svg viewBox="0 0 16 16"><path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M11.5 9.5v4h-9v-9h4"/></svg>',
  close: '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  min: '<svg viewBox="0 0 16 16"><path d="M3.5 11.5h9"/></svg>',
  max: '<svg viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10"/></svg>',
  grid: '<svg viewBox="0 0 16 16"><rect x="2.5" y="2.5" width="4.5" height="11"/><rect x="9" y="2.5" width="4.5" height="4.5"/><rect x="9" y="9" width="4.5" height="4.5"/></svg>',
};

const isHttp = (u) => /^https?:\/\//i.test(u || "");
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
const nf = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const stripHtml = (s, n = 300) => { const d = document.createElement("div"); d.innerHTML = String(s || ""); return (d.textContent || "").replace(/\s+/g, " ").trim().slice(0, n); };
const km = (a, b, c, d) => { const r = Math.PI / 180, x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2; return 12742 * Math.asin(Math.sqrt(Math.min(1, x))); };

// ---------------------------------------------------------------- Wikimedia client
const cache = new Map();
async function cached(key, ttlMs, fn) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < ttlMs) return hit.v;
  const v = await fn();
  cache.set(key, { t: Date.now(), v });
  if (cache.size > 600) cache.delete(cache.keys().next().value);
  return v;
}
async function api(base, params) {
  const u = new URL(base);
  for (const [k, v] of Object.entries({ format: "json", origin: "*", ...params })) u.searchParams.set(k, v);
  // bounded: a Wikimedia request that never answers must not leave panes "Loading..." forever
  const r = await fetch(u, { headers: { "Api-User-Agent": "NOCTUA-desktop/0.1 (in-app browser)" }, signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(host(base) + " HTTP " + r.status);
  return r.json();
}
async function labels(ids) {
  const out = {}, missing = [];
  for (const q of new Set(ids)) { const h = cache.get("lab:" + q); if (h) out[q] = h.v; else missing.push(q); }
  for (let i = 0; i < missing.length; i += 50) {
    const d = await api(WD, { action: "wbgetentities", ids: missing.slice(i, i + 50).join("|"), props: "labels", languages: "en", languagefallback: "1" });
    for (const [q, e] of Object.entries(d.entities || {})) { const l = (e.labels && e.labels.en && e.labels.en.value) || q; cache.set("lab:" + q, { t: Date.now(), v: l }); out[q] = l; }
  }
  return out;
}
async function commonsFiles(params) {
  const d = await api(COMMONS, { action: "query", prop: "imageinfo", iiprop: "url|extmetadata|size|mime", iiurlwidth: "1280",
    iiextmetadatafilter: "Artist|LicenseShortName|ImageDescription|DateTimeOriginal", ...params });
  const pages = Object.values((d.query && d.query.pages) || {}).sort((a, b) => (a.index || 0) - (b.index || 0));
  const out = [];
  for (const p of pages) {
    const ii = (p.imageinfo || [])[0];
    if (!ii || !ii.thumburl || !/^image\//.test(ii.mime || "")) continue;
    const md = ii.extmetadata || {};
    out.push({ title: String(p.title || "").replace(/^File:/, ""), thumb: ii.thumburl, url: ii.url, page: ii.descriptionurl,
      w: ii.thumbwidth || 4, h: ii.thumbheight || 3, author: stripHtml(md.Artist && md.Artist.value, 120),
      licence: stripHtml(md.LicenseShortName && md.LicenseShortName.value, 60), caption: stripHtml(md.ImageDescription && md.ImageDescription.value, 260),
      date: stripHtml(md.DateTimeOriginal && md.DateTimeOriginal.value, 30) });
  }
  return out;
}
async function images(files, category) {
  const jobs = [];
  if (files.length) jobs.push(commonsFiles({ titles: files.slice(0, 12).map((f) => "File:" + f).join("|") }));
  if (category) jobs.push(commonsFiles({ generator: "categorymembers", gcmtype: "file", gcmlimit: "40", gcmtitle: "Category:" + category }));
  const all = (await Promise.allSettled(jobs)).flatMap((r) => (r.status === "fulfilled" ? r.value : []));
  const seen = new Set();
  return all.filter((i) => !seen.has(i.title) && seen.add(i.title)).slice(0, 48);
}
async function summary(title) {
  try {
    const r = await fetch(WP_SUMMARY + encodeURIComponent(title.replace(/ /g, "_")));
    if (!r.ok) return null;
    const d = await r.json();
    return { title: d.title, extract: d.extract, thumb: d.thumbnail && d.thumbnail.source, original: d.originalimage && d.originalimage.source,
      url: d.content_urls && d.content_urls.desktop && d.content_urls.desktop.page, mobile: d.content_urls && d.content_urls.mobile && d.content_urls.mobile.page };
  } catch { return null; }
}
function fmtTime(v) {
  const m = /^([+-])(\d+)-(\d\d)-(\d\d)/.exec(v.time || "");
  if (!m) return v.time || "";
  const y = +m[2], ys = y + (m[1] === "-" ? " BCE" : ""), p = v.precision == null ? 11 : v.precision;
  if (p >= 11 && m[3] !== "00" && m[4] !== "00") return ys + "-" + m[3] + "-" + m[4];
  if (p === 10 && m[3] !== "00") return ys + "-" + m[3];
  if (p <= 8) return Math.floor(y / 10) * 10 + "s";
  return ys;
}
export async function wdEntity(qid) {
  if (!QID.test(qid)) throw new Error("bad Q-id " + qid);
  return cached("ent:" + qid, 6 * 3600e3, async () => {
    const d = await api(WD, { action: "wbgetentities", ids: qid, props: "labels|descriptions|aliases|claims|sitelinks", languages: "en", languagefallback: "1" });
    const e = (d.entities || {})[qid];
    if (!e || "missing" in e) throw new Error(qid + " not found");
    const claims = e.claims || {};
    const vals = (pid) => (claims[pid] || []).filter((c) => c.rank !== "deprecated" && c.mainsnak && c.mainsnak.datavalue)
      .sort((a, b) => (a.rank === "preferred" ? 0 : 1) - (b.rank === "preferred" ? 0 : 1)).map((c) => c.mainsnak.datavalue);
    const refs = [], raw = [];
    for (const [pid] of FACTS) {
      const v = vals(pid).slice(0, 6);
      if (!v.length) continue;
      raw.push([pid, v]);
      for (const dv of v) {
        if (dv.type === "wikibase-entityid") refs.push(dv.value.id);
        if (dv.type === "quantity") { const u = String(dv.value.unit || "").split("/").pop(); if (QID.test(u) && !UNITS[u]) refs.push(u); }
      }
    }
    const lab = refs.length ? await labels(refs) : {};
    const facts = [];
    for (const [pid, v] of raw) {
      let items = v.map((dv) => {
        const x = dv.value;
        if (dv.type === "wikibase-entityid") return { qid: x.id, text: lab[x.id] || x.id };
        if (dv.type === "time") return { text: fmtTime(x) };
        if (dv.type === "quantity") {
          const n = parseFloat(x.amount), u = String(x.unit || "").split("/").pop();
          return { text: (isNaN(n) ? x.amount : nf(n, Math.abs(n) >= 100 ? 0 : 2)) + (QID.test(u) ? " " + (UNITS[u] || lab[u] || "") : "") };
        }
        if (dv.type === "string") return pid === "P856" && isHttp(x) ? { text: host(x) || x, url: x } : { text: x };
        if (dv.type === "monolingualtext") return { text: x.text };
        return null;
      }).filter(Boolean);
      if (pid === "P1082") items = items.slice(0, 1);
      if (items.length) facts.push({ pid, label: FACTS.find((f) => f[0] === pid)[1], values: items });
    }
    const co = vals("P625")[0];
    const files = vals("P18").map((x) => x.value);
    const locator = vals("P242").map((x) => x.value)[0];
    const logoFile = [...vals("P154"), ...vals("P41"), ...vals("P94")].map((x) => x.value)[0];
    const cat = (vals("P373")[0] || {}).value;
    const sl = e.sitelinks || {};
    const enwiki = sl.enwiki && sl.enwiki.title;
    const [imgs, sum] = await Promise.all([images([...files, ...(locator ? [locator] : []), ...(logoFile ? [logoFile] : [])], cat), enwiki ? summary(enwiki) : null]);
    const norm = (s) => String(s || "").replace(/_/g, " ");
    const logo = logoFile ? imgs.find((i) => i.title === norm(logoFile)) : null;
    const loc = locator ? imgs.find((i) => i.title === norm(locator)) : null;
    const photos = imgs.filter((i) => i !== logo && i !== loc);
    return {
      qid, label: (e.labels && e.labels.en && e.labels.en.value) || qid,
      description: (e.descriptions && e.descriptions.en && e.descriptions.en.value) || "",
      aliases: ((e.aliases && e.aliases.en) || []).map((a) => a.value).slice(0, 6),
      facts, coord: co ? { lat: co.value.latitude, lon: co.value.longitude } : null,
      photos, logo, locator: loc, summary: sum, enwiki, category: cat,
      sitelinks: Object.keys(sl).filter((k) => /wiki$/.test(k) && k !== "commonswiki").length,
    };
  });
}
export async function wdResolve({ q = "", lat = null, lon = null, icao = null, maxKm = 30 }) {
  return cached("res:" + [q, lat && lat.toFixed(2), lon && lon.toFixed(2), icao].join("|"), 6 * 3600e3, async () => {
    let cands = [];
    if (icao && /^[A-Za-z0-9]{3,4}$/.test(icao)) {
      const d = await api(WD, { action: "query", list: "search", srsearch: "haswbstatement:P239=" + icao.toUpperCase(), srlimit: "3" });
      cands = ((d.query && d.query.search) || []).filter((s) => QID.test(s.title)).map((s) => ({ qid: s.title, why: "ICAO " + icao.toUpperCase() }));
    }
    if (!cands.length && q.trim()) {
      const d = await api(WD, { action: "wbsearchentities", search: q.trim().slice(0, 200), language: "en", type: "item", limit: "8" });
      const found = (d.search || []).map((s) => ({ qid: s.id, label: s.label, description: s.description }));
      if (found.length && lat != null && lon != null) {
        const ents = await api(WD, { action: "wbgetentities", ids: found.map((f) => f.qid).join("|"), props: "claims" });
        for (const f of found) {
          const c = ((((ents.entities || {})[f.qid] || {}).claims || {}).P625 || [])[0];
          const dv = c && c.mainsnak && c.mainsnak.datavalue;
          if (dv) { const k = km(lat, lon, dv.value.latitude, dv.value.longitude); if (k <= maxKm) cands.push({ ...f, km: k, why: "name match " + nf(k, 1) + " km away" }); }
        }
        cands.sort((a, b) => a.km - b.km);
      } else cands = found.map((f) => ({ ...f, why: "name match" }));
    }
    return { qid: cands.length ? cands[0].qid : null, candidates: cands.slice(0, 8) };
  });
}
export async function wpNearby(lat, lon, radius = 10000) {
  radius = Math.max(100, Math.min(10000, radius | 0));
  return cached("near:" + lat.toFixed(3) + "," + lon.toFixed(3) + "," + radius, 3600e3, async () => {
    const d = await api(WP, { action: "query", generator: "geosearch", ggscoord: lat + "|" + lon, ggsradius: String(radius), ggslimit: "40",
      prop: "coordinates|pageimages|description|pageprops", piprop: "thumbnail", pithumbsize: "480", ppprop: "wikibase_item", colimit: "50" });
    return Object.values((d.query && d.query.pages) || {}).map((p) => {
      const c = (p.coordinates || [])[0] || {};
      return { title: p.title, description: p.description, qid: p.pageprops && p.pageprops.wikibase_item, thumb: p.thumbnail && p.thumbnail.source,
        lat: c.lat, lon: c.lon, km: c.lat != null ? km(lat, lon, c.lat, c.lon) : null, url: "https://en.wikipedia.org/wiki/" + encodeURIComponent(String(p.title).replace(/ /g, "_")) };
    }).sort((a, b) => (a.km == null ? 1e9 : a.km) - (b.km == null ? 1e9 : b.km));
  });
}
async function wikiTitleToQid(title, lang = "en") {
  const d = await api("https://" + lang + ".wikipedia.org/w/api.php", { action: "query", prop: "pageprops", ppprop: "wikibase_item", redirects: "1", titles: title });
  const p = Object.values((d.query && d.query.pages) || {})[0];
  return p && p.pageprops && p.pageprops.wikibase_item;
}

/** Map a URL to the richest pane spec: Wikidata items and Wikipedia articles become entity cards. */
export async function specForUrl(url) {
  let u;
  try { u = new URL(url); } catch { return { type: "url", url }; }
  const wd = /(^|\.)wikidata\.org$/.test(u.hostname) && /\/(?:wiki|entity)\/(Q\d+)/.exec(u.pathname);
  if (wd) return { type: "entity", qid: wd[1] };
  const wp = /^([a-z-]+)\.(?:m\.)?wikipedia\.org$/.exec(u.hostname);
  const t = wp && /^\/wiki\/(.+)$/.exec(u.pathname);
  if (t && !/^(Special|File|Category|Talk|User|Help|Wikipedia|Template|Portal):/.test(decodeURIComponent(t[1]))) {
    try { const q = await wikiTitleToQid(decodeURIComponent(t[1]).replace(/_/g, " "), wp[1]); if (q) return { type: "entity", qid: q, tab: "article", article: url }; } catch { /* fall through */ }
  }
  return { type: "url", url };
}

// ---------------------------------------------------------------- layout
/** Grid placement for n tiles: {cols, rows, cells:[{c, r, cs, rs}]} (1-based CSS grid lines). */
export function tileLayout(n, wide = true) {
  if (n <= 0) return { cols: 1, rows: 1, cells: [] };
  if (!wide) { const cols = n === 1 ? 1 : 2, rows = Math.ceil(n / cols); return { cols, rows, cells: Array.from({ length: n }, (_, i) => ({ c: (i % cols) + 1, r: Math.floor(i / cols) + 1, cs: 1, rs: 1 })) }; }
  const grid = (cols, rows) => ({ cols, rows, cells: Array.from({ length: n }, (_, i) => ({ c: (i % cols) + 1, r: Math.floor(i / cols) + 1, cs: 1, rs: 1 })) });
  if (n === 1) return grid(1, 1);
  if (n === 2) return grid(2, 1);
  if (n === 3) return { cols: 2, rows: 2, cells: [{ c: 1, r: 1, cs: 1, rs: 2 }, { c: 2, r: 1, cs: 1, rs: 1 }, { c: 2, r: 2, cs: 1, rs: 1 }] };
  if (n === 4) return grid(2, 2);
  if (n === 5) return { cols: 3, rows: 2, cells: [{ c: 1, r: 1, cs: 1, rs: 2 }, { c: 2, r: 1, cs: 1, rs: 1 }, { c: 3, r: 1, cs: 1, rs: 1 }, { c: 2, r: 2, cs: 1, rs: 1 }, { c: 3, r: 2, cs: 1, rs: 1 }] };
  if (n === 6) return grid(3, 2);
  if (n === 7) return { cols: 4, rows: 2, cells: [{ c: 1, r: 1, cs: 1, rs: 2 }, ...[2, 3, 4].map((c) => ({ c, r: 1, cs: 1, rs: 1 })), ...[2, 3, 4].map((c) => ({ c, r: 2, cs: 1, rs: 1 }))] };
  if (n === 8) return grid(4, 2);
  if (n === 9) return grid(3, 3);
  return grid(4, Math.ceil(n / 4));
}

// ---------------------------------------------------------------- workspace
let uid = 0;

export class Dossier {
  constructor({ flyTo, select, highlight, story } = {}) {
    this.flyTo = flyTo || (() => {});
    this.select = select || (() => {});
    this.highlight = highlight || (() => {});
    this.story = story || null; // storyline panes: {overlay, replay(t)} (app.js)
    this.panes = [];
    this.focused = null;
    this.zoomed = null;
    this.state = "closed"; // closed | open | min
    this.max = false;
    this._build();
    this._intercept();
    // panes restored before their feature module registered (country.js, terror.js) render
    // once it has: modules fire "noctua:panels" after registering
    window.addEventListener("noctua:panels", () => {
      for (const p of this.panes) if (p._pendingExt && (window.__noctuaPanes || {})[p.spec.type]) { p._pendingExt = false; this._render(p); }
    });
    this._restore();
  }

  // ---- DOM scaffold ----
  _build() {
    const el = document.createElement("section");
    el.id = "dossier";
    el.className = "dossier hidden";
    el.setAttribute("aria-label", "Dossier browser");
    el.innerHTML =
      '<header class="ds-bar"><div class="ds-brand"><i></i>DOSSIER<span class="ds-count"></span></div>' +
      '<form class="ds-omni"><span>' + ICON.search + '</span><input placeholder="Search Wikidata, paste a link, or Q-id…" spellcheck="false"></form>' +
      '<div class="ds-ctl"><button type="button" data-a="retile" title="Re-tile (focused pane becomes master)">' + B.grid + "</button>" +
      '<button type="button" data-a="max" title="Expand workspace">' + B.max + "</button>" +
      '<button type="button" data-a="min" title="Minimise (B)">' + B.min + "</button>" +
      '<button type="button" data-a="closeall" title="Close all panes">' + B.close + "</button></div></header>" +
      '<div class="ds-grid"></div><div class="ds-empty"><div class="ds-empty-in"><b>Nothing open</b>Select something on the globe and press <kbd>I</kbd>, click any source link, or search above.</div></div>';
    document.body.appendChild(el);
    this.el = el;
    this.grid = el.querySelector(".ds-grid");
    const chip = document.createElement("button");
    chip.id = "dossier-chip";
    chip.className = "hidden";
    chip.title = "Restore dossier (B)";
    document.body.appendChild(chip);
    this.chip = chip;
    chip.addEventListener("click", () => this.show());

    el.querySelector(".ds-ctl").addEventListener("click", (e) => {
      const a = e.target.closest("button") && e.target.closest("button").dataset.a;
      if (a === "min") this.minimise();
      else if (a === "max") { this.max = !this.max; this.el.classList.toggle("max", this.max); this._layout(); }
      else if (a === "closeall") { this.panes.slice().forEach((p) => this.close(p, true)); this._save(); }
      else if (a === "retile" && this.focused) this.promote(this.focused);
    });
    el.querySelector(".ds-omni").addEventListener("submit", (e) => {
      e.preventDefault();
      const inp = e.target.querySelector("input"), v = inp.value.trim();
      if (!v) return;
      inp.value = "";
      if (isHttp(v)) this.openUrl(v);
      else if (QID.test(v.toUpperCase())) this.open({ type: "entity", qid: v.toUpperCase() });
      else this.open({ type: "search", q: v });
    });
    this._ro = new ResizeObserver(() => this._layout());
    this._ro.observe(this.grid);

    // lightbox
    const lb = document.createElement("div");
    lb.id = "ds-lightbox";
    lb.className = "hidden";
    lb.innerHTML = '<button class="lb-x" title="Close (Esc)">' + B.close + '</button><button class="lb-p" title="Previous (←)">' + B.back + '</button><figure><img alt=""><figcaption></figcaption></figure><button class="lb-n" title="Next (→)">' + B.fwd + "</button>";
    document.body.appendChild(lb);
    this.lb = lb;
    lb.addEventListener("click", (e) => {
      if (e.target.closest(".lb-x") || e.target === lb) this._lbClose();
      else if (e.target.closest(".lb-p")) this._lbStep(-1);
      else if (e.target.closest(".lb-n")) this._lbStep(1);
    });
  }

  // links clicked anywhere in the app open here (Ctrl / Shift / middle click -> system browser)
  _intercept() {
    document.addEventListener("click", (e) => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a || a.dataset.ext === "1" || a.closest("#ds-lightbox")) return;
      const href = a.getAttribute("href");
      if (!isHttp(href) || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      const inside = a.closest(".ds-pane");
      if (!inside && a.target !== "_blank") return;
      e.preventDefault();
      const pane = inside ? this.panes.find((p) => p.el === inside) : null;
      if (pane && !e.shiftKey && a.dataset.same === "1") this.navigateUrl(pane, href);
      else this.openUrl(href);
    }, true);
    document.addEventListener("keydown", (e) => {
      if (!this.lb.classList.contains("hidden")) {
        if (e.key === "Escape") { this._lbClose(); e.stopPropagation(); e.preventDefault(); }
        else if (e.key === "ArrowLeft") { this._lbStep(-1); e.stopPropagation(); }
        else if (e.key === "ArrowRight") { this._lbStep(1); e.stopPropagation(); }
        return;
      }
      if (this.state !== "open" || /INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) return;
      if (e.key === "Escape") { if (this.zoomed) this.zoom(null); else this.minimise(); e.stopPropagation(); e.preventDefault(); }
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "w" && this.focused) { this.close(this.focused); e.preventDefault(); e.stopPropagation(); }
      else if (e.altKey && e.key === "ArrowLeft" && this.focused) { this.history(this.focused, -1); e.preventDefault(); }
      else if (e.altKey && e.key === "ArrowRight" && this.focused) { this.history(this.focused, 1); e.preventDefault(); }
    }, true);
  }

  // ---- visibility ----
  show() { this.state = "open"; this.el.classList.remove("hidden"); this.chip.classList.add("hidden"); this._layout(); this._notify(); }
  minimise() {
    if (!this.panes.length) return this.hide();
    this.state = "min"; this.el.classList.add("hidden");
    this.chip.classList.remove("hidden");
    this.chip.innerHTML = "<i></i>DOSSIER <b>" + this.panes.length + "</b>" + this.panes.slice(0, 4).map((p) => "<span>" + esc(p.title || "") + "</span>").join("");
    this._notify();
  }
  hide() { this.state = "closed"; this.el.classList.add("hidden"); this.chip.classList.add("hidden"); this._notify(); }
  toggle() { if (this.state === "open") this.minimise(); else this.show(); }

  // ---- panes ----
  async openUrl(url, opts) { return this.open(await specForUrl(url), opts); }
  async navigateUrl(pane, url) { return this.navigate(pane, await specForUrl(url)); }

  /** Open a spec in a new pane (or focus an identical open one). */
  open(spec, { focus = true } = {}) {
    const key = specKey(spec);
    const dup = this.panes.find((p) => specKey(p.spec) === key);
    if (dup) { this.show(); this.focus(dup); if (spec.tab && dup.spec.type === "entity") this._setTab(dup, spec.tab); return dup; }
    if (this.panes.length >= MAX_PANES) this.close(this.panes.find((p) => p !== this.focused && !p.pinned) || this.panes[0], true);
    const pane = { id: ++uid, spec, hist: [spec], hi: 0, title: "Loading…", sub: "", coord: null, el: document.createElement("article") };
    pane.el.className = "ds-pane enter";
    pane.el.innerHTML = '<header class="ds-ph" draggable="true"><span class="ds-ico"></span><div class="ds-pt"><b></b><small></small></div><nav class="ds-pa">' +
      '<button data-a="back" title="Back (Alt+←)">' + B.back + '</button><button data-a="fwd" title="Forward (Alt+→)">' + B.fwd + "</button>" +
      '<span class="ds-mode"></span>' +
      '<button data-a="fly" title="Fly the globe here">' + B.fly + '</button><button data-a="star" title="Make master pane">' + B.star + "</button>" +
      '<button data-a="zoom" title="Zoom pane">' + B.zoom + '</button><a data-a="ext" data-ext="1" target="_blank" rel="noopener noreferrer" title="Open in system browser">' + B.ext + "</a>" +
      '<button data-a="close" title="Close (Ctrl+W)">' + B.close + '</button></nav></header><div class="ds-pb"></div>';
    this.panes.push(pane);
    this.grid.appendChild(pane.el);
    this._wire(pane);
    this.show();
    if (focus) this.focus(pane);
    this._layout();
    this._render(pane);
    setTimeout(() => pane.el.classList.remove("enter"), 260);
    this._save();
    return pane;
  }
  navigate(pane, spec) {
    pane.hist = pane.hist.slice(0, pane.hi + 1);
    pane.hist.push(spec); pane.hi = pane.hist.length - 1;
    pane.spec = spec; this._render(pane); this._save(); this._notify();
  }
  history(pane, d) {
    const i = pane.hi + d;
    if (i < 0 || i >= pane.hist.length) return;
    pane.hi = i; pane.spec = pane.hist[i]; this._render(pane); this._notify();
  }
  close(pane, quiet) {
    const i = this.panes.indexOf(pane);
    if (i < 0) return;
    this.panes.splice(i, 1);
    pane.el.remove();
    if (this.zoomed === pane) this.zoomed = null;
    if (this.focused === pane) this.focused = this.panes[Math.min(i, this.panes.length - 1)] || null;
    if (this.focused) this.focused.el.classList.add("focus");
    this._layout();
    if (!quiet) this._save();
    this._notify();
  }
  focus(pane) {
    for (const p of this.panes) p.el.classList.toggle("focus", p === pane);
    this.focused = pane;
    this._notify();
  }
  // which pane has focus and what is open, for feature modules (country.js globe sync)
  _notify() {
    window.dispatchEvent(new CustomEvent("noctua:dossier", { detail: { state: this.state, focused: this.focused ? this.focused.spec : null, specs: this.panes.map((p) => p.spec) } }));
  }
  promote(pane) {
    const i = this.panes.indexOf(pane);
    if (i > 0) { this.panes.splice(i, 1); this.panes.unshift(pane); this._layout(); this._save(); }
  }
  // intel console: the workspace expanded over the side panels with this pane alone
  console(pane, on) {
    if (!this.panes.includes(pane)) return;
    this.max = on; this.el.classList.toggle("max", on);
    if (on !== (this.zoomed === pane)) this.zoom(pane);
    else this._layout();
  }
  zoom(pane) {
    this.zoomed = this.zoomed === pane ? null : pane;
    this._layout();
  }
  swap(a, b) {
    const i = this.panes.indexOf(a), j = this.panes.indexOf(b);
    if (i < 0 || j < 0 || i === j) return;
    [this.panes[i], this.panes[j]] = [this.panes[j], this.panes[i]];
    this._layout(); this._save();
  }

  _layout() {
    const n = this.panes.length;
    this.el.classList.toggle("empty", !n);
    this.el.querySelector(".ds-count").textContent = n ? String(n) : "";
    if (!n) return;
    const w = this.grid.clientWidth;
    if (this.zoomed) {
      this.grid.style.gridTemplateColumns = "1fr"; this.grid.style.gridTemplateRows = "1fr";
      for (const p of this.panes) { p.el.style.display = p === this.zoomed ? "" : "none"; p.el.style.gridArea = "1 / 1 / 2 / 2"; }
      return;
    }
    const L = tileLayout(n, w > 900);
    this.grid.style.gridTemplateColumns = L.cols === 2 && n === 3 ? "1.25fr 1fr" : L.cols === 3 && n === 5 ? "1.3fr 1fr 1fr" : "repeat(" + L.cols + ", minmax(0, 1fr))";
    this.grid.style.gridTemplateRows = "repeat(" + L.rows + ", minmax(0, 1fr))";
    this.panes.forEach((p, i) => {
      const c = L.cells[i];
      p.el.style.display = "";
      p.el.style.gridArea = c.r + " / " + c.c + " / " + (c.r + c.rs) + " / " + (c.c + c.cs);
      p.el.classList.toggle("master", i === 0 && n > 1);
    });
  }

  _wire(pane) {
    const el = pane.el;
    el.addEventListener("pointerdown", () => this.focus(pane));
    el.querySelector(".ds-pa").addEventListener("click", (e) => {
      const b = e.target.closest("[data-a]");
      if (!b) return;
      const a = b.dataset.a;
      if (a === "ext") return; // plain link, opens the system browser
      if (a === "back") this.history(pane, -1);
      else if (a === "fwd") this.history(pane, 1);
      else if (a === "fly" && pane.coord) this.flyTo(pane.coord.lat, pane.coord.lon, pane.coord.alt || 2.5e4);
      else if (a === "star") this.promote(pane);
      else if (a === "zoom") this.zoom(pane);
      else if (a === "close") this.close(pane);
      else if (a === "mode") { pane.spec = { ...pane.spec, mode: b.dataset.m }; pane.hist[pane.hi] = pane.spec; this._render(pane); this._save(); }
    });
    const head = el.querySelector(".ds-ph");
    head.addEventListener("dblclick", (e) => { if (!e.target.closest("button,a")) this.zoom(pane); });
    head.addEventListener("dragstart", (e) => { this._drag = pane; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", "pane"); el.classList.add("dragging"); });
    head.addEventListener("dragend", () => { el.classList.remove("dragging"); this._drag = null; for (const p of this.panes) p.el.classList.remove("drop"); });
    el.addEventListener("dragover", (e) => { if (this._drag && this._drag !== pane) { e.preventDefault(); el.classList.add("drop"); } });
    el.addEventListener("dragleave", () => el.classList.remove("drop"));
    el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("drop"); if (this._drag && this._drag !== pane) this.swap(this._drag, pane); });
    // in-pane actions
    el.querySelector(".ds-pb").addEventListener("click", (e) => {
      const t = e.target.closest("[data-pivot],[data-qid],[data-tab],[data-img],[data-fly],[data-near],[data-radius],[data-sel]");
      if (!t) return;
      if (t.dataset.pivot) {
        let spec = null;
        try { spec = JSON.parse(t.dataset.pivot); } catch (err) { return; }
        if (e.target.closest("[data-fly]") && t.dataset.fly && !e.shiftKey) { const [la, lo] = t.dataset.fly.split(",").map(Number); this.flyTo(la, lo, 2.5e4); }
        if (e.shiftKey || e.ctrlKey) this.open(spec); else this.navigate(pane, spec);
      } else if (t.dataset.sel) this.select("events", t.dataset.sel);
      else if (t.dataset.qid) { const spec = { type: "entity", qid: t.dataset.qid }; if (e.shiftKey || e.ctrlKey) this.open(spec); else this.navigate(pane, spec); }
      else if (t.dataset.tab) this._setTab(pane, t.dataset.tab);
      else if (t.dataset.img != null) this._lbOpen(pane._photos || [], +t.dataset.img);
      else if (t.dataset.fly) { const [la, lo] = t.dataset.fly.split(",").map(Number); this.flyTo(la, lo, 2.5e4); }
      else if (t.dataset.near) { const [la, lo] = t.dataset.near.split(",").map(Number); this.open({ type: "nearby", lat: la, lon: lo, label: t.dataset.label || "" }); }
      else if (t.dataset.radius) { pane.spec = { ...pane.spec, radius: +t.dataset.radius }; pane.hist[pane.hi] = pane.spec; this._render(pane); this._save(); }
    });
  }

  _head(pane, { title, sub, icon, ext, coord, modes }) {
    pane.title = title; pane.sub = sub; pane.coord = coord || null;
    const el = pane.el;
    el.querySelector(".ds-ico").innerHTML = ICON[icon] || ((window.__noctuaPanes || {})[icon] || {}).icon || ICON.url;
    el.querySelector(".ds-pt b").textContent = title;
    el.querySelector(".ds-pt small").textContent = sub || "";
    el.querySelector(".ds-pt").title = title + (sub ? "\n" + sub : "");
    const ex = el.querySelector('[data-a="ext"]');
    if (ext) { ex.href = ext; ex.style.display = ""; } else ex.style.display = "none";
    el.querySelector('[data-a="fly"]').disabled = !coord;
    el.querySelector('[data-a="back"]').disabled = pane.hi <= 0;
    el.querySelector('[data-a="fwd"]').disabled = pane.hi >= pane.hist.length - 1;
    el.querySelector(".ds-mode").innerHTML = modes ? modes.map(([m, label, on, dis]) => '<button data-a="mode" data-m="' + m + '" class="' + (on ? "on" : "") + '"' + (dis ? " disabled title=\"" + esc(dis) + "\"" : "") + ">" + label + "</button>").join("") : "";
    if (this.state === "min") this.minimise();
  }

  async _render(pane) {
    const spec = pane.spec, body = pane.el.querySelector(".ds-pb"), tok = (pane._tok = {});
    body.innerHTML = skeleton();
    body.scrollTop = 0;
    const stale = () => pane._tok !== tok;
    try {
      if (spec.type === "entity") await this._entity(pane, body, stale);
      else if (spec.type === "url") await this._url(pane, body, stale);
      else if (spec.type === "nearby") await this._nearby(pane, body, stale);
      else if (spec.type === "search") await this._search(pane, body, stale);
      else if (spec.type === "resolve") await this._resolve(pane, body, stale);
      else if (spec.type === "intel") await renderIntel(spec, body, { head: (h) => this._head(pane, h), stale, wdResolve, wdEntity, highlight: this.highlight,
        console: (on) => this.console(pane, on), consoleOn: () => this.max && this.zoomed === pane, focused: () => this.focused === pane || this.panes.length === 1 });
      else if (spec.type === "story") {
        const st = this.story || {}, noop = () => {};
        await renderStoryline(spec, body, { head: (h) => this._head(pane, h), stale, highlight: this.highlight, flyTo: this.flyTo,
          overlay: st.overlay || { show: noop, fit: noop, setTime: noop, clear: noop }, replay: st.replay,
          intelEvent: (id, label) => this.open({ type: "intel", kind: "event", id, label: label || id }) });
      } else if ((window.__noctuaPanes || {})[spec.type]) { // panes registered by feature modules (terror.js, country.js)
        await window.__noctuaPanes[spec.type].render(spec, body, { head: (h) => this._head(pane, h), stale, flyTo: this.flyTo, highlight: this.highlight,
          select: this.select, open: (s, o) => this.open(s, o),
          // a pane that keeps its own state in its spec (country.js) updates it in place: persisted, no re-render
          setSpec: (s) => { pane.spec = s; pane.hist[pane.hi] = s; this._save(); } });
      } else pane._pendingExt = true; // its module has not registered yet: rendered on "noctua:panels"
    } catch (err) {
      if (stale()) return;
      this._head(pane, { title: pane.title === "Loading…" ? "Error" : pane.title, sub: spec.url || spec.qid || "", icon: spec.type, ext: spec.url });
      body.innerHTML = '<div class="ds-err"><b>Could not load</b><span>' + esc(err.message || err) + "</span>" + (spec.url ? '<a href="' + esc(spec.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">Open in system browser</a>' : "") + "</div>";
    }
  }

  // ---- entity ----
  async _entity(pane, body, stale) {
    const e = await wdEntity(pane.spec.qid);
    if (stale()) return;
    pane._photos = e.photos;
    pane._ent = e;
    const inst = (e.facts.find((f) => f.pid === "P31") || { values: [] }).values.map((v) => v.text).slice(0, 2).join(" · ");
    this._head(pane, { title: e.label, sub: e.qid + (inst ? " · " + inst : ""), icon: "entity", ext: (e.summary && e.summary.url) || "https://www.wikidata.org/wiki/" + e.qid,
      coord: e.coord ? { ...e.coord, alt: 2e4 } : null });
    const hero = (e.photos[0] && e.photos[0].thumb) || (e.summary && (e.summary.original || e.summary.thumb)) || "";
    body.innerHTML =
      '<div class="en">' +
      '<div class="en-hero' + (hero ? "" : " nohero") + '">' + (hero ? '<img class="en-bg" src="' + esc(hero) + '" alt="" referrerpolicy="no-referrer"' + (e.photos[0] ? ' data-img="0"' : "") + ">" : "") +
      '<div class="en-ht">' + (e.logo ? '<img class="en-logo" src="' + esc(e.logo.thumb) + '" alt="">' : "") +
      '<div><div class="en-eb">' + esc(e.qid) + (inst ? " · " + esc(inst) : "") + "</div><h1>" + esc(e.label) + "</h1>" + (e.description ? "<p>" + esc(e.description) + "</p>" : "") + "</div></div></div>" +
      '<nav class="en-tabs">' + tab("overview", "Overview") + tab("photos", "Photos <sup>" + e.photos.length + "</sup>", !e.photos.length) +
      tab("article", "Article", !e.enwiki) + tab("wikidata", "Wikidata") + "</nav>" +
      '<div class="en-panel"></div></div>';
    this._setTab(pane, pane.spec.tab || "overview");
  }
  _setTab(pane, t) {
    const e = pane._ent;
    if (!e) return;
    if (pane.spec.tab !== t) { pane.spec = { ...pane.spec, tab: t }; pane.hist[pane.hi] = pane.spec; this._save(); }
    for (const b of pane.el.querySelectorAll(".en-tabs [data-tab]")) b.classList.toggle("on", b.dataset.tab === t);
    const P = pane.el.querySelector(".en-panel");
    pane.el.querySelector(".en").classList.toggle("framed", t === "article" || t === "wikidata");
    if (t === "photos") P.innerHTML = gallery(e.photos, 0) + attribution(e);
    else if (t === "article") {
      const src = pane.spec.article ? pane.spec.article.replace("://en.wikipedia.org", "://en.m.wikipedia.org") : e.summary && e.summary.mobile;
      P.innerHTML = src ? frame(src) : '<div class="ds-err">No English Wikipedia article</div>';
    } else if (t === "wikidata") P.innerHTML = frame("https://m.wikidata.org/wiki/" + e.qid);
    else P.innerHTML = overview(e);
  }

  // ---- web page ----
  async _url(pane, body, stale) {
    const url = pane.spec.url, h = host(url);
    const wiki = WIKIMEDIA_HOST.test(h);
    let mode = pane.spec.mode;
    if (wiki) {
      this._head(pane, { title: decodeURIComponent((new URL(url).pathname.split("/").pop() || h)).replace(/_/g, " "), sub: h, icon: "url", ext: url,
        modes: [["live", "Live", true]] });
      body.innerHTML = frame(url.replace("://en.wikipedia.org", "://en.m.wikipedia.org"));
      return;
    }
    this._head(pane, { title: h || url, sub: url, icon: "url", ext: url });
    const r = await fetchJson("/api/browse/read?url=" + encodeURIComponent(url), 100000);
    if (stale()) return;
    if (!mode) mode = r.frameable && r.paragraphs.length < 3 ? "live" : "reader";
    if (mode === "live" && !r.frameable) mode = "reader";
    this._head(pane, { title: r.title || h, sub: (r.site || h) + (r.via === "alterlab" ? " · via AlterLab" : ""), icon: "url", ext: r.final_url || url,
      modes: [["reader", "Reader", mode === "reader"], ["live", "Live", mode === "live", r.frameable ? "" : "This site does not allow embedding"]] });
    if (mode === "live") { body.innerHTML = frame(r.final_url || url); return; }
    const when = r.published ? new Date(r.published) : null;
    body.innerHTML = '<article class="rd">' +
      (r.image ? '<div class="rd-hero"><img src="' + esc(r.image) + '" alt="" referrerpolicy="no-referrer" onerror="this.parentNode.remove()"></div>' : "") +
      '<div class="rd-in"><div class="rd-site">' + esc(r.site || h) + (when && !isNaN(when) ? ' · <time title="' + esc(utcTitle(when)) + '">' + esc(fmtDateTime(when, { zone: true })) + "</time>" : "") + (r.author ? " · " + esc(r.author) : "") + "</div>" +
      "<h1>" + esc(r.title || h) + "</h1>" + (r.description ? '<p class="rd-lede">' + esc(r.description) + "</p>" : "") +
      (r.paragraphs.length ? r.paragraphs.map((p) => "<p>" + esc(p) + "</p>").join("") :
        '<div class="ds-err"><b>No readable text</b><span>' + esc(r.status >= 400 ? "The site answered HTTP " + r.status + " (likely a bot wall)." : "The page has no extractable article text.") + '</span><a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">Open in system browser</a></div>') +
      '<footer class="rd-foot">Reader view extracted by NOCTUA · <a href="' + esc(r.final_url || url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">original</a></footer></div></article>';
  }

  // ---- nearby ----
  async _nearby(pane, body, stale) {
    const { lat, lon, label } = pane.spec, radius = pane.spec.radius || 10000;
    this._head(pane, { title: "Near " + (label || nf(lat, 3) + ", " + nf(lon, 3)), sub: "Wikipedia · " + radius / 1000 + " km radius", icon: "nearby", coord: { lat, lon, alt: 3e4 } });
    const items = await wpNearby(lat, lon, radius);
    if (stale()) return;
    pane._photos = items.filter((i) => i.thumb).map((i) => ({ thumb: i.thumb, url: i.thumb, title: i.title, caption: i.description || "", page: i.url }));
    body.innerHTML = '<div class="nb"><div class="nb-bar">' + [1000, 5000, 10000].map((r) => '<button data-radius="' + r + '" class="' + (r === radius ? "on" : "") + '">' + r / 1000 + " km</button>").join("") +
      "<span>" + items.length + " articles</span></div>" +
      (items.length ? '<div class="nb-grid">' + items.map((i) =>
        '<div class="nb-card"' + (i.qid ? ' data-qid="' + esc(i.qid) + '"' : "") + ">" +
        (i.thumb ? '<div class="nb-img"><img loading="lazy" src="' + esc(i.thumb) + '" alt=""></div>' : '<div class="nb-img none">' + ICON.nearby + "</div>") +
        '<div class="nb-tx"><b>' + esc(i.title) + "</b><small>" + esc(i.description || "") + "</small></div>" +
        '<div class="nb-meta"><span>' + (i.km != null ? nf(i.km, i.km < 10 ? 1 : 0) + " km" : "") + "</span>" +
        (i.lat != null ? '<button data-fly="' + i.lat + "," + i.lon + '" title="Fly the globe here">' + B.fly + "</button>" : "") +
        (i.qid ? "" : '<a href="' + esc(i.url) + '" data-same="1">article</a>') + "</div></div>").join("") + "</div>"
        : '<div class="ds-err"><b>No Wikipedia articles within ' + radius / 1000 + " km</b></div>") + "</div>";
  }

  // ---- search / resolve ----
  async _search(pane, body, stale) {
    const q = pane.spec.q;
    this._head(pane, { title: "“" + q + "”", sub: "Wikidata search", icon: "search" });
    const r = await wdResolve({ q });
    if (stale()) return;
    body.innerHTML = candidates(r.candidates, q);
  }
  async _resolve(pane, body, stale) {
    const s = pane.spec;
    this._head(pane, { title: s.label || s.q || s.icao || "Resolving…", sub: "Finding the Wikidata item…", icon: "search" });
    const r = await wdResolve(s);
    if (stale()) return;
    if (r.qid) { const spec = { type: "entity", qid: r.qid }; pane.hist[pane.hi] = spec; pane.spec = spec; return this._render(pane); }
    if (s.lat != null) { const spec = { type: "nearby", lat: s.lat, lon: s.lon, label: s.label || s.q }; pane.hist[pane.hi] = spec; pane.spec = spec; return this._render(pane); }
    this._head(pane, { title: s.label || s.q, sub: "No Wikidata match", icon: "search" });
    body.innerHTML = candidates(r.candidates, s.q);
  }

  // ---- lightbox ----
  _lbOpen(list, i) { if (!list.length) return; this._lb = { list, i }; this.lb.classList.remove("hidden"); this._lbShow(); }
  _lbStep(d) { if (!this._lb) return; const n = this._lb.list.length; this._lb.i = (this._lb.i + d + n) % n; this._lbShow(); }
  _lbClose() { this.lb.classList.add("hidden"); this._lb = null; }
  _lbShow() {
    const { list, i } = this._lb, im = list[i];
    const img = this.lb.querySelector("img");
    img.src = im.thumb; img.alt = im.title || "";
    this.lb.querySelector("figcaption").innerHTML = "<b>" + esc(im.caption || im.title || "") + "</b><span>" +
      [im.author && "© " + esc(im.author), im.licence && esc(im.licence), im.date && esc(im.date), (i + 1) + " / " + list.length].filter(Boolean).join(" · ") + "</span>" +
      (im.page ? '<a href="' + esc(im.page) + '" target="_blank" rel="noopener noreferrer" data-ext="1">Source on Wikimedia Commons</a>' : "");
  }

  // ---- persistence ----
  _save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ panes: this.panes.map((p) => p.spec), state: this.state === "open" ? "open" : "min" })); } catch { /* quota */ }
  }
  _restore() {
    let s;
    try { s = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { s = null; }
    if (!s || !Array.isArray(s.panes) || !s.panes.length) return;
    for (const spec of s.panes.slice(0, MAX_PANES)) if (spec && spec.type) this.open(spec, { focus: false });
    if (this.panes[0]) this.focus(this.panes[0]);
    this.minimise(); // restored minimised: never cover the globe on start
  }
}

function specKey(s) {
  if (s.type === "entity") return "e:" + s.qid;
  if (s.type === "url") return "u:" + s.url;
  if (s.type === "nearby") return "n:" + (+s.lat).toFixed(3) + "," + (+s.lon).toFixed(3);
  if (s.type === "search") return "s:" + s.q;
  if (s.type === "intel") return intelKey(s);
  if (s.type === "story") return storyKey(s);
  const ext = (window.__noctuaPanes || {})[s.type];
  if (ext && ext.key) return ext.key(s);
  // a feature pane restored before its module registered: keyed by its whole spec, so two of them
  // (UA and RU country panes, two Terror Watch panes) are not taken for duplicates of each other
  if (s.type !== "resolve") return "x:" + JSON.stringify(s);
  return "r:" + [s.q, s.icao, s.lat, s.lon].join("|");
}
const tab = (id, label, disabled) => '<button data-tab="' + id + '"' + (disabled ? " disabled" : "") + ">" + label + "</button>";
const frame = (src) => '<iframe class="ds-frame" src="' + esc(src) + '" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-popups allow-forms allow-popups-to-escape-sandbox" loading="lazy"></iframe>';
const skeleton = () => '<div class="ds-skel"><i style="height:38%"></i><i style="width:62%"></i><i style="width:88%"></i><i style="width:74%"></i><i style="width:80%"></i></div>';

// justified photo tiles: each tile grows in proportion to its aspect ratio
function gallery(photos, limit) {
  const list = limit ? photos.slice(0, limit) : photos;
  if (!list.length) return '<div class="ds-err">No photos on Wikimedia Commons</div>';
  return '<div class="jg">' + list.map((p, i) => {
    const ar = Math.max(0.4, Math.min(3, (p.w || 4) / (p.h || 3)));
    return '<figure data-img="' + i + '" style="--ar:' + ar.toFixed(3) + '" title="' + esc(p.caption || p.title) + '">' +
      '<img loading="lazy" src="' + esc(p.thumb) + '" alt="' + esc(p.title) + '" style="aspect-ratio:' + ar.toFixed(3) + '">' +
      (p.author ? "<figcaption>" + esc(p.author) + "</figcaption>" : "") + "</figure>";
  }).join("") + "<i></i></div>";
}
function attribution(e) {
  return '<p class="en-attr">Photos from Wikimedia Commons' + (e.category ? ' · <a href="https://commons.wikimedia.org/wiki/Category:' + esc(encodeURIComponent(e.category.replace(/ /g, "_"))) + '" target="_blank" rel="noopener noreferrer" data-ext="1">Category:' + esc(e.category) + "</a>" : "") +
    ". Each image carries its own author and licence (shown in the viewer).</p>";
}
function overview(e) {
  const facts = e.facts.filter((f) => f.pid !== "P31");
  const factRows = facts.map((f) => "<dt>" + esc(f.label) + "</dt><dd>" + f.values.map((v) =>
    v.qid ? '<button class="chip" data-qid="' + esc(v.qid) + '" title="Open (Shift: new pane)">' + esc(v.text) + "</button>" :
      v.url ? '<a href="' + esc(v.url) + '" target="_blank" rel="noopener noreferrer">' + esc(v.text) + "</a>" : "<span>" + esc(v.text) + "</span>").join("") + "</dd>").join("");
  const sum = e.summary && e.summary.extract;
  return '<div class="en-ov"><div class="en-main">' +
    (sum ? '<div class="en-sum"><p>' + esc(sum) + '</p><button class="en-more" data-tab="article">Read the full article →</button></div>' : "") +
    (factRows ? '<dl class="en-facts">' + factRows + "</dl>" : "") +
    (e.aliases.length ? '<div class="en-alias"><span>Also known as</span>' + e.aliases.map((a) => "<em>" + esc(a) + "</em>").join("") + "</div>" : "") +
    '</div><aside class="en-side">' +
    (e.photos.length ? '<div class="en-sec"><h4>Photos <button data-tab="photos">all ' + e.photos.length + " →</button></h4>" + gallery(e.photos, 7) + "</div>" : "") +
    (e.coord ? '<div class="en-sec en-geo"><h4>Location</h4>' + (e.locator ? '<img src="' + esc(e.locator.thumb) + '" alt="Locator map">' : "") +
      '<div class="en-coord"><span class="mono">' + nf(e.coord.lat, 4) + ", " + nf(e.coord.lon, 4) + '</span><button data-fly="' + e.coord.lat + "," + e.coord.lon + '">' + B.fly + ' Fly to</button><button data-near="' + e.coord.lat + "," + e.coord.lon + '" data-label="' + esc(e.label) + '">' + ICON.nearby + " Nearby</button></div></div>" : "") +
    '<div class="en-sec en-src"><h4>Sources</h4><a href="https://www.wikidata.org/wiki/' + esc(e.qid) + '" target="_blank" rel="noopener noreferrer" data-ext="1">Wikidata ' + esc(e.qid) + "</a>" +
    (e.summary && e.summary.url ? '<a href="' + esc(e.summary.url) + '" target="_blank" rel="noopener noreferrer" data-ext="1">English Wikipedia</a>' : "") +
    "<small>In " + e.sitelinks + " Wikipedia language editions · Wikidata CC0 · text CC BY-SA</small></div></aside></div>";
}
function candidates(list, q) {
  if (!list.length) return '<div class="ds-err"><b>No Wikidata items for “' + esc(q || "") + "”</b></div>";
  return '<ul class="cands">' + list.map((c) => '<li data-qid="' + esc(c.qid) + '"><b>' + esc(c.label || c.qid) + "</b><small>" + esc(c.description || "") + "</small><em>" + esc(c.qid) + (c.why ? " · " + esc(c.why) : "") + "</em></li>").join("") + "</ul>";
}
