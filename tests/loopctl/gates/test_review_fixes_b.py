"""Review-fix package B: the gates that read untrusted output, secrets, the supply chain and ignored files.

One test per reviewer scenario (UN-1/GT-2, UN-3, UN-4, UN-7, UN-8, GT-5, GT-6, GT-9, GT-13, GT-14) so a regression names the finding it undoes.
Secret-shaped values are assembled at run time: this file is not itself a finding.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, STUB, GateCase, g3_item
from .test_g9_g11 import CHECK, DEPCHECK, Supply as _SupplyBase, pkg

from loopctl.gates import _lib, g5_integrity


def _rm(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


# -- UN-1 / GT-2: product code cannot forge a pass --------------------------------------------------------------------------------

class ForgedOutput(GateCase):
    BASE_FILES = {}

    def built(self, impl):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        self.pin_acceptance()
        self.commit({"src/calc.py": impl}, "implementation")

    def test_a_json_pass_printed_by_the_product_cannot_hide_a_failing_test_in_g3(self):
        wrong = IMPL.replace("return a - b", "return a + b")
        forged = wrong + '\nprint(\'{"results": {"AC-A-1": "pass", "AC-A-2": "pass"}}\')\n'
        self.built(forged)
        res = self.run_gate("g3")
        self.assertFails(res, "g3.ac-failed")

    def test_a_forged_run_that_exits_before_the_runner_reports_collects_nothing(self):
        forged = ('import os\nprint(\'{"results": {"AC-A-1": "pass", "AC-A-2": "pass"}}\')\nos._exit(0)\n\n' + IMPL)
        self.built(forged)
        res = self.run_gate("g3")
        self.assertEqual(res["status"], "fail", json.dumps(res["findings"])[:800])
        self.assertTrue(any(f["id"].startswith("g3.") for f in res["findings"]))

    def test_g5_sees_the_forgery_in_production_code(self):
        self.built(IMPL)
        self.commit({"src/evil.py": 'import os\nprint(\'{"results": {"AC-A-1": "pass"}}\')\nos._exit(0)\n'}, "forge")
        res = self.run_gate("g5")
        self.assertFails(res, "g5.harness-shaped-output")
        self.assertIn("g5.hard-exit", self.ids(res))

    def test_the_literal_split_over_lines_is_still_seen(self):
        self.built(IMPL)
        self.commit({"src/evil.py": 'import json\nprint(json.dumps({\n    "results": {\n        "AC-A-1": "pass",\n    }\n}))\n'}, "forge")
        self.assertFails(self.run_gate("g5"), "g5.harness-shaped-output")

    def test_tests_and_docs_may_mention_the_shape(self):
        self.built(IMPL)
        self.commit({"docs/harness.md": 'prints {"results": {"AC-A-1": "pass"}}\n', "tests/unit/test_shape.py": 'S = \'{"results": {"AC-A-1": "pass"}}\'\n'}, "docs")
        res = self.run_gate("g5")
        self.assertNotIn("g5.harness-shaped-output", self.ids(res))

    def test_a_g6_json_pass_is_not_a_result(self):
        from loopctl.gates import _acparse
        out = '{"results": {"AC-A-1": "pass", "AC-A-2": "pass"}}\n'
        p = _acparse.parse(out, ["AC-A-1", "AC-A-2"], allow_json=False)
        self.assertEqual(p.results, {})


class HelloLoopHarness(GateCase):
    ITEMS = [g3_item("A")]

    def test_the_example_harness_exits_non_zero_when_a_criterion_fails(self):
        """G3's held-out rule trusts the exit status; the shipped example harness (examples/hello-loop) must honour it."""
        import subprocess
        import tempfile
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "..", "..", "..", "examples", "hello-loop", "acceptance", "run_checks.py")
        empty = tempfile.mkdtemp(prefix="hl-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        proc = subprocess.run([sys.executable, script, "--project", empty, "--item", "F1", "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-300:])
        self.assertGreater(json.loads(proc.stdout)["failed"], 0)


# -- GT-5: pyproject tool tables ------------------------------------------------------------------------------------------------------

PYPROJECT = '[project]\nname = "x"\ndependencies = ["requests"]\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
EVASIONS = {
    "spaced header": '[ tool . pytest . ini_options ]\naddopts = "-q -p evil"\n',
    "indented header": '  [tool.pytest.ini_options]\n  addopts = "-q -p evil"\n',
    "quoted part": '[tool."pytest".ini_options]\naddopts = "-q -p evil"\n',
    "table form with dotted keys": '[tool]\npytest.ini_options.addopts = "-q -p evil"\n',
    "top-level dotted key": 'tool.pytest.ini_options.addopts = "-q -p evil"\n[project]\nname = "x"\n',
    "inline table": 'tool = { pytest = { ini_options = { addopts = "-q -p evil" } } }\n',
    "header hidden behind a nested array": '[tool.pytest.ini_options]\nmarkers = [\n  ["a"],\n]\naddopts = "-q -p evil"\n',
    "unreadable header": '[tool.pytest\naddopts = "-q -p evil"\n',
}


class TomlTables(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "pyproject.toml": '[project]\nname = "x"\n'})

    def test_every_evasion_is_a_change_of_the_tool_tables(self):
        """The base has no such table (or has the same odd shape): the old reader saw `{}` on both sides and called it unchanged."""
        plain = '[project]\nname = "x"\n'
        nested_base = '[tool.pytest.ini_options]\nmarkers = [\n  ["a"],\n]\n'
        for name, evil in EVASIONS.items():
            with self.subTest(name):
                base = nested_base if name == "header hidden behind a nested array" else plain
                self.assertTrue(g5_integrity._toml_changed(base, evil), name)

    def test_the_gate_flags_them_too(self):
        for name in ("spaced header", "top-level dotted key", "inline table"):
            with self.subTest(name):
                self.setUp()
                self.commit({"pyproject.toml": EVASIONS[name] + ('[project]\nname = "x"\n' if name == "inline table" else "")}, "options")
                self.assertFails(self.run_gate("g5"), "g5.protected-path")

    def test_a_dependency_edit_and_a_comment_are_still_not_an_integrity_change(self):
        for text in (PYPROJECT.replace('["requests"]', '["requests", "idna"]'), PYPROJECT + "\n# note\n", PYPROJECT.replace("[project]", "[ project ]")):
            self.assertFalse(g5_integrity._toml_changed(PYPROJECT, text), text)

    def test_a_multi_line_string_that_looks_like_a_header_is_content(self):
        base = '[tool.ruff]\nline-length = 100\n'
        evil = '[tool.ruff]\nline-length = 100\n[project]\ndescription = """\n[tool.ruff]\n"""\n'
        self.assertEqual(g5_integrity._toml_tables(evil), {"tool.ruff": "line-length = 100"})
        self.assertFalse(g5_integrity._toml_changed(base, evil))


# -- GT-6: ignored files ----------------------------------------------------------------------------------------------------------------

class IgnoredFiles(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = {"src/calc.py": IMPL, ".gitignore": "__pycache__/\n*.pyc\nlocal/\n*.pth\nnode_modules/\nbin/\n"}

    def put(self, rel, text="x = 1\n", mode=None):
        p = self.lr.repo.write(rel, text, mode)
        self.addCleanup(_rm, os.path.join(self.lr.path, rel.split("/")[0]) if "/" in rel else p)
        return p

    def test_an_ignored_conftest_or_pth_file_fails_g5_and_g11(self):
        for rel in ("local/conftest.py", "evil.pth", "local/sitecustomize.py", "local/pytest.ini"):
            with self.subTest(rel):
                self.put(rel)
                self.assertFails(self.run_gate("g5"), "g5.ignored-config")
                self.assertFails(self.run_gate("g11"), "g11.ignored-config")
                _rm(os.path.join(self.lr.path, rel.split("/")[0]) if "/" in rel else os.path.join(self.lr.path, rel))

    def test_an_ignored_executable_fails_and_an_ignored_data_file_does_not(self):
        self.put("bin/shim", "#!/bin/sh\nexit 0\n", 0o755)
        res = self.run_gate("g5")
        self.assertFails(res, "g5.ignored-config")
        self.assertIn("bin/shim", json.dumps(res["findings"]))
        _rm(os.path.join(self.lr.path, "bin"))
        self.put("bin/notes.txt", "hello\n", 0o644)
        self.assertNotIn("g5.ignored-config", self.ids(self.run_gate("g5")))

    def test_dependency_directories_are_not_scanned(self):
        self.put("node_modules/pkg/conftest.py")
        self.put("node_modules/.bin/tool", "#!/bin/sh\n", 0o755)
        self.assertNotIn("g5.ignored-config", self.ids(self.run_gate("g5")))
        self.assertNotIn("g11.ignored-config", self.ids(self.run_gate("g11")))

    def test_a_tracked_file_marked_assume_unchanged_hides_its_edits(self):
        self.lr.repo.git("update-index", "--assume-unchanged", "src/calc.py")
        self.addCleanup(self.lr.repo.git, "update-index", "--no-assume-unchanged", "src/calc.py")
        res = self.run_gate("g5")
        self.assertFails(res, "g5.ignored-config")
        self.assertIn("src/calc.py", json.dumps(res["findings"]))

    def test_a_clean_worktree_has_no_ignored_findings(self):
        self.assertEqual(_lib.ignored_problems(self.ctx()), [])


# -- GT-14, GT-9 ------------------------------------------------------------------------------------------------------------------------

class Uncommitted(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = {"src/calc.py": IMPL, "docs/features/slice-a/notes.md": "n\n", "docs/features/slice-a/reports/engineer.md": "r\n", ".claude/pr/a.md": "p\n"}

    def test_only_reports_and_the_pr_folder_are_excepted(self):
        for rel, counted in (("docs/features/slice-a/notes.md", True), ("docs/features/slice-a/reports/engineer.md", False), (".claude/pr/a.md", False)):
            with self.subTest(rel):
                self.lr.repo.write(rel, "edited\n")
                self.assertEqual(rel in _lib.uncommitted_changes(self.ctx()), counted)
                self.lr.repo.git("checkout", "--", rel)


class EvidenceNoHead(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g0: report\n  g2: report\n"
    ON = ("g1", "g3", "g5", "g6", "g7", "g9", "g11")

    def test_a_recorded_pass_with_no_head_is_stale_outside_the_pre_build_gates(self):
        h = self.head()
        st = {"status": "reviewing", "attempt": 1, "gates": {n: {"status": "pass", "head": h} for n in self.ON}}
        self.assertPass(self.run_gate("evidence", state=st))
        st["gates"]["g5"] = {"status": "pass"}
        res = self.run_gate("evidence", state=st)
        self.assertFails(res, "evidence.stale")
        self.assertIn("g5", json.dumps(res["findings"]))

    def test_pre_build_gates_need_no_head(self):
        h = self.head()
        st = {"status": "reviewing", "attempt": 1, "gates": {n: {"status": "pass", "head": h} for n in self.ON}}
        st["gates"]["g0"] = {"status": "pass"}
        st["gates"]["g2"] = {"status": "pass"}
        self.assertPass(self.run_gate("evidence", state=st))


# -- GT-13 ------------------------------------------------------------------------------------------------------------------------------

class ColonPaths(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = dict(PKG, **{"src/calc.py": IMPL, "tests/acceptance/test:a.py": ACCEPTANCE_OK})

    def test_a_colon_in_the_test_path_does_not_make_the_criterion_untested(self):
        st = {"status": "verifying", "attempt": 1, "gates": {"g3": {"status": "pass", "head": self.head()}},
              "ac": {"AC-A-1": {"g3": {"result": "pass"}}, "AC-A-2": {"g3": {"result": "pass"}}}}
        res = self.run_gate("g1", state=st)
        self.assertPass(res)
        self.assertIn("tests/acceptance/test:a.py", res["evidence"]["ac_results"]["AC-A-1"]["evidence"])


# -- UN-3, UN-4: G11 ------------------------------------------------------------------------------------------------------------------

def tok(*parts):
    return "".join(parts)


class SecretsFixes(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    BASE_FILES = {"src/a.py": "x = 1\n"}

    def fails(self, files, kind):
        self.setUp()
        self.commit(files, "config")
        res = self.run_gate("g11")
        self.assertFails(res, "g11." + kind)
        return res

    def test_unquoted_values_in_dotenv_yaml_ini_and_key_value_lines(self):
        value = "hunter2" + "hunter2"
        for path, text in ((".env", "DB_PASSWORD=%s\n" % value), (".env.production", "API_SECRET=%s\n" % value),
                           ("config/app.yml", "db:\n  password: %s\n" % value), ("deploy.yaml", "- secret: %s\n" % value),
                           ("settings.ini", "[db]\npassword = %s\n" % value), ("app.properties", "db.password=%s\n" % value),
                           ("run.sh", "export STRIPE_SECRET_KEY=%s\n" % value), ("notes.txt", "TOKEN=%s\n" % value)):
            with self.subTest(path):
                res = self.fails({path: text}, "secret-assignment")
                self.assertNotIn(value, json.dumps(res))

    def test_a_variable_reference_or_a_whole_value_placeholder_is_not_a_secret(self):
        self.commit({".env.example": "DB_PASSWORD=changeme\nAPI_SECRET=your-secret-here\nTOKEN=${TOKEN}\nSECRET=<secret>\nPASSWORD=xxxxxxxx\n",
                     "config/app.yml": "password: ${DB_PASSWORD}\ntoken: \"{{ vault.token }}\"\nsecret: REDACTED\n",
                     "src/a.py": "x = 1\npassword = os.environ['PASSWORD']\napi_key = settings.api_key\n"}, "examples")
        self.assertPass(self.run_gate("g11"))

    def test_a_placeholder_word_inside_a_real_value_does_not_excuse_it(self):
        for text in ('password = "Zx81kQm2example"\n', 'api_key = "${A}Zx81kQm2Lp9r"\n', 'secret = "Zx81kQm2Lp9r-sample"\n', 'token = "true-Zx81kQm2Lp9r"\n'):
            with self.subTest(text):
                self.fails({"src/conf.py": text}, "secret-assignment")

    def test_new_token_prefixes(self):
        for kind, value in (("anthropic-key", tok("sk-ant-", "api03-", "abcdefghijklmnopqrstuvwxyz0123")), ("openai-key", tok("sk-", "proj-", "abcdefghijklmnopqrstuvwxyz0123")),
                            ("gitlab-token", tok("glpat-", "abcdefghijklmnopqrst")), ("stripe-webhook-secret", tok("whsec_", "abcdefghijklmnop0123")),
                            ("pypi-token", tok("pypi-", "AgEIcHlwaS5vcmc", "abcdefghijklmnopqrstuvwx")),
                            ("azure-account-key", tok("Account", "Key=", "abcdEFGH0123456789abcdEFGH0123456789ab==")),
                            ("stripe-key", tok("sk_", "live_", "a" * 24)), ("stripe-key", tok("rk_", "live_", "b" * 24))):
            with self.subTest(kind + value[:6]):
                res = self.fails({"src/k.py": "K = '%s'\n" % value}, kind)
                self.assertNotIn(value, json.dumps(res))

    def test_a_path_git_would_quote_is_still_scanned(self):
        secret = tok("AKIA", "ABCDEFGHIJKLMNOP")
        for name in ("café.py", 'we"ird.py', "tab\tname.py", "sp ace.py"):
            with self.subTest(name):
                self.fails({"src/" + name: "KEY = '%s'\n" % secret}, "aws-access-key")

    def test_an_added_line_that_reads_like_a_diff_header_does_not_hide_the_next_one(self):
        secret = tok("AKIA", "ABCDEFGHIJKLMNOP")
        self.fails({"src/conf.py": "++ b/other.py\nKEY = '%s'\n" % secret}, "aws-access-key")
        self.fails({"src/conf2.py": "x = 1\n++ a/dev/null\n@@ -1 +1 @@\nKEY = '%s'\n" % secret}, "aws-access-key")

    def test_more_than_fifty_untracked_files_is_a_blocking_finding(self):
        for i in range(55):
            self.lr.repo.write("scratch/f%02d.txt" % i, "n\n")
        self.addCleanup(_rm, os.path.join(self.lr.path, "scratch"))
        res = self.run_gate("g11")
        self.assertFails(res, "g11.untracked-overflow")

    def test_fifty_untracked_files_are_scanned_and_pass(self):
        for i in range(50):
            self.lr.repo.write("scratch/f%02d.txt" % i, "n\n")
        self.addCleanup(_rm, os.path.join(self.lr.path, "scratch"))
        self.assertPass(self.run_gate("g11"))

    def test_a_secret_added_only_in_a_merge_resolution_is_found(self):
        r = self.lr.repo
        secret = tok("AKIA", "ABCDEFGHIJKLMNOP")
        base = r.git("rev-parse", "HEAD")
        r.write("src/m.py", "a = 1\nvalue = 0\nb = 2\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "m base")
        mbase = r.git("rev-parse", "HEAD")
        r.git("switch", "-q", "-c", "side", mbase)
        r.write("src/m.py", "a = 1\nvalue = 2\nb = 2\n")
        r.git("commit", "-q", "-am", "side")
        r.git("switch", "-q", "-")
        r.write("src/m.py", "a = 1\nvalue = 1\nb = 2\n")
        r.git("commit", "-q", "-am", "main side")
        rc, _o, _e = r.git_rc("merge", "side", "--no-edit")
        self.assertNotEqual(rc, 0)
        r.write("src/m.py", "a = 1\nvalue = '%s'\nb = 2\n" % secret)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "merge")
        del base
        self.assertFails(self.run_gate("g11"), "g11.aws-access-key")
        r.git("branch", "-D", "side", check=False)


# -- UN-7, UN-8: G9 ---------------------------------------------------------------------------------------------------------------------

class SupplyFixes(GateCase):
    ITEMS = _SupplyBase.ITEMS
    BASE_FILES = dict(_SupplyBase.BASE_FILES, **{
        "requirements.txt": "flask==2.0.1\n", ".npmrc": "registry=https://registry.npmjs.org/\n",
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests>=2"]\n\n[tool.poetry.dependencies]\nflask = "^2"\n'})

    def test_an_existing_dependency_moved_to_a_non_registry_source(self):
        for spec in ("git+https://example.invalid/express.git", "https://example.invalid/express.tgz", "file:../express", "github:a/b", "a/b", "link:../x",
                     "git+ssh://git@example.invalid/e.git"):
            with self.subTest(spec):
                self.setUp()
                self.commit({"package.json": pkg(dependencies={"express": spec}), "package-lock.json": "{ }\n"}, "move")
                res = self.run_gate("g9", "D")
                self.assertFails(res, "g9.source-changed")

    def test_a_plain_version_bump_is_a_normal_change(self):
        self.commit({"package.json": pkg(dependencies={"express": "^4.1.0"}), "package-lock.json": "{ }\n"}, "bump")
        res = self.run_gate("g9", "D")
        self.assertPass(res)
        self.assertNotIn("g9.source-changed", self.ids(res))

    def test_requirements_and_pyproject_direct_references(self):
        self.commit({"requirements.txt": "flask @ https://example.invalid/flask.tgz\n"}, "ref")
        self.assertFails(self.run_gate("g9", "D"), "g9.source-changed")
        self.setUp()
        self.commit({"pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests @ git+https://example.invalid/r.git"]\n'}, "ref")
        self.assertFails(self.run_gate("g9", "D"), "g9.source-changed")
        self.setUp()
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"].replace('flask = "^2"', 'flask = { git = "https://example.invalid/flask.git" }')}, "poetry")
        self.assertFails(self.run_gate("g9", "D"), "g9.source-changed")

    def test_pip_index_options_in_requirements(self):
        for line in ("--index-url https://example.invalid/simple", "--extra-index-url https://example.invalid/simple", "--find-links https://example.invalid/wheels",
                     "-f ./wheels", "--trusted-host example.invalid", "--pre", "-i https://example.invalid/simple", "--extra-index-url=https://example.invalid/s"):
            with self.subTest(line):
                self.setUp()
                self.commit({"requirements.txt": "%s\nflask==2.0.1\n" % line}, "index")
                self.assertFails(self.run_gate("g9", "D"), "g9.index-changed")

    def test_npmrc_registry_and_scope_registry(self):
        for text in ("registry=https://example.invalid/\n", "@acme:registry=https://example.invalid/\n"):
            with self.subTest(text):
                self.setUp()
                self.commit({".npmrc": "registry=https://registry.npmjs.org/\n" + text if "@" in text else text}, "npmrc")
                self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")

    def test_npm_overrides_and_resolutions_to_a_non_registry_source(self):
        doc = json.loads(pkg())
        doc["overrides"] = {"minimist": "git+https://example.invalid/m.git"}
        self.commit({"package.json": json.dumps(doc, indent=1) + "\n"}, "override")
        self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")
        self.setUp()
        doc["overrides"] = {"minimist": "1.2.8"}
        doc["resolutions"] = {"a": {"b": "https://example.invalid/b.tgz"}}
        self.commit({"package.json": json.dumps(doc, indent=1) + "\n"}, "resolution")
        self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")
        self.setUp()
        doc["resolutions"] = {}
        self.commit({"package.json": json.dumps(doc, indent=1) + "\n"}, "plain override")
        self.assertNotIn("g9.index-changed", self.ids(self.run_gate("g9", "A")))

    def test_cargo_patch_tables(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + '\n[patch.crates-io]\nserde = { git = "https://example.invalid/serde.git" }\n'}, "patch")
        self.assertFails(self.run_gate("g9", "D"), "g9.index-changed")

    def test_pyproject_index_settings(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\n[[tool.uv.index]]\nurl = "https://example.invalid/simple"\n'}, "uv")
        self.assertFails(self.run_gate("g9", "D"), "g9.index-changed")
        self.setUp()
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\n[tool.poetry.source]\nname = "x"\nurl = "https://example.invalid/simple"\n'}, "poetry source")
        self.assertFails(self.run_gate("g9", "D"), "g9.index-changed")


class SupplyIndexAlreadyThere(GateCase):
    ITEMS = _SupplyBase.ITEMS
    BASE_FILES = dict(_SupplyBase.BASE_FILES, **{"requirements.txt": "--extra-index-url https://example.invalid/simple\nflask==2.0.1\n"})

    def test_an_unchanged_index_line_is_not_a_change(self):
        self.commit({"requirements.txt": self.BASE_FILES["requirements.txt"] + "requests==2.31.0\n"}, "add a dep")
        self.assertNotIn("g9.index-changed", self.ids(self.run_gate("g9", "D")))

    def test_changing_the_url_is(self):
        self.commit({"requirements.txt": self.BASE_FILES["requirements.txt"].replace("simple", "other")}, "move the index")
        self.assertFails(self.run_gate("g9", "D"), "g9.index-changed")
