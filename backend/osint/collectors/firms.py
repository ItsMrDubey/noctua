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
