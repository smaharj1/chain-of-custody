#!/usr/bin/env python3
"""verify: the kit-owned G6 definition, standalone (Phase 3; report §9 G6, §19.7 L-27; RC-7, CO-11).

It runs, in order and each in its own process group:

    build, lint, typecheck     optional (verify_build / verify_lint / verify_typecheck)
    item-tests                 the item's own targeted test command (items file), if any
    tests                      verify_tests: the whole suite; must exit 0 AND collect more than 0 tests
    smoke                      verify_dev (boot, poll, request, kill) or verify_run (a harness that exits)

What it guarantees:

  * The definition is read from the BASE sha, never from the head. The commands come from
    `.claude/loop.config.md` as committed at the base, and the per-item commands from an items file read
    at the base too, so a change set cannot weaken its own gate (CO-11). `--allow-head-config` is the
    local-only escape hatch for a project that has not committed its config yet; the result then says
    `config_source.kind == "head"` and `trusted == false`.
  * A required stage with no command is a definition error (exit 2), never a silent pass.
  * Every stage runs under an allowlisted environment (`env -i` plus PATH, HOME, locale and toolchain
    locators), so a token in the calling shell never reaches test code.
  * Every stage runs in a new session and the whole group is killed afterwards, on success, failure and
    timeout. A process still alive after the stage's main command exited is a leak: it is killed and
    reported. Dedicated verify ports are checked free before the smoke stage and free again after it.
  * No per-AC parsing yet (loopctl's G1/G3 do that). Test and skip counts are read from common runner
    summaries (unittest, pytest, jest, vitest, mocha, cargo, go -v) when recognised.

Importable API (loopctl wraps these):

    load_definition(repo, base=None, item=None, items_file=None, routes=(), allow_head_config=False,
                    extra_env=None) -> Definition          raises DefinitionError
    run_verify(definition, keep_going=False, only=None, log_dir=None, attempt=1, write_log=True) -> dict
    verify(repo, ..., **load_and_run_options) -> dict       never raises for a bad definition; returns status "error"
    parse_test_counts(text) -> (collected or None, skipped or None)
    exit_code(result, apply_gate=False) -> 0 | 1 | 2

CLI:  python3 .claude/scripts/verify.py [--repo DIR] [--base REV] [--item ID] [--items-file PATH]
          [--route /path]... [--only build,lint,typecheck,item-tests,tests,smoke] [--keep-going]
          [--attempt N] [--env NAME=VALUE]... [--log-dir DIR | --no-log] [--apply-gate]
          [--allow-head-config] [--json]
Exit codes: 0 pass (or gate off), 1 a stage failed, 2 definition, usage or internal error.

JSON output, schema `verify.v1` (stable; loopctl and CI read it):

    {"schema": "verify.v1",
     "status": "pass" | "fail" | "error" | "skipped",     skipped = gates.g6 is off and --apply-gate was given
     "ok": bool,                                           true only for a complete pass (or skipped)
     "complete": bool,                                     false when --only ran a subset
     "gate": "on" | "report" | "off",                      gates.g6 at the base sha
     "blocking": bool,                                     gate == "on"
     "failure_class": "none" | "config" | "env" | "build" | "lint" | "typecheck" | "tests" | "smoke"
                      | "timeout" | "leak",                of the first failing stage
     "repo": str, "item": str | null, "base": sha | null, "head": sha | null, "dirty": bool,
     "config_source": {"kind": "base-sha" | "head", "sha": str | null, "path": str},
     "trusted": bool,                                      false when the head config was used
     "routes": [str], "ac_ids": [str],                     echoed from the items file; not evaluated here
     "stages": [{"name", "command", "status": "pass"|"fail"|"error"|"timeout"|"skipped",
                 "exit_code": int | null, "duration_s": float, "tests_collected": int | null,
                 "skipped_tests": int | null, "reason": str, "output_tail": str}],
     "findings": [{"id", "severity": "error" | "warn" | "info", "message"}],
     "log": path | null, "duration_s": float, "message": str}

Output is redacted (kitlib.redact) before it is printed or logged.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitlib import config as kconfig  # noqa: E402
from kitlib import gitutil, jsonio, redact, runstate  # noqa: E402

SCHEMA = "verify.v1"
STAGE_ORDER = ("build", "lint", "typecheck", "item-tests", "tests", "smoke")
REQUIRABLE = ("build", "lint", "typecheck", "tests", "smoke")
_CONFIG_KEY = {"build": "verify_build", "lint": "verify_lint", "typecheck": "verify_typecheck", "tests": "verify_tests"}
_CLASS = {"build": "build", "lint": "lint", "typecheck": "typecheck", "item-tests": "tests", "tests": "tests", "smoke": "smoke"}

# Variables passed to every stage. Toolchain locators only: nothing here carries a credential.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "TMPDIR", "TZ", "SHELL",
            "NVM_DIR", "NVM_BIN", "VOLTA_HOME", "PNPM_HOME", "COREPACK_HOME", "ASDF_DIR", "ASDF_DATA_DIR", "MISE_DATA_DIR",
            "PYENV_ROOT", "PYENV_VERSION", "VIRTUAL_ENV", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "JAVA_HOME",
            "DOTNET_ROOT", "SDKROOT", "DEVELOPER_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SECRET_NAME_RE = re.compile(r"(TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE|API_?KEY|ACCESS_?KEY|SESSION)|"
                             r"^(AWS|GH|GITHUB|AZURE|GOOGLE|GCP|ANTHROPIC|OPENAI|NPM|SSH|KUBE|DOCKER|TF|PULUMI|VAULT)_", re.I)
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~%/:@?&=+,#-]*\Z")
_TAIL_BYTES = 4000
_LOG_BYTES = 200000
_KILL_GRACE_S = 2.0
PORT_SETTLE_S = 2.0   # how long a port may stay bound after the smoke stage before it counts as leaked


class DefinitionError(Exception):
    """The verify definition cannot be used (missing config, unconfigured required stage, bad input)."""


# --------------------------------------------------------------------------------------------------
# definition
# --------------------------------------------------------------------------------------------------

class Definition(object):
    """Everything verify needs, resolved from the base sha. Plain attributes; `to_dict()` for the JSON."""

    def __init__(self) -> None:
        self.repo = ""  # type: str
        self.item = None  # type: Optional[str]
        self.base = None  # type: Optional[str]
        self.head = None  # type: Optional[str]
        self.dirty = False
        self.config_source = {"kind": "base-sha", "sha": None, "path": kconfig.CONFIG_PATH}  # type: Dict[str, Any]
        self.trusted = True
        self.commands = {}  # type: Dict[str, Optional[str]]
        self.dev = None  # type: Optional[str]
        self.run = None  # type: Optional[str]
        self.ports = []  # type: List[int]
        self.ready_path = "/"
        self.timeout_s = 600
        self.run_timeout_s = 120
        self.env_allow = []  # type: List[str]
        self.extra_env = {}  # type: Dict[str, str]
        self.require = ["tests"]  # type: List[str]
        self.routes = []  # type: List[str]
        self.ac_ids = []  # type: List[str]
        self.gate = "on"
        self.findings = []  # type: List[Dict[str, str]]

    def add_finding(self, fid: str, severity: str, message: str) -> None:
        self.findings.append({"id": fid, "severity": severity, "message": message})


def _is_unset(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip() or value.strip() == "<command>"


def _check_env_name(name: str, where: str) -> None:
    if not _NAME_RE.match(name):
        raise DefinitionError("%s: %r is not a valid environment variable name" % (where, name))
    if _SECRET_NAME_RE.search(name):
        raise DefinitionError("%s: %s looks like a credential variable; verify never passes credentials to test code" % (where, name))


_GIT_PINS = ["-c", "core.fsmonitor=false"]
# Files that define or run the gate. A change to one of them in the branch is a guardrail change (H6): flagged, not blocked.
_KIT_SURFACE = (".claude/scripts", ".claude/hooks", ".claude/settings.json", ".claude/settings.local.json", ".claude/loop.config.md",
                ".github/workflows")


def _is_clean(top: str) -> bool:
    """`git status` with fsmonitor off, so a hostile `.git/config` cannot run a program while we look."""
    proc = gitutil.run_git(_GIT_PINS + ["status", "--porcelain=v1", "--untracked-files=normal"], cwd=top, pin_hooks=True)
    return proc.returncode == 0 and not proc.stdout.strip()


def _kit_changes(top: str, base_sha: str) -> List[str]:
    proc = gitutil.run_git(_GIT_PINS + ["diff", "--name-only", "--no-ext-diff", "--no-textconv", "-z", base_sha, "--"] + list(_KIT_SURFACE),
                           cwd=top, pin_hooks=True)
    return [p for p in proc.stdout.split("\0") if p] if proc.returncode == 0 else []


def resolve_base(repo: str, base: Optional[str]) -> str:
    """The base sha: `--base` as given, else the merge-base of HEAD with the default branch."""
    if base:
        if base.startswith("-"):
            raise DefinitionError("--base may not start with '-'")
        sha = gitutil.rev_parse(base + "^{commit}", repo)
        if not sha:
            raise DefinitionError("--base %s does not resolve to a commit in this repository" % base)
        return sha
    for cand in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        tip = gitutil.rev_parse(cand + "^{commit}", repo)
        if not tip:
            continue
        proc = gitutil.run_git(["merge-base", "HEAD", tip], cwd=repo)
        out = proc.stdout.strip()
        if proc.returncode == 0 and out:
            return out.splitlines()[0]
    raise DefinitionError("cannot find the base: no origin/HEAD, main or master; pass --base <rev>")


def _read_items_spec(repo: str, base_sha: Optional[str], items_file: str, item: str) -> Dict[str, Any]:
    path = items_file
    text = None  # type: Optional[str]
    abs_path = path if os.path.isabs(path) else os.path.join(repo, path)
    real = os.path.realpath(abs_path)
    top = os.path.realpath(repo)
    inside = real == top or real.startswith(top + os.sep)
    if inside:
        rel = os.path.relpath(real, top).replace(os.sep, "/")
        if rel.startswith("-"):
            raise DefinitionError("--items-file may not start with '-'")
        if base_sha is None:
            raise DefinitionError("an items file inside the repository is read at the base sha, which is not available")
        text = gitutil.show_file(base_sha, rel, repo)
        if text is None:
            raise DefinitionError("%s does not exist at the base sha %s" % (rel, base_sha[:12]))
    else:
        try:
            with open(real, "r", encoding="utf-8") as fh:
                text = fh.read(1 << 20)
        except OSError as exc:
            raise DefinitionError("cannot read the items file: %s" % exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DefinitionError("the items file is not valid JSON: %s" % exc)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict) or item not in items or not isinstance(items[item], dict):
        raise DefinitionError("the items file has no entry for item %r" % item)
    spec = items[item]
    if "tests" in spec and spec["tests"] is not None and not isinstance(spec["tests"], str):
        raise DefinitionError("items.%s.tests must be a string" % item)
    if "demo_route" in spec and spec["demo_route"] is not None and not isinstance(spec["demo_route"], str):
        raise DefinitionError("items.%s.demo_route must be a string or null" % item)
    if "ac_ids" in spec and not (isinstance(spec["ac_ids"], list) and all(isinstance(a, str) for a in spec["ac_ids"])):
        raise DefinitionError("items.%s.ac_ids must be a list of strings" % item)
    return spec


def load_definition(repo: str, base: Optional[str] = None, item: Optional[str] = None, items_file: Optional[str] = None,
                    routes: Sequence[str] = (), allow_head_config: bool = False,
                    extra_env: Optional[Dict[str, str]] = None) -> Definition:
    """Resolve the verify definition at the base sha. Raises DefinitionError."""
    top = gitutil.toplevel(repo)
    if not top:
        raise DefinitionError("%s is not inside a git repository" % repo)
    d = Definition()
    d.repo = top
    if item is not None and not runstate.valid_id(item):
        raise DefinitionError("--item %r is not a valid item id" % item)
    d.item = item
    d.head = gitutil.rev_parse("HEAD", top)
    d.dirty = not _is_clean(top)

    cfg = None
    base_error = None  # type: Optional[str]
    try:
        d.base = resolve_base(top, base)
        cfg = kconfig.load_at_sha(top, d.base)
        d.config_source = {"kind": "base-sha", "sha": d.base, "path": kconfig.CONFIG_PATH}
    except DefinitionError as exc:
        base_error = str(exc)
    except kconfig.ConfigError as exc:
        base_error = str(exc)
    if cfg is None:
        if not allow_head_config:
            raise DefinitionError("%s (the verify definition is read from the base sha; --allow-head-config reads the working tree, "
                                  "for local use only)" % base_error)
        try:
            cfg = kconfig.load_file(os.path.join(top, kconfig.CONFIG_PATH))
        except (OSError, kconfig.ConfigError) as exc:
            raise DefinitionError("cannot read %s from the base sha (%s) or the working tree (%s)" % (kconfig.CONFIG_PATH, base_error, exc))
        d.config_source = {"kind": "head", "sha": None, "path": kconfig.CONFIG_PATH}
        d.trusted = False
        d.add_finding("verify.untrusted-config", "warn", "the definition was read from the working tree, not the base sha: "
                      "a change set can weaken its own gate this way; use only locally")
    if cfg.errors:
        raise DefinitionError("%s has errors: %s" % (kconfig.CONFIG_PATH, "; ".join(str(i) for i in cfg.errors[:5])))

    d.gate = cfg.gate("g6")
    for stage, key in _CONFIG_KEY.items():
        v = cfg.get(key)
        d.commands[stage] = None if _is_unset(v) else str(v).strip()
    v = cfg.get("verify_dev")
    d.dev = None if _is_unset(v) else str(v).strip()
    v = cfg.get("verify_run")
    d.run = None if _is_unset(v) else str(v).strip()
    d.commands["smoke"] = d.dev or d.run
    ports = cfg.get_list("verify_ports")
    if not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in ports):
        raise DefinitionError("verify_ports must be a list of port numbers")
    d.ports = [int(p) for p in ports]
    d.ready_path = cfg.get_str("verify_ready_path")
    if not _ROUTE_RE.match(d.ready_path):
        raise DefinitionError("verify_ready_path %r is not a path" % d.ready_path)
    d.timeout_s = cfg.get_int("verify_timeout_s")
    d.run_timeout_s = cfg.get_int("verify_run_timeout_s")
    if d.timeout_s <= 0 or d.run_timeout_s <= 0:
        raise DefinitionError("verify_timeout_s and verify_run_timeout_s must be positive")
    d.env_allow = [str(n) for n in cfg.get_list("verify_env_allow")]
    for n in d.env_allow:
        _check_env_name(n, "verify_env_allow")
    d.require = [str(r) for r in cfg.get_list("verify_require")]
    bad = [r for r in d.require if r not in REQUIRABLE]
    if bad:
        raise DefinitionError("verify_require names unknown stage(s) %s; valid: %s" % (", ".join(bad), ", ".join(REQUIRABLE)))
    if d.dev and not d.ports:
        raise DefinitionError("verify_dev needs verify_ports (the first port is where verify polls and sends requests)")

    for k, v in (extra_env or {}).items():
        _check_env_name(k, "--env")
        d.extra_env[k] = str(v)

    spec = {}  # type: Dict[str, Any]
    if item is not None and items_file:
        spec = _read_items_spec(top, d.base, items_file, item)
    elif items_file and item is None:
        d.add_finding("verify.items-file-ignored", "info", "--items-file was given without --item and is ignored")
    if spec.get("tests"):
        d.commands["item-tests"] = str(spec["tests"]).strip() or None
    d.ac_ids = list(spec.get("ac_ids") or [])
    all_routes = ([spec["demo_route"]] if spec.get("demo_route") else []) + list(routes)
    for r in all_routes:
        if not _ROUTE_RE.match(r):
            raise DefinitionError("route %r is not a plain URL path" % r)
    d.routes = all_routes

    if d.base:
        changed = _kit_changes(top, d.base)
        if changed:
            d.add_finding("verify.kit-modified", "warn",
                          "this branch changes files that define or run the gate (%s%s): the gate used the base sha's copy of the config; a human reviews "
                          "the change (H6)" % (", ".join(changed[:5]), ", ..." if len(changed) > 5 else ""))

    # Drift of the H6 keys between the base config and the working tree: informational, verify used the base.
    if d.config_source["kind"] == "base-sha":
        head_path = os.path.join(top, kconfig.CONFIG_PATH)
        if os.path.isfile(head_path):
            try:
                head_cfg = kconfig.load_file(head_path)
                changed = [n for n, _b, _h in kconfig.h6_changes(cfg, head_cfg)]
                if changed:
                    d.add_finding("verify.config-drift", "warn",
                                  "protected keys differ between the base sha and the working tree (%s); verify used the base values. "
                                  "A change to them needs a human (H6)" % ", ".join(changed[:8]))
            except (OSError, kconfig.ConfigError):
                d.add_finding("verify.config-drift", "warn", "%s in the working tree cannot be parsed" % kconfig.CONFIG_PATH)
    return d


def _missing_required(d: Definition, stages: Sequence[str]) -> List[str]:
    out = []
    for name in stages:
        if name in d.require and not d.commands.get(name):
            out.append(name)
    return out


# --------------------------------------------------------------------------------------------------
# process groups
# --------------------------------------------------------------------------------------------------

_LIVE = set()  # type: Set[int]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_group(pgid: int, grace: float = _KILL_GRACE_S, leader: Optional["subprocess.Popen"] = None) -> None:
    """TERM the group, wait up to `grace` seconds, then KILL what is left.

    Pass the group `leader` (a Popen) so it is reaped while we wait: an unreaped zombie leader keeps the group "alive"."""
    def alive() -> bool:
        if leader is not None:
            leader.poll()
        return _group_alive(pgid)

    _signal_group(pgid, signal.SIGTERM)
    end = time.time() + grace
    while time.time() < end and alive():
        time.sleep(0.02)
    if alive():
        _signal_group(pgid, signal.SIGKILL)
        end = time.time() + grace
        while time.time() < end and alive():
            time.sleep(0.02)


def _kill_all_live() -> None:
    for pgid in list(_LIVE):
        _kill_group(pgid, 0.5)


class _Terminated(BaseException):
    def __init__(self, signum: int) -> None:
        BaseException.__init__(self, "terminated by signal %d" % signum)
        self.signum = signum


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        _kill_all_live()
        raise _Terminated(signum)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, handler)
        except (ValueError, OSError):
            pass


def build_env(d: Definition) -> Dict[str, str]:
    env = {}  # type: Dict[str, str]
    for name in list(BASE_ENV) + d.env_allow:
        if name in os.environ:
            env[name] = os.environ[name]
    env.update(d.extra_env)
    env["KIT_VERIFY"] = "1"
    if d.ports:
        env["VERIFY_PORT"] = str(d.ports[0])
    return env


def _read_tail(path: str, nbytes: int) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
            data = fh.read(nbytes)
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


class _Proc(object):
    """A started shell command in its own session, with its output going to a temp file."""

    def __init__(self, command: str, cwd: str, env: Dict[str, str]) -> None:
        fd, self.out_path = tempfile.mkstemp(prefix="verify-", suffix=".out")
        self._out = os.fdopen(fd, "wb")
        try:
            self.proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=self._out, stderr=subprocess.STDOUT, start_new_session=True)
        except BaseException:
            self._out.close()
            self._discard()
            raise
        self.pgid = self.proc.pid
        _LIVE.add(self.pgid)
        self.leaked = False
        self.timed_out = False

    def wait(self, timeout: float) -> Optional[int]:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            _kill_group(self.pgid, leader=self.proc)
            try:
                return self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return None

    def finish(self) -> None:
        """Reap the group: whatever is still alive after the main command ended is a leak."""
        if self.proc.poll() is None:
            _kill_group(self.pgid, leader=self.proc)
        elif _group_alive(self.pgid):
            self.leaked = True
            _kill_group(self.pgid, leader=self.proc)
        _LIVE.discard(self.pgid)
        try:
            self._out.close()
        except OSError:
            pass

    def tail(self, nbytes: int = _TAIL_BYTES) -> str:
        return _read_tail(self.out_path, nbytes)

    def _discard(self) -> None:
        try:
            os.unlink(self.out_path)
        except OSError:
            pass

    def close(self) -> None:
        self.finish()
        self._discard()


# --------------------------------------------------------------------------------------------------
# test-output parsing (no per-AC parsing; counts only)
# --------------------------------------------------------------------------------------------------

def parse_test_counts(text: str) -> Tuple[Optional[int], Optional[int]]:
    """(tests collected, tests skipped) from a runner summary, or (None, None) if it is not recognised."""
    m = None
    # unittest
    ms = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    if ms:
        sk = re.findall(r"skipped=(\d+)", text)
        return int(ms[-1]), (int(sk[-1]) if sk else 0)
    if re.search(r"^NO TESTS RAN", text, re.M):
        return 0, 0
    # cargo
    cargo = re.findall(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text)
    if cargo:
        return sum(int(a) + int(b) + int(c) for a, b, c in cargo), sum(int(c) for _a, _b, c in cargo)
    # jest
    m = None
    for m in re.finditer(r"^Tests:\s+(.*)$", text, re.M):
        pass
    if m:
        counts = dict((k, int(v)) for v, k in re.findall(r"(\d+) (failed|skipped|todo|passed|total)", m.group(1)))
        if "total" in counts:
            return counts["total"], counts.get("skipped", 0) + counts.get("todo", 0)
    # vitest
    m = None
    for m in re.finditer(r"^\s*Tests\s+(.*?)\((\d+)\)\s*$", text, re.M):
        pass
    if m:
        sk = re.search(r"(\d+) skipped", m.group(1))
        return int(m.group(2)), int(sk.group(1)) if sk else 0
    # pytest
    if re.search(r"^\W*no tests ran", text, re.M | re.I):
        return 0, 0
    last = None
    for last in re.finditer(r"^\W*((?:\d+ \w+)(?:, \d+ \w+)*)(?: \(.*?\))? in [\d.]+s", text, re.M):
        pass
    if last:
        counts = {}  # type: Dict[str, int]
        for n, k in re.findall(r"(\d+) (\w+)", last.group(1)):
            counts[k] = counts.get(k, 0) + int(n)
        if any(k in counts for k in ("passed", "failed", "skipped", "error", "errors")):
            total = sum(counts.get(k, 0) for k in ("passed", "failed", "skipped", "xfailed", "xpassed", "error", "errors"))
            return total, counts.get("skipped", 0)
    # mocha
    passing = re.findall(r"^\s*(\d+) passing", text, re.M)
    if passing:
        failing = re.findall(r"^\s*(\d+) failing", text, re.M)
        pending = re.findall(r"^\s*(\d+) pending", text, re.M)
        sk = int(pending[-1]) if pending else 0
        return int(passing[-1]) + (int(failing[-1]) if failing else 0) + sk, sk
    # go test -v
    gov = re.findall(r"^\s*--- (PASS|FAIL|SKIP): ", text, re.M)
    if gov:
        return len(gov), sum(1 for g in gov if g == "SKIP")
    return None, None


# --------------------------------------------------------------------------------------------------
# ports and http
# --------------------------------------------------------------------------------------------------

def port_busy(port: int) -> bool:
    """True if something holds `port` on loopback (IPv4 or IPv6).

    A listener answers the connect; a listener with a full backlog makes it time out. Both mean busy. Only a refusal
    (nothing listening) or no route for that address family means free."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            s.settimeout(0.5)
            rc = s.connect_ex((host, port))
            if rc == 0 or rc in (errno.ETIMEDOUT, errno.EAGAIN, errno.EINPROGRESS, errno.EWOULDBLOCK):
                return True
        except socket.timeout:
            return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def busy_ports(ports: Sequence[int]) -> List[int]:
    return [p for p in ports if port_busy(p)]


def _wait_ports_free(ports: Sequence[int], seconds: float) -> List[int]:
    end = time.time() + seconds
    busy = busy_ports(ports)
    while busy and time.time() < end:
        time.sleep(0.1)
        busy = busy_ports(ports)
    return busy


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def http_status(url: str, timeout: float = 3.0) -> Optional[int]:
    """Status of a GET to a loopback URL; None if nothing answered. Never follows redirects, never leaves 127.0.0.1."""
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("verify only talks to its own loopback server")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------------------------------

def _stage(name: str, command: Optional[str], status: str, reason: str = "") -> Dict[str, Any]:
    return {"name": name, "command": command, "status": status, "exit_code": None, "duration_s": 0.0,
            "tests_collected": None, "skipped_tests": None, "reason": reason, "output_tail": ""}


def _with_route(command: str, route: Optional[str]) -> str:
    if not route:
        return command
    if "{route}" in command:
        return command.replace("{route}", shlex.quote(route))
    return "%s %s" % (command, shlex.quote(route))


def _finish_stage(st: Dict[str, Any], p: _Proc, rc: Optional[int], t0: float, count_tests: bool) -> str:
    """Fill the common fields from a finished process. Returns the full (unredacted) output tail."""
    st["exit_code"] = rc
    st["duration_s"] = round(time.time() - t0, 3)
    text = p.tail(_LOG_BYTES)
    if count_tests:
        st["tests_collected"], st["skipped_tests"] = parse_test_counts(text)
    return text


def run_command_stage(d: Definition, name: str, command: str, env: Dict[str, str], count_tests: bool,
                      findings: List[Dict[str, str]]) -> Tuple[Dict[str, Any], str]:
    st = _stage(name, redact.redact(command), "fail")
    t0 = time.time()
    p = _Proc(command, d.repo, env)
    try:
        rc = p.wait(d.timeout_s)
    finally:
        p.finish()
    text = _finish_stage(st, p, rc, t0, count_tests)
    p._discard()
    if p.timed_out:
        st["status"] = "timeout"
        st["reason"] = "no result after %ds; the process group was killed" % d.timeout_s
    elif rc == 0:
        st["status"] = "pass"
        if count_tests:
            n = st["tests_collected"]
            if n == 0:
                st["status"], st["reason"] = "fail", "the command exited 0 but collected no tests"
            elif n is None:
                findings.append({"id": "verify.test-count-unknown", "severity": "warn",
                                 "message": "%s: the test count could not be read from the output; a run that collects nothing would pass" % name})
    else:
        st["reason"] = "exited %s" % ("with a signal" if rc is None else rc)
        if rc in (126, 127):
            st["reason"] = "exit %d: the command was not found or not executable (environment, not code)" % rc
    if p.leaked:
        findings.append({"id": "verify.leaked-process", "severity": "warn",
                         "message": "%s left processes running after it finished; the group was killed" % name})
    st["output_tail"] = text[-_TAIL_BYTES:]
    return st, text


def run_smoke_stage(d: Definition, env: Dict[str, str], findings: List[Dict[str, str]]) -> Tuple[Dict[str, Any], str]:
    command = d.commands.get("smoke")
    st = _stage("smoke", redact.redact(command) if command else command, "fail")
    t0 = time.time()
    busy = busy_ports(d.ports)
    if busy:
        st["reason"] = "port(s) %s already in use; stop what holds them (a dev server left running?). Not retried." % ", ".join(str(b) for b in busy)
        st["duration_s"] = round(time.time() - t0, 3)
        st["_class"] = "env"
        return st, ""
    if not d.ports:
        findings.append({"id": "verify.ports-undeclared", "severity": "warn",
                         "message": "verify_ports is empty: the smoke stage cannot check that its ports are free before and after"})
    text = ""
    leaked_ports = []  # type: List[int]
    if d.dev:
        text = _smoke_dev(d, env, st, findings)
    else:
        route = d.routes[0] if d.routes else None
        p = _Proc(_with_route(command or "", route), d.repo, env)
        try:
            rc = p.wait(d.run_timeout_s)
        finally:
            p.finish()
        text = _finish_stage(st, p, rc, t0, False)
        p._discard()
        if p.timed_out:
            st["status"], st["reason"] = "timeout", "no result after %ds; the process group was killed" % d.run_timeout_s
        elif rc == 0:
            st["status"] = "pass"
        else:
            st["reason"] = "exited %s" % ("with a signal" if rc is None else rc)
            if rc in (126, 127):
                st["reason"] = "exit %d: the command was not found or not executable (environment, not code)" % rc
        if p.leaked:
            findings.append({"id": "verify.leaked-process", "severity": "error",
                             "message": "smoke: processes were still running after the harness exited (the local-dev.md leak); the group was killed"})
            if st["status"] == "pass":
                st["status"], st["reason"], st["_class"] = "fail", "the harness leaked processes", "leak"
    if d.ports:
        leaked_ports = _wait_ports_free(d.ports, PORT_SETTLE_S)
        if leaked_ports:
            findings.append({"id": "verify.port-leak", "severity": "error",
                             "message": "port(s) %s are still in use after the smoke stage: something outside the stage's process group holds them"
                             % ", ".join(str(x) for x in leaked_ports)})
            if st["status"] == "pass":
                st["status"], st["reason"], st["_class"] = "fail", "ports still bound after the smoke stage", "leak"
    st["duration_s"] = round(time.time() - t0, 3)
    st["output_tail"] = text[-_TAIL_BYTES:]
    return st, text


def _smoke_dev(d: Definition, env: Dict[str, str], st: Dict[str, Any], findings: List[Dict[str, str]]) -> str:
    base_url = "http://127.0.0.1:%d" % d.ports[0]
    p = _Proc(d.dev or "", d.repo, env)
    deadline = time.time() + d.run_timeout_s
    try:
        ready = False
        while time.time() < deadline:
            if p.proc.poll() is not None:
                st["exit_code"] = p.proc.returncode
                st["reason"] = "the dev server exited before it became ready (exit %s)" % p.proc.returncode
                return p.tail(_LOG_BYTES)
            s = http_status(base_url + d.ready_path, 2.0)
            if s is not None and s < 400:
                ready = True
                break
            time.sleep(0.2)
        if not ready:
            st["status"] = "timeout"
            st["reason"] = "%s%s did not answer below 400 within %ds" % (base_url, d.ready_path, d.run_timeout_s)
            return p.tail(_LOG_BYTES)
        for route in (d.routes or [d.ready_path]):
            s = http_status(base_url + route, 5.0)
            if s is None or s >= 400:
                st["reason"] = "GET %s -> %s" % (route, s if s is not None else "no answer")
                return p.tail(_LOG_BYTES)
        st["status"] = "pass"
        st["reason"] = "GET %s ok" % ", ".join(d.routes or [d.ready_path])
        return p.tail(_LOG_BYTES)
    finally:
        p.finish()
        p._discard()


# --------------------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------------------

def _write_log(d: Definition, log_dir: Optional[str], attempt: int, parts: List[Tuple[str, str]], findings: List[Dict[str, str]]) -> Optional[str]:
    try:
        if log_dir is None:
            common = gitutil.common_dir(d.repo)
            if not common:
                return None
            log_dir = os.path.join(common, "loopctl", "verify")
        jsonio.ensure_dir(log_dir, 0o700)
        name = "%s-a%d.log" % (d.item or "run", attempt)
        body = []
        for stage, text in parts:
            body.append("===== %s =====\n%s\n" % (stage, redact.redact(text[-_LOG_BYTES:])))
        path = os.path.join(log_dir, name)
        jsonio.atomic_write_text(path, "".join(body), 0o600)
        return path
    except (OSError, ValueError) as exc:
        findings.append({"id": "verify.log-failed", "severity": "warn", "message": "could not write the log: %s" % exc})
        return None


def run_verify(d: Definition, keep_going: bool = False, only: Optional[Sequence[str]] = None, log_dir: Optional[str] = None,
               attempt: int = 1, write_log: bool = True) -> Dict[str, Any]:
    t_start = time.time()
    findings = list(d.findings)
    selected = [s for s in STAGE_ORDER if only is None or s in only]
    complete = only is None or set(only) >= set(STAGE_ORDER)
    result = {"schema": SCHEMA, "status": "fail", "ok": False, "complete": complete, "gate": d.gate, "blocking": d.gate == "on",
              "failure_class": "none", "repo": d.repo, "item": d.item, "base": d.base, "head": d.head, "dirty": d.dirty,
              "config_source": d.config_source, "trusted": d.trusted, "routes": list(d.routes), "ac_ids": list(d.ac_ids),
              "stages": [], "findings": findings, "log": None, "duration_s": 0.0, "message": ""}  # type: Dict[str, Any]

    missing = _missing_required(d, [s for s in selected if s in REQUIRABLE])
    if missing:
        result["status"] = "error"
        result["failure_class"] = "config"
        result["message"] = ("required stage(s) with no command: %s (verify_require in %s at the base sha). "
                             "A required stage is never skipped silently." % (", ".join(missing), kconfig.CONFIG_PATH))
        for name in missing:
            result["stages"].append(_stage(name, None, "error", "required but not configured"))
        result["duration_s"] = round(time.time() - t_start, 3)
        return result

    env = build_env(d)
    log_parts = []  # type: List[Tuple[str, str]]
    failed = None  # type: Optional[Dict[str, Any]]
    fail_class = "none"
    for name in STAGE_ORDER:
        if name not in selected:
            continue
        command = d.commands.get(name)
        if not command:
            result["stages"].append(_stage(name, None, "skipped", "not configured"))
            continue
        if failed is not None and not keep_going:
            result["stages"].append(_stage(name, command, "skipped", "an earlier stage failed"))
            continue
        if name == "smoke":
            st, text = run_smoke_stage(d, env, findings)
        else:
            st, text = run_command_stage(d, name, command, env, name in ("item-tests", "tests"), findings)
        cls = st.pop("_class", None)
        if "output_tail" in st:
            st["output_tail"] = redact.redact(st["output_tail"]) if st["status"] != "pass" else ""
        st["reason"] = redact.redact(st["reason"])
        log_parts.append((name, text))
        result["stages"].append(st)
        if st["status"] in ("fail", "timeout", "error") and failed is None:
            failed = st
            if cls:
                fail_class = cls
            elif st["status"] == "timeout":
                fail_class = "timeout"
            elif st["exit_code"] in (126, 127):
                fail_class = "env"
            else:
                fail_class = _CLASS[name]
    if write_log and log_parts:
        result["log"] = _write_log(d, log_dir, attempt, log_parts, findings)

    ran = [s for s in result["stages"] if s["status"] != "skipped"]
    if failed is not None:
        result["status"] = "fail"
        result["failure_class"] = fail_class
        result["message"] = "%s failed: %s" % (failed["name"], failed["reason"] or "see output")
    elif not ran:
        result["status"] = "error"
        result["failure_class"] = "config"
        result["message"] = "nothing ran: no stage has a command"
    else:
        result["status"] = "pass"
        result["ok"] = complete
        result["message"] = "%d stage(s) passed" % len(ran) if complete else "the selected stages passed; this is not a full verify"
    result["duration_s"] = round(time.time() - t_start, 3)
    return result


def verify(repo: str, keep_going: bool = False, only: Optional[Sequence[str]] = None, log_dir: Optional[str] = None,
           attempt: int = 1, write_log: bool = True, apply_gate: bool = False, **load_options: Any) -> Dict[str, Any]:
    """load_definition + run_verify. A bad definition comes back as a result with status "error"."""
    try:
        d = load_definition(repo, **load_options)
    except DefinitionError as exc:
        return {"schema": SCHEMA, "status": "error", "ok": False, "complete": False, "gate": "on", "blocking": True,
                "failure_class": "config", "repo": repo, "item": load_options.get("item"), "base": None, "head": None, "dirty": False,
                "config_source": {"kind": "none", "sha": None, "path": kconfig.CONFIG_PATH}, "trusted": False, "routes": [],
                "ac_ids": [], "stages": [], "findings": [], "log": None, "duration_s": 0.0, "message": str(exc)}
    if apply_gate and d.gate == "off":
        return _skipped_result(d)
    return run_verify(d, keep_going, only, log_dir, attempt, write_log)


def _skipped_result(d: Definition) -> Dict[str, Any]:
    return {"schema": SCHEMA, "status": "skipped", "ok": True, "complete": False, "gate": "off", "blocking": False,
            "failure_class": "none", "repo": d.repo, "item": d.item, "base": d.base, "head": d.head, "dirty": d.dirty,
            "config_source": d.config_source, "trusted": d.trusted, "routes": [], "ac_ids": [], "stages": [],
            "findings": [{"id": "verify.gate-off", "severity": "warn", "message": "gates.g6 is off at the base sha; nothing was verified"}],
            "log": None, "duration_s": 0.0, "message": "gates.g6 is off at the base sha"}


def exit_code(result: Dict[str, Any], apply_gate: bool = False) -> int:
    st = result.get("status")
    if st == "error":
        return 2
    if st in ("pass", "skipped"):
        return 0
    if apply_gate and result.get("gate") == "report":
        return 0
    return 1


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

def format_text(res: Dict[str, Any]) -> str:
    lines = ["verify: %s%s  item=%s base=%s head=%s%s" % (
        res["status"].upper(), " (%s)" % res["failure_class"] if res["failure_class"] not in ("none", None) else "",
        res.get("item") or "-", (res.get("base") or "-")[:10], (res.get("head") or "-")[:10], " dirty" if res.get("dirty") else "")]
    lines.append("  " + res.get("message", ""))
    for st in res["stages"]:
        extra = ""
        if st.get("tests_collected") is not None:
            extra = "  %d tests" % st["tests_collected"]
        lines.append("  %-10s %-8s %s%s%s" % (st["name"], st["status"], "%.1fs" % st["duration_s"] if st["status"] != "skipped" else "",
                                              extra, "  " + st["reason"] if st["reason"] else ""))
    for f in res["findings"]:
        lines.append("  [%s] %s: %s" % (f["severity"], f["id"], f["message"]))
    for st in res["stages"]:
        if st["status"] in ("fail", "timeout", "error") and st.get("output_tail"):
            lines.append("--- %s output (tail) ---" % st["name"])
            lines.append(st["output_tail"].rstrip())
            break
    if res.get("log"):
        lines.append("  log: %s" % res["log"])
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run the kit-owned G6 verify definition (read from the base sha).")
    ap.add_argument("--repo", default=".", help="repository (default: current directory)")
    ap.add_argument("--base", help="base revision; default: merge-base of HEAD with origin/HEAD, main or master")
    ap.add_argument("--item", help="plan item id (labels the log; selects the entry in --items-file)")
    ap.add_argument("--items-file", help="JSON with per-item commands: {\"items\": {ID: {tests, demo_route, ac_ids}}}; "
                    "read at the base sha when it is inside the repository")
    ap.add_argument("--route", action="append", default=[], help="a URL path to request in the smoke stage (repeatable)")
    ap.add_argument("--only", help="comma-separated subset of stages; the result is marked incomplete")
    ap.add_argument("--keep-going", action="store_true", help="run later stages after a failure")
    ap.add_argument("--attempt", type=int, default=1, help="attempt number, used in the log name")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE", help="extra environment for the stages (repeatable)")
    ap.add_argument("--log-dir", help="where to write the log (default: <git-common-dir>/loopctl/verify)")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--apply-gate", action="store_true", help="honour gates.g6: off skips, report never fails the exit code")
    ap.add_argument("--allow-head-config", action="store_true", help="read the config from the working tree if the base has none (local use only)")
    ap.add_argument("--json", action="store_true", help="print the verify.v1 result as JSON")
    args = ap.parse_args(argv)

    only = None  # type: Optional[List[str]]
    if args.only:
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        bad = [s for s in only if s not in STAGE_ORDER]
        if bad:
            ap.error("unknown stage(s) in --only: %s (valid: %s)" % (", ".join(bad), ", ".join(STAGE_ORDER)))
    extra = {}  # type: Dict[str, str]
    for kv in args.env:
        if "=" not in kv:
            ap.error("--env wants NAME=VALUE, got %r" % kv)
        k, v = kv.split("=", 1)
        extra[k] = v
    _install_signal_handlers()
    try:
        res = verify(os.path.abspath(args.repo), keep_going=args.keep_going, only=only, log_dir=args.log_dir, attempt=args.attempt,
                     write_log=not args.no_log, apply_gate=args.apply_gate, base=args.base, item=args.item, items_file=args.items_file,
                     routes=args.route, allow_head_config=args.allow_head_config, extra_env=extra)
    except _Terminated as exc:
        sys.stderr.write("verify: %s; process groups killed\n" % exc)
        return 128 + exc.signum
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=False))
    else:
        print(format_text(res))
    return exit_code(res, args.apply_gate)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # an internal error must never read as a pass
        sys.stderr.write("verify: internal error: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
