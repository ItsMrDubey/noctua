import asyncio
import json
import logging
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


def _pos(mtype="PositionReport", **body):
    base = {"Latitude": 30.0, "Longitude": 32.0, "Sog": 5.0, "Cog": 90.0, "TrueHeading": 91}
    base.update(body)
    return {"MessageType": mtype, "MetaData": {"MMSI": 1},
            "Message": {mtype: base}}


@pytest.mark.parametrize("mtype", ["StandardClassBPositionReport", "ExtendedClassBPositionReport"])
def test_class_b_reports_parsed_as_positions(mtype):
    pos, track = parse_message(_pos(mtype))
    assert track is None and pos.track_id == "sea:1"
    assert (pos.lat, pos.lon, pos.speed_kts, pos.heading_deg) == (30.0, 32.0, 5.0, 91)


async def test_subscribe_includes_class_b():
    ws = FakeWS([json.dumps(POS_MSG)])
    c = AisCollector({"batch_seconds": 0.05})
    c._connect = lambda *a, **k: ws
    gen = c.stream(SimpleNamespace(settings=SimpleNamespace(aisstream_api_key="k")))
    await asyncio.wait_for(gen.__anext__(), timeout=2)
    await gen.aclose()
    types = json.loads(ws.sent[0])["FilterMessageTypes"]
    assert {"StandardClassBPositionReport", "ExtendedClassBPositionReport"} <= set(types)


def test_cog_360_unavailable():
    pos, _ = parse_message(_pos(Cog=360.0, TrueHeading=511))
    assert pos.heading_deg is None
    pos, _ = parse_message(_pos(Cog=360.0, TrueHeading=45))
    assert pos.heading_deg == 45


def test_sog_sentinel_102_3_unavailable():
    assert parse_message(_pos(Sog=102.3))[0].speed_kts is None
    assert parse_message(_pos(Sog=102.2))[0].speed_kts == 102.2


def test_position_sentinels_dropped():
    assert parse_message(_pos(Latitude=91.0)) == (None, None)
    assert parse_message(_pos(Longitude=181.0)) == (None, None)


async def test_stream_survives_bad_records(caplog):
    bad_time = json.loads(json.dumps(POS_MSG))
    bad_time["MetaData"]["time_utc"] = "garbage"
    bad_lat = _pos(Latitude="x")
    ws = FakeWS([json.dumps(bad_time), json.dumps(bad_lat), json.dumps(POS_MSG)])
    c = AisCollector({"batch_seconds": 0.05})
    c._connect = lambda *a, **k: ws
    gen = c.stream(SimpleNamespace(settings=SimpleNamespace(aisstream_api_key="k")))
    with caplog.at_level(logging.WARNING, logger="osint.collectors.ais"):
        batch = await asyncio.wait_for(gen.__anext__(), timeout=2)
    await gen.aclose()
    assert len(batch.positions) == 1
    assert len([r for r in caplog.records if "skipped 2" in r.getMessage()]) == 1
