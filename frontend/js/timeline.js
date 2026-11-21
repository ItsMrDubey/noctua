// Timeline dock: 48 h scrubber over the activity histogram, Live button, play/pause with speed.
// Replay mode re-tints the whole UI amber (body.replay) and updates the command-bar pill.
import { $ } from "/static/js/ui.js";
import { Histogram } from "/static/js/histogram.js";

const SPAN_MIN = 48 * 60;
const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + "Z";

// onChange(dateOrNull): null = back to live, Date = replay at that time. onPlay(bool) while auto-playing.
export class Timeline {
  constructor(onChange, onPlay) {
    this.onChange = onChange; this.onPlay = onPlay || (() => {});
    this.slider = $("tl-slider");
    this.at = null;
    this.playing = false; this.timer = null;
    this.slider.min = 0; this.slider.max = SPAN_MIN; this.slider.value = SPAN_MIN;
    this.hist = new Histogram(SPAN_MIN);
    this.slider.addEventListener("input", () => this._fromSlider());
    $("tl-live").addEventListener("click", () => this.goLive());
    $("tl-play").addEventListener("click", () => this.togglePlay());
    setInterval(() => this._labels(), 30000);
    this._labels();
    this._render();
  }
  _start() { return Date.now() - SPAN_MIN * 60000; }
  _fromSlider() {
    const v = +this.slider.value;
    if (v >= SPAN_MIN) { this.goLive(); return; }
    this.at = new Date(this._start() + v * 60000);
    this._render();
    this.onChange(this.at);
  }
  // jump relative to now (negative minutes) — used by the command palette
  jump(minAgo) {
    const v = Math.max(0, SPAN_MIN - minAgo);
    this.slider.value = v;
    this._fromSlider();
  }
  goLive() {
    this._stop();
    this.at = null; this.slider.value = SPAN_MIN;
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
    $("tl-start").textContent = fmt(new Date(this._start())) + " · −48 h";
    $("tl-end").textContent = "now";
  }
  _render() {
    const ind = $("tl-mode");
    ind.textContent = this.at ? "REPLAY " + fmt(this.at) : "LIVE · real time";
    ind.classList.toggle("replay", !!this.at);
    $("timeline").classList.toggle("replay", !!this.at);
    $("tl-live").classList.toggle("on", !this.at);
    document.body.classList.toggle("replay", !!this.at);
    $("mode-pill").classList.toggle("replay", !!this.at);
    const mt = $("mode-text");
    const txt = this.at ? (this.playing ? "REPLAY ▸" : "REPLAY") : "LIVE";
    if (mt.textContent !== txt) mt.textContent = txt;
    this.hist.setCursor(this.at ? +this.slider.value / SPAN_MIN : null);
  }
}
