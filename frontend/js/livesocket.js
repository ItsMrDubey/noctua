// WebSocket client with exponential backoff. Falls back to polling (caller checks isOpen()).
// Messages are parsed on arrival but handed over in batches, at most once per animation frame
// (every 250 ms while the tab is hidden, where frames do not run): a backlog after a stall or a
// hidden tab is applied in one pass instead of one globe update per message.
import { apiToken } from "/static/js/api.js";
export class LiveSocket {
  /** onBatch(messages[]) gets every message received since the previous call, in order. */
  constructor(onBatch) {
    this.onBatch = onBatch;
    this.ws = null; this.delay = 1000; this.open = false; this.warned = false;
    this.queue = []; this.scheduled = false;
    // dev overlay (F): message count and main-thread ms spent parsing + applying
    this.stats = { n: 0, ms: 0, max: 0, bytes: 0 };
    this.connect();
  }
  isOpen() { return this.open; }
  connect() {
    let ws;
    try {
      ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/live" + (apiToken ? "?token=" + encodeURIComponent(apiToken) : ""));
    } catch (e) { this._retry(); return; }
    this.ws = ws;
    ws.onopen = () => { this.open = true; this.delay = 1000; this.warned = false; };
    ws.onmessage = (ev) => {
      const t = performance.now();
      try { this.queue.push(JSON.parse(ev.data)); } catch (e) { console.error("ws message", e); }
      this.stats.n++; this.stats.bytes += ev.data.length || 0; this.stats.ms += performance.now() - t;
      this._schedule();
    };
    ws.onclose = () => {
      this.open = false;
      if (!this.warned) { this.warned = true; console.warn("live websocket unavailable; polling instead (retrying quietly)"); }
      this._retry();
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  }
  _schedule() {
    if (this.scheduled) return;
    this.scheduled = true;
    const run = () => {
      this.scheduled = false;
      const batch = this.queue;
      if (!batch.length) return;
      this.queue = [];
      const t = performance.now();
      try { this.onBatch(batch); } catch (e) { console.error("ws batch", e); }
      const d = performance.now() - t;
      this.stats.ms += d; this.stats.max = Math.max(this.stats.max, d);
    };
    if (document.hidden) { setTimeout(run, 250); return; }
    // a tab hidden after scheduling stops rAF: the timer bounds the backlog regardless
    const raf = requestAnimationFrame(run);
    setTimeout(() => { cancelAnimationFrame(raf); if (this.scheduled) run(); }, 1000);
  }
  _retry() {
    const d = this.delay;
    this.delay = Math.min(30000, this.delay * 2);
    setTimeout(() => this.connect(), d);
  }
}
