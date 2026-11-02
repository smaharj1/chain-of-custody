"""The hello-loop fixture is satisfiable and its criteria are falsifiable (IMP-2, DEC-27).

The reference solution is applied one item at a time to a temporary copy of the fixture
(exactly the files a baseline run starts from, minus the overlay). For each item the
fixture's own verify commands must pass once the item is applied and its held-out acceptance
criteria must ALL fail one item earlier. A broken fixture therefore fails CI.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from typing import Dict, List

from . import REPO_ROOT, SCRIPTS_DIR
from tests.baseline import FIXTURE, load_module, load_tool
from tests.helpers import BaseTestCase, clean_env
from kitlib import config, gitutil

PREPARE = load_tool("prepare")
CHECKS = load_module(os.path.join(FIXTURE, "acceptance", "run_checks.py"), "hello_loop_run_checks")
ITEMS = ["F1", "F2", "F3"]
TAG = "v1.2.0"


def read_json(*parts):
    with open(os.path.join(FIXTURE, *parts), "r", encoding="utf-8") as fh:
        return json.load(fh)


def tag_exists() -> bool:
    return gitutil.rev_parse("refs/tags/%s^{commit}" % TAG, REPO_ROOT) is not None


class FixtureCase(BaseTestCase):
    """Builds temp copies of the fixture at a given stage and runs shell commands in them."""

    def setUp(self):
        super().setUp()
        self.shim = self.tmpdir("hello-shim-")
        os.symlink(sys.executable, os.path.join(self.shim, "python3"))
        env = clean_env(home=self.tmpdir("hello-home-"))
        env["PATH"] = self.shim + os.pathsep + env.get("PATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("DATABASE_URL", None)
        self.env = env

    def project(self, upto: str = "") -> str:
        """The run's starting tree, then reference-solution/F1.. up to and including `upto`."""
        dest = os.path.join(self.tmpdir("hello-proj-"), "p")
        for rel in PREPARE.fixture_files(FIXTURE):
            dst = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(FIXTURE, rel), dst)
        for item in ITEMS[: ITEMS.index(upto) + 1] if upto else []:
            stage = os.path.join(FIXTURE, "reference-solution", item)
            for dirpath, _dirs, files in os.walk(stage):
                for fn in files:
                    src = os.path.join(dirpath, fn)
                    dst = os.path.join(dest, os.path.relpath(src, stage))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
        return dest

    def sh(self, command: str, cwd: str, timeout: int = 180):
        return subprocess.run(command, shell=True, cwd=cwd, env=self.env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

    def acceptance(self, project: str, item: str) -> Dict[str, str]:
        """Run the held-out checks (from the fixture, since runs do not contain them)."""
        cmd = "python3 %s --project . --item %s --json" % (
            os.path.join(FIXTURE, "acceptance", "run_checks.py"), item)
        proc = self.sh(cmd, project)
        try:
            return json.loads(proc.stdout)["results"][item]
        except (ValueError, KeyError):
            self.fail("acceptance output unreadable:\n" + proc.stdout[-800:])


class Plan(unittest.TestCase):
    def setUp(self):
        self.plan = read_json("build-plan.json")
        self.verify = read_json("verify.json")

    def test_v1_shape(self):
        keys = ["id", "milestone", "title", "slug", "dependsOn", "intent", "acceptance", "requiresEnv", "status",
                "blockReason", "branch", "mergeCommit", "verifyEvidence", "downMigrationRef", "reworkNotes",
                "securityWaiver"]
        self.assertEqual(sorted(self.plan), ["createdAt", "goal", "items"])
        for it in self.plan["items"]:
            self.assertEqual(sorted(it), sorted(keys), it["id"])
            self.assertEqual(it["status"], "pending")
            for k in ("blockReason", "branch", "mergeCommit", "verifyEvidence", "downMigrationRef",
                      "reworkNotes", "securityWaiver"):
                self.assertIsNone(it[k], (it["id"], k))

    def test_dag_is_valid_acyclic_and_real(self):
        ids = [it["id"] for it in self.plan["items"]]
        self.assertEqual(ids, ITEMS)
        deps = {it["id"]: it["dependsOn"] for it in self.plan["items"]}
        for item, ds in deps.items():
            for d in ds:
                self.assertIn(d, ids)
        order, seen = [], set()
        while len(order) < len(ids):  # topological sort succeeds iff acyclic
            ready = [i for i in ids if i not in seen and all(d in seen for d in deps[i])]
            self.assertTrue(ready, "cycle in dependsOn")
            for i in ready:
                seen.add(i)
                order.append(i)
        self.assertEqual(deps, {"F1": [], "F2": ["F1"], "F3": ["F2"]})

    def test_acceptance_carries_ids_and_a_runtime_assertion(self):
        seen = set()
        for it in self.plan["items"]:
            ids = []
            for line in it["acceptance"]:
                m = re.match(r"^(AC-%s-\d+): " % it["id"], line)
                self.assertIsNotNone(m, line)
                ids.append(m.group(1))
            self.assertEqual(len(set(ids)), len(ids))
            self.assertFalse(seen & set(ids))
            seen |= set(ids)
            self.assertTrue(any("`" in a and ("returns" in a or "python3" in a) for a in it["acceptance"]),
                            "%s has no falsifiable runtime assertion" % it["id"])
            self.assertEqual(self.verify["items"][it["id"]]["ac_ids"], ids)
        self.assertEqual(seen, set(CHECKS.CHECKS), "every AC id needs exactly one held-out check")

    def test_milestones_give_two_checkpoints(self):
        self.assertEqual([it["milestone"] for it in self.plan["items"]],
                         ["M1 Data and API", "M1 Data and API", "M2 Interface"])

    def test_pins(self):
        with open(os.path.join(FIXTURE, ".python-version")) as fh:
            self.assertRegex(fh.read().strip(), r"^3\.\d+$")
        with open(os.path.join(FIXTURE, "pyproject.toml")) as fh:
            text = fh.read()
        self.assertRegex(text, r'requires-python\s*=\s*">=3\.9"')
        self.assertIn("dependencies = []", text)


class Python39(unittest.TestCase):
    def test_fixture_sources_parse_as_python_39(self):
        n = 0
        for dirpath, dirs, files in os.walk(FIXTURE):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in files:
                if fn.endswith(".py"):
                    path = os.path.join(dirpath, fn)
                    with open(path, "r", encoding="utf-8") as fh:
                        src = fh.read()
                    ast.parse(src, path, feature_version=(3, 9))
                    if fn != "__init__.py":
                        self.assertIn("from __future__ import annotations", src, path)
                    n += 1
        self.assertGreater(n, 20)


class Solution(FixtureCase):
    def test_starting_tree_has_a_passing_smoke_suite(self):
        proj = self.project()
        proc = self.sh("python3 -m unittest discover -s tests -t .", proj)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertRegex(proc.stdout, r"Ran [1-9]\d* tests?")

    def test_each_item_passes_its_verify_commands_when_applied(self):
        verify = read_json("verify.json")
        for item in ITEMS:
            with self.subTest(item=item):
                proj = self.project(item)
                spec = verify["items"][item]
                tests = self.sh(spec["tests"], proj)
                self.assertEqual(tests.returncode, 0, tests.stdout)
                self.assertRegex(tests.stdout, r"Ran [1-9]\d* tests?")
                results = self.acceptance(proj, item)
                self.assertEqual(sorted(results), sorted(spec["ac_ids"]))
                self.assertEqual({k: v for k, v in results.items() if v != "pass"}, {})

    def test_full_verify_tests_and_verify_run_at_the_end(self):
        verify = read_json("verify.json")
        proj = self.project("F3")
        proc = self.sh(verify["verify_tests"], proj)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for item in ITEMS:
            route = verify["items"][item]["demo_route"] or "/healthz"
            with self.subTest(route=route):
                run = self.sh("%s %s" % (verify["verify_run"], route), proj)
                self.assertEqual(run.returncode, 0, run.stdout)

    def test_verify_run_fails_on_the_wrong_status(self):
        proj = self.project("F3")
        self.assertNotEqual(self.sh("python3 scripts/verify_run.py /nope", proj).returncode, 0)
        self.assertEqual(self.sh("python3 scripts/verify_run.py /nope --status 404", proj).returncode, 0)

    def test_every_criterion_fails_one_item_earlier(self):
        before = {"F1": "", "F2": "F1", "F3": "F2"}
        for item in ITEMS:
            with self.subTest(item=item):
                results = self.acceptance(self.project(before[item]), item)
                passed = [k for k, v in results.items() if v == "pass"]
                self.assertEqual(passed, [], "criteria that pass without the item's work: %s" % passed)

    def test_down_migration_round_trip_on_the_final_tree(self):
        proj = self.project("F3")
        proc = self.sh("python3 -m hello_loop.migrate && python3 -m hello_loop.seed && "
                       "python3 -m hello_loop.migrate --down && python3 -m hello_loop.migrate && "
                       "python3 -m hello_loop.migrate --down", proj)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("reverted 0001_create_bookmarks", proc.stdout)


class Overlay(unittest.TestCase):
    OVERLAY = os.path.join(FIXTURE, "kit-overlay")

    def test_loop_config_is_a_valid_filled_v1_config(self):
        cfg = config.load_file(os.path.join(self.OVERLAY, ".claude", "loop.config.md"))
        self.assertEqual([i for i in cfg.issues], [])
        for key in ("verify_tests", "verify_run", "db_ephemeral"):
            self.assertFalse(cfg.is_placeholder(key), key)
            self.assertNotIn("<command>", cfg.get_str(key))
        self.assertEqual(cfg.get_str("autonomy"), "per-milestone")
        self.assertEqual(cfg.get_str("db_local"), "sqlite")
        self.assertEqual(cfg.evidence_gated_violations(), [])
        verify = read_json("verify.json")
        self.assertEqual(cfg.get_str("verify_tests"), verify["verify_tests"])
        self.assertEqual(cfg.get_str("verify_run"), verify["verify_run"])
        for rel in ("scripts/ephemeral_db.py", "scripts/verify_run.py"):
            self.assertTrue(os.path.isfile(os.path.join(FIXTURE, rel)), rel)

    def test_overlay_differs_from_the_v1_template_only_in_the_three_commands(self):
        if not tag_exists():
            self.skipTest("tag %s not present" % TAG)
        base = gitutil.show_file(TAG, ".claude/loop.config.md", REPO_ROOT).splitlines()
        with open(os.path.join(self.OVERLAY, ".claude", "loop.config.md"), encoding="utf-8") as fh:
            mine = fh.read().splitlines()
        self.assertEqual(len(base), len(mine))
        keys = ("db_ephemeral", "verify_run", "verify_tests")
        changed = []
        for i, (a, b) in enumerate(zip(base, mine)):
            if a != b:
                # a changed line is one of the three keys, or the comment line under verify_run
                owner = a if not a.startswith(" ") else base[i - 1]
                changed.append(owner.split(":")[0])
        self.assertEqual(sorted(set(changed)), sorted(keys))

    def test_no_template_sentinels_in_the_overlaid_markdown(self):
        for rel in (".claude/CLAUDE.md",) + tuple(".claude/context/" + f for f in sorted(
                os.listdir(os.path.join(self.OVERLAY, ".claude", "context")))):
            with open(os.path.join(self.OVERLAY, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("<!--", text, rel)
            self.assertNotIn("TODO(primer)", text, rel)
            self.assertNotIn("<command>", text, rel)

    def test_every_v1_primer_is_overlaid_removed_or_protocol(self):
        if not tag_exists():
            self.skipTest("tag %s not present" % TAG)
        listing = gitutil.run_git(["ls-tree", "-r", "--name-only", TAG, ".claude/context/"], cwd=REPO_ROOT).stdout
        v1 = [p for p in listing.split("\n") if p]
        protocol = {".claude/context/engineer-protocol.md", ".claude/context/primer-protocol.md"}
        overlaid = {".claude/context/" + f for f in os.listdir(os.path.join(self.OVERLAY, ".claude", "context"))}
        removed = set(PREPARE.removals(FIXTURE))
        self.assertEqual(sorted(p for p in v1 if p not in protocol and p not in overlaid and p not in removed), [])
        for path in removed:
            self.assertIn(path, set(v1) | set(
                gitutil.run_git(["ls-tree", "-r", "--name-only", TAG, ".claude/agents/"], cwd=REPO_ROOT)
                .stdout.split("\n")), path)

    def test_claude_md_roster_matches_the_remaining_agents(self):
        with open(os.path.join(self.OVERLAY, ".claude", "CLAUDE.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Engineer subagents: database, backend, app\n", text)
        self.assertNotIn("admin-app", text)
        self.assertNotIn("infra", text.split("## Tech Stack")[0] + text.split("## Agents and Modes")[1])

    def test_nothing_in_the_run_set_leaks_the_answer_key(self):
        files = PREPARE.fixture_files(FIXTURE)
        for rel in files:
            top = rel.split(os.sep)[0]
            self.assertNotIn(top, ("reference-solution", "acceptance", "kit-overlay"), rel)
        for rel in ("verify.json", "README.md"):
            self.assertNotIn(rel, files)
        self.assertIn("build-plan.json", files)
        self.assertIn("hello_loop/migrate.py", files)


if __name__ == "__main__":
    unittest.main()
