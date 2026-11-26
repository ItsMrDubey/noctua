// Globe declutter: level of detail, aggregation, clustering and a priority budget for the live
// layers, plus overlap-free labels across all label layers. Rules live in lod.js (pure, tested);
// this module applies them to the Cesium layers.
//
//   fires     zoomed out (> 1,500 km): heat cells on a stable lat/lon grid (count -> size, summed
//             FRP -> colour) + war-likely fires (conflict score >= threshold) individually
//   ships     > 800 km: density cells + notable ships individually (military, watchlist /
//             sanctioned / loitering-dark alerts, fast movers in conflict theatres)
//   aircraft  > 2,500 km: screen clusters, the most important aircraft of each drawn with a
//             "+N" badge (high-priority / emergency / watchlist always drawn)
//   events    every height: screen clusters (stacked city-centre reports too); the most
//             important event of each cluster (status, confidence, severity, recency by event
//             time) is drawn with a "+N" badge coloured by the most severe category; the top few
//             get title labels. Click a badge: zoom to the cluster, or fan it out (spiderfy)
//             when its members share one spot
//   quakes    > 3,000 km: M4.5+ only
//   budget    individually drawn markers on screen are capped per height band (lod.js BUDGET),
//             filled by priority with a minimum share per layer; the selection, watchlist
//             tracks and high-priority aircraft are always drawn. Everything gated out is
//             still counted in a cell / badge and still listed in the panels.
//
// Recomputed on camera moveEnd (and when a zoom step changes mid-flight), on data / filter
// changes (throttled: tracks every 2.5 s), never per frame. Mode "auto" | "off" persists in
// localStorage (noctua.declutter); "off" restores the plain rendering.
// Layer-tree wiring: call ctx.declutter.setMode("auto" | "off"), read .mode, listen via
// .onModeChange((mode) => {}). Until the tree has a row, a small "LOD" button sits in the HUD.
import { bandOf, gridStep, aggregate, mergeClose, allocate, placeLabels, spiderOffsets, topCategory, eventPriority, shipPriority, aircraftPriority, firePriority, BUDGET, LOD, LABEL_CAP, EVENT_LABELS } from "/static/js/lod.js";
import { catColor, COLORS } from "/static/js/symbology.js";
import { evTitle } from "/static/js/ui.js";

const C = Cesium;
const STORE_KEY = "noctua.declutter";
const FONT = "600 11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";
const BLACK = C.Color.BLACK.withAlpha(0.85);
const FIRE_CELL = Array.from({ length: 11 }, (_, i) => C.Color.fromHsl(0.13 - 0.13 * (i / 10), 1, 0.52 - 0.12 * (i / 10), 0.42 + 0.33 * (i / 10)));
const SHIP_CELL = Array.from({ length: 11 }, (_, i) => C.Color.fromCssColorString(COLORS.ship).withAlpha(0.22 + 0.4 * (i / 10)));
const CELL_OUT = C.Color.BLACK.withAlpha(0.35);
const BADGE_OFF = new C.Cartesian2(14, -14);
const BADGE_EYE = new C.Cartesian3(0, 0, -50); // drawn just in front of the glyph it annotates
const LABEL_OFF = new C.Cartesian2(18, 4); // right of the glyph, clear of its "+N" badge
const ZERO2 = new C.Cartesian2(0, 0);
const CELL_FADE = new C.NearFarScalar(2e6, 1.0, 3e7, 0.8);
const TRACK_DELAY = 2500, DATA_DELAY = 300, VIEW_DELAY = 120;
const AIR_BADGE = COLORS.air, AIR_HOT = COLORS.hp;
const fmtN = (n) => (n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));

export class Declutter {
  // ctx: the app context (byKind, viewer, watch, refLayers, infra, mods.sig, tracklist, fps, fireScore)
  constructor(ctx) {
    this.ctx = ctx; this.viewer = ctx.viewer; this.scene = ctx.viewer.scene;
    this.L = ctx.byKind;
    let saved = "auto";
    try { saved = localStorage.getItem(STORE_KEY) || "auto"; } catch (e) { /* storage disabled */ }
    this.mode = saved === "off" ? "off" : "auto";
    this.listeners = [];
    const P = this.scene.primitives;
    this.fireCells = P.add(new C.PointPrimitiveCollection());
    this.shipCells = P.add(new C.PointPrimitiveCollection());
    P.lowerToBottom(this.shipCells); P.lowerToBottom(this.fireCells); // under every glyph layer
    this.badges = P.add(new C.BillboardCollection({ scene: this.scene }));
    this.evLabels = P.add(new C.LabelCollection());
    this.imgs = new Map();
    this.allow = { fires: new Set(), ships: new Set(), aircraft: new Set(), events: new Set() };
    this.agg = { fires: false, ships: false, aircraft: false, events: false, quakes: false };
    this.step = {}; this.dirty = new Set(["fires", "ships", "aircraft", "events"]);
    this.stale = { fires: true, ships: true }; this.cellAt = { fires: 0, ships: 0 };
    this.hot = { fires: null, ships: null }; this.thr = null; // cached notable candidates
    this.spider = null; this.selected = null; this.timer = 0; this.due = Infinity; this.stats = {};
    this.hidden = new Set(); // labels this module hid (label overlap)
    this.band = null;
    this._installGates();
    this._hookModules();
    const cam = this.viewer.camera;
    cam.moveEnd.addEventListener(() => this.schedule(null, VIEW_DELAY));
    cam.moveStart.addEventListener(() => this.unspider());
    cam.changed.addEventListener(() => { if (this._stepsChanged()) this.schedule(null, 60); });
    this.schedule(null, 50);
  }

  // ---------------------------------------------------------------- wiring
  _installGates() {
    const { fires, ships, aircraft, events, quakes } = this.L;
    const sel = (it) => it.id === this.selected;
    fires.gate = (it) => !this.agg.fires || sel(it) || this.allow.fires.has(it.id);
    ships.gate = (it) => !this.agg.ships || sel(it) || this.allow.ships.has(it.id) || this._watched(it.id);
    aircraft.gate = (it) => !this.agg.aircraft || sel(it) || this.allow.aircraft.has(it.id) || !!it.high_priority || this._watched(it.id);
    events.gate = (it) => !this.agg.events || sel(it) || this.allow.events.has(it.id) || (!!this.spider && this.spider.set.has(it.id));
    quakes.gate = (it) => !this.agg.quakes || sel(it) || (it.mag || 0) >= LOD.quakes.minMag;
    for (const k of ["fires", "ships", "aircraft", "events", "quakes"]) {
      const l = this.L[k];
      l.onDirty = () => { this.dirty.add(k); this.schedule(k, k === "ships" || k === "aircraft" ? TRACK_DELAY : DATA_DELAY); };
    }
  }
  _hookModules() {
    const ctx = this.ctx;
    // every selection path in app.js (select / clearSelection / showRef / showModule) ends in
    // tracklist.setSelected(id | null): keep the selection drawn whatever its zoom priority
    const tl = ctx.tracklist;
    if (tl && tl.setSelected) {
      const base = tl.setSelected.bind(tl);
      tl.setSelected = (id) => { base(id); this.select(id); };
    }
    // watchlist matches change which tracks are always drawn
    const w = ctx.watch;
    if (w && w.sync) {
      const base = w.sync.bind(w);
      w.sync = () => { base(); this.dirty.add("ships"); this.dirty.add("aircraft"); this.schedule("ships", DATA_DELAY); };
    }
    // modules that rebuild their label pools: drop our overlap hides for that pool, re-place
    const relabelOf = (obj, pool) => {
      if (!obj || !obj.relabel) return;
      const base = obj.relabel.bind(obj);
      obj.relabel = (...a) => { this._forget(pool); const r = base(...a); this.labelsSoon(); return r; };
    };
    if (ctx.refLayers) relabelOf(ctx.refLayers, ctx.refLayers.pool);
    if (ctx.infra) relabelOf(ctx.infra, ctx.infra.pool);
    // dev overlay (F): band, budget use, cells / clusters
    if (ctx.fps) {
      const prev = ctx.fps.extra;
      ctx.fps.extra = () => (prev ? prev() : "") + "\nLOD " + (this.mode === "off" ? "off" : (this.band || "-") + " · " + (this.stats.drawn || 0) + "/" + (this.stats.budget === Infinity ? "∞" : this.stats.budget || 0) +
        " drawn · " + (this.stats.cells || 0) + " cells · " + (this.stats.clusters || 0) + " clusters · " + (this.stats.labels || 0) + " labels · " + (this.stats.ms || 0).toFixed(1) + " ms");
    }
  }
  _watched(id) {
    const w = this.ctx.watch;
    if (!w) return false;
    if (w.active && w.active.has(id)) return true;
    const r = w.recent && w.recent.get(id); // loitering near a cable / dark alert in the last 6 h
    return !!(r && Date.now() - Date.parse(r.ts) < 6 * 36e5);
  }
  collections() { return [[this.fireCells, "lod:fire-cells"], [this.shipCells, "lod:ship-cells"], [this.badges, "lod:badges"], [this.evLabels, "lod:event-labels"]]; }

  setMode(mode) {
    this.mode = mode === "off" ? "off" : "auto";
    try { localStorage.setItem(STORE_KEY, this.mode); } catch (e) { /* storage disabled */ }
    for (const k of Object.keys(this.L)) this.dirty.add(k);
    this.unspider();
    this.update();
    for (const fn of this.listeners) fn(this.mode);
  }
  onModeChange(fn) { this.listeners.push(fn); }
  select(id) {
    const prev = this.selected;
    this.selected = id || null;
    for (const k of ["fires", "ships", "aircraft", "events", "quakes"]) {
      const l = this.L[k];
      if (!this.agg[k]) continue; // nothing gated (and close-up tracks may be drawn as 3D models)
      for (const x of [prev, this.selected]) {
        const p = x && l.map.get(x);
        if (p) { const s = l.shown(l.items.get(x)); if (p.show !== s) p.show = s; }
      }
    }
    this.scene.requestRender();
  }

  // ---------------------------------------------------------------- scheduling
  schedule(kind, delay) {
    const at = performance.now() + delay;
    if (this.timer && at >= this.due) return;
    clearTimeout(this.timer);
    this.due = at;
    this.timer = setTimeout(() => { this.timer = 0; this.due = Infinity; this.update(); }, delay);
  }
  _height() { return this.viewer.camera.positionCartographic.height; }
  _canvasH() { return this.scene.canvas.clientHeight || 900; }
  _fov() { const f = this.viewer.camera.frustum; return f.fovy || f.fov || 1.0472; }
  _stepsChanged() {
    if (this.mode === "off") return false;
    const h = this._height(), fov = this._fov(), ch = this._canvasH();
    const ev = gridStep(h, LOD.events.cellPx, fov, ch);
    if (ev !== this.step.events) return true;
    for (const k of ["fires", "ships", "aircraft"]) if ((h > LOD[k].aggregateAbove) !== this.agg[k]) return true;
    return bandOf(h) !== this.band;
  }

  // ---------------------------------------------------------------- the pass
  update() {
    const t0 = performance.now();
    const on = this.mode === "auto";
    const h = this._height(), fov = this._fov(), ch = this._canvasH();
    const band = bandOf(h);
    const prevAgg = { ...this.agg };
    this.band = band;
    for (const k of ["fires", "ships", "aircraft"]) this.agg[k] = on && h > LOD[k].aggregateAbove;
    this.agg.events = on;
    this.agg.quakes = on && h > LOD.quakes.aggregateAbove;
    const view = this._viewTest();
    const cands = [];
    const stats = { cells: 0, clusters: 0 };

    // fires: heat cells + war-likely candidates; ships: density cells + notable candidates
    const thr = this._fireThr();
    if (thr !== this.thr) { this.thr = thr; this.dirty.add("fires"); }
    for (const k of ["fires", "ships"]) {
      const L = this.L[k], col = k === "fires" ? this.fireCells : this.shipCells;
      if (this.dirty.has(k)) { this.stale[k] = true; this.hot[k] = null; }
      if (this.agg[k]) {
        const step = gridStep(h, LOD[k].cellPx, fov, ch);
        // ship deltas arrive every second or two: their cells are rebuilt at most every 15 s
        if (step !== this.step[k] || !prevAgg[k] || (this.stale[k] && (k === "fires" || t0 - this.cellAt[k] > 15000))) {
          this.step[k] = step; this.stale[k] = false; this.cellAt[k] = t0;
          const list = [];
          for (const it of L.items.values()) if (L.filter(it)) list.push(it);
          if (k === "fires") this._drawCells(col, k, aggregate(list, step, { weight: (it) => it.frp || 0 }), step, FIRE_CELL, (c, max) => Math.log10(1 + c.sum) / Math.log10(1 + max));
          else this._drawCells(col, k, aggregate(list, step), step, SHIP_CELL, (c, max) => Math.log10(1 + c.n) / Math.log10(1 + max));
        }
        if (!this.hot[k]) {
          this.hot[k] = [];
          for (const it of L.items.values()) {
            const p = k === "fires" ? firePriority(it, thr) : shipPriority(it, false);
            if (p > 0 && L.filter(it)) this.hot[k].push([it, p]);
          }
        }
        for (const [it, p] of this.hot[k]) if (view(it.lat, it.lon)) cands.push({ layer: k, id: it.id, prio: p });
      } else if (col.length) { col.removeAll(); this.step[k] = null; }
      col.show = this.agg[k] && L.show;
      stats.cells += this.agg[k] ? col.length : 0;
    }

    // aircraft + events: screen clusters (one representative each)
    const clusters = [];
    const now = Date.now();
    const air = this.L.aircraft;
    if (this.agg.aircraft) {
      const step = this.step.aircraft = gridStep(h, LOD.aircraft.cellPx, fov, ch);
      const list = [];
      for (const it of air.items.values()) if (air.filter(it)) list.push(it);
      const prio = (it) => { const p = aircraftPriority(it, this._watched(it.id)); return p === Infinity ? 1e6 : p; };
      for (const c of mergeClose(aggregate(list, step, { prio, members: true }), step * 0.7)) {
        if (!view(c.lat, c.lon)) continue;
        clusters.push({ kind: "aircraft", c });
        cands.push({ layer: "aircraft", id: c.top.id, prio: c.topPrio >= 1e6 ? Infinity : c.topPrio });
      }
    }
    const events = this.L.events;
    if (this.agg.events) {
      const step = this.step.events = gridStep(h, LOD.events.cellPx, fov, ch);
      const list = [];
      for (const it of events.items.values()) if (events.filter(it)) list.push(it);
      for (const c of mergeClose(aggregate(list, step, { prio: (it) => eventPriority(it, now), members: true }), step * 0.7)) {
        if (!view(c.lat, c.lon)) continue;
        clusters.push({ kind: "events", c });
        cands.push({ layer: "events", id: c.top.id, prio: c.topPrio });
      }
    } else this.step.events = null;
    stats.clusters = clusters.filter((x) => x.c.n > 1).length;

    // budget
    const budget = BUDGET[band];
    const alloc = allocate(cands, budget);
    const changed = {};
    for (const k of Object.keys(this.allow)) {
      const a = alloc.get(k) || new Set(), b = this.allow[k];
      changed[k] = a.size !== b.size || [...a].some((x) => !b.has(x));
      this.allow[k] = a;
    }
    stats.drawn = cands.filter((c) => (alloc.get(c.layer) || new Set()).has(c.id)).length;
    stats.budget = budget;

    // apply: only layers whose gate can have changed (regate re-shows billboards the 3D model
    // layer hid, so aircraft / ships are left alone while neither is aggregated, or with the same
    // allow set; data changes were gated on arrival)
    for (const k of ["fires", "ships", "aircraft"]) if (this.agg[k] !== prevAgg[k] || (this.agg[k] && changed[k])) this.L[k].regate();
    events.regate();
    if (this.agg.quakes !== prevAgg.quakes) this.L.quakes.regate();
    this._drawBadges(clusters, h);
    this._drawEventLabels(clusters, band);
    this.dirty.clear();
    this.stats = { ...stats, ms: performance.now() - t0 };
    this.placeLabels();
    this.stats.ms = performance.now() - t0;
    this.scene.requestRender();
  }

  _fireThr() {
    const fs = this.ctx.fireScore;
    return fs && fs.state && Number.isFinite(fs.state.thr) ? fs.state.thr : 50;
  }
  // in-view test for lat/lon: inside the (padded) view rectangle and on the camera's side of
  // the globe
  _viewTest() {
    const cam = this.viewer.camera, r = cam.computeViewRectangle(this.scene.globe.ellipsoid);
    const flat = this.scene.mode !== C.SceneMode.SCENE3D; // 2D / Columbus: no far side
    const cp = cam.positionWC, R = 6378137, d = C.Cartesian3.magnitude(cp);
    const ux = cp.x / d, uy = cp.y / d, uz = cp.z / d, horizon = R / d - 0.02;
    const deg = C.Math.toDegrees, rad = Math.PI / 180;
    let w = -180, s = -90, e = 180, n = 90, full = true;
    if (r) {
      w = deg(r.west); e = deg(r.east); s = deg(r.south); n = deg(r.north);
      const span = e >= w ? e - w : e - w + 360;
      const pl = span * 0.15, pa = (n - s) * 0.15;
      full = span >= 300;
      w -= pl; e += pl; s -= pa; n += pa;
    }
    return (lat, lon) => {
      if (!full) {
        if (lat < s || lat > n) return false;
        const x = ((lon - w) % 360 + 360) % 360, span = ((e - w) % 360 + 360) % 360;
        if (x > span) return false;
      }
      if (flat) return true;
      const cl = Math.cos(lat * rad);
      return cl * Math.cos(lon * rad) * ux + cl * Math.sin(lon * rad) * uy + Math.sin(lat * rad) * uz > horizon;
    };
  }

  // ---------------------------------------------------------------- drawing
  _drawCells(col, kind, cells, step, ramp, intensity) {
    col.removeAll();
    let max = 0;
    for (const c of cells) max = Math.max(max, kind === "fires" ? c.sum : c.n);
    const px = LOD[kind].cellPx;
    for (const c of cells) {
      const t = max > 0 ? Math.min(1, Math.max(0, intensity(c, max))) : 0;
      const p = col.add({
        position: C.Cartesian3.fromDegrees(c.lon, c.lat), pixelSize: Math.min(px - 4, 4 + 4 * Math.log10(c.n)),
        color: ramp[Math.round(t * 10)], outlineColor: CELL_OUT, outlineWidth: 1, translucencyByDistance: CELL_FADE,
      });
      p.id = { lod: "cell", kind, n: c.n, sum: c.sum, w: c.w, s: c.s, e: c.e, nn: c.nn, step };
    }
  }
  _img(text, color, big) {
    const key = text + "|" + color + "|" + (big ? 1 : 0);
    let url = this.imgs.get(key);
    if (url) return url;
    const fs = big ? 12 : 9, hgt = big ? 19 : 13;
    const cv = document.createElement("canvas"), g = cv.getContext("2d");
    g.font = "700 " + fs + "px Consolas, monospace";
    const w = Math.max(hgt, Math.ceil(g.measureText(text).width) + (big ? 12 : 8));
    cv.width = w + 2; cv.height = hgt + 2;
    g.font = "700 " + fs + "px Consolas, monospace";
    const r = hgt / 2;
    g.beginPath(); g.moveTo(1 + r, 1); g.arcTo(1 + w, 1, 1 + w, 1 + hgt, r); g.arcTo(1 + w, 1 + hgt, 1, 1 + hgt, r); g.arcTo(1, 1 + hgt, 1, 1, r); g.arcTo(1, 1, 1 + w, 1, r); g.closePath();
    g.fillStyle = "rgba(5,8,13,0.88)"; g.fill();
    g.lineWidth = big ? 2 : 1.5; g.strokeStyle = color; g.stroke();
    g.fillStyle = color; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(text, 1 + w / 2, 1 + hgt / 2 + 0.5);
    url = cv.toDataURL("image/png");
    if (this.imgs.size > 400) this.imgs.clear();
    this.imgs.set(key, url);
    return url;
  }
  _drawBadges(clusters) {
    const B = this.badges;
    B.removeAll();
    this.badgeIndex = [];
    for (const { kind, c } of clusters) {
      const L = this.L[kind];
      const repShown = this.allow[kind].has(c.top.id) || (kind === "aircraft" && (c.top.high_priority || this._watched(c.top.id)));
      if (c.n <= 1 && repShown) continue;
      if (!L.show) continue;
      const members = c.ids.map((id) => L.items.get(id)).filter(Boolean);
      const color = kind === "events" ? catColor(topCategory(members.map((m) => m.category))) : members.some((m) => m.high_priority) ? AIR_HOT : AIR_BADGE;
      const spiderHere = this.spider && this.spider.key === kind + ":" + c.top.id;
      if (spiderHere) continue;
      const id = { lod: "cluster", kind, ids: c.ids, top: c.top.id, w: c.w, s: c.s, e: c.e, nn: c.nn, n: c.n, lat: c.lat, lon: c.lon };
      let b;
      if (repShown) {
        const p = L.positionOf(c.top.id);
        if (!p) continue;
        b = B.add({ position: p, image: this._img("+" + fmtN(c.n - 1), color, false), pixelOffset: BADGE_OFF, eyeOffset: BADGE_EYE });
      } else {
        b = B.add({ position: C.Cartesian3.fromDegrees(c.lon, c.lat, kind === "aircraft" ? 9000 : 0), image: this._img(fmtN(c.n), color, true) });
      }
      b.id = id;
    }
  }
  _drawEventLabels(clusters, band) {
    const lb = this.evLabels;
    lb.removeAll();
    if (this.mode === "off" || !this.L.events.show) return;
    const reps = clusters.filter((x) => x.kind === "events" && this.allow.events.has(x.c.top.id)).sort((a, b) => b.c.topPrio - a.c.topPrio).slice(0, EVENT_LABELS[band]);
    for (const { c } of reps) {
      const it = c.top, p = this.L.events.positionOf(it.id);
      const t = evTitle(it);
      if (!p || !t) continue;
      const l = lb.add({ position: p, text: t.length > 38 ? t.slice(0, 37) + "…" : t, font: FONT, fillColor: C.Color.fromCssColorString(catColor(it.category)), outlineColor: BLACK, outlineWidth: 3,
        style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER, pixelOffset: LABEL_OFF, scale: 0.92 });
      l.id = { lod: "label", kind: "events", id: it.id }; // clicking the title selects the event
      l._prio = 70 + c.topPrio / 10;
    }
  }

  // ---------------------------------------------------------------- labels: no overlap
  labelsSoon() { clearTimeout(this._lt); this._lt = setTimeout(() => this.placeLabels(), 30); }
  _forget(pool) { for (const l of this.hidden) if (l._col === pool) this.hidden.delete(l); }
  _labelSets() {
    const ctx = this.ctx, out = [];
    const add = (col, prio) => { if (col) out.push([col, prio]); };
    if (ctx.watch) add(ctx.watch.labels, 95);
    const sig = ctx.mods && ctx.mods.sig;
    if (sig) { add(sig.squawks.lb, 90); add(sig.outages.lb, 60); }
    add(this.evLabels, null); // own priorities (70+)
    if (ctx.refLayers) { add(ctx.refLayers.chokeLabels, 55); add(ctx.refLayers.pool, 40); add(ctx.refLayers.countryLabels, 20); }
    if (ctx.infra) add(ctx.infra.pool, 38);
    return out;
  }
  placeLabels() {
    // restore what we hid last time (labels removed since are skipped)
    for (const l of this.hidden) if (l._col && l._col.contains(l)) l.show = true;
    this.hidden.clear();
    if (this.mode === "off") { this.stats.labels = null; this.scene.requestRender(); return; }
    const scene = this.scene, cam = this.viewer.camera, cp = cam.positionWC;
    const occ = scene.mode === C.SceneMode.SCENE3D ? new C.EllipsoidalOccluder(scene.globe.ellipsoid, cp) : { isPointVisible: () => true };
    const W = scene.canvas.clientWidth, H = scene.canvas.clientHeight;
    const boxes = [], refs = [], tmp = new C.Cartesian2();
    for (const [col, prio] of this._labelSets()) {
      if (!col.show) continue;
      for (let i = 0; i < col.length; i++) {
        const l = col.get(i);
        if (!l.show || !l.position || !l.text) continue;
        l._col = col;
        if (!occ.isPointVisible(l.position)) continue;
        const d = C.Cartesian3.distance(cp, l.position), ddc = l.distanceDisplayCondition;
        if (ddc && (d < ddc.near || d > ddc.far)) continue;
        const tr = l.translucencyByDistance;
        if (tr && tr.nearValue + (tr.farValue - tr.nearValue) * Math.min(1, Math.max(0, (d - tr.near) / (tr.far - tr.near))) < 0.1) continue;
        const w = C.SceneTransforms.worldToWindowCoordinates(scene, l.position, tmp);
        if (!w || w.x < -200 || w.y < -40 || w.x > W + 40 || w.y > H + 40) continue;
        const fs = (parseFloat(/(\d+(?:\.\d+)?)px/.exec(l.font || "") ? /(\d+(?:\.\d+)?)px/.exec(l.font)[1] : 11) || 11) * (l.scale || 1);
        const bw = String(l.text).length * fs * 0.62 + 4, bh = fs * 1.25;
        const off = l.pixelOffset || ZERO2;
        const ho = l.horizontalOrigin, vo = l.verticalOrigin;
        const x = w.x + off.x - (ho === C.HorizontalOrigin.CENTER ? bw / 2 : ho === C.HorizontalOrigin.RIGHT ? bw : 0);
        const y = w.y + off.y - (vo === C.VerticalOrigin.CENTER ? bh / 2 : vo === C.VerticalOrigin.TOP ? 0 : bh * 0.85);
        boxes.push({ x, y, w: bw, h: bh, prio: prio == null ? (l._prio || 70) : prio - i * 1e-4 });
        refs.push(l);
      }
    }
    // the glyphs that event labels annotate (and cluster badges) stay uncovered
    const fixed = [];
    const ev = this.L.events;
    if (ev.show) for (const id of this.allow.events) {
      const p = ev.map.get(id);
      const w = p && p.show && occ.isPointVisible(p.position) ? C.SceneTransforms.worldToWindowCoordinates(scene, p.position, tmp) : null;
      if (w) { const r = (p.width || 24) * 0.45; fixed.push({ x: w.x - r, y: w.y - r, w: 2 * r, h: 2 * r }); }
    }
    for (let i = 0; i < this.badges.length; i++) {
      const b = this.badges.get(i), w = occ.isPointVisible(b.position) ? C.SceneTransforms.worldToWindowCoordinates(scene, b.position, tmp) : null;
      if (w) { const o = b.pixelOffset || ZERO2; fixed.push({ x: w.x + o.x - 10, y: w.y + o.y - 8, w: 20, h: 16 }); }
    }
    const keep = new Set(placeLabels(boxes, LABEL_CAP[this.band || "world"], 2, 64, fixed));
    for (let i = 0; i < refs.length; i++) if (!keep.has(i)) { refs[i].show = false; this.hidden.add(refs[i]); }
    this.stats.labels = keep.size;
    scene.requestRender();
  }

  // ---------------------------------------------------------------- picking
  // a badge wins over the glyph under it only when the click is on the badge itself
  nearBadge(picked, pos) {
    const b = picked.primitive;
    if (!b || !b.position) return false;
    const w = C.SceneTransforms.worldToWindowCoordinates(this.scene, b.position);
    if (!w) return false;
    const o = b.pixelOffset || ZERO2;
    return Math.hypot(w.x + o.x - pos.x, w.y + o.y - pos.y) < 11;
  }
  pick(id) {
    if (!id) return false;
    const span = Math.max(id.e - id.w, id.nn - id.s);
    if (id.lod === "cluster" && id.kind === "events" && (span < 0.02 || this._height() < 2.5e5)) { this.spiderfy(id); return true; }
    const tip = document.getElementById("pick-tip");
    if (tip && id.lod === "cell") {
      tip.textContent = (id.kind === "fires" ? id.n.toLocaleString() + " fire detections · " + Math.round(id.sum).toLocaleString() + " MW total FRP" : id.n.toLocaleString() + " ships") + " · zooming in";
      tip.classList.remove("hidden");
      clearTimeout(tip._t); tip._t = setTimeout(() => tip.classList.add("hidden"), 4000);
    }
    // zoom to the members' extent (at least a cell), below the aggregation height
    const pad = Math.max(span * 0.25, id.lod === "cell" ? id.step * 0.3 : 0.15);
    let w = id.w - pad, e = id.e + pad, s = Math.max(-89, id.s - pad), n = Math.min(89, id.nn + pad);
    if (id.lod === "cell") { const half = id.step / 2; const la = (s + n) / 2, lo = (w + e) / 2; w = Math.min(w, lo - half); e = Math.max(e, lo + half); s = Math.min(s, la - half); n = Math.max(n, la + half); }
    this.viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(w, s, e, n), duration: 1.2 });
    return true;
  }
  // fan the members of a same-spot event cluster out around it (until the camera moves)
  spiderfy(id) {
    this.unspider();
    const L = this.L[id.kind];
    const ids = id.ids.filter((x) => L.map.has(x));
    const offs = spiderOffsets(ids.length);
    ids.forEach((x, i) => { L.map.get(x).pixelOffset = new C.Cartesian2(offs[i][0], offs[i][1]); });
    this.spider = { key: id.kind + ":" + id.top, kind: id.kind, ids, set: new Set(ids) };
    L.regate();
    this.badges.removeAll(); this.update();
  }
  unspider() {
    if (!this.spider) return;
    const L = this.L[this.spider.kind];
    for (const x of this.spider.ids) { const p = L.map.get(x); if (p) p.pixelOffset = ZERO2; }
    this.spider = null;
    L.regate();
    this.schedule(null, VIEW_DELAY);
  }
}

// Mounts the HUD toggle (until the layer tree has a row for it) and returns the instance.
export function installDeclutter(ctx) {
  const d = new Declutter(ctx);
  const row = document.querySelector("#hud .hud-btns");
  if (row) {
    const b = document.createElement("button");
    b.className = "ghost"; b.id = "hud-lod";
    b.textContent = "LOD";
    const sync = () => {
      b.classList.toggle("on", d.mode === "auto");
      b.title = d.mode === "auto" ? "Declutter on: dense layers aggregate and glyphs cluster when zoomed out (click: draw every marker)" : "Declutter off: every marker drawn (click: aggregate and cluster when zoomed out)";
    };
    b.addEventListener("click", () => d.setMode(d.mode === "auto" ? "off" : "auto"));
    d.onModeChange(sync);
    row.appendChild(b);
    sync();
  }
  return d;
}
