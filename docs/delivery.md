# Interactive PR delivery

How a feature built in the interactive lane (`/pm`, `/architect`, `/tech-lead`) leaves your machine as a draft pull request, with the audit trail inside the PR. Nothing here is the autonomous loop: that comes later, with `loopctl`. Nothing here pushes: **you** run the last two commands.

The agent-facing version of this page is `.claude/context/delivery.md`. This page is for the person who installs and runs it.

## What you get

```
/tech-lead  ->  branch feat/<slug>  ->  one WIP commit per engineer  ->  code review on each
            ->  verify.py (build, lint, typecheck, tests, smoke; definition from the base sha)
            ->  pr_prepare.py init + fill  ->  pr_prepare.py prepare
            ->  YOU run:  git push ...   and   gh pr create --draft --body-file ...
            ->  CI: verify (untrusted), optional claude-review (trusted)  ->  CI-fix if red (max 2)
            ->  YOU merge (squash, "PR title and description")
```

Three dials in `.claude/loop.config.md`. All are H6-protected and read at the base sha, so a feature branch cannot loosen them.

| Key | Default | Set it to |
|---|---|---|
| `git_autonomy` | `none` | `branch+commit` to let the Tech Lead create the branch and the WIP commits |
| `delivery` | `local` | `draft-pr` once your rulesets and required checks are in place (`python3 .claude/scripts/doctor.py --level`) |
| `max_ci_fix_attempts` | `2` | leave it |

With the defaults nothing changes from v1.

## Setup, once per project

1. Fill the verify commands in `.claude/loop.config.md`: `verify_tests` (must exit 0 and collect more than zero tests) and, if you have them, `verify_build`, `verify_lint`, `verify_typecheck`. List the stages you will not let anyone skip in `verify_require` (default: `tests`). **Commit this to your default branch first**: verify reads it from there.
2. For a smoke test, choose one:
   - `verify_run`: a harness command that boots the app, asserts a route and exits (the pattern in `.claude/context/local-dev.md`). The demo route is appended as its last argument, or substituted for `{route}`.
   - `verify_dev`: a dev-server command that never exits. verify.py starts it in its own process group, polls `http://127.0.0.1:<first verify port><verify_ready_path>`, requests the demo route, and kills the group. Needs `verify_ports`.
   Set `verify_ports` to dedicated ports (not your dev defaults). A busy port is reported as an environment failure and never retried.
3. Ignore the directory where PR bodies and saved verify results live (the kit's own repo does too):
   ```bash
   echo '.claude/pr/' >> .gitignore
   ```
4. Copy the workflows you want into `.github/workflows/`: `verify.yml` (recommended; make the job `verify` a required check, pinned to the GitHub Actions app) and, optionally, `claude-review.yml`. Pin each `uses:` to a commit SHA. `python3 .claude/scripts/ci_lint.py` must stay free of errors. `docs/security/split-ci.md` explains why they look the way they do.
5. For `claude-review.yml`: set the secret `ANTHROPIC_API_KEY` (spend-capped workspace) and the variable `CLAUDE_CODE_VERSION` (the version your canary passed). A maintainer comments `/review` on a PR to run it. The CLI flags in the file are the ones the report names; check them once against your pinned version.
6. The first PR that installs the kit cannot pass `verify` (the base has no `verify.py` yet). A human merges that one; every later PR is protected.

## verify.py

```
python3 .claude/scripts/verify.py [--base REV] [--item ID --items-file FILE] [--route /path]... [--json] [--keep-going] [--only STAGES]
```

Stages, in order: `build`, `lint`, `typecheck`, `item-tests`, `tests`, `smoke`. Each runs in its own session under an allowlisted environment (`PATH`, `HOME`, locale and toolchain locators; no token, key or cloud variable of your shell), and its whole process group is killed afterwards. A process that outlives its stage is reported as a leak; a verify port still bound after the smoke stage is a leak too.

Where the definition comes from:

- the commands and ports: `.claude/loop.config.md` **at the base sha** (the merge-base with your default branch, or `--base`);
- per-item commands: an items file shaped like `examples/hello-loop/verify.json` (`{"items": {ID: {"tests", "demo_route", "ac_ids"}}}`), read at the base sha when it is inside the repository. Plan items from `loopctl` will supply the same fields later.

Exit codes: `0` pass, `1` a stage failed, `2` the definition is unusable (a required stage has no command, the base has no config, a bad route). `--apply-gate` honours `gates.g6`: `off` skips, `report` never fails the exit code. CI uses it.

Result, schema `verify.v1` (`--json`). The fields `loopctl` and CI read:

| Field | Meaning |
|---|---|
| `status` | `pass`, `fail`, `error` (definition problem), or `skipped` (gate off) |
| `ok`, `complete` | `ok` is true only for a full pass; `complete` is false after `--only` |
| `failure_class` | `none`, `config`, `env`, `build`, `lint`, `typecheck`, `tests`, `smoke`, `timeout`, `leak`. `config` and `env` mean the code is not at fault: do not retry an engineer |
| `base`, `head`, `dirty`, `config_source`, `trusted` | what was verified and where the definition came from (`trusted: false` only with `--allow-head-config`) |
| `stages[]` | `name`, `command`, `status`, `exit_code`, `duration_s`, `tests_collected`, `skipped_tests`, `reason`, `output_tail` (redacted) |
| `findings[]` | `id`, `severity`, `message` (for example `verify.config-drift`, `verify.leaked-process`, `verify.port-leak`) |
| `log` | path of the redacted full log under `<git-common-dir>/loopctl/verify/` |

It does not parse acceptance criteria (G1, G3), compare skip counts against the base, or check test-file integrity (G5). Those arrive with `loopctl`'s gates; it will wrap this script and keep this result shape.

## pr_prepare.py

```
python3 .claude/scripts/pr_prepare.py init      # write .claude/pr/<slug>.md from the template, pre-filled
python3 .claude/scripts/pr_prepare.py check     # validate the body and the git state
python3 .claude/scripts/pr_prepare.py prepare   # check, then print the commands for your delivery setting
python3 .claude/scripts/pr_prepare.py reverts --sha <commit>... | --prs-json FILE --pr N
```

It runs read-only git only and never runs `gh`. When `prepare` is clean it prints, for `delivery: draft-pr`:

```
git push --set-upstream origin feat/<slug>
gh pr create --draft --base main --head feat/<slug> --title '...' --body-file .../.claude/pr/<slug>.md
```

When the branch is already pushed it prints a plain `git push origin feat/<slug>` and, with `--pr N`, `gh pr edit N --title ... --body-file ...`. It never prints a force push. With `delivery: local` it prints `git switch main` and `git merge --no-ff feat/<slug>`; with `push-branch`, only the push. Run them in your own terminal, or in the session with `!` at L0 and L1.

It refuses (exit 1, no commands) for: an unfilled `<fill: ...>` marker or empty audit field; Verify not PASS; an engineer or code review that is not approved; an acceptance criterion without evidence and PASS; Doc Delta or Primer Delta written as NONE without a `checked:` line, or dropped from an engineer report; a stale `Head sha`; an uncommitted tree; a saved verify result that is not a complete, trusted pass for this exact HEAD; a diff touching a path a human must review (migrations, `.github/`, `.claude/`, lockfiles, test and CI config, deploy files, plus `always_human_paths` from the base config) while the body says NONE; more CI-fix commits than `max_ci_fix_attempts`, or one that edits workflows, tests or lockfiles; a remote branch the local one has diverged from; a protected or malformed branch name; approver-only labels (`factory:*`, `plan-ok`, `preview`); a secret-shaped string in the body.

Result, schema `pr_prepare.v1` (`--json`): `ok`, `branch`, `base`, `base_sha`, `head_sha`, `delivery`, `title`, `ahead`, `human_gated[]`, `ci_fix{count,max,commits}`, `reports[]`, `findings[]` (`id`, `severity`, `message`), `commands[]` (only when `prepare` is clean). Importable: `pr_prepare.validate_body`, `parse_body`, `parse_report`, `classify_paths`, `find_reverts`, `reverts_from_prs`. `loopctl` reuses the audit-block validation.

## The audit block

`.claude/templates/pr-body.md` is the template. The block records, per PR: feature, lane, delivery, branch, base and head shas, design-critic verdict, the verify run, security review, human-gated paths, CI-fix commits, waivers and the CI evidence link, then the engineers table, the acceptance matrix, Doc Delta, Primer Delta and notes. The PR body is the durable record (DEC-10): squash merges drop commit trailers, so set the repository's squash message to "PR title and description" (`docs/security/rulesets.md`).

## Limits

- This is not autonomy level L2. The push is yours, with your own credentials; the agent holds none (`docs/security/credentials.md`).
- Nothing reads GitHub. CI results, review comments and revert PRs reach the Tech Lead because you paste them (or save `gh` output you ran yourself and pass it with `--prs-json`).
- The saved verify result and the engineer reports are files in your working tree: they catch mistakes and stale work, not a determined forger. The evidence that counts is the CI check run on the PR (DEC-10); the audit block links it once the PR exists.
- `verify.py` cannot make a test runner honest. A runner whose output it cannot read gets a `verify.test-count-unknown` warning, not a block.
- Windows is not supported (process groups).
