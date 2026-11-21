// Bottom timeline bar: 48 h range slider, Live button, play/pause with speed.
import { $ } from "/static/js/ui.js";

const SPAN_MIN = 48 * 60;
const pad = (n) => String(n).padStart(2, "0");
const fmt = (d) => d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + " UTC";

// onChange(dateOrNull): null = back to live, Date = replay at that time.
export class Timeline {
  constructor(onChange) {
    this.onChange = onChange;
    this.slider = $("tl-slider");
    this.at = null; // Date in replay, null when live
    this.playing = false; this.timer = null;
    this.slider.min = 0; this.slider.max = SPAN_MIN; this.slider.value = SPAN_MIN;
    this.slider.addEventListener("input", () => this._fromSlider());
    $("tl-live").addEventListener("click", () => this.goLive());
    $("tl-play").addEventListener("click", () => this.togglePlay());
    setInterval(() => this._labels(), 30000);
    this._labels();
  }
  _start() { return Date.now() - SPAN_MIN * 60000; }
  _fromSlider() {
    const v = +this.slider.value;
    if (v >= SPAN_MIN) { this.goLive(); return; }
    this.at = new Date(this._start() + v * 60000);
    this._render();
    this.onChange(this.at);
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
    this.playing = false; clearInterval(this.timer);
    $("tl-play").textContent = "Play";
  }
  _labels() {
    $("tl-start").textContent = fmt(new Date(this._start()));
    $("tl-end").textContent = "now";
  }
  _render() {
    const ind = $("tl-mode");
    ind.textContent = this.at ? "REPLAY " + fmt(this.at) : "LIVE";
    ind.classList.toggle("replay", !!this.at);
    $("timeline").classList.toggle("replay", !!this.at);
    $("tl-live").classList.toggle("on", !this.at);
  }
}
