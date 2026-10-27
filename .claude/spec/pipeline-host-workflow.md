# Pipeline host: `workflow` (DESIGN-ONLY)

**Status**: not built, on purpose. `pipeline_host: workflow` is registered in the config (`kitlib/schema/8-parallel.json`, evidence-gated) so the value has a name, but the doctor reports it as an **error** (`parallel.pipeline-host`) and `loopctl run` refuses to start with it (`parallel.pipeline-host` preflight row), whether or not the evidence gate has passed. Nothing sits behind the value. Report sections 11.1 item 3, 11.2, 12.1, 13 (P13, A21) and 16.1 row 8; `docs/design/v2-implementation-decisions.md` IMP-1 ("Workflow pipeline host: DESIGN-ONLY (V3, V23 unverified)") and IMP-7.

This file is the design an implementer would start from once the probes below are answered. It is short because most of what a Workflow host needs is already built for the other two hosts.

## What it would be

`pipeline_host` says **who fans reviews and critiques out**, and nothing else (report 11.2). It is a different axis from `runner` (where an item runs) and from `parallel_mode` (how many items run at once):

| `pipeline_host` | Reviews and critiques are | Status |
|---|---|---|
| `conductor` (default) | sequential `Agent` calls from the conductor, or sequential children of the driver | the only host that exists |
| `workflow` | one saved Workflow that runs the code-reviewer, the security-reviewer and the design-critic side by side, and returns one verdict each | design only |

The reviewers are read-only, so running them at the same time is safe (D-26): the merge rule is "any block blocks". The point of a Workflow host is wall-clock time and a cheaper conductor context, not new capability. The driver already runs a reviewer per child, one after another; a parallel review fan-out inside a parallel item would save minutes per item and nothing else.

## What would be true if it were built

1. **Saved, never improvised.** The Workflow lives in `.claude/workflows/<name>`, is reviewed like any kit file (CODEOWNERS, H6), and is invoked only by the human-run `/orchestrate`. A Workflow is never composed by a model at run time and never launched by a subagent.
2. **Never headless.** The driver's child profile and every engineer's `disallowedTools` keep `Workflow` denied (RC-17: in `-p` an allow rule can launch one because nothing prompts). A Workflow host therefore cannot be a driver child's tool. Under `runner: driver` the fan-out would have to be the driver itself starting three reviewer children at once, which needs no Workflow at all and is the better design; `pipeline_host: workflow` would then be meaningful only for `runner: in-session` / `sequential`.
3. **Event-driven done.** As everywhere (report 11.3): a review is finished when its report file is valid and loopctl records the verdict, never when a Workflow step returns. The Workflow writes `docs/features/<slug>/reviews/<role>-r<N>.md` and the conductor calls `loopctl gate` / `advance` as it does today.
4. **Cost.** Each reviewer's `total_cost_usd` still goes to the ledger. If a Workflow does not report per-agent cost (V8), the ledger records `unknown` and parks, exactly as for a child with a missing cost field (IMP-25).
5. **Custody.** Reviewers keep their own agent definitions, `disallowedTools: Agent, Skill, Workflow, mcp__*` and their critic settings profile. A Workflow step is only a way to start them.

## What must be answered first (probes, `tools/probes/README.md`)

| Probe | Question | Why it blocks |
|---|---|---|
| V3 | Does a Workflow give each of its agents the isolation and the hooks (guards, telemetry, nonce) a normal subagent gets? | If its agents run without the PreToolUse guards, a Workflow is a way around every guard. |
| V23 | Can a Workflow be launched from `-p`, or from a subagent, and what exactly does an allow rule on `Workflow` open? | The kit's whole answer to RC-17 is a deny; the host cannot exist on a platform where the deny is not the whole story. |
| V8 | Does a Workflow's agents' cost reach a field the ledger can read? | Without a cost the ledger parks. |
| V18 | Fork-mode behaviour of a Workflow step under `-p` and interactively | Dispatch must be event-driven; a step that can block the parent changes the design. |

## What changes when it is built

- `parallel.pipeline-host` stops being an error for `workflow`, and the preflight row accepts it only with a saved Workflow file at the base sha.
- The doctor checks the saved Workflow file: path under `.claude/workflows/`, listed in CODEOWNERS, no `Bash` in its tool list, the reviewer roles named exactly.
- `tests/loopctl/parallel/` gains the fan-out case against the fake `claude`. Until then `tests/loopctl/parallel/test_config.py` pins that the value is refused.

## Why not now

The evidence the report asks for is missing in both directions: the platform answers (V3, V23) are unverified, and there is no fixture metric a parallel review would move (one review takes minutes; an item takes tens of minutes). IMP-1 makes the call: DESIGN-ONLY, a config name, a doctor error.
