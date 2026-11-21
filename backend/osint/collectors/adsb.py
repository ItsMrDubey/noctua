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
