import itertools
import json

import pytest

from osint.fires import frontline

# A toy "Ukraine" (lon 30-40, lat 45-52) whose eastern part is occupied by two adjacent
# polygons sharing the lat-50 edge; the east edge (lon 40) is the border with Russia.
UKRAINE = [[[30, 45], [40, 45], [40, 52], [30, 52], [30, 45]]]
OCC_A = [[[36, 46], [40, 46], [40, 50], [36, 50], [36, 46]]]
OCC_B = [[[36, 50], [40, 50], [40, 51], [36, 51], [36, 50]]]


def feature(name, rings, kind="Polygon"):
    return {"type": "Feature", "properties": {"name": name},
            "geometry": {"type": kind, "coordinates": rings}}


def payload():
    fc = {"type": "FeatureCollection", "features": [
        feature("Окуповано /// Occupied /// geoJSON.status.occupied", OCC_A),
        feature("Статус невідомий /// Unknown status /// geoJSON.status.unknown\n", OCC_B),
        feature("Придністров'я /// Transnistria /// geoJSON.territories.transnistria",
                [[[28, 46], [29, 46], [29, 47], [28, 46]]]),
        {"type": "Feature", "properties": {"name": "Direction of attack"},
         "geometry": {"type": "Point", "coordinates": [37, 48]}},
    ]}
    return {"id": 42, "datetime": "22.11 o 22:37", "map": fc}


def countries(tmp_path):
    (tmp_path / frontline.COUNTRIES).write_text(json.dumps({"features": [
        {"properties": {"ADM0_A3": "UKR"}, "geometry": {"type": "Polygon", "coordinates": UKRAINE}},
        {"properties": {"ADM0_A3": "RUS"}, "geometry": {"type": "Polygon", "coordinates": [
            [[40, 45], [50, 45], [50, 52], [40, 52], [40, 45]]]}}]}), encoding="utf-8")


def segments(lines):
    return {(tuple(a), tuple(b)) for line in lines for a, b in itertools.pairwise(line)}


def test_occupied_polygons_keep_only_occupied_and_grey_zone():
    polys = frontline.occupied_polygons(payload()["map"])
    assert polys == [OCC_A, OCC_B]


def test_front_line_drops_shared_edges_and_the_border(tmp_path):
    countries(tmp_path)
    rec = frontline.build(payload(), tmp_path, probe_km=1.0)
    assert rec["clipped_to_ukraine"] is True and rec["source_id"] == 42
    segs = segments(rec["lines"])
    assert ((36, 46), (40, 46)) in segs  # south edge faces held land
    assert ((36, 50), (36, 46)) in segs and ((36, 51), (36, 50)) in segs  # west edges
    assert ((40, 51), (36, 51)) in segs  # north edge of the grey zone
    assert not any(a[0] == 40 and b[0] == 40 for a, b in segs)  # Russian border
    assert ((40, 50), (36, 50)) not in segs and ((36, 50), (40, 50)) not in segs  # shared
    assert rec["length_km"] == pytest.approx(4 * 75.6 + 5 * 111.2 + 4 * 70.4, rel=0.03)


def test_front_line_without_countries_uses_every_outer_edge(tmp_path):
    rec = frontline.build(payload(), tmp_path)
    assert rec["clipped_to_ukraine"] is False
    assert any(a[0] == 40 and b[0] == 40 for a, b in segments(rec["lines"]))


def test_build_rejects_a_map_without_occupied_polygons(tmp_path):
    with pytest.raises(ValueError):
        frontline.build({"map": {"features": []}}, tmp_path)


def test_cache_roundtrip_and_geojson(tmp_path):
    countries(tmp_path)
    rec = frontline.build(payload(), tmp_path)
    frontline.save(rec, tmp_path)
    loaded = frontline.load(tmp_path)
    assert loaded["lines"] == rec["lines"]
    gj = frontline.geojson(loaded)
    assert gj["features"][0]["geometry"]["type"] == "MultiLineString"
    assert gj["features"][0]["properties"]["source_datetime"] == "22.11 o 22:37"
    assert frontline.lines_latlon(loaded)[0][0] == (rec["lines"][0][0][1], rec["lines"][0][0][0])
    assert frontline.geojson(None) == {"type": "FeatureCollection", "features": []}
    assert frontline.load(tmp_path / "missing") is None
