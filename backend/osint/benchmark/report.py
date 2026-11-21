"""Static Markdown investor brief: ``python -m osint.benchmark.report --window 7d
--out digests/benchmark-<date>.md`` (``<date>`` is replaced with today's UTC date)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from osint.benchmark.data import Sources, parse_ts
from osint.benchmark.sample import gdelt_details
from osint.benchmark.scorecard import BenchmarkService, iso_db
from osint.benchmark.store import BenchmarkStore
from osint.core.geo import utcnow

TITLES = {
    "duplicated": "An incident GDELT duplicated that we merged",
    "centroid": "A GDELT country-centroid row we rejected",
    "sensor": "A sensor-corroborated event",
    "mismatch": "A mismatch our verifier caught",
    "latency": "A latency example",
}


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "—"
    if unit == "USD":
        return f"${v:,.4f}"
    if unit == "%":
        return f"{v:.1f}%"
    if isinstance(v, float):
        return f"{v:,.2f}" if unit != "min" else f"{v:,.1f} min"
    return f"{v:,}" + (f" {unit}" if unit == "min" else "")


def find_examples(src: Sources, start: datetime, now: datetime) -> dict[str, dict | None]:
    """Five illustrative cases from the window (None where the window has none)."""
    s = iso_db(start)
    out: dict[str, dict | None] = dict.fromkeys(TITLES)
    centroid_ids: list[tuple[str, str]] = []
    with src.events_db() as db:
        if db is None:
            return out
        row = db.execute(
            "SELECT e.id, e.title, e.place_name, e.precision, COUNT(*) AS n, "
            "(SELECT COUNT(*) FROM event_documents d WHERE d.event_id = e.id "
            " AND d.role != 'gdelt') FROM gdelt_decisions g JOIN events e ON e.id = g.event_id "
            "WHERE g.decision = 'accepted' AND e.t_end >= ? GROUP BY e.id "
            "ORDER BY n DESC LIMIT 1", (s,)).fetchone()
        if row:
            gids = [r[0] for r in db.execute(
                "SELECT gdelt_event_id FROM gdelt_decisions WHERE event_id = ? "
                "AND decision = 'accepted' LIMIT 200", (row[0],))]
            out["duplicated"] = {"event_id": row[0], "title": row[1], "place": row[2],
                                 "precision": row[3], "gdelt_rows": row[4], "articles": row[5],
                                 "gdelt_ids": gids}
        row = db.execute(
            "SELECT e.id, e.title, e.place_name, c.score, c.kinds FROM event_corroboration c "
            "JOIN events e ON e.id = c.event_id WHERE c.corroborated = 1 AND e.t_end >= ? "
            "AND e.id NOT IN (SELECT event_id FROM sensor_events) ORDER BY c.score DESC "
            "LIMIT 1", (s,)).fetchone()
        if row:
            out["sensor"] = {"event_id": row[0], "title": row[1], "place": row[2],
                             "score": row[3], "kinds": json.loads(row[4] or "[]")}
        else:
            row = db.execute(
                "SELECT e.id, e.title, e.place_name, s.detector FROM sensor_events s JOIN "
                "events e ON e.id = s.event_id WHERE e.t_end >= ? ORDER BY e.confidence DESC "
                "LIMIT 1", (s,)).fetchone()
            if row:
                out["sensor"] = {"event_id": row[0], "title": row[1], "place": row[2],
                                 "detector": row[3], "kinds": []}
        row = db.execute(
            "SELECT e.id, e.title, e.place_name, v.reasons, v.quotes FROM event_verification v "
            "JOIN events e ON e.id = v.event_id WHERE v.verdict = 'mismatch' AND e.t_end >= ? "
            "ORDER BY e.source_diversity DESC, e.id LIMIT 1", (s,)).fetchone()
        if row:
            out["mismatch"] = {"event_id": row[0], "title": row[1], "place": row[2],
                               "reasons": json.loads(row[3] or "[]"),
                               "quotes": json.loads(row[4] or "[]")}
        best = None
        for eid, title, fs, fa, fg in db.execute(
                "SELECT e.id, e.title, e.first_seen, "
                "MIN(CASE WHEN d.role != 'gdelt' THEN d.published_at END), "
                "MIN(CASE WHEN d.role = 'gdelt' THEN d.published_at END) FROM events e "
                "JOIN event_documents d ON d.event_id = e.id WHERE e.t_end >= ? "
                "AND e.origin = 'news' "
                "GROUP BY e.id", (s,)):
            fs, fa, fg = parse_ts(fs), parse_ts(fa), parse_ts(fg)
            if fs is None or fa is None or fg is None:
                continue
            lead = (fg - fs).total_seconds() / 60
            if abs(lead) <= src.cfg.latency_max_h * 60 and (best is None or lead > best[0]):
                best = (lead, {"event_id": eid, "title": title, "first_article": fa.isoformat(),
                               "our_first_seen": fs.isoformat(),
                               "gdelt_first_row": fg.isoformat(), "lead_min": round(lead, 1)})
        out["latency"] = best[1] if best else None
        centroid_ids = db.execute(
            "SELECT gdelt_event_id, reason FROM gdelt_decisions WHERE decided_at >= ? AND "
            "decision = 'rejected' AND reason IN ('no_article_backing', 'no_location') "
            "ORDER BY rowid DESC LIMIT 2000", (s,)).fetchall()
    if out["duplicated"]:
        det = gdelt_details(src, out["duplicated"].pop("gdelt_ids"), start, now)
        out["duplicated"]["gdelt_places"] = sorted(
            {d["geo_name"] for d in det.values() if d.get("geo_name")})[:8]
        out["duplicated"]["gdelt_urls"] = len({d["url"] for d in det.values() if d.get("url")})
    if centroid_ids:
        reasons = dict(centroid_ids)
        det = gdelt_details(src, list(reasons), start, now)
        cands = [(int(d.get("num_mentions") or 0), gid, d) for gid, d in det.items()
                 if str(d.get("geo_type")) == "1"]
        if cands:
            _, gid, d = max(cands, key=lambda c: (c[0], c[1]))
            out["centroid"] = {"gdelt_event_id": gid, "reason": reasons[gid], **d}
    return out


def _example_md(key: str, ex: dict | None) -> list[str]:
    lines = [f"### {TITLES[key]}", ""]
    if ex is None:
        return [*lines, "_No example in this window._", ""]
    if key == "duplicated":
        lines.append(f"**{ex['title']}** ({ex.get('place') or 'unlocated'}, "
                     f"{ex['precision']}): GDELT emitted **{ex['gdelt_rows']} rows** "
                     f"(from {ex.get('gdelt_urls', '?')} distinct source URLs) for this one "
                     f"incident; we hold it as **1 event** backed by {ex['articles']} "
                     f"article(s).")
        if ex.get("gdelt_places"):
            lines.append(f"GDELT placed those rows at: {'; '.join(ex['gdelt_places'])}.")
    elif key == "centroid":
        lines.append(f"GDELT row {ex['gdelt_event_id']} — \"{ex.get('title')}\" — was placed "
                     f"only at the country centroid ({ex.get('geo_name')}, ActionGeo_Type 1) "
                     f"with {ex.get('num_mentions')} mentions. We rejected it "
                     f"(`{ex['reason']}`). Source: {ex.get('url')}")
    elif key == "sensor":
        what = (f"sensor evidence: {', '.join(ex['kinds'])} (score {ex['score']:.2f})"
                if ex.get("kinds") else f"created by the `{ex.get('detector')}` detector")
        lines.append(f"**{ex['title']}** ({ex.get('place') or 'unlocated'}) — {what}. "
                     "GDELT cannot corroborate events with physical sensors.")
    elif key == "mismatch":
        lines.append(f"**{ex['title']}** ({ex.get('place') or 'unlocated'}): the extractor "
                     f"read the article and flagged a mismatch: "
                     f"{'; '.join(map(str, ex['reasons'])) or 'n/a'}.")
        if ex.get("quotes"):
            q = ex["quotes"][0]
            text = (q.get("quote") or q.get("text")) if isinstance(q, dict) else q
            field = f" ({q.get('field')})" if isinstance(q, dict) and q.get("field") else ""
            lines.append(f"> {text}{field}")
    elif key == "latency":
        side = "before" if ex["lead_min"] > 0 else "after"
        lines.append(f"**{ex['title']}**: first article {ex['first_article']}, our event "
                     f"{ex['our_first_seen']}, GDELT's first row {ex['gdelt_first_row']} — "
                     f"we had it {abs(ex['lead_min']):.0f} min {side} GDELT.")
    return [*lines, ""]


def render(card: dict, examples: dict[str, dict | None]) -> str:
    m = card["metrics"]
    d, g, lat = m["duplication"], m["geolocation"], m["latency"]
    lines = [
        f"# GDELT 2.0 benchmark — {card['window']} window",
        "",
        (f"As of {card['as_of']} (window from {card['window_start']}; platform producing "
        f"events since {card.get('live_since') or 'n/a'})."),
        "",
        "## Headline: ours vs GDELT",
        "",
        "| Metric | Ours | GDELT | Better |",
        "|---|---|---|---|",
    ]
    for h in card["headline"]:
        gd = _fmt(h["gdelt"], h["unit"]) if h["gdelt"] is not None else \
            (h.get("gdelt_note") or "—")
        if h["gdelt"] is not None and h.get("gdelt_note"):
            gd += f" ({h['gdelt_note']})"
        lines.append(f"| {h['label']} | {_fmt(h['ours'], h['unit'])} | {gd} | "
                     f"{h['better']} |")
    lines += [
        "",
        (f"**Duplication:** {d['statement']} — a {_fmt(d['dedup_ratio'])}x reduction. "
        f"Across the whole GDELT feed in our categories, {d['gdelt']['window_rows_tracked']:,} "
        f"rows collapse to {d['gdelt']['window_distinct_keys_tracked']:,} distinct "
        "(date, code, actors, place) keys even before any text-level grouping."),
        "",
        (f"**Geolocation:** our news events — {g['ours']['by_precision']}; GDELT (tracked "
        f"categories) — {g['gdelt']['tracked']['by_type']}. GDELT country-centroid rows we "
        f"rejected: {g['gdelt_country_centroid_rejected']['total']:,}. On matched rows our "
        f"location was finer {g['matched_rows']['ours_finer']}x, equal "
        f"{g['matched_rows']['same']}x, coarser {g['matched_rows']['ours_coarser']}x."),
        "",
        (f"**Latency:** basis `{lat['basis']}` over {lat['headline']['pairs']} matched "
        f"incidents — ours median {_fmt(lat['headline']['ours_median_min'], 'min')} / p90 "
        f"{_fmt(lat['headline']['ours_p90_min'], 'min')}; GDELT median "
        f"{_fmt(lat['headline']['gdelt_median_min'], 'min')} / p90 "
        f"{_fmt(lat['headline']['gdelt_p90_min'], 'min')}. Our live time-to-event over "
        f"{lat['ours_time_to_event_live']['events']} events: median "
        f"{_fmt(lat['ours_time_to_event_live']['median_min'], 'min')}."),
        "",
        f"**Verification:** {m['verification']['gdelt']['capability']}",
        "",
        "## Method",
        "",
    ]
    for key, metric in m.items():
        lines.append(f"- **{key.replace('_', ' ').title()}** — {metric['definition']}")
    lines += ["", "## Caveats", ""]
    lines += [f"- {c}" for c in card["caveats"]]
    lines += ["", "## Illustrative examples", ""]
    for key in TITLES:
        lines += _example_md(key, examples.get(key))
    lines += ["## Raw numbers", "", "```json",
              json.dumps(m, indent=1, default=str, ensure_ascii=False), "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from osint.config import Settings
    from osint.store.lake import Lake

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", default="7d")
    ap.add_argument("--out", default="digests/benchmark-<date>.md")
    args = ap.parse_args(argv)
    settings = Settings()
    lake = Lake(settings.lake_dir, duckdb_memory_limit="256MB", duckdb_threads=2)
    src = Sources.from_settings(settings, lake)
    if args.window not in src.cfg.windows:
        ap.error(f"unknown window {args.window!r}; choose from {sorted(src.cfg.windows)}")
    store = BenchmarkStore(settings.data_dir / "benchmark.sqlite")
    try:
        now = utcnow()
        card = BenchmarkService(src, store).compute(args.window, now)
        start = now - timedelta(hours=src.cfg.window_hours(args.window))
        text = render(card, find_examples(src, start, now))
    finally:
        store.close()
        lake.close()
    out = Path(args.out.replace("<date>", now.strftime("%Y-%m-%d")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
