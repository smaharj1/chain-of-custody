"""The plan: `plan/plan.json` and `plan/items/<id>.json` (report section 10.3, 19.7 slicing; IMP-20).

    plan/plan.json          {schema, name, goal, created, milestones?, ext?}
    plan/items/<id>.json    one file per item, so a machine-owned file never conflicts (CS-22)

The definition is tracked and immutable on main; it changes only through a plan PR (H2). Item STATUS is not in these
files: it lives on `refs/loopctl/state/<plan>` (state.py). Everything is read from a git revision (`load_at_rev`,
the base sha) or, for drafting, from the working tree (`load_worktree`). A plan file is untrusted input: parsing is
strict (no duplicate keys, no NaN, bounded size and depth, unknown fields are errors), ids and paths are validated
before they become a path, a ref or a command argument, and symlinks are refused.

Field spelling follows the report where it spells a field (`touches.deps_change`, `finding_ids`) and camelCase
elsewhere, as v1 did (`dependsOn`, `requiresEnv`, `riskClass`). See `.claude/spec/loopctl.md`.

`validate()` returns the findings plus the topological order, the critical path, the DAG width and the AC digests.
"""
from __future__ import annotations

import os
import re
import stat
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from kitlib import gitutil

from . import common
from .common import Finding

__all__ = ["PLAN_DIR", "PLAN_FILE", "ITEMS_DIR", "ITEM_SCHEMA", "PLAN_SCHEMA", "SIZES", "RISK_CLASSES",
           "DATA_LOSS_CLASSES", "LoadedPlan", "load_worktree", "load_at_rev", "validate", "ValidationReport",
           "topo_order", "find_cycle", "critical_path", "dag_width", "ancestors", "descendants",
           "ac_digest", "plan_digest", "item_human_gate", "verify_items_file", "ac_ids", "falsifiable",
           "waiver_active", "KNOWN_ITEM_KEYS"]

PLAN_DIR = "plan"
PLAN_FILE = "plan/plan.json"
ITEMS_DIR = "plan/items"
PLAN_SCHEMA = "loopctl.plan.v1"
ITEM_SCHEMA = "loopctl.item.v1"
SIZES = ("S", "M", "L")
RISK_CLASSES = ("low", "medium", "high")
DATA_LOSS_CLASSES = ("none", "lossy-recoverable", "lossy")
MAX_ITEMS = 300
MAX_FILE_BYTES = 256 * 1024
MAX_ENGINEERS_PREFERRED, MAX_ENGINEERS_HARD = 2, 3
MAX_ACCEPTANCE_PREFERRED = 3

KNOWN_PLAN_KEYS = {"schema", "name", "goal", "created", "milestones", "ext"}
KNOWN_ITEM_KEYS = {"schema", "id", "milestone", "title", "slug", "size", "dependsOn", "intent", "acceptance",
                   "requiresEnv", "touches", "estimate", "riskClass", "migrations", "verify", "rework", "waivers", "ext"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}\Z")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
_MIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}\Z")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~%/:@?&=+,#-]{0,200}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")


# -- loading ----------------------------------------------------------------------------------------------

class LoadedPlan(object):
    """The parsed plan. `findings` holds what could not be parsed; the dicts hold what could."""

    def __init__(self, source: str) -> None:
        self.source = source                      # "worktree" or a revision
        self.plan = None  # type: Optional[Dict[str, Any]]
        self.items = {}  # type: Dict[str, Dict[str, Any]]
        self.findings = []  # type: List[Finding]
        self.agents = []  # type: List[str]       # .claude/agents/*.md names at the same source
        self.extra = {}  # type: Dict[str, Optional[str]]   # load_at_rev(extra=...): other files read in the same calls

    @property
    def name(self) -> Optional[str]:
        return self.plan.get("name") if self.plan else None

    @property
    def fatal(self) -> bool:
        return self.plan is None

    def err(self, fid: str, msg: str, item: Optional[str] = None) -> None:
        self.findings.append(Finding("error", fid, msg, item))

    def warn(self, fid: str, msg: str, item: Optional[str] = None) -> None:
        self.findings.append(Finding("warn", fid, msg, item))

    def ids(self) -> List[str]:
        return sorted(self.items, key=common.natural_key)


def _parse(text: str, what: str, lp: LoadedPlan, item: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        obj = common.strict_loads(text, max_depth=10, max_bytes=MAX_FILE_BYTES)
    except (ValueError, RecursionError) as exc:
        lp.err("plan.json", "%s is not usable JSON: %s" % (what, common.clean_text(exc, 160)), item)
        return None
    if not isinstance(obj, dict):
        lp.err("plan.json", "%s must be a JSON object" % what, item)
        return None
    return obj


def load_worktree(repo: str) -> LoadedPlan:
    """Read plan/ from the working tree (drafting). Symlinks and special files are refused."""
    lp = LoadedPlan("worktree")
    pdir = os.path.join(repo, PLAN_DIR)
    pfile = os.path.join(repo, PLAN_FILE)
    idir = os.path.join(repo, ITEMS_DIR)

    def read_regular(path: str, label: str) -> Optional[str]:
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            lp.err("plan.not-regular", "%s is not a regular file (symlinks are refused)" % label)
            return None
        if st.st_size > MAX_FILE_BYTES:
            lp.err("plan.too-big", "%s is larger than %d bytes" % (label, MAX_FILE_BYTES))
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    if os.path.islink(pdir) or os.path.islink(idir):
        lp.err("plan.not-regular", "plan/ or plan/items/ is a symlink; refused")
        return lp
    text = read_regular(pfile, PLAN_FILE)
    if text is None:
        if not any(f.id in ("plan.not-regular", "plan.too-big") for f in lp.findings):
            lp.err("plan.missing", "%s does not exist" % PLAN_FILE)
        return lp
    lp.plan = _parse(text, PLAN_FILE, lp)
    names = []  # type: List[str]
    if os.path.isdir(idir):
        names = sorted(os.listdir(idir))
    for fn in names:
        full = os.path.join(idir, fn)
        _load_item_file(lp, fn, lambda f=full, n=fn: read_regular(f, "plan/items/" + n))
    _worktree_agents(repo, lp)
    return lp


def _worktree_agents(repo: str, lp: LoadedPlan) -> None:
    d = os.path.join(repo, ".claude", "agents")
    try:
        lp.agents = sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md"))
    except OSError:
        lp.agents = []


def load_at_rev(repo: str, rev: str, extra: Sequence[str] = ()) -> LoadedPlan:
    """Read plan/ as committed at `rev` (the base sha). No working-tree file is consulted. `extra` names more files to read in
    the same git calls (the loop.config); their text lands in `lp.extra` (None when absent)."""
    from . import gitio
    lp = LoadedPlan(rev)
    try:
        rows = gitio.ls_tree(repo, rev, [PLAN_DIR, ".claude/agents"] + list(extra))
    except ValueError:
        lp.err("plan.missing", "cannot list %s at %s" % (PLAN_DIR, rev[:12]))
        return lp
    entries = {str(r["path"]): r for r in rows}
    lp.agents = sorted(os.path.basename(p)[:-3] for p in entries
                       if p.startswith(".claude/agents/") and p.endswith(".md") and p.count("/") == 2)
    wanted = {}  # type: Dict[str, str]
    for path in extra:
        r = entries.get(path)
        if r is not None and r["mode"] in ("100644", "100755") and r["type"] == "blob" and 0 <= int(r["size"]) <= MAX_FILE_BYTES * 8:
            wanted[path] = str(r["sha"])
    for path, r in sorted(entries.items()):
        if not path.startswith(PLAN_DIR + "/"):
            continue
        if r["mode"] == "120000":
            lp.err("plan.not-regular", "%s is a symlink; refused" % common.clean_text(path, 100))
        elif r["mode"] not in ("100644", "100755") or r["type"] != "blob":
            lp.err("plan.not-regular", "%s is not a regular file" % common.clean_text(path, 100))
        elif int(r["size"]) > MAX_FILE_BYTES or int(r["size"]) < 0:
            lp.err("plan.too-big", "%s is larger than %d bytes" % (common.clean_text(path, 100), MAX_FILE_BYTES))
        else:
            wanted[path] = str(r["sha"])
    blobs = gitio.cat_blobs(repo, list(wanted.values()), MAX_FILE_BYTES * 8)
    for path in extra:
        b = blobs.get(wanted.get(path, ""))
        lp.extra[path] = b.decode("utf-8", "replace") if b is not None else None

    def text_of(path: str) -> Optional[str]:
        b = blobs.get(wanted.get(path, ""))
        return b.decode("utf-8", "replace") if b is not None else None

    if PLAN_FILE not in entries:
        lp.err("plan.missing", "%s does not exist at %s" % (PLAN_FILE, rev[:12]))
        return lp
    if PLAN_FILE in wanted:
        t = text_of(PLAN_FILE)
        if t is not None:
            lp.plan = _parse(t, PLAN_FILE, lp)
    for path in sorted(entries):
        if not path.startswith(PLAN_DIR + "/") or path == PLAN_FILE:
            continue
        if not path.startswith(ITEMS_DIR + "/") or path.count("/") != 2:
            lp.err("plan.stray-file", "%s is not part of the plan layout" % common.clean_text(path, 100))
            continue
        if path in wanted:
            _load_item_file(lp, path.split("/")[-1], lambda p=path: text_of(p))
    return lp


def _load_item_file(lp: LoadedPlan, filename: str, reader) -> None:
    if not filename.endswith(".json"):
        lp.err("plan.stray-file", "plan/items/%s is not an item file (*.json)" % common.clean_text(filename, 60))
        return
    stem = filename[:-5]
    if not common.valid_item_id(stem):
        lp.err("plan.item-id", "plan/items/%s: the file name must be a valid item id (letters and digits, starts with a letter)"
               % common.clean_text(filename, 60))
        return
    text = reader()
    if text is None:
        return
    obj = _parse(text, "plan/items/%s" % filename, lp, stem)
    if obj is None:
        return
    if obj.get("id") != stem:
        lp.err("plan.item-id", "plan/items/%s: id %r does not match the file name" % (filename, common.clean_text(obj.get("id"), 40)), stem)
        return
    if len(lp.items) >= MAX_ITEMS:
        lp.err("plan.too-many-items", "more than %d items" % MAX_ITEMS)
        return
    lp.items[stem] = obj


# -- structure checks -------------------------------------------------------------------------------------------

def _type_check(obj: Dict[str, Any], key: str, types: tuple, where: str, lp: LoadedPlan, item: Optional[str],
                required: bool = True) -> bool:
    if key not in obj:
        if required:
            lp.err("schema.missing", "%s: '%s' is required" % (where, key), item)
        return False
    v = obj[key]
    ok = isinstance(v, types) and not (isinstance(v, bool) and bool not in types)
    if not ok:
        lp.err("schema.type", "%s: '%s' has the wrong type (%s)" % (where, key, type(v).__name__), item)
    return ok


def _str_list(obj: Dict[str, Any], key: str, where: str, lp: LoadedPlan, item: str, rx=None, limit: int = 60,
              maxlen: int = 200) -> List[str]:
    if key not in obj:
        return []
    v = obj[key]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        lp.err("schema.type", "%s: '%s' must be a list of strings" % (where, key), item)
        return []
    if len(v) > limit:
        lp.err("schema.size", "%s: '%s' has more than %d entries" % (where, key, limit), item)
        return []
    out = []
    for x in v:
        if len(x) > maxlen or (rx is not None and not rx.match(x)):
            lp.err("schema.value", "%s: '%s' holds an invalid entry %r" % (where, key, common.clean_text(x, 40)), item)
        else:
            out.append(x)
    return out


def _check_ext(obj: Dict[str, Any], where: str, lp: LoadedPlan, item: Optional[str]) -> None:
    if "ext" in obj:
        if not isinstance(obj["ext"], dict):
            lp.err("schema.type", "%s: 'ext' must be an object" % where, item)
        elif len(common.canonical_json(obj["ext"])) > 8192:
            lp.err("schema.size", "%s: 'ext' is larger than 8 KiB" % where, item)


def _check_plan(lp: LoadedPlan) -> None:
    p = lp.plan
    if p is None:
        return
    for k in sorted(set(p) - KNOWN_PLAN_KEYS):
        lp.err("schema.unknown-field", "plan.json: unknown field %r" % common.clean_text(k, 40))
    if p.get("schema") != PLAN_SCHEMA:
        lp.err("schema.version", "plan.json: schema must be %r" % PLAN_SCHEMA)
    if not common.valid_plan_name(p.get("name")):
        lp.err("plan.name", "plan.json: name must be a lower-case slug (letters, digits, '-'), at most 41 characters")
    if _type_check(p, "goal", (str,), "plan.json", lp, None) and not (1 <= len(p["goal"].strip()) <= 4000):
        lp.err("schema.value", "plan.json: goal must be 1..4000 characters")
    if "created" in p and (not isinstance(p["created"], str) or not _DATE_RE.match(p["created"])):
        lp.err("schema.value", "plan.json: created must be YYYY-MM-DD")
    if "milestones" in p:
        ms = p["milestones"]
        if not isinstance(ms, list) or not all(isinstance(m, str) and 0 < len(m) <= 120 for m in ms) or len(set(ms)) != len(ms):
            lp.err("schema.value", "plan.json: milestones must be a list of distinct non-empty strings")
    _check_ext(p, "plan.json", lp, None)


def _check_item(lp: LoadedPlan, iid: str, it: Dict[str, Any], milestones: Sequence[str]) -> None:
    w = "item %s" % iid
    for k in sorted(set(it) - KNOWN_ITEM_KEYS):
        lp.err("schema.unknown-field", "%s: unknown field %r (unknown fields fail loudly; see migrate-plan)" % (w, common.clean_text(k, 40)), iid)
    if it.get("schema") != ITEM_SCHEMA:
        lp.err("schema.version", "%s: schema must be %r" % (w, ITEM_SCHEMA), iid)
    for key, mx in (("milestone", 120), ("title", 160), ("intent", 4000)):
        if _type_check(it, key, (str,), w, lp, iid) and not (1 <= len(it[key].strip()) <= mx):
            lp.err("schema.value", "%s: '%s' must be 1..%d characters" % (w, key, mx), iid)
    if milestones and isinstance(it.get("milestone"), str) and it["milestone"] not in milestones:
        lp.err("plan.milestone", "%s: milestone %r is not in plan.json milestones" % (w, common.clean_text(it["milestone"], 60)), iid)
    if _type_check(it, "slug", (str,), w, lp, iid) and not common.valid_slug(it["slug"]):
        lp.err("schema.value", "%s: slug must be lower-case letters, digits and '-' (it becomes part of a branch name)" % w, iid)
    if _type_check(it, "size", (str,), w, lp, iid) and it["size"] not in SIZES:
        lp.err("schema.value", "%s: size must be one of %s" % (w, ", ".join(SIZES)), iid)
    deps = _str_list(it, "dependsOn", w, lp, iid, rx=common.ID_RE, limit=40, maxlen=24) if "dependsOn" in it else None
    if "dependsOn" not in it:
        lp.err("schema.missing", "%s: 'dependsOn' is required (use [] for none)" % w, iid)
    elif deps is not None and len(set(deps)) != len(deps):
        lp.warn("plan.dup-dependency", "%s: dependsOn lists an id twice" % w, iid)
    _str_list(it, "requiresEnv", w, lp, iid, rx=_ENV_RE, limit=30, maxlen=64)
    if "riskClass" in it and it["riskClass"] not in RISK_CLASSES:
        lp.err("schema.value", "%s: riskClass must be one of %s" % (w, ", ".join(RISK_CLASSES)), iid)

    # acceptance criteria with frozen ids (G1)
    if _type_check(it, "acceptance", (list,), w, lp, iid):
        acs = it["acceptance"]
        if not acs:
            lp.err("ac.none", "%s: at least one acceptance criterion is required" % w, iid)
        if len(acs) > 20:
            lp.err("schema.size", "%s: more than 20 acceptance criteria" % w, iid)
        seen = set()  # type: Set[str]
        for i, ac in enumerate(acs[:20]):
            if not isinstance(ac, dict) or set(ac) - {"id", "text"} or not isinstance(ac.get("id"), str) or not isinstance(ac.get("text"), str):
                lp.err("ac.shape", "%s: acceptance[%d] must be {\"id\": \"AC-%s-<n>\", \"text\": \"...\"}" % (w, i, iid), iid)
                continue
            m = common.AC_RE.match(ac["id"])
            if not m or m.group(1) != iid:
                lp.err("ac.id", "%s: acceptance id %r must look like AC-%s-<n>" % (w, common.clean_text(ac["id"], 30), iid), iid)
            if ac["id"] in seen:
                lp.err("ac.dup", "%s: acceptance id %s is used twice" % (w, common.clean_text(ac["id"], 30)), iid)
            seen.add(ac["id"])
            if not (3 <= len(ac["text"].strip()) <= 1200):
                lp.err("ac.text", "%s: %s text must be 3..1200 characters" % (w, common.clean_text(ac["id"], 30)), iid)
        if acs and not any(isinstance(a, dict) and isinstance(a.get("text"), str) and falsifiable(a["text"]) for a in acs):
            lp.err("ac.not-falsifiable", "%s: no criterion is falsifiable. Name a test that must exist and pass, or an exact route and "
                   "the response it must give; 'the app boots' does not count (L-22)" % w, iid)

    # touches (footprint) and estimate
    if "touches" in it:
        t = it["touches"]
        if not isinstance(t, dict) or set(t) - {"domains", "paths", "schema", "deps_change"}:
            lp.err("schema.type", "%s: touches must be {domains, paths, schema, deps_change}" % w, iid)
        else:
            _str_list(t, "domains", w + " touches", lp, iid, rx=_DOMAIN_RE, limit=10, maxlen=32)
            for pth in _str_list(t, "paths", w + " touches", lp, iid, limit=40, maxlen=200):
                if not common.safe_glob(pth):
                    lp.err("path.unsafe", "%s: touches.paths entry %r is not a safe relative glob (no '..', no absolute path, no .git)"
                           % (w, common.clean_text(pth, 60)), iid)
            for b in ("schema", "deps_change"):
                if b in t and not isinstance(t[b], bool):
                    lp.err("schema.type", "%s: touches.%s must be true or false" % (w, b), iid)
    if "estimate" in it:
        e = it["estimate"]
        if not isinstance(e, dict) or set(e) - {"loc", "engineers"}:
            lp.err("schema.type", "%s: estimate must be {loc, engineers}" % w, iid)
        else:
            if "loc" in e and (not isinstance(e["loc"], int) or isinstance(e["loc"], bool) or not 0 <= e["loc"] <= 100000):
                lp.err("schema.value", "%s: estimate.loc must be a whole number" % w, iid)
            _str_list(e, "engineers", w + " estimate", lp, iid, rx=_ROLE_RE, limit=8, maxlen=41)

    _check_migrations(lp, iid, it, w)
    if "verify" in it:
        v = it["verify"]
        if not isinstance(v, dict) or set(v) - {"tests", "demoRoute"}:
            lp.err("schema.type", "%s: verify must be {tests, demoRoute}" % w, iid)
        else:
            t = v.get("tests")
            if t is not None and (not isinstance(t, str) or not t.strip() or len(t) > 500 or re.search(r"[\x00-\x08\x0a-\x1f\x7f]", t)):
                lp.err("schema.value", "%s: verify.tests must be one line of at most 500 characters or null" % w, iid)
            r = v.get("demoRoute")
            if r is not None and (not isinstance(r, str) or not _ROUTE_RE.match(r)):
                lp.err("schema.value", "%s: verify.demoRoute must be a URL path starting with '/' or null" % w, iid)
    _check_rework_waivers(lp, iid, it, w)
    _check_ext(it, w, lp, iid)


def _check_migrations(lp: LoadedPlan, iid: str, it: Dict[str, Any], w: str) -> None:
    if "migrations" not in it:
        return
    ms = it["migrations"]
    if not isinstance(ms, list) or len(ms) > 8:
        lp.err("schema.type", "%s: migrations must be a list of at most 8 entries" % w, iid)
        return
    for i, m in enumerate(ms):
        mw = "%s migrations[%d]" % (w, i)
        allowed = {"name", "up", "down", "dataLossClass", "stage", "noDownReason"}
        if not isinstance(m, dict) or set(m) - allowed:
            lp.err("schema.type", "%s must be an object with %s" % (mw, ", ".join(sorted(allowed))), iid)
            continue
        if not isinstance(m.get("name"), str) or not _MIG_NAME_RE.match(m["name"]):
            lp.err("schema.value", "%s: name must be letters, digits, '.', '_', '-'" % mw, iid)
        for k in ("up", "down"):
            v = m.get(k)
            if v is not None and not common.safe_rel_path(v):
                lp.err("path.unsafe", "%s: %s %r is not a safe relative path" % (mw, k, common.clean_text(v, 60)), iid)
        cls = m.get("dataLossClass")
        if cls not in DATA_LOSS_CLASSES:
            lp.err("migration.data-loss-class", "%s: dataLossClass is required and must be one of %s (CO-18, .claude/context/migrations.md)"
                   % (mw, ", ".join(DATA_LOSS_CLASSES)), iid)
        if "stage" in m and m["stage"] not in ("expand", "backfill", "contract"):
            lp.err("schema.value", "%s: stage must be expand, backfill or contract" % mw, iid)
        if m.get("down") is None:
            reason = m.get("noDownReason")
            if cls != "lossy" or not isinstance(reason, str) or not reason.strip():
                lp.err("migration.no-down", "%s: a migration needs a down-migration, except a class 'lossy' one whose noDownReason says why "
                       "none can exist (CO-18)" % mw, iid)
        if cls == "lossy":
            lp.warn("migration.lossy", "%s is lossy: it is human-gated and its down is never run automatically" % mw, iid)


def waiver_active(w: Dict[str, Any], today: str) -> bool:
    exp = w.get("expiry")
    return isinstance(exp, str) and bool(_DATE_RE.match(exp)) and exp >= today


def _check_rework_waivers(lp: LoadedPlan, iid: str, it: Dict[str, Any], w: str) -> None:
    if "rework" in it:
        rw = it["rework"]
        if not isinstance(rw, list) or len(rw) > 50:
            lp.err("schema.type", "%s: rework must be a list of at most 50 entries" % w, iid)
        else:
            for i, r in enumerate(rw):
                if not isinstance(r, dict) or set(r) - {"ts", "note", "by"} or not isinstance(r.get("note"), str) or len(r["note"]) > 2000:
                    lp.err("schema.type", "%s: rework[%d] must be {ts, note, by}" % (w, i), iid)
    if "waivers" in it:
        ws = it["waivers"]
        if not isinstance(ws, list) or len(ws) > 20:
            lp.err("schema.type", "%s: waivers must be a list of at most 20 entries" % w, iid)
            return
        for i, v in enumerate(ws):
            ww = "%s waivers[%d]" % (w, i)
            if not isinstance(v, dict) or set(v) - {"approver", "expiry", "finding_ids", "reason", "commit"}:
                lp.err("schema.type", "%s must be {approver, expiry, finding_ids, reason, commit?}" % ww, iid)
                continue
            if not isinstance(v.get("approver"), str) or not v["approver"].strip() or len(v["approver"]) > 100:
                lp.err("waiver.approver", "%s: approver (a named human) is required (H6)" % ww, iid)
            if not isinstance(v.get("expiry"), str) or not _DATE_RE.match(v["expiry"]):
                lp.err("waiver.expiry", "%s: expiry (YYYY-MM-DD) is required (H6)" % ww, iid)
            fids = v.get("finding_ids")
            if not isinstance(fids, list) or not fids or not all(isinstance(x, str) and _FINDING_ID_RE.match(x) for x in fids) or len(fids) > 50:
                lp.err("waiver.findings", "%s: finding_ids must list the specific findings waived" % ww, iid)
            if not isinstance(v.get("reason"), str) or not v["reason"].strip():
                lp.err("waiver.reason", "%s: a written reason is required" % ww, iid)
            if "commit" in v and not (isinstance(v["commit"], str) and _SHA_RE.match(v["commit"])):
                lp.err("schema.value", "%s: commit must be a full 40-character sha" % ww, iid)


# -- ACs ------------------------------------------------------------------------------------------------------------

_FALSIFIABLE_RES = [
    re.compile(r"`[^`\n]{2,}`"),                                               # a command, route, file or response in a code span
    re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+/\S*"),                # an HTTP request
    re.compile(r"\b[1-5]\d\d\b"),                                              # a status code
    re.compile(r"\bexit(?:s|ed)?(?:\s+code)?\s+\d+\b", re.I),                  # an exit code
    re.compile(r"\btests?\b.{0,60}\b(?:exist|pass|fail|green|red)\w*", re.I | re.S),   # a named test that must pass
]


def falsifiable(text: str) -> bool:
    return any(rx.search(text) for rx in _FALSIFIABLE_RES)


def ac_ids(item: Dict[str, Any]) -> List[str]:
    return [a["id"] for a in item.get("acceptance", []) if isinstance(a, dict) and isinstance(a.get("id"), str)]


def ac_digest(item: Dict[str, Any]) -> str:
    """The frozen-AC fingerprint (H1): changes iff an id or a text changes, or one is added or removed."""
    rows = sorted(((a.get("id"), a.get("text")) for a in item.get("acceptance", []) if isinstance(a, dict)), key=lambda r: str(r[0]))
    return common.sha256_hex(common.canonical_json(rows))


def plan_digest(lp: LoadedPlan) -> str:
    return common.sha256_hex(common.canonical_json({"plan": lp.plan, "items": {k: lp.items[k] for k in sorted(lp.items)}}))


# -- graph -----------------------------------------------------------------------------------------------------------

def _deps(items: Dict[str, Dict[str, Any]], iid: str) -> List[str]:
    d = items[iid].get("dependsOn")
    return [x for x in d if isinstance(x, str) and x in items] if isinstance(d, list) else []


def find_cycle(items: Dict[str, Dict[str, Any]]) -> Optional[List[str]]:
    """A dependency cycle as [a, b, ..., a], or None. Iterative, deterministic."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {k: WHITE for k in items}
    for root in sorted(items, key=common.natural_key):
        if colour[root] != WHITE:
            continue
        stack = [(root, iter(sorted(_deps(items, root), key=common.natural_key)))]
        path = [root]
        colour[root] = GREY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if colour[nxt] == GREY:
                return path[path.index(nxt):] + [nxt]
            if colour[nxt] == WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, iter(sorted(_deps(items, nxt), key=common.natural_key))))
    return None


def topo_order(items: Dict[str, Dict[str, Any]], milestones: Sequence[str] = ()) -> List[str]:
    """Kahn's algorithm with a deterministic tie-break (L-13): among ready items, lower milestone index first, then
    the natural order of the id (F2 before F10). Items on a cycle are omitted."""
    mindex = {m: i for i, m in enumerate(milestones)}

    def key(i: str) -> Tuple[Any, ...]:
        return (mindex.get(items[i].get("milestone"), len(mindex)), common.natural_key(i))

    indeg = {i: len(set(_deps(items, i))) for i in items}
    users = {i: [] for i in items}  # type: Dict[str, List[str]]
    for i in items:
        for d in set(_deps(items, i)):
            users[d].append(i)
    ready = sorted((i for i, n in indeg.items() if n == 0), key=key)
    out = []  # type: List[str]
    while ready:
        i = ready.pop(0)
        out.append(i)
        for u in users[i]:
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
        ready.sort(key=key)
    return out


def ancestors(items: Dict[str, Dict[str, Any]], iid: str) -> Set[str]:
    seen = set()  # type: Set[str]
    todo = list(_deps(items, iid))
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(_deps(items, n))
    return seen


def descendants(items: Dict[str, Dict[str, Any]], iid: str) -> Set[str]:
    users = {i: [] for i in items}  # type: Dict[str, List[str]]
    for i in items:
        for d in _deps(items, i):
            users[d].append(i)
    seen = set()  # type: Set[str]
    todo = list(users.get(iid, []))
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(users.get(n, []))
    return seen


def critical_path(items: Dict[str, Dict[str, Any]], order: Sequence[str]) -> List[str]:
    """The longest dependency chain (by item count). Ties break towards the earlier id."""
    best = {}  # type: Dict[str, Tuple[int, Optional[str]]]
    for i in order:
        cand = [(best[d][0], d) for d in _deps(items, i) if d in best]
        if cand:
            top = max(c[0] for c in cand)
            parent = sorted((d for n, d in cand if n == top), key=common.natural_key)[0]
            best[i] = (top + 1, parent)
        else:
            best[i] = (1, None)
    if not best:
        return []
    end = sorted((i for i in best if best[i][0] == max(b[0] for b in best.values())), key=common.natural_key)[0]
    chain = []  # type: List[str]
    cur = end  # type: Optional[str]
    while cur is not None:
        chain.append(cur)
        cur = best[cur][1]
    return list(reversed(chain))


def dag_width(items: Dict[str, Dict[str, Any]], order: Sequence[str]) -> Tuple[int, List[List[str]], List[str]]:
    """(maximum antichain size, the levels, one maximum antichain). The maximum number of items that could run at once is the
    size of the largest set of items with no dependency path between any two (Dilworth: n minus a maximum matching of the
    transitive closure; the antichain itself comes from the minimum vertex cover, Koenig)."""
    ids = list(order)
    reach = {i: ancestors(items, i) for i in ids}   # i depends (transitively) on reach[i]
    adj = {i: sorted(reach[i], key=common.natural_key) for i in ids}
    match_right = {}  # type: Dict[str, str]     # right copy v -> left copy u
    match_left = {}  # type: Dict[str, str]

    def augment(u: str, seen: Set[str]) -> bool:
        for v in adj[u]:
            if v in seen:
                continue
            seen.add(v)
            if v not in match_right or augment(match_right[v], seen):
                match_right[v] = u
                match_left[u] = v
                return True
        return False

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 4 * len(ids) + 200))
    try:
        for u in ids:
            augment(u, set())
    finally:
        sys.setrecursionlimit(old)
    width = len(ids) - len(match_right)
    # alternating reachability from the unmatched left vertices
    zl = {u for u in ids if u not in match_left}
    zr = set()  # type: Set[str]
    todo = list(zl)
    while todo:
        u = todo.pop()
        for v in adj[u]:
            if v in zr or match_left.get(u) == v:
                continue
            zr.add(v)
            w = match_right.get(v)
            if w is not None and w not in zl:
                zl.add(w)
                todo.append(w)
    witness = sorted((x for x in ids if x in zl and x not in zr), key=common.natural_key)
    level = {}  # type: Dict[str, int]
    for i in ids:
        level[i] = 1 + max([level[d] for d in _deps(items, i)] or [0])
    levels = []  # type: List[List[str]]
    for i in ids:
        while len(levels) < level[i]:
            levels.append([])
        levels[level[i] - 1].append(i)
    return width, levels, witness


# -- validation -----------------------------------------------------------------------------------------------------

class ValidationReport(object):
    def __init__(self) -> None:
        self.findings = []  # type: List[Finding]
        self.order = []  # type: List[str]
        self.critical = []  # type: List[str]
        self.width = 0
        self.levels = []  # type: List[List[str]]
        self.witness = []  # type: List[str]
        self.digest = ""
        self.ac_digests = {}  # type: Dict[str, str]
        self.name = None  # type: Optional[str]
        self.source = ""
        self.limits = {}  # type: Dict[str, Any]

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": "loopctl.validate.v1", "ok": self.ok, "plan": self.name, "source": self.source,
                "items": len(self.order), "order": self.order,
                "critical_path": {"length": len(self.critical), "items": self.critical},
                "width": {"max": self.width, "levels": self.levels, "example": self.witness},
                "digest": self.digest, "ac_digests": self.ac_digests, "limits": self.limits,
                "findings": [f.to_dict() for f in self.findings]}


def validate(lp: LoadedPlan, max_item_loc: int = 400, roster: Optional[Iterable[str]] = None,
             always_human_paths: Sequence[str] = ()) -> ValidationReport:
    """Everything `loopctl validate` checks: schema, AC ids, the DAG, slicing, data-loss classes."""
    rep = ValidationReport()
    rep.source = lp.source
    rep.limits = {"max_item_loc": max_item_loc}
    rep.findings = list(lp.findings)
    if lp.plan is None:
        return rep
    rep.name = lp.plan.get("name") if isinstance(lp.plan.get("name"), str) else None
    sub = LoadedPlan(lp.source)
    sub.plan, sub.items, sub.agents = lp.plan, lp.items, lp.agents
    _check_plan(sub)
    ms = lp.plan.get("milestones") if isinstance(lp.plan.get("milestones"), list) else []
    ms = [m for m in ms if isinstance(m, str)]
    if not lp.items:
        sub.err("plan.empty", "the plan has no items")
    lower = {}  # type: Dict[str, str]
    for iid in lp.ids():
        if iid.lower() in lower:
            sub.err("plan.item-id", "item ids %s and %s differ only by case; refused (case-folding file systems)" % (lower[iid.lower()], iid), iid)
        lower[iid.lower()] = iid
        _check_item(sub, iid, lp.items[iid], ms)
    known_roles = set(roster or ()) | set(lp.agents)
    for iid in lp.ids():
        it = lp.items[iid]
        deps = it.get("dependsOn") if isinstance(it.get("dependsOn"), list) else []
        for d in deps:
            if not isinstance(d, str):
                continue
            if d == iid:
                sub.err("dag.self", "item %s depends on itself" % iid, iid)
            elif d not in lp.items:
                sub.err("dag.missing", "item %s depends on %s, which does not exist" % (iid, common.clean_text(d, 30)), iid)
        est = it.get("estimate") if isinstance(it.get("estimate"), dict) else {}
        _slicing(sub, iid, it, est, max_item_loc, known_roles)
        gate = item_human_gate(it, always_human_paths)
        if gate:
            sub.findings.append(Finding("info", "gate.human", "item %s needs a human at merge: %s" % (iid, "; ".join(gate)), iid))
    cyc = find_cycle(lp.items)
    if cyc:
        sub.err("dag.cycle", "dependency cycle: %s" % " -> ".join(cyc))
    rep.findings = list(lp.findings) + sub.findings
    # structural failure above (a cycle) makes the metrics meaningless but still computable for acyclic parts
    rep.order = topo_order(lp.items, ms)
    if not cyc and not any(f.id in ("dag.missing", "dag.self") for f in rep.findings):
        rep.critical = critical_path(lp.items, rep.order)
        rep.width, rep.levels, rep.witness = dag_width(lp.items, rep.order)
    rep.digest = plan_digest(lp)
    rep.ac_digests = {i: ac_digest(lp.items[i]) for i in lp.ids()}
    return rep


def _slicing(lp: LoadedPlan, iid: str, it: Dict[str, Any], est: Dict[str, Any], max_loc: int, roles: Set[str]) -> None:
    if it.get("size") == "L":
        lp.err("slice.size-l", "item %s is sized L: split it into slices a reviewer can read in one sitting (L-35)" % iid, iid)
    loc = est.get("loc")
    if isinstance(loc, int) and not isinstance(loc, bool) and loc > max_loc:
        lp.err("slice.loc", "item %s estimates %d changed lines; the limit is max_item_loc %d" % (iid, loc, max_loc), iid)
    migs = it.get("migrations") if isinstance(it.get("migrations"), list) else []
    if len(migs) > 1:
        lp.err("slice.migrations", "item %s has %d migrations; one per slice (expand, backfill and contract are separate items)" % (iid, len(migs)), iid)
    eng = est.get("engineers") if isinstance(est.get("engineers"), list) else []
    if len(eng) > MAX_ENGINEERS_HARD:
        lp.err("slice.engineers", "item %s needs %d engineers; three at most" % (iid, len(eng)), iid)
    elif len(eng) > MAX_ENGINEERS_PREFERRED:
        lp.warn("slice.engineers", "item %s needs %d engineers; two are preferred" % (iid, len(eng)), iid)
    for e in eng:
        if isinstance(e, str) and roles and e not in roles:
            lp.warn("plan.unknown-engineer", "item %s names engineer %r, who is not in .claude/agents or the roster" % (iid, common.clean_text(e, 40)), iid)
    n_ac = len(it.get("acceptance") or []) if isinstance(it.get("acceptance"), list) else 0
    if n_ac > MAX_ACCEPTANCE_PREFERRED:
        lp.warn("slice.acceptance", "item %s has %d acceptance criteria; three or fewer is the slicing rule" % (iid, n_ac), iid)
    touches = it.get("touches") if isinstance(it.get("touches"), dict) else {}
    if migs and touches and touches.get("schema") is False:
        lp.warn("plan.schema-flag", "item %s has a migration but touches.schema is false" % iid, iid)


def item_human_gate(item: Dict[str, Any], always_human_paths: Sequence[str] = ()) -> List[str]:
    """Why an item needs a human at merge (H3, H4): the reasons, empty if none. A lossy down-migration, a high risk class,
    a footprint inside an always-human path, a granted waiver."""
    reasons = []  # type: List[str]
    for m in item.get("migrations") or []:
        if isinstance(m, dict) and m.get("dataLossClass") == "lossy":
            reasons.append("lossy down-migration %s" % common.clean_text(m.get("name"), 40))
    if item.get("riskClass") == "high":
        reasons.append("risk class high")
    if item.get("waivers"):
        reasons.append("security waiver on the item")
    paths = []
    touches = item.get("touches")
    if isinstance(touches, dict) and isinstance(touches.get("paths"), list):
        paths = [p for p in touches["paths"] if isinstance(p, str)]
    if paths:
        try:
            import pr_prepare
            hits = pr_prepare.classify_paths(paths, list(always_human_paths))
        except Exception:  # noqa: BLE001 - classification is advisory here; the merge-time check re-runs it on the real diff
            hits = []
        for h in hits[:3]:
            reasons.append("footprint %s: %s" % (common.clean_text(h["path"], 50), h["reason"]))
    if isinstance(touches, dict) and touches.get("deps_change") is True:
        reasons.append("adds or changes dependencies")
    return reasons


def verify_items_file(lp: LoadedPlan) -> Dict[str, Any]:
    """The `--items-file` document `verify.py` reads (verify.v1 inputs): {"items": {ID: {tests, demo_route, ac_ids}}}."""
    out = {}  # type: Dict[str, Any]
    for iid in lp.ids():
        it = lp.items[iid]
        v = it.get("verify") if isinstance(it.get("verify"), dict) else {}
        out[iid] = {"tests": v.get("tests"), "demo_route": v.get("demoRoute"), "ac_ids": ac_ids(it)}
    return {"items": out}
