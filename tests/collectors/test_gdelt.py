import hashlib
import io
import logging
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.gdelt import (
    COL,
    LASTUPDATE_URL,
    N_COLUMNS,
    GdeltCollector,
    export_slots,
    latest_export_url,
    parse_export,
    parse_row,
)
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

FETCHED = datetime(2026, 11, 21, 14, 31, tzinfo=UTC)
EXPORT = "http://data.gdeltproject.org/gdeltv2/20261121143000.export.CSV.zip"


def row(**over: str) -> str:
    cols = [""] * N_COLUMNS
    values = {
        "GlobalEventID": "1334365828", "SQLDATE": "20261121", "Actor1Code": "RUS",
        "Actor1Name": "RUSSIA", "Actor1CountryCode": "RUS", "Actor2Code": "UKR",
        "Actor2Name": "UKRAINE", "Actor2CountryCode": "UKR", "IsRootEvent": "1",
        "EventCode": "190", "EventBaseCode": "190", "EventRootCode": "19", "QuadClass": "4",
        "GoldsteinScale": "-10.0", "NumMentions": "12", "NumSources": "3",
        "NumArticles": "10", "AvgTone": "-6.5", "ActionGeo_Type": "4",
        "ActionGeo_FullName": "Kharkiv, Kharkivs'ka Oblast', Ukraine",
        "ActionGeo_CountryCode": "UP", "ActionGeo_ADM1Code": "UP07",
        "ActionGeo_Lat": "49.9808", "ActionGeo_Long": "36.2527",
        "ActionGeo_FeatureID": "-1044367", "DATEADDED": "20261121143000",
        "SOURCEURL": "https://news.example/kharkiv-strike",
    }
    values.update(over)
    for name, value in values.items():
        cols[COL[name]] = value
    return "\t".join(cols)


def make_zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("20261121143000.export.CSV", "\n".join(lines) + "\n")
    return buf.getvalue()


def lastupdate(data: bytes, url: str = EXPORT) -> str:
    md5 = hashlib.md5(data).hexdigest()
    return (f"{len(data)} {md5} {url}\n"
            "62295 4c72 http://data.gdeltproject.org/gdeltv2/20261121143000.mentions.CSV.zip\n"
            "3236 5cf7 http://data.gdeltproject.org/gdeltv2/20261121143000.gkg.csv.zip\n")


def test_column_table_matches_codebook():
    assert N_COLUMNS == 61
    assert COL["GlobalEventID"] == 0 and COL["EventCode"] == 26
    assert COL["EventRootCode"] == 28 and COL["QuadClass"] == 29
    assert COL["ActionGeo_Type"] == 51 and COL["ActionGeo_Lat"] == 56
    assert COL["DATEADDED"] == 59 and COL["SOURCEURL"] == 60


def test_parse_row():
    d = parse_row(row().split("\t"), FETCHED)
    assert d.kind == "gdelt_event" and d.origin_id == "1334365828"
    assert d.url == "https://news.example/kharkiv-strike"
    assert d.published_at == datetime(2026, 11, 21, 14, 30, tzinfo=UTC)
    assert d.title == "Russia — Fight — Ukraine (190) in Kharkiv, Kharkivs'ka Oblast', Ukraine"
    assert d.attrs["event_root_code"] == "19" and d.attrs["quad_class"] == 4
    assert d.attrs["geo_type"] == 4 and d.attrs["geo_type_name"] == "world_city"
    assert (d.attrs["geo_lat"], d.attrs["geo_lon"]) == (49.9808, 36.2527)
    assert d.attrs["num_articles"] == 10 and d.attrs["goldstein"] == -10.0
    # two events from one article are different documents
    other = parse_row(row(GlobalEventID="2").split("\t"), FETCHED)
    assert other.id != d.id
    assert parse_row(row(SOURCEURL="").split("\t"), FETCHED) is None


def test_parse_export_skips_malformed(caplog):
    lines = [row(), "too\tfew\tcolumns", row(GlobalEventID="5", DATEADDED="garbage"),
             row(GlobalEventID="6", ActionGeo_Lat="", ActionGeo_Long="", ActionGeo_Type="0"),
             ""]
    with caplog.at_level(logging.WARNING):
        docs = parse_export(make_zip(lines), FETCHED)
    assert [d.origin_id for d in docs] == ["1334365828", "6"]
    assert docs[1].attrs["geo_lat"] is None
    assert "skipped 2 malformed" in caplog.text


def test_parse_export_rejects_non_csv_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError, match="no CSV"):
        parse_export(buf.getvalue(), FETCHED)


def test_latest_export_url():
    assert latest_export_url(lastupdate(b"x")) == EXPORT
    assert latest_export_url("garbage") is None


def _ctx(http, tmp_path):
    return CollectorContext(http=http, settings=SimpleNamespace(),
                            store=SqliteStore(tmp_path / "s.sqlite"))


@respx.mock
async def test_poll_downloads_once_per_export(tmp_path):
    data = make_zip([row(), row(GlobalEventID="7")])
    respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(200, text=lastupdate(data)))
    download = respx.get(EXPORT.replace("http://", "https://")).mock(
        return_value=httpx.Response(200, content=data))
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        c = GdeltCollector({})
        batch = await c.poll(ctx)
        assert len(batch.documents) == 2 and batch.size() == 2
        assert ctx.get_cursor("gdelt")["last_file"] == "20261121143000.export.CSV.zip"
        again = await c.poll(ctx)  # lastupdate unchanged -> no download
    assert again.documents == [] and download.call_count == 1
    assert (c.name, c.domain, c.commercial_ok) == ("gdelt", "events", True)
    assert "attribution" in c.licence


@respx.mock
async def test_poll_checksum_mismatch_and_http_errors(tmp_path):
    data = make_zip([row()])
    respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(
        200, text=lastupdate(b"different bytes")))
    respx.get(EXPORT.replace("http://", "https://")).mock(
        return_value=httpx.Response(200, content=data))
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            await GdeltCollector({}).poll(ctx)
        assert ctx.get_cursor("gdelt") is None  # not advanced on failure
    respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await GdeltCollector({}).poll(_ctx(http, tmp_path))


def test_export_slots():
    base = "http://data.gdeltproject.org/gdeltv2/"
    assert export_slots(EXPORT, None, 3) == [
        base + "20261121143000.export.CSV.zip", base + "20261121141500.export.CSV.zip",
        base + "20261121140000.export.CSV.zip"]
    assert export_slots(EXPORT, "20261121141500.export.CSV.zip", 8) == [EXPORT]
    assert export_slots(EXPORT, "20261121143000.export.CSV.zip", 8) == []
    assert export_slots(base + "odd.export.CSV.zip", None, 8) == [base + "odd.export.CSV.zip"]


@respx.mock
async def test_poll_skips_listed_but_unpublished_exports(tmp_path):
    https = "https://data.gdeltproject.org/gdeltv2/"
    data = make_zip([row()])
    older = make_zip([row(GlobalEventID="8"), row(GlobalEventID="9")])
    respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(200, text=lastupdate(data)))
    listed = respx.get(https + "20261121143000.export.CSV.zip").mock(
        return_value=httpx.Response(404))
    respx.get(https + "20261121141500.export.CSV.zip").mock(return_value=httpx.Response(404))
    respx.get(https + "20261121140000.export.CSV.zip").mock(
        return_value=httpx.Response(200, content=older))
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        c = GdeltCollector({})
        batch = await c.poll(ctx)  # first run: newest published export only
        assert len(batch.documents) == 2
        assert ctx.get_cursor("gdelt")["last_file"] == "20261121140000.export.CSV.zip"
        again = await c.poll(ctx)  # still unpublished: no error, cursor unchanged
        assert again.documents == []
        assert ctx.get_cursor("gdelt")["last_file"] == "20261121140000.export.CSV.zip"
        listed.mock(return_value=httpx.Response(200, content=data))
        caught_up = await c.poll(ctx)  # listed export now published; 14:15 stays a gap
        assert len(caught_up.documents) == 1
        assert ctx.get_cursor("gdelt")["last_file"] == "20261121143000.export.CSV.zip"


@respx.mock
async def test_poll_catches_up_missed_exports(tmp_path):
    https = "https://data.gdeltproject.org/gdeltv2/"
    data = make_zip([row()])
    respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(200, text=lastupdate(data)))
    respx.get(https + "20261121143000.export.CSV.zip").mock(
        return_value=httpx.Response(200, content=data))
    respx.get(https + "20261121141500.export.CSV.zip").mock(
        return_value=httpx.Response(200, content=make_zip([row(GlobalEventID="8")])))
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        ctx.set_cursor("gdelt", {"last_file": "20261121140000.export.CSV.zip"})
        batch = await GdeltCollector({}).poll(ctx)
    assert len(batch.documents) == 2
    assert ctx.get_cursor("gdelt")["last_file"] == "20261121143000.export.CSV.zip"


@respx.mock
async def test_poll_first_run_nothing_published(tmp_path):
    respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(200, text=lastupdate(b"x")))
    respx.get(url__regex=r"https://data\.gdeltproject\.org/gdeltv2/.*export").mock(
        return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="none of the last 8 exports published"):
            await GdeltCollector({}).poll(_ctx(http, tmp_path))
