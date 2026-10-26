"""pr_prepare.py: body validation, git checks, H3 paths, CI-fix, reverts, labels, and the commands it prints."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import pr_prepare

from . import FeatureCase, GOOD_REPORT, good_body
from .. import REPO_ROOT

SCRIPT = os.path.join(REPO_ROOT, ".claude", "scripts", "pr_prepare.py")


def ids(res, severity=None):
    return [f["id"] for f in res["findings"] if severity is None or f["severity"] == severity]


class BodyText(unittest.TestCase):
    """validate_body is pure text: no git needed."""

    HEAD = "a" * 40
    BASE = "b" * 40

    def body(self, **kw):
        return good_body(self.HEAD, self.BASE, **kw)

    def errors(self, text, lane=None):
        return [f.id for f in pr_prepare.validate_body(text, lane) if f.severity == "error"]

    def test_a_complete_body_has_no_findings(self):
        self.assertEqual(pr_prepare.validate_body(self.body()), [])

    def test_template_markers_are_errors_but_a_comment_mentioning_them_is_not(self):
        text = self.body().replace("Adds the x endpoint so clients can list x.", "<fill: summary>")
        self.assertIn("audit.placeholder", self.errors(text))
        # a comment can mention the marker; but only the kit's own comments are allowed at all (see Disguise)
        self.assertEqual(self.errors(self.body().replace("<!-- kit:pr-body v1 -->", "<!-- kit:pr-body v1: remove every <fill: x> -->")), [])

    def test_every_audit_field_is_required_and_non_empty(self):
        for spec in pr_prepare.FIELDS:
            with self.subTest(spec.name):
                lines = self.body().split("\n")
                gone = [ln for ln in lines if not ln.startswith("| %s |" % spec.name)]
                self.assertIn("audit.field-missing", self.errors("\n".join(gone)))
                blank = ["| %s | |" % spec.name if ln.startswith("| %s |" % spec.name) else ln for ln in lines]
                self.assertIn("audit.empty-field", self.errors("\n".join(blank)))

    def test_field_values_are_checked(self):
        cases = [
            ("| Lane | full |", "| Lane | maybe |"),
            ("| Delivery | draft-pr |", "| Delivery | yolo |"),
            ("| Head sha | " + self.HEAD + " |", "| Head sha | abc123 |"),
            ("| Branch | feat/x |", "| Branch | -rf |"),
            ("| Base | main |", "| Base | a..b |"),
            ("| Waivers | NONE |", "| Waivers | |"),
            ("| Feature | x |", "| Feature | Has Spaces |"),
        ]
        for old, new in cases:
            with self.subTest(new):
                self.assertTrue(self.errors(self.body().replace(old, new)), new)

    def test_none_is_valid_only_where_the_field_allows_it(self):
        self.assertEqual(self.errors(self.body().replace("| Verify (G6) | PASS", "| Verify (G6) | NONE")), ["audit.bad-value", "audit.verify-not-pass"])
        self.assertEqual(self.errors(self.body().replace("| Design critic | DESIGN_APPROVED (round 1) |", "| Design critic | NONE |")), ["audit.bad-value"])

    def test_verify_must_pass(self):
        self.assertIn("audit.verify-not-pass", self.errors(self.body(verify="FAIL (tests)")))

    def test_security_not_run_is_a_warning(self):
        text = self.body().replace("N/A (nothing sensitive)", "NOT RUN (no time)")
        found = pr_prepare.validate_body(text)
        self.assertEqual([(f.id, f.severity) for f in found], [("audit.security-not-run", "warn")])

    def test_engineers_must_be_approved_and_reviewed(self):
        base = self.body()
        self.assertIn("audit.engineer-not-approved", self.errors(base.replace("| backend | APPROVED |", "| backend | PARTIAL |")))
        self.assertIn("audit.review-not-approved", self.errors(base.replace("CODE_REVIEW_APPROVED", "CHANGES_REQUESTED")))
        self.assertIn("audit.empty-field", self.errors(base.replace("| abc1234 |", "| |")))
        self.assertEqual(self.errors(base.replace("CODE_REVIEW_APPROVED", "SKIPPED (trivial change)")), [])
        self.assertIn("audit.no-engineers", self.errors(self.body(engineers=[])))

    def test_every_criterion_needs_evidence_and_pass(self):
        base = self.body()
        self.assertIn("audit.criterion-not-pass", self.errors(base.replace("| PASS |\n\n## Doc", "| FAIL |\n\n## Doc")))
        self.assertIn("audit.empty-field", self.errors(base.replace("| tests.test_x::test_list |", "| |")))
        self.assertIn("audit.no-criteria", self.errors(base.split("| Criterion")[0] + "## Doc Delta\n\nNONE\nchecked: a\n\n## Primer Delta\n\nNONE\nchecked: b\n\n## Notes\n\nNONE\n"))

    def test_none_deltas_need_a_checked_line(self):
        self.assertIn("audit.none-without-checked", self.errors(self.body(doc="NONE")))
        self.assertIn("audit.none-without-checked", self.errors(self.body(primer="NONE")))
        self.assertEqual(self.errors(self.body(doc="- README: document the /x route")), [])

    def test_secrets_title_and_size(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        self.assertIn("audit.secret", self.errors(self.body().replace("NONE\n", "key " + secret + "\n", 1)))
        self.assertIn("audit.title", self.errors(self.body(title="x" * 80)))
        self.assertIn("audit.title", self.errors(self.body(title="-rf things")))
        self.assertIn("audit.too-long", self.errors(self.body() + "x" * 61000))

    def test_missing_markers_and_sections(self):
        self.assertIn("audit.markers", self.errors(self.body().replace("<!-- audit:end -->", "")))
        self.assertIn("audit.section-missing", self.errors(self.body().replace("## Notes", "## Other")))

    def test_the_template_lists_exactly_the_fields_in_the_code_in_order(self):
        with open(os.path.join(REPO_ROOT, ".claude", "templates", "pr-body.md")) as fh:
            text = fh.read()
        parsed = pr_prepare.parse_body(text)
        self.assertEqual(list(parsed.fields), [f.name for f in pr_prepare.FIELDS])
        for name in pr_prepare.REQUIRED_SECTIONS:
            self.assertIn(name, parsed.sections)
        found = pr_prepare.validate_body(text)
        self.assertTrue(found)
        self.assertEqual(set(f.id for f in found), {"audit.placeholder"})

    def test_parse_report(self):
        rep = pr_prepare.parse_report(GOOD_REPORT)
        self.assertEqual((rep["status"], rep["iteration"], rep["missing"], rep["problems"]), ("APPROVED", "1", [], []))
        bad = pr_prepare.parse_report(GOOD_REPORT.replace("## Doc Delta\nNONE\nchecked: README, env vars\n\n", "").replace("APPROVED", "DONE"))
        self.assertIn("Doc Delta", bad["missing"])
        self.assertTrue(bad["problems"])
        self.assertTrue(pr_prepare.parse_report(GOOD_REPORT.replace("checked: layering", ""))["problems"])


class Disguise(unittest.TestCase):
    """What the checker reads must be what a reviewer sees on GitHub: comments, code fences and indentation cannot hide a table."""

    HEAD, BASE = "a" * 40, "b" * 40

    def body(self, **kw):
        return good_body(self.HEAD, self.BASE, **kw)

    def errors(self, text):
        return [f.id for f in pr_prepare.validate_body(text) if f.severity == "error"]

    def test_a_hidden_comment_is_refused(self):
        text = self.body(verify="FAIL (tests)").replace("# Add the x endpoint", "# Add the x endpoint\n\n<!-- looks fine to the checker -->")
        self.assertIn("audit.hidden-comment", self.errors(text))
        fake = "<!-- audit:begin v1 -->\n| Field | Value |\n|---|---|\n| Verify (G6) | PASS |\n<!-- audit:end -->\n"
        self.assertIn("audit.markers", self.errors(fake + self.body()))

    def test_a_fake_table_in_a_code_fence_is_not_read(self):
        good = self.body()
        real_bad = self.body(verify="FAIL (tests)")
        fake = "\n```\n" + good.split("<!-- audit:begin v1 -->")[1].split("<!-- audit:end -->")[0] + "```\n"
        for text in (real_bad + fake, fake + real_bad):
            with self.subTest(text[:20]):
                self.assertIn("audit.verify-not-pass", self.errors(text))
        fenced_markers = good.replace("<!-- audit:begin v1 -->", "```\n<!-- audit:begin v1 -->").replace("<!-- audit:end -->", "<!-- audit:end -->\n```")
        self.assertIn("audit.markers", self.errors(fenced_markers))

    def test_an_unclosed_fence_swallows_the_rest_and_fails_closed(self):
        self.assertTrue(self.errors("```\n" + self.body()))

    def test_duplicate_fields_are_refused(self):
        text = self.body().replace("| Waivers | NONE |", "| Waivers | NONE |\n| Waivers | NONE |")
        self.assertIn("audit.duplicate-field", self.errors(text))

    def test_indented_or_quoted_tables_are_not_read(self):
        indented = "\n".join(("    " + ln if ln.startswith("|") else ln) for ln in self.body().split("\n"))
        self.assertIn("audit.field-missing", self.errors(indented))
        quoted = "\n".join(("> " + ln if ln.startswith("|") else ln) for ln in self.body().split("\n"))
        self.assertIn("audit.field-missing", self.errors(quoted))

    def test_two_audit_blocks_are_refused(self):
        b = self.body()
        block = b[b.index("<!-- audit:begin v1 -->"):b.index("<!-- audit:end -->") + len("<!-- audit:end -->")]
        self.assertIn("audit.markers", self.errors(b + "\n" + block))

    def test_a_second_engineers_table_is_checked_too(self):
        extra = "\n| Engineer | Status | Iterations | Code review | Report | Commits |\n|---|---|---|---|---|---|\n| app | PARTIAL | 1 | CODE_REVIEW_APPROVED | inline | abc1234 |\n"
        text = self.body().replace("\n## Acceptance matrix", extra + "\n## Acceptance matrix")
        self.assertIn("audit.engineer-not-approved", self.errors(text))


class Paths(unittest.TestCase):
    def test_h3_classification(self):
        paths = [".github/workflows/ci.yml", ".claude/settings.json", "db/migrate/001.rb", "app/migrations/0002.py", "package.json",
                 "web/pnpm-lock.yaml", "src/auth/login.ts", "Dockerfile", "README.md", "src/app.py", "infra/main.tf", "conftest.py",
                 "services/api/conftest.py", "serverless.yml", ".env.local"]
        got = {g["path"] for g in pr_prepare.classify_paths(paths)}
        self.assertEqual(got, {".github/workflows/ci.yml", ".claude/settings.json", "db/migrate/001.rb", "app/migrations/0002.py",
                               "package.json", "web/pnpm-lock.yaml", "Dockerfile", "infra/main.tf", "conftest.py", "services/api/conftest.py",
                               "serverless.yml", ".env.local"})
        got = {g["path"] for g in pr_prepare.classify_paths(paths, ["src/auth/**"])}
        self.assertIn("src/auth/login.ts", got)
        self.assertNotIn("README.md", got)

    def test_ci_fix_forbidden_paths(self):
        bad = ["tests/test_a.py", "src/a.test.ts", ".github/workflows/ci.yml", "package-lock.json", "e2e/login.spec.ts", "conftest.py",
               "tests/acceptance/x.py", "src/__snapshots__/a.snap"]
        self.assertEqual({p for p, _ in pr_prepare.ci_fix_violations(bad)}, set(bad))
        self.assertEqual(pr_prepare.ci_fix_violations(["src/app.py", "README.md"]), [])
        self.assertEqual({p for p, _ in pr_prepare.ci_fix_violations(["package-lock.json", "src/a.test.ts"], allow_lockfiles=True)}, {"src/a.test.ts"})

    def test_glob_semantics(self):
        rx = pr_prepare.glob_to_regex
        self.assertTrue(rx("src/auth/**").match("src/auth/a/b.ts"))
        self.assertFalse(rx("src/auth/**").match("src/authx/b.ts"))
        self.assertTrue(rx("*.lock").match("deep/dir/x.lock"))
        self.assertFalse(rx("src/*.py").match("src/a/b.py"))
        self.assertTrue(rx("**/migrations/**").match("migrations/001.sql"))


class Trailers(unittest.TestCase):
    def test_parse(self):
        msg = "fix(x): ci-fix attempt 1\n\nsome prose\n\nCI-Fix-Of: abc1234\nFeature: x\n"
        self.assertEqual(pr_prepare.parse_trailers(msg), {"ci-fix-of": ["abc1234"], "feature": ["x"]})
        self.assertEqual(pr_prepare.parse_trailers("subject only"), {})
        self.assertEqual(pr_prepare.parse_trailers("subject\n\nprose line: not a trailer block\nmore prose"), {})

    def test_reverts_from_recorded_prs(self):
        prs = [{"number": 7, "title": "Add x", "body": ""}, {"number": 9, "title": "Revert \"Add x\"", "body": "Reverts #7"},
               {"number": 10, "title": "Revert something else", "body": "Reverts #8"}, {"number": 11, "title": "revert: x (#7)", "body": None},
               {"number": 12, "title": "Add y", "body": "mentions #7"}]
        self.assertEqual(pr_prepare.reverts_from_prs(prs, 7), [9, 11])
        self.assertEqual(pr_prepare.reverts_from_prs(prs, 70), [])


class Checks(FeatureCase):
    def check(self, r, mode="check", **kw):
        return pr_prepare.check(r.path, mode=mode, **kw)

    def test_a_clean_branch_prepares_the_draft_pr_commands(self):
        r = self.make_feature()
        self.ready(r)
        res = self.check(r, mode="prepare")
        self.assertTrue(res["ok"], res["findings"])
        self.assertEqual(res["schema"], "pr_prepare.v1")
        self.assertEqual((res["branch"], res["base"], res["delivery"], res["ahead"]), ("feat/x", "main", "draft-pr", 1))
        cmds = res["commands"]
        self.assertTrue(cmds[0].startswith("# Run these yourself"))
        self.assertIn("git push --set-upstream origin feat/x", cmds)
        create = [c for c in cmds if c.startswith("gh pr create")][0]
        self.assertIn("--draft", create)
        self.assertIn("--base main --head feat/x", create)
        self.assertIn("--title 'Add the x endpoint'", create)
        self.assertIn("--body-file", create)
        self.assertIn(os.path.join(r.path, ".claude", "pr", "x.md"), create)
        text = "\n".join(cmds)
        for forbidden in ("--force", "-f ", "--no-verify", "--delete", " +refs"):
            self.assertNotIn(forbidden, text)

    def test_check_mode_prints_no_commands(self):
        r = self.make_feature()
        self.ready(r)
        res = self.check(r)
        self.assertTrue(res["ok"])
        self.assertEqual(res["commands"], [])

    def test_delivery_local_prints_a_local_merge_only(self):
        r = self.make_feature({"delivery": "local"})
        self.ready(r, delivery="local")
        res = self.check(r, mode="prepare")
        self.assertTrue(res["ok"], res["findings"])
        text = "\n".join(res["commands"])
        self.assertIn("git switch main", text)
        self.assertIn("git merge --no-ff feat/x", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("gh ", text)

    def test_push_branch_prints_only_the_push(self):
        r = self.make_feature({"delivery": "push-branch"})
        self.ready(r, delivery="push-branch")
        text = "\n".join(self.check(r, mode="prepare")["commands"])
        self.assertIn("git push --set-upstream origin feat/x", text)
        self.assertNotIn("gh pr", text)

    def test_delivery_override_must_match_the_body(self):
        r = self.make_feature()
        self.ready(r)
        res = self.check(r, delivery="local")
        self.assertIn("audit.mismatch", ids(res, "error"))

    def test_an_existing_remote_branch_gets_a_plain_push_and_pr_edit(self):
        r = self.make_feature()
        self.ready(r)
        r.git("update-ref", "refs/remotes/origin/feat/x", r.head())
        res = self.check(r, mode="prepare", pr_number=12)
        self.assertTrue(res["ok"], res["findings"])
        text = "\n".join(res["commands"])
        self.assertIn("git push origin feat/x", text)
        self.assertNotIn("--set-upstream", text)
        self.assertIn("gh pr edit 12", text)
        self.assertNotIn("gh pr create", text)

    def test_a_diverged_remote_branch_is_refused_and_never_forced(self):
        r = self.make_feature()
        self.ready(r)
        r.git("switch", "-q", "main")
        other = self.commit(r, {"other.txt": "x"}, "someone else's work")
        r.git("switch", "-q", "feat/x")
        r.git("update-ref", "refs/remotes/origin/feat/x", other)
        res = self.check(r, mode="prepare")
        self.assertIn("git.diverged", ids(res, "error"))
        self.assertEqual(res["commands"], [])
        r.git("update-ref", "refs/remotes/origin/feat/x", self.base)  # an ancestor of the branch is fine
        self.assertNotIn("git.diverged", ids(self.check(r)))

    def test_a_stale_head_sha_is_an_error(self):
        r = self.make_feature()
        self.ready(r)
        self.commit(r, {"src/more.py": "x = 1\n"}, "wip(x): backend iteration 2\n\nFeature: x\n")
        res = self.check(r, mode="prepare")
        self.assertIn("audit.stale-head", ids(res, "error"))
        self.assertIn("verify.stale", ids(res, "error"))
        self.assertEqual(res["commands"], [])

    def test_an_uncommitted_change_is_an_error_but_the_body_and_untracked_feature_files_are_not(self):
        r = self.make_feature()
        self.ready(r)
        r.write("docs/features/x/briefs/backend.md", "brief")  # untracked feature folder: allowed
        self.assertNotIn("git.dirty", ids(self.check(r)))
        r.write("src/app.py", "def handler():\n    return 201\n")
        self.assertIn("git.dirty", ids(self.check(r), ))
        r.git("checkout", "--", "src/app.py")
        r.write("scratch.txt", "x")
        self.assertIn("git.dirty", ids(self.check(r)))

    def test_protected_and_oddly_named_branches(self):
        r = self.make_feature()
        self.ready(r)
        r.git("switch", "-q", "main")
        res = self.check(r)
        self.assertIn("git.protected-branch", ids(res, "error"))
        self.assertIn("git.nothing-to-deliver", ids(res, "error"))
        r.git("switch", "-q", "feat/x")
        res = self.check(r, branch="--upload-pack=x")
        self.assertIn("git.bad-branch", ids(res, "error"))

    def test_not_checked_out(self):
        r = self.make_feature()
        self.ready(r)
        r.git("switch", "-q", "main")
        res = self.check(r, branch="feat/x")
        self.assertIn("git.not-checked-out", ids(res, "error"))

    def test_missing_body_and_missing_template_markers(self):
        r = self.make_feature()
        self.write_verify(r)
        self.assertIn("audit.no-body", ids(self.check(r), "error"))

    def test_wip_commit_without_trailer_warns(self):
        r = self.make_feature()
        self.ready(r)
        self.commit(r, {"src/b.py": "b = 1\n"}, "wip(x): app iteration 1")
        self.ready(r)
        res = self.check(r)
        self.assertIn("git.trailer-missing", ids(res, "warn"))

    def test_revert_inside_the_branch_warns(self):
        r = self.make_feature()
        b = self.commit(r, {"src/b.py": "b\n"}, "wip(x): add b\n\nFeature: x\n")
        self.commit(r, {"src/b.py": ""}, "Revert \"add b\"\n\nThis reverts commit %s.\n\nFeature: x\n" % b)
        self.ready(r)
        res = self.check(r)
        self.assertIn("git.revert-in-branch", ids(res, "warn"))

    def test_find_reverts_in_history(self):
        r = self.make_feature()
        merge = self.commit(r, {"m.txt": "m"}, "feat: m")
        rev = self.commit(r, {"m.txt": ""}, "Revert \"feat: m\"\n\nThis reverts commit %s.\n" % merge)
        self.assertEqual(pr_prepare.find_reverts(r.path, [merge, "f" * 40]), {merge: [rev]})
        self.assertEqual(pr_prepare.find_reverts(r.path, ["nothex"]), {})

    def test_labels(self):
        r = self.make_feature()
        self.ready(r)
        for lab in ("factory:go", "factory:feature", "plan-ok", "preview"):
            with self.subTest(lab):
                res = self.check(r, mode="prepare", labels=[lab])
                self.assertIn("label.approver-only", ids(res, "error"))
                self.assertEqual(res["commands"], [])
        self.assertIn("label.bad", ids(self.check(r, labels=["a;b"]), "error"))
        res = self.check(r, mode="prepare", labels=["needs-docs"])
        self.assertTrue(res["ok"])
        self.assertIn("--label needs-docs", [c for c in res["commands"] if c.startswith("gh pr create")][0])


class Reports(FeatureCase):
    def test_a_report_that_is_not_approved_blocks(self):
        r = self.make_feature(report=GOOD_REPORT.replace("APPROVED", "PARTIAL", 1))
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertIn("report.not-approved", ids(res, "error"))

    def test_a_malformed_report_blocks(self):
        r = self.make_feature(report=GOOD_REPORT.replace("## Doc Delta\nNONE\nchecked: README, env vars\n\n", ""))
        self.ready(r)
        self.assertIn("report.malformed", ids(pr_prepare.check(r.path), "error"))

    def test_no_reports_blocks_the_full_lane_but_not_the_quick_lane(self):
        r = self.make_feature(reports=[])
        self.ready(r, lane="quick")
        res = pr_prepare.check(r.path)
        self.assertNotIn("report.none", ids(res))
        self.write_body(r, good_body(r.head(), self.base, lane="full"))
        self.assertIn("report.none", ids(pr_prepare.check(r.path), "error"))

    def test_every_report_must_appear_in_the_engineers_table(self):
        r = self.make_feature(reports=["backend", "database"])
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertIn("audit.engineer-missing", ids(res, "error"))
        self.write_body(r, good_body(r.head(), self.base, engineers=["backend", "database"]))
        self.assertNotIn("audit.engineer-missing", ids(pr_prepare.check(r.path)))

    def test_a_delta_in_a_report_cannot_be_dropped_from_the_body(self):
        rep = GOOD_REPORT.replace("## Doc Delta\nNONE\nchecked: README, env vars", "## Doc Delta\nREADME: document the new env var X")
        r = self.make_feature(report=rep)
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertIn("audit.delta-dropped", ids(res, "error"))
        self.ready(r, doc="- backend: README: document the new env var X")
        self.assertNotIn("audit.delta-dropped", ids(pr_prepare.check(r.path)))


class HumanGatedPaths(FeatureCase):
    def test_none_is_refused_over_a_diff_that_touches_a_gated_path(self):
        r = self.make_feature(files={"db/migrations/001.sql": "create table x();", ".github/workflows/ci.yml": "name: ci\n"})
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertIn("audit.h3-mismatch", ids(res, "error"))
        self.assertEqual({g["path"] for g in res["human_gated"]}, {"db/migrations/001.sql", ".github/workflows/ci.yml"})

    def test_listing_every_path_satisfies_it_and_a_partial_list_does_not(self):
        r = self.make_feature(files={"db/migrations/001.sql": "create table x();", ".github/workflows/ci.yml": "name: ci\n"})
        self.ready(r, human="db/migrations/001.sql (migration); .github/workflows/ci.yml (CI)")
        self.assertNotIn("audit.h3-mismatch", ids(pr_prepare.check(r.path)))
        self.assertNotIn("audit.h3-unlisted", ids(pr_prepare.check(r.path)))
        self.ready(r, human="db/migrations/001.sql (migration)")
        self.assertIn("audit.h3-unlisted", ids(pr_prepare.check(r.path), "error"))

    def test_project_paths_come_from_the_base_config(self):
        r = self.make_feature({"always_human_paths": ["src/auth/**"]}, files={"src/auth/login.py": "x = 1\n"})
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertIn("audit.h3-mismatch", ids(res, "error"))
        self.assertEqual([g["path"] for g in res["human_gated"]], ["src/auth/login.py"])

    def test_a_branch_cannot_clear_its_own_human_paths(self):
        r = self.make_feature({"always_human_paths": ["src/auth/**"]}, files={"src/auth/login.py": "x = 1\n"})
        self.commit(r, {".claude/loop.config.md": "# c\n\n```yaml\ndelivery: draft-pr\n```\n"}, "wip(x): drop the paths\n\nFeature: x\n")
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertIn("src/auth/login.py", [g["path"] for g in res["human_gated"]])

    def test_a_gated_file_moved_or_deleted_by_the_branch_is_still_gated(self):
        r = self.make_feature(base_files={"db/migrations/0.sql": "create table a();", "infra/net.tf": "# net\n"})
        r.git("mv", "db/migrations/0.sql", "docs/0.sql")
        r.git("rm", "-q", "infra/net.tf")
        r.git("commit", "-q", "-m", "wip(x): move and drop\n\nFeature: x\n")
        self.ready(r)
        res = pr_prepare.check(r.path)
        self.assertEqual({g["path"] for g in res["human_gated"]}, {"db/migrations/0.sql", "infra/net.tf"})
        self.assertIn("audit.h3-mismatch", ids(res, "error"))


class VerifyResult(FeatureCase):
    def check_with(self, **over):
        r = self.make_feature()
        self.write_body(r, good_body(r.head(), self.base))
        self.write_verify(r, **over)
        return pr_prepare.check(r.path)

    def test_a_good_result(self):
        res = self.check_with()
        self.assertTrue(res["ok"], res["findings"])

    def test_bad_results(self):
        cases = [
            ({"status": "fail", "ok": False}, "verify.not-pass"),
            ({"complete": False}, "verify.not-pass"),
            ({"head": "c" * 40}, "verify.stale"),
            ({"dirty": True}, "verify.dirty"),
            ({"trusted": False}, "verify.untrusted"),
        ]
        for over, fid in cases:
            with self.subTest(fid + str(over)):
                self.assertIn(fid, ids(self.check_with(**over), "error"))

    def test_no_result_is_a_warning_not_a_pass(self):
        r = self.make_feature()
        self.write_body(r, good_body(r.head(), self.base))
        res = pr_prepare.check(r.path)
        self.assertTrue(res["ok"])
        self.assertIn("verify.no-result", ids(res, "warn"))

    def test_an_unreadable_result_is_an_error(self):
        r = self.make_feature()
        self.write_body(r, good_body(r.head(), self.base))
        path = self.write_verify(r)
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIn("verify.unreadable", ids(pr_prepare.check(r.path), "error"))
        with open(path, "w") as fh:
            json.dump({"schema": "other"}, fh)
        self.assertIn("verify.unreadable", ids(pr_prepare.check(r.path), "error"))


class CiFix(FeatureCase):
    def fix(self, r, files, n=1, sha="abc1234"):
        return self.commit(r, files, "fix(x): ci-fix attempt %d\n\nCI-Fix-Of: %s\nFeature: x\n" % (n, sha))

    def test_within_the_cap_and_named_in_the_body(self):
        r = self.make_feature()
        self.fix(r, {"src/app.py": "def handler():\n    return 201\n"})
        self.ready(r, cifix="abc1234")
        res = pr_prepare.check(r.path)
        self.assertTrue(res["ok"], res["findings"])
        self.assertEqual(res["ci_fix"]["count"], 1)
        self.assertEqual(res["ci_fix"]["max"], 2)

    def test_the_cap_comes_from_the_base_config(self):
        r = self.make_feature({"max_ci_fix_attempts": 1})
        self.fix(r, {"src/a.py": "1\n"}, 1)
        self.fix(r, {"src/b.py": "2\n"}, 2)
        self.ready(r, cifix="abc1234")
        self.assertIn("ci-fix.cap", ids(pr_prepare.check(r.path), "error"))

    def test_a_branch_cannot_raise_its_own_cap(self):
        r = self.make_feature({"max_ci_fix_attempts": 1})
        self.commit(r, {".claude/loop.config.md": "# c\n\n```yaml\nmax_ci_fix_attempts: 9\ndelivery: draft-pr\n```\n"}, "wip(x): config\n\nFeature: x\n")
        self.fix(r, {"src/a.py": "1\n"}, 1)
        self.fix(r, {"src/b.py": "2\n"}, 2)
        self.ready(r, cifix="abc1234")
        res = pr_prepare.check(r.path)
        self.assertEqual(res["ci_fix"]["max"], 1)
        self.assertIn("ci-fix.cap", ids(res, "error"))

    def test_forbidden_paths(self):
        for path in (".github/workflows/ci.yml", "tests/test_a.py", "package-lock.json"):
            with self.subTest(path):
                r = self.make_feature()
                self.fix(r, {path: "changed\n"})
                self.ready(r, cifix="abc1234")
                self.assertIn("ci-fix.forbidden-path", ids(pr_prepare.check(r.path), "error"))

    def test_a_dependency_item_may_edit_its_lockfile(self):
        r = self.make_feature()
        self.fix(r, {"package-lock.json": "{}\n"})
        self.ready(r, cifix="abc1234", human="package-lock.json (lockfile)")
        self.assertNotIn("ci-fix.forbidden-path", ids(pr_prepare.check(r.path, dependency_item=True)))

    def test_the_body_must_name_the_ci_fix(self):
        r = self.make_feature()
        self.fix(r, {"src/app.py": "x = 2\n"})
        self.ready(r, cifix="NONE")
        self.assertIn("audit.cifix-mismatch", ids(pr_prepare.check(r.path), "error"))

    def test_a_malformed_trailer(self):
        r = self.make_feature()
        self.commit(r, {"src/app.py": "x = 3\n"}, "fix(x): ci-fix attempt 1\n\nCI-Fix-Of: not-a-sha\nFeature: x\n")
        self.ready(r, cifix="abc1234")
        self.assertIn("ci-fix.trailer", ids(pr_prepare.check(r.path), "error"))


class Init(FeatureCase):
    def test_init_fills_what_git_and_the_reports_know(self):
        r = self.make_feature()
        r.write("docs/features/x/requirements.md", "# Requirements: List the x things\n")
        self.write_verify(r)
        text, ctx = pr_prepare.init_body(r.path)
        parsed = pr_prepare.parse_body(text)
        self.assertEqual(parsed.title, "List the x things")
        f = parsed.fields
        self.assertEqual((f["Feature"], f["Lane"], f["Delivery"], f["Branch"], f["Base"]), ("x", "full", "draft-pr", "feat/x", "main"))
        self.assertEqual((f["Base sha"], f["Head sha"]), (self.base, r.head()))
        self.assertTrue(f["Verify (G6)"].startswith("PASS"))
        self.assertEqual(f["Human-gated paths"], "NONE")
        self.assertEqual(f["CI-Fix-Of"], "NONE")
        self.assertEqual(parsed.engineers[0]["Engineer"], "backend")
        self.assertEqual(parsed.engineers[0]["Status"], "APPROVED")
        self.assertEqual(parsed.engineers[0]["Report"], "docs/features/x/reports/backend.md")
        self.assertIn("NONE", parsed.sections["Doc Delta"])
        self.assertIn("checked: README, env vars", parsed.sections["Doc Delta"])
        left = {f.id for f in pr_prepare.validate_body(text)}
        self.assertEqual(left, {"audit.placeholder"})
        self.assertNotIn("<fill: feature slug>", text)

    def test_init_lists_human_gated_paths_and_ci_fix_shas(self):
        r = self.make_feature(files={"db/migrations/1.sql": "x"})
        self.commit(r, {"src/a.py": "1\n"}, "fix(x): ci-fix attempt 1\n\nCI-Fix-Of: abc1234\nFeature: x\n")
        text, _ = pr_prepare.init_body(r.path)
        f = pr_prepare.parse_body(text).fields
        self.assertIn("db/migrations/1.sql", f["Human-gated paths"])
        self.assertEqual(f["CI-Fix-Of"], "abc1234")

    def test_init_carries_report_deltas(self):
        rep = GOOD_REPORT.replace("## Primer Delta\nNONE\nchecked: layering", "## Primer Delta\nbackend.md: first background job; exemplar src/jobs/x.py")
        r = self.make_feature(report=rep)
        text, _ = pr_prepare.init_body(r.path)
        self.assertIn("first background job", pr_prepare.parse_body(text).sections["Primer Delta"])

    def test_a_completed_init_body_passes_the_check(self):
        r = self.make_feature()
        self.write_verify(r)
        text, ctx = pr_prepare.init_body(r.path, title="Add the x endpoint")
        filled = {
            "Security review": "N/A (nothing sensitive)", "Design critic": "DESIGN_APPROVED (round 1)", "Waivers": "NONE",
        }
        import re
        for k, v in filled.items():
            text = re.sub(r"^(\| %s \| )<fill:[^>]*>( \|)$" % re.escape(k), lambda m: m.group(1) + v + m.group(2), text, flags=re.M)
        text = text.replace("<fill: two or three sentences: what changed and why, written for a reviewer who has not seen the feature folder>", "Adds x.")
        text = text.replace("<fill: CODE_REVIEW_APPROVED, or SKIPPED (trivial change)>", "CODE_REVIEW_APPROVED")
        text = text.replace("<fill: short shas of the WIP commits>", r.head()[:10])
        text = text.replace("<fill: the acceptance criterion, copied from requirements.md>", "x works")
        text = text.replace("<fill: the test name, command or route that proves it>", "tests.test_x")
        text = text.replace("<fill: PASS>", "PASS")
        text = text.replace("<fill: NONE, or open questions, known gaps and anything the reviewer should look at first>", "NONE")
        self.assertEqual(re.findall(r"<fill:[^>]*>", re.sub(r"<!--.*?-->", "", text, flags=re.S)), [])
        self.write_body(r, text)
        res = pr_prepare.check(r.path, mode="prepare")
        self.assertTrue(res["ok"], res["findings"])

    def test_init_refuses_to_overwrite(self):
        r = self.make_feature()
        text, ctx = pr_prepare.init_body(r.path)
        pr_prepare.write_body(ctx.body_file, text)
        with self.assertRaises(pr_prepare.PrError):
            pr_prepare.write_body(ctx.body_file, text)
        pr_prepare.write_body(ctx.body_file, text, overwrite=True)


class Cli(FeatureCase):
    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              cwd=cwd, timeout=60)

    def test_init_check_prepare_flow_and_exit_codes(self):
        r = self.make_feature()
        out = self.run_cli("init", "--repo", r.path)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("still to fill", out.stdout)
        again = self.run_cli("init", "--repo", r.path)
        self.assertEqual(again.returncode, 2)
        self.assertIn("--overwrite", again.stderr)
        bad = self.run_cli("prepare", "--repo", r.path)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("NOT READY", bad.stdout)
        self.assertIn("No commands are printed", bad.stdout)
        self.assertNotIn("git push", bad.stdout)
        self.ready(r)
        good = self.run_cli("prepare", "--repo", r.path, "--json")
        self.assertEqual(good.returncode, 0, good.stdout)
        data = json.loads(good.stdout)
        self.assertTrue(data["ok"])
        self.assertTrue(any(c.startswith("git push --set-upstream") for c in data["commands"]))
        text = self.run_cli("prepare", "--repo", r.path)
        self.assertIn("READY", text.stdout)
        self.assertIn("git push --set-upstream origin feat/x", text.stdout)

    def test_usage_and_not_a_repo(self):
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(self.run_cli("check", "--repo", self.tmpdir()).returncode, 2)

    def test_reverts_subcommand(self):
        r = self.make_feature()
        merge = self.commit(r, {"m.txt": "m"}, "feat: m")
        self.commit(r, {"m.txt": ""}, "Revert\n\nThis reverts commit %s.\n" % merge)
        out = self.run_cli("reverts", "--repo", r.path, "--sha", merge, "--json")
        self.assertEqual(out.returncode, 1)
        self.assertIn(merge, json.loads(out.stdout)["commits"])
        prs = os.path.join(self.tmpdir(), "prs.json")
        with open(prs, "w") as fh:
            json.dump([{"number": 3, "title": "Revert x", "body": "Reverts #2"}], fh)
        out = self.run_cli("reverts", "--repo", r.path, "--prs-json", prs, "--pr", "2", "--json")
        self.assertEqual(json.loads(out.stdout)["prs"], [3])
        self.assertEqual(self.run_cli("reverts", "--repo", r.path, "--prs-json", prs).returncode, 2)


if __name__ == "__main__":
    unittest.main()
