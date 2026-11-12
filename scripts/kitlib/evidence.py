"""The evidence gate as a human-committed file (IMP-1, report §6.2 L4, §16).

No agent may run `claude`, so "3 clean fixture runs plus 1 real feature" cannot be
proved by this build. The maintainer commits `docs/baseline/evidence.json` when the
gate is genuinely met. Until then `passed(repo)` is False and these features refuse
to enable: unattended L4, L5, `parallel_mode: worktree`, G4 mutation, the G5 AST
lint, remote lock, G10, `pipeline_host: workflow`, the G8 verifier. The doctor caps
the effective level at L3 (IMP-21).

File format (`schema` is `evidence.v1`; unknown extra fields are allowed):

    {
      "schema": "evidence.v1",
      "fixture_runs": [ {"id": "run-1", "clean": true, ...}, ... at least 3 ... ],
      "real_feature": {"id": "...", "completed": true, ...},
      "attested_by": "<name>",
      "attested_at": "<date>"
    }

Any string value that still looks like a template placeholder (`<...>`, `TODO`,
`CHANGEME`) makes the file incomplete.
"""
from __future__ import annotations

import os
import re
from typing import Any, List, Optional

from . import jsonio

__all__ = ["EVIDENCE_PATH", "SCHEMA", "MIN_FIXTURE_RUNS", "EvidenceStatus", "check", "passed"]

EVIDENCE_PATH = os.path.join("docs", "baseline", "evidence.json")
SCHEMA = "evidence.v1"
MIN_FIXTURE_RUNS = 3

_PLACEHOLDER_RE = re.compile(r"^\s*(?:<[^<>]*>|TODO.*|CHANGEME.*|TBD)\s*$", re.I)


class EvidenceStatus(object):
    """Truthy iff the gate passed; `reason` says why not (empty when it passed)."""

    def __init__(self, ok: bool, reason: str = "", path: Optional[str] = None, data: Any = None) -> None:
        self.ok = ok
        self.reason = reason
        self.path = path
        self.data = data

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return "EvidenceStatus(ok=%r, reason=%r)" % (self.ok, self.reason)


def _placeholders(obj: Any, trail: str = "") -> List[str]:
    found = []  # type: List[str]
    if isinstance(obj, str):
        if _PLACEHOLDER_RE.match(obj):
            found.append(trail or "<root>")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found += _placeholders(v, trail + "." + str(k) if trail else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _placeholders(v, "%s[%d]" % (trail, i))
    return found


def check(repo: str) -> EvidenceStatus:
    path = os.path.join(repo, EVIDENCE_PATH)
    if not os.path.isfile(path):
        return EvidenceStatus(False, "%s is absent (the evidence gate is a human-committed file)" % EVIDENCE_PATH, path)
    try:
        data = jsonio.read_json(path)
    except (ValueError, OSError) as exc:
        return EvidenceStatus(False, "%s is not valid JSON: %s" % (EVIDENCE_PATH, exc), path)
    if not isinstance(data, dict):
        return EvidenceStatus(False, "%s must be a JSON object" % EVIDENCE_PATH, path)
    if data.get("schema") != SCHEMA:
        return EvidenceStatus(False, "schema must be %r, got %r" % (SCHEMA, data.get("schema")), path)
    ph = _placeholders(data)
    if ph:
        return EvidenceStatus(False, "template placeholders remain in: " + ", ".join(ph[:5]), path)
    runs = data.get("fixture_runs")
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        return EvidenceStatus(False, "fixture_runs must be a list of objects", path)
    clean = [r for r in runs if r.get("clean") is True and isinstance(r.get("id"), str) and r.get("id")]
    if len(clean) < MIN_FIXTURE_RUNS:
        return EvidenceStatus(False, "need %d clean fixture runs, found %d" % (MIN_FIXTURE_RUNS, len(clean)), path)
    ids = [r["id"] for r in clean]
    if len(set(ids)) != len(ids):
        return EvidenceStatus(False, "fixture run ids must be unique", path)
    feat = data.get("real_feature")
    if not isinstance(feat, dict) or feat.get("completed") is not True or not feat.get("id"):
        return EvidenceStatus(False, "real_feature must be an object with an id and completed: true", path)
    who = data.get("attested_by")
    if not isinstance(who, str) or not who.strip():
        return EvidenceStatus(False, "attested_by must name the human who attests the evidence", path)
    return EvidenceStatus(True, "", path, data)


def passed(repo: str) -> bool:
    """True only when `docs/baseline/evidence.json` exists, parses, and is complete."""
    return bool(check(repo))
