"""jsonio: atomic writes and exclusive create (report §10.3, §19.7 Lock)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest

from kitlib import jsonio
from tests.helpers import BaseTestCase


class AtomicWrite(BaseTestCase):
    def test_write_and_overwrite(self):
        p = os.path.join(self.tmpdir(), "a.json")
        jsonio.atomic_write_json(p, {"b": 1, "a": [1, 2]})
        self.assertEqual(jsonio.read_json(p), {"a": [1, 2], "b": 1})
        jsonio.atomic_write_json(p, {"v": 2})
        self.assertEqual(jsonio.read_json(p), {"v": 2})
        with open(p) as fh:
            self.assertTrue(fh.read().endswith("}\n"))

    def test_no_temp_files_left_behind(self):
        d = self.tmpdir()
        p = os.path.join(d, "a.txt")
        for i in range(5):
            jsonio.atomic_write_text(p, "x%d" % i)
        self.assertEqual(os.listdir(d), ["a.txt"])

    def test_mode_is_applied(self):
        p = os.path.join(self.tmpdir(), "secret")
        jsonio.atomic_write_text(p, "s", mode=0o600)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_failed_write_keeps_the_old_content(self):
        d = self.tmpdir()
        p = os.path.join(d, "a.json")
        jsonio.atomic_write_json(p, {"ok": True})
        with self.assertRaises(TypeError):
            jsonio.atomic_write_json(p, {"bad": object()})
        self.assertEqual(jsonio.read_json(p), {"ok": True})
        self.assertEqual(os.listdir(d), ["a.json"])

    def test_readers_never_see_a_partial_file_while_a_writer_loops(self):
        p = os.path.join(self.tmpdir(), "a.json")
        jsonio.atomic_write_json(p, {"n": 0, "pad": "x" * 50000})
        stop = threading.Event()
        errors = []

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                jsonio.atomic_write_json(p, {"n": n, "pad": "x" * 50000})

        t = threading.Thread(target=writer)
        t.start()
        try:
            for _ in range(300):
                try:
                    d = jsonio.read_json(p)
                    if len(d["pad"]) != 50000:
                        errors.append("short")
                except ValueError as exc:
                    errors.append(str(exc))
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [])

    def test_read_json_opt(self):
        d = self.tmpdir()
        self.assertIsNone(jsonio.read_json_opt(os.path.join(d, "missing")))
        self.assertEqual(jsonio.read_json_opt(os.path.join(d, "missing"), {"x": 1}), {"x": 1})
        bad = os.path.join(d, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{oops")
        with self.assertRaises(ValueError):
            jsonio.read_json_opt(bad)


class ExclusiveCreate(BaseTestCase):
    def test_second_create_fails(self):
        p = os.path.join(self.tmpdir(), "lock")
        self.assertTrue(jsonio.try_create_exclusive(p, b"one"))
        self.assertFalse(jsonio.try_create_exclusive(p, b"two"))
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(), b"one")
        with self.assertRaises(FileExistsError):
            jsonio.create_exclusive(p, b"three")

    def test_content_and_mode(self):
        p = os.path.join(self.tmpdir(), "lock")
        jsonio.create_exclusive(p, b'{"a": 1}', mode=0o600)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(jsonio.read_json(p), {"a": 1})

    def test_no_temp_files_left_behind(self):
        d = self.tmpdir()
        p = os.path.join(d, "lock")
        jsonio.try_create_exclusive(p, b"1")
        jsonio.try_create_exclusive(p, b"2")
        self.assertEqual(os.listdir(d), ["lock"])

    def test_thread_contention_has_exactly_one_winner(self):
        p = os.path.join(self.tmpdir(), "lock")
        n = 24
        barrier = threading.Barrier(n)
        wins = []

        def go(i):
            barrier.wait()
            if jsonio.try_create_exclusive(p, json.dumps({"owner": i}).encode()):
                wins.append(i)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(jsonio.read_json(p), {"owner": wins[0]})

    def test_process_contention_has_exactly_one_winner(self):
        d = self.tmpdir()
        p = os.path.join(d, "lock")
        go = os.path.join(d, "go")
        code = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from kitlib import jsonio\n"
            "while not os.path.exists(%r):\n"
            "    time.sleep(0.001)\n"
            "print('WIN' if jsonio.try_create_exclusive(%r, sys.argv[1].encode()) else 'LOSE')\n"
        ) % (os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".claude", "scripts"), go, p)
        procs = [self.procs.spawn([sys.executable, "-c", code, str(i)]) for i in range(6)]
        open(go, "w").close()
        outs = [pr.communicate(timeout=30)[0].decode().strip() for pr in procs]
        self.assertEqual(outs.count("WIN"), 1, outs)
        self.assertEqual(outs.count("LOSE"), 5, outs)

    def test_a_concurrent_reader_never_sees_an_empty_lock(self):
        d = self.tmpdir()
        stop = threading.Event()
        seen_empty = []

        def reader(path):
            while not stop.is_set():
                try:
                    with open(path, "rb") as fh:
                        if fh.read() == b"":
                            seen_empty.append(1)
                except FileNotFoundError:
                    pass

        for i in range(200):
            p = os.path.join(d, "lock%d" % i)
            t = threading.Thread(target=reader, args=(p,))
            t.start()
            jsonio.try_create_exclusive(p, b"x" * 4096)
            stop.set()
            t.join()
            stop.clear()
        self.assertEqual(seen_empty, [])


if __name__ == "__main__":
    unittest.main()
