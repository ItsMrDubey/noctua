"""Global AIS positions and static data from the AISStream websocket."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import websockets

from osint.core.collector import CollectorContext, StreamCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Position, TrackInfo

log = logging.getLogger(__name__)

AIS_URL = "wss://stream.aisstream.io/v0/stream"
GLOBAL_BBOX = [[[-90, -180], [90, 180]]]
POSITION_TYPES = (
    "PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"
)

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
    if mtype in POSITION_TYPES:
        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        lat, lon = float(lat), float(lon)  # TypeError/ValueError -> caller skips record
        if lat == 91.0 or lon == 181.0:  # AIS "not available" sentinels
            return None, None
        cog = body.get("Cog")
        if cog is not None and cog >= 360:  # 360 = not available
            cog = None
        heading = body.get("TrueHeading")
        if heading is None or heading == 511:  # 511 = not available
            heading = cog
        sog = body.get("Sog")
        ts = parse_ais_time(meta["time_utc"]) if meta.get("time_utc") else utcnow()
        return Position(
            track_id=track_id, kind="sea", ts=ts, lat=lat, lon=lon,
            source="aisstream",
            speed_kts=sog if sog is not None and sog < 102.3 else None,  # 102.3 = n/a
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
            "FilterMessageTypes": [*POSITION_TYPES, "ShipStaticData"],
        }
        batch_s = float(self.cfg.get("batch_seconds", 5))
        batch_max = int(self.cfg.get("batch_max", 5000))
        async with self._connect(AIS_URL, open_timeout=30, ping_interval=20,
                                 max_size=2**20) as ws:
            await ws.send(json.dumps(subscribe))
            batch, count, deadline = Batch(source=self.name), 0, time.monotonic() + batch_s
            skipped = 0
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
                    try:
                        pos, track = parse_message(msg)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        skipped += 1
                        count += 1
                        pos = track = None
                    if pos is not None:
                        batch.positions.append(pos)
                    if track is not None:
                        batch.tracks.append(track)
                    count += 1
                if time.monotonic() >= deadline or count >= batch_max:
                    if skipped:
                        log.warning("AIS: skipped %d malformed message(s) in batch", skipped)
                    skipped = 0
                    if batch.size():
                        yield batch
                    batch, count, deadline = Batch(source=self.name), 0, time.monotonic() + batch_s
