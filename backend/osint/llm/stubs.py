"""Placeholders for the commercial extraction backends (not implemented in the POC)."""

from osint.llm.base import LLMResult


class AnthropicApiBackend:
    """Anthropic Messages API with structured outputs (commercial path, per-token billing)."""

    name = "anthropic_api"

    def __init__(self, model: str = "haiku", **_: object) -> None:
        self.model = model

    async def extract(self, instruction: str, text: str, schema: dict) -> LLMResult:
        raise NotImplementedError(
            "AnthropicApiBackend is not implemented yet: use backend 'claude_cli' in "
            "config/extraction.yaml (needs an ANTHROPIC_API_KEY and the anthropic SDK).")


class AlterLabByokBackend:
    """AlterLab /extract with the user's own LLM key (spec §5 stage 4, commercial path)."""

    name = "alterlab_byok"

    def __init__(self, model: str = "haiku", **_: object) -> None:
        self.model = model

    async def extract(self, instruction: str, text: str, schema: dict) -> LLMResult:
        raise NotImplementedError(
            "AlterLabByokBackend is not implemented yet: use backend 'claude_cli' in "
            "config/extraction.yaml (needs an AlterLab key with a BYOK LLM configured).")
