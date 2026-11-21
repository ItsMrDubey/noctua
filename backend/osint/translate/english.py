"""English fields for API responses, computed at response time from the translation cache.

``title_en``/``summary_en``/``snippet_en`` are None while a translation is pending (the UI
shows the original with a "translating..." hint). English originals pass through.
"""

from osint.pipeline.triage import detect_language
from osint.translate.store import TranslationStore, clean, norm_lang


def _english_or_none(ref: dict | None, text: str | None, max_chars: int,
                     lang: str = "") -> str | None:
    """The cached English for ``text`` if ``ref`` still points at that text."""
    t = clean(text, max_chars)
    if not t:
        return None
    if ref is not None and ref.get("text") == t:
        if ref.get("status") == "passthrough":
            return (text or "").strip()  # English original, unclipped
        return ref.get("text_en") or None
    # not registered yet (or the text changed since): English originals pass through
    if lang and lang != "en":
        return None
    return (text or "").strip() if detect_language(t) == "en" else None


def _lang(ref: dict | None, text: str | None) -> str:
    if ref is not None and ref.get("lang"):
        return ref["lang"]
    return detect_language(text or "")


def _supported_summaries(extractions, event_ids: list[str]) -> dict[str, str]:
    if extractions is None or not event_ids:
        return {}
    out: dict[str, str] = {}
    for eid, rec in extractions.get_many(event_ids).items():
        res = rec.get("result") or {}
        if rec.get("status") == "done" and rec.get("verdict") == "supported" and \
                isinstance(res.get("summary"), str) and res["summary"].strip():
            out[eid] = res["summary"].strip()
    return out


def annotate_events(store: TranslationStore | None, items: list[dict], *,
                    extractions=None, summaries: bool = False,
                    max_chars: int = 300) -> None:
    """Add ``title_en`` and ``lang`` (and ``summary_en`` when ``summaries``) in place."""
    if not items:
        return
    refs: dict[str, dict] = {}
    if store is not None:
        names = [f"e:{it['id']}:title" for it in items]
        if summaries:
            names += [f"e:{it['id']}:summary" for it in items]
        refs = store.lookup_refs(names)
    for it in items:
        ref = refs.get(f"e:{it['id']}:title")
        it["lang"] = _lang(ref, it.get("title"))
        it["title_en"] = _english_or_none(ref, it.get("title"), max_chars)
        if summaries:
            it["summary_en"] = _english_or_none(refs.get(f"e:{it['id']}:summary"),
                                                it.get("summary"), max_chars)
    # extraction (verified): its English one-sentence summary is the best summary_en and
    # the fallback headline while the title translation is not ready
    need = [it["id"] for it in items if summaries or it["title_en"] is None]
    ext = _supported_summaries(extractions, need)
    for it in items:
        s = ext.get(it["id"])
        if s is None:
            continue
        if summaries:
            it["summary_en"] = s
        if it["title_en"] is None:
            it["title_en"] = s


def annotate_documents(store: TranslationStore | None, docs: list[dict], *,
                       max_chars: int = 300) -> None:
    """Add ``title_en``, ``snippet_en`` and ``lang`` to an event's documents in place."""
    refs: dict[str, dict] = {}
    if store is not None and docs:
        refs = store.lookup_refs([f"d:{d['doc_id']}:{f}" for d in docs
                                  for f in ("title", "snippet")])
    for d in docs:
        tref = refs.get(f"d:{d['doc_id']}:title")
        d["lang"] = (d.get("language") or "") or _lang(tref, d.get("title"))
        d["title_en"] = _english_or_none(tref, d.get("title"), max_chars,
                                         norm_lang(d.get("language")))
        sref = refs.get(f"d:{d['doc_id']}:snippet")
        d["snippet_en"] = None if sref is None else sref.get("text_en")
