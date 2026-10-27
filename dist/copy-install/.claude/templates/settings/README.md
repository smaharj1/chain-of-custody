# Settings templates (report section 7.2)

Per-project settings belong to the project (A28). The kit ships templates and never overwrites a project's `settings.json` on upgrade (CO-33). Everything in this directory except this file is **generated** from `.claude/scripts/guards/hooks.manifest.json` and the sandbox profile by `python3 .claude/scripts/guards/wiring.py --write`; `--check` (run by the tests) fails when they drift. Do not edit them by hand.

| File | What it is | Used by |
|---|---|---|
| `interactive.settings.json` | An allow template with **named scripts only** (`Bash(npm run test:*)`, not `Bash(npm:*)`), an `ask` list for the git writes, the deny block, `enableAllProjectMcpServers: false`, and the sandbox profile | A human merging it into their `.claude/settings.json`. Replace the example scripts with the project's own |
| `headless-child.settings.json` | The profile a driver child is started with, as `--settings <file>`: `permissions.defaultMode: dontAsk`, an explicit allow list, the deny block (which includes `Workflow`, RC-17), no project MCP servers, the sandbox on, `env: {}` for the driver to fill, and the kit hooks (a child does not inherit them, V5) | `loopctl run` (Phase 4c) |
| `critic.settings.json` | The same, for a critic child: Edit, Write, MultiEdit and NotebookEdit denied, no installs | the driver, for `code-reviewer`, `design-critic`, `security-reviewer` |
| `deny-baseline.json` | The deny block alone | `python3 .claude/scripts/doctor.py` diffs the effective settings against it (`guards.deny-drift`) |

## The deny block

Infrastructure mutators (`terraform apply|destroy|import|state`, `cdk deploy|destroy|bootstrap`, `pulumi up|destroy`, `kubectl apply|delete`, `helm install|upgrade|uninstall`, `sam deploy`, `serverless deploy`, `fly|vercel|netlify|wrangler|railway` deploys, `aws cloudformation deploy`, `aws s3 rm|sync`, `aws iam`), `sudo`, privileged Docker and `docker push`, `Bash(claude:*)` and `npx @anthropic-ai/claude-code`, pushes to `main`/`master` and force, delete, mirror and tag pushes, `commit --no-verify`, `gh pr merge|release|secret|workflow run|repo edit`, `Read` of `.env` files except `.env.example` (and `~/.aws`, `~/.ssh`, `~/.config/gh`), `Edit` of the guardrail paths, `mcp__*__run_script`, `mcp__*deploy*`, and `Workflow`.

It is a typo guard (P25): permission deny rules are not a security boundary, and Read/Edit rules do not cover what a subprocess does. It is still worth having because it works in bypass mode, and it is the second layer under the guards, which deny the same commands (the tests check that every `Bash(...)` entry is also refused by the guards).

## Sandbox

`sandbox.enabled: true`, `failIfUnavailable: true`, `allowUnsandboxedCommands: false`, no `docker` in `excludedCommands`, the network allowlist with `169.254.0.0/16` denied, `denyRead` on credential directories and `denyWrite` on `.git/hooks`, `.git/config`, `.git/info`, `.claude/settings.json`, `.claude/hooks` and `.mcp.json`. The source is `.claude/templates/security/sandbox.settings.json`; the key names follow the report and are unverified against a live CLI (V19). Mandatory from L2.

## Not in these files

- **Model pinning and fork mode.** `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (when a model profile is pinned) and the fork-mode and background-task variables go into the child's `env` block, per runner, written by the driver (report 11.3, 11.4). The template leaves `env` empty.
- **Managed settings** for L3 and above (`allowManagedHooksOnly`, `permissions.disableBypassPermissionsMode`, the kit hooks as managed hooks): `.claude/templates/security/managed-settings.example.json`.
- **MCP lockdown.** The deny block already refuses `mcp__*__run_script` and `mcp__*deploy*`; the fuller snippet (`enabledMcpjsonServers`, per-server denies) is `.claude/templates/security/mcp-lockdown.settings.json`.
- **Modes never edit settings.** They print a snippet for the human (CO-16).

To make the staged v2 wiring live, merge the hooks block of `.claude/settings.v2.json` into `.claude/settings.json` in one reviewed commit and remove the v1 hooks; the doctor's `guards.wiring` check says when it is consistent.
