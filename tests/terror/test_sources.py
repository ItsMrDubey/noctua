"""Parsers against payloads recorded from the live sources on 2026-11-23 (trimmed)."""

from datetime import UTC, datetime

import pytest

from osint.terror import baseline, countries
from osint.terror.sources import SOURCES, feeds, licence_entries, official
from osint.terror.sources import designations as des
from tests.terror.conftest import FIX


def _b(name: str) -> bytes:
    return (FIX / name).read_bytes()


# ---------------------------------------------------------------- designations

def test_fto_listed_and_delisted_with_official_dates():
    rows = des.parse_fto(_b("fto.html"))
    by = {r["name"]: r for r in rows}
    k = by["Islamic State’s Khorasan Province (ISIS-K)"]
    assert k["status"] == "listed" and k["listed_on"] == "2016-01-14"
    hts = by["al-Nusrah Front, aka Hay’at Tahrir al-Sham (ANF/HTS)"]
    assert hts["status"] == "delisted" and hts["delisted_on"] == "2025-07-08"
    assert hts["listed_on"] == "2014-05-15"


def test_sdn_keeps_only_sdgt_and_reads_linked_to():
    rows = des.parse_sdn(_b("sdn.csv"))
    assert all("SDGT" in r["programmes"] for r in rows) and len(rows) == 3
    nasrallah = next(r for r in rows if r["name"].startswith("NASRALLAH"))
    assert nasrallah["kind"] == "person" and nasrallah["linked_to"] == ["HIZBALLAH"]
    assert all(r["listed_on"] is None for r in rows)  # the CSV states no dates


def test_un_list_types_and_dates():
    rows = des.parse_un(_b("un.xml"))
    assert {r["list_id"] for r in rows} == {"un_1267", "un_1988"}
    asg = next(r for r in rows if r["name"] == "ABU SAYYAF GROUP")
    assert asg["listed_on"] == "2001-10-06" and asg["kind"] == "organisation"
    assert not any("DPRK" in r["programmes"] for r in rows)


def test_uk_proscription_month_precision():
    rows = des.parse_uk(_b("uk_proscribed.json"))
    by = {r["name"]: r for r in rows}
    assert by["Palestine Action"]["listed_on"] == "2025-07"  # month only, as published
    assert by["Abu Sayyaf Group (ASG)"]["listed_on"] == "2001-03"


def test_eu_entries_english_names_and_no_invented_listing_date():
    rows = des.parse_eu(_b("eu.xml"))
    assert {r["list_id"] for r in rows} == {"eu_terr", "eu_isil_aq"}
    assert "Al-Qaeda in the Indian Subcontinent (AQIS)" in [r["name"] for r in rows]
    assert all(r["listed_on"] is None for r in rows)
    assert all("legal acts in the export" in (r["remarks"] or "") for r in rows)


def test_name_variants_split_official_names():
    from osint.terror.groups import name_variants

    v = name_variants("al-Nusrah Front, aka Hay’at Tahrir al-Sham (ANF/HTS)")
    assert "Hay’at Tahrir al-Sham" in v and "HTS" in v and "ANF" in v


# ---------------------------------------------------------------- threat levels

def test_mi5_national_and_northern_ireland_levels():
    lv = official.parse_mi5(_b("mi5.xml"))
    assert [(x["scope"], x["level"], x["rank"]) for x in lv] == [
        ("national", "SEVERE", 4), ("Northern Ireland-related terrorism in Northern Ireland",
                                    "SUBSTANTIAL", 3)]
    assert lv[0]["as_of"] == "2026-04-30" and "SEVERE" in lv[0]["quote"]


def test_vigipirate_new_2026_scale():
    lv = official.parse_vigipirate(_b("vigipirate.html"))[0]
    assert lv["level"] == "vigilance renforcée" and lv["rank"] == 2 and lv["scale_max"] == 3
    assert lv["as_of"] == "2026-06-22"


def test_nctv_level_and_assessment_month():
    lv = official.parse_nctv(_b("nctv.html"))[0]
    assert lv["rank"] == 4 and lv["scale_max"] == 5 and lv["as_of"] == "2026-06"


def test_asio_level_without_invented_date():
    lv = official.parse_asio(_b("asio.html"))[0]
    assert lv["level"] == "PROBABLE" and lv["as_of"] is None
    assert lv["quote"].startswith("Australia")


def test_ntas_no_current_advisory_from_feed_and_page_text():
    lv = official.parse_ntas(_b("ntas_feed.xml"), _b("ntas.html"))[0]
    assert lv["level"] == "No current advisory" and lv["advisories"] == []
    lv2 = official.parse_ntas_text(official.page_text(_b("ntas.html")))[0]
    assert lv2["level"] == "No current advisory"


def test_changed_wording_raises_instead_of_guessing():
    with pytest.raises(official.ParseError):
        official.parse_asio(b"<html>The threat level page moved</html>")
    with pytest.raises(official.ParseError):
        official.parse_vigipirate(b"<html>maintenance</html>")


def test_state_advisories_fips_mapping_and_terrorism_flag():
    rows = official.parse_state_advisories(_b("state_ta.json"))
    by = {r["country"]: r for r in rows}
    assert set(by) == {"IR", "YE", "FR", "GB"}  # YM -> YE, UK -> GB (FIPS 10-4)
    assert by["YE"]["level"] == 4 and by["YE"]["terrorism"] is True
    assert "terrorism" in by["FR"]["quote"]


def test_country_helpers():
    assert countries.from_fips("YM") == "YE" and countries.from_fips("UK") == "GB"
    assert countries.iso2("DR Congo (Zaire)") == "CD" and countries.iso2("Côte d'Ivoire") == "CI"
    assert countries.iso2("Russia (Soviet Union)") == "RU" and countries.iso2("XX") is None


# ---------------------------------------------------------------- justice / research feeds

def test_doj_keeps_terror_tagged_and_nsd_releases_only():
    rows = feeds.parse_doj(_b("doj.json"))
    assert len(rows) == 3
    assert {r["basis"] for r in rows} >= {"DOJ topic tag: Domestic Terrorism",
                                          "NSD release naming terrorism"}
    assert all(r["published_at"] for r in rows)


def test_europol_items_without_dates_are_dated_by_first_sight():
    now = datetime(2026, 11, 23, tzinfo=UTC)
    rows = feeds.parse_europol(_b("europol.xml"), now)
    assert rows and all(r["published_at"] is None for r in rows)
    assert all(r["date_basis"].startswith("first seen") for r in rows)


def test_research_feed_is_context_only():
    rows = feeds.parse_research(_b("gnet.xml"), "gnet_research")
    assert len(rows) == 2 and all(r["kind"] == "analysis" for r in rows)


# ---------------------------------------------------------------- UCDP baseline

def test_ucdp_aggregate_drops_state_actors_and_maps_groups(kb):
    rows = baseline.aggregate([("GEDEvent_v26_0_10", baseline.rows_from_csv(
        _b("ucdp_candidate.csv")))], kb)
    actors = {r[2] for r in rows}
    assert "IS" in actors and not any(a.startswith("Government of") for a in actors)
    is_rows = [r for r in rows if r[2] == "IS"]
    assert all(r[3] == "isis" and r[4] == "jihadist" for r in is_rows)
    cartel = [r for r in rows if r[2] == "Sinaloa Cartel"]
    assert cartel and cartel[0][4] == "criminal"
    assert all(len(r[0]) == 7 and len(r[1]) == 2 for r in rows)  # month, ISO2


def test_ucdp_final_rows_win_over_candidates(kb):
    row = {"id": "1", "type_of_violence": "3", "side_a": "IS", "country": "Syria",
           "date_start": "2025-05-02", "best": "5", "low": "4", "high": "6"}
    newer = dict(row, best="9")
    out = baseline.aggregate([("ged261", [row]), ("GEDEvent_v26_0_10", [newer])], kb)
    assert out[0][7] == 5 and out[0][5] == "ged261"


# ---------------------------------------------------------------- register

def test_every_source_has_licence_and_commercial_flag():
    ids = [s.id for s in SOURCES]
    assert len(ids) == len(set(ids))
    for s in SOURCES:
        assert s.licence and s.commercial in ("ok", "review", "no")
        assert s.urls and all(u.startswith("https://") for u in s.urls)
    ent = licence_entries()
    assert all(e["id"].startswith("terror:") for e in ent)
    assert next(e for e in ent if e["id"] == "terror:gtd")["commercial"] == "no"


def test_advisories_one_row_per_country_highest_level():
    rows = official.merge_advisories([
        {"country": "PS", "place": "Gaza", "level": 4, "terrorism": True, "risks": ["terrorism"],
         "title": "Gaza - Level 4", "url": "u1"},
        {"country": "PS", "place": "The West Bank", "level": 3, "terrorism": True,
         "risks": ["terrorism"], "title": "The West Bank - Level 3", "url": "u2"}])
    assert len(rows) == 1 and rows[0]["level"] == 4 and len(rows[0]["parts"]) == 2
    assert countries.iso2("The West Bank") == "PS"
