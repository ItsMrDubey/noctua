# M0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the legacy `osint/` scripts into a Python package that collects global military ADS-B, AIS, NASA FIRMS and USGS data on a resilient async scheduler. Data goes into SQLite (tracks/state) plus hour-partitioned Parquet (positions/observations), with health and metrics endpoints.

**Architecture:** One process (`osint run`) runs a FastAPI app whose lifespan starts a `Runtime`. The Runtime holds the stores, a bounded-queue `Ingestor`, and a `Scheduler` that drives poll and stream collectors with backoff and a circuit breaker. Collectors return `Batch` objects of msgspec structs. The Ingestor validates, quarantines, thins positions, dedupes observations and buffers, then writes Parquet in one atomic step. H3 cells (resolutions 3/5/7) are attached to every geo row.

**Tech Stack:** Python 3.12 (uv), FastAPI, uvicorn, httpx, websockets, msgspec, pydantic-settings, PyYAML, pyarrow, DuckDB, h3 v4; pytest, pytest-asyncio, respx, ruff.

**Spec:** `docs/superpowers/specs/2026-11-21-global-event-intel-design.md` (§2 architecture, §3 data model, §4 sources, §9 reliability, §11 M0).

## Global Constraints

- Python `>=3.12`, managed with `uv`. All commands run from the repo root `C:\Users\ItsMr\Documents\Projects\osint`, through `uv run ...`.
- Package lives at `backend/osint/` and is imported as `osint`.
- All timestamps are timezone-aware **UTC** internally. (IST appears only in later digest output.)
- Secrets come only from `.env` (git-ignored) via `Settings`. No key literals in tracked files.
- Coverage is **global**: no hardcoded regional bounding boxes.
- Position thinning: **air 60 s, sea 300 s** per track.
- Every collector declares `name`, `domain`, `licence`, `commercial_ok`.
- H3 resolutions **3, 5, 7**, stored as unsigned 64-bit integers.
- Parquet layout: `data/lake/<dataset>/date=YYYY-MM-DD/hour=HH/part-*.parquet`, zstd compression, written to `.tmp` and then `os.replace`d.
- Bad records go to the SQLite `quarantine` table with a reason. They are never silently dropped.
- Do not modify files in `legacy/` except the key scrub in Task 1.

## File Structure

```
pyproject.toml                       project + deps + tool config
.env.example                         documented env vars (no values)
config/sources.yaml                  per-collector enable/interval/params
reference/plane-alert-mil.csv        military aircraft DB (moved from root)
legacy/                              old scripts (keys scrubbed), reference only
backend/osint/__init__.py
backend/osint/config.py              Settings + load_sources()
backend/osint/cli.py                 `osint run`
backend/osint/runtime.py             Runtime: wires store/lake/ingestor/scheduler/http
backend/osint/core/models.py         Position, TrackInfo, Observation, Batch
backend/osint/core/geo.py            h3_cells, haversine_km, valid_coord, utcnow
backend/osint/core/metrics.py        Metrics (counters/gauges, Prometheus text)
backend/osint/core/collector.py      BaseCollector/PollCollector/StreamCollector, CollectorContext, registry
backend/osint/core/scheduler.py      Scheduler + SourceHealth
backend/osint/store/sqlite.py        SqliteStore (sources, source_state, tracks, quarantine)
backend/osint/store/lake.py          Lake (Parquet write, DuckDB query) + row converters
backend/osint/pipeline/ingest.py     Ingestor (validate, thin, dedupe, buffer, flush, live state)
backend/osint/enrich/mil_types.py    copied verbatim from legacy
backend/osint/enrich/military.py     MilDb loader
backend/osint/collectors/__init__.py imports collector modules (registration)
backend/osint/collectors/adsb.py     global /mil from several ADS-B aggregators
backend/osint/collectors/ais.py      AISStream global websocket
backend/osint/collectors/firms.py    NASA FIRMS world CSV
backend/osint/collectors/usgs.py     USGS GeoJSON feeds
backend/osint/api/app.py             create_app(runtime): /health /metrics /api/sources /api/live/summary
tests/...                            one test module per unit
README.md                            setup + run
```

---

### Task 1: Repository hygiene — move legacy, scrub keys, reference data

**Files:**
- Create: `legacy/` (moved files), `reference/plane-alert-mil.csv`, `.env.example`
- Modify: `.gitignore`, `legacy/fire_data.py:10`, `legacy/fire_data.py.txt:6`, `legacy/ship_data.py:8`

**Interfaces:**
- Consumes: nothing
- Produces: `reference/plane-alert-mil.csv` (used by Task 10), `.env.example` (variable names used by Task 2)

- [ ] **Step 1: Move the files**

Run (Git Bash):
```bash
cd /c/Users/ItsMr/Documents/Projects/osint
mkdir -p legacy reference data/legacy-outputs
mv bases.py correlation.py fire_data.py fire_data.py.txt flight_data.py mil_filter.py mil_types.py \
   seismic_data.py serve.py ship_data.py storage.py update_dash.py update_dash2.py update_dash3.py \
   update_dash4.py dashboard.html legacy/
mv plane-alert-mil.csv reference/
mv bases.json Bases.kml correlations.json fires.json flights.json seismic.json ships.json \
   LiveFires.kml LiveFlights.kml LiveSeismic.kml LiveShips.kml data/legacy-outputs/
mv osint.db data/legacy-osint.db
rm -rf __pycache__
ls
```
Expected `ls` output: `data  docs  legacy  reference` (plus dotfiles).

- [ ] **Step 2: Scrub the hardcoded keys**

```bash
sed -i 's/^NASA_MAP_KEY = ".*"/NASA_MAP_KEY = __import__("os").environ.get("FIRMS_MAP_KEY", "")/' legacy/fire_data.py legacy/fire_data.py.txt
sed -i 's/^AISSTREAM_API_KEY = ".*"/AISSTREAM_API_KEY = __import__("os").environ.get("AISSTREAM_API_KEY", "")/' legacy/ship_data.py
grep -rnE '[0-9a-f]{32,}' legacy/ ; echo "exit=$?"
```
Expected: no matches, `exit=1`.

- [ ] **Step 3: Write `.env.example`**

```dotenv
# Copy to .env and fill in. .env is git-ignored.
# NASA FIRMS map key: https://firms.modaps.eosdis.nasa.gov/api/map_key/
FIRMS_MAP_KEY=
# AISStream API key: https://aisstream.io/apikeys
AISSTREAM_API_KEY=
# AlterLab API key (used from M2)
ALTERLAB_API_KEY=
# Optional overrides
# OSINT_DATA_DIR=./data
# OSINT_CONFIG_DIR=./config
# OSINT_HOST=127.0.0.1
# OSINT_PORT=8765
```

- [ ] **Step 4: Extend `.gitignore`**

Append:
```gitignore
.pytest_cache/
.ruff_cache/
*.egg-info/
```

- [ ] **Step 5: Commit**

```bash
git add .gitignore .env.example legacy reference
git status --short
git commit -m "chore: move legacy scripts, scrub hardcoded keys, add reference data"
```
Expected `git status` before commit: only `A` entries under `legacy/`, `reference/`, plus `.env.example` and `M .gitignore`. Nothing from `data/`.

---

### Task 2: Project scaffold and Settings

**Files:**
- Create: `pyproject.toml`, `backend/osint/__init__.py`, `backend/osint/config.py`, `config/sources.yaml`, `tests/__init__.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `.env.example` variable names (Task 1)
- Produces:
  - `osint.config.ROOT: Path` (repo root)
  - `osint.config.Settings` with fields `firms_map_key: str`, `aisstream_api_key: str`, `alterlab_api_key: str`, `data_dir: Path`, `config_dir: Path`, `host: str`, `port: int` and properties `sqlite_path`, `lake_dir`, `reference_dir`
  - `osint.config.load_sources(config_dir: Path) -> dict[str, dict[str, Any]]`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "osint"
version = "0.1.0"
description = "Global event intelligence platform (POC)"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn>=0.32",
  "httpx>=0.27",
  "websockets>=13",
  "msgspec>=0.18",
  "pydantic-settings>=2.6",
  "pyyaml>=6.0",
  "pyarrow>=17",
  "duckdb>=1.1",
  "h3>=4.1",
]

[project.scripts]
osint = "osint.cli:main"

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "respx>=0.21", "ruff>=0.7"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["backend/osint"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["backend", "tests"]
```

- [ ] **Step 2: Create the package and sync**

```bash
mkdir -p backend/osint tests config
printf '"""Global event intelligence platform."""\n' > backend/osint/__init__.py
: > tests/__init__.py
uv python pin 3.12
uv sync
```
Expected: `uv sync` ends with `Installed N packages` and creates `.venv/`.

- [ ] **Step 3: Write `config/sources.yaml`**

```yaml
# One block per collector. Missing keys use the collector's defaults.
adsb:
  enabled: true
  interval_s: 30
  endpoints:
    adsb.lol: https://api.adsb.lol/v2
    airplanes.live: https://api.airplanes.live/v2
    adsb.fi: https://opendata.adsb.fi/api/v2
ais:
  enabled: true
  batch_seconds: 5
firms:
  enabled: true
  interval_s: 900
  area: world
  days: 1
usgs:
  enabled: true
  interval_s: 120
```

- [ ] **Step 4: Write the failing test `tests/test_config.py`**

```python
from pathlib import Path

from osint.config import ROOT, Settings, load_sources


def test_settings_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMS_MAP_KEY", "abc")
    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)
    assert s.firms_map_key == "abc"
    assert s.data_dir == tmp_path
    assert s.sqlite_path == tmp_path / "osint.sqlite"
    assert s.lake_dir == tmp_path / "lake"


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("OSINT_DATA_DIR", raising=False)
    s = Settings(_env_file=None)
    assert s.data_dir == ROOT / "data"
    assert s.port == 8765
    assert s.reference_dir == ROOT / "reference"


def test_load_sources_normalises_empty_blocks(tmp_path: Path):
    (tmp_path / "sources.yaml").write_text("usgs:\n  interval_s: 120\nais:\n", encoding="utf-8")
    assert load_sources(tmp_path) == {"usgs": {"interval_s": 120}, "ais": {}}


def test_repo_sources_yaml_lists_m0_collectors():
    assert set(load_sources(ROOT / "config")) == {"adsb", "ais", "firms", "usgs"}
```

- [ ] **Step 5: Run it and confirm it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.config'`.

- [ ] **Step 6: Write `backend/osint/config.py`**

```python
"""Runtime settings (.env + environment) and YAML source config."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    firms_map_key: str = ""
    aisstream_api_key: str = ""
    alterlab_api_key: str = ""
    data_dir: Path = Field(default=ROOT / "data", alias="OSINT_DATA_DIR")
    config_dir: Path = Field(default=ROOT / "config", alias="OSINT_CONFIG_DIR")
    host: str = Field(default="127.0.0.1", alias="OSINT_HOST")
    port: int = Field(default=8765, alias="OSINT_PORT")

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "osint.sqlite"

    @property
    def lake_dir(self) -> Path:
        return self.data_dir / "lake"

    @property
    def reference_dir(self) -> Path:
        return ROOT / "reference"


def load_sources(config_dir: Path) -> dict[str, dict[str, Any]]:
    """Load config/sources.yaml as {collector_name: config_dict}."""
    with (config_dir / "sources.yaml").open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {name: (cfg or {}) for name, cfg in data.items()}
```

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock .python-version backend config tests
git commit -m "feat: project scaffold with settings and source config"
```

---

### Task 3: Core models and geo helpers

**Files:**
- Create: `backend/osint/core/__init__.py`, `backend/osint/core/models.py`, `backend/osint/core/geo.py`, `tests/core/__init__.py`, `tests/core/test_models_geo.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Position(track_id, kind, ts, lat, lon, source, alt_ft=None, speed_kts=None, heading_deg=None)` (frozen, kw-only)
  - `TrackInfo(track_id, kind, ident, name=None, reg=None, type_code=None, operator=None, category=None, is_mil=False, high_priority=False, extra: dict[str,str]={})`
  - `Observation(id, type, ts, lat, lon, source, value=None, version="", attrs: dict={})` (frozen)
  - `Batch(source, positions=[], tracks=[], observations=[])` with `.size() -> int`
  - `geo.H3_RESOLUTIONS = (3, 5, 7)`, `geo.h3_cells(lat, lon) -> tuple[int, int, int]`, `geo.haversine_km(lat1, lon1, lat2, lon2) -> float`, `geo.valid_coord(lat, lon) -> bool`, `geo.utcnow() -> datetime`

- [ ] **Step 1: Write the failing test `tests/core/test_models_geo.py`**

```python
from datetime import UTC, datetime

import h3

from osint.core.geo import H3_RESOLUTIONS, h3_cells, haversine_km, utcnow, valid_coord
from osint.core.models import Batch, Observation, Position, TrackInfo


def test_h3_cells_returns_one_int_per_resolution():
    cells = h3_cells(51.5, -0.12)
    assert len(cells) == 3
    assert [h3.get_resolution(h3.int_to_str(c)) for c in cells] == list(H3_RESOLUTIONS)


def test_haversine_london_paris():
    assert abs(haversine_km(51.5074, -0.1278, 48.8566, 2.3522) - 343.5) < 2


def test_valid_coord():
    assert valid_coord(0, 0)
    assert not valid_coord(91, 0)
    assert not valid_coord(0, 181)
    assert not valid_coord(float("nan"), 0)
    assert not valid_coord(None, 0)


def test_utcnow_is_aware():
    assert utcnow().tzinfo is UTC


def test_batch_size_counts_all_records():
    ts = datetime(2026, 11, 21, tzinfo=UTC)
    b = Batch(
        source="t",
        positions=[Position(track_id="air:a", kind="air", ts=ts, lat=1, lon=2, source="t")],
        tracks=[TrackInfo(track_id="air:a", kind="air", ident="a")],
        observations=[Observation(id="x", type="fire", ts=ts, lat=1, lon=2, source="t")],
    )
    assert b.size() == 3
    assert Batch(source="t").size() == 0
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `mkdir -p tests/core && : > tests/core/__init__.py && uv run pytest tests/core/test_models_geo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.core'`.

- [ ] **Step 3: Write `backend/osint/core/__init__.py`** (empty) **and `backend/osint/core/models.py`**

```python
"""Records produced by collectors and consumed by the pipeline."""

from datetime import datetime

import msgspec


class Position(msgspec.Struct, frozen=True, kw_only=True):
    track_id: str  # "air:<icao hex>" | "sea:<mmsi>"
    kind: str  # "air" | "sea"
    ts: datetime  # timezone-aware UTC
    lat: float
    lon: float
    source: str
    alt_ft: int | None = None
    speed_kts: float | None = None
    heading_deg: float | None = None


class TrackInfo(msgspec.Struct, kw_only=True):
    track_id: str
    kind: str
    ident: str
    name: str | None = None
    reg: str | None = None
    type_code: str | None = None
    operator: str | None = None
    category: str | None = None
    is_mil: bool = False
    high_priority: bool = False
    extra: dict[str, str] = msgspec.field(default_factory=dict)


class Observation(msgspec.Struct, frozen=True, kw_only=True):
    id: str  # stable id, e.g. "fire:<hash>" | "quake:<usgs id>"
    type: str  # "fire" | "quake"
    ts: datetime
    lat: float
    lon: float
    source: str
    value: float | None = None  # FRP for fires, magnitude for quakes
    version: str = ""  # changes when the upstream record is revised
    attrs: dict[str, object] = msgspec.field(default_factory=dict)


class Batch(msgspec.Struct, kw_only=True):
    source: str
    positions: list[Position] = msgspec.field(default_factory=list)
    tracks: list[TrackInfo] = msgspec.field(default_factory=list)
    observations: list[Observation] = msgspec.field(default_factory=list)

    def size(self) -> int:
        return len(self.positions) + len(self.tracks) + len(self.observations)
```

- [ ] **Step 4: Write `backend/osint/core/geo.py`**

```python
"""Geospatial and time helpers."""

import math
from datetime import UTC, datetime

import h3

H3_RESOLUTIONS = (3, 5, 7)
_EARTH_RADIUS_KM = 6371.0088


def h3_cells(lat: float, lon: float) -> tuple[int, int, int]:
    """H3 cell ids (as unsigned ints) at resolutions 3, 5 and 7."""
    return tuple(h3.str_to_int(h3.latlng_to_cell(lat, lon, r)) for r in H3_RESOLUTIONS)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def valid_coord(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def utcnow() -> datetime:
    return datetime.now(UTC)
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/core/test_models_geo.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/osint/core tests/core
git commit -m "feat: core record models and H3/geo helpers"
```

---

### Task 4: Metrics registry

**Files:**
- Create: `backend/osint/core/metrics.py`, `tests/core/test_metrics.py`

**Interfaces:**
- Produces: `Metrics()` with `inc(name, value=1.0, **labels)`, `set(name, value, **labels)`, `get(name, **labels) -> float` (0.0 if missing), `render_prometheus() -> str`

- [ ] **Step 1: Write the failing test `tests/core/test_metrics.py`**

```python
from osint.core.metrics import Metrics


def test_counters_accumulate_per_label_set():
    m = Metrics()
    m.inc("runs_total", source="a")
    m.inc("runs_total", 2, source="a")
    m.inc("runs_total", source="b")
    assert m.get("runs_total", source="a") == 3
    assert m.get("runs_total", source="b") == 1
    assert m.get("runs_total", source="zzz") == 0


def test_gauges_overwrite():
    m = Metrics()
    m.set("queue_depth", 5)
    m.set("queue_depth", 2)
    assert m.get("queue_depth") == 2


def test_prometheus_rendering_sorted_labels():
    m = Metrics()
    m.inc("runs_total", source="a", result="ok")
    m.set("queue_depth", 4)
    text = m.render_prometheus()
    assert 'runs_total{result="ok",source="a"} 1.0' in text
    assert "queue_depth 4.0" in text
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/core/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.core.metrics'`.

- [ ] **Step 3: Write `backend/osint/core/metrics.py`**

```python
"""Minimal in-process metrics with Prometheus text output."""

from collections import defaultdict

_Key = tuple[str, tuple[tuple[str, str], ...]]


def _key(name: str, labels: dict[str, object]) -> _Key:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items()))


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[_Key, float] = defaultdict(float)
        self._gauges: dict[_Key, float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: object) -> None:
        self._counters[_key(name, labels)] += value

    def set(self, name: str, value: float, **labels: object) -> None:
        self._gauges[_key(name, labels)] = float(value)

    def get(self, name: str, **labels: object) -> float:
        k = _key(name, labels)
        if k in self._gauges:
            return self._gauges[k]
        return self._counters.get(k, 0.0)

    def render_prometheus(self) -> str:
        lines = []
        for (name, labels), value in sorted({**self._counters, **self._gauges}.items()):
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{label_str}}} {float(value)}" if labels else f"{name} {float(value)}")
        return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/core/test_metrics.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/osint/core/metrics.py tests/core/test_metrics.py
git commit -m "feat: in-process metrics registry"
```

---

### Task 5: SQLite store

**Files:**
- Create: `backend/osint/store/__init__.py`, `backend/osint/store/sqlite.py`, `tests/store/__init__.py`, `tests/store/test_sqlite.py`

**Interfaces:**
- Consumes: `TrackInfo` (Task 3)
- Produces: `SqliteStore(path: Path)` with:
  - `register_source(name, domain, licence, commercial_ok) -> None`
  - `list_sources() -> list[dict]`
  - `get_cursor(name) -> dict | None` and `set_cursor(name, cursor: dict) -> None`
  - `upsert_tracks(tracks: list[TrackInfo], seen: datetime) -> None`
  - `get_track(track_id) -> dict | None`
  - `quarantine(source, record_type, reason, payload: dict) -> None` and `quarantine_count() -> int`
  - `close() -> None`

  All methods are thread-safe (internal lock).

- [ ] **Step 1: Write the failing test `tests/store/test_sqlite.py`**

```python
from datetime import UTC, datetime, timedelta

import pytest

from osint.core.models import TrackInfo
from osint.store.sqlite import SqliteStore

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_cursor_roundtrip(store):
    assert store.get_cursor("usgs") is None
    store.set_cursor("usgs", {"last": "x"})
    store.set_cursor("usgs", {"last": "y"})
    assert store.get_cursor("usgs") == {"last": "y"}


def test_upsert_tracks_merges_fields(store):
    store.upsert_tracks([TrackInfo(track_id="air:a", kind="air", ident="a", name="RCH1",
                                   extra={"desc": "C-17"})], T0)
    store.upsert_tracks([TrackInfo(track_id="air:a", kind="air", ident="a", is_mil=True,
                                   extra={"squawk": "7700"})], T0 + timedelta(minutes=5))
    t = store.get_track("air:a")
    assert t["name"] == "RCH1"
    assert t["is_mil"] == 1
    assert t["extra"] == {"desc": "C-17", "squawk": "7700"}
    assert t["first_seen"] == T0.isoformat()
    assert t["last_seen"] == (T0 + timedelta(minutes=5)).isoformat()


def test_quarantine_and_sources(store):
    store.quarantine("adsb", "position", "lat out of range", {"lat": 99})
    assert store.quarantine_count() == 1
    store.register_source("adsb", "aviation", "mixed", False)
    store.register_source("adsb", "aviation", "mixed-v2", False)
    assert [(s["name"], s["licence"], s["commercial_ok"]) for s in store.list_sources()] == [
        ("adsb", "mixed-v2", 0)
    ]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `mkdir -p tests/store && : > tests/store/__init__.py && uv run pytest tests/store/test_sqlite.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.store'`.

- [ ] **Step 3: Write `backend/osint/store/__init__.py`** (empty) **and `backend/osint/store/sqlite.py`**

```python
"""SQLite store for relational state: sources, cursors, tracks, quarantine."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from osint.core.geo import utcnow
from osint.core.models import TrackInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, domain TEXT NOT NULL, licence TEXT NOT NULL,
  commercial_ok INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_state (
  name TEXT PRIMARY KEY, cursor TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tracks (
  track_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ident TEXT NOT NULL,
  name TEXT, reg TEXT, type_code TEXT, operator TEXT, category TEXT,
  is_mil INTEGER NOT NULL DEFAULT 0, high_priority INTEGER NOT NULL DEFAULT 0,
  extra TEXT NOT NULL DEFAULT '{}', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tracks_kind_mil ON tracks(kind, is_mil);
CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, record_type TEXT NOT NULL,
  reason TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""

_UPSERT_TRACK = """
INSERT INTO tracks (track_id, kind, ident, name, reg, type_code, operator, category,
                    is_mil, high_priority, extra, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(track_id) DO UPDATE SET
  name = COALESCE(excluded.name, tracks.name),
  reg = COALESCE(excluded.reg, tracks.reg),
  type_code = COALESCE(excluded.type_code, tracks.type_code),
  operator = COALESCE(excluded.operator, tracks.operator),
  category = COALESCE(excluded.category, tracks.category),
  is_mil = MAX(tracks.is_mil, excluded.is_mil),
  high_priority = MAX(tracks.high_priority, excluded.high_priority),
  extra = json_patch(tracks.extra, excluded.extra),
  last_seen = MAX(tracks.last_seen, excluded.last_seen)
"""


class SqliteStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def register_source(self, name: str, domain: str, licence: str, commercial_ok: bool) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "domain=excluded.domain, licence=excluded.licence, "
                "commercial_ok=excluded.commercial_ok, updated_at=excluded.updated_at",
                (name, domain, licence, int(commercial_ok), utcnow().isoformat()),
            )

    def list_sources(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get_cursor(self, name: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT cursor FROM source_state WHERE name=?", (name,)).fetchone()
        return json.loads(row["cursor"]) if row else None

    def set_cursor(self, name: str, cursor: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO source_state VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "cursor=excluded.cursor, updated_at=excluded.updated_at",
                (name, json.dumps(cursor), utcnow().isoformat()),
            )

    def upsert_tracks(self, tracks: list[TrackInfo], seen: datetime) -> None:
        if not tracks:
            return
        ts = seen.isoformat()
        rows = [
            (t.track_id, t.kind, t.ident, t.name, t.reg, t.type_code, t.operator, t.category,
             int(t.is_mil), int(t.high_priority), json.dumps(t.extra), ts, ts)
            for t in tracks
        ]
        with self._lock, self._db:
            self._db.executemany(_UPSERT_TRACK, rows)

    def get_track(self, track_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tracks WHERE track_id=?", (track_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["extra"] = json.loads(out["extra"])
        return out

    def quarantine(self, source: str, record_type: str, reason: str, payload: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO quarantine (source, record_type, reason, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (source, record_type, reason, json.dumps(payload, default=str),
                 utcnow().isoformat()),
            )

    def quarantine_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/store/test_sqlite.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/osint/store tests/store
git commit -m "feat: SQLite store for sources, cursors, tracks and quarantine"
```

---

### Task 6: Parquet lake with DuckDB queries

**Files:**
- Create: `backend/osint/store/lake.py`, `tests/store/test_lake.py`

**Interfaces:**
- Consumes: `Position`, `Observation` (Task 3); `h3_cells` (Task 3)
- Produces:
  - `position_row(p: Position) -> dict` and `observation_row(o: Observation) -> dict`
  - `SCHEMAS: dict[str, pa.Schema]` with keys `"positions"` and `"observations"`
  - `Lake(root: Path)` with:
    - `write(dataset, rows: list[dict]) -> int` (number of files written)
    - `query(sql, params=None) -> list[tuple]` (views named `positions` and `observations` always exist)
    - `recent_observation_keys(since: datetime) -> set[tuple[str, str]]` ((id, version) pairs)

- [ ] **Step 1: Write the failing test `tests/store/test_lake.py`**

```python
from datetime import UTC, datetime, timedelta

from osint.core.models import Observation, Position
from osint.store.lake import Lake, observation_row, position_row

T0 = datetime(2026, 11, 21, 12, 30, tzinfo=UTC)


def _pos(ts):
    return Position(track_id="air:a", kind="air", ts=ts, lat=10, lon=20, source="t", alt_ft=1000)


def test_write_partitions_by_hour_and_is_queryable(tmp_path):
    lake = Lake(tmp_path)
    rows = [position_row(_pos(T0)), position_row(_pos(T0 + timedelta(hours=1)))]
    assert lake.write("positions", rows) == 2
    assert (tmp_path / "positions" / "date=2026-11-21" / "hour=12").is_dir()
    assert (tmp_path / "positions" / "date=2026-11-21" / "hour=13").is_dir()
    assert not list(tmp_path.rglob("*.tmp"))
    assert lake.query("SELECT count(*), max(alt_ft) FROM positions") == [(2, 1000)]


def test_empty_dataset_query_returns_zero(tmp_path):
    assert Lake(tmp_path).query("SELECT count(*) FROM observations") == [(0,)]


def test_recent_observation_keys(tmp_path):
    lake = Lake(tmp_path)
    old = Observation(id="fire:1", type="fire", ts=T0 - timedelta(days=3), lat=1, lon=1, source="t")
    new = Observation(id="quake:2", version="v9", type="quake", ts=T0, lat=1, lon=1, source="t",
                      attrs={"place": "x"})
    lake.write("observations", [observation_row(old), observation_row(new)])
    assert lake.recent_observation_keys(T0 - timedelta(days=1)) == {("quake:2", "v9")}
    assert lake.query("SELECT attrs FROM observations WHERE id='quake:2'") == [('{"place":"x"}',)]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/store/test_lake.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.store.lake'`.

- [ ] **Step 3: Write `backend/osint/store/lake.py`**

```python
"""Append-only Parquet lake partitioned by hour, queried with DuckDB."""

import os
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb
import msgspec
import pyarrow as pa
import pyarrow.parquet as pq

from osint.core.geo import h3_cells
from osint.core.models import Observation, Position

_TS = pa.timestamp("us", tz="UTC")
_H3 = [("h3_3", pa.uint64()), ("h3_5", pa.uint64()), ("h3_7", pa.uint64())]

SCHEMAS: dict[str, pa.Schema] = {
    "positions": pa.schema([
        ("track_id", pa.string()), ("kind", pa.string()), ("ts", _TS),
        ("lat", pa.float64()), ("lon", pa.float64()), ("alt_ft", pa.int32()),
        ("speed_kts", pa.float32()), ("heading_deg", pa.float32()), ("source", pa.string()),
        *_H3,
    ]),
    "observations": pa.schema([
        ("id", pa.string()), ("version", pa.string()), ("type", pa.string()), ("ts", _TS),
        ("lat", pa.float64()), ("lon", pa.float64()), ("value", pa.float64()),
        ("source", pa.string()), ("attrs", pa.string()),
        *_H3,
    ]),
}


def _h3_fields(lat: float, lon: float) -> dict:
    c3, c5, c7 = h3_cells(lat, lon)
    return {"h3_3": c3, "h3_5": c5, "h3_7": c7}


def position_row(p: Position) -> dict:
    return {
        "track_id": p.track_id, "kind": p.kind, "ts": p.ts, "lat": p.lat, "lon": p.lon,
        "alt_ft": p.alt_ft, "speed_kts": p.speed_kts, "heading_deg": p.heading_deg,
        "source": p.source, **_h3_fields(p.lat, p.lon),
    }


def observation_row(o: Observation) -> dict:
    return {
        "id": o.id, "version": o.version, "type": o.type, "ts": o.ts, "lat": o.lat,
        "lon": o.lon, "value": o.value, "source": o.source,
        "attrs": msgspec.json.encode(o.attrs).decode(), **_h3_fields(o.lat, o.lon),
    }


class Lake:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def write(self, dataset: str, rows: list[dict]) -> int:
        schema = SCHEMAS[dataset]
        groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for r in rows:
            groups[(r["ts"].strftime("%Y-%m-%d"), r["ts"].hour)].append(r)
        for (date, hour), group in groups.items():
            part_dir = self.root / dataset / f"date={date}" / f"hour={hour:02d}"
            part_dir.mkdir(parents=True, exist_ok=True)
            name = f"part-{time.time_ns()}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = part_dir / f"{name}.tmp"
            pq.write_table(pa.Table.from_pylist(group, schema=schema), tmp, compression="zstd")
            os.replace(tmp, part_dir / name)
        return len(groups)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        for dataset, schema in SCHEMAS.items():
            ds_dir = self.root / dataset
            if ds_dir.is_dir() and any(ds_dir.rglob("*.parquet")):
                pattern = (ds_dir / "**" / "*.parquet").as_posix()
                con.execute(
                    f"CREATE VIEW {dataset} AS SELECT * FROM read_parquet('{pattern}', "
                    "hive_partitioning=true, union_by_name=true)"
                )
            else:
                con.register(dataset, schema.empty_table())
        return con

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        con = self._connect()
        try:
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def recent_observation_keys(self, since: datetime) -> set[tuple[str, str]]:
        rows = self.query("SELECT id, version FROM observations WHERE ts >= ?", [since])
        return {(r[0], r[1]) for r in rows}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/store/test_lake.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/osint/store/lake.py tests/store/test_lake.py
git commit -m "feat: hour-partitioned Parquet lake with DuckDB queries"
```

---

### Task 7: Ingestor (validate, thin, dedupe, buffer, flush)

**Files:**
- Create: `backend/osint/pipeline/__init__.py`, `backend/osint/pipeline/ingest.py`, `tests/pipeline/__init__.py`, `tests/pipeline/test_ingest.py`

**Interfaces:**
- Consumes: `SqliteStore` (Task 5), `Lake`, `position_row`, `observation_row` (Task 6), `Metrics` (Task 4), models and `valid_coord`/`utcnow` (Task 3)
- Produces: `Ingestor(store, lake, metrics, *, thin_s=None, flush_interval_s=30.0, flush_rows=50_000, queue_size=64)` with:
  - `async submit(batch) -> None` (blocks when the queue is full, which is how backpressure works)
  - `process(batch) -> None` (synchronous)
  - `flush() -> None`
  - `warm_start(since) -> None`
  - `prune(older_than) -> None`
  - `async run() -> None`
  - `async stop() -> None`
  - attribute `live: dict[str, Position]`

  Default `thin_s` is `{"air": 60.0, "sea": 300.0}`.

- [ ] **Step 1: Write the failing test `tests/pipeline/test_ingest.py`**

```python
import asyncio
from datetime import timedelta

import pytest

from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.models import Batch, Observation, Position, TrackInfo
from osint.pipeline.ingest import Ingestor
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore


@pytest.fixture
def parts(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    lake = Lake(tmp_path / "lake")
    yield store, lake, Metrics()
    store.close()


def _pos(ts, lat=10.0, track="air:a", kind="air"):
    return Position(track_id=track, kind=kind, ts=ts, lat=lat, lon=20.0, source="t")


def test_positions_thinned_per_track(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    t0 = utcnow() - timedelta(minutes=10)
    ing.process(Batch(source="t", positions=[
        _pos(t0), _pos(t0 + timedelta(seconds=30)), _pos(t0 + timedelta(seconds=61), lat=11.0)]))
    ing.flush()
    assert lake.query("SELECT count(*) FROM positions") == [(2,)]
    assert ing.live["air:a"].lat == 11.0
    assert metrics.get("positions_thinned_total", source="t") == 1


def test_invalid_records_quarantined(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    ing.process(Batch(source="t", positions=[_pos(utcnow(), lat=95.0)],
                      observations=[Observation(id="x", type="fire", ts=utcnow() + timedelta(days=1),
                                                lat=1, lon=1, source="t")]))
    ing.flush()
    assert store.quarantine_count() == 2
    assert lake.query("SELECT count(*) FROM positions") == [(0,)]


def test_observations_deduped_by_id_and_version(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    ts = utcnow() - timedelta(minutes=5)
    o1 = Observation(id="quake:1", version="a", type="quake", ts=ts, lat=1, lon=1, source="t")
    o2 = Observation(id="quake:1", version="b", type="quake", ts=ts, lat=1, lon=1, source="t")
    ing.process(Batch(source="t", observations=[o1, o1, o2]))
    ing.flush()
    assert lake.query("SELECT count(*) FROM observations") == [(2,)]


def test_warm_start_skips_already_stored(parts):
    store, lake, metrics = parts
    ts = utcnow() - timedelta(minutes=5)
    o = Observation(id="fire:1", type="fire", ts=ts, lat=1, lon=1, source="t")
    first = Ingestor(store, lake, metrics)
    first.process(Batch(source="t", observations=[o]))
    first.flush()
    second = Ingestor(store, lake, metrics)
    second.warm_start(utcnow() - timedelta(hours=48))
    second.process(Batch(source="t", observations=[o]))
    second.flush()
    assert lake.query("SELECT count(*) FROM observations") == [(1,)]


def test_tracks_upserted(parts):
    store, lake, metrics = parts
    Ingestor(store, lake, metrics).process(
        Batch(source="t", tracks=[TrackInfo(track_id="sea:1", kind="sea", ident="1", name="X")]))
    assert store.get_track("sea:1")["name"] == "X"


async def test_run_and_stop_flush_everything(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=60)
    task = asyncio.create_task(ing.run())
    await ing.submit(Batch(source="t", positions=[_pos(utcnow())]))
    await asyncio.sleep(0.05)
    await ing.stop()
    assert task.done()
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `mkdir -p tests/pipeline && : > tests/pipeline/__init__.py && uv run pytest tests/pipeline/test_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.pipeline'`.

- [ ] **Step 3: Write `backend/osint/pipeline/__init__.py`** (empty) **and `backend/osint/pipeline/ingest.py`**

```python
"""Ingest stage: validate, quarantine, thin, dedupe, buffer and flush to the lake."""

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta

import msgspec

from osint.core.geo import utcnow, valid_coord
from osint.core.metrics import Metrics
from osint.core.models import Batch, Position
from osint.store.lake import Lake, observation_row, position_row
from osint.store.sqlite import SqliteStore

log = logging.getLogger(__name__)

DEFAULT_THIN_S = {"air": 60.0, "sea": 300.0}
_MAX_FUTURE = timedelta(minutes=10)


class Ingestor:
    def __init__(
        self,
        store: SqliteStore,
        lake: Lake,
        metrics: Metrics,
        *,
        thin_s: dict[str, float] | None = None,
        flush_interval_s: float = 30.0,
        flush_rows: int = 50_000,
        queue_size: int = 64,
    ) -> None:
        self.store, self.lake, self.metrics = store, lake, metrics
        self.thin_s = thin_s or DEFAULT_THIN_S
        self.flush_interval_s = flush_interval_s
        self.flush_rows = flush_rows
        self.queue: asyncio.Queue[Batch] = asyncio.Queue(maxsize=queue_size)
        self.live: dict[str, Position] = {}
        self._buffers: dict[str, list[dict]] = {"positions": [], "observations": []}
        self._last_written: dict[str, datetime] = {}
        self._seen_obs: dict[tuple[str, str], datetime] = {}
        self._last_flush = time.monotonic()
        self._task: asyncio.Task | None = None

    async def submit(self, batch: Batch) -> None:
        await self.queue.put(batch)
        self.metrics.set("ingest_queue_depth", self.queue.qsize())

    def warm_start(self, since: datetime) -> None:
        for key in self.lake.recent_observation_keys(since):
            self._seen_obs[key] = since

    def _bad(self, batch: Batch, record_type: str, record: object, reason: str) -> None:
        self.store.quarantine(batch.source, record_type, reason, msgspec.to_builtins(record))
        self.metrics.inc("records_quarantined_total", source=batch.source, type=record_type)

    def process(self, batch: Batch) -> None:
        now = utcnow()
        self.store.upsert_tracks(batch.tracks, now)
        for p in batch.positions:
            if not valid_coord(p.lat, p.lon):
                self._bad(batch, "position", p, "invalid coordinates")
                continue
            if p.ts > now + _MAX_FUTURE:
                self._bad(batch, "position", p, "timestamp in the future")
                continue
            current = self.live.get(p.track_id)
            if current is None or p.ts >= current.ts:
                self.live[p.track_id] = p
            last = self._last_written.get(p.track_id)
            if last is not None and (p.ts - last).total_seconds() < self.thin_s.get(p.kind, 0.0):
                self.metrics.inc("positions_thinned_total", source=batch.source)
                continue
            self._last_written[p.track_id] = p.ts
            self._buffers["positions"].append(position_row(p))
        for o in batch.observations:
            if not valid_coord(o.lat, o.lon):
                self._bad(batch, "observation", o, "invalid coordinates")
                continue
            if o.ts > now + _MAX_FUTURE:
                self._bad(batch, "observation", o, "timestamp in the future")
                continue
            key = (o.id, o.version)
            if key in self._seen_obs:
                self.metrics.inc("observations_duplicate_total", source=batch.source)
                continue
            self._seen_obs[key] = o.ts
            self._buffers["observations"].append(observation_row(o))
        self.metrics.inc("batches_ingested_total", source=batch.source)
        self.metrics.set("live_tracks", len(self.live))

    def _buffered(self) -> int:
        return sum(len(rows) for rows in self._buffers.values())

    def flush(self) -> None:
        for dataset, rows in self._buffers.items():
            if rows:
                self.lake.write(dataset, rows)
                self.metrics.inc("lake_rows_written_total", len(rows), dataset=dataset)
                self._buffers[dataset] = []
        self._last_flush = time.monotonic()

    def prune(self, older_than: datetime) -> None:
        self._seen_obs = {k: ts for k, ts in self._seen_obs.items() if ts >= older_than}
        self._last_written = {k: ts for k, ts in self._last_written.items() if ts >= older_than}
        self.live = {k: p for k, p in self.live.items() if p.ts >= older_than}

    async def run(self) -> None:
        self._task = asyncio.current_task()
        while True:
            remaining = self.flush_interval_s - (time.monotonic() - self._last_flush)
            try:
                batch = await asyncio.wait_for(self.queue.get(), timeout=max(remaining, 0.01))
            except TimeoutError:
                batch = None
            if batch is not None:
                await asyncio.to_thread(self.process, batch)
                self.metrics.set("ingest_queue_depth", self.queue.qsize())
            due = time.monotonic() - self._last_flush >= self.flush_interval_s
            if self._buffered() and (due or self._buffered() >= self.flush_rows):
                await asyncio.to_thread(self.flush)
            elif due:
                self._last_flush = time.monotonic()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        while not self.queue.empty():
            self.process(self.queue.get_nowait())
        self.flush()
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/pipeline/test_ingest.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/osint/pipeline tests/pipeline
git commit -m "feat: ingestor with validation, thinning, dedupe and buffered lake writes"
```

---

### Task 8: Collector base classes, registry and scheduler

**Files:**
- Create: `backend/osint/core/collector.py`, `backend/osint/core/scheduler.py`, `backend/osint/collectors/__init__.py`, `tests/core/test_scheduler.py`

**Interfaces:**
- Consumes: `Batch` (Task 3), `Metrics` (Task 4), `SqliteStore` (Task 5), `Settings` (Task 2), `utcnow` (Task 3)
- Produces:
  - `CollectorContext(http: httpx.AsyncClient, settings: Settings, store: SqliteStore)` with `get_cursor(name)` and `set_cursor(name, cursor)`
  - `BaseCollector(cfg: dict)` with ClassVars `name`, `domain`, `licence`, `commercial_ok`
  - `PollCollector` with ClassVar `default_interval_s`, property `interval_s`, and `async poll(ctx) -> Batch`
  - `StreamCollector` with `stream(ctx) -> AsyncIterator[Batch]`
  - `register(cls)` decorator, `REGISTRY: dict[str, type[BaseCollector]]`, `build_collectors(sources_cfg) -> list[BaseCollector]`
  - `SourceHealth` dataclass (`status`, `consecutive_failures`, `last_success`, `last_error`, `last_batch_size`)
  - `Scheduler(collectors, ctx, submit, metrics, *, base_backoff_s=1.0, max_backoff_s=600.0, breaker_threshold=5, breaker_cooldown_s=600.0, poll_timeout_s=120.0)` with `health: dict[str, SourceHealth]`, `async start()` and `async stop()`

- [ ] **Step 1: Write the failing test `tests/core/test_scheduler.py`**

```python
import asyncio

import pytest

from osint.core.collector import (
    REGISTRY, PollCollector, StreamCollector, build_collectors, register,
)
from osint.core.metrics import Metrics
from osint.core.models import Batch
from osint.core.scheduler import Scheduler


class OkPoll(PollCollector):
    name, domain, licence, commercial_ok = "okpoll", "test", "test", True
    default_interval_s = 0.01

    async def poll(self, ctx):
        return Batch(source=self.name)


class FailPoll(PollCollector):
    name, domain, licence, commercial_ok = "failpoll", "test", "test", True

    async def poll(self, ctx):
        raise ConnectionError("boom")


class TwoThenDie(StreamCollector):
    name, domain, licence, commercial_ok = "stream2", "test", "test", True

    async def stream(self, ctx):
        yield Batch(source=self.name)
        yield Batch(source=self.name)
        raise ConnectionError("socket closed")


async def _run(collectors, seconds=0.15, **kw):
    submitted = []

    async def submit(batch):
        submitted.append(batch)

    metrics = Metrics()
    sched = Scheduler(collectors, ctx=None, submit=submit, metrics=metrics,
                      base_backoff_s=0.001, breaker_cooldown_s=60, **kw)
    await sched.start()
    await asyncio.sleep(seconds)
    await sched.stop()
    return sched, submitted, metrics


async def test_poll_success_marks_healthy():
    sched, submitted, metrics = await _run([OkPoll({})])
    assert len(submitted) >= 3
    assert sched.health["okpoll"].status == "healthy"
    assert metrics.get("collector_runs_total", source="okpoll", result="ok") >= 3


async def test_repeated_failures_open_breaker():
    sched, submitted, metrics = await _run([FailPoll({})], breaker_threshold=3)
    h = sched.health["failpoll"]
    assert h.status == "down"
    assert h.consecutive_failures == 3  # breaker cooldown (60 s) stops further attempts
    assert "ConnectionError: boom" in h.last_error
    assert submitted == []


async def test_stream_batches_submitted_then_degraded():
    sched, submitted, _ = await _run([TwoThenDie({})], seconds=0.05, breaker_threshold=100)
    assert len(submitted) >= 2
    assert sched.health["stream2"].status == "degraded"


def test_interval_from_cfg():
    assert OkPoll({"interval_s": 5}).interval_s == 5.0
    assert OkPoll({}).interval_s == 0.01


def test_build_collectors_respects_enabled_and_unknown():
    register(OkPoll)
    try:
        assert [c.name for c in build_collectors({"okpoll": {"enabled": True}})] == ["okpoll"]
        assert build_collectors({"okpoll": {"enabled": False}}) == []
        with pytest.raises(KeyError, match="nope"):
            build_collectors({"nope": {}})
    finally:
        REGISTRY.pop("okpoll", None)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/core/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.core.collector'`.

- [ ] **Step 3: Write `backend/osint/collectors/__init__.py`**

```python
"""Collector plugins. Importing a module registers its collector."""
```

- [ ] **Step 4: Write `backend/osint/core/collector.py`**

```python
"""Collector contract, execution context and registry."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from osint.config import Settings
from osint.core.models import Batch
from osint.store.sqlite import SqliteStore


@dataclass
class CollectorContext:
    http: httpx.AsyncClient
    settings: Settings
    store: SqliteStore

    def get_cursor(self, name: str) -> dict | None:
        return self.store.get_cursor(name)

    def set_cursor(self, name: str, cursor: dict) -> None:
        self.store.set_cursor(name, cursor)


class BaseCollector(ABC):
    name: ClassVar[str]
    domain: ClassVar[str]
    licence: ClassVar[str]
    commercial_ok: ClassVar[bool]

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg


class PollCollector(BaseCollector):
    default_interval_s: ClassVar[float] = 60.0

    @property
    def interval_s(self) -> float:
        return float(self.cfg.get("interval_s", self.default_interval_s))

    @abstractmethod
    async def poll(self, ctx: CollectorContext) -> Batch: ...


class StreamCollector(BaseCollector):
    @abstractmethod
    def stream(self, ctx: CollectorContext) -> AsyncIterator[Batch]: ...


REGISTRY: dict[str, type[BaseCollector]] = {}


def register(cls: type[BaseCollector]) -> type[BaseCollector]:
    REGISTRY[cls.name] = cls
    return cls


def build_collectors(sources_cfg: dict[str, dict[str, Any]]) -> list[BaseCollector]:
    import osint.collectors  # noqa: F401  (registers built-in collectors)

    out: list[BaseCollector] = []
    for name, cfg in sources_cfg.items():
        if not cfg.get("enabled", True):
            continue
        if name not in REGISTRY:
            raise KeyError(f"unknown collector '{name}' in sources.yaml")
        out.append(REGISTRY[name](cfg))
    return out
```

- [ ] **Step 5: Write `backend/osint/core/scheduler.py`**

```python
"""Runs collectors with timeouts, exponential backoff with jitter and a circuit breaker."""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from osint.core.collector import BaseCollector, PollCollector, StreamCollector
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.models import Batch

log = logging.getLogger(__name__)


@dataclass
class SourceHealth:
    status: str = "starting"  # starting | healthy | degraded | down
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_error: str | None = None
    last_batch_size: int = 0


class Scheduler:
    def __init__(
        self,
        collectors: list[BaseCollector],
        ctx: object,
        submit: Callable[[Batch], Awaitable[None]],
        metrics: Metrics,
        *,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 600.0,
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 600.0,
        poll_timeout_s: float = 120.0,
    ) -> None:
        self.collectors = collectors
        self.ctx = ctx
        self.submit = submit
        self.metrics = metrics
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown_s = breaker_cooldown_s
        self.poll_timeout_s = poll_timeout_s
        self.health = {c.name: SourceHealth() for c in collectors}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for c in self.collectors:
            if isinstance(c, PollCollector):
                coro = self._run_poll(c)
            elif isinstance(c, StreamCollector):
                coro = self._run_stream(c)
            else:
                raise TypeError(f"{c.name} is neither a poll nor a stream collector")
            self._tasks.append(asyncio.create_task(coro, name=f"collector:{c.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    def _failure_delay(self, failures: int) -> float:
        if failures >= self.breaker_threshold:
            return self.breaker_cooldown_s
        delay = min(self.max_backoff_s, self.base_backoff_s * 2 ** (failures - 1))
        return delay * random.uniform(0.5, 1.0)

    def _ok(self, c: BaseCollector, batch: Batch, duration_s: float) -> None:
        h = self.health[c.name]
        h.status, h.consecutive_failures = "healthy", 0
        h.last_success, h.last_batch_size = utcnow(), batch.size()
        self.metrics.inc("collector_runs_total", source=c.name, result="ok")
        self.metrics.inc("records_collected_total", batch.size(), source=c.name)
        self.metrics.set("collector_last_duration_seconds", duration_s, source=c.name)

    def _fail(self, c: BaseCollector, exc: BaseException) -> float:
        h = self.health[c.name]
        h.consecutive_failures += 1
        h.status = "down" if h.consecutive_failures >= self.breaker_threshold else "degraded"
        h.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self.metrics.inc("collector_runs_total", source=c.name, result="error")
        log.warning("collector %s failed (%d in a row): %s", c.name, h.consecutive_failures,
                    h.last_error)
        return self._failure_delay(h.consecutive_failures)

    async def _run_poll(self, c: PollCollector) -> None:
        while True:
            t0 = time.monotonic()
            try:
                batch = await asyncio.wait_for(c.poll(self.ctx), timeout=self.poll_timeout_s)
                await self.submit(batch)
                self._ok(c, batch, time.monotonic() - t0)
                delay = c.interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — isolate every collector failure
                delay = self._fail(c, exc)
            await asyncio.sleep(delay)

    async def _run_stream(self, c: StreamCollector) -> None:
        while True:
            t0 = time.monotonic()
            try:
                async for batch in c.stream(self.ctx):
                    await self.submit(batch)
                    self._ok(c, batch, time.monotonic() - t0)
                    t0 = time.monotonic()
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await asyncio.sleep(self._fail(c, exc))
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/core/test_scheduler.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/osint/core/collector.py backend/osint/core/scheduler.py backend/osint/collectors tests/core/test_scheduler.py
git commit -m "feat: collector contract, registry and resilient scheduler"
```

---

### Task 9: Runtime, API and CLI

**Files:**
- Create: `backend/osint/runtime.py`, `backend/osint/api/__init__.py`, `backend/osint/api/app.py`, `backend/osint/cli.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: everything from Tasks 2–8
- Produces:
  - `Runtime(settings, collectors)` with attributes `metrics`, `store`, `lake`, `ingestor`, `scheduler`, `ctx`, and `async start()` / `async stop()`
  - `create_app(runtime) -> FastAPI` with these routes:
    - `GET /health` → `{"status": "ok", "sources": {name: health}}`
    - `GET /metrics` (Prometheus text)
    - `GET /api/sources`
    - `GET /api/live/summary` → `{"tracks": {"air": n, "sea": n}, "quarantined": n}`
  - CLI `osint run`

- [ ] **Step 1: Write the failing test `tests/test_app.py`**

```python
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from osint.api.app import create_app
from osint.config import Settings
from osint.core.collector import PollCollector
from osint.core.geo import utcnow
from osint.core.models import Batch, Observation, Position
from osint.runtime import Runtime


class FakeCollector(PollCollector):
    name, domain, licence, commercial_ok = "fake", "test", "CC0", True
    default_interval_s = 0.05

    async def poll(self, ctx):
        ts = utcnow() - timedelta(seconds=5)
        return Batch(
            source=self.name,
            positions=[Position(track_id="air:f1", kind="air", ts=ts, lat=1, lon=2, source="fake")],
            observations=[Observation(id="fire:f", type="fire", ts=ts, lat=1, lon=2, source="fake")],
        )


def test_app_runs_collectors_and_reports(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    runtime = Runtime(settings, [FakeCollector({})])
    with TestClient(create_app(runtime)) as client:
        for _ in range(100):
            if client.get("/health").json()["sources"]["fake"]["status"] == "healthy":
                break
            time.sleep(0.05)
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["sources"]["fake"]["status"] == "healthy"
        assert client.get("/api/live/summary").json() == {"tracks": {"air": 1}, "quarantined": 0}
        assert client.get("/api/sources").json()[0]["name"] == "fake"
        assert 'collector_runs_total{result="ok",source="fake"}' in client.get("/metrics").text
    # leaving the context runs shutdown, which flushes the ingestor
    assert runtime.lake.query("SELECT count(*) FROM observations") == [(1,)]
    assert runtime.lake.query("SELECT count(*) FROM positions") == [(1,)]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.api'`.

- [ ] **Step 3: Write `backend/osint/runtime.py`**

```python
"""Wires stores, ingestor, scheduler and HTTP client into one runnable unit."""

import asyncio
import contextlib
from datetime import timedelta

import httpx

from osint.config import Settings
from osint.core.collector import BaseCollector, CollectorContext
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.scheduler import Scheduler
from osint.pipeline.ingest import Ingestor
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

DEDUPE_WINDOW = timedelta(hours=48)
USER_AGENT = "osint-poc/0.1"


class Runtime:
    def __init__(self, settings: Settings, collectors: list[BaseCollector]) -> None:
        self.settings = settings
        self.collectors = collectors
        self.metrics = Metrics()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteStore(settings.sqlite_path)
        self.lake = Lake(settings.lake_dir)
        self.ingestor = Ingestor(self.store, self.lake, self.metrics)
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0
        )
        self.ctx = CollectorContext(http=self.http, settings=settings, store=self.store)
        self.scheduler = Scheduler(collectors, self.ctx, self.ingestor.submit, self.metrics)
        for c in collectors:
            self.store.register_source(c.name, c.domain, c.licence, c.commercial_ok)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await asyncio.to_thread(self.ingestor.warm_start, utcnow() - DEDUPE_WINDOW)
        self._tasks.append(asyncio.create_task(self.ingestor.run(), name="ingestor"))
        self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
        await self.scheduler.start()

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(600)
            self.ingestor.prune(utcnow() - DEDUPE_WINDOW)

    async def stop(self) -> None:
        await self.scheduler.stop()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self.ingestor.stop()  # drains the queue and flushes buffers
        await self.http.aclose()
        self.store.close()
```

- [ ] **Step 4: Write `backend/osint/api/__init__.py`** (empty) **and `backend/osint/api/app.py`**

```python
"""HTTP API: health, metrics, sources and live summary."""

from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from osint.runtime import Runtime


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        yield
        await runtime.stop()

    app = FastAPI(title="OSINT", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        sources = {}
        for name, h in runtime.scheduler.health.items():
            d = asdict(h)
            d["last_success"] = h.last_success.isoformat() if h.last_success else None
            sources[name] = d
        return {"status": "ok", "sources": sources}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return runtime.metrics.render_prometheus()

    @app.get("/api/sources")
    def sources() -> list[dict]:
        return runtime.store.list_sources()

    @app.get("/api/live/summary")
    def live_summary() -> dict:
        kinds = Counter(p.kind for p in list(runtime.ingestor.live.values()))
        return {"tracks": dict(kinds), "quarantined": runtime.store.quarantine_count()}

    return app
```

- [ ] **Step 5: Write `backend/osint/cli.py`**

```python
"""Command line entry point: `osint run`."""

import argparse
import logging

import uvicorn

from osint.api.app import create_app
from osint.config import Settings, load_sources
from osint.core.collector import build_collectors
from osint.runtime import Runtime


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="osint")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run collectors and the API server")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        settings = Settings()
        collectors = build_collectors(load_sources(settings.config_dir))
        app = create_app(Runtime(settings, collectors))
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_app.py -v`
Expected: 1 passed.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass (30 so far).

- [ ] **Step 8: Commit**

```bash
git add backend/osint/runtime.py backend/osint/api backend/osint/cli.py tests/test_app.py
git commit -m "feat: runtime wiring, health/metrics API and osint run CLI"
```

---

### Task 10: Military enrichment and global ADS-B collector

**Files:**
- Create: `backend/osint/enrich/__init__.py`, `backend/osint/enrich/mil_types.py` (copied), `backend/osint/enrich/military.py`, `backend/osint/collectors/adsb.py`, `tests/collectors/__init__.py`, `tests/collectors/test_adsb.py`
- Modify: `backend/osint/collectors/__init__.py`

**Interfaces:**
- Consumes: `PollCollector`, `register`, `CollectorContext` (Task 8); `Batch`, `Position`, `TrackInfo` (Task 3); `Settings.reference_dir` (Task 2)
- Produces:
  - `MilDb(entries: dict[str, dict[str, str]])` with `MilDb.from_csv(path)`, `get(hex)` and `__len__`
  - `parse_aircraft(ac: dict, now_s: float, mil_db: MilDb, source: str) -> tuple[Position | None, TrackInfo] | None`
  - `AdsbCollector` registered as `"adsb"`

- [ ] **Step 1: Copy the military type tables verbatim**

```bash
mkdir -p backend/osint/enrich tests/collectors
: > backend/osint/enrich/__init__.py
: > tests/collectors/__init__.py
cp legacy/mil_types.py backend/osint/enrich/mil_types.py
uv run python -c "from osint.enrich.mil_types import get_mil_category_from_type as g, is_high_priority as h; print(g('C17'), h('C17'))"
```
Expected: `(True, 'Transport/Tanker') True`

- [ ] **Step 2: Write the failing test `tests/collectors/test_adsb.py`**

```python
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.adsb import AdsbCollector, parse_aircraft
from osint.enrich.military import MilDb

NOW_MS = 1763726400000  # 2025-11-21T12:00:00Z


def test_mildb_from_csv(tmp_path):
    p = tmp_path / "db.csv"
    p.write_text(
        "$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\n"
        "AE1234,05-5140,USAF,Boeing C-17A,C17,Mil,Cargo,x,y,Big Hello,https://x\n",
        encoding="utf-8",
    )
    db = MilDb.from_csv(p)
    assert len(db) == 1
    assert db.get("ae1234")["operator"] == "USAF"
    assert MilDb.from_csv(tmp_path / "missing.csv").get("ae1234") is None


def test_parse_aircraft_enriches_from_db():
    db = MilDb({"ae1234": {"reg": "05-5140", "operator": "USAF", "type": "Boeing C-17A",
                           "icao_type": "C17", "category": "Big Hello", "tag1": "Cargo"}})
    pos, track = parse_aircraft(
        {"hex": "AE1234", "flight": "RCH123 ", "lat": 10.0, "lon": 20.0, "alt_baro": 30000,
         "gs": 450.0, "track": 90.0, "seen_pos": 2.0, "squawk": "1234"},
        NOW_MS / 1000, db, "adsb:test")
    assert pos.track_id == "air:ae1234"
    assert pos.ts == datetime.fromtimestamp(NOW_MS / 1000 - 2.0, UTC)
    assert (pos.alt_ft, pos.speed_kts, pos.heading_deg) == (30000, 450.0, 90.0)
    assert track.name == "RCH123" and track.reg == "05-5140" and track.type_code == "C17"
    assert track.operator == "USAF" and track.is_mil and track.high_priority
    assert track.extra == {"desc": "Boeing C-17A", "role": "Cargo", "squawk": "1234"}


def test_parse_aircraft_edge_cases():
    db = MilDb({})
    pos, track = parse_aircraft({"hex": "abc123", "alt_baro": "ground", "lat": 1, "lon": 1,
                                 "dbFlags": 1}, NOW_MS / 1000, db, "s")
    assert pos.alt_ft == 0 and track.is_mil and not track.high_priority
    pos, track = parse_aircraft({"hex": "abc124", "t": "F16"}, NOW_MS / 1000, db, "s")
    assert pos is None and track.is_mil
    assert parse_aircraft({"hex": "~2b0001", "lat": 1, "lon": 1}, NOW_MS / 1000, db, "s") is None


@respx.mock
async def test_poll_merges_endpoints_prefers_freshest():
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS, "ac": [{"hex": "ae1234", "lat": 10.0, "lon": 20.0, "t": "C17",
                               "seen_pos": 5.0}]}))
    respx.get("https://b.test/v2/mil").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS, "ac": [{"hex": "ae1234", "lat": 10.1, "lon": 20.1, "t": "C17",
                               "seen_pos": 1.0}]}))
    respx.get("https://c.test/v2/mil").mock(return_value=httpx.Response(503))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2", "b": "https://b.test/v2",
                                     "c": "https://c.test/v2"}})
    c._mil_db = MilDb({})
    async with httpx.AsyncClient() as http:
        batch = await c.poll(SimpleNamespace(http=http))
    assert len(batch.positions) == 1 and len(batch.tracks) == 1
    assert batch.positions[0].lat == 10.1
    assert batch.positions[0].source == "adsb:b"


@respx.mock
async def test_poll_raises_when_all_endpoints_fail():
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(500))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2"}})
    c._mil_db = MilDb({})
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all ADS-B endpoints failed"):
            await c.poll(SimpleNamespace(http=http))
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/collectors/test_adsb.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.collectors.adsb'`.

- [ ] **Step 4: Write `backend/osint/enrich/military.py`**

```python
"""plane-alert military aircraft database (ICAO hex → details)."""

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class MilDb:
    def __init__(self, entries: dict[str, dict[str, str]]) -> None:
        self._entries = entries

    @classmethod
    def from_csv(cls, path: Path) -> "MilDb":
        if not path.exists():
            log.warning("military DB %s not found; enrichment disabled", path)
            return cls({})
        entries: dict[str, dict[str, str]] = {}
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hex_code = (row.get("$ICAO") or "").strip().lower()
                if hex_code:
                    entries[hex_code] = {
                        "reg": row.get("$Registration", ""),
                        "operator": row.get("$Operator", ""),
                        "type": row.get("$Type", ""),
                        "icao_type": row.get("$ICAO Type", ""),
                        "category": row.get("Category", ""),
                        "tag1": row.get("$Tag 1", ""),
                    }
        log.info("loaded %d military aircraft from %s", len(entries), path.name)
        return cls(entries)

    def get(self, hex_code: str) -> dict[str, str] | None:
        return self._entries.get(hex_code)

    def __len__(self) -> int:
        return len(self._entries)
```

- [ ] **Step 5: Write `backend/osint/collectors/adsb.py`**

```python
"""Global military ADS-B from several free readsb-compatible aggregators (/v2/mil)."""

import asyncio
import time
from datetime import UTC, datetime

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch, Position, TrackInfo
from osint.enrich.mil_types import get_mil_category_from_type, is_high_priority
from osint.enrich.military import MilDb

DEFAULT_ENDPOINTS = {
    "adsb.lol": "https://api.adsb.lol/v2",
    "airplanes.live": "https://api.airplanes.live/v2",
    "adsb.fi": "https://opendata.adsb.fi/api/v2",
}


def parse_aircraft(
    ac: dict, now_s: float, mil_db: MilDb, source: str
) -> tuple[Position | None, TrackInfo] | None:
    hex_code = (ac.get("hex") or "").strip().lower()
    if not hex_code or hex_code.startswith("~"):  # "~" = non-ICAO (TIS-B) address
        return None
    entry = mil_db.get(hex_code) or {}
    ac_type = ac.get("t") or entry.get("icao_type") or ""
    type_is_mil, type_category = get_mil_category_from_type(ac_type)
    is_mil = bool((ac.get("dbFlags") or 0) & 1) or bool(entry) or type_is_mil
    track_id = f"air:{hex_code}"
    extra = {
        "desc": ac.get("desc") or entry.get("type") or "",
        "role": entry.get("tag1") or "",
        "squawk": ac.get("squawk") or "",
    }
    track = TrackInfo(
        track_id=track_id,
        kind="air",
        ident=hex_code,
        name=(ac.get("flight") or "").strip() or None,
        reg=ac.get("r") or entry.get("reg") or None,
        type_code=ac_type or None,
        operator=entry.get("operator") or None,
        category=entry.get("category") or type_category or None,
        is_mil=is_mil,
        high_priority=is_mil and is_high_priority(ac_type),
        extra={k: v for k, v in extra.items() if v},
    )
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None, track
    alt = ac.get("alt_baro")
    if alt == "ground":
        alt_ft = 0
    else:
        alt = alt if isinstance(alt, (int, float)) else ac.get("alt_geom")
        alt_ft = int(alt) if isinstance(alt, (int, float)) else None
    age = float(ac.get("seen_pos", ac.get("seen", 0)) or 0)
    pos = Position(
        track_id=track_id, kind="air", ts=datetime.fromtimestamp(now_s - age, UTC),
        lat=float(lat), lon=float(lon), source=source, alt_ft=alt_ft,
        speed_kts=ac.get("gs"), heading_deg=ac.get("track"),
    )
    return pos, track


@register
class AdsbCollector(PollCollector):
    name = "adsb"
    domain = "aviation"
    licence = "mixed: adsb.lol ODbL; airplanes.live and adsb.fi non-commercial"
    commercial_ok = False
    default_interval_s = 30.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._mil_db: MilDb | None = None

    async def _fetch(self, ctx: CollectorContext, base: str) -> tuple[float, list[dict]]:
        r = await ctx.http.get(f"{base}/mil", timeout=20)
        r.raise_for_status()
        data = r.json()
        return float(data.get("now") or time.time() * 1000) / 1000, data.get("ac") or []

    async def poll(self, ctx: CollectorContext) -> Batch:
        if self._mil_db is None:
            self._mil_db = MilDb.from_csv(ctx.settings.reference_dir / "plane-alert-mil.csv")
        endpoints: dict[str, str] = self.cfg.get("endpoints") or DEFAULT_ENDPOINTS
        results = await asyncio.gather(
            *(self._fetch(ctx, url) for url in endpoints.values()), return_exceptions=True
        )
        best: dict[str, tuple[float, dict, float, str]] = {}
        errors = []
        for api, res in zip(endpoints, results, strict=True):
            if isinstance(res, BaseException):
                errors.append(f"{api}: {type(res).__name__}: {res}")
                continue
            now_s, aircraft = res
            for ac in aircraft:
                hex_code = (ac.get("hex") or "").strip().lower()
                age = float(ac.get("seen_pos", ac.get("seen", 1e9)) or 0)
                if hex_code and (hex_code not in best or age < best[hex_code][0]):
                    best[hex_code] = (age, ac, now_s, api)
        if len(errors) == len(endpoints):
            raise RuntimeError("all ADS-B endpoints failed: " + "; ".join(errors))
        batch = Batch(source=self.name)
        for _, ac, now_s, api in best.values():
            parsed = parse_aircraft(ac, now_s, self._mil_db, f"adsb:{api}")
            if parsed is None:
                continue
            pos, track = parsed
            batch.tracks.append(track)
            if pos is not None:
                batch.positions.append(pos)
        return batch
```

- [ ] **Step 6: Register the module** — replace `backend/osint/collectors/__init__.py` with:

```python
"""Collector plugins. Importing a module registers its collector."""

from osint.collectors import adsb  # noqa: F401
```

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `uv run pytest tests/collectors/test_adsb.py -v`
Expected: 5 passed.

- [ ] **Step 8: Commit**

```bash
git add backend/osint/enrich backend/osint/collectors tests/collectors
git commit -m "feat: global military ADS-B collector with plane-alert enrichment"
```

---

### Task 11: Global AIS stream collector

**Files:**
- Create: `backend/osint/collectors/ais.py`, `tests/collectors/test_ais.py`
- Modify: `backend/osint/collectors/__init__.py`

**Interfaces:**
- Consumes: `StreamCollector`, `register` (Task 8); models (Task 3); `Settings.aisstream_api_key` (Task 2)
- Produces:
  - `parse_ais_time(s) -> datetime`
  - `ship_type_name(code) -> str`
  - `parse_message(msg: dict) -> tuple[Position | None, TrackInfo | None]`
  - `AisCollector` registered as `"ais"`, with attribute `_connect` (defaults to `websockets.connect`; replaceable in tests)

- [ ] **Step 1: Write the failing test `tests/collectors/test_ais.py`**

```python
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from osint.collectors.ais import AisCollector, parse_ais_time, parse_message, ship_type_name

POS_MSG = {
    "MessageType": "PositionReport",
    "MetaData": {"MMSI": 211234567, "ShipName": "EVER GIVEN  ", "latitude": 30.0,
                 "longitude": 32.5, "time_utc": "2026-11-21 10:00:05.917014384 +0000 UTC"},
    "Message": {"PositionReport": {"Latitude": 30.01, "Longitude": 32.51, "Sog": 12.3,
                                   "Cog": 181.0, "TrueHeading": 511}},
}
STATIC_MSG = {
    "MessageType": "ShipStaticData",
    "MetaData": {"MMSI": 211234567, "ShipName": "EVER GIVEN"},
    "Message": {"ShipStaticData": {"Name": "EVER GIVEN", "Type": 35, "CallSign": "H3RC ",
                                   "Destination": "ROTTERDAM", "ImoNumber": 9811000}},
}


def test_parse_ais_time():
    assert parse_ais_time("2026-11-21 10:00:05.917014384 +0000 UTC") == datetime(
        2026, 11, 21, 10, 0, 5, tzinfo=UTC)


def test_ship_type_name():
    assert ship_type_name(35) == "Military Ops"
    assert ship_type_name(74) == "Cargo"
    assert ship_type_name(None) == "Unknown"
    assert ship_type_name(99) == "Other"


def test_parse_position_report():
    pos, track = parse_message(POS_MSG)
    assert track is None
    assert pos.track_id == "sea:211234567" and pos.kind == "sea"
    assert (pos.lat, pos.lon, pos.speed_kts) == (30.01, 32.51, 12.3)
    assert pos.heading_deg == 181.0  # TrueHeading 511 = unavailable → falls back to COG


def test_parse_static_data():
    pos, track = parse_message(STATIC_MSG)
    assert pos is None
    assert track.name == "EVER GIVEN" and track.is_mil and track.category == "Military Ops"
    assert track.extra == {"callsign": "H3RC", "destination": "ROTTERDAM", "imo": "9811000"}


def test_parse_ignores_unknown_and_missing_mmsi():
    assert parse_message({"MessageType": "Other", "MetaData": {"MMSI": 1}}) == (None, None)
    assert parse_message({"MessageType": "PositionReport", "MetaData": {}}) == (None, None)


class FakeWS:
    def __init__(self, msgs):
        self.msgs, self.sent = list(msgs), []

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        if self.msgs:
            return self.msgs.pop(0)
        await asyncio.sleep(3600)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_stream_batches_messages_and_subscribes_globally():
    ws = FakeWS([json.dumps(POS_MSG), json.dumps(STATIC_MSG)])
    c = AisCollector({"batch_seconds": 0.05})
    c._connect = lambda *a, **k: ws
    gen = c.stream(SimpleNamespace(settings=SimpleNamespace(aisstream_api_key="k")))
    batch = await asyncio.wait_for(gen.__anext__(), timeout=2)
    await gen.aclose()
    assert len(batch.positions) == 1 and len(batch.tracks) == 1
    sub = json.loads(ws.sent[0])
    assert sub["APIKey"] == "k"
    assert sub["BoundingBoxes"] == [[[-90, -180], [90, 180]]]


async def test_stream_requires_key():
    gen = AisCollector({}).stream(SimpleNamespace(settings=SimpleNamespace(aisstream_api_key="")))
    with pytest.raises(RuntimeError, match="AISSTREAM_API_KEY"):
        await gen.__anext__()


async def test_stream_raises_on_server_error():
    c = AisCollector({"batch_seconds": 0.05})
    c._connect = lambda *a, **k: FakeWS([json.dumps({"error": "Api Key Is Not Valid"})])
    gen = c.stream(SimpleNamespace(settings=SimpleNamespace(aisstream_api_key="k")))
    with pytest.raises(RuntimeError, match="Api Key Is Not Valid"):
        await gen.__anext__()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/collectors/test_ais.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.collectors.ais'`.

- [ ] **Step 3: Write `backend/osint/collectors/ais.py`**

```python
"""Global AIS positions and static data from the AISStream websocket."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import websockets

from osint.core.collector import CollectorContext, StreamCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Position, TrackInfo

AIS_URL = "wss://stream.aisstream.io/v0/stream"
GLOBAL_BBOX = [[[-90, -180], [90, 180]]]

SHIP_TYPES = {
    0: "Not Available", 30: "Fishing", 31: "Towing", 32: "Towing (Large)", 33: "Dredging",
    34: "Diving Ops", 35: "Military Ops", 36: "Sailing", 37: "Pleasure Craft",
    40: "High Speed Craft", 50: "Pilot Vessel", 51: "Search & Rescue", 52: "Tug",
    53: "Port Tender", 55: "Law Enforcement", 58: "Medical Transport", 60: "Passenger",
    70: "Cargo", 80: "Tanker", 90: "Other",
}


def ship_type_name(code: int | None) -> str:
    if code is None:
        return "Unknown"
    code = int(code)
    return SHIP_TYPES.get(code) or SHIP_TYPES.get(code // 10 * 10) or f"Type {code}"


def parse_ais_time(value: str) -> datetime:
    """AISStream format: '2026-11-21 10:00:05.917014384 +0000 UTC'."""
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def parse_message(msg: dict) -> tuple[Position | None, TrackInfo | None]:
    meta = msg.get("MetaData") or {}
    mmsi = str(meta.get("MMSI") or "").strip()
    if not mmsi:
        return None, None
    track_id = f"sea:{mmsi}"
    mtype = msg.get("MessageType")
    body = (msg.get("Message") or {}).get(mtype) or {}
    if mtype == "PositionReport":
        heading = body.get("TrueHeading")
        if heading is None or heading == 511:  # 511 = not available
            heading = body.get("Cog")
        sog = body.get("Sog")
        ts = parse_ais_time(meta["time_utc"]) if meta.get("time_utc") else utcnow()
        return Position(
            track_id=track_id, kind="sea", ts=ts,
            lat=body.get("Latitude", meta.get("latitude")),
            lon=body.get("Longitude", meta.get("longitude")),
            source="aisstream",
            speed_kts=sog if sog is not None and sog < 102.2 else None,  # 102.3 = n/a
            heading_deg=heading,
        ), None
    if mtype == "ShipStaticData":
        code = body.get("Type")
        extra = {
            "callsign": (body.get("CallSign") or "").strip(),
            "destination": (body.get("Destination") or "").strip(),
            "imo": str(body.get("ImoNumber") or "") if body.get("ImoNumber") else "",
        }
        return None, TrackInfo(
            track_id=track_id, kind="sea", ident=mmsi,
            name=(body.get("Name") or meta.get("ShipName") or "").strip() or None,
            type_code=str(code) if code is not None else None,
            category=ship_type_name(code),
            is_mil=code == 35,
            extra={k: v for k, v in extra.items() if v},
        )
    return None, None


@register
class AisCollector(StreamCollector):
    name = "ais"
    domain = "maritime"
    licence = "AISStream terms (verify before commercial use)"
    commercial_ok = False

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._connect = websockets.connect

    async def stream(self, ctx: CollectorContext) -> AsyncIterator[Batch]:
        key = ctx.settings.aisstream_api_key
        if not key:
            raise RuntimeError("AISSTREAM_API_KEY not set")
        subscribe = {
            "APIKey": key,
            "BoundingBoxes": self.cfg.get("bboxes") or GLOBAL_BBOX,
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        }
        batch_s = float(self.cfg.get("batch_seconds", 5))
        batch_max = int(self.cfg.get("batch_max", 5000))
        async with self._connect(AIS_URL, open_timeout=30, ping_interval=20,
                                 max_size=2**20) as ws:
            await ws.send(json.dumps(subscribe))
            batch, count, deadline = Batch(source=self.name), 0, time.monotonic() + batch_s
            while True:
                remaining = deadline - time.monotonic()
                raw = None
                if remaining > 0:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except TimeoutError:
                        raw = None
                if raw is not None:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if "error" in msg:
                        raise RuntimeError(f"AISStream error: {msg['error']}")
                    pos, track = parse_message(msg)
                    if pos is not None:
                        batch.positions.append(pos)
                    if track is not None:
                        batch.tracks.append(track)
                    count += 1
                if time.monotonic() >= deadline or count >= batch_max:
                    if batch.size():
                        yield batch
                    batch, count, deadline = Batch(source=self.name), 0, time.monotonic() + batch_s
```

- [ ] **Step 4: Register the module** — in `backend/osint/collectors/__init__.py` replace the import line with:

```python
from osint.collectors import adsb, ais  # noqa: F401
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/collectors/test_ais.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/osint/collectors tests/collectors/test_ais.py
git commit -m "feat: global AISStream collector with batched websocket ingestion"
```

---

### Task 12: Global NASA FIRMS collector

**Files:**
- Create: `backend/osint/collectors/firms.py`, `tests/collectors/test_firms.py`
- Modify: `backend/osint/collectors/__init__.py`

**Interfaces:**
- Consumes: `PollCollector`, `register` (Task 8); `Observation`, `Batch` (Task 3); `Settings.firms_map_key` (Task 2)
- Produces:
  - `confidence_label(conf) -> str`
  - `parse_row(row: dict, product: str) -> Observation | None`
  - `FirmsCollector` registered as `"firms"`

- [ ] **Step 1: Write the failing test `tests/collectors/test_firms.py`**

```python
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.firms import FirmsCollector, confidence_label, parse_row

HEADER = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
          "confidence,version,bright_ti5,frp,daynight\n")
ROW1 = "29.1,47.9,330.5,0.4,0.4,2026-11-21,0105,N20,VIIRS,h,2.0NRT,290.1,12.5,N\n"
ROW2 = "-3.2,120.5,310.0,0.4,0.4,2026-11-21,0510,N20,VIIRS,n,2.0NRT,285.0,,D\n"


def _ctx(http, key="k"):
    return SimpleNamespace(http=http, settings=SimpleNamespace(firms_map_key=key))


def test_confidence_label():
    assert confidence_label("h") == "High"
    assert confidence_label("85") == "High (85%)"
    assert confidence_label("10") == "Low (10%)"
    assert confidence_label("") == ""


def test_parse_row():
    import csv
    import io
    row = next(csv.DictReader(io.StringIO(HEADER + ROW1)))
    o = parse_row(row, "VIIRS_NOAA20_NRT")
    assert o.type == "fire" and o.ts == datetime(2026, 11, 21, 1, 5, tzinfo=UTC)
    assert (o.lat, o.lon, o.value) == (29.1, 47.9, 12.5)
    assert o.attrs["confidence"] == "High" and o.attrs["brightness_k"] == 330.5
    assert o.id.startswith("fire:") and o.id == parse_row(row, "VIIRS_NOAA20_NRT").id
    assert o.id != parse_row(row, "VIIRS_SNPP_NRT").id


@respx.mock
async def test_poll_tolerates_one_failing_product():
    base = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/k"
    respx.get(f"{base}/P1/world/1").mock(return_value=httpx.Response(200, text=HEADER + ROW1 + ROW2))
    respx.get(f"{base}/P2/world/1").mock(return_value=httpx.Response(200, text="Invalid MAP_KEY."))
    c = FirmsCollector({"products": ["P1", "P2"]})
    async with httpx.AsyncClient() as http:
        batch = await c.poll(_ctx(http))
    assert len(batch.observations) == 2
    assert batch.observations[1].value is None  # empty frp


@respx.mock
async def test_poll_raises_when_all_fail():
    respx.get(url__startswith="https://firms.modaps.eosdis.nasa.gov/").mock(
        return_value=httpx.Response(200, text="Invalid MAP_KEY."))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all FIRMS products failed"):
            await FirmsCollector({"products": ["P1"]}).poll(_ctx(http))


async def test_poll_requires_key():
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="FIRMS_MAP_KEY"):
            await FirmsCollector({}).poll(_ctx(http, key=""))
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/collectors/test_firms.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.collectors.firms'`.

- [ ] **Step 3: Write `backend/osint/collectors/firms.py`**

```python
"""NASA FIRMS active fire detections, worldwide (VIIRS + MODIS NRT)."""

import asyncio
import csv
import hashlib
import io
from datetime import UTC, datetime

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch, Observation

BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
DEFAULT_PRODUCTS = ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT"]


def confidence_label(conf: str) -> str:
    mapping = {"l": "Low", "n": "Nominal", "h": "High"}
    if conf in mapping:
        return mapping[conf]
    try:
        val = int(conf)
    except (TypeError, ValueError):
        return str(conf or "")
    if val >= 80:
        return f"High ({val}%)"
    if val >= 30:
        return f"Nominal ({val}%)"
    return f"Low ({val}%)"


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_row(row: dict, product: str) -> Observation | None:
    lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
    if lat is None or lon is None or not row.get("acq_date"):
        return None
    hhmm = str(row.get("acq_time", "0")).zfill(4)
    ts = datetime.strptime(f"{row['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=UTC)
    raw_id = f"{product}|{row['latitude']}|{row['longitude']}|{row['acq_date']}|{hhmm}"
    return Observation(
        id="fire:" + hashlib.sha1(raw_id.encode()).hexdigest()[:16],
        type="fire", ts=ts, lat=lat, lon=lon, source=f"firms:{product}",
        value=_float(row.get("frp")),
        attrs={
            "brightness_k": _float(row.get("bright_ti4") or row.get("brightness")),
            "confidence": confidence_label(row.get("confidence", "")),
            "satellite": row.get("satellite", ""),
            "instrument": row.get("instrument", ""),
            "daynight": row.get("daynight", ""),
        },
    )


@register
class FirmsCollector(PollCollector):
    name = "firms"
    domain = "fires"
    licence = "NASA open data (attribution: NASA FIRMS)"
    commercial_ok = True
    default_interval_s = 900.0

    async def _fetch(self, ctx: CollectorContext, key: str, product: str) -> list[Observation]:
        area = self.cfg.get("area", "world")
        days = int(self.cfg.get("days", 1))
        r = await ctx.http.get(f"{BASE_URL}/{key}/{product}/{area}/{days}", timeout=120)
        r.raise_for_status()
        text = r.text
        if not text.startswith("latitude"):
            raise RuntimeError(f"FIRMS {product}: {text[:200].strip()}")
        rows = csv.DictReader(io.StringIO(text))
        return [o for o in (parse_row(row, product) for row in rows) if o is not None]

    async def poll(self, ctx: CollectorContext) -> Batch:
        key = ctx.settings.firms_map_key
        if not key:
            raise RuntimeError("FIRMS_MAP_KEY not set")
        products = self.cfg.get("products") or DEFAULT_PRODUCTS
        results = await asyncio.gather(
            *(self._fetch(ctx, key, p) for p in products), return_exceptions=True
        )
        errors = [f"{p}: {r}" for p, r in zip(products, results, strict=True)
                  if isinstance(r, BaseException)]
        if len(errors) == len(products):
            raise RuntimeError("all FIRMS products failed: " + "; ".join(errors))
        batch = Batch(source=self.name)
        for r in results:
            if not isinstance(r, BaseException):
                batch.observations.extend(r)
        return batch
```

- [ ] **Step 4: Register the module** — in `backend/osint/collectors/__init__.py` replace the import line with:

```python
from osint.collectors import adsb, ais, firms  # noqa: F401
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/collectors/test_firms.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/osint/collectors tests/collectors/test_firms.py
git commit -m "feat: worldwide NASA FIRMS fire collector"
```

---

### Task 13: Global USGS seismic collector

**Files:**
- Create: `backend/osint/collectors/usgs.py`, `tests/collectors/test_usgs.py`
- Modify: `backend/osint/collectors/__init__.py`

**Interfaces:**
- Consumes: `PollCollector`, `register`, `CollectorContext` (Task 8); `SqliteStore` (Task 5); models (Task 3)
- Produces:
  - `parse_feature(f: dict) -> Observation | None`
  - `UsgsCollector` registered as `"usgs"`. Its first poll (no cursor) reads `all_day`; later polls read `all_hour`. Observation `version` = USGS `updated` timestamp, so revisions are stored.

- [ ] **Step 1: Write the failing test `tests/collectors/test_usgs.py`**

```python
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import respx

from osint.collectors.usgs import FEED_URL, UsgsCollector, parse_feature
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

FEATURE = {
    "id": "us7000abcd",
    "properties": {"mag": 5.4, "place": "10 km S of Bandar Abbas, Iran", "time": 1763726400000,
                   "updated": 1763726500000, "felt": 3, "tsunami": 0, "type": "earthquake",
                   "magType": "mww", "url": "https://earthquake.usgs.gov/x"},
    "geometry": {"type": "Point", "coordinates": [56.27, 27.1, 10.0]},
}


def test_parse_feature():
    o = parse_feature(FEATURE)
    assert o.id == "quake:us7000abcd" and o.version == "1763726500000"
    assert o.ts == datetime(2025, 11, 21, 12, 0, tzinfo=UTC)
    assert (o.lat, o.lon, o.value) == (27.1, 56.27, 5.4)
    assert o.attrs["depth_km"] == 10.0 and o.attrs["event_type"] == "earthquake"
    assert parse_feature({"id": "x", "properties": {}, "geometry": None}) is None


@respx.mock
async def test_first_poll_backfills_day_then_hour(tmp_path):
    day = respx.get(FEED_URL.format(window="all_day")).mock(
        return_value=httpx.Response(200, json={"features": [FEATURE]}))
    hour = respx.get(FEED_URL.format(window="all_hour")).mock(
        return_value=httpx.Response(200, json={"features": []}))
    store = SqliteStore(tmp_path / "s.sqlite")
    async with httpx.AsyncClient() as http:
        ctx = CollectorContext(http=http, settings=SimpleNamespace(), store=store)
        c = UsgsCollector({})
        first = await c.poll(ctx)
        second = await c.poll(ctx)
    store.close()
    assert len(first.observations) == 1 and second.observations == []
    assert day.call_count == 1 and hour.call_count == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/collectors/test_usgs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.collectors.usgs'`.

- [ ] **Step 3: Write `backend/osint/collectors/usgs.py`**

```python
"""USGS real-time earthquake (and explosion/quarry blast) feed, worldwide."""

from datetime import UTC, datetime

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Observation

FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{window}.geojson"


def parse_feature(f: dict) -> Observation | None:
    props = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2 or props.get("time") is None or not f.get("id"):
        return None
    return Observation(
        id=f"quake:{f['id']}",
        version=str(props.get("updated") or ""),
        type="quake",
        ts=datetime.fromtimestamp(props["time"] / 1000, UTC),
        lat=float(coords[1]),
        lon=float(coords[0]),
        source="usgs",
        value=props.get("mag"),
        attrs={
            "depth_km": coords[2] if len(coords) > 2 else None,
            "place": props.get("place"),
            "event_type": (props.get("type") or "earthquake").lower(),
            "mag_type": props.get("magType"),
            "felt": props.get("felt") or 0,
            "tsunami": bool(props.get("tsunami")),
            "url": props.get("url"),
        },
    )


@register
class UsgsCollector(PollCollector):
    name = "usgs"
    domain = "seismic"
    licence = "USGS public domain"
    commercial_ok = True
    default_interval_s = 120.0

    async def poll(self, ctx: CollectorContext) -> Batch:
        cursor = ctx.get_cursor(self.name)
        window = "all_hour" if cursor else self.cfg.get("backfill_window", "all_day")
        r = await ctx.http.get(FEED_URL.format(window=window), timeout=30)
        r.raise_for_status()
        features = r.json().get("features") or []
        batch = Batch(source=self.name)
        batch.observations.extend(o for o in map(parse_feature, features) if o is not None)
        ctx.set_cursor(self.name, {"last_poll": utcnow().isoformat(), "window": window})
        return batch
```

- [ ] **Step 4: Register the module** — in `backend/osint/collectors/__init__.py` replace the import line with:

```python
from osint.collectors import adsb, ais, firms, usgs  # noqa: F401
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/collectors/test_usgs.py -v`
Expected: 2 passed.

- [ ] **Step 6: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check backend tests`
Expected: all tests pass; ruff prints `All checks passed!`. If ruff reports issues, fix them and re-run before committing.

- [ ] **Step 7: Commit**

```bash
git add backend/osint/collectors tests/collectors/test_usgs.py
git commit -m "feat: worldwide USGS seismic collector"
```

---

### Task 14: README and live smoke run

**Files:**
- Create: `README.md`
- Uses: `.env` (created locally by the user; never committed)

**Interfaces:**
- Consumes: the full system
- Produces: verified running M0

- [ ] **Step 1: Write `README.md`**

````markdown
# OSINT — Global Event Intelligence (POC)

Collects global military ADS-B, AIS, NASA FIRMS fires and USGS seismic data into a local
SQLite + Parquet store. See `docs/superpowers/specs/` for the design.

## Setup

```powershell
uv sync
copy .env.example .env   # then fill in FIRMS_MAP_KEY and AISSTREAM_API_KEY
```

## Run

```powershell
uv run osint run
```

- Health: http://127.0.0.1:8765/health
- Live summary: http://127.0.0.1:8765/api/live/summary
- Metrics (Prometheus text): http://127.0.0.1:8765/metrics

Collectors are configured in `config/sources.yaml`. Data lives in `data/`
(`osint.sqlite` and `lake/<dataset>/date=…/hour=…/*.parquet`).

## Test

```powershell
uv run pytest -q
uv run ruff check backend tests
```

## Data licences

Each collector records its licence in the `sources` table (`GET /api/sources`).
Sources with `commercial_ok = 0` must be replaced before any commercial use.
````

- [ ] **Step 2: Check `.env` exists with keys (user-provided)**

Run: `uv run python -c "from osint.config import Settings as S; s=S(); print(bool(s.firms_map_key), bool(s.aisstream_api_key))"`
Expected: `True True`. If either is `False`, stop and ask the user to fill in `.env` from `.env.example`.

- [ ] **Step 3: Start the server in the background**

Run (background): `uv run osint run`
Expected log lines within ~10 s: `Uvicorn running on http://127.0.0.1:8765` and `loaded N military aircraft from plane-alert-mil.csv`.

- [ ] **Step 4: Verify health after ~2 minutes**

Run: `curl -s http://127.0.0.1:8765/health`
Expected: `adsb`, `firms` and `usgs` show `"status":"healthy"`. `ais` shows `healthy` once its first 5 s batch arrives. Any `degraded` or `down` source has a `last_error` explaining why: fix the cause (bad key, endpoint change) before continuing.

Run: `curl -s http://127.0.0.1:8765/api/live/summary`
Expected: `{"tracks":{"air":<hundreds+>,"sea":<thousands+>},"quarantined":<small>}`.

- [ ] **Step 5: Stop the server (Ctrl+C / stop the background task), then verify the lake**

```bash
uv run python -c "from osint.store.lake import Lake; from osint.config import Settings; l=Lake(Settings().lake_dir); print(l.query('SELECT kind, count(*) FROM positions GROUP BY kind')); print(l.query('SELECT type, count(*) FROM observations GROUP BY type'))"
```
Expected: non-zero counts for `air`, `sea`, `fire` and `quake`.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, run and test instructions"
```

---

## Done criteria for M0

- `uv run pytest -q` is green and `uv run ruff check backend tests` is clean.
- `uv run osint run` collects global military aircraft, ships, fires and quakes. `/health` shows every source as healthy, and Parquet partitions fill under `data/lake/`.
- No secrets in tracked files (`git grep -nE '[0-9a-f]{32,}'` returns nothing).
