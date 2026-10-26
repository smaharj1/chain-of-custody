#!/usr/bin/env python3
"""labels: create the factory labels in a GitHub repository (Phase 11a; report 19.7 Q4, RC-20; IMP-15).

Run by a human, as someone who may create labels, from a plain terminal. It refuses to run when `CLAUDECODE` is set.
Dry run by default: it prints every `gh` call and sends nothing. Add --apply to make them.

    labels.py list                                              the labels, from the template (offline)
    labels.py apply --repo OWNER/NAME [--labels-file FILE]      DRY RUN: prints one `gh label create` per label
    labels.py apply --repo OWNER/NAME [--labels-file FILE] --apply

Each label is one call: `gh label create NAME --repo OWNER/NAME --color C --description D --force`. `--force`
updates a label that already exists, so the script can be run again. The label list is `.claude/templates/github/labels.json`
unless --labels-file names another file; every name, colour and description is checked against a strict pattern before it
reaches a command line, and a name never starts with `-`. Arguments go to `gh` as an argument list (no shell).

Labels do not carry trust: anyone with triage rights can apply one. `label-guard.yml` removes the `approver_only` ones
when they are applied by anyone outside `factory_approvers`. This script does not set that variable: see docs/ops.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
KIT_ROOT = os.path.dirname(SCRIPTS)  # .claude
DEFAULT_LABELS = os.path.join(KIT_ROOT, "templates", "github", "labels.json")

sys.path.insert(0, SCRIPTS)

import humanonly  # noqa: E402
from doctorlib import safeio  # noqa: E402

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_. /-]{0,49}$")
_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
MAX_LABELS = 100
MAX_FILE = 256 * 1024
_LABEL_FIELDS = {"name", "color", "description", "approver_only"}


class LabelError(ValueError):
    """A label file or an argument that must not reach a command line."""


def check_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _SLUG_RE.match(repo):
        raise LabelError("--repo must be OWNER/NAME (letters, digits, '.', '_' and '-')")
    owner, name = repo.split("/")
    if owner in (".", "..") or name in (".", "..") or name.endswith(".git"):
        raise LabelError("--repo must be OWNER/NAME, not a path or a .git name")
    return repo


class Label(object):
    __slots__ = ("name", "color", "description", "approver_only")

    def __init__(self, name: str, color: str, description: str, approver_only: bool) -> None:
        self.name, self.color, self.description, self.approver_only = name, color.lower(), description, approver_only


def _check_description(text: Any, where: str) -> str:
    if not isinstance(text, str):
        raise LabelError("%s: description must be a string" % where)
    if len(text) > 100:
        raise LabelError("%s: description is longer than 100 characters" % where)
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            raise LabelError("%s: description contains a control or invisible character (U+%04X)" % (where, ord(ch)))
    return text


def parse_labels(data: Any, source: str = "labels") -> List[Label]:
    """Validate a decoded labels file. Raises LabelError naming the first problem."""
    if not isinstance(data, dict) or not isinstance(data.get("labels"), list):
        raise LabelError("%s: expected an object with a 'labels' list" % source)
    items = data["labels"]
    if not items:
        raise LabelError("%s: the labels list is empty" % source)
    if len(items) > MAX_LABELS:
        raise LabelError("%s: more than %d labels" % (source, MAX_LABELS))
    out = []  # type: List[Label]
    seen = set()  # type: set
    for i, raw in enumerate(items):
        where = "%s: label %d" % (source, i + 1)
        if not isinstance(raw, dict):
            raise LabelError("%s: not an object" % where)
        extra = set(raw) - _LABEL_FIELDS
        if extra:
            raise LabelError("%s: unknown field(s) %s" % (where, ", ".join(sorted(str(e) for e in extra))))
        name, color = raw.get("name"), raw.get("color")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise LabelError("%s: name %r is not allowed (start with a letter or digit; letters, digits and : _ . / space - only; at most 50 characters)" % (where, name))
        if name != name.strip() or "//" in name:
            raise LabelError("%s: name %r has stray spaces or slashes" % (where, name))
        if not isinstance(color, str) or not _COLOR_RE.match(color):
            raise LabelError("%s: color %r must be six hex digits without '#'" % (where, color))
        desc = _check_description(raw.get("description", ""), where)
        approver_only = raw.get("approver_only", False)
        if not isinstance(approver_only, bool):
            raise LabelError("%s: approver_only must be true or false" % where)
        key = name.lower()
        if key in seen:
            raise LabelError("%s: duplicate label name %r (GitHub label names are case-insensitive)" % (where, name))
        seen.add(key)
        out.append(Label(name, color, desc, approver_only))
    return out


def load_labels(path: str) -> List[Label]:
    text, problem = safeio.read_text_status(path, MAX_FILE)
    if text is None:
        raise LabelError("cannot read %s: %s" % (path, problem))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LabelError("%s is not valid JSON: %s" % (path, exc))
    return parse_labels(data, os.path.basename(path))


class Call(object):
    """One `gh` call."""

    def __init__(self, argv: List[str], note: str = "") -> None:
        self.argv, self.note = argv, note

    def render(self) -> str:
        cmd = " ".join(shlex.quote(a) for a in self.argv)
        return ("# " + self.note + "\n" + cmd) if self.note else cmd


def build_plan(repo: str, labels: Sequence[Label]) -> List[Call]:
    check_repo(repo)
    calls = []  # type: List[Call]
    for lab in labels:
        note = "approver-only: label-guard removes it when anyone else applies it" if lab.approver_only else ""
        calls.append(Call(["gh", "label", "create", lab.name, "--repo", repo, "--color", lab.color,
                           "--description", lab.description, "--force"], note))
    return calls


def run_gh(call: Call, runner: Callable = subprocess.run, env: Optional[Dict[str, str]] = None):
    try:
        p = runner(call.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, shell=False, env=env)
    except FileNotFoundError:
        return 127, "", "gh is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    out = p.stdout.decode("utf-8", "replace") if isinstance(p.stdout, bytes) else (p.stdout or "")
    err = p.stderr.decode("utf-8", "replace") if isinstance(p.stderr, bytes) else (p.stderr or "")
    return p.returncode, out, err


def cmd_list(args: argparse.Namespace) -> int:
    labels = load_labels(args.labels_file or DEFAULT_LABELS)
    for lab in labels:
        print("%-18s #%s  %s%s" % (lab.name, lab.color, lab.description, "  [approver-only]" if lab.approver_only else ""))
    return 0


def cmd_apply(args: argparse.Namespace, env: Dict[str, str], runner: Callable) -> int:
    labels = load_labels(args.labels_file or DEFAULT_LABELS)
    calls = build_plan(args.repo, labels)
    mode = "APPLY" if args.apply else "DRY RUN (nothing is sent; add --apply to make these calls)"
    print("labels apply: %s, repo %s, %d label(s)\n" % (mode, args.repo, len(calls)))
    for i, c in enumerate(calls, 1):
        print("[%d/%d] %s\n" % (i, len(calls), c.render()))
    if args.apply:
        for i, c in enumerate(calls, 1):
            rc, out, err = run_gh(c, runner, env)
            if rc != 0:
                sys.stderr.write("labels: call %d failed (exit %d): %s\nStopped; the labels after it were not sent. Fix the error and run again (it is safe to repeat).\n"
                                 % (i, rc, err.strip()[:300]))
                return 1
            print("ok: " + c.argv[3])
    print("\nStill yours to do:")
    print("  - set the repository variable FACTORY_APPROVERS to the logins in factory_approvers (label-guard.yml reads it):")
    print("      gh variable set FACTORY_APPROVERS --repo %s --body 'login1,login2'" % args.repo)
    print("  - copy .claude/templates/github/workflows/label-guard.yml into .github/workflows/ (without it, labels carry no trust)")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="labels.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="print the labels (offline)")
    ls.add_argument("--labels-file", default=None)
    a = sub.add_parser("apply", help="create the labels (dry run by default)")
    a.add_argument("--repo", required=True, help="OWNER/NAME")
    a.add_argument("--labels-file", default=None, help="a labels file (default: the kit template)")
    a.add_argument("--apply", action="store_true", help="actually make the calls (without this, nothing is sent)")
    return ap


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None, runner: Callable = subprocess.run) -> int:
    env = dict(os.environ if env is None else env)
    msg = humanonly.refusal("labels", env)
    if msg:
        sys.stderr.write(msg + "\n")
        return 2
    args = parser().parse_args(argv)
    try:
        if args.cmd == "list":
            return cmd_list(args)
        return cmd_apply(args, env, runner)
    except LabelError as exc:
        sys.stderr.write("labels: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
