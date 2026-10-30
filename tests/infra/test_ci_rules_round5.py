"""Fifth review, package D: the variable-command regex is bounded (D1), a stray quote in an earlier line does not hide a multi-line value (D2), a comment
ends at its line before continuations are joined (D3), `$'..'` is a plain word (D4), the variable-command regex reads flags, values, prefixes and
arrays (D5), and its verb ends at a word boundary that is not a hyphen (D6).

A row is read by the doctor's deploy-class list and by the lint's apply-class list (`_BothReaders`); the variable-command rows are read by the doctor's
label and by the lint's `infra.apply-indirect` rule, which share one regex text (`workflows.var_command_re`).
"""
from __future__ import annotations

import time
import unittest

import ci_lint
from doctorlib import deploy, workflows
from infra import ci_rules

from .test_ci_rules_review14 import DISPATCH_WF, _BothReaders, infra_rules

VAR_LABEL = "deploy through a variable"
VAR_RX = dict(workflows.DEPLOY_COMMANDS)[VAR_LABEL]


def wf_of(run):
    return DISPATCH_WF % "\n".join("          " + l for l in run.split("\n"))


def timed(f, *a):
    t = time.perf_counter()
    try:
        f(*a)
    except workflows.ScanLimit:
        pass
    return time.perf_counter() - t


class VariableCommandIsBounded(unittest.TestCase):
    """D1: `(?:\\w+=\\S*[ \\t]+)*` and `(?:-\\S+[ \\t]+)*` ran over `;`, `&` and `|` to the end of the text from every start (44 s at 200 KB)."""

    SHAPES = (lambda n: ";A=x" * (n // 4), lambda n: "&A=x" * (n // 4), lambda n: "|A=x" * (n // 4), lambda n: "(A=x" * (n // 4), lambda n: "{A=x" * (n // 4),
              lambda n: ";$A -x" * (n // 6), lambda n: ";$A -x -y" * (n // 9), lambda n: ';"$A" -x' * (n // 8), lambda n: ";$A" * (n // 3),
              lambda n: "then $A -x " * (n // 11), lambda n: "do $A -x " * (n // 9), lambda n: "A=x " * (n // 4) + "$T -x", lambda n: "$T " + "-x " * (n // 3) + "plan",
              lambda n: ";sudo A=x $T -x " * (n // 16), lambda n: ";$(a)" * (n // 5), lambda n: ";`a`" * (n // 4), lambda n: ("A=x " * 5 + "$T -x -y\n") * (n // 40),
              lambda n: ";A=" + "x" * n, lambda n: "sudo " * (n // 5), lambda n: "$T " + "-xxxxxxxxxx " * (n // 12) + "plan")

    def test_each_regex_grows_linearly_to_200_kb(self):
        # a quadratic regex takes 16x as long on 4x the input, a linear one 4x; the cut is 8x. The ratio holds on a loaded machine, because both runs slow
        # down together, and the best of 3 runs removes a one-off stall. The old regex took 44 s at 200 KB (the 2 s cap is a backstop); a shape under
        # 50 ms at 200 KB cannot hide harmful growth, so only the slower shapes are timed again
        def best(rx, text, k=3):
            b = float("inf")
            for _ in range(k):
                t = time.perf_counter()
                rx.search(text)
                b = min(b, time.perf_counter() - t)
            return b
        for rx in (VAR_RX, ci_rules._INDIRECT_RE):
            for make in self.SHAPES:
                big = best(rx, make(200000), 1)
                self.assertLess(big, 2.0, make(40)[:20])
                if big > 0.05:
                    small, big = best(rx, make(50000)), min(big, best(rx, make(200000), 2))
                    self.assertLess(big / small, 8.0, "%s: %.3f s -> %.3f s" % (make(40)[:20], small, big))

    def test_the_reviewers_inputs_whole_pipeline(self):
        for text in (";A=x" * 50000, ";$A -x" * 33333):
            workflows.normalize_shell.cache_clear()
            self.assertLess(timed(workflows.deploy_labels, text), 2.0)
            workflows.normalize_shell.cache_clear()
            self.assertLess(timed(ci_lint.lint_text, wf_of(text), "w.yml"), 2.0)

    def test_classify_workflow_on_a_100_kb_script(self):
        text = wf_of(";A=x" * 25000)
        t = time.perf_counter()
        deploy.classify_workflow(".github/workflows/w.yml", text)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_the_lint_with_an_infra_job_and_a_hostile_script(self):
        # the lint's own regex only runs in a job that runs an infra tool
        for text in (";A=x" * 50000, ";$A -x" * 33333):
            workflows.normalize_shell.cache_clear()
            self.assertLess(timed(ci_lint.lint_text, wf_of("terraform plan\n" + text), "w.yml"), 2.0)

    def test_a_run_stops_at_a_separator(self):
        for text in ("$T -x; apply", "$T -x && apply", "$T -x | apply", '$T -x "a;b" apply'):
            with self.subTest(text):
                self.assertNotIn(VAR_LABEL, workflows.deploy_labels(text))

    def test_the_bounds_are_wide_enough_for_an_honest_line(self):
        self.assertIn(VAR_LABEL, workflows.deploy_labels("A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 $T apply"))
        self.assertIn(VAR_LABEL, workflows.deploy_labels("$T " + "-x " * 15 + "apply"))


class StrayQuoteBeforeMultiLineValue(_BothReaders, unittest.TestCase):
    """D2: a stray quote in an earlier line (a heredoc body, `say "`) pairs with the opener of a value that runs over several lines and shifts every
    pairing after it. The passes over several lines start again after a line with an odd number of quotes."""

    def test_the_reviewers_inputs(self):
        for pre in ("cat <<'EOF'\ndon't\nEOF", 'cat <<\'EOF\'\nsay "\nEOF', "cat <<'EOF'\ndon't\nEOF"):
            with self.subTest(pre):
                self.assertHit(pre + "\ncdk -c 'a\nb' deploy", "cdk deploy")
        self.assertHit('cat <<\'EOF\'\nsay "\nEOF\ncdk -c "a\nb" deploy', "cdk deploy")
        self.assertHit("cat <<'EOF'\ndon't\nEOF\ncdk -c ${{ format('{0}',\n 'x') }} deploy", "cdk deploy")

    def test_the_same_with_terraform_and_kubectl(self):
        self.assertHit("cat <<'EOF'\ndon't\nEOF\nterraform -chdir='a\nb' apply -auto-approve", "terraform apply")
        self.assertHit("cat <<'EOF'\ndon't\nEOF\nkubectl --context 'a\nb' apply -f x", "kubectl apply")

    def test_any_number_of_stray_lines_before_the_value(self):
        for count in (1, 2, 3, 4, 5, 7, 20, 200):
            for stray in ("don't", "it's"):
                cmd = "cat <<'EOF'\n%s\nEOF\ncdk -c 'a\nb' deploy" % "\n".join([stray] * count)
                with self.subTest("%d x %s" % (count, stray)):
                    self.assertHit(cmd, "cdk deploy")

    def test_double_quote_strays_before_a_double_quoted_value(self):
        for count in (1, 2, 3, 6, 30):
            cmd = 'cat <<\'EOF\'\n%s\nEOF\ncdk -c "a\nb" deploy' % "\n".join(['say "'] * count)
            with self.subTest(count):
                self.assertHit(cmd, "cdk deploy")

    def test_a_value_over_several_lines_with_no_stray_is_unchanged(self):
        self.assertHit("cdk -c 'a\nb' deploy", "cdk deploy")
        self.assertHit('cdk -c "a\nb" deploy', "cdk deploy")

    def test_a_read_only_neighbour_stays_quiet(self):
        for cmd in ("cat <<'EOF'\ndon't\nEOF\ncdk -c 'a\nb' synth", "cat <<'EOF'\ndon't\nEOF\ncdk synth 'a\nb'",
                    "cat <<'EOF'\ndon't deploy\nEOF\necho 'a\nb'"):
            with self.subTest(cmd):
                self.assertQuiet(cmd, "cdk deploy")

    def test_the_extra_passes_are_bounded(self):
        self.assertLessEqual(len(workflows._fresh_starts("don't\n" * 1000)), workflows._FRESH_STARTS)
        self.assertEqual(workflows._fresh_starts("a\nb\nc"), [])
        self.assertEqual(workflows._fresh_starts("a'\nb\nc'\nd"), [3, 8])

    def test_hostile_stray_lines_are_fast(self):
        for text in ("cat <<'EOF'\n" + "don't\n" * 33000 + "EOF\ncdk -c 'a\nb' deploy", ("it's 'x\n") * 25000 + "cdk -c 'a\nb' deploy",
                     ('say "\n' * 30000) + 'cdk -c "a\nb" deploy', ("`\n" * 60000) + "cdk deploy"):
            workflows.normalize_shell.cache_clear()
            self.assertLess(timed(workflows.deploy_labels, text), 2.0)
            workflows.normalize_shell.cache_clear()
            self.assertLess(timed(ci_lint.lint_text, wf_of(text), "w.yml"), 2.0)


class CommentEndsAtItsLine(unittest.TestCase):
    """D3: a backslash at the end of a comment continues nothing. The lint cut comments after it joined continuations, so the next line was read as
    part of the comment."""

    def test_the_reviewers_input(self):
        self.assertIn("infra.apply-ungated", infra_rules("echo a # c \\\nterraform apply"))

    def test_other_shapes(self):
        for cmd in ("echo a # c \\\ncdk deploy", "# whole line comment \\\nterraform apply", "echo a # c \\\r\nterraform apply", "echo a # c \\\n\nterraform apply",
                    "echo ok # it's \\\nterraform apply"):
            with self.subTest(cmd):
                self.assertIn("infra.apply-ungated", infra_rules(cmd))

    def test_a_real_continuation_is_still_joined(self):
        # the flags of a call that runs over two lines are read as one line
        self.assertIn("infra.apply-ungated", infra_rules("terraform \\\n  apply -auto-approve"))
        self.assertIn("infra.apply-ungated", infra_rules("terraform -chdir=infra \\\n  apply"))
        self.assertEqual([c[1] for c in ci_rules._tf_calls("terraform plan \\\n  -out=tfplan")], ["plan"])

    def test_a_commented_out_deploy_after_a_continuation_is_still_not_one(self):
        for cmd in ("echo a \\\n  # terraform apply", "echo a # terraform apply \\\n  and more", "# terraform apply \\\n# cdk deploy"):
            with self.subTest(cmd):
                self.assertNotIn("infra.apply-ungated", infra_rules(cmd))


class AnsiQuotedWords(_BothReaders, unittest.TestCase):
    """D4: bash reads `$'x'` and `$"x"` as the word `x`."""

    def test_the_reviewers_inputs(self):
        self.assertHit("$'terraform' apply", "terraform apply")
        self.assertHit("terraform $'apply'", "terraform apply")

    def test_other_shapes(self):
        for cmd, label in (('$"terraform" apply', "terraform apply"), ("cdk $'deploy'", "cdk deploy"), ("$'kubectl' $'apply' -f x", "kubectl apply"),
                           ("$'helm' upgrade r c", "helm upgrade"), ("$'cdk' $'deploy' --all", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_neighbour_that_is_not_a_deploy_stays_quiet(self):
        for cmd, label in (("cdk synth $'deploy'", "cdk deploy"), ("terraform $'plan'", "terraform apply"), ("echo $'a b' deploy", "cdk deploy")):
            with self.subTest(cmd):
                self.assertQuiet(cmd, label)


class VariableCommandFlagsPrefixesAndArrays(unittest.TestCase):
    """D5: the doctor's variable-command label reads flags with values, `sudo` and `env` in front, and `${NAME[@]}`; the lint's `infra.apply-indirect`
    rule reads the same text."""

    INPUTS = ('"$T" -var-file x.tfvars apply', '"$T" -chdir infra apply', 'sudo "$T" apply', 'env X=1 "$T" apply', '"${CMD[@]}" apply')

    def test_the_reviewers_inputs_are_labels(self):
        for cmd in self.INPUTS:
            with self.subTest(cmd):
                self.assertIn(VAR_LABEL, workflows.deploy_labels(cmd))

    def test_the_lint_reads_them_too(self):
        for cmd in self.INPUTS:
            with self.subTest(cmd):
                self.assertTrue(ci_rules._INDIRECT_RE.search(ci_rules._norm_run(cmd)))

    def test_other_shapes(self):
        for cmd in ('"$T" -chdir=infra apply', '$T -var "a b" apply', "$T -var-file=x.tfvars -auto-approve apply", "$T -- apply", "$T $FLAGS apply",
                    '"$T" "$@" apply', "sudo env A=1 B=2 $T destroy", "exec $T deploy", "nohup $T deploy", "time $T apply", '"${CMD[*]}" deploy',
                    "cd x && sudo -n 2>/dev/null; $T apply", "if true; then env X=1 $T apply; fi"):
            with self.subTest(cmd):
                self.assertIn(VAR_LABEL, workflows.deploy_labels(cmd))

    def test_a_flag_value_is_not_the_verb_but_the_verb_is_still_found(self):
        self.assertIn(VAR_LABEL, workflows.deploy_labels('"$T" -x apply apply'))       # `-x apply` then the verb
        self.assertIn(VAR_LABEL, workflows.deploy_labels('"$T" -x apply'))             # `-x` then the verb
        self.assertNotIn(VAR_LABEL, workflows.deploy_labels('"$T" -x plan'))

    def test_a_neighbour_that_is_not_a_command_is_quiet(self):
        for cmd in ('echo "$T" apply', 'x="$T" apply', "echo sudo $T apply", '"$T" --version', "$T plan", "env X=1 $T plan", "sudo $T",
                    "$T -x; apply", "echo hi # sudo $T apply"):
            with self.subTest(cmd):
                self.assertNotIn(VAR_LABEL, workflows.deploy_labels(cmd))


class VariableCommandVerbEnd(unittest.TestCase):
    """D6: `\\b` matched before a hyphen, so `"$CMD" deploy-docs` was a deploy."""

    def test_a_word_that_starts_with_a_verb_is_not_one(self):
        for cmd in ('"$CMD" deploy-docs', "$CMD apply-patch", '"$T" destroy-all-the-things', "$CMD deploy_all", "$CMD deployment", '"$X" applying'):
            with self.subTest(cmd):
                self.assertNotIn(VAR_LABEL, workflows.deploy_labels(cmd))
                self.assertFalse(ci_rules._INDIRECT_RE.search(ci_rules._norm_run(cmd)), cmd)

    def test_the_verb_itself_is_still_one(self):
        for cmd in ('"$CMD" deploy', '"$CMD" deploy --all', '"$CMD" deploy;', '"$CMD" deploy)', "$CMD apply\nnext", "$CMD destroy -auto-approve"):
            with self.subTest(cmd):
                self.assertIn(VAR_LABEL, workflows.deploy_labels(cmd))
                self.assertTrue(ci_rules._INDIRECT_RE.search(ci_rules._norm_run(cmd)), cmd)

    def test_up_in_the_lint_has_the_same_end(self):
        self.assertTrue(ci_rules._INDIRECT_RE.search(ci_rules._norm_run("$CMD up")))
        self.assertFalse(ci_rules._INDIRECT_RE.search(ci_rules._norm_run("$CMD up-to-date")))


if __name__ == "__main__":
    unittest.main()
