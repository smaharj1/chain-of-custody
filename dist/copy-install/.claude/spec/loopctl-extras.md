# loopctl extras: remote lock, claims, manifest, G10, L5 classifier

**Status**: Phase 11b. Implements report sections 10.3 "Remote lock" and 19.7 "Lock (Remote)" (RC-10, RC-12), 9 G10 (CO-25), 16.1 row 11 (F-25 multi-human claims, F-26 reproducibility, L5 for low-risk classes), 6.2 L5 and 6.1 H3, B5 (RC-1), and DEC-3, DEC-33, DEC-38. IMP-1 forms: the remote lock, claims and G10 are **OPT-IN** and **evidence-gated**; the manifest is a command; the L5 classifier is **built, with enabling DESIGN-ONLY**. Operator guide: `docs/loopctl-extras.md`. Config keys: `kitlib/schema/11b-loopctl-extras.json` (and `gates.g10` in `4b-gates.json`, next to `gates.g4`).

Nothing here merges anything. `merge_policy` is `human` unless the base sha's config says otherwise, no code path runs `gh pr merge` or merges to the default branch, and `guards/g_git.py` denies `gh pr merge` and every push of `refs/loopctl/*` to every agent.

## Commands (all in `loopctl/extras.py`, classified in `cli.py` and `guards/g_exec.py`)

| Command | Class | What it does |
|---|---|---|
| `remote-lock status` | READ | the lock on the remote, without its token |
| `remote-lock acquire \| renew \| release \| steal` | Tier 1 write | compare-and-swap pushes of `refs/loopctl/lock/<plan>` |
| `claim list \| show ITEM` | READ | the claims on the plan |
| `claim take \| release ITEM` | Tier 1 write | create-only / lease-guarded pushes of `refs/loopctl/claim/<plan>/<item>` |
| `merge-class ITEM` | READ | would this item be eligible for the L5 low-risk lane; exit 0 yes, 1 no |
| `manifest show \| verify` | READ | a reproducibility manifest, and its check against git |
| `manifest write [ITEM]` | Tier 1 write | writes `<git-common-dir>/loopctl/manifests/<plan>-<item>-<head>.json` |

`remote-lock` and `claim` need `remote_lock: <remote name>` and the evidence gate. The remote is a configured NAME; a URL, an option-looking string or an unconfigured name is refused before git is asked.

## The remote lock (`remote_lock.py`)

One run per plan across machines. The ref `refs/loopctl/lock/<plan>` points at a commit whose tree is exactly one file, `lock.json`, with the lease record shape of `parallel/mutex.py`: `{schema, plan, name, token, owner, pid, host, acquired, hb, ttl_s, meta, runId?}`. History on the ref is the audit trail (acquire, heartbeats, takeovers).

- **Create-only.** `git push --force-with-lease=<ref>: <remote> <new>:<ref>` (`kitlib.gitutil.push_cas` with no expected value): "must not exist". Two machines that both read a free lock cannot both win; the loser is told so. Heartbeat and takeover are the same push with the expected value set to the commit just read; release is a lease-guarded delete. A bare `--force-with-lease` is never used.
- **Reading** fetches the ref into `refs/loopctl/cache/...` and judges that, in two git calls (fetch, one batched read). Anything that is not exactly a valid record is `corrupt`: a blob or tag at the ref, a tree with a second file or another name, JSON that is not strict (duplicate keys, NaN), a record for another plan, an unknown field, a heartbeat more than 10 minutes in the future (it would never expire), a ttl outside 60 s to 7 days. **Corrupt counts as held.** `steal --corrupt` replaces it, after a human has looked.
- **Stale** means the heartbeat is older than the record's ttl (`lock_stale_min`, default 60 minutes, never shorter than 60 s) **and** the owner is gone: a dead pid on this host, or (another host) a human saying `--host-dead`. A lapsed heartbeat whose owner is alive here is **not stale**: a slow gate is not a dead run. A lock from another host is never judged from here. `--after` can only lengthen the wait.
- **Holders find out.** `renew`, `holds` and `release` compare the token, so an owner whose lock was taken over gets `False`, never an overwrite. Inside a driver run, `Keeper` heartbeats every ttl/4 through the driver's `stop_hooks`: a lost lock stops the run at once (`remote_lock_lost`); an unreachable remote stops it only after half the ttl without an answer (`remote_lock_unreachable`), well before another machine could take the lock.
- **Wiring.** `startrun.preflight` prints a `remote-lock` row and a `claims` row when `remote_lock` is set (a run that cannot take the lock does not start). `startrun.start` takes the remote lock after the local one for `runner: driver` and gives it back before. The prose conductor (`runner: sequential`) takes and releases it by hand with `remote-lock acquire` and `release`; nothing heartbeats it there, so a long sequential session should `renew` it.
- **Advisory, not a boundary (RC-12).** Rulesets cover branches and tags only. Anyone with `contents: write` can move or delete the ref, and a well-formed forgery cannot be told from a real record. The lock coordinates cooperating machines. Fetching a forged ref downloads whatever it names and the read holds it in memory; a very large forged object is a nuisance for someone who could simply delete the ref anyway, not a bypass.
- **Fallback** for a host that rejects custom refs: a `factory:running` label and an assignee on the tracking issue (`docs/bootstrap.md`).
- **Tested against a local bare remote only**, with two clones of it standing for two machines. Never against a real remote.

## Claims (`claims.py`, F-25)

One ref per item, `refs/loopctl/claim/<plan>/<item>`, same shape (`claim.json`: `{schema, plan, item, by, token, host, claimed, expires, ttl_s, note?, over?, override?}`). The first push wins (create-only). The same `by` claiming again renews. `claim_ttl_h` (default 72) later anyone may take it over; the new record names the old holder (`over`). `take --override` replaces a live claim and says so (`override: true`, `over`). `release` is by the holder or `--override`. An unreadable claim ref counts as claimed. A claim that would expire further ahead than 30 days is corrupt (it would never lapse).

The driver never takes a claim. `Driver.eligible_now` removes the items others claimed (`remote_lock.ClaimsFilter`: asks the remote at most every 30 s, falls back to the last answer for 10 minutes, then to "nothing is eligible", never to "everything is"), in the serial driver and in the parallel scheduler. Names are labels: `LOOPCTL_USER` or the OS user. Two people who type the same name look like one person renewing. The human approvers of the plan are still `factory_approvers`; a claim is not an approval.

## The L5 classifier (`merge_class.py`)

`classify(facts)` is pure; `facts_from_context` reads the base sha's config and plan, the state and the diff `base..head`. `eligible` is true only if every rule holds; a missing fact is a failed rule. `action` is always `human-merge` and `merges` is always `false`.

Rules: `policy` (`merge_policy: auto-low-risk`), `level` (`autonomy_level: L5` and the doctor's effective level is L5; an unknown level is not L5), `evidence`, `promotion` (`promotion_attested` is a date), `mutation` (`gates.g4` explicitly `on`), `deploy`, `paths`, `item`, `gates` (every gate that is `on`, and G4, has a pass or an allowed skip for **this head**), `ci_fix` (no CI-fix attempt), `flag` (user-visible copy names `ext.featureFlag`).

- **B5 (`deploy`).** Judged from the attestation (`deploy_triggers`, `git_integrated_hosts`, `deploy_surface.*`): an unattested surface fails; every trigger must be `gated-by-environment` or attested as not deploying (`fires_on: none`); a host must deploy nothing on a push or be gated; a malformed, misspelled or empty-looking entry is an ungated deploy. This is the second wall: the doctor's `b5-deploy-surface` cap (which also reads the workflows) is the first, and the classifier needs both. **A repo that deploys on merge to main without a human gate is never eligible**, whatever the item looks like (RC-1, DEC-38).
- **Paths.** Low-risk means: matches `low_risk_paths` (default `docs/**`, `**/*.md`) and none of `always_human_paths`, the H3 list (`pr_prepare.HUMAN_PATTERNS`: migrations, infra, lockfiles, test and lint config, `.github/`, `.claude/`, plugin and skill directories, CODEOWNERS, `.env*`), the deploy-surface floor, or a security-sensitive word in a non-document path (auth, payment, billing, schema, migration, infra, deploy, secret, token, crypto, ...). Comparison is case-insensitive; a path with `..`, `.`, a backslash, a NUL, a leading `/`, `-` or `~`, a drive letter or a `.git` part fails; a symlink or submodule entry never qualifies; renames are judged as a delete plus an add. Documents are `.md .rst .adoc` and plain images; anything else under `low_risk_paths` is "copy" and needs a feature flag.
- **Doctor.** `doctorlib/extras.py`: `l5.merge-policy` refuses `merge_policy: auto-low-risk` (an error) until the evidence gate, the promotion bar, mutation testing and B5 hold; `l5.b5` says why a deploying repo is capped; `levelcalc` adds `merge_policy is auto-low-risk` and `low_risk_paths` to the L5 prerequisites. Enabling is DESIGN-ONLY: with every prerequisite true, this build still merges nothing.

## The reproducibility manifest (`manifest.py`, F-26)

A sealed JSON file of what a piece of the loop ran on: `repo` (base and head), `kit` (KIT_VERSION at the base, loopctl's version), `config` (sha256 of loop.config at the base, gate modes, the behaviour keys), `plan` (plan and item digests at the base), `toolchain` (`.nvmrc`, `.tool-versions`, `.python-version`, `package.json` engines, `pyproject` requires-python, ... at the head), `lockfiles` (git object ids at the head), `verify` (sha256 and head of each kit-owned command), `evidence` (git object id at the base), `gates` and `environment` (recorded), and `hash` (sha256 of the canonical form of everything else).

`manifest verify` checks the **seal** (editing any field or the hash breaks it) and the **facts**: everything re-derivable is read again from git at the recorded commits and compared, so a manifest that was edited and re-sealed still cannot claim a config, plan, pin, lockfile or evidence file those commits do not hold. `gates` and `environment` are protected by the seal and, if you pass `--expect-hash` (from the PR's audit block, or any channel the writer cannot edit), by that. The event log records each hash written (`anchored`); it is local and forgeable (CS-21), a convenience and not the proof. **It is not a signature**: someone who controls a repository can write a manifest that is true about a repository they made up.

## G10 product checks (`gates/g10_product.py`)

OPT-IN, EXPERIMENTAL, evidence-gated (`gates.g10` is `off` by default and refuses `on` and `report` until the evidence gate passes; the gate module also refuses to run without it). No tool is bundled. Four named checks, each a command of yours: `g10_a11y_cmd`, `g10_perf_cmd`, `g10_pii_cmd`, `g10_observability_cmd` (empty = not run), with `g10_timeout_s`.

- **No shell.** The command is split once (`shlex`); `; & | < > ` $ \ ( ) { } * ? !` are refused (the `notify_command` rule), as are a newline, more than 16 arguments and an argument over 300 characters. The program is an absolute path, a name on `PATH`, or a file in the worktree (resolved, and refused if it leaves the worktree). The refused command runs nothing.
- **An item cannot rewrite its judge.** A program or argument that is a file the item changed fails with `g10.tool-edited`.
- **Scope.** `LOOPCTL_G10_SCOPE` names a JSON file `{schema: g10.scope.v1, item, base, head, files}` of the changed files, minus tests, documents (`.md .rst .adoc .txt`), `docs/` and `plan/`. No such file: the gate is skipped.
- **Result contract.** The command prints, on its last output line, `{"schema": "g10.v1", "check": "<its name>", "checked": N, "violations": N, "details": [...]}`. The gate computes the verdict: `checked >= 1` (a check that looked at nothing proves nothing) and `violations == 0`. A result for another check, a missing line, a count that is not a whole number (a bool is not), and any extra field are failures. `details` are shown, never trusted. Failure classes: a command that exits 126 or 127 is `env`, a timeout `transient`, the rest `item`.
- **CI.** `.claude/templates/github/workflows/g10-product.yml` runs `python3 -m loopctl.gates._ci g10` from the kit extracted at the base sha, in an untrusted job with no secrets and no OIDC. `driver.VERIFY_GATES` gains `g10` (a no-op while it is `off`).

### Contract tests against `api-contract.md` (DESIGN-ONLY)

The report lists them under G10. They need a runner that reads the project's own contract document and drives its own API, and what a contract is differs per project (OpenAPI, a hand-written table, GraphQL). They are **DESIGN-ONLY** (IMP-1): no key, no code and no test here. The intended shape, for whoever builds it against a real project: a fifth named check, `g10_contract_cmd`, under the same rules (no shell, a file the item changed is refused, `g10.v1` result with `check: contract`), whose wrapper reads `docs/features/<slug>/api-contract.md` at the **base** sha (the contract is frozen at design lock) and reports how many endpoints it exercised and how many disagreed. What would unblock it: one real project with a machine-readable contract and a fixture that proves a changed endpoint is caught.

## What this phase does not do

- It does not merge, auto-merge, approve, label or open a pull request, and it does not call `gh`.
- It does not run against a real remote (the tests use local bare repositories) or start `claude`.
- The remote lock does not make two humans safe from each other; it makes cooperating ones visible to each other.
- It does not heartbeat the lock of a prose (`runner: sequential`) conductor.
