# 6-infra-ci: infrastructure CI templates, the pre-plan source policy and the infra ci_lint rules

## Changes

- `.claude/scripts/infra/` (new package, Python 3.9, standard library only):
  - `hcl.py`: a careful HCL2 scanner. Comments (`#`, `//`, `/* */`), quoted strings with escapes and nested interpolation, heredocs (`<<` and `<<-`), bare or quoted block labels, one-line blocks, CR/CRLF and the other Unicode line breaks, and the JSON syntax converted to the same tree. Fails closed on anything it is unsure of.
  - `source_policy.py` (IMP-25): the pre-plan bans, run before `init`/`plan` from the base ref's copy: `external`/`http` data sources, every provisioner, unpinned module sources (registry without an exact version, git without a full-commit ref, archives without a checksum, local paths that leave the tree, non-literals), new providers (declared, implied or via `provider =`) against the base ref plus its lock file plus an allowance file read from the base ref, new backend types, endpoint/proxy overrides in provider, backend and remote-state blocks, provider `exec` blocks, `file()`-family reads outside the tree, `terraform_remote_state` over `http`, committed `.terraform/` and `terraform.d/`, provider binaries, symlinks, terragrunt, and `parse.unparsed` for anything it cannot read. `--post-init` also scans the modules `init -backend=false` downloaded. 40 rule ids; see `docs/security/infra-ci.md`.
  - `gate.py`: `authorize` (actors in `factory_approvers`, never a bot), `require-main-ref`, `base-is-main`, `hash-file` / `verify-hash`, `require-run-approval`, `plan-digest`, `plan-summary` (values-free markdown).
  - `ci_rules.py`: 22 `infra.*` rules for `ci_lint.py` (apply trigger, apply gate, saved plan, plan hash, base-is-main, dispatch ref, digest, plan and apply in one job, lock/refresh flags, credentialed plan gate, policy before init, policy from the PR's tree, gate steps that can fail quietly, static cloud keys, and more).
- `.claude/scripts/ci_lint.py`: one call into `infra/ci_rules.py` at the end of `lint_workflow` (ci_lint had no plug-in point) and an import. The rules fire only on workflows that run terraform, tofu, terragrunt, pulumi, cdk, cloudformation, kubectl or helm.
- `.claude/scripts/doctorlib/ci.py`, `levelcalc.py`, `deploy.py` (Phase 1 files, minimal edits): the doctor's `ci.lint` check now also reports the `infra.*` findings and counts them against the L3 "split CI" prerequisite; `hashicorp/setup-terraform`, `opentofu/setup-opentofu`, `terraform-linters/setup-tflint` and the cloud credential/login actions are no longer reported as "uninspectable deploys" (their names matched the deploy heuristic, which would have capped any repository using the shipped `infra-check.yml` at L1).
- `.claude/templates/infra/workflows/`: `infra-check.yml` (untrusted, no secrets, no OIDC), `infra-plan.yml` (preview after a human `plan-ok` label or dispatch, plan-only OIDC role, values-free comment, label removed on new commits), `infra-apply.environment.yml` (post-merge plan of `main`, Environment approval, apply of that plan by hash), `infra-apply.dispatch.yml` (human-only two-step dispatch with a plan digest), `infra-drift.yml` (scheduled plan, issue, never applies).
- `.claude/templates/infra/oidc/` (`aws-plan-main.trust.json`, `aws-plan-main-job-workflow-ref.trust.json`), `iam/state-lock.policy.json`, `policy/plan.rego` + `plan_test.rego` (conftest bundle), `infra-policy.json`, `mcp-deploy-deny.settings.json`.
- `docs/security/infra-ci.md` (new); `docs/security/infra-human-gate.md` (two sentences now point at it).
- Tests: `tests/infra/` (162 tests, 5 s): scanner, about 150 bypass rows against the source policy, 20 planted-change fixture repositories, gate checks, 60+ mutation rows against the shipped workflows, template structure, and the doctor's view of the installed workflows. Fixtures in `tests/fixtures/infra/`.
- `tools/check-refs.allow`: two entries for files the templates are installed as in a user's project.

## Upgrade impact

New files only, plus three small edits to Phase 1 code. A project that already has an infrastructure workflow will see new `infra.*` findings in `ci_lint.py` and the doctor; an `infra.*` error is an unmet L3 "split CI" prerequisite. In particular an apply on push to `main` with no `environment:` is now an error (`infra.apply-ungated`), where before it was only a deploy-surface L4 cap. The setup-action exemption can only remove findings.

## New config keys

None. The source policy's allowance (`allowed_providers`, `allowed_backends`, `allow_version_ranges`, `allow_git_tag_refs`) lives in `.claude/infra-policy.json`, read from the base ref, because the check must read it from a tree the pull request cannot change. `factory_approvers` (registered by `1-boundary`) is used through the repository variable `FACTORY_APPROVERS`.

## Pending-human actions

- Install the templates (`docs/security/infra-ci.md`, "Installing it"): choose one apply variant and install it as `.github/workflows/infra-apply.yml`; set `TF_ROOT`; pin every action to a commit SHA; copy `infra-policy.json` to `.claude/infra-policy.json` and add a `.tflint.hcl`.
- Create the OIDC roles from the four trust files in `.claude/templates/security/oidc/` and the two in `.claude/templates/infra/oidc/`; attach `plan-readonly.policy.json` (and `state-lock.policy.json` to the post-merge plan role). Scope the apply role yourself.
- Set the repository variables (`FACTORY_APPROVERS`, `FACTORY_BOT_LOGIN`, `AWS_REGION`, the role ARNs, `CONFTEST_VERSION`, `CONFTEST_SHA256`), create the Environments with a `main`-only deployment branch policy, and give `production` required reviewers with self-review prevented (Enterprise for private repositories; otherwise use the dispatch variant).
- Add `infra-check` to the required status checks of the main ruleset; make `.github/**`, `infra/**`, `.claude/**` and `.terraform.lock.hcl` CODEOWNERS paths (a code owner who is not the bot).
- Attest the deployment surface: the Environment variant is a deploy on `push:main` with `gate: gated-by-environment`.
- **Unverified live (no cloud account, no network here):** a real plan and a real apply through each workflow; the OIDC roles and their `sub` conditions (including the AWS condition for `job_workflow_ref`, already pending from Phase 1); the shape of `GET /actions/runs/{id}/approvals` that `gate.py require-run-approval` reads (written from the REST documentation); the API-tarball download with `tar --strip-components=1` for a fork PR; conftest's release asset name; that discarding `plan` stdout leaves terraform's errors on stderr; `opa test` on the rego bundle (no `opa` or `conftest` was available, so only its structure is tested).
- Infracost delta (report 8.3): not built; see "Decisions made".

## Docs impact

- `docs/security/README.md`: add `infra-ci.md` to the "Supporting policy" table, and `.claude/scripts/infra/`, `.claude/templates/infra/` to the "Files" table (see pointers).
- `docs/security/split-ci.md`: the sentence "The verify, heldout and infra workflows come with the phases that own them" can link `infra-ci.md` for the infra ones.
- GLOSSARY.md: terms `plan-ok`, preview plan, post-merge plan, apply variant (Environment / dispatch), plan digest, source policy.
- docs/faq.md: "Why can't the agent run `terraform plan`?" (plan executes provider and module code, so it runs in CI with a plan-only role after a policy check and a human label) and "Why does the infra check refuse my new provider / module range?" (a new provider is a separate, reviewed change to `.claude/infra-policy.json`; module versions are exact).

## Decisions made

- **ci_lint integration.** `ci_lint.py` has no rule plug-in point, so it calls one function in `infra/ci_rules.py` and wraps the tuples into its own `Finding`. The new module holds all infra rules and is tested on its own.
- **No `git checkout` of the PR in the privileged trigger.** `ci_lint`'s own rule (`ci.pr-target-head-checkout`) forbids a `pull_request_target` job that checks out the head, so the `fetch` job downloads the PR at the approved sha as an API archive, in a job with `contents: read` and nothing else. That also pins the exact commit the approver saw. `plan` never touches the PR through git; it gets the packed source as an artifact whose sha256 `fetch` recorded.
- **The plan job writes the values-free summary itself; no plan JSON artifact.** A plan prints values, and HCL such as `file("/proc/self/environ")` could put the job's credentials into a value. The plan's stdout is discarded, the plan file and JSON never leave the job, and only a summary of addresses and actions (rendered by the base ref's `gate.py`) is uploaded for the `comment` job. The cost is that trusted code runs after PR HCL in the same job; a hostile result needs a malicious pinned provider, which already holds the plan-only role.
- **Plan and apply are separate jobs.** An Environment approval precedes its job, so one job cannot show reviewers a plan. `ci_lint` enforces this (`infra.apply-same-job-as-plan`).
- **A separate post-merge plan role (`plan-main`).** The preview role has no lock permission, hence `-lock=false`; the post-merge plan must lock, so it gets the same read permissions plus `state-lock.policy.json`, under its own trust subject. The apply role is only reachable from the `apply` job.
- **Two apply variants, one installed file name.** `infra-apply.yml` is the name the Phase 1 `job_workflow_ref` trust policy pins, so the Environment and dispatch variants are alternatives, not a pair.
- **`require-run-approval`.** GitHub creates an Environment with no reviewers the first time a workflow names it, which would let `apply` run unattended. The apply job refuses unless the run's review history has an approval from a listed approver.
- **Dispatch variant = two human dispatches with a digest.** One dispatch cannot show a human the exact plan before applying. `mode=plan` prints a digest; `mode=apply` takes it as input, plans again and applies only on an equal digest. The digest covers resource changes, output changes and variables, not timestamps or tool version.
- **The source policy is strict by default.** Exact registry versions only; git refs must be a full SHA; archives need a checksum; `terragrunt` and files it cannot parse fail. Teams loosen two of these (`allow_version_ranges`, `allow_git_tag_refs`) in the base-ref policy file; they can never allow `external` or `http`. Alternatives considered: warn-only for ranges (rejected: a release published tomorrow would flow into a plan that executes it).
- **No baseline means fail.** New providers and backends are judged against the base ref. With no `--base` the run fails unless `--initial` is given; the first infrastructure PR in a repository is reviewed by hand.
- **Third-party modules downloaded by `init`** are scanned (`--post-init`) with the hard bans as errors and the pinning rules as warnings; a module with a provisioner is refused and must be forked or replaced.
- **Doctor edits.** `deploy.py`'s name heuristic called `hashicorp/setup-terraform` and `setup-tflint` uninspectable deploys. A small exemption list was added rather than weakening the heuristic. The doctor now reports `infra.*` findings through `ci.lint` and counts their errors with `ci.*` for L3.
- **Infracost is not templated.** It needs an API key; placing a secret in a workflow that also handles artifacts built from PR code is a decision for the maintainer. Documented in `infra-ci.md`.
- **No DB-migration workflow.** The migration command is project-specific and `ci_lint` cannot tell an ephemeral CI database from a shared one. The policy (H4, post-merge, same gate) is documented.
- **The rego bundle is TEMPLATE** (IMP-25): structural tests only.
- **Adversarial self-review.** Rows for each attempt are in `tests/infra/test_source_policy.py` (`HCL_ROWS`, `MODULE_ROWS`, `JsonSyntax`, `TreeRules`), `tests/infra/test_ci_rules.py` (`ERROR_ROWS`) and `tests/fixtures/infra/planted/`. A review of my own parser found one real bug (newlines inside an object literal were dropped, so a multi-line `required_providers` entry without commas read as a non-literal); it is fixed and has rows.

## Conductor: pointers to add

- `docs/security/README.md`: "Supporting policy" table -> add a row `Infrastructure CI: source check, preview, apply, drift | [infra-ci.md](infra-ci.md)`; "Files" table -> add `.claude/scripts/infra/` (source policy, gate checks, ci_lint infra rules) and `.claude/templates/infra/` (workflows, OIDC, policy, MCP deny).
- `docs/security/split-ci.md`: the paragraph after the jobs table -> link `infra-ci.md` for the infra workflows; the rules table -> add a line "`infra.*` rules for infrastructure workflows: see infra-ci.md".
- `docs/security/mcp.md`: Rules -> mention `.claude/templates/infra/mcp-deploy-deny.settings.json` as the broader deny set (Phase 2 owns `.claude/templates/settings/`; if it ships a deny snippet, merge the two lists).
- `tools/check-refs.allow`: this phase appends two lines at the end of the file; expect a trivial merge conflict if another phase also appended.
- README.md / getting-started: optional one-line mention that infrastructure CI templates exist; the L3 table already says IaC needs an apply gate.
