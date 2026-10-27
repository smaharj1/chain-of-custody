# loopctl gates: what each one checks, reads and refuses

**Status**: Phase 4b. Implements report section 9 (`docs/design/review-2026-10.md`) under IMP-1, IMP-22 and IMP-24 (`docs/design/v2-implementation-decisions.md`) on the plug-in contract of `.claude/spec/loopctl.md`. The driver that runs gates in a loop is Phase 4c.

A gate is a module in `.claude/scripts/loopctl/gates/` with `NAME`, `OWNER_METRIC` and `run(ctx) -> GateResult`. The runner (`gates/__init__.py`) applies the `gates.<name>: on | report | off` toggle **from the base sha** and turns a crashing or lying gate into an `error`. Run one by hand with `python3 .claude/scripts/loopctl gate <name> <item>`; `loopctl advance` refuses a move whose required gates (`REQUIRED_FOR`) have not passed for this head.

| Gate | Form | Default | Needed at | Checks | Owner metric |
|---|---|---|---|---|---|
| **G0** env | FULL | on | `designing -> design_review` | doctor's effective level is not below the configured one; `requiresEnv` set; clean tree; toolchain pins; verify ports free; `env_check_cmd`; baseline green (recorded for G6) | share of failures classed `env` |
| **G1** AC ids | FULL | on | `verifying -> reviewing` | criteria unchanged since H1; every id named by a test file at HEAD; an executed passing result for THIS head from G3 or G6 | AC coverage |
| **G2** stubs | FULL | on | `design_review -> building`, `stubbing -> building` | `ext.g3` declared; criteria covered; stubs are stubs; stubs before tests; **red by assertion against the stubs**; pins the tests | enabler for G3 |
| **G3** acceptance | FULL | on | `verifying -> reviewing` | pinned tests unchanged; every criterion executed and passing, none skipped; real assertions; held-out tests pass (driver) or are CI-only | held-out vs visible pass rate |
| **G4** mutation | OPT-IN, experimental | **off**, evidence-gated | CI | `mutation_cmd` score >= `mutation_min` on the changed lines | surviving-mutant rate |
| **G5** integrity | FULL (glob) + OPT-IN (AST lint) | on | `verifying -> reviewing` | no test, config, CI, lint or snapshot change the plan did not allow; pinned tests untouched; with `g5_ast_lint`: skip markers, test-env detection, mocks of the footprint, snapshots with code, `-u` | integrity flags per item; false positives |
| **G6** verify | FULL | on | `verifying -> reviewing` | wraps `verify.py`; per-AC executed and passed; no skip among AC tests; skipped count not rising; base test names subset of head's; migrations up/down/up on a prod-dialect database | verify-pass-but-CI-fail rate |
| **G7** reviews | FULL | on | `reviewing -> pr_open \| awaiting_human \| merged` | committed, fresh, consistent code review (and security review when required); waivers | reviewer precision |
| **G9** supply chain | FULL | on | `verifying -> reviewing` | new dependencies declared, from a registry, not look-alikes, lockfile moved, no install scripts | new dependencies per item |
| **G11** secrets | FULL | on | `verifying -> reviewing` | secret-shaped text in the item's commits, messages, reports and logs; optional external scanner | secrets reaching a PR (0) |
| **Evidence** | FULL | on | every move from `reviewing` | every `on` gate has a recorded pass for this head; CI check run green once there is a PR; writes the local evidence file | none |
| G8 | a view | | | `loopctl status --matrix` | |
| G10 | deferred to Phase 11 | | | | |

Failure classes follow the taxonomy of `.claude/spec/loopctl.md`: a broken machine is `env` (parked, not retried), a busy port or a timeout is `transient`, a failing test or a policy breach is `item`. A finding named `reward_hack: true` (an edited pinned acceptance test) is what the driver turns into a discard.

## What the plan declares (`ext`, read from the plan at the base sha)

The plan is immutable on main and changes only through a plan PR (H2), so everything below is human-reviewed. `ext` is free-form for gates (at most 8 KiB); these keys are the gates' contract.

```json
"ext": {
  "g3": {
    "visible":  ["tests/acceptance/f2/**"],          // the acceptance-author's visible tests; pinned
    "stubs":    ["src/bookmarks/api.py"],            // interface stubs, committed first
    "cmd":      "python3 -m unittest -v tests.acceptance.test_f2",   // default: verify.tests
    "class":    "behaviour",                         // or "characterization" (a refactor: green on both sides)
    "heldout":  true,                                // held-out tests exist for this item
    "junit":    "reports/f2.xml",                    // optional: JUnit XML the command writes
    "exempt":   "documentation only"                 // OR a reason: no executable tests; passes with a human flag
  },
  "g5":  {"allow": ["package.json"]},                // test or config files this item may edit
  "g9":  {"allow": ["somepkg"]},                     // a look-alike or non-registry dependency a human has accepted
  "g11": {"allow": ["tests/fixtures/**"]},           // paths that may hold a fake secret
  "sensitive": true                                  // G7: needs a security review
}
```

An item with no `ext.g3` fails G2 (`g2.not-declared`: add `ext.g3`, or `ext.g3.exempt` with the reason) and runs G3 on its `verify.tests` with a warning that nothing is pinned. A bad declaration (unknown key, unsafe glob) is a finding, never ignored.

## Reading test results (`gates/_acparse.py`)

A criterion is proved by a test that **names its id** and passed. The id may be spelled `AC-F2-1`, `ac_f2_1`, `AC F2 1`, `TestAC_F2_1` (separators required, so `AC-F2-1` never matches `AC-F2-10`). Three sources are read, in this order of preference:

1. a JSON object on the output: `{"results": {"AC-F2-1": "pass"}}` or `{"results": {"F2": {"AC-F2-1": "pass"}}}` (the shape of `examples/hello-loop/acceptance/run_checks.py --json`). **Only the exact words pass, passed, ok, green, success count as a pass**; any other text is a failure;
2. a JUnit XML file named by `ext.g3.junit`;
3. verbose runner lines: unittest `-v`, pytest `-v` / `-rA`, go `-v`, TAP, cargo test, jest and vitest verbose.

Verdicts: `pass`; `fail` (an assertion failed, or a raising stub: RED); `error` (an import, collection, syntax, attribute or signature error: INVALID, the test never ran); `skipped` (never a pass). Several tests naming one criterion: any error beats any fail beats a pass.

**Limit.** Text parsing can be spoofed by code that prints a runner-looking line. A spoofed pass never hides a failing test (fail beats pass), it cannot create a test file (G1 needs the id in a pinned test file), and the Evidence gate wants the CI check run on the merge candidate. Prefer JSON or JUnit.

## G2 and G3 in detail

**Stubs first, red against them.** G2 runs at the stubbing stage. Against the stubs every criterion must be RED: a `NotImplementedError` or an assertion failure. An `ImportError`, a missing function or a wrong signature is invalid; a criterion that passes against a stub proves nothing; one that did not run is not red. G2 then writes `<git-common-dir>/loopctl/pins/<item>-a<n>.json` (the blob ids of the visible tests, and of every test, config and scaffolding file the acceptance-author committed so far) and adds the visible globs to `locked_paths.json` (source `g3`), which the file guard enforces (`engineer-locked`). From then on a changed, deleted or newly added file under those globs is `g3.pinned-edited` / `g5.pinned-test-edited` with `reward_hack: true`, wherever the change came from (a commit, `git checkout`, an uncommitted edit). Without a pin (the tests rode in the plan PR) the base sha is the reference.

**Characterization** (`ext.g3.class: characterization`). A refactor has no stubs. G2 requires the criteria GREEN on the untouched base, G3 requires them green after, and both flag the item for a human to check that behaviour is preserved.

**Held-out tests (RC-6).** Under `runner: driver` they live in `<git-common-dir>/loopctl/heldout/<item>/`, outside the checkout, with `denyRead` in the child's sandbox (4c). `heldout_cmd` runs in the worktree with `LOOPCTL_HELDOUT_DIR`, `LOOPCTL_ITEM` and `LOOPCTL_BASE`. If the plan says `ext.g3.heldout: true` and the directory is empty, G3 FAILS (class `env`): a driver run without its held-out tests must not look like a pass. Under `runner: sequential` no per-subagent sandbox is documented (P42), so held-out tests are CI-only: `heldout.yml` restores them from a cache that the trusted `heldout-sync.yml` fills from a private store, runs `python3 -m loopctl.gates._ci heldout` (kit, config and plan from the base sha) and prints criterion ids and counts only. `autonomy_level: L2` or higher with neither the driver nor `g3_ci_only` in `platform_attested` fails G3 for an item that declares held-out tests (`g3.heldout-isolation`).

## G5, G9, G11 in detail

**G5 glob check.** Matched at any depth: test directories and files (`tests/`, `__tests__/`, `*.test.*`, `test_*.py`, `*_test.go`, ...), `conftest.py`, pytest, jest, vitest, playwright, mocha and karma configuration, `pyproject.toml` (only its `[tool.pytest|coverage|ruff|mypy|...]` tables), `package.json` (only `scripts`, `jest`, `vitest`, `mocha`, hooks), `tsconfig*.json`, `Makefile`, snapshot directories and `*.snap`, `.env*`, `.envrc`, CI workflows, lint configuration. A change is expected, and only noted, when it is a test file inside the item's `touches.paths` or matches `ext.g5.allow`; everything else is `g5.protected-path`; a deleted test is always `g5.test-deleted`.

**G5 AST lint (OPT-IN, experimental).** `g5_ast_lint: [python, javascript]`; refuses until the evidence gate has passed. Python findings are errors (stdlib `ast`); JavaScript findings are warnings tagged `confidence: low` (regex heuristics).

**G9.** Reads `package.json`, `requirements*.txt`, `pyproject.toml` (PEP 621 and Poetry), `go.mod`, `Cargo.toml` and `Gemfile` at the base and at HEAD. The kit never reaches a registry: existence, age, downloads and licence come from your `dep_check_cmd`; without it new dependencies are listed UNCHECKED for the human. The look-alike check uses a bundled list of about 80 popular names per ecosystem and an edit distance of 1 (2 for long names): a hit is an error unless the dependency is verified old and widely used, or the plan allows it.

**G11.** The built-in scan uses the patterns of `kitlib/redact.py`, reads every commit's added lines (a secret added and removed again inside the branch is still in the history), commit messages, uncommitted and untracked files, the item's reports and verify logs. A finding names the kind and `path (commit)` and never the value. Event files are redacted at write; a hit there is a warning.

## G6 in detail

`verify.py` runs the base sha's definition (`verify_build`, `verify_lint`, `verify_typecheck`, the item's `verify.tests`, `verify_tests`, the smoke stage) and G6 adds the checks above. `migrate_check_cmd` receives `LOOPCTL_ITEM`, `LOOPCTL_MIGRATION`, `LOOPCTL_MIGRATION_UP`, `LOOPCTL_MIGRATION_DOWN` (empty for a lossy migration: its down is never run) and `LOOPCTL_MIGRATION_CLASS`; it must print `dialect: <engine>` and G6 compares that with `db_local` (SQLite only when production is SQLite, DEC-8). No such database is `g6.no-prod-dialect-db` (class `env`), which the doctor maps to the L1 cap (RC-21).

## G7 in detail

The conductor (or the driver's review child) saves the reviewers' output verbatim and commits it:

```
docs/features/<slug>/reports/code-review.md        required       CODE_REVIEW_APPROVED | CHANGES_REQUESTED
docs/features/<slug>/reports/security-review.md    when required  SECURITY_APPROVED | SECURITY_BLOCKED
docs/features/<slug>/reports/design-review.md      optional       DESIGN_APPROVED | DESIGN_NEEDS_REVISION
```

G7 reads them from git at HEAD. A security review is required when `security_review` is `every_item`, or `sensitive` and the item is (`ext.sensitive`, risk class high, or a footprint under an auth, payment, billing, upload, session, login, password, crypto or permission path). A review is **stale** when a commit that changes anything outside the reports directory came after the commit that last touched the review file.

## The CI halves

`python3 -m loopctl.gates._ci heldout|mutation --repo DIR --base SHA [--head-ref REF] [--dir DIR]` is what `heldout.yml` and `mutation.yml` run, with `PYTHONPATH` set to the kit extracted from the BASE sha. The item is picked from the branch name (`feat/<slug>-a<n>`, matched strictly); a pull request that is not an item branch has nothing to run. `mutation` reads `gates.g4` and `docs/baseline/evidence.json` at the base, so a pull request can neither switch the gate on nor supply its own evidence.

## Config keys (owner `4b-gates`, all H6)

`gates.g4` (explicit key, default `off`, evidence-gated for `on` and `report`), `g5_ast_lint` (evidence-gated), `heldout_cmd`, `mutation_cmd`, `mutation_min`, `mutation_timeout_s`, `env_check_cmd`, `migrate_check_cmd`, `dep_check_cmd`, `license_allowlist`, `secrets_scan_cmd`. The per-gate toggles `gates.g0` ... `gates.evidence` are the foundation's `gates.*` family.
