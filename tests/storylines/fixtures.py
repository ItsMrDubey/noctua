"""Small storyline fixtures: a Benghazi-like thread, a distant look-alike incident and a
recurring background place, as linker nodes (pure tests) or as event rows (store / API)."""

import math
from datetime import UTC, datetime, timedelta

import numpy as np

from osint.storylines.features import own_places, terms_of
from osint.storylines.link import Node

T0 = datetime(2026, 11, 21, 23, 0, tzinfo=UTC)
DIM = 16


def vec(angle_deg: float, axis: int = 1) -> np.ndarray:
    """Unit vector in the plane of dimensions 0 and ``axis``: the cosine of two vectors on
    the same plane is cos(angle difference)."""
    v = np.zeros(DIM, dtype=np.float32)
    v[0], v[axis] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


def node(id: str, hours: float, title: str, *, category: str = "armed_conflict",
         lat: float | None = None, lon: float | None = None, precision: str | None = "city",
         place: str | None = None, country: str | None = None, angle: float = 0.0,
         axis: int = 1, mentioned: tuple[str, ...] = (), origin: str = "news",
         detector: str | None = None, span_h: float = 0.0, refs: tuple[str, ...] = (),
         mentions: int = 3, sources: int = 2, h3_5: str | None = None) -> Node:
    t = T0 + timedelta(hours=hours)
    row = {"lat": lat, "lon": lon, "precision": precision, "place_name": place,
           "h3_5": h3_5 or (f"cell-{place}" if place and lat is not None else None)}
    own = own_places(row)
    place_words = {w for k in set(mentioned) | own if k.startswith("p:") for w in k[2:].split()}
    terms, names, lower = terms_of(title, [], place_words,
                                   {k[2:] for k in set(mentioned) | own if k.startswith("p:")})
    return Node(id=id, origin=origin, category=category, t=t, t_end=t + timedelta(hours=span_h),
                title=title, lat=lat, lon=lon, precision=precision, place_name=place,
                country=country, own=own, mentioned=frozenset(mentioned), terms=terms,
                names=names, lower=lower,
                vector=None if origin == "sensor" else vec(angle, axis),
                sensor_refs=frozenset(refs), detector=detector, mentions=mentions,
                sources=sources, status="reported", first_report=t, updated="u1")


BENGHAZI = (32.11, 20.07)


def benghazi_thread() -> list[Node]:
    lat, lon = BENGHAZI
    return [
        node("E1", 0, "Suspected drone attack caused an explosion on the Russian corvette "
             "Merkury at Benghazi port", lat=lat, lon=lon, precision="point",
             place="Benghazi Naval Base", country="LY", mentioned=("p:benghazi",),
             mentions=70, sources=52),
        node("E2", 15, "Satellite images show the burning corvette Merkury in the port",
             category="maritime_security", lat=lat + 0.01, lon=lon, place="Benghazi",
             country="LY", angle=30, mentioned=("p:benghazi",)),
        node("E3", 19, "Second vessel Ascalon struck by a sea drone off the Libyan coast",
             category="maritime_security", lat=lat + 0.05, lon=lon - 0.05, place="Benghazi",
             country="LY", angle=25, mentioned=("p:benghazi",)),
        node("E4", 25, "UN mission expresses deep concern over the attacks on Benghazi port",
             category="political", lat=lat, lon=lon, place="Benghazi", country="LY",
             angle=40, mentioned=("p:benghazi",)),
    ]


def finland_lookalike() -> list[Node]:
    # the same kind of incident (a drone attack on a Russian ship), 2,900 km away
    return [node("F1", 2, "Russian cargo ship Sparta struck by drones in the Gulf of Finland",
                 lat=60.2, lon=26.9, precision="city", place="Kotka", country="FI", angle=8),
            node("F2", 6, "Fire on the cargo ship Sparta still burning off Kotka",
                 category="maritime_security", lat=60.2, lon=26.9, place="Kotka",
                 country="FI", angle=12)]


def kyiv_background(n: int = 8) -> list[Node]:
    # a strike on the same city every night: one background, not one story
    return [node(f"K{i}", 24 * i - 70, f"Russian drone attack on Kyiv overnight, district {i} hit",
                 lat=50.45, lon=30.52, place="Kyiv", country="UA", angle=-60 + i,
                 axis=2) for i in range(n)]


def as_row(n: Node, *, now: datetime) -> dict:
    """An events-table row for a node (SqliteStore.save_events)."""
    iso = (lambda d: d.astimezone(UTC).isoformat(timespec="milliseconds"))
    import base64
    vec_b64 = None if n.vector is None else base64.b64encode(
        np.asarray(n.vector, dtype=np.float16).tobytes()).decode("ascii")
    return {"id": n.id, "category": n.category, "title": n.title, "summary": "",
            "actors": [], "lat": n.lat, "lon": n.lon, "precision": n.precision,
            "place_name": n.place_name, "country": n.country, "geo_confidence": 0.9,
            "h3_3": None, "h3_5": None, "h3_7": None, "t_start": iso(n.t), "t_end": iso(n.t_end),
            "status": "multi-source", "confidence": 0.7, "version": 1,
            "mention_count": n.mentions, "source_diversity": n.sources,
            "first_seen": iso(n.t), "updated_at": iso(now),
            "origin": n.origin,
            "cluster_state": {"vector": vec_b64, "places": sorted(n.mentioned), "words": []}}
