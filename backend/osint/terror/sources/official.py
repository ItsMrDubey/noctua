"""Official national terrorism threat levels and travel advisories.

Only levels a government publishes itself, read from its own page / feed. Each parser
returns dicts with the level exactly as published, its rank on the published scale, the
verbatim sentence it was read from (``quote``) and the as-of date the source states (None
when it states none -- never the fetch time). A page whose wording no longer matches
raises ``ParseError`` (the level is then shown as unavailable, never guessed).
"""

from __future__ import annotations

import html as htmllib
import json
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from osint.terror import countries

MI5_URL = "https://www.mi5.gov.uk/UKThreatLevel/UKThreatLevel.xml"
MI5_PAGE = "https://www.mi5.gov.uk/threats-and-advice/threat-levels"
VIGIPIRATE_URL = "https://www.sgdsn.gouv.fr/vigipirate"
NCTV_URL = "https://www.nctv.nl/onderwerpen/dtn"
ASIO_URL = ("https://www.nationalsecurity.gov.au/national-threat-level/current-national-"
            "terrorism-threat-level")
NTAS_FEED = "https://www.dhs.gov/ntas/1.1/feed.xml"
NTAS_PAGE = "https://www.dhs.gov/national-terrorism-advisory-system"
STATE_TA_URL = "https://cadataapi.state.gov/api/TravelAdvisories"

UK_SCALE = ["LOW", "MODERATE", "SUBSTANTIAL", "SEVERE", "CRITICAL"]
AU_SCALE = ["NOT EXPECTED", "POSSIBLE", "PROBABLE", "EXPECTED", "CERTAIN"]
NL_SCALE = ["1 - minimaal", "2 - beperkt", "3 - aanzienlijk", "4 - substantieel", "5 - kritiek"]
FR_SCALE_2026 = ["vigilance", "vigilance renforcée", "alerte attentat"]
FR_SCALE_OLD = ["vigilance", "sécurité renforcée – risque attentat", "urgence attentat"]
_MONTHS_FR = {m: i for i, m in enumerate(
    ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
     "octobre", "novembre", "décembre"], 1)}
_MONTHS_NL = {m: i for i, m in enumerate(
    ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september",
     "oktober", "november", "december"], 1)}
_WS = re.compile(r"\s+")


class ParseError(ValueError):
    """The source's wording changed: no level can be read reliably."""


def page_text(body: bytes) -> str:
    t = body.decode("utf-8", errors="replace")
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.DOTALL | re.IGNORECASE)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return _WS.sub(" ", t).strip()


def _sentence(text: str, pos: int, limit: int = 300) -> str:
    start = max(text.rfind(". ", 0, pos) + 2, 0, pos - 200)
    end = text.find(". ", pos)
    end = len(text) if end < 0 else end + 1
    return text[start:end][:limit].strip()


def _level(country: str, authority: str, level: str, scale: list[str], *, quote: str,
           as_of: str | None, url: str, scope: str = "national", note: str | None = None
           ) -> dict:
    rank = None
    for i, s in enumerate(scale):
        if s.casefold() == level.casefold():
            rank = i + 1
    return {"country": country, "authority": authority, "scope": scope, "level": level,
            "rank": rank, "scale": scale, "scale_max": len(scale), "quote": quote,
            "as_of": as_of, "source_url": url, "note": note}


# ------------------------------------------------------------------ UK (MI5 / JTAC)


def parse_mi5(body: bytes) -> list[dict]:
    root = ET.fromstring(body)
    item = root.find("channel/item")
    if item is None:
        raise ParseError("MI5 feed has no item")
    desc = page_text((item.findtext("description") or "").encode())
    title = item.findtext("title") or ""
    m = re.search(r"national threat level is (\w+)", desc, re.IGNORECASE) or \
        re.search(r"Threat Level:\s*(\w+)", title, re.IGNORECASE)
    if not m or m.group(1).upper() not in UK_SCALE:
        raise ParseError(f"MI5 wording changed: {title!r}")
    as_of = None
    raw_date = root.findtext("channel/lastBuildDate")
    if raw_date:
        try:
            as_of = parsedate_to_datetime(raw_date).date().isoformat()
        except (TypeError, ValueError):
            as_of = None
    out = [_level("GB", "MI5 / Joint Terrorism Analysis Centre", m.group(1).upper(), UK_SCALE,
                  quote=desc[:300], as_of=as_of, url=MI5_PAGE,
                  note="as_of = the feed's last build date (the date the level was last "
                       "set or re-published)")]
    ni = re.search(r"Northern Ireland from Northern Ireland-related terrorism is (\w+)", desc,
                   re.IGNORECASE)
    if ni and ni.group(1).upper() in UK_SCALE:
        out.append(_level("GB", "MI5", ni.group(1).upper(), UK_SCALE, quote=desc[:300],
                          as_of=as_of, url=MI5_PAGE,
                          scope="Northern Ireland-related terrorism in Northern Ireland"))
    return out


# ------------------------------------------------------------------ France (Vigipirate)


def _fr_date(s: str) -> str | None:
    m = re.search(r"(\d{1,2})(?:er)?\s+(" + "|".join(_MONTHS_FR) + r")\s+(\d{4})", s,
                  re.IGNORECASE)
    if not m:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS_FR[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def parse_vigipirate(body: bytes) -> list[dict]:
    text = page_text(body)
    m = re.search(r"(?:Positionnée|positionnée|placée|Placée|maintenue)\s+(?:au|en)\s+(?:nouveau\s+)?"
                  r"(?:stade|niveau)\s+«\s*([^»]+?)\s*»", text)
    scale = FR_SCALE_2026
    if not m:
        m = re.search(r"niveau\s+«\s*(urgence attentat|sécurité renforcée[^»]*|vigilance)\s*»",
                      text, re.IGNORECASE)
        scale = FR_SCALE_OLD
    if not m:
        raise ParseError("Vigipirate wording changed")
    level = m.group(1).strip()
    lv = next((s for s in scale if s.casefold() == level.casefold()), level)
    eff = re.search(r"prend effet à compter du ([^.]{4,40}?\d{4})", text)
    period = re.search(r"posture Vigipirate pour la période «\s*([^»]+?)\s*»", text)
    return [_level("FR", "SGDSN (Plan Vigipirate)", lv, scale, quote=_sentence(text, m.start()),
                   as_of=_fr_date(eff.group(1)) if eff else None, url=VIGIPIRATE_URL,
                   note=(f"posture « {period.group(1)} »; " if period else "")
                   + "as_of = the date the posture took effect, as stated")]


# ------------------------------------------------------------------ Netherlands (NCTV)


def parse_nctv(body: bytes) -> list[dict]:
    text = page_text(body)
    m = re.search(r"dreigingsniveau[^.]{0,80}?\bniveau\s+([1-5])\s+op een schaal van 5", text,
                  re.IGNORECASE)
    if not m:
        raise ParseError("NCTV wording changed")
    n = int(m.group(1))
    dtn = re.search(r"DTN van (" + "|".join(_MONTHS_NL) + r")\s+(\d{4})", text, re.IGNORECASE)
    as_of = f"{int(dtn.group(2)):04d}-{_MONTHS_NL[dtn.group(1).lower()]:02d}" if dtn else None
    return [_level("NL", "NCTV (Dreigingsbeeld Terrorisme Nederland)", NL_SCALE[n - 1],
                   NL_SCALE, quote=_sentence(text, m.start()), as_of=as_of, url=NCTV_URL,
                   note="as_of = month of the threat assessment (DTN) that set the level")]


# ------------------------------------------------------------------ Australia (ASIO)


def parse_asio(body: bytes) -> list[dict]:
    text = page_text(body)
    m = re.search(r"(?:Australia\W{1,3}s )?general terrorism threat level is (NOT EXPECTED|"
                  r"POSSIBLE|PROBABLE|EXPECTED|CERTAIN)\b[^.]{0,200}\.?", text)
    if not m:
        raise ParseError("ASIO wording changed")
    return [_level("AU", "ASIO (National Terrorism Threat Advisory System)", m.group(1),
                   AU_SCALE, quote=m.group(0)[:300], as_of=None, url=ASIO_URL,
                   note="the page does not state when the level was set")]


# ------------------------------------------------------------------ US (DHS NTAS)


def parse_ntas(feed: bytes, page: bytes | None = None) -> list[dict]:
    """NTAS has no standing level: it issues time-limited Bulletins / Alerts. The XML feed
    lists the current ones (empty = none); the page text is a cross-check."""
    root = ET.fromstring(feed)
    alerts = []
    for a in list(root):
        alerts.append({"type": a.get("type") or a.tag, "title": (a.findtext("summary") or
                       a.get("title") or "").strip()[:200],
                       "start": a.get("start"), "end": a.get("end"), "link": a.get("link")})
    quote = None
    if page:
        t = page_text(page)
        i = t.find("Current Advisories")
        if i >= 0:
            quote = t[i:i + 160]
        if "There are no current advisories" in t and alerts:
            raise ParseError("NTAS feed and page disagree")
    level = f"{len(alerts)} current advisory(ies)" if alerts else "No current advisory"
    out = _level("US", "DHS National Terrorism Advisory System", level,
                 ["No current advisory", "Bulletin", "Elevated Alert", "Imminent Alert"],
                 quote=quote or ("feed lists no current advisory" if not alerts else ""),
                 as_of=None, url=NTAS_PAGE,
                 note="NTAS issues time-limited bulletins/alerts rather than a standing "
                      "level; expired bulletins are not current")
    if alerts:
        kinds = " ".join(x["type"] or "" for x in alerts).lower()
        out["rank"] = 4 if "imminent" in kinds else 3 if "elevated" in kinds else 2
        out["level"] = alerts[0]["type"] or out["level"]
    else:
        out["rank"] = 1
    out["advisories"] = alerts
    return [out]


def parse_ntas_text(text: str) -> list[dict]:
    """NTAS from the page text alone (used when the feed is unreachable): the "Current
    Advisories" section up to "Expired Advisories"."""
    t = _WS.sub(" ", text or "")
    i = t.find("Current Advisories")
    if i < 0:
        raise ParseError("NTAS page wording changed")
    j = t.find("Expired Advisories", i)
    cur = t[i:j if j > 0 else i + 600]
    alerts = []
    if "There are no current advisories" not in cur:
        for m in re.finditer(r"(National Terrorism Advisory System (?:Bulletin|Alert)[^|]{0,80})",
                             cur):
            kind = "Bulletin" if "Bulletin" in m.group(1) else "Alert"
            alerts.append({"type": kind, "title": m.group(1).strip()[:200], "start": None,
                           "end": None, "link": NTAS_PAGE})
        if not alerts:
            raise ParseError("NTAS current section not understood")
    feed = b"<alerts/>" if not alerts else b"<alerts>" + b"".join(
        b"<alert type='" + a["type"].encode() + b"'/>" for a in alerts) + b"</alerts>"
    out = parse_ntas(feed, None)
    out[0]["quote"] = cur[:200]
    out[0]["advisories"] = alerts or out[0]["advisories"]
    return out


# ------------------------------------------------------------------ US State Dept advisories

_RISK_TERMS = ("terrorism", "unrest", "crime", "kidnapping", "civil unrest", "health",
               "wrongful detention", "natural disaster", "other", "arbitrary arrest")


def parse_state_advisories(body: bytes) -> list[dict]:
    """cadataapi.state.gov TravelAdvisories JSON: one advisory per country; ``Category``
    holds FIPS 10-4 codes. ``terrorism`` is True only when the advisory text itself names
    terrorism as a risk ("due to ... terrorism")."""
    out = []
    for a in json.loads(body):
        title = a.get("Title") or ""
        m = re.search(r"Level\s+(\d)\s*:\s*([^<]+)$", title.strip())
        cats = a.get("Category") or []
        place = title.split(" - Level")[0].strip()
        # the advisory's own place name first (State tags Gaza and the West Bank with Israel's
        # FIPS code), then the FIPS 10-4 category
        iso = countries.iso2(place)
        for c in cats:
            iso = iso or countries.from_fips(c)
        summary = page_text((a.get("Summary") or "").encode())
        due = re.search(r"due to ([^.]{0,300})", summary, re.IGNORECASE)
        risks = [r for r in _RISK_TERMS if due and re.search(rf"\b{r}\b", due.group(1), re.IGNORECASE)]
        out.append({"country": iso, "place": place, "level": int(m.group(1)) if m else None,
                    "label": m.group(2).strip() if m else None, "title": title,
                    "terrorism": "terrorism" in risks, "risks": risks,
                    "quote": due.group(0)[:300] if due else None,
                    "published": a.get("Published"), "updated": a.get("Updated"),
                    "url": a.get("Link"), "fips": cats})
    return merge_advisories(out)


def merge_advisories(items: list[dict]) -> list[dict]:
    """One row per country: several advisories for one ISO code (Gaza + West Bank) keep the
    highest level; every part is listed."""
    by: dict = {}
    for a in items:
        key = a["country"] or ("?" + a["place"])
        cur = by.get(key)
        if cur is None:
            by[key] = {**a, "parts": [{"place": a["place"], "level": a["level"], "url": a["url"]}]}
            continue
        cur["parts"].append({"place": a["place"], "level": a["level"], "url": a["url"]})
        cur["terrorism"] = cur["terrorism"] or a["terrorism"]
        cur["risks"] = sorted(set(cur["risks"]) | set(a["risks"]))
        if (a["level"] or 0) > (cur["level"] or 0):
            parts = cur["parts"]
            by[key] = {**a, "parts": parts, "terrorism": cur["terrorism"], "risks": cur["risks"]}
    out = list(by.values())
    for a in out:
        if len(a["parts"]) > 1:
            a["title"] = a["title"] + " (highest of: " + ", ".join(
                f"{p['place']} L{p['level']}" for p in a["parts"]) + ")"
    return out


PARSERS = {"mi5": parse_mi5, "vigipirate": parse_vigipirate, "nctv": parse_nctv,
           "asio": parse_asio}
