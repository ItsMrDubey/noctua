// Command palette (Ctrl+K or /): search tracks, places, layers and actions from one field.
import { esc, evTitle } from "/static/js/ui.js";
import { uiGlyph, threatColor } from "/static/js/symbology.js";

const MAX_PER_GROUP = { Tracks: 8, Places: 6, Layers: 6, Actions: 8 };

// subsequence-aware score: exact prefix > word prefix > substring > subsequence
function score(hay, q) {
  if (!q) return 1;
  const h = hay.toLowerCase();
  const i = h.indexOf(q);
  if (i === 0) return 100 - h.length * 0.01;
  if (i > 0) return (/[\s·/(-]/.test(h[i - 1]) ? 70 : 45) - i * 0.1;
  let j = 0;
  for (let k = 0; k < h.length && j < q.length; k++) if (h[k] === q[j]) j++;
  return j === q.length && q.length > 3 ? 15 : 0;
}

export class Palette {
  constructor(ctx) {
    this.ctx = ctx;
    const el = document.createElement("div");
    el.id = "palette";
    el.className = "hidden";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", "Command palette");
    el.innerHTML = '<div class="pl-box glass"><div class="pl-in"><svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.6"/><path d="M10.4 10.4L14 14"/></svg>' +
      '<input id="pl-q" type="text" placeholder="Search tracks, places, layers, actions" autocomplete="off" spellcheck="false" aria-label="Command">' +
      '<kbd>Esc</kbd></div><div class="pl-list" id="pl-list" role="listbox"></div>' +
      '<div class="pl-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>Enter</kbd> run</span><span><kbd>Esc</kbd> close</span></div></div>';
    document.body.appendChild(el);
    this.el = el; this.q = el.querySelector("#pl-q"); this.list = el.querySelector("#pl-list");
    this.items = []; this.act = 0;
    this.q.addEventListener("input", () => this.update());
    this.q.addEventListener("keydown", (e) => this.key(e));
    el.addEventListener("mousedown", (e) => { if (e.target === el) this.close(); });
    this.list.addEventListener("click", (e) => { const r = e.target.closest("[data-i]"); if (r) this.run(+r.dataset.i); });
    this.list.addEventListener("mousemove", (e) => { const r = e.target.closest("[data-i]"); if (r && +r.dataset.i !== this.act) { this.act = +r.dataset.i; this.paint(); } });
  }
  get isOpen() { return !this.el.classList.contains("hidden"); }
  open(prefill) {
    this.ctx.refLayers.ensureAll();
    this.el.classList.remove("hidden");
    this.q.value = prefill || "";
    this.q.focus();
    this.update();
  }
  close() { this.el.classList.add("hidden"); this.q.blur(); }
  key(e) {
    if (e.key === "Escape") { e.preventDefault(); this.close(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!this.items.length) return;
      this.act = (this.act + (e.key === "ArrowDown" ? 1 : -1) + this.items.length) % this.items.length;
      this.paint();
      const r = this.list.querySelector('[data-i="' + this.act + '"]');
      if (r) r.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") { e.preventDefault(); this.run(this.act); }
  }
  run(i) {
    const it = this.items[i];
    if (!it) return;
    this.close();
    it.run();
  }

  candidates(q) {
    const c = this.ctx, out = [];
    // tracks
    if (q) {
      for (const l of [c.air, c.ships]) {
        for (const it of l.visibleItems()) {
          const s = Math.max(score(String(it.name || ""), q), score(String(it.id), q) - 5, score(String(it.reg || ""), q), score(String(it.type_code || ""), q) - 10, score(String(it.operator || ""), q) - 15);
          if (s > 0) out.push({ group: "Tracks", s: s + (it.high_priority ? 3 : 0), label: it.name || it.id, sub: [l.kind === "aircraft" ? "Aircraft" : "Ship", it.type_code, it.reg || it.operator].filter(Boolean).join(" · "),
            glyph: '<span style="color:' + threatColor(l.kind, it) + '">' + uiGlyph(l.kind, it) + "</span>", run: () => { c.select(l.kind, it.id); c.flyToItem(l.kind, it.id); } });
        }
      }
      // news events, by their English title (falls back to the original while translating)
      const evl = c.byKind && c.byKind.events;
      if (evl) {
        let n = 0;
        for (const it of evl.visibleItems()) {
          const t = evTitle(it);
          const s = Math.max(score(t, q), it.title_en && it.title ? score(String(it.title), q) - 10 : 0, score(String(it.place_name || ""), q) - 10);
          if (s <= 0) continue;
          out.push({ group: "Events", s: s - 2, label: t || it.id, sub: ["Event", String(it.category || "").replace(/_/g, " "), it.place_name].filter(Boolean).join(" · "),
            glyph: '<span style="color:' + threatColor("events", it) + '">' + uiGlyph("events", it) + "</span>", run: () => { c.select("events", it.id); c.flyToItem("events", it.id); } });
          if (++n >= 40) break;
        }
      }
      // places
      for (const p of c.refLayers.places()) {
        const s = score(p.name, q);
        if (s <= 0) continue;
        const it = p.item;
        out.push({ group: "Places", s: s + (p.kind === "country" ? 5 : p.kind === "base" ? 3 : 0), label: p.name, sub: { airport: "Airport", medairport: "Airport", port: "Seaport", base: "Military installation", chokepoint: "Chokepoint", country: "Country" }[p.kind] || p.kind,
          glyph: '<span style="color:#3ddcc2">' + uiGlyph("ref") + "</span>",
          run: () => { if (p.ref) c.showRef(p.ref); c.flyTo(it.lat, it.lon, p.kind === "country" ? 3e6 : p.kind === "chokepoint" ? 1e6 : 2.5e5); } });
      }
    }
    // layers (from the tree, so labels match what the user sees)
    for (const inp of document.querySelectorAll("#left input[type=checkbox][id]")) {
      if (inp.disabled) continue;
      const lab = inp.closest("label");
      const name = lab ? lab.textContent.replace(/\s+/g, " ").replace(/[\d,–]+$/, "").trim() : inp.id;
      const sec = inp.closest(".grp") ? inp.closest(".grp").querySelector("h3").textContent : "";
      const s = q ? Math.max(score(name, q), score(sec + " " + name, q) - 10) : 0;
      if (q && s <= 0) continue;
      if (!q) continue;
      out.push({ group: "Layers", s, label: (inp.checked ? "Hide " : "Show ") + name, sub: sec + (inp.checked ? " · on" : " · off"),
        glyph: '<i class="pl-chk' + (inp.checked ? " on" : "") + '"></i>', run: () => inp.click() });
    }
    // actions
    for (const a of this.actions()) {
      const s = q ? score(a.label, q) : 1;
      if (s > 0) out.push({ group: "Actions", s, ...a, glyph: '<i class="pl-act">›</i>' });
    }
    return out;
  }
  actions() {
    const c = this.ctx, t = c.timeline;
    return [
      { label: "Go live", sub: "Return to real time · L", run: () => t.goLive() },
      { label: t.playing ? "Pause replay" : "Play replay", sub: "Space", run: () => t.togglePlay() },
      { label: "Replay from 6 h ago", sub: "Timeline", run: () => t.jump(360) },
      { label: "Replay from 24 h ago", sub: "Timeline", run: () => t.jump(1440) },
      { label: "Replay from 48 h ago", sub: "Timeline", run: () => t.jump(2879) },
      { label: "World view", sub: "Camera · H", run: () => c.hud.home() },
      { label: "Toggle 2D / 3D", sub: "Camera", run: () => c.hud.toggleMode() },
      { label: "Toggle layer panel", sub: "Panels · [", run: () => c.tree.toggleCollapsed() },
      { label: "Show alerts", sub: "Panels", run: () => c.tracklist.setPane("alerts") },
      { label: "Clear selection", sub: "Esc", run: () => c.clearSelection() },
      { label: "Follow selected track", sub: "Camera", run: () => c.setFollow() },
      { label: "Toggle FPS meter", sub: "Diagnostics · F", run: () => c.fps.toggle() },
    ];
  }
  update() {
    const q = this.q.value.trim().toLowerCase();
    const all = this.candidates(q);
    const groups = ["Tracks", "Places", "Layers", "Actions"];
    this.items = [];
    for (const g of groups) {
      const xs = all.filter((x) => x.group === g).sort((a, b) => b.s - a.s).slice(0, MAX_PER_GROUP[g]);
      this.items.push(...xs);
    }
    this.act = 0;
    this.paint(true);
  }
  paint(full) {
    if (full) {
      let html = "", g = null;
      if (!this.items.length) html = '<div class="pl-empty">No match. Try a callsign, registration, ICAO type, base, chokepoint or country.</div>';
      this.items.forEach((it, i) => {
        if (it.group !== g) { g = it.group; html += '<div class="pl-grp">' + g + "</div>"; }
        html += '<div class="pl-row" role="option" data-i="' + i + '"><span class="pl-g">' + it.glyph + '</span><span class="pl-l">' + esc(it.label) + '</span><span class="pl-s">' + esc(it.sub || "") + "</span></div>";
      });
      this.list.innerHTML = html;
    }
    for (const r of this.list.querySelectorAll(".pl-row")) r.classList.toggle("act", +r.dataset.i === this.act);
  }
}
