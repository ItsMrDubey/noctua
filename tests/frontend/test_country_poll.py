"""Country pane polling and refresh helpers (frontend/js/country-state.js): failure backoff,
due sources, debounce, the refresh merge (server order, window, cap), the focus predicate's
fetch time and the pre-migration note. Pure functions run under node; skipped without node."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MOD = (ROOT / "frontend" / "js" / "country-state.js").as_uri()
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(body: str):
    """Evaluate ``body`` (async JS returning a JSON-able value) with country-state.js as S."""
    src = (
        f'import * as S from "{MOD}";\nconst out = await (async () => {{ {body} }})();\n'
        "console.log(JSON.stringify(out));\n"
    )
    r = subprocess.run([NODE, "--input-type=module"], input=src.encode(), capture_output=True,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_retry_delay_backs_off_and_respects_the_timeout():
    assert run("return [1, 2, 3, 4, 5, 9].map((n) => S.retryDelay(n));") == [
        10000, 20000, 40000, 60000, 60000, 60000]
    # never shorter than the source's request timeout
    assert run("return [1, 2, 3, 4].map((n) => S.retryDelay(n, 20000));") == [
        20000, 20000, 40000, 60000]
    assert run("return [S.retryDelay(0), S.retryDelay(1, 90000)];") == [10000, 90000]


def test_due_sources_skip_requests_in_flight():
    got = run(
        "const s = { summary: { busy: true, next: 0 }, list: { busy: false, next: 5000 },"
        " ids: { busy: false, next: 1000 } };"
        "return [S.dueSources(s, 2000), S.dueSources(s, 6000), S.dueSources(s, 0, true)];"
    )
    assert got == [["ids"], ["list", "ids"], ["list", "ids"]]


def test_debounce_coalesces_rapid_calls():
    got = run(
        "const calls = []; const d = S.debounce((x) => calls.push(x), 40);"
        "d(1); d(2); d(3);"
        "const mid = calls.length;"
        "await new Promise((r) => setTimeout(r, 90));"
        "d(4); d.cancel(); await new Promise((r) => setTimeout(r, 60));"
        "d(5); d.flush();"
        "return [mid, calls, d.pending()];"
    )
    assert got == [0, [3, 5], False]


def test_merge_refresh_resorts_trims_window_and_caps():
    got = run(
        "const old = [{ id: 'a', lifecycle: 'active', updated_at: '2026-11-21T10:00:00Z' },"
        " { id: 'b', lifecycle: 'closed', updated_at: '2026-11-21T11:00:00Z' },"
        " { id: 'gone', lifecycle: 'active', updated_at: '2026-11-10T00:00:00Z' }];"
        "const page = [{ id: 'n', lifecycle: 'cooling', updated_at: '2026-11-21T12:00:00Z' },"
        " { id: 'a', lifecycle: 'developing', updated_at: '2026-11-21T11:30:00Z' },"
        " { id: 'm', lifecycle: 'active', updated_at: '2026-11-21T09:00:00Z' }];"
        "const r = S.mergeRefresh(old, page, { since: '2026-11-14T12:00:00Z' });"
        "const c = S.mergeRefresh(old, page, { max: 2 });"
        "const t = S.mergeRefresh([{ id: 'x', lifecycle: 'active', updated_at: '2026-11-21T10:00:00Z' },"
        " { id: 'y', lifecycle: 'active', updated_at: '2026-11-21T10:00:00Z' }], []);"
        "return [r.items.map((e) => e.id), r.added, r.trimmed, c.items.map((e) => e.id), c.trimmed,"
        " t.items.map((e) => e.id), S.ITEMS_MAX];"
    )
    # lifecycle rank, then updated_at desc, then id desc (the server order); 'gone' left the window
    assert got == [["a", "m", "n", "b"], 2, 0, ["a", "m"], 3, ["y", "x"], 2000]


def test_focus_predicate_keeps_events_newer_than_the_ids_fetch_undimmed():
    got = run(
        "const ids = new Set(['a']);"
        "const evs = [{ id: 'a', updated_at: '2026-11-21T10:00:00Z' },"
        " { id: 'old', updated_at: '2026-11-21T10:00:00Z', country: 'UA' },"
        " { id: 'new', updated_at: '2026-11-21T12:00:01Z', country: 'PL' }, { id: 'nots' }];"
        "const at = '2026-11-21T12:00:00Z';"
        "const ex = S.focusPredicate(ids, { cc: 'UA', fetchedAt: at }),"
        " ms = S.focusPredicate(ids, { cc: 'UA', fetchedAt: Date.parse(at) }),"
        " tr = S.focusPredicate(ids, { cc: 'UA', truncated: true, fetchedAt: at }),"
        " none = S.focusPredicate(ids, { cc: 'UA' });"
        "return [evs.map(ex), evs.map(ms), evs.map(tr), evs.map(none)];"
    )
    assert got == [[True, False, True, False], [True, False, True, False],
                   [True, True, True, True], [True, False, False, False]]


def test_migration_note_and_counterparts_empty_text():
    got = run(
        "return [S.migrationNote({ migrated: false }), S.migrationNote({ migrated: true }),"
        " S.migrationNote({}), S.migrationNote(null), S.counterpartsEmpty({ migrated: false }),"
        " S.counterpartsEmpty({ migrated: true })];"
    )
    note = ("Actor countries not backfilled yet — abroad, involvement and counterparts are "
            "partial (run migrate_countries)")
    assert got == [note, "", "", "", note, "No foreign counterparts in this window"]
