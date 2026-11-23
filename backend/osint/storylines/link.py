"""Pair scoring for storylines: may two *different* events belong to one narrative thread?

Same-event clustering (pipeline.cluster) asks "is this the same incident?" with cosine
thresholds around 0.85-0.92. A storyline links distinct events - the attack, the fire a
satellite saw at the site, the imagery of the burning ship, the UN statement, the follow-up
strike - so the cosine needed is lower and other evidence carries more weight. Every link
is a sum of points, each with a human-readable reason:

  cosine       multilingual centroid cosine (pipeline.embed vectors stored per event),
               0 points at ``cos_floor``, ``cos_points`` at ``cos_full``
  place        the best of: the same specific place (place name or H3 res-5 cell), both at
               city/point precision within ``near_km``, one event's documents naming the
               other's place ("UN concerned about the attacks on Benghazi port"), or both
               naming the same place. A *busy* place (``busy_place_events`` events located
               there or naming it, on ``busy_place_days`` different days: Kyiv, Gaza City)
               counts a quarter: "another strike on Kyiv" is the background, not a thread;
               one incident's burst of coverage on one day does not make its place busy
  terms        words shared by the English titles and actor names, each weighted by how
               rare it is in the window (``merkury`` counts, ``drone`` not) and whether it
               is a *name*: capitalised mid-sentence in sentence-case English titles
               ("Russian corvette Merkury has sunk"); other words weigh ``generic_term``
  sensor       both events cite the same sensor detection (corroborations ref id)
  time         minus ``gap_penalty`` x gap / ``max_gap_h``, the gap measured between the two
               events' activity windows (best-known time to last report)

Hard gates: compatible categories (``LinkConfig.compatible``), a gap of at most
``max_gap_h``, and an *anchor*: place, term or sensor points of at least ``anchor_min``.
The cosine alone never links (two drone strikes in two countries are not one story).

Sensor-derived events (earthquake, wildfire alert, internet outage) have no text: they link
to a news event by place and time only (``score_sensor``).

Pure functions over ``Node`` records; ``features.py`` builds the nodes from the stores.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from osint.core.geo import haversine_km
from osint.storylines.config import LinkConfig, SensorConfig

FINE = ("city", "point")


@dataclass(slots=True)
class Node:
    """One event as the linker sees it."""

    id: str
    origin: str                    # news | sensor
    category: str
    t: datetime                    # best-known time (occurred band start, else first report)
    t_end: datetime
    title: str = ""
    lat: float | None = None
    lon: float | None = None
    precision: str | None = None
    place_name: str | None = None
    country: str | None = None
    own: frozenset[str] = frozenset()        # its own location: place key, H3 res-5 cell
    mentioned: frozenset[str] = frozenset()  # place keys its documents name ("p:benghazi")
    terms: frozenset[str] = frozenset()      # folded content words + actor names
    names: frozenset[str] = frozenset()      # terms seen capitalised mid-sentence
    lower: frozenset[str] = frozenset()      # terms seen lower-case mid-sentence
    vector: np.ndarray | None = None
    sensor_refs: frozenset[str] = frozenset()  # "fire:<observation id>" ...
    detector: str | None = None    # sensor events: pipeline.sensor_events detector
    mentions: int = 0
    sources: int = 0
    status: str = ""
    t_basis: str = "first_report"  # occurred_claims | first_report | sensor_observed
    t_precision: str | None = None
    t_lo: datetime | None = None
    t_hi: datetime | None = None
    first_report: datetime | None = None
    official: bool = False
    actors: tuple[str, ...] = ()
    updated: str = ""              # the event row's updated_at (incremental passes)

    @property
    def located_fine(self) -> bool:
        return self.lat is not None and (self.precision or "") in FINE

    @property
    def place_keys(self) -> set[str]:
        """Own place-name keys (no H3 cells)."""
        return {k for k in self.own if k.startswith("p:")}


@dataclass
class Corpus:
    """Document frequencies over the window: how common a term / located place is."""

    n: int = 0
    term_df: Counter = field(default_factory=Counter)
    place_df: Counter = field(default_factory=Counter)
    name_df: Counter = field(default_factory=Counter)
    lower_df: Counter = field(default_factory=Counter)
    place_days: dict = field(default_factory=dict)   # place key -> set of UTC dates

    @classmethod
    def of(cls, nodes: Iterable[Node]) -> Corpus:
        c = cls()
        for nd in nodes:
            if nd.origin != "news":
                continue
            c.add(nd)
        return c

    def add(self, nd: Node) -> None:
        self.n += 1
        self.term_df.update(nd.terms)
        keys = nd.own | nd.mentioned
        self.place_df.update(keys)
        day = nd.t.date()
        for k in keys:
            self.place_days.setdefault(k, set()).add(day)
        self.name_df.update(nd.names)
        self.lower_df.update(nd.lower)

    def is_name(self, term: str) -> bool:
        """Capitalised mid-sentence at least as often as lower-case (actor names count as
        capitalised)."""
        n = self.name_df.get(term, 0)
        return n > 0 and n >= self.lower_df.get(term, 0)

    def term_weight(self, term: str, cfg: LinkConfig) -> float:
        """1 for a rare term (df <= term_df_full), 0 for a common one (df >= term_df_zero x
        events), log-linear in between."""
        df = max(1, self.term_df.get(term, 0))
        hi = max(cfg.term_df_full + 1.0, cfg.term_df_zero * max(1, self.n))
        if df <= cfg.term_df_full:
            return 1.0
        if df >= hi:
            return 0.0
        return 1.0 - (math.log(df) - math.log(cfg.term_df_full)) / (
            math.log(hi) - math.log(cfg.term_df_full))

    def busy(self, keys: Iterable[str], cfg: LinkConfig) -> bool:
        """A background place: many events (located there or naming it) on many days.
        One incident's burst of coverage (many events, one or two days) is not busy."""
        return any(self.place_df.get(k, 0) >= cfg.busy_place_events
                   and len(self.place_days.get(k, ())) >= cfg.busy_place_days for k in keys)


@dataclass
class Link:
    a: str
    b: str
    score: float
    linked: bool
    reasons: list[dict]          # {kind, text, points}
    parts: dict[str, float]      # kind -> points (calibration)

    @property
    def text(self) -> str:
        return "; ".join(r["text"] for r in self.reasons)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None:
        return None
    den = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
    return float(np.asarray(a) @ np.asarray(b)) / den if den else None


def gap_h(a: Node, b: Node) -> float:
    """Hours between the two events' activity windows [t, t_end] (best-known time to the
    last report): 0 when one was still being reported when the other began (a statement
    issued while the attack is still in the news is close to it, whatever its start)."""
    lo, hi = (a, b) if a.t <= b.t else (b, a)
    return max(0.0, (hi.t - max(lo.t, lo.t_end)).total_seconds() / 3600.0)


def offset_h(a: Node, b: Node) -> float:
    """Start-to-start hours from a to b (signed): the order shown in the reasons."""
    return (b.t - a.t).total_seconds() / 3600.0


def fmt_gap(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.1f} d"


def _key_label(key: str) -> str:
    return key.removeprefix("p:")


def place_points(a: Node, b: Node, corpus: Corpus, cfg: LinkConfig
                 ) -> tuple[float, dict | None]:
    """Best place relation of two events: (points, reason) or (0, None); a negative value
    for two distinct specific places far apart that neither names (the topic may match,
    the place says it is another incident)."""
    best: tuple[float, dict | None] = (0.0, None)

    def consider(pts: float, kind: str, text: str, keys: Iterable[str]) -> None:
        nonlocal best
        busy = corpus.busy(keys, cfg)
        if busy:
            pts *= 0.25
            text += " (busy place: weighs 1/4)"
        if pts > best[0]:
            best = (pts, {"kind": kind, "text": text, "points": round(pts, 2)})

    shared = a.own & b.own
    if shared:
        name = a.place_name if a.place_keys & b.place_keys else (a.place_name or b.place_name)
        consider(cfg.same_place_points, "same_place", f"same place: {name or 'same H3 cell'}",
                 shared)
    d = None
    if a.located_fine and b.located_fine:
        d = haversine_km(a.lat, a.lon, b.lat, b.lon)
        if d <= cfg.near_km and not shared:
            consider(cfg.near_points * (1.0 - 0.5 * d / cfg.near_km), "near",
                     f"{d:.1f} km apart ({a.place_name} / {b.place_name})", a.own | b.own)
    for x, y in ((a, b), (b, a)):
        hit = sorted(x.place_keys & y.mentioned)
        if hit:
            consider(cfg.mention_points, "mentions",
                     f"“{_short(y.title)}” names {_key_label(hit[0]).title()}", hit)
    both = sorted(k for k in a.mentioned & b.mentioned if k.startswith("p:"))
    if both:
        consider(cfg.mention_points * 0.75, "both_name",
                 f"both name {_key_label(both[0]).title()}", both)
    if best[1] is None and a.country and b.country and a.country.upper() == b.country.upper() \
            and "country" in (a.precision, b.precision):
        best = (cfg.country_points, {"kind": "country", "text": f"same country ({a.country})",
                                     "points": cfg.country_points})
    if best[1] is None and d is not None and d > 300 and a.place_keys and b.place_keys:
        best = (-1.0, {"kind": "far", "text": f"different places {d:.0f} km apart "
                                              f"({a.place_name} / {b.place_name})",
                       "points": -1.0})
    return best


def _short(title: str, n: int = 48) -> str:
    t = " ".join((title or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def term_points(a: Node, b: Node, corpus: Corpus, cfg: LinkConfig) -> tuple[float, dict | None]:
    shared = a.terms & b.terms
    if not shared:
        return 0.0, None
    weighted = sorted(((corpus.term_weight(t, cfg) * (
        cfg.name_term if corpus.is_name(t) else cfg.generic_term), t) for t in shared),
        reverse=True)
    weighted = [(w, t) for w, t in weighted if w > 0.05]
    if not weighted:
        return 0.0, None
    pts = min(cfg.term_points_max, sum(w for w, _ in weighted))
    shown = [t.split(":", 1)[-1] for _, t in weighted[:5]]
    names = [x.title() for x, (_, t) in zip(shown, weighted, strict=False) if corpus.is_name(t)]
    words = [x for x, (_, t) in zip(shown, weighted, strict=False) if not corpus.is_name(t)]
    text = "; ".join(x for x in ("shared names: " + ", ".join(names) if names else "",
                                 "shared words: " + ", ".join(words) if words else "") if x)
    return pts, {"kind": "terms", "text": text, "points": round(pts, 2)}


def score_pair(a: Node, b: Node, corpus: Corpus, cfg: LinkConfig) -> Link | None:
    """Link two news events, or None when a hard gate fails (category, gap)."""
    if not cfg.compatible(a.category, b.category):
        return None
    gap = gap_h(a, b)
    if gap > cfg.max_gap_h:
        return None
    reasons: list[dict] = []
    parts: dict[str, float] = {}
    c = cosine(a.vector, b.vector)
    if c is not None:
        pts = cfg.cos_points * min(1.0, max(0.0, (c - cfg.cos_floor) / (
            cfg.cos_full - cfg.cos_floor)))
        parts["cosine"] = pts
        reasons.append({"kind": "cosine", "text": f"cosine {c:.2f}", "points": round(pts, 2)})
    pp, pr = place_points(a, b, corpus, cfg)
    if pr:
        parts["place"] = pp
        reasons.append(pr)
    tp, tr = term_points(a, b, corpus, cfg)
    if tr:
        parts["terms"] = tp
        reasons.append(tr)
    refs = sorted(a.sensor_refs & b.sensor_refs)
    sp = 0.0
    if refs:
        sp = parts["sensor"] = cfg.sensor_ref_points
        reasons.append({"kind": "sensor", "text": f"same sensor detection: {refs[0]}"
                        + (f" (+{len(refs) - 1})" if len(refs) > 1 else ""), "points": sp})
    penalty = cfg.gap_penalty * gap / cfg.max_gap_h if cfg.max_gap_h else 0.0
    parts["time"] = -penalty
    off = offset_h(a, b)
    text = ("+" if off >= 0 else "−") + fmt_gap(abs(off))
    if gap < abs(off) - 1:
        text += " (first still in the news)" if gap == 0 else f" ({fmt_gap(gap)} after its last report)"
    reasons.append({"kind": "time", "text": text, "points": round(-penalty, 2)})
    if a.category != b.category:
        reasons.append({"kind": "category", "text": f"{a.category} ~ {b.category}",
                        "points": 0.0})
    score = sum(parts.values())
    anchor = max(0.0, pp) + tp + sp
    linked = score >= cfg.link_threshold and anchor >= cfg.anchor_min
    return Link(a.id, b.id, round(score, 3), linked, reasons, parts)


def score_sensor(sensor: Node, news: Node, scfg: SensorConfig, cfg: LinkConfig) -> Link | None:
    """A short-lived sensor event close to a news event in place and time (no text: the
    place and time are the whole case). None when it does not qualify."""
    if sensor.detector not in scfg.detectors:
        return None
    if (sensor.t_end - sensor.t).total_seconds() / 3600 > scfg.max_span_h:
        return None
    if not cfg.compatible(sensor.category, news.category):
        return None
    dt = (sensor.t - news.t).total_seconds() / 3600
    if dt < -scfg.before_h or dt > scfg.after_h:
        return None
    reasons: list[dict] = []
    if sensor.lat is not None and news.located_fine and sensor.precision != "country":
        d = haversine_km(sensor.lat, sensor.lon, news.lat, news.lon)
        if d > scfg.near_km:
            return None
        pts = cfg.same_place_points * (1.0 - 0.5 * d / scfg.near_km)
        reasons.append({"kind": "near", "text": f"{sensor.detector} {d:.1f} km from "
                                                f"{news.place_name}", "points": round(pts, 2)})
    elif sensor.country and news.country and sensor.country.upper() == news.country.upper() \
            and (sensor.precision == "country" or news.precision == "country"):
        pts = cfg.same_place_points * 0.5
        reasons.append({"kind": "country", "text": f"{sensor.detector} in the same country "
                                                   f"({news.country})", "points": pts})
    else:
        return None
    sign = "+" if dt >= 0 else "−"
    penalty = cfg.gap_penalty * abs(dt) / max(scfg.after_h, scfg.before_h, 1.0)
    reasons.append({"kind": "time", "text": f"{sign}{fmt_gap(abs(dt))}",
                    "points": round(-penalty, 2)})
    score = cfg.link_threshold + pts - penalty
    return Link(news.id, sensor.id, round(score, 3), True, reasons,
                {"place": pts, "time": -penalty})
