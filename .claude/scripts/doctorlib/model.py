"""Result, Context and the check registry (IMP-22).

Everything the doctor does to the outside world goes through a seam on `Context`, so tests
replace it: `run` (subprocesses), `reach` (network probes), `ghapi` (GitHub), `now`, `env`,
`fs_root`. Defaults are the real thing; tests pass fakes. Nothing in this package opens a
socket or starts a process except through those seams.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = ["Result", "Context", "check", "gatherer", "is_known_id", "CHECKS", "GATHERERS", "run_checks", "default_run",
           "default_reach", "passed", "failed", "unknown", "skipped",
           "PASS", "FAIL", "UNKNOWN", "SKIPPED", "ERROR", "WARN", "INFO"]

PASS, FAIL, UNKNOWN, SKIPPED = "pass", "fail", "unknown", "skipped"
ERROR, WARN, INFO = "error", "warn", "info"
_STATUSES = (PASS, FAIL, UNKNOWN, SKIPPED)
_SEVERITIES = (ERROR, WARN, INFO)


class Result(object):
    """One finding. `severity` is the outcome's severity: a passing check is `info`.

    status: pass | fail | unknown | skipped. `unknown` means the doctor could not tell
    (not probed, not attested): it never counts as a pass.
    """

    __slots__ = ("id", "severity", "status", "message", "remediation", "detail")

    def __init__(self, id: str, status: str, message: str, severity: Optional[str] = None,
                 remediation: str = "", detail: Optional[Dict[str, Any]] = None) -> None:
        if status not in _STATUSES:
            raise ValueError("bad status %r" % (status,))
        if severity is None:
            severity = INFO if status in (PASS, SKIPPED) else ERROR
        if severity not in _SEVERITIES:
            raise ValueError("bad severity %r" % (severity,))
        self.id = id
        self.status = status
        self.severity = severity
        self.message = message
        self.remediation = remediation
        self.detail = detail or {}

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "severity": self.severity, "status": self.status,
             "message": self.message, "remediation": self.remediation}
        if self.detail:
            d["detail"] = self.detail
        return d

    def __repr__(self) -> str:
        return "Result(%s %s/%s: %s)" % (self.id, self.status, self.severity, self.message)


def passed(id: str, message: str, **kw: Any) -> Result:
    return Result(id, PASS, message, INFO, **kw)


def failed(id: str, message: str, remediation: str = "", severity: str = ERROR, **kw: Any) -> Result:
    return Result(id, FAIL, message, severity, remediation, **kw)


def unknown(id: str, message: str, remediation: str = "", severity: str = WARN, **kw: Any) -> Result:
    return Result(id, UNKNOWN, message, severity, remediation, **kw)


def skipped(id: str, message: str, **kw: Any) -> Result:
    return Result(id, SKIPPED, message, INFO, **kw)


# --- default seams -------------------------------------------------------------------------

def default_run(argv: Sequence[str], timeout: float = 10.0, env: Optional[Mapping[str, str]] = None,
                cwd: Optional[str] = None, input_text: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a fixed argv (shell=False). Returns (returncode, stdout, stderr); (127, "", reason)
    if the program is missing; (124, "", "timeout") on timeout."""
    try:
        proc = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              env=dict(env) if env is not None else None, cwd=cwd, shell=False,
                              input=input_text.encode("utf-8") if input_text is not None else None)
    except FileNotFoundError:
        return 127, "", "not found: %s" % argv[0]
    except PermissionError as exc:
        return 126, "", "not executable: %s (%s)" % (argv[0], exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as exc:
        return 126, "", str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def default_reach(host: str, port: int, timeout: float = 1.0) -> Optional[bool]:
    """TCP-connect probe. True = reachable, False = refused/unreachable. Tests replace this.
    Only called when the doctor is run with --live."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        s.close()


class Context(object):
    """Everything a check may look at. Construct with `Context.build(...)`."""

    def __init__(self, repo: str, home: str, env: Mapping[str, str], fs_root: str = "/",
                 live: bool = False, now: Optional[float] = None,
                 run: Callable[..., Tuple[int, str, str]] = default_run,
                 reach: Optional[Callable[..., Optional[bool]]] = None,
                 ghapi: Optional[Callable[[str], Tuple[int, Any]]] = None,
                 claude_version: Optional[str] = None, config_ref: Optional[str] = None,
                 extra_settings: Optional[List[str]] = None, workflow_exempt: Optional[List[str]] = None,
                 configured_level: Optional[str] = None, platform: Optional[str] = None, groups: Optional[List[str]] = None) -> None:
        self.repo = os.path.abspath(repo)
        self.home = os.path.abspath(home)
        self.env = dict(env)
        self.fs_root = fs_root
        self.live = live
        self.now = time.time() if now is None else now
        self.run = run
        self.reach = reach              # None = never probe the network
        self.ghapi = ghapi              # None = no GitHub data (fixture absent and not live)
        self.claude_version = claude_version
        self.config_ref = config_ref
        self.extra_settings = list(extra_settings or [])
        self.workflow_exempt = [os.path.abspath(p) for p in (workflow_exempt or [])]
        self.configured_level_override = configured_level
        self.platform = platform or sys.platform
        self._groups = groups
        self._facts = {}  # type: Dict[str, Any]
        self._cfg_loaded = False
        self._cfg = None  # type: Any
        self._cfg_error = None  # type: Optional[str]

    @property
    def groups(self) -> List[str]:
        """Names of the groups the current user belongs to (injectable for tests)."""
        if self._groups is None:
            names = []  # type: List[str]
            try:
                import grp
                for gid in os.getgroups():
                    try:
                        names.append(grp.getgrgid(gid).gr_name)
                    except KeyError:
                        pass
            except ImportError:
                pass
            self._groups = names
        return self._groups

    def git_env(self) -> Dict[str, str]:
        """The environment for the doctor's own git calls: GIT_DIR and friends removed, so a hostile
        environment cannot point the doctor at another repository. GIT_CONFIG_* stays (it is policy)."""
        drop = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EDITOR", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_PROXY_COMMAND")
        env = {k: v for k, v in self.env.items() if k not in drop}
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def git(self, args: Sequence[str], timeout: float = 10.0, input_text: Optional[str] = None, harden: bool = True) -> Tuple[int, str, str]:
        """Run git in the repo through the `run` seam. `harden` pins fsmonitor and hooks off, so reading a hostile
        repository's config cannot run its code (report §5.9)."""
        argv = ["git"] + (["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull] if harden else []) + list(args)
        return self.run(argv, timeout, self.git_env(), self.repo if os.path.isdir(self.repo) else None, input_text)

    # -- paths ---------------------------------------------------------------------
    def host_path(self, absolute: str) -> str:
        """Map an absolute system path (/var/run/docker.sock) under `fs_root`."""
        if self.fs_root in ("", "/"):
            return absolute
        return os.path.join(self.fs_root, absolute.lstrip("/"))

    def home_path(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)

    def repo_path(self, *parts: str) -> str:
        return os.path.join(self.repo, *parts)

    def getenv(self, name: str, default: str = "") -> str:
        return self.env.get(name, default)

    # -- facts (memoised gatherers; tests may pre-seed) --------------------------------------
    def fact(self, name: str) -> Any:
        if name not in self._facts:
            if name not in GATHERERS:
                raise KeyError("no gatherer named %r" % name)
            self._facts[name] = GATHERERS[name](self)
        return self._facts[name]

    def set_fact(self, name: str, value: Any) -> None:
        self._facts[name] = value

    # -- config ------------------------------------------------------------------------
    @property
    def config(self):
        """The loop.config as a kitlib Config, or None (see config_error)."""
        if not self._cfg_loaded:
            self._cfg_loaded = True
            try:
                from kitlib import config as config_mod
                from . import safeio
                if self.config_ref:
                    self._cfg = config_mod.load_at_sha(self.repo, self.config_ref)
                else:
                    path = os.path.join(self.repo, config_mod.CONFIG_PATH)
                    text, problem = safeio.read_text_status(path, 1 << 20)
                    if problem == safeio.MISSING:
                        raise FileNotFoundError(path)
                    if problem:
                        self._cfg_error = "%s is %s" % (os.path.join(".claude", "loop.config.md"), problem)
                        return self._cfg
                    self._cfg = config_mod.load_text(text, source=path)
            except FileNotFoundError:
                self._cfg_error = "%s not found" % os.path.join(".claude", "loop.config.md")
            except Exception as exc:  # ConfigError and anything else: never crash the doctor
                self._cfg_error = str(exc)
        return self._cfg

    @property
    def config_error(self) -> Optional[str]:
        self.config  # noqa: B018 - force load
        return self._cfg_error

    def cfg(self, name: str, default: Any = None) -> Any:
        c = self.config
        return c.get(name, default) if c is not None else default


# --- registry -------------------------------------------------------------------------------

CHECKS = []  # type: List[Dict[str, Any]]
GATHERERS = {}  # type: Dict[str, Callable[[Context], Any]]


def gatherer(name: str) -> Callable:
    """Register a fact gatherer. It reads the world (files, env, injected seams) and returns
    plain data; checks judge that data. Tests may pre-seed a fact with `ctx.set_fact`."""
    def deco(fn: Callable) -> Callable:
        if name in GATHERERS:
            raise ValueError("duplicate gatherer %s" % name)
        GATHERERS[name] = fn
        return fn
    return deco


def check(id: str, group: str, doc: str = "", emits: Sequence[str] = ()) -> Callable:
    """Register a check. The function takes a Context and returns a Result or a list of them.
    `id` is the stable id; results may extend it with a suffix (`deploy.workflow.<file>`) and a
    check that reports under other ids lists them in `emits` (a trailing `.` means "any suffix")."""
    def deco(fn: Callable) -> Callable:
        if any(c["id"] == id for c in CHECKS):
            raise ValueError("duplicate check id %s" % id)
        CHECKS.append({"id": id, "group": group, "fn": fn, "emits": tuple(emits), "doc": doc or (fn.__doc__ or "").strip().split("\n")[0]})
        return fn
    return deco


def is_known_id(rid: str) -> bool:
    """True if `rid` is a registered check id, a dotted extension of one, or declared in some check's `emits`."""
    for c in CHECKS:
        if rid == c["id"] or rid.startswith(c["id"] + "."):
            return True
        for e in c["emits"]:
            if rid == e or (e.endswith(".") and rid.startswith(e)):
                return True
    return False


def run_checks(ctx: Context, groups: Optional[Iterable[str]] = None) -> List[Result]:
    wanted = set(groups) if groups is not None else None
    out = []  # type: List[Result]
    for c in CHECKS:
        if wanted is not None and c["group"] not in wanted:
            continue
        try:
            res = c["fn"](ctx)
        except Exception as exc:  # a broken check is a failed check, never a crash or a pass
            res = Result(c["id"], FAIL, "check crashed: %s: %s" % (type(exc).__name__, exc), ERROR,
                         "report this as a doctor bug; treat the result as unknown")
        if res is None:
            continue
        if isinstance(res, Result):
            res = [res]
        out.extend(res)
    return out
