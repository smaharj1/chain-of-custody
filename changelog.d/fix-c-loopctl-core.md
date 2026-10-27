# fix-c-loopctl-core: review fixes for loopctl state, ledger, branches, driver, parallel, cli, extras, startrun, unattended, status and the orchestrate skill

Package C of the review-fix batch (decisions: D2 to D7, D11 and D3 loopctl side, plus LC-1 to LC-14, GT-1/3/4/7/8/9/11, UN-9, UN-14).

## Changes

- **Shared command table in the CLI** (D1b, D11). `loopctl/cli.py` carries no classification of its own: `READ_COMMANDS`, `FLAG_READ`, `WRITE_FLAGS` and `classification()` are views of `kitlib/loopctl_table.py`. `tests/loopctl/test_guard_alignment.py` compares the table with every subparser (derived from argparse, so a new action without a table entry fails), with every `approve` scope x choice, the abbreviation and parser-differential cases, and with what `extras` refuses.
- **Human-only commands** (D3, D11, GD-8). `remote-lock acquire|renew|release|steal` and `claim take|release` refuse first, before any config, git or network access, under `CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` (present even when empty): `humanonly.refusal`. `resume` and `reconcile --live` use the same check; `launch.check_nesting` too. `approve` carries no marker check on purpose (a printed command run with `!` may carry the marker); the guard is its one boundary. `cli._approve_parser()` is the parser `cmd_approve` uses, so the parity test parses with the real one.
- **Approvals bound to kind, item, attempt and head** (D2, LC-1). Records get `kind` (`merge`, `unpark`, `ack`, `checkpoint`, `plan`, `env`, `breaker`, `cost`, `demotion`, `unblock`) and, for merge, `headSha`. Only an unused `kind: merge` record for the item's attempt and current head satisfies the merge gate (`_find_approval(st, iid, attempt, head)`; Driver and unattended pass the head). Everything but `merge` and `checkpoint` is written consumed. An unused merge approval is voided (`used`, `voided`) when the item leaves the approvable states (reviewing, pr_open, awaiting_human) any way but merging, and when `advance` changes `headSha`. `approve checkpoint` is accepted only while `header.parked` is a checkpoint pause for that item (otherwise refused); `Driver.pending_checkpoint` and the unattended preflight accept only `kind: checkpoint`. `approve item`/`checkpoint` choices are `approve|reject|stop`. A record without `kind` never merges.
- **Fresh heads** (LC-5, GT-4, GT-9). `advance` judges a head for every step into reviewing, pr_open, awaiting_human or merged and any step with a required gate: `--head` defaults to the branch tip (refused when it cannot be resolved); a gate result for another head, or with no head outside g0/g2, is stale. Under `delivery: local` the merge commit's second parent must be the judged head, otherwise the head must be in its history. `Loop.preflight_merge` runs before the driver's `git merge` (LC-13) together with `check_stop`; a refusal parks the item `merge_refused`.
- **Small state fixes.** The `platform` counter resets on any forward step (LC-10). An escalated transient is recorded as class `item` and judged by the normal retry rule (LC-11). `record_gate(s)` need the run lock (LC-14); `park` keeps `need=False` (parking is the safe direction).
- **Parked work is kept** (D7, LC-2). `Loop.park`, `Loop.clarify` (third round), `Loop.fail` and `reconcile --apply` move `feat/*` to `blocked/*` (`Loop._shelve`, idempotent, skips a branch checked out in a non-loop checkout or in a parallel worker's worktree). `state.live_branches(st)` (in-flight, blocked and needs_human branches plus `parkedBranch`; not pending) feeds the driver's recovery and `reconcile`; `branches.sweep` also keeps any `feat/<slug>-a<n>` whose slug and attempt a live record names.
- **Ledger** (LC-3, LC-4, LC-6). An unacknowledged unknown cost of ANY run parks every run until `approve cost`; an ack covers every unknown written before it. A killed child is charged at least half its allowance (`ESTIMATE_FLOOR`), entries are marked `estimated: true`, `totals()` counts them. The driver judges the ledger before a failed child's failure is recorded (`budget_stop` always fires, with the child's cause); `approve cost` lifts a ledger-code park (`cost_unknown`, `budget_*`) and writes no item approval.
- **Parallel integration** (D4, GT-1, GT-11). After a rebase every `VERIFY_GATES` gate and `g7` run again (off gates skipped, blocking g5 ends the round); an item branch with a merge commit parks `ESCALATION_NEEDED` instead of being rebased.
- **G4 in the driver** (D5, GT-7): `VERIFY_GATES` ends with `g4`.
- **Post-merge check** (D6, GT-8): after `merge_approved` the driver runs g6 on the integration tip; red stops the run `integration_red` (runStatus stopped, breaker +1, no revert). `integration_red` and `merge_refused` have next-step text.
- **Fences** (UN-9, UN-14): `common.fence_data`; `rework_text` puts gate messages and failure detail in one fence after the instructions; the review prompt calls the work data; a saved review starts with an untrusted marker.
- **Status** (D11): `status --pending-human` carries `human_step` (printed as `HUMAN STEP: python3 .claude/scripts/loopctl ...`, with the 12-character head for a merge approval) and a `reconcile-live` entry when delivery is not local and PRs are waiting.
- **startrun**: preflight row `worktree.dedicated` (a WARNING, ok=true) for a driver run outside a linked worktree (LC-7); the sequential hand-off text names the human steps and `--head`.
- **Prose and docs**: the orchestrate skill (`SKILL.md`, `loop.md`, `preamble.md`) has an "Approvals" section (relay forms with the scope first and `--note`; a HUMAN STEP table), prints the remote-lock lines for the human and never pushes; specs `loopctl.md` (who may run what, approvals, heads, spend), `loopctl-driver.md`, `loopctl-parallel.md`, `loopctl-gates.md` (G4, verdict-merge rule, new finding ids, GT-10 note), `loopctl-extras.md`; docs `loopctl-extras.md`, `delivery.md`, `ops.md`, `parallel.md`, `unattended.md`.
- Tests: new `test_approvals.py`, `test_branches.py`, `parallel/test_rebase_reverify.py` (real `REQUIRED_FOR`, real Evidence), `extras/test_human_only.py`, `fault_injection/test_review_fixes.py`, additions to the alignment, ledger, failure, status, machine, cli, prose and launch tests. Existing tests changed: `LoopRepo.walk/complete/do_merge` build a real branch and pass its head (a step into reviewing needs one); the fault tests that read `lastFailure` after an unknown cost now read the `budget_stop` event; the SIGTERM test acknowledges the killed child's unknown cost before the next run.

### Second review (R-LC-1 to R-LC-14, R-A-5)

- **`approve item`** (R-LC-1, 2, 9, 11). `reject` and `stop` on a `needs_human` item are refused with a pointer to the human-only `approve unblock ITEM --choice reset|keep|replan` (they no longer un-park it; the same for the checkpoint scope). `stop` voids an unused merge approval of the item. New optional `--head PREFIX` (7 or more lowercase hex characters): refused when the recorded head does not start with it; the `HUMAN STEP` line for a merge approval now ends `--head <12 chars>`. An `unblock --choice reset` and a `reject` reset the `platform` counter too (only `discards` is kept).
- **Merge commit shape** (R-LC-3). Under `delivery: local`, `advance merged` needs exactly two parents, the judged head as the second, and a tree equal to `git merge-tree --write-tree <first> <second>`: an evil merge (`--no-commit` plus a file) or an octopus merge is refused. Needs git 2.38 or newer; an older git cannot run the check, and the merge is then refused, never accepted unchecked.
- **`loopctl run` inside a Claude Code session** (R-LC-4). With `runner: driver` it is refused before the run lock, the remote lock or any push (`startrun.nesting_problem`; preflight row `nesting`, shown by `run --dry-run`).
- **Orphans are ledgered** (R-LC-6). `Driver.ledger_orphans`: every child record of a dead driver (killed now, or already gone) gets an unknown spend entry (`source: orphan`), so the run parks until `approve cost` and the day budget cannot undercount silently.
- **Next steps that work** (R-LC-7). Every `cost_unknown`, `merge_refused`, `security_blocked`, `platform_outage` and demotion line now says `approve unblock ITEM --by NAME --choice reset`; a test parses every printed `approve` command with the real parser.
- **`gate --head`** (R-LC-8). A gate records the HEAD of the checkout (or `--worktree`) it ran in; a `--head` that is not that HEAD (7+ characters of it) is refused.
- **Prose and stale answers** (R-LC-10). The skill and `loop.md` no longer offer `approve checkpoint --choice reject` (loopctl always refuses it: the item is merged); `approve breaker` and an abort void unused checkpoint answers (`void_checkpoint_approvals`).
- **Abort** (R-LC-12, table change in package A). Documented: `abort --rollback-to` is a `HUMAN STEP`; plain `abort` and `abort --dry-run` are unchanged. `SKILL.md` and `.claude/spec/loopctl.md` say so.
- **Crash recovery** (R-LC-13). `reconcile` recovers a merge lost to a crash only when the merge's second parent is the judged head (once the item reached reviewing or later) and, for a human-gated item or one awaiting a human, a merge approval for that head stands (finding `reconcile.recover-unapproved`).
- **Tests** (R-LC-14, R-A-5): new `tests/loopctl/test_review13_fixes.py`; `test_guard_alignment` puts the positional item before the options (Python 3.9's argparse); `test_run` restores the real `loopctl.driver` module instead of popping it from `sys.modules` (the polluter of the order-dependent `unattended/test_runs` failure: the later import made a second module object, so the test's patch of `Driver.run_items` missed the one `run` used); `test_status` and the SIGKILL crash test follow the new `--head` line and the orphan's unknown spend.

## Upgrade impact

- An orphaned child of a crashed driver now parks the next run (unknown spend) until `loopctl approve cost`; a SIGKILLed unattended run is no longer silently continued.
- A `HUMAN STEP` approve line printed before this change has no `--head`; it still works (the head is optional).
- Open approvals written before this change carry no `kind` and no `headSha`: they no longer merge. The human approves again (`status --pending-human` shows the line with the head).
- `loopctl advance` without `--head` now uses the branch tip, and refuses when the branch is missing: pass `--head`.
- An unknown spend entry from an earlier run now parks the next run until `loopctl approve cost` (it used to be forgotten by a new run id).
- `loopctl remote-lock acquire|renew|release|steal` and `claim take|release` are human-only: an agent session (or a script run inside one) is refused; run them in a plain terminal. The driver's own Keeper is unchanged.
- `approve checkpoint` outside a driver checkpoint pause is refused; use `approve item --choice reject` for an item in flight. `approve item` accepts `approve|reject|stop` only.
- With `gates.g4` `on` or `report`, the driver now runs G4 in the verify round (bounded by `mutation_timeout_s`).

## New config keys

None.

## Pending-human actions

- Read `docs/ops.md` ("The loop worktree, a stale lock and parked work"): run `loopctl run` from a dedicated linked worktree; if a lock's pid was reused and the lock never looks stale, a person deletes `<git-common-dir>/loopctl/lock` by hand after `loopctl lock status` shows no run is alive (no command releases a lock that looks alive; there is no `lock release --force`).
- Re-approve any item that was waiting for a merge approval across this upgrade.

## Docs impact

Hot files get pointers below. Edited here: the orchestrate skill, `.claude/spec/loopctl*.md`, `docs/loopctl-extras.md`, `docs/delivery.md`, `docs/ops.md`, `docs/parallel.md`, `docs/unattended.md`. The older fragment `changelog.d/11b-loopctl-extras.md` still says the remote-lock and claim writes are "Tier 1, conductor session only" and that the conductor runs them: this fragment supersedes it (human-only, printed as HUMAN STEP lines); it was not edited.

## Decisions made

- Moves between the approvable states (reviewing -> pr_open -> awaiting_human) keep an unused merge approval (the head binding already protects it); leaving them any other way, or a head change, voids it. D2 read literally would void on every non-merge exit.
- `approve checkpoint` needs a driver checkpoint pause. In `runner: sequential` nothing parks the header, so the conductor keeps recording checkpoint answers in `loop.jsonl`; the prose says so.
- A pending item's old branch is not "live" (recovery resets it and `begin` creates the next branch); D7 text listed pending-with-branch as live, which would block `begin` after a reset-inflight.
- `Loop._shelve` skips a branch checked out in a parallel worker's worktree (the scheduler settles it) and one checked out in a non-loop checkout.
- Human feedback in `rework[-1].note` stays an instruction (a plain, cleaned line), not a data fence: it is the human's own words recorded by `approve --by`.
- UN-14: the review prompt is not fenced (it embeds no diff); the saved review gets an untrusted marker line instead of a fence so g7's heading parser is unaffected.
- `approve cost` with no run record acknowledges under run id `none`.
- `worktree.dedicated` is a preflight row with ok=true and a WARNING text (the rows have no warn level).
- GT-9 state side: the stale rule is in `Loop._check_forward` (local `PRE_BUILD = (g0, g2)` copy; B owns the Evidence copy).
- R-LC-1: `reject` and `stop` on a `needs_human` item are refused (not turned into an acknowledgement), so the conductor's relay cannot even leave a record that looks like a decision about a stop.
- R-LC-3: a merge that git cannot recreate cleanly (conflicts) is refused by `advance`; a conflict a person resolved is recorded through `reconcile`. No git 2.38 means refuse, not accept.
- R-LC-6: an orphan's spend is written as unknown, not estimated: nothing measured it.
- R-LC-8: `--head` is accepted as a prefix of at least 7 characters of the checkout's HEAD, and the full HEAD is what is recorded.
- R-LC-5: no new command (decision of the conductor); the remedy is the human deleting the lock file.

### Known gaps

- R-LC-3 checks the merge commit's shape only under `delivery: local`; under other deliveries the judged head must be in the merge commit's history and a squash merge is recorded by `reconcile` (as before). `reconcile`'s `merged-recovered` and `pr-merged` do not compare the tree, because a conflict a person resolved by hand legitimately differs from a plain merge (`merge_conflict` says to resolve it and run `reconcile --apply`); an ungated item's hand-made merge of its judged head is still accepted there.
- R-LC-8: `gate --worktree` is not checked to be a worktree of this repository, and a gate on a dirty tree records that checkout's HEAD.
- R-LC-9: the head assertion is optional; a person who omits `--head` approves whatever head is recorded.
- R-LC-6: the orphan's real cost is never known to loopctl; the unknown entry only forces the human to look. A killed child's estimate for an unattended run is unchanged.
- `--by` is recorded, not authenticated; a human who pastes a conductor-printed command without reading it is outside the kit's reach. The guard is the only boundary for `approve` (no marker check in the CLI).
- `reconcile.scan`'s `pr-merged` trusts any integration-branch commit named by the PR facts; acceptable only because those facts are now human-supplied (`--live`).
- Under other deliveries than local, `advance merged` requires the judged head in the merge commit's history, so a squash merge is recorded by `reconcile`, not by `advance`.
- LC-8 (pid reuse keeps a lock from looking stale), LC-9 (heartbeat/release/takeover are not atomic) and LC-12 (a process group outliving its leader after a driver crash) are documented, not fixed.
- The post-merge check detects a red integration branch after the human-approved merge; it does not prevent it or revert it.
- A killed child's estimate is a guess bounded by its allowance; it can over-charge.

### Findings

second review: fixed R-LC-1, 2, 3, 4, 6, 7, 8 (head from the checkout), 9, 10 (prose and stale answers), 11, 13, 14 and R-A-5; documented R-LC-5 (hand-delete the lock file; no new command, per decision), R-LC-12 (the table change is package A's); gaps above. First round: fixed: LC-1, LC-2, LC-3, LC-4, LC-5, LC-6, LC-10, LC-11, LC-13, LC-14, GT-1, GT-3 (extras side), GT-4, GT-7, GT-8, GT-9 (state side), GT-11, UN-9, UN-14, GD-5 and GD-8 (loopctl side); LC-7 fixed as a WARNING row + doc; documented: LC-7 (doc), LC-8, LC-9, LC-12; no change: none. LC-14 `park` keeps `need=False` by design (documented).

## Conductor: pointers to add

- `loopctl/cli.py`, `unattended promotion` branch (package E's UN-13): replace `facts = promotion.parse_facts(raw)` by `facts = promotion.parse_facts(raw, origin="file:" + os.path.basename(a.facts))`. Not done here: `parse_facts` has no `origin` parameter in this worktree.
- `README.md`, `docs/getting-started.md`, `docs/faq.md`, `GLOSSARY.md`, `AGENTS.md`, `.claude/CLAUDE.md`: mention that approvals and lifting stops are human steps the conductor prints (`HUMAN STEP: ...`, `loopctl status --pending-human`), that no agent pushes (the remote lock and claims are human-only commands), and that a merge approval is for one head. `GLOSSARY.md`: "human step", "merge approval (bound to a head)".
- `.claude/context/delivery.md` (not owned): if it says the conductor takes the remote lock or pushes at delivery, change it to the HUMAN STEP wording of `SKILL.md`.
- Regenerate `plugin/`, `dist/`, `.claude/KIT_MANIFEST` (shipped sources changed: skills, scripts, specs, docs).
- Package A: `guards/g_exec.py` consumes `kitlib.loopctl_table` (done by A); the corpus flips listed in D11 are A's.
- `docs/getting-started.md` (line 75) and `GLOSSARY.md`: at a checkpoint, `reject` is the prose loop's (`runner: sequential`) un-ship; with `runner: driver`, `loopctl approve checkpoint` takes `approve|stop` only, and a reject is the human's own revert or `abort --rollback-to`. A merge approval's printed line now ends with `--head <12 chars>`.
- `docs/v2-pending-human.md`: after a driver crash, `loopctl approve cost` is needed once more (the orphan's spend is unknown).
- Package A: `abort --rollback-to` must be human-only in `kitlib/loopctl_table.py` (R-LC-12); `SKILL.md` and `.claude/spec/loopctl.md` already say so.
- Regenerate `plugin/`, `dist/`, `.claude/KIT_MANIFEST` again (skills, scripts, specs and docs changed).
