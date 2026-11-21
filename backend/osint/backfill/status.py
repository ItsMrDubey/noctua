"""Backfill status from the ``backfill:<source>`` checkpoints (for the API)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from osint.backfill import SOURCES
from osint.backfill.common import CURSOR_PREFIX, HEARTBEAT_STALE
from osint.core.geo import utcnow


def _running(state: dict[str, Any], now: datetime) -> bool:
    if state.get("state") != "running":
        return False
    try:
        updated = datetime.fromisoformat(str(state.get("updated_at")))
    except ValueError:
        return False
    return now - updated < HEARTBEAT_STALE


def summarize(state: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    if not state:
        return {"state": "never_run"}
    s = state.get("state")
    if s == "running" and not _running(state, now):
        s = "stale"  # no heartbeat: the process died or was killed
    return {
        "state": s,
        "running": s == "running",
        "window": state.get("window"),
        "last_checkpoint": state.get("last_checkpoint"),
        "units_done": len(state.get("done") or []),
        "rows": state.get("rows") or {},
        "run_rows": state.get("run_rows") or {},
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "finished_at": state.get("finished_at"),
        "message": state.get("message"),
        "run_errors": state.get("run_errors") or 0,
        "errors": (state.get("errors") or [])[-5:],
        "summary": state.get("summary"),
        "progress": state.get("progress"),
    }


def backfill_status(store: Any) -> dict[str, Any]:
    now = utcnow()
    out = {src: summarize(store.get_cursor(CURSOR_PREFIX + src), now) for src in SOURCES}
    cluster = store.get_cursor(CURSOR_PREFIX + "cluster")
    out["cluster"] = cluster or {"state": "never_run"}
    return {"sources": out, "generated_at": now.isoformat()}
