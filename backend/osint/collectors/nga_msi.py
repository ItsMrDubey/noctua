"""NGA Maritime Safety Information broadcast warnings (NAVAREA IV/XII, HYDROLANT/HYDROPAC/
HYDROARC), public domain. Stored as documents (kind='nav_warning') with the parsed geometry.

Endpoint: https://msi.nga.mil/api/publications/broadcast-warn?output=json&status=A
-> {"broadcast-warn": [{msgYear, msgNumber, navArea, subregion, text, status, issueDate,
authority, cancelDate, ...}]}.

Active warnings stay in force for weeks, but ingest drops documents published more than 48 h
before they are seen, so a warning first seen late gets ``published_at`` clamped to 47 h ago
(the real issue time is kept in attrs.issued_at). Each warning is emitted once and re-emitted
after ``reemit_days`` while still active; the cursor carries the active set, which the naval
module uses to ignore cancelled warnings.
"""

import logging
from datetime import UTC, datetime, timedelta

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Document, clip_snippet, document_id
from osint.naval import msi

log = logging.getLogger(__name__)

API_URL = "https://msi.nga.mil/api/publications/broadcast-warn"
AREA_NAMES = {"4": "NAVAREA IV", "12": "NAVAREA XII", "A": "HYDROARC", "P": "HYDROPAC",
              "C": "HYDROARC", "L": "HYDROLANT"}
TEXT_MAX = 4000
CLAMP = timedelta(hours=47)


def warning_key(w: dict) -> str:
    return f"{w.get('navArea')}-{w.get('msgNumber')}-{w.get('msgYear')}"


def parse_warning(w: dict, now: datetime, strong: list[str], medium: list[str]) -> Document:
    """One API record -> Document. Raises KeyError/ValueError/TypeError when malformed."""
    area = str(w["navArea"]).strip()
    number = int(w["msgNumber"])
    year = int(w["msgYear"])
    text = " ".join(str(w.get("text") or "").split())
    if not text:
        raise ValueError("empty warning text")
    issued = msi.parse_dtg(str(w.get("issueDate") or "")) or now
    published = max(issued, now - CLAMP)
    published = min(published, now)
    geoms = msi.parse_geometries(text)
    s, m = msi.keyword_flags(text, strong, medium)
    area_name = AREA_NAMES.get(area.upper(), f"NAVAREA {area}")
    key = f"{area}-{number}-{year}"
    title = f"{area_name} {number}/{year % 100:02d}: " + text[:90]
    url = f"{API_URL}?navArea={area}&msgNumber={number}&msgYear={year}&output=json"
    return Document(
        id=document_id(url, origin_id=key), source="nga_msi", url=url, title=title,
        snippet=clip_snippet(text), language="en", published_at=published, fetched_at=now,
        kind="nav_warning", origin_id=key,
        attrs={
            "nav_area": area, "nav_area_name": area_name, "msg_number": number,
            "msg_year": year, "subregion": w.get("subregion"), "authority": w.get("authority"),
            "status": w.get("status"), "issued_at": issued.isoformat(),
            "published_clamped": published != issued,
            "cancel_at": (c.isoformat() if (c := msi.cancel_time(text)) else None),
            "text": text[:TEXT_MAX], "geometries": geoms, "keywords": s + m,
            "licence": "NGA MSI, US Government public domain",
        },
    )


@register
class NgaMsiCollector(PollCollector):
    name = "nga_msi"
    domain = "maritime"
    licence = "NGA MSI broadcast warnings - US Government public domain"
    commercial_ok = True
    default_interval_s = 1800.0
    poll_timeout_s = 90.0

    async def poll(self, ctx: CollectorContext) -> Batch:
        now = utcnow()
        params = {"output": "json", "status": self.cfg.get("status", "A")}
        r = await ctx.http.get(self.cfg.get("url", API_URL), params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        records = data.get("broadcast-warn") if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise TypeError("unexpected NGA MSI payload (no broadcast-warn list)")
        from osint.naval.config import NavalConfig

        nc = NavalConfig.load(ctx.settings.config_dir / "naval.yaml").sec("nav_warnings")
        strong, medium = nc.get("strong_keywords") or [], nc.get("medium_keywords") or []
        cur = ctx.get_cursor(self.name) or {}
        emitted: dict[str, str] = dict(cur.get("emitted") or {})
        reemit = timedelta(days=float(self.cfg.get("reemit_days", 30)))
        batch = Batch(source=self.name)
        active: list[str] = []
        skipped = 0
        for w in records:
            try:
                if str(w.get("status") or "A").upper() != "A":
                    continue
                if w.get("msgNumber") is None or w.get("msgYear") is None:
                    raise KeyError("msgNumber/msgYear")
                key = warning_key(w)
                active.append(key)
                last = emitted.get(key)
                if last and now - datetime.fromisoformat(last) < reemit:
                    continue
                batch.documents.append(parse_warning(w, now, strong, medium))
                emitted[key] = now.astimezone(UTC).isoformat()
            except (KeyError, ValueError, TypeError, AttributeError):
                skipped += 1
        if skipped:
            log.warning("nga_msi: skipped %d malformed warning(s)", skipped)
        alive = set(active)
        ctx.set_cursor(self.name, {
            "last_poll": now.isoformat(), "active": sorted(alive), "count": len(alive),
            "emitted": {k: v for k, v in emitted.items() if k in alive},
        })
        return batch
