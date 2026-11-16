# Conductor: parallel test runner

## Changes
- `tools/run_tests.py` runs each test module in its own `python3 -m unittest` process, in parallel (one job per CPU by default). `tools/test.sh` with no arguments now calls it; with arguments it runs `unittest discover` serially as before. The whole suite went from about 51 s to about 14 s after Phase 1 merged, keeping the IMP-24 "under 60 s" budget reachable for later phases.

## Upgrade impact
- None for kit users. Contributors get faster local runs; `tools/test.sh -v ...` behaves as before.

## New config keys
- None.

## Pending-human actions
- None.

## Docs impact
- README Development section (release stage) should mention `tools/run_tests.py --slowest N`.

## Decisions made
- Shard by module, not by test, so each module keeps its own interpreter and fixtures cannot leak between modules. Exit code 5 ("no tests ran", Python 3.12+) counts as a pass only when the module ran zero tests.

## Conductor: pointers to add
- README Development section: mention the parallel runner.
