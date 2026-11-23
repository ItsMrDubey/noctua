from osint.profiles.osgraph import Graph, build, norm_imo, norm_reg
from tests.profiles import ftm_fixture


def _graph(tmp_path):
    out = tmp_path / "os_assets.sqlite"
    counts = build(ftm_fixture.write(tmp_path), out, {"sanctions": {"version": "t"}})
    return Graph(out), counts


def test_build_keeps_assets_their_parties_and_sanctions_only(tmp_path):
    g, counts = _graph(tmp_path)
    assert counts["assets"] == 2  # tanker merged across both exports + the aircraft
    ents = g.entities(["NK-tanker", "NK-glory", "NK-parent", "NK-unrelated", "NK-idas"])
    assert set(ents) == {"NK-tanker", "NK-glory", "NK-parent", "NK-idas"}  # the person is unrelated
    props = ents["NK-tanker"]["props"]
    assert props["topics"] == ["sanction", "mare.shadow"]
    assert props["previousName"] == ["Venture", "Crudemed", "Velmar"]
    assert props["deadweightTonnage"] == ["115643"]
    assert set(ents["NK-tanker"]["datasets"]) == {"us_ofac_sdn", "ua_war_sanctions"}
    assert ents["NK-tanker"]["url"] == "https://www.opensanctions.org/entities/NK-tanker/"
    assert "NK-unrelated" not in g.sanctions(["NK-unrelated"])
    assert g.meta()["sources"] == {"sanctions": {"version": "t"}}


def test_find_by_identifiers(tmp_path):
    g, _ = _graph(tmp_path)
    assert g.find("imo", "IMO 9832547") == ["NK-tanker"]
    assert g.find("imo", "9832547") == ["NK-tanker"]
    assert g.find("mmsi", "273278060") == ["NK-tanker"]
    assert g.find("ref", "imo-vsl-9832547") == ["NK-tanker"]
    assert g.find("reg", "ra-85041") == ["ofac-43200"]
    assert g.find("reg", "RA85041") == ["ofac-43200"]
    assert g.find("imo", "1234567") == [] and g.find("reg", "") == []


def test_chain_walks_owners_and_parents(tmp_path):
    g, _ = _graph(tmp_path)
    ch = g.chain("NK-tanker")
    by = {(lk["party_id"], lk["depth"]) for lk in ch["links"]}
    assert ("NK-glory", 1) in by and ("NK-prominent", 1) in by and ("NK-idas", 1) in by
    assert ("NK-parent", 2) in by  # "Owned or Controlled By" from the owner upwards
    roles = {lk["party_id"]: lk["role"] for lk in ch["links"] if lk["depth"] == 1 and lk["schema"] == "UnknownLink"}
    assert roles == {"NK-idas": "commerce manager"}
    assert [s["props"]["programId"] for s in ch["sanctions"]["NK-glory"]] == [["GB-RUS"]]
    assert len(ch["sanctions"]["NK-tanker"]) == 2


def test_missing_graph_is_empty(tmp_path):
    g = Graph(tmp_path / "nope.sqlite")
    assert not g.available and g.find("imo", "9832547") == [] and g.meta() == {}
    assert g.chain("x")["links"] == []


def test_normalisers():
    assert norm_imo("IMO9832547") == "9832547" and norm_imo("n/a") is None
    assert norm_reg("ra-85041") == "RA85041" and norm_reg("E.27-27") == "E2727"
