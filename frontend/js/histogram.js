// 48 h activity histogram under the timeline scrubber.
// Bars: fire detections + quakes per hour (stacked), binned client-side from the 48 h layer feeds.
// Lines: aircraft / ships in the air / at sea, sampled from /api/snapshot every 4 h (lazy, sequential).
import { $, esc } from "/static/js/ui.js";
import { fetchJson } from "/static/js/api.js";

const H = 48;
const COL = { fires: "#ff6a1a", quakes: "#ffd24a", events: "#a99bff", air: "#5ee3ea", sea: "#7fa8ff" };
const SAMPLE_EVERY_H = 4;

export const tsMs = (v) => {
  if (v == null) return NaN;
  if (typeof v === "number") return v < 1e11 ? v * 1000 : v;
  const s = String(v);
  // tolerate "YYYY-MM-DD HH:MM:SS" and naive timestamps (backend is UTC)
  const iso = s.includes("T") ? s : s.replace(" ", "T");
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
};

// Hourly bins over [t0, t0 + 48 h]; items stamped up to one hour ahead (GDELT 15-min batches
// are labelled with the batch end) land in the newest bin instead of being dropped.
export function binHours(items, field, t0) {
  const b = new Array(H).fill(0);
  for (const it of items || []) {
    const t = tsMs(it && it[field]);
    if (isNaN(t)) continue;
    let i = Math.floor((t - t0) / 3600e3);
    if (i === H) i = H - 1;
    if (i >= 0 && i < H) b[i]++;
  }
  return b;
}

export class Histogram {
  constructor(spanMin) {
    this.span = spanMin;
    this.cv = $("tl-hist");
    this.ctx = this.cv.getContext("2d");
    this.fires = new Array(H).fill(0); this.quakes = new Array(H).fill(0); this.events = new Array(H).fill(0);
    this.air = []; this.sea = []; this.cursor = null; this.t0 = Date.now() - H * 3600e3;
    this.ok = { obs: false };
    new ResizeObserver(() => this.draw()).observe(this.cv);
    this._hover();
    setTimeout(() => this.loadObs(), 2500);
    setTimeout(() => this.loadSamples(), 6000);
    setInterval(() => this.loadObs(), 10 * 60e3);
    setInterval(() => this.loadSamples(), 30 * 60e3);
  }
  // Each feed is fetched and binned independently: one failing request (e.g. a transient 500)
  // must not zero the others. Timestamps carry offsets / microseconds; parse via tsMs.
  async loadObs() {
    const now = Date.now(), t0 = now - H * 3600e3;
    const [f, q, ev] = await Promise.allSettled([
      fetchJson("/api/layers/fires?hours=48", 30000), fetchJson("/api/layers/quakes?hours=48", 30000),
      fetchJson("/api/events?limit=10000&since=" + encodeURIComponent(new Date(t0).toISOString()), 30000),
    ]);
    this.t0 = t0;
    if (f.status === "fulfilled") { this.fires = binHours(f.value.items, "ts", t0); this.truncF = !!f.value.truncated; }
    if (q.status === "fulfilled") { this.quakes = binHours(q.value.items, "ts", t0); this.truncQ = !!q.value.truncated; }
    if (ev.status === "fulfilled") this.events = binHours(ev.value.items, "t_start", t0);
    this.trunc = !!(this.truncF || this.truncQ);
    this.ok.obs = f.status === "fulfilled" || q.status === "fulfilled";
    // retry soon if anything failed rather than waiting for the 10 min refresh
    if ([f, q, ev].some((r) => r.status === "rejected")) { clearTimeout(this._retry); this._retry = setTimeout(() => this.loadObs(), 20000); }
    this.draw();
  }
  async loadSamples() {
    const now = Date.now();
    const air = [], sea = [];
    for (let h = 0; h <= H; h += SAMPLE_EVERY_H) {
      const at = new Date(now - h * 3600e3).toISOString();
      try {
        const [a, s] = await Promise.all([
          fetchJson("/api/snapshot?kind=air&window_min=10&at=" + encodeURIComponent(at), 20000),
          fetchJson("/api/snapshot?kind=sea&window_min=30&at=" + encodeURIComponent(at), 20000),
        ]);
        air.push({ t: Date.parse(at), n: (a.items || []).length });
        sea.push({ t: Date.parse(at), n: (s.items || []).length });
      } catch (e) { if (h === 0) return; }
      this.air = air.slice().sort((x, y) => x.t - y.t);
      this.sea = sea.slice().sort((x, y) => x.t - y.t);
      this.draw();
      await new Promise((r) => setTimeout(r, 300));
    }
  }
  setCursor(frac) { this.cursor = frac; this.draw(); }
  legend() {
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    const lastAir = this.air.length ? this.air[this.air.length - 1].n : null;
    $("dk-legend").innerHTML =
      '<span><i style="--c:' + COL.fires + '"></i>Fires <em>' + sum(this.fires).toLocaleString() + (this.trunc ? "+" : "") + "</em></span>" +
      '<span><i style="--c:' + COL.quakes + '"></i>Quakes <em>' + sum(this.quakes).toLocaleString() + "</em></span>" +
      '<span title="News events by first-report hour"><i style="--c:' + COL.events + '"></i>Events <em>' + sum(this.events).toLocaleString() + "</em></span>" +
      '<span title="Sampled every ' + SAMPLE_EVERY_H + ' h from replay snapshots"><i class="l" style="--c:' + COL.air + '"></i>Aircraft' + (lastAir != null ? " <em>" + lastAir + "</em>" : "") + "</span>" +
      '<span title="Sampled every ' + SAMPLE_EVERY_H + ' h from replay snapshots"><i class="l d" style="--c:' + COL.sea + '"></i>Ships</span>' +
      '<span class="dk-unit">per hour · 48 h</span>';
  }
  draw() {
    const cv = this.cv, dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const c = this.ctx;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    const top = 6, base = h - 11, ph = base - top;
    const bw = w / H;
    // 6-hour grid + UTC labels
    c.font = "9px Consolas, 'Cascadia Mono', monospace";
    c.textBaseline = "alphabetic";
    for (let i = 0; i <= H; i++) {
      const t = new Date(this.t0 + i * 3600e3);
      if (t.getUTCHours() % 6 !== 0) continue;
      const x = Math.round(i * bw) + 0.5;
      c.fillStyle = t.getUTCHours() === 0 ? "rgba(159,176,192,.28)" : "rgba(159,176,192,.12)";
      c.fillRect(x, top, 1, ph);
      c.fillStyle = "rgba(159,176,192,.55)";
      c.fillText(t.getUTCHours() === 0 ? t.toISOString().slice(5, 10) : String(t.getUTCHours()).padStart(2, "0") + "Z", x + 3, h - 2);
    }
    // stacked bars
    const tot = this.fires.map((f, i) => f + this.quakes[i] + this.events[i]);
    const max = Math.max(1, ...tot);
    const sc = (v) => Math.sqrt(v / max) * ph; // sqrt keeps quiet hours visible next to fire spikes
    for (let i = 0; i < H; i++) {
      const x = i * bw + 1, bwid = Math.max(1, bw - 2);
      const hq = sc(this.quakes[i]), ht = sc(this.fires[i] + this.quakes[i]), hv = sc(tot[i]);
      if (hv <= 0) continue;
      c.fillStyle = COL.events; c.globalAlpha = 0.7;
      c.fillRect(x, base - hv, bwid, hv - ht);
      c.fillStyle = COL.fires; c.globalAlpha = 0.55;
      c.fillRect(x, base - ht, bwid, ht - hq);
      c.fillStyle = COL.quakes; c.globalAlpha = 0.75;
      c.fillRect(x, base - hq, bwid, hq);
    }
    c.globalAlpha = 1;
    // sampled lines
    const line = (pts, col, dash) => {
      if (pts.length < 2) return;
      const m = Math.max(1, ...pts.map((p) => p.n));
      c.strokeStyle = col; c.lineWidth = 1.3; c.setLineDash(dash ? [3, 3] : []);
      c.beginPath();
      pts.forEach((p, i) => {
        const x = ((p.t - this.t0) / (H * 3600e3)) * w, y = base - (p.n / m) * ph * 0.92;
        if (i) c.lineTo(x, y); else c.moveTo(x, y);
      });
      c.stroke(); c.setLineDash([]);
      c.fillStyle = col;
      for (const p of pts) { const x = ((p.t - this.t0) / (H * 3600e3)) * w; c.fillRect(x - 1.5, base - (p.n / m) * ph * 0.92 - 1.5, 3, 3); }
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
      const i = Math.floor(f * H);
      const t = new Date(this.t0 + i * 3600e3);
      const near = (pts) => { let b = null; for (const p of pts) if (!b || Math.abs(p.t - t) < Math.abs(b.t - t)) b = p; return b && Math.abs(b.t - t) < 2.5 * 3600e3 ? b.n : null; };
      const a = near(this.air), s = near(this.sea);
      tip.innerHTML = "<b>" + esc(t.toISOString().slice(5, 13).replace("T", " ")) + ":00Z</b> +1 h<br>fires " + this.fires[i] + " · quakes " + this.quakes[i] +
        (a != null ? "<br>aircraft ≈ " + a + (s != null ? " · ships ≈ " + s : "") : "");
      tip.style.left = Math.min(r.width - 70, Math.max(70, e.clientX - r.left)) + "px";
      tip.classList.remove("hidden");
    });
    slider.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  }
}
