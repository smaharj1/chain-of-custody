"""Tests for .claude/scripts/pr_prepare.py (Phase 3, IMP-25, report §16.1 Phase 3, §19.7).

`FeatureCase` builds a temp repo with `main` (config, template, gitignore) and a feature branch `feat/x` with one WIP commit,
an approved engineer report and a passing saved verify result. `good_body` renders a complete audit block for it.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Mapping, Optional

from kitlib import miniyaml

from .. import REPO_ROOT
from ..helpers import BaseTestCase, TempRepo

GOOD_REPORT = """# Backend Report — x

## Status
APPROVED

## Summary
Added the endpoint.

## Files Changed
- `src/app.py` — created — the endpoint

## Tests Added (or N/A)
- N/A

## API Contract Status
AS_SPECIFIED — matches

## Iteration
1

## Open Questions / Brief Gaps
- none

## Primer Delta
NONE
checked: layering

## Doc Delta
NONE
checked: README, env vars

## Verification
- python3 -m unittest: PASS
"""

CONFIG_DEFAULT = {"delivery": "draft-pr", "git_autonomy": "branch+commit"}


def config_text(values: Mapping[str, Any]) -> str:
    lines = []  # type: List[str]
    for k, v in values.items():
        if isinstance(v, list):
            lines.append("%s: [%s]" % (k, ", ".join(miniyaml.dump_scalar(x) for x in v)))
        else:
            lines.append("%s: %s" % (k, miniyaml.dump_scalar(v)))
    return "# Loop Config\n\n```yaml\n%s\n```\n" % "\n".join(lines)


def good_body(head: str, base_sha: str, *, lane: str = "full", delivery: str = "draft-pr", human: str = "NONE", cifix: str = "NONE",
              title: str = "Add the x endpoint", engineers: Optional[List[str]] = None, doc: str = "NONE\nchecked: README, env vars",
              primer: str = "NONE\nchecked: layering", verify: str = "PASS (verify.v1; base abc; stages: tests)") -> str:
    rows = "\n".join("| %s | APPROVED | 1 | CODE_REVIEW_APPROVED | docs/features/x/reports/%s.md | abc1234 |" % (e, e)
                     for e in (engineers if engineers is not None else ["backend"]))
    return """<!-- kit:pr-body v1 -->
# %(title)s

## Summary

Adds the x endpoint so clients can list x.

## Audit block

<!-- audit:begin v1 -->
| Field | Value |
|---|---|
| Feature | x |
| Lane | %(lane)s |
| Delivery | %(delivery)s |
| Branch | feat/x |
| Base | main |
| Base sha | %(base)s |
| Head sha | %(head)s |
| Design critic | %(critic)s |
| Verify (G6) | %(verify)s |
| Security review | N/A (nothing sensitive) |
| Human-gated paths | %(human)s |
| CI-Fix-Of | %(cifix)s |
| Waivers | NONE |
| CI evidence | PENDING: the CI check runs on this pull request are the evidence |
<!-- audit:end -->

## Engineers

| Engineer | Status | Iterations | Code review | Report | Commits |
|---|---|---|---|---|---|
%(rows)s

## Acceptance matrix

| Criterion | Evidence | Result |
|---|---|---|
| GET /x returns 200 | tests.test_x::test_list | PASS |

## Doc Delta

%(doc)s

## Primer Delta

%(primer)s

## Notes

NONE
""" % {"title": title, "lane": lane, "delivery": delivery, "base": base_sha, "head": head, "human": human, "cifix": cifix,
       "rows": rows, "doc": doc, "primer": primer, "verify": verify,
       "critic": "DESIGN_APPROVED (round 1)" if lane == "full" else "N/A (quick lane)"}


_CACHE = {}  # type: Dict[str, Any]


class CopiedRepo(TempRepo):
    """A TempRepo that is a copy of an already built one (a plain directory copy is far cheaper than a dozen git processes)."""

    def __init__(self, source: TempRepo) -> None:
        self._owns_parent = True
        self.parent = os.path.realpath(tempfile.mkdtemp(prefix="kit-repo-"))
        self.path = os.path.join(self.parent, "repo")
        shutil.copytree(source.path, self.path, symlinks=True)
        self.env = source.env


class FeatureCase(BaseTestCase):
    def make_feature(self, config: Optional[Mapping[str, Any]] = None, report: Optional[str] = GOOD_REPORT, reports: Optional[List[str]] = None,
                     files: Optional[Dict[str, str]] = None, base_files: Optional[Dict[str, str]] = None) -> TempRepo:
        """A feature repo. The default one is built once per process and copied."""
        if config is None and report == GOOD_REPORT and reports is None and files is None and base_files is None:
            if "default" not in _CACHE:
                src = self._build_feature(None, GOOD_REPORT, None, None, None, track=False)
                atexit.register(src.cleanup)
                _CACHE["default"] = (src, self.base)
            src, self.base = _CACHE["default"]
            r = CopiedRepo(src)
            self._repos.append(r)
            return r
        return self._build_feature(config, report, reports, files, base_files)

    def _build_feature(self, config: Optional[Mapping[str, Any]] = None, report: Optional[str] = GOOD_REPORT, reports: Optional[List[str]] = None,
                     files: Optional[Dict[str, str]] = None, base_files: Optional[Dict[str, str]] = None, track: bool = True) -> TempRepo:
        r = self.repo() if track else TempRepo()
        cfg = dict(CONFIG_DEFAULT)
        cfg.update(config or {})
        r.write(".claude/loop.config.md", config_text(cfg))
        r.write(".gitignore", ".claude/pr/\n")
        os.makedirs(os.path.join(r.path, ".claude", "templates"), exist_ok=True)
        shutil.copyfile(os.path.join(REPO_ROOT, ".claude", "templates", "pr-body.md"), os.path.join(r.path, ".claude", "templates", "pr-body.md"))
        r.write("README.md", "# x\n")
        for rel, text in (base_files or {}).items():
            r.write(rel, text)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "base")
        self.base = r.git("rev-parse", "HEAD")
        r.git("switch", "-q", "-c", "feat/x")
        r.write("src/app.py", "def handler():\n    return 200\n")
        names = reports if reports is not None else (["backend"] if report else [])
        for name in names:
            r.write("docs/features/x/reports/%s.md" % name, report or GOOD_REPORT)
        for rel, text in (files or {}).items():
            r.write(rel, text)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "wip(x): backend iteration 1\n\nFeature: x\nEngineer: backend\nReport: docs/features/x/reports/backend.md\n")
        return r

    def commit(self, r: TempRepo, files: Dict[str, str], message: str) -> str:
        for rel, text in files.items():
            r.write(rel, text)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", message)
        return r.head()

    def write_body(self, r: TempRepo, text: str, slug: str = "x") -> str:
        path = os.path.join(r.path, ".claude", "pr", slug + ".md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def write_verify(self, r: TempRepo, **over: Any) -> str:
        data = {"schema": "verify.v1", "status": "pass", "ok": True, "complete": True, "trusted": True, "dirty": False,
                "head": r.head(), "base": self.base, "stages": [{"name": "tests", "status": "pass"}]}
        data.update(over)
        path = os.path.join(r.path, ".claude", "pr", "x.verify.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def ready(self, r: TempRepo, **body_kw: Any) -> None:
        """Write a complete body and a passing verify result for the repo's current HEAD."""
        self.write_body(r, good_body(r.head(), self.base, **body_kw))
        self.write_verify(r)
