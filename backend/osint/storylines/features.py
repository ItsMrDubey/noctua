"""Build linker ``Node`` records from the stores (worker side).

Everything comes from records the pipeline already keeps; nothing is embedded or fetched:

* the event centroid vector (``cluster_state.vector``, pipeline.embed via pipeline.cluster),
  the place keys its documents name (``cluster_state.places``) and their title words
  (``cluster_state.words``);
* the English title from the translation cache (the same lookup the API uses) - English
  terms let a Russian and an Arabic report share "merkury" and "corvette";
* actors (GDELT actors on the event row, short capitalised extracted ``actor`` fields);
* sensor detections already linked to the event (``corroborations``: kind + ref id);
* the best-known time: the occurred band of the per-event timeline (osint.timeline.build,
  the very same code) when its claims agree and are finer than a date; otherwise the first
  report (an upper bound) - the basis and precision are kept on the node.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import UTC, datetime
from typing import Any

import numpy as np

from osint.enrich.gazetteer import fold
from osint.pipeline.cluster import PRECISION_RANK, title_tokens
from osint.storylines.link import Node
from osint.timeline.build import build_timeline, parse_ts

log = logging.getLogger(__name__)

# occurred precisions that are finer than "the whole day": these may move an event's time
FINE_OCCURRED = ("minute", "time_local", "part_of_day")
_ACTOR_OK = re.compile(r"^[A-Z][\w'’.-]*(?: [A-Z][\w'’.-]*){0,3}$")


def _vec(raw: Any) -> np.ndarray | None:
    if not raw:
        return None
    if isinstance(raw, str):
        return np.frombuffer(base64.b64decode(raw), dtype=np.float16).astype(np.float32)
    return np.asarray(raw, dtype=np.float32)


def place_key(name: str | None) -> str:
    """The clusterer's place token ("p:benghazi"): folded first comma part."""
    base = (name or "").split(",")[0]
    return "p:" + " ".join(fold(base).split())


def own_places(row: dict) -> frozenset[str]:
    """The event's own location as tokens; empty below admin1 precision (a country-level
    event is "somewhere in Libya", not a place)."""
    if row.get("lat") is None or PRECISION_RANK.get(row.get("precision") or "", 0) < 1:
        return frozenset()
    keys = set()
    if row.get("h3_5"):
        keys.add(f"h5:{row['h3_5']}")
    if row.get("place_name"):
        keys.add(place_key(row["place_name"]))
    return frozenset(keys)


_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_BREAK = re.compile(r"[:.!?|—–]\s*$")
_LETTER = re.compile(r"[^\W\d_]")
# news boilerplate that is capitalised ("BREAKING | ...", "WATCH: ...") but names nothing
BOILERPLATE = frozenset((
    "breaking", "urgent", "exclusive", "footage", "channels", "channel", "telegram", "osint",
    "media", "sources", "reportedly", "according", "officials", "official", "report",
    "reports", "reported", "update", "updates", "live", "watch", "video", "photos",
    "analysis", "opinion", "explainer"))


def case_info(title: str) -> tuple[set[str], set[str]]:
    """(capitalised, lower-case) content words of a *sentence-case* English title, words
    at the start of a sentence or clause excluded. A Title Case headline tells nothing."""
    words = [m for m in _WORD.finditer(title or "") if len(m.group(0)) >= 4]
    if not words or sum(w.group(0)[0].isupper() for w in words) > 0.6 * len(words):
        return set(), set()
    caps, low = set(), set()
    for m in words:
        before = title[:m.start()]
        if not _LETTER.search(before) or _BREAK.search(before):
            continue  # first word (after emoji / flags) or a clause start
        key = "w:" + fold(m.group(0))
        (caps if m.group(0)[0].isupper() else low).add(key)
    return caps, low


_CONNECT = frozenset({"of", "the", "de", "al", "el", "la", "and"})


def name_phrases(title: str) -> set[str]:
    """Multi-word names of a sentence-case English title ("Gulf of Finland", "Crimean
    Bridge", "Lady D"): runs of >= 2 capitalised words, "of" / "the" / "al" allowed inside.
    A shared phrase is far more specific than its words. Title Case headlines give none."""
    words = list(_WORD.finditer(title or ""))
    long = [w for w in words if len(w.group(0)) >= 4]
    if not long or sum(w.group(0)[0].isupper() for w in long) > 0.6 * len(long):
        return set()
    out: set[str] = set()
    run: list[str] = []

    def close() -> None:
        while run and run[-1].lower() in _CONNECT:
            run.pop()
        if sum(1 for w in run if w[0].isupper()) >= 2:
            out.add("n:" + " ".join(fold(w) for w in run))
        run.clear()

    prev_end = 0
    for m in words:
        w, gap = m.group(0), title[prev_end:m.start()]
        prev_end = m.end()
        if run and (_BREAK.search(gap) or gap.strip(" '’-")):
            close()  # punctuation between words ends a name
        if w[0].isupper() or (run and w.lower() in _CONNECT):
            run.append(w)
        else:
            close()
    close()
    return {p for p in out if p[2:] not in BOILERPLATE}


def terms_of(title_en: str | None, actors: list[str], place_words: set[str],
             place_names: set[str] = frozenset()
             ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(terms, names, lower): content words and multi-word names of the English title plus
    actor names, without the words of named places (the place is scored on its own).
    English only: words of a small-language title look rare in the window although they
    are generic ("four killed")."""
    phrases = {p for p in name_phrases(title_en or "") if p[2:] not in place_names}
    terms = set(title_tokens(title_en or "", limit=32)) | phrases
    caps, low = case_info(title_en or "")
    caps |= phrases
    for a in actors:
        toks = title_tokens(a, limit=8)
        terms |= toks
        caps |= toks
    keep = (lambda xs: frozenset(t for t in xs if t.startswith("n:") or (
        t[2:] not in place_words and t[2:] not in BOILERPLATE)))
    return keep(terms), keep(caps & terms), keep(low & terms)


def actor_names(row: dict, fields: list[dict]) -> list[str]:
    names = [a.get("name") for a in row.get("actors") or [] if isinstance(a, dict)]
    for f in fields:
        v = str(f.get("value") or "").strip()
        if f.get("field") == "actor" and _ACTOR_OK.match(v):
            names.append(v)
    seen, out = set(), []
    for n in names:
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def best_time(row: dict, docs: list[dict], when: list[dict]) -> dict:
    """{t, basis, precision, lo, hi, first_report} for an event, from the per-event
    timeline builder (so a storyline never disagrees with the event's own timeline)."""
    first = parse_ts(row.get("t_start"))
    if docs:
        first = min((parse_ts(d.get("published_at")) for d in docs if d.get("published_at")),
                    default=first)
    out = {"t": first, "basis": "first_report", "precision": "upper_bound", "lo": None,
           "hi": first, "first_report": first}
    if not when or first is None:
        return out
    tl = build_timeline({"id": row["id"], "t_start": row.get("t_start"), "lon": row.get("lon"),
                         "documents": docs, "fields": when})
    occ = tl.get("occurred") or {}
    if occ.get("basis") != "claims":
        return out
    lo, hi = parse_ts(occ["lo"]), parse_ts(occ["hi"])
    out.update(lo=lo, hi=hi, precision=occ.get("precision"))
    if occ.get("precision") in FINE_OCCURRED and lo is not None and lo <= first:
        out.update(t=lo, basis="occurred_claims")
    return out


def news_node(row: dict, *, title_en: str | None, fields: list[dict], docs: list[dict],
              refs: list[str]) -> Node:
    """Node of a news event row (``recent_events``: cluster_state included)."""
    st = row.get("cluster_state") or {}
    mentioned = frozenset(k for k in st.get("places") or [] if k.startswith("p:"))
    own = own_places(row)
    place_words = {w for k in mentioned | own if k.startswith("p:") for w in k[2:].split()}
    actors = actor_names(row, fields)
    terms, names, lower = terms_of(title_en or row.get("title"), actors, place_words,
                                   {k[2:] for k in mentioned | own if k.startswith("p:")})
    when = [f for f in fields if f.get("field") == "when"]
    bt = best_time(row, docs, when)
    return Node(
        id=row["id"], origin="news", category=row["category"], t=bt["t"],
        t_end=max(parse_ts(row["t_end"]), bt["t"]), title=title_en or row.get("title") or "",
        lat=row.get("lat"), lon=row.get("lon"), precision=row.get("precision"),
        place_name=row.get("place_name"), country=row.get("country"), own=own,
        mentioned=mentioned,
        terms=terms, names=names, lower=lower,
        vector=_vec(st.get("vector")), sensor_refs=frozenset(refs),
        mentions=int(row.get("mention_count") or 0),
        sources=int(row.get("source_diversity") or 0), status=row.get("status") or "",
        t_basis=bt["basis"], t_precision=bt["precision"], t_lo=bt["lo"], t_hi=bt["hi"],
        first_report=bt["first_report"], official=bool(st.get("official")),
        actors=tuple(actors[:8]), updated=str(row.get("updated_at") or ""),
    )


def sensor_node(row: dict, detector: str | None) -> Node:
    t0, t1 = parse_ts(row["t_start"]), parse_ts(row["t_end"])
    return Node(
        id=row["id"], origin="sensor", category=row["category"], t=t0, t_end=t1,
        title=row.get("title") or "", lat=row.get("lat"), lon=row.get("lon"),
        precision=row.get("precision"), place_name=row.get("place_name"),
        country=row.get("country"), own=own_places(row), detector=detector,
        status=row.get("status") or "", t_basis="sensor_observed", t_precision="exact",
        t_lo=t0, t_hi=t1, first_report=None, updated=str(row.get("updated_at") or ""))


def load_nodes(store, since: datetime, *, translations=None) -> list[Node]:
    """Nodes for every news and sensor event with t_end >= ``since``."""
    rows = store.recent_events(since, origins=("news", "sensor"))
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    news = [r for r in rows if r.get("origin") == "news"]
    titles: dict[str, str | None] = {}
    if news:
        items = [{"id": r["id"], "title": r.get("title")} for r in news]
        try:
            from osint.translate import english

            english.annotate_events(translations, items)
            titles = {it["id"]: it.get("title_en") for it in items}
        except Exception:  # titles in the source language still link by place and vector
            log.exception("storylines: English titles unavailable")
    fields = store.field_rows([r["id"] for r in news], ("when", "actor"))
    with_when = [eid for eid, fl in fields.items() if any(f["field"] == "when" for f in fl)]
    docs = store.event_documents_for(with_when) if with_when else {}
    refs: dict[str, list[str]] = {}
    for eid, rr in store.corroboration_rows(ids).items():
        refs[eid] = sorted({f"{r['kind']}:{r['ref_id']}" for r in rr})
    detectors = {eid: s.get("detector") for eid, s in store.corroboration_summaries(
        [r["id"] for r in rows if r.get("origin") == "sensor"]).items()}
    out = []
    for r in rows:
        try:
            if r.get("origin") == "news":
                out.append(news_node(r, title_en=titles.get(r["id"]),
                                     fields=fields.get(r["id"], []),
                                     docs=docs.get(r["id"], []), refs=refs.get(r["id"], [])))
            else:
                out.append(sensor_node(r, detectors.get(r["id"])))
        except (KeyError, TypeError, ValueError):
            log.warning("storylines: skipped malformed event %s", r.get("id"))
    return out


def utc(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.astimezone(UTC)
