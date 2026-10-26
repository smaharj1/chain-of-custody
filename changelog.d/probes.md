# probes: platform-probe harness (IMP-7) and the assumption register; no probe has been run

## Changes

- `tools/probes/README.md`: one section per open item of report §17 (V1, V3-V9, V12-V27; V2, V10, V11 get a one-line note). Each section has purpose, what it blocks, the conservative assumption the v2 build makes, the exact preparation and steps, a table of what each answer looks like, and where to record it. Order: V1, V5, V9, V18, V19, V22, V24, V27 first, then the rest by number.
- `tools/probes/make-scratch.sh` (thin `sh` wrapper) and `make_scratch.py`: create a scratch git repo outside this repo, install `tools/baseline/observe.py` as a hook-input logger on 16 hook events, merge the probe's settings snippet into `.claude/settings.json`, copy the probe's agents, skills, plugin directory, scripts and prompt (`PROBE-PROMPT.txt`), write `<scratch>.probe.json` (variant, commits to compare against) and print the exports and next step. `--dry-run` creates nothing. `--variant` picks a snippet variant (V9, V18, V23 need one). `--control` strips the probe's control key (V9 `onFailure`, V19 `failIfUnavailable`). `--events` overrides the event list. V6, V17 and V25 get a local bare `origin`. V15 and V27 need no scratch repo and say so. Refuses (exit 3) when `CLAUDECODE` is set, when the path is inside this repo or another git repo, or is not empty; a failure part-way removes everything it made. It never runs `claude`.
- `tools/probes/settings/`: snippets for V1, V5, V7, V9 (`missing-interpreter`, `crash`, `timeout`, each on PreToolUse, ConfigChange and PermissionRequest with `onFailure: block`), V18 (`fork-on`, `fork-off`, `fork-off-nobg`), V19, V20, V22, V23 (`allow`, `noallow`), V24; extras for V6, V8, V14, V21 (`V21-managed.json`, installed by the human with sudo, never into the project) and V25.
- `tools/probes/files/`: per-probe agents, skills, a plugin directory (V13, V22), scripts and prompts, plus `common/probe_hook.py` (modes `mark`, `crash`, `sleep`, `deny`, `inject`, `statusline`; the marker line is written before the mode's behaviour so a crash or timeout cannot lose it).
- `tools/probes/analyze.py`: `analyze.py V<n> <log> [--json] [--manifest FILE]` and `analyze.py summary <log>`. Reads the observe.py log plus the markers and prints the fields that answer the probe (V1: `agent_id`/`agent_type` per depth; V9: whether each failing hook's tool call still ran; V18: whether the parent's next action came before the slow child finished; V20: whether `SessionStart` with `source=compact` fired and whether the model quoted the injected nonce; V24: which of main/plain/loose/strict could read the held-out file). It prints a "reading" per answer but never a verdict. An empty log is reported as a finding (exit 0); an unreadable log or unknown probe is exit 2.
- `docs/design/probe-assumptions.md`: the IMP-7 table expanded for V1-V27: assumption, where it is encoded (paths that exist now; later phases marked "(planned)"), what changes if the probe disagrees.
- `docs/design/probe-results.template.md`: results table (V-item, variant/cell, date, Claude Code version, OS, observation, verdict, follow-up) with one row per cell of the priority probes.
- `tests/probes/` (98 tests, about 7 s): `make-scratch` refusals, dry run, and what each scratch holds (every probe and variant is prepared and checked); `probe_hook.py` modes; `analyze.py` against synthetic logs for every probe with an analyzer (both answers where a probe has two); snippet validity (JSON, known events, V9 coverage, no network); README/prompt/assumption/template consistency including "the README quotes every prompt verbatim" and "no probe file is git-ignored".
- `tools/check-refs.allow`: 15 lines appended (scratch-relative `.claude/probe/*`, the results file the human creates, and the later-phase paths named in `probe-assumptions.md`).

## Upgrade impact

None. These are files for kit developers and maintainers; nothing under `.claude/` changed and nothing here is installed into a user's project.

## New config keys

None.

## Pending-human actions

- **Run the probes.** Nothing here starts `claude`; a Claude Code session cannot run them (`make-scratch.sh` refuses). In a normal terminal: `tools/probes/make-scratch.sh "$HOME/probes/v1" --probe V1`, follow `tools/probes/README.md`, then `python3 tools/probes/analyze.py V1 <log>`. Priority order: V1, V5, V9, V18, V19 (on each OS you support), V22, V24, V27; then the rest. Roughly three to four hours in total for the priority eight.
- **Record the results.** Copy `docs/design/probe-results.template.md` to `docs/design/probe-results.md`, one row per run. For every row that overturns an assumption, make the change `docs/design/probe-assumptions.md` names.
- **Confirm the spellings the harness guesses**, each a single place: the 16 hook event names (`ALL_EVENTS` in `tools/probes/make_scratch.py`; symptom of a wrong one is a settings error naming the event); `onFailure` (V9 exists to settle it); `sandbox.failIfUnavailable` and `sandbox.filesystem.denyRead` (V19, V24); `CLAUDE_CODE_FORK_SUBAGENT` and `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` and whether the settings `env` block is applied early enough (V18); the `--setting-sources`, `--plugin-dir`, `--agents` flag spellings (V5, V13, V22); the guessed `sandbox:` frontmatter keys in the V24 agents (a no-op is the expected answer); the managed-settings paths in the V21 steps.
- **V27 needs real GitHub repos** on three plans and the human's own `gh` login; the steps create only disabled rulesets and delete them. An agent must not run it.
- Do not raise `claude_code_tested_max` from a probe result alone; IMP-21 ties it to the canary.

## Docs impact

- `README.md`: Development section, one line pointing at the probe harness (see pointers below).
- `GLOSSARY.md`: entry "probe" (a [probe] item of report §17; its harness is `tools/probes/`; assumed conservative until run) and "scratch repo" (the throwaway repo `make-scratch.sh` builds).
- `docs/faq.md` and the docs that say "UNVERIFIED" (`--bg` push, Workflow from a subagent, `worktree.baseRef`, a project `code-review` skill): point at `docs/design/probe-assumptions.md` for what is assumed and which probe settles it; replace the word only after a results row exists.
- `docs/v2-pending-human.md` (release stage): build from "Pending-human actions" above.
- `CHANGELOG.md` (release stage): one bullet for the probe harness; put the probes into the "Known unverified" list with a link to `docs/design/probe-assumptions.md`.

## Decisions made

- **Snippets are named `V<n>.json` or `V<n>-<variant>.json`, and variants are discovered from the file names.** V9, V18 and V23 require `--variant`; V9 has no plain snippet because three failing hooks installed together would confound each other. V9's control is `--control` (the same snippet minus `onFailure`), not a fourth file, so the two cannot drift.
- **Each probe machine-checks its answer from the hook log where it can.** Every prompt asks the model to run a tagged `echo probe-v<n>-...`, so the log holds the answer in a `tool_input.command`, independent of what the model says. V20 asks the model to echo the injected nonce back; V24 reads a held-out file through a script that prints a label; V18 compares timestamps of the parent's next action and the slow child's PostToolUse. Facts only the human can see (a dialog, a terminal message) are named in the section's Observe table.
- **V9 uses different tools for the three events** (Bash for PreToolUse, Write for PermissionRequest, a settings edit for ConfigChange) so one failing hook cannot mask another. The ConfigChange effect is observed through a deny rule on a file Read, which the hook either blocks or does not.
- **A base allow rule `Bash(echo probe-*)` goes into every scratch** so prompts do not stall on permission dialogs; probes that need other commands add their own allows in their snippet. `-p` probes use the project allow rules, which is itself part of what V5 measures.
- **The logger subscribes to 16 events**: the twelve of Phase 0a plus `ConfigChange`, `TeammateIdle`, `TaskCompleted` and `PostCompact`. `WorktreeCreate` and `WorktreeRemove` are left out on purpose: a WorktreeCreate hook replaces the platform's worktree creation, so a logger that prints nothing would break it. `observe.py` is reused unmodified.
- **`analyze.py` drops exact duplicate events** (the same hook registered from two settings sources in V5) and counts them in its header. Markers are never dropped.
- **`analyze.py` prints "reading" lines, not verdicts.** The human decides confirms/overturns/inconclusive in the results table; the script only says what the log shows and what each shape of answer would mean.
- **V15 and V27 have no scratch repo.** V15 is `claude plugin eval --help` plus one run; V27 is `gh api` calls against the human's own throwaway private repos. The V27 steps create `enforcement: disabled` rulesets and delete them; rule parameter names may need fixing, and the README says to fix a body that fails validation before concluding "unavailable".
- **V21 does not install managed settings.** The scratch writes the file to `.claude/probe/managed-settings.json` and the README tells the human to install it with sudo. A script that wrote under `/Library` or `/etc` would be a system-settings change.
- **V1 workflow agents (C) and V3 are manual.** The saved-workflow file format is not in the report; the steps send the human through the workflow-authoring skill instead of guessing a script.
- **Planned paths in `probe-assumptions.md` follow the report (§7.1, §10, §8),** not a built file: `.claude/scripts/guards/`, `.claude/scripts/loopctl.py`, `.claude/scripts/doctor.py`, `.claude/templates/settings/`, `docs/security/*`. The building phases may choose other paths; the conductor should reconcile at merge. They are allow-listed in `tools/check-refs.allow` so `check-refs` stays green until then.
- **Scratch repos are one per run and are never reused.** `make-scratch.sh` refuses a non-empty directory or an existing `<scratch>.observe.jsonl`/`.probe.json`, as `run-baseline.sh` does.

## Conductor: pointers to add

- `README.md`: Development section -> add "Platform probes: `tools/probes/README.md`; assumptions `docs/design/probe-assumptions.md`; results template `docs/design/probe-results.template.md`".
- `GLOSSARY.md`: new entries "probe" and "scratch repo" -> `tools/probes/README.md`.
- `docs/v2-pending-human.md`: build from the "Pending-human actions" above.
- `CHANGELOG.md`: "Known unverified" list -> link `docs/design/probe-assumptions.md`.
- `tools/check-refs.allow`: this topic appended 15 lines at the very end; expect a trivial textual merge if another topic also appended there. Once the planned files exist, `python3 tools/check-refs.py --list-unused-allow` shows which of these lines can be dropped.
- `docs/design/probe-assumptions.md`: after the other phases merge, check each "(planned)" path against what was built and fix the ones that differ.
