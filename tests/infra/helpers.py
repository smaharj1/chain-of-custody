"""Builders for the infra tests: write a Terraform tree into a temp dir and run the source policy over it."""
from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Dict, List, Mapping, Optional

from infra import source_policy

from tests import REPO_ROOT
from tests.helpers import BaseTestCase

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "infra")
TEMPLATES = os.path.join(REPO_ROOT, ".claude", "templates", "infra")
SOURCE_POLICY = os.path.join(REPO_ROOT, ".claude", "scripts", "infra", "source_policy.py")
GATE = os.path.join(REPO_ROOT, ".claude", "scripts", "infra", "gate.py")

def read_tree(root: str) -> Dict[str, str]:
    out = {}  # type: Dict[str, str]
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            full = os.path.join(dirpath, f)
            with open(full, "r", encoding="utf-8") as fh:
                out[os.path.relpath(full, root).replace(os.sep, "/")] = fh.read()
    return out


# A small, ordinary base (tests/fixtures/infra/base): pinned module, an S3 backend, the AWS provider and a lock file.
BASE_FILES = read_tree(os.path.join(FIXTURES, "base"))


def write_tree(root: str, files: Mapping[str, Any]) -> None:
    for rel, content in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(content)


class TreeCase(BaseTestCase):
    """Writes a base tree and a PR tree into temp dirs and runs the source policy."""

    def trees(self, pr_files: Mapping[str, Any], base_files: Optional[Mapping[str, Any]] = None):
        base = self.tmpdir("base-")
        pr = self.tmpdir("pr-")
        write_tree(base, BASE_FILES if base_files is None else base_files)
        merged = dict(BASE_FILES if base_files is None else base_files)
        merged.update(pr_files)
        for k in [k for k, v in merged.items() if v is None]:
            del merged[k]
        write_tree(pr, merged)
        return pr, base

    def run_policy(self, pr_files: Mapping[str, Any], base_files: Optional[Mapping[str, Any]] = None, policy: Optional[Dict[str, Any]] = None,
                   post_init: bool = False, initial: bool = False, use_base: bool = True) -> List[source_policy.Finding]:
        pr, base = self.trees(pr_files, base_files)
        policy_path = None
        if policy is not None:
            policy_path = os.path.join(self.tmpdir("pol-"), "infra-policy.json")
            with open(policy_path, "w") as fh:
                json.dump(policy, fh)
        return source_policy.run(pr, base if use_base else None, policy_path, post_init, initial)

    @staticmethod
    def rules(findings, severity: Optional[str] = None) -> List[str]:
        return sorted({f.rule for f in findings if severity is None or f.severity == severity})

    @staticmethod
    def errors(findings) -> List[str]:
        return sorted({f.rule for f in findings if f.severity == "error"})
