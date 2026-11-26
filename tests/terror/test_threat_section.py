"""board.threat_section: the official level + advisory parts of the country board alone."""

from datetime import UTC, datetime

from osint.terror import board
from osint.terror.store import TerrorStore

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)


def test_threat_section(tmp_path):
    s = TerrorStore(tmp_path / "t.sqlite")
    s.save_level({"country": "GB", "authority": "MI5", "scope": "national",
                  "level": "SUBSTANTIAL", "rank": 3, "scale_max": 5, "as_of": "2026-04-30",
                  "quote": "q", "source_url": "https://x"}, NOW)
    s.save_level({"country": "GB", "authority": "MI5", "scope": "Northern Ireland",
                  "level": "SEVERE", "rank": 4, "scale_max": 5, "as_of": "2026-04-30",
                  "quote": "q", "source_url": "https://x"}, NOW)
    s.save_advisories([{"country": "GB", "level": 2, "label": "Exercise increased caution",
                        "terrorism": True, "risks": ["terrorism"],
                        "title": "United Kingdom Travel Advisory", "published": "2026-06-01",
                        "updated": "2026-06-01", "url": "https://travel.example/gb",
                        "quote": "q"}], NOW)
    t = board.threat_section(s, "GB")
    # the national level, not the regional one that ranks higher
    assert (t["level"], t["source"], t["rank"], t["scale_max"], t["as_of"]) == \
        ("SUBSTANTIAL", "MI5", 3, 5, "2026-04-30")
    assert t["advisories"] == [{"level": 2, "label": "Exercise increased caution",
                                "title": "United Kingdom Travel Advisory",
                                "url": "https://travel.example/gb", "updated": "2026-06-01",
                                "terrorism": True}]
    assert board.threat_section(s, "FR") == {"level": None, "source": None, "rank": None,
                                             "scale_max": None, "as_of": None,
                                             "advisories": []}
    s.close()
