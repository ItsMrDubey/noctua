"""Actor lexicon and actor-country resolution (deterministic extraction spec §2.3).

``config/actors.yaml`` maps actor phrases to a home country (ISO2) and an actor class:
hand-written ``entries`` (ACLED / UCDP side names, armed forces, agencies), non-state
groups from ``config/terror_groups.yaml`` with a home country (``terror_group_homes``), and
phrases generated for every country in ``config/countries.yaml`` ("<demonym> forces",
"Government of <country>", ...). Matching is whole-word on gazetteer-folded text and the
longest phrase wins (``osint.enrich.phrases``). A lexicon that fails to load raises
``LexiconError``: a silent empty lexicon would make every event look domestic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from osint.config import ROOT
from osint.core import countries
from osint.enrich.gazetteer import name_key
from osint.enrich.phrases import PhraseHit, PhraseMatcher

log = logging.getLogger(__name__)

DEFAULT_ACTORS = ROOT / "config" / "actors.yaml"
DEFAULT_GROUPS = ROOT / "config" / "terror_groups.yaml"
CLASSES = frozenset({"state_military", "state_police", "state_government", "non_state_armed",
                     "protesters", "rioters", "civilians", "other"})
CLASS_NOUN = {"state_military": "forces", "state_police": "police",
              "state_government": "government", "protesters": "protesters",
              "rioters": "rioters", "civilians": "civilians"}


class LexiconError(RuntimeError):
    """The actor lexicon could not be loaded (fatal at startup)."""


@dataclass(frozen=True, slots=True)
class ActorEntry:
    name: str  # canonical actor name ("Russian forces")
    country: str | None  # home country, ISO2
    klass: str  # one of CLASSES


class ActorLexicon:
    def __init__(self, matcher: PhraseMatcher[ActorEntry]) -> None:
        if not len(matcher):
            raise LexiconError("actor lexicon is empty")
        self.matcher = matcher

    def __len__(self) -> int:
        return len(self.matcher)

    @classmethod
    def load(cls, path: Path = DEFAULT_ACTORS, *,
             groups_path: Path = DEFAULT_GROUPS) -> ActorLexicon:
        """Hand-written entries first, then terror-group homes, then generated phrases
        (the first entry of a phrase wins)."""
        try:
            cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise LexiconError(f"actor lexicon {path}: {e}") from e
        if not isinstance(cfg, dict):
            raise LexiconError(f"actor lexicon {path}: not a mapping")
        m: PhraseMatcher[ActorEntry] = PhraseMatcher()
        for source in (_entries(cfg, Path(path)), _terror_groups(cfg, Path(groups_path)),
                       _generated(cfg)):
            for phrase, entry in source:
                m.add(phrase, entry)
        return cls(m)

    def find(self, text: str) -> list[PhraseHit[ActorEntry]]:
        return self.matcher.find(text)

    def lookup(self, name: str) -> ActorEntry | None:
        """The entry of the longest phrase in an actor name ("Military Forces of Sudan
        (2019-)" -> Sudanese forces, SD), or None."""
        hits = self.find(name)
        if not hits:
            return None
        return max(hits, key=lambda h: (h.end - h.start, -h.start)).payload


@lru_cache(maxsize=1)
def default_lexicon() -> ActorLexicon:
    """The lexicon from ``config/`` (cached; used by the ACLED / UCDP writers)."""
    return ActorLexicon.load()


def _country(value: Any, where: str) -> str | None:
    if value is None:
        return None
    c = countries.get(str(value))
    if c is None or len(str(value).strip()) != 2:
        raise LexiconError(f"{where}: unknown ISO2 country {value!r} (quote codes: \"NO\")")
    return c.iso2


def _klass(value: Any, where: str) -> str:
    k = str(value or "")
    if k not in CLASSES:
        raise LexiconError(f"{where}: unknown actor class {value!r}")
    return k


def _entries(cfg: Mapping[str, Any], path: Path) -> Iterator[tuple[str, ActorEntry]]:
    for i, e in enumerate(cfg.get("entries") or []):
        where = f"{path} entries[{i}]"
        phrases = [str(p) for p in (e or {}).get("match") or []]
        if not phrases:
            raise LexiconError(f"{where}: no match phrases")
        entry = ActorEntry(str(e.get("name") or phrases[0]), _country(e.get("country"), where),
                           _klass(e.get("class"), where))
        for p in phrases:
            yield p, entry


def _terror_groups(cfg: Mapping[str, Any], groups_path: Path
                   ) -> Iterator[tuple[str, ActorEntry]]:
    homes = cfg.get("terror_group_homes") or {}
    if not homes:
        return
    from osint.terror.groups import GroupKB  # local: only needed while loading

    kb = GroupKB.load(groups_path)
    for gid, home in homes.items():
        where = f"terror_group_homes.{gid}"
        g = kb.get(str(gid))
        if g is None:
            raise LexiconError(f"{where}: no group {gid!r} in {groups_path}")
        if isinstance(home, Mapping):
            cc, klass = home.get("country"), home.get("class") or "non_state_armed"
        else:
            cc, klass = home, "non_state_armed"
        entry = ActorEntry(g.name, _country(cc, where), _klass(klass, where))
        for p in (g.name, *g.aliases):
            if len(name_key(p)) >= 3:
                yield p, entry


def _generated(cfg: Mapping[str, Any]) -> Iterator[tuple[str, ActorEntry]]:
    gen = cfg.get("generated") or {}
    extra = {str(k): [str(x) for x in v or []]
             for k, v in (cfg.get("extra_adjectives") or {}).items()}
    for k in extra:
        _country(k, f"extra_adjectives.{k}")
    skip = {str(x) for x in gen.get("skip_name_forms") or []}
    blocks = {key: {_klass(k, f"generated.{key}"): [str(x) for x in v or []]
                    for k, v in (gen.get(key) or {}).items()}
              for key in ("nouns", "demonym_nouns", "of_country", "country_suffix")}
    for c in countries.all_countries():
        adjs = list(dict.fromkeys([*c.demonyms, *extra.get(c.iso2, [])]))
        label = adjs[0] if adjs else c.name
        names = [c.name, *c.aliases]
        own = [] if c.iso2 in skip else [c.name]
        for key, table in blocks.items():
            for klass, words in table.items():
                entry = ActorEntry(f"{label} {CLASS_NOUN.get(klass, klass)}", c.iso2, klass)
                for w in words:
                    if key == "nouns":
                        phrases = [f"{a} {w}" for a in (*adjs, *own)]
                    elif key == "demonym_nouns":
                        phrases = [f"{a} {w}" for a in adjs]
                    else:  # "Government of Sudan", "Protesters (Kenya)"
                        phrases = [f"{w} {n}" for n in names]
                    for p in phrases:
                        yield p, entry


# ------------------------------------------------------------------- resolution


def _base_source(event: Mapping[str, Any]) -> str:
    eid = str(event.get("id") or "")
    return "acled" if eid.startswith("acled-") else "ucdp" if eid.startswith("ucdp-") \
        else "gdelt"


def resolve_actors(event: Mapping[str, Any], docs: Sequence[Any], lexicon: ActorLexicon,
                   llm_actors: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The event's actors, each with ``country`` (ISO2 or None) and ``source``
    (gdelt | acled | ucdp | rule | llm), duplicates removed by (name, country), in order:

    1. GDELT actors: the CAMEO country code (kept as ``cameo``) -> ISO2;
    2. ACLED / UCDP side names through the lexicon;
    3. lexicon hits in every document's title and snippet, role ``mentioned`` (news and
       sensor events only: an ACLED / UCDP document is generated from the side names);
    4. the LLM extraction's actor names through the lexicon.

    ``docs`` are ``osint.enrich.rules.base.ExtractDoc``-like (``title``, ``snippet``).
    Earlier ``rule`` / ``llm`` entries in ``event['actors']`` are dropped and re-derived,
    so the result does not depend on how often it ran."""
    base = _base_source(event)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    def add(actor: dict[str, Any]) -> None:
        key = (name_key(str(actor.get("name") or "")), actor.get("country"))
        if key[0] and key not in seen:
            seen.add(key)
            out.append(actor)

    for a in event.get("actors") or []:
        src = str(a.get("source") or base)
        if src in ("rule", "llm"):
            continue
        if src == "gdelt":
            # rows written before the registry kept the CAMEO code in "country"
            cameo = str((a["cameo"] if "cameo" in a else a.get("country")) or "").strip().upper()
            add({**a, "country": countries.to_iso2(cameo, "cameo") if cameo else None,
                 "cameo": cameo, "source": "gdelt"})
        else:
            hit = lexicon.lookup(str(a.get("name") or ""))
            add({**a, "country": hit.country if hit else None, "source": src})
    if base == "gdelt":
        for d in docs:
            for text in (getattr(d, "title", ""), getattr(d, "snippet", "")):
                for h in lexicon.find(text or ""):
                    e = h.payload
                    add({"name": e.name, "role": "mentioned", "type": e.klass,
                         "country": e.country, "source": "rule", "quote": h.text})
    for a in llm_actors:
        nm = str(a.get("name") or "").strip()
        if not nm:
            continue
        hit = lexicon.lookup(nm)
        add({"name": nm, "role": str(a.get("role") or "other"),
             "type": str(a.get("type") or (hit.klass if hit else "")),
             "country": hit.country if hit else None, "source": "llm"})
    return out


def derive(actors: Sequence[Mapping[str, Any]], country: str | None) -> dict[str, Any]:
    """``actor_countries`` (sorted ISO2, nulls dropped), ``foreign_involved`` (an actor
    country differs from the event's country) and ``attributed`` (any actor country)."""
    ccs = sorted({str(a["country"]) for a in actors if a.get("country")})
    home = (country or "").strip().upper()
    return {"actor_countries": ccs,
            "foreign_involved": bool(home) and any(c != home for c in ccs),
            "attributed": bool(ccs)}
