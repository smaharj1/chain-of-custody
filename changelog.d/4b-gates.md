# 4b-gates: the loopctl gates G0, G1, G2, G3, G5, G6, G7, G9, G11 and Evidence (FULL), G4 mutation and the G5 AST lint (OPT-IN)

## Changes

- `.claude/scripts/loopctl/gates/`: one module per gate on the plug-in contract of 4a (`NAME`, `OWNER_METRIC`, `run(ctx) -> GateResult`, `REQUIRED_FOR`, `DEFAULT_MODE`, `EVIDENCE_GATED`). `gates/__init__.py` is unchanged (no contract gap was found). Spec: `.claude/spec/loopctl-gates.md`.
  - `g0_env.py` (G0): wraps `doctor.py --json` (configured level must not exceed the effective one), `requiresEnv`, clean tree, toolchain pins (`.nvmrc`, `.python-version`, `.tool-versions`, `engines`, `requires-python`; `_toolchain.py`), verify ports, `env_check_cmd`, and a green baseline recorded for G6. Also pins the review files so an engineer cannot write the reviewers' verdicts.
  - `g1_acids.py` (G1): criteria unchanged since H1, every id named by a (pinned) test file, an executed passing result for THIS head from G3 or G6.
  - `g2_stubs.py` (G2): the plan declares `ext.g3`; stubs are stubs (stdlib `ast`: nothing runs at import); stubs committed before tests; tests RED by assertion or `NotImplementedError` against the stubs (an import, collection or signature error is invalid, not red); characterization items must be green; pins the acceptance tests (blob ids in `<git-common-dir>/loopctl/pins/` and `locked_paths.json` source `g3`).
  - `g3_acceptance.py` (G3): pinned tests unchanged (`reward_hack` finding), every criterion executed and passing, real assertions (no `assert True`, `expect(true)`, bare `raises(Exception)`), held-out tests (driver: `<git-common-dir>/loopctl/heldout/<item>/` with `heldout_cmd`; sequential: CI-only), visible vs held-out pass rate in the summary.
  - `g4_mutation.py` (G4, OPT-IN): tool-agnostic wrapper around `mutation_cmd`, the `mutation.v1` JSON contract, `mutation_min`. No tool bundled.
  - `g5_integrity.py` (G5): the extended path-glob check against the plan's footprint and `ext.g5.allow`, pinned tests, shadowing modules (`unittest.py`), the scripts the configured commands run, package.json and pyproject judged on their test and lint content; OPT-IN AST lint (`g5_ast_lint`): skip markers, test-env detection, mocks of the footprint, snapshots with code, `-u`. JS/TS findings are low-confidence warnings.
  - `g6_verify.py` (G6): wraps `verify.py` (definition from the base sha) and adds per-AC executed-and-passed, no skipped AC test, skipped count not rising (static and against the G0 baseline), base test names a subset of head's (renames followed), no weakened assertion, migrations up/down/up on a prod-dialect database via `migrate_check_cmd`.
  - `g7_reviews.py` (G7): reads the saved, committed, fresh and self-consistent `code-review.md`, `security-review.md`, `design-review.md` from git; waivers from the plan.
  - `g9_supply.py` (G9): new dependencies (npm, PyPI, Go, Cargo, Ruby) declared, from a registry, not look-alikes, lockfile moved, no install scripts; registry facts via `dep_check_cmd`, licences via `license_allowlist`.
  - `g11_secrets.py` (G11): built-in scan of every commit's added lines, messages, uncommitted files, reports and logs; optional `secrets_scan_cmd`.
  - `evidence_gate.py` (Evidence): every `on` gate has a recorded pass for this head (or only reports changed since), the PR's check run is green from `pr_open`, writes `<git-common-dir>/loopctl/evidence/<item>-a<n>.json`.
  - helpers (underscore-prefixed, not gates): `_lib.py`, `_acparse.py` (per-criterion results from JSON, JUnit and unittest/pytest/go/TAP/jest/cargo output), `_acrun.py`, `_astscan.py`, `_toolchain.py`, `_ci.py` (the CI halves).
- `.claude/agents/acceptance-author.md`: the separate, fresh-context agent that writes stubs, visible and held-out tests (engineer-class tools, never the implementation).
- `.claude/templates/github/workflows/heldout.yml`, `heldout-sync.yml`, `mutation.yml`: TEMPLATES that pass `ci_lint` (no secret in a pull-request job: the held-out tests reach the job through the Actions cache, filled by a trusted job).
- Config schema `kitlib/schema/4b-gates.json` (11 keys); `.claude/loop.config.md` and `examples/loop.config.md` regenerated.
- Tests: `tests/loopctl/gates/` (14 modules, 270 tests, none above 6 s alone), including the Phase 4b exit criterion on the real hello-loop fixture. Whole suite 53 s here (48 s before this phase).
- Outside this phase's ownership, each for a stated reason (see Conductor): `kitlib/caller.py` (roster), `tests/test_frontmatter.py`, `tests/doctor/test_level_caps.py`, `tests/loopctl/__init__.py`, `tests/loopctl/test_gates.py`, `tests/loopctl/test_cli.py`.

## Upgrade impact

Nothing changes for anyone who has not opted into loopctl Tier 2 (`loopctl_tier2: false`, `runner: sequential`): the V1 prose loop never calls a gate. For a project that has opted in, `loopctl advance` now ENFORCES the gates that are installed and `on` (4a only enforced registered ones), so every transition in `REQUIRED_FOR` needs `loopctl gate <name> <item>` first: `g0` (designing), `g2` (stubbing), `g6 g3 g1 g5 g9 g11` (verifying), `g7` and `evidence` (reviewing). A project can set any of them to `report` or `off` in `.claude/loop.config.md` (H6; read from the base sha). An item with no `ext.g3` in its plan item fails G2 until the plan declares its acceptance artifacts (or `ext.g3.exempt` with a reason).

`gates.g4` is now an explicit key defaulting to `off`; `gates.g4: on|report` and any `g5_ast_lint` value are refused by `loopctl`, the doctor (`config.evidence-gate`) and the runner until `docs/baseline/evidence.json` is complete. The doctor's L5 prerequisite "mutation testing on" therefore needs the evidence gate, as IMP-1 says.

## New config keys

Owner phase `4b-gates`, section "gates", all H6-protected. Evidence-gated: `gates.g4` (`on`, `report`), `g5_ast_lint` (any non-empty list).

- `gates.g4` (`on | report | off`, default `off`): OPT-IN, EXPERIMENTAL mutation testing.
- `g5_ast_lint` (list, default `[]`): `python`, `javascript`. OPT-IN, EXPERIMENTAL.
- `heldout_cmd` (command): runs the held-out tests under `runner: driver` and in the CI job; prints per-criterion results.
- `mutation_cmd` (command), `mutation_min` (int 60), `mutation_timeout_s` (int 1800): the G4 wrapper.
- `env_check_cmd` (command): G0's project probe (database reachable, services up).
- `migrate_check_cmd` (command): G6's up/down/up on an ephemeral prod-dialect database; compared with `db_local`.
- `dep_check_cmd` (command), `license_allowlist` (list): G9's registry facts and licences.
- `secrets_scan_cmd` (command): G11's optional external scanner (gitleaks and the like).

Existing keys read, not registered: `gates.*` (Foundations), `runner`, `autonomy_level`, `platform_attested`, `verify_*`, `db_local`, `security_review`, `delivery`.

## Pending-human actions

- Phase 4 exit criterion, live half: run hello-loop (or a real feature) with the gates on `report`, read each owner metric from `loopctl status --matrix` and the recorded gate summaries, and decide which gates earn `on`. The offline half (every gate passing and failing as specified, the full walk through `advance`, and G3 on the hello-loop reference solution with its held-out checks) is demonstrated by the tests.
- `heldout.yml` / `heldout-sync.yml` / `mutation.yml` have never run on GitHub: create the private held-out store repository, the `HELDOUT_STORE_REPO` variable and the read-only `HELDOUT_STORE_TOKEN` secret, run `heldout-sync` once, make `heldout` a required check, and confirm that a pull-request workflow restores a cache saved on the default branch (the design relies on it). Pin the actions to SHAs.
- Confirm the verbose-output formats on your pinned runners (unittest `-v`, pytest `-v`/`-rA`, jest, go `-v`, cargo): `_acparse.py` reads them heuristically; prefer a JSON harness or JUnit for anything it misses (a miss is a false FAIL, never a false pass).
- Under `runner: driver` (4c): confirm the child sandbox really denies reading `<git-common-dir>/loopctl/heldout/`; until then held-out isolation is as strong as the guards (RC-6, P42).
- Evidence gate: G4 and the G5 AST lint stay refused until `docs/baseline/evidence.json` is committed by a human.

## Docs impact

- README: the agents table gains `acceptance-author`; the scripts table points at `.claude/spec/loopctl-gates.md`.
- GLOSSARY: held-out tests, pin (a pinned acceptance test), red / invalid (a test that fails an assertion vs one that never ran), characterization, reward-hack signature, owner metric, evidence file.
- docs/security/split-ci.md: the `heldout` row now has a companion `heldout-sync.yml` and a `mutation` row; `docs/security/guards.md`: review files and acceptance tests join `locked_paths.json` (sources `g7`, `g3`).
- docs/getting-started.md: "what each gate needs from your plan" (`ext.g3`, the verbose-output convention, `ext.g5.allow`) and the order the conductor runs them.
- docs/faq.md: "Why does G2/G3 fail?" (not declared, invalid red, pinned edit), "Where do held-out tests live?" (driver vs CI-only).
- `.claude/spec/loop-engineering.md`: pointer to `.claude/spec/loopctl-gates.md`; `.claude/context/model-profiles.md`: `acceptance-author` is an engineer-class role (`quality` profile, opus).
- CHANGELOG: "Added" entry for the gates and the agent.

## Decisions made

- **Where reviews come from.** loopctl cannot be the reviewer. G7 reads the reviewers' strict output, saved and committed under `docs/features/<slug>/reports/`, and checks it: one verdict token, agreement with the findings, freshness (no later non-report commit), verify commands run, every criterion named, scanners stated. The files are pinned in `locked_paths.json` from G0 on, because the engineer writes into the same directory.
- **Acceptance tests are pinned by content.** G2 records blob ids and the engineer-forbidden globs; G3 and G5 compare against that (or against the base when the tests rode in the plan PR). A second G2 run refuses to re-pin a changed file (`g2.pin-violated`). Everything the acceptance-author committed before G2 passed (scaffolding, `__init__.py`, conftest) is "authorized" for G5 as long as it is byte-identical.
- **A raising stub is red.** `NotImplementedError` (what a stub is for) and assertion failures are red; any other exception (import, attribute, signature) is invalid. A stub may return a constant sentinel; if the test then passes it is `g2.not-red`.
- **Per-criterion results are read from text, and text can lie.** A pass counts only for a line naming a test that a pinned file defines, in that file's module (a docstring or a same-named test elsewhere does not count); a failing line beats a passing one; JSON from the plan-declared harness and JUnit are preferred. Residual risk: production code that prints a fake runner line for a test that never ran. Mitigations: the exit code and "collected more than zero" checks, pinned files (G1), and the CI check run (Evidence). Recorded here, not hidden.
- **Gates that run code require HEAD.** G2, G3, G4 and G6 refuse to judge a commit the worktree is not at (`env` error) and flag tracked uncommitted changes (`item`). Untracked files are G5's and G11's concern.
- **G5 treats `.claude/**`, `plan/**`, `.github/**`, CODEOWNERS, the scripts a configured command runs, and new files named like a runner or standard-library module as protected.** The plan's footprint and `ext.g5.allow` are the only rationale (the plan is read at the base sha and reviewed by a human).
- **The "heldout" job gets its tests from a cache, not a secret.** `ci_lint` rightly refuses secrets in pull-request jobs and the report's own table has a step-scoped token there. The two are reconciled by `heldout-sync.yml` (trusted, default branch, token for one step) saving the cache that the pull-request job only restores. The residual exposure (anyone who can run workflows can print the cache) is stated in the template.
- **G4 needs `gates.g4` explicit.** A wildcard family cannot carry an evidence gate, so `gates.g4` is its own key with `evidence_gated: [on, report]`; the generated configs now list `gates: g4: off`.
- **No `ac_results` from G2.** A test red against a stub is not an executed passing test and the AC x evidence matrix counts passes.
- **The Evidence gate tolerates saved reports.** A pass recorded before a commit that touches only `docs/features/<slug>/reports/` still counts (the review is saved after the verify gates ran).
- **`acceptance-author` is in the default roster as an engineer** (`kitlib/caller.py`), so the guards classify it; projects with a custom roster add it.
- **Test speed.** Per-test git calls dominate; the gates memoise their git reads per run and the test class builds one `Context`. 4a's tests hide the real gate modules (`LoopRepo(real_gates=False)`) because they judge the runner with fake gates.

Adversarial attempts, each with a test (`tests/loopctl/gates/test_adversarial*.py` and the module of the gate):

- G2: a stub that runs code at import (call, loop, class body, computing default argument, `exec`), a test that asserts nothing and is "red" only because the stub raises, a constant assertion, a wrong signature, an import error read as red, tests committed before stubs, a sentinel stub that makes the test pass, an uncommitted edit, re-running G2 to re-pin an edited test.
- G3: editing, deleting or adding under a pinned glob (committed or not), a trivial engineer test that repeats the criterion ids in its docstring while the pinned test cannot import, a same-named test in another module, output spoofed by production code, `assert True`, a skipped criterion, a criterion with no test line, judging a commit the worktree is not at, an uncommitted tracked fix, a branch that edits the config to turn the gate off, held-out tests missing, partial, or silent, L2 without isolation.
- G1: criterion ids repeated in the engineer's own test file, stale evidence, changed criteria.
- G5: a new `unittest.py`, `json.py`, `sitecustomize.py` (also nested), the script a verify command runs, `.gitattributes`, `.coveragerc`, `package.json` scripts and jest keys, `pyproject` pytest options, moving a base test out of the test tree, snapshots made with the code, `-u`.
- G6: weakening the verify definition on the branch, deleting or renaming a test, loosening an assertion or padding with a constant one, adding a skip (static and runtime), a skipped criterion test, a criterion that never printed a line, the wrong or an unnamed database dialect, a lossy migration's down.
- G7: a review pasted from another item, an uncommitted review, an approval that lists a blocker, a stale review, a review that never ran the verify commands, an engineer writing the review file (pinned).
- G9: a Cyrillic homoglyph of a popular name, an `npm:` alias, git/URL/file sources, a lockfile that moves alone, lifecycle scripts, `.npmrc` re-enabling scripts, a dependency the plan did not announce, a registry check that crashes or prints garbage (unknown, never safe).
- G11: a secret added then removed in the same branch, in a commit message, in an uncommitted file or report, an excuse in a trailing comment, a file too big to scan (warned, not skipped silently).
- CI: a pull request that supplies its own evidence file, a symlinked held-out store directory, a base given as a branch name, output that would leak assertion text.

## Conductor: pointers to add

- Merge order: 4b and 4c are independent; after both, run `python3 tools/gen-config.py --write` (`.claude/loop.config.md` and `examples/loop.config.md` conflict textually).
- Files outside this phase's ownership, with the reason: `.claude/scripts/kitlib/caller.py` (one roster line for `acceptance-author`); `tests/test_frontmatter.py` (the ninth agent; a new `AUTHORS` set); `tests/doctor/test_level_caps.py` (its "ready" fixture sets `gates.g4: on`, which is now evidence-gated: the L3 test drops it); `tests/loopctl/__init__.py` (`LoopRepo(real_gates=False)` hides the real gate modules from 4a's tests, which use fake gates; `real_gates=True` for `tests/loopctl/gates/`), `tests/loopctl/test_gates.py` (the not-installed example is now `g10`; the evidence-gated stand-in is `g10` because the real `gates.g4` is refused at config level), `tests/loopctl/test_cli.py` (source rules: `gates/g0_env.py` and `gates/_toolchain.py` may start processes, three more stdlib modules).
- For 4c (the driver): run the gates in this order with the worktree committed (WIP commit first; G2/G3/G4/G6 refuse a dirty tracked tree and a worktree not at `--head`): `g0` after `begin`; `g2` at stubbing; `g6`, `g3`, `g1`, `g5`, `g9`, `g11` at verifying (g1 reads g6's and g3's recorded results, so it runs last); save and COMMIT the reviewers' output as `docs/features/<slug>/reports/{code,security,design}-review.md`, then `g7`; then `evidence`. A finding with `reward_hack: true` (`g2.pin-violated`, `g3.pinned-edited`, `g5.pinned-test-edited`) is the discard signal: pass reason `reward_hack` to `Loop.fail`. Held-out tests: create `<git-common-dir>/loopctl/heldout/<item>/` (not under the engineer child's sandbox read allowance: `denyRead` for engineers, write access only for the `acceptance-author` child), put the harness there, and set `heldout_cmd`. The `acceptance-author` child gets the brief paths in `.claude/agents/acceptance-author.md`.
- `docs/security/split-ci.md` (Phase 1/3): update the `heldout` row (it now has `heldout-sync.yml`) and add `mutation`; `docs/security/rulesets.md`: `heldout` is a required check only under `runner: sequential`.
- `.claude/context/model-profiles.md`, `README.md` (agents table), `GLOSSARY.md`, `docs/getting-started.md`, `docs/faq.md`, `.claude/spec/loop-engineering.md`: see Docs impact (hot files, not edited here).
- `docs/design/deferred.md` (release stage): G8 verifier agent and G10 stay deferred; Windows is not supported by the process-group code in `verify.py`, which the gates reuse.
