# Split CI (B4, RC-3)

A pull request is code execution in CI. No job that runs pull-request-supplied code may hold a secret or an OIDC token, except the preview `plan` job, which runs only after a human gate and only after the static policy passed.

## Jobs and what each may hold

| Job | Trigger | Checks out | `permissions` | Secrets / OIDC | Runs |
|---|---|---|---|---|---|
| `verify` (untrusted) | `pull_request` | PR head | `contents: read` | **None.** No OIDC; network egress to package registries only | the kit's verify script (definition from the base ref), lint, typecheck, test-integrity, static IaC policy on source |
| `heldout` | `pull_request` | PR head, plus held-out tests fetched in a step that runs **before** PR code | `contents: read` | read-only token for the held-out store, scoped to that step | held-out acceptance tests. The bot has no `actions: read`, so it sees the check result and failing AC ids, never the log |
| `plan` (preview) | human `workflow_dispatch`, or `pull_request_target` after a human-applied `plan-ok` label | base-ref workflow and `.claude/`; PR source **as data**; `.terraform.lock.hcl` pinned | `contents: read`, `id-token: write`, `pull-requests: write` | OIDC plan-only role | `plan`, policy on the plan JSON, cost delta; posts a comment; plan file not kept |
| `apply` | push to `main`, then Environment approval (or the dispatch-only fallback) | `main` | `contents: read`, `id-token: write` | OIDC apply role | plan of `main` with lock and refresh, policy, human approval, apply that plan by hash |
| `claude-code-action` (optional) | maintainer `issue_comment` / review comment | base ref only; the PR diff is read through the API as data | `contents: read`, `pull-requests: write` | `ANTHROPIC_API_KEY` from a spend-capped workspace | review comments. Never checks out PR code with the key in scope |
| `label-guard` | `pull_request_target` / `issues`, `labeled` | nothing | `issues: write`, `pull-requests: write` | none | removes `factory:*`, `plan-ok` and `preview` labels applied by anyone outside `factory_approvers` |
| `drift` | schedule | `main` | `contents: read`, `id-token: write`, `issues: write` | OIDC read-only role | `plan -detailed-exitcode`; opens an issue on drift; never applies |

The kit ships `label-guard.yml` (`.claude/templates/github/workflows/`). The verify, heldout and infra workflows come with the phases that own them; they are written to pass `ci_lint.py`.

## ci_lint.py

`python3 .claude/scripts/ci_lint.py [--repo DIR] [--json]` lints `.github/workflows` with the kit's restricted YAML reader. Exit 0 means no errors and nothing unparsed; warnings do not fail. A workflow the reader cannot parse is reported as **`ci.unparsed`: manual review, never as passed**, and the raw text is still scanned for deploy commands and for `id-token: write` with a pull request trigger.

| Rule | Severity | Catches |
|---|---|---|
| `ci.oidc-with-pr-code` | error | a job with `id-token: write` that checks out PR code: on `pull_request` a default checkout is the PR's merge commit; on `pull_request_target`, `workflow_run` or comment triggers, a checkout of the head ref or `refs/pull/...`, `gh pr checkout`, or a fetch of the PR head. The write permission may come from the workflow or the job |
| `ci.secrets-in-pr-job` | error | any secret except `GITHUB_TOKEN` in a `pull_request` job (same-repo PRs, including the bot's, receive secrets), `secrets: inherit`, or a secret alongside PR code in a privileged trigger |
| `ci.pr-target-head-checkout` | error | `pull_request_target` / `workflow_run` / comment triggers that check out the PR |
| `ci.skills-not-restored` | error | a `claude -p` step or claude-code-action in a job that has not first restored `.claude`, `.mcp.json`, `plugin` and `.claude-plugin` (and `skills` if the repo has a top-level `skills/`) from the base ref with `git checkout <base> -- ...` or `git restore --source`. A job that checks out only the base ref needs no restore. Skill frontmatter hooks run in untrusted `-p` (RC-18) |
| `ci.claude-settings-sources` | error on PR jobs, else warn | `claude -p` without `--setting-sources user` or `--settings` |
| `ci.script-injection` | error | untrusted `${{ }}` text (titles, bodies, branch names, commit messages, label names) interpolated into `run:` or `github-script`; pass it through `env:` |
| `ci.deploy-in-pr-job` | error | a deploy-class command or action in a pull request job |
| `ci.floating-action-tag` | warn | `uses: org/action@v4` instead of a commit SHA |
| `ci.missing-permissions` | warn | no `permissions:` anywhere |
| `ci.write-permissions-pr-job` | warn | a write token alongside PR code |
| `ci.persist-credentials` | warn | checkout without `persist-credentials: false` in a PR job (the token stays in `.git/config`) |
| `ci.untrusted-comment-trigger` | warn | a comment or issue-text trigger with no `author_association` check |

The doctor runs the same rules as `ci.lint`; errors there are an unmet L3 prerequisite ("split CI").

## The "skill hooks in CI" fixture

A pull request adds a skill whose frontmatter has a `PreToolUse` hook that writes a marker file; the CI `-p` job must not create it. Offline, `tests/doctor/fixtures/workflows/claude-review-unsafe.yml` is rejected and `claude-review-safe.yml` passes. Whether a real job on a real runner creates the marker is the live half, in `verify-boundary.sh`'s list of manual steps.
