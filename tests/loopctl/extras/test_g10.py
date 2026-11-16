"""G10: product checks, tool-agnostic, OPT-IN and evidence-gated. No tool is bundled; the commands are the project's and run without a shell."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import unittest

import ci_lint

from tests import REPO_ROOT
from ..gates import GateCase
from .. import item_doc

from kitlib import config as kconfig

from loopctl import gates
from loopctl.gates import _ci, g10_product as g10

from . import EVIDENCE

PY = shlex.quote(sys.executable)
CHECK = '''import json, os, sys
check = sys.argv[1]
scope = json.load(open(os.environ["LOOPCTL_G10_SCOPE"]))
data = "g10-%s.txt" % check
spec = open(data).read().split() if os.path.exists(data) else ["3", "0"]
kind = spec[0]
if kind == "silent":
    print("no result for you")
elif kind == "exit":
    print("the browser would not start")
    sys.exit(1)
elif kind == "scope":
    print(json.dumps({"schema": "g10.v1", "check": check, "checked": len(scope["files"]), "violations": 0, "details": scope["files"]}))
elif kind == "other":
    print(json.dumps({"schema": "g10.v1", "check": "perf" if check != "perf" else "a11y", "checked": 3, "violations": 0}))
elif kind == "float":
    print(json.dumps({"schema": "g10.v1", "check": check, "checked": 3.5, "violations": 0}))
elif kind == "bool":
    print(json.dumps({"schema": "g10.v1", "check": check, "checked": True, "violations": 0}))
elif kind == "extra":
    print(json.dumps({"schema": "g10.v1", "check": check, "checked": 3, "violations": 0, "status": "pass"}))
elif kind == "details":
    print(json.dumps({"schema": "g10.v1", "check": check, "checked": 3, "violations": 2, "details": ["missing alt text on /home", "low contrast on /cart"]}))
else:
    print("noise before the result")
    print(json.dumps({"schema": "g10.v1", "check": check, "checked": int(spec[0]), "violations": int(spec[1])}))
'''
FILES = {"check.py": CHECK, "src/__init__.py": "", "src/page.py": "x = 1\n", "docs/readme.md": "hello\n", "tests/test_page.py": "def test_x():\n    pass\n"}
OPEN_GATE = "gates:\n  g10: 'on'\n"


def cmd(check):
    return "%s check.py %s" % (PY, check)


class Base(GateCase):
    ITEMS = [item_doc("A", acs=1, touches={"domains": ["backend"], "paths": ["src/**", "docs/**"], "schema": False, "deps_change": False})]
    BASE_FILES = FILES
    CFG = OPEN_GATE + "g10_a11y_cmd: \"%s\"\ng10_perf_cmd: \"%s\"\n" % (cmd("a11y"), cmd("perf"))

    def evidence(self):
        self.lr.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))

    def work(self, extra=None):
        files = {"src/page.py": "x = 2\n"}
        files.update(extra or {})
        self.commit(files, "work")


class G10(Base):
    def test_it_refuses_to_run_without_the_evidence_gate(self):
        self.work()
        res = self.run_gate("g10")
        self.assertEqual((res["status"], res["blocking"]), ("error", True))
        self.assertEqual(res["findings"][0]["id"], "gate.evidence-required")

    def test_clean_checks_pass_and_the_result_is_recorded_per_check(self):
        self.evidence()
        self.work()
        res = self.run_gate("g10")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["checks"], {"a11y": {"checked": 3, "violations": 0}, "perf": {"checked": 3, "violations": 0}})
        self.assertEqual(res["mode"], "on")

    def test_a_check_that_finds_violations_fails_the_item_with_what_it_said(self):
        self.evidence()
        self.work({"g10-a11y.txt": "details"})
        res = self.run_gate("g10")
        self.assertFails(res, "g10.violations")
        self.assertEqual(res["failure_class"], "item")
        self.assertIn("missing alt text on /home", res["findings"][0]["message"])
        self.assertEqual(res["evidence"]["checks"]["perf"]["violations"], 0)            # the other check still ran and passed

    def test_a_check_that_looked_at_nothing_proves_nothing(self):
        self.evidence()
        self.work({"g10-perf.txt": "0 0"})
        self.assertFails(self.run_gate("g10"), "g10.nothing-checked")

    def test_the_scope_is_the_changed_production_files_only(self):
        self.evidence()
        self.work({"g10-a11y.txt": "scope", "docs/readme.md": "changed\n", "tests/test_page.py": "def test_x():\n    assert 1\n"})
        res = self.run_gate("g10")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["checks"]["a11y"]["checked"], 1)               # src/page.py only: not the document, not the test, not a .txt

    def test_a_change_with_no_production_file_is_skipped(self):
        self.evidence()
        self.commit({"docs/readme.md": "more words\n", "tests/test_page.py": "def test_x():\n    assert 2\n"}, "docs and tests")
        self.assertEqual(self.run_gate("g10")["status"], "skipped")

    def test_a_result_that_breaks_the_contract_is_a_failure_never_a_pass(self):
        cases = {"silent": "g10.bad-result", "other": "g10.bad-result", "bool": "g10.bad-result", "extra": "g10.bad-result", "exit": "g10.command-failed"}
        for mode, fid in cases.items():
            with self.subTest(mode):
                self.setUp()
                self.evidence()
                self.work({"g10-a11y.txt": mode})
                res = self.run_gate("g10")
                self.assertFails(res, fid)
                self.assertNotEqual(res["status"], "pass")

    def test_an_item_cannot_rewrite_the_check_that_judges_it(self):
        self.evidence()
        self.work({"check.py": 'import json\nprint(json.dumps({"schema": "g10.v1", "check": "a11y", "checked": 99, "violations": 0}))\n'})
        res = self.run_gate("g10")
        self.assertFails(res, "g10.tool-edited")
        self.assertIn("check.py", res["findings"][0]["message"])

    def test_uncommitted_changes_are_not_judged(self):
        self.evidence()
        self.work()
        self.lr.repo.write("src/page.py", "x = 3\n")
        self.assertFails(self.run_gate("g10"), "g10.uncommitted-changes")

    def test_the_ci_half_runs_from_the_base_and_reports_annotations(self):
        self.evidence()
        self.work({"g10-a11y.txt": "details"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = _ci.main(["g10", "--repo", self.lr.path, "--base", self.base, "--head-ref", "feat/slice-a-a1"])
        self.assertEqual(rc, 1)
        self.assertIn("::error title=g10::", out.getvalue())
        self.assertEqual(_ci.main(["g10", "--repo", self.lr.path, "--base", "nothex"]), 2)


class Configs(Base):
    """The same repository judged under other base configs (the gate context carries the config as read at the base sha; a test swaps it)."""

    def judge(self, cfg_text):
        gctx = self.ctx("A")
        gctx.cfg = kconfig.load_text("```yaml\n%s```\n" % cfg_text)
        self.purge_bytecode()
        return gates.run_gate("g10", gctx)

    def test_report_mode_records_a_failure_without_blocking(self):
        self.evidence()
        self.work({"g10-a11y.txt": "1 4"})
        res = self.judge("gates:\n  g10: report\ng10_a11y_cmd: \"%s\"\n" % cmd("a11y"))
        self.assertEqual((res["status"], res["blocking"], res["mode"]), ("fail", False, "report"))

    def test_a_command_with_shell_syntax_is_refused_and_nothing_runs(self):
        self.evidence()
        self.work()
        res = self.judge(OPEN_GATE + "g10_a11y_cmd: \"%s check.py a11y; touch pwned\"\n" % PY)
        self.assertFails(res, "g10.bad-command")
        self.assertIn("without a shell", res["findings"][0]["message"])
        self.assertFalse(os.path.exists(os.path.join(self.lr.path, "pwned")))
        res = self.judge(OPEN_GATE + "g10_a11y_cmd: \"%s check.py a11y $(touch pwned)\"\n" % PY)
        self.assertFails(res, "g10.bad-command")
        self.assertFalse(os.path.exists(os.path.join(self.lr.path, "pwned")))

    def test_a_switched_on_gate_with_no_check_is_an_error_not_a_pass(self):
        self.evidence()
        self.work()
        res = self.judge(OPEN_GATE)
        self.assertEqual((res["status"], res["blocking"]), ("error", True))
        self.assertIn("no product-check tool", res["findings"][0]["message"])

    def test_the_default_is_off_and_off_runs_nothing(self):
        self.work()
        res = self.judge("g10_a11y_cmd: \"%s\"\n" % cmd("a11y"))
        self.assertEqual((res["status"], res["mode"], res["blocking"]), ("skipped", "off", False))


class Parse(unittest.TestCase):
    def test_what_is_a_command(self):
        argv = g10.parse_command("%s check.py a11y --url=http://localhost:3000/ -x" % PY, REPO_ROOT)
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertEqual(argv[1:], ["check.py", "a11y", "--url=http://localhost:3000/", "-x"])
        self.assertEqual(g10.parse_command("python3 -c pass", REPO_ROOT)[1:], ["-c", "pass"])

    def test_shell_syntax_and_look_alikes_are_all_refused(self):
        bad = ["python3 a.py; rm -rf /", "python3 a.py | tee x", "python3 a.py > out", "python3 a.py < in", "python3 a.py && ls", "python3 a.py &", "python3 $(whoami)", "python3 `id`",
               "python3 $HOME/x", "python3 a.py \\\n b", "python3 (a)", "python3 {a,b}", "python3 *.py", "python3 a?.py", "python3 !history", "python3 'unbalanced",
               "", "   ", "<command>", "x" * 1100, "python3 a.py\nrm x", "python3 a\x00b", " ".join(["a"] * 40), "python3 " + "y" * 400]
        for text in bad:
            with self.subTest(text[:30]):
                with self.assertRaises(g10.BadCommand):
                    g10.parse_command(text, REPO_ROOT)

    def test_the_program_must_be_a_real_executable_inside_or_outside_the_worktree_on_purpose(self):
        for text in ("no-such-program-anywhere --x", "/no/such/prog", "tools/missing.sh", "../../../../bin/sh", "./../outside"):
            with self.subTest(text):
                with self.assertRaises(g10.BadCommand):
                    g10.parse_command(text, REPO_ROOT)

    def test_the_ci_template_is_untrusted_and_reads_the_gate_state_from_the_base(self):
        wf = os.path.join(REPO_ROOT, "templates", "github", "workflows", "g10-product.yml")
        findings = ci_lint.lint_paths([wf], repo=REPO_ROOT)
        self.assertEqual([str(f) for f in findings if f.severity == "error"], [])
        self.assertEqual({f.rule for f in findings if f.severity == "warn"}, {"ci.floating-action-tag"})
        with open(wf, encoding="utf-8") as fh:
            text = "\n".join(ln for ln in fh.read().split("\n") if not ln.lstrip().startswith("#"))
        for banned in ("secrets.", "id-token", "pull_request_target", "contents: write"):
            self.assertNotIn(banned, text)
        self.assertIn('PYTHONPATH="$KIT_SCRIPTS" python3 -m loopctl.gates._ci g10', text)
        self.assertIn('git -C "$RUNNER_TEMP/kit" fetch', text)                # the kit is the pinned plugin release, never the pull request's
        self.assertIn('"$got" != "$KIT_SHA"', text)

    def test_contract_tests_are_design_only_no_key_no_code_no_gate_module(self):
        from kitlib import registry
        reg = registry.load_registry()
        self.assertEqual(sorted(k for k in reg.names() if k.startswith("g10")), ["g10_a11y_cmd", "g10_observability_cmd", "g10_perf_cmd", "g10_pii_cmd", "g10_timeout_s"])
        self.assertIn("g10", gates.KNOWN_GATES)
        self.assertEqual(g10.DEFAULT_MODE, "off")
        self.assertTrue(g10.EVIDENCE_GATED)
        self.assertEqual([c for c, _k in g10.CHECKS], ["a11y", "perf", "pii", "observability"])        # api-contract.md tests are not among them
        with open(os.path.join(REPO_ROOT, "spec", "loopctl-extras.md"), encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("DESIGN-ONLY", spec.split("Contract tests", 1)[1][:600])


if __name__ == "__main__":
    unittest.main()
