"""The keys, the doctor's rows, the command classification and the templates of Phase 8 (report 11.2, 11.5; IMP-1, IMP-11)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from doctorlib import parallel as doctor_parallel  # noqa: F401  (registers the checks)

from guards import g_exec
from guards.invoke import analyze
from kitlib import registry

from loopctl import cli

from ... import REPO_ROOT, SCRIPTS_DIR
from . import read
from ...doctor.helpers import BoundaryCase


def inv(args):
    ana = analyze("python3 .claude/scripts/loopctl " + " ".join(args), "/tmp")
    return [i for i in ana.invs if i.name.startswith("python")][0]


class Keys(unittest.TestCase):
    def test_the_registry_has_the_four_keys_with_the_gates_the_decisions_name(self):
        reg = registry.load_registry()
        pm, mp, ph, ttl = (reg.keys[k] for k in ("parallel_mode", "max_parallel", "pipeline_host", "lease_ttl_s"))
        self.assertEqual((pm.default, pm.evidence_gated, pm.h6_protected, pm.owner_phase), ("off", ["worktree"], True, "8-parallel"))
        self.assertEqual((mp.default, mp.evidence_gated, mp.h6_protected), (2, False, True))
        self.assertEqual((ph.default, ph.evidence_gated, list(ph.choices)), ("conductor", ["workflow"], ["conductor", "workflow"]))
        self.assertEqual((ttl.default, ttl.h6_protected), (300, False))

    def test_a_gated_value_is_listed_as_a_violation_until_the_evidence_gate_passes(self):
        from kitlib import config as kconfig
        cfg = kconfig.load_text("```yaml\nparallel_mode: worktree\npipeline_host: workflow\nmax_parallel: 3\n```\n")
        self.assertEqual(sorted(cfg.evidence_gated_violations()), ["parallel_mode", "pipeline_host"])
        self.assertEqual(kconfig.load_text("```yaml\nparallel_mode: off\n```\n").evidence_gated_violations(), [])
        self.assertTrue(kconfig.load_text("```yaml\nparallel_mode: sideways\n```\n").errors)

    def test_max_parallel_cannot_start_fifty_workers(self):
        from loopctl import parallel
        from kitlib import config as kconfig
        for raw, want in (("2", 2), ("0", 1), ("-3", 1), ("4", 4), ("50", 4), ("1000000", 4)):
            self.assertEqual(parallel.max_parallel(kconfig.load_text("```yaml\nmax_parallel: %s\n```\n" % raw)), want)

    def test_an_ordinary_run_never_loads_the_parallel_package(self):
        code = "import sys; sys.path.insert(0, %r); import loopctl.driver, loopctl.startrun, loopctl.cli; print('loopctl.parallel' in sys.modules)" % SCRIPTS_DIR
        out = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, shell=False, check=True).stdout.decode().strip()
        self.assertEqual(out, "False")


class Doctor(BoundaryCase):
    GROUPS = {"config"}

    def test_pipeline_host_workflow_is_an_error_even_with_the_evidence_gate_passed(self):
        res = self.results(self.ctx(self.kit_repo(config={"pipeline_host": "workflow"})))
        hit = self.assertFails(res, "parallel.pipeline-host")[0]
        self.assertEqual(hit.severity, "error")
        self.assertIn("design-only", hit.message)
        self.assertClean(self.results(self.ctx(self.kit_repo(config={"pipeline_host": "conductor"}))), "parallel.pipeline-host")

    def test_parallel_mode_needs_the_driver_a_sane_width_and_a_plain_worktrees_directory(self):
        repo = self.kit_repo(config={"parallel_mode": "worktree", "runner": "sequential", "max_parallel": 9})
        msgs = " ".join(r.message for r in self.assertFails(self.results(self.ctx(repo)), "parallel.mode"))
        self.assertIn("needs runner: driver", msgs)
        self.assertIn("max_parallel is 9", msgs)
        ok = self.kit_repo(config={"parallel_mode": "worktree", "runner": "driver", "max_parallel": 2}, files={".gitignore": ".claude/worktrees/\n"})
        self.assertClean(self.results(self.ctx(ok)), "parallel.mode")
        unignored = self.kit_repo(config={"parallel_mode": "worktree", "runner": "driver"})
        self.assertEqual(self.assertFails(self.results(self.ctx(unignored)), "parallel.mode")[0].severity, "warn")
        os.makedirs(os.path.join(ok.path, ".claude"), exist_ok=True)
        os.symlink("/tmp", os.path.join(ok.path, ".claude", "worktrees"))
        self.assertIn("symbolic link", " ".join(r.message for r in self.assertFails(self.results(self.ctx(ok)), "parallel.mode")))

    def test_off_says_nothing(self):
        res = [r for r in self.results(self.ctx(self.kit_repo())) if r.id == "parallel.mode"]
        self.assertEqual([r.status for r in res], ["skipped"])


class Classification(unittest.TestCase):
    READS = [["parallel", "check"], ["parallel", "check", "--json"], ["parallel", "status"], ["parallel", "leases"], ["parallel", "cleanup", "--dry-run"],
             ["parallel", "cascade", "A", "--dry-run"]]
    WRITES = [["parallel"], ["parallel", "cleanup"], ["parallel", "cleanup", "--apply"], ["parallel", "cascade", "A", "--by", "Pat"], ["parallel", "cascade", "A", "--apply", "--by", "Pat"],
              ["parallel", "cleanup", "--dry-run", "--apply"], ["parallel", "check", "--apply"], ["parallel", "frobnicate"], ["parallel", "cascade", "--dry-run", "--force"]]

    def test_the_guard_and_loopctl_agree_on_which_parallel_lines_write(self):
        t = cli.classification()
        self.assertEqual({k: tuple(v) for k, v in t["flag_read"].items()}, g_exec.LOOPCTL_FLAG_READ)
        self.assertIn("parallel", t["tier1"])
        for args in self.READS:
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), True)
        for args in self.WRITES:
            with self.subTest(args):
                self.assertIs(g_exec.loopctl_is_read(inv(args)), False)

    def test_halt_lets_only_the_reads_through(self):
        from guards import g_halt
        for args in self.READS:
            self.assertTrue(g_halt._read_only(None, inv(args)), args)
        for args in self.WRITES:
            self.assertFalse(g_halt._read_only(None, inv(args)), args)


class Templates(unittest.TestCase):
    def test_the_hook_snippet_and_the_include_template(self):
        doc = json.loads(read(os.path.join(REPO_ROOT, ".claude", "templates", "parallel", "worktree-hooks.settings.json")))
        for event, verb in (("WorktreeCreate", "create"), ("WorktreeRemove", "remove")):
            cmd = doc["hooks"][event][0]["hooks"][0]["command"]
            self.assertTrue(cmd.endswith("worktree_hook.py\" %s" % verb), cmd)
        self.assertNotIn("WorktreeCreate", read(os.path.join(SCRIPTS_DIR, "guards", "hooks.manifest.json")))          # not wired by the kit: a human merges the snippet
        from loopctl.parallel import worktrees
        text = read(os.path.join(REPO_ROOT, ".claude", "templates", "worktreeinclude"))
        self.assertEqual(worktrees.parse_include(text), ([".env.local", ".env.test"], []))


if __name__ == "__main__":
    unittest.main()
