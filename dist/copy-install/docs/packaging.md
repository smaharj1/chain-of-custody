# Packaging: the plugin, the copy install, and upgrading

The kit comes in two forms. They are built from the same files, so they behave the same where they overlap.

| Form | What it is | Where it is built | Use it when |
|---|---|---|---|
| **Plugin** | `plugin/`, with the marketplace file `.claude-plugin/marketplace.json` | generated | You want the modes, the agents and the guards in many projects, and updates through Claude Code |
| **Copy install** | `dist/copy-install/`: a `.claude/` folder, the Codex and Cursor adapters, and the docs the prose points at | generated | You want the whole kit inside the project, including the loop (`loopctl`, the driver, parallel runs, unattended runs) |

`.claude/` in this repository is the one source anyone edits. `plugin/`, `dist/copy-install/`, `.claude-plugin/marketplace.json` and `.claude/KIT_MANIFEST` are **generated** by `python3 tools/build-dist.py --write` from the files git tracks, and a test fails when they drift (`python3 tools/build-dist.py --check`). Never edit them by hand. A new file must be `git add`ed before the build sees it.

## Install as a plugin

```
/plugin marketplace add <this repository>
/plugin install chain-of-custody@chain-of-custody
```

Claude Code asks for three optional choices (`userConfig`): the protected branches, the integration branch, and extra paths that may be edited on a protected branch. Then run **`/chain-of-custody:init`** once per project. It writes what a plugin cannot ship, because a plugin's own `settings.json` applies only `agent` and `subagentStatusLine`:

- the deny block, the sandbox profile and `enableAllProjectMcpServers: false` into `.claude/settings.json` (added to your settings, never replacing a value of yours);
- the files a project owns, copied from the plugin when missing: `CLAUDE.md`, `loop.config.md` and the domain primers;
- your three choices into `.claude/loop.config.md`, through the key registry (they are guardrail keys);
- the kit's lines in `.gitignore`.

`/init` plans and shows a diff. A person applies it, from a plain terminal: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init_project.py" --repo . --apply`. It refuses inside a Claude Code session, and the guards deny an agent running it. Claude Code has a built-in `/init` (it writes a `CLAUDE.md`); in a plugin the kit's one is namespaced, so use the full name.

Modes are namespaced too: `/chain-of-custody:pm`, `:architect`, `:tech-lead`, `:planner`, `:orchestrate`, `:bootstrap`, `:retro`, `:init`, `:migrate`. The text of each mode says `/pm`, `/architect` and so on; read those as the namespaced names.

**What the plugin form does not do (yet).** The loop's machinery reads the kit from the project's own git history at the base commit: `loopctl run` with `runner: driver`, unattended and parallel runs, the doctor's copy-install checks, and the GitHub setup script that `/bootstrap` generates (`setup.sh` looks for `.claude/scripts/github/` in the project). These need the **copy install**. In a plugin install the interactive modes, the agents, the guard hooks, `verify.py`, `pr_prepare.py` (it falls back to the plugin's own PR template) and the read-only `loopctl` commands work from the plugin's `scripts/`; `doctor.py` runs but may report copy-install files as missing. Making the whole loop plugin-native is deferred.

**Unverified assumption (probe V13).** The plugin's prose and hooks use `${CLAUDE_PLUGIN_ROOT}`. Whether Claude Code expands it inside skill and agent text, and whether plugin hooks fire for project agents, has not been checked against the real binary. If it does not expand in text, the fix is one constant (`PLUGIN_ROOT_VAR` in `tools/distlib/rewrite.py`). If plugin hooks do not fire, `/init` must write the hooks into the project settings instead. A human checks this once with `sh tools/validate-plugin-real.sh` and `claude --plugin-dir ./plugin`.

## Install as a copy

From a checkout of the kit (or a release archive):

```
cp -R dist/copy-install/. /path/to/your/project/
```

`dist/copy-install/` holds exactly what ships: the kit's `.claude/` (without the repository's own settings overrides, worktrees, runtime files, tests or tools), the adapters (`AGENTS.md`, `.cursor/rules/chain-of-custody.mdc`, `skills/chain-of-custody/`) and the docs the prose refers to. The v1 habit `cp -r .claude .` from a kit checkout still works, but it also copies whatever else is in that folder; prefer `dist/copy-install/`. Then run `/init` (the copy form needs only the settings and `.gitignore` parts), and `/bootstrap`.

`.claude/KIT_MANIFEST` lists every installed file with its SHA-256 and its class, so an upgrade can tell your changes from the kit's.

## Upgrade a copy install: `/migrate`

Coming from 1.x? Read [Migrating to v2](migrating-to-v2.md) first: it lists the order of steps and what changes for you.

`.claude/scripts/migrate.py` upgrades an install to the kit in another folder. It never asks you to hand-merge a file the kit and you did not both change, and it never overwrites a file you changed.

```
git clone <the kit> ../kit                      # or unpack a release; the script needs no network
python3 ../kit/.claude/scripts/migrate.py --repo .            # dry run: prints the plan, changes nothing
python3 ../kit/.claude/scripts/migrate.py --repo . --apply    # from a plain terminal, after a clean `git status`
```

Your install has no kit history, so the base of each three-way merge comes from an archive shipped in the kit: `.claude/migrate/v1.2.0.tar` is `git archive` of the `v1.2.0` tag. The script picks the archive that matches `.claude/KIT_VERSION`.

| Class | Files | Rule |
|---|---|---|
| kit | agents, skills, scripts, spec, templates, the protocols in `context/`, docs (the v1 `hooks/` scripts are no longer shipped) | not touched by you: replaced. Touched by you and the kit: `git merge-file` three-way merge; clean result written, conflict left for you. Touched by you only: kept. Deleted by you: stays deleted. No longer shipped: removed if you never changed it, kept and listed if you did |
| seed | `CLAUDE.md`, `loop.config.md`, the domain primers, `.claude/rules/` | yours once installed. Added when missing, replaced only if still byte-identical to the base template |
| frozen | `.claude/settings.json` | a guardrail: added when missing, never changed |

Your own files (a custom agent, a skill, a rule) are not in the manifest and are never looked at.

**Apply.** It refuses inside a Claude Code session, refuses a dirty git tree (commit first, so `git checkout` is your undo; `--allow-dirty` overrides), and refuses a kit folder whose files do not match its `KIT_MANIFEST`. Before writing it copies every file it replaces or removes to `.claude/.migrate/backup/<time>/`. `KIT_VERSION` and `KIT_MANIFEST` are written last, and only when no conflict is open, so a half-done migration is simply run again.

**Conflicts.** For each one your file is left alone and the merge result with markers is written to `.claude/.migrate/conflicts/<path>`. Resolve it in your file, then `migrate.py --repo . --resolved <path>`; or take the kit's version with `--take-kit <path>` (your copy is backed up). A resolution is remembered only while the file stays as you left it. When you are done, commit and delete `.claude/.migrate/`.

**What changes in v2 for an existing install.** The five v1 commands became skills: an unmodified `.claude/commands/*.md` is removed, a modified one stays (move your edits into the skill of the same name, then delete it). The v1 shell hooks (`guard-git.sh` and `guard-main-edit.sh`, in `.claude/hooks/`) were removed in 2.0.0. While your `settings.json` still names one, the migration keeps it (KEEP-RETIRED, "still wired in your settings.json"), because a hook whose script is missing guards nothing; once you have wired the v2 guards (next section), run the migration again and it removes the unmodified copies. A modified copy always stays and is listed. Your `settings.json` and `loop.config.md` are untouched; keys added since use their defaults, and `python3 .claude/scripts/doctor.py` shows what the new version expects. Wire the guards straight after applying the migration.

## The v2 guards are on by default

The guard suite (one Python hook entry point, a manifest, a self-test; `docs/security/guards.md`) replaced the v1 shell hooks in 2.0.0.

- **A new copy install** (`dist/copy-install/`) already has them: its `.claude/settings.json` is the generated v2 wiring plus an empty permission allowlist. `.claude/settings.v2.json` is a generated copy of the same wiring, kept so an upgrade has something to read.
- **The plugin** gets the same hooks from `hooks/hooks.json`, generated from the same manifest.
- **An existing install** keeps whatever its `.claude/settings.json` held, because settings are a guardrail and `migrate.py` never changes them. Switching is a human step:

1. Read `python3 .claude/scripts/guards/wiring.py --print`. It is the hooks block generated from `.claude/scripts/guards/hooks.manifest.json`.
2. In `.claude/settings.json`, replace the `hooks` block with that one. Keep your `permissions`. (Any entry that still names `guard-git.sh` or `guard-main-edit.sh` goes.)
3. Run `python3 .claude/scripts/doctor.py`, start a new session, and check that the `kit session_start: canary` line appears. No canary line means the hooks are not firing.
4. Run the migration again (dry run, then `--apply` from a plain terminal): with the old hooks no longer named in `settings.json`, it removes the v1 hook scripts you never changed.

## What a mode is now

Each mode is a skill: `.claude/skills/<mode>/SKILL.md`, and for the long ones a few reference files next to it that the skill links to (the Tech Lead's two lanes, the Orchestrator's preamble and loop). The old `.claude/commands/*.md` files are gone; there are no aliases.

| Skill | Who can start it | Why |
|---|---|---|
| `pm` | you, or the model when you describe a feature | it only interviews and writes `requirements.md` |
| `architect`, `tech-lead` | you | they hand off to the next mode only after you confirm, in words |
| `planner`, `orchestrate`, `bootstrap`, `retro`, `init`, `migrate` | you | they change plans, repositories, settings or the kit itself |

All of them have `argument-hint` and `effort`, none forks a context, and none is named `loop` or `code-review` (Claude Code has bundled commands by those names).

## Checking a build

- `python3 tools/build-dist.py --check`: the generated files equal what the sources produce (the test suite runs this).
- `python3 tools/validate-plugin.py --strict`: the stdlib stand-in for `claude plugin validate`: manifest keys, `userConfig`, hook scripts exist, agent and skill frontmatter, every `${CLAUDE_PLUGIN_ROOT}/` path in prose exists, no kit path left unrewritten, no runtime or junk files, the marketplace entry points at `./plugin`. Passing is necessary, not sufficient.
- `sh tools/validate-plugin-real.sh`: runs the real `claude plugin validate ./plugin --strict`. A human runs it; it starts the real `claude`.

## For maintainers

- The plugin keeps the layout of `.claude/` one level up: `scripts/`, `context/`, `spec/`, `templates/`, `agents/`, `skills/`, `docs/`, plus `hooks/hooks.json`, `evals/`, and `seed/` (the project-owned files `/init` copies). In plugin prose, kit paths such as `.claude/scripts/doctor.py` are rewritten to `${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py`; project paths (`.claude/CLAUDE.md`, `.claude/loop.config.md`, the primers, `.claude/loop.jsonl`) are not. Rules and exceptions: `tools/distlib/rewrite.py`.
- A new top-level area under `.claude/` fails the build until `tools/distlib/build.py` says where it goes (or that it stays out of the plugin).
- A new file class or a new project-owned file is a change to `.claude/scripts/kitlib/kitmanifest.py`.
- A new base for `migrate.py` (a future release that wants to upgrade from the current one) is `git archive --format=tar <tag> .claude AGENTS.md .cursor skills > .claude/migrate/v<version>.tar`.
- Never in an output: `.claude/worktrees/`, `settings.local.json`, runtime files, caches, `tests/`, `tools/`, `.claude/migrate/`. The build reads `git ls-files`, so an untracked file cannot leak.
