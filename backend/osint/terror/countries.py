"""Country names <-> ISO 3166-1 alpha-2, FIPS 10-4 -> ISO2 and capitals.

A static table derived from GeoNames ``countryInfo.txt`` (CC BY 4.0) ships with the
package (``data/countries.tsv``) so parsers work without the gazetteer download. The US
State Department tags advisories with FIPS 10-4 codes (``YM`` = Yemen, ``UK`` = United
Kingdom), UCDP uses its own spellings ("DR Congo (Zaire)") -- both are mapped here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).with_name("data") / "countries.tsv"

# extra spellings seen in UCDP, State Dept titles, sanctions lists and news
ALIASES: dict[str, list[str]] = {
    "US": ["United States of America", "USA", "U.S.", "US", "America", "United States"],
    "GB": ["United Kingdom of Great Britain and Northern Ireland", "UK", "U.K.", "Britain",
           "Great Britain", "England", "Scotland", "Wales", "Northern Ireland"],
    "RU": ["Russia (Soviet Union)", "Russian Federation"],
    "CD": ["DR Congo (Zaire)", "DR Congo", "Democratic Republic of the Congo", "DRC",
           "Congo, Democratic Republic of the", "Democratic Republic of Congo",
           "The Democratic Republic of the Congo", "Congo (Kinshasa)"],
    "CG": ["Congo", "Republic of the Congo", "Congo (Brazzaville)", "Republic of Congo"],
    "CI": ["Ivory Coast", "Cote d'Ivoire", "Côte d'Ivoire"],
    "MM": ["Myanmar (Burma)", "Burma"],
    "KH": ["Cambodia (Kampuchea)"],
    "YE": ["Yemen (North Yemen)"],
    "RS": ["Serbia (Yugoslavia)"],
    "MK": ["Macedonia, FYR", "North Macedonia", "Macedonia"],
    "SZ": ["Kingdom of eSwatini (Swaziland)", "Eswatini", "Swaziland"],
    "BA": ["Bosnia-Herzegovina", "Bosnia and Herzegovina"],
    "IR": ["Iran, Islamic Republic of"],
    "SY": ["Syrian Arab Republic"],
    "KP": ["North Korea", "Korea, North", "DPRK", "Democratic People's Republic of Korea"],
    "KR": ["South Korea", "Korea, South", "Republic of Korea"],
    "PS": ["Palestine", "Palestinian Territories", "Palestinian Territory", "Gaza",
           "West Bank", "The West Bank", "Gaza Strip"],
    "TR": ["Turkey", "Türkiye", "Turkiye"],
    "CZ": ["Czech Republic", "Czechia"],
    "LA": ["Laos"],
    "VN": ["Vietnam"],
    "BO": ["Bolivia"],
    "VE": ["Venezuela"],
    "TZ": ["Tanzania"],
    "MD": ["Moldova"],
    "BN": ["Brunei"],
    "CV": ["Cabo Verde", "Cape Verde"],
    "TL": ["Timor-Leste", "East Timor"],
    "GM": ["The Gambia", "Gambia"],
    "BS": ["The Bahamas", "Bahamas"],
    "VA": ["Holy See", "Vatican City", "Vatican"],
    "FM": ["Micronesia"],
    "SS": ["South Sudan"],
    "XK": ["Kosovo"],
    "TW": ["Taiwan"],
    "HK": ["Hong Kong"],
    "MO": ["Macau", "Macao"],
    "KN": ["Saint Kitts and Nevis"],
    "LC": ["Saint Lucia"],
    "VC": ["Saint Vincent and the Grenadines"],
    "SH": ["Saint Helena"],
}
# FIPS codes the State Department uses that are not (or not uniquely) in countryInfo
FIPS_EXTRA = {"KV": "XK", "GZ": "PS", "WE": "PS", "UK": "GB", "RS": "RU"}
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Country:
    iso2: str
    iso3: str
    fips: str
    name: str
    capital: str
    continent: str


def fold(s: str) -> str:
    """Case/diacritics/punctuation-insensitive key ("Côte d'Ivoire" -> "cote d ivoire")."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w]+", " ", s.casefold())
    return _WS.sub(" ", s).strip()


@lru_cache(maxsize=1)
def table() -> dict[str, Country]:
    out: dict[str, Country] = {}
    for line in DATA.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) >= 6:
            out[p[0]] = Country(p[0], p[1], p[2], p[3], p[4], p[5])
    return out


@lru_cache(maxsize=1)
def _name_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for c in table().values():
        idx.setdefault(fold(c.name), c.iso2)
        idx.setdefault(fold(c.iso3), c.iso2)
    for iso2, names in ALIASES.items():
        for n in names:
            idx[fold(n)] = iso2
    return idx


@lru_cache(maxsize=1)
def _fips_index() -> dict[str, str]:
    idx = {c.fips: c.iso2 for c in table().values() if c.fips}
    idx.update(FIPS_EXTRA)
    return idx


def iso2(name: str | None) -> str | None:
    """ISO2 for a country name / alias / ISO3 / ISO2; None when unknown."""
    if not name:
        return None
    raw = name.strip()
    if len(raw) == 2 and raw.isalpha() and raw.upper() in table():
        return raw.upper()
    return _name_index().get(fold(raw))


def from_fips(code: str | None) -> str | None:
    return _fips_index().get((code or "").strip().upper())


def name(iso: str | None) -> str | None:
    c = table().get((iso or "").upper())
    return c.name if c else None


def capital(iso: str | None) -> str | None:
    c = table().get((iso or "").upper())
    return c.capital if c else None


@lru_cache(maxsize=1)
def capitals() -> dict[str, str]:
    """folded capital name -> ISO2 (used by the metonym guard: "Kyiv says ...")."""
    out = {fold(c.capital): c.iso2 for c in table().values() if c.capital}
    # seats of government / metonyms that are not the formal capital in GeoNames
    out.update({"washington": "US", "washington dc": "US", "kyiv": "UA", "kiev": "UA",
                "jerusalem": "IL", "tel aviv": "IL", "the hague": "NL", "brussels": "BE",
                "beijing": "CN", "moscow": "RU", "tehran": "IR", "ankara": "TR",
                "london": "GB", "paris": "FR", "berlin": "DE", "islamabad": "PK",
                "new delhi": "IN", "delhi": "IN", "riyadh": "SA", "damascus": "SY",
                "baghdad": "IQ", "kabul": "AF", "cairo": "EG", "pyongyang": "KP",
                "seoul": "KR", "tokyo": "JP", "taipei": "TW", "minsk": "BY",
                "warsaw": "PL", "rome": "IT", "madrid": "ES", "abuja": "NG",
                "nairobi": "KE", "mogadishu": "SO", "sanaa": "YE", "doha": "QA",
                "beirut": "LB", "amman": "JO", "ottawa": "CA", "canberra": "AU"})
    return out
