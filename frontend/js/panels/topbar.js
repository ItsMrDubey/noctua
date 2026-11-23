// Command bar: ONE system-status pill ("● 18/20 feeds OK", coloured by the worst feed state,
// unread-alert badge) opening a popover with every feed's status / data age / rate / last
// error, the alerts count, the GDELT scorecard (evstats.js) and the signals & space summary
// (signals/index.js); plus the clocks (display zone from timefmt.js, secondary clock UTC).
import { $, esc } from "/static/js/ui.js";
import { fmtTime, tzLabel, getTz, onTzChange, utcTitle, fmtDateTime, toMs } from "/static/js/timefmt.js";
import { fmtAge } from "/static/js/freshness.js";

const BUCKETS = 20; // minutes of history per sparkline
const RANK = { down: 3, degraded: 2, starting: 1, healthy: 0 };

/** Popover behaviour shared by the command-bar popovers: click toggles, Esc / outside click
 *  closes (focus back on the button), aria-expanded kept in sync. Returns { open, close, isOpen }. */
export function bindPopover(btn, pop, { onOpen } = {}) {
  const isOpen = () => !pop.classList.contains("hidden");
  const close = (refocus) => {
    if (!isOpen()) return;
    pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false");
    if (refocus) btn.focus();
  };
  const open = () => {
    for (const other of document.querySelectorAll(".cmd-pop:not(.hidden)")) if (other !== pop && other._close) other._close();
    pop.classList.remove("hidden"); btn.setAttribute("aria-expanded", "true");
    if (onOpen) onOpen();
    const f = pop.querySelector("[autofocus], input, button, [tabindex]:not([tabindex='-1'])");
    (f || pop).focus({ preventScroll: true });
  };
  pop.classList.add("cmd-pop");
  pop._close = () => close(false);
  btn.addEventListener("click", (e) => { e.stopPropagation(); if (isOpen()) close(false); else open(); });
  // capture phase: Esc closes the popover without also clearing the globe selection
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) { e.stopImmediatePropagation(); e.preventDefault(); close(true); }
  }, true);
  document.addEventListener("pointerdown", (e) => { if (isOpen() && !pop.contains(e.target) && !btn.contains(e.target)) close(false); });
  return { open, close, isOpen };
}

export class StatusBar {
  constructor() {
    this.hist = {};      // source -> {minute -> records}
    this.lastSeen = {};  // source -> last_success string
    this.prevStatus = {};
    this.onTransition = null; // (name, from, to, info) for alerting
    this.last = null;
    const btn = $("sys-pill"), pop = $("sys-pop");
    if (btn && pop) {
      this.pop = bindPopover(btn, pop, { onOpen: () => this.render(this.last ? this.last[0] : null, this.last ? this.last[1] : {}) });
      $("sys-close").addEventListener("click", () => this.pop.close(true));
      $("sys-alerts-open").addEventListener("click", () => {
        this.pop.close(false);
        const t = document.querySelector('#tracks .ptab[data-p="alerts"]');
        if (t) { document.getElementById("tracks").classList.remove("hidden"); t.click(); }
      });
      onTzChange(() => { if (this.last) this.render(this.last[0], this.last[1]); });
      setInterval(() => { if (this.pop.isOpen() && this.last) this.render(this.last[0], this.last[1]); }, 2000); // data ages tick
    }
  }

  _record(name, s) {
    if (!s.last_success || this.lastSeen[name] === s.last_success) return;
    const first = this.lastSeen[name] === undefined;
    this.lastSeen[name] = s.last_success;
    if (first) return; // don't count the batch that happened before we connected
    const m = Math.floor(Date.now() / 60000);
    const h = (this.hist[name] = this.hist[name] || {});
    h[m] = (h[m] || 0) + (s.last_batch_size || 0);
    for (const k of Object.keys(h)) if (+k < m - BUCKETS) delete h[k];
  }

  _series(name) {
    const h = this.hist[name] || {};
    const m = Math.floor(Date.now() / 60000);
    const out = [];
    for (let i = m - BUCKETS + 1; i <= m; i++) out.push(h[i] || 0);
    return out;
  }

  _spark(vals) {
    const max = Math.max(1, ...vals);
    const pts = vals.map((v, i) => (i / (vals.length - 1)) * 34 + "," + (12 - (v / max) * 11).toFixed(1));
    return '<svg viewBox="0 0 34 12" preserveAspectRatio="none" aria-hidden="true"><polygon points="0,12 ' + pts.join(" ") + ' 34,12"/><polyline points="' + pts.join(" ") + '"/></svg>';
  }

  /** Unread alerts badge on the pill + the count inside the popover. */
  setAlerts(n, hot) {
    const b = $("sys-alerts"), t = n ? String(Math.min(99, n)) : "";
    if (b && b.textContent !== t) b.textContent = t;
    if (b) b.classList.toggle("hot", !!(n && hot));
    const c = $("sys-alerts-n");
    if (c) c.textContent = String(n || 0);
  }

  render(status, errors) {
    errors = errors || {};
    this.last = [status, errors];
    const rows = [];
    const sources = (status && status.sources) || {};
    const now = Date.now();
    let ok = 0, worst = status ? "healthy" : "down";
    const bump = (st) => { if ((RANK[st] || 0) > (RANK[worst] || 0)) worst = st; };
    const names = Object.keys(sources).sort((a, b) => (RANK[sources[b].status] || 0) - (RANK[sources[a].status] || 0) || a.localeCompare(b));
    for (const n of Object.keys(sources)) {
      const s = sources[n] || {};
      this._record(n, s);
      const prev = this.prevStatus[n];
      if (prev && prev !== s.status && this.onTransition) this.onTransition(n, prev, s.status, s);
      this.prevStatus[n] = s.status;
      if (s.status === "healthy") ok++;
      bump(s.status);
    }
    const open = this.pop ? this.pop.isOpen() : false;
    if (open) {
      for (const n of names) {
        const s = sources[n] || {};
        const vals = this._series(n);
        const rate = Math.max(vals[vals.length - 2] || 0, vals[vals.length - 1] || 0);
        const t = toMs(s.last_success);
        const age = isNaN(t) ? "–" : fmtAge((now - t) / 1000);
        const ageTip = isNaN(t) ? "never succeeded" : "last success " + fmtDateTime(t, { seconds: true, zone: true }) + "\n" + utcTitle(t) + "\nlast batch: " + (s.last_batch_size == null ? "–" : s.last_batch_size);
        const err = s.last_error ? String(s.last_error) : "";
        rows.push('<div class="fd ' + esc(s.status) + '" role="listitem"><i class="dot ' + esc(s.status) + '"></i><span class="nm">' + esc(n) + '</span><span class="st">' + esc(s.status || "unknown") +
          '</span><span class="age num" title="' + esc(ageTip) + '">' + age + '</span><span class="spk" title="records / min, last ' + BUCKETS + ' min">' + this._spark(vals) + '<span class="rate num">' + (rate ? fmtRate(rate) : "·") + "</span></span>" +
          (err ? '<div class="fd-err' + (s.status === "healthy" ? " old" : "") + '" title="' + esc(err) + '">' + esc(err.split("\n")[0]) + "</div>" : "") + "</div>");
      }
    }
    if (!status) rows.push('<div class="fd down" role="listitem"><i class="dot down"></i><span class="nm">api</span><span class="st">down</span><div class="fd-err">' + esc(errors.status || "no data") + "</div></div>");
    const errKeys = ["aircraft", "ships", "fires", "quakes", "events", "replay"].filter((k) => errors[k]);
    for (const k of errKeys) {
      bump("degraded");
      if (open) rows.push('<div class="fd degraded" role="listitem"><i class="dot degraded"></i><span class="nm">' + k + ' feed</span><span class="st">UI fetch</span><div class="fd-err" title="' + esc(errors[k]) + '">' + esc(errors[k]) + "</div></div>");
    }
    if (open) { const box = $("sources"); const h = rows.join(""); if (box._h !== h) { box._h = h; box.innerHTML = h; } }
    // the pill
    const total = Object.keys(sources).length;
    const txt = !status ? "API offline" : total ? ok + "/" + total + " feeds OK" : "No feeds";
    const tEl = $("sys-text"), dot = $("sys-dot");
    if (tEl && tEl.textContent !== txt) tEl.textContent = txt;
    if (dot) dot.className = "dot " + worst;
    const pill = $("sys-pill");
    if (pill) {
      pill.dataset.state = worst;
      const bad = names.filter((n) => sources[n].status !== "healthy");
      pill.title = "System status: " + txt + (bad.length ? " · not OK: " + bad.map((n) => n + " (" + sources[n].status + ")").join(", ") : "") + (errKeys.length ? " · UI fetch errors: " + errKeys.join(", ") : "") + "\nClick for feeds, GDELT, signals and alerts";
    }
    const sum = $("sys-sum");
    if (sum) sum.textContent = txt;
    // rail health dot for live tracks
    const rd = document.querySelector('.rail-btn[data-g="live"] .rail-dot');
    if (rd) {
      const st = Object.values(sources).map((s) => s.status);
      rd.className = "rail-dot" + (!status ? " bad" : st.includes("down") || Object.keys(errors).length ? " warn" : "");
    }
    const c = (status && status.counts) || {};
    const g = $("g-live-n");
    if (g) g.title = "Live store: " + (c.aircraft ?? "–") + " aircraft, " + (c.ships ?? "–") + " ships";
  }
  /** Fatal start-up message (Cesium missing, UI error) on the pill. */
  fatal(msg) { const t = $("sys-text"), d = $("sys-dot"); if (t) t.textContent = msg; if (d) d.className = "dot down"; }
}

const fmtRate = (n) => (n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n)) + "/m";

// Clocks: primary = the display zone (timefmt), secondary = UTC (or the browser zone when the
// display zone is UTC). Hover shows both with the date.
export function startClock() {
  const f = () => {
    const d = Date.now(), tz = getTz();
    const secTz = tz === "UTC" ? "local" : "UTC";
    const a = fmtTime(d, { seconds: true }) + "<small>" + esc(tzLabel(d)) + "</small>";
    const b = fmtTime(d, { tz: secTz }) + "<small>" + esc(secTz === "UTC" ? "UTC" : tzLabel(d, "local")) + "</small>";
    const c1 = $("clock"), c2 = $("clock-local");
    if (c1._h !== a) { c1._h = a; c1.innerHTML = a; }
    if (c2._h !== b) { c2._h = b; c2.innerHTML = b; }
    const box = c1.parentElement;
    if (box && (d / 1000 | 0) % 30 === 0 || box && !box.title) box.title = fmtDateTime(d, { zone: true }) + "\n" + utcTitle(d);
  };
  f();
  setInterval(f, 1000);
  onTzChange(f);
}
