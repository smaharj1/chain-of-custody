# 0b-prose: truth, prose and frontmatter pass (report §19.6, §19.7, IMP-17, IMP-20)

## Changes

Summary: every prose and frontmatter fix of Phase 0b, no new runtime mechanism. New shared rules went into small `.claude/context/` files and are pointed to, not pasted (CO-35). Tests: `tests/test_frontmatter.py` (13) and `tests/test_prose_references.py` (8) are new; `tests/test_config_template.py` was adjusted (see Decisions). Whole suite: 243 tests, about 9 s.

New files:
- `.claude/context/model-profiles.md` (§11.4, IMP-17): `quality` / `balanced` / `economy`, precedence, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`, round-2 escalation to opus `xhigh`.
- `.claude/context/migrations.md` (CO-18): expand, backfill (separate, idempotent, resumable job), contract, each its own item; down-migration data-loss classes `none | lossy-recoverable | lossy`; lossy downs are human-gated.
- `.claude/context/local-dev-rules.md` (DEC-8, L-27, L-38, CO-2 pointer): dialect parity, process groups and ports, readiness without sleep loops, secrets pointer.
- `.claude/context/probe-protocol.md` (CO-14): list the commands first, explicit go, sandboxed run with the network off via `.claude/scripts/probe_run.py` (a Phase 1 file).
- `.claude/context/platform-notes.md`: permissions snippet (CO-16, L-41), capabilities and fallback orders (P-9), `.claude/launch.json` (A27), documentation tools optional (A1, A26), SessionStart pattern (P-8.8), third-party skills (CO-46), root `skills/` collision note (P-18.7).
- `.claude/context/choosing-your-stack.md`: `git mv` from `docs/choosing-your-stack.md` (IMP-20, CO-47); the GLOSSARY link inside it was fixed.
- `.claude/templates/domain-glossary.md` (CO-30, IMP-20), `.claude/templates/rule.md` (path-scoped rule template for R9 / D-25 / A8).
- `.claude/scripts/kitlib/schema/0b-prose.json`, tests above.

### §19.6 checklist (row, file, what changed)

1. `.claude/CLAUDE.md` default posture (A30, CO-36): only for interactive sessions with no role prompt; a driver child or subagent follows its own prompt. Also: stack-guide path moved, code-reviewer comment no longer says "interactive uses the skill", domain-glossary pointer.
2. `.claude/commands/pm.md` brainstorming skill (P-3, CO-46): inline 8-step interview checklist using `AskUserQuestion`.
3. `.claude/commands/pm.md` default session (CO-36): same child exception.
4. `.claude/commands/pm.md` autonomous mode (L-7, P-16, F-34): `NEEDS_CLARIFICATION` (answerable from the plan) vs `ESCALATION_NEEDED` (needs a human) vs `PLAN_REVISION_NEEDED`.
5. `.claude/commands/planner.md` brainstorming (P-3): inline, `AskUserQuestion`.
6. `.claude/commands/planner.md` stack guide (CO-47): reads `.claude/context/choosing-your-stack.md`, read optional with a stated fallback.
7. `.claude/commands/planner.md` SQLite default (L-38, DEC-8, RC-21): in-process prod dialect, then container; SQLite only if prod is SQLite/libSQL; otherwise plan capped at L1 with a doctor error; never a silent fallback.
8. `.claude/commands/planner.md` allowlist offer (CO-16): prints a `settings.local.json` snippet for the human; agent writes to settings are denied (H6).
9. `.claude/commands/planner.md` "run each command once" (CO-14): sandboxed probe, list shown first, explicit go at L0-L1.
10. `.claude/commands/planner.md` decomposition (L-35, D-7): slicing rules, walking skeleton first, `size: S|M|L`, no `L` survives, 2a/2b/2c splits. Also records the plan approval (L-10).
11. `.claude/context/primer-protocol.md` run-once (CO-14): same probe. Also: documentation tool optional (A1), primer-delta step no longer numbered, new "Rules" section for path-scoped rules (D-25).
12. `.claude/commands/orchestrate.md` Workflow ban (D-8): optional `pipeline_host: workflow`, saved workflow only, never from a subagent or headless; not shipped yet (no config key).
13. `.claude/commands/orchestrate.md` lock (L-11, F-7): marked the V1 stopgap; loopctl O_EXCL lock replaces it in Phase 4.
14. `.claude/commands/orchestrate.md` capabilities (P-9, A13, CO-16): fixed fallback orders from `platform-notes.md`, no tool named; settings snippet for the human only.
15. `.claude/commands/orchestrate.md` design-critic cap (L-34): `max_design_rounds`, round 2 limited to round-1 blockers plus new Critical.
16. `.claude/commands/orchestrate.md` merge (CO-15, R5): dedicated clone; merge target is the clone's `main` (`delivery` will decide once it exists); refuse on a dirty tree or a diverged base (`git merge-base --is-ancestor main <branch>`).
17. `.claude/commands/tech-lead.md` security review (F-22): kept not-auto-dispatched, added that CI is the uniform gate.
18. `.claude/commands/tech-lead.md` review skill (R3, CO-20): `code-reviewer` agent in both lanes; mechanical quick-lane rule (`quick_lane_max_loc`, no always-human path, no new dependency, no schema, no auth or payments).
19. `.claude/commands/tech-lead.md` telemetry (A7, CO-21): hand-typed `model_used` and `tool_calls_approx` removed; the section records facts only and says why.
20. `.claude/commands/tech-lead.md` auto hand-off (D-17): hand off only after the user confirms.
21. `.claude/context/engineer-protocol.md` nominal budgets (L-30, CO-23, D-10): table removed; `maxTurns` unset (explained), `PARTIAL` is about not converging.
22. `.claude/context/engineer-protocol.md` iteration 2 (DEC-35, CO-22): resume by `agent_id`; one cap name, `max_retries_per_item`.
23. `.claude/commands/architect.md` critic rounds (L-34): same rule as orchestrate. Also `## Slices`, expand/backfill/contract, docs tool optional, plan mode suggested (CO-34).
24. `.claude/agents/database-engineer.md` verification comments (CO-18): derive-from-repo fallback like app-engineer; up, down, up.
25. `.claude/agents/database-engineer.md` backfill (CO-18): expand, separate backfill job, contract; data-loss class declared.
26. `.claude/agents/design-critic.md` (A19, CO-17, L-34): "cheap" dropped; added a11y, PII and retention, i18n, cost, expand/contract, API versioning, conformance to existing routes and error envelope, slices; blocker definition with four calibrated examples; round-2 scope.
27. `.claude/agents/code-reviewer.md` (R3, CO-17): skill reference removed; added test integrity and reward-hack signatures, AC traceability, running the verify commands itself, Doc Delta block, blocker definition.
28. `.claude/agents/security-reviewer.md` (P-14, A18, CO-24): scanner commands via Bash (`osv-scanner`, `npm audit`/`pip-audit`, `gitleaks`, `semgrep`, each only when installed, "not run" reported), `WebFetch` added for advisories, compliance claims dropped, remediation as suggested diffs in the report only, stable `SEC-<n>` IDs.
29. `.claude/agents/infra-engineer.md` secrets wording (CO-2): points to `docs/security/secrets-policy.md` (Phase 1 file), local dev values vs deployed secret manager.
30. `.claude/agents/infra-engineer.md` "stop and ask the user" (A21, L-15): `NEEDS_CLARIFICATION` with the listed triggers.
31. `.claude/context/local-dev.md` SQLite example (L-38, RC-21): dialect-parity rule (commented template example only).
32. `.claude/context/local-dev.md` verify-run harness (L-27): process-group launch and kill, port pre-check, deadline instead of sleep loops.
33. `.claude/context/local-dev.md` service stubs (CO-2): kept, secrets-policy pointer added.
34. `docs/faq.md` mobile (CO-31, P-9): iOS Simulator demos where the session has the tool. Also the `choosing-your-stack` link (the file moved).
35. `LICENSE` (CO-28): "Sujil Maharjan" (IMP-20), flagged for maintainer review.
36. `.github/CODEOWNERS` (CO-28, B4): no edit; kept for this repo. The shipped template with placeholders is Phase 1 (`.claude/templates/security/CODEOWNERS.template`).
37. `README.md` gitignore list (A27, P-9): `.claude/launch.json` only (IMP-20: the v2 event paths wait for Phase 2).
38. `README.md` context7 / "all eight agent files" (A26, P-1): optional docs MCP, no count. `superpowers` and `code-review` plugin lines removed (P-3). Also: Code Reviewer row, permissions paragraph, push contradiction (F-2), optional-copy command, file tree, links.
39. `README.md:178-241` architecture and requirements (CO-29): only the lines made false by this phase were edited; the rest is the release stage's docs workstream.
40. `examples/loop.config.md` line 45 (F-19, L-20): generated from `schema/v1.json`; it now says the hook blocks push/rebase/shared hard-reset for every actor and fails open without python3. Full key doc says nothing reads the flag.
41. `.claude/spec/loop-engineering.md` tool table (D-30, D-8): rewritten; plus preamble order, retry/discard, no in-session parallelism, dialect parity, interactive review, and a "Revision 3" section that says which older statements it supersedes (Revisions 1 and 2 kept as history).
42. Adapters `AGENTS.md`, `.cursor/rules/chain-of-custody.mdc`, `skills/chain-of-custody/SKILL.md` (P-19, CO-27): "advisory runtime" banner; parity table in `AGENTS.md` (about 3 KB, far below the 32 KiB limit).

### §19.7 and other spec items

- Slicing rules (L-35): planner, architect `## Slices`, `technical-design.md` template, `build-plan.md` template (`size`). Checks that need code (critical path, DAG width) are stated as arriving with loopctl.
- Blocker definition (L-34): design-critic and code-reviewer.
- Primer Delta (L-32): `checked:` line, malformed report rule, step moved before merge on the item branch; "empty delta N times" drift signal in the checkpoint.
- Migration guidance (CO-18): `migrations.md`, database-engineer, architect, design-critic, template.
- Process groups and ports (L-27), dialect parity (DEC-8): `local-dev-rules.md`, `local-dev.md`, planner, orchestrate, spec, examples.
- Permissions (L-41, CO-16), SessionStart pattern (P-8.8), `launch.json` (P-9), root `skills/` collision (P-18.7): `platform-notes.md`.
- Discard and retry (L-2), branch lifecycle with `dead/*`, `blocked/*` and the orphan sweep (L-3): `orchestrate.md`, spec §6, §9.
- Approvals recorded (L-10), waiver semantics (L-14), trailers `Loop-Item/Run/Attempt` (L-4, kept to the commit step): `orchestrate.md`.
- Doc Delta (F-30): `engineer-protocol.md` §12, code-reviewer blocks on an undocumented new env var or endpoint.
- Child-session prompt and PM posture (§11.6, CO-36): `CLAUDE.md`, `pm.md`, `tech-lead.md`.
- Model and effort routing (§11.4): `model-profiles.md`, `model_profile` key, orchestrate and tech-lead dispatch notes.
- Domain glossary (CO-30): template and pointers.

### Synthesis Phase 0 list (§16.1 row 0b), one line each

F-2: README publishing row and orchestrate Git Discipline say the human pushes and print the commands. F-19 / L-20: see row 40. D-5 / L-12: the "additive-schema DB+Backend parallel" option is deleted from tech-lead, orchestrate and the spec (the "phantom flag" is read as that option; see Decisions). tech-lead.md:225: row 20. D-30 / D-8: rows 12 and 41. A19: row 26. A26 / A27 / A30: rows 38, 37, 1. F-34 / L-7: row 4 and `engineer-protocol.md` §6 (one enum). NEEDS_CLARIFICATION everywhere: pm, orchestrate table, tech-lead A.6 and autonomous mode, infra-engineer, database/backend status notes, spec. R3: rows 18, 27. context7 optional: no agent has an MCP tool; prose says documentation tool else `WebFetch`. superpowers removed: rows 2, 5, 38. D-13 / D-14: resume by `agent_id`; engineers write their own report file plus JSON trailer. L-1: orchestrate preamble 0 (clean tree, dedicated clone). L-2, L-3: above. L-9: preamble reordered (state first, gates after) and renumbered. L-10, L-14: above. L-22: preamble 3 and verify step (falsifiable assertion, named test must appear as executed). L-23: demo fallback order and process groups.

### IMP-17 frontmatter (all 8 agents)

`maxTurns` unset with a comment in each file; `disallowedTools: Agent, Skill, Workflow, mcp__*`; explicit `tools:` without any `mcp__` entry; `effort: high` everywhere (the `quality` profile); `model: opus`; descriptions rewritten so they do not invite ad-hoc delegation (D-16) and no longer say "cheap", "once" or "uses the code-review Skill". `security-reviewer` gains `WebFetch`.

### IDs reviewed with no edit

- CO-28 CODEOWNERS (row 36): this repo's own file is kept by instruction.
- CO-27 beyond the adapters, CO-33, CO-35 for the `.claude/rules` loading itself: later phases (9, 10).
- F-3, F-28 (CI-fix limits), F-31 (`env.manifest`), L-4 beyond trailers, L-5 (revert detection), L-6 (reject cascade), L-11 (real lock), L-25 (down proof as a gate), L-32 enforcement, L-41 beyond the snippet, L-45: loopctl, gates or later phases (the report assigns them to Ph 2-11); orchestrate still states the V1 behaviour and says where loopctl takes over.
- P-21 "smaller items": no separate list exists in the repo; the README, critic and 'eight agents' items it covers are done.
- D-16 enforcement ("dispatch must carry a brief path"): waits on V7; descriptions are rewritten as the report says.

## Upgrade impact

A v1.2.0 install copying these files will notice:
- `docs/choosing-your-stack.md` moved to `.claude/context/choosing-your-stack.md`. `/planner` reads the new path (the read stays optional). A project that copied the old file into its `docs/` keeps a harmless extra copy.
- Agent frontmatter changed: no `mcp__plugin_context7_*` tools, `disallowedTools`, `effort`. An install that relied on context7 from the agents must add its tool names back to the `tools:` line and drop `mcp__*` from `disallowedTools:` (`platform-notes.md`). Install A/B forks of agent files will conflict on every changed line; `/migrate` (Phase 9) handles that.
- `max_review_rounds` and `tool_call_budget` are gone from the brief template; the cap is `max_retries_per_item`. Old briefs that still carry them are harmless.
- Engineers now write their own report file and end with a JSON trailer; reports without `## Doc Delta` or a `checked:` line count as malformed once the conductor enforces it.
- The Tech Lead no longer uses the `code-review:code-review` skill; it dispatches the `code-reviewer` agent in every lane.
- No in-session DB+Backend parallelism.
- Orchestrate preamble steps are renumbered (JSON-git reconciliation is now step 4, config readiness 6, tool preconditions 8). Anything quoting the old numbers is stale.
- Loop item branches are named `feat/<slug>-a<n>`; discarded ones become `dead/*`, parked ones `blocked/*`.
- `.claude/loop.config.md` gains four keys (below); old files still load, defaults fill in.

## New config keys

All in `.claude/scripts/kitlib/schema/0b-prose.json`; `gen-config --check` passes.
- `model_profile`: enum `quality | balanced | economy`, default `quality`, not H6-protected, not evidence-gated.
- `max_design_rounds`: int, default 2, H6-protected, not evidence-gated (section `retry`).
- `max_item_loc`: int, default 400, H6-protected, not evidence-gated. Planning rule now; `loopctl validate` checks it in Phase 4a. Phase 4a must not redeclare it.
- `quick_lane_max_loc`: int, default 30, H6-protected, not evidence-gated. Phase 2 must not redeclare it.

Documentation-only changes to existing v1 keys in `schema/v1.json` (keys, types and defaults unchanged): `enforce_engineer_git_hook` (what it does, fails open, nothing reads it), `db_local` (DEC-8 rule; default stays `sqlite`, see Decisions), `db_ephemeral` (template clone first). The example values for `db_local` and `db_ephemeral` in `examples/loop.config.md` now show Postgres. `always_human_paths` is deliberately not declared here; the quick-lane rule names the paths in words until the guard phase adds the key.

## Pending-human actions

- Review `LICENSE` ("Copyright (c) 2026 Sujil Maharjan"): the name was taken from the commit history (IMP-20); confirm it is the name to publish under.
- Confirm that the three reading choices in Decisions (L-12 read as the parallel option, L-9 read as the preamble reordering, the dedicated-clone rule) are what the report meant.
- hello-loop re-run showing "no regression" and the routing and refusal eval cases are the report's exit criterion for this phase. They need a live `claude`, so they are not run here (Phase 0a's `tools/baseline/run-baseline.sh` and Phase 2's evals). This phase is demonstrated offline by `tests/test_frontmatter.py` and `tests/test_prose_references.py`.

## Docs impact

Files this phase did not own, with the edit the release stage needs:
- `docs/getting-started.md`: lines 23-28 and 100 name `docs/choosing-your-stack.md`; the file is now `.claude/context/choosing-your-stack.md` and comes with the `.claude/` copy, so the optional copy command and the link should change. `tools/check-refs.allow` allowlists the old path until then.
- `GLOSSARY.md`: "Plugin / Skill" example names `code-review` and `context7` as things this kit uses (neither is required now); "Push" says "ask Claude: push my verified work" (the guard blocks that; Claude prints the command and the human runs it); "Code review" says the Tech Lead does it (it is the code-reviewer agent); add "Dialect parity", "Doc Delta", "Slice" if wanted.
- `CHANGELOG.md`: the 2.0.0 entry should list the Upgrade impact above.
- `README.md`: outside the §19.6 rows, the Telemetry bullets (`metrics.jsonl`) stay true until Phase 2; the gitignore list gets the v2 event paths in the release stage (IMP-20).
- `docs/assets/*.svg`: the team-workflow diagram's "tech lead reviews" wording and the loop diagram's "code-review skill" label, if any, should say the `code-reviewer` agent.

## Decisions made

1. **L-12 "phantom flag"** is not described in the repo (the audit files are not in it). Read as the "additive-schema DB+Backend parallel" option that orchestrate.md offered while declaring V1 strictly sequential: a flag that cannot exist. Deleted everywhere (D-5 groups it with L-12 in the Phase 0 list, which supports the reading).
2. **L-9 "preamble order"** read as: reconciliation with git must precede any status reset, and gates that can stop the run must follow the state steps. New order: git baseline, lock, sequential, plan, JSON-git reconciliation, resume (5.5 unblock, 5.6 run counter), config readiness, baseline, tool preconditions. Renumbered; every reference in the repo was updated (grep verified).
3. **Dedicated clone** (L-1, CO-15): stated as the way to run the loop, with an explicit "if the human runs in place anyway" path (clean tree is the minimum), because a hard refusal would break the v1 workflow for people who only have one checkout.
4. **Retry vs discard** (L-2): the report's "second discard on the same item is a hard stop" is implemented as item `blocked` + circuit breaker, continuing to the next eligible item, because v1's item-level "hard stop" has always meant a blocked item (the run-level pause is for systemic causes). `NEEDS_CLARIFICATION` that the plan can answer is answered into the brief and resumed instead of pausing the run; only an unanswerable one pauses.
5. **`db_local` default stays `sqlite`** in the registry: the v1 round-trip pins defaults, a registry default cannot depend on the project's production engine, and the doctor (Phase 1) is where a mismatch becomes an error. The doc says to set it explicitly and the Planner does.
6. **Hand-typed `metrics.jsonl` kept, slimmed**, rather than deleted: hook telemetry does not exist until Phase 2, and README documents the file. It now records only facts the Tech Lead knows (no `model_used`, no `tool_calls_approx`) and says why.
7. **`maxTurns` comment** is an HTML comment right after the frontmatter, not a YAML comment inside it, so no frontmatter parser can choke on it.
8. **Rules** (`.claude/rules`, R9, D-25): delivered as a template plus a primer-protocol section, not as shipped rule files, because rules are project invariants the agents derive.
9. **New keys** `max_item_loc` and `quick_lane_max_loc` are declared here (the prose needs them). `always_human_paths` is not, to avoid a duplicate when the guard phase adds it.
10. **Test change in `tests/test_config_template.py`** (Foundations file): the v1 text-level round-trip tests compare the frozen `tests/data/v1/` files with the regenerated v1 config. The report requires rewriting the docs of three v1 keys and the Notes prose, so those tests now skip exactly those blocks (`REWRITTEN_IN_0B`). Keys, defaults and order are still compared in full. `tests/data/v1/` is untouched.
11. **`tests/test_prose_references.py` `PENDING_RELEASE`** lists the hot files (GLOSSARY, getting-started, check-refs allowlist) that still carry stale references, and fails if one is fixed without removing its entry.
12. **Economy profile**: the report names it but defines nothing. Defined as sonnet engineers (frontends at `medium` effort), reviewers kept on opus, and marked experimental.
13. **Security-reviewer keeps `Bash`** (it needs it for scanners) and gains `WebFetch`; its read-only status rests on having no Edit or Write tool, which the frontmatter test asserts.
14. **Tech-lead dispatch order** keeps Frontends in the list but one after another, with a note that separate worktrees are the only parallel option (§11.5).
15. **Adapters**: the parity table says what the kit does not provide for Codex/Cursor rather than asserting what those tools can or cannot do themselves.

## Conductor: pointers to add

- `docs/getting-started.md`: lines 23-28 (copy command and sentence) and line 100 (link) -> point at `.claude/context/choosing-your-stack.md`; then delete the `docs/choosing-your-stack.md` entry from `tools/check-refs.allow` and the `docs/getting-started.md` entry from `PENDING_RELEASE` in `tests/test_prose_references.py`.
- `GLOSSARY.md`: "Plugin / Skill" (line 79), "Push" (line 69), "Code review" -> see Docs impact; then delete the `GLOSSARY.md` entry from `PENDING_RELEASE`.
- `CHANGELOG.md`: add the Upgrade impact list above to the 2.0.0 section.
- At merge, once Phase 1 and Phase 0a are in: remove the three entries tagged `# until Phase 1 merges` / `# until Phase 0a merges` from `tools/check-refs.allow` (`.claude/scripts/probe_run.py`, `docs/security/secrets-policy.md`, `tools/baseline/summarize.py`) and run `python3 tools/check-refs.py`.
- Possible merge conflicts: `tools/check-refs.allow` (appended block), `README.md` (Phase 1 and 0a may touch the same region), `.claude/scripts/kitlib/schema/` (no overlap expected; if Phase 1 declares `always_human_paths` or `max_item_loc` the registry refuses the duplicate).
- `.claude/settings.v2.json` (Phase 2): none needed from this phase.
- Phase 9: `.claude/commands/*.md` conversion should carry the `AskUserQuestion` interview checklists, and the renumbered preamble.
