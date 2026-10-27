# MCP is a tool plane

An MCP server is a set of tools the agent can call. A server that can deploy, run a script in your cloud account, or write to a database or a SaaS account is a mutation path that none of the credential checks sees, because the server holds the credential, not the agent (report section 5.6).

## Rules

- Engineers and critics get `disallowedTools: mcp__*`. A project that wants a documentation server for engineers names it on purpose: add its tool name to the agent's `tools:` list and remove it from the deny.
- In any session that is part of a loop run: deny `mcp__*__run_script`, `mcp__*deploy*` and any mutating cloud, database or SaaS tool; set `enableAllProjectMcpServers: false`.
- `.mcp.json`, `.claude-plugin/**` and `plugin/**` are H3 paths (a code owner reviews them) and are in the integrity glob for the test-integrity gate.
- Ambient deploy plugins (for example a "deploy on AWS" plugin) are disabled in factory profiles.

`.claude/templates/security/mcp-lockdown.settings.json` has the settings; `.claude/templates/security/mcp.json.example` is a `.mcp.json` with one named documentation server.

## What the doctor checks

| Check id | |
|---|---|
| `mcp.enable-all-project-servers` | fails if `enableAllProjectMcpServers` is true anywhere: any server a pull request adds to `.mcp.json` would start |
| `mcp.server.<name>` | every server from `.mcp.json`, user settings and this project's local scope. **Cloud, deploy host, container, GitHub or GitLab tooling is an error** (it caps the level at L1); a database or SaaS server is a warning; anything else is a warning that it is enabled |
| `mcp.plugin.<name>` | an enabled plugin whose name looks deploy-capable |
| `mcp.deny-rules` | `permissions.deny` lacks `mcp__*__run_script` or `mcp__*deploy*` |

The classification is by name, command, arguments and URL; the doctor cannot see a server's tools. If a flagged server is read-only and you want it, name it in `mcp_allowed_servers` in `.claude/loop.config.md` (H6): the doctor then reports it as accepted. A GitHub MCP server holding a token is flagged on purpose: it can act as you, bypassing B1.

## Settings are layered

The doctor reads managed, `--settings`, local, project and user settings in that order of precedence. A permissive project file cannot hide behind a stricter user file, and a stricter managed file wins over both.
