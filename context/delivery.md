# Delivery (interactive lane): branch, commit, verify, audit block, push

> Shared kit rule, not a project primer. Read by the Tech Lead (`/tech-lead`) and, for the design side, the Architect. Report: §16.1 Phase 3, §19.7 (trailers, CI-fix, revert detection, Doc Delta, labels), DEC-1, DEC-10, DEC-22, DEC-37, IMP-25. The autonomous `/orchestrate` loop does not use this file; with `loopctl`, an item that needs a pull request stops at `awaiting_human` and `pr_prepare.py` prints the same commands for a person to run (`${CLAUDE_PLUGIN_ROOT}/docs/delivery.md`).

## The two dials

Both live in `.claude/loop.config.md` and are read **at the base sha** (the merge-base with the default branch), so a feature branch cannot change its own delivery rules.

| Key | Values | Meaning |
|---|---|---|
| `git_autonomy` | `none` (default), `branch`, `branch+commit`, `branch+commit+push` | What **you** (the conductor) may do with git. `none`: ask the user to branch and commit, as in v1. `branch`: you create `feat/<slug>` yourself. `branch+commit`: you also make one WIP commit per engineer. `branch+commit+push` (the push tier, with `delivery: push-branch` or `draft-pr`): you also push your own feature branch and, under `draft-pr`, open the **draft** PR. |
| `delivery` | `local` (default), `push-branch`, `draft-pr` | What leaves the machine, and how. `local`: the user merges the branch into the local main; nothing is pushed. `push-branch`: the feature branch is pushed. `draft-pr`: it is pushed and a **draft** PR is opened. At the push tier you run those steps; below it the user does. |

"Default" is the value when the key is absent: `none` and `local`, which change nothing from v1. A project set up by `/chain-of-custody:init` starts at `git_autonomy: branch+commit+push` with `delivery: push-branch`: you push your own feature branch, and the user opens the PR. `delivery: draft-pr` is the next step once the repo has rulesets and required checks (`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --level` says when). Only a person changes either key, in a reviewed commit (H6).

## What never changes at any setting

- **No agent pushes `main` or any other protected branch, merges a PR, changes GitHub settings or creates tags.** Not you, not an engineer. The one push you may make is the push tier: with `git_autonomy: branch+commit+push` and `delivery: push-branch` or `draft-pr` (in the working tree and at the base sha), a plain `git push [-u] <remote> feat/<slug>` of your own feature branch, plus `gh pr create --draft` under `draft-pr`. Below that tier the guards (`${CLAUDE_PLUGIN_ROOT}/scripts/guards/`) deny `git push` and `gh pr create` for you too; do not look for a way around it. Engineers and other subagents never push. The remote lock and claims are **HUMAN STEPS** at every setting: you print them and wait.
- You never run `git rebase`, `git reset`, `git stash`, `git merge`, `git commit --amend`, `--no-verify`, a force of any kind, or `gh` against GitHub, except `gh pr create --draft` at the push tier under `delivery: draft-pr` (the line `pr_prepare.py prepare` prints). A fix is a new commit.
- Engineers stay git-read-only (engineer-protocol §8). Only you commit, and only when `git_autonomy` is `branch+commit` or `branch+commit+push`.
- `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pr_prepare.py` never pushes itself. It validates, then **prints** the commands under a header that says who runs them. `# CONDUCTOR STEPS` (the push tier): you run them yourself, in order, and report what each printed. `# HUMAN STEP` (any lower setting, `delivery: local`, editing a PR that already exists, and `gh pr create` when the base sha says `delivery: push-branch`): the user runs them, in a terminal or in this session with `!` at L0 and L1; you print each one as a line `HUMAN STEP: <command>` (the same wording the Orchestrator uses) and never run it yourself. From L2 up the push uses the bot's credential, never the user's (`${CLAUDE_PLUGIN_ROOT}/docs/security/bot-identity.md`); the human's push runs from a terminal outside the agent's session.
- Approver-only labels (`factory:*`, `plan-ok`, `preview`) are never yours to apply or request (label-guard removes them; RC-20).

## The flow, Lane A (full feature)

1. **Branch.** `git_autonomy: none`: ask the user to run `git switch -c feat/<slug>` (v1 behaviour). `branch`, `branch+commit` or `branch+commit+push`: run `git branch --show-current`; if it is `main` or `master`, run `git switch -c feat/<slug>` yourself. Never switch away from a branch that has uncommitted work. The quick lane uses `quick/<short-name>`.
2. **Per engineer, after `Status: APPROVED`, before review** (`branch+commit` and `branch+commit+push`): make one WIP commit so the reviewer sees exactly that engineer's work.
   - Stage by path: the files in the report's `## Files Changed` plus the report file itself. **Never `git add -A` or `git add .`**; check `git status --porcelain` for anything else dirty and ask. Never stage `.env*`, keys or anything the report does not name.
   - Message, with the trailers git understands:
     ```
     wip(<slug>): <engineer> iteration <n>

     Feature: <slug>
     Engineer: <engineer>
     Report: docs/features/<slug>/reports/<engineer>.md
     ```
   - The `code-reviewer` is then given the commit range `<previous WIP commit>..<this commit>` (or the base for the first engineer) as well as the file list. Review retries are new commits with the same subject and `iteration <n+1>`.
   `git_autonomy: none`: the user commits; ask for one commit per engineer and give the paths.
3. **Primer Delta, before the PR.** Apply the primer updates (tech-lead A.8 step 5) on the branch and commit them as `docs(primers): <slug>`, so the diff the reviewer and CI see is the diff that merges.
4. **Verify (G6).** Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/verify.py --json > .claude/pr/<slug>.verify.json` (make the directory; it is gitignored). Pass `--item` and `--items-file` when a plan item supplies per-item commands, `--env DATABASE_URL=...` for an ephemeral database. It runs build, lint, typecheck, the tests (which must collect more than zero), and the dev-server smoke, from the **base sha's** definition, under an allowlisted environment, each in its own process group. Read `failure_class`:
   | `failure_class` | What it means | What you do |
   |---|---|---|
   | `config` | A required stage has no command, or the base config is unusable | Stop. Tell the user which key (`verify_tests`, `verify_require`, ...). Do not edit the config yourself (H6). |
   | `env` | A verify port is busy, or a command was not found | Stop and say so. Do not resume an engineer: the code is not the problem. |
   | `build`, `lint`, `typecheck`, `tests`, `smoke` | The code failed that stage | Resume the implicated engineer by `agent_id` with the failure tail, within `max_retries_per_item`. |
   | `timeout`, `leak` | A stage hung, or left a server or port behind | Treat as `smoke`/`tests` failure; a leak is a bug in the harness or the code, not flakiness. |
   `ok: true` and `complete: true` are the only pass. A partial run (`--only`) is never evidence.
5. **Security review** if the feature touches auth, payments, uploads or user input (tech-lead A.2 note). Record the verdict.
6. **Write the PR body.** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pr_prepare.py init` writes `.claude/pr/<slug>.md` from `${CLAUDE_PLUGIN_ROOT}/templates/pr-body.md`, pre-filled with the branch, the shas, the engineers table and Doc/Primer Delta from the reports, the verify summary and the human-gated paths. Fill what is left (`<fill: ...>`): title, summary, design critic, security review, code-review verdicts, the acceptance matrix, waivers, notes. Rules:
   - Every field has a value. `NONE` is valid only where the field says so, and Doc Delta / Primer Delta need a `checked:` line beside it.
   - Every acceptance criterion in `requirements.md` is a row with **named evidence** (a test name, a command, a route) and `PASS`. The `code-reviewer` checks the matrix; you do not grade your own homework.
   - Do not put a secret, a token or a production URL anywhere in the body.
7. **Prepare.** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pr_prepare.py prepare`. It refuses (exit 1, no commands) while anything is wrong: a stale head sha, an uncommitted tree, a failing or stale verify result, an unapproved report, a "NONE" over a diff that touches a human-gated path, a CI-fix commit over the cap. When it is clean it prints, for `draft-pr`:
   ```
   git push --set-upstream origin feat/<slug>
   gh pr create --draft --base main --head feat/<slug> --title '...' --body-file .claude/pr/<slug>.md
   ```
   What you do follows the header above each group of lines, which `pr_prepare.py` decides from `git_autonomy` and `delivery` at the base sha:
   - `# CONDUCTOR STEPS`: run the lines in order yourself and report what each printed. If one fails, stop and tell the user; do not retry with other flags.
   - `# HUMAN STEP`: print each line as `HUMAN STEP: <command>` and stop until the user says it ran. A `# HUMAN STEP` group can follow conductor steps (editing a PR that already exists, or `gh pr create` when the base config says `push-branch`); run nothing below it.

   If you change any code after the body is written, rerun verify and update `Head sha`; the check catches a stale block.
8. **Hand-off message.** Say what was built per engineer (A.8 steps 1-3), list the human-gated paths (the user must read those closely), say which conductor steps you ran (the pushed branch, the draft PR's URL), and give whatever is left as `HUMAN STEP:` lines. Record telemetry (tech-lead section C).

Lane B (quick) is the same minus the design critic and reports: branch `quick/<short-name>`, `Lane` is `quick`, one engineer row with report `inline`, review `SKIPPED (trivial change)` only when the mechanical rule in B.3 holds.

## After the push: CI, CI-fix, merge

- CI runs `verify` (`${CLAUDE_PLUGIN_ROOT}/templates/github/workflows/verify.yml`: untrusted, no secrets) and, if the user installed it and comments `/review`, `claude-review.yml` (trusted, base ref only). `${CLAUDE_PLUGIN_ROOT}/docs/security/split-ci.md` has the job table. CI is the evidence; the `CI evidence` field says `PENDING` until the PR exists.
- **CI-fix.** The user pastes the failure: you do not read GitHub. You hold no credential of the user's; from L2 up the only one you can reach is the bot's, and it is for the push and the draft PR only. You get at most `max_ci_fix_attempts` (default 2) fix commits per PR. While fixing: do not edit `.github/workflows/**`, tests, acceptance tests, snapshots or lockfiles (a dependency item may edit its lockfile). Resume the implicated engineer with the failing log tail; commit as
  ```
  fix(<slug>): ci-fix attempt <n>

  CI-Fix-Of: <sha of the commit CI rejected>
  Feature: <slug>
  ```
  then rerun verify, update `Head sha` and the `CI-Fix-Of` field, and run `pr_prepare.py prepare` again: with the branch already pushed it prints a plain `git push origin feat/<slug>` and, with `--pr N`, `gh pr edit N --body-file ...` (always under `# HUMAN STEP`). Follow the headers as in step 7. Still red after the cap, or two attempts that change nothing: stop and hand it to the user.
- **Merge is the human's.** The repo should squash with "PR title and description" so the audit block survives (`${CLAUDE_PLUGIN_ROOT}/docs/security/rulesets.md`). Under `delivery: local` the user merges the branch themselves; the same checks ran.
- **Reverts.** If a human reverts a merged feature, it is `needs_human`, never silently rebuilt. `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pr_prepare.py reverts --sha <merge or feature commits>` finds `This reverts commit <sha>` lines in git history; `--prs-json <recorded gh pr list output> --pr <n>` finds revert PRs for PR delivery.

## Trailers

| Trailer | On | Meaning |
|---|---|---|
| `Feature: <slug>` | WIP and fix commits | The feature folder it belongs to |
| `Engineer: <name>` | WIP commits | Whose work the commit holds |
| `Report: <path>` | WIP commits | Where the engineer's report is |
| `CI-Fix-Of: <sha>` | CI-fix commits | The commit CI rejected; also recorded in the audit block, because a squash merge drops trailers (CS-20) |

The PR (number and audit block) is the durable record, not the trailers. `Loop-Item`, `Loop-Run` and `Loop-Attempt` belong to the loop (§19.7) and are not used here.

## Stacked slices

Default: one PR per feature. When the design's `## Slices` should merge separately, branch the next slice off the previous slice's branch and pass `--base feat/<previous>` to `pr_prepare.py`; it prints `gh pr create --base feat/<previous>`. GitHub retargets the child when the parent merges and its branch is deleted. Do not wait on review to start the next slice (DEC-33).

## Findings that mean "stop and tell the user"

`git.diverged` (the remote branch has commits yours lacks: never a force push), `git.protected-branch`, `audit.h3-mismatch` (the diff touches a path a human must review), `ci-fix.cap`, `verify.untrusted`, and any `report.*` error. The rest are fixable by you.
