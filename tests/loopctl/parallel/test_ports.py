"""Ports for parallel workers (report 19.7 L-27, 11.5 item 3 "port offset"): a slot per worker, never shared, never busy."""
from __future__ import annotations

import shutil
import socket
import tempfile
import unittest

from kitlib import runstate

from loopctl.parallel import mutex, ports


def listener(base):
    """A socket that is really listening on a free port at or above `base` (the probe is verify.py's own)."""
    for port in range(base, base + 200):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
            return s, port
        except OSError:
            s.close()
    raise unittest.SkipTest("no free port")


class Slots(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ports-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = mutex.LeaseStore(runstate.RunState(self.dir), ttl_s=60)
        self.busy_set = set()
        self.alloc = ports.PortAllocator(self.store, [4100, 4101], busy=lambda p: p in self.busy_set)

    def test_slots_shift_every_declared_port_and_are_never_shared(self):
        a, b = self.alloc.allocate("w:A"), self.alloc.allocate("w:B")
        self.assertEqual((a.slot, a.offset, a.ports), (0, 0, [4100, 4101]))
        self.assertEqual((b.slot, b.offset, b.ports), (1, 100, [4200, 4201]))
        self.assertEqual(b.env(), {"KIT_PORT_OFFSET": "100", "KIT_PORT_SLOT": "1", "KIT_PORTS": "4200,4201"})

    def test_a_released_slot_is_reused_lowest_first(self):
        a, b = self.alloc.allocate("w:A"), self.alloc.allocate("w:B")
        self.alloc.release(a)
        c = self.alloc.allocate("w:C")
        self.assertEqual(c.slot, 0)
        self.assertNotEqual(b.slot, c.slot)

    def test_a_busy_port_skips_its_whole_slot_and_says_which(self):
        self.busy_set.add(4101)                                   # slot 0's second port: a stranger's dev server, or a crashed worker's leak
        a = self.alloc.allocate("w:A")
        self.assertEqual(a.slot, 1)
        self.assertEqual(self.alloc.skipped, [(0, [4101])])
        self.assertIsNone(self.store.read(mutex.ports_lease(0)))   # the probe's own claim on slot 0 was given back

    def test_nothing_is_handed_out_when_every_slot_is_taken_or_busy(self):
        got = [self.alloc.allocate("w:%d" % n) for n in range(ports.MAX_SLOTS)]
        self.assertEqual(sorted(b.slot for b in got), list(range(ports.MAX_SLOTS)))
        self.assertIsNone(self.alloc.allocate("w:late"))
        self.alloc.release(got[3])
        self.busy_set.update([4100 + 300, 4101 + 300])
        self.assertIsNone(self.alloc.allocate("w:late"))

    def test_a_port_pushed_past_65535_ends_the_slots(self):
        alloc = ports.PortAllocator(self.store, [65500], busy=lambda p: False, stride=100)
        self.assertEqual(alloc.allocate("w:A").ports, [65500])
        self.assertIsNone(alloc.allocate("w:B"))                  # 65600 is not a port
        with self.assertRaises(ValueError):
            ports.block_ports([65500], 1)

    def test_no_declared_ports_still_gives_each_worker_its_own_slot(self):
        alloc = ports.PortAllocator(self.store, [], busy=lambda p: True)
        a, b = alloc.allocate("w:A"), alloc.allocate("w:B")
        self.assertEqual((a.ports, b.ports, a.offset, b.offset), ([], [], 0, 100))

    def test_the_real_probe_sees_a_listening_socket(self):
        s, port = listener(21000)
        self.addCleanup(s.close)
        alloc = ports.PortAllocator(self.store, [port])
        self.assertTrue(ports.port_busy(port))
        a = alloc.allocate("w:A")
        self.assertEqual((a.slot, a.ports), (1, [port + 100]))      # slot 0 is where the stranger listens
        self.assertEqual(alloc.skipped, [(0, [port])])

    def test_the_offset_a_gate_reads_is_a_plain_small_number(self):
        for raw, want in (("100", 100), ("0", 0), ("60000", 60000), ("60001", 0), ("-5", 0), ("1e3", 0), ("100 ", 0), ("", 0), ("٣", 0), (None, 0), (100, 0)):
            with self.subTest(raw):
                self.assertEqual(ports.offset_from_env({"KIT_PORT_OFFSET": raw} if raw is not None else {}), want)


if __name__ == "__main__":
    unittest.main()
