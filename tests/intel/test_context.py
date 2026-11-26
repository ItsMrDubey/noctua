"""Site-context sections of the intel console (osint.intel.context) against small fixtures:
a real SqliteStore, signal snapshots, infrastructure files, a cities1000 index, a fake lake
and a fake Open-Meteo / Wikimedia (no network)."""
import csv
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.intel import mount_intel
from osint.intel import aggregate, context, hooks
from osint.signals.cache import write_signal
from osint.store.sqlite import SqliteStore, iso
from tests.intel.conftest import SAT_ROWS

NOW = datetime.now(UTC).replace(microsecond=0)
LAT, LON = 47.76, 39.93
EID = "01EVCTX0000000000000000001"


def _ev(eid, lat, lon, t, *, status="multi-source", country="RU", origin=None, **kw):
    e = {"id": eid, "category": kw.pop("category", "armed_conflict"), "title": kw.pop("title", "x"),
         "summary": "", "actors": [], "lat": lat, "lon": lon, "precision": "city",
         "place_name": "Novoshakhtinsk", "country": country, "geo_confidence": 0.9,
         "t_start": iso(t), "t_end": iso(t), "status": status, "confidence": 0.8, "version": 1,
         "mention_count": 1, "source_diversity": 1, "first_seen": iso(t), "updated_at": iso(t), **kw}
    if origin:
        e["origin"] = origin
    return e


def _cities(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE places (country TEXT, k TEXT, name TEXT, admin1 TEXT, lat REAL, "
               "lon REAL, pop INTEGER)")
    rows = [("RU", "novoshakhtinsk", "Novoshakhtinsk", "61", 47.7579, 39.9364, 106_000),
            ("RU", "новошахтинск", "Novoshakhtinsk", "61", 47.7579, 39.9364, 106_000),  # alias row
            ("RU", "shakhty", "Shakhty", "61", 47.7085, 40.2160, 230_000),
            ("RU", "shakhty-district", "Shakhty Artyom", "61", 47.7100, 40.2300, 5_000),  # district
            ("RU", "gukovo", "Gukovo", "61", 48.0447, 39.9484, 62_000),
            ("UA", "far", "Far Town", "x", 49.5, 36.0, 1_000_000)]
    db.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


@pytest.fixture
def rt(tmp_path):
    store = SqliteStore(tmp_path / "osint.sqlite")
    t_ev = NOW - timedelta(hours=7)
    evs = [_ev(EID, LAT, LON, t_ev),
           *[_ev(f"01EVTEMPO{i:02d}", 47.5, 39.5, NOW - timedelta(days=i % 20, hours=1))
             for i in range(30)],
           _ev("ucdp-1", 47.9, 39.95, datetime(2026, 5, 3, tzinfo=UTC), status="historical",
               country="Russia (Soviet Union)", origin="historical"),
           _ev("ucdp-2", 48.0, 39.0, datetime(2026, 5, 9, tzinfo=UTC), status="historical",
               country="Russia (Soviet Union)", origin="historical")]
    docs = [{"event_id": EID, "doc_id": f"d{i}", "source": f"rss:o{i % 3}", "url": "https://e/x",
             "title": "t", "published_at": iso(NOW - timedelta(days=i % 5, hours=1)),
             "language": "en" if i % 2 else "ru", "role": "mention", "added_at": iso(NOW)}
            for i in range(10)]
    store.save_events(evs, documents=docs, fields=[
        {"event_id": "ucdp-1", "field": "fatalities_best", "value": "4", "doc_id": "u"},
        {"event_id": "ucdp-1", "field": "type_of_violence", "value": "state-based", "doc_id": "u"},
        {"event_id": "ucdp-2", "field": "fatalities_best", "value": "2", "doc_id": "u"},
        {"event_id": "ucdp-2", "field": "type_of_violence", "value": "one-sided", "doc_id": "u"}])
    (tmp_path / "gazetteer").mkdir()
    _cities(tmp_path / "gazetteer" / "cities1000.sqlite")
    (tmp_path / "gazetteer" / "countryInfo.txt").write_text(
        "#ISO\tISO3\tnum\tfips\tCountry\n" "RU\tRUS\t643\tRS\tRussia\n", encoding="utf-8")
    t = NOW.timestamp()
    write_signal(tmp_path, "satellites", {"updated": t, "objects": SAT_ROWS})
    write_signal(tmp_path, "gnss", {"updated": t, "window_min": 60, "thresholds": {"min_aircraft": 3},
                                    "cells": [{"h3": "83a", "lat": 47.9, "lon": 40.1, "n": 12, "bad": 5,
                                               "pct": 41.7, "level": "high", "b": [], "carried": 0},
                                              {"h3": "83b", "lat": 52.0, "lon": 20.0, "n": 9, "bad": 0,
                                               "pct": 0.0, "level": "low", "b": [], "carried": 0}]})
    write_signal(tmp_path, "outages", {"updated": t, "window_h": 24.0, "countries": [
        {"cc": "RU", "name": "Russia", "severity": 0.6, "score": 30000.0, "datasources": ["bgp"],
         "ongoing": True, "from": t - 3600, "until": t, "events": 1, "level": "high",
         "lat": 55, "lon": 37}], "events": [{"cc": "RU", "datasource": "bgp", "score": 30000.0}]})
    write_signal(tmp_path, "radiation", {"updated": t, "stations": [
        {"id": "sc:1", "source": "safecast", "name": "Rostov probe", "country": "RU", "lat": 47.23,
         "lon": 39.7, "ts": iso(NOW), "value": 0.11, "unit": "µSv/h"}]})
    write_signal(tmp_path, "radiation_baseline", {"updated": t, "thresholds": {},
                                                  "stations": {"sc:1": [0.1, 40]}})
    write_signal(tmp_path, "spaceweather", {"updated": t, "kp_now": [t, 2.3],
                                            "scales": {"now": {"G": 0, "S": 0, "R": 0}}})
    write_signal(tmp_path, "squawks", {"updated": t, "items": [
        {"id": "a:7700", "code": "7700", "label": "General emergency", "flight": "AFL1",
         "lat": 47.5, "lon": 40.0, "first_seen": t, "last_seen": t}]})
    ingestor = SimpleNamespace(live={}, _lock=threading.Lock())
    r = SimpleNamespace(store=store, lake=FakeLake(), ingestor=ingestor,
                        settings=SimpleNamespace(data_dir=tmp_path, config_dir=tmp_path, api_token=""))
    yield r
    store.close()


class FakeLake:
    def query(self, sql, params=None, start=None, end=None):
        us = lambda dt: int(dt.timestamp() * 1e6)
        if "count(DISTINCT track_id)" in sql:
            h = int(NOW.timestamp()) // 3600
            return [("air", h - 2, 3), ("sea", h - 1, 1)]
        if "max(gap)" in sql:
            return [("air:mil1", "air", 40, us(NOW - timedelta(hours=3)), us(NOW - timedelta(hours=2)),
                     4.2, 60e6),
                    ("sea:2731", "sea", 9, us(NOW - timedelta(hours=20)), us(NOW - timedelta(hours=1)),
                     12.0, 150 * 60e6)]
        return []


@pytest.fixture
def subj(rt):
    s = aggregate.resolve_subject(rt, kind="event", id=EID)
    s["cc"] = context.resolve_cc(rt, s)
    return s


def test_environment_light_and_weather(rt, subj, _offline):
    E = context.section_environment(rt, subj, NOW)
    assert E["light"]["event"]["phase"] in ("day", "night") or "twilight" in E["light"]["event"]["phase"]
    assert set(E["sun_next"]) == {"sunrise", "sunset", "civil_dawn", "civil_dusk"}
    W = E["weather"]
    assert W["current"]["label"] == "overcast" and W["downwind"] == "N"  # wind from 190 deg
    assert W["at_event"]["time"].endswith("Z") and W["at_event"]["wind_direction_10m"] == 270
    assert W["next48"]["cloud_max"] <= 100 and len(W["series"]["time"]) >= 70
    assert "model" in W["source"] and _offline.calls == 1


def test_environment_weather_failure_is_contained(rt, subj, monkeypatch):
    class Down:
        def get(self, lat, lon):
            raise httpx.ConnectError("offline")
    monkeypatch.setattr(context, "WEATHER", Down())
    E = context.section_environment(rt, subj, NOW)
    assert "Open-Meteo" in E["weather"]["error"] and E["light"]["now"]["sun_elev_deg"] is not None


def test_overhead_passes_with_cloud_and_event_split(rt, subj):
    O = context.section_overhead(rt, subj, NOW)
    assert O["satellites"] == ["LANDSAT 8", "SENTINEL-1C", "SENTINEL-2B"] and O["passes"]
    assert O["event_t"] and all("cloud_pct" in p for p in O["passes"])
    sar = O["by_kind"]["sar"]
    if sar["after_event"]:
        assert sar["after_event"]["t"] >= O["event_t"]
    if sar["before_event"]:
        assert sar["before_event"]["t"] < O["event_t"]
    assert "acquisition depends" in O["basis"]
    # cached per site/hour: a second call reuses the computed passes
    assert context.section_overhead(rt, subj, NOW)["passes"] == O["passes"]


def test_signals_at_site(rt, subj):
    G = context.section_signals(rt, subj, NOW)
    assert G["gnss"]["site"]["level"] == "high" and G["gnss"]["site"]["pct"] == 41.7
    assert [c["h3"] for c in G["gnss"]["cells"]] == ["83a"]  # the Polish cell is > 300 km away
    assert G["outage"]["country"]["ongoing"] and G["outage"]["datasources"] == ["bgp"]
    st = G["radiation"]["stations"][0]
    assert st["name"] == "Rostov probe" and st["baseline"] == 0.1 and st["distance_km"] < 70
    assert G["space"]["kp_now"][1] == 2.3 and G["squawks"]["items"][0]["code"] == "7700"


def test_exposure_infra_and_population(rt, subj, tmp_path, monkeypatch):
    from osint.infra import loader

    ref = tmp_path / "ref"
    ref.mkdir()
    with (ref / "gppd.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "gppd_idnr", "capacity_mw", "latitude", "longitude", "primary_fuel",
                    "country_long"])
        w.writerow(["Novocherkassk GRES", "RU1", "2258", "47.20", "40.21", "Coal", "Russia"])
        w.writerow(["Shakhty CHP", "RU2", "120", "47.71", "40.20", "Gas", "Russia"])
        w.writerow(["Far plant", "RU3", "999", "50.0", "45.0", "Gas", "Russia"])
    (ref / "refineries_osm.json").write_text(json.dumps([
        {"id": "r1", "name": "NZNP", "lat": 47.80, "lon": 39.88, "operator": "NZNP"}]))
    (ref / "pipelines_osm.json").write_text(json.dumps([
        {"id": "p1", "name": "Line A", "substance": "oil", "line": [[39.0, 47.77], [41.0, 47.77]]},
        {"id": "p2", "name": "Far line", "substance": "gas", "line": [[10.0, 10.0], [11.0, 11.0]]}]))
    (ref / "cables_tg.json").write_text(json.dumps({"cables": [], "landings": []}))
    monkeypatch.setattr(loader, "get_infra", lambda: loader.Infra(ref))
    X = context.section_exposure(rt, subj, NOW)
    assert X["power"]["count"] == 1 and X["power"]["mw_total"] == 120  # GRES is ~65 km away
    assert X["refineries"]["items"][0]["name"] == "NZNP"
    assert X["pipelines"]["items"][0]["name"] == "Line A" and X["pipelines"]["items"][0]["distance_km"] < 2
    pop = X["population"]
    # Shakhty's district within its urban radius is folded in; the alias row counts once
    assert pop["rings"]["10"] == 106_000 and pop["folded_districts"] == 1
    assert pop["rings"]["50"] == 106_000 + 230_000 + 62_000 and "not a gridded" in pop["basis"]


def test_activity_24h_gaps_and_watch(rt, subj):
    db = sqlite3.connect(rt.settings.data_dir / "infra_watch.sqlite")
    db.executescript("CREATE TABLE active (track_id TEXT PRIMARY KEY, severity TEXT, name TEXT, "
                     "lat REAL, lon REAL, ts TEXT, hits TEXT);"
                     "CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, track_id TEXT, "
                     "severity TEXT, title TEXT, sub TEXT, lat REAL, lon REAL, detail TEXT);")
    db.execute("INSERT INTO active VALUES (?,?,?,?,?,?,?)", (
        "sea:2731", "high", "X", LAT, LON, iso(NOW), json.dumps([{"entry": {
            "name": "SHADOW", "risk": ["Shadow fleet"], "source": "opensanctions"},
            "matched_on": "imo", "strength": "strong"}])))
    db.execute("INSERT INTO alerts VALUES (1,?,?,?,?,?,?,?,?,?)", (
        iso(NOW - timedelta(hours=2)), "loiter", "sea:2731", "warn", "Loitering", "", LAT, LON, "{}"))
    db.commit()
    db.close()
    if context.watch_flags not in hooks.TRACK_FLAGGERS:
        hooks.TRACK_FLAGGERS.append(context.watch_flags)
    try:
        A = context.section_activity(rt, subj, NOW)
    finally:
        hooks.TRACK_FLAGGERS.remove(context.watch_flags)
    assert (A["aircraft"], A["ships"], A["flagged"]) == (1, 1, 1)
    assert A["series"]["aircraft"][22] == 3 and A["series"]["ship"][23] == 1
    gap = A["gaps"][0]
    assert gap["id"] == "sea:2731" and gap["max_gap_min"] == 150
    assert gap["flags"][0]["label"] == "Shadow fleet" and "imo" in gap["flags"][0]["source"]
    assert A["watch_alerts"][0]["title"] == "Loitering"


def test_country_history_tempo_and_attention(rt, subj):
    C = context.section_country(rt, subj, NOW)
    assert C["cc"] == "RU" and C["name"] == "Russia"
    H = C["history"]
    assert H["total"] == 2 and H["fatalities_best"] == 6 and set(H["months"]) == {"2026-05"}
    assert H["by_type"] == {"state-based": 1, "one-sided": 1}
    T = C["tempo"]
    assert len(T["days"]) == 37 and T["last7"] >= 1 and T["baseline_per_day"] is not None
    N = context.section_attention(rt, subj, NOW)
    assert len(N["documents"]) == 30 and sum(N["documents"]) == 10
    assert max(N["outlets"]) <= 3


def test_resolve_cc_reverse_geocodes_points(rt):
    s = {"kind": "point", "lat": 47.70, "lon": 40.21}
    assert context.resolve_cc(rt, s) == "RU"
    assert context.resolve_cc(rt, {"kind": "point", "lat": 0.0, "lon": -30.0}) is None


def test_pageviews_ratio_cache_and_404():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "Missing" in str(req.url):
            return httpx.Response(404)
        assert "/user/Rostov-on-Don/daily/" in req.url.path
        items = [{"timestamp": (NOW - timedelta(days=60 - i)).strftime("%Y%m%d00"),
                  "views": 100 if i < 57 else 400} for i in range(60)]
        return httpx.Response(200, json={"items": items})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pv = context.pageviews("Rostov-on-Don", now=NOW, client=client)
    assert len(pv["views"]) == 60 and pv["baseline_median"] == 100 and pv["ratio"] >= 3
    context.pageviews("Rostov-on-Don", now=NOW, client=client)
    assert len(calls) == 1  # cached
    assert context.pageviews("Missing page", now=NOW, client=client)["missing"]


def test_api_mounts_context_sections_and_flagger(rt):
    app = FastAPI()
    mount_intel(app, rt)
    c = TestClient(app)
    try:
        j = c.get(f"/api/intel?kind=event&id={EID}&sections=environment,signals,country").json()
        assert j["subject"]["cc"] == "RU"
        assert not [k for k, v in j["sections"].items() if "error" in v]
        assert c.get("/api/intel?kind=actor&q=x&sections=environment").json()["sections"]["environment"] is None
        assert c.get("/api/intel/pageviews?title=X&project=evil.example").status_code == 422
    finally:
        if context.watch_flags in hooks.TRACK_FLAGGERS:
            hooks.TRACK_FLAGGERS.remove(context.watch_flags)


def test_ttl_cache_expiry():
    clock = [0.0]
    c = context.TTLCache(10, 4, clock=lambda: clock[0])
    c.put("a", 1)
    assert c.get("a") == 1
    clock[0] = 11
    assert c.get("a") is None
    for i in range(6):
        c.put(i, i)
    assert len(c._d) <= 5


def test_resolve_cc_trusts_event_country_only_after_the_migration(rt):
    from osint.store.sqlite import COUNTRY_MIGRATION_STATE

    s = {"kind": "event", "event": {"country": "GM"}, "lat": 47.70, "lon": 40.21}
    assert context.resolve_cc(rt, s) == "RU"  # unmigrated "GM" may be FIPS: lat/lon wins
    assert context.resolve_cc(rt, {"kind": "event",
                                   "event": {"country": "Russia (Soviet Union)"}}) == "RU"
    rt.store.set_cursor(COUNTRY_MIGRATION_STATE, {"done": True})
    assert context.resolve_cc(rt, s) == "GM"


def test_country_history_counts_iso2_normalised_rows(rt, subj):
    rt.store.save_events([_ev("ucdp-3", 47.9, 39.95, datetime(2026, 5, 20, tzinfo=UTC),
                              status="historical", country="RU", origin="historical")])
    assert context.section_country(rt, subj, NOW)["history"]["total"] == 3
