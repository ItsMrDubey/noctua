"""NGA MSI broadcast warning text parsing: coordinates, areas, dates and keyword flags."""

import re
from datetime import UTC, datetime

from osint.core.geo import haversine_km
from osint.naval.geo import mean_point

_NUM = r"\d{1,2}(?:\.\d+)?"
# 15-30.00N | 15-30N | 15-30-12N | 15-30.00 N   (degrees-minutes[-seconds])
_LAT_DM = rf"(\d{{1,2}})-({_NUM})(?:-({_NUM}))?\s?([NS])"
_LON_DM = rf"(\d{{1,3}})-({_NUM})(?:-({_NUM}))?\s?([EW])"
PAIR_DM = re.compile(_LAT_DM + r"[\s,/]{0,4}" + _LON_DM)
# 1530N 05545E | 1530.5N 05545.2E   (compact degrees+minutes)
PAIR_COMPACT = re.compile(
    r"(?<![\d.-])(\d{2})(\d{2}(?:\.\d+)?)([NS])[\s,/]{0,4}(\d{3})(\d{2}(?:\.\d+)?)([EW])(?![\d])")
# 15.5N 055.75E  (decimal degrees)
PAIR_DEC = re.compile(
    r"(?<![\d.-])(\d{1,2}\.\d+)\s?([NS])[\s,/]{0,4}(\d{1,3}\.\d+)\s?([EW])(?![\d])")

_ITEM = re.compile(r"(?:(?<=\s)|^)(?:[A-Z]|\d{1,2})\.\s+(?=\S)")
_CIRCLE = re.compile(r"WITHIN\s+(\d+(?:\.\d+)?)\s+(NAUTICAL\s+MILES?|MILES?|NM|KILOMETERS?|KM)\b"
                     r"(?:\s+RADIUS)?\s+OF")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
# 201530Z NOV 2026 | 201530Z NOV 26 | 201530 UTC NOV 26
_DTG = re.compile(r"\b(\d{2})(\d{2})(\d{2})\s?(?:Z|UTC)\s+([A-Z]{3})\w*\s+(\d{2,4})\b")
_CANCEL = re.compile(r"CANCEL\s+(?:THIS\s+)?(?:MSG|MESSAGE)\s+(\d{6}\s?(?:Z|UTC)\s+[A-Z]{3}\w*\s+\d{2,4})")

NM_KM = 1.852


def _dm(deg: str, minutes: str, seconds: str | None, hemi: str) -> float:
    v = float(deg) + float(minutes) / 60.0 + (float(seconds) / 3600.0 if seconds else 0.0)
    return -v if hemi in "SW" else v


def parse_coords(text: str) -> list[tuple[float, float]]:
    """Every (lat, lon) pair in ``text`` in document order (all supported formats)."""
    found: list[tuple[int, float, float]] = []
    taken: list[tuple[int, int]] = []

    def free(m: re.Match) -> bool:
        return all(m.end() <= a or m.start() >= b for a, b in taken)

    for m in PAIR_DM.finditer(text):
        lat = _dm(m.group(1), m.group(2), m.group(3), m.group(4))
        lon = _dm(m.group(5), m.group(6), m.group(7), m.group(8))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and float(m.group(2)) < 60:
            found.append((m.start(), lat, lon))
            taken.append(m.span())
    for m in PAIR_COMPACT.finditer(text):
        if not free(m):
            continue
        lat = _dm(m.group(1), m.group(2), None, m.group(3))
        lon = _dm(m.group(4), m.group(5), None, m.group(6))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and float(m.group(2)) < 60:
            found.append((m.start(), lat, lon))
            taken.append(m.span())
    for m in PAIR_DEC.finditer(text):
        if not free(m):
            continue
        lat = float(m.group(1)) * (-1 if m.group(2) == "S" else 1)
        lon = float(m.group(3)) * (-1 if m.group(4) == "W" else 1)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            found.append((m.start(), lat, lon))
            taken.append(m.span())
    found.sort()
    return [(la, lo) for _, la, lo in found]


def _segments(text: str) -> list[str]:
    """Split on numbered / lettered items ("1.", "A.") keeping their text."""
    starts = [m.start() for m in _ITEM.finditer(text)]
    if not starts or starts[0] != 0:
        starts = [0, *starts]
    return [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]


def _mode(seg: str) -> str | None:
    if _CIRCLE.search(seg):
        return "circle"
    if re.search(r"\bBOUN(?:D|DED)\b|\bAREAS?\b|\bPOLYGON\b", seg):
        return "polygon"
    if re.search(r"TRACK\s*LINE|TRACKLINE|\bJOINING\b|\bROUTE\b", seg):
        return "line"
    return None


def parse_geometries(text: str) -> list[dict]:
    """Geometries in a warning: {type: polygon|circle|line|point, coords: [[lon, lat]...],
    radius_km?}. A mode keyword ("AREA BOUNDED BY", "WITHIN 5 MILES OF", "TRACKLINE
    JOINING") carries over to the following lettered items until another one appears."""
    text = " ".join((text or "").upper().split())
    out: list[dict] = []
    mode: str | None = None
    radius_km = 0.0
    for seg in _segments(text):
        own = _mode(seg)
        if re.match(r"\d{1,2}\.\s", seg):  # a new numbered paragraph resets the mode
            mode = None
        if own is not None:
            mode = own
            if own == "circle":
                m = _CIRCLE.search(seg)
                r = float(m.group(1))
                unit = m.group(2)
                radius_km = r if unit.startswith(("KILO", "KM")) else r * NM_KM
        pts = parse_coords(seg)
        if not pts:
            continue
        coords = [[round(lo, 5), round(la, 5)] for la, lo in pts]
        if mode == "circle":
            for c in coords:
                out.append({"type": "circle", "coords": [c], "radius_km": round(radius_km, 2)})
        elif mode == "polygon" and len(coords) >= 3:
            ring = coords if coords[0] == coords[-1] else [*coords, coords[0]]
            out.append({"type": "polygon", "coords": ring})
        elif mode in ("line", "polygon") and len(coords) >= 2:
            out.append({"type": "line", "coords": coords})
        else:
            out.extend({"type": "point", "coords": [c]} for c in coords)
    return out


def geometry_summary(geoms: list[dict]) -> tuple[float, float, float] | None:
    """(lat, lon, radius_km) covering every geometry, or None."""
    pts: list[tuple[float, float]] = []
    pad = 0.0
    for g in geoms:
        for lo, la in g["coords"]:
            pts.append((la, lo))
        pad = max(pad, float(g.get("radius_km") or 0.0))
    if not pts:
        return None
    lat, lon = mean_point(pts)
    radius = max(haversine_km(lat, lon, la, lo) for la, lo in pts) + pad
    return lat, lon, radius


def parse_dtg(raw: str) -> datetime | None:
    """'201530Z NOV 2026' -> datetime (UTC)."""
    m = _DTG.search((raw or "").upper())
    if not m:
        return None
    day, hh, mm, mon, year = m.groups()
    month = _MONTHS.get(mon[:3])
    if month is None:
        return None
    y = int(year)
    y = y + 2000 if y < 100 else y
    try:
        return datetime(y, month, int(day), int(hh), int(mm), tzinfo=UTC)
    except ValueError:
        return None


def cancel_time(text: str) -> datetime | None:
    m = _CANCEL.search(" ".join((text or "").upper().split()))
    return parse_dtg(m.group(1)) if m else None


def keyword_flags(text: str, strong: list[str], medium: list[str]) -> tuple[list[str], list[str]]:
    up = " ".join((text or "").upper().split())
    s = [k for k in strong if re.search(rf"\b{re.escape(k)}\b", up)]
    m = [k for k in medium if re.search(rf"\b{re.escape(k)}\b", up)]
    return s, m
