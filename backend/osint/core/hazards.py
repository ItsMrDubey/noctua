"""Shared vocabulary for natural-hazard collectors (GDACS, NASA EONET, ReliefWeb).

Every hazard record becomes an ``Observation`` of type ``hazard``:

* ``id``      ``hazard:<source>:<upstream id>`` (stable across updates)
* ``version`` changes when the upstream record is revised (ingest de-duplicates on it)
* ``ts``      the upstream last-update time, clamped to at most ``TS_CLAMP`` in the past:
              long-running hazards (droughts, volcanoes) would otherwise land in lake
              partitions the hazard detector never reads. Real times are in attrs.
* ``value``   the source's severity number (magnitude, wind km/h, burnt ha, ...) or None
* ``attrs``   ``source``, ``hazard`` (one of HAZARD_TYPES), ``name``, ``alert_level``
              (green/orange/red, GDACS only), ``t_start`` / ``t_end`` / ``updated_at`` (ISO),
              ``precision`` (point | country), ``country`` / ``iso2`` / ``iso3``, ``url``, ...

``osint.pipeline.sensor_events`` (detector ``hazard``) turns them into ``disaster`` events and
``osint.pipeline.corroborate`` uses them as evidence for news events about disasters.
"""

from datetime import UTC, datetime, timedelta

HAZARD_TYPES = ("earthquake", "tropical_cyclone", "flood", "volcano", "drought", "wildfire",
                "tsunami", "severe_storm", "landslide", "extreme_temperature", "snow",
                "dust_haze", "sea_lake_ice", "water_color", "epidemic", "manmade", "other")
ALERT_LEVELS = ("green", "orange", "red")
TS_CLAMP = timedelta(hours=47)  # the hazard detector reads 48 h of observations


def parse_time(value: object) -> datetime | None:
    """ISO 8601 (with or without offset/Z; naive = UTC) -> aware UTC datetime, else None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def observed_ts(updated: datetime | None, now: datetime) -> datetime:
    """Observation ts: the upstream update time, within [now - TS_CLAMP, now]."""
    if updated is None:
        return now
    return min(now, max(updated, now - TS_CLAMP))


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def ring_centroid(ring: list) -> tuple[float, float] | None:
    """(lon, lat) vertex mean of a GeoJSON ring (good enough for small hazard polygons)."""
    pts = [p for p in ring if isinstance(p, list | tuple) and len(p) >= 2]
    if not pts:
        return None
    return (sum(float(p[0]) for p in pts) / len(pts), sum(float(p[1]) for p in pts) / len(pts))


def thin_ring(ring: list, max_points: int = 200) -> list[list[float]]:
    """Keep at most ``max_points`` vertices (every n-th, closed), rounded to 4 decimals."""
    step = max(1, -(-len(ring) // max_points))
    out = [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in ring[::step]]
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out
