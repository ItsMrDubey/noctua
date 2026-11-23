"""Independent-outlet counting for confirmation (lessons 2 and 3).

* An *outlet* is an organisation, not a feed: france24.com in English, French and Arabic
  is one outlet; aljazeera.com and aljazeera.net are one; every Telegram channel together
  is ``t.me`` (a channel post is one origin however many channels repost it).
* Reposts (near-identical titles, ``osint.intel.aggregate.repost_groups``) collapse into
  the first-published document's outlet, so a wire story copied by three sites is one
  origin.
* GDELT rows and the platform's own derived documents never count.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# organisations publishing under several domains
FAMILIES: dict[str, str] = {
    "aljazeera.net": "aljazeera", "aljazeera.com": "aljazeera", "ajnet.me": "aljazeera",
    "bbc.co.uk": "bbc", "bbc.com": "bbc", "dw.com": "dw", "dw.de": "dw",
    "france24.com": "france24", "rfi.fr": "rfi", "tass.ru": "tass", "tass.com": "tass",
    "ukrinform.ua": "ukrinform", "ukrinform.net": "ukrinform", "ukrinform.de": "ukrinform",
    "ukrinform.fr": "ukrinform", "ukrinform.es": "ukrinform", "ria.ru": "ria",
    "sputniknews.com": "sputnik", "sputnikglobe.com": "sputnik", "rt.com": "rt",
    "cnn.com": "cnn", "edition.cnn.com": "cnn", "reuters.com": "reuters",
    "apnews.com": "ap", "afp.com": "afp", "timesofisrael.com": "timesofisrael",
    "iranintl.com": "iranintl", "iranintl.net": "iranintl", "alarabiya.net": "alarabiya",
    "alhadath.net": "alarabiya", "hdth.tv": "alarabiya", "alarabiya.com": "alarabiya",
    "meduza.io": "meduza", "kyivindependent.com": "kyivindependent",
    "pravda.com.ua": "ukrpravda", "lemonde.fr": "lemonde", "scmp.com": "scmp",
    "allafrica.com": "allafrica", "presstv.ir": "presstv", "presstv.com": "presstv",
}
# feed/channel name -> organisation, for documents whose URL is a t.me post of a known
# outlet's own channel (still t.me: one origin family, never a second confirmation)
NON_COUNTING_SOURCES = ("gdelt", "sensor", "ucdp", "acled", "terror:")
_WWW = re.compile(r"^(?:www\d?|m|mobile|amp|edition|english|arabic|en|ar|fr|es|ru|uk|fa|de)\.")


def domain(url: str | None) -> str:
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    while True:
        new = _WWW.sub("", host)
        if new == host:
            break
        host = new
    return host


def registrable(host: str) -> str:
    """Rough eTLD+1 ("news.bbc.co.uk" -> "bbc.co.uk", "edition.cnn.com" -> "cnn.com")."""
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    second_level = {"co", "com", "org", "net", "gov", "ac", "edu", "gob", "go", "or", "ne"}
    if parts[-2] in second_level and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def outlet(doc: dict) -> str | None:
    """Organisation key of a document, None when it never counts (GDELT, sensors...)."""
    src = (doc.get("source") or "").lower()
    if not src or any(src.startswith(p) for p in NON_COUNTING_SOURCES):
        return None
    host = domain(doc.get("url"))
    if not host:
        return None
    if host in ("t.me", "telegram.me", "telegram.org"):
        return "t.me"
    reg = registrable(host)
    return FAMILIES.get(host) or FAMILIES.get(reg) or reg


def independent_outlets(docs: list[dict]) -> dict:
    """{"outlets": [...], "n": k, "origins": [[outlet, first_title, n_docs]...],
    "telegram_only": bool}: distinct organisations after collapsing reposts."""
    from osint.intel.aggregate import repost_groups  # light import, no heavy deps

    counted = [d for d in sorted(docs, key=lambda d: d.get("published_at") or "")
               if outlet(d) is not None]
    groups = repost_groups(counted)
    origins: dict[str, dict] = {}
    for g in groups:
        lead = counted[g[0]]
        key = outlet(lead)
        o = origins.setdefault(key, {"outlet": key, "first": lead.get("published_at"),
                                     "title": lead.get("title"), "docs": 0, "languages": set()})
        o["docs"] += len(g)
        for i in g:
            o["languages"].add((counted[i].get("language") or "").split("-")[0] or "?")
    # a repost by another outlet does not make that outlet an independent origin: only
    # repost-group leaders define origins (done above), members add nothing
    out = []
    for o in origins.values():
        out.append({**o, "languages": sorted(o["languages"])})
    out.sort(key=lambda o: o["first"] or "")
    names = [o["outlet"] for o in out]
    return {"outlets": names, "n": len(names), "origins": out,
            "telegram_only": bool(names) and set(names) == {"t.me"}}
