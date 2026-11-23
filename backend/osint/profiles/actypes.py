"""ICAO aircraft type designators (ICAO Doc 8643): designator -> manufacturer / model,
aircraft class, engine count and type, wake turbulence category.

ICAO's own Doc 8643 web service now redirects to a login, so the open copy maintained for
tar1090 / readsb (wiedehopf/tar1090-db, from the Mictronics database) is used:
``icao_aircraft_types2.js`` is gzip-compressed JSON ``{"B738": ["BOEING 737-800", "L2J",
"M"], ...}``. Fetched into ``data/reference/aircraft_types.json`` by ``osint infra-fetch
aircraft_types`` (small, ~35 kB; refreshed every 90 days).
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

FILE = "aircraft_types.json"
URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/icao_aircraft_types2.js"
SOURCE_URL = "https://github.com/wiedehopf/tar1090-db"
CLASS = {"L": "Landplane", "S": "Seaplane", "A": "Amphibian", "H": "Helicopter",
         "G": "Gyrocopter", "T": "Tilt-rotor"}
ENGINE = {"J": "jet", "T": "turboprop / turboshaft", "P": "piston", "E": "electric",
          "R": "rocket"}
WTC = {"L": "Light", "M": "Medium", "H": "Heavy", "J": "Super", "L/M": "Light / medium"}


def parse(raw: bytes) -> dict[str, list[str]]:
    """The published file (gzip or plain JSON) -> {designator: [name, desc, wtc]}."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    out = {}
    for k, v in data.items():
        if isinstance(v, list) and v:
            out[str(k).upper()] = [str(x or "") for x in (v + ["", "", ""])[:3]]
    return out


def describe(designator: str | None, table: dict[str, list[str]]) -> dict | None:
    """{designator, name, class, engines, engine_type, wtc} or None if unknown."""
    if not designator:
        return None
    row = table.get(designator.strip().upper())
    if row is None:
        return None
    name, desc, wtc = row
    out = {"designator": designator.strip().upper(), "name": name, "desc": desc,
           "wtc": WTC.get(wtc, wtc) or None}
    if len(desc) == 3:
        out["class"] = CLASS.get(desc[0])
        out["engines"] = int(desc[1]) if desc[1].isdigit() else None
        out["engine_type"] = ENGINE.get(desc[2])
    return out


def fetch(f) -> dict:
    """``Dataset.fetch`` for ``aircraft_types`` (``f``: osint.infra.datasets.Fetcher)."""
    table = parse(f.get(URL))
    if len(table) < 1000:
        raise RuntimeError(f"aircraft types: only {len(table)} designators")
    f.write_json(FILE, table)
    return {"count": len(table)}


class TypeTable:
    """Lazy loader for ``aircraft_types.json``; reloads when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sig = None
        self._table: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def table(self) -> dict[str, list[str]]:
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return {}
        with self._lock:
            if sig != self._sig:
                try:
                    self._table = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    log.warning("aircraft types unreadable: %s", exc)
                    self._table = {}
                self._sig = sig
            return self._table

    def describe(self, designator: str | None) -> dict | None:
        return describe(designator, self.table())
