import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from osint.core.models import Position
from osint.infra.job import DEFAULTS, Proximity, WatchJob, WatchStore, coast_points, load_config
from osint.infra.loader import Infra
from tests.infra.test_watchlist import MARITIME, TARGETS

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
CABLES = {"cables": [{"id": "c1", "name": "Baltic One", "color": None,
                      "lines": [[[20.0, 58.0], [22.0, 58.0]]]}], "landings": []}


def pos(tid, minutes, lat, lon, kts):
    return Position(track_id=tid, kind="sea", ts=T0 + timedelta(minutes=minutes), lat=lat, lon=lon,
                    source="t", speed_kts=kts)


@pytest.fixture
def ref(tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    (d / "cables_tg.json").write_text(json.dumps(CABLES), encoding="utf-8")
    (d / "os_maritime.csv").write_text(MARITIME, encoding="utf-8")
    (d / "os_targets_va.csv").write_text(TARGETS, encoding="utf-8")
    return d


def prox(ref, **over):
    cfg = {**DEFAULTS["proximity"], **over}
    return Proximity(cfg, Infra(ref), ports=[{"lat": 59.0, "lon": 24.0}],
                     coast=[(57.0, 25.0)])


def test_loiter_then_dark(ref):
    p = prox(ref)
    tid, meta = "sea:230000001", {"name": "ANCHOR DRAGGER", "category": "Cargo"}
    assert p.step(pos(tid, -60, 57.5, 21.0, 11.0), meta, T0, True) == []  # under way, off-cable
    out = []
    for m in range(0, 40, 5):  # 0.5 kn, ~0.6 km north of the cable for 35 min
        out += p.step(pos(tid, m, 58.005, 21.0, 0.5), meta, T0 + timedelta(minutes=m), True)
    assert len(out) == 1 and out[0]["kind"] == "proximity"
    assert "Baltic One" in out[0]["sub"] and out[0]["detail"]["minutes"] >= 30
    last = pos(tid, 35, 58.005, 21.0, 0.5)
    assert p.step(last, meta, T0 + timedelta(minutes=60), True) == []  # silent 25 min: not yet
    dark = p.step(last, meta, T0 + timedelta(minutes=100), True)
    assert len(dark) == 1 and dark[0]["kind"] == "dark" and dark[0]["title"].startswith("AIS dark")
    assert p.step(last, meta, T0 + timedelta(minutes=130), True) == []  # once


def test_proximity_filters(ref):
    p = prox(ref)
    # never seen under way (anchored since before we watched): no alert
    tid = "sea:2"
    assert all(p.step(pos(tid, m, 58.005, 21.0, 0.0), {"category": "Tanker"}, T0, True) == []
               for m in range(0, 60, 5))
    assert p.state[tid]["transit"] is False
    # excluded category, and a fast vessel over the cable
    for tid2, meta, kts in (("sea:3", {"category": "Fishing"}, 0.5), ("sea:4", {}, 12.0)):
        p.step(pos(tid2, -30, 57.5, 21.0, 11.0), meta, T0, True)
        assert all(p.step(pos(tid2, m, 58.005, 21.0, kts), meta, T0, True) == [] for m in range(0, 60, 5))
    p.forget({"sea:3"})
    assert "sea:2" not in p.state and "sea:3" in p.moving and "sea:4" not in p.moving


def test_coast_points(tmp_path):
    gj = {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[1, 2], [3, 4], [1, 2]]]}},
                       {"geometry": {"type": "MultiPolygon", "coordinates": [[[[5, 6], [7, 8]]]]}}]}
    (tmp_path / "c.geojson").write_text(json.dumps(gj), encoding="utf-8")
    assert coast_points(tmp_path / "c.geojson") == [(2, 1), (4, 3), (2, 1), (6, 5), (8, 7)]
    assert coast_points(tmp_path / "none.geojson") == []


class FakeStore:
    def __init__(self, tracks):
        self.tracks = tracks

    def get_tracks(self, ids):
        return {i: self.tracks[i] for i in ids if i in self.tracks}


def test_watch_job_pass(ref, tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "watchlist.yaml").write_text("entries:\n  - icao: ae0001\n    label: Watched jet\n",
                                            encoding="utf-8")
    live = {
        "sea:273125140": pos("sea:273125140", 0, 40.0, 10.0, 8.0),
        "sea:999": pos("sea:999", 0, 41.0, 10.0, 8.0),
        "air:ae0001": Position(track_id="air:ae0001", kind="air", ts=T0, lat=50, lon=8, source="t"),
        "sea:old": pos("sea:old", -600, 41.0, 11.0, 8.0),
    }
    rt = SimpleNamespace(
        settings=SimpleNamespace(config_dir=cfg_dir),
        ingestor=SimpleNamespace(_lock=threading.Lock(), live=live),
        store=FakeStore({"sea:273125140": {"name": "FIANIT", "extra": {"imo": "9319703"}}}))
    cfg = load_config(cfg_dir / "infra.yaml")
    cfg["job"]["auto_refresh"] = False
    store = WatchStore(tmp_path / "w.sqlite")
    try:
        job = WatchJob(rt, cfg, store, ref_dir=ref)
        assert job.maybe_refresh() is False
        last = job.run_pass(T0 + timedelta(minutes=1))
        assert last["matched"] == 2 and last["watch_alerts"] == 2
        act = {r["track_id"]: r for r in store.active()}
        assert act["sea:273125140"]["severity"] == "high" and act["sea:273125140"]["name"] == "FIANIT"
        assert act["air:ae0001"]["hits"][0]["entry"]["source"] == "watchlist"
        al = store.alerts()
        assert {a["kind"] for a in al} == {"watchlist"}
        assert any("Shadow fleet" in a["title"] for a in al)
        # a second pass raises nothing new; the API's since_id paging sees nothing either
        assert job.run_pass(T0 + timedelta(minutes=2))["watch_alerts"] == 0
        assert store.alerts(since_id=max(a["id"] for a in al)) == []
        assert store.get_meta("status")["matched"] == 2
        # re-alert after realert_h unseen
        assert store.sight("sea:273125140", "watch:v-shadow", T0 + timedelta(hours=30), timedelta(hours=24))
    finally:
        store.close()
