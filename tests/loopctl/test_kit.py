"""The kit is the plugin: its identity (version and fingerprint), the refusal when it changes under a run, where agents come from, and the
commands the kit prints (absolute paths to the plugin's scripts: a session's Bash has no ${CLAUDE_PLUGIN_ROOT})."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from kitlib import kitpaths
from loopctl import compact_hook, driver, kit, launch, plan, startrun

from . import LoopTestCase, item_doc
from .fault_injection import REAL_STUB_STAGE_WANTED, DriverTestCase

ENG = {"loc": 100, "engineers": ["backend-engineer"]}


def fake_kit(version="9.9.9", agents=("backend-engineer",)):
    """A throwaway plugin root: plugin.json, a script, an agent or two."""
    root = tempfile.mkdtemp(prefix="kit-root-")
    os.makedirs(os.path.join(root, ".claude-plugin"))
    with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "chain-of-custody", "version": version}, fh)
    os.makedirs(os.path.join(root, "scripts"))
    with open(os.path.join(root, "scripts", "a.py"), "w", encoding="utf-8") as fh:
        fh.write("print(1)\n")
    os.makedirs(os.path.join(root, "agents"))
    for a in agents:
        with open(os.path.join(root, "agents", a + ".md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: %s\n---\nRead ${CLAUDE_PLUGIN_ROOT}/context/engineer-protocol.md.\n" % a)
    return root


class Identity(unittest.TestCase):
    def setUp(self):
        self.root = fake_kit()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_version_is_plugin_json_and_unknown_when_unreadable(self):
        self.assertEqual(kit.version(self.root), "9.9.9")
        self.write(".claude-plugin/plugin.json", "not json")
        self.assertEqual(kit.version(self.root), "unknown")
        self.assertEqual(kit.version(os.path.join(self.root, "nowhere")), "unknown")

    def test_the_fingerprint_counts_names_and_contents_of_kit_files_only(self):
        fp = kit.fingerprint(self.root)
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        self.assertEqual(kit.fingerprint(self.root), fp)
        self.write("docs/notes.md", "not a kit area\n")
        self.write("scripts/__pycache__/a.cpython-39.pyc", "cache")
        self.assertEqual(kit.fingerprint(self.root), fp)
        self.write("scripts/a.py", "print(2)\n")
        changed = kit.fingerprint(self.root)
        self.assertNotEqual(changed, fp)
        self.write("scripts/b.py", "")
        self.assertNotEqual(kit.fingerprint(self.root), changed)
        os.rename(os.path.join(self.root, "scripts", "b.py"), os.path.join(self.root, "scripts", "c.py"))      # a rename alone counts
        self.assertNotEqual(kit.fingerprint(self.root), changed)

    def test_check_passes_on_the_same_kit_and_names_both_kits_on_a_change(self):
        rec = kit.identity(self.root)
        self.assertEqual(kit.check(rec, self.root), rec)
        self.write("agents/backend-engineer.md", "---\nname: backend-engineer\n---\nwidened\n")
        with self.assertRaises(kit.KitChanged) as cm:
            kit.check(rec, self.root)
        msg = str(cm.exception)
        self.assertIn("the kit changed mid-run: started with 9.9.9/%s, now 9.9.9/%s" % (rec["fingerprint"][:12], kit.fingerprint(self.root)[:12]), msg)
        self.assertIn("finish or abandon the run, then start a new one", msg)
        self.write("agents/backend-engineer.md", "---\nname: backend-engineer\n---\nRead ${CLAUDE_PLUGIN_ROOT}/context/engineer-protocol.md.\n")
        self.assertEqual(kit.check(rec, self.root), rec)                              # put back: the same kit again
        self.write(".claude-plugin/plugin.json", json.dumps({"version": "10.0.0"}))       # a version bump alone is a different kit too
        with self.assertRaises(kit.KitChanged) as cm:
            kit.check(rec, self.root)
        self.assertIn("now 10.0.0/", str(cm.exception))

    def test_a_run_without_a_kit_record_is_refused(self):
        for rec in (None, {}, {"version": "9.9.9"}, {"version": "9.9.9", "fingerprint": ""}, "9.9.9", ["x"]):
            with self.subTest(rec=rec):
                with self.assertRaises(kit.KitChanged) as cm:
                    kit.check(rec, self.root)
                self.assertIn("did not record its kit", str(cm.exception))

    def test_the_real_kit_has_an_identity_and_ships_its_agents(self):
        ident = kit.identity()
        self.assertNotEqual(ident["version"], "unknown")
        with open(kitpaths.PLUGIN_JSON, encoding="utf-8") as fh:
            self.assertEqual(ident["version"], json.load(fh)["version"])
        names = kit.agent_names()
        for role in ("acceptance-author", "backend-engineer", "code-reviewer", "design-critic", "security-reviewer"):
            self.assertIn(role, names)
        self.assertEqual(kit.agent_names(self.root), ["backend-engineer"])


class Agents(LoopTestCase):
    def test_a_role_with_no_override_comes_from_the_kit_with_the_root_filled_in(self):
        root = fake_kit()
        self.addCleanup(shutil.rmtree, root, True)
        lr = self.lr()
        text, src = kit.resolve_agent(lr.path, lr.base, "backend-engineer", root)
        self.assertEqual(src, {"kind": "kit", "path": "agents/backend-engineer.md", "version": "9.9.9"})
        self.assertIn("Read %s/context/engineer-protocol.md." % root, text)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", text)
        self.assertEqual(kit.describe_source(src), "kit 9.9.9 agents/backend-engineer.md")
        self.assertEqual(kit.resolve_agent(lr.path, lr.base, "ghost-engineer", root), (None, {"kind": "none"}))
        for bad in ("../backend-engineer", "Backend", "", "a/b", "x" * 60):
            self.assertEqual(kit.resolve_agent(lr.path, lr.base, bad, root)[1], {"kind": "none"}, bad)

    def test_the_override_at_the_base_sha_wins_and_an_uncommitted_one_does_not(self):
        root = fake_kit()
        self.addCleanup(shutil.rmtree, root, True)
        lr = self.lr()
        lr.repo.write(".claude/agents/backend-engineer.md", "---\nname: backend-engineer\n---\nProject rules. ${CLAUDE_PLUGIN_ROOT}/spec/loopctl.md\n")
        self.assertEqual(kit.resolve_agent(lr.path, lr.base, "backend-engineer", root)[1]["kind"], "kit")      # not at the base sha yet
        lr.repo.git("add", "-A")
        lr.repo.git("commit", "-q", "-m", "override")
        base = lr.repo.head()
        text, src = kit.resolve_agent(lr.path, base, "backend-engineer", root)
        self.assertEqual(src, {"kind": "override", "path": ".claude/agents/backend-engineer.md", "sha": base})
        self.assertEqual(text, "---\nname: backend-engineer\n---\nProject rules. %s/spec/loopctl.md\n" % root)

    def test_plan_roles_are_the_kit_agents_plus_the_overrides(self):
        roles = plan._with_kit_agents(["house-engineer"])
        self.assertIn("house-engineer", roles)
        for role in kit.agent_names():
            self.assertIn(role, roles)
        it = item_doc("A", estimate={"loc": 10, "engineers": ["backend-engineer"]})        # a kit agent, with no override anywhere
        lr = self.lr(items=[it])
        self.assertNotIn("plan.unknown-engineer", [f.id for f in lr.loop().ctx.plan.findings])


class Commands(LoopTestCase):
    def test_the_sequential_handoff_prints_the_plugin_script_by_absolute_path(self):
        lr = self.lr()
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 0)
        r = lr.ctl("run", "--session", "sess-1", "--nonce", lr.canary())
        self.assertEqual(r.rc, 0, r)
        base = kitpaths.script_cmd("loopctl")
        self.assertTrue(os.path.isabs(base.split(" ", 1)[1]), base)
        self.assertIn("  %s status --json" % base, r.out)
        self.assertIn(kitpaths.kit("skills", "orchestrate", "SKILL.md"), r.out)
        self.assertNotIn(".claude/scripts", r.out)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", r.out)

    def test_the_compact_hook_names_kit_files_by_absolute_path(self):
        self.assertEqual(compact_hook.LOOPCTL, "python3 " + os.path.join(kitpaths.KIT_ROOT, "scripts", "loopctl"))
        self.assertEqual(compact_hook.PROTOCOL, os.path.join(kitpaths.KIT_ROOT, "context", "engineer-protocol.md"))
        self.assertTrue(os.path.isfile(compact_hook.PROTOCOL))
        self.assertTrue(os.path.isfile(compact_hook.SPEC))

    def test_a_sequential_resume_under_a_changed_kit_is_refused(self):
        lr = self.lr()
        self.assertEqual(lr.ctl("approve", "plan", "--by", "Pat").rc, 0)
        nonce = lr.canary()
        self.assertEqual(lr.ctl("run", "--session", "sess-1", "--nonce", nonce).rc, 0)
        rs = lr.context().rs
        rec = rs.read_run()
        self.assertEqual(rec["kit"], kit.identity())                                    # recorded at the start
        rec["kit"] = dict(rec["kit"], fingerprint="0" * 64)
        rs.write_run(rec)
        r = lr.ctl("run", "--session", "sess-1", "--nonce", nonce)
        self.assertEqual(r.rc, startrun.EXIT_REFUSED, r)
        self.assertIn("refused: kit: the kit changed mid-run: started with %s/000000000000" % rec["kit"]["version"], r.err)

    def test_status_shows_the_kit_the_run_started_with_and_a_change(self):
        lr = self.lr()
        out = lr.ctl("status").out
        self.assertIn("kit: %s (no run recorded one)" % kit.version(), out)
        loop = lr.fast_start()
        out = lr.ctl("status").out
        self.assertIn("kit: run started with %s" % kit.version(), out)
        self.assertNotIn("NOW", out)
        rec = loop.ctx.rs.read_run()
        rec["kit"] = {"version": "0.0.1", "fingerprint": "f" * 64}
        loop.ctx.rs.write_run(rec)
        r = lr.ctl("status", "--json")
        doc = r.json["kit"]
        self.assertEqual((doc["run_version"], doc["matches"], doc["root"]), ("0.0.1", False, kitpaths.KIT_ROOT))
        self.assertIn("kit: run started with 0.0.1; NOW %s (the kit changed mid-run" % kit.version(), lr.ctl("status").out)


class DriverKit(DriverTestCase):
    def test_a_launch_under_a_changed_kit_is_refused(self):
        lr = self.dr(items=[item_doc("A")])
        drv = lr.driver_at("A")
        drv.check_kit()                                                                  # the kit the run recorded: fine
        drv.run_rec["kit"] = dict(drv.run_rec["kit"], fingerprint="0" * 64)
        with self.assertRaises(launch.LaunchRefused) as cm:
            drv.check_kit()
        self.assertIn("the kit changed mid-run", str(cm.exception))
        drv.run_rec.pop("kit")
        with self.assertRaises(launch.LaunchRefused) as cm:
            drv.check_kit()
        self.assertIn("did not record its kit", str(cm.exception))

    def test_a_kit_that_changes_between_two_children_stops_the_run_before_the_second(self):
        self.kit_version("2.0.0 (2026-10-01)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: 2.1.310\n")
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        real_identity, real_launch = kit.identity, driver.Driver.launch_child
        launches = []

        def launch_child(drv, *a, **kw):
            launches.append(a[1] if len(a) > 1 else kw.get("role"))
            if len(launches) == 2:                                                       # the plugin is updated while the first child ran
                kit.identity = lambda root=kitpaths.KIT_ROOT: dict(real_identity(root), fingerprint="1" * 64)
            return real_launch(drv, *a, **kw)
        driver.Driver.launch_child = launch_child
        self.addCleanup(setattr, driver.Driver, "launch_child", real_launch)
        self.addCleanup(setattr, kit, "identity", real_identity)
        r = lr.run()
        self.assertNotEqual(r.rc, 0, r)
        self.assertIn("the kit changed mid-run", r.out + r.err)
        self.assertEqual(len(lr.calls()), 1, lr.calls())                                 # the engineer ran; the reviewer never launched
        self.assertEqual(lr.calls()[0].get("role"), "backend-engineer")

    def test_children_load_the_kit_with_plugin_dir(self):
        self.kit_version("2.0.0 (2026-10-01)\nclaude_code_min: 2.1.302\nclaude_code_tested_max: 2.1.310\n")
        lr = self.dr(items=[item_doc("A", estimate=ENG)])
        lr.set_scenario(lr.happy())
        lr.approve()
        r = lr.run()
        self.assertEqual(r.rc, 0, r)
        for call in lr.calls():
            argv = call.get("argv") or []
            self.assertIn("--plugin-dir", argv, call)
            self.assertEqual(argv[argv.index("--plugin-dir") + 1], kitpaths.KIT_ROOT)
            self.assertNotIn("--bare", argv)

    def test_the_real_stub_stage_is_wanted_with_the_kits_acceptance_author_and_no_override(self):
        lr = self.dr(items=[item_doc("A")], cfg={"gates": {"g2": "on"}})
        drv = lr.driver_at("A", begin=False)
        drv.anchor = lr.base
        if "g2" not in __import__("loopctl.gates", fromlist=["installed"]).installed():
            self.skipTest("g2 is not installed in this test process")
        self.assertFalse(os.path.exists(os.path.join(lr.path, ".claude", "agents", "acceptance-author.md")))
        self.assertTrue(REAL_STUB_STAGE_WANTED(drv))


if __name__ == "__main__":
    unittest.main()
