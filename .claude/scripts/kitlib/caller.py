"""Who is calling a hook? (report §5.7, CS-7, CS-25; IMP-7 V1).

A pure function over the hook-input dict and the session registry. It never touches
the filesystem or git, so the corpus can drive it directly.

Rules (§5.7), in order:
  1. Malformed input (not a dict, no valid session_id)              -> `unknown`
  2. `agent_id` present: a subagent or teammate; the role is `agent_type`
       * agent_type is a string on the roster                       -> `engineer` or `critic`
       * agent_type is a string NOT on the roster                   -> `engineer`
         (IMP-7 V1: the build is agent-blind, so an unknown subagent gets the engineer
         restrictions)
       * agent_type missing or not a string                         -> `unknown-subagent`
         (callers should treat it as the most restricted caller)
  3. no `agent_id`, `session_id` registered:
       * record role "conductor"                                    -> `conductor`
       * record role on the roster                                  -> that role's kind
         (a driver child started with `--agent <role>`)
       * any other role                                             -> `unknown`
  4. no `agent_id`, `session_id` not registered                     -> `unknown`
     (least privilege, whatever `agent_type` says: a human's `--agent` session lands here)

`agent_type` is normalised first: a `<plugin>:` prefix is stripped and the name is
matched against the roster (CS-39).
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .runstate import valid_id

__all__ = ["CONDUCTOR", "ENGINEER", "CRITIC", "UNKNOWN_SUBAGENT", "UNKNOWN", "KINDS",
           "DEFAULT_ROSTER", "Caller", "normalize_agent_type", "classify"]

CONDUCTOR = "conductor"
ENGINEER = "engineer"
CRITIC = "critic"
UNKNOWN_SUBAGENT = "unknown-subagent"
UNKNOWN = "unknown"
KINDS = (CONDUCTOR, ENGINEER, CRITIC, UNKNOWN_SUBAGENT, UNKNOWN)

# role name -> kind. Projects with extra engineers pass a bigger roster.
DEFAULT_ROSTER = {
    "database-engineer": ENGINEER,
    "backend-engineer": ENGINEER,
    "app-engineer": ENGINEER,
    "admin-app-engineer": ENGINEER,
    "infra-engineer": ENGINEER,
    "code-reviewer": CRITIC,
    "design-critic": CRITIC,
    "security-reviewer": CRITIC,
}  # type: Dict[str, str]


class Caller(object):
    __slots__ = ("kind", "role", "agent_id", "session_id", "reason")

    def __init__(self, kind: str, role: Optional[str], agent_id: Optional[str],
                 session_id: Optional[str], reason: str) -> None:
        self.kind = kind
        self.role = role
        self.agent_id = agent_id
        self.session_id = session_id
        self.reason = reason

    @property
    def is_conductor(self) -> bool:
        return self.kind == CONDUCTOR

    @property
    def restricted(self) -> bool:
        """True for callers that get least privilege (unknown sessions, malformed subagents)."""
        return self.kind in (UNKNOWN, UNKNOWN_SUBAGENT)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "role": self.role, "agent_id": self.agent_id,
                "session_id": self.session_id, "reason": self.reason}

    def __repr__(self) -> str:
        return "Caller(%s, role=%r, %s)" % (self.kind, self.role, self.reason)


def normalize_agent_type(agent_type: Any) -> Optional[str]:
    """Strip a `<plugin>:` prefix. None if not a usable string."""
    if not isinstance(agent_type, str):
        return None
    name = agent_type.strip()
    if ":" in name:
        name = name.rsplit(":", 1)[1].strip()
    return name or None


def _role_kind(role: Optional[str], roster: Mapping[str, str]) -> Optional[str]:
    if role is None:
        return None
    kind = roster.get(role)
    return kind if kind in (ENGINEER, CRITIC) else None


def classify(hook_input: Any, registry: Mapping[str, Mapping[str, Any]],
             roster: Optional[Mapping[str, str]] = None) -> Caller:
    """Classify the caller of a hook. `registry` maps session_id -> registry record."""
    ros = DEFAULT_ROSTER if roster is None else roster
    if not isinstance(hook_input, Mapping):
        return Caller(UNKNOWN, None, None, None, "hook input is not an object")
    sid = hook_input.get("session_id")
    if not valid_id(sid):
        return Caller(UNKNOWN, None, None, None, "missing or invalid session_id")
    agent_id = hook_input.get("agent_id")
    role = normalize_agent_type(hook_input.get("agent_type"))

    if agent_id is not None and agent_id != "":
        if not isinstance(agent_id, str):
            return Caller(UNKNOWN_SUBAGENT, role, None, sid, "agent_id is not a string")
        if role is None:
            return Caller(UNKNOWN_SUBAGENT, None, agent_id, sid, "subagent without a usable agent_type")
        kind = _role_kind(role, ros)
        if kind is not None:
            return Caller(kind, role, agent_id, sid, "subagent on the roster")
        return Caller(ENGINEER, role, agent_id, sid,
                      "unknown agent_type %r treated as engineer (IMP-7 V1, agent-blind)" % role)

    rec = registry.get(sid) if isinstance(registry, Mapping) else None
    if rec is None:
        return Caller(UNKNOWN, role, None, sid, "session is not registered: least privilege")
    reg_role = normalize_agent_type(rec.get("role"))
    if reg_role == CONDUCTOR:
        return Caller(CONDUCTOR, CONDUCTOR, None, sid, "registered conductor session")
    kind = _role_kind(reg_role, ros)
    if kind is not None:
        return Caller(kind, reg_role, None, sid, "registered driver child with role %s" % reg_role)
    return Caller(UNKNOWN, reg_role, None, sid, "registered session with unknown role %r" % (rec.get("role"),))
