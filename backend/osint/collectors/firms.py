"""NASA FIRMS active fire detections, worldwide (VIIRS + MODIS NRT)."""

import asyncio
import csv
import hashlib
import io
import logging
from datetime import UTC, datetime

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Observation

log = logging.getLogger(__name__)

BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
DEFAULT_PRODUCTS = ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT"]
# world CSVs are large and FIRMS is slow; keep connect short but allow long reads
REQUEST_TIMEOUT = httpx.Timeout(600, connect=30)
# FIRMS day ranges are UTC calendar days and NRT detections arrive up to ~3 h after
# acquisition: for this many hours after midnight also ask for yesterday, or late
# detections from the previous evening would never be fetched
MIDNIGHT_OVERLAP_H = 6


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


def parse_csv(text: str, product: str) -> list[Observation]:
    """Sync CSV parsing (run in a thread); malformed rows are skipped and counted."""
    out: list[Observation] = []
    skipped = 0
    for row in csv.DictReader(io.StringIO(text)):
        try:
            obs = parse_row(row, product)
        except (ValueError, TypeError, KeyError):
            skipped += 1
            continue
        if obs is not None:
            out.append(obs)
    if skipped:
        log.warning("FIRMS %s: skipped %d malformed row(s)", product, skipped)
    return out


@register
class FirmsCollector(PollCollector):
    name = "firms"
    domain = "fires"
    licence = "NASA open data (attribution: NASA FIRMS)"
    commercial_ok = True
    default_interval_s = 300.0
    poll_timeout_s = 600.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._last_digest: dict[str, str] = {}  # product -> sha1 of the last CSV parsed

    def days(self, now: datetime) -> int:
        days = int(self.cfg.get("days", 1))
        if now.hour < int(self.cfg.get("midnight_overlap_h", MIDNIGHT_OVERLAP_H)):
            days = max(days, 2)
        return days

    async def _fetch(self, ctx: CollectorContext, key: str, product: str,
                     days: int) -> list[Observation]:
        area = self.cfg.get("area", "world")
        try:
            r = await ctx.http.get(f"{BASE_URL}/{key}/{product}/{area}/{days}", timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:  # message would contain the URL (and key)
            raise RuntimeError(f"FIRMS {product}: HTTP {e.response.status_code}") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"FIRMS {product}: {type(e).__name__}") from None
        text = r.text
        if not text.startswith("latitude"):
            raise RuntimeError(f"FIRMS {product}: {text.replace(key, '***')[:200].strip()}")
        # FIRMS refreshes a product a few times an hour; between updates a poll returns the
        # same CSV: skip parsing it and re-submitting thousands of known detections
        digest = hashlib.sha1(f"{days}|{text}".encode()).hexdigest()
        if self._last_digest.get(product) == digest:
            return []
        obs = await asyncio.to_thread(parse_csv, text, product)
        self._last_digest[product] = digest
        return obs

    async def poll(self, ctx: CollectorContext) -> Batch:
        key = ctx.settings.firms_map_key
        if not key:
            raise RuntimeError("FIRMS_MAP_KEY not set")
        products = self.cfg.get("products") or DEFAULT_PRODUCTS
        days = self.days(utcnow())
        results = await asyncio.gather(
            *(self._fetch(ctx, key, p, days) for p in products), return_exceptions=True
        )
        errors = [f"{p}: {str(r).replace(key, '***')}"
                  for p, r in zip(products, results, strict=True)
                  if isinstance(r, BaseException)]
        for err in errors:
            log.warning("FIRMS product failed: %s", err)
        if len(errors) == len(products):
            raise RuntimeError("all FIRMS products failed: " + "; ".join(errors))
        batch = Batch(source=self.name)
        for r in results:
            if not isinstance(r, BaseException):
                batch.observations.extend(r)
        return batch
