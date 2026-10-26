"""The per-invocation context: repository, run state, the BASE sha, the config and plan as read at that sha.

The base sha is the commit a branch was cut from (`merge-base HEAD <default branch>`, the same rule `verify.py` uses), or
`--base`. Config and plan are read from it, never from the branch under judgement (CO-11): a change set cannot raise its own
budget, turn a gate off, opt itself into Tier 2 or edit its own acceptance criteria. When the working tree's H6 keys differ from
the base's, `config_drift` lists them; they take effect only after a human merges them.

A context built without a trusted config (no loop.config committed at the base) can still serve the read commands, but
`tier2` is False and every Tier 2 command refuses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kitlib import caller as caller_mod
from kitlib import config as kconfig
from kitlib import evidence, gitutil, levels, runstate

from . import lgit, common, gates, ledger as ledger_mod, lock as lock_mod, plan as plan_mod, state as state_mod
from .common import Refused, UsageError

__all__ = ["Context", "SCRIPTS_DIR"]

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UNSET = object()


def _resolve_base(repo: str, base: Optional[str]) -> Optional[str]:
    """`--base` as given, else the merge-base of HEAD with the default branch. None when neither exists."""
    if base:
        if base.startswith("-"):
            raise UsageError("--base may not start with '-'")
        sha = gitutil.rev_parse(base + "^{commit}", repo, isolated=True)
        if not sha:
            raise UsageError("--base %s does not resolve to a commit" % common.clean_text(base, 60))
        return sha
    wanted = ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main", "refs/heads/master")
    have = lgit.run_git(["for-each-ref", "--format=%(refname)"] + list(wanted), cwd=repo, isolated=True, pin_hooks=True).stdout.split()
    for cand in wanted:                      # the same order verify.py uses
        if cand not in have:
            continue
        proc = lgit.run_git(["merge-base", "HEAD", cand], cwd=repo, isolated=True, pin_hooks=True)
        out = proc.stdout.strip()
        if proc.returncode == 0 and out:
            return out.splitlines()[0]
    return gitutil.rev_parse("HEAD^{commit}", repo, isolated=True)   # no default branch: the repo's own HEAD is the base (None if unborn)


class Context(object):
    def __init__(self) -> None:
        self.repo = ""
        self.rs = None  # type: Any
        self.env = {}  # type: Mapping[str, str]
        self.base_sha = None  # type: Optional[str]
        self.cfg = None  # type: Any
        self.cfg_trusted = False
        self.cfg_note = ""
        self.config_drift = []  # type: List[Tuple[str, Any, Any]]
        self.plan = None  # type: Any
        self.plan_trusted = False
        self.plan_source = ""
        self._evidence = None  # type: Any
        self._ib = _UNSET  # type: Any

    # -- construction ---------------------------------------------------------------------------------------------
    @classmethod
    def build(cls, path: str, env: Optional[Mapping[str, str]] = None, base: Optional[str] = None, plan: str = "auto",
              allow_head_config: bool = True) -> "Context":
        """`plan`: `base` (the committed plan at the base sha), `worktree` (a draft), `auto` (base, else the working tree,
        marked untrusted) or `none`."""
        c = cls()
        d = gitutil.nearest_existing_dir(path)
        proc = lgit.run_git(["rev-parse", "--show-toplevel", "--git-common-dir"], cwd=d, isolated=True) if d else None
        lines = proc.stdout.splitlines() if proc is not None and proc.returncode == 0 else []
        if len(lines) < 2:
            raise UsageError("%s is not inside a git repository" % common.clean_text(path, 80))
        c.repo = os.path.realpath(lines[0])
        common_dir = os.path.realpath(lines[1] if os.path.isabs(lines[1]) else os.path.join(d, lines[1]))
        c.env = os.environ if env is None else env
        c.rs = runstate.RunState(common_dir)
        c.base_sha = _resolve_base(c.repo, base)
        preloaded = None
        if plan in ("base", "auto") and c.base_sha:
            preloaded = plan_mod.load_at_rev(c.repo, c.base_sha, [kconfig.CONFIG_PATH])
        c._load_config(allow_head_config, preloaded)
        if plan != "none":
            c._load_plan(plan, preloaded)
        return c

    def _load_config(self, allow_head: bool, preloaded: Optional[plan_mod.LoadedPlan] = None) -> None:
        if self.base_sha:
            try:
                text = preloaded.extra.get(kconfig.CONFIG_PATH) if preloaded is not None else gitutil.show_file(
                    self.base_sha, kconfig.CONFIG_PATH, self.repo, isolated=True)
                if text is None:
                    raise kconfig.ConfigError("%s does not exist at %s" % (kconfig.CONFIG_PATH, self.base_sha[:12]))
                self.cfg = kconfig.load_text(text, source="%s:%s" % (self.base_sha[:12], kconfig.CONFIG_PATH))
                self.cfg_trusted = True
                self.cfg_note = "loop.config read at base sha %s" % self.base_sha[:12]
            except kconfig.ConfigError as exc:
                self.cfg_note = "no usable loop.config at the base sha (%s)" % common.clean_text(exc, 100)
        if self.cfg is None:
            path = os.path.join(self.repo, kconfig.CONFIG_PATH)
            if allow_head and os.path.isfile(path):
                try:
                    self.cfg = kconfig.load_file(path)
                    self.cfg_note += "; using the working tree's loop.config (untrusted: Tier 2 refuses)"
                except (OSError, kconfig.ConfigError) as exc:
                    self.cfg_note += "; the working tree's loop.config is unusable (%s)" % common.clean_text(exc, 100)
            if self.cfg is None:
                from kitlib import registry
                self.cfg = kconfig.Config({}, registry.load_registry(), [], "<defaults>")
                self.cfg_note += "; registry defaults in use"
        if self.cfg.errors:
            raise Refused("loop.config has errors: " + "; ".join(str(i) for i in self.cfg.errors[:3]))
        if self.cfg_trusted:
            head_path = os.path.join(self.repo, kconfig.CONFIG_PATH)
            if os.path.isfile(head_path):
                try:
                    self.config_drift = kconfig.h6_changes(self.cfg, kconfig.load_file(head_path))
                except (OSError, kconfig.ConfigError):
                    self.config_drift = [("<unparsable working-tree config>", None, None)]

    def _load_plan(self, mode: str, preloaded: Optional[plan_mod.LoadedPlan] = None) -> None:
        if mode in ("base", "auto") and self.base_sha:
            lp = preloaded if preloaded is not None else plan_mod.load_at_rev(self.repo, self.base_sha)
            if not lp.fatal or mode == "base":
                self.plan, self.plan_trusted, self.plan_source = lp, True, "base sha %s" % self.base_sha[:12]
                return
        if mode in ("worktree", "auto"):
            self.plan = plan_mod.load_worktree(self.repo)
            self.plan_trusted = False
            self.plan_source = "working tree (a draft: not trusted for gate decisions)"
            return
        self.plan = plan_mod.LoadedPlan("none")
        self.plan.err("plan.missing", "no plan could be read")

    # -- config-derived facts ------------------------------------------------------------------------------------------
    @property
    def tier2(self) -> bool:
        return bool(self.cfg_trusted and self.cfg.get("loopctl_tier2") is True)

    def require_tier2(self) -> None:
        """Raise Refused unless Tier 2 is on, trusted, and everything it reads comes from the base sha."""
        if not self.cfg_trusted:
            raise Refused("Tier 2 needs a loop.config committed at the base sha (%s). Commit it, or pass --base." % self.cfg_note)
        if not self.tier2:
            raise Refused("loopctl state writes (Tier 2) are off. Set loopctl_tier2: true in .claude/loop.config.md, commit it, and a "
                          "human reviews that change (H6). Tier 1 commands work without it.")
        if self.plan is None or self.plan.fatal or not self.plan_trusted:
            raise Refused("Tier 2 reads the plan committed at the base sha; %s" % (
                self.plan.findings[0].message if self.plan and self.plan.findings else "none was found"))
        bad = self.opt_in_violations()
        if bad:
            raise Refused("loop.config sets %s, which needs the evidence gate (%s). OPT-IN features refuse to enable (IMP-1)"
                          % (", ".join(bad), common.clean_text(self.evidence().reason, 100)))
        self._check_base_anchor()

    def _check_base_anchor(self) -> None:
        """The base sha is where gate decisions take their config and plan from, so a caller must not be able to choose a weaker one
        with `--base`. It has to be a commit on the integration branch (reviewed, merged work), and never older than the base this run
        started from (config only moves forward, through merges a human reviewed)."""
        ib = self.integration_branch()
        if not ib or not self.base_sha:
            return
        if not gitutil.is_ancestor(self.base_sha, "refs/heads/" + ib, self.repo, isolated=True):
            raise Refused("the base %s is not on the integration branch %s: gate decisions read the config and plan from reviewed, merged "
                          "commits only" % (self.base_sha[:12], ib))
        try:
            run = self.rs.read_run()
        except common.READ_ERRORS:
            run = None
        start = run.get("baseSha") if isinstance(run, dict) else None
        if isinstance(start, str) and gitutil.valid_oid(start) and start != self.base_sha \
                and gitutil.rev_parse(start + "^{commit}", self.repo, isolated=True) == start \
                and not gitutil.is_ancestor(start, self.base_sha, self.repo, isolated=True):
            raise Refused("the base %s is older than the base this run started from (%s): the config and plan cannot be rolled back"
                          % (self.base_sha[:12], start[:12]))

    @property
    def runner(self) -> str:
        return str(self.cfg.get("runner"))

    def roster(self) -> List[str]:
        out = list(caller_mod.DEFAULT_ROSTER)
        extra = self.cfg.get("roster")
        if isinstance(extra, dict):
            out += [str(k) for k in extra]
        return out

    def integration_branch(self) -> Optional[str]:
        if self._ib is not _UNSET:
            return self._ib
        self._ib = self._integration_branch()
        return self._ib

    def _integration_branch(self) -> Optional[str]:
        b = self.cfg.get("integration_branch")
        if isinstance(b, str) and b.strip():
            return b.strip() if gitutil.rev_parse("refs/heads/" + b.strip(), self.repo, isolated=True) else None
        for cand in ("main", "master"):
            if gitutil.rev_parse("refs/heads/" + cand, self.repo, isolated=True):
                return cand
        return None

    def protected_branches(self) -> List[str]:
        out = ["main", "master"]
        for b in self.cfg.get("protected_branches") or []:
            if isinstance(b, str) and b not in out:
                out.append(b)
        return out

    def evidence(self) -> Any:
        if self._evidence is None:
            self._evidence = evidence.check(self.repo)
        return self._evidence

    def evidence_passed(self) -> bool:
        return bool(self.evidence())

    def opt_in_violations(self) -> List[str]:
        """Evidence-gated config values that are set while the evidence gate has not passed (IMP-1)."""
        if self.evidence_passed():
            return []
        return self.cfg.evidence_gated_violations()

    # -- collaborators --------------------------------------------------------------------------------------------------------
    def plan_name(self) -> str:
        if self.plan is None or not self.plan.name:
            raise UsageError("the plan has no usable name (plan/plan.json)")
        return str(self.plan.name)

    def store(self) -> state_mod.StateStore:
        remote = self.cfg.get("state_remote")
        return state_mod.StateStore(self.repo, self.plan_name(), remote if isinstance(remote, str) else "")

    def lock(self) -> lock_mod.RunLock:
        return lock_mod.RunLock(self.rs, self.cfg.get_int("lock_stale_min"))

    def ledger(self) -> ledger_mod.Ledger:
        return ledger_mod.Ledger(self.rs)

    def required_gates_for(self, frm: Optional[str], to: str) -> List[str]:
        if frm is None:
            return []
        return gates.required_for(self.cfg, frm, to)

    def worktree_is_loop_worktree(self) -> bool:
        run = None
        try:
            run = self.rs.read_run()
        except common.READ_ERRORS:
            return False
        return bool(run and run.get("worktree") and os.path.realpath(run["worktree"]) == os.path.realpath(self.repo))

    # -- level ----------------------------------------------------------------------------------------------------------------------
    def level_caps(self) -> List[levels.Cap]:
        caps = [levels.evidence_cap(self.evidence_passed(), self.evidence().reason if not self.evidence_passed() else "")]
        return caps

    def effective_level(self, run=subprocess.run) -> Dict[str, Any]:
        """The doctor's effective level (it prints every cap and its reason), read at the base sha. The doctor is
        a separate program: this runs it and parses its `doctor.v1` JSON. Fails closed: an unreadable answer is L0."""
        argv = [sys.executable, os.path.join(SCRIPTS_DIR, "doctor.py"), "--json", "--repo", self.repo]
        if self.base_sha:
            argv += ["--config-ref", self.base_sha]
        try:
            proc = run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, timeout=120, env=dict(self.env))
            doc = json.loads(proc.stdout.decode("utf-8", "replace"))
            lvl = doc["level"]
            return {"configured": lvl.get("configured"), "effective": lvl.get("effective"), "caps": lvl.get("caps", []), "next": doc.get("next")}
        except Exception as exc:  # noqa: BLE001
            return {"configured": None, "effective": "L0", "caps": [], "error": "the doctor could not be read (%s)" % common.clean_text(exc, 80)}

    # -- gates ---------------------------------------------------------------------------------------------------------------
    def gate_context(self, item_id: str, head: Optional[str] = None, worktree: Optional[str] = None,
                     state_item: Optional[Dict[str, Any]] = None, level: Optional[str] = None) -> "gates.GateContext":
        if item_id not in self.plan.items:
            raise UsageError("item %r is not in the plan" % common.clean_text(item_id, 30))
        if not self.base_sha:
            raise Refused("there is no base sha to judge against")
        rec = state_item or {}
        reports = os.path.join(self.repo, "docs", "features", self.plan.items[item_id]["slug"], "reports")
        return gates.GateContext(
            repo=self.repo, item_id=item_id, item=self.plan.items[item_id], attempt=int(rec.get("attempt", 1)),
            base_sha=self.base_sha, head_sha=head or gitutil.rev_parse("HEAD^{commit}", worktree or self.repo, isolated=True),
            cfg=self.cfg, plan=self.plan, state_item=dict(rec),
            paths={"repo": self.repo, "runstate": self.rs.root, "heldout": self.rs.heldout_dir, "reports": reports},
            runner=self.runner, level=level, env=dict(self.env), evidence_passed=self.evidence_passed(),
            worktree=worktree)
