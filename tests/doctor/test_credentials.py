"""B2: the doctor finds every credential an agent's OS user could reach (RC-5, report §5.1, §8.1)."""
from __future__ import annotations

import os
import socket
import stat
import unittest

from tests.helpers.fakehome import FAKE_AWS_KEY_ID, FAKE_AWS_SECRET, FAKE_GH_TOKEN

from .helpers import BoundaryCase


class CredCase(BoundaryCase):
    GROUPS = {"credentials"}


class CleanMachine(CredCase):
    def test_no_hits_on_a_clean_home(self):
        repo = self.kit_repo()
        res = self.results(self.ctx(repo))
        errors = [r for r in res if r.id.startswith("cred.") and r.status == "fail"]
        self.assertEqual(errors, [])

    def test_the_planted_secret_values_never_appear_in_any_message(self):
        h = self.fake_home
        h.plant_all()
        repo = self.kit_repo()
        env_extra = {"GH_TOKEN": FAKE_GH_TOKEN, "AWS_SECRET_ACCESS_KEY": FAKE_AWS_SECRET, "AWS_ACCESS_KEY_ID": FAKE_AWS_KEY_ID,
                     "GIT_CONFIG_GLOBAL": h.planted["gitconfig"]}
        res = self.results(self.ctx(repo, env_extra))
        blob = "\n".join("%s %s %s %s" % (r.id, r.message, r.remediation, r.detail) for r in res)
        for secret in (FAKE_AWS_KEY_ID, FAKE_AWS_SECRET, FAKE_GH_TOKEN, "FAKESESSIONTOKEN"):
            self.assertNotIn(secret, blob)


class AwsFiles(CredCase):
    def test_static_keys_profile(self):
        self.fake_home.plant_aws_credentials("deploy")
        res = self.results(self.ctx(self.kit_repo()))
        hit = self.assertFails(res, "cred.aws.profile")
        self.assertIn("deploy", hit[0].message)
        self.assertIn("static", hit[0].message)

    def test_sso_profile_and_cache(self):
        self.fake_home.plant_aws_config("sso", sso=True)
        self.fake_home.plant_sso_cache()
        res = self.results(self.ctx(self.kit_repo()))
        self.assertFails(res, "cred.aws.profile")
        self.assertFails(res, "cred.aws.sso-cache")

    def test_config_with_only_region_is_fine(self):
        self.fake_home.plant_aws_config("default", sso=False)
        res = self.results(self.ctx(self.kit_repo()))
        self.assertClean(res, "cred.aws.profile")

    def test_credential_process_and_role_profile(self):
        p = os.path.join(self.fake_home.path, ".aws", "config")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as fh:
            fh.write("[profile a]\ncredential_process = /usr/local/bin/get-creds\n[profile b]\nrole_arn = arn:aws:iam::123456789012:role/x\nsource_profile = a\n"
                     "[profile c]\nweb_identity_token_file = /tmp/t\nrole_arn = arn:aws:iam::123456789012:role/y\n[profile d]\nregion = x\ns3 =\n  max_concurrent_requests = 5\n")
        res = self.results(self.ctx(self.kit_repo()))
        names = " ".join(r.message for r in self.by_id(res, "cred.aws.profile") if r.status == "fail")
        for n in ("'a'", "'b'", "'c'"):
            self.assertIn(n, names)
        self.assertNotIn("'d'", names)

    def test_cli_cache_with_live_keys_vs_expired(self):
        d = os.path.join(self.fake_home.path, ".aws", "cli", "cache")
        os.makedirs(d)
        with open(os.path.join(d, "live.json"), "w") as fh:
            fh.write('{"Credentials": {"AccessKeyId": "x", "SecretAccessKey": "y", "SessionToken": "z", "Expiration": "2099-01-01T00:00:00Z"}}')
        with open(os.path.join(d, "old.json"), "w") as fh:
            fh.write('{"Credentials": {"AccessKeyId": "x", "SecretAccessKey": "y", "SessionToken": "z", "Expiration": "2001-01-01T00:00:00Z"}}')
        res = self.results(self.ctx(self.kit_repo()))
        hits = self.assertFails(res, "cred.aws.sso-cache")
        sev = sorted((("live" if "live.json" in r.message else "old"), r.severity) for r in hits)
        self.assertEqual(sev, [("live", "error"), ("old", "warn")])

    def test_custom_aws_file_locations_via_env_are_followed(self):
        p = os.path.join(self.fake_home.path, "elsewhere", "creds")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as fh:
            fh.write("[x]\naws_access_key_id = %s\naws_secret_access_key = %s\n" % (FAKE_AWS_KEY_ID, FAKE_AWS_SECRET))
        res = self.results(self.ctx(self.kit_repo(), {"AWS_SHARED_CREDENTIALS_FILE": p}))
        self.assertTrue(any("'x'" in r.message for r in self.assertFails(res, "cred.aws.profile")))
        self.assertFails(res, "cred.env.aws")


class EnvironmentVariables(CredCase):
    CASES = [
        ({"AWS_ACCESS_KEY_ID": "AKIAFAKE"}, "cred.env.aws"), ({"AWS_PROFILE": "p"}, "cred.env.aws"),
        ({"AWS_SESSION_TOKEN": "t"}, "cred.env.aws"), ({"AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/t"}, "cred.env.aws"),
        ({"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/x"}, "cred.env.aws"),
        ({"AWS_BEARER_TOKEN_BEDROCK": "x"}, "cred.env.aws"),
        ({"GH_TOKEN": "x"}, "cred.env.github"), ({"GITHUB_TOKEN": "x"}, "cred.env.github"), ({"GH_ENTERPRISE_TOKEN": "x"}, "cred.env.github"),
        ({"GITHUB_PERSONAL_ACCESS_TOKEN": "x"}, "cred.env.github"), ({"HOMEBREW_GITHUB_API_TOKEN": "x"}, "cred.env.github"),
        ({"GOOGLE_APPLICATION_CREDENTIALS": "/x.json"}, "cred.env.cloud"), ({"AZURE_CLIENT_SECRET": "x"}, "cred.env.cloud"),
        ({"ARM_CLIENT_SECRET": "x"}, "cred.env.cloud"), ({"KUBECONFIG": "/x"}, "cred.env.cloud"), ({"DIGITALOCEAN_TOKEN": "x"}, "cred.env.cloud"),
        ({"CLOUDFLARE_API_TOKEN": "x"}, "cred.env.deploy"), ({"VERCEL_TOKEN": "x"}, "cred.env.deploy"), ({"NETLIFY_AUTH_TOKEN": "x"}, "cred.env.deploy"),
        ({"FLY_API_TOKEN": "x"}, "cred.env.deploy"), ({"PULUMI_ACCESS_TOKEN": "x"}, "cred.env.deploy"), ({"TF_TOKEN_app_terraform_io": "x"}, "cred.env.deploy"),
        ({"SUPABASE_SERVICE_ROLE_KEY": "x"}, "cred.env.deploy"),
        ({"DOCKER_HOST": "tcp://10.0.0.1:2375"}, "cred.env.docker"), ({"DOCKER_AUTH_CONFIG": "{}"}, "cred.env.docker"),
        ({"NPM_TOKEN": "x"}, "cred.env.registry"), ({"NODE_AUTH_TOKEN": "x"}, "cred.env.registry"), ({"TWINE_PASSWORD": "x"}, "cred.env.registry"),
        ({"CARGO_REGISTRY_TOKEN": "x"}, "cred.env.registry"), ({"POETRY_PYPI_TOKEN_PYPI": "x"}, "cred.env.registry"),
        ({"npm_config__authToken": "x"}, "cred.env.registry"), ({"UV_PUBLISH_TOKEN": "x"}, "cred.env.registry"),
        ({"SSH_AUTH_SOCK": "/tmp/agent"}, "cred.env.ssh"), ({"SSH_AGENT_PID": "42"}, "cred.env.ssh"), ({"GIT_ASKPASS": "/bin/ask"}, "cred.env.ssh"),
        ({"GIT_SSH_COMMAND": "ssh -i /x/id_ed25519"}, "cred.env.ssh"), ({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "store"}, "cred.env.ssh"),
        ({"GIT_CONFIG_PARAMETERS": "'credential.helper=store'"}, "cred.env.ssh"),
        ({"NETRC": "/x/.netrc"}, "cred.env.netrc"), ({"VAULT_TOKEN": "x"}, "cred.env.vault"),
        ({"DATABASE_URL": "postgres://app:s3cretpw@prod-db.example.invalid:5432/app"}, "cred.env.database"),
        ({"PIP_INDEX_URL": "https://user:pw12345@pypi.example.invalid/simple"}, "cred.env.registry"),
        ({"SOME_RANDOM_VAR": "sk_live_" + "A" * 24}, "cred.env.secret-shaped"),
        ({"MY_SERVICE_API_KEY": "abc"}, "cred.env.generic"),
    ]

    def test_each_variable_is_flagged_by_name_only(self):
        repo = self.kit_repo()
        for env, rid in self.CASES:
            with self.subTest(env=sorted(env)):
                res = self.check_one(self.ctx(repo, env), "cred.env")
                hit = self.assertFails(res, rid)
                text = " ".join(r.message for r in hit)
                self.assertTrue(any(k in text for k in env), text)
                for v in env.values():
                    if len(v) > 8:
                        self.assertNotIn(v, text)

    def test_safe_variables_are_not_flagged(self):
        env = {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1", "AWS_PAGER": "", "AWS_EC2_METADATA_DISABLED": "true", "ANTHROPIC_API_KEY": "sk-ant-x",
               "DATABASE_URL": "sqlite:///./dev.db", "LOCAL_DB_URL": "postgres://app:pw@localhost:5432/app", "PATH_LIKE": "a:b"}
        res = self.check_one(self.ctx(self.kit_repo(), env), "cred.env")
        env_hits = [r for r in res if r.id.startswith("cred.env") and r.status == "fail"]
        self.assertEqual(env_hits, [], env_hits)

    def test_empty_values_are_ignored(self):
        res = self.check_one(self.ctx(self.kit_repo(), {"GH_TOKEN": "", "AWS_PROFILE": ""}), "cred.env")
        self.assertClean(res, "cred.env")


class GitHubAndGit(CredCase):
    def test_gh_hosts_file_is_a_hit_and_names_the_user(self):
        self.fake_home.plant_gh_hosts()
        res = self.results(self.ctx(self.kit_repo()))
        hit = self.assertFails(res, "cred.gh.hosts")
        self.assertIn("fake-user", hit[0].message)

    def test_gh_hosts_logged_in_only_as_the_bot_is_fine(self):
        self.fake_home.plant_gh_hosts()
        repo = self.kit_repo({"bot": {"kind": "github-app", "login": "fake-user"}})
        res = self.results(self.ctx(repo))
        self.assertClean(res, "cred.gh.hosts")

    def test_gh_hosts_other_user_is_a_hit_even_when_a_bot_is_configured(self):
        self.fake_home.plant_gh_hosts()
        repo = self.kit_repo({"bot": {"kind": "github-app", "login": "acme-bot[bot]"}})
        self.assertFails(self.results(self.ctx(repo)), "cred.gh.hosts")

    def test_gh_hosts_keychain_login_has_no_token_in_the_file(self):
        p = os.path.join(self.fake_home.path, ".config", "gh", "hosts.yml")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as fh:
            fh.write("github.com:\n    git_protocol: https\n    user: some-human\n")
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.gh.hosts")
        self.assertIn("keychain", hit[0].message)

    def test_gh_config_dir_env_is_followed(self):
        d = os.path.join(self.fake_home.path, "alt-gh")
        os.makedirs(d)
        with open(os.path.join(d, "hosts.yml"), "w") as fh:
            fh.write("github.com:\n    user: x-user\n    oauth_token: %s\n" % FAKE_GH_TOKEN)
        self.assertFails(self.results(self.ctx(self.kit_repo(), {"GH_CONFIG_DIR": d})), "cred.gh.hosts")

    def test_git_credentials_store_file(self):
        self.fake_home.plant_git_credentials()
        self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.git.credentials-file")

    def test_xdg_git_credentials_store(self):
        p = os.path.join(self.fake_home.path, ".config", "git", "credentials")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as fh:
            fh.write("https://u:p@github.com\n")
        self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.git.credentials-file")

    def test_global_credential_helper(self):
        self.fake_home.plant_git_credential_helper()
        res = self.results(self.ctx(self.kit_repo(), {"GIT_CONFIG_GLOBAL": self.fake_home.planted["gitconfig"]}))
        hit = self.assertFails(res, "cred.git.credential-helper")
        self.assertIn("global", hit[0].message)

    def test_url_specific_helper_and_local_scope(self):
        repo = self.kit_repo()
        repo.git("config", "credential.https://github.com.helper", "!gh auth git-credential")
        res = self.results(self.ctx(repo))
        hit = self.assertFails(res, "cred.git.credential-helper")
        self.assertIn("local", hit[0].message)

    def test_empty_helper_reset_is_not_a_hit(self):
        repo = self.kit_repo()
        repo.git("config", "credential.helper", "")
        self.assertClean(self.results(self.ctx(repo)), "cred.git.config")

    def test_extraheader_left_by_actions_checkout(self):
        repo = self.kit_repo()
        repo.git("config", "http.https://github.com/.extraheader", "AUTHORIZATION: basic ZmFrZTpmYWtl")
        hit = self.assertFails(self.results(self.ctx(repo)), "cred.git.extraheader")
        self.assertIn("actions/checkout", hit[0].message)

    def test_remote_url_with_embedded_token_and_insteadof(self):
        repo = self.kit_repo(origin="https://fake-user:%s@github.com/acme/widgets.git" % FAKE_GH_TOKEN)
        res = self.results(self.ctx(repo))
        hit = self.assertFails(res, "cred.git.url-credential")
        self.assertNotIn(FAKE_GH_TOKEN, hit[0].message)
        repo2 = self.kit_repo()
        repo2.git("config", "url.https://tok123456789012345678901234567890@github.com/.insteadOf", "https://github.com/")
        self.assertFails(self.results(self.ctx(repo2)), "cred.git.url-credential")

    def test_ssh_remote_with_git_user_is_not_a_credential(self):
        repo = self.kit_repo(origin="ssh://git@github.com/acme/widgets.git")
        self.assertClean(self.results(self.ctx(repo)), "cred.git.config")

    def test_core_sshcommand_with_identity_file(self):
        repo = self.kit_repo()
        repo.git("config", "core.sshCommand", "ssh -i /home/x/.ssh/id_deploy -o IdentitiesOnly=yes")
        self.assertFails(self.results(self.ctx(repo)), "cred.git.ssh-command")

    def test_netrc_with_password(self):
        p = os.path.join(self.fake_home.path, ".netrc")
        with open(p, "w") as fh:
            fh.write("machine github.com login x password %s\nmachine example.invalid login y\n" % FAKE_GH_TOKEN)
        hits = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.netrc")
        self.assertEqual(len(hits), 2)
        self.assertNotIn(FAKE_GH_TOKEN, " ".join(h.message for h in hits))


class SshAndSockets(CredCase):
    def test_ssh_auth_sock_set_even_if_the_socket_is_missing(self):
        res = self.results(self.ctx(self.kit_repo(), {"SSH_AUTH_SOCK": "/nonexistent/agent.sock"}))
        hit = self.assertFails(res, "cred.ssh.agent")
        self.assertIn("SSH_AUTH_SOCK is set", hit[0].message)

    def test_real_agent_socket_in_home(self):
        cwd = os.getcwd()
        d = os.path.join(self.fake_home.path, ".1password")
        os.makedirs(d)
        os.chdir(d)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind("agent.sock")
            s.close()
        finally:
            os.chdir(cwd)
        res = self.results(self.ctx(self.kit_repo()))
        hit = self.assertFails(res, "cred.ssh.agent")
        self.assertIn("agent socket exists", hit[0].message)

    def test_unencrypted_private_key_is_an_error_and_encrypted_a_warning(self):
        self.fake_home.plant_ssh_key()
        p = os.path.join(self.fake_home.path, ".ssh", "id_enc")
        with open(p, "w") as fh:
            fh.write("-----BEGIN ENCRYPTED " + "PRIVATE KEY-----\nxx\n-----END ENCRYPTED " + "PRIVATE KEY-----\n")
        hits = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.ssh.keys")
        sev = {("id_fake" if "id_fake" in h.message else "id_enc"): h.severity for h in hits}
        self.assertEqual(sev, {"id_fake": "error", "id_enc": "warn"})

    def test_openssh_format_key_encrypted_vs_not(self):
        import base64
        magic = b"openssh-key-v1\x00"

        def blob(cipher):
            return magic + len(cipher).to_bytes(4, "big") + cipher + b"\x00" * 40
        d = os.path.join(self.fake_home.path, ".ssh")
        os.makedirs(d, exist_ok=True)
        for name, cipher in (("id_plain", b"none"), ("id_prot", b"aes256-ctr")):
            body = base64.b64encode(blob(cipher)).decode()
            with open(os.path.join(d, name), "w") as fh:
                fh.write("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + body + "\n-----END OPENSSH " + "PRIVATE KEY-----\n")
        hits = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.ssh.keys")
        sev = {("id_plain" if "id_plain" in h.message else "id_prot"): h.severity for h in hits}
        self.assertEqual(sev, {"id_plain": "error", "id_prot": "warn"})

    def test_public_keys_and_known_hosts_are_ignored(self):
        d = os.path.join(self.fake_home.path, ".ssh")
        os.makedirs(d)
        for n, t in (("id_ed25519.pub", "ssh-ed25519 AAAA x"), ("known_hosts", "github.com ssh-ed25519 AAAA"), ("config", "Host *\n")):
            with open(os.path.join(d, n), "w") as fh:
                fh.write(t)
        self.assertClean(self.results(self.ctx(self.kit_repo())), "cred.ssh")

    def test_identity_agent_in_ssh_config(self):
        d = os.path.join(self.fake_home.path, ".ssh")
        os.makedirs(d)
        with open(os.path.join(d, "config"), "w") as fh:
            fh.write("Host *\n  IdentityAgent ~/Library/agent.sock\n")
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.ssh.agent")
        self.assertEqual(hit[0].severity, "warn")

    def test_docker_socket_under_fs_root(self):
        d = os.path.join(self.fs_root, "var", "run")
        os.makedirs(d)
        cwd = os.getcwd()
        os.chdir(d)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind("docker.sock")
            s.close()
        finally:
            os.chdir(cwd)
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.docker.socket")
        self.assertIn("docker.sock", hit[0].message)

    def test_docker_socket_in_home_and_docker_host_env(self):
        d = os.path.join(self.fake_home.path, ".colima", "default")
        os.makedirs(d)
        cwd = os.getcwd()
        os.chdir(d)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind("docker.sock")
            s.close()
        finally:
            os.chdir(cwd)
        self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.docker.socket")
        self.assertFails(self.results(self.ctx(self.kit_repo(), {"DOCKER_HOST": "unix://" + os.path.join(d, "docker.sock")})), "cred.docker.socket")

    def test_docker_config_auths_credsstore_and_helpers(self):
        self.fake_home.plant_docker_config()
        self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.docker.config")
        p = os.path.join(self.fake_home.path, ".docker", "config.json")
        with open(p, "w") as fh:
            fh.write('{"credsStore": "osxkeychain"}')
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.docker.config")
        self.assertIn("osxkeychain", hit[0].message)


class CloudsAndRegistries(CredCase):
    def test_kube_config_contexts(self):
        self.fake_home.plant_kube_config()
        hit = self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.kube")
        self.assertIn("fake", hit[0].message)

    def test_kubeconfig_env_pointing_elsewhere(self):
        p = os.path.join(self.fake_home.path, "kc")
        with open(p, "w") as fh:
            fh.write("contexts:\n- context:\n    cluster: c\n    user: u\n  name: prod-admin\n")
        hit = self.assertFails(self.results(self.ctx(self.kit_repo(), {"KUBECONFIG": p})), "cred.kube")
        self.assertIn("prod-admin", hit[0].message)

    def test_in_cluster_service_account_token(self):
        d = os.path.join(self.fs_root, "var", "run", "secrets", "kubernetes.io", "serviceaccount")
        os.makedirs(d)
        with open(os.path.join(d, "token"), "w") as fh:
            fh.write("x")
        self.assertFails(self.results(self.ctx(self.kit_repo())), "cred.kube")

    def test_gcloud_and_azure(self):
        self.fake_home.plant_gcloud()
        self.fake_home.plant_azure()
        res = self.results(self.ctx(self.kit_repo()))
        ids = {r.id for r in res if r.status == "fail"}
        self.assertIn("cred.files.gcloud", ids)
        self.assertIn("cred.files.azure", ids)

    def test_cloudsdk_config_and_azure_config_dir_env(self):
        d = os.path.join(self.fake_home.path, "elsewhere")
        os.makedirs(d)
        with open(os.path.join(d, "credentials.db"), "w") as fh:
            fh.write("x")
        res = self.results(self.ctx(self.kit_repo(), {"CLOUDSDK_CONFIG": d}))
        self.assertTrue(any(r.id == "cred.files.gcloud" for r in res if r.status == "fail"))

    def test_package_registry_and_deploy_host_files(self):
        h = self.fake_home.path
        files = {".npmrc": "//registry.npmjs.org/:_authToken=npm_" + "A" * 36 + "\n",
                 ".pypirc": "[pypi]\nusername = __token__\npassword = pypi-AgEIcHlwaS5vcmc\n",
                 ".cargo/credentials.toml": '[registry]\ntoken = "cio_x"\n',
                 ".terraform.d/credentials.tfrc.json": '{"credentials": {"app.terraform.io": {"token": "x"}}}',
                 ".vault-token": "hvs.xxxx",
                 ".config/doctl/config.yaml": "access-token: dop_v1_x\n",
                 ".fly/config.yml": "access_token: fo1_x\n",
                 ".config/pip/pip.conf": "[global]\nindex-url = https://u:pw1234@pypi.example.invalid/simple\n",
                 ".m2/settings.xml": "<settings><servers><server><password>realpw</password></server></servers></settings>",
                 ".gradle/gradle.properties": "ossrhPassword=hunter2\n",
                 ".composer/auth.json": '{"github-oauth": {"github.com": "x"}, "http-basic": {"p": {"password": "x"}}}'}
        for rel, text in files.items():
            p = os.path.join(h, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(text)
        res = self.results(self.ctx(self.kit_repo()))
        found = {r.id.split(".", 2)[2] for r in res if r.id.startswith("cred.files.") and r.status == "fail"}
        self.assertTrue({"npm", "pypi", "cargo", "terraform", "vault", "doctl", "fly", "pip", "maven", "gradle", "composer"} <= found, found)

    def test_benign_versions_of_those_files_are_fine(self):
        h = self.fake_home.path
        files = {".npmrc": "registry=https://registry.npmjs.org/\n//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n",
                 ".config/pip/pip.conf": "[global]\nindex-url = https://pypi.org/simple\n",
                 ".m2/settings.xml": "<settings><servers><server><password>${env.PW}</password></server></servers></settings>",
                 ".gradle/gradle.properties": "org.gradle.daemon=true\n"}
        for rel, text in files.items():
            p = os.path.join(h, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(text)
        self.assertClean(self.results(self.ctx(self.kit_repo())), "cred.files")


class WorkingTree(CredCase):
    def test_untracked_production_env_and_keys_in_the_repo(self):
        repo = self.kit_repo()
        repo.write(".env.production", "DB=x\n")
        repo.write("deploy/server.pem", "-----BEGIN RSA " + "PRIVATE KEY-----\nxx\n-----END RSA " + "PRIVATE KEY-----\n")
        repo.write("infra/terraform.tfstate", "{}")
        repo.write("cert/public.pem", "-----BEGIN CERTIFICATE-----\nxx\n-----END CERTIFICATE-----\n")
        res = self.results(self.ctx(repo))
        ids = sorted(r.id for r in res if r.id.startswith("cred.tree") and r.status == "fail")
        self.assertEqual(ids, ["cred.tree.env-production", "cred.tree.private-key", "cred.tree.tfstate"])

    def test_node_modules_and_worktrees_are_not_scanned(self):
        repo = self.kit_repo()
        repo.write("node_modules/x/.env.production", "x")
        repo.write(".claude/worktrees/a/.env.production", "x")
        self.assertClean(self.results(self.ctx(repo)), "cred.tree")


class MetadataEndpoints(CredCase):
    def test_not_probed_by_default_is_unknown_not_a_pass(self):
        res = self.results(self.ctx(self.kit_repo()))
        r = self.by_id(res, "cred.net.metadata")[0]
        self.assertEqual(r.status, "unknown")

    def test_reachable_endpoint_is_an_error(self):
        probes = []
        res = self.results(self.ctx(self.kit_repo(), reach=lambda h, p, t=1.0: probes.append(h) or h == "169.254.169.254"))
        self.assertEqual(probes, ["169.254.169.254", "169.254.170.2", "169.254.170.23"])
        hit = self.assertFails(res, "cred.net.metadata")
        self.assertIn("169.254.169.254", hit[0].message)

    def test_unreachable_passes(self):
        res = self.results(self.ctx(self.kit_repo(), reach=lambda h, p, t=1.0: False))
        self.assertEqual(self.by_id(res, "cred.net.metadata")[0].status, "pass")

    def test_sandbox_network_deny_counts_when_not_probed(self):
        repo = self.kit_repo(files={".claude/settings.json": '{"sandbox": {"network": {"deniedCidrs": ["169.254.0.0/16"]}}}'})
        self.assertEqual(self.by_id(self.results(self.ctx(repo)), "cred.net.metadata")[0].status, "pass")

    def test_ecs_task_role_env(self):
        res = self.results(self.ctx(self.kit_repo(), {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/x"}, reach=lambda h, p, t=1.0: False))
        self.assertFails(res, "cred.net.ecs-env")


class LiveChecks(CredCase):
    """The live checks run only through an injected runner. Nothing here calls aws, gh, git credential or ssh for real."""

    def runner(self, table):
        calls = []

        def run(argv, timeout=10.0, env=None, cwd=None, input_text=None):
            calls.append(list(argv))
            if argv[0] == "git" and argv[1:3] == ["config", "--list"]:
                import subprocess
                p = subprocess.run(argv, env=dict(env), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return p.returncode, p.stdout.decode(), p.stderr.decode()
            for key, val in table.items():
                if argv[:len(key)] == list(key):
                    return val
            return 127, "", "not found: %s" % argv[0]
        run.calls = calls
        return run

    def test_skipped_without_live(self):
        res = self.results(self.ctx(self.kit_repo()))
        for rid in ("cred.live.sts", "cred.live.gh", "cred.live.git-fill", "cred.live.ssh"):
            self.assertEqual(self.by_id(res, rid)[0].status, "skipped")

    def test_sts_per_profile(self):
        self.fake_home.plant_aws_credentials("deploy")
        run = self.runner({("aws", "sts", "get-caller-identity"): (0, '{"Arn": "arn:aws:iam::123456789012:user/x"}', "")})
        res = self.results(self.ctx(self.kit_repo(), live=True, run=run))
        hit = self.assertFails(res, "cred.live.sts")
        self.assertIn("arn:aws:iam::123456789012:user/x", hit[0].message)
        self.assertIn(["aws", "sts", "get-caller-identity", "--output", "json", "--profile", "deploy"], run.calls)

    def test_gh_auth_status_other_account_and_bot(self):
        run = self.runner({("gh", "auth", "status"): (0, "github.com\n  Logged in to github.com account some-human (keyring)\n", "")})
        self.assertFails(self.results(self.ctx(self.kit_repo(), live=True, run=run)), "cred.live.gh")
        repo = self.kit_repo({"bot": {"kind": "github-app", "login": "some-human"}})
        self.assertClean(self.results(self.ctx(repo, live=True, run=run)), "cred.live.gh")

    def test_git_credential_fill_returns_a_password(self):
        run = self.runner({("git", "credential", "fill"): (0, "protocol=https\nhost=github.com\nusername=some-human\npassword=hunter2hunter2\n", "")})
        hit = self.assertFails(self.results(self.ctx(self.kit_repo(), live=True, run=run)), "cred.live.git-fill")
        self.assertNotIn("hunter2", hit[0].message)

    def test_ssh_authenticates_as_a_human(self):
        run = self.runner({("ssh", "-T"): (1, "", "Hi some-human! You've successfully authenticated, but GitHub does not provide shell access.")})
        self.assertFails(self.results(self.ctx(self.kit_repo(), live=True, run=run)), "cred.live.ssh")
        run2 = self.runner({("ssh", "-T"): (255, "", "git@github.com: Permission denied (publickey).")})
        self.assertClean(self.results(self.ctx(self.kit_repo(), live=True, run=run2)), "cred.live.ssh")


if __name__ == "__main__":
    unittest.main()
