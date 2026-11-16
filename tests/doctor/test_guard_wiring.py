"""Doctor checks for the guard wiring (Phase 2; report §7.1, §7.2, §5.8; IMP-13, IMP-21).

The kit is a plugin: its hooks/hooks.json is the only wiring. The checks read the kit's own files (guardchecks.KIT_ROOT) and
the settings files the doctor sees, which can switch the plugin's hooks off or wire kit scripts a second time.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from unittest import mock

from doctorlib import guardchecks, levelcalc
from guards import wiring
from kitlib import kitpaths, levels, runstate

from .helpers import BoundaryCase

MANIFEST = kitpaths.kit("scripts", "guards", "hooks.manifest.json")
PLUGIN_KEY = "chain-of-custody@chain-of-custody"
COPY_INSTALL_HOOK = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "onFailure": "block",
                                                                    "command": 'exec python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/guards/run.py" PreToolUse --id pre-bash'}]}]}


def _read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


class GuardWiring(BoundaryCase):
    GROUPS = {"settings"}

    def repo_with(self, settings=None, local=None):
        files = {}
        if settings is not None:
            files[".claude/settings.json"] = json.dumps(settings)
        if local is not None:
            files[".claude/settings.local.json"] = json.dumps(local)
        return self.kit_repo(files=files)

    def run_checks(self, repo):
        ctx = self.ctx(repo)
        return ctx, self.results(ctx)

    def fake_wiring_kit(self, manifest=None, hooks=None, drop_script=None):
        """A copy of the kit's wiring files in a temp tree, and guardchecks pointed at it."""
        d = tempfile.mkdtemp(prefix="kit-")
        self.addCleanup(shutil.rmtree, d, True)
        man = json.loads(_read(MANIFEST)) if manifest is None else manifest
        os.makedirs(os.path.join(d, "scripts", "guards"))
        with open(os.path.join(d, "scripts", "guards", "hooks.manifest.json"), "w") as fh:
            json.dump(man, fh)
        if hooks is not False:
            os.makedirs(os.path.join(d, "hooks"))
            with open(os.path.join(d, "hooks", "hooks.json"), "w") as fh:
                fh.write(wiring.dumps(hooks if hooks is not None else wiring.build_plugin_hooks(json.loads(_read(MANIFEST)))))
        for rel in wiring.hook_scripts(json.loads(_read(MANIFEST))):
            if rel == drop_script:
                continue
            p = os.path.join(d, "scripts", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        patcher = mock.patch.object(guardchecks, "KIT_ROOT", d)
        patcher.start()
        self.addCleanup(patcher.stop)
        return d

    # -- the plugin's hooks are the wiring ----------------------------------------------------------------
    def test_the_plugin_wiring_passes_with_no_project_hooks(self) -> None:
        repo = self.repo_with(settings={"permissions": {"allow": []}})
        ctx, res = self.run_checks(repo)
        for rid in ("guards.manifest", "guards.wiring", "guards.on-failure", "guards.v1-residue"):
            self.assertClean(res, rid)
        self.assertEqual([r.status for r in self.by_id(res, "guards.wiring")], ["pass"])
        self.assertEqual(guardchecks.cap_guards(ctx, levelcalc.R(res)).level, levels.L5)

    def test_the_plugin_disabled_in_project_settings_switches_the_guards_off(self) -> None:
        repo = self.repo_with(settings={"enabledPlugins": {PLUGIN_KEY: False}})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn("enabledPlugins", hit[0].message)
        self.assertEqual(guardchecks.cap_guards(ctx, levelcalc.R(res)).level, levels.L1)

    def test_any_marketplace_name_counts(self) -> None:
        repo = self.repo_with(settings={"enabledPlugins": {"chain-of-custody@another-marketplace": False, "other-plugin@x": False}})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertIn("another-marketplace", hit[0].message)
        self.assertNotIn("other-plugin", hit[0].message)

    def test_local_settings_take_precedence_over_project_settings(self) -> None:
        repo = self.repo_with(settings={"enabledPlugins": {PLUGIN_KEY: False}}, local={"enabledPlugins": {PLUGIN_KEY: True}})
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.wiring")

    def test_disable_all_hooks_switches_the_guards_off(self) -> None:
        repo = self.repo_with(local={"disableAllHooks": True})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertIn("disableAllHooks", hit[0].message)
        self.assertEqual(guardchecks.cap_guards(ctx, levelcalc.R(res)).level, levels.L1)

    def test_disable_all_hooks_false_is_fine(self) -> None:
        repo = self.repo_with(settings={"disableAllHooks": False})
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.wiring")

    def test_a_copy_install_hooks_block_is_wired_twice(self) -> None:
        repo = self.repo_with(settings={"hooks": COPY_INSTALL_HOOK})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.wiring")
        self.assertIn("wired twice", hit[0].message)
        self.assertIn("PreToolUse", hit[0].message)

    def test_the_managed_example_is_the_wiring_not_a_second_one(self) -> None:
        # allowManagedHooksOnly switches the plugin's hooks off, so the managed copy of the guards is the only wiring
        os.makedirs(os.path.join(self.fs_root, "etc", "claude-code"), exist_ok=True)
        path = os.path.join(self.fs_root, "etc", "claude-code", "managed-settings.json")
        with open(path, "w") as fh:
            fh.write(_read(kitpaths.kit("templates", "security", "managed-settings.example.json")))
        ctx, res = self.run_checks(self.repo_with(settings={"permissions": {"allow": []}}))
        self.assertClean(res, "guards.wiring")
        # without allowManagedHooksOnly both run: that is wiring twice
        doc = json.loads(_read(path))
        doc["allowManagedHooksOnly"] = False
        with open(path, "w") as fh:
            json.dump(doc, fh)
        ctx, res = self.run_checks(self.repo_with(settings={"permissions": {"allow": []}}))
        self.assertIn("wired twice", self.assertFails(res, "guards.wiring")[0].message)

    def test_a_project_hook_of_its_own_is_not_double_wiring(self) -> None:
        own = {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "python3 tools/run.py --fmt"}]}]}
        repo = self.repo_with(settings={"hooks": own})
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.wiring")

    def test_v1_hooks_still_wired_is_an_error(self) -> None:
        live = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/guard-git.sh"}]}]}}
        repo = self.repo_with(settings=live)
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.v1-residue")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn("guard-git.sh", hit[0].message)

    # -- the kit's own files ------------------------------------------------------------------------------
    def test_the_fake_kit_matches(self) -> None:
        self.fake_wiring_kit()
        ctx, res = self.run_checks(self.repo_with())
        for rid in ("guards.manifest", "guards.wiring", "guards.on-failure"):
            self.assertClean(res, rid)

    def test_a_missing_hooks_json(self) -> None:
        self.fake_wiring_kit(hooks=False)
        ctx, res = self.run_checks(self.repo_with())
        hit = self.assertFails(res, "guards.manifest")
        self.assertIn("hooks/hooks.json is missing", hit[0].message)
        self.assertFails(res, "guards.wiring")
        self.assertEqual(guardchecks.cap_guards(ctx, levelcalc.R(res)).level, levels.L1)

    def test_a_hooks_json_without_on_failure(self) -> None:
        hooks = wiring.build_plugin_hooks(json.loads(_read(MANIFEST)))
        for g in hooks["hooks"]["PreToolUse"]:
            for h in g["hooks"]:
                h.pop("onFailure")
        self.fake_wiring_kit(hooks=hooks)
        ctx, res = self.run_checks(self.repo_with())
        self.assertIn("differs", self.assertFails(res, "guards.manifest")[0].message)
        hit = self.assertFails(res, "guards.on-failure")
        self.assertEqual(hit[0].severity, "error")
        self.assertIn("RC-30", hit[0].message)
        self.assertFails(res, "guards.wiring")

    def test_a_missing_hook_script(self) -> None:
        self.fake_wiring_kit(drop_script="guards/run.py")
        ctx, res = self.run_checks(self.repo_with())
        hit = self.assertFails(res, "guards.manifest")
        self.assertIn("scripts/guards/run.py is missing", hit[0].message)
        self.assertFails(res, "guards.wiring")

    def test_no_manifest(self) -> None:
        d = self.fake_wiring_kit()
        os.remove(os.path.join(d, "scripts", "guards", "hooks.manifest.json"))
        ctx, res = self.run_checks(self.repo_with())
        self.assertIn("not installed", self.assertFails(res, "guards.manifest")[0].message)
        self.assertFails(res, "guards.wiring")

    def test_a_manifest_naming_a_missing_guard(self) -> None:
        manifest = json.loads(_read(MANIFEST))
        manifest["entries"][0]["guards"].append("not-a-guard")
        self.fake_wiring_kit(manifest=manifest)
        ctx, res = self.run_checks(self.repo_with())
        self.assertIn("not-a-guard", " ".join(r.message for r in self.assertFails(res, "guards.manifest")))

    def test_a_project_copy_of_the_manifest_is_not_read(self) -> None:
        repo = self.kit_repo(files={".claude/scripts/guards/hooks.manifest.json": "{not json"})
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.manifest")

    # -- selftest ---------------------------------------------------------------------------------
    def record(self, repo, **rec):
        st = runstate.RunState.for_path(repo.path).ensure()
        with open(os.path.join(st.root, "selftest.json"), "w") as fh:
            json.dump(rec, fh)

    def test_selftest_never_ran_is_unknown(self) -> None:
        repo = self.repo_with()
        ctx, res = self.run_checks(repo)
        r = self.by_id(res, "guards.selftest")[0]
        self.assertEqual((r.status, r.severity), ("unknown", "warn"))

    def test_selftest_passed(self) -> None:
        repo = self.repo_with()
        sha = hashlib.sha256(_read(MANIFEST, "rb")).hexdigest()
        self.record(repo, ok=True, cases=17, at="2026-10-26T00:00:00Z", manifest_sha256=sha, failures=[])
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.selftest")

    def test_selftest_against_another_manifest_is_stale(self) -> None:
        repo = self.repo_with()
        self.record(repo, ok=True, cases=17, manifest_sha256="0" * 64, failures=[])
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.selftest")
        self.assertEqual(hit[0].severity, "warn")

    def test_failed_selftest_caps_at_l0(self) -> None:
        repo = self.repo_with()
        self.record(repo, ok=False, cases=17, failures=["git push is denied: expected deny, got allow"])
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.selftest")
        self.assertEqual(hit[0].severity, "error")
        cap = guardchecks.cap_guards(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, levels.L0)
        self.assertIn("self-test failed", cap.reason)

    def test_corrupt_selftest_file_is_an_error(self) -> None:
        repo = self.repo_with()
        st = runstate.RunState.for_path(repo.path).ensure()
        with open(os.path.join(st.root, "selftest.json"), "w") as fh:
            fh.write("{not json")
        ctx, res = self.run_checks(repo)
        self.assertFails(res, "guards.selftest")

    # -- deny drift -----------------------------------------------------------------------------------
    def test_deny_drift(self) -> None:
        repo = self.repo_with(settings={"permissions": {"allow": []}})
        ctx, res = self.run_checks(repo)
        hit = self.assertFails(res, "guards.deny-drift")
        self.assertEqual(hit[0].severity, "warn")
        self.assertIn(kitpaths.kit("templates", "settings", "deny-baseline.json"), hit[0].remediation)
        repo = self.repo_with(settings={"permissions": {"deny": list(wiring.DENY_BASELINE)}})
        ctx, res = self.run_checks(repo)
        self.assertClean(res, "guards.deny-drift")

    def test_the_cap_is_part_of_the_level_computation(self) -> None:
        self.assertIn(guardchecks.cap_guards, levelcalc.CAPS)

    def test_ids_are_registered_for_list_checks(self) -> None:
        from doctorlib.model import is_known_id
        for rid in ("guards.manifest", "guards.wiring", "guards.on-failure", "guards.selftest", "guards.deny-drift", "guards.v1-residue"):
            self.assertTrue(is_known_id(rid), rid)


if __name__ == "__main__":
    import unittest
    unittest.main()
