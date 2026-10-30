"""ConfigChange blocker (report §7.1, §5.8, CS-13; RC-30): exit 2 on a change to project or local settings during a run.

Policy settings cannot be blocked by a hook [docs] (P32), so they are ignored. A run is active while
`<git-common-dir>/loopctl/lock` or `run.json` exists. Outside a run a human editing settings is normal and is
not blocked; an agent editing settings is blocked by guard-file wherever it happens.
"""
from __future__ import annotations

import os
from typing import Optional

from .lib import Ctx, Decision, deny

G = "guard-config-change"


def run_active(ctx: Ctx) -> bool:
    st = ctx.state
    if st is None:
        return False
    return st.lock_held() or os.path.lexists(st.run_path)


def check(ctx: Ctx) -> Optional[Decision]:
    src = str(ctx.hook.get("source") or ctx.hook.get("config_source") or "").lower()
    if "policy" in src or "managed" in src:
        return None
    if not run_active(ctx):
        return None
    what = src or "settings"
    return deny(G, "settings-during-run", "a change to %s (%s) arrived while a loop run is active; settings are an H6 guardrail and are changed between runs, "
                "by a human (report §7.1, CS-13)" % (what, str(ctx.hook.get("file_path", ""))[:80]))
