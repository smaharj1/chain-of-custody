"""doctor.py as a program: the doctor.v1 contract, exit codes, modes, safety around claude and --live, and portability (IMP-22, IMP-14)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from tests import REPO_ROOT, SCRIPTS_DIR
from tests.helpers import BaseTestCase, install_fake_claude, read_jsonl
from tests.helpers.fakehome import FAKE_AWS_KEY_ID, FAKE_AWS_SECRET, FAKE_GH_TOKEN

from doctorlib.model import CHECKS

from .helpers import BoundaryCase, DOCTOR, run_doctor

SECRET_FRAGMENTS = (FAKE_AWS_KEY_ID, FAKE_AWS_SECRET, FAKE_GH_TOKEN, "FAKESESSIONTOKEN", "ZmFrZTpmYWtl")


class Cli(BoundaryCase):
    def run_cli(self, repo, *args, env_extra=None):
        env = self.fake_home.env(env_extra)
        return run_doctor(["--repo", repo.path, "--home", self.fake_home.path, "--claude-version", "2.1.305"] + list(args), env)

    def test_json_document_shape(self):
        repo = self.kit_repo()
        rc, out, err = self.run_cli(repo, "--json")
        doc = json.loads(out)
        self.assertEqual(doc["schema"], "doctor.v1")
        self.assertEqual(set(doc), {"schema", "repo", "level", "next", "checks", "attestations"})
        self.assertEqual(set(doc["level"]), {"configured", "effective", "caps"})
        self.assertRegex(doc["level"]["configured"], r"^L[0-5]$")
        self.assertRegex(doc["level"]["effective"], r"^L[0-5]$")
        for cap in doc["level"]["caps"]:
            self.assertEqual(set(cap), {"name", "level", "reason"})
            self.assertTrue(cap["reason"])
        self.assertTrue(doc["checks"])
        for c in doc["checks"]:
            self.assertTrue({"id", "severity", "status", "message", "remediation"} <= set(c), c)
            self.assertIn(c["severity"], ("error", "warn", "info"))
            self.assertIn(c["status"], ("pass", "fail", "unknown", "skipped"))
        for a in doc["attestations"]:
            self.assertEqual(set(a), {"key", "status", "detail"})

    def test_out_of_the_box_is_l1_and_exit_is_one_because_errors_exist(self):
        repo = self.kit_repo()
        rc, out, err = self.run_cli(repo, "--json")
        doc = json.loads(out)
        self.assertEqual(doc["level"]["effective"], "L1")
        self.assertEqual(rc, 1)

    def test_exit_zero_when_no_errors(self):
        repo = self.kit_repo()
        rc, out, err = self.run_cli(repo, "--credentials")
        self.assertEqual(rc, 0, out)

    def test_exit_one_with_a_planted_credential(self):
        self.fake_home.plant_all()
        repo = self.kit_repo()
        rc, out, err = self.run_cli(repo, "--credentials", env_extra={"GIT_CONFIG_GLOBAL": self.fake_home.planted["gitconfig"]})
        self.assertEqual(rc, 1)
        for rid in ("cred.aws.profile", "cred.aws.sso-cache", "cred.gh.hosts", "cred.git.credentials-file", "cred.git.credential-helper", "cred.ssh.agent", "cred.ssh.keys",
                    "cred.docker.config", "cred.kube", "cred.files.gcloud", "cred.files.azure"):
            self.assertIn(rid, out)

    def test_the_boundary_fixture_human_credentials_planted_in_a_temp_home(self):
        """Report §15 item 9 'Human credentials (RC-5)': gh token, git credential helper, SSH agent, GH_TOKEN/GITHUB_TOKEN. Offline half: the doctor flags every one."""
        h = self.fake_home
        h.plant_gh_hosts()
        h.plant_git_credential_helper()
        h.plant_ssh_agent_socket()
        repo = self.kit_repo()
        rc, out, err = self.run_cli(repo, "--credentials", "--json", env_extra={"GH_TOKEN": FAKE_GH_TOKEN, "GITHUB_TOKEN": FAKE_GH_TOKEN, "GIT_CONFIG_GLOBAL": h.planted["gitconfig"]})
        doc = json.loads(out)
        failing = {c["id"] for c in doc["checks"] if c["status"] == "fail" and c["severity"] == "error"}
        self.assertTrue({"cred.gh.hosts", "cred.git.credential-helper", "cred.ssh.agent", "cred.env.github"} <= failing, failing)
        self.assertEqual(doc["level"]["effective"], "L1")
        cap = [c for c in doc["level"]["caps"] if c["name"] == "credentials"][0]
        self.assertEqual(cap["level"], "L1")
        self.assertEqual(rc, 1)

    def test_no_secret_value_is_ever_printed(self):
        self.fake_home.plant_all()
        repo = self.kit_repo(origin="https://fake-user:%s@github.com/acme/widgets.git" % FAKE_GH_TOKEN)
        extra = {"GH_TOKEN": FAKE_GH_TOKEN, "AWS_ACCESS_KEY_ID": FAKE_AWS_KEY_ID, "AWS_SECRET_ACCESS_KEY": FAKE_AWS_SECRET, "GIT_CONFIG_GLOBAL": self.fake_home.planted["gitconfig"]}
        for args in ([], ["--json"], ["--credentials"], ["--level"], ["--deploy-surface"]):
            rc, out, err = self.run_cli(repo, *args, env_extra=extra)
            for frag in SECRET_FRAGMENTS:
                self.assertNotIn(frag, out + err, "%s leaked with %s" % (frag, args))

    def test_modes_select_groups(self):
        repo = self.kit_repo()
        _, cred_out, _ = self.run_cli(repo, "--credentials", "--json")
        ids = {c["id"].split(".")[0] for c in json.loads(cred_out)["checks"]}
        self.assertEqual(ids, {"cred"})
        _, dep_out, _ = self.run_cli(repo, "--deploy-surface", "--json")
        self.assertEqual({c["id"].split(".")[0] for c in json.loads(dep_out)["checks"]}, {"deploy"})
        _, both, _ = self.run_cli(repo, "--credentials", "--deploy-surface", "--json")
        self.assertEqual({c["id"].split(".")[0] for c in json.loads(both)["checks"]}, {"cred", "deploy"})
        _, all_out, _ = self.run_cli(repo, "--json")
        self.assertGreater(len({c["id"].split(".")[0] for c in json.loads(all_out)["checks"]}), 8)

    def test_level_flag_prints_the_caps_and_accepts_a_level(self):
        repo = self.kit_repo()
        rc, out, err = self.run_cli(repo, "--level", "L4")
        self.assertIn("configured L4, effective L1", out)
        self.assertIn("prerequisites", out)
        self.assertIn("next:", out)
        self.assertNotIn("Credentials reachable", out)

    def test_bad_level_is_a_usage_error(self):
        rc, out, err = self.run_cli(self.kit_repo(), "--level", "L9")
        self.assertEqual(rc, 2)

    def test_unknown_flag_is_a_usage_error(self):
        rc, out, err = self.run_cli(self.kit_repo(), "--nope")
        self.assertEqual(rc, 2)

    def test_list_checks_prints_stable_ids(self):
        rc, out, err = run_doctor(["--list-checks"], self.fake_home.env())
        self.assertEqual(rc, 0)
        ids = [l.split()[0] for l in out.strip().split("\n")]
        self.assertEqual(len(ids), len(set(ids)))
        for want in ("cred.env", "cred.aws.profile", "cred.gh.hosts", "cred.git.config", "cred.ssh.agent", "cred.docker.socket", "cred.kube", "cred.net.metadata",
                     "deploy.attestation", "deploy.surface", "settings.workflow-allow", "sandbox.enabled", "sandbox.failIfUnavailable", "mcp.servers", "gh.main-protection",
                     "gh.bypass", "bot.identity", "codeowners.coverage", "ci.lint", "secrets.tracked", "secrets.gitignore", "version.claude", "config.evidence-gate"):
            self.assertIn(want, ids)

    def test_every_registered_check_id_follows_the_naming_scheme(self):
        for c in CHECKS:
            self.assertRegex(c["id"], r"^[a-zA-Z0-9]+(\.[A-Za-z0-9_-]+)+$")
            self.assertTrue(c["group"])
            for e in c["emits"]:
                self.assertRegex(e, r"^[a-zA-Z0-9]+(\.[A-Za-z0-9_-]+)*\.?$")

    def test_list_checks_shows_the_ids_a_check_emits_too(self):
        rc, out, err = run_doctor(["--list-checks"], self.fake_home.env())
        self.assertIn("deploy.push-workflow", out)
        self.assertIn("gh.required-review", out)
        self.assertIn("cred.git.credential-helper", out)

    def test_config_ref_reads_the_committed_config(self):
        repo = self.kit_repo(config={"autonomy_level": "L2"}, commit=True)
        base = repo.head()
        from .helpers import config_text
        repo.write(".claude/loop.config.md", config_text({"autonomy_level": "L5"}))
        _, out, _ = self.run_cli(repo, "--json")
        self.assertEqual(json.loads(out)["level"]["configured"], "L5")
        _, out, _ = self.run_cli(repo, "--json", "--config-ref", base)
        self.assertEqual(json.loads(out)["level"]["configured"], "L2")
        dirty = [c for c in json.loads(out)["checks"] if c["id"] == "config.dirty"][0]
        self.assertEqual(dirty["status"], "pass")

    def test_uncommitted_config_edits_are_flagged(self):
        repo = self.kit_repo(commit=True)
        from .helpers import config_text
        repo.write(".claude/loop.config.md", config_text({"autonomy_level": "L3"}))
        _, out, _ = self.run_cli(repo, "--json")
        dirty = [c for c in json.loads(out)["checks"] if c["id"] == "config.dirty"][0]
        self.assertEqual(dirty["status"], "fail")

    def test_control_characters_from_the_repo_are_neutralised_in_text_output(self):
        repo = self.kit_repo(files={".mcp.json": json.dumps({"mcpServers": {"aws-\x1b[31mred": {"command": "x"}}})})
        rc, out, err = self.run_cli(repo)
        self.assertNotIn("\x1b", out)
        self.assertIn("aws-?[31mred", out)


class SafetyAroundClaudeAndLive(BoundaryCase):
    def test_live_refuses_inside_a_claude_code_session(self):
        repo = self.kit_repo()
        env = self.fake_home.env({"CLAUDECODE": "1"})
        rc, out, err = run_doctor(["--live", "--repo", repo.path, "--home", self.fake_home.path], env)
        self.assertEqual(rc, 2)
        self.assertIn("human-only", err + out)

    def test_the_doctor_never_runs_claude_inside_a_session_even_if_one_is_on_the_path(self):
        d = self.tmpdir()
        fake = install_fake_claude(d, {"version": "2.1.305"})
        repo = self.kit_repo()
        env = self.fake_home.env({"CLAUDECODE": "1", "PATH": d + os.pathsep + "/usr/bin:/bin"})
        rc, out, err = run_doctor(["--json", "--repo", repo.path, "--home", self.fake_home.path], env)
        doc = json.loads(out)
        v = [c for c in doc["checks"] if c["id"] == "version.claude"][0]
        self.assertEqual(v["status"], "fail")
        self.assertIn("inside a Claude Code session", v["message"])
        self.assertEqual(read_jsonl(fake + ".log.jsonl"), [])
        self.assertFalse(os.path.exists(fake + ".state"))

    def test_from_a_plain_terminal_the_claude_on_path_is_asked_for_its_version(self):
        d = self.tmpdir()
        fake = install_fake_claude(d, {"version": "2.1.305 (Claude Code)"})
        repo = self.kit_repo()
        env = self.fake_home.env({"PATH": d + os.pathsep + "/usr/bin:/bin"})
        rc, out, err = run_doctor(["--json", "--repo", repo.path, "--home", self.fake_home.path], env)
        v = [c for c in json.loads(out)["checks"] if c["id"] == "version.claude"][0]
        self.assertEqual(v["status"], "pass", v["message"])

    def test_kit_claude_bin_is_how_tests_inject_the_fake(self):
        d = self.tmpdir()
        fake = install_fake_claude(d, {"version": "2.1.200"})
        repo = self.kit_repo()
        env = self.fake_home.env({"KIT_CLAUDE_BIN": fake, "CLAUDECODE": "1"})
        rc, out, err = run_doctor(["--json", "--repo", repo.path, "--home", self.fake_home.path], env)
        v = [c for c in json.loads(out)["checks"] if c["id"] == "version.claude"][0]
        self.assertIn("below", v["message"])

    def test_no_network_helper_is_invoked_without_live(self):
        """Without --live the doctor never calls the metadata probe or gh: a doctor run in a sandbox with no network must still work."""
        repo = self.kit_repo()
        rc, out, err = run_doctor(["--json", "--repo", repo.path, "--home", self.fake_home.path, "--claude-version", "2.1.305"], self.fake_home.env({"PATH": "/usr/bin:/bin"}))
        ids = {c["id"]: c for c in json.loads(out)["checks"]}
        self.assertEqual(ids["cred.net.metadata"]["status"], "unknown")
        for rid in ("cred.live.sts", "cred.live.gh", "cred.live.git-fill", "cred.live.ssh"):
            self.assertEqual(ids[rid]["status"], "skipped")


class Portable(BaseTestCase):
    def test_runs_as_python3_from_a_copy_of_the_kit_in_any_repo(self):
        """`python3 .claude/scripts/doctor.py` from a project the kit was copied into (single-step `cp -r .claude .` install)."""
        r = self.repo()
        shutil.copytree(SCRIPTS_DIR, os.path.join(r.path, ".claude", "scripts"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(REPO_ROOT, ".claude", "KIT_VERSION"), os.path.join(r.path, ".claude", "KIT_VERSION"))
        shutil.copy(os.path.join(REPO_ROOT, ".claude", "loop.config.md"), os.path.join(r.path, ".claude", "loop.config.md"))
        h = self.home()
        env = h.env()
        p = subprocess.run(["python3", ".claude/scripts/doctor.py", "--json", "--claude-version", "2.1.305", "--home", h.path], cwd=r.path, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertIn(p.returncode, (0, 1), p.stderr.decode())
        doc = json.loads(p.stdout.decode())
        self.assertEqual(doc["schema"], "doctor.v1")
        self.assertEqual(os.path.realpath(doc["repo"]), os.path.realpath(r.path))

    def test_runs_from_a_subdirectory_and_finds_the_repo(self):
        r = self.repo()
        os.makedirs(os.path.join(r.path, "src", "deep"))
        h = self.home()
        p = subprocess.run([sys.executable, DOCTOR, "--json", "--claude-version", "2.1.305", "--home", h.path], cwd=os.path.join(r.path, "src", "deep"), env=h.env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(os.path.realpath(json.loads(p.stdout.decode())["repo"]), os.path.realpath(r.path))

    def test_runs_outside_a_git_repository_without_crashing(self):
        d = self.tmpdir()
        h = self.home()
        p = subprocess.run([sys.executable, DOCTOR, "--json", "--claude-version", "2.1.305", "--home", h.path], cwd=d, env=h.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertIn(p.returncode, (0, 1), p.stderr.decode())
        doc = json.loads(p.stdout.decode())
        self.assertEqual(doc["level"]["effective"], "L1")

    def test_a_check_that_crashes_is_a_failed_check_not_a_pass_or_a_crash(self):
        from doctorlib import model

        @model.check("test.crash", "test-only")
        def boom(ctx):
            raise RuntimeError("boom")
        try:
            r = self.repo()
            h = self.home()
            ctx = model.Context(repo=r.path, home=h.path, env=h.env())
            res = model.run_checks(ctx, {"test-only"})
            self.assertEqual((res[0].id, res[0].status, res[0].severity), ("test.crash", "fail", "error"))
            self.assertIn("boom", res[0].message)
        finally:
            model.CHECKS[:] = [c for c in model.CHECKS if c["id"] != "test.crash"]


if __name__ == "__main__":
    unittest.main()
