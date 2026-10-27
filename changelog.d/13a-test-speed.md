# 13a-test-speed: the whole suite in about 40 s, and waits that survive a loaded machine

## Changes
- **Why it was slow.** The suite is bound by process creation, not by Python. Every `git` costs about 4 ms of CPU on a Mac and the
  machine starts roughly 600 programs a second however many cores it has (measured: 1 worker 160/s, 10 workers 580/s), so wall time
  is the number of processes divided by that rate. The suite started 35,000 processes; 25,000 of them were kit code asking git a
  read-only question (loopctl does ~8 per CLI call, ~5 per state write). Running modules in parallel could not help any more.
- **Now ~14,000 processes.** Wall time 75-80 s -> 37-50 s and summed module time 660-800 s -> 380-500 s on the merged tree under
  the same background load (quiet machine: about 37 s and 380 s; see "Decisions made" for the numbers).
- `tests/helpers/gitfast.py` (new): `kitlib.gitutil.run_git` is wrapped, while a test asks for it, so that `rev-parse`, `symbolic-ref`,
  `show-ref`, `for-each-ref`, `merge-base [--is-ancestor]`, `ls-tree`, `cat-file --batch` and `show REV:PATH` are answered by reading the
  repository's ref files and loose objects, and `hash-object -w`, `mktree` and `commit-tree` (loopctl's state commit) write the loose
  object themselves. It returns nothing for anything else or anything unusual (packed objects, worktrees, unusual config, a `GIT_*`
  variable that could change the answer) and the real git runs, so a wrong guess can only cost speed. Refs are never written by it
  (`update-ref`, `commit`, `merge` still run git). Switched on by `LoopRepo` (all loopctl and gate tests), `FeatureCase`
  (pr_prepare), `VerifyCase` and the guards corpus; `BaseTestCase.fast_git()` opts a test in. `TESTS_GITFAST=0` turns it off,
  `TESTS_GITFAST=verify` runs git as well and fails on any difference (it passed on the whole suite).
- `tests/test_gitfast.py` (new, 8 tests): a fixture repo with merges, annotated and light tags, a symbolic remote HEAD, look-alike
  directories, binary/empty/executable/symlink files and a root commit is asked ~290 questions in a loose, a ref-packed and a gc'd
  layout; every answer must equal git's or be declined. It also checks the bytes of what the shim writes against git's (same object ids
  for the same input and dates, several time zones).
- Fixtures are built once per process and copied (`shutil.copytree`): `TempRepo` (init + first commit), `LoopRepo` (committed plan and
  config, keyed by items/config), `gitrepo.cached_repo(key, build)` for a test file's own starting repo (used by `VerifyCase`).
  `LoopRepo.snapshot()` / `LoopRepo.restore()` copy a driven fixture (a merged chain, a half-run) to give each test its own. Sample hooks
  are not copied; `maintenance.auto` and `gc.auto` are off in the copy's config (a commit would start a second git process).
  `TempRepo.head()` reads the repo files (no process).
- `tests/helpers/inproc.py` (new): `run_script(path, args, env, cwd, stdin)` runs a script as `__main__` in this process and returns a
  `CompletedProcess`. Used for the CLI tests whose subject is what a script prints for given arguments: bootstrap_config (89 calls),
  doctor (`run_doctor`, 40), baseline summarize/observe, infra gate/source_policy, pr_prepare, repo_script. One real `python3 script.py`
  test per CLI is kept (`test_runs_as_a_script...`, `real=True`).
- **Waits** (flaky under load; `test_children` failed 3 tests at load 14): `procs.wait_for` never waits less than 30 s (it returns the
  moment the condition holds, so the happy path is unchanged); the 3-5 s upper bounds on "was killed promptly" are 30 s (far below the
  60 s the child would sleep); stall and canary limits that raced a child's start-up (children could be killed before they wrote their
  pid file) are 3-4 s where the test needs the pid, and the driver tests' default `stall_s` is 6 s; `verify` tests poll with `wait_for`
  instead of fixed loops; the fake claude installs its SIGTERM ignore before it writes the pid file (the file now means "ready").
  Fixed spots: `test_children` (Stall x2, tool-events, quiet-after-events, no-stall-without-probe, canary wait, 6 `read_pid` waits),
  `test_faults_process`, `test_faults_state`, `test_faults_lock`, `test_faults_crash`, `test_adversarial_driver`, `unattended/test_stalls`,
  `unattended/test_notify`, `verify/test_verify` (5 polling loops, 3 elapsed-time bounds), `doctor/test_adversarial`,
  `bootstrap/test_adversarial` and `kitlib/test_redact` (CPU-time bounds), `test_helpers` (SIGTERM race in the fake).
- Tests added: 8 (`test_gitfast`) and 2 real-process tests (`test_runs_as_a_script` in bootstrap_config and infra gate); source_policy and
  pr_prepare keep one existing test on `real=True`. Tests removed or merged: none.

## Upgrade impact
None. Tests and test helpers only; no production file changed.

## New config keys
None.

## Pending-human actions
None.

## Docs impact
None for the prose files. The conductor may point `docs/design/v2-implementation-decisions.md` IMP-24 at the guidance below.

## Decisions made
- **Test-writing guidance for every later phase (read before adding tests):**
  1. Count processes, not seconds. A `git`, `python3` or `sh` start is the cost; a few hundred dict operations are free. Check a new
     module with `PROF_LOG`-style counting or just `python3 tools/run_tests.py --slowest 15` and keep it under ~5 s alone.
  2. Build a repo once. `TempRepo()` and `LoopRepo(...)` already copy a per-process template; for your own starting point use
     `self.cached_repo(key, build)` (or `LoopRepo.snapshot()/restore()` for a driven fixture) rather than init/add/commit in `setUp`.
  3. Call CLIs in-process: `loopctl.cli.main` through `LoopRepo.ctl()`, other scripts through `tests.helpers.inproc.run_script`.
     Keep one real `subprocess` call per CLI for the process boundary (exit status, PATH lookup, signals, stdin pipe).
  4. Do not shell out to git for a read you can make through kit code (`gitutil`): inside `LoopRepo`/`FeatureCase`/`VerifyCase` it is
     answered without a process. Call `self.fast_git()` in other test classes that go through `gitutil.run_git`. Do not turn it on for
     a test whose subject is git itself.
  5. Waits: use `procs.wait_for(cond)` (never shorter than 30 s, returns at once). Never `time.sleep(n)` and hope; never a bound
     tighter than 30 s on "finished within N seconds" unless N is the thing under test. A stall/timeout knob that races a child's
     start-up (python takes seconds to start on a loaded machine) must be longer than that start-up whenever the test needs the child
     to have run: the fake claude's pid file means "ready".
  6. No fast/full tier split. The budget is for everything.
  7. If you change `gitfast.py`, run `tests.test_gitfast` and the suite with `TESTS_GITFAST=verify`; add the new shape to
     `Fixture.cases()` there.
- **Numbers.** Baseline = v2/integration (2279 tests, Phase 7 merged), this branch = 2289 tests. Same machine, 10 jobs. At background
  load ~10-14: baseline wall 75.6 s and 79.9 s, summed module time 662 s and 798 s; this branch wall 50.8 s and 50.4 s, sum 497 s and
  504 s. On a quiet machine this branch ran in 36.9 s and 38.0 s (sum 380 s); the unmerged baseline was 60.9-64.5 s (sum 608-645 s).
  Child CPU of the suite (`user+sys`, load independent, 6 jobs): 366 s -> 268 s before the later cuts. Processes started: 35,300 ->
  13,900.
- The shim lives in `tests/` and wraps the one function all kit git calls go through, instead of changing loopctl to batch its git
  calls. That would speed up production too but changes the code of phases already merged and of Phase 7; left as a possible follow-up.
- `TESTS_GITFAST` is not a `KIT_*` variable on purpose: several tests assert that no `KIT_*` variable leaks into a child.
- `wait_for` has a 30 s floor instead of editing 20 call sites: one place, and no caller can regress it.

## Conductor: pointers to add
- IMP-24 / `docs/design/v2-implementation-decisions.md`: add "see changelog.d/13a-test-speed.md, Test-writing guidance" next to the
  `< 60 s` budget. `builder-rules.md` test-budget lines should say: "build repos with templates/snapshots, call CLIs in-process, read the
  guidance in the 13a fragment".
