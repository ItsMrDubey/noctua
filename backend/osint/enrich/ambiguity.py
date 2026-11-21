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

def compute_common_tokens(
    rows: list[tuple[str, str, str]], gz: "Gazetteer", *, min_share: float = MIN_SHARE,
    min_docs: int = MIN_DOCS,
) -> dict[str, dict[str, Any]]:
    """rows: (language, title, snippet). Returns {language: {"docs": n, "tokens": [...]}}."""
    from osint.enrich.gazetteer import _CJK_RE, Gazetteer, name_key, tokens

    plain = Gazetteer(gz.places, gz.names, common=CommonTokens())  # no word filtering
    n_docs: Counter[str] = Counter()
    df: dict[str, Counter[str]] = defaultdict(Counter)
    lower_df: dict[str, Counter[str]] = defaultdict(Counter)
    # (lang, tokens, {(matched key, country)} of the places the text mentions)
    docs: list[tuple[str, set[str], set[tuple[str, str]]]] = []
    for lang, title, snippet in rows:
        if not lang:
            continue
        text = f"{title or ''}. {snippet or ''}"
        toks = tokens(text)
        uniq = {t for t, _ in toks}
        lowered = {t for t, o in toks if o != o.upper() and o[:1].islower()}
        mentions = {(name_key(h.matched), h.country)
                    for h in plain.match(text, lang, limit=100) if h.country}
        n_docs[lang] += 1
        df[lang].update(uniq)
        lower_df[lang].update(lowered)
        docs.append((lang, uniq, mentions))

    out: dict[str, dict[str, Any]] = {}
    for lang, counts in df.items():
        threshold = max(min_docs, min_share * n_docs[lang])
        # CJK has no word boundaries (matched by character windows): no word statistics
        frequent = [t for t, c in counts.items()
                    if c >= threshold and t in gz.names and not _CJK_RE.search(t)
                    and not any(gz.places[i].precision == "country" for i, _ in gz.names[t])]
        lang_docs = [d for d in docs if d[0] == lang]
        doubtful = set(frequent)  # their own mentions are not evidence for each other
        common = []
        for t in frequent:
            n = counts[t]
            if t.upper() != t.lower():  # cased script: case tells a word from a name
                if lower_df[lang][t] / n >= LOWER_SHARE:
                    common.append(t)
                continue
            # uncased script: a place name co-occurs with (other places of) its country
            # clearly more often than texts without it do; a word does not
            ccs = {gz.places[i].country for i, _ in gz.names[t]}
            with_t = without_t = hit_with = hit_without = 0
            for _, uniq, mentions in lang_docs:
                hit = any(cc in ccs for k, cc in mentions if k not in doubtful)
                if t in uniq:
                    with_t += 1
                    hit_with += hit
                else:
                    without_t += 1
                    hit_without += hit
            co = hit_with / with_t if with_t else 0.0
            base = hit_without / without_t if without_t else 0.0
            if co < 1.5 * base + 0.05:
                common.append(t)
        out[lang] = {"docs": n_docs[lang], "tokens": sorted(common)}
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
        "SELECT language, title, snippet FROM documents "
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
