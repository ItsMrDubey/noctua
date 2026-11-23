"""Event status, confidence and the EventBuilder loop (pipeline stages 3, 8 and 9).

``EventBuilder`` runs every ``interval_s`` seconds: it reads documents fetched since its
cursor from the lake (public ``Lake.query`` only), clusters them into events
(``osint.pipeline.cluster``), and persists events, document links, field provenance and
GDELT decisions to SQLite in one transaction. The cursor (``source_state`` row
``event_builder``) is the newest ``fetched_at`` seen; each pass re-reads a short overlap
window to catch late lake writes and skips documents it has already handled.

Status levels (spec §3): ``reported`` (1 outlet) -> ``multi-source`` (>= 2 independent
outlets, i.e. distinct article URL domains) -> ``sensor-corroborated`` (corroboration score
>= threshold, ``osint.pipeline.corroborate``, run inside the builder pass) -> ``official``
(M4 hook: ``EventState.official``). Sensor-derived events (``osint.pipeline.sensor_events``)
are not loaded into the clusterer.

Confidence is a weighted sum of components in 0..1, weights in ``config/events.yaml``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import h3
import yaml

from osint.config import ROOT
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.enrich.gazetteer import fold
from osint.pipeline.cluster import (
    CLUSTER_VERSION,
    PRECISION_RANK,
    ClusterDoc,
    Clusterer,
    Embedder,
    EventState,
)
from osint.pipeline.corroborate import CorroborationConfig, Corroborator
from osint.store.lake import FileLock, Lake
from osint.store.sqlite import SqliteStore

if TYPE_CHECKING:
    from osint.runtime import Runtime

log = logging.getLogger(__name__)

DEFAULT_CONFIG = ROOT / "config" / "events.yaml"
CURSOR_NAME = "event_builder"
REBUILD_STATE = "event_rebuild"
REBUILD_LOCK = ".event_rebuild.lock"
VERDICT_RULES = 2  # bump when apply_verdict_records semantics change
NO_ARTICLE_BACKING = "no_article_backing"
STATUS_LEVELS = ("reported", "multi-source", "sensor-corroborated", "official")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------- config


@dataclass
class EventsConfig:
    interval_s: float = 60.0
    min_relevance: float = 0.35
    min_relevance_create: float = 0.5
    min_place_confidence: float = 0.65
    category_gates: dict[str, list[str]] = field(default_factory=dict)
    cursor_overlap_min: float = 10.0
    batch_limit: int = 20000
    require_location: bool = False
    permutations: int = 128
    shingle: int = 5
    bands: int = 32
    index_window_h: float = 72.0
    text_threshold: float = 0.5
    overlap_threshold: float = 0.2
    place_only_window_h: float = 6.0
    refine_max_km: float = 300.0
    embed_threshold: float = 0.85
    min_shared_tokens: dict[str, int] = field(default_factory=lambda: {"default": 2})
    max_signatures: int = 8
    max_tokens: int = 64
    time_window_h: dict[str, float] = field(default_factory=lambda: {"default": 24.0})
    gdelt_tracked_roots: frozenset[str] = frozenset(
        {"13", "14", "15", "16", "17", "18", "19", "20"})
    gdelt_tracked_codes: tuple[str, ...] = ()
    gdelt_min_mentions: int = 3
    gdelt_stale_h: float = 24.0
    gdelt_stale_event_days: float = 7.0
    multi_source_min_domains: int = 2
    weights: dict[str, float] = field(default_factory=lambda: {
        "source_diversity": 0.25, "geo": 0.2, "relevance": 0.25, "recency": 0.1,
        "gdelt": 0.05, "sensor": 0.15})
    precision_score: dict[str, float] = field(default_factory=lambda: {
        "point": 1.0, "city": 1.0, "admin1": 0.6, "country": 0.2})
    recency_half_life_h: float = 24.0
    verification: dict[str, float] = field(default_factory=lambda: {
        "unsupported_cap": 0.2, "mismatch_penalty": 0.25, "supported_bonus": 0.1})

    def window_h(self, category: str) -> float:
        return float(self.time_window_h.get(category, self.time_window_h.get("default", 24)))

    def min_shared(self, category: str) -> int:
        return int(self.min_shared_tokens.get(category,
                                              self.min_shared_tokens.get("default", 2)))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EventsConfig:
        mh = d.get("minhash") or {}
        cl = d.get("clustering") or {}
        gd = d.get("gdelt") or {}
        conf = d.get("confidence") or {}
        base = cls()
        return cls(
            interval_s=float(d.get("interval_s", base.interval_s)),
            min_relevance=float(d.get("min_relevance", base.min_relevance)),
            min_relevance_create=float(d.get("min_relevance_create",
                                             base.min_relevance_create)),
            min_place_confidence=float(d.get("min_place_confidence",
                                             base.min_place_confidence)),
            category_gates={str(c): [str(t) for t in _flat_terms(v)]
                            for c, v in (d.get("category_gates") or {}).items()},
            cursor_overlap_min=float(d.get("cursor_overlap_min", base.cursor_overlap_min)),
            batch_limit=int(d.get("batch_limit", base.batch_limit)),
            require_location=bool(d.get("require_location", base.require_location)),
            permutations=int(mh.get("permutations", base.permutations)),
            shingle=int(mh.get("shingle", base.shingle)),
            bands=int(mh.get("bands", base.bands)),
            index_window_h=float(cl.get("index_window_h", base.index_window_h)),
            text_threshold=float(cl.get("text_threshold", base.text_threshold)),
            overlap_threshold=float(cl.get("overlap_threshold", base.overlap_threshold)),
            place_only_window_h=float(cl.get("place_only_window_h",
                                             base.place_only_window_h)),
            embed_threshold=float(cl.get("embed_threshold", base.embed_threshold)),
            refine_max_km=float(cl.get("refine_max_km", base.refine_max_km)),
            min_shared_tokens={str(k): int(v) for k, v in
                               (cl.get("min_shared_tokens") or base.min_shared_tokens).items()},
            max_signatures=int(cl.get("max_signatures", base.max_signatures)),
            max_tokens=int(cl.get("max_tokens", base.max_tokens)),
            time_window_h={str(k): float(v) for k, v in
                           (cl.get("time_window_h") or base.time_window_h).items()},
            gdelt_tracked_roots=frozenset(
                str(r).zfill(2) for r in gd.get("tracked_roots", base.gdelt_tracked_roots)),
            gdelt_tracked_codes=tuple(str(c) for c in gd.get("tracked_codes", ())),
            gdelt_min_mentions=int(gd.get("min_mentions", base.gdelt_min_mentions)),
            gdelt_stale_h=float(gd.get("stale_h", base.gdelt_stale_h)),
            gdelt_stale_event_days=float(gd.get("stale_event_days",
                                                base.gdelt_stale_event_days)),
            multi_source_min_domains=int((d.get("status") or {}).get(
                "multi_source_min_domains", base.multi_source_min_domains)),
            weights={str(k): float(v) for k, v in
                     (conf.get("weights") or base.weights).items()},
            precision_score={str(k): float(v) for k, v in
                             (conf.get("precision_score") or base.precision_score).items()},
            recency_half_life_h=float(conf.get("recency_half_life_h",
                                               base.recency_half_life_h)),
            verification={**base.verification, **{
                str(k): float(v) for k, v in (d.get("verification") or {}).items()}},
        )

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG) -> EventsConfig:
        if not path.is_file():
            log.warning("events config %s missing; using defaults", path)
            return cls()
        with path.open(encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})


def _flat_terms(block: Any) -> list[str]:
    """A term block is a list or {language: list} (triage.yaml syntax)."""
    if isinstance(block, dict):
        return [t for terms in block.values() for t in (terms or [])]
    return list(block or [])


def load_cameo_map(triage_yaml: Path) -> Callable[[str, str], str | None]:
    """CAMEO event code / root -> our category, from triage.yaml (overrides win)."""
    roots: dict[str, str] = {}
    overrides: dict[str, str] = {}
    if triage_yaml.is_file():
        with triage_yaml.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        roots = {str(k): str(v) for k, v in (data.get("cameo_roots") or {}).items()}
        overrides = {str(k): str(v) for k, v in (data.get("cameo_overrides") or {}).items()}

    def category(code: str, root: str) -> str | None:
        return overrides.get(code) or overrides.get(code[:3]) or roots.get(root)

    return category


# -------------------------------------------------------------- status/confidence


def event_status(ev: EventState, cfg: EventsConfig) -> str:
    if ev.official:
        return "official"
    if ev.sensor_corroborated:
        return "sensor-corroborated"
    if ev.source_diversity >= cfg.multi_source_min_domains:
        return "multi-source"
    return "reported"


def confidence_components(ev: EventState, cfg: EventsConfig, now: datetime,
                          sensor: float = 0.0) -> dict[str, float]:
    age_h = max(0.0, (now - ev.t_end).total_seconds() / 3600)
    geo = 0.0
    if ev.lat is not None:
        geo = cfg.precision_score.get(ev.precision or "", 0.0) * (ev.geo_confidence or 0.0)
    return {
        "source_diversity": 1 - 0.5 ** max(0, ev.source_diversity - 1),
        "geo": min(1.0, max(0.0, geo)),
        "relevance": min(1.0, max(0.0, ev.relevance)),
        "recency": 0.5 ** (age_h / cfg.recency_half_life_h) if cfg.recency_half_life_h else 1.0,
        "gdelt": 1.0 if ev.gdelt_accepted else 0.0,
        "sensor": min(1.0, max(0.0, sensor)),  # corroboration score (pipeline.corroborate)
    }


def event_confidence(ev: EventState, cfg: EventsConfig, now: datetime,
                     sensor: float = 0.0) -> float:
    comps = confidence_components(ev, cfg, now, sensor)
    total = sum(cfg.weights.get(k, 0.0) * v for k, v in comps.items())
    return round(min(1.0, max(0.0, total)), 4)


def apply_verdict(confidence: float, verdict: str | None, cfg: EventsConfig) -> float:
    """Article-extraction verdict adjustment (config ``verification``): unsupported caps,
    mismatch subtracts, supported adds."""
    v = cfg.verification
    if verdict == "unsupported":
        confidence = min(confidence, v.get("unsupported_cap", 0.2))
    elif verdict == "mismatch":
        confidence -= v.get("mismatch_penalty", 0.25)
    elif verdict == "supported":
        confidence += v.get("supported_bonus", 0.1)
    return round(min(1.0, max(0.0, confidence)), 4)


def make_scorer(cfg: EventsConfig, sensor: Callable[[str], float] | None = None,
                verdict: Callable[[str], str | None] | None = None
                ) -> Callable[[EventState, datetime], tuple[str, float]]:
    """``sensor(event_id)`` gives the event's corroboration score (0 when absent);
    ``verdict(event_id)`` its extraction verdict (None when not extracted)."""
    def score(ev: EventState, now: datetime) -> tuple[str, float]:
        s = sensor(ev.id) if sensor is not None else 0.0
        conf = event_confidence(ev, cfg, now, s)
        if verdict is not None:
            conf = apply_verdict(conf, verdict(ev.id), cfg)
        return event_status(ev, cfg), conf
    return score


# ------------------------------------------------------------------------- lake read

_DOC_SQL = """
SELECT id, source, kind, origin_id, url, title, snippet, language,
       epoch_us(published_at), epoch_us(fetched_at), attrs, relevance, categories, places,
       place_name, place_precision, place_confidence, lat, lon, h3_3, h3_5, h3_7
FROM documents
WHERE fetched_at >= ? AND published_at >= ? AND (relevance >= ? OR kind = 'gdelt_event')
ORDER BY fetched_at, id
LIMIT ?
"""


def rebuild_lock_path(sqlite_path: Path) -> Path:
    """``data/.event_rebuild.lock`` next to the events database."""
    return Path(sqlite_path).parent / REBUILD_LOCK


def _ts(us: int | None) -> datetime | None:
    return None if us is None else _EPOCH + timedelta(microseconds=us)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def row_to_doc(r: tuple) -> ClusterDoc:
    return ClusterDoc(
        id=r[0], source=r[1] or "", kind=r[2] or "article", origin_id=r[3] or "",
        url=r[4] or "", title=r[5] or "", snippet=r[6] or "", language=r[7] or "",
        published_at=_ts(r[8]), fetched_at=_ts(r[9]), attrs=_loads(r[10], {}),
        relevance=float(r[11] or 0.0),
        categories=[c for c in (r[12] or "").split(",") if c],
        places=_loads(r[13], []), place_name=r[14], place_precision=r[15],
        place_confidence=None if r[16] is None else float(r[16]),
        lat=r[17], lon=r[18], h3_3=r[19], h3_5=r[20], h3_7=r[21],
    )


# --------------------------------------------------------------------------- builder


@dataclass
class PassStats:
    documents: int = 0
    clustered: int = 0
    created: int = 0
    updated: int = 0
    gdelt_accepted: int = 0
    gdelt_rejected: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    corroboration_evaluated: int = 0
    verdicts_applied: int = 0
    sensor_corroborated: int = 0
    seconds: float = 0.0


class EventBuilder:
    """Periodic lake -> clusterer -> SQLite pass. ``run()`` is the async loop."""

    def __init__(self, store: SqliteStore, lake: Lake, metrics: Metrics | None = None,
                 config: EventsConfig | None = None, *,
                 cameo_category: Callable[[str, str], str | None] | None = None,
                 embedder: Embedder | None = None,
                 corroborator: Corroborator | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.lake = lake
        self.metrics = metrics or Metrics()
        self.cfg = config or EventsConfig()
        self.clock = clock
        self.corroborator = corroborator
        self.sensor_scores: dict[str, float] = (
            corroborator.scores if corroborator is not None else {})
        self.verifier: Any = None  # extraction store (pipeline.extract), set by wire()
        self.verdicts: dict[str, str] = {}  # event id -> applied extraction verdict
        self._verdict_cursor: datetime | None = None
        self._impact_cursor: datetime | None = None
        self.clusterer = Clusterer(self.cfg, make_scorer(
            self.cfg, lambda eid: self.sensor_scores.get(eid, 0.0),
            lambda eid: self.verdicts.get(eid)),
                                   cameo_category=cameo_category, embedder=embedder)
        self._seen: dict[str, datetime] = {}  # doc id -> fetched_at (overlap window)
        try:
            self._lake_time_window = "start" in inspect.signature(lake.query).parameters
        except (TypeError, ValueError):
            self._lake_time_window = False
        self._warm = False
        self._rebuild_at: str | None = None
        # OS lock shared with rebuild_events: passes are skipped while a rebuild runs
        path = getattr(store, "path", None)
        self.rebuild_lock: Path | None = rebuild_lock_path(path) if path else None
        self.paused = False
        self._stop = asyncio.Event()
        self.passes = 0
        self.last_pass: PassStats | None = None
        self.last_error: str | None = None

    @classmethod
    def from_settings(cls, settings: Any, store: SqliteStore, lake: Lake,
                      metrics: Metrics | None = None) -> EventBuilder:
        cfg_dir = Path(settings.config_dir)
        metrics = metrics or Metrics()
        corroborator = Corroborator(
            store, lake, metrics,
            CorroborationConfig.from_file(cfg_dir / "corroboration.yaml"))
        return cls(store, lake, metrics, EventsConfig.from_file(cfg_dir / "events.yaml"),
                   cameo_category=load_cameo_map(cfg_dir / "triage.yaml"),
                   corroborator=corroborator)

    # ---------------------------------------------------------------- state

    def warm_start(self) -> None:
        """Rebuild the in-memory index (recent events) and the processed-document set."""
        migrate_events(self.store, self.cfg.verification.get("unsupported_cap", 0.2))
        marker = self.store.get_cursor(REBUILD_STATE) or {}
        self._rebuild_at = marker.get("at")
        self.clusterer.generation = int(marker.get("generation") or 0)
        now = self.clock()
        since = now - timedelta(hours=self.cfg.index_window_h)
        # news events only (origin column): sensor and historical rows are never clustered
        n = self.clusterer.load(self.store.recent_events(since))
        if self.corroborator is not None:
            self.corroborator.warm_start(now)
        self.verdicts = {k: v["verdict"] for k, v in self.store.verifications().items()}
        self._impact_cursor = None  # re-apply impact upgrades to the reloaded index
        cursor = self._cursor()
        if cursor is not None:
            horizon = cursor - timedelta(minutes=self.cfg.cursor_overlap_min)
            # decisions/links made at or after the overlap start cover re-read docs
            for doc_id in self.store.event_doc_ids_since(horizon - timedelta(hours=1)):
                self._seen[doc_id] = cursor
        self._warm = True
        log.info("event builder warm start: %d recent events, %d seen documents", n,
                 len(self._seen))

    def _cursor(self) -> datetime | None:
        c = self.store.get_cursor(CURSOR_NAME) or {}
        us = c.get("fetched_at_us")
        return _ts(int(us)) if us is not None else None

    # ---------------------------------------------------------------- one pass

    def read_documents(self, since_fetched: datetime, now: datetime) -> list[ClusterDoc]:
        min_published = now - timedelta(hours=self.cfg.index_window_h)
        params = [since_fetched, min_published, self.cfg.min_relevance, self.cfg.batch_limit]
        if self._lake_time_window:  # partition pruning on published_at when supported
            rows = self.lake.query(_DOC_SQL, params, start=min_published)
        else:
            rows = self.lake.query(_DOC_SQL, params)
        out = []
        for r in rows:
            try:
                out.append(row_to_doc(r))
            except (TypeError, ValueError):
                log.warning("event builder: skipped malformed document row %r", r[0])
        return out

    def run_once(self) -> PassStats:
        """One synchronous pass (call from a worker thread). Skipped (``paused``) while an
        event rebuild holds the rebuild lock; after a rebuild the index is reloaded."""
        lock = FileLock(self.rebuild_lock, timeout=0) if self.rebuild_lock else None
        if lock is not None:
            try:
                lock.acquire()
            except TimeoutError:
                self.paused = True
                self._warm = False  # reload everything once the rebuild is done
                return PassStats()
        self.paused = False
        try:
            return self._run_once()
        finally:
            if lock is not None:
                lock.release()

    def _run_once(self) -> PassStats:
        t0 = time.perf_counter()
        if self._warm and (self.store.get_cursor(REBUILD_STATE) or {}).get(
                "at") != self._rebuild_at:
            self._warm = False  # a rebuild ran (another process): reload the index
        if not self._warm:
            self.clusterer.reset()
            self._seen.clear()
            self.warm_start()
        now = self.clock()
        cursor = self._cursor()
        if cursor is None:
            since = now - timedelta(hours=self.cfg.index_window_h)
        else:
            since = cursor - timedelta(minutes=self.cfg.cursor_overlap_min)
        docs = self.read_documents(since, now)
        fresh = [d for d in docs if d.id not in self._seen]
        res = self.clusterer.process(fresh, now)
        changed = [self.clusterer.events[eid].to_row() for eid in res.changed
                   if eid in self.clusterer.events]
        self.store.save_events(changed, res.documents, res.fields, res.decisions)
        newest = max((d.fetched_at for d in docs if d.fetched_at), default=None)
        if newest is not None and (cursor is None or newest > cursor):
            cursor = newest
            self.store.set_cursor(CURSOR_NAME, {
                "fetched_at_us": int((newest - _EPOCH) / timedelta(microseconds=1)),
                "fetched_at": newest.isoformat()})
        for d in fresh:
            self._seen[d.id] = d.fetched_at or now
        verified = 0
        if self.verifier is not None:
            try:
                verified = self.apply_verdicts(now)
            except Exception:  # verification must never stop event building
                self.metrics.inc("verification_errors_total")
                log.exception("applying extraction verdicts failed")
        try:
            self.apply_impacts(now)
        except Exception:  # impact upgrades must never stop event building
            self.metrics.inc("impact_errors_total")
            log.exception("applying impact sites failed")
        cres = None
        if self.corroborator is not None:
            try:
                cres = self.corroborator.maybe_run(self.clusterer.events, now,
                                                   self.clusterer.scorer)
            except Exception:  # corroboration must never stop event building
                self.metrics.inc("corroboration_errors_total")
                log.exception("corroboration pass failed")
        if cursor is not None:  # forget ids older than the next overlap window
            horizon = cursor - timedelta(minutes=self.cfg.cursor_overlap_min)
            self._seen = {k: v for k, v in self._seen.items() if v >= horizon}
        stats = PassStats(documents=len(fresh), clustered=res.clustered,
                          created=len(res.created), updated=len(res.updated),
                          gdelt_accepted=res.gdelt_accepted,
                          gdelt_rejected=dict(res.gdelt_rejected), skipped=dict(res.skipped),
                          corroboration_evaluated=cres.evaluated if cres else 0,
                          sensor_corroborated=len(cres.newly_corroborated) if cres else 0,
                          verdicts_applied=verified,
                          seconds=time.perf_counter() - t0)
        self._record(stats)
        return stats

    # ------------------------------------------------------------ verification

    def apply_verdicts(self, now: datetime) -> int:
        """Merge new extraction verdicts (``verifier.verdicts_since`` cursor) into events
        held in the clusterer's index (see ``apply_verdict_records``)."""
        since = self._verdict_cursor or now - timedelta(hours=self.cfg.index_window_h)
        changed = self.verifier.verdicts_since(since - timedelta(minutes=1))
        self._verdict_cursor = now
        ids = [e for e in changed if e in self.clusterer.events]
        if not ids:
            return 0
        return self.apply_verdict_records(
            {e: self.clusterer.events[e] for e in ids}, self.verifier.get_many(ids), now)

    def apply_verdict_records(self, events: dict[str, EventState], recs: dict[str, dict],
                              now: datetime, *, force: bool = False) -> int:
        """Apply extraction records to ``events``:

        * unsupported - and a mismatch on the event *type* (incl. an article about an
          untracked ``other`` event): confidence capped, hidden from the map default view
        * mismatch (place/country): confidence penalised; if the extraction geocoded a
          city/point place, the event moves there (old place kept as field
          ``location_before_extraction``)
        * supported: confidence bonus; verbatim quotes become event_fields; an event
          coarser than city is refined to the extraction's city/point place when it is in
          the same country (or, for an unlocated event, when the place name is quoted
          verbatim) - never a downgrade, never across countries
        Idempotent: a verdict whose fingerprint is unchanged is not re-applied (``force``
        re-applies, e.g. after a rebuild re-created the events)."""
        from osint.pipeline.extract.verdict import effective_verdict

        known = self.store.verifications(list(events))
        rows, fields, verifs = [], [], []
        for eid, ev in events.items():
            rec = recs.get(eid) or {}
            raw = rec.get("verdict")
            if rec.get("status") != "done" or raw not in (
                    "supported", "unsupported", "mismatch"):
                continue
            reasons = list(rec.get("reasons") or [])
            verdict = effective_verdict(raw, reasons)
            quotes = [q for q in rec.get("quotes") or [] if isinstance(q, dict)]
            geo = rec.get("geo") or None
            # VERDICT_RULES in the fingerprint: a rule change re-applies stored verdicts
            fp = json.dumps([VERDICT_RULES, verdict, reasons, geo, quotes], sort_keys=True,
                            default=str)
            fp = hashlib.sha1(fp.encode("utf-8")).hexdigest()[:16]
            self.verdicts[eid] = verdict
            if not force and (known.get(eid) or {}).get("fingerprint") == fp:
                continue
            doc_id = rec.get("doc_id") or "extraction"
            fine = bool(geo) and geo.get("precision") in ("city", "point") \
                and geo.get("lat") is not None and geo.get("lon") is not None
            if verdict == "mismatch" and fine:
                fields += self._relocate(ev, rec, geo, doc_id)
            if verdict == "supported":
                if fine and self._may_refine(ev, rec, geo, quotes):
                    fields += self._relocate(ev, rec, geo, doc_id, refine=True)
                for q in quotes:
                    if q.get("verbatim") and q.get("quote"):
                        fields.append({"event_id": eid, "field": str(q.get("field") or "quote"),
                                       "value": str(q["quote"])[:300], "doc_id": doc_id,
                                       "quote": str(q["quote"])[:300]})
            ev.status, ev.confidence = self.clusterer.scorer(ev, now)
            ev.updated_at = now
            rows.append(ev.to_row())
            verifs.append({"event_id": eid, "verdict": verdict, "reasons": reasons,
                           "quotes": quotes, "fingerprint": fp})
            self.metrics.inc("event_verdicts_applied_total", verdict=verdict)
        if verifs:
            self.store.save_events(rows, fields=fields)
            self.store.save_verifications(verifs)
        return len(verifs)

    def apply_impacts(self, now: datetime) -> int:
        """Upgrade events in the index to their best impact site (``event_impacts``,
        written by pipeline.impact_job): only a fine site (exact / facility /
        neighbourhood / settlement), only when the record was computed from the event's
        current location (an event moved meanwhile waits for a recompute), never across
        countries. The old location is kept as field ``location_before_impact``."""
        from osint.enrich.impact import FINE

        since = self._impact_cursor or now - timedelta(hours=self.cfg.index_window_h)
        recs = self.store.impacts_since(since - timedelta(minutes=1))
        self._impact_cursor = now
        rows, fields = [], []
        for eid, rec in recs.items():
            ev = self.clusterer.events.get(eid)
            best = rec.get("best") or {}
            if ev is None or best.get("precision") not in FINE or best.get("lat") is None:
                continue
            lat, lon = float(best["lat"]), float(best["lon"])
            if ev.lat is not None and abs(ev.lat - lat) < 1e-6 and abs(ev.lon - lon) < 1e-6:
                continue  # already there
            origin = rec.get("origin") or {}
            if ev.lat is None or origin.get("lat") is None or abs(
                    ev.lat - origin["lat"]) > 1e-6 or abs(ev.lon - origin["lon"]) > 1e-6:
                continue  # computed from another location: wait for the recompute
            if origin.get("country") and ev.country and \
                    origin["country"].upper() != ev.country.upper():
                continue
            name = str(best.get("name") or f"{lat:.4f}, {lon:.4f}")
            old_place = ev.place_name or ""
            old = f"{old_place} ({ev.lat}, {ev.lon}, {ev.precision})"
            old_cell = ev.h3_3
            ev.lat, ev.lon, ev.precision = lat, lon, "point"
            city = old_place.split(",")[0].strip()
            ev.place_name = name if not city or city.casefold() in name.casefold() \
                else f"{name}, {city}"
            ev.geo_confidence = float(best.get("confidence") or 0.6)
            ev.h3_3, ev.h3_5, ev.h3_7 = (h3.str_to_int(h3.latlng_to_cell(lat, lon, r))
                                         for r in (3, 5, 7))
            self.clusterer._index_geo(ev, old_cell)
            ev.status, ev.confidence = self.clusterer.scorer(ev, now)
            ev.updated_at = now
            rows.append(ev.to_row())
            note = (f"impact site ({best.get('precision')}, {best.get('resolver')}): "
                    f"{best.get('quote') or ''}")[:300]
            fields += [{"event_id": eid, "field": "location_before_impact",
                        "value": old[:300], "doc_id": "impact", "quote": note},
                       {"event_id": eid, "field": "location", "value": ev.place_name[:300],
                        "doc_id": "impact", "quote": note}]
            self.metrics.inc("event_impact_upgrades_total")
        if rows:
            self.store.save_events(rows, fields=fields)
        return len(rows)

    @staticmethod
    def _may_refine(ev: EventState, rec: dict, geo: dict, quotes: list[dict]) -> bool:
        """Supported extraction with a finer place: upgrade only, same country only."""
        if PRECISION_RANK.get(ev.precision or "", -1) >= PRECISION_RANK["city"]:
            return False  # already city/point: never moved by a supported verdict
        country = (geo.get("country") or "").upper()
        if ev.lat is not None and ev.country:
            return bool(country) and country == ev.country.upper()
        # unlocated (or no known country): the place must be quoted verbatim
        where = (rec.get("result") or {}).get("where") or {}
        name = fold(str(where.get("place_name") or "")).strip()
        if len(name) < 3:
            return False
        texts = [str(q.get("quote") or "") for q in quotes if q.get("verbatim")]
        texts += [ev.title or "", ev.summary or ""]
        return any(name in fold(t) for t in texts)

    def _relocate(self, ev: EventState, rec: dict, geo: dict, doc_id: str, *,
                  refine: bool = False) -> list[dict]:
        lat, lon = float(geo["lat"]), float(geo["lon"])
        if ev.lat is not None and abs(ev.lat - lat) < 1e-6 and abs(ev.lon - lon) < 1e-6:
            return []
        where = (rec.get("result") or {}).get("where") or {}
        name = where.get("place_name") or f"{lat:.2f}, {lon:.2f}"
        old = (f"{ev.place_name or ''} ({ev.lat}, {ev.lon}, {ev.precision})"
               if ev.lat is not None else f"{ev.place_name or ''} ({ev.precision})")
        old_cell = ev.h3_3
        ev.lat, ev.lon = lat, lon
        ev.precision, ev.place_name = str(geo["precision"]), name
        ev.country = geo.get("country") or ev.country
        ev.geo_confidence = 0.8
        ev.h3_3, ev.h3_5, ev.h3_7 = (h3.str_to_int(h3.latlng_to_cell(lat, lon, r))
                                     for r in (3, 5, 7))
        self.clusterer._index_geo(ev, old_cell)
        note = ("refined: supported extraction names a finer place" if refine
                else "moved: extraction place mismatch")
        return [{"event_id": ev.id, "field": "location_before_extraction", "value": old[:300],
                 "doc_id": doc_id, "quote": note},
                {"event_id": ev.id, "field": "location", "value": name, "doc_id": doc_id,
                 "quote": f"extraction: {name}"}]

    def _record(self, s: PassStats) -> None:
        m = self.metrics
        m.inc("events_created_total", s.created)
        m.inc("events_updated_total", s.updated)
        m.inc("documents_clustered_total", s.clustered)
        m.inc("gdelt_accepted_total", s.gdelt_accepted)
        for reason, n in s.gdelt_rejected.items():
            m.inc("gdelt_rejected_total", n, reason=reason)
        for reason, n in s.skipped.items():
            m.inc("documents_unclustered_total", n, reason=reason)
        m.set("event_builder_pass_seconds", s.seconds)
        m.set("event_builder_index_events", len(self.clusterer.events))
        self.passes += 1
        self.last_pass = s

    # ---------------------------------------------------------------- loop

    async def run(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive; the next pass retries
                self.last_error = f"{type(e).__name__}: {e}"
                self.metrics.inc("event_builder_errors_total")
                log.exception("event builder pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.interval_s)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------- migration / rebuild


def migrate_events(store: SqliteStore, unsupported_cap: float = 0.2) -> dict[str, int]:
    """Idempotent clean-up run at every EventBuilder start.

    1. Events backed only by GDELT rows (no article) are deleted with their documents,
       fields and corroborations: GDELT may only attach to article-backed events.
    2. After a ``rebuild_events`` run, events written by an older clusterer (e.g. by a
       server still running old code during the rebuild) are deleted too and the builder
       cursor is rewound to the rebuild's cursor so their documents are clustered again.
    3. Accepted GDELT decisions whose event is gone become rejected/no_article_backing.
    4. Stored type-mismatch verdicts are applied as unsupported (capped, off the map).
    Sensor and historical events are never touched (origin column).
    """
    out = {"gdelt_only": 0, "legacy": 0, "decisions": 0}
    ids = store.gdelt_only_event_ids()  # news events only (origin column)
    if ids:
        out["gdelt_only"] = store.delete_events(ids)
    marker = store.get_cursor(REBUILD_STATE)
    if marker and not marker.get("migrated"):
        # once per rebuild: news events left by an older clusterer (never sensor or
        # historical rows: legacy_event_ids/delete_events are restricted to origin news)
        legacy = store.legacy_event_ids(CLUSTER_VERSION)
        if legacy:
            out["legacy"] = store.delete_events(legacy)
            cur = store.get_cursor(CURSOR_NAME) or {}
            if marker.get("fetched_at_us") is not None and int(
                    cur.get("fetched_at_us") or 0) > int(marker["fetched_at_us"]):
                store.set_cursor(CURSOR_NAME, {k: marker[k] for k in
                                               ("fetched_at_us", "fetched_at") if k in marker})
        store.set_cursor(REBUILD_STATE, {**marker, "migrated": True})
    out["decisions"] = store.reject_orphan_gdelt_decisions(NO_ARTICLE_BACKING)
    n = store.type_mismatches_to_unsupported(unsupported_cap)
    if n:
        log.info("events migration: %d type-mismatch verdicts now unsupported", n)
    if any(out.values()):
        log.info("events migration: %s", out)
    return out


# rebuild replay reads (same columns as _DOC_SQL, for row_to_doc): one fetched_at day of
# articles at a time, then that day's GDELT rows streamed in fetched_at order
_REBUILD_ARTICLES_SQL = """
SELECT id, source, kind, origin_id, url, title, snippet, language,
       epoch_us(published_at), epoch_us(fetched_at), attrs, relevance, categories, places,
       place_name, place_precision, place_confidence, lat, lon, h3_3, h3_5, h3_7
FROM documents
WHERE fetched_at >= ? AND fetched_at < ? AND published_at >= ? AND relevance >= ?
  AND kind <> 'gdelt_event'
ORDER BY fetched_at, id
"""
_REBUILD_GDELT_SQL = """
SELECT id, source, kind, origin_id, url, title, snippet, language,
       epoch_us(published_at), epoch_us(fetched_at), attrs, relevance, categories, places,
       place_name, place_precision, place_confidence, lat, lon, h3_3, h3_5, h3_7
FROM documents
WHERE fetched_at >= ? AND fetched_at < ? AND published_at >= ? AND kind = 'gdelt_event'
ORDER BY fetched_at, id
"""
_FETCHED_RANGE_SQL = """
SELECT min(epoch_us(fetched_at)), max(epoch_us(fetched_at)) FROM documents
WHERE kind <> 'gdelt_event' AND relevance >= ?
"""


def _docs(rows: list[tuple]) -> list[ClusterDoc]:
    out = []
    for r in rows:
        try:
            d = row_to_doc(r)
        except (TypeError, ValueError):
            continue
        if d.fetched_at is not None:
            out.append(d)
    return out


def rebuild_events(store: SqliteStore, lake: Lake, config: EventsConfig | None = None, *,
                   cameo_category: Callable[[str, str], str | None] | None = None,
                   step: timedelta = timedelta(minutes=15),
                   metrics: Metrics | None = None, verifier: Any = None,
                   batch: int = 5000, lock_timeout: float = 600.0,
                   now: datetime | None = None) -> dict[str, Any]:
    """Re-cluster every lake document from scratch with the current rules, memory-bounded.

    Runs under the rebuild lock (``data/.event_rebuild.lock``): a live EventBuilder of any
    process skips its passes meanwhile and reloads its index afterwards. Documents are
    replayed one fetched_at day at a time (partition-pruned reads): the day's articles,
    and - only when the day has articles - its GDELT rows streamed in ``batch``-row reads,
    merged in fetched_at order and processed in ``step`` slices with the clock at the slice
    end, so time rules (GDELT staleness, windows, index pruning) behave as they did live.
    Only the clusterer's 72 h index and one slice stay in memory.

    Identity: an event re-created from the same primary article keeps its id (existing
    ids are carried over; new ones are derived from the primary article), so extraction,
    verification, imagery and benchmark labels stay linked. Events are stamped with a new
    rebuild generation; afterwards only *older-generation news* events are deleted - sensor
    and historical events are never touched. GDELT decisions are re-derived."""
    cfg = config or EventsConfig()
    with FileLock(rebuild_lock_path(store.path), lock_timeout):
        return _rebuild_locked(store, lake, cfg, cameo_category=cameo_category, step=step,
                               metrics=metrics, verifier=verifier, batch=batch, now=now)


def _rebuild_locked(store: SqliteStore, lake: Lake, cfg: EventsConfig, *,
                    cameo_category: Callable[[str, str], str | None] | None,
                    step: timedelta, metrics: Metrics | None, verifier: Any, batch: int,
                    now: datetime | None) -> dict[str, Any]:
    marker = store.get_cursor(REBUILD_STATE) or {}
    gen = int(marker.get("generation") or 0) + 1
    carry = store.news_primary_event_ids()
    carried = set(carry.values())
    store.clear_gdelt_decisions()
    sim = [utcnow()]
    builder = EventBuilder(store, lake, metrics or Metrics(), cfg,
                           cameo_category=cameo_category, clock=lambda: sim[0])
    builder._warm = True
    builder.clusterer.generation = gen
    builder.clusterer.id_override = carry
    builder.verdicts = {k: v["verdict"] for k, v in store.verifications().items()}
    totals = PassStats()
    peak = 0
    reset: set[str] = set()
    window = timedelta(hours=cfg.index_window_h)
    stale = timedelta(hours=cfg.gdelt_stale_h)
    rng = lake.query(_FETCHED_RANGE_SQL, [cfg.min_relevance])
    newest: datetime | None = None
    if rng and rng[0][0] is not None:
        day = _ts(rng[0][0]).replace(hour=0, minute=0, second=0, microsecond=0)
        last = _ts(rng[0][1])
        recent_ids: dict[str, datetime] = {}  # article id -> fetched (re-fetch dedupe)
        while day <= last:
            end = day + timedelta(days=1)
            articles = _docs(lake.query(
                _REBUILD_ARTICLES_SQL, [day, end, day - window, cfg.min_relevance],
                start=day - window, end=end))
            fresh = []
            for d in articles:  # first fetch of each article only (as live: _seen)
                if d.id not in recent_ids and d.published_at >= d.fetched_at - window:
                    recent_ids[d.id] = d.fetched_at
                    fresh.append(d)
            articles = fresh
            if articles:
                gdelt = (d for rows in lake.iter_query(
                    _REBUILD_GDELT_SQL, [day, end, day - stale], start=day - stale, end=end,
                    batch=batch) for d in _docs(rows))
                for slice_now, docs in _slices(articles, gdelt, step):
                    peak = max(peak, len(docs))
                    sim[0] = slice_now
                    newest = max(newest or slice_now, slice_now)
                    horizon = slice_now - window
                    docs = [d for d in docs if d.published_at >= horizon]
                    res = builder.clusterer.process(docs, slice_now)
                    again = [e for e in res.created if e in carried and e not in reset]
                    if again:  # an old event re-created under its id: re-derive its links
                        store.reset_event_links(again)
                        reset.update(again)
                    changed = [builder.clusterer.events[e].to_row() for e in res.changed
                               if e in builder.clusterer.events]
                    store.save_events(changed, res.documents, res.fields, res.decisions)
                    totals.documents += len(docs)
                    totals.clustered += res.clustered
                    totals.created += len(res.created)
                    totals.gdelt_accepted += res.gdelt_accepted
                    for k, v in res.gdelt_rejected.items():
                        totals.gdelt_rejected[k] = totals.gdelt_rejected.get(k, 0) + v
                    for k, v in res.skipped.items():
                        totals.skipped[k] = totals.skipped.get(k, 0) + v
            cutoff = end - window - timedelta(days=1)
            recent_ids = {k: v for k, v in recent_ids.items() if v >= cutoff}
            day = end
    deleted = store.delete_events(store.news_events_before_generation(gen))
    store.reject_orphan_gdelt_decisions(NO_ARTICLE_BACKING)
    verified = 0
    if verifier is not None:  # re-apply extraction results (relocations, caps)
        events = {r["id"]: EventState.from_row(r) for r in store.recent_events(_EPOCH)}
        builder.clusterer.reset()
        for ev in events.values():
            builder.clusterer._add_event(ev)
        ids = list(events)
        recs: dict[str, dict] = {}
        for i in range(0, len(ids), 500):
            recs.update(verifier.get_many(ids[i:i + 500]))
        verified = builder.apply_verdict_records(events, recs, now or utcnow(), force=True)
        builder.clusterer.reset()
    cursor: dict[str, Any] = {}
    if newest is not None:
        cursor = {"fetched_at_us": int((newest - _EPOCH) / timedelta(microseconds=1)),
                  "fetched_at": newest.isoformat()}
        store.set_cursor(CURSOR_NAME, cursor)
    store.set_cursor(REBUILD_STATE, {**cursor, "at": utcnow().isoformat(),
                                     "generation": gen})
    return {"documents": totals.documents, "clustered": totals.clustered,
            "created": totals.created, "gdelt_accepted": totals.gdelt_accepted,
            "gdelt_rejected": totals.gdelt_rejected, "skipped": totals.skipped,
            "generation": gen, "carried_over_ids": len(reset),
            "deleted_stale": deleted, "verdicts_reapplied": verified,
            "peak_slice_docs": peak}


def _slices(articles: list[ClusterDoc], gdelt: Iterator[ClusterDoc], step: timedelta
            ) -> Iterator[tuple[datetime, list[ClusterDoc]]]:
    """Merge two fetched_at-sorted streams and cut them into ``step`` slices; yields
    (clock = newest fetched_at in the slice, docs). Holds one slice at a time."""
    ai = 0
    pending = next(gdelt, None)
    while ai < len(articles) or pending is not None:
        first = min(d.fetched_at for d in (
            articles[ai] if ai < len(articles) else None, pending) if d is not None)
        end = first + step
        out: list[ClusterDoc] = []
        while ai < len(articles) and articles[ai].fetched_at < end:
            out.append(articles[ai])
            ai += 1
        while pending is not None and pending.fetched_at < end:
            out.append(pending)
            pending = next(gdelt, None)
        yield max(d.fetched_at for d in out), out


def wire(runtime: Runtime) -> EventBuilder:
    """Create the runtime's EventBuilder (``runtime.event_builder``) and return it; the
    caller schedules ``builder.run()`` as a background task."""
    builder = EventBuilder.from_settings(runtime.settings, runtime.store, runtime.lake,
                                         runtime.metrics)
    try:  # article-extraction verdicts (optional stage)
        from osint.pipeline.extract.job import get_store

        builder.verifier = get_store(runtime)
    except Exception:
        log.exception("extraction store unavailable; verdicts not merged")
    runtime.event_builder = builder
    return builder

