"""Static reference points for the fire scorer, cached under ``data/reference/``.

- Volcanoes: Smithsonian GVP "Volcanoes of the World" Holocene list (WFS GeoJSON) ->
  ``gvp_volcanoes.json`` [{name, lat, lon, last_eruption, country}].
- Gas flares: Colorado School of Mines EOG VIIRS Nightfire annual flare list (xlsx, no
  login) -> ``eog_flares.json`` [[lat, lon, type], ...]. Parsed with the standard library
  (an xlsx is a zip of XML sheets).
- Populated places: GeoNames cities15000 from the gazetteer download
  (``data/gazetteer/cities15000.zip``), read in place.
- Facilities: the repo's reference POIs (airbases, ports, military bases, nuclear sites).

Downloads happen in the worker (FireScoreJob) and refresh every ``refresh_days``; a failed
download keeps the previous cache. Missing data just disables that signal.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

VOLCANO_FILE = "gvp_volcanoes.json"
FLARE_FILE = "eog_flares.json"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_COL = re.compile(r"[A-Z]+")


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _get(http: httpx.AsyncClient, url: str, what: str) -> bytes:
    try:
        r = await http.get(url, timeout=httpx.Timeout(180, connect=20))
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"{what}: HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"{what}: {type(e).__name__}") from None
    if len(r.content) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError(f"{what}: response larger than {MAX_DOWNLOAD_BYTES} bytes")
    return r.content


# ------------------------------------------------------------------ volcanoes


def parse_volcanoes(data: dict) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or []:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = _float(coords[0]), _float(coords[1])
        if lat is None or lon is None:
            continue
        last = p.get("Last_Eruption_Year")
        out.append({"name": p.get("Volcano_Name") or "", "lat": round(lat, 4),
                    "lon": round(lon, 4), "country": p.get("Country") or "",
                    "last_eruption": int(last) if isinstance(last, int | float) else None})
    return out


async def fetch_volcanoes(http: httpx.AsyncClient, url: str, ref_dir: Path) -> int:
    raw = await _get(http, url, "GVP volcanoes")
    try:
        items = parse_volcanoes(json.loads(raw))
    except ValueError:
        raise RuntimeError("GVP volcanoes: invalid JSON") from None
    if not items:
        raise RuntimeError("GVP volcanoes: no features")
    _write_json(ref_dir / VOLCANO_FILE, items)
    return len(items)


def load_volcanoes(ref_dir: Path, since_year: int | None = None) -> list[dict]:
    items = _read_json(ref_dir / VOLCANO_FILE)
    if not isinstance(items, list):
        return []
    if since_year is None:
        return items
    return [v for v in items if v.get("last_eruption") is not None
            and v["last_eruption"] >= since_year]


# --------------------------------------------------------------------- flares


def _cells(row: ET.Element, shared: list[str]) -> dict[str, str]:
    out = {}
    for c in row.iter(f"{_NS}c"):
        m = _COL.match(c.get("r") or "")
        if not m:
            continue
        t = c.get("t")
        if t == "inlineStr":
            val = "".join(x.text or "" for x in c.iter(f"{_NS}t"))
        else:
            v = c.find(f"{_NS}v")
            val = "" if v is None or v.text is None else v.text
            if t == "s" and val.isdigit() and int(val) < len(shared):
                val = shared[int(val)]
        out[m.group(0)] = val
    return out


def parse_flare_xlsx(content: bytes) -> list[list]:
    """[[lat, lon, type]] from every sheet whose header has Latitude and Longitude
    columns (EOG: "flare upstream", "flare oil downstream", "flare gas downstream")."""
    out: list[list] = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
                      for si in root.iter(f"{_NS}si")]
        sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                        key=lambda n: int(re.sub(r"\D", "", n)))
        for name in sheets:
            cols: dict[str, str] | None = None
            with z.open(name) as fh:
                for _, el in ET.iterparse(fh):
                    if el.tag != f"{_NS}row":
                        continue
                    cells = _cells(el, shared)
                    el.clear()
                    if cols is None:
                        head = {v.strip().casefold(): k for k, v in cells.items()}
                        if "latitude" not in head or "longitude" not in head:
                            break  # a country summary sheet
                        cols = {"lat": head["latitude"], "lon": head["longitude"],
                                "type": head.get("type", "")}
                        continue
                    lat, lon = _float(cells.get(cols["lat"])), _float(cells.get(cols["lon"]))
                    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        continue
                    out.append([round(lat, 5), round(lon, 5), cells.get(cols["type"], "")])
    return out


async def fetch_flares(http: httpx.AsyncClient, url: str, ref_dir: Path) -> int:
    raw = await _get(http, url, "EOG flares")
    try:
        items = parse_flare_xlsx(raw)
    except (zipfile.BadZipFile, ET.ParseError, KeyError):
        raise RuntimeError("EOG flares: not an xlsx workbook") from None
    if not items:
        raise RuntimeError("EOG flares: no flare rows")
    _write_json(ref_dir / FLARE_FILE, items)
    return len(items)


def load_flares(ref_dir: Path) -> list[list]:
    items = _read_json(ref_dir / FLARE_FILE)
    return items if isinstance(items, list) else []


# --------------------------------------------------------------------- places


def load_cities(gazetteer_dir: Path, min_pop: int = 15000) -> list[tuple[float, float, str, int]]:
    """(lat, lon, name, population) from GeoNames cities15000.zip ([] when missing)."""
    path = gazetteer_dir / "cities15000.zip"
    if not path.is_file():
        log.warning("fire score: %s missing; urban signal off", path)
        return []
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            text = z.read("cities15000.txt").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        log.warning("fire score: %s unreadable; urban signal off", path)
        return []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 15:
            continue
        lat, lon = _float(p[4]), _float(p[5])
        try:
            pop = int(p[14] or 0)
        except ValueError:
            pop = 0
        if lat is None or lon is None or pop < min_pop:
            continue
        out.append((lat, lon, p[1], pop))
    return out


def urban_radius_km(pop: int) -> float:
    """Rough built-up radius of a place of ``pop`` inhabitants (2 km at 15k, ~9 km at 1M,
    capped at 25 km)."""
    return min(25.0, max(2.0, 0.009 * pop ** 0.5))


INFRA_GROUPS = {"airbase", "naval_base", "port", "refinery", "military", "nuclear", "nuclear_power"}


def load_pois() -> list[tuple[float, float, str, str]]:
    """(lat, lon, name, group) of facilities from the repo's reference POIs."""
    from osint.enrich.impact import ReferencePOIs

    return [(p["lat"], p["lon"], p.get("name") or "", p.get("group") or "")
            for p in ReferencePOIs().items
            if p.get("lat") is not None and p.get("group") in INFRA_GROUPS]
