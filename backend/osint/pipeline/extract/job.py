"""ExtractionJob: one LLM extraction per event, on its best article, then a verdict.

Every ``interval_s`` it picks events by priority (multi-source first, then confidence, then
most recently updated), skips events already extracted unless their primary document
changed, respects the hourly/daily call caps and runs ``concurrency`` calls at a time.
Results go to ExtractionStore (data_dir/extraction.sqlite); the article text is only ever
held in memory.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from osint.llm import CallRateLimiter, LLMBackend, LLMError, make_backend
from osint.pipeline.extract.config import ExtractionConfig
from osint.pipeline.extract.fetch import get_article_text
from osint.pipeline.extract.schema import EXTRACTION_SCHEMA, INSTRUCTION
from osint.pipeline.extract.store import ExtractionStore
from osint.pipeline.extract.verdict import Geocoder, gazetteer_geocoder, judge, normalise

log = logging.getLogger(__name__)
ROLE_RANK = {"primary": 0, "mention": 1, "gdelt": 2}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def best_document(docs: list[dict]) -> dict | None:
    """The event's best article: primary role first, then earliest published, http only."""
    usable = [d for d in docs if (d.get("url") or "").startswith(("http://", "https://"))]
    if not usable:
        return None
    return min(usable, key=lambda d: (ROLE_RANK.get(d.get("role"), 3),
                                      d.get("published_at") or "", d.get("doc_id") or ""))


def priority_key(e: dict) -> tuple:
    """Sort key (ascending = first): multi-source, then confidence, then recency."""
    return (-(1 if (e.get("source_diversity") or 0) >= 2 else 0),
            -(e.get("confidence") or 0.0), _neg_ts(e.get("updated_at")))


def _neg_ts(ts: str | None) -> float:
    dt = _parse(ts)
    return -dt.timestamp() if dt else 0.0


@dataclass
class WorkItem:
    event: dict
    doc: dict
    attempts: int = 0


class ExtractionJob:
    def __init__(self, event_store, store: ExtractionStore, cfg: ExtractionConfig,
                 backend: LLMBackend, *, limiter: CallRateLimiter | None = None,
                 geocoder: Geocoder | None = None, alterlab_key: str = "",
                 snippet_lookup=None, http_factory=None, clock=time.time) -> None:
        self.event_store, self.store, self.cfg, self.backend = event_store, store, cfg, backend
        self.clock = clock
        self.limiter = limiter or CallRateLimiter(
            cfg.max_calls_per_hour, cfg.max_calls_per_day, clock=clock,
            history=store.call_times_since(clock() - 86400))
        self.geocoder = geocoder
        self.alterlab_key = alterlab_key
        self.snippet_lookup = snippet_lookup
        self.http_factory = http_factory or (lambda: httpx.AsyncClient(
            follow_redirects=True, timeout=cfg.fetch.timeout_s))
        self._http: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(max(1, cfg.concurrency))
        self.passes = 0
        self.queue_size = 0
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.capped = False
        self.errors: deque[dict] = deque(maxlen=10)

    # --------------------------------------------------------------- selection

    def select(self, now: datetime | None = None) -> list[WorkItem]:
        now = now or utcnow()
        cfg = self.cfg
        events = self.event_store.list_events(
            since=now - timedelta(hours=cfg.event_age_h), status=",".join(cfg.statuses),
            min_confidence=cfg.min_confidence or None, limit=1000, slim=False)
        events = [e for e in events if (e.get("source_diversity") or 0) >= cfg.min_sources]
        done = self.store.get_many([e["id"] for e in events])
        retry = timedelta(minutes=cfg.retry_after_min)
        due: list[tuple[dict, dict | None]] = []
        for e in events:
            rec = done.get(e["id"])
            if rec is None:
                due.append((e, None))
            elif rec["status"] == "done":
                if rec.get("event_version") != e.get("version"):
                    due.append((e, rec))  # changed: re-extract only if primary doc changed
            elif rec["attempts"] < cfg.max_attempts and (_parse(rec["updated_at"]) or now) \
                    + retry <= now:
                due.append((e, rec))
        due.sort(key=lambda pair: priority_key(pair[0]))
        items: list[WorkItem] = []
        for e, rec in due:
            if len(items) >= cfg.max_per_run:
                break
            full = self.event_store.get_event(e["id"])
            doc = best_document((full or {}).get("documents") or [])
            if doc is None:
                continue  # sensor-only / no article: nothing to extract
            if rec is not None and rec["status"] == "done" and rec["doc_id"] == doc["doc_id"]:
                self.store.save(  # same article: keep result, remember the version
                    e["id"], **_keep(rec), event_version=e.get("version"), now=now)
                continue
            attempts = rec["attempts"] if rec is not None and rec["status"] == "error" else 0
            items.append(WorkItem(e, doc, attempts))
        self.queue_size = len(due)
        return items

    # --------------------------------------------------------------- one event

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = self.http_factory()
        return self._http

    async def process(self, item: WorkItem) -> dict | None:
        ev, doc = item.event, item.doc
        snippet = ""
        if self.snippet_lookup is not None:
            try:
                snippet = await asyncio.to_thread(self.snippet_lookup, doc) or ""
            except Exception:
                log.debug("snippet lookup failed for %s", doc.get("doc_id"), exc_info=True)
        art = await get_article_text(
            self._client(), doc["url"], title=doc.get("title") or "", snippet=snippet,
            cfg=self.cfg.fetch, alterlab_key=self.alterlab_key)
        if not self.limiter.try_acquire():
            self.capped = True
            return None
        text = f"Headline: {doc.get('title') or ''}\nURL: {doc['url']}\n\n{art.text}"
        now = utcnow()
        base = {"doc_id": doc["doc_id"], "url": doc["url"], "event_version": ev.get("version"),
                "text_source": art.source}
        try:
            res = await self.backend.extract(INSTRUCTION, text, EXTRACTION_SCHEMA)
        except (LLMError, NotImplementedError, OSError) as exc:
            err = f"{type(exc).__name__}: {exc}"
            self.store.log_call(self.clock(), event_id=ev["id"], ok=False, error=err)
            self.store.save(ev["id"], **base, status="error", now=now, error=err,
                            attempts=item.attempts + 1)
            self.errors.appendleft({"at": now.isoformat(timespec="seconds"),
                                    "event_id": ev["id"], "error": err[:300]})
            return None
        self.store.log_call(self.clock(), event_id=ev["id"], ok=True, cost_usd=res.cost_usd,
                            latency_s=res.latency_s, model=res.model)
        ext = normalise(res.data, text)
        v = await asyncio.to_thread(judge, ev, ext, geocode=self.geocoder,
                                    mismatch_km=self.cfg.mismatch_km)
        ext["text_source"] = art.source
        if art.error:
            ext["fetch_error"] = art.error[:300]
        self.store.save(ev["id"], **base, status="done", now=now,
                        is_real_event=ext["is_real_event"], verdict=v.verdict,
                        reasons=v.reasons, result=ext, geo=v.geo, distance_km=v.distance_km,
                        cost_usd=res.cost_usd, latency_s=round(res.latency_s, 2),
                        model=res.model, attempts=item.attempts + 1)
        return {"event_id": ev["id"], "title": ev.get("title"), "verdict": v.verdict,
                "reasons": v.reasons, "extraction": ext, "cost_usd": res.cost_usd,
                "latency_s": res.latency_s, "text_source": art.source, "url": doc["url"]}

    async def run_once(self) -> list[dict]:
        self.capped = False
        counts = self.limiter.counts()
        if counts["hour"] >= self.cfg.max_calls_per_hour or \
                counts["day"] >= self.cfg.max_calls_per_day:
            self.capped = True
            return []
        batch = await asyncio.to_thread(self.select)

        async def one(item: WorkItem) -> dict | None:
            async with self._sem:
                if self.capped:
                    return None
                try:
                    return await self.process(item)
                except Exception as exc:
                    log.exception("extraction failed for %s", item.event["id"])
                    self.errors.appendleft({"at": utcnow().isoformat(timespec="seconds"),
                                            "event_id": item.event["id"],
                                            "error": f"{type(exc).__name__}: {exc}"[:300]})
                    return None

        results = await asyncio.gather(*(one(i) for i in batch))
        self.last_run_at = utcnow().isoformat(timespec="seconds")
        return [r for r in results if r]

    async def run(self) -> None:
        if not self.cfg.enabled:
            log.info("extraction disabled (config/extraction.yaml)")
            return
        await asyncio.sleep(self.cfg.initial_delay_s)  # let boot/clustering settle first
        try:
            while True:
                try:
                    await self.run_once()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("extraction pass failed")
                self.passes += 1
                await asyncio.sleep(self.cfg.interval_s)
        finally:
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    # --------------------------------------------------------------- status

    def status(self) -> dict:
        now = self.clock()
        day_start = datetime.fromtimestamp(now, UTC).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        counts = self.limiter.counts()
        errors = list(self.errors) or self.store.last_errors()
        return {
            "enabled": self.cfg.enabled, "backend": getattr(self.backend, "name", "?"),
            "model": self.cfg.model,
            "calls": {"hour": counts["hour"], "day": counts["day"],
                      "today_utc": len(self.store.call_times_since(day_start)),
                      "max_per_hour": self.cfg.max_calls_per_hour,
                      "max_per_day": self.cfg.max_calls_per_day},
            "cost_usd": {"today_utc": round(self.store.cost_since(day_start), 6),
                         "last_24h": round(self.store.cost_since(now - 86400), 6),
                         "total": round(self.store.cost_since(0), 6)},
            "queue_size": self.queue_size, "capped": self.capped, "passes": self.passes,
            "last_run_at": self.last_run_at, "last_error": self.last_error,
            "last_errors": errors[:5], "verdicts": self.store.verdict_counts(),
        }


def _keep(rec: dict) -> dict:
    """Arguments re-saving an existing extraction row unchanged (cost not re-added)."""
    return {"doc_id": rec["doc_id"], "url": rec["url"], "status": rec["status"],
            "is_real_event": rec["is_real_event"], "verdict": rec["verdict"],
            "reasons": rec["reasons"], "result": rec["result"], "geo": rec["geo"],
            "distance_km": rec["distance_km"], "text_source": rec["text_source"],
            "cost_usd": 0.0, "latency_s": rec["latency_s"], "model": rec["model"],
            "error": rec["error"], "attempts": rec["attempts"]}


# ------------------------------------------------------------------- wiring


def get_store(runtime) -> ExtractionStore:
    """The runtime's extraction store, created on first use (shared by job and API)."""
    store = getattr(runtime, "extraction_store", None)
    if store is None:
        store = ExtractionStore(runtime.settings.data_dir / "extraction.sqlite")
        runtime.extraction_store = store
    return store


def lake_snippet_lookup(lake):
    """doc -> snippet from the documents lake (partition window around published_at)."""
    def lookup(doc: dict) -> str:
        pub = _parse(doc.get("published_at"))
        if pub is None:
            return ""
        rows = lake.query("SELECT snippet FROM documents WHERE id = ? LIMIT 1",
                          [doc["doc_id"]], start=pub - timedelta(hours=1),
                          end=pub + timedelta(hours=1))
        return (rows[0][0] or "") if rows else ""
    return lookup


def build_job(runtime) -> ExtractionJob:
    settings = runtime.settings
    cfg = ExtractionConfig.load(settings.config_dir / "extraction.yaml")
    kwargs = {"model": cfg.model}
    if cfg.backend == "claude_cli":
        kwargs.update(timeout_s=cfg.timeout_s, concurrency=cfg.concurrency,
                      cli_path=cfg.cli_path, cwd=settings.data_dir / "extraction_cwd")
    backend = make_backend(cfg.backend, **kwargs)
    gz = getattr(getattr(runtime, "triage", None), "gazetteer", None)
    return ExtractionJob(
        runtime.store, get_store(runtime), cfg, backend,
        geocoder=gazetteer_geocoder(gz) if gz is not None else None,
        alterlab_key=settings.alterlab_api_key,
        snippet_lookup=lake_snippet_lookup(runtime.lake))


def start(runtime) -> asyncio.Task:
    """Create the job (as runtime.extraction_job) and start its loop as a task."""
    job = build_job(runtime)
    runtime.extraction_job = job
    return asyncio.create_task(job.run(), name="extraction")
