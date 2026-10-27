"""Every rule id a guard can emit has a corpus row that triggers it and an entry in docs/security/guards.md (IMP-22, IMP-24 item 8)."""
from __future__ import annotations

import glob
import json
import os
import re
import unittest

from guards import g_git, paths

from tests import REPO_ROOT

GUARDS_DIR = os.path.join(REPO_ROOT, ".claude", "scripts", "guards")
CORPUS = os.path.join(REPO_ROOT, "tests", "guards", "corpus")
DOC = os.path.join(REPO_ROOT, "docs", "security", "guards.md")

PREFIX = {"g_git.py": "guard-git", "g_infra.py": "guard-infra", "g_exec.py": "guard-exec", "g_critic.py": "guard-critic-write", "g_halt.py": "guard-halt",
          "g_config.py": "guard-config-change"}


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def emitted_rule_ids():
    ids = set()
    for fname, pre in PREFIX.items():
        for m in re.finditer(r'(?:deny|ask)\(\s*G\s*,\s*"([a-z0-9-]+)"', read(os.path.join(GUARDS_DIR, fname))):
            ids.add("%s:%s" % (pre, m.group(1)))
    for m in re.finditer(r'(?:deny|ask)\(\s*(GM|G)\s*,\s*"([a-z0-9-]+)"', read(os.path.join(GUARDS_DIR, "g_file.py"))):
        ids.add("%s:%s" % ("guard-main-edit" if m.group(1) == "GM" else "guard-file", m.group(2)))
    ids = {i for i in ids if not i.endswith("-")}
    # ids built at run time
    ids |= {"guard-file:" + cat for cat in paths.PROTECTED_DESCRIPTIONS}
    ids |= {"guard-file:engineer-" + cat for cat, _ in paths.ENGINEER_ONLY}
    ids |= {"guard-git:" + name for name in g_git.RULE_NAME.values()}
    ids.add("guard-git:" + g_git.NEVER_RUN_RULE)
    ids |= {"guard-git:" + rule for rule in g_git.GH_RULES.values()}
    ids |= {"guard-exec:nul-byte", "guard-git:gh-mutation"}
    return ids


def corpus_rule_ids():
    out = set()
    for path in glob.glob(os.path.join(CORPUS, "*.json")):
        if os.path.basename(path) == "injection.json":
            continue
        for row in json.loads(read(path)):
            if row.get("rule"):
                out.add(row["rule"])
    return out


class RuleCoverage(unittest.TestCase):
    def test_the_extraction_finds_the_rules(self) -> None:
        ids = emitted_rule_ids()
        self.assertGreaterEqual(len(ids), 100)
        for known in ("guard-git:no-verify", "guard-infra:local-plan", "guard-exec:loopctl-write", "guard-file:settings", "guard-main-edit:shared-branch",
                      "guard-halt:halted", "guard-critic-write:edit", "guard-config-change:settings-during-run"):
            self.assertIn(known, ids)

    def test_every_rule_has_a_corpus_row(self) -> None:
        missing = sorted(emitted_rule_ids() - corpus_rule_ids())
        self.assertEqual(missing, [], "rule ids with no corpus row that triggers them")

    def test_corpus_names_only_real_rules(self) -> None:
        unknown = sorted(corpus_rule_ids() - emitted_rule_ids())
        self.assertEqual(unknown, [])

    def test_every_rule_is_documented(self) -> None:
        text = read(DOC)
        missing = []
        for rid in sorted(emitted_rule_ids()):
            guard, name = rid.split(":", 1)
            documented = ("`%s`" % rid in text) or ("`%s`" % name in text) or ("`%s:%s`" % (guard, name) in text)
            if guard == "guard-file" and ("`%s`" % name in text or name.startswith("engineer-")) and name.replace("engineer-", "") in text:
                documented = True
            if not documented:
                missing.append(rid)
        self.assertEqual(missing, [], "rule ids missing from docs/security/guards.md")

    def test_corpus_is_big_enough(self) -> None:
        n = 0
        for path in glob.glob(os.path.join(CORPUS, "*.json")):
            n += len(json.loads(read(path)))
        self.assertGreaterEqual(n, 150, "the brief asks for at least 150 bypass rows")

    def test_row_ids_are_unique_across_files(self) -> None:
        seen = {}
        for path in sorted(glob.glob(os.path.join(CORPUS, "*.json"))):
            for row in json.loads(read(path)):
                self.assertNotIn(row["id"], seen, "%s duplicates %s" % (os.path.basename(path), seen.get(row["id"])))
                seen[row["id"]] = os.path.basename(path)

    def test_every_deny_row_names_a_rule_and_every_row_a_reason_or_is_obvious(self) -> None:
        for path in glob.glob(os.path.join(CORPUS, "*.json")):
            if os.path.basename(path) == "injection.json":
                continue
            for row in json.loads(read(path)):
                self.assertIn(row["expect"], ("allow", "deny", "ask"), row["id"])
                if row["expect"] != "allow":
                    self.assertTrue(row.get("rule"), row["id"])
                self.assertTrue(row.get("cmd") or row.get("path") or row.get("input") is not None or row.get("tool"), row["id"])


if __name__ == "__main__":
    unittest.main()
