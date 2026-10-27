#!/usr/bin/env python3
"""Ablate the harness: which parts still pay their way? (report 15 item 3 `--ablation`, 16.2, 16.1 row 10; docs/harness-review.md)

    python3 tools/evals/ablate.py                         every part of evals/ablation/manifest.json, text report
    python3 tools/evals/ablate.py --json --out ab.json    write the kit.ablation.v1 file `loopctl harness --ablation ab.json` reads
    python3 tools/evals/ablate.py --part hook:guard-git   one part

It is the arithmetic around `run_local.py`, not a runner: it never starts a model. For each part in the manifest it grades the cases the part
protects twice, with the SAME graders:

    with the part on    the recorded transcripts in evals/transcripts/                (run_local's input)
    with it switched off the transcripts recorded in evals/ablation/transcripts/<part>/  (live, k=3 per case, made by `claude plugin eval` or by hand
                                                                                           with the part disabled as the manifest says; pending-human)

and writes the pass rates and a verdict per part (`loopctl.learning.harness.ablation_verdict`: pending, insufficient, keep, harmful, no-effect, exempt).
A part with no transcripts is `pending`, which is what a fresh checkout shows: the committed transcripts are synthetic and there is nothing honest to compare
until a live run has been made. Exit codes: 0 done, 2 usage or internal error (a pending part is not a failure).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_local  # noqa: E402
from loopctl.learning import harness  # noqa: E402


def part_dir(root: str, part_id: str) -> str:
    return os.path.join(root, part_id.replace(":", "__").replace("/", "_"))


def rate_of(res: Dict[str, Any]) -> Dict[str, Any]:
    if res.get("status") == "pending" or "rate" not in res:
        return {"rate": None, "runs": 0}
    return {"rate": float(res["rate"]), "runs": int(res.get("runs") or 0)}


def ablate(manifest: Dict[str, Any], cases: List[Dict[str, Any]], transcripts: str, ablated_root: str, only: Optional[List[str]] = None, model: str = "") -> Dict[str, Any]:
    by_id = {c["id"]: c for c in cases}
    base = {cid: rate_of(run_local.run_case(c, transcripts)) for cid, c in by_id.items()}
    parts = []  # type: List[Dict[str, Any]]
    for p in manifest.get("parts", []):
        if only and p["id"] not in only:
            continue
        rows = []  # type: List[Dict[str, Any]]
        for cid in p.get("cases", []):
            c = by_id.get(cid)
            if c is None:
                raise ValueError("part %s names the case %s, which does not exist" % (p["id"], cid))
            ab = rate_of(run_local.run_case(c, part_dir(ablated_root, p["id"])))
            rows.append({"id": cid, "class": c["class"], "base_rate": base[cid]["rate"] if base[cid]["rate"] is not None else 0.0, "base_runs": base[cid]["runs"],
                         "ablated_rate": ab["rate"] if ab["rate"] is not None else 0.0, "ablated_runs": ab["runs"]})
        part = {"id": p["id"], "kind": p.get("kind", ""), "class": p.get("class", "quality"), "how_to_disable": p.get("how_to_disable", ""), "cases": rows}
        part.update(harness.ablation_verdict(part))
        parts.append(part)
    return {"schema": harness.ABLATION_SCHEMA, "model": model, "parts": parts}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=os.path.join(REPO, "evals", "ablation", "manifest.json"))
    ap.add_argument("--cases", default=os.path.join(REPO, "evals", "cases"))
    ap.add_argument("--transcripts", default=os.path.join(REPO, "evals", "transcripts"))
    ap.add_argument("--ablated", default=os.path.join(REPO, "evals", "ablation", "transcripts"))
    ap.add_argument("--part", action="append", default=[])
    ap.add_argument("--model", default="", help="the model release under review (recorded)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None, help="write the JSON here")
    try:
        a = ap.parse_args(argv)
        with open(a.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        doc = ablate(manifest, run_local.load_cases(a.cases), a.transcripts, a.ablated, a.part, a.model)
        if a.part and not doc["parts"]:
            raise ValueError("no such part")
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write("ablate: %s: %s\n" % (type(exc).__name__, exc))
        return 2
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        for p in doc["parts"]:
            print("%-34s %-12s %-9s %s" % (p["id"], p["verdict"], p["class"], p["reason"]))
        print("%d part(s); pending: %d. A recommendation needs `loopctl harness --ablation FILE` and a person (docs/harness-review.md)." % (
            len(doc["parts"]), sum(1 for p in doc["parts"] if p["verdict"] == "pending")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
