#!/usr/bin/env bash
# PreToolUse(Bash) guard for loop-engineering runs.
#
# Blocks the git operations that NEITHER the Orchestrator conductor NOR engineer
# subagents may ever run: push / rebase / force-delete / hard-reset of a SHARED
# branch (main|master|origin/*). These never conflict with the conductor's
# allowed set (checkout -b, commit, merge --no-ff to LOCAL main, reset --hard on
# its OWN feature branch), so this is a safe always-on backstop.
#
# The WHOLE command is tokenized once with shlex (which respects quotes), so a
# quoted commit MESSAGE or a JSON string containing "git push" stays a single
# token and is never mistaken for a real invocation. `git` is only treated as a
# command when it sits at a real command boundary (start, or after | || && ;).
#
# HONEST LIMITATION: a hook cannot reliably tell an engineer-subagent `git commit`
# from the conductor's, so the "engineers are git-read-only" boundary remains a
# protocol convention (engineer-protocol.md §8). This hook enforces only the
# universally-forbidden, shared-history operations. On any parse failure it
# ALLOWS (never blocks on uncertainty) — the convention covers the rest.
#
# Wired via .claude/settings.json PreToolUse matcher "Bash".
# Exit 0 = allow; exit 2 = block (stderr shown to the model).

GUARD_INPUT="$(cat)" python3 <<'PY'
import os, sys, json, shlex

try:
    data = json.loads(os.environ.get("GUARD_INPUT", "") or "{}")
    cmd = data.get("tool_input", {}).get("command", "") or ""
    toks = shlex.split(cmd, comments=False, posix=True)
except Exception:
    sys.exit(0)  # unparseable -> allow (don't block on uncertainty)

OPS = {"|", "||", "&&", ";", "(", ")", "{", "}"}

def classify(sub, args):
    if sub == "push":
        return "git push (remote write — humans push at checkpoints)"
    if sub == "rebase":
        return "git rebase (history rewrite)"
    if sub == "branch" and ("-D" in args or ("--delete" in args and "--force" in args)):
        return "git branch -D (force delete)"
    if sub == "reset" and "--hard" in args:
        for a in args:
            if a == "--hard":
                continue
            base = a.split("/")[-1]
            if a in ("main", "master") or base in ("main", "master") or a.startswith("origin"):
                return "git reset --hard of a shared branch (%s)" % a
    return None

i, n = 0, len(toks)
while i < n:
    at_boundary = (i == 0) or (toks[i - 1] in OPS)
    if toks[i] == "git" and at_boundary:
        k = i + 1
        inv = []
        while k < n and toks[k] not in OPS:
            inv.append(toks[k]); k += 1
        # skip git global options: -C <path>, -c <kv>, --git-dir=..., -p, etc.
        j = 0
        while j < len(inv):
            t = inv[j]
            if t in ("-C", "-c"):
                j += 2; continue
            if t.startswith("-"):
                j += 1; continue
            break
        if j < len(inv):
            r = classify(inv[j], inv[j + 1:])
            if r:
                sys.stderr.write("BLOCKED by guard-git: %s\n" % r)
                sys.stderr.write("Forbidden shared-history git op during a loop run. The "
                                 "conductor merges to LOCAL main only and never pushes; you "
                                 "push at checkpoints.\n")
                sys.exit(2)
        i = k
    else:
        i += 1
sys.exit(0)
PY
