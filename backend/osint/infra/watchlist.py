"""Watchlists: an OpenSanctions index of sanctioned / high-risk vessels and aircraft plus the
user-editable ``config/watchlist.yaml``, matched against live AIS / ADS-B tracks.

Match strength: ``imo`` and ``reg`` (aircraft registration) are strong identifiers; ``mmsi``
is strong but MMSIs get re-used after a vessel is re-flagged; ``name`` (exact normalised
vessel name, distinctive names only) is weak and never alerts unless configured to.
"""

import csv
import fnmatch
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

OS_LICENCE = "OpenSanctions (CC BY-NC 4.0, non-commercial)"
# OpenSanctions topic -> (severity, label); unknown topics are ignored
RISK = {
    "sanction": ("high", "Sanctioned"),
    "mare.shadow": ("high", "Shadow fleet"),
    "poi": ("warn", "Person/vessel of interest"),
    "sanction.linked": ("warn", "Sanctions-linked"),
    "mare.detained": ("info", "Port-state detention"),
    "reg.warn": ("info", "Regulator warning"),
}
SEV_ORDER = {"high": 0, "warn": 1, "info": 2}
_IMO = re.compile(r"(?:IMO)?\s*(\d{7})$")
_MMSI = re.compile(r"^\d{9}$")
_REG = re.compile(r"^[A-Z0-9]{1,3}-?[A-Z0-9]{2,5}$")
_NAME_STOP = {"MT", "MV", "M/T", "M/V", "SS", "THE"}


def norm_imo(v: str | None) -> str | None:
    m = _IMO.match((v or "").strip().upper())
    return m.group(1) if m else None


def norm_reg(v: str | None) -> str | None:
    s = re.sub(r"[\s.]", "", (v or "").upper())
    if not _REG.match(s) or not re.search(r"[A-Z]", s):
        return None
    return s.replace("-", "")


def norm_name(v: str | None) -> str:
    toks = re.findall(r"[A-Z0-9]+", (v or "").upper())
    return " ".join(t for t in toks if t not in _NAME_STOP)


def distinctive(name: str) -> bool:
    """Names worth a (weak) name-only match: multi-word or numbered names, or long single
    words. Common single names (CHRISTINA, PROSPERITY) are shared by many hulls."""
    return len(name) >= 6 and (" " in name or any(c.isdigit() for c in name) or len(name) >= 12)


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(";") if x.strip()]


@dataclass
class Entry:
    id: str
    kind: str  # "vessel" | "aircraft"
    name: str
    source: str = "opensanctions"
    aliases: list[str] = field(default_factory=list)
    imo: str | None = None
    mmsi: str | None = None
    regs: list[str] = field(default_factory=list)
    flag: str | None = None
    topics: list[str] = field(default_factory=list)
    programs: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    sanctions: str | None = None
    url: str | None = None
    label: str | None = None  # user watchlist label
    note: str | None = None
    severity_override: str | None = None

    @property
    def severity(self) -> str:
        if self.severity_override:
            return self.severity_override
        sevs = [RISK[t][0] for t in self.topics if t in RISK]
        return min(sevs, key=SEV_ORDER.__getitem__) if sevs else "info"

    @property
    def risk_labels(self) -> list[str]:
        known = sorted((t for t in self.topics if t in RISK), key=lambda t: SEV_ORDER[RISK[t][0]])
        return [RISK[t][1] for t in known]  # most severe first (it labels the globe ring)

    def public(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v not in (None, [], "") and k != "severity_override"}
        d["severity"] = self.severity
        d["risk"] = self.risk_labels
        if self.source == "opensanctions":
            d["licence"] = OS_LICENCE
            d["commercial_ok"] = False
        return d


class SanctionsIndex:
    def __init__(self, maritime_csv: Path, targets_csv: Path,
                 topics: set[str] | None = None) -> None:
        self.topics = topics if topics is not None else {"sanction", "mare.shadow", "poi",
                                                          "sanction.linked"}
        self.entries: dict[str, Entry] = {}
        self.by_imo: dict[str, Entry] = {}
        self.by_mmsi: dict[str, Entry] = {}
        self.by_name: dict[str, list[Entry]] = {}
        self.by_reg: dict[str, Entry] = {}
        csv.field_size_limit(1 << 26)
        self._load_targets(targets_csv)
        self._load_maritime(maritime_csv)
        self._index()
        log.info("sanctions index: %s", self.summary())

    def _load_targets(self, path: Path) -> None:
        if not path.is_file():
            log.warning("watchlist file missing: %s", path)
            return
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                schema = r.get("schema")
                ids = _split(r.get("identifiers"))
                e = Entry(id=r["id"], kind="vessel" if schema == "Vessel" else "aircraft",
                          name=r.get("name") or r["id"], aliases=_split(r.get("aliases"))[:8],
                          topics=["sanction"], programs=_split(r.get("program_ids")),
                          datasets=_split(r.get("dataset")),
                          sanctions=(r.get("sanctions") or "")[:600] or None,
                          url=f"https://www.opensanctions.org/entities/{r['id']}/")
                if schema == "Vessel":
                    e.imo = next((norm_imo(x) for x in ids if x.upper().startswith("IMO")), None)
                    e.mmsi = next((x for x in ids if _MMSI.match(x)), None)
                elif schema == "Airplane":
                    regs = [norm_reg(x) for x in [e.name, *ids, *e.aliases]]
                    e.regs = sorted({x for x in regs if x})
                else:
                    continue
                self.entries[e.id] = e

    def _load_maritime(self, path: Path) -> None:
        if not path.is_file():
            log.warning("watchlist file missing: %s", path)
            return
        by_imo = {e.imo: e for e in self.entries.values() if e.imo}
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if (r.get("type") or "").upper() != "VESSEL":
                    continue
                topics = _split(r.get("risk"))
                imo, mmsi = norm_imo(r.get("imo")), (r.get("mmsi") or "").strip() or None
                e = self.entries.get(r.get("id") or "") or (by_imo.get(imo) if imo else None)
                if e is None:
                    if not set(topics) & self.topics:
                        continue
                    e = Entry(id=r["id"], kind="vessel", name=r.get("caption") or r["id"],
                              aliases=_split(r.get("aliases"))[:8],
                              datasets=_split(r.get("datasets")), url=r.get("url") or None)
                    self.entries[e.id] = e
                e.topics = sorted(set(e.topics) | set(topics))
                e.imo = e.imo or imo
                e.mmsi = e.mmsi or (mmsi if mmsi and _MMSI.match(mmsi) else None)
                e.flag = e.flag or (r.get("flag") or None)

    def _index(self) -> None:
        for e in self.entries.values():
            if e.kind == "vessel":
                if e.imo:
                    self.by_imo.setdefault(e.imo, e)
                if e.mmsi:
                    self.by_mmsi.setdefault(e.mmsi, e)
                for n in {norm_name(x) for x in [e.name, *e.aliases]}:
                    if n and distinctive(n):
                        self.by_name.setdefault(n, []).append(e)
            else:
                for r in e.regs:
                    self.by_reg.setdefault(r, e)

    def summary(self) -> dict:
        v = sum(1 for e in self.entries.values() if e.kind == "vessel")
        return {"vessels": v, "aircraft": len(self.entries) - v, "imo": len(self.by_imo),
                "mmsi": len(self.by_mmsi), "registrations": len(self.by_reg)}


class UserWatchlist:
    """config/watchlist.yaml, reloaded when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sig: tuple | None = None
        self.entries: list[dict] = []
        self.error: str | None = None
        self.reload()

    def reload(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._sig, self.entries, self.error = None, [], None
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        self._sig = sig
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            rows = data.get("entries") or []
            self.entries = [self._norm(r) for r in rows if isinstance(r, dict)]
            self.entries = [r for r in self.entries if r]
            self.error = None
        except (OSError, yaml.YAMLError, AttributeError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("watchlist.yaml unreadable: %s", exc)

    @staticmethod
    def _norm(r: dict) -> dict | None:
        out = {k: str(r[k]).strip() for k in ("mmsi", "icao", "callsign", "imo", "reg", "label",
                                              "note", "severity") if r.get(k) not in (None, "")}
        if "icao" in out:
            out["icao"] = out["icao"].lower()
        if "callsign" in out:
            out["callsign"] = out["callsign"].upper()
        if "imo" in out:
            out["imo"] = norm_imo(out["imo"]) or out["imo"]
        if "reg" in out:
            out["reg"] = norm_reg(out["reg"]) or out["reg"].upper().replace("-", "")
        if out.get("severity") not in SEV_ORDER:
            out["severity"] = "warn"
        return out if set(out) & {"mmsi", "icao", "callsign", "imo", "reg"} else None

    def match(self, kind: str, ident: str, meta: dict) -> list[tuple[dict, str]]:
        extra = meta.get("extra") or {}
        callsigns = [x.upper() for x in (meta.get("name") if kind == "air" else extra.get("callsign"),)
                     if x]
        imo = norm_imo(extra.get("imo"))
        reg = norm_reg(meta.get("reg"))
        out = []
        for r in self.entries:
            if kind == "sea" and r.get("mmsi") == ident:
                out.append((r, "mmsi"))
            elif kind == "air" and r.get("icao") == ident:
                out.append((r, "icao"))
            elif kind == "sea" and imo and r.get("imo") == imo:
                out.append((r, "imo"))
            elif kind == "air" and reg and r.get("reg") == reg:
                out.append((r, "reg"))
            elif r.get("callsign") and any(fnmatch.fnmatchcase(c, r["callsign"]) for c in callsigns):
                out.append((r, "callsign"))
        return out


class Matcher:
    """Matches one track (kind, ident, metadata dict as stored in ``tracks``) against both
    lists. Returns hit dicts, strongest first."""

    def __init__(self, sanctions: SanctionsIndex | None, user: UserWatchlist | None,
                 name_matches: bool = True) -> None:
        self.sanctions = sanctions
        self.user = user
        self.name_matches = name_matches
        self._lock = threading.Lock()

    def match(self, track_id: str, meta: dict | None) -> list[dict]:
        kind, _, ident = track_id.partition(":")
        meta = meta or {}
        hits: list[dict] = []
        s = self.sanctions
        if s is not None:
            extra = meta.get("extra") or {}
            if kind == "sea":
                imo = norm_imo(extra.get("imo"))
                # an IMO on both sides that differs means another hull: an MMSI hit is then a
                # re-used MMSI (weak) and a name hit is no match at all
                clash = lambda e: bool(imo and e.imo and e.imo != imo)
                cand = [(s.by_imo.get(imo) if imo else None, "imo"), (s.by_mmsi.get(ident), "mmsi")]
                for e, how in cand:
                    if e is not None and not any(h["entry"]["id"] == e.id for h in hits):
                        hits.append(self._hit(e.public(), how, "weak" if clash(e) else "strong"))
                if not hits and self.name_matches:
                    n = norm_name(meta.get("name"))
                    for e in s.by_name.get(n, [])[:3] if n and distinctive(n) else []:
                        if not clash(e):
                            hits.append(self._hit(e.public(), "name", "weak"))
            elif kind == "air":
                reg = norm_reg(meta.get("reg"))
                e = s.by_reg.get(reg) if reg else None
                if e is not None:
                    hits.append(self._hit(e.public(), "reg", "strong"))
        if self.user is not None:
            for r, how in self.user.match(kind, ident, meta):
                hits.append(self._hit({"id": "user:" + (r.get("label") or how), "source": "watchlist",
                                       "kind": "vessel" if kind == "sea" else "aircraft",
                                       "name": r.get("label") or ident, "label": r.get("label"),
                                       "note": r.get("note"), "severity": r["severity"],
                                       "risk": ["User watchlist"]}, how, "strong"))
        hits.sort(key=lambda h: (h["strength"] != "strong", SEV_ORDER[h["entry"]["severity"]]))
        return hits

    @staticmethod
    def _hit(entry: dict, how: str, strength: str) -> dict:
        return {"entry": entry, "matched_on": how, "strength": strength}
