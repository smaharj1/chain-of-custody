#!/usr/bin/env python3
"""probe_run: the sandboxed first-run probe (Phase 1; report §16.1 Phase 1, CO-14; IMP-25).

`/planner` Phase 2b and `primer-protocol.md` tell the agent to run an existing repo's
execution-critical commands once, to prove them before writing them down. On a repo the user
has not vetted those commands run unknown code. This tool is the sandbox for that:

  * With no `--go` it only prints the command list and a short digest of it. That is the dry run
    and the default. The human reads the list and says "go".
  * `--go <digest>` runs the commands, but only if the digest matches the list as it is now, so
    what runs is exactly what the human saw. The commands run in `docker run --network none` with
    all capabilities dropped, a read-only copy of the repo, no credentials and no environment
    passed through. If docker is missing it prints the commands for the human to run in a sandbox
    of their own and exits 3. It never runs anything on the host.

Commands come from loop.config (verify_tests, verify_run, db_ephemeral) and from fenced command blocks
under a few headings of `.claude/context/local-dev.md`, plus any `--cmd`. A server command
(verify_run, "Run the App Locally") is expected to keep running: reaching the timeout counts as started.

Exit codes: 0 plan printed, or every command passed; 1 a command failed; 2 usage or digest mismatch;
3 docker is unavailable (commands printed for the human).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doctorlib import safeio  # noqa: E402
from kitlib import config as config_mod  # noqa: E402
from kitlib import redact  # noqa: E402

CONFIG_KEYS = (("verify_tests", "run"), ("db_ephemeral", "run"), ("verify_run", "server"))
LOCAL_DEV_SECTIONS = (("Run the App Locally", "server"), ("Apply Migrations to the Durable Dev DB", "run"),
                      ("Seed Data", "run"), ("Reset the Local DB", "run"))


def _fenced_commands(section_text: str) -> List[str]:
    out = []  # type: List[str]
    in_fence = False
    for raw in section_text.split("\n"):
        line = raw.strip()
        if line.startswith("<!--"):
            line = line[4:].strip()
            if line.endswith("-->"):
                line = line[:-3].strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or not line or line.startswith("#"):
            continue
        cmd = re.sub(r"\s+#\s.*$", "", line).strip()
        if cmd and "<" not in cmd.split()[0]:
            out.append(cmd)
    return out


def commands_from_local_dev(text: str) -> List[Tuple[str, str, str]]:
    out = []  # type: List[Tuple[str, str, str]]
    sections = re.split(r"(?m)^##\s+", text)
    for sec in sections:
        title, _, body = sec.partition("\n")
        for want, kind in LOCAL_DEV_SECTIONS:
            if title.strip() == want:
                # template placeholders live in HTML comments; a filled-in file has real fences
                if "<!--" in body and "```" in body and not re.search(r"(?m)^```", body):
                    continue
                for c in _fenced_commands(body):
                    out.append(("local-dev.md: " + want, kind, c))
    return out


def gather_commands(repo: str, extra: Sequence[str], use_config: bool = True) -> List[Dict[str, str]]:
    cmds = []  # type: List[Dict[str, str]]
    seen = set()  # type: set

    def add(source: str, kind: str, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd or cmd == config_mod.PLACEHOLDER or cmd in seen:
            return
        seen.add(cmd)
        cmds.append({"source": source, "kind": kind, "command": cmd})
    if use_config:
        text = safeio.read_text(os.path.join(repo, config_mod.CONFIG_PATH), 1 << 20)
        if text is not None:
            try:
                cfg = config_mod.load_text(text)
                for key, kind in CONFIG_KEYS:
                    v = cfg.get(key)
                    if isinstance(v, str):
                        add("loop.config: " + key, kind, v)
            except config_mod.ConfigError:
                pass
        ld = safeio.read_text(os.path.join(repo, ".claude", "context", "local-dev.md"), 1 << 20)
        if ld is not None:
            for src, kind, c in commands_from_local_dev(ld):
                add(src, kind, c)
    for c in extra:
        add("--cmd", "run", c)
    return cmds


def digest(cmds: Sequence[Dict[str, str]]) -> str:
    """Short hash of the command list (kinds and text, in order). The human's "go" names this."""
    h = hashlib.sha256()
    for c in cmds:
        h.update(("%s\t%s\n" % (c["kind"], c["command"])).encode("utf-8"))
    return h.hexdigest()[:12]


def docker_argv(docker: str, name: str, repo: str, image: str, command: str, uid: Optional[int] = None, gid: Optional[int] = None) -> List[str]:
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else (uid or 1000)
    gid = os.getgid() if gid is None and hasattr(os, "getgid") else (gid or 1000)
    return [docker, "run", "--rm", "--name", name, "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "512", "--memory", "2g", "--user", "%d:%d" % (uid, gid),
            "--tmpfs", "/work:rw,exec,size=2g", "--tmpfs", "/tmp:rw,exec,size=512m",
            "--mount", "type=bind,source=%s,target=/src,readonly" % os.path.abspath(repo),
            "-e", "HOME=/tmp", "-w", "/work", image, "sh", "-c", "cp -a /src/. /work/ && " + command]


def sandbox_env() -> Dict[str, str]:
    """The docker client's own environment: PATH only. Nothing credential-bearing is passed on."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}


def run_one(docker: str, repo: str, image: str, c: Dict[str, str], index: int, timeout: float, tag: str,
            run=subprocess.run) -> Dict[str, Any]:
    name = "kit-probe-%s-%d" % (tag, index)
    argv = docker_argv(docker, name, repo, image, c["command"])
    try:
        proc = run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, shell=False, env=sandbox_env())
    except subprocess.TimeoutExpired as exc:
        try:
            run([docker, "kill", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, shell=False, env=sandbox_env())
        except (OSError, subprocess.TimeoutExpired):
            pass
        tail = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, (bytes, bytearray)) else ""
        ok = c["kind"] == "server"
        return {"command": c["command"], "kind": c["kind"], "ok": ok, "exit": None,
                "result": "still running at the %ds timeout (started)" % timeout if ok else "timed out after %ds" % timeout,
                "tail": redact.redact("\n".join(tail.strip().split("\n")[-15:]))}
    except OSError as exc:
        return {"command": c["command"], "kind": c["kind"], "ok": False, "exit": None, "result": "could not start docker: %s" % exc, "tail": ""}
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    ok = proc.returncode == 0
    return {"command": c["command"], "kind": c["kind"], "ok": ok, "exit": proc.returncode,
            "result": "exit 0" if ok else ("exited %d%s" % (proc.returncode, " (a server should keep running)" if c["kind"] == "server" else "")),
            "tail": redact.redact("\n".join(out.strip().split("\n")[-15:]))}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="probe_run.py", description="Sandboxed first-run probe for an unvetted repo's commands (CO-14, IMP-25).")
    ap.add_argument("--repo", default=".", help="repository root (default: current directory)")
    ap.add_argument("--cmd", action="append", default=[], help="an extra command to probe; repeatable")
    ap.add_argument("--no-config", action="store_true", help="do not read loop.config and local-dev.md; only --cmd")
    ap.add_argument("--image", default="", help="container image that has the repo's toolchain (required with --go)")
    ap.add_argument("--go", default=None, metavar="DIGEST", help="run the commands; DIGEST must match the list printed by the dry run")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds per command (default 300)")
    ap.add_argument("--docker", default="docker", help="docker executable (default: docker)")
    ap.add_argument("--json", action="store_true", help="print the plan or the results as JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None, run=subprocess.run) -> int:
    args = parser().parse_args(argv)
    repo = os.path.abspath(args.repo)
    if any(ch in repo for ch in ",\n\r"):
        sys.stderr.write("probe_run: refusing: the repository path contains a comma or a newline, which would corrupt the docker mount option\n")
        return 2
    cmds = gather_commands(repo, args.cmd, use_config=not args.no_config)
    dg = digest(cmds)
    plan = {"schema": "probe_run.v1", "repo": repo, "image": args.image or None, "network": "none", "digest": dg, "commands": cmds}
    if not cmds:
        print("probe_run: nothing to probe (no commands in loop.config or local-dev.md, and no --cmd).")
        return 0
    if args.go is None:
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print("probe_run: DRY RUN. Nothing was executed. The commands below would run once, inside a container with no network:\n")
            for i, c in enumerate(cmds, 1):
                print("  %d. [%s] %s" % (i, c["kind"], c["command"]))
                print("       from %s" % c["source"])
            print("\nContainer: docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges, repo bind-mounted read-only at /src, copied to a tmpfs.")
            print("Read the list. If it is safe to run, say go, then run:\n")
            print("  python3 .claude/scripts/probe_run.py --repo %s --image <image with your toolchain> --go %s" % (repo, dg))
            print("\n(The digest changes whenever the list changes; an old go does not carry over.)")
        return 0
    if args.go != dg:
        sys.stderr.write("probe_run: refusing: the go digest %r does not match the current command list (%s). Run the dry run again and read the list.\n" % (args.go, dg))
        return 2
    if not args.image:
        sys.stderr.write("probe_run: --image is required with --go (an image that has your toolchain; there is no default)\n")
        return 2
    docker = shutil.which(args.docker, path=os.environ.get("PATH") or None) if os.sep not in args.docker else (args.docker if os.access(args.docker, os.X_OK) else None)
    if not docker:
        print("probe_run: docker is not available. Nothing was run. Run these yourself in a sandbox with the network off:\n")
        for c in cmds:
            print("  " + c["command"])
        return 3
    results = [run_one(docker, repo, args.image, c, i, args.timeout, dg, run) for i, c in enumerate(cmds, 1)]
    ok = all(r["ok"] for r in results)
    if args.json:
        print(json.dumps({"schema": "probe_run.v1", "digest": dg, "ok": ok, "results": results}, indent=2))
    else:
        for r in results:
            print("%s  [%s] %s: %s" % ("ok  " if r["ok"] else "FAIL", r["kind"], r["command"], r["result"]))
            if not r["ok"] and r["tail"]:
                for ln in r["tail"].split("\n"):
                    print("      | " + ln)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
