"""Deterministic-extraction columns and store methods (osint.store.sqlite)."""
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from osint.store.sqlite import EVENTS_SCHEMA, SqliteStore, iso

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _ev(eid="E1", **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "t_start": iso(T0),
            "t_end": iso(T0), "status": "reported", "confidence": 0.4, "version": 1,
            "mention_count": 1, "source_diversity": 1, "first_seen": iso(T0),
            "updated_at": iso(T0), "country": "UA"}
    return {**base, **kw}


def _doc(eid, did, at, role="mention"):
    return {"event_id": eid, "doc_id": did, "source": "rss:t",
            "url": f"https://{did}.example/a", "title": f"title {did}",
            "published_at": iso(at), "language": "en", "role": role, "added_at": iso(at)}


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_new_columns_defaults_and_country_index(store):
    store.save_events([_ev()])
    e = store.get_event("E1")
    assert e["actor_countries"] == [] and e["foreign_involved"] == 0
    assert e["attributed"] == 0 and e["lifecycle"] is None
    idx = {r[1] for r in store._db.execute("PRAGMA index_list(events)")}
    assert "events_country" in idx
    slim = store.list_events(limit=5)[0]
    assert {"lifecycle", "actor_countries", "foreign_involved", "attributed"} <= set(slim)


def test_save_events_writes_derived_and_a_plain_upsert_keeps_them(store):
    store.save_events([_ev(actor_countries=["RU", "UA", "RU"], foreign_involved=True,
                           attributed=True, lifecycle="active")])
    store.save_events([_ev(version=2, title="t2")])  # a clusterer row without them
    e = store.get_event("E1")
    assert e["actor_countries"] == ["RU", "UA"] and e["foreign_involved"] == 1
    assert e["attributed"] == 1 and e["lifecycle"] == "active" and e["version"] == 2


def test_set_event_derived_with_and_without_actors(store):
    store.save_events([_ev(), _ev("E2")])
    n = store.set_event_derived([
        {"id": "E1", "actors": [{"name": "Russian forces", "country": "RU", "source": "rule"}],
         "actor_countries": ["RU"], "foreign_involved": True, "attributed": True},
        {"id": "E2", "actor_countries": [], "foreign_involved": False, "attributed": False},
        {"id": "missing", "actor_countries": ["RU"], "foreign_involved": True,
         "attributed": True}])
    assert n == 2
    e1 = store.get_event("E1")
    assert e1["actors"][0]["source"] == "rule" and e1["actor_countries"] == ["RU"]
    assert store.get_event("E2")["attributed"] == 0


def test_rule_fields_replace_only_rule_rows(store):
    store.save_events([_ev()], fields=[{"event_id": "E1", "field": "location",
                                        "value": "Kyiv", "doc_id": "d1", "quote": "in Kyiv"}])
    store.replace_rule_fields(["E1"], [{"event_id": "E1", "field": "headline", "value": "Old",
                                        "doc_id": "d1", "quote": "Old"}])
    store.replace_rule_fields(["E1"], [
        {"event_id": "E1", "field": "headline", "value": "New", "doc_id": "d2", "quote": "New"},
        {"event_id": "E1", "field": "casualties.killed", "value": "3", "doc_id": "d2",
         "quote": "kills 3"}])
    rules = store.rule_fields(["E1"])["E1"]
    assert [(r["field"], r["value"]) for r in rules] == [("casualties.killed", "3"),
                                                          ("headline", "New")]
    fields = {(f["field"], f["value"]) for f in store.get_event("E1")["fields"]}
    # rule values live in their own table: event_fields (the dossier's extracted facts)
    # keeps only clusterer / LLM provenance
    assert fields == {("location", "Kyiv")}
    store.replace_rule_fields(["E1"], [])
    assert store.rule_fields(["E1"]) == {}
    assert store.get_event("E1")["fields"][0]["field"] == "location"


def test_rule_rows_follow_event_deletes_and_merges(store):
    store.save_events([_ev("E1"), _ev("E2"), _ev("E3")])
    rows = [{"event_id": e, "field": "headline", "value": e, "doc_id": "d", "quote": e}
            for e in ("E1", "E2", "E3")]
    store.replace_rule_fields(["E1", "E2", "E3"], rows)
    assert store.delete_events(["E3"]) == 1
    assert "E3" not in store.rule_fields(["E1", "E2", "E3"])
    store.merge_events([{"survivor": "E1", "loser": "E2"}], [])
    assert set(store.rule_fields(["E1", "E2"])) == {"E1"}
    store.clear_news_events()
    assert store._db.execute("SELECT COUNT(*) FROM event_rule_fields").fetchone()[0] == 0


def test_rule_documents_and_events_for_rules(store):
    store.save_events([_ev(actors=[{"name": "Russia", "country": "RUS"}], summary="s")],
                      documents=[_doc("E1", "d1", T0, "primary"), _doc("E1", "g1", T0, "gdelt")])
    docs = store.rule_documents(["E1"])["E1"]
    assert [d["doc_id"] for d in docs] == ["d1"] and docs[0]["url"] == "https://d1.example/a"
    ev = store.events_for_rules(["E1", "nope"])
    assert set(ev) == {"E1"} and ev["E1"]["actors"][0]["country"] == "RUS"
    assert ev["E1"]["origin"] == "news" and ev["E1"]["summary"] == "s"
    assert ev["E1"]["category"] == _ev()["category"]


def test_lifecycle_inputs_and_set_lifecycle(store):
    store.save_events(
        [_ev(), _ev("E2", lifecycle="closed"), _ev("E3")],
        documents=[_doc("E1", "d1", T0 - timedelta(hours=1), "primary"),
                   _doc("E1", "d2", T0 - timedelta(hours=8)), _doc("E1", "g", T0, "gdelt"),
                   _doc("E2", "d3", T0)])
    rows = {r["id"]: r for r in store.lifecycle_inputs(T0 - timedelta(hours=6))}
    assert set(rows) == {"E1", "E3"}  # closed events are not rescanned
    assert rows["E1"]["last_doc_at"] == iso(T0 - timedelta(hours=1))
    assert rows["E1"]["recent_docs"] == 1  # the GDELT row does not count
    assert rows["E3"]["last_doc_at"] is None and rows["E3"]["recent_docs"] == 0
    only = store.lifecycle_inputs(T0 - timedelta(hours=6), ids=["E2"])
    assert [r["id"] for r in only] == ["E2"] and only[0]["lifecycle"] == "closed"
    assert store.set_lifecycle({"E1": "developing", "E3": "active"}) == 2
    assert store.get_event("E1")["lifecycle"] == "developing"


def test_an_existing_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(EVENTS_SCHEMA)
    db.execute("INSERT INTO event_fields VALUES ('E1', 'location', 'Kyiv', 'd1', '')")
    db.commit()
    db.close()
    s = SqliteStore(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(events)")}
    assert {"actor_countries", "foreign_involved", "attributed", "lifecycle"} <= cols
    assert "event_rule_fields" in {r[0] for r in s._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()
    SqliteStore(path).close()  # idempotent on the next start


def test_event_fields_keeps_its_pre_branch_schema(tmp_path):
    """Older worktrees share the database and insert event_fields rows positionally
    (``INSERT OR IGNORE INTO event_fields VALUES (?,?,?,?,?)``): the table must keep
    exactly its five columns after this version opened it."""
    path = tmp_path / "shared.sqlite"
    s = SqliteStore(path)
    s.replace_rule_fields(["E1"], [{"event_id": "E1", "field": "headline", "value": "h",
                                    "doc_id": "d1", "quote": "h"}])
    s.close()
    db = sqlite3.connect(path)
    try:
        assert [r[1] for r in db.execute("PRAGMA table_info(event_fields)")] == [
            "event_id", "field", "value", "doc_id", "quote"]
        db.executemany("INSERT OR IGNORE INTO event_fields VALUES (?,?,?,?,?)",
                       [("E1", "location", "Kyiv", "d1", "in Kyiv")])
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM event_fields").fetchone()[0] == 1
    finally:
        db.close()


def test_a_source_column_from_an_earlier_build_is_folded_back(tmp_path):
    """A database opened by an earlier build of this branch has event_fields.source and
    rule rows in event_fields: they move to event_rule_fields and the column goes."""
    path = tmp_path / "early.sqlite"
    db = sqlite3.connect(path)
    db.executescript(EVENTS_SCHEMA)
    db.execute("ALTER TABLE event_fields ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX event_fields_source ON event_fields(source, event_id)")
    db.execute("INSERT INTO event_fields VALUES ('E1', 'location', 'Kyiv', 'd1', '', '')")
    db.execute("INSERT INTO event_fields VALUES ('E1', 'headline', 'H', 'd1', 'H', 'rule')")
    db.commit()
    db.close()
    s = SqliteStore(path)
    assert [r[1] for r in s._db.execute("PRAGMA table_info(event_fields)")] == [
        "event_id", "field", "value", "doc_id", "quote"]
    rows = s.rule_fields(["E1"])["E1"]
    assert [(r["field"], r["value"]) for r in rows] == [("headline", "H")]
    assert [tuple(r) for r in s._db.execute("SELECT field FROM event_fields")] == [("location",)]
    s.close()
