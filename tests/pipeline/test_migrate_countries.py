import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from osint.enrich.actors import ActorLexicon
from osint.enrich.rules import RuleSet
from osint.pipeline.events import REBUILD_STATE, rebuild_lock_path
from osint.pipeline.migrate_countries import main, migrate, normalise_country
from osint.pipeline.rule_pass import RulePass
from osint.store.lake import FileLock
from osint.store.sqlite import COUNTRY_MIGRATION_STATE, SqliteStore, iso

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _ev(eid, country, **kw):
    t = NOW - timedelta(hours=1)
    base = {"id": eid, "category": "armed_conflict", "title": "t", "summary": "",
            "t_start": iso(t), "t_end": iso(t), "status": "reported", "confidence": 0.5,
            "version": 1, "mention_count": 1, "source_diversity": 1, "first_seen": iso(t),
            "updated_at": iso(t), "country": country, "actors": []}
    return {**base, **kw}


@pytest.mark.parametrize(("eid", "origin", "value", "out"), [
    ("01N", "news", "UA", ("UA", "kept")), ("01N", "news", "ua", ("UA", "code")),
    ("01N", "news", "UP", ("UA", "fips")), ("01N", "news", "GM", ("GM", "kept")),
    ("01N", "news", "UKR", ("UA", "code")), ("01N", "news", "Ukraine", ("UA", "name")),
    ("01N", "news", None, (None, "empty")), ("01N", "news", "QQ", (None, "unresolved")),
    ("acled-1", "historical", "Sudan", ("SD", "name")),
    ("ucdp-1", "historical", "Russia (Soviet Union)", ("RU", "name")),
    ("ucdp-1", "historical", "RU", ("RU", "kept"))])
def test_normalise_country_by_source(eid, origin, value, out):
    assert normalise_country(eid, origin, value) == out


def _fill(s):
    s.save_events(
        [_ev("01NEWSISO", "UA"),
         _ev("01NEWSFIPS", "UP", actors=[{"name": "Russia", "role": "source", "type": "RUS",
                                          "country": "RUS"}]),
         _ev("acled-SDN1", "Sudan", origin="historical", status="historical", actors=[
             {"name": "Rapid Support Forces", "role": "actor1", "type": "2", "country": ""},
             {"name": "Military Forces of Sudan (2019-)", "role": "actor2", "type": "1",
              "country": ""}]),
         _ev("ucdp-9", "Russia (Soviet Union)", origin="historical", status="historical",
             actors=[{"name": "Government of Russia (Soviet Union)", "role": "side_a",
                      "type": "", "country": ""},
                     {"name": "Government of Ukraine", "role": "side_b", "type": "",
                      "country": ""}]),
         _ev("01NEWSNULL", None)],
        documents=[{"event_id": "01NEWSISO", "doc_id": "d1", "source": "rss:t",
                    "url": "https://a.example/1",
                    "title": "Russian shelling of Kharkiv kills 3 civilians",
                    "published_at": iso(NOW - timedelta(hours=1)), "language": "en",
                    "role": "primary", "added_at": iso(NOW)}])


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "osint.sqlite")
    _fill(s)
    yield s
    s.close()


@pytest.fixture(scope="module")
def lexicon():
    return ActorLexicon.load()


def test_migration_normalises_all_four_formats(store, lexicon):
    rp = RulePass(store, lexicon=lexicon, rules=RuleSet.load())
    dry = migrate(store, rp, now=NOW, dry_run=True)
    assert dry["dry_run"] is True and dry["rewritten"] == {"fips": 1, "name": 2, "code": 0}
    assert dry["after"]["by_format"]["name"] == 0 and dry["before"]["by_format"]["name"] == 2
    assert store.get_event("acled-SDN1")["country"] == "Sudan"  # nothing written
    assert store.get_cursor(COUNTRY_MIGRATION_STATE) is None

    rep = migrate(store, rp, now=NOW)
    ev = store.get_event
    assert [ev(e)["country"] for e in ("01NEWSISO", "01NEWSFIPS", "acled-SDN1", "ucdp-9")] \
        == ["UA", "UA", "SD", "RU"]
    a = ev("01NEWSFIPS")["actors"][0]
    assert (a["country"], a["cameo"], a["source"]) == ("RU", "RUS", "gdelt")
    assert ev("01NEWSFIPS")["foreign_involved"] == 1
    u = ev("ucdp-9")
    assert u["actor_countries"] == ["RU", "UA"] and u["foreign_involved"] == 1
    assert ev("acled-SDN1")["actor_countries"] == ["SD"] and ev("acled-SDN1")["attributed"] == 1
    assert ev("01NEWSISO")["actor_countries"] == ["RU"]
    assert all(ev(e)["lifecycle"] == "active" for e in ("01NEWSISO", "01NEWSFIPS", "ucdp-9"))
    got = {(r["field"], r["value"]) for r in store.rule_fields(["01NEWSISO"])["01NEWSISO"]}
    assert ("casualties.killed", "3") in got
    assert rep["rule_field_coverage"]["casualties.killed"] == 1
    assert dry["rule_field_coverage"] == rep["rule_field_coverage"]
    assert store.rule_fields(["ucdp-9"]) == {}
    assert rep["attributed_share"] == 0.8 and rep["events"] == 5
    assert dry["lifecycle"] == rep["lifecycle"]  # the dry run previews what is written
    assert store.get_cursor(COUNTRY_MIGRATION_STATE)["done"] is True
    assert store.get_cursor(REBUILD_STATE)["migrated"] is True
    with FileLock(rebuild_lock_path(store.path), timeout=0):
        pass  # the rebuild lock was released

    again = migrate(store, rp, now=NOW)
    assert again["rewritten"] == {"fips": 0, "name": 0, "code": 0}
    assert again["kept"] == 4 and again["empty"] == 1


def test_migration_is_idempotent(store, lexicon):
    rp = RulePass(store, lexicon=lexicon, rules=RuleSet.load())
    migrate(store, rp, now=NOW)
    ids = ("01NEWSISO", "01NEWSFIPS", "acled-SDN1", "ucdp-9", "01NEWSNULL")
    first = {e: store.get_event(e) for e in ids}
    fields = store.rule_fields(list(ids))
    migrate(store, rp, now=NOW)
    assert {e: store.get_event(e) for e in ids} == first
    assert store.rule_fields(list(ids)) == fields


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    db = tmp_path / "osint.sqlite"
    s = SqliteStore(db)
    _fill(s)
    s.close()
    before = _digest(db)
    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp_path))
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out and '"name": 2' in out
    assert _digest(db) == before
    assert not rebuild_lock_path(db).exists()  # the rebuild lock was never taken
    assert not (tmp_path / "lake").exists() and not (tmp_path / "extraction.sqlite").exists()


def test_cli_dry_run_on_a_pre_migration_schema(tmp_path, monkeypatch, capsys):
    """The live DB may predate the derived columns / event_rule_fields: a dry run reads
    it as is and never adds them."""
    db = tmp_path / "osint.sqlite"
    s = SqliteStore(db)
    _fill(s)
    s.close()
    con = sqlite3.connect(db)
    for col in ("actor_countries", "foreign_involved", "attributed", "lifecycle"):
        con.execute("DROP INDEX IF EXISTS events_open")
        try:
            con.execute(f"ALTER TABLE events DROP COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    con.execute("DROP TABLE event_rule_fields")
    con.commit()
    con.close()
    cols = _columns(db)
    assert "lifecycle" not in cols["events"] and not cols["event_rule_fields"]
    before = _digest(db)
    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp_path))
    assert main(["--dry-run"]) == 0
    assert '"rewritten": {\n      "fips": 1,\n      "name": 2' in capsys.readouterr().out
    assert _digest(db) == before and _columns(db) == cols


def _columns(db):
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
                for t in ("events", "event_fields", "event_rule_fields")}
    finally:
        con.close()


def test_unresolved_country_is_kept_and_reported(store, lexicon):
    store.save_events([_ev("01NEWSODD", "Atlantis")])
    rp = RulePass(store, lexicon=lexicon, rules=RuleSet.load())
    rep = migrate(store, rp, now=NOW)
    assert store.get_event("01NEWSODD")["country"] == "Atlantis"  # raw value not erased
    assert rep["unresolved"] == {"Atlantis": 1}


def test_rebuild_marker_keeps_a_pending_cleanup_flag(store, lexicon):
    store.set_cursor(REBUILD_STATE, {"at": "2026-01-01T00:00:00+00:00", "migrated": False})
    rp = RulePass(store, lexicon=lexicon, rules=RuleSet.load())
    migrate(store, rp, now=NOW)
    marker = store.get_cursor(REBUILD_STATE)
    assert marker["migrated"] is False and marker["at"] != "2026-01-01T00:00:00+00:00"


def test_rerun_keeps_the_first_completion_values(store, lexicon):
    rp = RulePass(store, lexicon=lexicon, rules=RuleSet.load())
    migrate(store, rp, now=NOW)
    first = store.get_cursor(COUNTRY_MIGRATION_STATE)
    assert first["rewritten"] == 3
    migrate(store, rp, now=NOW + timedelta(hours=2))
    again = store.get_cursor(COUNTRY_MIGRATION_STATE)
    assert (again["at"], again["rewritten"]) == (first["at"], 3)
    assert again["last_at"] == iso(NOW + timedelta(hours=2)) and again["last_rewritten"] == 0


def test_cli_dry_run_leaves_no_side_files(tmp_path, monkeypatch, capsys):
    from osint.pipeline.extract.store import ExtractionStore
    db = tmp_path / "osint.sqlite"
    s = SqliteStore(db)
    _fill(s)
    s.close()
    ExtractionStore(tmp_path / "extraction.sqlite").close()
    for f in tmp_path.glob("*-wal"):
        f.unlink()
    for f in tmp_path.glob("*-shm"):
        f.unlink()
    before = sorted(p.name for p in tmp_path.iterdir())
    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp_path))
    assert main(["--dry-run"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert "Stop the server" not in capsys.readouterr().out  # real-run notice only
