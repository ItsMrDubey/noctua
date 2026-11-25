# Country Context Panel — Design (Sub-project B)

**Date:** 2026-11-25
**Status:** Approved in brainstorming, awaiting spec review
**Goal:** A per-country workspace showing every event that concerns a country. Two filters: **location** (inside / abroad) and **involvement** (domestic / foreign-involved). Plus category chips, a time window, a header strip, a counterpart breakdown, and globe sync.
**Depends on:** A (ISO2-normalised `events.country`, `actor_countries`, `foreign_involved`, `attributed`, `lifecycle`) and C (styling).

---

## 1. Definitions (for a selected country X)

| Filter | Value | Rule |
|---|---|---|
| Location | `inside` | `events.country = X` |
| | `abroad` | `events.country ≠ X` (or null) **and** `X ∈ actor_countries` |
| | `all` | inside ∪ abroad |
| Involvement | `domestic` | `attributed` and every known country (location + actors) = X |
| | `foreign` | some actor or location country ≠ X, among events in the location set |
| | `unattributed` | `inside` and not `attributed`, so involvement is unknown |
| | `all` | everything in the location set |

- **Defaults:** `location=all`, `involvement=all`.
- Unattributed events are **never** counted as domestic. They show under `all` and `unattributed` with a "no actor data" badge.
- Example: a Russian strike inside Ukraine is `inside + foreign` from Ukraine's side and `abroad + foreign` from Russia's.

## 2. Backend

### 2.1 `GET /api/events` (`api/events.py:155`) — new optional parameters
- `country`: ISO2. When it is given:
  - `location=inside|abroad|all` (default `all`);
  - `involvement=domestic|foreign|unattributed|all` (default `all`);
  - `counterpart`: ISO2, which keeps only events where this country appears as the location or an actor alongside X.
- Uses the `events(country)` index from A. `abroad` uses a `json_each(actor_countries)` subquery, bounded by `since`.
- When `country` is set, each returned event gets `ctx: {location: "inside"|"abroad", involvement: "domestic"|"foreign"|"unattributed"}` so the UI doesn't recompute it.
- Bad input: a `country` value that isn't a known ISO2 code returns 400 with `{"error": "unknown_country"}`.

### 2.2 `GET /api/country/{cc}/summary?since=…` (new `api/country.py`)

```json
{
  "cc": "UA",
  "name": "Ukraine",
  "threat": {"level": "...", "source": "...", "advisories": [...]},
  "counts": {"inside": 0, "abroad": 0, "domestic": 0, "foreign": 0, "unattributed": 0},
  "lifecycle": {"developing": 0, "active": 0, "cooling": 0, "closed": 0},
  "spark": [{"day": "2026-11-01", "n": 0}],
  "top_actors": [{"name": "...", "country": "RU", "n": 0}],
  "counterparts": [{"cc": "RU", "name": "Russia", "n": 0, "as_location": 0, "as_actor": 0}],
  "bbox": [w, s, e, n]
}
```

- `threat` and `advisories` reuse `terror/board.py:65` `country_board`. Only the threat-level and advisory sections are called. If that fails, `threat` is `null` and the panel shows "unavailable".
- `spark` always covers 30 days, regardless of `since`.
- `counterparts` counts how often each foreign ISO2 appears across foreign-involved events in the window, as location or as actor. Top 15.
- `bbox` comes from the country polygons already used by the reference layer and the IODA overlays, exposed through `core/countries.py`.

## 3. Frontend

### 3.1 Entry points
- **Palette** (`js/palette.js:96`): choosing a country opens the country pane, then flies the camera there. Today it only flies.
- **Globe:** Alt + click on land resolves the country under the cursor (the country polygons already loaded for the reference layer) and opens the pane. A plain click still selects entities and events. An Alt + click tooltip hint appears once.
- **Pivots:** `pivot({type: "country", cc})` is registered in `browser.js`. These existing links route to it: the terror board's country links (`terror.js:304-307,331,451`) and the intel `ctry` tile (`intel-view.js:643`), each through a "Country context" link. The terror board stays its own pane.

### 3.2 Pane `country` (new `js/panels/country-view.js`, registered with the dossier browser)

Layout, top to bottom:

1. **Header strip:**
   - flag emoji, ISO2 and name;
   - threat level badge plus advisory count (expandable);
   - a 30-day sparkline (inline SVG, amber line);
   - lifecycle counts as four small badges;
   - the top 3 actors as chips.
2. **Filter bar:**
   - Location segmented control `ALL | INSIDE | ABROAD` (the `.seg .tab` pattern), with counts from `summary.counts`.
   - Involvement segmented control `ALL | DOMESTIC | FOREIGN | UNATTRIB.`, with counts.
   - Time window `24H | 7D | 30D | CUSTOM`. Custom opens two date inputs, and the default is 7D.
   - Category chips using the existing `chipSet` (`app.js:657`) and the category colours from C. All are on by default.
3. **Counterpart breakdown:**
   - A horizontal bar list of `counterparts`, showing name, a bar, and the count in mono.
   - Clicking a counterpart sets `counterpart=` and shows a removable `UA ↔ RU` chip in the filter bar.
4. **Event list:**
   - The existing event-row rendering from `eventlist.js` plus a lifecycle badge and a `ctx` badge (`INSIDE·FOREIGN` etc.).
   - Clicking a row selects the event on the globe and opens its dossier in an adjacent pane.
   - Sorted by lifecycle (`developing` first), then by last update.
   - Results are paged at 200, with a "load more" button.

**State:**
- One object `{cc, location, involvement, window, cats, counterpart}` in the pane.
- It goes into the pane's URL or hash so it survives reloads and the dossier browser's history.
- The pane refreshes every 60 s while open, using the same cadence as the live event polling.

### 3.3 Globe sync
While the country pane has focus:
- The country outline is drawn in `--line-2` with a faint amber fill (about 6%).
- Event entities not in the current filtered set are dimmed to 25% opacity, through `js/events.js`'s existing filter hook (`events.js:28-32`) with a new predicate. They are not removed, so the context stays visible.
- On first open, the camera fits `summary.bbox` (with padding). It does not re-fit on filter changes.
- Closing the pane or pressing Esc restores the normal filter and removes the outline.

## 4. Error handling
- A failed summary request means the header and counterparts show an error state with a retry button. The event list loads independently.
- An empty result shows "No events for UA in 7D with these filters", with a one-click "Reset filters" button.
- A country with no polygon (e.g. a tiny territory) means no outline and a fly-to on the centroid instead.

## 5. Testing
- **Backend:**
  - Unit tests for the location and involvement SQL on a fixture DB covering: an inside+domestic event, an inside+foreign event, an abroad event, an unattributed event, and the RU→UA strike seen from both sides.
  - The `counterpart` filter.
  - 400 for an unknown country.
  - The summary shape, and `threat` as `null` when the terror board raises.
- **Frontend:**
  - Manual check of each entry point (palette, Alt + click, terror pivot, intel tile).
  - Each filter combination changes the list and the counts consistently.
  - Dimming and restore on the globe.
  - State survives a reload.
  - Screenshots in the PR.

## 6. Out of scope
- Comparing two countries side by side.
- Country risk scores beyond the existing threat level and posture.
- Track-based foreign presence, such as foreign military aircraft or ships in a country's airspace or waters. A possible follow-up using ADS-B `origin_country` and vessel flags.
