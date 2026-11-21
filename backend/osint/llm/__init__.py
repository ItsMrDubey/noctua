"""LLM backends (pluggable) used by the extraction stage."""

from osint.llm.base import LLMBackend, LLMError, LLMResult, LLMTimeout
from osint.llm.claude_cli import ClaudeCliBackend
from osint.llm.ratelimit import CallRateLimiter
from osint.llm.stubs import AlterLabByokBackend, AnthropicApiBackend

BACKENDS = {
    "claude_cli": ClaudeCliBackend,
    "anthropic_api": AnthropicApiBackend,
    "alterlab_byok": AlterLabByokBackend,
}


def make_backend(name: str, **kwargs) -> LLMBackend:
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise ValueError(f"unknown LLM backend {name!r} (choose from {sorted(BACKENDS)})") \
            from None
    return cls(**kwargs)


__all__ = ["BACKENDS", "AlterLabByokBackend", "AnthropicApiBackend", "CallRateLimiter",
           "ClaudeCliBackend", "LLMBackend", "LLMError", "LLMResult", "LLMTimeout",
           "make_backend"]
