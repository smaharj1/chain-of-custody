"""Every shipped workflow template passes its own lint (D9).

The infra-strictness rules (ci_lint.py, infra/ci_rules.py) are errors. A template the kit ships that tripped one would teach people to ignore the
lint, so this lints every file under `templates/infra/workflows/` and `templates/github/workflows/`: zero errors, nothing unparsed, and the
warning set pinned exactly. The one warning the templates carry is a floating action tag; each such line says `# pin to SHA`.
"""
from __future__ import annotations

import os
import unittest

import ci_lint

from tests import REPO_ROOT

TEMPLATE_DIRS = [os.path.join(REPO_ROOT, ".claude", "templates", "infra", "workflows"),
                 os.path.join(REPO_ROOT, ".claude", "templates", "github", "workflows")]

# file -> the rules that may warn there. Anything else, or any error, is a failure.
EXPECTED_WARNINGS = {
    "infra-apply.dispatch.yml": ["ci.floating-action-tag"],
    "infra-apply.environment.yml": ["ci.floating-action-tag"],
    "infra-check.yml": ["ci.floating-action-tag"],
    "infra-drift.yml": ["ci.floating-action-tag"],
    "infra-plan.yml": ["ci.floating-action-tag"],
    "claude-review.yml": ["ci.floating-action-tag"],
    "dependency-check.yml": ["ci.floating-action-tag"],
    "g10-product.yml": ["ci.floating-action-tag"],
    "heldout-sync.yml": ["ci.floating-action-tag"],
    "heldout.yml": ["ci.floating-action-tag"],
    "intake.yml": ["ci.floating-action-tag"],
    "label-guard.yml": [],
    "mutation.yml": ["ci.floating-action-tag"],
    "smoke.yml": ["ci.floating-action-tag"],
    "verify.yml": ["ci.floating-action-tag"],
}


def templates():
    out = {}
    for d in TEMPLATE_DIRS:
        for n in sorted(os.listdir(d)):
            if n.endswith((".yml", ".yaml")):
                out[n] = os.path.join(d, n)
    return out


class TemplatesLintClean(unittest.TestCase):
    def test_the_pinned_table_names_every_shipped_template(self):
        self.assertEqual(sorted(templates()), sorted(EXPECTED_WARNINGS), "a template was added or removed: update EXPECTED_WARNINGS and lint it")

    def test_zero_errors_nothing_unparsed_and_the_exact_warning_set(self):
        for name, path in templates().items():
            with self.subTest(name):
                findings = ci_lint.lint_paths([path], None, REPO_ROOT)
                self.assertEqual([str(f) for f in findings if f.severity == "error"], [])
                self.assertNotIn("ci.unparsed", [f.rule for f in findings])
                self.assertEqual(sorted({f.rule for f in findings if f.severity == "warn"}), EXPECTED_WARNINGS[name])

    def test_the_directories_lint_clean_together(self):
        findings = ci_lint.lint_paths(TEMPLATE_DIRS, None, REPO_ROOT)
        self.assertEqual([str(f) for f in findings if f.severity == "error"], [])


if __name__ == "__main__":
    unittest.main()
