# Platform notes: permissions, capabilities, hooks, docs tools

> Shared kit rule (not a project primer). The prose files point here instead of repeating it. Report: CO-16, L-41, P-9, P-8.8, A13, A26, A27, P-18.7, CO-46; §12.1.

## Permissions snippet (the human applies it)

The kit ships `"allow": []` on purpose, and **agents never edit** `.claude/settings.json` or `.claude/settings.local.json`. Those files are guardrails (H6): an agent that can widen its own permissions has no boundary. Where a long run would stall on permission prompts, the Planner or Orchestrator **prints a proposed snippet** and the human pastes it into `.claude/settings.local.json` (user-local, gitignored) or reviews it into `settings.json`.

Build the snippet from `.claude/loop.config.md` (`verify_tests`, `verify_run`) and `.claude/context/local-dev.md`:

```json
{
  "permissions": {
    "allow": [
      "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
      "Bash(pnpm test:*)", "Bash(pnpm lint:*)", "Bash(pnpm build:*)", "Bash(pnpm db:migrate:*)"
    ]
  }
}
```

Rules for the snippet:

- Allow **read-only git** and the project's **test, lint, build** commands. Nothing that writes shared state, pushes, deploys or talks to cloud services.
- No blanket entries (`Bash(*)`, `Bash(pnpm:*)`). A rule that matches any subcommand matches `pnpm publish` too.
- A deny rule is a typo guard, not a boundary: `Bash` deny patterns are easy to get around, which is why the boundary lives in the sandbox, credentials and CI (docs/security), not in this list.
- **`/fewer-permission-prompts`** may be used by a human in an **interactive** session. It writes allow rules mined from past transcripts, which can include commands that mutate things, so the human reads the diff before accepting it. It is never used to build a loop's allowlist.
- The future headless driver passes its own `--settings` and runs with `--permission-mode dontAsk`; nothing prompts there, and a denied call is logged as an event.

## Capabilities and fallbacks

Never assume a tool exists because it did on the maintainer's machine. The Orchestrator picks from what this session actually has, in this order, and records the choice.

| Need | Order of preference |
|---|---|
| **See the running app** (auto-demo) | A browser-preview MCP, if one is present: start the app, open the route, take a screenshot. Then the bundled `/run` skill. Then a text demo: the route plus an HTTP status or `curl` check. |
| **Mobile project** | The iOS Simulator tool, where the session has one: launch the build, screenshot the screen. Otherwise the text demo above. |
| **Notify the human** at a checkpoint or hard-stop | A push-notification tool if the session has one (and a project `notify_command` hook if configured). Otherwise a clearly marked `CHECKPOINT` or `HARD-STOP` banner in the transcript. |
| **Library documentation** | A documentation MCP if one is available (see below). Otherwise `WebFetch` on the library's own docs. |

## `.claude/launch.json`

The desktop app's preview reads `.claude/launch.json` to know how to start the dev server. It is **user-local** (paths, ports and commands differ per machine), so it is gitignored and never committed (`README.md` lists it with the other ignored files). When it exists, the browser-preview capability uses it; when it does not, the Orchestrator starts the app from `local-dev.md` instead. A future `/bootstrap` may generate it.

## Documentation tools are optional

Agents do not require any particular documentation MCP. The engineer, critic and reviewer agent files carry an explicit `tools:` allowlist **without** any MCP tool, plus `disallowedTools: Agent, Skill, Workflow, mcp__*`. The prose rule is: **if a documentation tool is available, use it for library API questions; otherwise use `WebFetch`.** Do not rely on training memory for library APIs.

To use a docs MCP in a project: add its tool names to the `tools:` line of the agents that should have it, and remove `mcp__*` from that agent's `disallowedTools` (the deny wins over the allow, so both edits are needed). Tool names depend on how the server was installed (a plugin names them `mcp__plugin_<plugin>_<server>__*`, a plain server `mcp__<server>__*`), so copy the names from your own session. MCP servers are a tool plane the project governs: an agent that can call a cloud or deploy MCP tool has more reach than its `Bash` allowlist suggests.

## SessionStart hooks: keep them small

If a project adds a SessionStart hook (the kit's own, or a custom one):

- Inject **`additionalContext` of 1 KiB or less**. A 150-line dump on every session wastes context in every run.
- Give the hook a **`timeout`**, and never run slow checks (`aws sts get-caller-identity`, `docker compose ps`, `lsof`) synchronously. Do network checks on demand, not at start.
- Do not tell the model to use plan mode or any other mode switch: it fights the kit's modes (`/pm`, `/architect`, ...).
- The agent's environment should not hold cloud credentials for such a check to find in the first place.

## Third-party skills and plugins

Some plugins install SessionStart text that tells the model it MUST use a particular skill before doing anything. That fights the kit's own mode prompts. In a factory profile (a loop run, a driver child) **disable third-party skills and plugins whose startup text is that insistent.** Subagents cannot call skills anyway (`Skill` is denied), so the kit does not depend on any: the interview checklist in `/pm` and `/planner` is inline.

Never name a kit skill `loop` (the platform has a bundled `/loop`) or `code-review` (the platform has a bundled `/code-review`, and which one wins is unverified). The kit's reviewer is the `code-reviewer` **agent**; the bundled `/code-review` or `/security-review` can be used as an optional second opinion in an interactive session.

## The root `skills/` folder and the Codex adapter

`skills/chain-of-custody/SKILL.md` is the **Codex** discovery file and points at paths under `.claude/`. If the repository root were also used as a Claude Code **plugin** root, that file would load as a Claude skill and point at paths that do not exist there. So: do not use this repository's root as a plugin root. The packaging phase moves the plugin to `plugin/` with the marketplace file at the root, and generates the Codex and Cursor installs into `dist/copy-install/` with their paths rewritten. Until then, install by copying `.claude/` as the README says.
