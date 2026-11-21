"""MinHash signatures and an LSH banding index (stdlib only; spec §2 "cheapest filter first").

Text is normalised (NFKC, case-folded, punctuation collapsed to single spaces) and cut into
overlapping character 5-grams, so the method works for any script without tokenisation.
Each shingle is hashed once with blake2b (64 bit). Two signature methods, same output shape
(k 32-bit slots; the estimated Jaccard similarity is the share of equal slots):

* ``"oph"`` (default) - one-permutation hashing with rotation densification (Li, Owen & Zhang
  2012; Shrivastava 2017): the hash picks one of k bins and the bin keeps its minimum;
  empty bins borrow from the next non-empty bin. O(shingles) per document, ~50x faster
  than k separate permutations in pure Python, with the same estimator.
* ``"classic"`` - k permutations from the universal family ``(a*x + b) mod (2^61 - 1)``
  with fixed-seed coefficients, as in datasketch. O(k x shingles).

``LSHIndex`` splits a signature into ``bands`` bands of ``rows`` slots; two items become
candidates when any band is identical. The S-curve threshold is about (1/bands)^(1/rows)
(32 x 4 -> ~0.42).
"""

import hashlib
import random
import re
import unicodedata
from collections import defaultdict
from collections.abc import Hashable, Iterable

MERSENNE_61 = (1 << 61) - 1
MAX_HASH = (1 << 32) - 1
_DENSIFY_C = 0x9E3779B1  # odd offset per densification step
DEFAULT_PERMUTATIONS = 128
DEFAULT_SHINGLE = 5

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(_NON_WORD.sub(" ", text).split())


def shingles(text: str, k: int = DEFAULT_SHINGLE) -> set[str]:
    """Character k-grams of the normalised text (the whole text if shorter than k)."""
    t = normalise(text)
    if not t:
        return set()
    if len(t) <= k:
        return {t}
    return {t[i : i + k] for i in range(len(t) - k + 1)}


def _hash64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "little")


class MinHasher:
    """Builds fixed-length MinHash signatures; same seed -> comparable signatures."""

    def __init__(self, num_perm: int = DEFAULT_PERMUTATIONS, shingle: int = DEFAULT_SHINGLE,
                 seed: int = 1, method: str = "oph") -> None:
        if not 16 <= num_perm <= 512:
            raise ValueError("num_perm must be in 16..512")
        if method not in ("oph", "classic"):
            raise ValueError("method must be 'oph' or 'classic'")
        rng = random.Random(seed)
        self.num_perm = num_perm
        self.shingle = shingle
        self.method = method
        self._perms = [(rng.randrange(1, MERSENNE_61), rng.randrange(0, MERSENNE_61))
                       for _ in range(num_perm)]

    def signature_of(self, items: Iterable[str]) -> tuple[int, ...]:
        hashes = [_hash64(s) for s in items]
        if not hashes:
            return (MAX_HASH,) * self.num_perm
        if self.method == "oph":
            return self._oph(hashes)
        p = MERSENNE_61
        return tuple(min((a * h + b) % p for h in hashes) & MAX_HASH for a, b in self._perms)

    def _oph(self, hashes: list[int]) -> tuple[int, ...]:
        k = self.num_perm
        bins: list[int | None] = [None] * k
        for h in hashes:
            b = h % k
            v = (h >> 32) & MAX_HASH
            cur = bins[b]
            if cur is None or v < cur:
                bins[b] = v
        if None not in bins:
            return tuple(bins)  # type: ignore[arg-type]
        out = list(bins)
        for i in range(k):  # rotation densification: borrow from the next non-empty bin
            if bins[i] is None:
                dist = 1
                while bins[(i + dist) % k] is None:
                    dist += 1
                out[i] = (bins[(i + dist) % k] + dist * _DENSIFY_C) & MAX_HASH
        return tuple(out)  # type: ignore[arg-type]

    def signature(self, text: str) -> tuple[int, ...]:
        return self.signature_of(shingles(text, self.shingle))


def jaccard(a: tuple[int, ...] | list[int], b: tuple[int, ...] | list[int]) -> float:
    """Estimated Jaccard similarity of two equal-length signatures."""
    if len(a) != len(b) or not a:
        raise ValueError("signatures must be non-empty and of equal length")
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def exact_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


class LSHIndex:
    """Banded LSH over MinHash signatures; keys can be removed (bounded memory)."""

    def __init__(self, num_perm: int = DEFAULT_PERMUTATIONS, bands: int = 32) -> None:
        if num_perm % bands:
            raise ValueError("num_perm must be divisible by bands")
        self.bands = bands
        self.rows = num_perm // bands
        self._tables: list[dict[tuple[int, ...], set[Hashable]]] = [
            defaultdict(set) for _ in range(bands)]
        self._keys: dict[Hashable, list[tuple[int, ...]]] = defaultdict(list)

    def _band_keys(self, sig: tuple[int, ...] | list[int]) -> list[tuple[int, ...]]:
        r = self.rows
        return [tuple(sig[i * r : (i + 1) * r]) for i in range(self.bands)]

    def insert(self, key: Hashable, sig: tuple[int, ...] | list[int]) -> None:
        """Add a signature under ``key`` (a key may hold several signatures)."""
        bks = self._band_keys(sig)
        for table, bk in zip(self._tables, bks, strict=True):
            table[bk].add(key)
        self._keys[key].extend(bks)

    def query(self, sig: tuple[int, ...] | list[int]) -> set[Hashable]:
        out: set[Hashable] = set()
        for table, bk in zip(self._tables, self._band_keys(sig), strict=True):
            hit = table.get(bk)
            if hit:
                out |= hit
        return out

    def remove(self, key: Hashable) -> None:
        bks = self._keys.pop(key, None)
        if not bks:
            return
        for i, bk in enumerate(bks):
            table = self._tables[i % self.bands]
            bucket = table.get(bk)
            if bucket is not None:
                bucket.discard(key)
                if not bucket:
                    del table[bk]

    def __contains__(self, key: Hashable) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)
