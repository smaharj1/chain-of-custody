"""The verify-run harness: boot the app, wait for it, assert one route, shut it down.

    python3 scripts/verify_run.py [ROUTE] [--status 200] [--method GET]

ROUTE defaults to /healthz; pass the item's demo target (for example /api/bookmarks or /).
The exit code is 0 only when the route answers with the expected status. The database is
DATABASE_URL when set (the per-item ephemeral copy), otherwise a throwaway file. Pending
migrations are applied to it, and the sample data is loaded when the seed module exists.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("route", nargs="?", default="/healthz")
    ap.add_argument("--status", type=int, default=200)
    ap.add_argument("--method", default="GET")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    tmpdir = tempfile.mkdtemp(prefix="hello-loop-verify-")
    env = dict(os.environ)
    if "DATABASE_URL" not in env:
        env["DATABASE_URL"] = "file:" + os.path.join(tmpdir, "verify.db")
    port = free_port()
    env["PORT"] = str(port)
    proc = None
    try:
        for module in ("hello_loop.migrate", "hello_loop.seed"):
            if module == "hello_loop.seed" and not os.path.isfile(os.path.join(ROOT, "hello_loop", "seed.py")):
                continue
            rc = subprocess.call([sys.executable, "-m", module], cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL)
            if rc != 0:
                print("verify_run: %s failed (exit %d)" % (module, rc), file=sys.stderr)
                return 1
        proc = subprocess.Popen([sys.executable, "-m", "hello_loop.server"], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        base = "http://127.0.0.1:%d" % port
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                print("verify_run: server exited early (code %s)" % proc.returncode, file=sys.stderr)
                return 1
            try:
                urllib.request.urlopen(base + "/healthz", timeout=2).close()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            print("verify_run: server did not answer /healthz within %.0fs" % args.timeout, file=sys.stderr)
            return 1
        req = urllib.request.Request(base + args.route, method=args.method,
                                     data=b"" if args.method == "POST" else None)
        try:
            status = urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as err:
            status = err.code
        print("%s %s -> %d (want %d)" % (args.method, args.route, status, args.status))
        return 0 if status == args.status else 1
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
