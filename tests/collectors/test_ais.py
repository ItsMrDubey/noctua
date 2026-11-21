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
