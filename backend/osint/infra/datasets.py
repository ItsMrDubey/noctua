"""Critical-infrastructure and watchlist datasets: registry, downloads and normalisation.

Every dataset is fetched into ``data/reference/`` (never committed) and normalised once at
download time into a compact file the loader reads quickly. ``infra_meta.json`` records
when each dataset was fetched; ``refresh()`` only re-downloads datasets older than their
``max_age_days`` (or all with ``force``). Downloads go to ``<file>.part`` and are renamed
only after they parsed, so a failed refresh keeps the previous copy.

Run ``osint infra-fetch [ids...] [--force]``; the worker's watch job also refreshes stale
OpenSanctions data on its own (config/infra.yaml).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from osint.infra.geo import simplify

log = logging.getLogger(__name__)

# Wikimedia's robot policy rejects user agents without a contact URL / address: set
# OSINT_CONTACT (e.g. a project URL or mailbox) to identify this installation.
USER_AGENT = "osint-poc/0.1 ({contact}; reference data fetch) httpx/" + httpx.__version__
DEFAULT_CONTACT = "https://meta.wikimedia.org/wiki/User-Agent_policy; contact via OSINT_CONTACT"
META_FILE = "infra_meta.json"
OVERPASS = ("https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
TG = "https://www.submarinecablemap.com/api/v3"
GPPD_ZIP = "https://wri-dataportal-prod.s3.amazonaws.com/manual/global_power_plant_database_v_1_3.zip"
OS_MARITIME = "https://data.opensanctions.org/datasets/latest/maritime/maritime.csv"
OS_TARGETS = "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"
# NGA's MSI site is often down (503 apology page); the RIMES CKAN datastore holds a copy
WPI_URLS = ("https://msi.nga.mil/api/publications/download?key=16694622/SFH00000/UpdatedPub150.csv&type=download",
            "https://ckan.rimes.int/datastore/dump/23538e38-830f-4df1-b69d-4469fa6ee7af")
PIPE_TOL_DEG = 0.004  # ~400 m: plenty for a globe overlay and proximity screening
OS_KEEP_SCHEMA = {"Vessel", "Airplane"}

PIPELINE_QUERY = """[out:json][timeout:600];
(way["man_made"="pipeline"]["usage"="transmission"]["substance"~"^(gas|natural_gas|oil|crude_oil|lng|lpg|oil;gas|gas;oil|condensate)$"];
 way["man_made"="pipeline"]["location"="underwater"]["substance"~"^(gas|natural_gas|oil|crude_oil|lng|lpg|oil;gas|gas;oil|condensate)$"];
 way["seamark:type"="pipeline_submarine"]["seamark:pipeline_submarine:product"~"^(gas|oil|chemicals)$"];);
out tags geom;"""
REFINERY_QUERY = """[out:json][timeout:300];
(way["industrial"="refinery"];relation["industrial"="refinery"];node["industrial"="refinery"];);
out center tags;"""
_WD_SELECT = """SELECT ?i ?label ?coord ?country ?operator ?height ?inception WHERE {{
  ?i wdt:P31/wdt:P279* wd:{cls} ; wdt:P625 ?coord . {extra}
  OPTIONAL {{ ?i wdt:P17 ?c . ?c rdfs:label ?country FILTER(lang(?country)="en") }}
  OPTIONAL {{ ?i wdt:P137 ?o . ?o rdfs:label ?operator FILTER(lang(?operator)="en") }}
  OPTIONAL {{ ?i wdt:P571 ?inception }}
  OPTIONAL {{ ?i rdfs:label ?label FILTER(lang(?label)="en") }}
}}"""
WIKIDATA_CLASSES = {
    # id: (class QID, extra triple patterns)
    "lng": ("Q15709854", ""),  # liquefied natural gas terminal
    "ground_stations": ("Q1349167", ""),  # ground station (satellite)
    "dams": ("Q12323", "?i p:P2048/psn:P2048/wikibase:quantityAmount ?height FILTER(?height >= 50)"),
}


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Dataset:
    id: str
    name: str
    files: tuple[str, ...]
    max_age_days: float
    fetch: Callable[[Fetcher], dict]
    licence: str
    url: str
    attribution: str
    commercial: str  # "ok" | "review" | "no" (as in api/licences.py)
    notes: str = ""


# ------------------------------------------------------------------ download plumbing


class Fetcher:
    def __init__(self, directory: Path, client: httpx.Client | None = None) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        ua = USER_AGENT.format(contact=os.environ.get("OSINT_CONTACT") or DEFAULT_CONTACT)
        self.http = client or httpx.Client(headers={"User-Agent": ua},
                                           follow_redirects=True, timeout=600.0)

    def get(self, url: str, *, params: dict | None = None, data: dict | None = None,
            headers: dict | None = None) -> bytes:
        r = (self.http.post(url, data=data, headers=headers) if data is not None
             else self.http.get(url, params=params, headers=headers))
        if r.status_code != 200:
            raise FetchError(f"{url}: HTTP {r.status_code}")
        body = r.content
        if body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            raise FetchError(f"{url}: got an HTML page instead of data")
        return body

    def first(self, urls: tuple[str, ...], **kw) -> bytes:
        errors = []
        for u in urls:
            try:
                return self.get(u, **kw)
            except (FetchError, httpx.HTTPError) as exc:
                errors.append(str(exc))
        raise FetchError("; ".join(errors))

    def overpass(self, query: str) -> dict:
        try:
            return json.loads(self.first(OVERPASS, data={"data": query}))
        except ValueError as exc:
            raise FetchError(f"overpass: bad JSON ({exc})") from None

    def sparql(self, query: str) -> list[dict]:
        body = self.get(WIKIDATA_SPARQL, params={"query": query},
                        headers={"Accept": "application/sparql-results+json"})
        try:
            return json.loads(body)["results"]["bindings"]
        except (ValueError, KeyError) as exc:
            raise FetchError(f"wikidata: bad response ({exc})") from None

    def write(self, name: str, data: bytes | str) -> None:
        """Atomic replace of data/reference/<name>."""
        p = self.dir / name
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        os.replace(tmp, p)

    def write_json(self, name: str, obj: object) -> None:
        self.write(name, json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


# ------------------------------------------------------------------ normalisers (pure)


def norm_cables(cable_geo: dict, landing_geo: dict) -> dict:
    cables = []
    for f in cable_geo.get("features") or []:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        if g.get("type") == "LineString":
            lines = [g["coordinates"]]
        elif g.get("type") == "MultiLineString":
            lines = g["coordinates"]
        else:
            continue
        lines = [[[round(x, 4), round(y, 4)] for x, y, *_ in ln] for ln in lines if len(ln) >= 2]
        if lines:
            cables.append({"id": p.get("id") or p.get("feature_id"), "name": p.get("name") or "",
                           "color": p.get("color") or None, "lines": lines})
    landings = []
    for f in landing_geo.get("features") or []:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        landings.append({"id": p.get("id"), "name": p.get("name") or "", "lat": round(lat, 5),
                         "lon": round(lon, 5), "tbd": bool(p.get("is_tbd"))})
    return {"cables": cables, "landings": landings}


def norm_pipelines(overpass: dict, tol_deg: float = PIPE_TOL_DEG) -> list[dict]:
    out = []
    for e in overpass.get("elements") or []:
        geom = e.get("geometry") or []
        if e.get("type") != "way" or len(geom) < 2:
            continue
        t = e.get("tags") or {}
        coords = simplify([[g["lon"], g["lat"]] for g in geom], tol_deg)
        sub = (t.get("substance") or t.get("seamark:pipeline_submarine:product") or "").lower()
        offshore = t.get("location") == "underwater" or t.get("seamark:type") == "pipeline_submarine"
        out.append({
            "id": f"osm:w{e['id']}", "name": t.get("name") or t.get("ref") or "",
            "substance": "oil" if "oil" in sub else "gas" if ("gas" in sub or sub in ("lng", "lpg")) else sub or "other",
            "operator": t.get("operator") or None, "offshore": offshore,
            "diameter": t.get("diameter") or None, "start_date": t.get("start_date") or None,
            "line": [[round(x, 4), round(y, 4)] for x, y in coords],
        })
    return out


def norm_refineries(overpass: dict) -> list[dict]:
    out = []
    for e in overpass.get("elements") or []:
        c = e.get("center") or ({"lat": e["lat"], "lon": e["lon"]} if "lat" in e else None)
        if not c:
            continue
        t = e.get("tags") or {}
        out.append({"id": f"osm:{e['type'][0]}{e['id']}", "name": t.get("name:en") or t.get("name") or "",
                    "operator": t.get("operator") or None, "lat": round(c["lat"], 5),
                    "lon": round(c["lon"], 5), "product": t.get("product") or None,
                    "wikidata": t.get("wikidata") or None})
    return out


_POINT = re.compile(r"Point\(\s*(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)")


def _v(b: dict, k: str) -> str | None:
    x = b.get(k)
    return x.get("value") if isinstance(x, dict) else None


def norm_wikidata(bindings: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for b in bindings:
        qid = (_v(b, "i") or "").rsplit("/", 1)[-1]
        m = _POINT.search(_v(b, "coord") or "")
        if not qid.startswith("Q") or not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        prev = by_id.get(qid)
        if prev:  # several OPTIONAL combinations: keep the first, fill gaps
            for k, key in (("country", "country"), ("operator", "operator")):
                if prev[k] is None:
                    prev[k] = _v(b, key)
            continue
        h = _v(b, "height")
        by_id[qid] = {
            "id": "wd:" + qid, "wikidata": qid, "name": _v(b, "label") or qid,
            "lat": round(lat, 5), "lon": round(lon, 5), "country": _v(b, "country"),
            "operator": _v(b, "operator"), "height_m": round(float(h), 1) if h else None,
            "inception": (_v(b, "inception") or "")[:4] or None,
        }
    return list(by_id.values())


def filter_os_targets(raw: Iterable[str], out: io.TextIOBase) -> int:
    """Stream OpenSanctions targets.simple.csv keeping only Vessel / Airplane rows."""
    csv.field_size_limit(1 << 26)
    rd = csv.DictReader(raw)
    cols = ["id", "schema", "name", "aliases", "countries", "identifiers", "sanctions",
            "program_ids", "dataset", "first_seen", "last_seen", "last_change"]
    w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    n = 0
    for row in rd:
        if row.get("schema") in OS_KEEP_SCHEMA:
            w.writerow(row)
            n += 1
    return n


# ------------------------------------------------------------------ fetchers


def _fetch_cables(f: Fetcher) -> dict:
    data = norm_cables(json.loads(f.get(f"{TG}/cable/cable-geo.json")),
                       json.loads(f.get(f"{TG}/landing-point/landing-point-geo.json")))
    if not data["cables"]:
        raise FetchError("submarine cables: no features")
    f.write_json("cables_tg.json", data)
    return {"count": len(data["cables"]), "landings": len(data["landings"])}


def _fetch_power(f: Fetcher) -> dict:
    z = zipfile.ZipFile(io.BytesIO(f.get(GPPD_ZIP)))
    name = next((n for n in z.namelist() if n.endswith("global_power_plant_database.csv")), None)
    if not name:
        raise FetchError("GPPD zip without global_power_plant_database.csv")
    body = z.read(name)
    f.write("gppd.csv", body)
    return {"count": body.count(b"\n") - 1}


def _fetch_pipelines(f: Fetcher) -> dict:
    items = norm_pipelines(f.overpass(PIPELINE_QUERY))
    if not items:
        raise FetchError("pipelines: overpass returned nothing")
    f.write_json("pipelines_osm.json", items)
    return {"count": len(items), "offshore": sum(1 for x in items if x["offshore"])}


def _fetch_refineries(f: Fetcher) -> dict:
    items = norm_refineries(f.overpass(REFINERY_QUERY))
    if not items:
        raise FetchError("refineries: overpass returned nothing")
    f.write_json("refineries_osm.json", items)
    return {"count": len(items)}


def _fetch_wikidata(f: Fetcher) -> dict:
    out, counts = {}, {}
    for key, (cls, extra) in WIKIDATA_CLASSES.items():
        out[key] = norm_wikidata(f.sparql(_WD_SELECT.format(cls=cls, extra=extra)))
        counts[key] = len(out[key])
        time.sleep(1.0)  # be polite to the public endpoint
    f.write_json("infra_wikidata.json", out)
    return {"count": sum(counts.values()), **counts}


def _fetch_wpi(f: Fetcher) -> dict:
    body = f.first(WPI_URLS)
    if b"Main Port Name" not in body[:4000]:
        raise FetchError("WPI: unexpected CSV header")
    f.write("wpi.csv", body)
    return {"count": body.count(b"\n") - 1}


def _fetch_opensanctions(f: Fetcher) -> dict:
    mar = f.get(OS_MARITIME)
    if b'"imo"' not in mar[:500] and b"imo" not in mar[:500]:
        raise FetchError("OpenSanctions maritime.csv: unexpected header")
    f.write("os_maritime.csv", mar)
    # targets.simple.csv (~80 MB) streamed and filtered to vessels / aircraft
    tmp = f.dir / "os_targets_va.csv.part"
    with f.http.stream("GET", OS_TARGETS) as r:
        if r.status_code != 200:
            raise FetchError(f"{OS_TARGETS}: HTTP {r.status_code}")
        lines = (ln + "\n" for ln in r.iter_lines())
        with tmp.open("w", encoding="utf-8", newline="") as out:
            n = filter_os_targets(lines, out)
    os.replace(tmp, f.dir / "os_targets_va.csv")
    return {"count": mar.count(b"\n") - 1, "targets": n}


DATASETS: dict[str, Dataset] = {d.id: d for d in [
    Dataset("cables", "Submarine cables & landing points (TeleGeography)",
            ("cables_tg.json",), 30, _fetch_cables,
            "TeleGeography public map (CC BY-SA 4.0 for map references); underlying data under a "
            "commercial licence", "https://www.submarinecablemap.com/",
            "Submarine cable data: TeleGeography submarinecablemap.com", "no",
            "Public JSON behind the free map. Commercial use of the underlying data requires a "
            "TeleGeography data licence."),
    Dataset("power", "Global Power Plant Database v1.3 (WRI)", ("gppd.csv",), 365, _fetch_power,
            "CC BY 4.0", "https://datasets.wri.org/dataset/globalpowerplantdatabase",
            "Global Power Plant Database, World Resources Institute (CC BY 4.0)", "ok",
            "Static release (2021); capacities may be dated."),
    Dataset("pipelines", "Oil & gas transmission / submarine pipelines (OpenStreetMap)",
            ("pipelines_osm.json",), 30, _fetch_pipelines, "ODbL 1.0",
            "https://www.openstreetmap.org/copyright", "(c) OpenStreetMap contributors (ODbL)",
            "review", "Share-alike on derived databases; coverage is uneven by country."),
    Dataset("refineries", "Oil refineries (OpenStreetMap)", ("refineries_osm.json",), 30,
            _fetch_refineries, "ODbL 1.0", "https://www.openstreetmap.org/copyright",
            "(c) OpenStreetMap contributors (ODbL)", "review", "industrial=refinery features."),
    Dataset("wikidata", "LNG terminals, large dams (>= 50 m), satellite ground stations (Wikidata)",
            ("infra_wikidata.json",), 30, _fetch_wikidata, "CC0 1.0", "https://query.wikidata.org/",
            "Wikidata (CC0)", "ok", "Coverage limited to items with coordinates."),
    Dataset("wpi", "World Port Index (NGA Pub 150)", ("wpi.csv",), 90, _fetch_wpi,
            "US Government work (public domain)", "https://msi.nga.mil/Publications/WPI",
            "NGA World Port Index", "ok", "Falls back to the RIMES CKAN mirror when NGA is down."),
    Dataset("opensanctions", "OpenSanctions maritime + sanctioned vessels / aircraft",
            ("os_maritime.csv", "os_targets_va.csv"), 2, _fetch_opensanctions,
            "CC BY-NC 4.0 (free for non-commercial use)", "https://www.opensanctions.org/licensing/",
            "Sanctions data: OpenSanctions.org (CC BY-NC 4.0)", "no",
            "Commercial use requires an OpenSanctions data licence."),
]}


# ------------------------------------------------------------------ refresh


def read_meta(directory: Path) -> dict:
    try:
        return json.loads((directory / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def age_days(directory: Path, ds: Dataset, now: float | None = None) -> float | None:
    """Days since the dataset was fetched (file mtime when the meta has no record)."""
    now = time.time() if now is None else now
    m = read_meta(directory).get(ds.id) or {}
    ts = m.get("fetched_at")
    if ts is None:
        paths = [directory / n for n in ds.files]
        if not all(p.is_file() for p in paths):
            return None
        ts = min(p.stat().st_mtime for p in paths)
    return max(0.0, (now - float(ts)) / 86400)


def stale(directory: Path, ds: Dataset) -> bool:
    a = age_days(directory, ds)
    return a is None or a >= ds.max_age_days


def refresh(directory: Path, ids: list[str] | None = None, *, force: bool = False,
            client: httpx.Client | None = None) -> dict[str, dict]:
    """Fetch the given (default: all) datasets that are stale; returns per-id results."""
    f = Fetcher(directory, client)
    results: dict[str, dict] = {}
    for did in ids or list(DATASETS):
        ds = DATASETS.get(did)
        if ds is None:
            results[did] = {"ok": False, "error": "unknown dataset"}
            continue
        if not force and not stale(directory, ds):
            results[did] = {"ok": True, "skipped": "fresh", "age_days": round(age_days(directory, ds), 1)}
            continue
        t0 = time.time()
        try:
            info = ds.fetch(f)
        except Exception as exc:  # noqa: BLE001 — one failing source never blocks the others
            log.warning("infra dataset %s failed: %s", did, exc)
            results[did] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            continue
        meta = read_meta(directory)
        meta[did] = {"fetched_at": time.time(), **info}
        (directory / META_FILE).write_text(json.dumps(meta, indent=1), encoding="utf-8")
        results[did] = {"ok": True, "seconds": round(time.time() - t0, 1), **info}
        log.info("infra dataset %s: %s", did, results[did])
    return results
