"""UCDP Georeferenced Event Dataset (GED) backfill -> historical armed-conflict events.

Licence: CC BY 4.0 (attribution: "UCDP Georeferenced Event Dataset (GED), Uppsala
Conflict Data Program"). The UCDP API needs an access token (HTTP 401 without one), so
the public CSV downloads are used: the yearly global GED zip plus the monthly candidate
CSVs, discovered on https://ucdp.uu.se/downloads/ (or pinned in config/backfill.yaml).

Each GED row becomes one event through the event store's public ``save_events`` (bulk,
idempotent upsert keyed by ``ucdp-<id>``): category ``armed_conflict``, subcategory from
``type_of_violence``, precision/confidence from ``where_prec``/``date_prec``, fatalities
(best/low/high and per side) as provenance fields, one ``event_documents`` row with
source ``ucdp``. Status is ``historical`` — a curated dataset is not one of the live
corroboration levels (reported .. official), so it gets its own status-agnostic value.
``updated_at``/``first_seen`` are the event's end date so history does not flood the
"recently updated" live view.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import h3

from osint.backfill.common import BackfillContext, FetchError, Summary, Window, get_with_retry
from osint.core.countries import to_iso2
from osint.core.geo import h3_cells, utcnow, valid_coord
from osint.core.models import clip_snippet

log = logging.getLogger("osint.backfill.ucdp")

DOWNLOADS_URL = "https://ucdp.uu.se/downloads/"
SOURCE = "ucdp"
STATUS = "historical"
CATEGORY = "armed_conflict"
LICENCE = "CC BY 4.0 - UCDP Georeferenced Event Dataset (GED), Uppsala Conflict Data Program"
VIOLENCE = {1: "state-based", 2: "non-state", 3: "one-sided"}
# where_prec (GED codebook): 1 exact location, 2 within ~25 km, 3 second-order admin
# division, 4 first-order admin division, 5 linear feature / between places, 6 country,
# 7 international waters or airspace
WHERE_PREC = {1: ("point", 1.0), 2: ("city", 0.85), 3: ("admin1", 0.65), 4: ("admin1", 0.5),
              5: ("admin1", 0.4), 6: ("country", 0.2), 7: ("country", 0.1)}
# date_prec: 1 exact day, 2 within 2-6 days, 3 within the week, 4 within the month,
# 5 longer than a month
DATE_PREC = {1: 1.0, 2: 0.85, 3: 0.7, 4: 0.5, 5: 0.3}
MAX_ZIP_BYTES = 500 * 1024 * 1024
MAX_CSV_BYTES = 2 * 1024 * 1024 * 1024
# events per commit: each carries ~20 field rows, so ~5k rows per transaction (a huge
# transaction pins the WAL and blocks the live server's writers)
BATCH = 250

_GED_ZIP = re.compile(r'href="([^"]*/ged/ged(\d+)-csv\.zip)"', re.IGNORECASE)
_CANDIDATE = re.compile(r'href="([^"]*/candidateged/GEDEvent_v[\d_]+\.csv)"', re.IGNORECASE)


def discover(html: str) -> list[str]:
    """Newest global GED CSV zip + the candidate (monthly) CSVs listed on the page."""
    out: list[str] = []
    geds = sorted(((int(v), u) for u, v in _GED_ZIP.findall(html)), reverse=True)
    if geds:
        out.append(geds[0][1])
    for u in _CANDIDATE.findall(html):
        if u not in out:
            out.append(u)
    return out


def candidate_urls(html: str) -> list[str]:
    """The candidate (monthly) CSV URLs listed on the downloads page, in page order."""
    return list(dict.fromkeys(_CANDIDATE.findall(html)))


def _int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: str) -> datetime:
    text = (v or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"bad UCDP date {v!r}")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def precision(where_prec: int | None) -> tuple[str, float]:
    return WHERE_PREC.get(where_prec or 0, ("country", 0.1))


def confidence(where_prec: int | None, date_prec: int | None) -> float:
    geo = precision(where_prec)[1]
    when = DATE_PREC.get(date_prec or 0, 0.3)
    return round(0.6 * geo + 0.4 * when, 4)


def map_row(row: dict[str, str], dataset: str, now: datetime) -> dict[str, Any] | None:
    """One GED CSV row -> {"event", "document", "fields"} for ``save_events``; None if
    the row has no id, valid coordinates or dates."""
    uid = (row.get("id") or "").strip()
    lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
    if not uid or not valid_coord(lat, lon):
        return None
    t_start, t_end = _ts(row.get("date_start", "")), _ts(row.get("date_end", ""))
    if t_end < t_start:
        t_start, t_end = t_end, t_start
    where_prec, date_prec = _int(row.get("where_prec")), _int(row.get("date_prec"))
    prec, geo_conf = precision(where_prec)
    tov = _int(row.get("type_of_violence"))
    sub = VIOLENCE.get(tov or 0, "unknown")
    best, low, high = (_int(row.get(k), 0) for k in ("best", "low", "high"))
    side_a, side_b = (row.get("side_a") or "").strip(), (row.get("side_b") or "").strip()
    place = (row.get("where_coordinates") or "").strip() or (row.get("adm_1") or "").strip()
    country = (row.get("country") or "").strip()
    dyad = (row.get("dyad_name") or "").strip() or f"{side_a} - {side_b}"
    if place and country and country.casefold() not in place.casefold():
        where = f"{place}, {country}"
    else:
        where = place or country
    title = clip_snippet(f"{dyad} ({sub} violence): {best} killed"
                         + (f" in {where}" if where else ""))
    headline = clip_snippet((row.get("source_headline") or "").replace(";", "; "))
    summary = clip_snippet(" ".join(p for p in (
        (row.get("where_description") or "").strip(), headline) if p))
    c3, c5, c7 = h3_cells(lat, lon)
    offices = {o.strip() for o in (row.get("source_office") or "").split(";") if o.strip()}
    n_sources = _int(row.get("number_of_sources"), 0) or 0
    event_id = f"ucdp-{uid}"
    doc_id = f"ucdp:{uid}"
    event = {
        "id": event_id, "category": CATEGORY, "subcategory": sub, "cameo_root": None,
        "title": title, "summary": summary,
        "actors": [a for a in ({"name": side_a, "role": "side_a", "type": "", "country": None,
                                "source": SOURCE},
                               {"name": side_b, "role": "side_b", "type": "", "country": None,
                                "source": SOURCE})
                   if a["name"]],
        "lat": lat, "lon": lon, "precision": prec, "place_name": place or country or None,
        "country": to_iso2(country, "name") if country else None, "geo_confidence": geo_conf,
        "h3_3": h3.int_to_str(c3), "h3_5": h3.int_to_str(c5), "h3_7": h3.int_to_str(c7),
        "t_start": _iso(t_start), "t_end": _iso(t_end), "status": STATUS,
        "origin": "historical",
        "confidence": confidence(where_prec, date_prec), "version": 1,
        "mention_count": max(1, n_sources), "source_diversity": max(1, len(offices)),
        "gdelt_ids": [], "first_seen": _iso(t_end), "updated_at": _iso(t_end),
        "cluster_state": {"source": SOURCE, "dataset": dataset, "ucdp_id": uid,
                          "imported_at": _iso(now)},
    }
    document = {
        "event_id": event_id, "doc_id": doc_id, "source": SOURCE,
        "url": f"https://ucdp.uu.se/downloads/#{dataset}", "title": title,
        "published_at": _iso(t_end), "language": "en", "role": "primary",
        "added_at": _iso(now)}
    quote = headline or title
    values: dict[str, object] = {
        "fatalities_best": best, "fatalities_low": low, "fatalities_high": high,
        "deaths_a": _int(row.get("deaths_a"), 0), "deaths_b": _int(row.get("deaths_b"), 0),
        "deaths_civilians": _int(row.get("deaths_civilians"), 0),
        "deaths_unknown": _int(row.get("deaths_unknown"), 0),
        "type_of_violence": sub, "conflict_name": (row.get("conflict_name") or "").strip(),
        "dyad_name": dyad, "where_prec": where_prec, "date_prec": date_prec,
        "event_clarity": _int(row.get("event_clarity")), "ucdp_id": uid,
        "ucdp_relid": (row.get("relid") or "").strip(), "dataset": dataset,
        "source": SOURCE, "licence": LICENCE, "category": CATEGORY,
        "time": _iso(t_start), "location": place or country,
    }
    fields = [{"event_id": event_id, "field": k, "value": str(v), "doc_id": doc_id,
               "quote": quote} for k, v in values.items() if v not in (None, "")]
    return {"event": event, "document": document, "fields": fields, "t_start": t_start}


def iter_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """Rows of a GED CSV, or of the first CSV inside a GED zip (streamed)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            members = [i for i in z.infolist() if i.filename.lower().endswith(".csv")]
            if not members:
                raise ValueError("no CSV in UCDP zip")
            if members[0].file_size > MAX_CSV_BYTES:
                raise ValueError("UCDP CSV too large")
            with z.open(members[0]) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig",
                                                           newline=""))
    else:
        with path.open(encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)


def dataset_name(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    return re.sub(r"(-csv)?\.(zip|csv)$", "", name, flags=re.IGNORECASE)


async def download(ctx: BackfillContext, url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    size = 0
    try:
        async with ctx.http.stream("GET", url, timeout=300) as r:
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    size += len(chunk)
                    if size > MAX_ZIP_BYTES:
                        raise FetchError("download too large")
                    f.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return size


def import_file(ctx: BackfillContext, path: Path, dataset: str, window: Window,
                summary: Summary) -> tuple[int, int, int]:
    """Map and upsert rows in [window) by date_start; returns (events, out of window,
    malformed). Sync (run in a thread)."""
    now = utcnow()
    events: list[dict] = []
    docs: list[dict] = []
    fields: list[dict] = []
    n = outside = bad = 0

    def flush() -> None:
        if events and not ctx.dry_run:
            ctx.store.save_events(events, docs, fields)
            ctx.checkpoint.add_rows("events", len(events))
            ctx.checkpoint.heartbeat(progress={"file": dataset, "events": n})
        events.clear()
        docs.clear()
        fields.clear()

    for row in iter_csv_rows(path):
        try:
            m = map_row(row, dataset, now)
        except (ValueError, TypeError, KeyError):
            bad += 1
            continue
        if m is None:
            bad += 1
            continue
        if not (window.start <= m["t_start"] < window.end):
            outside += 1
            continue
        events.append(m["event"])
        docs.append(m["document"])
        fields.extend(m["fields"])
        n += 1
        if len(events) >= BATCH:
            flush()
            if n % 25_000 < BATCH:
                log.info("ucdp %s: %d events so far", dataset, n)
    flush()
    summary.add("events", n)
    return n, outside, bad


async def run(ctx: BackfillContext, window: Window) -> Summary:
    summary = Summary(source=SOURCE)
    cp = ctx.checkpoint
    urls = list(ctx.opt("urls", []) or [])
    if not urls:
        try:
            r = await get_with_retry(ctx.http, str(ctx.opt("downloads_url", DOWNLOADS_URL)),
                                     allow_status=())
            assert r is not None
            urls = discover(r.text)
        except FetchError as e:
            summary.state = "failed"
            summary.message = f"could not read the UCDP downloads page: {e}"
            return summary
        if not ctx.opt("include_candidate", True):
            urls = urls[:1]
    if not urls:
        summary.state = "failed"
        summary.message = "no GED CSV download found on the UCDP downloads page"
        return summary
    summary.units_total = len(urls)
    span = f"{window.start:%Y-%m-%d}..{window.end:%Y-%m-%d}"
    for url in urls:
        dataset = dataset_name(url)
        key = f"{dataset}|{span}"
        if cp.is_done(key):
            summary.units_skipped += 1
            continue
        t0 = time.perf_counter()
        dest = ctx.tmp_dir / "ucdp" / url.rsplit("/", 1)[-1]
        try:
            size = await download(ctx, url, dest)
            log.info("ucdp %s: downloaded %.1f MB", dataset, size / 1e6)
            n, outside, bad = await asyncio.to_thread(import_file, ctx, dest, dataset,
                                                      window, summary)
        except Exception as e:  # noqa: BLE001 — next file continues
            msg = f"{dataset}: {type(e).__name__}: {e}"
            summary.errors.append(msg)
            cp.error(msg)
            log.error("ucdp %s", msg)
            continue
        finally:
            dest.unlink(missing_ok=True)
        log.info("ucdp %s: %d events imported, %d outside the window, %d malformed, %.1fs",
                 dataset, n, outside, bad, time.perf_counter() - t0)
        cp.mark_done(key, events=n)
        summary.units_done += 1
    summary.extra["attribution"] = LICENCE
    return summary


def default_since() -> date:
    return date(1989, 1, 1)
