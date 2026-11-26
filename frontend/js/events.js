// News events: map layer (billboards by category / status / confidence), filter state, 60 s incremental poll.
import { Layer, GLYPH_SCALE, GLYPH_FADE } from "/static/js/layers.js";
import { EVENT_CATS, eventGlyph, cesiumColor } from "/static/js/symbology.js";
import { fetchJson } from "/static/js/api.js";

const C = Cesium;
const EV_SCALE = GLYPH_SCALE, EV_FADE = GLYPH_FADE; // same distance scale / fade as the track glyphs
const COLOR = {}, COLOR_FAINT = {};
for (const k of Object.keys(EVENT_CATS)) {
  COLOR[k] = cesiumColor("--cat-" + k);
  COLOR_FAINT[k] = cesiumColor("--cat-" + k, 0.5);
}
const DEFAULT = cesiumColor("--cat-other");
const DEFAULT_FAINT = cesiumColor("--cat-other", 0.5);
export const STATUSES = ["reported", "multi-source", "sensor-detected", "sensor-corroborated", "official"];
const isHollow = (it) => it.precision === "country";
const tsOf = (s) => { const t = Date.parse(s); return isNaN(t) ? 0 : t; };

export class EventLayer extends Layer {
  constructor(scene) {
    super(scene, new C.BillboardCollection({ scene }));
    this.kind = "events";
    this.cats = new Set(Object.keys(EVENT_CATS));
    this.statuses = new Set(STATUSES);
    this.minConf = 0;
    this.replayAt = null;
    this.lastUpdated = null;
    this.filter = (it) => {
      if (!it) return false;
      if (!this.cats.has(it.category) && Object.hasOwn(EVENT_CATS, it.category)) return false;
      if (!this.statuses.has(it.status)) return false;
      if ((it.confidence || 0) < this.minConf) return false;
      if (this.replayAt && it._t0 > this.replayAt) return false;
      return true;
    };
  }
  make(it) {
    return this.col.add({ image: eventGlyph(it.category, it.status, isHollow(it), it.subcategory), scaleByDistance: EV_SCALE, translucencyByDistance: EV_FADE, disableDepthTestDistance: 0 });
  }
  upsert(list) {
    for (const it of list) {
      if (!it) continue;
      it._t0 = tsOf(it.t_start);
      if (it.updated_at && (!this.lastUpdated || it.updated_at > this.lastUpdated)) this.lastUpdated = it.updated_at;
    }
    return super.upsert(list);
  }
  key(it) { return it.lat + "|" + it.lon + "|" + it.category + "|" + it.subcategory + "|" + it.status + "|" + it.precision + "|" + it.confidence; }
  apply(p, it) {
    p.position = C.Cartesian3.fromDegrees(it.lon, it.lat, 0);
    const img = eventGlyph(it.category, it.status, isHollow(it), it.subcategory);
    if (p.image !== img) p.image = img;
    const hollow = isHollow(it);
    p.color = (hollow ? COLOR_FAINT : COLOR)[it.category] || (hollow ? DEFAULT_FAINT : DEFAULT);
    p.width = p.height = Math.round(18 + 16 * Math.min(1, Math.max(0, it.confidence || 0)));
  }
  setReplay(at) { this.replayAt = at ? at.getTime() : null; this.refilter(); }
  sortedVisible() { return this.visibleItems(); }
}

// Fetches /api/events: full load first, then only what changed since the newest updated_at.
export class EventFeed {
  constructor(layer, onData) { this.layer = layer; this.onData = onData; this.n = 0; }
  async tick() {
    const full = this.n % 10 === 0 || !this.layer.lastUpdated; // periodic full resync heals any drift
    const q = "limit=5000" + (full ? "" : "&since=" + encodeURIComponent(this.layer.lastUpdated));
    const data = await fetchJson("/api/events?" + q, 20000);
    const items = Array.isArray(data && data.items) ? data.items : [];
    const first = this.n === 0;
    this.n++;
    if (full) this.layer.sync(items); else this.layer.upsert(items);
    this.onData(items, first);
  }
}
