"""Incremental event clustering and GDELT adjudication (pipeline stage 3).

A relevant document joins an existing event when either

* **text path** — its MinHash signature is an LSH candidate of the event and the estimated
  Jaccard similarity with one of the event's signatures is >= ``text_threshold``
  (syndicated copies, rewrites in the same language), within the event's time window,
  unless a different specific place vetoes it (templated "strike hits <town>"); or
* **geo path** — the event shares a category with the document, lies in the document's
  H3 res-3 cell or a neighbour (``grid_disk(k=1)``), overlaps in time (per-category window)
  and passes a token-overlap check. Tokens are script-agnostic where it matters: the
  place (gazetteer/GDELT name key or H3 res-5 cell) counts as one token, plus title words
  and GDELT actor words. Two specific but different places never join on this path.

* **semantic path** — with an ``Embedder`` (multilingual sentence embeddings, spec §2.6,
  ``osint.pipeline.embed``) the vector of the document's title + snippet is compared with
  the centroids of the nearby events (H3 res-3 disk, or the same country when one side is
  only located at country level; a compatible category, ``compatible_categories``, e.g.
  armed_conflict ~ maritime_security; the time window; the same country; no conflicting
  specific place). The cosine needed depends on how the two locations relate
  (``EventsConfig.threshold``): the same place, one side country-level only, or neither.
  This groups one incident reported in English, Arabic, Russian and Farsi (no shared words,
  no shared MinHash shingles). The default ``NullEmbedder`` turns the path off.

``find_merges``/``merge`` consolidate events that are already split (e.g. created before the
semantic path existed, or by two first reports in different languages in the same pass)
with the same guards on the event vectors (``merge_threshold``); the largest event keeps
its id. See ``osint.pipeline.events.EventBuilder.consolidate``.

Title language: an event's title is its primary article's, except that an English article
joining an event whose title is not English replaces the title and summary (the event list
should read in English when English coverage exists).

Otherwise a new event is created, but only from a real article whose triage relevance is at
least ``min_relevance_create`` (gated categories such as ``political`` also need a concrete
action keyword, ``category_gates``). The event's title and location come from that first
(primary) article only: the location is the article's best gazetteer hit whose matched text
occurs in its own title/snippet (country precision only when the country is named in the
title); without one the event is kept as ``precision='unlocated'`` (no coordinates).

GDELT rows never create events (their titles are synthetic and their SOURCEURL is often
unrelated): they only take the geo path to attach to an existing article-backed event
(role ``gdelt``, never changing its title or location) or are rejected with a reason:
``stale``, ``irrelevant_category``, ``no_location``, ``no_article_backing``. Every decision
is returned for the ``gdelt_decisions`` table.

Pure CPU and deterministic for a given input order; the caller persists the results.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import struct
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import h3
import numpy as np

from osint.core.geo import haversine_km, valid_coord
from osint.enrich.gazetteer import fold
from osint.pipeline.minhash import LSHIndex, MinHasher, jaccard

if TYPE_CHECKING:
    from osint.pipeline.events import EventsConfig

log = logging.getLogger(__name__)

PRECISION_RANK = {"point": 3, "city": 2, "admin1": 1, "country": 0}
UNLOCATED = "unlocated"
# bumped when event semantics change; rows below it are legacy (see events.migrate_events)
CLUSTER_VERSION = 2
GDELT_GEO_PRECISION = {1: "country", 2: "admin1", 3: "city", 4: "city", 5: "admin1"}
ROLE_PRIMARY, ROLE_MENTION, ROLE_GDELT = "primary", "mention", "gdelt"

# ------------------------------------------------------------------------------ ids

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(ts: datetime | None = None) -> str:
    """26-character ULID: 48-bit millisecond time + 80 random bits, Crockford base32.
    Lexical order follows creation time."""
    ms = int((ts.timestamp() if ts else time.time()) * 1000)
    value = (ms & ((1 << 48) - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def event_id_for(doc_id: str, ts: datetime) -> str:
    """Deterministic ULID-shaped event id: 48-bit milliseconds of ``ts`` (the primary
    article's published_at) + 80 bits of sha1(primary document id). The same primary
    article always yields the same id, so extraction results, verification, imagery and
    benchmark labels stay linked across event rebuilds."""
    ms = int(ts.timestamp() * 1000)
    digest = int.from_bytes(hashlib.sha1(doc_id.encode("utf-8")).digest()[:10], "big")
    value = (ms & ((1 << 48) - 1)) << 80 | digest
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


# ------------------------------------------------------------------------- documents

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
# function words and news boilerplate that carry no event identity
_STOP_WORDS = """
the and for with from that this have has had was were are been will would could should
into over after before about against amid says said say saying told tells report reports
reported reporting news live update updates latest video watch photos photo more than
what when where which while who whom whose why how their there they them then than also
just only some many most much other another such very can may might must not new year
years day days week weeks month today yesterday tomorrow first last two three four five
del las los por para con una que como más sobre tras ante desde entre pero sus este esta
les des une pour dans sur par avec aux est qui que pas plus après selon contre
der die das und ist nicht ein eine den dem des mit von auf für sich auch nach bei
"""
_STOP = frozenset(_STOP_WORDS.split())


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host or url


def _place_key(name: str) -> str:
    base = (name or "").split(",")[0]
    return "p:" + " ".join(fold(base).split())


def _cells(lat: float, lon: float) -> tuple[int, int, int]:
    return tuple(h3.str_to_int(h3.latlng_to_cell(lat, lon, r)) for r in (3, 5, 7))


def title_tokens(text: str, limit: int = 24) -> set[str]:
    """Content words of a title: folded, >= 4 letters (any length for CJK), no stopwords."""
    out: set[str] = set()
    for m in _WORD.finditer(fold(text or "")):
        w = m.group(0)
        if w.isdigit() or w in _STOP:
            continue
        if len(w) >= 4 or (_CJK.search(w) and len(w) >= 2):
            out.add("w:" + w)
            if len(out) >= limit:
                break
    return out


@dataclass(slots=True)
class ClusterDoc:
    """A triaged document as the clusterer sees it (one row of the lake's documents)."""

    id: str
    source: str
    url: str
    title: str
    published_at: datetime
    kind: str = "article"
    snippet: str = ""
    language: str = ""
    origin_id: str = ""
    fetched_at: datetime | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    relevance: float = 0.0
    categories: list[str] = field(default_factory=list)
    places: list[dict[str, Any]] = field(default_factory=list)
    place_name: str | None = None
    place_precision: str | None = None
    place_confidence: float | None = None
    lat: float | None = None
    lon: float | None = None
    h3_3: int | None = None
    h3_5: int | None = None
    h3_7: int | None = None

    def __post_init__(self) -> None:
        if not valid_coord(self.lat, self.lon):
            self.lat = self.lon = None
            self.h3_3 = self.h3_5 = self.h3_7 = None
        elif self.h3_3 is None:
            self.h3_3, self.h3_5, self.h3_7 = _cells(self.lat, self.lon)

    @property
    def is_gdelt(self) -> bool:
        return self.kind == "gdelt_event"

    @property
    def located(self) -> bool:
        return self.lat is not None

    @property
    def domain(self) -> str:
        return _domain(self.url)

    @property
    def specific(self) -> bool:
        """True when the best place is finer than a country."""
        return self.located and PRECISION_RANK.get(self.place_precision or "", 0) >= 1

    def text_place(self, min_confidence: float) -> dict[str, Any] | None:
        """Best gazetteer hit supported by this document's own text: its matched text occurs
        in the title/snippet and its confidence is >= ``min_confidence``; a country-level hit
        must be named in the title. Title hits first, then non-demonyms, then the most
        specific, then the most confident."""
        title = " ".join(fold(self.title or "").split())
        text = " ".join(fold(f"{self.title} {self.snippet}").split())
        best, best_key = None, None
        for p in self.places:
            matched = " ".join(fold(str(p.get("matched") or "")).split())
            prec = str(p.get("precision") or "")
            if not matched or prec not in PRECISION_RANK or not valid_coord(
                    p.get("lat"), p.get("lon")):
                continue
            if float(p.get("confidence") or 0) < min_confidence or matched not in text:
                continue
            in_title = matched in title
            if prec == "country" and not in_title:
                continue
            # "Russian strike on X": a demonym names a side, not the place -> ranked last
            name = " ".join(fold(str(p.get("name") or "")).split())
            demonym = prec == "country" and len(matched) > len(name) >= 4                 and matched.startswith(name[:4])
            key = (in_title, not demonym, PRECISION_RANK[prec],
                   float(p.get("confidence") or 0))
            if best_key is None or key > best_key:
                best, best_key = p, key
        return best

    def relocate(self, min_confidence: float) -> None:
        """Replace the lake's best place with the text-supported one (or none)."""
        p = self.text_place(min_confidence)
        if p is None:
            self.lat = self.lon = None
            self.h3_3 = self.h3_5 = self.h3_7 = None
            self.place_name = self.place_precision = None
            self.place_confidence = None
            return
        self.lat, self.lon = float(p["lat"]), float(p["lon"])
        self.place_name, self.place_precision = str(p.get("name") or ""), str(p["precision"])
        self.place_confidence = float(p.get("confidence") or 0)
        self.h3_3, self.h3_5, self.h3_7 = _cells(self.lat, self.lon)

    def place_keys(self) -> set[str]:
        """Script-agnostic place identity tokens (city/admin1 names, H3 res-5 cell)."""
        keys: set[str] = set()
        for p in self.places:
            if PRECISION_RANK.get(str(p.get("precision") or ""), 0) >= 1 \
                    and float(p.get("confidence") or 0) >= 0.5 and p.get("name"):
                keys.add(_place_key(str(p["name"])))
        if self.specific:
            if self.place_name:
                keys.add(_place_key(self.place_name))
            keys.add(f"h5:{self.h3_5:x}")
        return keys

    def words(self) -> set[str]:
        """Title content words, without the words of the places it names (the place is
        already one token: "Strait of Hormuz" must not count three times)."""
        if self.is_gdelt:
            a = self.attrs
            return title_tokens(f"{a.get('actor1') or ''} {a.get('actor2') or ''}")
        place_words: set[str] = set()
        for p in self.places:
            for t in (p.get("name"), p.get("matched")):
                place_words |= title_tokens(str(t or ""), limit=64)
        return title_tokens(self.title) - place_words

    def actors(self) -> list[dict[str, str]]:
        if not self.is_gdelt:
            return []
        out = []
        for n, role in ((1, "source"), (2, "target")):
            name = str(self.attrs.get(f"actor{n}") or "").strip()
            if name:
                out.append({"name": name.title(), "role": role,
                            "type": str(self.attrs.get(f"actor{n}_code") or ""),
                            "country": str(self.attrs.get(f"actor{n}_country") or "")})
        return out


# ---------------------------------------------------------------------------- events


def _pack(sig: tuple[int, ...]) -> str:
    return base64.b64encode(struct.pack(f"<{len(sig)}I", *sig)).decode("ascii")


def _unpack(text: str) -> tuple[int, ...]:
    raw = base64.b64decode(text)
    return struct.unpack(f"<{len(raw) // 4}I", raw)


def _pack_vec(v: np.ndarray | None) -> str | None:
    """float16 + base64 (768 bytes for 384 dimensions): compact enough for cluster_state."""
    if v is None:
        return None
    return base64.b64encode(np.asarray(v, dtype=np.float16).tobytes()).decode("ascii")


def _unpack_vec(v: Any) -> np.ndarray | None:
    if not v:
        return None
    if isinstance(v, str):
        return np.frombuffer(base64.b64decode(v), dtype=np.float16).astype(np.float32)
    return np.asarray(v, dtype=np.float32)  # legacy JSON list


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class EventState:
    """In-memory event under construction; ``to_row`` gives the SQLite events row."""

    id: str
    category: str
    t_start: datetime
    t_end: datetime
    first_seen: datetime
    updated_at: datetime
    title: str = ""
    summary: str = ""
    subcategory: str | None = None
    cameo_root: str | None = None
    actors: list[dict[str, str]] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    precision: str | None = None
    place_name: str | None = None
    country: str | None = None
    geo_confidence: float | None = None
    h3_3: int | None = None
    h3_5: int | None = None
    h3_7: int | None = None
    status: str = "reported"
    confidence: float = 0.0
    version: int = 0
    mention_count: int = 0
    domains: set[str] = field(default_factory=set)
    gdelt_ids: list[str] = field(default_factory=list)
    # clustering state
    words: set[str] = field(default_factory=set)
    place_keys: set[str] = field(default_factory=set)
    sigs: list[tuple[int, ...]] = field(default_factory=list)
    relevance: float = 0.0
    title_score: float = -1.0
    has_article: bool = False
    # semantic state: centroid of the member documents' vectors (pipeline.embed)
    vector: np.ndarray | None = None
    vec_n: int = 0  # documents averaged into ``vector``
    title_lang: str = ""
    languages: set[str] = field(default_factory=set)
    # hooks for M3/M4 status levels
    sensor_corroborated: bool = False
    official: bool = False
    gen: int = 0  # rebuild generation that wrote the event (events.rebuild_events)

    @property
    def source_diversity(self) -> int:
        return len(self.domains)

    @property
    def gdelt_accepted(self) -> bool:
        return bool(self.gdelt_ids)

    def to_row(self) -> dict[str, Any]:
        hx = (lambda c: h3.int_to_str(c) if c is not None else None)
        return {
            "id": self.id, "category": self.category, "subcategory": self.subcategory,
            "cameo_root": self.cameo_root, "title": self.title, "summary": self.summary,
            "actors": self.actors, "lat": self.lat, "lon": self.lon,
            "precision": self.precision, "place_name": self.place_name,
            "country": self.country, "geo_confidence": self.geo_confidence,
            "h3_3": hx(self.h3_3), "h3_5": hx(self.h3_5), "h3_7": hx(self.h3_7),
            "t_start": _iso(self.t_start), "t_end": _iso(self.t_end), "status": self.status,
            "confidence": round(self.confidence, 4), "version": self.version,
            "mention_count": self.mention_count, "source_diversity": self.source_diversity,
            "gdelt_ids": self.gdelt_ids, "first_seen": _iso(self.first_seen),
            "updated_at": _iso(self.updated_at),
            "cluster_state": {
                "domains": sorted(self.domains), "words": sorted(self.words),
                "places": sorted(self.place_keys), "sigs": [_pack(s) for s in self.sigs],
                "relevance": self.relevance, "title_score": self.title_score,
                "has_article": self.has_article, "vector": _pack_vec(self.vector),
                "vec_n": self.vec_n,
                "title_lang": self.title_lang, "languages": sorted(self.languages),
                "sensor_corroborated": self.sensor_corroborated, "official": self.official,
                "v": CLUSTER_VERSION, "gen": self.gen,
            },
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EventState:
        st = row.get("cluster_state") or {}
        cell = (lambda s: h3.str_to_int(s) if s else None)
        return cls(
            id=row["id"], category=row["category"], subcategory=row.get("subcategory"),
            cameo_root=row.get("cameo_root"), title=row["title"],
            summary=row.get("summary") or "", actors=list(row.get("actors") or []),
            lat=row.get("lat"), lon=row.get("lon"), precision=row.get("precision"),
            place_name=row.get("place_name"), country=row.get("country"),
            geo_confidence=row.get("geo_confidence"), h3_3=cell(row.get("h3_3")),
            h3_5=cell(row.get("h3_5")), h3_7=cell(row.get("h3_7")),
            t_start=_parse_iso(row["t_start"]), t_end=_parse_iso(row["t_end"]),
            first_seen=_parse_iso(row["first_seen"]),
            updated_at=_parse_iso(row["updated_at"]), status=row["status"],
            confidence=row["confidence"], version=row["version"],
            mention_count=row["mention_count"], domains=set(st.get("domains") or []),
            gdelt_ids=list(row.get("gdelt_ids") or []), words=set(st.get("words") or []),
            place_keys=set(st.get("places") or []),
            sigs=[_unpack(s) for s in st.get("sigs") or []],
            relevance=float(st.get("relevance") or 0.0),
            title_score=float(st.get("title_score", -1.0)),
            has_article=bool(st.get("has_article")), vector=_unpack_vec(st.get("vector")),
            vec_n=int(st.get("vec_n") or 0), title_lang=str(st.get("title_lang") or ""),
            languages=set(st.get("languages") or []),
            sensor_corroborated=bool(st.get("sensor_corroborated")),
            official=bool(st.get("official")), gen=int(st.get("gen") or 0),
        )


# ------------------------------------------------------------------------ embedding


class Embedder(Protocol):
    """Pluggable sentence embedder (``osint.pipeline.embed.OnnxEmbedder``, spec §2.6): one
    vector per text, or None when unavailable. ``keys`` (document ids) allow caching."""

    def embed(self, texts: list[str], keys: list[str] | None = None
              ) -> Sequence[Any] | None: ...


class NullEmbedder:
    """Default: no embeddings, so the semantic path is skipped."""

    def embed(self, texts: list[str], keys: list[str] | None = None) -> None:
        return None


def _cosine(a: Any, b: Any) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    den = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
    return float(a @ b) / den if den else 0.0


def _relation(prec_a: str | None, prec_b: str | None) -> str:
    """'contained' when exactly one side is located at country level only, else 'none'."""
    return "contained" if (prec_a == "country") != (prec_b == "country") else "none"


def english_title_better(lang: str | None, ev: EventState) -> bool:
    """An English title replaces a non-English event title (never the other way round)."""
    return (lang or "") == "en" and (ev.title_lang or "") != "en"


_NOISE = re.compile(r"https?://\S+|[#@]\w+", re.UNICODE)
EMBED_MIN_WORDS = 4


def _content_words(text: str) -> int:
    return sum(1 for m in _WORD.finditer(_NOISE.sub(" ", text))
               if len(m.group(0)) >= 2 and not m.group(0).isdigit())


def embed_text(title: str, snippet: str = "", max_chars: int = 400) -> str:
    """The text embedded for a document: title and snippet without links and hashtags (a
    snippet that repeats the title replaces it). On labelled event pairs title+snippet
    beats the title alone (at 98% precision it finds 30% of the same-incident pairs instead
    of 21%: one-line headlines in two languages often stress different aspects). "" when
    fewer than ``EMBED_MIN_WORDS`` words remain ("#Ukraine"): no vector, such posts would
    match anything."""
    t = " ".join(_NOISE.sub(" ", title or "").split())
    sn = " ".join(_NOISE.sub(" ", snippet or "").split())
    if t and sn.startswith(t.rstrip(".:…")):
        t = ""
    text = f"{t}. {sn}" if t and sn else (t or sn)
    text = text[:max_chars]
    return text if _content_words(text) >= EMBED_MIN_WORDS else ""


def embed_key(doc_id: str, text: str) -> str:
    """Cache key: the document id plus a hash of the embedded text (a rule change re-embeds)."""
    return f"{doc_id}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def embed_many(embedder: Embedder, items: list[tuple[str, str]]) -> list[Any]:
    """Vectors for (doc id, text) pairs; None for empty texts or when the embedder is
    unavailable or fails (the semantic path is optional: never fail a pass over it)."""
    out: list[Any] = [None] * len(items)
    idx = [i for i, (_, t) in enumerate(items) if t]
    if not idx:
        return out
    try:
        vecs = embedder.embed([items[i][1] for i in idx],
                              [embed_key(items[i][0], items[i][1]) for i in idx])
    except Exception:
        log.exception("embedding %d documents failed", len(idx))
        return out
    if vecs is None or len(vecs) != len(idx):
        return out
    for i, v in zip(idx, vecs, strict=True):
        out[i] = v
    return out


# ------------------------------------------------------------------------- results


@dataclass
class ClusterResult:
    """Everything one ``process`` call changed, ready to persist."""

    created: set[str] = field(default_factory=set)
    updated: set[str] = field(default_factory=set)
    documents: list[dict[str, Any]] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    clustered: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    gdelt_accepted: int = 0
    gdelt_rejected: dict[str, int] = field(default_factory=dict)
    joins: dict[str, int] = field(default_factory=dict)  # path -> documents (text/geo/...)

    def join(self, path: str) -> None:
        self.joins[path] = self.joins.get(path, 0) + 1

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def changed(self) -> set[str]:
        return self.created | self.updated


Scorer = Callable[[EventState, datetime], tuple[str, float]]


@dataclass
class Merge:
    """One event folded into another by ``Clusterer.merge`` (the loser's id is gone)."""

    survivor: str
    loser: str
    similarity: float
    fields: list[dict[str, Any]] = field(default_factory=list)


class Clusterer:
    def __init__(self, cfg: EventsConfig, scorer: Scorer, *,
                 cameo_category: Callable[[str, str], str | None] | None = None,
                 embedder: Embedder | None = None) -> None:
        self.cfg = cfg
        self.scorer = scorer
        self.cameo_category = cameo_category or (lambda code, root: None)
        self.embedder: Embedder = embedder or NullEmbedder()
        self.hasher = MinHasher(cfg.permutations, cfg.shingle)
        self.lsh = LSHIndex(cfg.permutations, cfg.bands)
        self.events: dict[str, EventState] = {}
        self._geo: dict[int, set[str]] = {}
        # country (upper case) -> event ids, and event id -> its indexed country
        self._by_country: dict[str, set[str]] = {}
        self._ev_country: dict[str, str] = {}
        self.generation = 0  # stamped on created events (rebuild generation)
        # primary document id -> event id to reuse (a rebuild keeps pre-existing ids)
        self.id_override: dict[str, str] = {}
        # (language, title) -> True when its English translation is already cached; a
        # merge of two non-English events then keeps the translated title (EventBuilder)
        self.translated: Callable[[str, str], bool] | None = None
        from osint.pipeline.triage import Lexicon  # local: triage loads the gazetteer stack

        self._gates = Lexicon()
        self._gated = set(cfg.category_gates)
        for cat, terms in cfg.category_gates.items():
            for t in terms:
                self._gates.add(cat, t)
        if not self._gates.terms:
            self._gates = None

    # --------------------------------------------------------------- index upkeep

    def _index_country(self, ev: EventState, *, drop: bool = False) -> None:
        old = self._ev_country.get(ev.id)
        new = None if drop else ((ev.country or "").upper() or None)
        if old == new:
            return
        if old is not None:
            bucket = self._by_country.get(old)
            if bucket is not None:
                bucket.discard(ev.id)
                if not bucket:
                    del self._by_country[old]
            del self._ev_country[ev.id]
        if new is not None:
            self._by_country.setdefault(new, set()).add(ev.id)
            self._ev_country[ev.id] = new

    def _index_geo(self, ev: EventState, old_cell: int | None) -> None:
        self._index_country(ev)
        if old_cell == ev.h3_3:
            return
        if old_cell is not None:
            bucket = self._geo.get(old_cell)
            if bucket is not None:
                bucket.discard(ev.id)
                if not bucket:
                    del self._geo[old_cell]
        if ev.h3_3 is not None:
            self._geo.setdefault(ev.h3_3, set()).add(ev.id)

    def reset(self) -> None:
        """Forget every in-memory event (after an external rebuild)."""
        self.events.clear()
        self._geo.clear()
        self._by_country.clear()
        self._ev_country.clear()
        self.lsh = LSHIndex(self.cfg.permutations, self.cfg.bands)

    def _add_event(self, ev: EventState) -> None:
        self.events[ev.id] = ev
        self._index_geo(ev, None)
        for sig in ev.sigs:
            self.lsh.insert(ev.id, sig)

    def _drop_event(self, event_id: str) -> None:
        ev = self.events.pop(event_id, None)
        if ev is None:
            return
        self.lsh.remove(event_id)
        self._index_country(ev, drop=True)
        if ev.h3_3 is not None:
            bucket = self._geo.get(ev.h3_3)
            if bucket is not None:
                bucket.discard(event_id)
                if not bucket:
                    del self._geo[ev.h3_3]

    def load(self, rows: Iterable[dict[str, Any]]) -> int:
        """Warm start from persisted event rows (``SqliteStore.recent_events``)."""
        n = 0
        for row in rows:
            self._add_event(EventState.from_row(row))
            n += 1
        return n

    def prune(self, now: datetime) -> int:
        cutoff = now - timedelta(hours=self.cfg.index_window_h)
        old = [eid for eid, ev in self.events.items() if ev.t_end < cutoff]
        for eid in old:
            self._drop_event(eid)
        return len(old)

    # ----------------------------------------------------------------- matching

    def _in_window(self, ev: EventState, doc: ClusterDoc, hours: float) -> bool:
        w = timedelta(hours=hours)
        return ev.t_start - w <= doc.published_at <= ev.t_end + w

    def _text_match(self, doc: ClusterDoc, sig: tuple[int, ...],
                    places: set[str]) -> EventState | None:
        """Near-duplicate text within the event's time window; a conflicting specific
        place vetoes (templated headlines: "drone strike hits <town>")."""
        best, best_sim = None, self.cfg.text_threshold
        for eid in self.lsh.query(sig):
            ev = self.events.get(eid)
            if ev is None or not self._in_window(ev, doc, self.cfg.window_h(ev.category)):
                continue
            if places and ev.place_keys and not places & ev.place_keys:
                continue
            sim = max(jaccard(sig, s) for s in ev.sigs)
            if sim >= best_sim:
                best, best_sim = ev, sim
        return best

    def _geo_candidates(self, doc: ClusterDoc) -> list[EventState]:
        if doc.h3_3 is None:
            return []
        out = []
        for cell in h3.grid_disk(h3.int_to_str(doc.h3_3), 1):
            for eid in self._geo.get(h3.str_to_int(cell), ()):
                ev = self.events[eid]
                if ev.category in doc.categories \
                        and self._in_window(ev, doc, self.cfg.window_h(ev.category)):
                    out.append(ev)
        return out

    def overlap(self, doc_places: set[str], doc_words: set[str],
                ev: EventState) -> tuple[bool, int, float] | None:
        """(place shared, shared tokens, overlap ratio); None when the places conflict."""
        place_shared = bool(doc_places & ev.place_keys)
        if doc_places and ev.place_keys and not place_shared:
            return None  # two specific, different places -> different events
        shared = int(place_shared) + len(doc_words & ev.words)
        size_doc = int(bool(doc_places)) + len(doc_words)
        size_ev = int(bool(ev.place_keys)) + len(ev.words)
        denom = min(size_doc, size_ev)
        return place_shared, shared, (shared / denom if denom else 0.0)

    def _geo_match(self, doc: ClusterDoc, places: set[str], words: set[str],
                   candidates: list[EventState]) -> EventState | None:
        best, best_key = None, None
        for ev in candidates:
            ov = self.overlap(places, words, ev)
            if ov is None:
                continue
            place_shared, shared, ratio = ov
            need = self.cfg.min_shared(ev.category)
            if place_shared and shared == 1 and (
                    not self._in_window(ev, doc, self.cfg.place_only_window_h)
                    or (not doc.is_gdelt and doc.language
                        and doc.language in ev.languages)):
                # the place alone only joins close in time, and only across languages
                # (two same-language reports of one incident share a content word)
                continue
            ok = (place_shared and shared >= need) or (shared - place_shared) >= max(2, need)
            if not ok or ratio < self.cfg.overlap_threshold:
                continue
            key = (shared, ratio, ev.t_end)
            if best_key is None or key > best_key:
                best, best_key = ev, key
        return best

    def _semantic_candidates(self, doc: ClusterDoc) -> list[EventState]:
        """Events with a vector in the document's H3 res-3 disk, of a category compatible
        with one of the document's, whose time window covers the document."""
        if doc.h3_3 is None:
            return []
        ids = {eid for cell in h3.grid_disk(h3.int_to_str(doc.h3_3), 1)
               for eid in self._geo.get(h3.str_to_int(cell), ())}
        country = (self._country(doc) or "").upper()
        for eid in self._by_country.get(country, ()) if country else ():
            # a country-level report ("in Libya") contains the city-level ones and vice versa
            if doc.place_precision == "country" or self.events[eid].precision == "country":
                ids.add(eid)
        out = []
        for eid in sorted(ids):
            ev = self.events[eid]
            if ev.vector is not None and self.cfg.compatible(ev.category, doc.categories) \
                    and self._in_window(ev, doc, self.cfg.window_h(ev.category)):
                out.append(ev)
        return out

    def _semantic_match(self, doc: ClusterDoc, vec: Any, places: set[str], words: set[str],
                        candidates: list[EventState]) -> tuple[EventState, float] | None:
        """Best event at cosine >= the threshold of its place relation (the document and
        the event located at the same specific place, or not); a conflicting specific place
        or another country never joins."""
        if vec is None:
            return None
        country = (self._country(doc) or "").upper()
        own = self.own_places(doc)
        best, best_sim = None, -1.0
        for ev in candidates:
            if country and ev.country and country != ev.country.upper():
                continue
            if self.overlap(places, words, ev) is None:
                continue  # two specific, different places
            rel = "shared" if own & self.own_places(ev) else _relation(doc.place_precision,
                                                                       ev.precision)
            need = self.cfg.threshold(rel)
            sim = _cosine(vec, ev.vector)
            if sim >= need and sim > best_sim:
                best, best_sim = ev, sim
        return (best, best_sim) if best is not None else None

    # ------------------------------------------------------------ consolidation

    @staticmethod
    def own_places(ev: EventState | ClusterDoc) -> set[str]:
        """The location itself as place tokens (name key, H3 res-5 cell); empty below
        admin1 precision. Not the places its documents mention: a roundup event mentions
        many."""
        if ev.lat is None or PRECISION_RANK.get(
                (ev.precision if isinstance(ev, EventState) else ev.place_precision) or "",
                0) < 1:
            return set()
        keys = {f"h5:{ev.h3_5:x}"} if ev.h3_5 is not None else set()
        if ev.place_name:
            keys.add(_place_key(ev.place_name))
        return keys

    def _place_relation(self, a: EventState, b: EventState) -> str | None:
        """How the locations of two events relate (thresholds ``Clusterer.threshold``):

        * 'shared': the same specific place (name or H3 res-5 cell);
        * 'contained': exactly one of the two is located only at country level (same
          country, checked by the caller): "in Libya" vs the Benghazi event;
        * 'none': both country-level, or two different sites of one area each named by
          the other's coverage (two strikes of one night around Kyiv);
        * None: specific, different places neither event's documents mention: never."""
        pa, pb = self.own_places(a), self.own_places(b)
        if pa & pb:
            return "shared"
        if pa and pb and not (pa & b.place_keys or pb & a.place_keys):
            return None
        return _relation(a.precision, b.precision)

    def pair_similarity(self, a: EventState, b: EventState) -> float | None:
        """Centroid cosine of two events when they may be one incident (compatible
        category, neighbouring H3 res-3 cells, overlapping time windows, no conflicting
        place, both article-backed) and the cosine reaches the merge threshold of their
        place relation; else None."""
        cfg = self.cfg
        if a.vector is None or b.vector is None or not (a.has_article and b.has_article):
            return None
        if a.h3_3 is None or b.h3_3 is None or not cfg.compatible(a.category, [b.category]):
            return None
        if not self._near(a, b):
            return None
        w = timedelta(hours=max(cfg.window_h(a.category), cfg.window_h(b.category)))
        if a.t_start - w > b.t_end or b.t_start - w > a.t_end:
            return None
        if a.country and b.country and a.country.upper() != b.country.upper():
            return None
        rel = self._place_relation(a, b)
        if rel is None:
            return None
        need = cfg.threshold(rel, merge=True)
        sim = _cosine(a.vector, b.vector)
        return sim if sim >= need else None

    @staticmethod
    def _contains(a: EventState, b: EventState) -> bool:
        """Same country and one of the two is only located at country level."""
        return bool(a.country) and (a.country or "").upper() == (b.country or "").upper() \
            and "country" in (a.precision, b.precision)

    def _near(self, a: EventState, b: EventState) -> bool:
        if a.h3_3 == b.h3_3 or self._contains(a, b):
            return True
        return h3.int_to_str(b.h3_3) in h3.grid_disk(h3.int_to_str(a.h3_3), 1)

    def _neighbours(self, ev: EventState) -> list[EventState]:
        """Events in the H3 res-3 disk of ``ev`` or in its country with one of the two at
        country level."""
        if ev.h3_3 is None:
            return []
        ids = {eid for cell in h3.grid_disk(h3.int_to_str(ev.h3_3), 1)
               for eid in self._geo.get(h3.str_to_int(cell), ())}
        for eid in self._by_country.get((ev.country or "").upper(), ()) if ev.country else ():
            if self._contains(ev, self.events[eid]):
                ids.add(eid)
        ids.discard(ev.id)
        return [self.events[e] for e in sorted(ids)]

    def find_merges(self, ids: Iterable[str] | None = None) -> list[tuple[float, str, str]]:
        """Candidate pairs (similarity, event id, event id), best first, among ``ids`` (all
        indexed events when None) and their neighbours."""
        pool = self.events if ids is None else {e: self.events[e] for e in ids
                                                 if e in self.events}
        seen: set[tuple[str, str]] = set()
        out = []
        for ev in pool.values():
            for other in self._neighbours(ev):
                key = (min(ev.id, other.id), max(ev.id, other.id))
                if key in seen:
                    continue
                seen.add(key)
                sim = self.pair_similarity(ev, other)
                if sim is not None:
                    out.append((sim, *key))
        out.sort(key=lambda t: (-t[0], t[1], t[2]))
        return out

    def consolidate(self, ids: Iterable[str] | None, now: datetime) -> list[Merge]:
        """Merge split events among ``ids`` (all when None) and their neighbours, best pair
        first, re-scoring the merged event after each merge (average linkage: a chain
        A~B~C only collapses while the merged centroids stay similar)."""
        merges: list[Merge] = []
        pairs = self.find_merges(ids)
        while pairs:
            sim, a, b = pairs[0]
            m = self.merge(self.events[a], self.events[b], now, similarity=sim)
            merges.append(m)
            # pairs of the two merged events are re-scored against the merged event only
            pairs = [p for p in pairs if not {p[1], p[2]} & {a, b}]
            pairs += self.find_merges([m.survivor])
            pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
        return merges

    @staticmethod
    def survivor_of(a: EventState, b: EventState) -> tuple[EventState, EventState]:
        """(survivor, loser): the larger event keeps its id (then the older, then the
        smaller id, so the choice is deterministic)."""
        ka = (-a.mention_count, a.t_start, a.id)
        kb = (-b.mention_count, b.t_start, b.id)
        return (a, b) if ka <= kb else (b, a)

    def merge(self, a: EventState, b: EventState, now: datetime, *,
              similarity: float = 0.0) -> Merge:
        """Fold the smaller of two events into the larger (in memory; the caller persists
        the survivor row and moves the loser's links, ``SqliteStore.merge_events``)."""
        cfg = self.cfg
        s, lo = self.survivor_of(a, b)
        m = Merge(survivor=s.id, loser=lo.id, similarity=round(similarity, 4))
        self._drop_event(lo.id)
        s.version = max(s.version, lo.version) + 1
        s.mention_count += lo.mention_count
        s.domains |= lo.domains
        s.languages |= lo.languages
        s.t_start, s.t_end = min(s.t_start, lo.t_start), max(s.t_end, lo.t_end)
        s.first_seen = min(s.first_seen, lo.first_seen)
        s.updated_at = now
        s.relevance = max(s.relevance, lo.relevance)
        s.place_keys |= lo.place_keys
        for w in sorted(lo.words):
            if len(s.words) >= cfg.max_tokens:
                break
            s.words.add(w)
        for sig in lo.sigs:
            if len(s.sigs) >= cfg.max_signatures:
                break
            s.sigs.append(sig)
            self.lsh.insert(s.id, sig)
        for g in lo.gdelt_ids:
            if g not in s.gdelt_ids:
                s.gdelt_ids.append(g)
        known = {x["name"].casefold() for x in s.actors}
        s.actors += [x for x in lo.actors if x["name"].casefold() not in known]
        s.cameo_root = s.cameo_root or lo.cameo_root
        s.subcategory = s.subcategory or lo.subcategory
        s.has_article = s.has_article or lo.has_article
        s.sensor_corroborated = s.sensor_corroborated or lo.sensor_corroborated
        s.official = s.official or lo.official
        if lo.vector is not None:
            if s.vector is None:
                s.vector, s.vec_n = lo.vector.copy(), lo.vec_n
            else:
                n = max(1, s.vec_n) + max(1, lo.vec_n)
                s.vector = (s.vector * max(1, s.vec_n) + lo.vector * max(1, lo.vec_n)) / n
                s.vec_n = n
        why = None
        if english_title_better(lo.title_lang, s):
            why = "English source title preferred"
        elif s.title_lang != "en" and lo.title_lang != "en" and self.translated is not None \
                and not self.translated(s.title_lang, s.title) \
                and self.translated(lo.title_lang, lo.title):
            why = "title with a cached English translation preferred"
        if why:
            s.title, s.summary = lo.title, lo.summary
            s.title_score, s.title_lang = lo.title_score, lo.title_lang
            m.fields.append({"event_id": s.id, "field": "title", "value": lo.title,
                             "doc_id": f"merge:{lo.id}", "quote": why})
        if lo.lat is not None and PRECISION_RANK.get(lo.precision or "", -1) > PRECISION_RANK.get(
                s.precision or "", -1) and (s.lat is None or (s.country or "").upper() == (
                lo.country or "").upper()):
            # the merged event keeps the finer location (never a downgrade, never across
            # countries): a country-level "in Libya" event absorbing the Benghazi one
            if s.lat is not None:
                m.fields.append({"event_id": s.id, "field": "location_before_merge",
                                 "value": f"{s.place_name or ''} ({s.lat}, {s.lon}, "
                                          f"{s.precision})"[:300],
                                 "doc_id": f"merge:{lo.id}", "quote": "finer place of a merged event"})
            old_cell = s.h3_3
            s.lat, s.lon, s.precision = lo.lat, lo.lon, lo.precision
            s.place_name, s.country, s.geo_confidence = lo.place_name, lo.country,                 lo.geo_confidence
            s.h3_3, s.h3_5, s.h3_7 = lo.h3_3, lo.h3_5, lo.h3_7
            self._index_geo(s, old_cell)
            m.fields.append({"event_id": s.id, "field": "location", "value": lo.place_name or "",
                             "doc_id": f"merge:{lo.id}", "quote": lo.place_name or ""})
        m.fields.append({"event_id": s.id, "field": "merged_event", "value": lo.id,
                         "doc_id": f"merge:{lo.id}",
                         "quote": f"cosine {similarity:.3f}: {lo.title}"[:300]})
        s.status, s.confidence = self.scorer(s, now)
        return m

    # ---------------------------------------------------------------- mutation

    def _new_event(self, doc: ClusterDoc, category: str, now: datetime) -> EventState:
        eid = self.id_override.get(doc.id) or event_id_for(doc.id, doc.published_at)
        if eid in self.events:  # same primary article again (re-fetch): keep ids unique
            eid = new_ulid(now)
        ev = EventState(id=eid, category=category, t_start=doc.published_at,
                        t_end=doc.published_at, first_seen=now, updated_at=now,
                        title=doc.title, summary=doc.snippet,
                        precision=None if doc.located else UNLOCATED, gen=self.generation)
        self._add_event(ev)
        return ev

    def _attach(self, ev: EventState, doc: ClusterDoc, role: str, now: datetime,
                res: ClusterResult, sig: tuple[int, ...] | None, places: set[str],
                words: set[str], vec: Any) -> None:
        cfg = self.cfg
        ev.version += 1
        ev.mention_count += 1
        if not doc.is_gdelt:  # GDELT SOURCEURLs are often unrelated: not independent outlets
            ev.domains.add(doc.domain)
            if doc.language:
                ev.languages.add(doc.language)
        ev.t_start = min(ev.t_start, doc.published_at)
        ev.t_end = max(ev.t_end, doc.published_at)
        ev.updated_at = now
        ev.relevance = max(ev.relevance, doc.relevance)
        ev.place_keys |= places
        for w in sorted(words):
            if len(ev.words) >= cfg.max_tokens:
                break
            ev.words.add(w)
        if sig is not None and len(ev.sigs) < cfg.max_signatures:
            ev.sigs.append(sig)
            self.lsh.insert(ev.id, sig)
        if vec is not None:
            self._add_vector(ev, vec)
        # location and title come from the primary (first) article only: a later article
        # or a GDELT row never moves the event or renames it
        primary = role == ROLE_PRIMARY and not doc.is_gdelt
        if primary and doc.located:
            old_cell = ev.h3_3
            ev.lat, ev.lon = doc.lat, doc.lon
            ev.precision, ev.place_name = doc.place_precision, doc.place_name
            ev.geo_confidence = doc.place_confidence
            ev.country = self._country(doc)
            ev.h3_3, ev.h3_5, ev.h3_7 = doc.h3_3, doc.h3_5, doc.h3_7
            self._index_geo(ev, old_cell)
            res.fields.append(self._field(ev, "location", doc.place_name or "", doc,
                                          self._place_quote(doc)))
        elif role == ROLE_MENTION and not doc.is_gdelt and doc.located:
            self._refine(ev, doc, res)
        if primary:
            ev.title, ev.summary = doc.title, doc.snippet
            ev.title_score = doc.relevance
            ev.title_lang = doc.language or ""
            if not doc.located:
                ev.precision = UNLOCATED
        elif role == ROLE_MENTION and not doc.is_gdelt and doc.title \
                and english_title_better(doc.language, ev):
            ev.title, ev.summary = doc.title, doc.snippet
            ev.title_score, ev.title_lang = doc.relevance, doc.language
            res.fields.append(self._field(ev, "title", doc.title, doc,
                                          "English source title preferred"))
        if not doc.is_gdelt:
            ev.has_article = True
        if doc.is_gdelt:
            if doc.origin_id and doc.origin_id not in ev.gdelt_ids:
                ev.gdelt_ids.append(doc.origin_id)
            root = str(doc.attrs.get("event_root_code") or "") or None
            ev.cameo_root = ev.cameo_root or root
            ev.subcategory = ev.subcategory or (str(doc.attrs.get("event_code") or "") or None)
            known = {a["name"].casefold() for a in ev.actors}
            for a in doc.actors():
                if a["name"].casefold() not in known:
                    ev.actors.append(a)
                    known.add(a["name"].casefold())
                    res.fields.append(self._field(ev, "actor", a["name"], doc,
                                                  f"{a['role']}: {a['name']}"))
            if doc.attrs.get("event_code"):
                res.fields.append(self._field(ev, "cameo_code", doc.attrs["event_code"], doc,
                                              doc.title))
        res.fields.append(self._field(ev, "category", ev.category, doc, doc.title))
        res.fields.append(self._field(ev, "time", _iso(doc.published_at), doc, doc.title))
        res.documents.append({
            "event_id": ev.id, "doc_id": doc.id, "source": doc.source, "url": doc.url,
            "title": doc.title, "published_at": _iso(doc.published_at),
            "language": doc.language or "", "role": role, "added_at": _iso(now)})
        ev.status, ev.confidence = self.scorer(ev, now)

    def _add_vector(self, ev: EventState, vec: Any) -> None:
        """Fold a member document's vector into the event centroid (running mean). Scoring
        is centroid-only: on labelled pairs the best single member vector was less precise."""
        v = np.asarray(vec, dtype=np.float32)
        if ev.vector is None or ev.vec_n <= 0:
            ev.vector, ev.vec_n = v.copy(), 1
        else:
            ev.vec_n += 1
            ev.vector = ev.vector + (v - ev.vector) / ev.vec_n

    def _refine(self, ev: EventState, doc: ClusterDoc, res: ClusterResult) -> None:
        """A later article whose own text names a finer place (its text-supported best
        place) refines the event location: same country, within ``refine_max_km`` of the
        current point, strictly more precise. Never a downgrade, never across countries,
        never for unlocated events (no country to check against)."""
        if ev.lat is None or not ev.country or _place_key(doc.place_name or "") == _place_key(
                ev.place_name or ""):
            return  # (the same place is not a finer one, whatever its precision label)
        if PRECISION_RANK.get(doc.place_precision or "", -1) <= PRECISION_RANK.get(
                ev.precision or "", -1):
            return
        country = self._country(doc)
        if not country or country.upper() != ev.country.upper():
            return
        if haversine_km(ev.lat, ev.lon, doc.lat, doc.lon) > self.cfg.refine_max_km:
            return
        before = f"{ev.place_name or ''} ({ev.lat}, {ev.lon}, {ev.precision})"
        old_cell = ev.h3_3
        ev.lat, ev.lon = doc.lat, doc.lon
        ev.precision, ev.place_name = doc.place_precision, doc.place_name
        ev.geo_confidence = doc.place_confidence
        ev.h3_3, ev.h3_5, ev.h3_7 = doc.h3_3, doc.h3_5, doc.h3_7
        self._index_geo(ev, old_cell)
        res.fields.append(self._field(ev, "location_before_refinement", before, doc,
                                      "refined by a later article naming a finer place"))
        res.fields.append(self._field(ev, "location", doc.place_name or "", doc,
                                      self._place_quote(doc)))

    @staticmethod
    def _country(doc: ClusterDoc) -> str | None:
        if doc.is_gdelt:
            return str(doc.attrs.get("geo_country") or "") or None  # FIPS 10-4
        for p in doc.places:
            if p.get("name") == doc.place_name and p.get("country"):
                return str(p["country"])
        return None

    @staticmethod
    def _place_quote(doc: ClusterDoc) -> str:
        for p in doc.places:
            if p.get("name") == doc.place_name and p.get("matched"):
                return str(p["matched"])
        return doc.place_name or ""

    @staticmethod
    def _field(ev: EventState, name: str, value: object, doc: ClusterDoc,
               quote: str) -> dict[str, Any]:
        return {"event_id": ev.id, "field": name, "value": str(value), "doc_id": doc.id,
                "quote": (quote or "")[:300]}

    # ------------------------------------------------------------------- driver

    def process(self, docs: Iterable[ClusterDoc], now: datetime) -> ClusterResult:
        """Cluster a batch: articles first (oldest first), then GDELT rows, so GDELT
        candidates can be checked against the news in the same pass."""
        res = ClusterResult()
        self.prune(now)
        before = set(self.events)
        docs = sorted(docs, key=lambda d: (d.is_gdelt, d.published_at, d.id))
        articles = [d for d in docs if not d.is_gdelt]
        vectors = self._embed(articles)
        for doc, vec in zip(articles, vectors, strict=True):
            self._process_article(doc, vec, now, res)
        for doc in docs:
            if doc.is_gdelt:
                self._process_gdelt(doc, now, res)
        res.updated = {eid for eid in res.updated if eid in before} - res.created
        return res

    def _embed(self, docs: list[ClusterDoc]) -> list[Any]:
        """Vectors of the documents that can cluster (relevant, with a category)."""
        if isinstance(self.embedder, NullEmbedder):
            return [None] * len(docs)
        return embed_many(self.embedder, [
            (d.id, embed_text(d.title, d.snippet)
             if d.relevance >= self.cfg.min_relevance and d.categories else "")
            for d in docs])

    def _process_article(self, doc: ClusterDoc, vec: Any, now: datetime,
                         res: ClusterResult) -> None:
        if doc.relevance < self.cfg.min_relevance or not doc.categories:
            res.skip("irrelevant")
            return
        doc.categories = self._gate(doc)
        if not doc.categories:
            res.skip("no_action")
            return
        doc.relocate(self.cfg.min_place_confidence)
        sig = self.hasher.signature(f"{doc.title} {doc.snippet}")
        places, words = doc.place_keys(), doc.words()
        ev, path = self._text_match(doc, sig, places), "text"
        if ev is None:
            ev, path = self._geo_match(doc, places, words, self._geo_candidates(doc)), "geo"
        if ev is None and vec is not None:
            hit = self._semantic_match(doc, vec, places, words, self._semantic_candidates(doc))
            ev, path = (hit[0], "semantic") if hit else (None, "")
        if ev is None:
            if self.cfg.require_location and not doc.located:
                res.skip("unlocated")
                return
            if doc.relevance < self.cfg.min_relevance_create:
                res.skip("below_create_relevance")
                return
            ev = self._new_event(doc, doc.categories[0], now)
            res.created.add(ev.id)
            role = ROLE_PRIMARY
        else:
            res.updated.add(ev.id)
            res.join(path)
            role = ROLE_MENTION if ev.has_article else ROLE_PRIMARY
        self._attach(ev, doc, role, now, res, sig, places, words, vec)
        res.clustered += 1

    def _gate(self, doc: ClusterDoc) -> list[str]:
        """Drop gated categories (e.g. political) that lack a concrete action keyword."""
        if not self._gates:
            return doc.categories
        hits = None
        out = []
        for c in doc.categories:
            if c in self._gated:
                if hits is None:
                    hits = {self._gates.terms[t].group
                            for t in self._gates.find(f"{doc.title} . {doc.snippet}")}
                if c not in hits:
                    continue
            out.append(c)
        return out

    def _gdelt_category(self, doc: ClusterDoc) -> str | None:
        if doc.categories:
            return doc.categories[0]
        a = doc.attrs
        return self.cameo_category(str(a.get("event_code") or ""),
                                   str(a.get("event_root_code") or ""))

    def _decide(self, doc: ClusterDoc, decision: str, reason: str, now: datetime,
                res: ClusterResult, category: str | None,
                event_id: str | None = None) -> None:
        res.decisions.append({
            "gdelt_event_id": doc.origin_id or doc.id, "decision": decision,
            "reason": reason, "event_id": event_id, "doc_id": doc.id,
            "category": category, "decided_at": _iso(now)})
        if decision == "accepted":
            res.gdelt_accepted += 1
        else:
            res.gdelt_rejected[reason] = res.gdelt_rejected.get(reason, 0) + 1

    def _process_gdelt(self, doc: ClusterDoc, now: datetime, res: ClusterResult) -> None:
        g = self.cfg
        a = doc.attrs
        category = self._gdelt_category(doc)
        if now - doc.published_at > timedelta(hours=g.gdelt_stale_h) or self._old_sqldate(doc):
            self._decide(doc, "rejected", "stale", now, res, category)
            return
        code = str(a.get("event_code") or "")
        root = str(a.get("event_root_code") or "")
        tracked = root in g.gdelt_tracked_roots or any(
            code.startswith(c) for c in g.gdelt_tracked_codes)
        if not tracked or category is None:
            self._decide(doc, "rejected", "irrelevant_category", now, res, category)
            return
        if not doc.located:
            self._decide(doc, "rejected", "no_location", now, res, category)
            return
        if category not in doc.categories:
            doc.categories = [category, *doc.categories]
        places, words = doc.place_keys(), doc.words()
        ev = self._geo_match(doc, places, words, self._geo_candidates(doc))
        if ev is not None:
            res.updated.add(ev.id)
            self._attach(ev, doc, ROLE_GDELT, now, res, None, places, words, None)
            self._decide(doc, "accepted", "duplicate_of_event", now, res, category, ev.id)
            res.clustered += 1
            return
        # GDELT never creates an event: no matching article-backed event -> rejected
        self._decide(doc, "rejected", "no_article_backing", now, res, category)

    def _old_sqldate(self, doc: ClusterDoc) -> bool:
        raw = str(doc.attrs.get("sqldate") or "")
        try:
            day = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return False
        return doc.published_at - day > timedelta(days=self.cfg.gdelt_stale_event_days)
