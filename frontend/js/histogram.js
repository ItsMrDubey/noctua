// Activity histogram under the timeline scrubber; bins adapt to the range (hourly up to 7 d, daily beyond).
// Bars (stacked): news events, UCDP historical conflict events, fire detections, quakes. One
// /api/history/counts request per layer (server-side GROUP BY), not full rows.
// Lines: aircraft / ships in the air / at sea, sampled from /api/snapshot (lazy, sequential).
import { $, esc } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";
import { fmtDate, fmtTime, fmtDateTime, tzOffset, tzLabel, utcTitle, onTzChange } from "/static/js/timefmt.js";

const COL = { fires: "#ff6a1a", quakes: "#ffd24a", events: "#a99bff", hist: "#8f86c9", air: "#5ee3ea", sea: "#7fa8ff" };
const HOUR = 3600e3, DAY = 86400e3, CHUNK_H = 168;
const SAMPLES = 12;

export const tsMs = (v) => {
  if (v == null) return NaN;
  if (typeof v === "number") return v < 1e11 ? v * 1000 : v;
  const s = String(v);
  // tolerate "YYYY-MM-DD HH:MM:SS" and naive timestamps (backend is UTC)
  const iso = s.includes("T") ? s : s.replace(" ", "T");
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
};

// Bins of binMs over [t0, t0 + n * binMs]; items stamped up to one bin ahead (GDELT 15-min batches
// are labelled with the batch end) land in the newest bin instead of being dropped.
export function binItems(items, field, t0, binMs, n, into) {
  const b = into || new Array(n).fill(0);
  for (const it of items || []) {
    const t = tsMs(it && it[field]);
    if (isNaN(t)) continue;
    let i = Math.floor((t - t0) / binMs);
    if (i === n) i = n - 1;
    if (i >= 0 && i < n) b[i]++;
  }
  return b;
}
// backwards compatible 48 h hourly helper
export const binHours = (items, field, t0) => binItems(items, field, t0, HOUR, 48);

// Server-side aggregates ([[bin_start_ms, n], ...], UTC-aligned bins) into our n bins starting at t0.
export function binCounts(counts, t0, binMs, n) {
  const b = new Array(n).fill(0);
  for (const [t, c] of counts || []) {
    let i = Math.floor((t - t0) / binMs);
    if (i === n) i = n - 1;
    if (i >= 0 && i < n) b[i] += c;
  }
  return b;
}

export class Histogram {
  constructor(spanMin) {
    this.cv = $("tl-hist");
    onTzChange(() => this.draw());
    this.ctx = this.cv.getContext("2d");
    this.cursor = null; this.gen = 0; this.sgen = 0;
    this._configure(spanMin);
    new ResizeObserver(() => this.draw()).observe(this.cv);
    this._hover();
    setTimeout(() => this.loadObs(), 2500);
    setTimeout(() => this.loadSamples(), 6000);
    setInterval(() => this.loadObs(), 10 * 60e3);
    setInterval(() => this.loadSamples(), 30 * 60e3);
  }
  _configure(spanMin) {
    this.span = spanMin;
    this.binMs = spanMin <= 7 * 1440 ? HOUR : DAY;
    this.n = Math.ceil((spanMin * 60000) / this.binMs);
    this.t0 = Date.now() - spanMin * 60000;
    const z = () => new Array(this.n).fill(0);
    this.fires = z(); this.quakes = z(); this.events = z(); this.hist = z();
    this.air = []; this.sea = []; this.trunc = false; this.firesNA = false;
  }
  setSpan(spanMin) {
    if (spanMin === this.span) return;
    this._configure(spanMin);
    this.draw();
    this.loadObs();
    this.loadSamples();
  }
  get spanMs() { return this.span * 60000; }
  // Each feed is fetched and binned independently: one failing request (e.g. a transient 500)
  // must not zero the others. Timestamps carry offsets / microseconds; parse via tsMs.
  async loadObs() {
    const g = ++this.gen;
    this.loading = true; this.draw();
    const now = Date.now(), t0 = now - this.spanMs, n = this.n, bin = this.binMs;
    const q = (layer) => fetchJson("/api/history/counts?layer=" + layer + "&bin=" + (bin === HOUR ? "hour" : "day") +
      "&start=" + encodeURIComponent(new Date(t0).toISOString()) + "&end=" + encodeURIComponent(new Date(now).toISOString()), 30000);
    const [f, qk, ev, hi] = await Promise.allSettled([q("fires"), q("quakes"), q("events"), q("historical")]);
    if (g !== this.gen) return; // range changed meanwhile
    this.loading = false;
    this.t0 = t0;
    const put = (r, key) => { if (r.status === "fulfilled") this[key] = binCounts(r.value.counts, t0, bin, n); };
    put(f, "fires"); put(qk, "quakes"); put(ev, "events"); put(hi, "hist");
    this.trunc = false;
    // retry soon if anything failed rather than waiting for the 10 min refresh
    if ([f, qk, ev, hi].some((r) => r.status === "rejected")) { clearTimeout(this._retry); this._retry = setTimeout(() => this.loadObs(), 20000); }
    this.draw();
  }
  async loadSamples() {
    const g = ++this.sgen, span = this.spanMs, now = Date.now(), step = span / SAMPLES;
    const air = [], sea = [];
    this.air = []; this.sea = [];
    for (let k = 0; k <= SAMPLES; k++) {
      if (g !== this.sgen) return;
      const at = new Date(now - k * step).toISOString();
      try {
        const [a, s] = await Promise.all([
          fetchJson("/api/snapshot?kind=air&window_min=10&at=" + encodeURIComponent(at), 20000),
          fetchJson("/api/snapshot?kind=sea&window_min=30&at=" + encodeURIComponent(at), 20000),
        ]);
        if (span !== this.spanMs) return;
        air.push({ t: Date.parse(at), n: (a.items || []).length });
        sea.push({ t: Date.parse(at), n: (s.items || []).length });
      } catch (e) { if (k === 0) return; }
      this.air = air.slice().sort((x, y) => x.t - y.t);
      this.sea = sea.slice().sort((x, y) => x.t - y.t);
      this.draw();
      await new Promise((r) => setTimeout(r, 300));
    }
  }
  setCursor(frac) { this.cursor = frac; this.draw(); }
  get rangeLabel() { return this.span >= 365 * 1440 ? "1 y" : this.span >= 1440 * 2 + 1 ? Math.round(this.span / 1440) + " d" : Math.round(this.span / 60) + " h"; }
  legend() {
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    const lastAir = this.air.length ? this.air[this.air.length - 1].n : null;
    const nh = sum(this.hist), every = this.binMs === HOUR ? Math.round(this.span / 60 / SAMPLES) + " h" : Math.round(this.span / 1440 / SAMPLES) + " d";
    $("dk-legend").innerHTML =
      '<span' + (this.firesNA ? ' title="Fire detections are not aggregated beyond 30 d"' : "") + '><i style="--c:' + COL.fires + '"></i>Fires <em>' + (this.firesNA ? "n/a" : sum(this.fires).toLocaleString() + (this.trunc ? "+" : "")) + "</em></span>" +
      '<span><i style="--c:' + COL.quakes + '"></i>Quakes <em>' + sum(this.quakes).toLocaleString() + "</em></span>" +
      '<span title="News events by first-report time"><i style="--c:' + COL.events + '"></i>Events <em>' + sum(this.events).toLocaleString() + "</em></span>" +
      (nh ? '<span title="UCDP historical conflict events"><i style="--c:' + COL.hist + '"></i>UCDP <em>' + nh.toLocaleString() + "</em></span>" : "") +
      '<span title="Sampled every ' + every + ' from replay snapshots"><i class="l" style="--c:' + COL.air + '"></i>Aircraft' + (lastAir != null ? " <em>" + lastAir + "</em>" : "") + "</span>" +
      '<span title="Sampled every ' + every + ' from replay snapshots"><i class="l d" style="--c:' + COL.sea + '"></i>Ships</span>' +
      '<span class="dk-unit">' + (this.loading ? "loading… · " : "") + "per " + (this.binMs === HOUR ? "hour" : "day") + " · " + this.rangeLabel + "</span>";
  }
  // grid ticks: [time, label|null, major]
  _ticks() {
    const t0 = this.t0, t1 = t0 + this.spanMs, out = [];
    const days = this.span / 1440;
    if (days > 60) { // months
      const d = new Date(t0); d.setUTCDate(1); d.setUTCHours(0, 0, 0, 0);
      for (d.setUTCMonth(d.getUTCMonth() + 1); d.getTime() <= t1; d.setUTCMonth(d.getUTCMonth() + 1)) {
        const m = d.getUTCMonth();
        out.push([d.getTime(), m === 0 ? String(d.getUTCFullYear()) : String(m + 1).padStart(2, "0"), m === 0]);
      }
      return out;
    }
    // hour / day boundaries of the display zone (timefmt.js): step through wall-clock time
    const step = days <= 2 ? 6 * HOUR : DAY;
    const off0 = tzOffset(t0) * 60000;
    for (let w = Math.ceil((t0 + off0) / step) * step; ; w += step) {
      const t = w - tzOffset(w - off0) * 60000;
      if (t > t1) break;
      if (t < t0) continue;
      const hh = fmtTime(t).slice(0, 2), dom = +fmtDate(t).slice(8, 10);
      if (days <= 2) out.push([t, hh === "00" ? fmtDate(t, { short: true }) : hh + "h", hh === "00"]);
      else if (days <= 8) out.push([t, fmtDate(t, { short: true }), true]);
      else out.push([t, dom % 5 === 1 && dom < 30 ? fmtDate(t, { short: true }) : null, dom === 1]);
    }
    return out;
  }
  draw() {
    const cv = this.cv, dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const c = this.ctx, N = this.n, span = this.spanMs;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    const top = 6, base = h - 11, ph = base - top;
    const bw = w / N;
    c.font = "9px Consolas, 'Cascadia Mono', monospace";
    c.textBaseline = "alphabetic";
    for (const [t, label, major] of this._ticks()) {
      const x = Math.round(((t - this.t0) / span) * w) + 0.5;
      c.fillStyle = major ? "rgba(159,176,192,.28)" : "rgba(159,176,192,.12)";
      c.fillRect(x, top, 1, ph);
      if (label) { c.fillStyle = "rgba(159,176,192,.55)"; c.fillText(label, x + 3, h - 2); }
    }
    // stacked bars: quakes, fires, historical, events (bottom -> top)
    let max = 1;
    for (let i = 0; i < N; i++) max = Math.max(max, this.fires[i] + this.quakes[i] + this.events[i] + this.hist[i]);
    const sc = (v) => Math.sqrt(v / max) * ph; // sqrt keeps quiet bins visible next to spikes
    const gap = bw > 5 ? 2 : bw > 2.5 ? 1 : 0;
    for (let i = 0; i < N; i++) {
      const x = i * bw + gap / 2, bwid = Math.max(1, bw - gap);
      const q = this.quakes[i], f = q + this.fires[i], hs = f + this.hist[i], tot = hs + this.events[i];
      if (tot <= 0) continue;
      const hq = sc(q), hf = sc(f), hh = sc(hs), hv = sc(tot);
      c.fillStyle = COL.events; c.globalAlpha = 0.7; c.fillRect(x, base - hv, bwid, hv - hh);
      c.fillStyle = COL.hist; c.globalAlpha = 0.45; c.fillRect(x, base - hh, bwid, hh - hf);
      c.fillStyle = COL.fires; c.globalAlpha = 0.55; c.fillRect(x, base - hf, bwid, hf - hq);
      c.fillStyle = COL.quakes; c.globalAlpha = 0.75; c.fillRect(x, base - hq, bwid, hq);
    }
    c.globalAlpha = 1;
    // sampled lines
    const line = (pts, col, dash) => {
      if (pts.length < 2) return;
      const m = Math.max(1, ...pts.map((p) => p.n));
      c.strokeStyle = col; c.lineWidth = 1.3; c.setLineDash(dash ? [3, 3] : []);
      c.beginPath();
      pts.forEach((p, i) => {
        const x = ((p.t - this.t0) / span) * w, y = base - (p.n / m) * ph * 0.92;
        if (i) c.lineTo(x, y); else c.moveTo(x, y);
      });
      c.stroke(); c.setLineDash([]);
      c.fillStyle = col;
      for (const p of pts) { const x = ((p.t - this.t0) / span) * w; c.fillRect(x - 1.5, base - (p.n / m) * ph * 0.92 - 1.5, 3, 3); }
    };
    line(this.sea, COL.sea, true);
    line(this.air, COL.air, false);
    // future-of-cursor dimming while replaying
    if (this.cursor != null && this.cursor < 1) {
      c.fillStyle = "rgba(3,7,12,.55)";
      c.fillRect(this.cursor * w, 0, w - this.cursor * w, h);
      c.fillStyle = "rgba(242,173,61,.08)";
      c.fillRect(0, 0, this.cursor * w, h);
    }
    c.fillStyle = "rgba(120,175,205,.25)";
    c.fillRect(0, base, w, 1);
    this.legend();
  }
  _hover() {
    const slider = $("tl-slider"), wrap = slider.parentElement;
    const tip = document.createElement("div");
    tip.className = "dk-tip hidden";
    wrap.appendChild(tip);
    slider.addEventListener("mousemove", (e) => {
      const r = slider.getBoundingClientRect();
      const f = Math.min(0.9999, Math.max(0, (e.clientX - r.left) / r.width));
      const i = Math.floor(f * this.n);
      const t = new Date(this.t0 + i * this.binMs);
      const near = (pts) => { let b = null; for (const p of pts) if (!b || Math.abs(p.t - t) < Math.abs(b.t - t)) b = p; return b && Math.abs(b.t - t) < 0.6 * (this.spanMs / SAMPLES) ? b.n : null; };
      const a = near(this.air), s = near(this.sea);
      const head = this.binMs === HOUR ? esc(fmtDateTime(t, { short: true }) + " " + tzLabel(t).split(" ")[0]) + "</b> +1 h" : esc(fmtDateTime(t, { zone: true })) + "</b> +1 d";
      tip.title = utcTitle(t);
      tip.innerHTML = "<b>" + head + "<br>" + (this.firesNA ? "" : "fires " + this.fires[i] + " · ") + "quakes " + this.quakes[i] + " · events " + this.events[i] +
        (this.hist[i] ? " · UCDP " + this.hist[i] : "") +
        (a != null ? "<br>aircraft ≈ " + a + (s != null ? " · ships ≈ " + s : "") : "");
      tip.style.left = Math.min(r.width - 70, Math.max(70, e.clientX - r.left)) + "px";
      tip.classList.remove("hidden");
    });
    slider.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  }
}
