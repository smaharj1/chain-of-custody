"""Ports for parallel workers (report 19.7 "Process groups and ports" L-27, 11.5 item 3 "port offset"; D-22).

Two workers must not start dev servers on the same port. The project declares its verify ports once (`verify_ports`, the first one is where
`verify` polls); a worker gets a SLOT, and slot `n` shifts every declared port by `n * STRIDE`:

    verify_ports: [4100, 4101]      slot 0 -> 4100 4101      slot 1 -> 4200 4201      slot 2 -> 4300 4301

A slot is a lease (`ports-<n>`, mutex.py), so no two workers hold one, and the slot is skipped when anything is listening on any of its ports
(`port_busy`, verify.py's own probe): a stranger's dev server, or the leaked server of a worker that crashed. The worker passes the shift to its
children and to `verify` as `KIT_PORT_OFFSET` (verify.py reads it through the gate; `KIT_PORT_SLOT` and `KIT_PORTS` are there for the project's
own scripts, for example to name an ephemeral database per slot). With no `verify_ports` the slot still exists and still shifts nothing.

When every slot is taken or busy, `allocate` returns None: the scheduler does not start another worker until one ends. It never reuses a busy
port, and it never hands one slot to two workers.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mutex

__all__ = ["STRIDE", "MAX_SLOTS", "Block", "PortAllocator", "block_ports", "port_busy", "offset_from_env"]

STRIDE = 100
MAX_SLOTS = 8


def port_busy(port: int) -> bool:
    """Something holds the port on loopback (verify.py's definition, so the scheduler and the verify stage agree)."""
    import verify
    return bool(verify.port_busy(port))


def block_ports(base: Sequence[int], slot: int, stride: int = STRIDE) -> List[int]:
    """The ports of slot `slot`. Raises ValueError if the shift leaves the port range."""
    out = []  # type: List[int]
    for p in base:
        q = int(p) + slot * stride
        if not 0 < q < 65536:
            raise ValueError("port %d shifted by slot %d is outside 1..65535" % (p, slot))
        out.append(q)
    return out


def offset_from_env(env: Any) -> int:
    """`KIT_PORT_OFFSET` as an int in 0..60000, else 0. Malformed values are ignored, never trusted."""
    raw = env.get("KIT_PORT_OFFSET") if env is not None else None
    if isinstance(raw, str) and re.match(r"^[0-9]{1,5}\Z", raw) and int(raw) <= 60000:       # ASCII digits only: str.isdigit() accepts other scripts' digits
        return int(raw)
    return 0


class Block(object):
    """A worker's ports."""

    def __init__(self, slot: int, ports: List[int], lease: mutex.Lease, stride: int = STRIDE) -> None:
        self.slot, self.ports, self.lease, self.offset = slot, ports, lease, slot * stride

    def env(self) -> Dict[str, str]:
        return {"KIT_PORT_OFFSET": str(self.offset), "KIT_PORT_SLOT": str(self.slot), "KIT_PORTS": ",".join(str(p) for p in self.ports)}

    def to_dict(self) -> Dict[str, Any]:
        return {"slot": self.slot, "offset": self.offset, "ports": list(self.ports)}


class PortAllocator(object):
    def __init__(self, store: mutex.LeaseStore, base_ports: Sequence[int], busy: Callable[[int], bool] = port_busy, stride: int = STRIDE,
                 slots: int = MAX_SLOTS, ttl_s: Optional[float] = None) -> None:
        self.store = store
        self.ttl_s = ttl_s
        self.base = [int(p) for p in base_ports]
        self.busy = busy
        self.stride = stride
        self.slots = max(1, min(MAX_SLOTS, slots))
        self.skipped = []  # type: List[Tuple[int, List[int]]]       # (slot, the busy ports) of the last allocation: for the event log

    def allocate(self, owner: str, **meta: Any) -> Optional[Block]:
        """The lowest slot that no worker holds and whose ports are all free, or None."""
        self.skipped = []
        for slot in range(self.slots):
            try:
                ports = block_ports(self.base, slot, self.stride)
            except ValueError:
                break                                   # the next slots only go further out
            try:
                lease = self.store.acquire(mutex.ports_lease(slot), owner, ttl_s=self.ttl_s, slot=slot, ports=ports, **meta)
            except mutex.LeaseHeld:
                continue
            taken = [p for p in ports if self.busy(p)]
            if taken:
                self.store.release(lease)
                self.skipped.append((slot, taken))
                continue
            return Block(slot, ports, lease, self.stride)
        return None

    def release(self, block: Optional[Block]) -> None:
        if block is not None:
            self.store.release(block.lease)
