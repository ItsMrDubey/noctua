"""GET /api/events in country mode (spec B 2.1): location / involvement / counterpart
filters, ctx, ordering, window, paging, the ids-only form and 400 for bad input."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import events as events_api
from osint.api.events import mount_events
from tests.country_fixture import T0, build_store

Q = "/api/events?all=true&country=UA"


class _RT:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def store(tmp_path):
    s = build_store(tmp_path / "e.sqlite")
    yield s
    s.close()


@pytest.fixture
def client(store, monkeypatch):
    # the fixture's events are dated T0; pin "now" so the default 7-day window holds them
    monkeypatch.setattr(events_api, "_utcnow", lambda: T0 + timedelta(hours=1))
    app = FastAPI()
    mount_events(app, _RT(store))
    return TestClient(app)


def ids(resp):
    assert resp.status_code == 200, resp.text
    return [it["id"] for it in resp.json()["items"]]


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_location_involvement_and_order(client):
    assert ids(client.get(Q)) == ["E1", "E2", "E6", "E3", "E4"]
    assert ids(client.get("/api/events?all=true&country=ua")) == ["E1", "E2", "E6", "E3", "E4"]
    assert set(ids(client.get(Q + "&location=inside"))) == {"E1", "E2", "E4"}
    assert set(ids(client.get(Q + "&location=abroad"))) == {"E3", "E6"}
    assert ids(client.get(Q + "&involvement=domestic")) == ["E1"]
    assert set(ids(client.get(Q + "&involvement=foreign"))) == {"E2", "E3", "E6"}
    assert ids(client.get(Q + "&involvement=unattributed")) == ["E4"]
    assert ids(client.get(Q + "&location=inside&involvement=foreign")) == ["E2"]
    assert ids(client.get(Q + "&location=abroad&involvement=domestic")) == []


def test_ctx_from_both_sides(client):
    ua = {it["id"]: it["ctx"] for it in client.get(Q).json()["items"]}
    assert ua["E2"] == {"location": "inside", "involvement": "foreign"}
    assert ua["E4"] == {"location": "inside", "involvement": "unattributed"}
    ru_items = client.get("/api/events?all=true&country=RU").json()["items"]
    ru = {it["id"]: it["ctx"] for it in ru_items}
    assert ru == {"E2": {"location": "abroad", "involvement": "foreign"},
                  "E3": {"location": "inside", "involvement": "foreign"}}
    first = client.get(Q).json()["items"][0]
    assert first["lifecycle"] == "developing" and first["actor_countries"] == ["UA"]
    assert "ctx" not in client.get("/api/events?all=true").json()["items"][0]


def test_counterpart(client):
    assert set(ids(client.get(Q + "&counterpart=RU"))) == {"E2", "E3"}
    assert ids(client.get(Q + "&counterpart=pl")) == ["E6"]
    assert ids(client.get(Q + "&counterpart=FR")) == []


def test_window_paging_ids_and_category(client):
    since, until = z(T0 - timedelta(minutes=35)), z(T0 - timedelta(minutes=15))
    assert ids(client.get(Q + "&since=" + since + "&until=" + until)) == ["E2", "E3"]
    assert ids(client.get(Q + "&limit=2")) == ["E1", "E2"]
    assert ids(client.get(Q + "&limit=2&offset=2")) == ["E6", "E3"]
    j = client.get(Q + "&ids=true").json()
    # ids only: most recently updated first (no lifecycle sort)
    assert j == {"layer": "events", "count": 5, "ids": ["E1", "E2", "E3", "E4", "E6"],
                 "truncated": False}
    assert "E4" not in ids(client.get(Q + "&category=armed_conflict"))
    assert client.get(Q + "&until=never").status_code == 422
    assert client.get(Q + "&offset=-1").status_code == 422


def test_country_window_is_always_bounded(client):
    j = client.get(Q).json()
    assert j["until"].startswith("2026-11-21T13:00:00")
    assert j["since"].startswith("2026-11-14T13:00:00")  # now - 7 days
    # an explicit window is echoed as given; no envelope fields without country
    assert client.get(Q + "&since=2026-11-21T11:00:00Z").json()["since"].startswith(
        "2026-11-21T11:00:00")
    assert "since" not in client.get("/api/events?all=true").json()


def test_default_window_excludes_old_events(store, monkeypatch):
    monkeypatch.setattr(events_api, "_utcnow", lambda: T0 + timedelta(days=30))
    app = FastAPI()
    mount_events(app, _RT(store))
    assert TestClient(app).get(Q).json()["items"] == []


def test_bad_input_is_400(client):
    for url in ("/api/events?country=ZZ", "/api/events?country=UA&counterpart=ZZ"):
        r = client.get(url)
        assert r.status_code == 400 and r.json() == {"error": "unknown_country"}
    r = client.get(Q + "&location=nowhere")
    assert r.status_code == 400 and r.json() == {"error": "bad_location"}
    r = client.get(Q + "&involvement=x")
    assert r.status_code == 400 and r.json() == {"error": "bad_involvement"}
    r = client.get(Q + "&counterpart=ua")
    assert r.status_code == 400 and r.json() == {"error": "bad_counterpart"}


def test_since_is_clamped_to_365_days(client):
    j = client.get(Q + "&since=2020-01-01T00:00:00Z").json()
    assert j["until"].startswith("2026-11-21T13:00:00")
    assert j["since"].startswith("2025-11-21T13:00:00")  # until - 365 days
    # a narrower explicit window is untouched
    assert client.get(Q + "&since=2026-11-21T11:00:00Z").json()["since"].startswith(
        "2026-11-21T11:00:00")


def test_ids_truncated_flag(client):
    full = client.get(Q + "&ids=true").json()
    assert full["truncated"] is False and full["count"] > 1
    cut = client.get(Q + f"&ids=true&limit={full['count']}").json()
    assert cut["truncated"] is True
    assert client.get(Q + "&ids=true&limit=1").json()["truncated"] is True


@pytest.mark.parametrize("extra", [
    "", "&location=inside", "&location=abroad", "&involvement=domestic",
    "&involvement=foreign", "&involvement=unattributed", "&location=inside&involvement=foreign",
    "&counterpart=RU", "&category=disaster", "&category=armed_conflict&location=abroad",
    "&since=2026-11-21T11:25:00Z&until=2026-11-21T11:45:00Z",
])
def test_ids_form_matches_the_full_query(client, extra):
    full = ids(client.get(Q + extra))
    got = client.get(Q + extra + "&ids=true").json()
    assert set(got["ids"]) == set(full) and got["count"] == len(full)
    assert got["truncated"] is False


def test_ids_form_skips_row_decoding_and_ctx(client, monkeypatch):
    """ids=true reads only ids: no _event_out / event_ctx / annotation per row."""
    from osint.store import sqlite as sqlite_mod

    def boom(*_a, **_k):
        raise AssertionError("ids=true must not decode rows")

    monkeypatch.setattr(sqlite_mod, "_event_out", boom)
    monkeypatch.setattr(sqlite_mod, "event_ctx", boom)
    monkeypatch.setattr(events_api, "add_corroboration", boom)
    got = client.get(Q + "&ids=true")
    assert got.status_code == 200 and len(got.json()["ids"]) == 5


def test_ids_query_orders_by_update_without_lifecycle_sort(store):
    """The ids query sorts by updated_at only (the events_updated index), never by the
    lifecycle CASE, so it needs no temp b-tree for its ORDER BY."""
    sql, params = store.event_ids_sql(country="UA", since=T0 - timedelta(days=7), until=T0,
                                      limit=10)
    assert "CASE" not in sql.split("ORDER BY", 1)[1]
    plan = " | ".join(r[3] for r in store._db.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "TEMP B-TREE" not in plan, plan
    assert store.list_event_ids(country="UA", since=T0 - timedelta(days=7), limit=2) == ["E1", "E2"]
