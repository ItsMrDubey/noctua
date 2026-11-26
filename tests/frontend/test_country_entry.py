"""Country context entry points (frontend/js/country-entry.js): the one-time Alt+click hint
state, the lookup gate (a double Alt+click makes one lookup), the /api/country/at query and its
outcome, and the data-newpane pivot rule. The module is pure, so each case runs under node and
returns JSON; skipped without node."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MOD = (ROOT / "frontend" / "js" / "country-entry.js").as_uri()
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# a Storage stand-in; `fail` makes every access throw (blocked site data)
STORE = (
    "const mk = (fail) => { const m = new Map(); return {"
    " getItem: (k) => { if (fail) throw new Error('blocked'); return m.has(k) ? m.get(k) : null; },"
    " setItem: (k, v) => { if (fail) throw new Error('blocked'); m.set(k, String(v)); } }; };\n"
)


def run(body: str):
    """Evaluate ``body`` (JS returning a JSON-able value) with country-entry.js as E."""
    src = (
        f'import * as E from "{MOD}";\n{STORE}const out = (() => {{ {body} }})();\n'
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


def test_hint_shown_once_and_storage_failures_count_as_seen():
    assert run('return E.HINT_KEY;') == "noctua.country.althint"
    assert run(
        "const s = mk(false); const a = E.hintDue(s); const w = E.markHintSeen(s);"
        " return [a, w, E.hintDue(s), s.getItem(E.HINT_KEY)];"
    ) == [True, True, False, "1"]
    # blocked storage: never due (no nagging), marking reports failure without throwing
    assert run("const s = mk(true); return [E.hintDue(s), E.markHintSeen(s)];") == [False, False]
    assert run("return [E.hintDue(null), E.markHintSeen(null)];") == [False, True]


def test_hint_trigger_is_a_plain_primary_click():
    assert run(
        "return [{ button: 0 }, { button: 0, altKey: true }, { button: 2 },"
        " { button: 0, ctrlKey: true }, { button: 0, shiftKey: true }, null].map(E.hintTrigger);"
    ) == [True, False, False, False, False, False]


def test_lookup_gate_debounces_double_alt_click():
    assert run(
        "const g = E.lookupGate(800); const r = [];"
        " r.push(g.begin(1000));"  # first click starts a lookup
        " r.push(g.begin(1100));"  # second click while it is in flight: dropped
        " g.end();"
        " r.push(g.begin(1500));"  # done, but within 800 ms of the last start: dropped
        " r.push(g.begin(1900));"  # later: a new lookup
        " r.push(g.busy); g.end(); r.push(g.busy); return r;"
    ) == [True, False, False, True, True, False]


def test_at_url_formats_and_wraps():
    assert run("return E.atUrl(48.85661, 2.35222);") == "/api/country/at?lat=48.8566&lon=2.3522"
    assert run("return E.atUrl(95, 190);") == "/api/country/at?lat=90.0000&lon=-170.0000"
    assert run("return [E.atUrl(NaN, 0), E.atUrl(0, Infinity)];") == [None, None]


def test_at_outcome():
    assert run('return E.atOutcome(200, { cc: "fr", name: "France" });') == {
        "cc": "FR",
        "name": "France",
    }
    assert run("return E.atOutcome(404, null);") == "ocean"
    assert run(
        'return [E.atOutcome(422, null), E.atOutcome(0, null), E.atOutcome(500, null),'
        ' E.atOutcome(200, {}), E.atOutcome(200, { cc: "FRA" })];'
    ) == ["fail"] * 5


def test_wants_new_pane():
    assert run(
        'return [[{}, { newpane: "1" }], [{}, {}], [{ shiftKey: true }, {}],'
        ' [{ ctrlKey: true }, {}], [{}, { newpane: "0" }], [null, null]]'
        ".map(([e, d]) => E.wantsNewPane(e, d));"
    ) == [True, False, True, True, False, False]
