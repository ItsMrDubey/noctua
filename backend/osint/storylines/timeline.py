"""Storyline timeline: the member events' own timelines (osint.timeline.build, unchanged)
merged into one, so a thread is never timed differently from its events.

Every entry keeps its kind (occurred / reported / observed / milestone), its precision and
basis, and gains the member it came from (``event_id``, ``event_title``, ``event_role``) and a
display ``lane``:

  occurred  claimed occurrence bands      reports   publications
  sensors   FIRMS / quake / hazard / traffic hits     imagery   satellite passes
  status    recorded status changes        system    ingest / evidence-linked clock times

Per-event "first report / first sensor / first imagery" flags are replaced by thread-level
ones; ``T±`` is relative to the thread's first report. Occurred claims are never merged
across events (each event happened when it happened): each member's occurred band is
listed in ``occurred_by_event`` with its basis, and conflicting claims stay per event.
"""

from __future__ import annotations

from datetime import UTC, datetime

from osint.timeline.build import parse_ts, sort_key

THREAD_FLAGS = ("first_report", "first_sensor", "first_imagery")
LANES = ("occurred", "reports", "sensors", "imagery", "status", "system")


def lane_of(e: dict) -> str:
    if e["kind"] == "occurred":
        return "occurred"
    if e["kind"] == "reported":
        return "reports"
    if e["kind"] == "observed":
        return "imagery" if e.get("sensor") == "imagery" else "sensors"
    return "status" if e.get("milestone") == "status" else "system"


def merge(story: dict, timelines: dict[str, dict]) -> dict:
    """``story`` = StorylineStore.get record; ``timelines`` = event id -> build_timeline."""
    members = {m["event_id"]: m for m in story.get("members") or []}
    entries: list[dict] = []
    unplaced: list[dict] = []
    occurred: list[dict] = []
    conflicts: list[dict] = []
    for eid, m in members.items():
        tl = timelines.get(eid)
        if not tl:
            continue
        tag = {"event_id": eid, "event_title": m.get("title"), "event_role": m.get("role")}
        for e in tl.get("entries") or []:
            e = {**e, **tag, "flags": [f for f in e.get("flags") or []
                                       if f not in THREAD_FLAGS]}
            e["lane"] = lane_of(e)
            entries.append(e)
        unplaced += [{**u, **tag} for u in tl.get("unplaced") or []]
        occ = tl.get("occurred") or {}
        occurred.append({**tag, "t": m.get("t"), "t_basis": m.get("t_basis"),
                         "t_precision": m.get("t_precision"), "first_report": (
                             tl.get("anchor") or {}).get("first_report"), **occ})
        if occ.get("conflicts"):
            conflicts.append({**tag, "pairs": occ["conflicts"]})
    # thread-level flags
    rep = [e for e in entries if e["kind"] == "reported" and parse_ts(e.get("t"))]
    first = min(rep, key=lambda e: parse_ts(e["t"])) if rep else None
    if first:
        first["flags"].append("first_report")
    sensed = [e for e in entries if e["kind"] == "observed" and e.get("sensor") != "imagery"
              and parse_ts(e.get("t"))]
    if sensed:
        min(sensed, key=lambda e: parse_ts(e["t"]))["flags"].append("first_sensor")
    post = [e for e in entries if e.get("sensor") == "imagery" and e.get("role") == "after"
            and parse_ts(e.get("t"))]
    if post:
        min(post, key=lambda e: parse_ts(e["t"]))["flags"].append("first_imagery")
    t0 = parse_ts(first["t"]) if first else None
    for e in entries:
        t = parse_ts(e.get("t") or e.get("lo"))
        e["t_rel_min"] = None if (t is None or t0 is None) else round(
            (t - t0).total_seconds() / 60.0, 1)
    entries.sort(key=lambda e: (*sort_key(e), e["event_id"]))
    times = [parse_ts(e.get("t") or e.get("lo")) for e in entries]
    times = [t for t in times if t is not None]
    origin_occ = next((o for o in occurred if o["event_id"] == story.get("origin_event_id")),
                      None) or {}
    origin_tl = timelines.get(story.get("origin_event_id")) or {}
    return {
        "storyline_id": story.get("id"),
        "title": story.get("title"),
        "anchor": {"first_report": first["t"] if first else None},
        "local_offset_h": origin_tl.get("local_offset_h"),
        # the head shows the origin's occurred band (what started the thread)
        "occurred": {k: origin_occ.get(k) for k in (
            "lo", "hi", "precision", "basis", "qualifier", "tz_basis", "n_claims",
            "conflicts") if k in origin_occ} or {"basis": "none", "conflicts": []},
        "occurred_by_event": sorted(occurred, key=lambda o: (o.get("t") or "", o["event_id"])),
        "conflicts": conflicts,
        "entries": entries,
        "unplaced": unplaced,
        "lanes": list(LANES),
        "span": {"lo": _iso(min(times)) if times else None,
                 "hi": _iso(max(times)) if times else None},
        "status_history_recorded": any(t.get("status_history_recorded")
                                       for t in timelines.values()),
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")
