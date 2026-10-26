"""Create or delete a throwaway copy of the dev database for one item's verification.

    python3 scripts/ephemeral_db.py create <ITEM_ID>   prints the DATABASE_URL to export
    python3 scripts/ephemeral_db.py delete <ITEM_ID>

The copy is made from .data/dev.db when it exists, otherwise from an empty file. It lives
in the system temp directory, so a failed item's schema changes are discarded for free.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_DB = os.path.join(ROOT, ".data", "dev.db")


def target(item_id: str) -> str:
    if not re.match(r"^[A-Za-z0-9_-]+$", item_id):
        raise SystemExit("bad item id: %r" % item_id)
    return os.path.join(tempfile.gettempdir(), "hello-loop-%s.db" % item_id)


def main(argv) -> int:
    if len(argv) != 2 or argv[0] not in ("create", "delete"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = target(argv[1])
    if argv[0] == "create":
        if os.path.isfile(DEV_DB):
            shutil.copyfile(DEV_DB, path)
        else:
            open(path, "wb").close()
        print("file:" + path)
    elif os.path.exists(path):
        os.unlink(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
