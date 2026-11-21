"""CountryCities: country-constrained cities1000 lookup for the extraction geocoder."""

import zipfile

from osint.enrich.gazetteer import CountryCities
from osint.pipeline.extract.verdict import gazetteer_geocoder

ROWS = [
    # gid, name, ascii, alternates, lat, lon, cc, admin1 code, population
    (1, "Kupiansk", "Kupiansk", "Kupyansk,Купянськ", 49.71, 37.61, "UA", "07", 26000),
    (2, "Shakhtarske", "Shakhtarske", "", 47.96, 36.0, "UA", "04", 3000),
    (3, "Kupiansk", "Kupiansk", "", 10.0, 10.0, "XX", "01", 90000),  # other country
    (4, "Pokrovske", "Pokrovske", "", 47.98, 36.23, "UA", "12", 9000),
    (5, "Pokrovske", "Pokrovske", "", 48.11, 37.92, "UA", "05", 4000),
]


def _write(dest):
    dest.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join([str(g), n, a, alt, str(lat), str(lon), "P", "PPL", cc, "", a1, "", "",
                        "", str(pop), "", "100", "Europe/X", "2026-01-01"])
             for g, n, a, alt, lat, lon, cc, a1, pop in ROWS]
    with zipfile.ZipFile(dest / "cities1000.zip", "w") as z:
        z.writestr("cities1000.txt", "\n".join(lines) + "\n")
    (dest / "admin1CodesASCII.txt").write_text(
        "UA.05\tDonetsk\tDonetsk\t1\nUA.12\tDnipropetrovsk\tDnipropetrovsk\t1\n",
        encoding="utf-8")
    return dest


def test_lookup_is_country_constrained(tmp_path):
    cc = CountryCities(_write(tmp_path))
    hit = cc.lookup("Kupyansk", "", "UA")
    assert hit is not None and (hit.lat, hit.country, hit.precision) == (49.71, "UA", "city")
    assert cc.lookup("Kupiansk", "", "PL") is None
    assert cc.lookup("Kupiansk", "", "") is None  # no stated country: not used
    assert cc.lookup("Shakhtarske, Zaporizhzhia Oblast", "", "UA").lat == 47.96
    # admin1 disambiguates, else the most populous
    assert cc.lookup("Pokrovske", "Donetsk", "ua").lat == 48.11
    assert cc.lookup("Pokrovske", "", "UA").lat == 47.98
    assert (tmp_path / "cities1000.sqlite").is_file()
    assert CountryCities(tmp_path).lookup("Kupiansk", "", "UA") is not None  # cached


def test_missing_zip_falls_back(tmp_path):
    assert CountryCities(tmp_path).lookup("Kupiansk", "", "UA") is None


def test_extraction_geocoder_prefers_cities1000(tmp_path):
    class Empty:
        def match(self, text, language, country_hint=""):
            return []

    geo = gazetteer_geocoder(Empty(), CountryCities(_write(tmp_path)))
    assert geo("Shakhtarske", "", "UA") == (47.96, 36.0, "UA", "city")
    assert geo("Nowhere", "", "UA") is None
