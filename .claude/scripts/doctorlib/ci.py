"""CI lint results as doctor checks (L3 needs split CI, report §8.4, DEC-39). The rules live in `ci_lint.py`."""
from __future__ import annotations

import os
import sys
from typing import List

from .model import Context, Result, check, failed, passed, skipped, ERROR, WARN


@check("ci.lint", "ci", emits=("ci.",))
def check_ci_lint(ctx: Context) -> List[Result]:
    wdir = ctx.repo_path(".github", "workflows")
    if not os.path.isdir(wdir):
        return [skipped("ci.lint", "no .github/workflows directory")]
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import ci_lint  # lazy: ci_lint imports doctorlib.workflows
    findings = ci_lint.lint_repo(ctx.repo)
    out = []  # type: List[Result]
    for f in findings:
        out.append(failed(f.rule, "%s%s: %s" % (f.file, " [job %s]" % f.job if f.job else "", f.message),
                          "see docs/security/split-ci.md", severity=ERROR if f.severity == "error" else WARN))
    if not any(f.severity == "error" for f in findings):
        out.append(passed("ci.lint", "%d workflow file(s) pass the split-CI rules" % len(ci_lint.workflow_files(wdir))))
    return out
