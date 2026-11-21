"""Known nuclear sites: Wikidata (CC0) + a curated list of publicly documented sites.

Wikidata comes from ``data/reference/nuclear_wikidata.json`` (raw SPARQL JSON written by
scripts/fetch_reference.sh; missing file = curated only). The curated list
(``nuclear_curated.json``) only holds sites documented on Wikipedia / IAEA / government pages,
each with its ``source_url``. Merge: a Wikidata record within MERGE_KM of a curated site with
a similar name is dropped (curated wins); duplicate Wikidata records are merged the same way.
"""

import difflib
import json
import logging
import re
from pathlib import Path

from osint.core.geo import haversine_km
from osint.reference.geometry import in_bbox

log = logging.getLogger(__name__)

MERGE_KM = 3.0
UNIT_KM = 3.0  # a bare "nuclear reactor" item this close to a power plant is one of its units
CURATED_LICENCE = "Project-owned list; facts from the cited public source"
WIKIDATA_LICENCE = "CC0 1.0 (Wikidata)"
TYPES = ("power", "research", "enrichment", "reprocessing", "conversion", "fuel_fab",
         "weapons_lab", "test_site", "storage", "heavy_water", "other")
# UI groups (chips): fuel cycle covers every front/back-end facility
GROUPS = {"power": "power", "research": "research", "weapons_lab": "weapons", "test_site": "test",
          **{t: "fuel_cycle" for t in ("enrichment", "reprocessing", "conversion", "fuel_fab",
                                       "storage", "heavy_water", "other")}}
# most specific Wikidata root class wins when an item matches several
_KIND_ORDER = ["power", "enrichment", "reprocessing", "test_site", "research", "storage",
               "reactor", "facility"]
_KIND_TYPE = {"reactor": "research", "facility": "other"}
_USE_STATUS = {
    "in use": "operating", "decommissioned": "decommissioned",
    "nuclear decommissioning": "decommissioning", "permanently closed": "shut_down",
    "building or structure under construction": "under_construction",
    "cancelled": None, "proposed building or structure": None,  # never built: not a site
}
_POINT = re.compile(r"Point\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", re.IGNORECASE)
_STOP = {"nuclear", "power", "plant", "station", "npp", "facility", "site", "the", "of", "complex",
         "centre", "center", "generating", "atomic", "energy", "reactor", "national", "and", "de",
         "la", "le", "unit", "units", "kernkraftwerk", "centrale", "nucléaire", "nucleaire", "aes"}


def _norm(name: str) -> str:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(t for t in toks if t not in _STOP)


def similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if set(na.split()) & set(nb.split()):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.6


def _val(b: dict, k: str) -> str | None:
    v = b.get(k)
    return v.get("value") if isinstance(v, dict) else None


def _status(use: str | None, dissolved: str | None, service: str | None) -> str | None:
    if use:
        u = use.lower()
        if u in _USE_STATUS:
            return _USE_STATUS[u]
        if "construction" in u:
            return "under_construction"
        if "decommission" in u or "closed" in u or "abandon" in u:
            return "decommissioned"
    if dissolved:
        return "shut_down"
    return "operating" if service else "unknown"


def _num(v: str | None) -> float | None:
    try:
        return round(float(v), 1) if v not in (None, "") else None
    except ValueError:
        return None


def load_wikidata(path: Path) -> list[dict]:
    if not path.is_file():
        log.warning("reference file missing: %s", path)
        return []
    try:
        bindings = json.loads(path.read_text(encoding="utf-8"))["results"]["bindings"]
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("nuclear wikidata unreadable: %s", path)
        return []
    by_id: dict[str, dict] = {}
    for b in bindings:
        uri = _val(b, "i") or ""
        qid = uri.rsplit("/", 1)[-1]
        m = _POINT.search(_val(b, "coord") or "")
        kind = _val(b, "kind")
        if not qid.startswith("Q") or not m or kind not in _KIND_ORDER:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        status = _status(_val(b, "state_of_use"), _val(b, "dissolved"), _val(b, "service"))
        if status is None:
            continue
        prev = by_id.get(qid)
        if prev and _KIND_ORDER.index(prev["_kind"]) <= _KIND_ORDER.index(kind):
            continue
        native = _val(b, "native")
        label = _val(b, "label") or native or qid
        by_id[qid] = {
            "id": "wd:" + qid, "name": label, "name_native": native if native != label else None,
            "type": _KIND_TYPE.get(kind, kind), "_kind": kind, "status": status,
            "country": _val(b, "country"), "operator": _val(b, "operator"),
            "capacity_mw": _num(_val(b, "capacity_mw")),
            "service_entry": (_val(b, "service") or "")[:10] or None,
            "dissolved": (_val(b, "dissolved") or "")[:10] or None,
            "lat": round(lat, 5), "lon": round(lon, 5), "source": "Wikidata",
            "source_url": "https://www.wikidata.org/wiki/" + qid, "licence": WIKIDATA_LICENCE,
            "wikidata": qid,
        }
    items = list(by_id.values())
    plants = [x for x in items if x["_kind"] == "power"]
    out = []
    for x in items:
        if x["_kind"] == "reactor" and any(
                haversine_km(x["lat"], x["lon"], p["lat"], p["lon"]) < UNIT_KM for p in plants):
            continue  # reactor unit of a plant already listed
        out.append(x)
    for x in out:
        del x["_kind"]
    return out


def load_curated(path: Path | None = None) -> list[dict]:
    p = path or Path(__file__).with_name("nuclear_curated.json")
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("curated nuclear sites unreadable")
        return []
    out = []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None or r.get("type") not in TYPES:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
        out.append({
            "id": "cur:" + slug, "name": r["name"], "name_native": None, "type": r["type"],
            "status": r.get("status") or "unknown", "country": r.get("country"),
            "operator": r.get("operator"), "capacity_mw": r.get("capacity_mw"),
            "service_entry": None, "dissolved": None, "lat": r["lat"], "lon": r["lon"],
            "source": "Curated", "source_url": r.get("source_url"), "licence": CURATED_LICENCE,
            "notes": r.get("notes"), "wikidata": None,
        })
    return out


def _dupe(a: dict, b: dict) -> bool:
    return haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) < MERGE_KM and similar(a["name"], b["name"])


def merge(curated: list[dict], wikidata: list[dict]) -> list[dict]:
    """Curated first; Wikidata records that duplicate a kept record are dropped (their QID and
    missing fields are folded into the kept curated record)."""
    kept: list[dict] = [dict(c) for c in curated]
    for w in wikidata:
        hit = next((k for k in kept if _dupe(k, w)), None)
        if hit is None:
            kept.append(w)
            continue
        if hit["source"] == "Curated":
            hit["wikidata"] = hit.get("wikidata") or w.get("wikidata")
            for f in ("operator", "capacity_mw", "country", "service_entry", "dissolved", "name_native"):
                if hit.get(f) is None and w.get(f) is not None:
                    hit[f] = w[f]
    return kept


class NuclearSites:
    def __init__(self, data_dir: Path, curated_path: Path | None = None) -> None:
        self.items = merge(load_curated(curated_path), load_wikidata(data_dir / "nuclear_wikidata.json"))
        log.info("nuclear sites loaded: %s", self.summary())

    def summary(self) -> dict:
        out: dict[str, dict[str, int]] = {}
        for x in self.items:
            d = out.setdefault(x["source"], {})
            d[x["type"]] = d.get(x["type"], 0) + 1
        return out

    def query(self, bbox=None, types: set[str] | None = None) -> list[dict]:
        """``types`` may hold concrete types or UI groups (power, research, fuel_cycle, weapons, test)."""
        return [x for x in self.items
                if (not types or x["type"] in types or GROUPS.get(x["type"]) in types)
                and in_bbox(x["lat"], x["lon"], bbox)]


_cache: NuclearSites | None = None


def get_nuclear() -> NuclearSites:
    global _cache
    if _cache is None:
        from osint.reference.loader import default_dir

        _cache = NuclearSites(default_dir())
    return _cache
