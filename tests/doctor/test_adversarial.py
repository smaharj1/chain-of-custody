"""Adversarial self-review of the doctor (IMP-24 item 8): each row is a way an agent with Bash could hold a mutating credential,
or hide a deploy path, or attack the doctor itself, that an earlier version of the doctor missed or could have missed.

Every test names the attack in its docstring. A row that cannot be detected statically is marked in docs/security/credentials.md
under "What the doctor cannot see" and has a test that pins the honest `unknown` rather than a silent pass.
"""
from __future__ import annotations

import json
import os
import time
import unittest

from doctorlib import github
from doctorlib.model import is_known_id, run_checks

from tests.helpers.fakehome import FAKE_AWS_KEY_ID, FAKE_AWS_SECRET, FAKE_GH_TOKEN

from . import ghfixtures as gf
from .helpers import BoundaryCase, guarded_run, run_doctor


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


class Cred(BoundaryCase):
    GROUPS = {"credentials"}

    # -- credentials exported from places this process's own env does not show ----------------------
    def test_token_exported_from_shell_startup_files(self):
        """The doctor's env is clean but every shell the agent starts sources ~/.zshrc and exports GH_TOKEN."""
        h = self.fake_home.path
        write(os.path.join(h, ".zshrc"), "export PATH=$PATH:/x\nexport GH_TOKEN=%s\n" % FAKE_GH_TOKEN)
        write(os.path.join(h, ".bashrc"), "export AWS_ACCESS_KEY_ID=%s\nexport AWS_SECRET_ACCESS_KEY=%s\n" % (FAKE_AWS_KEY_ID, FAKE_AWS_SECRET))
        write(os.path.join(h, ".config", "fish", "config.fish"), "set -gx NPM_TOKEN npm_%s\n" % ("A" * 36))
        write(os.path.join(h, ".profile"), "GITHUB_TOKEN=$(gh auth token)\n")
        hits = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.shellrc")
        msg = " ".join(r.message for r in hits)
        for want in (".zshrc", ".bashrc", "config.fish", ".profile", "GH_TOKEN", "AWS_ACCESS_KEY_ID", "NPM_TOKEN", "GITHUB_TOKEN"):
            self.assertIn(want, msg)
        for secret in (FAKE_GH_TOKEN, FAKE_AWS_SECRET):
            self.assertNotIn(secret, msg)

    def test_bash_env_startup_file(self):
        """BASH_ENV runs a file in every non-interactive shell; the doctor reads the file it names."""
        p = write(os.path.join(self.fake_home.path, "boot.sh"), "export VERCEL_TOKEN=abc123456789\n")
        res = self.results(self.ctx(self.kit_repo(), {"BASH_ENV": p}))
        self.assertTrue(any("boot.sh" in r.message for r in self.assertFails(res, "cred.shellrc")))
        self.assertEqual(self.assertFails(res, "cred.env.shell")[0].severity, "warn")

    def test_env_variable_named_env_that_is_not_a_file_is_ignored(self):
        res = self.check_one(self.ctx(self.kit_repo(), {"ENV": "production"}), "cred.env")
        self.assertClean(res, "cred.env")

    def test_repo_envrc_and_dotenv_with_real_credentials_but_dev_values_are_fine(self):
        repo = self.kit_repo()
        write(os.path.join(repo.path, ".envrc"), "export AWS_PROFILE=prod-admin\n")
        write(os.path.join(repo.path, ".env"), "DATABASE_URL=postgres://dev:dev@localhost:5432/app\nSTRIPE_SECRET_KEY=sk_test_%s\nPORT=3000\nAPI_KEY=changeme\n" % ("A" * 24))
        res = self.results(self.ctx(repo))
        hits = " ".join(r.message for r in self.assertFails(res, "cred.shellrc"))
        self.assertIn(".envrc", hits)
        self.assertNotIn(".env (", hits)
        write(os.path.join(repo.path, ".env"), "STRIPE_SECRET_KEY=sk_live_%s\n" % ("A" * 24))
        hits = " ".join(r.message for r in self.assertFails(self.results(self.ctx(repo)), "cred.shellrc"))
        self.assertIn("STRIPE_SECRET_KEY", hits)

    def test_dotenv_pointing_at_a_shared_database(self):
        repo = self.kit_repo()
        write(os.path.join(repo.path, ".env.local"), "DATABASE_URL=postgres://app:pw12345@prod-db.example.invalid:5432/app\n")
        self.assertIn("DATABASE_URL", self.assertFails(self.results(self.ctx(repo)), "cred.shellrc")[0].message)

    # -- history ---------------------------------------------------------------------------------------
    def test_secret_typed_into_a_command_and_left_in_history(self):
        """`export GH_TOKEN=ghp_...` typed once is readable by the agent forever."""
        h = self.fake_home.path
        write(os.path.join(h, ".zsh_history"), ": 1:0;ls\n: 2:0;curl -H 'Authorization: token %s' https://api.example.invalid\n" % FAKE_GH_TOKEN)
        write(os.path.join(h, ".bash_history"), "mysql -u root --password=hunter2hunter2 db\n")
        res = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.history")
        sev = {("zsh" if "zsh" in r.message else "bash"): r.severity for r in res}
        self.assertEqual(sev, {"zsh": "error", "bash": "warn"})
        self.assertNotIn(FAKE_GH_TOKEN, " ".join(r.message for r in res))

    def test_clean_history_passes(self):
        write(os.path.join(self.fake_home.path, ".bash_history"), "ls\ncd /tmp\ngit status\n")
        self.assertClean(self.results(self.ctx(self.kit_repo())), "cred.history")

    # -- ssh -------------------------------------------------------------------------------------------
    def test_private_key_outside_ssh_dir_named_in_ssh_config(self):
        h = self.fake_home.path
        key = write(os.path.join(h, "keys", "deploy_key"), "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nxx\n-----END OPENSSH " + "PRIVATE KEY-----\n")
        write(os.path.join(h, ".ssh", "config"), "Host github.com\n  IdentityFile %s\n" % key)
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.ssh.identity-files")
        self.assertIn("deploy_key", hit[0].message)

    def test_tilde_form_of_identity_file(self):
        h = self.fake_home.path
        write(os.path.join(h, "keys", "k"), "-----BEGIN RSA " + "PRIVATE KEY-----\nxx\n")
        write(os.path.join(h, ".ssh", "config"), "Host x\n  IdentityFile ~/keys/k\n")
        self.assertFails(self.results(self.ctx(self.kit_repo(), {"HOME": h})), "cred.ssh.identity-files")

    def test_ssh_controlmaster_socket_is_an_authenticated_session_with_no_key(self):
        import socket
        d = os.path.join(self.fake_home.path, ".ssh", "sockets")
        os.makedirs(d)
        cwd = os.getcwd()
        os.chdir(d)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind("cm-prod")
            s.close()
        finally:
            os.chdir(cwd)
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.ssh.control-sockets")
        self.assertIn("cm-prod", hit[0].message)

    # -- containers --------------------------------------------------------------------------------------
    def test_remote_docker_context(self):
        h = self.fake_home.path
        write(os.path.join(h, ".docker", "contexts", "meta", "abc", "meta.json"), '{"Name": "prod", "Endpoints": {"docker": {"Host": "ssh://deploy@prod.example.invalid"}}}')
        write(os.path.join(h, ".docker", "contexts", "meta", "def", "meta.json"), '{"Name": "local", "Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}')
        hits = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.docker.contexts")
        self.assertEqual(len(hits), 1)
        self.assertIn("prod", hits[0].message)

    # -- registries and databases ------------------------------------------------------------------------
    def test_registry_tokens_in_the_repo_itself(self):
        repo = self.kit_repo()
        write(os.path.join(repo.path, ".npmrc"), "//registry.npmjs.org/:_authToken=npm_%s\n" % ("B" * 36))
        write(os.path.join(repo.path, ".pypirc"), "[pypi]\npassword = pypi-AgEIcHlwaS5vcmc\n")
        write(os.path.join(repo.path, "sub", ".cargo", "credentials.toml"), "x")  # not at the root: not scanned
        res = self.assertFails(self.results(self.ctx(repo)), "cred.repo-files")
        self.assertEqual(sorted(r.id for r in res), ["cred.repo-files.npm", "cred.repo-files.pypi"])

    def test_database_password_files_and_env(self):
        h = self.fake_home.path
        write(os.path.join(h, ".pgpass"), "db.example.invalid:5432:app:app:pw12345\n")
        write(os.path.join(h, ".my.cnf"), "[client]\nuser=root\npassword=hunter2\n")
        res = self.results(self.ctx(self.kit_repo(), {"PGPASSWORD": "x", "MYSQL_PWD": "y"}))
        ids = {r.id for r in res if r.status == "fail"}
        self.assertTrue({"cred.files.pgpass", "cred.files.mysql", "cred.env.database"} <= ids, ids)

    def test_password_manager_sessions_and_stores(self):
        h = self.fake_home.path
        write(os.path.join(h, ".password-store", "github.gpg"), "x")
        res = self.results(self.ctx(self.kit_repo(), {"OP_SESSION_my": "tok", "BW_SESSION": "tok"}))
        self.assertTrue(self.assertFails(res, "cred.env.vault"))
        self.assertTrue(self.assertFails(res, "cred.files.pass"))
        self.assertTrue(self.assertFails(res, "cred.keyring"))

    def test_terraform_cli_config_and_secret_vars(self):
        res = self.check_one(self.ctx(self.kit_repo(), {"TF_CLI_CONFIG_FILE": "/x/tf.rc", "TF_VAR_db_password": "x", "TF_VAR_region": "us-east-1"}), "cred.env")
        names = " ".join(r.message for r in self.assertFails(res, "cred.env.deploy"))
        self.assertIn("TF_CLI_CONFIG_FILE", names)
        self.assertIn("TF_VAR_db_password", names)
        self.assertNotIn("TF_VAR_region", names)

    def test_aws_login_cache(self):
        d = os.path.join(self.fake_home.path, ".aws", "login", "cache")
        write(os.path.join(d, "x.json"), '{"accessToken": {"accessKeyId": "x"}, "refreshToken": "y"}')
        write(os.path.join(d, "y.json"), '{"AccessKeyId": "x", "Expiration": "2099-01-01T00:00:00Z"}')
        self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.aws.sso-cache")

    # -- indirection in git ------------------------------------------------------------------------------------
    def test_credential_in_a_submodule_config(self):
        repo = self.kit_repo()
        write(os.path.join(repo.path, ".git", "modules", "vendor", "lib", "config"), '[remote "origin"]\n\turl = https://u:%s@github.com/x/y.git\n' % ("t" * 20))
        hit = self.assertFails(self.results(self.ctx(repo)), "cred.git.submodules")
        self.assertIn("modules", hit[0].message)

    def test_credential_in_a_per_worktree_config(self):
        repo = self.kit_repo()
        write(os.path.join(repo.path, ".git", "worktrees", "wt", "config.worktree"), "[credential]\n\thelper = store\n")
        self.assertFails(self.results(self.ctx(repo)), "cred.git.submodules")

    def test_git_dir_in_the_environment_cannot_redirect_the_doctors_git_calls(self):
        """GIT_DIR pointing at another repository must not change which repo the doctor inspects."""
        other = self.repo()
        other.git("config", "credential.helper", "store")
        mine = self.kit_repo()
        res = self.results(self.ctx(mine, {"GIT_DIR": os.path.join(other.path, ".git"), "GIT_WORK_TREE": other.path}))
        self.assertClean(res, "cred.git.config")

    def test_hostile_repo_config_cannot_run_code_through_the_doctors_git_calls(self):
        """core.fsmonitor / core.hooksPath / core.pager / alias set in the repo's own config must not execute while the doctor reads it."""
        repo = self.kit_repo()
        marker = os.path.join(self.tmpdir(), "marker")
        script = write(os.path.join(self.tmpdir(), "evil.sh"), "#!/bin/sh\necho ran >> %s\nexit 0\n" % marker, 0o755)
        repo.git("config", "core.fsmonitor", script)
        repo.git("config", "core.hooksPath", os.path.dirname(script))
        repo.git("config", "core.pager", script)
        repo.git("config", "diff.external", script)
        write(os.path.join(os.path.dirname(script), "post-checkout"), "#!/bin/sh\necho ran >> %s\n" % marker, 0o755)
        run_checks(self.ctx(repo))
        self.assertFalse(os.path.exists(marker), "the doctor ran code from the repository's git config")

    # -- the machine itself ---------------------------------------------------------------------------------------
    def test_root_equivalent_group_membership(self):
        for group in ("docker", "lxd", "libvirt", "disk"):
            with self.subTest(group=group):
                res = self.results(self.ctx(self.kit_repo(), groups=["staff", group]))
                self.assertIn(group, self.assertFails(res, "cred.groups")[0].message)
        self.assertClean(self.results(self.ctx(self.kit_repo(), groups=["staff", "admin"])), "cred.groups")

    def test_passwordless_sudo(self):
        def sudo_ok(argv, timeout=10.0, env=None, cwd=None, input_text=None):
            if argv[0] == "sudo":
                return 0, "User x may run the following commands:\n    (ALL) NOPASSWD: ALL\n", ""
            return guarded_run(argv, timeout, env, cwd, input_text)
        self.assertFails(self.results(self.ctx(self.kit_repo(), run=sudo_ok)), "cred.sudo")
        self.assertClean(self.results(self.ctx(self.kit_repo())), "cred.sudo")

    def test_sudo_that_needs_a_password_is_fine_and_missing_sudo_is_fine(self):
        def no_sudo(argv, timeout=10.0, env=None, cwd=None, input_text=None):
            if argv[0] == "sudo":
                return 127, "", "not found"
            return guarded_run(argv, timeout, env, cwd, input_text)
        self.assertClean(self.results(self.ctx(self.kit_repo(), run=no_sudo)), "cred.sudo")

    def test_running_as_your_own_os_user_is_called_out_per_platform(self):
        res = self.results(self.ctx(self.kit_repo(), platform="darwin"))
        r = self.by_id(res, "cred.same-user")[0]
        self.assertEqual((r.status, r.severity), ("unknown", "warn"))
        self.assertIn("keychain", r.message)
        r = self.by_id(self.results(self.ctx(self.kit_repo(), platform="linux")), "cred.same-user")[0]
        self.assertIn("/proc/<pid>/environ", r.message)
        r = self.by_id(self.results(self.ctx(self.kit_repo({"agent_isolation": "separate-os-user"}))), "cred.same-user")[0]
        self.assertEqual(r.status, "pass")

    def test_dbus_session_and_gpg_keys(self):
        write(os.path.join(self.fake_home.path, ".gnupg", "private-keys-v1.d", "k.key"), "x")
        res = self.results(self.ctx(self.kit_repo(), {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}))
        msg = self.assertFails(res, "cred.keyring")[0].message
        self.assertIn("D-Bus", msg)
        self.assertIn("gnupg", msg)

    # -- attacks on the doctor ---------------------------------------------------------------------------------
    def test_named_pipe_in_place_of_a_file_does_not_hang_the_doctor(self):
        """A hostile repo replaces config/settings/CODEOWNERS/workflows with FIFOs: a plain open() would block forever."""
        repo = self.kit_repo()
        for rel in (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", ".github/CODEOWNERS", ".worktreeinclude", ".npmrc", "package.json", ".github/workflows/x.yml",
                    ".claude/context/backend.md"):
            p = os.path.join(repo.path, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.exists(p):
                os.unlink(p)
            os.mkfifo(p)
        os.unlink(os.path.join(repo.path, ".claude", "loop.config.md"))
        os.mkfifo(os.path.join(repo.path, ".claude", "loop.config.md"))
        os.unlink(os.path.join(repo.path, ".claude", "KIT_VERSION"))
        os.mkfifo(os.path.join(repo.path, ".claude", "KIT_VERSION"))
        rc, out, err = run_doctor(["--json", "--repo", repo.path, "--home", self.fake_home.path, "--claude-version", "2.1.305"], self.fake_home.env(), timeout=40)
        self.assertIn(rc, (0, 1), err)
        doc = json.loads(out)
        ids = {c["id"]: c for c in doc["checks"]}
        self.assertEqual(ids["config.load"]["status"], "fail")
        self.assertIn("not a regular file", ids["config.load"]["message"])
        self.assertEqual(doc["level"]["effective"], "L1")

    def test_symlink_to_a_device_is_not_read(self):
        repo = self.kit_repo()
        os.symlink("/dev/zero", os.path.join(repo.path, ".mcp.json"))
        t0 = time.time()
        res = run_checks(self.ctx(repo))
        self.assertLess(time.time() - t0, 20)
        self.assertTrue(any(r.id == "mcp.parse" and "not a regular file" in r.message for r in res))

    def test_huge_settings_file_is_capped_not_slurped(self):
        repo = self.kit_repo()
        with open(os.path.join(repo.path, ".claude", "settings.json"), "w") as fh:
            fh.write('{"a": "' + "x" * (6 << 20) + '"}')
        res = run_checks(self.ctx(repo))
        self.assertTrue(self.assertFails(res, "settings.parse"))

    def test_unparsable_json_error_does_not_echo_file_content(self):
        repo = self.kit_repo(files={".claude/settings.json": '{"token": "%s" oops' % FAKE_GH_TOKEN})
        hit = self.assertFails(run_checks(self.ctx(repo)), "settings.parse")
        self.assertNotIn(FAKE_GH_TOKEN, hit[0].message)


class DeployHiding(BoundaryCase):
    GROUPS = {"deploy-surface"}

    def wf(self, run, on="push", extra=""):
        return "name: x\non: %s\njobs:\n  d:\n    runs-on: ubuntu-latest\n%s    steps:\n      - uses: actions/checkout@v4\n      - run: %s\n" % (on, extra, run)

    def surface(self, repo):
        return self.ctx(repo).fact("deploy_surface")

    def test_deploy_hidden_in_a_script_the_workflow_calls(self):
        for run, files in (("./scripts/go.sh", {"scripts/go.sh": "#!/bin/sh\nterraform apply -auto-approve\n"}),
                           ("bash tools/ship.sh --prod", {"tools/ship.sh": "cdk deploy\n"}),
                           ("python3 ci/release.py", {"ci/release.py": "import subprocess\nsubprocess.run('kubectl apply -f k8s/', shell=True)\n"}),
                           ("make rollout", {"Makefile": "rollout:\n\thelm upgrade app ./chart\n"}),
                           ("npm run publish:stage", {"package.json": json.dumps({"scripts": {"publish:stage": "flyctl deploy"}})}),
                           ("pnpm release", {"package.json": json.dumps({"scripts": {"release": "x", "prerelease": "wrangler deploy"}})}),
                           ("just ship", {"justfile": "ship:\n    sst deploy\n"})):
            with self.subTest(run=run):
                f = {".github/workflows/x.yml": self.wf(run)}
                f.update(files)
                repo = self.kit_repo(files=f)
                wfs = self.surface(repo)["workflows"]
                self.assertTrue(wfs[0]["deploys"], run)
                self.assertTrue(any("script or local action" in l for l in wfs[0]["labels"]) or "deploy script" in wfs[0]["labels"], wfs[0]["labels"])

    def test_the_referenced_script_joins_the_fingerprint(self):
        f = {".github/workflows/x.yml": self.wf("./scripts/go.sh"), "scripts/go.sh": "echo hi\n"}
        repo = self.kit_repo(files=f)
        before = self.surface(repo)["fingerprint"]
        self.assertIn("scripts/go.sh", self.surface(repo)["files"])
        repo.write("scripts/go.sh", "echo hi\nterraform apply\n")
        self.assertNotEqual(self.surface(repo)["fingerprint"], before)

    def test_deploy_in_a_local_composite_action(self):
        f = {".github/workflows/x.yml": "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/actions/ship\n",
             ".github/actions/ship/action.yml": "name: ship\nruns:\n  using: composite\n  steps:\n    - run: aws s3 sync ./dist s3://prod-bucket\n      shell: bash\n"}
        wfs = self.surface(self.kit_repo(files=f))["workflows"]
        self.assertTrue(wfs[0]["deploys"])

    def test_external_reusable_workflow_that_sounds_like_a_deploy(self):
        f = {".github/workflows/x.yml": "name: x\non: push\njobs:\n  go:\n    uses: acme/shared/.github/workflows/deploy-prod.yml@main\n"}
        wfs = self.surface(self.kit_repo(files=f))["workflows"]
        self.assertTrue(wfs[0]["deploys"])
        self.assertIn("cannot be inspected", wfs[0]["labels"][0])

    def test_external_action_with_a_deploy_like_name(self):
        text = "name: x\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: acme-corp/deploy-to-prod@v2\n      - uses: actions/checkout@v4\n"
        wfs = self.surface(self.kit_repo(files={".github/workflows/x.yml": text}))["workflows"]
        self.assertTrue(wfs[0]["deploys"])
        self.assertIn("cannot be inspected", wfs[0]["labels"][0])

    def test_script_path_traversal_outside_the_repo_is_not_read(self):
        outside = write(os.path.join(self.tmpdir(), "evil.sh"), "terraform apply\n")
        f = {".github/workflows/x.yml": self.wf("bash ../../../../../../../../%s" % outside.lstrip("/"))}
        wfs = self.surface(self.kit_repo(files=f))["workflows"]
        self.assertFalse(wfs[0]["deploys"])

    def test_exclusion_only_branch_filter_fires_on_every_other_branch(self):
        text = "name: x\non:\n  push:\n    branches:\n      - '!main'\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cdk deploy\n"
        wfs = self.surface(self.kit_repo(files={".github/workflows/x.yml": text}))["workflows"]
        self.assertEqual(wfs[0]["fires_on"], ["push:branches"])

    def test_look_alike_branch_patterns_are_not_treated_as_main(self):
        for pat in ("main*", "ma*", "[m]ain", "**", "*", "release/**", "main/**"):
            with self.subTest(pattern=pat):
                text = "name: x\non:\n  push:\n    branches: ['%s']\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cdk deploy\n" % pat
                wfs = self.surface(self.kit_repo(files={".github/workflows/x.yml": text}))["workflows"]
                self.assertEqual(wfs[0]["fires_on"], ["push:branches"], pat)

    def test_github_pages_without_a_workflow(self):
        repo = self.kit_repo(files={"CNAME": "docs.example.invalid\n"})
        self.assertFails(self.results(self.ctx(repo)), "deploy.host.github-pages")
        repo = self.kit_repo()
        fx = gf.build()
        fx["repos/acme/widgets/pages"] = {"source": {"branch": "main", "path": "/docs"}, "html_url": "https://acme.github.io/widgets/"}
        res = self.results(self.ctx(repo, ghapi=github.fixture_ghapi(fx)))
        hit = self.assertFails(res, "deploy.host.github-pages")
        self.assertIn("pages", hit[0].message)

    def test_iac_automation_that_watches_the_repo(self):
        for fname, host in (("atlantis.yaml", "atlantis"), (".spacelift/config.yml", "spacelift"), ("env0.yml", "env0")):
            with self.subTest(file=fname):
                res = self.check_one(self.ctx(self.kit_repo(files={fname: "x\n"})), "deploy.surface")
                self.assertFails(res, "deploy.host.%s" % host)

    def test_deploy_surface_files_swapped_for_pipes_void_the_attestation(self):
        repo0 = self.kit_repo(files={"Dockerfile": "FROM scratch\n"})
        ctx0 = self.ctx(repo0)
        fp = ctx0.fact("deploy_surface")["fingerprint"]
        os.unlink(os.path.join(repo0.path, "Dockerfile"))
        os.mkfifo(os.path.join(repo0.path, "Dockerfile"))
        self.assertNotEqual(self.ctx(repo0).fact("deploy_surface")["fingerprint"], fp)


class EveryIdIsDocumented(BoundaryCase):
    def test_every_id_any_check_emits_is_registered_or_declared(self):
        """Stable ids (IMP-22): an id that appears in output but is not in --list-checks is a contract break."""
        h = self.fake_home
        h.plant_all()
        files = {".github/workflows/deploy.yml": "name: d\non: push\njobs:\n  d:\n    runs-on: x\n    steps:\n      - run: cdk deploy\n", "vercel.json": "{}", ".env.production": "x",
                 ".mcp.json": json.dumps({"mcpServers": {"aws": {"command": "x"}, "postgres": {"command": "y"}, "docs": {"command": "z"}}}),
                 ".claude/settings.json": json.dumps({"permissions": {"allow": ["Workflow"], "defaultMode": "bypassPermissions"}, "enableAllProjectMcpServers": True,
                                                      "enabledPlugins": {"deploy-on-aws@x": True}}),
                 ".devcontainer/devcontainer.json": json.dumps({"runArgs": ["--privileged"]}), ".worktreeinclude": ".env.production\n", "infra/main.tf": "",
                 ".github/CODEOWNERS": "* @a\n", ".env": "A=1\n", "pkg/.npmrc": "x"}
        repo = self.kit_repo(files=files, config={"bot": {"kind": "machine-user-pat", "login": "b"}})
        repo.write(".npmrc", "//r/:_authToken=abcdef123456\n")
        installation = {"permissions": {"workflows": "write"}}
        fx = gf.build(rulesets=[gf.ruleset(bypass=[gf.ADMIN_ALWAYS], rules=[gf.pr_rule(0, False, False), gf.checks_rule(("x",), None)])], installation=installation,
                      environments={"environments": [gf.env_with_reviewers(self_review_blocked=False)]}, actions_perms={"default_workflow_permissions": "write"},
                      info=gf.repo_info(squash_ok=False))
        ctx = self.ctx(repo, {"GH_TOKEN": "x", "AWS_PROFILE": "p", "DATABASE_URL": "postgres://u:pw1234@prod.example.invalid/db", "KUBECONFIG": "/x", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/x"},
                       ghapi=github.fixture_ghapi(fx), reach=lambda a, b, c=1.0: True, live=True,
                       run=lambda argv, timeout=10.0, env=None, cwd=None, input_text=None: guarded_run(argv, timeout, env, cwd, input_text))
        res = run_checks(ctx)
        unknown_ids = sorted({r.id for r in res if not is_known_id(r.id)})
        self.assertEqual(unknown_ids, [])
        self.assertGreater(len({r.id for r in res}), 60)


if __name__ == "__main__":
    unittest.main()
