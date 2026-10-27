"""The L5 eligibility classifier (report 6.2 L5, B5, DEC-3, DEC-38): it answers a question and merges nothing. Pure rules first, then the command."""
from __future__ import annotations

import copy
import json
import os
import re
import unittest

from kitlib import config as kconfig

from loopctl import cli, extras, merge_class as mc

from .. import LoopTestCase, item_doc
from . import EVIDENCE

HEAD = "a" * 40
LOW = ["docs/**", "**/*.md"]
GOOD_ITEM = item_doc("A", riskClass="low", touches={"domains": ["docs"], "paths": ["docs/**"], "schema": False, "deps_change": False}, migrations=[], waivers=[])
GOOD_GATES = {"g4": {"status": "skipped", "mode": "on", "head": HEAD, "blocking": False}, "g6": {"status": "pass", "mode": "on", "head": HEAD, "blocking": False}}
GATED_DEPLOY = [{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "gated-by-environment"}]


def facts(**over):
    f = {"item_id": "A", "item": copy.deepcopy(GOOD_ITEM), "state_item": {"gates": copy.deepcopy(GOOD_GATES), "counters": {"ci_fix": 0}}, "policy": "auto-low-risk",
         "autonomy_level": "L5", "effective_level": "L5", "evidence": True, "promotion_attested": "2026-10-20", "mutation": {"explicit": True, "mode": "on"},
         "deploy": {"ok": True, "reasons": []}, "files": ["docs/guide.md"], "low_risk_paths": LOW, "always_human_paths": [], "head": HEAD, "gate_modes": {"g6": "on", "g4": "on"}}
    f.update(over)
    return f


def failing(verdict):
    return [r["id"].split(".", 1)[1] for r in verdict["rules"] if not r["ok"]]


class Rules(unittest.TestCase):
    def test_a_documentation_change_with_everything_in_order_is_eligible_and_still_merges_nothing(self):
        v = mc.classify(facts())
        self.assertTrue(v["eligible"], v["reasons"])
        self.assertEqual((v["class"], v["action"], v["merges"]), ("docs", "human-merge", False))
        self.assertIn("a person merges", v["note"])

    def test_each_rule_fails_closed(self):
        broken = {
            "policy": facts(policy="human"),
            "level": facts(effective_level="L4"),
            "level unknown": facts(effective_level=None),
            "level not configured": facts(autonomy_level="L4"),
            "evidence": facts(evidence=False),
            "promotion": facts(promotion_attested=""),
            "promotion junk": facts(promotion_attested="soon"),
            "mutation": facts(mutation={"explicit": False, "mode": "off"}),
            "mutation report": facts(mutation={"explicit": True, "mode": "report"}),
            "deploy": facts(deploy={"ok": False, "reasons": ["x"]}),
            "deploy not judged": facts(deploy=None),
            "no files": facts(files=[]),
            "risk": facts(item=dict(GOOD_ITEM, riskClass="medium")),
            "migration": facts(item=dict(GOOD_ITEM, migrations=[{"name": "m"}])),
            "schema": facts(item=dict(GOOD_ITEM, touches={"domains": ["docs"], "paths": ["docs/**"], "schema": True, "deps_change": False})),
            "deps": facts(item=dict(GOOD_ITEM, touches={"domains": ["docs"], "paths": ["docs/**"], "schema": False, "deps_change": True})),
            "domain": facts(item=dict(GOOD_ITEM, touches={"domains": ["payments"], "paths": ["docs/**"], "schema": False, "deps_change": False})),
            "waiver": facts(item=dict(GOOD_ITEM, waivers=[{"approver": "a"}])),
            "no item": facts(item={}),
            "gate missing": facts(state_item={"gates": {}, "counters": {}}),
            "gate on another head": facts(state_item={"gates": {"g4": dict(GOOD_GATES["g4"], head="b" * 40), "g6": GOOD_GATES["g6"]}, "counters": {}}),
            "gate failed": facts(state_item={"gates": dict(GOOD_GATES, g6={"status": "fail", "mode": "on", "head": HEAD, "blocking": True}), "counters": {}}),
            "gate skipped that must run": facts(state_item={"gates": dict(GOOD_GATES, g6={"status": "skipped", "mode": "on", "head": HEAD}), "counters": {}}),
            "ci fix": facts(state_item={"gates": GOOD_GATES, "counters": {"ci_fix": 1}}),
            "a report-mode gate that failed": facts(gate_modes={"g6": "report", "g4": "on"},
                                                    state_item={"gates": dict(GOOD_GATES, g6={"status": "fail", "mode": "report", "head": HEAD, "blocking": False}), "counters": {}}),
        }
        for label, f in broken.items():
            with self.subTest(label):
                v = mc.classify(f)
                self.assertFalse(v["eligible"], label)
                self.assertTrue(v["reasons"])
                self.assertEqual((v["action"], v["merges"]), ("human-merge", False))

    def test_a_gate_that_had_nothing_to_judge_may_have_skipped_but_not_every_gate(self):
        modes = {"g6": "on", "g4": "on", "g9": "on"}
        ok = facts(gate_modes=modes, state_item={"gates": dict(GOOD_GATES, g9={"status": "skipped", "mode": "on", "head": HEAD}), "counters": {}})
        self.assertTrue(mc.classify(ok)["eligible"])
        bad = facts(gate_modes=modes, state_item={"gates": dict(GOOD_GATES, g9={"status": "pass", "mode": "on", "head": HEAD}, g6={"status": "skipped", "mode": "on", "head": HEAD}), "counters": {}})
        self.assertIn("gates", failing(mc.classify(bad)))

    def test_user_visible_copy_needs_a_feature_flag_and_documents_do_not(self):
        copy_item = dict(GOOD_ITEM)
        v = mc.classify(facts(files=["web/locales/en.json"], low_risk_paths=LOW + ["web/locales/**"], item=copy_item))
        self.assertEqual(failing(v), ["flag"])
        v = mc.classify(facts(files=["web/locales/en.json"], low_risk_paths=LOW + ["web/locales/**"], item=dict(copy_item, ext={"featureFlag": "new-copy"})))
        self.assertTrue(v["eligible"], v["reasons"])
        self.assertEqual(v["class"], "copy")
        for bad in ("x y", "a;b", "", None, 5):
            v = mc.classify(facts(files=["web/locales/en.json"], low_risk_paths=LOW + ["web/locales/**"], item=dict(copy_item, ext={"featureFlag": bad})))
            self.assertIn("flag", failing(v), bad)


class Paths(unittest.TestCase):
    def rows(self, paths, low=LOW, human=()):
        rows, cls = mc.path_rows(paths, low, human)
        return {r["path"]: r for r in rows}, cls

    def test_what_is_low_risk(self):
        rows, cls = self.rows(["docs/a.md", "README.md", "docs/img/x.png", "guide/deep/er/notes.rst"], low=LOW + ["guide/**"])
        self.assertTrue(all(r["ok"] for r in rows.values()), rows)
        self.assertEqual(cls, "docs")
        self.assertEqual(self.rows(["docs/app.js"])[1], "copy")

    def test_nothing_that_a_human_owns_or_that_names_a_sensitive_thing_is_low_risk(self):
        never = [".github/workflows/ci.yml", ".claude/loop.config.md", ".claude/agents/x.md", "plugin/a.md", ".mcp.json", "CODEOWNERS", "infra/main.tf", "db/migrate/1.sql",
                 "migrations/001.sql", "package.json", "package-lock.json", "pyproject.toml", "jest.config.js", "Dockerfile", "docker-compose.yml", "vercel.json", "k8s/app.yaml",
                 "docs/Dockerfile", ".env", ".env.production", "src/auth/login.py", "src/payments/stripe.ts", "db/schema.sql", "src/billing.py", "terraform/main.tf",
                 "skills/x/SKILL.md", ".husky/pre-commit", "CNAME", "app/oauth_callback.py"]
        rows, _ = self.rows(never, low=LOW + ["**"])             # even a project that lists everything as low-risk cannot widen the never-list
        for p in never:
            self.assertFalse(rows[p]["ok"], p)

    def test_case_tricks_dot_dot_and_odd_names_do_not_get_through(self):
        tricks = [".GitHub/workflows/ci.yml", ".GITHUB/workflows/ci.yml", ".Claude/settings.json", "Infra/main.tf", "DOCS/../.github/workflows/x.yml", "docs/../.claude/x.md",
                  "/etc/passwd", "docs\\..\\.github\\x.yml", "docs/./../x", "~/x.md", "-rf.md", "docs//a.md", "", "docs/a.md\x00.js", ".git/config", "docs/.git/hooks/x.md",
                  "C:/x.md", "PACKAGE.JSON", "Package-Lock.json", "DOCKERFILE", "SRC/AUTH/x.py"]
        rows, _ = self.rows(tricks, low=LOW + ["**"])
        for p in tricks:
            r = rows[mc.common.clean_text(p, 120)]
            self.assertFalse(r["ok"], p)

    def test_symlinks_and_submodules_are_never_low_risk_even_under_docs(self):
        rows, _ = mc.path_rows([{"path": "docs/link.md", "old_mode": "000000", "new_mode": "120000"}, {"path": "docs/sub", "old_mode": "160000", "new_mode": "160000"},
                                {"path": "docs/ok.md", "old_mode": "100644", "new_mode": "100644"}], LOW + ["docs/**"], [])
        self.assertEqual([r["ok"] for r in rows], [False, False, True])

    def test_the_projects_own_always_human_paths_apply_in_any_case(self):
        rows, _ = self.rows(["docs/legal/terms.md", "Docs/Legal/Terms.md", "docs/guide.md"], human=["docs/legal/**"])
        self.assertEqual([rows[p]["ok"] for p in ("docs/legal/terms.md", "Docs/Legal/Terms.md", "docs/guide.md")], [False, False, True])


class Deploy(unittest.TestCase):
    """B5, from the attestation: L5 is never available where a merge to main deploys without a human gate."""
    AT = {"deploy_surface.attested_at": "2026-10-20", "deploy_surface.fingerprint": "f" * 64}

    def verdict(self, **cfg):
        values = dict(self.AT)
        values.update(cfg)
        return mc.deploy_verdict(lambda k, d=None: values.get(k, d))

    def test_a_gated_or_deploy_free_repo_passes(self):
        self.assertTrue(self.verdict()["ok"])
        self.assertTrue(self.verdict(deploy_triggers=GATED_DEPLOY)["ok"])
        self.assertTrue(self.verdict(deploy_triggers=[{"workflow": ".github/workflows/x.yml", "fires_on": "none", "gate": "manual"}])["ok"])
        self.assertTrue(self.verdict(git_integrated_hosts=[{"host": "vercel", "previews_on": "label", "production_on": "none", "gate": "none"}])["ok"])

    def test_every_way_of_deploying_on_main_without_a_gate_is_refused(self):
        bad = {
            "ungated push to main": dict(deploy_triggers=[{"workflow": "d.yml", "fires_on": "push:main", "gate": "none"}]),
            "ungated release": dict(deploy_triggers=[{"workflow": "d.yml", "fires_on": ["release"], "gate": "none"}]),
            "manual does not hold for a push": dict(deploy_triggers=[{"workflow": "d.yml", "fires_on": "push:main", "gate": "manual"}]),
            "a gate name that is not one": dict(deploy_triggers=[{"workflow": "d.yml", "fires_on": "push:main", "gate": "Gated-By-Environment"}]),
            "a gate that is a boolean": dict(deploy_triggers=[{"workflow": "d.yml", "fires_on": "push:main", "gate": True}]),
            "no gate at all": dict(deploy_triggers=[{"workflow": "d.yml", "fires_on": "push:main"}]),
            "not a mapping": dict(deploy_triggers=["deploy.yml"]),
            "a mapping missing fires_on": dict(deploy_triggers=[{"workflow": "d.yml", "gate": "gated-by-environment"}]),
            "a host that ships main": dict(git_integrated_hosts=[{"host": "vercel", "previews_on": "none", "production_on": "main", "gate": "none"}]),
            "a host that is unknown": dict(git_integrated_hosts=[{"host": "netlify", "gate": "none"}]),
            "a host without a name": dict(git_integrated_hosts=[{"previews_on": "none", "production_on": "none"}]),
            "triggers that are not a list": dict(deploy_triggers="deploy.yml"),
            "an empty-looking entry beside a good one": dict(deploy_triggers=GATED_DEPLOY + [{}]),
        }
        for label, cfg in bad.items():
            with self.subTest(label):
                v = self.verdict(**cfg)
                self.assertFalse(v["ok"], label)
                self.assertTrue(v["reasons"])

    def test_an_unattested_surface_is_never_clear(self):
        v = mc.deploy_verdict(lambda k, d=None: d)
        self.assertFalse(v["ok"])
        self.assertIn("not attested", v["reasons"][0])
        self.assertFalse(self.verdict(**{"deploy_surface.fingerprint": ""})["ok"])

    def test_an_item_crafted_to_look_eligible_on_a_repo_that_deploys_on_main_is_not(self):
        crafted = facts(deploy=mc.deploy_verdict(lambda k, d=None: {"deploy_surface.attested_at": "2026-10-20", "deploy_surface.fingerprint": "f" * 64,
                                                                     "deploy_triggers": [{"workflow": ".github/workflows/deploy.yml", "fires_on": "push:main", "gate": "none"}]}.get(k, d)))
        v = mc.classify(crafted)
        self.assertFalse(v["eligible"])
        self.assertEqual(failing(v), ["deploy"])                    # every other rule holds: policy, level, evidence, mutation, a docs-only diff, a low-risk item
        self.assertIn("deploy.yml", v["reasons"][0])


class Command(LoopTestCase):
    CFG = ("autonomy_level: L5\nmerge_policy: auto-low-risk\npromotion_attested: '2026-10-20'\ngates:\n  g4: 'on'\ndeploy_surface:\n  attested_at: '2026-10-20'\n  fingerprint: '%s'\n" % ("f" * 64))

    def repo(self, extra="", evidence=True):
        lr = self.lr(items=[item_doc("A", riskClass="low", touches={"domains": ["docs"], "paths": ["docs/**"], "schema": False, "deps_change": False})],
                     extra_cfg=self.CFG + extra)
        if evidence:
            lr.repo.write("docs/baseline/evidence.json", json.dumps(EVIDENCE))
            lr.commit_all("evidence")
        lr.fast_start()
        lr.loop().begin("A", lr.session)
        head = lr.repo.head()
        lr.loop().record_gates("A", [("g4", {"status": "skipped", "mode": "on", "head": head, "blocking": False, "findings": []})], lr.session)
        return lr, head

    def run_cmd(self, lr, *args, level="L5"):
        old, extras.LEVEL_FN = extras.LEVEL_FN, (lambda ctx: level)
        try:
            return lr.ctl("merge-class", "A", *args)
        finally:
            extras.LEVEL_FN = old

    def test_a_docs_only_item_is_eligible_exits_0_and_changes_nothing(self):
        lr, head = self.repo()
        before = lr.repo.git("for-each-ref", "--format=%(refname) %(objectname)")
        r = self.run_cmd(lr, "--paths", "docs/guide.md", "--json")
        self.assertEqual(r.rc, 0, r)
        v = r.json
        self.assertTrue(v["eligible"], v["reasons"])
        self.assertTrue(v["what_if"])
        self.assertEqual((v["action"], v["merges"]), ("human-merge", False))
        self.assertEqual(lr.repo.git("for-each-ref", "--format=%(refname) %(objectname)"), before)
        self.assertIn("a person still merges", self.run_cmd(lr, "--paths", "docs/guide.md").out)

    def test_each_precondition_is_read_from_the_base_and_the_doctors_level(self):
        lr, head = self.repo()
        self.assertEqual(self.run_cmd(lr, "--paths", "docs/guide.md", "--json", level="L4").rc, 1)           # the doctor says L4
        self.assertEqual(self.run_cmd(lr, "--paths", "src/auth.py", "--json").rc, 1)
        self.assertEqual(self.run_cmd(lr, "--paths", ".github/workflows/ci.yml", "--json").rc, 1)
        # no level at all (the doctor could not be read) is not L5
        old, extras.LEVEL_FN = extras.LEVEL_FN, (lambda ctx: None)
        try:
            self.assertEqual(lr.ctl("merge-class", "A", "--paths", "docs/guide.md", "--json").rc, 1)
        finally:
            extras.LEVEL_FN = old

    def test_without_the_opt_in_nothing_is_eligible_however_good_the_diff(self):
        lr = self.lr(items=[item_doc("A", riskClass="low", touches={"domains": ["docs"], "paths": ["docs/**"], "schema": False, "deps_change": False})])
        lr.fast_start()
        r = self.run_cmd(lr, "--paths", "docs/guide.md", "--json")
        self.assertEqual(r.rc, 1)
        self.assertFalse(r.json["eligible"])
        self.assertIn("policy", failing(r.json))
        self.assertIn("evidence", failing(r.json))

    def test_unknown_items_and_a_head_that_is_an_option_are_usage_errors(self):
        lr, _ = self.repo()
        self.assertEqual(lr.ctl("merge-class", "ZZ").rc, 2)
        self.assertEqual(lr.ctl("merge-class", "A", "--head", "--upload-pack=x").rc, 2)
        self.assertEqual(lr.ctl("merge-class", "../A").rc, 2)

    def test_a_branch_diff_is_read_from_git_including_a_symlink_and_a_rename(self):
        lr, head = self.repo()
        lr.repo.git("switch", "-q", "-c", "feat/slice-a-a1")                     # the item's branch: the diff is merge-base(main)..HEAD
        lr.repo.write("docs/ok.md", "hello\n")
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "docs")
        h2 = lr.repo.head()
        old, extras.LEVEL_FN = extras.LEVEL_FN, (lambda ctx: "L5")
        try:
            lr.loop().record_gates("A", [("g4", {"status": "skipped", "mode": "on", "head": h2, "blocking": False, "findings": []})], lr.session)
            ok = lr.ctl("merge-class", "A", "--head", "HEAD", "--json")
            self.assertEqual(ok.rc, 0, ok)
            self.assertEqual([f["path"] for f in ok.json["files"]], ["docs/ok.md"])
            os.symlink("../.github/workflows/ci.yml", os.path.join(lr.path, "docs", "sneaky.md"))
            lr.repo.git("add", "-A")
            lr.repo.git("commit", "-q", "-m", "link")
            lr.loop().record_gates("A", [("g4", {"status": "skipped", "mode": "on", "head": lr.repo.head(), "blocking": False, "findings": []})], lr.session)
            bad = lr.ctl("merge-class", "A", "--json")
            self.assertEqual(bad.rc, 1)
            self.assertIn("paths", failing(bad.json))
            self.assertIn("symbolic link", json.dumps(bad.json["files"]))
        finally:
            extras.LEVEL_FN = old


class NoCodePathMerges(unittest.TestCase):
    """Hard rule: no code path runs `gh pr merge` or merges to the default branch (IMP-1, DEC-3)."""

    def test_merge_policy_is_a_closed_enum_that_defaults_to_human_and_cannot_be_smuggled_open(self):
        reg = kconfig.registry_mod.load_registry() if hasattr(kconfig, "registry_mod") else __import__("kitlib.registry", fromlist=["x"]).load_registry()
        key = reg.get("merge_policy")
        self.assertEqual((key.default, key.choices, key.h6_protected), ("human", ("human", "auto-low-risk"), True))
        self.assertTrue(key.is_gated_value("auto-low-risk"))
        self.assertFalse(key.is_gated_value("human"))
        for bad in ("auto", "automerge", "auto-merge", "pr-automerge", "always", "true", "yes", "human; gh pr merge --auto", "Human", " human", "", None, True, 1,
                    ["auto-low-risk"], "auto-low-risk\ngh pr merge"):
            with self.subTest(repr(bad)):
                self.assertIsNotNone(key.check_value(bad), bad)
        for line in ("merge_policy: auto-merge", "merge_policy: [human]", "merge_policy: 'human\\ngh pr merge'", "merge_policy: true"):
            cfg = kconfig.load_text("```yaml\n%s\n```\n" % line)
            self.assertTrue(cfg.errors, line)
        self.assertEqual(kconfig.load_text("```yaml\ndelivery: local\n```\n").get("merge_policy"), "human")

    def test_no_source_file_runs_gh_pr_merge_and_the_new_modules_merge_nothing(self):
        scripts = os.path.join(os.path.dirname(os.path.abspath(cli.__file__)), "..")
        banned = re.compile(r"""["']pr["']\s*,\s*["']merge["']|\bgh\s+pr\s+merge\b.*\b(run|Popen|run_git|call)\b""")
        seen = 0
        for sub in ("loopctl", "doctorlib"):
            for dirpath, _d, names in os.walk(os.path.join(scripts, sub)):
                for n in names:
                    if n.endswith(".py"):
                        with open(os.path.join(dirpath, n), encoding="utf-8") as fh:
                            for i, ln in enumerate(fh, 1):
                                self.assertIsNone(banned.search(ln), "%s:%d runs gh pr merge: %s" % (os.path.join(sub, n), i, ln.strip()[:120]))
                        seen += 1
        self.assertGreater(seen, 40)
        base = os.path.dirname(os.path.abspath(cli.__file__))
        for rel in ("merge_class.py", "manifest.py", "claims.py", "extras.py", "remote_lock.py", os.path.join("gates", "g10_product.py")):
            with open(os.path.join(base, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotRegex(text, r"""\[\s*["'](merge|rebase|cherry-pick|pull)["']""", rel)          # no git argv that merges
            self.assertNotIn('"gh"', text, rel)

    def test_the_new_modules_start_no_process_at_all(self):
        base = os.path.join(os.path.dirname(os.path.abspath(cli.__file__)))
        for rel in ("merge_class.py", "manifest.py", "claims.py", "extras.py", os.path.join("gates", "g10_product.py")):
            with open(os.path.join(base, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("subprocess", text.replace("shell=False", ""), rel)
            self.assertNotIn("os.system", text, rel)


if __name__ == "__main__":
    unittest.main()
