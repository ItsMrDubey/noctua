"""Headline and summary rules (deterministic extraction spec §2.4): the title of the
document nearest the event centroid (embeddings), else the primary document's, else the
earliest; the summary is that document's snippet cut at a sentence boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from osint.enrich.rules.base import QUOTE_MAX, ExtractDoc, FieldValue

SUMMARY_MAX = 280
_SENT_END = re.compile(r"[.!?؟](?=\s|$)|[。！？]")
_DOTTED = re.compile(r"(?:\w\.)+\w")  # u.s, p.m, e.g, d.c (terminator already stripped)
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "gen", "lt", "col", "sgt", "capt", "cpt", "cmdr",
    "maj", "adm", "gov", "sen", "rep", "st", "mt", "ft", "no", "nos", "vs", "etc", "inc",
    "ltd", "co", "corp", "jr", "sr", "hon", "rev", "pres", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "approx", "dept", "est", "fig",
    "u.s", "u.k", "u.n", "d.c", "e.u",
})


def _cosine(a: Any, b: Any) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    den = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / den if den else -1.0


def pick_document(event: Mapping[str, Any], docs: Sequence[ExtractDoc]) -> ExtractDoc | None:
    """The document whose vector is nearest ``event['vector']`` (the cluster centroid);
    without vectors the primary document, else the earliest. Documents without a title
    are never picked."""
    cands = [d for d in docs if (d.title or "").strip()]
    if not cands:
        return None
    centroid = event.get("vector")
    if centroid is not None:
        scored = [(_cosine(d.vector, centroid), d) for d in cands if d.vector is not None]
        if scored:
            return min(scored, key=lambda t: (-t[0], t[1].published_at, t[1].doc_id))[1]
    primary = [d for d in cands if d.role == "primary"]
    return min(primary or cands, key=lambda d: (d.published_at, d.doc_id))


def _valid_end(t: str, m: re.Match[str]) -> bool:
    """A terminator is a sentence end unless it closes an abbreviation, an initial or a
    dotted acronym, or the next word starts in lowercase ("3 p.m. local time")."""
    nxt = t[m.end():].lstrip()
    if nxt and nxt[0].islower():
        return False
    if m.group() != ".":
        return True
    token = t[:m.start()].rsplit(None, 1)[-1].lstrip("([\"'“‘").lower() if t[:m.start()].strip() else ""
    if token in _ABBREVIATIONS or len(token) == 1 and token.isalpha():
        return False
    return not _DOTTED.fullmatch(token)


def cut_summary(text: str, max_chars: int = SUMMARY_MAX) -> str:
    """Whitespace-collapsed ``text`` of at most ``max_chars``: cut at the last real sentence
    end that fits (abbreviations, initials and dotted acronyms do not end a sentence), else
    whole words and an ellipsis."""
    t = " ".join((text or "").split())
    if len(t) <= max_chars:
        return t
    window = t[:max_chars + 1]
    ends = [m.end() for m in _SENT_END.finditer(window)
            if m.end() <= max_chars and _valid_end(t, m)]
    if ends:
        return t[:ends[-1]]
    cut = t[:max_chars - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-–—") + "…"


def headline_summary(event: Mapping[str, Any], docs: Sequence[ExtractDoc], *,
                     max_chars: int = SUMMARY_MAX) -> list[FieldValue]:
    d = pick_document(event, docs)
    if d is None:
        return []
    title = " ".join(d.title.split())
    out = [FieldValue("headline", title, d.doc_id, title[:QUOTE_MAX])]
    summary = cut_summary(d.snippet, max_chars)
    if summary:
        out.append(FieldValue("summary", summary, d.doc_id, summary[:QUOTE_MAX]))
    return out
