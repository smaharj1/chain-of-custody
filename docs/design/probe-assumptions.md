# Probe assumptions: what v2 assumes for every open item in report §17

No probe was run in the v2 build (IMP-7). For each V-item the build assumed the conservative answer.
This file records that assumption, **where it is encoded**, and **what changes if the probe says
otherwise**. The harness that confirms each one is `tools/probes/` (steps: `tools/probes/README.md`,
one section per item; results: `docs/design/probe-results.template.md`).

How to read the "Encoded in" lines: every path named exists in the 2.0.0 tree. A line that says *not
built* names something the build did not make; a line that says *unverified* names platform behaviour no
probe has confirmed yet. The probe results (`docs/design/probe-results.md` *(planned: a person creates it from the template)*) are still
to be filled in by a person with the real `claude`; nothing here has been confirmed.

Unknown never widens trust. Where a probe could only make the kit *less* restrictive, the build stays
restrictive until the human records the result and makes the change in the place named here.

Summary:

| Item | Build assumes | Probe priority |
|---|---|---|
| V1 | agent-blind: unknown `agent_type` with an `agent_id` is an engineer | 1 |
| V2 | closed (yes) | - |
| V3 | `pipeline_host: workflow` not implemented | later |
| V4 | no kit skill relies on `context: fork` | later |
| V5 | nothing inherited by a driver child; everything passed explicitly | 2 |
| V6 | `isolation: worktree` not used | later |
| V7 | no brief-path enforcement at SubagentStart | later |
| V8 | cost comes from `-p` JSON per child only | later |
| V9 | kit hooks fail closed themselves; canary detects non-firing | 3 |
| V10, V11 | moot / closed | - |
| V12 | critics keep CLAUDE.md | later |
| V13 | plugin hooks.json is the only wiring; canary detects non-firing | later |
| V14, V16, V17, V25, V26 | documentation only | later |
| V15 | pin the CLI instead of `eval --model` | later |
| V18 | event-driven dispatch; fork env set explicitly; never trust a blocking return | 4 |
| V19 | doctor reads settings and needs a human attestation; unknown means L1 | 5 |
| V20 | implemented, with the `loopctl status --json` re-read fallback | later |
| V21 | managed settings are overridable by the same OS user | later |
| V22 | driver launch line unit-tested against the fake `claude` only | 6 |
| V23 | `Workflow` denied headlessly and to engineers; doctor fails on a stray allow | later |
| V24 | no per-subagent sandbox; held-out tests driver-only or CI-only | 7 |
| V27 | unknown means unavailable; doctor caps the level unless attested | 8 |

---

### V1. `agent_id` / `agent_type` at depth 2-3, in Workflow agents and driver children (P1)

- **Assumption.** Agent-blind. A call with `agent_id` but a missing or off-roster `agent_type` is an
  engineer (restrictive). A call with no `agent_id` and an unregistered `session_id` gets least
  privilege whatever `agent_type` says. `agent_type` is only trusted at depth 1 where the platform
  documents it.
- **Encoded in.** `scripts/kitlib/caller.py` (`classify`, rule 2 and rule 4; the module
  docstring quotes IMP-7), tests in `tests/kitlib/test_caller.py`; guard corpus rows for "agent_id
  present, agent_type missing" (`caller-018`) and an unregistered `--agent` session (`caller-007`,
  `caller-010`) in `tests/guards/corpus/callers.json`; the guard library `scripts/guards/`
  (`lib.py`) calls `classify` and never reads `agent_type` alone. Unverified: the hook input at depth
  2-3 and in Workflow agents.
- **If the probe shows otherwise.**
  - *Both fields present at every depth:* roster-aware rules (critic vs engineer vs unknown) can apply
    at depth 2-3. Change `classify` rule 2 only if the off-roster branch should become `unknown-subagent`
    instead of `engineer`; add corpus rows for depth 2 and 3 critics. No safety change is required.
  - *`agent_id` absent at depth 2-3:* a nested subagent is indistinguishable from its parent session.
    The registered-session rule (rule 3) would give a nested engineer the parent's rights. Treat as a
    blocker: cap `max_depth` at 1 in the roster defaults and in the engineer/critic `disallowedTools`
    (`Agent`), and document that item-runner nesting is unsafe.
  - *Driver child carries `agent_id`:* rule 3 (driver child with a registered role) would never match;
    revisit session registration for children.

### V2. Do the rules files load in subagents? **Closed: yes [docs] (P21).**

- **Assumption.** Rules load in subagents, so path-scoped rules can carry invariants (R9).
- **Encoded in.** Phase 0b prose. The kit ships no rules directory; a project's own rules files (`.claude/rules/<topic>.md`) and the drafts `/retro` proposes (`.claude/rules/<name>.md`) rely on this.
- **If otherwise.** Not applicable; no probe is shipped.

### V3. Workflow per-agent `isolation` and `subagent_type` (P12)

- **Assumption.** Not available. `pipeline_host: workflow` is not implemented; reviews fan out from the
  conductor with sequential Agent calls (`pipeline_host: conductor`).
- **Encoded in.** The config key `pipeline_host` (`conductor` | `workflow`, evidence-gated) in
  `scripts/kitlib/schema/8-parallel.json`; the doctor check `parallel.pipeline-host`
  (`scripts/doctorlib/parallel.py`), which reports `workflow` as an error whatever the evidence,
  and the `loopctl run` preflight row of the same name; the design note
  `spec/pipeline-host-workflow.md`; the host paragraph of `skills/orchestrate/SKILL.md`.
  Nothing implements the `workflow` value.
- **If otherwise.** The `workflow` choice can be built behind a saved workflow file, following the design note. Per-agent worktree
  isolation would also give parallel reviewers write isolation; record the exact option names.

### V4. `context: fork` + `agent:` skill starts clean (P9)

- **Assumption.** Unknown, so no kit skill relies on it. Critiques are subagent dispatches.
- **Encoded in.** The skills under `skills/` use no `context: fork`;
  `tests/test_frontmatter.py` asserts that no mode forks.
- **If otherwise.** If it starts clean, on-demand critique skills (`/critique`) become cheap to add;
  if it inherits the conversation, keep critics as explicit Agent dispatches and note it in the skill guide.

### V5. Driver child (`-p --agent`) inheritance: hooks, settings, sandbox, branch point

- **Assumption.** Nothing is inherited. The driver passes `--plugin-dir`, `--settings`, `--agents`,
  `--allowedTools` and `--permission-mode` explicitly, creates the worktree itself, and sets the working
  directory. A child's hooks come only from the plugin it is given with `--plugin-dir` (`hooks/hooks.json`).
- **Encoded in.** The driver launch line, `launch.build_argv` and `launch.build_settings` in
  `scripts/loopctl/launch.py`, run by `scripts/loopctl/driver.py` (report §11.1 item 2).
  `build_settings` reads the kit's template for the child's kind
  (`templates/settings/headless-child.settings.json`, or `critic.settings.json` for a critic:
  `permissions.defaultMode: dontAsk`, explicit `allowedTools`, `enableAllProjectMcpServers: false`), adds
  the project's verify commands as allow rules, write-denies the runstate directory, read-denies the
  held-out tests, sets the fork-mode variables (V18), and drops any `hooks` key, so nothing is wired
  twice. `launch.validate_settings` refuses a profile that carries hooks of its own or a `Workflow` or
  `claude` allow; the doctor's `settings.workflow-allow` check (`scripts/doctorlib/settings.py`) does the
  same for the project's settings;
  `scripts/kitlib/runstate.py` documents that held-out tests are hidden only under
  `runner: driver` because the child has its own sandbox settings. Unverified on the real CLI:
  `--session-id` with `-p`, and `--allowedTools` taking one argument per rule.
- **If the probe shows otherwise.**
  - *Project settings are inherited:* the explicit flags stay (they are still the only reproducible
    surface). Check that an inherited project setting cannot widen the child's profile. Do not remove the
    flags.
  - *Plugin hooks do not load through `--plugin-dir`:* the child runs with no guards. The canary check
    refuses the child (`startrun.py`, `launch.py`); treat it as a blocker for `runner: driver`.
  - *Sandbox is NOT applied to the child:* the driver must pass sandbox settings in `--settings` (the
    assumption already requires it) and the doctor must verify the child's effective sandbox with the
    liveness canary; held-out isolation under `runner: driver` is void until fixed.
  - *The child branches from somewhere other than the launch directory's HEAD:* `loopctl` must assert
    `git rev-parse HEAD` in the child's first report.

### V6. `isolation: worktree` base-branch resolution

- **Assumption.** Not used. The conductor and loopctl create worktrees from the recorded base.
- **Encoded in.** Worktree creation in `scripts/loopctl/parallel/worktrees.py` (a plain
  `git worktree add <path> <existing branch>`, never `isolation: worktree` or `worktree.baseRef`) and
  `startrun.py` for the loop worktree; agent frontmatter under `agents/` has no `isolation:` key.
  The opt-in hooks `scripts/worktree_hook.py` and `templates/parallel/worktree-hooks.settings.json`
  assume the `WorktreeCreate` / `WorktreeRemove` event names, their input fields (`name`, `worktree_path`,
  `cwd`) and that the platform takes the path from the hook's standard output; all unverified, and the plugin's
  `hooks.json` does not wire them.
- **If otherwise.** If it branches from the current HEAD, `isolation: worktree` becomes a convenience for
  parallel items; if from the remote default branch (D-3), it stays unusable for stacked items. If the
  hook names or fields differ, change `worktree_hook.py` and the snippet together.

### V7. SubagentStart sees the dispatch prompt (D-16)

- **Assumption.** No. Brief-path enforcement is by report parse and prose, not a SubagentStart hook.
- **Encoded in.** The engineer-report validator `scripts/loopctl/report.py` (the JSON trailer's
  `report_path` must be the path the brief named: `trailer-mismatch`); dispatch prose in
  `skills/tech-lead/SKILL.md`. The SubagentStart hook only records telemetry
  (`scripts/guards/telemetry.py`).
- **If otherwise.** Add a SubagentStart guard in `scripts/guards/` that denies a
  dispatch whose prompt names no brief path. Keep the report check as the second layer.

### V8. SubagentStop / statusLine expose model, cost, tokens (P33)

- **Assumption.** No. Cost comes from the `-p` JSON output per driver child only; in-session dispatches
  have an estimated cost.
- **Encoded in.** The spend ledger `scripts/loopctl/ledger.py` (§10.5); an unknown cost parks the
  run (IMP-25) and an unattended run estimates the cost of a child it had to kill
  (`scripts/loopctl/unattended.py`); `tools/baseline/summarize.py` derives counts from tool
  events only. Unverified: `total_cost_usd` and the `subtype` values of the `-p` JSON result.
- **If otherwise.** Log the fields into the spend ledger for in-session runners and tighten
  `--max-budget-usd` accounting; the ledger remains an estimate (P26).

### V9. `onFailure: block` with a missing interpreter, a crashing script, a timeout (P2, RC-30, IMP-16)

- **Assumption.** The field's name and placement are unverified and **nothing depends on them**. Every
  kit hook entry point fails closed itself: any exception or malformed input exits 2 with a reason. The
  one case the field alone would cover, a missing interpreter, is reported by the SessionStart check and
  caught by the liveness canary (a session whose nonce is missing is refused).
  `"onFailure": "block"` is still written on every blocking-capable hook.
- **Encoded in.** The fail-closed entry point `scripts/guards/run.py` with `engine.py`;
  `"onFailure": "block"` written by `scripts/guards/wiring.py` into the plugin's `hooks/hooks.json`, the
  only place the guards are wired (`templates/settings/` holds permission templates with no hooks); the
  doctor row `guards.on-failure`, which reads `hooks/hooks.json`; the canary in `scripts/session_start.py` and the `loopctl` canary check
  (`startrun.py`, `launch.py`); `tools/baseline/observe.py` is deliberately the opposite (always exit 0).
  Unverified: the field name and placement, and the behaviour on a missing interpreter or a timeout.
- **If the probe shows otherwise.**
  - *The field is misnamed or in the wrong place:* fix the spelling in `wiring.py`, regenerate
    `hooks/hooks.json` with `wiring.py --write`, and fix the doctor's `guards.on-failure` check; because the wrappers fail closed already, no guard changes.
  - *A missing interpreter fails open even with the field:* the canary is the only protection; make the
    canary mandatory (not advisory) at every level above L1 and keep the SessionStart banner.
  - *A timeout fails open:* give every handler a `timeout` below the point where the platform proceeds and
    have the wrapper `exit 2` on its own deadline.
  - *ConfigChange fails open:* the ConfigChange blocker is not a boundary; B2/B3 stay the boundary.

### V10. `run_script` code visible in `tool_input` (P39). **Moot.** Cloud MCP is deny-only.

### V11. `--max-budget-usd` covers subagents. **Closed: yes, as an estimate (P26).**

- **Encoded in.** `Ledger.launch_budget` (`scripts/loopctl/ledger.py`) passes the remaining budget
  as `--max-budget-usd`; with parallel workers it reserves a share per live child (`live_children`), so
  the sum of the allowances cannot pass the ceiling. Unverified: how far a child that is killed can
  overshoot its flag. The unattended supervisor therefore charges a killed child an estimate between
  half and all of its allowance (`docs/unattended.md`).

### V12. `omitClaudeMd` effect on critics

- **Assumption.** Critics keep CLAUDE.md (no `omitClaudeMd`). A change needs an eval A/B.
- **Encoded in.** Critic frontmatter in `agents/code-reviewer.md`, `design-critic.md` and
  `security-reviewer.md` (no such key).
- **If otherwise.** If `omitClaudeMd: true` works, run the eval A/B before adding it; the saving is
  context tokens, the risk is a critic that misses project conventions.

### V13. Plugin `hooks.json` for plugin and project agents; `${CLAUDE_PLUGIN_ROOT}` in commands

- **Assumption.** Plugin hooks fire for the plugin's agents and for project agents, and
  `${CLAUDE_PLUGIN_ROOT}` expands in hook commands and in skill and agent text. The plugin's `hooks.json`
  is the only wiring of the guards: `/init` writes no hooks, and the doctor reports a second wiring of
  the kit's scripts in project settings. The liveness canary detects hooks that do not fire.
- **Encoded in.** `hooks/hooks.json` (generated by `wiring.py --write` from
  `scripts/guards/hooks.manifest.json`; every handler runs a script under `${CLAUDE_PLUGIN_ROOT}/scripts/`); the
  canary (`scripts/session_start.py`); the doctor row `guards.wiring` and `wiring.py --check --project`;
  the `/init` skill (`skills/init/SKILL.md`, `scripts/init_project.py`), which writes permissions and the
  sandbox only. Unverified: all of it, on the real binary (`tools/validate-plugin-real.sh` and
  `claude --plugin-dir .` in a scratch project).
- **If otherwise.** If plugin hooks do not fire for project agents (or at all), the guards have no other
  wiring: the kit needs a project-level wiring again (generated by `wiring.py`, written by `/init`), and
  `guards.wiring` must accept it instead of reporting it as a second wiring. If `${CLAUDE_PLUGIN_ROOT}`
  does not expand in hook commands, the handlers need absolute paths written at install time; if it does
  not expand in skill and agent text, the prose's `${CLAUDE_PLUGIN_ROOT}/...` paths need another form.

### V14. Do denials count toward the auto-mode pause? (P23)

- **Assumption.** Documentation only; moot under `dontAsk`, which the driver uses.
- **Encoded in.** The headless profile template `templates/settings/headless-child.settings.json`
  (`dontAsk`); `docs/faq.md` points here for what is assumed.
- **If otherwise.** If denials count, interactive auto-mode users should be told that guard denials can pause
  auto mode; no code change.

### V15. `claude plugin eval --model` pin (P29)

- **Assumption.** No pin. Pin the Claude Code CLI version and set the model in the eval's settings.
- **Encoded in.** `tools/evals/` (`run_local.py` grades recorded transcripts; `ablate.py` reads recorded
  ablation transcripts); `claude_code_tested_max` in `KIT_VERSION` (IMP-8) and
  `scripts/kitlib/levels.py` (`version_cap`). P27 (the flags of `claude plugin eval --ablation`)
  is used by the harness review (`docs/harness-review.md`); if the flags differ, `tools/evals/ablate.py`
  works from hand-recorded transcripts.
- **If otherwise.** If `--model` works, the eval runner passes it and the eval record stores the model id.

### V16. Named spawn with teams off: teammate or subagent (P34)

- **Assumption.** Documentation only. Resume is by `agent_id` (SendMessage), never by `name`.
- **Encoded in.** The conductor prose in `skills/tech-lead/SKILL.md` and
  `skills/orchestrate/SKILL.md` ("resume by `agent_id`"). There is no guard-corpus row for `name`
  on an Agent call.
- **If otherwise.** None required; record the result for the FAQ.

### V17. `--bg` push behavior (P15)

- **Assumption.** Documentation only. Every statement that `--bg` can push or open draft PRs is UNVERIFIED
  until run. B1 and B4 (server-side) are the boundary, so the answer cannot widen what a bot can do.
- **Encoded in.** `--bg` is "not a runner" in §11.2; the docs say UNVERIFIED (`docs/faq.md`).
- **If otherwise.** Replace UNVERIFIED with the observed behavior in the docs.

### V18. Fork-mode env names and wait semantics (P36, RC-29)

- **Assumption.** Dispatch is event-driven: "done" is a valid report file *and* a loopctl state transition,
  never "the Agent call returned". The driver sets the fork-mode and background-task env explicitly per
  runner and the doctor asserts them. Critics and engineers are always dispatched with an explicit
  `subagent_type`, so they are never forks of the conductor.
- **Encoded in.** The event-driven wait in `scripts/loopctl/driver.py` (a valid report file plus
  a state transition) and the child launch in `launch.py`; the headless profile's `env` block
  (`launch.FORK_ENV`: `CLAUDE_CODE_FORK_SUBAGENT=0`, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`); the doctor
  check `fork.env` (`scripts/doctorlib/extras.py`, `FORK_ENV_EXPECTED`); the orchestrate
  prose. If a name is wrong, change `launch.FORK_ENV` and `FORK_ENV_EXPECTED` together.
- **If the probe shows otherwise.**
  - *The env names are wrong:* fix them in the headless profile and the doctor; until fixed the driver
    relies on the event-driven wait alone, which is safe.
  - *Fork-off makes the Agent call block reliably in every surface:* a blocking return may be used as a
    *hint* to poll sooner, never as proof of completion.
  - *The parent blocks in a surface the report says it does not (or the reverse):* update §11.3's table in
    the docs and the doctor's per-runner expectation; the event-driven design needs no change.

### V19. Sandbox `failIfUnavailable` on macOS, Linux, devcontainer (P38)

- **Assumption.** Unknown. The doctor reads the sandbox settings and requires a human attestation
  `sandbox_verified: <date>`; without it the effective level is L1. At L2+ the sandbox is mandatory (B3),
  so an unattested sandbox keeps L2+ off.
- **Encoded in.** The check ids `sandbox.failIfUnavailable`, `sandbox.deny-paths`,
  `sandbox.registry-path` and `sandbox.attestation`, and the cap `levelcalc.cap_sandbox`
  (`scripts/doctorlib/settings.py`, `levelcalc.py`); the cap arithmetic in
  `scripts/kitlib/levels.py` (`effective_level`); the key `sandbox_verified` in
  `scripts/kitlib/schema/1-boundary.json`; the sandbox profile
  `templates/security/sandbox.settings.json` and `wiring.py --sandbox-snippet`
  ([sandbox.md](../security/sandbox.md)). Unverified: the `sandbox.*` key names, the `Edit(//absolute/path)`
  spelling and glob entries in `denyWrite`, and whether a child's sandbox honours them.
- **If the probe shows otherwise.**
  - *Fail-closed on every OS:* the doctor may accept `failIfUnavailable: true` in the effective settings as
    evidence for that OS (still with the attestation for the OS it was not run on).
  - *Fail-open on an OS:* keep the attestation requirement and add that OS to the doctor's "sandbox
    cannot be assumed" list; recommend a devcontainer or a separate OS user there.

### V20. SessionStart `compact` can inject `additionalContext` (§10.6, P30)

- **Assumption.** Implemented, with a fallback: orchestrate prose re-reads `loopctl status --json` at each
  stage, so a failed injection loses nothing but a few tokens.
- **Encoded in.** The SessionStart(`compact`) handler `scripts/loopctl/compact_hook.py` (an entry
  of `scripts/guards/hooks.manifest.json`); the re-read of `loopctl status --json` at each stage in
  `skills/orchestrate/SKILL.md`.
- **If otherwise.** If `compact` never fires or the context is not seen, delete the handler (keep the
  prose) and drop §10.6's first bullet; if it works, the prose re-read can be made conditional.

### V21. Can managed settings be overridden by the same OS user?

- **Assumption.** Yes, overridable. L3+ recommends a separate OS user or a credential-free container;
  managed settings are defence in depth only.
- **Encoded in.** The managed-settings recipe in `docs/security/sandbox.md` and the template
  `templates/security/managed-settings.example.json` (there is no separate
  `managed-settings.md`); the doctor's L3 prerequisite "managed settings or credential-free container"
  (`managed.settings`, `levelcalc.py`). The managed-settings file paths are unverified.
- **If otherwise.** If the same user cannot override a root-owned managed file, the recipe becomes a
  sufficient L3 prerequisite alone and the doctor may accept it; record which routes were tried.

### V22. `--agents <json>` + `--agent <role>` in `-p` with plugin-namespaced agents

- **Assumption.** The driver launch line is built and unit-tested only against the fake `claude`
  (`tests/helpers/fake_claude.py`); the first real run is the probe.
- **Encoded in.** The launch-line builder `launch.build_argv` in `scripts/loopctl/launch.py`
  (`--agents` carries `{role: {description, prompt, tools, disallowedTools, model, effort}}`) and its tests
  (`tests/loopctl/fault_injection/test_launch_unit.py`); the fake in `tests/helpers/fake_claude.py`; `caller.normalize_agent_type`
  in `scripts/kitlib/caller.py` strips a `<plugin>:` prefix so both spellings classify.
- **If the probe shows otherwise.** Change only the builder (flag spelling, which spelling of the role the
  roster JSON uses, whether `--agents` can override a project agent) and its golden test; the roster
  prefix normalisation stays.

### V23. Can a subagent launch a Workflow? Does an allow rule let a `-p` child?

- **Assumption.** `Workflow` is denied in the headless profile and in engineers' `disallowedTools`; the
  doctor fails on any `Workflow` allow outside the human-run `/orchestrate` profile. The kit behaves the
  same whatever the answer.
- **Encoded in.** The deny block and headless profile templates (`templates/settings/`);
  `launch.validate_settings` (refuses a profile that allows `Workflow`); the doctor checks
  `settings.workflow-allow` and `settings.agent-workflow` (`scripts/doctorlib/settings.py`); the
  "Workflow allow" fixtures in `tests/loopctl/fault_injection/` and `tests/doctor/test_adversarial.py`
  `pipeline_host: workflow` is refused (V3).
- **If otherwise.** Nothing to change; replace UNVERIFIED with the observed behavior in §11.1 and P13.

### V24. Does a subagent's Bash honor a different sandbox from its parent? (RC-6, RC-31, P42)

- **Assumption.** No per-subagent sandbox. Held-out isolation under `in-session` and `sequential` is
  impossible; held-out tests are driver-only or CI-only, and L2+ requires `runner: driver` or CI-only G3.
- **Encoded in.** `scripts/kitlib/runstate.py` (held-out directory "hidden from engineers only
  under runner: driver"); the L2 prerequisite "held-out isolation (RC-6)" in
  `scripts/doctorlib/levelcalc.py` (`runner: driver`, or `g3_ci_only` in `platform_attested`);
  G3's `g3.heldout-isolation` finding (`spec/loopctl-gates.md`); the CI half in
  `templates/github/workflows/heldout.yml` with `heldout-sync.yml`. The child profile's
  `denyRead` of the held-out directory and the V24 sandbox frontmatter keys are unverified.
- **If the probe shows otherwise.**
  - *A subagent can use a stricter sandbox via frontmatter:* in-session held-out isolation becomes possible
    with a documented frontmatter key; add it to the engineer agents and relax the L2 prerequisite only
    after a second probe on a different OS.
  - *A subagent's Bash is outside the parent's sandbox:* much worse than assumed; the doctor must also
    refuse `in-session` runners at L2+ for any project that has sandbox-dependent guards.
  - *Frontmatter can relax the sandbox (probe-sb-loose reads the file):* a boundary hole; add a guard for
    `sandbox` keys in agent frontmatter under the H6 paths and tell upstream.

### V25. `worktree.baseRef: "head"` (L-45)

- **Assumption.** Documentation only. The conductor creates worktrees itself, so the setting is not used.
- **Encoded in.** Worktree creation in `scripts/loopctl/parallel/worktrees.py` (see V6); §19.7 notes.
- **If otherwise.** Replace UNVERIFIED with the result; no code change.

### V26. Does a project `code-review` skill override the bundled one? (L-45)

- **Assumption.** Unknown; the kit never names a skill `code-review`.
- **Encoded in.** The skill name list under `skills/`; `tests/test_frontmatter.py` (`RESERVED`
  rejects `loop` and `code-review`).
- **If otherwise.** None required; if a project skill wins, mention it in the skill guide as a way to
  customise review.

### V27. GitHub plan features on private repos (RC-2, P43)

- **Assumption.** Unknown means unavailable. The doctor caps the effective level unless the human attests
  the feature (`rulesets`, `code-owner review`, `push rulesets`, `merge queue`).
- **Encoded in.** The platform-cap computation in `scripts/doctorlib/levelcalc.py`
  (`cap_platform`) using `scripts/kitlib/levels.py` (`Cap`, `effective_level`); the platform table
  in `docs/security/rulesets.md` ("Platform requirements"; there is no `platform-requirements.md`); the
  attestation key `platform_attested` in `scripts/kitlib/schema/1-boundary.json`. Needs real
  GitHub plans to settle.
- **If the probe shows otherwise.** For each feature found available on a plan, move it from "attest"
  to "doctor reads it with `gh api`" for that plan and update the §8.4 table row; features found
  unavailable keep their fallback.

---

## Other platform names the build relies on (no V number)

- **Notification hooks** (`templates/notify/notify-hooks.settings.json`, `scripts/notify.py`).
  The events `Notification`, `StopFailure` and `PermissionRequest`, and the input fields the hook reads
  (an event name, a notification type, a tool name), are unverified. The hook takes no free text from its
  input, so a wrong field name loses a notification, not safety. If the names differ, change the snippet and
  `notify.py --hook` together.
- **`CLAUDE_CODE_ENTRYPOINT`** as a second session marker beside `CLAUDECODE` (`scripts/humanonly.py`):
  assumed to be set in a Claude Code session; a human-only script refuses when either is present, even empty.
  A process can unset both, so this is a tripwire, not a boundary.
- **CLI flags** used outside the driver launch line: `--plugin-dir` (the real-binary plugin check,
  `tools/validate-plugin-real.sh`, `docs/packaging.md`) and `--setting-sources` (the CI workflows
  `claude-review.yml` and `intake.yml`, which `ci_lint.py` checks for). Unverified on the pinned CLI.
