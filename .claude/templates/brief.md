# Brief: <Engineer Role> — <Feature Title>

**Feature slug**: `<slug>` (or `quick-<short-name>` for quick-lane tasks)
**Engineer**: <!-- your project's engineer subagent name (e.g., backend-engineer, database-engineer, app-engineer) -->
**Authored by**: Tech Lead
**Date**: <YYYY-MM-DD>
**Iteration**: 1
**Retry cap**: `max_retries_per_item` from `.claude/loop.config.md` (default 2). One cap name across the kit. There is no tool-call budget (`.claude/context/engineer-protocol.md` section 4).
**Report path**: `docs/features/<slug>/reports/<engineer>.md` (write your own report here; the conductor does not re-type it)

---

## Required Reading (in order)

1. `.claude/context/engineer-protocol.md` (the shared rulebook — read first)
2. `.claude/context/<your-domain>.md` (your primer — Tech Lead: replace this with the actual path, e.g. `backend.md`)
3. `.claude/context/shared-frontend.md` (frontend engineers only)
4. `docs/features/<slug>/requirements.md` *(loop mode: requirements arrive inline in the dispatch prompt instead — treat them as equivalent)*
5. `docs/features/<slug>/technical-design.md` — read **your domain's section** in full
6. `docs/features/<slug>/api-contract.md`

## Specific Files to Read for This Task

(Tech Lead populates these. The engineer should treat this list as the working set. If you need to read something not on this list to make a decision, surface it as a brief gap in your final report — that feedback is how briefs improve.)

- `path/to/file.ext:42-80` — context for the section you'll modify
- `path/to/file.ext` — full file (canonical exemplar to follow)
- `path/to/file.ext` — file you'll be modifying

## Canonical Exemplar

**Follow this file's pattern**: `path/to/exemplar.ext`

Any new code in this scope should match its style, layering, naming, and (where applicable) testing approach.

## Scope for This Engineer

(One paragraph. State exactly what THIS engineer is responsible for. The other engineers' work is NOT your scope.)

## Dependencies

- [ ] **Database Engineer** must complete migration X first -> see `docs/features/<slug>/reports/database.md`
- [ ] **Backend Engineer** must define endpoint Y first -> see `docs/features/<slug>/reports/backend.md`
- [ ] None (parallelizable)

## Acceptance Criteria

What "done" looks like for this engineer specifically.

- [ ] ...
- [ ] ...
- [ ] Lint clean. Existing tests still pass. Domain verification commands all PASS.
- [ ] The independent `code-reviewer` agent returns no blockers within the retry cap (same reviewer in every lane).
- [ ] The report's `## Primer Delta` and `## Doc Delta` are filled in (`NONE` with a `checked:` line is fine). A new endpoint or environment variable is documented.

## Out of Scope

(Explicit fences. Don't touch these areas even if they look related.)

- ...
- ...

## Clarifications

(Empty at dispatch. If you return `NEEDS_CLARIFICATION`, the Tech Lead or Orchestrator writes the answer here before resuming you, so the record exists. Read this section again when you are resumed.)

- ...

## Notes from Tech Lead / Architect

(Anything the engineer needs to know that isn't in the design doc — recent decisions, things to avoid, parallelism notes.)

- ...

---

## Iteration 2 Addendum (only filled in for re-dispatch after review)

> When Tech Lead **resumes you** (by `agent_id`) after a `CHANGES_REQUESTED` review, this section or the resume message will list:
>
> - **Blockers to address** (from the `code-reviewer` agent)
> - **Suggestions to address** (unless you have a stated reason to disagree)
> - **Pointer to your iteration-1 report** at `docs/features/<slug>/reports/<engineer>.md` (`<engineer>` = the domain shortname: `database`, `backend`, `app`, `admin-app`, `infra`)
