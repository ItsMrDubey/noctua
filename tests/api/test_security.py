import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from osint.api.app import create_app
from osint.api.security import is_loopback
from osint.cli import check_bind
from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Position
from osint.runtime import Runtime

NS = {"k": "http://www.opengis.net/kml/2.2"}
LOCAL = "http://127.0.0.1"


def _rt(tmp_path, **kw):
    return Runtime(Settings(_env_file=None, data_dir=tmp_path, **kw), [])


def test_foreign_host_rejected(tmp_path):
    client = TestClient(create_app(_rt(tmp_path)), base_url="http://evil.example")
    assert client.get("/health").status_code == 400
    for host in ("127.0.0.1", "localhost", "[::1]"):
        assert TestClient(create_app(_rt(tmp_path)),
                          base_url=f"http://{host}:8765").get("/health").status_code == 200


def test_allowed_hosts_setting(tmp_path):
    rt = _rt(tmp_path, allowed_hosts="osint.lan, 10.0.0.5")
    assert TestClient(create_app(rt), base_url="http://osint.lan").get(
        "/health").status_code == 200


def test_open_without_token(tmp_path):
    client = TestClient(create_app(_rt(tmp_path)), base_url=LOCAL)
    assert client.get("/api/config").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_token_required_when_configured(tmp_path):
    client = TestClient(create_app(_rt(tmp_path, api_token="s3cret")), base_url=LOCAL)
    for path in ("/api/config", "/metrics", "/docs", "/openapi.json", "/kml/master.kml"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/api/config", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    # query token only for KML / WebSocket (Google Earth and browsers can't send headers)
    assert client.get("/api/config?token=s3cret").status_code == 401
    assert client.get("/kml/master.kml?token=s3cret").status_code == 200
    assert client.get("/health").status_code == 200  # liveness stays open
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("ws://127.0.0.1/ws/live") as ws:
        ws.receive_json()
    with client.websocket_connect("ws://127.0.0.1/ws/live?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_kml_hrefs_carry_token(tmp_path):
    client = TestClient(create_app(_rt(tmp_path, api_token="a b&c")), base_url=LOCAL)
    root = ET.fromstring(client.get("/kml/master.kml?token=a%20b%26c").content)
    hrefs = [h.text for h in root.findall(".//k:NetworkLink/k:Link/k:href", NS)]
    assert hrefs[0] == "http://127.0.0.1/kml/aircraft.kml?token=a%20b%26c"
    assert all(h.endswith("?token=a%20b%26c") for h in hrefs)


def test_cli_refuses_public_bind_without_token():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    check_bind(Settings(_env_file=None))
    with pytest.raises(SystemExit, match="OSINT_API_TOKEN"):
        check_bind(Settings(_env_file=None, host="0.0.0.0"))
    check_bind(Settings(_env_file=None, host="0.0.0.0", api_token="t"))


def test_live_summary_uses_max_age(tmp_path):
    rt = _rt(tmp_path)
    now = utcnow()
    for tid, kind, age in (("air:1", "air", timedelta(minutes=1)),
                           ("air:2", "air", timedelta(hours=2)),
                           ("sea:1", "sea", timedelta(hours=5)),
                           ("sea:2", "sea", timedelta(hours=10))):
        rt.ingestor.live[tid] = Position(track_id=tid, kind=kind, ts=now - age, lat=0, lon=0,
                                         source="t")
    client = TestClient(create_app(rt), base_url=LOCAL)
    assert client.get("/api/live/summary").json()["tracks"] == {"air": 1, "sea": 1}


def test_signed_urls_for_img_paths(tmp_path):
    from osint.api.security import sign_path, valid_signature

    client = TestClient(create_app(_rt(tmp_path, api_token="s3cret")), base_url=LOCAL)
    path = "/api/webcams/tfl:00001.1/snapshot"
    assert client.get(path).status_code == 401
    signed = client.get("/api/sign", params={"path": path}, headers={"Authorization": "Bearer s3cret"}).json()["url"]
    assert "sig=" in signed and "exp=" in signed
    assert client.get(signed).status_code == 404  # authenticated by signature, unknown camera
    assert client.get(signed.replace("tfl:00001.1", "tfl:00002.1")).status_code == 401  # bound to the path
    assert client.get(signed.replace("sig=", "sig=0")).status_code == 401
    assert client.get(signed, headers={"Authorization": "Bearer wrong"}).status_code == 404  # sig suffices
    assert client.get("/api/sign", params={"path": path}).status_code == 401
    assert client.get("/api/sign", params={"path": "/api/events"},
                      headers={"Authorization": "Bearer s3cret"}).status_code == 422
    # not a general bypass: other protected paths ignore signatures
    assert client.get(sign_path("s3cret", "/api/config") + "?sig=x&exp=9999999999999").status_code == 401
    # expiry
    old = sign_path("s3cret", path, now=1000)
    q = old.split("?", 1)[1]
    assert not valid_signature("s3cret", path, q, now=1000 + 301)
    assert valid_signature("s3cret", path, q, now=1000 + 299)
    assert sign_path("", path) == path  # no token configured: plain URL


def test_config_ion_token_only_for_loopback_or_authenticated(tmp_path):
    rt = _rt(tmp_path, cesium_ion_token="ion-secret")
    # TestClient peers as "testclient" (not loopback): no token handed out without auth
    assert TestClient(create_app(rt), base_url=LOCAL).get("/api/config").json()["cesium_ion_token"] is None
    rt2 = _rt(tmp_path, cesium_ion_token="ion-secret", api_token="s3cret")
    c = TestClient(create_app(rt2), base_url=LOCAL)
    assert c.get("/api/config", headers={"Authorization": "Bearer s3cret"}).json()["cesium_ion_token"] == "ion-secret"
