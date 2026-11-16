"""Third-round review fixes, package B (R3-GATES-1 .. R3-GATES-7): the readers of TOML, go.mod, YAML and Python that the second round left blind.

One test class per finding so a regression names it. Secret-shaped values are assembled at run time: this file is not itself a finding.
"""
from __future__ import annotations

import time
import unittest

from . import GateCase
from . import test_g9_g11 as _g9
from . import test_review_fixes_b2 as _b2

from kitlib import redact
from loopctl.gates import g5_integrity, g9_supply, g11_secrets

V = "Ab12" + "Cd34Ef56" + "Gh78"
EVIL = 'evil = { git = "https://example.invalid/e" }\n'


def parsed(path, text):
    return g9_supply.parse_manifest(path, text)


def spec_of(path, text, name):
    return parsed(path, text).get(name)


class Elapsed:
    """Context manager: fails the test when the block takes longer than `limit` seconds (the bound is generous; a quadratic reader misses it by far)."""

    def __init__(self, case, label, limit=2.0):
        self.case, self.label, self.limit = case, label, limit

    def __enter__(self):
        self.t = time.time()

    def __exit__(self, *exc):
        if exc[0] is None:
            took = time.time() - self.t
            self.case.assertLess(took, self.limit, "%s took %.1fs" % (self.label, took))


# -- R3-GATES-2: a header-looking line inside an array or a multi-line string is not a header -------------------------------------------------

class TomlStateTracking(unittest.TestCase):
    POETRY_ARRAY = '[tool.poetry.dependencies]\nrequests = "1"\nx = [\n  ["a"]\n]\n' + EVIL
    POETRY_STRING = '[tool.poetry.dependencies]\nrequests = "1"\ndoc = """\n[x]\n"""\n' + EVIL
    CARGO_ARRAY = '[package]\nname = "x"\n[dependencies]\nserde = "1"\nfeat = [\n  ["a"]\n]\n' + EVIL
    CARGO_STRING = '[package]\nname = "x"\n[dependencies]\nserde = "1"\ndoc = """\n[x]\n"""\n' + EVIL

    def test_the_reviewers_inputs_show_the_dependency_after_the_array_or_string(self):
        for path, text in (("pyproject.toml", self.POETRY_ARRAY), ("pyproject.toml", self.POETRY_STRING), ("Cargo.toml", self.CARGO_ARRAY), ("Cargo.toml", self.CARGO_STRING)):
            with self.subTest(path + " " + text[-60:]):
                got = parsed(path, text)
                self.assertIn("evil", got)
                self.assertTrue(g9_supply._nonregistry(got["evil"]), got["evil"])
                self.assertEqual(g9_supply.unread_tables(path, text), [])       # nothing is unread: the reader placed every line

    def test_a_nested_array_line_is_not_an_unread_table(self):
        """It was (a false positive the second round left open): a line inside an array that starts with `[` is array content."""
        text = '[project]\nname = "x"\ndependencies = [\n  "a",\n  ["b"],\n]\n[tool.x]\nk = 1\n'
        self.assertEqual(g9_supply.unread_tables("pyproject.toml", text), [])

    def test_tables_after_a_multi_line_string_or_array_are_still_read(self):
        text = '[project]\ndescription = """\n[tool.x]\n"""\ndependencies = ["evil @ https://example.invalid/e.tgz"]\n'
        self.assertIn("evil", parsed("pyproject.toml", text))
        text = '[tool.x]\nk = [\n  [1],\n  [2],\n]\n[tool.poetry.dependencies]\n' + EVIL
        self.assertIn("evil", parsed("pyproject.toml", text))

    def test_a_dependency_hidden_in_a_string_or_array_is_not_invented(self):
        text = '[tool.poetry.dependencies]\nrequests = "1"\nnote = """\nevil = { git = "x" }\n"""\n'
        self.assertEqual(sorted(parsed("pyproject.toml", text)), ["note", "requests"])      # the line inside the string is not a key of its own
        self.assertEqual(sorted(parsed("Cargo.toml", '[dependencies]\n# [x]\nserde = "1"\n')), ["serde"])

    def test_a_string_or_array_that_is_never_closed_is_unread(self):
        for text in ('[dependencies]\nserde = "1"\nx = [\n', '[dependencies]\nx = """\n[dependencies.evil]\ngit = "u"\n', '[dependencies]\nx = "abc\n[dependencies.evil]\n',
                     '[dependencies]\nserde = "1"\n]\n'):
            with self.subTest(text[-30:]):
                got = g9_supply.unread_tables("Cargo.toml", text)
                self.assertTrue([u for u in got if u.startswith("unreadable toml")], got)

    def test_headers_inside_quotes_and_comments_do_not_confuse_the_state(self):
        text = '[dependencies]\ns = "a [ b"  # [ c\nt = \'x ] y\'\nserde = "1"\n[dependencies.evil]\ngit = "https://example.invalid/e"\n'
        got = parsed("Cargo.toml", text)
        self.assertEqual(sorted(got), ["evil", "s", "serde", "t"])
        self.assertEqual(g9_supply.unread_tables("Cargo.toml", text), [])

    def test_escaped_quotes_in_a_multi_line_string(self):
        text = '[dependencies]\nd = """a\\""" b\n[x]\n"""\n' + EVIL
        self.assertIn("evil", parsed("Cargo.toml", text))

class SupplyCase(GateCase):
    """The repository of the second round's supply tests (`SupplyBlindSpots`), without its tests."""
    ITEMS = _b2.SupplyBlindSpots.ITEMS
    BASE_FILES = _b2.SupplyBlindSpots.BASE_FILES

    def new_deps(self, res):
        return [d["name"] for d in res["evidence"]["new_dependencies"]]


class TomlStateGate(SupplyCase):
    """The same inputs through the gate: the new dependency is seen, and a git source is an error."""

    def test_poetry_array_then_git_dependency(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"] + '\nx = [\n  ["a"]\n]\n' + EVIL}, "poetry")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))

    def test_cargo_array_and_triple_quote_then_git_dependency(self):
        for tail in ('feat = [\n  ["a"]\n]\n', 'doc = """\n[x]\n"""\n'):
            with self.subTest(tail):
                self.setUp()
                base = self.BASE_FILES["Cargo.toml"].replace('serde = "1"\n', 'serde = "1"\n' + tail + EVIL)
                self.commit({"Cargo.toml": base}, "cargo")
                res = self.run_gate("g9", "D")
                self.assertFails(res, "g9.non-registry-source")
                self.assertIn("evil", self.new_deps(res))

    def test_unterminated_array_added_is_a_finding_for_a_human(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"] + "\n[dependencies.x]\nfeat = [\n"}, "open")
        self.assertFails(self.run_gate("g9", "D"), "g9.dependency-table-unread")


# -- R3-GATES-3: dotted and quoted dependency keys ---------------------------------------------------------------------------------------------

class DottedAndQuotedKeys(unittest.TestCase):
    def test_cargo_dotted_and_quoted_keys(self):
        for line, want in (('evil.git = "https://example.invalid/e"', "git"), ('evil.path = "../evil"', "path"), ('"evil" = { git = "https://example.invalid/e" }', "git"),
                           ("'evil' = { path = '../e' }", "path"), ('evil . git = "u"', "git"), ('"evil".git = "u"', "git"), ('  evil.git="u"', "git")):
            with self.subTest(line):
                text = '[package]\nname = "x"\n[dependencies]\nserde = "1"\n' + line + "\n"
                got = parsed("Cargo.toml", text)
                self.assertEqual(sorted(got), ["evil", "serde"], line)
                self.assertTrue(g9_supply._nonregistry(got["evil"]), got["evil"])
                self.assertIn(want, got["evil"])

    def test_a_dotted_key_without_a_source_is_just_a_dependency(self):
        got = parsed("Cargo.toml", '[dependencies]\nserde.workspace = true\nrand.version = "0.8"\nrand.features = ["small_rng"]\n')
        self.assertEqual(sorted(got), ["rand", "serde"])
        self.assertFalse(g9_supply._nonregistry(got["serde"]))
        self.assertFalse(g9_supply._nonregistry(got["rand"]))

    def test_several_dotted_lines_for_one_name_are_joined(self):
        got = parsed("Cargo.toml", '[dependencies]\nevil.version = "1"\nevil.git = "https://example.invalid/e"\n')
        self.assertTrue(g9_supply._nonregistry(got["evil"]), got)

    def test_poetry_quoted_and_dotted_keys(self):
        for line in ('"evil" = { git = "https://example.invalid/e" }', 'evil.git = "https://example.invalid/e"', "'evil'.path = '../e'"):
            with self.subTest(line):
                got = parsed("pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.9"\nrequests = "1"\n' + line + "\n")
                self.assertEqual(sorted(got), ["evil", "requests"])
                self.assertTrue(g9_supply._nonregistry(got["evil"]), got["evil"])

    def test_project_dependencies_with_a_quoted_key(self):
        for line in ('"dependencies" = ["evil @ https://example.invalid/e.tgz"]', "'dependencies' = ['evil']", 'project."dependencies" = ["evil"]'):
            with self.subTest(line):
                text = ('[project]\nname = "x"\n' + line + "\n") if not line.startswith("project.") else (line + '\n[project]\nname = "x"\n')
                self.assertIn("evil", parsed("pyproject.toml", text))
        self.assertIn("evil", parsed("pyproject.toml", '[project]\nname = "x"\n"optional-dependencies"."dev" = ["evil"]\n'))

    def test_a_multi_line_inline_table_is_read_whole(self):
        text = '[dependencies]\nevil = {\n  version = "1",\n  git = "https://example.invalid/e",\n}\n'
        self.assertTrue(g9_supply._nonregistry(parsed("Cargo.toml", text)["evil"]))

    def test_a_dependency_table_written_as_a_dotted_key_elsewhere_is_unread(self):
        for text in ('dependencies.evil.git = "https://example.invalid/e"\n[package]\nname = "x"\n', '[workspace]\nmembers = []\ndependencies.evil = { git = "u" }\n',
                     '[package]\nname = "x"\nbuild-dependencies.evil.path = "../e"\n'):
            with self.subTest(text[:40]):
                got = g9_supply.unread_tables("Cargo.toml", text)
                self.assertTrue([u for u in got if "evil" in u], got)
        got = g9_supply.unread_tables("pyproject.toml", '[tool.poetry]\nname = "x"\ndependencies.evil = { git = "u" }\n')
        self.assertTrue([u for u in got if "evil" in u], got)
        # a list is read, and setuptools' `{ file = … }` is a requirements file, not a source
        self.assertEqual(g9_supply.unread_tables("pyproject.toml", '[tool.setuptools.dynamic]\ndependencies = { file = ["requirements.txt"] }\n'), [])
        self.assertEqual(g9_supply.unread_tables("pyproject.toml", '[project]\ndependencies = ["a"]\n'), [])

    def test_a_statement_the_reader_cannot_read_in_a_dependency_table_is_unread(self):
        for text in ('[dependencies]\nserde = "1"\névil = { git = "https://example.invalid/e" }\n', '[dependencies]\nserde = "1"\n]\nevil = { git = "u" }\n',
                     '[dependencies]\nserde = "1"\n"evil = { git = "u" }\n', '[dependencies]\nserde = "1"\nevil { git = "u" }\n'):
            with self.subTest(text[-40:]):
                got = g9_supply.unread_tables("Cargo.toml", text)
                self.assertTrue([u for u in got if u.startswith("dependencies:")], got)
        got = g9_supply.unread_tables("pyproject.toml", '[tool.poetry.dependencies]\nrequests = "1"\névil = { git = "u" }\n')
        self.assertTrue([u for u in got if u.startswith("tool.poetry.dependencies:")], got)
        for text in ('[dependencies]\nserde = "1"\nx = [\n  "a",\n]\ny = """\n]\n"""\n# note\n\n', '[dependencies.serde]\nversion = "1"\nfeatures = ["derive"]\n'):
            with self.subTest(text[-40:]):
                self.assertEqual(g9_supply.unread_tables("Cargo.toml", text), [])

    def test_names_that_are_not_package_names_are_not_dropped(self):
        got = parsed("Cargo.toml", '[dependencies]\n"we ird" = "1"\n_under = "1"\n')
        self.assertEqual(sorted(got), ["_under", "we ird"])


class DottedKeysGate(SupplyCase):
    def test_cargo_dotted_git_and_path(self):
        for line in ('evil.git = "https://example.invalid/e"', 'evil.path = "../evil"', '"evil" = { git = "https://example.invalid/e" }'):
            with self.subTest(line):
                self.setUp()
                self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"].replace('serde = "1"\n', 'serde = "1"\n' + line + "\n")}, "dotted")
                res = self.run_gate("g9", "D")
                self.assertFails(res, "g9.non-registry-source")
                self.assertIn("evil", self.new_deps(res))

    def test_an_existing_dependency_moved_to_a_dotted_git_source(self):
        self.commit({"Cargo.toml": self.BASE_FILES["Cargo.toml"].replace('serde = "1"\n', 'serde.git = "https://example.invalid/serde"\n')}, "move")
        self.assertFails(self.run_gate("g9", "A"), "g9.source-changed")

    def test_poetry_quoted_key(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"].replace('flask = "^2"\n', 'flask = "^2"\n"evil" = { git = "https://example.invalid/e" }\n')}, "q")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))

    def test_project_dependencies_quoted_key(self):
        self.commit({"pyproject.toml": self.BASE_FILES["pyproject.toml"].replace('dependencies = ["requests>=2"]', '"dependencies" = ["requests>=2", "evil @ https://example.invalid/e.tgz"]')}, "q")
        res = self.run_gate("g9", "D")
        self.assertFails(res, "g9.non-registry-source")
        self.assertIn("evil", self.new_deps(res))

    def test_a_dotted_workspace_dependency_table_goes_to_a_human(self):
        self.commit({"Cargo.toml": 'workspace.dependencies.evil.git = "https://example.invalid/e"\n' + self.BASE_FILES["Cargo.toml"]}, "dotted ws")
        self.assertFails(self.run_gate("g9", "D"), "g9.dependency-table-unread")


# -- R3-GATES-4: linear time ---------------------------------------------------------------------------------------------------------------

class SupplyIsLinear(unittest.TestCase):
    # Sizes are just big enough for the round-2 code to take over a second: the unclosed `dependencies = [` shapes took 16 s at 136k, the registry-line
    # regex with a leading `\s*` 1.3 s at 10k blanks and 5 s at 20k. The linear code reads them in a few hundredths of a second.
    N = 60000

    def test_a_registry_file_line_of_blanks(self):
        n = 12000
        for path in (".npmrc", "pip.conf"):
            for text in (" " * n, " " * n + "x", "\t" * n, " \t" * (n // 2) + "\n" + " " * n + "registry", ("a " * (n // 2)), "registry" * (n // 8),
                         ("registryx " * (n // 10)), ("; " * (n // 2)), "#" + " " * n):
                with self.subTest(path + " " + repr(text[:10])):
                    with Elapsed(self, path, 0.5):
                        g9_supply._index_overrides(path, text)

    def test_unclosed_arrays_are_read_once(self):
        n = self.N
        for name, path, text in (("dependencies = [ repeated", "pyproject.toml", "[project]\n" + "dependencies = [\n" * (n // 17)),
                                 ("dependencies = [ on one line", "pyproject.toml", "[project]\n" + "dependencies = [" * (n // 16)),
                                 ("x = [ under [dependencies]", "Cargo.toml", "[dependencies]\n" + "x = [\n" * (n // 6)),
                                 ("x = [ under dependency-groups", "pyproject.toml", "[dependency-groups]\n" + "x = [\n" * (n // 6)),
                                 ("x = [ under tool.poetry.dependencies", "pyproject.toml", "[tool.poetry.dependencies]\n" + "x = [\n" * (n // 6)),
                                 ("quotes never closed", "pyproject.toml", "[project]\ndependencies = [" + '"a, ' * (n // 4)),
                                 ("many one-line arrays", "pyproject.toml", "[project]\n" + 'dependencies = ["a"]\n' * (n // 21)),
                                 ("many nested", "pyproject.toml", "[project]\ndependencies = " + "[" * n),
                                 ("long string", "pyproject.toml", '[project]\ndependencies = ["' + "a" * n + '"]\n'),
                                 ("many keys", "Cargo.toml", "[dependencies]\n" + "a = 1\n" * (n // 6)),
                                 ("many dotted keys", "Cargo.toml", "[dependencies]\n" + "a.b.c.d = 1\n" * (n // 12)),
                                 ("one dotted key", "Cargo.toml", "[dependencies]\n" + "a." * (n // 2) + " = 1\n"),
                                 ("blank key", "Cargo.toml", "[dependencies]\n" + " " * n + "= 1\n"),
                                 ("quotes", "Cargo.toml", "[dependencies]\n" + '"a" ' * (n // 4) + "\n"),
                                 ("open string lines", "Cargo.toml", "[dependencies]\n" + 'x = """\n' * (n // 8)),
                                 ("backslashes", "Cargo.toml", '[dependencies]\nx = "' + "\\a" * (n // 2) + "\n"),
                                 ("ml backslashes", "Cargo.toml", '[dependencies]\nx = """' + "\\" * n + "\n")):
            for fn in (g9_supply.parse_manifest, g9_supply.unread_tables, g9_supply._index_overrides):
                with self.subTest(name + " " + fn.__name__):
                    with Elapsed(self, name + " " + fn.__name__, 1.0):
                        fn(path, text)

    def test_nothing_is_lost_by_reading_once(self):
        """After an array that is never closed the later `dependencies = [` lines are inside it; the first array's items are all read."""
        text = '[project]\ndependencies = [\n  "a",\n  "b",\n' + "dependencies = [\n" * 5
        self.assertEqual(sorted(parsed("pyproject.toml", text)), ["a", "b"])
        text = '[project]\ndependencies = ["a"]\ndependencies = ["b"]\n'            # two separate arrays, both read
        self.assertEqual(sorted(parsed("pyproject.toml", text)), ["a", "b"])


# -- R3-GATES-5: YAML values are read to the next blank --------------------------------------------------------------------------------------

class YamlWholeValue(unittest.TestCase):
    def hit(self, text, path="docker-compose.yml"):
        self.assertTrue(g11_secrets.scan(text, path), (path, text))

    def miss(self, text, path="docker-compose.yml"):
        self.assertFalse(g11_secrets.scan(text, path), (path, text))

    def test_the_reviewers_compose_lines(self):
        self.hit("services:\n  db:\n    environment:\n      - POSTGRES_PASSWORD=Ab12;Cd34Ef56Gh78\n")
        self.hit("services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: Ab12,Cd34Ef56Gh78\n")
        self.hit("      POSTGRES_PASSWORD: Ab12;Cd34Ef56Gh78", "compose.yaml")
        self.hit("password: Ab12)Cd34Ef56Gh78", "app.yml")
        self.hit('      - "POSTGRES_PASSWORD=Ab12;Cd34Ef56Gh78"')

    def test_the_value_still_ends_at_a_blank(self):
        self.miss("      POSTGRES_PASSWORD: Ab12 Cd34Ef56Gh78 # a sentence")
        self.miss("      POSTGRES_PASSWORD: none, then Ab12Cd34Ef56Gh78")
        self.hit("      POSTGRES_PASSWORD: Ab12Cd34Ef56Gh78 # a comment")

    def test_placeholders_and_short_values_stay_quiet(self):
        for text in ("      POSTGRES_PASSWORD: changeme", "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}", "      POSTGRES_PASSWORD: a;b",
                     "      - POSTGRES_PASSWORD=${PW:?set it}", "password: !vault |", "password: *anchor,x", "password: &a1 Ab"):
            with self.subTest(text):
                self.miss(text)

    def test_toml_json_and_xml_keep_the_shared_cut(self):
        self.assertEqual(g11_secrets._whole_value("Ab12", "password: Ab12;Cd34Ef56Gh78", len("password: "), "a.toml"), "Ab12")
        self.assertEqual(g11_secrets._whole_value("Ab12", "password: Ab12;Cd34Ef56Gh78", len("password: "), "a.json"), "Ab12")
        self.assertEqual(g11_secrets._whole_value("Ab12", "password: Ab12;Cd34Ef56Gh78", len("password: "), "a.yml"), "Ab12;Cd34Ef56Gh78")

    def test_hostile_yaml_is_linear(self):
        for text in ("a: " + "password: " * 5000, "password: " + "x," * 25000, ("password: x;y\n" * 5000), "- " + "PASSWORD=;" * 8000):
            with self.subTest(text[:20]):
                with Elapsed(self, text[:20], 1.0):
                    g11_secrets.scan(text, "a.yml")


# -- R3-GATES-6: go.mod replace blocks -------------------------------------------------------------------------------------------------------

class GoModReplaceBlock(unittest.TestCase):
    OLD = "module x\n\nreplace (\n\ta => ../a\n)\n\nrequire a v1.0.0\n"
    NEW = "module x\n\nreplace (\n\ta => ../a\n\tgithub.com/real/pkg => github.com/evil/pkg v1.0.0\n)\n\nrequire a v1.0.0\n"

    def added(self, old, new):
        return sorted(set(g9_supply._index_overrides("go.mod", new)) - set(g9_supply._index_overrides("go.mod", old)))

    def test_a_line_added_to_an_existing_block_is_a_change(self):
        self.assertEqual(self.added(self.OLD, self.NEW), ["replace github.com/real/pkg => github.com/evil/pkg v1.0.0"])

    def test_single_line_replace_still_counts(self):
        self.assertEqual(self.added("module x\n", "module x\nreplace a => ../a\n"), ["replace a => ../a"])
        self.assertEqual(self.added("module x\n", "module x\n  replace a => ../a\n"), ["replace a => ../a"])

    def test_the_block_forms(self):
        for opener in ("replace (", "replace(", "replace (  // note", "replace\t("):
            with self.subTest(opener):
                self.assertEqual(g9_supply._index_overrides("go.mod", "module x\n%s\n\ta => ../a\n\t// comment\n)\nrequire b v1\n" % opener), ["replace a => ../a"])

    def test_lines_outside_a_block_are_not_replace_lines(self):
        self.assertEqual(g9_supply._index_overrides("go.mod", "module x\nrequire (\n\ta v1\n)\nexclude b v2\n// replace c => d\n"), [])

    def test_an_unchanged_block_is_quiet(self):
        self.assertEqual(self.added(self.NEW, self.NEW + "\n// note\n"), [])



class GoModReplaceGate(GateCase):
    ITEMS = _g9.Supply.ITEMS
    BASE_FILES = dict(_g9.Supply.BASE_FILES, **{"go.mod": GoModReplaceBlock.OLD})

    def test_a_line_added_to_a_replace_block_is_an_index_change(self):
        self.commit({"go.mod": GoModReplaceBlock.NEW}, "replace")
        self.assertFails(self.run_gate("g9", "A"), "g9.index-changed")

    def test_an_edit_elsewhere_in_go_mod_is_quiet(self):
        self.commit({"go.mod": GoModReplaceBlock.OLD.replace("v1.0.0", "v1.1.0")}, "bump")
        self.assertNotIn("g9.index-changed", self.ids(self.run_gate("g9", "A")))


# -- R3-GATES-7: the minor items ---------------------------------------------------------------------------------------------------------

class NestedDefaults(unittest.TestCase):
    def hit(self, text, path):
        self.assertTrue(g11_secrets.scan(text, path), (path, text))

    def miss(self, text, path):
        self.assertFalse(g11_secrets.scan(text, path), (path, text))

    def test_a_default_inside_a_default(self):
        for path, text in (("Dockerfile", "ENV DB_PASSWORD=${A:-${B:-%s}}" % V), ("x.env", "DB_PASSWORD=${A:-${B:-%s}}" % V), ("c.yml", "password: ${A:-${B:-%s}}" % V),
                           ("c.yml", 'password: "${A:-${B:-%s}}"' % V), ("run.sh", "export DB_PASSWORD=${A:-${B:-%s}}" % V), ("c.yml", "password: ${A:-${B:-${C:-%s}}}" % V)):
            with self.subTest(text):
                self.hit(text, path)

    def test_a_reference_followed_by_a_literal_inside_a_default(self):
        for path, text in (("Dockerfile", "ENV DB_PASSWORD=${X:-${Y}%s}" % V), ("c.yml", "password: ${X:-${Y}%s}" % V), ("c.yml", "password: ${X:-%s${Y}}" % V),
                           ("x.env", "DB_PASSWORD=${X:-${Y:-short}%s}" % V)):
            with self.subTest(text):
                self.hit(text, path)

    def test_nested_references_without_a_literal_are_placeholders(self):
        for path, text in (("Dockerfile", "ENV DB_PASSWORD=${A:-${B}}"), ("c.yml", "password: ${A:-${B:-${C}}}"), ("c.yml", "password: ${A:-${B:-changeme}}"),
                           ("c.yml", "password: ${A:-${B:-postgres}}"), ("c.yml", "password: ${A:-$B}"), ("x.env", "DB_PASSWORD=${A:-${B:?set it}}"),
                           ("c.yml", "password: ${A:-${B:-short}x}")):
            with self.subTest(text):
                self.miss(text, path)

    def test_deep_nesting_and_unclosed_references_give_up_quietly(self):
        deep = "${A:-" * 6 + V + "}" * 6
        self.assertIsInstance(g11_secrets.scan("password: " + deep, "c.yml"), list)
        for text in ("password: ${A:-${B:-" + V, "password: ${A:-${B:-" + V + "}", "password: ${"):
            with self.subTest(text):
                self.assertIsInstance(g11_secrets.scan(text, "c.yml"), list)

    def test_hostile_references_are_linear(self):
        n = 50000
        for text in ("password: ${A:-" * (n // 15), "password: ${" * (n // 12), "password: ${A:-${B:-" * (n // 20), "password: " + "${" * (n // 2), "password: ${A:-" + "${B:-" * (n // 5),
                     ("password: ${A:-${B:-${C:-x}}}\n") * (n // 28), "password: ${A:-" + "}" * n, "ENV PASSWORD=${A:-" + "${a}" * (n // 4) + "}"):
            for path in ("c.yml", "x.env", "Dockerfile"):
                with self.subTest(text[:24] + path):
                    with Elapsed(self, text[:24] + path, 1.0):
                        g11_secrets.scan(text, path)

    def test_a_default_in_an_xml_element(self):
        self.assertTrue(g11_secrets.scan("<password>${V:-%s}</password>" % V, "settings.xml"))
        self.assertTrue(g11_secrets.scan("<password>${V:-${W:-%s}}</password>" % V, "settings.xml"))
        self.assertFalse(g11_secrets.scan("<password>${V}</password>", "settings.xml"))
        self.assertFalse(g11_secrets.scan("<password>${V:-changeme}</password>", "settings.xml"))
        self.assertFalse(g11_secrets.scan("<password>$PASSWORD1234</password>", "settings.xml"))
        self.assertFalse(g11_secrets.scan("<password>${env.PW}</password>", "settings.xml"))

    def test_the_padded_element_is_a_hit_in_g11_too(self):
        for blank in (" ", "\n"):
            for pad in (60, 560, 5000):
                with self.subTest(repr(blank) + str(pad)):
                    self.assertTrue(g11_secrets.scan("<password>" + V + blank * pad + "</password>", "a.xml"))
                    self.assertFalse(g11_secrets.scan("<password>changeme" + blank * pad + "</password>", "a.xml"))

    def test_a_containerfile_is_a_dockerfile(self):
        for name in ("Containerfile", "Containerfile.prod", "build/Containerfile", "app.containerfile"):
            with self.subTest(name):
                self.hit("ENV DB_PASSWORD=%s" % V, name)
                self.hit("ENV DB_PASSWORD %s" % V, name)
                self.hit("ENV DB_PASSWORD=Ab12;Cd34Ef56Gh78 X=1", name)
                self.miss("ENV DB_PASSWORD=changeme", name)
        self.assertTrue(g11_secrets._CONFIG_NAME.search("Dockerfile"))


class PipToolsInGate(GateCase):
    ITEMS = _g9.Supply.ITEMS
    BASE_FILES = dict(_g9.Supply.BASE_FILES, **{"requirements.in": "flask\n", "requirements.txt": "flask==2.0.1\n", "requirements/dev.in": "pytest\n", "requirements/dev.txt": "pytest==7.0.0\n"})

    def test_a_compiled_requirements_file_is_the_lock_of_its_in_file(self):
        self.commit({"requirements.in": "flask\nnewthing\n", "requirements.txt": "flask==2.0.1\nnewthing==1.0.0\n"}, "pip-tools")
        res = self.run_gate("g9", "D")
        self.assertNotIn("g9.lockfile-not-updated", self.ids(res))
        self.assertIn("newthing", [d["name"] for d in res["evidence"]["new_dependencies"]])

    def test_an_in_file_without_the_compiled_file_is_still_refused(self):
        self.commit({"requirements.in": "flask\nnewthing\n"}, "no compile")
        self.assertFails(self.run_gate("g9", "D"), "g9.lockfile-not-updated")

    def test_the_compiled_file_must_be_beside_it(self):
        self.commit({"requirements/dev.in": "pytest\nnewthing\n", "requirements.txt": "flask==2.0.1\nother==1.0.0\n"}, "elsewhere")
        self.assertFails(self.run_gate("g9", "D"), "g9.lockfile-not-updated")
        self.setUp()
        self.commit({"requirements/dev.in": "pytest\nnewthing\n", "requirements/dev.txt": "pytest==7.0.0\nnewthing==1.0.0\n"}, "beside")
        self.assertNotIn("g9.lockfile-not-updated", self.ids(self.run_gate("g9", "D")))

    def test_a_containerfile_installing_without_a_frozen_lock(self):
        self.commit({"Containerfile": "FROM x\nRUN npm install\n"}, "containerfile")
        self.assertIn("g9.install-hygiene", self.ids(self.run_gate("g9", "A")))


class ModuleExitsThroughNames(unittest.TestCase):
    def exits(self, text):
        return g5_integrity.module_exits(text)

    def test_import_aliases(self):
        for text in ("import os as o\no._exit(0)\n", "from os import _exit as e\ne(0)\n", "import sys as s\ns.exit()\n", "from sys import exit as leave\nleave(1)\n",
                     "import os.path, os as operating\noperating.abort()\n", "from os import abort as stop\nstop()\n", "import builtins\nbuiltins.exit()\n",
                     "import os as o, sys\nif True:\n    o._exit(0)\n", "def f():\n    import os as o\n    o._exit(0)\nimport os as o\no._exit(0)\n"):
            with self.subTest(text[:40]):
                self.assertGreater(self.exits(text), 0)

    def test_getattr_and_dynamic_import(self):
        for text in ('import os\ngetattr(os, "_exit")(0)\n', 'getattr(__import__("os"), "_exit")(0)\n', '__import__("os")._exit(0)\n',
                     'import importlib\nimportlib.import_module("os")._exit(0)\n', 'import sys as s\ngetattr(s, "exit")()\n', 'import os\nos.execv("/bin/true", ["x"])\n'):
            with self.subTest(text[:40]):
                self.assertGreater(self.exits(text), 0)

    def test_a_module_level_call_to_a_function_that_exits(self):
        for text in ("import sys\ndef main():\n    sys.exit(0)\nmain()\n", "import os\ndef a():\n    os._exit(0)\ndef b():\n    a()\ndef c():\n    b()\nc()\n",
                     "import sys\ndef m():\n    sys.exit()\nif True:\n    m()\n",
                     "import sys as s\ndef m():\n    s.exit()\nm()\n", "import sys\ndef m():\n    raise SystemExit(1)\nm()\n"):
            with self.subTest(text[:40]):
                self.assertGreater(self.exits(text), 0)

    def test_what_stays_quiet(self):
        for text in ("import sys\ndef main():\n    sys.exit(0)\nif __name__ == '__main__':\n    main()\n", "import os\ndef f():\n    os._exit(0)\n",
                     "import os\ndef f():\n    def g():\n        os._exit(0)\n    return 1\nf()\n", "import sys\nclass A:\n    def run(self):\n        sys.exit()\ndef run():\n    pass\nrun()\n",
                     "import os as o\ndef f():\n    return o.getcwd()\nf()\n", "import sys\ndef main():\n    sys.exit(0)\nx = main\n", "import sys\ndef a():\n    b()\ndef b():\n    sys.exit()\n",
                     'import os\ngetattr(os, "getcwd")()\n', "from os import path as p\np.join('a', 'b')\n", "import sys\n\ndef m(): sys.exit()\nlam = lambda: m()\n"):
            with self.subTest(text[:40]):
                self.assertEqual(self.exits(text), 0)

    def test_the_count_grows_with_each_call(self):
        self.assertEqual(self.exits("import sys\ndef m():\n    sys.exit()\nm()\nm()\n"), 2)
        base = "import sys\ndef m():\n    sys.exit()\n"
        self.assertGreater(self.exits(base + "m()\n"), self.exits(base))

    def test_hostile_files_are_linear(self):
        chain = "import sys\ndef f0():\n    sys.exit()\n" + "".join("def f%d():\n    f%d()\n" % (i, i - 1) for i in range(1, 3000)) + "f2999()\n"
        many = "import os as o\n" + "def f(): pass\n" * 20000
        deep = "import sys\n" + "x = " + "(" * 150 + "1" + ")" * 150 + "\nsys.exit()\n"
        calls = "f" + "()" * 3000 + "\n"
        for name, text in (("a chain of 3000 functions", chain), ("20000 functions", many), ("deep parentheses", deep), ("a call chain", calls)):
            with self.subTest(name):
                with Elapsed(self, name, 1.0):
                    self.exits(text)
        self.assertGreater(self.exits(chain), 0)


class RedactAndScanAgree(unittest.TestCase):
    def test_redact_and_scan_agree_on_the_padded_element(self):
        text = "<password>" + V + " " * 560 + "</password>"
        self.assertNotIn(V, redact.redact(text))
        self.assertTrue(g11_secrets.scan(text, "a.xml"))


if __name__ == "__main__":
    unittest.main()
