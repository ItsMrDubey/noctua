"""Country registry (osint.core.countries) over the committed config/countries.yaml."""
import json

import pytest

from osint.core import countries
from osint.core.countries import bbox, centroid, demonym_index, name, to_iso2


@pytest.mark.parametrize(("fips", "iso2"), [
    ("GM", "DE"), ("UK", "GB"), ("RS", "RU"), ("AU", "AT"), ("AS", "AU"), ("CH", "CN"),
    ("BG", "BD"), ("UP", "UA"), ("IS", "IL"), ("SZ", "CH"), ("GZ", "PS"), ("WE", "PS"),
    ("KV", "XK"), ("gm", "DE")])
def test_fips_codes(fips, iso2):
    assert to_iso2(fips, "fips") == iso2


@pytest.mark.parametrize(("code", "iso2"), [
    ("GM", "GM"), ("RS", "RS"), ("AU", "AU"), ("AS", "AS"), ("CH", "CH"), ("BG", "BG"),
    ("NO", "NO"), ("ua", "UA"), (" UA ", "UA"), ("UK", None), ("ZZ", None)])
def test_iso2_codes_are_never_read_as_fips(code, iso2):
    assert to_iso2(code, "iso2") == iso2


def test_cameo_codes():
    for code, iso2 in (("USA", "US"), ("RUS", "RU"), ("ISR", "IL"), ("UKR", "UA"),
                       ("ROM", "RO"), ("WSB", "PS"), ("KOS", "XK"), ("RUSMIL", "RU")):
        assert to_iso2(code, "cameo") == iso2, code
    for code in ("IGO", "NGO", "REB", "AFR", "MEA", "EUR", "WST", "SEA", "IGOUNO", "MIL"):
        assert to_iso2(code, "cameo") is None, code


@pytest.mark.parametrize(("value", "iso2"), [
    ("Ukraine", "UA"), ("Russian Federation", "RU"), ("Russia (Soviet Union)", "RU"),
    ("DRC", "CD"), ("DR Congo (Zaire)", "CD"), ("Democratic Republic of Congo", "CD"),
    ("Republic of Congo", "CG"), ("Cote d'Ivoire", "CI"), ("Côte d’Ivoire", "CI"),
    ("Ivory Coast", "CI"), ("Yemen (North Yemen)", "YE"), ("Bosnia-Herzegovina", "BA"),
    ("Kingdom of eSwatini (Swaziland)", "SZ"), ("Zimbabwe (Rhodesia)", "ZW"),
    ("Cambodia (Kampuchea)", "KH"), ("Serbia (Yugoslavia)", "RS"), ("Macedonia, FYR", "MK"),
    ("Palestine", "PS"), ("United States of America", "US"), ("the Netherlands", "NL"),
    ("Caribbean Netherlands", "BQ"), ("Sudan", "SD"), ("South Sudan", "SS"),
    ("Atlantis", None)])
def test_names_and_aliases(value, iso2):
    assert to_iso2(value, "name") == iso2


def test_iso3_and_auto():
    assert to_iso2("DEU", "iso3") == "DE"
    assert to_iso2("GM", "auto") == "GM"  # two letters are ISO2, never FIPS
    assert to_iso2("UKR", "auto") == "UA"
    assert to_iso2("Ukraine", "auto") == "UA"
    assert to_iso2("UK", "auto") == "GB"  # not ISO2 -> the alias
    assert to_iso2("", "auto") is None and to_iso2(None, "fips") is None


def test_unknown_hint_raises():
    with pytest.raises(ValueError):
        to_iso2("UA", "bogus")


def test_names_and_demonyms():
    assert name("UA") == "Ukraine" and name("ua") == "Ukraine"
    assert name("ZZ") == "ZZ" and name(None) == ""
    idx = demonym_index()
    assert idx["russian"] == "RU" and idx["ukrainian"] == "UA" and idx["israeli"] == "IL"
    assert idx["congolese"] == "CD" and idx["bissau guinean"] == "GW"
    assert "iran" not in idx and idx["iranian"] == "IR"
    idx["russian"] = "XX"
    assert demonym_index()["russian"] == "RU"  # a copy per call


def test_every_country_has_codes():
    rows = countries.all_countries()
    assert len(rows) == 252
    assert all(len(c.iso2) == 2 and len(c.iso3) == 3 and c.cameo for c in rows)
    assert countries.get("no").name == "Norway"


def _square(w, s, e, n):
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def _feature(cc, polys, label):
    geom = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
            else {"type": "MultiPolygon", "coordinates": polys})
    return {"type": "Feature", "geometry": geom,
            "properties": {"ISO_A2": cc, "NAME": cc, "LABEL_X": label[1], "LABEL_Y": label[0]}}


@pytest.fixture
def polygons(tmp_path, monkeypatch):
    """A small Natural Earth-shaped file behind osint.signals.countries.get_countries."""
    from osint.signals import countries as sig

    feats = [
        _feature("UA", [_square(22.13, 45.23, 40.13, 52.35)], (49.72, 32.14)),
        # mainland up to 180 E plus Chukotka west of 180 W
        _feature("RU", [_square(19.6, 41.2, 180.0, 81.85), _square(-180.0, 64.0, -169.7, 71.6)],
                 (58.25, 44.69)),
        # metropolitan France + Corsica + French Guiana 7000 km away
        _feature("FR", [_square(-4.76, 42.33, 8.23, 51.1), _square(8.54, 41.38, 9.56, 43.03),
                        _square(-54.6, 2.1, -51.6, 5.8)], (46.7, 2.55)),
    ]
    path = tmp_path / "ne.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                    encoding="utf-8")
    loaded = sig.Countries(path)
    monkeypatch.setattr(sig, "get_countries", lambda: loaded)
    return loaded


def test_bbox_from_country_polygons(polygons):
    assert bbox("ua") == (22.13, 45.23, 40.13, 52.35)
    w, s, e, n = bbox("RU")
    assert (w, e) == (19.6, -169.7) and (s, n) == (41.2, 81.85)  # crosses the antimeridian
    w, _, e, _ = bbox("FR")
    assert (w, e) == (-4.76, 9.56)  # Corsica kept, French Guiana dropped
    assert bbox("DE") is None and bbox("ZZ") is None and bbox(None) is None
    assert centroid("UA") == (49.72, 32.14) and centroid("DE") is None


def test_main_bbox_helper():
    assert countries.main_bbox([]) is None
    ring = [10.0, 0.0, 12.0, 0.0, 12.0, 2.0, 10.0, 2.0, 10.0, 0.0]
    assert countries.main_bbox([[ring]]) == (10.0, 0.0, 12.0, 2.0)


def test_unresolved_values_are_counted_until_drained():
    countries.drain_unresolved()
    assert to_iso2("Atlantis", "name") is None
    to_iso2("Atlantis", "name")
    to_iso2("IGO", "cameo")  # a known non-state code is not "unresolved"
    assert countries.drain_unresolved() == {"name:Atlantis": 2}
    assert countries.drain_unresolved() == {}


def test_unresolved_keys_are_bounded(monkeypatch):
    monkeypatch.setattr(countries, "UNRESOLVED_MAX_KEYS", 3)
    countries.drain_unresolved()
    for i in range(5):
        to_iso2(f"Atlantis{i}", "name")
    to_iso2("Atlantis0", "name")  # a value already counted keeps counting
    to_iso2("Atlantis4", "name")
    out = countries.drain_unresolved()
    assert len(out) == 4 and out[countries.UNRESOLVED_OVERFLOW] == 3
    assert out["name:Atlantis0"] == 2 and sum(out.values()) == 7
