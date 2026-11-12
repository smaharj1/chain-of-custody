"""Tests for scripts/adapt.py: the guidance-only copy of the kit for Codex and Cursor projects."""
from __future__ import annotations

import contextlib
import io
import os
import re

import adapt
from kitlib import kitpaths

from . import REPO_ROOT
from .helpers import BaseTestCase

OUT = adapt.OUT_DIR


def walk(root):
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


class AdaptTest(BaseTestCase):

    def run_adapt(self, target, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = adapt.main(["--target", target] + list(args), kit_root=REPO_ROOT)
        return rc, out.getvalue() + err.getvalue()

    def read(self, *parts):
        with open(os.path.join(*parts), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_copies_the_guidance_and_nothing_that_runs(self):
        t = self.tmpdir()
        rc, out = self.run_adapt(t)
        self.assertEqual(rc, 0, out)
        base = os.path.join(t, OUT)
        for rel in ("skills/pm/SKILL.md", "skills/tech-lead/SKILL.md", "agents/code-reviewer.md", "context/engineer-protocol.md",
                    "templates/brief.md", "spec/loop-engineering.md", "GUIDE.md", "README.md", "VERSION"):
            self.assertTrue(os.path.isfile(os.path.join(base, rel)), rel)
        for rel in adapt.copied_files(REPO_ROOT):
            self.assertTrue(os.path.isfile(os.path.join(base, rel)), rel)
        top = set(os.listdir(base))
        for gone in ("scripts", "hooks", "seed", "tests", "docs", ".claude-plugin", "migrate", "evals"):
            self.assertNotIn(gone, top)
        for mode in adapt.CLAUDE_ONLY_SKILLS:
            self.assertFalse(os.path.exists(os.path.join(base, "skills", mode)), mode)
        for rel in walk(base):
            self.assertFalse(rel.endswith(".py"), rel)
        version = self.read(base, "VERSION")
        self.assertIn("version: " + adapt.plugin_meta(REPO_ROOT)["version"], version)
        self.assertIn("commit: ", version)

    def test_no_plugin_root_left_in_any_rewritten_file(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        for rel, data in walk(t).items():
            if rel.endswith((".md", ".mdc")) or rel.endswith("VERSION"):
                self.assertNotIn(kitpaths.PLUGIN_ROOT_VAR.encode(), data, rel)
                self.assertNotIn(("`%s` is" % OUT).encode(), data, rel)

    def test_no_command_runs_a_url(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        command_url = re.compile(rb'(?:python3|sh|bash) +"?https?://')
        for rel, data in walk(t).items():
            self.assertNotIn(b"python3 https://", data, rel)
            self.assertIsNone(command_url.search(data), rel)
        delivery = self.read(t, OUT, "context", "delivery.md")
        self.assertIn("scripts/pr_prepare.py (%s; " % adapt.PLUGIN_ONLY, delivery)

    def test_non_markdown_templates_are_copied_byte_for_byte(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        copied = [rel for rel in adapt.copied_files(REPO_ROOT) if not rel.endswith(".md")]
        self.assertTrue(any(rel.endswith(".yml") for rel in copied))
        for rel in copied:
            with open(os.path.join(REPO_ROOT, rel), "rb") as fh:
                want = fh.read()
            with open(os.path.join(t, OUT, rel), "rb") as fh:
                self.assertEqual(fh.read(), want, rel)
        for rel in kitpaths.kit_files(REPO_ROOT, ("templates",)):
            if adapt.claude_only_template(rel):
                self.assertFalse(os.path.exists(os.path.join(t, OUT, rel)), rel)
        self.assertTrue(adapt.claude_only_template("templates/settings/interactive.settings.json"))
        self.assertTrue(adapt.claude_only_template("templates/notify/notify-hooks.settings.json"))
        self.assertFalse(adapt.claude_only_template("templates/github/workflows/verify.yml"))

    def test_every_relative_link_in_the_copy_resolves(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        link = re.compile(r"\]\(([^)\s]+)\)")
        checked = 0
        for rel in walk(t):
            if not rel.endswith(".md"):
                continue
            path = os.path.join(t, rel)
            for target in link.findall(self.read(path)):
                target = target.split("#")[0]
                if not target or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target) or "<" in target:
                    continue
                checked += 1
                self.assertTrue(os.path.exists(os.path.join(os.path.dirname(path), target)), "%s -> %s" % (rel, target))
        self.assertGreater(checked, 5)

    def test_rewrites_point_at_the_copy_or_the_release(self):
        meta = adapt.plugin_meta(REPO_ROOT)
        files = adapt.copied_files(REPO_ROOT)
        seeds = adapt.seed_files(REPO_ROOT)
        ctx = [f[len("context/"):] for f in files if f.startswith("context/")]
        rw = adapt.Rewriter(files, seeds, ctx, meta["version"], meta["repo"])
        url = "%s/blob/v%s/" % (meta["repo"], meta["version"])
        R = kitpaths.PLUGIN_ROOT_VAR
        self.assertEqual(rw(R + "/context/engineer-protocol.md"), OUT + "/context/engineer-protocol.md")
        self.assertEqual(rw("see " + R + "/agents/<role>.md."), "see " + OUT + "/agents/<role>.md.")
        only = "(%s; %s" % (adapt.PLUGIN_ONLY, url)
        self.assertEqual(rw("run " + R + "/scripts/loopctl status"), "run scripts/loopctl %sscripts/loopctl) status" % only)
        self.assertEqual(rw("`python3 " + R + "/scripts/pr_prepare.py prepare`"),
                         "`scripts/pr_prepare.py %sscripts/pr_prepare.py) prepare`" % only)
        self.assertEqual(rw('HUMAN STEP: python3 "' + R + '/scripts/loopctl" approve plan'),
                         "HUMAN STEP: scripts/loopctl %sscripts/loopctl) approve plan" % only)
        self.assertEqual(rw('`python3 "$KIT/scripts/verify.py"`'), "`scripts/verify.py %sscripts/verify.py)`" % only)
        self.assertEqual(rw("`python3 tool.py --doc " + R + "/docs/x.md`"), "`python3 tool.py --doc docs/x.md %sdocs/x.md)`" % only)
        self.assertEqual(rw("python3 .claude/scripts/doctor.py --level"), "scripts/doctor.py %sscripts/doctor.py) --level" % only)
        self.assertEqual(rw('--policy "' + R + '/templates/infra/policy"'), '--policy "%s/templates/infra/policy"' % OUT)
        self.assertEqual(rw("read " + R + "/docs/unattended.md."), "read " + url + "docs/unattended.md.")
        self.assertEqual(rw(R + "/skills/init/SKILL.md"), url + "skills/init/SKILL.md")
        self.assertEqual(rw("(`" + R + "` is not set in the Bash tool)"), "(%s is not set in the Bash tool)" % adapt.PLUGIN_ROOT_PROSE)
        self.assertNotIn(OUT, rw(R))
        self.assertEqual(rw(R + "/seed/.claude/CLAUDE.md"), "%s/project/%s" % (OUT, adapt.PROJECT_FILE))
        self.assertEqual(rw("`.claude/context/backend.md`"), "`%s/project/context/backend.md`" % OUT)
        self.assertEqual(rw("`.claude/context/<domain>.md`"), "`%s/project/context/<domain>.md`" % OUT)
        self.assertEqual(rw(".claude/context/choosing-your-stack.md"), OUT + "/context/choosing-your-stack.md")
        self.assertEqual(rw("edit .claude/loop.config.md and .claude/CLAUDE.md"),
                         "edit %s/project/loop.config.md and %s/project/%s" % (OUT, OUT, adapt.PROJECT_FILE))
        self.assertEqual(rw("see [x](../GLOSSARY.md#terms)", "context/a.md"), "see [x](%sGLOSSARY.md#terms)" % url)
        self.assertEqual(rw("[l](loop.md)", "skills/orchestrate/SKILL.md"), "[l](loop.md)")
        self.assertEqual(rw("[p](../../../context/engineer-protocol.md)", "seed/.claude/context/backend.md", "project/context/backend.md"),
                         "[p](../../context/engineer-protocol.md)")
        self.assertEqual(rw("[s](../../../scripts/doctor.py)", "seed/.claude/context/backend.md", "project/context/backend.md"),
                         "[s](%sscripts/doctor.py)" % url)
        self.assertEqual(rw("~/.claude/context/x.md"), "~/.claude/context/x.md")
        self.assertEqual(rw(".claude/settings.json"), ".claude/settings.json")

    def test_writes_agents_md_and_mdc_with_the_marker(self):
        t = self.tmpdir()
        rc, out = self.run_adapt(t)
        self.assertEqual(rc, 0, out)
        agents = self.read(t, "AGENTS.md")
        mdc = self.read(t, ".cursor", "rules", "chain-of-custody.mdc")
        for text in (agents, mdc):
            self.assertIn(adapt.MARKER, text)
            self.assertIn(OUT + "/skills/pm/SKILL.md", text)
            self.assertIn(OUT + "/agents/", text)
            self.assertIn("loopctl", text)
            self.assertIn("**No.**", text)
            self.assertIn("%s/project/%s" % (OUT, adapt.PROJECT_FILE), text)
            self.assertIn("act as the PM", text)
            self.assertIn("Push rule", text)
            self.assertIn("git_autonomy: branch+commit+push", text)
            self.assertIn("never force-push", text)
            self.assertIn("Nothing here checks this rule or knows which branches are protected", text)
            self.assertIn("| Push tier", text)
            self.assertIn("| Per-role tool limits (`tools:` / `disallowedTools:`", text)
            self.assertIn("## Verify and deliver here", text)
            self.assertIn("feat/<slug>", text)
            self.assertNotIn("read-only\n", text)
        self.assertTrue(mdc.startswith("---\ndescription:"))
        self.assertIn("alwaysApply: true", mdc)
        for mode, _purpose in adapt.ROUTES:
            self.assertIn("skills/%s/SKILL.md" % mode, agents)

    def test_tool_selects_the_file(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t, "--tool", "codex")[0], 0)
        self.assertTrue(os.path.isfile(os.path.join(t, "AGENTS.md")))
        self.assertFalse(os.path.exists(os.path.join(t, ".cursor")))
        t2 = self.tmpdir()
        self.assertEqual(self.run_adapt(t2, "--tool", "cursor")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(t2, "AGENTS.md")))
        self.assertTrue(os.path.isfile(os.path.join(t2, ".cursor", "rules", "chain-of-custody.mdc")))

    def test_users_own_agents_md_is_kept_unless_forced(self):
        t = self.tmpdir()
        with open(os.path.join(t, "AGENTS.md"), "w") as fh:
            fh.write("# my own rules\n")
        rc, out = self.run_adapt(t, "--tool", "codex")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(t, "AGENTS.md"), "# my own rules\n")
        self.assertIn("SKIPPED AGENTS.md", out)
        self.assertIn(OUT + "/GUIDE.md", out)
        rc, out = self.run_adapt(t, "--tool", "codex", "--force")
        self.assertEqual(rc, 0, out)
        self.assertIn(adapt.MARKER, self.read(t, "AGENTS.md"))
        self.assertEqual(self.read(t, "AGENTS.md.pre-adapt"), "# my own rules\n")

    def test_rerun_is_idempotent_and_replaces_the_generated_dir(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        first = walk(t)
        stray = os.path.join(t, OUT, "skills", "stale.md")
        with open(stray, "w") as fh:
            fh.write("left over from an old version\n")
        rc, out = self.run_adapt(t)
        self.assertEqual(rc, 0, out)
        self.assertEqual(walk(t), first)
        self.assertIn("unchanged AGENTS.md", out)
        self.assertFalse(os.path.exists(os.path.join(t, OUT + ".adapt-tmp")))

    def test_project_files_are_seeded_once_and_never_overwritten(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        proj = os.path.join(t, OUT, "project")
        for rel in adapt.seed_files(REPO_ROOT):
            self.assertTrue(os.path.isfile(os.path.join(proj, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(proj, "CLAUDE.md")))
        project = self.read(proj, adapt.PROJECT_FILE)
        for claude_only in (".claude/", "$KIT", "`/pm`", "/chain-of-custody:", "kit:", "loopctl\""):
            self.assertNotIn(claude_only, project)
        config = self.read(proj, "loop.config.md")
        self.assertRegex(config, r"(?m)^git_autonomy: none +#")
        self.assertRegex(config, r"(?m)^delivery: local +#")
        self.assertNotRegex(config, r"(?m)^(git_autonomy|delivery): (branch|push|draft)")
        self.assertIn("Codex / Cursor copy", config)
        self.assertNotIn(".claude/scripts/", config)
        self.assertNotIn("$KIT/", config)
        self.assertEqual(self.read(t, OUT, adapt.SEED_COPY_DIR, "loop.config.md"), config)
        primer = os.path.join(proj, "context", "backend.md")
        with open(primer, "w") as fh:
            fh.write("# our backend\n")
        os.remove(os.path.join(proj, "loop.config.md"))
        rc, out = self.run_adapt(t)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(primer), "# our backend\n")
        self.assertTrue(os.path.isfile(os.path.join(proj, "loop.config.md")))
        self.assertIn("seeded 1 project files", out)

    def test_refuses_the_kit_itself(self):
        rc, out = self.run_adapt(REPO_ROOT)
        self.assertEqual(rc, 1)
        self.assertIn("the kit itself", out)
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, OUT)))
        rc, out = self.run_adapt(os.path.join(REPO_ROOT, "docs"))
        self.assertEqual(rc, 1)

    def test_refuses_a_chain_of_custody_dir_it_did_not_write(self):
        t = self.tmpdir()
        os.makedirs(os.path.join(t, OUT))
        with open(os.path.join(t, OUT, "notes.md"), "w") as fh:
            fh.write("mine\n")
        rc, out = self.run_adapt(t)
        self.assertEqual(rc, 1)
        self.assertIn("did not write it", out)
        self.assertEqual(self.read(t, OUT, "notes.md"), "mine\n")

    def test_symlinked_tool_files_are_refused_even_with_force(self):
        t = self.tmpdir()
        elsewhere = os.path.join(self.tmpdir(), "theirs.md")
        with open(elsewhere, "w") as fh:
            fh.write("<!-- %s -->\nnot yours\n" % adapt.MARKER)
        os.symlink(elsewhere, os.path.join(t, "AGENTS.md"))
        os.makedirs(os.path.join(t, ".cursor", "rules"))
        os.symlink(elsewhere, os.path.join(t, ".cursor", "rules", "chain-of-custody.mdc"))
        for extra in ((), ("--force",)):
            rc, out = self.run_adapt(t, *extra)
            self.assertEqual(rc, 1, out)
            self.assertIn("REFUSED AGENTS.md", out)
            self.assertIn("REFUSED %s" % adapt.MDC, out)
            self.assertIn(elsewhere, out)
            self.assertEqual(self.read(elsewhere), "<!-- %s -->\nnot yours\n" % adapt.MARKER)
        self.assertFalse(os.path.exists(os.path.join(t, "AGENTS.md.pre-adapt")))

    def test_symlinked_parent_dir_and_generated_dir_are_refused(self):
        t = self.tmpdir()
        real_cursor = self.tmpdir()
        os.symlink(real_cursor, os.path.join(t, ".cursor"))
        rc, out = self.run_adapt(t, "--tool", "cursor")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertEqual(os.listdir(real_cursor), [])
        t2 = self.tmpdir()
        os.symlink(self.tmpdir(), os.path.join(t2, OUT))
        rc, out = self.run_adapt(t2)
        self.assertEqual(rc, 1, out)
        self.assertIn("never writes through a symlink", out)

    def test_changed_seed_is_reported_for_kept_files(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        version = os.path.join(t, OUT, "VERSION")
        text = self.read(version)
        self.assertRegex(text, r"(?m)^seed: loop\.config\.md sha256:[0-9a-f]{64}$")
        self.assertRegex(text, r"(?m)^generated: AGENTS\.md sha256:[0-9a-f]{64}$")
        rc, out = self.run_adapt(t)
        self.assertNotIn("seed for", out)
        primer = os.path.join(t, OUT, "project", "context", "backend.md")
        with open(primer, "w") as fh:
            fh.write("# our backend\n")
        old = "sha256:" + "0" * 64
        text = re.sub(r"(?m)^(seed: (loop\.config\.md|context/backend\.md)) \S+$", r"\1 " + old, self.read(version))
        with open(version, "w") as fh:
            fh.write(text)
        rc, out = self.run_adapt(t)
        self.assertEqual(rc, 0, out)
        self.assertIn("seed for %s/project/context/backend.md changed since v" % OUT, out)
        self.assertIn("compare with %s/%s/context/backend.md" % (OUT, adapt.SEED_COPY_DIR), out)
        self.assertIn("seed for %s/project/loop.config.md changed" % OUT, out)
        self.assertEqual(self.read(primer), "# our backend\n")

    def test_switching_tools_reports_the_stale_file(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t)[0], 0)
        rc, out = self.run_adapt(t, "--tool", "codex")
        self.assertEqual(rc, 0, out)
        self.assertIn("stale %s" % adapt.MDC, out)
        self.assertIn("re-run with --tool both", out)
        rc, out = self.run_adapt(t, "--tool", "both")
        self.assertNotIn("stale", out)

    def test_hand_edits_in_a_marked_file_are_kept_as_prev(self):
        t = self.tmpdir()
        self.assertEqual(self.run_adapt(t, "--tool", "codex")[0], 0)
        path = os.path.join(t, "AGENTS.md")
        edited = self.read(path) + "\nOur own rule.\n"
        with open(path, "w") as fh:
            fh.write(edited)
        rc, out = self.run_adapt(t, "--tool", "codex")
        self.assertEqual(rc, 0, out)
        self.assertIn("AGENTS.md.prev", out)
        self.assertEqual(self.read(t, "AGENTS.md.prev"), edited)
        self.assertNotIn("Our own rule.", self.read(path))
        os.remove(os.path.join(t, "AGENTS.md.prev"))
        rc, out = self.run_adapt(t, "--tool", "codex")
        self.assertIn("unchanged AGENTS.md", out)
        self.assertFalse(os.path.exists(os.path.join(t, "AGENTS.md.prev")))

    def test_loop_config_posture_rewrite(self):
        text = "# Loop Config\n\nintro\n\ndelivery: push-branch            # local | push-branch\n" \
               "git_autonomy: branch+commit+push   # none | ...\n"
        got = adapt.adapter_loop_config(text)
        self.assertIn("\ndelivery: local                  # local | push-branch\n", got)
        self.assertIn("\ngit_autonomy: none                 # none | ...\n", got)
        self.assertTrue(got.startswith("# Loop Config\n\n> **Codex / Cursor copy**"))
        with self.assertRaises(adapt.AdaptError):
            adapt.adapter_loop_config("# Loop Config\n\ndelivery: local\n")
