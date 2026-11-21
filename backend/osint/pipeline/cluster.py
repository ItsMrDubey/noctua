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

An optional ``Embedder`` (multilingual sentence embeddings, spec §2.6) adds a semantic path
among the geo/time candidates; the default is a no-op.

Otherwise a new candidate event is created. GDELT rows only take the geo path (their titles
are synthetic) and are accepted (attached, or a new event) or rejected with a reason:
``stale``, ``irrelevant_category``, ``no_location``, ``country_centroid_only``,
``low_mentions``. Every decision is returned for the ``gdelt_decisions`` table.

Pure CPU and deterministic for a given input order; the caller persists the results.
"""

from __future__ import annotations

import base64
import math
import os
import re
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import h3

from osint.core.geo import valid_coord
from osint.enrich.gazetteer import fold
from osint.pipeline.minhash import LSHIndex, MinHasher, jaccard

if TYPE_CHECKING:
    from osint.pipeline.events import EventsConfig

PRECISION_RANK = {"point": 3, "city": 2, "admin1": 1, "country": 0}
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
        if self.is_gdelt:
            a = self.attrs
            return title_tokens(f"{a.get('actor1') or ''} {a.get('actor2') or ''}")
        return title_tokens(self.title)

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
    vector: list[float] | None = None
    # hooks for M3/M4 status levels
    sensor_corroborated: bool = False
    official: bool = False

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
                "has_article": self.has_article, "vector": self.vector,
                "sensor_corroborated": self.sensor_corroborated, "official": self.official,
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
            has_article=bool(st.get("has_article")), vector=st.get("vector"),
            sensor_corroborated=bool(st.get("sensor_corroborated")),
            official=bool(st.get("official")),
        )


# ------------------------------------------------------------------------ embedding


class Embedder(Protocol):
    """Pluggable sentence embedder (e.g. multilingual-e5-small via ONNX, spec §2.6)."""

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


class NullEmbedder:
    """Default: no embeddings, so the semantic path is skipped."""

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=False))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


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

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def changed(self) -> set[str]:
        return self.created | self.updated


Scorer = Callable[[EventState, datetime], tuple[str, float]]


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

    # --------------------------------------------------------------- index upkeep

    def _index_geo(self, ev: EventState, old_cell: int | None) -> None:
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
            ok = (place_shared and shared >= need) or (shared - place_shared) >= max(2, need)
            if not ok or ratio < self.cfg.overlap_threshold:
                continue
            key = (shared, ratio, ev.t_end)
            if best_key is None or key > best_key:
                best, best_key = ev, key
        return best

    def _semantic_match(self, vec: list[float] | None,
                        candidates: list[EventState]) -> EventState | None:
        if vec is None:
            return None
        best, best_sim = None, self.cfg.embed_threshold
        for ev in candidates:
            if ev.vector is not None:
                sim = _cosine(vec, ev.vector)
                if sim >= best_sim:
                    best, best_sim = ev, sim
        return best

    # ---------------------------------------------------------------- mutation

    def _new_event(self, doc: ClusterDoc, category: str, now: datetime) -> EventState:
        ev = EventState(id=new_ulid(now), category=category, t_start=doc.published_at,
                        t_end=doc.published_at, first_seen=now, updated_at=now,
                        title=doc.title, summary=doc.snippet)
        self._add_event(ev)
        return ev

    def _attach(self, ev: EventState, doc: ClusterDoc, role: str, now: datetime,
                res: ClusterResult, sig: tuple[int, ...] | None, places: set[str],
                words: set[str], vec: list[float] | None) -> None:
        cfg = self.cfg
        ev.version += 1
        ev.mention_count += 1
        ev.domains.add(doc.domain)
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
            n = ev.mention_count
            ev.vector = vec if ev.vector is None else [
                (a * (n - 1) + b) / n for a, b in zip(ev.vector, vec, strict=False)]
        # location: keep the most specific, then most confident place
        if doc.located:
            new_rank = (PRECISION_RANK.get(doc.place_precision or "", 0),
                        doc.place_confidence or 0.0)
            old_rank = (PRECISION_RANK.get(ev.precision or "", -1), ev.geo_confidence or 0.0)
            if ev.lat is None or new_rank > old_rank:
                old_cell = ev.h3_3
                ev.lat, ev.lon = doc.lat, doc.lon
                ev.precision, ev.place_name = doc.place_precision, doc.place_name
                ev.geo_confidence = doc.place_confidence
                ev.country = self._country(doc)
                ev.h3_3, ev.h3_5, ev.h3_7 = doc.h3_3, doc.h3_5, doc.h3_7
                self._index_geo(ev, old_cell)
                res.fields.append(self._field(ev, "location", doc.place_name or "", doc,
                                              self._place_quote(doc)))
        # title / summary: best article (relevance, English preferred for display)
        if not doc.is_gdelt:
            score = doc.relevance + (0.05 if doc.language == "en" else 0.0)
            if not ev.has_article or score > ev.title_score:
                ev.title, ev.summary = doc.title, doc.snippet
                ev.title_score = score
            ev.has_article = True
        elif not ev.has_article and not ev.title:
            ev.title, ev.summary = doc.title, doc.snippet
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

    def _embed(self, docs: list[ClusterDoc]) -> list[list[float] | None]:
        if not docs:
            return []
        vecs = self.embedder.embed([f"{d.title}. {d.snippet}" for d in docs])
        if vecs is None or len(vecs) != len(docs):
            return [None] * len(docs)
        return list(vecs)

    def _process_article(self, doc: ClusterDoc, vec: list[float] | None, now: datetime,
                         res: ClusterResult) -> None:
        if doc.relevance < self.cfg.min_relevance or not doc.categories:
            res.skip("irrelevant")
            return
        sig = self.hasher.signature(f"{doc.title} {doc.snippet}")
        places, words = doc.place_keys(), doc.words()
        ev = self._text_match(doc, sig, places)
        if ev is None:
            candidates = self._geo_candidates(doc)
            ev = self._geo_match(doc, places, words, candidates) \
                or self._semantic_match(vec, candidates)
        if ev is None:
            if self.cfg.require_location and not doc.located:
                res.skip("unlocated")
                return
            ev = self._new_event(doc, doc.categories[0], now)
            res.created.add(ev.id)
            role = ROLE_PRIMARY
        else:
            res.updated.add(ev.id)
            role = ROLE_MENTION if ev.has_article else ROLE_PRIMARY
        self._attach(ev, doc, role, now, res, sig, places, words, vec)
        res.clustered += 1

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
        geo_type = a.get("geo_type")
        if geo_type == 1 or doc.place_precision == "country":
            self._decide(doc, "rejected", "country_centroid_only", now, res, category)
            return
        try:
            mentions = int(a.get("num_mentions") or 0)
        except (TypeError, ValueError):
            mentions = 0
        if mentions < g.gdelt_min_mentions:
            self._decide(doc, "rejected", "low_mentions", now, res, category)
            return
        ev = self._new_event(doc, category, now)
        res.created.add(ev.id)
        self._attach(ev, doc, ROLE_GDELT, now, res, None, places, words, None)
        self._decide(doc, "accepted", "new_event", now, res, category, ev.id)
        res.clustered += 1

    def _old_sqldate(self, doc: ClusterDoc) -> bool:
        raw = str(doc.attrs.get("sqldate") or "")
        try:
            day = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return False
        return doc.published_at - day > timedelta(days=self.cfg.gdelt_stale_event_days)
