# Infrastructure: apply is always a human decision

**No autonomy level unlocks infrastructure apply, deploy or destroy (H4).** The agent writes infrastructure code. CI checks the source, makes a plan after a policy check, and a human approves the plan that gets applied. This page states that as policy; the CI templates that implement it are in the infra CI phase, and the doctor already refuses to let the pieces it can see go wrong (IMP-25).

## The rule

1. **The agent never holds a cloud credential.** Not read-only, not for a "quick plan". `terraform plan`, `cdk diff`, `cdk synth` and `pulumi preview` execute provider and program code, so running them with any credential is running arbitrary code with that credential. The agent runs only `fmt`, `validate`, `tflint`, `init -backend=false`, `cfn-lint`, `cfn-guard`, and `cdk synth` only in a sandbox with no credentials and no network.
2. **Plans run in CI, after a static check.** An untrusted job with no secrets and no OIDC token checks the source: format, validate, lint, a ban on `external` and `http` data sources, `local-exec` and `remote-exec` provisioners, unpinned module `source =` and new `required_providers`, plus checkov or tfsec. Only after that, and only after a human applies a `plan-ok` label or dispatches the workflow, does a plan-only role (reached through OIDC) make a **preview plan** for the pull request. The plan file is not kept.
3. **The applied plan is the post-merge plan of `main`.** After merge, CI makes a fresh plan of `main` with state lock and refresh on, policy runs on it, a human approves that exact plan, and CI applies it **by hash in the same run**. Apply refuses a plan whose base sha is not the current `main`.
4. **Apply needs a human.** Use an Environment with required reviewers and "prevent self-review" where your GitHub plan offers it (Enterprise on private repositories). Otherwise use a `workflow_dispatch` the bot cannot trigger (it has no `actions` scope), run by a person in `factory_approvers`, or apply from your own machine outside the agent's OS user. The doctor then labels the project "apply gate = human dispatch" and caps infrastructure projects at **L3** ([rulesets.md](rulesets.md)).
5. **Migrations against a shared or persistent database are H4 too**, behind the same gate, post-merge. The loop migrates only a local, ephemeral database. Development and production database credentials are separate secrets, never the same.
6. **Nothing else in the repository deploys on the agent's push or merge without a human** ([deploy-surface.md](deploy-surface.md)).
7. **Deploy-shaped MCP tools are denied** ([mcp.md](mcp.md)).

## OIDC trust

Apply credentials exist only in CI, reached through OIDC, never as stored secrets. The trust policy pins the subject:

| File (in `.claude/templates/security/oidc/`) | Subject condition | Use |
|---|---|---|
| `aws-apply-environment.trust.json` | `repo:ORG/REPO:environment:production` | apply, behind an Environment with reviewers |
| `aws-apply-job-workflow-ref.trust.json` | `repo:ORG/REPO:job_workflow_ref:ORG/REPO/.github/workflows/infra-apply.yml@refs/heads/main` | apply, where Environments are unavailable: pinned to the dispatch-only workflow on main |
| `aws-plan-preview.trust.json` | `repo:ORG/REPO:environment:plan-preview` | the preview plan, plan-only role |
| `aws-drift-readonly.trust.json` | `repo:ORG/REPO:environment:drift` | the scheduled drift plan, read-only role; opens an issue, never applies |

Never `repo:ORG/REPO:*` and never a `ref:` subject: both let a pull request, or any branch the bot can push, assume the role. The examples use placeholders (`acme/widgets`, account 123456789012); replace them.

The `job_workflow_ref` form needs the claim in the subject. Customize the repository's OIDC `sub` claim to include it (GitHub's `actions/oidc/customization/sub` API with `use_default: false` and `include_claim_keys: ["repo", "job_workflow_ref"]`); otherwise use IAM condition keys for additional claims if your account supports them. Whether your account does is untested here: confirm with a test assume-role before trusting it.

`.claude/templates/security/iam/plan-readonly.policy.json` is an example plan-only permissions policy: metadata reads, state read, and an explicit deny on secret values. It is an example, not an exhaustive policy.

## CI job split

Which job may hold what is in [split-ci.md](split-ci.md). `ci_lint.py` enforces the parts it can: no job with `id-token: write` checks out PR code, no secrets in a pull request job.

## What the doctor checks

| Check id | |
|---|---|
| `deploy.iac` | infrastructure as code present (apply is H4) |
| `gh.environments` | Environment reviewers and self-review prevention, or "apply gate = human dispatch" |
| `cred.*` | no cloud credential reachable |
| `ci.oidc-with-pr-code` | `id-token: write` together with PR code |
| `deploy.surface` | an apply that runs on push |

Not built here, because they need a live cloud account: the plan, policy and apply workflows themselves, and drift detection. They are TEMPLATE work in the infra CI phase and are applied by a human.
