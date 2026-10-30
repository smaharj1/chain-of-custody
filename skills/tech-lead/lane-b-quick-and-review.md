# Tech Lead: Lane B (quick) and code review

Reference file of the `tech-lead` skill ([SKILL.md](SKILL.md)). Read it for a quick-lane task, and for the review rules both lanes share.

## Lane B — Quick Lane

For genuinely small, scoped tasks: bug fixes, single-page polish, prop renames, validation tweaks, micro-refactors that don't change a contract or schema.

### B.1 Triage

If the task touches a contract, a schema, or 3+ files across multiple apps — it's not quick. Push back: recommend `/pm`.

If it's truly small:

- Identify the engineer from the file path the user named (or ask one question).
- Confirm the canonical pattern in the relevant primer matches what's needed. If that primer is still a template, derive it first (primer-readiness above) — a quick task is exactly where a missing exemplar turns a one-line fix into an invented pattern.

### B.2 In-message brief

No feature folder. Just an in-message brief naming:

- The engineer
- Files to modify (absolute paths)
- The change in 2-3 sentences
- The pattern/exemplar to follow (one file path)
- Acceptance criteria (1-2 lines)
- Whether to skip review (default: don't skip)

### B.3 Dispatch + optional review

0. **Branch check**: if `git branch --show-current` reports `main`/`master`, branch first: ask the user (`git checkout -b quick/<short-name>`) when `git_autonomy` is `none`, otherwise do it yourself (`git switch -c quick/<short-name>`) — the on-main hook blocks engineer edits to app source.
1. Invoke the engineer subagent. Pass the in-message brief verbatim.
2. When it returns, dispatch the `code-reviewer` agent on the diff, **unless the change is trivial by the mechanical rule below**. When in doubt: review.
3. If review flags blockers, resume the engineer by `agent_id`. Cap at `max_retries_per_item` (default 2).

   **Trivial means all of these hold** (CO-20; not a judgement call):
   - at most `quick_lane_max_loc` changed lines (default 30; `git diff --shortstat`);
   - no path the project treats as always-human: `.claude/`, CI workflows, infrastructure code, lockfiles and secrets files;
   - no new dependency;
   - no schema or migration change;
   - nothing touching auth or payments.

   Anything else gets the review. If the user says "skip review" for a change that fails this rule, say so plainly and run it; they can still read the diff themselves.
4. **Primer delta.** Read the engineer's `## Primer Delta` and apply it, same as A.8 step 5. Usually `NONE` for a quick task — but not always, and this is the lane where drift accumulates unnoticed: "make this page nicer" is exactly how a styling convention gets established with no design doc anywhere to record it.
5. Record telemetry (section C).
6. Deliver as in A.8 step 7, in the quick form (`.claude/context/delivery.md`, "Lane B"): a WIP commit when `git_autonomy` is `branch+commit`, verify, then `pr_prepare.py init`, fill, `prepare`, and show the user the commands it prints. Never push, never open the PR.

### B.4 When to escalate quick to full

If during quick-lane work the engineer returns `NEEDS_CLARIFICATION` and the gap is anything contract-shaped or design-shaped: STOP. Tell the user: "This is bigger than a quick task — the gap is [X]. Recommend `/pm` to scope it as a feature."

---

## Lane Common: Code Review (one reviewer in every lane)

Both interactive lanes and the autonomous loop review with the **`code-reviewer` agent**. Why an agent, not an in-session skill:

- It runs in a **clean context**, so it has not seen your reasoning and cannot share your blind spots. A same-session review of work you briefed is weaker than it looks.
- Its verdict is **parseable** (`CODE_REVIEW_APPROVED | CHANGES_REQUESTED`), the same way every time, and it can run the verify commands itself.
- It is the same reviewer in every lane, so quality does not depend on which mode someone happened to use.

Engineer subagents lack the `Skill` and `Agent` tools by design: review is the conductor's job to dispatch, never the engineer's.

> **The autonomous loop adds a security pass.** Under `/orchestrate`, sensitive items also get the **`security-reviewer` Agent**, and the conductor gates `done` on both verdicts — see the Autonomous Mode section below.
