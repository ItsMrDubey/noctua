// Collapsible sortable table of current aircraft / ships; click a row to select + fly to it.
import { $, esc } from "/static/js/ui.js";

const COLS = {
  aircraft: [["name", "Callsign"], ["type_code", "Type"], ["operator", "Operator"], ["alt_ft", "Alt ft"], ["speed_kts", "Kts"]],
  ships: [["name", "Name"], ["type_code", "Type"], ["operator", "Operator"], ["speed_kts", "Kts"]],
};
const MAX_ROWS = 300;

export class TrackList {
  constructor(layers, onPick) {
    this.layers = layers; this.onPick = onPick;
    this.tab = "aircraft"; this.sort = { aircraft: ["high_priority", -1], ships: ["name", 1] };
    this.open = true; this.dirty = true;
    $("tl-collapse").addEventListener("click", () => { this.open = !this.open; this.render(true); });
    for (const b of document.querySelectorAll("#tracks .tab")) {
      b.addEventListener("click", () => { this.tab = b.dataset.tab; this.render(true); });
    }
    $("tracks-table").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-k]");
      if (th) {
        const cur = this.sort[this.tab];
        this.sort[this.tab] = [th.dataset.k, cur[0] === th.dataset.k ? -cur[1] : 1];
        this.render(true); return;
      }
      const tr = e.target.closest("tr[data-id]");
      if (tr) this.onPick(this.tab, tr.dataset.id);
    });
    setInterval(() => this.render(false), 3000);
  }
  setVisible(v) { $("tracks").classList.toggle("hidden", !v); }
  render(force) {
    $("tracks").classList.toggle("collapsed", !this.open);
    $("tl-collapse").textContent = this.open ? "–" : "+";
    for (const b of document.querySelectorAll("#tracks .tab")) b.classList.toggle("on", b.dataset.tab === this.tab);
    if (!this.open || $("tracks").classList.contains("hidden")) return;
    const layer = this.layers[this.tab];
    const [k, dir] = this.sort[this.tab];
    const rows = layer.visibleItems().sort((a, b) => {
      const x = a[k], y = b[k];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : (x === y ? 0 : x < y ? -1 : 1)) * dir;
    });
    const cols = COLS[this.tab];
    const head = "<tr>" + cols.map(([key, label]) => '<th data-k="' + key + '">' + label + (key === k ? (dir > 0 ? " ▲" : " ▼") : "") + "</th>").join("") + "</tr>";
    const body = rows.slice(0, MAX_ROWS).map((it) => '<tr data-id="' + esc(it.id) + '"' + (it.high_priority ? ' class="hp"' : "") + ">" +
      cols.map(([key]) => "<td>" + esc(it[key] == null ? (key === "name" ? it.id : "") : it[key]) + "</td>").join("") + "</tr>").join("");
    $("tracks-count").textContent = rows.length > MAX_ROWS ? MAX_ROWS + " of " + rows.length : String(rows.length);
    $("tracks-table").innerHTML = head + body;
  }
}
