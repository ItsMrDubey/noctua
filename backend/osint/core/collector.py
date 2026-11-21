"""Collector contract, execution context and registry."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from osint.config import Settings
from osint.core.models import Batch
from osint.store.sqlite import SqliteStore


@dataclass
class CollectorContext:
    http: httpx.AsyncClient
    settings: Settings
    store: SqliteStore

    def get_cursor(self, name: str) -> dict | None:
        return self.store.get_cursor(name)

    def set_cursor(self, name: str, cursor: dict) -> None:
        self.store.set_cursor(name, cursor)


class BaseCollector(ABC):
    name: ClassVar[str]
    domain: ClassVar[str]
    licence: ClassVar[str]
    commercial_ok: ClassVar[bool]

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg


class PollCollector(BaseCollector):
    default_interval_s: ClassVar[float] = 60.0

    @property
    def interval_s(self) -> float:
        return float(self.cfg.get("interval_s", self.default_interval_s))

    @abstractmethod
    async def poll(self, ctx: CollectorContext) -> Batch: ...


class StreamCollector(BaseCollector):
    @abstractmethod
    def stream(self, ctx: CollectorContext) -> AsyncIterator[Batch]: ...


REGISTRY: dict[str, type[BaseCollector]] = {}


def register(cls: type[BaseCollector]) -> type[BaseCollector]:
    REGISTRY[cls.name] = cls
    return cls


def build_collectors(sources_cfg: dict[str, dict[str, Any]]) -> list[BaseCollector]:
    import osint.collectors  # noqa: F401  (registers built-in collectors)

    out: list[BaseCollector] = []
    for name, cfg in sources_cfg.items():
        cfg = cfg or {}  # an empty YAML block parses as None
        if not cfg.get("enabled", True):
            continue
        if name not in REGISTRY:
            raise KeyError(f"unknown collector '{name}' in sources.yaml")
        out.append(REGISTRY[name](cfg))
    return out
