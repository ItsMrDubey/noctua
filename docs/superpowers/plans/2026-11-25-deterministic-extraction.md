# Deterministic Extraction & Country Normalisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every event's country, actor countries, headline, summary, casualties, weapons/platforms and lifecycle current from deterministic rules, without the LLM (spec `docs/superpowers/specs/2026-11-25-deterministic-extraction-design.md`).

**Architecture:** A static country registry (`osint/core/countries.py` over a generated, committed `config/countries.yaml`) normalises every write path to ISO2. An actor lexicon (`config/actors.yaml` + terror-group homes + phrases generated per country) resolves actor countries; pure rule extractors (`osint/enrich/rules/`) produce headline / summary / casualties / weapons as `event_fields` rows with `source='rule'`. The EventBuilder refreshes `events.lifecycle` and runs a CPU-only `RulePass` over the events each pass touched; a one-off CLI migrates existing rows under the event-rebuild lock; the event-detail API merges rule and LLM values with `field_source`.

**Tech Stack:** Python 3.12, SQLite (`osint.store.sqlite`), PyYAML, numpy, pytest (`uv run pytest`), ruff (line length 100).

## Global Constraints

- No network access at runtime: `config/countries.yaml` is "generated once from public ISO/FIPS/CAMEO tables and committed".
- The LLM extraction job (`pipeline/extract/job.py`) stays an optional upgrade: its hourly/daily caps and the queue's `priority_key` are unchanged; no change to the LLM prompt or schema.
- Rule extraction and actor resolution are CPU-only and target < 5 ms per event; they run "after the pass commits ... in the same worker".
- Matching folds case and diacritics with `enrich.gazetteer.fold` (via `gazetteer.tokens` / `name_key`) and uses whole-word matches; the longest match wins.
- "If the lexicon fails to load at startup, the error is fatal" (`LexiconError`, never a silent empty lexicon).
- Values that don't resolve become `null`, are logged once per distinct value per pass at debug level, and are counted as `unresolved_country` (EventBuilder `PassStats`, shown in the worker status snapshot).
- "A rule extractor that raises an exception is skipped for that event and the error is logged."
- Schema changes go in `store/sqlite.py` as idempotent `ALTER TABLE`s; index on `events(country)`.
- Lifecycle thresholds live in `config/events.yaml` under `lifecycle:` (6 h + ≥ 2 documents / 48 h / 7 d).
- Summary ≤ 280 characters, cut at a sentence boundary.
- Fixed interfaces consumed by sub-project B (keep the names exactly): `osint/core/countries.py` `to_iso2(value, hint) -> str | None`, `name(iso2) -> str`, `demonym_index() -> dict[str, str]`, `bbox(iso2) -> tuple[float, float, float, float] | None`; events columns `country` (ISO2), `actor_countries` (JSON list of ISO2), `foreign_involved` (int 0/1), `attributed` (int 0/1), `lifecycle` (TEXT: `developing|active|cooling|closed`), index on `events(country)`; actor dicts carry `country` and `source`. `bbox` is computed from the Natural Earth polygons already loaded by `backend/osint/signals/countries.py` (lazily, cached), not from a hand-made table.
- No UI changes. Tests never reach the network. `pytest` runs with `filterwarnings = error`.
- Quote ISO2 codes in hand-written YAML (`"NO"` is a YAML boolean otherwise).

## File map

| File | Responsibility |
|---|---|
| `scripts/gen_countries.py` (new) | One-off generator of `config/countries.yaml` from GeoNames `countries.tsv` (committed) and embedded CAMEO / alias / demonym tables |
| `config/countries.yaml` (new, generated) | ISO2/ISO3/FIPS/CAMEO/name/aliases/demonyms for all 252 GeoNames countries |
| `backend/osint/core/countries.py` (new) | Registry API: `to_iso2`, `name`, `demonym_index`, `bbox` / `centroid` (lazily from the Natural Earth polygons of `osint.signals.countries`), `get`, `all_countries`, `drain_unresolved` |
| `backend/osint/pipeline/cluster.py`, `pipeline/triage.py`, `collectors/acled.py`, `backfill/ucdp.py`, `pipeline/extract/verdict.py`, `intel/context.py` | Write-path normalisation to ISO2 |
| `backend/osint/store/sqlite.py` | New columns / index / `event_fields.source`, derived-column and rule-field store methods, migration helpers |
| `backend/osint/enrich/phrases.py` (new) | Whole-word longest-match phrase matcher on folded tokens |
| `backend/osint/enrich/actors.py` (new) + `config/actors.yaml` (new) | Actor lexicon, `resolve_actors`, `derive` |
| `backend/osint/enrich/rules/` (new package) + `config/extract_rules.yaml` (new) | `FieldValue`, `ExtractDoc`, headline/summary, weapons, casualties, `RuleSet` |
| `backend/osint/pipeline/lifecycle.py` (new) | `LifecycleConfig`, `lifecycle_state`, `refresh_lifecycle` |
| `backend/osint/pipeline/rule_pass.py` (new) | `RulePass`: actor resolution + derived columns + rule fields for a set of events |
| `backend/osint/pipeline/events.py`, `config/events.yaml` | Lifecycle config, per-pass lifecycle refresh, RulePass trigger, `unresolved_country` |
| `backend/osint/pipeline/migrate_countries.py` (new) | `python -m osint.pipeline.migrate_countries [--dry-run]` |
| `backend/osint/api/events.py` | `field_precedence`, `extracted` + `field_source` on event detail |

---

### Task 1: Country registry

**Files:**
- Create: `scripts/gen_countries.py`
- Create: `config/countries.yaml` (generated by the script, committed)
- Create: `backend/osint/core/countries.py`
- Test: `tests/core/test_countries.py`

**Interfaces:**
- Consumes: `osint.enrich.gazetteer.name_key(name: str) -> str`; `osint.terror.countries.ALIASES: dict[str, list[str]]`, `FIPS_EXTRA: dict[str, str]`; `osint.config.ROOT`; `osint.signals.countries.get_countries() -> Countries` (process-wide, lazily loaded from `data/reference/ne_50m_admin_0_countries.geojson`; empty when the file is missing) with `Countries.shape(cc) -> list[list[list[float]]]` (polygons -> rings -> flat `[lon, lat, lon, lat, ...]` rounded to 2 decimals, `[]` when unknown) and `Countries.point(cc) -> tuple[float, float, str] | None` (`(lat, lon, name)`); `Countries(path)` reads ISO_A2 (else ISO_A2_EH) and LABEL_X / LABEL_Y.
- Produces (module `osint.core.countries`):
  - `Hint = Literal["fips", "cameo", "iso2", "iso3", "name", "auto"]`
  - `@dataclass(frozen=True, slots=True) class Country: iso2: str; iso3: str; fips: tuple[str, ...]; cameo: tuple[str, ...]; name: str; aliases: tuple[str, ...]; demonyms: tuple[str, ...]`
  - `class Registry` with `Registry.load(path: Path = DEFAULT_PATH) -> Registry`
  - `registry() -> Registry` (lru-cached)
  - `to_iso2(value: str | None, hint: Hint) -> str | None` (raises `ValueError` for an unknown hint)
  - `get(iso2: str | None) -> Country | None` (no unresolved counting)
  - `all_countries() -> list[Country]`
  - `name(iso2: str | None) -> str` (unknown code -> the code itself; None -> "")
  - `demonym_index() -> dict[str, str]` (name_key(demonym) -> ISO2, a copy)
  - `bbox(iso2: str | None) -> tuple[float, float, float, float] | None` — `(west, south, east, north)` of the main territory, computed lazily from the `osint.signals.countries` polygons and cached per polygon set; west > east crosses the antimeridian; None for an unknown code or no polygon
  - `main_bbox(polys: list[list[list[float]]]) -> tuple[float, float, float, float] | None` (pure helper)
  - `centroid(iso2: str | None) -> tuple[float, float] | None` — `(lat, lon)` Natural Earth label point
  - `drain_unresolved() -> dict[str, int]` — `{"<hint>:<value>": count}` since the last drain

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_countries.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_countries.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.core.countries'`

- [ ] **Step 3: Write the generator script**

Create `scripts/gen_countries.py`:

```python
"""Generate config/countries.yaml for osint.core.countries.

    uv run python scripts/gen_countries.py [--tsv PATH] [--out PATH]

Sources (no network access):
* backend/osint/terror/data/countries.tsv -- GeoNames countryInfo.txt (CC BY 4.0): ISO 3166-1
  alpha-2 / alpha-3, FIPS 10-4 and the English short name (committed in the repo);
* osint.terror.countries.ALIASES / FIPS_EXTRA -- UCDP, State Department and news spellings;
* the tables below -- CAMEO country codes that are not ISO alpha-3, CAMEO region and
  actor-type codes (no country), more aliases, and English demonyms.

Re-run only when a table changes and commit the output; the runtime never reads the sources.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from osint.enrich.gazetteer import name_key
from osint.terror.countries import ALIASES as TERROR_ALIASES
from osint.terror.countries import FIPS_EXTRA as TERROR_FIPS_EXTRA

ROOT = Path(__file__).resolve().parents[1]

HEADER = """\
# GENERATED by scripts/gen_countries.py -- do not edit by hand: change the script's tables
# and re-run it. Sources: GeoNames countryInfo (CC BY 4.0), CAMEO codebook country codes.
# Read by osint.core.countries. fips / cameo: every code that maps to the country.
# (Bounding boxes come from the Natural Earth polygons in osint.signals.countries.)
"""

# FIPS 10-4 codes GDELT / the State Department use beyond countryInfo's one per country
FIPS_EXTRA = {"GZ": "PS"}  # Gaza Strip (West Bank "WE" is countryInfo's code for PS)

# CAMEO country codes are ISO alpha-3 except these (CAMEO codebook, GDELT CAMEO.country.txt)
CAMEO_EXTRA = {
    "RO": ["ROM"], "XK": ["KOS"], "PS": ["WSB", "GZS"], "TL": ["TMP"], "CD": ["ZAR"],
    "RS": ["YUG"], "MM": ["BUR"],
}

# CAMEO codes that are regions or actor types, never a country (to_iso2 -> None, not
# counted as unresolved)
CAMEO_NON_STATE = [
    # regions
    "AFR", "ASA", "BLK", "CAS", "CAU", "CEU", "CFR", "CRB", "EAF", "EEU", "EIN", "EUR", "LAM",
    "MDT", "MEA", "NAF", "NMR", "PGS", "SAF", "SAM", "SAS", "SCN", "SEA", "WAF", "WST",
    # actor types without a country prefix
    "AGR", "AMN", "BUS", "COP", "CRM", "CVL", "EDU", "ELI", "ENV", "GOV", "HLH", "IGO", "IMG",
    "INS", "IRC", "JUD", "LAB", "LEG", "MED", "MIL", "MNC", "MOD", "NGO", "OPP", "RAD", "REB",
    "REF", "SEP", "SPY", "UIS", "UNO",
]

# spellings beyond countryInfo's name and osint.terror.countries.ALIASES (ACLED, UCDP, UN)
EXTRA_ALIASES = {
    "AE": ["UAE", "U.A.E.", "Emirates"],
    "AX": ["Åland", "Åland Islands"],
    "BL": ["Saint-Barthelemy", "St Barthelemy"],
    "BN": ["Brunei Darussalam"],
    "BO": ["Plurinational State of Bolivia"],
    "BQ": ["Caribbean Netherlands", "Bonaire, Sint Eustatius and Saba", "Bonaire"],
    "CN": ["People's Republic of China", "PRC", "Mainland China"],
    "CW": ["Curaçao"],
    "FK": ["Falkland Islands (Malvinas)", "Malvinas"],
    "FM": ["Federated States of Micronesia"],
    "IR": ["Islamic Republic of Iran"],
    "KN": ["St Kitts and Nevis"],
    "LA": ["Lao PDR", "Lao People's Democratic Republic"],
    "LC": ["St Lucia"],
    "MD": ["Republic of Moldova"],
    "MF": ["Saint-Martin", "Saint Martin (French part)"],
    "MK": ["Former Yugoslav Republic of Macedonia"],
    "NL": ["Netherlands", "Holland", "Kingdom of the Netherlands"],
    "PN": ["Pitcairn Islands"],
    "PS": ["State of Palestine", "Occupied Palestinian Territory",
           "Occupied Palestinian Territories"],
    "RE": ["Réunion"],
    "SA": ["KSA", "Kingdom of Saudi Arabia"],
    "SH": ["Saint Helena, Ascension and Tristan da Cunha"],
    "ST": ["São Tomé and Príncipe"],
    "SX": ["Sint Maarten (Dutch part)"],
    "SZ": ["Kingdom of eSwatini"],
    "TW": ["Chinese Taipei"],
    "TZ": ["United Republic of Tanzania"],
    "VC": ["St Vincent and the Grenadines"],
    "VE": ["Bolivarian Republic of Venezuela"],
    "VG": ["Virgin Islands, British"],
    "VI": ["Virgin Islands, U.S.", "US Virgin Islands", "United States Virgin Islands"],
    "VN": ["Viet Nam"],
}

# English demonyms (adjectival forms). Each demonym belongs to one country only: the
# ambiguous "Congolese" / "Dominican" / "Guinean" / "Samoan" are given to the country they
# usually mean in news (CD, DO, GN, WS); uninhabited territories have none.
DEMONYMS = {
    "AD": ["Andorran"], "AE": ["Emirati"], "AF": ["Afghan"], "AG": ["Antiguan", "Barbudan"],
    "AI": ["Anguillan"], "AL": ["Albanian"], "AM": ["Armenian"],
    "AN": ["Netherlands Antillean"], "AO": ["Angolan"], "AQ": [],
    "AR": ["Argentine", "Argentinian"], "AS": ["American Samoan"], "AT": ["Austrian"],
    "AU": ["Australian"], "AW": ["Aruban"], "AX": ["Alandish"], "AZ": ["Azerbaijani", "Azeri"],
    "BA": ["Bosnian", "Herzegovinian"], "BB": ["Barbadian", "Bajan"], "BD": ["Bangladeshi"],
    "BE": ["Belgian"], "BF": ["Burkinabe"], "BG": ["Bulgarian"], "BH": ["Bahraini"],
    "BI": ["Burundian"], "BJ": ["Beninese"], "BL": ["Barthelemois"], "BM": ["Bermudian"],
    "BN": ["Bruneian"], "BO": ["Bolivian"], "BQ": ["Bonairean"], "BR": ["Brazilian"],
    "BS": ["Bahamian"], "BT": ["Bhutanese"], "BV": [], "BW": ["Botswanan", "Batswana"],
    "BY": ["Belarusian", "Belarussian"], "BZ": ["Belizean"],
    "CA": ["Canadian"], "CC": ["Cocos Islander"], "CD": ["Congolese"],
    "CF": ["Central African"], "CG": [], "CH": ["Swiss"], "CI": ["Ivorian"],
    "CK": ["Cook Islander"], "CL": ["Chilean"], "CM": ["Cameroonian"], "CN": ["Chinese"],
    "CO": ["Colombian"], "CR": ["Costa Rican"], "CS": [], "CU": ["Cuban"],
    "CV": ["Cape Verdean", "Cabo Verdean"], "CW": ["Curacaoan"], "CX": ["Christmas Islander"],
    "CY": ["Cypriot"], "CZ": ["Czech"],
    "DE": ["German"], "DJ": ["Djiboutian"], "DK": ["Danish"], "DM": [], "DO": ["Dominican"],
    "DZ": ["Algerian"],
    "EC": ["Ecuadorian"], "EE": ["Estonian"], "EG": ["Egyptian"], "EH": ["Sahrawi", "Sahrawian"],
    "ER": ["Eritrean"], "ES": ["Spanish"], "ET": ["Ethiopian"],
    "FI": ["Finnish"], "FJ": ["Fijian"], "FK": ["Falkland Islander"], "FM": ["Micronesian"],
    "FO": ["Faroese"], "FR": ["French"],
    "GA": ["Gabonese"], "GB": ["British"], "GD": ["Grenadian"], "GE": ["Georgian"],
    "GF": ["French Guianese"], "GG": [], "GH": ["Ghanaian"], "GI": ["Gibraltarian"],
    "GL": ["Greenlandic"], "GM": ["Gambian"], "GN": ["Guinean"], "GP": ["Guadeloupean"],
    "GQ": ["Equatorial Guinean", "Equatoguinean"], "GR": ["Greek", "Hellenic"], "GS": [],
    "GT": ["Guatemalan"], "GU": ["Guamanian"], "GW": ["Bissau-Guinean"], "GY": ["Guyanese"],
    "HK": ["Hong Konger", "Hongkonger"], "HM": [], "HN": ["Honduran"], "HR": ["Croatian"],
    "HT": ["Haitian"], "HU": ["Hungarian"],
    "ID": ["Indonesian"], "IE": ["Irish"], "IL": ["Israeli"], "IM": ["Manx"], "IN": ["Indian"],
    "IO": [], "IQ": ["Iraqi"], "IR": ["Iranian"], "IS": ["Icelandic"], "IT": ["Italian"],
    "JE": [], "JM": ["Jamaican"], "JO": ["Jordanian"], "JP": ["Japanese"],
    "KE": ["Kenyan"], "KG": ["Kyrgyz", "Kyrgyzstani"], "KH": ["Cambodian", "Khmer"],
    "KI": ["I-Kiribati"], "KM": ["Comoran", "Comorian"], "KN": ["Kittitian", "Nevisian"],
    "KP": ["North Korean"], "KR": ["South Korean"], "KW": ["Kuwaiti"], "KY": ["Caymanian"],
    "KZ": ["Kazakh", "Kazakhstani"],
    "LA": ["Lao", "Laotian"], "LB": ["Lebanese"], "LC": ["Saint Lucian"],
    "LI": ["Liechtensteiner"], "LK": ["Sri Lankan"], "LR": ["Liberian"],
    "LS": ["Basotho", "Mosotho"], "LT": ["Lithuanian"], "LU": ["Luxembourgish", "Luxembourger"],
    "LV": ["Latvian"], "LY": ["Libyan"],
    "MA": ["Moroccan"], "MC": ["Monegasque", "Monacan"], "MD": ["Moldovan"],
    "ME": ["Montenegrin"], "MF": ["Saint-Martinoise"], "MG": ["Malagasy"],
    "MH": ["Marshallese"], "MK": ["North Macedonian", "Macedonian"], "ML": ["Malian"],
    "MM": ["Burmese"], "MN": ["Mongolian"], "MO": ["Macanese"], "MP": ["Northern Marianan"],
    "MQ": ["Martinican", "Martiniquais"], "MR": ["Mauritanian"], "MS": ["Montserratian"],
    "MT": ["Maltese"], "MU": ["Mauritian"], "MV": ["Maldivian"], "MW": ["Malawian"],
    "MX": ["Mexican"], "MY": ["Malaysian"], "MZ": ["Mozambican"],
    "NA": ["Namibian"], "NC": ["New Caledonian"], "NE": ["Nigerien"], "NF": ["Norfolk Islander"],
    "NG": ["Nigerian"], "NI": ["Nicaraguan"], "NL": ["Dutch"], "NO": ["Norwegian"],
    "NP": ["Nepali", "Nepalese"], "NR": ["Nauruan"], "NU": ["Niuean"], "NZ": ["New Zealander"],
    "OM": ["Omani"],
    "PA": ["Panamanian"], "PE": ["Peruvian"], "PF": ["French Polynesian"],
    "PG": ["Papua New Guinean"], "PH": ["Filipino", "Philippine"], "PK": ["Pakistani"],
    "PL": ["Polish"], "PM": ["Saint-Pierrais"], "PN": ["Pitcairn Islander"],
    "PR": ["Puerto Rican"], "PS": ["Palestinian"], "PT": ["Portuguese"], "PW": ["Palauan"],
    "PY": ["Paraguayan"],
    "QA": ["Qatari"],
    "RE": ["Reunionese"], "RO": ["Romanian"], "RS": ["Serbian"], "RU": ["Russian"],
    "RW": ["Rwandan"],
    "SA": ["Saudi", "Saudi Arabian"], "SB": ["Solomon Islander"], "SC": ["Seychellois"],
    "SD": ["Sudanese"], "SE": ["Swedish"], "SG": ["Singaporean"], "SH": ["Saint Helenian"],
    "SI": ["Slovenian", "Slovene"], "SJ": [], "SK": ["Slovak"], "SL": ["Sierra Leonean"],
    "SM": ["Sammarinese"], "SN": ["Senegalese"], "SO": ["Somali"], "SR": ["Surinamese"],
    "SS": ["South Sudanese"], "ST": ["Sao Tomean"], "SV": ["Salvadoran"],
    "SX": ["Sint Maartener"], "SY": ["Syrian"], "SZ": ["Swazi", "Emaswati"],
    "TC": ["Turks and Caicos Islander"], "TD": ["Chadian"], "TF": [], "TG": ["Togolese"],
    "TH": ["Thai"], "TJ": ["Tajik", "Tajikistani"], "TK": ["Tokelauan"], "TL": ["Timorese"],
    "TM": ["Turkmen"], "TN": ["Tunisian"], "TO": ["Tongan"], "TR": ["Turkish"],
    "TT": ["Trinidadian", "Tobagonian"], "TV": ["Tuvaluan"], "TW": ["Taiwanese"],
    "TZ": ["Tanzanian"],
    "UA": ["Ukrainian"], "UG": ["Ugandan"], "UM": [], "US": ["American"], "UY": ["Uruguayan"],
    "UZ": ["Uzbek", "Uzbekistani"],
    "VA": [], "VC": ["Vincentian"], "VE": ["Venezuelan"], "VG": ["British Virgin Islander"],
    "VI": ["U.S. Virgin Islander"], "VN": ["Vietnamese"], "VU": ["Ni-Vanuatu"],
    "WF": ["Wallisian", "Futunan"], "WS": ["Samoan"],
    "XK": ["Kosovar", "Kosovan"],
    "YE": ["Yemeni"], "YT": ["Mahoran"],
    "ZA": ["South African"], "ZM": ["Zambian"], "ZW": ["Zimbabwean"],
}


def read_tsv(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("\t")]
        rows.append({"iso2": p[0], "iso3": p[1], "fips": p[2], "name": p[3]})
    return rows


def _unique(pairs: list[tuple[str, str]], what: str) -> None:
    seen: dict[str, str] = {}
    for key, iso2 in pairs:
        if key in seen and seen[key] != iso2:
            raise SystemExit(f"{what} {key!r} maps to both {seen[key]} and {iso2}")
        seen[key] = iso2


def build(rows: list[dict[str, str]]) -> dict:
    codes = {r["iso2"] for r in rows}
    if codes != set(DEMONYMS):
        raise SystemExit(f"DEMONYMS mismatch: missing {sorted(codes - set(DEMONYMS))}, "
                         f"unknown {sorted(set(DEMONYMS) - codes)}")
    fips_extra: dict[str, list[str]] = {}
    for code, iso2 in {**TERROR_FIPS_EXTRA, **FIPS_EXTRA}.items():
        fips_extra.setdefault(iso2, []).append(code)
    aliases: dict[str, list[str]] = {k: list(v) for k, v in TERROR_ALIASES.items()}
    for k, v in EXTRA_ALIASES.items():
        aliases.setdefault(k, []).extend(v)
    for table, what in ((aliases, "alias"), (CAMEO_EXTRA, "CAMEO"), (fips_extra, "FIPS")):
        unknown = set(table) - codes
        if unknown:
            raise SystemExit(f"{what} table names unknown ISO2 codes {sorted(unknown)}")
    out = []
    for r in sorted(rows, key=lambda r: r["iso2"]):
        iso2 = r["iso2"]
        own = name_key(r["name"])
        out.append({
            "iso2": iso2, "iso3": r["iso3"],
            "fips": list(dict.fromkeys(f for f in [r["fips"], *fips_extra.get(iso2, [])] if f)),
            "cameo": list(dict.fromkeys([r["iso3"], *CAMEO_EXTRA.get(iso2, [])])),
            "name": r["name"],
            "aliases": [a for a in dict.fromkeys(aliases.get(iso2, [])) if name_key(a) != own],
            "demonyms": DEMONYMS[iso2],
        })
    _unique([(f, c["iso2"]) for c in out for f in c["fips"]], "FIPS code")
    _unique([(k, c["iso2"]) for c in out for k in c["cameo"]], "CAMEO code")
    _unique([(name_key(n), c["iso2"]) for c in out for n in (c["name"], *c["aliases"])],
            "country name")
    _unique([(name_key(d), c["iso2"]) for c in out for d in c["demonyms"]], "demonym")
    clash = set(CAMEO_NON_STATE) & {k for c in out for k in c["cameo"]}
    if clash:
        raise SystemExit(f"non-state CAMEO codes collide with country codes: {sorted(clash)}")
    return {"cameo_non_state": sorted(CAMEO_NON_STATE), "countries": out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tsv", type=Path,
                    default=ROOT / "backend" / "osint" / "terror" / "data" / "countries.tsv")
    ap.add_argument("--out", type=Path, default=ROOT / "config" / "countries.yaml")
    args = ap.parse_args(argv)
    doc = build(read_tsv(args.tsv))
    args.out.write_text(HEADER + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                                width=100, default_flow_style=None),
                        encoding="utf-8")
    print(f"wrote {args.out} ({len(doc['countries'])} countries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Generate `config/countries.yaml`**

Run: `uv run python scripts/gen_countries.py`
Expected: `wrote ...\config\countries.yaml (252 countries)`

Check the head of the file:

Run: `head -20 config/countries.yaml`
Expected (first entries):
```yaml
# GENERATED by scripts/gen_countries.py -- do not edit by hand: ...
cameo_non_state: [AFR, AGR, AMN, ...]
countries:
- iso2: AD
  iso3: AND
  fips: [AN]
  cameo: [AND]
  name: Andorra
  aliases: []
  demonyms: [Andorran]
```
and `grep -n "iso2: 'NO'" config/countries.yaml` prints one line (PyYAML quotes `'NO'`).

- [ ] **Step 5: Write the registry module**

Create `backend/osint/core/countries.py`:

```python
"""Country registry (deterministic extraction spec §2.1).

ISO 3166-1 alpha-2 <-> alpha-3 <-> FIPS 10-4 <-> CAMEO country code <-> English short name,
plus aliases and demonyms per country. Data: ``config/countries.yaml``, generated by
``scripts/gen_countries.py`` from GeoNames countryInfo and committed; nothing is fetched at
runtime. Bounding boxes and label points come lazily from the Natural Earth polygons that
``osint.signals.countries`` already loads for the IODA overlays (``bbox`` / ``centroid``).

Callers always say which code system a value is in (``hint``): GDELT ActionGeo codes are
FIPS 10-4 (``GM`` = Germany, ``UK`` = United Kingdom), GDELT actor country codes are CAMEO
(mostly ISO alpha-3), ACLED / UCDP give names. ``auto`` reads two letters as ISO2, never as
FIPS. Values that do not resolve give None and are counted (``drain_unresolved``).
"""

from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from osint.config import ROOT
from osint.enrich.gazetteer import name_key

log = logging.getLogger(__name__)

DEFAULT_PATH = ROOT / "config" / "countries.yaml"
Hint = Literal["fips", "cameo", "iso2", "iso3", "name", "auto"]
HINTS = frozenset(("fips", "cameo", "iso2", "iso3", "name", "auto"))
MAIN_KM = 1500.0  # polygons farther than this from the main territory leave the bbox
_PAREN = re.compile(r"\(([^)]*)\)")


@dataclass(frozen=True, slots=True)
class Country:
    iso2: str
    iso3: str
    fips: tuple[str, ...]
    cameo: tuple[str, ...]
    name: str
    aliases: tuple[str, ...]
    demonyms: tuple[str, ...]


class Registry:
    """Lookup tables over the country rows."""

    def __init__(self, rows: list[Country], cameo_non_state: list[str]) -> None:
        self.rows = rows
        self.by_iso2 = {c.iso2: c for c in rows}
        self.iso3 = {c.iso3: c.iso2 for c in rows if c.iso3}
        self.fips = {f: c.iso2 for c in rows for f in c.fips}
        self.cameo = {k: c.iso2 for c in rows for k in c.cameo}
        self.non_state = frozenset(cameo_non_state)
        self.names: dict[str, str] = {}
        self.demonyms: dict[str, str] = {}
        for c in rows:
            for n in (c.name, *c.aliases):
                if k := name_key(n):
                    self.names.setdefault(k, c.iso2)
            for d in c.demonyms:
                if k := name_key(d):
                    self.demonyms.setdefault(k, c.iso2)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> Registry:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        rows = []
        for r in data.get("countries") or []:
            rows.append(Country(
                iso2=str(r["iso2"]), iso3=str(r.get("iso3") or ""),
                fips=tuple(str(x) for x in r.get("fips") or ()),
                cameo=tuple(str(x) for x in r.get("cameo") or ()),
                name=str(r["name"]), aliases=tuple(str(x) for x in r.get("aliases") or ()),
                demonyms=tuple(str(x) for x in r.get("demonyms") or ())))
        if not rows:
            raise ValueError(f"country registry {path} has no countries")
        return cls(rows, [str(x) for x in data.get("cameo_non_state") or ()])


@lru_cache(maxsize=1)
def registry() -> Registry:
    return Registry.load()


_unresolved_lock = threading.Lock()
_unresolved: dict[str, int] = {}


def _note_unresolved(value: str, hint: str) -> None:
    key = f"{hint}:{value}"
    with _unresolved_lock:
        first = key not in _unresolved
        _unresolved[key] = _unresolved.get(key, 0) + 1
    if first:  # once per distinct value between two drains (one EventBuilder pass)
        log.debug("country value %r (%s) did not resolve", value, hint)


def drain_unresolved() -> dict[str, int]:
    """``{"<hint>:<value>": count}`` of the values that did not resolve since the last
    drain, and reset (the EventBuilder drains once per pass: ``unresolved_country``)."""
    with _unresolved_lock:
        out = dict(_unresolved)
        _unresolved.clear()
    return out


def _by_name(reg: Registry, raw: str) -> str | None:
    key = name_key(raw)
    hit = reg.names.get(key)
    if hit is None and _PAREN.search(raw):
        # UCDP "Russia (Soviet Union)", "Zimbabwe (Rhodesia)": the base name, else the
        # bracketed one
        hit = reg.names.get(name_key(_PAREN.sub(" ", raw)))
        for inner in _PAREN.findall(raw):
            hit = hit or reg.names.get(name_key(inner))
    if hit is None and key.startswith("the "):
        hit = reg.names.get(key[4:])
    return hit


def to_iso2(value: str | None, hint: Hint) -> str | None:
    """ISO2 code of ``value`` read as ``hint``, or None (empty, unknown, or a CAMEO region /
    actor-type code such as ``IGO`` or ``AFR``).

    * ``iso2``: a registry ISO2 code (case-insensitive);
    * ``fips``: FIPS 10-4 (GDELT ActionGeo: ``GM`` -> DE, ``UK`` -> GB, ``RS`` -> RU);
    * ``cameo``: a CAMEO country code (``RUS``, ``ROM``, ``WSB``) or a full CAMEO actor
      code, read by its 3-letter prefix (``RUSMIL`` -> RU, ``IGOUNO`` -> None);
    * ``iso3``: ISO alpha-3;
    * ``name``: English name or alias, case/diacritic/punctuation-insensitive; a UCDP
      bracket ("Russia (Soviet Union)") falls back to the base name, then the bracketed one;
    * ``auto``: two letters ISO2 (never FIPS), three letters ISO3 then CAMEO, then a name.
    """
    if hint not in HINTS:
        raise ValueError(f"unknown country hint {hint!r}")
    raw = (value or "").strip()
    if not raw:
        return None
    reg = registry()
    code = raw.upper()
    out: str | None
    if hint == "iso2":
        out = code if code in reg.by_iso2 else None
    elif hint == "fips":
        out = reg.fips.get(code)
    elif hint == "iso3":
        out = reg.iso3.get(code)
    elif hint == "cameo":
        if code[:3] in reg.non_state:
            return None
        out = reg.cameo.get(code[:3]) if len(code) >= 3 else None
    elif hint == "name":
        out = _by_name(reg, raw)
    else:
        out = None
        if len(code) == 2 and code.isalpha():
            out = code if code in reg.by_iso2 else None
        elif len(code) == 3 and code.isalpha():
            out = reg.iso3.get(code) or reg.cameo.get(code)
        out = out or _by_name(reg, raw)
    if out is None:
        _note_unresolved(raw, hint)
    return out


def get(iso2: str | None) -> Country | None:
    """The registry row of an ISO2 code (no unresolved counting), or None."""
    return registry().by_iso2.get((iso2 or "").strip().upper())


def all_countries() -> list[Country]:
    return list(registry().rows)


def name(iso2: str | None) -> str:
    """English short name of ``iso2``; the code itself when unknown, "" for None/empty."""
    c = get(iso2)
    return c.name if c else (iso2 or "").strip().upper()


def demonym_index() -> dict[str, str]:
    """Demonym key -> ISO2, e.g. {"russian": "RU", "ukrainian": "UA"}. Keys are
    ``osint.enrich.gazetteer.name_key`` forms (case, diacritics and punctuation folded:
    "Bissau-Guinean" -> "bissau guinean"). A fresh copy per call."""
    return dict(registry().demonyms)


# ------------------------------------------------------- polygons (bbox, centroid)

Box = tuple[float, float, float, float]
_box_lock = threading.Lock()


def _ring_box(ring: list[float]) -> Box:
    lons, lats = ring[0::2], ring[1::2]
    return min(lons), min(lats), max(lons), max(lats)


def _lon_gap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Degrees between two longitude intervals on the circle (0 when they overlap)."""
    best = 360.0
    for shift in (-360.0, 0.0, 360.0):
        w, e = b[0] + shift, b[1] + shift
        if w <= a[1] and a[0] <= e:
            return 0.0
        best = min(best, abs(w - a[1]), abs(a[0] - e))
    return best


def _gap_km(a: Box, b: Box) -> float:
    dlat = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    mid = math.radians((max(a[1], b[1]) + min(a[3], b[3])) / 2)
    dlon = _lon_gap((a[0], a[2]), (b[0], b[2])) * max(0.0, math.cos(mid))
    return 111.2 * math.hypot(dlat, dlon)


def _area(b: Box) -> float:
    return (b[2] - b[0]) * (b[3] - b[1]) * max(0.01, math.cos(math.radians((b[1] + b[3]) / 2)))


def main_bbox(polys: list[list[list[float]]]) -> Box | None:
    """(west, south, east, north) of the polygons (``osint.signals.countries`` shape:
    polygons -> rings -> flat [lon, lat, ...]; outer ring first) within MAIN_KM of the
    largest one, transitively: France without French Guiana / Réunion, the US without
    Hawaii. west > east when the territory crosses the antimeridian (Russia, Fiji)."""
    boxes = [_ring_box(poly[0]) for poly in polys if poly and len(poly[0]) >= 2]
    if not boxes:
        return None
    seed = max(boxes, key=_area)
    kept = [seed]
    rest = [b for b in boxes if b is not seed]
    grew = True
    while grew:
        grew = False
        for b in list(rest):
            if any(_gap_km(b, k) <= MAIN_KM for k in kept):
                kept.append(b)
                rest.remove(b)
                grew = True
    w, e = min(b[0] for b in kept), max(b[2] for b in kept)
    if e - w > 180:
        sw = min(b[0] + 360 if b[0] < 0 else b[0] for b in kept)
        se = max(b[2] + 360 if b[2] < 0 else b[2] for b in kept)
        if se - sw < e - w:
            w, e = sw, (se - 360 if se > 180 else se)
    return (round(w, 4), round(min(b[1] for b in kept), 4), round(e, 4),
            round(max(b[3] for b in kept), 4))


def _polygons():
    """The process-wide Natural Earth country polygons (``osint.signals.countries``, loaded
    lazily on first use; empty when data/reference has no admin-0 file)."""
    from osint.signals import countries as sig  # local: reads the reference file lazily

    return sig.get_countries()


def bbox(iso2: str | None) -> Box | None:
    """(west, south, east, north) of the country's main territory from the Natural Earth
    polygons (``main_bbox``), or None (unknown code / no polygon). Cached on the polygon
    set itself, so a reloaded set never serves stale boxes."""
    code = (iso2 or "").strip().upper()
    if len(code) != 2:
        return None
    shapes = _polygons()
    with _box_lock:
        cache: dict[str, Box | None] = shapes.__dict__.setdefault("_core_bbox_cache", {})
        if code in cache:
            return cache[code]
    box = main_bbox(shapes.shape(code))
    with _box_lock:
        cache[code] = box
    return box


def centroid(iso2: str | None) -> tuple[float, float] | None:
    """(lat, lon) Natural Earth label point of the country, or None."""
    code = (iso2 or "").strip().upper()
    p = _polygons().point(code) if len(code) == 2 else None
    return (p[0], p[1]) if p else None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/core/test_countries.py -v`
Expected: all PASS.

Run: `uv run ruff check backend/osint/core/countries.py scripts/gen_countries.py tests/core/test_countries.py`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add scripts/gen_countries.py config/countries.yaml backend/osint/core/countries.py tests/core/test_countries.py
git commit -m "feat(countries): ISO2/FIPS/CAMEO/name country registry; bbox from Natural Earth polygons"
```

---

### Task 2: Write-path normalisation to ISO2

**Files:**
- Modify: `backend/osint/pipeline/cluster.py:212-214` (imports), `:281-291` (`ClusterDoc.actors`), `:344` (`EventState.actors` type), `:1114-1121` (`Clusterer._country`)
- Modify: `backend/osint/pipeline/triage.py:16-19` (imports), `:346-350` (GDELT `PlaceHit`)
- Modify: `backend/osint/core/models.py:113` (comment)
- Modify: `backend/osint/collectors/acled.py:36-39` (imports), `:143-148`
- Modify: `backend/osint/backfill/ucdp.py:34-36` (imports), `:155-159`
- Modify: `backend/osint/pipeline/extract/verdict.py:13-14` (imports), `:148` (`where.country`)
- Modify: `backend/osint/store/sqlite.py:141-148` (constant `COUNTRY_MIGRATION_STATE`)
- Modify: `backend/osint/intel/context.py:50` (imports), `:470-477` (`resolve_cc`), `:793-794` (`section_country` names)
- Test: `tests/pipeline/test_cluster.py`, `tests/pipeline/test_triage.py`, `tests/collectors/test_acled.py`, `tests/backfill/test_ucdp_adsb_backfill.py`, `tests/extract/test_verdict.py`, `tests/intel/test_context.py`

**Interfaces:**
- Consumes: `to_iso2(value, hint)` from Task 1.
- Produces:
  - GDELT actor dicts from `ClusterDoc.actors()`: `{"name", "role", "type", "country": ISO2 | None, "cameo": str, "source": "gdelt"}`.
  - `Clusterer._country(doc) -> str | None` always ISO2 (FIPS for GDELT converted, gazetteer codes registry-checked).
  - ACLED / UCDP event dicts: `country` ISO2 (or None); actors `{"name", "role", "type", "country": None, "source": "acled" | "ucdp"}` (Task 5 fills `country`).
  - `osint.store.sqlite.COUNTRY_MIGRATION_STATE = "country_migration"` (cursor name; Task 10 writes `{"done": True, ...}`).
  - `context.resolve_cc(runtime, subj)`: trusts `events.country` only after the migration marker exists.

- [ ] **Step 1: Write the failing tests**

Append to `tests/pipeline/test_cluster.py`:

```python
# ------------------------------------------------------------ country normalisation


def test_gdelt_geo_and_actor_countries_become_iso2():
    g = gdelt(1)
    g.attrs.update({"geo_country": "GM", "actor1_country": "RUS", "actor2_country": "IGO"})
    assert Clusterer._country(g) == "DE"  # FIPS GM is Germany, not Gambia
    a1, a2 = g.actors()
    assert (a1["country"], a1["cameo"], a1["source"]) == ("RU", "RUS", "gdelt")
    assert a2["country"] is None and a2["cameo"] == "IGO"
    assert Clusterer._country(gdelt(2)) == "UA"  # fixture code "UP"


def test_article_place_country_is_checked_against_the_registry():
    assert Clusterer._country(art(1, "Strike hits Kharkiv")) == "UA"
    bad = art(2, "Strike hits Kharkiv")
    bad.places[0]["country"] = "XX"
    assert Clusterer._country(bad) is None
```

Append to `tests/pipeline/test_triage.py`:

```python
def test_gdelt_coded_geo_country_is_iso2(triage):
    r = triage.assess(_doc(
        "Russia — Fight — Ukraine (190) in Kharkiv, Ukraine", "Kharkiv, Ukraine",
        kind="gdelt_event", attrs={
            "event_code": "190", "event_root_code": "19", "quad_class": 4,
            "num_articles": 8, "geo_type": 4, "geo_lat": 49.98, "geo_lon": 36.25,
            "geo_name": "Kharkiv, Kharkivs'ka Oblast', Ukraine", "geo_country": "UP"}))
    assert r.best_place.country == "UA"
```

In `tests/collectors/test_acled.py`, inside `test_map_row`, after the line `assert (e["lat"], e["lon"]) == (13.629, 25.3494)` add:

```python
    assert e["country"] == "SD"
    assert [a["source"] for a in e["actors"]] == ["acled", "acled"]
```

In `tests/backfill/test_ucdp_adsb_backfill.py`, inside `test_ucdp_row_mapping_precision_and_time`, after `assert ev["h3_3"] and ev["lat"] == 34.663769` add:

```python
    assert ev["country"] == "AF" and [a["source"] for a in ev["actors"]] == ["ucdp", "ucdp"]
```

Append to `tests/extract/test_verdict.py`:

```python
def test_extracted_country_is_checked_against_the_registry():
    def country(value):
        where = {"place_name": "Kharkiv", "admin1": None, "country": value,
                 "precision": "city"}
        return normalise(raw(where=where), TEXT)["where"]["country"]

    assert country("ua") == "UA"
    assert country("XX") is None
    assert country("UK") == "GB"  # not ISO2: read as a name/alias
```

Append to `tests/intel/test_context.py`:

```python
def test_resolve_cc_trusts_event_country_only_after_the_migration(rt):
    from osint.store.sqlite import COUNTRY_MIGRATION_STATE

    s = {"kind": "event", "event": {"country": "GM"}, "lat": 47.70, "lon": 40.21}
    assert context.resolve_cc(rt, s) == "RU"  # unmigrated "GM" may be FIPS: lat/lon wins
    assert context.resolve_cc(rt, {"kind": "event",
                                   "event": {"country": "Russia (Soviet Union)"}}) == "RU"
    rt.store.set_cursor(COUNTRY_MIGRATION_STATE, {"done": True})
    assert context.resolve_cc(rt, s) == "GM"


def test_country_history_counts_iso2_normalised_rows(rt, subj):
    rt.store.save_events([_ev("ucdp-3", 47.9, 39.95, datetime(2026, 5, 20, tzinfo=UTC),
                              status="historical", country="RU", origin="historical")])
    assert context.section_country(rt, subj, NOW)["history"]["total"] == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_cluster.py tests/pipeline/test_triage.py tests/collectors/test_acled.py tests/backfill/test_ucdp_adsb_backfill.py tests/extract/test_verdict.py tests/intel/test_context.py -v -k "iso2 or registry or map_row or ucdp_row or resolve_cc or country_history"`
Expected: FAIL — `_country(g)` returns `"GM"`, `KeyError: 'cameo'`, `"Sudan" != "SD"`, `ImportError: cannot import name 'COUNTRY_MIGRATION_STATE'`, history total 2.

- [ ] **Step 3: Implement — cluster.py**

In `backend/osint/pipeline/cluster.py` add the import after `from osint.core.geo import haversine_km, valid_coord`:

```python
from osint.core.countries import to_iso2
```

Replace `ClusterDoc.actors`:

```python
    def actors(self) -> list[dict[str, Any]]:
        """GDELT actors: the CAMEO country code becomes ISO2 (kept as ``cameo``)."""
        if not self.is_gdelt:
            return []
        out = []
        for n, role in ((1, "source"), (2, "target")):
            name = str(self.attrs.get(f"actor{n}") or "").strip()
            if name:
                cameo = str(self.attrs.get(f"actor{n}_country") or "").strip().upper()
                out.append({"name": name.title(), "role": role,
                            "type": str(self.attrs.get(f"actor{n}_code") or ""),
                            "country": to_iso2(cameo, "cameo") if cameo else None,
                            "cameo": cameo, "source": "gdelt"})
        return out
```

In `EventState` change the field `actors: list[dict[str, str]] = field(default_factory=list)` to:

```python
    actors: list[dict[str, Any]] = field(default_factory=list)
```

Replace `Clusterer._country`:

```python
    @staticmethod
    def _country(doc: ClusterDoc) -> str | None:
        """ISO2 of the document's place: GDELT ActionGeo codes are FIPS 10-4 (``GM`` is
        Germany); gazetteer hits are ISO2 already and are checked against the registry."""
        if doc.is_gdelt:
            return to_iso2(str(doc.attrs.get("geo_country") or ""), "fips")
        for p in doc.places:
            if p.get("name") == doc.place_name and p.get("country"):
                return to_iso2(str(p["country"]), "iso2")
        return None
```

- [ ] **Step 4: Implement — triage.py and models.py**

In `backend/osint/pipeline/triage.py` add after `from osint.config import ROOT`:

```python
from osint.core.countries import to_iso2
```

and in `_gdelt` replace `name=str(a.get("geo_name") or ""), country=str(a.get("geo_country") or ""),` with:

```python
                name=str(a.get("geo_name") or ""),
                country=to_iso2(str(a.get("geo_country") or ""), "fips") or "",
```

In `backend/osint/core/models.py` change the `PlaceHit.country` comment to:

```python
    country: str  # ISO 3166-1 alpha-2 ("" if unknown; GDELT FIPS codes are converted)
```

- [ ] **Step 5: Implement — ACLED and UCDP**

In `backend/osint/collectors/acled.py` add after `from osint.core.collector import ...`:

```python
from osint.core.countries import to_iso2
```

and replace the `actors` and `country` entries of the `event` dict in `map_row`:

```python
        "actors": [a for a in (
            {"name": actor1, "role": "actor1", "type": _s(row, "inter1"), "country": None,
             "source": SOURCE},
            {"name": actor2, "role": "actor2", "type": _s(row, "inter2"), "country": None,
             "source": SOURCE})
            if a["name"]],
        "lat": lat, "lon": lon, "precision": prec, "place_name": location or admin1 or None,
        "country": to_iso2(country, "name") if country else None, "geo_confidence": geo_conf,
```

In `backend/osint/backfill/ucdp.py` add after `from osint.core.geo import ...`:

```python
from osint.core.countries import to_iso2
```

and replace the `actors` and `country` entries of the `event` dict in `map_row`:

```python
        "actors": [a for a in ({"name": side_a, "role": "side_a", "type": "", "country": None,
                                "source": SOURCE},
                               {"name": side_b, "role": "side_b", "type": "", "country": None,
                                "source": SOURCE})
                   if a["name"]],
        "lat": lat, "lon": lon, "precision": prec, "place_name": place or country or None,
        "country": to_iso2(country, "name") if country else None, "geo_confidence": geo_conf,
```

(`where`, `title` and `place_name` keep using the raw UCDP / ACLED name.)

- [ ] **Step 6: Implement — verdict.py**

In `backend/osint/pipeline/extract/verdict.py` add after `from osint.core.geo import haversine_km`:

```python
from osint.core.countries import to_iso2
```

and in `normalise` replace `"country": country if re.fullmatch(r"[A-Z]{2}", country) else None,` with:

```python
            # ISO2 per the instruction, checked against the registry; a non-ISO2 answer
            # such as "UK" resolves through the country names
            "country": to_iso2(country, "auto"),
```

- [ ] **Step 7: Implement — migration marker and intel context**

In `backend/osint/store/sqlite.py` add after `HISTORICAL_STATUS = "historical"`:

```python
# source_state cursor written by osint.pipeline.migrate_countries once events.country and
# actors[].country hold ISO2 codes ({"done": true, "at": ..., "rewritten": n})
COUNTRY_MIGRATION_STATE = "country_migration"
```

In `backend/osint/intel/context.py` add after `from osint.core.geo import haversine_km`:

```python
from osint.core.countries import to_iso2
from osint.store.sqlite import COUNTRY_MIGRATION_STATE
```

Replace `resolve_cc`:

```python
def _countries_migrated(runtime) -> bool:
    get = getattr(getattr(runtime, "store", None), "get_cursor", None)
    if get is None:
        return False
    try:
        return bool((get(COUNTRY_MIGRATION_STATE) or {}).get("done"))
    except sqlite3.Error:
        return False


def resolve_cc(runtime, subj: dict) -> str | None:
    """ISO2 of the subject. ``events.country`` is trusted once the country migration
    (``osint.pipeline.migrate_countries``) has normalised it; before that a two-letter
    value may still be a FIPS code, so the lat/lon reverse geocode wins and the stored
    value is only a fallback."""
    raw = ((subj.get("event") or {}).get("country") or "").strip()
    if raw and _countries_migrated(runtime):
        cc = to_iso2(raw, "iso2")
        if cc:
            return cc
    if subj.get("lat") is not None:
        s = settlements(runtime)
        cc = s.country_at(subj["lat"], subj["lon"]) if s else None
        if cc:
            return cc
    return to_iso2(raw, "auto") if raw else None
```

In `section_country` replace `names = [x for x in [name, *UCDP_ALIASES.get(cc, [])] if x]` with:

```python
    # the ISO2 code first: UCDP rows normalised by osint.pipeline.migrate_countries
    names = [x for x in [cc, name, *UCDP_ALIASES.get(cc, [])] if x]
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/pipeline/test_cluster.py tests/pipeline/test_triage.py tests/collectors tests/backfill tests/extract tests/intel tests/pipeline/test_event_builder.py -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/osint/pipeline/cluster.py backend/osint/pipeline/triage.py backend/osint/core/models.py backend/osint/collectors/acled.py backend/osint/backfill/ucdp.py backend/osint/pipeline/extract/verdict.py backend/osint/store/sqlite.py backend/osint/intel/context.py tests/pipeline/test_cluster.py tests/pipeline/test_triage.py tests/collectors/test_acled.py tests/backfill/test_ucdp_adsb_backfill.py tests/extract/test_verdict.py tests/intel/test_context.py
git commit -m "feat(countries): normalise GDELT/ACLED/UCDP/LLM country codes to ISO2 on write"
```

---

### Task 3: Events schema — derived columns, `event_fields.source`, store methods

**Files:**
- Modify: `backend/osint/store/sqlite.py` (constants after `HISTORICAL_STATUS`; `SLIM_EVENT_COLUMNS` ~:180; `_event_out` ~:221; `SqliteStore.__init__` ~:270; `save_events` ~:426-449; `merge_events` insert ~:890; new methods after `event_documents_for` ~:842)
- Test: `tests/store/test_sqlite_derived.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (on `SqliteStore`):
  - columns `events.actor_countries TEXT NOT NULL DEFAULT '[]'`, `foreign_involved INTEGER NOT NULL DEFAULT 0`, `attributed INTEGER NOT NULL DEFAULT 0`, `lifecycle TEXT`; index `events_country`; `event_fields.source TEXT NOT NULL DEFAULT ''`.
  - `DERIVED_COLUMNS = ("actor_countries", "foreign_involved", "attributed")`, `RULE_SOURCE = "rule"`.
  - `save_events(...)`: event dicts that carry all `DERIVED_COLUMNS` write them; a dict with `lifecycle` writes it; dicts without them leave the stored values alone.
  - `set_event_derived(rows: list[dict]) -> int` — rows `{id, actor_countries, foreign_involved, attributed[, actors]}`.
  - `set_lifecycle(states: dict[str, str]) -> int`
  - `lifecycle_inputs(recent_since: datetime, ids: list[str] | None = None) -> list[dict]` — `{id, origin, t_end, lifecycle, last_doc_at, recent_docs}`.
  - `replace_rule_fields(event_ids: list[str], fields: list[dict]) -> None`
  - `rule_fields(event_ids: list[str]) -> dict[str, list[dict]]` — `{field, value, doc_id, quote}` rows of `source='rule'`.
  - `rule_documents(event_ids: list[str]) -> dict[str, list[dict]]` — `{doc_id, source, url, title, published_at, language, role}`, gdelt rows excluded, oldest first.
  - `events_for_rules(event_ids: list[str]) -> dict[str, dict]` — `{id, origin, country, actors, title, summary, t_end}`.
  - List/detail event dicts carry `actor_countries` (list), `foreign_involved`, `attributed` (ints), `lifecycle`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_sqlite_derived.py`:

```python
"""Deterministic-extraction columns and store methods (osint.store.sqlite)."""
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from osint.store.sqlite import EVENTS_SCHEMA, SqliteStore, iso

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _ev(eid="E1", **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "t_start": iso(T0),
            "t_end": iso(T0), "status": "reported", "confidence": 0.4, "version": 1,
            "mention_count": 1, "source_diversity": 1, "first_seen": iso(T0),
            "updated_at": iso(T0), "country": "UA"}
    return {**base, **kw}


def _doc(eid, did, at, role="mention"):
    return {"event_id": eid, "doc_id": did, "source": "rss:t",
            "url": f"https://{did}.example/a", "title": f"title {did}",
            "published_at": iso(at), "language": "en", "role": role, "added_at": iso(at)}


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_new_columns_defaults_and_country_index(store):
    store.save_events([_ev()])
    e = store.get_event("E1")
    assert e["actor_countries"] == [] and e["foreign_involved"] == 0
    assert e["attributed"] == 0 and e["lifecycle"] is None
    idx = {r[1] for r in store._db.execute("PRAGMA index_list(events)")}
    assert "events_country" in idx
    slim = store.list_events(limit=5)[0]
    assert {"lifecycle", "actor_countries", "foreign_involved", "attributed"} <= set(slim)


def test_save_events_writes_derived_and_a_plain_upsert_keeps_them(store):
    store.save_events([_ev(actor_countries=["RU", "UA", "RU"], foreign_involved=True,
                           attributed=True, lifecycle="active")])
    store.save_events([_ev(version=2, title="t2")])  # a clusterer row without them
    e = store.get_event("E1")
    assert e["actor_countries"] == ["RU", "UA"] and e["foreign_involved"] == 1
    assert e["attributed"] == 1 and e["lifecycle"] == "active" and e["version"] == 2


def test_set_event_derived_with_and_without_actors(store):
    store.save_events([_ev(), _ev("E2")])
    n = store.set_event_derived([
        {"id": "E1", "actors": [{"name": "Russian forces", "country": "RU", "source": "rule"}],
         "actor_countries": ["RU"], "foreign_involved": True, "attributed": True},
        {"id": "E2", "actor_countries": [], "foreign_involved": False, "attributed": False},
        {"id": "missing", "actor_countries": ["RU"], "foreign_involved": True,
         "attributed": True}])
    assert n == 2
    e1 = store.get_event("E1")
    assert e1["actors"][0]["source"] == "rule" and e1["actor_countries"] == ["RU"]
    assert store.get_event("E2")["attributed"] == 0


def test_rule_fields_replace_only_rule_rows(store):
    store.save_events([_ev()], fields=[{"event_id": "E1", "field": "location",
                                        "value": "Kyiv", "doc_id": "d1", "quote": "in Kyiv"}])
    store.replace_rule_fields(["E1"], [{"event_id": "E1", "field": "headline", "value": "Old",
                                        "doc_id": "d1", "quote": "Old"}])
    store.replace_rule_fields(["E1"], [
        {"event_id": "E1", "field": "headline", "value": "New", "doc_id": "d2", "quote": "New"},
        {"event_id": "E1", "field": "casualties.killed", "value": "3", "doc_id": "d2",
         "quote": "kills 3"}])
    rules = store.rule_fields(["E1"])["E1"]
    assert [(r["field"], r["value"]) for r in rules] == [("casualties.killed", "3"),
                                                          ("headline", "New")]
    fields = {(f["field"], f["value"]) for f in store.get_event("E1")["fields"]}
    assert ("location", "Kyiv") in fields and ("headline", "Old") not in fields
    store.replace_rule_fields(["E1"], [])
    assert store.rule_fields(["E1"]) == {}


def test_rule_documents_and_events_for_rules(store):
    store.save_events([_ev(actors=[{"name": "Russia", "country": "RUS"}], summary="s")],
                      documents=[_doc("E1", "d1", T0, "primary"), _doc("E1", "g1", T0, "gdelt")])
    docs = store.rule_documents(["E1"])["E1"]
    assert [d["doc_id"] for d in docs] == ["d1"] and docs[0]["url"] == "https://d1.example/a"
    ev = store.events_for_rules(["E1", "nope"])
    assert set(ev) == {"E1"} and ev["E1"]["actors"][0]["country"] == "RUS"
    assert ev["E1"]["origin"] == "news" and ev["E1"]["summary"] == "s"


def test_lifecycle_inputs_and_set_lifecycle(store):
    store.save_events(
        [_ev(), _ev("E2", lifecycle="closed"), _ev("E3")],
        documents=[_doc("E1", "d1", T0 - timedelta(hours=1), "primary"),
                   _doc("E1", "d2", T0 - timedelta(hours=8)), _doc("E1", "g", T0, "gdelt"),
                   _doc("E2", "d3", T0)])
    rows = {r["id"]: r for r in store.lifecycle_inputs(T0 - timedelta(hours=6))}
    assert set(rows) == {"E1", "E3"}  # closed events are not rescanned
    assert rows["E1"]["last_doc_at"] == iso(T0 - timedelta(hours=1))
    assert rows["E1"]["recent_docs"] == 1  # the GDELT row does not count
    assert rows["E3"]["last_doc_at"] is None and rows["E3"]["recent_docs"] == 0
    only = store.lifecycle_inputs(T0 - timedelta(hours=6), ids=["E2"])
    assert [r["id"] for r in only] == ["E2"] and only[0]["lifecycle"] == "closed"
    assert store.set_lifecycle({"E1": "developing", "E3": "active"}) == 2
    assert store.get_event("E1")["lifecycle"] == "developing"


def test_an_existing_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(EVENTS_SCHEMA)
    db.execute("INSERT INTO event_fields VALUES ('E1', 'location', 'Kyiv', 'd1', '')")
    db.commit()
    db.close()
    s = SqliteStore(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(events)")}
    assert {"actor_countries", "foreign_involved", "attributed", "lifecycle"} <= cols
    assert s._db.execute("SELECT source FROM event_fields").fetchone()[0] == ""
    s.close()
    SqliteStore(path).close()  # idempotent on the next start
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/store/test_sqlite_derived.py -v`
Expected: FAIL with `KeyError: 'actor_countries'` / `AttributeError: 'SqliteStore' object has no attribute 'set_event_derived'`.

- [ ] **Step 3: Implement the schema migration and helpers**

In `backend/osint/store/sqlite.py`, after the `COUNTRY_MIGRATION_STATE` constant (Task 2), add:

```python
# Deterministic extraction (osint.pipeline.rule_pass): columns derived from an event's actors.
# Not in EVENT_COLUMNS, so the clusterer's upsert never overwrites them; writers that know
# them put them in the event dict (save_events) or call set_event_derived.
DERIVED_COLUMNS = ("actor_countries", "foreign_involved", "attributed")
RULE_SOURCE = "rule"  # event_fields.source of rule-extracted values ('' = clusterer/LLM)
_DERIVED_SQL = ("UPDATE events SET actor_countries=?, foreign_involved=?, attributed=? "
                "WHERE id=?")
_DERIVED_ACTORS_SQL = ("UPDATE events SET actors=?, actor_countries=?, foreign_involved=?, "
                       "attributed=? WHERE id=?")
```

Extend `SLIM_EVENT_COLUMNS`:

```python
SLIM_EVENT_COLUMNS = ("id", "lat", "lon", "precision", "category", "status", "confidence",
                      "title", "mention_count", "source_diversity", "t_start", "t_end",
                      "updated_at", "place_name", "country", "subcategory", "lifecycle",
                      "actor_countries", "foreign_involved", "attributed")
```

Add after `_dump_json`:

```python
def _derived_params(r: dict) -> tuple:
    return (json.dumps(sorted(set(r.get("actor_countries") or [])), separators=(",", ":")),
            int(bool(r.get("foreign_involved"))), int(bool(r.get("attributed"))))


def _write_derived(db: sqlite3.Connection, rows: list[dict]) -> int:
    """UPDATE the derived columns (and ``actors`` when the row has it); caller holds the
    store lock and the transaction."""
    n = 0
    for r in rows:
        if "actors" in r:
            cur = db.execute(_DERIVED_ACTORS_SQL, (_json_col(r["actors"], "actors"),
                                                   *_derived_params(r), r["id"]))
        else:
            cur = db.execute(_DERIVED_SQL, (*_derived_params(r), r["id"]))
        n += cur.rowcount
    return n
```

Replace `_event_out`:

```python
def _event_out(row: sqlite3.Row, *, keep_state: bool = False) -> dict:
    d = dict(row)
    for c in _JSON_DEFAULTS:
        if c in d:
            d[c] = json.loads(d[c])
    if "actor_countries" in d:
        d["actor_countries"] = json.loads(d["actor_countries"] or "[]")
    if not keep_state:
        d.pop("cluster_state", None)
    return d
```

In `SqliteStore.__init__`, after `self._migrate_origin(heal=migrate)` add:

```python
        self._migrate_derived()
```

and add the method after `_migrate_origin`:

```python
    def _migrate_derived(self) -> None:
        """Add the deterministic-extraction columns, the events(country) index and
        event_fields.source (idempotent; tolerates another process adding them first)."""
        with self._lock, self._db:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
            for col, ddl in (("actor_countries", "TEXT NOT NULL DEFAULT '[]'"),
                             ("foreign_involved", "INTEGER NOT NULL DEFAULT 0"),
                             ("attributed", "INTEGER NOT NULL DEFAULT 0"),
                             ("lifecycle", "TEXT")):
                if col not in cols:
                    try:
                        self._db.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e):
                            raise
            self._db.execute("CREATE INDEX IF NOT EXISTS events_country ON events(country)")
            fcols = {r[1] for r in self._db.execute("PRAGMA table_info(event_fields)")}
            if "source" not in fcols:
                try:
                    self._db.execute(
                        "ALTER TABLE event_fields ADD COLUMN source TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
            self._db.execute("CREATE INDEX IF NOT EXISTS event_fields_source "
                             "ON event_fields(source, event_id)")
```

- [ ] **Step 4: Implement the writes in `save_events` and `merge_events`**

In `save_events`, replace the `with self._lock, self._db:` block with:

```python
        derived = [{"id": e["id"], **{c: e[c] for c in DERIVED_COLUMNS}}
                   for e in events if all(c in e for c in DERIVED_COLUMNS)]
        life = [(e["lifecycle"], e["id"]) for e in events if e.get("lifecycle")]
        with self._lock, self._db:
            if ev_rows:
                self._db.executemany(_UPSERT_EVENT, ev_rows)
            if derived:
                _write_derived(self._db, derived)
            if life:
                self._db.executemany("UPDATE events SET lifecycle=? WHERE id=?", life)
            if doc_rows:
                self._db.executemany(
                    f"INSERT OR IGNORE INTO event_documents ({', '.join(_EVENT_DOC_COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(_EVENT_DOC_COLUMNS))})", doc_rows)
            if field_rows:
                self._db.executemany(
                    "INSERT OR IGNORE INTO event_fields (event_id, field, value, doc_id, quote) "
                    "VALUES (?,?,?,?,?)", field_rows)
            if dec_rows:
                self._db.executemany(
                    "INSERT OR IGNORE INTO gdelt_decisions VALUES (?,?,?,?,?,?,?)", dec_rows)
```

In `merge_events` replace `db.executemany("INSERT OR IGNORE INTO event_fields VALUES (?,?,?,?,?)", [` with:

```python
                db.executemany("INSERT OR IGNORE INTO event_fields (event_id, field, value, "
                               "doc_id, quote) VALUES (?,?,?,?,?)", [
```

- [ ] **Step 5: Implement the new store methods**

Add to `SqliteStore` after `event_documents_for`:

```python
    # ------------------------------------------------- deterministic extraction

    def set_event_derived(self, rows: list[dict]) -> int:
        """Write ``{id, actor_countries, foreign_involved, attributed[, actors]}`` rows
        (osint.pipeline.rule_pass); returns the events updated (unknown ids are skipped)."""
        if not rows:
            return 0
        with self._lock, self._db:
            return _write_derived(self._db, rows)

    def set_lifecycle(self, states: dict[str, str]) -> int:
        """event id -> lifecycle state (osint.pipeline.lifecycle)."""
        if not states:
            return 0
        with self._lock, self._db:
            return self._db.executemany("UPDATE events SET lifecycle=? WHERE id=?",
                                        [(s, eid) for eid, s in states.items()]).rowcount

    def lifecycle_inputs(self, recent_since: datetime, ids: list[str] | None = None
                         ) -> list[dict]:
        """{id, origin, t_end, lifecycle, last_doc_at, recent_docs} per event: the newest
        linked article's published_at (GDELT rows excluded) and the articles published at or
        after ``recent_since``. ``ids`` None: every event whose lifecycle is unset or not
        'closed' (a closed event only reopens when a pass touches it: pass its id)."""
        base = ("SELECT e.id, e.origin, e.t_end, e.lifecycle, MAX(d.published_at), "
                "COALESCE(SUM(d.published_at >= ?), 0) FROM events e "
                "LEFT JOIN event_documents d ON d.event_id = e.id AND d.role != 'gdelt' ")
        since = iso(recent_since)
        rows: list[tuple] = []
        with self._lock:
            if ids is None:
                rows = self._db.execute(
                    base + "WHERE e.lifecycle IS NULL OR e.lifecycle != 'closed' "
                    "GROUP BY e.id", (since,)).fetchall()
            else:
                for i in range(0, len(ids), 800):
                    chunk = ids[i: i + 800]
                    rows += self._db.execute(
                        base + f"WHERE e.id IN ({','.join('?' * len(chunk))}) GROUP BY e.id",
                        (since, *chunk)).fetchall()
        return [{"id": r[0], "origin": r[1], "t_end": r[2], "lifecycle": r[3],
                 "last_doc_at": r[4], "recent_docs": int(r[5] or 0)} for r in rows]

    def replace_rule_fields(self, event_ids: list[str], fields: list[dict]) -> None:
        """Replace the ``source='rule'`` rows of ``event_ids`` with ``fields``
        ({event_id, field, value, doc_id, quote}); rows of other sources are untouched."""
        rows = [(f["event_id"], f["field"], str(f["value"]), f["doc_id"],
                 f.get("quote") or "", RULE_SOURCE) for f in fields]
        with self._lock, self._db:
            for i in range(0, len(event_ids), 800):
                chunk = event_ids[i: i + 800]
                self._db.execute(
                    f"DELETE FROM event_fields WHERE source = ? AND event_id IN "
                    f"({','.join('?' * len(chunk))})", (RULE_SOURCE, *chunk))
            if rows:
                self._db.executemany(
                    "INSERT OR IGNORE INTO event_fields (event_id, field, value, doc_id, quote, "
                    "source) VALUES (?,?,?,?,?,?)", rows)

    def rule_fields(self, event_ids: list[str]) -> dict[str, list[dict]]:
        """event id -> its ``source='rule'`` rows {field, value, doc_id, quote}."""
        out: dict[str, list[dict]] = {}
        for i in range(0, len(event_ids), 800):
            chunk = event_ids[i: i + 800]
            with self._lock:
                rows = self._db.execute(
                    "SELECT event_id, field, value, doc_id, quote FROM event_fields WHERE "
                    f"source = ? AND event_id IN ({','.join('?' * len(chunk))}) "
                    "ORDER BY event_id, field, value, doc_id", (RULE_SOURCE, *chunk)).fetchall()
            for eid, f, v, d, q in rows:
                out.setdefault(eid, []).append({"field": f, "value": v, "doc_id": d,
                                                "quote": q})
        return out

    def rule_documents(self, event_ids: list[str]) -> dict[str, list[dict]]:
        """event id -> its documents except GDELT rows, oldest first: {doc_id, source, url,
        title, published_at, language, role} (rule extraction input)."""
        out: dict[str, list[dict]] = {}
        for i in range(0, len(event_ids), 800):
            chunk = event_ids[i: i + 800]
            with self._lock:
                rows = self._db.execute(
                    "SELECT event_id, doc_id, source, url, title, published_at, language, role "
                    "FROM event_documents WHERE role != 'gdelt' AND event_id IN "
                    f"({','.join('?' * len(chunk))}) ORDER BY event_id, published_at, doc_id",
                    chunk).fetchall()
            for r in rows:
                out.setdefault(r[0], []).append({
                    "doc_id": r[1], "source": r[2], "url": r[3], "title": r[4],
                    "published_at": r[5], "language": r[6], "role": r[7]})
        return out

    def events_for_rules(self, event_ids: list[str]) -> dict[str, dict]:
        """event id -> {id, origin, country, actors, title, summary, t_end}."""
        out: dict[str, dict] = {}
        for i in range(0, len(event_ids), 800):
            chunk = event_ids[i: i + 800]
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, origin, country, actors, title, summary, t_end FROM events "
                    f"WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            for r in rows:
                out[r[0]] = {"id": r[0], "origin": r[1], "country": r[2],
                             "actors": json.loads(r[3] or "[]"), "title": r[4],
                             "summary": r[5] or "", "t_end": r[6]}
        return out
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/store tests/api/test_events_api.py tests/pipeline -v`
Expected: all PASS (existing merge / rebuild / API tests still pass with the named-column inserts and the wider slim rows).

- [ ] **Step 7: Commit**

```bash
git add backend/osint/store/sqlite.py tests/store/test_sqlite_derived.py
git commit -m "feat(store): actor_countries/foreign_involved/attributed/lifecycle columns, events(country) index, rule fields"
```

---
### Task 4: Phrase matcher and actor lexicon

**Files:**
- Create: `backend/osint/enrich/phrases.py`
- Create: `backend/osint/enrich/actors.py`
- Create: `config/actors.yaml`
- Test: `tests/enrich/test_phrases.py`, `tests/enrich/test_actors.py`

**Interfaces:**
- Consumes: `osint.enrich.gazetteer.tokens(text) -> list[tuple[str, str]]`, `name_key`; `osint.core.countries.get`, `all_countries`, `Country` (Task 1); `osint.terror.groups.GroupKB.load(path)`, `GroupKB.get(gid) -> Group | None` (`Group.name`, `Group.aliases`).
- Produces:
  - `osint.enrich.phrases.PhraseHit[T]` (`payload: T`, `start: int`, `end: int`, `text: str`) and `PhraseMatcher[T]` with `add(phrase: str, payload: T) -> bool`, `find(text: str) -> list[PhraseHit[T]]`, `__len__`.
  - `osint.enrich.actors`: `CLASSES`, `class LexiconError(RuntimeError)`, `@dataclass(frozen=True, slots=True) class ActorEntry: name: str; country: str | None; klass: str`, `class ActorLexicon` with `ActorLexicon.load(path: Path = DEFAULT_ACTORS, *, groups_path: Path = DEFAULT_GROUPS) -> ActorLexicon`, `find(text) -> list[PhraseHit[ActorEntry]]`, `lookup(name) -> ActorEntry | None`, `__len__`; `default_lexicon() -> ActorLexicon` (lru-cached, `config/`).

- [ ] **Step 1: Write the failing tests**

Create `tests/enrich/test_phrases.py`:

```python
from osint.enrich.phrases import PhraseMatcher


def _m(*pairs):
    m = PhraseMatcher()
    for phrase, payload in pairs:
        m.add(phrase, payload)
    return m


def test_longest_phrase_wins_and_matches_do_not_overlap():
    m = _m(("Russian", "adj"), ("Russian forces", "forces"), ("forces", "noun"))
    assert [h.payload for h in m.find("Russian forces attack; forces regroup")] == [
        "forces", "noun"]


def test_whole_words_only():
    m = _m(("Iran", "IR"))
    assert m.find("Iranian officials said") == []
    assert [h.payload for h in m.find("Iran says")] == ["IR"]


def test_case_diacritics_and_punctuation_fold():
    m = _m(("cote d'ivoire army", "CI"), ("U.S. forces", "US"))
    hits = m.find("CÔTE D’IVOIRE ARMY and u.s. forces")
    assert [h.payload for h in hits] == ["CI", "US"]
    assert hits[1].text == "u.s forces" and (hits[1].start, hits[1].end) == (5, 8)


def test_trailing_star_is_a_prefix_on_the_last_word():
    m = _m(("беспилотник*", "drone"), ("cruise missile*", "cruise"))
    assert [h.payload for h in m.find("сбиты 20 беспилотников")] == ["drone"]
    assert [h.payload for h in m.find("two cruise missiles")] == ["cruise"]
    assert m.find("cruise ship") == []


def test_first_payload_of_a_phrase_is_kept():
    m = PhraseMatcher()
    assert m.add("IDF", 1) is True
    assert m.add("idf", 2) is False and m.add("  ", 3) is False
    assert len(m) == 1 and m.find("the IDF said")[0].payload == 1
```

Create `tests/enrich/test_actors.py`:

```python
"""Actor lexicon (osint.enrich.actors) over config/actors.yaml + terror groups + countries."""
import pytest

from osint.enrich.actors import ActorLexicon, LexiconError


@pytest.fixture(scope="module")
def lex():
    return ActorLexicon.load()


@pytest.mark.parametrize(("text", "country"), [
    ("Russian forces pushed west", "RU"), ("the Ukrainian army said", "UA"),
    ("Ukraine army units", "UA"), ("the IDF said", "IL"), ("U.S. forces struck", "US"),
    ("Iranian drones hit Kyiv", "IR"), ("Norwegian army exercise", "NO"),
    ("Russian shelling of Kyiv", "RU"), ("Houthis attack a ship", "YE"),
    ("Hezbollah fires rockets", "LB")])
def test_generated_hand_written_and_group_phrases(lex, text, country):
    hits = lex.find(text)
    assert hits and hits[0].payload.country == country


def test_whole_words_only(tmp_path):
    p = tmp_path / "actors.yaml"
    p.write_text('entries:\n  - match: ["Iran"]\n    country: "IR"\n'
                 '    class: state_government\n', encoding="utf-8")
    lx = ActorLexicon.load(p, groups_path=tmp_path / "none.yaml")
    assert lx.find("Iranian officials") == []
    assert lx.find("Iran says")[0].payload.country == "IR"


def test_longest_match_wins(lex):
    hits = lex.find("Military Forces of Ukraine (2019-) and Wagner Group")
    assert [(h.payload.country, h.payload.klass) for h in hits] == [
        ("UA", "state_military"), ("RU", "non_state_armed")]
    assert hits[0].text == "Military Forces of Ukraine"


@pytest.mark.parametrize(("side", "country", "klass"), [
    ("Military Forces of Sudan (2019-)", "SD", "state_military"),
    ("Rapid Support Forces", "SD", "non_state_armed"),
    ("Police Forces of Kenya (2022-)", "KE", "state_police"),
    ("Protesters (Kenya)", "KE", "protesters"),
    ("Civilians (Sudan)", "SD", "civilians"),
    ("Government of Russia (Soviet Union)", "RU", "state_government"),
    ("Government of Afghanistan", "AF", "state_government"),
    ("Hamas", "PS", "non_state_armed"), ("Al-Shabaab", "SO", "non_state_armed"),
    ("IRGC", "IR", "state_military")])
def test_acled_and_ucdp_side_names(lex, side, country, klass):
    e = lex.lookup(side)
    assert e is not None and (e.country, e.klass) == (country, klass)


def test_unknown_side_names_resolve_to_none(lex):
    assert lex.lookup("Civilians") is None and lex.lookup("IS") is None
    assert lex.lookup("Georgia police") is None  # no name forms for GE (US state)


def test_null_country_entries_block_shorter_matches(lex):
    [hit] = lex.find("UN peacekeepers came under fire")
    assert hit.payload.country is None


@pytest.mark.parametrize("body", [
    'entries:\n  - match: ["X army"]\n    country: "ZZ"\n    class: state_military\n',
    'entries:\n  - match: ["X army"]\n    country: NO\n    class: state_military\n',
    'entries:\n  - match: ["X army"]\n    country: "RU"\n    class: bogus\n',
    'entries:\n  - match: []\n    country: "RU"\n    class: state_military\n',
    'terror_group_homes:\n  nope: "RU"\n',
    "",
    "entries: [unclosed\n"])
def test_a_bad_lexicon_is_fatal(tmp_path, body):
    p = tmp_path / "actors.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(LexiconError):
        ActorLexicon.load(p, groups_path=tmp_path / "none.yaml")


def test_a_missing_lexicon_is_fatal(tmp_path):
    with pytest.raises(LexiconError):
        ActorLexicon.load(tmp_path / "missing.yaml")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/enrich/test_phrases.py tests/enrich/test_actors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.enrich.phrases'`.

- [ ] **Step 3: Implement the phrase matcher**

Create `backend/osint/enrich/phrases.py`:

```python
"""Whole-word phrase lookup on gazetteer-folded tokens (``osint.enrich.gazetteer.tokens``):
case-, diacritic- and punctuation-insensitive ("Côte d'Ivoire" -> cote d ivoire, "U.S." ->
u s). At each position the longest phrase wins and matches never overlap. A trailing ``*``
makes a phrase's last word a prefix ("беспилотник*" matches "беспилотников"). The first
payload added for a phrase is kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from osint.enrich.gazetteer import tokens


@dataclass(frozen=True, slots=True)
class PhraseHit[T]:
    payload: T
    start: int  # first token index
    end: int  # one past the last token
    text: str  # the matched words as written in the text


class PhraseMatcher[T]:
    def __init__(self) -> None:
        # first word -> [(words, last word is a prefix, payload)]
        self._by_first: dict[str, list[tuple[tuple[str, ...], bool, T]]] = {}
        self._prefix_words: list[tuple[str, T]] = []  # one-word prefix phrases
        self._keys: set[tuple[tuple[str, ...], bool]] = set()

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, phrase: str, payload: T) -> bool:
        """Add ``phrase``; False when it is empty or already present (first one wins)."""
        raw = (phrase or "").strip()
        prefix = raw.endswith("*")
        words = tuple(t for t, _ in tokens(raw.rstrip("*")))
        if not words or (words, prefix) in self._keys:
            return False
        self._keys.add((words, prefix))
        if prefix and len(words) == 1:
            self._prefix_words.append((words[0], payload))
            self._prefix_words.sort(key=lambda p: -len(p[0]))
        else:
            self._by_first.setdefault(words[0], []).append((words, prefix, payload))
        return True

    @staticmethod
    def _matches(folded: list[str], i: int, words: tuple[str, ...], prefix: bool) -> bool:
        n = len(words)
        if i + n > len(folded):
            return False
        for k in range(n):
            tok, w = folded[i + k], words[k]
            if prefix and k == n - 1:
                if not tok.startswith(w):
                    return False
            elif tok != w:
                return False
        return True

    def find(self, text: str) -> list[PhraseHit[T]]:
        pairs = tokens(text or "")
        folded = [t for t, _ in pairs]
        out: list[PhraseHit[T]] = []
        i = 0
        while i < len(folded):
            best: tuple[int, T] | None = None
            for words, prefix, payload in self._by_first.get(folded[i], ()):
                if (best is None or len(words) > best[0]) and self._matches(
                        folded, i, words, prefix):
                    best = (len(words), payload)
            if best is None:
                for stem, payload in self._prefix_words:
                    if folded[i].startswith(stem):
                        best = (1, payload)
                        break
            if best is None:
                i += 1
                continue
            n, payload = best
            originals: list[str] = []
            for _, orig in pairs[i:i + n]:  # "U.S" yields two tokens of one original
                if not originals or originals[-1] is not orig:
                    originals.append(orig)
            out.append(PhraseHit(payload, i, i + n, " ".join(originals)))
            i += n
        return out
```

- [ ] **Step 4: Write the lexicon config**

Create `config/actors.yaml`:

```yaml
# Actor lexicon (osint.enrich.actors): actor phrase -> home country (ISO2) and actor class.
# Matching is whole-word on case-, diacritic- and punctuation-folded text; the longest phrase
# wins; hand-written entries beat terror-group homes, which beat generated phrases.
# Quote every country code ("NO" is a YAML boolean otherwise). country: null names an actor
# with no home country; its phrases still block shorter generated matches inside them.
#
# classes: state_military | state_police | state_government | non_state_armed |
#          protesters | rioters | civilians | other

generated:
  # "<demonym | extra adjective | country name> <noun>": "Russian forces", "Ukraine army"
  nouns:
    state_military: [forces, force, troops, soldiers, army, navy, "air force", military,
                     "armed forces", marines, "special forces", "border guards", "coast guard"]
    state_police: [police, "security forces", "border police"]
    state_government: [government, ministry, authorities, officials, "foreign ministry",
                       "defence ministry", "defense ministry", "interior ministry"]
  # "<demonym | extra adjective> <noun>" only: after a country name these words usually
  # name the target ("Ukraine drone strikes"), after a demonym the attacker
  demonym_nouns:
    state_military: [drone, drones, missile, missiles, strike, strikes, airstrike, airstrikes,
                     "air strike", "air strikes", shelling, artillery, warplane, warplanes, jets,
                     "fighter jets", warship, warships, attack, attacks, offensive, raid, raids]
  # ACLED / UCDP state side names: "Military Forces of Sudan (2019-)", "Government of Ukraine"
  of_country:
    state_military: ["military forces of", "armed forces of"]
    state_police: ["police forces of"]
    state_government: ["government of"]
  # ACLED "<role> (<country>)" names: "Protesters (Kenya)", "Civilians (Sudan)"
  country_suffix:
    protesters: [protesters]
    rioters: [rioters]
    civilians: [civilians]
  # no "<country name> <noun>" forms: "Georgia police" is usually the US state,
  # "Jersey police" New Jersey
  skip_name_forms: ["GE", "JE"]

# adjectives beyond the demonyms in config/countries.yaml
extra_adjectives:
  "US": ["US", "U.S.", "United States"]
  "GB": ["UK", "U.K."]
  "AE": ["UAE"]
  "CD": ["DRC"]
  "KP": ["DPRK"]
  "NZ": ["New Zealand"]
  "HK": ["Hong Kong"]

# config/terror_groups.yaml group id -> home country (class non_state_armed unless given);
# groups without a single home country (Islamic State core, al-Qaeda core, AQIS, online
# networks) are not listed
terror_group_homes:
  iskp: "AF"
  iswap: "NG"
  is_sahel: "ML"
  is_somalia: "SO"
  is_mozambique: "MZ"
  is_drc_adf: "CD"
  is_sinai: "EG"
  is_libya: "LY"
  is_philippines: "PH"
  is_bangladesh: "BD"
  aqap: "YE"
  aqim: "DZ"
  jnim: "ML"
  alshabaab: "SO"
  hurras_al_din: "SY"
  hts: "SY"
  boko_haram: "NG"
  ansaru: "NG"
  ttp: "PK"
  taliban: {country: "AF", class: state_government}
  haqqani: "AF"
  let: "PK"
  trf: "IN"
  jem: "PK"
  bla: "PK"
  hamas: "PS"
  pij: "PS"
  pflp: "PS"
  hezbollah: "LB"
  houthis: "YE"
  kataib_hezbollah: "IQ"
  aah: "IQ"
  nujaba: "IQ"
  pkk: "TR"
  tak: "TR"
  dhkpc: "TR"
  eln: "CO"
  farc_dissidents: "CO"
  shining_path: "PE"
  npa: "PH"
  cpi_maoist: "IN"
  ltte: "LK"
  babbar_khalsa: "IN"
  new_ira: "GB"
  atomwaffen: "US"
  the_base: "US"
  nrm: "SE"
  rim: "RU"
  national_action: "GB"
  com_764: "US"
  fai_fri: "IT"
  palestine_action: "GB"
  muslim_brotherhood: "EG"
  irgc: {country: "IR", class: state_military}
  sinaloa: "MX"
  cjng: "MX"
  tren_de_aragua: "VE"
  ms13: "SV"
  viv_ansanm: "HT"
  comando_vermelho: "BR"
  pcc: "BR"

entries:
  # ---- state forces and agencies
  - match: ["Russian forces", "Russian army", "Russian Armed Forces", "Russian military",
            "VKS", "Russian Aerospace Forces", "Russian Navy", "Black Sea Fleet", "Africa Corps",
            "Военно-космические силы", "ВКС России", "ВС РФ", "Вооружённые силы России",
            "армия России", "російські окупанти", "окупанти", "рашисти"]
    name: Russian forces
    country: "RU"
    class: state_military
  - match: ["Kremlin", "Кремль"]
    name: Russian government
    country: "RU"
    class: state_government
  - match: ["Wagner Group", "Wagner PMC", "Wagner mercenaries", "ЧВК Вагнер", "группа Вагнера"]
    name: Wagner Group
    country: "RU"
    class: non_state_armed
  - match: ["Armed Forces of Ukraine", "Ukrainian Armed Forces", "AFU", "ZSU", "ЗСУ", "ВСУ",
            "Збройні сили України", "Вооруженные силы Украины", "SBU", "СБУ", "HUR", "ГУР",
            "Ukrainian military intelligence"]
    name: Ukrainian forces
    country: "UA"
    class: state_military
  - match: ["IDF", "Israel Defense Forces", "Israel Defence Forces", "Israeli army",
            "Israeli military", "Israeli Air Force", "Tsahal", "ЦАХАЛ", "جيش الاحتلال",
            "جيش الاحتلال الإسرائيلي", "الجيش الإسرائيلي"]
    name: Israel Defense Forces
    country: "IL"
    class: state_military
  - match: ["Shin Bet", "Mossad"]
    name: Israeli security services
    country: "IL"
    class: state_government
  - match: ["Pentagon", "US Central Command", "CENTCOM", "AFRICOM", "US Africa Command",
            "US Navy", "US Air Force", "US Army", "US Marines", "US Marine Corps"]
    name: American forces
    country: "US"
    class: state_military
  - match: ["White House", "State Department", "US State Department"]
    name: American government
    country: "US"
    class: state_government
  - match: ["Royal Navy", "Royal Air Force", "RAF", "British Army"]
    name: British forces
    country: "GB"
    class: state_military
  - match: ["Basij", "Quds Force", "Iranian Revolutionary Guards", "سپاه قدس", "بسیج"]
    name: Iranian forces
    country: "IR"
    class: state_military
  - match: ["PLA", "People's Liberation Army", "PLA Navy", "China Coast Guard",
            "Chinese coast guard"]
    name: Chinese forces
    country: "CN"
    class: state_military
  - match: ["KPA", "Korean People's Army"]
    name: North Korean forces
    country: "KP"
    class: state_military
  - match: ["Turkish Armed Forces", "TSK"]
    name: Turkish forces
    country: "TR"
    class: state_military
  - match: ["Sudanese Armed Forces", "SAF", "Sudanese army"]
    name: Sudanese forces
    country: "SD"
    class: state_military
  - match: ["Rapid Support Forces", "RSF", "Janjaweed", "قوات الدعم السريع", "الدعم السريع"]
    name: Rapid Support Forces
    country: "SD"
    class: non_state_armed
  - match: ["FARDC", "Armed Forces of the Democratic Republic of the Congo"]
    name: Congolese forces
    country: "CD"
    class: state_military
  - match: ["M23", "March 23 Movement", "AFC/M23", "Alliance Fleuve Congo"]
    name: M23
    country: "CD"
    class: non_state_armed
  - match: ["Tatmadaw", "Myanmar military", "Myanmar army", "junta forces"]
    name: Burmese forces
    country: "MM"
    class: state_military
  - match: ["Arakan Army", "Kachin Independence Army", "Ta'ang National Liberation Army",
            "Myanmar National Democratic Alliance Army", "MNDAA", "TNLA"]
    name: Myanmar ethnic armed organisations
    country: "MM"
    class: non_state_armed
  - match: ["ENDF", "Ethiopian National Defense Force"]
    name: Ethiopian forces
    country: "ET"
    class: state_military
  - match: ["Fano", "Fano militia", "TPLF", "Tigray Defense Forces", "Tigray Defence Forces",
            "Oromo Liberation Army"]
    name: Ethiopian armed groups
    country: "ET"
    class: non_state_armed
  - match: ["LNA", "Libyan National Army", "Haftar forces"]
    name: Libyan National Army
    country: "LY"
    class: non_state_armed
  - match: ["SDF", "Syrian Democratic Forces", "YPG", "People's Protection Units"]
    name: Syrian Democratic Forces
    country: "SY"
    class: non_state_armed
  - match: ["Saudi-led coalition", "Saudi-led coalition forces"]
    name: Saudi forces
    country: "SA"
    class: state_military
  - match: ["FAMa", "Malian Armed Forces"]
    name: Malian forces
    country: "ML"
    class: state_military
  - match: ["Somali National Army"]
    name: Somali forces
    country: "SO"
    class: state_military
  # ---- international actors without a home country (block shorter generated matches)
  - match: ["NATO", "UN peacekeepers", "UNIFIL", "MONUSCO", "KFOR", "African Union forces",
            "AUSSOM", "ATMIS", "EU naval force", "EUNAVFOR", "Operation Aspides"]
    name: International forces
    country: null
    class: other
```

- [ ] **Step 5: Implement the lexicon**

Create `backend/osint/enrich/actors.py`:

```python
"""Actor lexicon and actor-country resolution (deterministic extraction spec §2.3).

``config/actors.yaml`` maps actor phrases to a home country (ISO2) and an actor class:
hand-written ``entries`` (ACLED / UCDP side names, armed forces, agencies), non-state
groups from ``config/terror_groups.yaml`` with a home country (``terror_group_homes``), and
phrases generated for every country in ``config/countries.yaml`` ("<demonym> forces",
"Government of <country>", ...). Matching is whole-word on gazetteer-folded text and the
longest phrase wins (``osint.enrich.phrases``). A lexicon that fails to load raises
``LexiconError``: a silent empty lexicon would make every event look domestic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from osint.config import ROOT
from osint.core import countries
from osint.enrich.gazetteer import name_key
from osint.enrich.phrases import PhraseHit, PhraseMatcher

log = logging.getLogger(__name__)

DEFAULT_ACTORS = ROOT / "config" / "actors.yaml"
DEFAULT_GROUPS = ROOT / "config" / "terror_groups.yaml"
CLASSES = frozenset({"state_military", "state_police", "state_government", "non_state_armed",
                     "protesters", "rioters", "civilians", "other"})
CLASS_NOUN = {"state_military": "forces", "state_police": "police",
              "state_government": "government", "protesters": "protesters",
              "rioters": "rioters", "civilians": "civilians"}


class LexiconError(RuntimeError):
    """The actor lexicon could not be loaded (fatal at startup)."""


@dataclass(frozen=True, slots=True)
class ActorEntry:
    name: str  # canonical actor name ("Russian forces")
    country: str | None  # home country, ISO2
    klass: str  # one of CLASSES


class ActorLexicon:
    def __init__(self, matcher: PhraseMatcher[ActorEntry]) -> None:
        if not len(matcher):
            raise LexiconError("actor lexicon is empty")
        self.matcher = matcher

    def __len__(self) -> int:
        return len(self.matcher)

    @classmethod
    def load(cls, path: Path = DEFAULT_ACTORS, *,
             groups_path: Path = DEFAULT_GROUPS) -> ActorLexicon:
        """Hand-written entries first, then terror-group homes, then generated phrases
        (the first entry of a phrase wins)."""
        try:
            cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise LexiconError(f"actor lexicon {path}: {e}") from e
        if not isinstance(cfg, dict):
            raise LexiconError(f"actor lexicon {path}: not a mapping")
        m: PhraseMatcher[ActorEntry] = PhraseMatcher()
        for source in (_entries(cfg, Path(path)), _terror_groups(cfg, Path(groups_path)),
                       _generated(cfg)):
            for phrase, entry in source:
                m.add(phrase, entry)
        return cls(m)

    def find(self, text: str) -> list[PhraseHit[ActorEntry]]:
        return self.matcher.find(text)

    def lookup(self, name: str) -> ActorEntry | None:
        """The entry of the longest phrase in an actor name ("Military Forces of Sudan
        (2019-)" -> Sudanese forces, SD), or None."""
        hits = self.find(name)
        if not hits:
            return None
        return max(hits, key=lambda h: (h.end - h.start, -h.start)).payload


@lru_cache(maxsize=1)
def default_lexicon() -> ActorLexicon:
    """The lexicon from ``config/`` (cached; used by the ACLED / UCDP writers)."""
    return ActorLexicon.load()


def _country(value: Any, where: str) -> str | None:
    if value is None:
        return None
    c = countries.get(str(value))
    if c is None or len(str(value).strip()) != 2:
        raise LexiconError(f"{where}: unknown ISO2 country {value!r} (quote codes: \"NO\")")
    return c.iso2


def _klass(value: Any, where: str) -> str:
    k = str(value or "")
    if k not in CLASSES:
        raise LexiconError(f"{where}: unknown actor class {value!r}")
    return k


def _entries(cfg: Mapping[str, Any], path: Path) -> Iterator[tuple[str, ActorEntry]]:
    for i, e in enumerate(cfg.get("entries") or []):
        where = f"{path} entries[{i}]"
        phrases = [str(p) for p in (e or {}).get("match") or []]
        if not phrases:
            raise LexiconError(f"{where}: no match phrases")
        entry = ActorEntry(str(e.get("name") or phrases[0]), _country(e.get("country"), where),
                           _klass(e.get("class"), where))
        for p in phrases:
            yield p, entry


def _terror_groups(cfg: Mapping[str, Any], groups_path: Path
                   ) -> Iterator[tuple[str, ActorEntry]]:
    homes = cfg.get("terror_group_homes") or {}
    if not homes:
        return
    from osint.terror.groups import GroupKB  # local: only needed while loading

    kb = GroupKB.load(groups_path)
    for gid, home in homes.items():
        where = f"terror_group_homes.{gid}"
        g = kb.get(str(gid))
        if g is None:
            raise LexiconError(f"{where}: no group {gid!r} in {groups_path}")
        if isinstance(home, Mapping):
            cc, klass = home.get("country"), home.get("class") or "non_state_armed"
        else:
            cc, klass = home, "non_state_armed"
        entry = ActorEntry(g.name, _country(cc, where), _klass(klass, where))
        for p in (g.name, *g.aliases):
            if len(name_key(p)) >= 3:
                yield p, entry


def _generated(cfg: Mapping[str, Any]) -> Iterator[tuple[str, ActorEntry]]:
    gen = cfg.get("generated") or {}
    extra = {str(k): [str(x) for x in v or []]
             for k, v in (cfg.get("extra_adjectives") or {}).items()}
    for k in extra:
        _country(k, f"extra_adjectives.{k}")
    skip = {str(x) for x in gen.get("skip_name_forms") or []}
    blocks = {key: {_klass(k, f"generated.{key}"): [str(x) for x in v or []]
                    for k, v in (gen.get(key) or {}).items()}
              for key in ("nouns", "demonym_nouns", "of_country", "country_suffix")}
    for c in countries.all_countries():
        adjs = list(dict.fromkeys([*c.demonyms, *extra.get(c.iso2, [])]))
        label = adjs[0] if adjs else c.name
        names = [c.name, *c.aliases]
        own = [] if c.iso2 in skip else [c.name]
        for key, table in blocks.items():
            for klass, words in table.items():
                entry = ActorEntry(f"{label} {CLASS_NOUN.get(klass, klass)}", c.iso2, klass)
                for w in words:
                    if key == "nouns":
                        phrases = [f"{a} {w}" for a in (*adjs, *own)]
                    elif key == "demonym_nouns":
                        phrases = [f"{a} {w}" for a in adjs]
                    else:  # "Government of Sudan", "Protesters (Kenya)"
                        phrases = [f"{w} {n}" for n in names]
                    for p in phrases:
                        yield p, entry
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/enrich/test_phrases.py tests/enrich/test_actors.py -v`
Expected: all PASS (the lexicon holds ~22 000 phrases and loads in under a second).

- [ ] **Step 7: Commit**

```bash
git add backend/osint/enrich/phrases.py backend/osint/enrich/actors.py config/actors.yaml tests/enrich/test_phrases.py tests/enrich/test_actors.py
git commit -m "feat(actors): whole-word phrase matcher and actor-country lexicon"
```

---

### Task 5: Actor resolution, derived columns, ACLED/UCDP writers

**Files:**
- Modify: `backend/osint/enrich/actors.py` (append `resolve_actors`, `derive`)
- Modify: `backend/osint/collectors/acled.py` (`map_row` actors / derived), `backend/osint/backfill/ucdp.py` (`map_row` actors / derived)
- Test: `tests/enrich/test_actors.py`, `tests/collectors/test_acled.py`, `tests/backfill/test_ucdp_adsb_backfill.py`

**Interfaces:**
- Consumes: `ActorLexicon.find/lookup`, `default_lexicon()` (Task 4); `to_iso2` (Task 1); `SqliteStore.save_events` derived columns (Task 3).
- Produces:
  - `resolve_actors(event: Mapping[str, Any], docs: Sequence[Any], lexicon: ActorLexicon, llm_actors: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]` — `docs` need `.title` and `.snippet`; each actor dict has `name`, `role`, `type`, `country` (ISO2 | None), `source` (`gdelt|acled|ucdp|rule|llm`), plus `cameo` (gdelt) or `quote` (rule).
  - `derive(actors: Sequence[Mapping[str, Any]], country: str | None) -> dict[str, Any]` — `{"actor_countries": list[str], "foreign_involved": bool, "attributed": bool}`.
  - ACLED / UCDP `map_row` event dicts carry resolved `actors` and the three derived keys.

- [ ] **Step 1: Write the failing tests**

Append to `tests/enrich/test_actors.py`:

```python
from types import SimpleNamespace

from osint.enrich.actors import derive, resolve_actors


def _doc(title, snippet=""):
    return SimpleNamespace(title=title, snippet=snippet)


def test_resolution_order_sources_and_dedupe(lex):
    ev = {"id": "01NEWS", "country": "UA", "actors": [
        {"name": "Russia", "role": "source", "type": "RUS", "country": "RUS"},  # legacy row
        {"name": "Ukraine", "role": "target", "type": "UKR", "country": "UA",
         "cameo": "UKR", "source": "gdelt"},
        {"name": "Stale", "role": "mentioned", "country": "FR", "source": "rule"}]}
    docs = [_doc("Russian shelling of Kyiv kills 3", "Russian shelling continued"),
            _doc("Kyiv under fire")]
    out = resolve_actors(ev, docs, lex,
                         [{"name": "Israel Defense Forces", "role": "perpetrator",
                           "type": None}, {"name": "Unknown gunmen", "role": "other"}])
    assert [(a["name"], a["country"], a["source"]) for a in out] == [
        ("Russia", "RU", "gdelt"), ("Ukraine", "UA", "gdelt"),
        ("Russian forces", "RU", "rule"), ("Israel Defense Forces", "IL", "llm"),
        ("Unknown gunmen", None, "llm")]
    assert out[0]["cameo"] == "RUS" and out[2]["role"] == "mentioned"
    assert out[2]["quote"] == "Russian shelling"
    assert resolve_actors({**ev, "actors": out}, docs, lex, []) == out[:3]  # idempotent


def test_acled_and_ucdp_actors_use_side_names_not_documents(lex):
    ev = {"id": "ucdp-1", "actors": [
        {"name": "Government of Afghanistan", "role": "side_a", "type": "", "country": None},
        {"name": "Civilians", "role": "side_b", "type": "", "country": None}]}
    out = resolve_actors(ev, [_doc("Government of Afghanistan - Civilians")], lex)
    assert [(a["country"], a["source"]) for a in out] == [("AF", "ucdp"), (None, "ucdp")]


def test_derive():
    acts = [{"name": "a", "country": "RU"}, {"name": "b", "country": None},
            {"name": "c", "country": "UA"}, {"name": "d", "country": "RU"}]
    assert derive(acts, "UA") == {"actor_countries": ["RU", "UA"], "foreign_involved": True,
                                  "attributed": True}
    assert derive(acts[:2], "ru")["foreign_involved"] is False
    assert derive([], "UA") == {"actor_countries": [], "foreign_involved": False,
                                "attributed": False}
    assert derive(acts, None)["foreign_involved"] is False  # no location: not "foreign"
```

In `tests/collectors/test_acled.py` replace the line added in Task 2 (`assert [a["source"] for a in e["actors"]] == ["acled", "acled"]`) with:

```python
    assert [(a["country"], a["source"]) for a in e["actors"]] == [("SD", "acled"),
                                                                 ("SD", "acled")]
    assert e["actor_countries"] == ["SD"] and e["attributed"] is True
    assert e["foreign_involved"] is False
```

and append:

```python
def test_map_row_derived_columns_are_stored(tmp_path):
    m = map_row(acled_row(actor2="Military Forces of Chad (2021-)"), NOW)
    store = SqliteStore(tmp_path / "s.sqlite")
    store.save_events([m["event"]], [m["document"]], m["fields"])
    e = store.get_event("acled-SDN12345")
    assert e["actor_countries"] == ["SD", "TD"] and e["foreign_involved"] == 1
    store.close()
```

In `tests/backfill/test_ucdp_adsb_backfill.py` replace the line added in Task 2 with:

```python
    assert ev["country"] == "AF"
    assert [(a["country"], a["source"]) for a in ev["actors"]] == [("AF", "ucdp"),
                                                                  (None, "ucdp")]
    assert ev["actor_countries"] == ["AF"] and ev["foreign_involved"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/enrich/test_actors.py tests/collectors/test_acled.py tests/backfill/test_ucdp_adsb_backfill.py -v`
Expected: FAIL with `ImportError: cannot import name 'derive' from 'osint.enrich.actors'`.

- [ ] **Step 3: Implement resolution and derive**

In `backend/osint/enrich/actors.py` change `from collections.abc import Iterator, Mapping` to `from collections.abc import Iterator, Mapping, Sequence` and append:

```python
# ------------------------------------------------------------------- resolution


def _base_source(event: Mapping[str, Any]) -> str:
    eid = str(event.get("id") or "")
    return "acled" if eid.startswith("acled-") else "ucdp" if eid.startswith("ucdp-") \
        else "gdelt"


def resolve_actors(event: Mapping[str, Any], docs: Sequence[Any], lexicon: ActorLexicon,
                   llm_actors: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The event's actors, each with ``country`` (ISO2 or None) and ``source``
    (gdelt | acled | ucdp | rule | llm), duplicates removed by (name, country), in order:

    1. GDELT actors: the CAMEO country code (kept as ``cameo``) -> ISO2;
    2. ACLED / UCDP side names through the lexicon;
    3. lexicon hits in every document's title and snippet, role ``mentioned`` (news and
       sensor events only: an ACLED / UCDP document is generated from the side names);
    4. the LLM extraction's actor names through the lexicon.

    ``docs`` are ``osint.enrich.rules.base.ExtractDoc``-like (``title``, ``snippet``).
    Earlier ``rule`` / ``llm`` entries in ``event['actors']`` are dropped and re-derived,
    so the result does not depend on how often it ran."""
    base = _base_source(event)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    def add(actor: dict[str, Any]) -> None:
        key = (name_key(str(actor.get("name") or "")), actor.get("country"))
        if key[0] and key not in seen:
            seen.add(key)
            out.append(actor)

    for a in event.get("actors") or []:
        src = str(a.get("source") or base)
        if src in ("rule", "llm"):
            continue
        if src == "gdelt":
            # rows written before the registry kept the CAMEO code in "country"
            cameo = str((a["cameo"] if "cameo" in a else a.get("country")) or "").strip().upper()
            add({**a, "country": countries.to_iso2(cameo, "cameo") if cameo else None,
                 "cameo": cameo, "source": "gdelt"})
        else:
            hit = lexicon.lookup(str(a.get("name") or ""))
            add({**a, "country": hit.country if hit else None, "source": src})
    if base == "gdelt":
        for d in docs:
            for text in (getattr(d, "title", ""), getattr(d, "snippet", "")):
                for h in lexicon.find(text or ""):
                    e = h.payload
                    add({"name": e.name, "role": "mentioned", "type": e.klass,
                         "country": e.country, "source": "rule", "quote": h.text})
    for a in llm_actors:
        nm = str(a.get("name") or "").strip()
        if not nm:
            continue
        hit = lexicon.lookup(nm)
        add({"name": nm, "role": str(a.get("role") or "other"),
             "type": str(a.get("type") or (hit.klass if hit else "")),
             "country": hit.country if hit else None, "source": "llm"})
    return out


def derive(actors: Sequence[Mapping[str, Any]], country: str | None) -> dict[str, Any]:
    """``actor_countries`` (sorted ISO2, nulls dropped), ``foreign_involved`` (an actor
    country differs from the event's country) and ``attributed`` (any actor country)."""
    ccs = sorted({str(a["country"]) for a in actors if a.get("country")})
    home = (country or "").strip().upper()
    return {"actor_countries": ccs,
            "foreign_involved": bool(home) and any(c != home for c in ccs),
            "attributed": bool(ccs)}
```

- [ ] **Step 4: Resolve actors in the ACLED and UCDP writers**

In `backend/osint/collectors/acled.py` add the import:

```python
from osint.enrich.actors import default_lexicon, derive, resolve_actors
```

In `map_row`, directly before `event = {`, add:

```python
    cc = to_iso2(country, "name") if country else None
    actors = resolve_actors({"id": event_id, "actors": [a for a in (
        {"name": actor1, "role": "actor1", "type": _s(row, "inter1"), "source": SOURCE},
        {"name": actor2, "role": "actor2", "type": _s(row, "inter2"), "source": SOURCE})
        if a["name"]]}, (), default_lexicon())
```

and in the `event` dict replace the whole `"actors": [...]` entry with `"actors": actors,`, replace `"country": to_iso2(country, "name") if country else None,` with `"country": cc,`, and add after `"cluster_state": {...},` (still inside the dict):

```python
        **derive(actors, cc),
```

In `backend/osint/backfill/ucdp.py` add the import:

```python
from osint.enrich.actors import default_lexicon, derive, resolve_actors
```

In `map_row`, directly before `event = {`, add:

```python
    cc = to_iso2(country, "name") if country else None
    actors = resolve_actors({"id": event_id, "actors": [a for a in (
        {"name": side_a, "role": "side_a", "type": "", "source": SOURCE},
        {"name": side_b, "role": "side_b", "type": "", "source": SOURCE}) if a["name"]]},
        (), default_lexicon())
```

and in the `event` dict replace the `"actors": [...]` entry with `"actors": actors,`, replace `"country": to_iso2(country, "name") if country else None,` with `"country": cc,`, and add after the `"cluster_state": {...},` entry:

```python
        **derive(actors, cc),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/enrich tests/collectors tests/backfill -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/osint/enrich/actors.py backend/osint/collectors/acled.py backend/osint/backfill/ucdp.py tests/enrich/test_actors.py tests/collectors/test_acled.py tests/backfill/test_ucdp_adsb_backfill.py
git commit -m "feat(actors): resolve actor countries (GDELT/ACLED/UCDP/rule/LLM) and derive actor_countries"
```

---

### Task 6: Rule extractors — headline, summary, weapons

**Files:**
- Create: `backend/osint/enrich/rules/__init__.py`, `backend/osint/enrich/rules/base.py`, `backend/osint/enrich/rules/text.py`, `backend/osint/enrich/rules/weapons.py`
- Create: `config/extract_rules.yaml`
- Test: `tests/enrich/rules/__init__.py` (empty), `tests/enrich/rules/test_text.py`, `tests/enrich/rules/test_weapons.py`, `tests/enrich/rules/test_ruleset.py`

**Interfaces:**
- Consumes: `PhraseMatcher`, `PhraseHit` (Task 4).
- Produces:
  - `osint.enrich.rules.base`: `@dataclass(frozen=True, slots=True) class FieldValue: field: str; value: str; doc_id: str; quote: str` with `row(event_id: str) -> dict[str, str]`; `@dataclass(frozen=True, slots=True) class ExtractDoc: doc_id: str; title: str; snippet: str = ""; url: str = ""; source: str = ""; published_at: str = ""; role: str = "mention"; language: str = ""; vector: Any = None` with properties `domain: str`, `text: str`.
  - `osint.enrich.rules.text`: `pick_document(event, docs) -> ExtractDoc | None`, `cut_summary(text: str, max_chars: int = 280) -> str`, `headline_summary(event, docs, *, max_chars: int = 280) -> list[FieldValue]`.
  - `osint.enrich.rules.weapons`: `WeaponLexicon(labels, *, subsumed=None, ignore=())` with `hits(text)`, `__len__`; `weapons(event, docs, *, lexicon) -> list[FieldValue]`.
  - `osint.enrich.rules`: `RULE_FIELDS = ("headline", "summary", "casualties.killed", "casualties.injured", "weapons_platforms")`, `DEFAULT_RULES`, `Extractor` type, `@dataclass class RuleSet: summary_max_chars: int; weapons: WeaponLexicon` with `RuleSet.load(path: Path = DEFAULT_RULES) -> RuleSet`, `extractors() -> list[tuple[str, Extractor]]`, `run(event: Mapping[str, Any], docs: Sequence[ExtractDoc]) -> tuple[list[FieldValue], list[str]]` (values, names of extractors that raised). `event` keys used: `id`, `vector` (centroid or None).

- [ ] **Step 1: Write the failing tests**

Create `tests/enrich/rules/__init__.py` (empty file).

Create `tests/enrich/rules/test_text.py`:

```python
import numpy as np

from osint.enrich.rules.base import ExtractDoc
from osint.enrich.rules.text import cut_summary, headline_summary, pick_document


def _d(i, title, *, role="mention", at="2026-11-25T10:00:00.000+00:00", vec=None, snippet=""):
    return ExtractDoc(doc_id=f"d{i}", title=title, snippet=snippet, role=role,
                      published_at=at, vector=vec)


def test_primary_document_without_embeddings():
    docs = [_d(1, "Early mention", at="2026-11-25T09:00:00.000+00:00"),
            _d(2, "Primary report", role="primary")]
    assert pick_document({}, docs).doc_id == "d2"


def test_earliest_document_when_there_is_no_primary():
    docs = [_d(1, "Later", at="2026-11-25T11:00:00.000+00:00"),
            _d(2, "Earlier", at="2026-11-25T08:00:00.000+00:00"), _d(3, "  ")]
    assert pick_document({}, docs).doc_id == "d2"
    assert pick_document({}, [_d(1, "")]) is None and pick_document({}, []) is None


def test_document_nearest_the_centroid_wins():
    centroid = np.array([1.0, 0.0])
    docs = [_d(1, "Off topic", role="primary", vec=np.array([0.0, 1.0])),
            _d(2, "On topic", vec=np.array([0.9, 0.1])), _d(3, "No vector")]
    assert pick_document({"vector": centroid}, docs).doc_id == "d2"
    no_vectors = [_d(1, "A", role="primary"), _d(2, "B")]
    assert pick_document({"vector": centroid}, no_vectors).doc_id == "d1"


def test_cut_summary_at_a_sentence_boundary():
    assert cut_summary("  Short   text. ") == "Short text."
    long = "Sentence number one is here. " * 20
    out = cut_summary(long)
    assert len(out) <= 280 and out.endswith(".") and out.count(".") == 9
    words = cut_summary("word " * 100)
    assert len(words) <= 280 and words.endswith("…") and not words.endswith(" …")


def test_headline_and_summary_values():
    docs = [_d(1, "Missile hits Kharkiv", role="primary",
               snippet="A missile hit Kharkiv overnight. Officials said two people died.")]
    vals = headline_summary({}, docs)
    assert [(v.field, v.value, v.doc_id) for v in vals] == [
        ("headline", "Missile hits Kharkiv", "d1"),
        ("summary", "A missile hit Kharkiv overnight. Officials said two people died.", "d1")]
    assert vals[0].quote == "Missile hits Kharkiv"
    assert [v.field for v in headline_summary({}, [_d(1, "Title only")])] == ["headline"]
```

Create `tests/enrich/rules/test_weapons.py`:

```python
import pytest

from osint.enrich.rules import RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.enrich.rules.weapons import weapons


@pytest.fixture(scope="module")
def lexicon():
    return RuleSet.load().weapons


@pytest.mark.parametrize(("title", "labels"), [
    ("Russian drones and a Kh-101 cruise missile hit Kyiv", ["cruise missile", "drone"]),
    ("Missile strike on Dnipro", ["missile"]),
    ("Ballistic missile and more missiles", ["ballistic missile"]),
    ("Think tank warns of tank shortage", ["tank"]),
    ("Car bomb kills 5", ["VBIED"]),
    ("Удар баллистической ракетой по Одессе", ["ballistic missile"]),
    ("غارة جوية على غزة", ["airstrike"]),
    ("Shahed drones attack Odesa", ["drone"]),
    ("Sea drone hits Russian warship", ["naval drone", "warship"]),
    ("Shell company profits rise", [])])
def test_weapon_labels(lexicon, title, labels):
    vals = weapons({}, [ExtractDoc(doc_id="d1", title=title)], lexicon=lexicon)
    assert [v.value for v in vals] == labels
    assert all(v.field == "weapons_platforms" and v.doc_id == "d1" for v in vals)


def test_first_mention_is_quoted(lexicon):
    docs = [ExtractDoc(doc_id="late", title="Drones again", published_at="2026-11-25T12"),
            ExtractDoc(doc_id="early", title="x", snippet="FPV drones struck a tank",
                       published_at="2026-11-25T09")]
    vals = {v.value: v for v in weapons({}, docs, lexicon=lexicon)}
    assert vals["drone"].doc_id == "early" and vals["drone"].quote == "FPV drones"
```

Create `tests/enrich/rules/test_ruleset.py`:

```python
from osint.enrich.rules import RULE_FIELDS, RuleSet
from osint.enrich.rules.base import ExtractDoc


def test_ruleset_runs_every_extractor_and_isolates_failures(monkeypatch):
    rs = RuleSet.load()
    docs = [ExtractDoc(doc_id="d1", title="Drone strike on Kharkiv", role="primary",
                       url="https://a.example/1")]
    vals, errors = rs.run({"id": "E1"}, docs)
    assert errors == [] and {v.field for v in vals} >= {"headline", "weapons_platforms"}
    assert {v.field for v in vals} <= set(RULE_FIELDS)

    def boom(event, docs):
        raise RuntimeError("bad document")

    good = rs.extractors()
    monkeypatch.setattr(rs, "extractors", lambda: [("boom", boom), *good])
    vals2, errors2 = rs.run({"id": "E1"}, docs)
    assert errors2 == ["boom"] and vals2 == vals
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/enrich/rules -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.enrich.rules'`.

- [ ] **Step 3: Write the shared types**

Create `backend/osint/enrich/rules/base.py`:

```python
"""Shared types of the deterministic rule extractors (osint.enrich.rules)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

QUOTE_MAX = 300


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One rule-extracted value and its evidence (the matched span), stored in event_fields
    with source='rule'."""

    field: str
    value: str
    doc_id: str
    quote: str

    def row(self, event_id: str) -> dict[str, str]:
        return {"event_id": event_id, "field": self.field, "value": self.value,
                "doc_id": self.doc_id, "quote": self.quote[:QUOTE_MAX]}


@dataclass(frozen=True, slots=True)
class ExtractDoc:
    """A document linked to an event, as the rule extractors and actor resolution see it."""

    doc_id: str
    title: str
    snippet: str = ""
    url: str = ""
    source: str = ""
    published_at: str = ""  # UTC ISO-8601 (event_documents.published_at; sorts lexically)
    role: str = "mention"  # primary | mention
    language: str = ""
    vector: Any = None  # sentence embedding of title + snippet, when an embedder is on

    @property
    def domain(self) -> str:
        host = (urlsplit(self.url).hostname or "").lower().removeprefix("www.")
        return host or self.source or self.doc_id

    @property
    def text(self) -> str:
        return f"{self.title}. {self.snippet}" if self.snippet else self.title
```

- [ ] **Step 4: Write headline / summary**

Create `backend/osint/enrich/rules/text.py`:

```python
"""Headline and summary rules (deterministic extraction spec §2.4): the title of the
document nearest the event centroid (embeddings), else the primary document's, else the
earliest; the summary is that document's snippet cut at a sentence boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from osint.enrich.rules.base import QUOTE_MAX, ExtractDoc, FieldValue

SUMMARY_MAX = 280
_SENT_END = re.compile(r"[.!?。！？؟](?=\s|$)")


def _cosine(a: Any, b: Any) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    den = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / den if den else -1.0


def pick_document(event: Mapping[str, Any], docs: Sequence[ExtractDoc]) -> ExtractDoc | None:
    """The document whose vector is nearest ``event['vector']`` (the cluster centroid);
    without vectors the primary document, else the earliest. Documents without a title
    are never picked."""
    cands = [d for d in docs if (d.title or "").strip()]
    if not cands:
        return None
    centroid = event.get("vector")
    if centroid is not None:
        scored = [(_cosine(d.vector, centroid), d) for d in cands if d.vector is not None]
        if scored:
            return min(scored, key=lambda t: (-t[0], t[1].published_at, t[1].doc_id))[1]
    primary = [d for d in cands if d.role == "primary"]
    return min(primary or cands, key=lambda d: (d.published_at, d.doc_id))


def cut_summary(text: str, max_chars: int = SUMMARY_MAX) -> str:
    """Whitespace-collapsed ``text`` of at most ``max_chars``: whole sentences when one ends
    in the last two thirds of the limit, else whole words and an ellipsis."""
    t = " ".join((text or "").split())
    if len(t) <= max_chars:
        return t
    window = t[:max_chars + 1]
    ends = [m.end() for m in _SENT_END.finditer(window) if m.end() <= max_chars]
    if ends and ends[-1] >= max_chars // 3:
        return window[:ends[-1]]
    cut = t[:max_chars - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-–—") + "…"


def headline_summary(event: Mapping[str, Any], docs: Sequence[ExtractDoc], *,
                     max_chars: int = SUMMARY_MAX) -> list[FieldValue]:
    d = pick_document(event, docs)
    if d is None:
        return []
    title = " ".join(d.title.split())
    out = [FieldValue("headline", title, d.doc_id, title[:QUOTE_MAX])]
    summary = cut_summary(d.snippet, max_chars)
    if summary:
        out.append(FieldValue("summary", summary, d.doc_id, summary[:QUOTE_MAX]))
    return out
```

- [ ] **Step 5: Write the weapons extractor**

Create `backend/osint/enrich/rules/weapons.py`:

```python
"""Weapons / platforms by lexicon (deterministic extraction spec §2.4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from osint.enrich.phrases import PhraseHit, PhraseMatcher
from osint.enrich.rules.base import ExtractDoc, FieldValue

_IGNORE = ""  # payload of phrases that contain a weapon word but are no weapon ("think tank")


class WeaponLexicon:
    def __init__(self, labels: Mapping[str, Sequence[str]], *,
                 subsumed: Mapping[str, Sequence[str]] | None = None,
                 ignore: Sequence[str] = ()) -> None:
        self.matcher: PhraseMatcher[str] = PhraseMatcher()
        for phrase in ignore:
            self.matcher.add(str(phrase), _IGNORE)
        for label, phrases in labels.items():
            for phrase in phrases or ():
                self.matcher.add(str(phrase), str(label))
        self.subsumed = {str(k): {str(x) for x in v or ()} for k, v in (subsumed or {}).items()}

    def __len__(self) -> int:
        return len(self.matcher)

    def hits(self, text: str) -> list[PhraseHit[str]]:
        return [h for h in self.matcher.find(text) if h.payload != _IGNORE]


def weapons(event: Mapping[str, Any], docs: Sequence[ExtractDoc], *,
            lexicon: WeaponLexicon) -> list[FieldValue]:
    """One ``weapons_platforms`` value per canonical label, quoting its first mention
    (documents oldest first); a generic label is dropped when a more specific one it
    subsumes was found ("missile" next to "cruise missile")."""
    first: dict[str, FieldValue] = {}
    for d in sorted(docs, key=lambda d: (d.published_at, d.doc_id)):
        for text in (d.title, d.snippet):
            for h in lexicon.hits(text or ""):
                first.setdefault(h.payload, FieldValue("weapons_platforms", h.payload,
                                                       d.doc_id, h.text))
    drop = {g for g, specific in lexicon.subsumed.items() if g in first and specific & set(first)}
    return [v for label, v in sorted(first.items()) if label not in drop]
```

- [ ] **Step 6: Write the rule set and its config**

Create `config/extract_rules.yaml`:

```yaml
# Deterministic rule extractors (osint.enrich.rules), written to event_fields with
# source='rule'. A value from an LLM extraction whose verdict is supported outranks these
# in the API (field_source).

summary_max_chars: 280   # the summary is the picked document's snippet cut at a sentence end

# Weapons / platforms: canonical label -> phrases (whole words on case-, diacritic- and
# punctuation-folded text; the longest phrase wins; a trailing * makes the last word a
# prefix). Quote phrases with * or punctuation.
weapons:
  drone: [drone, drones, uav, uavs, "unmanned aerial vehicle*", "kamikaze drone*",
          "attack drone*", "loitering munition*", "fpv drone*", "shahed*", "geran*", "шахед*",
          "герань*", "беспилотник*", "бпла", "дрон*", "безпілотник*", "طائرة مسيرة",
          "طائرات مسيرة", "طائرة بدون طيار", "طائرات بدون طيار", "مسيرة مفخخة", "dron",
          "drones kamikazes"]
  naval drone: ["sea drone*", "naval drone*", "unmanned surface vessel*", usv, usvs,
                "морской дрон*", "морський дрон*"]
  ballistic missile: ["ballistic missile*", "iskander*", "kn-23", "fateh-110", "atacms",
                      "баллистическая ракета", "баллистической ракетой", "баллистические ракеты",
                      "баллистических ракет", "балістична ракета", "балістичні ракети",
                      "балістичних ракет", "صاروخ باليستي", "صواريخ باليستية",
                      "missile balistique", "missiles balistiques", "misil balístico",
                      "misiles balísticos"]
  cruise missile: ["cruise missile*", "kalibr*", "kh-101", "kh-555", "х-101", "tomahawk*",
                   "storm shadow*", "scalp", "крылатая ракета", "крылатые ракеты",
                   "крылатых ракет", "крилата ракета", "крилаті ракети", "крилатих ракет",
                   "صاروخ كروز", "صواريخ كروز", "missile de croisière", "missiles de croisière",
                   "misil de crucero", "misiles de crucero"]
  missile: [missile, missiles, ракета, ракеты, ракет, ракетой, ракети, ракетами, صاروخ,
            صواريخ, misil, misiles]
  rocket: [rocket, rockets, "roquette*", "cohete*"]
  MLRS: [mlrs, "multiple rocket launcher*", "himars", "grad", "smerch", "uragan", "tornado-s",
         "рсзо", "град", "ураган", "смерч", "راجمة صواريخ", "راجمات صواريخ"]
  artillery: [artillery, shelling, shells, "howitzer*", "артиллери*", "артобстрел*",
              "артилері*", "артобстріл*", "قصف مدفعي", "مدفعية", "artillerie", "obus",
              "artillería"]
  mortar: ["mortar*", "миномет*", "мінометн*", "قذائف هاون", "هاون", "mortier*", "mortero*"]
  airstrike: ["airstrike*", "air strike*", "air raid*", "aerial bombardment",
              "aerial bombing", "авиаудар*", "авиационный удар", "авіаудар*", "غارة جوية",
              "غارات جوية", "غارة", "غارات", "frappe aérienne", "frappes aériennes",
              "bombardement aérien", "ataque aéreo", "ataques aéreos", "bombardeo aéreo"]
  IED: [ied, ieds, "improvised explosive device*", "roadside bomb*",
        "самодельное взрывное устройство", "сву", "عبوة ناسفة", "عبوات ناسفة",
        "engin explosif improvisé", "artefacto explosivo improvisado"]
  VBIED: [vbied, vbieds, "car bomb*", "vehicle-borne ied*", "vehicle borne ied*", "truck bomb*",
          "suicide car bomb*", "سيارة مفخخة", "voiture piégée", "coche bomba"]
  tank: [tank, tanks, "танк*", دبابة, دبابات]
  frigate: ["frigate*", "фрегат*", فرقاطة, "frégate*", "fragata*"]
  destroyer: ["destroyer*", "эсминец", "эсминца", "есмінець", مدمرة]
  corvette: ["corvette*", "корвет*", "corbeta*"]
  submarine: ["submarine*", "подводная лодка", "подводной лодки", "подводную лодку",
              "подлодк*", "підводний човен", "підводного човна", غواصة, "sous-marin*",
              "submarino*"]
  aircraft carrier: ["aircraft carrier*", "авианос*", "авіанос*", "حاملة طائرات",
                     "porte-avions", "portaaviones"]
  landing ship: ["landing ship*", "amphibious assault ship*", "большой десантный корабль",
                 "бдк", "десантний корабель"]
  patrol boat: ["patrol boat*", "patrol vessel*", "fast attack craft", "патрульный катер",
                "сторожевой корабль", "زورق دورية"]
  warship: ["warship*", "naval vessel*", "military vessel*", "военный корабль",
            "военные корабли", "военного корабля", "військовий корабель", "бойовий корабель",
            "سفينة حربية", "سفن حربية", "navire de guerre", "buque de guerra"]
# a generic label is dropped when a more specific one is found in the same event
weapons_subsumed:
  missile: [ballistic missile, cruise missile]
  drone: [naval drone]
  rocket: [MLRS]
  IED: [VBIED]
  warship: [frigate, destroyer, corvette, submarine, aircraft carrier, landing ship, patrol boat]
# phrases that contain a weapon word but are not weapons
weapons_ignore: ["think tank*", "oil tank*", "fuel tank*", "water tank*", "storage tank*",
                 "septic tank*", "gas tank*"]
```

Create `backend/osint/enrich/rules/__init__.py`:

```python
"""Deterministic rule extractors (deterministic extraction spec §2.4).

Each extractor is a pure function ``(event, docs) -> list[FieldValue]``; the values are
written to ``event_fields`` with ``source='rule'`` and the matched span as ``quote``
(``osint.pipeline.rule_pass``). Configuration: ``config/extract_rules.yaml``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from osint.config import ROOT
from osint.enrich.rules.base import ExtractDoc, FieldValue
from osint.enrich.rules.text import SUMMARY_MAX, headline_summary
from osint.enrich.rules.weapons import WeaponLexicon, weapons

log = logging.getLogger(__name__)

DEFAULT_RULES = ROOT / "config" / "extract_rules.yaml"
RULE_FIELDS = ("headline", "summary", "casualties.killed", "casualties.injured",
               "weapons_platforms")
Extractor = Callable[[Mapping[str, Any], Sequence[ExtractDoc]], list[FieldValue]]


@dataclass
class RuleSet:
    summary_max_chars: int
    weapons: WeaponLexicon

    @classmethod
    def load(cls, path: Path = DEFAULT_RULES) -> RuleSet:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        lex = WeaponLexicon(cfg.get("weapons") or {}, subsumed=cfg.get("weapons_subsumed"),
                            ignore=cfg.get("weapons_ignore") or ())
        if not len(lex):
            raise ValueError(f"{path}: empty weapons lexicon")
        return cls(summary_max_chars=int(cfg.get("summary_max_chars", SUMMARY_MAX)),
                   weapons=lex)

    def extractors(self) -> list[tuple[str, Extractor]]:
        return [("headline_summary", partial(headline_summary,
                                             max_chars=self.summary_max_chars)),
                ("weapons", partial(weapons, lexicon=self.weapons))]

    def run(self, event: Mapping[str, Any], docs: Sequence[ExtractDoc]
            ) -> tuple[list[FieldValue], list[str]]:
        """Every extractor's values; one that raises is skipped (and logged) for this
        event only. Returns (values, names of the extractors that failed)."""
        out: list[FieldValue] = []
        failed: list[str] = []
        for name, fn in self.extractors():
            try:
                out += fn(event, docs)
            except Exception:
                log.exception("rule extractor %s failed for event %s", name, event.get("id"))
                failed.append(name)
        return out, failed
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/enrich/rules -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/osint/enrich/rules config/extract_rules.yaml tests/enrich/rules
git commit -m "feat(rules): headline/summary and weapons/platforms rule extractors"
```

---

### Task 7: Casualties extractor

**Files:**
- Create: `backend/osint/enrich/rules/casualties.py`
- Modify: `config/extract_rules.yaml` (append `casualties:`), `backend/osint/enrich/rules/__init__.py` (`RuleSet.casualties`, extractor list)
- Test: `tests/enrich/rules/test_casualties.py`

**Interfaces:**
- Consumes: `ExtractDoc`, `FieldValue` (Task 6).
- Produces: `CasualtyRules(cfg: Mapping[str, Any])` with `doc_value(field: str, text: str) -> tuple[int, str] | None`; `casualties(event, docs, *, rules: CasualtyRules) -> list[FieldValue]` (fields `casualties.killed`, `casualties.injured`); `RuleSet.casualties: CasualtyRules`; `RuleSet.extractors()` now also yields `("casualties", ...)`.

- [ ] **Step 1: Write the failing test**

Create `tests/enrich/rules/test_casualties.py`:

```python
import pytest

from osint.enrich.rules import RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.enrich.rules.casualties import casualties


@pytest.fixture(scope="module")
def rules():
    return RuleSet.load().casualties


@pytest.mark.parametrize(("text", "field", "value"), [
    ("Missile strike kills 12 in Kharkiv", "killed", 12),
    ("At least 12 people were killed in the attack", "killed", 12),
    ("12 dead after shelling", "killed", 12),
    ("Three people were killed in Kharkiv", "killed", 3),
    ("Death toll rises to 17", "killed", 17),
    ("1,200 killed in floods", "killed", 1200),
    ("killing 3 and injuring 12", "injured", 12),
    ("Strike on Kharkiv, 3 injured", "injured", 3),
    ("Frappe sur Kharkiv : 12 morts et 30 blessés", "killed", 12),
    ("Frappe sur Kharkiv : 12 morts et 30 blessés", "injured", 30),
    ("Ataque deja 7 muertos y 15 heridos", "killed", 7),
    ("Ataque deja 7 muertos y 15 heridos", "injured", 15),
    ("مقتل 12 شخصا في غارة إسرائيلية", "killed", 12),
    ("قتل 12", "killed", 12),
    ("مقتل ١٢ شخصا", "killed", 12),
    ("مقتل ثلاثة أشخاص وإصابة خمسة", "killed", 3),
    ("مقتل ثلاثة أشخاص وإصابة خمسة", "injured", 5),
    ("В результате удара погибли 12 человек, ранены 30", "killed", 12),
    ("В результате удара погибли 12 человек, ранены 30", "injured", 30),
    ("погибли пять человек", "killed", 5),
    ("Унаслідок атаки загинули 5 людей, ще 12 поранені", "killed", 5),
    ("Унаслідок атаки загинули 5 людей, ще 12 поранені", "injured", 12),
    ("загинули п’ять людей", "killed", 5),
    ("A 12-year-old was killed", "killed", None),
    ("Nobody was hurt", "injured", None)])
def test_patterns_per_language(rules, text, field, value):
    hit = rules.doc_value(field, text)
    assert (hit[0] if hit else None) == value


def test_median_over_distinct_domains_ignoring_silent_documents(rules):
    docs = [ExtractDoc("a", "Strike kills 10", url="https://a.example/1", published_at="1"),
            ExtractDoc("b", "12 killed in strike", url="https://b.example/1", published_at="2"),
            ExtractDoc("c1", "15 dead", url="https://c.example/1", published_at="3"),
            ExtractDoc("c2", "Death toll rises to 17", url="https://www.c.example/2",
                       published_at="4"),
            ExtractDoc("d", "No casualties reported", url="https://d.example/1",
                       published_at="5")]
    [v] = casualties({}, docs, rules=rules)
    # domains: a=10, b=12, c=max(15, 17)=17 -> lower median 12
    assert (v.field, v.value, v.doc_id, v.quote) == ("casualties.killed", "12", "b",
                                                     "12 killed")


def test_no_numbers_no_values(rules):
    assert casualties({}, [ExtractDoc("a", "Explosion reported in Kharkiv")],
                      rules=rules) == []


def test_ruleset_includes_casualties():
    rs = RuleSet.load()
    assert "casualties" in [name for name, _ in rs.extractors()]
    vals, _ = rs.run({"id": "E1"}, [ExtractDoc("a", "Drone strike kills 4",
                                               url="https://a.example/1")])
    assert ("casualties.killed", "4") in {(v.field, v.value) for v in vals}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/enrich/rules/test_casualties.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.enrich.rules.casualties'`.

- [ ] **Step 3: Append the patterns to `config/extract_rules.yaml`**

```yaml

# Casualties: regex templates per field and language, matched on the case-folded title and
# snippet; {n} is a number (digits, "1,200", Arabic-Indic digits, or a number word below).
# Per document the largest number counts; per event the lower median over distinct domains.
casualties:
  max_value: 100000
  number_words:
    en: {one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9,
         ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15,
         sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20}
    fr: {un: 1, une: 1, deux: 2, trois: 3, quatre: 4, cinq: 5, six: 6, sept: 7, huit: 8,
         neuf: 9, dix: 10, onze: 11, douze: 12, treize: 13, quatorze: 14, quinze: 15,
         seize: 16, dix-sept: 17, dix-huit: 18, dix-neuf: 19, vingt: 20}
    # "once" (11) is left out: it is an English word
    es: {uno: 1, una: 1, dos: 2, tres: 3, cuatro: 4, cinco: 5, seis: 6, siete: 7, ocho: 8,
         nueve: 9, diez: 10, doce: 12, trece: 13, catorce: 14, quince: 15, dieciséis: 16,
         diecisiete: 17, dieciocho: 18, diecinueve: 19, veinte: 20}
    ru: {один: 1, одна: 1, два: 2, две: 2, три: 3, четыре: 4, пять: 5, шесть: 6, семь: 7,
         восемь: 8, девять: 9, десять: 10, одиннадцать: 11, двенадцать: 12, тринадцать: 13,
         четырнадцать: 14, пятнадцать: 15, шестнадцать: 16, семнадцать: 17,
         восемнадцать: 18, девятнадцать: 19, двадцать: 20}
    uk: {один: 1, одна: 1, два: 2, дві: 2, три: 3, чотири: 4, "п'ять": 5, шість: 6, сім: 7,
         вісім: 8, "дев'ять": 9, десять: 10, одинадцять: 11, дванадцять: 12, тринадцять: 13,
         чотирнадцять: 14, "п'ятнадцять": 15, шістнадцять: 16, сімнадцять: 17,
         вісімнадцять: 18, "дев'ятнадцять": 19, двадцять: 20}
    ar: {واحد: 1, اثنان: 2, اثنين: 2, ثلاثة: 3, ثلاث: 3, أربعة: 4, أربع: 4, خمسة: 5, خمس: 5,
         ستة: 6, ست: 6, سبعة: 7, سبع: 7, ثمانية: 8, ثماني: 8, تسعة: 9, تسع: 9, عشرة: 10,
         عشر: 10, أحد عشر: 11, اثنا عشر: 12, ثلاثة عشر: 13, أربعة عشر: 14, خمسة عشر: 15,
         ستة عشر: 16, سبعة عشر: 17, ثمانية عشر: 18, تسعة عشر: 19, عشرون: 20, عشرين: 20}
  killed:
    en:
      - '\b(?:killed|kills|killing|kill)\s+(?:at\s+least\s+|some\s+|about\s+|around\s+|nearly\s+|more\s+than\s+|over\s+)?{n}\b'
      - '\b{n}(?:\s+(?:more|other|others|people|persons|civilians|soldiers|servicemen|troops|militants|fighters|children|women|police(?:\s+officers)?|officers|workers|migrants|passengers|residents))?(?:\s+(?:were|was|are|have\s+been|has\s+been|had\s+been|reportedly))*\s+(?:killed|dead|died|confirmed\s+dead)\b'
      - '\bdeath\s+toll\s+(?:has\s+)?(?:rises|rose|risen|climbs|climbed|reaches|reached|stands|stood|of|is|at|to)(?:\s+(?:to|at))?\s+(?:at\s+least\s+)?{n}\b'
      - '\b{n}\s+deaths?\b'
    fr:
      - '\b{n}(?:\s+(?:personnes|civils|soldats|militaires|enfants))?(?:\s+(?:ont\s+été|ont\s+ete|sont|auraient\s+été))?\s+(?:morts?|tués?|tuées?|décédés?|décès)\b'
      - '\b(?:tué|tuant|tue)\s+(?:au\s+moins\s+|plus\s+de\s+|près\s+de\s+)?{n}\b'
      - '\bbilan\s+(?:de|est\s+de|s''élève\s+à|grimpe\s+à|passe\s+à)\s+(?:au\s+moins\s+)?{n}\s+morts?\b'
    es:
      - '\b{n}(?:\s+(?:personas|civiles|soldados|militares|niños|mujeres))?(?:\s+(?:fueron|han\s+sido|resultaron))?\s+(?:muertos?|muertas?|fallecidos?|fallecidas?|asesinados?|asesinadas?)\b'
      - '\b(?:mató|matando|mata)\s+a\s+(?:al\s+menos\s+|más\s+de\s+)?{n}\b'
      - '\b(?:murieron|mueren|fallecieron)\s+(?:al\s+menos\s+)?{n}\b'
    ru:
      - '(?<!\w)(?:погибли|погибло|погиб|погибла|убиты|убито|убит)\s+(?:по\s+меньшей\s+мере\s+|не\s+менее\s+|как\s+минимум\s+|более\s+|около\s+)?{n}\b'
      - '\b{n}(?:\s+(?:человек|человека|людей|мирных\s+жителей|военных|военнослужащих|детей))?\s+(?:погибли|погибло|погибших|убиты|убито|убитых)\b'
    uk:
      - '(?<!\w)(?:загинули|загинуло|загинув|загинула|вбито|убито|вбиті)\s+(?:щонайменше\s+|принаймні\s+|понад\s+|близько\s+)?{n}\b'
      - '\b{n}(?:\s+(?:людей|людини|осіб|особи|цивільних|військових|дітей))?\s+(?:загинули|загинуло|загиблих|вбито|убито)\b'
    ar:
      - '(?<!\w)و?(?:مقتل|قتل|استشهاد|سقوط|ارتفاع\s+عدد\s+القتلى\s+إلى|ارتفاع\s+حصيلة\s+القتلى\s+إلى)\s+{n}(?!\w)'
      - '(?<!\w){n}\s+(?:قتلى|قتيلا|قتيلاً|قتيل|شهداء|شهيدا|شهيداً|شهيد)'
  injured:
    en:
      - '\b(?:injuring|injured|injures|wounding|wounded|wounds)\s+(?:at\s+least\s+|some\s+|about\s+|around\s+|nearly\s+|more\s+than\s+|over\s+|another\s+)?{n}\b'
      - '\b{n}(?:\s+(?:more|other|others|people|persons|civilians|soldiers|servicemen|troops|children|women|police(?:\s+officers)?|officers|workers|passengers|residents))?(?:\s+(?:were|was|are|have\s+been|has\s+been|had\s+been|reportedly))*\s+(?:injured|wounded|hurt)\b'
    fr:
      - '\b{n}(?:\s+(?:personnes|civils|soldats|militaires|enfants))?(?:\s+(?:ont\s+été|sont))?\s+(?:blessés?|blessées?)\b'
      - '\b(?:blessant|blessé)\s+(?:au\s+moins\s+|plus\s+de\s+)?{n}\b'
    es:
      - '\b{n}(?:\s+(?:personas|civiles|soldados|militares|niños))?(?:\s+(?:fueron|han\s+sido|resultaron))?\s+(?:heridos?|heridas?|lesionados?|lesionadas?)\b'
      - '\b(?:hirió|hiriendo)\s+a\s+(?:al\s+menos\s+|más\s+de\s+)?{n}\b'
    ru:
      - '(?<!\w)(?:ранены|ранено|ранен|пострадали|пострадало|пострадал)\s+(?:по\s+меньшей\s+мере\s+|не\s+менее\s+|как\s+минимум\s+|более\s+|около\s+|ещё\s+|еще\s+)?{n}\b'
      - '\b{n}(?:\s+(?:человек|человека|людей|мирных\s+жителей|военных|детей))?\s+(?:ранены|ранено|раненых|пострадали|пострадавших)\b'
    uk:
      - '(?<!\w)(?:поранені|поранено|поранений|постраждали|постраждало|постраждав)\s+(?:щонайменше\s+|принаймні\s+|понад\s+|близько\s+|ще\s+)?{n}\b'
      - '\b{n}(?:\s+(?:людей|людини|осіб|особи|цивільних|військових|дітей))?\s+(?:поранені|поранено|поранених|постраждали|постраждалих)\b'
    ar:
      - '(?<!\w)و?(?:إصابة|اصابة|جرح|إصابات)\s+{n}(?!\w)'
      - '(?<!\w){n}\s+(?:جرحى|جريحا|جريحاً|مصابين|مصابا|مصاباً|إصابات)'
```

- [ ] **Step 4: Write the extractor**

Create `backend/osint/enrich/rules/casualties.py`:

```python
"""Casualty counts by multilingual patterns (deterministic extraction spec §2.4).

Patterns come from ``config/extract_rules.yaml`` (``casualties``): regex templates per
field (killed / injured) and language, with ``{n}`` standing for a number in digits
("12", "1,200", Arabic-Indic "١٢") or a number word up to twenty. They run on the
case-folded title and snippet of every document, whatever its language. A document
counts with its largest number; the event value is the lower median over distinct
domains (one value per domain: its largest), ignoring documents that state no number.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from osint.enrich.rules.base import ExtractDoc, FieldValue

FIELDS = {"killed": "casualties.killed", "injured": "casualties.injured"}
_SEPARATORS = re.compile(r"[,  ]")


class CasualtyRules:
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.words: dict[str, int] = {}
        for table in (cfg.get("number_words") or {}).values():
            for word, value in (table or {}).items():
                self.words[unicodedata.normalize("NFC", str(word)).casefold()] = int(value)
        alt = "|".join(re.escape(w) for w in sorted(self.words, key=len, reverse=True))
        num = (r"(?P<n>\d{1,3}(?:[,  ]\d{3})+|\d+" + (f"|{alt}" if alt else "")
               + r")(?![\d%-]|[.,]\d)")
        self.max_value = int(cfg.get("max_value", 100000))
        self.patterns: dict[str, list[re.Pattern[str]]] = {}
        for field in FIELDS:
            by_lang = cfg.get(field) or {}
            self.patterns[field] = [re.compile(str(t).replace("{n}", num))
                                    for templates in by_lang.values() for t in templates or ()]
        if not any(self.patterns.values()):
            raise ValueError("extract_rules: no casualty patterns")

    def _value(self, raw: str) -> int | None:
        word = self.words.get(raw.casefold())
        if word is not None:
            return word
        try:
            v = int(_SEPARATORS.sub("", raw))
        except ValueError:
            return None
        return v if 0 <= v <= self.max_value else None

    def doc_value(self, field: str, text: str) -> tuple[int, str] | None:
        """(largest number, matched span) for ``field`` in ``text``, or None."""
        norm = unicodedata.normalize("NFC", text or "").replace("’", "'")
        low = norm.casefold()
        src = norm if len(low) == len(norm) else low  # spans index the original when possible
        best: tuple[int, str] | None = None
        for p in self.patterns.get(field, ()):
            for m in p.finditer(low):
                v = self._value(m.group("n"))
                if v is not None and (best is None or v > best[0]):
                    best = (v, src[m.start():m.end()])
        return best


def casualties(event: Mapping[str, Any], docs: Sequence[ExtractDoc], *,
               rules: CasualtyRules) -> list[FieldValue]:
    out: list[FieldValue] = []
    for field, name in FIELDS.items():
        per_domain: dict[str, tuple[int, ExtractDoc, str]] = {}
        for d in docs:
            hit = rules.doc_value(field, d.text)
            if hit is None:
                continue
            cur = per_domain.get(d.domain)
            if cur is None or hit[0] > cur[0]:
                per_domain[d.domain] = (hit[0], d, hit[1])
        if not per_domain:
            continue
        ranked = sorted(per_domain.values(), key=lambda t: (t[0], t[1].published_at,
                                                             t[1].doc_id))
        med = statistics.median_low([t[0] for t in ranked])
        value, doc, quote = next(t for t in ranked if t[0] == med)
        out.append(FieldValue(name, str(value), doc.doc_id, quote))
    return out
```

- [ ] **Step 5: Add casualties to the rule set**

In `backend/osint/enrich/rules/__init__.py` add the import:

```python
from osint.enrich.rules.casualties import CasualtyRules, casualties
```

and replace the `RuleSet` class with:

```python
@dataclass
class RuleSet:
    summary_max_chars: int
    weapons: WeaponLexicon
    casualties: CasualtyRules

    @classmethod
    def load(cls, path: Path = DEFAULT_RULES) -> RuleSet:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        lex = WeaponLexicon(cfg.get("weapons") or {}, subsumed=cfg.get("weapons_subsumed"),
                            ignore=cfg.get("weapons_ignore") or ())
        if not len(lex):
            raise ValueError(f"{path}: empty weapons lexicon")
        return cls(summary_max_chars=int(cfg.get("summary_max_chars", SUMMARY_MAX)),
                   weapons=lex, casualties=CasualtyRules(cfg.get("casualties") or {}))

    def extractors(self) -> list[tuple[str, Extractor]]:
        return [("headline_summary", partial(headline_summary,
                                             max_chars=self.summary_max_chars)),
                ("casualties", partial(casualties, rules=self.casualties)),
                ("weapons", partial(weapons, lexicon=self.weapons))]

    def run(self, event: Mapping[str, Any], docs: Sequence[ExtractDoc]
            ) -> tuple[list[FieldValue], list[str]]:
        """Every extractor's values; one that raises is skipped (and logged) for this
        event only. Returns (values, names of the extractors that failed)."""
        out: list[FieldValue] = []
        failed: list[str] = []
        for name, fn in self.extractors():
            try:
                out += fn(event, docs)
            except Exception:
                log.exception("rule extractor %s failed for event %s", name, event.get("id"))
                failed.append(name)
        return out, failed
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/enrich/rules -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/osint/enrich/rules config/extract_rules.yaml tests/enrich/rules/test_casualties.py
git commit -m "feat(rules): multilingual casualty patterns with a median over outlets"
```

---
### Task 8: Lifecycle

**Files:**
- Create: `backend/osint/pipeline/lifecycle.py`
- Modify: `backend/osint/pipeline/events.py` (imports ~:52; `EventsConfig` field + `from_dict` ~:118 / ~:216; `PassStats` ~:362; `EventBuilder.__init__` ~:381; `persist_merges` ~:657; `_run_once` ~:509-586; `_record` ~:850)
- Modify: `config/events.yaml` (append `lifecycle:`)
- Test: `tests/pipeline/test_lifecycle.py`

**Interfaces:**
- Consumes: `SqliteStore.lifecycle_inputs(recent_since, ids=None)`, `SqliteStore.set_lifecycle(states)` (Task 3).
- Produces:
  - `osint.pipeline.lifecycle`: `STATES = ("developing", "active", "cooling", "closed")`; `@dataclass(frozen=True) class LifecycleConfig: developing_h: float = 6.0; developing_min_docs: int = 2; active_h: float = 48.0; cooling_d: float = 7.0` with `from_dict(d: dict | None) -> LifecycleConfig`; `lifecycle_state(last_at: datetime | None, recent_docs: int, now: datetime, cfg: LifecycleConfig) -> str`; `lifecycle_for_row(row: dict, now: datetime, cfg: LifecycleConfig) -> str`; `refresh_lifecycle(store, now: datetime, cfg: LifecycleConfig, ids: Iterable[str] | None = None) -> int` (events changed).
  - `EventsConfig.lifecycle: LifecycleConfig`; `PassStats.lifecycle_changed: int`.
  - `EventBuilder._merge_survivors: set[str]` (filled by `persist_merges`) and the per-pass `touched` set = created ∪ updated ∪ merge survivors (Task 9 reuses it).

- [ ] **Step 1: Write the failing test**

Create `tests/pipeline/test_lifecycle.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from osint.config import ROOT
from osint.pipeline.events import EventsConfig
from osint.pipeline.lifecycle import (
    LifecycleConfig,
    lifecycle_for_row,
    lifecycle_state,
    refresh_lifecycle,
)
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore, iso
from tests.pipeline.test_event_builder import NOW as BUILDER_NOW
from tests.pipeline.test_event_builder import _builder, _row

CFG = LifecycleConfig()
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("age", "recent", "state"), [
    (timedelta(hours=6), 2, "developing"),
    (timedelta(hours=6, seconds=1), 2, "active"),
    (timedelta(hours=1), 1, "active"),  # one report only: not developing
    (timedelta(hours=48), 0, "active"),
    (timedelta(hours=48, seconds=1), 0, "cooling"),
    (timedelta(days=7), 0, "cooling"),
    (timedelta(days=7, seconds=1), 0, "closed")])
def test_boundaries(age, recent, state):
    assert lifecycle_state(NOW - age, recent, NOW, CFG) == state


def test_no_time_is_closed():
    assert lifecycle_state(None, 5, NOW, CFG) == "closed"


def test_thresholds_come_from_events_yaml():
    cfg = EventsConfig.from_file(ROOT / "config" / "events.yaml")
    assert cfg.lifecycle == LifecycleConfig(developing_h=6.0, developing_min_docs=2,
                                            active_h=48.0, cooling_d=7.0)
    assert EventsConfig.from_dict({}).lifecycle == LifecycleConfig()


def test_sensor_rows_use_t_end_and_others_the_newest_article():
    sensor = {"origin": "sensor", "t_end": iso(NOW - timedelta(hours=1)),
              "last_doc_at": None, "recent_docs": 0}
    assert lifecycle_for_row(sensor, NOW, CFG) == "active"
    news = {"origin": "news", "t_end": iso(NOW - timedelta(hours=1)),
            "last_doc_at": iso(NOW - timedelta(days=3)), "recent_docs": 0}
    assert lifecycle_for_row(news, NOW, CFG) == "cooling"
    bare = {"origin": "news", "t_end": iso(NOW - timedelta(days=9)), "last_doc_at": None,
            "recent_docs": 0}
    assert lifecycle_for_row(bare, NOW, CFG) == "closed"


def _ev(eid, t_end, **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "t_start": iso(t_end),
            "t_end": iso(t_end), "status": "reported", "confidence": 0.4, "version": 1,
            "mention_count": 1, "source_diversity": 1, "first_seen": iso(t_end),
            "updated_at": iso(t_end)}
    return {**base, **kw}


def _doc(eid, did, at):
    return {"event_id": eid, "doc_id": did, "source": "rss:t", "url": f"https://{did}.x/a",
            "title": "t", "published_at": iso(at), "language": "en", "role": "mention",
            "added_at": iso(at)}


def test_refresh_lifecycle_writes_changes_and_reopens_touched_events(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    store.save_events(
        [_ev("DEV", NOW), _ev("OLD", NOW - timedelta(days=10)),
         _ev("SEN", NOW - timedelta(hours=1), origin="sensor"),
         _ev("SHUT", NOW - timedelta(days=20), lifecycle="closed")],
        documents=[_doc("DEV", "a", NOW - timedelta(hours=1)),
                   _doc("DEV", "b", NOW - timedelta(hours=2)),
                   _doc("OLD", "c", NOW - timedelta(days=10)),
                   _doc("SHUT", "d", NOW - timedelta(days=20))])
    assert refresh_lifecycle(store, NOW, CFG) == 3
    got = {e: store.get_event(e)["lifecycle"] for e in ("DEV", "OLD", "SEN", "SHUT")}
    assert got == {"DEV": "developing", "OLD": "closed", "SEN": "active", "SHUT": "closed"}
    assert refresh_lifecycle(store, NOW, CFG) == 0  # nothing changed
    store.add_event_documents([_doc("SHUT", "e", NOW - timedelta(minutes=5))])
    assert refresh_lifecycle(store, NOW, CFG) == 0  # closed events are not rescanned...
    assert refresh_lifecycle(store, NOW, CFG, ["SHUT"]) == 1  # ...unless a pass touched them
    assert store.get_event("SHUT")["lifecycle"] == "active"
    assert refresh_lifecycle(store, NOW, CFG, []) == 0
    store.close()


def test_builder_pass_sets_lifecycle(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant",
             fetched=BUILDER_NOW - timedelta(minutes=50)),
        _row(2, "Missile strike on Kyiv power plant",
             fetched=BUILDER_NOW - timedelta(minutes=40), host="other.example")])
    b = _builder(store, lake, lambda: BUILDER_NOW)
    s = b.run_once()
    [ev] = store.list_events(limit=5)
    assert ev["lifecycle"] == "developing" and s.lifecycle_changed >= 1
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pipeline/test_lifecycle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.pipeline.lifecycle'`.

- [ ] **Step 3: Implement the lifecycle module**

Create `backend/osint/pipeline/lifecycle.py`:

```python
"""Event lifecycle (deterministic extraction spec §2.5).

From the time the newest linked article was published (``last_doc_at``; GDELT rows do not
count; sensor events use ``t_end``, when the signal was last seen):

* ``developing``: newest report <= ``developing_h`` old and >= ``developing_min_docs``
  reports within ``developing_h``;
* ``active``: newest report <= ``active_h`` old;
* ``cooling``: newest report <= ``cooling_d`` old;
* ``closed``: older.

Thresholds: ``config/events.yaml`` ``lifecycle:``. The EventBuilder refreshes the events a
pass touched plus every event not yet closed (``refresh_lifecycle``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

STATES = ("developing", "active", "cooling", "closed")


@dataclass(frozen=True)
class LifecycleConfig:
    developing_h: float = 6.0
    developing_min_docs: int = 2
    active_h: float = 48.0
    cooling_d: float = 7.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> LifecycleConfig:
        d = d or {}
        base = cls()
        return cls(developing_h=float(d.get("developing_h", base.developing_h)),
                   developing_min_docs=int(d.get("developing_min_docs",
                                                 base.developing_min_docs)),
                   active_h=float(d.get("active_h", base.active_h)),
                   cooling_d=float(d.get("cooling_d", base.cooling_d)))


def lifecycle_state(last_at: datetime | None, recent_docs: int, now: datetime,
                    cfg: LifecycleConfig) -> str:
    if last_at is None:
        return "closed"
    age = now - last_at
    if age <= timedelta(hours=cfg.developing_h) and recent_docs >= cfg.developing_min_docs:
        return "developing"
    if age <= timedelta(hours=cfg.active_h):
        return "active"
    if age <= timedelta(days=cfg.cooling_d):
        return "cooling"
    return "closed"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def lifecycle_for_row(row: dict, now: datetime, cfg: LifecycleConfig) -> str:
    """State of a ``SqliteStore.lifecycle_inputs`` row: sensor events by ``t_end``, others
    by their newest article (``t_end`` when they have none)."""
    if row.get("origin") == "sensor":
        last = _parse(row.get("t_end"))
    else:
        last = _parse(row.get("last_doc_at")) or _parse(row.get("t_end"))
    return lifecycle_state(last, int(row.get("recent_docs") or 0), now, cfg)


def refresh_lifecycle(store: Any, now: datetime, cfg: LifecycleConfig,
                      ids: Iterable[str] | None = None) -> int:
    """Recompute and store the lifecycle of ``ids`` (None: every event not yet closed);
    returns the number of events whose state changed."""
    if ids is not None:
        ids = list(ids)
        if not ids:
            return 0
    rows = store.lifecycle_inputs(now - timedelta(hours=cfg.developing_h), ids)
    changes = {}
    for r in rows:
        state = lifecycle_for_row(r, now, cfg)
        if state != r.get("lifecycle"):
            changes[r["id"]] = state
    if changes:
        store.set_lifecycle(changes)
    return len(changes)
```

- [ ] **Step 4: Add the thresholds to `config/events.yaml`**

Append:

```yaml

# Event lifecycle (osint.pipeline.lifecycle), from the newest linked article's published
# time (sensor events: when the signal was last seen); refreshed every builder pass
lifecycle:
  developing_h: 6           # newest report at most this old ...
  developing_min_docs: 2    # ... and at least this many reports within developing_h
  active_h: 48              # newest report at most this old
  cooling_d: 7              # at most this old; older -> closed
```

- [ ] **Step 5: Wire it into `EventsConfig` and the EventBuilder**

In `backend/osint/pipeline/events.py`:

Add after `from osint.pipeline.embed import EmbeddingConfig, build_embedder`:

```python
from osint.pipeline.lifecycle import LifecycleConfig, refresh_lifecycle
```

In `EventsConfig`, after the `verification: dict[str, float] = field(...)` field, add:

```python
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
```

In `EventsConfig.from_dict`, after the `verification={...},` argument add:

```python
            lifecycle=LifecycleConfig.from_dict(d.get("lifecycle")),
```

In `PassStats`, after `merged: int = 0  # events folded into another (consolidation)` add:

```python
    lifecycle_changed: int = 0  # events whose lifecycle state changed this pass
```

In `EventBuilder.__init__`, after `self._seen: dict[str, datetime] = {}  # doc id -> fetched_at (overlap window)` add:

```python
        self._merge_survivors: set[str] = set()  # merge survivors since the last pass
```

In `persist_merges`, after `n = self.store.merge_events(recs, rows)` add:

```python
        self._merge_survivors |= final
```

In `_run_once`, directly after the block

```python
        if res.changed:
            try:
                merged = self.consolidate(now, res.changed)
            except Exception:  # consolidation must never stop event building
                self.metrics.inc("event_merge_errors_total")
                log.exception("event consolidation failed")
```

add:

```python
        # events this pass created, added a document to, or merged into (spec §2.6)
        touched = (set(res.changed) | self._merge_survivors) & set(self.clusterer.events)
        self._merge_survivors = set()
```

directly after the block ending in `log.exception("applying impact sites failed")` add:

```python
        lifecycle_changed = 0
        try:  # touched events first (a closed event can reopen), then every open one
            lifecycle_changed = refresh_lifecycle(self.store, now, self.cfg.lifecycle, touched)
            lifecycle_changed += refresh_lifecycle(self.store, now, self.cfg.lifecycle)
        except Exception:  # lifecycle must never stop event building
            self.metrics.inc("lifecycle_errors_total")
            log.exception("lifecycle refresh failed")
```

and in the `PassStats(...)` construction replace `merged=merged, seconds=time.perf_counter() - t0)` with:

```python
                          merged=merged, lifecycle_changed=lifecycle_changed,
                          seconds=time.perf_counter() - t0)
```

In `_record`, after `m.inc("documents_clustered_total", s.clustered)` add:

```python
        m.inc("event_lifecycle_changes_total", s.lifecycle_changed)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/pipeline/test_lifecycle.py tests/pipeline/test_event_builder.py tests/pipeline/test_event_rebuild.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/osint/pipeline/lifecycle.py backend/osint/pipeline/events.py config/events.yaml tests/pipeline/test_lifecycle.py
git commit -m "feat(events): developing/active/cooling/closed lifecycle refreshed every builder pass"
```

---

### Task 9: Re-extraction trigger (`RulePass`) and `unresolved_country`

**Files:**
- Create: `backend/osint/pipeline/rule_pass.py`
- Modify: `backend/osint/pipeline/events.py` (imports; `PassStats`; `EventBuilder.__init__` / `from_settings` / `wire`; `apply_verdict_records`; `_run_once`; `_record`)
- Test: `tests/pipeline/test_rule_pass.py`, `tests/pipeline/test_event_builder.py`

**Interfaces:**
- Consumes: `ActorLexicon`, `resolve_actors`, `derive` (Tasks 4–5); `RuleSet.run`, `RuleSet.extractors` (Tasks 6–7); `ExtractDoc`, `FieldValue.row` (Task 6); `SqliteStore.events_for_rules`, `rule_documents`, `set_event_derived`, `replace_rule_fields` (Task 3); `osint.pipeline.cluster.Embedder`, `NullEmbedder`, `embed_many(embedder, items: list[tuple[str, str]]) -> list`, `embed_text(title, snippet) -> str`; `osint.translate.job.lake_snippets(lake, docs) -> dict[str, str]`; extraction store `get_many(ids) -> dict[str, dict]` (records with `status`, `verdict`, `result.actors`); `countries.drain_unresolved()` (Task 1); `touched` (Task 8).
- Produces:
  - `osint.pipeline.rule_pass.RulePassStats` (`events`, `fields`, `errors`, `attributed`, `foreign`: int; `seconds`: float).
  - `RulePass(store, *, lexicon, rules, lake=None, extractions=None, embedder=None, metrics=None)` with `run(event_ids: Iterable[str], *, states: Mapping[str, Any] | None = None, snippets: Mapping[str, str] | None = None, fields: bool = True, lake_snippets: bool = True, write: bool = True, country_override: Mapping[str, str | None] | None = None) -> RulePassStats`. `states` values need `.actors` (set when `write`) and `.vector`.
  - `EventBuilder.rule_pass: RulePass | None` (constructor kwarg `rule_pass`); `PassStats.rule_events`, `rule_fields`, `rule_errors`, `unresolved_country`.

- [ ] **Step 1: Write the failing tests**

Create `tests/pipeline/test_rule_pass.py`:

```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from osint.enrich.actors import ActorLexicon
from osint.enrich.rules import RuleSet
from osint.pipeline.rule_pass import RulePass
from osint.store.sqlite import SqliteStore, iso

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _ev(eid, **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "summary": "",
            "t_start": iso(NOW - timedelta(hours=2)), "t_end": iso(NOW - timedelta(hours=1)),
            "status": "reported", "confidence": 0.5, "version": 1, "mention_count": 2,
            "source_diversity": 2, "first_seen": iso(NOW), "updated_at": iso(NOW),
            "country": "UA", "actors": []}
    return {**base, **kw}


def _doc(eid, did, title, host, minutes, role="mention"):
    at = NOW - timedelta(minutes=minutes)
    return {"event_id": eid, "doc_id": did, "source": "rss:t", "url": f"https://{host}/{did}",
            "title": title, "published_at": iso(at), "language": "en", "role": role,
            "added_at": iso(at)}


class FakeExtractions:
    def __init__(self, recs):
        self.recs = recs

    def get_many(self, ids):
        return {i: self.recs[i] for i in ids if i in self.recs}


@pytest.fixture(scope="module")
def lexicon():
    return ActorLexicon.load()


@pytest.fixture
def rules():
    return RuleSet.load()


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.sqlite")
    acled_title = "Shelling: Military Forces of Russia (2000-) vs Civilians (Ukraine)"
    s.save_events(
        [_ev("NEWS1", actors=[{"name": "Russia", "role": "source", "type": "RUS",
                               "country": "RUS"}]),
         _ev("acled-UKR1", origin="historical", status="historical", title=acled_title,
             summary="Russian forces shelled Kherson, killing 2.", actors=[
                 {"name": "Military Forces of Russia (2000-)", "role": "actor1", "type": "8",
                  "country": None, "source": "acled"},
                 {"name": "Civilians (Ukraine)", "role": "actor2", "type": "7",
                  "country": None, "source": "acled"}])],
        documents=[
            _doc("NEWS1", "d1", "Russian shelling of Kharkiv kills 3 civilians", "a.example",
                 50, "primary"),
            _doc("NEWS1", "d2", "Three civilians killed in Kharkiv shelling", "b.example", 40),
            {"event_id": "acled-UKR1", "doc_id": "acled:UKR1", "source": "acled",
             "url": "https://acleddata.com/data/#UKR1", "title": acled_title,
             "published_at": iso(NOW - timedelta(hours=3)), "language": "en",
             "role": "primary", "added_at": iso(NOW)}])
    yield s
    s.close()


def test_rule_pass_resolves_actors_derives_and_writes_rule_fields(store, lexicon, rules):
    ext = FakeExtractions({"NEWS1": {"status": "done", "verdict": "supported", "result": {
        "actors": [{"name": "Israel Defense Forces", "role": "perpetrator", "type": None}]}}})
    state = SimpleNamespace(actors=[], vector=None)
    stats = RulePass(store, lexicon=lexicon, rules=rules, extractions=ext).run(
        ["NEWS1", "gone"], states={"NEWS1": state}, snippets={})
    assert stats.events == 1 and stats.errors == 0 and stats.fields >= 3
    ev = store.get_event("NEWS1")
    by_source = {a["source"]: a for a in ev["actors"]}
    assert (by_source["gdelt"]["country"], by_source["gdelt"]["cameo"]) == ("RU", "RUS")
    assert by_source["rule"]["country"] == "RU" and by_source["rule"]["role"] == "mentioned"
    assert by_source["llm"]["country"] == "IL"
    assert ev["actor_countries"] == ["IL", "RU"]
    assert ev["foreign_involved"] == 1 and ev["attributed"] == 1
    assert state.actors == ev["actors"]
    got = {(r["field"], r["value"]) for r in store.rule_fields(["NEWS1"])["NEWS1"]}
    assert ("headline", "Russian shelling of Kharkiv kills 3 civilians") in got
    assert ("casualties.killed", "3") in got and ("weapons_platforms", "artillery") in got


def test_unsupported_extractions_add_no_llm_actors(store, lexicon, rules):
    ext = FakeExtractions({"NEWS1": {"status": "done", "verdict": "mismatch", "result": {
        "actors": [{"name": "Israel Defense Forces", "role": "perpetrator"}]}}})
    RulePass(store, lexicon=lexicon, rules=rules, extractions=ext).run(["NEWS1"])
    assert "IL" not in store.get_event("NEWS1")["actor_countries"]


def test_historical_events_get_actors_but_no_rule_fields(store, lexicon, rules):
    RulePass(store, lexicon=lexicon, rules=rules).run(["acled-UKR1"])
    ev = store.get_event("acled-UKR1")
    assert [(a["country"], a["source"]) for a in ev["actors"]] == [("RU", "acled"),
                                                                  ("UA", "acled")]
    assert ev["foreign_involved"] == 1 and store.rule_fields(["acled-UKR1"]) == {}


def test_a_failing_extractor_is_skipped(store, lexicon, rules, monkeypatch):
    def boom(event, docs):
        raise RuntimeError("bad document")

    good = rules.extractors()
    monkeypatch.setattr(rules, "extractors", lambda: [("boom", boom), *good])
    stats = RulePass(store, lexicon=lexicon, rules=rules).run(["NEWS1"])
    assert stats.errors == 1
    assert store.rule_fields(["NEWS1"])["NEWS1"]  # the other extractors still wrote


def test_dry_run_with_a_country_override_writes_nothing(store, lexicon, rules):
    stats = RulePass(store, lexicon=lexicon, rules=rules).run(
        ["NEWS1"], write=False, country_override={"NEWS1": "RU"})
    assert stats.events == 1 and stats.attributed == 1 and stats.foreign == 0
    assert store.get_event("NEWS1")["actor_countries"] == []
    assert store.rule_fields(["NEWS1"]) == {}
```

Append to `tests/pipeline/test_event_builder.py`:

```python
def test_builder_pass_runs_rule_extraction(tmp_path):
    from osint.enrich.actors import ActorLexicon
    from osint.enrich.rules import RuleSet
    from osint.pipeline.rule_pass import RulePass

    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    title = "Russian shelling of Kyiv kills 3 civilians"
    lake.write("documents", [
        _row(1, title, fetched=NOW - timedelta(minutes=50)),
        _row(2, title, fetched=NOW - timedelta(minutes=40), host="other.example")])
    b = _builder(store, lake, lambda: NOW)
    b.rule_pass = RulePass(store, lexicon=ActorLexicon.load(), rules=RuleSet.load(), lake=lake)
    s = b.run_once()
    assert s.created == 1 and s.rule_events == 1 and s.rule_errors == 0
    [ev] = store.list_events(limit=10)
    assert ev["country"] == "UA" and ev["actor_countries"] == ["RU"]
    assert ev["foreign_involved"] == 1 and ev["attributed"] == 1
    got = {(r["field"], r["value"]) for r in store.rule_fields([ev["id"]])[ev["id"]]}
    assert {("headline", title), ("casualties.killed", "3"),
            ("weapons_platforms", "artillery")} <= got
    full = store.get_event(ev["id"])
    assert any(a["source"] == "rule" and a["country"] == "RU" for a in full["actors"])
    assert b.clusterer.events[ev["id"]].actors == full["actors"]
    store.close()


def test_builder_pass_reports_unresolved_countries(tmp_path):
    from osint.core import countries

    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    b = _builder(store, lake, lambda: NOW)
    countries.drain_unresolved()
    countries.to_iso2("Atlantis", "name")
    m = b.metrics
    assert b.run_once().unresolved_country == 1
    assert m.get("unresolved_country_total") == 1
    assert b.run_once().unresolved_country == 0
    store.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_rule_pass.py tests/pipeline/test_event_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.pipeline.rule_pass'`.

- [ ] **Step 3: Implement `RulePass`**

Create `backend/osint/pipeline/rule_pass.py`:

```python
"""Deterministic extraction for a set of events (deterministic extraction spec §2.3, §2.4,
§2.6): run by the EventBuilder after each pass for the events it touched, and by the
country migration for every event.

Per event: actors are re-resolved (``osint.enrich.actors.resolve_actors``) and the derived
columns written (actor_countries, foreign_involved, attributed); for news and sensor events
the rule extractors (``osint.enrich.rules.RuleSet``) replace the event's ``source='rule'``
fields (curated ACLED / UCDP rows keep their own values). Inputs: the event row, its
documents (titles from event_documents, snippets from the pass or the lake), the LLM
extraction when it is ``supported`` (its actor names) and, with an embedder, document
vectors for the headline pick. CPU only apart from the snippet read; an event whose actor
resolution fails is skipped and logged, a failing rule extractor only for that event.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from osint.core.metrics import Metrics
from osint.enrich.actors import ActorLexicon, derive, resolve_actors
from osint.enrich.rules import RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.pipeline.cluster import Embedder, NullEmbedder, embed_many, embed_text

log = logging.getLogger(__name__)
CHUNK = 500


@dataclass
class RulePassStats:
    events: int = 0
    fields: int = 0
    errors: int = 0
    attributed: int = 0
    foreign: int = 0
    seconds: float = 0.0


def _llm_actors(rec: Mapping[str, Any] | None) -> list[dict]:
    """Actor dicts of a supported LLM extraction (spec §2.3 step 4), else []."""
    if not rec or rec.get("status") != "done" or rec.get("verdict") != "supported":
        return []
    return [a for a in (rec.get("result") or {}).get("actors") or [] if isinstance(a, dict)]


class RulePass:
    def __init__(self, store: Any, *, lexicon: ActorLexicon, rules: RuleSet, lake: Any = None,
                 extractions: Any = None, embedder: Embedder | None = None,
                 metrics: Metrics | None = None) -> None:
        self.store, self.lexicon, self.rules = store, lexicon, rules
        self.lake, self.extractions = lake, extractions
        self.embedder = None if embedder is None or isinstance(embedder, NullEmbedder) \
            else embedder
        self.metrics = metrics or Metrics()

    def run(self, event_ids: Iterable[str], *, states: Mapping[str, Any] | None = None,
            snippets: Mapping[str, str] | None = None, fields: bool = True,
            lake_snippets: bool = True, write: bool = True,
            country_override: Mapping[str, str | None] | None = None) -> RulePassStats:
        """Process ``event_ids`` (missing ids are skipped). ``states``: the clusterer's
        in-memory events (their ``actors`` are updated so its next upsert keeps them;
        ``vector`` is the centroid for the headline pick). ``snippets``: doc id -> snippet
        already at hand (the pass's documents); others come from the lake unless
        ``lake_snippets`` is false. ``write=False`` computes the statistics only;
        ``country_override`` replaces events.country for that (the migration's dry run)."""
        t0 = time.perf_counter()
        stats = RulePassStats()
        ids = sorted(set(event_ids))
        for i in range(0, len(ids), CHUNK):
            self._chunk(ids[i:i + CHUNK], stats, states or {}, dict(snippets or {}), fields,
                        lake_snippets, write, country_override or {})
        stats.seconds = time.perf_counter() - t0
        self.metrics.inc("rule_pass_events_total", stats.events)
        self.metrics.set("rule_pass_seconds", round(stats.seconds, 4))
        return stats

    def _chunk(self, ids: list[str], stats: RulePassStats, states: Mapping[str, Any],
               snips: dict[str, str], fields: bool, lake_snippets: bool, write: bool,
               override: Mapping[str, str | None]) -> None:
        evs = self.store.events_for_rules(ids)
        docs = self.store.rule_documents(ids)
        if lake_snippets and self.lake is not None:
            need = [d for eid in ids if evs.get(eid, {}).get("origin") != "historical"
                    for d in docs.get(eid, []) if d["doc_id"] not in snips]
            if need:
                snips.update(self._lake_snippets(need))
        recs = self._extractions(ids)
        derived: list[dict] = []
        rule_rows: list[dict] = []
        rule_ids: list[str] = []
        for eid in ids:
            ev = evs.get(eid)
            if ev is None:
                continue  # merged away or deleted meanwhile
            if eid in override:
                ev = {**ev, "country": override[eid]}
            st = states.get(eid)
            edocs = self._docs(ev, docs.get(eid, []), snips, st)
            try:
                actors = resolve_actors(ev, edocs, self.lexicon, _llm_actors(recs.get(eid)))
            except Exception:
                stats.errors += 1
                self.metrics.inc("rule_extraction_errors_total", extractor="actors")
                log.exception("actor resolution failed for event %s", eid)
                continue
            d = derive(actors, ev.get("country"))
            stats.events += 1
            stats.attributed += int(d["attributed"])
            stats.foreign += int(d["foreign_involved"])
            derived.append({"id": eid, "actors": actors, **d})
            if st is not None and write:
                st.actors = actors
            if fields and ev.get("origin") != "historical":
                values, failed = self.rules.run(
                    {"id": eid, "vector": getattr(st, "vector", None)}, edocs)
                stats.errors += len(failed)
                for name in failed:
                    self.metrics.inc("rule_extraction_errors_total", extractor=name)
                rule_ids.append(eid)
                rule_rows += [v.row(eid) for v in values]
        stats.fields += len(rule_rows)
        if write:
            if derived:
                self.store.set_event_derived(derived)
            if rule_ids:
                self.store.replace_rule_fields(rule_ids, rule_rows)

    def _docs(self, ev: Mapping[str, Any], rows: list[dict], snips: Mapping[str, str],
              st: Any) -> list[ExtractDoc]:
        historical = ev.get("origin") == "historical"
        vecs = self._vectors(rows, snips) if getattr(st, "vector", None) is not None else {}
        out = []
        for r in rows:
            snippet = snips.get(r["doc_id"]) or (str(ev.get("summary") or "")
                                                if historical else "")
            out.append(ExtractDoc(
                doc_id=r["doc_id"], title=r["title"] or "", snippet=snippet,
                url=r["url"] or "", source=r["source"] or "",
                published_at=r["published_at"] or "", role=r["role"] or "mention",
                language=r["language"] or "", vector=vecs.get(r["doc_id"])))
        return out

    def _vectors(self, rows: list[dict], snips: Mapping[str, str]) -> dict[str, Any]:
        """Document vectors (embedding cache keyed by doc id, as the clusterer wrote them)."""
        if self.embedder is None or not rows:
            return {}
        try:
            vecs = embed_many(self.embedder, [
                (r["doc_id"], embed_text(r["title"] or "", snips.get(r["doc_id"], "")))
                for r in rows])
        except Exception:
            log.exception("document vectors for the headline pick failed")
            return {}
        return {r["doc_id"]: v for r, v in zip(rows, vecs, strict=True) if v is not None}

    def _lake_snippets(self, docs: list[dict]) -> dict[str, str]:
        from osint.translate.job import lake_snippets  # local: optional stage

        try:
            return lake_snippets(self.lake, docs)
        except Exception:  # titles alone still extract
            log.exception("snippet lookup for %d documents failed", len(docs))
            return {}

    def _extractions(self, ids: list[str]) -> dict[str, dict]:
        if self.extractions is None:
            return {}
        try:
            return self.extractions.get_many(list(ids))
        except Exception:
            log.exception("extraction lookup failed; LLM actors skipped")
            return {}
```

- [ ] **Step 4: Wire it into the EventBuilder**

In `backend/osint/pipeline/events.py`:

Add after `from osint.core.geo import utcnow`:

```python
from osint.core import countries
```

and after `from osint.pipeline.lifecycle import LifecycleConfig, refresh_lifecycle`:

```python
from osint.pipeline.rule_pass import RulePass
```

In `PassStats`, after `lifecycle_changed: int = 0 ...` add:

```python
    rule_events: int = 0  # events re-extracted by the rule pass
    rule_fields: int = 0  # rule field values written
    rule_errors: int = 0  # failed actor resolutions + failed rule extractors
    unresolved_country: int = 0  # country values that did not resolve (source health)
```

In `EventBuilder.__init__` add the keyword argument `rule_pass: RulePass | None = None,` after `corroborator: Corroborator | None = None,` and, after `self.corroborator = corroborator`, add:

```python
        self.rule_pass = rule_pass  # deterministic extraction after each pass (wire())
        self._verdict_ids: set[str] = set()  # events with newly applied LLM verdicts
```

Replace `from_settings` with:

```python
    @classmethod
    def from_settings(cls, settings: Any, store: SqliteStore, lake: Lake,
                      metrics: Metrics | None = None) -> EventBuilder:
        from osint.enrich.actors import ActorLexicon
        from osint.enrich.rules import RuleSet

        cfg_dir = Path(settings.config_dir)
        metrics = metrics or Metrics()
        corroborator = Corroborator(
            store, lake, metrics,
            CorroborationConfig.from_file(cfg_dir / "corroboration.yaml"))
        cfg = EventsConfig.from_file(cfg_dir / "events.yaml")
        embedder = build_embedder(cfg.embedding, Path(settings.data_dir))
        # a lexicon that fails to load raises LexiconError here: fatal at startup (spec §3)
        rule_pass = RulePass(
            store, lexicon=ActorLexicon.load(cfg_dir / "actors.yaml",
                                             groups_path=cfg_dir / "terror_groups.yaml"),
            rules=RuleSet.load(cfg_dir / "extract_rules.yaml"), lake=lake,
            embedder=embedder, metrics=metrics)
        return cls(store, lake, metrics, cfg,
                   cameo_category=load_cameo_map(cfg_dir / "triage.yaml"),
                   corroborator=corroborator, embedder=embedder, rule_pass=rule_pass)
```

In `apply_verdict_records`, directly before the final `return len(verifs)` add:

```python
        self._verdict_ids |= {v["event_id"] for v in verifs}
```

In `_run_once`, directly after the lifecycle block added in Task 8 (ending `log.exception("lifecycle refresh failed")`) add:

```python
        rule_ids = touched | (self._verdict_ids & set(self.clusterer.events))
        self._verdict_ids = set()
        rstats = None
        if self.rule_pass is not None and rule_ids:
            try:  # after the pass committed, same worker; LLM caps/priority unchanged
                rstats = self.rule_pass.run(rule_ids, states=self.clusterer.events,
                                            snippets={d.id: d.snippet for d in fresh})
            except Exception:  # rule extraction must never stop event building
                self.metrics.inc("rule_pass_errors_total")
                log.exception("rule extraction pass failed")
        unresolved = sum(countries.drain_unresolved().values())
```

and in the `PassStats(...)` construction replace `merged=merged, lifecycle_changed=lifecycle_changed,` with:

```python
                          merged=merged, lifecycle_changed=lifecycle_changed,
                          rule_events=rstats.events if rstats else 0,
                          rule_fields=rstats.fields if rstats else 0,
                          rule_errors=rstats.errors if rstats else 0,
                          unresolved_country=unresolved,
```

In `_record`, after `m.inc("event_lifecycle_changes_total", s.lifecycle_changed)` add:

```python
        m.inc("rule_extraction_errors_total", s.rule_errors)
        m.inc("unresolved_country_total", s.unresolved_country)
```

In `wire`, after `builder.verifier = get_store(runtime)` add:

```python
        if builder.rule_pass is not None:
            builder.rule_pass.extractions = builder.verifier  # LLM actor names (step 4)
```

(`PassStats` reaches the status popover through `state.job_status` → `jobs["event_builder"]["last_pass"]`, so `unresolved_country` needs no further plumbing.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/pipeline tests/test_runtime.py tests/test_state.py -v`
Expected: all PASS (`test_wire_attaches_builder` now also loads the lexicon from `config/`).

- [ ] **Step 6: Commit**

```bash
git add backend/osint/pipeline/rule_pass.py backend/osint/pipeline/events.py tests/pipeline/test_rule_pass.py tests/pipeline/test_event_builder.py
git commit -m "feat(events): rule extraction and actor resolution for events touched by each builder pass"
```

---

### Task 10: Migration CLI

**Files:**
- Create: `backend/osint/pipeline/migrate_countries.py`
- Modify: `backend/osint/store/sqlite.py` (new methods after `events_for_rules`)
- Test: `tests/pipeline/test_migrate_countries.py`

**Interfaces:**
- Consumes: `RulePass.run(..., fields, lake_snippets, write, country_override)` (Task 9); `refresh_lifecycle`, `LifecycleConfig` (Task 8); `countries.to_iso2`, `countries.get`, `countries.drain_unresolved` (Task 1); `rebuild_lock_path`, `REBUILD_STATE` (`osint.pipeline.events`); `FileLock(path, timeout)` context manager (`osint.store.lake`); `COUNTRY_MIGRATION_STATE` (Task 2).
- Produces:
  - `SqliteStore.event_countries() -> list[tuple[str, str, str | None]]` (id, origin, country), `set_event_countries(rows: list[tuple[str | None, str]]) -> int` ((country, id) pairs), `event_ids_since(since: datetime) -> list[str]` (t_end >= since).
  - `osint.pipeline.migrate_countries`: `normalise_country(event_id: str, origin: str, value: str | None) -> tuple[str | None, str]` (kind: `kept|fips|name|code|unresolved|empty`), `migrate(store, rule_pass, *, now, days=30.0, lifecycle=None, dry_run=False, lock_timeout=600.0) -> dict`, `main(argv=None) -> int`. After a real run the cursor `COUNTRY_MIGRATION_STATE` is `{"done": True, "at": ..., "rewritten": n}`.

- [ ] **Step 1: Write the failing test**

Create `tests/pipeline/test_migrate_countries.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from osint.enrich.actors import ActorLexicon
from osint.enrich.rules import RuleSet
from osint.pipeline.events import rebuild_lock_path
from osint.pipeline.migrate_countries import migrate, normalise_country
from osint.pipeline.rule_pass import RulePass
from osint.store.lake import FileLock
from osint.store.sqlite import COUNTRY_MIGRATION_STATE, SqliteStore, iso

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _ev(eid, country, **kw):
    t = NOW - timedelta(hours=1)
    base = {"id": eid, "category": "armed_conflict", "title": "t", "summary": "",
            "t_start": iso(t), "t_end": iso(t), "status": "reported", "confidence": 0.5,
            "version": 1, "mention_count": 1, "source_diversity": 1, "first_seen": iso(t),
            "updated_at": iso(t), "country": country, "actors": []}
    return {**base, **kw}


@pytest.mark.parametrize(("eid", "origin", "value", "out"), [
    ("01N", "news", "UA", ("UA", "kept")), ("01N", "news", "ua", ("UA", "code")),
    ("01N", "news", "UP", ("UA", "fips")), ("01N", "news", "GM", ("GM", "kept")),
    ("01N", "news", "UKR", ("UA", "code")), ("01N", "news", "Ukraine", ("UA", "name")),
    ("01N", "news", None, (None, "empty")), ("01N", "news", "QQ", (None, "unresolved")),
    ("acled-1", "historical", "Sudan", ("SD", "name")),
    ("ucdp-1", "historical", "Russia (Soviet Union)", ("RU", "name")),
    ("ucdp-1", "historical", "RU", ("RU", "kept"))])
def test_normalise_country_by_source(eid, origin, value, out):
    assert normalise_country(eid, origin, value) == out


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "osint.sqlite")
    s.save_events(
        [_ev("01NEWSISO", "UA"),
         _ev("01NEWSFIPS", "UP", actors=[{"name": "Russia", "role": "source", "type": "RUS",
                                          "country": "RUS"}]),
         _ev("acled-SDN1", "Sudan", origin="historical", status="historical", actors=[
             {"name": "Rapid Support Forces", "role": "actor1", "type": "2", "country": ""},
             {"name": "Military Forces of Sudan (2019-)", "role": "actor2", "type": "1",
              "country": ""}]),
         _ev("ucdp-9", "Russia (Soviet Union)", origin="historical", status="historical",
             actors=[{"name": "Government of Russia (Soviet Union)", "role": "side_a",
                      "type": "", "country": ""},
                     {"name": "Government of Ukraine", "role": "side_b", "type": "",
                      "country": ""}]),
         _ev("01NEWSNULL", None)],
        documents=[{"event_id": "01NEWSISO", "doc_id": "d1", "source": "rss:t",
                    "url": "https://a.example/1",
                    "title": "Russian shelling of Kharkiv kills 3 civilians",
                    "published_at": iso(NOW - timedelta(hours=1)), "language": "en",
                    "role": "primary", "added_at": iso(NOW)}])
    yield s
    s.close()


@pytest.fixture(scope="module")
def lexicon():
    return ActorLexicon.load()


def test_migration_normalises_all_four_formats(store, lexicon):
    rp = RulePass(store, lexicon=lexicon, rules=RuleSet.load())
    dry = migrate(store, rp, now=NOW, dry_run=True)
    assert dry["dry_run"] is True and dry["rewritten"] == {"fips": 1, "name": 2, "code": 0}
    assert dry["after"]["by_format"]["name"] == 0 and dry["before"]["by_format"]["name"] == 2
    assert store.get_event("acled-SDN1")["country"] == "Sudan"  # nothing written
    assert store.get_cursor(COUNTRY_MIGRATION_STATE) is None

    rep = migrate(store, rp, now=NOW)
    ev = store.get_event
    assert [ev(e)["country"] for e in ("01NEWSISO", "01NEWSFIPS", "acled-SDN1", "ucdp-9")] \
        == ["UA", "UA", "SD", "RU"]
    a = ev("01NEWSFIPS")["actors"][0]
    assert (a["country"], a["cameo"], a["source"]) == ("RU", "RUS", "gdelt")
    assert ev("01NEWSFIPS")["foreign_involved"] == 1
    u = ev("ucdp-9")
    assert u["actor_countries"] == ["RU", "UA"] and u["foreign_involved"] == 1
    assert ev("acled-SDN1")["actor_countries"] == ["SD"] and ev("acled-SDN1")["attributed"] == 1
    assert ev("01NEWSISO")["actor_countries"] == ["RU"]
    assert all(ev(e)["lifecycle"] == "active" for e in ("01NEWSISO", "01NEWSFIPS", "ucdp-9"))
    got = {(r["field"], r["value"]) for r in store.rule_fields(["01NEWSISO"])["01NEWSISO"]}
    assert ("casualties.killed", "3") in got
    assert store.rule_fields(["ucdp-9"]) == {}
    assert rep["attributed_share"] == 0.8 and rep["events"] == 5
    assert store.get_cursor(COUNTRY_MIGRATION_STATE)["done"] is True
    with FileLock(rebuild_lock_path(store.path), timeout=0):
        pass  # the rebuild lock was released

    again = migrate(store, rp, now=NOW)
    assert again["rewritten"] == {"fips": 0, "name": 0, "code": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pipeline/test_migrate_countries.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'osint.pipeline.migrate_countries'`.

- [ ] **Step 3: Add the store helpers**

Add to `SqliteStore` after `events_for_rules`:

```python
    def event_countries(self) -> list[tuple[str, str, str | None]]:
        """(id, origin, country) of every event (country migration)."""
        with self._lock:
            return [(r[0], r[1], r[2]) for r in self._db.execute(
                "SELECT id, origin, country FROM events ORDER BY id").fetchall()]

    def set_event_countries(self, rows: list[tuple[str | None, str]]) -> int:
        """Write (country, event id) pairs to events.country; returns rows updated."""
        n = 0
        with self._lock, self._db:
            for i in range(0, len(rows), WRITE_CHUNK):
                n += self._db.executemany("UPDATE events SET country=? WHERE id=?",
                                          rows[i: i + WRITE_CHUNK]).rowcount
        return n

    def event_ids_since(self, since: datetime) -> list[str]:
        """Ids of events of any origin with t_end >= ``since``."""
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT id FROM events WHERE t_end >= ? ORDER BY id", (iso(since),))]
```

- [ ] **Step 4: Implement the migration**

Create `backend/osint/pipeline/migrate_countries.py`:

```python
"""Country normalisation migration (deterministic extraction spec §2.7).

    python -m osint.pipeline.migrate_countries [--dry-run] [--days 30]

Rewrites ``events.country`` to ISO2 by each event's source: ACLED / UCDP names; for news
and sensor events a value that is not ISO2 is read as FIPS 10-4 when two letters (legacy
GDELT), ISO3 / CAMEO when three, else as a name. Re-resolves every event's actors
(``actors[].country`` ISO2 + ``source``), fills ``actor_countries`` / ``foreign_involved`` /
``attributed`` and ``lifecycle``, and runs the rule extractors for events of the last
``--days`` days. Holds ``data/.event_rebuild.lock`` (the lock of ``retriage
--rebuild-events``): the server's EventBuilder pauses meanwhile and reloads its index
afterwards. Prints a before/after report (country-code formats, FIPS / names rewritten,
unresolved values, attributed share). ``--dry-run`` writes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from osint.core import countries
from osint.core.geo import utcnow
from osint.pipeline.events import REBUILD_STATE, rebuild_lock_path
from osint.pipeline.lifecycle import LifecycleConfig, refresh_lifecycle
from osint.pipeline.rule_pass import RulePass
from osint.store.lake import FileLock
from osint.store.sqlite import COUNTRY_MIGRATION_STATE, SqliteStore, iso

log = logging.getLogger(__name__)


def normalise_country(event_id: str, origin: str, value: str | None
                      ) -> tuple[str | None, str]:
    """(ISO2 or None, kind) for an events row. kind: kept (already ISO2) | fips | name |
    code (ISO3 / CAMEO / case) | unresolved | empty."""
    raw = (value or "").strip()
    if not raw:
        return None, "empty"
    code = raw.upper()
    if origin == "historical" or event_id.startswith(("acled-", "ucdp-")):
        if len(raw) == 2 and countries.get(code) is not None:
            return code, "kept" if code == raw else "code"  # migrated before
        cc = countries.to_iso2(raw, "name")
        return cc, "name" if cc else "unresolved"
    if len(code) == 2 and code.isalpha():
        if countries.get(code) is not None:
            return code, "kept" if code == raw else "code"
        cc = countries.to_iso2(code, "fips")  # legacy GDELT ActionGeo code
        return cc, "fips" if cc else "unresolved"
    if len(code) == 3 and code.isalpha():
        cc = countries.get(countries.to_iso2(code, "auto") or "")
        return (cc.iso2, "code") if cc else (None, "unresolved")
    cc = countries.to_iso2(raw, "name")
    return cc, "name" if cc else "unresolved"


def _distribution(values: list[str | None], top: int = 20) -> dict[str, Any]:
    fmt: Counter[str] = Counter()
    for v in values:
        s = (v or "").strip()
        if not s:
            fmt["null"] += 1
        elif len(s) == 2 and countries.get(s) is not None and s == s.upper():
            fmt["iso2"] += 1
        elif len(s) == 2:
            fmt["two_letter_other"] += 1
        elif len(s) == 3 and s.isalpha():
            fmt["three_letter"] += 1
        else:
            fmt["name"] += 1
    return {"by_format": {k: fmt.get(k, 0) for k in
                          ("iso2", "two_letter_other", "three_letter", "name", "null")},
            "top": [[c, n] for c, n in Counter(v for v in values if v).most_common(top)]}


def migrate(store: SqliteStore, rule_pass: RulePass, *, now: datetime, days: float = 30.0,
            lifecycle: LifecycleConfig | None = None, dry_run: bool = False,
            lock_timeout: float = 600.0) -> dict[str, Any]:
    """Run the migration (under the event-rebuild lock unless ``dry_run``); returns the
    report."""
    if dry_run:
        return _migrate(store, rule_pass, now=now, days=days, lifecycle=lifecycle,
                        dry_run=True)
    with FileLock(rebuild_lock_path(store.path), lock_timeout):
        return _migrate(store, rule_pass, now=now, days=days, lifecycle=lifecycle,
                        dry_run=False)


def _migrate(store: SqliteStore, rule_pass: RulePass, *, now: datetime, days: float,
             lifecycle: LifecycleConfig | None, dry_run: bool) -> dict[str, Any]:
    countries.drain_unresolved()
    rows = store.event_countries()
    kinds: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    override: dict[str, str | None] = {}
    changes: list[tuple[str | None, str]] = []
    for eid, origin, value in rows:
        cc, kind = normalise_country(eid, origin, value)
        kinds[kind] += 1
        if kind == "unresolved":
            unresolved[str(value)] += 1
        override[eid] = cc
        if cc != value:
            changes.append((cc, eid))
    if not dry_run and changes:
        store.set_event_countries(changes)
    ids = [r[0] for r in rows]
    recent = set(store.event_ids_since(now - timedelta(days=days)))
    s_recent = rule_pass.run([e for e in ids if e in recent], fields=True,
                             write=not dry_run, country_override=override)
    s_old = rule_pass.run([e for e in ids if e not in recent], fields=False,
                          lake_snippets=False, write=not dry_run, country_override=override)
    lifecycle_changed = 0
    if not dry_run:
        lifecycle_changed = refresh_lifecycle(store, now, lifecycle or LifecycleConfig(), ids)
        marker = store.get_cursor(REBUILD_STATE) or {}
        # a new rebuild marker makes a running EventBuilder reload its index (normalised
        # countries and actors in memory); "migrated" keeps migrate_events from treating it
        # as a fresh rebuild (no legacy-event deletion)
        store.set_cursor(REBUILD_STATE, {**marker, "at": utcnow().isoformat(),
                                         "migrated": True})
        store.set_cursor(COUNTRY_MIGRATION_STATE, {"done": True, "at": iso(now),
                                                   "rewritten": len(changes)})
    total = len(ids)
    resolved = s_recent.events + s_old.events
    return {
        "dry_run": dry_run, "events": total,
        "before": _distribution([r[2] for r in rows]),
        "after": _distribution(list(override.values())),
        "rewritten": {"fips": kinds["fips"], "name": kinds["name"], "code": kinds["code"]},
        "kept": kinds["kept"], "empty": kinds["empty"],
        "unresolved": dict(unresolved.most_common(50)),
        "attributed_share": round((s_recent.attributed + s_old.attributed) / total, 4)
        if total else 0.0,
        "foreign_share": round((s_recent.foreign + s_old.foreign) / total, 4) if total else 0.0,
        "actor_events": resolved, "rule_field_values": s_recent.fields,
        "rule_errors": s_recent.errors + s_old.errors,
        "lifecycle_changed": lifecycle_changed,
        "unresolved_values": countries.drain_unresolved(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osint.pipeline.migrate_countries", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--days", type=float, default=30.0,
                    help="run the rule extractors for events of the last N days")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from osint.config import Settings
    from osint.enrich.actors import ActorLexicon
    from osint.enrich.rules import RuleSet
    from osint.pipeline.events import EventsConfig
    from osint.pipeline.extract.store import ExtractionStore
    from osint.store.lake import Lake

    st = Settings()
    store = SqliteStore(st.sqlite_path)
    lake = Lake(st.lake_dir)
    ext_path = st.data_dir / "extraction.sqlite"
    extractions = ExtractionStore(ext_path) if ext_path.is_file() else None
    cfg = EventsConfig.from_file(st.config_dir / "events.yaml")
    rule_pass = RulePass(
        store, lexicon=ActorLexicon.load(st.config_dir / "actors.yaml",
                                         groups_path=st.config_dir / "terror_groups.yaml"),
        rules=RuleSet.load(st.config_dir / "extract_rules.yaml"), lake=lake,
        extractions=extractions)
    try:
        report = migrate(store, rule_pass, now=utcnow(), days=args.days,
                         lifecycle=cfg.lifecycle, dry_run=args.dry_run)
    finally:
        if extractions is not None:
            extractions.close()
        lake.close()
        store.close()
    # ASCII-escaped: the Windows console code page cannot print every country name
    print(json.dumps({"migrate_countries": report}, indent=2, ensure_ascii=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

(In the test, `attributed_share` = 4 attributed events of 5: 01NEWSISO, 01NEWSFIPS, acled-SDN1, ucdp-9; 01NEWSNULL has no actors.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/pipeline/test_migrate_countries.py tests/pipeline/test_event_rebuild.py -v`
Expected: all PASS.

- [ ] **Step 6: Regression dry run on the real data**

Run: `uv run python -m osint.pipeline.migrate_countries --dry-run`
Expected: a JSON report; check by eye and attach it to the PR:
- `before.by_format.name` is roughly the number of ACLED + UCDP rows (~19 000 on the dev DB) and `after.by_format.name` is 0;
- `rewritten.name` matches it, `unresolved` lists only a handful of odd values;
- `after.top` is led by ISO2 codes such as `UA`, `MX`, `CO`, `NG`, `PK`;
- `attributed_share` is well above zero (UCDP side names resolve: expect > 0.5).
Do not run without `--dry-run` as part of this task; the user runs the real migration.

- [ ] **Step 7: Commit**

```bash
git add backend/osint/pipeline/migrate_countries.py backend/osint/store/sqlite.py tests/pipeline/test_migrate_countries.py
git commit -m "feat(countries): migrate_countries CLI normalising events.country and filling derived columns"
```

---

### Task 11: Field precedence in the event API

**Files:**
- Modify: `backend/osint/api/events.py` (imports; new `field_precedence`; `event_detail` ~:205-226)
- Test: `tests/api/test_events_api.py`

**Interfaces:**
- Consumes: `RULE_FIELDS` (Task 6); `SqliteStore.rule_fields`, `replace_rule_fields` (Task 3); extraction store `get(event_id) -> dict | None` (`runtime.extraction_store`, records with `status`, `verdict`, `result.summary`, `result.casualties`, `result.weapons_platforms`).
- Produces: `field_precedence(rule_rows: list[dict], extraction: dict | None) -> dict` returning `{"extracted": {"headline": str | None, "summary": str | None, "casualties": {"killed": int | None, "injured": int | None}, "weapons_platforms": list[str]}, "field_source": {field: "llm" | "rule" | None for field in RULE_FIELDS}}`; `GET /api/events/{id}` adds both keys.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_events_api.py`:

```python
from osint.api.events import field_precedence

RULE_ROWS = [
    {"field": "headline", "value": "Missile hits Kharkiv", "doc_id": "d1", "quote": "x"},
    {"field": "summary", "value": "A missile hit Kharkiv.", "doc_id": "d1", "quote": "x"},
    {"field": "casualties.killed", "value": "12", "doc_id": "d2", "quote": "12 killed"},
    {"field": "weapons_platforms", "value": "missile", "doc_id": "d1", "quote": "missile"},
    {"field": "weapons_platforms", "value": "drone", "doc_id": "d2", "quote": "drone"}]


def _ext(verdict, **result):
    return {"status": "done", "verdict": verdict, "result": {
        "summary": "LLM summary.", "casualties": {"killed": 14, "injured": None},
        "weapons_platforms": [], **result}}


def test_supported_llm_values_outrank_rule_values():
    out = field_precedence(RULE_ROWS, _ext("supported"))
    assert out["extracted"] == {"headline": "Missile hits Kharkiv", "summary": "LLM summary.",
                                "casualties": {"killed": 14, "injured": None},
                                "weapons_platforms": ["drone", "missile"]}
    assert out["field_source"] == {"headline": "rule", "summary": "llm",
                                   "casualties.killed": "llm", "casualties.injured": None,
                                   "weapons_platforms": "rule"}


def test_rule_values_without_a_supported_extraction():
    for ext in (None, _ext("mismatch"), _ext("unsupported"),
                {"status": "error", "verdict": None, "result": None}):
        out = field_precedence(RULE_ROWS, ext)
        assert out["extracted"]["summary"] == "A missile hit Kharkiv."
        assert out["extracted"]["casualties"]["killed"] == 12
        assert set(out["field_source"].values()) <= {"rule", None}
    empty = field_precedence([], None)
    assert empty["extracted"]["weapons_platforms"] == [] and empty["extracted"]["headline"] is None


class _Ext:
    def __init__(self, recs):
        self.recs = recs

    def get(self, eid):
        return self.recs.get(eid)

    def get_many(self, ids):
        return {i: self.recs[i] for i in ids if i in self.recs}


def test_event_detail_reports_extracted_fields_and_their_source(store):
    eid = _event(1, lat=0, lon=0)["id"]
    store.replace_rule_fields([eid], [{**r, "event_id": eid} for r in RULE_ROWS])
    rt = _RT(store)
    rt.extraction_store = _Ext({eid: _ext("supported")})
    app = FastAPI()
    mount_events(app, rt)
    d = TestClient(app).get(f"/api/events/{eid}").json()
    assert d["extracted"]["casualties"]["killed"] == 14
    assert d["field_source"]["casualties.killed"] == "llm"
    assert d["field_source"]["headline"] == "rule"
    rt.extraction_store = None
    d = TestClient(app).get(f"/api/events/{eid}").json()
    assert d["extracted"]["casualties"]["killed"] == 12
    assert d["field_source"]["casualties.killed"] == "rule"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/api/test_events_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'field_precedence'`.

- [ ] **Step 3: Implement**

In `backend/osint/api/events.py` add after `from osint.core.sensor_status import ...`:

```python
from osint.enrich.rules import RULE_FIELDS
```

Add after `impact_layer_item`:

```python
def field_precedence(rule_rows: list[dict], extraction: dict | None) -> dict:
    """Extracted fields of an event (deterministic extraction spec §2.4): a value of an LLM
    extraction whose verdict is ``supported`` outranks the rule value (``event_fields``
    ``source='rule'``); otherwise the rule value is used. ``field_source`` says which one
    each field came from (None: neither has a value). Both stay stored."""
    rule: dict[str, list[dict]] = {}
    for r in rule_rows:
        rule.setdefault(r["field"], []).append(r)
    rec = extraction or {}
    llm: dict[str, object] = {}
    if rec.get("status") == "done" and rec.get("verdict") == "supported":
        res = rec.get("result") or {}
        cas = res.get("casualties") or {}
        llm = {"summary": str(res.get("summary") or "").strip() or None,
               "casualties.killed": cas.get("killed"),
               "casualties.injured": cas.get("injured"),
               "weapons_platforms": sorted({str(w) for w in res.get("weapons_platforms") or []
                                            if w}) or None}
    values: dict[str, object] = {}
    source: dict[str, str | None] = {}
    for f in RULE_FIELDS:
        if llm.get(f) is not None:
            values[f], source[f] = llm[f], "llm"
            continue
        rows = rule.get(f)
        if not rows:
            values[f] = [] if f == "weapons_platforms" else None
            source[f] = None
        elif f == "weapons_platforms":
            values[f], source[f] = sorted({r["value"] for r in rows}), "rule"
        elif f.startswith("casualties."):
            values[f], source[f] = int(rows[0]["value"]), "rule"
        else:
            values[f], source[f] = rows[0]["value"], "rule"
    return {"extracted": {"headline": values["headline"], "summary": values["summary"],
                          "casualties": {"killed": values["casualties.killed"],
                                         "injured": values["casualties.injured"]},
                          "weapons_platforms": values["weapons_platforms"]},
            "field_source": source}
```

In `event_detail`, directly before `return _json(ev)` add:

```python
        rule_rows = (await asyncio.to_thread(store.rule_fields, [event_id])).get(event_id, [])
        ext_store = getattr(runtime, "extraction_store", None)
        ext_rec = (await asyncio.to_thread(ext_store.get, event_id)
                   if ext_store is not None else None)
        ev.update(field_precedence(rule_rows, ext_rec))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/api -v`
Expected: all PASS.

- [ ] **Step 5: Full suite and lint**

Run: `uv run pytest -q`
Expected: all PASS (slow-marked tests may skip).

Run: `uv run ruff check backend tests scripts`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add backend/osint/api/events.py tests/api/test_events_api.py
git commit -m "feat(api): extracted fields with LLM-over-rule precedence and field_source on event detail"
```

---

## Self-review

**Spec coverage**

| Spec item | Task |
|---|---|
| §2.1 registry: ISO2/ISO3/FIPS/CAMEO/name, aliases, demonyms, committed generated data, `to_iso2`/`name`/`demonym_index`, non-state CAMEO -> None, `auto` never FIPS | 1 (+ `bbox` from `osint.signals.countries` polygons per sub-project B) |
| §2.2 GDELT ActionGeo FIPS, actor CAMEO with `cameo` kept; ACLED/UCDP names; gazetteer/PlaceHit/LLM checked; `resolve_cc` trusts country after migration | 2 |
| §2.3 lexicon `config/actors.yaml`, generated demonym phrases, hand-written ACLED/UCDP + terror-group homes, fold + whole word + longest match | 4 |
| §2.3 resolution order 1–4, `country` + `source` on every actor, dedupe by (name, country), `actor_countries` / `foreign_involved` / `attributed` | 5 (writers), 9 (live), 10 (existing rows) |
| §2.4 headline / summary / casualties (6 languages, number words ≤ 20, median over domains) / weapons; `event_fields` `source='rule'` + quote | 3 (storage), 6, 7 |
| §2.4 precedence + `field_source` | 11 |
| §2.5 lifecycle states, thresholds in `events.yaml`, sensor by last-seen | 8 |
| §2.6 touched ids (new / doc added / merged) re-extracted after commit, same worker, LLM priority unchanged | 8 (`touched`), 9 |
| §2.7 migration CLI, lock, columns + `events(country)` index as idempotent ALTERs, before/after report, rules for last 30 days | 3 (schema), 10 |
| §3 null + debug log once per value per pass + `unresolved_country`; extractor failure isolated; lexicon load fatal | 1, 4, 6, 9 |
| §4 tests: collisions, CAMEO, names; lexicon; rules per language + median; lifecycle boundaries; migration fixture with four formats; real-data dry run | 1, 4–8, 10 |

**Placeholder scan:** no TBD/TODO; every code step carries full code; every config file is given in full (`countries.yaml` is produced by the committed script from the embedded tables and the committed GeoNames TSV).

**Type consistency:** `to_iso2(value, hint)`, `get`, `all_countries`, `bbox`, `centroid`, `drain_unresolved` (T1) are used with the same names in T2/T4/T9/T10. `ActorEntry.klass`, `ActorLexicon.find/lookup`, `resolve_actors(event, docs, lexicon, llm_actors)`, `derive(actors, country)` match between T4/T5/T9. `FieldValue.row`, `ExtractDoc` fields, `RuleSet.run/extractors`, `RULE_FIELDS` match between T6/T7/T9/T11. Store methods (`set_event_derived`, `set_lifecycle`, `lifecycle_inputs`, `replace_rule_fields`, `rule_fields`, `rule_documents`, `events_for_rules`, `event_countries`, `set_event_countries`, `event_ids_since`) are defined in T3/T10 before use. `RulePass.run(..., country_override=...)` keyword is the same in T9 and T10. `PassStats` fields added in T8 (`lifecycle_changed`) and T9 (`rule_events`, `rule_fields`, `rule_errors`, `unresolved_country`) match the constructor edits.

**Decisions taken where the spec was open (also reported to the requester):**
1. "verdict `accepted`" is read as the extraction verdict `supported` (the only accepting verdict in `pipeline/extract/verdict.py`); LLM actors (§2.3 step 4) are used under the same condition.
2. LLM extractions have no headline, so `headline` is always rule-derived.
3. The derived columns are not part of `EVENT_COLUMNS`, so the clusterer's upsert never nulls them; writers pass them in the event dict or via `set_event_derived`. `event_fields` gains a `source` column (`''` for existing writers).
4. Step 3 (document titles) is skipped for ACLED/UCDP events: their only document is generated from the side names, which would duplicate actors.
5. Rule fields are not written for `historical` (ACLED/UCDP) events, which carry curated values (`fatalities_best`, ...).
6. Current code never takes `events.country` from GDELT rows (only from the primary article's gazetteer place), so the migration reads a two-letter news value as FIPS only when it is not a valid ISO2 code.
7. `resolve_cc` before the migration prefers the lat/lon reverse geocode over a stored two-letter code; `section_country` also matches ISO2-normalised UCDP rows.
8. `bbox` is the main territory (polygons within 1500 km of the largest one) from the `osint.signals.countries` Natural Earth polygons, so French Guiana / Réunion / Hawaii do not blow up camera fits; antimeridian-crossing countries return west > east, the convention `list_events(bbox=...)` already uses.
9. Generated actor phrases add "<demonym> drone/missile/strike/shelling/..." forms so that "Russian shelling of Kyiv" attributes RU; country-name forms are limited to force/government nouns, and GE/JE name forms are skipped (US-state ambiguity).
10. Ambiguous demonyms are assigned to one country (Congolese CD, Dominican DO, Guinean GN, Samoan WS) so `demonym_index` stays one-to-one.
