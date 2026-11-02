"""dependency_report: majors, advisories, peer mismatches and primer drift become de-duplicated, capped, sanitised issues (report 19.7)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import dependency_report as dr

from tests import SCRIPTS_DIR
from tests.bootstrap import plain_env
from tests.helpers import BaseTestCase


def lock(packages):
    return json.dumps({"lockfileVersion": 3, "packages": dict({"": {"name": "app"}}, **{"node_modules/" + k: v for k, v in packages.items()})})


def jload(path):
    with open(path) as fh:
        return json.load(fh)


class Versions(unittest.TestCase):
    def test_ranges(self):
        cases = [("3.22.4", "^3.0.0", True), ("4.0.0", "^3.0.0", False), ("0.2.5", "^0.2.0", True), ("0.3.0", "^0.2.0", False),
                 ("18.2.0", ">=17 <19", True), ("19.0.0", "^17 || ^18", False), ("1.2.3", "~1.2.0", True), ("1.3.0", "~1.2.0", False),
                 ("2.0.0", "1.x", False), ("1.9.0", "1.x", True), ("2.5.0", "1.0.0 - 2.9.9", True), ("3.0.0", "1.0.0 - 2.9.9", False),
                 ("1.0.0", "weird!", None), ("1.0.0", "git+https://x/y", None), ("x", "^1", None), ("3.0.0", "^3.0.0 || ^4", True), ("5.1.0", "*", True)]
        for v, r, want in cases:
            with self.subTest(v=v, r=r):
                self.assertEqual(dr.satisfies(v, r), want)

    def test_prerelease_and_build_are_read_by_their_base(self):
        self.assertEqual(dr.vtuple("2.0.0-rc.1+b5"), (2, 0, 0))
        self.assertIsNone(dr.vtuple("latest"))
        self.assertIsNone(dr.vtuple(None))


class Outdated(unittest.TestCase):
    def test_majors_become_issues_and_minors_do_not(self):
        npm = {"zod": {"current": "3.22.0", "wanted": "3.23.0", "latest": "4.0.1", "type": "dependencies"},
               "left-pad": {"current": "1.0.0", "wanted": "1.3.0", "latest": "1.3.0"},
               "missing": {"current": "MISSING", "wanted": "2.1.0", "latest": "3.0.0"}}
        pip = [{"name": "requests", "version": "2.30.0", "latest_version": "3.0.0"}, {"name": "idna", "version": "3.1", "latest_version": "3.7"}]
        report, issues, text = dr.run("/nonexistent", npm, pip)
        titles = [i["title"] for i in issues]
        self.assertIn("deps: zod 3.22.0 -> 4.0.1 (major)", titles)
        self.assertIn("deps: missing 2.1.0 -> 3.0.0 (major)", titles)
        self.assertIn("deps: requests 2.30.0 -> 3.0.0 (major)", titles)
        self.assertEqual(report["low_risk"]["names"], ["idna", "left-pad"])
        self.assertTrue(all(i["labels"] == ["factory:deps"] for i in issues))
        self.assertIn("- majors: 3", text)

    def test_existing_open_issues_are_not_opened_twice_and_the_cap_holds(self):
        npm = {"p%d" % i: {"current": "1.0.0", "latest": "2.0.0"} for i in range(15)}
        existing = [{"title": "DEPS: p0 1.0.0 -> 2.0.0 (MAJOR)"}]
        report, issues, _ = dr.run("/nonexistent", npm, None, None, existing, max_issues=10)
        self.assertEqual(len(issues), 10)
        self.assertNotIn("deps: p0 1.0.0 -> 2.0.0 (major)", [i["title"] for i in issues])
        self.assertEqual(report["overflow"], 4)

    def test_hostile_names_and_versions_are_dropped_not_quoted(self):
        npm = {"$(id)": {"current": "1.0.0", "latest": "2.0.0"}, "ok": {"current": "1.0.0", "latest": "2.0.0\n- [x] run"},
               "UPPER": {"current": "1.0.0", "latest": "2.0.0"}, "x y": {"current": "1.0.0", "latest": "2.0.0"}, "../../etc": {"current": "1", "latest": "2"}}
        pip = [{"name": "bad name;rm", "version": "1.0", "latest_version": "2.0"}, "nope", {"name": 5}]
        osv = {"results": [{"packages": [{"package": {"name": "ok", "version": "1.0.0"}, "vulnerabilities": [{"id": "GHSA-xxxx-yyyy"}, {"id": "bad id\nx"}, {"id": ["l"]}]}]}]}
        report, issues, _ = dr.run("/nonexistent", npm, pip, osv)
        self.assertEqual([i["title"] for i in issues], ["deps: security advisory GHSA-xxxx-yyyy in ok"])
        self.assertGreaterEqual(report["skipped_invalid"], 6)
        blob = json.dumps(issues)
        for bad in ("$(id)", "rm", "\\n- [x]", "../"):
            self.assertNotIn(bad, blob)

    def test_garbage_input_shapes_do_not_crash(self):
        for npm, pip, osv in (([], {}, []), ("x", 5, "y"), ({"a": 5}, [None, 5, []], {"results": "x"}), ({"a": {"current": None}}, None, {"results": [None, {"packages": 5}]})):
            dr.run("/nonexistent", npm, pip, osv)


class Lockfile(BaseTestCase):
    def repo_with(self, files):
        d = self.tmpdir()
        for rel, content in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(content)
        return d

    def test_peer_mismatch_and_optional_peers(self):
        d = self.repo_with({"package-lock.json": lock({
            "zod": {"version": "4.0.0"},
            "@hookform/resolvers": {"version": "3.3.0", "peerDependencies": {"react-hook-form": "^7.0.0", "zod": "^3.0.0"}},
            "react-hook-form": {"version": "7.50.0"},
            "tool": {"version": "1.0.0", "peerDependencies": {"zod": "^3"}, "peerDependenciesMeta": {"zod": {"optional": True}}},
            "odd": {"version": "1.0.0", "peerDependencies": {"zod": "workspace:*"}}})})
        report, issues, _ = dr.run(d)
        self.assertEqual([(p["name"], p["peer"]) for p in report["peers"]], [("@hookform/resolvers", "zod")])
        self.assertEqual(issues[0]["title"], "deps: peer mismatch @hookform/resolvers needs zod ^3.0.0")

    def test_nested_and_hostile_lock_entries_are_ignored(self):
        d = self.repo_with({"package-lock.json": lock({"a": {"version": "1.0.0"}, "a/node_modules/b": {"version": "9.0.0"},
                                                      "$(id)": {"version": "1.0.0"}, "c": {"version": "1.0.0\n"}, "d": "string"})})
        v, _ = dr.npm_lock(d)
        self.assertEqual(v, {"a": "1.0.0"})
        with open(os.path.join(d, "package-lock.json"), "w") as fh:
            fh.write("{broken")
        self.assertEqual(dr.npm_lock(d), ({}, {}))

    def test_primer_drift_names_real_dependencies_only(self):
        d = self.repo_with({
            "package-lock.json": lock({"zod": {"version": "4.0.0"}, "next": {"version": "14.2.0"}, "react": {"version": "18.3.1"}}),
            ".claude/context/backend.md": "Validation uses `zod@3` everywhere. We use react 18.x. Next 2 steps: deploy.\nAlso nothing 5.x about foo.\n",
            ".claude/CLAUDE.md": "| Frontend | next v14 |\n"})
        report, issues, _ = dr.run(d)
        self.assertEqual([(x["name"], x["declared"], x["installed"]) for x in report["drift"]], [("zod", "3", "4.0.0")])
        self.assertEqual(issues[0]["title"], "deps: zod is 3 in the primer, 4.0.0 in the lockfile")

    def test_poetry_lock_is_read(self):
        d = self.repo_with({"poetry.lock": '[[package]]\nname = "Pydantic_Core"\nversion = "2.1.0"\n\n[[package]]\nname = "x"\nversion = "1.0"\n',
                            ".claude/context/backend.md": "We pin pydantic-core 1.x for now.\n"})
        self.assertEqual(dr.poetry_lock(d)["pydantic-core"], "2.1.0")
        report, _, _ = dr.run(d)
        self.assertEqual(report["drift"][0]["declared"], "1")

    def test_a_fifo_for_a_primer_or_lockfile_does_not_hang(self):
        d = self.repo_with({"package-lock.json": lock({"zod": {"version": "4.0.0"}})})
        os.makedirs(os.path.join(d, ".claude", "context"))
        os.mkfifo(os.path.join(d, ".claude", "context", "backend.md"))
        os.mkfifo(os.path.join(d, "poetry.lock"))
        dr.run(d)


class Cli(BaseTestCase):
    def test_writes_three_files_and_caps_the_flag(self):
        d = self.tmpdir()
        npm = os.path.join(d, "npm.json")
        with open(npm, "w") as fh:
            json.dump({"zod": {"current": "3.0.0", "latest": "4.0.0"}}, fh)
        out = os.path.join(d, "out")
        cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "dependency_report.py"), "--repo", d, "--out-dir", out, "--npm-outdated", npm]
        p = subprocess.run(cmd, env=plain_env(d), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["issues.json", "report.json", "summary.md"])
        self.assertEqual(jload(os.path.join(out, "issues.json"))[0]["title"], "deps: zod 3.0.0 -> 4.0.0 (major)")
        bad = subprocess.run(cmd + ["--max-issues", "999"], env=plain_env(d), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(bad.returncode, 2)

    def test_missing_or_hostile_input_files_mean_no_findings(self):
        d = self.tmpdir()
        fifo = os.path.join(d, "f.json")
        os.mkfifo(fifo)
        out = os.path.join(d, "o")
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "dependency_report.py"), "--repo", d, "--out-dir", out, "--npm-outdated", fifo,
                            "--osv", os.path.join(d, "nope.json")], env=plain_env(d), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(jload(os.path.join(out, "issues.json")), [])


if __name__ == "__main__":
    unittest.main()
