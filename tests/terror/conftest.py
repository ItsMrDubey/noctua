from pathlib import Path

import pytest

from osint.terror.groups import GroupKB

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def kb() -> GroupKB:
    return GroupKB.load(ROOT / "config" / "terror_groups.yaml")


def doc(url: str, title: str, *, lang: str = "en", source: str = "rss:x",
        published: str = "2026-11-20T10:00:00+00:00") -> dict:
    return {"doc_id": url, "source": source, "url": url, "title": title,
            "published_at": published, "language": lang, "role": "mention"}


def event(eid: str = "E1", title: str = "Suicide bomber kills 12 at market in Peshawar",
          docs: list[dict] | None = None, **kw) -> dict:
    docs = docs if docs is not None else [doc("https://www.dawn.com/news/1", title)]
    return {"id": eid, "title": title, "category": "armed_conflict", "country": kw.get("country"),
            "lat": kw.get("lat"), "lon": kw.get("lon"), "precision": kw.get("precision"),
            "place_name": kw.get("place_name"), "t_start": "2026-11-20T10:00:00+00:00",
            "t_end": "2026-11-20T12:00:00+00:00", "status": "reported", "documents": docs}


def terr(**over) -> dict:
    """A verified terror extraction for a clear-cut non-state attack on civilians."""
    base = {
        "schema_version": 1, "kind": "attack", "occurred": "yes", "negation_quote": None,
        "summary": "A suicide bomber killed 12 people at a market.", "attack_type": "suicide",
        "target_type": "civilians",
        "perpetrator": {"name": "Tehrik-i-Taliban Pakistan", "type": "non_state_group",
                        "quote": "Tehrik-i-Taliban Pakistan claimed responsibility"},
        "attribution": {"level": "claimed", "by": "TTP", "channel": "statement",
                        "quote": "Tehrik-i-Taliban Pakistan claimed responsibility"},
        "motive": {"stated": None, "quote": None},
        "criteria": {"political_religious_social_goal": True, "intimidate_wider_audience": True,
                     "civilians_or_noncombatants_targeted": True,
                     "in_armed_conflict_zone": None},
        "where": {"place_name": "Peshawar", "admin1": None, "country": "PK", "precision": "city",
                  "quote": "at a market in Peshawar"},
        "when": None, "when_quote": None,
        "casualties": {"killed": 12, "killed_max": None, "injured": 30, "injured_max": None,
                       "includes_perpetrators": None, "quote": "killed 12 people"},
        "is_follow_up": False, "quotes": [], "unverified": []}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def geocoder(table: dict):
    def geocode(place, admin1, country):
        return table.get(place)
    return geocode
