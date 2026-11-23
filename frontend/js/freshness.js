// Per-layer data age in the layer tree ("40s", "6m"): how long since the feed behind each
// layer last delivered, amber / red past per-layer thresholds, so a lagging or dead feed is
// visible at a glance. The tooltip separates the stages: collector poll, rows landing in
// the store, the UI's last fetch, and the newest item's own timestamp (e.g. satellite
// latency for fires).
import { $ } from "/static/js/ui.js";
import { fmtTime, tzLabel, utcTitle } from "/static/js/timefmt.js";

// seconds after which the age turns amber / red (collector interval + store flush + slack)
const LIMITS = {
  aircraft: [90, 300], ships: [90, 300],
  fires: [15 * 60, 45 * 60], quakes: [4 * 60, 15 * 60], events: [10 * 60, 30 * 60],
};
const LAYERS = Object.keys(LIMITS);

export const fmtAge = (s) => (s == null || !isFinite(s) ? "–" : s < 60 ? Math.max(0, Math.round(s)) + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? (s / 3600).toFixed(s < 36000 ? 1 : 0) + "h" : Math.round(s / 86400) + "d");
const ms = (iso) => { const t = iso ? Date.parse(iso) : NaN; return isNaN(t) ? null : t; };

export class Freshness {
  constructor() {
    this.status = null;   // last /api/status
    this.fetched = {};    // layer -> ms of the UI's last successful fetch / live delta
    this.newest = {};     // layer -> ms of the newest item timestamp seen
    this.els = {};
    for (const k of LAYERS) {
      const b = $("c-" + k);
      if (!b) continue;
      const el = document.createElement("i");
      el.className = "age";
      b.parentNode.insertBefore(el, b);
      this.els[k] = el;
    }
    setInterval(() => this.render(), 1000);
  }
  /** The UI received data for a layer; `items` (optional) to track the newest timestamp. */
  note(layer, items, tsKey = "ts") {
    this.fetched[layer] = Date.now();
    if (!items || !items.length) return;
    let best = this.newest[layer] || 0;
    for (const it of items) { const t = ms(it && it[tsKey]); if (t && t > best) best = t; }
    this.newest[layer] = best;
  }
  setStatus(st) { this.status = st; }
  // age (s) shown for a layer: since its feed last delivered
  age(layer, now = Date.now()) {
    const L = (this.status && this.status.layers && this.status.layers[layer]) || {};
    if (layer === "events") return this.newest.events ? (now - this.newest.events) / 1000 : null;
    const t = ms(L.polled_at) || this.fetched[layer] || null;
    return t ? (now - t) / 1000 : null;
  }
  render() {
    const now = Date.now();
    for (const [k, el] of Object.entries(this.els)) {
      const a = this.age(k, now);
      const txt = fmtAge(a);
      if (el.textContent !== txt) el.textContent = txt;
      const [warn, bad] = LIMITS[k];
      const cls = "age" + (a == null ? "" : a > bad ? " stale" : a > warn ? " warn" : "");
      if (el.className !== cls) el.className = cls;
      el.title = this.tip(k, now);
    }
  }
  tip(k, now) {
    const L = (this.status && this.status.layers && this.status.layers[k]) || {};
    // relative age + the clock time in the display zone (timefmt.js) + the exact UTC instant
    const ago = (t) => (t ? fmtAge((now - t) / 1000) + " ago · " + fmtTime(t, { seconds: true }) + " " + tzLabel(t).split(" ")[0] + " (" + utcTitle(t) + ")" : "–");
    const lines = [];
    if (k === "events") lines.push("newest event update: " + ago(this.newest.events));
    else {
      lines.push((L.source || "feed") + " last poll: " + ago(ms(L.polled_at)));
      if (k === "fires" || k === "quakes") lines.push("new rows stored: " + ago(ms(L.data_at)));
      lines.push("newest item timestamp: " + ago(this.newest[k]));
    }
    lines.push("UI updated: " + ago(this.fetched[k]));
    return lines.join("\n");
  }
}
