// Glyph system: outline-wireframe map symbols for aircraft (by role) and ships (by type).
// All glyphs are white strokes over a dark halo so Cesium's billboard colour tints them.

const FIGHTER = new Set(["F15", "F16", "F18H", "F18S", "F35", "F22", "F5", "F86", "EUFI", "HAWK", "A10", "B1", "B2", "B52", "SB39", "SA37", "TOR", "AJET", "M339", "M346", "RFAL", "MIR2", "SU", "SU27", "SU30", "SU34", "SU35", "MG29", "MG31", "J10", "J11", "J20", "JF17", "F14", "AV8B", "HARR"]);
const TANKER = new Set(["K35R", "K35E", "KC10", "KC46", "KC2", "KC39", "A332", "A33M", "MRTT", "IL78", "K135", "DC10"]);
const TRANSPORT = new Set(["C17", "C5", "C5M", "C130", "C30J", "C2", "C27J", "C295", "C160", "A400", "CN35", "Y20", "IL76", "AN124", "A124", "AN12", "AN22", "AN26", "AN72", "C212", "DHC6", "CASA", "B748", "B77W", "B77L", "B752", "B737", "A321", "A342", "E135"]);
const ISR = new Set(["E3CF", "E3TF", "E6", "E737", "E7", "R135", "RC35", "P8", "P3", "E2", "U2", "ATLA", "E8", "GLF5", "GLEX", "CL60", "BE20", "B350", "DH8B", "DH8C", "E390", "F900", "LJ45", "E550", "PC12", "D228", "F406"]);
const UAV = new Set(["Q9", "Q4", "Q1", "MQ9", "RQ4", "MQ4", "HRON", "TB2", "UAV", "JPRO"]);
const HELO = new Set(["H60", "H47", "H53", "H53S", "UH1", "UH1Y", "V22", "NH90", "EH10", "LYNX", "PUMA", "TIGR", "H64", "MI8", "MI17", "MI24", "MI28", "KA52", "A139", "A169", "A119", "AS65", "AS32", "B412", "B06", "B429", "EC35", "EC45", "EC55", "EC25", "EC30", "H135", "H145", "H160", "H175", "S92", "S76", "R44", "AS50"]);
const TRAINER = new Set(["T38", "T6", "TEX2", "PC21", "PC7", "PC9", "KT1", "CT4", "G120", "TB30", "G115", "T7", "M326", "L39", "SR20", "C172", "P28A", "GA6C"]);

export const ROLE_LABEL = { fighter: "Fighter / strike", tanker: "Tanker", transport: "Transport", isr: "ISR / patrol", uav: "UAV", helo: "Rotary wing", trainer: "Trainer / light", generic: "Fixed wing" };

export function aircraftRole(it) {
  const t = String((it && it.type_code) || "").toUpperCase();
  const cat = String((it && it.category) || "").toLowerCase();
  if (UAV.has(t) || /uav|drone/.test(cat)) return "uav";
  if (HELO.has(t) || /helo|heli|rotor/.test(cat)) return "helo";
  if (TANKER.has(t) || /tanker/.test(cat) && !TRANSPORT.has(t)) return "tanker";
  if (FIGHTER.has(t) || t.startsWith("SU") || t.startsWith("MIG") || /fighter|attack|strike/.test(cat)) return "fighter";
  if (ISR.has(t) || /isr|surveil|recon|awacs|patrol/.test(cat)) return "isr";
  if (TRANSPORT.has(t) || /transport/.test(cat)) return "transport";
  if (TRAINER.has(t) || /trainer|aerobatic/.test(cat)) return "trainer";
  return "generic";
}

const wrap = (body, size = 32) => "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size + '" viewBox="0 0 32 32" stroke-linejoin="round" stroke-linecap="round">' +
  '<g fill="none" stroke="#000" stroke-opacity=".6" stroke-width="3.6">' + body + "</g>" +
  '<g fill="#fff" fill-opacity=".22" stroke="#fff" stroke-width="1.5">' + body + "</g></svg>");

// plain variant (no dark halo) for CSS masks in the UI
const plain = (body) => "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" stroke-linejoin="round" stroke-linecap="round"><g fill="#fff" fill-opacity=".25" stroke="#fff" stroke-width="1.6">' + body + "</g></svg>");
const P = {
  generic: '<path d="M16 2.5c1 0 1.6 1 1.6 2.4V12l10.4 6.4v2.4l-10.4-3v6.4l3 2.4V29l-4.6-1.2L11.4 29v-2.4l3-2.4v-6.4l-10.4 3v-2.4L14.4 12V4.9c0-1.4.6-2.4 1.6-2.4z"/>',
  fighter: '<path d="M16 2l2.2 9 9.3 10v2.6l-9.4-3.4-.6 4.8 3.2 3v1.8L16 28.6l-4.7 1.2V28l3.2-3-.6-4.8-9.4 3.4V21l9.3-10z"/>',
  transport: '<path d="M16 3c1.2 0 1.8 1.2 1.8 2.6v5.6L30 15.6v2.6l-12.2-2.6v8l3.6 2.4v2.2l-5.4-1.2-5.4 1.2v-2.2l3.6-2.4v-8L2 18.2v-2.6l12.2-4.4V5.6C14.2 4.2 14.8 3 16 3z"/>',
  tanker: '<path d="M16 3c1.2 0 1.8 1.2 1.8 2.6v5.6L30 15.6v2.6l-12.2-2.6v8l3.6 2.4v2.2l-5.4-1.2-5.4 1.2v-2.2l3.6-2.4v-8L2 18.2v-2.6l12.2-4.4V5.6C14.2 4.2 14.8 3 16 3z"/><path d="M16 27.6v3.4M13.6 31h4.8"/>',
  isr: '<path d="M16 2.5c1 0 1.6 1 1.6 2.4V12l10.4 6.4v2.4l-10.4-3v6.4l3 2.4V29l-4.6-1.2L11.4 29v-2.4l3-2.4v-6.4l-10.4 3v-2.4L14.4 12V4.9c0-1.4.6-2.4 1.6-2.4z"/><ellipse cx="16" cy="15.5" rx="6.2" ry="2.4"/>',
  uav: '<path d="M16 6l1 6.2 12.5 1.3v2l-12.5.6-.3 7.6 3.3 2v1.6l-4-.6-4 .6v-1.6l3.3-2-.3-7.6-12.5-.6v-2L15 12.2z"/>',
  helo: '<circle cx="16" cy="14" r="4.6"/><path d="M4.5 4.5l23 19M27.5 4.5l-23 19"/><path d="M16 18.6V29M12.8 29h6.4"/>',
  trainer: '<path d="M16 4c.9 0 1.4.9 1.4 2v6l9.6 1.6v2.6l-9.6-.6-.4 7.4 3 1.8V27l-4-.8-4 .8v-2.2l3-1.8-.4-7.4-9.6.6v-2.6l9.6-1.6V6c0-1.1.5-2 1.4-2z"/>',
};
const AIR_GLYPHS = Object.fromEntries(Object.entries(P).map(([k, v]) => [k, wrap(v)]));
export const aircraftGlyph = (role) => AIR_GLYPHS[role] || AIR_GLYPHS.generic;
const UI = {};
for (const [k, v] of Object.entries(P)) UI["a:" + k] = plain(v);

// ---- ships ----
export const SHIP_LABEL = { mil: "Military", cargo: "Cargo", tanker: "Tanker", passenger: "Passenger", fishing: "Fishing", service: "Tug / service", pleasure: "Sailing / pleasure", other: "Vessel" };
export function shipClass(it) {
  if (it && it.is_mil) return "mil";
  const raw = String((it && (it.type_code ?? it.category)) || "").toLowerCase();
  const n = parseInt(raw, 10);
  if (!isNaN(n) && String(n) === raw.trim()) {
    if (n === 35) return "mil";
    if (n === 30) return "fishing";
    if (n === 36 || n === 37) return "pleasure";
    if (n >= 50 && n <= 59 || n === 31 || n === 32 || n === 33) return "service";
    if (n >= 60 && n <= 69) return "passenger";
    if (n >= 70 && n <= 79) return "cargo";
    if (n >= 80 && n <= 89) return "tanker";
    return "other";
  }
  if (/milit|naval|warship|law/.test(raw)) return "mil";
  if (/tank/.test(raw)) return "tanker";
  if (/cargo|container|bulk|carrier/.test(raw)) return "cargo";
  if (/passenger|ferry|cruise/.test(raw)) return "passenger";
  if (/fish/.test(raw)) return "fishing";
  if (/tug|pilot|sar|dredg|service|tow/.test(raw)) return "service";
  if (/sail|pleasure|yacht/.test(raw)) return "pleasure";
  return "other";
}
const HULL = '<path d="M16 3.5l5.6 8.5v16.5H10.4V12z"/>';
const S = {
  other: HULL,
  cargo: HULL + '<path d="M12.8 14h6.4v4h-6.4zM12.8 20h6.4v4h-6.4z"/>',
  tanker: HULL + '<circle cx="16" cy="15.5" r="2.2"/><circle cx="16" cy="22.5" r="2.2"/>',
  passenger: HULL + '<path d="M12.8 15h6.4M12.8 19h6.4M12.8 23h6.4"/>',
  fishing: HULL + '<path d="M16 13v8.5a2.4 2.4 0 0 1-4.6 1"/>',
  service: '<path d="M16 6l5.4 6v14H10.6V12z"/><circle cx="16" cy="18" r="2.6"/>',
  pleasure: '<path d="M16 6l4.6 9.5L16 28l-4.6-12.5z"/><path d="M16 9v12"/>',
  mil: '<path d="M16 2.5l6 9.5v17H10V12z"/><path d="M16 9l3 4.5-3 4.5-3-4.5z"/><path d="M13 23.5h6"/>',
};
const SHIP_GLYPHS = Object.fromEntries(Object.entries(S).map(([k, v]) => [k, wrap(v)]));
export const shipGlyph = (cls) => SHIP_GLYPHS[cls] || SHIP_GLYPHS.other;
for (const [k, v] of Object.entries(S)) UI["s:" + k] = plain(v);

// ---- other glyphs (inspector + list) ----
const FIRE_P = '<path d="M16 3.5c1.5 4.5 7.5 8 7.5 14.5a7.5 7.5 0 0 1-15 0c0-3.6 2.2-5.6 3.6-7.6.4 2.4 1.4 3.6 2.6 4.2C14.6 11 15 7 16 3.5z"/>';
const QUAKE_P = '<circle cx="16" cy="16" r="3"/><circle cx="16" cy="16" r="8" fill="none"/><circle cx="16" cy="16" r="13" fill="none" stroke-dasharray="2 3"/>';
const REF_P = '<path d="M16 3l13 13-13 13L3 16z"/><circle cx="16" cy="16" r="2.4"/>';
export const FIRE_GLYPH = wrap(FIRE_P), QUAKE_GLYPH = wrap(QUAKE_P), REF_GLYPH = wrap(REF_P);
UI.fire = plain(FIRE_P); UI.quake = plain(QUAKE_P); UI.ref = plain(REF_P);

// ---- news events: one outline glyph per category ----
export const EVENT_CATS = {
  armed_conflict: ["Armed conflict", "#ff4b55", '<path d="M16 3l2.6 7 6.6-3.4-3.4 6.6 7.2 2.6-7.2 2.6 3.4 6.6-6.6-3.4L16 29l-2.6-7-6.6 3.4 3.4-6.6L3 16l7.2-2.6-3.4-6.6 6.6 3.4z"/>'],
  air_activity: ["Air activity", "#9ff3ff", '<path d="M16 3l9 25-9-5.5L7 28z"/>'],
  maritime_security: ["Maritime security", "#7fa8ff", '<circle cx="16" cy="7" r="2.8"/><path d="M16 10v18M11 14h10M6 18c1 5 5 9 10 9s9-4 10-9"/>'],
  civil_unrest: ["Civil unrest", "#ffb020", '<circle cx="16" cy="9" r="3"/><circle cx="7.5" cy="12" r="2.4"/><circle cx="24.5" cy="12" r="2.4"/><path d="M10.5 26v-6a5.5 5.5 0 0 1 11 0v6zM3 25v-4.5a4 4 0 0 1 5-3.8M29 25v-4.5a4 4 0 0 0-5-3.8"/>'],
  disaster: ["Disaster", "#ff6a1a", '<path d="M16 4L29 27H3z"/><path d="M16 12v8M16 23.2v.6"/>'],
  infrastructure: ["Infrastructure", "#d9b27c", '<path d="M5 28V13l11-7 11 7v15z"/><path d="M12 28v-8h8v8"/>'],
  political: ["Political", "#a99bff", '<path d="M8 4v25M8 6h16l-4 5 4 5H8"/>'],
  economic: ["Economic", "#4fdf8c", '<circle cx="16" cy="16" r="12"/><path d="M16 8.5v15M19.6 12c-1-1-2.3-1.5-3.6-1.5-2 0-3.5 1-3.5 2.6 0 3.6 7.2 1.8 7.2 5.2 0 1.6-1.6 2.6-3.7 2.6-1.4 0-2.7-.6-3.7-1.6"/>'],
  cyber_outage: ["Cyber / outage", "#e86bff", '<path d="M18.5 3L7 18h8l-2 11 12-16h-8z"/>'],
};
const EVENT_FALLBACK = ["Event", "#a99bff", '<circle cx="16" cy="16" r="8"/>'];
export const eventCat = (c) => EVENT_CATS[c] || EVENT_FALLBACK;
// disaster events from hazard alerts (GDACS / EONET / ReliefWeb): subcategory = hazard type
export const HAZARDS = {
  earthquake: ["Earthquake", QUAKE_P],
  tropical_cyclone: ["Tropical cyclone", '<circle cx="16" cy="16" r="3.4"/><path fill="none" d="M16 4.5c-5.6 0-9.8 3.6-10.6 8.6 2.2-3 5.4-4.2 8.6-3.8M16 27.5c5.6 0 9.8-3.6 10.6-8.6-2.2 3-5.4 4.2-8.6 3.8"/>'],
  flood: ["Flood", '<path fill="none" d="M3.5 11c3-3 5.5-3 8.3 0s5.5 3 8.3 0 5.5-3 8.4 0M3.5 18c3-3 5.5-3 8.3 0s5.5 3 8.3 0 5.5-3 8.4 0M3.5 25c3-3 5.5-3 8.3 0s5.5 3 8.3 0 5.5-3 8.4 0"/>'],
  volcano: ["Volcano", '<path d="M3.5 28l8-13h9l8 13z"/><path fill="none" d="M14 11l-2.4-5.5M18 11l2.4-5.5M16 10V3.5"/>'],
  drought: ["Drought", '<circle cx="16" cy="12" r="5"/><path fill="none" d="M16 3v2.2M7 12H4.8M27.2 12H25M9.6 5.6l1.6 1.6M22.4 5.6l-1.6 1.6M4 23.5h24M10 23.5l2.2 4M20.5 23.5l-1.4 4"/>'],
  wildfire: ["Wildfire", FIRE_P],
};
const innerGlyph = (cat, sub) => (cat === "disaster" && HAZARDS[sub] ? HAZARDS[sub][1] : eventCat(cat)[2]);
export const EVENT_STATUS = {
  "reported": "Reported", "multi-source": "Multi-source", "sensor-corroborated": "Sensor-corroborated", "official": "Official",
};
// map glyph: white strokes (tinted by billboard colour) inside a status ring; country-level = hollow
const ringSvg = (cat, status, hollow, sub) => {
  const inner = '<g transform="translate(20 20) scale(.5) translate(-16 -16)" fill="' + (hollow ? "none" : "#fff") + '" fill-opacity=".25" stroke="#fff" stroke-width="3" stroke-linejoin="round" stroke-linecap="round">' + innerGlyph(cat, sub) + "</g>";
  const halo = '<g fill="none" stroke="#000" stroke-opacity=".55" stroke-width="3.4">';
  const dbl = status === "sensor-corroborated" || status === "official";
  const dash = status === "reported" ? ' stroke-dasharray="2.4 3"' : "";
  const w = status === "reported" ? 1.1 : 1.7;
  const rings = '<circle cx="20" cy="20" r="18"/>' + (dbl ? '<circle cx="20" cy="20" r="15"/>' : "");
  return "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40">' +
    halo + rings + "</g>" + '<g fill="none" stroke="#fff" stroke-width="' + w + '"' + dash + ">" + rings + "</g>" + inner + "</svg>");
};
const EVENT_IMG = new Map();
export function eventGlyph(cat, status, hollow, sub) {
  const hz = cat === "disaster" && HAZARDS[sub] ? sub : "";
  const k = cat + "|" + status + "|" + (hollow ? 1 : 0) + "|" + hz;
  let v = EVENT_IMG.get(k);
  if (!v) { v = ringSvg(cat, status, hollow, hz); EVENT_IMG.set(k, v); }
  return v;
}
for (const [k, v] of Object.entries(EVENT_CATS)) UI["e:" + k] = plain(v[2]);
UI["e:"] = plain(EVENT_FALLBACK[2]);
for (const [k, v] of Object.entries(HAZARDS)) UI["h:" + k] = plain(v[1]);

// palette (CSS) for map + legend
export const COLORS = { air: "#9ff3ff", airCiv: "#c8d4de", hp: "#ff4b55", ship: "#7fa8ff", shipMil: "#ff4b55", fire: "#ff6a1a", quake: "#ffd24a" };
export function threatColor(kind, it) {
  if (kind === "aircraft") return it && it.high_priority ? COLORS.hp : it && it.is_mil ? COLORS.air : COLORS.airCiv;
  if (kind === "ships") return it && it.is_mil ? COLORS.shipMil : COLORS.ship;
  if (kind === "fires") return COLORS.fire;
  if (kind === "quakes") return (it && it.mag >= 5) ? COLORS.hp : COLORS.quake;
  if (kind === "events") return eventCat(it && it.category)[1];
  return "#3ddcc2";
}
export function glyphFor(kind, it) {
  if (kind === "aircraft") return aircraftGlyph(aircraftRole(it));
  if (kind === "ships") return shipGlyph(shipClass(it));
  if (kind === "fires") return FIRE_GLYPH;
  if (kind === "quakes") return QUAKE_GLYPH;
  if (kind === "events") return eventGlyph(it && it.category, it && it.status, false, it && it.subcategory);
  return REF_GLYPH;
}

// CSS-mask glyph for UI chrome: <i class="gi" style="--g:url(..)">, coloured by background
export function uiGlyph(kind, it) {
  const k = kind === "aircraft" ? "a:" + aircraftRole(it) : kind === "ships" ? "s:" + shipClass(it) : kind === "fires" ? "fire" : kind === "quakes" ? "quake" : kind === "events" ? (it && it.category === "disaster" && HAZARDS[it.subcategory] ? "h:" + it.subcategory : "e:" + (EVENT_CATS[it && it.category] ? it.category : "")) : "ref";
  return '<i class="gi" style="--g:url(&quot;' + UI[k] + '&quot;)"></i>';
}
