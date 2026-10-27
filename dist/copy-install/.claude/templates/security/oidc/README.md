# OIDC trust policies (AWS)

These are examples. Replace the account id, the repository and the names, and read `docs/security/infra-human-gate.md` first.

| File | Subject it trusts | For |
|---|---|---|
| `aws-plan-preview.trust.json` | `environment:plan-preview` | the preview plan role |
| `aws-drift-readonly.trust.json` | `environment:drift` | the drift role |
| `aws-apply-environment.trust.json` | `environment:production` | the apply role of the Environment variant of `infra-apply.yml` |
| `aws-apply-job-workflow-ref.trust.json` | `job_workflow_ref:<repo>/.github/workflows/infra-apply.yml@refs/heads/main` | the apply role of the dispatch variant |

## Pick one apply trust, not both

`aws-apply-environment.trust.json` and `aws-apply-job-workflow-ref.trust.json` are alternatives. Use the one that matches the apply workflow you installed
(the Environment variant, or the dispatch variant). Do not create both roles for the same account and do not merge the two statements into one role: an
apply role that trusts two subjects is reachable through whichever gate is weaker.

## The `job_workflow_ref` trust needs a repository-wide customisation

GitHub puts `repo:OWNER/REPO:environment:NAME` or `repo:OWNER/REPO:ref:refs/heads/BRANCH` in the `sub` claim. It puts `job_workflow_ref` in `sub` only
after you change the repository's OIDC subject claim template to include it (`use_default: false`, `include_claim_keys: ["repo", "job_workflow_ref"]`).
That template is per repository (or organisation), not per role, so changing it rewrites the `sub` of **every** job in the repository:

* the Environment-style subjects (`...:environment:plan-preview`, `...:environment:drift`, `...:environment:production`) stop appearing in tokens, so every
  trust policy that names one stops matching;
* every other role in the repository then needs a trust written against the new subject shape (`repo:OWNER/REPO:job_workflow_ref:...`), which is
  pinned to a workflow file instead of an Environment.

So the dispatch variant is a repository-wide choice. If you are not ready to move all of your trust policies, use the Environment variant. The
doctor cannot see your cloud account: whichever you choose, check the trust policy by hand (a person reads every apply role before it is created).

None of this changes who approves an apply. A person approves every infrastructure apply, in the Environment (reviewers) or at the dispatch
(`gate.py authorize`), and these files only decide which workflow can obtain the role.
