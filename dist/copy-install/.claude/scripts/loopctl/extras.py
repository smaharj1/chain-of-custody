"""The command line of the loopctl extras (Phase 11b): `remote-lock`, `claim`, `merge-class`, `manifest`. `loopctl/cli.py` routes to these.

Classification (the exec guard reads the same table; see `loopctl/cli.py` and `guards/g_exec.py`):

    remote-lock status        READ           the lock on the remote, without its token
    remote-lock acquire|renew|release|steal   TIER 1 WRITE   push to refs/loopctl/lock/<plan> on the remote named by `remote_lock`
    claim list|show           READ           the claims on the plan's items
    claim take|release        TIER 1 WRITE   push to refs/loopctl/claim/<plan>/<item>
    merge-class               READ           would this item be eligible for the L5 low-risk lane? (nothing here merges)
    manifest show|verify      READ           a reproducibility manifest, or its check against git
    manifest write            TIER 1 WRITE   writes <git-common-dir>/loopctl/manifests/<plan>-<item>-<head>.json

`remote-lock`, `claim` and a non-`human` `merge_policy` are OPT-IN and evidence-gated (IMP-1): they refuse while docs/baseline/evidence.json is incomplete. Every push
is a compare-and-swap loopctl runs itself, to the remote NAMED in the base config, never to a URL. None of these commands merges, opens a pull request or runs `gh`.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Mapping, Optional

from kitlib import gitutil, jsonio

from . import common
from .common import EXIT_FINDINGS, EXIT_OK, EXIT_REFUSED, Refused, UsageError

__all__ = ["cmd_remote_lock", "cmd_claim", "cmd_merge_class", "cmd_manifest", "LEVEL_FN"]

# The doctor's effective level for `merge-class`. None = ask the real doctor (as unattended does); a test replaces it.
LEVEL_FN = None  # type: Optional[Callable[[Any], Optional[str]]]


def _cli() -> Any:
    from . import cli
    return cli


def _need_remote(ctx: Any) -> str:
    """The configured remote lock name, after the evidence gate. Raises Refused."""
    remote = ctx.cfg.get("remote_lock")
    if not isinstance(remote, str) or not remote:
        raise Refused("remote_lock is not set in the base config (OPT-IN, EXPERIMENTAL): name the git remote that holds refs/loopctl/lock/<plan>")
    bad = ctx.opt_in_violations()
    if "remote_lock" in bad:
        raise Refused("remote_lock is OPT-IN and evidence-gated: docs/baseline/evidence.json is not complete (IMP-1)")
    return remote


def _owner(a: Any, env: Mapping[str, str]) -> str:
    from .remote_lock import identity
    return common.clean_text(getattr(a, "owner", None) or getattr(a, "by", None) or identity(dict(env)), 60)


def _line(label: str, value: Any) -> str:
    return "  %-10s %s" % (label, common.clean_text(value, 160))


# -- remote-lock ---------------------------------------------------------------------------------------------------------------------

def cmd_remote_lock(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("remote-lock", "remote-lock status | acquire | renew | release | steal: one run per plan across machines, an advisory ref on the remote "
                                    "(refs/loopctl/lock/<plan>). Needs `remote_lock: <remote name>` and the evidence gate. Not a security boundary (RC-12).")
    ap.add_argument("action", choices=("status", "acquire", "renew", "release", "steal"))
    ap.add_argument("--owner", default=None, help="who holds it, as people should read it (default: LOOPCTL_USER or the OS user)")
    ap.add_argument("--host-dead", action="store_true", help="steal: you know the machine that holds the lock is gone")
    ap.add_argument("--corrupt", action="store_true", help="steal: replace a ref that is not a lock record, after you have looked at it")
    ap.add_argument("--after", type=float, default=None, help="steal: minutes without a heartbeat (default: the record's own limit; never shorter)")
    a = ap.parse_args(args)
    from . import remote_lock as rl
    ctx = cli._ctx(a, env, plan="base")
    remote = _need_remote(ctx)
    plan = ctx.plan_name()
    lock = rl.RemoteLock(ctx.repo, remote, plan, ttl_s=float(ctx.cfg.get_int("lock_stale_min") or 60) * 60.0)
    if a.action == "status":
        st = lock.status(host_dead=a.host_dead)
        if a.json:
            out.json(dict(st, schema="loopctl.remote-lock-status.v1"))
        elif st["state"] == "none":
            out.text("remote lock %s on %s: free" % (st["ref"], remote))
        elif st["state"] == "corrupt":
            out.text("remote lock %s on %s: NOT A LOCK RECORD (%s); it counts as held; `remote-lock steal --corrupt` replaces it after you have looked" % (
                st["ref"], remote, common.clean_text(st["why"], 160)))
        else:
            rec = st["record"]
            out.text("remote lock %s on %s: held by %s on %s (run %s), heartbeat %ss ago%s" % (
                st["ref"], remote, common.clean_text(rec.get("owner"), 40), common.clean_text(rec.get("host"), 40), common.clean_text(rec.get("runId") or "-", 40), st["age_s"],
                " (STALE: %s)" % common.clean_text(st["stale_why"], 200) if st["stale"] else ""))
        return EXIT_OK
    if ctx.rs.is_halted() and a.action != "release":
        raise Refused("HALT is set; the remote lock is not handed out")
    owner = _owner(a, env)
    run = None
    try:
        rec = ctx.rs.read_run()
        run = rec.get("runId") if isinstance(rec, dict) and isinstance(rec.get("runId"), str) else None
    except common.READ_ERRORS:
        run = None
    keeper = rl.Keeper(lock, ctx.rs, owner, run_id=run)
    if a.action == "acquire":
        held = keeper.start()
        msg = "acquired"
    elif a.action == "steal":
        held = lock.steal(owner, host_dead=a.host_dead, corrupt=a.corrupt, after_s=(a.after * 60.0 if a.after else None), run_id=run)
        keeper.held = held
        keeper._remember()
        msg = "stolen"
    else:
        keeper.held = _remembered(ctx, rl, remote, plan, owner)
        if a.action == "renew":
            ok = lock.renew(keeper.held)
            if ok:
                keeper._remember()
            out.text("renewed" if ok else "this machine no longer holds the remote lock")
            return EXIT_OK if ok else EXIT_REFUSED
        ok = keeper.release()
        out.text("released" if ok else "this machine did not hold the remote lock (nothing was removed)")
        return EXIT_OK if ok else EXIT_REFUSED
    doc = {"schema": "loopctl.remote-lock.v1", "action": msg, "ref": lock.ref, "remote": remote, "owner": owner, "oid": held.oid}
    out.json(doc) if a.json else out.text("remote lock %s: %s on %s by %s" % (msg, lock.ref, remote, owner))
    return EXIT_OK


def _remembered(ctx: Any, rl: Any, remote: str, plan: str, owner: str) -> Any:
    """The token this machine wrote when it took the lock (renew and release need it)."""
    try:
        prior = jsonio.read_json_opt(rl.state_path(ctx.rs))
    except common.READ_ERRORS:
        prior = None
    if not (isinstance(prior, dict) and prior.get("plan") == plan and prior.get("remote") == remote and isinstance(prior.get("token"), str)):
        raise Refused("this machine has no record of holding the remote lock for %s: nothing to renew or release (`remote-lock status` shows who has it)" % plan)
    return rl.Held(prior["token"], str(prior.get("oid") or ""), owner)


# -- claim ------------------------------------------------------------------------------------------------------------------------------

def cmd_claim(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("claim", "claim list | show ITEM | take ITEM | release ITEM: who is working on which item (F-25). One advisory ref per item on the remote "
                              "(refs/loopctl/claim/<plan>/<item>); the first claim wins. The driver skips an item someone else has claimed.")
    ap.add_argument("action", choices=("list", "show", "take", "release"))
    ap.add_argument("item", nargs="?", default=None)
    ap.add_argument("--by", default=None, help="your name (default: LOOPCTL_USER or the OS user)")
    ap.add_argument("--note", default="", help="take: a short note for the next person")
    ap.add_argument("--override", action="store_true", help="take or release a claim that is someone else's and still live (recorded)")
    a = ap.parse_args(args)
    from . import claims as claims_mod
    ctx = cli._ctx(a, env, plan="base")
    remote = _need_remote(ctx)
    cl = claims_mod.Claims(ctx.repo, remote, ctx.plan_name(), ttl_h=float(ctx.cfg.get_int("claim_ttl_h") or 72))
    if a.action == "list":
        rows = cl.listing()
        if a.json:
            out.json({"schema": "loopctl.claims.v1", "plan": ctx.plan_name(), "claims": rows})
            return EXIT_OK
        if not rows:
            out.text("no claims on %s" % ctx.plan_name())
        for r in rows:
            c = r.get("claim") or {}
            out.text("  %-6s %-8s %s" % (r["item"], r["state"], ("by %s since %s, %s" % (common.clean_text(c.get("by"), 40), common.clean_text(c.get("claimed"), 24),
                                                                                 "expires in %dh" % max(0, int(r.get("expires_in_s", 0)) // 3600) if r["state"] == "live" else "expired")
                                                       if c else common.clean_text(r.get("why"), 120))))
        return EXIT_OK
    if not a.item:
        raise UsageError("%s needs an item id" % a.action)
    if ctx.plan and not ctx.plan.fatal and a.item not in ctx.plan.items:
        raise UsageError("the plan has no item %s" % common.clean_text(a.item, 30))
    if a.action == "show":
        r = cl.get(a.item)
        out.json(r) if a.json else out.text("%s: %s%s" % (a.item, r["state"], (" by %s" % common.clean_text((r["claim"] or {}).get("by"), 40)) if r["claim"] else ""))
        return EXIT_OK
    if ctx.rs.is_halted():
        raise Refused("HALT is set; claims are not changed")
    by = _owner(a, env)
    res = cl.take(a.item, by, a.note, a.override) if a.action == "take" else cl.release(a.item, by, a.override)
    out.json(dict(res, schema="loopctl.claim.v1")) if a.json else out.text("%s: %s" % (a.item, res["action"]))
    return EXIT_OK


# -- merge-class ----------------------------------------------------------------------------------------------------------------------------

def cmd_merge_class(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("merge-class", "merge-class ITEM: would this item be eligible for the L5 low-risk lane? Reads only and merges nothing: a person merges every pull "
                                    "request (merge_policy: human). Exit 0 = eligible, 1 = not eligible.")
    ap.add_argument("item")
    ap.add_argument("--head", default="HEAD", help="the commit to judge (default HEAD); the diff is base..head")
    ap.add_argument("--paths", default=None, help="what-if: judge these comma-separated paths instead of the diff (the answer says so)")
    a = ap.parse_args(args)
    from . import merge_class as mc
    ctx = cli._ctx(a, env, plan="base")
    if not common.valid_item_id(a.item) or not ctx.plan or ctx.plan.fatal or a.item not in ctx.plan.items:
        raise UsageError("the plan has no item %s" % common.clean_text(a.item, 30))
    if a.head.startswith("-"):
        raise UsageError("--head may not start with '-'")
    head = gitutil.rev_parse(a.head + "^{commit}", ctx.repo, isolated=True)
    if not head:
        raise UsageError("--head %s does not resolve to a commit" % common.clean_text(a.head, 40))
    files = None
    if a.paths is not None:
        files = [p.strip() for p in a.paths.split(",") if p.strip()]
    fn = LEVEL_FN or _default_level
    try:
        level = fn(ctx)
    except Exception as exc:  # noqa: BLE001 - a doctor that cannot be read is "not L5", never a crash and never a yes
        level = None
        out.err("loopctl merge-class: the doctor's level could not be read (%s); an unknown level is not L5" % common.clean_text(exc, 80))
    facts = mc.facts_from_context(ctx, a.item, head, level, files)
    verdict = mc.classify(facts)
    if files is not None:
        verdict["what_if"] = True
    if a.json:
        out.json(verdict)
    else:
        out.text("merge-class %s at %s: %s" % (a.item, head[:12], "ELIGIBLE for the low-risk lane (a person still merges)" if verdict["eligible"] else "not eligible; a person merges"))
        for r in verdict["rules"]:
            out.text("  [%s] %-7s %s" % ("ok" if r["ok"] else "NO", r["id"].split(".", 1)[1], common.clean_text(r["message"], 220)))
        if verdict.get("what_if"):
            out.text("  (what-if: the paths were given with --paths, not read from git)")
    return EXIT_OK if verdict["eligible"] else EXIT_FINDINGS


def _default_level(ctx: Any) -> Optional[str]:
    """The doctor's effective level; None against the test double (and so never L5)."""
    from . import unattended
    return unattended.default_level(ctx)


# -- manifest -------------------------------------------------------------------------------------------------------------------------------

def cmd_manifest(args: List[str], env: Mapping[str, str], out: Any) -> int:
    cli = _cli()
    ap = cli._parser("manifest", "manifest write [ITEM] | show [NAME] | verify [NAME]: the reproducibility manifest (F-26): what a piece of the loop ran on, sealed with a hash "
                                 "and checkable against git. `verify` re-reads the config, plan, pins and lockfiles at the recorded commits.")
    ap.add_argument("action", choices=("write", "show", "verify"))
    ap.add_argument("name", nargs="?", default=None, help="write: the item id (default: the whole plan); show, verify: a stored name or `latest` (default)")
    ap.add_argument("--head", default="HEAD", help="write: the commit the manifest describes (default HEAD)")
    ap.add_argument("--file", default=None, help="verify, show: a manifest file anywhere (a regular file, at most 256 KiB)")
    ap.add_argument("--expect-hash", default=None, help="verify: the hash you were given by a source the writer cannot edit (the PR's audit block)")
    a = ap.parse_args(args)
    from . import manifest as mf
    ctx = cli._ctx(a, env, plan="base" if a.action == "write" else "none")
    if a.action == "write":
        iid = a.name
        if iid is not None and (not common.valid_item_id(iid) or not ctx.plan or ctx.plan.fatal or iid not in ctx.plan.items):
            raise UsageError("the plan has no item %s" % common.clean_text(iid, 30))
        if a.head.startswith("-"):
            raise UsageError("--head may not start with '-'")
        head = gitutil.rev_parse(a.head + "^{commit}", ctx.repo, isolated=True)
        if not head or not ctx.base_sha:
            raise UsageError("--head does not resolve to a commit, or there is no base commit")
        sitem = None
        try:
            st, _ = ctx.store().read()
            sitem = st.items.get(iid) if st is not None and iid else None
        except common.LoopctlError:
            sitem = None
        run = None
        try:
            rec = ctx.rs.read_run()
            run = rec.get("runId") if isinstance(rec, dict) and isinstance(rec.get("runId"), str) else None
        except common.READ_ERRORS:
            run = None
        doc = mf.build(ctx.repo, ctx.base_sha, head, iid, sitem, dict(env), run, ctx.plan.name if ctx.plan else None)
        path = mf.write(ctx.rs, doc)
        if a.json:
            out.json({"schema": "loopctl.manifest-written.v1", "path": path, "hash": doc["hash"]})
        else:
            out.text("manifest written: %s" % os.path.relpath(path, ctx.repo) if path.startswith(ctx.repo) else "manifest written: %s" % path)
            out.text("hash: %s   (put this where the writer cannot edit it, e.g. the PR's audit block; `manifest verify --expect-hash` checks it)" % doc["hash"])
        return EXIT_OK
    path = a.file or mf.find(ctx.rs, a.name or "latest")
    doc = mf.read_file(path)
    if a.action == "show":
        out.json(doc) if a.json else out.text(_show(doc))
        return EXIT_OK
    anchored = any(e.get("event") == "manifest" and e.get("hash") == doc.get("hash") for e in ctx.rs.read_events("loopctl"))
    res = mf.verify(doc, ctx.repo, a.expect_hash, anchored)
    if a.json:
        out.json(res)
    else:
        out.text("manifest %s: %s" % (common.clean_text(os.path.basename(path), 60), "OK" if res["ok"] else "DOES NOT HOLD"))
        out.text(_line("seal", "the recorded hash matches the document" if res["seal"] else "the recorded hash does NOT match the document"))
        out.text(_line("anchor", "this machine wrote a manifest with this hash" if res["anchored"] else "not in this machine's event log (a manifest from elsewhere, or the log is gone)"))
        if res["expect"] is not None:
            out.text(_line("expected", "the hash is the expected one" if res["expect"] else "the hash is NOT the expected one"))
        out.text(_line("checked", ", ".join(res["checked"]) or "nothing could be re-derived"))
        out.text(_line("recorded", "%s (the seal protects them; git cannot re-derive them)" % ", ".join(res["not_rederivable"])))
        for d in res["facts"][:12]:
            out.text("  DIFFERS  %s: recorded %s, git holds %s" % (d["field"], d["recorded"], d["git_holds"]))
        for p in res["problems"]:
            out.text("  PROBLEM  " + common.clean_text(p, 200))
    return EXIT_OK if res["ok"] else EXIT_FINDINGS


def _show(doc: Dict[str, Any]) -> str:
    rp = doc.get("repo") or {}
    pl = doc.get("plan") or {}
    lines = ["manifest %s" % common.clean_text(doc.get("hash"), 70),
             _line("written", doc.get("created")), _line("plan", "%s item %s" % (pl.get("name"), pl.get("item") or "(all)")),
             _line("base", rp.get("base")), _line("head", rp.get("head")), _line("kit", (doc.get("kit") or {}).get("version")),
             _line("config", (doc.get("config") or {}).get("sha256")), _line("toolchain", ", ".join(sorted((doc.get("toolchain") or {}))) or "none pinned"),
             _line("lockfiles", ", ".join(sorted((doc.get("lockfiles") or {}))) or "none"), _line("gates", ", ".join(
                 "%s=%s" % (k, (v or {}).get("status")) for k, v in sorted((doc.get("gates") or {}).items())) or "none recorded")]
    return "\n".join(lines)
