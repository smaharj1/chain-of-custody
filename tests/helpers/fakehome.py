"""A fake HOME that can plant fake credentials (IMP-14, report §8.1 L2, RC-5).

Doctor and guard tests run with HOME pointing at one of these and a scrubbed environment,
so they never read the real home. Every planted value is fake and is assembled at runtime
(no secret-shaped literal sits in the repo). `plant_all()` plants every kind the doctor
looks for; the individual `plant_*` methods plant one so a test can assert on a single
check id.
"""
from __future__ import annotations

import os
import socket
from typing import Dict, List, Mapping, Optional

__all__ = ["FakeHome", "clean_env", "FAKE_AWS_KEY_ID", "FAKE_AWS_SECRET", "FAKE_GH_TOKEN",
           "CREDENTIAL_ENV_PREFIXES", "CREDENTIAL_ENV_NAMES"]

# Fake, but shaped like the real thing so redaction and scanners are exercised.
FAKE_AWS_KEY_ID = "AKIA" + "FAKE" * 4                # 20 chars
FAKE_AWS_SECRET = "FAKESECRET" * 4                   # 40 chars
FAKE_GH_TOKEN = "gh" + "p_" + "F" * 36
FAKE_SESSION_TOKEN = "FAKESESSIONTOKEN" * 3

CREDENTIAL_ENV_PREFIXES = ("AWS_", "GH_", "GITHUB_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_", "AZURE_",
                           "ARM_", "KUBE", "DOCKER_", "TF_VAR_", "NPM_", "ANTHROPIC_", "OPENAI_")
CREDENTIAL_ENV_NAMES = ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "CLAUDECODE", "GIT_ASKPASS", "SSH_ASKPASS",
                        "NODE_AUTH_TOKEN", "CI")


def clean_env(home: Optional[str] = None, extra: Optional[Mapping[str, str]] = None,
              keep_path: bool = True) -> Dict[str, str]:
    """os.environ minus every credential-ish variable, with HOME and git config pinned (IMP-14)."""
    env = {}  # type: Dict[str, str]
    for k, v in os.environ.items():
        if k.startswith(CREDENTIAL_ENV_PREFIXES) or k in CREDENTIAL_ENV_NAMES or k.startswith("GIT_"):
            continue
        env[k] = v
    if not keep_path:
        env["PATH"] = "/usr/bin:/bin"
    if home:
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


class FakeHome(object):
    def __init__(self, path: str) -> None:
        self.path = os.path.realpath(path)
        os.makedirs(self.path, exist_ok=True)
        self.planted = {}  # type: Dict[str, str]
        self._env_extra = {}  # type: Dict[str, str]

    def _write(self, rel: str, text: str, mode: int = 0o600) -> str:
        p = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(p, mode)
        return p

    # -- AWS ---------------------------------------------------------------------
    def plant_aws_config(self, profile: str = "default", sso: bool = False) -> str:
        if sso:
            text = ("[profile %s]\nsso_session = fake\nsso_account_id = 000000000000\n"
                    "sso_role_name = FakeAdmin\nregion = us-east-1\n\n[sso-session fake]\n"
                    "sso_start_url = https://fake.invalid/start\nsso_region = us-east-1\n" % profile)
        else:
            text = "[%s]\nregion = us-east-1\n" % ("default" if profile == "default" else "profile " + profile)
        p = self._write(".aws/config", text)
        self.planted["aws_config"] = p
        return p

    def plant_aws_credentials(self, profile: str = "default") -> str:
        text = ("[%s]\naws_access_key_id = %s\naws_secret_access_key = %s\n"
                % (profile, FAKE_AWS_KEY_ID, FAKE_AWS_SECRET))
        p = self._write(".aws/credentials", text)
        self.planted["aws_credentials"] = p
        return p

    def plant_sso_cache(self) -> str:
        p = self._write(".aws/sso/cache/fake0000.json",
                        '{"accessToken": "%s", "expiresAt": "2099-01-01T00:00:00Z", '
                        '"startUrl": "https://fake.invalid/start"}\n' % FAKE_SESSION_TOKEN)
        self.planted["aws_sso_cache"] = p
        return p

    # -- GitHub / git / ssh -----------------------------------------------------------
    def plant_gh_hosts(self) -> str:
        p = self._write(".config/gh/hosts.yml",
                        "github.com:\n    user: fake-user\n    oauth_token: %s\n    git_protocol: https\n" % FAKE_GH_TOKEN)
        self.planted["gh_hosts"] = p
        return p

    def plant_git_credentials(self) -> str:
        p = self._write(".git-credentials", "https://fake-user:%s@github.com\n" % FAKE_GH_TOKEN)
        self.planted["git_credentials"] = p
        return p

    def plant_git_credential_helper(self) -> str:
        """A ~/.gitconfig naming a credential helper. Tests set GIT_CONFIG_GLOBAL to it explicitly."""
        p = self._write(".gitconfig", "[credential]\n\thelper = store\n")
        self.planted["gitconfig"] = p
        return p

    def plant_ssh_agent_socket(self, real: bool = False) -> str:
        """A placeholder for the ssh-agent socket and SSH_AUTH_SOCK pointing at it.

        Default is a plain file (the doctor only checks the variable and the path). With
        real=True it binds a unix socket when the path is short enough (macOS limit ~104)."""
        p = os.path.join(self.path, ".ssh", "agent.sock")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        made = False
        if real and len(p) < 100:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.bind(p)
                made = True
            except OSError:
                pass
            finally:
                s.close()
        if not made:
            with open(p, "w") as fh:
                fh.write("")
        self.planted["ssh_agent_socket"] = p
        self._env_extra["SSH_AUTH_SOCK"] = p
        return p

    def plant_ssh_key(self) -> str:
        p = self._write(".ssh/id_fake", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nFAKEFAKEFAKE\n-----END OPENSSH " + "PRIVATE KEY-----\n")
        self.planted["ssh_key"] = p
        return p

    # -- other clouds -------------------------------------------------------------------
    def plant_kube_config(self) -> str:
        p = self._write(".kube/config",
                        "apiVersion: v1\nkind: Config\nclusters:\n- name: fake\n  cluster:\n    server: https://fake.invalid\n"
                        "users:\n- name: fake\n  user:\n    token: %s\ncontexts:\n- name: fake\n  context:\n    cluster: fake\n    user: fake\n"
                        "current-context: fake\n" % FAKE_SESSION_TOKEN)
        self.planted["kube_config"] = p
        return p

    def plant_gcloud(self) -> str:
        p = self._write(".config/gcloud/application_default_credentials.json",
                        '{"type": "authorized_user", "client_id": "fake", "client_secret": "fake", "refresh_token": "%s"}\n'
                        % FAKE_SESSION_TOKEN)
        self.planted["gcloud_adc"] = p
        return p

    def plant_azure(self) -> str:
        p = self._write(".azure/msal_token_cache.json", '{"AccessToken": {"fake": {"secret": "%s"}}}\n' % FAKE_SESSION_TOKEN)
        self.planted["azure_token_cache"] = p
        return p

    def plant_docker_config(self) -> str:
        p = self._write(".docker/config.json", '{"auths": {"registry.invalid": {"auth": "ZmFrZTpmYWtl"}}}\n')
        self.planted["docker_config"] = p
        return p

    def plant_all(self) -> List[str]:
        self.plant_aws_config(sso=True)
        self.plant_aws_credentials()
        self.plant_sso_cache()
        self.plant_gh_hosts()
        self.plant_git_credentials()
        self.plant_git_credential_helper()
        self.plant_ssh_agent_socket()
        self.plant_ssh_key()
        self.plant_kube_config()
        self.plant_gcloud()
        self.plant_azure()
        self.plant_docker_config()
        return sorted(self.planted.values())

    # -- environment -----------------------------------------------------------------------
    def env(self, extra: Optional[Mapping[str, str]] = None, include_planted: bool = True) -> Dict[str, str]:
        """A scrubbed environment with HOME at this fake home. `include_planted` adds the
        variables that planting implies (SSH_AUTH_SOCK)."""
        merged = dict(self._env_extra) if include_planted else {}
        if extra:
            merged.update(extra)
        env = clean_env(self.path, merged)
        if "GIT_CONFIG_GLOBAL" in merged:
            env["GIT_CONFIG_GLOBAL"] = merged["GIT_CONFIG_GLOBAL"]
        return env
