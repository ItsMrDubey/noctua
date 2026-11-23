"""Evaluation of Terror Watch classification against the hand-labelled sample
(tests/terror/fixtures/eval_labels.json).

Two pools of real data:

* ``platform`` -- news events from the platform's own store (multilingual; full pipeline:
  LLM extraction, rules, outlet counting, duplicate merging);
* ``ucdp`` -- real news headlines that UCDP coded (Candidate 2026): many genuine attacks
  with a reference location (UCDP coordinates), headline-only (no outlet count, so never
  "confirmed").

Each label lists the acceptable outcomes (e.g. ["possible", "attack"]) -- ambiguous items are
not forced into one class. Metrics: precision / recall of "terrorist attack" (reported or
confirmed), precision of "confirmed", class accuracy of plots / arrests / threats,
attribution-level accuracy, country and coordinate accuracy, duplicate rate. Wilson 95 %
intervals are reported with every proportion (small samples).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from osint.core.geo import haversine_km
from osint.terror import classify as cls
from osint.terror.extract import normalise
from osint.terror.schema import INSTRUCTION, TERROR_SCHEMA

LABELS = Path(__file__).resolve().parents[3] / "tests" / "terror" / "fixtures" / \
    "eval_labels.json"
LIVE = ("confirmed", "reported")


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - r) / d, 3), round((c + r) / d, 3)]


def _prop(k: int, n: int) -> dict:
    return {"k": k, "n": n, "value": round(k / n, 3) if n else None, "ci95": wilson(k, n)}


def outcome(rec: dict | None) -> str:
    """Predicted outcome name comparable with the labels."""
    if rec is None or rec.get("status") in (None, "excluded", "pending"):
        return "no"
    if rec.get("merged_into"):
        return "duplicate"
    if rec["status"] == "context":
        return "ct_operation"
    if rec["status"] == "possible":
        return "possible"
    return rec.get("klass") or "no"


def synthetic_event(item: dict) -> dict:
    t = f"{item.get('date') or '2026-01-01'}T12:00:00+00:00"
    return {"id": "eval-" + item["id"], "title": item["title"], "category": "armed_conflict",
            "country": None, "lat": None, "lon": None, "precision": None, "t_start": t,
            "t_end": t, "documents": [{"doc_id": item["id"], "source": "eval:ucdp-headline",
                                       "url": "", "title": item["title"], "published_at": t,
                                       "language": "en"}]}


async def extract_headline(backend, store, item: dict, now: datetime) -> dict | None:
    key = "eval:" + item["id"]
    rec = store.extraction(key)
    if rec and rec.get("status") == "done":
        return rec["result"]
    text = f"Headline: {item['title']}\nDate: {item.get('date')}\n\n{item['title']}"
    res = await backend.extract(INSTRUCTION, text, TERROR_SCHEMA)
    out = normalise(res.data, text)
    store.log_call(now.timestamp(), event_id=key, ok=True, cost_usd=res.cost_usd,
                   latency_s=res.latency_s, model=res.model)
    store.save_extraction(key, now=now, status="done", result=out, cost_usd=res.cost_usd,
                          latency_s=res.latency_s, model=res.model)
    return out


def predictions(job, items: list[dict], now: datetime) -> dict[str, dict | None]:
    """Incident records for every labelled item (platform: from the store after
    ``job.derive``; ucdp: classified from the stored headline extraction)."""
    out: dict[str, dict | None] = {}
    for it in items:
        if it["pool"] == "platform":
            out[it["id"]] = job.store.incident("ti-" + it["id"])
        else:
            rec = job.store.extraction("eval:" + it["id"])
            terr = rec["result"] if rec and rec.get("status") == "done" else None
            out[it["id"]] = cls.classify(synthetic_event(it), None, terr, kb=job.kb,
                                         geocode=job.geocoder, now=now) if terr else None
    return out


def score(items: list[dict], preds: dict[str, dict | None]) -> dict:
    tp = fp = 0
    fps, fns = [], []
    conf_ok = conf_n = 0
    conf_bad = []
    rec_hit = rec_n = 0
    cls_ok = cls_n = 0
    attr_ok = attr_n = 0
    attr_bad = []
    cc_ok = cc_n = 0
    km_ok = km_n = 0
    km_list = []
    group_ok = group_n = 0
    possible_n = 0
    live_by_dup: dict[str, int] = {}
    n_live = 0
    for it in items:
        r = preds.get(it["id"])
        o = outcome(r)
        acc = set(it["label"])
        primary = it["label"][0]
        if o in ("possible",):
            possible_n += 1
        if r is not None and o not in ("no", "duplicate"):
            n_live += 1
            if it.get("dup"):
                live_by_dup[it["dup"]] = live_by_dup.get(it["dup"], 0) + 1
        # terrorist attack (reported or confirmed)
        if o == "attack":
            if "attack" in acc:
                tp += 1
            else:
                fp += 1
                fps.append({"id": it["id"], "title": it["title"][:120], "label": it["label"],
                            "status": r["status"], "rules": r.get("rules")})
        if primary == "attack":
            rec_n += 1
            if o in ("attack", "possible"):
                rec_hit += 1
            else:
                fns.append({"id": it["id"], "title": it["title"][:120], "outcome": o,
                            "reason": (r or {}).get("excluded_reason"),
                            "rules": (r or {}).get("rules")})
        if r is not None and r.get("status") == "confirmed" and not r.get("merged_into"):
            conf_n += 1
            good = (("attack" in acc and o == "attack") or (o in acc)) and \
                it.get("confirmable", False)
            conf_ok += good
            if not good:
                conf_bad.append({"id": it["id"], "title": it["title"][:120],
                                 "label": it["label"]})
        if o in ("plot", "arrest", "prosecution", "threat"):
            cls_n += 1
            cls_ok += o in acc
        if o in ("attack", "possible") and it.get("attr"):
            attr_n += 1
            lvl = (r.get("attribution") or {}).get("level")
            if lvl in it["attr"]:
                attr_ok += 1
            else:
                attr_bad.append({"id": it["id"], "predicted": lvl, "label": it["attr"]})
        if o in ("attack", "possible", "plot", "arrest", "prosecution", "threat") and \
                it.get("cc") and r.get("country"):
            cc_n += 1
            cc_ok += r["country"] == it["cc"]
        if o in ("attack", "possible") and it.get("lat") is not None and \
                r.get("lat") is not None and r.get("precision") in ("point", "city", "admin1"):
            km = haversine_km(r["lat"], r["lon"], it["lat"], it["lon"])
            lim = 50 if r.get("precision") in ("point", "city") else 200
            km_n += 1
            km_ok += km <= lim
            km_list.append({"id": it["id"], "km": round(km), "precision": r.get("precision"),
                            "place": r.get("place_name")})
        if it.get("group") and o in ("attack", "possible"):
            group_n += 1
            g = r.get("group") or {}
            group_ok += g.get("group_id") == it["group"] and g.get("kind") == "link"
    dup_extra = sum(n - 1 for n in live_by_dup.values() if n > 1)
    return {
        "attack_precision": _prop(tp, tp + fp), "attack_recall": _prop(rec_hit, rec_n),
        "confirmed_precision": _prop(conf_ok, conf_n),
        "other_class_precision": _prop(cls_ok, cls_n),
        "attribution_accuracy": _prop(attr_ok, attr_n),
        "group_link_accuracy": _prop(group_ok, group_n),
        "country_accuracy": _prop(cc_ok, cc_n),
        "coordinate_accuracy": _prop(km_ok, km_n),
        "duplicate_rate": _prop(dup_extra, n_live),
        "possible": possible_n, "live_predictions": n_live,
        "false_positives": fps, "missed_attacks": fns, "confirmed_errors": conf_bad,
        "attribution_errors": attr_bad[:20], "distances": km_list,
    }


def report(items: list[dict], preds: dict, *, cost: dict | None = None) -> dict:
    by_pool = {}
    for pool in ("platform", "ucdp"):
        sub = [i for i in items if i["pool"] == pool]
        by_pool[pool] = score(sub, preds)
    langs = {}
    for i in items:
        for lg in i.get("langs") or []:
            langs[lg] = langs.get(lg, 0) + 1
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "n_items": len(items), "pools": {p: sum(1 for i in items if i["pool"] == p)
                                             for p in ("platform", "ucdp")},
            "overall": score(items, preds), "by_pool": by_pool, "llm_cost": cost,
            "notes": [("labels list acceptable outcomes; 'possible' is never counted as a "
                      "terrorist attack in precision"), ("ucdp items are headline-only: no "
                      "outlet count, so they can never be 'confirmed'"),
                      "Wilson 95% intervals; small samples"]}


def load_labels(path: Path = LABELS) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["items"]


async def run(job, items: list[dict], *, llm: bool = True, max_calls: int = 400,
              concurrency: int = 2) -> dict:
    """Make sure every labelled item has a terror extraction (LLM calls only for missing
    ones, bounded by ``max_calls``), re-derive incidents, score. Returns the report."""
    import asyncio

    now = datetime.now(UTC)
    t0 = job.store.cost_since(0)
    if llm and job.backend is not None:
        ids = {i["id"] for i in items if i["pool"] == "platform"}
        cands = [e for e in job.candidates(now) if e["id"] in ids]
        job.cfg.llm.max_per_run = max_calls
        used = job.limiter.counts()
        job.limiter.per_hour = used["hour"] + max_calls
        job.limiter.per_day = used["day"] + max_calls
        sel = job.select_llm(cands, now)
        await job.extract_pass(sel, now)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(it):
            async with sem:
                try:
                    await extract_headline(job.backend, job.store, it, now)
                except Exception as exc:  # noqa: BLE001 — scored as no extraction
                    job.store.log_call(now.timestamp(), event_id="eval:" + it["id"], ok=False,
                                       error=str(exc)[:200])

        await asyncio.gather(*(one(i) for i in items if i["pool"] == "ucdp"))
    job.derive(now)
    preds = predictions(job, items, now)
    t1 = job.store.cost_since(0)
    cost = {"calls_total": t1["calls"], "cost_usd_total": t1["cost_usd"],
            "calls_this_run": t1["calls"] - t0["calls"],
            "cost_usd_this_run": round(t1["cost_usd"] - t0["cost_usd"], 6)}
    return report(items, preds, cost=cost)
