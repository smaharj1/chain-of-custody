# Untrusted content and prompt injection

An instruction hidden in an issue, a pull request comment, a web page, a dependency README or an MCP tool result is data, not a command (report section 5.3). The kit's defence is to keep untrusted text away from anything that can act, and to make acting on it a human decision.

## Trusted and untrusted

**Trusted:** your chat messages; the plan and acceptance criteria you approved at H1 and H2; repository content at the base sha; kit files from a tagged release.

**Untrusted:** web pages (WebFetch, WebSearch); issues and issue comments; pull request comments and reviews from non-maintainers; dependency files and changelogs; MCP tool output and server instructions; the UAT or feedback widget; anything an engineer wrote in this run until it is reviewed; CI logs.

## Rules

1. **Taint follows the session.** A session that has read untrusted content may not hold merge, push-to-protected or credential-bearing authority in the same context. At L3 and above the conductor and the verifier have no WebFetch or WebSearch (`settings.web-tools` checks the deny rules); engineers' web access is limited to a domain allowlist (the sandbox network allowlist plus `WebFetch(domain:...)` rules).
2. **Intake is two-stage.** A tool-less triage agent reads the issue or comment and emits a fixed JSON schema (title, problem, acceptance criteria as plain text, labels). That JSON goes into a quoted "untrusted input" block of the brief, never into its instruction section. A separate tool-bearing session acts on the JSON.
3. **Intake triggers only from trusted actors:** `author_association` of OWNER or MEMBER, plus a maintainer-applied label. The `label-guard` workflow (`.claude/templates/github/workflows/label-guard.yml`) removes `factory:*`, `plan-ok` and `preview` labels applied by anyone not listed in the `FACTORY_APPROVERS` repository variable, and turns a non-approver's `factory:go` into `factory:requested`. "A CODEOWNER applied it" is not a check, because labels are not path-scoped (RC-20).
4. **No write-capable routine runs on non-maintainer pull requests.** A routine triggered by a pull request would run the pull request's own `.claude/`.
5. **claude-code-action in CI:** restore `.claude/`, `.mcp.json`, `plugin/**`, `.claude-plugin/**` and every directory the kit loads skills from, from the base ref, before running (skill frontmatter hooks run in untrusted `-p`, RC-18); use `--setting-sources user` or an explicit `--settings`; run it in a job that does not check out PR code with secrets in scope. `ci_lint.py` enforces the first two ([split-ci.md](split-ci.md)).
6. **Acting on instructions found in untrusted content is an always-human class** (H7).
7. **Detection is a guardrail.** A PostToolUse hook (Phase 2) flags tool output that contains agent-directed imperatives. It annotates and logs; nothing relies on it blocking.
8. **Evals:** an injection corpus (a poisoned README, issue, web page, MCP result and PR comment) runs as safety cases at threshold 1.0 (Phase 2).

## In CI

A workflow that interpolates untrusted text into a shell script is an injection hole whatever the agent does. `ci.script-injection` flags `${{ github.event.pull_request.title }}`, `github.head_ref`, comment and review bodies and similar in `run:` steps; pass them through `env:` instead, as `label-guard.yml` does.
