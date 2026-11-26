"""Whole-word phrase lookup on gazetteer-folded tokens (``osint.enrich.gazetteer.tokens``):
case-, diacritic- and punctuation-insensitive ("Côte d'Ivoire" -> cote d ivoire, "U.S." ->
u s). At each position the longest phrase wins and matches never overlap. A trailing ``*``
makes a phrase's last word a prefix ("беспилотник*" matches "беспилотников"). The first
payload added for a phrase is kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from osint.enrich.gazetteer import tokens


@dataclass(frozen=True, slots=True)
class PhraseHit[T]:
    payload: T
    start: int  # first token index
    end: int  # one past the last token
    text: str  # the matched words as written in the text


class PhraseMatcher[T]:
    def __init__(self) -> None:
        # first word -> [(words, last word is a prefix, payload)]
        self._by_first: dict[str, list[tuple[tuple[str, ...], bool, T]]] = {}
        self._prefix_words: list[tuple[str, T]] = []  # one-word prefix phrases
        self._keys: set[tuple[tuple[str, ...], bool]] = set()

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, phrase: str, payload: T) -> bool:
        """Add ``phrase``; False when it is empty or already present (first one wins)."""
        raw = (phrase or "").strip()
        prefix = raw.endswith("*")
        words = tuple(t for t, _ in tokens(raw.rstrip("*")))
        if not words or (words, prefix) in self._keys:
            return False
        self._keys.add((words, prefix))
        if prefix and len(words) == 1:
            self._prefix_words.append((words[0], payload))
            self._prefix_words.sort(key=lambda p: -len(p[0]))
        else:
            self._by_first.setdefault(words[0], []).append((words, prefix, payload))
        return True

    @staticmethod
    def _matches(folded: list[str], i: int, words: tuple[str, ...], prefix: bool) -> bool:
        n = len(words)
        if i + n > len(folded):
            return False
        for k in range(n):
            tok, w = folded[i + k], words[k]
            if prefix and k == n - 1:
                if not tok.startswith(w):
                    return False
            elif tok != w:
                return False
        return True

    def find(self, text: str) -> list[PhraseHit[T]]:
        pairs = tokens(text or "")
        folded = [t for t, _ in pairs]
        out: list[PhraseHit[T]] = []
        i = 0
        while i < len(folded):
            best: tuple[int, T] | None = None
            for words, prefix, payload in self._by_first.get(folded[i], ()):
                if (best is None or len(words) > best[0]) and self._matches(
                        folded, i, words, prefix):
                    best = (len(words), payload)
            if best is None:
                for stem, payload in self._prefix_words:
                    if folded[i].startswith(stem):
                        best = (1, payload)
                        break
            if best is None:
                i += 1
                continue
            n, payload = best
            originals: list[str] = []
            for _, orig in pairs[i:i + n]:  # "U.S" yields two tokens of one original
                if not originals or originals[-1] is not orig:
                    originals.append(orig)
            out.append(PhraseHit(payload, i, i + n, " ".join(originals)))
            i += n
        return out
