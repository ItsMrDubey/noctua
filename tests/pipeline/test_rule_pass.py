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


def _counter_total(metrics, name):
    """A counter summed over all its label sets."""
    return sum(v for (n, _), v in metrics._counters.items() if n == name)


class CountingLake:
    """Lake stand-in: snippets by doc id, counting queries and the ids asked for."""

    def __init__(self, snippets):
        self.snippets, self.queries, self.asked = snippets, 0, []

    def query(self, sql, params=None, **kw):
        self.queries += 1
        self.asked += list(params or [])
        return [(i, self.snippets[i]) for i in params or [] if i in self.snippets]


def _big_event_store(tmp_path, n=10):
    """BIG: a primary (oldest) and ``n`` mentions m0 (oldest) .. m{n-1} (newest)."""
    s = SqliteStore(tmp_path / "big.sqlite")
    docs = [_doc("BIG", "p", "Russian shelling of Kharkiv kills 3 civilians", "a.example",
                 500, "primary")]
    docs += [_doc("BIG", f"m{i}", f"Kharkiv shelling report {i}", f"h{i}.example", 400 - i)
             for i in range(n)]
    s.save_events([_ev("BIG")], documents=docs)
    return s


def _recording(rules, monkeypatch):
    seen: dict[str, list[str]] = {}
    real = rules.extractors()

    def record(event, docs):
        seen[event["id"]] = sorted(d.doc_id for d in docs)
        return []

    monkeypatch.setattr(rules, "extractors", lambda: [("record", record), *real])
    return seen


def test_documents_fed_to_the_extractors_are_capped_per_event(tmp_path, lexicon, rules,
                                                             monkeypatch):
    s = _big_event_store(tmp_path)
    # the stored rule headline (an old mention) stays a candidate, like the primary
    s.replace_rule_fields(["BIG"], [{"event_id": "BIG", "field": "headline", "value": "x",
                                     "doc_id": "m1", "quote": "x"}])
    seen = _recording(rules, monkeypatch)
    lake = CountingLake({f"m{i}": f"snippet {i}" for i in range(10)})
    RulePass(s, lexicon=lexicon, rules=rules, lake=lake, max_docs_per_event=3).run(["BIG"])
    assert seen["BIG"] == ["m1", "m7", "m8", "m9", "p"]
    assert set(lake.asked) <= {"m1", "m7", "m8", "m9", "p"}  # only the capped set
    s.close()


def test_the_document_cap_comes_from_extract_rules_yaml(lexicon, rules):
    assert rules.max_docs_per_event == 50
    assert RulePass(None, lexicon=lexicon, rules=rules).max_docs_per_event == 50


def test_lake_snippets_are_cached_between_passes(tmp_path, lexicon, rules):
    s = _big_event_store(tmp_path, n=3)
    lake = CountingLake({"m0": "Three civilians killed.", "m1": "Shelling overnight."})
    rp = RulePass(s, lexicon=lexicon, rules=rules, lake=lake)
    rp.run(["BIG"], snippets={"m2": "fresh snippet"})
    first = lake.queries
    assert first >= 1 and "m2" not in lake.asked  # the pass's own snippets are used
    rp.run(["BIG"])
    assert lake.queries == first  # found and not-found documents are both cached
    s.close()


def test_the_snippet_cache_is_bounded(tmp_path, lexicon, rules):
    s = _big_event_store(tmp_path, n=3)
    lake = CountingLake({"m0": "a", "m1": "b", "m2": "c", "p": "d"})
    rp = RulePass(s, lexicon=lexicon, rules=rules, lake=lake, snippet_cache_size=2)
    rp.run(["BIG"])
    assert len(rp._snippets) == 2
    before = lake.queries
    rp.run(["BIG"])
    assert lake.queries > before  # evicted documents are read again
    s.close()


def test_a_failed_lake_lookup_is_not_cached(tmp_path, lexicon, rules):
    class Flaky(CountingLake):
        fail = True

        def query(self, sql, params=None, **kw):
            if self.fail:
                self.queries += 1
                raise OSError("lake down")
            return super().query(sql, params, **kw)

    s = _big_event_store(tmp_path, n=1)
    lake = Flaky({"m0": "Three civilians killed."})
    rp = RulePass(s, lexicon=lexicon, rules=rules, lake=lake)
    rp.run(["BIG"])
    lake.fail = False
    n = lake.queries
    rp.run(["BIG"])
    assert lake.queries > n and rp._snippets.get("m0") == "Three civilians killed."
    s.close()


def test_one_failing_extractor_counts_one_error(store, lexicon, rules, monkeypatch):
    from osint.core.metrics import Metrics

    def boom(event, docs):
        raise RuntimeError("bad document")

    good = rules.extractors()
    monkeypatch.setattr(rules, "extractors", lambda: [("boom", boom), *good])
    m = Metrics()
    RulePass(store, lexicon=lexicon, rules=rules, metrics=m).run(["NEWS1"])
    assert _counter_total(m, "rule_extraction_errors_total") == 1
    assert m.get("rule_extraction_errors_total", extractor="boom") == 1


def test_a_poison_event_is_skipped_and_the_chunk_proceeds(tmp_path, lexicon, rules,
                                                          monkeypatch):
    from osint.core.metrics import Metrics

    s = SqliteStore(tmp_path / "p.sqlite")
    s.save_events([_ev("BAD"), _ev("GOOD")], documents=[
        _doc("BAD", "b1", "Russian shelling of Kharkiv kills 3 civilians", "a.example", 30,
             "primary"),
        _doc("GOOD", "g1", "Russian shelling of Kharkiv kills 3 civilians", "a.example", 30,
             "primary")])
    rp = RulePass(s, lexicon=lexicon, rules=rules, metrics=Metrics())
    real = rp._docs

    def docs(ev, rows, snips, st):
        if ev["id"] == "BAD":
            raise ValueError("poison document")
        return real(ev, rows, snips, st)

    monkeypatch.setattr(rp, "_docs", docs)
    stats = rp.run(["BAD", "GOOD"])
    assert stats.events == 1 and stats.errors == 1
    assert _counter_total(rp.metrics, "rule_extraction_errors_total") == 1
    assert s.get_event("GOOD")["actor_countries"] == ["RU"]
    assert s.get_event("BAD")["actor_countries"] == []
    assert "GOOD" in s.rule_fields(["BAD", "GOOD"])
    s.close()


def test_derived_columns_and_rule_fields_are_written_in_one_transaction(store, lexicon,
                                                                         rules, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "write_rule_results", broken)
    state = SimpleNamespace(actors=["old"], vector=None)
    with pytest.raises(RuntimeError):
        RulePass(store, lexicon=lexicon, rules=rules).run(["NEWS1"],
                                                          states={"NEWS1": state})
    assert state.actors == ["old"]  # memory only follows a successful write


def test_write_rule_results_is_atomic(store):
    derived = [{"id": "NEWS1", "actor_countries": ["RU"], "foreign_involved": 1,
                "attributed": 1}]
    with pytest.raises(KeyError):  # a malformed field row aborts the whole write
        store.write_rule_results(derived, ["NEWS1"], [{"event_id": "NEWS1"}])
    assert store.get_event("NEWS1")["actor_countries"] == []
    store.write_rule_results(derived, ["NEWS1"], [
        {"event_id": "NEWS1", "field": "headline", "value": "h", "doc_id": "d1"}])
    assert store.get_event("NEWS1")["actor_countries"] == ["RU"]
    assert [r["field"] for r in store.rule_fields(["NEWS1"])["NEWS1"]] == ["headline"]


def test_a_stored_headline_inside_the_window_does_not_widen_it(tmp_path, lexicon, rules,
                                                                monkeypatch):
    s = _big_event_store(tmp_path)
    s.replace_rule_fields(["BIG"], [{"event_id": "BIG", "field": "headline", "value": "x",
                                     "doc_id": "m8", "quote": "x"}])
    seen = _recording(rules, monkeypatch)
    RulePass(s, lexicon=lexicon, rules=rules, max_docs_per_event=3).run(["BIG"])
    assert seen["BIG"] == ["m7", "m8", "m9", "p"]  # no older document pulled in
    s.close()
