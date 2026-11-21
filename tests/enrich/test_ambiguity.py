"""Common-word false positives (e.g. Arabic "كيف" = "how" is a Kyiv alternate name)."""

import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from osint.enrich.ambiguity import CommonTokens, build_common_tokens, load_seed
from osint.enrich.gazetteer import Gazetteer, best_place
from tests import gazetteer_fixture as fx

EXTRA_CITIES = [
    (2747373, "The Hague", "The Hague", "Den Haag,لاهاي", 52.08, 4.30, "PPLG", "NL", "11",
     474292),
    (2639577, "Reading", "Reading", "", 51.46, -0.97, "PPL", "GB", "ENG", 318014),
    (4407066, "Reading", "Reading", "", 40.34, -75.93, "PPL", "US", "PA", 88082),
    (2972191, "Tours", "Tours", "", 47.39, 0.69, "PPLA4", "FR", "24", 141621),
    (3000001, "Aleppo", "Aleppo", "حلب", 36.20, 37.16, "PPLA", "SY", "09", 1602264),
    (3000002, "Springfield", "Springfield", "", 39.80, -89.64, "PPLA", "US", "IL", 116250),
    (3000003, "Springfield", "Springfield", "", 37.21, -93.29, "PPL", "GB", "ENG", 120000),
]
EXTRA_COUNTRIES = [("NL", "NLD", "Netherlands"), ("GB", "GBR", "United Kingdom"),
                   ("SY", "SYR", "Syria")]


def _write(dest, cities, countries, admin1):
    dest.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join([str(gid), name, ascii_, alts, str(lat), str(lon), "P", fcode, cc,
                        "", a1, "", "", "", str(pop), "", "100", "Europe/X", "2026-01-01"])
             for gid, name, ascii_, alts, lat, lon, fcode, cc, a1, pop in cities]
    with zipfile.ZipFile(dest / "cities15000.zip", "w") as z:
        z.writestr("cities15000.txt", "\n".join(lines) + "\n")
    info = ["#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital"]
    info += [f"{cc}\t{iso3}\t000\t{cc}\t{name}\tX" for cc, iso3, name in countries]
    (dest / "countryInfo.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
    (dest / "admin1CodesASCII.txt").write_text(
        "\n".join(f"{code}\t{name}\t{name}\t1" for code, name in admin1) + "\n",
        encoding="utf-8")
    return dest


def _cities():
    out = []
    for row in fx.CITIES:
        if row[1] == "Kyiv":  # GeoNames really lists the Arabic word for "how"
            row = (*row[:3], row[3] + ",كيف", *row[4:])
        out.append(row)
    return out + EXTRA_CITIES


@pytest.fixture(scope="module")
def gz(tmp_path_factory):
    src = _write(tmp_path_factory.mktemp("gaz"), _cities(), fx.COUNTRIES + EXTRA_COUNTRIES,
                 fx.ADMIN1)
    return Gazetteer.load(src)


def _best(gz, text, lang=""):
    b = best_place(gz.match(text, lang))
    return b.name if b is not None else None


def test_seed_is_small_and_has_known_cases():
    seed = load_seed()
    assert sum(len(v) for v in seed.values()) <= 30
    assert "كيف" in seed["ar"] and "nice" in seed["en"] and "tours" in seed["fr"]


def test_arabic_how_is_not_kyiv(gz):
    assert _best(gz, "كيف ستتعامل المحكمة مع القضية؟", "ar") is None
    assert _best(gz, "كيف ستتعامل لاهاي مع القضية؟", "ar") == "The Hague"
    assert "Kyiv" not in {h.name for h in gz.match("كيف ستتعامل لاهاي مع القضية؟", "ar")}


@pytest.mark.parametrize(("text", "lang"), [
    ("قصف روسي على كييف", "ar"), ("Масована атака дронів на Київ", "uk"),
    ("Взрывы в Киеве ночью", "ru"), ("Drone attack on Kyiv overnight", "en"),
])
def test_proper_kyiv_spellings_still_match(gz, text, lang):
    assert _best(gz, text, lang) == "Kyiv"


def test_english_common_words(gz):
    assert _best(gz, "Reading is fun", "en") is None
    assert _best(gz, "Fans in Reading, England celebrate", "en") == "Reading"
    assert best_place(gz.match("Fans in Reading, England", "en")).country == "GB"
    assert _best(gz, "nice weather today", "en") is None
    assert _best(gz, "Attack in Nice, France", "en") == "Nice"
    assert _best(gz, "We had a Nice time", "en") is None  # no French co-mention


def test_french_common_word_needs_country(gz):
    assert _best(gz, "Tours guidés gratuits", "fr") is None
    assert _best(gz, "Des Tours guidés gratuits", "fr") is None
    assert _best(gz, "Manifestation à Tours, en France", "fr") == "Tours"


def test_short_non_latin_city_names_survive(gz):
    assert _best(gz, "معارك في حلب", "ar") == "Aleppo"


def test_corpus_common_token_ignored_in_non_latin_script(gz):
    common = CommonTokens.seed().merged(CommonTokens({"ar": frozenset({"لاهاي"})}))
    g2 = Gazetteer(gz.places, gz.names, common=common)
    assert _best(g2, "كيف ستتعامل لاهاي مع القضية؟", "ar") is None


def test_ambiguous_city_without_country_is_unlocated(gz):
    assert _best(gz, "Tornado hits Springfield", "en") is None
    assert _best(gz, "Tornado hits Springfield, United States", "en") == "Springfield"


class _FakeLake:
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, params=None, *, start=None, end=None):
        return self.rows


def test_build_common_tokens_from_corpus(gz, tmp_path):
    now = datetime.now(UTC)
    rows = []
    for i in range(100):
        # "كيف" is a frequent word unrelated to Ukraine; "كييف" co-occurs with Ukraine
        rows.append(("ar", f"كيف نفهم الخبر رقم {i}", "تحليل", now - timedelta(minutes=i)))
        if i % 4 == 0:
            rows.append(("ar", "قصف على كييف", "أوكرانيا تقول", now))
        rows.append(("en", f"A nice day {i}", "Readers enjoy it", now))
        if i % 3 == 0:
            rows.append(("en", "Strike on Kyiv", "Ukraine says", now))
    out = tmp_path / "common_tokens.json"
    build_common_tokens(_FakeLake(rows), out, gazetteer=gz)
    data = json.loads(out.read_text(encoding="utf-8"))
    ar, en = set(data["languages"]["ar"]["tokens"]), set(data["languages"]["en"]["tokens"])
    assert "كيف" in ar and "كييف" not in ar
    assert "nice" in en and "kyiv" not in en
    common = CommonTokens.load(out)
    assert common.is_common("كيف", "ar") and not common.is_common("كييف", "ar")
