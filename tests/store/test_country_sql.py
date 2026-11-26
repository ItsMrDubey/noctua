"""Country context rules (spec B §1) on the fixture DB: location, involvement, counterpart,
ctx from both sides, ordering, window/paging and the summary aggregates."""

from datetime import timedelta

import pytest

from osint.store.country_sql import as_list, event_ctx
from tests.country_fixture import T0, build_store

INSIDE_DOM = {"location": "inside", "involvement": "domestic"}
INSIDE_FOR = {"location": "inside", "involvement": "foreign"}
ABROAD_FOR = {"location": "abroad", "involvement": "foreign"}
INSIDE_UNATTR = {"location": "inside", "involvement": "unattributed"}


@pytest.fixture
def store(tmp_path):
    s = build_store(tmp_path / "e.sqlite")
    yield s
    s.close()


def ids(items):
    return [it["id"] for it in items]


def test_event_ctx_rules():
    assert event_ctx("UA", "UA", ["UA"], 1) == INSIDE_DOM
    assert event_ctx("UA", "UA", ["RU", "UA"], 1) == INSIDE_FOR
    assert event_ctx("RU", "UA", ["RU", "UA"], 1) == ABROAD_FOR
    assert event_ctx("UA", "UA", [], 0) == INSIDE_UNATTR
    assert event_ctx("UA", "UA", "[]", None) == INSIDE_UNATTR  # never domestic
    # location unknown, only X acts: every KNOWN country is X
    assert event_ctx("UA", None, ["UA"], 1) == {"location": "abroad", "involvement": "domestic"}
    # location NULL, only X acts, not attributed: never domestic (matches the SQL buckets)
    assert event_ctx("UA", None, ["UA"], 0)["involvement"] != "domestic"
    assert as_list('["RU","UA"]') == ["RU", "UA"]
    assert as_list(None) == [] and as_list("bad json") == [] and as_list(["UA", None]) == ["UA"]


def test_location_sets_and_order(store):
    def lst(**kw):
        return ids(store.list_events(country="UA", **kw))

    # lifecycle first (developing, active, cooling, closed), then latest update;
    # E5 is unrelated and H1 is historical
    assert lst() == ["E1", "E2", "E6", "E3", "E4"]
    assert set(lst(location="inside")) == {"E1", "E2", "E4"}
    assert set(lst(location="abroad")) == {"E3", "E6"}


def test_involvement_sets(store):
    def lst(**kw):
        return ids(store.list_events(country="UA", **kw))

    assert lst(involvement="domestic") == ["E1"]
    assert set(lst(involvement="foreign")) == {"E2", "E3", "E6"}
    assert lst(involvement="unattributed") == ["E4"]
    assert lst(location="inside", involvement="foreign") == ["E2"]
    assert lst(location="abroad", involvement="domestic") == []
    assert lst(location="abroad", involvement="unattributed") == []


def test_strike_seen_from_both_sides(store):
    ua = {it["id"]: it["ctx"] for it in store.list_events(country="UA")}
    assert ua == {"E1": INSIDE_DOM, "E2": INSIDE_FOR, "E3": ABROAD_FOR, "E4": INSIDE_UNATTR,
                  "E6": ABROAD_FOR}
    ru = {it["id"]: it["ctx"] for it in store.list_events(country="RU")}
    assert ru == {"E2": ABROAD_FOR, "E3": INSIDE_FOR}
    assert ids(store.list_events(country="RU", involvement="domestic")) == []
    first = store.list_events(country="UA")[0]
    assert first["lifecycle"] == "developing" and first["actor_countries"] == ["UA"]
    assert first["attributed"] == 1
    assert "ctx" not in store.list_events()[0]  # plain list: unchanged


def test_counterpart(store):
    assert set(ids(store.list_events(country="UA", counterpart="RU"))) == {"E2", "E3"}
    assert ids(store.list_events(country="UA", counterpart="PL")) == ["E6"]
    assert ids(store.list_events(country="UA", counterpart="FR")) == []


def test_window_paging_and_category(store):
    since, until = T0 - timedelta(minutes=35), T0 - timedelta(minutes=15)
    assert ids(store.list_events(country="UA", since=since, until=until)) == ["E2", "E3"]
    assert ids(store.list_events(country="UA", limit=2)) == ["E1", "E2"]
    assert ids(store.list_events(country="UA", limit=2, offset=2)) == ["E6", "E3"]
    assert "E4" not in ids(store.list_events(country="UA", category="armed_conflict"))


def test_country_events_and_spark(store):
    rows = store.country_events("UA", since=T0 - timedelta(days=1))
    assert {r["id"] for r in rows} == {"E1", "E2", "E3", "E4", "E6"}
    e2 = next(r for r in rows if r["id"] == "E2")
    assert e2["actor_countries"] == ["RU", "UA"] and e2["ctx"] == INSIDE_FOR
    assert e2["lifecycle"] == "active" and e2["actors"][0]["name"] == "Russian forces"
    only = store.country_events("UA", since=T0 - timedelta(days=1), category="disaster")
    assert [r["id"] for r in only] == ["E4"]
    assert store.country_events("UA", since=T0 - timedelta(minutes=15)) != []
    assert store.country_spark("UA", T0 - timedelta(days=30)) == [("2026-11-21", 5)]


def test_unattributed_actor_only_row_is_never_domestic(store):
    # location NULL, actors [UA], attributed 0: not domestic in ctx nor in the SQL
    with store._lock, store._db:
        store._db.execute("UPDATE events SET country=NULL, actor_countries='[\"UA\"]', "
                          "attributed=0 WHERE id='E5'")
    got = {it["id"]: it for it in store.list_events(country="UA")}
    assert got["E5"]["ctx"]["involvement"] != "domestic"
    assert "E5" not in ids(store.list_events(country="UA", involvement="domestic"))


def test_country_spark_plan_uses_an_index(store):
    """The 30-day spark must not full-scan events (it runs on every summary poll)."""
    from osint.store.country_sql import country_where
    where, params = country_where("UA")
    where.append("events.t_start >= ?")
    params.append("2026-10-01T00:00:00Z")
    plan = " | ".join(r[3] for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT substr(t_start, 1, 10), count(*) FROM events WHERE "
        f"{' AND '.join(where)} GROUP BY 1 ORDER BY 1", params))
    assert "SCAN events" not in plan, plan
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


# every shape a row can take as seen from UA: location UA / elsewhere / NULL / empty, actor
# countries in every mix (incl. junk values as_list drops), attributed 1 / 0; some of
# them are inconsistent (inside + not attributed + foreign actors) but possible in the DB
_LOCS = ("UA", "RU", None, "")
_ACTORS = ("[]", '["UA"]', '["RU"]', '["RU","UA"]', '["UA",""]', '["UA",5]', '[null,"UA"]',
           '[5]', "", '{"a":"UA"}', '"UA"', '["PL","RU"]')  # NOT NULL column
_ATTR = (1, 0)  # NOT NULL in the schema


@pytest.fixture
def shapes_store(tmp_path):
    from osint.store.sqlite import SqliteStore
    from tests.country_fixture import _row

    s = SqliteStore(tmp_path / "shapes.sqlite")
    combos = [(loc, acs, att) for loc in _LOCS for acs in _ACTORS for att in _ATTR]
    s.save_events([_row(f"S{i:03d}", "UA", 5 + i % 50, "armed_conflict")
                   for i, _ in enumerate(combos)])
    with s._lock, s._db:
        for i, (loc, acs, att) in enumerate(combos):
            s._db.execute("UPDATE events SET country=?, actor_countries=?, attributed=?, "
                          "lifecycle='active' WHERE id=?", (loc, acs, att, f"S{i:03d}"))
    yield s, combos
    s.close()


def test_sql_filters_mirror_event_ctx_for_every_shape(shapes_store):
    """row in filter F  <=>  event_ctx(row) == F, for every location x involvement."""
    from osint.store.country_sql import INVOLVEMENTS, LOCATIONS

    s, combos = shapes_store
    want_all = {f"S{i:03d}" for i, (loc, acs, _a) in enumerate(combos)
                if loc == "UA" or "UA" in as_list(acs)}
    ctx = {f"S{i:03d}": event_ctx("UA", loc, acs, att) for i, (loc, acs, att) in enumerate(combos)}
    base = s.list_events(country="UA", limit=10_000)
    assert {it["id"] for it in base} == want_all
    assert all(it["ctx"] == ctx[it["id"]] for it in base)
    for loc in LOCATIONS:
        for inv in INVOLVEMENTS:
            got = {it["id"] for it in s.list_events(country="UA", location=loc,
                                                     involvement=inv, limit=10_000)}
            want = {i for i in want_all
                    if loc in ("all", ctx[i]["location"]) and inv in ("all", ctx[i]["involvement"])}
            assert got == want, (loc, inv, sorted(got ^ want))
    # the summary counts come from the same ctx: they add up per axis
    rows = s.country_events("UA", since=T0 - timedelta(days=1))
    assert {r["id"] for r in rows} == want_all
