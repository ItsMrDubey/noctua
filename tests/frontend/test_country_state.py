"""Country pane state (frontend/js/country-state.js): spec normalisation, query strings,
category toggles, labels and the sparkline path. The module is pure (no DOM / Cesium), so
each case runs it under node and returns JSON; skipped without node."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MOD = (ROOT / "frontend" / "js" / "country-state.js").as_uri()
NOW = "Date.UTC(2026, 10, 21, 12)"  # 2026-11-21T12:00Z
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")
DEFAULTS = {
    "type": "country",
    "cc": "UA",
    "location": "all",
    "involvement": "all",
    "window": "7d",
    "from": None,
    "to": None,
    "cats": None,
    "counterpart": None,
}


def run(body: str):
    """Evaluate ``body`` (JS returning a JSON-able value) with country-state.js as S."""
    src = (
        f'import * as S from "{MOD}";\nconst out = (() => {{ {body} }})();\n'
        "console.log(JSON.stringify(out));\n"
    )
    r = subprocess.run(
        [NODE, "--input-type=module"],
        input=src.encode(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_norm_state_defaults_and_validation():
    assert run('return S.normState({ type: "country", cc: "ua" });') == DEFAULTS
    assert (
        run(
            'return S.normState({ cc: "UA", location: "x", involvement: "y", '
            'window: "9d", counterpart: "ua", cats: "z" });'
        )
        == DEFAULTS
    )
    c = run(
        'return S.normState({ cc: "UA", window: "custom", from: "2026-11-01", '
        'to: "bad", counterpart: "ru", cats: ["armed_conflict", 3] });'
    )
    assert (c["window"], c["from"], c["to"], c["counterpart"], c["cats"]) == (
        "custom",
        "2026-11-01",
        None,
        "RU",
        ["armed_conflict"],
    )
    assert run('return S.normState({ cc: "UA", window: "7d", from: "2026-11-01" }).from;') is None
    assert (
        run(
            'return S.resetFilters(S.normState({ cc: "UA", location: "inside", '
            'involvement: "foreign", window: "30d", cats: ["a"], counterpart: "RU" }));'
        )
        == DEFAULTS
    )


def test_window_range():
    def rng(state):
        return run("return S.windowRange(S.normState(" + state + "), " + NOW + ");")

    assert rng("{ cc: 'UA', window: '24h' }") == {
        "since": "2026-11-20T12:00:00.000Z",
        "until": None,
    }
    # CUSTOM without a start date falls back to the 7-day default
    assert rng("{ cc: 'UA', window: 'custom' }") == {
        "since": "2026-11-14T12:00:00.000Z",
        "until": None,
    }
    assert rng("{ cc: 'UA', window: 'custom', from: '2026-11-01', to: '2026-11-10' }") == {
        "since": "2026-11-01T00:00:00Z",
        "until": "2026-11-10T23:59:59Z",
    }


def test_events_and_summary_queries():
    def evq(state, opts):
        return run(
            "return S.eventsQuery(S.normState(" + state + "), { now: " + NOW + ", " + opts + " });"
        )

    assert (
        evq("{ cc: 'UA' }", "allCats: ['a', 'b']")
        == "country=UA&all=true&since=2026-11-14T12%3A00%3A00.000Z&limit=200"
    )
    assert evq(
        "{ cc: 'UA', location: 'abroad', involvement: 'foreign', counterpart: 'RU', "
        "window: '24h', cats: ['b'] }",
        "allCats: ['a', 'b'], offset: 200",
    ) == (
        "country=UA&all=true&location=abroad&involvement=foreign&counterpart=RU"
        "&since=2026-11-20T12%3A00%3A00.000Z&category=b&limit=200&offset=200"
    )
    # every category off: matches nothing on the server
    assert evq("{ cc: 'UA', cats: [] }", "allCats: ['a'], ids: true, limit: 10000") == (
        "country=UA&all=true&since=2026-11-14T12%3A00%3A00.000Z&category=none&limit=10000&ids=true"
    )
    assert (
        run(
            "return S.summaryQuery(S.normState({ cc: 'UA', window: 'custom', "
            "from: '2026-11-01', to: '2026-11-10', cats: ['a', 'b'] }), "
            "{ allCats: ['a', 'b'] });"
        )
        == "since=2026-11-01T00%3A00%3A00Z&until=2026-11-10T23%3A59%3A59Z"
    )


def test_toggle_cat():
    assert run(
        "return [S.toggleCat(null, 'a', ['a', 'b']), "
        "S.toggleCat(['b'], 'a', ['a', 'b']), "
        "S.toggleCat(['b'], 'b', ['a', 'b'])];"
    ) == [["b"], None, []]


def test_labels_flag_spark_and_empty_text():
    assert run(
        "return [S.ctxLabel({ location: 'inside', involvement: 'foreign' }), "
        "S.ctxLabel({ location: 'inside', involvement: 'unattributed' }), "
        "S.ctxLabel(null)];"
    ) == ["INSIDE·FOREIGN", "INSIDE·UNATTRIB.", ""]
    assert run("return [S.flagEmoji('UA'), S.flagEmoji('u1')];") == ["\U0001f1fa\U0001f1e6", ""]
    assert run("return S.pairLabel(S.normState({ cc: 'UA', counterpart: 'RU' }));") == "UA ↔ RU"
    assert (
        run("return S.sparkPath([{ n: 0 }, { n: 2 }, { n: 1 }], 100, 20);")
        == "M0,19.5L50,0.5L100,10"
    )
    assert run("return S.sparkPath([{ n: 3 }], 100, 20);") == ""
    assert (
        run("return S.emptyText(S.normState({ cc: 'UA' }));")
        == "No events for UA in 7D with these filters"
    )
    assert (
        run("return S.emptyText(S.normState({ cc: 'UA', window: 'custom', from: '2026-11-01' }));")
        == "No events for UA in 2026-11-01 → now with these filters"
    )


def test_pinned_until_and_merge_page():
    q = run(
        "return S.eventsQuery(S.normState({ cc: 'UA' }), { now: " + NOW + ", offset: 200, "
        "until: '2026-11-21T12:00:00+00:00' });"
    )
    assert q == (
        "country=UA&all=true&since=2026-11-14T12%3A00%3A00.000Z"
        "&until=2026-11-21T12%3A00%3A00%2B00%3A00&limit=200&offset=200"
    )
    assert run(
        "return S.mergePage([{ id: 'a', v: 1 }, { id: 'b' }], "
        "[{ id: 'b', v: 2 }, { id: 'c' }]).map((e) => e.id);"
    ) == ["a", "b", "c"]
    assert run("return S.mergePage(null, [{ id: 'a' }]).length;") == 1


def test_labels_use_country_names():
    assert (
        run(
            "return S.pairLabel(S.normState({ cc: 'UA', counterpart: 'RU' }), "
            "{ UA: 'Ukraine', RU: 'Russia' });"
        )
        == "Ukraine ↔ Russia"
    )
    assert (
        run("return S.emptyText(S.normState({ cc: 'UA' }), { UA: 'Ukraine' });")
        == "No events for Ukraine in 7D with these filters"
    )


def test_threat_tier_scales_rank_to_the_five_threat_tokens():
    out = run(
        "return [S.threatTier({ rank: 4, scale_max: 4 }), S.threatTier({ rank: 1, scale_max: 5 }),"
        " S.threatTier({ rank: 3, scale_max: 5 }), S.threatTier({ rank: 9, scale_max: 4 }),"
        " S.threatTier({ rank: null, scale_max: 4 }), S.threatTier({ rank: 2, scale_max: 0 }),"
        " S.threatTier(null)];"
    )
    assert out == [4, 1, 2, 4, None, None, None]


def test_summary_names_maps_the_country_and_its_counterparts():
    out = run(
        'return S.summaryNames({ cc: "UA", name: "Ukraine", counterparts: ['
        '{ cc: "RU", name: "Russia" }, { cc: "BY", name: "" }, { cc: "x", name: "X" }] });'
    )
    assert out == {"UA": "Ukraine", "RU": "Russia"}
    assert run("return S.summaryNames(null);") == {}


def test_control_selector_refinds_each_filter_control():
    assert run(
        'return [S.controlSelector({k:"location",v:"inside"}), S.controlSelector({cat:"disaster"}),'
        ' S.controlSelector({date:"from"}), S.controlSelector({cx:"uncp"}), S.controlSelector({cx:"adv"}),'
        ' S.controlSelector(null)];'
    ) == [
        '[data-k="location"][data-v="inside"]',
        '[data-cat="disaster"]',
        '[data-date="from"]',
        '[data-cx="uncp"]',
        None,
        None,
    ]
