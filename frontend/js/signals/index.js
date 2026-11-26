// Signals & space: wires the layer-tree controls (Signals group), the space weather widget and
// command-bar pill, the server signal alert feed (-> alert center toasts) and the inspector views
// for GNSS cells, squawks, outages, radiation stations and satellites. app.js only constructs it,
// routes picks with a `signal` id here and calls clearSelection() when another object is selected.
import { fetchJson } from "/static/js/api.js";
import { $, esc, nf, ago, agoHtml, kv, settleLoading } from "/static/js/ui.js";
import { fmtDateTime, fmtTime, utcTitle } from "/static/js/timefmt.js";
import { GnssLayer, GNSS_LEVELS } from "/static/js/signals/gnss.js";
import { SquawkLayer, OutageLayer, RadiationLayer, AuroraLayer, OUTAGE_COLORS, SQK_COLOR, radColor } from "/static/js/signals/overlays.js";
import { SatelliteLayer, SAT_GROUPS, footprintKm } from "/static/js/signals/satellites.js";

const KP_COLORS = ["#3ddc84", "#3ddc84", "#3ddc84", "#3ddc84", "#c9e84a", "#ffd24a", "#ffa040", "#ff6a3d", "#ff4b55", "#ff2bd6"];
const kpColor = (kp) => KP_COLORS[Math.max(0, Math.min(9, Math.floor(kp || 0)))];
const SCALE_COLOR = ["#66798c", "#ffd24a", "#ffa040", "#ff6a3d", "#ff4b55", "#ff2bd6"];
const ts = (s) => (s == null ? null : s * 1000);
const PREFS = "noctua.signals";
// picked only when nothing else is under the cursor (large translucent areas)
export const AREA_SIGNALS = new Set(["gnss", "outage"]);
const FLY_ALT = { gnss: 1.5e6, outage: 4e6, squawk: 6e5, rad: 3e5 };
const loadPrefs = () => { try { return JSON.parse(localStorage.getItem(PREFS) || "{}") || {}; } catch (e) { return {}; } };

export class Signals {
  // hooks: show(item, eyebrow, tint, render(el)) opens an inspector module view; alerts: AlertCenter;
  // flyTo(lat, lon, alt)
  constructor(viewer, hooks) {
    this.viewer = viewer; this.hooks = hooks;
    const scene = viewer.scene;
    this.gnss = new GnssLayer(scene);
    this.squawks = new SquawkLayer(scene);
    this.outages = new OutageLayer(scene);
    this.rad = new RadiationLayer(scene);
    this.aurora = new AuroraLayer(viewer);
    this.sats = new SatelliteLayer(viewer, { onSelect: (it) => this._showSat(it) });
    this.sats.onTrack = (it) => { if (this.sel && this.sel.kind === "sat" && this.sel.it === it) this._renderSat(true); };
    this.sats.onStatus = () => this._counts();
    this.status = null; this.sel = null; this.alertSince = null;
    settleLoading($("sig-pill"), 15000, "Signals status unavailable");
    for (const l of [this.gnss, this.squawks, this.outages, this.rad, this.aurora]) l.onData = () => { this._counts(); this._refreshSel(); };
    this._bind();
    this._pollStatus();
    setInterval(() => this._pollStatus(), 60000);
    setTimeout(() => this._pollAlerts(), 5000);
    setInterval(() => this._pollAlerts(), 30000);
  }

  // ---------------------------------------------------------------- controls
  _bind() {
    const prefs = loadPrefs();
    const save = () => {
      const p = { groups: [...this.sats.groups] };
      for (const id of ["s-gnss", "s-squawks", "s-outages", "s-sats", "s-aurora", "s-radiation", "s-gnss-regions"]) p[id] = $(id).checked;
      p.hours = this.gnss.hours;
      try { localStorage.setItem(PREFS, JSON.stringify(p)); } catch (e) { /* storage disabled */ }
    };
    const err = (id) => (e) => { const l = $(id).closest("label"); l.title = "Unavailable: " + ((e && e.message) || e); };
    for (const l of [["s-gnss", this.gnss], ["s-squawks", this.squawks], ["s-outages", this.outages], ["s-radiation", this.rad], ["s-aurora", this.aurora]]) l[1].onError = err(l[0]);
    const toggles = {
      "s-gnss": (on) => this.gnss.setOn(on), "s-squawks": (on) => this.squawks.setOn(on), "s-outages": (on) => this.outages.setOn(on),
      "s-radiation": (on) => this.rad.setOn(on), "s-aurora": (on) => this.aurora.setOn(on),
      "s-sats": (on) => this.sats.setOn(on).then(() => this._counts(), err("s-sats")),
      "s-gnss-regions": (on) => this.gnss.setRegions(on),
    };
    if (Array.isArray(prefs.groups)) this.sats.groups = new Set(prefs.groups.filter((g) => SAT_GROUPS[g]));
    if (prefs.hours) { this.gnss.hours = +prefs.hours; $("s-gnss-h").value = String(prefs.hours); }
    for (const [id, fn] of Object.entries(toggles)) {
      const box = $(id);
      if (id in prefs) box.checked = !!prefs[id];
      box.addEventListener("change", (e) => { fn(e.target.checked); if (!e.target.checked && this.sel && this._kindOf(id) === this.sel.kind) this.clearSelection(); save(); this._counts(); });
      if (box.checked) fn(true);
    }
    $("s-gnss-h").addEventListener("change", (e) => { this.gnss.setHours(+e.target.value); save(); });
    $("sig-wx").addEventListener("click", () => this.showSpaceWeather());
    $("sig-wx").title = "Space weather details";
    const chips = $("sat-chips");
    chips.innerHTML = Object.entries(SAT_GROUPS).map(([k, [l, c]]) => '<button class="chip' + (this.sats.groups.has(k) ? " on" : "") + '" data-k="' + k + '" style="--c:' + c + '" title="' + esc(k) + '">' + esc(l) + "</button>").join("");
    chips.addEventListener("click", (e) => {
      const b = e.target.closest(".chip");
      if (!b) return;
      b.classList.toggle("on");
      const set = new Set(this.sats.groups);
      if (b.classList.contains("on")) set.add(b.dataset.k); else set.delete(b.dataset.k);
      this.sats.setGroups(set).then(() => this._counts(), () => {});
      save();
    });
  }
  _kindOf(id) { return { "s-gnss": "gnss", "s-squawks": "squawk", "s-outages": "outage", "s-radiation": "rad", "s-sats": "sat" }[id]; }

  _counts() {
    const set = (id, on, n) => { const el = $(id); if (el) el.textContent = on && n != null ? Number(n).toLocaleString() : ""; };
    set("c-gnss", this.gnss.on, this.gnss.cells.length);
    set("c-squawks", this.squawks.on, this.squawks.items.length);
    set("c-outages", this.outages.on, this.outages.items.length);
    set("c-radiation", this.rad.on, this.rad.items.length);
    set("c-sats", this.sats.on, this.sats.count);
    if (this.sats.error) $("s-sats").closest("label").title = "Satellites: " + this.sats.error;
  }

  // ---------------------------------------------------------------- status widget + pill
  async _pollStatus() {
    let s;
    try { s = await fetchJson("/api/signals/status", 10000); } catch (e) { return; }
    this.status = s;
    this._renderWidget();
    this._renderPill();
    this._counts();
  }
  _renderWidget() {
    const s = this.status, sw = (s && s.spaceweather) || {};
    const box = $("sig-wx");
    if (!sw.updated) { box.innerHTML = ""; return; }
    const kp = sw.kp_now ? sw.kp_now[1] : null, sc = sw.scales || {}, wind = sw.solar_wind || {};
    const scale = (k) => { const v = sc[k]; return '<i class="' + (v ? "on" : "") + '" style="--c:' + SCALE_COLOR[v || 0] + '" title="NOAA ' + k + '-scale now">' + k + (v == null ? "–" : v) + "</i>"; };
    const al = sw.latest_alert;
    box.innerHTML = '<div class="kp" style="--c:' + kpColor(kp) + '"><b>' + (kp == null ? "–" : nf(kp, 1)) + "</b><span>Planetary Kp" + (sw.g_now ? " · G" + sw.g_now + " storm" : " · quiet") + "</span></div>" +
      '<div class="scales">' + scale("G") + scale("S") + scale("R") + "</div>" +
      (this._kpBars || "") +
      '<div class="ln"><span>Solar wind</span><b>' + (wind.speed_kms == null ? "–" : nf(wind.speed_kms) + " km/s") + "</b></div>" +
      '<div class="ln"><span>IMF Bz</span><b>' + (wind.bz_nt == null ? "–" : nf(wind.bz_nt, 1) + " nT") + "</b></div>" +
      (al ? '<div class="al" title="' + esc(al.text || al.headline) + '">' + esc(al.kind + ": " + al.headline) + " · " + esc(ago(ts(al.issued)).split(" (")[0]) + "</div>" : "");
    if (!this._kpBars || Date.now() - (this._kpAt || 0) > 600000) this._loadKp();
  }
  async _loadKp() {
    this._kpAt = Date.now();
    try {
      const d = await fetchJson("/api/signals/spaceweather", 15000);
      this.sw = d;
      const kp = (d.kp || []).slice(-24);
      this._kpBars = kp.length ? '<div class="bars" title="Kp, last 3 days (3-hourly)">' + kp.map(([t, v]) => '<i style="height:' + Math.max(6, (v / 9) * 100) + "%;--c:" + kpColor(v) + '" title="' + esc(fmtDateTime(t * 1000, { short: true, zone: true }) + " · Kp " + v + "\n" + utcTitle(t * 1000)) + '"></i>').join("") + "</div>" : "";
      this._renderWidget();
    } catch (e) { /* keep */ }
  }
  _renderPill() {
    const s = this.status;
    const el = $("sig-pill");
    if (!s) return;
    const g = s.gnss || {}, q = s.squawks || {}, o = s.outages || {}, sw = s.spaceweather || {}, r = s.radiation || {}, sat = s.satellites || {};
    const kp = sw.kp_now ? sw.kp_now[1] : null;
    const hot = (n, txt) => (n ? '<b class="hot">' + txt + "</b>" : "<b>" + txt + "</b>");
    const lvl = q.active || g.high || r.elevated ? "down" : g.medium || o.ongoing || (sw.g_now || 0) > 0 ? "degraded" : "healthy";
    const row = (k, v) => '<div class="sp-kv"><span>' + k + "</span><b>" + v + "</b></div>";
    const html = '<div class="sp-kv sp-lead"><span><i class="dot ' + lvl + '"></i>Kp ' + (kp == null ? "–" : nf(kp, 1)) + " · GNSS " + hot(g.high, nf(g.high || 0)) + " · SQK " + hot(q.active, nf(q.active || 0)) + " · NET " + nf(o.ongoing || 0) +
      '</span><small title="' + esc(utcTitle(ts(s.now))) + '">' + esc(ago(ts(s.now)).split(" (")[0]) + "</small></div>" +
      "<h6>GNSS interference · live</h6>" + row("Jamming cells (&gt; 10 %)", nf(g.high)) + row("Elevated cells", nf(g.medium)) + row("Aircraft samples / poll", nf(g.samples)) +
      "<h6>Emergency squawks</h6>" + row("Active now", nf(q.active)) + row("Last 24 h", nf(q.day)) +
      "<h6>Internet outages (IODA)</h6>" + row("Countries · ongoing", nf(o.countries) + " · " + nf(o.ongoing)) +
      "<h6>Space weather</h6>" + row("Kp · G-scale", (kp == null ? "–" : nf(kp, 1)) + " · G" + (sw.g_now == null ? "–" : sw.g_now)) +
      "<h6>Radiation</h6>" + row("Stations · elevated", nf(r.stations) + " · " + nf(r.elevated)) +
      "<h6>Satellites</h6>" + row("Objects cached", nf(sat.objects)) +
      '<button type="button" class="ghost sp-link" data-a="sig-layers">Signals &amp; space layers →</button>';
    if (el._h !== html) { el._h = html; el.innerHTML = html; }
    if (!el._bound) {
      el._bound = true;
      el.addEventListener("click", (e) => {
        if (!e.target.closest('[data-a="sig-layers"]')) return;
        const p = document.getElementById("sys-pop");
        if (p && p._close) p._close();
        const b = document.querySelector('.rail-btn[data-g="signals"]'); if (b) b.click();
      });
    }
    const dot = $("sig-rail-dot");
    if (dot) dot.className = "rail-dot" + (lvl === "down" ? " bad" : lvl === "degraded" ? " warn" : "");
    const gn = $("g-signals-n");
    if (gn) gn.textContent = String((q.active || 0) + (g.high || 0) + (o.ongoing || 0) + (r.elevated || 0));
  }

  // ---------------------------------------------------------------- alert feed
  async _pollAlerts() {
    const al = this.hooks.alerts;
    if (!al) return;
    let d;
    try { d = await fetchJson("/api/signals/alerts?since=" + (this.alertSince || 0), 10000); } catch (e) { return; }
    const items = (d.items || []).slice().sort((a, b) => a.ts - b.ts);
    const first = this.alertSince == null;
    this.alertSince = Math.max(this.alertSince || 0, ...items.map((a) => a.ts), d.updated ? d.updated - 3600 * 48 : 0);
    if (first) {
      // first load: one summary of what happened in the last 30 min instead of a toast storm
      const recent = items.filter((a) => Date.now() / 1000 - a.ts < 1800 && a.kind !== "aircraft");
      if (recent.length) al.push({ sev: recent.some((a) => a.sev === "high") ? "high" : "warn", title: recent.length + " signal alert" + (recent.length > 1 ? "s" : "") + " in the last 30 min", sub: recent.slice(-3).map((a) => a.title).join(" · ") });
      return;
    }
    for (const a of items.slice(-6)) {
      if (a.kind === "aircraft") continue; // the client already toasts new high-priority aircraft
      al.push({ sev: a.sev === "high" ? "high" : "warn", title: a.title, sub: a.sub || "", target: a.lat != null ? { lat: a.lat, lon: a.lon } : null });
    }
  }

  // ---------------------------------------------------------------- picking + inspector
  pick(id) {
    if (!id || !id.signal) return false;
    if (id.signal === "sat") return this.sats.pick(id);
    if (this.sel && this.sel.kind === "sat") { this.sel = null; this.sats.clearSelection(); }
    let it = null;
    if (id.signal === "gnss") it = this.gnss.byCell.get(id.cell);
    else if (id.signal === "squawk") it = this.squawks.byId.get(id.id);
    else if (id.signal === "outage") it = this.outages.byCc.get(id.cc);
    else if (id.signal === "rad") it = this.rad.items[id.i];
    if (!it) return false;
    this.sel = { kind: id.signal, it };
    this._render();
    return true;
  }
  // Inspector: the first render of a selection opens the module view (app.js showModule); later
  // refreshes of the same selection only replace its HTML.
  _open(item, eyebrow, tint, body, refresh) {
    if (refresh && this._el && this._el.isConnected) { if (this._el._h !== body) { this._el._h = body; this._el.innerHTML = body; } return; }
    this._showing = true;
    try { this.hooks.show(item, eyebrow, tint, (el) => { this._el = el; el._h = body; el.innerHTML = body; }); } finally { this._showing = false; }
  }
  clearSelection() {
    if (!this.sel || this._showing) return; // showModule releases modules before showing ours
    const k = this.sel.kind;
    this.sel = null;
    if (k === "sat") this.sats.clearSelection();
  }
  _refreshSel() {
    if (!this.sel || this.sel.kind === "sat") return;
    const s = this.sel, it = s.kind === "gnss" ? this.gnss.byCell.get(s.it.h3) : s.kind === "squawk" ? this.squawks.byId.get(s.it.id) : s.kind === "outage" ? this.outages.byCc.get(s.it.cc) : null;
    if (it) { s.it = it; this._render(true); }
  }
  _render(refresh) {
    const { kind, it } = this.sel;
    const [eyebrow, tint, body] = kind === "gnss" ? this._gnssHtml(it) : kind === "squawk" ? this._squawkHtml(it) : kind === "outage" ? this._outageHtml(it) : this._radHtml(it);
    this._open({ ...it, name: it.name || it.flight || it.h3, flyAlt: FLY_ALT[kind] }, eyebrow, tint, body, refresh);
  }
  _gnssHtml(c) {
    const [label, col] = GNSS_LEVELS[c.level] || GNSS_LEVELS.low;
    const m = this.gnss.meta || {}, th = m.thresholds || {};
    const near = (m.regions || []).slice().sort((a, b) => Math.hypot(a.lat - c.lat, a.lon - c.lon) - Math.hypot(b.lat - c.lat, b.lon - c.lon))[0];
    return ["GNSS interference · " + (this.gnss.hours === 1 ? "live 1 h" : this.gnss.hours + " h"), col,
      '<h2 class="dz-title">' + esc(nf(c.pct, 1)) + "% degraded</h2>" + '<div class="sig-gauge" style="--c:' + col + '"><i style="width:' + Math.min(100, c.pct) + '%"></i></div>' +
      kv([["Assessment", esc(label)], ["Aircraft", nf(c.n)], ["Degraded", nf(c.bad) + (c.carried ? " (+" + nf(c.carried) + " carried from elsewhere, not counted)" : "")], ["Cell (H3 r" + (m.res || 3) + ")", esc(c.h3)], ["Centre", nf(c.lat, 3) + ", " + nf(c.lon, 3)],
        ["Watch region", esc(near ? near.name : "")], ["Updated", agoHtml(ts(m.updated))]]) +
      '<div class="sig-legend">' + Object.values(GNSS_LEVELS).map(([l, cc]) => '<span><i style="--c:' + cc + '"></i>' + esc(l) + "</span>").join("") + "</div>" +
      '<div class="row-note" style="padding-left:0">Degraded = ADS-B NIC &lt; ' + esc(th.nic_min || 7) + " or NACp &lt; " + esc(th.nacp_min || 8) + " or GPS reported lost. Jamming, not spoofing: a spoofed receiver still reports high integrity. Source: adsb.fi open data (all traffic in the watch regions).</div>"];
  }
  _squawkHtml(e) {
    const col = SQK_COLOR[e.code] || "#ff8a3d";
    const dur = (e.last_seen - e.first_seen) / 60;
    return ["Emergency squawk · " + (e.active ? "active" : "ended"), col,
      '<h2 class="dz-title">' + esc(e.code) + " · " + esc(e.label) + "</h2>" +
      kv([["Flight", esc(e.flight)], ["ICAO hex", esc(String(e.hex).toUpperCase())], ["Registration", esc(e.reg)], ["Type", esc([e.type, e.desc].filter(Boolean).join(" · "))],
        ["Military", e.mil ? "Yes" : "No"], ["Altitude", e.alt_ft == null ? "–" : nf(e.alt_ft) + " ft"], ["Ground speed", e.gs == null ? "–" : nf(e.gs) + " kt"],
        ["Squawk / status", esc([e.squawk, e.emergency && e.emergency !== "none" ? e.emergency : ""].filter(Boolean).join(" · "))],
        ["First seen", agoHtml(ts(e.first_seen))], ["Last seen", agoHtml(ts(e.last_seen))], ["Duration", nf(dur) + " min"], ["Position", nf(e.lat, 4) + ", " + nf(e.lon, 4)]]) +
      '<div class="row-note" style="padding-left:0">One entry per flight and code. 7500 is often a mis-set transponder; confirm with ATC / news before acting. Source: adsb.fi open data.</div>' +
      '<div class="dz-acts"><a href="https://globe.adsb.fi/?icao=' + encodeURIComponent(e.hex) + '" target="_blank" rel="noopener noreferrer">Open on globe.adsb.fi</a></div>'];
  }
  _outageHtml(c) {
    const col = OUTAGE_COLORS[c.level] || OUTAGE_COLORS.low;
    const evs = (this.outages.events || []).filter((e) => e.cc === c.cc).sort((a, b) => b.until - a.until);
    const span = (e) => fmtDateTime(e.from * 1000, { short: true }) + " → " + (e.ongoing ? "now" : fmtTime(e.until * 1000));
    const spanUtc = (e) => utcTitle(e.from * 1000) + " → " + (e.ongoing ? "now" : utcTitle(e.until * 1000));
    return ["Internet outage · IODA", col, '<h2 class="dz-title">' + esc(c.name) + "</h2>" +
      kv([["Status", c.ongoing ? "Ongoing" : "Ended"], ["Severity", esc(c.level) + " · " + nf(c.severity, 2)], ["Datasources", esc(c.datasources.join(", "))], ["Peak score", nf(c.score)],
        ["Window", '<span title="' + esc(utcTitle(c.from * 1000)) + '">' + esc(fmtDateTime(c.from * 1000, { zone: true })) + "</span> →"]]) +
      '<ul class="sig-list">' + evs.slice(0, 12).map((e) => '<li><span title="' + esc(spanUtc(e)) + '">' + esc(e.datasource) + " · " + esc(span(e)) + "</span><b>" + nf(e.score) + "</b></li>").join("") + "</ul>" +
      '<div class="row-note" style="padding-left:0">bgp / ping-slash24 = routing / probing loss (strong); gtr = Google traffic; merit-nt = network telescope (noisy, drawn faint).</div>' +
      '<div class="dz-acts"><a href="https://ioda.inetintel.cc.gatech.edu/country/' + encodeURIComponent(c.cc) + '" target="_blank" rel="noopener noreferrer">Open IODA dashboard</a></div>'];
  }
  _radHtml(s) {
    const hot = s.status === "elevated" || s.status === "alert";
    const col = hot ? "#ff4b55" : radColor(s.value);
    return ["Radiation · " + (s.source === "bfs" ? "BfS ODL" : "Safecast"), col, '<h2 class="dz-title">' + esc(nf(s.value, 3)) + " µSv/h</h2>" +
      kv([["Station", esc(s.name)], ["Status", esc(s.status)], ["7-day median", s.baseline == null ? "– (building)" : nf(s.baseline, 3) + " µSv/h"],
        ["Ratio", s.baseline ? nf(s.value / s.baseline, 2) + "×" : "–"], ["Measured", agoHtml(s.ts)], ["Country", esc(s.country)], ["Tube / CPM", esc([s.tube, s.cpm].filter((x) => x != null).join(" · "))],
        ["Cosmic part", s.cosmic == null ? "" : nf(s.cosmic, 3) + " µSv/h"], ["Position", nf(s.lat, 4) + ", " + nf(s.lon, 4)], ["ID", esc(s.id)]]) +
      '<div class="row-note" style="padding-left:0">Typical background 0.05–0.2 µSv/h. Rain briefly raises readings (radon progeny washout); an anomaly is judged against the station\'s own median.</div>'];
  }
  async showSpaceWeather() {
    let d = this.sw;
    try { d = await fetchJson("/api/signals/spaceweather", 15000); this.sw = d; } catch (e) { if (!d) return; }
    if (this.sel && this.sel.kind === "sat") this.sats.clearSelection();
    this.sel = { kind: "spacewx", it: d };
    const kp = d.kp_now ? d.kp_now[1] : null, sc = (d.scales || {}).now || {}, w = d.solar_wind || {};
    const fl = (d.flares || []).slice(-5).reverse(), fc = ((d.scales || {}).forecast || [])[0] || {};
    const body = '<h2 class="dz-title">Kp ' + (kp == null ? "–" : nf(kp, 2)) + (d.g_now ? " · G" + d.g_now + " storm" : " · quiet") + "</h2>" +
      kv([["NOAA scales now", "G" + (sc.G ?? "–") + " · S" + (sc.S ?? "–") + " · R" + (sc.R ?? "–")],
        ["Forecast", fc.date ? esc(fc.date) + " · G" + (fc.G ?? "–") + (fc.r_minor_prob ? " · R1-2 " + esc(fc.r_minor_prob) + "%" : "") + (fc.s_prob ? " · S1+ " + esc(fc.s_prob) + "%" : "") : ""],
        ["Solar wind", w.speed_kms == null ? "–" : nf(w.speed_kms) + " km/s"], ["IMF Bt / Bz", (w.bt_nt == null ? "–" : nf(w.bt_nt, 1)) + " / " + (w.bz_nt == null ? "–" : nf(w.bz_nt, 1)) + " nT"],
        ["F10.7 flux", d.f107 && d.f107.flux != null ? nf(d.f107.flux) + " sfu" : ""], ["Updated", agoHtml(ts(d.updated))]]) +
      (fl.length ? '<section class="dz-sec"><h3>X-ray flares</h3><ul class="sig-list">' + fl.map((f) => '<li><span title="' + esc(f.max ? utcTitle(f.max * 1000) : "") + '">' + esc(f.max ? fmtDateTime(f.max * 1000, { short: true }) : "–") + "</span><b>" + esc(f.class || "?") + "</b></li>").join("") + "</ul></section>" : "") +
      '<section class="dz-sec"><h3>SWPC alerts</h3>' + ((d.alerts || []).slice(0, 8).map((a) => '<div class="sig-msg" title="' + esc(a.product + "\n" + utcTitle(a.issued * 1000)) + '"><b>' + esc(fmtDateTime(a.issued * 1000, { short: true })) + " · " + esc(a.kind) + "</b>\n" + esc(a.headline) + "</div>").join("") || '<div class="dz-empty">None</div>') + "</section>" +
      '<div class="row-note" style="padding-left:0">G-scale from Kp (5 = G1 … 9 = G5). Storms degrade HF radio, GNSS accuracy and satellite drag; strong ones can also raise GNSS-interference readings.</div>';
    this._open({ name: "Space weather", lat: null, lon: null }, "Space weather · NOAA SWPC", kpColor(kp), body, false);
  }
  _showSat(it) {
    this.sel = { kind: "sat", it };
    this._renderSat(false);
  }
  _renderSat(refresh) {
    const it = this.sel.it, inf = this.sats.info, r = it.row;
    const groups = (it.groups || []).map((g) => (SAT_GROUPS[g] || [g])[0]).join(", ");
    const mm = r[4], period = 1440 / mm, a = Math.cbrt(398600.4418 * Math.pow((period * 60) / (2 * Math.PI), 2));
    const apo = a * (1 + r[5]) - 6378.137, peri = a * (1 - r[5]) - 6378.137;
    const epochAge = (Date.now() - Date.parse(it.epoch + (it.epoch.endsWith("Z") ? "" : "Z"))) / 86400e3;
    const col = (SAT_GROUPS[Object.keys(SAT_GROUPS).find((g) => (it.groups || []).includes(g))] || SAT_GROUPS.active)[1];
    const body = '<h2 class="dz-title">' + esc(it.name) + "</h2>" +
      kv([["NORAD ID", esc(it.norad)], ["Int'l designator", esc(it.intl)], ["Groups", esc(groups)],
        ["Position", inf ? nf(inf.lat, 2) + ", " + nf(inf.lon, 2) : "computing…"], ["Altitude", inf ? nf(inf.alt) + " km" : "–"], ["Speed", inf ? nf(inf.speed, 2) + " km/s" : "–"],
        ["Footprint", inf ? "radius " + nf(footprintKm(inf.alt)) + " km (≥ 10° elevation)" : "–"],
        ["Orbit", nf(peri) + " × " + nf(apo) + " km · " + nf(r[6], 1) + "° incl."], ["Period", nf(period, 1) + " min"], ["Element set", '<span title="' + esc(utcTitle(it.epoch)) + '">' + esc(fmtDateTime(it.epoch, { zone: true })) + "</span> · " + nf(epochAge, 1) + " d old"]]) +
      '<div class="row-note" style="padding-left:0">SGP4 propagation in the browser (satellite.js) from CelesTrak GP data. Solid ring: current orbit; dashed: ground track ± half an orbit; cyan circle: coverage footprint.</div>' +
      '<div class="dz-acts"><a href="https://celestrak.org/satcat/table-satcat.php?CATNR=' + encodeURIComponent(it.norad) + '" target="_blank" rel="noopener noreferrer">CelesTrak SATCAT entry</a></div>';
    this._open({ name: it.name, lat: inf ? inf.lat : null, lon: inf ? inf.lon : null, norad: it.norad, flyAlt: inf ? inf.alt * 1000 + 2.5e6 : 2e7 }, "Satellite · " + (groups || "active"), col, body, refresh);
  }
}
