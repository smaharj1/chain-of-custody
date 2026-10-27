"""Every changelog.d fragment has the seven sections the definition of done requires (IMP-24)."""
from __future__ import annotations

import glob
import os
import re
import unittest

from . import REPO_ROOT

HEADINGS = ["Changes", "Upgrade impact", "New config keys", "Pending-human actions", "Docs impact",
            "Decisions made", "Conductor: pointers to add"]


def fragments():
    return sorted(p for p in glob.glob(os.path.join(REPO_ROOT, "changelog.d", "*.md"))
                  if os.path.basename(p) != "README.md")


def headings(text):
    in_fence = False
    out = []
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            m = re.match(r"^## (.+?)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


class FragmentFormat(unittest.TestCase):
    def test_readme_exists_and_documents_every_heading(self):
        with open(os.path.join(REPO_ROOT, "changelog.d", "README.md")) as fh:
            text = fh.read()
        for h in HEADINGS:
            self.assertIn("## " + h, text)

    def test_fragments_have_the_seven_sections_in_order_and_are_not_empty(self):
        for path in fragments():
            with self.subTest(os.path.basename(path)):
                with open(path) as fh:
                    text = fh.read()
                self.assertTrue(text.startswith("# "), "first line must be '# <topic>: <summary>'")
                self.assertEqual(headings(text), HEADINGS)
                parts = re.split(r"^## .+$", text, flags=re.M)[1:]
                for h, body in zip(HEADINGS, parts):
                    self.assertTrue(body.strip(), "section %r is empty (write 'None.')" % h)

    def test_own_fragment_present(self):
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "changelog.d", "00-foundations.md")))

    def test_checker_catches_a_broken_fragment(self):
        self.assertNotEqual(headings("# t\n\n## Changes\nx\n"), HEADINGS)
        self.assertEqual(headings("# t\n```\n## Changes\n```\n"), [])


if __name__ == "__main__":
    unittest.main()
