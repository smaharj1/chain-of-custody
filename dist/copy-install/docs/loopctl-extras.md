# The loopctl extras: remote lock, claims, manifest, product checks, and the L5 question

Five small features that sit next to the loop. Three are opt-in and stay off until the evidence gate has passed (`docs/baseline/evidence.json`, see [docs/security/README.md](security/README.md)). Nothing here merges anything.

| Feature | Commands | Off by default? | What it is for |
|---|---|---|---|
| Remote lock | `loopctl remote-lock status\|acquire\|renew\|release\|steal` | yes (`remote_lock` empty), evidence-gated | one run per plan across machines |
| Claims | `loopctl claim list\|show\|take\|release` | yes (needs `remote_lock`) | two people, one item |
| Manifest | `loopctl manifest write\|show\|verify` | a command, never automatic | what exactly a piece of the loop ran on |
| G10 product checks | `gates.g10`, `g10_*_cmd` | yes (`gates.g10: off`), evidence-gated | accessibility, performance, PII, observability, with your own tools |
| L5 question | `loopctl merge-class ITEM` | `merge_policy: human` | would this item be low-risk enough, if auto-merge existed |

The design is in [.claude/spec/loopctl-extras.md](../.claude/spec/loopctl-extras.md). The keys are in `loop.config` under "Loopctl extras" and are read from the base sha, never from the branch under judgement.

## Remote lock: one run per plan, across machines

The local run lock covers one machine. If two laptops, or a laptop and a cloud routine, can run the same plan, set

```yaml
remote_lock: origin          # the name of a git remote, never a URL
```

and commit it through review (it is a guardrail key). A `runner: driver` run then takes `refs/loopctl/lock/<plan>` on that remote when it starts, heartbeats it while it runs, and gives it back when it ends. A run that cannot take it does not start (`loopctl run --dry-run` shows the `remote-lock` row). Taking it is create-only: if two machines ask at the same moment, git applies one push and refuses the other.

These four are **human-only**: you run them in a plain terminal (or with `!` in the prompt); the guard denies them to every agent, the conductor included, and they refuse inside a Claude Code session. With `runner: sequential` the conductor prints each as a `HUMAN STEP` line and waits. Claims (`claim take|release`) are the same.

By hand, for the prose conductor (`runner: sequential`) or to look:

```
python3 .claude/scripts/loopctl remote-lock status          # who has it, how old; never the token
python3 .claude/scripts/loopctl remote-lock acquire         # take it (this machine keeps the proof of holding it)
python3 .claude/scripts/loopctl remote-lock renew           # a long sequential session should do this now and then
python3 .claude/scripts/loopctl remote-lock release
```

**A stuck lock.** A lock is stale when its heartbeat is older than `lock_stale_min` (default 60) **and** its owner is gone: a dead process on this machine, or, for another machine, you saying so:

```
python3 .claude/scripts/loopctl remote-lock steal                  # this machine's own dead run
python3 .claude/scripts/loopctl remote-lock steal --host-dead      # you know the other machine is gone
python3 .claude/scripts/loopctl remote-lock steal --corrupt        # the ref is not a lock record at all; look at it first
```

A lock whose heartbeat lapsed but whose process is alive on the same machine is not stolen: a slow gate is not a dead run. If a lock names this machine and a process id that now belongs to something else, no command will take it (the same rule as the local lock); delete the ref yourself with `git push <remote> :refs/loopctl/lock/<plan>` after you have looked. Agents cannot: the guards deny every push of `refs/loopctl/*` to them.

**What it is not.** It is advisory coordination, not a security boundary. GitHub rulesets cover branches and tags only, so anyone with `contents: write` can move or delete these refs, and a well-formed forgery looks like a real record. It keeps cooperating machines out of each other's way. Hosts that reject custom refs can use the fallback in [docs/bootstrap.md](bootstrap.md): a `factory:running` label and an assignee on the tracking issue.

## Claims: two people, one item

Before you start an item by hand, or want the loop to leave it alone:

```
python3 .claude/scripts/loopctl claim take F2 --by alice --note "doing the schema first"
python3 .claude/scripts/loopctl claim list
python3 .claude/scripts/loopctl claim release F2 --by alice
```

The first claim wins. Someone else asking gets your name back and no claim. Claiming again under the same name renews it. A claim lapses after `claim_ttl_h` hours (default 72); then anyone can take it, and the new claim names the old holder. `--override` replaces a live claim (it is recorded as an override). The driver never takes a claim; it skips an item that someone else claimed and, when nothing else can run, stops and says which items wait on whom.

Names are labels you type (`--by`, or `LOOPCTL_USER`, or your OS user). A driver run counts as you (`LOOPCTL_USER` or your OS user), so claim under that name to have your own run work on the item. Nothing proves who typed a name, so two people who type the same name look like one person. A claim is not an approval: approvals are still `factory_approvers` and `loopctl approve`.

## The reproducibility manifest

```
python3 .claude/scripts/loopctl manifest write F1       # one item (omit F1 for the whole plan)
python3 .claude/scripts/loopctl manifest show
python3 .claude/scripts/loopctl manifest verify         # latest; or NAME, or --file PATH
python3 .claude/scripts/loopctl manifest verify --expect-hash <hash from the PR's audit block>
```

It records, sealed with a hash: the base and head commits, the kit version, the hash of `loop.config` at the base and every gate's mode, the plan and item digests, the toolchain pins and lockfile object ids at the head, the kit-owned verify commands, the evidence file, what the state recorded for the gates, and the machine's Python and platform. `verify` checks the seal (any edit, including to the recorded hash, breaks it) and then re-reads everything git can re-derive at the recorded commits, so a manifest that was edited and re-sealed still cannot claim a config or a lockfile those commits do not hold. The parts git cannot re-derive (the gate results, the environment) are protected by the seal, and, if you pass `--expect-hash` from somewhere the writer cannot edit (the PR's audit block), by that. It is not a signature: it answers "what was this run on?", not "who says so?". `loopctl audit render ITEM` puts the stored hash on a `Manifest:` line in the PR's audit block, so the hash lives where the manifest's writer cannot edit it; `loopctl audit check` fails (`audit.manifest-broken`) when the stored manifest does not verify (see [delivery.md](delivery.md)).

## Product checks (G10)

Off by default and evidence-gated. You supply the tools; the kit bundles none. Up to four named checks:

```yaml
gates:
  g10: report                      # on | report | off
g10_a11y_cmd: python3 tools/g10-a11y.py
g10_perf_cmd: tools/g10-perf.sh --budget=250
g10_pii_cmd: python3 tools/g10-pii.py
g10_observability_cmd: python3 tools/g10-obs.py
g10_timeout_s: 600
```

Each is split into a program and arguments and run **without a shell**: semicolons, pipes, redirects, `$`, backticks, parentheses, braces, `*`, `?` and `!` in the string are refused, and a program or argument that is a file this item changed is refused (an item cannot rewrite the check that judges it). Wrap anything fancier in a script of yours that the item cannot edit. The command is given `LOOPCTL_G10_SCOPE`, a JSON file listing the changed non-test, non-document files, and must print one line last:

```json
{"schema": "g10.v1", "check": "a11y", "checked": 12, "violations": 0}
```

The gate decides: `checked` at least 1 (a check that looked at nothing proves nothing) and `violations` 0. Optional `details` (a short list of strings) is shown, not trusted. A result for another check, a missing line, a count that is not a whole number or any extra field is a failure. `.claude/templates/github/workflows/g10-product.yml` is the CI half; it runs the kit from the base sha in a job with no secrets. Contract tests against `api-contract.md` are design-only; see the spec.

## The L5 question: `loopctl merge-class`

```
python3 .claude/scripts/loopctl merge-class F1
python3 .claude/scripts/loopctl merge-class F1 --paths docs/guide.md      # what if the change were only this
```

It prints, rule by rule, whether item F1 would be eligible for a low-risk lane **if auto-merge existed and were switched on**, and exits 0 for yes, 1 for no. It merges nothing, opens nothing and never runs `gh`. `merge_policy` is `human` by default and `human` is the only value that does anything: a person merges every pull request. `auto-low-risk` is an opt-in the registry and the doctor refuse until the evidence gate, the promotion bar (`promotion_attested`), mutation testing (`gates.g4: on`) and B5 all hold, and even then this build merges nothing; enabling is design-only.

The rules: the policy is `auto-low-risk`; the level is L5 and the doctor agrees; the evidence gate passed; the promotion bar is attested; G4 is on; **no ungated deploy** (a repo that deploys on merge to main without an Environment approval is never eligible, because there even a docs merge is a production deploy); every changed file is low-risk (documents, plus the globs you list in `low_risk_paths`, and never anything in `always_human_paths`, migrations, infra, auth, payments, lockfiles, CI or test config, `.github/`, `.claude/`, or a symlink); the item is `riskClass: low` with no migration, schema, dependency or sensitive-domain footprint; every gate that is on passed for this head; no CI-fix commit; and anything user-visible names a feature flag in `ext.featureFlag`.

`python3 .claude/scripts/doctor.py` shows the same thing from the other side: `l5.merge-policy` and `l5.b5`, plus the two new L4 prerequisites (a working `notify_command` and an external watchdog that has checked in) and `lock.remote`, `g10.config` and `fork.env` (a probe assumption: V18).

## When something looks wrong

| You see | It means | Do |
|---|---|---|
| `remote lock ... is held by NAME on HOST` | another machine has the plan | wait, or `remote-lock status` and talk to them |
| `... is not a loopctl lock` | the ref holds something else (a hand edit, a forgery) | look at it with `git fetch <remote> refs/loopctl/lock/<plan>` and `git show FETCH_HEAD`; then `steal --corrupt` |
| `the remote has not answered a heartbeat` and the run stopped | the remote was unreachable for half the lock's time limit | fix the connection; the run stopped on purpose before another machine could take the lock |
| `ITEM is claimed by NAME` | someone is on it | ask them, or `claim take ITEM --override` |
| `manifest ... DOES NOT HOLD` | the file was edited, or it describes commits this repository does not have | `manifest verify --json` lists each field and what git holds |
| `g10.tool-edited` | the item changed the check that judges it | put the check where the item cannot change it |
| `merge-class` says not eligible | one or more rules fail | read the rule list; that is the point |
