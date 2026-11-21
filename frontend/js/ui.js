// DOM helpers: formatting, key/value grids, details content.
export const $ = (id) => document.getElementById(id);
const ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ENT[c]);
export const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
export const nf = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));

// ---- English display of events / sources (backend adds title_en / summary_en / snippet_en / lang;
// title_en is null while a translation is pending; older backends have none of these fields) ----
export const isEn = (lang) => /^en\b/i.test(String(lang || ""));
export const evTitle = (it) => (it && (it.title_en || it.title)) || "";
// original shown because the English text has not arrived yet
export const trPending = (it) => !!(it && it.title && Object.hasOwn(it, "title_en") && it.title_en == null && !isEn(it.lang));
export const TR_TAG = '<span class="tr-pend" title="English translation pending; showing the original">translating…</span>';
// true when the record carries a translation that differs from its source-language text
export const hasOriginal = (it) => !!(it && ((it.title_en && it.title && it.title_en !== it.title) || (it.summary_en && it.summary && it.summary_en !== it.summary)));

export function ago(ts) {
  if (ts == null) return "–";
  const t = typeof ts === "number" ? (ts < 1e11 ? ts * 1000 : ts) : Date.parse(ts);
  if (isNaN(t)) return esc(ts);
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  const rel = s < 60 ? s + " s ago" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : Math.floor(s / 86400) + " d ago";
  return rel + " (" + new Date(t).toISOString().replace("T", " ").slice(0, 19) + " UTC)";
}

export const kv = (rows) => '<div class="kv">' + rows.filter((r) => r[1] != null && r[1] !== "").map((r) => "<span>" + esc(r[0]) + "</span><div>" + r[1] + "</div>").join("") + "</div>";
const coords = (it) => nf(it.lat, 4) + ", " + nf(it.lon, 4);
const extras = (ex, skip) => Object.entries(ex || {}).filter(([k]) => !skip.includes(k)).map(([k, v]) => [k, esc(typeof v === "object" ? JSON.stringify(v) : v)]);
const unit = (v, u, d = 0) => (v == null ? "–" : nf(v, d) + " " + u);

// Returns [title, bodyHtml]
export function detailsHtml(kind, it) {
  const ex = it.extra || {};
  if (kind === "aircraft") {
    const flags = [it.high_priority ? "HIGH PRIORITY" : "", it.is_mil ? "MILITARY" : ""].filter(Boolean).join(" · ");
    return [it.name || it.id, kv([["Callsign", esc(it.name)], ["ID / hex", esc(it.id)], ["Registration", esc(it.reg)], ["Type", esc(it.type_code)], ["Description", esc(ex.desc)],
      ["Operator", esc(it.operator)], ["Category", esc(it.category)], ["Flags", esc(flags)],
      ["Altitude", it.alt_ft == null ? "–" : nf(it.alt_ft) + " ft (" + nf(it.alt_ft * 0.3048) + " m)"],
      ["Speed", unit(it.speed_kts, "kts")], ["Heading", it.heading_deg == null ? "–" : nf(it.heading_deg) + "°"],
      ["Squawk", esc(ex.squawk)], ["Position", coords(it)], ["Last seen", ago(it.ts)], ["Source", esc(it.source)], ...extras(ex, ["squawk", "desc"])])];
  }
  if (kind === "ships") {
    return [it.name || it.id, kv([["Name", esc(it.name)], ["ID / MMSI", esc(it.id)], ["Type", esc(it.type_code)], ["Operator", esc(it.operator)], ["Category", esc(it.category)],
      ["Military", it.is_mil ? "Yes" : "No"], ["Speed", unit(it.speed_kts, "kts", 1)], ["Heading", it.heading_deg == null ? "–" : nf(it.heading_deg) + "°"],
      ["Position", coords(it)], ["Last seen", ago(it.ts)], ["Source", esc(it.source)], ...extras(ex, [])])];
  }
  if (kind === "fires") {
    return ["Fire detection", kv([["ID", esc(it.id)], ["FRP", unit(it.frp, "MW", 1)], ["Confidence", esc(it.confidence)], ["Satellite", esc(it.satellite)],
      ["Day / night", it.daynight === "D" ? "Day" : it.daynight === "N" ? "Night" : esc(it.daynight)], ["Position", coords(it)], ["Detected", ago(it.ts)]])];
  }
  const url = /^https?:\/\//.test(it.url || "") ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">USGS event page</a>' : "";
  return ["M " + nf(it.mag, 1) + " · " + (it.place || "Earthquake"), kv([["Magnitude", nf(it.mag, 1)], ["Place", esc(it.place)], ["Depth", unit(it.depth_km, "km", 1)],
    ["Type", esc(it.event_type)], ["Position", coords(it)], ["Time", ago(it.ts)], ["Link", url], ["ID", esc(it.id)]])];
}
