from osint.enrich.rules import RULE_FIELDS, RuleSet
from osint.enrich.rules.base import ExtractDoc


def test_ruleset_runs_every_extractor_and_isolates_failures(monkeypatch):
    rs = RuleSet.load()
    docs = [ExtractDoc(doc_id="d1", title="Drone strike on Kharkiv", role="primary",
                       url="https://a.example/1")]
    vals, errors = rs.run({"id": "E1"}, docs)
    assert errors == [] and {v.field for v in vals} >= {"headline", "weapons_platforms"}
    assert {v.field for v in vals} <= set(RULE_FIELDS)

    def boom(event, docs):
        raise RuntimeError("bad document")

    good = rs.extractors()
    monkeypatch.setattr(rs, "extractors", lambda: [("boom", boom), *good])
    vals2, errors2 = rs.run({"id": "E1"}, docs)
    assert errors2 == ["boom"] and vals2 == vals
