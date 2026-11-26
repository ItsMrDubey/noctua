"""Weapons / platforms by lexicon (deterministic extraction spec §2.4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from osint.enrich.phrases import PhraseHit, PhraseMatcher
from osint.enrich.rules.base import ExtractDoc, FieldValue

_IGNORE = ""  # payload of phrases that contain a weapon word but are no weapon ("think tank")


class WeaponLexicon:
    def __init__(self, labels: Mapping[str, Sequence[str]], *,
                 subsumed: Mapping[str, Sequence[str]] | None = None,
                 ignore: Sequence[str] = ()) -> None:
        self.matcher: PhraseMatcher[str] = PhraseMatcher()
        for phrase in ignore:
            self.matcher.add(str(phrase), _IGNORE)
        for label, phrases in labels.items():
            for phrase in phrases or ():
                self.matcher.add(str(phrase), str(label))
        self.subsumed = {str(k): {str(x) for x in v or ()} for k, v in (subsumed or {}).items()}

    def __len__(self) -> int:
        return len(self.matcher)

    def hits(self, text: str) -> list[PhraseHit[str]]:
        return [h for h in self.matcher.find(text) if h.payload != _IGNORE]


def weapons(event: Mapping[str, Any], docs: Sequence[ExtractDoc], *,
            lexicon: WeaponLexicon) -> list[FieldValue]:
    """One ``weapons_platforms`` value per canonical label, quoting its first mention
    (documents oldest first); a generic label is dropped when a more specific one it
    subsumes was found ("missile" next to "cruise missile")."""
    first: dict[str, FieldValue] = {}
    for d in sorted(docs, key=lambda d: (d.published_at, d.doc_id)):
        for text in (d.title, d.snippet):
            for h in lexicon.hits(text or ""):
                first.setdefault(h.payload, FieldValue("weapons_platforms", h.payload,
                                                       d.doc_id, h.text))
    drop = {g for g, specific in lexicon.subsumed.items() if g in first and specific & set(first)}
    return [v for label, v in sorted(first.items()) if label not in drop]
