// Time display: the single source of truth for showing times in the UI.
//
// The viewer picks a display zone (Settings, gear in the command bar): "local" (default, the
// browser's own zone), "UTC", or any IANA zone ("Europe/London"). Every absolute time in the
// UI goes through these helpers; relative times ("3m ago") stay relative. Keep the exact UTC
// instant reachable on hover: put utcTitle(ts) in a title attribute (timeHtml() does this).
//
// API (stable; other modules import these names):
//   getTz()                 -> "local" | "UTC" | IANA zone   the stored preference
//   setTz(tz)               store the preference ("local" | "UTC" | IANA); notifies listeners
//   onTzChange(cb)          cb(tz) after every change; returns an unsubscribe function
//   resolvedTz(tz?)         the IANA zone actually used ("local" -> the browser's zone)
//   localTz()               the browser's IANA zone
//   tzLabel(at?, tz?)       "IST +05:30", "GMT", "UTC" (offset at instant `at`, default now)
//   tzOffset(at?, tz?)      offset in minutes east of UTC at that instant
//   fmtTime(ts, opts)       "14:03" ({ seconds: true } -> "14:03:22")
//   fmtDate(ts, opts)       "2026-11-23"
//   fmtDateTime(ts, opts)   "2026-11-23 14:03" ({ seconds, zone: true -> + " IST", short: true -> "11-23 14:03" })
//   utcTitle(ts)            "2026-11-23 08:33:22 UTC" (exact instant, for title attributes)
//   timeHtml(ts, opts)      '<time datetime=… title="… UTC">fmtDateTime(ts)</time>'
//   toMs(ts)                ms epoch from an ISO string (zone-less = UTC) / ms / epoch seconds / Date; NaN if invalid
//   listZones()             IANA zones (Intl.supportedValuesOf, with a short fallback list)
// Every formatter accepts opts.tz to override the preference for one call (e.g. "UTC").
// Invalid / missing timestamps format as "–".

const KEY = "noctua.tz";
const listeners = new Set();
const cache = new Map();
let pref = null;

const FALLBACK_ZONES = ["UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Kyiv", "Europe/Moscow", "Europe/Istanbul",
  "Asia/Dubai", "Asia/Tehran", "Asia/Karachi", "Asia/Kolkata", "Asia/Dhaka", "Asia/Bangkok", "Asia/Singapore", "Asia/Shanghai",
  "Asia/Taipei", "Asia/Seoul", "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland", "Pacific/Honolulu", "America/Anchorage",
  "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York", "America/Sao_Paulo", "Africa/Lagos",
  "Africa/Cairo", "Africa/Johannesburg", "Africa/Nairobi"];

export function localTz() {
  try { return new Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) { return "UTC"; }
}
const valid = (tz) => {
  if (tz === "local" || tz === "UTC") return true;
  try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); return true; } catch (e) { return false; }
};

export function getTz() {
  if (pref == null) {
    let v = null;
    try { v = typeof localStorage !== "undefined" ? localStorage.getItem(KEY) : null; } catch (e) { v = null; }
    pref = v && valid(v) ? v : "local";
  }
  return pref;
}
export function setTz(tz) {
  const v = tz && valid(tz) ? tz : "local";
  pref = v;
  try { if (typeof localStorage !== "undefined") localStorage.setItem(KEY, v); } catch (e) { /* storage disabled */ }
  for (const cb of [...listeners]) { try { cb(v); } catch (e) { console.warn("tz listener", e); } }
}
export function onTzChange(cb) { listeners.add(cb); return () => listeners.delete(cb); }
export const resolvedTz = (tz) => { const t = tz || getTz(); return t === "local" ? localTz() : t; };

export function toMs(ts) {
  if (ts == null || ts === "") return NaN;
  if (ts instanceof Date) return ts.getTime();
  if (typeof ts === "number") return ts < 1e11 ? ts * 1000 : ts;
  const s = String(ts);
  // ISO date-times without a zone are UTC (the backend's convention), not browser-local
  return Date.parse(/T\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(s) ? s + "Z" : s);
}

function fmt(tz) {
  let f = cache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    cache.set(tz, f);
  }
  return f;
}
function parts(ms, tz) {
  const o = {};
  for (const p of fmt(tz).formatToParts(ms)) o[p.type] = p.value;
  if (o.hour === "24") o.hour = "00";
  return o;
}

export function tzOffset(at, tz) {
  const z = resolvedTz(tz), ms = at == null ? Date.now() : toMs(at);
  if (z === "UTC" || isNaN(ms)) return 0;
  const p = parts(ms, z);
  const wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return Math.round((wall - Math.floor(ms / 1000) * 1000) / 60000);
}
const offStr = (m) => (m >= 0 ? "+" : "−") + String(Math.floor(Math.abs(m) / 60)).padStart(2, "0") + ":" + String(Math.abs(m) % 60).padStart(2, "0");

function abbr(ms, z) {
  const langs = [];
  try { if (typeof navigator !== "undefined" && navigator.language) langs.push(navigator.language); } catch (e) { /* no navigator */ }
  langs.push("en-US", "en-GB", "en-IN");
  for (const l of langs) {
    const k = "abbr|" + l + "|" + z;
    let f = cache.get(k);
    if (!f) { try { f = new Intl.DateTimeFormat(l, { timeZone: z, timeZoneName: "short" }); } catch (e) { continue; } cache.set(k, f); }
    const n = (f.formatToParts(ms).find((p) => p.type === "timeZoneName") || {}).value || "";
    if (/^[A-Z]{2,5}$/.test(n) && n !== "GMT" || (n === "GMT" && tzOffset(ms, z) === 0)) return n;
  }
  return z.split("/").pop().replace(/_/g, " ");
}
export function tzLabel(at, tz) {
  const z = resolvedTz(tz);
  if (z === "UTC" || z === "Etc/UTC") return "UTC";
  const ms = at == null ? Date.now() : toMs(at);
  const m = tzOffset(ms, z);
  return abbr(ms, z) + " " + offStr(m);
}

export function fmtTime(ts, opts = {}) {
  const ms = toMs(ts);
  if (isNaN(ms)) return "–";
  const p = parts(ms, resolvedTz(opts.tz));
  return p.hour + ":" + p.minute + (opts.seconds ? ":" + p.second : "");
}
export function fmtDate(ts, opts = {}) {
  const ms = toMs(ts);
  if (isNaN(ms)) return "–";
  const p = parts(ms, resolvedTz(opts.tz));
  return opts.short ? p.month + "-" + p.day : p.year + "-" + p.month + "-" + p.day;
}
export function fmtDateTime(ts, opts = {}) {
  const ms = toMs(ts);
  if (isNaN(ms)) return "–";
  const z = resolvedTz(opts.tz), p = parts(ms, z);
  const d = opts.short ? p.month + "-" + p.day : p.year + "-" + p.month + "-" + p.day;
  let s = d + " " + p.hour + ":" + p.minute + (opts.seconds ? ":" + p.second : "");
  if (opts.zone) s += " " + tzLabel(ms, z).split(" ")[0];
  return s;
}
export function utcTitle(ts) {
  const ms = toMs(ts);
  return isNaN(ms) ? "" : new Date(ms).toISOString().replace("T", " ").slice(0, 19) + " UTC";
}
const ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const e = (s) => String(s).replace(/[&<>"']/g, (c) => ENT[c]);
export function timeHtml(ts, opts = {}) {
  const ms = toMs(ts);
  if (isNaN(ms)) return "–";
  const txt = opts.time ? fmtTime(ms, opts) : fmtDateTime(ms, opts);
  return '<time datetime="' + new Date(ms).toISOString() + '" title="' + e(utcTitle(ms)) + '">' + e(txt) + "</time>";
}
export function listZones() {
  try { if (typeof Intl.supportedValuesOf === "function") { const z = Intl.supportedValuesOf("timeZone"); if (z && z.length) return z.includes("UTC") ? z : ["UTC", ...z]; } } catch (e2) { /* old engine */ }
  return FALLBACK_ZONES.slice();
}
