# Country Context Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-country dossier pane showing every event that concerns a country, filtered by location (inside / abroad), involvement (domestic / foreign / unattributed), time window, category and counterpart, with a header strip, counterpart bars, an event list and globe sync.

**Architecture:** The backend gets one shared SQL module (`osint/store/country_sql.py`) that defines the location / involvement / counterpart rules once. `SqliteStore.list_events` and two new store aggregates use it. `GET /api/events` gains `country` parameters plus a per-item `ctx`, and a new `api/country.py` serves the summary, the country outline and point-to-country lookup. The frontend registers a `country` pane through the dossier's existing pane registry (`window.__noctuaPanes`, the same way `terror.js` does it). Its state lives in the pane spec, which the dossier persists to localStorage. A pure state module (`country-state.js`) builds every query and label and is unit-tested under node. A `CountryGlobe` draws the outline and dims events through a new `EventLayer.setFocus` predicate.

**Tech Stack:** Python 3.12, FastAPI, SQLite (`json_each`), msgspec, pytest + FastAPI `TestClient`; plain ES modules with no toolchain (bundled by `backend/osint/frontend_build.py`), CesiumJS, node (for pure-module tests only).

## Global Constraints

- This plan runs **after sub-projects A (deterministic extraction) and C (intel-console reskin) are merged into `master`**. Task 0 checks their interfaces and stops if any is missing.
- It consumes these interfaces exactly:
  - `osint.core.countries.to_iso2(value, hint) -> str | None`
  - `osint.core.countries.name(iso2) -> str`
  - `osint.core.countries.bbox(iso2) -> tuple[w, s, e, n] | None`
  - `events` columns `country` (ISO2), `actor_countries` (JSON list of ISO2), `foreign_involved` (0/1), `attributed` (0/1) and `lifecycle` TEXT in `developing|active|cooling|closed`, plus an index on `events(country)`.
  - CSS tokens `--line-2`, `--acc`, `--acc-rgb`, `--raise`, `--f-mono`, `--glow` and `--cat-<category>`.
  - The category colours are whatever `frontend/js/symbology.js` exports as `EVENT_CATS` (today `EVENT_CATS[k] = [label, "#hex", svg]`). C may change how that table is filled, for example from `--cat-*` vars, but this plan only reads `EVENT_CATS[k][0]` and `EVENT_CATS[k][1]`.
- **Location and involvement rules for country X (spec §1):**
  - inside: `events.country = X`.
  - abroad: `events.country ≠ X` (or NULL) **and** `X ∈ actor_countries`.
  - domestic: `attributed` **and** every known country (location + actors) = X.
  - foreign: some actor or location country ≠ X.
  - unattributed: inside **and** not `attributed`.
  - Unattributed events are **never** domestic.
- **Defaults:** `location=all`, `involvement=all`, time window `7D`, all categories on.
- **Bad input:** a `country` or `counterpart` value that is not a known ISO2 code returns HTTP **400** with exactly `{"error": "unknown_country"}`.
- **Numbers from the spec:**
  - `spark` always covers **30** days.
  - `counterparts` holds the top **15**; the header shows the top **3** actors.
  - The event list pages at **200**, with "load more".
  - The pane refreshes every **60 s**.
  - Dimmed events are drawn at **25 %** opacity.
  - The country fill is about **6 %** amber, and the outline uses `--line-2`.
- **Frontend build subset** (`backend/osint/frontend_build.py`):
  - only single-line named imports, `import { a, b } from "/static/js/x.js";`;
  - only top-level `export function` / `export const` / `export class` declarations, starting at column 0;
  - no `export default`, no `export {}`, no dynamic `import()`, no import cycles.
- **Style:**
  - Python follows ruff with line length 100.
  - pytest runs with `filterwarnings = error`.
  - The minimum UI font size is 11px.
  - Colours come from C's tokens. Only fallbacks inside `getComputedStyle` reads may be literal.
- No new Python or JS dependencies.
- Shell commands are bash (Git Bash on Windows) and run from the repo root `C:\Users\ItsMr\Documents\Projects\OSINT`. Every Python command goes through `uv run`.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `backend/osint/store/country_sql.py` | create | The spec §1 rules as SQL fragments (`country_where`), the per-event `event_ctx`, `as_list`, the lifecycle ordering SQL |
| `backend/osint/store/sqlite.py` | modify | `list_events` gains `until`, `offset` and `country` / `location` / `involvement` / `counterpart`; new `country_events` and `country_spark` |
| `backend/osint/api/events.py` | modify | `/api/events` country params, `ctx`, `until`, `offset`, `ids`; helpers `country_code` and `unknown_country` |
| `backend/osint/terror/board.py` | modify | `threat_section(store, cc)`: the level and advisory parts of `country_board` only |
| `backend/osint/api/country.py` | create | `/api/country/{cc}/summary`, `/api/country/{cc}/shape`, `/api/country/at` |
| `backend/osint/api/m1.py` | modify | Registers `mount_country` |
| `backend/osint/reference/loader.py` | modify | `iso_a2` on `/api/reference/countries` items (for the palette) |
| `frontend/js/country-state.js` | create | Pure pane state: normalisation, query strings, category toggles, labels, sparkline path |
| `frontend/js/panels/country-view.js` | create | The `country` pane: header, filter bar, counterpart bars, event list |
| `frontend/js/country-globe.js` | create | Outline, dimming and fit for the active country pane |
| `frontend/js/country.js` | create | `installCountry(ctx)`: pane registration, globe sync wiring, Alt+click, `open(cc)` |
| `frontend/js/browser.js` | modify | Extra hooks for registered panes (`select`, `open`, `setSpec`), deferred rendering of restored registered panes, `noctua:dossier` notifications, `data-newpane` pivots |
| `frontend/js/eventlist.js` | modify | Exports `eventRowInner(it, extra)` (shared row markup) |
| `frontend/js/events.js` | modify | `EventLayer.setFocus(pred)` dims non-matching glyphs to 25 % |
| `frontend/js/palette.js` | modify | Choosing a country opens its pane, then flies there |
| `frontend/js/terror.js` | modify | "Country context" links on the boards and in the feed |
| `frontend/js/panels/intel-view.js` | modify | "Country context" link in the `ctry` tile |
| `frontend/app.js` | modify | Calls `installCountry(ctx)` |
| `frontend/css/country.css` | create | Pane styles (C tokens) |
| `frontend/index.html` | modify | Links `country.css` |
| `tests/country_fixture.py` | create | The shared event fixture (UA/RU sides) and a tiny country GeoJSON |
| `tests/store/test_country_sql.py` | create | SQL rules on the fixture DB |
| `tests/api/test_country_api.py` | create | `/api/events` country mode |
| `tests/api/test_country_summary.py` | create | Summary, threat null, shape, point lookup |
| `tests/terror/test_threat_section.py` | create | `board.threat_section` |
| `tests/frontend/test_country_state.py` | create | `country-state.js` under node |
| `tests/reference/test_reference.py` | modify | `iso_a2` on countries |

---

### Task 0: Preflight (A and C are merged)

**Files:**
- None modified. This task only checks.

**Interfaces:**
- Consumes: everything listed under Global Constraints.
- Produces: the branch `feat/country-context`, created from an up-to-date `master` with a green baseline.

- [ ] **Step 1: Branch from master**

```bash
git checkout master && git pull --ff-only && git checkout -b feat/country-context
```
Expected: `Switched to a new branch 'feat/country-context'`.

- [ ] **Step 2: Verify the backend interfaces from A**

```bash
uv run python - <<'EOF'
import pathlib
import tempfile

from osint.core import countries
from osint.store.sqlite import SqliteStore

assert countries.to_iso2("UA", hint="iso2") == "UA", "to_iso2(iso2) broken"
assert countries.to_iso2("ZZ", hint="iso2") is None, "to_iso2 must reject unknown codes"
assert isinstance(countries.name("UA"), str) and countries.name("UA"), "name() broken"
box = countries.bbox("UA")
assert box is None or len(box) == 4, "bbox() must return (w, s, e, n) or None"
s = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "preflight.sqlite")
cols = {r[1] for r in s._db.execute("PRAGMA table_info(events)")}
need = {"country", "actor_countries", "foreign_involved", "attributed", "lifecycle"}
assert need <= cols, f"missing events columns: {need - cols}"
first = [s._db.execute(f"PRAGMA index_info({r[1]})").fetchone()
         for r in s._db.execute("PRAGMA index_list(events)")]
assert any(f is not None and f[2] == "country" for f in first), "no index on events(country)"
s.close()
print("backend preflight OK")
EOF
```
Expected: `backend preflight OK`. If an assertion fails, **STOP**: report which interface is missing and do not continue. Sub-project A is not merged, or it differs from the interface this plan assumes.

- [ ] **Step 3: Verify the frontend interfaces from C**

```bash
for t in --line-2 --acc --acc-rgb --raise --f-mono --glow; do grep -q -- "$t:" frontend/css/theme.css || echo "MISSING token $t"; done
grep -c -- "--cat-" frontend/css/theme.css
grep -nE "^export const (EVENT_CATS|eventCat)|^export function threatColor" frontend/js/symbology.js
```
Expected:
- no `MISSING` lines;
- a `--cat-` count of at least 9;
- three `symbology.js` lines: `EVENT_CATS`, `eventCat` and `threatColor`.

If any is missing, **STOP** and report it.

- [ ] **Step 4: Green baseline**

```bash
uv run pytest tests/api tests/store tests/terror tests/frontend tests/reference -q
```
Expected: all pass, with node-dependent tests skipped if node is absent. If anything fails on a clean `master`, **STOP** and report it. Do not build on a red baseline.

---

### Task 1: Country filter rules in the store

**Files:**
- Create: `backend/osint/store/country_sql.py`
- Modify: `backend/osint/store/sqlite.py`:
  - imports at the top (around lines 9-10);
  - `list_events` (around lines 498-548);
  - new methods after `country_tempo` (around line 628).
- Create: `tests/country_fixture.py`
- Test: `tests/store/test_country_sql.py`

**Interfaces:**
- Consumes: the A columns `country`, `actor_countries`, `attributed` and `lifecycle`.
- Produces:
  - `country_sql.country_where(cc: str, location: str = "all", involvement: str = "all", counterpart: str | None = None) -> tuple[list[str], list[object]]`
  - `country_sql.event_ctx(cc: str, country: str | None, actor_countries, attributed) -> {"location": "inside"|"abroad", "involvement": "domestic"|"foreign"|"unattributed"}`
  - `country_sql.as_list(value) -> list[str]`
  - `country_sql.COUNTRY_COLUMNS = ("actor_countries", "attributed", "lifecycle")`
  - `country_sql.LIFECYCLE_RANK_SQL`
  - `SqliteStore.list_events(..., until: datetime | None = None, offset: int = 0, country: str | None = None, location: str = "all", involvement: str = "all", counterpart: str | None = None) -> list[dict]`. In country mode each item also carries `actor_countries` (list), `attributed`, `lifecycle` and `ctx`. Items are ordered by lifecycle (developing first), then `updated_at` DESC.
  - `SqliteStore.country_events(cc, *, since: datetime, until: datetime | None = None, category: str | None = None) -> list[dict]`. Each dict has keys `id`, `country`, `actor_countries`, `attributed`, `lifecycle`, `actors` and `ctx`.
  - `SqliteStore.country_spark(cc, since: datetime) -> list[tuple[str, int]]`
  - `tests/country_fixture.py`: `T0`, `build_store(path) -> SqliteStore` and `write_geojson(path) -> Path`.

- [ ] **Step 1: Write the shared fixture**

Create `tests/country_fixture.py`:

```python
"""Shared fixture for the country context tests (spec B §5).

Seven events seen from Ukraine and Russia. Sub-project A's columns (actor_countries,
foreign_involved, attributed, lifecycle) and the actors are written with SQL, so the fixture
does not depend on A's write path.

  E1  UA, actors UA          inside + domestic for UA
  E2  UA, actors RU + UA     Russian strike in Ukraine: UA inside+foreign, RU abroad+foreign
  E3  RU, actors UA          Ukrainian strike in Russia: UA abroad+foreign, RU inside+foreign
  E4  UA, no actors          unattributed (category disaster)
  E5  FR, actors FR          unrelated
  E6  PL, actors PL + UA     UA abroad+foreign, counterpart PL
  H1  UA, actors UA          historical (UCDP): never in the country view
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from osint.store.sqlite import SqliteStore, iso

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

# id: (country, actor ISO2s, lifecycle, minutes before T0 (updated_at), category, actors)
CASES = {
    "E1": ("UA", ["UA"], "developing", 10, "armed_conflict",
           [{"name": "Ukrainian police", "country": "UA"}]),
    "E2": ("UA", ["RU", "UA"], "active", 20, "armed_conflict",
           [{"name": "Russian forces", "country": "RU"},
            {"name": "Ukrainian army", "country": "UA"}]),
    "E3": ("RU", ["UA"], "cooling", 30, "armed_conflict",
           [{"name": "Ukrainian army", "country": "UA"}]),
    "E4": ("UA", [], "closed", 40, "disaster", []),
    "E5": ("FR", ["FR"], "active", 50, "armed_conflict",
           [{"name": "French police", "country": "FR"}]),
    "E6": ("PL", ["PL", "UA"], "active", 60, "armed_conflict",
           [{"name": "Polish police", "country": "PL"},
            {"name": "Ukrainian army", "country": "UA"}]),
    "H1": ("UA", ["UA"], "closed", 70, "armed_conflict",
           [{"name": "Ukrainian army", "country": "UA"}]),
}


def _row(eid: str, country: str, minutes: int, category: str) -> dict:
    t = T0 - timedelta(minutes=minutes)
    return {"id": eid, "category": category, "title": f"Event {eid}", "summary": "s",
            "actors": [], "lat": 48.0, "lon": 31.0, "precision": "city", "place_name": "P",
            "country": country, "geo_confidence": 0.9, "h3_3": "831196fffffffff",
            "t_start": iso(t - timedelta(hours=1)), "t_end": iso(t), "status": "multi-source",
            "confidence": 0.9, "version": 2, "mention_count": 2, "source_diversity": 2,
            "gdelt_ids": [], "first_seen": iso(t), "updated_at": iso(t), "cluster_state": {}}


def build_store(path: Path) -> SqliteStore:
    s = SqliteStore(path)
    s.save_events([_row(eid, c[0], c[3], c[4]) for eid, c in CASES.items()])
    with s._lock, s._db:
        for eid, (cc, acs, life, _m, _cat, actors) in CASES.items():
            s._db.execute(
                "UPDATE events SET country=?, actors=?, actor_countries=?, foreign_involved=?, "
                "attributed=?, lifecycle=? WHERE id=?",
                (cc, json.dumps(actors), json.dumps(sorted(acs)),
                 int(any(a != cc for a in acs)), int(bool(acs)), life, eid))
        s._db.execute("UPDATE events SET origin='historical' WHERE id='H1'")
    return s


def write_geojson(path: Path) -> Path:
    """Two square 'countries': UA (with a hole) and PL (a MultiPolygon)."""
    feats = [
        {"type": "Feature",
         "properties": {"ISO_A2": "UA", "NAME": "Ukraine", "LABEL_X": 31.0, "LABEL_Y": 48.0},
         "geometry": {"type": "Polygon", "coordinates": [
             [[22.0, 44.0], [40.0, 44.0], [40.0, 52.0], [22.0, 52.0], [22.0, 44.0]],
             [[30.0, 46.0], [32.0, 46.0], [32.0, 47.5], [30.0, 47.5], [30.0, 46.0]]]}},
        {"type": "Feature",
         "properties": {"ISO_A2": "PL", "NAME": "Poland", "LABEL_X": 19.0, "LABEL_Y": 52.0},
         "geometry": {"type": "MultiPolygon", "coordinates": [[
             [[14.0, 49.0], [21.9, 49.0], [21.9, 55.0], [14.0, 55.0], [14.0, 49.0]]]]}},
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                    encoding="utf-8")
    return path
```

- [ ] **Step 2: Write the failing store tests**

Create `tests/store/test_country_sql.py`:

```python
"""Country context rules (spec B §1) on the fixture DB: location, involvement, counterpart,
ctx from both sides, ordering, window/paging and the summary aggregates."""

from datetime import timedelta

import pytest

from osint.store.country_sql import as_list, event_ctx
from tests.country_fixture import T0, build_store

INSIDE_DOM = {"location": "inside", "involvement": "domestic"}
INSIDE_FOR = {"location": "inside", "involvement": "foreign"}
ABROAD_FOR = {"location": "abroad", "involvement": "foreign"}
INSIDE_UNATTR = {"location": "inside", "involvement": "unattributed"}


@pytest.fixture
def store(tmp_path):
    s = build_store(tmp_path / "e.sqlite")
    yield s
    s.close()


def ids(items):
    return [it["id"] for it in items]


def test_event_ctx_rules():
    assert event_ctx("UA", "UA", ["UA"], 1) == INSIDE_DOM
    assert event_ctx("UA", "UA", ["RU", "UA"], 1) == INSIDE_FOR
    assert event_ctx("RU", "UA", ["RU", "UA"], 1) == ABROAD_FOR
    assert event_ctx("UA", "UA", [], 0) == INSIDE_UNATTR
    assert event_ctx("UA", "UA", "[]", None) == INSIDE_UNATTR  # never domestic
    # location unknown, only X acts: every KNOWN country is X
    assert event_ctx("UA", None, ["UA"], 1) == {"location": "abroad", "involvement": "domestic"}
    assert as_list('["RU","UA"]') == ["RU", "UA"]
    assert as_list(None) == [] and as_list("bad json") == [] and as_list(["UA", None]) == ["UA"]


def test_location_sets_and_order(store):
    def lst(**kw):
        return ids(store.list_events(country="UA", **kw))

    # lifecycle first (developing, active, cooling, closed), then latest update;
    # E5 is unrelated and H1 is historical
    assert lst() == ["E1", "E2", "E6", "E3", "E4"]
    assert set(lst(location="inside")) == {"E1", "E2", "E4"}
    assert set(lst(location="abroad")) == {"E3", "E6"}


def test_involvement_sets(store):
    def lst(**kw):
        return ids(store.list_events(country="UA", **kw))

    assert lst(involvement="domestic") == ["E1"]
    assert set(lst(involvement="foreign")) == {"E2", "E3", "E6"}
    assert lst(involvement="unattributed") == ["E4"]
    assert lst(location="inside", involvement="foreign") == ["E2"]
    assert lst(location="abroad", involvement="domestic") == []
    assert lst(location="abroad", involvement="unattributed") == []


def test_strike_seen_from_both_sides(store):
    ua = {it["id"]: it["ctx"] for it in store.list_events(country="UA")}
    assert ua == {"E1": INSIDE_DOM, "E2": INSIDE_FOR, "E3": ABROAD_FOR, "E4": INSIDE_UNATTR,
                  "E6": ABROAD_FOR}
    ru = {it["id"]: it["ctx"] for it in store.list_events(country="RU")}
    assert ru == {"E2": ABROAD_FOR, "E3": INSIDE_FOR}
    assert ids(store.list_events(country="RU", involvement="domestic")) == []
    first = store.list_events(country="UA")[0]
    assert first["lifecycle"] == "developing" and first["actor_countries"] == ["UA"]
    assert first["attributed"] == 1
    assert "ctx" not in store.list_events()[0]  # plain list: unchanged


def test_counterpart(store):
    assert set(ids(store.list_events(country="UA", counterpart="RU"))) == {"E2", "E3"}
    assert ids(store.list_events(country="UA", counterpart="PL")) == ["E6"]
    assert ids(store.list_events(country="UA", counterpart="FR")) == []


def test_window_paging_and_category(store):
    since, until = T0 - timedelta(minutes=35), T0 - timedelta(minutes=15)
    assert ids(store.list_events(country="UA", since=since, until=until)) == ["E2", "E3"]
    assert ids(store.list_events(country="UA", limit=2)) == ["E1", "E2"]
    assert ids(store.list_events(country="UA", limit=2, offset=2)) == ["E6", "E3"]
    assert "E4" not in ids(store.list_events(country="UA", category="armed_conflict"))


def test_country_events_and_spark(store):
    rows = store.country_events("UA", since=T0 - timedelta(days=1))
    assert {r["id"] for r in rows} == {"E1", "E2", "E3", "E4", "E6"}
    e2 = next(r for r in rows if r["id"] == "E2")
    assert e2["actor_countries"] == ["RU", "UA"] and e2["ctx"] == INSIDE_FOR
    assert e2["lifecycle"] == "active" and e2["actors"][0]["name"] == "Russian forces"
    only = store.country_events("UA", since=T0 - timedelta(days=1), category="disaster")
    assert [r["id"] for r in only] == ["E4"]
    assert store.country_events("UA", since=T0 - timedelta(minutes=15)) != []
    assert store.country_spark("UA", T0 - timedelta(days=30)) == [("2026-11-21", 5)]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/store/test_country_sql.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'osint.store.country_sql'`.

- [ ] **Step 4: Write `country_sql.py`**

Create `backend/osint/store/country_sql.py`:

```python
"""The country context rules (spec B §1) as SQL fragments over ``events``, plus the same
rules per event in Python (``event_ctx``) so the API never makes the UI recompute them.

For a selected country X (ISO 3166-1 alpha-2):

  location     inside        events.country = X
               abroad        events.country is not X (or NULL) and X is in actor_countries
  involvement  domestic      attributed and every known country (location + actors) = X
               foreign       some actor or location country != X
               unattributed  inside and not attributed (involvement unknown)

Unattributed events are never domestic. Historical (curated UCDP) rows are excluded."""

from __future__ import annotations

import json

LOCATIONS = ("all", "inside", "abroad")
INVOLVEMENTS = ("all", "domestic", "foreign", "unattributed")
# columns the country view adds to the slim list rows
COUNTRY_COLUMNS = ("actor_countries", "attributed", "lifecycle")
LIFECYCLE_RANK_SQL = ("CASE events.lifecycle WHEN 'developing' THEN 0 WHEN 'active' THEN 1 "
                      "WHEN 'cooling' THEN 2 WHEN 'closed' THEN 3 ELSE 4 END")

_ACTOR_IS = ("EXISTS (SELECT 1 FROM json_each(events.actor_countries) "
             "WHERE json_each.value = ?)")
_ACTOR_NOT = ("EXISTS (SELECT 1 FROM json_each(events.actor_countries) "
              "WHERE json_each.value != ?)")
_INSIDE = "events.country = ?"
_ABROAD = f"((events.country IS NULL OR events.country != ?) AND {_ACTOR_IS})"


def country_where(cc: str, location: str = "all", involvement: str = "all",
                  counterpart: str | None = None) -> tuple[list[str], list[object]]:
    """WHERE clauses (to AND together) and their parameters, in order."""
    where: list[str] = []
    params: list[object] = []
    if location == "inside":
        where.append(_INSIDE)
        params.append(cc)
    elif location == "abroad":
        where.append(_ABROAD)
        params += [cc, cc]
    else:
        where.append(f"({_INSIDE} OR {_ABROAD})")
        params += [cc, cc, cc]
    if involvement == "domestic":
        where.append("(events.attributed = 1 AND (events.country IS NULL OR events.country = ?)"
                     f" AND NOT {_ACTOR_NOT})")
        params += [cc, cc]
    elif involvement == "foreign":
        where.append(f"((events.country IS NOT NULL AND events.country != ?) OR {_ACTOR_NOT})")
        params += [cc, cc]
    elif involvement == "unattributed":
        where.append("(events.country = ? AND coalesce(events.attributed, 0) = 0)")
        params.append(cc)
    if counterpart:
        where.append(f"(events.country = ? OR {_ACTOR_IS})")
        params += [counterpart, counterpart]
    where.append("events.origin != 'historical'")
    return where, params


def as_list(value) -> list[str]:
    """actor_countries as a list of ISO2 strings, whether stored JSON text or decoded."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str) and x]


def event_ctx(cc: str, country: str | None, actor_countries, attributed) -> dict:
    """``{location, involvement}`` of one event as seen from ``cc`` (same rules as the SQL)."""
    actors = as_list(actor_countries)
    location = "inside" if country == cc else "abroad"
    if location == "inside" and not attributed:
        involvement = "unattributed"
    elif (country is not None and country != cc) or any(a != cc for a in actors):
        involvement = "foreign"
    else:
        involvement = "domestic"
    return {"location": location, "involvement": involvement}
```

- [ ] **Step 5: Wire it into `SqliteStore`**

In `backend/osint/store/sqlite.py`, change the import block:

```python
from osint.core.geo import utcnow
from osint.core.models import TrackInfo
```
to:
```python
from osint.core.geo import utcnow
from osint.core.models import TrackInfo
from osint.store.country_sql import (
    COUNTRY_COLUMNS,
    LIFECYCLE_RANK_SQL,
    as_list,
    country_where,
    event_ctx,
)
```

Replace the whole `list_events` method. It starts at `    def list_events(self, *, bbox: tuple[float, float, float, float] | None = None,` and ends at the `        return [_event_out(r) for r in rows]` just before `    def search_events`. The new method is:

```python
    def list_events(self, *, bbox: tuple[float, float, float, float] | None = None,
                    since: datetime | None = None, category: str | None = None,
                    status: str | None = None, min_confidence: float | None = None,
                    limit: int = 1000, slim: bool = True, include_unlocated: bool = True,
                    map_default: bool = False, origin: str | None = None,
                    until: datetime | None = None, offset: int = 0,
                    country: str | None = None, location: str = "all",
                    involvement: str = "all", counterpart: str | None = None) -> list[dict]:
        """Events, most recently updated first. bbox = (west, south, east, north); west > east
        wraps the antimeridian. Comma-separated category/status values match any of them.

        ``include_unlocated=False`` drops events without coordinates. ``map_default`` keeps
        only events worth showing on the map: status multi-source / sensor-corroborated /
        sensor-detected / official, or reported with city/point precision and confidence >= 0.5.

        ``country`` (ISO2) keeps the non-historical events that concern that country, narrowed
        by ``location`` / ``involvement`` / ``counterpart`` (store.country_sql). Each item then
        also carries ``actor_countries``, ``attributed``, ``lifecycle`` and ``ctx``, and the
        order is lifecycle (developing first), then most recently updated."""
        where: list[str] = []
        params: list[object] = []
        if not include_unlocated:
            where.append("lat IS NOT NULL")
        if map_default:
            where.append("(status IN ('multi-source', 'sensor-corroborated', 'sensor-detected', "
                         "'official') OR "
                         "(status = 'reported' AND precision IN ('city', 'point') "
                         "AND confidence >= 0.5))")
            where.append("id NOT IN (SELECT event_id FROM event_verification "
                         "WHERE verdict = 'unsupported')")
        if bbox is not None:
            w, s, e, n = bbox
            where.append("lat IS NOT NULL AND lat BETWEEN ? AND ?")
            params += [s, n]
            if w <= e:
                where.append("lon BETWEEN ? AND ?")
            else:
                where.append("(lon >= ? OR lon <= ?)")
            params += [w, e]
        if since is not None:
            where.append("updated_at >= ?")
            params.append(iso(since))
        if until is not None:
            where.append("updated_at <= ?")
            params.append(iso(until))
        for col, raw in (("category", category), ("status", status), ("origin", origin)):
            values = [v.strip() for v in (raw or "").split(",") if v.strip()]
            if values:
                where.append(f"{col} IN ({','.join('?' * len(values))})")
                params += values
        if min_confidence is not None:
            where.append("confidence >= ?")
            params.append(min_confidence)
        if country:
            cw, cp = country_where(country, location, involvement, counterpart)
            where += cw
            params += cp
        cols = ", ".join(SLIM_EVENT_COLUMNS) if slim else "*"
        if country and slim:
            cols += "".join(f", {c}" for c in COUNTRY_COLUMNS if c not in SLIM_EVENT_COLUMNS)
        sql = f"SELECT {cols} FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = f"{LIFECYCLE_RANK_SQL}, " if country else ""
        sql += f" ORDER BY {order}updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [max(0, int(limit)), max(0, int(offset))]
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        out = [_event_out(r) for r in rows]
        if country:
            for it in out:
                it["actor_countries"] = as_list(it.get("actor_countries"))
                it["ctx"] = event_ctx(country, it.get("country"), it["actor_countries"],
                                      it.get("attributed"))
        return out
```

Insert these two methods directly after the `country_tempo` method (before `def document_volume`):

```python
    def country_events(self, cc: str, *, since: datetime, until: datetime | None = None,
                       category: str | None = None) -> list[dict]:
        """Non-historical events in ``cc``'s location set (inside or abroad) updated in
        [since, until], for the country summary: id, country, actor_countries (list),
        attributed, lifecycle, actors (list) and ``ctx``."""
        where, params = country_where(cc)
        where.append("events.updated_at >= ?")
        params.append(iso(since))
        if until is not None:
            where.append("events.updated_at <= ?")
            params.append(iso(until))
        cats = [v.strip() for v in (category or "").split(",") if v.strip()]
        if cats:
            where.append(f"events.category IN ({','.join('?' * len(cats))})")
            params += cats
        with self._lock:
            rows = self._db.execute(
                "SELECT id, country, actor_countries, attributed, lifecycle, actors FROM events "
                f"WHERE {' AND '.join(where)}", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["actor_countries"] = as_list(d["actor_countries"])
            try:
                d["actors"] = json.loads(d["actors"] or "[]")
            except ValueError:
                d["actors"] = []
            d["ctx"] = event_ctx(cc, d["country"], d["actor_countries"], d["attributed"])
            out.append(d)
        return out

    def country_spark(self, cc: str, since: datetime) -> list[tuple[str, int]]:
        """(first-report day, events) for ``cc``'s location set with t_start >= since."""
        where, params = country_where(cc)
        where.append("events.t_start >= ?")
        params.append(iso(since))
        with self._lock:
            rows = self._db.execute(
                "SELECT substr(t_start, 1, 10), count(*) FROM events WHERE "
                f"{' AND '.join(where)} GROUP BY 1 ORDER BY 1", params).fetchall()
        return [(d, n) for d, n in rows]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/store/test_country_sql.py -v`
Expected: 7 passed.

Run: `uv run pytest tests/store tests/api/test_events_api.py -q`
Expected: all pass. The plain `list_events` is unchanged apart from `OFFSET 0`.

- [ ] **Step 7: Commit**

```bash
git add backend/osint/store/country_sql.py backend/osint/store/sqlite.py tests/country_fixture.py tests/store/test_country_sql.py
git commit -m "feat(country): location/involvement/counterpart rules in the event store"
```

---

### Task 2: `GET /api/events` country parameters and `ctx`

**Files:**
- Modify: `backend/osint/api/events.py`:
  - imports (lines 7-10);
  - new helpers after `_json` (around line 63);
  - the `list_events` route (around lines 155-171).
- Test: `tests/api/test_country_api.py`

**Interfaces:**
- Consumes:
  - `SqliteStore.list_events(..., until, offset, country, location, involvement, counterpart)` from Task 1;
  - `osint.core.countries.to_iso2(value, hint="iso2")`.
- Produces:
  - `events.country_code(raw: str | None) -> str | None`: a known ISO2 in upper case, else None.
  - `events.unknown_country() -> JSONResponse`: a 400 response with `{"error": "unknown_country"}`.
  - `GET /api/events` takes these new query parameters:
    - `country`;
    - `location=all|inside|abroad`;
    - `involvement=all|domestic|foreign|unattributed`;
    - `counterpart`;
    - `until` (ISO-8601);
    - `offset` (≥ 0);
    - `ids=true`, which returns `{"layer", "count", "ids"}`.

- [ ] **Step 1: Write the failing API tests**

Create `tests/api/test_country_api.py`:

```python
"""GET /api/events in country mode (spec B §2.1): location / involvement / counterpart
filters, ctx, ordering, window, paging, the ids-only form and 400 for unknown countries."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.events import mount_events
from tests.country_fixture import T0, build_store

Q = "/api/events?all=true&country=UA"


class _RT:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def store(tmp_path):
    s = build_store(tmp_path / "e.sqlite")
    yield s
    s.close()


@pytest.fixture
def client(store):
    app = FastAPI()
    mount_events(app, _RT(store))
    return TestClient(app)


def ids(resp):
    assert resp.status_code == 200, resp.text
    return [it["id"] for it in resp.json()["items"]]


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_location_involvement_and_order(client):
    assert ids(client.get(Q)) == ["E1", "E2", "E6", "E3", "E4"]
    assert ids(client.get("/api/events?all=true&country=ua")) == ["E1", "E2", "E6", "E3", "E4"]
    assert set(ids(client.get(Q + "&location=inside"))) == {"E1", "E2", "E4"}
    assert set(ids(client.get(Q + "&location=abroad"))) == {"E3", "E6"}
    assert ids(client.get(Q + "&involvement=domestic")) == ["E1"]
    assert set(ids(client.get(Q + "&involvement=foreign"))) == {"E2", "E3", "E6"}
    assert ids(client.get(Q + "&involvement=unattributed")) == ["E4"]
    assert ids(client.get(Q + "&location=inside&involvement=foreign")) == ["E2"]
    assert ids(client.get(Q + "&location=abroad&involvement=domestic")) == []


def test_ctx_from_both_sides(client):
    ua = {it["id"]: it["ctx"] for it in client.get(Q).json()["items"]}
    assert ua["E2"] == {"location": "inside", "involvement": "foreign"}
    assert ua["E4"] == {"location": "inside", "involvement": "unattributed"}
    ru_items = client.get("/api/events?all=true&country=RU").json()["items"]
    ru = {it["id"]: it["ctx"] for it in ru_items}
    assert ru == {"E2": {"location": "abroad", "involvement": "foreign"},
                  "E3": {"location": "inside", "involvement": "foreign"}}
    first = client.get(Q).json()["items"][0]
    assert first["lifecycle"] == "developing" and first["actor_countries"] == ["UA"]
    assert "ctx" not in client.get("/api/events?all=true").json()["items"][0]


def test_counterpart(client):
    assert set(ids(client.get(Q + "&counterpart=RU"))) == {"E2", "E3"}
    assert ids(client.get(Q + "&counterpart=pl")) == ["E6"]
    assert ids(client.get(Q + "&counterpart=FR")) == []


def test_window_paging_ids_and_category(client):
    since, until = z(T0 - timedelta(minutes=35)), z(T0 - timedelta(minutes=15))
    assert ids(client.get(Q + "&since=" + since + "&until=" + until)) == ["E2", "E3"]
    assert ids(client.get(Q + "&limit=2")) == ["E1", "E2"]
    assert ids(client.get(Q + "&limit=2&offset=2")) == ["E6", "E3"]
    j = client.get(Q + "&ids=true").json()
    assert j == {"layer": "events", "count": 5, "ids": ["E1", "E2", "E6", "E3", "E4"]}
    assert "E4" not in ids(client.get(Q + "&category=armed_conflict"))
    assert client.get(Q + "&until=never").status_code == 422
    assert client.get(Q + "&offset=-1").status_code == 422


def test_unknown_country_is_400(client):
    for url in ("/api/events?country=ZZ", "/api/events?country=UA&counterpart=ZZ"):
        r = client.get(url)
        assert r.status_code == 400 and r.json() == {"error": "unknown_country"}
    assert client.get(Q + "&location=nowhere").status_code == 422
    assert client.get(Q + "&involvement=x").status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/api/test_country_api.py -v`
Expected: FAIL. Most tests fail on assertions because `country` is ignored today, so the ids include E5/H1 and no `ctx` key is present. `test_unknown_country_is_400` fails with `200 != 400`.

- [ ] **Step 3: Implement the parameters**

In `backend/osint/api/events.py` change:

```python
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from osint.api import kml
```
to:
```python
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from osint.api import kml
from osint.core import countries
```

After the `_json` function, add:

```python
def country_code(raw: str | None) -> str | None:
    """A known ISO 3166-1 alpha-2 code in any case -> upper-case ISO2; anything else -> None."""
    if not raw or not raw.strip():
        return None
    return countries.to_iso2(raw.strip().upper(), hint="iso2")


def unknown_country() -> JSONResponse:
    return JSONResponse({"error": "unknown_country"}, status_code=400)
```

Replace this route:

```python
    @app.get("/api/events")
    async def list_events(
        bbox: str | None = None, since: str | None = None, category: str | None = None,
        status: str | None = None,
        min_confidence: float | None = Query(None, ge=0.0, le=1.0),
        limit: int = Query(1000, ge=1, le=MAX_LIMIT),
        all_: bool = Query(False, alias="all"),
        include_unlocated: bool = False,
    ) -> Response:
        """Map view by default: multi-source / sensor-corroborated / official events, or
        reported ones with city/point precision and confidence >= 0.5; located only.
        ``all=true`` returns everything; ``include_unlocated=true`` adds unlocated events."""
        items = await asyncio.to_thread(
            store.list_events, bbox=parse_bbox(bbox), since=parse_since(since),
            category=category, status=status, min_confidence=min_confidence, limit=limit,
            include_unlocated=all_ or include_unlocated,
            map_default=not all_ and not status)
        await asyncio.to_thread(add_corroboration, store, items)
```
with:
```python
    @app.get("/api/events")
    async def list_events(
        bbox: str | None = None, since: str | None = None, category: str | None = None,
        status: str | None = None,
        min_confidence: float | None = Query(None, ge=0.0, le=1.0),
        limit: int = Query(1000, ge=1, le=MAX_LIMIT),
        all_: bool = Query(False, alias="all"),
        include_unlocated: bool = False,
        until: str | None = None,
        offset: int = Query(0, ge=0),
        country: str | None = None,
        location: str = Query("all", pattern="^(all|inside|abroad)$"),
        involvement: str = Query("all", pattern="^(all|domestic|foreign|unattributed)$"),
        counterpart: str | None = None,
        ids: bool = False,
    ) -> Response:
        """Map view by default: multi-source / sensor-corroborated / official events, or
        reported ones with city/point precision and confidence >= 0.5; located only.
        ``all=true`` returns everything; ``include_unlocated=true`` adds unlocated events.

        ``country`` (ISO2) is the country context view (spec B §2.1): ``location``,
        ``involvement`` and ``counterpart`` narrow it, every item carries ``ctx``
        ({location, involvement}), and items are ordered by lifecycle, then last update.
        Unknown codes -> 400 {"error": "unknown_country"}. ``ids=true`` returns only the
        matching ids (the globe's dimming set)."""
        cc = cp = None
        if country:
            cc = country_code(country)
            if cc is None:
                return unknown_country()
            if counterpart:
                cp = country_code(counterpart)
                if cp is None:
                    return unknown_country()
        items = await asyncio.to_thread(
            store.list_events, bbox=parse_bbox(bbox), since=parse_since(since),
            until=parse_since(until), category=category, status=status,
            min_confidence=min_confidence, limit=limit, offset=offset,
            include_unlocated=all_ or include_unlocated,
            map_default=not all_ and not status, country=cc, location=location,
            involvement=involvement, counterpart=cp)
        if ids:
            return _json({"layer": "events", "count": len(items),
                          "ids": [it["id"] for it in items]})
        await asyncio.to_thread(add_corroboration, store, items)
```
The remaining lines of the route stay unchanged: `add_impacts`, `add_english` and the final `return _json({...})`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/api/test_country_api.py tests/api/test_events_api.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/osint/api/events.py tests/api/test_country_api.py
git commit -m "feat(country): /api/events country, location, involvement, counterpart and ctx"
```

---

### Task 3: Country summary endpoint

**Files:**
- Modify: `backend/osint/terror/board.py`. Add `threat_section` before `def _slim` (around line 128).
- Create: `backend/osint/api/country.py`
- Modify: `backend/osint/api/m1.py`. Add the registration after the terror line (around line 135).
- Test: `tests/terror/test_threat_section.py`, `tests/api/test_country_summary.py`

**Interfaces:**
- Consumes:
  - `SqliteStore.country_events` and `SqliteStore.country_spark` (Task 1);
  - `events.country_code`, `events.unknown_country`, `events.parse_since` and `events._json` (Task 2);
  - `osint.core.countries.name` and `osint.core.countries.bbox`;
  - the Terror Watch store's `levels(cc)` and `advisories(cc)`;
  - `osint.signals.countries.get_countries().point(cc) -> (lat, lon, name) | None`.
- Produces:
  - `board.threat_section(store, cc) -> {"level", "source", "rank", "scale_max", "as_of", "advisories": [{"level", "label", "title", "url", "updated", "terrorism"}]}`
  - `country.summarise(cc, rows, spark_rows, today: date) -> {"counts", "lifecycle", "spark", "top_actors", "counterparts"}`
  - `country.threat_for(runtime, cc) -> dict | None`
  - `country._shapes() -> Countries` (patched in tests)
  - `country.mount_country(app, runtime, *, now=None)`
  - `GET /api/country/{cc}/summary?since&until&category` returns the spec §2.2 JSON plus `centroid: [lat, lon] | null`, `since` and `until`.

- [ ] **Step 1: Write the failing `threat_section` test**

Create `tests/terror/test_threat_section.py`:

```python
"""board.threat_section: the official level + advisory parts of the country board alone."""

from datetime import UTC, datetime

from osint.terror import board
from osint.terror.store import TerrorStore

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)


def test_threat_section(tmp_path):
    s = TerrorStore(tmp_path / "t.sqlite")
    s.save_level({"country": "GB", "authority": "MI5", "scope": "national",
                  "level": "SUBSTANTIAL", "rank": 3, "scale_max": 5, "as_of": "2026-04-30",
                  "quote": "q", "source_url": "https://x"}, NOW)
    s.save_level({"country": "GB", "authority": "MI5", "scope": "Northern Ireland",
                  "level": "SEVERE", "rank": 4, "scale_max": 5, "as_of": "2026-04-30",
                  "quote": "q", "source_url": "https://x"}, NOW)
    s.save_advisories([{"country": "GB", "level": 2, "label": "Exercise increased caution",
                        "terrorism": True, "risks": ["terrorism"],
                        "title": "United Kingdom Travel Advisory", "published": "2026-06-01",
                        "updated": "2026-06-01", "url": "https://travel.example/gb",
                        "quote": "q"}], NOW)
    t = board.threat_section(s, "GB")
    # the national level, not the regional one that ranks higher
    assert (t["level"], t["source"], t["rank"], t["scale_max"], t["as_of"]) == \
        ("SUBSTANTIAL", "MI5", 3, 5, "2026-04-30")
    assert t["advisories"] == [{"level": 2, "label": "Exercise increased caution",
                                "title": "United Kingdom Travel Advisory",
                                "url": "https://travel.example/gb", "updated": "2026-06-01",
                                "terrorism": True}]
    assert board.threat_section(s, "FR") == {"level": None, "source": None, "rank": None,
                                             "scale_max": None, "as_of": None,
                                             "advisories": []}
    s.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/terror/test_threat_section.py -v`
Expected: FAIL with `AttributeError: module 'osint.terror.board' has no attribute 'threat_section'`.

- [ ] **Step 3: Implement `threat_section`**

In `backend/osint/terror/board.py`, insert this function directly above `def _slim(i: dict) -> dict:`:

```python
def threat_section(store, cc: str) -> dict:
    """The official threat level and travel advisories of ``cc``: the levels / advisory parts
    of ``country_board`` alone (the same store reads), for the country context pane.
    ``level`` is the national-scope level ranked highest on its own scale; None when no
    monitored authority publishes one."""
    levels = store.levels(cc)
    national = [lv for lv in levels if (lv.get("scope") or "national") == "national"] or levels
    best = max(national, default=None,
               key=lambda lv: (lv.get("rank") or 0) / (lv.get("scale_max") or 1)) or {}
    advisories = [{k: a.get(k) for k in ("level", "label", "title", "url", "updated",
                                         "terrorism")} for a in store.advisories(cc)]
    return {"level": best.get("level"), "source": best.get("authority"),
            "rank": best.get("rank"), "scale_max": best.get("scale_max"),
            "as_of": best.get("as_of"), "advisories": advisories}
```

Run: `uv run pytest tests/terror/test_threat_section.py -v`
Expected: PASS.

- [ ] **Step 4: Write the failing summary tests**

Create `tests/api/test_country_summary.py`:

```python
"""GET /api/country/{cc}/summary (spec B §2.2): counts, lifecycle, 30-day spark, top actors,
counterparts, bbox / centroid, threat (null when Terror Watch fails), 400 for unknown codes."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import country as country_api
from osint.api.country import mount_country
from osint.core import countries
from osint.signals.countries import Countries
from osint.terror import board
from osint.terror.store import TerrorStore
from tests.country_fixture import T0, build_store, write_geojson


class _RT:
    def __init__(self, store, terror_store):
        self.store = store
        self.terror_store = terror_store


@pytest.fixture
def cclient(tmp_path, monkeypatch):
    shapes = Countries(write_geojson(tmp_path / "ne.geojson"))
    monkeypatch.setattr(country_api, "_shapes", lambda: shapes)
    monkeypatch.setattr(countries, "bbox", lambda cc: {"UA": (22.0, 44.0, 40.0, 52.0)}.get(cc))
    store = build_store(tmp_path / "e.sqlite")
    ts = TerrorStore(tmp_path / "terror.sqlite")
    ts.save_level({"country": "UA", "authority": "SBU", "scope": "national", "level": "HIGH",
                   "rank": 3, "scale_max": 4, "as_of": "2026-11-01", "quote": "q",
                   "source_url": "https://x"}, T0)
    ts.save_advisories([{"country": "UA", "level": 4, "label": "Do not travel",
                         "terrorism": True, "risks": ["armed conflict"],
                         "title": "Ukraine Travel Advisory", "published": "2026-10-01",
                         "updated": "2026-10-01", "url": "https://travel.example/ua",
                         "quote": "q"}], T0)
    app = FastAPI()
    mount_country(app, _RT(store, ts), now=lambda: T0 + timedelta(hours=1))
    yield TestClient(app)
    ts.close()
    store.close()


def test_summary_shape(cclient):
    r = cclient.get("/api/country/ua/summary?since=2026-11-20T00:00:00Z")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["cc"] == "UA" and j["name"] == countries.name("UA")
    assert j["counts"] == {"inside": 3, "abroad": 2, "domestic": 1, "foreign": 3,
                           "unattributed": 1}
    assert j["lifecycle"] == {"developing": 1, "active": 2, "cooling": 1, "closed": 1}
    assert len(j["spark"]) == 30
    assert j["spark"][0] == {"day": "2026-10-23", "n": 0}
    assert j["spark"][-1] == {"day": "2026-11-21", "n": 5}
    assert j["top_actors"][0] == {"name": "Ukrainian army", "country": "UA", "n": 3}
    assert [a["name"] for a in j["top_actors"]] == [
        "Ukrainian army", "Polish police", "Russian forces", "Ukrainian police"]
    assert j["counterparts"] == [
        {"cc": "RU", "name": countries.name("RU"), "n": 2, "as_location": 1, "as_actor": 1},
        {"cc": "PL", "name": countries.name("PL"), "n": 1, "as_location": 1, "as_actor": 1}]
    assert j["bbox"] == [22.0, 44.0, 40.0, 52.0] and j["centroid"] == [48.0, 31.0]
    assert j["threat"] == {"level": "HIGH", "source": "SBU", "rank": 3, "scale_max": 4,
                           "as_of": "2026-11-01", "advisories": [
                               {"level": 4, "label": "Do not travel",
                                "title": "Ukraine Travel Advisory",
                                "url": "https://travel.example/ua", "updated": "2026-10-01",
                                "terrorism": True}]}


def test_default_window_category_and_no_bbox(cclient):
    j = cclient.get("/api/country/UA/summary").json()  # default: the last 7 days
    assert j["counts"]["inside"] == 3
    d = cclient.get("/api/country/UA/summary?category=disaster").json()
    assert d["counts"] == {"inside": 1, "abroad": 0, "domestic": 0, "foreign": 0,
                           "unattributed": 1}
    assert d["spark"][-1]["n"] == 5  # the spark ignores the window and the categories
    pl = cclient.get("/api/country/PL/summary").json()
    assert pl["bbox"] is None and pl["centroid"] == [52.0, 19.0]


def test_unknown_country_is_400(cclient):
    r = cclient.get("/api/country/ZZ/summary")
    assert r.status_code == 400 and r.json() == {"error": "unknown_country"}
    assert cclient.get("/api/country/UA/summary?since=yesterday").status_code == 422


def test_threat_null_when_terror_board_fails(cclient, monkeypatch):
    def boom(store, cc):
        raise RuntimeError("terror store unavailable")

    monkeypatch.setattr(board, "threat_section", boom)
    j = cclient.get("/api/country/UA/summary").json()
    assert j["threat"] is None and j["counts"]["inside"] == 3
```

- [ ] **Step 5: Run them to verify they fail**

Run: `uv run pytest tests/api/test_country_summary.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'osint.api.country'`.

- [ ] **Step 6: Implement `api/country.py`**

Create `backend/osint/api/country.py`:

```python
"""Country context API (spec B §2.2): the per-country summary behind the country pane.

Counts, lifecycle, counterparts and top actors cover the non-historical events in the
country's location set (inside or abroad, osint.store.country_sql) updated in the window
(default: the last 7 days). ``spark`` always covers the last 30 days by first-report day.
``threat`` is the Terror Watch official level + advisories (``board.threat_section``), or
null when Terror Watch is unavailable. ``bbox`` comes from osint.core.countries; ``centroid``
(the Natural Earth label point) is the fly-to fallback for countries without a polygon."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from fastapi import FastAPI
from fastapi.responses import Response

from osint.api.events import _json, country_code, parse_since, unknown_country
from osint.core import countries
from osint.signals.countries import Countries, get_countries
from osint.terror import board
from osint.terror.store import get_store as terror_store

log = logging.getLogger(__name__)

SPARK_DAYS = 30
DEFAULT_WINDOW = timedelta(days=7)
TOP_COUNTERPARTS = 15
TOP_ACTORS = 10
LIFECYCLES = ("developing", "active", "cooling", "closed")


def _shapes() -> Countries:
    """Natural Earth country label points and outlines (the IODA overlay's source)."""
    return get_countries()


def summarise(cc: str, rows: list[dict], spark_rows: list[tuple[str, int]],
              today: date) -> dict:
    """Counts, lifecycle, top actors, counterparts and the 30-day spark from
    ``SqliteStore.country_events`` rows and ``country_spark`` rows."""
    counts = dict.fromkeys(("inside", "abroad", "domestic", "foreign", "unattributed"), 0)
    lifecycle = dict.fromkeys(LIFECYCLES, 0)
    actors: Counter = Counter()
    n: Counter = Counter()
    as_location: Counter = Counter()
    as_actor: Counter = Counter()
    for r in rows:
        ctx = r["ctx"]
        counts[ctx["location"]] += 1
        counts[ctx["involvement"]] += 1
        if r.get("lifecycle") in lifecycle:
            lifecycle[r["lifecycle"]] += 1
        seen: set[tuple] = set()
        for a in r.get("actors") or []:
            if not isinstance(a, dict) or not str(a.get("name") or "").strip():
                continue
            key = (str(a["name"]).strip(), a.get("country"))
            if key not in seen:
                seen.add(key)
                actors[key] += 1
        if ctx["involvement"] != "foreign":
            continue
        loc, acs = r.get("country"), set(r.get("actor_countries") or [])
        for other in ({loc} | acs) - {cc, None}:
            n[other] += 1
            if other == loc:
                as_location[other] += 1
            if other in acs:
                as_actor[other] += 1
    by_day = dict(spark_rows)
    spark = []
    for i in range(SPARK_DAYS - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        spark.append({"day": day, "n": by_day.get(day, 0)})
    top = sorted(actors.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1] or ""))
    cps = sorted(n.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "counts": counts, "lifecycle": lifecycle, "spark": spark,
        "top_actors": [{"name": k[0], "country": k[1], "n": v} for k, v in top[:TOP_ACTORS]],
        "counterparts": [{"cc": c, "name": countries.name(c), "n": v,
                          "as_location": as_location[c], "as_actor": as_actor[c]}
                         for c, v in cps[:TOP_COUNTERPARTS]],
    }


def threat_for(runtime, cc: str) -> dict | None:
    """``board.threat_section`` for ``cc``, or None (logged) when Terror Watch fails."""
    try:
        return board.threat_section(terror_store(runtime), cc)
    except Exception:
        log.warning("threat level unavailable for %s", cc, exc_info=True)
        return None


def mount_country(app: FastAPI, runtime, *, now: Callable[[], datetime] | None = None) -> None:
    store = runtime.store
    clock = now or (lambda: datetime.now(UTC))

    @app.get("/api/country/{cc}/summary")
    async def country_summary(cc: str, since: str | None = None, until: str | None = None,
                              category: str | None = None) -> Response:
        """Header strip, filter counts and counterparts of the country pane."""
        code = country_code(cc)
        if code is None:
            return unknown_country()
        t = clock()
        lo = parse_since(since) or t - DEFAULT_WINDOW
        hi = parse_since(until)
        today = t.astimezone(UTC).date()
        spark_from = datetime.combine(today - timedelta(days=SPARK_DAYS - 1), time(0), UTC)
        rows = await asyncio.to_thread(store.country_events, code, since=lo, until=hi,
                                       category=category)
        spark = await asyncio.to_thread(store.country_spark, code, spark_from)
        threat = await asyncio.to_thread(threat_for, runtime, code)
        point = (await asyncio.to_thread(_shapes)).point(code)
        box = countries.bbox(code)
        return _json({"cc": code, "name": countries.name(code), "threat": threat,
                      **summarise(code, rows, spark, today),
                      "bbox": list(box) if box else None,
                      "centroid": [point[0], point[1]] if point else None,
                      "since": lo.isoformat(), "until": hi.isoformat() if hi else None})
```

- [ ] **Step 7: Register the router**

In `backend/osint/api/m1.py`, directly after this line:

```python
    __import__("osint.api.terror", fromlist=["mount_terror"]).mount_terror(app, runtime)
```
add:
```python
    __import__("osint.api.country", fromlist=["mount_country"]).mount_country(app, runtime)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/api/test_country_summary.py tests/terror/test_threat_section.py tests/test_app.py -v`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add backend/osint/terror/board.py backend/osint/api/country.py backend/osint/api/m1.py tests/terror/test_threat_section.py tests/api/test_country_summary.py
git commit -m "feat(country): /api/country/{cc}/summary with threat, counts, spark, counterparts"
```

---

### Task 4: Country geometry endpoints and ISO2 on reference countries

The reference layer only loads country **label points** and unattributed border **lines**. The country polygons live server-side in `osint/signals/countries.py`, the IODA overlay's source. So Alt+click hit-testing runs on the server, and the outline is served per country.

**Files:**
- Modify: `backend/osint/api/country.py`. Add the point-in-polygon helpers and two routes.
- Modify: `backend/osint/reference/loader.py`: `load_countries` (around lines 162-172).
- Test:
  - `tests/api/test_country_summary.py` (append);
  - `tests/reference/test_reference.py` (an import plus one test).

**Interfaces:**
- Consumes:
  - `Countries.shapes: dict[str, list[list[list[float]]]]`, holding flat `[lon, lat, …]` rings with the outer ring first, then holes;
  - `Countries.shape(cc)`;
  - `country_code` and `unknown_country` (Task 2).
- Produces:
  - `GET /api/country/at?lat&lon` returns `{"cc", "name"}`, or 404 `{"error": "no_country"}`, or 422 when out of range.
  - `GET /api/country/{cc}/shape` returns `{"cc", "polygons"}`, where `polygons` is `[]` when the country has none. It sets `Cache-Control: max-age=86400`.
  - `country.country_at(shapes, lat, lon) -> str | None`
  - `/api/reference/countries` items gain `iso_a2: str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_country_summary.py`:

```python
def test_shape(cclient):
    r = cclient.get("/api/country/ua/shape")
    assert r.status_code == 200 and r.headers["cache-control"] == "max-age=86400"
    s = r.json()
    assert s["cc"] == "UA" and len(s["polygons"]) == 1 and len(s["polygons"][0]) == 2
    assert s["polygons"][0][0][:4] == [22.0, 44.0, 40.0, 44.0]
    assert cclient.get("/api/country/FR/shape").json()["polygons"] == []
    assert cclient.get("/api/country/ZZ/shape").status_code == 400


def test_country_at_point(cclient):
    assert cclient.get("/api/country/at?lat=48&lon=31").json() == {
        "cc": "UA", "name": countries.name("UA")}
    assert cclient.get("/api/country/at?lat=52&lon=18").json()["cc"] == "PL"
    for lat, lon in ((46.5, 31.0), (0.0, 0.0)):  # inside UA's hole, open sea
        r = cclient.get(f"/api/country/at?lat={lat}&lon={lon}")
        assert r.status_code == 404 and r.json() == {"error": "no_country"}
    assert cclient.get("/api/country/at?lat=95&lon=0").status_code == 422
```

In `tests/reference/test_reference.py`, change the first line from `import math` to:

```python
import json
import math
```
and append:
```python
def test_countries_carry_iso2(tmp_path):
    def feat(name, a2, eh, x, y):
        return {"type": "Feature", "geometry": None,
                "properties": {"NAME": name, "ISO_A2": a2, "ISO_A2_EH": eh,
                               "ISO_A3": "-99", "LABEL_X": x, "LABEL_Y": y}}

    p = tmp_path / "c.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        feat("Ukraine", "UA", "UA", 31.0, 48.0),
        feat("Norway", "-99", "NO", 9.0, 61.0),      # Natural Earth's -99 quirk
        feat("N. Cyprus", "-99", "-99", 33.5, 35.2)]}), encoding="utf-8")
    out = loader.load_countries(p)
    assert [(c["name"], c["iso_a2"]) for c in out] == [
        ("Ukraine", "UA"), ("Norway", "NO"), ("N. Cyprus", None)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/api/test_country_summary.py tests/reference/test_reference.py -v`
Expected:
- `test_shape` and `test_country_at_point` FAIL with 404 (no such route). Note: `/api/country/at` currently 404s for every point, so the UA assertion fails first.
- `test_countries_carry_iso2` FAILS with `KeyError: 'iso_a2'`.

- [ ] **Step 3: Implement the routes**

In `backend/osint/api/country.py`, change `from fastapi.responses import Response` to:

```python
from fastapi import Query
from fastapi.responses import JSONResponse, Response
```
Keep the existing `from fastapi import FastAPI` line above it.

Add these helpers after `_shapes()`:

```python
def _in_ring(lon: float, lat: float, ring: list[float]) -> bool:
    """Even-odd ray cast over a flat [lon, lat, ...] ring."""
    inside = False
    k = len(ring) // 2
    j = k - 1
    for i in range(k):
        xi, yi, xj, yj = ring[2 * i], ring[2 * i + 1], ring[2 * j], ring[2 * j + 1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def country_at(shapes: Countries, lat: float, lon: float) -> str | None:
    """ISO2 of the country polygon containing the point (in the outer ring, not in a hole).
    Polygon bboxes are indexed once per Countries object."""
    index = getattr(shapes, "_pip_index", None)
    if index is None:
        index = []
        for cc, polys in shapes.shapes.items():
            for poly in polys:
                if not poly or len(poly[0]) < 6:
                    continue
                xs, ys = poly[0][0::2], poly[0][1::2]
                index.append((cc, poly, min(xs), min(ys), max(xs), max(ys)))
        shapes._pip_index = index
    for cc, poly, w, s, e, n in index:
        if not (w <= lon <= e and s <= lat <= n):
            continue
        if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
            return cc
    return None
```

Inside `mount_country`, after the `country_summary` route, add:

```python
    @app.get("/api/country/at")
    async def country_at_point(lat: float = Query(..., ge=-90, le=90),
                               lon: float = Query(..., ge=-180, le=180)) -> Response:
        """The country under a globe point (Alt+click on the globe)."""
        shapes = await asyncio.to_thread(_shapes)
        cc = await asyncio.to_thread(country_at, shapes, lat, lon)
        code = country_code(cc) if cc else None
        if code is None:
            return JSONResponse({"error": "no_country"}, status_code=404)
        return _json({"cc": code, "name": countries.name(code)})

    @app.get("/api/country/{cc}/shape")
    async def country_shape(cc: str) -> Response:
        """Outline polygons (flat [lon, lat, ...] rings, outer first) for the globe sync."""
        code = country_code(cc)
        if code is None:
            return unknown_country()
        shapes = await asyncio.to_thread(_shapes)
        return JSONResponse({"cc": code, "polygons": shapes.shape(code)},
                            headers={"Cache-Control": "max-age=86400"})
```

- [ ] **Step 4: Add `iso_a2` to reference countries**

In `backend/osint/reference/loader.py`, replace:

```python
        out.append({"name": p.get("NAME") or p.get("ADMIN") or "", "iso_a3": p.get("ISO_A3") or p.get("ADM0_A3"),
                    "lon": round(lon, 3), "lat": round(lat, 3)})
```
with:
```python
        out.append({"name": p.get("NAME") or p.get("ADMIN") or "", "iso_a3": p.get("ISO_A3") or p.get("ADM0_A3"),
                    "iso_a2": _iso2(p), "lon": round(lon, 3), "lat": round(lat, 3)})
```
and insert above `def load_countries(path: Path) -> list[dict]:`:
```python
def _iso2(p: dict) -> str | None:
    """ISO 3166-1 alpha-2 of a Natural Earth admin-0 feature (ISO_A2, else ISO_A2_EH: some
    countries carry -99 in ISO_A2)."""
    for key in ("ISO_A2", "ISO_A2_EH"):
        v = str(p.get(key) or "")
        if len(v) == 2 and v.isalpha():
            return v.upper()
    return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/api/test_country_summary.py tests/reference tests/api/test_reference.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/osint/api/country.py backend/osint/reference/loader.py tests/api/test_country_summary.py tests/reference/test_reference.py
git commit -m "feat(country): country outline and point lookup endpoints, iso_a2 on reference countries"
```

---

### Task 5: Frontend pane state and API query builder

**Files:**
- Create: `frontend/js/country-state.js`
- Test: `tests/frontend/test_country_state.py`

**Interfaces:**
- Consumes: the API parameters from Tasks 2-3.
- Produces (all pure; no imports):
  - `LOCATIONS`, `INVOLVEMENTS`, `WINDOWS`, `LIFECYCLES` (arrays) and `PAGE = 200`.
  - `normState(spec) -> {type: "country", cc, location, involvement, window, from, to, cats, counterpart}`:
    - `window` is one of `24h|7d|30d|custom`;
    - `from` and `to` are `YYYY-MM-DD` or null, and only kept when `window === "custom"`;
    - `cats` is null (all on) or an array;
    - `counterpart` is ISO2 or null, never equal to `cc`.
  - `resetFilters(st) -> state`, which keeps `cc` only.
  - `windowRange(st, now = Date.now()) -> {since: string, until: string|null}`.
  - `eventsQuery(st, {offset = 0, limit = PAGE, now, allCats = [], ids = false}) -> string`.
  - `summaryQuery(st, {now, allCats = []}) -> string`.
  - `toggleCat(cats, k, allCats) -> string[] | null`.
  - `ctxLabel(ctx) -> string`, e.g. `"INSIDE·FOREIGN"`.
  - `flagEmoji(cc) -> string`.
  - `sparkPath(spark, w, h) -> string`, an SVG path `d`.
  - `emptyText(st) -> string`.
  - `pairLabel(st) -> string`, e.g. `"UA ↔ RU"`.

- [ ] **Step 1: Write the failing node tests**

Create `tests/frontend/test_country_state.py`:

```python
"""Country pane state (frontend/js/country-state.js): spec normalisation, query strings,
category toggles, labels and the sparkline path. The module is pure (no DOM / Cesium), so
each case runs it under node and returns JSON; skipped without node."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MOD = (ROOT / "frontend" / "js" / "country-state.js").as_uri()
NOW = "Date.UTC(2026, 10, 21, 12)"  # 2026-11-21T12:00Z
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")
DEFAULTS = {"type": "country", "cc": "UA", "location": "all", "involvement": "all",
            "window": "7d", "from": None, "to": None, "cats": None, "counterpart": None}


def run(body: str):
    """Evaluate ``body`` (JS returning a JSON-able value) with country-state.js as S."""
    src = (f'import * as S from "{MOD}";\nconst out = (() => {{ {body} }})();\n'
           "console.log(JSON.stringify(out));\n")
    r = subprocess.run([NODE, "--input-type=module"], input=src.encode(), capture_output=True,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_norm_state_defaults_and_validation():
    assert run('return S.normState({ type: "country", cc: "ua" });') == DEFAULTS
    assert run('return S.normState({ cc: "UA", location: "x", involvement: "y", '
               'window: "9d", counterpart: "ua", cats: "z" });') == DEFAULTS
    c = run('return S.normState({ cc: "UA", window: "custom", from: "2026-11-01", '
            'to: "bad", counterpart: "ru", cats: ["armed_conflict", 3] });')
    assert (c["window"], c["from"], c["to"], c["counterpart"], c["cats"]) == \
        ("custom", "2026-11-01", None, "RU", ["armed_conflict"])
    assert run('return S.normState({ cc: "UA", window: "7d", from: "2026-11-01" }).from;') \
        is None
    assert run('return S.resetFilters(S.normState({ cc: "UA", location: "inside", '
               'involvement: "foreign", window: "30d", cats: ["a"], counterpart: "RU" }));') \
        == DEFAULTS


def test_window_range():
    def rng(state):
        return run("return S.windowRange(S.normState(" + state + "), " + NOW + ");")

    assert rng("{ cc: 'UA', window: '24h' }") == \
        {"since": "2026-11-20T12:00:00.000Z", "until": None}
    # CUSTOM without a start date falls back to the 7-day default
    assert rng("{ cc: 'UA', window: 'custom' }") == \
        {"since": "2026-11-14T12:00:00.000Z", "until": None}
    assert rng("{ cc: 'UA', window: 'custom', from: '2026-11-01', to: '2026-11-10' }") == \
        {"since": "2026-11-01T00:00:00Z", "until": "2026-11-10T23:59:59Z"}


def test_events_and_summary_queries():
    def evq(state, opts):
        return run("return S.eventsQuery(S.normState(" + state + "), { now: " + NOW + ", "
                   + opts + " });")

    assert evq("{ cc: 'UA' }", "allCats: ['a', 'b']") == \
        "country=UA&all=true&since=2026-11-14T12%3A00%3A00.000Z&limit=200"
    assert evq("{ cc: 'UA', location: 'abroad', involvement: 'foreign', counterpart: 'RU', "
               "window: '24h', cats: ['b'] }", "allCats: ['a', 'b'], offset: 200") == (
        "country=UA&all=true&location=abroad&involvement=foreign&counterpart=RU"
        "&since=2026-11-20T12%3A00%3A00.000Z&category=b&limit=200&offset=200")
    # every category off: matches nothing on the server
    assert evq("{ cc: 'UA', cats: [] }", "allCats: ['a'], ids: true, limit: 10000") == (
        "country=UA&all=true&since=2026-11-14T12%3A00%3A00.000Z&category=none"
        "&limit=10000&ids=true")
    assert run("return S.summaryQuery(S.normState({ cc: 'UA', window: 'custom', "
               "from: '2026-11-01', to: '2026-11-10', cats: ['a', 'b'] }), "
               "{ allCats: ['a', 'b'] });") == \
        "since=2026-11-01T00%3A00%3A00Z&until=2026-11-10T23%3A59%3A59Z"


def test_toggle_cat():
    assert run("return [S.toggleCat(null, 'a', ['a', 'b']), "
               "S.toggleCat(['b'], 'a', ['a', 'b']), "
               "S.toggleCat(['b'], 'b', ['a', 'b'])];") == [["b"], None, []]


def test_labels_flag_spark_and_empty_text():
    assert run("return [S.ctxLabel({ location: 'inside', involvement: 'foreign' }), "
               "S.ctxLabel({ location: 'inside', involvement: 'unattributed' }), "
               "S.ctxLabel(null)];") == ["INSIDE·FOREIGN", "INSIDE·UNATTRIB.", ""]
    assert run("return [S.flagEmoji('UA'), S.flagEmoji('u1')];") == \
        ["\U0001F1FA\U0001F1E6", ""]
    assert run("return S.pairLabel(S.normState({ cc: 'UA', counterpart: 'RU' }));") == \
        "UA ↔ RU"
    assert run("return S.sparkPath([{ n: 0 }, { n: 2 }, { n: 1 }], 100, 20);") == \
        "M0,19.5L50,0.5L100,10"
    assert run("return S.sparkPath([{ n: 3 }], 100, 20);") == ""
    assert run("return S.emptyText(S.normState({ cc: 'UA' }));") == \
        "No events for UA in 7D with these filters"
    assert run("return S.emptyText(S.normState({ cc: 'UA', window: 'custom', "
               "from: '2026-11-01' }));") == \
        "No events for UA in 2026-11-01 → now with these filters"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/frontend/test_country_state.py -v`
Expected: FAIL with node's `ERR_MODULE_NOT_FOUND` (country-state.js does not exist). If node is not installed, all tests are SKIPPED. In that case install node (any LTS) before continuing, because this task's verification depends on it.

- [ ] **Step 3: Implement the module**

Create `frontend/js/country-state.js`:

```js
// Country context pane state (spec B §3.2): one object per pane, kept in the dossier pane spec
// (the dossier persists specs in localStorage, so it survives reloads and pane history).
// Builds every /api/events and /api/country query of the pane, plus its labels.
// Pure: no imports, no DOM, no Cesium (unit-tested under node: tests/frontend/test_country_state.py).
export const LOCATIONS = ["all", "inside", "abroad"];
export const INVOLVEMENTS = ["all", "domestic", "foreign", "unattributed"];
export const WINDOWS = ["24h", "7d", "30d", "custom"];
export const LIFECYCLES = ["developing", "active", "cooling", "closed"];
export const PAGE = 200;
const HOURS = { "24h": 24, "7d": 168, "30d": 720 };
const CC = /^[A-Z]{2}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const pick = (v, list, d) => (list.includes(v) ? v : d);
const day = (v) => (DAY.test(String(v || "")) ? String(v) : null);

export function normState(spec) {
  const s = spec || {};
  const cc = String(s.cc || "").toUpperCase();
  const win = pick(s.window, WINDOWS, "7d");
  const cp = String(s.counterpart || "").toUpperCase();
  return {
    type: "country", cc,
    location: pick(s.location, LOCATIONS, "all"),
    involvement: pick(s.involvement, INVOLVEMENTS, "all"),
    window: win,
    from: win === "custom" ? day(s.from) : null,
    to: win === "custom" ? day(s.to) : null,
    cats: Array.isArray(s.cats) ? s.cats.filter((c) => typeof c === "string") : null,
    counterpart: CC.test(cp) && cp !== cc ? cp : null,
  };
}

export const resetFilters = (st) => normState({ cc: st.cc });

// CUSTOM needs a start date; until one is picked the 7-day default applies
export function windowRange(st, now = Date.now()) {
  if (st.window === "custom" && st.from) return { since: st.from + "T00:00:00Z", until: st.to ? st.to + "T23:59:59Z" : null };
  return { since: new Date(now - (HOURS[st.window] || HOURS["7d"]) * 3600e3).toISOString(), until: null };
}

// null = no category filter; "none" = every chip off (matches no event)
function catsParam(st, allCats) {
  if (!st.cats) return null;
  if (!st.cats.length) return "none";
  if (allCats.length && allCats.every((c) => st.cats.includes(c))) return null;
  return st.cats.join(",");
}

export function eventsQuery(st, { offset = 0, limit = PAGE, now = Date.now(), allCats = [], ids = false } = {}) {
  const r = windowRange(st, now), q = new URLSearchParams();
  q.set("country", st.cc);
  q.set("all", "true");
  if (st.location !== "all") q.set("location", st.location);
  if (st.involvement !== "all") q.set("involvement", st.involvement);
  if (st.counterpart) q.set("counterpart", st.counterpart);
  q.set("since", r.since);
  if (r.until) q.set("until", r.until);
  const c = catsParam(st, allCats);
  if (c) q.set("category", c);
  q.set("limit", String(limit));
  if (offset) q.set("offset", String(offset));
  if (ids) q.set("ids", "true");
  return q.toString();
}

export function summaryQuery(st, { now = Date.now(), allCats = [] } = {}) {
  const r = windowRange(st, now), q = new URLSearchParams();
  q.set("since", r.since);
  if (r.until) q.set("until", r.until);
  const c = catsParam(st, allCats);
  if (c) q.set("category", c);
  return q.toString();
}

// flip one category chip; null when every category is on again
export function toggleCat(cats, k, allCats) {
  const on = new Set(cats || allCats);
  if (on.has(k)) on.delete(k); else on.add(k);
  return allCats.every((c) => on.has(c)) ? null : allCats.filter((c) => on.has(c));
}

export const ctxLabel = (ctx) => (ctx ? (ctx.location + "·" + (ctx.involvement === "unattributed" ? "unattrib." : ctx.involvement)).toUpperCase() : "");

export const flagEmoji = (cc) => (CC.test(String(cc || "")) ? String.fromCodePoint(...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65)) : "");

export const pairLabel = (st) => (st.counterpart ? st.cc + " ↔ " + st.counterpart : "");

// polyline path of daily counts across a w x h box (half-pixel inset so the stroke stays inside)
export function sparkPath(spark, w, h) {
  const v = (spark || []).map((d) => +d.n || 0);
  if (v.length < 2) return "";
  const max = Math.max(1, ...v), f = (x) => +x.toFixed(1);
  return v.map((n, i) => (i ? "L" : "M") + f((i * w) / (v.length - 1)) + "," + f(h - 0.5 - (n / max) * (h - 1))).join("");
}

export function emptyText(st) {
  const when = st.window === "custom" ? (st.from ? st.from + " → " + (st.to || "now") : "7D") : st.window.toUpperCase();
  return "No events for " + st.cc + " in " + when + " with these filters";
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/frontend/test_country_state.py tests/frontend/test_js_syntax.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/js/country-state.js tests/frontend/test_country_state.py
git commit -m "feat(country): pure pane state and query builder for the country pane"
```

---

### Task 6: Country pane with header strip

**Files:**
- Modify: `frontend/js/browser.js`:
  - the constructor (around lines 280-293);
  - the registered-pane branch of `_render` (around lines 567-569).
- Create:
  - `frontend/js/panels/country-view.js`;
  - `frontend/js/country.js`;
  - `frontend/css/country.css`.
- Modify:
  - `frontend/app.js`: the imports (around line 33) and the install (around line 930);
  - `frontend/index.html`: line 21.

**Interfaces:**
- Consumes:
  - `GET /api/country/{cc}/summary` (Task 3);
  - from `country-state.js`: `normState`, `summaryQuery`, `flagEmoji`, `sparkPath`, `LIFECYCLES` (Task 5);
  - `fetchJson(url, timeoutMs)` from `api.js`;
  - `esc` and `nf` from `ui.js`;
  - `EVENT_CATS` from `symbology.js`.
- Produces:
  - Registered panes now get these hooks: `{head, stale, flyTo, highlight, select(kind, id), open(spec, {focus}), setSpec(spec)}`. `setSpec` replaces the pane's current spec and history entry, then persists. It does not re-render.
  - Panes restored before their module registered are rendered when `"noctua:panels"` fires.
  - `country-view.js` exports `COUNTRY_PANE = {icon, key, render}` and `renderCountry(spec, body, hooks)`.
  - `country.js` exports `installCountry(ctx) -> {open(cc)}`.
  - `ctx.country` is available in `app.js`, so `window.__osint.country.open("UA")` works.

- [ ] **Step 1: Give registered panes the hooks they need**

In `frontend/js/browser.js`, replace:

```js
    this._build();
    this._intercept();
    this._restore();
```
with:
```js
    this._build();
    this._intercept();
    // panes restored before their feature module registered (country.js, terror.js) render
    // once it has: modules fire "noctua:panels" after registering
    window.addEventListener("noctua:panels", () => {
      for (const p of this.panes) if (p._pendingExt && (window.__noctuaPanes || {})[p.spec.type]) { p._pendingExt = false; this._render(p); }
    });
    this._restore();
```

Replace:
```js
      } else if ((window.__noctuaPanes || {})[spec.type]) { // panes registered by feature modules (e.g. terror.js)
        await window.__noctuaPanes[spec.type].render(spec, body, { head: (h) => this._head(pane, h), stale, flyTo: this.flyTo, highlight: this.highlight });
      }
    } catch (err) {
```
with:
```js
      } else if ((window.__noctuaPanes || {})[spec.type]) { // panes registered by feature modules (terror.js, country.js)
        await window.__noctuaPanes[spec.type].render(spec, body, { head: (h) => this._head(pane, h), stale, flyTo: this.flyTo, highlight: this.highlight,
          select: this.select, open: (s, o) => this.open(s, o),
          // a pane that keeps its own state in its spec (country.js) updates it in place: persisted, no re-render
          setSpec: (s) => { pane.spec = s; pane.hist[pane.hi] = s; this._save(); } });
      } else pane._pendingExt = true; // its module has not registered yet: rendered on "noctua:panels"
    } catch (err) {
```

- [ ] **Step 2: Create the pane module**

Create `frontend/js/panels/country-view.js`:

```js
// COUNTRY CONTEXT pane (spec B §3.2): every event that concerns one country. State lives in the
// pane spec (country-state.js), which the dossier persists, so it survives reloads and pane
// history; the pane refreshes on the live event poll's cadence while it is open.
import { fetchJson } from "/static/js/api.js";
import { esc, nf } from "/static/js/ui.js";
import { EVENT_CATS } from "/static/js/symbology.js";
import { LIFECYCLES, normState, summaryQuery, flagEmoji, sparkPath } from "/static/js/country-state.js";

const ICON = '<svg viewBox="0 0 16 16"><path d="M3.5 14.5V2"/><path d="M3.5 2.5h9l-2.2 3 2.2 3h-9"/></svg>';
const REFRESH_MS = 60000; // the live event poll's cadence (app.js)

class CountryPane {
  constructor(spec, body, hooks) {
    this.st = normState(spec);
    this.body = body;
    this.hooks = hooks;
    this.allCats = Object.keys(EVENT_CATS);
    this.summary = null; this.sumErr = null; this.sumSeq = 0; this.advOpen = false;
    body.innerHTML = '<div class="dk-root cx-pane"><div class="cx-head"></div><div class="cx-filters"></div><div class="cx-cp"></div><div class="cx-list"></div></div>';
    this.root = body.firstElementChild;
    this.root.addEventListener("click", (e) => this.click(e));
  }
  dead() { return this.hooks.stale() || !this.body.isConnected; }
  title(s) {
    this.hooks.head({ title: (flagEmoji(this.st.cc) + " " + (s ? s.name : this.st.cc)).trim(), sub: this.st.cc + " · COUNTRY CONTEXT", icon: "country",
      coord: s && s.centroid ? { lat: s.centroid[0], lon: s.centroid[1], alt: 3e6 } : null });
  }
  async start() {
    this.title(null);
    this.renderAll();
    this.timer = setInterval(() => { if (this.dead()) clearInterval(this.timer); else this.load("refresh"); }, REFRESH_MS);
    await this.load("reset");
  }
  load() { return this.loadSummary(); }
  renderAll() { this.paintSummary(); }
  paintSummary() { this.renderHead(); }
  async loadSummary() {
    const seq = ++this.sumSeq;
    try {
      const s = await fetchJson("/api/country/" + encodeURIComponent(this.st.cc) + "/summary?" + summaryQuery(this.st, { allCats: this.allCats }), 20000);
      if (this.dead() || seq !== this.sumSeq) return;
      this.summary = s; this.sumErr = null;
      this.title(s);
    } catch (err) {
      if (this.dead() || seq !== this.sumSeq) return;
      this.sumErr = String((err && err.message) || err);
    }
    this.paintSummary();
  }
  renderHead() {
    const el = this.root.querySelector(".cx-head"), s = this.summary;
    if (this.sumErr) {
      el.innerHTML = '<div class="cx-err"><b>Summary unavailable</b><span>' + esc(this.sumErr) + '</span><button type="button" data-cx="retry">Retry</button></div>';
      return;
    }
    if (!s) { el.innerHTML = '<div class="cx-loading">Loading ' + esc(this.st.cc) + " summary…</div>"; return; }
    const t = s.threat, advs = (t && t.advisories) || [];
    const threat = t
      ? '<button type="button" class="cx-threat" data-cx="adv" aria-expanded="' + this.advOpen + '" title="' + esc(t.as_of ? "As of " + t.as_of : "Official threat level") + '"><b>' + esc(t.level || "No official level") + "</b>" +
        (t.source ? "<small>" + esc(t.source) + "</small>" : "") + '<span class="cx-n">' + nf(advs.length) + " advisor" + (advs.length === 1 ? "y" : "ies") + "</span></button>"
      : '<span class="cx-threat off" title="Terror Watch threat data could not be read">Threat level unavailable</span>';
    const adv = t && this.advOpen ? '<ul class="cx-adv">' + (advs.length ? advs.map((a) => "<li><b>" + esc(a.label || "Level " + a.level) + "</b> " +
      (a.url ? '<a href="' + esc(a.url) + '" target="_blank" rel="noopener noreferrer">' + esc(a.title || "advisory") + "</a>" : esc(a.title || "")) +
      (a.updated ? " <small>" + esc(a.updated) + "</small>" : "") + (a.terrorism ? ' <span class="cx-terr">terrorism</span>' : "") + "</li>").join("")
      : "<li>No travel advisories for " + esc(s.cc) + ".</li>") + "</ul>" : "";
    const days = s.spark || [], total = days.reduce((a, d) => a + (d.n || 0), 0);
    const range = days.length ? days[0].day + " to " + days[days.length - 1].day : "";
    const spark = '<svg class="cx-spark" viewBox="0 0 120 24" preserveAspectRatio="none" role="img" aria-label="' + esc(nf(total) + " events in the last 30 days") + '"><title>' + esc(nf(total) + " events, " + range) + '</title><path d="' + sparkPath(days, 120, 24) + '"/></svg>';
    const life = '<div class="cx-life">' + LIFECYCLES.map((k) => '<span class="cx-lc lc-' + k + '" title="' + k + '">' + k.slice(0, 3).toUpperCase() + " <b>" + nf((s.lifecycle || {})[k] || 0) + "</b></span>").join("") + "</div>";
    const actors = '<div class="cx-actors">' + (s.top_actors || []).slice(0, 3).map((a) => '<span class="dk-chip" title="' + esc(nf(a.n) + " events") + '">' + esc(a.name) + (a.country ? " · " + esc(a.country) : "") + "</span>").join("") + "</div>";
    el.innerHTML = '<div class="cx-id"><span class="cx-flag" aria-hidden="true">' + flagEmoji(s.cc) + '</span><b class="cx-cc">' + esc(s.cc) + '</b><span class="cx-name">' + esc(s.name) + "</span></div>" + threat + spark + life + actors + adv;
  }
  click(e) {
    const t = e.target.closest("button");
    if (!t || !this.root.contains(t)) return;
    if (t.dataset.cx === "retry") this.loadSummary();
    else if (t.dataset.cx === "adv") { this.advOpen = !this.advOpen; this.renderHead(); }
  }
}

export async function renderCountry(spec, body, hooks) {
  const st = normState(spec);
  if (!/^[A-Z]{2}$/.test(st.cc)) {
    hooks.head({ title: "Country", sub: "COUNTRY CONTEXT", icon: "country" });
    body.innerHTML = '<div class="cx-err"><b>Not a country code</b><span>' + esc(spec.cc) + "</span></div>";
    return;
  }
  await new CountryPane(spec, body, hooks).start();
}

export const COUNTRY_PANE = { icon: ICON, key: (s) => "ctry:" + String(s.cc || "").toUpperCase(), render: renderCountry };
```

- [ ] **Step 3: Create the installer**

Create `frontend/js/country.js`:

```js
// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js) and
// exposes open(cc) for the palette, the globe and pivots. Returns { open }.
import { COUNTRY_PANE } from "/static/js/panels/country-view.js";

export function installCountry(ctx) {
  const { dossier } = ctx;
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, { country: COUNTRY_PANE });
  window.dispatchEvent(new Event("noctua:panels")); // renders country panes restored before this ran
  const open = (cc) => {
    const code = String(cc || "").trim().toUpperCase();
    return /^[A-Z]{2}$/.test(code) ? dossier.open({ type: "country", cc: code }) : null;
  };
  return { open };
}
```

- [ ] **Step 4: Install it from `app.js` and link the CSS**

In `frontend/app.js`, after `import { Dossier } from "/static/js/browser.js";` add:

```js
import { installCountry } from "/static/js/country.js";
```

Replace:
```js
  ctx.terror = installTerror(ctx); // Terror Watch: layer, Terror tab, dossier panes, intel tiles
```
with:
```js
  ctx.terror = installTerror(ctx); // Terror Watch: layer, Terror tab, dossier panes, intel tiles
  ctx.country = installCountry(ctx); // Country context: dossier pane, globe sync, open(cc)
```

In `frontend/index.html`, after `<link rel="stylesheet" href="/static/css/terror.css">` add:

```html
<link rel="stylesheet" href="/static/css/country.css">
```

Create `frontend/css/country.css`:

```css
/* COUNTRY CONTEXT pane (js/panels/country-view.js). Tokens from theme.css (sub-project C). */
.cx-pane { display: flex; flex-direction: column; gap: 8px; padding: 8px; font: 12px var(--f-body); color: var(--fg); }
.cx-pane button:focus-visible { outline: none; box-shadow: var(--glow); }
.cx-head { display: grid; grid-template-columns: auto auto minmax(80px, 1fr); align-items: center; gap: 8px 12px; padding: 8px; background: var(--raise); border: 1px solid var(--line); }
.cx-id { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.cx-flag { font-size: 18px; line-height: 1; }
.cx-cc { font: 600 13px var(--f-mono); color: var(--acc); letter-spacing: .06em; }
.cx-name { font: 600 11px var(--f-head); letter-spacing: .08em; text-transform: uppercase; color: var(--fg); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cx-threat { display: inline-flex; align-items: baseline; gap: 6px; padding: 3px 8px; border: 1px solid var(--line-2); background: transparent; color: var(--fg); font: 11px var(--f-mono); cursor: pointer; }
.cx-threat b { color: var(--acc); font-weight: 600; text-transform: uppercase; }
.cx-threat small, .cx-threat .cx-n { color: var(--fg-2); font-size: 11px; }
.cx-threat.off { cursor: default; color: var(--dim); border-style: dashed; }
.cx-spark { width: 100%; height: 24px; }
.cx-spark path { fill: none; stroke: var(--acc); stroke-width: 1.2; vector-effect: non-scaling-stroke; }
.cx-life { display: flex; flex-wrap: wrap; gap: 4px; grid-column: 1 / 3; }
.cx-lc { font: 11px var(--f-mono); padding: 1px 5px; border: 1px solid var(--line-2); color: var(--fg-2); white-space: nowrap; }
.cx-lc b { color: var(--fg); font-weight: 600; }
.cx-lc.lc-developing { border-color: var(--acc); color: var(--acc); }
.cx-lc.lc-closed { color: var(--dim); border-style: dashed; }
.cx-actors { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }
.cx-adv { grid-column: 1 / -1; margin: 0; padding: 6px 0 0; list-style: none; border-top: 1px solid var(--line); font: 11px var(--f-mono); color: var(--fg-2); }
.cx-adv li { padding: 2px 0; }
.cx-adv b { color: var(--fg); }
.cx-terr { color: var(--red); }
.cx-err { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px; border: 1px dashed var(--red); color: var(--fg-2); font: 11px var(--f-mono); }
.cx-err b { color: var(--red); }
.cx-err button { padding: 3px 10px; border: 1px solid var(--line-2); background: transparent; color: var(--acc); font: 11px var(--f-mono); cursor: pointer; }
.cx-loading { padding: 8px; color: var(--dim); font: 11px var(--f-mono); }
.cx-sub { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font: 600 11px var(--f-head); letter-spacing: .08em; text-transform: uppercase; color: var(--fg-2); padding: 4px 0; border-bottom: 1px solid var(--line); }
.cx-sub small { font: 11px var(--f-mono); letter-spacing: 0; text-transform: none; color: var(--dim); }
```

- [ ] **Step 5: Run the frontend checks**

Run: `uv run pytest tests/frontend -q`
Expected: all pass. That covers the module syntax of the new files and `test_real_frontend_bundles`, which proves the import subset and the lack of cycles.

- [ ] **Step 6: Manual check**

Start the server with `uv run osint run` and open `http://127.0.0.1:8765`. In the DevTools console, run `__osint.country.open("UA")`.

Expected:
- A dossier pane titled "🇺🇦 Ukraine" with sub "UA · COUNTRY CONTEXT".
- The header strip shows the flag, `UA` and the name, a threat badge (or "Threat level unavailable"), an amber 30-day sparkline, four lifecycle badges and up to 3 actor chips.
- Clicking the threat badge expands the advisories.
- `__osint.country.open("ZZ")` returns null and opens nothing.
- Reload the page, then reopen the dossier with `B`. The UA pane is restored and renders without a console error.

- [ ] **Step 7: Commit**

```bash
git add frontend/js/browser.js frontend/js/panels/country-view.js frontend/js/country.js frontend/app.js frontend/index.html frontend/css/country.css
git commit -m "feat(country): country dossier pane with header strip (threat, sparkline, lifecycle, actors)"
```

---

### Task 7: Filter bar

**Files:**
- Modify: `frontend/js/panels/country-view.js`
- Modify: `frontend/css/country.css` (append)

**Interfaces:**
- Consumes:
  - from `country-state.js`: `LOCATIONS`, `INVOLVEMENTS`, `WINDOWS`, `pairLabel`, `toggleCat` (Task 5);
  - `hooks.setSpec` (Task 6);
  - `summary.counts`.
- Produces:
  - `CountryPane.set(next)`: normalises the state, persists it through `setSpec`, re-renders and reloads.
  - `CountryPane.renderFilters()` and `CountryPane.change(e)`.

- [ ] **Step 1: Import the state helpers**

In `frontend/js/panels/country-view.js`, replace:

```js
import { LIFECYCLES, normState, summaryQuery, flagEmoji, sparkPath } from "/static/js/country-state.js";
```
with:
```js
import { LOCATIONS, INVOLVEMENTS, WINDOWS, LIFECYCLES, normState, summaryQuery, toggleCat, flagEmoji, sparkPath, pairLabel } from "/static/js/country-state.js";
```

- [ ] **Step 2: Listen to date inputs**

Replace:
```js
    this.root.addEventListener("click", (e) => this.click(e));
  }
```
with:
```js
    this.root.addEventListener("click", (e) => this.click(e));
    this.root.addEventListener("change", (e) => this.change(e));
  }
```

- [ ] **Step 3: Add `set`, `renderFilters` and `change`, and paint the filters**

Replace:
```js
  paintSummary() { this.renderHead(); }
```
with:
```js
  paintSummary() { this.renderHead(); this.renderFilters(); }
  // any filter change: new state into the pane spec (persisted), redraw, reload
  set(next) {
    this.st = normState(next);
    this.hooks.setSpec(this.st);
    this.renderAll();
    this.load("reset");
  }
  renderFilters() {
    const st = this.st, c = (this.summary && this.summary.counts) || null, tot = c ? c.inside + c.abroad : null;
    const seg = (key, values, labels, counts) => '<span class="seg cx-seg" role="group" aria-label="' + key + '">' + values.map((v, i) =>
      '<button type="button" data-k="' + key + '" data-v="' + v + '" class="' + (st[key] === v ? "on" : "") + '" aria-pressed="' + (st[key] === v) + '">' + labels[i] +
      (counts && counts[v] != null ? " <b>" + nf(counts[v]) + "</b>" : "") + "</button>").join("") + "</span>";
    const loc = seg("location", LOCATIONS, ["ALL", "INSIDE", "ABROAD"], c && { all: tot, inside: c.inside, abroad: c.abroad });
    const inv = seg("involvement", INVOLVEMENTS, ["ALL", "DOMESTIC", "FOREIGN", "UNATTRIB."], c && { all: tot, domestic: c.domestic, foreign: c.foreign, unattributed: c.unattributed });
    const win = seg("window", WINDOWS, ["24H", "7D", "30D", "CUSTOM"], null);
    const dates = st.window === "custom" ? '<span class="cx-dates"><input type="date" data-date="from" value="' + esc(st.from || "") + '" aria-label="From (UTC)"><span aria-hidden="true">→</span><input type="date" data-date="to" value="' + esc(st.to || "") + '" aria-label="To (UTC)"></span>' : "";
    const pair = st.counterpart ? '<button type="button" class="cx-pair" data-cx="uncp" title="Remove the counterpart filter">' + esc(pairLabel(st)) + ' <span aria-hidden="true">×</span></button>' : "";
    const on = (k) => !st.cats || st.cats.includes(k);
    const cats = '<div class="chips cx-cats">' + this.allCats.map((k) => '<button type="button" class="chip' + (on(k) ? " on" : "") + '" data-cat="' + k + '" aria-pressed="' + on(k) + '" style="--c:' + EVENT_CATS[k][1] + '" title="' + esc(EVENT_CATS[k][0]) + '">' + esc(EVENT_CATS[k][0]) + "</button>").join("") + "</div>";
    this.root.querySelector(".cx-filters").innerHTML = '<div class="cx-frow">' + loc + inv + '</div><div class="cx-frow">' + win + dates + pair + "</div>" + cats;
  }
  change(e) {
    const d = e.target.closest("[data-date]");
    if (d) this.set({ ...this.st, window: "custom", [d.dataset.date]: d.value || null });
  }
```

- [ ] **Step 4: Handle filter clicks**

Replace the whole `click(e) { … }` method of `CountryPane` with:

```js
  click(e) {
    const t = e.target.closest("button");
    if (!t || !this.root.contains(t)) return;
    const st = this.st, a = t.dataset.cx;
    if (a === "retry") this.loadSummary();
    else if (a === "adv") { this.advOpen = !this.advOpen; this.renderHead(); }
    else if (a === "uncp") this.set({ ...st, counterpart: null });
    else if (t.dataset.k) this.set({ ...st, [t.dataset.k]: t.dataset.v });
    else if (t.dataset.cat) this.set({ ...st, cats: toggleCat(st.cats, t.dataset.cat, this.allCats) });
  }
```

- [ ] **Step 5: Append the filter styles**

Append to `frontend/css/country.css`:

```css
/* filter bar */
.cx-filters { display: flex; flex-direction: column; gap: 6px; }
.cx-frow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.cx-seg button { font: 600 11px var(--f-head); letter-spacing: .08em; padding: 4px 8px; background: transparent; border: 1px solid var(--line); color: var(--fg-2); cursor: pointer; }
.cx-seg button b { font: 11px var(--f-mono); color: var(--dim); margin-left: 2px; }
.cx-seg button.on { color: var(--fg); border-bottom: 2px solid var(--acc); }
.cx-seg button.on b { color: var(--acc); }
.cx-dates { display: inline-flex; align-items: center; gap: 4px; font: 11px var(--f-mono); color: var(--dim); }
.cx-dates input { font: 11px var(--f-mono); color: var(--fg); background: transparent; border: 1px solid var(--line-2); padding: 2px 4px; color-scheme: dark; }
.cx-pair { font: 11px var(--f-mono); padding: 3px 8px; border: 1px solid var(--acc); color: var(--acc); background: rgba(var(--acc-rgb), .08); cursor: pointer; }
.cx-cats { padding: 0; }
.cx-cats .chip { cursor: pointer; font-size: 11px; }
```

- [ ] **Step 6: Run the frontend checks**

Run: `uv run pytest tests/frontend -q`
Expected: all pass.

- [ ] **Step 7: Manual check**

Reload, open `__osint.country.open("UA")`, and check:

- The location control `ALL | INSIDE | ABROAD` and the involvement control `ALL | DOMESTIC | FOREIGN | UNATTRIB.` show counts from the summary, and `ALL` equals inside + abroad.
- Clicking `INSIDE` underlines it. The `B` counts can change only through the window or categories, because counts are computed over the whole location set.
- `24H` / `30D` change the counts, and `CUSTOM` shows two date inputs. Picking a "from" date reloads, and the summary request in DevTools > Network carries `since=…T00:00:00Z`.
- Toggling a category chip dims it, and the summary request carries `category=`. Turning every chip off sends `category=none`, and all counts are 0.
- Reload the page and reopen the dossier. The filters are restored.

- [ ] **Step 8: Commit**

```bash
git add frontend/js/panels/country-view.js frontend/css/country.css
git commit -m "feat(country): filter bar (location, involvement, window, categories) with counts"
```

---

### Task 8: Counterpart breakdown

**Files:**
- Modify: `frontend/js/panels/country-view.js`
- Modify: `frontend/css/country.css` (append)

**Interfaces:**
- Consumes:
  - `summary.counterparts` entries `[{cc, name, n, as_location, as_actor}]` (Task 3);
  - `CountryPane.set` (Task 7).
- Produces: `CountryPane.renderCounterparts()`. Clicking a counterpart toggles `st.counterpart`, and the filter bar shows the removable `UA ↔ RU` chip (Task 7).

- [ ] **Step 1: Render the bars with the summary**

Replace:
```js
  paintSummary() { this.renderHead(); this.renderFilters(); }
```
with:
```js
  paintSummary() { this.renderHead(); this.renderFilters(); this.renderCounterparts(); }
  renderCounterparts() {
    const el = this.root.querySelector(".cx-cp"), s = this.summary;
    if (this.sumErr) { el.innerHTML = '<div class="cx-err"><b>Counterparts unavailable</b><button type="button" data-cx="retry">Retry</button></div>'; return; }
    if (!s) { el.innerHTML = ""; return; }
    const list = s.counterparts || [], max = Math.max(1, ...list.map((c) => c.n));
    el.innerHTML = '<div class="cx-sub">Counterparts <small>foreign-involved events · as location / as actor</small></div>' + (list.length
      ? '<ul class="cx-bars">' + list.map((c) => {
        const on = this.st.counterpart === c.cc;
        return '<li><button type="button" data-cp="' + esc(c.cc) + '" class="' + (on ? "on" : "") + '" aria-pressed="' + on + '" title="' + esc(c.name + ": " + c.as_location + " as location, " + c.as_actor + " as actor. Click to filter " + this.st.cc + " ↔ " + c.cc) + '">' +
          '<span class="cx-bn">' + flagEmoji(c.cc) + " " + esc(c.name) + '</span><span class="cx-bar"><i style="width:' + ((c.n / max) * 100).toFixed(1) + '%"></i></span><span class="cx-bv">' + nf(c.n) + "</span></button></li>";
      }).join("") + "</ul>"
      : '<div class="dz-empty">No foreign involvement in this window.</div>');
  }
```

- [ ] **Step 2: Handle counterpart clicks**

Replace the whole `click(e) { … }` method with:

```js
  click(e) {
    const t = e.target.closest("button");
    if (!t || !this.root.contains(t)) return;
    const st = this.st, a = t.dataset.cx;
    if (a === "retry") this.loadSummary();
    else if (a === "adv") { this.advOpen = !this.advOpen; this.renderHead(); }
    else if (a === "uncp") this.set({ ...st, counterpart: null });
    else if (t.dataset.k) this.set({ ...st, [t.dataset.k]: t.dataset.v });
    else if (t.dataset.cat) this.set({ ...st, cats: toggleCat(st.cats, t.dataset.cat, this.allCats) });
    else if (t.dataset.cp) this.set({ ...st, counterpart: st.counterpart === t.dataset.cp ? null : t.dataset.cp });
  }
```

- [ ] **Step 3: Append the bar styles**

Append to `frontend/css/country.css`:

```css
/* counterpart breakdown */
.cx-bars { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.cx-bars button { width: 100%; display: grid; grid-template-columns: minmax(0, 11em) 1fr 3.5em; align-items: center; gap: 8px; padding: 2px 4px; background: transparent; border: 1px solid transparent; color: var(--fg); font: 11px var(--f-body); text-align: left; cursor: pointer; }
.cx-bars button:hover { background: var(--raise); }
.cx-bars button.on { border-color: var(--acc); }
.cx-bn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cx-bar { height: 6px; background: var(--line); }
.cx-bar i { display: block; height: 100%; background: var(--acc); opacity: .75; }
.cx-bv { font: 11px var(--f-mono); color: var(--fg-2); text-align: right; }
```

- [ ] **Step 4: Run the frontend checks**

Run: `uv run pytest tests/frontend -q`
Expected: all pass.

- [ ] **Step 5: Manual check**

Open UA:
- The "Counterparts" list shows bars, with names, mono counts, and the largest bar at full width.
- Clicking "Russia" adds a `UA ↔ RU` chip to the filter bar, and the summary request carries no counterpart parameter. The list (next task) uses it.
- Clicking the chip, or Russia again, removes it.
- Stop the server and click a filter. The header and the counterparts both show an error with a Retry button. Restart the server, click Retry, and both recover.

- [ ] **Step 6: Commit**

```bash
git add frontend/js/panels/country-view.js frontend/css/country.css
git commit -m "feat(country): counterpart breakdown bars and counterpart filter"
```

---

### Task 9: Event list

**Files:**
- Modify: `frontend/js/eventlist.js`:
  - add `eventRowInner` after `statusPill` (around line 21);
  - update the `paint` loop (around lines 69-77).
- Modify: `frontend/js/panels/country-view.js`
- Modify: `frontend/css/country.css` (append)

**Interfaces:**
- Consumes:
  - `GET /api/events?country=…` (Task 2);
  - from `country-state.js`: `eventsQuery`, `PAGE`, `ctxLabel`, `emptyText`, `resetFilters` (Task 5);
  - `hooks.select` and `hooks.open` (Task 6);
  - `threatColor` from `symbology.js`.
- Produces:
  - `eventlist.eventRowInner(it, extra = "") -> string`: the inner markup of a list row, with `extra` placed after the status pill.
  - `CountryPane.loadList(mode)`, where mode is `"reset" | "more" | "refresh"`.
  - `CountryPane.renderList()` and `CountryPane.pick(id)`.

- [ ] **Step 1: Extract the shared row markup**

In `frontend/js/eventlist.js`, after the `export const statusPill = …` line add:

```js
// one event row's inner markup (glyph, title, status, outlets, place, age); the country pane's
// list (panels/country-view.js) reuses it with extra badges after the status pill
export function eventRowInner(it, extra = "") {
  const c = threatColor("events", it);
  return '<div class="g" style="color:' + c + '">' + uiGlyph("events", it) + '</div><div class="t">' + esc(evTitle(it) || it.id) + (trPending(it) ? " " + TR_TAG : "") + "</div>" +
    '<div class="m">' + statusPill(it.status) + extra + "<span>" + (it.source_diversity || 0) + " outlet" + (it.source_diversity === 1 ? "" : "s") + "</span><span>" + esc(it.place_name || it.precision || "") + '</span><time title="' + esc(evTimes(it)) + '">' + relTime(activeAt(it)) + "</time></div>";
}
```

In `paint()`, replace:
```js
      const c = threatColor("events", it);
      html += '<div class="' + cls + '" data-i="' + i + '" data-id="' + esc(it.id) + '" style="top:' + i * ROW_H + 'px;--c:' + c + '" title="' + esc(eventCat(it.category)[0]) + '">' +
        '<div class="g" style="color:' + c + '">' + uiGlyph("events", it) + '</div><div class="t">' + esc(evTitle(it) || it.id) + (trPending(it) ? " " + TR_TAG : "") + "</div>" +
        '<div class="m">' + statusPill(it.status) + "<span>" + (it.source_diversity || 0) + " outlet" + (it.source_diversity === 1 ? "" : "s") + "</span><span>" + esc(it.place_name || it.precision || "") + '</span><time title="' + esc(evTimes(it)) + '">' + relTime(activeAt(it)) + "</time></div></div>";
```
with:
```js
      html += '<div class="' + cls + '" data-i="' + i + '" data-id="' + esc(it.id) + '" style="top:' + i * ROW_H + 'px;--c:' + threatColor("events", it) + '" title="' + esc(eventCat(it.category)[0]) + '">' + eventRowInner(it) + "</div>";
```

- [ ] **Step 2: Import what the list needs**

In `frontend/js/panels/country-view.js`, replace the import lines:

```js
import { EVENT_CATS } from "/static/js/symbology.js";
import { LOCATIONS, INVOLVEMENTS, WINDOWS, LIFECYCLES, normState, summaryQuery, toggleCat, flagEmoji, sparkPath, pairLabel } from "/static/js/country-state.js";
```
with:
```js
import { EVENT_CATS, threatColor } from "/static/js/symbology.js";
import { eventRowInner } from "/static/js/eventlist.js";
import { LOCATIONS, INVOLVEMENTS, WINDOWS, LIFECYCLES, PAGE, normState, resetFilters, eventsQuery, summaryQuery, toggleCat, ctxLabel, flagEmoji, sparkPath, emptyText, pairLabel } from "/static/js/country-state.js";
```

After the `const REFRESH_MS = …` line add:

```js
// lifecycle + ctx badges after the status pill; unattributed rows say so (never "domestic")
const badges = (it) => {
  const ctx = it.ctx, lc = it.lifecycle;
  return (lc ? '<span class="cx-lc lc-' + esc(lc) + '">' + esc(lc.toUpperCase()) + "</span>" : "") +
    (ctx ? '<span class="cx-ctx">' + esc(ctxLabel(ctx)) + "</span>" : "") +
    (ctx && ctx.involvement === "unattributed" ? '<span class="cx-noact">no actor data</span>' : "");
};
```

- [ ] **Step 3: Add list state and keyboard activation**

Replace:
```js
    this.summary = null; this.sumErr = null; this.sumSeq = 0; this.advOpen = false;
```
with:
```js
    this.summary = null; this.sumErr = null; this.sumSeq = 0; this.advOpen = false;
    this.items = []; this.more = false; this.listErr = null; this.listLoading = true; this.listSeq = 0;
```

Replace:
```js
    this.root.addEventListener("change", (e) => this.change(e));
  }
```
with:
```js
    this.root.addEventListener("change", (e) => this.change(e));
    this.root.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.dataset && e.target.dataset.ev) this.click(e); });
  }
```

- [ ] **Step 4: Load and render the list**

Replace:
```js
  load() { return this.loadSummary(); }
  renderAll() { this.paintSummary(); }
```
with:
```js
  // every data source of the pane: "reset" after a filter change, "refresh" on the timer
  load(mode) { return Promise.all([this.loadSummary(), this.loadList(mode)]); }
  renderAll() { this.paintSummary(); this.renderList(); }
  // pages of PAGE events, sorted by the server (lifecycle, then last update); "refresh" re-reads
  // as many as are shown, "more" appends the next page
  async loadList(mode) {
    const seq = ++this.listSeq, append = mode === "more";
    const offset = append ? this.items.length : 0;
    const limit = mode === "refresh" ? Math.max(PAGE, this.items.length) : PAGE;
    if (mode === "reset") { this.listLoading = true; this.items = []; this.renderList(); }
    try {
      const page = await fetchJson("/api/events?" + eventsQuery(this.st, { offset, limit, allCats: this.allCats }), 20000);
      if (this.dead() || seq !== this.listSeq) return;
      const got = page.items || [];
      this.items = append ? this.items.concat(got) : got;
      this.more = got.length === limit;
      this.listErr = null;
    } catch (err) {
      if (this.dead() || seq !== this.listSeq) return;
      this.listErr = String((err && err.message) || err);
    }
    this.listLoading = false;
    this.renderList();
  }
  renderList() {
    const el = this.root.querySelector(".cx-list");
    if (this.listErr) { el.innerHTML = '<div class="cx-err"><b>Events unavailable</b><span>' + esc(this.listErr) + '</span><button type="button" data-cx="retry-list">Retry</button></div>'; return; }
    if (this.listLoading && !this.items.length) { el.innerHTML = '<div class="cx-loading">Loading events…</div>'; return; }
    if (!this.items.length) { el.innerHTML = '<div class="dz-empty cx-empty">' + esc(emptyText(this.st)) + ' <button type="button" data-cx="reset">Reset filters</button></div>'; return; }
    el.innerHTML = '<div class="cx-sub">Events <small>' + nf(this.items.length) + (this.more ? "+" : "") + " · developing first</small></div>" +
      '<div class="cx-rows">' + this.items.map((it) => '<div class="erow cx-row" role="button" tabindex="0" data-ev="' + esc(it.id) + '" style="--c:' + threatColor("events", it) + '" title="Select on the globe and open its dossier">' + eventRowInner(it, badges(it)) + "</div>").join("") + "</div>" +
      (this.more ? '<button type="button" class="cx-more" data-cx="more">Load ' + PAGE + " more</button>" : "");
  }
  // select on the globe and open the event's intel dossier in a pane beside this one
  pick(id) {
    const it = this.items.find((x) => x.id === id);
    this.hooks.open({ type: "intel", kind: "event", id, label: (it && (it.title_en || it.title)) || id }, { focus: false });
    this.hooks.select("events", id);
  }
```

- [ ] **Step 5: Handle list clicks**

Replace the whole `click(e) { … }` method with:

```js
  click(e) {
    const t = e.target.closest("button,[data-ev]");
    if (!t || !this.root.contains(t)) return;
    const st = this.st, a = t.dataset.cx;
    if (a === "retry") this.loadSummary();
    else if (a === "adv") { this.advOpen = !this.advOpen; this.renderHead(); }
    else if (a === "uncp") this.set({ ...st, counterpart: null });
    else if (a === "reset") this.set(resetFilters(st));
    else if (a === "more") this.loadList("more");
    else if (a === "retry-list") this.loadList("reset");
    else if (t.dataset.k) this.set({ ...st, [t.dataset.k]: t.dataset.v });
    else if (t.dataset.cat) this.set({ ...st, cats: toggleCat(st.cats, t.dataset.cat, this.allCats) });
    else if (t.dataset.cp) this.set({ ...st, counterpart: st.counterpart === t.dataset.cp ? null : t.dataset.cp });
    else if (t.dataset.ev) this.pick(t.dataset.ev);
  }
```

- [ ] **Step 6: Append the list styles**

Append to `frontend/css/country.css`:

```css
/* event list: eventlist.js rows in normal flow (the Events tab positions them absolutely) */
.cx-rows { display: flex; flex-direction: column; }
.cx-rows .erow { position: relative; top: auto; height: auto; min-height: 42px; padding: 4px 8px 4px 0; }
.cx-rows .erow:focus-visible { outline: none; box-shadow: var(--glow); }
.cx-ctx { font: 600 11px var(--f-mono); color: var(--acc); white-space: nowrap; }
.cx-noact { font: 11px var(--f-mono); color: var(--dim); border: 1px dashed var(--line-2); padding: 0 4px; white-space: nowrap; }
.cx-row .cx-lc { padding: 0 4px; }
.cx-empty { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 0; }
.cx-empty button, .cx-more { padding: 3px 10px; border: 1px solid var(--line-2); background: transparent; color: var(--acc); font: 11px var(--f-mono); cursor: pointer; }
.cx-more { align-self: center; margin-top: 6px; }
```

- [ ] **Step 7: Run the frontend checks**

Run: `uv run pytest tests/frontend -q`
Expected: all pass.

- [ ] **Step 8: Manual check**

The Events tab still renders rows exactly as before, which confirms the refactor is behaviour-neutral. Then open UA and check:

- The list shows rows with lifecycle and `INSIDE·FOREIGN`-style badges. `developing` rows come first.
- Unattributed rows carry "no actor data".
- Each filter combination changes the list and the counts consistently. For example, `INSIDE` + `DOMESTIC` lists only rows badged `INSIDE·DOMESTIC`.
- Clicking a counterpart narrows the list to events involving it.
- Clicking a row selects it on the globe (reticle) and opens its intel pane beside the country pane, without replacing it.
- For a busy country with 30D, "Load 200 more" appends rows.
- A filter set with no results shows "No events for UA in 7D with these filters" with "Reset filters", which restores the defaults.

- [ ] **Step 9: Commit**

```bash
git add frontend/js/eventlist.js frontend/js/panels/country-view.js frontend/css/country.css
git commit -m "feat(country): country event list with lifecycle/ctx badges, paging and reset"
```

---

### Task 10: Globe sync (outline, dimming, fit)

**Files:**
- Modify: `frontend/js/events.js`:
  - add constants after `DEFAULT_FAINT` (around line 14);
  - the constructor (around line 26);
  - `key` and `apply` (around lines 37-49);
  - add `setFocus` after `setReplay`.
- Create: `frontend/js/country-globe.js`
- Modify: `frontend/js/panels/country-view.js`
- Modify: `frontend/js/country.js` (full replacement)
- Modify: `frontend/js/browser.js`: `show`, `minimise`, `hide`, `navigate`, `history`, `close`, `focus`, plus the new `_notify`.

**Interfaces:**
- Consumes:
  - `GET /api/country/{cc}/shape` (Task 4);
  - `GET /api/events?…&ids=true` (Task 2);
  - `summary.bbox` and `summary.centroid` (Task 3);
  - the CSS tokens `--line-2` and `--acc`.
- Produces:
  - `EventLayer.setFocus(pred: ((it) => boolean) | null)`: events where `pred` is false are drawn at 25 % alpha and are never hidden.
  - `CountryGlobe(viewer, events)` with `.activate(cc)`, `.deactivate()`, `.setIds(cc, Set)`, `.fit(cc, summary)` and `.active`.
  - `country-view.js`: `setCountrySink(sink)`.
  - `Dossier._notify()` dispatches `window` event `"noctua:dossier"` with `detail = {state, focused: spec|null, specs: spec[]}`.
  - `installCountry(ctx) -> {open, globe}`.

- [ ] **Step 1: Add dimming to the event layer**

In `frontend/js/events.js`, after:
```js
const DEFAULT_FAINT = DEFAULT.withAlpha(0.5);
```
add:
```js
// country pane focus (country-globe.js): glyphs outside the pane's filtered set at 25 %
const DIM = 0.25, COLOR_DIM = {}, COLOR_FAINT_DIM = {};
for (const [k, c] of Object.entries(COLOR)) { COLOR_DIM[k] = c.withAlpha(DIM); COLOR_FAINT_DIM[k] = c.withAlpha(0.5 * DIM); }
const DEFAULT_DIM = DEFAULT.withAlpha(DIM), DEFAULT_FAINT_DIM = DEFAULT.withAlpha(0.5 * DIM);
```

Replace:
```js
    this.lastUpdated = null;
    this.filter = (it) => {
```
with:
```js
    this.lastUpdated = null;
    this.focus = null; // (it) => bool from the country pane: false = dimmed, never hidden
    this.filter = (it) => {
```

Replace:
```js
  key(it) { return it.lat + "|" + it.lon + "|" + it.category + "|" + it.subcategory + "|" + it.status + "|" + it.precision + "|" + it.confidence; }
```
with:
```js
  key(it) { return it.lat + "|" + it.lon + "|" + it.category + "|" + it.subcategory + "|" + it.status + "|" + it.precision + "|" + it.confidence + "|" + (this.focus ? (this.focus(it) ? 1 : 0) : ""); }
```

Replace:
```js
    p.color = (hollow ? COLOR_FAINT : COLOR)[it.category] || (hollow ? DEFAULT_FAINT : DEFAULT);
```
with:
```js
    const dim = !!this.focus && !this.focus(it);
    const pal = dim ? (hollow ? COLOR_FAINT_DIM : COLOR_DIM) : (hollow ? COLOR_FAINT : COLOR);
    p.color = pal[it.category] || (dim ? (hollow ? DEFAULT_FAINT_DIM : DEFAULT_DIM) : (hollow ? DEFAULT_FAINT : DEFAULT));
```

Replace:
```js
  setReplay(at) { this.replayAt = at ? at.getTime() : null; this.refilter(); }
```
with:
```js
  setReplay(at) { this.replayAt = at ? at.getTime() : null; this.refilter(); }
  // dim (not hide) every glyph the predicate rejects; null restores normal colours
  setFocus(pred) { this.focus = pred || null; this.restyle(); }
```

- [ ] **Step 2: Create the globe sync**

Create `frontend/js/country-globe.js`:

```js
// Globe sync for the country pane (spec B §3.3). While a country pane is the active context:
// its outline in --line-2 over a faint (6 %) amber fill, event glyphs outside the pane's
// filtered set dimmed to 25 % (EventLayer.setFocus, never removed), and the camera fitted to
// the country's bbox once per country per session (no re-fit on filter changes; a country
// without a polygon gets no outline and a fly-to on its centroid). deactivate() restores all.
import { fetchJson } from "/static/js/api.js";

const C = window.Cesium;
const FILL_ALPHA = 0.06;
const cssVar = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

export class CountryGlobe {
  constructor(viewer, events) {
    this.viewer = viewer; this.scene = viewer.scene; this.events = events;
    this.active = null;          // ISO2 of the active country pane, or null
    this.sets = new Map();       // cc -> Set of event ids in that pane's filtered set
    this.pendingFit = new Map(); // cc -> summary waiting for its pane to become active
    this.fitted = new Set();     // countries already fitted this session
    this.shapes = new Map();     // cc -> polygons ([] = no polygon)
    this.fill = null; this.edges = null; this.seq = 0;
  }
  activate(cc) {
    if (!cc || this.active === cc) return;
    this.active = cc;
    this._dim();
    this._outline(cc);
    const s = this.pendingFit.get(cc);
    if (s) this.fit(cc, s);
  }
  deactivate() {
    if (!this.active) return;
    this.active = null; this.seq++;
    this.events.setFocus(null);
    this._clearOutline();
  }
  setIds(cc, ids) { this.sets.set(cc, ids); if (this.active === cc) this._dim(); }
  fit(cc, summary) {
    if (this.fitted.has(cc)) return;
    if (this.active !== cc) { this.pendingFit.set(cc, summary); return; }
    this.pendingFit.delete(cc); this.fitted.add(cc);
    const b = summary.bbox;
    if (b && b.length === 4) {
      const [w, s, e, n] = b, spanX = e >= w ? e - w : e - w + 360;
      const px = Math.max(0.5, spanX * 0.08), py = Math.max(0.5, (n - s) * 0.08);
      this.viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(Math.max(-180, w - px), Math.max(-90, s - py), Math.min(180, e + px), Math.min(90, n + py)), duration: 1.4 });
    } else if (summary.centroid) {
      this.viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(summary.centroid[1], summary.centroid[0], 3e6), duration: 1.4 });
    }
  }
  _dim() {
    const ids = this.active ? this.sets.get(this.active) : null;
    this.events.setFocus(ids ? (it) => ids.has(it.id) : null);
  }
  async _outline(cc) {
    const seq = ++this.seq;
    this._clearOutline();
    let polys = this.shapes.get(cc);
    if (!polys) {
      try { polys = (await fetchJson("/api/country/" + encodeURIComponent(cc) + "/shape", 20000)).polygons || []; } catch (e) { polys = []; }
      this.shapes.set(cc, polys);
    }
    if (seq !== this.seq || this.active !== cc || !polys.length) return;
    const line = C.Color.fromCssColorString(cssVar("--line-2", "rgba(255,176,0,.28)"));
    const fill = C.Color.fromCssColorString(cssVar("--acc", "#ffb000")).withAlpha(FILL_ALPHA);
    const mat = C.Material.fromType("Color", { color: line });
    const inst = [];
    this.edges = this.scene.primitives.add(new C.PolylineCollection());
    for (const poly of polys) {
      if (!poly.length || poly[0].length < 6) continue;
      const holes = poly.slice(1).filter((h) => h.length >= 6).map((h) => new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(h)));
      inst.push(new C.GeometryInstance({
        geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(poly[0]), holes), vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT, granularity: C.Math.RADIANS_PER_DEGREE * 2 }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(fill) },
      }));
      for (const ring of poly) if (ring.length >= 6) this.edges.add({ positions: C.Cartesian3.fromDegreesArray([...ring, ring[0], ring[1]]), width: 1.5, material: mat });
    }
    if (inst.length) this.fill = this.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false }), asynchronous: false, allowPicking: false }));
    this.scene.requestRender();
  }
  _clearOutline() {
    if (this.fill) { this.scene.primitives.remove(this.fill); this.fill = null; }
    if (this.edges) { this.scene.primitives.remove(this.edges); this.edges = null; }
    this.scene.requestRender();
  }
}
```

- [ ] **Step 3: Feed the globe from the pane**

In `frontend/js/panels/country-view.js`, after the `const REFRESH_MS = …` line add:

```js
// globe sync (country-globe.js), installed by country.js: fit(cc, summary), setIds(cc, Set)
const noop = () => {};
let sink = { fit: noop, setIds: noop };
export function setCountrySink(s) { sink = s || { fit: noop, setIds: noop }; }
```

Replace:
```js
    this.items = []; this.more = false; this.listErr = null; this.listLoading = true; this.listSeq = 0;
```
with:
```js
    this.items = []; this.more = false; this.listErr = null; this.listLoading = true; this.listSeq = 0; this.idSeq = 0;
```

Replace:
```js
      this.summary = s; this.sumErr = null;
      this.title(s);
```
with:
```js
      this.summary = s; this.sumErr = null;
      this.title(s);
      sink.fit(s.cc, s);
```

Replace:
```js
  load(mode) { return Promise.all([this.loadSummary(), this.loadList(mode)]); }
```
with:
```js
  load(mode) { return Promise.all([this.loadSummary(), this.loadList(mode), this.loadIds()]); }
  // the whole filtered set (not just the shown page) for the globe's dimming
  async loadIds() {
    const seq = ++this.idSeq, cc = this.st.cc;
    try {
      const r = await fetchJson("/api/events?" + eventsQuery(this.st, { limit: 10000, allCats: this.allCats, ids: true }), 20000);
      if (this.dead() || seq !== this.idSeq) return;
      sink.setIds(cc, new Set(r.ids || []));
    } catch (err) {
      console.warn("country ids", err); // the globe keeps the previous set; the list shows the error
    }
  }
```

- [ ] **Step 4: Tell modules which pane is active**

In `frontend/js/browser.js`, make these replacements.

Replace:
```js
  show() { this.state = "open"; this.el.classList.remove("hidden"); this.chip.classList.add("hidden"); this._layout(); }
```
with:
```js
  show() { this.state = "open"; this.el.classList.remove("hidden"); this.chip.classList.add("hidden"); this._layout(); this._notify(); }
```

Replace:
```js
    this.chip.innerHTML = "<i></i>DOSSIER <b>" + this.panes.length + "</b>" + this.panes.slice(0, 4).map((p) => "<span>" + esc(p.title || "") + "</span>").join("");
  }
```
with:
```js
    this.chip.innerHTML = "<i></i>DOSSIER <b>" + this.panes.length + "</b>" + this.panes.slice(0, 4).map((p) => "<span>" + esc(p.title || "") + "</span>").join("");
    this._notify();
  }
```

Replace:
```js
  hide() { this.state = "closed"; this.el.classList.add("hidden"); this.chip.classList.add("hidden"); }
```
with:
```js
  hide() { this.state = "closed"; this.el.classList.add("hidden"); this.chip.classList.add("hidden"); this._notify(); }
```

Replace:
```js
    pane.spec = spec; this._render(pane); this._save();
```
with:
```js
    pane.spec = spec; this._render(pane); this._save(); this._notify();
```

Replace:
```js
    pane.hi = i; pane.spec = pane.hist[i]; this._render(pane);
```
with:
```js
    pane.hi = i; pane.spec = pane.hist[i]; this._render(pane); this._notify();
```

Replace:
```js
    this._layout();
    if (!quiet) this._save();
  }
```
with:
```js
    this._layout();
    if (!quiet) this._save();
    this._notify();
  }
```

Replace:
```js
  focus(pane) {
    for (const p of this.panes) p.el.classList.toggle("focus", p === pane);
    this.focused = pane;
  }
```
with:
```js
  focus(pane) {
    for (const p of this.panes) p.el.classList.toggle("focus", p === pane);
    this.focused = pane;
    this._notify();
  }
  // which pane has focus and what is open, for feature modules (country.js globe sync)
  _notify() {
    window.dispatchEvent(new CustomEvent("noctua:dossier", { detail: { state: this.state, focused: this.focused ? this.focused.spec : null, specs: this.panes.map((p) => p.spec) } }));
  }
```

- [ ] **Step 5: Wire the globe in the installer**

Replace the whole of `frontend/js/country.js` with:

```js
// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js), drives
// the globe sync (country-globe.js) from the active country pane and exposes open(cc) for the
// palette, the globe and pivots. Returns { open, globe }.
import { COUNTRY_PANE, setCountrySink } from "/static/js/panels/country-view.js";
import { CountryGlobe } from "/static/js/country-globe.js";

const ccOf = (s) => String((s && s.cc) || "").toUpperCase();

export function installCountry(ctx) {
  const { viewer, dossier, byKind } = ctx;
  const globe = new CountryGlobe(viewer, byKind.events);
  setCountrySink(globe);
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, { country: COUNTRY_PANE });
  // the active country is the focused country pane, kept while it stays open (opening an
  // event's intel pane beside it keeps the context); closing it, minimising or closing the
  // dossier, or Esc restores the globe
  window.addEventListener("noctua:dossier", (e) => {
    const d = e.detail || {};
    if (d.state !== "open") { globe.deactivate(); return; }
    if (d.focused && d.focused.type === "country") { globe.activate(ccOf(d.focused)); return; }
    if (globe.active && !(d.specs || []).some((s) => s && s.type === "country" && ccOf(s) === globe.active)) globe.deactivate();
  });
  // capture phase: runs even when the dossier's own Esc handler stops propagation
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !/INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) globe.deactivate();
  }, true);
  window.dispatchEvent(new Event("noctua:panels")); // renders country panes restored before this ran
  const open = (cc) => {
    const code = String(cc || "").trim().toUpperCase();
    return /^[A-Z]{2}$/.test(code) ? dossier.open({ type: "country", cc: code }) : null;
  };
  return { open, globe };
}
```

- [ ] **Step 6: Run the frontend checks**

Run: `uv run pytest tests/frontend -q`
Expected: all pass.

- [ ] **Step 7: Manual check**

Turn the Events layer on and open UA. Check:

- The camera fits Ukraine with padding, once.
- An amber `--line-2` outline with a faint fill is drawn.
- Event glyphs outside the pane's filtered set are visibly dimmed, about 25 %, while those inside stay full.
- Changing a filter re-dims to the new set without re-fitting the camera.
- Clicking a row opens the intel pane beside the country pane. The outline and dimming stay.
- Pressing Esc, closing the country pane, minimising the dossier with `B`, or "close all" each restores the normal colours and removes the outline.
- Focusing the country pane again (click its body) restores the outline and dimming, without re-fitting.
- Open a country with no polygon, such as `__osint.country.open("MC")` (Monaco, if your data lacks it), or any code whose `/api/country/XX/shape` returns `[]`. There is no outline, and the camera flies to its centroid.

- [ ] **Step 8: Commit**

```bash
git add frontend/js/events.js frontend/js/country-globe.js frontend/js/panels/country-view.js frontend/js/country.js frontend/js/browser.js
git commit -m "feat(country): globe sync - outline, 25% dimming of other events, one-time bbox fit"
```

---

### Task 11: Entry points (palette, Alt+click, pivots)

**Files:**
- Modify: `frontend/js/palette.js` (the Places `run`, around lines 96-97)
- Modify: `frontend/js/country.js` (full replacement)
- Modify: `frontend/js/browser.js`: the pivot click in `_wire`, around line 522.
- Modify: `frontend/js/terror.js`:
  - a helper after `pivot` (line 52);
  - lines 286, 304, 306, 307, 331 and 451.
- Modify: `frontend/js/panels/intel-view.js`: the `country(C)` renderer, around lines 323-328.
- Modify: `frontend/css/country.css` (append)

**Interfaces:**
- Consumes:
  - `ctx.country.open(cc)` (Task 6);
  - `GET /api/country/at` (Task 4);
  - `/api/reference/countries` items with `iso_a2` (Task 4);
  - the `#pick-tip` element.
- Produces:
  - `data-newpane="1"` on a `data-pivot` element opens the spec in a new pane instead of navigating the current one.
  - Alt+click on the globe opens the country under the cursor.
  - A one-time hint is stored in localStorage under `noctua.country.althint`.

- [ ] **Step 1: Make pivots able to open beside the current pane**

In `frontend/js/browser.js` replace:

```js
        if (e.shiftKey || e.ctrlKey) this.open(spec); else this.navigate(pane, spec);
      } else if (t.dataset.sel) this.select("events", t.dataset.sel);
```
with:
```js
        if (e.shiftKey || e.ctrlKey || t.dataset.newpane === "1") this.open(spec); else this.navigate(pane, spec);
      } else if (t.dataset.sel) this.select("events", t.dataset.sel);
```

- [ ] **Step 2: Palette: open the pane, then fly**

In `frontend/js/palette.js` replace:

```js
          run: () => { if (p.ref) c.showRef(p.ref); c.flyTo(it.lat, it.lon, p.kind === "country" ? 3e6 : p.kind === "chokepoint" ? 1e6 : 2.5e5); } });
```
with:
```js
          // a country opens its context pane (country.js), then the camera flies there
          run: () => { if (p.kind === "country" && it.iso_a2 && c.country) c.country.open(it.iso_a2); else if (p.ref) c.showRef(p.ref); c.flyTo(it.lat, it.lon, p.kind === "country" ? 3e6 : p.kind === "chokepoint" ? 1e6 : 2.5e5); } });
```

- [ ] **Step 3: Alt+click and the one-time hint**

Replace the whole of `frontend/js/country.js` with:

```js
// COUNTRY CONTEXT (spec B): registers the `country` dossier pane (panels/country-view.js), drives
// the globe sync (country-globe.js) from the active country pane, opens a country on Alt+click
// on the globe and exposes open(cc) for the palette and pivots. Returns { open, globe }.
import { fetchJson } from "/static/js/api.js";
import { $ } from "/static/js/ui.js";
import { COUNTRY_PANE, setCountrySink } from "/static/js/panels/country-view.js";
import { CountryGlobe } from "/static/js/country-globe.js";

const HINT_KEY = "noctua.country.althint";
const ccOf = (s) => String((s && s.cc) || "").toUpperCase();

function tip(text, ms = 4000) {
  const el = $("pick-tip");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), ms);
}

export function installCountry(ctx) {
  const { viewer, dossier, byKind } = ctx;
  const C = window.Cesium;
  const globe = new CountryGlobe(viewer, byKind.events);
  setCountrySink(globe);
  window.__noctuaPanes = Object.assign(window.__noctuaPanes || {}, { country: COUNTRY_PANE });
  // the active country is the focused country pane, kept while it stays open (opening an
  // event's intel pane beside it keeps the context); closing it, minimising or closing the
  // dossier, or Esc restores the globe
  window.addEventListener("noctua:dossier", (e) => {
    const d = e.detail || {};
    if (d.state !== "open") { globe.deactivate(); return; }
    if (d.focused && d.focused.type === "country") { globe.activate(ccOf(d.focused)); return; }
    if (globe.active && !(d.specs || []).some((s) => s && s.type === "country" && ccOf(s) === globe.active)) globe.deactivate();
  });
  // capture phase: runs even when the dossier's own Esc handler stops propagation
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !/INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) globe.deactivate();
  }, true);
  window.dispatchEvent(new Event("noctua:panels")); // renders country panes restored before this ran
  const open = (cc) => {
    const code = String(cc || "").trim().toUpperCase();
    return /^[A-Z]{2}$/.test(code) ? dossier.open({ type: "country", cc: code }) : null;
  };
  // Alt + click on land: the country under the cursor (server-side point-in-polygon). A plain
  // click keeps selecting entities and events (app.js), Cesium routes the two separately.
  const handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction(async (click) => {
    const cart = viewer.camera.pickEllipsoid(click.position, viewer.scene.globe.ellipsoid);
    if (!cart) return;
    const g = C.Cartographic.fromCartesian(cart);
    const lat = C.Math.toDegrees(g.latitude), lon = C.Math.toDegrees(g.longitude);
    const r = await fetchJson("/api/country/at?lat=" + lat.toFixed(4) + "&lon=" + lon.toFixed(4), 8000).catch(() => null);
    if (r && r.cc) open(r.cc); else tip("No country here");
  }, C.ScreenSpaceEventType.LEFT_CLICK, C.KeyboardEventModifier.ALT);
  // shown once per browser, on the first plain click on the globe
  const hint = (e) => {
    if (e.altKey || e.button !== 0) return;
    viewer.scene.canvas.removeEventListener("pointerup", hint);
    let seen = true;
    try { seen = localStorage.getItem(HINT_KEY) === "1"; if (!seen) localStorage.setItem(HINT_KEY, "1"); } catch (err) { seen = true; }
    if (!seen) tip("Tip: Alt + click a country to open its context", 6000);
  };
  viewer.scene.canvas.addEventListener("pointerup", hint);
  return { open, globe };
}
```

- [ ] **Step 4: Terror Watch "Country context" links**

In `frontend/js/terror.js`, insert directly above `const intelSpec = (i) => ({ type: "intel", kind: "event", id: i.event_id, label: i.title || i.event_id });`:

```js
// "Country context" link: opens the country pane (country.js) beside the board, which stays its own pane
const ctxLink = (cc) => (cc ? ' <a href="#" class="tw-ctx" data-newpane="1" data-pivot=\'' + esc(JSON.stringify({ type: "country", cc })) + "'>Country context</a>" : "");
```

Make these five replacements, each matching exactly once:

1. In `renderCountry`:
   `pivot({ type: "terror-def" }, "definition &amp; sources") + "</div>" +`
   becomes
   `pivot({ type: "terror-def" }, "definition &amp; sources") + " ·" + ctxLink(cc) + "</div>" +`
2. Hotspots in `renderGlobal`:
   `"<tr><td>" + pivot({ type: "terror-country", cc: h.country }, esc(h.name)) + "</td><td>"`
   becomes
   `"<tr><td>" + pivot({ type: "terror-country", cc: h.country }, esc(h.name)) + ctxLink(h.country) + "</td><td>"`
3. News by country:
   `pivot({ type: "terror-country", cc: c.country }, esc(c.name)) + " " + nf(c.attacks) + "</li>"`
   becomes
   `pivot({ type: "terror-country", cc: c.country }, esc(c.name)) + " " + nf(c.attacks) + ctxLink(c.country) + "</li>"`
4. Official levels:
   `pivot({ type: "terror-country", cc: lv.country }, esc(lv.country)) + "</td>`
   becomes
   `pivot({ type: "terror-country", cc: lv.country }, esc(lv.country)) + ctxLink(lv.country) + "</td>`
5. Group areas in `renderGroup`:
   `pivot({ type: "terror-country", cc: a.country }, esc(a.name)) + "</td>`
   becomes
   `pivot({ type: "terror-country", cc: a.country }, esc(a.name)) + ctxLink(a.country) + "</td>`

In `TerrorPanel.row` (the level / advisory change item; the right panel uses `data-open`), replace:

```js
" → " + (x.level ?? "?")) + "</a>" +
```
with:
```js
" → " + (x.level ?? "?")) + "</a>" + ' <a href="#" class="tw-ctx" data-open=\'' + esc(JSON.stringify({ type: "country", cc: x.country })) + "'>Country context</a>" +
```

- [ ] **Step 5: Intel `ctry` tile link**

In `frontend/js/panels/intel-view.js`, insert above `function country(C) {`:

```js
// the full country context pane (country.js), opened beside the intel pane
const countryLink = (cc) => (cc ? '<a href="#" class="cx-link" data-newpane="1"' + pivot({ type: "country", cc }) + ">Country context →</a>" : "");
```
and replace:
```js
  let h = '<div class="ic-kv"><div><b>' + nf(T.last7)
```
with:
```js
  let h = countryLink(C.cc) + '<div class="ic-kv"><div><b>' + nf(T.last7)
```

- [ ] **Step 6: Append the link styles**

Append to `frontend/css/country.css`:

```css
/* "Country context" links (Terror Watch boards and feed, intel country tile) */
.tw-ctx, .cx-link { color: var(--acc); text-decoration: underline dotted; text-underline-offset: 2px; font: 11px var(--f-mono); white-space: nowrap; }
.cx-link { display: inline-block; margin-bottom: 6px; }
```

- [ ] **Step 7: Run the frontend checks**

Run: `uv run pytest tests/frontend -q`
Expected: all pass.

- [ ] **Step 8: Manual check of every entry point**

1. **Palette:** press Ctrl+K and type "ukraine". Choosing "Ukraine · Country" opens the UA pane and the camera flies there.
2. **Alt+click:**
   - Alt+click on France opens the FR pane.
   - Alt+click on open sea shows "No country here".
   - A plain click still selects events and tracks.
   - In a fresh profile (or after `localStorage.removeItem("noctua.country.althint")`), the first plain click shows the Alt+click hint once.
3. **Terror pivot:** open Terror Watch's global board. Each hotspot, news row and level row has a "Country context" link that opens the country pane in a **new** pane, while the board stays. The same holds for a group's areas, a country board's disclaimer line, and a level change in the right-panel Terror feed.
4. **Intel tile:** select an event, press `I`, and find the "Country context" tile. "Country context →" opens the country pane beside the intel pane.

- [ ] **Step 9: Commit**

```bash
git add frontend/js/palette.js frontend/js/country.js frontend/js/browser.js frontend/js/terror.js frontend/js/panels/intel-view.js frontend/css/country.css
git commit -m "feat(country): entry points - palette, Alt+click, terror board and intel tile pivots"
```

---

### Task 12: Verification

**Files:**
- None, unless a check fails. In that case fix in the task's files and re-run.

**Interfaces:**
- Consumes: everything above.
- Produces: a green suite, a built bundle, and the PR screenshots.

- [ ] **Step 1: Full test suite and lint**

```bash
uv run pytest -q
uv run ruff check backend tests
```
Expected:
- pytest: all pass. Tests marked `slow` may skip when their data is absent.
- ruff: `All checks passed!`

- [ ] **Step 2: Production bundle**

```bash
uv run python -m osint.frontend_build
```
Expected: it exits 0 and writes `frontend/dist/index.html` plus `assets/app-<hash>.js` / `.css`. A `BuildError` means an import or export outside the supported subset. Fix that module.

- [ ] **Step 3: Real-data sanity**

Start `uv run osint run`, then in another shell:

```bash
curl -s "http://127.0.0.1:8765/api/country/UA/summary" -o /dev/null -w "%{http_code} %{time_total}s\n"
curl -s "http://127.0.0.1:8765/api/events?all=true&country=UA&location=abroad&limit=5" | head -c 600; echo
curl -s "http://127.0.0.1:8765/api/events?country=ZZ"; echo
```
Expected:
- `200` with a time under 1 s;
- abroad items whose `ctx.location` is `"abroad"`;
- `{"error":"unknown_country"}`.

If the server runs with `OSINT_API_TOKEN`, add `-H "Authorization: Bearer $OSINT_API_TOKEN"` to each curl.

- [ ] **Step 4: Spec §5 frontend checklist (dist build and dev)**

Open `http://127.0.0.1:8765` and check:

- [ ] Each entry point works: palette, Alt+click, terror pivot, intel tile (Task 11, Step 8).
- [ ] Every location × involvement combination changes the list and the counts consistently, including `UNATTRIB.` showing only "no actor data" rows.
- [ ] Counterpart filter and chip removal work.
- [ ] The window `24H`/`7D`/`30D`/`CUSTOM` and category chips work.
- [ ] The globe dims non-matching events and restores them on close, Esc, minimise and close all.
- [ ] The state survives a reload. Set INSIDE + FOREIGN + 30D + one category off + a counterpart, reload, and press `B`: everything is restored.
- [ ] Error states: stop the server, then use Retry for the summary and the list. The empty state offers "Reset filters".
- [ ] DevTools console shows no errors.

- [ ] **Step 5: Screenshots for the PR**

Use the `run` skill (or a headless browser) to capture:
1. the UA pane with header, filters, counterparts and list over the globe, showing the outline and dimming;
2. `INSIDE·FOREIGN` with a `UA ↔ RU` chip;
3. the empty state;
4. the summary error state;
5. the Terror Watch board with "Country context" links.

Save them under the scratchpad and attach them to the PR.

- [ ] **Step 6: Final commit (only if Steps 1-4 needed fixes)**

```bash
git status --short
git add backend frontend tests
git commit -m "fix(country): verification fixes"
```
Expected: `git status --short` is empty afterwards.

---

## Spec Coverage

| Spec item | Task |
|---|---|
| §1 location / involvement definitions, unattributed never domestic, RU→UA from both sides | 1, 2 |
| §2.1 `country`, `location`, `involvement`, `counterpart`, `ctx`, `json_each` bounded by `since`, 400 `unknown_country` | 1, 2 |
| §2.2 summary: threat + advisories via the terror board (null on failure), counts, lifecycle, 30-day spark, top actors, top-15 counterparts, bbox | 3 (centroid fallback: 3; polygons: 4) |
| §3.1 palette opens then flies; Alt+click with one-time hint; pivots from the terror board and intel tile, the board stays its own pane | 11 (`data-newpane`) |
| §3.2 header strip | 6 |
| §3.2 filter bar with counts, 7D default, custom dates, category chips | 7 |
| §3.2 counterpart bars + removable `UA ↔ RU` chip | 8 |
| §3.2 event list with lifecycle + ctx badges, row click selects and opens the dossier beside, lifecycle sort, 200 paging | 9 |
| §3.2 state object in the pane spec, survives reload and history; 60 s refresh | 5, 6, 7 |
| §3.3 outline `--line-2` + 6 % amber fill, 25 % dimming, one-time fit, restore on close / Esc | 10 |
| §4 summary error + retry, list independent, empty state + reset, no-polygon → centroid fly-to | 6, 8, 9, 10 |
| §5 backend tests (fixture DB, counterpart, 400, summary shape, threat null) | 1, 2, 3, 4 |
| §5 frontend manual checks + screenshots | 6-12 |
