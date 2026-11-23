"""Groups knowledge base and entity linking (lesson 8: name-only matches are leads).

* ``GroupKB.load(path)`` reads config/terror_groups.yaml (identity only; designation status
  comes from the official lists, activity from incident data).
* ``GroupKB.link(...)`` links an incident's perpetrator to a group. A link is made only
  when a group alias appears as a whole word in a *verbatim* attribution / perpetrator /
  claim quote; an alias found only in the extracted perpetrator name (not in any quote) is
  a ``lead`` with low confidence; similarity is never used.
* ``GroupKB.match_designation(names)`` attaches an official list entry to a group by exact
  folded-name equality (strong aliases only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from osint.terror.countries import fold

CATEGORY_LABELS = {
    "jihadist": "Jihadist / Salafi-jihadist", "shia_militant": "Shia militant",
    "palestinian_militant": "Palestinian militant", "ethno_separatist": "Ethno-nationalist / "
    "separatist", "left_wing": "Left-wing / Marxist", "right_wing_dve": "Racially or "
    "ethnically motivated / far-right", "anarchist": "Anarchist / violent left",
    "nihilist_dve": "Nihilistic violent extremism", "other_political": "Other political",
    "criminal": "Criminal organisation (designated by some states)",
    "unknown": "Not categorised (from an official list)",
}
# attribution confidence by level when the alias is in a verbatim quote
LINK_CONFIDENCE = {"claimed": 0.9, "attributed_by_authorities": 0.8, "suspected": 0.6,
                   "unknown": 0.5}
LEAD_CONFIDENCE = 0.3


@dataclass
class Group:
    id: str
    name: str
    category: str = "unknown"
    parent: str | None = None
    aliases: list[str] = field(default_factory=list)
    weak_aliases: list[str] = field(default_factory=list)
    ucdp_actors: list[str] = field(default_factory=list)
    note: str | None = None
    conflict_party: bool = False  # a party to an ongoing armed conflict (warfare vs terrorism)
    auto: bool = False  # created from an official list entry (no curated aliases)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category,
                "category_label": CATEGORY_LABELS.get(self.category, self.category),
                "parent": self.parent, "aliases": self.aliases,
                "weak_aliases": self.weak_aliases, "ucdp_actors": self.ucdp_actors,
                "note": self.note, "conflict_party": self.conflict_party, "auto": self.auto}


def _alias_rx(alias: str) -> re.Pattern:
    a = fold(alias)
    # whole-word on the folded text; folded text uses single spaces
    return re.compile(rf"(?<![\w]){re.escape(a)}(?![\w])")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", fold(s)).strip("_")[:60] or "group"


_PAREN = re.compile(r"\(([^)]*)\)")
_SPLIT = re.compile(r"\s*(?:,|;|\balso known as\b|\baka\b|\ba\.k\.a\.?|\bformerly\b|"
                    r"\bf\.k\.a\.?|\bincluding\b|\bwhich translates as\b)\s*", re.IGNORECASE)


def name_variants(raw: str) -> list[str]:
    """Names in an official list entry: "Islamic State's Khorasan Province (ISIS-K)" ->
    [the full name, "ISIS-K"]; "al-Nusrah Front, aka Hay'at Tahrir al-Sham (ANF/HTS)" ->
    [..., "Hay'at Tahrir al-Sham", "ANF", "HTS"]."""
    out: list[str] = []
    text = re.sub(r"\s+", " ", raw or "").strip()
    for inner in _PAREN.findall(text):
        for p in re.split(r"[/,]| or ", inner):
            p = p.strip()
            if p and not re.fullmatch(r"\d{4}|formerly .*|incl.*", p, re.IGNORECASE):
                out.append(p)
    base = _PAREN.sub(" ", text)
    for p in _SPLIT.split(base):
        p = p.strip(" .-–:")
        if len(p) >= 3:
            out.append(p)
    seen: set[str] = set()
    res = []
    for v in [text, *out]:
        k = fold(v)
        if k and k not in seen:
            seen.add(k)
            res.append(v)
    return res


class GroupKB:
    def __init__(self, groups: list[Group]) -> None:
        self.groups: dict[str, Group] = {}
        self._strong: list[tuple[re.Pattern, str, str]] = []  # (rx, group id, alias)
        self._weak: dict[str, list[str]] = {}
        self._exact: dict[str, str] = {}
        self._ucdp: dict[str, str] = {}
        for g in groups:
            self.add(g)

    @classmethod
    def load(cls, path: Path) -> GroupKB:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        groups = []
        for d in (data or {}).get("groups") or []:
            groups.append(Group(
                id=str(d["id"]), name=str(d.get("name") or d["id"]),
                category=str(d.get("category") or "unknown"), parent=d.get("parent"),
                aliases=[str(a) for a in d.get("aliases") or []],
                weak_aliases=[str(a) for a in d.get("weak_aliases") or []],
                ucdp_actors=[str(a) for a in d.get("ucdp_actors") or []], note=d.get("note"),
                conflict_party=bool(d.get("conflict_party"))))
        return cls(groups)

    def add(self, g: Group) -> None:
        self.groups[g.id] = g
        for a in [g.name, *g.aliases]:
            if len(fold(a)) >= 3:
                self._strong.append((_alias_rx(a), g.id, a))
                self._exact.setdefault(fold(a), g.id)
        for a in g.weak_aliases:
            self._weak.setdefault(fold(a), []).append(g.id)
        for u in g.ucdp_actors:
            self._ucdp[fold(u)] = g.id
        # longest aliases first: "Islamic State Khorasan Province" before "Islamic State"
        self._strong.sort(key=lambda t: -len(t[2]))

    def add_auto(self, items: list[dict]) -> None:
        """Re-add groups created from official lists (persisted by the worker)."""
        for g in items or []:
            if g.get("id") and g["id"] not in self.groups:
                self.add(Group(id=g["id"], name=g.get("name") or g["id"],
                               category=g.get("category") or "unknown",
                               aliases=list(g.get("aliases") or []),
                               weak_aliases=list(g.get("weak_aliases") or []), auto=True))

    def auto_groups(self) -> list[dict]:
        return [g.as_dict() for g in self.groups.values() if g.auto]

    def get(self, gid: str | None) -> Group | None:
        return self.groups.get(gid or "")

    def for_ucdp_actor(self, actor: str) -> str | None:
        return self._ucdp.get(fold(actor))

    def children(self, gid: str) -> list[str]:
        return [g.id for g in self.groups.values() if g.parent == gid]

    # ------------------------------------------------------------- free-text matches

    def find(self, text: str | None) -> list[tuple[str, str]]:
        """[(group id, alias)] for strong aliases in ``text`` (most specific first; an
        alias contained in a longer matched alias is not reported again)."""
        t = fold(text or "")
        if not t:
            return []
        hits: list[tuple[str, str]] = []
        spans: list[tuple[int, int]] = []
        for rx, gid, alias in self._strong:
            for m in rx.finditer(t):
                if any(s <= m.start() and m.end() <= e for s, e in spans):
                    continue
                spans.append((m.start(), m.end()))
                if gid not in [h[0] for h in hits]:
                    hits.append((gid, alias))
        return hits

    def link(self, *, perpetrator: str | None, quotes: list[str], level: str) -> dict | None:
        """Link to a group. ``quotes`` are verbatim-verified attribution / perpetrator /
        claim quotes. Returns {"group_id", "alias", "confidence", "kind": "link"|"lead",
        "basis"} or None."""
        for q in quotes:
            hits = self.find(q)
            if hits:
                gid, alias = hits[0]
                others = [h[0] for h in hits[1:]]
                return {"group_id": gid, "alias": alias, "kind": "link",
                        "confidence": LINK_CONFIDENCE.get(level, 0.5),
                        "basis": f"alias '{alias}' in a verbatim quote: \"{q[:160]}\"",
                        "also_mentioned": others}
        p = (perpetrator or "").strip()
        if p:
            hits = self.find(p)
            if hits:
                gid, alias = hits[0]
                return {"group_id": gid, "alias": alias, "kind": "lead",
                        "confidence": LEAD_CONFIDENCE,
                        "basis": f"name only: extracted perpetrator '{p[:80]}' contains alias "
                                 f"'{alias}' but no verbatim quote supports it (lead, not "
                                 "attribution)", "also_mentioned": [h[0] for h in hits[1:]]}
            weak = self._weak.get(fold(p))
            if weak and len(weak) == 1:
                in_quote = any(_alias_rx(p).search(fold(q)) for q in quotes)
                return {"group_id": weak[0], "alias": p, "kind": "link" if in_quote else "lead",
                        "confidence": (LINK_CONFIDENCE.get(level, 0.5) - 0.2) if in_quote
                        else LEAD_CONFIDENCE,
                        "basis": f"ambiguous short name '{p}' "
                                 + ("in a verbatim quote" if in_quote else
                                    "(extracted name only; lead)"), "also_mentioned": []}
        return None

    # ------------------------------------------------------------- designations

    def match_designation(self, names: list[str]) -> tuple[str | None, str | None]:
        """(group id, matched name) when an official entry name/alias equals a strong
        alias exactly (folded); (None, None) otherwise."""
        for n in names:
            gid = self._exact.get(fold(n))
            if gid:
                return gid, n
        return None, None

    def auto_group(self, name: str, aliases: list[str]) -> Group:
        """A KB entry for an official-list organisation not in the curated file."""
        variants = name_variants(name)
        main = variants[0]
        clean = re.sub(r"\s*\([^)]*\)", "", main).strip() or main
        gid = "list_" + slug(clean)
        if gid in self.groups:
            return self.groups[gid]
        strong = [v for v in [*variants[1:], *aliases] if len(fold(v)) >= 5
                  and not re.fullmatch(r"[A-Z0-9\-/]{2,6}", v.strip())]
        weak = [v for v in [*variants[1:], *aliases] if re.fullmatch(r"[A-Z0-9\-/]{2,6}",
                                                                      v.strip())]
        g = Group(id=gid, name=clean, category="unknown", aliases=strong[:20],
                  weak_aliases=weak[:5], auto=True)
        self.add(g)
        return g
