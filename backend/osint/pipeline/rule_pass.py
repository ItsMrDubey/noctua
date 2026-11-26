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
pick.

Bounded cost per event: the actor resolution and the extractors see at most the event's
primary document(s), the document of its stored rule headline and its newest
``max_docs_per_event`` documents (config/extract_rules.yaml, default 50), so the casualty
median, the weapon list and the headline pick are computed over that capped set. Lake
snippets are kept in a bounded LRU (doc id -> snippet, found or not), so a document already
read is not queried again in later passes. CPU only apart from the snippet read. An event
that fails anywhere (its documents, actor resolution, derivation, row building) is skipped
and logged and the rest of the chunk proceeds; a failing rule extractor only drops that
extractor for that event. A chunk's derived columns and rule fields are written in one
transaction, and the in-memory states' ``actors`` follow only after that write.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from osint.core.metrics import Metrics
from osint.enrich.actors import ActorLexicon, derive, resolve_actors
from osint.enrich.rules import MAX_DOCS_PER_EVENT, RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.pipeline.cluster import Embedder, NullEmbedder, embed_many, embed_text

log = logging.getLogger(__name__)
CHUNK = 500
SNIPPET_CACHE = 20_000  # doc ids whose lake snippet (or its absence) is remembered


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


def cap_documents(rows: list[dict], limit: int, keep: Iterable[str] = ()) -> list[dict]:
    """``rows`` (oldest first) reduced to the newest ``limit`` non-primary documents plus
    the primary document(s) and the ids in ``keep`` (wherever they fall), in their original
    order; unchanged when within the limit. The newest window does not depend on ``keep``,
    so pinning the stored headline does not pull an older document in."""
    if len(rows) <= limit:
        return rows
    others = [r["doc_id"] for r in rows if r.get("role") != "primary"]
    chosen = set(others[len(others) - max(0, limit):]) | set(keep) | {
        r["doc_id"] for r in rows if r.get("role") == "primary"}
    return [r for r in rows if r["doc_id"] in chosen]


class RulePass:
    def __init__(self, store: Any, *, lexicon: ActorLexicon, rules: RuleSet, lake: Any = None,
                 extractions: Any = None, embedder: Embedder | None = None,
                 metrics: Metrics | None = None, max_docs_per_event: int | None = None,
                 snippet_cache_size: int = SNIPPET_CACHE) -> None:
        self.store, self.lexicon, self.rules = store, lexicon, rules
        self.lake, self.extractions = lake, extractions
        self.embedder = None if embedder is None or isinstance(embedder, NullEmbedder) \
            else embedder
        self.metrics = metrics or Metrics()
        self.max_docs_per_event = int(
            max_docs_per_event if max_docs_per_event is not None
            else getattr(rules, "max_docs_per_event", MAX_DOCS_PER_EVENT))
        self.snippet_cache_size = max(0, int(snippet_cache_size))
        self._snippets: OrderedDict[str, str] = OrderedDict()  # LRU: doc id -> snippet

    def run(self, event_ids: Iterable[str], *, states: Mapping[str, Any] | None = None,
            snippets: Mapping[str, str] | None = None, fields: bool = True,
            lake_snippets: bool = True, write: bool = True,
            country_override: Mapping[str, str | None] | None = None) -> RulePassStats:
        """Process ``event_ids`` (missing ids are skipped). ``states``: the clusterer's
        in-memory events (their ``actors`` are updated so its next upsert keeps them;
        ``vector`` is the centroid for the headline pick); an id without a state (evicted
        from memory) is still processed from the store. ``snippets``: doc id -> snippet
        already at hand (the pass's documents); others come from the snippet cache or the
        lake unless ``lake_snippets`` is false. ``write=False`` computes the statistics only;
        ``country_override`` replaces events.country for that (the migration's dry run)."""
        t0 = time.perf_counter()
        stats = RulePassStats()
        ids = sorted(set(event_ids))
        for i in range(0, len(ids), CHUNK):
            self._chunk(ids[i:i + CHUNK], stats, states or {}, snippets or {}, fields,
                        lake_snippets, write, country_override or {})
        stats.seconds = time.perf_counter() - t0
        self.metrics.inc("rule_pass_events_total", stats.events)
        self.metrics.set("rule_pass_seconds", round(stats.seconds, 4))
        return stats

    def _chunk(self, ids: list[str], stats: RulePassStats, states: Mapping[str, Any],
               given: Mapping[str, str], fields: bool, lake_snippets: bool, write: bool,
               override: Mapping[str, str | None]) -> None:
        evs = self.store.events_for_rules(ids)
        docs = self._capped(ids, self.store.rule_documents(ids))
        snips = self._snippets_for(evs, docs, given, lake_snippets)
        recs = self._extractions(ids)
        derived: list[dict] = []
        rule_rows: list[dict] = []
        rule_ids: list[str] = []
        updates: list[tuple[Any, list[dict]]] = []
        for eid in ids:
            ev = evs.get(eid)
            if ev is None:
                continue  # merged away or deleted meanwhile
            stage = "event"
            try:  # one poison event is skipped, the rest of the chunk proceeds
                if eid in override:
                    ev = {**ev, "country": override[eid]}
                st = states.get(eid)
                edocs = self._docs(ev, docs.get(eid, []), snips, st)
                stage = "actors"
                actors = resolve_actors(ev, edocs, self.lexicon, _llm_actors(recs.get(eid)))
                stage = "event"
                d = derive(actors, ev.get("country"))
                values, failed = None, []
                if fields and ev.get("origin") != "historical":
                    values, failed = self.rules.run(
                        {"id": eid, "vector": getattr(st, "vector", None)}, edocs)
                rows = [v.row(eid) for v in values or ()]
            except Exception:
                stats.errors += 1
                self.metrics.inc("rule_extraction_errors_total", extractor=stage)
                log.exception("rule extraction (%s) failed for event %s", stage, eid)
                continue
            stats.events += 1
            stats.attributed += int(d["attributed"])
            stats.foreign += int(d["foreign_involved"])
            derived.append({"id": eid, "actors": actors, **d})
            if st is not None:
                updates.append((st, actors))
            stats.errors += len(failed)
            for name in failed:
                self.metrics.inc("rule_extraction_errors_total", extractor=name)
            if values is not None:
                rule_ids.append(eid)
                rule_rows += rows
        stats.fields += len(rule_rows)
        if write and (derived or rule_ids):
            self.store.write_rule_results(derived, rule_ids, rule_rows)  # one transaction
            for st, actors in updates:  # memory follows only a committed write
                st.actors = actors

    def _capped(self, ids: list[str], docs: dict[str, list[dict]]) -> dict[str, list[dict]]:
        """Each event's documents capped (``cap_documents``); the stored rule headline's
        document stays a candidate so the pick does not drift with the window."""
        big = [eid for eid in ids if len(docs.get(eid, ())) > self.max_docs_per_event]
        if not big:
            return docs
        try:
            heads = {eid: [r["doc_id"] for r in rows if r["field"] == "headline"]
                     for eid, rows in self.store.rule_fields(big).items()}
        except Exception:
            log.exception("stored headline lookup failed; newest documents only")
            heads = {}
        return {**docs, **{eid: cap_documents(docs[eid], self.max_docs_per_event,
                                              heads.get(eid, ())) for eid in big}}

    def _snippets_for(self, evs: Mapping[str, dict], docs: Mapping[str, list[dict]],
                      given: Mapping[str, str], lake_snippets: bool) -> dict[str, str]:
        """doc id -> snippet for the (capped) documents of non-historical events: the pass's
        own snippets, then the LRU cache, then one lake lookup for the rest (remembered,
        found or not; a failed lookup is not remembered)."""
        out: dict[str, str] = {}
        need: list[dict] = []
        cache = self._snippets
        for eid, rows in docs.items():
            if evs.get(eid, {}).get("origin") == "historical":
                continue
            for d in rows:
                did = d["doc_id"]
                if did in out:
                    continue
                if did in given:
                    out[did] = given[did]
                    self._remember(did, given[did])
                elif did in cache:
                    cache.move_to_end(did)
                    out[did] = cache[did]
                elif lake_snippets and self.lake is not None:
                    out[did] = ""  # placeholder until the lookup (dedupes `need`)
                    need.append(d)
        if need:
            found = self._lake_snippets(need)
            for d in need:
                did = d["doc_id"]
                out[did] = (found or {}).get(did, "")
                if found is not None:  # a failed lookup is retried next pass
                    self._remember(did, out[did])
        return out

    def _remember(self, doc_id: str, snippet: str) -> None:
        if not self.snippet_cache_size:
            return
        self._snippets[doc_id] = snippet
        self._snippets.move_to_end(doc_id)
        while len(self._snippets) > self.snippet_cache_size:
            self._snippets.popitem(last=False)

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

    def _lake_snippets(self, docs: list[dict]) -> dict[str, str] | None:
        """{doc id: snippet} from the lake; None when the lookup failed."""
        from osint.translate.job import lake_snippets  # local: optional stage

        try:
            return lake_snippets(self.lake, docs)
        except Exception:  # titles alone still extract
            log.exception("snippet lookup for %d documents failed", len(docs))
            return None

    def _extractions(self, ids: list[str]) -> dict[str, dict]:
        if self.extractions is None:
            return {}
        try:
            return self.extractions.get_many(list(ids))
        except Exception:
            log.exception("extraction lookup failed; LLM actors skipped")
            return {}
