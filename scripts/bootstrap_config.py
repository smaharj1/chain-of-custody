#!/usr/bin/env python3
"""bootstrap_config: write `loop.config.md` values through the key registry (Phase 11a; IMP-11, report 6.1 H6, DEC-13).

Almost every loop.config key is H6-protected: a caps, gates, delivery, verify-command or attestation change is a guardrail change that only a
human makes. So /bootstrap does not edit the file. It PLANS (an agent may), and a human APPLIES (from a plain terminal):

    bootstrap_config.py [--repo DIR] plan  --set KEY=VALUE [--set ...] [--name config-plan.json]
        Checks each key and value against the registry (known key, right type, allowed choice, evidence gate) and writes
        `.claude/bootstrap/<name>`. It prints what would change and flags the H6 keys and the keys whose value is a command.
        It changes no config.
    bootstrap_config.py [--repo DIR] apply PLAN [--write]
        HUMAN-RUN. Refuses inside a Claude Code session. Re-checks every key in the plan against the registry (the plan file is not
        trusted), edits only the value of the planned lines in `.claude/loop.config.md` (comments and every other line stay),
        and then parses the result: it writes only if the planned keys have exactly the planned values, no other key changed, and
        the file has no validation error that it did not have before. Dry run unless --write.

VALUE is read like a loop.config value: `true`, `5`, `[a, b]`, `"text"`, or plain text. A value that is a command (verify_tests and the
like) is run later by verify.py and the loop: read it before you apply. Mappings and lists of mappings are not edited here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import humanonly  # noqa: E402
from doctorlib import safeio  # noqa: E402
from kitlib import config as config_mod  # noqa: E402
from kitlib import evidence, gitutil, jsonio, miniyaml, registry  # noqa: E402

SCHEMA = "bootstrap_config_plan.v1"
PLAN_DIR = os.path.join(".claude", "bootstrap")
CONFIG_REL = os.path.join(".claude", "loop.config.md")
MAX_PLAN = 64 * 1024
MAX_KEYS = 40
_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")
_PLAN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.json\Z")
_LINE_KEY = re.compile(r"^(\s*)([a-z][a-z0-9_]*):(.*)$")


class ConfigPlanError(ValueError):
    pass


def resolve_root(repo: str) -> str:
    repo = os.path.abspath(repo)
    top = gitutil.toplevel(repo, isolated=True) if os.path.isdir(repo) else None
    return os.path.realpath(top) if top else os.path.realpath(repo)


# ------------------------------------------------------------------------------------------------ checking values

def parse_value(raw: str) -> Any:
    if raw.strip() == "":
        return ""
    if "\n" in raw or "\r" in raw or "\0" in raw:
        raise ConfigPlanError("a value cannot contain a newline or NUL")
    try:
        return miniyaml.parse_scalar(raw)
    except miniyaml.YamlError as exc:
        raise ConfigPlanError("cannot read the value %r: %s" % (raw[:40], exc))


def check_entry(reg: registry.Registry, root: str, name: Any, value: Any) -> registry.Key:
    """The registry key for (name, value), or a ConfigPlanError saying what is wrong."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ConfigPlanError("%r is not a loop.config key name" % str(name)[:40])
    key = reg.get(name)
    if key is None:
        sug = reg.suggest(name)
        raise ConfigPlanError("unknown key %s%s" % (name, " (did you mean %s?)" % " or ".join(sug) if sug else ""))
    problem = key.check_value(value)
    if problem:
        raise ConfigPlanError("%s: %s" % (name, problem))
    if isinstance(value, dict) or (isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)):
        raise ConfigPlanError("%s: mappings and nested lists are edited by hand, not here" % name)
    if isinstance(value, str) and value.strip() == registry.PLACEHOLDER:
        raise ConfigPlanError("%s: %s is the 'not filled in' placeholder, not a value" % (name, registry.PLACEHOLDER))
    if key.is_gated_value(value) and not evidence.passed(root):
        raise ConfigPlanError("%s=%r needs the evidence gate (docs/baseline/evidence.json), which has not passed (IMP-1)" % (name, value))
    return key


def flags_for(key: registry.Key) -> List[str]:
    out = []  # type: List[str]
    if key.h6_protected:
        out.append("H6")
    if key.type == "command":
        out.append("command: run later by verify.py and the loop")
    return out


def current_values(root: str, reg: registry.Registry) -> Dict[str, Any]:
    text, _ = safeio.read_text_status(os.path.join(root, CONFIG_REL), 1 << 20)
    if not text:
        return reg.defaults()
    try:
        return config_mod.load_text(text, reg).effective()
    except config_mod.ConfigError:
        return reg.defaults()


# ------------------------------------------------------------------------------------------------ plan

def build_plan(root: str, sets: Sequence[str], reg: Optional[registry.Registry] = None) -> Dict[str, Any]:
    reg = reg or registry.load_registry()
    if not sets:
        raise ConfigPlanError("nothing to plan: pass --set KEY=VALUE")
    if len(sets) > MAX_KEYS:
        raise ConfigPlanError("at most %d --set entries" % MAX_KEYS)
    values = {}  # type: Dict[str, Any]
    for item in sets:
        if "=" not in item:
            raise ConfigPlanError("--set wants KEY=VALUE, got %r" % item[:40])
        name, raw = item.split("=", 1)
        name = name.strip()
        if name in values:
            raise ConfigPlanError("%s is set twice" % name)
        value = parse_value(raw)
        check_entry(reg, root, name, value)
        values[name] = value
    return {"schema": SCHEMA, "set": values}


def write_plan(root: str, plan: Dict[str, Any], name: str) -> str:
    if not _PLAN_NAME.match(name):
        raise ConfigPlanError("--name must look like config-plan.json (letters, digits, '.', '_', '-')")
    claude = os.path.join(root, ".claude")
    if os.path.islink(claude) or not os.path.isdir(claude):
        raise ConfigPlanError(".claude is missing or a symlink")
    d = os.path.join(root, PLAN_DIR)
    if os.path.islink(d):
        raise ConfigPlanError(".claude/bootstrap is a symlink: refusing to write through it")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
        raise ConfigPlanError("%s exists and is not a regular file" % os.path.join(PLAN_DIR, name))
    jsonio.atomic_write_json(path, plan)
    return os.path.join(PLAN_DIR, name)


def _shown(value: Any) -> str:
    try:
        return miniyaml.dump_scalar(value)
    except ValueError:
        return repr(value)[:60]


def show_plan(root: str, plan: Dict[str, Any], reg: registry.Registry) -> str:
    cur = current_values(root, reg)
    lines = []
    for name in sorted(plan["set"]):
        key = reg.get(name)
        fl = flags_for(key)
        lines.append("  %-28s %s  ->  %s%s" % (name, _shown(cur.get(name)) if name in cur else "(unset)",
                                                _shown(plan["set"][name]), ("   [" + "; ".join(fl) + "]") if fl else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ the line editor

def _block(lines: List[str]) -> Tuple[int, int]:
    """(first, end) indexes of the yaml lines of a config file, using the loader's own fence rules."""
    text = "\n".join(lines)
    yaml_text, start = config_mod.extract_yaml_block(text)
    count = len(yaml_text.split("\n")) if yaml_text != "" else 0
    return start, start + count


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find(lines: List[str], lo: int, hi: int, depth: int, leaf: str) -> Optional[int]:
    for i in range(lo, hi):
        m = _LINE_KEY.match(lines[i])
        if m and len(m.group(1)) == 2 * depth and m.group(2) == leaf:
            return i
    return None


def _child_range(lines: List[str], idx: int, hi: int) -> Tuple[int, int]:
    """The lines under the mapping key at `idx`: from idx+1 up to the next line indented no deeper than the key."""
    base = _indent(lines[idx])
    j = idx + 1
    while j < hi:
        s = lines[j].strip()
        if s and not s.startswith("#") and _indent(lines[j]) <= base:
            break
        j += 1
    while j > idx + 1 and (not lines[j - 1].strip() or lines[j - 1].strip().startswith("#")):
        j -= 1  # trailing comments and blanks belong to what follows
    return idx + 1, j


def _value_split(line: str, lineno: int) -> Tuple[str, str, str]:
    """(head up to and including 'key: ', old value text, tail with the comment) of a `key: value  # comment` line."""
    body = miniyaml._strip_comment(line, lineno)
    tail = line[len(body):]
    m = _LINE_KEY.match(body)
    if not m:
        raise ConfigPlanError("line %d is not a plain `key: value` line" % lineno)
    rest = m.group(3)
    head = body[:len(body) - len(rest)]
    return head + " ", rest.strip(), tail


def set_value(lines: List[str], name: str, rendered: str) -> List[str]:
    """`lines` with `name` set to `rendered` (a dump_scalar text). Edits the value in place; adds the line if the key is absent."""
    first, end = _block(lines)
    parts = name.split(".")
    lo, hi = first, end
    idx = None  # type: Optional[int]
    for depth, part in enumerate(parts):
        idx = _find(lines, lo, hi, depth, part)
        if idx is None:
            break
        if depth < len(parts) - 1:
            lo, hi = _child_range(lines, idx, hi)
    out = list(lines)
    if idx is not None:
        head, old, tail = _value_split(lines[idx], idx + 1)
        if old == "" or old.startswith("|") or old.startswith(">"):
            raise ConfigPlanError("%s is a block value in this file: edit it by hand" % name)
        gap = ""
        if tail.strip():
            old_gap = tail[:len(tail) - len(tail.lstrip())]
            gap = " " * max(2, len(old_gap) + len(old) - len(rendered))   # keeps the comment in its column when it can
        out[idx] = head + rendered + gap + tail.lstrip()
        return out
    # the key is absent: add it. A nested key goes at the end of its parent mapping, creating the parent if needed.
    if len(parts) == 1:
        out.insert(end, "%s: %s" % (parts[0], rendered))
        return out
    parent = _find(lines, first, end, 0, parts[0])
    if len(parts) != 2:
        raise ConfigPlanError("%s is nested more than two levels: edit it by hand" % name)
    if parent is None:
        out[end:end] = ["%s:" % parts[0], "  %s: %s" % (parts[1], rendered)]
        return out
    _, cend = _child_range(lines, parent, end)
    out.insert(cend, "  %s: %s" % (parts[1], rendered))
    return out


def apply_plan_text(text: str, plan_values: Dict[str, Any], reg: registry.Registry) -> Tuple[str, List[Tuple[str, str, str]]]:
    """(new file text, [(key, old line text, new line text)]) or a ConfigPlanError. Checks the result by parsing it."""
    try:
        before = config_mod.load_text(text, reg)
    except config_mod.ConfigError as exc:
        raise ConfigPlanError("loop.config.md does not parse before any edit: %s" % str(exc).split("\n")[0][:160])
    lines = text.split("\n")
    changes = []  # type: List[Tuple[str, str, str]]
    for name in sorted(plan_values):
        rendered = miniyaml.dump_scalar(plan_values[name])
        old_lines = list(lines)
        lines = set_value(lines, name, rendered)
        diff = [(a, b) for a, b in zip(old_lines + [""] * (len(lines) - len(old_lines)), lines) if a != b]
        changes.append((name, diff[0][0].strip() if diff else "", diff[0][1].strip() if diff else ""))
    new_text = "\n".join(lines)
    try:
        after = config_mod.load_text(new_text, reg)
    except config_mod.ConfigError as exc:
        raise ConfigPlanError("the edited file would not parse (%s): nothing written" % str(exc).split("\n")[0][:160])
    if len(after.errors) > len(before.errors) or {str(i) for i in after.errors} - {str(i) for i in before.errors}:
        new = sorted({str(i) for i in after.errors} - {str(i) for i in before.errors})
        raise ConfigPlanError("the edit would add validation errors (%s): nothing written" % "; ".join(new)[:200])
    eff_before, eff_after = before.effective(), after.effective()
    for name, value in plan_values.items():
        if eff_after.get(name) != value:
            raise ConfigPlanError("after the edit %s reads %r, not %r: nothing written" % (name, eff_after.get(name), value))
    for name in set(eff_before) | set(eff_after):
        if name not in plan_values and eff_before.get(name) != eff_after.get(name):
            raise ConfigPlanError("the edit would also change %s: nothing written" % name)
    return new_text, changes


# ------------------------------------------------------------------------------------------------ apply

def load_plan(root: str, path: str, reg: registry.Registry) -> Dict[str, Any]:
    if not os.path.isabs(path):
        in_root = os.path.join(root, path)
        path = in_root if os.path.lexists(in_root) else os.path.join(os.getcwd(), path)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise ConfigPlanError("the plan file is a symlink")
    except OSError:
        raise ConfigPlanError("cannot read the plan file")
    data, problem = safeio.read_bytes(path, MAX_PLAN + 1)
    if data is None or len(data) > MAX_PLAN:
        raise ConfigPlanError("cannot read the plan file (%s)" % (problem or "too large"))
    try:
        plan = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ConfigPlanError("the plan file is not JSON")
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA or set(plan) != {"schema", "set"} or not isinstance(plan["set"], dict):
        raise ConfigPlanError("the plan file is not a %s document" % SCHEMA)
    if not plan["set"] or len(plan["set"]) > MAX_KEYS:
        raise ConfigPlanError("the plan sets no keys, or too many")
    for name, value in plan["set"].items():
        check_entry(reg, root, name, value)
    return plan


def config_path(root: str) -> str:
    p = os.path.join(root, CONFIG_REL)
    try:
        st = os.lstat(p)
    except OSError:
        raise ConfigPlanError(".claude/loop.config.md does not exist")
    if not stat.S_ISREG(st.st_mode):
        raise ConfigPlanError(".claude/loop.config.md is a symlink or not a regular file")
    return p


def _short(line: str) -> str:
    return miniyaml._strip_comment(line, 0).strip()


def cmd_apply(root: str, plan_path: str, write: bool, out=sys.stdout) -> int:
    reg = registry.load_registry()
    plan = load_plan(root, plan_path, reg)
    path = config_path(root)
    text, problem = safeio.read_text_status(path, 1 << 20)
    if text is None:
        raise ConfigPlanError("cannot read loop.config.md: %s" % problem)
    new_text, changes = apply_plan_text(text, plan["set"], reg)
    out.write("bootstrap_config apply: %s\n" % ("WRITE" if write else "DRY RUN (nothing is written; add --write)"))
    for name, old, new in changes:
        fl = flags_for(reg.get(name))
        out.write("  %s\n    - %s\n    + %s%s\n" % (name, _short(old) or "(not present)", _short(new), ("\n    [" + "; ".join(fl) + "]") if fl else ""))
    if write:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        jsonio.atomic_write_text(path, new_text, mode)
        out.write("written: %s. Commit it yourself (an H6 change).\n" % CONFIG_REL)
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bootstrap_config.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--set", action="append", default=[], dest="sets", metavar="KEY=VALUE")
    p.add_argument("--name", default="config-plan.json")
    a = sub.add_parser("apply")
    a.add_argument("plan")
    a.add_argument("--write", action="store_true")
    return ap


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    args = parser().parse_args(argv)
    if args.cmd == "apply":
        msg = humanonly.refusal("bootstrap_config", env)
        if msg:
            sys.stderr.write(msg + "\n")
            return 2
    try:
        root = resolve_root(args.repo)
        if args.cmd == "plan":
            reg = registry.load_registry()
            plan = build_plan(root, args.sets, reg)
            rel = write_plan(root, plan, args.name)
            print("bootstrap_config plan: wrote %s (no config was changed)\n%s" % (rel, show_plan(root, plan, reg)))
            print("A human applies it from a plain terminal:\n  python3 .claude/scripts/bootstrap_config.py apply %s          (dry run)\n"
                  "  python3 .claude/scripts/bootstrap_config.py apply %s --write" % (rel, rel))
            return 0
        return cmd_apply(root, args.plan, args.write)
    except (ConfigPlanError, registry.RegistryError) as exc:
        sys.stderr.write("bootstrap_config: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
