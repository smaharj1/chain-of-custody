"""Held-out acceptance checks for the hello-loop fixture.

These are black-box checks written from the build plan's acceptance criteria. They never
import the implementation: they run its commands and talk to its server over HTTP, so they
work on any correct solution. The harness does NOT copy this directory into a baseline run.
Use it afterwards to count escaped defects, and in this repo to prove the plan can be
satisfied (tests/test_hello_loop.py).

    python3 acceptance/run_checks.py --project DIR [--item F1|F2|F3|all] [--json]

Exit 0 when every selected criterion passes, 1 when any fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

ITEM_ORDER = ["F1", "F2", "F3"]
CHECKS = {}  # type: Dict[str, Callable[["Env"], None]]
ITEM_OF = {}  # type: Dict[str, str]


class Fail(Exception):
    pass


def check(ac_id: str):
    def deco(fn):
        CHECKS[ac_id] = fn
        ITEM_OF[ac_id] = ac_id.split("-")[1]
        return fn
    return deco


def expect(cond: bool, message: str) -> None:
    if not cond:
        raise Fail(message)


class Env(object):
    """A project directory plus a throwaway database, a command runner and a server."""

    def __init__(self, project: str) -> None:
        self.project = os.path.abspath(project)
        self.tmp = tempfile.mkdtemp(prefix="hello-loop-accept-")
        self.db = os.path.join(self.tmp, "accept.db")
        self.proc = None  # type: Optional[subprocess.Popen]
        self.base = ""

    @property
    def env(self) -> Dict[str, str]:
        e = dict(os.environ)
        e["DATABASE_URL"] = "file:" + self.db
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        return e

    def reset_db(self) -> None:
        self.stop()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable] + list(args), cwd=self.project, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def sql(self, query: str, params: tuple = ()) -> list:
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(query, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def migrated(self) -> None:
        self.reset_db()
        done = self.run("-m", "hello_loop.migrate")
        expect(done.returncode == 0, "migrate failed: " + done.stderr.decode()[-300:])

    def start(self) -> None:
        self.stop()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        e = self.env
        e["PORT"] = str(port)
        self.proc = subprocess.Popen([sys.executable, "-m", "hello_loop.server"], cwd=self.project, env=e,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.base = "http://127.0.0.1:%d" % port
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise Fail("server exited early (code %s)" % self.proc.returncode)
            try:
                urllib.request.urlopen(self.base + "/healthz", timeout=2).close()
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        raise Fail("server did not come up")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None

    def http(self, method: str, path: str, body: Optional[bytes] = None,
             content_type: Optional[str] = None) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def jhttp(self, method: str, path: str, payload=None) -> Tuple[int, object]:
        body = json.dumps(payload).encode() if payload is not None else None
        status, headers, raw = self.http(method, path, body, "application/json" if body is not None else None)
        try:
            return status, json.loads(raw.decode())
        except ValueError:
            raise Fail("%s %s: response is not JSON (status %d): %r" % (method, path, status, raw[:120]))

    def fresh_server(self) -> None:
        self.migrated()
        self.start()

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def tests_pass(env: Env, module: str) -> None:
    path = os.path.join(env.project, *module.split(".")) + ".py"
    expect(os.path.isfile(path), "%s does not exist" % os.path.relpath(path, env.project))
    done = env.run("-m", "unittest", module)
    out = done.stderr.decode()
    m = re.search(r"Ran (\d+) tests?", out)
    expect(done.returncode == 0, "%s failed: %s" % (module, out[-300:]))
    expect(bool(m) and int(m.group(1)) > 0, "%s ran no tests" % module)


# ---- F1 -------------------------------------------------------------------------------

@check("AC-F1-1")
def f1_1(env: Env) -> None:
    env.migrated()
    cols = {r[1]: r for r in env.sql("PRAGMA table_info(bookmarks)")}
    expect(sorted(cols) == ["created_at", "id", "title", "url"], "columns are %s" % sorted(cols))
    expect(cols["id"][2].upper() == "INTEGER" and cols["id"][5] == 1, "id must be INTEGER PRIMARY KEY")
    for name in ("url", "title", "created_at"):
        expect(cols[name][2].upper() == "TEXT" and cols[name][3] == 1, "%s must be TEXT NOT NULL" % name)
    env.sql("INSERT INTO bookmarks (url, title, created_at) VALUES ('https://u.test/', 't', 'x')")
    try:
        env.sql("INSERT INTO bookmarks (url, title, created_at) VALUES ('https://u.test/', 't2', 'y')")
    except Exception:
        return
    raise Fail("url is not UNIQUE")


@check("AC-F1-2")
def f1_2(env: Env) -> None:
    env.migrated()
    again = env.run("-m", "hello_loop.migrate")
    expect(again.returncode == 0, "second migrate failed")
    rows = env.sql("SELECT id FROM schema_migrations WHERE id = '0001_create_bookmarks'")
    expect(len(rows) == 1, "0001_create_bookmarks recorded %d times" % len(rows))


@check("AC-F1-3")
def f1_3(env: Env) -> None:
    env.migrated()
    expect(bool(env.sql("PRAGMA table_info(bookmarks)")), "bookmarks table does not exist before --down")
    down = env.run("-m", "hello_loop.migrate", "--down")
    expect(down.returncode == 0, "--down failed: " + down.stderr.decode()[-200:])
    tables = [r[0] for r in env.sql("SELECT name FROM sqlite_master WHERE type='table' AND name='bookmarks'")]
    expect(not tables, "bookmarks table still exists after --down")
    expect(not env.sql("SELECT id FROM schema_migrations WHERE id = '0001_create_bookmarks'"),
           "schema_migrations row still present after --down")


@check("AC-F1-4")
def f1_4(env: Env) -> None:
    env.migrated()
    for _ in range(2):
        done = env.run("-m", "hello_loop.seed")
        expect(done.returncode == 0, "seed failed: " + done.stderr.decode()[-200:])
    n = env.sql("SELECT COUNT(*) FROM bookmarks")[0][0]
    expect(n == 3, "expected exactly 3 rows after seeding twice, found %d" % n)


@check("AC-F1-5")
def f1_5(env: Env) -> None:
    tests_pass(env, "tests.test_schema")


# ---- F2 -------------------------------------------------------------------------------

GOOD = {"url": "https://example.com/", "title": "Example"}


@check("AC-F2-1")
def f2_1(env: Env) -> None:
    env.fresh_server()
    status, body = env.jhttp("POST", "/api/bookmarks", GOOD)
    expect(status == 201, "status %d, want 201" % status)
    expect(isinstance(body, dict) and sorted(body) == ["created_at", "id", "title", "url"], "keys: %r" % (body,))
    expect(body["url"] == GOOD["url"] and body["title"] == GOOD["title"], "url/title not echoed")
    expect(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", str(body["created_at"])) is not None,
           "created_at format: %r" % body["created_at"])


@check("AC-F2-2")
def f2_2(env: Env) -> None:
    env.fresh_server()
    for n in range(3):
        env.jhttp("POST", "/api/bookmarks", {"url": "https://e.test/%d" % n, "title": "T%d" % n})
    status, body = env.jhttp("GET", "/api/bookmarks")
    expect(status == 200, "status %d" % status)
    titles = [b["title"] for b in body["bookmarks"]]
    expect(titles == ["T2", "T1", "T0"], "order is %s, want newest first" % titles)


@check("AC-F2-3")
def f2_3(env: Env) -> None:
    env.fresh_server()
    bad = [{"url": "https://e.test/", "title": ""}, {"url": "https://e.test/", "title": "   "},
           {"url": "https://e.test/"}, {"title": "no url"}, {"url": "ftp://e.test/", "title": "T"},
           {"url": "javascript:alert(1)", "title": "T"}, ["a", "list"], "a string", 7]
    for payload in bad:
        status, body = env.jhttp("POST", "/api/bookmarks", payload)
        expect(status == 400, "%r -> %d, want 400" % (payload, status))
        expect(isinstance(body, dict) and body.get("error"), "%r -> no error message" % (payload,))
    status, _, _ = env.http("POST", "/api/bookmarks", b"{not json", "application/json")
    expect(status == 400, "malformed JSON -> %d, want 400" % status)
    _, body = env.jhttp("GET", "/api/bookmarks")
    expect(body["bookmarks"] == [], "invalid input stored a row")


@check("AC-F2-4")
def f2_4(env: Env) -> None:
    env.fresh_server()
    env.jhttp("POST", "/api/bookmarks", GOOD)
    status, body = env.jhttp("POST", "/api/bookmarks", dict(GOOD, title="Other"))
    expect(status == 409, "duplicate -> %d, want 409" % status)
    expect(isinstance(body, dict) and body.get("error"), "no error message")
    _, listing = env.jhttp("GET", "/api/bookmarks")
    expect(len(listing["bookmarks"]) == 1, "duplicate added a row")


@check("AC-F2-5")
def f2_5(env: Env) -> None:
    env.fresh_server()
    _, created = env.jhttp("POST", "/api/bookmarks", GOOD)
    status, body = env.jhttp("GET", "/api/bookmarks/%d" % created["id"])
    expect(status == 200 and body == created, "GET one -> %d %r" % (status, body))
    for path in ("/api/bookmarks/99999", "/api/bookmarks/abc"):
        status, body = env.jhttp("GET", path)
        expect(status == 404, "%s -> %d, want 404" % (path, status))
        expect(isinstance(body, dict) and body.get("error"), "%s: no error message" % path)


@check("AC-F2-6")
def f2_6(env: Env) -> None:
    tests_pass(env, "tests.test_bookmarks_api")
    done = env.run("scripts/verify_run.py", "/api/bookmarks")
    expect(done.returncode == 0, "verify_run /api/bookmarks failed: " + (done.stdout + done.stderr).decode()[-200:])


# ---- F3 -------------------------------------------------------------------------------

def form(env: Env, **fields: str) -> Tuple[int, Dict[str, str], bytes]:
    return env.http("POST", "/bookmarks", urllib.parse.urlencode(fields).encode(),
                    "application/x-www-form-urlencoded")


@check("AC-F3-1")
def f3_1(env: Env) -> None:
    env.fresh_server()
    env.jhttp("POST", "/api/bookmarks", {"url": "https://a.test/", "title": "First"})
    env.jhttp("POST", "/api/bookmarks", {"url": "https://b.test/", "title": "Second"})
    status, headers, raw = env.http("GET", "/")
    page = raw.decode()
    expect(status == 200, "GET / -> %d" % status)
    expect(headers.get("Content-Type", "").lower() == "text/html; charset=utf-8",
           "Content-Type is %r" % headers.get("Content-Type"))
    expect('<a href="https://a.test/">First</a>' in page, "link for First missing")
    expect('<a href="https://b.test/">Second</a>' in page, "link for Second missing")
    expect(page.index(">Second</a>") < page.index(">First</a>"), "not newest first")


@check("AC-F3-2")
def f3_2(env: Env) -> None:
    env.fresh_server()
    status, _, raw = env.http("GET", "/")
    expect(status == 200 and "No bookmarks yet." in raw.decode(), "empty-state text missing")


@check("AC-F3-3")
def f3_3(env: Env) -> None:
    env.fresh_server()
    env.jhttp("POST", "/api/bookmarks", {"url": "https://x.test/?a=1&b=2", "title": "<script>alert(1)</script>"})
    _, _, raw = env.http("GET", "/")
    page = raw.decode()
    expect("&lt;script&gt;alert(1)&lt;/script&gt;" in page, "title is not escaped")
    expect("<script" not in page.lower(), "page contains a raw <script> tag")
    expect("a=1&amp;b=2" in page, "& in url is not escaped")


@check("AC-F3-4")
def f3_4(env: Env) -> None:
    env.fresh_server()
    _, _, raw = env.http("GET", "/")
    page = raw.decode()
    expect('<form method="post" action="/bookmarks">' in page, "form tag missing")
    expect('name="url"' in page and 'name="title"' in page, "form inputs missing")
    status, headers, _ = form(env, url="https://ok.test/", title="Fine")
    expect(status == 303 and headers.get("Location") == "/", "valid post -> %d %r" % (status, headers.get("Location")))
    expect(env.sql("SELECT title FROM bookmarks WHERE url = 'https://ok.test/'") == [("Fine",)], "not stored")
    status, _, raw = form(env, url="notaurl", title="Bad")
    expect(status == 400, "invalid post -> %d, want 400" % status)
    low = raw.decode().lower()
    expect(any(w in low for w in ("must", "invalid", "required", "error")), "400 page shows no validation message")
    expect(env.sql("SELECT COUNT(*) FROM bookmarks")[0][0] == 1, "invalid post stored a row")
    status, _, raw = form(env, url="https://ok2.test/", title="   ")
    expect(status == 400, "blank title -> %d, want 400" % status)


@check("AC-F3-5")
def f3_5(env: Env) -> None:
    tests_pass(env, "tests.test_page")
    done = env.run("scripts/verify_run.py", "/")
    expect(done.returncode == 0, "verify_run / failed: " + (done.stdout + done.stderr).decode()[-200:])


def run(project: str, items: List[str]) -> Dict[str, Dict[str, str]]:
    results = {}  # type: Dict[str, Dict[str, str]]
    env = Env(project)
    try:
        for item in items:
            results[item] = {}
            for ac in sorted(a for a in CHECKS if ITEM_OF[a] == item):
                try:
                    CHECKS[ac](env)
                    results[item][ac] = "pass"
                except Fail as exc:
                    results[item][ac] = "fail: %s" % exc
                except Exception as exc:  # a crashing check is a failed criterion, not a crash
                    results[item][ac] = "fail: %s: %s" % (type(exc).__name__, exc)
                finally:
                    env.stop()
    finally:
        env.close()
    return results


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Held-out acceptance checks for hello-loop")
    ap.add_argument("--project", required=True, help="directory holding hello_loop/ to check")
    ap.add_argument("--item", default="all", choices=ITEM_ORDER + ["all"])
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    items = ITEM_ORDER if args.item == "all" else [args.item]
    results = run(args.project, items)
    failed = sum(1 for item in results.values() for v in item.values() if v != "pass")
    if args.json:
        print(json.dumps({"failed": failed, "results": results}, indent=2))
    else:
        for item, acs in results.items():
            for ac, verdict in acs.items():
                print("%s  %s" % (ac, verdict))
        print("%d criteria failed" % failed if failed else "all criteria pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
