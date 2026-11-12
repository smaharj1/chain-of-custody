#!/usr/bin/env python3
"""gate: the small, testable checks the infrastructure workflow templates call (report §8.2, §8.4, RC-2, RC-4).

The workflows keep their logic here, in a script taken from the BASE ref, so it can be tested and so a
pull request cannot change it. Nothing here talks to a network, runs terraform, or applies anything.

  authorize --actor LOGIN [--actor LOGIN ...] --approvers a,b,c [--bot LOGIN ...]
        exit 0 only if every actor is listed in the approvers (`factory_approvers`) and none of them is a
        bot. An empty approver list refuses. A bot that appears in the list is a misconfiguration and refuses.
  base-is-main --planned-sha SHA --main-sha SHA
        exit 0 only if the commit that was planned is still the tip of main (RC-4).
  hash-file FILE                      print the sha256 of a file
  verify-hash --file FILE --sha256 H  exit 0 only if the file still has that sha256 (the saved plan, by hash)
  plan-digest --plan-json FILE        print a digest of what the plan would change, stable across runs
  plan-summary --plan-json FILE       print a values-free markdown summary (addresses and actions only)
  require-main-ref --ref REF          exit 0 only if REF is refs/heads/main
  require-run-approval --approvals-json FILE --environment NAME --approvers a,b [--bot LOGIN]
        exit 0 only if the run's review history (GET /actions/runs/{id}/approvals) holds an approval for that
        Environment by a listed approver who is not a bot. An Environment that GitHub created on first use,
        with no required reviewers, produces no approval, so a missing gate is refused at run time.

Exit 0: ok. Exit 1: refused (one-line reason on stderr). Exit 2: usage or internal error.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMENT_MARKER = "<!-- kit:infra-plan-preview -->"
MAX_ROWS = 200


class Refused(Exception):
    pass


def _logins(text: str) -> List[str]:
    return [t.lstrip("@").strip().lower() for t in re.split(r"[,\s]+", text or "") if t.strip()]


def authorize(actors: Sequence[str], approvers_text: str, bots: Sequence[str]) -> None:
    approvers = _logins(approvers_text)
    if not approvers:
        raise Refused("the approver list is empty (set FACTORY_APPROVERS to the same logins as factory_approvers)")
    bot_set = {b.lstrip("@").strip().lower() for b in bots if b and b.strip()}
    for a in approvers:
        if a.endswith("[bot]") or a in bot_set:
            raise Refused("the approver list names a bot (%s): a bot never approves" % a)
    if not actors:
        raise Refused("no actor given")
    for actor in actors:
        who = (actor or "").lstrip("@").strip().lower()
        if not who:
            raise Refused("empty actor")
        if who.endswith("[bot]") or who in bot_set:
            raise Refused("actor %s is a bot identity" % who)
        if who not in approvers:
            raise Refused("actor %s is not in factory_approvers" % who)


def base_is_main(planned: str, main: str) -> None:
    p, m = (planned or "").strip().lower(), (main or "").strip().lower()
    if not _SHA1.match(p):
        raise Refused("planned sha %r is not a 40-character commit sha" % planned)
    if not _SHA1.match(m):
        raise Refused("main sha %r is not a 40-character commit sha" % main)
    if p != m:
        raise Refused("the plan was made from %s but main is now %s: plan again from main" % (p[:12], m[:12]))


def require_main_ref(ref: str) -> None:
    if ref != "refs/heads/main":
        raise Refused("ref %r is not refs/heads/main" % ref)


def require_run_approval(approvals: Any, environment: str, approvers_text: str, bots: Sequence[str]) -> str:
    """Returns the login whose approval counts. `approvals` is the list GitHub returns for a workflow run."""
    approvers = _logins(approvers_text)
    if not approvers:
        raise Refused("the approver list is empty")
    if not isinstance(approvals, list):
        raise Refused("the approvals document is not a list")
    bot_set = {b.lstrip("@").strip().lower() for b in bots if b and b.strip()}
    for entry in approvals:
        if not isinstance(entry, dict) or str(entry.get("state", "")).lower() != "approved":
            continue
        envs = [str(e.get("name", "")) for e in entry.get("environments") or [] if isinstance(e, dict)]
        if environment not in envs:
            continue
        login = str((entry.get("user") or {}).get("login", "")).strip().lower()
        if login and login in approvers and not login.endswith("[bot]") and login not in bot_set:
            return login
    raise Refused("no approval of Environment %r by a listed approver in this run: the Environment may have no required reviewers" % environment)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_hash(path: str, expected: str) -> None:
    exp = (expected or "").strip().lower()
    if exp.startswith("sha256:"):
        exp = exp[7:]
    if not _SHA256.match(exp):
        raise Refused("expected hash %r is not a sha256 hex digest" % expected)
    if not os.path.isfile(path):
        raise Refused("%s is not a file" % path)
    if not hmac.compare_digest(sha256_file(path), exp):
        raise Refused("%s does not match the approved hash: it changed after it was planned" % path)


def _load_plan(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Refused("cannot read plan JSON %s (%s)" % (path, exc))
    if not isinstance(data, dict) or "resource_changes" not in data and "format_version" not in data:
        raise Refused("%s is not terraform plan JSON" % path)
    return data


def plan_digest(plan: Dict[str, Any]) -> str:
    """sha256 over what the plan changes: resource changes, output changes, variables. Timestamps and the
    tool version are left out, so two plans of the same state and source have the same digest."""
    keep = []  # type: List[Dict[str, Any]]
    for rc in plan.get("resource_changes") or []:
        ch = rc.get("change") or {}
        keep.append({
            "address": rc.get("address"), "mode": rc.get("mode"), "type": rc.get("type"), "provider": rc.get("provider_name"),
            "deposed": rc.get("deposed"), "actions": ch.get("actions"), "before": ch.get("before"), "after": ch.get("after"),
            "after_unknown": ch.get("after_unknown"), "before_sensitive": ch.get("before_sensitive"),
            "after_sensitive": ch.get("after_sensitive"), "replace_paths": ch.get("replace_paths"),
        })
    keep.sort(key=lambda r: (str(r["address"]), str(r["deposed"])))
    doc = {"resource_changes": keep, "output_changes": plan.get("output_changes") or {}, "variables": plan.get("variables") or {}}
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _action(actions: Any) -> str:
    a = [str(x) for x in actions or []]
    if a == ["no-op"] or not a:
        return "no-op"
    if "delete" in a and "create" in a:
        return "replace"
    return a[0] if len(a) == 1 else "+".join(a)


def _cell(text: Any, limit: int = 160) -> str:
    s = re.sub(r"[\r\n\t]+", " ", str(text))
    s = s.replace("|", "\\|").replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")
    return s[:limit] + ("..." if len(s) > limit else "")


def plan_summary(plan: Dict[str, Any], title: str = "Infrastructure plan preview") -> str:
    """Values-free markdown: counts and (address, action). Safe to post to a pull request or an issue."""
    rows = []  # type: List[tuple]
    counts = {}  # type: Dict[str, int]
    for rc in plan.get("resource_changes") or []:
        act = _action((rc.get("change") or {}).get("actions"))
        if act == "no-op":
            continue
        counts[act] = counts.get(act, 0) + 1
        rows.append((str(rc.get("address", "?")), act))
    rows.sort()
    lines = [COMMENT_MARKER, "### %s" % _cell(title, 80), ""]
    if not rows:
        lines.append("No resource changes.")
    else:
        lines.append("Changes: " + ", ".join("%d %s" % (counts[k], k) for k in sorted(counts)) + ".")
        lines += ["", "| Resource | Action |", "|---|---|"]
        for addr, act in rows[:MAX_ROWS]:
            lines.append("| `%s` | %s |" % (_cell(addr), _cell(act, 30)))
        if len(rows) > MAX_ROWS:
            lines.append("| ... %d more | |" % (len(rows) - MAX_ROWS))
    lines += ["", "Plan digest: `%s`" % plan_digest(plan),
              "", "This is a preview. It is not what gets applied: the plan that is applied is made again from `main` after merge and approved by a person. Values are not shown."]
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Checks the infrastructure workflow templates call (report §8).")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("authorize")
    a.add_argument("--actor", action="append", default=[])
    a.add_argument("--approvers", default="")
    a.add_argument("--bot", action="append", default=[])
    b = sub.add_parser("base-is-main")
    b.add_argument("--planned-sha", required=True)
    b.add_argument("--main-sha", required=True)
    r = sub.add_parser("require-main-ref")
    r.add_argument("--ref", required=True)
    q = sub.add_parser("require-run-approval")
    q.add_argument("--approvals-json", required=True)
    q.add_argument("--environment", required=True)
    q.add_argument("--approvers", default="")
    q.add_argument("--bot", action="append", default=[])
    h = sub.add_parser("hash-file")
    h.add_argument("file")
    v = sub.add_parser("verify-hash")
    v.add_argument("--file", required=True)
    v.add_argument("--sha256", required=True)
    d = sub.add_parser("plan-digest")
    d.add_argument("--plan-json", required=True)
    s = sub.add_parser("plan-summary")
    s.add_argument("--plan-json", required=True)
    s.add_argument("--title", default="Infrastructure plan preview")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_usage(sys.stderr)
        return 2
    try:
        if args.cmd == "authorize":
            authorize(args.actor, args.approvers, args.bot)
            print("authorized: %s" % ", ".join(args.actor))
        elif args.cmd == "base-is-main":
            base_is_main(args.planned_sha, args.main_sha)
            print("base is main: %s" % args.planned_sha.strip().lower()[:12])
        elif args.cmd == "require-main-ref":
            require_main_ref(args.ref)
            print("ref ok")
        elif args.cmd == "require-run-approval":
            try:
                with open(args.approvals_json, "r", encoding="utf-8") as fh:
                    approvals = json.load(fh)
            except (OSError, ValueError) as exc:
                raise Refused("cannot read the approvals document (%s)" % exc)
            print("approved by %s" % require_run_approval(approvals, args.environment, args.approvers, args.bot))
        elif args.cmd == "hash-file":
            print(sha256_file(args.file))
        elif args.cmd == "verify-hash":
            verify_hash(args.file, args.sha256)
            print("hash ok")
        elif args.cmd == "plan-digest":
            print(plan_digest(_load_plan(args.plan_json)))
        elif args.cmd == "plan-summary":
            sys.stdout.write(plan_summary(_load_plan(args.plan_json), args.title))
    except Refused as exc:
        sys.stderr.write("gate %s: refused: %s\n" % (args.cmd, exc))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - fail closed
        sys.stderr.write("gate: internal error: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
