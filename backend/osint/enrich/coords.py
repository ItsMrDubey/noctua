"""Coordinates written in text (decimal degrees, hemisphere letters or DMS).

Used to ground LLM-extracted impact-site coordinates: a point is only accepted when the
same coordinates are actually written in the article.
"""

import re

_SEP = r"\s*[,;/]?\s*"
_DMS = (r"(\d{1,3})\s*[°º˚]\s*(?:(\d{1,2}(?:\.\d+)?)\s*['′’]\s*)?"
        r"(?:(\d{1,2}(?:\.\d+)?)\s*(?:\"|″|''|”)\s*)?")
DMS_PAIR = re.compile(_DMS + r"([NS])" + _SEP + _DMS + r"([EW])", re.IGNORECASE)
HEMI_PAIR = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*°?\s*([NS])" + _SEP
                       + r"(\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])", re.IGNORECASE)
# bare "48.1234, 37.5678": at least 3 decimals each so "1.5, 2.5" never matches
DEC_PAIR = re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{3,})\s*[,;]\s*(-?\d{1,3}\.\d{3,})(?![\d.])")
MATCH_DEG = 0.01  # ~1 km: LLM value vs the coordinates written in the text


def _dms(d: str, m: str | None, s: str | None) -> float:
    return float(d) + float(m or 0) / 60 + float(s or 0) / 3600


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def find_coordinates(text: str) -> list[tuple[float, float]]:
    """Every (lat, lon) pair written in ``text``."""
    out: list[tuple[float, float]] = []
    for m in DMS_PAIR.finditer(text or ""):
        lat = _dms(m.group(1), m.group(2), m.group(3))
        lon = _dms(m.group(5), m.group(6), m.group(7))
        lat = -lat if m.group(4).upper() == "S" else lat
        lon = -lon if m.group(8).upper() == "W" else lon
        out.append((lat, lon))
    for m in HEMI_PAIR.finditer(text or ""):
        lat, lon = float(m.group(1)), float(m.group(3))
        lat = -lat if m.group(2).upper() == "S" else lat
        lon = -lon if m.group(4).upper() == "W" else lon
        out.append((lat, lon))
    for m in DEC_PAIR.finditer(text or ""):
        out.append((float(m.group(1)), float(m.group(2))))
    return [(round(a, 6), round(b, 6)) for a, b in out if _valid(a, b)]


def grounded_coordinate(lat: float | None, lon: float | None, quote: str,
                        text: str) -> tuple[float, float] | None:
    """The site's coordinates if they are written in the article.

    An LLM value is kept only when it matches (within ``MATCH_DEG``) a pair written in
    ``text``; without an LLM value, a single pair written in the site's own ``quote`` is
    used. Otherwise None (no guessing)."""
    if lat is not None and lon is not None:
        if not _valid(lat, lon):
            return None
        for a, b in find_coordinates(text):
            if abs(a - lat) <= MATCH_DEG and abs(b - lon) <= MATCH_DEG:
                return a, b
        return None
    in_quote = find_coordinates(quote)
    return in_quote[0] if len(in_quote) == 1 else None
