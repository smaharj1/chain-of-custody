"""DEC-1: the doctor suggests `delivery: draft-pr` once the ruleset checks pass.

    delivery.suggest-draft-pr   `delivery` is still `local` and the checks that prove a pull request cannot be bypassed all pass (main requires a
                                pull request and a review, code-owner review is on, the required checks are pinned, nobody bypasses). An
                                information row only: it changes nothing, and the default stays `local`. It is never a failure.

Registered by `levelcalc` importing this module (a check module needs no edit to `doctor.py`). Nothing here pushes or opens a pull request:
`draft-pr` makes `pr_prepare.py` print the commands, and the human runs them.
"""
from __future__ import annotations

from typing import List

from . import github
from .model import Context, Result, check, passed, skipped, FAIL, UNKNOWN


@check("delivery.suggest-draft-pr", "config", doc="suggests delivery: draft-pr once the ruleset checks pass (DEC-1); information only")
def check_suggest_draft_pr(ctx: Context) -> Result:
    if ctx.config is None:
        return skipped("delivery.suggest-draft-pr", "no loop.config")
    current = str(ctx.cfg("delivery", "local") or "local")
    if current != "local":
        return skipped("delivery.suggest-draft-pr", "delivery is %s: nothing to suggest" % current)
    rows = []  # type: List[Result]
    for fn in (github.check_main_protection, github.check_codeowner_review, github.check_required_checks, github.check_bypass):
        res = fn(ctx)
        rows.extend(res if isinstance(res, list) else [res])
    bad = [r for r in rows if r.status in (FAIL, UNKNOWN)]
    if bad or not rows:
        why = "%s: %s" % (bad[0].id, bad[0].message) if bad else "no ruleset data"
        return skipped("delivery.suggest-draft-pr", "delivery stays local: the ruleset checks do not all pass yet (%s)" % why)
    return passed("delivery.suggest-draft-pr",
                  "the ruleset checks pass (pull request and review required, code owners, pinned required checks, no bypass): you can set delivery: draft-pr "
                  "in loop.config. The conductor then prepares a branch and a draft pull request for you to push and open yourself; it never pushes. "
                  "The default stays local.")
