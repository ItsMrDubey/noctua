"""Calibrate storyline linking on real data (read-only; run it on a copy of data/).

    python -m osint.storylines.calibrate pairs --data COPY --out pairs.jsonl
        candidate pairs (hard gates passed: compatible categories, gap, an indexed anchor),
        a stratified random sample of --per-bin pairs per score bin, each with its sampling
        weight (pairs in the bin / pairs sampled); add "label": 1 (same thread) or 0 (not)
        to each line by hand
    python -m osint.storylines.calibrate eval --data COPY --labels pairs.jsonl
        re-scores the labelled pairs with the current config and prints precision / recall
        per threshold, weighted back to the whole candidate population (a pair counts as
        predicted when it links: score and anchor)
    python -m osint.storylines.calibrate run --data COPY
        the threads a full pass builds, with roles and link reasons, plus the pass cost

``--now`` fixes the clock (ISO time) so a copy taken earlier gives the same window.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

from osint.storylines import assemble
from osint.storylines.config import StorylineConfig
from osint.storylines.features import load_nodes
from osint.storylines.link import score_pair
from osint.timeline.build import parse_ts


def _setup(args):
    from osint.config import ROOT
    from osint.core.geo import utcnow
    from osint.store.sqlite import SqliteStore
    from osint.translate.store import TranslationStore

    data = Path(args.data)
    cfg = StorylineConfig.load(Path(args.config or ROOT / "config" / "storylines.yaml"))
    store = SqliteStore(data / "osint.sqlite", migrate=False)
    tr = TranslationStore(data / "translations.sqlite") \
        if (data / "translations.sqlite").is_file() else None
    now = parse_ts(args.now) if args.now else utcnow()
    t0 = time.perf_counter()
    nodes = load_nodes(store, now - timedelta(days=cfg.window_days), translations=tr)
    load_s = time.perf_counter() - t0
    return cfg, nodes, load_s


BINS = (-99.0, 1.5, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 99.0)


def _pairs(asm: assemble.Assembler, cfg: StorylineConfig):
    seen = set()
    for nd in asm.nodes.values():
        if nd.origin != "news":
            continue
        for o in asm.candidates(nd):
            if o.origin != "news":
                continue
            a, b = (o, nd) if (o.t, o.id) <= (nd.t, nd.id) else (nd, o)
            if (a.id, b.id) in seen:
                continue
            seen.add((a.id, b.id))
            lk = score_pair(a, b, asm.corpus, cfg.linking)
            if lk is not None:
                yield a, b, lk


def _desc(n) -> dict:
    return {"id": n.id, "title": n.title, "place": n.place_name, "precision": n.precision,
            "category": n.category, "t": n.t.isoformat(timespec="minutes")}


def cmd_pairs(args) -> int:
    cfg, nodes, _ = _setup(args)
    asm = assemble.build(nodes, cfg)
    rows = list(_pairs(asm, cfg))
    rng = random.Random(args.seed)
    pick = []
    for lo, hi in itertools.pairwise(BINS):
        inbin = [r for r in rows if lo <= r[2].score < hi]
        rng.shuffle(inbin)
        take = inbin[:args.per_bin]
        w = len(inbin) / len(take) if take else 0.0
        pick += [(*r, w, f"{lo:g}..{hi:g}") for r in take]
        print(f"bin {lo:>5g}..{hi:<5g} {len(inbin):6d} pairs, sampled {len(take)}")
    with open(args.out, "w", encoding="utf-8") as f:
        for a, b, lk, w, bin_ in sorted(pick, key=lambda r: -r[2].score):
            f.write(json.dumps({"a": _desc(a), "b": _desc(b), "score": lk.score,
                                "linked": lk.linked, "why": lk.text, "bin": bin_,
                                "weight": round(w, 3), "label": None},
                               ensure_ascii=False) + "\n")
    print(f"{len(rows)} gated pairs, wrote {len(pick)}")
    return 0


def cmd_eval(args) -> int:
    cfg, nodes, _ = _setup(args)
    asm = assemble.build(nodes, cfg)
    by = asm.nodes
    labelled = [json.loads(x) for x in Path(args.labels).read_text(encoding="utf-8").splitlines()
                if x.strip()]
    labelled = [x for x in labelled if x.get("label") in (0, 1)]
    scored = []
    for x in labelled:
        a, b = by.get(x["a"]["id"]), by.get(x["b"]["id"])
        if a is None or b is None:
            continue
        lk = score_pair(a, b, asm.corpus, cfg.linking)
        anchor = 0.0 if lk is None else max(0.0, lk.parts.get("place", 0.0)) + lk.parts.get(
            "terms", 0.0) + lk.parts.get("sensor", 0.0)
        scored.append((x["label"], -99.0 if lk is None else lk.score, anchor,
                       float(x.get("weight") or 1.0)))
    pos = sum(w for lab, _, _, w in scored if lab == 1)
    npos = sum(1 for lab, *_ in scored if lab == 1)
    print(f"{len(scored)} labelled pairs ({npos} same-thread, {len(scored) - npos} not); "
          f"weighted to {sum(w for *_, w in scored):.0f} candidate pairs, "
          f"{pos:.0f} same-thread")
    print("threshold  precision  recall  f1     predicted | sample: precision  recall")
    for th in [x / 4 for x in range(10, 29)]:
        pred = [(lab, w) for lab, s, anc, w in scored
                if s >= th and anc >= cfg.linking.anchor_min]
        tp = sum(w for lab, w in pred if lab == 1)
        p = tp / sum(w for _, w in pred) if pred else 0.0
        r = tp / pos if pos else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        tpu = sum(1 for lab, _ in pred if lab == 1)
        pu = tpu / len(pred) if pred else 0.0
        ru = tpu / npos if npos else 0.0
        mark = " <- config" if th == cfg.linking.link_threshold else ""
        print(f"{th:9.2f}  {p:9.3f}  {r:6.3f}  {f1:.3f}  {len(pred):5d}     | "
              f"{pu:9.3f}  {ru:6.3f}{mark}")
    # what the job outputs: both events in the same storyline after assembly
    threads = {n: s.key for s in asm.threads() for n in s.members}
    same = [(x["label"], float(x.get("weight") or 1.0),
             threads.get(x["a"]["id"]) is not None
             and threads.get(x["a"]["id"]) == threads.get(x["b"]["id"])) for x in labelled]
    for name, wt in (("weighted", True), ("labelled sample", False)):
        tp = sum(v if wt else 1.0 for lab, v, s in same if lab == 1 and s)
        pp = sum(v if wt else 1.0 for lab, v, s in same if s)
        ps = sum(v if wt else 1.0 for lab, v, s in same if lab == 1)
        print(f"same storyline after assembly ({name}): precision "
              f"{tp / pp if pp else 0:.3f} recall {tp / ps if ps else 0:.3f}")
    return 0


def cmd_run(args) -> int:
    cfg, nodes, load_s = _setup(args)
    t0 = time.perf_counter()
    asm = assemble.build(nodes, cfg)
    build_s = time.perf_counter() - t0
    threads = sorted(asm.threads(), key=lambda s: (-len(s.members), s.key))
    print(json.dumps({"nodes": len(nodes), "threads": len(threads),
                      "members": sum(len(s.members) for s in threads),
                      "load_s": round(load_s, 3), "assemble_s": round(build_s, 3),
                      **asm.stats}))
    for s in threads[: args.limit]:
        r = assemble.roles(s)
        print(f"\n### {assemble.theatre_of(s)[1]} | {assemble.representative(s).title[:100]}")
        for n in sorted(s.members.values(), key=lambda n: (n.t, n.id)):
            print(f"  {n.t:%m-%d %H:%M} {n.t_basis:15} {r[n.id][0]:11} {n.category:17} "
                  f"{(n.place_name or '-')[:20]:20} | {n.title[:90]}")
        for lk in sorted(s.links.values(), key=lambda x: -x.score)[:args.links]:
            print(f"      {lk.score:5.2f}  {lk.text[:160]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osint.storylines.calibrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("pairs", "eval", "run"))
    ap.add_argument("--data", required=True, help="a data directory (use a copy)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--now", default=None)
    ap.add_argument("--out", default="pairs.jsonl")
    ap.add_argument("--labels", default="pairs.jsonl")
    ap.add_argument("--per-bin", type=int, default=35, help="pairs sampled per score bin")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--links", type=int, default=4)
    args = ap.parse_args(argv)
    try:  # titles in any script on a Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    return {"pairs": cmd_pairs, "eval": cmd_eval, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
