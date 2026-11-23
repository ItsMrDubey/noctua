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

## History histogram

`GET /api/history/counts?layer=fires|quakes|events|historical|aircraft|ships&bin=hour|day&start=&end=`
returns sparse `[[bin_start_ms, n], ...]` aggregates computed server-side (closed bins cached in memory,
warmed after start-up); the timeline histogram issues one request per layer.
