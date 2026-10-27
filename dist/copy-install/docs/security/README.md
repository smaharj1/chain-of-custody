# Security: the real boundary

This directory is the human's guide to the security model of the Chain of Custody kit (report section 5). Read this page first, then the page for the part you are setting up.

## The one idea

Assume any agent that holds a Bash tool can run arbitrary code as the OS user it runs as. Hooks, permission deny rules, `tools:` lists and prose rules are guardrails: they catch honest mistakes and say why. They are not the boundary. The boundary is what still holds after an agent has escaped all of them.

It is built from five things the agent cannot change from inside its session:

| | Element | What it stops | Page |
|---|---|---|---|
| B1 | **Identity.** The agent acts on GitHub as a bot, never as you | The agent approving its own gate, merging as admin, editing workflows | [bot-identity.md](bot-identity.md) |
| B2 | **Credentials.** No mutating cloud, database, deploy or GitHub credential is reachable by the agent's OS user, yours included | Infrastructure changes by any route; pushing or merging as you | [credentials.md](credentials.md) |
| B3 | **OS sandbox or container**, mandatory from L2 | Interpreter walk-arounds, git-config escalation, credential reads | [sandbox.md](sandbox.md) |
| B4 | **Server-side policy.** Rulesets where the bot never bypasses, CODEOWNERS, required checks, Environments, OIDC subjects, split CI | Everything the agent pushes being trusted before review; CI as a code-execution path | [rulesets.md](rulesets.md), [split-ci.md](split-ci.md) |
| B5 | **Deployment surface.** Every path by which a commit becomes a deploy is inventoried and gated | A bot push, a docs merge, a revert or a tag becoming a production deploy with no human | [deploy-surface.md](deploy-surface.md) |

Infrastructure apply, deploy and destroy are never automated at any level (H4). The agent writes infrastructure code; CI plans it after a policy check; a human approves the apply. See [infra-human-gate.md](infra-human-gate.md).

## Supporting policy

| Topic | Page |
|---|---|
| Secrets: where they may live, what is ignored, what never appears in artifacts | [secrets-policy.md](secrets-policy.md) |
| Dependencies and supply chain | [supply-chain.md](supply-chain.md) |
| MCP is a tool plane | [mcp.md](mcp.md) |
| Untrusted content and prompt injection | [untrusted-content.md](untrusted-content.md) |
| Git escalation paths | [git-escalation.md](git-escalation.md) |
| Claude Code version floor, range, pin and canary | [version-policy.md](version-policy.md) |
| Running an unknown repo's commands for the first time | [first-run-probe.md](first-run-probe.md) |

## The doctor

`python3 .claude/scripts/doctor.py` checks all of this and prints the autonomy level that is safe right now. It is standalone (Python 3.9 or newer, standard library only) and runs from any project the kit was copied into.

```
python3 .claude/scripts/doctor.py                    everything, human-readable
python3 .claude/scripts/doctor.py --json             the doctor.v1 document
python3 .claude/scripts/doctor.py --credentials      B2 only
python3 .claude/scripts/doctor.py --deploy-surface   B5 only, with the fingerprint to attest
python3 .claude/scripts/doctor.py --level L3         the effective level if you configured L3, and every cap
python3 .claude/scripts/doctor.py --gh-fixture FILE  GitHub facts from recorded gh api JSON
python3 .claude/scripts/doctor.py --list-checks      every stable check id
```

`--live` adds the checks that talk to other programs or the network (`aws sts get-caller-identity` per profile, `gh auth status`, `git credential fill`, `ssh -T git@github.com`, and a probe of the metadata endpoints). It is for a human at a plain terminal and refuses to run inside a Claude Code session. Agents never run it.

The doctor never prints a secret value. It prints names and paths.

### The effective level

```
effective = min(configured, prerequisites, credentials, sandbox, MCP, B5, platform, evidence, version)
```

The doctor prints every term, whether or not it binds, with the reason. Out of the box, with nothing attested, the result is **L1**: a supervised local loop with better guards. That is the safe reading of the report, and it is on purpose. Each step up needs something a human has to do and attest:

| Level | Name | Needs (the doctor checks each) |
|---|---|---|
| L0 | Assist | nothing |
| L1 | Supervised local loop | nothing |
| L2 | Push-branch | a bot identity (a machine-user PAT that expires within 14 days is allowed here); a clean credential audit that includes your own gh, git and SSH credentials; the sandbox configured fail-closed and attested; the deployment surface attested with no ungated deploy on a bot-pushable ref; a liveness canary; held-out tests run by the driver or only in CI; GitHub, with protection on the default branch |
| L3 | Draft-PR factory | L2, plus the bot as a GitHub App; rulesets where the bot never bypasses; code-owner review; required checks (verify, evidence, secrets scan); CODEOWNERS covering the guardrail paths; split CI; no web tools for the conductor; managed settings or a credential-free container; for infrastructure projects, an apply gate the bot cannot trigger |
| L4 | Async factory | L3, plus the evidence gate (a human-committed `docs/baseline/evidence.json`), a tested Claude Code version, a spend ceiling, loopctl installed |
| L5 | Low-risk auto-merge | L4, plus the promotion bar attested, mutation testing on, and no deploy on main without a human gate |

Two caps hold at every level. A credential the agent can reach caps at L1. A deploy that fires on a bot-pushable ref with no human gate caps at L1; one that fires on main with no human gate caps at L4.

### What the doctor cannot see

It sees what is stored and what is configured. It cannot see what your running processes hold, what an unlocked keychain will hand over, or what a dashboard setting does. Those are called out as `unknown` and need an attestation or an isolation choice: see [credentials.md](credentials.md) and [deploy-surface.md](deploy-surface.md).

## What this kit ships and what only you can do

Shipped and tested offline: the doctor, `ci_lint.py`, the SessionStart canary hook (`session_start.py`), the sandboxed first-run probe (`probe_run.py`), ruleset JSON, the CODEOWNERS template, OIDC trust policy examples, a devcontainer template, a managed-settings example, a sandbox snippet, an MCP lockdown example, a label-guard workflow, and two dry-run-by-default scripts for the GitHub side.

Only a human can: create the bot and its credential, apply the rulesets (`.claude/scripts/github/apply-boundary.sh`, dry run first), attest the deployment surface and the sandbox, install managed settings, pin the Claude Code CLI, run the canary, and commit the evidence file. Nothing in this kit merges, applies, deploys or runs `claude`. The live halves of the boundary fixtures (the real ruleset rejecting a bot push, a real CI job not creating a marker file) are verified by `.claude/scripts/github/verify-boundary.sh` and by hand, and are listed as pending in the changelog fragment.

## Files

| Path | What it is |
|---|---|
| `.claude/scripts/doctor.py`, `.claude/scripts/doctorlib/` | the doctor and its checks |
| `.claude/scripts/ci_lint.py` | the workflow linter for the split-CI rules |
| `.claude/scripts/session_start.py` | SessionStart: version check and liveness canary |
| `.claude/scripts/probe_run.py` | the sandboxed first-run probe |
| `.claude/scripts/github/` | `apply-boundary.sh`, `verify-boundary.sh`, `boundary.py` |
| `.claude/templates/security/` | rulesets, CODEOWNERS, OIDC, IAM, devcontainer, managed settings, sandbox, MCP, gitignore |
| `.claude/templates/github/workflows/label-guard.yml` | removes approval labels applied by non-approvers |
