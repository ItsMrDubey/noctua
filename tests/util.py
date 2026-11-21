"""Shared test helpers."""

import asyncio
import time
from collections.abc import Callable


async def eventually(pred: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> None:
    """Poll ``pred`` until it is true; fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)
