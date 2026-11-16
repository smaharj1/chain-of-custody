# 14-plugin-only: the repository root is the plugin; one install form; agents may push feature branches; 2.0.0

## Changes

- **The repository root is the plugin.** The kit moved out of `.claude/` to the root (`agents/`, `skills/`, `hooks/`, `scripts/`, `context/`, `spec/`, `templates/`, `seed/`, `migrate/`, `KIT_VERSION`); `.claude-plugin/marketplace.json` has source `./`. The generated `plugin/` and `dist/` trees, `tools/build-dist.py`, `tools/distlib/` and `tests/dist/test_build.py` are gone. The offline validator moved to `tools/pluginlib/validate.py`.
- **Kit paths.** Kit prose names kit files as `${CLAUDE_PLUGIN_ROOT}/...`; scripts find them through `scripts/kitlib/kitpaths.py`; commands the kit prints use absolute paths. Human docs name a command a person runs as `python3 "$KIT/scripts/<x>"`, where `$KIT` is the folder the plugin is installed in (`/chain-of-custody:init` and the doctor print it).
- **Codex and Cursor.** `scripts/adapt.py --target <project> [--tool codex|cursor]` writes a guidance-only copy into the project (`.chain-of-custody/`, `AGENTS.md`, `.cursor/rules/chain-of-custody.mdc`); `adapters/SETUP.md` says what it does not enforce. The root `AGENTS.md`, `.cursor/` and the Codex `skills/chain-of-custody/` are gone.
- **Kit development.** This repository's `.claude/` holds only a kit-development `CLAUDE.md` and a `settings.json` that turns the plugin off, so the kit's guards do not block edits to the kit. `tools/check-refs.py` resolves example-project paths under `examples/`. `tools/validate-plugin-real.sh` validates `.`.
- **Agents may push their own feature branch.** New `git_autonomy: branch+commit+push`: with `delivery: push-branch` or `draft-pr`, set in the working tree and at the base sha, the main session (the conductor, or an unregistered session) may run a plain `git push [-u] <remote> <branch>` of one local branch that is not protected (`protected_branches`, `integration_branch`, main, master, the remote's default branch), and `gh pr create --draft` under `draft-pr`. Force, leases, tags, deletes, mirrors, URLs, push options, `git lfs push` and a tainted session stay denied; subagents never push; `gh pr ready` and `gh pr merge` stay human. New guard rules `guard-git:push-config`, `push-option`, `gh-pr-create` and `guard-exec:kit-copy`; corpus files `push.json` and `kit.json`. `pr_prepare.py prepare` prints `# CONDUCTOR STEPS` at the push tier and `# HUMAN STEP` below it.
- **The starter config pushes.** The registry gained a `seed` field (the value the starter config writes when it differs from the default). `/chain-of-custody:init` writes `git_autonomy: branch+commit+push` and `delivery: push-branch`; a project that leaves the keys out keeps `none` and `local`.
- **Guards from the plugin only.** `hooks/hooks.json` wires the guards; no settings template carries hooks. `wiring.py --check --project <dir>` and the doctor row `guards.wiring` report a double wiring, `disableAllHooks`, or the plugin turned off. The `kit` path category protects the installed plugin (`~/.claude/plugins`) and, in a checkout of the kit, its run areas.
- **Migrate is safer.**
  - Only the kit's own hook scripts count as kit hooks. A team script under `.claude/scripts/` that settings still run or name is NEEDS YOU (move it, fix the reference); other team scripts are backed up with their file mode.
  - A file under a symlinked folder is NEEDS YOU; a symlink outside `.claude/` is left in place.
  - Every destination is checked before the first write. The order is settings and the `/init` files, then the moves, then `KIT_VERSION` and `KIT_MANIFEST` last, so a stopped run can be run again.
  - The kit's hooks come out of `.claude/settings.local.json` too, after a backup to `settings.local.json.pre-migrate`.
  - `CLAUDE.md` and primers still exactly the old starter text are replaced (UPDATE); `loop.config.md` never is.
  - The printed apply command repeats `--from-version`, `--base-archive` and `--option`.
  - Old Codex/Cursor adapters get the `adapt.py` command.
- **Init.**
  - The printed apply command carries every choice, including those from `CLAUDE_PLUGIN_OPTION_*`.
  - `--repo` inside a git repository means its root.
  - A choice the config already has is not written again.
  - The settings it writes add `extraKnownMarketplaces` and `enabledPlugins`, so a teammate who trusts the folder is asked to install the plugin; a project's own `false` is kept.
- **adapt.py.**
  - Kit scripts are named "Claude Code plugin only" with a link instead of being run from URLs; the guide covers verify and deliver by hand. Only Markdown is rewritten, and the settings templates are left out.
  - It seeds `project/PROJECT.md` and a `loop.config.md` at `none`/`local`. The guide gains the PM posture, a push rule, and parity rows for the push tier and per-role tool limits.
  - It refuses symlinks (even with `--force`), keeps a `.prev` copy of a hand-edited file, and reports a stale file after `--tool` changes.
  - On update it reports seed files that changed (hashes in `VERSION`, `project-seed/`); relative links in the copy resolve.
- **Push-tier guard holes closed.**
  - git's dashed binaries (`git-core/git-push`, `git-lfs`, `git-send-pack` and the rest) are checked as `git <sub>`; the transport plumbing (send-pack, http-push, fetch-pack, remote-http and others) is never run.
  - A command that runs git, gh or hub may not set HOME, XDG_CONFIG_HOME, PREFIX or GH_REPO by any route (a prefix assignment, `env`, `export`, `declare`, `read`, `printf -v`). The guard reads git config with its own HOME.
  - A push without a colon is refused when the branch tracks a different branch; push `<b>:<b>`.
  - No local branch may be named HEAD, `<remote>/...` or `refs/...`; base refs are fully qualified.
  - The push lanes are feat/, quick/, fix/, chore/ and docs/, the same as pr_prepare, and a test keeps them in step. `--no-progress` is allowed.
  - Copying kit code out is refused for stream programs that write a file (`cat`, `dd`, `tee`, `sed`, `gzip` and others), gtar, bsdtar, 7z and `git clone` of the kit.
- **Plugin packaging.**
  - `intake-triage`'s description is valid YAML again; `validate-plugin` rejects an unquoted frontmatter value with `: ` or ` #`.
  - `validate-plugin --strict` passes: human docs may name old paths, and their `${CLAUDE_PLUGIN_ROOT}/...` refs are still checked.
  - Every generated hook command runs `python3 -B`, so hooks write no `__pycache__` into the plugin folder.
  - SubagentStart tells each subagent the kit folder and how to read `${CLAUDE_PLUGIN_ROOT}` in kit files.
  - The notify hook template names the kit by absolute path and always exits 0, so a moved kit no longer denies permission prompts.
  - The managed-settings example is generated from the guard manifest and carries the full guard wiring (`allowManagedHooksOnly` turns plugin hooks off). The doctor errors when such a file leaves a guard unwired, and no longer calls it double-wired.
  - `run.py explain` reads the repository's `loop.config.md`, and says when it falls back to the defaults.
- **pr_prepare reads the base sha.** Who runs each step follows `git_autonomy` and `delivery` at the base sha, not the `--delivery` override: an override to `draft-pr` over a base `push-branch` prints `gh pr create` under its own `# HUMAN STEP`, and over a base `local` the whole list is human; the JSON gains `base_delivery`.
- **The seed scans clean.** The `seed/.claude/context/local-dev.md` comment no longer reads as a secret assignment (a fresh `/init` gave a false `secrets.in-kit-files` ERROR); a test scans every file under `seed/`.
- **Delivery prose matches the push tier.** `context/delivery.md` covers `branch+commit+push`, the `gh pr create --draft` exception, and running `# CONDUCTOR STEPS` versus printing `# HUMAN STEP`; `docs/delivery.md`, the README, the glossary, `docs/security/guards.md`, `docs/bootstrap.md`, the bootstrap skill and the generated config say the key-absent default is `none`/`local` and that `/init` starts at `branch+commit+push`/`push-branch`.
- **Doctor rows for the push tier.** `delivery.suggest-draft-pr` also fires at `push-branch`; the new `delivery.push-tier` row reports `git_autonomy` and `delivery` at the base sha and WARNs when the push tier is on and `bot.kind` is `none` (the push then uses the person's credential).
- **`ci.kit-placeholder`.** `ci_lint` errors on a literal `{{KIT_` left in a file in `.github/workflows` (the templates keep theirs); `docs/delivery.md` and `docs/security/split-ci.md` say the setup script fills KIT_REF and KIT_SHA, or how to fill them by hand.
- **Printed commands.** The `--live` remediations in `creds.py` and `levelcalc.py` print the full `python3 <kit>/scripts/doctor.py --credentials --live`; the generated config's comments name kit scripts as `$KIT/scripts/...`, not `.claude/scripts/...` or `${CLAUDE_PLUGIN_ROOT}`.
- **`doctor.py --level` names what fails.** The count line lists the failing error and warning ids (up to 12 each).
- **Version** 2.0.0 in `KIT_VERSION`, `plugin.json` and `marketplace.json`; `claude_code_tested_max` stays `none`.

## Upgrade impact

- **Breaking: the copy install is gone.** A project with a copied `.claude/` installs the plugin and runs `/chain-of-custody:migrate`, which removes the kit files it never changed and keeps the project's own files (`docs/migrating-to-v2.md`).
- **Breaking: kit paths moved.** Anything of yours that ran `.claude/scripts/...` runs `python3 "$KIT/scripts/..."`.
- Codex and Cursor users run `scripts/adapt.py` from a clone of the kit at a release tag.

## New config keys

None. `git_autonomy` gained the value `branch+commit+push`.

## Pending-human actions

1. `sh tools/validate-plugin-real.sh`, then `claude --plugin-dir .` in a scratch project (probe V13: hooks fire, `${CLAUDE_PLUGIN_ROOT}` expands, `/chain-of-custody:init` is the kit's).
2. Tag `v2.0.0` and push the tag.

## Docs impact

Done in this branch: README, GLOSSARY, getting-started, faq, packaging, migrating-to-v2, v2-pending-human and the security docs.

## Decisions made

- **One install form.** Two forms doubled every path rule and the generated trees drifted; the plugin is the form Claude Code supports.
- **Not renamed.** The plugin stays `chain-of-custody`.
- **Agents push feature branches; `main` stays human.** Supersedes "agents never push" (`docs/design/v2-implementation-decisions.md`). The guard holds the push to one shape; the server-side rules (rulesets on protected branches) are the boundary. Merging, the remote lock and claims stay human at every setting.

## Conductor: pointers to add

None.
