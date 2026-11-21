"""WebSocket live push: full snapshot then 2 s deltas computed from Ingestor.live."""

import asyncio
import contextlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from osint.api import layers
from osint.core.geo import utcnow
from osint.runtime import Runtime

INTERVAL_S = 2.0
_KIND_KEY = {"air": "aircraft", "sea": "ships"}


class Differ:
    """Per-connection state: last-sent ts per id. Track metadata comes from the shared
    TrackMetaCache, so connections don't each query the store."""

    def __init__(self, runtime: Runtime, meta: layers.TrackMetaCache | None = None) -> None:
        self.rt = runtime
        self.meta = meta or layers.TrackMetaCache(runtime)
        self.sent: dict[str, object] = {}

    def step(self, full: bool) -> dict:
        now = utcnow()
        cur = {}
        for p in layers.copy_live(self.rt):
            key = _KIND_KEY.get(p.kind)
            if key and p.ts >= now - layers.MAX_AGE[key]:
                cur[p.track_id] = p
        changed = [p for tid, p in cur.items() if full or self.sent.get(tid) != p.ts]
        tracks = self.meta.get_many([p.track_id for p in changed]) if changed else {}
        msg: dict = {"type": "snapshot" if full else "delta"}
        for kind, key in _KIND_KEY.items():
            msg[key] = [layers.slim_item(p, tracks.get(p.track_id))
                        for p in changed if p.kind == kind]
        msg["removed"] = [] if full else [t for t in self.sent if t not in cur]
        for t in msg["removed"]:
            del self.sent[t]
        for p in changed:
            self.sent[p.track_id] = p.ts
        msg["generated_at"] = now.isoformat()
        return msg

    def step_encoded(self, full: bool) -> str:
        return layers.encode(self.step(full)).decode()


def mount(app: FastAPI, runtime: Runtime, meta: layers.TrackMetaCache | None = None) -> None:
    shared = meta or layers.TrackMetaCache(runtime)

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket) -> None:
        await ws.accept()
        diff = Differ(runtime, shared)
        try:
            await ws.send_text(await asyncio.to_thread(diff.step_encoded, True))
            while True:
                await asyncio.sleep(INTERVAL_S)
                await ws.send_text(await asyncio.to_thread(diff.step_encoded, False))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
