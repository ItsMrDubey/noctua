"""Hourly + daily call caps (sliding windows) for LLM backends billed to the user's plan."""

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable


class CallRateLimiter:
    def __init__(self, per_hour: int, per_day: int, *,
                 clock: Callable[[], float] = time.time,
                 history: Iterable[float] = ()) -> None:
        self.per_hour, self.per_day = per_hour, per_day
        self.clock = clock
        self._calls: deque[float] = deque(sorted(history))
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and self._calls[0] <= now - 86400:
            self._calls.popleft()

    def counts(self) -> dict[str, int]:
        now = self.clock()
        with self._lock:
            self._prune(now)
            hour = sum(1 for t in self._calls if t > now - 3600)
            return {"hour": hour, "day": len(self._calls)}

    def try_acquire(self) -> bool:
        """Reserve one call if both caps allow it."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            hour = sum(1 for t in self._calls if t > now - 3600)
            if hour >= self.per_hour or len(self._calls) >= self.per_day:
                return False
            self._calls.append(now)
            return True
