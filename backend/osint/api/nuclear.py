"""Known nuclear sites reference layer: GET /api/reference/nuclear?bbox=&type=."""

import asyncio

from fastapi import FastAPI, HTTPException

from osint.api.layers import parse_bbox
from osint.reference.nuclear import GROUPS, TYPES, NuclearSites, get_nuclear

SOURCE = [
    {"id": "wikidata-nuclear", "name": "Wikidata nuclear facilities", "licence": "CC0 1.0",
     "url": "https://query.wikidata.org/", "attribution": "Wikidata"},
    {"id": "curated-nuclear", "name": "Curated nuclear sites (Wikipedia / IAEA / government sources)",
     "licence": "Project-owned", "url": None, "attribution": "OSINT curated list"},
]
_VALID = set(TYPES) | set(GROUPS.values())


def mount_nuclear(app: FastAPI, sites: NuclearSites | None = None) -> None:
    getter = (lambda: sites) if sites is not None else get_nuclear

    @app.get("/api/reference/nuclear")
    async def nuclear(bbox: str | None = None, type: str | None = None) -> dict:
        bb = None
        if bbox:
            bb = parse_bbox(bbox)
            if bb is None:
                raise HTTPException(422, "invalid bbox, expected w,s,e,n")
        types = {t for t in (type or "").split(",") if t} or None
        if types and not types <= _VALID:
            raise HTTPException(422, "unknown type; one of " + ", ".join(sorted(_VALID)))
        data = await asyncio.to_thread(getter)
        items = data.query(bb, types)
        return {"items": items, "count": len(items), "sources": SOURCE}
