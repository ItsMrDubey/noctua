from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from osint.backfill.common import BackfillContext, BackfillStore, Checkpoint
from osint.config import ROOT, Settings
from osint.store.lake import Lake


def make_settings(tmp_path: Path, **over: object) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, config_dir=ROOT / "config",
                    firms_map_key=str(over.pop("firms_map_key", "")), **over)


@pytest.fixture
async def make_ctx(tmp_path: Path) -> AsyncIterator:
    clients: list[httpx.AsyncClient] = []
    stores: list[BackfillStore] = []
    lakes: list[Lake] = []

    def factory(source: str, cfg: dict | None = None, *, dry_run: bool = False,
                **settings_over: object) -> BackfillContext:
        settings = make_settings(tmp_path, **settings_over)
        store = BackfillStore(settings.sqlite_path)
        lake = Lake(settings.lake_dir)
        http = httpx.AsyncClient()
        clients.append(http)
        stores.append(store)
        lakes.append(lake)
        return BackfillContext(settings=settings, cfg=cfg or {}, store=store, lake=lake,
                               http=http, checkpoint=Checkpoint(store, source, dry_run=dry_run),
                               dry_run=dry_run, tmp_dir=tmp_path / "tmp")

    yield factory
    for c in clients:
        await c.aclose()
    for lk in lakes:
        lk.close()
    for s in stores:
        s.close()
