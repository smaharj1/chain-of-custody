"""`loopctl gaps`: gaps that repeat in the run data, drafted as rule or primer changes for a person to review (report 19.7 L-33, 16.1 row 10; DEC-28, H6).

What it does
    1. `detect(data)` finds what repeats: engineers' brief gaps (L-33), non-empty Primer Deltas, a gate finding that blocks again and again,
       an item-class failure reason that recurs, `env` failures, report form problems, a critic with low precision, a primer going stale.
    2. `plan_drafts(gaps)` decides what to draft for each: a RULE skeleton (`.claude/rules/<domain>-<kind>.md`, from `${CLAUDE_PLUGIN_ROOT}/templates/rule.md`)
       or a PROPOSAL (a markdown note for a primer, a rubric or the doctor).
    3. `write_drafts(...)` writes them under `<git-common-dir>/loopctl/gaps/<id>/`, never anywhere else, with the git and gh commands in `COMMANDS.txt`.
    4. `commands(...)` returns the commands for a person to run. loopctl never runs them (H6): it does not commit, push, switch branches or open
       a pull request, and this module starts no process at all.

What it must not do, and how that is held
    * Write outside its draft directory, or a file a guardrail owns (hooks, settings, agents, commands, skills, the kit's own files, `loop.config`,
      rulesets and workflows, `docs/baseline/evidence.json`). A drafted file's target is checked twice: it has to match the one pattern a rule
      may have (`.claude/rules/<name>.md`), and it must not match the protected list. Every write is exclusive-create, refuses a symlink on the way,
      and checks the real path stays under the draft directory.
    * Let log text become an instruction. A rule's words, its file name and its path suggestions come from closed vocabularies and the approved
      plan, with `<fill>` where a person must think. The quoted evidence (report sections, gate messages) is in `README.md` and the proposals only,
      in a fenced block labelled UNTRUSTED DATA (`learning.fence`). `scan_text` is the check the tests run over every drafted rule.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import common, failure
from . import calibration, fence, inline, token, trailers
from .data import RunData

__all__ = ["SCHEMA", "KINDS", "PROTECTED", "RULE_TARGET_RE", "PRIMERS", "DOMAINS", "detect", "plan_drafts", "target_problem", "write_drafts", "commands",
           "draft_id", "DraftDir", "GapError", "MAX_DRAFTS"]

SCHEMA = "loopctl.gaps.v1"
KINDS = ("brief-gap", "primer-delta", "gate-repeat", "failure-repeat", "env-failure", "trailer-form", "critic-precision", "empty-delta")
MAX_DRAFTS = 12
DOMAINS = ("database", "backend", "app", "admin-app", "infra")
# domain -> the primer a delta for it belongs to (a closed map: a report's file name never chooses a path)
PRIMERS = {"database": "database.md", "backend": "backend.md", "app": "app.md", "admin-app": "admin-app.md", "infra": "infra.md"}
RULE_TARGET_RE = re.compile(r"^\.claude/rules/[a-z0-9][a-z0-9-]{0,40}\.md\Z")
# Defence in depth: even if the pattern above were widened, none of these may be a drafted target (H6, B3, CS-25).
PROTECTED = (".claude/settings", ".claude/hooks/", ".claude/scripts/", ".claude/agents/", ".claude/commands/", ".claude/skills/", ".claude/loop.config",
             ".claude/CLAUDE.md", ".claude/spec/", ".claude/templates/", ".claude/context/", ".claude/launch.json", ".claude-plugin/", "plugin/", ".mcp.json", ".github/",
             "docs/baseline/", "docs/security/", "CODEOWNERS", ".gitignore", ".gitattributes", ".git/", ".worktreeinclude", "plan/", "evals/", "tools/", "tests/")
TOP_LEVEL = ("README.md", "COMMANDS.txt", "PR_BODY.md", "MANIFEST.json")
_PROPOSAL_RE = re.compile(r"^proposals/[0-9a-z][0-9a-z-]{0,60}\.md\Z")
_GLOB_RE = re.compile(r"^[A-Za-z0-9_./*?@+-]{1,80}\Z")          # no quote, newline, space or `#`: a glob goes into YAML front matter and an HTML comment
_GATE_ID_RE = re.compile(r"^(?:g\d{1,2}|evidence|docs)\.[a-z0-9][a-z0-9-]{0,30}\Z")
_ITEM_FAILURES = ("tests_failed", "review_blocked", "ac_unclear", "malformed_report", "reward_hack", "partial", "lint_failed", "build_failed", "ci_failed", "design_blocked")
_SUGGEST = {
    "brief-gap": "Engineers keep finding the same hole in their briefs. Decide the invariant the gaps point at and write it as a rule, so the next brief does not need to say it.",
    "primer-delta": "Engineers report that this primer disagrees with the code. Read each delta against the code, then update the primer (the Tech Lead's wrap-up step) or reject it.",
    "gate-repeat": "The same gate finding blocks different items. Decide whether the rule it enforces belongs in the briefs (a rule) or in the primer's exemplar.",
    "failure-repeat": "The same item-class failure recurs. Decide whether the briefs, a primer or a rule would prevent it.",
    "env-failure": "Environment failures recur. They belong to the doctor, `local-dev.md` and the toolchain pins (G0), not to a rule: check which of the three missed it.",
    "trailer-form": "Engineers keep getting the report or trailer form wrong. Check the engineer protocol's wording; when the clean streak is long enough, `report_trailers: block` is the next step.",
    "critic-precision": "A critic's blockers are mostly not fixed. Read the false positives and overruled ones, then sharpen that critic's rubric examples (an agent file: H6, a person edits it).",
    "empty-delta": "Several items added structure and reported no primer change. The primers are probably stale: refresh them from the code before the next milestone.",
}


class GapError(common.LoopctlError):
    exit_code = common.EXIT_REFUSED


# -- detection -------------------------------------------------------------------------------------------------------

def _domains(data: RunData) -> List[str]:
    out = list(DOMAINS)
    for it in data.plan_items.values():
        t = it.get("touches") if isinstance(it.get("touches"), dict) else {}
        for d in t.get("domains") if isinstance(t.get("domains"), list) else []:
            if isinstance(d, str) and token(d) == d and d not in out:
                out.append(d)
    return out


def _meaningful(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    t = text.strip()
    head = t.split(None, 1)[0].strip(".:-").lower() if t else ""
    return bool(t) and head not in ("none", "n/a", "na", "nothing", "no")


def _globs(data: RunData, items: Sequence[str]) -> List[str]:
    out = []  # type: List[str]
    for iid in items:
        t = (data.plan_items.get(iid) or {}).get("touches")
        for g in (t.get("paths") if isinstance(t, dict) and isinstance(t.get("paths"), list) else []):
            if isinstance(g, str) and _GLOB_RE.match(g) and common.safe_glob(g) and g not in out:
                out.append(g)
    return out[:6]


def detect(data: RunData, min_occ: int = 2) -> List[Dict[str, Any]]:
    """The gaps the data shows, most frequent first. Each is {kind, key, count, items, domain, globs, evidence: [(label, text)], numbers}. Only
    `evidence` holds untrusted text; every other field is a count, a validated token or an id the plan owns."""
    min_occ = max(1, int(min_occ))
    domains = _domains(data)
    out = []  # type: List[Dict[str, Any]]

    def gap(kind: str, key: str, items: Sequence[str], domain: str, evidence: Sequence[Tuple[str, str]], count: Optional[int] = None, **numbers: Any) -> None:
        its = sorted(set(items), key=common.natural_key)
        out.append({"kind": kind, "key": key, "count": count if count is not None else len(its), "items": its, "domain": domain, "globs": _globs(data, its),
                    "evidence": [(l, t) for l, t in evidence][:3], "numbers": numbers})

    by_dom = {}  # type: Dict[str, List[Dict[str, Any]]]
    for rep in data.reports:
        by_dom.setdefault(token(rep.get("domain"), domains), []).append(rep)
    for dom in sorted(by_dom):
        reps = by_dom[dom]
        g = [r for r in reps if _meaningful(r.get("gaps"))]
        if len({r["item"] for r in g}) >= min_occ:
            gap("brief-gap", dom, [r["item"] for r in g], dom, [("report %s" % r["path"], r.get("gaps", "")) for r in g], count=len(g))
        p = [r for r in reps if r.get("primer_delta_text")]
        if p:
            gap("primer-delta", dom, [r["item"] for r in p], dom, [("report %s" % r["path"], r.get("primer_delta_text", "")) for r in p], count=len(p))

    gate_items = {}  # type: Dict[str, set]
    for e in data.events:
        if e.get("event") == "gate" and e.get("blocking") is True and isinstance(e.get("item"), str):
            for fid in e.get("ids") if isinstance(e.get("ids"), list) else []:
                if isinstance(fid, str) and _GATE_ID_RE.match(fid):
                    gate_items.setdefault(fid, set()).add(e["item"])
    for iid, r in data.items.items():
        for gname, gres in (r.get("gates") or {}).items() if isinstance(r, dict) else []:
            if isinstance(gres, dict) and gres.get("blocking") and gres.get("status") in ("fail", "error"):
                for f in gres.get("findings") or []:
                    fid = f.get("id") if isinstance(f, dict) else None
                    if isinstance(fid, str) and _GATE_ID_RE.match(fid):
                        gate_items.setdefault(fid, set()).add(iid)
    for fid in sorted(gate_items):
        if len(gate_items[fid]) >= min_occ:
            ev = []  # type: List[Tuple[str, str]]
            for iid in sorted(gate_items[fid], key=common.natural_key)[:3]:
                for gname, gres in ((data.items.get(iid) or {}).get("gates") or {}).items():
                    for f in (gres.get("findings") or []) if isinstance(gres, dict) else []:
                        if isinstance(f, dict) and f.get("id") == fid and f.get("message"):
                            ev.append(("gate finding %s on item %s" % (fid, iid if common.valid_item_id(iid) else "?"), str(f["message"])))
            gap("gate-repeat", fid, sorted(gate_items[fid]), "other", ev)

    fail_items = {}  # type: Dict[str, set]
    env_items = set()  # type: set
    for e in data.events:
        if e.get("event") == "fail" and isinstance(e.get("item"), str):
            reason = token(e.get("reason"), failure.KNOWN_REASONS)
            if e.get("failure_class") == failure.ENV:
                env_items.add(e["item"])
            elif reason in _ITEM_FAILURES:
                fail_items.setdefault(reason, set()).add(e["item"])
    for reason in sorted(fail_items):
        if len(fail_items[reason]) >= min_occ:
            gap("failure-repeat", reason, sorted(fail_items[reason]), "other", [])
    if len(env_items) >= min_occ:
        gap("env-failure", "env", sorted(env_items), "other", [])

    tr = trailers.outcomes(data.events)
    warned = [r for r in tr if r["outcome"] == "warned"]
    if len(warned) >= min_occ:
        kinds = {}  # type: Dict[str, int]
        for r in warned:
            for k in r["kinds"]:
                kinds[k] = kinds.get(k, 0) + 1
        gap("trailer-form", "report-form", [r["item"] for r in warned if r["item"]], "other", [], count=len(warned), kinds=kinds)

    n_min = data.cfg.get("calibration_min_samples")
    for c in calibration.report(data.calibration, n_min if isinstance(n_min, int) and not isinstance(n_min, bool) else 5)["critics"]:
        if c["verdict"] == "low-precision":
            gap("critic-precision", c["critic"], [], "other", [], count=c["decided"], precision=c["precision"], fixed=c["fixed"], overruled=c["overruled"],
                false_positive=c["false_positive"])
    out.sort(key=lambda g: (-g["count"], KINDS.index(g["kind"]), g["key"]))
    return out


# -- drafts ----------------------------------------------------------------------------------------------------------

def draft_id(gaps: Sequence[Dict[str, Any]]) -> str:
    """Stable for the same findings: running `gaps --write` twice on the same data names the same directory instead of drafting twice."""
    sig = "|".join("%s:%s:%d:%s" % (g["kind"], g["key"], g["count"], ",".join(g["items"])) for g in gaps)
    return "gaps-" + common.sha256_hex(sig)[:12]


def target_problem(rel: Any) -> Optional[str]:
    """Why a drafted file may NOT be written at this repository-relative path, or None when it may. The one allowed shape is a rule file."""
    if not isinstance(rel, str) or not common.safe_rel_path(rel):
        return "not a safe relative path"
    low = rel.lower()
    for p in PROTECTED:
        if low.startswith(p.lower()) or ("/" + p.lower().rstrip("/") + "/") in ("/" + low):
            return "a guardrail owns %s (H6): a person edits it, a draft never does" % p
    if not RULE_TARGET_RE.match(rel):
        return "only .claude/rules/<name>.md can be drafted; everything else is a proposal note"
    return None


_RULE = """---
paths:
%(paths)s
---

# <fill: the invariant, as an imperative sentence>

<!-- DRAFT from `loopctl gaps` %(id)s. Not a rule yet: a person fills every <fill>, checks it against the evidence in the draft directory's README.md,
     and deletes this comment. Nothing in this file was copied from the logs. -->
%(suggest)s
- <fill: what to do, in one or two lines.>
- <fill: the one exception, if there is one, and how to recognise it.>
- Why: <fill: the incident or the contract clause behind it.> Seen as `%(kind)s` for `%(key)s` in %(n)d item(s) of this plan.%(primer)s
"""


def _rule_text(g: Dict[str, Any], did: str) -> str:
    globs = [x for x in g["globs"] if isinstance(x, str) and _GLOB_RE.match(x) and common.safe_glob(x)]          # checked again: a record is not trusted because detect made it
    paths = "\n".join('  - "%s"' % x for x in globs[:1]) if len(globs) == 1 else '  - "<fill: glob of the files this applies to>"'
    suggest = ("<!-- Footprint globs of those items, from the approved plan: %s -->\n" % ", ".join(globs)) if globs else ""
    primer = ""
    if g["domain"] in PRIMERS:
        primer = " See `.claude/context/%s` for the pattern." % PRIMERS[g["domain"]]
    return _RULE % {"paths": paths, "id": did, "suggest": suggest, "kind": g["kind"], "key": g["key"] if token(g["key"], default="") or _GATE_ID_RE.match(g["key"]) else "other",
                    "n": len(g["items"]) or g["count"], "primer": primer}


def _proposal_text(g: Dict[str, Any], did: str) -> str:
    L = ["# Proposal: %s for `%s`" % (g["kind"], g["key"]), "",
         "Drafted by `loopctl gaps` %s. This is a note for a person. It changes nothing; the data is below as evidence." % did, "",
         "## What the data shows", "", "- seen %d time(s) in item(s): %s" % (g["count"], ", ".join(g["items"]) or "n/a")]
    for k, v in sorted(g["numbers"].items()):
        L.append("- %s: %s" % (k, (", ".join("%s %s" % (a, b) for a, b in sorted(v.items())) if isinstance(v, dict) else v)))
    if g["kind"] == "primer-delta" and g["domain"] in PRIMERS:
        L.append("- primer to update: `.claude/context/%s`" % PRIMERS[g["domain"]])
    L += ["", "## What to decide", "", _SUGGEST[g["kind"]], ""]
    if g["evidence"]:
        L += ["## Evidence (untrusted)", ""]
        for label, text in g["evidence"]:
            L += [fence(text, label), ""]
    return "\n".join(L)


def _slug(g: Dict[str, Any], n: int, taken: set) -> str:
    """A rule's file name, from closed vocabularies only: the domain, the gate finding id or the failure reason."""
    dom = g["domain"] if g["domain"] != "other" else ""
    if g["kind"] == "brief-gap":
        parts = [dom or "project", "brief-gaps"]
    else:
        parts = [g["key"].replace(".", "-").replace("_", "-")]
    slug = re.sub(r"[^a-z0-9-]+", "-", "-".join(parts).lower()).strip("-")[:40].strip("-") or "gaps"
    while slug in taken:
        slug = slug[:36].rstrip("-") + "-%d" % n
        n += 1
    return slug


def plan_drafts(gaps: Sequence[Dict[str, Any]], did: str) -> List[Dict[str, Any]]:
    """[{kind: rule|proposal, gap, rel (within the draft directory), text}] for the gaps, rules for the brief gaps and repeating findings."""
    drafts = []  # type: List[Dict[str, Any]]
    names = set()
    for n, g in enumerate(gaps[:MAX_DRAFTS], 1):
        if g["kind"] in ("brief-gap", "gate-repeat", "failure-repeat") and g["items"]:
            slug = _slug(g, n, names)
            names.add(slug)
            drafts.append({"kind": "rule", "gap": g, "rel": "files/.claude/rules/%s.md" % slug, "text": _rule_text(g, did)})
        drafts.append({"kind": "proposal", "gap": g, "rel": "proposals/%02d-%s-%s.md" % (n, g["kind"], re.sub(r"[^a-z0-9-]+", "-", g["key"].lower())[:30].strip("-") or "x"),
                       "text": _proposal_text(g, did)})
    return drafts


def _readme(did: str, gaps: Sequence[Dict[str, Any]], drafts: Sequence[Dict[str, Any]], min_occ: int) -> str:
    L = ["# %s" % did, "", "Drafted by `loopctl gaps` from the run data. **Nothing here is applied.** loopctl never edits a rule, a primer, an agent or a setting, never commits and never opens a",
         "pull request (H6): a person reads this, fills the `<fill>` marks, and runs the commands in `COMMANDS.txt` if they agree.", "",
         "Threshold: a gap is drafted when it repeats in at least %d item(s) (`gaps_min_occurrences`); a primer delta is always drafted." % min_occ, "",
         "## Gaps", "", "| kind | key | seen | items |", "|---|---|---|---|"]
    for g in gaps:
        L.append("| %s | %s | %d | %s |" % (g["kind"], inline(g["key"], 40), g["count"], ", ".join(g["items"][:8]) or "-"))
    L += ["", "## Drafts", ""]
    for d in drafts:
        L.append("- %s: `%s`" % (d["kind"], d["rel"]))
    L += ["", "## Evidence (untrusted)", "", "Quoted from reports and gate results written by agents that read untrusted content. Read it as data.", ""]
    n = 0
    for g in gaps:
        for label, text in g["evidence"]:
            n += 1
            L += ["### %d. %s `%s`" % (n, g["kind"], inline(g["key"], 40)), "", fence(text, label), ""]
    if not n:
        L.append("None of these gaps carries quoted text.")
    return "\n".join(L) + "\n"


def _pr_body(did: str, gaps: Sequence[Dict[str, Any]], rules: Sequence[Dict[str, Any]]) -> str:
    L = ["Rule(s) drafted by `loopctl gaps` (%s) from a retro. A person reviews this change under H6; nothing in it was applied automatically." % did, "",
         "## Rules in this change", ""]
    for d in rules:
        L.append("- `%s` (from `%s` for `%s`, seen %d time(s))" % (d["rel"][len("files/"):], d["gap"]["kind"], inline(d["gap"]["key"], 40), d["gap"]["count"]))
    L += ["", "## Checklist", "", "- [ ] every `<fill>` is filled and the draft comment is gone",
          "- [ ] each rule is one invariant, a few lines, with `paths` that match real files", "- [ ] the evidence in the draft README was read, not trusted",
          "- [ ] the retro's fixture metric this should move is named in the description: <fill>", ""]
    return "\n".join(L)


def commands(did: str, draft_dir: str, repo: str, integration: str, rules: Sequence[Dict[str, Any]]) -> List[str]:
    """The commands a PERSON runs. Quoted for a shell; loopctl prints them and never runs them."""
    q = shlex.quote
    branch = "learning/" + did
    L = ["# These are for you to run, after you have read and filled the drafts. loopctl never runs them (H6).", "cd %s" % q(repo)]
    if not rules:
        L += ["# No rule file was drafted: the proposals in %s are notes. Apply the ones you agree with by hand (a primer is edited by the Tech Lead at wrap-up)." % q(os.path.join(draft_dir, "proposals"))]
        return L
    L += ["git switch -c %s %s" % (q(branch), q(integration)), "cp -R %s/. ." % q(os.path.join(draft_dir, "files"))]
    L += ["# edit the copied file(s): fill every <fill>, delete the DRAFT comment"]
    L += ["git add -- %s" % q(d["rel"][len("files/"):]) for d in rules]
    L += ["git commit -m %s" % q("Add rule drafted from a retro (%s)" % did), "git push -u origin %s" % q(branch),
          "gh pr create --draft --base %s --head %s --title %s --body-file %s" % (q(integration), q(branch), q("Rule from retro %s" % did), q(os.path.join(draft_dir, "PR_BODY.md")))]
    return L


class DraftDir(object):
    """A directory loopctl may write drafts into, and nothing else. Every path is checked before it is created."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.created = False

    def create(self) -> bool:
        """Make the directory (and `gaps/`) without following a symlink. False when it already exists (the same findings were drafted before)."""
        parent = os.path.dirname(self.root)
        for d in (os.path.dirname(parent), parent):
            if os.path.islink(d):
                raise GapError("%s is a symlink: refusing to draft through it" % d)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if os.path.islink(parent):
            raise GapError("%s is a symlink: refusing to draft through it" % parent)
        try:
            os.mkdir(self.root, 0o700)
        except FileExistsError:
            if os.path.islink(self.root) or not os.path.isdir(self.root):
                raise GapError("%s exists and is not a directory: refusing to treat it as an earlier draft" % self.root)
            return False
        self.created = True
        return True

    def _check(self, rel: str) -> str:
        if not common.safe_rel_path(rel):
            raise GapError("%r is not a relative path inside the draft directory" % common.clean_text(rel, 60))
        if rel.startswith("files/"):
            why = target_problem(rel[len("files/"):])
            if why:
                raise GapError("%s cannot be drafted: %s" % (common.clean_text(rel[len("files/"):], 80), why))
        elif rel.startswith("proposals/"):
            if not _PROPOSAL_RE.match(rel):
                raise GapError("%r is not a proposal note name" % common.clean_text(rel, 60))
        elif rel not in TOP_LEVEL:
            raise GapError("%r is outside the draft layout" % common.clean_text(rel, 60))
        full = os.path.join(self.root, rel)
        cur = self.root
        for part in rel.split("/")[:-1]:
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                raise GapError("%s is a symlink: refusing to draft through it" % common.clean_text(rel, 60))
        real = os.path.realpath(os.path.dirname(full))
        if not (real + os.sep).startswith(os.path.realpath(self.root) + os.sep):
            raise GapError("%r would be written outside the draft directory" % common.clean_text(rel, 60))
        return full

    def write(self, rel: str, text: str) -> str:
        full = self._check(rel)
        os.makedirs(os.path.dirname(full), mode=0o700, exist_ok=True)
        full = self._check(rel)                                           # again: the directories exist now, and none of them may be a link
        fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        return full


def write_drafts(rs: Any, gaps: Sequence[Dict[str, Any]], repo: str, integration: str, min_occ: int) -> Dict[str, Any]:
    """Write the drafts for `gaps` and return {id, dir, created, files, rules, commands}. An existing directory for the same findings is reported, not rewritten."""
    did = draft_id(gaps)
    root = os.path.join(rs.root, "gaps", did)
    dd = DraftDir(root)
    drafts = plan_drafts(gaps, did)
    rules = [d for d in drafts if d["kind"] == "rule"]
    cmds = commands(did, root, repo, integration, rules)
    if not dd.create():
        return {"id": did, "dir": root, "created": False, "files": [], "rules": [d["rel"][len("files/"):] for d in rules], "commands": cmds}
    files = []  # type: List[Dict[str, str]]
    for d in drafts:
        dd.write(d["rel"], d["text"])
        files.append({"path": d["rel"], "sha256": common.sha256_hex(d["text"])})
    extra = {"README.md": _readme(did, gaps, drafts, min_occ), "COMMANDS.txt": "\n".join(cmds) + "\n"}
    if rules:
        extra["PR_BODY.md"] = _pr_body(did, gaps, rules)
    for rel, text in extra.items():
        dd.write(rel, text)
        files.append({"path": rel, "sha256": common.sha256_hex(text)})
    manifest = {"schema": SCHEMA, "id": did, "created": common.iso(), "gaps": [{"kind": g["kind"], "key": g["key"], "count": g["count"], "items": g["items"]} for g in gaps],
                "files": files}
    dd.write("MANIFEST.json", common.canonical_json(manifest) + "\n")
    return {"id": did, "dir": root, "created": True, "files": [f["path"] for f in files] + ["MANIFEST.json"], "rules": [d["rel"][len("files/"):] for d in rules], "commands": cmds}


# -- a check the tests run over every drafted rule -----------------------------------------------------------------------------

def scan_text(rule_text: str, untrusted: Sequence[str]) -> List[str]:
    """Problems in a drafted rule: any fragment of the untrusted texts (every 24-character window) found in it. Empty list = clean."""
    flat = re.sub(r"\s+", " ", rule_text)
    bad = []  # type: List[str]
    for u in untrusted:
        u = re.sub(r"\s+", " ", u or "").strip()
        for i in range(0, max(1, len(u) - 23), 8):
            frag = u[i:i + 24]
            if len(frag) == 24 and frag in flat:
                bad.append(frag)
                break
    return bad
