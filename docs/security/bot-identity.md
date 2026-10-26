# Bot identity (B1)

The agent acts on GitHub as a bot. Never as you. Everything server-side in this model (rulesets, required reviews, Environments) assumes the agent's pushes and pull requests come from an identity that has no special rights.

## Which identity at which level

| Level | Identity | Why |
|---|---|---|
| L0, L1 | none: you push yourself | The loop never pushes |
| L2 | A dedicated machine user with a fine-grained personal access token that **expires within 14 days** | Acceptable only here. A PAT is a standing credential, so its lifetime is capped |
| L3 and above | A **GitHub App**, installed on this repository only | Short-lived installation tokens, scoped permissions, an identity that cannot be an admin |
| any | **Never a PAT that belongs to a human** | It carries your rights: admin, bypass, approval |

The doctor reads `bot.kind`, `bot.login`, `bot.id` and `bot.token_expires` from `.claude/loop.config.md`. A machine-user PAT fails if `bot.token_expires` is missing, already past, or more than 14 days away; it warns two days before expiry. `bot.login` must not be the account that `gh` is logged in as on this machine unless that account is the bot. A GitHub App's login ends in `[bot]`; the doctor warns if it does not.

## Permissions

Grant contents: write and pull requests: write. Nothing else.

| Permission | Why it must be absent |
|---|---|
| workflows | The bot could edit CI that runs with your tokens |
| actions | The bot could dispatch the human-only apply workflow, and could read logs (held-out test output, RC-6) |
| administration | Rulesets, branch protection, deploy keys |
| environments | The bot could approve its own Environment gate |
| secrets | Reading or writing Actions secrets |
| deployments, pages, repository hooks | Deploy-shaped side effects (B5) |
| checks, statuses | An app with checks: write can post a passing check run and satisfy a required check. Pin each required check to the Actions app (`integration_id`) as the ruleset templates do |
| members and organization permissions | Not needed |

`python3 .claude/scripts/doctor.py --gh-fixture FILE` reads the installation's permissions from `repos/{owner}/{repo}/installation` when that response is in the fixture (it needs the App's own credentials, so `verify-boundary.sh` records a 404 for most people; check the App's settings page by hand then).

## Self-review and bypass

- The bot is never a bypass actor in any ruleset (see [rulesets.md](rulesets.md)). The doctor names the bot if it is, by `bot.id`.
- The bot is never a code owner. `codeowners.bot` fails if its login appears in CODEOWNERS.
- Turn on "prevent self-review" for every Environment that has reviewers, and `require_last_push_approval` in the pull request rule, so the identity that pushed cannot be the one that approves.
- Workflows must not be able to approve pull requests: `apply-boundary.sh` sets `can_approve_pull_request_reviews` to false.

## Setting it up

1. Create the App (or the machine user) in your account or organization. Install it on this repository only.
2. Copy its numeric id and login into `.claude/loop.config.md` as `bot.kind`, `bot.login`, `bot.id` (and `bot.token_expires` for a PAT). This file is H6-protected: change it through review.
3. Keep the bot's credential away from the agent's own environment variables where you can. The loop's driver passes it to the process that pushes, not to the agent (the L2 push is made by the human-started process, never by an agent).
4. Run `python3 .claude/scripts/doctor.py --level` and read the `bot.identity` line.

Creating the App, minting a token and installing it cannot be done by this kit; it is pending-human by design.
