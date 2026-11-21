"""WebSocket live push: full snapshot then 2 s deltas computed from Ingestor.live."""

import asyncio
import contextlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from osint.api import layers
from osint.core.geo import utcnow
from osint.core.models import Position
from osint.runtime import Runtime

INTERVAL_S = 2.0
_KIND_KEY = {"air": "aircraft", "sea": "ships"}


class Differ:
    """Per-connection state: last-sent ts per id and cached track metadata."""

    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.sent: dict[str, object] = {}
        self.tracks: dict[str, dict] = {}

    def _copy_live(self) -> list[Position]:
        with self.rt.ingestor._lock:
            return list(self.rt.ingestor.live.values())

    def step(self, full: bool) -> dict:
        now = utcnow()
        cur = {}
        for p in self._copy_live():
            key = _KIND_KEY.get(p.kind)
            if key and p.ts >= now - layers.MAX_AGE[key]:
                cur[p.track_id] = p
        changed = [p for tid, p in cur.items() if full or self.sent.get(tid) != p.ts]
        new_ids = [p.track_id for p in changed if p.track_id not in self.tracks]
        if new_ids:
            fetched = self.rt.store.get_tracks(new_ids)
            for tid in new_ids:
                self.tracks[tid] = fetched.get(tid) or {}
        msg: dict = {"type": "snapshot" if full else "delta"}
        for kind, key in _KIND_KEY.items():
            ps = [p for p in changed if p.kind == kind]
            msg[key] = layers.position_items(self.rt, ps, self.tracks)
        msg["removed"] = [] if full else [t for t in self.sent if t not in cur]
        for t in msg["removed"]:
            del self.sent[t]
            self.tracks.pop(t, None)
        for p in changed:
            self.sent[p.track_id] = p.ts
        msg["generated_at"] = now.isoformat()
        return msg


def mount(app: FastAPI, runtime: Runtime) -> None:
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket) -> None:
        await ws.accept()
        diff = Differ(runtime)
        try:
            await ws.send_json(await asyncio.to_thread(diff.step, True))
            while True:
                await asyncio.sleep(INTERVAL_S)
                await ws.send_json(await asyncio.to_thread(diff.step, False))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
