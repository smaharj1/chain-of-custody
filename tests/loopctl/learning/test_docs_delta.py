"""The Docs Delta gate (report 19.7 F-30, 16.1 row 10): the pure judgement, and the gate against real commits.

The adversarial question is the one the brief asks: can the gate be satisfied by an empty or whitespace-only docs change? It must not be. A
documentation change counts only when it says the thing (the new name in an added line of prose); a blank line, whitespace, a reflow,
punctuation, a deletion, a file under the feature's scratch folder or a test does not, and neither does a Doc Delta that is missing, blank, NONE with no
`checked:` line, or too short to say anything.
"""
from __future__ import annotations

import unittest

from .. import item_doc
from ..fault_injection import report_text
from ..gates import GateCase

from loopctl import gates
from loopctl.learning import docsdelta as dd


def diff(**files):
    """{path: (added lines, removed lines)} -> parse_diff output."""
    return {p: {"added": list(a), "removed": list(r)} for p, (a, r) in files.items()}


ENV_ADD = "key = os.environ['PAYMENTS_API_KEY']"
NONE_CHECKED = {"name": "backend", "present": True, "text": "NONE\nchecked: README, env vars", "declared": False, "checked": True}
BLANK = {"name": "backend", "present": False, "text": "", "declared": False, "checked": False}
NONE_BARE = {"name": "backend", "present": True, "text": "NONE", "declared": False, "checked": False}
DECLARED = {"name": "backend", "present": True, "declared": True, "checked": True,
            "text": "README needs a section describing PAYMENTS_API_KEY, the key for the payments provider, with a dev default"}
SHORT = {"name": "backend", "present": True, "declared": True, "checked": False, "text": "PAYMENTS_API_KEY README"}


def ids(res, severity=None):
    return [f["id"] for f in res["findings"] if severity is None or f["severity"] == severity]


class Words(unittest.TestCase):
    def test_a_change_that_says_nothing_adds_no_words(self):
        n = dd.net_new_words
        self.assertEqual(n(["", "   ", "\t"], []), 0)
        self.assertEqual(n(["...", "---", "# "], []), 0)                                    # punctuation and a bare heading mark
        self.assertEqual(n(["The  quick brown   fox"], ["The quick brown fox"]), 0)        # a reflow
        self.assertEqual(n(["THE QUICK BROWN FOX"], ["the quick brown fox"]), 0)           # a case change
        self.assertEqual(n([], ["a lot of words that were deleted"]), 0)
        self.assertEqual(n(["set PAYMENTS_API_KEY to the key your provider gave you"], []), 9)

    def test_parse_diff_reads_unified_zero_output(self):
        text = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,0 +2,2 @@\n+hello world\n+\n"
                "diff --git a/old.md b/old.md\ndeleted file mode 100644\n--- a/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
                "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n")
        d = dd.parse_diff(text)
        self.assertEqual(d, {"README.md": {"added": ["hello world", ""], "removed": []}})

    def test_what_counts_as_documentation_and_as_source(self):
        for p in ("README.md", "docs/api.md", "docs/guide/setup.rst", "CHANGELOG.md", "packages/x/README.md", ".env.example", "api/openapi.yaml"):
            self.assertTrue(dd.is_doc_path(p), p)
        for p in ("docs/features/slice-a/reports/backend.md", "docs/features/x/briefs/db.md", ".claude/context/backend.md", "plan/items/A.json", "tests/notes.md",
                  "src/app.py", "requirements.txt", "node_modules/x/README.md", "docs/baseline/evidence.json"):
            self.assertFalse(dd.is_doc_path(p), p)
        self.assertTrue(dd.is_code_path("src/app.py"))
        for p in ("README.md", "tests/test_app.py", "package-lock.json", "src/app.test.js", "config/app.yaml", ".claude/scripts/x.py", "docs/features/a/reports/b.md"):
            self.assertFalse(dd.is_code_path(p), p)


class Triggers(unittest.TestCase):
    def names(self, **files):
        return sorted((t["kind"], t["name"]) for t in dd.extract_triggers(diff(**files)))

    def test_new_variables_routes_and_flags_in_source(self):
        got = self.names(**{
            "src/app.py": ([ENV_ADD, "port = os.getenv('APP_PORT')", "@app.get('/api/tasks/<id>')", "p.add_argument('--dry-run')"], []),
            "web/server.js": (["const k = process.env.SESSION_SECRET;", "app.post('/login', h)", "program.option('-v, --verbose-mode')"], []),
            "svc/main.go": (["http.HandleFunc(\"/metrics\", h)"], [])})
        self.assertEqual(got, [("env", "APP_PORT"), ("env", "PAYMENTS_API_KEY"), ("env", "SESSION_SECRET"), ("flag", "--dry-run"), ("flag", "--verbose-mode"),
                               ("route", "/api/tasks"), ("route", "/login")])

    def test_a_moved_line_a_test_a_doc_and_an_ordinary_variable_are_not_triggers(self):
        self.assertEqual(self.names(**{"src/a.py": ([ENV_ADD], [ENV_ADD])}), [])                              # the same line removed: not new
        self.assertEqual(self.names(**{"tests/test_a.py": ([ENV_ADD], [])}), [])
        self.assertEqual(self.names(**{"README.md": ([ENV_ADD], [])}), [])
        self.assertEqual(self.names(**{"src/a.py": (["os.environ['HOME']", "os.getenv('PATH')", "os.environ.get('lowercase_name')"], [])}), [])
        self.assertEqual(self.names(**{"docs/features/a/reports/b.md": ([ENV_ADD], [])}), [])

    def test_documented_needs_the_name_in_an_added_line_of_prose_or_an_env_example(self):
        d = dd.documented_in
        self.assertEqual(d("PAYMENTS_API_KEY", diff(**{"README.md": (["Set `PAYMENTS_API_KEY` to your provider key."], [])})), "README.md")
        self.assertIsNone(d("PAYMENTS_API_KEY", diff(**{"README.md": (["PAYMENTS_API_KEY"], [])})))               # a bare mention says nothing
        self.assertIsNone(d("PAYMENTS_API_KEY", diff(**{"README.md": ([], ["Set PAYMENTS_API_KEY to your provider key."])})))      # removed, not added
        self.assertEqual(d("PAYMENTS_API_KEY", diff(**{".env.example": (["PAYMENTS_API_KEY="], [])})), ".env.example")
        self.assertIsNone(d("PAYMENTS_API_KEY", diff(**{".env.example": (["OTHER_KEY="], [])})))
        self.assertIsNone(d("/api/tasks", diff(**{".env.example": (["/api/tasks"], [])}), "route"))                 # an env example documents variables only

    def test_parse_delta(self):
        self.assertEqual(dd.parse_delta("## Doc Delta\nNONE\nchecked: README\n\n## Verification\n- x")["declared"], False)
        self.assertEqual(dd.parse_delta("## Doc Delta\n\n   \n\n## Verification\n")["present"], False)
        self.assertEqual(dd.parse_delta("# R\n## Status\nAPPROVED\n")["present"], False)
        d = dd.parse_delta("## Doc Delta\nREADME: add PAYMENTS_API_KEY\nchecked: README\n")
        self.assertEqual((d["present"], d["declared"], d["checked"]), (True, True, True))
        self.assertTrue(dd.parse_delta("## Doc Delta\nnone.\n")["present"])


class Evaluate(unittest.TestCase):
    CODE = {"src/app.py": ([ENV_ADD], [])}

    def run_(self, deltas, **docs):
        files = dict(self.CODE)
        files.update(docs)
        return dd.evaluate(diff(**files), deltas, 5)

    def test_the_diff_adds_a_variable_and_nothing_says_so(self):
        res = self.run_([NONE_CHECKED])
        self.assertEqual(ids(res, "error"), ["docs.undeclared"])

    def test_empty_and_whitespace_only_docs_changes_do_not_satisfy_it(self):
        attempts = {
            "a blank line": {"README.md": (["", ""], [])},
            "spaces and tabs": {"README.md": (["   ", "\t", "    "], [])},
            "punctuation": {"README.md": (["...", "---", "***"], [])},
            "a reflowed paragraph": {"README.md": (["Set the  key  you got"], ["Set the key you got"])},
            "only deletions": {"README.md": ([], ["PAYMENTS_API_KEY is the key for the payments provider"])},
            "the name in a removed line only": {"README.md": (["Nothing to see here at all"], ["PAYMENTS_API_KEY is the key for the payments provider"])},
            "a bare name": {"README.md": (["PAYMENTS_API_KEY"], [])},
            "unrelated words": {"README.md": (["Thanks for reading this readme, it is a good one"], [])},
            "in the feature's scratch folder": {"docs/features/slice-a/reports/backend.md": (["Set PAYMENTS_API_KEY to the key from your provider"], [])},
            "in a test directory": {"tests/notes.md": (["Set PAYMENTS_API_KEY to the key from your provider"], [])},
            "in the kit's own files": {".claude/context/backend.md": (["Set PAYMENTS_API_KEY to the key from your provider"], [])},
            "in a code comment": {"src/other.py": (["# Set PAYMENTS_API_KEY to the key from your provider"], [])},
            "an env example for another variable": {".env.example": (["OTHER_KEY="], [])},
        }
        for name, docs in attempts.items():
            with self.subTest(name):
                res = self.run_([NONE_CHECKED], **docs)
                self.assertIn("docs.undeclared", ids(res, "error"), name)

    def test_a_missing_blank_or_unchecked_delta_is_no_answer(self):
        for name, delta in (("blank", BLANK), ("NONE without checked", NONE_BARE), ("too short to say anything", SHORT)):
            with self.subTest(name):
                res = self.run_([delta])
                self.assertIn("docs.undeclared", ids(res, "error"))
        self.assertIn("docs.no-delta", ids(self.run_([BLANK]), "error"))
        self.assertIn("docs.unchecked", ids(self.run_([NONE_BARE]), "error"))
        self.assertIn("docs.no-report", ids(self.run_([]), "error"))

    def test_a_substantive_documentation_change_that_names_it_passes(self):
        res = self.run_([NONE_CHECKED], **{"README.md": (["## Configuration", "", "Set `PAYMENTS_API_KEY` to the key your payments provider gave you."], [])})
        self.assertEqual(ids(res, "error"), [])
        self.assertEqual(res["documented"], 1)
        res = self.run_([NONE_CHECKED], **{".env.example": (["PAYMENTS_API_KEY="], [])})
        self.assertEqual(ids(res, "error"), [])

    def test_reported_for_follow_up_is_a_warning_not_a_pass_by_stealth(self):
        res = self.run_([DECLARED])
        self.assertEqual(ids(res, "error"), [])
        self.assertIn("docs.reported-not-done", ids(res, "warn"))
        res = self.run_([DECLARED], **{"README.md": (["", "  "], [])})                          # a whitespace edit changes nothing about that
        self.assertIn("docs.reported-not-done", ids(res, "warn"))
        self.assertIn("docs.insubstantial", ids(res, "warn"))

    def test_no_trigger_no_problem_and_a_declared_delta_with_no_docs_is_a_warning(self):
        res = dd.evaluate(diff(**{"src/app.py": (["x = 1"], [])}), [NONE_CHECKED], 5)
        self.assertEqual((ids(res), res["summary"]), ([], "0 trigger(s), 0 documented, 0 error(s)"))
        res = dd.evaluate(diff(**{"src/app.py": (["x = 1"], [])}), [DECLARED], 5)
        self.assertEqual(ids(res), ["docs.declared-not-done"])
        res = dd.evaluate(diff(**{"src/app.py": (["x = 1"], [])}), [BLANK], 5)
        self.assertEqual((ids(res, "error"), ids(res, "warn")), ([], []))                          # nothing to document: a blank section is not an error here

    def test_the_threshold_is_the_configured_one(self):
        d = diff(**{"src/a.py": (["x = 1"], []), "README.md": (["one two three four"], [])})
        self.assertIn("docs.insubstantial", ids(dd.evaluate(d, [NONE_CHECKED], 5)))
        self.assertNotIn("docs.insubstantial", ids(dd.evaluate(d, [NONE_CHECKED], 4)))


class TheGate(GateCase):
    """The gate against real commits: the report and the docs are read from the head commit, the diff is base..head."""
    ITEMS = [item_doc("A", acs=1)]
    BASE_FILES = {"src/app.py": "import os\n\n\ndef main():\n    return 1\n", "README.md": "# App\n\nA small app.\n"}
    APP = "import os\n\n\ndef main():\n    return os.environ['PAYMENTS_API_KEY']\n"
    REPORT = "docs/features/slice-a/reports/backend.md"

    def report(self, doc_delta):
        return report_text("APPROVED").replace("NONE\nchecked: README", doc_delta)

    def test_a_new_variable_with_no_documentation_fails_and_report_mode_does_not_block(self):
        self.commit({"src/app.py": self.APP, self.REPORT: self.report("NONE\nchecked: README")})
        res = self.run_gate("docs")
        self.assertFails(res, "docs.undeclared")
        self.assertEqual((res["mode"], res["blocking"]), ("report", False))
        self.assertEqual(res["evidence"]["triggers"], [{"kind": "env", "name": "PAYMENTS_API_KEY"}])

    def test_a_blank_line_in_the_readme_changes_nothing(self):
        self.commit({"src/app.py": self.APP, "README.md": "# App\n\nA small app.\n\n\n   \n", self.REPORT: self.report("NONE\nchecked: README")})
        res = self.run_gate("docs")
        self.assertFails(res, "docs.undeclared")
        self.assertIn("docs.insubstantial", self.ids(res))

    def test_a_documentation_change_that_names_it_passes(self):
        self.commit({"src/app.py": self.APP, "README.md": "# App\n\nA small app.\n\nSet `PAYMENTS_API_KEY` to the key your payments provider gave you.\n",
                     self.REPORT: self.report("README.md: documented PAYMENTS_API_KEY\nchecked: README, env vars")})
        res = self.run_gate("docs")
        self.assertPass(res)
        self.assertIn("docs.documented", self.ids(res))

    def test_an_uncommitted_readme_is_not_evidence(self):
        self.commit({"src/app.py": self.APP, self.REPORT: self.report("NONE\nchecked: README")})
        self.lr.repo.write("README.md", "# App\n\nSet `PAYMENTS_API_KEY` to the key your payments provider gave you.\n")      # edited, never committed
        self.assertFails(self.run_gate("docs"), "docs.undeclared")

    def test_the_report_in_the_feature_folder_is_not_documentation(self):
        self.commit({"src/app.py": self.APP, self.REPORT: self.report("NONE\nchecked: README") + "\nSet PAYMENTS_API_KEY to the key from your payments provider.\n"})
        self.assertFails(self.run_gate("docs"), "docs.undeclared")

    def test_reported_for_follow_up_passes_with_a_warning(self):
        self.commit({"src/app.py": self.APP, self.REPORT: self.report("README needs a section on PAYMENTS_API_KEY, the key for the payments provider\nchecked: README")})
        res = self.run_gate("docs")
        self.assertPass(res)
        self.assertIn("docs.reported-not-done", self.ids(res))

    def test_a_change_with_nothing_to_document_passes(self):
        self.commit({"src/app.py": "def main():\n    return 2\n", self.REPORT: self.report("NONE\nchecked: README")})
        self.assertPass(self.run_gate("docs"))

    def test_the_driver_runs_it_with_the_verify_gates_and_the_registry_knows_it(self):
        from loopctl import driver
        self.assertIn("docs", driver.VERIFY_GATES)
        self.assertIn("docs", gates.KNOWN_GATES)
        self.assertEqual(gates.get("docs").DEFAULT_MODE, "report")


class TheGateOn(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    CFG = "gates:\n  docs: on\ndocs_delta_min_words: 3\n"
    BASE_FILES = TheGate.BASE_FILES

    def test_on_it_blocks_and_the_threshold_comes_from_the_config(self):
        self.commit({"src/app.py": TheGate.APP, TheGate.REPORT: report_text("APPROVED").replace("NONE\nchecked: README", "NONE\nchecked: README")})
        res = self.run_gate("docs")
        self.assertEqual((res["status"], res["mode"], res["blocking"]), ("fail", "on", True))
        self.assertEqual(res["failure_class"], "item")
        self.assertEqual(res["findings"][0]["reason"], "doc_delta_missing")
        self.assertIn("docs", gates.required_for(self.ctx().cfg, "verifying", "reviewing"))


if __name__ == "__main__":
    unittest.main()
