"""No real-project names anywhere in the repo (IMP-19).

Every tracked text file is tokenised (`[A-Za-z0-9_-]+`, lower-cased, plus every path
segment) and each token's SHA-256 is compared with `tests/data/leak-denylist.sha256`.
The denylist stores only hashes, so it does not leak the names it guards. A hit is reported
as file:line with the hash prefix, never the token itself.

Variants of each token (underscores and hyphens swapped, or removed) are checked too.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unittest

from . import REPO_ROOT

DENYLIST = os.path.join(REPO_ROOT, "tests", "data", "leak-denylist.sha256")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
MAX_BYTES = 2 * 1024 * 1024


def load_denylist(path=DENYLIST):
    hashes = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip().lower()
            if line:
                if not re.match(r"^[0-9a-f]{64}$", line):
                    raise ValueError("denylist line is not a sha256 hex digest: %r" % line[:20])
                hashes.add(line)
    return hashes


def tracked_files(repo=REPO_ROOT):
    out = subprocess.run(["git", "-C", repo, "ls-files", "-z"], stdout=subprocess.PIPE, check=True).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def variants(token):
    yield token
    if "_" in token or "-" in token:
        yield token.replace("_", "-")
        yield token.replace("-", "_")
        yield token.replace("-", "").replace("_", "")


def digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hits_in_tokens(tokens, denied):
    out = []
    for tok in tokens:
        for v in variants(tok):
            if digest(v) in denied:
                out.append(digest(v))
                break
    return out


def scan_text(text, denied):
    """[(line number, hash)] for every denied token in `text`."""
    found = []
    for n, line in enumerate(text.split("\n"), 1):
        for tok in TOKEN_RE.findall(line.lower()):
            for v in variants(tok):
                h = digest(v)
                if h in denied:
                    found.append((n, h))
                    break
    return found


def scan_path(rel, denied):
    low = rel.lower()
    toks = set(TOKEN_RE.findall(low)) | set(low.split("/"))
    return hits_in_tokens(sorted(toks), denied)


def is_text(data):
    return b"\0" not in data[:8000]


class NoProjectNamesTest(unittest.TestCase):
    def test_denylist_present_and_well_formed(self):
        denied = load_denylist()
        self.assertGreaterEqual(len(denied), 1)

    def test_no_denied_token_in_any_tracked_file(self):
        denied = load_denylist()
        problems = []
        for rel in tracked_files():
            for h in scan_path(rel, denied):
                problems.append("%s: path contains a denied name (hash %s...)" % (rel, h[:10]))
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) > MAX_BYTES:
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            if not is_text(data):
                continue
            for n, h in scan_text(data.decode("utf-8", "replace"), denied):
                problems.append("%s:%d: contains a denied name (hash %s...)" % (rel, n, h[:10]))
        self.assertEqual(problems, [], "real-project names found (use 'Install A'/'Install B' or invented names):\n"
                         + "\n".join(problems))

    def test_scanner_detects_a_planted_token(self):
        planted = "zz-planted-name"
        denied = {digest(planted)}
        self.assertEqual([n for n, _ in scan_text("ok\nsee ZZ-Planted-Name here\n", denied)], [2])
        self.assertEqual([n for n, _ in scan_text("see zz_planted_name\n", denied)], [1])
        self.assertTrue(scan_path("docs/ZZ-planted-name/readme.md", denied))
        self.assertEqual(scan_text("nothing here\n", denied), [])


if __name__ == "__main__":
    unittest.main()
