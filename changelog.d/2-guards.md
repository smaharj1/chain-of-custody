# 2-guards: guard suite v2, settings templates, corpus and evals (staged, not active)

## Changes

- `.claude/scripts/guards/`: one hook entry point (`run.py`, Python 3.9, standard library) replacing the v1 shell hooks. A manifest (`hooks.manifest.json`) drives everything; `wiring.py` generates `.claude/settings.v2.json`, the three settings templates and the plugin form of the hooks (`build_plugin_hooks`, for Phase 9). Every blocking event fails closed and is wired `onFailure: block`; the wired command exits 2 if `python3` is missing.
  - **Shell analysis** (`shellparse.py`, `invoke.py`): quotes, `$(...)`, backticks, `${...}` operators, brace expansion, `$IFS`, heredocs and here-strings, redirects, process substitution, aliases and functions; static evaluation of pure producers (`echo`, `printf`, `base64 -d`, `rev`, `tr`, `xxd -r -p`, `cat file`); a script written and run in one command is read; wrappers are unwrapped (sudo, env, command, exec, nohup, timeout, nice, xargs, find -exec, watch, script, ssh, tmux, screen, at, sandbox wrappers, poetry run, npx, docker run); `npm|pnpm|yarn|bun run` (with pre/post scripts), `make`, `just`, `turbo`, `nx` are expanded; cwd is tracked across `cd`, `pushd`, `env -C`, `git -C`, `make -C`, `npm --prefix`.
  - **Caller** (`kitlib.caller.classify`, section 5.7): engineer, critic, unknown-subagent, conductor (a registered session) or unknown (an unregistered main session). Roster = the eight kit agents plus the `roster` key. A tainted conductor at L2 and above is treated as unknown.
  - **Git** (`g_git.py`): every v1 rule, the section 5.9 escalations (push needs `delivery`, a named remote, an explicit `feat/*` refspec, an explicit lease; protected branches; force, delete, mirror, tags; `-c` config keys and `GIT_*` environment; aliases; `--no-verify` abbreviations; tree-switching with `reset --hard`, `clean`; `fetch` into a protected ref) and `gh` (API writes, release, secret, workflow, repo, ruleset, auth; `gh pr merge` always denied). Tiers: 0 read only, 1 stage and branch, 2 commit and merge into a non-protected branch, 3 conductor.
  - **File** (`g_file.py`): main-edit (worktree-aware), guardrail paths (`.claude/settings*.json`, `.claude/hooks`, `.claude/scripts`, `.git/hooks`, `.git/config`, `.mcp.json`, CI, CODEOWNERS), credential files, self-modification (H6), links to protected files, write targets of about 60 commands and of inline code in python, node, perl, ruby, php, awk, vim, ex and emacs, archive and directory landings, find name patterns, persistence (cron, launchctl, systemctl), MCP path arguments.
  - **Infra** (`g_infra.py`): verb classifier (plan/validate allowed, apply/destroy/deploy/delete denied) for terraform, cdk, pulumi, kubectl, helm, sam, serverless, aws, gcloud, az, docker and the deploy CLIs.
  - **Exec** (`g_exec.py`): `claude` and the Agent SDK denied everywhere (CS-2), `loopctl` write subcommands only from the conductor (RC-13), `Workflow` denied headlessly (RC-17), a NUL-byte pre-check and an oversize check.
  - **Critic write-guard** (`g_critic.py`), **HALT** (`g_halt.py`), **ConfigChange blocker** (`g_config.py`), **injection flag and taint** (`g_taint.py`; PostToolUse context, taint file under `<git-common-dir>/loopctl/taint/`).
  - **Telemetry** (`telemetry.py`): per-`agent_id` JSONL under `<git-common-dir>/loopctl/events/`, redacted, no hash chain. **Self-test** (`selftest.py`, 15 cases, `run.py --selftest [--record]`).
  - **CLI helpers**: `run.py explain <command>`, `register-session`, `unregister-session`, `set-halt`, `clear-halt` (the last four refuse when `CLAUDECODE` is set).
- `.claude/settings.v2.json` (staged, IMP-13) and `.claude/templates/settings/`: `interactive`, `headless-child`, `critic` templates, `deny-baseline.json`, README. Named scripts only in `allow`; `Workflow`, `claude`, pushes to main, `--no-verify`, infra mutators, `.env` reads denied.
- Doctor: `.claude/scripts/doctorlib/guardchecks.py` registers `guards.manifest`, `guards.wiring`, `guards.on-failure`, `guards.selftest`, `guards.deny-drift` and a `guards` level cap (L0 on a failed self-test, L1 when unwired). `doctor.py` gets a one-line import change.
- Config schema `kitlib/schema/2-guards.json`; `.claude/loop.config.md` regenerated.
- `docs/security/guards.md`: reference with all rule ids and a "What the guards cannot see" section.
- Corpus (`tests/guards/corpus/`, about 1,400 rows): git, file, infra, exec, critic, callers, five adversarial rounds, a false-positive (allow) corpus of ordinary dev commands, and an injection corpus. Every deny message carries a stable rule id (IMP-22) and a coverage test checks that every rule id has a row.
- Evals: 20 synthetic cases in `evals/cases/`, transcripts in `evals/transcripts/`, `tools/evals/run_local.py` (deterministic graders including `guard_replay`, which re-runs the real guards over the recorded decisions), `tools/evals/make_synthetic.py`. Live runs are pending-human.
- Tests: `tests/guards/*`, `tests/doctor/test_guard_wiring.py`, `tests/test_evals.py`. Each guard module runs under 10 s (corpus 7 s, hooks 6 s); a fuzz run over 240k generated commands produced no crash.

## Upgrade impact

Staged only. Nothing is active until the release stage merges the hooks block of `.claude/settings.v2.json` into `.claude/settings.json` and removes the v1 hooks in one reviewed commit (IMP-13). `.claude/settings.json` and `.claude/hooks/*` are untouched. `.claude/loop.config.md` is regenerated with four new keys. After activation, edits to `.claude/scripts/**` and `.claude/context` become H6 (a human, or kit-dev mode, edits them).

## New config keys

All owner phase `2-guards`, all H6-protected, none evidence-gated. Section "Guards".

- `roster`: map `role: engineer | critic`, default `{}`.
- `protected_branches`: list, default `[main, master]`.
- `integration_branch`: string, default empty.
- `main_edit_allow`: list of globs, default `[]`.

Read, not registered here: `delivery` (`local | push-branch | draft-pr | pr-automerge`), `git_autonomy` (`none | branch | branch+commit`), `merge_policy`. Phase 3 registers them and must H6-protect them with these values.

## Pending-human actions

- Activate the guards (IMP-13): merge the hooks block of `.claude/settings.v2.json`, remove the v1 hooks, run `python3 .claude/scripts/guards/wiring.py --check` and `python3 .claude/scripts/doctor.py`, then `python3 .claude/scripts/guards/run.py --selftest --record`.
- Register the prose-loop conductor: `python3 .claude/scripts/guards/run.py register-session --role conductor --session <id>` from a plain terminal (loopctl does this itself later).
- Verify V9 on the pinned CLI: that `onFailure: block` is honoured and where it goes in the hook entry (the guards also fail closed by themselves).
- Re-run the hello-loop against the live hooks and the live evals (`claude plugin eval`); the synthetic evals prove only the graders and the replay.
- Delete the stray file `.git/loopctl/events/s-s1.jsonl` in the main checkout (an early manual run created it; the sandbox refused the delete).
- Review `docs/security/guards.md` "What the guards cannot see" and decide whether any gap needs a platform control.

## Docs impact

- README.md: the hook-file listing (`guard-git.sh`, `guard-main-edit.sh`, `guard-infra.sh`, `guard-exec.sh` become `.claude/scripts/guards/`); link `docs/security/guards.md` from `docs/security/README.md`.
- docs/getting-started.md and docs/faq.md: "Why was my command blocked?" (rule id, `run.py explain`), "How do I register the conductor?"
- GLOSSARY.md: guard, rule id, caller kind, taint, HALT, corpus, `guard_replay`.
- AGENTS.md: the safety-hooks row. `engineer-protocol` section 8 and the `enforce_engineer_git_hook` documentation mention the old hook files.
- CHANGELOG.md: "Added" entry for the guard suite, templates, corpus and evals.

## Decisions made

- **Guardrail, not boundary.** The suite is documented as a typo guard. It cannot see code in files it did not read, imports it does not follow, tree-switching git operations (`read-tree -u`, `checkout <rev>`, `reset --hard`, merge) that overwrite guarded files, `git ls-files | xargs rm`, `defaults write`, races between hook and execution, pip install scripts, or archive and patch contents that do not exist at hook time.
- **One entry point, manifest-driven.** No `if:` filters (unverified), no Stop hook, one `run.py` that dispatches by event.
- **Fail closed.** Any exception on a blocking event exits 2; unknown shapes (dynamic command names, unresolvable `cd`) are asked or denied, never allowed.
- **Conductor registration is a human act** (or loopctl later). An unregistered main session is `unknown`, least privilege.
- **`git_autonomy` semantics.** `none` makes an unknown session read-only; `branch` allows stage and branch; `branch+commit` allows commit and merge into a non-protected branch.
- **Conductor push** needs `delivery` in `push-branch | draft-pr | pr-automerge`, a named remote, an explicit `feat/*` refspec, and an explicit `--force-with-lease=<ref>:<sha>` if a lease is used. `gh pr merge` is always denied in this build.
- **`never-run` rule id** covers the external-program and server git subcommands (gui, daemon, difftool, mergetool, send-email, svn, p4).
- **Echo is treated as escape-interpreting** (zsh-like), the more dangerous reading.
- **Agent-authored script files are scanned** (untracked or modified); tracked and unchanged ones are trusted.
- **`.claude` protection is anchored** to `~/.claude` or a git tree, so a stray folder elsewhere is not a false positive.
- **Wiring groups reuse the doctor's "settings" group** and the `guards` cap is appended to `levelcalc.CAPS` without rewriting doctor core.
- **Telemetry** is a JSONL file per `agent_id`, values redacted, no hash chain (it is not evidence).
- **Eval transcripts name the project `/repo`;** `guard_replay` rewrites it to a scratch git tree.

## Conductor: pointers to add

- `docs/security/README.md`: link `guards.md`.
- Phase 3: register `delivery`, `git_autonomy`, `merge_policy` as H6-protected, using the values above.
- Phase 4a (loopctl): `loopctl begin` writes the session registry under `<git-common-dir>/loopctl/sessions/`; `locked_paths.json` (read by `g_file`), `taint/`, `selftest.json` and `events/` are contracts the guards already read or write.
- `probe-assumptions.md`: update V1 (agent-blind hook input) and V9 (`onFailure`) with the unverified wording used here.
- Phase 9: use `wiring.build_plugin_hooks` for the plugin `hooks.json`.
- Release stage: switch `.claude/settings.json` to the v2 hooks and delete `.claude/hooks/guard-*.sh` in the same commit.
