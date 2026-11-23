"""Terror extraction: one LLM call per candidate event (its best article), normalised with
the same verification discipline as the event extraction -- every quote is checked
verbatim against the text the model saw; a field whose supporting quote is not in the text
is kept but marked unverified, and the classifier never lets an unverified field upgrade
anything.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from osint.terror.schema import (
    ATTACK_TYPES,
    ATTRIBUTION,
    INSTRUCTION,
    KINDS,
    OCCURRED,
    PERP_TYPES,
    PRECISIONS,
    QUOTE_MAX,
    SCHEMA_VERSION,
    TARGET_TYPES,
    TERROR_SCHEMA,
)

log = logging.getLogger(__name__)
_WS = re.compile(r"\s+")


def _fold(s: str) -> str:
    return _WS.sub(" ", s or "").strip().casefold()


def _str(v, limit: int = 300) -> str | None:
    if v is None:
        return None
    s = _WS.sub(" ", str(v)).strip()
    return s[:limit] or None


def _int(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return max(0, int(v))


def _bool(v) -> bool | None:
    return v if isinstance(v, bool) else None


def _enum(v, allowed: list[str], default: str) -> str:
    return v if v in allowed else default


class Quotes:
    """Verbatim checker over the text the model saw."""

    def __init__(self, text: str) -> None:
        self.folded = _fold(text)
        self.unverified: list[str] = []

    def check(self, field: str, q) -> str | None:
        quote = _str(q, QUOTE_MAX)
        if not quote:
            return None
        if _fold(quote) in self.folded:
            return quote
        self.unverified.append(field)
        return None


def normalise(raw: dict, text: str) -> dict:
    """Coerce a model result into the schema; quotes not found verbatim are dropped (the
    field name is listed in ``unverified``)."""
    q = Quotes(text)
    perp = raw.get("perpetrator") if isinstance(raw.get("perpetrator"), dict) else {}
    attr = raw.get("attribution") if isinstance(raw.get("attribution"), dict) else {}
    mot = raw.get("motive") if isinstance(raw.get("motive"), dict) else {}
    crit = raw.get("criteria") if isinstance(raw.get("criteria"), dict) else {}
    where = raw.get("where") if isinstance(raw.get("where"), dict) else {}
    cas = raw.get("casualties") if isinstance(raw.get("casualties"), dict) else {}
    country = (_str(where.get("country"), 8) or "").upper()
    killed, killed_max = _int(cas.get("killed")), _int(cas.get("killed_max"))
    injured, injured_max = _int(cas.get("injured")), _int(cas.get("injured_max"))
    if killed is not None and killed_max is not None and killed_max < killed:
        killed_max = None
    if injured is not None and injured_max is not None and injured_max < injured:
        injured_max = None
    extra = []
    for x in raw.get("quotes") or []:
        if isinstance(x, dict):
            v = q.check("quotes." + (_str(x.get("field"), 40) or "other"), x.get("quote"))
            if v:
                extra.append({"field": _str(x.get("field"), 40) or "other", "quote": v})
    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": _enum(raw.get("kind"), KINDS, "none"),
        "occurred": _enum(raw.get("occurred"), OCCURRED, "unclear"),
        "negation_quote": q.check("negation_quote", raw.get("negation_quote")),
        "summary": _str(raw.get("summary"), 400) or "",
        "attack_type": _enum(raw.get("attack_type"), ATTACK_TYPES, "unknown"),
        "target_type": _enum(raw.get("target_type"), TARGET_TYPES, "unknown"),
        "perpetrator": {"name": _str(perp.get("name"), 160),
                        "type": _enum(perp.get("type"), PERP_TYPES, "unknown"),
                        "quote": q.check("perpetrator", perp.get("quote"))},
        "attribution": {"level": _enum(attr.get("level"), ATTRIBUTION, "unknown"),
                        "by": _str(attr.get("by"), 160), "channel": _str(attr.get("channel"), 120),
                        "quote": q.check("attribution", attr.get("quote"))},
        "motive": {"stated": _str(mot.get("stated"), 200),
                   "quote": q.check("motive", mot.get("quote"))},
        "criteria": {k: _bool(crit.get(k)) for k in (
            "political_religious_social_goal", "intimidate_wider_audience",
            "civilians_or_noncombatants_targeted", "in_armed_conflict_zone")},
        "where": {"place_name": _str(where.get("place_name"), 120),
                  "admin1": _str(where.get("admin1"), 120),
                  "country": country if re.fullmatch(r"[A-Z]{2}", country) else None,
                  "precision": _enum(where.get("precision"), PRECISIONS, "unknown"),
                  "quote": q.check("where", where.get("quote"))},
        "when": _str(raw.get("when"), 40),
        "when_quote": q.check("when", raw.get("when_quote")),
        "casualties": {"killed": killed, "killed_max": killed_max, "injured": injured,
                       "injured_max": injured_max,
                       "includes_perpetrators": _bool(cas.get("includes_perpetrators")),
                       "quote": q.check("casualties", cas.get("quote"))},
        "is_follow_up": bool(raw.get("is_follow_up")),
        "quotes": extra[:12],
    }
    out["unverified"] = sorted(set(q.unverified))
    return out


@dataclass
class ExtractItem:
    event: dict
    doc: dict


async def extract_one(backend, http, item: ExtractItem, *, fetch_cfg, alterlab_key: str = "",
                      snippet: str = "") -> tuple[dict, dict]:
    """(normalised result, call meta) for one event. Raises LLMError/OSError on failure."""
    from osint.pipeline.extract.fetch import get_article_text

    doc = item.doc
    art = await get_article_text(http, doc["url"], title=doc.get("title") or "",
                                 snippet=snippet, cfg=fetch_cfg, alterlab_key=alterlab_key)
    text = f"Headline: {doc.get('title') or ''}\nURL: {doc['url']}\n\n{art.text}"
    res = await backend.extract(INSTRUCTION, text, TERROR_SCHEMA)
    out = normalise(res.data, text)
    out["text_source"] = art.source
    if art.error:
        out["fetch_error"] = art.error[:200]
    meta = {"cost_usd": res.cost_usd, "latency_s": round(res.latency_s, 2), "model": res.model,
            "doc_id": doc.get("doc_id"), "url": doc["url"], "text_source": art.source}
    return out, meta


async def gather_limited(coros, limit: int):
    sem = asyncio.Semaphore(max(1, limit))

    async def one(c):
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros), return_exceptions=True)
