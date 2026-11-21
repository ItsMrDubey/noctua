// WebSocket client with exponential backoff. Falls back to polling (caller checks isOpen()).
import { apiToken } from "/static/js/api.js";
export class LiveSocket {
  constructor(onMessage) {
    this.onMessage = onMessage;
    this.ws = null; this.delay = 1000; this.open = false; this.warned = false;
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
      try { this.onMessage(JSON.parse(ev.data)); } catch (e) { console.error("ws message", e); }
    };
    ws.onclose = () => {
      this.open = false;
      if (!this.warned) { this.warned = true; console.warn("live websocket unavailable; polling instead (retrying quietly)"); }
      this._retry();
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  }
  _retry() {
    const d = this.delay;
    this.delay = Math.min(30000, this.delay * 2);
    setTimeout(() => this.connect(), d);
  }
}
