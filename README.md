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
derived positions database), airplanes.live and adsb.fi (non-commercial), AISStream, AlterLab.

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
