"""Failure taxonomy and retry caps (report section 10.4, CO-8, CO-22, L-8, RC-19).

Every failure carries a `failure_class` (CO-8):

    class       example                           retry?               uses retry budget?   trips the breaker?
    platform    API 5xx, overloaded, CLI crash    yes, backoff+jitter  no (own cap)         no
    transient   flaky test, port in use           yes, x2              yes                  no
    env         toolchain, DB down, missing var   no: park             no                   no; demotes the level
    item        tests fail, review blocked        per max_retries      yes                  yes (no-progress: same
                                                                                            failing AC ids twice)

`decide()` is a pure function: item counters + config + the failure in, a `Decision` out. `state.fail()` applies it.
Counters are durable in state, are independent per loop (verify, review, partial, design, ci_fix: never multiplied,
L-8) and are reset only by a discard (a fresh attempt). The class of a known reason is fixed: a caller cannot
relabel an `env` failure as `platform` to dodge a cap (a test row pins this).

Discard path (RC-19): the stage cap exhausted, a no-progress repeat or a reward-hack signature discards the branch and
starts attempt a<n+1> from the item's base; a second discard on the same item is a hard stop and trips the breaker.
A critic cap hit (design) and the CI-fix cap are hard stops with no discard.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence

from .common import Refused, UsageError

__all__ = ["CLASSES", "PLATFORM", "TRANSIENT", "ENV", "ITEM", "KNOWN_REASONS", "COUNTERS", "STAGE_COUNTER",
           "Decision", "decide", "classify_exit", "backoff_s", "reason_class", "check_reason"]

PLATFORM, TRANSIENT, ENV, ITEM = "platform", "transient", "env", "item"
CLASSES = (PLATFORM, TRANSIENT, ENV, ITEM)

# reason -> class. A known reason keeps its class whatever the caller says.
KNOWN_REASONS = {
    "api_5xx": PLATFORM, "overloaded": PLATFORM, "rate_limit": PLATFORM, "cli_crash": PLATFORM, "network": PLATFORM,
    "flaky_test": TRANSIENT, "port_in_use": TRANSIENT, "stall": TRANSIENT, "timeout": TRANSIENT,
    "toolchain_mismatch": ENV, "db_down": ENV, "missing_env_var": ENV, "doctor_failed": ENV, "tree_dirty": ENV,
    "canary_missing": ENV, "disk_full": ENV,
    "tests_failed": ITEM, "review_blocked": ITEM, "ac_unclear": ITEM, "malformed_report": ITEM, "reward_hack": ITEM,
    "partial": ITEM, "lint_failed": ITEM, "build_failed": ITEM, "ci_failed": ITEM, "design_blocked": ITEM,
}
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}\Z")

# the independent per-attempt counters (L-8)
COUNTERS = ("verify", "review", "partial", "design", "ci_fix", "transient", "clarify")
# the stage a failure belongs to -> the counter it uses
STAGE_COUNTER = {"verifying": "verify", "verify": "verify", "reviewing": "review", "review": "review", "partial": "partial",
                 "designing": "design", "design_review": "design", "design": "design", "pr_open": "ci_fix", "awaiting_human": "ci_fix",
                 "ci_fix": "ci_fix", "building": "verify", "stubbing": "verify"}
# where an item-class retry goes back to
RETRY_STATUS = {"verifying": "building", "reviewing": "building", "design_review": "designing", "pr_open": "building",
                "awaiting_human": "building", "building": "building", "designing": "designing", "stubbing": "stubbing"}


class Decision(object):
    """What to do about a failure. `action`: retry | discard | park | hard_stop."""

    def __init__(self, action: str, fclass: str, reason: str) -> None:
        self.action = action
        self.fclass = fclass
        self.reason = reason
        self.counter = None  # type: Optional[str]
        self.consumes = False            # used retry budget (the stage counter went up)
        self.backoff_s = 0
        self.breaker = False             # the circuit breaker goes up by one
        self.demote = False              # the effective level is capped (env)
        self.block_reason = None  # type: Optional[str]
        self.to_status = None  # type: Optional[str]
        self.no_progress = False
        self.note = ""
        self.counters = {}  # type: Dict[str, int]   # the new counter values
        self.new_attempt = False
        self.status_kind = None  # type: Optional[str]   # blocked | needs_human for park and hard_stop

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    def __repr__(self) -> str:
        return "Decision(%s, %s/%s, counter=%s)" % (self.action, self.fclass, self.reason, self.counter)


def reason_class(reason: str) -> Optional[str]:
    return KNOWN_REASONS.get(reason)


def check_reason(fclass: str, reason: str) -> None:
    if fclass not in CLASSES:
        raise UsageError("failure class must be one of %s, got %r" % (", ".join(CLASSES), fclass))
    if not isinstance(reason, str) or not _REASON_RE.match(reason):
        raise UsageError("failure reason must be lower_snake_case (2..40 characters)")
    known = KNOWN_REASONS.get(reason)
    if known is not None and known != fclass:
        raise Refused("reason %r is a %s failure; it cannot be recorded as %s (a failure cannot be relabelled to dodge a cap)"
                      % (reason, known, fclass))


def backoff_s(base: int, attempt_n: int, seed: str, cap: int = 900) -> int:
    """Exponential backoff with deterministic jitter in [0.5, 1.0] of the step, so a test can predict it and a fleet
    of drivers still spreads out (the seed is the item and attempt)."""
    step = min(cap, max(1, base) * (2 ** max(0, attempt_n - 1)))
    h = int(hashlib.sha256(("%s:%d" % (seed, attempt_n)).encode()).hexdigest()[:8], 16) / float(0xFFFFFFFF)
    return max(1, int(step * (0.5 + 0.5 * h)))


def classify_exit(text: str = "", exit_code: Optional[int] = None, signal_name: Optional[str] = None,
                  http_status: Optional[int] = None, timed_out: bool = False) -> Dict[str, str]:
    """A helper for the driver: map what a child session showed to (class, reason). Unknown shapes are `item`
    failures only when there is a clear test or review signal; otherwise a crash is `platform` (the CLI is not the
    item) and a timeout is `transient`."""
    low = (text or "").lower()
    if http_status is not None and 500 <= http_status <= 599 or "overloaded_error" in low or "overloaded" in low and "529" in low:
        return {"failure_class": PLATFORM, "reason": "overloaded" if "529" in low or "overloaded" in low else "api_5xx"}
    if http_status == 429 or "rate limit" in low or "rate_limit" in low:
        return {"failure_class": PLATFORM, "reason": "rate_limit"}
    if timed_out:
        return {"failure_class": TRANSIENT, "reason": "stall"}
    if signal_name or (exit_code is not None and exit_code not in (0, 1)):
        return {"failure_class": PLATFORM, "reason": "cli_crash"}
    if "address already in use" in low or "eaddrinuse" in low:
        return {"failure_class": TRANSIENT, "reason": "port_in_use"}
    if "command not found" in low or "no such file or directory" in low and "node" in low:
        return {"failure_class": ENV, "reason": "toolchain_mismatch"}
    return {"failure_class": ITEM, "reason": "tests_failed"}


def _int(cfg: Any, name: str) -> int:
    v = cfg.get(name)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def decide(item: Dict[str, Any], status: str, cfg: Any, fclass: str, reason: str,
           failed_acs: Sequence[str] = (), stage: Optional[str] = None, seed: str = "") -> Decision:
    """The decision for one failure of `item` (a state item dict) currently in `status`."""
    check_reason(fclass, reason)
    counters = dict(item.get("counters") or {})
    for c in COUNTERS + ("platform", "discards"):
        counters.setdefault(c, 0)
    d = Decision("retry", fclass, reason)
    stage_name = stage or status
    counter = STAGE_COUNTER.get(stage_name, "verify")
    d.counter = counter
    max_retries = _int(cfg, "max_retries_per_item")

    if fclass == PLATFORM:
        counters["platform"] += 1
        d.counters = counters
        if counters["platform"] > _int(cfg, "max_platform_retries"):
            d.action = "park"
            d.status_kind = "needs_human"
            d.block_reason = "platform_outage"
            d.note = "%d platform failures in a row: an outage, not a bug in the item" % counters["platform"]
            return d
        d.backoff_s = backoff_s(_int(cfg, "platform_backoff_base_s") or 30, counters["platform"], seed or str(item.get("attempt")))
        d.note = "retry %d after %ds; no retry budget used" % (counters["platform"], d.backoff_s)
        return d

    if fclass == ENV:
        d.action = "park"
        d.status_kind = "blocked"
        d.block_reason = "env:%s" % reason
        d.demote = True
        d.counters = counters
        d.note = "environment problem: parked; fix it (loopctl doctor) and unblock; the effective level is capped"
        return d

    # transient and item failures use retry budget
    escalate = False
    if fclass == TRANSIENT:
        counters["transient"] += 1
        if counters["transient"] > _int(cfg, "max_transient_retries"):
            escalate = True
            d.note = "transient failure repeated %d times: treated as an item failure" % counters["transient"]
    counters[counter] = counters.get(counter, 0) + 1
    d.consumes = True
    d.counters = counters

    prev = item.get("lastFailure") if isinstance(item.get("lastFailure"), dict) else {}
    prev_acs = set(prev.get("failedAcs") or [])
    cur_acs = set(failed_acs or [])
    if (fclass == ITEM or escalate) and cur_acs and cur_acs == prev_acs and prev.get("class") in (ITEM, TRANSIENT):
        d.no_progress = True
    exhausted = counters[counter] > max_retries or d.no_progress or reason == "reward_hack" or escalate

    if counter == "design":
        if counters["design"] > _int(cfg, "max_design_rounds"):
            return _hard_stop(d, "needs_human", "design_cap", "the design critic is still blocking after max_design_rounds", breaker=False)
        d.to_status = RETRY_STATUS.get(status, "designing")
        return d
    if counter == "ci_fix":
        if counters["ci_fix"] > _int(cfg, "max_ci_fix_attempts"):
            return _hard_stop(d, "needs_human", "ci_fix_cap", "CI is still red after max_ci_fix_attempts", breaker=False)
        d.to_status = RETRY_STATUS.get(status, "building")
        return d

    if not exhausted:
        d.to_status = RETRY_STATUS.get(status, status)
        if fclass == TRANSIENT:
            d.backoff_s = backoff_s(5, counters["transient"], seed or str(item.get("attempt")), cap=60)
        return d

    if counters["discards"] >= 1:
        why = "no progress: the same acceptance criteria failed twice" if d.no_progress else \
            ("a reward-hack signature after a discard" if reason == "reward_hack" else "retry cap exhausted after a fresh attempt")
        return _hard_stop(d, "blocked", "retries_exhausted", why + "; a second discard is a hard stop", breaker=True)
    d.action = "discard"
    d.new_attempt = True
    d.to_status = "pending"
    d.note = ("no progress (same failing acceptance criteria twice): " if d.no_progress else
              ("reward-hack signature: " if reason == "reward_hack" else "retry cap exhausted: ")) + "discard and start a fresh attempt"
    return d


def _hard_stop(d: Decision, kind: str, code: str, note: str, breaker: bool) -> Decision:
    d.action = "hard_stop"
    d.status_kind = kind
    d.block_reason = code
    d.breaker = breaker
    d.note = note
    d.to_status = kind
    return d
