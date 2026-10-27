"""The readers the gates share: per-criterion results out of test output, static reads of test files, toolchain pins, the plan's ext.g3."""
from __future__ import annotations

import unittest

from .. import item_doc
from loopctl.gates import _acparse, _astscan, _lib, _toolchain

IDS = ["AC-A-1", "AC-A-2", "AC-A-10"]


class AcParse(unittest.TestCase):
    def v(self, text, ids=IDS):
        p = _acparse.parse(text, ids)
        return {a: r["verdict"] for a, r in p.results.items()}

    def test_unittest_verbose_lines(self):
        out = ("test_ac_a_1_adds (tests.acceptance.test_a.T) ... ok\n"
               "test_ac_a_2_subtracts (tests.acceptance.test_a.T) ... FAIL\n"
               "test_ac_a_10_other (tests.acceptance.test_a.T) ... ERROR\n")
        self.assertEqual(self.v(out), {"AC-A-1": "pass", "AC-A-2": "fail", "AC-A-10": "error"})

    def test_unittest_docstring_second_line_is_joined(self):
        out = "test_one (tests.t.T)\nAC-A-1 adds two numbers ... ok\n"
        self.assertEqual(self.v(out), {"AC-A-1": "pass"})

    def test_ids_are_separator_bounded(self):
        # AC-A-1 must not match AC-A-10, and AC-A-1 must not match AC-A1-1 or AC-AA-1
        self.assertEqual(self.v("test_ac_a_10_x (m.T) ... ok\n", ["AC-A-1", "AC-A-10"]), {"AC-A-10": "pass"})
        self.assertEqual(self.v("test_ac_a1_1_x (m.T) ... ok\ntest_ac_aa_1 (m.T) ... ok\n", ["AC-A-1"]), {})

    def test_skips_never_count_as_a_pass(self):
        out = "test_ac_a_1 (m.T) ... skipped 'later'\ntest_ac_a_2 (m.T) ... expected failure\n"
        self.assertEqual(self.v(out), {"AC-A-1": "skipped", "AC-A-2": "skipped"})

    def test_a_failing_test_beats_a_passing_one_for_the_same_criterion(self):
        out = "test_ac_a_1_one (m.T) ... ok\ntest_ac_a_1_two (m.T) ... FAIL\n"
        self.assertEqual(self.v(out), {"AC-A-1": "fail"})
        self.assertEqual(self.v("test_ac_a_1_one (m.T) ... skipped 'x'\ntest_ac_a_1_two (m.T) ... ok\n"), {"AC-A-1": "pass"})

    def test_pytest_verbose_and_summary_lines(self):
        out = ("tests/test_a.py::test_ac_a_1_adds PASSED\n"
               "tests/test_a.py::test_ac_a_2_x FAILED\n"
               "FAILED tests/test_a.py::test_ac_a_2_x - AssertionError: assert 1 == 2\n")
        self.assertEqual(self.v(out), {"AC-A-1": "pass", "AC-A-2": "fail"})
        err = "FAILED tests/test_a.py::test_ac_a_1 - ImportError: cannot import name 'calc'\n"
        self.assertEqual(self.v(err), {"AC-A-1": "error"})

    def test_go_tap_jest_and_cargo(self):
        self.assertEqual(self.v("--- PASS: TestAC_A_1 (0.00s)\n--- FAIL: TestAC_A_2 (0.01s)\n--- SKIP: TestAC_A_10 (0.00s)\n"),
                         {"AC-A-1": "pass", "AC-A-2": "fail", "AC-A-10": "skipped"})
        self.assertEqual(self.v("ok 1 - AC-A-1 adds\nnot ok 2 - AC-A-2 subtracts\nok 3 - AC-A-10 later # SKIP no\n"),
                         {"AC-A-1": "pass", "AC-A-2": "fail", "AC-A-10": "skipped"})
        self.assertEqual(self.v("  ✓ AC-A-1 adds (3 ms)\n  ✕ AC-A-2 subtracts (4 ms)\n"), {"AC-A-1": "pass", "AC-A-2": "fail"})
        self.assertEqual(self.v("test ac_a_1 ... ok\ntest ac_a_2 ... FAILED\ntest ac_a_10 ... ignored\n"),
                         {"AC-A-1": "pass", "AC-A-2": "fail", "AC-A-10": "skipped"})

    def test_a_raising_stub_is_red_but_other_errors_are_invalid(self):
        out = """test_ac_a_1_adds (tests.t.T.test_ac_a_1_adds) ... ERROR
test_ac_a_2_subs (tests.t.T.test_ac_a_2_subs) ... ERROR
test_ac_a_10_x (tests.t.T.test_ac_a_10_x) ... FAIL

======================================================================
ERROR: test_ac_a_1_adds (tests.t.T.test_ac_a_1_adds)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "t.py", line 5, in test_ac_a_1_adds
    calc.add(1, 2)
  File "calc.py", line 2, in add
    raise NotImplementedError
NotImplementedError

======================================================================
ERROR: test_ac_a_2_subs (tests.t.T.test_ac_a_2_subs)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "t.py", line 8, in test_ac_a_2_subs
    calc.sub(1, 2)
AttributeError: module 'calc' has no attribute 'sub'

======================================================================
FAIL: test_ac_a_10_x (tests.t.T.test_ac_a_10_x)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "t.py", line 11, in test_ac_a_10_x
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 3 tests in 0.001s
"""
        self.assertEqual(self.v(out), {"AC-A-1": "fail", "AC-A-2": "error", "AC-A-10": "fail"})
        self.assertEqual(self.v("FAILED tests/test_a.py::test_ac_a_1 - NotImplementedError\n"), {"AC-A-1": "fail"})

    def test_collection_errors_are_invalid_not_red(self):
        out = ("ERROR collecting tests/test_a.py\nImportError while importing test module\n"
               "tests/test_a.py::test_ac_a_1 FAILED\n")
        p = _acparse.parse(out, IDS)
        self.assertTrue(p.invalid)
        self.assertEqual(p.verdict("AC-A-1"), "error")
        # ... but a real assertion anywhere in the output keeps the red
        out2 = out + "E   AssertionError: 1 != 2\n"
        self.assertEqual(_acparse.parse(out2, IDS).verdict("AC-A-1"), "fail")

    def test_json_results_flat_and_per_item(self):
        flat = '{"results": {"AC-A-1": "pass", "AC-A-2": "fail", "AC-Z-9": "pass"}}'
        self.assertEqual(self.v(flat), {"AC-A-1": "pass", "AC-A-2": "fail"})
        nested = 'noise\n{\n "failed": 1,\n "results": {"A": {"AC-A-1": "pass", "AC-A-2": "red"}}\n}\nmore'
        self.assertEqual(self.v(nested), {"AC-A-1": "pass", "AC-A-2": "fail"})
        self.assertEqual(_acparse.parse(flat, IDS).source, "json")

    def test_a_harness_that_prints_a_failure_message_instead_of_a_verdict_word_has_failed(self):
        text = '{"failed": 2, "results": {"A": {"AC-A-1": "pass", "AC-A-2": "expected 201, got 500: boom", "AC-A-10": "passed 3 of 5 checks"}}}'
        self.assertEqual(self.v(text), {"AC-A-1": "pass", "AC-A-2": "fail", "AC-A-10": "fail"})

    def test_json_garbage_falls_back_to_text(self):
        self.assertEqual(self.v('{"results": nope}\ntest_ac_a_1 (m.T) ... ok\n'), {"AC-A-1": "pass"})

    def test_junit(self):
        xml = ('<testsuite><testcase classname="t.T" name="test_ac_a_1"/>'
               '<testcase classname="t.T" name="test_ac_a_2"><failure type="AssertionError" message="assert 1 == 2"/></testcase>'
               '<testcase classname="t.T" name="test_ac_a_10"><error type="ImportError" message="x"/></testcase></testsuite>')
        p = _acparse.parse_junit(xml, IDS)
        self.assertEqual({a: r["verdict"] for a, r in p.results.items()}, {"AC-A-1": "pass", "AC-A-2": "fail", "AC-A-10": "error"})
        self.assertIsNone(_acparse.parse_junit("<!DOCTYPE x [<!ENTITY a 'b'>]><x/>", IDS))
        self.assertIsNone(_acparse.parse_junit("not xml", IDS))
        self.assertIsNone(_acparse.parse_junit("<a>" + "x" * (3 * 1024 * 1024) + "</a>", IDS))

    def test_allowed_restricts_lines_to_the_pinned_tests_and_their_modules(self):
        allowed = _acparse.Allowed({"test_ac_a_1_adds"}, {"tests.acceptance.test_a", "tests/acceptance/test_a.py"})
        ok = "test_ac_a_1_adds (tests.acceptance.test_a.TestAcceptance.test_ac_a_1_adds) ... ok\n"
        twin = "test_ac_a_1_adds (tests.unit.test_twin.TestAcceptance.test_ac_a_1_adds) ... ok\n"
        docstring = "test_trivial (tests.unit.test_mine.Mine.test_trivial)\nAC-A-1 is covered ... ok\n"
        pytest_ok = "tests/acceptance/test_a.py::TestAcceptance::test_ac_a_1_adds PASSED\n"
        pytest_twin = "tests/unit/test_twin.py::TestAcceptance::test_ac_a_1_adds PASSED\n"
        bare = "test_ac_a_1_adds ... ok\n"            # a runner that prints no module: the name alone has to do
        for text, want in ((ok, {"AC-A-1": "pass"}), (twin, {}), (docstring, {}), (pytest_ok, {"AC-A-1": "pass"}), (pytest_twin, {}), (bare, {"AC-A-1": "pass"})):
            p = _acparse.parse(text, ["AC-A-1"], allowed)
            self.assertEqual({a: r["verdict"] for a, r in p.results.items()}, want, text)
        self.assertEqual(self.v(twin, ["AC-A-1"]), {"AC-A-1": "pass"})          # without the filter every line counts

    def test_a_line_without_a_status_is_ignored(self):
        self.assertEqual(self.v("running AC-A-1 now\n"), {})


class AstScan(unittest.TestCase):
    def fns(self, src, ids=("AC-A-1", "AC-A-2")):
        pats = {a: _lib.ac_pattern(a) for a in ids}
        return {f["name"]: f for f in _astscan.py_test_functions(src, pats)}

    def test_real_and_fake_assertions(self):
        f = self.fns('''
import unittest
class T(unittest.TestCase):
    def test_ac_a_1(self):
        self.assertEqual(f(), 3)
    def test_ac_a_2_true(self):
        assert True
    def test_ac_a_1_const(self):
        self.assertEqual(1, 1)
    def test_ac_a_1_bare(self):
        with self.assertRaises(Exception):
            f()
    def test_ac_a_1_typed(self):
        with self.assertRaises(ValueError):
            f()
    def test_ac_a_1_nothing(self):
        f()
    def test_ac_a_1_helper(self):
        self.check_response(f())
    def test_ac_a_1_istrue(self):
        self.assertTrue(True)
    def test_ac_a_1_pytest_raises(self):
        with pytest.raises(Exception):
            f()
''')
        self.assertEqual((f["T.test_ac_a_1"]["real"], f["T.test_ac_a_1"]["fake"]), (1, 0))
        self.assertEqual(f["T.test_ac_a_2_true"]["fake"], 1)
        self.assertEqual(f["T.test_ac_a_1_const"]["fake"], 1)
        self.assertEqual(f["T.test_ac_a_1_bare"]["fake"], 1)
        self.assertEqual((f["T.test_ac_a_1_typed"]["real"], f["T.test_ac_a_1_typed"]["fake"]), (1, 0))
        self.assertEqual((f["T.test_ac_a_1_nothing"]["real"], f["T.test_ac_a_1_nothing"]["fake"]), (0, 0))
        self.assertTrue(f["T.test_ac_a_1_helper"]["helper"])
        self.assertEqual(f["T.test_ac_a_1_istrue"]["fake"], 1)
        self.assertEqual(f["T.test_ac_a_1_pytest_raises"]["fake"], 1)

    def test_an_ac_id_in_the_class_name_or_docstring_counts(self):
        f = self.fns('''
class TestAC_A_1:
    def test_x(self):
        assert 1
class Other:
    def test_doc(self):
        """AC-A-2: documented"""
        assert f()
''')
        self.assertEqual(f["TestAC_A_1.test_x"]["acs"], ["AC-A-1"])
        self.assertEqual(f["Other.test_doc"]["acs"], ["AC-A-2"])

    def test_skip_markers_in_python(self):
        src = '''
import unittest, pytest
@unittest.skip("x")
def test_a(): pass
@pytest.mark.skipif(True, reason="y")
def test_b(): pass
@pytest.mark.xfail
def test_c(): pass
def test_d(self):
    self.skipTest("z")
def test_e():
    pytest.skip("w")
pytestmark = pytest.mark.skip
class TestK:
    @unittest.expectedFailure
    def test_f(self): pass
'''
        self.assertEqual(len(_astscan.skip_markers("t.py", src)), 7)
        self.assertEqual(_astscan.skip_markers("t.py", "def test_ok():\n    assert 1\n"), [])

    def test_a_skipped_class_marks_its_methods_skipped(self):
        f = self.fns('''
import unittest
@unittest.skip("whole class")
class T(unittest.TestCase):
    def test_ac_a_1(self):
        self.assertEqual(1, f())
''')
        self.assertTrue(f["T.test_ac_a_1"]["skipped"])

    def test_syntax_error_falls_back_and_still_counts(self):
        self.assertIsNone(_astscan.py_test_functions("def test_(:\n", {}))
        self.assertEqual(len(_astscan.skip_markers("t.py", "def test_(:\n  @unittest.skip('x')\n")), 1)

    def test_javascript_markers_names_and_assertions(self):
        src = """
describe.skip('x', () => {});
it.only('runs alone', () => { expect(1 + 1).toBe(2); });
xit('later', () => {});
test('AC-A-1 adds', () => { expect(true).toBe(true); });
// it.skip('commented out')
"""
        self.assertEqual(len(_astscan.skip_markers("t.test.js", src)), 3)
        self.assertEqual(_astscan.test_names("t.test.js", src), {"runs alone", "AC-A-1 adds"})
        self.assertEqual(_astscan.js_assertion_problems(src)["fake"], 1)

    def test_names_in_other_languages(self):
        self.assertEqual(_astscan.test_names("a_test.go", "func TestOne(t *testing.T) {}\nfunc helper() {}\n"), {"TestOne"})
        self.assertEqual(_astscan.test_names("a.rs", "#[test]\nfn adds() {}\n"), {"adds"})
        self.assertEqual(_astscan.test_names("A.java", "@Test\npublic void adds() {}\n"), {"adds"})
        self.assertEqual(_astscan.test_names("a.txt", "test_x"), set())

    def test_env_detection_mocks_and_snapshot_flags(self):
        self.assertEqual(_astscan.env_detection_count("if os.environ.get('PYTEST_CURRENT_TEST'): x()\nif process.env.JEST_WORKER_ID {}"), 2)
        self.assertEqual(_astscan.env_detection_count("if NODE_ENV === 'test' {}"), 1)
        self.assertEqual(_astscan.env_detection_count("NODE_ENV === 'production'"), 0)
        t = _astscan.mock_targets("tests/test_x.py", "with patch('hello.handlers.bookmarks.create'):\n  pass\n")
        self.assertIn("hello/handlers/bookmarks.py", t)
        self.assertIn("hello/handlers/bookmarks/create.py", t)
        js = _astscan.mock_targets("web/src/a.test.ts", "jest.mock('../lib/api')\n")
        self.assertIn("web/lib/api.ts", js)
        self.assertEqual(_astscan.update_snapshot_flags("jest -u\nvitest run --update-snapshots\nUPDATE_SNAPSHOTS=1"), 3)
        self.assertEqual(_astscan.update_snapshot_flags("jest --ci"), 0)


class Toolchain(unittest.TestCase):
    def test_satisfies(self):
        s = _toolchain.satisfies
        self.assertTrue(s("v20.11.1", "20"))
        self.assertTrue(s("v20.11.1", "20.11"))
        self.assertFalse(s("v20.11.1", "20.12"))
        self.assertFalse(s("v25.0.0", "20"))
        self.assertTrue(s("Python 3.12.4", ">=3.9"))
        self.assertFalse(s("Python 3.8.1", ">=3.9"))
        self.assertTrue(s("v18.2.0", "^18.0.0"))
        self.assertFalse(s("v19.0.0", "^18.0.0"))
        self.assertTrue(s("v20.3.0", ">=18 <22"))
        self.assertFalse(s("v22.1.0", ">=18 <22"))
        self.assertTrue(s("v22.1.0", "20.x || 22.x"))
        self.assertFalse(s("v23.1.0", "20.x || 22.x"))
        self.assertTrue(s("v20.5.0", "~20.5.0"))
        self.assertFalse(s("v20.6.0", "~20.5.0"))
        self.assertIsNone(s("v20.0.0", "lts/*"))
        self.assertIsNone(s("not a version", "20"))

    def test_pins_are_read_from_the_usual_files(self):
        files = {".nvmrc": "v20.11.0\n", ".python-version": "3.12\n", ".tool-versions": "nodejs 18.1.0\n# c\nruby 3.2.2\nterraform 1.5.0\n",
                 "package.json": '{"engines": {"node": ">=18"}}', "pyproject.toml": '[project]\nrequires-python = ">=3.9"\n'}
        pins = _toolchain.read_pins(files.get)
        got = sorted((p.tool, p.spec) for p in pins)
        self.assertEqual(got, [("node", "18.1.0"), ("node", "20.11.0"), ("node", ">=18"), ("python", "3.12"), ("python", ">=3.9"), ("ruby", "3.2.2")])

    def test_check_reports_mismatch_missing_tool_and_unparsed_pins(self):
        class P(object):
            def __init__(self, out, rc=0):
                self.stdout, self.returncode = out, rc
        def fake(argv, **kw):
            if argv[0] == "node":
                return P(b"v25.1.0\n")
            raise OSError("no such tool")
        rows = _toolchain.check([_toolchain.Pin("node", "20", ".nvmrc"), _toolchain.Pin("ruby", "3.2", ".ruby-version"), _toolchain.Pin("node", "lts/*", "x")], {}, fake)
        self.assertEqual([r["level"] for r in rows], ["error", "error", "info"])
        self.assertIn("pinned node 20 but the installed version is v25.1.0", rows[0]["message"])
        self.assertIn("not installed", rows[1]["message"])


class PlanInputs(unittest.TestCase):
    def test_g3_spec_defaults_and_problems(self):
        spec, probs = _lib.g3_spec(item_doc("A", verify={"tests": "pytest -v"}))
        self.assertEqual((spec["declared"], spec["cmd"], spec["class"], spec["visible"]), (False, "pytest -v", "behaviour", []))
        self.assertEqual(probs, [])
        spec, probs = _lib.g3_spec(item_doc("A", ext={"g3": {"visible": ["tests/**", "../etc/**"], "class": "weird", "bogus": 1, "exempt": " "}}))
        self.assertEqual(sorted(p["id"] for p in probs), ["g3.ext-class", "g3.ext-exempt", "g3.ext-unknown-key", "g3.ext-unsafe-path"])
        self.assertEqual(spec["visible"], ["tests/**"])
        self.assertEqual(spec["class"], "behaviour")

    def test_g5_allow_and_footprint_drop_unsafe_globs(self):
        it = item_doc("A", ext={"g5": {"allow": ["package.json", "/etc/passwd", "**"]}}, touches={"paths": ["src/**", "../x"]})
        self.assertEqual(_lib.g5_allow(it), ["package.json"])
        self.assertEqual(_lib.footprint(it), ["src/**"])

    def test_path_sets(self):
        for p in ("tests/a.py", "src/__tests__/x.js", "a/b/c.test.ts", "conftest.py", "pkg/conftest.py", ".github/workflows/ci.yml", "jest.config.js",
                  "web/vitest.config.ts", ".env.local", "pkg/__snapshots__/a.snap", "pyproject.toml", "package.json", "Makefile"):
            self.assertTrue(_lib.is_protected(p), p)
        for p in ("src/app.py", "README.md", "docs/a.md", "lib/contest.py"):
            self.assertFalse(_lib.is_protected(p), p)
        self.assertTrue(_lib.is_test_path("tests/x.py"))
        self.assertFalse(_lib.is_test_path("conftest.py"))

    def test_ac_pattern(self):
        rx = _lib.ac_pattern("AC-F2-1")
        self.assertTrue(rx.search("test_ac_f2_1_ok"))
        self.assertTrue(rx.search("AC F2 1"))
        self.assertFalse(rx.search("AC-F2-10"))
        self.assertFalse(rx.search("AC-F21-1"))
        self.assertFalse(_lib.ac_pattern("nonsense").search("anything"))


if __name__ == "__main__":
    unittest.main()
