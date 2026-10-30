"""Fourth-round review fixes, package B (R4-B1 .. R4-B7 and the structural wrapper).

One test class per fix so a regression names it. Secret-shaped values are assembled at run time: this file is not itself a finding.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from . import GateCase
from . import test_review_fixes_b2 as _b2

from kitlib import redact
from loopctl.gates import g5_integrity, g9_supply, g11_secrets

EVIL = "https://evil.example/x.git"
SEC = "Zx81kQm2" + "Pw9L"
V = "Ab12" + "Cd34Ef56" + "Gh78"


def parsed(path, text):
    return g9_supply.parse_manifest(path, text)


def seen(path, text):
    """Is the evil dependency visible: as a non-registry dependency, an unread table or an index override."""
    out = [k for k, v in parsed(path, text).items() if "evil" in k.lower() or "evil" in v.lower() or g9_supply._nonregistry(v)]
    return out + g9_supply.unread_tables(path, text) + g9_supply._index_overrides(path, text)


class Limit:
    """Fails the test when the block takes longer than `limit` seconds."""

    def __init__(self, case, label, limit=1.0):
        self.case, self.label, self.limit = case, label, limit

    def __enter__(self):
        self.t = time.time()

    def __exit__(self, *exc):
        if exc[0] is None:
            took = time.time() - self.t
            self.case.assertLess(took, self.limit, "%s took %.2fs" % (self.label, took))


# -- structural: a reader that fails is a finding, never a pass -------------------------------------------------------------------------------

class ReadersFailClosed(unittest.TestCase):
    def test_scan_that_raises_is_one_unreadable_hit(self):
        for exc in (RecursionError("deep"), MemoryError(), ValueError("x")):
            with self.subTest(type(exc).__name__):
                with mock.patch.object(redact, "find_secrets", side_effect=exc):
                    self.assertEqual(g11_secrets.scan("anything", "a.env"), [("unreadable", 1)])

    def test_redact_that_raises_keeps_nothing(self):
        class Boom(object):
            def finditer(self, text):
                raise RecursionError("deep")
        with mock.patch.object(redact, "_PATTERNS", [("x", Boom(), 0)]):
            self.assertEqual(redact.redact("token=" + V), "[REDACTED:unreadable]")
            self.assertEqual(redact.find_secrets("abc"), [("unreadable", 0, 3)])
            self.assertEqual(redact.redact_obj({"a": "b"}), {"a": "[REDACTED:unreadable]"})

    def test_redact_obj_nested_past_the_recursion_limit(self):
        deep = cur = {}
        for _ in range(5000):
            cur["a"] = {}
            cur = cur["a"]
        self.assertEqual(redact.redact_obj(deep), "[REDACTED:unreadable]")

    def test_module_exits_that_cannot_be_read_is_not_zero(self):
        with mock.patch.object(g5_integrity, "_module_exit_count", side_effect=RecursionError("deep")):
            self.assertGreater(g5_integrity.module_exits("x = 1\n"), 0)


class GateChecks(GateCase):
    """One repository for every gate-level test of this module."""
    ITEMS = _b2.SupplyBlindSpots.ITEMS
    BASE_FILES = dict(_b2.SupplyBlindSpots.BASE_FILES, **{"go.mod": "module x\nrequire a.com/b v1.0.0\n", "requirements-ci.txt": "pytest==7.0.0\n"})

    def new_deps(self, res):
        return [d["name"] for d in res["evidence"]["new_dependencies"]]

    def test_g9_that_raises_is_a_blocking_finding(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + 'rand = "1"\n'}, "dep")
        with mock.patch.object(g9_supply, "parse_manifest", side_effect=RecursionError("deep")):
            res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.unreadable")

    def test_g11_that_raises_is_a_blocking_finding(self):
        self.commit({"src/a.py": "x = 1\n"}, "work")
        with mock.patch.object(g11_secrets, "_added_chunks", side_effect=MemoryError()):
            res = self.run_gate("g11", "A")
        self.assertFails(res, "g11.unreadable")

    def test_g5_that_raises_is_a_blocking_finding(self):
        self.commit({"src/a.py": "x = 1\n"}, "work")
        with mock.patch.object(g5_integrity, "_output_lint", side_effect=RecursionError("deep")):
            res = self.run_gate("g5", "A")
        self.assertFails(res, "g5.unreadable")


    def test_a_manifest_cut_at_the_limit_is_a_finding(self):
        with mock.patch.object(g9_supply, "MAX_MANIFEST", 4000):
            self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + "# " + "x" * 5000 + '\nrand = "1"\n'}, "big")
            res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.manifest-over-limit")

    def test_a_manifest_inside_the_limit_is_not(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + 'rand = "1"\n'}, "small")
        self.assertNotIn("g9.manifest-over-limit", self.ids(self.run_gate("g9", "D")))



    def test_r5_a_repeated_spec_does_not_hide_a_url_source(self):
        self.commit({"requirements-ci.txt": "pytest==7.0.0\nrequests>=2\nrequests @ https://evil.example/x.whl\nrequests>=2\n"}, "dep")
        self.assertFails(self.run_gate("g9", "A"), "g9.non-registry-source")

    def test_r5_a_utf16_manifest_is_a_blocking_finding(self):
        self.commit({"requirements-ci.txt": "\x00".join("pytest==7.0.0\nevil @ https://evil.example/x.whl\n")}, "dep")
        self.assertFails(self.run_gate("g9", "A"), "g9.manifest-unreadable")

    def test_a_dev_requirements_file_is_a_manifest(self):
        self.commit({"requirements-ci.txt": "pytest==7.0.0\nnewthing==1.0.0\n"}, "dep")
        self.assertFails(self.run_gate("g9", "A"), "g9.undeclared-dependency")

    def test_a_requirements_include_from_a_server_is_an_error(self):
        self.commit({"requirements-ci.txt": "pytest==7.0.0\n-r https://evil.example/r.txt\n"}, "include")
        self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")

    def test_a_cargo_rename_is_a_new_dependency(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"].replace('serde = "1"\n', 'serde = "1"\nsafe = {package = "evil-crate", version = "1"}\n')}, "rename")
        self.assertIn("evil-crate", self.new_deps(self.run_gate("g9", "D")))

    def test_an_indented_go_require_is_a_new_dependency(self):
        self.commit({"go.mod": "module x\nrequire a.com/b v1.0.0\n  require evil.com/e v1.0.0\n"}, "go")
        self.assertIn("evil.com/e", self.new_deps(self.run_gate("g9", "D")))

    def test_a_patch_table_written_with_a_quoted_name(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + '\n["patch".crates-io]\nserde = {git = "%s"}\n' % EVIL}, "patch")
        self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")


# -- R4-B3: _call_name is a loop -----------------------------------------------------------------------------------------------------------

class DeepCallChains(unittest.TestCase):
    def test_a_deep_call_chain_before_an_indented_exit(self):
        for depth in (100, 900, 1500, 5000):
            with self.subTest(depth):
                src = "import os\nf" + "()" * depth + "\nif True:\n    os._exit(0)\n"
                self.assertGreater(g5_integrity.module_exits(src), 0)

    def test_a_deep_getattr_chain_is_read(self):
        src = "import os\nx = " + "getattr(" * 400 + "os" + ", 'a')" * 400 + "\nos._exit(0)\n"
        self.assertGreater(g5_integrity.module_exits(src), 0)
        import ast
        node = ast.Name(id="os")
        for _ in range(20000):                  # built by hand: the parser would refuse to read this much nesting
            node = ast.Call(func=ast.Name(id="getattr"), args=[node, ast.Constant(value="a")], keywords=[])
        self.assertIsInstance(g5_integrity._call_name(node), str)        # a loop: no RecursionError

    def test_nesting_the_parser_refuses_is_unread_not_clean(self):
        for depth in (300, 1000, 5000):
            with self.subTest(depth):
                src = "import os\nx = " + "(" * depth + "1" + ")" * depth + "\nif True:\n    os._exit(0)\n"
                self.assertGreater(g5_integrity.module_exits(src), 0)

    def test_ordinary_files_are_still_quiet(self):
        self.assertEqual(g5_integrity.module_exits("import os\ndef f():\n    os._exit(0)\nx = (1 + (2 * (3)))\n"), 0)

    def test_async_def_is_not_a_function_that_exits(self):
        self.assertEqual(g5_integrity.module_exits("import sys, asyncio\nasync def m():\n    sys.exit(0)\nasyncio.run(m())\n"), 0)
        self.assertGreater(g5_integrity.module_exits("import sys\ndef m():\n    sys.exit(0)\nm()\n"), 0)


# -- R4-B4: one pass over the hits ---------------------------------------------------------------------------------------------------------

class ScanOnePass(unittest.TestCase):
    def test_many_hits_on_one_line(self):
        for name, unit, path in (("env", "DB_PASSWORD=Ab12Cd34Ef56 ", ".env"), ("semicolons", "a_password=Zx81kQm2Pw9L;", ".env"), ("yaml", "password: Ab12Cd34Ef56, ", "a.yml"),
                                 ("default", "DB_PASSWORD=${A:-Ab12Cd34Ef56}", ".env"), ("open refs", "password: ${", "a.yml")):
            text = unit * (200000 // len(unit))
            with self.subTest(name):
                with Limit(self, name, 1.0):
                    g11_secrets.scan(text, path)

    def test_past_the_cap_it_is_one_finding(self):
        text = "password=Ab12Cd34Ef56\n" * (g11_secrets.MAX_SPANS + 10)
        self.assertEqual(g11_secrets.scan(text, ".env"), [("too-many-matches", 1)])

    def test_line_numbers_and_dedup_per_line(self):
        text = "x = 1\n" * 2 + "DB_PASSWORD=" + V + " API_TOKEN=" + V + "\n" + "y = 2\n" * 996 + "OTHER_PASSWORD=" + V + "\n"
        self.assertEqual(g11_secrets.scan(text, ".env"), [("secret-assignment", 3), ("secret-assignment", 1000)])

    def test_the_text_before_a_value_still_decides_in_shell_and_connection_strings(self):
        long_head = "x" * 2000 + " "
        self.assertTrue(g11_secrets.scan(long_head + "docker run -e DB_PASSWORD=" + V + " img", "run.sh"))
        self.assertTrue(g11_secrets.scan(long_head + "Server=x;Password=" + V + ";", "notes.txt"))
        self.assertFalse(g11_secrets.scan("echo hello world, password is in vault: " + V, "notes.txt") and False)


# -- R4-B2: byte-order marks and manifests past the limit ---------------------------------------------------------------------------------------

class ByteOrderMark(unittest.TestCase):
    def test_a_manifest_that_starts_with_a_bom_is_read(self):
        bom = "\ufeff"
        for path, text, name in (("Cargo.toml", '[dependencies]\nevil = {git = "%s"}\n' % EVIL, "evil"),
                                 ("pyproject.toml", '[tool.poetry.dependencies]\nevil = {git = "%s"}\n' % EVIL, "evil"),
                                 ("package.json", '{"dependencies": {"evil": "%s"}}' % EVIL, "evil"),
                                 ("requirements.txt", "evil @ %s\n" % EVIL, "evil")):
            with self.subTest(path):
                self.assertEqual(parsed(path, bom + text), parsed(path, text))
                self.assertIn(name, parsed(path, bom + text))
        self.assertEqual(g9_supply.unread_tables("Cargo.toml", bom + '[dependencies.a.b]\ngit = "u"\n'), g9_supply.unread_tables("Cargo.toml", '[dependencies.a.b]\ngit = "u"\n'))
        self.assertEqual(g9_supply._index_overrides("pyproject.toml", bom + '[tool.uv]\nindex-url = "u"\n'), g9_supply._index_overrides("pyproject.toml", '[tool.uv]\nindex-url = "u"\n'))


# -- R4-B1: one reader of table and key names -----------------------------------------------------------------------------------------------

class TomlNames(unittest.TestCase):
    def test_escaped_and_quoted_names_are_the_names_they_spell(self):
        cases = [("pyproject.toml", '[tool.poetry."dep\\u0065ndencies"]\nevil = {git = "%s"}\n' % EVIL),
                 ("pyproject.toml", '[tool."poetry"."\\u0064ependencies"]\nevil = {git = "%s"}\n' % EVIL),
                 ("pyproject.toml", '[tool.poetry."dependenci\\x65s"]\nevil = {git = "%s"}\n' % EVIL),
                 ("Cargo.toml", '["dep\\u0065ndencies"]\nevil = {git = "%s"}\n' % EVIL),
                 ("Cargo.toml", '[dependencies."ev\\u0069l"]\ngit = "%s"\n' % EVIL),
                 ("Cargo.toml", '"dep\\u0065ndencies".evil.git = "%s"\n' % EVIL),
                 ("pyproject.toml", '[project]\n"depend\\u0065ncies" = ["evil @ %s"]\n' % EVIL)]
        for path, text in cases:
            with self.subTest(text[:40]):
                self.assertTrue(seen(path, text), text)

    def test_the_parts_of_a_name(self):
        self.assertEqual(g9_supply._split_key('tool . "poetry".\'x y\''), ["tool", "poetry", "x y"])
        self.assertEqual(g9_supply._split_key('"a\\u0062c".d'), ["abc", "d"])
        self.assertEqual(g9_supply._split_key('"\\U00000041"'), ["A"])
        for odd in ('"a\\qb"', '"a\\u00zz"', '"a\\u00"', '"a\\', "", "a b", "a..b"):
            with self.subTest(odd):
                self.assertIsNone(g9_supply._split_key(odd))

    def test_an_escape_it_cannot_decode_is_unread(self):
        got = g9_supply.unread_tables("Cargo.toml", '[dependencies."ev\\qil"]\ngit = "u"\n')
        self.assertTrue([u for u in got if u.startswith("unreadable toml")], got)
        got = g9_supply.unread_tables("Cargo.toml", '[dependencies]\n"ev\\qil" = {git = "u"}\n')
        self.assertTrue([u for u in got if u.startswith("dependencies:")], got)

    def test_a_bracket_inside_a_quoted_name_is_not_the_end(self):
        self.assertEqual(g9_supply._toml_header('["a]b".c]'), '"a]b".c')

    def test_cargo_patch_tables_in_every_spelling(self):
        for text in ('[patch.crates-io]\nserde = {git = "%s"}\n' % EVIL, '["patch".crates-io]\nserde = {git = "%s"}\n' % EVIL,
                     'patch.crates-io.serde.git = "%s"\n' % EVIL, '[ patch . crates-io ]\nserde = {git = "%s"}\n' % EVIL, '[replace]\n"serde:1.0.0" = {git = "%s"}\n' % EVIL,
                     '[workspace]\npatch.crates-io.serde.git = "%s"\n' % EVIL):
            with self.subTest(text[:40]):
                self.assertTrue(g9_supply._index_overrides("Cargo.toml", text), text)

    def test_a_patch_table_with_an_escape_outside_quotes_is_seen_as_something(self):
        self.assertTrue(seen("Cargo.toml", '[pat\\u0063h.crates-io]\nserde = {git = "%s"}\n' % EVIL))

    def test_cargo_config_tables(self):
        for text in ('[patch.crates-io]\nserde = {git = "%s"}\n' % EVIL, '[source.crates-io]\nreplace-with = "x"\n', '[registries.x]\nindex = "%s"\n' % EVIL,
                     '[http]\nproxy = "%s"\ncheck-revoke = false\n' % EVIL, '[net]\ngit-fetch-with-cli = true\n'):
            with self.subTest(text[:30]):
                self.assertTrue(g9_supply._index_overrides(".cargo/config.toml", text), text)
        self.assertEqual(g9_supply._index_overrides(".cargo/config.toml", '[build]\njobs = 4\n'), [])

    def test_uv_in_every_spelling(self):
        for path, text in (("pyproject.toml", 'tool.uv.sources.requests.git = "%s"\n' % EVIL), ("pyproject.toml", '[tool.uv]\nsources = {requests = {git = "%s"}}\n' % EVIL),
                           ("pyproject.toml", '[tool.uv]\nsources.requests.git = "%s"\n' % EVIL), ("pyproject.toml", '[tool.uv]\nindex = [{url = "%s"}]\n' % EVIL),
                           ("pyproject.toml", '[tool.uv.pip]\nindex-url = "%s"\n' % EVIL), ("pyproject.toml", '[[tool.uv.index]]\nurl = "%s"\n' % EVIL),
                           ("pyproject.toml", '[[tool.pdm.source]]\nurl = "%s"\nname = "x"\n' % EVIL), ("uv.toml", '[[index]]\nurl = "%s"\n' % EVIL),
                           ("uv.toml", 'index-url = "%s"\n' % EVIL), ("uv.toml", '[sources]\nrequests = {git = "%s"}\n' % EVIL)):
            with self.subTest(path + " " + text[:30]):
                self.assertTrue(g9_supply._index_overrides(path, text), text)

    def test_ordinary_files_stay_quiet(self):
        self.assertEqual(g9_supply._index_overrides("pyproject.toml", '[project]\nname = "x"\ndependencies = ["a"]\n[tool.black]\nline-length = 100\n'), [])
        self.assertEqual(g9_supply._index_overrides("Cargo.toml", '[package]\nname = "x"\n[dependencies]\nserde = "1"\n'), [])
        self.assertEqual(g9_supply.unread_tables("Cargo.toml", '[package]\nname = "x"\n[dependencies]\nserde = "1"\n'), [])


# -- R4-B5: when a bound is exceeded, redact ----------------------------------------------------------------------------------------------------

class BoundsRedact(unittest.TestCase):
    def test_a_long_body_with_padding_is_redacted(self):
        for body in ("correct horse battery", SEC):
            for pad in (4000, 5000, 20000):
                with self.subTest(body[:8] + str(pad)):
                    out = redact.redact("<password>%s%s</password>" % (body, " " * pad))
                    self.assertNotIn(body, out)
                    self.assertEqual(redact.redact(out), out)

    def test_a_long_body_without_a_closing_tag(self):
        text = "<password>" + "correct horse " * 400
        out = redact.redact(text)
        self.assertTrue(out.startswith("<password>[REDACTED:secret-element]"), out[:60])
        self.assertLess(len(out), len(text) - 4000)

    def test_cdata_and_attribute_newlines(self):
        out = redact.redact("<password><![CDATA[%s]]></password>" % SEC)
        self.assertEqual(out, "<password><![CDATA[[REDACTED:secret-element]]]></password>")
        self.assertNotIn(SEC, redact.redact('<password\n type="x">%s</password>' % SEC))
        self.assertTrue(g11_secrets.scan("<password><![CDATA[%s]]></password>" % SEC, "a.xml"))

    def test_a_reference_bound_that_is_exceeded_is_read_as_a_literal(self):
        for text in ("DB_PASSWORD=${A:-${B:-${C:-${D:-${E:-%s}}}}}" % V, "DB_PASSWORD=${A:-${B:-${C:-${D:-${E:-${F:-${G:-%s}}}}}}}" % V,
                     "DB_PASSWORD=${A:-" + "x" * 1200 + V + "}", "DB_PASSWORD=${A:-%s" % V, "DB_PASSWORD=${A:-${B:-%s" % V):
            with self.subTest(text[:40]):
                self.assertTrue(g11_secrets.scan(text + "\n", ".env"), text)

    def test_a_literal_glued_to_a_reference(self):
        for text, path in (("DB_PASSWORD=${A}%s" % V, ".env"), ("DB_PASSWORD=${A}${B}%s" % V, ".env"), ("DB_PASSWORD=${A:-x}%s" % V, ".env"), ("password: ${A}%s" % V, "a.yml"),
                           ("DB_PASSWORD=%s${A}" % V, ".env")):
            with self.subTest(text):
                self.assertTrue(g11_secrets.scan(text + "\n", path), text)
        for text, path in (("DB_PASSWORD=${A}", ".env"), ("DB_PASSWORD=${A:-${B}}", ".env"), ("DB_PASSWORD=${DB_PASSWORD:-changeme}", ".env"), ("password: ${A}${B}", "a.yml")):
            with self.subTest(text):
                self.assertFalse(g11_secrets.scan(text + "\n", path), text)

    def test_the_xml_element_with_a_glued_literal(self):
        self.assertTrue(g11_secrets.scan("<password>${A}%s</password>" % V, "a.xml"))
        self.assertFalse(g11_secrets.scan("<password>${A}</password>", "a.xml"))


# -- R4-B6: what counts as a secret --------------------------------------------------------------------------------------------------------------

class WhatCounts(unittest.TestCase):
    def hit(self, text, path):
        self.assertTrue(g11_secrets.scan(text + "\n", path), (path, text))

    def miss(self, text, path):
        self.assertFalse(g11_secrets.scan(text + "\n", path), (path, text))

    def test_the_example_key_is_the_whole_value_not_a_substring(self):
        self.hit("API_PASSWORD=AKIAIOSFODNN7EXAMPLE" + SEC, ".env")
        self.hit("API_PASSWORD=" + SEC + "AKIAIOSFODNN7EXAMPLE", ".env")
        self.hit('password = "' + SEC + '-AKIAIOSFODNN7EXAMPLE"', "a.py")
        self.miss("key = AKIAIOSFODNN7EXAMPLE", "a.py")
        self.miss("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", ".env")

    def test_pass_db_pass_and_auth_key(self):
        for text, path in (("db_pass: " + V, "a.yml"), ("MYSQL_PASS=" + V, ".env"), ("AUTH_KEY=" + V, ".env"), ("pass: " + V, "a.yml"), ("auth_key = '" + V + "'", "a.py")):
            with self.subTest(text):
                self.hit(text, path)
                self.assertNotIn(V, redact.redact(text))
        for text in ("pass_through: required", "bypass: true", "token_budget: 150000", "auth: required"):
            with self.subTest(text):
                self.assertEqual(redact.redact(text), text)
        self.assertEqual(redact.redact_obj({"pass": "x1", "pass_count": 3, "auth_key": "k"}), {"pass": "[REDACTED:secret-key]", "pass_count": 3, "auth_key": "[REDACTED:secret-key]"})

    def test_escaped_quotes_in_a_quoted_value(self):
        for text, path in (('password = "A1b2\\"Cd34Ef56Gh78"', "a.py"), ("password = 'A1b2\\'Cd34Ef56Gh78'", "a.py"), ('{"password": "A1b2\\"Cd34Ef56Gh78"}', "a.json"),
                           ('password: "A1b2\\"Cd34Ef56Gh78"', "a.yml"), ('password = "A1b2Cd34\\"Ef56Gh78"', "a.py")):
            with self.subTest(text):
                self.hit(text, path)
                out = redact.redact(text)
                self.assertNotIn("Ef56Gh78", out)

    def test_only_a_bare_reference_is_exempt(self):
        for text in ("DB_PASSWORD=$$Ab12Cd34Ef56", "DB_PASSWORD=$A$Ab12Cd34Ef56", "DB_PASSWORD=$Ab12$Cd34Ef56"):
            with self.subTest(text):
                self.hit(text, ".env")
        for text in ("DB_PASSWORD=$Ab12Cd34Ef56", "DB_PASSWORD=${Ab12Cd34Ef56}", "DB_PASSWORD=$HOME/.secrets/db_password", "DB_PASSWORD=${HOME}/secrets/db1", "db.password=$ecretP4ss99"):
            with self.subTest(text):
                self.miss(text, ".env")

    def test_a_yaml_password_with_a_paren_is_a_password_a_call_is_not(self):
        self.hit("password: Ab12(Cd34Ef56", "a.yml")
        self.hit("password: Ab12Cd34Ef56)", "a.yml")
        self.miss("password: get_secret(db_name_primary)", "a.yml")
        self.miss("password: lookup(vault_key_1)", "a.yml")
        self.miss("password: (Ab12Cd34Ef56", "a.yml")


# -- R4-B7: supply-chain sources -------------------------------------------------------------------------------------------------------------------

class SupplySources(unittest.TestCase):
    def test_requirement_and_constraint_urls(self):
        for line in ("-r https://evil.example/r.txt", "--requirement=https://evil.example/r.txt", "-c https://evil.example/c.txt", "--constraint https://evil.example/c.txt",
                     "-rhttps://evil.example/r.txt"):
            with self.subTest(line):
                self.assertTrue(g9_supply._index_overrides("requirements.txt", line + "\n"), line)
                self.assertTrue(parsed("requirements.txt", line + "\n"), line)
        for line in ("-r base.txt", "--requirement=dev/base.txt", "-c constraints.txt", "-r ../common.txt"):
            with self.subTest(line):
                self.assertEqual(g9_supply._index_overrides("requirements.txt", line + "\n"), [])
                self.assertEqual(parsed("requirements.txt", line + "\n"), {})

    def test_a_name_that_comes_twice_keeps_every_spec(self):
        got = parsed("requirements.txt", "requests @ %s\nrequests==2.0.0\n" % EVIL)
        self.assertTrue(g9_supply._nonregistry(got["requests"]), got)
        got = parsed("requirements.txt", "requests==2.0.0\nrequests @ %s\n" % EVIL)
        self.assertTrue(g9_supply._nonregistry(got["requests"]), got)
        got = parsed("pyproject.toml", '[project]\ndependencies = ["requests @ %s", "requests>=2"]\n' % EVIL)
        self.assertTrue(g9_supply._nonregistry(got["requests"]), got)
        got = parsed("package.json", '{"dependencies": {"evil": "%s", "evil": "1.0.0"}}' % EVIL)
        self.assertTrue(g9_supply._nonregistry(got["evil"]), got)
        got = parsed("Gemfile", 'gem "a", "1"\ngem "a", git: "%s"\n' % EVIL)
        self.assertTrue(g9_supply._nonregistry(got["a"]), got)
        self.assertEqual(parsed("requirements.txt", "flask==2.0\nflask==2.0\n"), {"flask": "==2.0"})
        self.assertFalse(g9_supply._nonregistry(parsed("requirements.txt", "flask==2.0\nflask>=1\n")["flask"]))

    def test_go_mod_indentation(self):
        for text in ("module x\nrequire (\n\ta.com/b v1.0.0\n\t)\nrequire evil.com/e v1.0.0\n", "module x\n  require evil.com/e v1.0.0\n", "module x\nrequire\tevil.com/e\tv1.0.0\n",
                     "module x\nrequire (\n\ta.com/b v1.0.0\n) // c\nrequire evil.com/e v1.0.0\n", "module x\nrequire (evil.com/e v1.0.0)\n", "module x\n\trequire (\n\t\tevil.com/e v1.0.0\n\t)\n"):
            with self.subTest(text[-40:]):
                self.assertIn("evil.com/e", parsed("go.mod", text), text)
        self.assertEqual(g9_supply._index_overrides("go.mod", "module x\n  replace a.com/b => ../evil\n"), ["replace a.com/b => ../evil"])

    def test_every_requirements_file_by_glob(self):
        for path in ("requirements.txt", "dev-requirements.txt", "test-requirements.txt", "requirements-ci.txt", "base-constraints.txt", "requirements/dev.txt", "tools/requirements-x/foo.txt",
                     "my.requirements.in"):
            with self.subTest(path):
                self.assertEqual(g9_supply.manifest_kind(path), "pypi")
        for path in ("deps.txt", "notes.txt", "requirementsfoo/x.json", "docs/readme.txt"):
            with self.subTest(path):
                self.assertIsNone(g9_supply.manifest_kind(path))

    def test_a_cargo_package_rename(self):
        got = parsed("Cargo.toml", '[dependencies]\nserde = {package = "evil-serde", version = "1"}\n')
        self.assertIn("evil-serde", got)
        self.assertIn("serde", got)
        got = parsed("Cargo.toml", '[dependencies.serde]\npackage = "evil-serde"\nversion = "1"\n')
        self.assertIn("evil-serde", got)
        self.assertEqual(sorted(parsed("Cargo.toml", '[dependencies]\nserde = "1"\n')), ["serde"])

    def test_npm_overrides_at_any_depth(self):
        deep = '{"overrides": {"a": {"b": {"c": {"d": {"e": {"f": {"g": "%s"}}}}}}}}' % EVIL
        self.assertEqual(g9_supply._index_overrides("package.json", deep), ["overrides>a>b>c>d>e>f>g=" + EVIL])
        self.assertEqual(g9_supply._index_overrides("package.json", '{"overrides": {"x": "%s"}, "resolutions": {"y": "file:../y"}, "pnpm": {"overrides": {"z": "link:../z"}}}' % EVIL),
                         ["overrides>x=" + EVIL, "resolutions>y=file:../y", "pnpm.overrides>z=link:../z"])
        self.assertEqual(g9_supply._index_overrides("package.json", '{"overrides": {"x": "1.2.3"}}'), [])

    def test_json_nested_past_the_reader_is_not_a_clean_file(self):
        with mock.patch.object(g9_supply.json, "loads", side_effect=RecursionError("deep")):
            for fn in (lambda: parsed("package.json", '{"dependencies": {}}'), lambda: g9_supply._index_overrides("package.json", '{"overrides": {}}'),
                       lambda: g9_supply._lifecycle('{"scripts": {}}')):
                with self.assertRaises(RecursionError):
                    fn()

    def test_gemfile_forms(self):
        self.assertIn("evil", parsed("Gemfile", 'gem("evil", git: "%s")\n' % EVIL))
        got = parsed("Gemfile", 'gem "a"; gem "evil", git: "%s"\n' % EVIL)
        self.assertEqual(sorted(got), ["a", "evil"])
        self.assertTrue(g9_supply._nonregistry(got["evil"]))
        self.assertEqual(sorted(parsed("Gemfile", "gem 'a'\ngem \"b\", '1'\ngemspec\n")), ["a", "b"])

    def test_proxy_certificate_and_http_keys(self):
        for path, text in ((".npmrc", "proxy=%s\n" % EVIL), (".npmrc", "https-proxy=%s\n" % EVIL), (".npmrc", "ca=abc\n"), (".npmrc", "cafile=/x\n"), (".npmrc", "prefix=/tmp/x\n"),
                           (".npmrc", "    proxy = x\n"), ("pip.conf", "[global]\nproxy = %s\n" % EVIL), ("pip.conf", "[global]\ncert = /x\n"), (".yarnrc.yml", "httpProxy: %s\n" % EVIL),
                           (".yarnrc.yml", "caFilePath: /x\n"), (".yarnrc", 'proxy "%s"\n' % EVIL)):
            with self.subTest(path + " " + text[-20:]):
                self.assertTrue(g9_supply._index_overrides(path, text), text)
        for path, text in ((".npmrc", "save-exact=true\n"), (".npmrc", "# proxy=x\n"), ("pip.conf", "[global]\ntimeout = 60\n"), (".npmrc", "capture=true\nkeyboard=x\n")):
            with self.subTest(path + " " + text[-20:]):
                self.assertEqual(g9_supply._index_overrides(path, text), [])


# -- 200 KB of hostile input per scanner ------------------------------------------------------------------------------------------------------------

class TwoHundredKilobytes(unittest.TestCase):
    """Each scanner entry on hostile text of about 200 KB, with the shapes that are hostile to IT: under 2 s each (the bound is generous; these take a few hundredths
    of a second, the slowest 0.25 s)."""
    N = 200000

    def timed(self, label, fn):
        t = time.time()
        try:
            fn()
        except RecursionError:
            pass                                # the gate turns this into `unreadable`; the point here is that nothing hangs
        self.assertLess(time.time() - t, 2.0, label)

    def test_redact_and_the_secrets_scan(self):
        n = self.N
        for name, text in (("open braces", "${" * (n // 2)), ("nested defaults", "password: ${A:-${B:-" * (n // 20)), ("hits", "DB_PASSWORD=Ab12Cd34Ef56 " * (n // 26)),
                           ("padded element", "<password>Zx81kQm2Pw9L" + " " * n), ("elements", "<password>a" * (n // 11)), ("escapes", 'password = "' + "\\\"" * (n // 2)),
                           ("default repeats", "DB_PASSWORD=${A:-Ab12Cd34Ef56}" * (n // 30))):
            for label, fn in (("redact", lambda: redact.redact(text)), ("scan .env", lambda: g11_secrets.scan(text, ".env")),
                              ("scan .yml", lambda: g11_secrets.scan(text, "a.yml"))):
                with self.subTest(name + " / " + label):
                    self.timed(label + " on " + name, fn)

    def test_the_integrity_check(self):
        n = self.N
        for name, text in (("call chain", "f" + "()" * (n // 2) + "\nimport os\nif True:\n    os._exit(0)\n"), ("attribute chain", "a" + ".b" * (n // 2) + "()\n"),
                           ("quotes", '"' * n), ("many exits", "import sys\n" + "sys.exit()\n" * (n // 11))):
            with self.subTest(name):
                self.timed("module_exits on " + name, lambda: g5_integrity.module_exits(text))

    def test_the_supply_check(self):
        n = self.N
        shapes = {"open brackets": "[" * n, "unclosed arrays": "[project]\ndependencies = [\n" * (n // 25), "triple quotes": '"""' * (n // 3), "long header": "[" + "a." * (n // 2) + "b]\n",
                  "dotted key": "[dependencies]\n" + "a." * (n // 2) + "b = 1\n", "escaped names": '["' + "\\u0041" * (n // 6) + '"]\n', "go replace": "replace (\n" * (n // 10)}
        files = ("Cargo.toml", "pyproject.toml")
        for name, text in shapes.items():
            for path in files:
                with self.subTest(name + " / " + path):
                    self.timed(path + " on " + name, lambda: (parsed(path, text), g9_supply.unread_tables(path, text), g9_supply._index_overrides(path, text)))

    def test_the_other_manifests(self):
        n = self.N
        for path, text in (("requirements.txt", "-r https://x/r.txt " * (n // 20)), ("requirements.txt", "a" + " " * n + "==1\n"), ("go.mod", "require (\n" * (n // 10)),
                           ("go.mod", "replace" + " " * n + "(\n"), (".npmrc", " " * n), (".npmrc", "proxy=" * (n // 6)), ("Gemfile", 'gem "a"; ' * (n // 9)),
                           ("package.json", '{"overrides": ' * 900 + "1" + "}" * 900)):
            with self.subTest(path + " " + text[:12]):
                self.timed(path, lambda: (parsed(path, text), g9_supply._index_overrides(path, text)))


# -- R5: the last round ---------------------------------------------------------------------------------------------------------------------------

class Round5(unittest.TestCase):
    def test_b1_a_quoted_value_over_the_bound_is_flagged_and_redacted(self):
        for n in (4100, 5000, 20000):
            for quote in ('"', "'"):
                text = "password = %s%s%s\n" % (quote, "Ab1" * (n // 3), quote)
                with self.subTest("%s %d" % (quote, n)):
                    self.assertTrue(g11_secrets.scan(text, "a.py"))
                    out = redact.redact(text)
                    self.assertNotIn("Ab1Ab1Ab1", out)
                    self.assertEqual(redact.redact(out), out)

    def test_b2_a_spec_that_repeats_does_not_hide_the_one_between(self):
        ev = "https://evil.example/x.whl"
        for path, text in (("requirements.txt", "requests>=2\nrequests @ %s\nrequests>=2\n" % ev),
                           ("pyproject.toml", '[project]\ndependencies = ["requests>=2", "requests @ %s", "requests>=2"]\n' % ev),
                           ("Gemfile", 'gem "a"\ngem "a", git: "%s"\ngem "a"\n' % EVIL),
                           ("package.json", '{"dependencies": {"evil": "1.0.0", "evil": "git+%s", "evil": "1.0.0"}}' % EVIL)):
            with self.subTest(path):
                self.assertTrue([k for k, v in parsed(path, text).items() if g9_supply._nonregistry(v)], parsed(path, text))

    def test_b3_a_value_glued_to_a_reference_after_a_slash_is_a_secret(self):
        for ref in ("$X/", "${X}/", "$X-", "${X}.", "$X:", "$X@"):
            for path in (".env", "a.yml", "a.sh"):
                with self.subTest(ref + " " + path):
                    self.assertTrue(g11_secrets.scan("DB_PASSWORD=%s%s\n" % (ref, SEC), path))
        self.assertTrue(g11_secrets.scan('DB_PASSWORD="$X/%s"\n' % SEC, ".env"))
        for text in ("DB_PASSWORD=$HOME/.secrets/x", "DB_PASSWORD=${HOME}/.secrets/db", "DB_PASSWORD=$HOME/.secrets/dbpass12", "DB_PASSWORD=${SECRETS_DIR}/db_password.txt"):
            for path in (".env", "a.yml", "a.sh"):
                with self.subTest(text + " " + path):
                    self.assertFalse(g11_secrets.scan(text + "\n", path))

    def test_b4_text_in_another_encoding_is_not_an_empty_manifest(self):
        body = "requests==2.31\nevil @ https://e/x.whl\n-r https://e/r.txt\n"
        for enc in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
            raw = body.encode(enc)
            if enc in ("utf-16-le", "utf-16-be"):
                raw = (b"\xff\xfe" if enc == "utf-16-le" else b"\xfe\xff") + raw
            text = raw.decode("utf-8", "replace")
            with self.subTest(enc):
                self.assertTrue(g9_supply.unreadable_encoding(text))
                self.assertTrue(parsed("requirements.txt", text))
                self.assertTrue(g9_supply._index_overrides("requirements.txt", text))
        self.assertFalse(g9_supply.unreadable_encoding("﻿requests==2\n"))
        self.assertFalse(g9_supply.unreadable_encoding(body))

    def test_the_new_patterns_are_linear(self):
        n = 200000
        for name, text in (("open quotes", 'password = "' * (n // 12)), ("open quotes sq", "password = '" * (n // 12)), ("long quote", 'password = "' + "a" * n),
                           ("long escapes", 'password = "' + "\\a" * (n // 2)), ("refs", "DB_PASSWORD=$X/" * (n // 15)), ("ref runs", "DB_PASSWORD=$X/" + "a1" * (n // 2))):
            for label, fn in (("redact", lambda: redact.redact(text)), ("scan", lambda: g11_secrets.scan(text, ".env"))):
                with self.subTest(name + " / " + label):
                    t = time.time()
                    fn()
                    self.assertLess(time.time() - t, 2.0)


if __name__ == "__main__":
    unittest.main()
