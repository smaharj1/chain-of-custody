# Rulesets, CODEOWNERS and platform requirements (B4)

Server-side policy is the part of the boundary the agent cannot reach from inside a session. This page says what to configure, what the doctor checks, and what a repository plan can and cannot offer.

## Rules of the model

1. **The bot is never a bypass actor.** Not on any ruleset, not through a role it holds, not as an App.
2. **A solo maintainer may bypass only in `pull_request` mode.** So you can merge your own change to a guardrail path, and every bypass is logged. Set `solo_maintainer: true` in `.claude/loop.config.md` to tell the doctor this is a one-human install.
3. **A team of two or more humans uses no bypass actors at all** (`solo_maintainer: false`, the default).
4. Required review, required status checks, code-owner review and a block on force pushes and deletion apply to the default branch and to `integration/**` branches.
5. Changes to `.claude/`, `.mcp.json`, `.claude-plugin/`, `plugin/`, skill directories, `scripts/`, `.github/`, infrastructure code and the deploy-surface floor need a code owner who is not the bot.

## Applying it

The templates are in `.claude/templates/security/rulesets/`:

| File | Use |
|---|---|
| `main-protection.team.json` | Branch ruleset, no bypass actors |
| `main-protection.solo.json` | Same, with the repository admin role bypassing in pull_request mode only |
| `tags-protection.solo.json`, `tags-protection.team.json` | Tags are releases (H5); creating, updating or deleting one needs a human. The team file needs a release team id |

Apply them with the script, which is a dry run unless you add `--apply`, prints every `gh api` call it would make, and refuses to run inside a Claude Code session:

```
.claude/scripts/github/apply-boundary.sh --repo OWNER/NAME --variant solo
.claude/scripts/github/apply-boundary.sh --repo OWNER/NAME --variant solo --apply
.claude/scripts/github/verify-boundary.sh --repo OWNER/NAME --run
```

`apply` also sets the squash-merge commit to "PR title and description" (so the audit block in the PR body survives a squash, CS-20), makes workflow tokens read-only and unable to approve pull requests, and can create one Environment with reviewers and self-review prevention. It does not create the bot, and it does not write CODEOWNERS: copy `.claude/templates/security/CODEOWNERS.template` to `.github/CODEOWNERS`, replace every placeholder, and commit it through a pull request.

`verify-boundary.sh` records the settings as a fixture (`gh api` GET calls only), then runs the doctor on it. The doctor's GitHub checks, offline:

| Check id | Fails when |
|---|---|
| `gh.main-protection` | no ruleset or protection on the default branch requires a pull request |
| `gh.required-review` | fewer than one approving review is required |
| `gh.codeowner-review` | code-owner review is not required (the effective level is at most L2) |
| `gh.last-push-approval` | the most recent push need not be approved by someone else |
| `gh.required-checks` | verify, evidence and a secrets scan are not all required |
| `gh.required-checks-source` | a required check accepts a result from any app |
| `gh.bypass` | the bot, any app, a team, a write or maintain role, or (on a team install) anyone can bypass; or a solo admin bypasses in `always` mode |
| `gh.environments` | no Environment has required reviewers, or one allows self-review |
| `gh.actions-permissions` | workflow tokens are not read-only, or workflows may approve PRs |
| `gh.squash-setting` | squash commits do not use the PR title and description |
| `gh.bot-permissions` | the bot holds a permission it must not (see [bot-identity.md](bot-identity.md)) |

## Platform requirements (private repositories)

Every server-side control is a GitHub feature, so **L2 and above need a GitHub-hosted repository**; another forge stays at L1 until this table has rows for it. For a private repository the doctor reads visibility and plan from the API when it can, and otherwise asks you to attest. Unknown means unavailable.

| Feature | Used for | Private repo needs | Fallback when missing |
|---|---|---|---|
| Environment required reviewers, prevent self-review | H4 apply, H5 deploy, shared-database migrate | **Enterprise.** On Free, Pro and Team, required reviewers are for public repos only | Apply runs only from a `workflow_dispatch` the bot cannot trigger (the bot has no `actions` scope) or from your machine outside the agent's OS user. The doctor labels it "apply gate = human dispatch" and caps infrastructure projects at **L3** |
| Environment wait timers, custom protection rules | optional hardening | same | not needed |
| Environment secrets | scoping apply credentials | Pro, Team or Enterprise | No long-lived apply secret at all: an OIDC role pinned to the dispatch-only apply workflow |
| Rulesets or branch protection: required reviews, required status checks, block direct push | B4, H0, H3 | A paid plan (unverified, V27) | Without protection on the default branch, the bot's contents: write can push to it directly: effective level **L1** |
| Require review from code owners | H3, H6 | same as rulesets (V27) | effective level at most L2 |
| Push rulesets (block paths on push) | optional | unverified (V27) | CODEOWNERS plus required review |
| Merge queue | optional | unverified (V27) | serial merges |
| Rulesets on custom refs | not offered | Rulesets target branches and tags only, so `refs/loopctl/*` is advisory coordination, never a boundary | none |

The features marked unverified are probe items (V27). Until you confirm one on your plan, list it in `platform_attested` in `.claude/loop.config.md`; the doctor then treats it as available but says it did not verify it.

## What the offline checks do not prove

The ruleset templates and the doctor agree with each other (a test applies the templates in imagination and checks the doctor passes them). They do not prove GitHub rejects a bot push: that needs a real repository and a bot credential. `verify-boundary.sh` prints the three live steps to do by hand: a bot push to the default branch must be rejected, a bot must be unable to approve and merge its own pull request, and a pull request that adds a skill with a hook must not create a marker in the claude -p CI job.
