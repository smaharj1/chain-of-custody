# Sandbox, containers, managed settings and the liveness canary (B3)

The OS sandbox is what makes the agent's shell unable to do the things the credential and git sections worry about, even after it has escaped every hook and prose rule. It is **mandatory from L2**.

## The sandbox profile

`.claude/templates/security/sandbox.settings.json` is a snippet to merge into your project (or, better, managed) settings:

| Setting | Value | Why |
|---|---|---|
| `sandbox.enabled` | true | commands run in the OS sandbox |
| `sandbox.failIfUnavailable` | true | a missing sandbox stops the command; it never runs unsandboxed |
| `sandbox.allowUnsandboxedCommands` | false | the agent cannot retry a failed command outside the sandbox |
| `sandbox.excludedCommands` | empty | in particular, **docker and podman are not excluded**: a container escapes the sandbox |
| network | allowlist of the registries and hosts the loop needs; deny 169.254.0.0/16 and cloud control-plane endpoints | no metadata credentials, no free egress |
| `denyRead` | `~/.aws`, `~/.ssh`, `~/.config/gh`, `~/.config/gcloud`, `~/.azure`, `~/.kube`, `~/.docker`, `.netrc`, `.git-credentials`, `.npmrc`, `.env.production*`, `*.pem`, `*.key` (and held-out tests under the git common dir) | credentials and held-out tests |
| `denyWrite` | `.git/hooks`, `.git/config`, `.git/info`, `.claude/settings.json`, `.claude/hooks`, `.mcp.json` | git and guardrail self-modification |

The snippet also repeats the file rules as `Read(...)` / `Edit(...)` deny rules in `permissions.deny`, because Claude Code derives sandbox filesystem limits from permission rules. The report names `denyRead` and `denyWrite` lists, and the exact key names for the network and filesystem blocks are **unverified (V19)** against the Claude Code version you run. The doctor accepts either form (`sandbox.filesystem.denyRead`, `sandbox.denyRead`, or the permission rules). Check the snippet against `claude`'s settings reference before relying on it.

For a linked git worktree the common git directory is outside the worktree path: add its absolute path (`git rev-parse --git-common-dir`) to `denyWrite` for `hooks`, `config` and `info`.

## "Configured" is not "active" (V19)

Whether `failIfUnavailable` actually stops a command on your macOS, Linux or devcontainer is a probe item. The doctor can check the configuration (`sandbox.enabled`, `sandbox.failIfUnavailable`, `sandbox.allowUnsandboxedCommands`, `sandbox.excludedCommands`, `sandbox.deny-paths`) but not the behaviour. A human confirms it:

1. In a session, ask the agent to read `~/.aws/config` and to write to `.git/hooks/test`. Both must be refused.
2. Then set `sandbox_verified: "<date>"` in `.claude/loop.config.md`.

Until then the sandbox cap holds the level at **L1**. A devcontainer, container or separate OS user that you attest with `agent_isolation` counts instead.

## Containers and separate OS users

The sandbox is not the only way. A credential-free container, or a separate OS user that has none of your credentials, achieves B2 and B3 together (see [credentials.md](credentials.md)). `.claude/templates/security/devcontainer/devcontainer.json` drops all capabilities, forwards no credentials, mounts nothing and runs as an unprivileged user. `devcontainer.lint` flags credential mounts, privileged flags, host namespaces, a docker daemon, `${localEnv:...}` forwarding of credential variables and root.

## Managed settings (L3 and above)

Project settings are writable by the agent. Managed settings are not, if you install them as another user (V21 is the open question of whether the same OS user could override them: assume it can, so use a separate OS user or a container for L3).

`.claude/templates/security/managed-settings.example.json` has the recipe:

- `allowManagedHooksOnly: true`: only the hooks in the managed file run.
- `permissions.disableBypassPermissionsMode: "disable"`. This key is valid in any settings file; **only the managed copy survives a child's `--settings`**, so a child launched with `--settings '{"disableAllHooks":true}'` cannot undo it (RC-31).
- `enableAllProjectMcpServers: false` and a deny list for `Workflow`, `mcp__*__run_script` and `mcp__*deploy*`.
- The kit's hooks installed as managed hooks. The example's SessionStart command points at `/opt/chain-of-custody/scripts/session_start.py`: **copy `.claude/scripts/` to a directory the agent's user cannot write** and point the managed hooks there. A managed hook that runs a script from the repository runs whatever the agent put there.

Where managed settings live: `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS, `/etc/claude-code/managed-settings.json` on Linux. `managed.settings` reads them if it can see them; otherwise set `managed_settings_verified` (or `agent_isolation: container`).

## The hook-liveness canary

Hooks are guardrails, and a guardrail that did not load is worse than none because everything looks fine. In untrusted `-p` contexts (a fresh clone, CI, cloud) project `permissions.allow` and subagent frontmatter hooks are not applied.

`.claude/scripts/session_start.py` is the SessionStart hook that proves hooks load. It:

1. writes a random nonce for the session to `<git-common-dir>/loopctl/nonces/<session_id>` (through `kitlib.runstate`) and returns it in the session context as `canary session=<id> nonce=<hex>`;
2. compares `claude --version` with `claude_code_min` and `claude_code_tested_max` in `.claude/KIT_VERSION` ([version-policy.md](version-policy.md)): a loud banner at L0 to L2, and a **refusal at L3 and above** (no nonce is written);
3. announces a HALT and keeps the returned context under 1 KiB.

It is advisory and never blocks: every path exits 0. Before loopctl exists, the conductor's prose checks that the context line is there and the file matches; with no nonce it does not dispatch. loopctl enforces the same from Phase 4. The nonce file is writable by the agent's user unless the sandbox denies it, so the canary proves "the hook ran", not "the agent is honest".

Wiring (the release stage adds it to `settings.json`; this phase does not edit that file):

```
"SessionStart": [{"hooks": [{"type": "command", "timeout": 10,
    "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/scripts/session_start.py\""}]}]
```

`onFailure: block` on blocking-capable hooks, and the guards' own fail-closed handling, are Phase 2. `onFailure` has no effect on Stop, SubagentStop, TaskCompleted, TeammateIdle or async hooks, so those stay advisory and the loop re-validates them.
