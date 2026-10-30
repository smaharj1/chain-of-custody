#!/usr/bin/env python3
"""WorktreeCreate and WorktreeRemove hooks (report 11.5 item 3, D-3, D-22; Phase 8). OPT-IN: not wired by the kit; the snippet is
`.claude/templates/parallel/worktree-hooks.settings.json`, merged by a human in one reviewed commit (H6).

A WorktreeCreate hook REPLACES the platform's own worktree creation for `claude --worktree` and for a subagent with `isolation: worktree`, and the
platform takes the worktree's absolute path from the hook's standard output. That is exactly what the report wants (D-3: the platform branches from
the remote default branch, and whether `worktree.baseRef: head` is honoured is unverified, V6 / V25): this hook cuts the new branch off the LOCAL
integration branch's tip, makes the worktree under `<repo>/.claude/worktrees/<name>` and nothing else, copies `.worktreeinclude` (only `.env.local`
and `.env.test`), reserves a port slot (the report's "port offset") and prints the path. WorktreeRemove takes it away and gives the port slot back.

    python3 .claude/scripts/worktree_hook.py create     (hook JSON on stdin: {"name": ..., "cwd": ...})
    python3 .claude/scripts/worktree_hook.py remove     (hook JSON on stdin: {"worktree_path": ..., "cwd": ...})

WHAT KEEPS IT SAFE. The worktree's name comes from hook input, which a prompt-injected session could shape: it must be one plain name (letters, digits,
`.`, `_`, `-`, no separator), the path is built under the repository's own `.claude/worktrees/` and nowhere else, and a path to remove must resolve
inside that directory with no symbolic link on the way. A name or path that fails any of that is refused (exit 2) and nothing is created or removed.
The branch is `worktree-<name>`, never a name the input chooses freely. No command from the repository or the input is run; git runs with a fixed
argument list, hooks off, the user's global git config ignored (loopctl's own `lgit`).

The hook event names and the fields of their input (`name`, `worktree_path`, `cwd`) are UNVERIFIED on the pinned CLI (a probe in tools/probes/README.md);
unknown shapes fail closed. What the hook does NOT do: install dependencies and create an ephemeral database. Both are stack-specific; the port slot
(`KIT_PORT_OFFSET`, `KIT_PORT_SLOT`, see `loopctl parallel status`) is what a project's own setup script keys them on.

Exit codes: 0 done (create printed the path); 2 refused or any error (the hook fails closed); 64 bad invocation.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, TextIO

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

MAX_INPUT = 64 * 1024
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 2, 64


class Refusal(Exception):
    pass


def _read_input(stdin: TextIO) -> Dict[str, Any]:
    raw = stdin.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise Refusal("hook input is larger than %d bytes" % MAX_INPUT)
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Refusal("hook input is not JSON")
    if not isinstance(doc, dict):
        raise Refusal("hook input is not a JSON object")
    return doc


def _repo_of(doc: Mapping[str, Any]) -> str:
    from kitlib import gitutil
    cwd = doc.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise Refusal("the hook input has no usable cwd")
    top = gitutil.toplevel(cwd, isolated=True)
    cd = gitutil.common_dir(cwd, isolated=True)
    if not top or not cd:
        raise Refusal("cwd is not inside a git repository")
    # a hook run from inside a linked worktree still means the project: its worktrees live under the main checkout
    main = os.path.dirname(os.path.realpath(cd)) if os.path.basename(os.path.realpath(cd)) == ".git" else os.path.realpath(top)
    return main


def _store(repo: str, cfg: Any) -> Any:
    from kitlib import runstate
    from loopctl.parallel import mutex
    rs = runstate.RunState.for_path(repo)
    if rs is None:
        raise Refusal("no run state for this repository")
    # the hook process ends the moment it has printed the path, so its slot must not be tied to its pid (pid 0: never "dead"); WorktreeRemove gives it back
    return mutex.LeaseStore(rs, ttl_s=float(cfg.get_int("lease_ttl_s") or 300), pid=0), rs


def create(doc: Mapping[str, Any], env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    from kitlib import gitutil
    from loopctl import branches, context
    from loopctl.common import LoopctlError
    from loopctl.parallel import ports, worktrees
    name = doc.get("name", doc.get("worktree_name"))
    if not worktrees.valid_hook_name(name):
        raise Refusal("%r is not an acceptable worktree name (one plain name: letters, digits, '.', '_', '-')" % str(name)[:40])
    repo = _repo_of(doc)
    try:
        ctx = context.Context.build(repo, env, plan="none", allow_head_config=False)
    except LoopctlError as exc:
        raise Refusal("cannot read the configuration: %s" % str(exc)[:120])
    ib = ctx.integration_branch()
    if not ib:
        raise Refusal("there is no integration branch (main, master or integration_branch) to cut the worktree from")
    tip = gitutil.rev_parse("refs/heads/%s^{commit}" % ib, repo, isolated=True)
    if not tip:
        raise Refusal("the integration branch %s has no commit" % ib)
    branch = "worktree-%s" % name
    if not gitutil.valid_ref("refs/heads/" + branch):
        raise Refusal("%r is not a usable branch name" % branch)
    path = worktrees.safe_path(repo, name, "hook")                         # refuses anything outside <repo>/.claude/worktrees
    if os.path.lexists(path):
        raise Refusal("%s already exists" % os.path.relpath(path, repo))
    from loopctl import lgit
    if gitutil.rev_parse("refs/heads/%s^{commit}" % branch, repo, isolated=True) is None:
        r = lgit.run_git(["branch", branch, tip], cwd=repo, isolated=True, pin_hooks=True)
        if r.returncode != 0:
            raise Refusal("git branch failed: %s" % r.stderr.strip()[:120])
    made = worktrees.create(repo, name, branch, kind="hook")
    try:
        names, _bad = worktrees.parse_include(worktrees.read_include(repo, tip) or "")
        copied = worktrees.apply_include(repo, made, names)
        store, _rs = _store(repo, ctx.cfg)
        alloc = ports.PortAllocator(store, [int(p) for p in ctx.cfg.get_list("verify_ports") if isinstance(p, int) and not isinstance(p, bool)], ttl_s=7 * 86400.0)
        block = alloc.allocate("hook:%s" % name, worktree=made, kind="hook")
    except Exception:
        worktrees.remove(repo, made, save=False)                           # all or nothing: no half-made worktree is left for the platform to find
        raise
    if block is None:
        worktrees.remove(repo, made, save=False)
        raise Refusal("no free port slot (every slot is held or busy)")
    err.write("worktree_hook: %s on %s from %s (%s); port slot %d (KIT_PORT_OFFSET=%d)%s\n" % (
        os.path.relpath(made, repo), branch, ib, tip[:12], block.slot, block.offset, "; copied %s" % ", ".join(copied) if copied else ""))
    out.write(made + "\n")
    return EXIT_OK


def remove(doc: Mapping[str, Any], env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    from loopctl import context
    from loopctl.parallel import worktrees
    raw = doc.get("worktree_path", doc.get("path"))
    if not isinstance(raw, str) or not os.path.isabs(raw):
        raise Refusal("the hook input has no absolute worktree path")
    repo = _repo_of(doc)
    if not worktrees.inside_root(repo, raw) or not worktrees.valid_hook_name(os.path.basename(os.path.realpath(raw))):
        raise Refusal("%s is not a worktree under %s: nothing was removed" % (raw[:100], worktrees.ROOT_REL))
    real = os.path.realpath(raw)
    ctx = context.Context.build(repo, env, plan="none", allow_head_config=False)
    worktrees.remove(repo, real, save=True)
    store, _rs = _store(repo, ctx.cfg)
    released = []  # type: List[str]
    for rec in store.list():
        meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        if not rec.get("corrupt") and meta.get("worktree") == real and meta.get("kind") == "hook":
            if store.remove(str(rec.get("name"))):
                released.append(str(rec.get("name")))
    err.write("worktree_hook: removed %s%s\n" % (os.path.relpath(real, repo), "; released %s" % ", ".join(released) if released else ""))
    return EXIT_OK


def main(argv: List[str], stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
         env: Optional[Mapping[str, str]] = None) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    if len(argv) != 1 or argv[0] not in ("create", "remove"):
        err.write("usage: worktree_hook.py create|remove   (hook JSON on stdin)\n")
        return EXIT_USAGE
    try:
        doc = _read_input(stdin or sys.stdin)
        e = dict(os.environ if env is None else env)
        return create(doc, e, out, err) if argv[0] == "create" else remove(doc, e, out, err)
    except Refusal as exc:
        err.write("worktree_hook: refused: %s\n" % str(exc)[:300])
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - a hook fails closed on anything it did not expect
        err.write("worktree_hook: refused: %s: %s\n" % (type(exc).__name__, str(exc)[:200]))
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
