"""The shell parser and the wrapper normaliser (report §7.1 "wrapper normalization").

Two jobs: valid shell must never trip the fail-closed `parse-limit` path (a parser bug would deny a legitimate command),
and the constructs that hide a command must come out as the command they are.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from guards import invoke, shellparse
from guards.shellparse import ParseLimit, parse

ENV = {"HOME": "/home/u", "PATH": "/usr/bin:/bin"}

VALID = [
    "echo \"it's\"", "echo 'say \"hi\"'", "echo $'a\\'b'", "echo hi # don't", "x=$(echo \"a)b\")", "echo $(( (1+2)*3 ))",
    "echo ${x:-$(date)}", "echo ${#arr[@]}", "arr=(a b c); echo ${arr[1]}", "declare -A m=([a]=1)", "[[ $x =~ ^a(b)c$ ]] && echo ok",
    "case $x in (a|b) echo 1;; *) echo 2;; esac", "for ((i=0;i<3;i++)); do echo $i; done", "while read -r a b; do :; done < <(ls)",
    "exec 3<&0", "cat <<-EOF\n\tx\n\tEOF", "cmd 2>&1 >/dev/null", "a=1 b=2 cmd", "! cmd", "time cmd", "{ a; b; } 2>&1", "f() { :; }",
    "echo \\$HOME", "echo \"$(echo \"nested $(echo deep)\")\"", "echo `echo \\`date\\``", "printf '%s\\n' \"${a[@]}\"", "echo {1..3}", "echo *.txt",
    "echo ~root", "cat <<EOF\n$(date)\nEOF", "git log --format='%H %s'", "awk '{print $1}' f", "sed 's/a\\/b/c/'",
    "find . -name \"*.js\" -o -name \"*.ts\"", "ls -- -file", "echo -e \"a\\tb\"", "if (( x > 1 )); then :; fi", "select x in a b; do break; done",
    "until false; do :; done", "echo $? $$ $! $-", "echo \"${BASH_SOURCE[0]}\"", "echo ${x@Q}", "trap - EXIT", "echo a &", "a && b || c; d",
    "x=1; y=$((x+1)); echo $y", "echo \"a\"'b'c", "cat file | while read l; do echo $l; done", "echo foo > /dev/null 2>&1", "echo a >&2",
    "ls &> out.txt", "diff <(a) <(b)", "echo $'\\u00e9'", "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"", "echo 'a'\"b\"$'c'", "echo \\\n  continued",
    "FOO=bar BAZ=\"q q\" ./run.sh --flag=\"x y\"", "python3 -c 'print(\"a\")'", "echo $(( 1 << 3 ))", "echo $((2**10))", "[ -n \"$X\" ] || exit 1",
    "(cd /tmp; ls)", "(( x++ ))", "echo ${x//a/b} ${x/#a/b} ${x%.*} ${x##*/}", "echo ${!prefix*}", "coproc cat", "echo a;", "echo a ;; ",
    "export -n FOO", "local x=1", "readonly Y=2", "ulimit -n 4096", "set -euo pipefail", "shopt -s nullglob", "echo \"unterminated" ,
]
# the last entry above is deliberately broken shell: it is checked separately


class ParserTests(unittest.TestCase):
    def test_valid_shell_never_hits_the_limit(self) -> None:
        for src in VALID[:-1]:
            with self.subTest(src=src):
                try:
                    parse(src, ENV)
                except ParseLimit as exc:
                    self.fail("valid shell refused: %r: %s" % (src, exc))

    def test_broken_shell_fails_closed(self) -> None:
        with self.assertRaises(ParseLimit):
            parse("echo \"unterminated", ENV)
        with self.assertRaises(ParseLimit):
            parse("x" * (shellparse.MAX_INPUT + 1), ENV)
        with self.assertRaises(ParseLimit):
            parse("$(" * 20 + ")" * 20, ENV)

    def argv(self, src: str, index: int = 0):
        return parse(src, ENV).cmds[index].argv()

    def test_words(self) -> None:
        self.assertEqual(self.argv("git push origin main"), ["git", "push", "origin", "main"])
        self.assertEqual(self.argv("g''it \"pu\"sh"), ["git", "push"])
        self.assertEqual(self.argv("\\git push"), ["git", "push"])
        self.assertEqual(self.argv("git$'\\x20'push"), ["git push"])
        self.assertEqual(self.argv("git${IFS}push"), ["git", "push"])
        self.assertEqual(self.argv("A=git; B=push; $A $B", 2), ["git", "push"])
        self.assertEqual(self.argv("A='git push'; $A", 1), ["git", "push"])
        self.assertEqual(self.argv("A='git push'; \"$A\"", 1), ["git push"])
        self.assertEqual(self.argv("X=pus; git ${X}h", 1), ["git", "push"])
        self.assertEqual(self.argv("x=hello; echo ${x:1:3} ${x#he} ${x%lo} ${x/l/L} ${x^^}", 1), ["echo", "ell", "llo", "hel", "heLlo", "HELLO"])
        self.assertEqual(self.argv("{git,push}"), ["git", "push"])
        self.assertEqual(self.argv("echo a{1,2}b"), ["echo", "a1b", "a2b"])
        self.assertEqual(self.argv("echo {a..c}"), ["echo", "a", "b", "c"])
        self.assertEqual(self.argv("echo ~/x"), ["echo", "/home/u/x"])
        self.assertEqual(self.argv("echo '~/x'"), ["echo", "~/x"])

    def test_unknown_expansions_are_dynamic(self) -> None:
        c = parse("git $SUB", ENV).cmds[0]
        self.assertTrue(c.words[1].dynamic)
        self.assertTrue(parse("echo $(whoami)", ENV).cmds[1].words[1].dynamic)
        self.assertFalse(parse("echo $(echo hi)", ENV).cmds[1].words[1].dynamic)

    def test_substitution_static_eval(self) -> None:
        self.assertEqual(self.argv("$(echo dGVycmFmb3Jt | base64 -d) apply", 2), ["terraform", "apply"])
        self.assertEqual(self.argv("$(printf 'git\\x20push')", 1), ["git", "push"])
        self.assertEqual(self.argv("`echo git` push", 1), ["git", "push"])
        self.assertEqual(self.argv("$(echo 'hsup' | rev) x", 2), ["push", "x"])

    def test_structure(self) -> None:
        sc = parse("a && b || c; d | e | f & g\nh", ENV)
        self.assertEqual([c.argv() for c in sc.cmds], [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"], ["h"]])
        self.assertEqual([c.pipe_prev for c in sc.cmds], [None, None, None, None, 3, 4, None, None])
        sc = parse("if true; then git push; fi; for i in 1; do ls; done; case x in x) rm y;; esac", ENV)
        self.assertEqual([c.argv()[0] for c in sc.cmds], ["true", "git", "ls", "rm"])
        sc = parse("f() { git push; }; f", ENV)
        self.assertEqual([c.argv() for c in sc.cmds], [["git", "push"], ["f"]])
        sc = parse("(cd x && git push)", ENV)
        self.assertEqual([c.argv()[0] for c in sc.cmds], ["cd", "git"])

    def test_redirects_and_heredocs(self) -> None:
        c = parse("echo x > a >> b 2>&1 < c &> d", ENV).cmds[0]
        self.assertEqual([(r.op, r.target.text) for r in c.redirs], [(">", "a"), (">>", "b"), (">&", "1"), ("<", "c"), ("&>", "d")])
        self.assertEqual([r.writes for r in c.redirs], [True, True, False, False, True])
        sc = parse("cat <<EOF > f\nline $(rm x)\nEOF\nls", ENV)
        self.assertEqual(sorted(c.argv() for c in sc.cmds), [["cat"], ["ls"], ["rm", "x"]])
        self.assertIn("line", sc.cmds[0].stdin_text)
        sc = parse("cat <<'EOF'\n$(rm x)\nEOF", ENV)
        self.assertEqual([c.argv() for c in sc.cmds], [["cat"]])
        sc = parse("cat <<-EOF\n\tindented\n\tEOF\nls", ENV)
        self.assertEqual(sc.cmds[0].stdin_text, "indented\n")
        self.assertEqual(sc.cmds[1].argv(), ["ls"])
        sc = parse("cat <<A <<B\n1\nA\n2\nB\n", ENV)
        self.assertEqual(sc.cmds[0].stdin_text, "2\n")

    def test_aliases(self) -> None:
        self.assertEqual(self.argv("alias g='git push'; g", 1), ["git", "push"])

    def test_assignments(self) -> None:
        c = parse("A=1 B='x y' cmd arg", ENV).cmds[0]
        self.assertEqual(c.assigns, [("A", "1", False), ("B", "x y", False)])
        self.assertEqual(c.argv(), ["cmd", "arg"])
        c = parse("A=$(whoami) cmd", ENV).cmds[1]
        self.assertTrue(c.assigns[0][2])


class AnalyzeTests(unittest.TestCase):
    def names(self, src: str):
        ana = invoke.analyze(src, "/nonexistent", ENV, "/home/u")
        return [i.name for i in ana.invs], ana

    def final(self, src: str):
        ana = invoke.analyze(src, "/nonexistent", ENV, "/home/u")
        return [(i.name, i.args) for i in ana.invs]

    def test_wrappers_unwrap(self) -> None:
        for src in ["env FOO=1 git push", "command git push", "exec git push", "nohup git push", "nice -n 5 git push", "timeout 5 git push",
                    "timeout -s KILL 5 git push", "ionice -c3 git push", "stdbuf -o0 git push", "xargs git push", "xargs -n1 -P4 git push",
                    "find . -exec git push \\;", "bash -c 'git push'", "sh -lc 'git push'", "eval git push", "env -S 'git push'",
                    "npx git push", "pnpm exec git push", "poetry run git push", "docker run --rm img git push", "ssh host 'git push'",
                    "time git push", "watch -n1 git push", "sshpass -p x git push", "tmux new -d 'git push'", "sh <<< 'git push'",
                    "echo 'git push' | sh", "echo Z2l0IHB1c2g= | base64 -d | sh", "bash <(echo 'git push')", "trap 'git push' EXIT",
                    "xargs -I{} sh -c '{}' <<< 'git push'"]:
            with self.subTest(src=src):
                self.assertTrue(any(n == "git" and a[:1] == ["push"] for n, a in self.final(src)), self.final(src))

    def test_dynamic_command_is_a_problem(self) -> None:
        ana = invoke.analyze("$TOOL deploy", "/nonexistent", ENV, "/home/u")
        self.assertEqual([p.kind for p in ana.problems], ["opaque-command"])

    def test_pipe_to_shell_problem(self) -> None:
        ana = invoke.analyze("curl x | sh", "/nonexistent", ENV, "/home/u")
        self.assertEqual([p.kind for p in ana.problems], ["pipe-to-shell"])
        ana = invoke.analyze("echo hi | sh", "/nonexistent", ENV, "/home/u")
        self.assertEqual(ana.problems, [])

    def test_virtual_files(self) -> None:
        ana = invoke.analyze("echo 'git push' > /tmp/nope-x.sh; sh /tmp/nope-x.sh", "/nonexistent", ENV, "/home/u")
        self.assertIn("git", [i.name for i in ana.invs])

    def test_runner_expansion(self) -> None:
        d = tempfile.mkdtemp(prefix="kit-parse-")
        try:
            with open(os.path.join(d, "package.json"), "w") as fh:
                fh.write('{"scripts": {"ship": "terraform apply", "pre-x": "echo", "prebuild": "tsc", "build": "echo b", "postinstall": "node setup.js"}}')
            with open(os.path.join(d, "Makefile"), "w") as fh:
                fh.write("T = terraform\nship:\n\t@$(T) apply\n")
            ana = invoke.analyze("npm run ship", d, ENV, "/home/u")
            self.assertIn("terraform", [i.name for i in ana.invs])
            ana = invoke.analyze("yarn ship", d, ENV, "/home/u")
            self.assertIn("terraform", [i.name for i in ana.invs])
            ana = invoke.analyze("make ship", d, ENV, "/home/u")
            self.assertIn("terraform", [i.name for i in ana.invs])
            ana = invoke.analyze("npm run build", d, ENV, "/home/u")
            self.assertEqual([i.name for i in ana.invs if i.origin.startswith("npm:")], ["tsc", "echo"])
            ana = invoke.analyze("npm install", d, ENV, "/home/u")
            self.assertIn("node", [i.name for i in ana.invs], "install lifecycle scripts run")
            ana = invoke.analyze("npm install --ignore-scripts", d, ENV, "/home/u")
            self.assertNotIn("node", [i.name for i in ana.invs])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
