# Infrastructure CI: check, preview, apply, drift

This page is the guide to the infrastructure CI templates (report section 8, RC-2, RC-3, RC-4, CO-39). The rule they implement is on [infra-human-gate.md](infra-human-gate.md): **the agent writes infrastructure code; CI checks it and plans it; a person approves the apply.** Nothing in this kit applies, deploys or destroys anything, and no template lets the bot or an agent start an apply. You install the templates; the first apply is yours.

## The flow

| Step | Workflow | Starts when | Holds | What it does |
|---|---|---|---|---|
| 1. Source check | `infra-check.yml` | every pull request | `contents: read`. No secret, no OIDC, no cloud credential | The base ref's `source_policy.py` judges the PR's source **before** terraform runs; then `fmt`, `init -backend=false`, the policy again on the modules `init` downloaded, `validate`, `tflint` with the base ref's config, optional checkov |
| 2. Preview plan | `infra-plan.yml` | a person in `factory_approvers` applies the `plan-ok` label, or runs the workflow by hand | OIDC, **plan-only** role, in the `plan` job only | Re-checks the approver, downloads the PR source at the approved sha as data, runs the policy again, then `plan -lock=false`. Posts a values-free summary on the PR. A new commit removes the label |
| 3. Apply | `infra-apply.yml` (one of two variants) | a push to `main`, then a person approves; or a person dispatches it | OIDC, apply role, in the `apply` job only | A fresh plan of `main` with lock and refresh, policy on the plan JSON, a person approves **that** plan, CI applies that file by hash in the same run |
| 4. Drift | `infra-drift.yml` | a schedule | OIDC, read-only role, `issues: write` | `plan -detailed-exitcode` of `main`; opens or updates an issue when reality differs. Never applies |

The preview plan is a preview. It is never applied. The plan that is applied is made again from `main` after the merge (RC-4), so a plan made from an older tree can never be applied.

## What is in the kit

| Path | What it is |
|---|---|
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/workflows/infra-check.yml`, `infra-plan.yml`, `infra-drift.yml` | the three workflows that never apply |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/workflows/infra-apply.environment.yml` | apply behind an Environment with required reviewers (public repositories, or Enterprise for private ones) |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/workflows/infra-apply.dispatch.yml` | apply by a human-only `workflow_dispatch`, for plans without Environment reviewers (the doctor calls this "apply gate = human dispatch" and caps IaC projects at L3) |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/oidc/` | trust policies for the post-merge plan role, next to the four in `${CLAUDE_PLUGIN_ROOT}/templates/security/oidc/` |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/iam/state-lock.policy.json` | the lock-only permissions the post-merge plan role adds to `${CLAUDE_PLUGIN_ROOT}/templates/security/iam/plan-readonly.policy.json` |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/policy/` | the rego bundle for the plan JSON (`plan.rego`, `plan_test.rego`). TEMPLATE: not run by the kit's tests |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/infra-policy.json` | the allowance file the source policy reads from the base ref |
| `${CLAUDE_PLUGIN_ROOT}/templates/infra/mcp-deploy-deny.settings.json` | deny rules for deploy-class MCP tools |
| `${CLAUDE_PLUGIN_ROOT}/scripts/infra/source_policy.py`, `hcl.py` | the pre-plan source policy and its HCL scanner |
| `${CLAUDE_PLUGIN_ROOT}/scripts/infra/gate.py` | the small checks the workflows call |
| `${CLAUDE_PLUGIN_ROOT}/scripts/infra/ci_rules.py` | the `infra.*` rules `ci_lint.py` applies |
| `tests/infra/`, `tests/fixtures/infra/` | the tests, the fixture repositories and the planted-change corpus |

## Installing it (a human, once)

1. Copy the workflows into `.github/workflows/`. Pick **one** apply variant and install it as `infra-apply.yml` (the `job_workflow_ref` trust policy pins that file name). Set `TF_ROOT` in each file. Pin every action to a commit SHA (each line says so; `ci_lint.py` warns until you do).
2. Create the OIDC roles and trust policies. Never `repo:ORG/REPO:*` and never a `ref:` subject ([infra-human-gate.md](infra-human-gate.md)). The four roles:

   | Role | Trust file | Permissions |
   |---|---|---|
   | preview plan | `${CLAUDE_PLUGIN_ROOT}/templates/security/oidc/aws-plan-preview.trust.json` | `plan-readonly.policy.json` only (no lock, hence `-lock=false`) |
   | post-merge plan of `main` | `${CLAUDE_PLUGIN_ROOT}/templates/infra/oidc/aws-plan-main.trust.json`, or `aws-plan-main-job-workflow-ref.trust.json` for the dispatch variant | `plan-readonly.policy.json` plus `${CLAUDE_PLUGIN_ROOT}/templates/infra/iam/state-lock.policy.json` |
   | apply | `${CLAUDE_PLUGIN_ROOT}/templates/security/oidc/aws-apply-environment.trust.json`, or `aws-apply-job-workflow-ref.trust.json` (pick one: they are alternatives, see below) | yours to scope, and no wider than the change needs |
   | drift | `${CLAUDE_PLUGIN_ROOT}/templates/security/oidc/aws-drift-readonly.trust.json` | `plan-readonly.policy.json` only |

   **The two apply trusts are alternatives.** `aws-apply-environment.trust.json` goes with the Environment variant of `infra-apply.yml`; `aws-apply-job-workflow-ref.trust.json` goes with the dispatch variant. Create one apply role, not both. The workflow-ref trust only matches after you change the repository's OIDC subject claim template to include `job_workflow_ref`, and that template applies to every job in the repository: the Environment-style subjects of the other roles stop appearing, so their trust policies must be rewritten too. Choose the dispatch variant only if you are ready to do that. See `${CLAUDE_PLUGIN_ROOT}/templates/security/oidc/README.md`.

3. Set repository variables (not secrets): `FACTORY_APPROVERS` (the same logins as `factory_approvers`), `FACTORY_BOT_LOGIN` (the bot's login, so a listed bot is still refused), `AWS_REGION`, the role ARNs the workflows name, and for the apply workflows `CONFTEST_VERSION` and `CONFTEST_SHA256` (the release you vetted; the workflow checks the download against the hash). Optional: `INFRA_CHECKOV_VERSION`.
4. Create the Environments `plan-preview`, `plan-main` (Environment variant), `drift` and `production` (Environment variant). Restrict each to the `main` branch (deployment branch policy; the doctor warns, as `deploy.environment-branch-policy`, when an Environment that an apply job or any other credentialed job (OIDC, a credential action, a stored cloud key) names has none in the GitHub data it can read. The plan and drift roles trust the Environment name alone, so a branch the bot pushed could name one and get the role. That check is best effort: with no GitHub data it is skipped). `production` needs **required reviewers and "prevent self-review"**. GitHub creates an Environment with no reviewers the first time a workflow names it, which gates nothing: the apply job refuses when the run's review history holds no approval from a listed approver, and the doctor reads the reviewers when it has GitHub data.
5. Add `infra-check` to the required status checks of the main ruleset ([rulesets.md](rulesets.md)). Make `.github/**`, `infra/**`, `.claude/**` and `.terraform.lock.hcl` CODEOWNERS paths with a code owner who is not the bot. A `pull_request` workflow runs the PR's own copy of itself, so a PR could weaken `infra-check.yml`; the code-owner rule on `.github/**` is what stops that.
6. Install `label-guard.yml` ([split-ci.md](split-ci.md)) so `plan-ok` applied by anyone outside `factory_approvers` is removed. `infra-plan.yml` does not rely on it: it checks the approver itself.
7. Copy `infra-policy.json` to `.claude/infra-policy.json` and a `.tflint.hcl` to the repository root, both through a pull request. The first infrastructure PR has no baseline: set the repository variable `INFRA_INITIAL=true` for that PR only, then unset it.
8. Attest the deployment surface ([deploy-surface.md](deploy-surface.md)): the Environment variant's `infra-apply.yml` is a deploy that fires on `push:main` with `gate: gated-by-environment`; the dispatch variant is human-started and needs no trigger entry.
9. Merge `mcp-deploy-deny.settings.json` into the settings an agent session uses (the rules are the same list as `mcp-lockdown.settings.json`; remove a pattern only for a server you named on purpose, [mcp.md](mcp.md)).

`${CLAUDE_PLUGIN_ROOT}/templates/infra/policy/` is read from the checkout of `main` at run time. Copy it wherever you like and change the `--policy` path in the two apply workflows.

## The source policy

`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/infra/source_policy.py --root <PR checkout> --base <base checkout> --policy <base>/.claude/infra-policy.json`

Plan executes provider and module code, so the check runs first and reads the source with a scanner of its own (no terraform binary). Run it from the **base** ref's copy: a pull request cannot edit the policy that judges it. Run it again with `--post-init` after `terraform init -backend=false` to scan the modules that init downloaded (their unpinned nested sources are warnings, since you cannot edit them; every other ban is an error).

| Rule id | Refuses |
|---|---|
| `data.external`, `data.http` | data sources that run a program or fetch from the network. Also inside `check` blocks |
| `data.remote-state-http` | `terraform_remote_state` over the `http` backend, or with a backend that is not a literal |
| `provisioner.local-exec`, `.remote-exec`, `.other` | every provisioner, including `dynamic "provisioner"` |
| `module.registry-no-version`, `.registry-range`, `.registry-nonliteral` | a registry module without an exact `version` |
| `module.git-unpinned`, `.git-tag` | a git module without `?ref=` a full commit SHA |
| `module.archive-unpinned` | an http, s3, gcs or hg module without `checksum=sha256:...` (or a full revision) |
| `module.local-escape` | a local path that leaves the scanned tree, or `file://` |
| `module.source-nonliteral`, `.source-unknown` | a source that is not one plain string, or not an address the policy understands. A repeated `source` counts as non-literal |
| `provider.new` | a provider (declared in `required_providers`, named by a `provider` block or the meta-argument, or implied by a resource type) that the base ref does not use, its lock file does not list, and the policy file does not allow |
| `provider.banned`, `provider.non-literal-source` | the `external` and `http` providers in any namespace or alias; a source that is not a literal |
| `provider.endpoint-override` | `endpoint`, `endpoints`, `*_endpoint`, proxy and CA-bundle settings in a provider, backend, `cloud` or remote-state block: a way to send the role's temporary credentials to a host the PR chose |
| `provider.exec` | an `exec` block in a provider (kubernetes, helm) |
| `backend.changed` | a backend type the base ref does not use and the policy file does not allow |
| `expr.file-outside-tree` | `file()`, `templatefile()` and the other file functions (and `data "local_file"`) reading an absolute, `~` or `..` path: the plan job's credentials are in its environment |
| `tree.dot-terraform`, `tree.plugin-dir`, `tree.provider-binary`, `tree.symlink`, `tree.special-file`, `tree.terragrunt`, `tree.unreadable` | a committed `.terraform/` or `terraform.d/`, a provider binary, links, pipes, terragrunt configuration (its hooks run commands; not judged here), directories it could not list |
| `parse.unparsed` | a file the scanner could not read, including malformed `*.tf.json`, invalid UTF-8, files over 2 MiB. **Never a pass.** |
| `policy.invalid` | a policy file that is missing, unreadable, has an unknown key or a wrong type. Nothing is allowed then |

Warnings: `module.*` in downloaded modules, `expr.file-dynamic`, `backend.attrs-changed`, `lock.changed`, `tree.cli-config`, `provider.initial`.

It scans `*.tf`, `*.tf.json`, `*.tofu`, `*.tofu.json`, `*.tftest.hcl` and `*.tftest.json` in every directory except `.git`, case-insensitively, and the lock file for the provider baseline. It does not follow links.

**The policy file** (`infra-policy.json`) has five keys: `schema` (`infra-policy.v1`), `allowed_providers`, `allowed_backends`, `allow_version_ranges`, `allow_git_tag_refs`. It is read from the base ref, so allowing a new provider is its own reviewed change. It can never allow `external` or `http`. `allow_git_tag_refs` accepts any ref except `HEAD`, `main` and `master`; a tag can be moved, so use it only for a repository you control.

**How the scanner reads ambiguous text.** Where it could disagree with terraform it takes the reading in which more text is code: a bare carriage return and the Unicode line separators end a comment; a heredoc ends at any line whose trimmed text is the marker. It refuses what it is unsure of: an unterminated string, comment or heredoc, a comment or heredoc inside an interpolation, a block label with interpolation, two statements on one line, a brace on the next line, an unknown character, duplicate keys in JSON syntax, a list in JSON syntax that mixes objects with other values.

### What it does not cover

- **The CI lint and the doctor's deploy-surface detection are heuristic readers of shell and workflow text.** They catch honest mistakes and cheap evasions and fail closed where they can (a file the lint cannot read is `ci.unreadable`, an error; a workflow the doctor cannot read counts as a deploy), but they are not complete: a command in a script file, an `eval`, an alias, a tool with no pattern, or a value passed through step outputs is outside them, and a clean lint is not evidence that nothing applies. Two limits of the text reader in particular: a quoted value that runs over several lines is paired by a few passes that restart after a line with an odd number of quotes, so a stray quote on the value's own line can still pair it wrongly; and comments are cut line by line, a line whose quoting is unclear keeping its `#`, so a commented-out apply on such a line is read as an apply (a false alarm, never a miss). The control that holds is the environment-protected apply ([infra-human-gate.md](infra-human-gate.md)): a person approves every apply, whatever the lint saw.
- **A plan runs the PR's HCL and the pinned providers.** After the policy, what is left is what HCL can do with the plan role's read access: a data source with a side effect your role allows (`aws_lambda_invocation` needs `lambda:InvokeFunction`, which the example plan-only policy does not grant), and `file()` with a path computed at plan time (`expr.file-dynamic`, a warning). The human who applies `plan-ok` is expected to have read the source. The plan-only role's limits, the discarded plan output, and the values-free summary bound the rest.
- **Modules and providers are trusted at the version you pinned.** An exact version or commit pins what is fetched, not whether it is safe. Provider binaries are pinned by `.terraform.lock.hcl`; changing it is an H3 path.
- **Override files.** An `override.tf` that sets only `version` on a module is reported as a module without a source. That is on purpose.
- **Policy on the plan JSON** is the rego bundle, which this kit could not run.
- **Anything it cannot read** (terragrunt, Pulumi, CDK programs) is outside it. For those, keep the same human gate and review the code by hand.

## gate.py

`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/infra/gate.py <command>`. Exit 0 ok, 1 refused (one line on stderr), 2 usage or internal error.

| Command | Refuses unless |
|---|---|
| `authorize --actor A [--actor B] --approvers a,b --bot BOT` | every actor is in the approver list and none is a bot. An empty list refuses. A bot named in the list refuses. A `[bot]` login always refuses |
| `require-main-ref --ref REF` | REF is `refs/heads/main` |
| `base-is-main --planned-sha S --main-sha M` | both are 40-character commit SHAs and they are equal |
| `hash-file FILE` / `verify-hash --file F --sha256 H` | (verify) the file still has that sha256 |
| `require-run-approval --approvals-json F --environment E --approvers a,b` | the run's review history has an approval of E by a listed approver who is not a bot |
| `plan-digest --plan-json F` | prints `sha256:...` over the resource changes, output changes and variables; stable across runs of the same state and source (timestamps and tool version excluded) |
| `plan-summary --plan-json F` | prints a markdown summary of resource addresses and actions, with no values, safe to post |

The workflows run these from a checkout of the base ref, not from the pull request.

## The two apply variants

**Environment variant.** `plan` (environment `plan-main`, no reviewers) makes the plan of `main` with lock and refresh on, runs `conftest` on the plan JSON, records the sha256 of the plan file and the planned commit, writes the summary and digest to the run summary, and uploads the plan file. `apply` (environment `production`) does not start until the required reviewers approve. They read the plan in the run summary. Then it checks the review history, the plan file's hash and that the planned commit is still the tip of `main`, assumes the apply role, and runs `terraform apply tfplan`. Plan and apply are separate jobs because an Environment approval comes before its job starts; one job would ask the reviewers to approve something that had not been planned.

**Dispatch variant.** No Environment is needed. A person in `factory_approvers` runs the workflow twice. `mode=plan` shows the plan and its digest. `mode=apply` with `approved_digest` makes the plan again and applies it only if the digest matches, the file's hash is unchanged and `main` has not moved. The bot has no `actions` scope, so it cannot dispatch; the workflow also checks the actor and the triggering actor itself and refuses any `[bot]` login. The OIDC subject is the workflow file, so every job in it can reach both roles: the human dispatch and these checks are the gate, not job separation.

In both, `terraform apply tfplan` also refuses a saved plan when the state changed since it was made.

**Not built here:** the infracost delta (report section 8.3). It needs an API key, and a secret in the same workflow as artifacts built from PR code is a decision for you. Run `infracost breakdown --path plan.json` in a separate, trusted job if you want it.

## ci_lint rules for infrastructure workflows

`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/ci_lint.py` now also applies `infra/ci_rules.py` to any workflow that runs terraform, tofu, terragrunt, pulumi, cdk, cloudformation, kubectl or helm. The rules are structural: they check the gates are in the workflow, in order. They cannot check that an Environment has reviewers or that a variable is set. A gate is recognised only if it uses `gate.py`; a hand-written comparison is reported, so a person reads it.

| Rule | Severity | Catches |
|---|---|---|
| `infra.apply-trigger` | error | an apply that a schedule, pull request, comment, tag, other branch or `repository_dispatch` can start |
| `infra.apply-ungated` | error | an apply with no `environment:` and no dispatch-only `gate.py authorize` |
| `infra.apply-same-job-as-plan` | error | plan and apply in one job |
| `infra.apply-without-saved-plan` | error | `terraform apply` / `destroy` without a plan file argument (including `-auto-approve` alone, and `-var`, `-target` and `-var-file` forms) |
| `infra.apply-plan-hash-unverified`, `infra.apply-main-sha-unchecked` | error | no `verify-hash` or `base-is-main` before the apply, in the apply job |
| `infra.apply-digest-unchecked` | error | a dispatch apply that does not compare the plan digest |
| `infra.dispatch-ref-unchecked` | error | a dispatch apply that does not require `refs/heads/main` |
| `infra.apply-ref-input` | error | an apply path that checks out a ref chosen elsewhere, or PR code |
| `infra.apply-flags` | error | `-lock=false` or `-refresh=false` on an apply, or on a plan in a workflow that applies |
| `infra.plan-ungated` | error | a credentialed plan on a pull request, comment, or push to any branch but `main` (or a tag), without the `plan-ok` label and `gate.py authorize` |
| `infra.policy-after-init` | error | `init`, `validate` or `plan` on pull-request source before `source_policy.py` ran in the job or a job it needs |
| `infra.policy-from-pr`, `infra.policy-bypassable` | error | the policy run from the PR's own tree; or with `continue-on-error` or a trailing `or true` |
| `infra.gate-bypassable` | error | a `gate.py` step that can fail quietly (`continue-on-error`, a trailing `or true`, a pipe, `set +e`); `continue-on-error` on a job in an apply path; an apply job with `if: always()` or `failure()` |
| `infra.apply-environment-dynamic` | error | an `environment:` chosen at run time |
| `infra.static-credentials` | error | a stored cloud key in `secrets` instead of OIDC |
| `infra.input-in-run` | error | a dispatch input interpolated into a `run:` script |
| `infra.environment-approval-unverified` | error | an apply gated only by `environment:`, with no `gate.py require-run-approval` in the job or a job it needs. An Environment GitHub creates on first use has no reviewers and gates nothing; the lint cannot see GitHub, so the apply must check the run's approval history itself |
| `infra.apply-indirect` | error / warn | `$VAR apply`, `destroy`, `deploy` or `up` as a command: the tool name is hidden. Error in a job that holds cloud credentials, warning elsewhere. `TF=terraform` set in the same script is followed, so `$TF apply` is read as `terraform apply` |
| `infra.apply-concurrency`, `infra.apply-tool-unsupported` | warn | no `concurrency:` group; an apply tool the saved-plan flow does not cover (cdk, cdktf, sam, pulumi, terragrunt, copilot, eb, ansible, kubectl, helm) |

What counts as an apply. Besides `terraform`/`tofu apply|destroy`: `cdk` and `cdktf deploy|destroy`, `sam deploy`, `pulumi up|update|destroy`, `terragrunt apply`, `terragrunt run-all|run --all apply`, `copilot [svc|env|job] deploy`, `eb deploy|create|terminate`, `serverless|sls deploy`, `ansible-playbook`, `kubectl apply|replace|delete|patch|create`, `helm upgrade|install|rollback`, `aws cloudformation deploy...`, and the actions `hashicorp/terraform-github-actions`, `dflook/terraform-apply`, `pulumi/actions`, `gruntwork-io/terragrunt-action`. The command is read after normalising its spelling: a path (`/usr/local/bin/terraform`, `./bin/tofu`), quotes (`terraform 'apply'`), `$(which terraform)` and `${TF:-terraform}`; a path through `${DIR}` or `$(pwd)` (`${DIR}/cdk deploy`); a package runner and its version (`npx aws-cdk@2 deploy`, `pnpm dlx aws-cdk deploy`, `pipx run aws-sam-cli deploy`). The doctor and the lint share one rule for what may stand in front of a program name (`workflows.PROG_BEHIND`): `x-cdk deploy` and `foo.cdk deploy` are a call in neither. A flag value with blanks in it (`--context ${{ matrix.ctx }}`, `-n $(echo x y)`, `-c a="x y"`, `-n my\ ns`) does not hide the verb. A `uses: docker://IMAGE` step is read as its `args` (and `entrypoint`); a known IaC image (terraform, tofu, terragrunt, pulumi, kubectl, helm, aws-cli) with no args counts as an apply. Still outside the lint: a command inside a script file or an `eval`, which `.github/**` CODEOWNERS review and the doctor's one-level script scan are for.

Other rules `ci_lint.py` applies to every workflow, and which matter here:

| Rule | Severity | Catches |
|---|---|---|
| `ci.credentials-on-bot-push` | error | a job with cloud credentials (OIDC, a credential action, a stored key) or secrets that a push to any branch but `main`, or a tag, starts, with no `environment:`. The bot can make such a push |
| `ci.docker-uses-unpinned` | error / warn | `uses: docker://IMAGE` without `@sha256:`; an error when the job holds credentials |
| `ci.remote-workflow-secrets` | error / warn | a reusable workflow from another repository that receives `secrets:` or an OIDC token. An error when a pull request, the bot or a privileged trigger can start it, or when its ref is not a commit SHA; a warning on a push to `main` or a dispatch only |
| `ci.script-injection` | error / warn | `${{ }}` text a stranger can choose, inside `run:` or `github-script`: any `github.event.*` except numbers, `action` and `repository` fields, `inputs.*`, `github.head_ref`, `github.ref_name`, `github.ref`, `client_payload`, `toJSON(github.event)`. `github.actor` is a warning. A comparison that is the top-level operator of an operand (`==`, `contains()`, `!x`) yields only true or false and is allowed; one inside a call (`format('{0}', title, 1 == 1)`) is not. `${{ env.NAME }}` is judged by how `NAME` is set: an `env:` entry (workflow, job or step) whose value interpolates event text, directly or through another entry, is the same injection. Pass the value through `env:` and read `$NAME` in the shell. Not seen: a value laundered through `steps.*.outputs`, `needs.*.outputs` or `$GITHUB_ENV` |
| `ci.oidc-with-pr-code` | error | `id-token: write` with a checkout of pull-request code. A reusable workflow (`on: workflow_call`) runs on its callers' events: the lint reads its callers in the same directory, and a callee nobody in the set calls is read as if a pull request called it. A callee with no `permissions:` of its own gets `id-token` from a caller that grants it. On `pull_request` and `merge_group`, `github.sha` and `github.ref` are the PR merge commit, so a checkout of either is PR code |
| `ci.pr-target-checkout-expression`, `ci.pr-code-fetch`, `ci.artifact-from-pr-run` | error | on `pull_request_target`, `workflow_run`, `issue_comment` and `issues`: a job with secrets or OIDC that checks out a computed `ref` or `repository` (only the default branch, `github.sha`, `github.ref` and literal `main` are accepted: a `steps.*` or `needs.*` output can carry the PR head sha), fetches and unpacks an archive, or downloads artifacts another run built (any `*download-artifact*` action; the official action only when it names a `run-id`, `repository` or `github-token`, or under `workflow_run`) |

Every gate command (`authorize`, `require-main-ref`, `require-run-approval`, `verify-hash`, `base-is-main`, `plan-digest`) counts only when it is the command itself (an `echo gate.py ...` does not), in a step with no `if:` of any kind, with `continue-on-error` absent or `false`, with no `shell:` other than `bash` or `sh` (and no workflow or job `defaults.run.shell` other than those), with no `if`, `case`, loop, `&&`, `||` or `exit` before it in the script, and in the apply job (before the apply) or a job the apply job needs. `authorize` also needs an `--actor` taken from the run. A main-ref check written as a job-level `if:` counts when it is not widened with `||` or a status function. The `plan-ok` label that gates a preview plan is read from the job-level `if:` of the job or a job it needs, not from step text.

## Drift

`infra-drift.yml` runs on a schedule, plans `main` with `-detailed-exitcode` and the read-only role, and opens or updates one issue labelled `infra-drift` with addresses and actions only. Exit code 1 fails the run; 2 opens the issue. It uses `-lock=false` because the read-only role cannot write the lock. It never applies, and `ci_lint.py` refuses an apply in any workflow a schedule can start.

## Policy on the plan

`plan.rego` denies deleting or replacing a stateful resource, an IAM policy that allows every action on every resource, a trust policy with a wildcard principal or a `repo:` wildcard subject, ingress from the whole internet on ports other than 80 and 443, a public bucket ACL, and (as a second layer behind the source policy) `external` and `http` data sources and provisioners in the plan's configuration. It warns about other deletions. Run `opa test ${CLAUDE_PLUGIN_ROOT}/templates/infra/policy` once yourself: this kit has no `opa` or `conftest`, so the bundle is TEMPLATE (IMP-25) and the tests check only its structure.

## Who may run what (report section 8.2)

| Tool | The agent, locally | CI with a plan-only role | Human-gated apply |
|---|---|---|---|
| terraform / tofu | `fmt`, `validate`, `tflint`, `init -backend=false` | after `plan-ok`: `plan -lock=false` as a preview, output discarded, plan file not kept | after merge: `plan` of `main` with lock and refresh, policy, a person approves, `apply <that plan>` by hash |
| CDK / CloudFormation | `cdk synth` only in a sandbox with no credentials and no network (synth runs code), `cfn-lint`, `cfn-guard` | `cdk diff`, change-set create | change-set execute |
| pulumi | none (preview runs the program) | `preview` | `up` |
| kubectl / helm | `kubeconform`, `helm lint`, `helm template` | `kubectl diff` with a read-only kubeconfig | `apply`, `upgrade` |
| gcloud / az / aws | read-only `describe` or `list` only if the doctor shows a read-only identity; otherwise none | none | none |
| flyctl, vercel, netlify, railway, render | none | preview deploys only when a person applies a `preview` label, with a preview-only token (a preview deploy is a deploy) | production deploy |
| database migrate | a local, ephemeral database only (`durable_db_scope: local`) | an ephemeral CI database | a shared or production database, post-merge, behind an Environment or the dispatch gate |
| MCP deploy or `run_script` tools | denied | none | none |

The templates in this phase implement the terraform and tofu column. For the other tools keep the same shape (a gate no agent can pass, the apply only after merge, a person approving what will run); `ci_lint.py` warns `infra.apply-tool-unsupported` so the workflow gets read by a person. A migration workflow against a shared database is not shipped: the migration command is yours, and `ci_lint.py` cannot tell an ephemeral database from a shared one, so it does not guess.

## Other section 8.3 items

| Item | Where |
|---|---|
| Infra item "done": source plus tests, a clean source check, a preview plan after the gate, a plan summary on the PR | `infra-check.yml` and `infra-plan.yml`. The infracost delta is not built (see above); tests are the project's own (terratest, `cdk assert`) |
| Drift routine | `infra-drift.yml` |
| Separate development and production database credentials | the Databases section below |
| Ambient deploy plugins disabled in factory profiles | the doctor's `mcp.plugin.*` checks; `mcp-deploy-deny.settings.json` for tools |
| Strip `AWS_*`, `GOOGLE_*`, `AZURE_*`, `KUBECONFIG`, `GH_TOKEN`, `SSH_AUTH_SOCK` and deploy tokens from the driver's child environment | the loop driver, not this phase |

## Databases

Development and production database credentials are separate secrets and never the same one. The loop migrates only a local, ephemeral database; a migration against a shared or persistent database is H4 and runs post-merge behind the same Environment or dispatch gate ([infra-human-gate.md](infra-human-gate.md)).

## What the doctor and the tests prove

| Exit criterion | Shown by |
|---|---|
| A planted infrastructure change gets a plan comment only after policy passes | `tests/fixtures/infra/planted/` (20 planted changes, each refused for named rules) and `tests/infra/test_templates.py` (the comment job needs the plan job, which needs the fetch job that runs the policy, which needs the approver check) |
| Apply cannot run without a human approval | `tests/infra/test_ci_rules.py` (an apply without an Environment or an actor check, or with any other trigger, is an error), `gate.py authorize` and `require-run-approval` tests, and the doctor tests for the two variants |
| A post-merge plan whose base sha is not `main` refuses | `gate.py base-is-main` tests, and `infra.apply-main-sha-unchecked` |
| The RC-1 deploy-surface fixtures still refuse | `tests/infra/test_templates.py` (`DoctorSeesThem`) next to the Phase 1 fixtures |

Pending a human with a cloud account: the first real plan, the first real apply, the OIDC roles, and the rego bundle with `opa`. See the changelog fragment for the list.

## Hostile workflows and cost

A workflow comes from a stranger, so every pattern `ci_lint.py` and the doctor run over its text is linear. Flags between a tool and its verb (`terraform -no-color -chdir=x apply`) are matched by `workflows.flags_re`, where a token is a flag or a value, never both; there is no cap on the number of flags, so adding flags does not hide a verb. A run of flags stops in front of a token that is the tool's name (`cdk -a cdk deploy`, `--app=cdk`, `-a x/cdk`) and the match starts there instead, which is what keeps a line of repeated `cdk -a` words from being scanned once per word; a value that only begins with the name (`cdk --app cdk.out deploy`, `kubectl --kubeconfig kubectl/cfg apply`) is an ordinary value. For `terragrunt`, `run`, `run-all`, `stack` and `--` may stand anywhere between the program and `apply`/`destroy`.

A flag value can hold blanks and still be one word to the shell: a quoted string of any length, `$(echo x y)`, a backtick pair, `${X:-a b}`, `${{ matrix.dir }}` and `a\ b`. `workflows.normalize_shell` finds these spans in one pass over the text (a stack, not a regex, so an opener with no closer costs nothing more than the delimiters in the text) and adds, behind a separator line, a copy of each line that holds one in which the blanks inside the span are one word. The original text stays beside the copy, so a command inside a span (`sh -c "kubectl apply -f x"`, `$(terraform apply)`) is read as it was written; a quoted string or substitution is also read again as code, to three levels, for the values inside it (`sh -c "kubectl --context ${{ a b }} apply"`). An opener with no closer is an ordinary character and the scan goes on after it. Quotes are paired within their line (a stray apostrophe in a heredoc body or in `echo don't` cannot pair with a quote on another line), and a value that runs over several lines is tried as well; `$'...'` strings take `\'`; a `#` starts a comment only outside quotes, `$(...)` and `${...}`, and a line whose quoting is unbalanced or unclear keeps its `#` (`workflows.strip_comments`). The pass looks at 100,000 delimiters per script at most; a script with more than that is unreadable: `ci_lint` reports `ci.unreadable` (an error) and the doctor counts the workflow as a deploy. Any exception or `RecursionError` in the lint or in the doctor's reader gets the same treatment. A NUL in a script is read both as a separator and as dropped. A span the pass does not close is read as the flag run always read it. `tests/infra/test_ci_rules_review14.py` keeps a generous time bound on 200 KB inputs.

## What the plan role can read

`plan-readonly.policy.json` allows the reads `terraform plan` needs to describe resources and denies secret values (Secrets Manager, SSM parameters, `kms:Decrypt`, objects outside the state bucket, API Gateway API key values). The API key deny covers `/apikeys` and `/usageplans/*/keys`, because `GET /usageplans/{id}/keys` returns each key's `value` too. It cannot hide everything a provider reads to plan: `lambda:GetFunction` returns a function's environment variables, and the state file the role reads holds them as well. Keep secrets out of Lambda environment variables. A project that manages `aws_api_gateway_api_key` or `aws_api_gateway_usage_plan_key` in Terraform has to remove the `NeverReadApiKeyValues` deny knowingly, because the provider reads the key value; the values are then readable by the plan role.

## The posted review comment

`claude-review.yml` passes the review text through `kitlib.redact` (from the default branch's `.claude/scripts`) before it posts it, and posts nothing if the redactor cannot run. A plugin-only install that has no `${CLAUDE_PLUGIN_ROOT}/scripts/kitlib` in the repository must copy `kitlib/` there or edit that step.
