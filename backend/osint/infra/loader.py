"""Lazy, cached loader for the normalised infrastructure files written by
``osint.infra.datasets``. Tolerates missing files (empty layer + ``available: False``)."""

import csv
import json
import logging
import threading
from functools import cached_property
from pathlib import Path

from osint.reference.geometry import in_bbox
from osint.reference.loader import default_dir, is_military

log = logging.getLogger(__name__)

# GPPD primary_fuel -> UI group
FUEL_GROUP = {"Nuclear": "nuclear", "Coal": "coal", "Petcoke": "coal", "Gas": "gas",
              "Oil": "oil", "Hydro": "hydro", "Wind": "wind", "Solar": "solar",
              "Wave and Tidal": "hydro"}
FUEL_GROUPS = ("nuclear", "coal", "gas", "oil", "hydro", "wind", "solar", "other")
POINT_LAYERS = ("power", "refineries", "lng", "dams", "ground_stations", "landings")
AIRFIELD_TYPES = ("heliport", "small_airport", "seaplane_base")


def _json(path: Path):
    if not path.is_file():
        log.warning("infra file missing: %s", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("infra file unreadable: %s", path)
        return None


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def load_power(path: Path) -> list[dict]:
    if not path.is_file():
        log.warning("infra file missing: %s", path)
        return []
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            lat, lon = _f(r.get("latitude")), _f(r.get("longitude"))
            if lat is None or lon is None:
                continue
            fuel = r.get("primary_fuel") or "Other"
            year = _f(r.get("commissioning_year"))
            out.append({
                "id": r.get("gppd_idnr") or f"{lat:.4f},{lon:.4f}", "name": r.get("name") or "",
                "fuel": fuel, "group": FUEL_GROUP.get(fuel, "other"),
                "other_fuels": [x for x in (r.get("other_fuel1"), r.get("other_fuel2"),
                                            r.get("other_fuel3")) if x] or None,
                "mw": _f(r.get("capacity_mw")), "country": r.get("country_long") or r.get("country"),
                "owner": r.get("owner") or None, "year": int(year) if year else None,
                "lat": round(lat, 5), "lon": round(lon, 5), "source": r.get("source") or None,
                "url": r.get("url") if (r.get("url") or "").startswith("https://") else None,
            })
    return out


def load_airfields(path: Path) -> list[dict]:
    """OurAirports heliports, small airports and seaplane bases (the reference layer already
    serves large/medium airports)."""
    if not path.is_file():
        log.warning("infra file missing: %s", path)
        return []
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            typ = r.get("type") or ""
            if typ not in AIRFIELD_TYPES:
                continue
            lat, lon = _f(r.get("latitude_deg")), _f(r.get("longitude_deg"))
            if lat is None or lon is None:
                continue
            name = r.get("name") or ""
            out.append({"id": r.get("ident"), "name": name, "type": typ, "lat": round(lat, 5),
                        "lon": round(lon, 5), "country": r.get("iso_country"),
                        "municipality": r.get("municipality") or None,
                        "elevation_ft": _f(r.get("elevation_ft")),
                        "is_military": is_military(name, r.get("keywords") or "")})
    return out


class Infra:
    """All infrastructure datasets; each one is parsed on first use."""

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or default_dir()
        self._lock = threading.Lock()

    def _has(self, name: str) -> bool:
        return (self.dir / name).is_file()

    @cached_property
    def _cables(self) -> dict:
        return _json(self.dir / "cables_tg.json") or {"cables": [], "landings": []}

    @property
    def cables(self) -> list[dict]:
        return self._cables["cables"]

    @property
    def landings(self) -> list[dict]:
        return self._cables["landings"]

    @cached_property
    def pipelines(self) -> list[dict]:
        return _json(self.dir / "pipelines_osm.json") or []

    @cached_property
    def power(self) -> list[dict]:
        return load_power(self.dir / "gppd.csv")

    @cached_property
    def refineries(self) -> list[dict]:
        return _json(self.dir / "refineries_osm.json") or []

    @cached_property
    def _wikidata(self) -> dict:
        return _json(self.dir / "infra_wikidata.json") or {}

    @property
    def lng(self) -> list[dict]:
        return self._wikidata.get("lng") or []

    @property
    def dams(self) -> list[dict]:
        return self._wikidata.get("dams") or []

    @property
    def ground_stations(self) -> list[dict]:
        return self._wikidata.get("ground_stations") or []

    @cached_property
    def airfields(self) -> list[dict]:
        return load_airfields(self.dir / "airports.csv")

    @cached_property
    def _power_by_id(self) -> dict[str, dict]:
        return {p["id"]: p for p in self.power}

    def power_item(self, pid: str) -> dict | None:
        return self._power_by_id.get(pid)

    def points(self, layer: str) -> list[dict]:
        if layer not in POINT_LAYERS:
            raise KeyError(layer)
        with self._lock:  # first parse of a layer happens once
            return getattr(self, layer)

    def query_airfields(self, bbox, types: set[str] | None = None, military_only: bool = False,
                        limit: int = 5000) -> tuple[list[dict], bool]:
        out = []
        with self._lock:
            items = self.airfields
        for a in items:
            if types and a["type"] not in types:
                continue
            if military_only and not a["is_military"]:
                continue
            if in_bbox(a["lat"], a["lon"], bbox):
                if len(out) >= limit:
                    return out, True
                out.append(a)
        return out, False

    def summary(self) -> dict:
        files = {"cables": "cables_tg.json", "landings": "cables_tg.json",
                 "pipelines": "pipelines_osm.json", "power": "gppd.csv",
                 "refineries": "refineries_osm.json", "lng": "infra_wikidata.json",
                 "dams": "infra_wikidata.json", "ground_stations": "infra_wikidata.json",
                 "airfields": "airports.csv"}
        return {k: {"available": self._has(f), "count": len(getattr(self, k)) if self._has(f) else 0}
                for k, f in files.items()}


_cache: Infra | None = None
_cache_lock = threading.Lock()


def get_infra() -> Infra:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = Infra()
        return _cache


def reset_infra() -> None:
    """Drop the cached datasets (after a refresh)."""
    global _cache
    with _cache_lock:
        _cache = None
