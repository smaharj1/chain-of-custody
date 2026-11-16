"""Claims: two humans, one item (report 16.1 row 11, F-25; built on the remote ref lock, RC-10, RC-12). OPT-IN, advisory.

The remote lock says one RUN works on a plan. A claim says one PERSON works on an item: before you start an item by hand (or want the loop to leave it
alone), `loopctl claim take F2`. It is the same mechanism as the lock, one ref per item:

    refs/loopctl/claim/<plan>/<item>     a commit whose tree is exactly one file, `claim.json`

CREATE-ONLY: the first person's push wins and the second is refused with the first person's name; two people who claim at the same moment cannot both
get it (the push carries `--force-with-lease=<ref>:`, "must not exist"). The same name claiming again renews. A claim expires after `claim_ttl_h` hours;
after that anyone may take it over (the new record names the old holder). `take --override` replaces a live claim, and the new record says it did and
whose it was. `release` deletes it, by its holder or `--override`.

WHAT THE LOOP DOES WITH THEM. The driver never takes a claim. It skips an item somebody else has claimed (an unreadable claim ref counts as claimed) and
runs the others; when nothing else can run it stops and says which items wait on whom.

NAMES ARE LABELS. `by` is whatever the person types (default: `LOOPCTL_USER` or the OS user). Nothing proves who typed it, so two people who type the same
name look like one person who renews. This coordinates cooperating people; it is not a boundary (RC-12), and a well-formed forged claim cannot be told from a
real one. Rulesets cannot protect `refs/loopctl/*`.
"""
from __future__ import annotations

import re
import secrets
import socket
from typing import Any, Callable, Dict, List, Optional

from kitlib import gitutil, runstate

from . import common
from .common import Refused, UsageError
from .remote_lock import RefDoc, Observed, RemoteUnreachable

__all__ = ["Claims", "validate_claim", "claim_ref", "SCHEMA", "CLAIM_FILE", "MIN_TTL_S", "MAX_TTL_S"]

SCHEMA = "loopctl.claim.v1"
CLAIM_FILE = "claim.json"
MIN_TTL_S = 3600.0
MAX_TTL_S = 30 * 86400.0
FUTURE_SKEW_S = 600.0
MAX_CLAIMS = 400
_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}\Z")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}\Z")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")


def claim_ref(plan: str, item: str) -> str:
    if not common.valid_plan_name(plan):
        raise UsageError("invalid plan name %r" % common.clean_text(plan, 40))
    if not common.valid_item_id(item):
        raise UsageError("invalid item id %r" % common.clean_text(item, 30))
    return "refs/loopctl/claim/%s/%s" % (plan, item)


def validate_claim(doc: Any, plan: str, item: str, now: float) -> Optional[str]:
    if not isinstance(doc, dict):
        return "the record is not a JSON object"
    extra = sorted(set(doc) - {"schema", "plan", "item", "by", "token", "host", "claimed", "expires", "ttl_s", "note", "over", "override"})
    if extra:
        return "unknown field(s): %s" % ", ".join(common.clean_text(k, 20) for k in extra[:3])
    if doc.get("schema") != SCHEMA:
        return "schema is not %s" % SCHEMA
    if doc.get("plan") != plan or doc.get("item") != item:
        return "the record is for another plan or item"
    if not isinstance(doc.get("by"), str) or not _BY_RE.match(doc["by"]):
        return "the name is empty or has unusual characters"
    if not isinstance(doc.get("token"), str) or not _TOKEN_RE.match(doc["token"]):
        return "the token is not 32 hex characters"
    if not isinstance(doc.get("host"), str) or not _HOST_RE.match(doc["host"]):
        return "the host is empty or has unusual characters"
    if not isinstance(doc.get("claimed"), str) or runstate.parse_iso(doc["claimed"]) is None:
        return "claimed is not a time"
    exp, ttl = doc.get("expires"), doc.get("ttl_s")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl != ttl or not MIN_TTL_S <= ttl <= MAX_TTL_S:
        return "ttl_s is outside %d hours to %d days" % (MIN_TTL_S // 3600, MAX_TTL_S // 86400)
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp != exp or exp in (float("inf"), float("-inf")) or exp < 0:
        return "expires is not a time"
    if exp > now + MAX_TTL_S + FUTURE_SKEW_S:
        return "the claim expires further ahead than any claim may (a claim like that would never lapse)"
    for k in ("note", "over"):
        v = doc.get(k)
        if v is not None and not (isinstance(v, str) and len(v) <= 200):
            return "%s is not short text" % k
    if doc.get("override") not in (None, True):
        return "override is not a flag"
    return None


class Claims(object):
    def __init__(self, repo: str, remote: str, plan: str, ttl_h: float = 72.0, clock: Optional[Callable[[], float]] = None,
                 host: Optional[str] = None) -> None:
        self.repo, self.remote, self.plan = repo, remote, plan
        claim_ref(plan, "A")                                       # validates the plan name
        self.ttl_s = min(MAX_TTL_S, max(MIN_TTL_S, float(ttl_h) * 3600.0))
        self.clock = clock or common.now
        self.host = host or socket.gethostname()
        self.before_push = None  # type: Optional[Callable[[], None]]

    def _doc(self, item: str) -> RefDoc:
        d = RefDoc(self.repo, self.remote, claim_ref(self.plan, item), CLAIM_FILE, lambda x: validate_claim(x, self.plan, item, self.clock()))
        d.before_push = self.before_push
        return d

    def _record(self, item: str, by: str, note: str, token: Optional[str] = None, claimed: Optional[str] = None, over: Optional[str] = None,
                override: bool = False) -> Dict[str, Any]:
        if not isinstance(by, str) or not _BY_RE.match(by):
            raise UsageError("--by must be a short plain name (letters, digits, . _ @ -)")
        now = self.clock()
        rec = {"schema": SCHEMA, "plan": self.plan, "item": item, "by": by, "token": token or secrets.token_hex(16), "host": self.host,
               "claimed": claimed or common.iso(now), "expires": now + self.ttl_s, "ttl_s": self.ttl_s}
        if note:
            rec["note"] = common.clean_text(note, 160)
        if over:
            rec["over"] = over
        if override:
            rec["override"] = True
        return rec

    # -- reading ------------------------------------------------------------------------------------------------------------------------
    def get(self, item: str) -> Dict[str, Any]:
        """{item, state: none|live|expired|corrupt, claim, why}."""
        return self._summary(item, self._doc(item).observe())

    def _summary(self, item: str, obs: Observed) -> Dict[str, Any]:
        if obs.state == "none":
            return {"item": item, "state": "none", "claim": None, "why": ""}
        if obs.state == "corrupt":
            return {"item": item, "state": "corrupt", "claim": None, "why": obs.why}
        cl = dict(obs.doc or {})
        cl.pop("token", None)
        live = float(cl.get("expires") or 0) > self.clock()
        return {"item": item, "state": "live" if live else "expired", "claim": cl, "why": "",
                "expires_in_s": int(float(cl.get("expires") or 0) - self.clock())}

    def listing(self) -> List[Dict[str, Any]]:
        """Every claim on the plan: one ls-remote, one fetch, then local reads."""
        probe = self._doc("A")
        probe.check_remote()
        pat = "refs/loopctl/claim/%s/*" % self.plan
        proc = probe._git(["ls-remote", "--", self.remote, pat], timeout=60)
        if proc.returncode != 0:
            raise RemoteUnreachable("cannot list claims on %s: %s" % (self.remote, common.clean_text(proc.stderr, 120)))
        prefix = "refs/loopctl/claim/%s/" % self.plan
        listed = {}  # type: Dict[str, str]
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].startswith(prefix) and gitutil.valid_oid(parts[0]):
                item = parts[1][len(prefix):]
                if common.valid_item_id(item):
                    listed[item] = parts[0]
        if not listed:
            return []
        if len(listed) > MAX_CLAIMS:
            raise RemoteUnreachable("%d claim refs on %s for plan %s (more than %d): a flood of refs is not a plan's claims; remove them by hand" % (len(listed), self.remote, self.plan, MAX_CLAIMS))
        docs = {item: self._doc(item) for item in listed}
        specs = ["+%s:%s" % (d.ref, d.cache_ref()) for d in docs.values()]
        f = probe._git(["fetch", "--no-tags", "--quiet", "--"] + [self.remote] + specs, timeout=120)
        if f.returncode != 0:
            raise RemoteUnreachable("cannot fetch claims from %s: %s" % (self.remote, common.clean_text(f.stderr, 120)))
        out = []  # type: List[Dict[str, Any]]
        for item in sorted(listed, key=common.natural_key):
            out.append(self._summary(item, docs[item].examine()))
        return out

    def held_by_others(self, me: str) -> Dict[str, Dict[str, Any]]:
        """{item: summary} for the items someone other than `me` holds right now (a live claim, or a claim ref that cannot be read)."""
        out = {}  # type: Dict[str, Dict[str, Any]]
        for s in self.listing():
            if s["state"] == "corrupt" or (s["state"] == "live" and (s["claim"] or {}).get("by") != me):
                out[s["item"]] = s
        return out

    # -- writing ------------------------------------------------------------------------------------------------------------------------
    def take(self, item: str, by: str, note: str = "", override: bool = False) -> Dict[str, Any]:
        d = self._doc(item)
        obs = d.observe()
        cur = obs.doc or {}
        if obs.state == "none":
            rec = self._record(item, by, note)
            new = d.make_commit(rec, None, "claim %s/%s by %s" % (self.plan, item, by))
            if not d.push(new, None):
                raise Refused("someone claimed %s first (compare-and-swap lost): see `loopctl claim list`" % item)
            return {"item": item, "action": "claimed", "claim": _public(rec)}
        if obs.state == "corrupt":
            if not override:
                raise Refused("%s exists on %s but is not a loopctl claim (%s); it counts as claimed. Look at it, then `claim take %s --override --by NAME`" % (
                    d.ref, self.remote, obs.why, item))
            rec = self._record(item, by, note, override=True)
            new = d.make_commit(rec, None, "claim %s/%s by %s (override of an unreadable claim)" % (self.plan, item, by))
            if not d.push(new, obs.oid):
                raise Refused("the claim changed while it was being replaced (compare-and-swap lost); retry")
            return {"item": item, "action": "overrode an unreadable claim", "claim": _public(rec)}
        mine = cur.get("by") == by
        live = float(cur.get("expires") or 0) > self.clock()
        if mine:
            rec = self._record(item, by, note or cur.get("note") or "", cur.get("token"), cur.get("claimed"))
            action = "renewed"
        elif not live:
            rec = self._record(item, by, note, over=cur.get("by"))
            action = "took over an expired claim of %s" % common.clean_text(cur.get("by"), 40)
        elif override:
            rec = self._record(item, by, note, over=cur.get("by"), override=True)
            action = "overrode the claim of %s" % common.clean_text(cur.get("by"), 40)
        else:
            raise Refused("%s is claimed by %s (since %s, expires in %d hours). Ask them, or `claim take %s --override --by NAME` if they have gone" % (
                item, common.clean_text(cur.get("by"), 40), common.clean_text(cur.get("claimed"), 24),
                max(0, int((float(cur.get("expires") or 0) - self.clock()) // 3600)), item))
        new = d.make_commit(rec, obs.oid, "claim %s/%s by %s (%s)" % (self.plan, item, by, action.split(" ")[0]))
        if not d.push(new, obs.oid):
            raise Refused("the claim changed while it was being written (compare-and-swap lost); look at `loopctl claim list` and retry")
        return {"item": item, "action": action, "claim": _public(rec)}

    def release(self, item: str, by: str, override: bool = False) -> Dict[str, Any]:
        d = self._doc(item)
        obs = d.observe()
        if obs.state == "none":
            raise Refused("%s has no claim" % item)
        cur = obs.doc or {}
        if obs.state == "corrupt":
            if not override:
                raise Refused("%s is not a loopctl claim (%s); `claim release %s --override --by NAME` removes it after you have looked" % (d.ref, obs.why, item))
        elif cur.get("by") != by and not override:
            raise Refused("%s is claimed by %s, not %s; only they can release it (or `--override`)" % (item, common.clean_text(cur.get("by"), 40), common.clean_text(by, 40)))
        if not d.delete(obs.oid or ""):
            raise Refused("the claim changed while it was being released (compare-and-swap lost); retry")
        return {"item": item, "action": "released", "was": common.clean_text(cur.get("by") or "(unreadable)", 40)}


def _public(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if k != "token"}
