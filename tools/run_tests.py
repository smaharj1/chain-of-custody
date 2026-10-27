#!/usr/bin/env python3
"""Run the kit test suite with one unittest process per test module, in parallel (IMP-4, IMP-24).

The suite is mostly subprocess-bound (temp git repos, fake claude, shell wrappers), so modules
run concurrently. Each module runs in its own interpreter, exactly as `python3 -m unittest
<module>` would, so modules cannot leak state into each other. Output of failing modules is
printed in full; passing modules print nothing.

Modules start longest-first, using the durations of the previous run (kept in the git common dir, never tracked),
so one slow module does not start last and set the wall time.

Usage: tools/run_tests.py [-j N]   (tools/test.sh calls this when given no arguments)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)


def discover_modules(root: str) -> list:
    modules = []
    tests_dir = os.path.join(root, "tests")
    for dirpath, dirnames, filenames in os.walk(tests_dir):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not d.startswith("."))
        if not os.path.exists(os.path.join(dirpath, "__init__.py")):
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if name.startswith("test") and name.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, name[:-3]), root)
                modules.append(rel.replace(os.sep, "."))
    return modules


def timings_path(root: str) -> str:
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
    except OSError:
        return ""
    return os.path.join(root, common, "kit-test-timings.json") if common else ""


def load_timings(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_timings(path: str, results: list) -> None:
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({r[0]: round(r[4], 2) for r in results}, f, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def run_module(module: str) -> tuple:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    match = RAN_RE.search(proc.stdout)
    count = int(match.group(1)) if match else 0
    # Exit 5 is "no tests ran" (Python 3.12+); an empty module is not a failure.
    ok = proc.returncode == 0 or (proc.returncode == 5 and count == 0)
    return module, ok, count, proc.stdout, time.monotonic() - start


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--slowest", type=int, default=0, help="print the N slowest modules")
    args = parser.parse_args(argv)

    modules = discover_modules(REPO_ROOT)
    if not modules:
        print("no test modules found under tests/", file=sys.stderr)
        return 1
    tpath = timings_path(REPO_ROOT)
    previous = load_timings(tpath)
    # Unknown (new) modules first: they may be slow, and running them early costs nothing.
    order = sorted(modules, key=lambda m: -previous.get(m, float("inf")))
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = sorted(pool.map(run_module, order))
    elapsed = time.monotonic() - start
    save_timings(tpath, results)

    failed = [r for r in results if not r[1]]
    for module, _, _, output, _ in failed:
        print("=" * 70)
        print("FAILED MODULE: %s" % module)
        print(output.rstrip())
    if args.slowest:
        for module, _, _, _, secs in sorted(results, key=lambda r: -r[4])[: args.slowest]:
            print("%6.1fs  %s" % (secs, module))
    total = sum(r[2] for r in results)
    print("-" * 70)
    print("Ran %d tests in %.3fs (%d modules, %d jobs)" % (total, elapsed, len(modules), args.jobs))
    print()
    if failed:
        print("FAILED (%d module(s): %s)" % (len(failed), ", ".join(r[0] for r in failed)))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
