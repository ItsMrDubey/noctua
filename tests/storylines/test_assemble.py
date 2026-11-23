from osint.storylines import assemble
from osint.storylines.config import StorylineConfig
from tests.storylines.fixtures import (
    BENGHAZI,
    benghazi_thread,
    finland_lookalike,
    kyiv_background,
    node,
)

CFG = StorylineConfig()


def _threads(nodes):
    asm = assemble.build(nodes, CFG)
    return asm, {frozenset(s.members): s for s in asm.threads()}


def test_benghazi_thread_roles_title_theatre():
    nodes = benghazi_thread() + finland_lookalike() + kyiv_background()
    _, threads = _threads(nodes)
    bz = next(s for k, s in threads.items() if "E1" in k)
    assert set(bz.members) == {"E1", "E2", "E3", "E4"}
    roles = assemble.roles(bz)
    assert roles["E1"][0] == "origin"
    assert roles["E2"][0] == "development"  # imagery of the damage: the same incident
    assert roles["E3"][0] == "follow_up" and "Second vessel Ascalon struck" in roles["E3"][1]
    assert roles["E4"] == ("reaction", "political statement / response after the origin")
    rep = assemble.representative(bz)
    assert rep.id == "E1"  # most-reported member gives the title
    assert assemble.theatre_of(bz, {"LY": "Libya"}) == ("LY:conflict", "Libya · conflict")
    # every accepted link inside the thread carries its reasons
    assert all(lk.reasons and lk.score >= CFG.linking.link_threshold
               for lk in bz.links.values())


def test_lookalike_incident_far_away_is_its_own_thread():
    _, threads = _threads(benghazi_thread() + finland_lookalike())
    assert frozenset({"F1", "F2"}) in threads
    assert not any({"E1", "F1"} <= k for k in threads)


def test_far_kinetic_event_rejected_even_when_it_links():
    # an attack 2,900 km away whose title shares the thread's rare names: the place says no
    nodes = benghazi_thread() + [node(
        "Z1", 20, "Merkury sister ship Ascalon attacked by drones near Kotka", lat=60.2,
        lon=26.9, place="Kotka", country="FI", angle=10)]
    asm, threads = _threads(nodes)
    assert not any("Z1" in k for k in threads)
    assert asm.stats["rejected_far"] >= 1


def test_recurring_background_does_not_chain():
    _, threads = _threads(kyiv_background(10))
    assert not threads  # a strike on Kyiv every night: no catch-all storyline


def test_statement_never_bridges_to_another_incident():
    # a second government statement condemns a *different* attack: it may join the thread
    # as a reaction, but the other attack never joins through it
    thread = benghazi_thread()
    other_attack = node("O1", 26, "Drone strike damages UN premises in Khartoum", lat=15.5,
                        lon=32.5, place="Khartoum", country="SD", angle=35,
                        mentioned=("p:khartoum",))
    statement = node("O2", 28, "Saudi Arabia condemns drone attacks on Benghazi and Khartoum",
                     category="political", lat=24.7, lon=46.7, place="Riyadh",
                     country="SA", angle=38, mentioned=("p:benghazi", "p:khartoum"))
    _, threads = _threads(thread + [other_attack, statement])
    bz = next(s for k, s in threads.items() if "E1" in k)
    assert "O1" not in bz.members


def test_sensor_events_attach_to_threads_never_alone():
    lat, lon = BENGHAZI
    fire = node("S1", 1, "Wildfire alert", origin="sensor", category="disaster",
                lat=lat + 0.02, lon=lon, detector="hazard_wildfire", precision="point")
    quake = node("S2", 2, "Seismic event", origin="sensor", category="armed_conflict",
                 lat=lat + 0.02, lon=lon, detector="seismic", precision="point")
    lonely = node("S3", 2, "Seismic event", origin="sensor", category="armed_conflict",
                  lat=-30, lon=150, detector="seismic", precision="point")
    _, threads = _threads(benghazi_thread() + [fire, quake, lonely])
    bz = next(s for k, s in threads.items() if "E1" in k)
    assert "S2" in bz.members and assemble.roles(bz)["S2"][0] == "sensor"
    assert "S1" not in bz.members  # disaster alert vs a conflict thread: incompatible
    assert not any("S3" in k for k in threads)


def test_assembly_is_deterministic_and_order_independent():
    nodes = benghazi_thread() + finland_lookalike() + kyiv_background()
    a = sorted(sorted(s.members) for s in assemble.build(nodes, CFG).threads())
    b = sorted(sorted(s.members) for s in assemble.build(nodes[::-1], CFG).threads())
    assert a == b


def test_incremental_add_matches_full_build():
    nodes = benghazi_thread() + finland_lookalike()
    asm = assemble.build(nodes[:2], CFG)
    for n in nodes[2:]:
        asm.corpus.add(n)
        asm.add(n)
    inc = sorted(sorted(s.members) for s in asm.threads())
    full = sorted(sorted(s.members) for s in assemble.build(nodes, CFG).threads())
    assert inc == full
