import json
import math

import h3

from osint.naval.geo import AirportIndex, OceanMask, land_cells_from_geojson, mean_point
from tests.naval.helpers import synthetic_geo


def test_land_mask_from_geojson_keeps_small_islands(tmp_path):
    big = [[[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]]]
    tiny = [[[30.0, 10.0], [30.01, 10.0], [30.01, 10.01], [30.0, 10.0]]]  # smaller than a cell
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": big}},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "MultiPolygon", "coordinates": [tiny]}}]}
    p = tmp_path / "land.geojson"
    p.write_text(json.dumps(gj))
    land = land_cells_from_geojson(p)
    mask = OceanMask(land)
    assert not mask.is_ocean(0, 0)
    assert not mask.is_ocean(10.005, 30.005)          # vertex cell kept
    assert mask.is_ocean(0, 10)
    assert mask.coast_km(0, 0) == 0
    d = mask.coast_km(0, 5)                           # 3 deg east of the square's edge ~ 333 km
    assert 250 < d < 400


def test_coast_distance_and_open_ocean():
    geo = synthetic_geo()
    assert geo.coast_km(0, 0) == 0
    assert geo.coast_km(0, 1.0) == 0                  # inside the ~300 km island
    near = geo.coast_km(0, 3.2)
    far = geo.coast_km(0, 6.0)
    assert 0 < near < far
    assert geo.open_ocean(0, 8, 80)
    assert not geo.open_ocean(0, 2.8, 80)


def test_airport_index_and_wrap():
    idx = AirportIndex([(10.0, 179.9, "EAST"), (50.0, 0.0, "MID")])
    d, ident = idx.nearest_km(10.0, -179.9, 100)
    assert ident == "EAST" and d < 30
    assert idx.nearest_km(0, 0, 100) == (math.inf, None)


def test_mean_point_antimeridian():
    lat, lon = mean_point([(0, 179), (0, -179)])
    assert abs(abs(lon) - 180) < 1e-6 and abs(lat) < 1e-9
    assert h3.get_resolution(h3.latlng_to_cell(0, 0, 4)) == 4
