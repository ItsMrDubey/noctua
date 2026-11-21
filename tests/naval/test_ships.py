from osint.naval import indicators as I
from tests.naval.helpers import cfg, synthetic_geo, ts

US = 1_000_000


def rows_loiter(tid, lon0=9.0, speed=6.0, drift=0.01, hours=14, lat0=0.0):
    return [(tid, int(ts(h * 60).timestamp() * US), lat0 + (h % 3) * drift, lon0 + (h % 2) * drift,
             None, speed, 0.0) for h in range(hours)]


def test_support_ship_name_matching():
    sc = cfg().sec("ships")
    assert I.support_ship_match({"name": "USNS JOHN LEWIS"}, sc) == "named"
    assert I.support_ship_match({"name": "LARAMIE"}, sc) == "named"
    assert I.support_ship_match({"name": "MAERSK LARAMIE"}, sc) is None
    assert I.support_ship_match({"name": "SOME TUG", "type_code": "35"}, sc) == "military"
    assert I.support_ship_match({"name": "EVER GIVEN", "type_code": "70"}, sc) is None


def test_loitering_oiler_in_open_water_is_evidence():
    metas = {"sea:1": {"name": "USNS PECOS"}}
    evs = I.ship_indicators(rows_loiter("sea:1"), metas, synthetic_geo(), cfg(), now=ts(14 * 60))
    assert len(evs) == 1
    e = evs[0]
    assert e.kind == "support_ship" and e.weight == 2.5 and "loitering" in e.description


def test_transiting_or_coastal_or_short_tracks_are_not_loitering():
    metas = {"sea:2": {"name": "USNS PECOS"}}
    geo, c = synthetic_geo(), cfg()
    moving = rows_loiter("sea:2", speed=16)
    assert not I.ship_indicators(moving, metas, geo, c, now=ts(900))
    wide = [(r[0], r[1], r[2], 9.0 + i * 0.2, None, 8.0, 0.0) for i, r in enumerate(rows_loiter("sea:2"))]
    assert not I.ship_indicators(wide, metas, geo, c, now=ts(900))      # radius > 60 km
    coastal = rows_loiter("sea:2", lon0=2.6)
    assert not I.ship_indicators(coastal, metas, geo, c, now=ts(900))   # near the island
    short = rows_loiter("sea:2", hours=3)
    assert not I.ship_indicators(short, metas, geo, c, now=ts(900))
    unknown = I.ship_indicators(rows_loiter("sea:3"), {"sea:3": {"name": "EVER GIVEN"}}, geo, c,
                                now=ts(900))
    assert not unknown
