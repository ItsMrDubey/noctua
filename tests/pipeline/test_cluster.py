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
    c = make()
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


def test_irrelevant_documents_skipped():
    c = make()
    res = c.process([art(1, "Kharkiv strike", rel=0.1), art(2, "x", cat=None)], NOW)
    assert res.skipped == {"irrelevant": 2} and not c.events


def test_bookkeeping_same_domain_and_location_upgrade():
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
    assert ev.precision == "city"
    assert ev.t_end - ev.t_start == timedelta(minutes=30)
    assert any(f["field"] == "location" for f in res.fields)


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
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    c = make(cfg(min_shared_tokens={"default": 9}), embedder=Fixed())
    res = c.process([art(1, "Alpha bravo Kharkiv", cat="political"),
                     art(2, "Charlie delta", cat="political", minutes=5)], NOW)
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
    assert ev.status == "multi-source" and res.gdelt_accepted == 1
    assert [x["role"] for x in res.documents if x["doc_id"] == "doc:g100"] == ["gdelt"]


def test_gdelt_new_event_then_duplicate():
    c = make()
    res = c.process([gdelt(1), gdelt(2, url="https://gdeltsrc1.example/story")], NOW)
    assert _decision(res, 1)["reason"] == "new_event"
    assert _decision(res, 2)["reason"] == "duplicate_of_event"
    ev = next(iter(c.events.values()))
    assert ev.gdelt_ids == ["1", "2"] and ev.source_diversity == 1


@pytest.mark.parametrize("doc,reason", [
    (gdelt(1, root="04", code="042", cat="political"), "irrelevant_category"),
    (gdelt(2, geo_type=1, where=("Ukraine", 49.0, 32.0)), "country_centroid_only"),
    (gdelt(3, mentions=1), "low_mentions"),
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
    res = c.process([gdelt(7, cat=None)], NOW)
    assert _decision(res, 7)["reason"] == "new_event"


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
