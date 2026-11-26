# Intel-Console Reskin (Amber Phosphor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reskin the globe UI from the cyan "NOCTUA glass" look to a matte near-black intel console with an amber phosphor accent and mono data. Layout stays as it is, and the UI inconsistencies the spec lists get fixed along the way.

**Architecture:** All colours become CSS custom properties in `frontend/css/theme.css`. That includes the new `--cat-*`, `--st-*`, `--thr-*` and `--sym-*` symbology tokens. `frontend/js/symbology.js` reads them once at startup with `getComputedStyle`, caches the CSS strings and the Cesium `Color`s, and exports getters that the globe modules and sub-project B use. A one-shot codemod moves the per-feature CSS onto the tokens and raises every font size below 11px to 11px. Two Python checks keep the rules enforced: a WCAG contrast check and a CSS lint. Both run under pytest. Before and after screenshots come from a small Node DevTools-protocol script driving headless Edge.

**Tech Stack:** Plain ES modules and vendored CesiumJS, with no JS toolchain. Python 3 for the scripts and pytest. Node 22 for the module tests and screenshots. Edge or Chrome runs headless. The FastAPI backend serves the UI.

**Spec:** `docs/superpowers/specs/2026-11-25-intel-console-reskin-design.md`

## Global Constraints

- Layout stays the same: no panel or navigation restructuring, no theme switcher, no light mode, no new features. The country context is sub-project B.
- Token names stay where possible so existing `var(--…)` uses keep working. New names: `--raise`, `--glow`, `--radius`, `--red-2`, `--cat-*`, `--st-*`, `--thr-0..4`, `--sym-*`, `--globe-line`, `--globe-base`, `--foot-h`.
- Spec values, copied verbatim: `--void #0a0a08`, `--deep #11110d`, `--raise #181812`, `--glass #11110dF2`, `--line rgba(255,176,0,.14)`, `--line-2 rgba(255,176,0,.28)`, `--fg #f2e6c9`, `--fg-2 #b8a57e`, `--dim #6e6450`, `--acc`/`--amber #ffb000`, `--acc-rgb: 255,176,0`, `--red #ff4d2e`, `--green #7fd16b`, `--ion #7fb8c9`, `--violet #c792ea`, `--glow: 0 0 0 1px var(--acc), 0 0 8px rgba(var(--acc-rgb),.35)`, `--radius: 2px`.
- `backdrop-filter` blur is removed everywhere. Drop shadows are removed. Glows (zero offset) are allowed.
- Replay mode: `body.replay` sets `--acc` to `#7fb8c9` (cyan). The `REPLAY` tag in the command bar stays.
- Type: `--f-mono` (JetBrains Mono, tabular-nums) for every number, ID, coordinate, timestamp, code and table cell. `--f-head` (Bahnschrift) for section and panel titles: uppercase, `letter-spacing: .08em`, 11px. `--f-body` at 12px. **Minimum font size is 11px.**
- Palette rules: categories are distinct from each other and from amber. Every symbology colour is at least 3:1 on `--void` and on the darkened globe. The threat scale runs dim sand → amber-orange → red. Status brightness rises from reported to official. Text: at least 4.5:1, and at least 3:1 for `--dim`.
- CRT effect: `body.fx-crt`, off by default, behind a toggle in Settings, `pointer-events: none`.
- Spacing is on an 8px scale: `--gap: 8px`.
- Bundler constraints (`backend/osint/frontend_build.py`):
  - imports must be single-line named imports of the form `import { a, b } from "/static/js/x.js";`;
  - `export` must be at column 0 on `const`/`let`/`function`/`class` declarations;
  - no `export default`, `export {…}` or `export *`;
  - no dynamic `import()`;
  - no import cycles.
- `frontend/dist/` is gitignored. The server rebuilds it automatically when a source file is newer (`backend/osint/api/static.py` `ensure_fresh`). Never commit `dist`.
- Do not commit `data/` (gitignored), which is where the screenshots go.

---

## Decisions this plan fixes (spec left them open)

**Palette.** Verified with `scripts/check_contrast.py`: 140/140 checks pass.

| Token | Hex | Use |
|---|---|---|
| `--cat-armed_conflict` | `#ff4d4d` | event category |
| `--cat-air_activity` | `#8fd3ff` | 〃 |
| `--cat-maritime_security` | `#5b8cff` | 〃 |
| `--cat-civil_unrest` | `#ff7eb6` | 〃 (was amber `#ffb020`, so it had to move) |
| `--cat-disaster` | `#ff7a1a` | 〃 |
| `--cat-infrastructure` | `#45c9b0` | 〃 |
| `--cat-political` | `#b69cff` | 〃 |
| `--cat-economic` | `#7fd16b` | 〃 |
| `--cat-cyber_outage` | `#e36bff` | 〃 |
| `--cat-other` | `#a89f8a` | unknown category / fallback |
| `--st-reported` | `#7d735f` | status, L=.174 |
| `--st-multi-source` | `#a39470` | L=.301 |
| `--st-sensor-detected` | `#c7ad6e` | L=.432 |
| `--st-sensor-corroborated` | `#e6c35c` | L=.566 |
| `--st-official` | `#ffe066` | L=.755 |
| `--thr-0` … `--thr-4` | `#8c8270` `#c9a66b` `#ff9f1c` `#ff6a1f` `#ff3b2b` | threat 0..4 (hue 39→5°) |
| `--sym-air` / `--sym-air-civ` / `--sym-hp` | `#8fe8f5` / `#bdb6a5` / `#ff4d2e` | aircraft mil / civil / high-priority |
| `--sym-ship` / `--sym-ship-mil` | `#6f9cff` / `#ff4d2e` | ships |
| `--sym-fire` / `--sym-quake` | `#ff7a1a` / `#f2e45c` | sensors |
| `--red-2` | `#ff8a70` | light red for alert *text* (replaces the many `#ff9ca2`-style literals) |
| `--globe-line` / `--globe-base` | `rgba(255, 176, 0, .18)` / `#0c0b08` | graticule + country borders / globe base colour |

**Darkened-globe reference.** `#24221a` (`GLOBE_REF` in the script). This is a land mid-tone of the Esri base after the new tint. Ocean is darker, so a colour that passes here also passes on the ocean. Override it with `--globe #rrggbb` after sampling a real screenshot.

**Exports and tokens sub-project B can rely on.** Defined in Task 4 and Task 5.

- `cssColor(name: string): string`: the CSS colour string of any theme token, e.g. `cssColor("--line-2")`. Returns `""` if the token is unknown.
- `cesiumColor(name: string, alpha?: number): Cesium.Color`: cached and shared, so callers must not mutate it. For the selected-country outline: `cesiumColor("--line-2")`.
- `catColor(cat: string): string`: the category colour (`--cat-<cat>`, or `--cat-other` for an unknown category).
- `statusColor(status: string): string`: `--st-<status>`, or `--st-reported` for an unknown status.
- `threatLevelColor(level: number): string`: `--thr-0..4`, with the level clamped and rounded.
- These keep their names and shapes: `PALETTE_FALLBACK`, `EVENT_CATS` (still `[label, cssColour, glyphSvg]`), `EVENT_STATUS` (labels), `COLORS`, `threatColor(kind, it)` and `eventCat(cat)`.
- CSS tokens:
  - `--line-2` for the selected country outline;
  - `--globe-line` for borders and the graticule;
  - `--acc`/`--amber` for selection;
  - `--raise` for cards;
  - `--glow` for focus and active state;
  - `--radius`.

**Other resolutions**

1. `rgba(var(--c),.18)` in the spec cannot work, because `--c` holds a hex colour. Chips use `color-mix(in srgb, var(--c) 18%, transparent)` instead. `color-mix` is already used in the codebase, and Edge, Chrome and WebView2 support it.
2. Inline swatches that mirror `symbology.js` colours move to tokens: aircraft, ships, fires, quakes, news events, borders and graticule. Swatches that mirror colours hard-coded in other JS layers stay literal: reference bases, airports, ports, infra, signals, movements, carriers, weather and UCDP. If they changed, they would stop matching the globe. Recolouring those layers is out of scope.
3. Statuses become a sand brightness ramp, so they still read in greyscale. Status pills, dossier chips and classification bars use `--st-*`. The green for corroborated and cyan for multi-source go away.
4. Old amber that meant "replay" now uses `--acc`, which is cyan in replay. This covers the mode pill, the timeline border, the mode text and the reticle. Live selection is amber.
5. Timeline range "buttons styled as tabs": the `<select id="tl-range">` becomes a `.seg` group of four buttons (`#tl-r-48h`, `#tl-r-7d`, `#tl-r-30d`, `#tl-r-1y`). Same behaviour, and `timeline.js` and `app.js` are adjusted to match.
6. Histogram "amber bars": the events series, which dominates, draws in `--amber`. Fires, quakes, aircraft and ships keep their symbology colours so the legend stays meaningful.
7. Footer. At the 11px minimum, the attribution line no longer fits in the 8px gap under the dock. A `--foot-h: 14px` strip is added: the dock, the HUD and every column whose `bottom` derives from the dock sit 14px higher. The licences link moves to the front so it never gets truncated.
8. The legacy per-feature literals are mapped mechanically by `scripts/reskin_codemod.py`. The full mapping is in Task 3. The codemod runs once and is not committed. Two data-legend gradients are left as they are and tagged `/* legend */`: `.sw.hex` (viridis density) and `.wx-scale` (IR cloud ramp).
9. Spec table rows with no CSS file of their own: the timeline lives in `dock.css` and the tabs in `panels.css`. Cesium widget overrides go into `theme.css`.
10. Ordering differs from the brief: the per-feature CSS colour work happens in Task 3 (the codemod), early, so that every later task edits post-codemod text. Task 10 handles the per-feature residuals that live in JS.

## How to run and screenshot

- Launch: `uv run osint run`. This runs collectors, pipeline and API in one process, and the UI is at **http://127.0.0.1:8765/**. With no worker running, `uv run osint api` also serves the UI from the stores, but with stale data.
- The server rebuilds `frontend/dist` itself when a source file changes. To serve the unbundled sources, set `OSINT_FRONTEND=src` (PowerShell: `$env:OSINT_FRONTEND="src"`). Hard-reload the browser after CSS edits.
- Screenshots: `node scripts/shoot_views.mjs <label>` (created in Task 1). It writes `data/screens/<label>/01-globe.png … 08-settings.png` and `console-errors.txt`. It uses headless Edge (`C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe`) through the DevTools protocol. Set the env var `BROWSER` to use another Chromium.
- The `run` skill or claude-in-chrome can be used for interactive checks. The keyboard-focus check in Task 10 needs a real browser.

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `scripts/shoot_views.mjs` (new) | headless screenshots of the 8 review views | 1, 11 |
| `scripts/check_contrast.py` (new) | WCAG and palette rules for `theme.css` | 2 |
| `scripts/check_css.py` (new) | CSS lint: font ≥ 11px, no blur, no drop shadow, no legacy or literal colours | 3 |
| `scripts/reskin_codemod.py` (temporary, not committed) | literal → token and font-size migration | 3 |
| `tests/frontend/test_theme.py` (new) | pytest wrapper for the three checks and the symbology token tests | 2, 3, 4 |
| `frontend/css/theme.css` | tokens, base elements, `.glass`, Cesium widgets, CRT | 2, 11 |
| `frontend/js/symbology.js` | palette read and cache, getters | 4 |
| `frontend/js/events.js`, `layers.js`, `declutter.js` | Cesium colours from tokens | 4 |
| `frontend/index.html` | swatches, timeline range tabs, footer order, loading states | 4, 5, 9, 10 |
| `frontend/js/theme/globe.js`, `reference.js`, `reticle.js` | globe tint, atmosphere, graticule and borders, selection colour | 5 |
| `frontend/css/shell.css` | command bar, rail, chips, popovers, HUD | 3, 6, 7, 10 |
| `frontend/css/panels.css` | tabs, inspector sections, kv grid, status pills, states | 3, 6, 7, 10 |
| `frontend/css/dossier.css`, `browser.css`, `asset-profile.css` | dossier cards, pivots, browser panes | 3, 8, 10 |
| `frontend/css/dock.css`, `frontend/js/timeline.js`, `histogram.js`, `frontend/app.js` | timeline | 3, 9 |
| `frontend/js/ui.js`, `panels/evstats.js`, `signals/index.js`, `terror.js`, `eventlist.js`, `panels/sensor-detection.js` | empty and error states, residual colours and fonts | 10 |
| `frontend/js/panels/settings.js` | CRT toggle | 11 |
| `desktop/ui/index.html` | Tauri splash recolour | 10 |

---

### Task 1: Screenshot harness and "before" baseline

**Files:**
- Create: `scripts/shoot_views.mjs`

**Interfaces:**
- Consumes: the running UI at `http://127.0.0.1:8765/`, `window.__osint` (the app context: `byKind`, `select`, `dossier.minimise()`, `timeline.goLive()`, exposed at `frontend/app.js:935`).
- Produces: `node scripts/shoot_views.mjs <label> [url]` writes `data/screens/<label>/{01-globe,02-layer-tree,03-inspector-event,04-events-tab,05-terror-tab,06-dossier-intel,07-timeline-replay,08-settings}.png` and `console-errors.txt`. Task 11 appends a `09-crt` view.

- [ ] **Step 1: Write the script**

```js
// Screenshots of the reskin review views through headless Edge/Chrome over the DevTools protocol
// (Node 22+: built-in fetch + WebSocket, no npm packages).
// Usage: node scripts/shoot_views.mjs <label> [url]   -> data/screens/<label>/<view>.png
// Needs the UI running (uv run osint run, http://127.0.0.1:8765/). BROWSER overrides the binary.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";

const label = process.argv[2] || "shots";
const url = process.argv[3] || "http://127.0.0.1:8765/";
const CANDIDATES = [process.env.BROWSER, "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe", "/usr/bin/google-chrome", "/usr/bin/chromium"].filter(Boolean);
const bin = CANDIDATES.find((p) => existsSync(p));
if (!bin) { console.error("no Edge/Chrome found; set BROWSER"); process.exit(2); }
const out = join("data", "screens", label);
mkdirSync(out, { recursive: true });
const port = 9333;
// fresh profile per run: no localStorage (layer state, CRT toggle) leaks between before / after
const profile = resolve("data", "screens", ".profile-" + label);
rmSync(profile, { recursive: true, force: true });
const proc = spawn(bin, ["--headless=new", "--remote-debugging-port=" + port, "--user-data-dir=" + profile,
  "--window-size=1600,900", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
  "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 150; i++) {
    try {
      const list = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* browser still starting */ }
    await sleep(200);
  }
  throw new Error("DevTools endpoint did not come up");
}

const ws = new WebSocket(await target());
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let seq = 0;
const pending = new Map();
const errors = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.text + " " + ((d.params.exceptionDetails.exception || {}).description || ""));
  if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errors.push(d.params.args.map((a) => a.value ?? a.description).join(" "));
};
const send = (method, params = {}) => new Promise((r) => { const id = ++seq; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const run = async (expr) => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true }); if (r.result && r.result.exceptionDetails) throw new Error(expr + ": " + r.result.exceptionDetails.text); };
const shot = async (name) => {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(out, name + ".png"), Buffer.from(r.result.data, "base64"));
  console.log("wrote " + join(out, name + ".png"));
};

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url });
await sleep(15000); // globe tiles + first polls

const VIEWS = [
  ["01-globe", ""],
  ["02-layer-tree", "document.querySelector('#rail .rail-btn[data-g=\"events\"]').click()"],
  ["03-inspector-event", "(() => { const o = window.__osint, it = [...o.byKind.events.items.values()][0]; if (!it) throw new Error('no events loaded'); o.select('events', it.id); })()"],
  ["04-events-tab", "document.querySelector('.ptab[data-p=\"events\"]').click()"],
  ["05-terror-tab", "document.querySelector('.ptab[data-p=\"terror\"]').click()"],
  ["06-dossier-intel", "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'i' }))"],
  ["07-timeline-replay", "(() => { window.__osint.dossier.minimise(); const s = document.getElementById('tl-slider'); s.value = String(Math.round(+s.max / 2)); s.dispatchEvent(new Event('input')); })()"],
  ["08-settings", "(() => { window.__osint.timeline.goLive(); document.getElementById('cfg-btn').click(); })()"],
];
for (const [name, js] of VIEWS) {
  try { if (js) await run(js); } catch (e) { console.warn("step " + name + " failed: " + e.message); }
  await sleep(name === "06-dossier-intel" ? 6000 : 2500);
  await shot(name);
}
writeFileSync(join(out, "console-errors.txt"), errors.join("\n"));
console.log(errors.length + " console errors (see " + join(out, "console-errors.txt") + ")");
ws.close();
proc.kill();
process.exit(0);
```

- [ ] **Step 2: Smoke-test the harness without the server**

Run: `node scripts/shoot_views.mjs smoke "data:text/html,<h1>hi</h1>"`
Expected: eight `wrote data\screens\smoke\0N-….png` lines. Steps 02–05, 07 and 08 print `step … failed` warnings, because the page has no app. Then `0 console errors`. Delete the folder afterwards: `Remove-Item -Recurse -Force data/screens/smoke, data/screens/.profile-smoke`.

- [ ] **Step 3: Capture the "before" baseline**

In terminal A: `uv run osint run`. Wait for `Uvicorn running on http://127.0.0.1:8765`.
In terminal B: `node scripts/shoot_views.mjs before`
Expected: eight PNGs in `data/screens/before/`. Open `03-inspector-event.png` and `06-dossier-intel.png` and check that they show an event inspector and the intel pane. If `03` warns `no events loaded`, wait about a minute for the first events poll and re-run. Keep `console-errors.txt` as the baseline: later runs must not add errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/shoot_views.mjs
git commit -m "chore(ui): headless screenshot harness for the reskin review views"
```

---

### Task 2: Theme tokens and contrast check

**Files:**
- Create: `scripts/check_contrast.py`
- Create: `tests/frontend/test_theme.py`
- Modify: `frontend/css/theme.css` (full rewrite, all 109 lines)

**Interfaces:**
- Produces:
  - every token in the table under "Decisions";
  - `--foot-h: 14px`, used by Task 10;
  - `parse_tokens(css: str) -> dict[str, str]` in `scripts/check_contrast.py`, which resolves `var(--x)` indirection and is used by the Task 4 test;
  - `main(argv: list[str]) -> int` in the same script.
- `body.replay` sets `--acc: #7fb8c9; --acc-rgb: 127, 184, 201`.

- [ ] **Step 1: Write the failing test**

`tests/frontend/test_theme.py`:

```python
"""Intel-console theme: WCAG contrast + palette rules (scripts/check_contrast.py), CSS lint
(scripts/check_css.py) and js/symbology.js reading its colours from the theme tokens."""

import importlib.util
import json
import re
import shutil
import subprocess

import pytest

from osint.config import ROOT

SCRIPTS = ROOT / "scripts"
THEME = ROOT / "frontend" / "css" / "theme.css"
SYM = (ROOT / "frontend" / "js" / "symbology.js").as_uri()
NODE = shutil.which("node")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_contrast_and_palette_rules_pass(capsys):
    assert _load("check_contrast").main([str(THEME)]) == 0, capsys.readouterr().out
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/frontend/test_theme.py -v`
Expected: FAIL with `FileNotFoundError` for `scripts/check_contrast.py`.

- [ ] **Step 3: Write `scripts/check_contrast.py`**

```python
"""WCAG contrast + palette rules for the intel-console theme (frontend/css/theme.css).

Reads the first `:root { ... }` block of theme.css and checks:
  text      --fg / --fg-2 >= 4.5:1 and --dim >= 3:1 on --void, --deep, --raise;
            accent text colours (--acc --red --red-2 --green --ion --violet) >= 4.5:1 on the same;
  symbology every --cat-*, --st-*, --thr-*, --sym-* >= 3:1 on --void and on the darkened globe;
  distinct  categories pairwise CIE76 dE >= 20 and each dE >= 20 from --acc (no amber category);
  order     --st-* luminance strictly increasing reported -> official, saturation non-decreasing;
            --thr-0..4 hue non-increasing (sand -> amber-orange -> red), luminance of 0 lowest.

Usage: python scripts/check_contrast.py [path/to/theme.css] [--globe #rrggbb]
Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import colorsys
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_THEME = ROOT / "frontend" / "css" / "theme.css"
# a land mid-tone of the Esri base imagery after the globe tint (brightness .55, saturation .35,
# warm hue shift); ocean is darker, so text and glyphs that pass on this pass on the ocean too
GLOBE_REF = "#24221a"
STATUS_ORDER = ["reported", "multi-source", "sensor-detected", "sensor-corroborated", "official"]
BGS = ["--void", "--deep", "--raise"]


def parse_tokens(css: str) -> dict[str, str]:
    m = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    if not m:
        raise SystemExit("no :root block found")
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    toks = {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}
    for _ in range(5):  # resolve var(--x) indirection
        for k, v in toks.items():
            r = re.fullmatch(r"var\((--[\w-]+)\)", v)
            if r and r.group(1) in toks:
                toks[k] = toks[r.group(1)]
    return toks


def rgb(value: str) -> tuple[float, float, float]:
    v = value.strip().lower()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # alpha ignored (opaque checks)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+).*\)", v)
    if m:
        return tuple(int(x) / 255 for x in m.groups())
    raise ValueError(f"not a colour: {value!r}")


def lum(c) -> float:
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def lab(c):
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    x = (0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]) / 0.95047
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    z = (0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]) / 1.08883
    f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in (x, y, z)]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def delta_e(a, b) -> float:
    return math.dist(lab(a), lab(b))


def main(argv: list[str]) -> int:
    theme, globe = DEFAULT_THEME, GLOBE_REF
    args = list(argv)
    if "--globe" in args:
        i = args.index("--globe")
        globe = args[i + 1]
        del args[i:i + 2]
    if args:
        theme = Path(args[0])
    t = parse_tokens(theme.read_text(encoding="utf-8"))
    results: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str) -> None:
        results.append((ok, msg))

    for name, need in [("--fg", 4.5), ("--fg-2", 4.5), ("--dim", 3.0), ("--acc", 4.5), ("--red", 4.5),
                       ("--red-2", 4.5), ("--green", 4.5), ("--ion", 4.5), ("--violet", 4.5)]:
        for bg in BGS:
            r = ratio(rgb(t[name]), rgb(t[bg]))
            check(r >= need, f"text  {name:<8} on {bg:<7} {r:5.2f} (>= {need})")

    sym = [k for k in t if re.match(r"--(cat|st|thr|sym)-", k)]
    for name in sym:
        for label, bg in (("--void", t["--void"]), ("globe", globe)):
            r = ratio(rgb(t[name]), rgb(bg))
            check(r >= 3.0, f"sym   {name:<24} on {label:<6} {r:5.2f} (>= 3.0)")

    cats = [k for k in t if k.startswith("--cat-")]
    for i, a in enumerate(cats):
        d = delta_e(rgb(t[a]), rgb(t["--acc"]))
        check(d >= 20, f"dist  {a:<24} vs --acc {d:5.1f} (>= 20)")
        for b in cats[i + 1:]:
            d = delta_e(rgb(t[a]), rgb(t[b]))
            check(d >= 20, f"dist  {a} vs {b} {d:5.1f} (>= 20)")

    st = [rgb(t["--st-" + s]) for s in STATUS_ORDER]
    ls = [lum(c) for c in st]
    sats = [colorsys.rgb_to_hls(*c)[2] for c in st]
    check(all(a < b for a, b in zip(ls, ls[1:])), "order --st-* luminance increasing " + " < ".join(f"{x:.3f}" for x in ls))
    check(all(a <= b + 1e-9 for a, b in zip(sats, sats[1:])), "order --st-* saturation non-decreasing " + " <= ".join(f"{x:.2f}" for x in sats))

    thr = [rgb(t[f"--thr-{i}"]) for i in range(5)]
    hues = [colorsys.rgb_to_hls(*c)[0] * 360 for c in thr]
    check(all(a >= b for a, b in zip(hues, hues[1:])), "order --thr-0..4 hue non-increasing " + " >= ".join(f"{x:.0f}" for x in hues))
    check(lum(thr[0]) == min(lum(c) for c in thr), "order --thr-0 is the dimmest step")

    bad = [m for ok, m in results if not ok]
    for ok, m in results:
        print(("PASS " if ok else "FAIL ") + m)
    print(f"{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run it against the old theme and watch it fail**

Run: `uv run pytest tests/frontend/test_theme.py -v`
Expected: FAIL with `KeyError: '--red-2'`, because the old theme lacks the new tokens.

- [ ] **Step 5: Replace `frontend/css/theme.css` with the new content**

```css
/* NOCTUA // GLOBAL WATCH — intel-console theme (amber phosphor): design tokens, base elements,
   panel primitive, Cesium widget overrides. Globe colours (js/symbology.js, js/theme/globe.js) are
   read from these custom properties at startup: keep PALETTE_FALLBACK in js/symbology.js equal to
   the values here (tests/frontend/test_theme.py checks it). */
:root {
  /* surfaces */
  --void: #0a0a08;
  --deep: #11110d;
  --raise: #181812;
  --glass: #11110df2;
  --glass-hi: #181812f7;
  --line: rgba(255, 176, 0, .14);
  --line-2: rgba(255, 176, 0, .28);
  /* text */
  --fg: #f2e6c9;
  --fg-2: #b8a57e;
  --dim: #6e6450;
  /* accents */
  --amber: #ffb000;
  --acc: var(--amber);
  --acc-rgb: 255, 176, 0;
  --red: #ff4d2e;
  --red-2: #ff8a70;
  --green: #7fd16b;
  --ion: #7fb8c9;
  --violet: #c792ea;
  /* event categories (js/symbology.js EVENT_CATS): distinct, none amber (amber = selection) */
  --cat-armed_conflict: #ff4d4d;
  --cat-air_activity: #8fd3ff;
  --cat-maritime_security: #5b8cff;
  --cat-civil_unrest: #ff7eb6;
  --cat-disaster: #ff7a1a;
  --cat-infrastructure: #45c9b0;
  --cat-political: #b69cff;
  --cat-economic: #7fd16b;
  --cat-cyber_outage: #e36bff;
  --cat-other: #a89f8a;
  /* event status: luminance + saturation rise with corroboration (reads in greyscale) */
  --st-reported: #7d735f;
  --st-multi-source: #a39470;
  --st-sensor-detected: #c7ad6e;
  --st-sensor-corroborated: #e6c35c;
  --st-official: #ffe066;
  /* threat scale 0..4: dim sand -> amber-orange -> red */
  --thr-0: #8c8270;
  --thr-1: #c9a66b;
  --thr-2: #ff9f1c;
  --thr-3: #ff6a1f;
  --thr-4: #ff3b2b;
  /* track + sensor symbology (js/symbology.js COLORS) */
  --sym-air: #8fe8f5;
  --sym-air-civ: #bdb6a5;
  --sym-hp: #ff4d2e;
  --sym-ship: #6f9cff;
  --sym-ship-mil: #ff4d2e;
  --sym-fire: #ff7a1a;
  --sym-quake: #f2e45c;
  /* globe (js/theme/globe.js, js/reference.js) */
  --globe-line: rgba(255, 176, 0, .18);
  --globe-base: #0c0b08;
  /* effects */
  --glow: 0 0 0 1px var(--acc), 0 0 8px rgba(var(--acc-rgb), .35);
  --radius: 2px;
  /* type */
  --f-head: "Bahnschrift SemiCondensed", "Bahnschrift", "Segoe UI Variable Display", "Segoe UI Variable", "Inter", "Arial Narrow", sans-serif;
  --f-body: "Segoe UI Variable Text", "Segoe UI Variable", "Inter", "Segoe UI", system-ui, sans-serif;
  --f-mono: "JetBrains Mono", "Cascadia Mono", "Cascadia Code", Consolas, "SFMono-Regular", monospace;
  /* motion + layout (8px spacing scale) */
  --t-fast: 140ms;
  --t-med: 200ms;
  --ease: cubic-bezier(.2, .7, .2, 1);
  --gap: 8px;
  --bar-h: 48px;
  --dock-h: 118px;
  --foot-h: 14px;
}
/* replay: amber is the live default, so replay switches the accent to cyan */
body.replay { --acc: #7fb8c9; --acc-rgb: 127, 184, 201; }

* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; overflow: hidden; background: var(--void); color: var(--fg); font: 12px/1.45 var(--f-body); -webkit-font-smoothing: antialiased; }
#globe { position: fixed; inset: 0; }
.cesium-viewer-bottom, .cesium-viewer-toolbar { display: none !important; }
.hidden { display: none !important; }
::selection { background: rgba(var(--acc-rgb), .35); color: var(--fg); }
a { color: var(--acc); text-decoration: none; border-bottom: 1px dotted rgba(var(--acc-rgb), .6); }
a:hover { border-bottom-style: solid; }
kbd { font: 11px var(--f-mono); color: var(--fg-2); border: 1px solid var(--line-2); border-bottom-width: 2px; border-radius: var(--radius); padding: 0 4px; background: var(--raise); }
/* data in mono: numbers, ids, coordinates, timestamps, code, table cells */
.mono, .num, code, kbd, time, output, td { font-family: var(--f-mono); font-variant-numeric: tabular-nums; }

/* vignette + replay frame (pointer-transparent) */
#vignette { position: fixed; inset: 0; pointer-events: none; z-index: 5;
  background: radial-gradient(ellipse at 50% 46%, transparent 55%, rgba(10, 10, 8, .6) 100%); }
#vignette::after { content: ""; position: absolute; inset: 0; opacity: 0; transition: opacity var(--t-med) var(--ease);
  box-shadow: inset 0 0 0 1px rgba(var(--acc-rgb), .55), inset 0 0 90px rgba(var(--acc-rgb), .12); }
body.replay #vignette::after { opacity: 1; }

/* ---- panel primitive: matte surface, hairline border, bracketed corners (no blur, no shadow) ---- */
.glass {
  position: fixed; z-index: 10;
  background: var(--glass);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  transition: border-color var(--t-med);
}
.glass::before, .glass::after { content: ""; position: absolute; width: 8px; height: 8px; pointer-events: none; border: 0 solid rgba(var(--acc-rgb), .6); }
.glass::before { top: -1px; left: -1px; border-top-width: 1px; border-left-width: 1px; }
.glass::after { bottom: -1px; right: -1px; border-bottom-width: 1px; border-right-width: 1px; }

/* section / panel titles: head font, uppercase, 11px */
.eyebrow, h3 { margin: 0; font: 600 11px/1 var(--f-head); letter-spacing: .08em; text-transform: uppercase; color: var(--dim); }

/* ---- controls ---- */
button { font: 600 11px/1 var(--f-head); letter-spacing: .08em; text-transform: uppercase; color: var(--fg);
  background: transparent; border: 1px solid var(--line-2); border-radius: var(--radius); padding: 6px 10px; cursor: pointer;
  transition: background var(--t-fast), border-color var(--t-fast), color var(--t-fast); }
button:hover { background: var(--raise); border-color: rgba(var(--acc-rgb), .6); }
button.on { background: rgba(var(--acc-rgb), .16); border-color: var(--acc); color: var(--fg); }
button:disabled { opacity: .35; cursor: not-allowed; }
button.ghost { background: transparent; border-color: transparent; color: var(--fg-2); }
button.ghost:hover { color: var(--fg); border-color: var(--line-2); }
/* keyboard focus: one visible treatment on every interactive element */
:focus { outline: none; }
:focus-visible { outline: none; box-shadow: var(--glow) !important; }
input, select { font: 12px var(--f-mono); color: var(--fg); background: var(--void); border: 1px solid var(--line-2); border-radius: var(--radius); padding: 3px 6px; outline: none; color-scheme: dark; }
input:focus, select:focus { border-color: var(--acc); }
input[type=checkbox] { appearance: none; -webkit-appearance: none; width: 12px; height: 12px; margin: 0; padding: 0; flex: none; border: 1px solid var(--line-2); border-radius: 0; background: var(--void); cursor: pointer; position: relative; }
input[type=checkbox]:checked { border-color: var(--acc); background: rgba(var(--acc-rgb), .2); }
input[type=checkbox]:checked::after { content: ""; position: absolute; inset: 2px; background: var(--acc); }
input[type=checkbox]:disabled { opacity: .3; cursor: not-allowed; }

/* status dots */
.dot { width: 7px; height: 7px; flex: none; border-radius: 50%; background: var(--dim); color: var(--dim); }
.dot.healthy { background: var(--green); color: var(--green); }
.dot.degraded { background: var(--amber); color: var(--amber); }
.dot.down { background: var(--red); color: var(--red); }
.dot.starting { background: var(--violet); color: var(--violet); }

/* scrollbars: thin, --line-2 */
* { scrollbar-width: thin; scrollbar-color: var(--line-2) transparent; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: var(--line-2); }
::-webkit-scrollbar-track { background: transparent; }

#pick-tip { top: calc(var(--bar-h) + 20px); left: 50%; transform: translateX(-50%); padding: 6px 12px; pointer-events: none; font: 11px var(--f-mono); }
/* attribution footer: its own --foot-h strip under the dock; the licences link leads, the rest truncates */
#attrib { position: fixed; z-index: 9; bottom: 0; left: 50%; transform: translateX(-50%); height: var(--foot-h); max-width: calc(100vw - 2 * var(--gap)); overflow: hidden; text-overflow: ellipsis;
  font: 11px/14px var(--f-mono); color: var(--dim); white-space: nowrap; pointer-events: none; }
#attrib a { pointer-events: auto; color: var(--acc); }

/* ---- Cesium widgets (most are disabled in theme/globe.js; these can still appear) ---- */
.cesium-widget-errorPanel { background: rgba(10, 10, 8, .92); }
.cesium-widget-errorPanel-content { background: var(--deep); color: var(--fg); border: 1px solid var(--line-2); border-radius: var(--radius); box-shadow: none; font: 12px var(--f-body); }
.cesium-widget-errorPanel-header { color: var(--red); font: 600 11px var(--f-head); letter-spacing: .08em; text-transform: uppercase; }
.cesium-widget-errorPanel-message { color: var(--fg-2); font: 11px var(--f-mono); }
.cesium-button { background: var(--raise); color: var(--fg); fill: var(--fg); border: 1px solid var(--line-2); border-radius: var(--radius); }
.cesium-button:hover { background: var(--raise); color: var(--fg); border-color: var(--acc); box-shadow: none; }
.cesium-infoBox { background: var(--deep); color: var(--fg); border: 1px solid var(--line-2); border-radius: var(--radius); box-shadow: none; }
.cesium-infoBox-title { background: var(--raise); color: var(--fg); font: 600 11px var(--f-head); letter-spacing: .08em; text-transform: uppercase; }
.cesium-credit-lightbox-overlay { background: rgba(10, 10, 8, .8); }
.cesium-credit-lightbox { background: var(--deep); color: var(--fg); border: 1px solid var(--line-2); border-radius: var(--radius); }
.cesium-credit-lightbox a { color: var(--acc); }

@keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes slide-l { from { opacity: 0; transform: translateX(-10px); } to { opacity: 1; transform: none; } }
@keyframes slide-r { from { opacity: 0; transform: translateX(14px); } to { opacity: 1; transform: none; } }
@keyframes blink { 50% { opacity: .25; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 1ms !important; animation-iteration-count: 1 !important; transition-duration: 1ms !important; }
}
```

- [ ] **Step 6: Run the contrast check and the test**

Run: `uv run python scripts/check_contrast.py`
Expected output (tail):

```
PASS order --st-* luminance increasing 0.174 < 0.301 < 0.432 < 0.566 < 0.755
PASS order --st-* saturation non-decreasing 0.14 <= 0.22 <= 0.44 <= 0.73 <= 1.00
PASS order --thr-0..4 hue non-increasing 39 >= 38 >= 35 >= 20 >= 5
PASS order --thr-0 is the dimmest step
140/140 checks passed
```

Text lines to spot-check:

- `PASS text  --fg     on --void  15.98`
- `PASS text  --fg-2   on --raise  7.40`
- `PASS text  --dim    on --raise  3.06` (the tightest)
- `PASS sym   --st-reported            on globe   3.40`

Run: `uv run pytest tests/frontend/test_theme.py -v`
Expected: `1 passed`.

- [ ] **Step 7: Build and look**

Run: `uv run python -m osint.frontend_build`
Expected: JSON summary with `"css_files": 13`, and no `frontend build failed`.
Reload http://127.0.0.1:8765/. Panels should be near-black, borders amber hairlines, with no blur behind panels. Per-feature colours are still old: Task 3 fixes those.

- [ ] **Step 8: Commit**

```bash
git add scripts/check_contrast.py tests/frontend/test_theme.py frontend/css/theme.css
git commit -m "feat(ui): amber intel-console tokens, symbology palette and WCAG contrast check"
```

---

### Task 3: Mechanical CSS migration and CSS lint

**Files:**
- Create: `scripts/check_css.py`
- Create, then delete before committing: `scripts/reskin_codemod.py`
- Modify: `tests/frontend/test_theme.py` (append one test)
- Modify, via the codemod: `frontend/css/{asset-profile,browser,dock,dossier,imagery-viewer,naval,panels,shell,signals,terror,webcams}.css`
- Modify by hand: `frontend/css/browser.css:11-12,47,169,176,178` and `frontend/css/shell.css:34,213,229`

**Interfaces:**
- Consumes: tokens from Task 2.
- Produces:
  - `lint(css_dir: Path) -> list[str]` in `scripts/check_css.py`;
  - the `/* legend */` line tag convention;
  - every later task edits the post-codemod text, and the quoted "old" strings below already include the codemod's changes.

- [ ] **Step 1: Append the failing lint test to `tests/frontend/test_theme.py`**

```python


def test_css_lint_clean():
    found = _load("check_css").lint(ROOT / "frontend" / "css")
    assert found == [], "\n".join(found)
```

- [ ] **Step 2: Write `scripts/check_css.py`**

```python
"""Lint frontend/css for the intel-console theme rules.

  font    no font size below 11px (font-size and the font shorthand)
  blur    no backdrop-filter
  shadow  no drop shadows (box-shadow / text-shadow with a y offset >= 4px)
  legacy  none of the old NOCTUA cyan palette literals
  literal outside theme.css: no hex colours other than #000 / #fff and no rgba() with literal
          non-neutral rgb, except on lines tagged /* legend */ (data legends mirroring globe ramps)

Usage: python scripts/check_css.py [css_dir]   -> exit 0 when clean, 1 with one line per finding.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "frontend" / "css"
FONT = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px|font:\s*(?:[a-z-]+\s+|\d00\s+)*(\d+(?:\.\d+)?)px")
SHADOW_DECL = re.compile(r"(?:box|text)-shadow:\s*([^;}]*)")
NUM = re.compile(r"-?\d*\.?\d+(?:px)?")
LEGACY = ("94, 227, 234", "120, 175, 205", "#5ee3ea", "#05080d", "#08101a", "#03060a", "242, 173, 61", "255, 75, 85")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
NEUTRAL = {("0", "0", "0"), ("255", "255", "255")}


def drop_shadow_y(value: str) -> float:
    """Largest vertical offset among the shadows in a box-/text-shadow value."""
    v = value
    while re.search(r"\([^()]*\)", v):
        v = re.sub(r"\([^()]*\)", "", v)  # drop colour functions (nested parens)
    ys = []
    for part in v.split(","):
        nums = [t for t in part.split() if NUM.fullmatch(t)]
        if len(nums) >= 2:
            ys.append(abs(float(nums[1].removesuffix("px"))))
    return max(ys, default=0.0)


def lint(css_dir: Path) -> list[str]:
    out: list[str] = []
    for f in sorted(css_dir.glob("*.css")):
        theme = f.name == "theme.css"
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{f.name}:{n}"
            code = re.sub(r"/\*.*?\*/", "", line)
            for m in FONT.finditer(code):
                v = float(m.group(1) or m.group(2))
                if v < 11:
                    out.append(f"font    {where} {v:g}px < 11px")
            if "backdrop-filter" in code:
                out.append(f"blur    {where} backdrop-filter")
            for m in SHADOW_DECL.finditer(code):
                if drop_shadow_y(m.group(1)) >= 4:
                    out.append(f"shadow  {where} drop shadow y={drop_shadow_y(m.group(1)):g}px")
            for lit in LEGACY:
                if lit in code.lower():
                    out.append(f"legacy  {where} {lit}")
            if theme or "/* legend */" in line:
                continue
            for h in HEX.findall(code):
                if h.lower() not in ("#000", "#fff", "#000000", "#ffffff"):
                    out.append(f"literal {where} {h}")
            for m in RGB.finditer(code):
                if m.groups() not in NEUTRAL:
                    out.append(f"literal {where} {m.group(0)}")
    return out


def main(argv: list[str]) -> int:
    found = lint(Path(argv[0]) if argv else CSS)
    for x in found:
        print(x)
    print(f"{len(found)} findings")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 3: Run the lint and watch it fail**

Run: `uv run python scripts/check_css.py`
Expected: exit 1, with several hundred `font`, `literal` and `legacy` findings across the 11 per-feature files, plus `blur` and `shadow` in browser and shell. `theme.css` has none.
Run: `uv run pytest tests/frontend/test_theme.py::test_css_lint_clean -v` and check that it FAILS.

- [ ] **Step 4: Write the one-shot codemod `scripts/reskin_codemod.py`**

```python
"""One-shot codemod for the intel-console reskin: maps the old literal colours in
frontend/css/*.css (theme.css excluded) to theme tokens and raises every font size below 11px
to 11px. Idempotent; preserves line endings. Run once from the repo root:
python scripts/reskin_codemod.py"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "frontend" / "css"

EXACT = {
    "var(--fg-2, #b9c6d2)": "var(--fg-2)",
    "var(--ok, #6fcf97)": "var(--green)",
    "var(--glass-hi, rgba(8, 14, 22, .96))": "var(--glass-hi)",
}
# data legends that mirror globe-layer ramps: left untouched, tagged so the lint allows them
LEGEND_LINES = (".sw.hex {", "rgba(223, 231, 238, .15), #eef5fa")
TRIPLES = {
    (242, 173, 61): "--amber", (255, 75, 85): "--red", (255, 59, 78): "--red", (79, 223, 140): "--green",
    (5, 8, 13): "--void", (6, 12, 20): "--void", (2, 5, 9): "--void", (8, 14, 22): "--void",
    (5, 11, 18): "--void", (4, 9, 15): "--void", (3, 8, 14): "--void",
    (255, 210, 74): "--sym-quake", (255, 106, 26): "--sym-fire", (169, 155, 255): "--violet",
    (94, 227, 234): "--ion", (90, 184, 255): "--ion", (184, 199, 214): "--fg-2", (159, 176, 192): "--fg-2",
    (150, 170, 190): "--dim", (255, 61, 127): "--thr-4", (255, 155, 61): "--thr-3",
}
HEX = {
    "#eef6fb": "--fg", "#f2f8fc": "--fg", "#dfe8f0": "--fg", "#c9d6e2": "--fg", "#c7d3de": "--fg",
    "#c8d4de": "--fg", "#b8c7d6": "--fg-2", "#b9c6d2": "--fg-2",
    "#ff9ca2": "--red-2", "#ff8a90": "--red-2", "#ff9da2": "--red-2", "#ff8a8f": "--red-2", "#ff6b78": "--red-2",
    "#ffc2c5": "--red-2", "#ffd6d8": "--red-2", "#ff8fb4": "--red-2", "#ff6a5a": "--red-2", "#ff5a5a": "--red",
    "#ffd24a": "--sym-quake", "#ff6a1a": "--sym-fire", "#9ff3ff": "--sym-air", "#7fa8ff": "--sym-ship",
    "#d9b27c": "--cat-infrastructure", "#a99bff": "--violet", "#5ee3ea": "--ion", "#5ab8ff": "--ion",
    "#f2ad3d": "--amber", "#ffd08a": "--amber", "#ff9a5a": "--sym-fire", "#ffb27a": "--sym-fire",
    "#ff8a3d": "--sym-fire", "#ffb066": "--sym-fire", "#ff3d7f": "--thr-4", "#2a3644": "--raise",
    "#1a2533": "--raise", "#0b1520": "--void", "#060c14": "--void", "#0b1017": "--void", "#08101a": "--void",
    "#06121a": "--void",
}
RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")
HEXRE = re.compile(r"#[0-9a-fA-F]{6}\b")
FONT_SIZE = re.compile(r"(font-size:\s*)(\d+(?:\.\d+)?)px")
FONT_SHORT = re.compile(r"(font:\s*(?:[a-z-]+\s+|\d00\s+)*)(\d+(?:\.\d+)?)px")


def _rgba(m: re.Match) -> str:
    tok = TRIPLES.get(tuple(int(x) for x in m.group(1, 2, 3)))
    if not tok:
        return m.group(0)
    pct = round(float(m.group(4)) * 100, 1)
    return f"color-mix(in srgb, var({tok}) {pct:g}%, transparent)"


def _hex(m: re.Match) -> str:
    tok = HEX.get(m.group(0).lower())
    return f"var({tok})" if tok else m.group(0)


def _size(m: re.Match) -> str:
    return m.group(1) + ("11" if float(m.group(2)) < 11 else m.group(2)) + "px"


def convert_line(line: str) -> str:
    if any(s in line for s in LEGEND_LINES):
        return line if "/* legend */" in line else line.rstrip() + " /* legend */"
    for a, b in EXACT.items():
        line = line.replace(a, b)
    line = RGBA.sub(_rgba, line)
    line = HEXRE.sub(_hex, line)
    line = FONT_SIZE.sub(_size, line)
    return FONT_SHORT.sub(_size, line)


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else CSS
    changed = 0
    for f in sorted(root.glob("*.css")):
        if f.name == "theme.css":
            continue
        src = f.read_bytes().decode("utf-8")
        out = "\n".join(convert_line(l) for l in src.split("\n"))
        if out != src:
            f.write_bytes(out.encode("utf-8"))
            changed += 1
            print(f"rewrote {f.name}")
    print(f"{changed} files changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 5: Run the codemod twice (the second run proves it is idempotent)**

Run: `uv run python scripts/reskin_codemod.py`
Expected: eleven `rewrote …` lines (`asset-profile.css`, `browser.css`, `dock.css`, `dossier.css`, `imagery-viewer.css`, `naval.css`, `panels.css`, `shell.css`, `signals.css`, `terror.css`, `webcams.css`), then `11 files changed`.
Run it again. Expected: `0 files changed`.
Spot-check with `git diff frontend/css/terror.css`. For example, `.tw-st-confirmed { color: #ff6b78; border-color: rgba(255, 59, 78, .6); …` should now read `.tw-st-confirmed { color: var(--red-2); border-color: color-mix(in srgb, var(--red) 60%, transparent); …`. Line endings must be unchanged: the diff shows only the edited lines.

- [ ] **Step 6: Run the lint and expect exactly 10 manual findings**

Run: `uv run python scripts/check_css.py`
Expected:

```
shadow  browser.css:11 drop shadow y=24px
blur    browser.css:12 backdrop-filter
shadow  browser.css:47 drop shadow y=10px
shadow  browser.css:169 drop shadow y=8px
blur    browser.css:176 backdrop-filter
shadow  browser.css:178 drop shadow y=20px
shadow  shell.css:34 drop shadow y=16px
blur    shell.css:213 backdrop-filter
shadow  shell.css:213 drop shadow y=10px
shadow  shell.css:229 drop shadow y=24px
10 findings
```

- [ ] **Step 7: Remove the blur and drop shadows by hand**

`frontend/css/browser.css`. Delete these two lines entirely (lines 11–12):

```
  box-shadow: 0 24px 60px rgba(0, 0, 0, .55), inset 0 0 0 1px rgba(255, 255, 255, .015);
  backdrop-filter: blur(14px) saturate(1.1);
```

`frontend/css/browser.css`. This edit also makes the active pane border `--line-2`, as the spec requires:
- old: `.ds-pane.focus { border-color: rgba(var(--acc-rgb), .45); box-shadow: 0 0 0 1px rgba(var(--acc-rgb), .12), 0 10px 30px rgba(0, 0, 0, .35); }`
- new: `.ds-pane.focus { border-color: var(--line-2); box-shadow: none; }`

`frontend/css/browser.css`:
- old: `letter-spacing: .24em; box-shadow: 0 8px 24px rgba(0, 0, 0, .4); animation: rise`
- new: `letter-spacing: .24em; animation: rise`

`frontend/css/browser.css`:
- old: `background: color-mix(in srgb, var(--void) 92%, transparent); backdrop-filter: blur(6px); animation: rise`
- new: `background: color-mix(in srgb, var(--void) 92%, transparent); animation: rise`

`frontend/css/browser.css`:
- old: `#ds-lightbox img { max-width: 100%; max-height: 82vh; object-fit: contain; box-shadow: 0 20px 60px rgba(0, 0, 0, .6); }`
- new: `#ds-lightbox img { max-width: 100%; max-height: 82vh; object-fit: contain; border: 1px solid var(--line-2); }`

`frontend/css/shell.css`:
- old: `padding: 10px 12px 12px; background: var(--void); border: 1px solid var(--line-2); box-shadow: 0 16px 40px rgba(0, 0, 0, .6); text-transform: none;`
- new: `padding: 8px 12px 12px; background: var(--deep); border: 1px solid var(--line-2); border-radius: var(--radius); text-transform: none;`

`frontend/css/shell.css`:
- old: `border-left: 2px solid var(--sev, var(--acc)); box-shadow: 0 10px 28px rgba(0, 0, 0, .5); cursor: pointer; animation: slide-r var(--t-med) var(--ease) both; backdrop-filter: blur(8px); }`
- new: `border-left: 2px solid var(--sev, var(--acc)); cursor: pointer; animation: slide-r var(--t-med) var(--ease) both; }`

`frontend/css/shell.css`:
- old: `border-color: var(--line-2); box-shadow: 0 24px 60px rgba(0, 0, 0, .6), 0 0 0 1px rgba(var(--acc-rgb), .08); }`
- new: `border-color: var(--line-2); box-shadow: none; }`

- [ ] **Step 8: Lint, tests and build**

Run: `uv run python scripts/check_css.py`. Expected: `0 findings`.
Run: `uv run pytest tests/frontend -q`. Expected: all pass. `test_js_syntax` is skipped only if node is missing.
Run: `uv run python -m osint.frontend_build`. Expected: JSON summary, no error.

- [ ] **Step 9: Screenshot check**

Run `node scripts/shoot_views.mjs t3`. Open `data/screens/t3/06-dossier-intel.png` and `05-terror-tab.png`:
- no cyan text or borders remain in the dossier or the Terror pane;
- labels are larger (11px);
- nothing overflows its row.

If a dense label row clips, note the selector in the commit message: Tasks 6–8 restyle those areas. The known candidate is `.tl-k` (22px-wide timeline kind badge), which Task 8 widens.

- [ ] **Step 10: Delete the codemod, then commit**

```bash
rm scripts/reskin_codemod.py
git add scripts/check_css.py tests/frontend/test_theme.py frontend/css
git commit -m "feat(ui): move per-feature CSS onto theme tokens, 11px minimum, no blur or drop shadows"
```

---

### Task 4: Symbology reads the theme tokens

**Files:**
- Modify: `frontend/js/symbology.js:1-2` (insert the palette block), `:98-110` (EVENT_CATS), `:121-123` (EVENT_STATUS), `:147-156` (COLORS and threatColor)
- Modify: `frontend/js/events.js:3,8-14`, `frontend/js/layers.js:3-9,22`, `frontend/js/declutter.js:28,36,44-45`
- Modify: `frontend/index.html:100-105,112,132,133` (swatches)
- Modify: `frontend/css/shell.css` (`#ev-status .chip` rule)
- Test: `tests/frontend/test_theme.py` (append two node tests)

**Interfaces:**
- Consumes: the tokens from Task 2.
- Produces, exported from `/static/js/symbology.js`:
  - `PALETTE_FALLBACK: {[token: string]: string}`;
  - `cssColor(name: string): string`;
  - `cesiumColor(name: string, alpha?: number): Cesium.Color` (cached and shared, so do not mutate);
  - `catColor(cat: string): string`;
  - `statusColor(status: string): string`;
  - `threatLevelColor(level: number): string`.
- Unchanged shapes: `EVENT_CATS[k] = [label, cssColour, glyph]`, `EVENT_STATUS` (labels), `COLORS` (keys `air, airCiv, hp, ship, shipMil, fire, quake`) and `threatColor(kind, it): string`.
- When the palette is read: `symbology.js` evaluates at module load. Browsers run module scripts only after the `<head>` stylesheets have loaded, because the spec makes deferred and module scripts wait for script-blocking stylesheets. This holds for the source tree (13 `<link>`s) and for `dist` (one bundled `<link>` in the head). All consumers (`events.js`, `layers.js`, `declutter.js`, `app.js`, panels) are imported by `app.js`, so they evaluate after the stylesheets too. `PALETTE_FALLBACK` covers node and a missing stylesheet.

- [ ] **Step 1: Append the failing tests to `tests/frontend/test_theme.py`**

```python


def _node(prelude: str, body: str):
    src = prelude + f'\nconst S = await import("{SYM}");\nconst out = (() => {{ {body} }})();\nconsole.log(JSON.stringify(out));\n'
    r = subprocess.run([NODE, "--input-type=module"], input=src.encode(), capture_output=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


def _norm(v: str) -> str:
    return re.sub(r"\s+", "", v).lower()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_symbology_fallbacks_equal_theme_tokens():
    tokens = _load("check_contrast").parse_tokens(THEME.read_text(encoding="utf-8"))
    out = _node("", "return { fb: S.PALETTE_FALLBACK, cat: S.catColor('economic'), unknown: S.catColor('nope'),"
                    " arr: S.EVENT_CATS.economic[1], thr: S.threatColor('events', { category: 'economic' }),"
                    " st: S.statusColor('bogus'), hi: S.threatLevelColor(9), lo: S.threatLevelColor(-3), air: S.COLORS.air };")
    fb = out["fb"]
    for k, v in fb.items():
        assert k in tokens, f"{k} is not declared in theme.css"
        assert _norm(v) == _norm(tokens[k]), f"{k}: symbology.js {v} != theme.css {tokens[k]}"
    sym = {k for k in tokens if re.match(r"--(cat|st|thr|sym)-", k)}
    assert sym <= set(fb), f"missing from PALETTE_FALLBACK: {sorted(sym - set(fb))}"
    assert out["cat"] == out["arr"] == out["thr"] == fb["--cat-economic"]
    assert out["unknown"] == fb["--cat-other"]
    assert out["st"] == fb["--st-reported"]
    assert (out["hi"], out["lo"]) == (fb["--thr-4"], fb["--thr-0"])
    assert out["air"] == fb["--sym-air"]


MOCK_DOM = """
globalThis.document = { documentElement: {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: (k) => ({ "--cat-economic": "  #123456 ", "--line-2": " rgba(1, 2, 3, .4)" })[k] || "" });
globalThis.Cesium = { Color: { fromCssColorString: (s) => ({ s, withAlpha(a) { return { s: this.s, a }; } }) } };
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_symbology_reads_theme_tokens_and_caches_cesium_colours():
    out = _node(MOCK_DOM, "const a = S.cesiumColor('--cat-economic', 0.5); return { cat: S.catColor('economic'),"
                          " arr: S.EVENT_CATS.economic[1], line: S.cssColor('--line-2'), air: S.COLORS.air, ces: a,"
                          " same: a === S.cesiumColor('--cat-economic', 0.5), plain: S.cesiumColor('--cat-economic') };")
    assert out["cat"] == out["arr"] == "#123456"
    assert out["line"] == "rgba(1, 2, 3, .4)"
    assert out["air"] == "#8fe8f5"  # not set by the mock: falls back
    assert out["ces"] == {"s": "#123456", "a": 0.5} and out["same"] is True
    assert out["plain"] == {"s": "#123456"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/frontend/test_theme.py -k symbology -v`
Expected: 2 FAIL with `TypeError: S.catColor is not a function` in the node stderr.

- [ ] **Step 3: Insert the palette block into `frontend/js/symbology.js`**

Edit old string (line 2):

```js
// All glyphs are white strokes over a dark halo so Cesium's billboard colour tints them.
```

New:

```js
// All glyphs are white strokes over a dark halo so Cesium's billboard colour tints them.

// ---- palette: colours are CSS custom properties in css/theme.css (:root), read once at startup.
// Module scripts run after the <head> stylesheets have loaded (source tree and dist bundle alike),
// so getComputedStyle already sees theme.css. PALETTE_FALLBACK is used where there is no DOM (node
// tests) or a token is missing; it must equal theme.css (tests/frontend/test_theme.py).
export const PALETTE_FALLBACK = {
  "--void": "#0a0a08", "--fg": "#f2e6c9", "--amber": "#ffb000", "--acc": "#ffb000", "--ion": "#7fb8c9", "--red": "#ff4d2e",
  "--line-2": "rgba(255, 176, 0, .28)", "--globe-line": "rgba(255, 176, 0, .18)", "--globe-base": "#0c0b08",
  "--cat-armed_conflict": "#ff4d4d", "--cat-air_activity": "#8fd3ff", "--cat-maritime_security": "#5b8cff",
  "--cat-civil_unrest": "#ff7eb6", "--cat-disaster": "#ff7a1a", "--cat-infrastructure": "#45c9b0",
  "--cat-political": "#b69cff", "--cat-economic": "#7fd16b", "--cat-cyber_outage": "#e36bff", "--cat-other": "#a89f8a",
  "--st-reported": "#7d735f", "--st-multi-source": "#a39470", "--st-sensor-detected": "#c7ad6e",
  "--st-sensor-corroborated": "#e6c35c", "--st-official": "#ffe066",
  "--thr-0": "#8c8270", "--thr-1": "#c9a66b", "--thr-2": "#ff9f1c", "--thr-3": "#ff6a1f", "--thr-4": "#ff3b2b",
  "--sym-air": "#8fe8f5", "--sym-air-civ": "#bdb6a5", "--sym-hp": "#ff4d2e", "--sym-ship": "#6f9cff",
  "--sym-ship-mil": "#ff4d2e", "--sym-fire": "#ff7a1a", "--sym-quake": "#f2e45c",
};
let CS = null;
try { CS = getComputedStyle(document.documentElement); } catch (e) { /* no DOM (node tests) */ }
const readVar = (name) => (CS ? String(CS.getPropertyValue(name) || "").trim() : "");
const PAL = {};
for (const k of Object.keys(PALETTE_FALLBACK)) PAL[k] = readVar(k) || PALETTE_FALLBACK[k];
// CSS colour string of a theme token ("" if unknown). Values are fixed at startup: replay's --acc
// switch is CSS-only, so read "--amber" / "--ion" explicitly when a JS colour must follow a mode.
export function cssColor(name) {
  if (!Object.hasOwn(PAL, name)) PAL[name] = readVar(name);
  return PAL[name];
}
// Cesium.Color for a token (optionally with alpha), cached: callers share the instance, never mutate it.
const CESIUM_COLORS = new Map();
export function cesiumColor(name, alpha) {
  const key = name + "|" + (alpha == null ? "" : alpha);
  let c = CESIUM_COLORS.get(key);
  if (!c) {
    c = globalThis.Cesium.Color.fromCssColorString(cssColor(name) || "#ffffff");
    if (alpha != null) c = c.withAlpha(alpha);
    CESIUM_COLORS.set(key, c);
  }
  return c;
}
```

- [ ] **Step 4: Point the event categories at the tokens**

Ten exact replacements in `frontend/js/symbology.js`:

| old | new |
|---|---|
| `["Armed conflict", "#ff4b55",` | `["Armed conflict", cssColor("--cat-armed_conflict"),` |
| `["Air activity", "#9ff3ff",` | `["Air activity", cssColor("--cat-air_activity"),` |
| `["Maritime security", "#7fa8ff",` | `["Maritime security", cssColor("--cat-maritime_security"),` |
| `["Civil unrest", "#ffb020",` | `["Civil unrest", cssColor("--cat-civil_unrest"),` |
| `["Disaster", "#ff6a1a",` | `["Disaster", cssColor("--cat-disaster"),` |
| `["Infrastructure", "#d9b27c",` | `["Infrastructure", cssColor("--cat-infrastructure"),` |
| `["Political", "#a99bff",` | `["Political", cssColor("--cat-political"),` |
| `["Economic", "#4fdf8c",` | `["Economic", cssColor("--cat-economic"),` |
| `["Cyber / outage", "#e86bff",` | `["Cyber / outage", cssColor("--cat-cyber_outage"),` |
| `const EVENT_FALLBACK = ["Event", "#a99bff",` | `const EVENT_FALLBACK = ["Event", cssColor("--cat-other"),` |

Then:
- old: `export const eventCat = (c) => EVENT_CATS[c] || EVENT_FALLBACK;`
- new:

```js
export const eventCat = (c) => EVENT_CATS[c] || EVENT_FALLBACK;
// category colour (CSS string): --cat-<cat>, --cat-other for unknown categories
export const catColor = (cat) => eventCat(cat)[1];
```

- [ ] **Step 5: Add the status and threat getters after `EVENT_STATUS`**

Old:

```js
  "reported": "Reported", "multi-source": "Multi-source", "sensor-detected": "Sensor-detected", "sensor-corroborated": "Sensor-corroborated", "official": "Official",
};
```

New:

```js
  "reported": "Reported", "multi-source": "Multi-source", "sensor-detected": "Sensor-detected", "sensor-corroborated": "Sensor-corroborated", "official": "Official",
};
// status colour (CSS string): brightness rises reported -> official; unknown = reported
export const statusColor = (st) => cssColor("--st-" + (Object.hasOwn(EVENT_STATUS, st) ? st : "reported"));
// threat scale 0..4 (dim sand -> amber-orange -> red), level clamped and rounded
export const threatLevelColor = (level) => cssColor("--thr-" + Math.max(0, Math.min(4, Math.round(+level || 0))));
```

- [ ] **Step 6: Replace `COLORS` and the `threatColor` default**

- old: `export const COLORS = { air: "#9ff3ff", airCiv: "#c8d4de", hp: "#ff4b55", ship: "#7fa8ff", shipMil: "#ff4b55", fire: "#ff6a1a", quake: "#ffd24a" };`
- new:

```js
export const COLORS = {
  air: cssColor("--sym-air"), airCiv: cssColor("--sym-air-civ"), hp: cssColor("--sym-hp"), ship: cssColor("--sym-ship"),
  shipMil: cssColor("--sym-ship-mil"), fire: cssColor("--sym-fire"), quake: cssColor("--sym-quake"),
};
```

- old: `  return "#3ddcc2";\n}` (the last line of `threatColor`)
- new: `  return cssColor("--ion");\n}`

- [ ] **Step 7: Run the symbology tests**

Run: `uv run pytest tests/frontend/test_theme.py -v`
Expected: 4 passed.

- [ ] **Step 8: Switch the Cesium consumers to the cached colours**

`frontend/js/events.js`:
- old: `import { EVENT_CATS, eventCat, eventGlyph } from "/static/js/symbology.js";`
- new: `import { EVENT_CATS, eventGlyph, cesiumColor } from "/static/js/symbology.js";`

Old:

```js
for (const [k, v] of Object.entries(EVENT_CATS)) {
  COLOR[k] = C.Color.fromCssColorString(v[1]);
  COLOR_FAINT[k] = COLOR[k].withAlpha(0.5);
}
const DEFAULT = C.Color.fromCssColorString(eventCat("")[1]);
const DEFAULT_FAINT = DEFAULT.withAlpha(0.5);
```

New:

```js
for (const k of Object.keys(EVENT_CATS)) {
  COLOR[k] = cesiumColor("--cat-" + k);
  COLOR_FAINT[k] = cesiumColor("--cat-" + k, 0.5);
}
const DEFAULT = cesiumColor("--cat-other");
const DEFAULT_FAINT = cesiumColor("--cat-other", 0.5);
```

`frontend/js/layers.js`:
- old: `import { aircraftRole, aircraftGlyph, shipClass, shipGlyph, COLORS } from "/static/js/symbology.js";`
- new: `import { aircraftRole, aircraftGlyph, shipClass, shipGlyph, cesiumColor } from "/static/js/symbology.js";`

Old:

```js
const AIR_MIL = C.Color.fromCssColorString(COLORS.air);
const AIR_CIV = C.Color.fromCssColorString(COLORS.airCiv);
const AIR_RED = C.Color.fromCssColorString(COLORS.hp);
const SHIP_BLUE = C.Color.fromCssColorString(COLORS.ship);
const SHIP_RED = C.Color.fromCssColorString(COLORS.shipMil);
```

New:

```js
const AIR_MIL = cesiumColor("--sym-air");
const AIR_CIV = cesiumColor("--sym-air-civ");
const AIR_RED = cesiumColor("--sym-hp");
const SHIP_BLUE = cesiumColor("--sym-ship");
const SHIP_RED = cesiumColor("--sym-ship-mil");
```

- old: `const QUAKE = C.Color.fromCssColorString(COLORS.quake).withAlpha(0.85);`
- new: `const QUAKE = cesiumColor("--sym-quake", 0.85);`

`frontend/js/declutter.js`:
- old: `import { EVENT_CATS } from "/static/js/symbology.js";`
- new: `import { catColor, COLORS } from "/static/js/symbology.js";`
- old: `C.Color.fromCssColorString("#7fa8ff").withAlpha(0.22 + 0.4 * (i / 10))`
- new: `C.Color.fromCssColorString(COLORS.ship).withAlpha(0.22 + 0.4 * (i / 10))`

Old (two lines, 44–45):

```js
const catColor = (c) => (EVENT_CATS[c] || ["", "#a99bff"])[1];
const AIR_BADGE = "#9ff3ff", AIR_HOT = "#ff4b55";
```

New:

```js
const AIR_BADGE = COLORS.air, AIR_HOT = COLORS.hp;
```

- [ ] **Step 9: Confirm no stale references remain**

Run: `git grep -n "COLORS\.\|eventCat\|EVENT_CATS" -- frontend/js/layers.js frontend/js/events.js frontend/js/declutter.js`
Expected: only `events.js` `EVENT_CATS` uses (`Object.keys(EVENT_CATS)`, the `cats` Set and `Object.hasOwn(EVENT_CATS, …)`) and the two `COLORS.` lines in `declutter.js`. `layers.js` shows nothing.

- [ ] **Step 10: Status chip colours and the symbology swatches in `index.html`**

`frontend/css/shell.css`:
- old: `#ev-status .chip { --c: var(--fg-2); }`
- new:

```css
#ev-status .chip[data-k="reported"] { --c: var(--st-reported); }
#ev-status .chip[data-k="multi-source"] { --c: var(--st-multi-source); }
#ev-status .chip[data-k="sensor-detected"] { --c: var(--st-sensor-detected); }
#ev-status .chip[data-k="sensor-corroborated"] { --c: var(--st-sensor-corroborated); }
#ev-status .chip[data-k="official"] { --c: var(--st-official); }
```

`frontend/index.html`. Each old string is unique:

| old | new |
|---|---|
| `id="t-aircraft" checked><span class="sw gl" style="--c:#9ff3ff">` | `id="t-aircraft" checked><span class="sw gl" style="--c:var(--sym-air)">` |
| `id="t-aircraft-hp"><span class="sw gl" style="--c:#ff4b55">` | `id="t-aircraft-hp"><span class="sw gl" style="--c:var(--sym-hp)">` |
| `id="t-ships" checked><span class="sw hull" style="--c:#7fa8ff">` | `id="t-ships" checked><span class="sw hull" style="--c:var(--sym-ship)">` |
| `id="t-ships-mil"><span class="sw hull" style="--c:#ff4b55">` | `id="t-ships-mil"><span class="sw hull" style="--c:var(--sym-ship-mil)">` |
| `id="t-miltrails"><span class="sw ln" style="--c:#9ff3ff">` | `id="t-miltrails"><span class="sw ln" style="--c:var(--sym-air)">` |
| `id="t-models3d" checked><span class="sw gl" style="--c:#d3dee8">` | `id="t-models3d" checked><span class="sw gl" style="--c:var(--sym-air-civ)">` |
| `id="t-events" checked><span class="sw ring" style="--c:#a99bff">` | `id="t-events" checked><span class="sw ring" style="--c:var(--cat-other)">` |
| `id="t-fires" checked><span class="sw" style="--c:#ff6a1a">` | `id="t-fires" checked><span class="sw" style="--c:var(--sym-fire)">` |
| `id="t-quakes" checked><span class="sw ring" style="--c:#ffd24a">` | `id="t-quakes" checked><span class="sw ring" style="--c:var(--sym-quake)">` |

- [ ] **Step 11: Tests, build and look**

Run: `uv run pytest tests/frontend -q`. Expected: all pass.
Run: `uv run python -m osint.frontend_build`. Expected: no error.
Run: `node scripts/shoot_views.mjs t4`. Then check:
- `02-layer-tree.png`: the category chips are pink, teal, blue and so on, and no chip is amber;
- `01-globe.png`: event glyphs use the new category colours;
- `console-errors.txt` has no more errors than the `before` baseline.

- [ ] **Step 12: Commit**

```bash
git add frontend/js/symbology.js frontend/js/events.js frontend/js/layers.js frontend/js/declutter.js frontend/index.html frontend/css/shell.css tests/frontend/test_theme.py
git commit -m "feat(ui): symbology reads category/status/threat colours from theme tokens"
```

---

### Task 5: Globe look

**Files:**
- Modify: `frontend/js/theme/globe.js:3-4,35-38,124-129,177,215,221`
- Modify: `frontend/js/reference.js:4-6,112`
- Modify: `frontend/js/reticle.js:3,10-11,64`
- Modify: `frontend/index.html` (borders and graticule swatches)

**Interfaces:**
- Consumes: `cesiumColor(name, alpha?)` from Task 4.
- Produces: country borders and the graticule are drawn in `--globe-line`. Selection reticle colour: `--amber` when live, `--ion` in replay. Sub-project B draws the selected country outline with `cesiumColor("--line-2")`.

- [ ] **Step 1: `frontend/js/theme/globe.js` imports and GPU help colours**

- old: `import { makeImagery } from "/static/js/layers.js";`
- new:

```js
import { makeImagery } from "/static/js/layers.js";
import { cesiumColor } from "/static/js/symbology.js";
```

- old: `"background:rgba(3,6,10,.92);color:#cfe8ff;font:14px/1.5 'Segoe UI',sans-serif";`
- new: `"background:rgba(10,10,8,.94);color:#f2e6c9;font:14px/1.5 'Segoe UI',sans-serif";`
- old: `msg.style.cssText = "max-width:560px;padding:24px;border:1px solid #1f3a52;border-radius:6px";`
- new: `msg.style.cssText = "max-width:560px;padding:24px;border:1px solid #4a3a10;border-radius:2px";`

- [ ] **Step 2: Base colour, atmosphere and background**

Old:

```js
  // look: deep night-ocean base, atmosphere on
  globe.baseColor = C.Color.fromCssColorString("#07111c");
  globe.enableLighting = false;
  globe.showGroundAtmosphere = true;
  if (scene.skyAtmosphere) { scene.skyAtmosphere.show = true; scene.skyAtmosphere.brightnessShift = -0.25; scene.skyAtmosphere.saturationShift = -0.3; }
  scene.backgroundColor = C.Color.fromCssColorString("#03060a");
```

New:

```js
  // look: warm near-black base, subdued amber-tinted atmosphere (hueShift .5 turns the blue limb amber)
  globe.baseColor = cesiumColor("--globe-base");
  globe.enableLighting = false;
  globe.showGroundAtmosphere = true;
  globe.atmosphereHueShift = 0.5; globe.atmosphereSaturationShift = -0.6; globe.atmosphereBrightnessShift = -0.35;
  if (scene.skyAtmosphere) { scene.skyAtmosphere.show = true; scene.skyAtmosphere.hueShift = 0.5; scene.skyAtmosphere.brightnessShift = -0.45; scene.skyAtmosphere.saturationShift = -0.6; }
  scene.backgroundColor = cesiumColor("--void");
```

- [ ] **Step 3: Imagery tint (warmer, darker)**

Old:

```js
  // darken + desaturate so data pops (kept mild: never dim the base to near-black)
  base.brightness = 0.78; base.contrast = 1.1; base.saturation = 0.6; base.gamma = 1.0;
```

New:

```js
  // intel-console tint: darker, desaturated, hue nudged towards amber so data and the accent pop
  base.brightness = 0.55; base.contrast = 1.15; base.saturation = 0.35; base.hue = -0.03; base.gamma = 1.0;
```

- [ ] **Step 4: Amber graticule**

- old: `attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString(lat === 0 ? "#5ee3ea" : "#5ee3ea").withAlpha(lat === 0 ? 0.32 : 0.14)) } }));`
- new: `attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(lat === 0 ? cesiumColor("--amber", 0.32) : cesiumColor("--globe-line")) } }));`
- old: `attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString("#5ee3ea").withAlpha(lon === 0 ? 0.3 : 0.12)) } }));`
- new: `attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(lon === 0 ? cesiumColor("--amber", 0.3) : cesiumColor("--globe-line")) } }));`

- [ ] **Step 5: Country borders in `frontend/js/reference.js`**

- old: `import { airportMaxDist, portMaxDist } from "/static/js/lod.js";`
- new:

```js
import { airportMaxDist, portMaxDist } from "/static/js/lod.js";
import { cesiumColor } from "/static/js/symbology.js";
```

- old: `attributes: land ? { color: C.ColorGeometryInstanceAttribute.fromColor(col(CYAN, 0.5)) } : undefined,`
- new: `attributes: land ? { color: C.ColorGeometryInstanceAttribute.fromColor(cesiumColor("--globe-line")) } : undefined,`

- [ ] **Step 6: Selection reticle in `frontend/js/reticle.js`**

- old: `import { REDUCED } from "/static/js/ui.js";`
- new:

```js
import { REDUCED } from "/static/js/ui.js";
import { cesiumColor } from "/static/js/symbology.js";
```

Old:

```js
const ION = C.Color.fromCssColorString("#5ee3ea"), AMBER = C.Color.fromCssColorString("#f2ad3d"), TEAL = C.Color.fromCssColorString("#3ddcc2");
const HALO = C.Color.fromCssColorString("#ff4b55");
```

New:

```js
const ION = cesiumColor("--ion"), AMBER = cesiumColor("--amber"), TEAL = C.Color.fromCssColorString("#3ddcc2");
const HALO = cesiumColor("--sym-hp");
```

- old: `const col = replay ? AMBER : this.kind === "ref" ? TEAL : ION;`
- new: `const col = replay ? ION : this.kind === "ref" ? TEAL : AMBER; // amber = live selection, cyan = replay`

- [ ] **Step 7: Borders and graticule swatches in `frontend/index.html`**

| old | new |
|---|---|
| `id="r-borders" checked><span class="sw ln" style="--c:#36c5ff">` | `id="r-borders" checked><span class="sw ln" style="--c:var(--amber)">` |
| `id="t-grid"><span class="sw grid" style="--c:#5ee3ea">` | `id="t-grid"><span class="sw grid" style="--c:var(--amber)">` |

- [ ] **Step 8: Build and look at the globe**

Run: `uv run pytest tests/frontend -q && uv run python -m osint.frontend_build`. Expected: pass, no build error.
Run: `node scripts/shoot_views.mjs t5`. Open `01-globe.png` and compare it with `before/01-globe.png`. Check:
- the land is darker and browner, not greener;
- the ocean is near-black;
- the limb glow is dim amber, not blue;
- the borders are thin amber.

If the land reads greener or cooler, change `base.hue = -0.03` to `base.hue = 0.03` and re-shoot. Cesium's hue sign depends on the HSB conversion, so verify it visually. Record the chosen sign in the commit message.
If the borders are invisible at world zoom, check with the "Country borders" layer on: `.18` is the spec value. Do not raise it without a spec change.
Click any track: the reticle must be amber. Drag the timeline into replay and click: the reticle must be cyan.

- [ ] **Step 9: Commit**

```bash
git add frontend/js/theme/globe.js frontend/js/reference.js frontend/js/reticle.js frontend/index.html
git commit -m "feat(ui): warm dark globe, amber graticule/borders, amber live selection reticle"
```

---

### Task 6: Shell: command bar, rail, chips, tabs

**Files:**
- Modify: `frontend/css/shell.css` (lines around 5–7, 99, 116, 165–168, 185, 200, 209)
- Modify: `frontend/css/panels.css:49-52,59,97,161`

**Interfaces:**
- Consumes: tokens. `--c` stays the chip colour variable (set inline by `app.js:657` and `terror.js:479`, and by the `#ev-status` rules from Task 4).
- Produces:
  - chip states `.chip`, `.chip.on`, `.chip:disabled`;
  - tab states `.ptab.on`, `.seg button.on`, `.tabs .tab.on` (amber 2px underline);
  - Task 9 reuses `.seg` for the range tabs.

- [ ] **Step 1: Command bar (solid `--deep`, `--line` bottom edge, no glow filter, replay tag in accent)**

- old: `#cmdbar { top: var(--gap); left: var(--gap); right: var(--gap); height: var(--bar-h);`
- new: `#cmdbar.glass { background: var(--deep); border-width: 0 0 1px; }\n#cmdbar { top: var(--gap); left: var(--gap); right: var(--gap); height: var(--bar-h);`
- old: `stroke-width: 1.3; filter: drop-shadow(0 0 4px rgba(var(--acc-rgb), .5)); transition: stroke var(--t-med); }`
- new: `stroke-width: 1.3; transition: stroke var(--t-med); }`
- old: `#cmdbar .mode-pill.replay { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 60%, transparent); background: color-mix(in srgb, var(--amber) 10%, transparent); }`
- new: `#cmdbar .mode-pill.replay { color: var(--acc); border-color: rgba(var(--acc-rgb), .6); background: rgba(var(--acc-rgb), .1); }`

- [ ] **Step 2: Rail (solid, amber left bar instead of a fill)**

- old: `.rail-btn.on { border-left: 2px solid var(--acc); background: rgba(var(--acc-rgb), .08); }`
- new: `#rail.glass { background: var(--deep); }\n.rail-btn.on { border-color: transparent; border-left: 2px solid var(--acc); background: transparent; }`

- [ ] **Step 3: Chips (square, `--line` border; `.on` = 18 % `--c` fill, `--c` border, `--fg` text; disabled = dim and dashed)**

Old (lines 165–168):

```css
.chips { display: flex; flex-wrap: wrap; gap: 4px; padding: 2px 0 5px 21px; }
.chip { font: 600 11px/1 var(--f-mono); text-transform: uppercase; letter-spacing: .06em; padding: 3px 6px; border: 1px solid var(--c); color: var(--c); background: transparent; opacity: .4; }
.chip.on { opacity: 1; background: rgba(255, 255, 255, .05); }
.chip:hover { background: rgba(255, 255, 255, .08); }
```

New:

```css
.chips { display: flex; flex-wrap: wrap; gap: 4px; padding: 2px 0 8px 24px; }
.chip { font: 600 11px/1 var(--f-mono); text-transform: uppercase; letter-spacing: .04em; padding: 3px 6px; border: 1px solid var(--line); border-radius: 0; color: var(--fg-2); background: transparent; }
.chip.on { background: color-mix(in srgb, var(--c, var(--acc)) 18%, transparent); border-color: var(--c, var(--acc)); color: var(--fg); }
.chip:hover { border-color: var(--line-2); color: var(--fg); background: var(--raise); }
.chip.on:hover { background: color-mix(in srgb, var(--c, var(--acc)) 26%, transparent); }
.chip:disabled, .chip:disabled:hover { color: var(--dim); border-style: dashed; border-color: var(--line); background: transparent; opacity: 1; }
```

`frontend/css/panels.css`:
- old: `#wc-chips .chip:disabled { cursor: not-allowed; opacity: .2; }`
- new: `#wc-chips .chip:disabled { cursor: not-allowed; }`

- [ ] **Step 4: Remove the local focus rule and the HUD glows**

`frontend/css/shell.css`:
- old: `#left .grp > header:focus-visible { outline: 1px solid var(--acc); outline-offset: 2px; }\n` → new: an empty string (delete the line; the global `:focus-visible` glow from Task 2 applies).
- old: `.compass .needle { fill: var(--acc); filter: drop-shadow(0 0 3px rgba(var(--acc-rgb), .7)); }`
- new: `.compass .needle { fill: var(--acc); }`
- old: `.hud-btns button.on { color: var(--acc); border-color: rgba(var(--acc-rgb), .7); box-shadow: inset 0 0 8px rgba(var(--acc-rgb), .25); }`
- new: `.hud-btns button.on { color: var(--acc); border-color: rgba(var(--acc-rgb), .7); box-shadow: inset 0 -2px 0 var(--acc); }`

- [ ] **Step 5: Tabs (uppercase head font, amber 2px underline) in `frontend/css/panels.css`**

Old (lines 49–51):

```css
.ptab { height: 36px; padding: 0 12px; border: 0; border-bottom: 2px solid transparent; background: none; color: var(--dim); font-size: 11px; letter-spacing: .18em; }
.ptab:hover { background: rgba(var(--acc-rgb), .05); color: var(--fg); }
.ptab.on { color: var(--fg); border-bottom-color: var(--acc); background: linear-gradient(0deg, rgba(var(--acc-rgb), .1), transparent); box-shadow: none; }
```

New:

```css
.ptab { height: 36px; padding: 0 12px; border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: none; color: var(--dim); font: 600 11px/1 var(--f-head); letter-spacing: .08em; text-transform: uppercase; }
.ptab:hover { background: none; color: var(--fg); }
.ptab.on { color: var(--fg); border-bottom-color: var(--acc); background: none; }
```

- old: `.seg button { padding: 4px 8px; font-size: 11px; margin-left: -1px; }`
- new:

```css
.seg button { padding: 4px 8px; font: 600 11px/1 var(--f-head); letter-spacing: .08em; text-transform: uppercase; margin-left: -1px; border-radius: 0; border-color: var(--line); background: transparent; color: var(--dim); }
.seg button:hover { color: var(--fg); background: transparent; border-color: var(--line); }
.seg button.on { color: var(--fg); background: transparent; border-color: var(--line); box-shadow: inset 0 -2px 0 var(--acc); }
```

- old: `.tabs { margin-left: auto; display: flex; } .tabs .tab { padding: 4px 8px; font-size: 11px; margin-left: -1px; }`
- new: `.tabs { margin-left: auto; display: flex; } .tabs .tab { padding: 4px 8px; font-size: 11px; margin-left: -1px; border-radius: 0; }\n.tabs .tab.on { background: transparent; box-shadow: inset 0 -2px 0 var(--acc); }`

- [ ] **Step 6: Lint, build, look**

Run: `uv run python scripts/check_css.py && uv run python -m osint.frontend_build`. Expected: `0 findings`, no build error.
Run: `node scripts/shoot_views.mjs t6`. Check:
- `02-layer-tree.png`:
  - the active rail icon has an amber left bar and no fill;
  - the category chips are square;
  - an "on" chip has a tinted fill and a coloured border.
- `04-events-tab.png`: the EVENTS tab is uppercase with an amber underline.
- `05-terror-tab.png`: all six tabs fit.

- [ ] **Step 7: Commit**

```bash
git add frontend/css/shell.css frontend/css/panels.css
git commit -m "feat(ui): solid command bar and rail, square chips, underlined uppercase tabs"
```

---

### Task 7: Panels and inspector

**Files:**
- Modify: `frontend/css/panels.css:10,22-24,33-36,99,105-109`
- Modify: `frontend/css/shell.css:132` (`.grp > header b`)

**Interfaces:**
- Consumes: `--st-*` tokens from Task 2. `.pill.st-<status>` classes are emitted by `eventlist.js:20` `statusPill`.
- Produces: the section-header pattern `TITLE ··· count` (a dotted `::after` filler, the count right-aligned in mono). Task 8 reuses the same rule shape for `.dk-h`.

- [ ] **Step 1: Inspector header, no glows**

- old: `.dz-head::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; background: var(--threat); box-shadow: 0 0 10px var(--threat); }`
- new: `.dz-head::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; background: var(--threat); }`
- old: `.dz-glyph .gi { width: 30px; height: 30px; filter: drop-shadow(0 0 4px currentColor); }`
- new: `.dz-glyph .gi { width: 30px; height: 30px; }`

- [ ] **Step 2: Section headers `TITLE ··· count/status`**

- old: `.dz-sec { padding: 10px 14px 0; }`
- new: `.dz-sec { padding: 8px 16px 0; }`

Old:

```css
.dz-sec > h3 { margin-bottom: 6px; display: flex; gap: 8px; align-items: baseline; }
.dz-sec > h3 small { margin-left: auto; font: 11px var(--f-mono); letter-spacing: 0; text-transform: none; color: var(--dim); }
```

New:

```css
.dz-sec > h3 { margin-bottom: 8px; display: flex; gap: 8px; align-items: baseline; }
.dz-sec > h3::after, .grp > header::after { content: ""; flex: 1 1 auto; order: 1; min-width: 12px; border-bottom: 1px dotted var(--line-2); align-self: center; }
.dz-sec > h3 small { order: 2; font: 11px var(--f-mono); letter-spacing: 0; text-transform: none; color: var(--fg-2); }
```

`frontend/css/shell.css`:
- old: `.grp > header b { margin-left: auto; font-size: 11px; color: var(--acc); font-weight: 500; }`
- new: `.grp > header b, .grp > header .g-on { order: 2; }\n.grp > header b { font: 500 11px var(--f-mono); color: var(--acc); }`

- [ ] **Step 3: Key/value mono grid**

Old:

```css
.kv { display: grid; grid-template-columns: 96px 1fr; gap: 0; margin: 0; font-size: 12px; }
.kv span, .kv div { padding: 3.5px 0; border-bottom: 1px solid rgba(255, 255, 255, .035); }
.kv span { font: 600 11px/1.9 var(--f-head); letter-spacing: .14em; text-transform: uppercase; color: var(--dim); }
.kv div { font: 11.5px/1.5 var(--f-mono); color: var(--fg); word-break: break-word; }
```

New:

```css
.kv { display: grid; grid-template-columns: 104px 1fr; gap: 0 8px; margin: 0; font-size: 12px; }
.kv span, .kv div { padding: 4px 0; border-bottom: 1px solid var(--line); }
.kv span { font: 500 11px/1.7 var(--f-mono); letter-spacing: .02em; text-transform: uppercase; color: var(--dim); }
.kv div { font: 12px/1.5 var(--f-mono); color: var(--fg); word-break: break-word; }
```

- [ ] **Step 4: Status pills on the `--st-*` brightness ramp**

Old (lines 105–109):

```css
.pill { font: 600 11px/1 var(--f-head); letter-spacing: .12em; text-transform: uppercase; padding: 2px 5px; border: 1px solid var(--line-2); color: var(--fg-2); white-space: nowrap; }
.pill.st-multi-source { color: var(--ion); border-color: color-mix(in srgb, var(--ion) 50%, transparent); }
.pill.st-sensor-corroborated { color: var(--green); border-color: color-mix(in srgb, var(--green) 55%, transparent); background: color-mix(in srgb, var(--green) 8%, transparent); }
.pill.st-sensor-detected { color: var(--acc); border-color: var(--acc); }
.pill.st-official { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 55%, transparent); background: color-mix(in srgb, var(--amber) 8%, transparent); }
```

New:

```css
.pill { font: 600 11px/1 var(--f-head); letter-spacing: .08em; text-transform: uppercase; padding: 2px 5px; border: 1px solid var(--st, var(--line-2)); color: var(--st, var(--fg-2)); white-space: nowrap; }
.pill.st-reported { --st: var(--st-reported); border-style: dashed; }
.pill.st-multi-source { --st: var(--st-multi-source); }
.pill.st-sensor-detected { --st: var(--st-sensor-detected); }
.pill.st-sensor-corroborated { --st: var(--st-sensor-corroborated); }
.pill.st-official { --st: var(--st-official); background: color-mix(in srgb, var(--st-official) 10%, transparent); }
```

- [ ] **Step 5: Lint, build, look**

Run: `uv run python scripts/check_css.py && uv run python -m osint.frontend_build`. Expected: `0 findings`, no build error.
Run: `node scripts/shoot_views.mjs t7`. Check:
- `03-inspector-event.png`: section titles read `TITLE ········ meta`, with the meta right-aligned in mono and the key/value labels in mono;
- `04-events-tab.png`: the status pills get brighter from reported to official;
- `02-layer-tree.png`: the group headers read `TRACKS ······· 1,234`.

- [ ] **Step 6: Commit**

```bash
git add frontend/css/panels.css frontend/css/shell.css
git commit -m "feat(ui): inspector section headers, mono key/value grid, status brightness pills"
```

---

### Task 8: Dossier cards, pivot links, browser panes

**Files:**
- Modify: `frontend/css/dossier.css:12,19-23,30,37,43-48,71-72,75` and append pivot rules
- Modify: `frontend/css/browser.css:9,22,44,51`
- Modify: `frontend/css/asset-profile.css:84-85`

**Interfaces:**
- Consumes:
  - `--st-*`;
  - the `sec()` markup from `frontend/js/panels/dossier-kit.js:91`, which is `h3.dk-h > i.dk-tw, span, em.dk-tag?, small`;
  - pivot elements: `a[data-pivot]` (`terror.js:50`), `[data-pivot]` attributes (`intel-view.js:42`, `sensor-detection.js:13`) and `.ap-link` (`asset-profile.js:137`).
- Produces: `.dk-*` cards in the inspector style; pivots are amber with a dotted underline.

- [ ] **Step 1: Card surface: drop the grid backdrop, glows and the old status colours**

`frontend/css/dossier.css`:
- old: `.dk-root::before { content: ""; position: absolute; inset: 0; pointer-events: none; opacity: .5;`
- new: `.dk-root::before { content: none; position: absolute; inset: 0; pointer-events: none; opacity: .5;`

Old (lines 20–23):

```css
.dk-class.st-sensor-corroborated b { color: var(--green); box-shadow: 0 0 8px color-mix(in srgb, var(--green) 25%, transparent); }
.dk-class.st-sensor-detected b { color: var(--acc); }
.dk-class.st-official b { color: var(--amber); }
.dk-class.st-reported b { color: var(--fg-2); border-style: dashed; }
```

New:

```css
.dk-class.st-sensor-corroborated b { color: var(--st-sensor-corroborated); }
.dk-class.st-sensor-detected b { color: var(--st-sensor-detected); }
.dk-class.st-official b { color: var(--st-official); }
.dk-class.st-reported b { color: var(--st-reported); border-style: dashed; }
.dk-class.st-multi-source b { color: var(--st-multi-source); }
```

- old: `.dk-head::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; background: var(--threat); box-shadow: 0 0 10px var(--threat); }`
- new: `.dk-head::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; background: var(--threat); }`
- old: `.dk-gauge .fg { filter: drop-shadow(0 0 3px currentColor); }`
- new: `.dk-gauge .fg { filter: none; }`

Old (lines 44–48):

```css
.dk-chip.st-sensor-corroborated { color: var(--green); border-color: color-mix(in srgb, var(--green) 55%, transparent); background: color-mix(in srgb, var(--green) 8%, transparent); }
.dk-chip.st-sensor-detected { color: var(--acc); border-color: var(--acc); }
.dk-chip.st-multi-source { color: var(--ion); border-color: color-mix(in srgb, var(--ion) 50%, transparent); }
.dk-chip.st-official { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 55%, transparent); }
.dk-chip.st-reported { border-style: dashed; }
```

New:

```css
.dk-chip.st-sensor-corroborated { color: var(--st-sensor-corroborated); border-color: var(--st-sensor-corroborated); }
.dk-chip.st-sensor-detected { color: var(--st-sensor-detected); border-color: var(--st-sensor-detected); }
.dk-chip.st-multi-source { color: var(--st-multi-source); border-color: var(--st-multi-source); }
.dk-chip.st-official { color: var(--st-official); border-color: var(--st-official); background: color-mix(in srgb, var(--st-official) 10%, transparent); }
.dk-chip.st-reported { color: var(--st-reported); border-color: var(--st-reported); border-style: dashed; }
```

- [ ] **Step 2: Section headers (same pattern as the inspector)**

Old:

```css
.dk-sec { padding: 9px 14px 0; }
.dk-h { display: flex; gap: 7px; align-items: baseline; margin: 0 0 6px; padding-bottom: 4px; border-bottom: 1px solid rgba(var(--acc-rgb), .12); cursor: pointer; user-select: none; }
```

New:

```css
.dk-sec { padding: 8px 16px 0; }
.dk-h { display: flex; gap: 8px; align-items: baseline; margin: 0 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--line); cursor: pointer; user-select: none; }
.dk-h::after { content: ""; flex: 1 1 auto; order: 1; min-width: 12px; border-bottom: 1px dotted var(--line-2); align-self: center; }
```

- old: `.dk-h small { margin-left: auto; font: 11px var(--f-mono); letter-spacing: 0; text-transform: none; color: var(--dim); }`
- new: `.dk-h small { order: 2; font: 11px var(--f-mono); letter-spacing: 0; text-transform: none; color: var(--fg-2); }`

The timeline kind badge must fit 11px text:
- old: `.tl-e { display: grid; grid-template-columns: 44px 22px 1fr;`
- new: `.tl-e { display: grid; grid-template-columns: 44px 30px 1fr;`
- old: `letter-spacing: .06em; width: 22px; text-align: center;`
- new: `letter-spacing: .02em; width: 30px; text-align: center;`

- [ ] **Step 3: Append the pivot-link rules to the end of `frontend/css/dossier.css`**

```css

/* pivots: amber, dotted underline (dossier, intel pane, Terror Watch, asset profile) */
a[data-pivot], span[data-pivot], b[data-pivot], .ap-link { color: var(--acc); border-bottom: 1px dotted rgba(var(--acc-rgb), .6); text-decoration: none; cursor: pointer; }
a[data-pivot]:hover, span[data-pivot]:hover, b[data-pivot]:hover, .ap-link:hover { color: var(--acc); border-bottom-style: solid; }
```

`frontend/css/asset-profile.css` (it loads after `dossier.css`, so its own rules must agree):

Old:

```css
.ap-link { all: unset; cursor: pointer; font: 600 12.5px var(--f-body); color: var(--fg); border-bottom: 1px dotted var(--line-2); }
.ap-link:hover { color: var(--acc); border-bottom-color: var(--acc); }
```

New:

```css
.ap-link { all: unset; cursor: pointer; font: 600 12.5px var(--f-body); color: var(--acc); border-bottom: 1px dotted rgba(var(--acc-rgb), .6); }
.ap-link:hover { color: var(--acc); border-bottom-style: solid; }
```

- [ ] **Step 4: Browser panes (`--raise` title bars, `--deep` surfaces)**

`frontend/css/browser.css`:
- old: `  background: color-mix(in srgb, var(--void) 86%, transparent);`
- new: `  background: var(--deep);`
- old: `border-bottom: 1px solid var(--line); background: linear-gradient(180deg, rgba(var(--acc-rgb), .05), transparent); }`
- new: `border-bottom: 1px solid var(--line); background: var(--raise); }`
- old: `flex-direction: column; background: color-mix(in srgb, var(--void) 92%, transparent); border: 1px solid var(--line); container-type`
- new: `flex-direction: column; background: var(--deep); border: 1px solid var(--line); container-type`
- old: `cursor: grab; user-select: none; background: rgba(0, 0, 0, .25); }`
- new: `cursor: grab; user-select: none; background: var(--raise); }`

The active pane border was set to `--line-2` in Task 3.

- [ ] **Step 5: Lint, build, look**

Run: `uv run python scripts/check_css.py && uv run python -m osint.frontend_build`. Expected: `0 findings`, no build error.
Run: `node scripts/shoot_views.mjs t8`. Check `06-dossier-intel.png`:
- pane title bars are `--raise`;
- the focused pane has an amber `--line-2` border;
- pivot links are amber and dotted;
- section headers have the dotted leader;
- the timeline kind badges in `03-inspector-event.png` do not clip.

- [ ] **Step 6: Commit**

```bash
git add frontend/css/dossier.css frontend/css/browser.css frontend/css/asset-profile.css
git commit -m "feat(ui): dossier cards in inspector style, amber dotted pivots, raised pane title bars"
```

---

### Task 9: Timeline and histogram

**Files:**
- Modify: `frontend/index.html:279` (range select → tabs)
- Modify: `frontend/js/timeline.js:24,31,44` and add `_markRange`
- Modify: `frontend/app.js:704`
- Modify: `frontend/js/histogram.js:5-9,168,172-176,213-219`
- Modify: `frontend/css/dock.css:3,7,21,24-25` and append a rule

**Interfaces:**
- Consumes: `cssColor` (Task 4) and `.seg` button styles (Task 6).
- Produces:
  - range buttons `#tl-r-48h`, `#tl-r-7d`, `#tl-r-30d`, `#tl-r-1y` with `data-k` = a `RANGES` key, inside `#tl-range.seg`;
  - `Timeline._markRange(key: string): void`.
- `RANGES` and `setRange(key)` are unchanged, and `app.js:811-813` keeps calling `timeline.setRange`.

- [ ] **Step 1: Replace the range `<select>` in `frontend/index.html`**

- old: `<select id="tl-range" title="Timeline range"><option value="48h">48 h</option><option value="7d">7 d</option><option value="30d">30 d</option><option value="1y">1 y</option></select>`
- new: `<span id="tl-range" class="seg tl-range" role="group" aria-label="Timeline range"><button type="button" id="tl-r-48h" data-k="48h">48 h</button><button type="button" id="tl-r-7d" data-k="7d">7 d</button><button type="button" id="tl-r-30d" data-k="30d">30 d</button><button type="button" id="tl-r-1y" data-k="1y">1 y</button></span>`

- [ ] **Step 2: `frontend/js/timeline.js`**

Replace all (2 occurrences, lines 24 and 44):
- old: `$("tl-range").value = key;`
- new: `this._markRange(key);`

Then:
- old: `    $("tl-range").addEventListener("change", (e) => this.setRange(e.target.value));`
- new: `    $("tl-range").addEventListener("click", (e) => { const b = e.target.closest("button[data-k]"); if (b && !b.disabled) this.setRange(b.dataset.k); });`
- old: `  get label() { return RANGES[this.key][1]; }`
- new:

```js
  get label() { return RANGES[this.key][1]; }
  _markRange(key) {
    for (const b of document.querySelectorAll("#tl-range button[data-k]")) {
      const on = b.dataset.k === key;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
  }
```

Also update the header comment line 2:
- old: `// Live button, play/pause with speed. Replay mode re-tints the whole UI amber (body.replay) and`
- new: `// Live button, play/pause with speed. Replay mode switches the accent to cyan (body.replay) and`

- [ ] **Step 3: `frontend/app.js:704`, so that replay-unsupported still disables the range**

- old: `disable(["tl-live", "tl-play", "tl-speed", "tl-range", "tl-slider"], "Replay not supported by this backend version");`
- new: `disable(["tl-live", "tl-play", "tl-speed", "tl-r-48h", "tl-r-7d", "tl-r-30d", "tl-r-1y", "tl-slider"], "Replay not supported by this backend version");`

Run: `git grep -n "tl-range" -- frontend`
Expected: only `index.html` (the span), `timeline.js` (the click listener and the `#tl-range button` selector) and `dock.css` if present. No `.value` uses remain.

- [ ] **Step 4: `frontend/js/histogram.js`: amber bars, 11px labels, warm grid**

- old: `import { fetchJson } from "/static/js/api.js";`
- new:

```js
import { fetchJson } from "/static/js/api.js";
import { cssColor } from "/static/js/symbology.js";
```

- old: `const COL = { fires: "#ff6a1a", quakes: "#ffd24a", events: "#a99bff", hist: "#8f86c9", air: "#5ee3ea", sea: "#7fa8ff" };`
- new:

```js
// events (the dominant series) in amber; sensors / tracks keep their symbology colours; UCDP matches its layer swatch
const COL = { fires: cssColor("--sym-fire"), quakes: cssColor("--sym-quake"), events: cssColor("--amber"), hist: "#8f86c9", air: cssColor("--sym-air"), sea: cssColor("--sym-ship") };
```

Further replacements:

| old | new |
|---|---|
| `const top = 6, base = h - 11, ph = base - top;` | `const top = 6, base = h - 13, ph = base - top;` |
| `c.font = "9px Consolas, 'Cascadia Mono', monospace";` | `c.font = "11px 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";` |
| `c.fillStyle = major ? "rgba(159,176,192,.28)" : "rgba(159,176,192,.12)";` | `c.fillStyle = major ? "rgba(255,176,0,.24)" : "rgba(255,176,0,.10)";` |
| `if (label) { c.fillStyle = "rgba(159,176,192,.55)";` | `if (label) { c.fillStyle = "rgba(184,165,126,.85)";` |
| `c.fillStyle = "rgba(3,7,12,.55)";` | `c.fillStyle = "rgba(10,10,8,.6)";` |
| `c.fillStyle = "rgba(242,173,61,.08)";` | `c.fillStyle = "rgba(127,184,201,.08)";` |
| `c.fillStyle = "rgba(120,175,205,.25)";` | `c.fillStyle = "rgba(255,176,0,.28)";` |

- [ ] **Step 5: `frontend/css/dock.css`: replay in accent, glowing playhead**

| old | new |
|---|---|
| `#timeline.replay { border-color: color-mix(in srgb, var(--amber) 45%, transparent); }` | `#timeline.replay { border-color: rgba(var(--acc-rgb), .45); }` |
| `.dk-ctl .mode.replay { color: var(--amber); }` | `.dk-ctl .mode.replay { color: var(--acc); }` |
| `#tl-slider:focus-visible { outline: 1px solid var(--acc); outline-offset: 1px; }` | an empty string (delete the line; the global glow applies) |
| `#tl-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 11px; height: 100%; background: linear-gradient(90deg, transparent 5px, var(--acc) 5px, var(--acc) 6px, transparent 6px); border: 0; box-shadow: none; }` | `#tl-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 2px; height: 100%; background: var(--acc); border: 0; border-radius: 0; box-shadow: var(--glow); }` |
| `#tl-slider::-moz-range-thumb { width: 11px; height: 100%; border: 0; border-radius: 0; background: linear-gradient(90deg, transparent 5px, var(--acc) 5px, var(--acc) 6px, transparent 6px); }` | `#tl-slider::-moz-range-thumb { width: 2px; height: 100%; border: 0; border-radius: 0; background: var(--acc); box-shadow: var(--glow); }` |

Append to the end of `dock.css`:

```css
.dk-ctl .tl-range button { padding: 5px 8px; }
```

- [ ] **Step 6: Tests, lint, build, look**

Run: `uv run pytest tests/frontend -q && uv run python scripts/check_css.py && uv run python -m osint.frontend_build`. Expected: pass, `0 findings`, no build error.
Run: `node scripts/shoot_views.mjs t9`. Check `07-timeline-replay.png`:
- the histogram bars are amber;
- the playhead is a glowing 2px line in cyan, because replay switches `--acc`;
- the range shows four tab buttons, the active one underlined;
- the axis labels are 11px and not clipped;
- the mode pill reads `REPLAY` in cyan.

In a live browser, click `7 d`: the histogram re-bins and the tab underline moves. Reload: the range persists (localStorage `noctua.tl.range`).

- [ ] **Step 7: Commit**

```bash
git add frontend/index.html frontend/js/timeline.js frontend/app.js frontend/js/histogram.js frontend/css/dock.css
git commit -m "feat(ui): amber histogram, glowing playhead, timeline range as tabs"
```

---

### Task 10: UI fixes: footer clearance, empty/error states, focus, residual colours and fonts

**Files:**
- Modify: `frontend/index.html:56,60,123,303`
- Modify: `frontend/css/shell.css` (`#rail`, `#left`, `#hud` bottoms), `frontend/css/panels.css` (`#rightcol` bottom, `.tk-body:focus-visible`, and append state rules), `frontend/css/dock.css` (`#timeline` bottom), `frontend/css/browser.css` (`.dossier` bottom)
- Modify: `frontend/js/ui.js` (append `stateHtml`, `settleLoading`)
- Modify: `frontend/js/panels/evstats.js:3,12-13`, `frontend/js/signals/index.js:6,36`, `frontend/js/terror.js:16-19,22,25,98-99,419-424,447,451,454,480`, `frontend/js/eventlist.js:73`, `frontend/js/panels/sensor-detection.js:45`, `frontend/js/declutter.js:327`
- Modify: `desktop/ui/index.html:3-19`

**Interfaces:**
- Consumes: `--foot-h` (Task 2) and `cssColor` (Task 4).
- Produces, exported from `/static/js/ui.js`:
  - `stateHtml(kind: "loading" | "empty" | "err", title: string, detail?: string): string`;
  - `settleLoading(el: HTMLElement, ms: number, title: string): void`. It replaces a still-present `.state-loading` with an error state after `ms`.
- CSS: `.state`, `.state-loading`, `.state-empty`, `.state-err`.

- [ ] **Step 1: Footer clearance (11px attribution gets its own `--foot-h` strip)**

| file | old | new |
|---|---|---|
| shell.css | `left: var(--gap); bottom: calc(var(--dock-h) + var(--gap) * 2); width: 42px;` | `left: var(--gap); bottom: calc(var(--dock-h) + var(--gap) * 2 + var(--foot-h)); width: 42px;` |
| shell.css | `bottom: calc(var(--dock-h) + var(--gap) * 2); width: 266px;` | `bottom: calc(var(--dock-h) + var(--gap) * 2 + var(--foot-h)); width: 266px;` |
| shell.css | `#hud { right: var(--gap); bottom: var(--gap);` | `#hud { right: var(--gap); bottom: calc(var(--gap) + var(--foot-h));` |
| panels.css | `bottom: calc(var(--dock-h) + var(--gap) * 2); width: 360px;` | `bottom: calc(var(--dock-h) + var(--gap) * 2 + var(--foot-h)); width: 360px;` |
| dock.css | `right: calc(var(--gap) * 2 + 200px); bottom: var(--gap);` | `right: calc(var(--gap) * 2 + 200px); bottom: calc(var(--gap) + var(--foot-h));` |
| browser.css | `  bottom: calc(var(--dock-h) + var(--gap) * 2);` | `  bottom: calc(var(--dock-h) + var(--gap) * 2 + var(--foot-h));` |

Run: `git grep -n "var(--dock-h) + var(--gap) \* 2)" -- frontend/css`. Expected: no output.

`frontend/index.html`, line 303, puts the licences link first:
- old: `<footer id="attrib">Imagery Esri / NASA GIBS · `
- new: `<footer id="attrib"><a href="#" id="lic-open">Attributions &amp; licences</a> · Imagery Esri / NASA GIBS · `
- old: ` · Signals: adsb.fi, CelesTrak, IODA (Georgia Tech), NOAA SWPC, BfS (dl-de/by-2-0), Safecast · <a href="#" id="lic-open">Attributions &amp; licences</a></footer>`
- new: ` · Signals: adsb.fi, CelesTrak, IODA (Georgia Tech), NOAA SWPC, BfS (dl-de/by-2-0), Safecast</footer>`

- [ ] **Step 2: Add the state helpers to the end of `frontend/js/ui.js`**

```js

// List / tile states. A loading state must settle into empty or error: settleLoading() turns a
// still-present .state-loading inside el into an error after ms (the request timeout).
export const stateHtml = (kind, title, detail) =>
  '<div class="state state-' + kind + '"><b>' + esc(title) + "</b>" + (detail ? esc(detail) : "") + "</div>";
export function settleLoading(el, ms, title) {
  if (!el) return;
  setTimeout(() => {
    if (el.querySelector(".state-loading")) el.innerHTML = stateHtml("err", title, "No answer within " + Math.round(ms / 1000) + " s; retrying in the background.");
  }, ms);
}
```

Append the state styles to the end of `frontend/css/panels.css`:

```css
/* ---- list / tile states: loading settles into empty or error (ui.js stateHtml / settleLoading) ---- */
.state { padding: 16px; font: 12px/1.5 var(--f-body); color: var(--fg-2); }
.state b { display: block; margin-bottom: 4px; font: 600 11px/1.2 var(--f-head); letter-spacing: .08em; text-transform: uppercase; color: var(--fg); }
.state-loading { color: var(--dim); font-family: var(--f-mono); }
.state-err b { color: var(--red-2); }
#cmdbar .sp-card .state { padding: 2px 0; }
```

- [ ] **Step 3: The two popover tiles that could stay on "loading…"**

`frontend/index.html`:
- old: `<div id="ev-score" class="sp-card" aria-label="GDELT scorecard"><div class="none">loading…</div></div>`
- new: `<div id="ev-score" class="sp-card" aria-label="GDELT scorecard"><div class="state state-loading">Loading…</div></div>`
- old: `<div id="sig-pill" class="sp-card" aria-label="Signals &amp; space status"><div class="none">loading…</div></div>`
- new: `<div id="sig-pill" class="sp-card" aria-label="Signals &amp; space status"><div class="state state-loading">Loading…</div></div>`

`frontend/js/panels/evstats.js`:
- old: `import { $, esc } from "/static/js/ui.js";`
- new: `import { $, esc, stateHtml } from "/static/js/ui.js";`

Old:

```js
  const s = await fetchJson("/api/events/stats", 10000);
  const g = s.gdelt || {}, acc = g.accepted || {}, rej = g.rejected || {};
  const el = $("ev-score");
```

New:

```js
  const el = $("ev-score");
  let s;
  try { s = await fetchJson("/api/events/stats", 10000); } catch (e) {
    if (!el._h) el.innerHTML = stateHtml("err", "Scorecard unavailable", String((e && e.message) || e));
    throw e; // poll() still reports it
  }
  const g = s.gdelt || {}, acc = g.accepted || {}, rej = g.rejected || {};
```

`frontend/js/signals/index.js`:
- old: `import { $, esc, nf, ago, agoHtml, kv } from "/static/js/ui.js";`
- new: `import { $, esc, nf, ago, agoHtml, kv, settleLoading } from "/static/js/ui.js";`
- old: `    this.status = null; this.sel = null; this.alertSince = null;`
- new: `    this.status = null; this.sel = null; this.alertSince = null;\n    settleLoading($("sig-pill"), 15000, "Signals status unavailable");`

- [ ] **Step 4: Terror feed error state and explicit empty text**

`frontend/js/terror.js`:
- old: `import { $, esc, nf } from "/static/js/ui.js";`
- new: `import { $, esc, nf, stateHtml } from "/static/js/ui.js";`
- old: `import { sec, wireSections, chip } from "/static/js/panels/dossier-kit.js";`
- new: `import { sec, wireSections, chip } from "/static/js/panels/dossier-kit.js";\nimport { cssColor } from "/static/js/symbology.js";`

Old:

```js
    const [feed, g] = await Promise.all([fetchJson("/api/terror/feed?" + qs, 30000), this.global ? Promise.resolve(this.global) : fetchJson("/api/terror/board/global", 30000).catch(() => null)]);
```

New:

```js
    let feed, g;
    try {
      [feed, g] = await Promise.all([fetchJson("/api/terror/feed?" + qs, 30000), this.global ? Promise.resolve(this.global) : fetchJson("/api/terror/board/global", 30000).catch(() => null)]);
    } catch (e) {
      this.el.innerHTML = stateHtml("err", "Terror Watch feed unavailable", String((e && e.message) || e));
      return;
    }
```

- old: `'<div class="empty">Nothing in the window for these filters.</div>'`
- new: `stateHtml("empty", "No items in window", "Nothing matches these filters in the selected window.")`

`frontend/js/eventlist.js:73`:
- old: `<b>No events</b>`
- new: `<b>No events in window</b>`

- [ ] **Step 5: Residual per-feature colours in JS and the index.html terror swatch**

`frontend/js/terror.js`:

| old | new |
|---|---|
| `export const TW_STATUS = { confirmed: "#ff3b4e", reported: "#ff9b3d", possible: "#a99bff" };` | `export const TW_STATUS = { confirmed: cssColor("--red"), reported: cssColor("--thr-3"), possible: cssColor("--violet") || "#c792ea" };` |
| `attributed_by_authorities: "#5ee3ea",` | `attributed_by_authorities: cssColor("--ion"),` |
| `C.Color.fromCssColorString(c.n > 1 ? "#05080d" :` | `C.Color.fromCssColorString(c.n > 1 ? cssColor("--void") :` |
| `<i class="tw-dot sq" style="--c:#f2ad3d">` | `<i class="tw-dot sq" style="--c:var(--amber)">` |
| `<i class="tw-dot sq" style="--c:#5ee3ea">` | `<i class="tw-dot sq" style="--c:var(--ion)">` |
| `<i class="tw-dot sq" style="--c:#8fa3b8">` | `<i class="tw-dot sq" style="--c:var(--fg-2)">` |
| `data-k="' + k + '" style="--c:#8fa3b8">'` | `data-k="' + k + '" style="--c:var(--fg-2)">'` |

One more replacement on line 98, kept out of the table because it contains `||`:
- old: `C.Color.fromCssColorString(TW_STATUS[it.status] || "#a99bff")`
- new: `C.Color.fromCssColorString(TW_STATUS[it.status] || TW_STATUS.possible)`

`--violet` is not in `PALETTE_FALLBACK`. In the browser `cssColor` reads it from `theme.css`, and the `|| "#c792ea"` keeps node evaluation non-empty.

`frontend/index.html`:
- old: `id="tw-incidents" checked><span class="sw dm" style="--c:#ff3b4e">`
- new: `id="tw-incidents" checked><span class="sw dm" style="--c:var(--red)">`

- [ ] **Step 6: Residual JS font size and redundant focus rule**

`frontend/js/panels/sensor-detection.js:45`:
- old: `style="font:9px var(--f-mono);fill:var(--fg-2)"`
- new: `style="font:11px var(--f-mono);fill:var(--fg-2)"`

`frontend/css/panels.css`:
- old: `.tk-body:focus-visible { box-shadow: inset 0 0 0 1px var(--acc); }\n` → new: an empty string (delete the line).

`frontend/js/declutter.js:327`. The small cluster badges are drawn at 9px; raise them to 11px and grow the badge height to fit:
- old: `const fs = big ? 12 : 9, hgt = big ? 19 : 13;`
- new: `const fs = big ? 12 : 11, hgt = big ? 19 : 15;`

Run: `git grep -nE "font[-:][^;\"']*\b([5-9]|10)(\.[0-9])?px" -- frontend/js frontend/app.js`
Expected: no output.

- [ ] **Step 7: Tauri splash (`desktop/ui/index.html`): replace the whole `<style>` block**

```html
<style>
  html,body{margin:0;height:100%;background:#0a0a08;color:#f2e6c9;font:13px/1.5 "Segoe UI",sans-serif;overflow:hidden}
  .wrap{height:100%;display:grid;place-items:center}
  .box{text-align:center}
  .mark{width:96px;height:96px;margin:0 auto 22px;border:3px solid #ffb000;border-radius:50%;position:relative;box-shadow:0 0 24px rgba(255,176,0,.25)}
  .eye{position:absolute;top:36px;width:18px;height:18px;border:4px solid #ffb000;border-radius:50%}
  .eye::after{content:"";position:absolute;inset:5px;background:#ffb000;border-radius:50%}
  .l{left:18px}.r{right:18px}
  h1{font:600 18px/1 "Segoe UI",sans-serif;letter-spacing:.42em;margin:0 0 6px}
  h1 span{color:#ffb000}
  .sub{letter-spacing:.3em;font-size:11px;color:#6e6450;margin-bottom:26px}
  .bar{width:260px;height:2px;background:#181812;margin:0 auto 12px;overflow:hidden}
  .bar i{display:block;height:100%;width:40%;background:#ffb000;animation:s 1.4s ease-in-out infinite}
  @keyframes s{0%{transform:translateX(-100%)}100%{transform:translateX(260%)}}
  #msg{color:#b8a57e;font-size:12px;min-height:18px}
  #err{color:#ff8a70;font-size:12px;max-width:520px;margin:10px auto 0;white-space:pre-wrap}
</style>
```

- [ ] **Step 8: Tests, lint, build; keyboard-focus and states check**

Run: `uv run pytest tests/frontend -q && uv run python scripts/check_css.py && uv run python -m osint.frontend_build`. Expected: pass, `0 findings`, no build error.

Keyboard focus, with the `run` skill or a real browser at http://127.0.0.1:8765/:
1. Click the empty globe.
2. Press Tab repeatedly. Every focused control must show the amber 1px ring with a soft glow: search, sys pill, gear, rail buttons, layer group headers, checkboxes, tabs, list body, timeline buttons, slider.
3. Mouse clicks must not leave a ring.

States:
1. Stop the server (Ctrl+C).
2. Reload the page with it stopped. Then start the server again (`uv run osint run`) and open the system popover within 10 s: the GDELT scorecard and Signals tiles may show "Loading…". After 15 s with no answer they must show the red-titled "unavailable" state, not "Loading…".
3. In the Terror tab with the server stopped and a refresh triggered, the tab shows "Terror Watch feed unavailable".

Footer: the attribution line is fully readable at 11px under the dock, and "Attributions & licences" is first and clickable.

- [ ] **Step 9: Commit**

```bash
git add frontend desktop/ui/index.html
git commit -m "fix(ui): settled loading/empty/error states, global focus glow, 11px footer strip, residual tokens"
```

---

### Task 11: CRT scanline toggle

**Files:**
- Modify: `frontend/js/panels/settings.js:7,23-25`
- Modify: `frontend/css/theme.css` (after the vignette block)
- Modify: `scripts/shoot_views.mjs` (add a view)

**Interfaces:**
- Consumes: the settings popover (`installSettings`, `#cfg-pop`).
- Produces:
  - `body.fx-crt` class;
  - localStorage key `noctua.fx.crt` (`"1"` | `"0"`), off by default;
  - checkbox `#cfg-crt`.

- [ ] **Step 1: Add the overlay CSS to `frontend/css/theme.css`**

- old: `body.replay #vignette::after { opacity: 1; }`
- new:

```css
body.replay #vignette::after { opacity: 1; }
/* optional scanline overlay (Settings > Display); above the globe, under the panels, never takes input */
body.fx-crt::after { content: ""; position: fixed; inset: 0; z-index: 6; pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(0, 0, 0, .22) 0 1px, transparent 1px 3px); }
```

Run: `git grep -n "body::after" -- frontend/css`. Expected: no output, so the selector does not collide.

- [ ] **Step 2: Add the toggle in `frontend/js/panels/settings.js`**

- old: `const MAX_LIST = 60;`
- new: `const MAX_LIST = 60;\nconst CRT_KEY = "noctua.fx.crt";`

Old:

```js
    '<div class="row-note">Times show in this zone; hover any time for the exact UTC instant. Relative ages ("3m ago") are unaffected.</div>' +
    "</section>";
  const q = $("cfg-zone-q"), list = $("cfg-zones");
```

New:

```js
    '<div class="row-note">Times show in this zone; hover any time for the exact UTC instant. Relative ages ("3m ago") are unaffected.</div>' +
    "</section>" +
    '<section class="sp-sec"><h5>Display</h5>' +
    '<label class="row"><input type="checkbox" id="cfg-crt"><span>Scanline overlay <small>CRT effect · off by default</small></span></label>' +
    "</section>";
  const q = $("cfg-zone-q"), list = $("cfg-zones");
  const crt = $("cfg-crt");
  let crtOn = false;
  try { crtOn = localStorage.getItem(CRT_KEY) === "1"; } catch (e) { /* storage disabled */ }
  crt.checked = crtOn;
  document.body.classList.toggle("fx-crt", crtOn);
  crt.addEventListener("change", () => {
    document.body.classList.toggle("fx-crt", crt.checked);
    try { localStorage.setItem(CRT_KEY, crt.checked ? "1" : "0"); } catch (e) { /* storage disabled */ }
  });
```

The existing `pop` change listener ignores the checkbox because it only acts on `input[name="cfg-tz"]`.

- [ ] **Step 3: Add the view to the screenshot script**

`scripts/shoot_views.mjs`:
- old: `  ["08-settings", "(() => { window.__osint.timeline.goLive(); document.getElementById('cfg-btn').click(); })()"],`
- new:

```js
  ["08-settings", "(() => { window.__osint.timeline.goLive(); document.getElementById('cfg-btn').click(); })()"],
  ["09-crt", "(() => { document.getElementById('cfg-crt').click(); document.getElementById('cfg-btn').click(); })()"],
```

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/frontend -q && uv run python scripts/check_css.py && uv run python -m osint.frontend_build`. Expected: pass, `0 findings`, no build error.
Run: `node scripts/shoot_views.mjs t11`. Check:
- `08-settings.png` shows a "Display · Scanline overlay" checkbox, unchecked;
- `09-crt.png` shows fine horizontal scanlines over the globe and the panels' surroundings.

In a real browser with the overlay on: drag, zoom and click a track. Globe interaction must be unaffected, because `pointer-events: none`. Reload: the overlay persists. Untick it: it is gone.

- [ ] **Step 5: Commit**

```bash
git add frontend/js/panels/settings.js frontend/css/theme.css scripts/shoot_views.mjs
git commit -m "feat(ui): optional CRT scanline overlay toggle in Settings (off by default)"
```

---

### Task 12: Final verification

**Files:** none modified, unless a check fails. In that case fix it in the owning task's files and commit as `fix(ui): …`.

**Interfaces:**
- Consumes: everything above.
- Produces: `data/screens/before/*` and `data/screens/after/*` for the PR, plus the check outputs.

- [ ] **Step 1: Run every automated check**

Run: `uv run pytest tests/frontend -q`
Expected: all pass, including `test_theme.py`, `test_build.py` and `test_js_syntax.py` (every module, including `symbology.js`).

Run: `uv run python scripts/check_contrast.py | Select-Object -Last 1`
Expected: `140/140 checks passed`

Run: `uv run python scripts/check_css.py`
Expected: `0 findings`

Run: `git grep -n "backdrop-filter\|#5ee3ea\|94, 227, 234" -- frontend/css`
Expected: no output.

Run: `uv run python -m osint.frontend_build`
Expected: JSON with `"css_files": 13` and `"modules"` equal to the previous build's count. The new modules do not add files: `symbology.js` already existed.

- [ ] **Step 2: Production bundle in the browser**

Make sure `OSINT_FRONTEND` is unset (PowerShell: `Remove-Item Env:OSINT_FRONTEND -ErrorAction SilentlyContinue`), so the server serves `frontend/dist`. Restart `uv run osint run`.
Run: `node scripts/shoot_views.mjs after`
Expected: nine PNGs. `data/screens/after/console-errors.txt` has no entries beyond those in `data/screens/before/console-errors.txt`. Compare it with `Compare-Object (Get-Content data/screens/before/console-errors.txt) (Get-Content data/screens/after/console-errors.txt)`: no `=>` lines.

- [ ] **Step 3: Review the before/after pairs against the spec**

For each of `01`–`08`, open `before/` and `after/` side by side and confirm:
1. **Globe:** warm, dark; amber graticule and borders when on; no blue atmosphere.
2. **Layer tree:** solid panels; amber rail bar; square chips with tinted "on" fill; no category chip is amber.
3. **Inspector with an event:** `TITLE ··· meta` headers; mono key/value grid; status pill on the sand brightness ramp.
4. **Events tab:** uppercase tabs with an amber underline; rows readable at 11px; "No events in window" when filtered empty.
5. **Terror tab:** six tabs fit; status pills are red, orange and violet; no cyan.
6. **Dossier with the intel pane:** raised pane title bars; amber dotted pivots; `--line-2` active border.
7. **Timeline in replay:** amber bars; cyan glowing playhead; range tabs; `REPLAY` tag in cyan.
8. **Settings popover:** solid `--deep`, no shadow; time options and the Display/CRT row.

If anything looks wrong, fix it in the relevant task's file, re-run Steps 1–2, and commit.

- [ ] **Step 4: Tauri desktop shell, checked once**

The desktop app navigates to the same served UI (`desktop/src/main.rs:194`, `win.navigate(url)`), so it loads the same CSS bundle. With the backend running, start the shell: `cargo run --manifest-path desktop/Cargo.toml`.
Expected:
- the splash shows the amber owl mark and a sand-coloured "Starting backend…";
- the window then shows the same amber UI as `after/01-globe.png`.

If the Rust toolchain is not installed, record "desktop check skipped: no cargo" in the PR description instead.

- [ ] **Step 5: Prepare the PR attachments (no commit)**

The PR description lists the eight before/after pairs from `data/screens/before` and `data/screens/after` (attach the PNGs) and pastes the last five lines of `check_contrast.py` and the `0 findings` line of `check_css.py`. `data/` is gitignored, so do not commit the images.

---

## Self-review

**Spec coverage**

| Spec section | Task |
|---|---|
| §1 hard-coded colours: `symbology.js:98/121/149` | 4 |
| §1: `globe.js` | 5 |
| §1: inline `--c` swatches | 4, 5, 10 |
| §2 tokens table, with values verbatim | 2 |
| §2 no blur / no drop shadows | 2 (`theme.css`), 3 (browser, shell, lint rule) |
| §2 replay: cyan `--acc` and the `REPLAY` tag | 2, 6 (mode pill), 9 (timeline), 5 (reticle) |
| §2 type: mono data | 2 (`td`, `time`, `code`, `.num`) |
| §2 type: head titles at 11px uppercase .08em | 2, 6, 7 |
| §2 type: body 12px | 2 |
| §2 type: 11px minimum | 3 (codemod and lint), 9 (canvas), 10 (JS inline) |
| §2 optional CRT | 11 |
| §3 tokens, read once and cached | 2, 4 |
| §3 swatches as `var(--cat-…)` | 4 (symbology swatches; others kept literal by decision 2) |
| §3 palette rules and the contrast script | 2 |
| §4 imagery, atmosphere | 5 |
| §4 graticule and borders | 5 |
| §4 `--line-2` for B | 4/5 exports |
| §4 vignette tinted to `--void` | 2 |
| §5 command bar and rail | 6 |
| §5 chips | 6 |
| §5 tabs | 6 |
| §5 panels and inspector | 7 |
| §5 dossier | 8 |
| §5 browser | 3 (active border), 8 |
| §5 timeline | 9 |
| §5 per-feature CSS | 3 (codemod), 10 (JS residuals) |
| §5 Cesium widgets | 2 |
| §5 UI fixes: 8px spacing | 2 (`--gap: 8px`) and the padding touched in 6–8 |
| §5 UI fixes: empty/error states | 10 |
| §5 UI fixes: focus | 2, 6, 9, 10 |
| §5 UI fixes: scrollbars | 2 |
| §6 screenshots | 1, 12 |
| §6 contrast check | 2, 12 |
| §6 build and no console errors | every task, 12 |
| §6 Tauri | 10 (splash), 12 |
| §7 out of scope | respected: no layout or nav restructure; the footer strip is the minimal clearance the 11px rule forces (decision 7) |

**Placeholder scan.** No TBD or TODO. One step is a visual judgement, and it comes with an explicit rule: the hue sign in Task 5 Step 8, with the exact alternative value.

**Type and name consistency.** These names match across Tasks 4, 5, 9, 10 and 11 and the B interface list:
- `cssColor`, `cesiumColor(name, alpha?)`, `catColor`, `statusColor`, `threatLevelColor`, `PALETTE_FALLBACK`;
- `stateHtml(kind, title, detail?)`, `settleLoading(el, ms, title)`;
- `_markRange(key)`;
- `#tl-r-48h|7d|30d|1y`, `#cfg-crt`, `noctua.fx.crt`, `--foot-h`.

The `.state-loading` class is set in `index.html` (Task 10 Step 3) and checked by `settleLoading`. Test names are unique within `test_theme.py`.
