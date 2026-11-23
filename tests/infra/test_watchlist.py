import pytest

from osint.infra.watchlist import Matcher, SanctionsIndex, UserWatchlist, norm_imo, norm_reg

MARITIME = ('"type","caption","imo","risk","countries","flag","mmsi","id","url","datasets","aliases"\n'
            '"VESSEL","GREAT JACOMBO","IMO9319703","mare.shadow;sanction","","km","","v-shadow",'
            '"https://www.opensanctions.org/entities/v-shadow/","eu","FIANIT OLD"\n'
            '"VESSEL","PORT STATE GUY","IMO9000001","mare.detained","","pa","","v-det","","x",""\n'
            '"VESSEL","STRONG WIND 7","IMO9000002","poi","","pa","538009985","v-poi","","x",""\n'
            '"ORGANIZATION","Some Co","","sanction","","","","o1","","x",""\n')
TARGETS = ('"id","schema","name","aliases","countries","identifiers","sanctions","program_ids","dataset",'
           '"first_seen","last_seen","last_change"\n'
           '"v-shadow","Vessel","Great Jacombo","","","IMO9319703","EU Annex XLII","EU-RUS;GB-RUS","EU","","",""\n'
           '"v-sdn","Vessel","CONRAD","","","518998562;E5U4542;IMO9546722","OFAC","US-TERR","OFAC","","",""\n'
           '"a-ra","Airplane","RA-85041","","","85041","OFAC","US-RUSHAR","OFAC","","",""\n')


@pytest.fixture
def index(tmp_path):
    (tmp_path / "m.csv").write_text(MARITIME, encoding="utf-8")
    (tmp_path / "t.csv").write_text(TARGETS, encoding="utf-8")
    return SanctionsIndex(tmp_path / "m.csv", tmp_path / "t.csv")


def test_normalisers():
    assert norm_imo("IMO9319703") == "9319703" and norm_imo("9319703") == "9319703"
    assert norm_imo("123") is None and norm_imo(None) is None
    assert norm_reg("RA-85041") == "RA85041" and norm_reg("ep-mof") == "EPMOF"
    assert norm_reg("149") is None  # MSN, not a registration


def test_index_merges_and_filters_topics(index):
    s = index.summary()
    assert s["vessels"] == 3 and s["aircraft"] == 1  # detained-only vessel and the company dropped
    e = index.by_imo["9319703"]
    assert e.severity == "high" and "Shadow fleet" in e.risk_labels and e.programs == ["EU-RUS", "GB-RUS"]
    assert e.flag == "km"
    e.topics = ["mare.detained", "poi", "sanction"]  # most severe label first (globe ring tag)
    assert e.risk_labels == ["Sanctioned", "Person/vessel of interest", "Port-state detention"]
    assert index.by_mmsi["518998562"].name == "CONRAD"
    assert index.by_reg["RA85041"].programs == ["US-RUSHAR"]


def test_matcher(index):
    m = Matcher(index, None)
    # renamed vessel: IMO wins over the AIS name
    h = m.match("sea:273125140", {"name": "FIANIT", "extra": {"imo": "9319703"}})
    assert h[0]["matched_on"] == "imo" and h[0]["strength"] == "strong"
    assert h[0]["entry"]["commercial_ok"] is False and "OpenSanctions" in h[0]["entry"]["licence"]
    assert m.match("sea:518998562", {})[0]["matched_on"] == "mmsi"
    # MMSI re-used by a hull with another IMO -> weak
    assert m.match("sea:518998562", {"extra": {"imo": "9111111"}})[0]["strength"] == "weak"
    # distinctive name only -> weak; a different IMO -> no match at all
    assert m.match("sea:1", {"name": "STRONG WIND 7"})[0]["strength"] == "weak"
    assert m.match("sea:1", {"name": "STRONG WIND 7", "extra": {"imo": "9555555"}}) == []
    assert m.match("air:154c31", {"reg": "RA-85041"})[0]["entry"]["name"] == "RA-85041"
    assert m.match("air:abc", {"reg": "N123AB"}) == [] and m.match("sea:2", None) == []


def test_user_watchlist(tmp_path):
    p = tmp_path / "watchlist.yaml"
    p.write_text("entries:\n  - icao: AE1234\n    label: Tanker\n    severity: high\n"
                 "  - callsign: 'RCH*'\n  - mmsi: '211000000'\n    label: Ferry\n  - label: nothing\n",
                 encoding="utf-8")
    u = UserWatchlist(p)
    assert len(u.entries) == 3 and u.entries[1]["severity"] == "warn"
    m = Matcher(None, u)
    assert m.match("air:ae1234", {})[0]["entry"]["label"] == "Tanker"
    assert m.match("air:000001", {"name": "RCH871"})[0]["matched_on"] == "callsign"
    assert m.match("sea:211000000", {})[0]["entry"]["severity"] == "warn"
    assert m.match("air:000002", {"name": "DLH1"}) == []
    p.write_text("entries: [", encoding="utf-8")  # broken edit keeps the last good list
    u.reload()
    assert u.error and len(u.entries) == 3
    assert UserWatchlist(tmp_path / "missing.yaml").entries == []
