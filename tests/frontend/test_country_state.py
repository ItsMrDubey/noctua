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


def test_merge_top():
    # a refresh's newest page: new ids on top in page order, shown rows replaced in place, older kept
    out = run(
        "const r = S.mergeTop([{ id: 'a', lc: 'active' }, { id: 'b' }, { id: 'old' }], "
        "[{ id: 'n1' }, { id: 'a', lc: 'cooling' }, { id: 'n2' }, { id: 'n1', dup: 1 }]);"
        "return { ids: r.items.map((e) => e.id), added: r.added, a: r.items[2].lc, n1: r.items[0].dup || 0 };"
    )
    assert out == {"ids": ["n1", "n2", "a", "b", "old"], "added": 2, "a": "cooling", "n1": 0}
    assert run("const r = S.mergeTop(null, null); return [r.items.length, r.added];") == [0, 0]
    same = run("const r = S.mergeTop([{ id: 'a' }], [{ id: 'a', v: 2 }]); return [r.added, r.items[0].v];")
    assert same == [0, 2]


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
        ' S.controlSelector({cp:"RU"}),'
        ' S.controlSelector(null)];'
    ) == [
        '[data-k="location"][data-v="inside"]',
        '[data-cat="disaster"]',
        '[data-date="from"]',
        '[data-cx="uncp"]',
        None,
        '[data-cp="RU"]',
        None,
    ]


def test_fit_box_pads_and_handles_the_antimeridian():
    r = run(
        "return [S.fitBox([22.1, 44.4, 40.2, 52.4]), S.fitBox([19.6, 41.2, -169.73, 81.85]),"
        " S.fitBox([177, -18.3, -178.2, -16]), S.fitBox([-179.8, 10, -170, 20]),"
        " S.fitBox([0, 0, 359, 1]), S.fitBox([7.41, 43.72, 7.44, 43.75]),"
        " S.fitBox(null), S.fitBox([1, 2, 3]), S.fitBox([0, 10, 1, 5])];"
    )
    ua, ru, fj, near, world, mc, *bad = r
    # UA: 8 % padding each side, no wrap
    assert ua == pytest.approx([22.1 - 1.448, 44.4 - 0.64, 40.2 + 1.448, 52.4 + 0.64])
    # RU crosses the antimeridian: w > e is kept (Cesium.Rectangle spans east across 180)
    span = -169.73 - 19.6 + 360
    assert ru == pytest.approx([19.6 - span * 0.08, 41.2 - 3.252, -169.73 + span * 0.08, 85.102])
    assert ru[0] > ru[2]
    # FJ: a small box across 180, min 0.5 deg padding
    assert fj == pytest.approx([176.5, -18.8, -177.7, -15.5])
    # a box that the padding pushes across the antimeridian wraps instead of clamping
    assert near[0] == pytest.approx(179.416) and near[2] == pytest.approx(-169.216)
    # padded span >= 360: the whole longitude range
    assert world == pytest.approx([-180, -0.5, 180, 1.5])
    assert mc == pytest.approx([6.91, 43.22, 7.94, 44.25])
    assert bad == [None, None, None]


def test_focus_predicate_exact_and_truncated():
    r = run(
        'const ids = new Set(["a"]);'
        'const evs = [{id: "a", country: "RU"}, {id: "b", country: "UA"},'
        ' {id: "c", country: "PL", actor_countries: ["UA"]},'
        " {id: 'd', country: 'PL', actor_countries: '[\"UA\"]'},"
        ' {id: "e", country: "PL", actor_countries: ["DE"]}, {id: "f", country: null}, {id: "g"}];'
        "const ex = S.focusPredicate(ids, { cc: 'UA' }),"
        " tr = S.focusPredicate(ids, { truncated: true, cc: 'UA' });"
        "return [evs.map(ex), evs.map(tr), S.focusPredicate(null)];"
    )
    exact, trunc, none = r
    assert exact == [True, False, False, False, False, False, False]
    # capped set: anything that could belong to UA's filtered set is unknown, so not dimmed
    assert trunc == [True, True, True, True, False, False, True]
    assert none is None


def test_outline_runs_close_rings_and_drop_the_antimeridian_seam():
    r = run(
        "return [S.outlineRuns([0, 0, 10, 0, 10, 10, 0, 10]),"
        " S.outlineRuns([170, 0, 180, 0, 180, 10, 170, 10, 170, 0]),"
        " S.outlineRuns([-180, 0, -170, 0, -170, 10, -180, 10, -180, 0]),"
        " S.outlineRuns([0, 0, 1, 1]), S.outlineRuns(null)];"
    )
    square, east, west, short, none = r
    assert square == [[0, 0, 10, 0, 10, 10, 0, 10, 0, 0]]
    assert east == [[180, 10, 170, 10, 170, 0, 180, 0]]
    assert west == [[-180, 0, -170, 0, -170, 10, -180, 10]]
    assert short == [] and none == []


def test_globe_action_follows_the_dossier_not_esc():
    r = run(
        'const ua = {type: "country", cc: "ua"}, ru = {type: "country", cc: "RU"},'
        ' intel = {type: "intel", kind: "event", id: "x"};'
        "return ["
        ' S.globeAction({state: "open", focused: ua, specs: [ua]}, null),'
        ' S.globeAction({state: "open", focused: ru, specs: [ua, ru]}, "UA"),'
        ' S.globeAction({state: "open", focused: intel, specs: [ua, intel]}, "UA"),'
        ' S.globeAction({state: "open", focused: ua, specs: [ua]}, "UA"),'
        ' S.globeAction({state: "min", focused: ua, specs: [ua]}, "UA"),'
        ' S.globeAction({state: "closed", focused: null, specs: []}, "UA"),'
        ' S.globeAction({state: "min", focused: ua, specs: [ua]}, null),'
        ' S.globeAction({state: "open", focused: intel, specs: [intel]}, "UA"),'
        ' S.globeAction({state: "open", focused: null, specs: []}, null),'
        " S.globeAction(null, null),"
        ' S.globeAction({state: "open", focused: intel, specs: [ua, intel]}, null, "UA"),'
        ' S.globeAction({state: "open", focused: intel, specs: [intel]}, null, "UA"),'
        ' S.globeAction({state: "open", focused: ru, specs: [ua, ru]}, null, "UA")];'
    )
    assert r == [
        {"op": "activate", "cc": "UA"},
        {"op": "activate", "cc": "RU"},
        {"op": "keep"},  # intel pane focused beside the country pane: context stays
        {"op": "activate", "cc": "UA"},  # inner-layer Esc re-announces the same state: no reset
        {"op": "deactivate"},  # Esc / B minimised the dossier
        {"op": "deactivate"},
        {"op": "keep"},
        {"op": "deactivate"},  # the country pane was closed
        {"op": "keep"},
        {"op": "keep"},
        {"op": "activate", "cc": "UA"},  # restored with the intel pane focused: context comes back
        {"op": "keep"},  # its pane was closed meanwhile
        {"op": "activate", "cc": "RU"},  # a focused country pane wins
    ]
