# Deferred and design-only items

What 2.0.0-rc.1 does not build, with the reason and what would unblock it. Each item names the changelog fragment it came from (`changelog.d/<name>.md`; the fragments stay in the repository for rc.1). The status words are those of `docs/design/v2-implementation-decisions.md` (IMP-1): **DESIGN-ONLY** is a short spec and no code; **not built** is a known gap that could be built offline but was left out.

Checks that need a person, a credential or a live `claude` are in [`docs/v2-pending-human.md`](../v2-pending-human.md), not here.

## Design-only

### UAT feedback widget
A "report a problem" control inside the app for testers, so the report arrives as an issue with the environment and build attached. The control posts to an endpoint on the app's own server, never to GitHub from the browser. The endpoint holds a fine-grained token limited to Issues: write on one repository, rate-limits per session, and files an issue labelled `factory:requested` (never `factory:go`). Fixed fields come from the build and request, not the tester: commit sha stamped at build, environment name, route, user agent, timestamp. The tester's words go in a fenced block marked untrusted; screenshots are stored by the app and linked. The text reaches the loop only through two-stage intake, after an approver applies `factory:go`.
- **Why deferred:** it is application code (framework, auth and hosting differ per project) and needs a live token and a real UI to test; the kit ships no UI.
- **Unblocked by:** a project with a chosen stack and a scoped token; then an offline contract test of the payload-to-issue mapping.
- **From:** `11a-bootstrap-ops`. See also `docs/ops.md`.

### Monitoring to intake (F-14)
An uptime or error monitor files or updates a bug issue without a human copying it. The vendor's GitHub integration or a small webhook function, using the same narrow token, opens one issue per signal (stable title, deduplicated by a fingerprint in the title), label `factory:bug`, a body of fixed vendor fields plus a link. Any log or stack trace is fenced and marked untrusted; severity is mapped from the vendor's scale onto the closed set; the receiver has a flood limit. Never `factory:go`. `smoke.yml` is the in-repository version of the same loop.
- **Why deferred:** vendor-specific, and it needs a live monitor and token.
- **Unblocked by:** a chosen vendor.
- **From:** `11a-bootstrap-ops`.

### Reproduce-first bug lane (Q4)
A `factory:bug` issue goes through two-stage intake (built), then triage (severity, duplicates, repro steps), then a failing test committed first as the acceptance criterion (the acceptance-author, G3), then a fix through the quick lane, verify, and a pull request. Severity high or critical, or a change under `always_human_paths`, is never auto-merged. Built: the labels, `intake.yml`, the schema fields `severity`, `repro_steps` and `duplicate_of`, the smoke issue. Not built: the loop side that turns a triaged issue into a plan item and runs the lane.
- **Why deferred:** it needs the intake record consumed by a real plan, which has not run live.
- **Unblocked by:** a live intake run (`loopctl unattended intake`) and a passing G3 on a real item.
- **From:** `11a-bootstrap-ops`, `7-unattended`.

### G8 verifier agent
G8 exists only as a view: `loopctl status --matrix` prints the acceptance-criteria by evidence matrix. A separate verifier agent that checks each criterion against the running system is not built, and the evidence gate lists it among the features that stay refused.
- **Why deferred:** the report puts it behind the evidence gate and it needs a live agent run to calibrate.
- **Unblocked by:** the evidence gate passing and calibration data (`loopctl calibration report`).
- **From:** `4b-gates`.

### G10 contract tests against `api-contract.md`
Product checks (a11y, performance, PII, observability) are built as G10 (opt-in, evidence-gated). Checking a changed endpoint against the feature's `api-contract.md` is written up in `.claude/spec/loopctl-extras.md` only: no key, no code, no test (one test asserts that).
- **Why deferred:** it needs a machine-readable contract, and the kit's contract is prose.
- **Unblocked by:** one real project with a machine-readable contract and a fixture that proves a changed endpoint is caught.
- **From:** `11b-loopctl-extras`.

### L5 auto-merge enabling
`merge_policy: auto-low-risk` only feeds an eligibility classifier (`loopctl merge-class`); its `action` is always `human-merge`, no code path runs a merge, and `gh pr merge` is denied to every caller. Enabling auto-merge is design-only by policy (IMP-1).
- **Why deferred:** a merge by software is an irreversible action; the report puts it behind the evidence gate, a promotion bar from server-side facts, mutation testing and a repository that does not deploy on merge to main.
- **Unblocked by:** all four conditions above, a decision by the maintainer, and a ruleset that lets a bot merge only what the classifier names.
- **From:** `3-pr-delivery`, `11b-loopctl-extras`.

### `pipeline_host: workflow`
Running the pipeline as a saved Workflow instead of the conductor. The key exists (`conductor | workflow`), `workflow` is evidence-gated and the doctor treats it as an error (`parallel.pipeline-host`); the preflight refuses it. The design is in `.claude/spec/pipeline-host-workflow.md`.
- **Why deferred:** it depends on platform behaviour that is still a probe (Workflow per-agent `isolation` and `subagent_type`, V3; a Workflow started headlessly is denied).
- **Unblocked by:** probe V3 and a live parallel run.
- **From:** `8-parallel`, `0b-prose`.

## Not built (known gaps that could be built)

### Pushing a branch or opening a PR from the loop (L2 bot push)
The driver stops an item that needs a PR at `awaiting_human`; a person pushes (`pr_prepare.py` prints the commands) and `reconcile` records the merge. Since the review fixes no agent pushes at all (D3): the old conductor push at `delivery: push-branch|draft-pr` is denied (`guard-git:push`). A bot push from the driver process is not built.
- **Why deferred:** it needs a live bot credential, `gh` and a forge, and nothing in this build may touch them; it also conflicts with the no-agent-push rule unless the bot identity is a separate process with its own ruleset.
- **Unblocked by:** the bot created and attested (`docs/security/bot-identity.md`) and a decision to reverse D3 for that identity.
- **From:** `4c-driver`, `7-unattended`, `fix-a-guards-sandbox`.

### Batching loopctl's git calls
Every loopctl command re-reads the state ref and the base sha: about eight git processes per CLI call and five per state write, nothing cached across processes. The test suite avoids the cost with a shim (`tests/helpers/gitfast.py`); production code still pays it.
- **Why deferred:** batching changes the code of phases already merged and needs care around the compare-and-swap; measured gain is small against model time.
- **Unblocked by:** a measured wall-time problem in a real run; `git cat-file --batch` for reads is the likely first step.
- **From:** `13a-test-speed`, `4a-loopctl-core`.

### Run-level watchdog and measured cost for killed children: partly built
Both were deferred in `4c-driver` and built in `7-unattended`: a thread in the driver notices a silent driver, and a killed child is priced from what the run itself measured (highest dollars per second, a 25% margin, capped by its allowance, recorded `estimated`); the review fixes added a floor of half the allowance. What remains deferred: a cost measured from the child's session transcript (the transcript format is a probe), and installing the external watchdog (`loopctl unattended watch`) for you; it is a cron line the kit cannot install.
- **Unblocked by:** a live transcript sample; a scheduler on the machine that runs the loop.
- **From:** `4c-driver`, `7-unattended`, `fix-c-loopctl-core`.

### Design stage in the driver
In driver mode the plan item is the design: there is no architect or design-critic child, and `max_design_rounds` is unused by the driver. The design-critic profile still exists in `launch.py`.
- **Why deferred:** H1 and H2 already approve the item's intent, criteria and footprint, and slicing bounds its size.
- **Unblocked by:** evidence that driver-mode items fail for design reasons that a critic would have caught.
- **From:** `4c-driver`.

### Re-verify an approved item after other items merged
In a parallel run an approved (human-gated) item is merged three-way and unrebased, because the approval is for one head and a rebase would change it. Nothing re-runs verify on the combined result; the post-merge check on the integration tip (`integration_red`) detects a red branch after the fact but does not prevent or revert it.
- **Unblocked by:** a decision on whether a moved base voids the approval.
- **From:** `8-parallel`, `fix-c-loopctl-core`.

### Per-worktree setup for parallel runs
The kit gives each worker a port slot (`KIT_PORT_OFFSET`, `KIT_ITEM`), not an install step or an ephemeral database; stack-specific.
- **Unblocked by:** a stack-specific setup script a project writes.
- **From:** `8-parallel`.

### Plugin form of the loop machinery
The plugin covers the interactive modes, the agents, the guard hooks and the stand-alone scripts. The driver, unattended and parallel modes, the generated `setup.sh` and the hook snippets (notify, worktree) read the kit from the project's git history and need the copy install. Rewriting them for a plugin root would touch a dozen merged phases.
- **Unblocked by:** demand from plugin users and a probe of `${CLAUDE_PLUGIN_ROOT}` expansion in skills and hooks (V13).
- **From:** `9-packaging`.

### Infrastructure: cost delta and database migration workflow
Infracost is not templated (it needs an API key, and placing a secret in a workflow that handles artifacts built from PR code is the maintainer's decision). No DB-migration workflow ships: the command is project-specific and `ci_lint` cannot tell an ephemeral CI database from a shared one; the policy (H4, post-merge, same gate) is documented in `docs/security/infra-ci.md`.
- **Unblocked by:** a maintainer decision on where the key lives; a project's migration command.
- **From:** `6-infra-ci`.

### Attestation exemption for remote reusable workflows
The lint (`ci.remote-workflow-secrets`) has no attestation input, so a person who accepts such a workflow does so in the doctor's `deploy_triggers`, not by silencing the lint.
- **From:** `fix-d-infra-lint`.

### Learning-loop gaps
The Docs Delta gate covers three trigger families (env var, route, command-line flag). The empty-Primer-Delta streak is computed by `loopctl retro`, not shown by `loopctl status`. Calibration outcomes beyond "a later round dropped it" are recorded by hand. Ablation covers prompt-level parts; a gate can be ablated only if a project adds an eval case for it. Primer changes are proposals, not files.
- **Why deferred:** each needs real run data before a threshold or a heuristic means anything.
- **From:** `10-learning`.

### Promotion facts builder
The promotion bar reads a `promotion.facts.v1` file made from GitHub or CI; the recipe is prose in `docs/unattended.md`, and no script builds the file.
- **Unblocked by:** a real repository's PR history to test the GitHub queries against.
- **From:** `7-unattended`.

### Dispatch must carry a brief path (D-16 enforcement)
The agent descriptions no longer invite ad-hoc delegation, but nothing enforces that a dispatch carries a brief path.
- **Unblocked by:** probe V7 (whether SubagentStart sees the dispatch prompt).
- **From:** `0b-prose`.

### Windows
The process-group code in `verify.py` (reused by the gates, the driver and the probe wrappers) is POSIX only. Windows is not supported.
- **From:** `4b-gates`.
