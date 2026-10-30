"""Second-round review fixes, package B: R-GATES-1 .. R-GATES-6 and the quadratic `url-credentials` regex.

One test class per finding so a regression names it. Secret-shaped values are assembled at run time: this file is not itself a finding.
"""
from __future__ import annotations

import importlib
import json
import os
import random
import sys
import time
import unittest
from unittest import mock

from .. import item_doc
from . import ACCEPTANCE_OK, IMPL, PKG, STUB, GateCase, g3_item
from . import test_g9_g11 as _g9
from .test_g4 import EVIDENCE, MUTATE

from loopctl.gates import g4_mutation, g5_integrity, g9_supply, g11_secrets

try:
    tomllib = importlib.import_module("tomllib")
except ImportError:                                   # Python < 3.11
    tomllib = None


def tok(*parts):
    return "".join(parts)


# -- R-GATES-1: GT-5, an escaped quote in a multi-line string, and the tools that hold the verify command --------------------------------

REVIEWER_HEAD = '[project]\nname = "x"\ndescription = """a\\""" more\n"""\n[tool.pytest.ini_options]\naddopts = "-q -p evil"\nnote = "x"\n# """\n'
BASE_PROJECT = '[project]\nname = "x"\n'


class TomlEscapedQuotes(unittest.TestCase):
    def test_the_reviewers_exact_input_is_a_change(self):
        self.assertEqual(g5_integrity._toml_tables(REVIEWER_HEAD), {"tool.pytest.ini_options": 'addopts = "-q -p evil"\nnote = "x"'})
        self.assertTrue(g5_integrity._toml_changed(BASE_PROJECT, REVIEWER_HEAD))

    def test_an_escaped_backslash_before_the_closing_quotes_does_close_the_string(self):
        text = 'a = """x\\\\"""\n[tool.pytest.ini_options]\naddopts = "1"\n'
        self.assertEqual(g5_integrity._toml_tables(text), {"tool.pytest.ini_options": 'addopts = "1"'})

    def test_extra_quotes_at_the_end_belong_to_the_string(self):
        text = 'a = """x""""\n[tool.pytest.ini_options]\naddopts = "1"\n'
        self.assertEqual(g5_integrity._toml_tables(text), {"tool.pytest.ini_options": 'addopts = "1"'})
        text = 'a = """x"""""\n[tool.pytest.ini_options]\naddopts = "1"\n'
        self.assertEqual(g5_integrity._toml_tables(text), {"tool.pytest.ini_options": 'addopts = "1"'})

    def test_a_line_continuation_backslash_inside_the_string(self):
        text = 'a = """x \\\n  y"""\n[tool.pytest.ini_options]\naddopts = "1"\n'
        self.assertEqual(g5_integrity._toml_tables(text), {"tool.pytest.ini_options": 'addopts = "1"'})

    def test_literal_strings_have_no_escapes(self):
        text = "a = '''x\\'''\n[tool.pytest.ini_options]\naddopts = \"1\"\n"
        self.assertEqual(g5_integrity._toml_tables(text), {"tool.pytest.ini_options": 'addopts = "1"'})

    def test_variants_with_the_escape_in_other_positions(self):
        for desc in ('"""\\"""\n"""', '"""\\"\\"\\""""', '"""a\\"""b\\"""c"""', '"""\\\\\\""""\n"""'):
            with self.subTest(desc):
                text = "[project]\ndescription = %s\n[tool.pytest.ini_options]\naddopts = \"-p evil\"\n" % desc
                got = g5_integrity._toml_tables(text)
                if got is not None:             # an unreadable file counts as changed, which is also safe
                    self.assertIn("tool.pytest.ini_options", got, desc)
                self.assertTrue(g5_integrity._toml_changed(BASE_PROJECT, text), desc)

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_differential_fuzz_against_tomllib(self):
        """Documents built from quote, backslash, hash and bracket fragments: whenever the standard parser sees a pytest option the reader must report a change
        (or give up, which also counts as a change)."""
        frags = ['"""', "'''", '\\"', "\\\\", '"', "'", "#", " x ", "\n", "a", "[", "]", "\\\n"]
        rng = random.Random(1313)
        checked = 0
        for _ in range(4000):
            junk = "".join(rng.choice(frags) for _ in range(rng.randint(1, 9)))
            text = "[project]\nname = \"x\"\nd = %s\n[tool.pytest.ini_options]\naddopts = \"-p evil\"\n" % junk
            try:
                doc = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            if doc.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts") != "-p evil":
                continue
            checked += 1
            self.assertTrue(g5_integrity._toml_changed(BASE_PROJECT, text), repr(junk))
        self.assertGreater(checked, 20)


class TomlVerifyCommandTools(unittest.TestCase):
    def test_hatch_envs_pdm_scripts_and_poe_tasks_are_integrity_tables(self):
        for name, text in (("hatch", '[tool.hatch.envs.test.scripts]\nrun = "pytest -p evil"\n'), ("hatch envs table", '[tool.hatch.envs.test]\nscripts = { run = "x" }\n'),
                           ("pdm scripts", '[tool.pdm.scripts]\ntest = "pytest -p evil"\n'), ("poe", '[tool.poe.tasks]\ntest = "pytest -p evil"\n'),
                           ("taskipy", '[tool.taskipy.tasks]\ntest = "x"\n'), ("dotted key", 'tool.hatch.envs.test.scripts.run = "x"\n')):
            with self.subTest(name):
                head = text + BASE_PROJECT if name == "dotted key" else BASE_PROJECT + text          # a dotted key is top level only before the first header
                self.assertTrue(g5_integrity._toml_changed(BASE_PROJECT, head), name)

    def test_other_hatch_and_pdm_tables_are_not(self):
        for text in ('[tool.hatch.version]\npath = "a.py"\n', '[tool.hatch.build.targets.wheel]\npackages = ["a"]\n', '[tool.pdm.build]\nincludes = ["a"]\n'):
            with self.subTest(text):
                self.assertFalse(g5_integrity._toml_changed(BASE_PROJECT, BASE_PROJECT + text), text)


# -- R-GATES-2: D8, a product module that prints a runner's output and ends the process -------------------------------------------------

FORGED_TEXT = ('import os\nprint("test_ac_a_1_adds (tests.acceptance.test_a.TestAcceptance.test_ac_a_1_adds) ... ok", flush=True)\n'
               'print("test_ac_a_2_subtracts (tests.acceptance.test_a.TestAcceptance.test_ac_a_2_subtracts) ... ok", flush=True)\n'
               'print("Ran 2 tests in 0.001s\\n\\nOK", flush=True)\nos._exit(0)\n\n')


class ForgedRunnerOutput(GateCase):
    ITEMS = [g3_item("A")]
    BASE_FILES = {}

    def built(self, impl):
        self.commit(dict(PKG, **{"src/calc.py": STUB}), "stubs")
        self.commit({"tests/acceptance/test_a.py": ACCEPTANCE_OK}, "tests")
        self.pin_acceptance()
        self.commit({"src/calc.py": impl}, "implementation")

    def test_the_reviewers_forgery_is_an_error_in_g5(self):
        self.built(FORGED_TEXT + STUB)
        res = self.run_gate("g5")
        self.assertFails(res, "g5.module-exit", "g5.runner-shaped-output")

    def test_the_forgery_without_the_exit_is_still_runner_shaped(self):
        self.built(IMPL + '\nprint("test_ac_a_2_subtracts (tests.acceptance.test_a.TestAcceptance.test_ac_a_2_subtracts) ... ok")\n')
        self.assertFails(self.run_gate("g5"), "g5.runner-shaped-output")

    def test_the_forgery_inside_a_function_is_runner_shaped_even_if_the_exit_is_not_at_module_level(self):
        body = ('import os\n\ndef add(a, b):\n    print("Ran 2 tests in 0.001s")\n    print("OK")\n    os._exit(0)\n\n\ndef subtract(a, b):\n    return a - b\n')
        self.built(body)
        self.assertFails(self.run_gate("g5"), "g5.runner-shaped-output")

    def test_pytest_and_tap_shapes(self):
        for text in ('print("tests/acceptance/test_a.py::test_ac_a_1_adds PASSED")\n', 'print("ok 1 - test_ac_a_1_adds")\n', 'print("%s ... ok" % "test_ac_a_1_adds")\n'):
            with self.subTest(text):
                self.setUp()
                self.built(IMPL + "\n" + text)
                self.assertFails(self.run_gate("g5"), "g5.runner-shaped-output")

    def test_module_level_exits_in_every_import_time_position(self):
        for name, body in (("os._exit", "import os\nos._exit(0)\n"), ("sys.exit", "import sys\nsys.exit(0)\n"), ("exit", "exit(0)\n"), ("quit", "quit()\n"),
                           ("raise SystemExit", "raise SystemExit(0)\n"), ("inside an if", "import os\nif True:\n    os._exit(0)\n"),
                           ("inside a try", "import os\ntry:\n    os._exit(0)\nexcept Exception:\n    pass\n"), ("class body", "import os\nclass A:\n    os._exit(0)\n"),
                           ("a default value", "import os\ndef f(x=os._exit(0)):\n    return x\n"), ("a decorator", "import os\n@(lambda f: os._exit(0))\ndef f():\n    pass\n"),
                           ("os.abort", "import os\nos.abort()\n"), ("from os import _exit", "from os import _exit\n_exit(0)\n"),
                           ("a with block", "import os\nwith open('x') as f:\n    os._exit(0)\n")):
            with self.subTest(name):
                self.assertGreater(g5_integrity.module_exits(body), 0, name)

    def test_a_cli_a_function_and_a_syntax_error_are_judged_correctly(self):
        self.assertEqual(g5_integrity.module_exits('import sys\n\ndef main():\n    sys.exit(2)\n\nif __name__ == "__main__":\n    sys.exit(main())\n'), 0)
        self.assertEqual(g5_integrity.module_exits('import sys\nif "__main__" == __name__:\n    sys.exit(main())\n'), 0)
        self.assertEqual(g5_integrity.module_exits("def f():\n    import os\n    os._exit(1)\n"), 0)
        self.assertEqual(g5_integrity.module_exits("x = lambda: exit(1)\n"), 0)
        self.assertGreater(g5_integrity.module_exits("import os\nos._exit(0)\ndef broken(:\n"), 0)       # does not parse: the column-0 call counts

    def test_the_else_of_a_main_guard_runs_at_import(self):
        """The body of `if __name__ == "__main__":` is a CLI; its `else:` (and `elif:`) runs when the module is imported, so an exit there counts."""
        self.assertEqual(g5_integrity.module_exits('import os\nif __name__ == "__main__":\n    pass\nelse:\n    os._exit(0)\n'), 1)
        self.assertEqual(g5_integrity.module_exits('import os\nif "__main__" == __name__:\n    pass\nelse:\n    os._exit(0)\n'), 1)
        self.assertEqual(g5_integrity.module_exits('import os\nif __name__ == "__main__":\n    pass\nelif os.environ.get("X"):\n    os._exit(0)\n'), 1)
        self.assertEqual(g5_integrity.module_exits('import sys\nif __name__ == "__main__":\n    sys.exit(0)\nelse:\n    x = 1\n'), 0)
        self.assertEqual(g5_integrity.module_exits('import sys\nif __name__ == "__main__":\n    sys.exit(0)\n'), 0)

    def test_the_else_of_a_main_guard_is_an_error_in_g5(self):
        self.built(IMPL + '\nimport os\nif __name__ == "__main__":\n    pass\nelse:\n    os._exit(0)\n')
        self.assertFails(self.run_gate("g5"), "g5.module-exit")

    def test_a_helper_named_test_something_that_logs_ok_is_not_runner_shaped(self):
        """`def test_connection(` names a function; it does not print a test's name, so a `... ok` log line near it is not a forged runner line."""
        self.built(IMPL + '\n\ndef test_connection(host):\n    log.info("%s ... ok", host)\n    return True\n')
        self.assertNotIn("g5.runner-shaped-output", self.ids(self.run_gate("g5")))

    def test_but_a_one_line_def_that_prints_the_shape_still_is(self):
        self.built(IMPL + '\n\ndef f(): print("test_ac_a_1_adds (tests.acceptance.test_a.TestAcceptance.test_ac_a_1_adds) ... ok")\n')
        self.assertFails(self.run_gate("g5"), "g5.runner-shaped-output")

    def test_a_cli_with_a_main_guard_is_not_flagged(self):
        self.built(IMPL + '\n\ndef main():\n    return 0\n\n\nif __name__ == "__main__":\n    import sys\n    sys.exit(main())\n')
        res = self.run_gate("g5")
        self.assertNotIn("g5.module-exit", self.ids(res))
        self.assertNotIn("g5.runner-shaped-output", self.ids(res))

    def test_ordinary_output_is_not_runner_shaped(self):
        self.built(IMPL + '\nprint("connecting to the database... ok")\nprint("Ran 3 jobs")\nprint("ok")\nprint("tests passed: see report")\n')
        res = self.run_gate("g5")
        self.assertNotIn("g5.runner-shaped-output", self.ids(res))

    def test_tests_and_docs_may_print_the_shape(self):
        self.built(IMPL)
        self.commit({"tests/unit/test_x.py": 'print("test_x (m.T.test_x) ... ok")\nimport os\nos._exit(0)\n', "docs/a.md": "test_x (m.T.test_x) ... ok\nRan 2 tests\n"}, "tests")
        res = self.run_gate("g5")
        self.assertNotIn("g5.module-exit", self.ids(res))
        self.assertNotIn("g5.runner-shaped-output", self.ids(res))


# -- R-GATES-3: G11 and redact ----------------------------------------------------------------------------------------------------------

V = "Ab12" + "Cd34Ef56" + "Gh78"
BANG = "!Xx7" + "Qm9Zk2pq"


class SecretsReach(unittest.TestCase):
    """`scan()` must see what `redact()` redacts, for the shapes the reviewer listed."""

    def hit(self, text, path):
        self.assertTrue(g11_secrets.scan(text, path), (path, text))

    def miss(self, text, path):
        self.assertFalse(g11_secrets.scan(text, path), (path, text))

    def test_the_reviewers_list(self):
        for path, text in (("Dockerfile", "ENV DB_PASSWORD=%s" % V), ("Dockerfile", "ARG TOKEN=%s" % V), ("Dockerfile.prod", "ENV API_SECRET=%s" % V),
                           ("setup.sh", "mysql --password=%s -h x" % V), ("run.sh", "docker run -e DB_PASSWORD=%s img" % V),
                           ("x.json", '"Env": ["DB_PASSWORD=%s"]' % V), ("appsettings.json", '"Db": "Server=x;Password=%s;"' % V),
                           ("settings.xml", "<password>%s</password>" % V), ("web.config", '<add key="DbPassword" value="%s"/>' % V),
                           ("Db.cs", 'var c = "Server=x;Password=%s;";' % V), ("Makefile", "\tDB_PASSWORD=%s make deploy" % V),
                           ("run.sh", "mysql --password %s -h x" % V), ("run.sh", "sshpass -p %s ssh host" % V), ("run.sh", "mysql -u root -p%s db" % V)):
            with self.subTest(path + " " + text[:30]):
                self.hit(text, path)

    def test_a_first_character_that_is_not_syntax_in_a_dotenv_or_ini(self):
        for first in "!*&|>{[<(":
            with self.subTest(first):
                self.hit("DB_PASSWORD=%sXx7Qm9Zk2pq" % first, ".env")
                self.hit("password = %sXx7Qm9Zk2pq" % first, "settings.ini")
        self.hit("DB_PASSWORD=%s" % BANG, ".env.production")

    def test_but_a_yaml_tag_anchor_or_alias_is_syntax(self):
        for first in "!*&|>{[<":
            with self.subTest(first):
                self.miss("password: %sXx7Qm9Zk2pq" % first, "a.yml")

    def test_secret_key_base_signing_and_encryption_keys(self):
        for text, path in (("secret_key_base: %s" % V, "config/secrets.yml"), ("SECRET_KEY_BASE=%s" % V, ".env"), ("SIGNING_KEY=%s" % V, ".env"),
                           ("ENCRYPTION_KEY=%s" % V, ".env"), ("master_key: %s" % V, "a.yaml"), ("HMAC_KEY=%s" % V, ".env")):
            with self.subTest(text[:12]):
                self.hit(text, path)

    def test_none_of_these_is_a_secret(self):
        for text, path in (("DB_PASSWORD=$DB_PASSWORD", "Dockerfile"), ("ENV DB_PASSWORD=${DB_PASSWORD}", "Dockerfile"), ("mysql --password $PW -h x", "run.sh"),
                           ("<password>${db.password}</password>", "pom.xml"), ("password: ${DB_PASSWORD}", "a.yml"), ("--token-budget 5000", "run.sh"),
                           ("mysql -u root -p db", "run.sh"), ("<password>changeme</password>", "a.xml"), ("<passwordPolicy>strict-policy-1</passwordPolicy>", "a.xml"),
                           ("foo(password=args.password1)", "a.py"), ("x = dict(password=cfg.password2)", "a.py"), ("password=get_password(1)", "a.sh"),
                           ("secret_key_base: ${SECRET_KEY_BASE}", "a.yml"), ("DB_PASSWORD=%(db_password)s", "a.ini"), ("--password ./secret-file.txt", "run.sh"),
                           ("    DB_PASSWORD = %s" % V, "settings.py"),          # an unquoted right-hand side in source code is a name, not a literal
                           ('"password": 12', "a.json"), ('"tokens": 1200000', "a.json")):
            with self.subTest(text):
                self.miss(text, path)

    def test_the_value_is_never_in_a_finding(self):
        for kind, _line in g11_secrets.scan("ENV DB_PASSWORD=%s" % V, "Dockerfile"):
            self.assertNotIn(V, kind)

    def test_a_password_with_punctuation_in_a_dotenv_or_ini_is_read_to_the_end_of_the_token(self):
        """`,` `;` `"` `)` are ordinary password characters in a dotenv, ini, properties or Dockerfile; the shared pattern stops at the first of them."""
        for path, text in ((".env", "DB_PASSWORD=Ab12;Cd34Ef56"), (".env", 'DB_PASSWORD=Xx7"Qm9Zk2pq'), (".env", "DB_PASSWORD=Ab12,Cd34Ef56)"),
                           ("settings.ini", "password = Ab12;Cd34Ef56 # note"), ("Dockerfile", "ENV DB_PASSWORD=Ab12;Cd34Ef56")):
            with self.subTest(path + " " + text):
                self.hit(text, path)
        self.miss("DB_PASSWORD=changeme   # example", ".env")

    def test_the_value_ends_at_a_blank_not_at_the_end_of_the_line(self):
        """The first reading of the fix read to the end of the line: `ENV DB_PASSWORD=<secret> API_URL=https://x` was missed (a `://` with no `@` rejects a
        value), and `ENV DB_PASSWORD=none OTHER=1` and `RUN DB_PASSWORD=test ./run-tests.sh` were judged by what followed the value."""
        for text in ("ENV DB_PASSWORD=%s API_URL=https://api.example.com" % V, "ENV API_URL=https://api.example.com DB_PASSWORD=%s" % V,
                     "ENV DB_PASSWORD=%s API_URL=`date`" % V, "ENV DB_PASSWORD=Ab12;Cd34Ef56 API_URL=https://api.example.com",
                     "ENV DB_PASSWORD=%s OTHER=1" % V):
            with self.subTest(text):
                self.hit(text, "Dockerfile")
        for text in ("ENV DB_PASSWORD=none OTHER=1", "RUN DB_PASSWORD=test ./run-tests.sh", "ENV DB_PASSWORD=changeme API_URL=https://api.example.com",
                     "ENV DB_PASSWORD=example OTHER=`date`"):
            with self.subTest(text):
                self.miss(text, "Dockerfile")

    def test_dockerfile_env_without_an_equals_sign(self):
        self.hit("ENV DB_PASSWORD %s" % V, "Dockerfile")
        self.hit("ENV DB_PASSWORD '%s'" % V, "Dockerfile")
        self.miss("ENV DB_PASSWORD changeme", "Dockerfile")
        self.miss("ENV PATH /usr/local/bin", "Dockerfile")

    def test_a_default_in_a_variable_reference_is_a_committed_secret(self):
        """`${NAME}` is a placeholder; `${NAME:-literal}` carries the literal into the repository, so the literal is judged as a value of its own."""
        for path, text in (("a.yml", 'password: "${DB_PASS:-%s}"' % V), ("a.yml", "password: ${DB_PASS:-%s}" % V), ("docker-compose.yml", "      DB_PASSWORD: ${DB_PASSWORD:-%s}" % V),
                           (".env", "DB_PASSWORD=${DB_PASSWORD:-%s}" % V), ("Dockerfile", "ENV DB_PASSWORD=${DB_PASSWORD:-%s}" % V), ("run.sh", 'PASSWORD="${PW:-%s}"' % V),
                           ("run.sh", "PASSWORD=${PW:-%s}" % V), ("a.yml", 'password: "${DB_PASS-%s}"' % V), ("a.yml", 'password: "${DB_PASS:=%s}"' % V),
                           ("a.yml", 'password: "${DB_PASS=%s}"' % V), ("run.sh", 'PASSWORD=${PW:-"%s"}' % V), ("a.yml", "password: ${DB_PASS:+%s}" % V),
                           ("a.toml", 'password = "${DB_PASS:-%s}"' % V)):
            with self.subTest(path + " " + text):
                self.hit(text, path)

    def test_a_variable_reference_without_a_literal_default_is_a_placeholder(self):
        for path, text in (("a.yml", 'password: "${DB_PASS}"'), ("a.yml", "password: ${DB_PASS}"), ("a.yml", 'password: "${DB_PASS:?must be set}"'),
                           ("a.yml", 'password: "${DB_PASS:-}"'), ("a.yml", 'password: "${DB_PASS:-changeme}"'), ("a.yml", "password: ${DB_PASS:-changeme}"),
                           ("a.yml", 'password: "${DB_PASS:-example-password-1}"'), ("docker-compose.yml", "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}"),
                           ("docker-compose.yml", 'POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:-postgres}"'), (".env", "DB_PASSWORD=${DB_PASSWORD:-$OTHER_PASSWORD}"),
                           ("pom.xml", "<password>${db.password}</password>"), ("a.yml", 'password: "${env:DB_PASSWORD}"')):
            with self.subTest(path + " " + text):
                self.miss(text, path)

    def test_a_placeholder_word_inside_a_default_does_not_excuse_a_real_one(self):
        self.hit('password: "${DB_PASS:-changeme%s}"' % V, "a.yml")
        self.hit("password: ${DB_PASS:-example%s}" % V, "a.yml")

    def test_hostile_lines_are_read_in_linear_time(self):
        for name, text in (("unclosed braces", "password: ${" * 6000), ("unclosed braces, one token", "password=${" * 6000), ("repeated assignments", "password=" + "password=Ab12," * 5000),
                           ("long default", "password: ${A:-" + "x1" * 25000 + "}")):
            with self.subTest(name):
                t = time.time()
                g11_secrets.scan(text, ".env")
                g11_secrets.scan(text, "a.yml")
                self.assertLess(time.time() - t, 1.5)


class SecretsRedactAlso(unittest.TestCase):
    def test_redact_catches_the_same_shapes(self):
        from kitlib import redact
        for text in ("<password>%s</password>" % V, '<add key="DbPassword" value="%s"/>' % V, "mysql --password %s -h x" % V, "sshpass -p %s ssh h" % V,
                     "mysql -u root -p%s db" % V, "secret_key_base: %s" % V, "SIGNING_KEY=%s" % V, "ENCRYPTION_KEY=%s" % V):
            with self.subTest(text):
                out = redact.redact(text)
                self.assertNotIn(V, out)
                self.assertEqual(redact.redact(out), out)


# -- R-GATES-6: large files are read in slices and blocking when they cannot be --------------------------------------------------------------

class Slices(unittest.TestCase):
    def test_a_small_text_is_one_slice(self):
        self.assertEqual(g11_secrets.slices("a\nb"), ["a\nb"])

    def test_a_large_text_is_cut_at_line_ends(self):
        with mock.patch.object(g11_secrets, "MAX_BYTES", 100), mock.patch.object(g11_secrets, "MAX_SLICES", 10):
            text = "\n".join("line %03d xxxxxxxxxx" % i for i in range(30))
            parts = g11_secrets.slices(text)
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(len(p) < 100 for p in parts))
            self.assertEqual("\n".join(parts), text)

    def test_a_secret_on_the_cut_of_a_single_line_is_whole_in_one_slice(self):
        with mock.patch.object(g11_secrets, "MAX_BYTES", 10000), mock.patch.object(g11_secrets, "MAX_SLICES", 10):
            secret = tok("AKIA", "ABCDEFGHIJKLMNOP")
            for pad in range(9970, 10010):
                text = "a" * pad + " " + secret + " " + "b" * 15000
                parts = g11_secrets.slices(text)
                self.assertTrue(any(secret in p for p in parts), pad)

    def test_a_long_line_is_cut_with_an_overlap_that_covers_all_of_it(self):
        # MAX_BYTES must be larger than the 4096-character overlap: with a smaller one `ln[MAX_BYTES - 4097:]` is a NEGATIVE index and the slicing is not what
        # these tests would think it is
        text = "".join("%05d" % i for i in range(10000))                 # 50000 distinct-looking characters
        with mock.patch.object(g11_secrets, "MAX_BYTES", 10000), mock.patch.object(g11_secrets, "MAX_SLICES", 100):
            parts = g11_secrets.slices(text)
        self.assertGreater(len(parts), 5)
        self.assertTrue(all(len(p) < 10000 for p in parts))
        for a, b in zip(parts, parts[1:]):
            self.assertEqual(a[-4096:], b[:4096])                         # a secret on a cut is whole in one of the two pieces
        self.assertEqual(parts[0] + "".join(p[4096:] for p in parts[1:]), text)

    def test_too_many_slices_gives_none(self):
        text = "".join("%05d" % i for i in range(10000))
        with mock.patch.object(g11_secrets, "MAX_BYTES", 10000), mock.patch.object(g11_secrets, "MAX_SLICES", 100):
            needed = len(g11_secrets.slices(text))
        self.assertGreater(needed, 3)
        with mock.patch.object(g11_secrets, "MAX_BYTES", 10000), mock.patch.object(g11_secrets, "MAX_SLICES", needed):
            self.assertIsNotNone(g11_secrets.slices(text))                # exactly as many as allowed
        with mock.patch.object(g11_secrets, "MAX_BYTES", 10000), mock.patch.object(g11_secrets, "MAX_SLICES", needed - 1):
            self.assertIsNone(g11_secrets.slices(text))                   # one too many: only the slice count can say so
        with mock.patch.object(g11_secrets, "MAX_BYTES", 100), mock.patch.object(g11_secrets, "MAX_SLICES", 3):
            self.assertIsNone(g11_secrets.slices("x\n" * 500))              # many short lines (no long-line cutting involved)


class LargeFiles(GateCase):
    ITEMS = [item_doc("A", acs=1), item_doc("F", acs=1, slug="slice-f", ext={"g11": {"allow": ["data/**"]}})]
    BASE_FILES = {"src/a.py": "x = 1\n"}

    def test_a_secret_past_six_mebibytes_is_found_not_skipped(self):
        secret = tok("AKIA", "ABCDEFGHIJKLMNOP")
        with mock.patch.object(g11_secrets, "MAX_BYTES", 40000):         # the real limit is 6 MiB; `test_adversarial_inputs` reads a 7 MB file against the real limit
            filler = ("filler line with nothing in it at all, only words\n" * 1100)       # about 55 KB: more than one slice
            self.assertGreater(len(filler), g11_secrets.MAX_BYTES)
            self.commit({"data/big.txt": filler + "KEY = '%s'\n" % secret}, "big")
            res = self.run_gate("g11")
        self.assertFails(res, "g11.aws-access-key")
        self.assertNotIn("g11.skipped-large", self.ids(res))

    def test_a_file_that_cannot_be_read_in_full_blocks(self):
        with mock.patch.object(g11_secrets, "MAX_BYTES", 2000), mock.patch.object(g11_secrets, "MAX_SLICES", 2):
            self.commit({"src/blob.txt": "plain words and more plain words\n" * 400}, "blob")
            res = self.run_gate("g11")
        self.assertFails(res, "g11.skipped-large")

    def test_the_plan_can_allow_the_path(self):
        with mock.patch.object(g11_secrets, "MAX_BYTES", 2000), mock.patch.object(g11_secrets, "MAX_SLICES", 2):
            self.commit({"data/blob.txt": "plain words and more plain words\n" * 400}, "blob")
            res = self.run_gate("g11", "F")
        self.assertPass(res)
        self.assertEqual([f["severity"] for f in res["findings"] if f["id"] == "g11.skipped-large"], ["warn"])


class LargeFilesExternal(GateCase):
    ITEMS = [item_doc("A", acs=1)]
    BASE_FILES = {"src/a.py": "x = 1\n", "scan.py": "import os, sys\nsys.exit(1 if os.path.exists('scan-fails') else 0)\n"}
    CFG = "secrets_scan_cmd: \"%s scan.py\"\n" % sys.executable

    def test_a_passing_external_scanner_covers_the_skipped_file(self):
        with mock.patch.object(g11_secrets, "MAX_BYTES", 2000), mock.patch.object(g11_secrets, "MAX_SLICES", 2):
            self.commit({"src/blob.txt": "plain words and more plain words\n" * 400}, "blob")
            res = self.run_gate("g11")
        self.assertPass(res)
        self.assertEqual([f["severity"] for f in res["findings"] if f["id"] == "g11.skipped-large"], ["warn"])


# -- R-GATES-4: G9 ----------------------------------------------------------------------------------------------------------------------

class SupplyBlindSpots(GateCase):
    ITEMS = _g9.Supply.ITEMS
    BASE_FILES = dict(_g9.Supply.BASE_FILES, **{
        "requirements/base.txt": "flask==2.0.1\n", "requirements/dev/test.txt": "pytest==7.0.0\n", ".npmrc": "registry=https://registry.npmjs.org/\n",
        ".yarnrc.yml": "nodeLinker: node-modules\n",
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests>=2"]\n\n[build-system]\nrequires = ["setuptools"]\n\n[tool.poetry.dependencies]\nflask = "^2"\n',
        "Cargo.toml": '[package]\nname = "x"\n\n[dependencies]\nserde = "1"\n\n[[bin]]\nname = "tool"\npath = "src/main.rs"\n'})

    def new_deps(self, res):
        return [d["name"] for d in res["evidence"]["new_dependencies"]]

    def test_requirements_in_a_directory(self):
        self.commit({"requirements/base.txt": "--extra-index-url https://example.invalid/simple\nflask==2.0.1\n"}, "index")
        self.assertFails(self.run_gate("g9", "D"), "g9.index-changed")
        self.setUp()
        self.commit({"requirements/dev/test.txt": "pytest==7.0.0\nnewthing==1.0.0\n"}, "dep")
        self.assertFails(self.run_gate("g9", "A"), "g9.undeclared-dependency")
        self.setUp()
        self.commit({"requirements/base.txt": "flask==2.0.1\nnewthing==1.0.0\n"}, "dep")
        res = self.run_gate("g9", "D")
        self.assertEqual(self.new_deps(res), ["newthing"])

    def test_the_path_rule_names_only_requirements_directories(self):
        self.assertEqual(g9_supply.manifest_kind("requirements/base.txt"), "pypi")
        self.assertEqual(g9_supply.manifest_kind("deps/requirements-dev/x/y.txt"), "pypi")
        self.assertEqual(g9_supply.manifest_kind("constraints/c.txt"), "pypi")
        self.assertIsNone(g9_supply.manifest_kind("docs/notes.txt"))
        self.assertIsNone(g9_supply.manifest_kind("requirementsfoo/x.json"))

    def test_poetry_dependency_subtables(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\n[tool.poetry.dependencies.evil]\ngit = "https://example.invalid/e.git"\n'}, "poetry sub")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))
        self.setUp()
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\n[tool.poetry.group.dev.dependencies.evil]\nversion = "1.0"\n'}, "poetry group sub")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))
        self.setUp()
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\n[tool.poetry.dependencies.evil]\nversion = "1.0"\n'}, "poetry sub, undeclared")
        self.assertFails(self.run_gate("g9", "A"), "g9.undeclared-dependency")

    def test_cargo_dependency_subtables_and_target_tables(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + '\n[dependencies.evil]\ngit = "https://example.invalid/e"\n'}, "cargo sub")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))
        self.setUp()
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + "\n[target.'cfg(unix)'.dependencies]\nevil = \"1\"\n"}, "cargo target")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))
        self.setUp()
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + "\n[target.'cfg(unix)'.dependencies.evil]\nversion = \"1\"\n"}, "cargo target sub")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))
        self.setUp()
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + "\n[workspace.dependencies]\nevil = \"1\"\n"}, "cargo workspace")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))
        self.setUp()
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + '\n[dev-dependencies.evil]\nversion = "1"\n'}, "cargo dev sub")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))

    def test_a_cargo_array_of_tables_does_not_become_a_dependency(self):
        parsed = g9_supply.parse_manifest("Cargo.toml", self.BASE_FILES["Cargo.toml"])
        self.assertEqual(parsed, {"serde": '"1"'})

    def test_build_system_requires(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"].replace('requires = ["setuptools"]', 'requires = ["setuptools", "evil @ https://example.invalid/e.tgz"]')}, "build")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))

    def test_dotted_keys_at_the_top_level(self):
        for line in ('project.dependencies = ["evil>=1"]', 'project.optional-dependencies.dev = ["evil>=1"]'):
            with self.subTest(line):
                self.setUp()
                self.commit({"pyproject.toml": line + "\n" + self.BASE_FILES["pyproject.toml"]}, "dotted")
                self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))

    def test_pdm_uv_and_hatch_dependency_lists(self):
        for table in ('[tool.pdm.dev-dependencies]\ntest = ["evil @ https://example.invalid/e.tgz"]', '[tool.uv]\ndev-dependencies = ["evil>=1"]',
                      '[tool.hatch.envs.t]\ndependencies = ["evil>=1"]', '[dependency-groups]\ndev = ["evil>=1"]'):
            with self.subTest(table):
                self.setUp()
                self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + "\n" + table + "\n"}, "lists")
                self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))

    def test_an_unknown_dependency_table_fails_closed(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\n[tool.acme.dependencies]\nevil = "git+https://example.invalid/e.git"\n'}, "unknown")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.dependency-table-unread")
        self.setUp()
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + '\n[dependencies.a.b]\ngit = "https://example.invalid/e"\n'}, "unknown cargo")
        self.assertFails(self.run_gate("g9", "D"), "g9.dependency-table-unread")

    def test_an_unchanged_unknown_table_and_ordinary_edits_do_not_fire(self):
        with_table = self.BASE_FILES["pyproject.toml"] + '\n[tool.acme.dependencies]\nx = "1"\n'
        self.assertEqual(g9_supply.unread_tables("pyproject.toml", with_table), g9_supply.unread_tables("pyproject.toml", with_table + "\n# note\n"))
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"].replace("name = \"x\"", "name = \"y\"")}, "rename")
        res = self.run_gate("g9", "D")
        self.assertNotIn("g9.dependency-table-unread", self.ids(res))
        self.assertPass(res)

    def test_yarnrc_and_npmrc_execution_settings(self):
        for path, text in ((".yarnrc.yml", "nodeLinker: node-modules\nyarnPath: .yarn/releases/evil.cjs\n"), (".npmrc", "registry=https://registry.npmjs.org/\nscript-shell=/tmp/evil.sh\n"),
                           (".npmrc", "registry=https://registry.npmjs.org/\nnode-options=--require /tmp/evil.js\n"), (".yarnrc.yml", "nodeLinker: node-modules\nenableScripts: true\n")):
            with self.subTest(text.splitlines()[-1]):
                self.setUp()
                self.commit({path: text}, "rc")
                self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")

    def test_unchanged_rc_files_and_plain_version_bumps_are_quiet(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"].replace('"requests>=2"', '"requests>=2.31"'), "requirements/base.txt": "flask==2.1.0\n"}, "bumps")
        res = self.run_gate("g9", "A")
        self.assertPass(res)


    def test_pip_tools_in_files(self):
        self.assertEqual(g9_supply.manifest_kind("requirements.in"), "pypi")
        self.assertEqual(g9_supply.manifest_kind("requirements/base.in"), "pypi")
        self.assertEqual(g9_supply.parse_manifest("requirements.in", "flask\nnewthing>=1\n"), {"flask": "*", "newthing": ">=1"})
        self.assertEqual(g9_supply._index_overrides("requirements.in", "--extra-index-url https://example.invalid/s\n"), ["--extra-index-url https://example.invalid/s"])

    def test_yarn_v1_yarn_path(self):
        self.commit({".yarnrc": 'yarn-path ".yarn/releases/evil.cjs"\n'}, "rc")
        self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")

    # -- a header the reader cannot place is not a place to hide a dependency (fail closed) --

    def cargo_with(self, tail):
        return self.BASE_FILES["Cargo.toml"] + tail

    def test_a_header_longer_than_300_characters_is_still_a_header(self):
        long_cfg = "cfg(" + "x" * 400 + ")"
        tail = "\n[target.'%s'.dependencies]\nevil = { git = \"https://example.invalid/e\" }\n" % long_cfg
        self.assertTrue([u for u in g9_supply.unread_tables("Cargo.toml", self.cargo_with(tail)) if "evil" in u])
        self.commit({"Cargo.toml": self.cargo_with(tail)}, "long header")
        self.assertFails(self.run_gate("g9", "D"), "g9.dependency-table-unread")

    def test_a_long_header_in_a_pyproject_too(self):
        tail = "\n[tool.%s.dependencies]\nevil = \"git+https://example.invalid/e.git\"\n" % ("x" * 400)
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + tail}, "long header")
        self.assertFails(self.run_gate("g9", "D"), "g9.dependency-table-unread")

    def test_an_indented_header_is_a_header(self):
        tail = '\n  [dependencies.evil]\n  git = "https://example.invalid/e"\n'
        self.assertIn("evil", g9_supply.parse_manifest("Cargo.toml", self.cargo_with(tail)))
        self.commit({"Cargo.toml": self.cargo_with(tail)}, "indented")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))
        self.setUp()
        self.commit({"Cargo.toml": self.cargo_with("\n\t[dependencies]\n\tevil = \"1\"\n")}, "indented, plain")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))
        self.setUp()
        pyproject = self.BASE_FILES["pyproject.toml"] + '\n  [tool.poetry.dependencies.evil]\n  git = "https://example.invalid/e.git"\n'
        self.commit({"pyproject.toml": pyproject}, "indented poetry")
        self.assertIn("evil", self.new_deps(self.run_gate("g9", "D")))

    def test_a_bracket_line_that_is_no_header_is_an_unread_table(self):
        for line in ("[dependencies.evil] junk", "[dependencies.evil", "[target.'cfg(a[b])'.dependencies]", "[]", "[[]]", "  [ ]  "):
            with self.subTest(line):
                got = g9_supply.unread_tables("Cargo.toml", self.cargo_with("\n%s\nevil = { git = \"https://example.invalid/e\" }\n" % line))
                self.assertTrue([u for u in got if u.startswith("unparsed table header")], got)
        got = g9_supply.unread_tables("pyproject.toml", self.BASE_FILES["pyproject.toml"] + "\n[tool.x.dependencies] junk\nevil = \"1\"\n")
        self.assertTrue([u for u in got if u.startswith("unparsed table header")], got)

    def test_an_unparsed_bracket_line_blocks_only_when_it_is_new(self):
        self.commit({"Cargo.toml": self.cargo_with("\n[dependencies.evil] junk\nevil = \"1\"\n")}, "unparsed")
        self.assertFails(self.run_gate("g9", "D"), "g9.dependency-table-unread")
        base_with = self.cargo_with("\n[dependencies.evil] junk\nx = 1\n")
        self.assertEqual(g9_supply.unread_tables("Cargo.toml", base_with), g9_supply.unread_tables("Cargo.toml", base_with + "\n# a note\n"))

    def test_headers_with_comments_and_double_brackets_still_parse(self):
        for ln, name in (("[dependencies]", "dependencies"), ("[ dependencies ] # note", "dependencies"), ("[[bin]]", "bin"), ("  [[bin]]  ", "bin"),
                         ("[target.'cfg(unix)'.dependencies]", "target.'cfg(unix)'.dependencies"), ("[a]]", "a")):
            with self.subTest(ln):
                self.assertEqual(g9_supply._toml_header(ln), name)
        for ln in ("", "x = [1]", "[", "[]", "[a] x", "[a[b]]", "# [a]"):
            with self.subTest(ln):
                self.assertIsNone(g9_supply._toml_header(ln))

    def test_an_npmrc_of_blank_lines_is_read_in_linear_time(self):
        t = time.time()
        self.commit({".npmrc": "registry=https://registry.npmjs.org/\n" + "\n" * 100000}, "blank lines")
        self.run_gate("g9", "A")
        self.assertLess(time.time() - t, 3.0)


class SupplyHostileInputs(unittest.TestCase):
    def test_hostile_pyproject_and_cargo_are_read_in_linear_time(self):
        cases = {"dependencies run": "[project]\n" + "dependencies" * 22000 + "\n", "dotted": "[project]\n" + "a." * 22000 + "\n", "spaces in header": "[" + " " * 25000 + "\n",
                 "header word": "[" + "a" * 25000 + "\n", "many tables": "[tool.x.dependencies]\n" * 2500, "target": "[target." + "a." * 22000 + "dependencies]\nx = 1\n",
                 # `^\s*` under re.M ran over every following blank line from each blank line (a quadratic scan of 200k blank lines)
                 "blank lines": "[project]\n" + "\n" * 50000, "blank lines with spaces": "[project]\n" + " \n" * 25000, "dependency table of blank lines": "[tool.x.dependencies]\n" + "\n" * 50000,
                 "blank lines, tabs": "[project]\n" + "\t\n" * 25000, "bracket lines": "[\n" * 25000, "unparsed headers": "[a] x\n" * 8000,
                 "long header with blanks": "[a" + " " * 25000 + "b.dependencies]\nx = 1\n", "long header": "[" + "a." * 25000 + "dependencies]\nx = 1\n",
                 "long indented header": "   [" + "a." * 25000 + "dependencies]\nx = 1\n", "double brackets": "[[" * 12000 + "\n", "double brackets, unclosed": "[[a\n" * 12000}
        for name, text in cases.items():
            for path in ("pyproject.toml", "Cargo.toml"):
                with self.subTest(name + " " + path):
                    t = time.time()
                    g9_supply.parse_manifest(path, text)
                    g9_supply.unread_tables(path, text)
                    g9_supply._index_overrides(path, text)
                    self.assertLess(time.time() - t, 1.0)

    def test_hostile_go_mod_gemfile_and_requirements_are_read_in_linear_time(self):
        cases = {"go.mod": ("require (\n" * 100000, "\n" * 300000, "replace x\n" * 50000, "require " * 50000, "require (\n" + "a v1\n" * 100000),
                 "Gemfile": ("gem \"" + "x" * 50 + "\n") * 5000, "Gemfile blank": "\n" * 300000, 'Gemfile source': 'source "' * 50000, "requirements.txt": "-" * 300000,
                 "package.json": "{" * 20000}
        for path, texts in cases.items():
            for text in (texts if isinstance(texts, tuple) else (texts,)):
                with self.subTest(path + " " + repr(text[:12])):
                    t = time.time()
                    g9_supply.parse_manifest(path.split()[0], text)
                    g9_supply._index_overrides(path.split()[0], text)
                    self.assertLess(time.time() - t, 1.0)

    def test_a_go_mod_is_still_read(self):
        text = "module x\n\nrequire (\n\tgithub.com/a/b v1.2.3\n\tgithub.com/c/d v0.1.0 // indirect\n)\n\nrequire github.com/e/f v2.0.0\n"
        self.assertEqual(g9_supply.parse_manifest("go.mod", text), {"github.com/a/b": "v1.2.3", "github.com/c/d": "v0.1.0", "github.com/e/f": "v2.0.0"})
        self.assertEqual(g9_supply.parse_manifest("go.mod", "require (\n\tgithub.com/a/b v1.2.3\n"), {"github.com/a/b": "v1.2.3"})       # an unclosed block is still read

    def test_a_gemfile_option_on_the_next_line_is_still_a_source(self):
        self.assertEqual(g9_supply.parse_manifest("Gemfile", 'gem "rails"\ngem "evil",\n  git: "https://example.invalid/e.git"\n'),
                         {"rails": "*", "evil": 'git: "https://example.invalid/e.git"'})


# -- R-GATES-5: G4 and file names git would quote ------------------------------------------------------------------------------------------

class G4OddNames(GateCase):
    ITEMS = [g3_item("A")]
    CFG = "gates:\n  g4: on\nmutation_cmd: \"%s mutate.py\"\nmutation_min: 60\n" % sys.executable
    BASE_FILES = dict(PKG, **{"mutate.py": MUTATE, "src/calc.py": IMPL, "tests/acceptance/test_a.py": ACCEPTANCE_OK})

    def evidence(self):
        self.lr.repo.write("docs/baseline/evidence.json", EVIDENCE)

    def test_non_ascii_and_spaced_names_are_in_the_scope(self):
        self.evidence()
        self.commit({"src/café.py": "def f():\n    return 1\n", "src/my file.py": "def g():\n    return 2\n", 'src/we"ird.py': "x = 1\n", "src/tab\tname.py": "y = 2\n"}, "odd names")
        scope = g4_mutation.changed_lines(self.ctx())
        self.assertEqual(sorted(scope), sorted(["src/café.py", "src/my file.py", 'src/we"ird.py', "src/tab\tname.py"]))

    def test_only_oddly_named_code_changed_does_not_skip(self):
        self.evidence()
        self.commit({"src/café.py": "def f():\n    return 1\n"}, "only an odd name")
        res = self.run_gate("g4")
        self.assertNotEqual(res["status"], "skipped")
        self.assertPass(res)

    def test_the_users_diff_prefix_settings_do_not_matter(self):
        self.evidence()
        self.lr.repo.git("config", "diff.noprefix", "true")
        try:
            self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
            self.assertEqual(list(g4_mutation.changed_lines(self.ctx())), ["src/calc.py"])
        finally:
            self.lr.repo.git("config", "--unset", "diff.noprefix", check=False)

    def test_a_failing_git_diff_is_an_error_not_a_skip(self):
        self.evidence()
        self.commit({"src/calc.py": IMPL + "\n# change\n"}, "work")
        with mock.patch.object(g4_mutation, "changed_lines", side_effect=ValueError("git diff base..head failed")):
            res = self.run_gate("g4")
        self.assertEqual(res["status"], "error")


if __name__ == "__main__":
    unittest.main()
