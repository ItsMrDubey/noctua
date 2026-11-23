"""Globe declutter rules (frontend/js/lod.js): height bands, aggregation grids, priorities, the
on-screen marker budget and label placement. lod.js is pure (no Cesium / DOM), so each case
runs it under node and returns JSON; skipped without node."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
LOD = (ROOT / "frontend" / "js" / "lod.js").as_uri()
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(body: str):
    """Evaluate `body` (JS returning a JSON-able value) with lod.js imported as L."""
    src = f'import * as L from "{LOD}";\nconst out = (() => {{ {body} }})();\nconsole.log(JSON.stringify(out));\n'
    r = subprocess.run([NODE, "--input-type=module"], input=src.encode(), capture_output=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


def test_bands():
    assert run("return [2e7, 7e6, 5e6, 1.6e6, 1e6, 1.2e5].map(L.bandOf);") == \
        ["world", "world", "continent", "region", "area", "local"]
    assert run("return L.BUDGET.local === Infinity && L.BUDGET.world < L.BUDGET.continent;") is True


def test_grid_step_snaps_to_powers_of_two_and_is_monotonic():
    steps = run("return [2e7, 5e6, 1.6e6, 4e5, 1e5, 1e4].map((h) => L.gridStep(h, 40, 1.0472, 900));")
    assert all(s > 0 and abs(s - 2 ** round(__import__('math').log2(s))) < 1e-12 for s in steps)
    assert steps == sorted(steps, reverse=True)
    # the same step anywhere within a zoom step: panning does not re-bin
    assert run("return L.gridStep(2.0e7, 22, 1.0472, 900) === L.gridStep(2.2e7, 22, 1.0472, 900);") is True


def test_cell_ids_stable_and_wrap_the_antimeridian():
    r = run("""
      const s = 2;
      return {
        same: L.cellOf(50.1, 30.1, s) === L.cellOf(50.9, 30.9, s),
        other: L.cellOf(50.1, 30.1, s) !== L.cellOf(52.1, 30.1, s),
        wrap: L.cellOf(10, 180, s) === L.cellOf(10, -180, s),
        pole: Number.isFinite(L.cellOf(90, 0, s)) && Number.isFinite(L.cellOf(-90, 0, s)),
      };""")
    assert r == {"same": True, "other": True, "wrap": True, "pole": True}


def test_aggregate_counts_weights_and_representative():
    r = run("""
      const items = [
        { id: "a", lat: 50.2, lon: 30.2, frp: 10, p: 1 }, { id: "b", lat: 50.4, lon: 30.6, frp: 30, p: 5 },
        { id: "c", lat: 10, lon: 10, frp: 5, p: 2 }, { id: "x", lat: null, lon: 1 },
      ];
      const cells = L.aggregate(items, 2, { weight: (it) => it.frp, prio: (it) => it.p, members: true }).sort((a, b) => b.n - a.n);
      return cells.map((c) => ({ n: c.n, sum: c.sum, max: c.max, top: c.top.id, ids: c.ids, lat: +c.lat.toFixed(2) }));""")
    assert r == [{"n": 2, "sum": 40, "max": 30, "top": "b", "ids": ["a", "b"], "lat": 50.3},
                 {"n": 1, "sum": 5, "max": 5, "top": "c", "ids": ["c"], "lat": 10}]
    # every item ends up in exactly one cell (nothing is lost by aggregation)
    assert run("""
      const items = Array.from({ length: 5000 }, (_, i) => ({ id: i, lat: (i * 37) % 170 - 85, lon: (i * 91) % 360 - 180 }));
      return L.aggregate(items, 4).reduce((a, c) => a + c.n, 0);""") == 5000


def test_merge_close_keeps_all_members_and_best_representative():
    r = run("""
      const cells = [
        { n: 3, lat: 50, lon: 30, w: 30, e: 30, s: 50, nn: 50, sum: 0, max: 0, top: { id: "lo" }, topPrio: 10, ids: ["1", "2", "lo"] },
        { n: 1, lat: 50.3, lon: 30.2, w: 30.2, e: 30.2, s: 50.3, nn: 50.3, sum: 0, max: 0, top: { id: "hi" }, topPrio: 50, ids: ["hi"] },
        { n: 2, lat: 10, lon: 10, w: 10, e: 10, s: 10, nn: 10, sum: 0, max: 0, top: { id: "far" }, topPrio: 5, ids: ["f1", "far"] },
      ];
      const m = L.mergeClose(cells, 1).sort((a, b) => b.n - a.n);
      return m.map((c) => [c.n, c.top.id, c.ids.slice().sort()]);""")
    assert r == [[4, "hi", ["1", "2", "hi", "lo"]], [2, "far", ["f1", "far"]]]


def test_event_priority_status_first_then_confidence_recency():
    r = run("""
      const now = Date.parse("2026-11-23T12:00:00Z"), h = 36e5;
      const ev = (status, conf, ageH, cat = "political", precision = "city") => L.eventPriority({ status, confidence: conf, _t0: now - ageH * h, category: cat, precision }, now);
      return {
        corroborated_over_reported: ev("sensor-corroborated", 0.4, 6) > ev("reported", 0.9, 6),
        official_over_multi: ev("official", 0.5, 6) > ev("multi-source", 0.5, 6),
        conf: ev("reported", 0.9, 6) > ev("reported", 0.2, 6),
        recent: ev("reported", 0.5, 1) > ev("reported", 0.5, 72),
        severity: ev("reported", 0.5, 6, "armed_conflict") > ev("reported", 0.5, 6, "economic"),
        country_demoted: ev("reported", 0.5, 6, "political", "country") < ev("reported", 0.5, 6),
        top: L.topCategory(["economic", "armed_conflict", "political"]),
      };""")
    assert r == {"corroborated_over_reported": True, "official_over_multi": True, "conf": True, "recent": True,
                 "severity": True, "country_demoted": True, "top": "armed_conflict"}


def test_track_and_fire_priorities():
    r = run("""
      return {
        watched: L.shipPriority({ lat: 0, lon: 0 }, true),
        mil: L.shipPriority({ is_mil: true, lat: 0, lon: 0 }) > 0,
        fastBlackSea: L.shipPriority({ speed_kts: 30, lat: 44, lon: 33 }) > 0,
        fastAtlantic: L.shipPriority({ speed_kts: 30, lat: 40, lon: -40 }),
        bogusSpeed: L.shipPriority({ speed_kts: 102, lat: 44, lon: 33 }),
        cargo: L.shipPriority({ speed_kts: 12, lat: 44, lon: 33 }),
        hpAircraft: L.aircraftPriority({ high_priority: true }),
        milOverCiv: L.aircraftPriority({ is_mil: true }) > L.aircraftPriority({}),
        fireBelow: L.firePriority({ conflict_score: 40 }, 50),
        fireUnscored: L.firePriority({}, 50),
        fireHigher: L.firePriority({ conflict_score: 90, frp: 5 }, 50) > L.firePriority({ conflict_score: 60, frp: 5 }, 50),
      };""")
    assert r["watched"] is None  # JSON has no Infinity: serialised as null
    assert r == {**r, "mil": True, "fastBlackSea": True, "fastAtlantic": 0, "bogusSpeed": 0, "cargo": 0,
                 "hpAircraft": None, "milOverCiv": True, "fireBelow": 0, "fireUnscored": 0, "fireHigher": True}


def test_allocate_budget_priority_and_min_share():
    r = run("""
      const c = [];
      for (let i = 0; i < 1000; i++) c.push({ layer: "fires", id: "f" + i, prio: 60 + i / 1000 });
      for (let i = 0; i < 50; i++) c.push({ layer: "events", id: "e" + i, prio: 20 + i });
      c.push({ layer: "ships", id: "watched", prio: Infinity });
      c.push({ layer: "ships", id: "zero", prio: 0 });
      const a = L.allocate(c, 100, 0.15);
      const n = (k) => (a.get(k) || new Set()).size;
      return { fires: n("fires"), events: n("events"), ships: [...(a.get("ships") || [])],
               bestFire: a.get("fires").has("f999"), worstFire: a.get("fires").has("f0"), bestEvent: a.get("events").has("e49") };""")
    # events keep their 15 % share although every fire outranks them; the watched ship is free
    assert r == {"fires": 85, "events": 15, "ships": ["watched"], "bestFire": True, "worstFire": False, "bestEvent": True}
    assert run("""const a = L.allocate([{ layer: "x", id: 1, prio: 1 }, { layer: "x", id: 2, prio: 2 }], Infinity);
                  return a.get("x").size;""") == 2
    # the budget is never exceeded, whatever the mix
    assert run("""
      const c = Array.from({ length: 3000 }, (_, i) => ({ layer: ["a", "b", "c", "d"][i % 4], id: i, prio: (i * 7919) % 101 + 1 }));
      const a = L.allocate(c, 450);
      return [...a.values()].reduce((s, x) => s + x.size, 0);""") == 450


def test_place_labels_no_overlap_priority_and_cap():
    r = run("""
      const labels = [
        { x: 0, y: 0, w: 100, h: 14, prio: 1 },     // overlaps the higher-priority one below
        { x: 50, y: 5, w: 100, h: 14, prio: 9 },
        { x: 300, y: 300, w: 80, h: 14, prio: 5 },
        { x: 600, y: 10, w: 80, h: 14, prio: 4 },
        { x: 900, y: 10, w: 80, h: 14, prio: 3 },   // blocked by a fixed glyph box
      ];
      return { all: L.placeLabels(labels, Infinity, 2, 64, [{ x: 920, y: 0, w: 30, h: 30 }]).sort(), cap: L.placeLabels(labels, 2) };""")
    assert r == {"all": [1, 2, 3], "cap": [1, 2]}
    # randomised: accepted boxes never intersect
    assert run("""
      let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
      const ls = Array.from({ length: 400 }, () => ({ x: rnd() * 1500, y: rnd() * 850, w: 40 + rnd() * 160, h: 14, prio: rnd() }));
      const k = L.placeLabels(ls, Infinity, 0);
      for (const i of k) for (const j of k) {
        if (i >= j) continue;
        const a = ls[i], b = ls[j];
        if (a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y) return false;
      }
      return k.length > 20;""") is True


def test_spider_offsets_distinct():
    r = run("const o = L.spiderOffsets(25); return [o.length, new Set(o.map((p) => p.join(','))).size, Math.min(...o.map((p) => Math.hypot(p[0], p[1])))];")
    assert r[0] == 25 and r[1] == 25 and r[2] >= 25


def test_lod_tables():
    r = run("""
      return {
        gw: L.powerMaxDist(2500), big: L.powerMaxDist(1200), small: L.powerMaxDist(5), none: L.powerMaxDist(null),
        largePort: L.portMaxDist("large"), unknownPort: L.portMaxDist(undefined),
        largeAp: L.airportMaxDist({ type: "large_airport" }), milAp: L.airportMaxDist({ type: "small_airport", is_military: true }),
        order: L.powerMaxDist(2500) >= L.powerMaxDist(1200) && L.powerMaxDist(1200) >= L.powerMaxDist(300) && L.powerMaxDist(300) >= L.powerMaxDist(50),
      };""")
    assert r["gw"] is None and r["order"] is True  # Infinity -> null in JSON
    assert r["big"] == 9e6 and r["small"] == 3e5 and r["none"] == 3e5
    assert r["largePort"] > r["unknownPort"] and r["largeAp"] > r["milAp"]
