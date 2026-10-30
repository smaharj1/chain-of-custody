# Split CI (B4, RC-3)

A pull request is code execution in CI. No job that runs pull-request-supplied code may hold a secret or an OIDC token, except the preview `plan` job, which runs only after a human gate and only after the static policy passed.

## Jobs and what each may hold

| Job | Trigger | Checks out | `permissions` | Secrets / OIDC | Runs |
|---|---|---|---|---|---|
| `verify` (untrusted) | `pull_request` | PR head | `contents: read` | **None.** No OIDC; network egress to package registries only | the kit's verify script (definition from the base ref), lint, typecheck, test-integrity, static IaC policy on source |
| `heldout` | `pull_request` | PR head; the held-out tests are **restored from the Actions cache** in the last step before the tests run, with nothing from the PR running before it | `contents: read` | none (the store token lives in `heldout-sync.yml`) | held-out acceptance tests. The bot has no `actions: read`, so it sees the check result and failing AC ids, never the log. A required check only under `runner: sequential` |
| `heldout-sync` (the companion of `heldout`) | push to `main`, or a human `workflow_dispatch` | the default branch; never PR code | `contents: read` | read-only token for the private held-out store (`HELDOUT_STORE_TOKEN`), in scope for one step | fetches the tests and saves them in the default branch's Actions cache, which PR workflows can restore but not overwrite |
| `mutation` | `pull_request` | PR head | `contents: read` | **None** | the CI half of G4 (mutation testing). Opt-in: does nothing until `gates.g4` is on and `docs/baseline/evidence.json` is committed, both read at the base sha |
| `g10-product` | `pull_request` | PR head | `contents: read` | **None** | the CI half of G10 (your own accessibility, performance, PII and observability checks). Opt-in and evidence-gated like `mutation` |
| `intake` | `issues`, `labeled` (`factory:go` applied by a `FACTORY_APPROVERS` login) | the default branch only | `triage`: `contents: read`; `apply`: `contents: read`, `issues: write` | `ANTHROPIC_API_KEY` in one step of `triage` only (that job has no tools and no write); no OIDC | stage 1 of intake: a tool-less model reads the issue inside a nonce fence and answers JSON; stage 2 (`apply`, no model) validates it against a fixed schema and adds one `factory:<type>` label and one comment ([../ops.md](../ops.md)) |
| `dependency-check` | weekly `schedule`, or `workflow_dispatch` | the default branch | `scan`: `contents: read`; `report`: `contents: read`, `issues: write` | none | `scan` runs the package managers with `--ignore-scripts` and saves JSON; `report` runs no package manager and opens up to ten `factory:deps` issues |
| `smoke` | `schedule` (every six hours), or `workflow_dispatch` | the default branch | `smoke`: `contents: read`; `report`: `issues: write` | none | `verify.py --only smoke` and/or an HTTPS check of `SMOKE_URL`; on failure `report` (no project code) opens or comments on one `factory:bug` issue |
| `plan` (preview) | human `workflow_dispatch`, or `pull_request_target` after a human-applied `plan-ok` label | base-ref workflow and `.claude/`; PR source **as data**; `.terraform.lock.hcl` pinned | `contents: read`, `id-token: write`, `pull-requests: write` | OIDC plan-only role | `plan`, policy on the plan JSON, cost delta; posts a comment; plan file not kept |
| `apply` | push to `main`, then Environment approval (or the dispatch-only fallback) | `main` | `contents: read`, `id-token: write` | OIDC apply role | plan of `main` with lock and refresh, policy, human approval, apply that plan by hash |
| `claude-code-action` (optional) | maintainer `issue_comment` / review comment | base ref only; the PR diff is read through the API as data | `contents: read`, `pull-requests: write` | `ANTHROPIC_API_KEY` from a spend-capped workspace | review comments. Never checks out PR code with the key in scope |
| `label-guard` | `pull_request_target` / `issues`, `labeled` | nothing | `issues: write`, `pull-requests: write` | none | removes `factory:*`, `plan-ok` and `preview` labels applied by anyone outside `factory_approvers` |
| `drift` | schedule | `main` | `contents: read`, `id-token: write`, `issues: write` | OIDC read-only role | `plan -detailed-exitcode`; opens an issue on drift; never applies |

The kit ships these workflows in `${CLAUDE_PLUGIN_ROOT}/templates/github/workflows/`: `label-guard.yml`, `verify.yml`, `claude-review.yml`, `heldout.yml` with `heldout-sync.yml`, `mutation.yml`, `g10-product.yml`, `intake.yml`, `dependency-check.yml` and `smoke.yml`. The infrastructure workflows (`infra-check`, `infra-plan`, `infra-apply`, `infra-drift`) are in `${CLAUDE_PLUGIN_ROOT}/templates/infra/workflows/` and are described in [infra-ci.md](infra-ci.md). All are written to pass `ci_lint.py`; copy them, pin each action to a commit SHA, and see [../ops.md](../ops.md) for the three operations workflows.

## ci_lint.py

`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/ci_lint.py [--repo DIR] [--json]` lints `.github/workflows` with the kit's restricted YAML reader. Exit 0 means no errors and nothing unparsed; warnings do not fail. A workflow the reader cannot parse is reported as **`ci.unparsed`: manual review, never as passed**, and the raw text is still scanned for deploy commands and for `id-token: write` with a pull request trigger.

| Rule | Severity | Catches |
|---|---|---|
| `ci.oidc-with-pr-code` | error | a job with `id-token: write` that checks out PR code: on `pull_request` a default checkout is the PR's merge commit; on `pull_request_target`, `workflow_run` or comment triggers, a checkout of the head ref or `refs/pull/...`, `gh pr checkout`, or a fetch of the PR head. The write permission may come from the workflow or the job |
| `ci.secrets-in-pr-job` | error | any secret except `GITHUB_TOKEN` in a `pull_request` job (same-repo PRs, including the bot's, receive secrets), `secrets: inherit`, or a secret alongside PR code in a privileged trigger |
| `ci.pr-target-head-checkout` | error | `pull_request_target` / `workflow_run` / comment triggers that check out the PR |
| `ci.skills-not-restored` | error | a `claude -p` step or claude-code-action in a job that has not first restored `.claude`, `.mcp.json`, `plugin` and `.claude-plugin` (and `skills` if the repo has a top-level `skills/`) from the base ref with `git checkout <base> -- ...` or `git restore --source`. A job that checks out only the base ref needs no restore. Skill frontmatter hooks run in untrusted `-p` (RC-18) |
| `ci.claude-settings-sources` | error on PR jobs, else warn | `claude -p` without `--setting-sources user` or `--settings` |
| `ci.script-injection` | error / warn | untrusted `${{ }}` text (titles, bodies, branch names, commit messages, label names, any `github.event.*` except numbers, `inputs.*`, `github.ref_name`, `client_payload`) interpolated into `run:` or `github-script`; pass it through `env:`. `github.actor` is a warning |
| `ci.deploy-in-pr-job` | error | a deploy-class command or action in a pull request job |
| `ci.docker-uses-unpinned` | error / warn | `uses: docker://IMAGE` without `@sha256:`; an error when the job holds cloud credentials or secrets |
| `ci.credentials-on-bot-push` | error | a job with cloud credentials (OIDC, a credential action, a stored key) or secrets that a push to any branch but `main`, or a tag, starts, with no `environment:`. The bot can make such a push |
| `ci.remote-workflow-secrets` | error / warn | a reusable workflow from another repository that receives `secrets:` or an OIDC token. An error when a pull request, the bot or a privileged trigger can start it, or when its ref is not a commit SHA |
| `ci.pr-target-checkout-expression`, `ci.pr-code-fetch`, `ci.artifact-from-pr-run` | error | a `pull_request_target` or `workflow_run` job that holds secrets or an OIDC token and checks out a computed ref, downloads an archive and unpacks it, or downloads artifacts another run built |
| `ci.unreadable` | error | a workflow the lint cannot read (an exception, a recursion or memory error, or more than 100,000 delimiters in one script); its callees are judged as if a pull request called them |
| `ci.floating-action-tag` | warn | `uses: org/action@v4` instead of a commit SHA |
| `ci.missing-permissions` | warn | no `permissions:` anywhere |
| `ci.write-permissions-pr-job` | warn | a write token alongside PR code |
| `ci.persist-credentials` | warn | checkout without `persist-credentials: false` in a PR job (the token stays in `.git/config`) |
| `ci.untrusted-comment-trigger` | warn | a comment or issue-text trigger with no `author_association` check |

`infra.*` rules apply to workflows that run terraform, tofu, pulumi, cdk, kubectl or helm: see [infra-ci.md](infra-ci.md).

The doctor runs the same rules as `ci.lint`; errors there are an unmet L3 prerequisite ("split CI").

## The "skill hooks in CI" fixture

A pull request adds a skill whose frontmatter has a `PreToolUse` hook that writes a marker file; the CI `-p` job must not create it. Offline, `tests/doctor/fixtures/workflows/claude-review-unsafe.yml` is rejected and `claude-review-safe.yml` passes. Whether a real job on a real runner creates the marker is the live half, in `verify-boundary.sh`'s list of manual steps.
