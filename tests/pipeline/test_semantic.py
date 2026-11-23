"""Cross-language clustering: semantic path guards, English titles, event consolidation,
merge persistence (store, side stores, redirects) and the builder's warm-start merge."""

import sqlite3
from datetime import timedelta

import numpy as np
import pytest

from osint.core.metrics import Metrics
from osint.core.models import PlaceHit
from osint.pipeline.cluster import embed_key, embed_text
from osint.pipeline.events import EventBuilder, EventsConfig, make_scorer
from osint.pipeline.merge import rekey_external
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore
from tests.pipeline.test_cluster import CHUHUIV, NOW, art, cfg, make
from tests.pipeline.test_event_builder import _row

BENGHAZI = ("Benghazi", 32.1167, 20.0667)

# topic keywords in several languages -> one axis each; "other" when none matches
TOPICS = {
    "ship": ("ship", "vessel", "корабл", "судно", "سفينة", "navire", "کشتی"),
    "strike": ("strike", "удар", "ضربة", "frappe"),
    "protest": ("protest", "протест", "manifest"),
}


class TopicEmbedder:
    """Fake sentence encoder: a vector per text from multilingual topic keywords."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts, keys=None):
        self.calls += 1
        out = []
        for t in texts:
            low = t.lower()
            v = np.array([float(any(k in low for k in kws)) for kws in TOPICS.values()]
                         + [0.0], dtype=np.float32)
            if not v.any():
                v[-1] = 1.0
            out.append(v / np.linalg.norm(v))
        return out


def scfg(**kw) -> EventsConfig:
    return cfg(**{"embed_threshold": 0.75, "embed_threshold_contained": 0.78,
                  "embed_threshold_unshared": 0.88, "merge_threshold": 0.75,
                  "merge_threshold_contained": 0.78, "merge_threshold_unshared": 0.88,
                  "compatible_categories": [["armed_conflict", "maritime_security"]], **kw})


def bz(i, title, *, lang="en", cat="armed_conflict", minutes=0, where=BENGHAZI,
       precision="city", country="LY", **kw):
    d = art(i, title, where=where, cat=cat, minutes=minutes, lang=lang, precision=precision,
            **kw)
    for p in d.places:
        p["country"] = country
    return d


# ------------------------------------------------------------------ embed text


def test_embed_text_drops_links_hashtags_and_thin_posts():
    assert embed_text("#Libya #Russia", "") == ""
    assert embed_text("#Ukraine", "https://t.me/x") == ""
    # a snippet that repeats the title replaces it
    assert embed_text("Drone hits ship in port", "Drone hits ship in port. More here") == \
        "Drone hits ship in port. More here"
    assert embed_text("Drone hits ship", "Officials say the port was closed") == \
        "Drone hits ship. Officials say the port was closed"
    assert embed_key("doc:1", "a") != embed_key("doc:1", "b")


# --------------------------------------------------------------- semantic path


def test_cross_language_report_joins_via_semantic_path():
    c = make(scfg(), embedder=TopicEmbedder())
    res = c.process([
        bz(1, "Drone attack hits Russian ship in Benghazi port"),
        bz(2, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم", lang="ar", minutes=20),
        bz(3, "Взрыв на российском корабле в порту Бенгази", lang="ru", minutes=40,
           cat="maritime_security"),  # compatible category
    ], NOW)
    assert len(res.created) == 1 and res.joins.get("semantic", 0) >= 1
    ev = next(iter(c.events.values()))
    assert ev.mention_count == 3 and ev.languages == {"en", "ar", "ru"}
    assert ev.vector is not None and ev.vec_n == 3


def test_semantic_path_guards():
    # same topic vector everywhere: only the guards keep these apart
    e = TopicEmbedder()
    c = make(scfg(), embedder=e)
    res = c.process([
        art(1, "Russian strike on ship repair yard Kharkiv"),
        # a different specific place in the same H3 disk
        art(2, "Удар по судоремонтному заводу, Чугуїв", where=("Chuhuiv", *CHUHUIV),
            lang="uk", minutes=5),
        # incompatible category
        art(3, "Protest over the ship strike in Kharkiv yard", cat="political", lang="fr",
            minutes=10),
        # outside the armed_conflict window (12 h)
        art(4, "Kharkiv: strike on a ship yard, again tonight", lang="es", minutes=60 * 14),
    ], NOW + timedelta(hours=15))
    assert len(res.created) == 4 and res.joins.get("semantic", 0) == 0


def test_country_level_report_needs_the_higher_threshold_and_same_country():
    # a country-level report vs a city event: the 'contained' threshold (unreachable here)
    c = make(scfg(embed_threshold_contained=1.01), embedder=TopicEmbedder())
    res = c.process([
        bz(1, "Drone attack hits Russian ship in Benghazi port"),
        bz(2, "Russian vessel struck by sea drone, officials in Libya say",
           where=("Libya", 26.3, 17.2), precision="country", minutes=10),
    ], NOW)
    assert len(res.created) == 2
    c = make(scfg(), embedder=TopicEmbedder())
    res = c.process([
        bz(1, "Drone attack hits Russian ship in Benghazi port"),
        bz(2, "Russian vessel struck by sea drone, officials in Libya say",
           where=("Libya", 26.3, 17.2), precision="country", minutes=10),
        # another country's ship story never joins, whatever the cosine
        bz(3, "Russian ship hit by drone in the Gulf, Egypt says", country="EG",
           where=("Egypt", 26.8, 30.8), precision="country", minutes=20),
    ], NOW)
    assert len(res.created) == 2
    ev = next(e for e in c.events.values() if e.mention_count == 2)
    assert ev.place_name == "Benghazi"


def test_null_embedder_keeps_lexical_behaviour():
    c = make(scfg())
    res = c.process([bz(1, "Drone attack hits Russian ship in Benghazi port"),
                     bz(2, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم", lang="ar",
                        minutes=20)], NOW)
    # place-only join across languages within place_only_window_h (lexical geo path)
    assert res.joins.get("semantic", 0) == 0


# ---------------------------------------------------------------- English title


def test_english_article_replaces_non_english_title_once():
    c = make(scfg(), embedder=TopicEmbedder())
    c.process([bz(1, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم", lang="ar")], NOW)
    res = c.process([bz(2, "Russian ship explodes in Benghazi port after attack",
                        minutes=30)], NOW + timedelta(minutes=40))
    ev = next(iter(c.events.values()))
    assert ev.title.startswith("Russian ship explodes") and ev.title_lang == "en"
    assert any(f["field"] == "title" for f in res.fields)
    c.process([bz(3, "Взрыв на российском корабле в порту Бенгази", lang="ru",
                  minutes=50)], NOW + timedelta(hours=1))
    assert ev.title.startswith("Russian ship explodes")  # never back to non-English


# ---------------------------------------------------------------- consolidation


def _split(c):
    """Two events of one incident created without embeddings (lexically split)."""
    emb, c.embedder = c.embedder, make(scfg()).embedder  # NullEmbedder
    c.process([bz(1, "Drone attack hits Russian ship in Benghazi port"),
               bz(2, "Second drone attack: another ship burning in Benghazi port",
                  minutes=5),
               bz(3, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم بطائرة مسيرة بحرية",
                  lang="ar", minutes=60 * 7)], NOW)
    c.embedder = emb
    for ev in c.events.values():  # hydrate as the builder does
        ev.vector = TopicEmbedder().embed([ev.title])[0]
        ev.vec_n = ev.mention_count
    return c


def test_consolidate_merges_split_events_into_the_largest():
    c = _split(make(scfg(), embedder=TopicEmbedder()))
    assert len(c.events) == 2
    big = max(c.events.values(), key=lambda e: e.mention_count)
    merges = c.consolidate(None, NOW + timedelta(hours=8))
    assert len(merges) == 1 and merges[0].survivor == big.id
    assert list(c.events) == [big.id]
    assert big.mention_count == 3 and big.languages == {"en", "ar"}
    assert big.title_lang == "en" and "Russian ship" in big.title
    assert any(f["field"] == "merged_event" for f in merges[0].fields)
    assert c.consolidate(None, NOW + timedelta(hours=8)) == []  # idempotent


def test_consolidate_respects_place_category_and_threshold():
    c = make(scfg(), embedder=TopicEmbedder())
    c.process([art(1, "Russian strike on ship repair yard Kharkiv"),
               art(2, "Удар по судоремонтному заводу, Чугуїв", where=("Chuhuiv", *CHUHUIV),
                   lang="uk", minutes=5)], NOW)
    for ev in c.events.values():
        ev.vector = np.ones(4, dtype=np.float32)
    assert c.consolidate(None, NOW) == []  # different specific places
    c = make(scfg(merge_threshold=1.01), embedder=TopicEmbedder())
    _split(c)
    assert c.consolidate(None, NOW + timedelta(hours=8)) == []


def test_merge_keeps_the_finer_location():
    c = make(scfg())  # no embedder: the events are created split
    c.process([bz(1, "Russian vessel struck by sea drone, officials in Libya say",
                  where=("Libya", 26.3, 17.2), precision="country"),
               bz(2, "Libya: drone boats hit a Russian ship, say officials",
                  where=("Libya", 26.3, 17.2), precision="country", url="https://x.example/2",
                  minutes=1)], NOW)
    c.process([bz(3, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم بطائرة مسيرة بحرية",
                  lang="ar", minutes=60 * 7)], NOW + timedelta(hours=7))
    assert len(c.events) == 2  # created before vectors existed
    c.embedder = TopicEmbedder()
    for ev in c.events.values():
        ev.vector = TopicEmbedder().embed(["ship"])[0]
    (m,) = c.consolidate(None, NOW + timedelta(hours=8))
    ev = c.events[m.survivor]
    assert ev.mention_count == 3 and ev.place_name == "Benghazi" and ev.precision == "city"


# -------------------------------------------------------------- persistence


def test_store_merge_moves_links_and_redirects(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    c = _split(make(scfg(), embedder=TopicEmbedder()))
    big = max(c.events.values(), key=lambda e: e.mention_count)
    small = min(c.events.values(), key=lambda e: e.mention_count)
    store.save_events([e.to_row() for e in c.events.values()], documents=[
        {"event_id": big.id, "doc_id": "doc:1", "source": "s", "url": "u1", "title": "t",
         "published_at": "2026-11-21T11:00:00.000+00:00", "language": "en",
         "role": "primary", "added_at": "x"},
        {"event_id": small.id, "doc_id": "doc:3", "source": "s", "url": "u3", "title": "t",
         "published_at": "2026-11-21T18:00:00.000+00:00", "language": "ar",
         "role": "primary", "added_at": "x"}],
        fields=[{"event_id": small.id, "field": "location", "value": "Benghazi",
                 "doc_id": "doc:3", "quote": "q"}],
        decisions=[{"gdelt_event_id": "g1", "decision": "accepted", "reason": "dup",
                    "event_id": small.id, "decided_at": "x"}])
    store.save_verifications([{"event_id": small.id, "verdict": "unsupported", "reasons": [],
                               "quotes": [], "fingerprint": "f"}])
    store.save_corroborations([{"event_id": small.id, "kind": "fire", "ref_id": "r1",
                                "rule": "x", "score": 0.5}],
                              summaries=[{"event_id": small.id, "score": 0.5,
                                          "kinds": ["fire"], "evidence": 1,
                                          "corroborated": False}])
    merges = c.consolidate(None, NOW + timedelta(hours=8))
    recs = [{"survivor": m.survivor, "loser": m.loser, "similarity": m.similarity,
             "fields": m.fields} for m in merges]
    assert store.merge_events(recs, [big.to_row()]) == 1
    ev = store.get_event(big.id)
    docs = {d["doc_id"]: d["role"] for d in ev["documents"]}
    assert docs == {"doc:1": "primary", "doc:3": "mention"}
    assert {f["field"] for f in ev["fields"]} >= {"location", "merged_event"}
    assert ev["mention_count"] == 3
    # the old id resolves to the survivor
    assert store.get_event(small.id)["redirected_from"] == small.id
    assert store.get_event(small.id, follow=False) is None
    assert store.resolve_event_id(small.id) == big.id
    # a rejected verdict is not inherited; corroborations and GDELT decisions move
    assert store.verifications([big.id]) == {}
    assert [r["ref_id"] for r in store.list_corroborations(big.id)] == ["r1"]
    assert store.corroboration_summaries([big.id])[big.id]["score"] == 0.5
    db = sqlite3.connect(tmp_path / "s.sqlite")
    assert db.execute("SELECT event_id FROM gdelt_decisions").fetchone()[0] == big.id
    assert db.execute("SELECT COUNT(*) FROM events WHERE id=?", (small.id,)).fetchone()[0] == 0
    # idempotent: the loser is gone
    assert store.merge_events(recs, [big.to_row()]) == 0
    # chains: merging the survivor later re-points the old redirect
    t = big.to_row()
    t["id"] = "Z" * 26
    store.save_events([t])
    store.merge_events([{"survivor": "Z" * 26, "loser": big.id, "similarity": 0.9}], [t])
    assert store.resolve_event_id(small.id) == "Z" * 26
    store.close()


def test_store_merge_never_touches_other_origins(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    c = _split(make(scfg(), embedder=TopicEmbedder()))
    rows = [e.to_row() for e in c.events.values()]
    rows[1]["origin"] = "historical"
    store.save_events(rows)
    assert store.merge_events([{"survivor": rows[0]["id"], "loser": rows[1]["id"],
                                "similarity": 1.0}], []) == 0
    assert store.get_event(rows[1]["id"], follow=False) is not None
    store.close()


def test_rekey_external_moves_supported_extractions_and_imagery(tmp_path):
    from osint.imagery.store import ImageryStore
    from osint.pipeline.extract.store import ExtractionStore

    ext = ExtractionStore(tmp_path / "extraction.sqlite")
    ext.save("L1", doc_id="d", url="u", status="done", now=NOW, verdict="supported")
    ext.save("L2", doc_id="d", url="u", status="done", now=NOW, verdict="unsupported")
    ext.close()
    img = ImageryStore(tmp_path / "imagery.sqlite")
    img.upsert("L1", "after", status="ok", now=NOW, path="imagery/L1/after.png")
    img.upsert("S", "after", status="ok", now=NOW, path="imagery/S/after.png")
    img.upsert("L1", "before", status="ok", now=NOW, path="imagery/L1/before.png")
    img.close()
    out = rekey_external(tmp_path, [{"survivor": "S", "loser": "L1"},
                                    {"survivor": "S", "loser": "L2"}])
    assert out == {"extraction": 1, "imagery": 1}
    ext = ExtractionStore(tmp_path / "extraction.sqlite")
    assert ext.get("S")["verdict"] == "supported" and ext.get("L2") is not None
    ext.close()
    img = ImageryStore(tmp_path / "imagery.sqlite")
    assert {a["role"]: a["path"] for a in img.get_assets("S")} == {
        "after": "imagery/S/after.png", "before": "imagery/L1/before.png"}
    img.close()
    assert rekey_external(tmp_path / "missing", [{"survivor": "S", "loser": "L"}]) == {
        "extraction": 0, "imagery": 0}


# --------------------------------------------------------------------- builder


LIBYA_HIT = PlaceHit(name="Benghazi", country="LY", lat=32.1167, lon=20.0667,
                     precision="city", confidence=0.9, matched="Benghazi")


def test_builder_warm_start_hydrates_and_merges_split_events(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    t0 = NOW - timedelta(hours=8)
    rows = [_row(1, "Drone attack hits Russian ship in Benghazi port", fetched=t0,
                 place=LIBYA_HIT),
            _row(2, "Second drone attack: another ship burning in Benghazi port",
                 fetched=t0 + timedelta(minutes=5), place=LIBYA_HIT),
            _row(3, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم بطائرة مسيرة بحرية",
                 fetched=t0 + timedelta(hours=7), place=LIBYA_HIT)]
    rows[2]["language"], rows[2]["snippet"] = "ar", "Benghazi"
    lake.write("documents", rows)
    clock = [NOW]
    c = scfg()
    # first: lexical only (as before this change) -> two events
    b = EventBuilder(store, lake, Metrics(), c, clock=lambda: clock[0])
    b.run_once()
    assert len(store.list_events()) == 2
    old = {e["id"] for e in store.list_events()}
    # restart with an embedder: warm start hydrates vectors and merges
    emb = TopicEmbedder()
    m = Metrics()
    b2 = EventBuilder(store, lake, m, c, clock=lambda: clock[0], embedder=emb)
    b2.run_once()
    events = store.list_events()
    assert len(events) == 1 and emb.calls >= 1
    survivor = events[0]["id"]
    (gone,) = old - {survivor}
    assert store.resolve_event_id(gone) == survivor
    assert m.get("events_merged_total") == 1
    full = store.get_event(survivor)
    assert len(full["documents"]) == 3 and full["title"].startswith(("Drone", "Second"))
    # a second restart finds nothing more to merge
    b3 = EventBuilder(store, lake, Metrics(), c, clock=lambda: clock[0], embedder=emb)
    b3.run_once()
    assert len(store.list_events()) == 1
    store.close()
    lake.close()


def test_builder_semantic_scorer_unchanged_without_embedder(tmp_path):
    c = scfg()
    b = EventBuilder(SqliteStore(tmp_path / "s.sqlite"), Lake(tmp_path / "lake"), Metrics(),
                     c)
    assert b.consolidate(NOW) == 0 and b.hydrate() == 0
    assert make_scorer(c)  # scorer construction unaffected
    b.store.close()
    b.lake.close()


@pytest.mark.parametrize("cats,ok", [(("maritime_security",), True), (("political",), False)])
def test_compatible_categories(cats, ok):
    assert scfg().compatible("armed_conflict", list(cats)) is ok


def test_place_relation_tiers():
    c = make(scfg())  # lexical only: three separate events
    c.process([bz(1, "Drone attack hits Russian ship in Benghazi port"),
               bz(2, "Libya: drone boats hit a Russian ship, say officials", minutes=1,
                  where=("Libya", 26.3, 17.2), precision="country", url="https://x.example/2"),
               bz(3, "Libyan navy: second vessel hit by drone boats off the coast",
                  minutes=2, where=("Libya", 26.3, 17.2), precision="country",
                  url="https://y.example/3", cat="maritime_security")], NOW)
    city = next(e for e in c.events.values() if e.precision == "city")
    countries = [e for e in c.events.values() if e.precision == "country"]
    assert c._place_relation(city, city) == "shared"
    assert len(countries) == 2
    assert {c._place_relation(city, e) for e in countries} == {"contained"}
    assert c._place_relation(*countries) == "none"
    k = make(scfg())
    k.process([art(1, "Russian strike on ship repair yard Kharkiv"),
               art(2, "Удар по судоремонтному заводу, Чугуїв", where=("Chuhuiv", *CHUHUIV),
                   lang="uk", minutes=5)], NOW)
    a, b = k.events.values()
    assert k._place_relation(a, b) is None  # two specific places, never the same incident
    cf = scfg()
    assert (cf.threshold("shared"), cf.threshold("contained"), cf.threshold("none"),
            cf.threshold("none", merge=True)) == (0.75, 0.78, 0.88, 0.88)


# ------------------------------------------------------------------- titles


def test_merge_keeps_a_title_with_a_cached_translation():
    c = make(scfg())
    c.process([bz(1, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم بطائرة مسيرة بحرية",
                  lang="ar"),
               bz(2, "Взрыв на российском корабле в порту Бенгази после атаки дрона",
                  lang="ru", minutes=60 * 7, cat="maritime_security")], NOW)
    assert len(c.events) == 2
    c.embedder = TopicEmbedder()
    for ev in c.events.values():
        ev.vector = TopicEmbedder().embed(["ship"])[0]
    c.translated = lambda lang, text: lang == "ru"  # only the Russian title is translated
    (m,) = c.consolidate(None, NOW + timedelta(hours=8))
    ev = c.events[m.survivor]
    assert ev.title.startswith("Взрыв") and ev.title_lang == "ru"
    assert any(f["field"] == "title" and "translation" in f["quote"] for f in m.fields)


def test_hydrate_gives_old_events_their_english_title(tmp_path):
    from osint.store.sqlite import iso

    store = SqliteStore(tmp_path / "s.sqlite")
    c = make(scfg())
    c.process([bz(1, "سفينة روسية تنفجر في ميناء بنغازي بعد هجوم بطائرة مسيرة بحرية",
                  lang="ar")], NOW)
    (ev,) = c.events.values()
    row = ev.to_row()
    row["cluster_state"].pop("title_lang")  # written before title languages existed
    docs = [{"event_id": ev.id, "doc_id": "doc:1", "source": "s", "url": "u1",
             "title": ev.title, "published_at": iso(NOW - timedelta(hours=1)),
             "language": "ar", "role": "primary", "added_at": iso(NOW)},
            {"event_id": ev.id, "doc_id": "doc:9", "source": "s", "url": "u9",
             "title": "Russian ship explodes in Benghazi port",
             "published_at": iso(NOW), "language": "en", "role": "mention",
             "added_at": iso(NOW)}]
    store.save_events([row], documents=docs)
    lake = Lake(tmp_path / "lake")
    b = EventBuilder(store, lake, Metrics(), scfg(), clock=lambda: NOW + timedelta(hours=1))
    b.run_once()
    got = store.get_event(ev.id)
    assert got["title"] == "Russian ship explodes in Benghazi port"
    assert any(f["field"] == "title" and f["doc_id"] == "doc:9" for f in got["fields"])
    assert got["updated_at"] == row["updated_at"]  # no fake "update" for the list order
    store.close()
    lake.close()


def test_translated_lookup_reads_the_translation_cache(tmp_path):
    from osint.pipeline.events import translated_lookup
    from osint.translate.store import TranslationStore, clean, text_key

    ts = TranslationStore(tmp_path / "t.sqlite")
    text = "سفينة روسية  تنفجر"
    key = text_key("ar", clean(text))
    ts.enqueue([{"key": key, "lang": "ar", "text": clean(text), "prio": 0, "sort_ts": "",
                 "english": False}], NOW)
    f = translated_lookup(ts)
    assert f("ar", text) is False
    ts.save_done({key: "Russian ship explodes"}, "m", NOW)
    assert f("ar", text) is True and f("fa", text) is False
    assert translated_lookup(None) is None
    ts.close()
