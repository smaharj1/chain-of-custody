#!/usr/bin/env bash
# PreToolUse(Bash) guard for loop-engineering runs.
#
# Blocks the git operations that NEITHER the Orchestrator conductor NOR engineer
# subagents may ever run: push / rebase / force-delete of a NON-feature branch /
# hard-reset of a SHARED branch (main|master|origin/*). Force-deleting the run's
# own `feat/*` (or `dead/*`) branches is explicitly ALLOWED — the loop's fail
# path ("discard branch") requires `git branch -D feat/<slug>`, so blocking it
# would make the conductor trip its own guard.
#
# The WHOLE command is tokenized once with shlex (which respects quotes), so a
# quoted commit MESSAGE or a JSON string containing "git push" stays a single
# token and is never mistaken for a real invocation. Before tokenizing, the
# command is normalized so ordinary chaining forms (newlines, `&&` without
# spaces, subshells, `&`) land on real command boundaries; env-var prefixes
# (`FOO=1 git push`) and wrappers (`command`, `exec`, `nohup`) are skipped at a
# boundary before testing for `git`.
#
# HONEST LIMITATIONS:
# - A hook cannot reliably tell an engineer-subagent `git commit` from the
#   conductor's, so the "engineers are git-read-only" boundary remains a
#   protocol convention (engineer-protocol.md §8). This hook enforces only the
#   universally-forbidden, shared-history operations.
# - Indirect execution (`echo git push | sh`) is an accepted bypass: this guard
#   allows on uncertainty by design (never block on a parse it can't trust) —
#   the convention covers adversarial forms.
# - If python3 is missing, the guard announces itself INACTIVE and allows.
#
# Wired via .claude/settings.json PreToolUse matcher "Bash".
# Exit 0 = allow; exit 2 = block (stderr shown to the model).

command -v python3 >/dev/null 2>&1 || {
  echo "guard-git: python3 not found — guard INACTIVE (allowing)" >&2
  exit 0
}

GUARD_INPUT="$(cat)" python3 <<'PY'
import os, re, sys, json, shlex

try:
    data = json.loads(os.environ.get("GUARD_INPUT", "") or "{}")
    cmd = data.get("tool_input", {}).get("command", "") or ""
    # Normalize chaining forms onto token boundaries BEFORE tokenizing.
    # Newlines become `;` (shlex would otherwise swallow them as whitespace);
    # the rest get padded so `&&`-without-spaces, subshells, and `&` split.
    # Inside quoted strings these replacements only mutate the token's content
    # — shlex still returns one token, so quoted mentions stay unblocked.
    cmd = cmd.replace("\n", " ; ")
    for ch in (";", "|", "&", "(", ")"):
        cmd = cmd.replace(ch, " %s " % ch)
    toks = shlex.split(cmd, comments=False, posix=True)
except Exception:
    sys.exit(0)  # unparseable -> allow (don't block on uncertainty)

OPS = {"|", "||", "&&", ";", "&", "(", ")", "{", "}"}
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"command", "exec", "nohup"}

def classify(sub, args):
    if sub == "push":
        return "git push (remote write — humans push at checkpoints)"
    if sub == "rebase":
        return "git rebase (history rewrite)"
    if sub == "branch" and ("-D" in args or ("--delete" in args and "--force" in args)):
        targets = [a for a in args if not a.startswith("-")]
        if targets and all(t.startswith(("feat/", "dead/")) for t in targets):
            return None  # conductor discarding its own item branch — allowed
        return "git branch -D of a non-feat branch (force delete)"
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
    j = i
    if at_boundary:
        # skip env-assignment prefixes and wrappers: FOO=1 command git push
        while j < n and (ENV_RE.match(toks[j]) or toks[j] in WRAPPERS):
            j += 1
    if at_boundary and j < n and toks[j] == "git":
        k = j + 1
        inv = []
        while k < n and toks[k] not in OPS:
            inv.append(toks[k]); k += 1
        # skip git global options: -C <path>, -c <kv>, --git-dir=..., -p, etc.
        g = 0
        while g < len(inv):
            t = inv[g]
            if t in ("-C", "-c"):
                g += 2; continue
            if t.startswith("-"):
                g += 1; continue
            break
        if g < len(inv):
            r = classify(inv[g], inv[g + 1:])
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
