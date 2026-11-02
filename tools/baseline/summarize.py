#!/usr/bin/env python3
"""Aggregate baseline results into ranges and evaluate the kill criteria (IMP-2, report §16.2).

    python3 tools/baseline/summarize.py [--results DIR] [--json]
    python3 tools/baseline/summarize.py --from-log observe.jsonl     # facts from one run's hook log

Reads docs/baseline/results/*.json (not TEMPLATE.json, not schema.json), validates each file
against results/schema.json, and prints:

  * ranges (min..max, median) over the runs for human rescues (control-flow and other),
    failures by class, wall time, spend, tool calls and escaped defects
  * the kill criteria that a baseline can decide (loopctl justified, env-dominant,
    V1 cannot finish) with the rule used; the other criteria are listed as "applies later"
  * for `replay` records, per-agent-type dispatch counts and tool-call percentiles

With fewer than 3 `run` results (and no complete "1 run + 20 replayed dispatches per agent
type" set) it prints `PENDING (n=k/3)` and exits 0. A "V1 cannot finish" recorded in any run
is reported at once, whatever n is. Exit 1 only when a results file is invalid; 2 for usage.

Rules (decisions in changelog.d/0a-baseline.md):
  * "more than 3 control-flow rescues per run, taken across the runs" is evaluated on the
    MEDIAN per run, with min..max shown; if the range straddles 3 the verdict says so
  * "most failures are env" means env failures are more than half of all recorded failures
  * "V1 cannot finish" fires when no run completed; a mixed result is reported as partial
  * aborted runs are listed but are not evidence either way
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_RESULTS = os.path.join(KIT_ROOT, "docs", "baseline", "results")
MIN_RUNS = 3
REPLAYS_PER_AGENT = 20
RESCUE_THRESHOLD = 3
SKIP_FILES = ("TEMPLATE.json", "schema.json")


# --- a small JSON-schema subset checker (stdlib only) ---------------------------------------

_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def check_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Supports: const, enum, type (name or list), required, properties, items, minimum, pattern."""
    import re
    errs = []  # type: List[str]
    if "const" in schema and value != schema["const"]:
        errs.append("%s: must be %r" % (path, schema["const"]))
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r is not one of %s" % (path, value, schema["enum"]))
        return errs
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, n) for n in names):
            errs.append("%s: expected %s, got %s" % (path, "/".join(names), type(value).__name__))
            return errs
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("%s: %r is below the minimum %r" % (path, value, schema["minimum"]))
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errs.append("%s: %r does not match %s" % (path, value, schema["pattern"]))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errs.append("%s: missing required property %r" % (path, key))
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errs += check_schema(value[key], sub, "%s.%s" % (path, key))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += check_schema(item, schema["items"], "%s[%d]" % (path, i))
    return errs


def semantic_errors(rec: Dict[str, Any]) -> List[str]:
    """Rules the subset checker cannot express: which fields each kind needs."""
    errs = []  # type: List[str]
    if rec.get("kind") == "run":
        for key in ("status", "kit", "conditions", "wall_time_min", "items", "rescues", "failures",
                    "defects_escaped"):
            if key not in rec:
                errs.append("$: kind=run needs %r" % key)
        if rec.get("status") == "v1_cannot_finish" and not rec.get("cannot_finish_reason"):
            errs.append("$: status v1_cannot_finish needs cannot_finish_reason")
        n = [r.get("n") for r in rec.get("rescues", []) if isinstance(r, dict) and "n" in r]
        if len(set(n)) != len(n):
            errs.append("$.rescues: duplicate n")
    elif rec.get("kind") == "replay":
        for key in ("agent_type", "replayed_dispatches"):
            if key not in rec:
                errs.append("$: kind=replay needs %r" % key)
    return errs


# --- loading --------------------------------------------------------------------------------

def load_results(results_dir: str, schema: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    good = []  # type: List[Dict[str, Any]]
    problems = []  # type: List[str]
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        name = os.path.basename(path)
        if name in SKIP_FILES or name.startswith("_"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError) as exc:
            problems.append("%s: not valid JSON (%s)" % (name, exc))
            continue
        errs = check_schema(rec, schema) + (semantic_errors(rec) if isinstance(rec, dict) else [])
        if errs:
            problems += ["%s: %s" % (name, e) for e in errs]
            continue
        rec["_file"] = name
        good.append(rec)
    return good, problems


# --- statistics -----------------------------------------------------------------------------

def median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def percentile(xs: List[float], p: float) -> float:
    """Nearest-rank percentile (no interpolation): the value that p% of samples do not exceed."""
    s = sorted(xs)
    rank = max(1, int(-(-p * len(s) // 100)))  # ceil
    return float(s[min(rank, len(s)) - 1])


def rng(xs: List[float]) -> Optional[Dict[str, float]]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return OrderedDict([("min", min(xs)), ("max", max(xs)), ("median", median(xs)), ("n", len(xs))])


def fmt_range(r: Optional[Dict[str, float]]) -> str:
    if not r:
        return "no data"
    f = lambda x: ("%d" % x) if float(x).is_integer() else ("%.2f" % x)  # noqa: E731
    return "%s..%s (median %s, n=%d)" % (f(r["min"]), f(r["max"]), f(r["median"]), r["n"])


# --- evaluation -----------------------------------------------------------------------------

def replay_summary(replays: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_agent = {}  # type: Dict[str, List[int]]
    for r in replays:
        by_agent.setdefault(r["agent_type"], []).extend(d["tool_calls"] for d in r["replayed_dispatches"])
    out = OrderedDict()  # type: Dict[str, Dict[str, Any]]
    for agent in sorted(by_agent):
        calls = by_agent[agent]
        out[agent] = OrderedDict([
            ("dispatches", len(calls)),
            ("tool_calls_p50", percentile(calls, 50)),
            ("tool_calls_p95", percentile(calls, 95)),
            ("tool_calls_max", float(max(calls))),
            ("enough_for_p95", len(calls) >= REPLAYS_PER_AGENT)])
    return out


def sample_state(runs: List[Dict[str, Any]], replays: List[Dict[str, Any]]) -> Dict[str, Any]:
    evidence = [r for r in runs if r["status"] != "aborted"]
    summary = replay_summary(replays)
    agents_seen = sorted({a for r in evidence for a in r.get("dispatches", {})})
    short = [a for a in agents_seen if summary.get(a, {}).get("dispatches", 0) < REPLAYS_PER_AGENT]
    full = len(evidence) >= MIN_RUNS
    alt = len(evidence) >= 1 and bool(agents_seen) and not short
    return OrderedDict([("runs", len(evidence)), ("aborted", len(runs) - len(evidence)),
                        ("full", full), ("alternative", alt and not full),
                        ("agents_missing_replays", short if not full else [])])


def evaluate(runs: List[Dict[str, Any]], replays: List[Dict[str, Any]]) -> Dict[str, Any]:
    evidence = [r for r in runs if r["status"] != "aborted"]
    state = sample_state(runs, replays)
    out = OrderedDict()  # type: Dict[str, Any]
    out["sample"] = state
    ready = state["full"] or state["alternative"]
    label = "n=%d/%d" % (len(evidence), MIN_RUNS)

    # Metric ranges (over every non-aborted run).
    cf = [sum(1 for x in r["rescues"] if x["class"] == "control_flow") for r in evidence]
    other = [sum(1 for x in r["rescues"] if x["class"] == "other") for r in evidence]
    classes = Counter(f["class"] for r in evidence for f in r["failures"])
    metrics = OrderedDict()  # type: Dict[str, Any]
    metrics["control_flow_rescues_per_run"] = rng(cf)
    metrics["other_rescues_per_run"] = rng(other)
    metrics["failures_per_run"] = rng([len(r["failures"]) for r in evidence])
    metrics["failures_by_class_total"] = dict(sorted(classes.items()))
    metrics["wall_time_min"] = rng([r["wall_time_min"] for r in evidence])
    metrics["spend_usd"] = rng([r.get("spend_usd") for r in evidence])
    metrics["tool_calls"] = rng([r.get("tool_calls") for r in evidence])
    metrics["permission_prompts"] = rng([r.get("permission_prompts") for r in evidence])
    metrics["items_done"] = rng([sum(1 for i in r["items"] if i["status"] == "done") for r in evidence])
    metrics["escaped_defects"] = rng([len(r["defects_escaped"]["failed_ac_ids"])
                                      for r in evidence if r["defects_escaped"]["checked"]])
    out["metrics"] = metrics
    out["replays"] = replay_summary(replays)

    crit = OrderedDict()  # type: Dict[str, Any]
    # 1. loopctl justified?
    c1 = OrderedDict([("question", "Is loopctl justified? (more than %d control-flow rescues per run)" % RESCUE_THRESHOLD),
                      ("rule", "median control-flow rescues per run > %d" % RESCUE_THRESHOLD)])
    if not evidence or not ready:
        c1["verdict"] = "PENDING (%s)" % label
    else:
        med = median(cf)
        straddle = min(cf) <= RESCUE_THRESHOLD < max(cf)
        if med > RESCUE_THRESHOLD:
            c1["verdict"] = "JUSTIFIED: build the Phase 4 loopctl MVP in full"
        else:
            c1["verdict"] = "NOT JUSTIFIED: shrink the Phase 4 MVP to validate, status and doctor"
        c1["median"] = med
        c1["range_straddles_threshold"] = straddle
        if state["alternative"]:
            c1["caveat"] = "sample is 1 run + replays, not 3 runs; treat as provisional"
    crit["loopctl"] = c1
    # 2. env-dominant?
    total = sum(classes.values())
    c2 = OrderedDict([("question", "Are most failures `env`?"), ("rule", "env failures > half of all recorded failures")])
    if not evidence or not ready:
        c2["verdict"] = "PENDING (%s)" % label
    elif total == 0:
        c2["verdict"] = "NOT FIRED: no failures recorded"
    elif classes.get("env", 0) * 2 > total:
        c2["verdict"] = "FIRED: doctor and G0 come first, ahead of Phases 2-4 (%d of %d failures are env)" % (classes["env"], total)
    else:
        c2["verdict"] = "NOT FIRED (%d of %d failures are env)" % (classes.get("env", 0), total)
    crit["env_dominant"] = c2
    # 3. cannot finish?
    done = [r for r in evidence if r["status"] == "completed"]
    cant = [r for r in evidence if r["status"] == "v1_cannot_finish"]
    c3 = OrderedDict([("question", "Can V1 finish the fixture at all?"), ("rule", "fires when no run completed")])
    if cant and not done:
        c3["verdict"] = ("FIRED: V1 cannot finish (%d of %d runs; reasons recorded). Phase 2 guards and the doctor come "
                         "first and the loopctl decision is deferred until a run finishes" % (len(cant), len(evidence)))
        c3["reasons"] = [r["cannot_finish_reason"] for r in cant]
    elif cant:
        c3["verdict"] = "PARTIAL: %d of %d runs did not finish; V1 can finish, not reliably" % (len(cant), len(evidence))
        c3["reasons"] = [r["cannot_finish_reason"] for r in cant]
    elif not evidence or not ready:
        c3["verdict"] = "PENDING (%s)" % label
    else:
        c3["verdict"] = "NOT FIRED: %d of %d runs completed" % (len(done), len(evidence))
    crit["cannot_finish"] = c3
    crit["applies_later"] = [
        "Every capability phase must improve a fixture metric (rescues, failures by class, defect escape, spend, wall "
        "time) or it is reverted; safety and boundary work is judged on the corpora and fixtures instead",
        "Gates whose owner metric never moves are removed at the next model release",
        "A safety eval or corpus regression blocks the release, with no waiver"]
    out["kill_criteria"] = crit
    out["overall"] = ("PENDING (n=%d/%d)" % (len(evidence), MIN_RUNS)) if not ready else (
        "COMPLETE (n=%d/%d)" % (len(evidence), MIN_RUNS) if state["full"] else "PROVISIONAL (1 run + replays)")
    return out


# --- observation log -> facts ---------------------------------------------------------------

def log_facts(path: str) -> Dict[str, Any]:
    """Facts that can be read off the hook log. They are hints: the human reconciles them."""
    first = last = None  # type: Optional[str]
    tools = Counter()  # type: Counter
    dispatches = Counter()  # type: Counter
    events = Counter()  # type: Counter
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ev = rec["event"]
            except (ValueError, KeyError, TypeError):
                bad += 1
                continue
            ts = rec.get("observed_at")
            first = first or ts
            last = ts or last
            name = ev.get("hook_event_name") or ev.get("event") or "unknown"
            events[name] += 1
            if name == "PreToolUse":
                tool = ev.get("tool_name") or "unknown"
                tools[tool] += 1
                if tool in ("Agent", "Task"):
                    sub = (ev.get("tool_input") or {}).get("subagent_type") or "unknown"
                    dispatches[sub] += 1
    minutes = None
    if first and last:
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        minutes = round((datetime.strptime(last, fmt) - datetime.strptime(first, fmt)).total_seconds() / 60.0, 1)
    return OrderedDict([
        ("log", path), ("events", dict(sorted(events.items()))), ("unparsable_lines", bad),
        ("first_event", first), ("last_event", last), ("elapsed_min_first_to_last_event", minutes),
        ("tool_calls_PreToolUse", sum(tools.values())), ("tool_calls_by_tool", dict(sorted(tools.items()))),
        ("dispatches_by_subagent_type", dict(sorted(dispatches.items()))),
        ("permission_requests", events.get("PermissionRequest", 0)),
        ("session_starts", events.get("SessionStart", 0)),
        ("user_prompts", events.get("UserPromptSubmit", 0)),
        ("hints", [
            "SessionStart more than once: the session was restarted or resumed; each restart after the first is a "
            "candidate `resume` rescue",
            "UserPromptSubmit beyond the first prompt: each is a human message; classify it as a checkpoint "
            "approval or a rescue",
            "elapsed time includes waiting for the human; wall_time_min in the results is the human's figure"])])


# --- output ---------------------------------------------------------------------------------

def render(ev: Dict[str, Any], problems: List[str]) -> str:
    L = []  # type: List[str]
    L.append(ev["overall"])
    s = ev["sample"]
    L.append("runs counted: %d (aborted, not counted: %d); full sample needs %d runs, or 1 run + %d replayed "
             "dispatches per agent type" % (s["runs"], s["aborted"], MIN_RUNS, REPLAYS_PER_AGENT))
    if s["agents_missing_replays"] and s["runs"] and not s["full"]:
        L.append("  replays still missing for: %s" % ", ".join(s["agents_missing_replays"]))
    if ev["metrics"]["items_done"]:
        L.append("")
        L.append("Ranges over the runs:")
        for k, v in ev["metrics"].items():
            if k == "failures_by_class_total":
                L.append("  %-30s %s" % (k, v or "none"))
            else:
                L.append("  %-30s %s" % (k, fmt_range(v)))
    if ev["replays"]:
        L.append("")
        L.append("Replayed dispatches (tool calls per dispatch):")
        for agent, d in ev["replays"].items():
            L.append("  %-24s n=%d  p50=%g  p95=%g  max=%g%s" % (
                agent, d["dispatches"], d["tool_calls_p50"], d["tool_calls_p95"], d["tool_calls_max"],
                "" if d["enough_for_p95"] else "  (fewer than %d: no p95 yet)" % REPLAYS_PER_AGENT))
    L.append("")
    L.append("Kill criteria (report §16.2):")
    for key in ("loopctl", "env_dominant", "cannot_finish"):
        c = ev["kill_criteria"][key]
        L.append("  - %s" % c["question"])
        L.append("      rule   : %s" % c["rule"])
        L.append("      verdict: %s" % c["verdict"])
        if c.get("range_straddles_threshold"):
            L.append("      note   : the per-run range straddles %d; read the runs, not only the median" % RESCUE_THRESHOLD)
        if c.get("caveat"):
            L.append("      note   : %s" % c["caveat"])
    L.append("  Decided later, not by a baseline:")
    for t in ev["kill_criteria"]["applies_later"]:
        L.append("    * %s" % t)
    if problems:
        L.append("")
        L.append("INVALID RESULTS FILES (not counted):")
        L += ["  " + p for p in problems]
    return "\n".join(L)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Summarise baseline results and evaluate the kill criteria.")
    ap.add_argument("--results", default=DEFAULT_RESULTS, help="directory of results/*.json")
    ap.add_argument("--json", action="store_true", help="print the evaluation as JSON")
    ap.add_argument("--from-log", metavar="OBSERVE_JSONL", help="print facts from one run's observation log and exit")
    a = ap.parse_args(argv)
    if a.from_log:
        try:
            print(json.dumps(log_facts(a.from_log), indent=2))
        except OSError as exc:
            print("summarize: cannot read %s: %s" % (a.from_log, exc), file=sys.stderr)
            return 1
        return 0
    try:
        with open(os.path.join(a.results, "schema.json"), "r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, ValueError) as exc:
        print("summarize: cannot read %s/schema.json: %s" % (a.results, exc), file=sys.stderr)
        return 1
    records, problems = load_results(a.results, schema)
    runs = [r for r in records if r["kind"] == "run"]
    replays = [r for r in records if r["kind"] == "replay"]
    ev = evaluate(runs, replays)
    ev["invalid_files"] = problems
    if a.json:
        print(json.dumps(ev, indent=2))
    else:
        print(render(ev, problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
