# Deployment surface (B5)

The bot is allowed to push branches and open pull requests. That must not become a deploy nobody approved. A repository can deploy on push, on a tag, on a release, on a `workflow_run`, on a comment, on a schedule, from a host that watches the repository, or from a release-automation bot. Any of these turns a push the bot is allowed to make into a production event.

`python3 .claude/scripts/doctor.py --deploy-surface` inventories them and compares the result with what a human attested.

## What it scans

**Workflows in `.github/workflows/`.** For every job it looks for deploy-class commands: `cdk deploy`, `terraform apply` and `tofu apply`, `sst deploy`, `serverless deploy`, `amplify publish`, `fly deploy`, `wrangler deploy`, `vercel --prod`, `netlify deploy`, `kubectl apply`, `helm upgrade`, `pulumi up`, `aws cloudformation deploy`, `aws ecs update-service`, `aws s3 sync`, `gcloud run deploy`, `az webapp deploy`, `firebase deploy`, `docker push`, `npm publish` and its siblings, `gh release create`, release automation (`release-please`, `changesets`, `semantic-release`), and the well-known deploy actions. It follows one step of indirection: a script a step runs (`./scripts/go.sh`, `python ci/release.py`), a `make`, `just` or `npm run` target, a local composite action, a reusable workflow in the same repository, and an external reusable workflow with a deploy-like name (which it cannot read and says so). The scripts it read join the fingerprint.

It records which events fire the workflow:

| Fires on | Counts as |
|---|---|
| `push` to `main` or `master` only | main |
| `push` to any other branch, exclusion-only branch filters, `branches: ['**']`, a pattern like `main*`, any `tags:`, no filter | **bot-pushable** |
| `release`, `create`, `repository_dispatch`, `issue_comment`, `issues`, `deployment`, `status`, `check_run` and other events the bot's token can cause | bot-pushable |
| `pull_request`, `pull_request_target` (previews) | bot-pushable |
| `workflow_run` | bot-pushable, or main if filtered to main |
| `schedule` | main (cron runs on the default branch) |
| `workflow_dispatch` only | human-started, not counted (the bot has no `actions` scope) |

A job that names an `environment:` has a gate only if that Environment has required reviewers; the doctor cross-checks this against recorded GitHub data when it has it.

**Host and CD configuration files**: `vercel.json`, `netlify.toml`, `amplify.yml`, `fly.toml`, `wrangler.toml`, `render.yaml`, `railway.json`, `Procfile`, `app.yaml`, `firebase.json`, `.do/app.yaml`, GitHub Pages (`CNAME`, `.nojekyll`, or the Pages API), Atlantis, Spacelift, env0, GitOps directories (Flux, Argo CD), and CD systems it cannot read (`.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`, `buildspec.yml`, `azure-pipelines.yml`, ...).

**Infrastructure as code**, to tell you apply is H4 and to apply the platform cap in [rulesets.md](rulesets.md).

## What you attest

The repository cannot show what a host's dashboard does. A human commits an attestation in `.claude/loop.config.md` (H6, through review):

```yaml
deploy_triggers:             # every workflow that deploys, found or declared
  - workflow: .github/workflows/deploy.yml
    fires_on: "push:main"
    gate: gated-by-environment      # or: none, or: manual
git_integrated_hosts:        # hosts that watch the repo; the repo cannot show these
  - host: vercel
    previews_on: label              # none | label | all-branches | pull-requests
    production_on: main             # none | main | <another branch>
    gate: manual                    # none | gated-by-environment | manual
deploy_surface:
  attested_at: "2026-10-26"
  fingerprint: "<the sha256 that --deploy-surface prints>"
```

- `fires_on: none` declares that a workflow the doctor flagged does not deploy (use it for a workflow it could not parse).
- `previews_on: label` means previews start only when a human applies a label (RC-1); branch previews that start for every push are `all-branches` and ungated.
- A host config file with no entry in `git_integrated_hosts` is **unattested** and counts as an ungated deploy. "No git integration" is an entry too: `previews_on: none`, `production_on: none`.
- Even a repository with no deploy files needs the `deploy_surface` stamp once, because dashboards are invisible to the repo.

The fingerprint is a hash of every deploy-surface file (the H3 floor list, workflows, host and CD configs, the scripts workflows run, and `package.json` scripts). When any of them changes, the attestation is **void** until a human re-attests: the doctor says "a deploy-surface file changed" and caps the level at L1. Re-run `--deploy-surface`, read what changed, then update the stamp.

## Caps

| Finding | Effective level |
|---|---|
| Surface not attested, or the attestation is stale | L1 |
| A deploy fires on a bot-pushable ref with gate `none`, or the workflow or host is unattested | **L1** |
| A deploy fires on main with gate `none` | **L4**: no auto-merge (L5 is refused) |
| Everything gated or attested as not deploying | no cap |

Release automation (release-please, changesets) is a deploy trigger on many repositories: its pull requests and tags are H5 and are never auto-merged at any level.

## Gating a deploy properly

Put the deploying job behind an Environment with required reviewers and "prevent self-review" (an Enterprise feature on private repositories), or make it `workflow_dispatch` only. For hosts, turn off branch previews or make them label-triggered, and turn on a manual promote for production. The bot never holds the `actions` or `environments` scope, so it can do neither.

## Limits

The doctor reads the repository, not GitHub's or a host's settings. Attestation is a human claim that the doctor can check only for consistency (an Environment named in a job but missing reviewers in recorded GitHub data; an attested event the workflow does not fire on; a file that changed). The fixture "deploy by side effect" is proven offline: a repo with an `on: push` deploy workflow is refused at L2 by the doctor, and a repo that deploys on merge to main is refused L5. Whether a host's dashboard really matches your attestation is pending-human.
