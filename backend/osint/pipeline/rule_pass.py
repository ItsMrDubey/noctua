"""Deterministic extraction for a set of events (deterministic extraction spec §2.3, §2.4,
§2.6): run by the EventBuilder after each pass for the events it touched, and by the
country migration for every event.

Per event: actors are re-resolved (``osint.enrich.actors.resolve_actors``) and the derived
columns written (actor_countries, foreign_involved, attributed); for news and sensor events
the rule extractors (``osint.enrich.rules.RuleSet``) replace the event's ``source='rule'``
fields (curated ACLED / UCDP rows keep their own values). Inputs: the event row (incl. its
category, which gates the generic "<demonym> strike" phrases), its documents (titles from
event_documents, snippets from the pass or the lake), the LLM extraction when it is
``supported`` (its actor names) and, with an embedder, document vectors for the headline
pick. CPU only apart from the snippet read; an event whose actor resolution fails is
skipped and logged, a failing rule extractor only for that event.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from osint.core.metrics import Metrics
from osint.enrich.actors import ActorLexicon, derive, resolve_actors
from osint.enrich.rules import RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.pipeline.cluster import Embedder, NullEmbedder, embed_many, embed_text

log = logging.getLogger(__name__)
CHUNK = 500


@dataclass
class RulePassStats:
    events: int = 0
    fields: int = 0
    errors: int = 0
    attributed: int = 0
    foreign: int = 0
    seconds: float = 0.0


def _llm_actors(rec: Mapping[str, Any] | None) -> list[dict]:
    """Actor dicts of a supported LLM extraction (spec §2.3 step 4), else []."""
    if not rec or rec.get("status") != "done" or rec.get("verdict") != "supported":
        return []
    return [a for a in (rec.get("result") or {}).get("actors") or [] if isinstance(a, dict)]


class RulePass:
    def __init__(self, store: Any, *, lexicon: ActorLexicon, rules: RuleSet, lake: Any = None,
                 extractions: Any = None, embedder: Embedder | None = None,
                 metrics: Metrics | None = None) -> None:
        self.store, self.lexicon, self.rules = store, lexicon, rules
        self.lake, self.extractions = lake, extractions
        self.embedder = None if embedder is None or isinstance(embedder, NullEmbedder) \
            else embedder
        self.metrics = metrics or Metrics()

    def run(self, event_ids: Iterable[str], *, states: Mapping[str, Any] | None = None,
            snippets: Mapping[str, str] | None = None, fields: bool = True,
            lake_snippets: bool = True, write: bool = True,
            country_override: Mapping[str, str | None] | None = None) -> RulePassStats:
        """Process ``event_ids`` (missing ids are skipped). ``states``: the clusterer's
        in-memory events (their ``actors`` are updated so its next upsert keeps them;
        ``vector`` is the centroid for the headline pick); an id without a state (evicted
        from memory) is still processed from the store. ``snippets``: doc id -> snippet
        already at hand (the pass's documents); others come from the lake unless
        ``lake_snippets`` is false. ``write=False`` computes the statistics only;
        ``country_override`` replaces events.country for that (the migration's dry run)."""
        t0 = time.perf_counter()
        stats = RulePassStats()
        ids = sorted(set(event_ids))
        for i in range(0, len(ids), CHUNK):
            self._chunk(ids[i:i + CHUNK], stats, states or {}, dict(snippets or {}), fields,
                        lake_snippets, write, country_override or {})
        stats.seconds = time.perf_counter() - t0
        self.metrics.inc("rule_pass_events_total", stats.events)
        self.metrics.set("rule_pass_seconds", round(stats.seconds, 4))
        return stats

    def _chunk(self, ids: list[str], stats: RulePassStats, states: Mapping[str, Any],
               snips: dict[str, str], fields: bool, lake_snippets: bool, write: bool,
               override: Mapping[str, str | None]) -> None:
        evs = self.store.events_for_rules(ids)
        docs = self.store.rule_documents(ids)
        if lake_snippets and self.lake is not None:
            need = [d for eid in ids if evs.get(eid, {}).get("origin") != "historical"
                    for d in docs.get(eid, []) if d["doc_id"] not in snips]
            if need:
                snips.update(self._lake_snippets(need))
        recs = self._extractions(ids)
        derived: list[dict] = []
        rule_rows: list[dict] = []
        rule_ids: list[str] = []
        for eid in ids:
            ev = evs.get(eid)
            if ev is None:
                continue  # merged away or deleted meanwhile
            if eid in override:
                ev = {**ev, "country": override[eid]}
            st = states.get(eid)
            edocs = self._docs(ev, docs.get(eid, []), snips, st)
            try:
                actors = resolve_actors(ev, edocs, self.lexicon, _llm_actors(recs.get(eid)))
            except Exception:
                stats.errors += 1
                self.metrics.inc("rule_extraction_errors_total", extractor="actors")
                log.exception("actor resolution failed for event %s", eid)
                continue
            d = derive(actors, ev.get("country"))
            stats.events += 1
            stats.attributed += int(d["attributed"])
            stats.foreign += int(d["foreign_involved"])
            derived.append({"id": eid, "actors": actors, **d})
            if st is not None and write:
                st.actors = actors
            if fields and ev.get("origin") != "historical":
                values, failed = self.rules.run(
                    {"id": eid, "vector": getattr(st, "vector", None)}, edocs)
                stats.errors += len(failed)
                for name in failed:
                    self.metrics.inc("rule_extraction_errors_total", extractor=name)
                rule_ids.append(eid)
                rule_rows += [v.row(eid) for v in values]
        stats.fields += len(rule_rows)
        if write:
            if derived:
                self.store.set_event_derived(derived)
            if rule_ids:
                self.store.replace_rule_fields(rule_ids, rule_rows)

    def _docs(self, ev: Mapping[str, Any], rows: list[dict], snips: Mapping[str, str],
              st: Any) -> list[ExtractDoc]:
        historical = ev.get("origin") == "historical"
        vecs = self._vectors(rows, snips) if getattr(st, "vector", None) is not None else {}
        out = []
        for r in rows:
            snippet = snips.get(r["doc_id"]) or (str(ev.get("summary") or "")
                                                if historical else "")
            out.append(ExtractDoc(
                doc_id=r["doc_id"], title=r["title"] or "", snippet=snippet,
                url=r["url"] or "", source=r["source"] or "",
                published_at=r["published_at"] or "", role=r["role"] or "mention",
                language=r["language"] or "", vector=vecs.get(r["doc_id"])))
        return out

    def _vectors(self, rows: list[dict], snips: Mapping[str, str]) -> dict[str, Any]:
        """Document vectors (embedding cache keyed by doc id, as the clusterer wrote them)."""
        if self.embedder is None or not rows:
            return {}
        try:
            vecs = embed_many(self.embedder, [
                (r["doc_id"], embed_text(r["title"] or "", snips.get(r["doc_id"], "")))
                for r in rows])
        except Exception:
            log.exception("document vectors for the headline pick failed")
            return {}
        return {r["doc_id"]: v for r, v in zip(rows, vecs, strict=True) if v is not None}

    def _lake_snippets(self, docs: list[dict]) -> dict[str, str]:
        from osint.translate.job import lake_snippets  # local: optional stage

        try:
            return lake_snippets(self.lake, docs)
        except Exception:  # titles alone still extract
            log.exception("snippet lookup for %d documents failed", len(docs))
            return {}

    def _extractions(self, ids: list[str]) -> dict[str, dict]:
        if self.extractions is None:
            return {}
        try:
            return self.extractions.get_many(list(ids))
        except Exception:
            log.exception("extraction lookup failed; LLM actors skipped")
            return {}
