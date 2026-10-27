#!/usr/bin/env python3
"""notify: reach the human when a run stops, parks, stalls or needs them (report 12.1 "Notifications", R2, DEC-31, L-42; Phase 7).

This is the hook half of R2. The kit NEVER sends anything itself: it runs the program the human wrote in `notify_command` (loop.config,
read at the base sha like every guardrail key) and nothing else. PushNotification, where the platform has it, is the interactive
channel (prose in docs/unattended.md); this script is for the CLI, the headless driver and the Notification / StopFailure hooks.

WHAT KEEPS IT SAFE (a message can be built from text a stranger wrote, so the channel is narrow):

  * `notify_command` is split into an argument vector once (shlex, no shell). Shell syntax in it (`; | & < > $ ` \\ ( ) { } * ? !`) is
    REFUSED rather than guessed at; the program must be an absolute path or a bare name on PATH; and it must not live inside the
    repository or the git directory, because an agent can edit files there and this runs with the human's rights.
  * The message is never on the command line. It goes to the program on stdin as one JSON document and in `KIT_NOTIFY_*` variables.
    Every field is closed vocabulary or plain text restricted to letters, digits, space and `. , : _ ( ) / # @ % + = -`; everything
    else becomes a space. Item titles, report text, issue text and child output are never included: only item ids that pass the id
    rule, run ids, reason codes and fixed sentences.
  * The program gets a scrubbed environment: PATH, HOME, the locale, KIT_NOTIFY_* and the names in `notify_env_allow`, minus the
    cloud, GitHub, SSH, git and Claude credential families (those are never passed, whatever the config says).
  * It runs in its own process group with a timeout (`notify_timeout_s`); a hung program is killed and the failure is reported.
  * By hand it sends only `--kind test`: the other kinds are the loop's, so a process that can run this script cannot make the loop appear to
    say something (`--dry-run` still shows what any of them would look like).
  * The hook form (`--hook`) takes NO free text from its input: only the hook event name and a few closed-vocabulary fields, so a
    process that pipes text into it cannot choose what the human reads.

    python3 .claude/scripts/notify.py --hook                       (Notification / StopFailure / PermissionRequest; JSON on stdin)
    python3 .claude/scripts/notify.py --kind test [--repo .]
    python3 .claude/scripts/notify.py --kind stop --level alert --item F1 --code security_blocked --run run-123 --dry-run     (shows a message; sends nothing)

Exit codes: 0 sent, or nothing is configured and `--hook` was used, or `--dry-run`; 1 the program failed or timed out;
2 internal error or unusable arguments (hooks fail closed); 3 refused: no notify_command, or one that breaks the rules above.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "kit.notify.v1"
KINDS = ("start", "done", "waiting", "stop", "stall", "demoted", "watchdog", "hook", "test")
LEVELS = ("info", "action", "alert")
HOOK_EVENTS = ("Notification", "StopFailure", "PermissionRequest", "Stop", "SubagentStop")
MAX_BODY = 300
MAX_ARGV = 16
MAX_ARG = 300
STAMP_NAME = "notify.stamp"

TITLES = {"start": "loop run started", "done": "loop run finished", "waiting": "loop run waits for you", "stop": "loop run stopped",
          "stall": "loop run stalled", "demoted": "loop run demoted to L3", "watchdog": "loop run needs a look",
          "hook": "Claude Code needs attention", "test": "test notification from the loop"}

BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TMPDIR", "TZ")
# Never passed to the program, whatever notify_env_allow says (B3, RC-5).
_HARD_CREDENTIALS = re.compile(r"^(AWS_|GH_|GITHUB_|GITLAB_|BITBUCKET_|GOOGLE_|GCLOUD_|CLOUDSDK_|AZURE_|ARM_|KUBE|DOCKER_|TF_VAR_|NPM_|NODE_AUTH|SSH_|GIT_|"
                               r"ANTHROPIC_|CLAUDE|OPENAI_)", re.I)
_SHELL_SYNTAX = re.compile(r"[;&|<>`$\\(){}*?!]")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
_ITEM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,23}\Z")
_CODE_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,60}\Z")
_UNSAFE_TEXT = re.compile(r"[^A-Za-z0-9 .,:_()/#@%+=-]+")


class NotifyError(ValueError):
    """The notify_command breaks a rule above. Nothing was run."""


# -- text ---------------------------------------------------------------------------------------------------------------------

def clean(text: Any, limit: int = MAX_BODY) -> str:
    """Plain text for a notification: only `A-Za-z0-9 .,:_()/#@%+=-`, anything else a space, runs of space collapsed, capped."""
    s = _UNSAFE_TEXT.sub(" ", text if isinstance(text, str) else str(text))
    s = re.sub(r" {2,}", " ", s).strip()
    return s[:limit].rstrip()


def _field(value: Any, rx: "re.Pattern[str]") -> Optional[str]:
    return value if isinstance(value, str) and rx.match(value) else None


def compose(kind: str, level: str, run: Optional[str] = None, item: Optional[str] = None, code: Optional[str] = None,
            outcome: Optional[str] = None, detail: Optional[str] = None, next_step: Optional[str] = None,
            now: Optional[float] = None) -> Dict[str, Any]:
    """The message document. `kind` and `level` come from fixed lists; `run`, `item` and `code` are dropped unless they match their patterns;
    `detail` and `next_step` are the caller's own fixed sentences (never repository text) and are cleaned anyway."""
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % ", ".join(KINDS))
    if level not in LEVELS:
        raise ValueError("level must be one of %s" % ", ".join(LEVELS))
    run_id, item_id, code_s = _field(run, _ID_RE), _field(item, _ITEM_RE), _field(code, _CODE_RE)
    out = _field(outcome, _CODE_RE)
    bits = []  # type: List[str]
    if item_id:
        bits.append("item %s" % item_id)
    if out:
        bits.append("outcome %s" % out)
    if code_s:
        bits.append("reason %s" % code_s)
    if detail:
        bits.append(clean(detail, 160))
    if next_step:
        bits.append("Next: %s" % clean(next_step, 160))
    ts = time.time() if now is None else now
    return {"schema": SCHEMA, "kind": kind, "level": level, "title": TITLES[kind], "body": clean(". ".join(bits) or TITLES[kind]),
            "run": run_id, "item": item_id, "code": code_s, "outcome": out, "host": clean(socket.gethostname(), 60),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))}


# -- the command --------------------------------------------------------------------------------------------------------------

def _inside(path: str, root: str) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def parse_command(text: Any, path_env: Optional[str] = None, forbid: Sequence[str] = ()) -> List[str]:
    """The argument vector for `notify_command`, with the program resolved to a real path. Raises NotifyError (nothing runs) when the
    string is empty, still a placeholder, uses shell syntax, names a relative or missing program, or a program inside `forbid`
    (the repository and the git directory)."""
    if not isinstance(text, str) or not text.strip() or text.strip() == "<command>":
        raise NotifyError("notify_command is not set")
    s = text.strip()
    if len(s) > 1000 or any(c in s for c in "\n\r\0"):
        raise NotifyError("notify_command must be one short line")
    m = _SHELL_SYNTAX.search(s)
    if m:
        raise NotifyError("notify_command is run without a shell, so %r cannot mean what a shell would make of it: write the program and its "
                          "arguments only, and put anything else in a script of yours" % m.group(0))
    try:
        argv = shlex.split(s, posix=True)
    except ValueError as exc:
        raise NotifyError("notify_command has unbalanced quotes (%s)" % exc)
    if not argv or len(argv) > MAX_ARGV or any(len(a) > MAX_ARG for a in argv):
        raise NotifyError("notify_command has no program, or too many or too long arguments")
    prog = argv[0]
    if "/" in prog:
        if not os.path.isabs(prog):
            raise NotifyError("notify_command names its program by a relative path; use an absolute path or a name on PATH")
        found = prog if os.path.isfile(prog) and os.access(prog, os.X_OK) else None
    else:
        found = shutil.which(prog, path=path_env if path_env is not None else os.environ.get("PATH", os.defpath))
    if not found:
        raise NotifyError("notify_command: %s is not an executable file" % clean(prog, 80))
    real = os.path.realpath(found)
    for root in forbid:
        if root and _inside(real, root):
            raise NotifyError("notify_command runs %s, which is inside the repository or its git directory: an agent can edit that file, and this "
                              "runs with your rights. Install the program outside the repository" % clean(real, 100))
    return [real] + argv[1:]


def forbidden_roots(repo: str, common_dir: str) -> List[str]:
    """Where a notify_command program may not live: the worktree, the git directory, and the main checkout when this is a linked worktree."""
    roots = [repo, common_dir]
    if os.path.basename(os.path.normpath(common_dir)) == ".git":
        roots.append(os.path.dirname(os.path.normpath(common_dir)))
    return [r for r in roots if r]


def build_env(base: Mapping[str, str], allow: Sequence[str], message: Mapping[str, Any]) -> Dict[str, str]:
    env = {}  # type: Dict[str, str]
    names = set(BASE_ENV) | {n for n in allow if isinstance(n, str) and _NAME_RE.match(n) and not _HARD_CREDENTIALS.match(n)}
    for k, v in base.items():
        if k in names and isinstance(v, str):
            env[k] = v
    for key in ("kind", "level", "title", "body", "run", "item", "code", "outcome", "host", "at"):
        val = message.get(key)
        if isinstance(val, str):
            env["KIT_NOTIFY_" + key.upper()] = val
    return env


def send(command: Any, message: Mapping[str, Any], env: Mapping[str, str], forbid: Sequence[str] = (), timeout_s: float = 20.0,
         allow: Sequence[str] = (), popen: Callable[..., Any] = subprocess.Popen) -> Dict[str, Any]:
    """Run the configured program once with `message`. Never raises. {ok, rc, why}: `why` is empty on success, else a short cleaned reason."""
    try:
        argv = parse_command(command, env.get("PATH"), forbid)
    except NotifyError as exc:
        return {"ok": False, "rc": None, "why": clean(str(exc), 200), "refused": True}
    cenv = build_env(env, allow, message)
    payload = (json.dumps(dict(message), sort_keys=True) + "\n").encode("utf-8")
    cwd = cenv.get("HOME") if cenv.get("HOME") and os.path.isdir(cenv.get("HOME", "")) else "/"
    proc = None
    try:
        proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=cenv, cwd=cwd, shell=False, start_new_session=True)
        try:
            _out, err = proc.communicate(payload, timeout=max(1.0, float(timeout_s)))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "rc": None, "why": "timed out after %ds and was killed" % int(timeout_s)}
        rc = proc.returncode
        if rc == 0:
            return {"ok": True, "rc": 0, "why": ""}
        return {"ok": False, "rc": rc, "why": "exit %s%s" % (rc, (": " + clean(err.decode("utf-8", "replace")[-200:], 120)) if err else "")}
    except OSError as exc:
        return {"ok": False, "rc": None, "why": "could not start: %s" % clean(exc.strerror or type(exc).__name__, 100)}
    finally:
        if proc is not None and proc.poll() is None:
            _kill_group(proc)


def _kill_group(proc: Any) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


# -- config and the loop's use of it --------------------------------------------------------------------------------------------

def settings_from_config(cfg: Any) -> Dict[str, Any]:
    """The three keys, from a loopctl Config (read at the base sha)."""
    cmd = cfg.get("notify_command")
    timeout = cfg.get("notify_timeout_s")
    allow = cfg.get("notify_env_allow")
    return {"command": cmd if isinstance(cmd, str) else "",
            "timeout_s": float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 else 20.0,
            "allow": [a for a in allow if isinstance(a, str)] if isinstance(allow, list) else []}


def configured(settings: Mapping[str, Any], env: Mapping[str, str], forbid: Sequence[str] = ()) -> Tuple[bool, str]:
    """(usable, why-not): the check `unattended` runs before it starts. Runs nothing."""
    try:
        parse_command(settings.get("command"), env.get("PATH"), forbid)
    except NotifyError as exc:
        return False, str(exc)
    return True, ""


def _stamp_recent(common_dir: str, min_interval_s: float) -> bool:
    path = os.path.join(common_dir, "loopctl", STAMP_NAME)
    try:
        return time.time() - os.stat(path).st_mtime < min_interval_s
    except OSError:
        return False


def _touch_stamp(common_dir: str) -> None:
    d = os.path.join(common_dir, "loopctl")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, STAMP_NAME), "w") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


# -- the hook form --------------------------------------------------------------------------------------------------------------

def hook_message(raw: str) -> Dict[str, Any]:
    """A message from a hook's stdin JSON. Free text in the input (a notification's message, an error) is NOT used."""
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    ev = doc.get("hook_event_name")
    ev = ev if ev in HOOK_EVENTS else "hook"
    ntype = _field(doc.get("notification_type"), re.compile(r"^[a-z_]{1,40}\Z"))
    tool = _field(doc.get("tool_name"), re.compile(r"^[A-Za-z0-9_:.-]{1,60}\Z"))
    bits = ["event %s" % ev]
    if ntype:
        bits.append("type %s" % ntype)
    if tool and ev == "PermissionRequest":
        bits.append("tool %s" % tool)
    level = "alert" if ev == "StopFailure" else "action"
    msg = compose("hook", level, detail=", ".join(bits), next_step="open the session")
    return msg


# -- command line ---------------------------------------------------------------------------------------------------------------

def _main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None, stderr: Any = None,
          env: Optional[Mapping[str, str]] = None, popen: Callable[..., Any] = subprocess.Popen) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    envm = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="notify.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default=None)
    ap.add_argument("--hook", action="store_true", help="read a hook's JSON on stdin (Notification, StopFailure, PermissionRequest)")
    ap.add_argument("--kind", choices=KINDS, default="test")
    ap.add_argument("--level", choices=LEVELS, default="info")
    ap.add_argument("--item", default=None)
    ap.add_argument("--run", default=None)
    ap.add_argument("--code", default=None)
    ap.add_argument("--outcome", default=None)
    ap.add_argument("--min-interval-s", type=float, default=None, help="skip if a notification was sent less than this long ago (default 30 with --hook)")
    ap.add_argument("--dry-run", action="store_true", help="print the message and the command that would run; run nothing")
    a = ap.parse_args(list(argv) if argv is not None else None)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from loopctl.context import Context
    ctx = Context.build(os.path.abspath(a.repo), envm, base=a.base, plan="none", allow_head_config=False)
    st = settings_from_config(ctx.cfg)
    if a.hook:
        msg = hook_message((stdin or sys.stdin).read(65536))
    else:
        for name, rx in (("--item", _ITEM_RE), ("--run", _ID_RE), ("--code", _CODE_RE), ("--outcome", _CODE_RE)):
            v = getattr(a, name[2:])
            if v is not None and not rx.match(v):
                err.write("notify: %s is not a valid %s\n" % (name, name[2:]))
                return 2
        if a.kind != "test" and not a.dry_run:
            err.write("notify: only the loop sends a %r message; by hand use --kind test (or --dry-run to see one)\n" % a.kind)
            return 2
        msg = compose(a.kind, a.level, run=a.run, item=a.item, code=a.code, outcome=a.outcome)
    forbid = forbidden_roots(ctx.repo, ctx.rs.common_dir)
    ok, why = configured(st, envm, forbid)
    if a.dry_run:
        out.write(json.dumps({"message": msg, "configured": ok, "why_not": why, "would_run": parse_command(st["command"], envm.get("PATH"), forbid) if ok else None},
                             indent=2, sort_keys=True) + "\n")
        return 0
    if not ok:
        if a.hook:
            return 0                                   # a hook runs on every notification; nothing configured is not an error
        err.write("notify: refused: %s\n" % why)
        return 3
    gap = a.min_interval_s if a.min_interval_s is not None else (30.0 if a.hook else 0.0)
    if gap and _stamp_recent(ctx.rs.common_dir, gap):
        return 0
    if gap:
        _touch_stamp(ctx.rs.common_dir)
    res = send(st["command"], msg, envm, forbid, st["timeout_s"], st["allow"], popen=popen)
    if not res["ok"]:
        err.write("notify: the notify_command failed: %s\n" % res["why"])
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None, **kw: Any) -> int:
    """Hook entry point: any exception is a one-line reason on stderr and exit 2 (fail closed, IMP-16)."""
    try:
        return _main(argv, **kw)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    except BaseException as exc:  # noqa: BLE001
        sys.stderr.write("notify: internal error: %s: %s\n" % (type(exc).__name__, clean(exc, 160)))
        return 2


if __name__ == "__main__":
    sys.exit(main())
