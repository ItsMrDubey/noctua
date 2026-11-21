"""Common words that are also place names, learnt from our own document corpus.

GeoNames alternate names include everyday words ("كيف" = "how" is listed for Kyiv; English
"Nice", "Reading"; French "Tours"). ``build_common_tokens`` computes, per language, the
document frequency of normalised tokens over recent lake documents (title + snippet) and
keeps gazetteer name keys that are frequent (>= ``min_share`` of the language's documents
and >= ``min_docs`` documents) *and* behave like words rather than places:

* cased scripts: the token often appears lower-case (place names are capitalised);
* uncased scripts (Arabic, Persian, ...): the token does not co-occur with the country of
  its candidate places much more than the language's base rate ("كييف" co-occurs with
  Ukraine, "كيف" does not).

The result is written to ``data/gazetteer/common_tokens.json``; a tiny committed seed
(``data/ambiguous_seed.json`` in this package) covers known cases before that file exists.
Regenerate with ``python -m osint.enrich.ambiguity``.
"""

import json
import logging
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from osint.enrich.gazetteer import Gazetteer

log = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "data" / "ambiguous_seed.json"
COMMON_TOKENS_FILE = "common_tokens.json"
MIN_SHARE = 0.005
MIN_DOCS = 20
MAX_DOCS = 50_000
LOWER_SHARE = 0.3  # cased scripts: a word if >= this share of its documents use it lower-case
_COUNTRY_NGRAM = 4


def is_latin(token: str) -> bool:
    return all(ch.isascii() or "LATIN" in unicodedata.name(ch, "") for ch in token
               if ch.isalpha())


def load_seed(path: Path = SEED_PATH) -> dict[str, frozenset[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {lang: frozenset(words) for lang, words in data.items() if not lang.startswith("_")}


class CommonTokens:
    """Folded tokens that are common words, per language."""

    def __init__(self, by_lang: dict[str, frozenset[str]] | None = None) -> None:
        self.by_lang = dict(by_lang or {})
        self._any = frozenset().union(*self.by_lang.values()) if self.by_lang else frozenset()

    def is_common(self, token: str, language: str = "") -> bool:
        if not language:
            return token in self._any
        return token in self.by_lang.get(language, ())

    def merged(self, other: "CommonTokens") -> "CommonTokens":
        langs = set(self.by_lang) | set(other.by_lang)
        return CommonTokens({lang: self.by_lang.get(lang, frozenset())
                             | other.by_lang.get(lang, frozenset()) for lang in langs})

    @classmethod
    def load(cls, path: Path) -> "CommonTokens":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls({lang: frozenset(v.get("tokens", []))
                    for lang, v in data.get("languages", {}).items()})

    @classmethod
    def seed(cls) -> "CommonTokens":
        return cls(load_seed())

    @classmethod
    def default(cls, src_dir: Path | None = None) -> "CommonTokens":
        """The seed plus ``<src_dir>/common_tokens.json`` when it exists and is readable."""
        common = cls.seed()
        path = src_dir / COMMON_TOKENS_FILE if src_dir is not None else None
        if path is not None and path.exists():
            try:
                common = common.merged(cls.load(path))
            except (OSError, ValueError):
                log.warning("unreadable %s; using the seed only", path, exc_info=True)
        return common


# ------------------------------------------------------------------ building

def _country_keys(gz: "Gazetteer") -> dict[str, frozenset[str]]:
    """Name key -> countries it names (country names, aliases, demonyms)."""
    out: dict[str, set[str]] = defaultdict(set)
    for key, cands in gz.names.items():
        for i, _ in cands:
            p = gz.places[i]
            if p.precision == "country" and p.country:
                out[key].add(p.country)
    return {k: frozenset(v) for k, v in out.items()}


def _doc_countries(folded: list[str], country_keys: dict[str, frozenset[str]]) -> set[str]:
    found: set[str] = set()
    for n in range(1, _COUNTRY_NGRAM + 1):
        for i in range(len(folded) - n + 1):
            ccs = country_keys.get(" ".join(folded[i:i + n]))
            if ccs:
                found |= ccs
    return found


def compute_common_tokens(
    rows: list[tuple[str, str, str]], gz: "Gazetteer", *, min_share: float = MIN_SHARE,
    min_docs: int = MIN_DOCS,
) -> dict[str, dict[str, Any]]:
    """rows: (language, title, snippet). Returns {language: {"docs": n, "tokens": [...]}}."""
    from osint.enrich.gazetteer import tokens

    country_keys = _country_keys(gz)
    n_docs: Counter[str] = Counter()
    df: dict[str, Counter[str]] = defaultdict(Counter)
    lower_df: dict[str, Counter[str]] = defaultdict(Counter)
    base: dict[str, Counter[str]] = defaultdict(Counter)  # lang -> country -> docs
    docs: list[tuple[str, set[str], set[str]]] = []  # (lang, tokens, countries)
    for lang, title, snippet in rows:
        if not lang:
            continue
        toks = tokens(f"{title or ''}. {snippet or ''}")
        folded = [t for t, _ in toks]
        uniq = set(folded)
        lowered = {t for t, o in toks if o != o.upper() and o[:1].islower()}
        countries = _doc_countries(folded, country_keys)
        n_docs[lang] += 1
        df[lang].update(uniq)
        lower_df[lang].update(lowered)
        base[lang].update(countries)
        docs.append((lang, uniq, countries))

    # frequent gazetteer keys that are not country names, per language
    frequent: dict[str, set[str]] = {}
    for lang, counts in df.items():
        threshold = max(min_docs, min_share * n_docs[lang])
        frequent[lang] = {t for t, c in counts.items() if c >= threshold and t in gz.names
                          and t not in country_keys}
    cand_countries = {t: frozenset(gz.places[i].country for i, _ in gz.names[t])
                      for ts in frequent.values() for t in ts}
    co: dict[str, Counter[str]] = defaultdict(Counter)
    for lang, uniq, countries in docs:
        for t in frequent[lang] & uniq:
            if countries & cand_countries[t]:
                co[lang][t] += 1

    out: dict[str, dict[str, Any]] = {}
    for lang, ts in frequent.items():
        common = []
        for t in ts:
            n = df[lang][t]
            sample = next(iter(t))
            if sample.upper() != sample.lower():  # cased script: case says word vs name
                is_word = lower_df[lang][t] / n >= LOWER_SHARE
            else:  # uncased: a place co-occurs with its country well above the base rate
                b = max((base[lang][cc] for cc in cand_countries[t]), default=0) / n_docs[lang]
                rate = co[lang][t] / n
                is_word = rate < max(0.15, min(3 * b, b + 0.25))
            if is_word:
                common.append(t)
        out[lang] = {"docs": n_docs[lang], "tokens": sorted(common)}
    for lang in n_docs:
        out.setdefault(lang, {"docs": n_docs[lang], "tokens": []})
    return out


def build_common_tokens(lake: Any, out_path: Path, *, gazetteer: "Gazetteer | None" = None,
                        max_docs: int = MAX_DOCS, days: int = 30,
                        min_share: float = MIN_SHARE, min_docs: int = MIN_DOCS) -> Path:
    """Compute common tokens over up to ``max_docs`` recent lake documents and write them
    to ``out_path`` (JSON). ``lake`` needs the public ``Lake.query`` API."""
    if gazetteer is None:
        from osint.enrich.gazetteer import Gazetteer

        gazetteer = Gazetteer.load(out_path.parent)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = lake.query(
        "SELECT language, title, snippet, published_at FROM documents "
        "WHERE published_at >= ? AND kind <> 'gdelt_event' "
        "ORDER BY published_at DESC LIMIT ?", [since, max_docs], start=since)
    languages = compute_common_tokens([(r[0], r[1], r[2]) for r in rows[:max_docs]], gazetteer,
                                      min_share=min_share, min_docs=min_docs)
    data = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "docs": min(len(rows), max_docs), "min_share": min_share, "min_docs": min_docs,
            "languages": languages}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    return out_path


def main() -> None:
    from osint.config import Settings
    from osint.store.lake import Lake

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    lake = Lake(settings.lake_dir)
    try:
        out = build_common_tokens(lake, settings.data_dir / "gazetteer" / COMMON_TOKENS_FILE)
    finally:
        lake.close()
    data = json.loads(out.read_text(encoding="utf-8"))
    for lang, v in sorted(data["languages"].items()):
        log.info("%s: %d docs, %d common tokens", lang, v["docs"], len(v["tokens"]))


if __name__ == "__main__":
    main()
