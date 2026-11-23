"""Assemble aircraft / vessel profiles: identity, photo, sanctions, ownership chain, live state,
history and notable activity, each value carrying its source, plus a ranked ``highlights``
list and a context-dependent section order (what is notable for this asset comes first).

Rules: an owner, operator or sanctions reason is only ever shown as a source states it, with
that source; absent data is "unknown" together with what was checked. OpenSanctions matches
use strong identifiers only (IMO / MMSI for vessels, registration for aircraft); an MMSI-only
vessel match is flagged because MMSIs are re-used after re-flagging.

Response shape (``/api/profile/{kind}/{id}``)::

    {kind, id, key, generated_at, context, title, subtitle,
     highlights: [{severity: high|warn|info, text, source, section}],
     order: [section ids, most notable first],
     sections: {id: {id, title, severity, notable, summary, fields: [{label, value, source,
                url?}], ...section payload}},
     sources: {id: {name, status, fetched_at, licence, url, attribution, error?}},
     pending: bool}
"""

from __future__ import annotations

import csv
import logging
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from osint.core.geo import utcnow
from osint.enrich.mil_types import get_mil_category_from_type, is_high_priority
from osint.profiles import behaviour as bh
from osint.profiles.actypes import SOURCE_URL as TYPES_URL
from osint.profiles.lookups import SPECS
from osint.profiles.osgraph import ENTITY_URL, norm_imo, norm_reg

log = logging.getLogger(__name__)

SEV_RANK = {"high": 0, "warn": 1, "info": 2, "none": 3}
EMERG = {"7500": "Unlawful interference (hijack)", "7600": "Radio failure",
         "7700": "General emergency"}
OS_TOPICS = {"sanction": "Sanctioned", "sanction.linked": "Sanctions-linked",
             "mare.shadow": "Shadow fleet", "poi": "Person / entity of interest",
             "mare.detained": "Port-state detention", "export.control": "Export control",
             "reg.warn": "Regulator warning", "debarment": "Debarred", "crime": "Crime",
             "role.oligarch": "Oligarch", "gov.soe": "State-owned enterprise",
             "mil": "Military", "role.pep": "Politically exposed person"}
HIGH_TOPICS = {"sanction", "mare.shadow"}
WARN_TOPICS = {"sanction.linked", "poi", "export.control", "debarment", "crime"}
SOURCE_META = {
    "adsb": ("Live ADS-B (adsb.lol / adsb.fi / OpenSky)", ("mixed: adsb.lol ODbL; adsb.fi, "
             "OpenSky non-commercial"), "https://adsb.lol/"),
    "ais": ("Live AIS (AISStream)", "AISStream terms", "https://aisstream.io/"),
    "plane_alert": ("plane-alert-db (military aircraft)", "ODbL 1.0",
                    "https://github.com/sdr-enthusiasts/plane-alert-db"),
    "icao8643": ("ICAO Doc 8643 type designators (tar1090-db copy)",
                 "No licence stated (factual type data, review)", TYPES_URL),
    "opensanctions": ("OpenSanctions (sanctions + War & Sanctions graph)", "CC BY-NC 4.0",
                      "https://www.opensanctions.org/licensing/"),
    "os_maritime": ("OpenSanctions maritime.csv", "CC BY-NC 4.0",
                    "https://www.opensanctions.org/datasets/maritime/"),
    "watchlist": ("User watchlist (config/watchlist.yaml)", "local", ""),
    "lake": ("Our track history (positions lake)", "derived from the live feeds", ""),
    "signals": ("Signals: emergency squawks / GNSS (adsb.fi derived)", "non-commercial",
                "https://adsb.fi/"),
    "infra_watch": ("Infrastructure watch alerts", "derived", ""),
    "events": ("Our news / sensor events", "mixed (per event source)", ""),
    "frontline": ("DeepStateMap.live front line", "DeepStateMap terms", "https://deepstatemap.live/"),
}


def F(label: str, value, source: str, url: str | None = None, note: str | None = None) -> dict | None:
    if value in (None, "", []):
        return None
    d = {"label": label, "value": value, "source": source}
    if url:
        d["url"] = url
    if note:
        d["note"] = note
    return d


def _fields(*rows) -> list[dict]:
    return [r for r in rows if r]


def _first(*vals):
    for v in vals:
        if v not in (None, "", []):
            return v
    return None


def _sev_max(*sevs: str) -> str:
    sevs = [s for s in sevs if s]
    return min(sevs, key=SEV_RANK.__getitem__) if sevs else "none"


def _date(v) -> str | None:
    return str(v)[:10] if v else None


# ------------------------------------------------------------------ static references


class PlaneAlert:
    """Full plane-alert-db rows (the collector's MilDb keeps only a few columns)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, dict] | None = None
        self._lock = threading.Lock()

    def get(self, hex_code: str) -> dict | None:
        with self._lock:
            if self._rows is None:
                self._rows = {}
                try:
                    with self.path.open(encoding="utf-8") as fh:
                        for r in csv.DictReader(fh):
                            h = (r.get("$ICAO") or "").strip().lower()
                            if h:
                                self._rows[h] = {
                                    "reg": r.get("$Registration") or "",
                                    "operator": r.get("$Operator") or "",
                                    "type": r.get("$Type") or "", "icao_type": r.get("$ICAO Type") or "",
                                    "cmpg": r.get("#CMPG") or "", "category": r.get("Category") or "",
                                    "tags": [t for t in (r.get("$Tag 1"), r.get("$#Tag 2"),
                                                         r.get("$#Tag 3")) if t],
                                    "link": r.get("$#Link") or ""}
                except OSError as exc:
                    log.warning("plane-alert db unreadable: %s", exc)
            return self._rows.get(hex_code)


class MaritimeCsv:
    """``os_maritime.csv`` rows by IMO / MMSI (risk topics, lists, aliases, flag)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sig = None
        self.by_imo: dict[str, list[dict]] = {}
        self.by_mmsi: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        by_imo, by_mmsi = {}, {}
        with self.path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if (r.get("type") or "").upper() != "VESSEL":
                    continue
                row = {"id": r.get("id"), "name": r.get("caption"), "imo": norm_imo(r.get("imo")),
                       "risk": [x for x in (r.get("risk") or "").split(";") if x],
                       "datasets": [x for x in (r.get("datasets") or "").split(";") if x],
                       "flag": r.get("flag") or None, "url": r.get("url") or None,
                       "aliases": [x for x in (r.get("aliases") or "").split(";") if x][:12],
                       "countries": [x for x in (r.get("countries") or "").split(";") if x]}
                if (imo := norm_imo(r.get("imo"))):
                    by_imo.setdefault(imo, []).append(row)
                if (m := (r.get("mmsi") or "").strip()):
                    by_mmsi.setdefault(m, []).append(row)
        self.by_imo, self.by_mmsi, self._sig = by_imo, by_mmsi, sig

    def find(self, imo: str | None, mmsi: str | None) -> tuple[list[dict], str | None]:
        with self._lock:
            self._load()
            if imo and imo in self.by_imo:
                return self.by_imo[imo], "imo"
            if mmsi and mmsi in self.by_mmsi:
                return self.by_mmsi[mmsi], "mmsi"
            return [], None


def watch_active_row(path: Path, track_id: str) -> dict | None:
    import json
    import sqlite3

    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            r = db.execute("SELECT severity, hits, ts FROM active WHERE track_id=?",
                           (track_id,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    if r is None:
        return None
    try:
        return {"severity": r[0], "hits": json.loads(r[1]), "ts": r[2]}
    except ValueError:
        return None


# ------------------------------------------------------------------ OpenSanctions sections


def _sanction_rows(recs: list[dict]) -> list[dict]:
    out = []
    for s in recs:
        p = s.get("props") or {}
        out.append({
            "authority": "; ".join(p.get("authority") or []) or None,
            "program": "; ".join(p.get("program") or []) or None,
            "program_id": "; ".join(p.get("programId") or []) or None,
            "program_url": (p.get("programUrl") or [None])[0],
            "reason": " / ".join(p.get("reason") or []) or None,
            "provisions": p.get("provisions") or [],
            "summary": " ".join(p.get("summary") or []) or None,
            "start": _date(_first(*(p.get("startDate") or []), *(p.get("listingDate") or []))),
            "listed": _date((p.get("listingDate") or [None])[0]),
            "end": _date((p.get("endDate") or [None])[0]),
            "status": (p.get("status") or [None])[0],
            "country": "; ".join(p.get("country") or []) or None,
            "source_urls": (p.get("sourceUrl") or [])[:4],
            "datasets": s.get("datasets") or [],
            "last_change": s.get("last_change"),
        })
    out.sort(key=lambda r: (r["start"] or "9999", r["authority"] or ""))
    return out


def _programs(rows: list[dict]) -> list[str]:
    seen = []
    for r in rows:
        for p in (r.get("program_id") or "").split("; "):
            if p and p not in seen:
                seen.append(p)
    return seen


def _party(ent: dict | None, recs: list[dict], pid: str) -> dict:
    if ent is None:
        return {"id": pid, "name": pid, "url": ENTITY_URL.format(id=pid), "missing": True}
    p = ent.get("props") or {}
    rows = _sanction_rows(recs)
    topics = p.get("topics") or []
    return {"id": ent["id"], "name": ent.get("caption"), "schema": ent.get("schema"),
            "url": ent.get("url"), "countries": sorted(set((p.get("country") or [])
                                                          + (p.get("jurisdiction") or []))),
            "topics": topics, "risk": [OS_TOPICS.get(t, t) for t in topics],
            "sanctioned": "sanction" in topics,
            "programs": _programs(rows), "sanctions": rows[:8],
            "imo_company": (p.get("imoNumber") or [None])[0],
            "registration": (p.get("registrationNumber") or [None])[0],
            "address": (p.get("address") or [None])[0],
            "notes": (p.get("notes") or [None])[0], "datasets": ent.get("datasets") or [],
            "last_change": ent.get("last_change")}


def _role(link: dict) -> str:
    role = (link.get("role") or "").strip()
    if link["schema"] == "Ownership":
        return role[:1].upper() + role[1:] if role else "Owner"
    if link["schema"] == "Directorship":
        return role or "Director"
    if link["schema"] == "Representation":
        return role or "Representative"
    return (role[:1].upper() + role[1:]) if role else "Linked (role not stated)"


def os_asset_view(graph, asset_ids: list[str], matched_on: str) -> dict:
    """Sanctions + ownership payloads for the matched OpenSanctions asset entities."""
    entries, links = [], []
    for aid in asset_ids[:3]:
        ch = graph.chain(aid)
        ent = ch["entities"].get(aid)
        if ent is None:
            continue
        p = ent.get("props") or {}
        rows = _sanction_rows(ch["sanctions"].get(aid, []))
        entries.append({
            "id": aid, "name": ent.get("caption"), "url": ent.get("url"), "schema": ent.get("schema"),
            "matched_on": matched_on, "topics": p.get("topics") or [],
            "risk": [OS_TOPICS.get(t, t) for t in p.get("topics") or []],
            "programs": _programs(rows) or p.get("programId") or [], "records": rows,
            "narrative": _first(*(p.get("description") or []), *(p.get("notes") or [])),
            "aliases": p.get("alias") or [], "previous_names": p.get("previousName") or [],
            "past_flags": p.get("pastFlags") or [], "flag": (p.get("flag") or [None])[0],
            "type": (p.get("type") or [None])[0], "build": (p.get("buildDate") or [None])[0],
            "imo": (p.get("imoNumber") or [None])[0], "mmsi": (p.get("mmsi") or [None])[0],
            "callsign": (p.get("callSign") or [None])[0],
            "gross_tonnage": _first(*(p.get("grossRegisteredTonnage") or []), *(p.get("tonnage") or [])),
            "deadweight": (p.get("deadweightTonnage") or [None])[0],
            "model": (p.get("model") or [None])[0], "serial": (p.get("serialNumber") or [None])[0],
            "registration": (p.get("registrationNumber") or [None])[0],
            "source_urls": (p.get("sourceUrl") or [])[:6], "datasets": ent.get("datasets") or [],
            "first_seen": ent.get("first_seen"), "last_change": ent.get("last_change"),
        })
        for lk in ch["links"]:
            pid = lk["party_id"]
            ep = lk.get("props") or {}
            links.append({"role": _role(lk), "schema": lk["schema"], "depth": lk["depth"],
                          "of": lk["of"], "asset": aid,
                          "start": _date((ep.get("startDate") or [None])[0]),
                          "end": _date((ep.get("endDate") or [None])[0]),
                          "percentage": (ep.get("percentage") or [None])[0],
                          "datasets": lk.get("datasets") or [],
                          "source_urls": (ep.get("sourceUrl") or [])[:2],
                          "party": _party(ch["entities"].get(pid), ch["sanctions"].get(pid, []), pid)})
    # per level: current links (no end date) first, newest start first, undated last
    links.sort(key=lambda x: x["start"] or "", reverse=True)
    links.sort(key=lambda x: (x["depth"], x["end"] is not None))
    return {"entries": entries, "links": links}


# ------------------------------------------------------------------ assembly helpers


class Builder:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.sections: dict[str, dict] = {}
        self.highlights: list[dict] = []
        self.sources: dict[str, dict] = {}

    def hl(self, severity: str, text: str, source: str, section: str) -> None:
        self.highlights.append({"severity": severity, "text": text, "source": source,
                                "section": section})

    def sec(self, sid: str, title: str, *, severity: str = "none", notable: bool | None = None,
            summary: str = "", fields: list | None = None, **payload) -> dict:
        s = {"id": sid, "title": title, "severity": severity,
             "notable": severity in ("high", "warn") if notable is None else notable,
             "summary": summary, "fields": [f for f in fields or [] if f], **payload}
        self.sections[sid] = s
        return s

    def src(self, sid: str, status: str = "ok", fetched_at=None, **extra) -> None:
        if sid in SPECS:
            sp = SPECS[sid]
            meta = {"name": sp.name, "licence": sp.licence, "url": sp.url,
                    "attribution": sp.attribution}
        else:
            name, lic, url = SOURCE_META.get(sid, (sid, "", ""))
            meta = {"name": name, "licence": lic, "url": url}
        cur = self.sources.get(sid)
        if cur and cur["status"] == "ok" and status != "ok":
            return
        if isinstance(fetched_at, int | float):
            fetched_at = datetime.fromtimestamp(fetched_at, UTC).isoformat(timespec="seconds")
        self.sources[sid] = {**meta, "status": status, "fetched_at": fetched_at,
                             **{k: v for k, v in extra.items() if v is not None}}

    def lookup(self, sid: str, res: dict) -> dict | None:
        """Register a lookup result as a source; returns its data when ok."""
        if res.get("status") == "skipped":
            return None
        self.src(sid, res.get("status", "pending"), res.get("fetched_at"), error=res.get("error"),
                 stale=res.get("stale") or None)
        return res.get("data") if res.get("status") == "ok" else None

    def order(self, base: list[str]) -> list[str]:
        rank = {s: i for i, s in enumerate(base)}
        return sorted(self.sections, key=lambda s: (SEV_RANK[self.sections[s]["severity"]]
                                                   if self.sections[s]["severity"] in ("high", "warn") else 3,
                                                   rank.get(s, 99)))

    def result(self, **head) -> dict:
        self.highlights.sort(key=lambda h: SEV_RANK[h["severity"]])
        pending = any(s["status"] == "pending" for s in self.sources.values())
        return {**head, "generated_at": utcnow().isoformat(timespec="seconds"),
                "highlights": self.highlights, "sections": self.sections,
                "sources": self.sources, "pending": pending}


def _authority_short(a: str) -> str:
    return a.split(";")[0].strip()


def _best_reason(reasons: list[str], cap: int = 220) -> str | None:
    """The most descriptive stated reason (bare article numbers like "1.1" lose)."""
    good = [r for r in reasons if len(r) >= 40] or reasons
    if not good:
        return None
    r = max(good, key=len)
    return r[:cap] + ("…" if len(r) > cap else "")


def merge_maritime(rows: list[dict]) -> list[dict]:
    """maritime.csv has one row per source list; fold them into one row per risk topic."""
    by: dict[str, dict] = {}
    for r in rows:
        for t in r["risk"] or ["(no topic)"]:
            row = by.setdefault(t, {"topic": t, "label": OS_TOPICS.get(t, t), "datasets": [],
                                    "ids": [], "urls": []})
            row["datasets"] += [d for d in r["datasets"] if d not in row["datasets"]]
            if r["id"] not in row["ids"]:
                row["ids"].append(r["id"])
                if r.get("url"):
                    row["urls"].append(r["url"])
    rank = lambda t: 0 if t in HIGH_TOPICS else 1 if t in WARN_TOPICS else 2
    return sorted(by.values(), key=lambda x: rank(x["topic"]))


def _sanctions_section(b: Builder, osv: dict, *, checked: list[str], extra_listed: list[dict],
                       user_hits: list[dict], weak_hits: list[dict], label: str) -> None:
    """Strong identifier matches (IMO / MMSI / registration) are reported as listed; name-only
    watchlist hits are leads: "possible name match, unverified", with their doubts."""
    entries = osv["entries"]
    sanctioned = [e for e in entries if "sanction" in e["topics"]]
    lists = merge_maritime(extra_listed)
    listed = bool(entries or lists or user_hits)
    sev = "none"
    if user_hits:
        sev = _sev_max(sev, *[h.get("severity") or "warn" for h in user_hits])
        for h in user_hits:
            b.hl(h.get("severity") or "warn", f"User watchlist: {h.get('label') or h.get('name')}"
                 + (f" ({h['note']})" if h.get("note") else ""), "watchlist", "sanctions")
        b.src("watchlist")
    covered: set[str] = set()
    for e in entries:
        topics = set(e["topics"])
        covered |= topics
        progs = e["programs"]
        if "sanction" in topics or e["records"]:
            is_sanc = "sanction" in topics
            s_e = "high" if is_sanc or topics & HIGH_TOPICS else "warn"
            sev = _sev_max(sev, s_e)
            auths = sorted({_authority_short(r["authority"]) for r in e["records"] if r["authority"]})
            verb = "Sanctioned" if is_sanc else "Listed (" + ", ".join(e["risk"]) + ")"
            text = (f"{verb} by {len(auths)} authorit{'y' if len(auths) == 1 else 'ies'}"
                    if auths else verb)
            text += f" ({', '.join(progs[:8])}{' …' if len(progs) > 8 else ''})" if progs else ""
            reason = _best_reason([r["reason"] for r in e["records"] if r["reason"]]
                                  + ([e["narrative"]] if e.get("narrative") else []))
            if reason:
                text += f": {reason}"
            b.hl(s_e, text, "opensanctions", "sanctions")
        elif topics & HIGH_TOPICS:
            sev = "high"
            b.hl("high", f"Listed: {', '.join(e['risk'])} ({', '.join(e['datasets'][:2])})",
                 "opensanctions", "sanctions")
        elif topics & WARN_TOPICS:
            sev = _sev_max(sev, "warn")
            b.hl("warn", f"Listed: {', '.join(e['risk'])} ({', '.join(e['datasets'][:2])})",
                 "opensanctions", "sanctions")
        if e["matched_on"] == "mmsi":
            b.hl("warn", "OpenSanctions match on MMSI only: MMSIs are re-used after re-flagging, "
                 "verify the IMO", "opensanctions", "sanctions")
    for row in lists:
        t = row["topic"]
        s = "high" if t in HIGH_TOPICS else "warn" if t in WARN_TOPICS else "info"
        if s == "info" or t not in covered:
            b.hl(s, f"{row['label']} ({', '.join(row['datasets'][:4])})", "os_maritime", "sanctions")
        if s != "info":
            sev = _sev_max(sev, s)
    for h in weak_hits:
        b.hl("info", f"Possible name match, unverified: {h['name']} ({', '.join(h['risk'])})"
             + (f" - doubts: {'; '.join(h['doubts'])}" if h.get("doubts") else ""),
             "opensanctions", "sanctions")
    if listed:
        progs = sorted({p for e in entries for p in e["programs"]})
        summary = (f"{label} listed" + (f": {', '.join(progs)}" if progs else "")
                   + (f" ({', '.join(r['label'] for r in lists)})" if lists and not progs else ""))
    elif weak_hits:
        summary = (f"Not listed by identifier; {len(weak_hits)} possible name match(es), "
                   "unverified")
    else:
        summary = "Not listed. Checked: " + "; ".join(checked)
    b.sec("sanctions", "Sanctions & watchlists", severity=sev,
          notable=listed or bool(weak_hits), summary=summary, listed=listed,
          sanctioned=bool(sanctioned), entries=entries, lists=lists, user=user_hits,
          weak=weak_hits, checked=checked)


def group_links(links: list[dict]) -> list[dict]:
    """One row per (party, level) with all its roles (OFAC, UK and GUR often name the same
    company as owner, manager and "property in the interest of")."""
    out: dict[tuple, dict] = {}
    for lk in links:
        k = (lk["party"]["id"], lk["depth"], lk["of"])
        g = out.setdefault(k, {"party": lk["party"], "depth": lk["depth"], "of": lk["of"],
                               "asset": lk["asset"], "roles": [], "current": False})
        role = {k2: lk[k2] for k2 in ("role", "schema", "start", "end", "percentage",
                                      "datasets", "source_urls")}
        if not any(r["role"] == role["role"] and r["start"] == role["start"] for r in g["roles"]):
            g["roles"].append(role)
        g["current"] = g["current"] or not lk["end"]
    res = list(out.values())
    for g in res:
        g["roles"].sort(key=lambda r: (r["role"].startswith("Linked"), r["start"] or ""))
        g["latest"] = max((r["start"] or "" for r in g["roles"]), default="")
    res.sort(key=lambda g: g["latest"], reverse=True)
    res.sort(key=lambda g: (g["depth"], not g["current"]))
    return res


def _ownership_section(b: Builder, osv: dict, extra_rows: list[dict], label: str,
                       checked: list[str]) -> None:
    groups = group_links(osv["links"])
    sev = "none"
    for g in groups:
        pt = g["party"]
        roles = ", ".join(dict.fromkeys(r["role"] for r in g["roles"]))
        if pt.get("sanctioned"):
            sev = "high"
            b.hl("high" if g["depth"] == 1 else "warn",
                 f"{roles}{' (parent company)' if g['depth'] > 1 else ''}: {pt['name']} is sanctioned"
                 + (f" ({', '.join(pt['programs'][:5])})" if pt["programs"] else ""),
                 "opensanctions", "ownership")
        elif set(pt.get("topics") or []) & WARN_TOPICS and g["depth"] == 1:
            sev = _sev_max(sev, "warn")
    first = [g for g in groups if g["depth"] == 1]
    if first:
        summary = "; ".join(f"{', '.join(dict.fromkeys(r['role'] for r in g['roles']))}: "
                            f"{g['party']['name']}" for g in first[:2])
        summary += f" (+{len(first) - 2} more)" if len(first) > 2 else ""
    elif extra_rows:
        summary = "; ".join(f"{r['label']}: {r['value']}" for r in extra_rows[:2])
    else:
        summary = "Unknown. Checked: " + "; ".join(checked)
    b.sec("ownership", "Ownership & operators", severity=sev,
          notable=bool(groups) or sev != "none", summary=summary, groups=groups,
          fields=extra_rows, checked=checked)


def _activity_items(b: Builder, act: dict, kind: str) -> None:
    items, sev = [], "none"

    def add(s: str, text: str, source: str, **kw) -> None:
        nonlocal sev
        items.append({"severity": s, "text": text, "source": source, **kw})
        if s in ("high", "warn"):
            sev = _sev_max(sev, s)

    for sq in act.get("squawks") or []:
        s = "high" if sq["code"] in ("7500", "7700") else "warn"
        txt = (f"Squawk {sq['code']} {sq.get('label') or ''} as {sq.get('flight') or '?'} "
               f"({'active' if sq.get('active') else 'ended'}, first seen {(sq.get('first_seen') or '?')[:16]}Z)")
        add(s, txt, "signals", lat=sq.get("lat"), lon=sq.get("lon"))
        b.hl(s, txt, "signals", "activity")
    if (o := act.get("orbit")):
        txt = (f"Orbit / racetrack: {o['path_km']} km flown within {o['radius_km']} km of "
               f"{o['centre'][0]:.2f}, {o['centre'][1]:.2f}"
               + (f" (median {o['median_alt_ft']:,} ft)" if o.get("median_alt_ft") else ""))
        add("info", txt, "lake", lat=o["centre"][0], lon=o["centre"][1])
        b.hl("info", txt, "lake", "activity")
    if (fl := act.get("frontline")):
        s = "warn" if fl["km"] <= 100 else "info"
        txt = f"Closest approach to the Ukraine front line: {fl['km']} km ({fl['at'][:16]}Z)"
        add(s, txt, "frontline", lat=fl["lat"], lon=fl["lon"])
        b.hl(s, txt, "frontline", "activity")
    for g in act.get("gnss") or []:
        txt = (f"Crossed GNSS-interference cell ({g['level']}, {g.get('pct')}% of "
               f"{g.get('aircraft')} aircraft degraded) at {g['at'][:16]}Z")
        add("info", txt, "signals", lat=g.get("lat"), lon=g.get("lon"))
    if act.get("gnss"):
        b.hl("info", f"Flew through {len(act['gnss'])} GNSS-interference cell(s) in the last hour",
             "signals", "activity")
    for a in act.get("alerts") or []:
        if a["kind"] == "watchlist":
            continue
        s = "high" if a["severity"] == "high" else "warn"
        txt = f"{a['title']}: {a['sub']} ({a['ts'][:16]}Z)"
        add(s, txt, "infra_watch", lat=a.get("lat"), lon=a.get("lon"))
        b.hl(s, txt, "infra_watch", "activity")
    for g in act.get("gaps") or []:
        s = "warn" if g["hours"] >= 12 or g["jump_km"] >= 100 else "info"
        txt = (f"No AIS fixes for {g['hours']} h ({g['from'][:16]}Z to {g['to'][:16]}Z), "
               f"reappeared {g['jump_km']} km away")
        add(s, txt, "lake", lat=g["lat"], lon=g["lon"])
    if (big := [g for g in act.get("gaps") or [] if g["hours"] >= 12]):
        b.hl("warn", f"{len(big)} AIS gap(s) of 12 h or more in 7 days (longest "
             f"{max(g['hours'] for g in big)} h); feed gaps and AIS switched off look the same",
             "lake", "activity")
    for j in act.get("jumps") or []:
        txt = f"Position jump of {j['km']} km (implied {j['implied_kts']} kt) at {j['at'][:16]}Z"
        add("warn", txt, "lake", lat=j["lat"], lon=j["lon"])
    if act.get("jumps"):
        b.hl("warn", f"{len(act['jumps'])} impossible position jump(s): possible spoofing or bad "
             "fixes", "lake", "activity")
    for ev in act.get("events") or []:
        txt = f"{ev['km']} km from: {ev['title']} ({ev.get('category') or 'event'}, {ev['at'][:16]}Z)"
        add("info", txt, "events", lat=ev["lat"], lon=ev["lon"], event_id=ev["id"])
    if act.get("events"):
        b.hl("info", f"Passed within {act['events'][0]['km']} km of {len(act['events'])} reported "
             "event(s)", "events", "activity")
    summary = (f"{len(items)} notable item(s)" if items else
               "Nothing notable in our data (" + act.get("window", "") + ")")
    b.sec("activity", "Activity & behaviour", severity=sev, notable=bool(items), summary=summary,
          items=items, window=act.get("window"))


def _history_section(b: Builder, summ: dict | None, seen: dict | None, countries: list[dict],
                     window: str, extra: list[dict] | None = None) -> None:
    if summ is None and not (seen and seen.get("days")):
        b.sec("history", "Track history", summary=f"No positions in our data ({window})",
              notable=False, checked=["positions lake"])
        b.src("lake", "not_found")
        return
    b.src("lake")
    parts = []
    if summ:
        parts.append(f"{window}: {summ['fixes']} fixes, {summ['distance_km']:,} km")
    if countries:
        named = [c["name"] for c in countries if c.get("cc")]
        if named:
            parts.append(", ".join(named[:4]) + (" …" if len(named) > 4 else ""))
    if seen:
        parts.append(f"seen on {seen['days']} day(s) of the last {seen['window_days']}"
                     + (f" (data since {seen['coverage_since']})" if seen.get("coverage_since") else ""))
    b.sec("history", "Track history", notable=False, summary="; ".join(parts),
          track=summ, seen=seen, countries=countries, window=window, stops=extra or [])


# ------------------------------------------------------------------ aircraft


AIR_BASE = {
    "sanctioned": ["sanctions", "ownership", "identity", "live", "activity", "history"],
    "emergency": ["activity", "live", "identity", "ownership", "sanctions", "history"],
    "military": ["identity", "activity", "live", "ownership", "sanctions", "history"],
    "civil": ["live", "identity", "ownership", "sanctions", "activity", "history"],
}


def aircraft(ctx, hex_code: str, look: dict, local: dict) -> dict:
    """``look``: lookup results by source id; ``local``: gathered local data."""
    b = Builder("aircraft")
    tr = local.get("track") or {}
    live = local.get("live")
    ex = tr.get("extra") or {}
    pa = local.get("plane_alert")
    hx = b.lookup("hexdb", look.get("hexdb", {"status": "skipped"}))
    ad = b.lookup("adsbdb", look.get("adsbdb", {"status": "skipped"}))
    route = b.lookup("adsbdb_route", look.get("adsbdb_route", {"status": "skipped"}))
    if tr or live:
        b.src("adsb", fetched_at=tr.get("last_seen"))
    if pa:
        b.src("plane_alert")
    reg = local["reg"]
    designator = _first(tr.get("type_code"), pa and pa["icao_type"], hx and hx.get("icao_type"),
                        ad and ad.get("icao_type"))
    tdesc = ctx.types.describe(designator)
    if tdesc:
        b.src("icao8643")
    osv = local["os"]
    os_ent = osv["entries"][0] if osv["entries"] else None
    is_mil = bool(tr.get("is_mil") or pa or get_mil_category_from_type(designator)[0])
    sq = ex.get("squawk")
    squawks = local["activity"].get("squawks") or []
    emergency = sq in EMERG or any(s.get("active") for s in squawks)
    context = ("sanctioned" if os_ent else "emergency" if emergency else
               "military" if is_mil else "civil")

    # identity
    model = _first(hx and hx.get("type"), ad and ad.get("type"), pa and pa["type"], ex.get("desc"),
                   os_ent and os_ent.get("model"))
    model_src = ("hexdb" if hx and hx.get("type") else "adsbdb" if ad and ad.get("type") else
                 "plane_alert" if pa and pa["type"] else "adsb" if ex.get("desc") else "opensanctions")
    manu = _first(hx and hx.get("manufacturer"), ad and ad.get("manufacturer"))
    fields = _fields(
        F("Registration", reg, local["reg_src"]),
        F("ICAO hex", hex_code.upper(), "adsb" if tr else "hexdb"),
        F("Manufacturer", manu, "hexdb" if hx and hx.get("manufacturer") else "adsbdb"),
        F("Model", model, model_src),
        F("Type designator", designator, "adsb" if tr.get("type_code") else
          "plane_alert" if pa and pa["icao_type"] else "hexdb"),
        F("ICAO type", tdesc and tdesc["name"], "icao8643"),
        F("Class / engines", tdesc and " · ".join(x for x in (
            tdesc.get("class"), f"{tdesc['engines']} × {tdesc['engine_type']}"
            if tdesc.get("engines") and tdesc.get("engine_type") else None) if x), "icao8643"),
        F("Wake category", tdesc and tdesc.get("wtc"), "icao8643"),
        F("Serial (msn)", os_ent and os_ent.get("serial"), "opensanctions", os_ent and os_ent["url"]),
        F("Built", os_ent and os_ent.get("build"), "opensanctions", os_ent and os_ent["url"]),
        F("Operator", _first(pa and pa["operator"], tr.get("operator")), "plane_alert" if pa else "adsb"),
        F("Role (plane-alert tags)", pa and " · ".join(pa["tags"]), "plane_alert",
          pa and pa["link"] if pa and pa["link"].startswith("https://") else None),
        F("plane-alert category", pa and pa["category"], "plane_alert"),
        F("Military category", get_mil_category_from_type(designator)[1] if is_mil and not pa else None,
          "adsb"),
        F("Registered owner", _first(hx and hx.get("owner"), ad and ad.get("owner")),
          "hexdb" if hx and hx.get("owner") else "adsbdb"),
        F("Owner country", ad and ad.get("owner_country"), "adsbdb"),
    )
    if not reg:
        fields.append(F("Registration", "unknown", "adsb",
                        note="checked: live ADS-B, plane-alert-db, hexdb.io, adsbdb.com"))
    sub = " · ".join(x for x in (designator, _first(pa and pa["operator"], tr.get("operator"),
                                                     hx and hx.get("owner")), reg) if x)
    b.sec("identity", "Identity", notable=True, fields=fields,
          summary=" · ".join(x for x in (manu, model, reg) if x) or "Unknown type",
          military=is_mil, high_priority=bool(tr.get("high_priority") or is_high_priority(designator)),
          type_info=tdesc)
    if is_mil and pa and pa["tags"]:
        b.hl("info", f"{model or designator}: {' · '.join(pa['tags'])} ({pa['operator']})",
             "plane_alert", "identity")
    elif is_mil and (model or designator):
        b.hl("info", f"Military {model or designator}" + (f" ({tr['operator']})" if tr.get("operator") else ""),
             "adsb", "identity")

    # photo: planespotters first (by hex, then registration), Commons as the fallback
    ps = b.lookup("planespotters", look.get("planespotters", {"status": "skipped"}))
    cm = b.lookup("commons", look.get("commons", {"status": "skipped"})) if not ps else None
    photo = ({"source": "planespotters", **ps} if ps else {"source": "commons", **cm} if cm else None)
    loading = not photo and "pending" in (b.sources.get("planespotters", {}).get("status"),
                                          b.sources.get("commons", {}).get("status"))
    b.sec("photo", "Photo", notable=bool(photo), photo=photo,
          summary="" if photo else "Looking for a photo…" if loading else
          "No photo found (checked: Planespotters.net by hex / registration"
          + (", Wikimedia Commons by registration" if reg else "") + ")")

    # sanctions + ownership
    checked = [f"OpenSanctions graph by registration {reg}" if reg else
               "OpenSanctions (no registration known to match on)",
               "user watchlist" + (" (matched)" if local["user_hits"] else "")]
    _sanctions_section(b, osv, checked=checked, extra_listed=[], user_hits=local["user_hits"],
                       weak_hits=local.get("weak_hits") or [], label="Aircraft")
    if osv["entries"] or local["graph_ok"]:
        b.src("opensanctions", fetched_at=local.get("graph_built"))
    own_rows = _fields(
        F("Registered owner", _first(hx and hx.get("owner")), "hexdb"),
        F("Registered owner", ad and ad.get("owner") if not (hx and hx.get("owner")) or
          (ad and ad.get("owner") and ad.get("owner") != hx.get("owner")) else None, "adsbdb"),
        F("Operator", pa and pa["operator"], "plane_alert"),
        F("Operator ICAO code", _first(hx and hx.get("operator_flag")), "hexdb"),
    )
    _ownership_section(b, osv, own_rows, "aircraft",
                       ["OpenSanctions ownership links", "hexdb.io / adsbdb.com registered owner"])

    # live flight
    lv = []
    if live:
        lv = _fields(
            F("Callsign", tr.get("name"), "adsb"),
            F("Altitude", f"{live['alt_ft']:,} ft" if live.get("alt_ft") is not None else None, "adsb"),
            F("Ground speed", f"{live['speed_kts']:.0f} kt" if live.get("speed_kts") is not None else None, "adsb"),
            F("Heading", f"{live['heading_deg']:.0f}°" if live.get("heading_deg") is not None else None, "adsb"),
            F("Squawk", (f"{sq} - {EMERG[sq]}" if sq in EMERG else sq) if sq else None, "adsb"),
            F("Position", f"{live['lat']:.4f}, {live['lon']:.4f}", "adsb"),
            F("Last fix", live.get("ts"), "adsb"),
            F("Feed", live.get("source"), "adsb"),
        )
    rt = None
    if route and (route.get("origin") or route.get("destination")):
        o, d = route.get("origin") or {}, route.get("destination") or {}
        rt = {"origin": o, "destination": d, "airline": route.get("airline"),
              "caveat": "Route usually flown under this callsign (route database), not a filed "
                        "flight plan"}
        lv.append(F("Route", f"{o.get('icao_code') or '?'} {o.get('municipality') or ''} → "
                    f"{d.get('icao_code') or '?'} {d.get('municipality') or ''}".strip(), "adsbdb_route"))
        if route.get("airline"):
            lv.append(F("Airline", route["airline"].get("name"), "adsbdb_route"))
        if context == "civil":
            b.hl("info", f"{(route.get('airline') or {}).get('name') or tr.get('name')}: "
                 f"{o.get('municipality') or o.get('icao_code')} → {d.get('municipality') or d.get('icao_code')}",
                 "adsbdb_route", "live")
    summary = ("Not currently tracked" if not live else
               " · ".join(x for x in (tr.get("name"), f"{live['alt_ft']:,} ft" if live.get("alt_ft") is not None else None,
                                      f"{live['speed_kts']:.0f} kt" if live.get("speed_kts") is not None else None) if x))
    sev = "high" if sq in ("7500", "7700") else "warn" if sq == "7600" else "none"
    if sq in EMERG:
        b.hl(sev, f"Squawking {sq} ({EMERG[sq]}) now", "adsb", "live")
    b.sec("live", "Current flight", severity=sev, notable=bool(live), summary=summary,
          fields=lv, route=rt, live=live)

    # history + activity
    _history_section(b, local.get("summary"), local.get("seen"), local.get("countries") or [],
                     "last 24 h")
    _activity_items(b, local["activity"], "aircraft")
    if local["activity"].get("squawks"):
        b.src("signals")
    if local["activity"].get("frontline"):
        b.src("frontline")
    if local["activity"].get("events"):
        b.src("events")
    title = _first(reg, tr.get("name"), hex_code.upper())
    order = b.order(AIR_BASE[context])
    return b.result(kind="aircraft", id=f"air:{hex_code}", context=context, order=order,
                    key={"hex": hex_code, "reg": reg, "callsign": tr.get("name")},
                    title=title, subtitle=" · ".join(x for x in (model, sub) if x))


# ------------------------------------------------------------------ vessel


SEA_BASE = {
    "sanctioned": ["sanctions", "ownership", "identity", "activity", "live", "history"],
    "listed": ["sanctions", "ownership", "activity", "identity", "live", "history"],
    "plain": ["identity", "live", "activity", "history", "ownership", "sanctions"],
}


def vessel(ctx, mmsi: str | None, look: dict, local: dict) -> dict:
    b = Builder("vessel")
    tr = local.get("track") or {}
    ex = tr.get("extra") or {}
    live = local.get("live")
    if tr or live:
        b.src("ais", fetched_at=tr.get("last_seen"))
    osv = local["os"]
    os_ent = osv["entries"][0] if osv["entries"] else None
    mrows = local["maritime"]
    if mrows:
        b.src("os_maritime", fetched_at=local.get("maritime_at"))
    imo = local["imo"]
    sanctioned = any("sanction" in e["topics"] for e in osv["entries"])
    listed = bool(osv["entries"] or mrows or local["user_hits"])
    context = "sanctioned" if sanctioned else "listed" if listed else "plain"

    name = _first(tr.get("name"), os_ent and os_ent["name"], mrows and mrows[0]["name"])
    prev = list(dict.fromkeys((os_ent["previous_names"] if os_ent else [])
                              + [a for r in mrows for a in r["aliases"]]))
    prev = [p for p in prev if (p or "").casefold() != (name or "").casefold()]
    past_flags = os_ent["past_flags"] if os_ent else []
    flag = _first(os_ent and os_ent.get("flag"), mrows and mrows[0]["flag"])
    fields = _fields(
        F("Name (AIS)", tr.get("name"), "ais"),
        F("Listed name", os_ent and os_ent["name"] if os_ent and os_ent["name"] != tr.get("name") else None,
          "opensanctions", os_ent and os_ent["url"]),
        F("IMO", imo, local["imo_src"]),
        F("MMSI", mmsi, "ais" if tr else "opensanctions"),
        F("Call sign", _first(ex.get("callsign"), os_ent and os_ent.get("callsign")),
          "ais" if ex.get("callsign") else "opensanctions"),
        F("Flag", flag.upper() if flag else None, "opensanctions" if os_ent and os_ent.get("flag") else "os_maritime"),
        F("Type (AIS)", tr.get("category"), "ais"),
        F("Type (listing)", os_ent and os_ent.get("type"), "opensanctions"),
        F("Gross tonnage", os_ent and os_ent.get("gross_tonnage"), "opensanctions"),
        F("Deadweight", os_ent and os_ent.get("deadweight") and f"{os_ent['deadweight']} t", "opensanctions"),
        F("Built", os_ent and os_ent.get("build"), "opensanctions"),
        F("Former names", ", ".join(prev[:12]) if prev else None, "opensanctions"),
        F("Former flags", ", ".join(f.upper() for f in past_flags) if past_flags else None, "opensanctions"),
    )
    if not imo:
        fields.append(F("IMO", "unknown", "ais", note="checked: AIS static data (type 5), "
                        "OpenSanctions by MMSI"))
    fields.append(F("Dimensions", "unknown", "ais",
                    note="not in our AIS feed's static data (AISStream ShipStaticData dimensions "
                    "are not stored)"))
    b.sec("identity", "Identity", notable=True, fields=fields,
          summary=" · ".join(x for x in (name, f"IMO {imo}" if imo else None, tr.get("category")) if x))
    if prev or past_flags:
        sev = "warn" if listed else "info"
        b.hl(sev, f"{len(prev)} former name(s)" + (f" ({', '.join(prev[:4])}{'…' if len(prev) > 4 else ''})" if prev else "")
             + (f", {len(past_flags)} former flag(s) ({', '.join(f.upper() for f in past_flags)})" if past_flags else ""),
             "opensanctions", "identity")
    ais_name = (tr.get("name") or "").strip()
    if ais_name and os_ent and ais_name.casefold() != (os_ent["name"] or "").casefold():
        was_prev = any(ais_name.casefold() == p.casefold() for p in os_ent["previous_names"])
        b.hl("warn", f"AIS name {ais_name!r} differs from the listed name {os_ent['name']!r}"
             + (" (a former name)" if was_prev else ""), "opensanctions", "identity")

    cm = b.lookup("commons", look.get("commons", {"status": "skipped"}))
    photo = {"source": "commons", **cm} if cm else None
    b.sec("photo", "Photo", notable=bool(photo), photo=photo,
          summary="" if photo else "Looking for a photo…" if (look.get("commons") or {}).get("status")
          == "pending" else ("No photo found (checked: Wikimedia Commons files titled with "
                                    f"IMO {imo})" if imo else "No photo (no IMO number to search by)"))

    checked = [f"OpenSanctions graph by IMO {imo}" if imo else "OpenSanctions graph (no IMO)",
               f"OpenSanctions graph / maritime.csv by MMSI {mmsi}" if mmsi else "",
               "user watchlist"]
    _sanctions_section(b, osv, checked=[c for c in checked if c], extra_listed=mrows,
                       user_hits=local["user_hits"], weak_hits=local.get("weak_hits") or [],
                       label="Vessel")
    if local["graph_ok"]:
        b.src("opensanctions", fetched_at=local.get("graph_built"))
    _ownership_section(b, osv, [], "vessel",
                       ["OpenSanctions sanctions + War & Sanctions ownership / manager links"])

    lv = []
    if live:
        lv = _fields(
            F("Speed", f"{live['speed_kts']:.1f} kt" if live.get("speed_kts") is not None else None, "ais"),
            F("Heading", f"{live['heading_deg']:.0f}°" if live.get("heading_deg") is not None else None, "ais"),
            F("Destination (AIS)", ex.get("destination"), "ais"),
            F("Position", f"{live['lat']:.4f}, {live['lon']:.4f}", "ais"),
            F("Last fix", live.get("ts"), "ais"),
        )
    stops = local.get("stops") or []
    last_port = next((s for s in reversed(stops) if s.get("port")), None)
    if last_port:
        lv.append(F("Last port call (ours)", f"{last_port['port']} ({last_port['from'][:10]})", "lake",
                    note=f"stationary {last_port['from'][11:16]}–{last_port['to'][11:16]}Z within "
                    f"{last_port['port_km']} km of the port (WPI / Natural Earth)"))
    b.sec("live", "Current voyage", notable=bool(live), fields=lv,
          summary=" · ".join(x for x in (f"{live['speed_kts']:.1f} kt" if live and live.get("speed_kts") is not None else None,
                                          f"→ {ex['destination']}" if ex.get("destination") else None) if x)
          or ("Not currently tracked" if not live else "Live"))

    _history_section(b, local.get("summary"), local.get("seen"), [], "last 7 days", stops)
    _activity_items(b, local["activity"], "vessel")
    for k in ("infra_watch", "events"):
        if local["activity"].get({"infra_watch": "alerts", "events": "events"}[k]):
            b.src(k)
    order = b.order(SEA_BASE[context])
    return b.result(kind="vessel", id=f"sea:{mmsi}" if mmsi else f"imo:{imo}", context=context,
                    order=order, key={"mmsi": mmsi, "imo": imo, "name": name},
                    title=name or (f"MMSI {mmsi}" if mmsi else f"IMO {imo}"),
                    subtitle=" · ".join(x for x in (tr.get("category"), f"IMO {imo}" if imo else None,
                                                     f"MMSI {mmsi}" if mmsi else None,
                                                     flag.upper() if flag else None) if x))


# ------------------------------------------------------------------ local data gathering


def weak_hits_from(active: dict | None, strong_ids: set[str]) -> list[dict]:
    """Name-only sanctions hits of the watch job (``strength == "weak"``, with ``doubts``),
    minus entities already matched by identifier."""
    out = []
    for h in (active or {}).get("hits") or []:
        e = h.get("entry") or {}
        if e.get("source") == "watchlist" or h.get("strength") != "weak" or e.get("id") in strong_ids:
            continue
        out.append({"id": e.get("id"), "name": e.get("name"), "url": e.get("url"),
                    "risk": e.get("risk") or [], "programs": e.get("programs") or [],
                    "imo": e.get("imo"), "mmsi": e.get("mmsi"), "matched_on": h.get("matched_on"),
                    "doubts": h.get("doubts") or []})
    return out


def user_hits_from(active: dict | None) -> list[dict]:
    out = []
    for h in (active or {}).get("hits") or []:
        e = h.get("entry") or {}
        if e.get("source") == "watchlist":
            out.append({"label": e.get("label"), "name": e.get("name"), "note": e.get("note"),
                        "severity": e.get("severity"), "matched_on": h.get("matched_on")})
    return out


def gather_aircraft(ctx, hex_code: str, look_reg: str | None) -> dict:
    """Everything local for an aircraft (runs in a worker thread)."""
    rt = ctx.runtime
    tid = f"air:{hex_code}"
    tr = rt.store.get_tracks([tid]).get(tid) if rt is not None else None
    p = rt.ingestor.live.get(tid) if rt is not None else None
    live = None if p is None else {"ts": p.ts.isoformat(), "lat": p.lat, "lon": p.lon,
                                   "alt_ft": p.alt_ft, "speed_kts": p.speed_kts,
                                   "heading_deg": p.heading_deg, "source": p.source}
    pa = ctx.plane_alert.get(hex_code)
    reg, reg_src = None, None
    for v, s in (((tr or {}).get("reg"), "adsb"), (pa and pa["reg"], "plane_alert"), (look_reg, "hexdb")):
        if v:
            reg, reg_src = v, s
            break
    ids = ctx.graph.find("reg", reg) if reg else []
    osv = os_asset_view(ctx.graph, [i for i in ids], "reg") if ids else {"entries": [], "links": []}
    # an Airplane entity matched by registration only; a registration re-used by another
    # airframe would show a different serial / model, which the card shows next to ours
    pts = bh.lake_points(rt, tid, 24) if rt is not None else []
    act = {"window": "last 24 h"}
    act["squawks"] = bh.squawk_history(bh.read_json(ctx.signals_dir / "squawks.json"), hex_code)
    act["orbit"] = bh.orbit(pts)
    act["frontline"] = bh.frontline_distance(pts, ctx.frontline_lines())
    act["gnss"] = bh.gnss_hits(pts, bh.read_json(ctx.signals_dir / "gnss.json"))
    act["events"] = (bh.events_around(rt, pts, 30.0, 24, ((tr or {}).get("name") or "", reg or "", hex_code))
                     if rt is not None else [])
    active = watch_active_row(ctx.watch_path, tid)
    return {"track": tr, "live": live, "plane_alert": pa, "reg": reg, "reg_src": reg_src or "adsb",
            "os": osv, "graph_ok": ctx.graph.available, "graph_built": ctx.graph_built(),
            "summary": bh.summarize(pts), "seen": bh.days_seen(rt, tid, 30) if rt is not None else None,
            "countries": bh.countries_crossed(pts, bh.country_index()), "activity": act,
            "user_hits": user_hits_from(active),
            "weak_hits": weak_hits_from(active, {e["id"] for e in osv["entries"]})}


def gather_vessel(ctx, mmsi: str | None, imo_hint: str | None) -> dict:
    rt = ctx.runtime
    tid = f"sea:{mmsi}" if mmsi else None
    tr = rt.store.get_tracks([tid]).get(tid) if rt is not None and tid else None
    p = rt.ingestor.live.get(tid) if rt is not None and tid else None
    live = None if p is None else {"ts": p.ts.isoformat(), "lat": p.lat, "lon": p.lon,
                                   "speed_kts": p.speed_kts, "heading_deg": p.heading_deg,
                                   "source": p.source}
    ais_imo = norm_imo((tr or {}).get("extra", {}).get("imo"))
    imo, imo_src = (ais_imo, "ais") if ais_imo else (imo_hint, "opensanctions") if imo_hint else (None, "ais")
    ids, how = [], None
    if imo:
        ids, how = ctx.graph.find("imo", imo), "imo"
    if not ids and mmsi:
        cand = ctx.graph.find("mmsi", mmsi)
        ents = ctx.graph.entities(cand)
        # an MMSI hit whose IMO differs from the AIS IMO is another hull: no match
        ids = [i for i in cand if not (imo and (ents.get(i, {}).get("props") or {}).get("imoNumber")
                                       and norm_imo(ents[i]["props"]["imoNumber"][0]) != imo)]
        how = "mmsi"
    osv = os_asset_view(ctx.graph, ids, how or "imo") if ids else {"entries": [], "links": []}
    if not imo and osv["entries"] and osv["entries"][0].get("imo"):
        imo, imo_src = norm_imo(osv["entries"][0]["imo"]), "opensanctions"
    mrows, _ = ctx.maritime.find(imo, mmsi)
    # an MMSI-only maritime.csv row naming another IMO is another hull (re-used MMSI)
    mrows = [r for r in mrows if not (imo and r.get("imo") and r["imo"] != imo)]
    pts = bh.lake_points(rt, tid, 24 * 7) if rt is not None and tid else []
    act = {"window": "last 7 days"}
    act["gaps"] = bh.gaps(pts, 6.0)
    act["jumps"] = bh.jumps(pts, 50.0)
    act["alerts"] = bh.watch_alerts(ctx.watch_path, tid) if tid else []
    act["events"] = (bh.events_around(rt, pts, 50.0, 24 * 7, ((tr or {}).get("name") or "", mmsi or ""))
                     if rt is not None else [])
    ports = ctx.ports()
    active = watch_active_row(ctx.watch_path, tid) if tid else None
    return {"track": tr, "live": live, "imo": imo, "imo_src": imo_src, "os": osv,
            "graph_ok": ctx.graph.available, "graph_built": ctx.graph_built(),
            "maritime": mrows, "maritime_at": ctx.maritime_mtime(),
            "summary": bh.summarize(pts), "seen": bh.days_seen(rt, tid, 30) if rt is not None and tid else None,
            "stops": bh.stops(pts, ports), "activity": act, "user_hits": user_hits_from(active),
            "weak_hits": weak_hits_from(active, {e["id"] for e in osv["entries"]})}


class Context:
    """Shared, lazily loaded references for profile building."""

    def __init__(self, runtime, ref_dir: Path, data_dir: Path, plane_alert_csv: Path) -> None:
        from osint.profiles.actypes import FILE as TYPES_FILE
        from osint.profiles.actypes import TypeTable
        from osint.profiles.osgraph import FILE as GRAPH_FILE
        from osint.profiles.osgraph import Graph

        self.runtime = runtime
        self.ref_dir = ref_dir
        self.graph = Graph(ref_dir / GRAPH_FILE)
        self.types = TypeTable(ref_dir / TYPES_FILE)
        self.plane_alert = PlaneAlert(plane_alert_csv)
        self.maritime = MaritimeCsv(ref_dir / "os_maritime.csv")
        self.signals_dir = data_dir / "signals"
        self.watch_path = data_dir / "infra_watch.sqlite"
        self._fl: tuple[float, list] | None = None
        self._gb: tuple[float, str | None] | None = None
        self._ports: list[dict] | None = None

    def graph_built(self) -> str | None:
        now = time.monotonic()
        if self._gb is None or now - self._gb[0] > 300:
            built = self.graph.meta().get("built_at")
            self._gb = (now, datetime.fromtimestamp(built, UTC).isoformat(timespec="seconds")
                        if built else None)
        return self._gb[1]

    def maritime_mtime(self) -> str | None:
        try:
            return datetime.fromtimestamp((self.ref_dir / "os_maritime.csv").stat().st_mtime,
                                          UTC).isoformat(timespec="seconds")
        except OSError:
            return None

    def frontline_lines(self) -> list:
        now = time.monotonic()
        if self._fl is None or now - self._fl[0] > 3600:
            from osint.fires import frontline

            self._fl = (now, frontline.lines_latlon(frontline.load(self.ref_dir)))
        return self._fl[1]

    def ports(self) -> list[dict]:
        if self._ports is None:
            try:
                from osint.reference.loader import get_reference

                self._ports = get_reference().ports
            except Exception as exc:  # noqa: BLE001
                log.info("ports unavailable: %s", exc)
                self._ports = []
        return self._ports


_HEX = re.compile(r"^[0-9a-f]{6}$")


def parse_aircraft_key(raw: str) -> tuple[str | None, str | None]:
    """'43c38c' / 'air:43c38c' -> (hex, None); 'reg:RA-85041' -> (None, 'RA85041' as typed)."""
    s = raw.strip()
    if s.lower().startswith("air:"):
        s = s[4:]
    if s.lower().startswith("reg:"):
        return None, s[4:].strip().upper() or None
    s = s.lower()
    return (s, None) if _HEX.match(s) else (None, None)


def parse_vessel_key(raw: str) -> tuple[str | None, str | None]:
    """'538012620' / 'sea:538012620' -> (mmsi, None); 'imo:9336414' -> (None, '9336414')."""
    s = raw.strip()
    if s.lower().startswith("sea:"):
        s = s[4:]
    if s.lower().startswith("imo:"):
        return None, norm_imo(s[4:])
    return (s, None) if re.fullmatch(r"\d{9}", s) else (None, None)


__all__ = [
    "Context",
    "aircraft",
    "gather_aircraft",
    "gather_vessel",
    "norm_reg",
    "parse_aircraft_key",
    "parse_vessel_key",
    "vessel",
]
