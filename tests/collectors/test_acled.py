import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.acled import READ_URL, TOKEN_URL, AcledCollector, category, map_row
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def acled_row(**over):
    r = {"event_id_cnty": "SDN12345", "event_date": "2026-11-20", "year": 2026,
         "time_precision": 1, "disorder_type": "Political violence", "event_type": "Battles",
         "sub_event_type": "Armed clash", "actor1": "Rapid Support Forces",
         "actor2": "Military Forces of Sudan (2019-)", "inter1": "2", "inter2": "1",
         "interaction": "12", "civilian_targeting": "", "country": "Sudan",
         "admin1": "North Darfur", "admin2": "El Fasher", "location": "El Fasher",
         "latitude": "13.6290", "longitude": "25.3494", "geo_precision": 1,
         "source": "Sudan Tribune; Radio Dabanga", "source_scale": "National",
         "notes": "On 20 November 2026, RSF clashed with SAF in El Fasher. 12 killed.",
         "fatalities": 12, "timestamp": 1795400000}
    r.update(over)
    return r


def _ctx(http, tmp_path, email="me@example.org", password="pw"):
    return CollectorContext(http=http,
                            settings=SimpleNamespace(acled_email=email, acled_password=password),
                            store=SqliteStore(tmp_path / "s.sqlite"))


def _token(n=1):
    return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 86400,
                                     "access_token": f"tok{n}", "refresh_token": f"ref{n}"})


def test_map_row():
    m = map_row(acled_row(), NOW)
    e = m["event"]
    assert e["id"] == "acled-SDN12345" and e["category"] == "armed_conflict"
    assert e["subcategory"] == "armed clash" and e["status"] == "historical"
    assert e["origin"] == "historical" and e["precision"] == "city"
    assert (e["lat"], e["lon"]) == (13.629, 25.3494)
    assert e["t_start"].startswith("2026-11-20T00:00:00")
    assert e["title"].startswith("Armed clash: Rapid Support Forces vs Military Forces")
    assert "(12 killed)" in e["title"] and e["mention_count"] == 2
    assert e["confidence"] == round(0.6 * 0.85 + 0.4 * 1.0, 4)
    assert m["document"]["source"] == "acled" and m["timestamp"] == 1795400000
    fields = {f["field"]: f["value"] for f in m["fields"]}
    assert fields["fatalities_best"] == "12" and fields["event_type"] == "Battles"
    assert map_row(acled_row(latitude=""), NOW) is None
    assert map_row(acled_row(event_id_cnty=""), NOW) is None
    with pytest.raises(ValueError):
        map_row(acled_row(event_date="soon"), NOW)


def test_category_mapping():
    assert category("Protests", "Peaceful protest") == "civil_unrest"
    assert category("Riots", "Mob violence") == "civil_unrest"
    assert category("Explosions/Remote violence", "Air/drone strike") == "armed_conflict"
    assert category("Violence against civilians", "Attack") == "armed_conflict"
    assert category("Strategic developments", "Agreement") == "political"
    assert category("Strategic developments", "Looting/property destruction") == "civil_unrest"


async def test_idle_without_credentials(tmp_path, caplog):
    c = AcledCollector({})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path, email="", password="")
        with caplog.at_level(logging.INFO), respx.mock(assert_all_called=False) as mock:
            route = mock.route()
            for _ in range(3):
                assert (await c.poll(ctx)).size() == 0
        assert not route.called
    assert caplog.text.count("ACLED_EMAIL / ACLED_PASSWORD not set") == 1


@respx.mock
async def test_poll_logs_in_saves_events_and_is_incremental(tmp_path):
    token = respx.post(TOKEN_URL).mock(return_value=_token())
    read = respx.get(READ_URL).mock(side_effect=[
        httpx.Response(200, json={"status": 200, "success": True, "count": 2, "data": [
            acled_row(), acled_row(event_id_cnty="SDN12346", event_type="Protests",
                                   sub_event_type="Peaceful protest", timestamp=1795400500,
                                   actor2="", fatalities=0)]}),
        httpx.Response(200, json={"status": 200, "data": []}),
    ])
    c = AcledCollector({"days": 7})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        await c.poll(ctx)
        cur = ctx.get_cursor("acled")
        assert cur["state"] == "ok" and cur["last_timestamp"] == 1795400500
        assert "tok" not in str(cur) and "pw" not in str(cur)
        assert ctx.store.get_event("acled-SDN12346")["category"] == "civil_unrest"
        assert ctx.store.get_event("acled-SDN12345")["fields"]
        await c.poll(ctx)
    assert token.call_count == 1  # access token reused
    form = token.calls[0].request.content.decode()
    assert "grant_type=password" in form and "client_id=acled" in form
    first, second = (call.request for call in read.calls)
    assert first.headers["Authorization"] == "Bearer tok1"
    assert first.url.params["event_date_where"] == "BETWEEN"
    assert second.url.params["timestamp"] == "1795400499"
    assert second.url.params["timestamp_where"] == ">"


@respx.mock
async def test_paging(tmp_path):
    respx.post(TOKEN_URL).mock(return_value=_token())
    read = respx.get(READ_URL).mock(side_effect=[
        httpx.Response(200, json={"data": [acled_row(), acled_row(event_id_cnty="X2")]}),
        httpx.Response(200, json={"data": [acled_row(event_id_cnty="X3")]}),
    ])
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        await AcledCollector({"page_size": 2}).poll(ctx)
    assert [c.request.url.params["page"] for c in read.calls] == ["1", "2"]
    assert ctx.get_cursor("acled")["events"] == 3


@respx.mock
async def test_access_denied_backs_off_with_one_warning(tmp_path, caplog):
    token = respx.post(TOKEN_URL).mock(return_value=_token())
    read = respx.get(READ_URL).mock(
        return_value=httpx.Response(403, json={"message": "Access denied"}))
    c = AcledCollector({"denied_backoff_h": 6})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        with caplog.at_level(logging.WARNING):
            assert (await c.poll(ctx)).size() == 0
            await c.poll(ctx)  # still blocked: no request
        cur = ctx.get_cursor("acled")
        assert cur["state"] == "access_denied" and cur["blocked_until"]
        # a fresh collector (restart) honours the persisted back-off
        await AcledCollector({}).poll(ctx)
    assert read.call_count == 1 and token.call_count == 1
    assert caplog.text.count("lacks API access") == 1
    assert "tok1" not in caplog.text and "pw" not in caplog.text


@respx.mock
async def test_access_denied_in_a_200_body(tmp_path):
    respx.post(TOKEN_URL).mock(return_value=_token())
    respx.get(READ_URL).mock(return_value=httpx.Response(200, json={"message": "Access denied"}))
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        await AcledCollector({}).poll(ctx)
        assert ctx.get_cursor("acled")["state"] == "access_denied"


@respx.mock
async def test_expired_token_relogs_once(tmp_path):
    token = respx.post(TOKEN_URL).mock(side_effect=[_token(1), _token(2)])
    read = respx.get(READ_URL).mock(side_effect=[
        httpx.Response(401, json={"message": "Access denied: authentication required."}),
        httpx.Response(200, json={"data": [acled_row()]}),
    ])
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        await AcledCollector({}).poll(ctx)
        assert ctx.get_cursor("acled")["state"] == "ok"
    assert token.call_count == 2
    assert read.calls[1].request.headers["Authorization"] == "Bearer tok2"


@respx.mock
async def test_refresh_token_used_before_expiry(tmp_path):
    token = respx.post(TOKEN_URL).mock(side_effect=[_token(1), _token(2)])
    respx.get(READ_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    c = AcledCollector({})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        await c.poll(ctx)
        c._access_exp = 0.0  # access token expired, refresh token still valid
        await c.poll(ctx)
    form = token.calls[1].request.content.decode()
    assert "grant_type=refresh_token" in form and "refresh_token=ref1" in form
    assert "password" not in form


@respx.mock
async def test_rejected_login(tmp_path, caplog):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        400, json={"error": "invalid_grant"}))
    read = respx.get(READ_URL)
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        with caplog.at_level(logging.WARNING):
            await AcledCollector({}).poll(ctx)
        assert ctx.get_cursor("acled")["state"] == "login_failed"
    assert not read.called
    assert "login rejected" in caplog.text


@respx.mock
async def test_transport_error_raises_without_secrets(tmp_path):
    respx.post(TOKEN_URL).mock(return_value=_token())
    respx.get(READ_URL).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError) as exc:
            await AcledCollector({}).poll(_ctx(http, tmp_path))
    assert str(exc.value) == "acled: ConnectError"
