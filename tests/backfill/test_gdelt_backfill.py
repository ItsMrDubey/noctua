import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import respx

from osint.backfill import gdelt
from osint.backfill.cluster import CURSOR, run_cluster
from osint.backfill.common import Window
from osint.collectors.gdelt import COL, N_COLUMNS
from osint.core.geo import utcnow

DAY = (utcnow() - timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)


def row(event_id: str, ts: datetime, url: str, **over: str) -> str:
    cols = [""] * N_COLUMNS
    values = {
        "GlobalEventID": event_id, "SQLDATE": ts.strftime("%Y%m%d"), "Actor1Name": "RUSSIA",
        "Actor2Name": "UKRAINE", "IsRootEvent": "1", "EventCode": "190",
        "EventBaseCode": "190", "EventRootCode": "19", "QuadClass": "4",
        "NumMentions": "12", "NumSources": "3", "NumArticles": "10", "ActionGeo_Type": "4",
        "ActionGeo_FullName": "Kharkiv, Kharkivs'ka Oblast', Ukraine",
        "ActionGeo_CountryCode": "UP", "ActionGeo_Lat": "49.9808",
        "ActionGeo_Long": "36.2527", "DATEADDED": ts.strftime("%Y%m%d%H%M%S"),
        "SOURCEURL": url,
    }
    values.update(over)
    for name, value in values.items():
        cols[COL[name]] = value
    return "\t".join(cols)


def make_zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("x.export.CSV", "\n".join(lines) + "\n")
    return buf.getvalue()


def test_export_times_and_urls():
    w = Window(datetime(2026, 11, 20, 0, 5, tzinfo=UTC), datetime(2026, 11, 20, 1, 0, tzinfo=UTC))
    times = gdelt.export_times(w, now=datetime(2026, 11, 21, tzinfo=UTC))
    assert [t.strftime("%H%M") for t in times] == ["0015", "0030", "0045"]
    full = gdelt.export_times(Window(datetime(2026, 11, 20, tzinfo=UTC),
                                     datetime(2026, 11, 21, tzinfo=UTC)),
                              now=datetime(2026, 11, 22, tzinfo=UTC))
    assert len(full) == 96 and full[0].strftime("%H%M") == "0000"
    # files not yet published are not requested
    recent = gdelt.export_times(Window(datetime(2026, 11, 20, tzinfo=UTC),
                                       datetime(2026, 11, 21, tzinfo=UTC)),
                                now=datetime(2026, 11, 20, 1, 0, tzinfo=UTC))
    assert [t.strftime("%H%M") for t in recent] == ["0000", "0015", "0030"]
    assert gdelt.export_url(datetime(2026, 11, 20, 0, 15, tzinfo=UTC)) == \
        "https://data.gdeltproject.org/gdeltv2/20261120001500.export.CSV.zip"


def _mock_day(router: respx.Router, t0: datetime) -> None:
    t1, t2 = t0 + timedelta(minutes=15), t0 + timedelta(minutes=30)
    router.get(gdelt.export_url(t0)).mock(return_value=httpx.Response(200, content=make_zip([
        row("1", t0, "https://a.example/one"), row("2", t0, "https://b.example/two")])))
    router.get(gdelt.export_url(t1)).mock(return_value=httpx.Response(404))
    router.get(gdelt.export_url(t2)).mock(return_value=httpx.Response(200, content=make_zip([
        row("1", t0, "https://a.example/one"),  # duplicate id
        row("3", t2, "https://c.example/three")])))


async def test_gdelt_backfill_writes_dedupes_and_resumes(make_ctx):
    w = Window(DAY, DAY + timedelta(minutes=45))
    with respx.mock(assert_all_called=False) as router:
        _mock_day(router, DAY)
        ctx = make_ctx("gdelt", {"min_interval_s": 0})
        s = await gdelt.run(ctx, w)
        assert s.rows == {"documents": 3}
        assert s.extra["files_missing"] == 1 and s.units_done == 1
        n = ctx.lake.query("SELECT count(*), count(DISTINCT id) FROM documents")[0]
        assert n == (3, 3)
        cats = ctx.lake.query("SELECT DISTINCT categories, lat IS NOT NULL FROM documents")
        assert all(c[1] for c in cats)
        # resume: the day is checkpointed, nothing is fetched again
        calls = router.calls.call_count
        ctx2 = make_ctx("gdelt", {"min_interval_s": 0})
        s2 = await gdelt.run(ctx2, w)
        assert s2.units_skipped == 1 and router.calls.call_count == calls
        # a forced re-run (checkpoint cleared) still writes no duplicate ids
        ctx2.store.set_cursor("backfill:gdelt", {})
        ctx3 = make_ctx("gdelt", {"min_interval_s": 0})
        s3 = await gdelt.run(ctx3, w)
        assert s3.rows == {}
        assert ctx3.lake.query("SELECT count(*) FROM documents")[0][0] == 3


async def test_gdelt_dry_run_writes_nothing(make_ctx):
    w = Window(DAY, DAY + timedelta(minutes=45))
    with respx.mock(assert_all_called=False) as router:
        _mock_day(router, DAY)
        ctx = make_ctx("gdelt", {"min_interval_s": 0}, dry_run=True)
        s = await gdelt.run(ctx, w)
    assert s.rows == {"documents": 3}
    assert ctx.lake.query("SELECT count(*) FROM documents")[0][0] == 0
    assert ctx.store.get_cursor("backfill:gdelt") is None


async def test_cluster_replays_late_documents_into_events(make_ctx):
    w = Window(DAY, DAY + timedelta(minutes=45))
    with respx.mock(assert_all_called=False) as router:
        _mock_day(router, DAY)
        ctx = make_ctx("gdelt", {"min_interval_s": 0})
        await gdelt.run(ctx, w)
    s = run_cluster(ctx.settings, ctx.store, ctx.lake, w)
    assert s.rows["documents_read"] == 3
    # every row is adjudicated; only accepted decisions are persisted (persist_rejected
    # off), so GDELT rows without article backing leave no rows behind
    accepted = s.extra["gdelt_accepted"]
    assert accepted + sum(s.extra["gdelt_rejected"].values()) == 3
    assert len(ctx.store.event_doc_ids_since(DAY - timedelta(days=1))) == accepted
    assert all(e["t_end"] < (utcnow() - timedelta(hours=72)).isoformat()
               for e in ctx.store.list_events(limit=10))
    assert ctx.store.get_cursor(CURSOR)["state"] == "finished"
    # resumable: a second run replays nothing
    s2 = run_cluster(ctx.settings, ctx.store, ctx.lake, w)
    assert s2.units_done == 0 and s2.units_skipped >= 1


async def test_cluster_leaves_recent_documents_to_live_builder(make_ctx):
    recent = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=5)
    ctx = make_ctx("gdelt")
    s = run_cluster(ctx.settings, ctx.store, ctx.lake, Window(recent, utcnow()))
    assert s.units_total == 0 and "horizon" in (s.message or "")
