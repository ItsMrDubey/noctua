from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.backfill import mount_backfill
from osint.backfill.common import BackfillStore, Checkpoint


def test_backfill_status_endpoint(tmp_path):
    store = BackfillStore(tmp_path / "s.sqlite")
    cp = Checkpoint(store, "usgs")
    cp.start(None)
    cp.add_rows("observations", 42)
    cp.mark_done("2026-01")
    app = FastAPI()
    mount_backfill(app, SimpleNamespace(store=store))
    with TestClient(app) as client:
        body = client.get("/api/backfill/status").json()
    usgs = body["sources"]["usgs"]
    assert usgs["running"] is True and usgs["rows"] == {"observations": 42}
    assert usgs["last_checkpoint"] == "2026-01" and usgs["units_done"] == 1
    assert body["sources"]["ucdp"]["state"] == "never_run"
    store.close()
