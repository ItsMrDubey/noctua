import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.reference import mount_reference
from osint.reference import loader
from tests.reference.test_reference import AIRPORTS, RUNWAYS


@pytest.fixture
def client(tmp_path):
    (tmp_path / "airports.csv").write_text(AIRPORTS, encoding="utf-8")
    (tmp_path / "runways.csv").write_text(RUNWAYS, encoding="utf-8")
    app = FastAPI()
    mount_reference(app, loader.Reference(tmp_path))
    return TestClient(app)


def test_airports(client):
    j = client.get("/api/reference/airports?bbox=19,9,22,12&min_class=large").json()
    assert [a["ident"] for a in j["items"]] == ["KBIG"]
    assert client.get("/api/reference/airports?military_only=true").json()["count"] == 2
    assert client.get("/api/reference/airports?bbox=bad").status_code == 422
    assert client.get("/api/reference/airports?min_class=x").status_code == 422


def test_runways_span_limit(client):
    assert client.get("/api/reference/runways?bbox=0,0,30,30").status_code == 400
    j = client.get("/api/reference/runways?bbox=19.5,9.5,20.5,10.5").json()
    assert j["count"] == 1 and len(j["items"][0]["rect"]) == 4


def test_misc_endpoints(client):
    assert client.get("/api/reference/ports").json()["items"] == []
    assert client.get("/api/reference/bases?type=airbase").json()["count"] > 0
    assert client.get("/api/reference/chokepoints").json()["count"] >= 15
    assert client.get("/api/reference/countries").json()["items"] == []
    r = client.get("/api/reference/boundaries/land")
    assert r.status_code == 200 and r.headers["cache-control"] == "max-age=86400"
    assert client.get("/api/reference/boundaries/nope").status_code == 404
    assert client.get("/api/reference/sources").json()["items"]
