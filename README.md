# OSINT platform (POC)

Global event platform: collectors -> Parquet lake + SQLite -> API -> Cesium UI.

## Running

```
uv run osint supervise     # recommended: runs `osint run` as a child and restarts it after a crash
uv run osint run           # the server itself (UI at http://127.0.0.1:8765)
```

`supervise` logs every start/exit (exit code or signal) with timestamps to `data/supervisor.log` and
restarts with exponential backoff (2 s, 4 s, ... max 60 s); it gives up after 5 restarts within 10
minutes. A clean exit (code 0) or Ctrl+C is not restarted. `osint run` additionally writes native crash
tracebacks (faulthandler) and unhandled exceptions to `data/crash.log` (append, timestamped per start).

Memory tip (WSL2): cap the VM in `%UserProfile%\.wslconfig` (`[wsl2]` / `memory=8GB`) and run
`wsl --shutdown` when the platform is not needed so the vmmem process releases RAM.

## Security

- Binding beyond loopback requires `OSINT_API_TOKEN` (and `OSINT_ALLOWED_HOSTS`).
- With a token set every `/api`, `/ws`, `/kml`, `/metrics` request needs `Authorization: Bearer <token>`.
  - **UI**: open it once as `http://host:8765/?token=<token>`. The page stores the token in
    `sessionStorage` (tab-scoped), removes it from the address bar and sends it as a Bearer header on
    `fetch()` and as `?token=` on the live WebSocket. The token is never written into the HTML.
  - **Images** (`<img>` cannot send headers): webcam snapshots and imagery files use short-lived
    (5 min) HMAC-signed URLs (`?sig=&exp=`) minted by authenticated API responses (`snapshot_url`,
    imagery `url`, `GET /api/sign?path=`). Signatures are bound to the exact path and valid only for
    `GET /api/webcams/*/snapshot` and `/api/imagery/*/*`.
  - **Google Earth**: `/kml/master.kml?token=...` (links carry the token).
- `/api/config` (Cesium ion token) is only returned to loopback clients, or authenticated clients when
  a token is set.
- Webcam snapshot proxy: raster images only (JPEG/PNG/WebP/GIF; SVG refused), `nosniff` + CSP sandbox,
  no redirects, 5 MB cap, https-only unless a source is `http_only`, and a per-source `allowed_hosts`
  list in `config/webcams.yaml` (cameras whose image host is not listed are dropped at catalogue build).

## Licences and attribution

The UI footer opens an "Attributions & licences" panel; the same register is served as JSON at
`GET /api/sources/licences` (commercial-readiness checklist: `commercial` is `ok`, `review` or `no`).
Review before any commercial launch: Esri World Imagery, Cesium ion, adsb.lol (ODbL share-alike on the
derived positions database), adsb.fi, airplanes.live and OpenSky (non-commercial), AISStream, AlterLab,
GDACS and ReliefWeb (terms review).

## Hazard alerts

`gdacs` (earthquakes, cyclones, floods, volcanoes, droughts, forest fires), `eonet` (NASA open natural
events) and `reliefweb` (UN OCHA disasters + reports; needs an approved `RELIEFWEB_APPNAME`, disabled by
default) write `hazard` observations. The sensor-event detector `hazard` (`config/corroboration.yaml`)
turns them into `disaster` events (status `official`, glyph per hazard type), merging one hazard reported
by several sources and keying GDACS/EONET quakes on the matching USGS event; news events about disasters
use them as corroboration evidence.

## Critical infrastructure and watchlists

`uv run osint infra-fetch [ids...] [--force]` downloads the infrastructure layers into `data/reference/`
(normalised once at download; only stale datasets are re-fetched). The worker's watch job also refreshes
stale datasets in the background (`config/infra.yaml`, `job.auto_refresh`). Wikimedia rejects anonymous
clients: set `OSINT_CONTACT` (a URL or mailbox) to identify the installation in the User-Agent.

| id | layer | source | licence (commercial) | refresh |
|----|-------|--------|----------------------|---------|
| cables | submarine cables + landing points | TeleGeography public map JSON | public map CC BY-SA 4.0; data under a commercial licence (no) | 30 d |
| power | power plants (fuel, MW) | WRI Global Power Plant Database v1.3 | CC BY 4.0 (ok) | 365 d |
| pipelines | oil/gas transmission + submarine pipelines | OpenStreetMap via Overpass | ODbL (review, share-alike) | 30 d |
| refineries | oil refineries | OpenStreetMap via Overpass | ODbL (review) | 30 d |
| wikidata | LNG terminals, dams >= 50 m, satellite ground stations | Wikidata SPARQL | CC0 (ok) | 30 d |
| wpi | seaports (reference layer) | NGA World Port Index (RIMES mirror when NGA is down) | public domain (ok) | 90 d |
| opensanctions | sanctioned / shadow-fleet vessels, sanctioned aircraft | OpenSanctions `maritime` + `sanctions` targets | CC BY-NC 4.0 (no) | 2 d |

Heliports, small airfields and seaplane bases come from the OurAirports file the reference layer
already uses (viewport-loaded below ~700 km).

**Watchlists.** The worker matches live tracks every 60 s: vessels by IMO (strong), MMSI (strong unless the
AIS IMO contradicts it) and distinctive former/current names (weak, shown but never alerted); aircraft by
registration. `config/watchlist.yaml` adds your own MMSIs / IMOs / ICAO hexes / registrations / callsign
patterns with labels (reloaded on change). Matches are ringed on the globe, the track inspector shows the
programme, risk topics and OpenSanctions link, and the first sighting raises an alert (again after 24 h
unseen). OpenSanctions data is non-commercial (`commercial_ok = false`).

**Proximity alerts.** A vessel seen under way that then stays at <= 3 kn within 2 km of a submarine cable or
offshore pipeline for 30 min raises "Loitering near ..."; if it then stops transmitting for 60 min while the
AIS feed is alive, "AIS dark near ...". Positions near ports / coastlines and fishing, tug, pilot, passenger
etc. categories are ignored (all thresholds in `config/infra.yaml`). Alerts and current matches live in
`data/infra_watch.sqlite`; API: `/api/infra/*`, `/api/watch/active`, `/api/watch/alerts`, `/api/watch/list`.

## History histogram

`GET /api/history/counts?layer=fires|quakes|events|historical|aircraft|ships&bin=hour|day&start=&end=`
returns sparse `[[bin_start_ms, n], ...]` aggregates computed server-side (closed bins cached in memory,
warmed after start-up); the timeline histogram issues one request per layer.

## Signals & space layers

Layer group "Signals & space" (rail icon with radio waves) plus a status pill in the command bar.
Collectors run in the worker and write live snapshots to `data/signals/*.json` (served by
`/api/signals/*`); notable signals become sensor events (`osint.signals.job`, `config/signals.yaml`).

| Layer | Source (free, no key) | Refresh |
|---|---|---|
| GNSS interference (gpsjam-style H3 map) | adsb.fi all-traffic queries in watch regions (`sources.yaml` -> `adsb_signals.regions`): share of aircraft with ADS-B NIC < 7 / NACp < 8 / GPS lost | 2 min |
| Emergency squawks 7500/7600/7700 | adsb.fi `sqk` queries (worldwide), one entry per flight | 2 min |
| Satellites (orbit, ground track, footprint) | CelesTrak GP (OMM) groups, SGP4 in a browser worker | 6 h (CelesTrak blocks re-downloads within 2 h) |
| Internet outages (country shading) | IODA (Georgia Tech) outage events | 10 min |
| Space weather widget, aurora oval | NOAA SWPC (Kp, G/S/R scales, alerts, solar wind, OVATION) | 5 / 30 min |
| Radiation dose rate | BfS ODL (Germany) + Safecast realtime devices; anomalies vs each station's 7-day median | 15 min |

`scripts/fetch_satellitejs.sh` vendors satellite.js 6.0.1 into `frontend/vendor/satellite`
(otherwise the worker loads the same pinned build from jsDelivr / cdnjs). Not included: Cloudflare
Radar (API token required), EURDEP (registration required), lightning (Blitzortung forbids reuse;
no other free live feed).

## Terror Watch

Rail icon with a warning triangle, right-panel tab "Terror", dossier panes (global board, country
boards, US posture, group pages, definition & sources) and Intel-console tiles for incidents.
Defensive, public-source situational awareness: incidents, groups, designations, official threat
levels and indicators & warnings (I&W) -- indicators with stated baselines, never predictions.

* **Definition**: GTD-aligned (intentional violence by a sub-national actor + at least 2 of: political/
  religious/social goal, wider audience, outside legitimate warfare) with documented operational
  rules (warfare, insurgency vs security forces = "possible", crime, CT operations, plots/arrests as
  separate classes, denials never incidents). Served at `GET /api/terror/definition`.
* **Incidents** (`osint.terror`, worker job, `data/terror.sqlite`): news events whose headlines use
  terrorism vocabulary (multilingual) get one LLM extraction (verbatim-checked quotes; same backend and
  caps as event extraction, `config/terror.yaml`), then deterministic rules decide class, status
  (confirmed = >= 2 independent outlets, reposts and same-organisation feeds collapsed, Telegram = one
  outlet; reported = single origin), attribution level (claimed / authorities / suspected / unknown,
  each with its quote), location (from the incident description, capital metonyms rejected, precision
  stated), casualties (every source kept, ranges), event time (follow-up coverage flagged).
* **Sources** (all free; licence register at `/api/sources/licences`, health at `/api/terror/sources`):
  MI5/JTAC RSS, Vigipirate (SGDSN), NCTV, ASIO, DHS NTAS (feed; AlterLab fallback when dhs.gov blocks
  the client), State Dept travel advisories API, US FTO list, OFAC SDN (SDGT), UN consolidated list
  (1267/1988), UK proscribed groups (GOV.UK content API), EU FSF (TERR + EU ISIL/AQ), DOJ press API
  (terrorism topics / NSD), Europol newsroom, GNET research, UCDP GED + Candidate (one-sided violence
  by non-state actors, base rates). GTD is cited, not ingested (licence).
* **Groups**: `config/terror_groups.yaml` (identity and multilingual aliases only); designation status
  comes from the lists (exact-name matches), areas of operation and tempo from data. Name-only matches
  are shown as leads.
* `uv run osint terror-pass [--only ids] [--no-llm]` runs one pass; `uv run osint terror-eval` scores
  the classifier on the hand-labelled sample (`tests/terror/fixtures/eval_labels.json`) and writes
  `data/terror_eval.json` (shown in the Definition pane).
