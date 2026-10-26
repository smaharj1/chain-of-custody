# Probe assumptions: what v2 assumes for every open item in report §17

No probe was run in the v2 build (IMP-7). For each V-item the build assumed the conservative answer.
This file records that assumption, **where it is encoded**, and **what changes if the probe says
otherwise**. The harness that confirms each one is `tools/probes/` (steps: `tools/probes/README.md`,
one section per item; results: `docs/design/probe-results.template.md`).

How to read the "Encoded in" lines: a path without a note exists today. A path marked *(planned)* is
where the report puts the code (§7.1 guards, §10 loopctl, §8 doctor); the phase that builds it should
use that path or update this file. Paths for later phases follow the report, not a built file.

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
| V13 | plugin hooks.json plus canary; `/init` can write project hooks | later |
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
- **Encoded in.** `.claude/scripts/kitlib/caller.py` (`classify`, rule 2 and rule 4; the module
  docstring quotes IMP-7), tests in `tests/kitlib/test_caller.py`; guard corpus rows for "agent_id
  present, agent_type missing" in `tests/guards/` *(planned)*; the shared guard library
  `.claude/scripts/guards/` *(planned)* calls `classify` and never reads `agent_type` alone.
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
- **Encoded in.** Phase 0b prose and the rules files under `.claude/rules/` *(planned)*.
- **If otherwise.** Not applicable; no probe is shipped.

### V3. Workflow per-agent `isolation` and `subagent_type` (P12)

- **Assumption.** Not available. `pipeline_host: workflow` is not implemented; reviews fan out from the
  conductor with sequential Agent calls (`pipeline_host: conductor`).
- **Encoded in.** The config registry: `pipeline_host` is absent from `.claude/scripts/kitlib/schema/*.json`
  today (or accepts only `conductor`) *(planned: Phase 8 adds `workflow`)*; docs in `.claude/skills/orchestrate/`
  *(planned)*.
- **If otherwise.** Phase 8 can add the `workflow` choice behind a saved workflow file. Per-agent worktree
  isolation would also give parallel reviewers write isolation; record the exact option names.

### V4. `context: fork` + `agent:` skill starts clean (P9)

- **Assumption.** Unknown, so no kit skill relies on it. Critiques are subagent dispatches.
- **Encoded in.** The skills under `.claude/skills/` *(planned)* use no `context: fork`; a lint in
  `tools/check-skills.py` *(planned)* may enforce it.
- **If otherwise.** If it starts clean, on-demand critique skills (`/critique`) become cheap to add;
  if it inherits the conversation, keep critics as explicit Agent dispatches and note it in the skill guide.

### V5. Driver child (`-p --agent`) inheritance: hooks, settings, sandbox, branch point

- **Assumption.** Nothing is inherited. The driver passes `--settings`, `--agents`, `--allowedTools` and
  `--permission-mode` explicitly, creates the worktree itself, and sets the working directory.
- **Encoded in.** The driver launch line in `.claude/scripts/loopctl.py` *(planned)* (report §11.1 item 2);
  the headless profile template (`permissions.defaultMode: dontAsk`, explicit `allowedTools`,
  `enableAllProjectMcpServers: false`) *(planned)*; the doctor's check that the headless profile has no
  `Workflow` allow *(planned: `.claude/scripts/doctor.py`)*; `.claude/scripts/kitlib/runstate.py` documents
  that held-out tests are hidden only under `runner: driver` because the child has its own sandbox settings.
- **If the probe shows otherwise.**
  - *Project hooks and settings are inherited:* the explicit flags stay (they are still the only
    reproducible surface), but the hook-liveness canary can rely on project hooks in the child and
    `loopctl` may drop the duplicated `--settings` hooks. Do not remove the flags.
  - *Sandbox is NOT applied to the child:* the driver must pass sandbox settings in `--settings` (the
    assumption already requires it) and the doctor must verify the child's effective sandbox with the
    liveness canary; held-out isolation under `runner: driver` is void until fixed.
  - *The child branches from somewhere other than the launch directory's HEAD:* `loopctl` must assert
    `git rev-parse HEAD` in the child's first report.

### V6. `isolation: worktree` base-branch resolution

- **Assumption.** Not used. The conductor and loopctl create worktrees from the recorded base.
- **Encoded in.** Worktree creation in `.claude/scripts/loopctl.py` *(planned)*; agent frontmatter under
  `.claude/agents/` has no `isolation:` key.
- **If otherwise.** If it branches from the current HEAD, `isolation: worktree` becomes a convenience for
  Phase 8; if from the remote default branch (D-3), it stays unusable for stacked items.

### V7. SubagentStart sees the dispatch prompt (D-16)

- **Assumption.** No. Brief-path enforcement is by report parse and prose, not a SubagentStart hook.
- **Encoded in.** The engineer-report validator in `.claude/scripts/loopctl.py` *(planned)* (the report
  must name the brief path); dispatch prose in `.claude/commands/tech-lead.md` (becomes
  `.claude/skills/tech-lead/` in Phase 9, *planned*).
- **If otherwise.** Add a SubagentStart guard in `.claude/scripts/guards/` *(planned)* that denies a
  dispatch whose prompt names no brief path. Keep the report check as the second layer.

### V8. SubagentStop / statusLine expose model, cost, tokens (P33)

- **Assumption.** No. Cost comes from the `-p` JSON output per driver child only; in-session dispatches
  have an estimated cost.
- **Encoded in.** The spend ledger in `.claude/scripts/loopctl.py` *(planned)* (§10.5);
  `tools/baseline/summarize.py` derives counts from tool events only.
- **If otherwise.** Log the fields into the spend ledger for in-session runners and tighten
  `--max-budget-usd` accounting; the ledger remains an estimate (P26).

### V9. `onFailure: block` with a missing interpreter, a crashing script, a timeout (P2, RC-30, IMP-16)

- **Assumption.** The field's name and placement are unverified and **nothing depends on them**. Every
  kit hook entry point fails closed itself: any exception or malformed input exits 2 with a reason. The
  one case the field alone would cover, a missing interpreter, is reported by the SessionStart check and
  caught by the liveness canary (a session whose nonce is missing is refused).
  `"onFailure": "block"` is still written on every blocking-capable hook.
- **Encoded in.** The fail-closed entry-point wrapper in `.claude/scripts/kitlib/` *(planned: hook runtime
  in `.claude/scripts/guards/`)*; `"onFailure": "block"` in the hooks template *(planned: settings
  templates, `.claude/templates/settings/`)*; the canary in the SessionStart hook and `loopctl` dispatch
  check *(planned)*; `tools/baseline/observe.py` is deliberately the opposite (always exit 0).
- **If the probe shows otherwise.**
  - *The field is misnamed or in the wrong place:* fix the spelling in the hooks template and the doctor's
    expected-settings check; because the wrappers fail closed already, no guard changes.
  - *A missing interpreter fails open even with the field:* the canary is the only protection; make the
    canary mandatory (not advisory) at every level above L1 and keep the SessionStart banner.
  - *A timeout fails open:* give every handler a `timeout` below the point where the platform proceeds and
    have the wrapper `exit 2` on its own deadline.
  - *ConfigChange fails open:* the ConfigChange blocker is not a boundary; B2/B3 stay the boundary.

### V10. `run_script` code visible in `tool_input` (P39). **Moot.** Cloud MCP is deny-only.

### V11. `--max-budget-usd` covers subagents. **Closed: yes, as an estimate (P26).**

### V12. `omitClaudeMd` effect on critics

- **Assumption.** Critics keep CLAUDE.md (no `omitClaudeMd`). A change needs an eval A/B.
- **Encoded in.** Critic frontmatter in `.claude/agents/code-reviewer.md`, `design-critic.md` and
  `security-reviewer.md` (no such key).
- **If otherwise.** If `omitClaudeMd: true` works, run the eval A/B before adding it; the saving is
  context tokens, the risk is a critic that misses project conventions.

### V13. Plugin `hooks.json` for plugin and project agents; `${CLAUDE_PLUGIN_ROOT}` in commands

- **Assumption.** The plugin ships `hooks.json`, and the liveness canary detects non-firing hooks. `/init`
  can also write project-level hooks, so a project does not depend on plugin hooks.
- **Encoded in.** `plugin/hooks/hooks.json` *(planned, Phase 9)*; the canary *(planned)*; `/init` skill
  *(planned)*.
- **If otherwise.** If plugin hooks do not fire for project agents (or at all), `/init` must always write
  the hooks into project settings and the plugin's hooks.json becomes optional; if
  `${CLAUDE_PLUGIN_ROOT}` does not expand in command text, commands use relative paths via the skill
  directory instead.

### V14. Do denials count toward the auto-mode pause? (P23)

- **Assumption.** Documentation only; moot under `dontAsk`, which the driver uses.
- **Encoded in.** The headless profile template *(planned)*; a note in `docs/faq.md` *(conductor, release)*.
- **If otherwise.** If denials count, interactive auto-mode users should be told that guard denials can pause
  auto mode; no code change.

### V15. `claude plugin eval --model` pin (P29)

- **Assumption.** No pin. Pin the Claude Code CLI version and set the model in the eval's settings.
- **Encoded in.** The eval runner docs and `tools/evals/` *(planned, Phase 10)*; `claude_code_tested_max`
  in `.claude/KIT_VERSION` (IMP-8) and `.claude/scripts/kitlib/levels.py` (`version_cap`).
- **If otherwise.** If `--model` works, the eval runner passes it and the eval record stores the model id.

### V16. Named spawn with teams off: teammate or subagent (P34)

- **Assumption.** Documentation only. Resume is by `agent_id` (SendMessage), never by `name`.
- **Encoded in.** The conductor prose in `.claude/commands/tech-lead.md` / skills *(planned)* ("resume by
  `agent_id`"); a guard-corpus row for `name` on an Agent call *(planned)*.
- **If otherwise.** None required; record the result for the FAQ.

### V17. `--bg` push behavior (P15)

- **Assumption.** Documentation only. Every statement that `--bg` can push or open draft PRs is UNVERIFIED
  until run. B1 and B4 (server-side) are the boundary, so the answer cannot widen what a bot can do.
- **Encoded in.** `--bg` is "not a runner" in §11.2; the docs say UNVERIFIED (`docs/faq.md`, conductor).
- **If otherwise.** Replace UNVERIFIED with the observed behavior in the docs.

### V18. Fork-mode env names and wait semantics (P36, RC-29)

- **Assumption.** Dispatch is event-driven: "done" is a valid report file *and* a loopctl state transition,
  never "the Agent call returned". The driver sets the fork-mode and background-task env explicitly per
  runner and the doctor asserts them. Critics and engineers are always dispatched with an explicit
  `subagent_type`, so they are never forks of the conductor.
- **Encoded in.** The event-driven wait in `.claude/scripts/loopctl.py` *(planned)* (report file plus state
  transition); the headless profile's `env` block (`CLAUDE_CODE_FORK_SUBAGENT`,
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`) *(planned)*; the doctor check `fork.env` *(planned)*; the
  orchestrate prose.
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
- **Encoded in.** The check id `sandbox.failIfUnavailable` and the attestation cap in
  `.claude/scripts/doctor.py` *(planned)*; the cap arithmetic in `.claude/scripts/kitlib/levels.py`
  (`effective_level`); the key `sandbox_verified` in `.claude/scripts/kitlib/schema/<phase>.json`
  *(planned)*; the sandbox profile snippet *(planned)*.
- **If the probe shows otherwise.**
  - *Fail-closed on every OS:* the doctor may accept `failIfUnavailable: true` in the effective settings as
    evidence for that OS (still with the attestation for the OS it was not run on).
  - *Fail-open on an OS:* keep the attestation requirement and add that OS to the doctor's "sandbox
    cannot be assumed" list; recommend a devcontainer or a separate OS user there.

### V20. SessionStart `compact` can inject `additionalContext` (§10.6, P30)

- **Assumption.** Implemented, with a fallback: orchestrate prose re-reads `loopctl status --json` at each
  stage, so a failed injection loses nothing but a few tokens.
- **Encoded in.** The SessionStart(`compact`) handler in `.claude/scripts/guards/` *(planned)* or
  `.claude/scripts/loopctl.py` *(planned)*; the re-read step in the orchestrate skill *(planned)*.
- **If otherwise.** If `compact` never fires or the context is not seen, delete the handler (keep the
  prose) and drop §10.6's first bullet; if it works, the prose re-read can be made conditional.

### V21. Can managed settings be overridden by the same OS user?

- **Assumption.** Yes, overridable. L3+ recommends a separate OS user or a credential-free container;
  managed settings are defence in depth only.
- **Encoded in.** The managed-settings recipe doc `docs/security/managed-settings.md` *(planned)*; the
  doctor's L3+ prerequisite "separate OS user or container" *(planned)*.
- **If otherwise.** If the same user cannot override a root-owned managed file, the recipe becomes a
  sufficient L3 prerequisite alone and the doctor may accept it; record which routes were tried.

### V22. `--agents <json>` + `--agent <role>` in `-p` with plugin-namespaced agents

- **Assumption.** The driver launch line is built and unit-tested only against the fake `claude`
  (`tests/helpers/fake_claude.py`); the first real run is the probe.
- **Encoded in.** The launch-line builder in `.claude/scripts/loopctl.py` *(planned)* and its tests
  `tests/loopctl/` *(planned)*; the fake in `tests/helpers/fake_claude.py`; `caller.normalize_agent_type`
  in `.claude/scripts/kitlib/caller.py` strips a `<plugin>:` prefix so both spellings classify.
- **If the probe shows otherwise.** Change only the builder (flag spelling, which spelling of the role the
  roster JSON uses, whether `--agents` can override a project agent) and its golden test; the roster
  prefix normalisation stays.

### V23. Can a subagent launch a Workflow? Does an allow rule let a `-p` child?

- **Assumption.** `Workflow` is denied in the headless profile and in engineers' `disallowedTools`; the
  doctor fails on any `Workflow` allow outside the human-run `/orchestrate` profile. The kit behaves the
  same whatever the answer.
- **Encoded in.** The deny block and headless profile templates *(planned)*; the doctor check
  `settings.workflow-allow` *(planned)*; the boundary fixture "Workflow headlessly" in `tests/boundary/`
  *(planned)*.
- **If otherwise.** Nothing to change; replace UNVERIFIED with the observed behavior in §11.1 and P13.

### V24. Does a subagent's Bash honor a different sandbox from its parent? (RC-6, RC-31, P42)

- **Assumption.** No per-subagent sandbox. Held-out isolation under `in-session` and `sequential` is
  impossible; held-out tests are driver-only or CI-only, and L2+ requires `runner: driver` or CI-only G3.
- **Encoded in.** `.claude/scripts/kitlib/runstate.py` (held-out directory "hidden from engineers only
  under runner: driver"); the level prerequisite for G3 in `.claude/scripts/doctor.py` *(planned)*; DEC-11
  in the config registry *(planned)*.
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
- **Encoded in.** Worktree creation in `.claude/scripts/loopctl.py` *(planned)*; §19.7 notes.
- **If otherwise.** Replace UNVERIFIED with the result; no code change.

### V26. Does a project `code-review` skill override the bundled one? (L-45)

- **Assumption.** Unknown; the kit never names a skill `code-review`.
- **Encoded in.** The skill name list under `.claude/skills/` *(planned)* and a lint that rejects the name.
- **If otherwise.** None required; if a project skill wins, mention it in the skill guide as a way to
  customise review.

### V27. GitHub plan features on private repos (RC-2, P43)

- **Assumption.** Unknown means unavailable. The doctor caps the effective level unless the human attests
  the feature (`rulesets`, `code-owner review`, `push rulesets`, `merge queue`).
- **Encoded in.** The platform-cap computation in `.claude/scripts/doctor.py` *(planned)* using
  `.claude/scripts/kitlib/levels.py` (`Cap`, `effective_level`); the platform table in
  `docs/security/platform-requirements.md` *(planned)*; attestation keys in
  `.claude/scripts/kitlib/schema/<phase>.json` *(planned)*.
- **If the probe shows otherwise.** For each feature found available on a plan, move it from "attest"
  to "doctor reads it with `gh api`" for that plan and update the §8.4 table row; features found
  unavailable keep their fallback.
