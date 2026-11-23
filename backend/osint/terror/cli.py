"""`osint terror-pass` (one Terror Watch pass outside the worker) and `osint terror-eval`
(score the classifier on the hand-labelled sample; writes data_dir/terror_eval.json)."""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace


def add_parsers(sub) -> None:
    p = sub.add_parser("terror-pass", help="run one Terror Watch pass (sources, LLM, incidents, "
                       "indicators) against the configured data dir")
    p.add_argument("--only", default="", help="comma-separated source ids to refresh")
    p.add_argument("--no-llm", action="store_true", help="skip LLM extraction")
    e = sub.add_parser("terror-eval", help="evaluate Terror Watch on the labelled sample")
    e.add_argument("--no-llm", action="store_true", help="score stored extractions only")
    e.add_argument("--max-calls", type=int, default=400, help="cap on new LLM calls")


def _runtime():
    from osint.config import Settings
    from osint.enrich.gazetteer import LazyGazetteer
    from osint.store.sqlite import SqliteStore

    settings = Settings()
    return SimpleNamespace(settings=settings,
                           store=SqliteStore(settings.sqlite_path, migrate=False),
                           triage=SimpleNamespace(
                               gazetteer=LazyGazetteer(settings.data_dir / "gazetteer")),
                           _tasks=[])


def run(args: argparse.Namespace) -> int:
    from osint.terror import evaluate
    from osint.terror.job import build_job, utcnow

    rt = _runtime()
    job = build_job(rt)

    async def go() -> dict:
        if args.cmd == "terror-eval":
            rep = await evaluate.run(job, evaluate.load_labels(), llm=not args.no_llm,
                                     max_calls=args.max_calls)
            out = rt.settings.data_dir / "terror_eval.json"
            out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
            return {k: rep["overall"][k] for k in (
                "attack_precision", "attack_recall", "confirmed_precision",
                "other_class_precision", "attribution_accuracy", "country_accuracy",
                "coordinate_accuracy", "duplicate_rate")} | {"cost": rep["llm_cost"],
                                                             "report": str(out)}
        only = [s for s in args.only.split(",") if s] or None
        res = {"sources": await job.refresh_sources(only=only)}
        if not args.no_llm:
            now = utcnow()
            res["llm"] = await job.extract_pass(job.select_llm(job.candidates(now), now), now)
        res["derive"] = job.derive()
        res["indicators"] = job.compute_indicators()
        return res

    print(json.dumps(asyncio.run(go()), indent=1, default=str))
    return 0
