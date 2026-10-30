# Packaging: the plugin, and upgrading

The kit comes in one form: the Claude Code plugin `chain-of-custody`. The repository root is the plugin. `.claude-plugin/plugin.json` describes it, and `.claude-plugin/marketplace.json` lists it with source `./`, so the plugin id is `chain-of-custody@chain-of-custody`. Nothing of the kit is copied into a project. A project's `.claude/` holds only the project's own files.

Before 2.0 the kit was copied into each project's `.claude/`. That copy install is gone. A project that still has one moves onto the plugin with `/chain-of-custody:migrate` ([Migrating to v2](migrating-to-v2.md)).

Codex and Cursor cannot load a plugin. `python3 "$KIT/scripts/adapt.py" --target <project>` writes a guidance-only copy for them; `adapters/SETUP.md` says what it does not enforce.

## Install the plugin

```
/plugin marketplace add <this repository>
/plugin install chain-of-custody@chain-of-custody
```

Claude Code asks for three optional choices (`userConfig`): the protected branches, the integration branch, and extra paths that may be edited on a protected branch. Then run **`/chain-of-custody:init`** once per project. It writes what a plugin cannot ship, because a plugin's own `settings.json` applies only `agent` and `subagentStatusLine`:

- the deny block, the sandbox profile and `enableAllProjectMcpServers: false` into `.claude/settings.json` (added to your settings, never replacing a value of yours);
- the files a project owns, copied from the plugin's `seed/.claude/` when missing: `CLAUDE.md`, `loop.config.md` and the domain primers;
- your three choices into `.claude/loop.config.md`, through the key registry (they are guardrail keys);
- the kit's lines in `.gitignore`.

It writes no hooks: the guards come from the plugin (see below).

`/init` plans and shows a diff. A person applies it, from a plain terminal: `python3 "$KIT/scripts/init_project.py" --repo . --apply`. It refuses inside a Claude Code session, and the guards deny an agent running it. It also refuses while the project still holds an old copy install, and points to `/chain-of-custody:migrate`. Claude Code has a built-in `/init` (it writes a `CLAUDE.md`); the kit's one is namespaced, so use the full name.

`$KIT` is the folder the plugin is installed in. `/init` and the doctor print it as `kit: <path>`. Commands the kit prints for you already name the script by its full path.

Modes are namespaced too: `/chain-of-custody:pm`, `:architect`, `:tech-lead`, `:planner`, `:orchestrate`, `:bootstrap`, `:retro`, `:init`, `:migrate`. The text of each mode says `/pm`, `/architect` and so on; read those as the namespaced names.

The whole kit runs from the plugin: the modes, the agents, the guard hooks and the loop (`loopctl`, the driver, parallel and unattended runs). A run records the kit it started with (the plugin's version and a fingerprint of its files) and refuses to go on if the kit changes mid-run.

**Unverified assumption (probe V13).** The plugin's prose and hooks use `${CLAUDE_PLUGIN_ROOT}`. Whether Claude Code expands it inside skill and agent text, and whether plugin hooks fire for project agents, has not been checked against the real binary. If it does not expand in text, the prose's `${CLAUDE_PLUGIN_ROOT}/...` paths need another form. If plugin hooks do not fire, the guards have no other wiring, and the liveness canary shows it. A human checks this once with `sh tools/validate-plugin-real.sh` and `claude --plugin-dir .` in a scratch project.

## Upgrade

- **The plugin.** `claude plugin update chain-of-custody@chain-of-custody`, then start a new session. Read the `CHANGELOG.md` entries since your version first. Your project's `.claude/` holds only your own files, so an update never touches them. Config keys added since use their defaults; `python3 "$KIT/scripts/doctor.py"` shows what the new version expects. Finish or abandon a loop run before you update.
- **A copy install** (kit 1.x, or a 2.0 release candidate copied into `.claude/`): follow [Migrating to v2](migrating-to-v2.md). `/chain-of-custody:migrate` removes the kit files you never changed, keeps your own files and your edits, and takes the kit's hooks out of `.claude/settings.json`.

## The v2 guards are on by default

The guard suite (one Python hook entry point, a manifest, a self-test; `docs/security/guards.md`) replaced the v1 shell hooks in 2.0.0.

- **The plugin wires them.** `hooks/hooks.json` is the only wiring. It is generated from `scripts/guards/hooks.manifest.json` by `python3 "$KIT/scripts/guards/wiring.py" --write`, and every handler runs `${CLAUDE_PLUGIN_ROOT}/scripts/...`. The guards load whenever the plugin is enabled.
- **A project never wires them.** The templates in `templates/settings/` carry permissions and the sandbox, no hooks, and `/init` writes none. A project's settings can only switch the guards off (`disableAllHooks`, or the plugin set to false in `enabledPlugins`). `python3 "$KIT/scripts/guards/wiring.py" --check --project .` and the doctor row `guards.wiring` report that, and a second wiring of the kit's scripts.
- **Check that they fire.** Run `python3 "$KIT/scripts/doctor.py"`, start a new session, and look for the `kit session_start: canary` line. No canary line means the hooks are not firing.

## What a mode is now

Each mode is a skill: `skills/<mode>/SKILL.md` in the plugin, and for the long ones a few reference files next to it that the skill links to (the Tech Lead's two lanes, the Orchestrator's preamble and loop). The old `.claude/commands/*.md` files are gone; there are no aliases.

| Skill | Who can start it | Why |
|---|---|---|
| `pm` | you, or the model when you describe a feature | it only interviews and writes `requirements.md` |
| `architect`, `tech-lead` | you | they hand off to the next mode only after you confirm, in words |
| `planner`, `orchestrate`, `bootstrap`, `retro`, `init`, `migrate` | you | they change plans, repositories, settings or the kit itself |

All of them have `argument-hint` and `effort`, none forks a context, and none is named `loop` or `code-review` (Claude Code has bundled commands by those names).

A project can replace one of the kit's agents: copy `agents/<role>.md` to `.claude/agents/<role>.md` in the project and edit it. The doctor row `plugin.overrides` lists such files. An override does not get the kit's updates; delete it to use the kit's agent again.

## Checking the plugin

- `python3 tools/validate-plugin.py --strict`: the stdlib stand-in for `claude plugin validate`: manifest keys, `userConfig`, hook scripts exist, agent and skill frontmatter, every `${CLAUDE_PLUGIN_ROOT}/` path in kit prose exists, no kit path left as a project `.claude/` path, no runtime or junk files, the marketplace entry points at `./`. Passing is necessary, not sufficient.
- `python3 "$KIT/scripts/guards/wiring.py" --check`: `hooks/hooks.json` and the templates in `templates/settings/` equal what the manifest generates, and every script the hooks run exists.
- `sh tools/validate-plugin-real.sh`: runs the real `claude plugin validate . --strict`. A human runs it; it starts the real `claude`.

## For maintainers

- Kit prose names kit files as `${CLAUDE_PLUGIN_ROOT}/...` and project files as `.claude/...`. Scripts find kit files through `scripts/kitlib/kitpaths.py`, and a command the kit prints names its script by its absolute path (`kitpaths.script_cmd`).
- A new project-owned starter file goes in `seed/.claude/`; `/init` copies what is there when the project lacks it.
- `migrate/v1.2.0.tar` is `git archive --format=tar v1.2.0 .claude AGENTS.md .cursor skills`, and a test checks it against the tag. `migrate.py` only moves copy installs onto the plugin, so no release after 2.0 needs a new base. `scripts/kitlib/kitmanifest.py` tells a copy install's kit files from its project files.
- Never commit runtime files, caches or `settings.local.json`: the validator reads the files git tracks or would track, and fails on them.
