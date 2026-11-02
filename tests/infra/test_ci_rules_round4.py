"""Fourth review, package D: the structural fail-closed wrapper, the per-line shell tokenizer (D1, D2), bare variable words as flags (D3), the global
flags of `aws`, `gcloud` and `az` (D4), quotes inside a word and a variable in command position (D5), and the delimiter limit and NUL (D6).

Each class has one test per fix and a legitimate neighbour that must stay quiet. A row is read by the doctor's deploy-class list, by the lint's
apply-class list and, end to end, by `lint_text` (`_BothReaders`).
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import ci_lint
from doctorlib import deploy, workflows
from infra import ci_rules

from .test_ci_rules_review14 import DISPATCH_WF, _BothReaders, infra_rules


def lint(text, name="w.yml"):
    return ci_lint.lint_text(text, name)


def rules_of(findings):
    return sorted({f.rule for f in findings})


class UnreadableIsNeverClean(unittest.TestCase):
    """Structural: whatever stops the lint or the doctor's deploy-surface reader on a file (an exception, `RecursionError`, more delimiters than the span
    pass reads) is an error or a deploy, never a pass."""

    def wf(self, run):
        return DISPATCH_WF % "\n".join("          " + l for l in run.split("\n"))

    def test_an_exception_in_a_rule_is_an_unreadable_error(self):
        with mock.patch.object(ci_rules, "check", side_effect=ValueError("boom")):
            fs = lint(self.wf("echo hi"))
        self.assertEqual([(f.rule, f.severity) for f in fs], [("ci.unreadable", "error")])
        self.assertIn("ValueError", fs[0].message)

    def test_a_recursion_error_is_an_unreadable_error(self):
        with mock.patch.object(ci_rules, "check", side_effect=RecursionError("deep")):
            fs = lint(self.wf("echo hi"))
        self.assertEqual([(f.rule, f.severity) for f in fs], [("ci.unreadable", "error")])

    def test_a_script_past_the_delimiter_limit_is_an_unreadable_error(self):
        fs = lint(self.wf("echo " + "$(x)" * 60000 + "\nterraform apply"))
        self.assertEqual([(f.rule, f.severity) for f in fs], [("ci.unreadable", "error")])

    def test_an_unreadable_file_makes_lint_paths_fail(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "w.yml"), "w") as fh:
                fh.write(self.wf("echo hi"))
            with mock.patch.object(ci_rules, "check", side_effect=RuntimeError("x")):
                fs = ci_lint.lint_paths([d], None, d)
        self.assertIn("ci.unreadable", rules_of(fs))
        self.assertTrue(any(f.severity == "error" for f in fs))

    def test_a_clean_file_is_still_clean(self):
        self.assertNotIn("ci.unreadable", rules_of(lint(self.wf("echo hi"))))

    def test_the_doctors_reader_counts_an_unreadable_workflow_as_a_deploy(self):
        text = "name: d\non:\n  push:\n    branches: [main]\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        with mock.patch.object(workflows, "deploy_labels", side_effect=RecursionError("deep")):
            row = deploy.classify_workflow(".github/workflows/d.yml", text)
        self.assertTrue(row["deploys"])
        self.assertFalse(row["parsed"])
        self.assertIn("unreadable", row["labels"][0])
        self.assertIn("push:branches", row["fires_on"])

    def test_the_deploy_labels_of_a_script_past_the_limit_are_one_unreadable_label(self):
        self.assertEqual(workflows.deploy_labels("echo " + "$(x)" * 60000), [workflows.UNREADABLE_LABEL])


class ShellTokenizer(_BothReaders, unittest.TestCase):
    """D1 and D2: a comment is cut by a per-line tokenizer that knows quotes, `$'..'`, `\\'`, `$(..)` and `${..}`; a quote is paired within its line (a
    value that runs over several lines is also tried). When the state is unclear, nothing is stripped and no quoted span is excluded."""

    def cut(self, line):
        return workflows.strip_comments(line)

    def test_a_hash_inside_a_quote_or_an_expression_is_not_a_comment(self):
        for line in ('cdk -c "a #b" deploy', "cdk -c 'a #b' deploy", "cdk -c ${{ format('a #{0}', x) }} deploy", "cdk -c $'a #b' deploy",
                     'terraform -chdir="a #b" apply', 'kubectl --context "a #b" apply -f x', "cdk -c $(echo a #b) deploy", "cdk -c a\\ #b deploy"):
            with self.subTest(line):
                self.assertEqual(self.cut(line), line)

    def test_a_real_comment_is_cut(self):
        self.assertEqual(self.cut("cdk deploy # why"), "cdk deploy ")
        self.assertEqual(self.cut("# cdk deploy"), "")
        self.assertEqual(self.cut("echo 'a' # it's"), "echo 'a' ")
        self.assertEqual(self.cut('echo "a b" # x'), 'echo "a b" ')
        self.assertEqual(self.cut("echo a#b"), "echo a#b")                     # a `#` inside a word is not a comment
        self.assertEqual(self.cut("echo ${#x} # c"), "echo ${#x} ")

    def test_an_unbalanced_line_keeps_everything(self):
        for line in ("echo don't # cdk deploy", 'echo "a # b', "echo $(a # b", "echo `a # b", "echo ${a # b"):
            with self.subTest(line):
                self.assertEqual(self.cut(line), line)

    def test_an_ansi_c_string_with_an_escaped_quote(self):
        self.assertEqual(self.cut("echo $'it\\'s' # c"), "echo $'it\\'s' ")
        self.assertEqual(self.cut("echo $'a\\' #b"), "echo $'a\\' #b")

    def test_the_reviewers_hash_inputs_are_applies(self):
        for cmd, label in (('cdk -c "a #b" deploy', "cdk deploy"), ("cdk -c 'a #b' deploy", "cdk deploy"), ('kubectl --context "a #b" apply -f x', "kubectl apply"),
                           ("cdk -c ${{ format('a #{0}', x) }} deploy", "cdk deploy"), ("cdk -c $(echo a #b) deploy", "cdk deploy"),
                           ('terraform -chdir="a #b" apply', "terraform apply"), ("cdk -c a\\ #b deploy", "cdk deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_commented_out_deploy_is_still_not_one(self):
        for cmd in ("# cdk deploy", "echo hi # cdk deploy", 'echo "a" # kubectl apply -f x'):
            with self.subTest(cmd):
                self.assertNotIn("infra.apply-ungated", infra_rules(cmd))

    def test_a_heredoc_with_an_apostrophe_does_not_hide_what_follows(self):
        for body, tail, label in (("don't forget", "terraform -chdir='a b' apply -auto-approve", "terraform apply"), ("don't forget", "cdk -c a='x y' deploy", "cdk deploy"),
                                  ('say "hi', 'cdk -c a="x y" deploy', "cdk deploy"), ("use `x", "cdk -c a=`echo x y` deploy", "cdk deploy"),
                                  ("don't", "cdk -c a='x y' deploy", "cdk deploy"), ("don't won't", "cdk -c a='x y' deploy", "cdk deploy"),
                                  ("$( $( $( $( $( $( $( $(", "cdk -c a='x y' deploy", "cdk deploy"), ("' ' ' ' ' ' ' '", "cdk -c a='x y' deploy", "cdk deploy"),
                                  ("${{ ${{ ${{ ${{ ${{ ${{ ${{", "cdk -c a='x y' deploy", "cdk deploy")):
            cmd = "cat <<'EOF' > n.txt\n%s\nEOF\n%s" % (body, tail)
            with self.subTest(cmd[:40]):
                self.assertHit(cmd, label)

    def test_a_stray_apostrophe_on_another_line_does_not_pair_with_a_real_quote(self):
        for pre in ("echo don't", "# don't", "echo it\\'s", "echo x # don't", 'echo "it\'s"', "echo $'it\\'s'", "case $x in a) echo 1;; esac"):
            with self.subTest(pre):
                self.assertHit(pre + "\ncdk -c a='x y' deploy", "cdk deploy")

    def test_ansi_c_strings_with_an_escaped_quote_are_one_word(self):
        self.assertHit("cdk -c $'a\\'b c' deploy", "cdk deploy")
        self.assertHit("cdk -c $'a b' deploy", "cdk deploy")
        self.assertIn("terraform apply", workflows.deploy_labels("terraform -var $'a\\'b c' apply"))
        self.assertTrue(workflows._blank_spans("x $'a\\'b c' y"))

    def test_a_value_over_several_lines_is_still_one_word(self):
        self.assertHit("cdk -c 'a\nb' deploy", "cdk deploy")
        self.assertHit('cdk -c "a\n  b" deploy', "cdk deploy")

    def test_a_pad_of_quotes_does_not_hide_the_tail(self):
        for pad in ("''" * 2000, '""' * 2000, "\\a" * 2000, "``" * 2000):
            for tail in ("cdk -c a='x y' deploy", 'cdk -c a="x y" deploy', "cdk --profile ${{ matrix.p }} deploy"):
                with self.subTest(pad[:4] + tail[:12]):
                    self.assertHit("echo " + pad + "\n" + tail, "cdk deploy")


class BareVariablesAsFlags(_BothReaders, unittest.TestCase):
    """D3: `$FLAGS`, `${FLAGS}`, `"$@"`, `"${ARGS[@]}"` and `${{ inputs.flags }}` stand for any number of flags in front of the verb."""

    def test_the_reviewers_inputs(self):
        for cmd, label in (("terraform $TF_FLAGS apply", "terraform apply"), ("cdk $CDK_FLAGS deploy", "cdk deploy"), ('cdk "$@" deploy', "cdk deploy"),
                           ("cdk ${{ inputs.flags }} deploy", "cdk deploy"), ("kubectl $KARGS apply -f x", "kubectl apply")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_other_shapes(self):
        for cmd, label in (("cdk ${FLAGS} deploy", "cdk deploy"), ('cdk "${ARGS[@]}" deploy', "cdk deploy"), ("cdk ${ARGS[@]} deploy", "cdk deploy"),
                           ("cdk $A $B -c x=y $C deploy", "cdk deploy"), ("helm $H upgrade x y", "helm upgrade"), ("terraform $A $B apply tfplan", "terraform apply"),
                           ('terraform "$@" apply', "terraform apply"), ("cdk --profile p $F deploy", "cdk deploy"), ("cdk $* deploy", "cdk deploy"),
                           ("pulumi $P up", "pulumi up"), ("sam $S deploy", "sam deploy")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_variable_is_never_the_value_of_the_flag_before_it(self):
        # one parse per token: a flag, then a variable word, then the verb
        self.assertHit("cdk -c $A -c $B -c $C deploy", "cdk deploy")
        self.assertHit("kubectl -n $NS --context $CTX apply -f x", "kubectl apply")

    def test_a_variable_with_no_verb_after_it_stays_quiet(self):
        for cmd, label in (("cdk $CDK_FLAGS synth", "cdk deploy"), ("terraform $TF_FLAGS plan", "terraform apply"), ("kubectl $KARGS get pods", "kubectl apply"),
                           ("echo $A deploy", "cdk deploy"), ("cdk $F", "cdk deploy")):
            with self.subTest(cmd):
                self.assertQuiet(cmd, label)

    def test_terraform_flags_before_the_verb_are_read_as_the_doctor_reads_them(self):
        calls = ci_rules._tf_calls("terraform -no-color $F apply tfplan")
        self.assertEqual([(t, s) for t, s, _ in calls], [("terraform", "apply")])
        self.assertEqual(calls[0][2], ["tfplan"])


class GlobalFlagsOfCloudClis(_BothReaders, unittest.TestCase):
    """D4: `aws --region x cloudformation deploy`, `gcloud --project p run deploy`, `az --subscription s webapp deploy`: the flags before the service name
    are read with the same grammar as the flags before a verb."""

    def test_cloudformation_after_global_flags(self):
        for cmd in ("aws --region us-east-1 cloudformation deploy --stack-name x --template-file t.yml", "aws --profile prod cloudformation update-stack --stack-name x",
                    "aws --no-cli-pager cloudformation deploy --stack-name y", "aws --region x --profile y cloudformation delete-stack --stack-name z",
                    "aws --region ${{ vars.R }} cloudformation deploy --stack-name x", "aws $AWS_ARGS cloudformation deploy --stack-name x",
                    'aws --region "us east" cloudformation deploy --stack-name x'):
            with self.subTest(cmd):
                self.assertHit(cmd, "cloudformation")

    def test_cloudformation_without_flags_and_other_calls_are_unchanged(self):
        self.assertHit("aws cloudformation deploy --stack-name x", "cloudformation")
        self.assertHit("aws cloudformation deploy --region x --stack-name y", "cloudformation")
        for cmd in ("aws cloudformation package --template-file x", "aws --region x cloudformation describe-stacks", "aws s3 ls",
                    "x-aws cloudformation deploy --stack-name x", "aws-vault exec prod -- cdk synth"):
            with self.subTest(cmd):
                self.assertQuiet(cmd, "cloudformation")

    def test_the_doctor_reads_aws_gcloud_and_az_after_global_flags(self):
        for cmd, label in (("aws --region eu-west-1 ecs update-service --service s", "aws deploy"), ("aws --region x lambda update-function-code --function-name f", "aws deploy"),
                           ("aws --region x s3 sync build s3://bucket", "aws deploy"), ("gcloud --project p run deploy svc --image i", "gcloud deploy"),
                           ("gcloud --quiet run deploy svc", "gcloud deploy"), ("gcloud run deploy svc", "gcloud deploy"),
                           ("az --subscription s webapp deploy --name x", "az deploy"), ("az --only-show-errors webapp deploy", "az deploy"),
                           ("az webapp up", "az deploy")):
            with self.subTest(cmd):
                self.assertTrue(any(label in l for l in workflows.deploy_labels(cmd)), workflows.deploy_labels(cmd))

    def test_a_read_only_call_with_the_same_flags_is_quiet(self):
        for cmd in ("aws --region x ecs describe-services --services s", "gcloud --project p run services list", "az --subscription s webapp show --name x",
                    "aws --region x s3 ls", "gcloud --quiet config set project p"):
            with self.subTest(cmd):
                self.assertEqual([l for l in workflows.deploy_labels(cmd) if l in ("aws deploy", "gcloud deploy", "az deploy", "cloudformation deploy")], [])


class QuotesInsideWordsAndVariableCommands(_BothReaders, unittest.TestCase):
    """D5: a quote inside a word does nothing in the shell, so it is dropped before matching; a variable in command position in front of a deploy verb is
    deploy-capable."""

    def test_quotes_inside_the_tool_or_the_verb(self):
        for cmd, label in (("terra'form' apply", "terraform apply"), ('terraform ap"ply"', "terraform apply"), ("te'rra'form apply", "terraform apply"),
                           ('"terra"form apply', "terraform apply"), ("'t'e'r'r'a'f'o'r'm' apply", "terraform apply"), ('kube"ctl" apply -f x', "kubectl apply"),
                           ("cdk dep'loy'", "cdk deploy"), ('cd"k" deploy', "cdk deploy"), ("helm up\"grade\" x y", "helm upgrade")):
            with self.subTest(cmd):
                self.assertHit(cmd, label)

    def test_a_quote_that_is_not_inside_a_word_is_left_alone(self):
        for cmd, label in (("echo \"it's\" deploy", "cdk deploy"), ("echo 'a' 'b' cdk", "cdk deploy"), ("cdk synth 'deploy'", "cdk deploy")):
            with self.subTest(cmd):
                self.assertQuiet(cmd, label)

    def test_a_quoted_variable_after_an_assignment_is_the_tool(self):
        self.assertHit('T="terraform"\n"$T" apply', "terraform apply")
        self.assertHit("T=terraform\n$T apply", "terraform apply")
        self.assertHit("T='terraform'\n\"${T}\" apply", "terraform apply")

    def test_a_variable_in_command_position_is_deploy_capable(self):
        for cmd in ('"$T" apply', '"$CDK" deploy --all', "$TOOL destroy", "${RUNNER} apply -auto-approve", "FOO=1 $TOOL apply", "cd x && $TOOL deploy"):
            with self.subTest(cmd):
                self.assertIn("deploy through a variable", workflows.deploy_labels(cmd))
                self.assertTrue(ci_rules._INDIRECT_RE.search(ci_rules._norm_run(cmd)))      # the lint's own rule (an error in a credentialed job)

    def test_a_variable_that_is_not_in_command_position_or_has_no_verb_is_not(self):
        for cmd in ("echo $T apply", "$T", '"$T" --version', "$NPM run deploy", "x=$T apply", "echo hi # $T apply"):
            with self.subTest(cmd):
                self.assertNotIn("deploy through a variable", workflows.deploy_labels(cmd))


class DelimiterLimitAndNul(unittest.TestCase):
    """D6: a script with more delimiters than the span pass reads is unreadable (an error), not read as written; a NUL is a separator."""

    def test_nul_is_read_both_as_a_separator_and_as_dropped(self):
        for cmd in ("terra\x00form apply", "terraform\x00 apply", "terraform \x00 apply", "terraform\x00apply", "cdk\x00deploy", "kubectl \x00 --context x\x00 apply"):
            with self.subTest(repr(cmd)):
                self.assertTrue(workflows.deploy_labels(cmd), repr(cmd))
        self.assertIn("infra.apply-ungated", infra_rules("echo first\nterra\x00form apply"))
        self.assertIn("infra.apply-ungated", infra_rules("echo first\nterraform\x00 apply"))

    def test_a_nul_in_a_yaml_string_reaches_the_lint(self):
        for run in ('"echo first\\nterra\\0form apply"', '"echo first\\nterraform\\0 apply"'):
            text = "name: x\non:\n  workflow_dispatch:\npermissions:\n  contents: read\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: %s\n" % run
            with self.subTest(run):
                self.assertIn("infra.apply-ungated", rules_of(lint(text)))

    def test_the_separator_the_text_adds_is_not_one_the_text_can_make(self):
        out = workflows.normalize_shell("a\x00b")
        self.assertEqual(out.count("\n\x00\n"), 1)

    def test_the_limit_is_an_exception_with_a_name(self):
        self.assertTrue(issubclass(workflows.ScanLimit, Exception))
        with self.assertRaises(workflows.ScanLimit):
            workflows.normalize_shell.__wrapped__("'a b' " * 120000)

    def test_the_old_probe_pad_that_hid_the_tail_is_now_an_unreadable_file(self):
        # 30,000 `$(x)` on one line used to make the pass give up silently and read the rest as written
        run = "echo " + "$(x)" * 31000 + "\ncdk -c a='x y' deploy"
        fs = lint(DISPATCH_WF % "\n".join("          " + l for l in run.split("\n")))
        self.assertEqual(rules_of(fs), ["ci.unreadable"])
        self.assertEqual(workflows.deploy_labels(run), [workflows.UNREADABLE_LABEL])


class HostileInputUnderTwoSeconds(unittest.TestCase):
    """A 200 KB hostile script costs the whole lint and the doctor's reader under two seconds (an unreadable script is quick)."""

    SHAPES = (lambda n: "$(" * (n // 2), lambda n: '"a b" cdk -c a="x y" ' * (n // 22),
              lambda n: "cat <<'EOF'\n" + "don't\n" * (n // 6) + "EOF\ncdk -c a='x y' deploy", lambda n: "cdk $A $B $C " * (n // 13) + "deploy")

    def test_each_shape(self):
        for make in self.SHAPES:
            run = make(200000)
            t = time.time()
            ci_lint.lint_text(DISPATCH_WF % "\n".join("          " + l for l in run.split("\n")), "w.yml")
            workflows.deploy_labels(run)
            workflows.scan_text(run)
            self.assertLess(time.time() - t, 2.0, run[:30])

    def test_the_comment_cutter_alone(self):
        for run in ("'" * 200000, '"$(' * 66000, "# " * 100000, "a #" * 60000, "\\" * 200000, "${{ " * 50000 + "#"):
            t = time.time()
            workflows.strip_comments(run)
            self.assertLess(time.time() - t, 2.0, run[:10])


if __name__ == "__main__":
    unittest.main()
