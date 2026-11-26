"""Shared fixture for the country context tests (spec B §5).

Seven events seen from Ukraine and Russia. Sub-project A's columns (actor_countries,
foreign_involved, attributed, lifecycle) and the actors are written with SQL, so the fixture
does not depend on A's write path.

  E1  UA, actors UA          inside + domestic for UA
  E2  UA, actors RU + UA     Russian strike in Ukraine: UA inside+foreign, RU abroad+foreign
  E3  RU, actors UA          Ukrainian strike in Russia: UA abroad+foreign, RU inside+foreign
  E4  UA, no actors          unattributed (category disaster)
  E5  FR, actors FR          unrelated
  E6  PL, actors PL + UA     UA abroad+foreign, counterpart PL
  H1  UA, actors UA          historical (UCDP): never in the country view
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from osint.store.sqlite import SqliteStore, iso

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

# id: (country, actor ISO2s, lifecycle, minutes before T0 (updated_at), category, actors)
CASES = {
    "E1": ("UA", ["UA"], "developing", 10, "armed_conflict",
           [{"name": "Ukrainian police", "country": "UA"}]),
    "E2": ("UA", ["RU", "UA"], "active", 20, "armed_conflict",
           [{"name": "Russian forces", "country": "RU"},
            {"name": "Ukrainian army", "country": "UA"}]),
    "E3": ("RU", ["UA"], "cooling", 30, "armed_conflict",
           [{"name": "Ukrainian army", "country": "UA"}]),
    "E4": ("UA", [], "closed", 40, "disaster", []),
    "E5": ("FR", ["FR"], "active", 50, "armed_conflict",
           [{"name": "French police", "country": "FR"}]),
    "E6": ("PL", ["PL", "UA"], "active", 60, "armed_conflict",
           [{"name": "Polish police", "country": "PL"},
            {"name": "Ukrainian army", "country": "UA"}]),
    "H1": ("UA", ["UA"], "closed", 70, "armed_conflict",
           [{"name": "Ukrainian army", "country": "UA"}]),
}


def _row(eid: str, country: str, minutes: int, category: str) -> dict:
    t = T0 - timedelta(minutes=minutes)
    return {"id": eid, "category": category, "title": f"Event {eid}", "summary": "s",
            "actors": [], "lat": 48.0, "lon": 31.0, "precision": "city", "place_name": "P",
            "country": country, "geo_confidence": 0.9, "h3_3": "831196fffffffff",
            "t_start": iso(t - timedelta(hours=1)), "t_end": iso(t), "status": "multi-source",
            "confidence": 0.9, "version": 2, "mention_count": 2, "source_diversity": 2,
            "gdelt_ids": [], "first_seen": iso(t), "updated_at": iso(t), "cluster_state": {}}


def build_store(path: Path) -> SqliteStore:
    s = SqliteStore(path)
    s.save_events([_row(eid, c[0], c[3], c[4]) for eid, c in CASES.items()])
    with s._lock, s._db:
        for eid, (cc, acs, life, _m, _cat, actors) in CASES.items():
            s._db.execute(
                "UPDATE events SET country=?, actors=?, actor_countries=?, foreign_involved=?, "
                "attributed=?, lifecycle=? WHERE id=?",
                (cc, json.dumps(actors), json.dumps(sorted(acs)),
                 int(any(a != cc for a in acs)), int(bool(acs)), life, eid))
        s._db.execute("UPDATE events SET origin='historical' WHERE id='H1'")
    return s


def write_geojson(path: Path) -> Path:
    """Two square 'countries': UA (with a hole) and PL (a MultiPolygon)."""
    feats = [
        {"type": "Feature",
         "properties": {"ISO_A2": "UA", "NAME": "Ukraine", "LABEL_X": 31.0, "LABEL_Y": 48.0},
         "geometry": {"type": "Polygon", "coordinates": [
             [[22.0, 44.0], [40.0, 44.0], [40.0, 52.0], [22.0, 52.0], [22.0, 44.0]],
             [[30.0, 46.0], [32.0, 46.0], [32.0, 47.5], [30.0, 47.5], [30.0, 46.0]]]}},
        {"type": "Feature",
         "properties": {"ISO_A2": "PL", "NAME": "Poland", "LABEL_X": 19.0, "LABEL_Y": 52.0},
         "geometry": {"type": "MultiPolygon", "coordinates": [[
             [[14.0, 49.0], [21.9, 49.0], [21.9, 55.0], [14.0, 55.0], [14.0, 49.0]]]]}},
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                    encoding="utf-8")
    return path
