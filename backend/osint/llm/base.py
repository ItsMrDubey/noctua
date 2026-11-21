"""Pluggable LLM backend protocol for structured (JSON-schema) extraction."""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class LLMError(Exception):
    """A backend call failed (process error, bad output, refusal)."""


class LLMTimeout(LLMError):
    """A backend call exceeded its time limit (the call was cancelled/killed)."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    cost_usd: float = 0.0
    model: str = ""
    latency_s: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMBackend(Protocol):
    name: str

    async def extract(self, instruction: str, text: str, schema: dict) -> LLMResult:
        """Run ``instruction`` over ``text``; return an object matching ``schema``."""
        ...
