"""`migrate-plan`: a v1 `build-plan.json` -> `plan/plan.json` + `plan/items/<id>.json` (report section 10.3, CO-19).

Field mapping (CO-19), stated once:

    v1 field                       v2
    goal, createdAt                plan.json goal, created
    id, milestone, title, slug,    the item file, same names
      size, dependsOn, intent,
      requiresEnv
    acceptance ["AC-F1-1: text"]   acceptance [{"id": "AC-F1-1", "text": "text"}]; a line without an id gets AC-<item>-<n>
                                   (n = its position) and a warning, because ids are frozen at H1
    reworkNotes                    items/<id>.json#rework[]
    securityWaiver                 waivers[] with {approver, expiry, finding_ids, reason, commit}. v1 recorded no expiry: the
                                   migrated waiver expires today, so a human has to renew it (H6)
    downMigrationRef               migrations[].down (+ the data-loss class parsed from the text, else `lossy`: the safe reading)
    verifyEvidence                 not part of the definition; it goes to the state seed as the evidence the v1 run recorded
    status, blockReason, branch,   NOT in the item file. They form the STATE SEED, applied by `loopctl approve plan
      mergeCommit                  --seed-v1 build-plan.json`: done -> merged (the merge commit must exist), in_progress and
                                   verifying -> pending (v1 discards partial work), blocked -> blocked

Unknown fields fail loudly: the whole migration is refused and nothing is written. `--check` and `--dry-run` write nothing.
`--write` refuses to overwrite an existing `plan/` unless `--force`. Fields v1 never had (touches, estimate, verify,
migrations, riskClass) are added by a human or the Planner after the migration.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from kitlib import jsonio

from . import common, plan as plan_mod
from .common import Finding

__all__ = ["MigrationResult", "migrate_v1", "read_v1", "write_plan", "state_seed", "V1_ITEM_KEYS", "V1_TOP_KEYS"]

V1_TOP_KEYS = {"goal", "createdAt", "items"}
V1_ITEM_KEYS = {"id", "milestone", "title", "slug", "size", "dependsOn", "intent", "acceptance", "requiresEnv", "status",
                "blockReason", "branch", "mergeCommit", "verifyEvidence", "downMigrationRef", "reworkNotes", "securityWaiver"}
_AC_LINE = re.compile(r"^\s*(AC-([A-Za-z][A-Za-z0-9]{0,23})-(\d{1,3}))\s*[:.)\-]\s*(.+?)\s*$", re.S)
_CLASS_RE = re.compile(r"\b(lossy-recoverable|lossy|none)\b")


class MigrationResult(object):
    def __init__(self) -> None:
        self.plan_doc = {}  # type: Dict[str, Any]
        self.item_docs = {}  # type: Dict[str, Dict[str, Any]]
        self.seed = {}  # type: Dict[str, Dict[str, Any]]
        self.findings = []  # type: List[Finding]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def err(self, fid: str, msg: str, item: Optional[str] = None) -> None:
        self.findings.append(Finding("error", fid, msg, item))

    def warn(self, fid: str, msg: str, item: Optional[str] = None) -> None:
        self.findings.append(Finding("warn", fid, msg, item))

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": "loopctl.migrate.v1", "ok": self.ok, "plan": self.plan_doc.get("name"),
                "items": sorted(self.item_docs, key=common.natural_key),
                "state_seed": self.seed, "findings": [f.to_dict() for f in self.findings]}


def read_v1(text: str) -> Dict[str, Any]:
    d = common.strict_loads(text, max_depth=10, max_bytes=2 * 1024 * 1024)
    if not isinstance(d, dict):
        raise ValueError("build-plan.json must be a JSON object")
    return d


def migrate_v1(doc: Dict[str, Any], name: str, today: Optional[str] = None, default_size: str = "M") -> MigrationResult:
    res = MigrationResult()
    today = today or common.iso()[:10]
    if not common.valid_plan_name(name):
        res.err("migrate.name", "plan name %r must be a lower-case slug (letters, digits, '-')" % common.clean_text(name, 40))
        return res
    for k in sorted(set(doc) - V1_TOP_KEYS):
        res.err("migrate.unknown-field", "build-plan.json: unknown top-level field %r" % common.clean_text(k, 40))
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        res.err("migrate.items", "build-plan.json has no items list")
        return res
    goal = doc.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        res.err("migrate.goal", "build-plan.json has no goal")
        return res
    milestones = []  # type: List[str]
    for raw in items:
        if not isinstance(raw, dict):
            res.err("migrate.item", "an item is not an object")
            continue
        iid = raw.get("id")
        if not common.valid_item_id(iid):
            res.err("migrate.item-id", "item id %r is not valid in v2 (letters and digits, starts with a letter)" % common.clean_text(iid, 30))
            continue
        for k in sorted(set(raw) - V1_ITEM_KEYS):
            res.err("migrate.unknown-field", "item %s: unknown field %r" % (iid, common.clean_text(k, 40)), iid)
        if iid in res.item_docs:
            res.err("migrate.dup", "item id %s appears twice" % iid, iid)
            continue
        item = _item(raw, iid, res, default_size, today)
        res.item_docs[iid] = item
        if isinstance(item.get("milestone"), str) and item["milestone"] not in milestones:
            milestones.append(item["milestone"])
        res.seed[iid] = _seed_one(raw, iid, res)
    created = doc.get("createdAt")
    res.plan_doc = {"schema": plan_mod.PLAN_SCHEMA, "name": name, "goal": goal.strip(), "milestones": milestones}
    if isinstance(created, str) and re.match(r"^\d{4}-\d{2}-\d{2}\Z", created):
        res.plan_doc["created"] = created
    # the result must itself validate
    lp = plan_mod.LoadedPlan("migration")
    lp.plan, lp.items = res.plan_doc, res.item_docs
    rep = plan_mod.validate(lp)
    for f in rep.findings:
        if f.severity == "error":
            res.findings.append(Finding("error", "migrated." + f.id, f.message, f.item))
    return res


def _item(raw: Dict[str, Any], iid: str, res: MigrationResult, default_size: str, today: str) -> Dict[str, Any]:
    out = {"schema": plan_mod.ITEM_SCHEMA, "id": iid}  # type: Dict[str, Any]
    for k in ("milestone", "title", "slug", "intent"):
        out[k] = raw.get(k)
    size = raw.get("size")
    if size not in plan_mod.SIZES:
        if size is not None:
            res.err("migrate.size", "item %s: size %r is not S, M or L" % (iid, common.clean_text(size, 10)), iid)
        else:
            res.warn("migrate.size-defaulted", "item %s has no size (v1 plans before the slicing rules had none): set to %s; a human checks it" % (iid, default_size), iid)
        size = default_size
    out["size"] = size
    out["dependsOn"] = raw.get("dependsOn") if isinstance(raw.get("dependsOn"), list) else []
    out["requiresEnv"] = raw.get("requiresEnv") if isinstance(raw.get("requiresEnv"), list) else []
    acs = []  # type: List[Dict[str, str]]
    raw_acs = raw.get("acceptance")
    if not isinstance(raw_acs, list):
        res.err("migrate.acceptance", "item %s: acceptance is not a list" % iid, iid)
        raw_acs = []
    for n, line in enumerate(raw_acs, 1):
        if not isinstance(line, str):
            res.err("migrate.acceptance", "item %s: acceptance[%d] is not a string" % (iid, n - 1), iid)
            continue
        m = _AC_LINE.match(line)
        if m and m.group(2) == iid:
            acs.append({"id": m.group(1), "text": m.group(4)})
        elif m:
            res.err("migrate.ac-id", "item %s: %s belongs to another item" % (iid, m.group(1)), iid)
        else:
            res.warn("migrate.ac-id-generated", "item %s: acceptance line %d had no AC id: AC-%s-%d assigned" % (iid, n, iid, n), iid)
            acs.append({"id": common.ac_id_for(iid, n), "text": line.strip()})
    out["acceptance"] = acs
    notes = raw.get("reworkNotes")
    if notes:
        items = notes if isinstance(notes, list) else [notes]
        out["rework"] = [{"ts": today, "note": common.clean_text(n if isinstance(n, str) else str(n), 2000), "by": "v1"} for n in items][:50]
    mig = raw.get("downMigrationRef")
    if mig:
        text = mig if isinstance(mig, str) else str(mig)
        cm = _CLASS_RE.search(text)
        path = text.split()[0] if text.split() else ""
        if not cm:
            res.warn("migrate.data-loss-class", "item %s: downMigrationRef names no data-loss class: recorded as lossy (the safe reading)" % iid, iid)
        entry = {"name": re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))[:80] or "migration", "up": None,
                 "down": path if common.safe_rel_path(path) else None, "dataLossClass": cm.group(1) if cm else "lossy"}
        if entry["down"] is None:
            entry["noDownReason"] = "v1 recorded %s" % common.clean_text(text, 80)
            entry["dataLossClass"] = "lossy"
        out["migrations"] = [entry]
    w = raw.get("securityWaiver")
    if w:
        out["waivers"] = [_waiver(w, iid, res, today)]
    return out


def _waiver(w: Any, iid: str, res: MigrationResult, today: str) -> Dict[str, Any]:
    if not isinstance(w, dict):
        res.err("migrate.waiver", "item %s: securityWaiver is not an object" % iid, iid)
        return {}
    ids = []
    for f in (w.get("findings") or []):
        ids.append(re.sub(r"[^A-Za-z0-9._:-]", "_", str(f))[:64] or "finding")
    out = {"approver": common.clean_text(w.get("by") or "unknown", 90), "expiry": today,
           "finding_ids": ids or ["unspecified"], "reason": common.clean_text(w.get("reason") or "migrated from v1", 500)}
    if isinstance(w.get("commit"), str) and re.match(r"^[0-9a-f]{40}\Z", w["commit"]):
        out["commit"] = w["commit"]
    res.warn("migrate.waiver-expiry", "item %s: the v1 waiver had no expiry and was set to expire today; a human renews it (H6)" % iid, iid)
    return out


def _seed_one(raw: Dict[str, Any], iid: str, res: MigrationResult) -> Dict[str, Any]:
    status = raw.get("status") or "pending"
    seed = {"status": "pending", "blockReason": None, "branch": None, "mergeCommit": None, "v1Status": status}  # type: Dict[str, Any]
    if status in ("pending", "in_progress", "verifying"):
        pass
    elif status == "done":
        mc = raw.get("mergeCommit")
        if isinstance(mc, str) and re.match(r"^[0-9a-f]{40}\Z", mc):
            seed.update(status="merged", mergeCommit=mc)
        else:
            seed.update(status="needs_human", blockReason="merge_missing")
            res.warn("migrate.done-no-commit", "item %s is done in v1 with no mergeCommit: seeded needs_human; reconcile can recover it from git" % iid, iid)
    elif status == "blocked":
        seed.update(status="blocked", blockReason=common.clean_text(raw.get("blockReason") or "ESCALATION_NEEDED", 100))
    else:
        res.err("migrate.status", "item %s: status %r is not a v1 status" % (iid, common.clean_text(status, 20)), iid)
    ev = raw.get("verifyEvidence")
    if ev:
        seed["v1Evidence"] = common.clean_text(str(ev), 300)
    return seed


def state_seed(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The status seed of a v1 plan, for `approve plan --seed-v1`."""
    res = migrate_v1(doc, "seed")
    return res.seed


def write_plan(repo: str, res: MigrationResult, force: bool = False) -> List[str]:
    """Write plan/plan.json and plan/items/*.json. Refuses on errors and on an existing plan unless `force`."""
    if not res.ok:
        raise common.Refused("the migration has errors; nothing was written")
    pdir = os.path.join(repo, plan_mod.PLAN_DIR)
    if os.path.lexists(pdir) and not force:
        raise common.Refused("plan/ already exists; --force replaces plan.json and the item files named in the migration")
    idir = os.path.join(repo, plan_mod.ITEMS_DIR)
    for d in (pdir, idir):
        if os.path.islink(d):
            raise common.Refused("%s is a symlink; refused" % d)
    jsonio.ensure_dir(pdir, 0o755)
    jsonio.ensure_dir(idir, 0o755)
    written = []  # type: List[str]
    jsonio.atomic_write_json(os.path.join(repo, plan_mod.PLAN_FILE), res.plan_doc)
    written.append(plan_mod.PLAN_FILE)
    for iid in sorted(res.item_docs, key=common.natural_key):
        rel = "%s/%s.json" % (plan_mod.ITEMS_DIR, iid)
        jsonio.atomic_write_json(os.path.join(repo, rel), res.item_docs[iid])
        written.append(rel)
    return written
