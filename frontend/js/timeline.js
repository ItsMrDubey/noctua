// Timeline dock: scrubber over the activity histogram with a selectable range (48 h / 7 d / 30 d / 1 y),
// Live button, play/pause with speed. Replay mode switches the accent to cyan (body.replay) and
// updates the command-bar pill.
import { $ } from "/static/js/ui.js";
import { Histogram } from "/static/js/histogram.js";
import { fmtDateTime, utcTitle, onTzChange } from "/static/js/timefmt.js";

export const RANGES = { "48h": [48 * 60, "48 h", 10], "7d": [7 * 1440, "7 d", 60], "30d": [30 * 1440, "30 d", 360], "1y": [365 * 1440, "1 y", 1440] };
const RANGE_KEY = "noctua.tl.range";
// display zone from timefmt.js; the exact UTC instant is on the label's title
const fmt = (d) => fmtDateTime(d, { zone: true });

// onChange(dateOrNull): null = back to live, Date = replay at that time. onPlay(bool) while auto-playing.
// onRange(spanMin) after the range selector changes.
export class Timeline {
  constructor(onChange, onPlay, onRange) {
    this.onChange = onChange; this.onPlay = onPlay || (() => {}); this.onRange = onRange || (() => {});
    this.slider = $("tl-slider");
    this.at = null;
    this.playing = false; this.timer = null;
    let key = "48h";
    try { const k = localStorage.getItem(RANGE_KEY); if (k && RANGES[k]) key = k; } catch (e) { /* storage disabled */ }
    this.key = key; this.span = RANGES[key][0];
    this._markRange(key);
    $("tl-speed").value = String(RANGES[key][2]);
    this.slider.min = 0; this.slider.max = this.span; this.slider.value = this.span;
    this.hist = new Histogram(this.span);
    this.slider.addEventListener("input", () => this._fromSlider());
    $("tl-live").addEventListener("click", () => this.goLive());
    $("tl-play").addEventListener("click", () => this.togglePlay());
    $("tl-range").addEventListener("click", (e) => { const b = e.target.closest("button[data-k]"); if (b && !b.disabled) this.setRange(b.dataset.k); });
    setInterval(() => this._labels(), 30000);
    onTzChange(() => { this._labels(); this._render(); });
    this._labels();
    this._render();
  }
  get label() { return RANGES[this.key][1]; }
  _markRange(key) {
    for (const b of document.querySelectorAll("#tl-range button[data-k]")) {
      const on = b.dataset.k === key;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
  }
  start() { return Date.now() - this.span * 60000; }
  _start() { return this.start(); }
  setRange(key) {
    if (!RANGES[key] || key === this.key) return;
    this.key = key; this.span = RANGES[key][0];
    try { localStorage.setItem(RANGE_KEY, key); } catch (e) { /* ignore */ }
    this._markRange(key);
    $("tl-speed").value = String(RANGES[key][2]);
    this.slider.max = this.span;
    if (this.at) {
      // keep the replay instant when it still fits, else clamp to the new start
      const v = Math.max(0, Math.round((this.at.getTime() - this._start()) / 60000));
      this.slider.value = v;
      if (this.at.getTime() < this._start()) this.at = new Date(this._start());
    } else this.slider.value = this.span;
    this.hist.setSpan(this.span);
    this._labels();
    this._render();
    this.onRange(this.span);
    if (this.at) this.onChange(this.at);
  }
  _fromSlider() {
    const v = +this.slider.value;
    if (v >= this.span) { this.goLive(); return; }
    this.at = new Date(this._start() + v * 60000);
    this._render();
    this.onChange(this.at);
  }
  // jump relative to now (negative minutes) — used by the command palette
  jump(minAgo) {
    const v = Math.max(0, this.span - minAgo);
    this.slider.value = v;
    this._fromSlider();
  }
  goLive() {
    this._stop();
    this.at = null; this.slider.value = this.span;
    this._render();
    this.onChange(null);
  }
  togglePlay() {
    if (this.playing) { this._stop(); return; }
    if (!this.at) { this.at = new Date(this._start()); this.slider.value = 0; }
    this.playing = true;
    $("tl-play").textContent = "Pause";
    $("tl-play").classList.add("on");
    this.onPlay(true);
    this.timer = setInterval(() => {
      const step = +$("tl-speed").value; // minutes of data per real second
      const next = this.at.getTime() + step * 60000;
      if (next >= Date.now()) { this.goLive(); return; }
      this.at = new Date(next);
      this.slider.value = Math.round((next - this._start()) / 60000);
      this._render();
      this.onChange(this.at);
    }, 1000);
    this._render();
    this.onChange(this.at);
  }
  _stop() {
    if (this.playing) this.onPlay(false);
    this.playing = false; clearInterval(this.timer);
    $("tl-play").textContent = "Play";
    $("tl-play").classList.remove("on");
  }
  _labels() {
    $("tl-start").textContent = fmt(new Date(this._start())) + " · −" + this.label;
    $("tl-start").title = utcTitle(this._start());
    $("tl-end").textContent = "now";
  }
  _render() {
    const ind = $("tl-mode");
    ind.textContent = this.at ? "REPLAY " + fmt(this.at) : "LIVE · real time";
    ind.title = this.at ? utcTitle(this.at) : "";
    ind.classList.toggle("replay", !!this.at);
    $("timeline").classList.toggle("replay", !!this.at);
    $("tl-live").classList.toggle("on", !this.at);
    document.body.classList.toggle("replay", !!this.at);
    $("mode-pill").classList.toggle("replay", !!this.at);
    const mt = $("mode-text");
    const txt = this.at ? (this.playing ? "REPLAY ▸" : "REPLAY") : "LIVE";
    if (mt.textContent !== txt) mt.textContent = txt;
    this.hist.setCursor(this.at ? +this.slider.value / this.span : null);
  }
}
