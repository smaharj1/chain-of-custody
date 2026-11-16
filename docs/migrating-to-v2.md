# Migrating a copy install to 2.0.0

For a project that has the kit copied into its `.claude/`: kit v1.2.0, an older v1 copy, or a 2.0 release candidate. In 2.0.0 the kit is a plugin (`chain-of-custody@chain-of-custody`), and the copied kit files have to go: left in place, their hooks would run a second, older set of guards.

The move is one script, `scripts/migrate.py`, and the `/chain-of-custody:migrate` skill walks you through it. It works offline and never throws away a file you changed. It changes nothing until a person runs its `--apply` command from a plain terminal.

`docs/packaging.md` describes the plugin. This page is the order of steps and the cases that need a decision.

## Before you start

- Work on a branch and commit everything first: `git switch -c upgrade-kit-2`. `--apply` refuses uncommitted changes, so `git checkout` and the branch are your undo.
- Read the 2.0.0 sections of `CHANGELOG.md`, Upgrade impact first.
- Note which config keys you rely on. `.claude/loop.config.md` is yours and is never touched; keys added since use their defaults.

## Steps

1. **Install the plugin**, in Claude Code: `/plugin marketplace add smaharj1/chain-of-custody`, then `/plugin install chain-of-custody@chain-of-custody`. The dry run prints these two commands if the plugin does not look installed.
2. **Dry run.** Run `/chain-of-custody:migrate` in the project. It runs `migrate.py --repo .`, which prints the plan and changes nothing, and explains it. From a shell the same is `python3 "$KIT/scripts/migrate.py" --repo .`. The plan has these parts, in this order:
   - NEEDS YOU: a path migrate will not touch (a symlink, a file under a symlinked folder, a file that is not a regular file, a file over 8 MiB), or a file of yours under `.claude/scripts/` that your settings still run or name. Look at each one and remove, replace or move it by hand; for a script, move it to a project path such as `scripts/` and update the hook or permission rule.
   - KEEP AS PROJECT OVERRIDE: a `.claude/agents/<role>.md` you edited.
   - MOVE TO `.claude/kit-backup/<old version>/`: another kit file you edited, or a file the kit never shipped under `.claude/scripts/`.
   - LEFT IN PLACE: an `AGENTS.md`, `.cursor/rules/` or `skills/` file you edited, or a symlink there. These are outside `.claude/`. If you use Codex or Cursor, the plan prints the `adapt.py` command that writes the current adapter.
   - UPDATE: `CLAUDE.md` or a primer you never changed; the plugin's current starter file replaces it. `loop.config.md` is never replaced.
   - REMOVE: still the kit's file; the plugin has it now.
   - SETTINGS: the kit's hook entries it takes out of `.claude/settings.json` and `.claude/settings.local.json`.
   - then, what `/init` does: the starter files that are missing, the deny list and sandbox, your `userConfig` choices and the `.gitignore` lines.
3. **Apply.** From a plain terminal, not inside a Claude Code session, run the command the plan prints: `python3 "$KIT/scripts/migrate.py" --repo <project> --apply`, with the same `--from-version`, `--base-archive` and `--option` choices as the dry run. It refuses inside a Claude Code session, and the guards deny an agent running it. It refuses while any path NEEDS YOU, and it refuses a git work tree with uncommitted changes unless you pass `--allow-dirty`. It backs `.claude/settings.json` up to `.claude/settings.json.pre-init` (and `settings.local.json` to `settings.local.json.pre-migrate`) and writes `.claude/.migrate/report.json` as the receipt. It checks every path before the first write and removes the old `KIT_VERSION` and `KIT_MANIFEST` last, so if it stops part way, fix the cause and run the same command again.
4. **Check.** `python3 "$KIT/scripts/doctor.py"`: `plugin.copy-install` and `guards.v1-residue` must pass. Expect `L1` until you attest the boundary; `doctor.py --level` says what blocks the next level. Then `python3 "$KIT/scripts/guards/wiring.py" --check --project .`. Start a new session and look for the `kit session_start: canary` line; none means the hooks are not firing.
5. **Carry your edits over.** Each file in `.claude/kit-backup/<old version>/` is an edit of yours that the plugin's version does not have. Move what you still need into a project file: a rule, a primer line, a project skill, or an agent override. `/chain-of-custody:migrate` helps you draft the change.
6. **Commit.** Review `git status` and `git diff`, then commit. Delete `.claude/.migrate/` (it is in the kit's `.gitignore` lines). `.claude/kit-backup/` is not ignored, so the moved files show in `git status`. Delete it once you have carried your edits over.

## What happens to each file

migrate tells the kit's files from your edits by the SHA-256 the old version shipped for each file.

| File | What happens |
|---|---|
| a kit file whose hash matches the old kit | removed: the plugin has it now |
| a `.claude/agents/<role>.md` you edited | kept as a project override: from now on it replaces the kit's `<role>` agent; delete it to use the kit's |
| any other kit file under `.claude/` you edited (an edited `.claude/commands/*.md` included) | moved to `.claude/kit-backup/<old version>/` and listed |
| a file under `.claude/scripts/` the kit never shipped | moved to the backup too (with its file mode): the folder has to go, because `/init` and the doctor look for it. If your settings still run or name it, it NEEDS YOU instead |
| an `AGENTS.md`, `.cursor/rules/` or `skills/` file you edited | left where it is and listed; `adapt.py` writes the current Codex or Cursor adapter |
| `.claude/KIT_VERSION`, `.claude/KIT_MANIFEST` | removed last: the plugin's own `KIT_VERSION` is the version now |
| `CLAUDE.md` or a domain primer that is still the old starter text | replaced with the plugin's current starter file |
| `CLAUDE.md` and the primers you edited, `loop.config.md`, `.claude/rules/` | kept as they are: they are the project's. If an edited `CLAUDE.md` still names `.claude/commands/` or `.claude/scripts/`, the plan says so |
| files that are not the kit's (your own agent, skill, rule or hook script), outside `.claude/scripts/` | never looked at |
| `.claude/settings.json` | the kit's hook entries are taken out; your own hooks and everything else stay. `/init`'s part adds the marketplace and `enabledPlugins`, so teammates are asked to install the plugin |
| `.claude/settings.local.json` | the same: the kit's hook entries are taken out, after a backup to `settings.local.json.pre-migrate` |

A hook entry is the kit's when it runs one of the copied kit hook scripts under `.claude/scripts/` (`guards/run.py`, `session_start.py`, `loopctl/compact_hook.py`, `worktree_hook.py`), a v1 guard (`.claude/hooks/guard-git.sh`, `guard-main-edit.sh`), or `${CLAUDE_PLUGIN_ROOT}/scripts/...`. A matcher group, an event or the `hooks` key left empty is dropped. The plugin's `hooks/hooks.json` supplies the guards now; a copy in `settings.json` would run them twice.

The kit's modes are plugin skills now: `/chain-of-custody:<mode>`. An edited command file is backed up, not kept.

After the file part, migrate does what `/init` does (`init_project.py`), on the settings without the kit's hooks. `--option KEY=VALUE` passes a `userConfig` choice, as for `init_project.py`.

## How the kit's files are recognised

Your install has no kit history, so the hashes come from two places, and both are used:

- a v2 copy's `.claude/KIT_MANIFEST`, which lists every file the copy shipped with its SHA-256;
- the v1.2.0 tree the kit ships, `migrate/v1.2.0.tar` (`git archive` of the `v1.2.0` tag).

Only kit files count. The seed files (`CLAUDE.md`, `loop.config.md`, the primers, `.claude/rules/`) and the settings files are the project's. The one exception: a `CLAUDE.md` or primer that still matches the old starter text exactly is replaced with the current one.

- A copy that is neither v1.2.x nor a v2 copy with a manifest is compared with v1.2.0. A file of yours that differs from v1.2.0 is backed up or kept, never removed, and the plan says so. Delete the backup when you have looked at it.
- `--from-version X.Y.Z` names the old version when `.claude/KIT_VERSION` is missing. It names the backup folder; without it the folder is named `unknown`.
- `--base-archive FILE` takes the hashes from a tar of your old kit tree (`git archive` of its tag) instead of v1.2.0.
- A `KIT_MANIFEST` that cannot be read stops everything. Restore it from git, or delete it to fall back on the v1.2.0 hashes.

## Install A: a fork with local edits

A project that edited agent files, added a roster, changed `CLAUDE.md`, or appended its own rules.

- An edited agent file stays as a project override, with your edits and its old frontmatter. It does not get the kit's updates, including the v2 frontmatter (`effort`, `disallowedTools`). Compare it with the kit's `agents/<role>.md` (in `$KIT`), then either put your edit on top of the kit's file or delete the override. The doctor row `plugin.overrides` lists the overrides.
- An edited `CLAUDE.md`, `loop.config.md` and your rules are kept as they are.
- The roster is the `roster` key in `loop.config.md`. It is yours and untouched. A custom agent file not in the kit stays as it is; add its role to `roster` so the guards classify it.
- A hook script you wrote yourself in `.claude/hooks/` is kept, and so is its entry in `settings.json`. Only the two v1 kit hooks go.

## Install B: an older copy, infra agent without frontmatter

An older copy can differ from v1.2.0 in many files, and may have an `infra-engineer.md` whose opening `---` block is gone.

- Every file that differs from v1.2.0 counts as edited, so expect a long backup list. The dry run shows it before anything is written. Pass `--from-version` with the old version, so the backup folder has its name.
- `infra-engineer.md` differs, so it is kept as a project override. Without frontmatter it has no `tools:` or `disallowedTools:`, so it is not restricted by the kit's contract, and it would replace the kit's agent. Delete it, or copy the kit's `agents/infra-engineer.md` into `.claude/agents/` and paste your own body edits below its frontmatter.
- If the copy was running a deploy-capable MCP server or plugin, `doctor.py` flags it at L2 and above (`/init` adds the MCP lockdown). Remove or deny it before raising the level.
- Run `/bootstrap` afterwards if the copy never had primers or a filled `loop.config.md`; it asks the product questions and leaves the technical ones to the agents.

## Plans: `build-plan.json` to `plan/`

Only needed if you want loopctl (opt-in; the prose loop in `/orchestrate` still reads v1 plans). `python3 "$KIT/scripts/loopctl" migrate-plan --check` validates the conversion and writes nothing (`--dry-run` also writes nothing). `--write` creates `plan/plan.json` and `plan/items/*.json`; `--from FILE` names a plan other than `build-plan.json`, `--name`, `--default-size {S,M}` and `--repo` adjust it, and `--force` (human only) replaces an existing `plan/`. Review the files, add `touches`, `estimate`, `verify` and `migrations` where they matter, commit, then `loopctl approve plan --by <you> --seed-v1 build-plan.json` to carry the item statuses over. An expiring waiver is created for a v1 `securityWaiver`; a human renews it. Unknown fields fail loudly and nothing is written.

## Rollback to the copy install

- Not merged yet: `git switch -` and delete the branch. Nothing outside the branch changed.
- Merged: `git revert` the migration commit.
- One file: your edited copy is in `.claude/kit-backup/<old version>/`, and the old settings are in `.claude/settings.json.pre-init` until you delete them.
- While the copy is back, turn the plugin off for that project (`"enabledPlugins": {"chain-of-custody@chain-of-custody": false}` in `.claude/settings.json`), or both sets of guards run.
- loopctl state lives in `refs/loopctl/*` and `<git-common-dir>/loopctl/`; v1.2.0 ignores both. Delete them only if you want the history gone.
