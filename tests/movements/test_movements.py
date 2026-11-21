from datetime import UTC, datetime, timedelta

import h3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.movements import mount_movements
from osint.core.models import Position
from osint.movements import analysis
from osint.movements.airports import AirportIndex
from osint.movements.config import CorridorsConfig, LegsConfig, MovementsConfig, SurgesConfig
from osint.movements.legs import Fix, segment
from osint.movements.roles import classify, track_role
from osint.movements.service import MovementsService
from osint.store.lake import Lake, position_row

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
AIRPORTS = [
    {"ident": "AAAA", "name": "Alpha AB", "lat": 10.0, "lon": 20.0, "is_military": True},
    {"ident": "BBBB", "name": "Bravo Intl", "lat": 12.0, "lon": 24.0, "is_military": False},
    {"ident": "CCCC", "name": "Charlie", "lat": 40.0, "lon": 179.95},
]
US = 1_000_000


def _us(dt):
    return int(dt.timestamp() * US)


def flight(t0, a=(10.0, 20.0), b=(12.0, 24.0), cruise_n=10):
    """ground at a -> climb -> cruise -> ground at b, one fix per 5 minutes."""
    out = [(t0, a[0], a[1], 0, 5)]
    t = t0
    for i in range(1, cruise_n + 1):
        t += timedelta(minutes=5)
        f = i / (cruise_n + 1)
        out.append((t, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, 3000 if i in (1, cruise_n) else 30000, 420))
    t += timedelta(minutes=5)
    out.append((t, b[0] + 0.01, b[1], 200, 20))
    return out


def fixes(rows):
    return [Fix(_us(t), la, lo, alt, spd) for t, la, lo, alt, spd in rows]


@pytest.fixture
def index():
    return AirportIndex(AIRPORTS)


def test_airport_index_nearest_and_antimeridian(index):
    ap, d = index.nearest(10.05, 20.0, 15)
    assert ap["ident"] == "AAAA" and d < 6
    assert index.nearest(11.0, 22.0, 15) is None
    assert index.nearest(40.0, -179.98, 15)[0]["ident"] == "CCCC"


def test_leg_takeoff_cruise_landing(index):
    legs = segment("air:1", fixes(flight(NOW - timedelta(hours=3))), index, LegsConfig(), _us(NOW),
                   {"name": "RCH123", "type_code": "C17"}, "airlift")
    assert len(legs) == 1
    lg = legs[0]
    assert lg["origin"]["ident"] == "AAAA" and lg["origin_how"] == "ground"
    assert lg["dest"]["ident"] == "BBBB" and lg["dest_how"] == "ground" and lg["status"] == "landed"
    assert lg["callsign"] == "RCH123" and lg["max_alt_ft"] == 30000 and lg["n_fixes"] == 10


def test_gap_splits_legs_and_open_leg_is_in_flight(index):
    t0 = NOW - timedelta(hours=4)
    rows = flight(t0)[:-1]  # no landing: lost at cruise...
    last = rows[-1][0]
    # ...reappears 2 h later, cruising, still airborne at "now"
    rows += [(last + timedelta(hours=2, minutes=5 * i), 15.0, 30.0 + i * 0.1, 30000, 430) for i in range(4)]
    clock = _us(rows[-1][0] + timedelta(minutes=5))
    legs = segment("air:1", fixes(rows), index, LegsConfig(), clock, {}, "airlift")
    assert len(legs) == 2
    first, second = legs
    assert first["origin"]["ident"] == "AAAA"
    assert first["dest"] is None or first["dest_how"] == "approach"  # last fix at 3000 ft, not near B
    assert second["origin"] is None and second["status"] == "in_flight"


def test_roles():
    assert classify("C17") == "airlift" and classify("IL76") == "airlift" and classify("Y20") == "airlift"
    assert classify("K35R") == "tanker" and classify("KC46") == "tanker"
    assert classify("A332", "Air2Air", "Airbus A330-243-MRTT") == "tanker"
    assert classify("B762") == "tanker"
    assert classify("A332", "VIP Transport") == "airlift"
    assert classify("E3CF") == "isr" and classify("R135") == "isr" and classify("P8") == "isr"
    assert classify("Q9") == "isr" and classify("E2") == "isr"
    assert classify("F16") == "fighter" and classify("EUFI") == "fighter"
    assert classify("H60") == "helo" and classify("H64") == "helo" and classify("EC45") == "helo"
    assert classify(None, "Get To The Choppa") == "helo"
    assert classify("GLF5", "Bizjet") == "other" and classify(None) == "other"
    assert track_role({"type_code": None, "extra": {"desc": "Boeing KC-46A Pegasus"}}) == "tanker"


def test_corridor_aggregation():
    def leg(tid, o, d, role, t, call):
        ap = {a["ident"]: a for a in AIRPORTS}
        return {"track_id": tid, "callsign": call, "type_code": "C17", "role": role,
                "origin": ap.get(o), "dest": ap.get(d), "t_end_us": t, "t_end": str(t)}
    legs = [leg("a", "AAAA", "BBBB", "airlift", 3, "RCH1"), leg("b", "AAAA", "BBBB", "airlift", 5, "RCH2"),
            leg("a", "AAAA", "BBBB", "airlift", 1, "RCH1"), leg("c", "AAAA", "BBBB", "tanker", 2, "QID1"),
            leg("d", "AAAA", None, "airlift", 9, "X"), leg("e", "AAAA", "AAAA", "airlift", 9, "Y")]
    out = analysis.corridors(legs, CorridorsConfig(arc_points=32))
    assert [(c["id"], c["count"]) for c in out] == [("AAAA>BBBB:airlift", 3), ("AAAA>BBBB:tanker", 1)]
    c = out[0]
    assert c["aircraft"] == 2 and c["last_seen"] == "5" and c["callsigns"] == ["RCH2", "RCH1"]
    assert len(c["path"]) == 32 and c["path"][0] == [20.0, 10.0] and c["path"][-1] == [24.0, 12.0]
    assert analysis.corridors(legs, CorridorsConfig(), role="tanker")[0]["role"] == "tanker"


def test_great_circle_midpoint():
    p = analysis.great_circle(0, 0, 0, 90, 3)
    assert p[1] == [45.0, 0.0]


def _counts(entries):
    """entries: (cell, bucket, n tracks)."""
    return {(c, b): {f"t{i}" for i in range(n)} for c, b, n in entries}


def test_surge_zscore_vs_baseline():
    cfg = SurgesConfig(bucket_hours=6, baseline_days=7, z_min=2.5, min_count=3, std_floor=1.0, min_baseline_days=2)
    bucket = 6 * 3_600_000_000
    now = 1000 * bucket + 1
    cell, quiet = h3.latlng_to_cell(10, 20, 2), h3.latlng_to_cell(40, 40, 2)
    base = [(cell, 1000 - 4 * k, 2) for k in range(1, 8)]          # 2 aircraft every day
    base += [(quiet, 1000 - 4 * k, 5) for k in range(1, 8)]        # busy every day
    counts = _counts([*base, (cell, 1000, 8), (quiet, 1000, 5)])
    res = analysis.surges(counts, now, 24, cfg, history_start_us=now - 10 * 24 * 3_600_000_000)
    assert res["baseline_insufficient"] is False
    assert [x["cell"] for x in res["items"]] == [cell]
    s = res["items"][0]
    assert s["current"] == 8 and s["baseline_mean"] == 2 and s["z"] == 6.0 and s["ratio"] == 4.0
    assert len(s["boundary"]) == 6 and s["baseline_n"] == 7
    # too little history: nothing flagged
    short = analysis.surges(counts, now, 24, cfg, history_start_us=now - 3_600_000_000)
    assert short["items"] == [] and short["baseline_insufficient"] is True


def test_naval_name_regex():
    rx = analysis.naval_regex(MovementsConfig().naval.name_prefixes)
    for name in ["USS GERALD R. FORD", "USNS MERCY", "HMS QUEEN ELIZABETH", "INS VIKRANT", "FS AQUITAINE",
                 "TCG ANADOLU", "JS IZUMO", "HNLMS TROMP", "RFS ADMIRAL GORSHKOV"]:
        assert analysis.is_naval({"name": name}, rx), name
    for name in ["USSURI", "INSPIRATION", "MSC USS", "HMSTAR", "", None]:
        assert not analysis.is_naval({"name": name}, rx), name
    assert analysis.is_naval({"name": "GREY SHIP", "is_mil": True}, rx)


class FakeStore:
    def __init__(self, meta):
        self.meta = meta

    def get_tracks(self, ids):
        return {i: self.meta[i] for i in ids if i in self.meta}


def _pos(tid, kind, t, lat, lon, alt=None, spd=None):
    return position_row(Position(track_id=tid, kind=kind, ts=t, lat=lat, lon=lon, source="t", alt_ft=alt, speed_kts=spd))


@pytest.fixture
def svc(tmp_path):
    lake = Lake(tmp_path)
    rows = [_pos("air:1", "air", t, la, lo, alt, spd) for t, la, lo, alt, spd in flight(NOW - timedelta(hours=3))]
    rows += [_pos("air:2", "air", t, la, lo, alt, spd) for t, la, lo, alt, spd in flight(NOW - timedelta(hours=5))]
    rows += [_pos("air:civ", "air", t, la, lo, alt, spd) for t, la, lo, alt, spd in flight(NOW - timedelta(hours=3))]
    rows += [_pos("sea:1", "sea", NOW - timedelta(minutes=m), 30.0, 30.0 + m * 0.01, spd=12) for m in range(0, 120, 10)]
    rows += [_pos("sea:2", "sea", NOW - timedelta(minutes=5), 31.0, 31.0, spd=10)]
    lake.write("positions", rows)
    store = FakeStore({
        "air:1": {"name": "RCH1", "type_code": "C17", "is_mil": True, "extra": {}},
        "air:2": {"name": "RCH2", "type_code": "C17", "is_mil": True, "extra": {}},
        "air:civ": {"name": "DLH1", "type_code": "A320", "is_mil": False, "extra": {}},
        "sea:1": {"name": "USS TEST", "is_mil": False},
        "sea:2": {"name": "EVER GIVEN", "is_mil": False},
    })
    return MovementsService(lake, store, lambda: AIRPORTS, MovementsConfig(), clock=lambda: NOW)


def test_service_and_api_envelopes(svc):
    legs = svc.legs(24)
    assert {lg["track_id"] for lg in legs} == {"air:1", "air:2"}
    assert svc.legs(24, "tanker") == []
    app = FastAPI()
    mount_movements(app, service=svc)
    c = TestClient(app)
    j = c.get("/api/movements/corridors?hours=168").json()
    assert j["layer"] == "movements.corridors" and j["count"] == 1
    assert j["items"][0]["count"] == 2 and j["items"][0]["role"] == "airlift"
    j = c.get("/api/movements/legs?hours=24&role=airlift").json()
    assert j["count"] == 2 and "t_end_us" not in j["items"][0] and len(j["items"][0]["path"]) >= 2
    j = c.get("/api/movements/surges?hours=24").json()
    assert j["layer"] == "movements.surges" and j["baseline_insufficient"] is True and j["items"] == []
    j = c.get("/api/movements/naval?hours=24").json()
    assert [x["id"] for x in j["items"]] == ["sea:1"]
    assert j["items"][0]["match"] == "name_prefix" and len(j["items"][0]["track"]) == 12
    assert c.get("/api/movements/legs?role=bogus").status_code == 422
    assert c.get("/api/movements/corridors?hours=0").status_code == 422
