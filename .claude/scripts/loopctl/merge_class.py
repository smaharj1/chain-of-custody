"""The L5 eligibility classifier (report 6.2 L5, 6.1 H3, 1 (e), 19.7 CI-fix; DEC-3, DEC-33, DEC-38; B5, RC-1; IMP-1). DESIGN-ONLY enablement.

THIS MODULE ONLY ANSWERS A QUESTION: "if auto-merge existed and were switched on, would this item be allowed to use it?" It has no side effect. NO CODE
PATH IN THIS KIT MERGES ANYTHING: `merge_policy` defaults to `human`, `gh pr merge` is denied to every agent (guards/g_git.py), and the answer here
always says `action: human-merge`. The classifier exists so that a human can see, per item, why the low-risk lane would or would not apply, and so that the
doctor and the rules for turning the lane on are written down and tested before anyone is tempted to.

Every rule must hold. A missing fact is a failed rule (fail closed). The rules, in the order they print:

  policy      merge_policy is `auto-low-risk` (an H6 key; the registry also refuses it until the evidence gate passes)
  level       autonomy_level is L5 AND the doctor's effective level is L5 (the doctor owns the caps: credentials, sandbox, B5, platform, evidence)
  evidence    docs/baseline/evidence.json is complete
  promotion   `promotion_attested` holds the date a human confirmed the 6.3 bar
  mutation    `gates.g4` is explicitly `on` (G4 mutation testing, L5 prerequisite)
  deploy      B5: no ungated deploy, judged from the attestation in loop.config (`deploy_triggers`, `git_integrated_hosts`), and the surface is attested.
              A repo that deploys on merge to main without a human gate NEVER passes: there, even a docs merge is a production deploy (RC-1, DEC-38)
  paths       every changed file is low-risk: it matches `low_risk_paths` and NONE of: always_human_paths, the H3 list (migrations, infra, lockfiles, test
              and lint config, `.github/`, `.claude/`, ...), the deploy-surface floor, or a security-sensitive word (auth, payment, schema, ...). Paths
              are compared case-insensitively (APFS and NTFS fold case), a path with `..`, a backslash or an absolute path fails, and a symlink or
              submodule entry is never low-risk
  item        riskClass is low; no migration; no schema, dependency or sensitive-domain footprint; no active waiver
  gates       every gate that is `on` or `report` at the base has a PASS recorded for THIS head (a stale result is no result; G4, G9, G10 and the
              Docs Delta gate may instead have SKIPPED, having had nothing to judge); G4 is always among them
  ci_fix      no CI-fix commit on the item (report 19.7: auto-merge stays off after a CI fix until a fresh review, which is a human act here)
  flag        anything user-visible (a copy file that is not a document) names a feature flag in `ext.featureFlag` (CO-26)

Reads config from the BASE sha (CO-11) like every guardrail key. Pure functions over a `facts` dict, so every rule is testable without a repository.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import common, lgit

__all__ = ["SCHEMA", "classify", "deploy_verdict", "path_rows", "changed_files", "facts_from_context", "SENSITIVE_RE", "DOC_EXTS", "SENSITIVE_DOMAINS"]

SCHEMA = "loopctl.merge-class.v1"
DOC_EXTS = (".md", ".rst", ".adoc", ".png", ".jpg", ".jpeg", ".gif", ".webp")      # text and plain images: nothing that runs. An svg can carry a script; it is copy
SENSITIVE_DOMAINS = ("database", "db", "infra", "infrastructure", "security", "auth", "authentication", "authorization", "payments", "payment", "billing",
                     "migrations", "schema", "devops", "ci", "deploy")
# Words that make a path someone else's business (DEC-3: never infra, schema, auth, payments, .claude/). Matched in any path segment, folded to lower case.
SENSITIVE_RE = re.compile(r"(?:^|[^a-z0-9])(auth\w*|oauth\w*|login|logout|session|passw\w*|credential\w*|secret\w*|token\w*|payment\w*|billing|checkout|stripe|"
                          r"invoice\w*|permission\w*|rbac|acl|crypt\w*|security|schema\w*|migrat\w*|infra\w*|deploy\w*|release\w*|terraform|k8s|helm|docker\w*)(?:$|[^a-z0-9])")
_GATES_REQUIRED_PASS = ("pass",)
_FLAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_SKIP_OK = ("g4", "g9", "g10", "docs")           # gates that legitimately have nothing to judge (no production code, no dependency change, ...); any other gate must have PASSED


# -- B5, from the attestation -------------------------------------------------------------------------------------------------------

def _fires(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").replace(",", " ").split() if p.strip()]


def deploy_verdict(get: Callable[[str, Any], Any]) -> Dict[str, Any]:
    """{ok, reasons[]}. ok only when the human attested the deployment surface and every trigger and host in the attestation is gated by an
    Environment (or declared not to deploy). Anything else, including a malformed or empty-looking entry, is ungated. `get(name, default)` is the config."""
    reasons = []  # type: List[str]
    at = str(get("deploy_surface.attested_at", "") or "").strip()
    fp = str(get("deploy_surface.fingerprint", "") or "").strip()
    if not at or not fp:
        reasons.append("the deployment surface is not attested (deploy_surface.attested_at and .fingerprint are empty): nobody has shown what deploys on a merge")
    triggers = get("deploy_triggers", []) or []
    hosts = get("git_integrated_hosts", []) or []
    if not isinstance(triggers, list) or not isinstance(hosts, list):
        return {"ok": False, "reasons": reasons + ["deploy_triggers and git_integrated_hosts must be lists"]}
    for i, e in enumerate(triggers):
        if not isinstance(e, dict) or not e.get("workflow") or not e.get("fires_on") or not e.get("gate"):
            reasons.append("deploy_triggers[%d] is malformed (needs workflow, fires_on, gate): it counts as an ungated deploy" % i)
            continue
        fires, gate = _fires(e.get("fires_on")), e.get("gate")
        if fires == ["none"]:
            continue                                                    # attested as not deploying
        if gate != "gated-by-environment":
            reasons.append("%s fires on %s with gate %s: that is a deploy no human approves" % (
                common.clean_text(e.get("workflow"), 80), ", ".join(common.clean_text(f, 30) for f in fires[:4]), common.clean_text(gate, 30)))
    for i, e in enumerate(hosts):
        if not isinstance(e, dict) or not e.get("host"):
            reasons.append("git_integrated_hosts[%d] is malformed (needs host): it counts as an ungated deploy" % i)
            continue
        prev, prod = str(e.get("previews_on", "unknown")).lower(), str(e.get("production_on", "unknown")).lower()
        if prev in ("none", "label") and prod == "none":
            continue                                                    # a host that deploys nothing on a push
        if e.get("gate") != "gated-by-environment":
            reasons.append("%s deploys previews on %s and production on %s with gate %s" % (
                common.clean_text(e.get("host"), 40), common.clean_text(prev, 20), common.clean_text(prod, 20), common.clean_text(e.get("gate"), 30)))
    return {"ok": not reasons, "reasons": reasons}


# -- paths ---------------------------------------------------------------------------------------------------------------------------

def _rx(glob: str) -> Any:
    import pr_prepare
    return pr_prepare.glob_to_regex(glob)


def _floor_match(pattern: str, low: str) -> bool:
    """The deploy-surface floor's matching, over-approximated on purpose: `*` crosses `/`, and a pattern matches at any depth (a monorepo keeps
    apps/web/vercel.json). Both sides are lower case."""
    body = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return bool(re.match("^" + body + "$", low, re.S) or re.match("^(?:.*/)?" + body + "$", low, re.S))


def _human_reason(path: str, extra_globs: Sequence[str]) -> Optional[str]:
    """Why a person must review this path, or None. Case-insensitive: the path and each pattern are folded."""
    import pr_prepare
    low = path.lower()
    for pat, why in pr_prepare.HUMAN_PATTERNS:
        if _rx(pat.lower()).match(low):
            return why
    for g in extra_globs:
        if isinstance(g, str) and g and _rx(g.lower()).match(low):
            return "project always-human path (always_human_paths)"
    for pat in pr_prepare._deploy_floor():
        if _floor_match(pat.lower(), low):
            return "deploy surface (H3, RC-1)"
    return None


def path_rows(files: Sequence[Any], low_risk: Sequence[str], always_human: Sequence[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Per changed file: {path, ok, class: docs|copy|None, why}. `files` are paths or {path, old_mode, new_mode} dicts. Returns (rows, summary class)."""
    rows = []  # type: List[Dict[str, Any]]
    classes = set()  # type: set
    for f in files:
        path = f.get("path") if isinstance(f, dict) else f
        modes = (f.get("old_mode"), f.get("new_mode")) if isinstance(f, dict) else (None, None)
        row = {"path": common.clean_text(path, 120), "ok": False, "class": None, "why": ""}
        if not common.safe_rel_path(path):
            row["why"] = "not a plain relative path inside the repository"
        elif any(m in ("120000", "160000") for m in modes):
            row["why"] = "a symbolic link or submodule (it can point anywhere)"
        else:
            human = _human_reason(path, always_human)
            low = path.lower()
            is_doc = low.endswith(DOC_EXTS)
            sens = None if is_doc else SENSITIVE_RE.search(low)           # a document that talks about auth is still only text
            if human:
                row["why"] = human
            elif sens:
                row["why"] = "its path names %s (auth, payments, schema, infra and the like are never low-risk)" % common.clean_text(sens.group(1), 20)
            elif not any(isinstance(g, str) and g and _rx(g.lower()).match(low) for g in low_risk):
                row["why"] = "not under low_risk_paths"
            else:
                row["ok"], row["class"] = True, "docs" if is_doc else "copy"
                classes.add(row["class"])
        rows.append(row)
    return rows, ("copy" if "copy" in classes else ("docs" if classes else ""))


# -- the rules ------------------------------------------------------------------------------------------------------------------------

def _rule(rid: str, ok: bool, msg: str) -> Dict[str, Any]:
    return {"id": "merge-class." + rid, "ok": bool(ok), "message": msg}


def classify(facts: Dict[str, Any]) -> Dict[str, Any]:
    """The verdict for one item. `eligible` is True only if every rule holds. `action` is always `human-merge`: this build merges nothing."""
    rules = []  # type: List[Dict[str, Any]]
    item = facts.get("item") if isinstance(facts.get("item"), dict) else {}
    sitem = facts.get("state_item") if isinstance(facts.get("state_item"), dict) else {}

    policy = facts.get("policy")
    rules.append(_rule("policy", policy == "auto-low-risk",
                       "merge_policy is auto-low-risk" if policy == "auto-low-risk" else "merge_policy is %s: a person merges (the default, and the only setting that merges anything)" % common.clean_text(policy, 30)))
    lvl, eff = facts.get("autonomy_level"), facts.get("effective_level")
    if lvl != "L5":
        rules.append(_rule("level", False, "autonomy_level is %s, not L5" % common.clean_text(lvl, 10)))
    elif eff is None:
        rules.append(_rule("level", False, "the doctor's effective level was not computed: an unknown level is not L5"))
    else:
        rules.append(_rule("level", eff == "L5", "the doctor's effective level is %s" % common.clean_text(eff, 10) + ("" if eff == "L5" else " (L5 needs every prerequisite and cap clear: `loopctl doctor --level`)")))
    rules.append(_rule("evidence", bool(facts.get("evidence")), "the evidence gate passed" if facts.get("evidence") else "the evidence gate has not passed (docs/baseline/evidence.json)"))
    prom = str(facts.get("promotion_attested") or "").strip()
    rules.append(_rule("promotion", bool(_DATE_RE.match(prom)), "the promotion bar was attested on %s" % prom if _DATE_RE.match(prom) else "promotion_attested is empty or not a date (6.3 bar)"))
    mut = facts.get("mutation") or {}
    rules.append(_rule("mutation", mut.get("explicit") is True and mut.get("mode") == "on",
                       "gates.g4 is on" if mut.get("explicit") is True and mut.get("mode") == "on" else "gates.g4 is not explicitly `on` (mutation testing is an L5 prerequisite)"))
    dv = facts.get("deploy") or {"ok": False, "reasons": ["the deployment surface was not judged"]}
    rules.append(_rule("deploy", bool(dv.get("ok")), "no ungated deploy: B5 holds" if dv.get("ok") else "B5: %s" % common.clean_text("; ".join(dv.get("reasons") or ["not judged"])[:300], 300)))

    files = facts.get("files") if facts.get("files") is not None else facts.get("paths")
    rows, cls = path_rows(files or [], facts.get("low_risk_paths") or [], facts.get("always_human_paths") or [])
    bad = [r for r in rows if not r["ok"]]
    if not rows:
        rules.append(_rule("paths", False, "no changed file was given: there is nothing to classify (an empty diff is not low-risk, it is unknown)"))
    elif bad:
        rules.append(_rule("paths", False, "%d of %d changed file(s) are not low-risk: %s" % (len(bad), len(rows), "; ".join("%s (%s)" % (r["path"], r["why"]) for r in bad[:3]))))
    else:
        rules.append(_rule("paths", True, "all %d changed file(s) are low-risk (%s)" % (len(rows), cls)))

    touches = item.get("touches") if isinstance(item.get("touches"), dict) else {}
    why_item = []  # type: List[str]
    if item.get("riskClass") != "low":
        why_item.append("riskClass is %s" % common.clean_text(item.get("riskClass"), 12))
    if item.get("migrations"):
        why_item.append("it has a migration")
    if touches.get("schema") is not False:
        why_item.append("touches.schema is not false")
    if touches.get("deps_change") is not False:
        why_item.append("touches.deps_change is not false")
    doms = [str(d).lower() for d in touches.get("domains", []) if isinstance(d, str)] if isinstance(touches.get("domains"), list) else None
    if doms is None:
        why_item.append("touches.domains is missing")
    elif [d for d in doms if d in SENSITIVE_DOMAINS]:
        why_item.append("it touches %s" % ", ".join(d for d in doms if d in SENSITIVE_DOMAINS))
    if item.get("waivers"):
        why_item.append("it carries a waiver")
    if not item:
        why_item = ["the plan item was not found"]
    rules.append(_rule("item", not why_item, "a low-risk item with no migration, schema, dependency or sensitive-domain footprint" if not why_item else "; ".join(why_item)))

    gate_problems = []  # type: List[str]
    modes = facts.get("gate_modes") or {}
    recorded = sitem.get("gates") if isinstance(sitem.get("gates"), dict) else {}
    head = facts.get("head")
    wanted = sorted(n for n, m in modes.items() if m in ("on", "report"))          # a gate in `report` does not block a person, but a failure in it blocks a machine
    if "g4" not in wanted:
        wanted.append("g4")
    for n in wanted:
        rec = recorded.get(n)
        if not isinstance(rec, dict):
            gate_problems.append("%s has no recorded result" % n)
        elif not head or rec.get("head") != head:
            gate_problems.append("%s was run on another head (%s), not %s" % (n, common.clean_text(str(rec.get("head"))[:12], 12), common.clean_text(str(head)[:12], 12)))
        elif rec.get("status") not in _GATES_REQUIRED_PASS and not (rec.get("status") == "skipped" and n in _SKIP_OK):
            gate_problems.append("%s is %s" % (n, common.clean_text(rec.get("status"), 12)))
        elif rec.get("blocking"):
            gate_problems.append("%s is blocking" % n)
    rules.append(_rule("gates", not gate_problems, "every gate that is on passed for this head" if not gate_problems else "; ".join(gate_problems[:4])))

    ci = (sitem.get("counters") or {}).get("ci_fix", 0) if isinstance(sitem.get("counters"), dict) else 0
    rules.append(_rule("ci_fix", ci == 0, "no CI-fix commit" if ci == 0 else "%s CI-fix attempt(s): auto-merge stays off after a CI fix until a person has re-reviewed (19.7)" % common.clean_text(ci, 6)))

    ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
    flag = ext.get("featureFlag")
    if cls == "copy":
        ok_flag = isinstance(flag, str) and bool(_FLAG_RE.match(flag))
        rules.append(_rule("flag", ok_flag, "user-visible copy sits behind the feature flag %s" % common.clean_text(flag, 40) if ok_flag
                           else "user-visible copy needs `ext.featureFlag` on the item (CO-26)"))
    else:
        rules.append(_rule("flag", True, "documents only: nothing user-visible to hide behind a flag"))

    failing = [r["message"] for r in rules if not r["ok"]]
    return {"schema": SCHEMA, "item": facts.get("item_id"), "eligible": not failing, "class": cls or None, "action": "human-merge", "merges": False,
            "head": head, "rules": rules, "reasons": failing, "files": rows,
            "note": "Eligible means the low-risk lane would apply if it existed. Nothing here merges: a person merges every pull request."}


# -- gathering the facts from a repository ----------------------------------------------------------------------------------------------

def changed_files(repo: str, base: str, head: str) -> List[Dict[str, Any]]:
    """[{path, status, old_mode, new_mode}] for `base..head`, renames broken into delete + add so both names are judged. NUL-separated."""
    proc = lgit.run_git(["diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff", "--no-textconv", base, head, "--"], cwd=repo, binary=True)
    if proc.returncode != 0:
        raise common.Refused("git diff failed: %s" % common.clean_text(proc.stderr.decode("utf-8", "replace"), 120))
    parts = proc.stdout.split(b"\0")
    out = []  # type: List[Dict[str, Any]]
    i = 0
    while i + 1 < len(parts):
        meta = parts[i].decode("ascii", "replace")
        raw_path = parts[i + 1]
        i += 2
        m = re.match(r"^:(\d{6}) (\d{6}) [0-9a-f]+ [0-9a-f]+ ([A-Z])\d*$", meta)
        if not m:
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            path = "<non-utf8 path>"
        out.append({"path": path, "status": m.group(3), "old_mode": m.group(1), "new_mode": m.group(2)})
    return out


def facts_from_context(ctx: Any, iid: str, head: Optional[str], effective_level: Optional[str], files: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Everything `classify` needs, read from the base sha's config and plan, the state, and the diff `base..head`."""
    cfg = ctx.cfg
    item = (ctx.plan.items.get(iid) if ctx.plan and not ctx.plan.fatal else None) or {}
    sitem = {}  # type: Dict[str, Any]
    try:
        st, _ = ctx.store().read()
        if st is not None and iid in st.items:
            sitem = st.items[iid]
    except common.LoopctlError:
        sitem = {}
    if files is None:
        files = changed_files(ctx.repo, ctx.base_sha, head) if head and ctx.base_sha else []
    gate_modes = {}  # type: Dict[str, str]
    from . import gates as gates_mod
    for g in gates_mod.installed():
        gate_modes[g] = gates_mod.mode_for(cfg, g, gates_mod.get(g))
    return {"item_id": iid, "item": item, "state_item": sitem, "policy": cfg.get("merge_policy"), "autonomy_level": str(cfg.get("autonomy_level") or ""),
            "effective_level": effective_level, "evidence": ctx.evidence_passed(), "promotion_attested": cfg.get("promotion_attested"),
            "mutation": {"explicit": cfg.is_set("gates.g4"), "mode": cfg.get("gates.g4") if cfg.is_set("gates.g4") else "off"},
            "deploy": deploy_verdict(cfg.get), "files": list(files), "low_risk_paths": cfg.get_list("low_risk_paths"),
            "always_human_paths": cfg.get_list("always_human_paths"), "head": head, "gate_modes": gate_modes}
