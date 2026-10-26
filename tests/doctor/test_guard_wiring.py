"""Doctor checks for the guard wiring (Phase 2; report §7.1, §7.2, §5.8; IMP-13, IMP-21)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil

from doctorlib import guardchecks, levelcalc
from guards import wiring
from kitlib import levels, runstate

from tests import REPO_ROOT

from .helpers import BoundaryCase

MANIFEST = os.path.join(REPO_ROOT, ".claude", "scripts", "guards", "hooks.manifest.json")


def _read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


class GuardWiring(BoundaryCase):
    GROUPS = {"settings"}

    def repo_with_guards(self, live_settings=None, staged=True):
        text = _read(MANIFEST)
        manifest = json.loads(text)
        files = {".claude/scripts/guards/hooks.manifest.json": text}
        if staged:
            files[".claude/settings.v2.json"] = wiring.dumps(wiring.build_settings(manifest))
        if live_settings is not None:
            files[".claude/settings.json"] = json.dumps(live_settings)
        return self.kit_repo(files=files), manifest

    def run_checks(self, repo):
        ctx = self.ctx(repo)
        return ctx, self.results(ctx)

    def test_active_wiring_passes_everything(self) -> None:
        repo, manifest = self.repo_with_guards(live_settings=wiring.build_settings(manifest := json.loads(_read(MANIFEST))))
        ctx, res = self.run_checks(repo)
        for rid in ("guards.manifest", "guards.wiring", "guards.on-failure"):
            self.assertClean(res, rid)
        self.assertEqual([r.status for r in self.by_id(res, "guards.wiring")], ["pass"])
        cap = guardchecks.cap_guards(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, levels.L5)

    def test_staged_but_not_active_is_reported_as_such(self) -> None:
        repo, _ = self.repo_with_guards(live_settings={"permissions": {"allow": []}})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertEqual(hit[0].severity, "warn")
        self.assertIn("staged", hit[0].message)
        self.assertEqual(guardchecks.cap_guards(ctx, levelcalc.R(res)).level, levels.L1)

    def test_v1_hooks_still_wired(self) -> None:
        live = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh"}]}]}}
        repo, _ = self.repo_with_guards(live_settings=live)
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertIn("v1 shell hooks", hit[0].message)
        self.assertEqual(hit[0].severity, "warn")

    def test_partial_wiring_is_an_error(self) -> None:
        manifest = json.loads(_read(MANIFEST))
        live = wiring.build_settings(manifest)
        live["hooks"]["PreToolUse"].pop(0)           # the Bash guard is gone
        repo, _ = self.repo_with_guards(live_settings=live)
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn("inconsistent", hit[0].message)

    def test_a_changed_command_counts_as_missing(self) -> None:
        manifest = json.loads(_read(MANIFEST))
        live = wiring.build_settings(manifest)
        live["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = live["hooks"]["PreToolUse"][0]["hooks"][0]["command"].replace("exit 2", "exit 0")
        repo, _ = self.repo_with_guards(live_settings=live)
        ctx, res = self.run_checks(repo)
        self.assertFails(res, "guards.wiring")

    def test_missing_on_failure_is_an_error(self) -> None:
        manifest = json.loads(_read(MANIFEST))
        live = wiring.build_settings(manifest)
        for g in live["hooks"]["PreToolUse"]:
            for h in g["hooks"]:
                h.pop("onFailure")
        repo, _ = self.repo_with_guards(live_settings=live)
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.on-failure")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn("RC-30", hit[0].message)

    def test_hooks_in_local_or_user_settings_count(self) -> None:
        manifest = json.loads(_read(MANIFEST))
        repo, _ = self.repo_with_guards(live_settings={"permissions": {"allow": []}})
        repo.write(".claude/settings.local.json", json.dumps(wiring.build_settings(manifest)))
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.wiring")

    def test_no_guards_installed(self) -> None:
        repo = self.kit_repo()
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.manifest")
        self.assertIn("not installed", hit[0].message)
        self.assertFails(res, "guards.wiring")

    def test_stale_staged_file(self) -> None:
        text = _read(MANIFEST)
        repo = self.kit_repo(files={".claude/scripts/guards/hooks.manifest.json": text, ".claude/settings.v2.json": "{}\n"})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.manifest")
        self.assertIn("differs", hit[0].message)

    def test_a_manifest_naming_a_missing_guard(self) -> None:
        manifest = json.loads(_read(MANIFEST))
        manifest["entries"][0]["guards"].append("not-a-guard")
        repo = self.kit_repo(files={".claude/scripts/guards/hooks.manifest.json": json.dumps(manifest)})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.manifest")
        self.assertIn("not-a-guard", hit[0].message)

    # -- selftest ---------------------------------------------------------------------------------
    def record(self, repo, **rec):
        st = runstate.RunState.for_path(repo.path).ensure()
        with open(os.path.join(st.root, "selftest.json"), "w") as fh:
            json.dump(rec, fh)

    def test_selftest_never_ran_is_unknown(self) -> None:
        repo, _ = self.repo_with_guards()
        ctx, res = self.run_checks(repo)
        r = self.by_id(res, "guards.selftest")[0]
        self.assertEqual((r.status, r.severity), ("unknown", "warn"))

    def test_selftest_passed(self) -> None:
        repo, _ = self.repo_with_guards()
        sha = hashlib.sha256(_read(MANIFEST, "rb")).hexdigest()
        self.record(repo, ok=True, cases=15, at="2026-10-26T00:00:00Z", manifest_sha256=sha, failures=[])
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.selftest")

    def test_selftest_against_another_manifest_is_stale(self) -> None:
        repo, _ = self.repo_with_guards()
        self.record(repo, ok=True, cases=15, manifest_sha256="0" * 64, failures=[])
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.selftest")
        self.assertEqual(hit[0].severity, "warn")

    def test_failed_selftest_caps_at_l0(self) -> None:
        repo, _ = self.repo_with_guards(live_settings=wiring.build_settings(json.loads(_read(MANIFEST))))
        self.record(repo, ok=False, cases=15, failures=["git push is denied: expected deny, got allow"])
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.selftest")
        self.assertEqual(hit[0].severity, "error")
        cap = guardchecks.cap_guards(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, levels.L0)
        self.assertIn("self-test failed", cap.reason)

    def test_corrupt_selftest_file_is_an_error(self) -> None:
        repo, _ = self.repo_with_guards()
        st = runstate.RunState.for_path(repo.path).ensure()
        with open(os.path.join(st.root, "selftest.json"), "w") as fh:
            fh.write("{not json")
        ctx, res = self.run_checks(repo)
        self.assertFails(res, "guards.selftest")

    # -- deny drift -----------------------------------------------------------------------------------
    def test_deny_drift(self) -> None:
        repo, _ = self.repo_with_guards(live_settings={"permissions": {"allow": []}})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.deny-drift")
        self.assertEqual(hit[0].severity, "warn")
        repo, _ = self.repo_with_guards(live_settings={"permissions": {"deny": list(wiring.DENY_BASELINE)}})
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.deny-drift")

    def test_the_cap_is_part_of_the_level_computation(self) -> None:
        self.assertIn(guardchecks.cap_guards, levelcalc.CAPS)

    def test_ids_are_registered_for_list_checks(self) -> None:
        from doctorlib.model import is_known_id
        for rid in ("guards.manifest", "guards.wiring", "guards.on-failure", "guards.selftest", "guards.deny-drift"):
            self.assertTrue(is_known_id(rid), rid)


if __name__ == "__main__":
    import unittest
    unittest.main()
