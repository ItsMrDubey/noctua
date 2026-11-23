"""Official terrorist designation / proscription lists -> normalised entries.

Each parser takes the raw payload (bytes) and returns a list of entry dicts:
``{list_id, ref, name, aliases, kind, status, listed_on, delisted_on, programmes,
remarks, linked_to, source_url}`` where ``ref`` is stable across fetches (the list's own
identifier when it has one) and dates are ISO ``YYYY-MM-DD`` (or ``YYYY-MM`` when the list
only states a month). Nothing is inferred: a missing date stays None.
"""

from __future__ import annotations

import csv
import html as htmllib
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from osint.terror.groups import slug

FTO_URL = "https://www.state.gov/foreign-terrorist-organizations/"
SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
UK_URL = ("https://www.gov.uk/api/content/government/publications/proscribed-terror-groups-or-"
          "organisations--2/proscribed-terrorist-groups-or-organisations-accessible-version")
UK_PAGE = ("https://www.gov.uk/government/publications/proscribed-terror-groups-or-"
           "organisations--2/proscribed-terrorist-groups-or-organisations-accessible-version")
EU_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
          "?token=dG9rZW4tMjAxNw")

LISTS = {
    "us_fto": {"name": "US Foreign Terrorist Organizations (State Dept)", "jurisdiction": "US",
               "url": FTO_URL},
    "us_sdgt": {"name": "US Specially Designated Global Terrorists (OFAC SDN, SDGT programme)",
                "jurisdiction": "US", "url": SDN_URL},
    "un_1267": {"name": "UN Security Council ISIL (Da'esh) & Al-Qaida sanctions list (1267)",
                "jurisdiction": "UN", "url": UN_URL},
    "un_1988": {"name": "UN Security Council 1988 (Taliban) sanctions list",
                "jurisdiction": "UN", "url": UN_URL},
    "uk_proscribed": {"name": "UK proscribed terrorist groups (Home Office)",
                      "jurisdiction": "GB", "url": UK_PAGE},
    "eu_terr": {"name": "EU terrorist list (CP 2001/931/CFSP, Reg. 2580/2001)",
                "jurisdiction": "EU", "url": EU_URL},
    "eu_isil_aq": {"name": "EU autonomous ISIL (Da'esh) / Al-Qaida regime (Reg. 2016/1686)",
                   "jurisdiction": "EU", "url": EU_URL},
}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], 1)}
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: str) -> str:
    return _WS.sub(" ", htmllib.unescape(_TAG.sub(" ", fragment or ""))).strip()


def us_date(s: str) -> str | None:
    """"November 16, 2026" -> "2026-11-16"."""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def month_year(s: str) -> str | None:
    """"Proscribed March 2001" -> "2001-03" (the list states months only)."""
    m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|"
                  r"November|December)\s*(\d{4})", s or "", re.IGNORECASE)
    if not m:
        return None
    return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()]:02d}"


def _entry(list_id: str, ref: str, name: str, **kw) -> dict:
    return {"list_id": list_id, "ref": ref, "name": name, "aliases": kw.get("aliases") or [],
            "kind": kw.get("kind", "organisation"), "status": kw.get("status", "listed"),
            "listed_on": kw.get("listed_on"), "delisted_on": kw.get("delisted_on"),
            "programmes": kw.get("programmes") or [], "remarks": kw.get("remarks"),
            "linked_to": kw.get("linked_to") or [],
            "source_url": kw.get("source_url") or LISTS[list_id]["url"]}


# ------------------------------------------------------------------ US FTO


def parse_fto(body: bytes) -> list[dict]:
    """State Department FTO page: the designated table (Date Designated | Name) and the
    delisted table (Date Removed | Name | Date Originally Listed)."""
    page = body.decode("utf-8", errors="replace")
    out: list[dict] = []
    for table in re.findall(r"<table.*?</table>", page, re.DOTALL | re.IGNORECASE):
        heads = [_text(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.DOTALL)]
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL | re.IGNORECASE)
        delisted = any("removed" in h for h in heads)
        if not heads or not any("name" in h for h in heads):
            continue
        for row in rows:
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)]
            if len(cells) < 2 or not cells[1]:
                continue
            nm = cells[1]
            if delisted:
                out.append(_entry("us_fto", "fto:" + slug(nm), nm, status="delisted",
                                  delisted_on=us_date(cells[0]),
                                  listed_on=us_date(cells[2]) if len(cells) > 2 else None))
            else:
                out.append(_entry("us_fto", "fto:" + slug(nm), nm, listed_on=us_date(cells[0])))
    return out


# ------------------------------------------------------------------ OFAC SDN (SDGT)

_LINKED = re.compile(r"Linked To:\s*([^.;]+)", re.IGNORECASE)
_AKA = re.compile(r"a\.k\.a\.\s*'([^']+)'", re.IGNORECASE)


def parse_sdn(body: bytes) -> list[dict]:
    """OFAC SDN.CSV rows whose programme list contains SDGT. Columns: ent_num, SDN_Name,
    SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage, GRT, Vess_flag, Vess_owner,
    Remarks. "-0-" is OFAC's empty marker. The export carries no listing dates."""
    text = body.decode("latin-1")
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 12:
            continue
        progs = [p.strip(" []") for p in re.split(r"\]\s*\[", row[3])]
        if "SDGT" not in progs:
            continue
        kind_raw = row[2].strip()
        kind = {"individual": "person", "vessel": "vessel", "aircraft": "aircraft"}.get(
            kind_raw, "organisation")
        remarks = "" if row[11].strip() == "-0-" else row[11].strip()
        out.append(_entry("us_sdgt", "sdn:" + row[0].strip(), row[1].strip(), kind=kind,
                          programmes=progs, remarks=remarks[:600],
                          aliases=_AKA.findall(remarks)[:15],
                          linked_to=[x.strip() for x in _LINKED.findall(remarks)]))
    return out


# ------------------------------------------------------------------ UN consolidated


def parse_un(body: bytes) -> list[dict]:
    """UN Security Council consolidated list: ENTITIES / INDIVIDUALS of the Al-Qaida
    (1267/1989/2253) and Taliban (1988) regimes."""
    root = ET.fromstring(body)
    out = []
    for section, kind in (("ENTITIES", "organisation"), ("INDIVIDUALS", "person")):
        sec = root.find(section)
        if sec is None:
            continue
        for e in sec:
            lt = (e.findtext("UN_LIST_TYPE") or "").strip()
            list_id = {"Al-Qaida": "un_1267", "Taliban": "un_1988"}.get(lt)
            if list_id is None:
                continue
            name = " ".join(x for x in (e.findtext(t) for t in (
                "FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")) if x).strip()
            aliases = [a.findtext("ALIAS_NAME").strip() for a in
                       e.findall("ENTITY_ALIAS") + e.findall("INDIVIDUAL_ALIAS")
                       if (a.findtext("ALIAS_NAME") or "").strip()]
            ref = (e.findtext("REFERENCE_NUMBER") or e.findtext("DATAID") or "").strip()
            out.append(_entry(list_id, "un:" + ref, name, kind=kind, aliases=aliases[:20],
                              listed_on=(e.findtext("LISTED_ON") or "").strip()[:10] or None,
                              remarks=(e.findtext("COMMENTS1") or "").strip()[:600] or None,
                              programmes=[lt]))
    return out


# ------------------------------------------------------------------ UK proscription


def parse_uk(body: bytes) -> list[dict]:
    """GOV.UK content API JSON for the proscribed-groups page: each group is an <h3>
    heading "<names>: Proscribed <Month Year>" after "List of proscribed ...". Groups in the
    "Deproscription" section are skipped (they are not on the list)."""
    doc = json.loads(body)
    html = (doc.get("details") or {}).get("body") or ""
    start = html.find("List of proscribed")
    if start < 0:
        return []
    out = []
    for raw in re.findall(r"<h3[^>]*>(.*?)</h3>", html[start:], re.DOTALL | re.IGNORECASE):
        head = _text(raw)
        if not re.search(r"proscri", head, re.IGNORECASE):
            continue
        name = re.split(r"\s*[:–-]\s*(?=Proscri|proscri|JeM proscribed|Proscription)", head)[0]
        name = re.sub(r"\s*[-–:]\s*$", "", name).strip()
        if not name:
            continue
        out.append(_entry("uk_proscribed", "uk:" + slug(name), name, listed_on=month_year(head),
                          remarks=head[:300]))
    return out


# ------------------------------------------------------------------ EU

_EU_NS = {"e": "http://eu.europa.ec/fpi/fsd/export"}
_EU_PROGS = {"TERR": "eu_terr", "EUAQ": "eu_isil_aq"}


def parse_eu(body: bytes) -> list[dict]:
    """EU Financial Sanctions Files (FSF) full XML: entities/persons under programme TERR
    (CP 931 list) and EUAQ (autonomous ISIL/Al-Qaida regime)."""
    out = []
    for _, el in ET.iterparse(io.BytesIO(body), events=("end",)):
        if not el.tag.endswith("sanctionEntity"):
            continue
        regs = el.findall("e:regulation", _EU_NS)
        progs = {r.get("programme") for r in regs}
        hit = [p for p in progs if p in _EU_PROGS]
        if hit:
            st = el.find("e:subjectType", _EU_NS)
            kind = "person" if st is not None and st.get("code") == "person" else "organisation"
            names = el.findall("e:nameAlias", _EU_NS)
            allnames = [n.get("wholeName") for n in names if n.get("wholeName")]
            primary = _eu_primary(names) or (allnames[0] if allnames else "")
            acts = sorted(((r.get("publicationDate") or ""), r.get("numberTitle") or "")
                          for r in regs if r.get("programme") in _EU_PROGS)
            remark = el.findtext("e:remark", default="", namespaces=_EU_NS)
            act = (f"legal acts in the export: {', '.join(t for _, t in acts[-3:])} (latest "
                   f"published {acts[-1][0]})") if acts else None
            # the export lists the acts currently in force (the TERR list is re-adopted every
            # six months), not the original listing date: listed_on stays None
            out.append(_entry(_EU_PROGS[hit[0]], "eu:" + (el.get("logicalId") or ""), primary,
                              kind=kind, aliases=list(dict.fromkeys(allnames))[:25],
                              programmes=sorted(hit),
                              remarks="; ".join(x for x in (remark, act) if x) or None))
        el.clear()
    return out


def _latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.9


def _eu_primary(names) -> str | None:
    """English name first, then a language-neutral Latin-script name, then any Latin-script
    name (the export repeats each name in every EU language)."""
    for test in (lambda n: (n.get("nameLanguage") or "").upper() == "EN",
                 lambda n: not (n.get("nameLanguage") or "") and _latin(n.get("wholeName") or ""),
                 lambda n: _latin(n.get("wholeName") or "")):
        for n in names:
            if n.get("wholeName") and test(n):
                return n.get("wholeName")
    return None


PARSERS = {"us_fto": parse_fto, "us_sdgt": parse_sdn, "un": parse_un, "uk_proscribed": parse_uk,
           "eu": parse_eu}


def today_iso(now: datetime) -> str:
    return now.date().isoformat()
