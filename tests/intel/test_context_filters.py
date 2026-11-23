"""Intel console relevance filters: theatre storylines need a member near the point (not just
an overlapping bounding box), AIS-gap notability is limited to seagoing silences in open water,
and Wikidata nuclear sites never show a bare QID as their name."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import h3
import pytest

from osint.intel import context
from osint.naval import geo as naval_geo
from osint.reference import nuclear
from osint.storylines.store import StorylineStore

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
FRA = (49.9858, 8.3607)  # Frankfurt approach


# ---------------------------------------------------------------- storylines near a point

def _story(sid, members, t_last=NOW):
    lats = [m[0] for m in members]
    lons = [m[1] for m in members]
    return {"id": sid, "title": sid, "title_basis": "x", "theatre": "t", "theatre_label": "T",
            "t_first": (t_last - timedelta(days=1)).isoformat(), "t_first_basis": "x",
            "t_last": t_last.isoformat(), "n_events": len(members), "n_news": len(members),
            "n_sensor": 0, "representative_event_id": f"{sid}-0",
            "categories": [], "places": [], "actors": [], "countries": [], "roles": {},
            "min_lat": min(lats), "min_lon": min(lons), "max_lat": max(lats),
            "max_lon": max(lons), "lat": lats[0], "lon": lons[0], "origin_event_id": "E0",
            "fingerprint": sid, "links": [],
            "members": [{"event_id": f"{sid}-{i}", "storyline_id": sid, "role": "origin",
                         "role_basis": "", "title": "", "mentions": 1, "sources": 1,
                         "origin": "news", "category": "conflict", "t": t_last.isoformat(),
                         "t_basis": "first_report", "lat": la, "lon": lo}
                        for i, (la, lo) in enumerate(members)]}


@pytest.fixture
def stories(tmp_path):
    st = StorylineStore(tmp_path / "storylines.sqlite")
    st.sync([
        # Kyiv thread with one outlier member in France: its bounds cover Germany
        _story("kyiv", [(50.45, 30.52), (50.40, 30.60), (45.10, 3.13)]),
        # Hormuz thread with a member in Scotland: bounds cover all of Europe
        _story("hormuz", [(26.6, 56.4), (57.5, -4.0)]),
        # a Kyiv thread with one reaction event in Bonn (~150 km): one of five is not "here"
        _story("kyiv-eu", [(50.45, 30.52), (50.40, 30.60), (50.5, 30.4), (50.3, 30.7),
                           (50.73, 7.10)]),
        # a German thread 150 km from the point
        _story("germany", [(51.18, 9.13), (51.0, 9.5)]),
    ])
    yield st
    st.close()


def test_storylines_near_need_a_member_in_range(stories):
    bbox_only = {s["id"] for s in stories.list(bbox=(2.0, 46.0, 14.0, 54.0))}
    assert bbox_only == {"kyiv", "kyiv-eu", "hormuz", "germany"}  # the old (broken) theatre filter
    near = stories.list(near=(*FRA, 400.0), since=NOW - timedelta(days=7))
    assert [s["id"] for s in near] == ["germany"]
    assert 100 < near[0]["distance_km"] < 200


def test_country_section_lists_only_nearby_storylines(stories, monkeypatch):
    import osint.storylines.store as sstore

    monkeypatch.setattr(sstore, "get_store", lambda runtime: stories)
    rt = SimpleNamespace(store=SimpleNamespace(country_history=lambda names: [],
                                               country_tempo=lambda cc, since: []))
    monkeypatch.setattr(context, "country_names", lambda runtime: {"DE": "Germany"})
    C = context.section_country(rt, {"cc": "DE", "lat": FRA[0], "lon": FRA[1]}, NOW)
    assert [s["id"] for s in C["storylines"]] == ["germany"]
    assert C["storylines"][0]["distance_km"] <= 400


# ---------------------------------------------------------------- AIS gap notability

class _Mask:
    """Land = the res-4 cells of the given points; coast distance from a fixed table."""

    def __init__(self, land_pts, coast_km):
        self.land = {naval_geo.cell(*p) for p in land_pts}
        self._d = coast_km

    def is_land_cell(self, c):
        return c in self.land

    def coast_km(self, lat, lon):
        return self._d


@pytest.fixture
def mask(monkeypatch):
    holder = {}

    def use(land_pts, coast_km):
        holder["geo"] = SimpleNamespace(mask=_Mask(land_pts, coast_km))
    monkeypatch.setattr(naval_geo, "get_geo", lambda ref_dir=None: holder["geo"])
    return use


def test_rhine_barge_gap_is_not_notable(mask):
    mainz = (49.99, 8.27)
    mask([mainz], 0.0)
    g = {"gap_at": list(mainz), "max_gap_min": 332, "mean_interval_min": 3.0}
    n = context.ais_gap_notability(g)
    assert n["water"] == "inland_or_port" and not n["notable"]


def test_open_sea_gap_scales_with_reporting_rate(mask):
    mask([(0.0, 0.0)], 80.0)  # some land elsewhere; the gap is 80 km offshore
    sea = [57.0, 19.0]
    assert context.ais_gap_notability(
        {"gap_at": sea, "max_gap_min": 240, "mean_interval_min": 4.0})["notable"]
    # a ship that only reaches the platform every 30 min (sparse terrestrial coverage):
    # 4 h of silence is its normal rhythm, not a dark period
    sparse = context.ais_gap_notability(
        {"gap_at": sea, "max_gap_min": 240, "mean_interval_min": 30.0})
    assert not sparse["notable"] and sparse["threshold_min"] == 360


def test_coastal_and_unclassifiable_gaps_are_not_notable(mask):
    mask([(0.0, 0.0)], 5.0)
    assert context.ais_gap_notability(
        {"gap_at": [54.3, 10.2], "max_gap_min": 600, "mean_interval_min": 2.0})["water"] == "coastal"
    assert context.ais_gap_notability({"gap_at": None, "max_gap_min": 600})["notable"] is False


def test_activity_gaps_carry_notability(mask, monkeypatch):
    mask([(49.99, 8.27)], 0.0)
    us = lambda dt: int(dt.timestamp() * 1e6)

    class Lake:
        def query(self, sql, params=None, start=None, end=None):
            if "count(DISTINCT track_id)" in sql:
                return []
            return [("sea:211000001", "sea", 60, us(NOW - timedelta(hours=20)),
                     us(NOW - timedelta(hours=1)), 3.0, 332 * 60e6, 49.99, 8.27)]

    rt = SimpleNamespace(lake=Lake(), store=SimpleNamespace(get_tracks=lambda ids: {}),
                         settings=SimpleNamespace(data_dir=None))
    monkeypatch.setattr(context, "_watch_db", lambda runtime: None)
    A = context.section_activity(rt, {"lat": 49.99, "lon": 8.27, "radius_km": 25.0}, NOW)
    g = A["gaps"][0]
    assert g["max_gap_min"] == 332 and g["water"] == "inland_or_port" and g["notable"] is False


# ---------------------------------------------------------------- nuclear labels

def _binding(qid, kind, label=None, **kw):
    b = {"i": {"value": f"http://www.wikidata.org/entity/{qid}"}, "kind": {"value": kind},
         "coord": {"value": "Point(11.342 53.0333)"}}
    if label is not None:
        b["label"] = {"value": label}
    for k, v in kw.items():
        b[k] = {"value": v}
    return b


def test_wikidata_sites_never_named_by_qid(tmp_path):
    p = tmp_path / "nuclear_wikidata.json"
    p.write_text(json.dumps({"results": {"bindings": [
        _binding("Q127155785", "storage", "Q127155785", operator="BGZ Zwischenlager",
                 country="Germany", state_of_use="in use"),
        _binding("Q1438112", "research", country="France", state_of_use="in use"),
        _binding("Q555", "power", "Q555", label_any="Kernkraftwerk Philippsburg",
                 country="Germany", state_of_use="in use"),
        _binding("Q556", "power", "Gundremmingen", country="Germany", state_of_use="in use"),
    ]}}), encoding="utf-8")
    sites = {x["wikidata"]: x for x in nuclear.load_wikidata(p)}
    assert sites["Q127155785"]["name"] == "Nuclear storage site (BGZ Zwischenlager)"
    assert sites["Q1438112"]["name"] == "Research reactor, France"
    assert sites["Q555"]["name"] == "Kernkraftwerk Philippsburg"
    assert sites["Q556"]["name"] == "Gundremmingen" and sites["Q556"]["label_resolved"]
    assert not sites["Q1438112"]["label_resolved"]
    assert not any(h3.is_valid_cell(x["name"]) or x["name"].startswith("Q") for x in sites.values())
