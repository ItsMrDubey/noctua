"""Batch translation: up to ``batch_size`` short texts per LLM call, mapped back by index.

The texts go in on stdin as a JSON array ``[{"i", "lang", "text"}]``; the model answers
``{"items": [{"i", "en"}]}`` (JSON schema). Indices are validated; items missing from the
answer are retried once in a second call, then reported as failed.
"""

import json
import logging
from dataclasses import dataclass, field

from osint.llm import LLMBackend, LLMError

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "en": {"type": "string"}},
                "required": ["i", "en"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

INSTRUCTION = (
    "You are a news translator. The input (stdin) is a JSON array of items "
    '{"i": index, "lang": ISO language code or "", "text": headline or news snippet}. '
    "Translate each item faithfully into concise English news-style text. Keep the meaning "
    "and every fact; do not add, explain or summarise. Write names of people, places and "
    "organisations in their standard English forms (e.g. Kyiv, Kharkiv, Odesa, Tehran, "
    "Hezbollah, Houthis). If an item is already English, return it unchanged. Texts may be "
    "cut off mid-sentence: translate what is there. No commentary. Answer with JSON "
    '{"items": [{"i": index, "en": English text}]}, exactly one entry per input index.'
)


@dataclass
class BatchOutcome:
    done: dict[str, str] = field(default_factory=dict)   # key -> English
    failed: dict[str, str] = field(default_factory=dict)  # key -> error
    calls: list[dict] = field(default_factory=list)       # per call: ok, n, n_ok, cost...
    model: str = ""


def parse_items(data: dict, n: int) -> dict[int, str]:
    """{index: English} for valid, in-range, non-empty answers (first one wins)."""
    out: dict[int, str] = {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        i, en = it.get("i"), it.get("en")
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n:
            continue
        if not isinstance(en, str) or not en.strip() or i in out:
            continue
        out[i] = " ".join(en.split())
    return out


def payload(items: list[dict]) -> str:
    return json.dumps([{"i": n, "lang": it.get("src_lang") or it.get("lang") or "",
                        "text": it["text"]} for n, it in enumerate(items)],
                      ensure_ascii=False)


class BatchTranslator:
    def __init__(self, backend: LLMBackend, *, acquire=None) -> None:
        """``acquire()`` -> bool reserves one call against the rate caps (None = no caps)."""
        self.backend = backend
        self.acquire = acquire or (lambda: True)

    async def _call(self, items: list[dict], out: BatchOutcome) -> dict[int, str] | None:
        if not self.acquire():
            return None
        try:
            res = await self.backend.extract(INSTRUCTION, payload(items), SCHEMA)
        except (LLMError, NotImplementedError, OSError) as exc:
            err = f"{type(exc).__name__}: {exc}"
            out.calls.append({"ok": False, "n": len(items), "n_ok": 0, "error": err})
            log.warning("translation call failed (%d items): %s", len(items), err[:200])
            return {}
        got = parse_items(res.data, len(items))
        out.model = res.model or out.model
        out.calls.append({"ok": True, "n": len(items), "n_ok": len(got),
                          "cost_usd": res.cost_usd, "latency_s": res.latency_s,
                          "model": res.model})
        return got

    async def translate(self, items: list[dict]) -> BatchOutcome:
        """Translate one batch (dicts with key, src_lang, text). Items left out because the
        caps were reached are in neither ``done`` nor ``failed``."""
        out = BatchOutcome()
        if not items:
            return out
        got = await self._call(items, out)
        if got is None:
            return out
        for i, en in got.items():
            out.done[items[i]["key"]] = en
        missing = [it for i, it in enumerate(items) if i not in got]
        if not missing:
            return out
        err = out.calls[-1].get("error") or "missing from model answer"
        retry = await self._call(missing, out)
        if retry is None:  # capped: leave them pending
            return out
        for i, it in enumerate(missing):
            if i in retry:
                out.done[it["key"]] = retry[i]
            else:
                out.failed[it["key"]] = out.calls[-1].get("error") or err
        return out
