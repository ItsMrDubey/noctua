import gzip

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import static


@pytest.fixture
def fe(tmp_path):
    fe = tmp_path / "fe"
    (fe / "dist" / "assets").mkdir(parents=True)
    (fe / "vendor").mkdir()
    (fe / "js").mkdir()
    (fe / "index.html").write_text("<html>src</html>")
    (fe / "dist" / "index.html").write_text("<html>built</html>")
    body = b"console.log(1);" * 200
    (fe / "dist" / "assets" / "app-abc.js").write_bytes(body)
    (fe / "dist" / "assets" / "app-abc.js.gz").write_bytes(gzip.compress(body))
    (fe / "vendor" / "big.js").write_bytes(body)
    (fe / "js" / "x.js").write_text("export const a = 1;")
    return fe


@pytest.fixture
def client(fe):
    app = FastAPI()

    @app.get("/")
    def index():
        return static.index_response(fe)

    @app.get("/api/big")
    def big():
        return {"x": "y" * 5000}

    static.mount_static(app, fe)
    return TestClient(app)


def test_index_prefers_build(client, fe, monkeypatch):
    r = client.get("/")
    assert r.text == "<html>built</html>" and r.headers["cache-control"] == "no-cache"
    monkeypatch.setenv("OSINT_FRONTEND", "src")
    assert client.get("/").text == "<html>src</html>"
    monkeypatch.delenv("OSINT_FRONTEND")
    (fe / "dist" / "index.html").unlink()
    assert client.get("/").text == "<html>src</html>"


def test_precompressed_asset(client):
    r = client.get("/static/dist/assets/app-abc.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.text == "console.log(1);" * 200  # client transparently decodes
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == static.IMMUTABLE
    etag = r.headers["etag"]
    r2 = client.get("/static/dist/assets/app-abc.js",
                    headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert r2.status_code == 304


def test_identity_when_gzip_not_accepted(client):
    r = client.get("/static/dist/assets/app-abc.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers and r.content == b"console.log(1);" * 200


def test_stale_gz_ignored(client, fe):
    import os
    import time
    gz = fe / "dist" / "assets" / "app-abc.js.gz"
    old = time.time() - 100
    os.utime(gz, (old, old))
    r = client.get("/static/dist/assets/app-abc.js", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert static.FrontendFiles(directory=fe)._fresh_gz("dist/assets/app-abc.js")[1] is None


def test_cache_policy(client):
    assert client.get("/static/vendor/big.js").headers["cache-control"] == static.DAY
    assert client.get("/static/js/x.js").headers["cache-control"] == "no-cache"
    assert client.get("/static/js/x.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/static/nope.js").status_code == 404
    assert client.get("/static/../index.html").status_code == 404


def test_api_json_gzipped(client):
    r = client.get("/api/big", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.json()["x"] == "y" * 5000
