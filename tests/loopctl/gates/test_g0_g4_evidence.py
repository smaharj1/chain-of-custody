"""G0 (the environment is fit to start), G4 (mutation, OPT-IN), Evidence (the aggregator)."""
from __future__ import annotations

import json
import os
import socket
import stat
import sys

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, GateCase, g3_item

from loopctl import gates

EVIDENCE = json.dumps({"schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
                       "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"})
UNIT = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1 + 0)\n\n    @unittest.skip('known')\n    def test_two(self):\n        pass\n"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()


class G0(GateCase):
    ITEMS = [item_doc("A", acs=1), item_doc("E", acs=1, slug="slice-e", requiresEnv=["DATABASE_URL"])]
    CFG = ("verify_tests: \"%s -m unittest -v tests.unit.test_u\"\nverify_ports:\n  - %d\nenv_check_cmd: \"%s check_env.py\"\n" % (sys.executable, PORT, sys.executable))
    BASE_FILES = {"tests/__init__.py": "", "tests/unit/__init__.py": "", "tests/unit/test_u.py": UNIT, "check_env.py": "import os, sys\nsys.exit(1 if os.path.exists('db-down') else 0)\n"}

    def test_a_fit_environment_passes_and_records_the_baseline(self):
        res = self.run_gate("g0")
        self.assertPass(res)
        self.assertIn("baseline 2 tests, 1 skipped", res["evidence"]["summary"])
        from loopctl.gates import _lib
        path = os.path.join(_lib.runstate_of(self.ctx()).root, "baseline", "A.json")
        with open(path) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["collected"], rec["skipped"], rec["base"]), (2, 1, self.base))

    def test_the_doctor_is_read_and_a_level_it_allows_does_not_fail_the_gate(self):
        res = self.run_gate("g0")
        self.assertNotIn("g0.level-capped", self.ids(res))          # configured L1, effective L1

    def test_a_missing_required_environment_variable_names_the_variable_not_a_value(self):
        res = self.run_gate("g0", "E")
        self.assertFails(res, "g0.missing-env-var")
        self.assertIn("DATABASE_URL", [f["message"] for f in res["findings"] if f["id"] == "g0.missing-env-var"][0])
        self.assertEqual(res["failure_class"], "env")
        self.lr.env["DATABASE_URL"] = "postgres://user:pw@host/db"
        try:
            res = self.run_gate("g0", "E")
            self.assertNotIn("g0.missing-env-var", self.ids(res))
            self.assertNotIn("user:pw", json.dumps(res))
        finally:
            del self.lr.env["DATABASE_URL"]

    def test_a_dirty_tree_is_an_env_failure(self):
        with open(os.path.join(self.lr.path, "stray.txt"), "w") as fh:
            fh.write("x")
        res = self.run_gate("g0")
        self.assertFails(res, "g0.tree-dirty")
        self.assertEqual(res["failure_class"], "env")

    def test_design_documents_do_not_make_the_tree_dirty(self):
        d = os.path.join(self.lr.path, "docs", "features", "slice-a")
        os.makedirs(d)
        with open(os.path.join(d, "technical-design.md"), "w") as fh:
            fh.write("# design\n")
        self.assertNotIn("g0.tree-dirty", self.ids(self.run_gate("g0")))

    def test_a_toolchain_pin_that_does_not_match_the_installed_tool(self):
        bindir = os.path.join(self.lr.path, "..", "fakebin")
        os.makedirs(bindir, exist_ok=True)
        node = os.path.join(bindir, "node")
        with open(node, "w") as fh:
            fh.write("#!/bin/sh\necho v25.1.0\n")
        os.chmod(node, os.stat(node).st_mode | stat.S_IEXEC)
        old = self.lr.env["PATH"]
        self.lr.env["PATH"] = os.path.realpath(bindir) + os.pathsep + old
        try:
            # the pin must be on main: it is read from the base
            self.lr.repo.git("switch", "-q", "main")
            self.lr.repo.write(".nvmrc", "v20.11.0\n")
            self.lr.commit_all("pin node")
            try:
                self.lr.repo.git("switch", "-q", "-c", "feat/pin-test")
                res = self.run_gate("g0")
                self.assertFails(res, "g0.toolchain")
                self.assertIn("pinned node 20.11.0 but the installed version is v25.1.0", json.dumps(res["findings"]))
            finally:
                self.lr.repo.git("switch", "-q", "main")
                self.lr.repo.git("reset", "-q", "--hard", self.base)
        finally:
            self.lr.env["PATH"] = old

    def test_a_busy_verify_port_is_transient(self):
        s = socket.socket()
        s.bind(("127.0.0.1", PORT))
        s.listen(1)
        try:
            res = self.run_gate("g0")
            self.assertFails(res, "g0.port-in-use")
            self.assertEqual(res["failure_class"], "transient")
        finally:
            s.close()

    def test_the_project_env_check_failing_is_db_down(self):
        with open(os.path.join(self.lr.path, "db-down"), "w") as fh:
            fh.write("")
        res = self.run_gate("g0")
        self.assertIn("g0.env-check", self.ids(res))
        self.assertEqual(res["failure_class"], "env")

    def test_a_red_baseline_is_not_the_items_fault(self):
        self.lr.repo.git("switch", "-q", "main")
        self.lr.repo.write("tests/unit/test_u.py", UNIT.replace("1 + 0", "2"))
        self.lr.commit_all("red on main")
        try:
            self.lr.repo.git("switch", "-q", "-c", "feat/red-base")
            res = self.run_gate("g0")
            self.assertFails(res, "g0.baseline-red")
            self.assertEqual(res["failure_class"], "env")
        finally:
            self.lr.repo.git("switch", "-q", "main")
            self.lr.repo.git("reset", "-q", "--hard", self.base)

    def test_after_code_changed_the_baseline_is_not_rerun(self):
        self.commit({"src/x.py": "x = 1\n"}, "code")
        res = self.run_gate("g0")
        self.assertPass(res)
        self.assertIn("g0.baseline-skipped", self.ids(res))


class G0Level(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    CFG = "autonomy_level: L3\nverify_tests: \"%s -c 'print(1)'\"\n" % sys.executable

    def test_a_run_configured_above_the_doctors_level_does_not_start(self):
        res = self.run_gate("g0")
        self.assertFails(res, "g0.level-capped")
        self.assertEqual(res["failure_class"], "env")
        self.assertIn("configured level L3", [f["message"] for f in res["findings"] if f["id"] == "g0.level-capped"][0])


class G0NoVerify(GateCase):
    ITEMS = [item_doc("A", acs=1)]

    def test_an_unconfigured_baseline_is_an_env_failure(self):
        res = self.run_gate("g0")
        self.assertFails(res, "g0.baseline-config")


MUTATE = '''import json, os, sys
scope = json.load(open(os.environ["LOOPCTL_MUTATION_SCOPE"]))
mode = open("mutants.txt").read().strip() if os.path.exists("mutants.txt") else "70,30,0"
k, s, t = (int(x) for x in mode.split(","))
print("running mutants for", sorted(scope["files"]))
print(json.dumps({"schema": "mutation.v1", "killed": k, "survived": s, "timeout": t}))
'''


class G4(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: on\nmutation_cmd: \"%s mutate.py\"\nmutation_min: 60\n" % sys.executable
    BASE_FILES = dict(PKG, **{"mutate.py": MUTATE, "src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def evidence(self):
        self.lr.repo.write("docs/baseline/evidence.json", EVIDENCE)

    def mutants(self, text):
        with open(os.path.join(self.lr.path, "mutants.txt"), "w") as fh:
            fh.write(text)

    def test_it_refuses_to_run_without_the_evidence_gate(self):
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        # the repo cannot even opt in: the config level refuses; the runner refuses too
        res = self.run_gate("g4")
        self.assertEqual((res["status"], res["blocking"]), ("error", True))
        self.assertEqual(res["findings"][0]["id"], "gate.evidence-required")

    def test_score_at_or_above_mutation_min_passes(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        res = self.run_gate("g4")
        self.assertPass(res)
        self.assertIn("mutation score 70.0%", res["evidence"]["summary"])

    def test_a_low_score_fails_and_counts_timeouts_against(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        self.mutants("50,40,10")
        self.assertFails(self.run_gate("g4"), "g4.score-low")
        self.mutants("60,40,0")
        self.assertPass(self.run_gate("g4"))
        self.mutants("59,40,1")
        self.assertFails(self.run_gate("g4"), "g4.score-low")

    def test_no_mutants_for_changed_production_code_proves_nothing(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        self.mutants("0,0,0")
        self.assertFails(self.run_gate("g4"), "g4.no-mutants")

    def test_only_tests_and_docs_changed_skips(self):
        self.evidence()
        self.commit({"docs/a.md": "x\n", "tests/acceptance/test_a.py": ACCEPTANCE_OK + "\n"}, "docs")
        self.assertEqual(self.run_gate("g4")["status"], "skipped")

    def test_the_scope_file_lists_changed_lines_and_the_acceptance_globs(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n\ndef extra():\n    return 1\n"}, "work")
        self.mutants("7,3,0")
        spec = {}
        from loopctl.gates import g4_mutation
        ctx = self.ctx()
        spec = g4_mutation.changed_lines(ctx)
        self.assertEqual(list(spec), ["src/calc.py"])
        self.assertEqual(spec["src/calc.py"], [[len(IMPL.splitlines()) + 1, len(IMPL.splitlines()) + 4]])

    def test_garbage_or_negative_counts_are_an_error(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        self.mutants("-1,3,0")
        res = self.run_gate("g4")
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["failure_class"], "env")

    def test_a_wrapper_that_prints_nothing_useful_fails(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n", "mutate.py": "print('hello')\n"}, "work")
        # the command comes from the base, so the branch's own mutate.py is NOT used by the base config... it is the same path: be explicit
        res = self.run_gate("g4")
        self.assertEqual(res["status"], "fail")


class G4Unset(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: on\n"

    def test_no_command_is_an_error(self):
        self.lr.repo.write("docs/baseline/evidence.json", EVIDENCE)
        self.commit({"src/x.py": "x = 1\n"}, "work")
        res = self.run_gate("g4")
        self.assertEqual(res["status"], "error")
        self.assertIn("mutation_cmd is not set", res["findings"][0]["message"])


class EvidenceGate(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g0: report\n  g2: report\n"

    def record(self, head, **statuses):
        gs = {}
        for name, status in statuses.items():
            gs[name] = {"status": status, "head": head, "at": "2026-01-01T00:00:00Z"}
        return {"status": "reviewing", "attempt": 1, "gates": gs, "ac": {"AC-A-1": {"g3": {"result": "pass"}}}, "review": {"code": "CODE_REVIEW_APPROVED"}}

    ON = ("g1", "g3", "g5", "g6", "g7", "g9", "g11")

    def test_all_gates_recorded_for_this_head_pass_and_the_local_file_is_written(self):
        h = self.head()
        res = self.run_gate("evidence", state=self.record(h, **{n: "pass" for n in self.ON}))
        self.assertPass(res)
        self.assertIn("7 gate(s) recorded", res["evidence"]["summary"])
        from loopctl.gates import _lib
        path = os.path.join(_lib.runstate_of(self.ctx()).root, "evidence", "A-a1.json")
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["head"], doc["base"], doc["ci"]), (h, self.base, "n/a"))
        self.assertEqual(len(doc["digest"]), 64)
        self.assertIn("evidence.local-only", self.ids(res))

    def test_a_gate_that_never_ran_or_failed_is_missing(self):
        h = self.head()
        st = self.record(h, **{n: "pass" for n in self.ON if n != "g6"})
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")
        st = self.record(h, **{n: ("fail" if n == "g5" else "pass") for n in self.ON})
        res = self.run_gate("evidence", state=st)
        self.assertFails(res, "evidence.gate-missing")
        self.assertIn("g5", json.dumps(res["findings"]))

    def test_a_pass_for_an_older_head_is_stale(self):
        old = self.head()
        self.commit({"src/x.py": "x = 1\n"}, "more work")
        res = self.run_gate("evidence", state=self.record(old, **{n: "pass" for n in self.ON}))
        self.assertFails(res, "evidence.stale")

    def test_pre_build_gates_judged_the_start_and_only_need_to_have_passed(self):
        old = self.head()
        self.commit({"src/x.py": "x = 1\n"}, "more work")
        st = self.record(self.head(), **{n: "pass" for n in self.ON})
        st["gates"]["g0"] = {"status": "pass", "head": old}
        st["gates"]["g2"] = {"status": "pass", "head": old}
        self.assertPass(self.run_gate("evidence", state=st))

    def test_gates_in_report_or_off_mode_are_not_required(self):
        h = self.head()
        st = self.record(h, **{n: "pass" for n in self.ON})
        self.assertNotIn("g0", st["gates"])
        self.assertPass(self.run_gate("evidence", state=st))


class EvidenceWithG4(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g0: report\n  g2: report\n  g4: \"on\"\n"

    def test_a_mutation_gate_that_skipped_because_nothing_needed_mutating_is_not_missing(self):
        h = self.head()
        st = {"status": "reviewing", "attempt": 1, "gates": {n: {"status": "pass", "head": h} for n in EvidenceGate.ON}}
        st["gates"]["g4"] = {"status": "skipped", "head": h}
        self.assertPass(self.run_gate("evidence", state=st))
        st["gates"]["g4"] = {"status": "fail", "head": h}
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")
        del st["gates"]["g4"]
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")
        st["gates"]["g1"] = {"status": "skipped", "head": h}                  # only G4 may skip
        st["gates"]["g4"] = {"status": "pass", "head": h}
        self.assertFails(self.run_gate("evidence", state=st), "evidence.gate-missing")


class EvidenceCI(GateCase):
    ITEMS = [g3_item("A")]
    DELIVERY = "draft-pr"
    CFG = "gates:\n" + "".join("  %s: off\n" % g for g in ("g0", "g1", "g2", "g3", "g5", "g6", "g7", "g9", "g11"))

    def state(self, overlay, status="pr_open"):
        return {"status": status, "attempt": 1, "gates": {}, "overlay": overlay}

    def test_from_pr_open_the_check_run_is_the_gate(self):
        green = {"pr": 7, "state": "OPEN", "draft": True, "checks": "pass", "url": "https://github.com/x/y/pull/7"}
        res = self.run_gate("evidence", state=self.state(green))
        self.assertPass(res)
        self.assertIn("ci pass", res["evidence"]["summary"])
        for checks in ("fail", "pending"):
            self.assertFails(self.run_gate("evidence", state=self.state(dict(green, checks=checks))), "evidence.ci-not-green")

    def test_no_recorded_check_state_is_not_a_pass(self):
        self.assertFails(self.run_gate("evidence", state=self.state(None)), "evidence.ci-unknown")

    def test_before_the_pr_exists_there_is_no_ci_to_wait_for(self):
        self.assertPass(self.run_gate("evidence", state=self.state(None, status="reviewing")))


class Discovery(GateCase):
    ITEMS = [g3_item("A")]

    def test_every_gate_of_the_phase_is_installed_with_the_contract(self):
        self.assertEqual(gates.installed(), ["evidence", "g0", "g1", "g11", "g2", "g3", "g4", "g5", "g6", "g7", "g9"])
        for name in gates.installed():
            mod = gates.get(name)
            self.assertTrue(callable(mod.run))
            self.assertTrue(mod.OWNER_METRIC)
            self.assertEqual(mod.NAME, name)

    def test_g4_is_the_only_off_by_default_evidence_gated_gate(self):
        for name in gates.installed():
            mod = gates.get(name)
            self.assertEqual(getattr(mod, "EVIDENCE_GATED", False), name == "g4", name)
            self.assertEqual(getattr(mod, "DEFAULT_MODE", "on"), "off" if name == "g4" else "on", name)

    def test_required_transitions(self):
        want = {"g0": [("designing", "design_review")], "g2": [("design_review", "building"), ("stubbing", "building")],
                "g1": [("verifying", "reviewing")], "g3": [("verifying", "reviewing")], "g5": [("verifying", "reviewing")],
                "g6": [("verifying", "reviewing")], "g9": [("verifying", "reviewing")], "g11": [("verifying", "reviewing")]}
        for name, trans in want.items():
            self.assertEqual(list(gates.get(name).REQUIRED_FOR), trans, name)
        self.assertIn(("reviewing", "pr_open"), gates.get("g7").REQUIRED_FOR)
        self.assertIn(("awaiting_human", "merged"), gates.get("evidence").REQUIRED_FOR)
        self.assertEqual(getattr(gates.get("g4"), "REQUIRED_FOR", ()), ())


if __name__ == "__main__":
    import unittest
    unittest.main()
