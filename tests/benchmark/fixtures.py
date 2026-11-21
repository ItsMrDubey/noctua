"""Synthetic events SQLite + documents lake + extraction store with known answers.

24 h window ending NOW (see test_collect for the expected numbers):
  E1 armed_conflict/city Kyiv, multi-source, supported, 2 articles, GDELT g1 g2 g3
  E2 civil_unrest/country, mismatch, 1 article, GDELT g4 (GDELT row before our event)
  E3 disaster/point, sensor-corroborated (quake), no verdict, no GDELT
  E4 armed_conflict/city, same H3 cell as E1, 1 h later (possible residual duplicate)
  E5 political/unlocated, no documents, no verdict
  S1 sensor-derived event; E_OLD ended 3 days ago (outside 24 h, inside 7 d)
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from osint.benchmark.config import BenchmarkConfig
from osint.benchmark.data import Sources
from osint.benchmark.scorecard import BenchmarkService
from osint.benchmark.store import BenchmarkStore
from osint.pipeline.extract.store import ExtractionStore
from osint.store.lake import SCHEMAS, Lake
from osint.store.sqlite import SqliteStore, iso

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)
CELL = "85aaaaaafffffff"


def eid(name: str) -> str:
    return f"01BM{name:0>22}"


def event(name, *, category="armed_conflict", precision="city", status="reported",
          first_seen=NOW - 2 * H, t_start=None, t_end=None, diversity=1, h3_5=CELL,
          title=None, place="Kyiv"):
    return {"id": eid(name), "category": category, "title": title or f"Event {name}",
            "summary": "", "actors": [], "lat": None if precision == "unlocated" else 50.4,
            "lon": None if precision == "unlocated" else 30.5, "precision": precision,
            "place_name": None if precision == "unlocated" else place, "country": "UA",
            "geo_confidence": 0.9, "h3_3": None, "h3_5": h3_5, "h3_7": None,
            "t_start": iso(t_start or first_seen), "t_end": iso(t_end or first_seen + 10 * M),
            "status": status, "confidence": 0.6, "version": 1, "mention_count": 1,
            "source_diversity": diversity, "gdelt_ids": [], "first_seen": iso(first_seen),
            "updated_at": iso(first_seen), "cluster_state": {}}


def doc(event_name, doc_id, published, role="primary", source="rss:x"):
    return {"event_id": eid(event_name), "doc_id": doc_id, "source": source,
            "url": f"https://news.example/{doc_id}", "title": f"Article {doc_id}",
            "published_at": iso(published), "language": "en", "role": role,
            "added_at": iso(published)}


def decision(gid, decision_, reason, event_name=None, decided=NOW - 2 * H):
    return {"gdelt_event_id": gid, "decision": decision_, "reason": reason,
            "event_id": eid(event_name) if event_name else None, "doc_id": f"gd:{gid}",
            "category": "armed_conflict", "decided_at": iso(decided)}


# gid -> (published/DATEADDED, geo_type, root, key, mentions)
GDELT_ROWS = {
    "g1": (NOW - 2 * H - 45 * M, 4, "19", "k1", 10),
    "g2": (NOW - 2 * H - 40 * M, 1, "19", "k1", 5),
    "g3": (NOW - 2 * H - 30 * M, 4, "19", "k1", 3),
    "g4": (NOW - 2 * H - 20 * M, 1, "14", "k4", 4),
    "g5": (NOW - 3 * H, 3, "04", "k5", 2),
    "g6": (NOW - 30 * H, 1, "19", "k6", 2),
    "g7": (NOW - 4 * H, 1, "18", "k7", 66),
    "g8": (NOW - 4 * H, 4, "18", "k8", 3),
    "g9": (NOW - 4 * H, None, "18", "k9", 3),
    "g10": (NOW - 1 * H, 4, "19", "k10", 3),
    "g11": (NOW - 1 * H, 1, "04", "k11", 3),
}


def gdelt_doc(gid: str) -> dict:
    pub, geo, root, key, mentions = GDELT_ROWS[gid]
    attrs = {"event_code": f"{root}0", "event_root_code": root, "sqldate": "20261121",
             "actor1_code": "A", "actor2_code": "B", "geo_feature_id": key,
             "geo_type": geo, "geo_name": f"Place {key}", "num_mentions": mentions,
             "num_sources": 1}
    row = {f.name: None for f in SCHEMAS["documents"]}
    row.update({"id": f"gd:{gid}", "source": "gdelt", "kind": "gdelt_event", "origin_id": gid,
                "url": f"https://gdelt.example/{gid}", "title": f"GDELT {gid}",
                "snippet": "", "language": "", "published_at": pub, "fetched_at": pub,
                "content_hash": gid, "attrs": json.dumps(attrs), "categories": "",
                "places": "[]"})
    return row


def build(tmp: Path) -> dict:
    store = SqliteStore(tmp / "osint.sqlite")
    events = [
        event("E1", first_seen=NOW - 3 * H, diversity=2, status="multi-source"),
        event("E2", category="civil_unrest", precision="country", first_seen=NOW - 2 * H,
              h3_5="85bbbbbbfffffff"),
        event("E3", category="disaster", precision="point", first_seen=NOW - 4 * H - 50 * M,
              h3_5="85cccccccfffffff", status="sensor-corroborated"),
        event("E4", first_seen=NOW - 2 * H, t_start=NOW - 2 * H),
        event("E5", category="political", precision="unlocated", first_seen=NOW - H,
              h3_5=None),
        event("EOLD", first_seen=NOW - 3 * 24 * H, h3_5="85ddddddfffffff"),
    ]
    docs = [
        doc("E1", "a1", NOW - 3 * H - 20 * M), doc("E1", "a2", NOW - 3 * H, role="mention"),
        doc("E1", "gd:g1", GDELT_ROWS["g1"][0], role="gdelt", source="gdelt"),
        doc("E1", "gd:g2", GDELT_ROWS["g2"][0], role="gdelt", source="gdelt"),
        doc("E1", "gd:g3", GDELT_ROWS["g3"][0], role="gdelt", source="gdelt"),
        doc("E2", "a3", NOW - 2 * H - 10 * M),
        doc("E2", "gd:g4", GDELT_ROWS["g4"][0], role="gdelt", source="gdelt"),
        doc("E3", "a4", NOW - 5 * H), doc("E4", "a5", NOW - 2 * H - 5 * M),
        doc("EOLD", "a6", NOW - 3 * 24 * H - 10 * M),
    ]
    decisions = [
        decision("g1", "accepted", "duplicate_of_event", "E1"),
        decision("g2", "accepted", "duplicate_of_event", "E1"),
        decision("g3", "accepted", "duplicate_of_event", "E1"),
        decision("g4", "accepted", "duplicate_of_event", "E2"),
        decision("g5", "rejected", "irrelevant_category"),
        decision("g6", "rejected", "stale"),
        decision("g7", "rejected", "no_article_backing"),
        decision("g8", "rejected", "no_article_backing"),
        decision("g9", "rejected", "no_location"),
    ]
    store.save_events(events, docs, [], decisions)
    store.save_verifications([
        {"event_id": eid("E1"), "verdict": "supported", "fingerprint": "f"},
        {"event_id": eid("E2"), "verdict": "mismatch", "reasons": ["country: article UA"],
         "quotes": [{"field": "where", "quote": "in Lviv"}], "fingerprint": "f"},
        {"event_id": eid("E4"), "verdict": "unsupported", "fingerprint": "f"},
    ])
    store.save_corroborations([], summaries=[
        {"event_id": eid("E3"), "score": 0.8, "kinds": ["quake"], "evidence": 1,
         "corroborated": True}])
    s1 = event("S1", category="air_activity", precision="point", first_seen=NOW - H,
               h3_5="85eeeeeefffffff", status="sensor-corroborated")
    store.save_sensor_event(s1, {"detector": "orbit", "dedupe_key": "orbit:x:1", "cell": "x"},
                            [], {"event_id": s1["id"], "score": 1.0, "kinds": ["aircraft"],
                                 "evidence": 1, "corroborated": True})
    store.close()

    lake = Lake(tmp / "lake")
    lake.write("documents", [gdelt_doc(g) for g in GDELT_ROWS])

    ex = ExtractionStore(tmp / "extraction.sqlite")
    ex.log_call((NOW - H).timestamp(), event_id=eid("E1"), ok=True, cost_usd=0.01)
    ex.log_call((NOW - 2 * H).timestamp(), event_id=eid("E2"), ok=True, cost_usd=0.01)
    ex.log_call((NOW - 72 * H).timestamp(), event_id=eid("EOLD"), ok=True, cost_usd=1.0)
    ex.close()
    return {"lake": lake}


def service(tmp: Path, lake: Lake | None, cfg: BenchmarkConfig | None = None,
            extraction: bool = True) -> BenchmarkService:
    src = Sources(sqlite_path=tmp / "osint.sqlite",
                  extraction_path=(tmp / "extraction.sqlite") if extraction else None,
                  lake=lake, cfg=cfg or BenchmarkConfig(),
                  tracked_roots=frozenset({"13", "14", "15", "16", "17", "18", "19", "20"}))
    return BenchmarkService(src, BenchmarkStore(tmp / "benchmark.sqlite"), clock=lambda: NOW)


def raw(tmp: Path, sql: str, args=()) -> list:
    db = sqlite3.connect(tmp / "osint.sqlite")
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()
