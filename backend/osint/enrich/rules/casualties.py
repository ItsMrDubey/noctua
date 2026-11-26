"""Casualty counts by multilingual patterns (deterministic extraction spec §2.4).

Patterns come from ``config/extract_rules.yaml`` (``casualties``): regex templates per
field (killed / injured) and language, with ``{n}`` standing for a number in digits
("12", "1,200", Arabic-Indic "١٢") or a number word up to twenty. They run on the
case-folded title and snippet of every document, whatever its language. A document
counts with its largest number; the event value is the lower median over distinct
domains (one value per domain: its largest), ignoring documents that state no number.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from osint.enrich.rules.base import ExtractDoc, FieldValue

FIELDS = {"killed": "casualties.killed", "injured": "casualties.injured"}
_SEPARATORS = re.compile("[,  ]")


class CasualtyRules:
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.words: dict[str, int] = {}
        for table in (cfg.get("number_words") or {}).values():
            for word, value in (table or {}).items():
                self.words[unicodedata.normalize("NFC", str(word)).casefold()] = int(value)
        alt = "|".join(re.escape(w) for w in sorted(self.words, key=len, reverse=True))
        num = (r"(?P<n>\d{1,3}(?:[,  ]\d{3})+|\d+" + (f"|{alt}" if alt else "")
               + r")(?![\d%-]|[.,]\d)")
        self.max_value = int(cfg.get("max_value", 100000))
        self.patterns: dict[str, list[re.Pattern[str]]] = {}
        for field in FIELDS:
            by_lang = cfg.get(field) or {}
            self.patterns[field] = [re.compile(str(t).replace("{n}", num))
                                    for templates in by_lang.values() for t in templates or ()]
        if not any(self.patterns.values()):
            raise ValueError("extract_rules: no casualty patterns")

    def _value(self, raw: str) -> int | None:
        word = self.words.get(raw.casefold())
        if word is not None:
            return word
        try:
            v = int(_SEPARATORS.sub("", raw))
        except ValueError:
            return None
        return v if 0 <= v <= self.max_value else None

    def doc_value(self, field: str, text: str) -> tuple[int, str] | None:
        """(largest number, matched span) for ``field`` in ``text``, or None."""
        norm = unicodedata.normalize("NFC", text or "").replace("’", "'")
        low = norm.casefold()
        src = norm if len(low) == len(norm) else low  # spans index the original when possible
        best: tuple[int, str] | None = None
        for p in self.patterns.get(field, ()):
            for m in p.finditer(low):
                v = self._value(m.group("n"))
                if v is not None and (best is None or v > best[0]):
                    best = (v, src[m.start():m.end()])
        return best


def casualties(event: Mapping[str, Any], docs: Sequence[ExtractDoc], *,
               rules: CasualtyRules) -> list[FieldValue]:
    out: list[FieldValue] = []
    for field, name in FIELDS.items():
        per_domain: dict[str, tuple[int, ExtractDoc, str]] = {}
        for d in docs:
            hit = rules.doc_value(field, d.text)
            if hit is None:
                continue
            cur = per_domain.get(d.domain)
            if cur is None or hit[0] > cur[0]:
                per_domain[d.domain] = (hit[0], d, hit[1])
        if not per_domain:
            continue
        ranked = sorted(per_domain.values(), key=lambda t: (t[0], t[1].published_at,
                                                             t[1].doc_id))
        med = statistics.median_low([t[0] for t in ranked])
        value, doc, quote = next(t for t in ranked if t[0] == med)
        out.append(FieldValue(name, str(value), doc.doc_id, quote))
    return out
