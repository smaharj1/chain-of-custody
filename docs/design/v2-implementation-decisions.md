# v2 Implementation Decisions

**Status:** binding for the v2 build. These rulings turn `review-2026-10.md` (the "report") into an executable build. They were made by a delegated decision-maker because the maintainer asked for every judgement call to be settled without questions and will review everything at the end.

**Base:** the report at `f908238` (branch `docs/review-2026-10`, PR #7, unmerged). Kit baseline for measurement: `cb6218f` (v1.2.0 plus the unreleased docs-boundary move).

**Authority.** Where this file and the report disagree, this file wins for the build and says why. Where this file is silent, follow the report. Every deviation is listed in IMP-3, IMP-10..IMP-26 and flagged "Deviation".

**Hard constraints (restated, non-negotiable, every implementer reads them):**

- H4: infrastructure apply, deploy and destroy are never automated. A human approves every apply. Nothing built here may apply, deploy or destroy anything.
- Agents never push, never change GitHub or account settings, never create tags on a remote. `.claude/hooks/guard-git.sh` blocks `git push`. All work is committed locally; the human pushes at the end. GitHub-side configuration is delivered as files plus scripts and instructions the human runs.
- No real-project names anywhere in the repo, in commits or in test fixtures. The two real installs are "Install A" and "Install B" only. Implementers must not read or copy from the sibling project directories that appear among the session's additional working directories.
- Agents never launch `claude` (CS-2), not even `claude --version`. All tests use a fake binary (IMP-14).
- Agents never run a command with real cloud, GitHub or SSH credentials, and never run `doctor --live` on the host.

---

## IMP-1. Scope policy: build everything deterministic, gate the unproven in code, document the rest

**Decision.** Nothing in the report is skipped silently. Each item gets exactly one of four forms:

| Form | Meaning |
|---|---|
| **FULL** | Built, tested, on by default (or required by the report's default). |
| **OPT-IN** | Built and tested; off by default; labelled `experimental` in docs and in `loop.config` comments; the doctor refuses to let it be switched on until the evidence gate (below) passes, where the report puts it behind that gate. |
| **TEMPLATE** | Files the human installs or runs (workflows, rulesets, scripts, policies). Linted and unit-tested offline; never exercised against live GitHub, cloud or Claude. |
| **DESIGN-ONLY** | A short spec in `docs/design/deferred.md`, no code. Used only where the thing cannot be built or tested without a live platform feature that is still a probe, or where it is inherently app-specific. |

**The evidence gate becomes code.** The report's "evidence gate" (3 clean fixture runs plus 1 real feature) cannot pass in this build, because no agent may run `claude`. So it is represented as a human-committed file, `docs/baseline/evidence.json` (schema in `docs/baseline/evidence.template.json`). `kitlib.evidence.passed()` reads it. While it is absent or incomplete, the doctor's effective level is capped at L3, and these OPT-IN features refuse to enable: unattended L4, L5, `parallel_mode: worktree`, G4 mutation, the G5 non-glob lint, remote lock, G10, `pipeline_host: workflow`, the G8 verifier. Templates and docs are not gated; only runtime activation is.

**Kill criteria are written, and evaluated as "pending".** `docs/baseline/kill-criteria.md` holds the report's §16.2 text verbatim in spirit, plus one added consequence: if the baseline (N >= 3) shows 3 or fewer control-flow rescues, loopctl "Tier 2" (the write subcommands, `run` and the gates) is deprecated in the next minor release and Tier 1 (`validate`, `status`, `render`, `doctor`, `migrate-plan`, `lock`) stays. Because the V1 prose loop stays the default `runner: sequential`, unproven Tier 2 code never runs unless a human chooses it.

**Rationale.** The user asked for "all the changes", and nearly all of the report is testable offline with a fake `claude`, a local bare git remote and temp HOMEs. Skipping gated phases would contradict the instruction. Shipping them as defaults would contradict the report's evidence discipline and CS-34. A code-enforced gate plus off-by-default satisfies both, and it makes the maintainer's later decision cheap: delete if the kill criteria fire, enable if the gate passes.

**Consequences.** Every OPT-IN item names its gate in the schema registry (IMP-11) so the doctor can enforce it. `docs/design/deferred.md` is created in the release stage and lists every DESIGN-ONLY item with the reason and the probe or evidence that would unblock it.

Per-item rulings the report left open:

- **G4 mutation:** OPT-IN. Tool-agnostic wrapper only: `mutation_cmd` per stack in config, a normalised score JSON contract, `mutation_min` check, CI template. No Stryker, mutmut or cargo-mutants is bundled or installed.
- **G5:** the glob/integrity check is FULL (Phase 4 MVP). The Python AST lint (stdlib `ast`) is OPT-IN. JS/TS is a regex heuristic marked report-only and low-confidence. Other languages are DESIGN-ONLY.
- **G8:** the AC x evidence matrix view in `loopctl status` is FULL. The separate verifier agent is DESIGN-ONLY (an unrunnable prompt would be untested).
- **G10:** OPT-IN framework only (named commands in config, a normalised result contract, a CI template). No axe, Lighthouse or contract-test tooling is bundled. Contract tests against `api-contract.md` are DESIGN-ONLY.
- **Remote lock:** OPT-IN, built and tested against a local bare remote only. Advisory, as the report says.
- **Parallel worktrees:** OPT-IN, off by default, evidence-gated. The deterministic primitives and the scheduler are tested with the fake `claude`.
- **L5 auto-merge:** the eligibility classifier, the doctor prerequisites and the guard rule are built. Enabling it is DESIGN-ONLY by policy: `merge_policy` is hard-defaulted to `human`, there is no code path in this build that runs `gh pr merge`, and the doctor refuses L5 until the evidence gate and the promotion bar pass.
- **L5 and deploy-on-main (B5):** the classifier refuses automatically when `deploy_triggers` shows an ungated deploy on `main`.
- **Workflow pipeline host:** DESIGN-ONLY (V3, V23 unverified). The config key exists; setting it to `workflow` is a doctor error.
- **Statusline, desktop `start_session`, opusplan, Advisor, `/goal`, Artifact dashboard:** DESIGN-ONLY documentation (DEC-26, DEC-30, CO-34).

---

## IMP-2. Phase 0a: build the whole measurement kit, record the runs as pending

**Decision.** Build now: the fixture, the run harness, the observation hook, the summariser, the results schema and template, the kill-criteria file, a run protocol, and a `PENDING` ledger. Do not run V1. The three runs and the "1 run plus 20 replayed dispatches" alternative are recorded as pending-human.

**Deliverables.**

- `examples/hello-loop/`: a zero-dependency Python 3 stdlib service (`http.server`, `sqlite3`, `unittest`). Three items: schema, endpoint, UI (server-rendered HTML). Production dialect is declared SQLite, so DEC-8 is satisfied honestly. Pinned toolchain: `.python-version`, `pyproject.toml` with `requires-python`. A V1-shape `build-plan.json` with falsifiable ACs. A `kit-overlay/` of pre-filled primers, `local-dev.md` and `loop.config.md`, so the baseline measures the loop, not primer derivation. `reference-solution/` holds a known-good implementation used only to prove the plan is satisfiable; the harness never copies it into a run.
- `docs/baseline/README.md`: the run protocol. It lists the baseline deltas from §15 item 1 (sandbox, no cloud credentials, observation hook, demo capability), the rescue taxonomy (control-flow rescue vs other), and what each run records.
- `docs/baseline/kill-criteria.md`, `docs/baseline/results/TEMPLATE.json` plus its schema, `docs/baseline/RUNS.md` (three `PENDING` rows), `docs/baseline/evidence.template.json`.
- `tools/baseline/run-baseline.sh`: creates a clean clone at tag `v1.2.0`, overlays the fixture without `reference-solution/`, installs the observation hook, prints the exact `claude` command for the human, and **refuses to run if the `CLAUDECODE` environment variable is set** (i.e. inside a Claude Code session) or if `claude` is absent. It is human-run only.
- `tools/baseline/observe.py`: an always-exit-0 hook that appends hook stdin JSON to a log file. It changes nothing. The probe harness (IMP-7) reuses it.
- `tools/baseline/summarize.py`: aggregates `results/*.json` into ranges and evaluates the §16.2 kill criteria. With N < 3 it prints `PENDING (n=0/3)` and exits 0.
- Tag `v1.2.0` is created locally on `cb6218f` as an annotated tag (the human pushes it). The harness depends on it.

**Rationale.** All the value that can be produced without live runs is produced, and the run itself becomes one command for the human. Python stdlib keeps the fixture runnable in CI and offline, which satisfies DEC-27 (fixture in CI) with a pinned toolchain.

**Consequences.** `tests/test_hello_loop.py` applies `reference-solution/` to a temp copy and runs the fixture's verify command, so a broken fixture fails CI. The pending runs are listed in `docs/v2-pending-human.md` (IMP-15). Baseline conditions note that cb6218f includes the unreleased docs-boundary move, which is the same tree the report reviewed.

---

## IMP-3. Phase 9 (plugin packaging): do it, late, with source kept where it is

**Decision.** Do the full Phase 9 deliverable set, but **invert the source of truth** relative to the report. `.claude/` (runtime content) and `.claude/scripts/` (tools) remain the single hand-edited source. `plugin/`, `dist/copy-install/` and the Codex/Cursor adapters are **generated** by `tools/build-dist.py`, committed, and checked for drift by a test. Phase 9 runs after Phases 6, 7, 8, 10 and 11 and before the release stage.

Details:

- **Commands become skills in `.claude/skills/<mode>/SKILL.md`** (not in `plugin/` only), so copy-install users also get them. Long bodies split into progressive-disclosure reference files next to the skill. Frontmatter per §12.1 and DEC-17: `pm` model-invocable; `architect` and `tech-lead` `disable-model-invocation` with confirm-gated chaining; `planner` and `orchestrate` human-only; `argument-hint`, `effort`. No skill is named `loop` or `code-review`. The old `.claude/commands/*.md` files are removed in that phase; `/migrate` deletes them in upgraded installs.
- **Kit scripts live in `.claude/scripts/`**, so the single-step `cp -r .claude .` install still works. Dev-only tooling is in `tools/`. Tests are in `tests/`. None of `tools/` or `tests/` is shipped to users.
- `plugin/` gets: `.claude-plugin/plugin.json` (with `userConfig`), `agents/`, `skills/`, `hooks/hooks.json` (generated from the same `guards/hooks.manifest.json` as the copy-install settings), `scripts/`, `evals/`. The marketplace file is `.claude-plugin/marketplace.json` at the repo root, pointing at `./plugin`. This keeps the root `skills/chain-of-custody` Codex adapter from loading as a Claude skill (P-18.7).
- `/init` writes the project settings the plugin cannot ship (P19). `/migrate` plus `.claude/scripts/migrate.py` upgrade a copy-install using a shipped `KIT_MANIFEST` (sha256 per file) and `git merge-file` three-way merges against the v1.2.0 manifest, which is generated now from the tag. Local forks (Install A's roster and hook edits) surface as conflicts the human resolves; nothing is overwritten silently.
- `tools/validate-plugin.py` is the stdlib structural equivalent of `validate --strict` (required keys, path existence, hook script existence, no stray `Bash(...)` in `disallowedTools`, no skill named `loop` or `code-review`). Running the real `claude plugin validate` is a pending-human step with a ready-made script.

**Rationale.** Moving the source tree first would force every other phase, parallel build and doc to chase paths. Generating the plugin from the existing tree gives the same artifacts, leaves both real installs' upgrade path simple, and makes the restructure one mechanical step with a drift test. Doing it last means the long prose files (`orchestrate.md`, `tech-lead.md`) are edited serially by their phases first, and converted once.

**Deviation from the report.** The report makes `plugin/` the source and copy-install generated. We generate `plugin/` instead. The user-visible result is identical; the build is safer.

**Consequences.** New modes added by Phases 10 and 11 (`/retro`, `/bootstrap`) are written as `.claude/commands/*.md` and converted by Phase 9. `tools/check-refs.py` (Foundations) turns the final path sweep into a mechanical check.

---

## IMP-4. Language, runtime and test framework

**Decision.**

- All kit scripts (`doctor`, `verify`, `loopctl`, guards, migrate, probes, tools) are **Python 3 stdlib only**. Shell is allowed only for thin human-run wrappers (`run-baseline.sh`, `apply-boundary.sh`).
- **Minimum Python is 3.9.** Rationale: macOS's system `python3` under the Xcode command line tools is 3.9, and the guards run as fail-closed hooks, so a floor of 3.10 would block every tool call on a fresh Mac. Code must avoid `match`, parenthesised multi-item `with`, `X | Y` in runtime annotations (use `from __future__ import annotations`), `zip(strict=)`, `tomllib`, `str | None` outside annotations, and any API newer than 3.9. Build and local tests run on the Mac's 3.14.
- Enforcement: `tests/test_py39_syntax.py` parses every `.py` under `.claude/scripts/`, `tools/` and `tests/` with `ast.parse(..., feature_version=(3, 9))` and greps for a small list of known post-3.9 APIs. The kit-tests CI workflow runs a 3.9 and a 3.13 job.
- **Tests use stdlib `unittest`** (pytest is not installed and must not become a dependency). Table-driven corpora are JSON files driven through `subTest`. Run: `tools/test.sh` = `python3 -m unittest discover -s tests -t .`. The full suite must run offline in under 60 seconds with no sleeps over 2 seconds.
- macOS and Linux only. Windows is documented as unsupported (process groups, `fcntl`, shell wrappers).
- No third-party YAML. Config uses a restricted YAML subset parsed by `kitlib/miniyaml.py` (IMP-11).
- Node 22 and pnpm are not used by any kit tool. Docker is never required for tests; docker-dependent features degrade to "unavailable" with a doctor message.

**Rationale.** Zero install friction on the target machines, deterministic tests, and no supply-chain surface in a kit whose whole point is the supply chain.

**Consequences.** Every hook entry point is written to fail closed on any exception (exit 2 with a reason), independent of the platform's `onFailure` handling (IMP-16).

---

## IMP-5. Branch and commit strategy: one integration branch, per-phase topic branches merged `--no-ff`

**Decision.**

- Create `v2/integration` from the tip of `docs/review-2026-10` (the report must be present, since the work implements it). The first commit on it is this decisions file. PR #7 stays a pure review.
- Each phase or sub-phase works on its own topic branch off the **current** `v2/integration` tip: `v2/00-foundations`, `v2/0a-baseline`, `v2/1-boundary`, `v2/0b-prose`, `v2/2-guards`, `v2/3-pr-delivery`, `v2/6-infra-ci`, `v2/4a-loopctl-core`, `v2/4b-gates`, `v2/4c-driver`, `v2/7-unattended`, `v2/8-parallel`, `v2/10-learning`, `v2/11a-bootstrap-ops`, `v2/11b-loopctl-extras`, `v2/9-packaging`, `v2/12-release`, `v2/13-review-fixes`.
- Only the conductor merges, with `git merge --no-ff v2/<topic> -m "Merge Phase <n>: <name>"` into `v2/integration`, and only when the topic's definition of done holds (IMP-24). Each merge is therefore one revertable unit and the history shows per-phase boundaries. The maintainer may take the whole branch or cherry-pick merges.
- Each phase makes **one or more commits**, logically split (library, tests, prose, docs). Every commit message ends with the attribution line the harness provides.
- No rebase, no force, no `reset --hard`, no push, no remote tag pushes, no branch deletion (the repo's own guard blocks several of these). To pick up a newer integration tip, merge `v2/integration` into the topic branch.
- The main checkout stays on `v2/integration` and is never switched. It must never be on `main`.
- Topic work for parallel phases happens in worktrees the **conductor** creates: `git worktree add .claude/worktrees/<topic> -b v2/<topic> v2/integration` (the path is already gitignored). The conductor creates them rather than using `isolation: worktree` because base resolution is unverified (V6). All file walkers (`check-refs.py`, `build-dist.py`, tests) must exclude `.claude/worktrees/`.
- Subagents commit only on their own topic branch. They never merge, check out another branch in the main checkout, or touch another worktree.

**Rationale.** Pure stacked branches would force serial rebases that the guard forbids and would serialise work that is disjoint. A single flat branch loses per-phase review. The hybrid keeps both. Basing on the review branch avoids a second copy of the report.

**Consequences for the human.** Merge PR #7 first, then open the v2 PR from `v2/integration`; its diff then excludes the report. If #7 is squash-merged, merge `main` into `v2/integration` once before opening the PR. The tag `v1.2.0` and the branches are pushed by the human.

---

## IMP-6. Parallelism plan

**Decision.** At most **three** build subagents run at once, each in its own worktree, each on a disjoint file set. Everything else is serial.

Serial spine: Stage 1 (Foundations) -> Wave A -> Wave B -> Phase 4 -> Wave D -> Phase 9 -> Release -> Review.

Safe parallel groups (file sets are disjoint by construction):

| Wave | Runs in parallel | Why it is disjoint |
|---|---|---|
| A | Phase 0a, Phase 1, Phase 0b | 0a: `examples/hello-loop`, `docs/baseline`, `tools/baseline`. 1: `.claude/scripts/doctor*`, `ci_lint*`, `session_start.py`, `.claude/templates/security`, `.claude/templates/github`, `docs/security`, `.gitignore`. 0b: prose in `.claude/commands`, `agents`, `context`, `spec`, `CLAUDE.md`, `README.md`, `docs/faq.md`, `examples/loop.config.md`, `LICENSE`, `AGENTS.md`. |
| B | Phase 2, Phase 3, Phase 6 | 2: `.claude/scripts/guards`, `settings.v2.json`, `tests/guards`, `evals`. 3: `verify.py`, `pr_prepare.py`, `pr-body.md`, `verify.yml`, `claude-review.yml`, tech-lead/architect delivery prose. 6: `.claude/templates/infra`, `.claude/scripts/infra`, `docs/security/infra-ci.md`. |
| 4 | 4a first, then 4b and 4c together | 4a defines the loopctl core, the gate plug-in interface and the state layout. 4b owns `loopctl/gates/*`. 4c owns `driver.py`, `launch.py`, the fake `claude` and the fault-injection tests. |
| D | (a) chain 7 -> 8 -> 10, in parallel with (b) Phase 11a and Phase 6 leftovers | 7, 8 and 10 all touch loopctl, so they are serial with each other. 11a touches no loopctl file (bootstrap mode, intake and dependency workflows, labels). |
| D2 | 11b after the chain | Remote lock, G10, manifest and the L5 classifier touch loopctl and the doctor. |

Must be serial: anything editing `.claude/commands/orchestrate.md`, `tech-lead.md`, `planner.md`, `architect.md` or `.claude/spec/*` outside its wave owner; `.claude/settings.json`; `loopctl/cli.py`.

**Hot-file rule.** Parallel agents never edit a file owned by another group. If a phase needs a pointer in a hot prose file, it delivers its content as a new file (progressive disclosure: `.claude/context/<topic>.md`) and the conductor adds the one-line pointer at merge time. This also satisfies CO-35.

**Docs rule (deviation from §16.1 "docs workstream, every phase, same PR").** Phases do not edit `README.md`, `CHANGELOG.md`, `GLOSSARY.md`, `docs/getting-started.md`, `docs/faq.md`, `KIT_VERSION` or the SVGs, except Phase 0b for the exact lines in §19.6. Each phase instead adds `changelog.d/<topic>.md` (what changed, upgrade impact, new config keys) and a "docs impact" list. The release stage consolidates. Phase-owned new docs (`docs/security/*`, `docs/baseline/*`) are written by the phase.

**Rationale.** README, CHANGELOG and config templates are the files every phase wants to touch. Letting them diverge in parallel guarantees conflicts; consolidating once is cheaper and produces a coherent document.

---

## IMP-7. Probes: assume the conservative answer, ship a harness

**Decision.** No probe is run in this build. The build assumes the conservative answer for each, documents the assumption in `docs/design/probe-assumptions.md`, and ships a harness so the human can confirm quickly.

| Probe | Build assumes (and does) |
|---|---|
| V1 agent_id/type at depth 2-3 | Agent-blind: a call with `agent_id` but unknown `agent_type` is an engineer. |
| V3 Workflow per-agent isolation | `pipeline_host: workflow` not implemented. |
| V4 `context: fork` skills | No kit skill relies on it. |
| V5 driver child inheritance | Nothing is inherited. The driver passes `--settings`, `--agents`, `--allowedTools` and `--permission-mode` explicitly. |
| V6 `isolation: worktree` | Not used. The conductor creates worktrees. |
| V7 SubagentStart sees the prompt | No brief-path enforcement at SubagentStart. Enforced by report parse and prose. |
| V8 cost fields on SubagentStop | Cost comes from the `-p` JSON per child only. |
| V9 `onFailure: block` forms | Guards fail closed themselves; the canary detects non-firing. |
| V12 `omitClaudeMd` | Critics keep CLAUDE.md. |
| V13 plugin hooks and `${CLAUDE_PLUGIN_ROOT}` | The plugin ships hooks.json; the liveness canary detects non-firing; `/init` can also write project-level hooks. |
| V14, V16, V17, V25, V26 | Documentation only; B1/B4 are the boundary. |
| V15 eval `--model` pin | Pin the CLI instead. |
| V18 fork-mode semantics | Dispatch is event-driven (report file plus state transition); the driver sets fork env explicitly; never trusts a blocking return. |
| V19 sandbox `failIfUnavailable` | The doctor reads settings and requires a human attestation `sandbox_verified: <date>`; unknown means L1. |
| V20 SessionStart `compact` inject | Implemented, with a fallback: orchestrate prose re-reads `loopctl status --json` at each stage. |
| V21 managed-settings override by same OS user | Assume overridable: recommend a separate OS user or credential-free container for L3+. |
| V22 `--agents` plus `--agent` in `-p` | The driver launch line is built and unit-tested only against the fake `claude`; first real run is the probe. |
| V23 Workflow from `-p` or a subagent | `Workflow` is denied headlessly and to engineers; the doctor fails on a stray allow. |
| V24 per-subagent sandbox | Assume none. Held-out tests are driver-only or CI-only. |
| V27 GitHub plan features on private repos | Unknown means unavailable: the doctor caps the level unless the human attests the feature. |

**Harness.** `tools/probes/`: `README.md` (one section per V-item: purpose, exact steps, expected observation, where to record), `make-scratch.sh` (scratch repo with `observe.py` wired for hook-input capture), settings snippets for V1, V5, V7, V9, V18, V19, V20, V22, V23, V24, and `docs/design/probe-results.template.md`. Probes that need a live `claude` are human-run only; the scripts refuse to run inside a Claude Code session.

**Consequences.** `claude_code_tested_max` is recorded as `none` until the human's canary passes (IMP-21), which independently keeps L3+ off.

---

## IMP-8. Version and release

**Decision.** The result is **`2.0.0-rc.1`**. Final `2.0.0` is cut by the maintainer after: the baseline runs, the probes that matter, applying the GitHub-side boundary, a passing canary, and migrating both real installs.

- `.claude/KIT_VERSION` becomes a small key file: first line `2.0.0-rc.1 (<release date>)`, then `claude_code_min: 2.1.302` and `claude_code_tested_max: none`. `kitlib.semver` parses prerelease tags.
- `CHANGELOG.md`: one `## [2.0.0-rc.1]` section assembled in the release stage from `changelog.d/*.md`, with the existing `[Unreleased]` docs-boundary entry folded in under "Changed". It has an explicit "Breaking changes" list: fail-closed hooks and the python3 floor, the Claude Code version floor, `.claude/commands` replaced by `.claude/skills`, new `.claude/scripts/`, the changed `loop.config` schema, removed `metrics.jsonl`/`loop.jsonl` writers, removed `code-review:code-review` and superpowers dependencies, removed context7 from engineer `tools:`. A "Known unverified" list links `docs/v2-pending-human.md`. `changelog.d/` is deleted after consolidation except its README.
- **Migration guide:** `docs/migrating-to-v2.md` (copy-install upgrade step by step; the `/migrate` skill; what never gets overwritten; how forks are detected; a section each for Install A-style forks and Install B-style old copies with an infra agent that has no frontmatter; rollback to v1.2.0). `loopctl migrate-plan --check|--write` converts `build-plan.json`.
- The KIT_MANIFEST for v1.2.0 and for the release are generated by `tools/build-dist.py --manifest`.

**Rationale.** The loop has never run anywhere and nothing in v2 has met a live Claude Code. Calling that `2.0.0` would overstate it. A prerelease tag is honest and costs nothing; doctor, `/migrate` and the manifest handle it.

---

## IMP-9. Final build order

1. **Stage 0 (conductor):** create `v2/integration`; create local tag `v1.2.0` on `cb6218f`; commit this file.
2. **Stage 1: Foundations** (`v2/00-foundations`, serial, one agent). See IMP-10.
3. **Wave A (parallel):** Phase 0a, Phase 1, Phase 0b. Merge order: 1, 0b, 0a (largest conflict surface first; they are disjoint, so any order works).
4. **Wave B (parallel):** Phase 2, Phase 3, Phase 6. Merge order: 2, 3, 6.
5. **Phase 4:** 4a (core), then 4b and 4c in parallel, then merge 4a, 4b, 4c.
6. **Wave D:** chain Phase 7 -> Phase 8 -> Phase 10 (serial) in parallel with Phase 11a; then Phase 11b.
7. **Phase 9** packaging and generation.
8. **Release stage** (`v2/12-release`): config template regeneration, docs consolidation, CHANGELOG, migration guide, adapters and parity table, `deferred.md`, `probe-assumptions.md`, `docs/v2-pending-human.md`, KIT_VERSION, manifests, **guard activation** (IMP-13).
9. **Review stage** (`v2/13-review-fixes`): full test suite, ref check, dist drift check, leak scan, then independent `security-reviewer` and `code-reviewer` passes over the guards, doctor, loopctl and driver, then fixes. The maintainer reviews after this.

**Deviation from the report's 0a -> 1 -> 0b order.** The report ordered those phases so that edits would not muddy the baseline. The baseline runs against the immutable tag `v1.2.0`, so file order no longer matters. They are built concurrently. The report's causal dependencies (doctor before guards' level logic; verify and doctor before loopctl; guards' session registry contract before loopctl) are preserved through Foundations and the order above.

---

## IMP-10. Foundations stage (new, ahead of everything)

**Decision.** Before any phase, one agent builds the shared substrate so that parallel phases do not each invent it.

Deliverables (`v2/00-foundations`):

- `.claude/scripts/kitlib/`: `miniyaml.py`, `config.py` (loads the fenced YAML in `loop.config.md`; typed accessors; v1-key compatibility map), `gitutil.py` (toplevel and common-dir resolution from a path, branch, `rev-parse`, CAS helpers with explicit `<ref>:<old>`), `jsonio.py` (atomic write, O_EXCL create), `redact.py` (secret patterns), `semver.py`, `evidence.py`, `runstate.py` (the layout of `<git-common-dir>/loopctl/`: lock, HALT, session registry, nonce, events dir, heldout dir, with atomic read/write; this is the shared contract between guards and loopctl), `caller.py` (the §5.7 classifier as a pure function over hook input plus the registry; Phase 2 wires it, Phase 4 populates the registry).
- `.claude/scripts/kitlib/schema/*.json`: the `loop.config` key registry (IMP-11), seeded with every v1 key.
- `tests/helpers/`: temp git repo factory, fake HOME builder (plants fake credentials), **fake `claude`** (scriptable: succeed, crash, stall, malformed JSON, 529, emit cost, write a report file), fake `gh`, local bare remote factory.
- `tools/test.sh`, `.github/workflows/kit-tests.yml` (3.9 and 3.13 matrix; actions referenced by tag with a "pin to SHA" comment, the human pins), `tools/check-refs.py` (every `.claude/...`, `docs/...`, `tools/...` path mentioned in markdown exists; excludes `.claude/worktrees/`), `tests/test_py39_syntax.py`, `tests/test_no_project_names.py` (IMP-19), `tools/gen-config.py` (IMP-11), `changelog.d/README.md`.

**Rationale.** Phase 1's level logic, Phase 2's guards, Phase 3's verify and Phase 4's loopctl all need the same config parser, git resolver, state layout and caller classifier. Building them four times would produce four incompatible versions and fail the corpus.

---

## IMP-11. `loop.config` stays markdown with fenced YAML; keys come from a registry

**Decision.**

- The file stays `.claude/loop.config.md` with a fenced `yaml` block (backward compatible, human-friendly). The parser accepts a **restricted subset**: block mappings, block sequences of scalars or of simple mappings, scalars, `[a, b]` flow lists, `#` comments, `|` literal blocks. No anchors, tags or multi-document files. Anything else is a validation error with a line number.
- Every key lives in `kitlib/schema/<phase>.json`: `{name, type, default, owner_phase, h6_protected, evidence_gated, doc, deprecated_by}`. Each phase adds its own schema file, so keys never conflict.
- `.claude/loop.config.md` and `examples/loop.config.md` are **generated** by `tools/gen-config.py` from the registry plus hand-written `header.md` and `notes.md` templates. `tests/test_config_template.py` fails if the committed files drift. Foundations converts the v1 file into `schema/v1.json` and proves a round trip (same keys and defaults).
- Gate toggles (`gates.<name>: on|report|off`), `h6_protected` keys and base-sha reading follow §9: loopctl and `verify` read gate config from the base sha, never the head.
- `runner` has no auto-selected default beyond `sequential` (DEC-23). `delivery` defaults to `local`, `git_autonomy` to `none`, `model_profile` to `quality`, `parallel_mode` to `off`, `merge_policy` to `human`, `report_trailers` to `warn`.

**Rationale.** `loop.config.md` is the most-edited file in the plan; generation removes the conflict and gives the doctor one place to validate keys and H6 status.

---

## IMP-12. Hot files, ownership and the docs-fragment rule

**Decision.** (See IMP-6 for the mechanism.) Ownership of shared files:

- `.claude/commands/*.md`: Phase 0b first. Later phases add behaviour through new `.claude/context/*.md` files and the conductor adds pointers at merge time. Phase 3 owns the delivery edits to `tech-lead.md` and `architect.md` in Wave B (0b is merged before Wave B starts). Phase 4a owns the loopctl handoff block in `orchestrate.md`. Phase 9 converts all of them.
- `.claude/spec/loop-engineering.md`: Phase 0b rewrites the tool table. Phase 4a adds `.claude/spec/loopctl.md` as a separate file. The release stage updates the "LOCKED" banner to v4.
- `.claude/settings.json`: untouched until guard activation (IMP-13). Staged as `.claude/settings.v2.json`.
- `AGENTS.md`, `.cursor/rules`, `skills/chain-of-custody`: Phase 0b adds the parity table and "advisory runtime" banner; Phase 9 regenerates the adapters.
- `.github/CODEOWNERS` stays as is (it is this repo's own). The shipped template with placeholders is `.claude/templates/security/CODEOWNERS.template`.

---

## IMP-13. Guard activation is the last commit, not part of Phase 2

**Decision.** Phase 2 builds the v2 guards and stages the wiring as `.claude/settings.v2.json`. The **live** `.claude/settings.json` and the v1 shell hooks stay unchanged for the whole build. In the release stage, after the corpus is green and sample payloads have been piped through every guard by hand, one commit switches `settings.json` to the v2 wiring and removes `.claude/hooks/guard-git.sh` and `guard-main-edit.sh`.

**Rationale.** The build agents run under the repo's live hooks. A bug in a fail-closed v2 guard activated mid-build would block every agent, including the one fixing it.

**Consequences.** The wiring is generated from one source, `guards/hooks.manifest.json`, via `guards/wiring.py`; the same source emits the plugin's `hooks.json` in Phase 9.

---

## IMP-14. Test-environment rules (applies to every phase)

**Decision.**

- No test and no implementer command may execute the real `claude`. A `KIT_CLAUDE_BIN` environment variable points the doctor, the driver and the probes at the fake.
- No network. Tests that would call a registry or GitHub use recorded JSON fixtures and an injected client. `urllib` calls are behind a function that tests replace; an `--online` flag is off in tests.
- Tests that push (CAS over the remote path, remote lock) push only to a **temp bare repo** created in the test's tmpdir. They never touch the project's `origin`.
- Credentials: every doctor test sets `HOME` to a temp dir, clears `AWS_*`, `GH_*`, `GITHUB_*`, `SSH_AUTH_SOCK`, `KUBECONFIG`, sets `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_SYSTEM=/dev/null`, and plants fake files. Tests never read the real home. `doctor --live` (which calls `sts`, `gh auth status`, `ssh -T`) is never executed by an agent or in CI.
- `gh` and `docker` are faked or stubbed. GitHub-side scripts are tested for argument construction and dry-run output only.
- Processes started by tests (verify's dev-server, fake claude) run in their own session and are killed with `os.killpg` in `tearDown`.

---

## IMP-15. GitHub-side and human-only actions: files, scripts and one checklist

**Decision.** Everything that needs GitHub admin, account settings, cloud accounts or a live Claude is delivered as files plus a script the human runs, and is tracked in one place: `docs/v2-pending-human.md`, created in the release stage from `changelog.d` "pending-human" sections. It lists, in order:

1. Push branches and the `v1.2.0` tag; open the PR.
2. Run the three baseline runs (`tools/baseline/run-baseline.sh`), commit the results, run `tools/baseline/summarize.py`.
3. Create the GitHub App and bot identity per `docs/security/bot-identity.md`.
4. Apply rulesets and CODEOWNERS: `.claude/scripts/github/apply-boundary.sh` (dry-run by default; `--apply` is the human's), then `verify-boundary.sh`.
5. Run the probes in `tools/probes/` that matter (V1, V5, V9, V18, V19, V22, V24, V27 first).
6. Run the canary on the pinned CLI and set `claude_code_tested_max`.
7. Run `claude plugin validate plugin/` (script provided) and the eval suites.
8. Attest `deploy_triggers` and `git_integrated_hosts` in `loop.config`.
9. Commit `docs/baseline/evidence.json` when (and only when) the evidence gate is genuinely met.
10. Migrate Install A and Install B with `/migrate`; then cut `2.0.0`.

The scripts accept `--dry-run` as the default, print every `gh api` call they would make, and refuse to run when `CLAUDECODE` is set.

---

## IMP-16. `onFailure: block` syntax is unverified: do not depend on it

**Decision.** Wire `"onFailure": "block"` on every blocking-capable hook handler exactly as the report describes, record in `probe-assumptions.md` that the field's name and placement are unverified (V9), and also make every hook entry point fail closed itself: wrap `main()` so any exception, malformed input or unknown hook-input shape prints a reason and exits 2. A missing interpreter is the only case the platform field alone would cover; the SessionStart check and the liveness canary report it. Stop, SubagentStop, TaskCompleted, TeammateIdle and async hooks stay advisory and are re-validated by loopctl.

---

## IMP-17. Frontmatter, model profile and `maxTurns`

**Decision (Phase 0b).**

- `maxTurns` is **unset** on every agent (no baseline exists to support a p95). A comment in each agent file and the `nominal budgets` removal in `engineer-protocol.md` say so; the baseline summariser prints a p95 recommendation once data exists.
- Engineers and critics get `disallowedTools: Agent, Skill, Workflow, mcp__*` and an explicit `tools:` allowlist without any `mcp__plugin_context7_*` entries. A project that wants a documentation MCP adds its tool name to `tools:` and removes it from the deny (documented in README and `docs/faq.md`). Prose says "if a documentation tool is available, use it; otherwise WebFetch".
- `effort: high` on critics, `database-engineer` and `backend-engineer`; `high` on the frontends and infra too (the `quality` profile). The conductor escalates to `xhigh` at review round 2 (§11.4).
- `model: opus` stays in frontmatter (the `quality` profile). `.claude/context/model-profiles.md` defines `balanced` and `economy`: the conductor passes a per-invocation `model` and omits it when `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set. Default stays `quality` (DEC-16).
- `infra-engineer.md` (a file in Install B's copy has no frontmatter; the kit's own has it): keep frontmatter, add `NEEDS_CLARIFICATION`.

---

## IMP-18. Evals: kit-neutral cases plus local deterministic graders

**Decision.** The exact `claude plugin eval` suite format and `--model` pin are not verified here, and agents cannot run it. So `evals/cases/*.json` (about 14-20 cases from §15 item 3) are authored kit-neutral: `{id, class: safety|quality, setup, prompt, graders[], threshold, max_cost_usd}`. Graders that can run on a **recorded transcript** (regex, file exists, command-not-run, verdict-schema) run locally via `tools/evals/run_local.py` against checked-in synthetic transcripts. An implementer may consult the `claude-code-guide` agent for the real suite format; if it is documented, add `tools/evals/to-claude-eval.py`; if not, record it in `probe-results` as pending. Safety cases use threshold 1.0; quality 0.8 (a mean, P28). Live eval runs and `--ablation` are pending-human.

---

## IMP-19. Real-project-name leak check

**Decision.** `tests/test_no_project_names.py` tokenises every tracked text file (`[A-Za-z0-9_-]+`, lowercased, plus path segments) and compares token hashes (SHA-256) against `tests/data/leak-denylist.sha256`. The conductor generates that file in Stage 1 from the two sibling-project directory basenames and stores only the hashes, so the denylist itself does not leak the names. The conductor also runs a plain `grep -ri` with the clear-text names, from outside the repo, before each merge and at the end. Fixtures and examples use "Install A" and "Install B", and invented names (`acme`, `hello-loop`) elsewhere.

---

## IMP-20. Small rulings from the §19.6 edit list

- `LICENSE:3`: `Copyright (c) 2026 Sujil Maharjan` (the maintainer's name as it appears in the commit history). Flagged for the maintainer's review.
- `docs/choosing-your-stack.md` moves to `.claude/context/choosing-your-stack.md` (`git mv`), `planner.md:37` reads it from there, and the README link is updated (CO-47).
- `docs/domain-glossary.md` (CO-30) is delivered as `.claude/templates/domain-glossary.md` plus a one-paragraph mention (the file belongs in the user's project, not in the kit's docs).
- The three `local-dev.md` edits change commented-out template examples only, as the report notes.
- `README.md:80-88` adds `.claude/launch.json` and, once Phase 2 lands, the v2 event paths; since Phase 2 lands later, 0b writes only the `launch.json` line and the release stage updates the rest.
- Slicing rules (§19.7 L-35) go into Phase 0b prose (`planner.md`, `architect.md` `## Slices`, template). Their mechanical checks (`validate` reports critical path and DAG width; `size: L` must be split) arrive with Phase 4a.

---

## IMP-21. Version range and level caps

**Decision.** `KIT_VERSION` records `claude_code_min: 2.1.302` and `claude_code_tested_max: none`. The doctor's effective level is `min(configured, prerequisites, B5 cap, platform cap, evidence cap, version cap)` with a printed reason for each. `tested_max: none` caps at L2 (the report's rule: refusal at L3+ until the canary passes). A missing `evidence.json` caps at L3. A `claude` version the doctor cannot read (the real binary is never executed by agents) is "unknown", which caps at L1 until the human runs the doctor on their machine. So **out of the box, with nothing attested, v2 is an L1 kit with better guards**, which is the safe reading of the report.

---

## IMP-22. Doctor and gate interface contracts (so parallel work composes)

**Decision.**

- `doctor.py` emits human text by default and `--json` conforming to `doctor.v1`: `{schema, level: {configured, effective, caps: [{name, level, reason}]}, checks: [{id, severity: error|warn|info, status, message, remediation}], attestations: [...]}`. Exit codes: 0 no errors, 1 errors present (level capped), 2 usage or internal error. Flags: `--credentials`, `--deploy-surface`, `--level`, `--json`, `--repo <path>`, `--home <path>`, `--live` (human only). Every check has a stable id (`cred.aws.profile`, `deploy.push-workflow`, `sandbox.failIfUnavailable`, ...).
- Gate plug-ins (Phase 4a defines, 4b implements): `gates/<name>.py` exposing `NAME`, `OWNER_METRIC`, `run(ctx) -> GateResult{status: pass|fail|error|skipped, findings: [...], evidence: {...}, failure_class}`. `ctx` carries the item, the base sha, the config read **from the base sha**, and the paths. Toggles `on|report|off` are applied by the runner, not the gate.
- Hook entry points read JSON from stdin, write nothing to stdout unless the platform protocol requires it, and block with exit 2 and a one-line reason on stderr, like v1.
- Deny messages name the rule id (`guard-git:no-verify`) so the corpus rows and the telemetry match.

---

## IMP-23. Boundary fixtures: what can be proved offline

**Decision.** Of the five §15 item 9 fixtures: "deploy by side effect", "human credentials" (planted fake credentials in a temp HOME) and "Workflow headlessly" (doctor refuses a settings file with a `Workflow` allow) are fully testable offline and are required to pass in Phase 1. "Bypass list" is tested by the doctor's ruleset reader against recorded `gh api` JSON, and "skill hooks in CI" by `ci_lint` rejecting a workflow that does not restore skill directories from the base ref before `-p`. The live halves (the real ruleset rejecting a bot push, a real CI job not creating the marker) are in `verify-boundary.sh` for the human. Phase 1's exit criterion is therefore met offline and marked "live verification pending".

`ci_lint.py` is the stdlib structural linter for workflow YAML (restricted subset, IMP-11). It enforces RC-3, RC-18 and DEC-39: no job with `id-token: write` that checks out PR head code; no secrets in `pull_request` jobs; no `pull_request_target` checkout of the head ref; skill/plugin directories restored from the base ref before any `-p` step; floating action tags reported as warnings. A workflow the parser cannot read is reported "unparsed: manual review", never "passed".

---

## IMP-24. Definition of done for every topic branch

A topic branch is merged only when all hold:

1. `tools/test.sh` passes on the topic branch merged with the current `v2/integration` (offline, under 60 s).
2. `tools/check-refs.py` and `tests/test_py39_syntax.py` pass.
3. The leak check passes (hash test plus the conductor's grep).
4. A `changelog.d/<topic>.md` exists (changes, upgrade impact, new keys, pending-human actions, docs impact).
5. New config keys are in the registry; the generated config templates are regenerated and in sync.
6. The topic's exit criterion is demonstrated or explicitly marked "pending-human" with the reason (IMP-15).
7. No push, no network, no real `claude`, no real credentials were used; the final report says so.
8. For guards, doctor and loopctl: an adversarial self-review pass listing bypasses the author tried, with the corpus rows added.

---

## IMP-25. Items the report left too vague, with rulings

- **"Observation-only telemetry hook"** is `tools/baseline/observe.py` (IMP-2): append-only JSONL of hook stdin, secrets redacted, always exit 0. The Phase 2 telemetry hooks are separate and write per-`agent_id` files (§7.3, no hash chain).
- **"Sandboxed first-run probe"** is `.claude/scripts/probe_run.py`: prints the command list, requires an explicit `--go`, runs commands in `docker run --network none` if docker exists, otherwise prints the commands for the human and exits. A dry-run mode is the tested path.
- **"PR audit block"** (Phase 3) is `.claude/templates/pr-body.md` with named fields the conductor fills; `pr_prepare.py` validates that no field is empty and prints the exact `git push` and `gh pr create --draft --body-file` commands for the **human** to run. It never runs them. In the interactive lane the human may run them from the session with `!`.
- **Squash setting assertion** and **repo visibility/plan** are read by the doctor with `gh api` only when `--live` or when given recorded JSON via `--gh-fixture`; otherwise they are "attest required" and count as unknown.
- **Devcontainer template**, **ruleset JSON** and **OIDC trust policies** are TEMPLATE: JSON-linted, keys checked against the report, not applied.
- **Rego policy bundle** (Phase 6) is TEMPLATE and unverifiable without `conftest`; the Python `source_policy.py` provides the testable pre-plan bans (external/http data sources, `local-exec`/`remote-exec`, unpinned `source`, new `required_providers`).
- **Spend ledger** accepts the `total_cost_usd` field from the child's `-p` JSON as documented in the report; the fake `claude` emits it. If the real field name differs, the driver records `unknown` and parks, never guesses.
- **Cost projection** uses fixture p50/p90 once baseline data exists; before that it prints `unavailable (no baseline)` and requires the human to set `run_budget_usd` explicitly.
- **Notifications:** `notify_command` hook plus PushNotification prose (DEC-31). The Phase 7 hook only runs the configured command; it never sends anything itself.
- **`/fewer-permission-prompts`:** documented for the interactive lane only, never for building the headless allowlist.
- **Contradiction, Phase 3 vs L2:** "pushes happen from the human's session" in Phase 3, and the L2 "loop pushes with the bot identity". These are consistent: the L2 push is made by the human-started `loopctl run` process holding the bot credential, never by an agent. In this build `loopctl`'s push path is exercised only against temp bare remotes.
- **Contradiction, report §16.1 Phase 6 listed under the evidence gate vs the Minimal path** (infra rule as policy): Phase 1 delivers the infra human-gate policy as `docs/security/infra-human-gate.md`; Phase 6 delivers the CI templates. Neither is activated by the kit; the human installs them.
- **`loopctl` is a package directory** run as `python3 .claude/scripts/loopctl <cmd>` (a directory with `__main__.py`), not `scripts/loopctl.py`.
- **Window of risk in `loopctl` subprocess git:** loopctl runs git with fixed argument lists via `subprocess` with `shell=False`, `GIT_CONFIG_GLOBAL` and `core.hooksPath` pinned, and never interpolates plan or issue text into a command line.
- **`/bootstrap` greenfield repo creation** emits the `gh` commands as a script for the human (DEC-13). Its receipt is `.claude/bootstrap-receipt.json`.
- **UAT widget, monitoring-to-intake, reproduce-first bug lane, primer-lockfile drift routine:** the dependency check and intake workflow templates are built (Phase 11a); the UAT widget and monitoring intake are DESIGN-ONLY; the bug lane is a section in `docs/design/deferred.md`.

---

## IMP-26. Independent review before handing over

**Decision.** After the build, the conductor dispatches `security-reviewer` and `code-reviewer` (the kit's own agents, fresh context) over the guards, doctor, loopctl driver and the GitHub-side scripts, and one adversarial pass on "can an agent with Bash get around H4 in the shipped templates". Findings are fixed on `v2/13-review-fixes`; unfixed items are listed in `docs/v2-pending-human.md` under "Known gaps". The final summary to the maintainer lists what was built, what is OPT-IN, what is pending-human, and every deviation from the report.

---

## Per-phase build table

| Phase | Build now? | Form | Key deliverables (files) | Depends on |
|---|---|---|---|---|
| Stage 0 | yes | n/a | branch `v2/integration`, tag `v1.2.0`, this file | none |
| Foundations | yes | FULL | `.claude/scripts/kitlib/*`, `kitlib/schema/*.json`, `tests/helpers/*`, `tools/{test.sh,check-refs.py,gen-config.py}`, `.github/workflows/kit-tests.yml`, `tests/test_{py39_syntax,no_project_names,config_template}.py`, `changelog.d/README.md` | Stage 0 |
| 0a Baseline | yes (runs pending) | FULL tooling; runs PENDING | `examples/hello-loop/**`, `docs/baseline/{README,kill-criteria,RUNS}.md`, `docs/baseline/results/TEMPLATE.json`, `docs/baseline/evidence.template.json`, `tools/baseline/{run-baseline.sh,observe.py,summarize.py}`, `tests/test_hello_loop.py` | Foundations |
| 1 Boundary | yes | FULL doctor + lint; TEMPLATE for GitHub; live verification PENDING | `.claude/scripts/{doctor.py,ci_lint.py,session_start.py,probe_run.py}`, `.claude/scripts/github/{apply-boundary.sh,verify-boundary.sh}`, `.claude/templates/security/**`, `.claude/templates/github/workflows/label-guard.yml`, `docs/security/*` (incl. `infra-human-gate.md`), `.gitignore`, `tests/doctor/**` | Foundations |
| 0b Prose | yes | FULL | all §19.6 edits, slicing rules, frontmatter (IMP-17), `.claude/context/{model-profiles,choosing-your-stack}.md`, `.claude/templates/domain-glossary.md`, `LICENSE`, parity table in `AGENTS.md` | Foundations |
| 2 Guards v2 | yes | FULL; wired only at activation | `.claude/scripts/guards/**` (`run.py`, git/main-edit/infra/exec/critic-write/halt/config-change/selftest, `wiring.py`, `hooks.manifest.json`, telemetry, injection flag), `.claude/settings.v2.json`, deny/headless/allow templates, `tests/guards/corpus/*.json`, `evals/cases/*.json`, `tools/evals/run_local.py` | Foundations, 1 (session_start) |
| 3 Interactive PR delivery | yes | FULL | `.claude/scripts/{verify.py,pr_prepare.py}`, `.claude/templates/pr-body.md`, `.claude/templates/github/workflows/{verify.yml,claude-review.yml}`, `delivery`/`git_autonomy` keys, tech-lead/architect delivery prose, `tests/verify/**` | Foundations, 0b, 1 (ci_lint) |
| 4a loopctl core | yes | FULL (Tier 1) + Tier 2 writes, OPT-IN | `.claude/scripts/loopctl/{__main__,cli,plan,state,lock,failure,ledger,render,status,abort,reconcile,audit,report}.py`, `.claude/templates/plan-item.json`, `.claude/spec/loopctl.md`, `migrate-plan`, `examples/hello-loop/plan/items/*.json` | Foundations, 1, 3 |
| 4b gates | yes | FULL: G0, G1, G2/G3, G5-glob, G6, G7, G9, G11, Evidence; OPT-IN: G4, G5-AST | `loopctl/gates/*.py`, `.claude/agents/acceptance-author.md`, `.claude/templates/github/workflows/heldout.yml`, `tests/loopctl/gates/**` | 4a |
| 4c driver | yes | OPT-IN (`runner: driver`), tested with fake `claude` | `loopctl/{driver,launch,compact_hook,heartbeat}.py`, `tests/helpers/fake_claude.py` scenarios, `tests/loopctl/fault_injection/**` (serial cases from §15 item 5) | 4a |
| Evidence gate | recorded, not passable here | PENDING-HUMAN | `docs/baseline/evidence.json` absent by design; `loopctl evidence --check` | 0a, 4 |
| 6 Infra CI | yes | TEMPLATE + testable linters | `.claude/templates/infra/**` (workflows, rego, OIDC), `.claude/scripts/infra/source_policy.py`, `ci_lint` rule additions, `docs/security/infra-ci.md`, fixture repos in `tests/fixtures/` | 1, Foundations |
| 7 Unattended (L4) | yes | OPT-IN, evidence-gated | `loopctl/unattended.py`, `.claude/scripts/notify.py`, `.claude/agents/intake-triage.md`, `intake_schema.json`, `docs/unattended.md` | 4c |
| 8 Slicing and parallel | slicing: in 0b and 4a; parallel: yes | OPT-IN, evidence-gated; Workflow host DESIGN-ONLY | `loopctl/parallel/{worktrees,ports,mutex,footprint,scheduler,integrate,cascade}.py`, Worktree hooks, `.worktreeinclude` template, `.claude/spec/pipeline-host-workflow.md` | 4c, 7 (loopctl serial) |
| 10 Learning loop | yes | FULL warn-mode; block-mode OPT-IN | `loopctl/learning/*`, `.claude/commands/retro.md` (skill after 9), `report_trailers` key, Docs Delta gate, `docs/harness-review.md` | 4, 8 (serial on loopctl) |
| 11a Bootstrap and ops | yes | FULL prose/templates; UAT and monitoring DESIGN-ONLY | `.claude/commands/bootstrap.md`, `.claude/scripts/bootstrap_receipt.py`, `.claude/templates/github/workflows/{intake,dependency-check,smoke}.yml`, `labels.json` plus apply script, release-automation detection note | 1, 6 |
| 11b loopctl extras | yes | OPT-IN: remote lock, G10, manifest; L5 classifier only | `loopctl/{remote_lock,merge_class,manifest}.py`, `gates/g10_product.py`, doctor L5 prerequisites | chain 7-8-10 |
| 9 Packaging | yes | FULL generator; real plugin validation PENDING-HUMAN | `.claude/skills/*/SKILL.md` (from commands), `tools/{build-dist.py,validate-plugin.py}`, `plugin/**`, `.claude-plugin/marketplace.json`, `dist/copy-install/**`, `.claude/scripts/migrate.py`, `/init`, `/migrate`, `KIT_MANIFEST` | all prior |
| Release | yes | docs | regenerated config templates, README/GLOSSARY/getting-started/faq/SVG updates, `CHANGELOG.md` (2.0.0-rc.1), `docs/migrating-to-v2.md`, `docs/design/{deferred,probe-assumptions}.md`, `docs/v2-pending-human.md`, `KIT_VERSION`, guard activation commit | 9 |
| Review | yes | QA | full suite, drift and leak checks, security-reviewer and code-reviewer passes, `v2/13-review-fixes` | Release |
| Probes (V-items) | harness only | PENDING-HUMAN | `tools/probes/**`, `docs/design/probe-results.template.md` | Foundations |
