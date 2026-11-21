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
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import h3
import yaml

from osint.config import ROOT
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.pipeline.cluster import (
    CLUSTER_VERSION,
    ClusterDoc,
    Clusterer,
    Embedder,
    EventState,
)
from osint.pipeline.corroborate import CorroborationConfig, Corroborator
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

if TYPE_CHECKING:
    from osint.runtime import Runtime

log = logging.getLogger(__name__)

DEFAULT_CONFIG = ROOT / "config" / "events.yaml"
CURSOR_NAME = "event_builder"
REBUILD_STATE = "event_rebuild"
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
        migrate_events(self.store)
        now = self.clock()
        since = now - timedelta(hours=self.cfg.index_window_h)
        sensor_ids = self.store.sensor_event_ids()  # owned by pipeline.sensor_events
        n = self.clusterer.load(r for r in self.store.recent_events(since)
                                if r["id"] not in sensor_ids)
        if self.corroborator is not None:
            self.corroborator.warm_start(now)
        self.verdicts = {k: v["verdict"] for k, v in self.store.verifications().items()}
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
        """One synchronous pass (call from a worker thread)."""
        t0 = time.perf_counter()
        if not self._warm:
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
        """Merge new extraction verdicts (``verifier.verdicts_since`` cursor) into events:

        * unsupported: confidence capped (and hidden from the map default view)
        * mismatch: confidence penalised; if the extraction geocoded a city/point place, the
          event moves there (old place kept as field ``location_before_extraction``)
        * supported: confidence bonus; verbatim quotes become event_fields
        Idempotent: a verdict whose fingerprint is unchanged is not re-applied."""
        since = self._verdict_cursor or now - timedelta(hours=self.cfg.index_window_h)
        changed = self.verifier.verdicts_since(since - timedelta(minutes=1))
        self._verdict_cursor = now
        ids = [e for e in changed if e in self.clusterer.events]
        if not ids:
            return 0
        recs = self.verifier.get_many(ids)
        known = self.store.verifications(ids)
        rows, fields, verifs = [], [], []
        for eid in ids:
            rec = recs.get(eid) or {}
            verdict = rec.get("verdict")
            if rec.get("status") != "done" or verdict not in (
                    "supported", "unsupported", "mismatch"):
                continue
            quotes = [q for q in rec.get("quotes") or [] if isinstance(q, dict)]
            geo = rec.get("geo") or None
            fp = json.dumps([verdict, rec.get("reasons"), geo, quotes], sort_keys=True,
                            default=str)
            fp = hashlib.sha1(fp.encode("utf-8")).hexdigest()[:16]
            self.verdicts[eid] = verdict
            if (known.get(eid) or {}).get("fingerprint") == fp:
                continue
            ev = self.clusterer.events[eid]
            doc_id = rec.get("doc_id") or "extraction"
            if verdict == "mismatch" and geo and geo.get("precision") in ("city", "point") \
                    and geo.get("lat") is not None and geo.get("lon") is not None:
                fields += self._relocate(ev, rec, geo, doc_id)
            if verdict == "supported":
                for q in quotes:
                    if q.get("verbatim") and q.get("quote"):
                        fields.append({"event_id": eid, "field": str(q.get("field") or "quote"),
                                       "value": str(q["quote"])[:300], "doc_id": doc_id,
                                       "quote": str(q["quote"])[:300]})
            ev.status, ev.confidence = self.clusterer.scorer(ev, now)
            ev.updated_at = now
            rows.append(ev.to_row())
            verifs.append({"event_id": eid, "verdict": verdict,
                           "reasons": rec.get("reasons") or [], "quotes": quotes,
                           "fingerprint": fp})
            self.metrics.inc("event_verdicts_applied_total", verdict=verdict)
        if verifs:
            self.store.save_events(rows, fields=fields)
            self.store.save_verifications(verifs)
        return len(verifs)

    def _relocate(self, ev: EventState, rec: dict, geo: dict, doc_id: str) -> list[dict]:
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
        return [{"event_id": ev.id, "field": "location_before_extraction", "value": old[:300],
                 "doc_id": doc_id, "quote": "moved: extraction place mismatch"},
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


def migrate_events(store: SqliteStore) -> dict[str, int]:
    """Idempotent clean-up run at every EventBuilder start.

    1. Events backed only by GDELT rows (no article) are deleted with their documents,
       fields and corroborations: GDELT may only attach to article-backed events.
    2. After a ``rebuild_events`` run, events written by an older clusterer (e.g. by a
       server still running old code during the rebuild) are deleted too and the builder
       cursor is rewound to the rebuild's cursor so their documents are clustered again.
    3. Accepted GDELT decisions whose event is gone become rejected/no_article_backing.
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
    if any(out.values()):
        log.info("events migration: %s", out)
    return out


def rebuild_events(store: SqliteStore, lake: Lake, config: EventsConfig | None = None, *,
                   cameo_category: Callable[[str, str], str | None] | None = None,
                   step: timedelta = timedelta(minutes=15),
                   metrics: Metrics | None = None) -> dict[str, Any]:
    """Re-cluster every lake document from scratch with the current rules.

    Clears news events (and their documents/fields/corroborations) and all GDELT decisions,
    then replays the documents in fetched_at order in ``step`` slices, each with the clock
    set to the slice end, so time-dependent rules (GDELT staleness, windows, index pruning)
    behave as they did live. GDELT decisions are re-derived from the same lake rows. Sets
    the builder cursor and a rebuild marker (see ``migrate_events``)."""
    cfg = config or EventsConfig()
    store.clear_news_events()
    rows = lake.query(_DOC_SQL, [_EPOCH, _EPOCH, cfg.min_relevance, 2**62])
    docs = []
    for r in rows:
        try:
            docs.append(row_to_doc(r))
        except (TypeError, ValueError):
            continue
    docs = [d for d in docs if d.fetched_at is not None]
    sim = [docs[0].fetched_at if docs else utcnow()]
    builder = EventBuilder(store, lake, metrics or Metrics(), cfg,
                           cameo_category=cameo_category, clock=lambda: sim[0])
    builder._warm = True
    totals = PassStats()
    i = 0
    while i < len(docs):
        end = docs[i].fetched_at + step
        j = i
        while j < len(docs) and docs[j].fetched_at < end:
            j += 1
        now = docs[j - 1].fetched_at
        sim[0] = now
        horizon = now - timedelta(hours=cfg.index_window_h)
        batch = [d for d in docs[i:j] if d.published_at >= horizon]
        res = builder.clusterer.process(batch, now)
        changed = [builder.clusterer.events[e].to_row() for e in res.changed
                   if e in builder.clusterer.events]
        store.save_events(changed, res.documents, res.fields, res.decisions)
        totals.documents += len(batch)
        totals.clustered += res.clustered
        totals.created += len(res.created)
        totals.gdelt_accepted += res.gdelt_accepted
        for k, v in res.gdelt_rejected.items():
            totals.gdelt_rejected[k] = totals.gdelt_rejected.get(k, 0) + v
        for k, v in res.skipped.items():
            totals.skipped[k] = totals.skipped.get(k, 0) + v
        i = j
    cursor: dict[str, Any] = {}
    if docs:
        newest = docs[-1].fetched_at
        cursor = {"fetched_at_us": int((newest - _EPOCH) / timedelta(microseconds=1)),
                  "fetched_at": newest.isoformat()}
        store.set_cursor(CURSOR_NAME, cursor)
    store.set_cursor(REBUILD_STATE, {**cursor, "at": utcnow().isoformat()})
    return {"documents": totals.documents, "clustered": totals.clustered,
            "created": totals.created, "gdelt_accepted": totals.gdelt_accepted,
            "gdelt_rejected": totals.gdelt_rejected, "skipped": totals.skipped}


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


def _unused(*_: object) -> None:  # pragma: no cover - keeps math import explicit for ruff
    math.isfinite(0.0)
