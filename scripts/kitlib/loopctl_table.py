"""Who may run which loopctl command: the one table `loopctl/cli.py` and `guards/g_exec.py` both use (review fixes D1b, D3, D11).

    classify(rest) -> (class, sub, action)      rest = the argv after `loopctl`

class is one of:
    "read"   anyone may run it;
    "write"  changes the run state: only the registered conductor (and the human) may run it;
    "human"  grants an approval, lifts a stop or writes to a remote: a human runs it in a plain terminal, never an agent.

The rules fail closed: anything this table does not recognise is a write, an odd approve is human-only. `loopctl` parsers set
`allow_abbrev=False`, and a proper prefix of a write or human-only flag is still treated as that flag here.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Sequence, Tuple

__all__ = ["READ", "ACTION_READ", "FLAG_READ", "WRITE_FLAGS", "HUMAN_ONLY", "HUMAN_SUB", "HUMAN_FLAG", "HUMAN_UNLESS_DRY_RUN", "RECONCILE_FACT_FLAGS",
           "APPROVE_RELAY", "classify"]

READ = frozenset({"validate", "status", "render", "doctor", "audit", "report", "help", "--help", "-h", "version", "--version", "retro",
                  "harness", "gaps", "merge-class"})
# subcommand -> actions that only read; any other action of these subcommands is a write
ACTION_READ: Dict[str, FrozenSet[str]] = {
    "lock": frozenset({"status"}), "remote-lock": frozenset({"status"}), "claim": frozenset({"list", "show"}),
    "manifest": frozenset({"show", "verify"}), "unattended": frozenset({"check", "status", "promotion", "intake"}),
    "parallel": frozenset({"check", "status", "leases"}), "calibration": frozenset({"report", "scan"}),
}
# subcommand -> flags that make it read-only (exact token), unless a write flag is also present
FLAG_READ: Dict[str, Tuple[str, ...]] = {
    "reconcile": ("--dry-run",), "migrate-plan": ("--check", "--dry-run"), "cost": ("--projection",), "evidence": ("--check",),
    "run": ("--dry-run",), "abort": ("--dry-run",), "parallel": ("--dry-run",),
}
WRITE_FLAGS = ("--apply", "--write", "--force")

# human-only: commands that push to a remote (D3), grant an approval or lift a stop (D11)
HUMAN_ONLY: Dict[str, FrozenSet[str]] = {"remote-lock": frozenset({"acquire", "renew", "release", "steal"}),
                                         "claim": frozenset({"take", "release"})}
HUMAN_SUB = frozenset({"resume"})
HUMAN_FLAG: Dict[str, Tuple[str, ...]] = {"parallel": ("--apply",), "migrate-plan": ("--force",)}
# flags that make the command human-only unless it is a dry run (`abort --rollback-to SHA` resets the work tree; `abort --dry-run --rollback-to SHA` only reports)
HUMAN_UNLESS_DRY_RUN: Dict[str, Tuple[str, ...]] = {"abort": ("--rollback-to",)}
RECONCILE_FACT_FLAGS = ("--prs", "--rule-suites")       # with --apply: human; --live: human always
# approve scope -> the choices the conductor may relay from the human's chat reply (None: any choice)
APPROVE_RELAY: Dict[str, Optional[FrozenSet[str]]] = {"checkpoint": None, "item": frozenset({"reject", "stop"}),
                                                      "unblock": frozenset({"keep", "replan"})}


def _flag_name(tok: str) -> str:
    return tok.split("=", 1)[0]


def _has_flag(tokens: Sequence[str], flag: str) -> bool:
    """`flag`, `flag=...`, or a proper prefix of `flag` of at least three characters (`--f` for `--force`)."""
    for t in tokens:
        if not t.startswith("--"):
            continue
        name = _flag_name(t)
        if name == flag or (len(name) >= 3 and flag.startswith(name)):
            return True
    return False


def _approve_class(args: Sequence[str]) -> str:
    """args = the tokens after `approve`. "write" if the conductor may relay it, else "human"."""
    if not args or args[0].startswith("-"):
        return "human"          # scope must come first; `approve --by me item F1` fails closed
    scope = args[0]
    if scope not in APPROVE_RELAY:
        return "human"          # plan, env, breaker, cost, demotion, unknown
    choices = []
    toks = list(args[1:])
    k = 0
    while k < len(toks):
        t = toks[k]
        if t == "--choice":
            if k + 1 >= len(toks) or toks[k + 1].startswith("-"):
                return "human"
            choices.append(toks[k + 1])
            k += 2
            continue
        if t.startswith("--choice="):
            choices.append(t[len("--choice="):])
        elif t.startswith("--c"):
            return "human"      # an abbreviation or look-alike of --choice
        k += 1
    if len(choices) > 1:
        return "human"
    choice = choices[0] if choices else "approve"
    allowed = APPROVE_RELAY[scope]
    if allowed is None:
        return "write" if choice and not choice.startswith("-") else "human"
    return "write" if choice in allowed else "human"


def classify(rest: Sequence[str]) -> Tuple[str, str, Optional[str]]:
    """(class, subcommand, action) for `loopctl <rest...>`; see the module docstring."""
    rest = list(rest)
    first = next((k for k, a in enumerate(rest) if not a.startswith("-")), len(rest))
    lead = rest[:first]
    # loopctl takes no option before the command except --help/--version; anything else is not a clean invocation
    if any(a not in ("-h", "--help", "--version") for a in lead):
        return "write", "", None
    if first == len(rest):
        return "read", "", None
    sub = rest[first].lower()
    after = rest[first + 1:]
    action = after[0] if after and not after[0].startswith("-") else None

    if sub in HUMAN_ONLY:
        # the push actions: argparse finds the action word after any options (`remote-lock --owner x acquire`, `claim --by me take F1`), so the only form
        # that is not human is a plain read word first (or just `--help`); an option, an unknown word or anything else in doubt is a human step
        head = after[0] if after else None
        if head in ("-h", "--help") and len(after) == 1:
            return "read", sub, None
        if head is not None and head not in ACTION_READ.get(sub, ()):
            return "human", sub, action if action in HUMAN_ONLY[sub] else next((t for t in after if t in HUMAN_ONLY[sub]), action)
    if sub in HUMAN_SUB:
        return "human", sub, action
    if any(_has_flag(after, f) for f in HUMAN_FLAG.get(sub, ())):
        return "human", sub, action
    if sub in HUMAN_UNLESS_DRY_RUN and "--dry-run" not in after and any(_has_flag(after, f) for f in HUMAN_UNLESS_DRY_RUN[sub]):
        return "human", sub, action
    if sub == "reconcile":
        if _has_flag(after, "--live"):
            return "human", sub, action
        if _has_flag(after, "--apply") and any(_has_flag(after, f) for f in RECONCILE_FACT_FLAGS):
            return "human", sub, action
    if sub == "approve":
        return _approve_class(after), sub, action

    if any(_has_flag(after, f) for f in WRITE_FLAGS):
        return "write", sub, action
    if sub in READ:
        return "read", sub, action
    need = FLAG_READ.get(sub)
    if need and any(t in need for t in after):
        return "read", sub, action
    if sub in ACTION_READ and action in ACTION_READ[sub]:
        return "read", sub, action
    return "write", sub, action
