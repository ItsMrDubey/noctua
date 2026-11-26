"""Deterministic rule extractors (deterministic extraction spec §2.4).

Each extractor is a pure function ``(event, docs) -> list[FieldValue]``; the values are
written to ``event_fields`` with ``source='rule'`` and the matched span as ``quote``
(``osint.pipeline.rule_pass``). Configuration: ``config/extract_rules.yaml``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from osint.config import ROOT
from osint.enrich.rules.base import ExtractDoc, FieldValue
from osint.enrich.rules.text import SUMMARY_MAX, headline_summary
from osint.enrich.rules.weapons import WeaponLexicon, weapons

log = logging.getLogger(__name__)

DEFAULT_RULES = ROOT / "config" / "extract_rules.yaml"
RULE_FIELDS = ("headline", "summary", "casualties.killed", "casualties.injured",
               "weapons_platforms")
Extractor = Callable[[Mapping[str, Any], Sequence[ExtractDoc]], list[FieldValue]]


@dataclass
class RuleSet:
    summary_max_chars: int
    weapons: WeaponLexicon

    @classmethod
    def load(cls, path: Path = DEFAULT_RULES) -> RuleSet:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        lex = WeaponLexicon(cfg.get("weapons") or {}, subsumed=cfg.get("weapons_subsumed"),
                            ignore=cfg.get("weapons_ignore") or ())
        if not len(lex):
            raise ValueError(f"{path}: empty weapons lexicon")
        return cls(summary_max_chars=int(cfg.get("summary_max_chars", SUMMARY_MAX)),
                   weapons=lex)

    def extractors(self) -> list[tuple[str, Extractor]]:
        return [("headline_summary", partial(headline_summary,
                                             max_chars=self.summary_max_chars)),
                ("weapons", partial(weapons, lexicon=self.weapons))]

    def run(self, event: Mapping[str, Any], docs: Sequence[ExtractDoc]
            ) -> tuple[list[FieldValue], list[str]]:
        """Every extractor's values; one that raises is skipped (and logged) for this
        event only. Returns (values, names of the extractors that failed)."""
        out: list[FieldValue] = []
        failed: list[str] = []
        for name, fn in self.extractors():
            try:
                out += fn(event, docs)
            except Exception:
                log.exception("rule extractor %s failed for event %s", name, event.get("id"))
                failed.append(name)
        return out, failed
