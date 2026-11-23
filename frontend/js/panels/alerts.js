// Alert center: client-side detectors (new high-priority aircraft, emergency squawks,
// significant quakes, source outages) feeding a toast stack + the Alerts list.
import { $, esc, REDUCED, evTitle, trPending } from "/static/js/ui.js";
import { fmtTime, utcTitle } from "/static/js/timefmt.js";

const MAX = 200, MAX_TOASTS = 4, TTL = 9000;
const SEV = { high: "Threat", warn: "Warning", info: "Info", ok: "Restored", event: "Event" };
const hhmm = (d) => fmtTime(d);

export class AlertCenter {
  constructor(onFly) {
    this.onFly = onFly;
    this.list = [];
    this.seen = { hp: new Set(), quake: new Set(), ev: new Set() };
    this.primed = { hp: false, quake: false, ev: false };
    this.lastEv = 0;
    this.onChange = null;
    this.unread = 0;
    $("toasts").addEventListener("click", (e) => {
      const t = e.target.closest(".toast");
      if (!t) return;
      const a = this.list.find((x) => x.n === +t.dataset.n);
      if (a && a.target) this.onFly(a.target);
      this._dismiss(t);
    });
  }
  push(a) {
    a.n = (this._n = (this._n || 0) + 1);
    a.at = new Date();
    this.list.unshift(a);
    if (this.list.length > MAX) this.list.length = MAX;
    this.unread++;
    this._toast(a);
    if (this.onChange) this.onChange();
  }
  _toast(a) {
    const box = $("toasts");
    while (box.children.length >= MAX_TOASTS) box.lastChild.remove();
    const el = document.createElement("div");
    el.className = "toast sev-" + a.sev;
    el.dataset.n = a.n;
    el.title = a.target ? "Click to fly there" : "";
    el.innerHTML = '<span class="sev">' + SEV[a.sev] + '</span><span class="t">' + esc(a.title) + "</span><time>" + hhmm(a.at) + "</time>" + (a.sub ? '<span class="s">' + esc(a.sub) + "</span>" : "");
    box.prepend(el);
    setTimeout(() => this._dismiss(el), TTL);
  }
  _dismiss(el) {
    if (!el.isConnected) return;
    if (REDUCED) { el.remove(); return; }
    el.classList.add("out");
    setTimeout(() => el.remove(), 220);
  }

  // ---- detectors (call after each live sync) ----
  aircraft(items) {
    const first = !this.primed.hp;
    let fresh = [];
    for (const it of items) {
      if (!it.high_priority) continue;
      if (!this.seen.hp.has(it.id)) { this.seen.hp.add(it.id); fresh.push(it); }
    }
    this.primed.hp = true;
    if (first) {
      if (fresh.length) this.push({ sev: "info", title: fresh.length + " high-priority aircraft tracked", sub: fresh.slice(0, 4).map((x) => x.name || x.id).join(", ") + (fresh.length > 4 ? "…" : "") });
      return;
    }
    if (fresh.length > 5) { this.push({ sev: "high", title: fresh.length + " new high-priority aircraft", sub: fresh.slice(0, 4).map((x) => x.name || x.id).join(", ") + "…" }); return; }
    for (const it of fresh) {
      this.push({ sev: "high", title: "High-priority " + (it.type_code || "aircraft") + " · " + (it.name || it.id), sub: [it.operator || it.category, it.alt_ft != null ? Math.round(it.alt_ft).toLocaleString() + " ft" : null].filter(Boolean).join(" · "),
        target: { kind: "aircraft", id: it.id, lat: it.lat, lon: it.lon } });
    }
  }
  quakes(items) {
    const first = !this.primed.quake;
    this.primed.quake = true;
    for (const q of items) {
      if (!(q.mag >= 5) || this.seen.quake.has(q.id)) continue;
      this.seen.quake.add(q.id);
      const age = Date.now() - Date.parse(q.ts);
      if (first && age > 6 * 3600e3) continue; // only recent ones on first load
      this.push({ sev: q.mag >= 6.5 ? "high" : "warn", title: "M " + q.mag.toFixed(1) + " earthquake", sub: q.place || "", target: { kind: "quakes", id: q.id, lat: q.lat, lon: q.lon } });
    }
  }
  // News events: toast when a multi-source event with confidence >= 0.7 first shows up (max 1 per 10 s;
  // the rest stay unseen and surface on a later pass). The first pass only primes the seen set.
  events(items) {
    const first = !this.primed.ev;
    this.primed.ev = true;
    for (const it of items) {
      if (it.status !== "multi-source" || !(it.confidence >= 0.7) || this.seen.ev.has(it.id)) continue;
      if (first) { this.seen.ev.add(it.id); continue; }
      if (Date.now() - this.lastEv < 10000) return;
      this.seen.ev.add(it.id);
      this.lastEv = Date.now();
      this.push({ sev: "event", title: evTitle(it) || "New event", sub: (it.category || "").replace(/_/g, " ") + " · " + (it.source_diversity || 0) + " outlets · conf " + Number(it.confidence).toFixed(2) + (trPending(it) ? " · translating…" : ""),
        target: { kind: "events", id: it.id, lat: it.lat, lon: it.lon } });
    }
  }
  source(name, from, to, s) {
    if (to === "down") this.push({ sev: "warn", title: "Source down · " + name, sub: s.last_error || "" });
    else if (to === "healthy" && (from === "down" || from === "degraded")) this.push({ sev: "ok", title: "Source restored · " + name });
  }
  squawk(it) {
    const sq = it && it.extra && it.extra.squawk;
    if (!/^7[567]00$/.test(sq || "")) return;
    const k = it.id + sq;
    if (this.seen.hp.has(k)) return;
    this.seen.hp.add(k);
    this.push({ sev: "high", title: "Squawk " + sq + " · " + (it.name || it.id), sub: { 7500: "Unlawful interference", 7600: "Radio failure", 7700: "General emergency" }[sq], target: { kind: "aircraft", id: it.id, lat: it.lat, lon: it.lon } });
  }

  html() {
    if (!this.list.length) return '<div class="empty"><b>No alerts</b>New high-priority aircraft, emergency squawks, M5+ quakes, new corroborated events and source outages appear here.</div>';
    return '<ul class="alist">' + this.list.map((a) => '<li class="sev-' + a.sev + '" data-n="' + a.n + '"><time title="' + esc(utcTitle(a.at)) + '">' + hhmm(a.at) + "</time><b>" + esc(a.title) + "</b><small>" + esc(a.sub || SEV[a.sev]) + "</small></li>").join("") + "</ul>";
  }
  pick(n) { const a = this.list.find((x) => x.n === n); if (a && a.target) this.onFly(a.target); }
}
