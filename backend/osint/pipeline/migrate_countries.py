"""Country normalisation migration (deterministic extraction spec §2.7).

    python -m osint.pipeline.migrate_countries [--dry-run] [--days 30]

Rewrites ``events.country`` to ISO2 by each event's source: ACLED / UCDP names; for news
and sensor events a value that is not ISO2 is read as FIPS 10-4 when two letters (legacy
GDELT), ISO3 / CAMEO when three, else as a name. Re-resolves every event's actors
(``actors[].country`` ISO2 + ``source``), fills ``actor_countries`` / ``foreign_involved`` /
``attributed`` and ``lifecycle``, and runs the rule extractors for events of the last
``--days`` days. Holds ``data/.event_rebuild.lock`` (the lock of ``retriage
--rebuild-events``): the server's EventBuilder pauses meanwhile and reloads its index
afterwards. Prints a before/after report (country-code formats, FIPS / names rewritten,
unresolved values, attributed share). ``--dry-run`` writes nothing: it opens the databases
read-only (no schema upgrade either), takes no lock and writes no marker.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from osint.core import countries
from osint.core.geo import utcnow
from osint.pipeline.events import REBUILD_STATE, rebuild_lock_path
from osint.pipeline.lifecycle import LifecycleConfig, lifecycle_for_row, refresh_lifecycle
from osint.pipeline.rule_pass import RulePass
from osint.store.lake import FileLock
from osint.store.sqlite import COUNTRY_MIGRATION_STATE, SqliteStore, iso

log = logging.getLogger(__name__)


def normalise_country(event_id: str, origin: str, value: str | None
                      ) -> tuple[str | None, str]:
    """(ISO2 or None, kind) for an events row. kind: kept (already ISO2) | fips | name |
    code (ISO3 / CAMEO / case) | unresolved | empty."""
    raw = (value or "").strip()
    if not raw:
        return None, "empty"
    code = raw.upper()
    if origin == "historical" or event_id.startswith(("acled-", "ucdp-")):
        if len(raw) == 2 and countries.get(code) is not None:
            return code, "kept" if code == raw else "code"  # migrated before
        cc = countries.to_iso2(raw, "name")
        return cc, "name" if cc else "unresolved"
    if len(code) == 2 and code.isalpha():
        if countries.get(code) is not None:
            return code, "kept" if code == raw else "code"
        cc = countries.to_iso2(code, "fips")  # legacy GDELT ActionGeo code
        return cc, "fips" if cc else "unresolved"
    if len(code) == 3 and code.isalpha():
        cc = countries.get(countries.to_iso2(code, "auto") or "")
        return (cc.iso2, "code") if cc else (None, "unresolved")
    cc = countries.to_iso2(raw, "name")
    return cc, "name" if cc else "unresolved"


def _distribution(values: list[str | None], top: int = 20) -> dict[str, Any]:
    fmt: Counter[str] = Counter()
    for v in values:
        s = (v or "").strip()
        if not s:
            fmt["null"] += 1
        elif len(s) == 2 and countries.get(s) is not None and s == s.upper():
            fmt["iso2"] += 1
        elif len(s) == 2:
            fmt["two_letter_other"] += 1
        elif len(s) == 3 and s.isalpha():
            fmt["three_letter"] += 1
        else:
            fmt["name"] += 1
    return {"by_format": {k: fmt.get(k, 0) for k in
                          ("iso2", "two_letter_other", "three_letter", "name", "null")},
            "top": [[c, n] for c, n in Counter(v for v in values if v).most_common(top)]}


def migrate(store: SqliteStore, rule_pass: RulePass, *, now: datetime, days: float = 30.0,
            lifecycle: LifecycleConfig | None = None, dry_run: bool = False,
            lock_timeout: float = 600.0) -> dict[str, Any]:
    """Run the migration (under the event-rebuild lock unless ``dry_run``); returns the
    report. ``rule_pass`` must be the migration's own instance (its snippet cache is not
    shared with a live EventBuilder)."""
    if dry_run:
        return _migrate(store, rule_pass, now=now, days=days, lifecycle=lifecycle,
                        dry_run=True)
    with FileLock(rebuild_lock_path(store.path), lock_timeout):
        return _migrate(store, rule_pass, now=now, days=days, lifecycle=lifecycle,
                        dry_run=False)


def _lifecycle_preview(store: SqliteStore, now: datetime, cfg: LifecycleConfig,
                       ids: list[str]) -> dict[str, int]:
    """Lifecycle distribution the migration would write (dry run: computed, not stored;
    the inputs of ``SqliteStore.lifecycle_inputs`` without its ``lifecycle`` column, which a
    database predating the schema upgrade lacks)."""
    out: Counter[str] = Counter()
    since = iso(now - timedelta(hours=cfg.developing_h))
    for i in range(0, len(ids), 800):
        chunk = ids[i: i + 800]
        with store._lock:
            rows = store._db.execute(
                "SELECT e.id, e.origin, e.t_end, MAX(d.published_at), "
                "COALESCE(SUM(d.published_at >= ?), 0) FROM events e "
                "LEFT JOIN event_documents d ON d.event_id = e.id AND d.role != 'gdelt' "
                f"WHERE e.id IN ({','.join('?' * len(chunk))}) GROUP BY e.id",
                (since, *chunk)).fetchall()
        for r in rows:
            out[lifecycle_for_row({"origin": r[1], "t_end": r[2], "last_doc_at": r[3],
                                   "recent_docs": r[4]}, now, cfg)] += 1
    return dict(out.most_common())


def _lifecycle_counts(store: SqliteStore) -> dict[str, int]:
    with store._lock:
        rows = store._db.execute(
            "SELECT COALESCE(lifecycle, 'null'), COUNT(*) FROM events GROUP BY 1 "
            "ORDER BY 2 DESC").fetchall()
    return {r[0]: r[1] for r in rows}


def _migrate(store: SqliteStore, rule_pass: RulePass, *, now: datetime, days: float,
             lifecycle: LifecycleConfig | None, dry_run: bool) -> dict[str, Any]:
    t0 = time.perf_counter()
    cfg = lifecycle or LifecycleConfig()
    countries.drain_unresolved()
    rows = store.event_countries()
    kinds: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    override: dict[str, str | None] = {}
    changes: list[tuple[str | None, str]] = []
    for eid, origin, value in rows:
        cc, kind = normalise_country(eid, origin, value)
        kinds[kind] += 1
        if kind == "unresolved":
            # the raw value is kept (never erased); only reported
            unresolved[str(value)] += 1
            override[eid] = value
            continue
        override[eid] = cc
        if cc != value:
            changes.append((cc, eid))
    if not dry_run and changes:
        store.set_event_countries(changes)
    ids = [r[0] for r in rows]
    recent = set(store.event_ids_since(now - timedelta(days=days)))
    recent_ids = [e for e in ids if e in recent]
    s_recent = rule_pass.run(recent_ids, fields=True, write=not dry_run,
                             country_override=override)
    s_old = rule_pass.run([e for e in ids if e not in recent], fields=False,
                          lake_snippets=False, write=not dry_run, country_override=override)
    lifecycle_changed = 0
    if dry_run:
        lifecycle_dist = _lifecycle_preview(store, now, cfg, ids)
    else:
        lifecycle_changed = refresh_lifecycle(store, now, cfg, ids)
        lifecycle_dist = _lifecycle_counts(store)
        marker = store.get_cursor(REBUILD_STATE) or {}
        # a new rebuild marker makes a running EventBuilder reload its index (normalised
        # countries and actors in memory); "migrated" keeps migrate_events from treating it
        # as a fresh rebuild (no legacy-event deletion)
        store.set_cursor(REBUILD_STATE, {**marker, "at": utcnow().isoformat(),
                                         "migrated": marker.get("migrated", True)})
        prev = store.get_cursor(COUNTRY_MIGRATION_STATE) or {}
        first = prev if prev.get("done") else {}
        store.set_cursor(COUNTRY_MIGRATION_STATE, {
            "done": True, "at": first.get("at", iso(now)),
            "rewritten": first.get("rewritten", len(changes)),
            "last_at": iso(now), "last_rewritten": len(changes)})
    total = len(ids)
    resolved = s_recent.events + s_old.events
    return {
        "dry_run": dry_run, "events": total,
        "before": _distribution([r[2] for r in rows]),
        "after": _distribution(list(override.values())),
        "rewritten": {"fips": kinds["fips"], "name": kinds["name"], "code": kinds["code"]},
        "kept": kinds["kept"], "empty": kinds["empty"],
        "unresolved": dict(unresolved.most_common(50)),
        "attributed_share": round((s_recent.attributed + s_old.attributed) / total, 4)
        if total else 0.0,
        "foreign_share": round((s_recent.foreign + s_old.foreign) / total, 4) if total else 0.0,
        "actor_events": resolved, "recent_events": len(recent_ids),
        "rule_field_values": s_recent.fields,
        # events of the last ``days`` days with a rule value per field (historical events
        # keep their curated fields and are not counted)
        "rule_field_coverage": dict(sorted(s_recent.field_events.items())),
        "rule_errors": s_recent.errors + s_old.errors,
        "lifecycle": lifecycle_dist, "lifecycle_changed": lifecycle_changed,
        "unresolved_values": countries.drain_unresolved(),
        "seconds": round(time.perf_counter() - t0, 1),
    }


class _PreMigrationView:
    """Read-only store on a database that predates ``event_fields.source`` (dry run before
    the schema upgrade): there are no rule rows yet, so ``rule_fields`` is empty."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def rule_fields(self, event_ids: list[str]) -> dict[str, list[dict]]:
        return {}


def _side_files(db: Path) -> dict[Path, bool]:
    return {p: p.exists() for p in (Path(f"{db}-wal"), Path(f"{db}-shm"))}


def _drop_created_side_files(seen: dict[Path, bool]) -> None:
    """Remove the -wal/-shm that a read-only open of a WAL database created. The -wal only
    when it is empty; the -shm (a 32 KB wal-index, never 0 bytes) only when this run created
    it and its -wal is empty or gone (no frames to index). A file another process still
    holds open cannot be unlinked on Windows and is left alone."""
    for p, existed in sorted(seen.items(), key=lambda kv: not kv[0].name.endswith("-wal")):
        try:
            if existed or not p.exists():
                continue
            if p.name.endswith("-wal"):
                if p.stat().st_size == 0:
                    p.unlink()
            else:
                wal = p.with_name(p.name[:-4] + "-wal")
                if not wal.exists() or wal.stat().st_size == 0:
                    p.unlink()
        except OSError:
            log.warning("could not remove %s", p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osint.pipeline.migrate_countries", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--days", type=float, default=30.0,
                    help="run the rule extractors for events of the last N days")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from osint.config import Settings
    from osint.enrich.actors import ActorLexicon
    from osint.enrich.rules import RuleSet
    from osint.pipeline.events import EventsConfig
    from osint.pipeline.extract.store import ExtractionStore
    from osint.store.lake import Lake

    st = Settings()
    dry = args.dry_run
    # a dry run must not touch the (possibly live) databases: read-only connections, no
    # schema upgrade, no lake directory created
    ext_path = st.data_dir / "extraction.sqlite"
    seen = {**_side_files(st.sqlite_path), **_side_files(ext_path)} if dry else {}
    if not dry:
        print("Stop the server (or upgrade it to this version) and back up data/ before "
              "running without --dry-run; the run holds the rebuild lock and adds "
              "columns/indexes.", file=sys.stderr)
    store = SqliteStore.open_read_only(st.sqlite_path) if dry else SqliteStore(st.sqlite_path)
    view: Any = store
    if dry and not store.has_column("event_fields", "source"):
        view = _PreMigrationView(store)
    lake = Lake(st.lake_dir) if not dry or st.lake_dir.is_dir() else None
    extractions = ExtractionStore(ext_path, read_only=dry) if ext_path.is_file() else None
    cfg = EventsConfig.from_file(st.config_dir / "events.yaml")
    rule_pass = RulePass(  # the migration's own instance: its snippet LRU is not shared
        view, lexicon=ActorLexicon.load(st.config_dir / "actors.yaml",
                                        groups_path=st.config_dir / "terror_groups.yaml"),
        rules=RuleSet.load(st.config_dir / "extract_rules.yaml"), lake=lake,
        extractions=extractions)
    try:
        report = migrate(view, rule_pass, now=utcnow(), days=args.days,
                         lifecycle=cfg.lifecycle, dry_run=dry)
    finally:
        if extractions is not None:
            extractions.close()
        if lake is not None:
            lake.close()
        store.close()
        _drop_created_side_files(seen)
    # ASCII-escaped: the Windows console code page cannot print every country name
    print(json.dumps({"migrate_countries": report}, indent=2, ensure_ascii=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
