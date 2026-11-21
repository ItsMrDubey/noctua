"""Free local triage (pipeline stage 2): language, relevance, categories and places.

Pure CPU and deterministic; runs inside the ingest worker thread on every new document.
The lexicons live in config/triage.yaml (see the syntax notes there).
"""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from osint.config import ROOT
from osint.core.geo import valid_coord
from osint.core.models import Document, PlaceHit, TriageResult
from osint.enrich.gazetteer import LazyGazetteer, best_place, fold

DEFAULT_CONFIG = ROOT / "config" / "triage.yaml"
DEFAULT_GAZETTEER_DIR = ROOT / "data" / "gazetteer"

# ---------------------------------------------------------------------- language

def _words(text: str) -> list[str]:
    return text.split()


_STOPWORDS = {
    "en": _words("the of and to in is for on that with was as by at from are this have has it be"),
    "es": _words("el la los las de del que y en un una por para con se su al es lo como más"),
    "fr": _words("le la les des du de et en un une est pour que qui dans sur au aux par pas avec"),
    "de": _words("der die das und ist nicht ein eine den dem des mit von zu auf für im sich auch"),
    "pt": _words("o os as do da dos das que em um uma para com não por se no na é ao"),
    "it": _words("il lo gli della di che e un una per con non sono nel alla anche del le si"),
}
_STOPSETS = {lang: frozenset(words) for lang, words in _STOPWORDS.items()}
_PERSIAN_ONLY = set("پچژگکی")  # letters (almost) never used in Arabic text
_ARABIC_ONLY = set("ةيكى")  # Arabic forms that Persian writes differently
_UKRAINIAN_ONLY = set("іїєґ")
_RUSSIAN_ONLY = set("ыэъё")
_TURKISH_ONLY = set("ğışİ")
_WORD = re.compile(r"[^\W\d_]+")


def detect_language(text: str) -> str:
    """Script heuristic with stopword evidence for Latin text. Returns ISO 639-1 or ""."""
    counts: dict[str, int] = {}
    letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFEFF:
            script = "arabic"
        elif 0x0590 <= o <= 0x05FF:
            script = "he"
        elif 0x0400 <= o <= 0x04FF:
            script = "cyrillic"
        elif 0x3040 <= o <= 0x30FF:
            script = "ja"
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            script = "han"
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            script = "ko"
        elif 0x0370 <= o <= 0x03FF:
            script = "el"
        elif 0x0E00 <= o <= 0x0E7F:
            script = "th"
        elif 0x0900 <= o <= 0x097F:
            script = "hi"
        else:
            script = "latin"
        counts[script] = counts.get(script, 0) + 1
    if not letters:
        return ""
    non_latin = {k: v for k, v in counts.items() if k != "latin"}
    if non_latin:
        script, n = max(non_latin.items(), key=lambda kv: kv[1])
        if n >= 0.2 * letters:
            if script == "arabic":
                fa = sum(text.count(c) for c in _PERSIAN_ONLY)
                ar = sum(text.count(c) for c in _ARABIC_ONLY)
                return "fa" if fa > ar else "ar"
            if script == "cyrillic":
                lower = text.casefold()
                uk = sum(lower.count(c) for c in _UKRAINIAN_ONLY)
                ru = sum(lower.count(c) for c in _RUSSIAN_ONLY)
                return "uk" if uk > ru else "ru"
            if script == "han":
                return "ja" if counts.get("ja", 0) else "zh"
            return script
    if sum(text.count(c) for c in _TURKISH_ONLY) >= 2:
        return "tr"
    words = [w.casefold() for w in _WORD.findall(text)]
    hits = {lang: sum(w in stops for w in words) for lang, stops in _STOPSETS.items()}
    en = hits.pop("en")
    lang, n = max(hits.items(), key=lambda kv: kv[1])
    if n >= 2 and n > en * 1.5:
        return lang
    return "en"


# ---------------------------------------------------------------------- lexicons

_AR_CLITICS = ("وال", "بال", "فال", "كال", "لل", "ال", "و", "ف", "ب", "ل", "ك")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_TOK = re.compile(r"[^\W_]+")
_PREFIX_KEY = 3  # prefix terms are indexed by their first characters


def _tokens(text: str) -> list[str]:
    return _TOK.findall(fold(text))


def _flatten(block: Any) -> list[str]:
    """A term block is either a list or {language: list}."""
    if not block:
        return []
    if isinstance(block, dict):
        return [str(t) for terms in block.values() for t in (terms or [])]
    return [str(t) for t in block]


@dataclass(frozen=True, slots=True)
class _Term:
    tid: int
    group: str  # "mask" | "off_strong" | "off_weak" | "<category>:strong|weak|negative"
    parts: tuple[tuple[str, bool], ...]  # (token, is_prefix)


class Lexicon:
    """Token-indexed phrase matcher: one pass over the tokens, dictionary lookups only.

    Whole-word tokens match exactly, ``word*`` tokens by prefix (a ``*`` on a phrase applies
    to its last word; ``ракетн* удар*`` marks each word). Arabic-script tokens are also tried
    without a leading clitic. CJK terms match as substrings of the folded text.
    """

    def __init__(self) -> None:
        self.terms: list[_Term] = []
        self.exact: dict[str, list[_Term]] = {}
        self.prefix: dict[str, list[_Term]] = {}
        self.short_prefix: list[_Term] = []
        self.cjk: list[tuple[str, _Term]] = []

    def add(self, group: str, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        if _CJK.search(raw):
            text = "".join(_tokens(raw.rstrip("*")))
            if text:
                term = _Term(len(self.terms), group, ((text, False),))
                self.terms.append(term)
                self.cjk.append((text, term))
            return
        words = raw.split()
        parts: list[tuple[str, bool]] = []
        for k, w in enumerate(words):
            star = w.endswith("*") or (k == len(words) - 1 and raw.endswith("*"))
            toks = _tokens(w.rstrip("*"))
            for j, t in enumerate(toks):  # "b-52" -> ("b", "52")
                parts.append((t, star and j == len(toks) - 1))
        if not parts:
            return
        term = _Term(len(self.terms), group, tuple(parts))
        self.terms.append(term)
        first, is_prefix = parts[0]
        if not is_prefix:
            self.exact.setdefault(first, []).append(term)
        elif len(first) >= _PREFIX_KEY:
            self.prefix.setdefault(first[:_PREFIX_KEY], []).append(term)
        else:
            self.short_prefix.append(term)

    @staticmethod
    def _tok_ok(tok: str, part: tuple[str, bool]) -> bool:
        return tok.startswith(part[0]) if part[1] else tok == part[0]

    def _candidates(self, tok: str) -> list[_Term]:
        out = list(self.exact.get(tok, ()))
        out += [t for t in self.prefix.get(tok[:_PREFIX_KEY], ()) if tok.startswith(t.parts[0][0])]
        out += [t for t in self.short_prefix if tok.startswith(t.parts[0][0])]
        return out

    def find(self, text: str) -> set[int]:
        """Ids of (non-mask) terms found in ``text``. Mask terms consume their tokens first."""
        toks = _tokens(text)
        hits: set[int] = set()
        masked = [False] * len(toks)
        for mask_pass in (True, False):
            for i, tok in enumerate(toks):
                if masked[i]:
                    continue
                firsts = [tok]
                if "؀" <= tok[0] <= "ۿ":
                    firsts += [tok[len(c):] for c in _AR_CLITICS
                               if tok.startswith(c) and len(tok) - len(c) >= 2]
                for variant in firsts:
                    for term in self._candidates(variant):
                        if (term.group == "mask") != mask_pass:
                            continue
                        n = len(term.parts)
                        if i + n > len(toks) or any(masked[i + 1:i + n]):
                            continue
                        if all(self._tok_ok(toks[i + k], term.parts[k]) for k in range(1, n)):
                            if mask_pass:
                                for k in range(i, i + n):
                                    masked[k] = True
                            else:
                                hits.add(term.tid)
        if self.cjk:
            joined = "".join(t for t, m in zip(toks, masked, strict=True) if not m)
            if _CJK.search(joined):
                hits.update(term.tid for sub, term in self.cjk if sub in joined)
        return hits

class Matcher(Protocol):
    def match(self, text: str, language: str = "", **kw: Any) -> list[PlaceHit]: ...


@dataclass
class TriageConfig:
    threshold: float = 0.35
    relevant: float = 0.35
    weak_weight: float = 0.35
    negative_weight: float = 0.7
    snippet_weight: float = 0.6
    place_bonus: float = 0.05
    off_topic_penalty: float = 0.25
    off_topic_trigger: float = 1.0
    cameo_roots: dict[str, str] = field(default_factory=dict)
    cameo_overrides: dict[str, str] = field(default_factory=dict)
    gdelt_quad_relevance: dict[str, float] = field(default_factory=dict)


# GDELT ActionGeo_Type -> precision
GDELT_GEO_PRECISION = {1: "country", 2: "admin1", 3: "city", 4: "city", 5: "admin1"}


class Triage:
    def __init__(self, cfg: dict[str, Any], gazetteer: Matcher | None = None) -> None:
        scalars = {k: cfg[k] for k in ("threshold", "relevant", "weak_weight", "negative_weight",
                                       "snippet_weight", "place_bonus", "off_topic_penalty",
                                       "off_topic_trigger") if k in cfg}
        self.cfg = TriageConfig(
            **{k: float(v) for k, v in scalars.items()},
            cameo_roots={str(k): str(v) for k, v in (cfg.get("cameo_roots") or {}).items()},
            cameo_overrides={str(k): str(v)
                             for k, v in (cfg.get("cameo_overrides") or {}).items()},
            gdelt_quad_relevance={str(k): float(v) for k, v in
                                  (cfg.get("gdelt_quad_relevance") or {}).items()},
        )
        self.gazetteer = gazetteer
        self.lexicon = Lexicon()
        for t in _flatten(cfg.get("mask")):
            self.lexicon.add("mask", t)
        off = cfg.get("off_topic") or {}
        for kind in ("strong", "weak"):
            for t in _flatten(off.get(kind)):
                self.lexicon.add(f"off_{kind}", t)
        self.category_names: list[str] = []
        for name, c in (cfg.get("categories") or {}).items():
            self.category_names.append(name)
            for kind in ("strong", "weak", "negative"):
                for t in _flatten((c or {}).get(kind)):
                    self.lexicon.add(f"{name}:{kind}", t)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG,
                  gazetteer: Matcher | None = None) -> "Triage":
        with path.open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {}, gazetteer)

    @classmethod
    def default(cls, config_path: Path = DEFAULT_CONFIG,
                gazetteer_dir: Path = DEFAULT_GAZETTEER_DIR) -> "Triage":
        return cls.from_file(config_path, LazyGazetteer(gazetteer_dir))

    def preload(self) -> None:
        """Load the (lazy) gazetteer now; blocking, so call it from a worker thread."""
        get = getattr(self.gazetteer, "get", None)
        if get is not None:
            get()

    # ------------------------------------------------------------------ scoring

    def score_text(self, title: str, snippet: str) -> dict[str, float]:
        """Per-category scores in 0..1 for a title + snippet."""
        in_title = self.lexicon.find(title)
        in_snippet = self.lexicon.find(snippet) - in_title
        weights: dict[str, float] = {}
        for ids, w in ((in_title, 1.0), (in_snippet, self.cfg.snippet_weight)):
            for tid in ids:
                group = self.lexicon.terms[tid].group
                weights[group] = weights.get(group, 0.0) + w
        found = in_title | in_snippet  # off-topic context counts wherever it appears
        groups = [self.lexicon.terms[tid].group for tid in found]
        off = groups.count("off_strong") + 0.5 * groups.count("off_weak")
        penalty = self.cfg.off_topic_penalty if off >= self.cfg.off_topic_trigger else 1.0
        scores: dict[str, float] = {}
        for name in self.category_names:
            raw = (weights.get(f"{name}:strong", 0.0)
                   + self.cfg.weak_weight * weights.get(f"{name}:weak", 0.0)
                   - self.cfg.negative_weight * weights.get(f"{name}:negative", 0.0))
            if raw > 0:
                scores[name] = round((1 - math.exp(-raw)) * penalty, 4)
        return scores

    def _gdelt(self, doc: Document, scores: dict[str, float]) -> tuple[dict[str, float],
                                                                       list[PlaceHit]]:
        a = doc.attrs
        code, root = str(a.get("event_code") or ""), str(a.get("event_root_code") or "")
        cat = self.cfg.cameo_overrides.get(code) or self.cfg.cameo_overrides.get(code[:3]) \
            or self.cfg.cameo_roots.get(root)
        if cat:
            base = self.cfg.gdelt_quad_relevance.get(str(a.get("quad_class") or ""), 0.2)
            try:
                articles = max(int(a.get("num_articles") or 1), 1)
            except (TypeError, ValueError):
                articles = 1
            coded = min(1.0, base + 0.05 * math.log2(articles))
            scores[cat] = round(max(scores.get(cat, 0.0), coded), 4)
        places: list[PlaceHit] = []
        lat, lon = a.get("geo_lat"), a.get("geo_lon")
        try:
            geo_type = int(a.get("geo_type") or 0)
        except (TypeError, ValueError):
            geo_type = 0
        if isinstance(lat, int | float) and isinstance(lon, int | float) and geo_type \
                and valid_coord(lat, lon) and not (lat == 0 and lon == 0):
            precision = GDELT_GEO_PRECISION.get(geo_type, "country")
            places.append(PlaceHit(
                name=str(a.get("geo_name") or ""), country=str(a.get("geo_country") or ""),
                lat=float(lat), lon=float(lon), precision=precision,
                confidence={"city": 0.7, "admin1": 0.6}.get(precision, 0.5),
                matched="gdelt:ActionGeo", feature=f"gdelt:{geo_type}"))
        return scores, places

    def assess(self, doc: Document) -> TriageResult:
        language = doc.language or detect_language(f"{doc.title} {doc.snippet}")
        scores = self.score_text(doc.title, doc.snippet)
        if doc.kind == "gdelt_event":
            scores, places = self._gdelt(doc, scores)
        elif self.gazetteer is not None:
            hint = str(doc.attrs.get("country") or "")
            places = self.gazetteer.match(f"{doc.title}. {doc.snippet}", language,
                                          country_hint=hint)
        else:
            places = []
        best = best_place(places)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        categories = [c for c, s in ranked if s >= self.cfg.threshold]
        relevance = ranked[0][1] if ranked else 0.0
        if relevance > 0 and best is not None and best.confidence >= 0.65:
            relevance = min(1.0, relevance + self.cfg.place_bonus)
        return TriageResult(language=language, relevance=round(relevance, 4),
                            categories=categories, places=places, best_place=best,
                            scores=scores)


def is_relevant(result: TriageResult, threshold: float) -> bool:
    return result.relevance >= threshold
