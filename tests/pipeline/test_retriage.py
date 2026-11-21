from datetime import UTC, datetime

import pytest

from osint.config import ROOT
from osint.core.models import Document, PlaceHit, TriageResult
from osint.enrich.gazetteer import Gazetteer
from osint.pipeline.retriage import retriage
from osint.pipeline.triage import Triage
from osint.store.lake import Lake, document_row
from tests.gazetteer_fixture import write_gazetteer

T0 = datetime(2026, 11, 21, 12, 30, tzinfo=UTC)
KYIV = PlaceHit(name="Kyiv", country="UA", lat=50.45, lon=30.52, precision="city",
                confidence=0.9, matched="كييف")


@pytest.fixture(scope="module")
def triage(tmp_path_factory):
    gz = Gazetteer.load(write_gazetteer(tmp_path_factory.mktemp("gaz")))
    return Triage.from_file(ROOT / "config" / "triage.yaml", gz)


def _doc(i, title, lang="", published=T0):
    return Document(id=f"doc:{i}", source="rss:x", url=f"https://e.x/{i}", title=title,
                    snippet="", language=lang, published_at=published, fetched_at=published,
                    kind="article")


def _seed(lake, triage):
    stale = _doc(1, "كيف حال الجميع اليوم", "ar")  # Arabic "how": not a place
    good = _doc(2, "Missile strike hits Kharkiv overnight", "en")
    # stale rows as an older triage produced them: "how" placed at Kyiv, relevance high
    old = TriageResult(language="ar", relevance=0.9, categories=["armed_conflict"],
                       places=[KYIV], best_place=KYIV)
    rows = [document_row(stale, old), document_row(good, triage.assess(good))]
    lake.write("documents", rows[:1])
    lake.write("documents", rows[1:])
    other = _doc(3, "Strike near Kharkiv", "en", datetime(2026, 11, 20, 3, 5, tzinfo=UTC))
    lake.write("documents", [document_row(other, triage.assess(other))])


def _by_id(lake):
    rows = lake.query("SELECT id, place_name, lat, relevance, categories FROM documents")
    return {r[0]: r for r in rows}


def test_retriage_applies_fixes_and_preserves_rows(tmp_path, triage):
    lake = Lake(tmp_path)
    _seed(lake, triage)
    assert _by_id(lake)["doc:1"][1] == "Kyiv"
    stats = retriage(lake, triage)
    assert stats["docs"] == 3
    assert stats["became_unlocated"] == 1 and stats["newly_located"] == 0
    after = _by_id(lake)
    assert len(after) == 3
    assert after["doc:1"][1] is None and after["doc:1"][2] is None
    assert after["doc:2"][1] == "Kharkiv"
    assert stats["relevance_dropped_below"] == 1
    assert stats["partitions_rewritten"] == 1
    assert not list(tmp_path.rglob("*.tmp"))


def test_dry_run_writes_nothing(tmp_path, triage):
    lake = Lake(tmp_path)
    _seed(lake, triage)
    before = sorted(p.name for p in tmp_path.rglob("*.parquet"))
    stats = retriage(lake, triage, dry_run=True)
    assert stats["became_unlocated"] == 1 and stats["partitions_rewritten"] == 0
    assert sorted(p.name for p in tmp_path.rglob("*.parquet")) == before
    assert _by_id(lake)["doc:1"][1] == "Kyiv"


def test_second_run_is_idempotent(tmp_path, triage):
    lake = Lake(tmp_path)
    _seed(lake, triage)
    retriage(lake, triage)
    files = sorted(p.name for p in tmp_path.rglob("*.parquet"))
    stats = retriage(lake, triage)
    assert stats["partitions_rewritten"] == 0 and stats["docs_changed"] == 0
    assert stats["place_changed"] == 0 and stats["became_unlocated"] == 0
    assert sorted(p.name for p in tmp_path.rglob("*.parquet")) == files


def test_days_window_limits_partitions(tmp_path, triage):
    lake = Lake(tmp_path)
    _seed(lake, triage)
    stats = retriage(lake, triage, since=datetime(2026, 11, 21, tzinfo=UTC))
    assert stats["docs"] == 2
