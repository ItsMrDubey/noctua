"""/api/profile/*: entity resolution (IMO / MMSI / registration), contextual ordering,
highlights, weak name matches with doubts, sources and the pending flow."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.profiles import mount_profiles
from osint.core.models import Position
from osint.infra.job import WatchStore
from osint.profiles import lookups as L
from osint.profiles.osgraph import build
from tests.profiles import ftm_fixture

NOW = datetime.now(UTC).replace(microsecond=0)
MARITIME = ('"type","caption","imo","risk","countries","flag","mmsi","id","url","datasets","aliases"\n'
            '"VESSEL","VELMAR","IMO9832547","mare.shadow;poi","bb","bb","273278060","imo-vsl-9832547",'
            '"https://www.opensanctions.org/entities/imo-vsl-9832547","ua_war_sanctions","Venture"\n'
            '"VESSEL","OTHER","IMO1111111","sanction","","","273278060","x-other","","us_ofac_sdn",""\n')
PLANE_ALERT = ("$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\n"
               "43C38C,ZZ664,Royal Air Force,Boeing RC-135W Airseeker,R135,Mil,Eye In The Sky,SIGINT,"
               "Reconnaissance,Oxcart,https://www.raf.mod.uk\n")


class Store:
    def __init__(self, tracks):
        self.tracks = tracks

    def get_tracks(self, ids):
        return {i: self.tracks[i] for i in ids if i in self.tracks}

    def list_events(self, **kw):
        return []


class Lake:
    root = None

    def query(self, *a, **kw):
        return []


def track(tid, **kw):
    kind, _, ident = tid.partition(":")
    return {"track_id": tid, "kind": kind, "ident": ident, "name": None, "reg": None, "type_code": None,
            "operator": None, "category": None, "is_mil": 0, "high_priority": 0, "extra": {},
            "first_seen": NOW.isoformat(), "last_seen": NOW.isoformat(), **kw}


def pos(tid, lat, lon, **kw):
    return Position(track_id=tid, kind=tid.split(":")[0], ts=NOW, lat=lat, lon=lon, source="t", **kw)


@pytest.fixture
def setup(tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    build(ftm_fixture.write(tmp_path), ref / "os_assets.sqlite")
    (ref / "os_maritime.csv").write_text(MARITIME, encoding="utf-8")
    (ref / "aircraft_types.json").write_text(json.dumps({"R135": ["BOEING RC-135", "L4J", "H"]}))
    pa = tmp_path / "pa.csv"
    pa.write_text(PLANE_ALERT, encoding="utf-8")
    data = tmp_path / "data"
    (data / "signals").mkdir(parents=True)
    (data / "signals" / "squawks.json").write_text(json.dumps({"items": [
        {"hex": "3c55c7", "code": "7700", "label": "General emergency", "flight": "EWG4HV", "active": True,
         "first_seen": NOW.timestamp(), "last_seen": NOW.timestamp()}]}))
    ws = WatchStore(data / "infra_watch.sqlite")
    ws.set_active([{"track_id": "sea:538000001", "severity": "info", "name": "VELMAR", "lat": 1, "lon": 2,
                    "ts": NOW.isoformat(), "hits": [{"entry": {"id": "NK-tanker", "name": "VELMAR", "source": "opensanctions",
                                                               "risk": ["Sanctioned"], "url": "https://x"},
                                                     "matched_on": "name", "strength": "weak",
                                                     "doubts": ["no IMO broadcast; listed IMO 9832547"]}]}])
    ws.add_alert({"ts": NOW.isoformat(), "kind": "proximity", "track_id": "sea:273278060", "severity": "high",
                  "title": "Loitering near cable", "sub": "1.5 kt within 0.8 km of Estlink 2", "lat": 59.9, "lon": 25.5})
    ws.close()
    tracks = {
        "sea:273278060": track("sea:273278060", name="STRIZH", category="Tanker", type_code="80",
                               extra={"imo": "9832547", "callsign": "UAOE2", "destination": "EGPSD"}),
        "sea:538000001": track("sea:538000001", name="VELMAR", category="Tanker"),
        "air:43c38c": track("air:43c38c", name="RRR7223", reg="ZZ664", type_code="R135", operator="Royal Air Force",
                            category="Oxcart", is_mil=1, extra={"squawk": "1234"}),
        "air:3c55c7": track("air:3c55c7", name="EWG4HV", reg="D-AENG", type_code="A20N", extra={"squawk": "7700"}),
    }
    live = {"sea:273278060": pos("sea:273278060", 57.7, 10.8, speed_kts=8.1, heading_deg=354),
            "air:43c38c": pos("air:43c38c", 52.0, 4.0, alt_ft=33025, speed_kts=459),
            "air:3c55c7": pos("air:3c55c7", 52.3, 9.6, alt_ft=34975, speed_kts=450)}
    rt = SimpleNamespace(store=Store(tracks), ingestor=SimpleNamespace(live=live), lake=Lake(),
                         settings=SimpleNamespace(data_dir=data, reference_dir=tmp_path))
    calls = []

    async def never(http, arg):  # the background fetch: records the call, finds nothing
        calls.append(arg)

    lk = L.Lookups(data / "c.sqlite", "t@example.org", client=httpx.AsyncClient(),
                   fetchers={k: never for k in L.FETCHERS})
    lk.cache.put("hexdb:43c38c", "ok", L.parse_hexdb({"ModeS": "43C38C", "Registration": "ZZ664", "Manufacturer": "Boeing",
                                                       "ICAOTypeCode": "R135", "Type": "RC-135W Rivet Joint",
                                                       "RegisteredOwners": "Royal Air Force"}), None, 3600)
    lk.cache.put("adsbdb:43c38c", "not_found", None, None, 3600)
    lk.cache.put("adsbdb_route:RRR7223", "not_found", None, None, 3600)
    lk.cache.put("planespotters:43c38c|ZZ664", "ok", {"image": "https://t.plnspttrs.net/1_280.jpg", "thumb": "https://t.plnspttrs.net/1_t.jpg",
                                                       "link": "https://www.planespotters.net/photo/1923370/zz664", "credit": "Alon Varshaver"}, None, 3600)
    lk.cache.put("adsbdb:RA-85041", "ok", {"hex": "154C31", "reg": "RA-85041"}, None, 3600)
    app = FastAPI()
    mount_profiles(app, rt, ref_dir=ref, data_dir=data, lookups=lk, plane_alert_csv=pa)
    with TestClient(app) as c:
        yield c, calls
    lk.cache.close()


def test_sanctioned_vessel_leads_with_sanctions_and_ownership(setup):
    c, _ = setup
    d = c.get("/api/profile/vessel/273278060").json()
    assert d["context"] == "sanctioned" and d["order"][:2] == ["sanctions", "ownership"]
    assert d["key"] == {"mmsi": "273278060", "imo": "9832547", "name": "STRIZH"}
    hl = [h["text"] for h in d["highlights"]]
    assert hl[0].startswith("Sanctioned by 2 authorities (EU-MARE, US-RUSHAR)")
    assert "irregular and high-risk shipping" in hl[0]  # the descriptive reason, not the bare EO number
    assert any("Glory Shipping HK Limited is sanctioned" in t for t in hl)
    assert any("Parent Holding Ltd is sanctioned" in t for t in hl)  # parent of an owner
    assert any("former name" in t for t in hl) and any("differs from the listed name" in t for t in hl)
    assert any("Loitering near cable" in t for t in hl)
    s = d["sections"]["sanctions"]
    assert s["sanctioned"] and s["entries"][0]["matched_on"] == "imo"
    assert {r["authority"] for r in s["entries"][0]["records"]} == {"Office of Foreign Assets Control", "Council of the European Union"}
    assert [x["topic"] for x in s["lists"]] == ["mare.shadow", "poi"]  # the other-IMO MMSI row is dropped
    groups = d["sections"]["ownership"]["groups"]
    idas = next(g for g in groups if g["party"]["name"] == "Idas LLC")
    assert {r["role"] for r in idas["roles"]} == {"Owner", "Commerce manager"} and idas["roles"][0]["start"] == "2025-12-17"
    parent = next(g for g in groups if g["party"]["name"] == "Parent Holding Ltd")
    assert parent["depth"] == 2 and parent["party"]["sanctioned"]
    assert d["sources"]["opensanctions"]["status"] == "ok" and d["sources"]["ais"]["licence"]
    ident = {f["label"]: f for f in d["sections"]["identity"]["fields"]}
    assert ident["IMO"]["source"] == "ais" and ident["Deadweight"]["value"] == "115643 t"
    assert ident["Dimensions"]["value"] == "unknown" and "note" in ident["Dimensions"]


def test_name_only_match_is_an_unverified_lead(setup):
    c, _ = setup
    d = c.get("/api/profile/vessel/sea:538000001").json()
    s = d["sections"]["sanctions"]
    assert not s["listed"] and not s["sanctioned"] and s["entries"] == []
    assert s["weak"][0]["doubts"] == ["no IMO broadcast; listed IMO 9832547"]
    assert "possible name match" in s["summary"]
    assert not any(h["text"].startswith("Sanctioned") for h in d["highlights"])
    assert d["context"] == "plain"


def test_military_aircraft_profile(setup):
    c, calls = setup
    d = c.get("/api/profile/aircraft/43c38c").json()
    assert d["context"] == "military" and d["order"][0] == "identity"
    ident = {f["label"]: f for f in d["sections"]["identity"]["fields"]}
    assert ident["Model"]["value"] == "RC-135W Rivet Joint" and ident["Model"]["source"] == "hexdb"
    assert ident["Class / engines"]["value"] == "Landplane · 4 × jet" and ident["Wake category"]["value"] == "Heavy"
    assert ident["Role (plane-alert tags)"]["value"] == "Eye In The Sky · SIGINT · Reconnaissance"
    assert d["sections"]["photo"]["photo"]["credit"] == "Alon Varshaver"
    assert d["sections"]["sanctions"]["summary"].startswith("Not listed. Checked: OpenSanctions graph by registration ZZ664")
    assert not d["sections"]["sanctions"]["notable"]
    assert d["sources"]["hexdb"]["status"] == "ok" and d["sources"]["adsbdb"]["status"] == "not_found"
    assert d["pending"] is False and calls == []


def test_emergency_squawk_comes_first_and_lookups_go_pending(setup):
    c, _ = setup
    d = c.get("/api/profile/aircraft/air:3c55c7").json()
    assert d["context"] == "emergency" and d["order"][0] in ("activity", "live")
    assert d["highlights"][0]["severity"] == "high" and "7700" in d["highlights"][0]["text"]
    assert d["pending"] is True and d["sources"]["hexdb"]["status"] == "pending"


def test_sanctioned_aircraft_by_registration(setup):
    c, _ = setup
    d = c.get("/api/profile/aircraft/reg:RA-85041").json()
    assert d["context"] == "sanctioned" and d["key"]["hex"] == "154c31"
    assert d["highlights"][0]["text"].startswith("Sanctioned by 1 authority (US-RUSHAR)")
    g = d["sections"]["ownership"]["groups"][0]
    assert g["party"]["name"] == "223rd Flight Unit State Airlines" and g["roles"][0]["role"] == "Property in the interest of"
    ident = {f["label"]: f for f in d["sections"]["identity"]["fields"]}
    assert ident["Serial (msn)"]["value"] == "12A997"


def test_unknown_registration_and_bad_keys(setup):
    c, _ = setup
    d = c.get("/api/profile/aircraft/reg:XX-NOPE").json()
    assert d["pending"] and d["sources"]["adsbdb"]["status"] == "pending"
    assert c.get("/api/profile/aircraft/zzz").status_code == 422
    assert c.get("/api/profile/vessel/12").status_code == 422
    assert c.get("/api/profile/vessel/imo:9832547").json()["sections"]["sanctions"]["sanctioned"]
    srcs = c.get("/api/profile/sources").json()
    assert {s["id"] for s in srcs["lookups"]} == set(L.SPECS)
