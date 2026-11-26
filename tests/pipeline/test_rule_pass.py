from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from osint.enrich.actors import ActorLexicon
from osint.enrich.rules import RuleSet
from osint.pipeline.rule_pass import RulePass
from osint.store.sqlite import SqliteStore, iso

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _ev(eid, **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "summary": "",
            "t_start": iso(NOW - timedelta(hours=2)), "t_end": iso(NOW - timedelta(hours=1)),
            "status": "reported", "confidence": 0.5, "version": 1, "mention_count": 2,
            "source_diversity": 2, "first_seen": iso(NOW), "updated_at": iso(NOW),
            "country": "UA", "actors": []}
    return {**base, **kw}


def _doc(eid, did, title, host, minutes, role="mention"):
    at = NOW - timedelta(minutes=minutes)
    return {"event_id": eid, "doc_id": did, "source": "rss:t", "url": f"https://{host}/{did}",
            "title": title, "published_at": iso(at), "language": "en", "role": role,
            "added_at": iso(at)}


class FakeExtractions:
    def __init__(self, recs):
        self.recs = recs

    def get_many(self, ids):
        return {i: self.recs[i] for i in ids if i in self.recs}


@pytest.fixture(scope="module")
def lexicon():
    return ActorLexicon.load()


@pytest.fixture
def rules():
    return RuleSet.load()


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.sqlite")
    acled_title = "Shelling: Military Forces of Russia (2000-) vs Civilians (Ukraine)"
    s.save_events(
        [_ev("NEWS1", actors=[{"name": "Russia", "role": "source", "type": "RUS",
                               "country": "RUS"}]),
         _ev("acled-UKR1", origin="historical", status="historical", title=acled_title,
             summary="Russian forces shelled Kherson, killing 2.", actors=[
                 {"name": "Military Forces of Russia (2000-)", "role": "actor1", "type": "8",
                  "country": None, "source": "acled"},
                 {"name": "Civilians (Ukraine)", "role": "actor2", "type": "7",
                  "country": None, "source": "acled"}])],
        documents=[
            _doc("NEWS1", "d1", "Russian shelling of Kharkiv kills 3 civilians", "a.example",
                 50, "primary"),
            _doc("NEWS1", "d2", "Three civilians killed in Kharkiv shelling", "b.example", 40),
            {"event_id": "acled-UKR1", "doc_id": "acled:UKR1", "source": "acled",
             "url": "https://acleddata.com/data/#UKR1", "title": acled_title,
             "published_at": iso(NOW - timedelta(hours=3)), "language": "en",
             "role": "primary", "added_at": iso(NOW)}])
    yield s
    s.close()


def test_rule_pass_resolves_actors_derives_and_writes_rule_fields(store, lexicon, rules):
    ext = FakeExtractions({"NEWS1": {"status": "done", "verdict": "supported", "result": {
        "actors": [{"name": "Israel Defense Forces", "role": "perpetrator", "type": None}]}}})
    state = SimpleNamespace(actors=[], vector=None)
    stats = RulePass(store, lexicon=lexicon, rules=rules, extractions=ext).run(
        ["NEWS1", "gone"], states={"NEWS1": state}, snippets={})
    assert stats.events == 1 and stats.errors == 0 and stats.fields >= 3
    ev = store.get_event("NEWS1")
    by_source = {a["source"]: a for a in ev["actors"]}
    assert (by_source["gdelt"]["country"], by_source["gdelt"]["cameo"]) == ("RU", "RUS")
    assert by_source["rule"]["country"] == "RU" and by_source["rule"]["role"] == "mentioned"
    assert by_source["llm"]["country"] == "IL"
    assert ev["actor_countries"] == ["IL", "RU"]
    assert ev["foreign_involved"] == 1 and ev["attributed"] == 1
    assert state.actors == ev["actors"]
    got = {(r["field"], r["value"]) for r in store.rule_fields(["NEWS1"])["NEWS1"]}
    assert ("headline", "Russian shelling of Kharkiv kills 3 civilians") in got
    assert ("casualties.killed", "3") in got and ("weapons_platforms", "artillery") in got


def test_unsupported_extractions_add_no_llm_actors(store, lexicon, rules):
    ext = FakeExtractions({"NEWS1": {"status": "done", "verdict": "mismatch", "result": {
        "actors": [{"name": "Israel Defense Forces", "role": "perpetrator"}]}}})
    RulePass(store, lexicon=lexicon, rules=rules, extractions=ext).run(["NEWS1"])
    assert "IL" not in store.get_event("NEWS1")["actor_countries"]


def test_historical_events_get_actors_but_no_rule_fields(store, lexicon, rules):
    RulePass(store, lexicon=lexicon, rules=rules).run(["acled-UKR1"])
    ev = store.get_event("acled-UKR1")
    assert [(a["country"], a["source"]) for a in ev["actors"]] == [("RU", "acled"),
                                                                  ("UA", "acled")]
    assert ev["foreign_involved"] == 1 and store.rule_fields(["acled-UKR1"]) == {}


def test_a_failing_extractor_is_skipped(store, lexicon, rules, monkeypatch):
    def boom(event, docs):
        raise RuntimeError("bad document")

    good = rules.extractors()
    monkeypatch.setattr(rules, "extractors", lambda: [("boom", boom), *good])
    stats = RulePass(store, lexicon=lexicon, rules=rules).run(["NEWS1"])
    assert stats.errors == 1
    assert store.rule_fields(["NEWS1"])["NEWS1"]  # the other extractors still wrote


def test_dry_run_with_a_country_override_writes_nothing(store, lexicon, rules):
    stats = RulePass(store, lexicon=lexicon, rules=rules).run(
        ["NEWS1"], write=False, country_override={"NEWS1": "RU"})
    assert stats.events == 1 and stats.attributed == 1 and stats.foreign == 0
    assert store.get_event("NEWS1")["actor_countries"] == []
    assert store.rule_fields(["NEWS1"]) == {}


@pytest.mark.parametrize(("category", "expected"), [("armed_conflict", ["IL"]),
                                                    ("protest", [])])
def test_generic_demonym_nouns_use_the_stored_event_category(tmp_path, lexicon, rules,
                                                            category, expected):
    # "Israeli strike" is a conflict-only phrase: RulePass must hand resolve_actors the
    # event's real category (read from the store), not a category-less dict
    s = SqliteStore(tmp_path / "c.sqlite")
    s.save_events([_ev("E1", category=category, country="LB")], documents=[
        _doc("E1", "d1", "Israeli strike kills 5", "a.example", 30, "primary")])
    RulePass(s, lexicon=lexicon, rules=rules).run(["E1"])
    assert s.get_event("E1")["actor_countries"] == expected
    s.close()
