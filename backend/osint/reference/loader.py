"""Lazy, cached loader for downloaded reference datasets. Tolerates missing files."""

import csv
import json
import logging
import re
from pathlib import Path

from osint.config import ROOT
from osint.reference.geometry import in_bbox, runway_rect

log = logging.getLogger(__name__)

KEEP_TYPES = {"large_airport", "medium_airport"}
MIL_ABBR = re.compile(r"(?<![A-Za-z])(AB|AFB|RAF|NAS|NAF|MCAS|AAF|ANGB|ARB)(?![A-Za-z])")
MIL_WORDS = re.compile(
    r"(Air Base|Airbase|Air Station|Air Force|Military|Naval Air|Army Air|Base A[eé]rea|Aviabaza|"
    r"Fliegerhorst|Base A[eé]rienne|Aeronautica Militare)",
    re.IGNORECASE,
)


def default_dir() -> Path:
    return ROOT / "data" / "reference"


def is_military(name: str, keywords: str = "") -> bool:
    return bool(MIL_ABBR.search(name or "") or MIL_WORDS.search(name or "")) or bool(re.search(r"military", keywords or "", re.IGNORECASE))


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def _geojson(path: Path) -> dict | None:
    if not path.is_file():
        log.warning("reference file missing: %s", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("reference file unreadable: %s", path)
        return None


def load_airports(path: Path) -> list[dict]:
    if not path.is_file():
        log.warning("reference file missing: %s", path)
        return []
    out = []
    for r in _rows(path):
        name = r.get("name") or ""
        mil = is_military(name, r.get("keywords") or "")
        typ = r.get("type") or ""
        if typ in KEEP_TYPES or (mil and typ in {"small_airport", "heliport", "seaplane_base"}):
            lat, lon = _f(r.get("latitude_deg")), _f(r.get("longitude_deg"))
            if lat is None or lon is None:
                continue
            out.append({
                "ident": r.get("ident"), "name": name, "type": typ, "lat": round(lat, 5), "lon": round(lon, 5),
                "elevation_ft": _f(r.get("elevation_ft")), "iso_country": r.get("iso_country"),
                "municipality": r.get("municipality"), "iata": r.get("iata_code") or None,
                "icao": r.get("gps_code") or r.get("icao_code") or None, "is_military": mil,
            })
    return out


def load_runways(path: Path, idents: set[str]) -> dict[str, list[dict]]:
    """ident -> runways with centreline + rectangle."""
    if not path.is_file():
        log.warning("reference file missing: %s", path)
        return {}
    out: dict[str, list[dict]] = {}
    for r in _rows(path):
        ident = r.get("airport_ident")
        if ident not in idents or r.get("closed") == "1":
            continue
        c = [_f(r.get(k)) for k in ("le_latitude_deg", "le_longitude_deg", "he_latitude_deg", "he_longitude_deg")]
        if None in c:
            continue
        width = _f(r.get("width_ft")) or 100.0
        rect = runway_rect(c[0], c[1], c[2], c[3], width)
        if not rect:
            continue
        out.setdefault(ident, []).append({
            "airport": ident, "le": r.get("le_ident"), "he": r.get("he_ident"),
            "length_ft": _f(r.get("length_ft")), "width_ft": width, "surface": r.get("surface"),
            "rect": rect,
        })
    return out


_SIZE = {"L": "large", "M": "medium", "S": "small", "V": "very_small"}


def load_ports(wpi: Path, ne: Path) -> list[dict]:
    out: list[dict] = []
    if wpi.is_file():
        for r in _rows(wpi):
            low = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            lat = _f(low.get("latitude") or low.get("lat"))
            lon = _f(low.get("longitude") or low.get("lon"))
            name = low.get("main port name") or low.get("port name") or low.get("name")
            if lat is None or lon is None or not name:
                continue
            size = low.get("harbor size") or low.get("harbor size code") or ""
            out.append({"name": name.title() if name.isupper() else name,
                        "country": low.get("country code") or low.get("country") or "",
                        "lat": round(lat, 5), "lon": round(lon, 5),
                        "harbor_size": _SIZE.get(size[:1].upper(), size.lower() or None),
                        "harbor_type": low.get("harbor type") or None, "source": "NGA WPI"})
        return out
    gj = _geojson(ne)
    for f in (gj or {}).get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        out.append({"name": f["properties"].get("name") or "", "country": "", "lat": round(lat, 5),
                    "lon": round(lon, 5), "harbor_size": None, "harbor_type": None, "source": "Natural Earth"})
    return out


def load_bases() -> list[dict]:
    p = Path(__file__).with_name("bases_data.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("curated bases unreadable")
        return []


def _round_coords(c, nd=4):
    if c and isinstance(c[0], (int, float)):
        return [round(c[0], nd), round(c[1], nd)]
    return [_round_coords(x, nd) for x in c]


def load_boundaries(path: Path) -> dict:
    """GeoJSON FeatureCollection of LineStrings, coordinates rounded to 4 dp."""
    gj = _geojson(path)
    feats = []
    for f in (gj or {}).get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") not in ("LineString", "MultiLineString"):
            continue
        p = f.get("properties") or {}
        feats.append({"type": "Feature",
                      "properties": {"name": p.get("NAME") or p.get("name") or p.get("brk_name") or ""},
                      "geometry": {"type": g["type"], "coordinates": _round_coords(g["coordinates"])}})
    return {"type": "FeatureCollection", "features": feats}


def load_countries(path: Path) -> list[dict]:
    gj = _geojson(path)
    out = []
    for f in (gj or {}).get("features", []):
        p = f.get("properties") or {}
        lon, lat = p.get("LABEL_X"), p.get("LABEL_Y")
        if lon is None or lat is None:
            continue
        out.append({"name": p.get("NAME") or p.get("ADMIN") or "", "iso_a3": p.get("ISO_A3") or p.get("ADM0_A3"),
                    "lon": round(lon, 3), "lat": round(lat, 3)})
    return out


class Reference:
    """All reference data, loaded once."""

    def __init__(self, directory: Path | None = None) -> None:
        d = directory or default_dir()
        self.dir = d
        self.airports = load_airports(d / "airports.csv")
        self.runways = load_runways(d / "runways.csv", {a["ident"] for a in self.airports})
        for a in self.airports:
            a["runway_count"] = len(self.runways.get(a["ident"], []))
        self.ports = load_ports(d / "wpi.csv", d / "ne_10m_ports.geojson")
        self.bases = load_bases()
        self.boundaries = {
            "land": load_boundaries(d / "ne_50m_admin_0_boundary_lines_land.geojson"),
            "disputed": load_boundaries(d / "ne_10m_admin_0_boundary_lines_disputed_areas.geojson"),
            "maritime": load_boundaries(d / "ne_10m_admin_0_boundary_lines_maritime_indicator.geojson"),
        }
        self.countries = load_countries(d / "ne_50m_admin_0_countries.geojson")
        log.info("reference loaded: %s", self.summary())

    def summary(self) -> dict:
        return {"airports": len(self.airports), "runways": sum(map(len, self.runways.values())),
                "ports": len(self.ports), "bases": len(self.bases),
                "boundaries": {k: len(v["features"]) for k, v in self.boundaries.items()},
                "countries": len(self.countries)}

    def query_airports(self, bbox, min_class: str = "medium", military_only: bool = False, limit: int = 20000):
        out = []
        for a in self.airports:
            if military_only and not a["is_military"]:
                continue
            if min_class == "large" and a["type"] != "large_airport" and not a["is_military"]:
                continue
            if in_bbox(a["lat"], a["lon"], bbox):
                out.append(a)
                if len(out) >= limit:
                    break
        return out

    def query_runways(self, bbox):
        out = []
        for a in self.airports:
            if in_bbox(a["lat"], a["lon"], bbox):
                out.extend(self.runways.get(a["ident"], []))
        return out

    def query_ports(self, bbox):
        return [p for p in self.ports if in_bbox(p["lat"], p["lon"], bbox)]

    def query_bases(self, bbox, types: set[str] | None = None):
        return [b for b in self.bases if (not types or b["type"] in types) and in_bbox(b["lat"], b["lon"], bbox)]


_cache: Reference | None = None


def get_reference() -> Reference:
    global _cache
    if _cache is None:
        _cache = Reference()
    return _cache
