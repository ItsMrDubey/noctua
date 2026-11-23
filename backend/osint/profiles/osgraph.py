"""OpenSanctions vessel / aircraft entity graph: assets, their owners / managers / operators
(up the ownership chain), and every sanction record on any of them.

The CSVs the watchlist matcher uses (``os_maritime.csv``, ``os_targets_va.csv``) hold no
relationships and only a flattened sanctions string. The FollowTheMoney exports do: Vessel /
Airplane entities, Ownership / UnknownLink / Directorship / Representation edges and Sanction
records (authority, programme, reason, listing dates, source URLs). Two exports are read:

* ``sanctions`` collection ``entities.ftm.json`` (~380 MB): every sanctions list.
* ``ua_war_sanctions`` (~35 MB): Ukraine GUR "War & Sanctions" shadow-fleet register: vessels
  with former names / flags and dated owner, commercial manager and ISM (security) manager
  links that are often not on any government list yet.

(The ``maritime`` collection only publishes ``maritime.csv`` now; its ``entities.ftm.json``
is a stale 2025 artifact and is not used.)

Both are streamed to a temporary file and read twice: pass 1 keeps the assets and all edges
(a few tens of thousands), pass 2 the parties reachable from an asset (up to ``MAX_HOPS``
ownership levels) and the sanction records of assets and parties. The result is a small
SQLite file (``os_assets.sqlite``, ~30 MB) the API opens read-only: lookups by IMO / MMSI /
registration / entity id never load the graph into memory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

FILE = "os_assets.sqlite"
FTM_URLS = {
    "sanctions": "https://data.opensanctions.org/datasets/latest/sanctions/entities.ftm.json",
    "ua_war_sanctions":
        "https://data.opensanctions.org/datasets/latest/ua_war_sanctions/entities.ftm.json",
}
INDEX_URL = "https://data.opensanctions.org/datasets/latest/{name}/index.json"
ENTITY_URL = "https://www.opensanctions.org/entities/{id}/"
ASSET_SCHEMAS = {"Vessel", "Airplane"}
PARTY_SCHEMAS = {"Person", "Company", "Organization", "LegalEntity", "PublicBody"}
# edge schema -> (property holding the "from" side, property holding the "to" side)
EDGE_SCHEMAS = {
    "Ownership": ("owner", "asset"),
    "UnknownLink": ("subject", "object"),
    "Directorship": ("director", "organization"),
    "Representation": ("agent", "client"),
}
MAX_HOPS = 3  # asset <- owner <- owner's parent <- grandparent
# an UnknownLink "child -> parent" (subject is the controlled side)
_PARENT_ROLE = re.compile(r"owned or controlled|subsidiary|parent|acting for or on behalf",
                          re.IGNORECASE)
_IMO = re.compile(r"(\d{7})")
_REG_NORM = re.compile(r"[\s.\-]")

ASSET_PROPS = ("name", "alias", "previousName", "pastFlags", "flag", "imoNumber", "mmsi",
               "callSign", "type", "buildDate", "tonnage", "grossRegisteredTonnage",
               "deadweightTonnage", "registrationNumber", "model", "serialNumber", "country",
               "topics", "programId", "notes", "description", "sourceUrl", "owner", "operator")
PARTY_PROPS = ("name", "alias", "country", "jurisdiction", "nationality", "topics", "imoNumber",
               "registrationNumber", "innCode", "ogrnCode", "incorporationDate", "birthDate",
               "address", "notes", "sourceUrl", "programId", "website", "position")
SANCTION_PROPS = ("authority", "program", "programId", "programUrl", "reason", "provisions",
                  "summary", "startDate", "endDate", "listingDate", "status", "sourceUrl",
                  "country", "authorityId", "unscId")
# per-property value caps (lists) and text caps (characters)
LIST_CAP = {"alias": 10, "sourceUrl": 6, "address": 2, "provisions": 4, "previousName": 20}
TEXT_CAP = {"notes": 1500, "description": 2500, "reason": 900, "summary": 400}


def norm_reg(v: str | None) -> str:
    return _REG_NORM.sub("", (v or "").upper())


def norm_imo(v: str | None) -> str | None:
    m = _IMO.search(v or "")
    return m.group(1) if m else None


def _compact(props: dict, keep: tuple[str, ...]) -> dict:
    out: dict[str, list] = {}
    for k in keep:
        vals = [str(v) for v in props.get(k) or [] if v not in (None, "")]
        if not vals:
            continue
        seen, uniq = set(), []
        for v in vals:
            v = v[: TEXT_CAP[k]] + "…" if k in TEXT_CAP and len(v) > TEXT_CAP[k] else v
            if v.casefold() not in seen:
                seen.add(v.casefold())
                uniq.append(v)
        out[k] = uniq[: LIST_CAP.get(k, 30)]
    return out


def _meta(e: dict) -> dict:
    return {k: e[k] for k in ("datasets", "first_seen", "last_seen", "last_change") if e.get(k)}


def iter_ftm(paths: Iterable[Path]) -> Iterator[dict]:
    for p in paths:
        with Path(p).open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def _merge(into: dict, e: dict) -> None:
    """The same canonical entity can come from both exports: union its properties."""
    props = into.setdefault("properties", {})
    for k, vals in (e.get("properties") or {}).items():
        cur = props.setdefault(k, [])
        cur.extend(v for v in vals if v not in cur)
    for k in ("datasets", "referents"):
        cur = into.setdefault(k, [])
        cur.extend(v for v in e.get(k) or [] if v not in cur)
    for k in ("first_seen",):
        if e.get(k) and (not into.get(k) or e[k] < into[k]):
            into[k] = e[k]
    for k in ("last_seen", "last_change"):
        if e.get(k) and (not into.get(k) or e[k] > into[k]):
            into[k] = e[k]


def build(paths: list[Path], out: Path, sources: dict | None = None) -> dict:
    """Build ``out`` (SQLite) from FtM line files. Returns counts."""
    t0 = time.time()
    assets: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    for e in iter_ftm(paths):
        schema = e.get("schema")
        if schema in ASSET_SCHEMAS:
            _merge(assets.setdefault(e["id"], {"id": e["id"], "schema": schema}), e)
        elif schema in EDGE_SCHEMAS:
            a_prop, b_prop = EDGE_SCHEMAS[schema]
            p = e.get("properties") or {}
            if p.get(a_prop) and p.get(b_prop):
                edges.setdefault(e["id"], e)
    # parties reachable from assets: direct links, then the controlling side upwards
    by_to: dict[str, list[dict]] = defaultdict(list)
    by_from: dict[str, list[dict]] = defaultdict(list)
    for e in edges.values():
        a_prop, b_prop = EDGE_SCHEMAS[e["schema"]]
        for a in e["properties"][a_prop]:
            by_from[a].append(e)
        for b in e["properties"][b_prop]:
            by_to[b].append(e)
    keep_edges: dict[str, dict] = {}
    parties: set[str] = set()
    for aid, a in assets.items():  # Vessel.owner / .operator properties name parties too
        for k in ("owner", "operator"):
            parties.update(a.get("properties", {}).get(k) or [])
        for e in by_to.get(aid, []) + by_from.get(aid, []):
            keep_edges[e["id"]] = e
            a_prop, b_prop = EDGE_SCHEMAS[e["schema"]]
            for side in (a_prop, b_prop):
                parties.update(x for x in e["properties"][side] if x != aid)
    frontier = set(parties)
    for _ in range(MAX_HOPS - 1):
        nxt: set[str] = set()
        for pid in frontier:
            for e in by_to.get(pid, []):  # Ownership(asset=pid) -> owner
                if e["schema"] == "Ownership":
                    keep_edges[e["id"]] = e
                    nxt.update(e["properties"]["owner"])
            for e in by_from.get(pid, []):  # UnknownLink(subject=pid, "owned or controlled by")
                role = " ".join(e["properties"].get("role") or [])
                if e["schema"] == "UnknownLink" and _PARENT_ROLE.search(role):
                    keep_edges[e["id"]] = e
                    nxt.update(e["properties"]["object"])
        nxt -= parties
        nxt -= set(assets)
        parties |= nxt
        frontier = nxt
        if not frontier:
            break
    wanted = parties | set(assets)
    party_rows: dict[str, dict] = {}
    sanctions: dict[str, dict] = {}
    for e in iter_ftm(paths):
        schema = e.get("schema")
        if schema in PARTY_SCHEMAS and e["id"] in parties:
            _merge(party_rows.setdefault(e["id"], {"id": e["id"], "schema": schema}), e)
        elif schema == "Sanction":
            ents = [x for x in (e.get("properties") or {}).get("entity") or [] if x in wanted]
            if ents:
                sanctions.setdefault(e["id"], e)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entity (id TEXT PRIMARY KEY, schema TEXT NOT NULL, caption TEXT,
                                 data TEXT NOT NULL);
            CREATE TABLE ident (kind TEXT NOT NULL, value TEXT NOT NULL, id TEXT NOT NULL);
            CREATE TABLE edge (id TEXT PRIMARY KEY, schema TEXT NOT NULL, src TEXT NOT NULL,
                               dst TEXT NOT NULL, role TEXT, data TEXT NOT NULL);
            CREATE TABLE sanction (id TEXT NOT NULL, entity_id TEXT NOT NULL, data TEXT NOT NULL);
        """)
        ent_rows, ident_rows = [], []
        for a in assets.values():
            p = a.get("properties") or {}
            data = {"props": _compact(p, ASSET_PROPS), **_meta(a)}
            caption = (p.get("name") or [a["id"]])[0]
            ent_rows.append((a["id"], a["schema"], caption, json.dumps(data, ensure_ascii=False)))
            for v in p.get("imoNumber") or []:
                if (imo := norm_imo(v)):
                    ident_rows.append(("imo", imo, a["id"]))
            for v in p.get("mmsi") or []:
                if re.fullmatch(r"\d{9}", v.strip()):
                    ident_rows.append(("mmsi", v.strip(), a["id"]))
            if a["schema"] == "Airplane":
                regs = {norm_reg(v) for v in (p.get("registrationNumber") or [])
                        + (p.get("name") or []) if re.search(r"[A-Z].*\d|\d.*[A-Z]", v.upper())}
                ident_rows += [("reg", r, a["id"]) for r in regs if 3 <= len(r) <= 10]
            for r in a.get("referents") or []:
                ident_rows.append(("ref", r, a["id"]))
        for pr in party_rows.values():
            p = pr.get("properties") or {}
            data = {"props": _compact(p, PARTY_PROPS), **_meta(pr)}
            caption = (p.get("name") or [pr["id"]])[0]
            ent_rows.append((pr["id"], pr["schema"], caption, json.dumps(data, ensure_ascii=False)))
            for r in pr.get("referents") or []:
                ident_rows.append(("ref", r, pr["id"]))
        edge_rows = []
        for e in keep_edges.values():
            a_prop, b_prop = EDGE_SCHEMAS[e["schema"]]
            p = e["properties"]
            data = {"props": _compact(p, ("startDate", "endDate", "percentage", "summary",
                                          "sourceUrl", "role")), **_meta(e)}
            role = "; ".join(p.get("role") or []) or None
            for src in p[a_prop]:
                for dst in p[b_prop]:
                    edge_rows.append((f"{e['id']}:{src}:{dst}", e["schema"], src, dst, role,
                                      json.dumps(data, ensure_ascii=False)))
        sanc_rows = []
        for s in sanctions.values():
            p = s.get("properties") or {}
            data = {"props": _compact(p, SANCTION_PROPS), **_meta(s), "caption": s.get("caption")}
            for ent in p.get("entity") or []:
                if ent in wanted:
                    sanc_rows.append((s["id"], ent, json.dumps(data, ensure_ascii=False)))
        db.executemany("INSERT OR REPLACE INTO entity VALUES (?,?,?,?)", ent_rows)
        db.executemany("INSERT INTO ident VALUES (?,?,?)", ident_rows)
        db.executemany("INSERT OR REPLACE INTO edge VALUES (?,?,?,?,?,?)", edge_rows)
        db.executemany("INSERT INTO sanction VALUES (?,?,?)", sanc_rows)
        db.executescript("""
            CREATE INDEX ident_kv ON ident(kind, value);
            CREATE INDEX edge_src ON edge(src);
            CREATE INDEX edge_dst ON edge(dst);
            CREATE INDEX sanction_ent ON sanction(entity_id);
        """)
        counts = {"assets": len(assets), "parties": len(party_rows), "edges": len(edge_rows),
                  "sanctions": len(sanc_rows)}
        meta = {"built_at": time.time(), "counts": counts, "sources": sources or {}}
        db.execute("INSERT INTO meta VALUES ('meta', ?)", (json.dumps(meta),))
        db.commit()
    finally:
        db.close()
    os.replace(tmp, out)
    log.info("OpenSanctions asset graph: %s in %.0f s", counts, time.time() - t0)
    return counts


# ------------------------------------------------------------------ download (datasets.py)


def fetch(f) -> dict:
    """``Dataset.fetch`` for ``os_assets``: stream both FtM exports to temp files, build the
    SQLite graph, delete the downloads. ``f`` is an ``osint.infra.datasets.Fetcher``."""
    paths, sources = [], {}
    try:
        for name, url in FTM_URLS.items():
            dest = f.dir / f"os_{name}.ftm.json.part"
            paths.append(dest)
            with f.http.stream("GET", url) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"{url}: HTTP {r.status_code}")
                with dest.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
            try:
                idx = f.http.get(INDEX_URL.format(name=name)).json()
                sources[name] = {"version": idx.get("version"), "updated_at": idx.get("updated_at"),
                                 "url": url}
            except Exception:  # noqa: BLE001 — version info is optional
                sources[name] = {"url": url}
        counts = build(paths, f.dir / FILE, sources)
    finally:
        for p in paths:
            p.unlink(missing_ok=True)
    if not counts["assets"]:
        raise RuntimeError("OpenSanctions FtM: no Vessel / Airplane entities")
    return {"count": counts["assets"], **counts}


# ------------------------------------------------------------------ reader


def _load(row) -> dict:
    d = json.loads(row["data"])
    d["id"], d["schema"], d["caption"] = row["id"], row["schema"], row["caption"]
    d["url"] = ENTITY_URL.format(id=row["id"])
    return d


class Graph:
    """Read-only view of ``os_assets.sqlite``. Cheap to construct; one connection per call
    so it is safe from any thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True,
                             check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def meta(self) -> dict:
        if not self.available:
            return {}
        db = self._db()
        try:
            row = db.execute("SELECT value FROM meta WHERE key='meta'").fetchone()
            return json.loads(row["value"]) if row else {}
        finally:
            db.close()

    def find(self, kind: str, value: str | None) -> list[str]:
        """Entity ids by identifier: kind = imo | mmsi | reg | ref."""
        if not value or not self.available:
            return []
        value = (norm_imo(value) if kind == "imo" else norm_reg(value) if kind == "reg"
                 else value.strip())
        if not value:
            return []
        db = self._db()
        try:
            rows = db.execute("SELECT DISTINCT id FROM ident WHERE kind=? AND value=?",
                              (kind, value)).fetchall()
            return [r["id"] for r in rows]
        finally:
            db.close()

    def entities(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        if not ids or not self.available:
            return {}
        db = self._db()
        try:
            out = {}
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = f"SELECT * FROM entity WHERE id IN ({','.join('?' * len(chunk))})"
                for r in db.execute(q, chunk):
                    out[r["id"]] = _load(r)
            return out
        finally:
            db.close()

    def sanctions(self, ids: Iterable[str]) -> dict[str, list[dict]]:
        ids = list(dict.fromkeys(ids))
        out: dict[str, list[dict]] = defaultdict(list)
        if not ids or not self.available:
            return out
        db = self._db()
        try:
            q = f"SELECT * FROM sanction WHERE entity_id IN ({','.join('?' * len(ids))})"
            for r in db.execute(q, ids):
                d = json.loads(r["data"])
                d["id"] = r["id"]
                out[r["entity_id"]].append(d)
            return out
        finally:
            db.close()

    def edges(self, ids: Iterable[str]) -> list[dict]:
        """Every edge touching any of ``ids`` (either side)."""
        ids = list(dict.fromkeys(ids))
        if not ids or not self.available:
            return []
        db = self._db()
        try:
            marks = ",".join("?" * len(ids))
            rows = db.execute(f"SELECT * FROM edge WHERE src IN ({marks}) OR dst IN ({marks})",
                              ids + ids).fetchall()
            out = []
            for r in rows:
                d = json.loads(r["data"])
                out.append({"id": r["id"], "schema": r["schema"], "src": r["src"],
                            "dst": r["dst"], "role": r["role"], **d})
            return out
        finally:
            db.close()

    def chain(self, asset_id: str, max_depth: int = MAX_HOPS) -> dict:
        """Links of an asset and of its controlling parties, up to ``max_depth`` levels:
        {"links": [{edge, party_id, depth, parent_of}], "entities": {...}, "sanctions": {...}}."""
        links: list[dict] = []
        seen = {asset_id}
        frontier = [asset_id]
        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            nxt = []
            for e in self.edges(frontier):
                if depth == 1:
                    other = e["src"] if e["dst"] == asset_id else e["dst"]
                    of = asset_id
                elif e["schema"] == "Ownership" and e["dst"] in frontier:
                    other, of = e["src"], e["dst"]
                elif (e["schema"] == "UnknownLink" and e["src"] in frontier
                      and _PARENT_ROLE.search(e.get("role") or "")):
                    other, of = e["dst"], e["src"]
                else:
                    continue
                links.append({**e, "party_id": other, "depth": depth, "of": of})
                if other not in seen:
                    seen.add(other)
                    nxt.append(other)
            frontier = nxt
        ents = self.entities(seen)
        return {"links": links, "entities": ents, "sanctions": self.sanctions(seen)}
