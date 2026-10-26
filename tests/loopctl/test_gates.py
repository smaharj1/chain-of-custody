"""The gate plug-in interface and runner: toggles read from the base sha, OPT-IN and evidence-gated gates, crashing gates (report section 9, IMP-22)."""
from __future__ import annotations

import types
import unittest

from loopctl import gates
from loopctl.common import Refused, UsageError
from loopctl.gates import GateResult

from . import LoopTestCase, item_doc


def mod(name="g6", run=None, **attrs):
    m = types.SimpleNamespace(NAME=name, OWNER_METRIC="verify-pass-but-ci-fail", run=run or (lambda ctx: GateResult.passed(note="ok")), __name__="fake_" + name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class Base(LoopTestCase):
    def setUp(self):
        for n in list(gates._registered):
            gates.unregister(n)

    def tearDown(self):
        for n in list(gates._registered):
            gates.unregister(n)

    def ctx_for(self, lr, iid="A", head=None):
        loop = lr.loop(fresh=True)
        st, _ = loop.store.read()
        return loop, loop.ctx.gate_context(iid, head, None, st.items[iid] if st else {})


class Runner(Base):
    def started(self, **kw):
        lr = self.lr(**kw)
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        return lr

    def test_on_blocks_on_fail_and_error_report_never_blocks_off_skips(self):
        for mode, status, blocking in (("on", "fail", True), ("on", "error", True), ("on", "pass", False), ("report", "fail", False),
                                       ("report", "error", False), ("off", "fail", False)):
            with self.subTest(mode=mode, status=status):
                lr = self.started(extra_cfg="gates:\n  g6: %s" % mode)
                ran = []
                gates.register(mod(run=lambda ctx, s=status: ran.append(1) or GateResult(s)))
                _, gctx = self.ctx_for(lr)
                res = gates.run_gate("g6", gctx)
                self.assertEqual(res["blocking"], blocking)
                self.assertEqual(res["mode"], mode)
                self.assertEqual(bool(ran), mode != "off")
                if mode == "off":
                    self.assertEqual(res["status"], "skipped")
                gates.unregister("g6")

    def test_the_toggle_is_the_base_shas_not_the_branchs(self):
        lr = self.started(extra_cfg="gates:\n  g6: report")
        gates.register(mod(run=lambda ctx: GateResult("fail")))
        # a branch commit tries to turn its own gate off: the context is still built from the base
        lr.repo.git("switch", "-q", "-c", "feat/slice-a-a1", "main")
        lr.repo.write(".claude/loop.config.md", lr.cfg_text.replace("g6: report", "g6: off"))
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "weaken my own gate")
        loop = lr.loop(fresh=True)
        self.assertEqual(loop.ctx.base_sha, lr.base)
        self.assertEqual(loop.ctx.cfg.gate("g6"), "report")
        self.assertEqual([n for n, _b, _h in loop.ctx.config_drift], ["gates.g6"])         # listed, not applied
        _, gctx = self.ctx_for(lr)
        res = gates.run_gate("g6", gctx)
        self.assertEqual((res["mode"], res["status"], res["blocking"]), ("report", "fail", False))

    def test_a_gate_that_crashes_or_lies_is_an_error_never_a_pass(self):
        lr = self.started()
        def boom(ctx):
            raise RuntimeError("kaboom")
        shapes = {"crash": boom, "none": lambda ctx: None, "string": lambda ctx: "pass", "bad status": lambda ctx: GateResult("great"),
                  "bad class": lambda ctx: GateResult("fail", failure_class="whatever"), "bad evidence": lambda ctx: GateResult("pass", evidence=[1]),
                  "dict": lambda ctx: {"status": "nope"}}
        for name, fn in shapes.items():
            with self.subTest(name):
                gates.register(mod(run=fn))
                _, gctx = self.ctx_for(lr)
                res = gates.run_gate("g6", gctx)
                self.assertEqual(res["status"], "error")
                self.assertTrue(res["blocking"])
                self.assertEqual(res["failure_class"], "env")
                gates.unregister("g6")

    def test_a_dict_result_is_accepted_and_a_fail_without_a_class_is_an_item_failure(self):
        lr = self.started()
        gates.register(mod(run=lambda ctx: {"status": "fail", "findings": [{"id": "x", "severity": "error", "message": "m"}]}))
        _, gctx = self.ctx_for(lr)
        res = gates.run_gate("g6", gctx)
        self.assertEqual((res["status"], res["failure_class"]), ("fail", "item"))

    def test_opt_in_gates_default_to_off_and_evidence_gated_ones_refuse_to_run(self):
        lr = self.started()
        ran = []
        gates.register(mod("g4", run=lambda ctx: ran.append(1) or GateResult("pass"), DEFAULT_MODE="off", EVIDENCE_GATED=True))
        _, gctx = self.ctx_for(lr)
        res = gates.run_gate("g4", gctx)
        self.assertEqual((res["mode"], res["status"], ran), ("off", "skipped", []))
        lr2 = self.started(extra_cfg="gates:\n  g4: on")
        _, gctx = self.ctx_for(lr2)
        res = gates.run_gate("g4", gctx)
        self.assertEqual((res["status"], res["blocking"], ran), ("error", True, []))
        self.assertEqual(res["findings"][0]["id"], "gate.evidence-required")
        # once the human commits a complete evidence file the gate runs
        import json
        lr2.repo.write("docs/baseline/evidence.json", json.dumps({
            "schema": "evidence.v1", "fixture_runs": [{"id": "r%d" % i, "clean": True} for i in range(3)],
            "real_feature": {"id": "f", "completed": True}, "attested_by": "Pat"}))
        _, gctx = self.ctx_for(lr2)
        self.assertTrue(gctx.evidence_passed)
        self.assertEqual(gates.run_gate("g4", gctx)["status"], "pass")

    def test_findings_are_cleaned_and_capped(self):
        lr = self.started()
        gates.register(mod(run=lambda ctx: GateResult("fail", findings=[{"id": "x", "severity": "error", "message": "\x1b[31mred\x1b[0m" + "y" * 900}] * 80)))
        _, gctx = self.ctx_for(lr)
        res = gates.run_gate("g6", gctx)
        self.assertEqual(len(res["findings"]), 50)
        self.assertNotIn("\x1b", res["findings"][0]["message"])
        self.assertLessEqual(len(res["findings"][0]["message"]), 400)

    def test_an_unknown_or_broken_gate_is_a_clear_error(self):
        with self.assertRaises(gates.GateNotInstalled):
            gates.get("g6")
        with self.assertRaises(Exception):
            gates.register(types.SimpleNamespace(NAME="", OWNER_METRIC="x", run=None))

    def test_discovery_lists_gate_modules_in_the_package_and_reports_a_broken_one(self):
        import os, tempfile, shutil
        d = os.path.dirname(gates.__file__)
        path = os.path.join(d, "zz_broken_for_test.py")
        try:
            with open(path, "w") as fh:
                fh.write("raise ImportError('missing dependency')\n")
            found = gates.discover(refresh=True)
            self.assertIn("!zz_broken_for_test", found)
            gates._discovered = None
        finally:
            os.unlink(path)
            gates.discover(refresh=True)


class Advance(Base):
    def test_a_gate_marked_required_for_a_transition_must_have_passed(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        gates.register(mod("g6", REQUIRED_FOR=(("verifying", "reviewing"),)))
        loop = lr.loop(fresh=True)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "reviewing", lr.session)
        self.assertIn("gate g6 must pass", str(cm.exception))
        gctx = loop.ctx.gate_context("A", "a" * 40, None, lr.item("A"))
        res = gates.run_gate("g6", gctx)
        loop.record_gate("A", "g6", res, lr.session)
        with self.assertRaises(Refused) as cm:
            loop.advance("A", "reviewing", lr.session, head="b" * 40)
        self.assertIn("stale", str(cm.exception))
        loop.advance("A", "reviewing", lr.session, head="a" * 40)

    def test_a_required_gate_that_is_off_or_a_failing_one_in_report_mode_does_not_block(self):
        lr = self.lr(extra_cfg="gates:\n  g6: off")
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        gates.register(mod("g6", REQUIRED_FOR=(("verifying", "reviewing"),)))
        lr.loop(fresh=True).advance("A", "reviewing", lr.session)

    def test_a_failed_result_does_not_satisfy_the_requirement(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        lr.walk("A", "design_review", "building", "verifying")
        gates.register(mod("g6", run=lambda ctx: GateResult("fail"), REQUIRED_FOR=(("verifying", "reviewing"),)))
        loop = lr.loop(fresh=True)
        gctx = loop.ctx.gate_context("A", None, None, lr.item("A"))
        loop.record_gate("A", "g6", gates.run_gate("g6", gctx), lr.session)
        with self.assertRaises(Refused):
            loop.advance("A", "reviewing", lr.session)


class Evidence(Base):
    def test_ac_results_fold_into_the_matrix_and_unknown_acs_are_ignored(self):
        lr = self.lr()
        loop = lr.fast_start()
        loop.begin("A", lr.session)
        res = {"status": "pass", "mode": "on", "blocking": False, "head": "a" * 40, "failure_class": "none", "findings": [],
               "evidence": {"ac_results": {"AC-A-1": {"result": "pass", "evidence": "tests/test_a.py::test_one"},
                                           "AC-A-2": {"result": "fail", "evidence": "tests/test_a.py::test_two"},
                                           "AC-B-1": {"result": "pass", "evidence": "other item"},
                                           "AC-A-9": {"result": "pass", "evidence": "no such criterion"},
                                           "AC-A-3": {"result": "maybe"}},
                            "review": {"code": "CODE_REVIEW_APPROVED", "bogus": "x"}}}
        loop.record_gate("A", "g3", res, lr.session)
        rec = lr.item("A")
        self.assertEqual(sorted(rec["ac"]), ["AC-A-1", "AC-A-2"])
        self.assertEqual(rec["ac"]["AC-A-2"]["g3"]["result"], "fail")
        self.assertEqual(rec["review"], {"code": "CODE_REVIEW_APPROVED"})
        with self.assertRaises(UsageError):
            loop.record_gate("A", "../evil", res, lr.session)


if __name__ == "__main__":
    unittest.main()
