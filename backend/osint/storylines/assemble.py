"""Assemble linked events into storylines (threads), derive roles, titles and theatres.

Assignment, one event at a time in order of best-known time (``Assembler.add``):

1. Candidates come from inverted indexes, never a scan: events sharing a place key (own or
   named), a rare term, a sensor detection, or a ~0.5 degree grid cell (``near``), within
   ``max_gap_h``.
2. Each candidate pair is scored (``link.score_pair``); only *linked* pairs count.
3. A thread accepts the event when the event links to its **recent core** (members within
   ``core_window_h`` of the event's own time), the event's vector is close enough to the
   core centroid (``coherence_min``: the thread may not drift from "drone attack on the
   corvette" to "Libyan politics" one hop at a time) and the thread is younger than
   ``max_span_days``. The best-scoring thread wins; linked singletons are absorbed. Two
   established threads are never merged by one event (conservative: a bridge may be wrong).
4. Sensor-derived events attach afterwards to threads of >= 2 news events (``attach_sensors``).

Recurring background ("another strike on Kyiv") is kept out by the busy-place weight in
``link.place_points``; such events stay single and are counted in their *theatre* (country +
category family) one level up, see ``theatre_of``.

Roles are derived from the final member set, deterministically (``roles``):
  origin              the earliest news event by best-known time
  sensor              a sensor-derived event (earthquake, wildfire alert, outage ...)
  reaction            a statement, political response or commentary (category political,
                      an official source, reaction wording or a question / analysis title)
  follow_up           another kinetic event at a different place (> ``NEAR_KM`` away), or
                      a new attack / action >= ``FOLLOW_UP_H`` after the origin ("second
                      vessel blown up", "struck again")
  development         everything else: the same incident's aftermath (imagery of the
                      damage, casualty updates, "still burning")
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from osint.core.geo import haversine_km
from osint.storylines.config import StorylineConfig
from osint.storylines.link import Corpus, Link, Node, cosine, score_pair, score_sensor

FOLLOW_UP_H = 12.0
KINETIC = {"armed_conflict", "maritime_security", "air_activity", "disaster", "civil_unrest"}
FAMILY = {"armed_conflict": "conflict", "maritime_security": "conflict",
          "air_activity": "conflict", "disaster": "disaster", "civil_unrest": "unrest",
          "political": "politics", "economic": "infrastructure & economy",
          "infrastructure": "infrastructure & economy", "cyber_outage": "infrastructure & economy"}
_REACTION = re.compile(
    r"\b(condemn\w*|concern\w*|statement|calls? (for|on)|urg\w+|warn\w*|denounc\w*|"
    r"react\w*|respon\w+|vows?|demand\w*|deplor\w*|reject\w*|accus\w*|blam\w*|summon\w*|"
    r"investigat\w*|probe|pledg\w*|insists?|claims? responsibility|minist\w+|spokes\w+)\b",
    re.IGNORECASE)
# analysis pieces: a question headline or an explicit genre word
_COMMENTARY = re.compile(r"\?\W*$|^\W*(what|why|how|will|can|is|are|has|have|does|who)\b|"
                         r"\b(analysis|explainer|opinion|interview|expert|podcast)\b",
                         re.IGNORECASE)
# a new attack in the thread ("second vessel blown up", "fresh strikes", "attacked again")
_NEW_ACTION = re.compile(
    r"\b(second|another|new|fresh|renewed|third|further|more|again|repeated)\b.{0,40}?"
    r"\b(attack\w*|strike\w*|struck|hit|shell\w*|bomb\w*|missile\w*|raid\w*|blown up|"
    r"explosion\w*|clash\w*|target\w*)\b|\b(attack\w*|struck|hit|target\w*)\b.{0,20}?\bagain\b",
    re.IGNORECASE)
NEAR_KM = 25.0  # beyond this from the origin a kinetic event is "another place"
ROLE_ORDER = ("origin", "development", "follow_up", "reaction", "sensor")
GRID_DEG = 0.5


def _grid(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / GRID_DEG), math.floor(lon / GRID_DEG)


def is_reaction(n: Node) -> bool:
    return n.origin == "news" and (n.category == "political" or n.official
                                   or bool(_REACTION.search(n.title or "")))


def is_leaf(n: Node) -> bool:
    """A statement, reaction or commentary: it may join a thread, but it never carries the
    thread to a new incident (two condemnations by one government are alike; the
    incidents they condemn are not)."""
    return is_reaction(n) or bool(_COMMENTARY.search(n.title or ""))


def is_kinetic(n: Node) -> bool:
    return n.origin == "news" and n.category in KINETIC and not is_leaf(n)


@dataclass
class Story:
    key: int
    members: dict[str, Node] = field(default_factory=dict)
    links: dict[tuple[str, str], Link] = field(default_factory=dict)
    id: str | None = None        # persistent id (assigned by the job)

    @property
    def news(self) -> list[Node]:
        return [n for n in self.members.values() if n.origin == "news"]

    @property
    def t_first(self) -> datetime:
        return min(n.t for n in self.members.values())

    @property
    def t_last(self) -> datetime:
        return max(max(n.t, n.t_end) for n in self.members.values())

    def core(self, t: datetime, hours: float) -> list[Node]:
        w = timedelta(hours=hours)
        return [n for n in self.members.values() if abs(n.t - t) <= w]


class Assembler:
    """Incremental thread assembly over a time window (see module doc)."""

    def __init__(self, cfg: StorylineConfig, corpus: Corpus | None = None) -> None:
        self.cfg = cfg
        self.lk = cfg.linking
        self.corpus = corpus or Corpus()
        self.nodes: dict[str, Node] = {}
        self.story_of: dict[str, int] = {}
        self.stories: dict[int, Story] = {}
        self._next = 0
        self._by_place: dict[str, set[str]] = {}
        self._by_term: dict[str, set[str]] = {}
        self._by_ref: dict[str, set[str]] = {}
        self._by_cell: dict[tuple[int, int], set[str]] = {}
        self.stats = Counter()

    # ------------------------------------------------------------------ indexes

    def _index(self, nd: Node) -> None:
        for k in nd.place_keys | {k for k in nd.mentioned if k.startswith("p:")}:
            self._by_place.setdefault(k, set()).add(nd.id)
        for t in nd.terms:
            self._by_term.setdefault(t, set()).add(nd.id)
        for r in nd.sensor_refs:
            self._by_ref.setdefault(r, set()).add(nd.id)
        if nd.lat is not None and nd.lon is not None:
            self._by_cell.setdefault(_grid(nd.lat, nd.lon), set()).add(nd.id)

    def candidates(self, nd: Node) -> list[Node]:
        """Indexed events that could link to ``nd`` (anchor-bearing), within max_gap_h."""
        ids: set[str] = set()
        for k in nd.place_keys | {k for k in nd.mentioned if k.startswith("p:")}:
            ids |= self._by_place.get(k, set())
        for t in nd.terms:
            if self.corpus.term_weight(t, self.lk) > 0.05:  # common words are no anchor
                ids |= self._by_term.get(t, set())
        for r in nd.sensor_refs:
            ids |= self._by_ref.get(r, set())
        if nd.lat is not None and nd.lon is not None:
            gy, gx = _grid(nd.lat, nd.lon)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ids |= self._by_cell.get((gy + dy, gx + dx), set())
        ids.discard(nd.id)
        w = timedelta(hours=self.lk.max_gap_h)
        out = [self.nodes[i] for i in sorted(ids) if i in self.nodes]
        return [o for o in out if abs(o.t - nd.t) <= w]

    # ------------------------------------------------------------------ mutation

    def _new_story(self, nd: Node) -> Story:
        s = Story(self._next)
        self._next += 1
        s.members[nd.id] = nd
        self.stories[s.key] = s
        self.story_of[nd.id] = s.key
        return s

    def _move(self, nd: Node, dst: Story) -> None:
        src = self.stories.get(self.story_of.get(nd.id, -1))
        if src is not None and src is not dst:
            src.members.pop(nd.id, None)
            for k in [k for k in src.links if nd.id in k]:
                dst.links[k] = src.links.pop(k)
            if not src.members:
                del self.stories[src.key]
        dst.members[nd.id] = nd
        self.story_of[nd.id] = dst.key

    def _coherent(self, nd: Node, core: list[Node]) -> tuple[bool, float | None]:
        vecs = [n.vector for n in core if n.vector is not None]
        if nd.vector is None or not vecs:
            return True, None
        c = cosine(nd.vector, np.mean(vecs, axis=0))
        return (c is None or c >= self.lk.coherence_min), c

    def _far(self, nd: Node, story: Story) -> bool:
        """A kinetic event at a specific place more than ``far_km`` from every precisely
        located member, with no place named across (the thread's documents never name its
        place, it never names theirs): another incident of the same kind, not this thread
        (a drone boat off Benghazi is not the drone attack in the Gulf of Finland)."""
        if not is_kinetic(nd) or not nd.located_fine:
            return False
        fine = [m for m in story.news if m.located_fine]
        if not fine or min(haversine_km(nd.lat, nd.lon, m.lat, m.lon) for m in fine)                 <= self.lk.far_km:
            return False
        base = [m for m in story.news if not is_leaf(m)]  # statements bring no new places
        named = set().union(*(m.mentioned for m in base))
        theirs = set().union(*(m.place_keys for m in base))
        return not (nd.place_keys & named or nd.mentioned & theirs)

    def add(self, nd: Node) -> Story:
        """Index ``nd`` (a news event) and put it in its best thread (or a new one)."""
        self.nodes[nd.id] = nd
        links: list[tuple[Node, Link]] = []
        for other in self.candidates(nd):
            if other.origin != "news":
                continue
            a, b = (other, nd) if (other.t, other.id) <= (nd.t, nd.id) else (nd, other)
            lk = score_pair(a, b, self.corpus, self.lk)
            self.stats["pairs_scored"] += 1
            if lk is not None and lk.linked:
                links.append((other, lk))
        self._index(nd)
        by_story: dict[int, list[tuple[Node, Link]]] = {}
        for other, lk in links:
            by_story.setdefault(self.story_of[other.id], []).append((other, lk))
        best: tuple[float, int] | None = None
        span = timedelta(days=self.lk.max_span_days)
        for key, hits in by_story.items():
            s = self.stories[key]
            if len(s.members) < 2:
                continue  # singletons are absorbed below
            core = {n.id for n in s.core(nd.t, self.lk.core_window_h)}
            core_hits = [(o, lk) for o, lk in hits if o.id in core]
            if not core_hits:
                self.stats["rejected_not_core"] += 1
                continue
            if is_kinetic(nd):
                core_hits = [(o, lk) for o, lk in core_hits if not is_leaf(o)]
                if not core_hits:
                    self.stats["rejected_via_statement"] += 1
                    continue
            if nd.t - s.t_first > span or s.t_last - nd.t > span:
                self.stats["rejected_span"] += 1
                continue
            ok, _ = self._coherent(nd, [s.members[i] for i in core])
            if not ok:
                self.stats["rejected_drift"] += 1
                continue
            if self._far(nd, s):
                self.stats["rejected_far"] += 1
                continue
            top = max(lk.score for _, lk in core_hits)
            if best is None or (top, -key) > (best[0], -best[1]):
                best = (top, key)
        story = self.stories[best[1]] if best else self._new_story(nd)
        if best:
            self._move(nd, story)
        # absorb linked singletons (one event each: never merges two established threads)
        for key, hits in sorted(by_story.items()):
            s = self.stories.get(key)
            if s is None or s is story or len(s.members) != 1:
                continue
            (other, _), = hits[:1]
            if is_kinetic(other) and is_leaf(nd):
                self.stats["rejected_via_statement"] += 1  # same rule as joining
                continue
            if self._far(other, story):
                self.stats["rejected_far"] += 1
                continue
            ok, _ = self._coherent(other, [nd, *story.core(other.t, self.lk.core_window_h)])
            if ok:
                self._move(other, story)
        for other, lk in links:
            if self.story_of.get(other.id) == story.key:
                story.links[(lk.a, lk.b)] = lk
        return story

    def attach_sensors(self, sensors: Iterable[Node]) -> int:
        """Attach sensor-derived events to the best thread of >= ``min_members`` news events
        (never to single events: the event's own corroboration already shows those)."""
        n = 0
        for sn in sorted(sensors, key=lambda x: (x.t, x.id)):
            self.nodes[sn.id] = sn
            best: tuple[float, Story, Link] | None = None
            for s in self.stories.values():
                if len(s.news) < self.cfg.min_members:
                    continue
                for m in s.news:
                    if is_leaf(m):
                        continue  # sensors corroborate incidents, not statements
                    lk = score_sensor(sn, m, self.cfg.sensor, self.lk)
                    if lk is not None and (best is None or (lk.score, -s.key) > (best[0],
                                                                                  -best[1].key)):
                        best = (lk.score, s, lk)
            if best is None:
                continue
            _, s, lk = best
            s.members[sn.id] = sn
            self.story_of[sn.id] = s.key
            s.links[(lk.a, lk.b)] = lk
            n += 1
        return n

    def threads(self) -> list[Story]:
        """Stories with at least ``min_members`` news events."""
        return [s for s in self.stories.values() if len(s.news) >= self.cfg.min_members]


def build(nodes: Iterable[Node], cfg: StorylineConfig) -> Assembler:
    """Full deterministic assembly: corpus over all nodes, news in time order, then sensors."""
    nodes = list(nodes)
    asm = Assembler(cfg, Corpus.of(nodes))
    for nd in sorted((n for n in nodes if n.origin == "news"), key=lambda n: (n.t, n.id)):
        asm.add(nd)
    asm.attach_sensors(n for n in nodes if n.origin == "sensor")
    return asm


# ---------------------------------------------------------------------- derived facts


def roles(story: Story) -> dict[str, tuple[str, str]]:
    """event id -> (role, basis). Deterministic: time order, category, source type."""
    out: dict[str, tuple[str, str]] = {}
    news = sorted(story.news, key=lambda n: (n.t, n.id))
    if not news:
        return out
    origin = news[0]
    out[origin.id] = ("origin", "earliest event of the thread by best-known time")
    for n in story.members.values():
        if n.origin == "sensor":
            out[n.id] = ("sensor", f"sensor-derived event ({n.detector})")
    for n in news[1:]:
        dt = (n.t - origin.t).total_seconds() / 3600
        if n.category == "political":
            out[n.id] = ("reaction", "political statement / response after the origin")
        elif n.official:
            out[n.id] = ("reaction", "official source")
        elif _REACTION.search(n.title or ""):
            out[n.id] = ("reaction", "reaction wording in the title: “"
                         + _REACTION.search(n.title).group(0) + "”")
        elif _COMMENTARY.search(n.title or ""):
            out[n.id] = ("reaction", "analysis / commentary on the thread")
        elif n.category in KINETIC and n.located_fine and origin.located_fine \
                and not n.place_keys & origin.place_keys \
                and haversine_km(n.lat, n.lon, origin.lat, origin.lon) > NEAR_KM:
            out[n.id] = ("follow_up",
                         f"another {n.category.replace('_', ' ')} event at {n.place_name}")
        elif n.category in KINETIC and dt >= FOLLOW_UP_H and _NEW_ACTION.search(n.title or ""):
            said = _NEW_ACTION.search(n.title).group(0)[:40]
            out[n.id] = ("follow_up", f"a new attack {dt:.0f} h after the origin: “{said}”")
        else:
            out[n.id] = ("development",
                         f"same incident, {dt:+.0f} h from the origin (aftermath, updates)")
    return out


def representative(story: Story) -> Node:
    """The most-reported news member (outlets, then reports, then earliest)."""
    return max(story.news, key=lambda n: (n.sources, n.mentions, -n.t.timestamp(), n.id))


def theatre_of(story: Story, country_names: dict[str, str] | None = None
               ) -> tuple[str, str]:
    """(key, label) one level up: the dominant country of the located news members
    (weighted by reports) and the category family of the representative event."""
    w: Counter = Counter()
    for n in story.news:
        if n.country:
            w[n.country.upper()] += max(1, n.mentions)
    cc = w.most_common(1)[0][0] if w else "XX"
    fam = FAMILY.get(representative(story).category, "other")
    name = (country_names or {}).get(cc) or next(
        (n.place_name for n in story.news if (n.country or "").upper() == cc
         and n.precision == "country" and n.place_name), None) or (
        "unlocated" if cc == "XX" else cc)
    return f"{cc}:{fam}", f"{name} · {fam}"


def fingerprint(record: dict) -> str:
    return hashlib.sha1(json.dumps(record, sort_keys=True, default=str).encode(
        "utf-8")).hexdigest()[:16]
