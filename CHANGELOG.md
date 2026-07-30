# Changelog

All notable changes to Chain of Custody. Format follows [Keep a Changelog](https://keepachangelog.com/); versions are recorded in `.claude/KIT_VERSION` of every installed copy.

## [Unreleased]

### Renamed: Chain of Custody
- The project is now **Chain of Custody** (was "Claude Code AI Engineering Team"), tagline *"a role-based engineering team for Claude Code, where written specs are the only thing that crosses between agents."* The name points at the load-bearing mechanism rather than the org chart: every agent runs in a clean context, so the artifact is the only thing that crosses a boundary — each role takes possession, signs off, and hands on, and the trail is auditable.
- Updated the README title and opening, the `getting-started.md` intro, the GLOSSARY preamble, and the clone paths in the install manifest (`/tmp/ai-team` → `/tmp/chain-of-custody`). Generic references to "the kit" were left alone deliberately — they read better than repeating the proper noun.
- **Always write the name in full.** `custody` alone is crypto-custody and law-enforcement evidence software; `CoC` means Code of Conduct in every repo on GitHub. Note also that Block's [Buzz](https://www.techtimes.com/articles/321242/20260722/block-launches-buzz-open-source-workspace-where-ai-agents-sign-their-own-work.htm) (launched 2026-07-21) uses "signed chain of custody" for *cryptographic* agent-action provenance — a different meaning in an adjacent space, worth disambiguating if the two ever get compared.

### Examples — honesty pass
- **New `examples/README.md`** stating plainly that TaskFlow is fictional, that the folder is a hand-authored writing reference rather than the transcript of a real run, and that the kit ships **no runnable end-to-end demo**. Includes a here → real-install path map and a "don't copy these into your project" warning.
- **`features/csv-export/reports/backend.md`** gained a prominent disclaimer: its file list, test counts, `curl` check, and telemetry figures are invented. The structure is the lesson; the numbers are not evidence.
- One-line provenance notes added to `requirements.md`, `technical-design.md`, `api-contract.md`, and `briefs/backend.md` so readers who deep-link into a single artifact still learn it's fictional. The design note calls out specifically that the "existing" code it cites (`TaskRepository.listByBoard()`, `BoardPage.tsx`) is invented.
- `README.md` and `docs/getting-started.md` no longer describe `examples/` as a "complete filled-in example project" — it's now labelled a writing/calibration reference with no code behind it, and the file tree says so too.
- **Stale field name fixed**: the example brief still used `token_budget`; renamed to `tool_call_budget` to match `docs/features/_templates/brief.md` and `tech-lead.md`. (The 1.1.0 review believed this rename was fully purged; the examples folder was missed.)

### Known gaps (unchanged)
- No dogfooded demo: `/planner` → `/orchestrate` has never been run end to end with its output published, so the greenfield "front door" remains unvalidated. Tracked as the next piece of work.

## [1.1.0] — 2026-07-18

Full remediation of the July 2026 review ([docs/design/review-2026-07.md](docs/design/review-2026-07.md)).

### Loop runtime (state machine)
- **`needs_review` status deleted.** Checkpoints are now run-level `loop.jsonl` records; a checkpointed item stays `done` (it was already merged — the old status overwrote `done` and stranded items on session death).
- **Every item-level hard-stop now sets `blocked` + `blockReason`**, and a new preamble **unblock review** is the sanctioned recovery path (reset to `pending` / keep / route to `/planner`). Security stops gain a user-grantable `securityWaiver` field.
- **Checkpoints gained three exits**: approve / **reject** (revert merge + run the item's down-migration + reset to `pending` with `reworkNotes`) / stop. A failing demo route is `DEMO_FAILED` — a forced checkpoint.
- **Durable-DB migration step**: after each merge the conductor applies the item's migrations to the durable dev DB (new `migration_failed` hard-stop); down-migrations are recorded as `downMigrationRef` on the item.
- **Run counters re-derived from `loop.jsonl`** (`run_start` records + `breaker` flags) so the circuit breaker genuinely survives resumes; run lock replaced with a heartbeat lock (pid-based liveness could not work — each Bash call is a fresh shell).
- **Pass-path write ordering fixed** (single JSON update after merge) + reconciliation now recovers merged-but-unrecorded items from git log.
- **Preamble step 0**: `git init` on an empty folder; loop-mode briefs are written to disk and requirements passed inline (design-critic and templates updated to match).

### Hooks
- `guard-git.sh`: now **allows `git branch -D feat/*`** (the loop's own fail path needed it); blocks the ordinary-form bypasses (newline-chained commands, `FOO=1 git push`, `command git push`, `&&` without spaces, subshells); announces itself inactive if python3 is missing. 16-case test battery in the review doc.
- **New `guard-main-edit.sh`**: blocks Edit/Write to app source while on `main`/`master` (path-allowlisted for kit/plan/docs files) — the hook two docs referenced but the kit never shipped. Wired in `settings.json`; Tech Lead now runs a branch check before dispatch.

### Bootstrap safety floor (greenfield)
- Planner fills `loop.config.md` verify commands as a **plan output**; greenfield first item stamps `CLAUDE.md`'s stack table (kills the perpetual autonomy downgrade); Tech-Lead A.2 gains a scaffold ownership row (Backend owns the skeleton). A dedicated bootstrap mode remains deferred (§14).

### Docs
- README caught up with the loop layer: file tree, setup checklist (adds `local-dev.md` + `loop.config.md` — the old checklist guaranteed a first-run hard-stop), explicit install manifest, install-collision warning, existing-codebase primer-generation recipe, three-tier Prerequisites (context7 install-name trap documented), greenfield/existing fork in Quick Start.
- GLOSSARY gained the loop vocabulary, git-for-beginners terms, and environment terms; getting-started gained the checkpoint/push walkthrough and honest fill-in claims.
- Design doc Revision-2 staleness fixed (§2 Skill/Agent split, §8 security gate condition); `token_budget` renamed `tool_call_budget` in the brief/protocol layer; brief/report filenames standardized on domain shortnames; stray duplicate headings and wrong-direction references removed; security-reviewer description rewritten (no more auto-delegation bait).
- New **[docs/faq.md](docs/faq.md)**: testing strategy, CI, deployment cliff, monorepo/polyrepo, teams, abandoning items, test-count parsing, non-web rosters.

### Examples
- `examples/` is now actually complete: all 8 context primers (including the load-bearing `local-dev.md` with a concrete verify-run harness and Postgres-ephemeral variant), a filled `loop.config.md`, and a full example feature folder (`examples/features/csv-export/`). Fixed the database example's own convention violations (`onDelete`, `updated_at`).

### Packaging
- Added LICENSE (MIT), this CHANGELOG, `.claude/KIT_VERSION`, a README "Updating the Kit" section (never-edit vs user-owned file sets), `.gitignore` coverage for loop artifacts, and removed shipped `.DS_Store`/`worktrees` artifacts.

### Known accepted limitations (unchanged by design)
- `echo git push | sh` bypasses guard-git (adversarial form; allow-on-uncertainty is the documented policy).
- The hooks cannot distinguish engineer git writes from the conductor's — that boundary remains protocol convention.
- Read-only critic agents technically hold `Bash`; the read-only constraint is prompt-level.
- Mobile has no simulator auto-demo; demo targets fall back to text assertions.

## [1.0.0]

Initial public kit: PM/Architect/Tech-Lead modes, engineer subagents, design-critic + code-review gates, and the loop-engineering V1 layer (`/planner` + `/orchestrate`, locked design v3).
