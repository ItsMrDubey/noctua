"""GDELT 2.0 event exports (every 15 minutes), as coded-event Documents.

Each poll reads ``lastupdate.txt`` and downloads the exports from the listed one back in
15-minute slots to the last processed one (the cursor is its filename; at most
``max_lookback`` slots). lastupdate.txt can list exports up to ~30 min before they are
published (HTTP 404); those are skipped and picked up on a later poll. Exports parse the
61-column, tab-separated event rows in a worker thread. Rows enter the pipeline as candidates;
clustering later accepts or discards them (spec §5.3).
"""

import asyncio
import hashlib
import io
import logging
import zipfile
from datetime import UTC, datetime, timedelta

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Document, clip_snippet, document_id

log = logging.getLogger(__name__)

LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
MAX_ZIP_BYTES = 50 * 1024 * 1024  # an export is ~0.1-1 MB zipped
MAX_CSV_BYTES = 200 * 1024 * 1024  # zip-bomb guard on the uncompressed size
SLOT = timedelta(minutes=15)
SLOT_FORMAT = "%Y%m%d%H%M%S"
EXPORT_SUFFIX = ".export.CSV.zip"
MAX_LOOKBACK = 8  # slots (2 h) tried back from the listed export

# GDELT 2.0 event table column indices (0-based), from the GDELT 2.0 Event Codebook
# (http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf) and checked
# against a live export (61 columns).
N_COLUMNS = 61
COL = {
    "GlobalEventID": 0,
    "SQLDATE": 1,
    "Actor1Code": 5,
    "Actor1Name": 6,
    "Actor1CountryCode": 7,
    "Actor2Code": 15,
    "Actor2Name": 16,
    "Actor2CountryCode": 17,
    "IsRootEvent": 25,
    "EventCode": 26,
    "EventBaseCode": 27,
    "EventRootCode": 28,
    "QuadClass": 29,
    "GoldsteinScale": 30,
    "NumMentions": 31,
    "NumSources": 32,
    "NumArticles": 33,
    "AvgTone": 34,
    "ActionGeo_Type": 51,
    "ActionGeo_FullName": 52,
    "ActionGeo_CountryCode": 53,  # FIPS 10-4, not ISO
    "ActionGeo_ADM1Code": 54,
    "ActionGeo_Lat": 56,
    "ActionGeo_Long": 57,
    "ActionGeo_FeatureID": 58,
    "DATEADDED": 59,
    "SOURCEURL": 60,
}

CAMEO_ROOTS = {
    "01": "Make public statement", "02": "Appeal", "03": "Express intent to cooperate",
    "04": "Consult", "05": "Engage in diplomatic cooperation",
    "06": "Engage in material cooperation", "07": "Provide aid", "08": "Yield",
    "09": "Investigate", "10": "Demand", "11": "Disapprove", "12": "Reject",
    "13": "Threaten", "14": "Protest", "15": "Exhibit force posture",
    "16": "Reduce relations", "17": "Coerce", "18": "Assault", "19": "Fight",
    "20": "Use unconventional mass violence",
}
GEO_TYPES = {1: "country", 2: "us_state", 3: "us_city", 4: "world_city", 5: "world_state"}


def _float(v: str) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _int(v: str) -> int | None:
    try:
        return int(v) if v else None
    except ValueError:
        return None


def latest_export_url(text: str) -> str | None:
    """The ``*.export.CSV.zip`` URL from lastupdate.txt ("size md5 url" lines)."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].endswith(".export.CSV.zip"):
            return parts[2]
    return None


def export_slots(url: str, last_file: str | None, max_lookback: int) -> list[str]:
    """Export URLs from ``url`` back in 15-minute slots (newest first), stopping before
    ``last_file``; just ``[url]`` if its name is not a slot timestamp."""
    base, name = url.rsplit("/", 1)
    try:
        newest = datetime.strptime(name.removesuffix(EXPORT_SUFFIX), SLOT_FORMAT)
    except ValueError:
        return [url]
    out = []
    for i in range(max(1, max_lookback)):
        name = (newest - i * SLOT).strftime(SLOT_FORMAT) + EXPORT_SUFFIX
        if last_file is not None and name <= last_file:
            break
        out.append(f"{base}/{name}")
    return out


def _md5(text: str, url: str) -> str | None:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == url:
            return parts[1].lower()
    return None


def parse_row(cols: list[str], fetched_at: datetime) -> Document | None:
    """One export row -> Document; None if the row lacks an id, url or date."""
    if len(cols) != N_COLUMNS:
        raise ValueError(f"expected {N_COLUMNS} columns, got {len(cols)}")
    c = {name: cols[i].strip() for name, i in COL.items()}
    url = c["SOURCEURL"]
    if not c["GlobalEventID"] or not url.startswith(("http://", "https://")):
        return None
    added = datetime.strptime(c["DATEADDED"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    root = c["EventRootCode"].zfill(2) if c["EventRootCode"] else ""
    action = CAMEO_ROOTS.get(root, "Event")
    a1, a2 = c["Actor1Name"].title(), c["Actor2Name"].title()
    title = " ".join(p for p in (a1 or "Unknown actor", f"— {action}", f"— {a2}" if a2 else "")
                     if p)
    title += f" ({c['EventCode']})" if c["EventCode"] else ""
    if c["ActionGeo_FullName"]:
        title += f" in {c['ActionGeo_FullName']}"
    geo_type = _int(c["ActionGeo_Type"])
    attrs: dict[str, object] = {
        "event_code": c["EventCode"], "event_base_code": c["EventBaseCode"],
        "event_root_code": root, "quad_class": _int(c["QuadClass"]),
        "goldstein": _float(c["GoldsteinScale"]), "num_mentions": _int(c["NumMentions"]),
        "num_sources": _int(c["NumSources"]), "num_articles": _int(c["NumArticles"]),
        "avg_tone": _float(c["AvgTone"]), "is_root_event": c["IsRootEvent"] == "1",
        "sqldate": c["SQLDATE"],
        "actor1": c["Actor1Name"], "actor1_code": c["Actor1Code"],
        "actor1_country": c["Actor1CountryCode"],
        "actor2": c["Actor2Name"], "actor2_code": c["Actor2Code"],
        "actor2_country": c["Actor2CountryCode"],
        "geo_type": geo_type, "geo_type_name": GEO_TYPES.get(geo_type or 0, ""),
        "geo_name": c["ActionGeo_FullName"], "geo_country": c["ActionGeo_CountryCode"],
        "geo_adm1": c["ActionGeo_ADM1Code"], "geo_feature_id": c["ActionGeo_FeatureID"],
        "geo_lat": _float(c["ActionGeo_Lat"]), "geo_lon": _float(c["ActionGeo_Long"]),
        "attribution": "GDELT Project",
    }
    return Document(
        id=document_id(url, c["GlobalEventID"]), source="gdelt", url=url,
        title=clip_snippet(title), snippet=clip_snippet(c["ActionGeo_FullName"]),
        language="", published_at=added, fetched_at=fetched_at, kind="gdelt_event",
        origin_id=c["GlobalEventID"], attrs=attrs,
    )


def parse_export(data: bytes, fetched_at: datetime) -> list[Document]:
    """Unzip an export in memory and parse it (sync; run in a thread)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [i for i in z.infolist() if i.filename.lower().endswith(".csv")]
        if not members:
            raise ValueError("no CSV in GDELT export zip")
        info = members[0]
        if info.file_size > MAX_CSV_BYTES:
            raise ValueError(f"GDELT export too large ({info.file_size} bytes)")
        text = z.read(info).decode("utf-8", errors="replace")
    out: list[Document] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            doc = parse_row(line.split("\t"), fetched_at)
        except (ValueError, TypeError, KeyError):
            skipped += 1
            continue
        if doc is not None:
            out.append(doc)
        else:
            skipped += 1
    if skipped:
        log.warning("gdelt: skipped %d malformed row(s)", skipped)
    return out


@register
class GdeltCollector(PollCollector):
    name = "gdelt"
    domain = "events"
    licence = "GDELT open data (attribution required)"
    commercial_ok = True
    default_interval_s = 900.0

    async def poll(self, ctx: CollectorContext) -> Batch:
        batch = Batch(source=self.name)
        try:
            r = await ctx.http.get(self.cfg.get("lastupdate_url", LASTUPDATE_URL), timeout=30)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"gdelt: HTTP {e.response.status_code}") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"gdelt: {type(e).__name__}") from None
        listing = r.text
        listed = latest_export_url(listing)
        if listed is None:
            raise RuntimeError("gdelt: no export listed in lastupdate.txt")
        cursor = ctx.get_cursor(self.name) or {}
        last_file = cursor.get("last_file")
        newest: str | None = None
        unpublished = 0
        for url in export_slots(listed, last_file,
                                int(self.cfg.get("max_lookback", MAX_LOOKBACK))):
            filename = url.rsplit("/", 1)[-1]
            url = url.replace("http://", "https://", 1)  # the http URL only redirects
            try:
                z = await ctx.http.get(url, timeout=120)
                if z.status_code == 404:  # listed ahead of publication, or a gap
                    unpublished += 1
                    continue
                z.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"gdelt: HTTP {e.response.status_code}") from None
            except httpx.HTTPError as e:
                raise RuntimeError(f"gdelt: {type(e).__name__}") from None
            data = z.content
            if len(data) > MAX_ZIP_BYTES:
                raise RuntimeError(f"gdelt: export too large ({len(data)} bytes)")
            expected = _md5(listing, url.replace("https://", "http://", 1)) or _md5(listing, url)
            if expected and hashlib.md5(data).hexdigest() != expected:
                raise RuntimeError(f"gdelt: checksum mismatch for {filename}")
            batch.documents.extend(await asyncio.to_thread(parse_export, data, utcnow()))
            newest = newest or filename
            if last_file is None:
                break  # first run: the newest published export only
        if newest is None:
            if last_file is None:
                raise RuntimeError(f"gdelt: none of the last {unpublished} exports published")
            log.debug("gdelt: %d listed export(s) not published yet", unpublished)
            return batch
        if unpublished:
            log.debug("gdelt: skipped %d unpublished export(s)", unpublished)
        ctx.set_cursor(self.name, {"last_file": newest, "rows": len(batch.documents)})
        return batch
