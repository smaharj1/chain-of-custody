# 0a-baseline: the V1 measurement kit (fixture, run harness, observation hook, summariser); runs pending

## Changes

- `examples/hello-loop/`: a runnable fixture on the Python standard library (`http.server`, `sqlite3`, `unittest`), SQLite as the declared production dialect, `.python-version` (3.12) and `pyproject.toml` (`requires-python >=3.9`, no dependencies).
  - A working starter codebase (migration runner, router, server, `/healthz` handler, smoke tests, `scripts/verify_run.py` verify-run harness, `scripts/ephemeral_db.py`).
  - `build-plan.json`: V1-shape plan, three items with real dependencies (F1 schema, F2 JSON API, F3 server-rendered page), two milestones, 16 acceptance criteria that carry ids (`AC-F2-4`) and name a command or route.
  - `kit-overlay/`: pre-filled `.claude/CLAUDE.md`, six primers, `loop.config.md` (the v1.2.0 template with only the three execution-critical commands filled), `remove.txt` (admin-app and infra primers and agents).
  - `reference-solution/F1..F3`: known-good implementation, one directory per item, never copied into a run.
  - `acceptance/run_checks.py`: held-out black-box checks, one per criterion, not copied into a run; counts escaped defects after a run.
  - `verify.json`: per item, the test command, the held-out command and the demo route.
  - `README.md` for the fixture.
- `docs/baseline/`: `README.md` (run protocol, baseline conditions and deltas, rescue taxonomy, failure classes, what each run records, the "1 run + 20 replays" alternative), `kill-criteria.md` (report §16.2), `RUNS.md` (three `PENDING` rows and the replay table), `results/TEMPLATE.json` and `results/schema.json` (`baseline-result.v1`), `evidence.template.json` and `evidence.schema.json`.
- `tools/baseline/run-baseline.sh` (thin wrapper) and `prepare.py`: clone tag `v1.2.0` into a directory outside the repo, check out `main`, drop `origin`, overlay the fixture minus its answer key, delete the `remove.txt` paths, install the observation hook and the sandbox setting, commit, write `<run>.baseline.json`, print the exact `claude` command with credential variables unset. Refuses (exit 3) when `CLAUDECODE` is set, when the tag is missing, when the destination is inside a git repo or not empty; exit 4 when `claude` is not on `PATH`. Never runs `claude`. `--dry-run` creates nothing. A failure part-way removes the half-built directory.
- `tools/baseline/observe.py`: logging-only hook. Reads the event from stdin, redacts with `kitlib.redact.redact_obj`, shows `$HOME` as `~`, cuts strings over 50,000 characters, appends one JSON line (O_APPEND), prints nothing, always exits 0. The Phase 7 probe harness reuses it.
- `tools/baseline/summarize.py`: validates `results/*.json` with a small built-in schema checker, prints ranges (min..max, median, n), evaluates the three kill criteria a baseline can decide, prints `PENDING (n=k/3)` and exits 0 with fewer than 3 runs. `--from-log` derives tool-call, dispatch, permission-request and restart counts from the hook log. Exit 1 only for an invalid results file.
- Tests (stdlib `unittest`, about 70 new, about 15 s): `tests/test_hello_loop.py` (plan shape and DAG, each item's verify commands pass when its reference solution is applied, every criterion fails one item earlier, overlay is a valid filled v1 config and has no sentinels, every v1.2.0 primer is overlaid or removed, Python 3.9 syntax), `tests/baseline/test_observe.py`, `test_summarize.py` (including the evidence template round trip through `kitlib.evidence`), `test_run_baseline.py`. Whole suite: 291 tests, about 25 s on Python 3.14, about 32 s on 3.9.6.

## Upgrade impact

None. These are new files for kit developers; nothing under `.claude/` changed, and nothing here is installed into a user's project. `examples/hello-loop/` is not part of an install.

## New config keys

None. The overlay `loop.config.md` uses only v1 keys (`autonomy: per-milestone`, `db_local: sqlite`, defaults elsewhere) and validates against the registry.

## Pending-human actions

- **Run V1 on the fixture three times.** `tools/baseline/run-baseline.sh run-1` (then `run-2`, `run-3`) from a normal terminal, follow `docs/baseline/README.md`, fill `docs/baseline/results/run-N.json`, update `docs/baseline/RUNS.md`, run `python3 tools/baseline/summarize.py`. An agent cannot start `claude`. Alternative: one run plus 20 replayed dispatches per agent type (README section "The alternative").
- **Confirm three unverified assumptions on the first run**, each a single constant in `tools/baseline/prepare.py`: the sandbox setting keys (`sandbox.enabled`, `sandbox.network.allowLocalBinding`), the 12 hook event names in `OBSERVED_EVENTS`, and that `claude "/orchestrate"` starts an interactive session with that first message. The symptom of a wrong one is an ignored setting or a hook log with fewer events than expected.
- **Push tag `v1.2.0`** if other machines will run the baseline (the harness reads the local tag; the tag already exists locally on `cb6218f`).
- **Publish the transcripts and scrubbed hook logs** with the results (report §15 item 1) and set `transcript_ref`; choose where.
- **Record the kill-criteria decisions** in `docs/baseline/RUNS.md` "Decisions" once the sample rule is met, and carry the loopctl verdict into the Phase 4 scope.
- **The evidence gate** (`docs/baseline/evidence.json`) stays absent by design; `docs/baseline/evidence.template.json` is the starting point when 3 clean v2 runs and 1 real feature exist.

## Docs impact

- `README.md`: in the Development section Foundations adds, one line: baseline tooling lives in `docs/baseline/` and `tools/baseline/`, and the fixture is `examples/hello-loop/`.
- `examples/README.md` (Phase 0b owns it): the closing "What's missing" section says the kit ships no runnable demo. Replace it with a short note that `examples/hello-loop/` is a real, runnable fixture that the kit is measured against, and that it is hand-written, not output of a `/planner` and `/orchestrate` run (so the "never published" gap still stands for loop output until the baseline transcripts exist).
- `GLOSSARY.md`: add "baseline run", "rescue" (control-flow vs other) and "fixture" with a pointer to `docs/baseline/README.md`.
- `docs/getting-started.md`, `docs/faq.md`, the SVGs: no change from this topic.
- `CHANGELOG.md` (release stage): one bullet for the measurement kit.

## Decisions made

- **The fixture is an existing codebase, not greenfield.** The starter has a runnable server, migration runner and passing smoke test, so there is no skeleton item and the primers can describe real files (the primer protocol says to derive from code). The three items are exactly schema, endpoint, UI. This is a delta from the "greenfield first item" rule and is listed in the README's deltas table.
- **Reference solution is staged per item** (`reference-solution/F1`, `F2`, `F3`, applied cumulatively), not one flat tree, so a test can prove each item's criteria pass with the item and fail without it.
- **Held-out `acceptance/run_checks.py` is not in the brief.** It gives the "defect escape" metric (§16.2) a definition and lets the test prove criteria are falsifiable without relying on engineer-written tests. It imports nothing from the project, so it works on any correct solution.
- **`verify.json` instead of an extra field in `build-plan.json`.** The plan stays strictly V1-shaped (the Orchestrator at v1.2.0 may not tolerate unknown fields); per-item verify commands live beside it.
- **`autonomy: per-milestone`, not `unattended`.** `unattended` is evidence-gated in the new kit, which would make the overlay unusable for later re-runs of hello-loop on v2 trees. Two milestones give two checkpoint approvals, recorded separately and not counted as rescues.
- **HTTP 400 for invalid input, everywhere**, and escaping, duplicate (409) and non-numeric id (404) pinned in the criteria. They are the brief-gap classes the report's evidence names (§3.2), so the fixture exercises them.
- **Python pin is 3.12 with `requires-python >=3.9`.** The code runs on 3.9 to 3.14 (the suite was run on 3.9.6 and 3.14); the pin is what the baseline declares, and a different local `python3` is recorded in `conditions.python3`.
- **Kill-criterion rules the report leaves open** (documented in `kill-criteria.md` and `summarize.py`): control-flow rescues "per run, taken across the runs" is the median per run, with min..max and a flag when the range straddles 3; "most failures are env" is more than half of recorded failures; "V1 cannot finish" fires when no run completed (some completed = "partial"), and a recorded `v1_cannot_finish` is reported immediately even before 3 runs; `aborted` runs are not evidence either way.
- **Sample rule for the alternative.** One non-aborted run plus at least 20 replayed dispatches for every agent type that run dispatched gives a provisional verdict, labelled so. Percentiles are nearest-rank.
- **The clone keeps the whole kit tree** (README, docs, examples) next to the fixture, because the brief says "clean clone at tag v1.2.0". Real installs have only `.claude/`; the difference is listed as a delta. `origin` is removed so nothing can be pushed.
- **Observation hook goes in the clone's tracked `.claude/settings.json`** (committed, tree stays clean) and the log lives outside the clone, next to it, so the run cannot touch it casually. Its command holds absolute paths to this checkout's `observe.py`; if the checkout moves, the hook silently stops logging. `summarize.py --from-log` shows an empty log plainly.
- **`run-baseline.sh` is a 20-line wrapper; the logic is in `prepare.py`** so it can be tested and parsed under 3.9. Both refuse on `CLAUDECODE`, so calling `prepare.py` directly is no bypass.
- **Results validation is a built-in subset checker**, not `jsonschema` (stdlib only). `results/schema.json` is a real draft-07 document that other tools can use; the checker supports exactly the keywords the file uses (`const`, `enum`, `type`, `required`, `properties`, `items`, `minimum`, `pattern`), and the rules it cannot express (which fields each `kind` needs) are in `semantic_errors`.
- **`evidence.template.json` is deliberately invalid** (placeholders, `clean: false`) and a test fills it in and proves `kitlib.evidence.check` then accepts it, as Foundations asked.

## Conductor: pointers to add

- `README.md`: Development section -> add "Baseline tooling: `docs/baseline/README.md`, `tools/baseline/`; fixture `examples/hello-loop/`".
- `examples/README.md`: "What's missing" section -> replace with the note described under Docs impact.
- `GLOSSARY.md`: new entries "baseline run", "rescue", "fixture" -> `docs/baseline/README.md`.
- `docs/v2-pending-human.md`: build from the "Pending-human actions" above.
- `.github/workflows/kit-tests.yml`: no change needed; `tools/test.sh` already discovers the new tests. If CI checks out without tags, the two `tests/test_hello_loop.py` tests that read the `v1.2.0` tag skip themselves.
