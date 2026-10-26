"""Settings, sandbox, MCP, managed settings and devcontainer checks (RC-17, CS-10, B3; report §5.6, §7.2)."""
from __future__ import annotations

import json
import os
import unittest

from doctorlib import levelcalc, settings

from .helpers import BoundaryCase, run_doctor

SANDBOX_OK = {"sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "excludedCommands": []}}


class SettingsCase(BoundaryCase):
    GROUPS = {"settings", "sandbox", "mcp"}

    def with_settings(self, data, name=".claude/settings.json", **kw):
        repo = self.kit_repo(files={name: json.dumps(data)}, **kw)
        ctx = self.ctx(repo)
        return ctx, self.results(ctx)


class WorkflowHeadlessly(SettingsCase):
    """Report §15 item 9 'Workflow headlessly (RC-17)': a driver child with a Workflow allow is refused before launch."""

    def test_project_settings_allow(self):
        for rule in ("Workflow", "Workflow(deploy-review)", "Workflow(*)", " Workflow ", "*"):
            with self.subTest(rule=rule):
                ctx, res = self.with_settings({"permissions": {"allow": ["Read", rule]}})
                hit = self.assertFails(res, "settings.workflow-allow")
                self.assertEqual(hit[0].severity, "error")

    def test_top_level_allowedtools_key(self):
        ctx, res = self.with_settings({"allowedTools": ["Bash(npm test)", "Workflow"]})
        self.assertFails(res, "settings.workflow-allow")

    def test_local_settings_file(self):
        ctx, res = self.with_settings({"permissions": {"allow": ["Workflow"]}}, name=".claude/settings.local.json")
        self.assertFails(res, "settings.workflow-allow")

    def test_a_driver_childs_settings_file_passed_with_flag(self):
        repo = self.kit_repo()
        child = repo.write("child-settings.json", json.dumps({"permissions": {"defaultMode": "dontAsk", "allow": ["Read", "Workflow"]}}))
        ctx = self.ctx(repo, extra_settings=[child])
        hit = self.assertFails(self.results(ctx), "settings.workflow-allow")
        self.assertIn("child-settings.json", hit[0].message)
        self.assertIn("cli", hit[0].message)

    def test_the_human_run_orchestrate_profile_is_the_only_exemption(self):
        repo = self.kit_repo()
        prof = repo.write("orchestrate-profile.json", json.dumps({"permissions": {"allow": ["Workflow"]}}))
        ctx = self.ctx(repo, extra_settings=[prof], workflow_exempt=[prof])
        self.assertClean(self.results(ctx), "settings.workflow-allow")
        repo.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["Workflow"]}}))
        ctx = self.ctx(repo, extra_settings=[prof], workflow_exempt=[prof])
        self.assertFails(self.results(ctx), "settings.workflow-allow")

    def test_deny_workflow_is_fine_and_so_is_nothing(self):
        ctx, res = self.with_settings({"permissions": {"deny": ["Workflow"], "allow": ["Read"]}})
        self.assertClean(res, "settings.workflow-allow")

    def test_agent_definition_granting_workflow(self):
        repo = self.kit_repo(files={".claude/agents/e.md": "---\nname: e\ntools: Read, Edit, Workflow, Bash\n---\nbody\n"})
        self.assertFails(self.results(self.ctx(repo)), "settings.agent-workflow")
        repo2 = self.kit_repo(files={".claude/agents/e.md": "---\nname: e\ntools: Read, Edit\ndisallowedTools: Workflow\n---\nbody\n"})
        self.assertClean(self.results(self.ctx(repo2)), "settings.agent-workflow")

    def test_cli_exits_1_for_a_child_settings_file(self):
        repo = self.kit_repo()
        child = repo.write("child.json", json.dumps({"permissions": {"allow": ["Workflow"]}}))
        rc, out, err = run_doctor(["--settings", child, "--json", "--repo", repo.path, "--home", self.fake_home.path, "--claude-version", "2.1.305"], self.fake_home.env())
        self.assertEqual(rc, 1)
        ids = [c["id"] for c in json.loads(out)["checks"] if c["status"] == "fail" and c["severity"] == "error"]
        self.assertIn("settings.workflow-allow", ids)


class ParseAndModes(SettingsCase):
    def test_invalid_json_is_reported(self):
        repo = self.kit_repo(files={".claude/settings.json": "{not json"})
        hit = self.assertFails(self.results(self.ctx(repo)), "settings.parse")
        self.assertIn("not valid JSON", hit[0].message)

    def test_bypass_permissions_default(self):
        ctx, res = self.with_settings({"permissions": {"defaultMode": "bypassPermissions"}})
        self.assertFails(res, "settings.bypass-mode")
        ctx, res = self.with_settings({"skipDangerousModePermissionPrompt": True})
        self.assertFails(res, "settings.bypass-mode")

    def test_web_tools_denied_for_the_conductor(self):
        ctx, res = self.with_settings({"permissions": {"deny": ["WebFetch", "WebSearch"]}})
        self.assertClean(res, "settings.web-tools")
        ctx, res = self.with_settings({"permissions": {"deny": ["WebSearch"]}})
        self.assertIn("WebFetch", self.assertFails(res, "settings.web-tools")[0].message)

    def test_jsonc_comments_and_trailing_commas(self):
        text = '{\n  // c\n  "a": "x // not a comment", /* b */ "b": [1, 2,],\n}'
        self.assertEqual(json.loads(settings.strip_jsonc(text)), {"a": "x // not a comment", "b": [1, 2]})


class Sandbox(SettingsCase):
    def test_full_config_with_attestation_is_not_a_cap(self):
        repo = self.kit_repo(config={"sandbox_verified": "2026-10-26"}, files={".claude/settings.json": json.dumps(SANDBOX_OK)})
        ctx = self.ctx(repo)
        cap = levelcalc.cap_sandbox(ctx, levelcalc.R(self.results(ctx)))
        self.assertEqual(cap.level, 5)

    def test_config_without_attestation_is_l1(self):
        ctx, res = self.with_settings(SANDBOX_OK)
        cap = levelcalc.cap_sandbox(ctx, levelcalc.R(res))
        self.assertEqual(cap.level, 1)
        self.assertEqual(self.by_id(res, "sandbox.attestation")[0].status, "unknown")

    def test_each_setting_is_checked_separately(self):
        base = json.loads(json.dumps(SANDBOX_OK))
        for key, bad in (("enabled", False), ("failIfUnavailable", False), ("allowUnsandboxedCommands", True)):
            with self.subTest(key=key):
                d = json.loads(json.dumps(base))
                d["sandbox"][key] = bad
                ctx, res = self.with_settings(d)
                self.assertFails(res, "sandbox.%s" % key)

    def test_unset_is_not_assumed_safe(self):
        ctx, res = self.with_settings({})
        for key in ("enabled", "failIfUnavailable", "allowUnsandboxedCommands"):
            self.assertIn("not set", self.assertFails(res, "sandbox.%s" % key)[0].message)

    def test_docker_and_podman_may_not_be_excluded(self):
        d = json.loads(json.dumps(SANDBOX_OK))
        d["sandbox"]["excludedCommands"] = ["docker", "podman compose", "git"]
        ctx, res = self.with_settings(d)
        self.assertIn("docker", self.assertFails(res, "sandbox.excludedCommands")[0].message)

    def test_a_looser_project_file_cannot_hide_behind_a_stricter_user_file(self):
        """Precedence: local beats project beats user. The effective value is what counts."""
        repo = self.kit_repo(files={".claude/settings.json": json.dumps(SANDBOX_OK), ".claude/settings.local.json": json.dumps({"sandbox": {"enabled": False}})})
        self.assertFails(self.results(self.ctx(repo)), "sandbox.enabled")

    def test_managed_settings_win_over_everything(self):
        os.makedirs(os.path.join(self.fs_root, "etc", "claude-code"))
        with open(os.path.join(self.fs_root, "etc", "claude-code", "managed-settings.json"), "w") as fh:
            json.dump(SANDBOX_OK, fh)
        repo = self.kit_repo(files={".claude/settings.local.json": json.dumps({"sandbox": {"enabled": False}})})
        self.assertClean(self.results(self.ctx(repo)), "sandbox.enabled")

    def test_agent_isolation_attestation_satisfies_the_sandbox_cap(self):
        repo = self.kit_repo(config={"agent_isolation": "devcontainer"})
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_sandbox(ctx, levelcalc.R(self.results(ctx))).level, 5)

    def test_deny_paths_cover_the_credential_dirs_and_git_dirs(self):
        import os as _os
        with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", ".claude", "templates", "security", "sandbox.settings.json")) as fh:
            tmpl = json.load(fh)
        ctx, res = self.with_settings(tmpl)
        r = self.by_id(res, "sandbox.deny-paths")[0]
        self.assertEqual(r.status, "pass", r.message)
        ctx, res = self.with_settings({"sandbox": {"filesystem": {"denyRead": ["~/.aws"], "denyWrite": []}}})
        self.assertEqual(self.by_id(res, "sandbox.deny-paths")[0].status, "fail")


class Mcp(SettingsCase):
    def test_enable_all_project_servers(self):
        ctx, res = self.with_settings({"enableAllProjectMcpServers": True})
        self.assertFails(res, "mcp.enable-all-project-servers")
        ctx, res = self.with_settings({"enableAllProjectMcpServers": False})
        self.assertClean(res, "mcp.enable-all-project-servers")

    def test_deploy_capable_server_in_mcp_json_is_an_error(self):
        servers = {"mcpServers": {"aws-cdk": {"command": "uvx", "args": ["awslabs.cdk-mcp-server"]}, "vercel": {"type": "http", "url": "https://mcp.vercel.com"},
                                  "k8s": {"command": "npx", "args": ["mcp-server-kubernetes"]}, "gh": {"command": "github-mcp-server"}}}
        repo = self.kit_repo(files={".mcp.json": json.dumps(servers)})
        res = self.results(self.ctx(repo))
        for name in ("aws-cdk", "vercel", "k8s", "gh"):
            self.assertEqual(self.assertFails(res, "mcp.server.%s" % name)[0].severity, "error", name)

    def test_install_b_deploy_on_aws_plugin_enabled(self):
        ctx, res = self.with_settings({"enabledPlugins": {"deploy-on-aws@claude-plugins": True, "pr-review@x": True, "off-one-aws@y": False}})
        self.assertFails(res, "mcp.plugin.deploy-on-aws")
        self.assertEqual([r for r in res if r.id.startswith("mcp.plugin.") and r.status == "fail" and "off-one" in r.id], [])

    def test_user_scope_servers_are_seen(self):
        with open(os.path.join(self.fake_home.path, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"cloudflare": {"url": "https://mcp.cloudflare.com/sse"}}}, fh)
        res = self.results(self.ctx(self.kit_repo()))
        self.assertIn("user", self.assertFails(res, "mcp.server.cloudflare")[0].message)

    def test_local_scope_servers_for_this_project(self):
        repo = self.kit_repo()
        with open(os.path.join(self.fake_home.path, ".claude.json"), "w") as fh:
            json.dump({"projects": {repo.path: {"mcpServers": {"terraform": {"command": "terraform-mcp-server"}}}}}, fh)
        self.assertFails(self.results(self.ctx(repo)), "mcp.server.terraform")

    def test_database_and_saas_servers_warn(self):
        repo = self.kit_repo(files={".mcp.json": json.dumps({"mcpServers": {"postgres": {"command": "mcp-postgres"}, "slack": {"url": "https://slack.invalid/mcp"}}})})
        res = self.results(self.ctx(repo))
        self.assertEqual(self.assertFails(res, "mcp.server.postgres")[0].severity, "warn")
        self.assertEqual(self.assertFails(res, "mcp.server.slack")[0].severity, "warn")

    def test_an_accepted_server_is_downgraded(self):
        repo = self.kit_repo(config={"mcp_allowed_servers": ["aws-docs"]}, files={".mcp.json": json.dumps({"mcpServers": {"aws-docs": {"command": "uvx", "args": ["awslabs.aws-documentation-mcp-server"]}}})})
        res = self.results(self.ctx(repo))
        self.assertEqual(self.by_id(res, "mcp.server.aws-docs")[0].status, "pass")
        self.assertEqual(levelcalc.cap_mcp(self.ctx(repo), levelcalc.R(res)).level, 5)

    def test_cap_is_l1_for_a_deploy_capable_server(self):
        repo = self.kit_repo(files={".mcp.json": json.dumps({"mcpServers": {"aws": {"command": "x"}}})})
        ctx = self.ctx(repo)
        self.assertEqual(levelcalc.cap_mcp(ctx, levelcalc.R(self.results(ctx))).level, 1)

    def test_no_servers_is_clean(self):
        res = self.results(self.ctx(self.kit_repo()))
        self.assertEqual(self.by_id(res, "mcp.servers")[0].status, "pass")

    def test_malformed_mcp_json_is_reported(self):
        repo = self.kit_repo(files={".mcp.json": "{oops"})
        self.assertFails(self.results(self.ctx(repo)), "mcp.parse")

    def test_deny_rules_for_run_script_and_deploy_tools(self):
        ctx, res = self.with_settings({"permissions": {"deny": ["mcp__*__run_script"]}})
        self.assertIn("mcp__*deploy*", self.assertFails(res, "mcp.deny-rules")[0].message)
        ctx, res = self.with_settings({"permissions": {"deny": ["mcp__*__run_script", "mcp__*deploy*"]}})
        self.assertClean(res, "mcp.deny-rules")


class ManagedSettings(SettingsCase):
    def managed(self, data):
        os.makedirs(os.path.join(self.fs_root, "etc", "claude-code"), exist_ok=True)
        with open(os.path.join(self.fs_root, "etc", "claude-code", "managed-settings.json"), "w") as fh:
            json.dump(data, fh)

    def test_the_shipped_example_passes(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".claude", "templates", "security", "managed-settings.example.json")) as fh:
            self.managed(json.load(fh))
        self.assertClean(self.results(self.ctx(self.kit_repo())), "managed.settings")

    def test_missing_keys_are_named(self):
        self.managed({"allowManagedHooksOnly": False})
        msg = self.assertFails(self.results(self.ctx(self.kit_repo())), "managed.settings")[0].message
        self.assertIn("allowManagedHooksOnly", msg)
        self.assertIn("disableBypassPermissionsMode", msg)

    def test_nothing_visible_is_unknown_not_a_pass(self):
        self.assertEqual(self.by_id(self.results(self.ctx(self.kit_repo())), "managed.settings")[0].status, "unknown")

    def test_attested_or_container(self):
        r = self.by_id(self.results(self.ctx(self.kit_repo(config={"managed_settings_verified": "2026-10-26"}))), "managed.settings")[0]
        self.assertEqual(r.status, "pass")
        r = self.by_id(self.results(self.ctx(self.kit_repo(config={"agent_isolation": "container"}))), "managed.settings")[0]
        self.assertEqual(r.status, "pass")


class Devcontainer(SettingsCase):
    def dc(self, data, rel=".devcontainer/devcontainer.json", text=None):
        repo = self.kit_repo(files={rel: text if text is not None else json.dumps(data)})
        return self.results(self.ctx(repo))

    def test_shipped_template_is_clean(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".claude", "templates", "security", "devcontainer", "devcontainer.json")) as fh:
            text = fh.read()
        res = self.dc(None, text=text)
        self.assertClean(res, "devcontainer.lint")

    def test_credential_mounts_and_sockets(self):
        for mount in ("source=${localEnv:HOME}/.ssh,target=/root/.ssh,type=bind", "source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind",
                      "source=${localEnv:HOME}/.aws,target=/home/vscode/.aws,type=bind", "source=${localEnv:HOME}/.config/gh,target=/x,type=bind",
                      "source=${localEnv:HOME}/.gitconfig,target=/home/vscode/.gitconfig,type=bind", "source=${localEnv:HOME}/.kube,target=/x,type=bind"):
            with self.subTest(mount=mount):
                res = self.dc({"mounts": [mount], "customizations": {"vscode": {"settings": {"remote.containers.copyGitConfig": False,
                              "dev.containers.gitCredentialHelperConfigLocation": "none"}}}})
                self.assertEqual(self.assertFails(res, "devcontainer.lint")[0].severity, "error")

    def test_privileged_and_host_namespaces(self):
        for arg in ("--privileged", "--cap-add=SYS_ADMIN", "--net=host", "--network=host", "--pid=host", "-v/var/run/docker.sock:/s.sock"):
            with self.subTest(arg=arg):
                res = self.dc({"runArgs": [arg]})
                self.assertTrue(any(r.severity == "error" for r in self.assertFails(res, "devcontainer.lint")))

    def test_docker_in_docker_feature(self):
        res = self.dc({"features": {"ghcr.io/devcontainers/features/docker-in-docker:2": {}}})
        self.assertTrue(any("docker" in r.message for r in self.assertFails(res, "devcontainer.lint")))

    def test_forwarding_host_credentials_into_the_container(self):
        res = self.dc({"remoteEnv": {"GH_TOKEN": "${localEnv:GH_TOKEN}"}, "containerEnv": {"X": "${localEnv:AWS_ACCESS_KEY_ID}"}})
        self.assertEqual(len([r for r in res if r.status == "fail" and "into the container" in r.message]), 2)

    def test_git_config_copy_and_credential_helper_defaults_warn(self):
        res = self.dc({})
        msgs = " ".join(r.message for r in self.assertFails(res, "devcontainer.lint"))
        self.assertIn("copyGitConfig", msgs)
        self.assertIn("gitCredentialHelperConfigLocation", msgs)

    def test_root_user_warns(self):
        res = self.dc({"remoteUser": "root"})
        self.assertTrue(any("root" in r.message for r in self.assertFails(res, "devcontainer.lint")))

    def test_jsonc_comments_are_accepted(self):
        res = self.dc(None, text='{\n // comment\n "remoteUser": "vscode", /* c */\n}')
        self.assertFalse([r for r in res if r.id == "devcontainer.lint" and "valid JSON" in r.message])

    def test_other_devcontainer_locations(self):
        res = self.dc({"runArgs": ["--privileged"]}, rel=".devcontainer.json")
        self.assertFails(res, "devcontainer.lint")
        res = self.dc({"runArgs": ["--privileged"]}, rel=".devcontainer/agent/devcontainer.json")
        self.assertFails(res, "devcontainer.lint")

    def test_no_devcontainer_is_skipped(self):
        self.assertEqual(self.by_id(self.results(self.ctx(self.kit_repo())), "devcontainer.lint")[0].status, "skipped")


if __name__ == "__main__":
    unittest.main()
