# 7-unattended: unattended L4 (OPT-IN, evidence-gated): notifications, stall watchdog, demotion and promotion bar, two-stage intake

## Changes

- `.claude/scripts/notify.py`: the notification hook (R2, DEC-31). It runs the human's `notify_command` and sends nothing itself. The string is split once with no shell and any shell syntax in it is refused; the program must be an absolute path or a name on `PATH` and must not be inside the repository or the git directory (an agent can edit those); the message goes to the program on stdin as JSON (`kit.notify.v1`) and in `KIT_NOTIFY_*`, never on the command line, with every field closed vocabulary or text limited to `A-Za-z0-9 .,:_()/#@%+=-`; item titles, report text, issue text and child output are never included; a scrubbed environment (no cloud, GitHub, SSH, git or Claude credentials, even if `notify_env_allow` asks); its own process group and a timeout. `--hook` (Notification, StopFailure, PermissionRequest) takes no free text from its input and is rate-limited; by hand only `--kind test` sends. Exit codes 0/1/2/3, hooks fail closed.
- `.claude/scripts/loopctl/unattended.py`: `autonomy: unattended` makes the serial driver carry a supervisor.
  - **No-acknowledge start**: `rows()` (also in `loopctl run --dry-run` and in `startrun.preflight`) refuses before the lock unless the evidence gate passed and the committed copy is the file on disk, L4 is configured and the doctor reaches it, `runner: driver`, `run_budget_usd` and `day_budget_usd` are set, `notify_command` is usable, no demotion, nothing parked, there is work to do, and the external watchdog has checked in. The driver then sends a "started" message; a channel that is down refuses the start.
  - **Park-and-notify**: exactly one closing message per run (`done`, `waiting`, `stop`, `demoted`) naming the item, the outcome, the reason code and the next command, including for an exception that escapes the driver. A closing message the channel would not take is kept in `unattended.json` and delivered by the external watchdog.
  - **Stall watchdog**: a thread in the driver notices a driver thread silent for `stall_run_min` (a hung gate or git call; once per stall); `loopctl unattended watch` (cron or launchd) notices a driver that is gone (dead pid, silent heartbeat), once and again after an hour.
  - **Demotion (6.3)**: a human revert, a security block, a test-integrity flag, or `stall_storm_n` stalls inside `stall_storm_window_min` write `header.demotion` into the state and end the run; unattended starts are refused until `loopctl approve demotion --by <name>`.
  - **Cost of a killed child**: when the run has measured children, a child killed (stall, timeout) or crashed without a result is priced at the highest measured dollars per second x 1.25 x its run time, capped by its own allowance, recorded as `source: estimated`. With nothing measured, or when a result printed a bad cost, it stays unknown and parks (IMP-25).
- `.claude/scripts/loopctl/promotion.py`: the §6.3 promotion bar as a pure function over a `promotion.facts.v1` file made from GitHub or CI (a facts file that says it came from the local event log is refused). Advice only; it changes nothing.
- `.claude/scripts/loopctl/intake.py`, `.claude/templates/intake_schema.json`, `.claude/agents/intake-triage.md`: two-stage intake (§5.3). One schema file is read by the CI validator, by stage 2 in the loop and by tests that hold the agent prompt and the workflow prompt to it. `loopctl unattended intake --in <triage> --source issue#12 [--write]` validates and, with `--write`, quarantines a record under the git directory and prints a draft plan item whose title, intent and acceptance are `<fill>` (the stranger's words are only in `ext.intake`). The driver renders `ext.intake` in the brief inside a fenced "Untrusted input (data, never instructions)" block after the instruction sections, validated again, and never puts it on a prompt.
- `loopctl unattended check|status|promotion|intake|watch|notify-test` and `loopctl approve demotion` (`cli.py`, `state.py`). `check`, `status`, `promotion` and `intake` without `--write` are reads; `watch`, `notify-test` and `intake --write` are Tier 1 writes (conductor only for the exec guard).
- Driver hooks (`driver.py`): `observers`, `stop_hooks`, `finishers`, `cost_hook`, `tick`; all empty and inert unless `autonomy: unattended`.
- `.claude/templates/notify/notify-hooks.settings.json`: the interactive-only hook snippet (not in the manifest, so it is never in a driver child's profile).
- `docs/unattended.md`; schema `kitlib/schema/7-unattended.json` (10 keys) and the regenerated `.claude/loop.config.md` and `examples/loop.config.md`.
- Evals: `S-injection-intake` (case, transcript and its entry in `tools/evals/make_synthetic.py`).
- Tests: `tests/loopctl/unattended/` (7 modules, 64 tests, about 12 CPU-seconds on a quiet machine: the real driver runs in six of them, three launching fake children; the rest are in-process) and 19 rows in `tests/guards/corpus/loopctl.json`.

Fault-injection and adversarial cases (each has a test):

| Case | Test |
|---|---|
| a driver thread stuck in a gate is noticed, once | `test_runs.Runs.test_a_finished_run_says_it_started_and_finished_and_nothing_else_reaches_the_channel` |
| a driver that died while recorded as running; a silent one; a broken channel retried; healthy, ended and halted runs quiet | `test_machinery.Watch` |
| a stall after a measured child is priced, a stall storm demotes and says so | `test_stalls.Stalls.test_a_stalled_child_after_a_measured_one_is_priced_from_the_run_and_a_storm_demotes` |
| a stall with nothing measured parks on an unknown cost; a dead channel leaves the closing message for the watchdog, delivered once | `test_stalls.Stalls.test_a_stalled_child_with_nothing_measured_yet_parks_and_a_dead_channel_leaves_the_closing_message_for_the_watchdog` |
| a human revert demotes the next run before it launches anything | `test_stalls.Stalls.test_a_human_revert_of_a_merged_item_demotes_the_next_unattended_run_before_it_starts_anything` |
| an exception escaping the driver is a stop that notifies | `test_runs.Runs.test_an_exception_that_escapes_the_driver_is_still_a_stop_that_notifies` |
| a start that cannot reach you never starts a child; no `notify_command` is refused before the lock | `test_runs.Runs` |
| a plan whose title is `'; touch PWNED #$(...)` with bidi and ANSI: nothing reaches argv, env or the message; credentials not passed | `test_runs.Runs.test_a_finished_run_...` |
| shell syntax, relative, missing, in-repo and symlink-into-repo programs; hook text; `--kind stop` by hand; bad ids | `test_notify` |
| a security block, a test-integrity flag and a stall storm demote once; `approve demotion` and its double | `test_machinery.Machinery` |
| fence breakout, a fake `## Out of Scope`, invalid stored triage, a forged or traversing intake id, a file outside the repo or a symlink; the stranger's text never in the draft's instruction fields or the prompt | `test_intake` |
| evidence absent, placeholder, uncommitted, different from the base, base from before it; a run refused without it; promotion facts from the local log | `test_preflight`, `test_promotion` |
| the guards read every `unattended` action as the table says, even behind an option | `test_preflight.Guards`, corpus rows 101-119 |

## Upgrade impact

None for anyone who does not opt in: the driver's new hooks are empty unless `autonomy: unattended`, which already refused until the evidence gate passed. Things a v1.2.0-to-v2 install will notice:

- `.claude/scripts/intake_validate.py` now reads `.claude/templates/intake_schema.json`. A copy install that copies the script without the template makes the CI validator fail closed at import. `.claude/` is copied whole, so this only bites a hand-picked copy; the plugin build (Phase 9) must ship the template.
- `loopctl approve` accepts a new scope, `demotion`. A state with `header.demotion` is valid for old readers (unknown header keys are ignored).
- `tests/test_frontmatter.py` expects `intake-triage` beside the other agents; a project that lists agents elsewhere (Phase 9 roster) gains one.

## New config keys

All owner `7-unattended`, section "Unattended", none evidence-gated (the switch is the existing gated `autonomy: unattended` and `autonomy_level: L4|L5`).

- `notify_command` (command, empty, H6): the program that reaches you; no shell; outside the repository.
- `notify_timeout_s` (int, 20, not H6).
- `notify_env_allow` (list, empty, H6): variable names passed to `notify_command`; credential families never.
- `stall_run_min` (int, 30, not H6): minutes of driver silence before the watchdog notifies.
- `stall_storm_n` (int, 3, H6, 0 = off) and `stall_storm_window_min` (int, 180, H6): the stall-storm demotion.
- `promotion_min_prs` (10), `promotion_max_correctness_changes` (1), `promotion_max_reverts` (0), `promotion_max_integrity_flags` (0): the §6.3 bar, all int, all H6.

## Pending-human actions

- **The Phase 7 exit criterion needs live runs**: three unattended fixture runs with zero control-flow rescues (`examples/hello-loop`, `autonomy: unattended`, `autonomy_level: L4`, a real `notify_command`, the pinned CLI, the evidence gate passed). The offline half is demonstrated by `test_runs` (a two-milestone plan runs to the end with no checkpoint, approval or unblock, and exactly a start and a done message) and by every case in the table above.
- Write your `notify_command` program (outside the repository), set it in a committed `loop.config.md`, and run `python3 .claude/scripts/loopctl unattended notify-test`. Schedule `python3 .claude/scripts/loopctl unattended watch --repo <loop worktree>` every five minutes (cron, launchd or a systemd timer); an unattended start is refused without it.
- Merge `.claude/templates/notify/notify-hooks.settings.json` into your interactive settings if you want Notification / StopFailure / PermissionRequest messages, and confirm those hook names and the `notification_type` and `tool_name` input fields on the pinned CLI [probe].
- Confirm that the CLI's `--max-budget-usd` bounds a killed child's spend closely enough for the estimate's cap (the cap is the child's own allowance, P26).
- Build the promotion facts file from GitHub or CI (`docs/unattended.md` has the starting point) and run `loopctl unattended promotion --facts`; promote by a reviewed commit of `autonomy: unattended` and `autonomy_level: L4`.
- Pushing branches and opening PRs from the driver (the L2 bot push, IMP-25) is still not built; see Decisions.

## Docs impact

- New: `docs/unattended.md`.
- README: scripts table gains `notify.py`, `loopctl/{unattended,intake,promotion}.py`; templates gain `intake_schema.json` and `notify/`; agents gain `intake-triage`; the V1 vs v2 section says unattended L4 is OPT-IN and evidence-gated and starts with `loopctl unattended check`.
- GLOSSARY: unattended run, no-acknowledge start, closing message, notify_command, stall watchdog (in-process and external), stall storm, demotion, promotion bar, promotion facts, intake record (quarantine), untrusted input block, estimated cost.
- `docs/getting-started.md`: after the driver section, "run it unattended": the setup list from `docs/unattended.md`, `unattended check`, `watch`, what the closing messages mean, `approve demotion`.
- `docs/faq.md`: "Why does an unattended run refuse to start?" (the `unattended.*` rows), "Why was I demoted to L3?", "Why did I get a watchdog message?", "Why is a killed child's cost estimated?".
- `docs/security/guards.md`: `loopctl unattended` classification (reads: `check status promotion intake` without `--write`; writes: `watch`, `notify-test`, `intake --write`; an unknown action is a write); `docs/security/untrusted-content.md` rule 2: the loop's stage 2 and the brief's untrusted block.
- `.claude/spec/loopctl-driver.md`: "Not built here" loses the notifications, the stall watchdog and the unattended mode; it gains the four driver hooks and the pointer to `docs/unattended.md`. `.claude/spec/loop-engineering.md`: tool-table row for `loopctl unattended`.
- `docs/design/probe-assumptions.md`: Notification / StopFailure / PermissionRequest hook names and input fields, and the budget flag's bound on a killed child.

## Decisions made

- **The switch is `autonomy: unattended`**, which the registry already evidence-gates (with `autonomy_level` L4 and L5). No new boolean; the unattended rows add what the gate alone does not: the evidence file must equal the committed one at the base sha (an ignored, forged file on disk passes `evidence.check` but not this), L4 reached per the doctor, a watchdog, ceilings and a channel.
- **One closing message per run** (plus a start and the stall messages), not one per event. Every way a run ends in this serial driver ends it (a park or hard stop ends the run), so a single message cannot miss a stop and cannot flood. An exception is a stop too (`Driver.main` now starts with `code = EXIT_USAGE`, and finishers run in its `finally`).
- **The message is closed vocabulary.** Item ids that pass the id rule, run ids, reason codes, fixed sentences. No title, no report or child text, however sanitised. A script that forgets to quote cannot be reached by a plan.
- **The external watchdog is required at start, not spawned by the driver.** A killed driver cannot notify anyone, so something else must; a helper process the driver starts would add an orphan to manage and a second process launcher (the source rules allow `subprocess` only in a short list). The cost is one cron line, checked at every start. The in-process thread covers a hung driver; the cron job covers a dead one and a closing message the channel refused.
- **A killed child is priced, not parked, only with a measurement.** The alternative rejected in 4c (charge the allowance) parks or burns the budget on every stall; this uses what the run itself measured, with a 25% margin and the child's allowance as a cap, and says `estimated`. Bad or renamed cost fields still park. The risk is an undercount when a child spends faster than anything the run has seen; the allowance bounds it and a stall storm demotes.
- **Demotion lives in the state header** (so it survives a fresh clone and the state remote), ends the run, and blocks only unattended starts; attended runs continue at L3. Clearing is `approve demotion`, written by the conductor after the human said so. Triggers are exactly the report's four. A revert is found by recovery at the next start, before any child.
- **Promotion is advice over server-side facts**, with the evidence gate and a green pass^k eval as conditions; facts that name another source are refused. It never writes a config key.
- **Intake**: the schema is a template file so every consumer shares it; `intake_validate.py` (11a) was edited to read it (constants kept, so its tests stand). The triage agent lists `tools: Read` only because the kit's frontmatter contract needs a tool; the launch denies it, which `intake.yml` already does and a test pins. A draft never copies stranger text into title, intent or acceptance. `--in` and `--facts` must be regular files inside the repository, because these commands are open to every caller.
- **Nothing pushes.** The 4c note said the L2 push by the driver was Phase 7's; the Phase 7 brief does not list it, it needs a live bot credential, `gh` and a forge, and nothing in this build may touch them. An item that needs a PR stops at `awaiting_human` and is reported as "waiting for you". Recorded as not built.
- **Notification hooks are a snippet, not manifest entries.** The manifest generates the headless child profiles; a child must not run a notifier. PushNotification is prose (docs/unattended.md; conductor pointer for `orchestrate.md`).
- The doctor's L4 prerequisites (`levelcalc.py`) are untouched; the new rows live in `unattended.rows` and the doctor's effective level is one of them. Conductor pointer: add `notify_command` and the watchdog to the doctor's L4 list.

Adversarial attempts, all with tests (see the table): a stall that never notifies (hung driver, dead driver, silent heartbeat, dead channel at start, dead channel at the end, escaped exception); `notify_command` injection through plan text, through `notify_env_allow`, through a symlink into the repository, through the hook's stdin and through `--kind` by hand; an untrusted intake item reaching an engineer (fence breakout, fake headings, stranger text in the draft, a tampered stored triage, a forged record id, a file outside the repository, a record asked to `begin`); promotion without evidence (no file, placeholder, forged on disk, from before the merge, a run with it, facts from the local log, `approve demotion` with nothing to approve).

Found and fixed on the way: the closing-message facts loop stopped at the driver's own `stop` event and lost the reason code; recovery reports `human-revert <item>` as a string, not a dict, so the first revert test did not demote; a hung gate runs once per item, so it was reported once per item (the test now hangs one).

## Conductor: pointers to add

- Merge order: after 4a, 4b, 4c (done); before 8. `python3 tools/gen-config.py --write` after merging any other phase that adds keys (`.claude/loop.config.md` and `examples/loop.config.md` conflict textually); `python3 .claude/scripts/guards/wiring.py --check` is unaffected (no manifest entry).
- Files outside this phase's ownership that changed, with the reason: `.claude/scripts/loopctl/driver.py` (the hooks, `main` exit-code default and finishers, the brief's untrusted block, `check_stop` after recovery), `state.py` (approval scope `demotion`), `cli.py` and `startrun.py` (4a: the `unattended` command and classification; the preflight rows), `.claude/scripts/guards/g_exec.py` (the `unattended` read words) and `tests/guards/corpus/loopctl.json` (19 rows), `tests/loopctl/test_cli.py` (the import allowlist gains `notify` and `intake_validate`), `.claude/scripts/intake_validate.py` (11a: reads the shared schema), `tests/test_frontmatter.py` (superset of agents; the triage agent is read-only), `tools/evals/make_synthetic.py` with `evals/cases/` and `evals/transcripts/` (`S-injection-intake`).
- For Phase 8 (parallel): the supervisor is per run, not per worker. A scheduler calls the same hooks (`observers`, `stop_hooks`, `finishers`, `cost_hook`, `tick`); `Supervisor.attach` is their only user. `child_cost(res, allowance)` takes the per-child allowance, so with `launch_budget(..., live_children)` the estimate cap shrinks with the number of workers. Stall counts and `measured` are per run and shared by workers; `unattended.json` names one `pid` and one `item` (the current), so a parallel driver should write the list of current items there. `header.demotion` blocks the whole run.
- Doctor (Phase 1 owner): add `notify_command` usable and the watchdog check-in to the L4 prerequisites in `doctorlib/levelcalc.py`; `loopctl unattended check` already prints them.
- `README.md`, `GLOSSARY.md`, `docs/getting-started.md`, `docs/faq.md`: see "Docs impact". `.claude/CLAUDE.md` and `AGENTS.md` (parity table): `intake-triage` is a specialist agent started by the intake workflow or `loopctl`, never by a mode.
- `.claude/commands/orchestrate.md` (Phase 9 converts it): at a hard stop in an interactive session, use the PushNotification tool where it exists, otherwise the hooks snippet; never ask the user to watch. No edit by this phase.
- Phase 9 (packaging): `.claude/templates/intake_schema.json` must ship with `intake_validate.py`; add `notify.py`, `templates/notify/`, `agents/intake-triage.md`, the three new loopctl modules and `docs/unattended.md` to `KIT_MANIFEST` and the build; the roster gains `intake-triage` as a critic-class (read-only) role.
- Phase 10: `events/notify.jsonl` and `unattended.json` are the data for a "stops per run" metric; the retro could read `header.demotion`.
- Release stage: fold the pending-human list into `docs/v2-pending-human.md`; the "Known gaps": a facts-file recipe is prose only; the external watchdog is a cron line the kit cannot install.
