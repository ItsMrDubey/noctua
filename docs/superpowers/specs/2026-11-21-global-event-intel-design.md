# Global Event Intelligence Platform — POC Design

**Date:** 2026-11-21
**Status:** Draft for review
**Goal:** A proof of concept for a global event-intelligence platform that is measurably better and cheaper to run than GDELT 2.0, and is built for later commercialization. It covers military, aviation, maritime, news, satellite imagery and other domains, shown on a 3D globe.

---

## 1. Vision & POC success criteria

GDELT 2.0 reads world news every 15 minutes and codes it into events. Its known weaknesses:

1. **Duplicates.** One incident becomes dozens of rows, one per article.
2. **Bad geolocation.** Events land on country centres or on the wrong place with the same name.
3. **No physical confirmation.** It only knows what articles say.
4. **Weak provenance.** There are no confidence scores and fields aren't traceable to a source.
5. **Hard to consume.** Output is flat files with no live API.

The POC must show all four of the following:

| Pillar | Proof |
|---|---|
| Sensor-verified events | Events grouped from news, backed by FIRMS, ADS-B, AIS, seismic and satellite imagery, each with a status level and a confidence score |
| Beats GDELT on metrics | A live scorecard on the same time window: delay, location precision, duplicate rate, coverage, share of events with sensor backing, cost per verified event |
| Analyst product | A 3D globe with time playback, per-field sourcing, event chains, alerts and a digest |
| Domain depth | Military, aviation and maritime patterns (flight patterns, AIS dark periods, aircraft gathering, dark ships) that create events by themselves |

**Constraints:**
- Global coverage.
- Runs locally on one Windows PC, polling only (no inbound webhooks).
- AlterLab spend capped at **$50/month** (current balance ≈ $1,137).
- Free imagery only (no Google Maps key).
- Every source records its licence so non-commercial sources can be swapped out before launch.

## 2. Architecture

**Approach:** rebuild the existing `osint/` scripts as a Python package (FastAPI plus async collector plugins) with a Vite/TypeScript CesiumJS front end. Existing logic (military aircraft type lists, plane-alert database, bases list, FIRMS/ADS-B/AIS/USGS parsing, correlation rules) is moved over, not discarded.

```
osint/
├─ pyproject.toml · .env (git-ignored) · .env.example
├─ config/            regions.yaml · sources.yaml · alerts.yaml · taxonomy.yaml (+ CAMEO mapping)
├─ backend/osint/
│  ├─ config.py       settings from .env + YAML (pydantic-settings)
│  ├─ core/           data models (msgspec) · Collector protocol · registry · scheduler · bounded queues
│  ├─ store/          Store interface → sqlite.py (events and relational data) + lake.py (Parquet/DuckDB)
│  ├─ collectors/     one file per source (see §4)
│  ├─ pipeline/       triage · cluster · extract · geolocate · corroborate · link · score
│  ├─ enrich/         gazetteer (GeoNames, offline) · military classification · base lookup · H3 helpers
│  ├─ imagery/        GIBS layer catalogue · STAC search (Sentinel-2/Landsat/Sentinel-1) · image crops · before/after
│  ├─ alerts/         rule engine · websocket push · Windows toast · daily digest
│  ├─ budget/         AlterLab spend log, per-tag caps, polling slow-down
│  ├─ benchmark/      GDELT comparison and scorecard
│  └─ api/            FastAPI REST + /ws/live websocket + /kml/master.kml + /metrics
├─ frontend/          Vite + TypeScript + CesiumJS
├─ tests/             unit tests · recorded responses · labelled test set · replay
├─ data/              lake/ (Parquet) · osint.sqlite · gazetteer/ · imagery cache (git-ignored)
├─ digests/           daily briefs (git-ignored)
└─ legacy/            existing scripts, kept for reference until ported, then deleted
```

A single process (`osint run`) starts uvicorn, the scheduler and the pipeline workers.

### Efficiency principles

1. **Cheapest filter first.** The order is: exact hash → MinHash near-duplicate → language detection → gazetteer → small classifier → multilingual embedding → paid extraction. Each stage reports how much passes through and what it costs.
2. **Never repeat work.** Every stage is safe to re-run and keyed by a content hash. Extraction runs again only when an event changes meaningfully.
3. **H3 hexagon grid everywhere.** Every geo record carries H3 cells at resolutions 3, 5 and 7. Matching events to sensor data is a lookup of a cell and its neighbours within a time bucket, so no distance scans are needed. The same cells drive clustering and heatmaps at each zoom level.
4. **Storage split by use.** Live state is in memory. SQLite (WAL mode) holds events, links, storylines, sources and alerts. Parquet files split by hour and queried with DuckDB hold positions, observations, documents and GDELT rows. The path to production is PostgreSQL/PostGIS plus ClickHouse.
5. **One process with backpressure.** Async workers are connected by bounded queues and process in batches. There is no Redis or Kafka in the POC, and stage boundaries match a future message bus.
6. **Cross-language grouping on the local CPU.** multilingual-e5-small runs via ONNX Runtime.
7. **Fast globe.** The websocket carries compact binary (msgpack) updates, only for what changed and what's in view. The front end draws with Cesium's low-level point and billboard collections, with H3 level-of-detail clustering on the server.
8. **Measured, not guessed.** `/metrics` and the ops panel. Slow paths are rewritten in Rust only when profiling shows the need.

**Libraries:** FastAPI, uvicorn, httpx, msgspec, orjson, polars, duckdb, pyarrow, h3, datasketch (MinHash), onnxruntime, fasttext-langdetect, rasterio, pystac-client, win11toast, msgpack.

## 3. Data model

| Record | Key fields |
|---|---|
| **Source** | id, kind (news/rss/gdelt/official/sensor/imagery), url, language, country, licence, commercial_ok, reliability (learned) |
| **Document** | id, source_id, url, title, snippet (≤300 characters), language, published_at, fetched_at, content_hash, minhash, embedding_ref, relevance_score, state. **No full article text is stored.** |
| **Event** | id (ULID), category, subcategory, cameo_code, actors[{name, role, type}], geo {lat, lon, precision: point/city/admin1/country, gazetteer_id, confidence, h3[]}, t_start, t_end, status, confidence, version, mention_count, source_diversity, gdelt_ids[], first_seen, updated_at |
| **EventField** | event_id, field, value, source_doc_id, quote: the per-field source record |
| **Track / Position** | Track: an aircraft (ICAO hex) or ship (MMSI) with its static details. Positions: lat/lon/alt/speed/heading/time/H3, thinned to 1 per 5 min for ships and 1 per 60 s for aircraft |
| **Observation** | A fire hotspot, earthquake, imagery change or night-light drop: type, geo, time, measurements, source |
| **ImageryAsset** | event_id, collection (S2/Landsat/S1/GIBS), scene_id, acquired_at, cloud_pct, role (before/after), crop path, bounds |
| **Corroboration** | event_id, observation_or_track_id, rule, distance_km, dt_minutes, score |
| **EventLink** | from_event, to_event, type (causes/retaliation_for/escalates/responds_to/consequence_of/same_storyline), method (textual/sensor/heuristic), evidence, confidence |
| **Storyline** | id, title, event_ids, footprint (H3 cells), growth rate, updated_at |
| **Alert** | rule_id, event_id, severity, channels, delivered_at, suppressed_reason |

**Event status levels:** `reported` (1 outlet) → `multi-source` (≥2 independent outlets) → `sensor-corroborated` → `official` (confirmed by a watched official source).

**Taxonomy:** our own categories (armed conflict, air activity, maritime security, civil unrest, disaster, infrastructure, political/diplomatic, economic, cyber/outage), mapped to CAMEO root codes for comparison with GDELT.

## 4. Sources (collectors)

| Domain | Collector | Source | Licence notes |
|---|---|---|---|
| Aviation (military) | aviation_adsb | airplanes.live / adsb.lol global `/mil`, plus plane-alert database | adsb.lol is ODbL; airplanes.live is non-commercial, so it's POC only |
| Maritime | maritime_ais | AISStream global websocket | Check terms before commercial use |
| Fires | fires_firms | NASA FIRMS, global, VIIRS + MODIS | NASA open data |
| Seismic | seismic_usgs | USGS GeoJSON feeds | Public domain |
| News | news_alterlab | AlterLab news, every 15 min | Free; "News via AlterLab" attribution required |
| News | rss | Curated RSS list, multilingual | Store metadata and links only |
| Events | gdelt | GDELT 2.0 events + GKG, 15-minute exports | Open with attribution |
| Official | watch_official | UKMTO/JMIC, EASA conflict-zone bulletins, defence ministry and navy press pages (via AlterLab) | Public statements |
| Official (APIs) | nga_msi, faa_notam, reliefweb, rss_gov | NGA maritime warnings, FAA NOTAM API, ReliefWeb, defense.gov/CENTCOM feeds | Free APIs, fetched directly with no AlterLab cost |
| Facilities | bases | Existing curated list, extended worldwide | Our own data |
| Imagery | imagery_gibs | NASA GIBS WMTS: MODIS/VIIRS true colour, VIIRS night lights | Free, including commercial use |
| Imagery | imagery_stac | Sentinel-2 L2A and Landsat 8/9 via Earth Search STAC (AWS open data) | Copernicus is free including commercial; Landsat is public domain |
| Imagery (stretch) | imagery_sar | Sentinel-1 GRD via STAC: ship detection for dark-ship checks | Copernicus, free |

**Rule:** use a free official API or RSS feed directly whenever one exists. AlterLab is only for pages with no feed or that block bots.

## 5. Pipeline

```
ingest → normalise → triage → cluster → extract → geolocate → corroborate → link → score → publish → alert
```

1. **Ingest:** AlterLab news, RSS, GDELT, official sources. Each source saves its position so restarts resume cleanly.
2. **Triage (free):** hash and MinHash dedup, language detection, multilingual keyword lists, a small classifier, gazetteer hits. Most documents stop here.
3. **Cluster:** MinHash similarity plus embedding similarity plus the same category, H3 area (resolution 3 and neighbours) and time window. The document either joins an existing event (its version goes up) or creates a candidate event. **GDELT rows enter here as candidates:** they are accepted (attached as a source, with GDELT ID recorded) or discarded with a reason. The reasons are counted for the benchmark.
4. **Extract (AlterLab, paid):** once per event, on its best article. Fetch at the cheapest tier, escalating within `max_credits`. Extraction uses AlterLab with the user's OpenRouter key, set in AlterLab's dashboard. Fields: type, subtype, actors and roles, places, best location, incident time, casualties, weapons/platforms, quoted cause, stated consequences, claim type, and a supporting quote for each field.
5. **Geolocate:** the extracted place plus gazetteer candidates plus the area context. Falls back to AlterLab geocode only when confidence is low. Produces a point, a precision level and a confidence.
6. **Corroborate:** rules per category, using H3 lookups:
   - strike or explosion ↔ FIRMS hotspot or seismic event
   - air activity ↔ military aircraft tracks
   - maritime incident ↔ AIS dark periods, loitering or rerouting
   - infrastructure attack or outage ↔ VIIRS night-light drop
   - nearby bases and facilities
   - **Imagery:** for `sensor-corroborated` or high-impact events, find the closest Sentinel-2/Landsat scenes before and after (cloud cover ≤ 30%), store image crops (about 5×5 km windows read from cloud-optimised GeoTIFFs over HTTP), and record a simple change score.

   Sensor patterns also create events by themselves: unusual flight patterns (orbits, tanker/AWACS tracks), AIS dark periods, military aircraft gathering in one area, night-light drops, and (stretch) SAR-detected ships with no matching AIS.
7. **Link:** a textual cause in the extraction is resolved to an existing event (`retaliation_for`, `causes`). Sensor-observed downstream effects are linked back (`consequence_of`), for example AIS rerouting after a Red Sea attack. Heuristic escalation links (same actors and area within a time window) start at low confidence. All links are labelled hypotheses. Linked groups become **Storylines**.
8. **Score:** confidence combines source diversity, source reliability, sensor backing, location precision and how fresh the event is.
9. **Publish:** SQLite and Parquet, live state, websocket updates, REST, KML.
10. **Alert:** rules from `alerts.yaml` applied to new events and version changes.

## 6. AlterLab layer & budget ($50/month)

| Tag | Use | Monthly estimate |
|---|---|---|
| news.ingest | News headlines every 15 min | free |
| news.fetch | Best article per event (cheapest tier first) | ~$8 |
| news.extract | ~300 events/day, schema extraction | ~$10 (+ OpenRouter tokens, ~$2–5) |
| watch.official | ~60 pages with no feed, hourly; only changed pages processed | ~$20 |
| geo.resolve | Low-confidence location fallback | ~$2 |
| verify.search | Targeted checks for high-impact events | ~$2 |
| reserve | Crisis spikes | ~$8 |

- Each tag has an AlterLab `max_spend_usd` cap (enforced on AlterLab's side).
- A local spend log reads per-tag usage hourly. At 80% of a tag's monthly share, polling slows (e.g. watched pages hourly → every 3 hours). At 100% (`402 tag_budget_exhausted`), the system falls back to headline-only events at lower confidence, flagged as such.
- An AlterLab API key is used for news (the keyless limit is 60 calls/hour).

## 7. Globe UI

- **Engine:** CesiumJS on Vite/TS. Cesium World Terrain. Base maps: Bing/Esri satellite, switchable to **NASA GIBS daily true colour** and **VIIRS night lights** with a date picker.
- **Top bar:** Ctrl+K search (places, events, aircraft, ships, actors, storylines), alert ticker, per-source status lights.
- **Left — layers:** events (colour = category, size = confidence), event links and storylines (arcs), military flights with trails, ships with trails and dark-period markers, fires, quakes, bases, H3 density heatmaps, GDELT-rejected events (faded), imagery footprints.
- **Right — detail panel:** status level, confidence breakdown, per-field sources with quotes, contributing articles, linked sensor observations (highlighted on the globe), **before/after imagery slider**, arcs to causes and consequences. Storyline view: timeline, footprint, growth.
- **Bottom — timeline:** scrubber with an event-density histogram; playback filters all layers by time.
- **Ops tab:** throughput and delay per stage, cost per verified event, AlterLab spend by tag, collector health, GDELT scorecard.
- **Google Earth Pro:** `/kml/master.kml` with a NetworkLink per layer, each on its own refresh interval.

## 8. Alerts & digest

- **Channels:** in-app (ticker, sound, camera flies to the event), Windows toast (click opens the event), daily digest (06:00 IST, Markdown + HTML in `digests/`).
- **Default rules:**
  - a military or maritime event reaches `sensor-corroborated`
  - a new UKMTO/NAVAREA warning
  - high-priority military aircraft
  - a storyline grows more than 3× its normal rate
  - a new `retaliation_for` link
  - a night-light drop over a populated area

  Repeats are suppressed per rule.
- **Digest:** top events by confidence, new and growing storylines, main cause-and-effect chains, activity by domain, notable imagery changes, the GDELT scorecard, spend.

## 9. Reliability

- Retries with exponential backoff and random jitter. A circuit breaker for each source. Health status for each collector.
- A saved position for each source, so restarts resume without gaps or duplicates.
- msgspec schema checks. Malformed rows go to a quarantine table with the reason.
- SQLite in WAL mode. Parquet written to a temporary file and renamed in one step. A clean shutdown empties the queues first.
- Secrets live only in `.env`. **The FIRMS and AISStream keys found in plain files in `legacy/` must be regenerated.**

## 10. Testing

- pytest unit tests for each stage, with recorded real responses for each collector.
- A **labelled test set** of ~300 articles in 6+ languages (relevance, grouping, location, type), giving the precision and recall figures for the scorecard.
- **24-hour replay** of recorded input through the pipeline. Throughput, cost per event and quality are checked against limits.
- Matching rules tested on synthetic data with known answers.
- A Playwright smoke test for the globe.

## 11. Build order

| # | Milestone | Result |
|---|---|---|
| M0 | Foundation: git, `.env`, package layout, Store (SQLite + Parquet/DuckDB), collector system, scheduler, metrics, H3; ADS-B, AIS, FIRMS, USGS ported and made global | Global sensor data flowing |
| M1 | Globe v1: Cesium, live layers over the websocket, timeline, detail panel, NASA GIBS layers, KML master link | Earth Pro-like live view |
| M2 | News engine: AlterLab news + RSS + GDELT → triage → grouping → extraction → location → events | Grouped, located events |
| M3 | Sensor matching + scoring + events from sensor patterns + Sentinel-2/Landsat before/after imagery + night-light checks | Sensor-corroborated events with imagery |
| M4 | Official sources + alerts + digest | Notifications, morning brief |
| M5 | Event links & storylines | Cause-and-effect arcs, crisis threads |
| M6 | GDELT benchmark + ops scorecard | Proof figures |
| Later | SAR dark-ship detection, space objects (CelesTrak), sanctions (OpenSanctions), internet outages (IODA), economic signals, swapping out non-commercial sources | Wider coverage, commercial readiness |

Each milestone gets its own implementation plan.

## 12. Out of scope for the POC

Multi-user accounts and authentication, cloud deployment, inbound webhooks, Google 3D tiles, storing full article text, paid commercial data feeds, mobile apps.
