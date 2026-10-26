"""tools/baseline/summarize.py: results validation, ranges, kill-criteria verdicts, PENDING."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

from tests.baseline import BASELINE_DIR, load_tool
from tests.helpers import BaseTestCase
from kitlib import evidence
from . import REPO_ROOT

SUMMARIZE = os.path.join(BASELINE_DIR, "summarize.py")
RESULTS = os.path.join(REPO_ROOT, "docs", "baseline", "results")
S = load_tool("summarize")


def load_json(*parts):
    with open(os.path.join(*parts), "r", encoding="utf-8") as fh:
        return json.load(fh)


def base_run(run_id="run-1", status="completed", cf=0, other=0, env_failures=0, item_failures=0,
             wall=60.0, spend=5.0, dispatches=None):
    with open(os.path.join(RESULTS, "TEMPLATE.json")) as fh:
        rec = json.load(fh)
    rec.pop("_comment")
    rec.update(run_id=run_id, status=status, wall_time_min=wall, spend_usd=spend, tool_calls=100,
               dispatches=dispatches if dispatches is not None else {"backend-engineer": 2})
    rec["kit"].update(claude_version="x", model="m")
    rec["items"] = [{"id": i, "status": "done" if status == "completed" else "blocked", "retries": 0,
                     "block_reason": None} for i in ("F1", "F2", "F3")]
    rec["rescues"] = ([{"n": n, "class": "control_flow", "type": "resume", "description": "d"} for n in range(cf)]
                      + [{"n": 100 + n, "class": "other", "type": "permission", "description": "d"}
                         for n in range(other)])
    rec["failures"] = ([{"class": "env", "description": "d"}] * env_failures
                       + [{"class": "item", "description": "d"}] * item_failures)
    rec["defects_escaped"] = {"checked": True, "failed_ac_ids": []}
    if status == "v1_cannot_finish":
        rec["cannot_finish_reason"] = "loop deadlocked"
    return rec


def replay(agent, calls):
    return {"schema": "baseline-result.v1", "kind": "replay", "run_id": "replay-" + agent, "date": "2026-01-02",
            "agent_type": agent, "source_run": "run-1",
            "replayed_dispatches": [{"tool_calls": c, "outcome": "APPROVED"} for c in calls]}


class SummarizeCase(BaseTestCase):
    def results_dir(self, *records):
        d = self.tmpdir("results-")
        for name in ("schema.json", "TEMPLATE.json"):
            with open(os.path.join(RESULTS, name)) as src, open(os.path.join(d, name), "w") as dst:
                dst.write(src.read())
        for rec in records:
            with open(os.path.join(d, rec["run_id"] + ".json"), "w") as fh:
                json.dump(rec, fh)
        return d

    def run_cli(self, d, *args):
        proc = subprocess.run([sys.executable, SUMMARIZE, "--results", d] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return proc

    def evaluate(self, *records):
        d = self.results_dir(*records)
        proc = self.run_cli(d, "--json")
        return proc.returncode, json.loads(proc.stdout)


class Pending(SummarizeCase):
    def test_shipped_results_dir_prints_pending_and_exits_zero(self):
        proc = subprocess.run([sys.executable, SUMMARIZE], stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.startswith("PENDING (n=0/3)"), proc.stdout)

    def test_template_and_schema_are_not_counted_and_template_is_valid(self):
        d = self.results_dir()
        proc = self.run_cli(d)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("PENDING (n=0/3)", proc.stdout)
        with open(os.path.join(RESULTS, "TEMPLATE.json")) as fh:
            tpl = json.load(fh)
        with open(os.path.join(RESULTS, "schema.json")) as fh:
            schema = json.load(fh)
        self.assertEqual(S.check_schema(tpl, schema) + S.semantic_errors(tpl), [])

    def test_fewer_than_three_runs_is_pending_with_k(self):
        rc, ev = self.evaluate(base_run("run-1", cf=9), base_run("run-2", cf=9))
        self.assertEqual(rc, 0)
        self.assertEqual(ev["overall"], "PENDING (n=2/3)")
        self.assertTrue(ev["kill_criteria"]["loopctl"]["verdict"].startswith("PENDING (n=2/3)"))
        proc = self.run_cli(self.results_dir(base_run("run-1")))
        self.assertTrue(proc.stdout.startswith("PENDING (n=1/3)"))

    def test_aborted_runs_do_not_count(self):
        rc, ev = self.evaluate(base_run("run-1"), base_run("run-2"), base_run("run-3", status="aborted"))
        self.assertEqual(ev["overall"], "PENDING (n=2/3)")


class KillCriteria(SummarizeCase):
    def verdicts(self, *records):
        rc, ev = self.evaluate(*records)
        self.assertEqual(rc, 0)
        return ev, {k: ev["kill_criteria"][k]["verdict"] for k in ("loopctl", "env_dominant", "cannot_finish")}

    def three(self, **kw):
        return [base_run("run-%d" % i, **{k: (v[i - 1] if isinstance(v, list) else v) for k, v in kw.items()})
                for i in (1, 2, 3)]

    def test_more_than_three_rescues_per_run_justifies_loopctl(self):
        ev, v = self.verdicts(*self.three(cf=[5, 4, 6]))
        self.assertTrue(v["loopctl"].startswith("JUSTIFIED"))
        self.assertEqual(ev["overall"], "COMPLETE (n=3/3)")
        self.assertEqual(ev["metrics"]["control_flow_rescues_per_run"]["min"], 4)

    def test_three_or_fewer_does_not_and_boundary_is_not_justified(self):
        _, v = self.verdicts(*self.three(cf=[3, 3, 3]))
        self.assertTrue(v["loopctl"].startswith("NOT JUSTIFIED"))
        _, v = self.verdicts(*self.three(cf=[0, 1, 2]))
        self.assertTrue(v["loopctl"].startswith("NOT JUSTIFIED"))

    def test_median_is_used_and_straddle_is_flagged(self):
        ev, v = self.verdicts(*self.three(cf=[0, 4, 9]))
        self.assertTrue(v["loopctl"].startswith("JUSTIFIED"))
        self.assertTrue(ev["kill_criteria"]["loopctl"]["range_straddles_threshold"])
        ev, v = self.verdicts(*self.three(cf=[0, 1, 9]))
        self.assertTrue(v["loopctl"].startswith("NOT JUSTIFIED"))

    def test_other_rescues_do_not_count_toward_loopctl(self):
        _, v = self.verdicts(*self.three(cf=[1, 1, 1], other=[20, 20, 20]))
        self.assertTrue(v["loopctl"].startswith("NOT JUSTIFIED"))

    def test_env_dominant_needs_more_than_half(self):
        _, v = self.verdicts(*self.three(env_failures=[3, 2, 1], item_failures=[1, 1, 1]))
        self.assertTrue(v["env_dominant"].startswith("FIRED"), v)
        _, v = self.verdicts(*self.three(env_failures=[1, 1, 1], item_failures=[1, 1, 1]))
        self.assertTrue(v["env_dominant"].startswith("NOT FIRED"), v)
        _, v = self.verdicts(*self.three())
        self.assertIn("no failures", v["env_dominant"])

    def test_cannot_finish_fires_only_when_no_run_completed(self):
        _, v = self.verdicts(*[base_run("run-%d" % i, status="v1_cannot_finish") for i in (1, 2, 3)])
        self.assertTrue(v["cannot_finish"].startswith("FIRED"))
        _, v = self.verdicts(base_run("run-1"), base_run("run-2"), base_run("run-3", status="v1_cannot_finish"))
        self.assertTrue(v["cannot_finish"].startswith("PARTIAL"))
        _, v = self.verdicts(*self.three())
        self.assertTrue(v["cannot_finish"].startswith("NOT FIRED"))

    def test_cannot_finish_is_reported_before_three_runs(self):
        ev, v = self.verdicts(base_run("run-1", status="v1_cannot_finish"))
        self.assertTrue(v["cannot_finish"].startswith("FIRED"))
        self.assertEqual(ev["overall"], "PENDING (n=1/3)")
        self.assertTrue(v["loopctl"].startswith("PENDING"))
        self.assertEqual(ev["kill_criteria"]["cannot_finish"]["reasons"], ["loop deadlocked"])

    def test_one_run_plus_twenty_replays_per_agent_is_provisional(self):
        run = base_run("run-1", cf=5, dispatches={"backend-engineer": 4, "app-engineer": 2})
        short = [replay("backend-engineer", range(1, 21))]
        ev, v = self.verdicts(run, *short)
        self.assertEqual(ev["overall"], "PENDING (n=1/3)")
        self.assertEqual(ev["sample"]["agents_missing_replays"], ["app-engineer"])
        both = short + [replay("app-engineer", [10] * 25)]
        ev, v = self.verdicts(run, *both)
        self.assertEqual(ev["overall"], "PROVISIONAL (1 run + replays)")
        self.assertTrue(v["loopctl"].startswith("JUSTIFIED"))
        self.assertIn("provisional", ev["kill_criteria"]["loopctl"]["caveat"])
        self.assertEqual(ev["replays"]["backend-engineer"]["tool_calls_p95"], 19.0)
        self.assertEqual(ev["replays"]["backend-engineer"]["tool_calls_p50"], 10.0)
        self.assertTrue(ev["replays"]["app-engineer"]["enough_for_p95"])

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(S.percentile(list(range(1, 101)), 95), 95.0)
        self.assertEqual(S.percentile([7], 95), 7.0)
        self.assertEqual(S.median([1, 2, 3, 10]), 2.5)

    def test_ranges_are_reported_not_single_numbers(self):
        ev, _ = self.verdicts(*self.three(wall=[30.0, 90.0, 60.0], spend=[2.0, None, 6.0]))
        self.assertEqual(ev["metrics"]["wall_time_min"], {"min": 30.0, "max": 90.0, "median": 60.0, "n": 3})
        self.assertEqual(ev["metrics"]["spend_usd"]["n"], 2)
        text = self.run_cli(self.results_dir(*self.three(wall=[30.0, 90.0, 60.0]))).stdout
        self.assertIn("30..90 (median 60, n=3)", text)


class Validation(SummarizeCase):
    def test_invalid_file_is_reported_and_exits_one(self):
        bad = base_run("run-1")
        bad["status"] = "great"
        bad["rescues"] = [{"class": "weird", "type": "resume", "description": "x"}]
        d = self.results_dir(bad)
        proc = self.run_cli(d)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("run-1.json", proc.stdout)
        self.assertIn("INVALID RESULTS FILES", proc.stdout)
        self.assertIn("PENDING (n=0/3)", proc.stdout)

    def test_each_kind_of_error(self):
        schema = load_json(RESULTS, "schema.json")
        ok = base_run()
        self.assertEqual(S.check_schema(ok, schema) + S.semantic_errors(ok), [])
        cases = {
            "wrong const": lambda r: r.update(schema="v0"),
            "negative minutes": lambda r: r.update(wall_time_min=-1),
            "bad date": lambda r: r.update(date="yesterday"),
            "bad ac id": lambda r: r["defects_escaped"].update(failed_ac_ids=["F1-1"]),
            "bool as number": lambda r: r.update(wall_time_min=True),
            "missing status": lambda r: r.pop("status"),
            "cannot finish without reason": lambda r: r.update(status="v1_cannot_finish", cannot_finish_reason=None),
            "duplicate rescue n": lambda r: r.update(rescues=[
                {"n": 1, "class": "other", "type": "other", "description": "a"},
                {"n": 1, "class": "other", "type": "other", "description": "b"}]),
            "bad item id": lambda r: r["items"].append({"id": "F9", "status": "done"}),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                rec = copy.deepcopy(ok)
                mutate(rec)
                self.assertTrue(S.check_schema(rec, schema) + S.semantic_errors(rec), name)

    def test_unparsable_json_is_reported(self):
        d = self.results_dir()
        with open(os.path.join(d, "broken.json"), "w") as fh:
            fh.write("{nope")
        proc = self.run_cli(d)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("broken.json: not valid JSON", proc.stdout)


class FromLog(SummarizeCase):
    def test_facts_from_an_observation_log(self):
        d = self.tmpdir()
        log = os.path.join(d, "o.jsonl")
        rows = [
            ("2026-10-26T10:00:00.000Z", {"hook_event_name": "SessionStart"}),
            ("2026-10-26T10:00:05.000Z", {"hook_event_name": "UserPromptSubmit"}),
            ("2026-10-26T10:01:00.000Z", {"hook_event_name": "PreToolUse", "tool_name": "Bash"}),
            ("2026-10-26T10:02:00.000Z", {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                                          "tool_input": {"subagent_type": "backend-engineer"}}),
            ("2026-10-26T10:30:00.000Z", {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}),
            ("2026-10-26T11:00:00.000Z", {"hook_event_name": "SessionStart"}),
        ]
        with open(log, "w") as fh:
            for ts, ev in rows:
                fh.write(json.dumps({"observed_at": ts, "event": ev}) + "\n")
            fh.write("garbage\n")
        proc = self.run_cli(self.results_dir(), "--from-log", log)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        facts = json.loads(proc.stdout)
        self.assertEqual(facts["tool_calls_PreToolUse"], 2)
        self.assertEqual(facts["dispatches_by_subagent_type"], {"backend-engineer": 1})
        self.assertEqual(facts["session_starts"], 2)
        self.assertEqual(facts["permission_requests"], 1)
        self.assertEqual(facts["elapsed_min_first_to_last_event"], 60.0)
        self.assertEqual(facts["unparsable_lines"], 1)

    def test_missing_log_is_a_clean_error(self):
        proc = self.run_cli(self.results_dir(), "--from-log", "/nonexistent/o.jsonl")
        self.assertEqual(proc.returncode, 1)


class EvidenceTemplate(BaseTestCase):
    """docs/baseline/evidence.template.json must become a passing evidence.v1 file once filled in."""

    def fill(self):
        with open(os.path.join(REPO_ROOT, "docs", "baseline", "evidence.template.json")) as fh:
            data = json.load(fh)
        for i, run in enumerate(data["fixture_runs"]):
            run.update(id="e%d" % (i + 1), clean=True, date="2027-01-0%d" % (i + 1), kit_version="2.0.0",
                       level="L2", runner="driver", control_flow_rescues=0,
                       results_file="docs/baseline/results/e%d.json" % (i + 1), notes="clean")
        data["real_feature"].update(id="feature-1", completed=True, level="L2", description="d",
                                    pr_url="https://example.invalid/pr/1")
        data.update(kill_criteria_checked=True, attested_by="A Maintainer", attested_at="2027-02-01")
        return data

    def write(self, data):
        repo = self.tmpdir()
        os.makedirs(os.path.join(repo, "docs", "baseline"))
        with open(os.path.join(repo, evidence.EVIDENCE_PATH), "w") as fh:
            json.dump(data, fh)
        return repo

    def test_unfilled_template_is_rejected_for_placeholders(self):
        with open(os.path.join(REPO_ROOT, "docs", "baseline", "evidence.template.json")) as fh:
            repo = self.write(json.load(fh))
        status = evidence.check(repo)
        self.assertFalse(status)
        self.assertIn("placeholders", status.reason)

    def test_filled_template_passes(self):
        status = evidence.check(self.write(self.fill()))
        self.assertTrue(status, status.reason)

    def test_filled_template_with_a_run_not_clean_fails(self):
        data = self.fill()
        data["fixture_runs"][2]["clean"] = False
        self.assertIn("need 3 clean", evidence.check(self.write(data)).reason)

    def test_template_and_schema_agree(self):
        schema = load_json(REPO_ROOT, "docs", "baseline", "evidence.schema.json")
        data = self.fill()
        self.assertEqual(S.check_schema(data, dict(schema, properties=dict(
            schema["properties"], fixture_runs={"type": "array"}))), [])
        for run in data["fixture_runs"]:
            self.assertEqual(S.check_schema(run, schema["properties"]["fixture_runs"]["items"]), [])
        self.assertGreaterEqual(len(data["fixture_runs"]), schema["properties"]["fixture_runs"]["minItems"])
        for key in schema["required"]:
            self.assertIn(key, data)
