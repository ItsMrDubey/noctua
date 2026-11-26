import json
import math

from osint.reference import loader
from osint.reference.chokepoints import chokepoint_features
from osint.reference.geometry import in_bbox, runway_rect

AIRPORTS = (
    '"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft","continent","iso_country",'
    '"iso_region","municipality","scheduled_service","icao_code","iata_code","gps_code","local_code",'
    '"home_link","wikipedia_link","keywords"\n'
    '1,"KBIG","large_airport","Big Intl",10.0,20.0,100,"NA","US","US-X","Bigtown","yes",,"BIG","KBIG","BIG",,,\n'
    '2,"KMED","medium_airport","Med Field",11.0,21.0,5,"NA","US","US-X","Medtown","yes",,,"KMED","MED",,,\n'
    '3,"KSML","small_airport","Tiny Strip",12.0,22.0,5,"NA","US","US-X","T","no",,,"KSML","SML",,,\n'
    '4,"KAFB","small_airport","Foo AFB",13.0,23.0,5,"NA","US","US-X","T","no",,,"KAFB","AFB",,,\n'
    '5,"HELI","heliport","Hospital Heliport",14.0,24.0,5,"NA","US","US-X","T","no",,,"HELI","H",,,\n'
    '6,"MHEL","heliport","Plain Pad",15.0,25.0,5,"NA","US","US-X","T","no",,,"MHEL","M",,,"military"\n'
    '7,"NOLL","large_airport","No Coord",,,5,"NA","US","US-X","T","no",,,"NOLL","N",,,\n'
)
RUNWAYS = (
    '"id","airport_ref","airport_ident","length_ft","width_ft","surface","lighted","closed","le_ident",'
    '"le_latitude_deg","le_longitude_deg","le_elevation_ft","le_heading_degT","le_displaced_threshold_ft",'
    '"he_ident","he_latitude_deg","he_longitude_deg","he_elevation_ft","he_heading_degT","he_displaced_threshold_ft"\n'
    '1,1,"KBIG",10000,150,"ASP",1,0,"18",10.01,20.0,,,,"36",9.99,20.0,,,\n'
    '2,1,"KBIG",5000,100,"ASP",1,0,"09",,,,,,"27",,,,,\n'
    '3,2,"KMED",5000,100,"ASP",1,1,"09",11.0,20.99,,,,"27",11.0,21.01,,,\n'
)


def test_military_detection():
    assert loader.is_military("Ramstein AB")
    assert loader.is_military("RAF Lakenheath")
    assert loader.is_military("Nellis Air Force Base")
    assert loader.is_military("Plain", "military, usaf")
    assert not loader.is_military("Table Mountain Airport")
    assert not loader.is_military("Abbotsford Airport")


def test_airport_filtering(tmp_path):
    p = tmp_path / "airports.csv"
    p.write_text(AIRPORTS, encoding="utf-8")
    a = {x["ident"]: x for x in loader.load_airports(p)}
    assert set(a) == {"KBIG", "KMED", "KAFB", "MHEL"}
    assert a["KAFB"]["is_military"] and not a["KBIG"]["is_military"]
    assert a["KBIG"]["iata"] == "BIG" and a["KBIG"]["icao"] == "KBIG"


def test_runway_rect_ns_geometry():
    rect = runway_rect(10.0, 20.0, 11.0, 20.0, 328.084)  # 100 m wide, due north
    lons = sorted({p[0] for p in rect})
    assert len(rect) == 4
    half_deg = 50 / (6371008.8 * math.pi / 180 * math.cos(math.radians(10.5)))
    assert math.isclose(lons[0], 20 - half_deg, abs_tol=2e-6)
    assert math.isclose(lons[1], 20 + half_deg, abs_tol=2e-6)
    assert sorted({p[1] for p in rect}) == [10.0, 11.0]


def test_runways_skip_missing_coords_and_closed(tmp_path):
    p = tmp_path / "runways.csv"
    p.write_text(RUNWAYS, encoding="utf-8")
    rw = loader.load_runways(p, {"KBIG", "KMED"})
    assert list(rw) == ["KBIG"] and len(rw["KBIG"]) == 1
    assert rw["KBIG"][0]["length_ft"] == 10000 and len(rw["KBIG"][0]["rect"]) == 4


def test_bbox_and_antimeridian():
    assert in_bbox(5, 5, (0, 0, 10, 10)) and not in_bbox(50, 5, (0, 0, 10, 10))
    assert in_bbox(0, 179, (170, -5, -170, 5)) and in_bbox(0, -175, (170, -5, -170, 5))
    assert not in_bbox(0, 0, (170, -5, -170, 5))


def test_reference_queries(tmp_path):
    (tmp_path / "airports.csv").write_text(AIRPORTS, encoding="utf-8")
    (tmp_path / "runways.csv").write_text(RUNWAYS, encoding="utf-8")
    r = loader.Reference(tmp_path)
    assert {a["ident"] for a in r.query_airports(None, "large")} == {"KBIG", "KAFB", "MHEL"}
    assert {a["ident"] for a in r.query_airports((19, 9, 22, 12), "medium")} == {"KBIG", "KMED"}
    assert {a["ident"] for a in r.query_airports(None, "medium", True)} == {"KAFB", "MHEL"}
    assert len(r.query_runways((19, 9, 22, 12))) == 1
    ab = r.query_bases(None, {"airbase"})
    assert ab and all(b["type"] == "airbase" for b in ab)


def test_missing_files_give_empty_layers(tmp_path):
    r = loader.Reference(tmp_path)
    assert r.airports == [] and r.ports == [] and r.countries == []
    assert all(v["features"] == [] for v in r.boundaries.values())
    assert r.bases  # curated list ships with the package


def test_boundaries_rounding(tmp_path):
    p = tmp_path / "b.geojson"
    p.write_text('{"features":[{"properties":{"NAME":"x"},"geometry":{"type":"LineString",'
                 '"coordinates":[[1.123456789,2.987654321],[3.5,4.5]]}},'
                 '{"properties":{},"geometry":{"type":"Point","coordinates":[0,0]}}]}', encoding="utf-8")
    fc = loader.load_boundaries(p)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["geometry"]["coordinates"][0] == [1.1235, 2.9877]


def test_ports_fallback_and_wpi(tmp_path):
    ne = tmp_path / "ne.geojson"
    ne.write_text('{"features":[{"properties":{"name":"P"},"geometry":{"type":"Point","coordinates":[1,2]}}]}',
                  encoding="utf-8")
    assert loader.load_ports(tmp_path / "wpi.csv", ne)[0]["name"] == "P"
    wpi = tmp_path / "wpi.csv"
    wpi.write_text("Main Port Name,Country Code,Latitude,Longitude,Harbor Size,Harbor Type\n"
                   "ROTTERDAM,NL,51.9,4.1,L,Coastal Natural\n", encoding="utf-8")
    p = loader.load_ports(wpi, ne)[0]
    assert p["harbor_size"] == "large" and p["country"] == "NL"


def test_chokepoints_closed_rings():
    cps = chokepoint_features()
    assert len(cps) >= 15
    for c in cps:
        assert c["ring"][0] == c["ring"][-1] and len(c["ring"]) >= 4
        assert c["name"] and c["note"]


def test_countries_carry_iso2(tmp_path):
    def feat(name, a2, eh, x, y):
        return {"type": "Feature", "geometry": None,
                "properties": {"NAME": name, "ISO_A2": a2, "ISO_A2_EH": eh,
                               "ISO_A3": "-99", "LABEL_X": x, "LABEL_Y": y}}

    p = tmp_path / "c.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        feat("Ukraine", "UA", "UA", 31.0, 48.0),
        feat("Norway", "-99", "NO", 9.0, 61.0),      # Natural Earth's -99 quirk
        feat("N. Cyprus", "-99", "-99", 33.5, 35.2)]}), encoding="utf-8")
    out = loader.load_countries(p)
    assert [(c["name"], c["iso_a2"]) for c in out] == [
        ("Ukraine", "UA"), ("Norway", "NO"), ("N. Cyprus", None)]
