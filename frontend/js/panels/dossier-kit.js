// Dossier kit: the shared building blocks of the ops-center inspector style (event dossier,
// intel pane, and any feature inspector that wants the same look). Pure helpers that return
// HTML strings, plus a few DOM utilities (copy, download, toast, collapsible sections).
// Styles live in css/dossier.css under the .dk- prefix.
import { esc, nf } from "/static/js/ui.js";

// ---- time ----
const tms = (iso) => (iso == null ? NaN : typeof iso === "number" ? iso : Date.parse(iso));
// "2026-11-23 01:18Z" (date omitted with {time:true})
export function fmtUtc(iso, { time = false, sec = false } = {}) {
  const t = tms(iso);
  if (isNaN(t)) return "–";
  const s = new Date(t).toISOString();
  return (time ? "" : s.slice(0, 10) + " ") + s.slice(11, sec ? 19 : 16) + "Z";
}
// hover hint: local wall-clock time at the site (offset from the backend, longitude based)
export function localHint(iso, offsetH) {
  const t = tms(iso);
  if (isNaN(t)) return "";
  const parts = [fmtUtc(iso, { sec: true }).replace("Z", " UTC")];
  if (offsetH != null) {
    const l = new Date(t + offsetH * 3600e3).toISOString();
    parts.push("≈ " + l.slice(0, 10) + " " + l.slice(11, 16) + " site local (UTC" + (offsetH >= 0 ? "+" : "") + offsetH + ", by longitude)");
  }
  parts.push(new Date(t).toLocaleString() + " your time");
  return parts.join("\n");
}
// "T−1 h 14 min" / "T+2.6 min" relative to an anchor (minutes)
export function relMin(min) {
  if (min == null || isNaN(min)) return "";
  const sgn = min < 0 ? "−" : "+", a = Math.abs(min);
  if (a < 10) return "T" + sgn + (Math.round(a * 10) / 10) + " min";
  if (a < 60) return "T" + sgn + Math.round(a) + " min";
  if (a < 2880) { const h = Math.floor(a / 60), m = Math.round(a % 60); return "T" + sgn + h + " h" + (m ? " " + m + " min" : ""); }
  return "T" + sgn + Math.round(a / 1440) + " d";
}
export function since(iso) {
  const t = tms(iso);
  if (isNaN(t)) return "–";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" : s < 172800 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d";
}
// a span "→" with the date shown once when both ends share it
export function bandText(lo, hi) {
  const a = fmtUtc(lo), b = fmtUtc(hi);
  return a.slice(0, 10) === b.slice(0, 10) ? a + " → " + b.slice(11) : a + " → " + b;
}

// ---- geo ----
const R = 6371.0088, rad = (d) => (d * Math.PI) / 180;
export function distKm(la1, lo1, la2, lo2) {
  const a = Math.sin(rad(la2 - la1) / 2) ** 2 + Math.cos(rad(la1)) * Math.cos(rad(la2)) * Math.sin(rad(lo2 - lo1) / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}
export function bearing(la1, lo1, la2, lo2) {
  const y = Math.sin(rad(lo2 - lo1)) * Math.cos(rad(la2));
  const x = Math.cos(rad(la1)) * Math.sin(rad(la2)) - Math.sin(rad(la1)) * Math.cos(rad(la2)) * Math.cos(rad(lo2 - lo1));
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}
export const compass = (deg) => (deg == null ? "" : ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.floor(((deg + 22.5) % 360) / 45)]);
export const kmText = (km) => (km == null ? "–" : km < 1 ? Math.round(km * 1000) + " m" : km < 10 ? nf(km, 1) + " km" : nf(km) + " km");

// WGS84 -> MGRS (1 m), e.g. "37T EN 12345 67890"; null outside 80S..84N
export function mgrs(lat, lon) {
  if (lat == null || lon == null || lat < -80 || lat > 84) return null;
  let zone = Math.floor((lon + 180) / 6) + 1;
  if (lat >= 56 && lat < 64 && lon >= 3 && lon < 12) zone = 32;
  if (lat >= 72) { if (lon >= 0 && lon < 9) zone = 31; else if (lon >= 9 && lon < 21) zone = 33; else if (lon >= 21 && lon < 33) zone = 35; else if (lon >= 33 && lon < 42) zone = 37; }
  const a = 6378137, f = 1 / 298.257223563, e2 = f * (2 - f), ep2 = e2 / (1 - e2), k0 = 0.9996;
  const phi = rad(lat), lam = rad(lon), lam0 = rad((zone - 1) * 6 - 180 + 3);
  const N = a / Math.sqrt(1 - e2 * Math.sin(phi) ** 2), T = Math.tan(phi) ** 2, C = ep2 * Math.cos(phi) ** 2, A = Math.cos(phi) * (lam - lam0);
  const e4 = e2 * e2, e6 = e4 * e2;
  const M = a * ((1 - e2 / 4 - (3 * e4) / 64 - (5 * e6) / 256) * phi - ((3 * e2) / 8 + (3 * e4) / 32 + (45 * e6) / 1024) * Math.sin(2 * phi) +
    ((15 * e4) / 256 + (45 * e6) / 1024) * Math.sin(4 * phi) - ((35 * e6) / 3072) * Math.sin(6 * phi));
  const E = k0 * N * (A + ((1 - T + C) * A ** 3) / 6 + ((5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5) / 120) + 500000;
  let No = k0 * (M + N * Math.tan(phi) * ((A * A) / 2 + ((5 - T + 9 * C + 4 * C * C) * A ** 4) / 24 + ((61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6) / 720));
  if (lat < 0) No += 10000000;
  const band = "CDEFGHJKLMNPQRSTUVWXX"[Math.floor((lat + 80) / 8)];
  const col = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"][(zone - 1) % 3][Math.floor(E / 100000) - 1];
  const row = "ABCDEFGHJKLMNPQRSTUV"[(Math.floor(No / 100000) + (zone % 2 === 0 ? 5 : 0)) % 20];
  const pad = (v) => String(Math.floor(v % 100000)).padStart(5, "0");
  return zone + band + " " + col + row + " " + pad(E) + " " + pad(No);
}
export const latLonText = (lat, lon, d = 5) => (lat == null ? "–" : Math.abs(lat).toFixed(d) + "°" + (lat >= 0 ? "N" : "S") + " " + Math.abs(lon).toFixed(d) + "°" + (lon >= 0 ? "E" : "W"));

// ---- markup ----
// collapsible section; `id` keys the remembered open/closed state
const COLLAPSE_KEY = "noctua.dk.collapsed";
let collapsed = {};
try { collapsed = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}") || {}; } catch (e) { collapsed = {}; }
export function sec(id, title, body, { meta = "", cls = "", tag = "" } = {}) {
  const shut = collapsed[id] ? " shut" : "";
  return '<section class="dk-sec ' + cls + shut + '" data-sec="' + esc(id) + '"><h3 class="dk-h" tabindex="0" role="button" aria-expanded="' + !shut + '">' +
    '<i class="dk-tw"></i><span>' + esc(title) + "</span>" + (tag ? '<em class="dk-tag">' + tag + "</em>" : "") + '<small>' + meta + "</small></h3>" +
    '<div class="dk-body">' + body + "</div></section>";
}
export function wireSections(root) {
  if (root._dkWired) return;
  root._dkWired = true;
  const flip = (h) => {
    const s = h.closest(".dk-sec");
    s.classList.toggle("shut");
    h.setAttribute("aria-expanded", String(!s.classList.contains("shut")));
    collapsed[s.dataset.sec] = s.classList.contains("shut") || undefined;
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed)); } catch (e) { /* private mode */ }
  };
  root.addEventListener("click", (e) => { const h = e.target.closest(".dk-h"); if (h && root.contains(h) && !e.target.closest("button,a")) flip(h); });
  root.addEventListener("keydown", (e) => { const h = e.target.closest && e.target.closest(".dk-h"); if (h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); flip(h); } });
}
export const chip = (text, cls = "", title = "") => '<span class="dk-chip ' + cls + '"' + (title ? ' title="' + esc(title) + '"' : "") + ">" + esc(text) + "</span>";
// semicircle confidence gauge 0..1
export function gauge(v, label = "CONF") {
  const x = Math.max(0, Math.min(1, +v || 0)), L = 50.27; // arc length of r=16 half circle
  const col = x >= 0.75 ? "var(--green)" : x >= 0.5 ? "var(--acc)" : x >= 0.3 ? "var(--amber)" : "var(--red)";
  return '<div class="dk-gauge" title="Confidence ' + x.toFixed(2) + '"><svg viewBox="0 0 40 24" aria-hidden="true"><path d="M4 22 A16 16 0 0 1 36 22" class="bg"/>' +
    '<path d="M4 22 A16 16 0 0 1 36 22" class="fg" style="stroke:' + col + ";stroke-dasharray:" + (x * L).toFixed(1) + " " + L + '"/></svg><b>' + x.toFixed(2) + "</b><span>" + esc(label) + "</span></div>";
}
// tiny daily-count bar sparkline
export function bars(days, { w = 120, h = 18 } = {}) {
  const max = Math.max(1, ...days), bw = w / days.length;
  return '<svg class="dk-bars" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true">' +
    days.map((d, i) => (d ? '<rect x="' + (i * bw + 0.3).toFixed(2) + '" y="' + (h - (d / max) * h).toFixed(2) + '" width="' + Math.max(0.6, bw - 0.6).toFixed(2) + '" height="' + ((d / max) * h).toFixed(2) + '"/>' : "")).join("") + "</svg>";
}
export const copyBtn = (text, label = "copy") => '<button type="button" class="dk-copy" data-copy="' + esc(text) + '" title="Copy ' + esc(text) + '">' + esc(label) + "</button>";

// ---- DOM utilities ----
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall back below */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
export function toast(anchor, msg, bad = false) {
  const t = document.createElement("div");
  t.className = "dk-toast" + (bad ? " bad" : "");
  t.textContent = msg;
  (anchor || document.body).appendChild(t);
  setTimeout(() => t.classList.add("out"), 1400);
  setTimeout(() => t.remove(), 1800);
}
// [data-copy] buttons anywhere under root copy their payload
export function wireCopy(root) {
  if (root._dkCopy) return;
  root._dkCopy = true;
  root.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b || !root.contains(b)) return;
    e.preventDefault(); e.stopPropagation();
    const ok = await copyText(b.dataset.copy);
    toast(b.closest(".dk-sec, .dk-head, .dk-root") || root, ok ? "Copied" : "Copy failed", !ok);
  });
}
export function download(name, blob) {
  const u = URL.createObjectURL(blob), a = document.createElement("a");
  a.href = u; a.download = name; a.dataset.ext = "1"; a.style.display = "none";
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(u); a.remove(); }, 2000);
}
