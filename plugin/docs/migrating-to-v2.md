# Migrating a v1.2.0 install to 2.0.0

For a project that copied `.claude/` from kit v1.2.0 (or an older copy) and wants 2.0.0-rc.1. The upgrade is one script, `${CLAUDE_PLUGIN_ROOT}/scripts/migrate.py` (the `/migrate` skill walks you through it). It works offline, never overwrites a file you changed, and does nothing until you pass `--apply` from a plain terminal.

`${CLAUDE_PLUGIN_ROOT}/docs/packaging.md` has the full reference. This page is the order of steps and the cases that need a decision.

## Before you start

- Work on a branch with a clean `git status`: `git switch -c upgrade-kit-2`. `--apply` refuses a dirty tree, and the branch is your rollback.
- Get the new kit next to your project: `git clone <the kit> ../kit` (or unpack a release). Nothing is downloaded by the script.
- Read the 2.0.0-rc.1 section of `CHANGELOG.md`, Upgrade impact first.
- Note which config keys you rely on. `.claude/loop.config.md` is never touched; keys added since use their defaults.

## Steps

1. **Dry run.** `python3 ../kit/.claude/scripts/migrate.py --repo .` prints the plan and changes nothing. Read CONFLICT, MERGE, UPDATE, REMOVE and KEEP-RETIRED first; ADD is long (new scripts, specs, docs).
2. **Apply.** From a plain terminal, not inside a Claude Code session: `python3 ../kit/.claude/scripts/migrate.py --repo . --apply`. It copies every file it replaces or removes to `.claude/.migrate/backup/<time>/` first. `KIT_VERSION` and `KIT_MANIFEST` move last, and only when no conflict is open, so you can simply run it again.
3. **Conflicts.** For each one your file is left alone and the merge result with markers is in `.claude/.migrate/conflicts/<path>`. Edit your file, then `migrate.py --repo . --resolved <path>`; or take the kit's version with `--take-kit <path>` (your copy is backed up). Re-run the dry run until it lists nothing open.
4. **Switch the settings.** `.claude/settings.json` is never changed by the migration (it is a guardrail, human-edited). Follow [The v2 guards are on by default](packaging.md#the-v2-guards-are-on-by-default) and do it in the same commit as the migration. The v1 hooks `guard-git.sh` and `guard-main-edit.sh` are retired in 2.0.0; the migration keeps them while your old `hooks` block still names them, so run it again after the switch to remove the copies you never changed.
5. **Add the settings a copy install needs.** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_project.py` (dry run) shows the deny block, sandbox profile and MCP lockdown it would add to `settings.json`; `--apply` writes them, additively, from a plain terminal.
6. **Check.** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py` (expect `L1` until you attest the boundary: `doctor.py --level` says what blocks the next level) and `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/guards/wiring.py --check`. Start a new session and look for the `kit session_start: canary` line; none means the hooks are not firing.
7. **Commit.** Delete `.claude/.migrate/` first, then commit.

## What never gets overwritten

| Yours | Rule |
|---|---|
| `.claude/settings.json` | added if missing, otherwise never changed |
| `.claude/loop.config.md`, `CLAUDE.md`, the domain primers, `.claude/rules/` | replaced only if still byte-identical to the v1.2.0 template; otherwise yours |
| any kit file you changed | merged by `git merge-file` when the kit changed it too; kept when only you changed it |
| any kit file you deleted | stays deleted |
| files that are not in the kit (your own agent, skill, rule) | never looked at |
| a retired file you changed (an edited `.claude/commands/*.md`) | kept and listed; move your edit into the skill of the same name, then delete it |

## How forks are detected

Your install has no kit history, so each file is compared with two trees: the base, which is the v1.2.0 tree from `.claude/migrate/v1.2.0.tar` in the kit (picked by the version in your `.claude/KIT_VERSION`), and the new kit. Per file:

- same as the base: replaced by the new one;
- different from the base, kit unchanged: kept;
- different from the base and the kit changed it: three-way merge; a clean result is written, overlapping edits become a conflict.

If `.claude/KIT_VERSION` is missing or says something the kit has no archive for, the script stops and names the archives it has. Pass `--from-version 1.2.0` if your copy is a renamed or edited v1.2.0, or `--base-archive FILE` for a tar you made from your own base.

## Install A: a fork with local edits

A project that edited agent files, added a roster, changed `CLAUDE.md`, or appended its own rules.

- Edits in places the kit did not change (an appended section, a changed `CLAUDE.md`) merge cleanly or are kept. The kit's new frontmatter (`effort`, `disallowedTools`) arrives in the same file.
- A conflict appears only where you and the kit changed the same lines, typically an agent's `tools:` line or its opening paragraph. Resolve in your file with the marked copy open, or `--take-kit` and re-apply your edit on top.
- The roster is the `roster` key in `loop.config.md`. It is yours and untouched. A custom agent file not in the kit stays as it is; add its role to `roster` so the guards classify it.
- A v1 hook you wrote yourself in `.claude/hooks/` is kept (you changed or added it); only the two kit hooks are retired.

## Install B: an older copy, infra agent without frontmatter

An older copy can differ from v1.2.0 in many files, and may have an `infra-engineer.md` whose opening `---` block is gone.

- If its `KIT_VERSION` has no archive, use `--from-version 1.2.0`. The base is then wrong for the files that changed between your copy and v1.2.0, so expect more conflicts than Install A. The dry run shows the count before anything is written.
- Without frontmatter an agent has no `tools:` or `disallowedTools:`, so it is not restricted by the kit's contract. The kit's file has it. Expect a conflict at the top of that file: run `migrate.py --repo . --take-kit ${CLAUDE_PLUGIN_ROOT}/agents/infra-engineer.md` (your copy is backed up), then paste your own body edits from the backup below the frontmatter.
- If the copy was running a deploy-capable MCP server or plugin, `doctor.py` flags it at L2 and above (`/init` adds the MCP lockdown). Remove or deny it before raising the level.
- Run `/bootstrap` afterwards if the copy never had primers or a filled `loop.config.md`; it asks the product questions and leaves the technical ones to the agents.

## Plans: `build-plan.json` to `plan/`

Only needed if you want loopctl (opt-in; the prose loop in `/orchestrate` still reads v1 plans). `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/loopctl migrate-plan --check` validates the conversion and writes nothing (`--dry-run` also writes nothing). `--write` creates `plan/plan.json` and `plan/items/*.json`; `--from FILE` names a plan other than `build-plan.json`, `--name`, `--default-size {S,M}` and `--repo` adjust it, and `--force` (human only) replaces an existing `plan/`. Review the files, add `touches`, `estimate`, `verify` and `migrations` where they matter, commit, then `loopctl approve plan --by <you> --seed-v1 build-plan.json` to carry the item statuses over. An expiring waiver is created for a v1 `securityWaiver`; a human renews it. Unknown fields fail loudly and nothing is written.

## Rollback to v1.2.0

- Not merged yet: `git switch -` and delete the branch. Nothing outside the branch changed.
- Merged: `git revert` the migration commit. Backups of every replaced or removed file are also in `.claude/.migrate/backup/<time>/` until you delete that folder.
- Fresh v1.2.0 copy: `git archive v1.2.0 .claude | tar -x -C <project>` from a kit checkout that has the tag.
- Per file: `migrate.py --take-kit` backed it up; copy the backup over.
- loopctl state lives in `refs/loopctl/*` and `<git-common-dir>/loopctl/`; v1.2.0 ignores both. Delete them only if you want the history gone.
