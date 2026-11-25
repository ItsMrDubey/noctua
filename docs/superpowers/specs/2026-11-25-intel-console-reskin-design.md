# Intel-Console Reskin (Amber Phosphor) — Design (Sub-project C)

**Date:** 2026-11-25
**Status:** Approved in brainstorming, awaiting spec review
**Goal:** Give the globe UI a new intel-console identity: matte near-black surfaces, an amber phosphor accent and data set in mono. Layout stays as it is. Fix the UI inconsistencies found on the way.
**Runs in parallel with:** A (backend only). **Before:** B (built directly in this look).

---

## 1. Current state
- Plain ES modules and Cesium, with no framework or toolchain. `backend/osint/frontend_build.py` concatenates the modules into `frontend/dist/`.
- Tokens live in `frontend/css/theme.css` (`:root`): the cyan "NOCTUA" glass look (`--ion #5ee3ea` as `--acc`, `--glass` rgba with blur).
- Colours hard-coded outside the tokens:
  - `frontend/js/symbology.js:98` `EVENT_CATS`, `:121` `EVENT_STATUS`, `:149` `threatColor`;
  - `frontend/js/theme/globe.js`, the imagery tint, atmosphere and graticule;
  - inline `style="--c:#hex"` on the layer-row swatches in `frontend/index.html` (about lines 100–230).

## 2. Tokens (`theme.css`)

The token **names** stay the same wherever possible, so existing `var(--…)` uses keep working. Only the values change, plus the new names listed below.

| Token | Value | Use |
|---|---|---|
| `--void` | `#0a0a08` | Page and globe surround |
| `--deep` | `#11110d` | Panels |
| `--raise` *(new)* | `#181812` | Raised cards, hover |
| `--glass` | `#11110dF2` | Same role, no blur |
| `--line` | `rgba(255,176,0,.14)` | Hairlines |
| `--line-2` | `rgba(255,176,0,.28)` | Emphasis lines, active borders |
| `--fg` | `#f2e6c9` | Primary text |
| `--fg-2` | `#b8a57e` | Secondary text |
| `--dim` | `#6e6450` | Labels, disabled |
| `--acc` / `--amber` | `#ffb000` | Accent; `--acc-rgb: 255,176,0` |
| `--red` | `#ff4d2e` | Alert, critical |
| `--green` | `#7fd16b` | OK, live |
| `--ion` | `#7fb8c9` | Info. Kept as a secondary cool hue |
| `--violet` | `#c792ea` | Special (space, cyber) |
| `--glow` *(new)* | `0 0 0 1px var(--acc), 0 0 8px rgba(var(--acc-rgb),.35)` | Focus and active selection only |
| `--radius` *(new)* | `2px` | All corners |

- `backdrop-filter` blur is removed everywhere, and drop shadows are removed.
- **Replay mode:** `body.replay` used to set the accent to amber, which is now the default. It sets `--acc` to `#7fb8c9` (cyan) instead and adds a `REPLAY` tag in the command bar, so replay is still clearly different from live.

**Type**
- `--f-mono` (JetBrains Mono, `font-variant-numeric: tabular-nums`) is used for every number, ID, coordinate, timestamp, code and table cell.
- `--f-head` (Bahnschrift) is used for section and panel titles: uppercase, `letter-spacing: .08em`, 11px.
- `--f-body` (Segoe UI Variable) stays the body font at 12px.
- The minimum font size is 11px. Audit and remove any `font-size` below 11px.

**Optional effect:** a scanline or noise overlay (`body.fx-crt`) behind a toggle in Settings. It is off by default and must never sit over the globe canvas's interaction layer (`pointer-events: none`).

## 3. Symbology palette (`symbology.js`)
- Move the category, status and threat colours to CSS custom properties (`--cat-<name>`, `--st-<name>`, `--thr-0..4`) declared in `theme.css`.
- `symbology.js` reads them once at startup with `getComputedStyle` and caches them for Cesium, which needs `Color` objects.
- Inline swatches in `index.html` become `style="--c:var(--cat-…)"`.
- Palette rules:
  - categories stay distinct from each other **and** from the amber accent (no category is amber; the accent marks selection);
  - each colour has at least 3:1 contrast against `--void` and the darkened globe;
  - the threat scale runs dim sand → amber-orange → red;
  - status uses brightness (reported → multi-source → sensor-corroborated → official = increasing luminance and saturation), so it still works in greyscale.
- The exact hex values are fixed in the plan, after a contrast check script run against the globe screenshot.

## 4. Globe (`theme/globe.js`)
- Imagery: warmer and darker. Brightness about 0.55, saturation about 0.35, hue shifted slightly towards amber.
- Atmosphere: subdued, with a warm tint.
- Graticule and country borders: `rgba(255,176,0,.18)`. The selected country uses `--line-2` and is used by B.
- The `#vignette` overlay is tinted to `--void`.

## 5. Components (CSS files in `frontend/css/`)

| Area | Changes |
|---|---|
| `shell.css` command bar and rail | Solid `--deep`, a 1px `--line` bottom edge. The active rail icon gets an amber left bar instead of a filled background. Clocks in mono. |
| `.chips` / `.chip` | Square, a 1px `--line` border. `.on` fills with `rgba(var(--c),.18)`, adds a `--c` border and uses `--fg` text. Disabled chips use `--dim` and a dashed border. |
| Tabs (`.ptab`, `.seg .tab`) | Uppercase head font. The active tab gets an amber 2px underline. |
| `panels.css` / inspector | Section headers in the `TITLE ··· count/status` pattern, with the count right-aligned in mono. Key/value rows use a mono grid. |
| `dossier.css` `.dk-*` | Same card style as the inspector. Pivot links are amber and dotted-underlined. |
| `browser.css` | Pane title bars in `--raise`. Active pane border in `--line-2`. |
| Timeline / histogram | Amber bars and a playhead with `--glow`. Range buttons styled as tabs. |
| Per-feature CSS (`dock`, `naval`, `infra`, `signals`, `terror`, `webcams`, `asset-profile`, `imagery-viewer`) | Replace hard-coded colours with tokens. No layout changes. |
| Cesium widgets | Override the needed selectors from `widgets.css` (info box, credits, toolbar) to match. |

**UI fixes included**
- Spacing on an 8px scale (`--gap` and its multiples). Remove one-off margins where touched.
- Every list and tile has an explicit empty state ("No events in window") and an error state. A loading state turns into one of those within the request timeout, so nothing stays stuck on "Loading…".
- Visible keyboard focus (`:focus-visible` → `--glow`) on every interactive element.
- Scrollbars are thin and in `--line-2`.

## 6. Verification
- A screenshot before and after of: the default globe, the layer tree, the inspector with an event, the Events tab, the Terror tab, the dossier browser with the intel pane, the timeline in replay, and the settings popover. Taken with the `run` skill or a headless browser, and attached to the PR.
- Contrast check: a small script computes WCAG ratios for the text tokens on `--void`, `--deep` and `--raise` (≥ 4.5:1 for body text, ≥ 3:1 for `--dim` labels used only on secondary text) and for the symbology colours (≥ 3:1).
- `python -m osint.frontend_build` succeeds, and the dist page loads with no console errors.
- The Tauri desktop shell (`desktop/ui/index.html`) loads the same CSS. Check it once.

## 7. Out of scope
- Layout, panel or navigation restructuring.
- A theme switcher or light mode.
- New features. The country context is B.
