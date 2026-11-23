"""Terror Watch source register: every source with its URL, cadence, parser, licence and
verification note. Served at /api/terror/sources and merged into /api/sources/licences."""

from __future__ import annotations

from dataclasses import dataclass, field

from osint.terror.sources import designations as des
from osint.terror.sources import feeds, official

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 Noctua-OSINT/0.1")
BOT_UA = "Mozilla/5.0 (compatible; Noctua-OSINT/0.1)"


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    kind: str  # level | advisory | designation | docs | research | baseline | reference
    urls: tuple[str, ...]
    interval_h: float
    licence: str
    commercial: str  # ok | review | no
    attribution: str = ""
    terms_url: str = ""
    browser_ua: bool = False
    enabled: bool = True
    note: str = ""
    extra: dict = field(default_factory=dict)


SOURCES: list[SourceSpec] = [
    # ---------------------------------------------------------- official threat levels
    SourceSpec("mi5_threat_level", "UK national threat level (MI5 / JTAC)", "level",
               (official.MI5_URL,), 6, "Crown copyright (Open Government Licence v3.0 where "
               "applicable)", "review", "Source: MI5", "https://www.mi5.gov.uk/",
               note="RSS feed; the HTML page refuses automated clients"),
    SourceSpec("vigipirate", "France Plan Vigipirate posture (SGDSN)", "level",
               (official.VIGIPIRATE_URL,), 12, "Licence Ouverte / etalab-2.0 (stated on the page)",
               "ok", "Source: SGDSN", "https://www.sgdsn.gouv.fr/", browser_ua=True,
               note="the site serves a JavaScript challenge to non-browser user agents"),
    SourceSpec("nctv_threat_level", "Netherlands threat level (NCTV, DTN)", "level",
               (official.NCTV_URL,), 12, "Rijksoverheid content (CC0 per the Rijksoverheid "
               "copyright statement)", "review", "Source: NCTV", "https://www.nctv.nl/"),
    SourceSpec("asio_threat_level", "Australia national terrorism threat level (ASIO)", "level",
               (official.ASIO_URL,), 12, "Commonwealth of Australia (CC BY 4.0 per site "
               "copyright notice)", "review", "Source: Australian National Security",
               "https://www.nationalsecurity.gov.au/"),
    SourceSpec("dhs_ntas", "US National Terrorism Advisory System (DHS)", "level",
               (official.NTAS_FEED, official.NTAS_PAGE), 6, "US Government work (public "
               "domain)", "ok", "Source: DHS NTAS", "https://www.dhs.gov/", browser_ua=True,
               note="XML feed of current advisories; the page (cross-check) refuses bot "
                    "user agents"),
    SourceSpec("state_advisories", "US State Department travel advisories", "advisory",
               (official.STATE_TA_URL,), 12, "US Government work (public domain)", "ok",
               "Source: travel.state.gov", "https://travel.state.gov/",
               note="Worldwide Caution page refuses automated clients (HTTP 403): not used"),
    # ---------------------------------------------------------- designations
    SourceSpec("us_fto", "US Foreign Terrorist Organizations (State Dept)", "designation",
               (des.FTO_URL,), 24, "US Government work (public domain)", "ok",
               "Source: U.S. Department of State", "https://www.state.gov/"),
    SourceSpec("ofac_sdgt", "OFAC SDN list -- SDGT programme", "designation", (des.SDN_URL,), 24,
               "US Government work (public domain)", "ok", "Source: U.S. Treasury OFAC",
               "https://ofac.treasury.gov/", note="the CSV export carries no listing dates: "
               "additions are dated by first sight"),
    SourceSpec("un_1267_1988", "UN Security Council consolidated list (ISIL/AQ 1267, Taliban "
               "1988)", "designation", (des.UN_URL,), 24, "United Nations (public information, "
               "UN terms of use)", "review", "Source: UN Security Council",
               "https://www.un.org/en/about-us/terms-of-use"),
    SourceSpec("uk_proscribed", "UK proscribed terrorist groups (Home Office)", "designation",
               (des.UK_URL,), 24, "Open Government Licence v3.0", "ok", "Source: GOV.UK",
               "https://www.gov.uk/help/terms-conditions"),
    SourceSpec("eu_terrorist_list", "EU terrorist list (CP 931) and EU ISIL/AQ regime (FSF)",
               "designation", (des.EU_URL,), 24, "European Commission reuse policy (Decision "
               "2011/833/EU), attribution", "ok", "Source: European Commission, FSF",
               "https://data.europa.eu/"),
    # ---------------------------------------------------------- justice / LE
    SourceSpec("doj_press", "US DOJ press releases (terrorism topics / NSD)", "docs",
               (feeds.DOJ_URL,), 2, "US Government work (public domain)", "ok",
               "Source: U.S. Department of Justice", "https://www.justice.gov/",
               note="justice.gov RSS ignores topic filters; the JSON API is paged and filtered "
                    "client-side by DOJ's own topic tags"),
    SourceSpec("europol_news", "Europol newsroom", "docs", (feeds.EUROPOL_URL,), 6,
               "© Europol; metadata and links only", "review", "Source: Europol",
               "https://www.europol.europa.eu/", note="the feed has no item dates"),
    # ---------------------------------------------------------- research context
    SourceSpec("gnet_research", "GNET (Global Network on Extremism & Technology)", "research",
               (feeds.GNET_URL,), 12, "© GNET / King's College London; metadata and links only",
               "review", "Source: GNET", "https://gnet-research.org/",
               note="analysis context only; never an incident source"),
    SourceSpec("ctc_sentinel", "CTC Sentinel (West Point)", "research",
               ("https://ctc.westpoint.edu/feed/",), 24, "n/a", "review", enabled=False,
               note="verified 2026-11-23: the domain serves a parked page, no feed"),
    SourceSpec("icct", "ICCT (International Centre for Counter-Terrorism)", "research",
               ("https://icct.nl/rss.xml",), 24, "n/a", "review", enabled=False,
               note="verified 2026-11-23: feed reachable but its newest item is from 2023"),
    # ---------------------------------------------------------- baselines / references
    SourceSpec("ucdp_osv", "UCDP GED + Candidate: one-sided violence by non-state actors",
               "baseline", ("https://ucdp.uu.se/downloads/",), 24 * 7,
               "CC BY 4.0", "ok", "UCDP Georeferenced Event Dataset, Uppsala Conflict Data "
               "Program", "https://ucdp.uu.se/downloads/"),
    SourceSpec("gtd", "Global Terrorism Database (START)", "reference",
               ("https://www.start.umd.edu/gtd/",), 0, "START GTD end-user agreement "
               "(registration; no redistribution; commercial use requires a licence)", "no",
               enabled=False, note="not ingested: the licence does not allow redistribution; "
               "its definition is cited (see /api/terror/definition)"),
    SourceSpec("te_sat", "Europol EU Terrorism Situation & Trend Report (TE-SAT)", "reference",
               ("https://www.europol.europa.eu/publications-events/main-reports/tesat-report",),
               0, "© Europol", "review", enabled=False,
               note="annual PDF report: cited with a link, figures not machine-ingested"),
]
BY_ID = {s.id: s for s in SOURCES}


def licence_entries() -> list[dict]:
    """Entries in the /api/sources/licences register format."""
    out = []
    for s in SOURCES:
        out.append({"id": "terror:" + s.id, "name": f"Terror Watch -- {s.name}",
                    "licence": s.licence, "url": s.terms_url or (s.urls[0] if s.urls else ""),
                    "attribution": s.attribution or f"Source: {s.name}", "commercial": s.commercial,
                    "notes": (s.note + ("" if s.enabled else " (not used)")).strip()})
    return out
