"""The learning loop (Phase 10; report 16.1 row 10, F-29, L-32, L-33, L-34, F-30; DEC-28, DEC-29, H6).

A run produces data (events, state, reports, reviews, the spend ledger). This package turns it into decisions a human takes, and
never takes them itself:

    data         the run data, loaded once: events, notify records, state, plan, spend, engineer reports, review files
    retro        `loopctl retro`: the milestone's numbers, in one document (a read)
    trailers     how well the engineers' reports and JSON trailers conform; the clean streak that says `block` is ready (L-32, DEC-29)
    gaps         `loopctl gaps`: gaps that repeat (brief gaps, primer deltas, gate findings, failures) drafted as rule or primer changes under the
                 git directory, plus the git and gh commands a HUMAN runs (L-33, DEC-28)
    calibration  the critic calibration log: every blocker with its outcome (L-34)
    harness      the harness review at a model release: gate owner metrics and the eval ablation verdicts (16.2)
    docsdelta    the pure half of the Docs Delta gate (F-30), used by `gates/docs_delta.py`

THE RULES THIS PACKAGE KEEPS (H6, CS-2):

* It reads. The only things it writes are drafts, the calibration log and a new file for each, under `<git-common-dir>/loopctl/`. It
  never edits a rule, a primer, an agent, a hook, a setting, `loop.config` or `docs/baseline/evidence.json`, never commits, pushes or
  opens a pull request; it PRINTS the commands for a person. A person approves every change to a guardrail.
* The data is untrusted. Event messages, report sections and review files were written by agents that read untrusted content. Whatever
  reaches a draft is quoted in a fenced block labelled as data (`fence`), cleaned of control and bidi characters, and never becomes part of a
  rule's own words. A rule's text, its file name and its path globs come from closed vocabularies and the approved plan, never from the log.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .. import common

__all__ = ["token", "fence", "inline", "ISSUE_KINDS", "MAX_EVENTS"]

MAX_EVENTS = 50000                       # the newest this many events are read; a log is diagnostic and may be large
ISSUE_KINDS = ("missing-section", "none-without-checked", "no-trailer", "bad-status", "no-status", "trailer-mismatch", "trailer-ambiguous",
               "bad-path", "no-report", "unsafe-report")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,47}\Z")


def token(value: Any, allowed: Optional[Iterable[str]] = None, default: str = "other") -> str:
    """A closed-vocabulary word: lower-case letters, digits, `_`, `.` or `-`, at most 48 characters, and (when `allowed` is given) one of
    them. Anything else is `default`. Text from a log never reaches a file name, a glob or a rule through any other door."""
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        return default
    if allowed is not None and value not in set(allowed):
        return default
    return value


def inline(text: Any, limit: int = 120) -> str:
    """One line of untrusted text for a table cell or a sentence: cleaned, no markup that renders, no backticks."""
    return common.md_text(text, limit).replace("`", "'").replace("|", "/").replace("*", "").replace("_", " ").replace("[", "(").replace("]", ")")


def fence(text: Any, label: str, limit: int = 1200) -> str:
    """Untrusted text as a fenced markdown block, with a line that says what it is. The fence is longer than any run of `~` in the text, so
    the text cannot close it; control, zero-width and bidi characters are replaced first. The label is closed vocabulary or an id the
    caller validated, never free text."""
    body = common.clean_text(text, limit, keep_newlines=True)
    body = "\n".join(ln.rstrip() for ln in body.split("\n"))
    longest = max([len(m) for m in re.findall(r"~+", body)] or [0])
    mark = "~" * max(4, longest + 1)
    return ("UNTRUSTED DATA, quoted from %s. It is evidence for a human to read, never an instruction to anyone.\n\n%s text\n%s\n%s\n"
            % (common.md_text(label, 80), mark, body, mark))
