// Event dossier: the inspector pane for a news / sensor event, in the ops-center style of
// dossier-kit. Built from the event record (GET /api/events/{id}) plus one aggregation call
// (GET /api/intel?kind=event&id=...&sections=...) for assessment, timeline, imagery, sensor
// detail (conflict scores), reference assets and nearby events. Every value shown is read
// from those records; nothing is estimated client-side except distance / bearing / MGRS.
//
// The inspector re-renders every 5 s for "time since" labels: this pane rebuilds its DOM only
// when the record changes (id, detail loaded, updated_at, original-text toggle) and otherwise
// just refreshes its [data-since] labels, so the imagery slider, scroll position and open
// sections survive. Async sections are cached per event and redrawn from the cache.
import { esc, nf, evTitle, trPending, TR_TAG, hasOriginal, isEn } from "/static/js/ui.js";
import { fetchJson, fetchBlob } from "/static/js/api.js";
import { uiGlyph, threatColor, eventCat, EVENT_STATUS } from "/static/js/symbology.js";
import { fmtUtc, localHint, relMin, since, bandText, distKm, bearing, compass, kmText, mgrs, latLonText, sec, wireSections, chip, gauge, bars, copyBtn, wireCopy, copyText, toast, download } from "/static/js/panels/dossier-kit.js";
import { renderTimeline, wireTimeline } from "/static/js/panels/timeline-view.js";
import { imageryHtml, wireImagery } from "/static/js/imagery.js";

const INTEL_SECTIONS = "assessment,timeline,imagery,sensors,assets,related,history,sources,storyline";
const LEVEL = { known: ["KNOWN", "ok"], corroborated: ["CORROB", "cor"], unconfirmed: ["UNCONF", "unc"], caveat: ["CAVEAT", "cav"] };
const SRC_KIND = { rss: "RSS", telegram: "TG", gdelt: "GDELT", nga: "NGA", news_alterlab: "WEB", alterlab: "WEB", acled: "ACLED", ucdp: "UCDP", reliefweb: "RW" };
const ASSET = { base: "Base", airport: "Airfield", port: "Port", nuclear: "Nuclear", chokepoint: "Chokepoint" };
const SENSOR = { fire: "FIRMS", quake: "USGS", hazard: "Hazard", aircraft: "ADS-B", ship: "AIS", gnss_cell: "GNSS cell", outage: "IODA", radiation: "Probe", ais_gap: "AIS gap" };
// sensor-derived events: the detecting sensor's readings are detection evidence, not corroboration
const isSensor = (d) => d && (d.origin === "sensor" || d.source === "sensor");
const PREC_SENSOR = { gnss_jam: "H3 res-3 cell (~60 km edge)", outage: "country (IODA)", squawk: "aircraft position", radiation: "probe position" };
// extracted fields already shown elsewhere in the pane
const FIELD_LABEL = { cause_stated: "Stated cause", event_type: "Type", where: "Where", when: "When", actors: "Actors", actor: "Actor", casualties: "Casualties", location: "Location", weapons_platforms: "Weapons", consequences: "Consequences" };
const srcKind = (s) => String(s || "").split(":")[0];
const srcName = (s) => String(s || "").split(":").slice(1).join(":") || String(s || "");
const tok = (s) => new Set(String(s || "").toLowerCase().split(/[\W_]+/u).filter((w) => w.length > 2));
const jacc = (a, b) => { const A = tok(a), B = tok(b); if (!A.size || !B.size) return 0; let n = 0; A.forEach((w) => { if (B.has(w)) n++; }); return n / (A.size + B.size - n); };

export class EventDossier {
  constructor(hooks) {
    this.h = hooks; // {flyTo(lat,lon,alt), highlight(points|null), intel(), select(kind,id), openUrl(url), story(id, label)}
    this.cache = new Map(); // event id -> {intel, err, at}
    this.key = null; this.root = null; this.showOrig = false; this.origFor = null;
  }

  // the pane's DOM lives in `dz`; returns true when it (re)built
  render(dz, it, detail, nearNuc) {
    if (this.origFor !== it.id) { this.origFor = it.id; this.showOrig = false; }
    const d = detail || it;
    const key = [it.id, detail ? 1 : 0, d.updated_at || it.updated_at, this.showOrig ? 1 : 0, nearNuc ? 1 : 0].join("|");
    this.it = it; this.d = d; this.detail = detail; this.nearNuc = nearNuc;
    if (key === this.key && this.root && dz.firstElementChild === this.root) { this._tick(); return false; }
    const refetch = !this.key || this.key.split("|")[0] !== it.id || this.key.split("|")[2] !== key.split("|")[2];
    this.key = key;
    dz.innerHTML = this._html();
    this.root = dz.firstElementChild;
    this._wire();
    this._fillAsync();
    if (refetch || !this.cache.has(it.id)) this._load(it.id);
    return true;
  }
  forget() { this.key = null; this.root = null; this.h.highlight(null); }

  async _load(id) {
    const c = this.cache.get(id);
    if (c && c.pending) return;
    this.cache.set(id, { ...(c || {}), pending: true });
    try {
      const j = await fetchJson("/api/intel?kind=event&id=" + encodeURIComponent(id) + "&sections=" + INTEL_SECTIONS, 20000);
      this.cache.set(id, { intel: j.sections || {}, at: Date.now() });
    } catch (e) {
      this.cache.set(id, { ...(c || {}), err: String((e && e.message) || e) });
    }
    if (this.cache.size > 30) this.cache.delete(this.cache.keys().next().value);
    if (this.it && this.it.id === id && this.root && this.root.isConnected) this._fillAsync();
  }

  // ---------------------------------------------------------------- markup
  _target() {
    // where the thing is: resolved impact site, else FIRMS hull centroid, else the event point
    const d = this.d, imp = d.impact || {};
    if (imp.best && imp.best.lat != null) return { lat: imp.best.lat, lon: imp.best.lon, basis: imp.best.name || "impact site" };
    const hull = (imp.areas || []).find((a) => a.kind === "firms_hull" && a.geometry && a.geometry.type === "Polygon");
    if (hull) {
      const ring = hull.geometry.coordinates[0].slice(0, -1);
      return { lat: ring.reduce((s, p) => s + p[1], 0) / ring.length, lon: ring.reduce((s, p) => s + p[0], 0) / ring.length, basis: "FIRMS hotspot hull" };
    }
    return null;
  }

  _html() {
    const it = this.it, d = this.d, cat = eventCat(it.category), conf = Math.min(1, Math.max(0, +(d.confidence ?? it.confidence) || 0));
    const tx = { title: d.title ?? it.title, title_en: d.title_en ?? it.title_en, summary: d.summary, summary_en: d.summary_en, lang: d.lang ?? it.lang };
    if (!Object.hasOwn(d, "title_en") && !Object.hasOwn(it, "title_en")) delete tx.title_en;
    const docs = d.documents || [];
    const docOrig = docs.some((x) => x.title_en && x.title && x.title_en !== x.title);
    const langName = tx.lang || docs.map((x) => x.lang || x.language).find((l) => l && !isEn(l)) || "source";
    const status = String(it.status || d.status || "");
    const stCls = status.replace(/[^a-z-]/g, "");
    const color = threatColor("events", it);
    const m = mgrs(it.lat, it.lon), place = [d.place_name, d.country].filter(Boolean).join(", ");
    const kinds = [...new Set(docs.map((x) => srcKind(x.source)))];
    const firstRep = docs.map((x) => x.published_at).filter(Boolean).sort()[0] || it.t_start;
    const title = this.showOrig && hasOriginal(tx) ? tx.title : evTitle(tx) || it.id;
    const summary = this.showOrig && tx.summary ? tx.summary : tx.summary_en || tx.summary;

    const sensorEv = isSensor(d) || isSensor(it), sb = d.sensor || null, det = (sb && sb.detector) || d.detector || it.subcategory;
    const classBar = '<div class="dk-class st-' + stCls + '"><span>INTSUM // ' + esc((d.origin || "news").toUpperCase()) + "</span><b>" + esc((EVENT_STATUS[status] || status).toUpperCase()) + "</b>" +
      '<span class="dk-r" title="' + esc(localHint(firstRep, null)) + '">' + (sensorEv ? "DETECTED " : "FIRST RPT ") + '<i data-since="' + esc(firstRep || "") + '">' + esc(since(firstRep)) + "</i> AGO</span></div>";
    const head = '<header class="dk-head" style="--threat:' + color + '"><div class="dz-glyph" style="color:' + color + '">' + uiGlyph("events", it) + "</div>" +
      '<div class="dk-ht"><div class="dz-eyebrow">' + esc(cat[0]) + (it.subcategory ? " · " + esc(String(it.subcategory).replace(/_/g, " ")) : "") + "</div>" +
      '<h2 class="dz-title">' + esc(title) + (trPending(tx) ? " " + TR_TAG : "") + '</h2><div class="dz-sub">' + esc(place || it.precision || "") + "</div></div>" + gauge(conf) + "</header>";
    const chips = '<div class="dk-chips">' + chip(EVENT_STATUS[status] || status, "st st-" + stCls) +
      (d.verdict ? chip("verdict " + d.verdict, d.verdict === "supported" ? "ok" : "warn", "Extraction verification verdict") : "") +
      kinds.map((k) => chip(SRC_KIND[k] || k, "src")).join("") +
      (sensorEv ? chip("Sensor-derived" + (sb ? " · " + sb.sensor : ""), "sens", "Detected by this sensor; its readings are detection evidence, not independent corroboration") + (sb && sb.details && sb.details.lifecycle ? chip(sb.details.lifecycle, sb.details.lifecycle === "active" ? "warn" : "src", "Lifecycle from the latest reading") : "")
        : (d.corroboration_kinds || []).map((k) => chip(SENSOR[k] || k, "sens", "Sensor corroboration: " + k)).join("")) +
      (d.gdelt_ids && d.gdelt_ids.length ? chip("GDELT ×" + d.gdelt_ids.length, "src", d.gdelt_ids.join(", ")) : "") +
      (this.nearNuc ? chip("Near nuclear site", "nuc", "Within 50 km of " + this.nearNuc.site.name) : "") +
      (hasOriginal(tx) || docOrig ? '<button type="button" class="orig-tg ghost' + (this.showOrig ? " on" : "") + '" data-a="orig" title="Show the source-language text">Original (' + esc(langName) + ")</button>" : "") + "</div>";
    const tgt = this._target();
    const loc = '<div class="dk-loc"><div><span>POS</span><b>' + esc(latLonText(it.lat, it.lon)) + "</b>" + copyBtn(nf(it.lat, 5).replace(/,/g, "") + ", " + nf(it.lon, 5).replace(/,/g, ""), "copy") + "</div>" +
      (m ? "<div><span>MGRS</span><b>" + esc(m) + "</b>" + copyBtn(m.replace(/ /g, ""), "copy") + "</div>" : "") +
      "<div><span>PREC</span><b>" + esc((sensorEv && PREC_SENSOR[det]) || it.precision || "–") + (d.geo_confidence != null ? " · geo " + nf(d.geo_confidence, 2) : "") + (d.impact_precision ? " · impact " + esc(d.impact_precision) : "") + "</b></div>" +
      (tgt ? '<div><span>SITE</span><b title="' + esc(tgt.basis) + '">' + esc(latLonText(tgt.lat, tgt.lon, 4)) + " · " + esc(kmText(distKm(it.lat, it.lon, tgt.lat, tgt.lon))) + " " + compass(bearing(it.lat, it.lon, tgt.lat, tgt.lon)) + "</b></div>" : "") + "</div>";
    const tools = '<div class="dk-tools">' +
      '<button type="button" data-a="fly" title="Fly the camera to the event">Fly to</button>' +
      '<button type="button" data-a="zoom" title="Zoom in close on ' + esc(tgt ? tgt.basis : "the event location") + '">Zoom site</button>' +
      '<button type="button" data-a="intel" title="All-source intel buildup (I)">Intel <kbd>I</kbd></button>' +
      '<button type="button" data-a="brief" title="Copy an intel brief (Markdown) to the clipboard">Brief</button>' +
      '<button type="button" data-a="geojson" title="Download GeoJSON: event, impact areas, evidence">GeoJSON</button>' +
      '<button type="button" data-a="kml" title="Download KML for Google Earth">KML</button>' +
      '<button type="button" data-a="record" title="Show the full JSON record">Record</button></div>';
    const sum = summary ? '<div class="dk-sum">' + esc(summary) + "</div>" : "";
    const nuc = this.nearNuc ? '<div class="nucnear">Near nuclear facility: <b>' + esc(this.nearNuc.site.name) + "</b> (" + nf(this.nearNuc.km, 1) + " km)</div>" : "";

    const slot = (n) => '<div data-slot="' + n + '"><div class="dz-empty">' + (this.detail ? "Loading…" : "Loading record…") + "</div></div>";
    return '<div class="dk-root ev">' + classBar + head + chips + '<div data-slot="storyline"></div>' + loc + tools + sum + nuc +
      sec("assess", "Assessment", slot("assessment"), { meta: '<span data-meta="assessment"></span>' }) +
      sec("imagery", "Satellite imagery", slot("imagery"), { meta: '<span data-meta="imagery"></span>' }) +
      sec("evidence", sensorEv ? "Detection evidence" : "Sensor evidence", this._evidence(), { meta: esc(this._evidenceMeta()) }) +
      sec("timeline", "Timeline", slot("timeline"), { meta: "UTC · hover for local" }) +
      sec("sources", "Sources", this._sources(), { meta: docs.length ? docs.length + " reports · " + new Set(docs.map((x) => x.source)).size + " outlets" : "" }) +
      this._fields() +
      sec("context", "Context", slot("context"), { meta: '<span data-meta="context"></span>' }) +
      sec("prov", "Provenance", '<div class="prov"><div><i></i>ID ' + esc(it.id) + copyBtn(it.id, "copy") + "</div><div><i></i>v" + esc(d.version ?? "–") + " · updated <span data-since=\"" + esc(d.updated_at || it.updated_at || "") + '">' +
        esc(since(d.updated_at || it.updated_at)) + "</span> ago · first ingested " + esc(fmtUtc(d.first_seen)) + "</div><div><i></i>" + (this.detail ? "Full record · /api/events" : "List item (loading full record)") + "</div></div>", { cls: "small" }) +
      "</div>";
  }

  _evidenceMeta() {
    const c = this.d.corroborations;
    if (!c) return "";
    if (isSensor(this.d)) return c.length ? c.length + " reading" + (c.length === 1 ? "" : "s") + " of the detecting sensor (not corroboration)" : "";
    return c.length ? c.length + " detection" + (c.length === 1 ? "" : "s") + " · score " + nf(this.d.corroboration_score, 2) : "";
  }
  // corroborations grouped by sensor pass, with the fire conflict score from /api/intel sensors
  _evidence() {
    const d = this.d, it = this.it;
    if (!this.detail) return '<div class="dz-empty">Loading record…</div>';
    const rows = d.corroborations || [];
    if (!rows.length) return '<div class="dz-empty">No sensor evidence linked' + (String(it.status) === "sensor-corroborated" ? " (status from an earlier pass)" : "") + "</div>";
    const c = this.cache.get(it.id), fires = new Map((((c && c.intel && c.intel.sensors) || {}).fires || { items: [] }).items.map((f) => [f.id, f]));
    const firstRep = (d.documents || []).map((x) => x.published_at).filter(Boolean).sort()[0];
    const groups = new Map();
    for (const r of rows) {
      const det = r.details || {};
      const k = r.kind === "fire" ? "fire|" + det.ts + "|" + det.satellite : r.kind + "|" + r.ref_id;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    this._evPts = [];
    let h = "";
    for (const [k, g] of groups) {
      g.sort((a, b) => (a.distance_km ?? 0) - (b.distance_km ?? 0));
      const det = g[0].details || {}, kind = g[0].kind;
      const dt = firstRep && det.ts ? (Date.parse(det.ts) - Date.parse(firstRep)) / 60000 : null;
      const gi = this._evPts.length;
      let title = SENSOR[kind] || kind;
      if (kind === "fire") title += " " + (det.satellite || "") + " scan · " + g.length + " hotspot" + (g.length === 1 ? "" : "s");
      else if (kind === "quake") title += " M" + nf(det.magnitude, 1) + " " + (det.event_type || "");
      else if (kind === "hazard") title += " " + [det.source, det.hazard, det.alert_level].filter(Boolean).join(" ");
      else if (kind === "gnss_cell") title += " " + g[0].ref_id + " · " + det.degraded + "/" + det.aircraft + " aircraft degraded (" + nf(det.pct, 0) + "%)" + (det.window_min ? " · " + det.window_min + "-min window" : "");
      else if (kind === "outage") title += " " + (det.datasource || "") + " · score " + nf(det.score);
      else if (kind === "radiation") title += " " + nf(det.value, 3) + " µSv/h";
      else title += " " + (det.name || det.callsign || g[0].ref_id);
      h += '<div class="ev-g" data-g="' + gi + '"><div class="ev-gh"><b>' + esc(title) + '</b><time title="' + esc(localHint(det.ts, null)) + '">' + esc(fmtUtc(det.ts)) + "</time>" +
        (dt != null ? "<em>" + esc(relMin(dt)) + " vs first report</em>" : "") + "</div>";
      h += '<table class="ev-t"><tbody>' + g.map((r) => {
        const x = r.details || {}, f = fires.get(r.ref_id), i = this._evPts.length;
        const b = x.lat != null && it.lat != null ? compass(bearing(it.lat, it.lon, x.lat, x.lon)) : "";
        this._evPts.push({ lat: x.lat, lon: x.lon, label: (SENSOR[r.kind] || r.kind) + " " + kmText(r.distance_km), g: gi });
        const cs = f && f.conflict_score != null ? '<span class="cs" style="--v:' + Math.min(100, f.conflict_score) + '" title="Fire conflict score ' + f.conflict_score + "/100: " + esc((f.conflict_reasons || []).join(", ")) + '">' + f.conflict_score + "</span>" : '<span class="cs na" title="No conflict score">–</span>';
        const val = r.kind === "fire" ? nf(x.frp_mw, x.frp_mw < 10 ? 1 : 0) + " MW" : r.kind === "quake" ? "M" + nf(x.magnitude, 1) : esc(x.alert_level || x.type_code || "");
        return '<tr data-p="' + i + '"' + (x.lat != null ? ' class="geo"' : "") + "><td>" + esc(kmText(r.distance_km)) + " " + b + "</td><td>" + val + "</td><td>" +
          esc(x.confidence || x.depth_km != null ? (x.confidence || "depth " + nf(x.depth_km, 1) + " km") : "") + "</td><td>" + cs + '</td><td class="sc" title="Corroboration score (rule ' + esc(r.rule) + ')">' + nf(r.score, 2) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    }
    return '<div class="ev-list"><div class="ev-hd"><span>DIST</span><span>VALUE</span><span>CONF</span><span title="Fire conflict score 0-100">CONFLICT</span><span title="Corroboration score">SCORE</span></div>' + h + "</div>";
  }

  _sources() {
    const d = this.d;
    const docs = (d.documents || []).slice().sort((a, b) => String(a.published_at).localeCompare(String(b.published_at)));
    if (!this.detail) return '<div class="dz-empty">Loading sources…</div>';
    if (!docs.length) return '<div class="dz-empty">' + (isSensor(d) ? "Sensor-derived" + (d.sensor ? " (" + esc(d.sensor.sensor) + ")" : "") + "; no news reports" : "No source documents") + "</div>";
    const first = docs[0].published_at;
    // repost groups: near-identical titles collapse under the first publication
    const groups = [];
    for (const x of docs) {
      const t = x.title_en || x.title;
      const g = groups.find((gr) => jacc(gr[0].title_en || gr[0].title, t) >= 0.7);
      if (g) g.push(x); else groups.push([x]);
    }
    const card = (x, lead) => {
      const lang = x.lang || x.language || "", t = x.title_en || x.title, orig = x.title_en && x.title && x.title_en !== x.title;
      const snip = x.snippet_en || (isEn(lang) ? x.snippet : "");
      const dt = first && x.published_at ? (Date.parse(x.published_at) - Date.parse(first)) / 60000 : null;
      const link = /^https?:\/\//i.test(x.url || "") ? '<a href="' + esc(x.url) + '" target="_blank" rel="noopener noreferrer" title="Open in the dossier browser (Ctrl+click: system browser)">' + esc(t || x.url) + "</a>" : "<span>" + esc(t || "(untitled)") + "</span>";
      return '<div class="src' + (lead ? "" : " rp") + (this.showOrig ? " show-orig" : "") + '"><div class="src-h"><i class="sk sk-' + esc(srcKind(x.source)) + '">' + esc(SRC_KIND[srcKind(x.source)] || srcKind(x.source)) + "</i><b>" + esc(srcName(x.source)) + "</b>" +
        (lang ? "<em>" + esc(lang) + "</em>" : "") + (x.role === "primary" ? '<em class="pri">primary</em>' : "") + '<time title="' + esc(localHint(x.published_at, null)) + '">' + esc(fmtUtc(x.published_at)) +
        (dt ? " · " + esc(relMin(dt)) : "") + "</time>" + (orig ? '<button type="button" class="ghost src-tr" data-a="srcorig" title="Toggle the source-language title">orig</button>' : "") + "</div>" +
        '<div class="src-t">' + link + (trPending(x) ? " " + TR_TAG : "") + "</div>" + (orig ? '<div class="src-o" lang="' + esc(lang) + '">' + esc(x.title) + "</div>" : "") +
        (snip && snip !== t ? '<div class="src-snip">' + esc(snip) + "</div>" : "") + "</div>";
    };
    return groups.map((g) => card(g[0], true) + (g.length > 1 ? '<details class="src-rp"><summary>+' + (g.length - 1) + " repost" + (g.length > 2 ? "s" : "") + " of the same text: " +
      g.slice(1).map((x) => esc(srcName(x.source)) + " " + esc(fmtUtc(x.published_at, { time: true }))).join(", ") + "</summary>" + g.slice(1).map((x) => card(x, false)).join("") + "</details>" : "")).join("");
  }

  // extracted fields: deduped (field+value across documents), without what the pane already
  // shows (category, publish times, place = location, title = event type); quotes only when
  // they add something
  _fields() {
    const d = this.d, fl = d.fields || [];
    if (!fl.length) return "";
    const titles = new Set((d.documents || []).flatMap((x) => [x.title, x.title_en]).filter(Boolean).map((s) => s.trim().toLowerCase()));
    const title = String(d.title || "").trim().toLowerCase();
    const groups = new Map();
    for (const f of fl) {
      const v = String(f.value || "").trim();
      if (!v || f.field === "time" || f.field.startsWith("location_before")) continue;
      if (f.field === "category" && v === d.category) continue;
      if (f.field === "location" && v.toLowerCase() === String(d.place_name || "").toLowerCase()) continue;
      if (f.field === "event_type" && v.toLowerCase() === title) continue;
      const k = f.field + "\u0000" + v.toLowerCase();
      if (!groups.has(k)) groups.set(k, { field: f.field, value: v, docs: new Set(), quotes: new Set() });
      const g = groups.get(k);
      g.docs.add(f.doc_id);
      const q = String(f.quote || "").trim();
      if (q && q.toLowerCase() !== v.toLowerCase() && !titles.has(q.toLowerCase())) g.quotes.add(q);
    }
    if (!groups.size) return "";
    const rows = [...groups.values()].map((g) => '<div class="fx"><span>' + esc(FIELD_LABEL[g.field] || g.field.replace(/_/g, " ")) + "</span><div><b>" + esc(g.value) + "</b>" +
      (g.docs.size > 1 ? '<em title="Stated in ' + g.docs.size + ' documents">×' + g.docs.size + "</em>" : "") +
      (g.quotes.size ? '<details><summary>quote</summary>' + [...g.quotes].map((q) => "<blockquote>" + esc(q) + "</blockquote>").join("") + "</details>" : "") + "</div></div>").join("");
    return sec("fields", "Extracted facts", '<div class="fxl">' + rows + "</div>", { meta: groups.size + " distinct" });
  }

  // ---------------------------------------------------------------- async sections
  _fillAsync() {
    const root = this.root, it = this.it;
    if (!root) return;
    const c = this.cache.get(it.id) || {};
    const S = c.intel;
    const put = (name, html, meta) => {
      const el = root.querySelector('[data-slot="' + name + '"]');
      if (el) el.innerHTML = html;
      const m = root.querySelector('[data-meta="' + name + '"]');
      if (m && meta != null) m.innerHTML = meta;
      return el;
    };
    if (!S) {
      if (c.err) ["assessment", "imagery", "timeline", "context"].forEach((n) => put(n, '<div class="dz-empty">Unavailable: ' + esc(c.err) + "</div>"));
      return;
    }
    // storyline membership ("Part of storyline: ..."), worker-derived
    const SL = S.storyline;
    put("storyline", SL && SL.member ? '<div class="dk-story" data-a="story" data-id="' + esc(SL.member.storyline_id) + '" title="Open the storyline: linked events, lanes timeline, globe playback">' +
      "<span>PART OF STORYLINE</span><b>" + esc(SL.member.title) + "</b><small>" + esc(SL.member.role.replace("_", "-").toUpperCase() + " · " + SL.member.n_events + " events · " + SL.member.theatre_label +
        " · " + fmtUtc(SL.member.t_first) + " → " + fmtUtc(SL.member.t_last)) + "</small></div>" : "");
    // assessment
    const A = S.assessment;
    put("assessment", A && A.items ? '<ul class="as">' + A.items.map((x) => '<li class="lv-' + (LEVEL[x.level] || ["", ""])[1] + '"><i>' + esc((LEVEL[x.level] || [x.level])[0]) + "</i><span>" + esc(x.text) +
      '</span><small>' + esc(x.basis) + "</small></li>").join("") + "</ul>" : '<div class="dz-empty">' + esc((A && A.error) || "No assessment") + "</div>");
    // imagery (evidence points overlaid)
    const pts = (this._evPts || []).filter((p) => p.lat != null);
    const im = put("imagery", imageryHtml(S.imagery, { lat: it.lat, lon: it.lon }, pts),
      S.imagery && S.imagery.watch ? esc({ acquired: "before + after", awaiting: "after pending", expired: "watch ended", failed: "failed", not_scheduled: "not queued" }[S.imagery.watch.state] || S.imagery.watch.state) : "");
    if (im) wireImagery(im);
    // timeline
    const tl = put("timeline", renderTimeline(S.timeline));
    if (tl) wireTimeline(tl, S.timeline, { onHover: (e) => this.h.highlight(e ? [{ lat: e.lat, lon: e.lon, label: e.label }] : null) });
    // evidence again: conflict scores arrived
    const ev = root.querySelector('[data-sec="evidence"] .dk-body');
    if (ev && this.detail) ev.innerHTML = this._evidence();
    // context
    const cx = put("context", this._context(S), esc(S.related && S.related.counts ? S.related.counts["7d"] + " nearby events / 7 d" : ""));
    if (cx) this.h.cameras(cx.querySelector(".cx-cams"));
  }

  _context(S) {
    const it = this.it, out = [];
    const as = S.assets || {};
    if (as.error) out.push('<div class="dz-empty">Assets unavailable: ' + esc(as.error) + "</div>");
    const fl = as.frontline;
    if (fl) out.push('<div class="cx-fl" data-fly="' + fl.lat + "," + fl.lon + '"><span>FRONT LINE</span><b>' + esc(kmText(fl.distance_km)) + " " + esc(fl.compass) + "</b><small>" + esc(fl.source) + (fl.source_datetime ? " · " + esc(fl.source_datetime) : "") + "</small></div>");
    const items = (as.items || []).slice(0, 8);
    if (items.length) out.push('<div class="cx-h">Nearest reference assets</div><ul class="cx-l">' + items.map((a) => '<li data-fly="' + a.lat + "," + a.lon + '" title="Fly there"><i class="at at-' + esc(a.type) + '">' + esc(ASSET[a.type] || a.type) + "</i><b>" + esc(a.name || "–") +
      "</b><span>" + esc(kmText(a.distance_km)) + " " + esc(a.compass || "") + "</span></li>").join("") + "</ul>");
    else if (!as.error) out.push('<div class="dz-empty">No bases, nuclear sites, airfields or ports within ' + nf(as.radius_km) + " km</div>");
    const R = S.related || {};
    if (R.nearest) {
      out.push('<div class="cx-h">Events nearby <small>' + nf(R.radius_km) + " km · 24 h " + R.counts["24h"] + " · 7 d " + R.counts["7d"] + " · 30 d " + R.counts["30d"] + "</small>" + bars(R.sparkline.days) + "</div>");
      out.push(R.nearest.length ? '<ul class="cx-l ev">' + R.nearest.slice(0, 6).map((e) => '<li data-ev="' + esc(e.id) + '" title="Select this event"><i class="at">' + esc(eventCat(e.category)[0].split(" ")[0]) + "</i><b>" + esc(e.title || e.id) + "</b><span>" + esc(kmText(e.distance_km)) +
        " · " + esc(since(e.t_end || e.t_start)) + "</span></li>").join("") + "</ul>" : '<div class="dz-empty">No other events within ' + nf(R.radius_km) + " km in 30 days</div>");
    }
    const H = S.history || {};
    if (H.total) out.push('<div class="cx-h">Historical (UCDP GED) <small>' + nf(H.radius_km) + " km</small></div><div class=\"cx-hist\">" + nf(H.total) + " events · " + nf(H.fatalities_best) + " fatalities (best est.) · " +
      Object.keys(H.by_year).slice(0, 1).concat(Object.keys(H.by_year).slice(-1)).filter((v, i, a) => a.indexOf(v) === i).join("–") + "</div>");
    out.push('<div class="cx-h">Cameras nearby</div><div class="cx-cams"></div>');
    return out.join("");
  }

  _tick() {
    if (!this.root) return;
    this.root.querySelectorAll("[data-since]").forEach((el) => { const v = since(el.dataset.since); if (el.textContent !== v) el.textContent = v; });
  }

  // ---------------------------------------------------------------- actions
  _wire() {
    const root = this.root;
    wireSections(root); wireCopy(root);
    root.addEventListener("click", (e) => {
      const b = e.target.closest("[data-a]");
      if (b) { e.preventDefault(); this._act(b.dataset.a, b); return; }
      const fly = e.target.closest("[data-fly]");
      if (fly) { const [la, lo] = fly.dataset.fly.split(",").map(Number); this.h.flyTo(la, lo, 6e4); return; }
      const ev = e.target.closest("[data-ev]");
      if (ev) { this.h.select("events", ev.dataset.ev); return; }
      const tr = e.target.closest(".ev-t tr.geo");
      if (tr) { const p = this._evPts[+tr.dataset.p]; if (p) this.h.flyTo(p.lat, p.lon, 2.5e4); }
    });
    // evidence hover -> globe highlight (row = one point, group header = the whole pass)
    root.addEventListener("mouseover", (e) => {
      const tr = e.target.closest(".ev-t tr.geo"), gh = e.target.closest(".ev-gh");
      const key = tr ? "p" + tr.dataset.p : gh ? "g" + gh.parentElement.dataset.g : null;
      if (key === this._hov) return;
      this._hov = key;
      if (!key) return;
      const pts = tr ? [this._evPts[+tr.dataset.p]] : this._evPts.filter((p) => p.g === +gh.parentElement.dataset.g);
      this.h.highlight(pts.filter((p) => p && p.lat != null).concat([{ lat: this.it.lat, lon: this.it.lon, label: "event", origin: true }]));
    });
    root.addEventListener("mouseleave", () => { this._hov = null; this.h.highlight(null); });
    root.addEventListener("mouseout", (e) => { if (!e.relatedTarget || !e.relatedTarget.closest || !e.relatedTarget.closest(".ev-t, .ev-gh, .tl-list")) { if (this._hov) { this._hov = null; this.h.highlight(null); } } });
  }

  async _act(a, btn) {
    const it = this.it, id = it.id;
    if (a === "orig") { this.showOrig = !this.showOrig; this.key = null; this.h.rerender(); return; }
    if (a === "srcorig") { btn.closest(".src").classList.toggle("show-orig"); return; }
    if (a === "fly") return this.h.flyTo(it.lat, it.lon, it.precision === "country" ? 3e6 : it.precision === "admin1" ? 1.2e6 : 4e5);
    if (a === "zoom") { const t = this._target() || it; return this.h.flyTo(t.lat, t.lon, 1.2e4); }
    if (a === "intel") return this.h.intel();
    if (a === "story") return this.h.story && this.h.story(btn.dataset.id, btn.querySelector("b") && btn.querySelector("b").textContent);
    if (a === "brief") { const ok = await copyText(this.brief()); return toast(btn.parentElement, ok ? "Brief copied (Markdown)" : "Copy failed", !ok); }
    if (a === "geojson" || a === "kml") {
      try {
        const blob = await fetchBlob("/api/events/" + encodeURIComponent(id) + "/export?format=" + a, 15000);
        download("event-" + id + "." + a, blob);
      } catch (e) { toast(btn.parentElement, "Export failed: " + (e.message || e), true); }
      return;
    }
    if (a === "record") return this._record();
  }

  _record() {
    const m = document.createElement("div");
    m.className = "dk-modal";
    const json = JSON.stringify(this.detail || this.it, null, 2);
    m.innerHTML = '<div class="dk-mbox glass"><header><b>RECORD // ' + esc(this.it.id) + '</b><button type="button" data-copy="' + esc(json) + '">Copy JSON</button><button type="button" class="ghost dk-mx">×</button></header><pre>' + esc(json) + "</pre></div>";
    document.body.appendChild(m);
    wireCopy(m);
    const close = () => { m.remove(); document.removeEventListener("keydown", onKey, true); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); } };
    document.addEventListener("keydown", onKey, true);
    m.addEventListener("click", (e) => { if (e.target === m || e.target.closest(".dk-mx")) close(); });
  }

  // plain-text / Markdown intel brief of what the pane shows
  brief() {
    const it = this.it, d = this.d, S = (this.cache.get(it.id) || {}).intel || {};
    const L = [];
    const title = d.title_en || d.title || it.title;
    L.push("## INTSUM — " + title);
    L.push("**Status:** " + (EVENT_STATUS[it.status] || it.status) + " · confidence " + nf(d.confidence ?? it.confidence, 2) + " · " + eventCat(it.category)[0] + (d.verdict ? " · verdict " + d.verdict : ""));
    const occ = (S.timeline || {}).occurred || {};
    const docs = (d.documents || []).slice().sort((a, b) => String(a.published_at).localeCompare(String(b.published_at)));
    L.push("**Occurred:** " + (occ.basis === "claims" ? bandText(occ.lo, occ.hi) + " (" + [occ.qualifier, occ.precision].filter(Boolean).join(", ") + ")" : occ.basis === "sensor" ? bandText(occ.lo, occ.hi) + " (sensor detection window" + ((occ.windows || []).length > 1 ? "s" : "") + ")" : occ.conflicts && occ.conflicts.length ? "sources disagree" : "not stated") +
      (isSensor(d) ? " · **Source:** sensor-derived" + (d.sensor ? " (" + d.sensor.sensor + ")" : "") + ", no news reports" : " · **First report:** " + (docs[0] ? fmtUtc(docs[0].published_at) : fmtUtc(it.t_start))));
    const m = mgrs(it.lat, it.lon), t = this._target();
    L.push("**Location:** " + [d.place_name, d.country].filter(Boolean).join(", ") + " — " + nf(it.lat, 5) + ", " + nf(it.lon, 5) + (m ? " (MGRS " + m + ")" : "") + " · precision " + it.precision +
      (t ? " · site " + nf(t.lat, 5) + ", " + nf(t.lon, 5) + " (" + t.basis + ", " + kmText(distKm(it.lat, it.lon, t.lat, t.lon)) + " " + compass(bearing(it.lat, it.lon, t.lat, t.lon)) + ")" : ""));
    const s = d.summary_en || d.summary;
    if (s) L.push("", s);
    if (S.assessment && S.assessment.items) { L.push("", "**Assessment**"); S.assessment.items.forEach((x) => L.push("- [" + (LEVEL[x.level] || [x.level])[0] + "] " + x.text + " _(" + x.basis + ")_")); }
    const tl = S.timeline;
    if (tl && tl.entries) {
      L.push("", "**Timeline (UTC)**");
      tl.entries.filter((e) => e.kind !== "milestone" || e.milestone === "status").forEach((e) => L.push("- " + (e.kind === "occurred" ? bandText(e.lo, e.hi) : fmtUtc(e.t)) + " " + e.kind.toUpperCase() + " " +
        (e.kind === "reported" ? e.source : e.kind === "occurred" ? "“" + e.phrase + "” (" + e.source + ")" : (e.label || "") + (e.detail ? ": " + e.detail : "")) + (e.t_rel_min != null ? " [" + relMin(e.t_rel_min) + "]" : "")));
    }
    const im = S.imagery;
    if (im && im.assets) {
      const by = Object.fromEntries(im.assets.map((a) => [a.role, a]));
      L.push("", "**Imagery:** " + ["before", "after"].map((r) => r + " " + (by[r] && by[r].status === "ok" ? fmtUtc(by[r].acquired_at).slice(0, 10) + " (" + by[r].collection + ", cloud " + nf(by[r].cloud_pct) + "%)" :
        by[r] ? by[r].status.replace("_", " ") + (by[r].error ? " — " + by[r].error : "") : "none")).join(" · ") + (im.attributions && im.attributions.length ? ". " + im.attributions.join("; ") : ""));
    }
    L.push("", "**Sources**");
    docs.forEach((x) => L.push("- " + x.source + " (" + (x.lang || x.language || "?") + ") " + fmtUtc(x.published_at) + " — " + (x.title_en || x.title) + (x.url ? " — " + x.url : "")));
    L.push("", "_Event " + it.id + " · generated " + fmtUtc(new Date().toISOString()) + " from NOCTUA records_");
    return L.join("\n");
  }
}
