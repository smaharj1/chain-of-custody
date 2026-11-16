"""G2: interface stubs first, tests red against them (report section 9 G2, G3 \"red on main\", CS-15)."""
from __future__ import annotations

import os

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, STUB, UNITTEST_CMD, GateCase

from loopctl import locked
from loopctl.gates import _acrun

IMPL_WRONG_SUB = IMPL.replace("return a - b", "return a + b")
SENTINEL_STUB = "def add(a, b):\n    return 3\n\n\ndef sub(a, b):\n    raise NotImplementedError\n"


class G2(GateCase):
    def staged(self, stubs=STUB, tests=ACCEPTANCE_OK):
        self.commit(dict(PKG, **{"src/calc.py": stubs}), "stubs")
        self.commit({"tests/acceptance/test_a.py": tests}, "tests")

    def test_stubs_then_red_tests_pass_and_pin_the_tests(self):
        self.staged()
        res = self.run_gate("g2")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["summary"], "red on the stubs 2/2")
        self.assertNotIn("ac_results", res["evidence"])            # red on a stub is not an executed passing test: the matrix must not count it
        rs = self.ctx().__dict__["paths"]["runstate"]
        self.assertIn("tests/acceptance/**", locked.read(_acrun._lib.runstate_of(self.ctx()))["sources"]["g3"])
        from guards import lib as glib, paths as gpaths
        globs = glib.locked_paths(self.ctx_rs())
        self.assertTrue(gpaths.match_rel("tests/acceptance/test_a.py", globs))           # the file guard's own matcher denies an engineer's write to it
        self.assertFalse(gpaths.match_rel("src/calc.py", globs))
        pin = _acrun.read_pin(self.ctx())
        self.assertEqual(sorted(pin), ["tests/acceptance/__init__.py", "tests/acceptance/test_a.py"])
        self.assertTrue(os.path.isdir(rs))

    def test_running_g2_again_cannot_launder_an_edited_test(self):
        self.staged()
        self.assertPass(self.run_gate("g2"))
        self.assertPass(self.run_gate("g2"))                            # idempotent while nothing changed
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("3)", "None)"), "src/calc.py": STUB.replace("raise NotImplementedError", "return None")}, "edit and break")
        res = self.run_gate("g2")
        self.assertFails(res, "g2.pin-violated")
        self.assertTrue(any(f.get("reward_hack") for f in res["findings"]))

    def test_tests_that_pass_against_a_sentinel_stub_prove_nothing(self):
        self.staged(stubs="def add(a, b):\n    return 3\n\n\ndef sub(a, b):\n    return 3\n")
        self.assertFails(self.run_gate("g2"), "g2.not-red")

    def test_a_collection_or_import_error_is_invalid_not_red(self):
        self.staged(tests=ACCEPTANCE_OK.replace("from src import calc", "from src import not_there as calc"))
        res = self.run_gate("g2")
        self.assertFails(res, "g2.invalid-red")
        self.assertNotIn("g2.not-red", self.ids(res))

    def test_a_stub_with_logic_is_refused(self):
        self.staged(stubs=IMPL)
        self.assertFails(self.run_gate("g2"), "g2.stub-has-logic")

    def test_stubs_must_precede_the_tests(self):
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK, **{k: v for k, v in PKG.items()}}, "tests first")
        self.commit({"src/calc.py": STUB}, "stubs second")
        self.assertFails(self.run_gate("g2"), "g2.tests-before-stubs")

    def test_a_criterion_no_test_names_is_uncovered(self):
        self.staged(tests=ACCEPTANCE_OK.replace("test_ac_a_2_subtracts", "test_subtracts"))
        res = self.run_gate("g2")
        self.assertFails(res, "g2.ac-uncovered")

    def test_a_skipped_acceptance_test_is_refused(self):
        self.staged(tests=ACCEPTANCE_OK.replace("    def test_ac_a_1_adds", "    @unittest.skip('later')\n    def test_ac_a_1_adds"))
        self.assertFails(self.run_gate("g2"), "g2.skipped-ac-test")

    def test_no_visible_files_and_no_stub_files(self):
        self.commit({"src/__init__.py": ""}, "nothing")
        self.assertFails(self.run_gate("g2"), "g2.no-visible-tests")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        self.assertFails(self.run_gate("g2"), "g2.stub-missing")

    def test_characterization_expects_green_on_the_base(self):
        pass        # covered in Characterization below (it needs a different plan item)


class G2Plan(GateCase):
    ITEMS = [item_doc("A", acs=2), item_doc("B", acs=2, ext={"g3": {"exempt": "documentation only"}}),
             item_doc("C", acs=2, ext={"g3": {"visible": ["tests/acceptance/**"], "class": "characterization", "cmd": UNITTEST_CMD}}),
             item_doc("D", acs=2, ext={"g3": {"visible": ["tests/acceptance/**"], "cmd": UNITTEST_CMD, "bogus": 1}})]

    def test_an_item_with_no_declared_acceptance_artifacts_fails(self):
        res = self.run_gate("g2", "A")
        self.assertFails(res, "g2.not-declared")

    def test_an_exempt_item_passes_with_a_human_flag(self):
        res = self.run_gate("g2", "B")
        self.assertPass(res)
        self.assertIn("g2.exempt", self.ids(res))
        self.assertEqual(res["findings"][0]["severity"], "warn")

    def test_a_bad_declaration_is_a_finding(self):
        self.assertFails(self.run_gate("g2", "D"), "g3.ext-unknown-key")

    def test_characterization_wants_green_not_red(self):
        self.commit(dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK.replace("test_ac_a", "test_ac_c")}), "existing behaviour")
        res = self.run_gate("g2", "C")
        self.assertPass(res)
        self.assertIn("g3.characterization", self.ids(res))
        # red characterization tests mean the behaviour is not what the tests say
        self.commit({"src/calc.py": IMPL_WRONG_SUB}, "break")
        self.assertFails(self.run_gate("g2", "C"), "g2.not-green")


if __name__ == "__main__":
    import unittest
    unittest.main()
