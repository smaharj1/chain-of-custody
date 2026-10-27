"""The CI halves: heldout.yml, heldout-sync.yml, mutation.yml (TEMPLATE form, linted and unit-tested offline) and `python3 -m loopctl.gates._ci`."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import unittest

import ci_lint

from tests import REPO_ROOT
from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, GateCase, g3_item

from loopctl.gates import _ci

WF_DIR = os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows")
NAMES = ("heldout.yml", "heldout-sync.yml", "mutation.yml")


def read(name):
    with open(os.path.join(WF_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def code(name):
    return "\n".join(ln for ln in read(name).split("\n") if not ln.lstrip().startswith("#"))


def errors(text, name="t.yml"):
    return sorted({f.rule for f in ci_lint.lint_text(text, name) if f.severity == "error"})


class Templates(unittest.TestCase):
    def test_they_pass_ci_lint_with_no_errors_and_only_the_pin_warning(self):
        findings = [f for f in ci_lint.lint_paths([WF_DIR], repo=REPO_ROOT) if os.path.basename(f.file) in NAMES]
        self.assertEqual([str(f) for f in findings if f.severity == "error"], [])
        self.assertEqual({f.rule for f in findings if f.severity == "warn"}, {"ci.floating-action-tag"})

    def test_heldout_is_an_untrusted_job_without_any_secret(self):
        text = code("heldout.yml")
        self.assertRegex(text, r"(?m)^on:\n  pull_request:")
        for banned in ("secrets.", "id-token", "pull_request_target", "workflow_run", "contents: write", "pull-requests: write", "actions: read"):
            self.assertNotIn(banned, text, banned)
        self.assertIn("contents: read", text)
        self.assertIn("persist-credentials: false", text)
        self.assertRegex(text, r"(?m)^  heldout:\n    name: heldout$")

    def test_the_restore_comes_after_the_kit_and_right_before_the_tests(self):
        text = code("heldout.yml")
        steps = [s for s in re.split(r"(?m)^      - ", text.split("steps:", 1)[1])[1:]]
        names = [(re.search(r"name: (.*)", s) or re.search(r"uses: (.*)", s)).group(1) for s in steps]
        self.assertEqual(len(steps), 4, names)
        self.assertIn("checkout", steps[0])
        self.assertIn("Take the kit from the base sha", steps[1])
        self.assertIn("actions/cache/restore", steps[2])
        self.assertIn("loopctl.gates._ci heldout", steps[3])
        self.assertIn('git archive "$BASE_SHA" .claude/scripts', steps[1])
        self.assertIn("--base \"$BASE_SHA\"", steps[3])

    def test_untrusted_text_reaches_the_shell_only_through_env(self):
        for n in NAMES:
            for step in re.split(r"(?m)^      - ", code(n)):
                if "run:" in step:
                    run = step.split("run:", 1)[1]
                    self.assertNotIn("${{", run, n)

    def test_the_sync_job_is_trusted_and_the_token_is_scoped_to_one_step(self):
        text = code("heldout-sync.yml")
        self.assertNotIn("pull_request", text)
        self.assertIn("workflow_dispatch", text)
        steps = re.split(r"(?m)^      - ", text)
        with_secret = [s for s in steps if "secrets." in s]
        self.assertEqual(len(with_secret), 1)
        self.assertIn("persist-credentials: false", with_secret[0])
        self.assertIn("rm -rf store/.git", text)                    # no git metadata (and no token in .git/config) reaches the cache
        self.assertNotIn("id-token", text)

    def test_mutation_is_untrusted_and_reads_the_gate_state_from_the_base(self):
        text = code("mutation.yml")
        for banned in ("secrets.", "id-token", "pull_request_target", "contents: write"):
            self.assertNotIn(banned, text, banned)
        self.assertIn("loopctl.gates._ci mutation", text)
        self.assertIn('git archive "$BASE_SHA" .claude/scripts', text)

    def test_ci_lint_still_catches_a_secret_added_while_adapting_heldout(self):
        bad = read("heldout.yml").replace("path: ${{ runner.temp }}/heldout", "path: ${{ runner.temp }}/heldout\n          # t\n").replace(
            "HELDOUT_DIR: ${{ runner.temp }}/heldout", "HELDOUT_DIR: ${{ runner.temp }}/heldout\n          TOKEN: ${{ secrets.STORE }}")
        self.assertIn("ci.secrets-in-pr-job", errors(bad))
        bad = read("heldout.yml").replace("permissions:\n  contents: read", "permissions:\n  contents: read\n  id-token: write")
        self.assertIn("ci.oidc-with-pr-code", errors(bad))


def runner_script():
    import atexit
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="ci-heldout-runner-")
    atexit.register(shutil.rmtree, d, True)
    path = os.path.join(d, "run.py")
    with open(path, "w") as fh:
        fh.write("import json, os\nr = json.load(open(os.path.join(os.environ['LOOPCTL_HELDOUT_DIR'], 'r.json')))\n"
                 "print('detail: the secret assertion is assert_total == 42')\nprint(json.dumps({'results': r, 'note': 'assert_total == 42'}))\n")
    return path


class HeldoutCi(GateCase):
    ITEMS = [g3_item("A", heldout=True), item_doc("B", acs=1, slug="slice-b", ext={"g3": {"exempt": "no held-out here"}})]
    CFG = "runner: sequential\nheldout_cmd: \"%s %s\"\n" % (sys.executable, runner_script())
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def setUp(self):
        super().setUp()
        import tempfile
        self.store = os.path.realpath(tempfile.mkdtemp(prefix="heldout-store-"))
        self.addCleanup(__import__("shutil").rmtree, self.store, True)

    def put(self, results, item="A"):
        d = os.path.join(self.store, item)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "r.json"), "w") as fh:
            json.dump(results, fh)

    def ci(self, head_ref="feat/slice-a-a1", base=None, extra=()):
        out, err = io.StringIO(), io.StringIO()
        argv = ["heldout", "--repo", self.lr.path, "--base", base or self.base, "--head-ref", head_ref, "--dir", self.store] + list(extra)
        old = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(self.lr.env)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _ci.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return rc, out.getvalue(), err.getvalue()

    def test_all_criteria_passing_is_a_pass_with_ids_and_counts_only(self):
        self.put({"AC-A-1": "pass", "AC-A-2": "pass"})
        rc, out, _ = self.ci()
        self.assertEqual(rc, 0, out)
        self.assertIn("held-out 2/2 pass", out)
        self.assertNotIn("assert_total", out)

    def test_a_failing_criterion_names_the_id_and_never_the_harness_output(self):
        self.put({"AC-A-1": "pass", "AC-A-2": "expected assert_total == 42, got 41"})
        rc, out, err = self.ci()
        self.assertEqual(rc, 1, out)
        self.assertIn("::error title=heldout::AC-A-2 fails the held-out check", out)
        self.assertNotIn("assert_total", out + err)
        self.assertNotIn("expected", out + err)

    def test_a_pull_request_that_is_not_an_item_branch_has_nothing_to_run(self):
        for ref in ("dependabot/npm/x", "feat/unknown-slug-a1", "main", "feat/../x", ""):
            rc, out, _ = self.ci(head_ref=ref)
            self.assertEqual(rc, 0, ref)
            self.assertIn("::notice", out)

    def test_an_item_with_no_held_out_tests_declared_passes(self):
        rc, out, _ = self.ci(head_ref="feat/slice-b-a1")
        self.assertEqual(rc, 0)
        self.assertIn("declares no held-out tests", out)

    def test_declared_held_out_tests_missing_from_the_store_fail_never_pass(self):
        rc, out, _ = self.ci()
        self.assertEqual(rc, 1)
        self.assertIn("the restored store has none for it", out)

    def test_a_symlinked_item_directory_is_refused(self):
        outside = os.path.realpath(__import__("tempfile").mkdtemp(prefix="elsewhere-"))
        self.addCleanup(__import__("shutil").rmtree, outside, True)
        with open(os.path.join(outside, "r.json"), "w") as fh:
            json.dump({"AC-A-1": "pass", "AC-A-2": "pass"}, fh)
        os.symlink(outside, os.path.join(self.store, "A"))
        self.assertEqual(self.ci()[0], 1)

    def test_partial_results_fail(self):
        self.put({"AC-A-1": "pass"})
        rc, out, _ = self.ci()
        self.assertEqual(rc, 1)

    def test_a_bad_base_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                self.ci(base="--evil")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.ci(base="HEAD")[0], 2)           # a symbolic name is not a commit id: the base must be exact

    def test_the_head_ref_comes_from_the_environment_when_not_given(self):
        self.put({"AC-A-1": "pass", "AC-A-2": "pass"})
        out = io.StringIO()
        old = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(self.lr.env)
            os.environ["HEAD_REF"] = "feat/slice-a-a2"
            with contextlib.redirect_stdout(out):
                rc = _ci.main(["heldout", "--repo", self.lr.path, "--base", self.base, "--dir", self.store])
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(rc, 0, out.getvalue())


EVIDENCE = json.dumps({"schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
                       "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"})
MUTATE = ("import json, os\nscope = json.load(open(os.environ['LOOPCTL_MUTATION_SCOPE']))\n"
          "k, s = (int(x) for x in open('mutants.txt').read().split(',')) if os.path.exists('mutants.txt') else (8, 2)\n"
          "print(json.dumps({'schema': 'mutation.v1', 'killed': k, 'survived': s, 'timeout': 0}))\n")


class MutationCi(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: report\nmutation_cmd: \"%s mutate.py\"\n" % sys.executable
    BASE_FILES = dict(PKG, **{"mutate.py": MUTATE, "src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def ci(self, base=None):
        out = io.StringIO()
        old = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(self.lr.env)
            with contextlib.redirect_stdout(out):
                rc = _ci.main(["mutation", "--repo", self.lr.path, "--base", base or self.base, "--head-ref", "feat/slice-a-a1"])
        finally:
            os.environ.clear()
            os.environ.update(old)
        return rc, out.getvalue()

    def test_without_evidence_at_the_base_it_refuses(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        rc, out = self.ci()
        self.assertEqual(rc, 1)
        self.assertIn("evidence-gated", out)

    def test_a_pull_request_cannot_supply_its_own_evidence(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n", "docs/baseline/evidence.json": EVIDENCE}, "work and forged evidence")
        self.assertEqual(self.ci()[0], 1)


class MutationCiWithEvidence(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: report\nmutation_cmd: \"%s mutate.py\"\nmutation_min: 60\n" % sys.executable
    BASE_FILES = dict(PKG, **{"mutate.py": MUTATE, "src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK, "docs/baseline/evidence.json": EVIDENCE})
    ci = MutationCi.ci

    def test_pass_and_the_report_mode_does_not_block(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        rc, out = self.ci()
        self.assertEqual(rc, 0, out)
        self.assertIn("mutation score 80.0%", out)
        with open(os.path.join(self.lr.path, "mutants.txt"), "w") as fh:
            fh.write("1,9")
        rc, out = self.ci()
        self.assertEqual(rc, 0)                        # report mode: recorded, not blocking
        self.assertIn("::warning", out)

    def test_not_an_item_branch_has_nothing_to_mutate(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = _ci.main(["mutation", "--repo", self.lr.path, "--base", self.base, "--head-ref", "dependabot/x"])
        self.assertEqual(rc, 0)


class MutationCiOn(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: \"on\"\nmutation_cmd: \"%s mutate.py\"\n" % sys.executable
    BASE_FILES = MutationCiWithEvidence.BASE_FILES
    ci = MutationCi.ci

    def test_on_mode_blocks_a_low_score(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        with open(os.path.join(self.lr.path, "mutants.txt"), "w") as fh:
            fh.write("1,9")
        rc, out = self.ci()
        self.assertEqual(rc, 1, out)
        self.assertIn("below mutation_min", out)


class MutationCiOff(GateCase):
    ITEMS = [g3_item("A")]
    ci = MutationCi.ci

    def test_off_is_off(self):
        rc, out = self.ci()
        self.assertEqual(rc, 0)
        self.assertIn("gates.g4 is off", out)


if __name__ == "__main__":
    unittest.main()
