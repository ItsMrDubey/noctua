from datetime import UTC, datetime, timedelta

import pytest

from osint.pipeline.cluster import ClusterDoc, Clusterer, EventState, new_ulid, title_tokens
from osint.pipeline.events import (
    EventsConfig,
    confidence_components,
    event_confidence,
    event_status,
    make_scorer,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
KHARKIV = (49.9935, 36.2304)
CHUHUIV = (49.8353, 36.6880)
LIMA = (-12.0464, -77.0428)


def cfg(**kw) -> EventsConfig:
    c = EventsConfig(min_shared_tokens={"default": 2, "armed_conflict": 1},
                     time_window_h={"default": 24, "armed_conflict": 12, "political": 48})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def make(c: EventsConfig | None = None, **kw) -> Clusterer:
    c = c or cfg()
    return Clusterer(c, make_scorer(c), **kw)


def place(name, lat, lon, precision="city", country="UA", conf=0.9):
    return {"name": name, "lat": lat, "lon": lon, "precision": precision, "country": country,
            "confidence": conf, "matched": name}


def art(i, title, *, url=None, where=("Kharkiv", *KHARKIV), cat="armed_conflict",
        minutes=0, lang="en", rel=0.8, precision="city", snippet=""):
    name, lat, lon = where if where else (None, None, None)
    if name and name not in title and name not in snippet:
        snippet = f"{snippet} {name}".strip()  # the place must be named in the text
    return ClusterDoc(
        id=f"doc:{i}", source="rss:test", url=url or f"https://outlet{i}.example/a/{i}",
        title=title, snippet=snippet, language=lang,
        published_at=NOW - timedelta(hours=1) + timedelta(minutes=minutes),
        relevance=rel, categories=[cat] if cat else [],
        places=[place(name, lat, lon, precision)] if where else [],
        place_name=name, place_precision=precision if where else None,
        place_confidence=0.9 if where else None, lat=lat, lon=lon)


def gdelt(gid, *, geo_type=4, where=("Kharkiv, Kharkivs'ka Oblast', Ukraine", *KHARKIV),
          root="19", code="190", mentions=10, cat="armed_conflict", minutes=30,
          url=None, actor1="RUSSIA", actor2="UKRAINE", published=None, sqldate=None):
    name, lat, lon = where
    precision = {1: "country", 2: "admin1", 3: "city", 4: "city", 5: "admin1"}[geo_type]
    pub = published or NOW - timedelta(hours=1) + timedelta(minutes=minutes)
    return ClusterDoc(
        id=f"doc:g{gid}", source="gdelt", kind="gdelt_event", origin_id=str(gid),
        url=url or f"https://gdeltsrc{gid}.example/story", title=f"{actor1} — Fight — {actor2}",
        snippet=name, published_at=pub, relevance=0.6, categories=[cat] if cat else [],
        places=[place(name, lat, lon, precision, conf=0.7)], place_name=name,
        place_precision=precision, place_confidence=0.7, lat=lat, lon=lon,
        attrs={"event_code": code, "event_root_code": root, "num_mentions": mentions,
               "geo_type": geo_type, "geo_country": "UP", "actor1": actor1,
               "actor2": actor2, "actor1_code": "RUS", "actor2_code": "UKR",
               "sqldate": sqldate or pub.strftime("%Y%m%d")})


# ----------------------------------------------------------------------- basics


def test_ulid_sortable_and_unique():
    a = new_ulid(NOW)
    b = new_ulid(NOW + timedelta(milliseconds=5))
    assert len(a) == 26 and a < b and new_ulid(NOW) != a


def test_title_tokens_drop_stopwords_and_short_words():
    toks = title_tokens("The Russian strike on Kharkiv, officials said 2026")
    assert {"w:russian", "w:strike", "w:kharkiv", "w:officials"} <= toks
    assert "w:the" not in toks and "w:said" not in toks and "w:2026" not in toks


# ------------------------------------------------------------------- clustering


def test_multilingual_near_duplicates_join_on_place_category_time():
    c = make()
    docs = [
        art(1, "Russian drone strike hits Kharkiv apartment block"),
        art(2, "قصف بطائرة مسيرة روسية على خاركيف", lang="ar", minutes=20),
        art(3, "Удар беспилотника по Харькову", lang="ru", minutes=40),
    ]
    res = c.process(docs, NOW)
    assert len(res.created) == 1 and len(c.events) == 1
    ev = next(iter(c.events.values()))
    assert ev.mention_count == 3 and ev.version == 3 and ev.source_diversity == 3
    assert ev.status == "multi-source"
    assert ev.title == "Russian drone strike hits Kharkiv apartment block"  # en preferred
    roles = [d["role"] for d in res.documents]
    assert roles == ["primary", "mention", "mention"]
    assert res.clustered == 3


def test_different_nearby_places_stay_separate():
    c = make()
    res = c.process([
        art(1, "Russian drone strike hits Kharkiv"),
        art(2, "Russian drone strike hits Chuhuiv", where=("Chuhuiv", *CHUHUIV)),
        art(3, "Protest in Lima over pensions", where=("Lima", *LIMA), cat="civil_unrest"),
    ], NOW)
    assert len(res.created) == 3


def test_category_and_time_window_separate():
    c = make()
    c.process([art(1, "Explosion reported in Kharkiv")], NOW)
    res = c.process([
        art(2, "Kharkiv mayor meets delegation", cat="political"),  # other category
        art(3, "Explosion reported in Kharkiv again", minutes=60 * 14),  # > 12 h window
    ], NOW + timedelta(hours=14))
    assert len(res.created) == 2 and not res.updated


def test_text_path_joins_unlocated_copy_and_unlocated_new_is_skipped():
    c = make(cfg(require_location=True))
    title = "Huge blast at chemical plant in Kharkiv region injures dozens of workers"
    c.process([art(1, title, snippet="Emergency services said the fire was contained.")],
              NOW)
    res = c.process([
        art(2, title + "!", where=None, snippet="Emergency services said the fire was "
            "contained.", cat="disaster"),
        art(3, "Something unrelated happened somewhere", where=None),
    ], NOW)
    assert len(res.updated) == 1 and res.skipped == {"unlocated": 1}
    assert next(iter(c.events.values())).mention_count == 2


def test_unlocated_article_creates_unlocated_event_by_default():
    c = make()
    res = c.process([art(1, "Missile strike kills five", where=None)], NOW)
    [ev] = c.events.values()
    assert res.created and ev.precision == "unlocated" and ev.lat is None
    assert ev.to_row()["precision"] == "unlocated"


def test_irrelevant_documents_skipped():
    c = make()
    res = c.process([art(1, "Kharkiv strike", rel=0.1), art(2, "x", cat=None)], NOW)
    assert res.skipped == {"irrelevant": 2} and not c.events


def test_bookkeeping_same_domain_location_stays_with_primary():
    c = make()
    c.process([art(1, "Russian strike on Kharkiv region",
                   where=("Kharkiv Oblast", *KHARKIV), precision="admin1",
                   url="https://a.example/1")], NOW)
    ev = next(iter(c.events.values()))
    assert ev.precision == "admin1" and ev.status == "reported"
    res = c.process([art(2, "Russian strike on Kharkiv region", url="https://www.a.example/2",
                         minutes=30, where=("Kharkiv Oblast", *KHARKIV),
                         precision="city")], NOW + timedelta(minutes=5))
    assert res.updated == {ev.id} and not res.created
    assert ev.version == 2 and ev.mention_count == 2
    assert ev.source_diversity == 1 and ev.status == "reported"  # same outlet
    assert ev.precision == "admin1"  # location and title only from the primary article
    assert ev.t_end - ev.t_start == timedelta(minutes=30)
    assert not any(f["field"] == "location" for f in res.fields)


def test_warm_start_roundtrip_through_row():
    c = make()
    c.process([art(1, "Russian drone strike hits Kharkiv")], NOW)
    row = next(iter(c.events.values())).to_row()
    assert isinstance(row["h3_3"], str) and row["cluster_state"]["sigs"]
    c2 = make()
    assert c2.load([row]) == 1
    res = c2.process([art(2, "Russian drone strike hits Kharkiv", minutes=10,
                          lang="de")], NOW)
    assert res.updated == {row["id"]}
    assert EventState.from_row(row).sigs == c.events[row["id"]].sigs


def test_prune_drops_old_events_from_index():
    c = make()
    c.process([art(1, "Russian drone strike hits Kharkiv")], NOW)
    assert c.prune(NOW + timedelta(hours=80)) == 1
    assert not c.events and len(c.lsh) == 0 and not c._geo


def test_embedder_hook_joins_semantic_match():
    class Fixed:
        def embed(self, texts, keys=None):
            return [[1.0, 0.0] for _ in texts]

    c = make(cfg(min_shared_tokens={"default": 9}), embedder=Fixed())
    res = c.process([art(1, "Alpha bravo foxtrot Kharkiv", cat="political"),
                     art(2, "Charlie delta echo golf", cat="political", minutes=5)], NOW)
    assert len(res.created) == 1  # joined via cosine, not tokens


# ----------------------------------------------------------------------- GDELT


def _decision(res, gid):
    return next(d for d in res.decisions if d["gdelt_event_id"] == str(gid))


def test_gdelt_attaches_to_news_event():
    c = make()
    res = c.process([art(1, "Russian drone strike hits Kharkiv"), gdelt(100)], NOW)
    d = _decision(res, 100)
    assert (d["decision"], d["reason"]) == ("accepted", "duplicate_of_event")
    ev = c.events[d["event_id"]]
    assert ev.gdelt_ids == ["100"] and ev.cameo_root == "19" and ev.subcategory == "190"
    assert {a["name"] for a in ev.actors} == {"Russia", "Ukraine"}
    assert ev.title == "Russian drone strike hits Kharkiv"  # article title wins
    assert ev.status == "reported" and res.gdelt_accepted == 1  # GDELT is no outlet
    assert [x["role"] for x in res.documents if x["doc_id"] == "doc:g100"] == ["gdelt"]


def test_gdelt_never_creates_events():
    c = make()
    res = c.process([gdelt(1), gdelt(2, url="https://gdeltsrc1.example/story")], NOW)
    assert _decision(res, 1)["reason"] == "no_article_backing"
    assert _decision(res, 2)["reason"] == "no_article_backing"
    assert not c.events and not res.created and res.gdelt_accepted == 0


def test_gdelt_attach_keeps_article_title_and_location():
    c = make()
    c.process([art(1, "Russian drone strike hits Kharkiv")], NOW)
    ev = next(iter(c.events.values()))
    res = c.process([gdelt(3, where=("Kharkiv, Ukraine", *CHUHUIV))], NOW)
    assert _decision(res, 3)["reason"] == "duplicate_of_event"
    assert ev.title == "Russian drone strike hits Kharkiv" and ev.place_name == "Kharkiv"
    assert (ev.lat, ev.lon) == KHARKIV


@pytest.mark.parametrize("doc,reason", [
    (gdelt(1, root="04", code="042", cat="political"), "irrelevant_category"),
    (gdelt(2, geo_type=1, where=("Ukraine", 49.0, 32.0)), "no_article_backing"),
    (gdelt(3, mentions=1), "no_article_backing"),
    (gdelt(4, published=NOW - timedelta(hours=30)), "stale"),
    (gdelt(5, sqldate="20251121"), "stale"),
])
def test_gdelt_reject_reasons(doc, reason):
    c = make()
    res = c.process([doc], NOW)
    d = _decision(res, doc.origin_id)
    assert (d["decision"], d["reason"]) == ("rejected", reason)
    assert res.gdelt_rejected == {reason: 1} and not c.events


def test_gdelt_low_mentions_accepted_when_corroborated():
    c = make()
    res = c.process([art(1, "Russian drone strike hits Kharkiv"), gdelt(9, mentions=1)], NOW)
    assert _decision(res, 9)["reason"] == "duplicate_of_event"


def test_gdelt_category_from_cameo_map_when_triage_empty():
    c = make(cameo_category=lambda code, root: "armed_conflict" if root == "19" else None)
    c.process([art(1, "Russian drone strike hits Kharkiv")], NOW)
    res = c.process([gdelt(7, cat=None)], NOW)
    assert _decision(res, 7)["reason"] == "duplicate_of_event"


# ------------------------------------------------- article evidence / gates


def test_location_needs_text_evidence_and_country_in_title():
    c = make()
    d = art(1, "EU bans plastic waste exports outside OECD", cat="economic",
            where=("Turkey", 39.0, 35.0), precision="country",
            snippet="The biggest recipient so far was Turkey.")
    c.process([d], NOW)
    ev = next(iter(c.events.values()))
    assert ev.precision == "unlocated" and ev.lat is None  # country only in the snippet
    c2 = make()
    d2 = art(2, "Turkey bans plastic waste imports", cat="economic",
             where=("Turkey", 39.0, 35.0), precision="country")
    c2.process([d2], NOW)
    assert next(iter(c2.events.values())).precision == "country"
    # a hit whose matched text is not in the document is ignored
    c3 = make()
    d3 = art(3, "Drone strike on a city", snippet="")
    d3.places = [place("Kharkiv", *KHARKIV) | {"matched": "Харків"}]
    c3.process([d3], NOW)
    assert next(iter(c3.events.values())).precision == "unlocated"


def test_low_confidence_place_is_not_used():
    c = make()
    d = art(1, "Missile strike reported in Plaza")
    d.places = [place("Laudio", 43.1, -2.9, conf=0.55) | {"matched": "Plaza"}]
    c.process([d], NOW)
    assert next(iter(c.events.values())).lat is None


def test_create_needs_higher_relevance_but_join_does_not():
    c = make(cfg(min_relevance_create=0.5))
    res = c.process([art(1, "Kharkiv shelling", rel=0.45)], NOW)
    assert not c.events and res.skipped == {"below_create_relevance": 1}
    c.process([art(2, "Russian drone strike hits Kharkiv")], NOW)
    res = c.process([art(3, "Russian drone strike hits Kharkiv overnight", rel=0.4,
                         minutes=5)], NOW)
    assert res.updated and next(iter(c.events.values())).mention_count == 2


def test_political_needs_action_keyword():
    gates = {"political": ["sanction*", "expel*", "resign*", "выслал*"]}
    c = make(cfg(category_gates=gates, min_shared_tokens={"default": 2}))
    res = c.process([art(1, "Trump defends his closeness to Putin and Xi", cat="political",
                         where=("Washington", 38.9, -77.0))], NOW)
    assert not c.events and res.skipped == {"no_action": 1}
    res = c.process([art(2, "EU imposes new sanctions on Russian banks", cat="political",
                         where=("Brussels", 50.85, 4.35))], NOW)
    assert len(c.events) == 1
    res = c.process([art(3, "Россия выслала дипломатов", cat="political", lang="ru",
                         where=("Moscow", 55.75, 37.6))], NOW)
    assert len(c.events) == 2


# ------------------------------------------------------------ status/confidence


def _ev(**kw) -> EventState:
    base = {"id": "e", "category": "armed_conflict", "t_start": NOW, "t_end": NOW,
            "first_seen": NOW, "updated_at": NOW}
    return EventState(**{**base, **kw})


def test_status_transitions_and_hooks():
    c = cfg()
    ev = _ev(domains={"a.com"})
    assert event_status(ev, c) == "reported"
    ev.domains.add("b.com")
    assert event_status(ev, c) == "multi-source"
    ev.sensor_corroborated = True
    assert event_status(ev, c) == "sensor-corroborated"
    ev.official = True
    assert event_status(ev, c) == "official"


def test_confidence_components_and_monotonicity():
    c = cfg()
    lo = _ev(domains={"a.com"}, relevance=0.4)
    hi = _ev(domains={"a.com", "b.com", "c.com"}, relevance=0.9, lat=1.0, lon=1.0,
             precision="city", geo_confidence=0.9, gdelt_ids=["1"])
    comps = confidence_components(hi, c, NOW)
    assert comps["source_diversity"] == 0.75 and comps["gdelt"] == 1.0
    assert comps["recency"] == 1.0
    assert 0 <= event_confidence(lo, c, NOW) < event_confidence(hi, c, NOW) <= 1
    assert event_confidence(hi, c, NOW + timedelta(days=3)) < event_confidence(hi, c, NOW)


def test_place_only_join_needs_time_proximity():
    c = make()
    c.process([art(1, "Russian drone strike hits Kharkiv")], NOW)
    later = art(2, "Обстріл міста", lang="uk", minutes=60 * 8)  # place only, 8 h later
    res = c.process([later], NOW + timedelta(hours=8))
    assert res.created and len(c.events) == 2


def test_title_place_beats_snippet_city_and_demonym_ranks_last():
    d = art(1, "Russian missiles hit Poland", where=None,
            snippet="Moscow denied the strike.")
    d.places = [place("Moscow", 55.75, 37.6, country="RU"),
                place("Poland", 52.0, 19.0, precision="country", country="PL"),
                place("Russia", 60.0, 90.0, precision="country", country="RU")
                | {"matched": "Russian"}]
    assert d.text_place(0.65)["name"] == "Poland"


def _oblast_then_town(town=("Chuhuiv", *CHUHUIV), town_country="UA"):
    oblast = place("Kharkiv Oblast", 49.5, 36.5, "admin1")
    first = art(1, "Missile strike in Kharkiv Oblast overnight", where=None)
    first.places = [oblast]
    first.lat, first.lon, first.place_name = 49.5, 36.5, "Kharkiv Oblast"
    first.place_precision, first.place_confidence = "admin1", 0.9
    first.__post_init__()
    name, lat, lon = town
    second = art(2, f"Missile strike in Kharkiv Oblast overnight hits {name} depot",
                 where=None, minutes=20)
    second.places = [oblast, place(name, lat, lon, "city", country=town_country)]
    return first, second


def test_later_article_refines_location_within_country():
    c = make()
    first, second = _oblast_then_town()
    c.process([first], NOW)
    [ev] = c.events.values()
    assert ev.precision == "admin1"
    res = c.process([second], NOW)
    assert len(c.events) == 1 and ev.precision == "city" and ev.place_name == "Chuhuiv"
    assert any(f["field"] == "location_before_refinement" for f in res.fields)


def test_refinement_never_crosses_countries_or_long_distances():
    c = make()
    first, second = _oblast_then_town(town_country="RU")
    c.process([first], NOW)
    c.process([second], NOW)
    [ev] = c.events.values()
    assert ev.precision == "admin1"
    c = make()
    first, second = _oblast_then_town(town=("Lviv", 49.84, 24.03))  # ~900 km away
    c.process([first], NOW)
    c.process([second], NOW)
    assert next(e.place_name for e in c.events.values()) == "Kharkiv Oblast"


def test_event_ids_are_deterministic_by_primary_article():
    a, b = make(), make()
    a.process([art(1, "Missile strike on Kharkiv")], NOW)
    b.process([art(1, "Missile strike on Kharkiv")], NOW + timedelta(hours=1))
    assert list(a.events) == list(b.events) and len(next(iter(a.events))) == 26
