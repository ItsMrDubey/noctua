"""adsb.lol daily global history (ODbL) -> military aircraft positions.

Releases live in ``adsblol/globe_history_<year>`` on GitHub, one per day and variant,
tagged ``v<YYYY.MM.DD>-planes-readsb-<variant>-<n>``; the asset is a tar split into
~2 GB parts (``.tar.aa``, ``.tar.ab``, ...), about 3-4 GB per day. For each day the parts
are downloaded to ``data/backfill/tmp`` (only if 2x their size is free on disk), read as
one tar stream, and deleted afterwards. Only readsb trace files
(``traces/xx/trace_full_<hex>.json``, gzip JSON) are opened, in memory; nothing is
extracted to disk or executed.

Kept aircraft: military by ``dbFlags & 1``, by the plane-alert military list, or by a
military-only ICAO type code. Points are thinned to one per ``thin_s`` (60 s) and written
as lake ``positions`` (source ``adsb_history:adsb.lol``); tracks are upserted.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import re
import shutil
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from osint.backfill.common import BackfillContext, FetchError, Summary, Window, get_with_retry
from osint.core.geo import valid_coord
from osint.core.models import Position, TrackInfo
from osint.enrich.mil_types import get_mil_category_from_type, is_high_priority
from osint.enrich.military import MilDb
from osint.store.lake import position_row

log = logging.getLogger("osint.backfill.adsb_history")

API = "https://api.github.com"
REPO = "adsblol/globe_history_{year}"
SOURCE = "adsb_history:adsb.lol"
MAX_TRACE_BYTES = 64 * 1024 * 1024
_TRACE_RE = re.compile(r"(?:^|/)traces/[0-9a-f]{2}/trace_full_(~?[0-9a-f]{6})\.json$", re.IGNORECASE)
FLUSH_ROWS = 200_000


# ------------------------------------------------------------------------- parsing


@dataclass
class TraceResult:
    track: TrackInfo
    positions: list[Position] = field(default_factory=list)
    last_ts: datetime | None = None


def military(header: dict[str, Any], mil_db: MilDb) -> tuple[bool, str, dict[str, str]]:
    """(is_mil, category, plane-alert entry) for a trace header."""
    hex_code = str(header.get("icao") or "").strip().lower()
    entry = mil_db.get(hex_code) or {}
    ac_type = str(header.get("t") or entry.get("icao_type") or "")
    type_is_mil, type_cat = get_mil_category_from_type(ac_type)
    try:
        flagged = bool(int(header.get("dbFlags") or 0) & 1)
    except (TypeError, ValueError):
        flagged = False
    return (flagged or bool(entry) or type_is_mil,
            entry.get("category") or type_cat or "", entry)


def _alt(v: Any) -> int | None:
    if v == "ground":
        return 0
    return int(v) if isinstance(v, int | float) else None


def parse_trace(data: dict[str, Any], mil_db: MilDb, *, thin_s: float = 60.0,
                source: str = SOURCE) -> TraceResult | None:
    """A readsb ``trace_full`` JSON -> track + thinned positions; None if not military."""
    hex_code = str(data.get("icao") or "").strip().lower()
    if not hex_code or hex_code.startswith("~"):
        return None
    is_mil, category, entry = military(data, mil_db)
    if not is_mil:
        return None
    base = float(data.get("timestamp") or 0)
    ac_type = str(data.get("t") or entry.get("icao_type") or "")
    track_id = f"air:{hex_code}"
    name: str | None = None
    out: list[Position] = []
    last_kept: float | None = None
    for p in data.get("trace") or []:
        if not isinstance(p, list) or len(p) < 3:
            continue
        details = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if details and details.get("flight"):
            name = str(details["flight"]).strip() or name
        try:
            t = base + float(p[0])
            lat, lon = float(p[1]), float(p[2])
        except (TypeError, ValueError):
            continue
        if not valid_coord(lat, lon):
            continue
        if last_kept is not None and t - last_kept < thin_s:
            continue
        last_kept = t
        gs = p[4] if len(p) > 4 and isinstance(p[4], int | float) else None
        trk = p[5] if len(p) > 5 and isinstance(p[5], int | float) else None
        out.append(Position(track_id=track_id, kind="air",
                            ts=datetime.fromtimestamp(t, UTC), lat=lat, lon=lon,
                            source=source, alt_ft=_alt(p[3]) if len(p) > 3 else None,
                            speed_kts=gs, heading_deg=trk))
    extra = {"desc": str(data.get("desc") or entry.get("type") or ""),
             "role": entry.get("tag1") or "", "history": "adsb.lol globe_history"}
    track = TrackInfo(
        track_id=track_id, kind="air", ident=hex_code, name=name,
        reg=str(data.get("r") or entry.get("reg") or "") or None, type_code=ac_type or None,
        operator=str(data.get("ownOp") or entry.get("operator") or "") or None,
        category=category or None, is_mil=True,
        high_priority=is_high_priority(ac_type), extra={k: v for k, v in extra.items() if v})
    return TraceResult(track=track, positions=out,
                       last_ts=out[-1].ts if out else None)


def decode_member(raw: bytes) -> dict[str, Any]:
    if raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as g:
            raw = g.read(MAX_TRACE_BYTES + 1)
        if len(raw) > MAX_TRACE_BYTES:
            raise ValueError("trace too large")
    return json.loads(raw)


class ConcatReader(io.RawIOBase):
    """Read several files back to back as one stream (split tar parts)."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = list(paths)
        self._f: BinaryIO | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while True:
            if self._f is None:
                if not self._paths:
                    return 0
                self._f = self._paths.pop(0).open("rb")
            n = self._f.readinto(b)
            if n:
                return n
            self._f.close()
            self._f = None

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
        super().close()


def iter_traces(stream: BinaryIO) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """(member name, decoded trace or None if undecodable) for every trace member of a
    tar stream; other members are skipped without reading."""
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for m in tar:
            if not m.isfile() or not _TRACE_RE.search(m.name):
                continue
            if m.size > MAX_TRACE_BYTES:
                yield m.name, None
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            try:
                yield m.name, decode_member(f.read())
            except (ValueError, OSError, EOFError):
                yield m.name, None


# ---------------------------------------------------------------------- releases


def release_tag_prefix(day: date, variant: str) -> str:
    return f"v{day:%Y.%m.%d}-planes-readsb-{variant}-"


def pick_release(releases: list[dict[str, Any]], day: date,
                 variant: str) -> dict[str, Any] | None:
    prefix = release_tag_prefix(day, variant)
    best, best_n = None, -1
    for r in releases:
        tag = str(r.get("tag_name") or "")
        if not tag.startswith(prefix):
            continue
        try:
            n = int(tag[len(prefix):])
        except ValueError:
            continue
        if n > best_n and r.get("assets"):
            best, best_n = r, n
    return best


def tar_assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    assets = [a for a in release.get("assets") or []
              if re.search(r"\.tar(\.[a-z]{2})?$", str(a.get("name") or ""))]
    return sorted(assets, key=lambda a: a["name"])


def history_dates(w: Window) -> list[date]:
    """Whole UTC days covered by the window: from the start date up to, not including,
    the end date (today's archive is published the next morning)."""
    out = []
    d = w.start.date()
    while d < w.end.date():
        out.append(d)
        d += timedelta(days=1)
    return out


async def list_releases(ctx: BackfillContext, year: int, pages: int = 5) -> list[dict]:
    repo = str(ctx.opt("repo", REPO)).format(year=year)
    out: list[dict] = []
    for page in range(1, pages + 1):
        r = await get_with_retry(ctx.http, f"{API}/repos/{repo}/releases",
                                 params={"per_page": 100, "page": page},
                                 allow_status=(404,), timeout=60)
        if r is None:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 100:
            break
    return out


async def download_asset(ctx: BackfillContext, url: str, dest: Path, size: int,
                         attempts: int = 5) -> None:
    """Stream to ``dest``, resuming with a Range request after a dropped connection."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        have = dest.stat().st_size if dest.exists() else 0
        if size and have >= size:
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            async with ctx.http.stream("GET", url, headers=headers,
                                       timeout=300, follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}")
                mode = "ab" if have and r.status_code == 206 else "wb"
                with dest.open(mode) as f:
                    async for chunk in r.aiter_bytes(4 << 20):
                        f.write(chunk)
            if not size or dest.stat().st_size >= size:
                return
        except (FetchError, OSError, Exception) as e:  # noqa: BLE001 — retried
            log.warning("adsb_history: download of %s interrupted (%s); retry %d", dest.name,
                        type(e).__name__, attempt + 1)
            await asyncio.sleep(5 * (attempt + 1))
    raise FetchError(f"could not download {dest.name}")


# --------------------------------------------------------------------------- run


def process_archive(ctx: BackfillContext, parts: list[Path], mil_db: MilDb, thin_s: float,
                    summary: Summary) -> dict[str, int]:
    """Stream the tar parts; write military positions + tracks. Sync (thread)."""
    stats = {"traces": 0, "military": 0, "bad": 0, "positions": 0}
    buffer: list[dict] = []
    tracks: list[tuple[TrackInfo, datetime]] = []

    def flush() -> None:
        if buffer:
            ctx.write("positions", buffer, summary)
            buffer.clear()
        if tracks and not ctx.dry_run:
            for t, seen in tracks:
                ctx.store.upsert_tracks([t], seen)
            ctx.checkpoint.add_rows("tracks", len(tracks))
        tracks.clear()

    reader = io.BufferedReader(ConcatReader(parts), buffer_size=8 << 20)
    try:
        for _name, data in iter_traces(reader):
            stats["traces"] += 1
            if data is None:
                stats["bad"] += 1
                continue
            try:
                res = parse_trace(data, mil_db, thin_s=thin_s)
            except (ValueError, TypeError, KeyError):
                stats["bad"] += 1
                continue
            if res is None:
                continue
            stats["military"] += 1
            if res.last_ts is not None:
                tracks.append((res.track, res.last_ts))
            buffer.extend(position_row(p) for p in res.positions)
            stats["positions"] += len(res.positions)
            if len(buffer) >= FLUSH_ROWS:
                flush()
            if stats["traces"] % 100_000 == 0:
                log.info("adsb_history: %d traces read, %d military, %d positions",
                         stats["traces"], stats["military"], stats["positions"])
        flush()
    finally:
        reader.close()
    summary.add("tracks", stats["military"])
    return stats


async def run(ctx: BackfillContext, window: Window) -> Summary:
    summary = Summary(source="adsb_history")
    cp = ctx.checkpoint
    days = history_dates(window)
    summary.units_total = len(days)
    variant = str(ctx.opt("variant", "prod"))
    thin_s = float(ctx.opt("thin_s", 60))
    factor = float(ctx.opt("disk_factor", 2.0))
    max_bytes = float(ctx.opt("max_archive_gb", 12)) * 1e9
    mil_db = await asyncio.to_thread(
        MilDb.from_csv, ctx.settings.reference_dir / "plane-alert-mil.csv")
    releases: dict[int, list[dict]] = {}
    notes: list[str] = []
    for day in days:
        key = f"{day:%Y-%m-%d}|{variant}|thin{thin_s:g}"
        if cp.is_done(key):
            summary.units_skipped += 1
            continue
        try:
            if day.year not in releases:
                releases[day.year] = await list_releases(ctx, day.year)
        except FetchError as e:
            msg = f"{day}: GitHub release listing failed: {e}"
            summary.errors.append(msg)
            cp.error(msg)
            continue
        rel = pick_release(releases[day.year], day, variant)
        assets = tar_assets(rel) if rel else []
        if not assets:
            notes.append(f"{day}: no {variant} release asset found (skipped)")
            log.warning("adsb_history %s: no %s release found; skipped", day, variant)
            summary.units_skipped += 1
            continue
        total = sum(int(a.get("size") or 0) for a in assets)
        tmp = ctx.tmp_dir / "adsb_history" / str(rel["tag_name"])
        tmp.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(tmp).free
        if total > max_bytes or free < factor * total:
            notes.append(f"{day}: archive {total / 1e9:.1f} GB, {free / 1e9:.1f} GB free "
                         f"(need {factor:g}x, max {max_bytes / 1e9:.0f} GB) - skipped")
            log.warning("adsb_history %s: %s", day, notes[-1])
            summary.units_skipped += 1
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        if ctx.dry_run:
            log.info("adsb_history %s: dry run - would download %s (%d parts, %.2f GB)", day,
                     rel["tag_name"], len(assets), total / 1e9)
            summary.units_done += 1
            continue
        t0 = time.perf_counter()
        parts = [tmp / a["name"] for a in assets]
        try:
            for a, dest in zip(assets, parts, strict=True):
                log.info("adsb_history %s: downloading %s (%.2f GB)", day, a["name"],
                         int(a.get("size") or 0) / 1e9)
                await download_asset(ctx, a["browser_download_url"], dest,
                                     int(a.get("size") or 0))
            t1 = time.perf_counter()
            stats = await asyncio.to_thread(process_archive, ctx, parts, mil_db, thin_s,
                                            summary)
        except Exception as e:  # noqa: BLE001 — next day continues
            msg = f"{day}: {type(e).__name__}: {e}"
            summary.errors.append(msg)
            cp.error(msg)
            log.error("adsb_history %s", msg)
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # archives are deleted after each day
        log.info("adsb_history %s: %d traces, %d military aircraft, %d positions "
                 "(%d undecodable); download %.0fs, processing %.0fs", day, stats["traces"],
                 stats["military"], stats["positions"], stats["bad"], t1 - t0,
                 time.perf_counter() - t1)
        cp.mark_done(key, **stats)
        summary.units_done += 1
    if notes:
        summary.message = "; ".join(notes)
    return summary
