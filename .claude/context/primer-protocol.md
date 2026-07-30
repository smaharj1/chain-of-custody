# Primer Protocol — how primers get written and kept true

Generic kit logic, like `engineer-protocol.md`. **Not a file you fill in.** Every mode that reads a primer follows this.

## The rule

**Primers are agent-written. The user never fills them in.**

Assume the user cannot answer a technical question — not "won't," *cannot*. They may not know what an ORM is, which framework the app uses, or what a canonical exemplar would be. A primer question aimed at them is a dead end, and a primer left as a template silently degrades every downstream role: engineers match no exemplar, the Architect designs against nothing, and `/orchestrate` drops to `per-feature` autonomy for the life of the project.

So: whoever needs a primer that is still a template **derives it first**, then proceeds with the actual work.

| Role | When it writes primers |
|---|---|
| **Planner** (`/planner`) | At plan time — from the pre-baked stack decisions (greenfield) or from the code (existing project). Owns `local-dev.md` + `loop.config.md`, which the loop hard-stops without. |
| **Orchestrator** (`/orchestrate`) | Preamble, before the autonomy verdict — derives the primers this run needs instead of asking. |
| **Architect** (`/architect`) | Phase A.0, for the domains the design touches. Then Phase C for patterns the design introduces. |
| **Tech Lead** (`/tech-lead`) | Before dispatching an engineer whose domain primer is a template. Then A.8.5 for patterns that shipped. |
| **PM** (`/pm`) | Never. Reads primers; notes gaps for the Architect; does not author. |

## Derive from evidence, not from memory

The information is in the repo. Read it.

- **Stack and versions** — package manifests, lockfiles, config files, CI workflows.
- **Architecture and layering** — the actual directory tree and import direction, not the pattern you'd expect from the framework.
- **Conventions** — file naming, error handling, logging, test layout: read several real files and describe what they *do*, not what's idiomatic elsewhere.
- **Canonical exemplar** — name a real file per layer, one that exists and is representative. This is the highest-value line in any primer; engineers copy it.
- **Catalogs** (endpoints, tables) — **point at the source of truth** (`see src/routes/*.ts`) rather than enumerating. A pointer that stays true beats a list that goes stale.
- **Library specifics** — context7 MCP, not training memory.

Two hard limits:

- **Never assert what the code doesn't show.** An unsupported claim in a primer is worse than an absent one — engineers treat primers as fact.
- **Execution-critical commands must be proven, not guessed.** The run/test/reset commands in `local-dev.md` (and their `loop.config.md` counterparts) get **run once** to confirm they work before you write them down. A plausible-looking wrong command surfaces later as a mystery hard-stop mid-run.

## Greenfield: no code to read yet

On an empty folder the evidence doesn't exist. Then:

- Write what the **decided stack** determines (the Planner made those calls with the user in plain language — see `/planner` Phase 2).
- Leave the evidence-dependent sections — exemplars, catalogs, layering specifics — marked `TODO(primer): <what's missing>`, and let the baseline item's ship fill them (Orchestrator step 6 / Tech-Lead A.8.5). A primer that describes a repo layout nobody has built yet is fiction.

## What you may ask the user

Only questions a non-technical person can actually answer, only in plain language, and only when the answer **changes what you write**. Product and business ambiguity qualifies:

- "When two people edit the same thing at once, should the second person get a warning, or should the last save win?"
- "Can a regular user see other people's records, or only their own?"

Technical calls are yours. Make them from the evidence and the plan's pre-baked decisions, and record the call plus what it rests on so it's auditable — `Structured logging via pino (matches src/lib/logger.ts)`, not `Structured logging`. Batch whatever you must ask into one short round; don't interrogate.

## Sentinels are the readiness signal

`/orchestrate` greps primers and `CLAUDE.md` for template sentinels and downgrades autonomy when it finds them. So:

- **Delete the template comment block** in any section you fill. A filled section under a surviving placeholder comment still reads as unfilled.
- **`TODO(primer): <question>`** is the honest marker for something genuinely unknowable right now. It keeps a human in the loop on purpose — that's the intended behavior, not a failure.
- Never leave a half-filled section unmarked. Silence reads as "documented."

(The two protocol files in this directory are kit logic and quote these markers by design — the readiness grep skips them.)

## How primers are accessed

There is no auto-loader. A primer reaches an agent because that agent's own instructions name the path and it runs `Read` — which is why every role's reading list names a **concrete file**, not "the relevant context docs."

| Reader | Route |
|---|---|
| Engineer subagents | Domain agent file, Required Reading item 1 (names the exact primer), reinforced by `engineer-protocol.md` §1 and the brief's Required Reading. Frontend engineers read `shared-frontend.md` too. |
| Architect | Required Reading at mode switch — `architect.md` **every session**, domain primers for what the design touches |
| Tech Lead | Enough of each primer to route work; the engineer reads it in depth |
| Design critic / code reviewer | Required Reading — the primer is the standard a design or diff is measured against, which is what makes it worth maintaining |
| Orchestrator | Preamble, before the autonomy verdict |

Two things follow. **A primer nobody reads is dead weight** — if you add a primer, add it to a reading list. And **the exemplar pointer is the most-followed line in the kit**: engineers implement by matching the file it names. When you read a primer and its named exemplar path no longer exists, fix the pointer then and there — a dangling exemplar sends the next engineer to invent a pattern from scratch.

## Keeping them true

A primer is only worth reading if it matches reality, and reality moves every time an item ships. Drift is silent: nothing errors, the next Architect just designs from a description of a codebase that no longer exists.

**Deltas travel as reports, then get applied by one role.** The engineer who ships the code is the first to know a primer is stale — they're the one whose work had to disagree with it — but they don't edit it (one slice, one clean context, shared file). They report a `## Primer Delta` (engineer-protocol §11); the code reviewer adds `## Primer Staleness` if it spots more; and the role holding the whole picture **applies it in the same session**: Tech Lead at wrap-up (A.8 step 5, and B.3 in the quick lane), the Orchestrator at its per-item primer-delta step, the Architect for patterns its design introduces (Phase C). Same chain of custody as everything else in the kit — the artifact crosses the boundary, not the edit.

Also close out `TODO(primer)` markers as the code that resolves them lands. That's what lifts a loop's autonomy downgrade, and nothing re-checks it on its own.

Update in place. Primers are living documents, not append-only logs — replace the stale claim, don't stack a "Revision 3" note on top of it. When a primer's own accuracy is what's in question, the code wins: re-derive that section from the evidence.
