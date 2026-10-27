"""G9 (supply chain) and G11 (secrets)."""
from __future__ import annotations

import json
import os
import sys

from .. import item_doc
from . import GateCase

PKG = {"name": "x", "version": "1.0.0", "scripts": {"test": "jest"}, "dependencies": {"express": "^4.0.0"}, "devDependencies": {"jest": "^29.0.0"}}
DEPCHECK = '''import json, os, sys
name = os.environ["LOOPCTL_DEP_NAME"]
table = {"goodlib": {"exists": True, "age_days": 900, "downloads": 5000000, "license": "MIT"},
         "newlib": {"exists": True, "age_days": 3, "downloads": 40, "license": "MIT"},
         "ghostlib": {"exists": False},
         "gpllib": {"exists": True, "age_days": 900, "downloads": 90000, "license": "GPL-3.0"},
         "nolicense": {"exists": True, "age_days": 900, "downloads": 90000},
         "lodsh": {"exists": True, "age_days": 2000, "downloads": 900000, "license": "MIT"}}
if name == "crash":
    sys.exit(3)
if name == "garbage":
    print("not json"); sys.exit(0)
print(json.dumps(table.get(name, {"exists": True, "age_days": 400, "downloads": 12000, "license": "MIT"})))
'''
CHECK = "dep_check_cmd: \"%s scripts/depcheck.py\"\nlicense_allowlist:\n  - MIT\n  - Apache-2.0\n" % sys.executable


def pkg(**kw):
    d = json.loads(json.dumps(PKG))
    for k, v in kw.items():
        d.setdefault(k, {}).update(v)
    return json.dumps(d, indent=1) + "\n"


class Supply(GateCase):
    ITEMS = [item_doc("A", acs=1), item_doc("D", acs=1, slug="slice-d", touches={"domains": ["backend"], "paths": ["package.json"], "schema": False, "deps_change": True}),
             item_doc("P", acs=1, slug="slice-p", touches={"domains": ["backend"], "paths": ["package.json"], "schema": False, "deps_change": True},
                      ext={"g9": {"allow": ["lodsh", "mypkg"]}})]
    BASE_FILES = {"package.json": pkg(), "package-lock.json": "{}\n", "requirements.txt": "flask==2.0.1\n", "scripts/depcheck.py": DEPCHECK,
                  "pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests>=2"]\n', "go.mod": "module x\n\nrequire (\n\tgithub.com/a/b v1.0.0\n)\n",
                  "Cargo.toml": '[package]\nname = "x"\n\n[dependencies]\nserde = "1"\n'}

    def test_no_manifest_change_passes_with_zero_dependencies(self):
        self.commit({"src/a.py": "x = 1\n"}, "work")
        res = self.run_gate("g9")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["summary"], "0 new dependencies")

    def test_an_undeclared_dependency_is_refused(self):
        self.commit({"package.json": pkg(dependencies={"leftpad": "1.0.0"}), "package-lock.json": "{ }\n"}, "add leftpad")
        res = self.run_gate("g9", "A")
        self.assertFails(res, "g9.undeclared-dependency")
        self.assertEqual([d["name"] for d in res["evidence"]["new_dependencies"]], ["leftpad"])

    def test_a_declared_dependency_with_its_lockfile_passes_and_is_listed_unchecked(self):
        self.commit({"package.json": pkg(dependencies={"leftpad": "1.0.0"}), "package-lock.json": "{ }\n"}, "add leftpad")
        res = self.run_gate("g9", "D")
        self.assertPass(res)
        self.assertIn("g9.registry-unchecked", self.ids(res))
        self.assertIn("(unchecked)", res["evidence"]["summary"])

    def test_a_manifest_change_without_the_lockfile_is_refused(self):
        self.commit({"package.json": pkg(dependencies={"leftpad": "1.0.0"})}, "no lock")
        self.assertFails(self.run_gate("g9", "D"), "g9.lockfile-not-updated")

    def test_lifecycle_scripts_are_refused(self):
        self.commit({"package.json": pkg(scripts={"postinstall": "node setup.js"})}, "postinstall")
        self.assertFails(self.run_gate("g9", "A"), "g9.install-script-added")

    def test_non_registry_sources_skip_every_registry_check(self):
        self.commit({"package.json": pkg(dependencies={"evil": "git+https://example.invalid/evil.git"}), "package-lock.json": "{ }\n"}, "git dep")
        self.assertFails(self.run_gate("g9", "D"), "g9.non-registry-source")
        for spec in ("https://example.invalid/e.tgz", "file:../e", "github:a/b"):
            self.setUp()
            self.commit({"package.json": pkg(dependencies={"evil": spec}), "package-lock.json": "{ }\n"}, "dep")
            self.assertFails(self.run_gate("g9", "D"), "g9.non-registry-source")

    def test_the_plan_can_allow_a_named_non_registry_source(self):
        self.commit({"package.json": pkg(dependencies={"mypkg": "git+https://example.invalid/x.git"}), "package-lock.json": "{ }\n"}, "git dep")
        res = self.run_gate("g9", "P")
        self.assertEqual(res["status"], "pass", res["findings"])

    def test_a_look_alike_of_a_popular_package_is_refused_unless_the_plan_allows_it(self):
        self.commit({"package.json": pkg(dependencies={"lodsh": "1.0.0"}), "package-lock.json": "{ }\n"}, "lodsh")
        self.assertFails(self.run_gate("g9", "D"), "g9.lookalike")
        res = self.run_gate("g9", "P")
        self.assertPass(res)
        self.assertEqual([f["severity"] for f in res["findings"] if f["id"] == "g9.lookalike"], ["warn"])

    def test_a_popular_package_is_itself_not_a_look_alike(self):
        self.commit({"package.json": pkg(dependencies={"lodash": "4.0.0"}), "package-lock.json": "{ }\n"}, "lodash")
        self.assertNotIn("g9.lookalike", self.ids(self.run_gate("g9", "D")))

    def test_requirements_must_be_pinned_but_need_no_lockfile(self):
        self.commit({"requirements.txt": "flask==2.0.1\nrequests==2.31.0\n"}, "pinned")
        self.assertPass(self.run_gate("g9", "D"))
        self.setUp()
        self.commit({"requirements.txt": "flask==2.0.1\nrequests>=2\n"}, "loose")
        self.assertFails(self.run_gate("g9", "D"), "g9.unpinned")

    def test_pyproject_go_and_cargo_dependencies_are_read(self):
        self.commit({"pyproject.toml": '[project]\nname = "x"\nclassifiers = ["Programming Language :: Python"]\ndependencies = ["requests>=2", "newpkg[extra]>=1"]\n',
                     "go.mod": "module x\n\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/c/d v2.0.0\n)\n", "go.sum": "x\n",
                     "Cargo.toml": '[package]\nname = "x"\n\n[dependencies]\nserde = "1"\ntokio = { version = "1" }\n', "Cargo.lock": "x\n",
                     "poetry.lock": "x\n"}, "three ecosystems")
        res = self.run_gate("g9", "D")
        names = sorted((d["ecosystem"], d["name"]) for d in res["evidence"]["new_dependencies"])
        self.assertEqual(names, [("cargo", "tokio"), ("go", "github.com/c/d"), ("pypi", "newpkg")])

    def test_npmrc_may_not_reenable_install_scripts_and_workflows_warn(self):
        self.commit({".npmrc": "ignore-scripts=false\n"}, "npmrc")
        self.assertFails(self.run_gate("g9", "A"), "g9.scripts-reenabled")
        self.setUp()
        self.commit({".github/workflows/ci.yml": "jobs:\n  b:\n    steps:\n      - run: npm install\n"}, "ci")
        res = self.run_gate("g9", "A")
        self.assertIn("g9.install-hygiene", self.ids(res))
        self.assertEqual(res["status"], "pass")
        self.setUp()
        self.commit({".github/workflows/ci.yml": "jobs:\n  b:\n    steps:\n      - run: npm ci --ignore-scripts\n"}, "ci ok")
        self.assertNotIn("g9.install-hygiene", self.ids(self.run_gate("g9", "A")))

    def test_too_many_new_dependencies_is_refused(self):
        deps = {"pkg%d" % i: "1.0.%d" % i for i in range(25)}
        self.commit({"package.json": pkg(dependencies=deps), "package-lock.json": "{ }\n"}, "many")
        self.assertFails(self.run_gate("g9", "D"), "g9.too-many")


class SupplyChecked(GateCase):
    ITEMS = Supply.ITEMS
    CFG = CHECK
    BASE_FILES = Supply.BASE_FILES

    def add(self, **deps):
        self.commit({"package.json": pkg(dependencies=deps), "package-lock.json": "{ }\n"}, "dep")

    def test_a_package_that_exists_and_is_fine_passes_and_is_marked_checked(self):
        self.add(goodlib="1.0.0")
        res = self.run_gate("g9", "D")
        self.assertPass(res)
        self.assertTrue(res["evidence"]["new_dependencies"][0]["checked"])
        self.assertNotIn("(unchecked)", res["evidence"]["summary"])

    def test_a_name_that_could_carry_shell_syntax_never_reaches_the_command(self):
        marker = os.path.join(self.lr.path, "pwned")
        self.add(**{"x; touch pwned": "1.0.0"})
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.bad-name")
        self.assertFalse(os.path.exists(marker))

    def test_a_package_that_does_not_exist_is_a_hallucinated_name(self):
        self.add(ghostlib="1.0.0")
        self.assertFails(self.run_gate("g9", "D"), "g9.not-in-registry")

    def test_a_young_or_rare_package_is_a_warning(self):
        self.add(newlib="1.0.0")
        res = self.run_gate("g9", "D")
        self.assertPass(res)
        self.assertIn("g9.young-or-rare", self.ids(res))

    def test_a_licence_outside_the_allowlist_or_unknown_is_refused(self):
        self.add(gpllib="1.0.0")
        self.assertFails(self.run_gate("g9", "D"), "g9.license-not-allowed")
        self.setUp()
        self.add(nolicense="1.0.0")
        self.assertFails(self.run_gate("g9", "D"), "g9.license-unknown")

    def test_a_check_that_crashes_or_prints_garbage_is_unknown_never_safe(self):
        for name in ("crash", "garbage"):
            self.setUp()
            self.add(**{name: "1.0.0"})
            res = self.run_gate("g9", "D")
            self.assertFails(res, "g9.license-unknown")      # the allowlist is set and nothing says what the licence is
            self.assertFalse(res["evidence"]["new_dependencies"][0]["checked"])

    def test_a_verified_old_widely_used_look_alike_is_only_a_warning(self):
        self.add(lodsh="1.0.0")
        res = self.run_gate("g9", "D")
        self.assertEqual([f["severity"] for f in res["findings"] if f["id"] == "g9.lookalike"], ["warn"])


def aws_key():
    return "AKIA" + "ABCDEFGHIJKLMNOP"          # built at run time: this file is not itself a finding


def gh_token():
    return "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


SCAN_OK = "import sys; sys.exit(0)\n"


class Secrets(GateCase):
    ITEMS = [item_doc("A", acs=1), item_doc("F", acs=1, slug="slice-f", ext={"g11": {"allow": ["tests/fixtures/**"]}})]
    BASE_FILES = {"src/a.py": "x = 1\n"}

    def test_clean_changes_pass(self):
        self.commit({"src/a.py": "x = 2\nPASSWORD = os.environ['PASSWORD']\napi_key = settings.api_key\n"}, "work")
        res = self.run_gate("g11")
        self.assertPass(res)
        self.assertEqual(res["evidence"]["summary"], "built-in scan: 0 finding(s)")

    def test_cloud_and_vcs_tokens_in_added_lines(self):
        for text, kind in (("KEY = '%s'\n" % aws_key(), "aws-access-key"), ("T = '%s'\n" % gh_token(), "github-token"),
                           ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n", "private-key"),
                           ("db = 'postgres://app:hunter2hunter2@db.internal/x'\n", "url-credentials"),
                           ("password = \"s3cr3tValue99\"\n", "secret-assignment")):
            with self.subTest(kind):
                self.setUp()
                self.commit({"src/conf.py": text}, "config")
                res = self.run_gate("g11")
                self.assertFails(res, "g11." + kind)
                self.assertNotIn(aws_key(), json.dumps(res))
                self.assertNotIn("hunter2hunter2", json.dumps(res))
                self.assertNotIn("s3cr3tValue99", json.dumps(res))

    def test_placeholders_and_documented_examples_are_not_secrets(self):
        self.commit({"docs/setup.md": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\npassword = \"changeme\"\ntoken = \"<your-token-here>\"\n"
                                      "api_key = \"${API_KEY}\"\npassword = \"example-password-1\"\n"}, "docs")
        self.assertPass(self.run_gate("g11"))

    def test_a_secret_added_and_removed_again_is_still_in_the_history(self):
        self.commit({"src/conf.py": "KEY = '%s'\n" % aws_key()}, "oops")
        self.commit({"src/conf.py": "KEY = os.environ['KEY']\n"}, "fix")
        res = self.run_gate("g11")
        self.assertFails(res, "g11.aws-access-key")
        self.assertIn("commit", res["findings"][0]["message"])

    def test_deleting_a_secret_is_not_adding_one(self):
        self.commit({"src/conf.py": "x = 1\n"}, "tidy")
        self.assertPass(self.run_gate("g11"))

    def test_a_secret_in_a_commit_message(self):
        self.commit({"src/a.py": "x = 3\n"}, "use token %s for the deploy" % gh_token())
        self.assertFails(self.run_gate("g11"), "g11.github-token")

    def test_uncommitted_files_are_scanned_too(self):
        with open(os.path.join(self.lr.path, "scratch.txt"), "w") as fh:
            fh.write("%s\n" % aws_key())
        self.assertFails(self.run_gate("g11"), "g11.aws-access-key")

    def test_an_uncommitted_evidence_file_is_scanned(self):
        d = os.path.join(self.lr.path, "docs", "features", "slice-a", "reports")
        os.makedirs(d)
        with open(os.path.join(d, "backend-engineer.md"), "w") as fh:
            fh.write("## Status\nAPPROVED\nauth: %s\n" % gh_token())
        self.assertFails(self.run_gate("g11"), "g11.github-token")

    def test_the_plan_can_allow_a_fixture_path(self):
        self.commit({"tests/fixtures/creds.py": "KEY = '%s'\n" % aws_key()}, "fixture")
        res = self.run_gate("g11", "F")
        self.assertPass(res)
        self.assertEqual([f["severity"] for f in res["findings"]], ["warn"])
        self.setUp()
        self.commit({"src/creds.py": "KEY = '%s'\n" % aws_key()}, "not a fixture")
        self.assertFails(self.run_gate("g11", "F"), "g11.aws-access-key")

    def test_a_leftover_in_the_event_files_is_only_a_warning(self):
        from loopctl.gates import _lib
        rs = _lib.runstate_of(self.ctx())
        os.makedirs(rs.events_dir, exist_ok=True)
        path = os.path.join(rs.events_dir, "x.jsonl")
        with open(path, "w") as fh:
            fh.write('{"msg": "%s"}\n' % gh_token())
        try:
            res = self.run_gate("g11")
            self.assertPass(res)
            self.assertTrue(any(f["severity"] == "warn" for f in res["findings"]))
        finally:
            os.unlink(path)


class SecretsExternal(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    BASE_FILES = {"src/a.py": "x = 1\n", "scan.py": "import os, sys\nsys.exit(1 if os.path.exists('scan-fails') else 0)\n"}
    CFG = "secrets_scan_cmd: \"%s scan.py\"\n" % sys.executable

    def test_an_external_scanner_runs_on_top_and_a_non_zero_exit_fails(self):
        self.assertPass(self.run_gate("g11"))
        with open(os.path.join(self.lr.path, "scan-fails"), "w") as fh:
            fh.write("")
        res = self.run_gate("g11")
        self.assertFails(res, "g11.scanner")
        self.assertIn("built-in + external", res["evidence"]["summary"])


if __name__ == "__main__":
    import unittest
    unittest.main()
