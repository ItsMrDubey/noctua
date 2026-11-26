"""Shared types of the deterministic rule extractors (osint.enrich.rules)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

QUOTE_MAX = 300


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One rule-extracted value and its evidence (the matched span), stored in event_fields
    with source='rule'."""

    field: str
    value: str
    doc_id: str
    quote: str

    def row(self, event_id: str) -> dict[str, str]:
        return {"event_id": event_id, "field": self.field, "value": self.value,
                "doc_id": self.doc_id, "quote": self.quote[:QUOTE_MAX]}


@dataclass(frozen=True, slots=True)
class ExtractDoc:
    """A document linked to an event, as the rule extractors and actor resolution see it."""

    doc_id: str
    title: str
    snippet: str = ""
    url: str = ""
    source: str = ""
    published_at: str = ""  # UTC ISO-8601 (event_documents.published_at; sorts lexically)
    role: str = "mention"  # primary | mention
    language: str = ""
    vector: Any = None  # sentence embedding of title + snippet, when an embedder is on

    @property
    def domain(self) -> str:
        host = (urlsplit(self.url).hostname or "").lower().removeprefix("www.")
        return host or self.source or self.doc_id

    @property
    def text(self) -> str:
        return f"{self.title}. {self.snippet}" if self.snippet else self.title
