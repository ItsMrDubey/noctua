// Command bar: source-health chips with records/min sparklines, UTC + local clocks.
import { $, esc } from "/static/js/ui.js";

const BUCKETS = 20; // minutes of history per sparkline

export class StatusBar {
  constructor() {
    this.hist = {};      // source -> {minute -> records}
    this.lastSeen = {};  // source -> last_success string
    this.prevStatus = {};
    this.onTransition = null; // (name, from, to, info) for alerting
  }

  _record(name, s) {
    if (!s.last_success || this.lastSeen[name] === s.last_success) return;
    const first = this.lastSeen[name] === undefined;
    this.lastSeen[name] = s.last_success;
    if (first) return; // don't count the batch that happened before we connected
    const m = Math.floor(Date.now() / 60000);
    const h = (this.hist[name] = this.hist[name] || {});
    h[m] = (h[m] || 0) + (s.last_batch_size || 0);
    for (const k of Object.keys(h)) if (+k < m - BUCKETS) delete h[k];
  }

  _series(name) {
    const h = this.hist[name] || {};
    const m = Math.floor(Date.now() / 60000);
    const out = [];
    for (let i = m - BUCKETS + 1; i <= m; i++) out.push(h[i] || 0);
    return out;
  }

  _spark(vals) {
    const max = Math.max(1, ...vals);
    const pts = vals.map((v, i) => (i / (vals.length - 1)) * 34 + "," + (12 - (v / max) * 11).toFixed(1));
    return '<svg viewBox="0 0 34 12" preserveAspectRatio="none"><polygon points="0,12 ' + pts.join(" ") + ' 34,12"/><polyline points="' + pts.join(" ") + '"/></svg>';
  }

  render(status, errors) {
    const rows = [];
    const sources = (status && status.sources) || {};
    for (const n of Object.keys(sources)) {
      const s = sources[n] || {};
      this._record(n, s);
      const prev = this.prevStatus[n];
      if (prev && prev !== s.status && this.onTransition) this.onTransition(n, prev, s.status, s);
      this.prevStatus[n] = s.status;
      const vals = this._series(n);
      const prevMin = vals[vals.length - 2] || 0, curMin = vals[vals.length - 1] || 0;
      const rate = Math.max(prevMin, curMin);
      const tip = n + ": " + (s.status || "unknown") + "\nlast success: " + (s.last_success || "–") + "\nlast error: " + (s.last_error || "none") +
        "\nlast batch: " + (s.last_batch_size == null ? "–" : s.last_batch_size) + "\nrecords/min (last " + BUCKETS + " min sparkline): " + rate;
      rows.push('<div class="src ' + esc(s.status) + '" title="' + esc(tip) + '"><i class="dot ' + esc(s.status) + '"></i><span class="nm">' + esc(n) +
        '</span><span class="spk">' + this._spark(vals) + '<span class="rate">' + (rate ? fmtRate(rate) : "·") + "</span></span></div>");
    }
    if (!status) rows.push('<div class="src down" title="' + esc("status: " + (errors.status || "no data")) + '"><i class="dot down"></i><span class="nm">api</span></div>');
    for (const k of ["aircraft", "ships", "fires", "quakes", "replay"]) {
      if (errors[k]) rows.push('<div class="src degraded" title="' + esc(k + " feed: " + errors[k]) + '"><i class="dot degraded"></i><span class="nm">' + k + "</span></div>");
    }
    $("sources").innerHTML = rows.join("");
    // rail health dot for live tracks
    const dot = document.querySelector('.rail-btn[data-g="live"] .rail-dot');
    if (dot) {
      const st = Object.values(sources).map((s) => s.status);
      dot.className = "rail-dot" + (!status ? " bad" : st.includes("down") || Object.keys(errors).length ? " warn" : "");
    }
    const c = (status && status.counts) || {};
    const g = $("g-live-n");
    if (g) g.title = "Live store: " + (c.aircraft ?? "–") + " aircraft, " + (c.ships ?? "–") + " ships";
  }
}

const fmtRate = (n) => (n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n)) + "/m";

export function startClock() {
  const pad = (n) => String(n).padStart(2, "0");
  const tz = (() => {
    const o = -new Date().getTimezoneOffset();
    return (o >= 0 ? "+" : "−") + pad(Math.floor(Math.abs(o) / 60)) + (Math.abs(o) % 60 ? ":" + pad(Math.abs(o) % 60) : "");
  })();
  const f = () => {
    const d = new Date();
    $("clock").innerHTML = d.toISOString().slice(11, 19) + "<small>UTC</small>";
    $("clock-local").innerHTML = pad(d.getHours()) + ":" + pad(d.getMinutes()) + "<small>LCL " + tz + "</small>";
  };
  f();
  setInterval(f, 1000);
}
