"""Extraction verdicts: type mismatch hides the event; supported finer places refine it."""

from datetime import timedelta

from osint.core.models import PlaceHit
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore
from tests.pipeline.test_event_builder import NOW, _builder, _FakeVerifier, _row

UKRAINE = PlaceHit(name="Ukraine", country="UA", lat=49.0, lon=32.0, precision="country",
                   confidence=0.9, matched="Ukraine")
KHARKIV = {"lat": 49.99, "lon": 36.23, "country": "UA", "precision": "city"}


def _rec(verdict, *, reasons=(), geo=None, place=None, quotes=()):
    return {"status": "done", "verdict": verdict, "reasons": list(reasons), "doc_id": "d",
            "geo": geo, "quotes": list(quotes),
            "result": {"where": {"place_name": place}}}


def _setup(tmp_path, rows):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", rows)
    b = _builder(store, lake, lambda: NOW)
    b.run_once()
    ids = {e["title"]: e["id"] for e in store.list_events(limit=20)}
    return store, b, ids


def test_type_mismatch_is_hidden_and_capped(tmp_path):
    store, b, ids = _setup(tmp_path, [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=50)),
        _row(2, "Missile strike on Kyiv power plant", host="b.example",
             fetched=NOW - timedelta(minutes=45))])
    [eid] = ids.values()
    assert store.get_event(eid)["status"] == "multi-source"
    v = _FakeVerifier()
    reason = "type: article is other, event is armed_conflict"
    v.recs = {eid: _rec("mismatch", reasons=[reason])}
    b.verifier = v
    assert b.run_once().verdicts_applied == 1
    assert store.get_event(eid)["confidence"] <= 0.2
    assert store.verifications([eid])[eid]["verdict"] == "unsupported"
    assert not store.list_events(map_default=True, limit=10)
    store.close()


def test_supported_refines_coarse_event_within_its_country(tmp_path):
    store, b, ids = _setup(tmp_path, [
        _row(1, "Missile strike in Ukraine", fetched=NOW - timedelta(minutes=50),
             place=UKRAINE),
        _row(2, "Shelling reported across Ukraine overnight", host="b.example",
             fetched=NOW - timedelta(minutes=40), place=UKRAINE)])
    a, c = ids["Missile strike in Ukraine"], ids["Shelling reported across Ukraine overnight"]
    assert store.get_event(a)["precision"] == "country"
    v = _FakeVerifier()
    v.recs = {a: _rec("supported", geo=KHARKIV, place="Kharkiv"),
              # a different country is never adopted
              c: _rec("supported", geo={**KHARKIV, "country": "RU"}, place="Belgorod")}
    b.verifier = v
    assert b.run_once().verdicts_applied == 2
    ea, ec = store.get_event(a), store.get_event(c)
    assert (ea["precision"], ea["place_name"], ea["lat"]) == ("city", "Kharkiv", 49.99)
    assert any(f["field"] == "location_before_extraction" for f in ea["fields"])
    assert (ec["precision"], ec["place_name"]) == ("country", "Ukraine")
    store.close()


def test_supported_never_downgrades_or_moves_a_city_event(tmp_path):
    store, b, ids = _setup(tmp_path, [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=50))])
    [eid] = ids.values()
    v = _FakeVerifier()
    v.recs = {eid: _rec("supported", geo=KHARKIV, place="Kharkiv")}
    b.verifier = v
    b.run_once()
    assert store.get_event(eid)["place_name"] == "Kyiv"
    store.close()


def test_unlocated_event_refined_only_when_place_is_quoted(tmp_path):
    store, b, ids = _setup(tmp_path, [
        _row(1, "Explosion at an ammunition depot", fetched=NOW - timedelta(minutes=50),
             place=None),
        _row(2, "Huge blast destroys a fuel depot", fetched=NOW - timedelta(minutes=40),
             place=None, host="b.example")])
    q, n = ids["Explosion at an ammunition depot"], ids["Huge blast destroys a fuel depot"]
    assert store.get_event(q)["precision"] == "unlocated"
    v = _FakeVerifier()
    v.recs = {q: _rec("supported", geo=KHARKIV, place="Kharkiv", quotes=[
        {"field": "where", "quote": "the depot near Kharkiv exploded", "verbatim": True}]),
        n: _rec("supported", geo=KHARKIV, place="Kharkiv")}
    b.verifier = v
    b.run_once()
    assert store.get_event(q)["place_name"] == "Kharkiv"
    assert store.get_event(n)["precision"] == "unlocated"
    store.close()
