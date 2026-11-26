"""Event lifecycle (deterministic extraction spec §2.5).

From the time the newest linked article was published (``last_doc_at``; GDELT rows do not
count; sensor events use ``t_end``, when the signal was last seen):

* ``developing``: newest report <= ``developing_h`` old and >= ``developing_min_docs``
  reports within ``developing_h``;
* ``active``: newest report <= ``active_h`` old;
* ``cooling``: newest report <= ``cooling_d`` old;
* ``closed``: older.

Thresholds: ``config/events.yaml`` ``lifecycle:``. The EventBuilder refreshes the events a
pass touched plus the events not yet closed, at most ``max_open_per_pass`` of them by id
(``refresh_lifecycle``): the one-off classification of rows written before the column
existed spreads over passes, since rows that become ``closed`` leave the open set.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

STATES = ("developing", "active", "cooling", "closed")


@dataclass(frozen=True)
class LifecycleConfig:
    developing_h: float = 6.0
    developing_min_docs: int = 2
    active_h: float = 48.0
    cooling_d: float = 7.0
    max_open_per_pass: int = 20000  # open-event scan cap per pass (ids=None)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> LifecycleConfig:
        d = d or {}
        base = cls()
        return cls(developing_h=float(d.get("developing_h", base.developing_h)),
                   developing_min_docs=int(d.get("developing_min_docs",
                                                 base.developing_min_docs)),
                   active_h=float(d.get("active_h", base.active_h)),
                   cooling_d=float(d.get("cooling_d", base.cooling_d)),
                   max_open_per_pass=int(d.get("max_open_per_pass", base.max_open_per_pass)))


def lifecycle_state(last_at: datetime | None, recent_docs: int, now: datetime,
                    cfg: LifecycleConfig) -> str:
    if last_at is None:
        return "closed"
    age = now - last_at
    if age <= timedelta(hours=cfg.developing_h) and recent_docs >= cfg.developing_min_docs:
        return "developing"
    if age <= timedelta(hours=cfg.active_h):
        return "active"
    if age <= timedelta(days=cfg.cooling_d):
        return "cooling"
    return "closed"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def lifecycle_for_row(row: dict, now: datetime, cfg: LifecycleConfig) -> str:
    """State of a ``SqliteStore.lifecycle_inputs`` row: sensor events by ``t_end``, others
    by their newest article (``t_end`` when they have none)."""
    if row.get("origin") == "sensor":
        last = _parse(row.get("t_end"))
    else:
        last = _parse(row.get("last_doc_at")) or _parse(row.get("t_end"))
    return lifecycle_state(last, int(row.get("recent_docs") or 0), now, cfg)


def refresh_lifecycle(store: Any, now: datetime, cfg: LifecycleConfig,
                      ids: Iterable[str] | None = None) -> int:
    """Recompute and store the lifecycle of ``ids`` (None: the events not yet closed, the
    first ``cfg.max_open_per_pass`` by id); returns the number of events whose state
    changed."""
    if ids is not None:
        ids = list(ids)
        if not ids:
            return 0
    rows = store.lifecycle_inputs(now - timedelta(hours=cfg.developing_h), ids,
                                  limit=cfg.max_open_per_pass if ids is None else None)
    changes = {}
    for r in rows:
        state = lifecycle_for_row(r, now, cfg)
        if state != r.get("lifecycle"):
            changes[r["id"]] = state
    if changes:
        store.set_lifecycle(changes)
    return len(changes)
