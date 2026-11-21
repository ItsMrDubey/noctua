import csv
import gzip
import io
import json
import tarfile
from datetime import UTC, date, datetime

import httpx
import respx

from osint.backfill import adsb_history, ucdp
from osint.backfill.common import Window
from osint.enrich.military import MilDb

GED_FIELDS = ["id", "relid", "type_of_violence", "conflict_name", "dyad_name", "side_a",
              "side_b", "number_of_sources", "source_office", "source_headline",
              "where_prec", "where_coordinates", "where_description", "adm_1", "latitude",
              "longitude", "country", "event_clarity", "date_prec", "date_start", "date_end",
              "deaths_a", "deaths_b", "deaths_civilians", "deaths_unknown", "best", "high",
              "low"]


def ged_row(**over: str) -> dict[str, str]:
    r = {"id": "644785", "relid": "AFG-2026-3-951-40", "type_of_violence": "3",
         "conflict_name": "Government of Afghanistan - Civilians",
         "dyad_name": "Government of Afghanistan - Civilians",
         "side_a": "Government of Afghanistan", "side_b": "Civilians",
         "number_of_sources": "2", "source_office": "Hasht e Subh Daily;Kabul Now",
         "source_headline": "A Taliban militant killed a shopkeeper in Laghman",
         "where_prec": "1", "where_coordinates": "Mihtarlam town",
         "where_description": "Omerzai area of Mehtarlam", "adm_1": "Laghman province",
         "latitude": "34.663769", "longitude": "70.207390", "country": "Afghanistan",
         "event_clarity": "1", "date_prec": "1", "date_start": "2026-10-06 00:00:00.000",
         "date_end": "2026-10-07 00:00:00.000", "deaths_a": "0", "deaths_b": "0",
         "deaths_civilians": "1", "deaths_unknown": "0", "best": "1", "high": "2", "low": "1"}
    r.update(over)
    return r


NOW = datetime(2026, 11, 21, tzinfo=UTC)


def test_ucdp_row_mapping_precision_and_time():
    m = ucdp.map_row(ged_row(), "GEDEvent_v26_0_10", NOW)
    ev = m["event"]
    assert ev["id"] == "ucdp-644785" and ev["category"] == "armed_conflict"
    assert ev["subcategory"] == "one-sided" and ev["status"] == "historical"
    assert ev["precision"] == "point" and ev["geo_confidence"] == 1.0
    assert ev["confidence"] == 1.0
    assert ev["t_start"].startswith("2026-10-06T00:00:00") and ev["t_end"].startswith("2026-10-07")
    assert ev["updated_at"] == ev["t_end"]  # history does not look "recently updated"
    assert ev["h3_3"] and ev["lat"] == 34.663769
    assert ev["source_diversity"] == 2 and "1 killed" in ev["title"]
    fields = {f["field"]: f["value"] for f in m["fields"]}
    assert fields["fatalities_best"] == "1" and fields["fatalities_high"] == "2"
    assert fields["source"] == "ucdp" and "CC BY 4.0" in fields["licence"]
    assert m["document"]["source"] == "ucdp"

    m2 = ucdp.map_row(ged_row(where_prec="6", date_prec="4", type_of_violence="1"), "g", NOW)
    assert m2["event"]["precision"] == "country" and m2["event"]["subcategory"] == "state-based"
    assert m2["event"]["confidence"] == round(0.6 * 0.2 + 0.4 * 0.5, 4)
    assert ucdp.map_row(ged_row(type_of_violence="2", where_prec="4"), "g", NOW)["event"][
        "precision"] == "admin1"
    assert ucdp.map_row(ged_row(latitude=""), "g", NOW) is None


def test_ucdp_discover_downloads():
    html = ('<a href="https://ucdp.uu.se/downloads/ged/ged251-csv.zip">'
            '<a href="https://ucdp.uu.se/downloads/ged/ged261-csv.zip">'
            '<a href="https://ucdp.uu.se/downloads/ged/ged261-xlsx.zip">'
            '<a href="https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_0_10.csv">')
    assert ucdp.discover(html) == [
        "https://ucdp.uu.se/downloads/ged/ged261-csv.zip",
        "https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_0_10.csv"]
    assert ucdp.dataset_name("https://x/ged261-csv.zip") == "ged261"


def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=GED_FIELDS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


async def test_ucdp_run_imports_window_into_event_store(make_ctx):
    zbuf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("GEDEvent_v26_1.csv", _csv_bytes([
            ged_row(id="1"), ged_row(id="2", date_start="2010-01-01", date_end="2010-01-01"),
            ged_row(id="3", latitude="nan")]))
    urls = ["https://ucdp.example/ged/ged261-csv.zip",
            "https://ucdp.example/candidateged/GEDEvent_v26_0_10.csv"]
    with respx.mock:
        respx.get(urls[0]).mock(return_value=httpx.Response(200, content=zbuf.getvalue()))
        respx.get(urls[1]).mock(return_value=httpx.Response(
            200, content=_csv_bytes([ged_row(id="1"), ged_row(id="4")])))
        ctx = make_ctx("ucdp", {"urls": urls})
        s = await ucdp.run(ctx, Window(datetime(2015, 1, 1, tzinfo=UTC), NOW))
    assert s.units_done == 2 and s.rows == {"events": 3}  # id 1 twice (upsert)
    ev = ctx.store.get_event("ucdp-1")
    assert ev["status"] == "historical" and ev["documents"][0]["source"] == "ucdp"
    assert ctx.store.get_event("ucdp-2") is None  # before --since
    assert len(ctx.store.list_events(limit=100)) == 2
    assert not list((ctx.tmp_dir / "ucdp").glob("*"))  # downloads deleted


# ------------------------------------------------------------------- adsb history


def trace(hex_code: str, points: list[list], **header: object) -> dict:
    return {"icao": hex_code, "timestamp": 1795132800.0, "trace": points, **header}


def pts(offsets: list[float]) -> list[list]:
    return [[o, 30.0 + o / 1e4, -86.0, 1300 if o else "ground", 120.0, 90.0, 0, None,
             {"flight": "RCH123 "} if o == 0 else None, "adsb_icao"] for o in offsets]


def test_parse_trace_military_filter_and_thinning():
    db = MilDb({"43c6f1": {"reg": "ZZ330", "operator": "RAF", "icao_type": "A332",
                           "category": "Tanker", "tag1": "Voyager", "type": ""}})
    res = adsb_history.parse_trace(trace("ae73e9", pts([0, 10, 59, 61, 125, 130]),
                                         dbFlags=1, t="A119", r="170190"), db)
    assert res is not None and res.track.is_mil and res.track.reg == "170190"
    assert [p.ts.timestamp() - 1795132800.0 for p in res.positions] == [0, 61, 125]
    assert res.positions[0].alt_ft == 0 and res.positions[1].alt_ft == 1300
    assert res.track.name == "RCH123" and res.positions[0].source.startswith("adsb_history")
    # plane-alert list and military type codes also qualify; civil traffic does not
    assert adsb_history.parse_trace(trace("43c6f1", pts([0])), db).track.category == "Tanker"
    assert adsb_history.parse_trace(trace("aaaaaa", pts([0]), t="F16"), db) is not None
    assert adsb_history.parse_trace(trace("a092e9", pts([0]), dbFlags=4, t="B738"), db) is None
    assert adsb_history.parse_trace(trace("~abcdef", pts([0]), dbFlags=1), db) is None


def _tar_parts(tmp_path, members: dict[str, bytes], parts: int = 2) -> list:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    cut = len(raw) // parts
    paths = []
    for i in range(parts):
        p = tmp_path / f"day.tar.a{chr(97 + i)}"
        p.write_bytes(raw[i * cut:] if i == parts - 1 else raw[i * cut:(i + 1) * cut])
        paths.append(p)
    return paths


def test_process_archive_reads_split_tar(make_ctx, tmp_path):
    gz = lambda d: gzip.compress(json.dumps(d).encode())
    parts = _tar_parts(tmp_path, {
        "./traces/e9/trace_full_ae73e9.json": gz(trace("ae73e9", pts([0, 30, 60, 3600]),
                                                       dbFlags=1)),
        "./traces/e9/trace_full_a092e9.json": gz(trace("a092e9", pts([0, 60]), dbFlags=0)),
        "./traces/f1/trace_full_aaaaaa.json": b"\x1f\x8bnot gzip",
        "./heatmap/00.bin.ttf": b"ignored",
    })
    ctx = make_ctx("adsb_history")
    from osint.backfill.common import Summary
    s = Summary(source="adsb_history")
    stats = adsb_history.process_archive(ctx, parts, MilDb({}), 60.0, s)
    assert stats == {"traces": 3, "military": 1, "bad": 1, "positions": 3}
    assert ctx.lake.query("SELECT count(*), min(track_id) FROM positions")[0] == (3, "air:ae73e9")
    t = ctx.store.get_track("air:ae73e9")
    assert t["is_mil"] == 1 and t["last_seen"].startswith("2026-11-20T01:00")


def test_release_picking_and_dates():
    rels = [{"tag_name": "v2026.11.20-planes-readsb-staging-0", "assets": [{"name": "x.tar"}]},
            {"tag_name": "v2026.11.20-planes-readsb-prod-0",
             "assets": [{"name": "v.tar.ab", "size": 2}, {"name": "v.tar.aa", "size": 1},
                        {"name": "notes.txt"}]},
            {"tag_name": "v2026.11.20-planes-readsb-prod-1", "assets": []}]
    rel = adsb_history.pick_release(rels, date(2026, 11, 20), "prod")
    assert rel["tag_name"].endswith("prod-0")
    assert [a["name"] for a in adsb_history.tar_assets(rel)] == ["v.tar.aa", "v.tar.ab"]
    assert adsb_history.pick_release(rels, date(2026, 11, 19), "prod") is None
    w = Window(datetime(2026, 11, 19, 21, tzinfo=UTC), datetime(2026, 11, 21, 21, tzinfo=UTC))
    assert adsb_history.history_dates(w) == [date(2026, 11, 19), date(2026, 11, 20)]


async def test_adsb_history_skips_when_disk_is_short(make_ctx, monkeypatch):
    rels = [{"tag_name": "v2026.11.20-planes-readsb-prod-0", "assets": [
        {"name": "v.tar.aa", "size": 2_000_000_000, "browser_download_url": "https://x/a"}]}]
    monkeypatch.setattr(adsb_history.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 3_000_000_000})())
    with respx.mock:
        respx.get(url__startswith="https://api.github.com/repos/adsblol/globe_history_2026"
                  ).mock(return_value=httpx.Response(200, json=rels))
        ctx = make_ctx("adsb_history")
        s = await adsb_history.run(ctx, Window(datetime(2026, 11, 20, tzinfo=UTC),
                                               datetime(2026, 11, 21, tzinfo=UTC)))
    assert s.units_done == 0 and "skipped" in s.message and "GB free" in s.message
