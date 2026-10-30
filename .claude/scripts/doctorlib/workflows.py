"""A small structural reader for GitHub workflow YAML, shared by the doctor and `ci_lint.py`.

It parses with `kitlib.miniyaml` (restricted subset, IMP-11). A workflow the subset cannot read
raises `Unparsed`; callers must report "unparsed: manual review" and never treat it as clean
(IMP-23). `scan_text` is the fallback: it looks for deploy commands in the raw text so an
unparsed workflow is still examined.
"""
from __future__ import annotations

import functools
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from kitlib import miniyaml

__all__ = ["Unparsed", "Step", "Job", "Workflow", "parse", "deploy_labels", "deploy_uses_label", "scan_text",
           "branch_pattern_matches", "PR_EVENTS", "DEPLOY_COMMANDS", "normalize_shell", "push_main_only", "docker_image",
           "docker_command", "docker_is_iac", "docker_opaque_iac", "docker_pinned", "step_command_text", "perm_level", "job_credentialed",
           "CRED_SECRETS", "CRED_ACTIONS", "flags_re", "var_command_re", "VAR_WORD", "PROG_BEHIND", "ScanLimit", "UNREADABLE_LABEL", "strip_comments"]

PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment", "merge_group")


class Unparsed(ValueError):
    pass


UNREADABLE_LABEL = "unreadable script (more quotes and expressions than the reader follows): treated as a deploy"


class ScanLimit(Exception):
    """A script has more quotes, substitutions and expressions than the span pass will look at. The callers (`ci_lint.lint_text`, the doctor's
    `deploy.classify_workflow`) report the workflow as unreadable: it is never read as clean (D6)."""


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class Step(object):
    def __init__(self, raw: Any, index: int) -> None:
        d = _as_dict(raw)
        self.index = index
        self.name = str(d.get("name", "") or "")
        self.uses = str(d.get("uses", "") or "")
        self.run = str(d.get("run", "") or "")
        self.with_ = _as_dict(d.get("with"))
        self.env = _as_dict(d.get("env"))
        self.if_ = str(d.get("if", "") or "")
        self.raw = d

    @property
    def text(self) -> str:
        parts = [self.run, self.uses, self.if_]
        parts += [str(v) for v in self.with_.values()]
        parts += [str(v) for v in self.env.values()]
        return "\n".join(parts)


class Job(object):
    def __init__(self, job_id: str, raw: Any) -> None:
        d = _as_dict(raw)
        self.id = job_id
        env = d.get("environment")
        self.environment = (str(env.get("name", "")) if isinstance(env, dict) else str(env or "")) or None
        self.permissions = d.get("permissions")
        self.uses = str(d.get("uses", "") or "")
        self.if_ = str(d.get("if", "") or "")
        self.secrets = d.get("secrets")
        self.env = _as_dict(d.get("env"))
        self.steps = [Step(s, i) for i, s in enumerate(_as_list(d.get("steps")))]
        self.raw = d

    @property
    def text(self) -> str:
        parts = [self.uses, self.if_] + [s.text for s in self.steps] + [str(v) for v in self.env.values()]
        if self.secrets is not None:
            parts.append(str(self.secrets))
        return "\n".join(parts)


class Workflow(object):
    def __init__(self, path: str, data: Dict[str, Any]) -> None:
        self.path = path
        self.name = str(data.get("name", "") or "")
        self.events = self._events(data.get("on", data.get(True)))
        self.permissions = data.get("permissions")
        self.env = _as_dict(data.get("env"))
        self.jobs = {str(k): Job(str(k), v) for k, v in _as_dict(data.get("jobs")).items()}
        self.raw = data

    @staticmethod
    def _events(on: Any) -> Dict[str, Dict[str, Any]]:
        ev = {}  # type: Dict[str, Dict[str, Any]]
        if isinstance(on, str):
            ev[on] = {}
        elif isinstance(on, list):
            for e in on:
                ev[str(e)] = {}
        elif isinstance(on, dict):
            for k, v in on.items():
                ev[str(k)] = v if isinstance(v, dict) else {}
        return ev

    @property
    def text(self) -> str:
        return "\n".join(j.text for j in self.jobs.values())


def parse(text: str, path: str = "<workflow>") -> Workflow:
    try:
        data = miniyaml.parse(text)
    except miniyaml.YamlError as exc:
        raise Unparsed(str(exc))
    if not isinstance(data, dict):
        raise Unparsed("top level is not a mapping")
    if "jobs" not in data:
        raise Unparsed("no jobs: key")
    if "on" not in data and True not in data:
        raise Unparsed("no on: key")
    return Workflow(path, data)


# --- deploy-class commands (report §8.4) ----------------------------------------------------------------

TERRAGRUNT_WORDS = "run-all|run|stack|--"      # words that may stand between `terragrunt` and `apply`: `run --all -- apply`, `stack run apply`

# What may not stand in front of a program name for a word to be a call of it. The doctor and the lint share this one rule (the doctor read
# `x-cdk deploy` and `foo.cdk deploy` as calls and the lint did not): a `-`, `.` or `/` in front makes the name part of another word or of a path, and
# a path to a tool is rewritten to the bare name by `normalize_shell` before anything is matched.
PROG_BEHIND = r"(?<![\w./-])"


def flags_re(prog: str, behind: str = PROG_BEHIND, words: str = "") -> str:
    """The flags a command line may carry between a program and its verb, as a regex fragment: `-x`, `--long`, `--k=v`, `--k value`, `--k "a b"`.

    Linear on hostile text (R-INFRA-3). The old form let `-a` take the next `-a` as its value, so a run of N flags had 2**N parses and
    `terragrunt` followed by forty `-a ` words took a minute. Here a flag name must start with a word character (so `--a` has one parse) and a
    separate value may not look like a flag, so a token is a flag or a value, never both.

    The other cost is a match start inside a flag run: a call may start at any token that ends with the program's name (`cdk -a cdk deploy`
    starts again at the second `cdk`, and so does `--app=cdk` or `-a x/cdk`), and a run that swallowed such a token would be scanned again from
    there, once per token, which is quadratic. So a run stops in front of a token that ends with the program's name and lets the match start there
    instead. Only a token that is the whole name counts: a value that merely begins with it (`cdk --app cdk.out deploy`, `kubectl --kubeconfig
    kubectl/cfg apply`) is a value like any other. `prog` is a regex alternation of the program names; `behind` is the lookbehind that decides
    where the caller's match may start (`PROG_BEHIND`; the stop rule must agree with the callers' own);
    `words` is an alternation of bare words that may also stand in the run (terragrunt's `run`, `--`), which are then never taken as a value."""
    stop = r"(?!\S*%s(?:%s)(?!\S))" % (behind, prog)
    kw = r"(?:%s)(?!\S)" % words if words else ""
    not_kw = r"(?!%s)" % kw if words else ""
    # A quoted value may hold spaces. The three forms (closed double quotes, closed single quotes, anything else) cannot match the same text, so
    # a token has one parse and a failing line is not tried twice per quoted value; a quote that is not closed within 200 characters is just a word.
    # (`normalize_shell` has already turned every quoted string, `$(...)`, `${{ ... }}` and escaped blank into a word with no blank in it, a copy
    # in which the blank is `\x01`, so a longer quoted value is read by the last form.) A flag may carry its quoted value with no blank or `=` in
    # between (`-a'x y'`): only when the quote is closed (or the word holds a `\x01`), so that `echo "cdk -c" deploy` is not a flag with a value.
    quoted = r"""(?:"[^"\n]{0,200}"\S*|'[^'\n]{0,200}'\S*|(?!"[^"\n]{0,200}")(?!'[^'\n]{0,200}')\S+)"""
    attached = r"""(?:"[^"\n]{0,200}"\S*|'[^'\n]{0,200}'\S*|(?!"[^"\n]{0,200}")(?!'[^'\n]{0,200}')["'][^\s\x01]*\x01\S*)"""
    # A bare variable word (`$FLAGS`, `${FLAGS}`, `"$@"`, `"${ARGS[@]}"`, `${{ inputs.flags }}`) stands for any number of flags (D3). It is a word of its
    # own and never the value of the flag before it (the lookahead in `value`), so a token still has one parse.
    var = r"""(?:"\$|\$)\S*"""
    value = r"""[ \t]+(?!-+\w)(?!"?\$)%s%s%s""" % (stop, not_kw, quoted)
    flag = r"-{1,2}\w[\w-]*(?:=(?:%s|)|%s|%s)?" % (quoted, attached, value)
    return r"(?:\s+%s(?:%s|%s%s))*" % (stop, flag, var, "|" + kw if words else "")


class _Near(object):
    """`A ... B` on one line, as a matcher with `.search`. The regex `A[^\\n]*B` costs a scan to the end of the line for every `A`, so a line of
    ten thousand `curl` words (a workflow comes from a stranger) takes minutes. This takes the first `A` and looks for one `B` after it: same
    answer, two linear scans."""

    def __init__(self, a: str, b: str) -> None:
        self.a, self.b = re.compile(a), re.compile(b)
        self.pattern = a + " ... " + b

    def search(self, text: str) -> Optional[bool]:
        for line in text.split("\n"):
            m = self.a.search(line)
            if m and self.b.search(line, m.end()):
                return True
        return None


class _Any(object):
    def __init__(self, *parts: Any) -> None:
        self.parts = parts
        self.pattern = " | ".join(getattr(p, "pattern", "") for p in parts)

    def search(self, text: str) -> Optional[bool]:
        for p in self.parts:
            if p.search(text):
                return True
        return None


# A variable word: `$T`, `${T}`, `"$T"`, `"${T}"`, `"${CMD[@]}"`.
VAR_WORD = r"""(?:"\$\{?[A-Za-z_]\w*(?:\[[@*]\])?\}?"|\$\{?[A-Za-z_]\w*(?:\[[@*]\])?\}?)"""
# `NAME=value ` assignments in front of a command: at most eight, each value at most 200 characters, and none runs over `;`, `&`, `|` or a newline. Without
# these bounds a text of `;A=x` repeated took minutes: every start ran to the end of the text (D1).
_VAR_ENV = r"(?:\w{1,64}=[^\s;&|]{0,200}[ \t]+){0,8}"


def var_command_re(word: str = VAR_WORD, starts: str = r"[;&|({\n]|\bthen\b|\bdo\b", verbs: str = "apply|destroy|deploy") -> str:
    """The regex (a string; compile it with `re.M`) for a command whose program is a word the reader cannot see through (`"$T" apply`, `$CMD deploy`, and
    `sudo "$T" apply` or `env X=1 "$T" apply`): what it names is not visible here, so it is deploy-capable (D5). `word` is what stands in command position,
    `starts` what may stand in front of it, `verbs` the words that count. Between the word and the verb come at most sixteen flags (`-x`, `-chdir infra`,
    `-var-file=x`, `--`) and variable words (`$FLAGS`), on one line, none of them running over a separator; a flag's value is neither a flag nor a variable,
    so a token has one parse. The same text serves the doctor and the lint, and it is linear: every repeat is bounded and stops at a separator (D1). The verb
    ends at a word boundary that is not a hyphen (`"$CMD" deploy-docs` is no deploy, D6)."""
    return (r"(?:^|%s)[ \t]*" % starts + _VAR_ENV + r"(?:(?:sudo|exec|command|nohup|time|env)[ \t]+" + _VAR_ENV + r"){0,3}(?:%s)" % word
            + r"""(?:[ \t]+(?:-[^\s;&|]+(?:[ \t]+(?!-)(?!"?\$)[^\s;&|]+)?|"?\$[^\s;&|]*)){0,16}"""
            + r"[ \t]+(?:%s)(?![\w-])" % verbs)


DEPLOY_COMMANDS = [
    ("cdk deploy", re.compile(PROG_BEHIND + r"cdk" + flags_re("cdk") + r"\s+(?:deploy|destroy|bootstrap)\b")),
    ("cdktf deploy", re.compile(PROG_BEHIND + r"cdktf" + flags_re("cdktf") + r"\s+(?:deploy|destroy)\b")),
    ("sam deploy", re.compile(PROG_BEHIND + r"sam" + flags_re("sam") + r"\s+(?:deploy|sync|delete)\b")),
    ("terraform apply", re.compile(PROG_BEHIND + r"(?:terraform|tofu)" + flags_re("terraform|tofu") + r"\s+(?:apply|destroy|import|taint)\b")),
    ("terragrunt apply", re.compile(PROG_BEHIND + r"terragrunt" + flags_re("terragrunt", words=TERRAGRUNT_WORDS) + r"\s+(?:apply|destroy)\b")),
    ("copilot deploy", re.compile(PROG_BEHIND + r"copilot" + flags_re("copilot") + r"\s+(?:(?:svc|env|job|pipeline)\s+)?deploy\b")),
    ("eb deploy", re.compile(PROG_BEHIND + r"eb" + flags_re("eb") + r"\s+(?:deploy|create|terminate|upgrade|swap)\b")),
    ("ansible-playbook", re.compile(PROG_BEHIND + r"ansible-playbook\b")),
    ("sst deploy", re.compile(PROG_BEHIND + r"sst" + flags_re("sst") + r"\s+(?:deploy|remove)\b")),
    ("serverless deploy", re.compile(PROG_BEHIND + r"(?:serverless|sls)" + flags_re("serverless|sls") + r"\s+(?:deploy|remove)\b")),
    ("amplify publish", re.compile(PROG_BEHIND + r"amplify" + flags_re("amplify") + r"\s+(?:publish|push)\b")),
    ("fly deploy", re.compile(PROG_BEHIND + r"(?:flyctl|fly)" + flags_re("flyctl|fly") + r"\s+(?:deploy|launch)\b")),
    ("wrangler deploy", re.compile(PROG_BEHIND + r"wrangler" + flags_re("wrangler") + r"\s+(?:deploy|publish)\b")),
    ("vercel deploy", _Any(_Near(PROG_BEHIND + r"vercel\b", r"\s--prod\b"), re.compile(PROG_BEHIND + r"vercel\s+(?:deploy|--prebuilt)\b"))),
    ("netlify deploy", re.compile(PROG_BEHIND + r"netlify" + flags_re("netlify") + r"\s+deploy\b")),
    ("kubectl apply", re.compile(PROG_BEHIND + r"kubectl" + flags_re("kubectl") + r"\s+(?:apply|rollout|replace|patch|set|scale|delete|create)\b")),
    ("helm upgrade", re.compile(PROG_BEHIND + r"helm" + flags_re("helm") + r"\s+(?:upgrade|install|rollback|uninstall)\b")),
    ("pulumi up", re.compile(PROG_BEHIND + r"pulumi" + flags_re("pulumi") + r"\s+(?:up|update|destroy|refresh)\b")),
    ("deploy through a variable", re.compile(var_command_re(), re.M)),
    ("cloudformation deploy", re.compile(PROG_BEHIND + r"aws" + flags_re("aws") + r"\s+cloudformation\s+(?:deploy|create-stack|update-stack|execute-change-set|delete-stack)\b")),
    ("aws deploy", re.compile(PROG_BEHIND + r"aws" + flags_re("aws") + r"\s+(?:ecs\s+(?:update-service|run-task)|lambda\s+(?:update-function-code|publish-version|update-alias)|s3\s+(?:sync|cp|mv)\s+\S+\s+s3://|deploy\s+create-deployment|elasticbeanstalk\s+update-environment|apprunner\s+start-deployment)")),
    ("gcloud deploy", re.compile(PROG_BEHIND + r"gcloud" + flags_re("gcloud") + r"\s+(?:run\s+deploy|app\s+deploy|functions\s+deploy|builds\s+submit|compute\s+instances\s+create)")),
    ("az deploy", re.compile(PROG_BEHIND + r"az" + flags_re("az") + r"\s+(?:webapp\s+(?:deploy|up)|functionapp\s+deploy|containerapp\s+(?:up|update|create)|deployment\s+\S+\s+create)")),
    ("firebase deploy", re.compile(PROG_BEHIND + r"firebase" + flags_re("firebase") + r"\s+deploy\b")),
    ("railway up", re.compile(r"\brailway\s+(?:up|deploy)\b")),
    ("heroku deploy", re.compile(r"\bgit\s+push\s+heroku\b|\bheroku\s+container:release\b")),
    ("docker push", _Any(re.compile(PROG_BEHIND + r"docker" + flags_re("docker") + r"\s+push\b"), _Near(r"\bdocker\s+buildx\s+build\b", r"--push\b"))),
    ("package publish", re.compile(PROG_BEHIND + r"(?:npm|pnpm|yarn|bun)" + flags_re("npm|pnpm|yarn|bun") + r"\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgem\s+push\b|\bpoetry\s+publish\b|\buv\s+publish\b|\bgradle\w*\s+publish\b")),
    ("gh release", re.compile(r"\bgh\s+release\s+(?:create|upload|edit)\b")),
    ("release automation", re.compile(r"\bsemantic-release\b|\bchangesets?\s+publish\b|\bchangeset\s+version\b|\brelease-please\b")),
    ("deploy script", re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:deploy|release|publish|ship)(?::\S+)?\b|\bmake\s+(?:-\S+\s+)*(?:deploy|release|publish|apply)\b|\bjust\s+(?:deploy|release|publish|apply)\b|(?:^|[\s;&|])\./(?:scripts/|bin/)?(?:deploy|release|publish)[\w.-]*")),
    ("remote copy", _Any(_Near(r"\brsync\b", r"\S+@\S+:"), _Near(r"\bscp\b", r"\S+@\S+:"))),
    ("deploy hook", _Near(r"\bcurl\b", r"deploy[-_ ]?hook|api\.vercel\.com/v1/integrations/deploy|api\.netlify\.com/build_hooks|api\.render\.com/deploy")),
]  # type: List[Tuple[str, Any]]

# --- shell normalisation and docker:// steps (IN-2, IN-3, IN-6) ------------------------------------------------

# Programs whose invocation form is normalised before matching: a path (`/usr/local/bin/terraform`, `./bin/tofu`), a quoted word
# (`terraform 'apply'`), `$(which terraform)` and `${TF:-terraform}` all read as the bare word.
_CMD_TOOLS = ("terraform", "tofu", "terragrunt", "pulumi", "cdktf", "cdk8s", "cdk", "kubectl", "helm", "sam", "eb", "copilot", "ansible-playbook",
              "serverless", "sls", "sst", "aws", "gcloud", "az", "wrangler", "vercel", "netlify", "firebase", "flyctl")
_TOOL_ALT = "|".join(sorted(_CMD_TOOLS + ("opentofu",), key=len, reverse=True))
_WHICH_RE = re.compile(r"\$\(\s*(?:which|command\s+-v|type\s+-p|realpath|readlink\s+-f)\s+[\"']?(?:%s)[\"']?\s*\)|"
                       r"`\s*(?:which|command\s+-v|type\s+-p)\s+[\"']?(?:%s)[\"']?\s*`" % (_TOOL_ALT, _TOOL_ALT))
_WHICH_TOOL_RE = re.compile(r"(%s)" % _TOOL_ALT)
_DEFAULT_RE = re.compile(r"\$\{\w+:?-[\"']?(%s)[\"']?\}" % _TOOL_ALT)
# A path before a tool (`/usr/local/bin/terraform`, `hashicorp/terraform`), with the `:tag` or `@sha256:` of a container image dropped, so that
# `docker run hashicorp/terraform:1.9 apply` reads `terraform apply`.
_PATH_CMD_RE = re.compile(r"(?<![\w.@+~/-])(?:~|\.{1,2})?/?(?:[\w.@+~-]+/)+(%s)(?::[\w.-]+)?(?:@sha256:[0-9a-f]{16,})?(?=[\s;|&)\"']|$)" % _TOOL_ALT)
# The same after a variable or a command substitution (`${DIR}/cdk`, `"$DIR"/cdk`, `$(pwd)/bin/cdk`): the path part is dropped.
_EXPANDED_PATH_RE = re.compile(r"(?:\"?\$\{[^{}\n]{1,100}\}\"?|\"?\$\w+\"?|\$\([^()\n]{0,100}\))(?:/[\w.@+~-]+)*/(%s)(?=[\s;|&)\"']|$)" % _TOOL_ALT)
# A package that runs a tool (`npx aws-cdk deploy`, `pipx run aws-sam-cli deploy`) reads as the tool, and a version or dist-tag after a tool or such a
# package (`npx cdk@2 deploy`, `pnpm dlx aws-cdk@2.150.0 deploy`) is dropped. A version looks like one (digits, or a tag), so `ssh sam@host deploy`,
# where `sam` is a user name, is left alone, and a name with a dash in front (`x-aws-cdk`) is not the package.
_PKG_TOOL = {"aws-cdk": "cdk", "cdktf-cli": "cdktf", "aws-sam-cli": "sam", "firebase-tools": "firebase", "netlify-cli": "netlify"}
_PKG_VERSION = r"@(?:latest|next|canary|beta|alpha|rc|nightly|stable|[\^~<>=v]*\d[\w.*+-]*)"
_PKG_RE = re.compile(r"(?<![\w./@-])(?:(%s)(?:%s)?|(%s)%s)(?![\w.@/-])" % ("|".join(sorted(_PKG_TOOL, key=len, reverse=True)), _PKG_VERSION, _TOOL_ALT, _PKG_VERSION))
# `$'apply'` and `$"apply"` are the word `apply` for bash (ANSI-C and locale quoting), so the `$` in front of the quote goes with the quotes (D4).
_UNQUOTE_RE = re.compile(r"(?<![\w$\\])\$?([\"'])([A-Za-z][\w.-]*)\1(?![\w$])")
# An empty quote pair (`terra""form`) is nothing, unless its first quote is escaped: `\"` and then the quote that ends the string (`"a=\"b c\""`).
_EMPTY_QUOTES_RE = re.compile(r"""(?<!\\)(?:""|'')""")
# A quoted run of word characters that sits inside a word (`terra'form'`, `ap"ply"`, `'terra'form`): the quotes do nothing, so they are dropped.
_INWORD_QUOTES_RE = re.compile(r"""(?<=[\w.-])(["'])([\w.-]+)\1|(["'])([\w.-]+)\3(?=[\w.-])""")
_ASSIGN_RE = re.compile(r"(?:^|[;&|(\s])([A-Za-z_]\w*)=[\"']?(%s)[\"']?(?=\s|;|$)" % _TOOL_ALT, re.M)

# Blanks inside one word. A flag value can hold blanks (a quoted string, `$(echo a b)`, `${{ matrix.dir }}`, `a\ b`), and the flag run that
# `flags_re` reads is a run of blank-separated words, so such a value would hide the verb after it. `_blank_copy` finds these spans and returns a copy of
# the lines that hold one with every blank inside a span replaced by `_BLANK`, so the value is one word. The copy is added to the text, never put in
# its place: a command inside the span (`sh -c "kubectl apply -f x"`, `$(terraform apply)`) must stay visible as it was written.
_BLANK = "\x01"
_BLANK_RE = re.compile(r"\s")
_SPAN_RX = {"t": re.compile(r"""[\\'"`$#]"""),         # top level (a comment is skipped: its quotes pair with nothing)
            "d": re.compile(r"""[\\"`$]"""),           # inside double quotes
            "c": re.compile(r"""[\\'"`$()]"""),        # inside $( ): a `#` there is not a comment (the closing parenthesis would be part of it)
            "b": re.compile(r"""[\\'"`${}]"""),        # inside ${ }
            "e": re.compile(r"""['}]""")}              # inside ${{ }}: a string may hold `}}`
_ANSI_TAIL = re.compile(r"(?:[^'\\]|\\.)*'", re.S)     # the rest of a `$'...'` string
_SPAN_STEPS = 100000     # delimiters looked at per script; past that the script is unreadable (`ScanLimit`), not read as written (D6)
_SPAN_DEPTH = 40         # nesting followed; a deeper opener is an ordinary character
_SPAN_REDO = 6           # times a scan starts again after an opener that never closes
_SPAN_LEVELS = 3         # how deep the inside of a quoted command string is read again (`sh -c "kubectl --context ${{ a b }} apply"` is level two)


def _blank_spans(text: str, budget: Optional[List[int]] = None) -> List[Tuple[int, int]]:
    """The (start, end) of every closed span in `text` that can hold blanks: `'...'`, `"..."`, `` `...` ``, `$(...)`, `${...}` and `${{ ... }}`,
    nested ones included. One left-to-right pass with a stack. An opener with no closer is an ordinary character: the scan starts again just after
    it (at most `_SPAN_REDO` times, so a hostile text of unclosed openers is not scanned once per opener), which keeps a verb after it in view. A
    `${{` whose every `}}` sits inside a string is closed at the first of them. `budget` is the number of delimiters left to look at (one count for
    all the scans of one text)."""
    n = len(text)
    spans = []  # type: List[Tuple[int, int]]
    stack = []  # type: List[List[Any]]      # [kind, start, depth (c, b) or the first `}}` inside a string (e)]
    last = {q: text.rfind(q) for q in ("'", '"', "`")}
    last_expr = text.rfind("}}")
    budget = budget if budget is not None else [_SPAN_STEPS]
    i = redo = 0
    while True:
        if budget[0] <= 0:
            raise ScanLimit("more than %d quotes, substitutions and expressions in one script" % _SPAN_STEPS)
        budget[0] -= 1
        top = stack[-1] if stack else None
        kind = top[0] if top else "t"
        m = _SPAN_RX[kind].search(text, i)
        if m is None:
            if top is None:
                break
            redo += 1
            if redo > _SPAN_REDO:
                break
            if kind == "e" and top[2] >= 0:
                stack.pop()
                spans.append((top[1], top[2] + 2))
                i = top[2] + 2
            else:
                bottom = stack[0]
                while spans and spans[-1][1] > bottom[1]:
                    spans.pop()
                i = bottom[1] + 1
                del stack[:]
            continue
        j = m.start()
        c = text[j]
        i = j + 1
        if c == "\\":
            i = j + 2
            if text[j + 1:j + 2] in (" ", "\t"):       # an escaped blank is part of the word (`-n my\ ns`)
                spans.append((j, j + 2))
        elif c == "'":
            if kind == "e":
                k = text.find("'", j + 1)
                if k >= 0:
                    if top[2] < 0:
                        top[2] = text.find("}}", j + 1, k)
                    i = k + 1
            elif j < last["'"]:
                if j and text[j - 1] == "$":                    # `$'...'`: a backslash escapes the next character, `\'` included
                    m2 = _ANSI_TAIL.match(text, j + 1)
                    k = m2.end() - 1 if m2 else -1
                else:
                    k = text.find("'", j + 1)
                if k >= 0:
                    spans.append((j, k + 1))
                    i = k + 1
        elif c == "`":
            if j < last["`"]:
                k = text.find("`", j + 1)
                spans.append((j, k + 1))
                i = k + 1
        elif c == '"':
            if kind == "d":
                stack.pop()
                spans.append((top[1], j + 1))
            elif j < last['"'] and len(stack) < _SPAN_DEPTH:
                stack.append(["d", j, 0])
        elif c == "$":
            if len(stack) >= _SPAN_DEPTH:
                pass
            elif text.startswith("${{", j):
                i = j + 3
                if j + 3 <= last_expr:
                    if redo > 2:               # after repeated trouble: the first `}}` ends it, no string is read
                        k = text.find("}}", j + 3)
                        spans.append((j, k + 2))
                        i = k + 2
                    else:
                        stack.append(["e", j, -1])
            elif text.startswith("$(", j):
                stack.append(["c", j, 0])
                i = j + 2
            elif text.startswith("${", j):
                stack.append(["b", j, 0])
                i = j + 2
        elif c == "#":                                  # a comment runs to the end of its line when the `#` starts a word
            if j == 0 or text[j - 1] in " \t\n;&|(":
                k = text.find("\n", j)
                i = n if k < 0 else k
        elif c == "}" and kind == "e":
            if text.startswith("}}", j):
                stack.pop()
                spans.append((top[1], j + 2))
                i = j + 2
        elif c in "({":
            top[2] += 1
        elif c in ")}":
            if top[2]:
                top[2] -= 1
            else:
                stack.pop()
                spans.append((top[1], j + 1))
    return spans


def _merge_spans(spans: List[Tuple[int, int]]) -> List[List[int]]:
    merged = []  # type: List[List[int]]
    for a, b in sorted(spans):
        if merged and a < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


_FRESH_STARTS = 6        # passes over several lines that start again after a line with an odd number of quotes (D2)


def _fresh_starts(piece: str) -> List[int]:
    """The offsets of the line starts right after each of the first `_FRESH_STARTS` lines that hold an odd number of `'`, `"` or `` ` ``. A stray quote
    (a heredoc body: `don't`; `say "`) pairs with the quote that opens a multi-line value later on and shifts every pairing after it. For one kind of
    quote there are only two ways to pair a run of quote characters, and starting the pass again right after the first odd line gives the other one,
    however many stray lines come before the value. A start that pairs wrongly only adds a copy of some lines; the text itself is never changed."""
    out = []  # type: List[int]
    pos = 0
    n = len(piece)
    while pos < n and len(out) < _FRESH_STARTS:
        end = piece.find("\n", pos)
        if end < 0:
            break
        line = piece[pos:end]
        pos = end + 1
        if line.count("'") % 2 or line.count('"') % 2 or line.count("`") % 2:
            out.append(pos)
    return out


def _piece_spans(piece: str, off: int, budget: List[int]) -> Tuple[List[Tuple[int, int]], List[List[Tuple[int, int]]]]:
    """The spans of one piece of text, found two ways, as (line by line, over several lines). Line by line: a quote that is not closed on its line is
    an ordinary character there, so a stray apostrophe (a heredoc body, `echo don't`) cannot pair with a quote on another line and hide the values
    after it (D2). Over the whole piece, keeping only the spans that run over several lines (`cdk -c 'a` newline `b' deploy`): one list of spans from a
    pass over the whole piece, and one from each pass that starts again after a line with an odd number of quotes (`_fresh_starts`), because a stray
    quote in an earlier line would otherwise pair with the opener of the value. The passes are kept apart (a span of one pass that overlaps a span of
    another would join them into one), and each only adds a copy of some lines."""
    lines = []  # type: List[Tuple[int, int]]
    multi = []  # type: List[List[Tuple[int, int]]]
    if not _SPAN_RX["t"].search(piece):
        return lines, multi
    if "\n" not in piece:
        return [(a + off, b + off) for a, b in _blank_spans(piece, budget)], multi
    pos = 0
    for line in piece.split("\n"):
        if len(line) > 1 and _SPAN_RX["t"].search(line):
            lines.extend((a + pos + off, b + pos + off) for a, b in _blank_spans(line, budget))
        pos += len(line) + 1
    for start in [0] + _fresh_starts(piece):
        sub = piece[start:] if start else piece
        multi.append([(a + start + off, b + start + off) for a, b in _blank_spans(sub, budget) if sub.find("\n", a, b) >= 0])
    return lines, multi


def _blank_levels(text: str) -> List[List[List[int]]]:
    """The outermost spans of `text` (level one), then the outermost spans inside those (level two), and so on to `_SPAN_LEVELS`. A quoted string, a
    substitution or a backtick pair is often a command of its own (`sh -c "..."`, `ssh host '...'`, `docker run img sh -c "..."`) with values in it
    that hold blanks, so the inside of each span is read again as code. The blanks are made one word at one level at a time: at level two the
    blanks of the outer string stay, and the command words in it stay apart."""
    budget = [_SPAN_STEPS]
    levels = []  # type: List[List[List[int]]]
    pieces = [(0, text)]
    for _ in range(_SPAN_LEVELS):
        by_line = []  # type: List[Tuple[int, int]]
        by_text = []  # type: List[List[Tuple[int, int]]]       # one list per pass over several lines (the same pass of every piece goes together)
        for off, piece in pieces:
            one, passes = _piece_spans(piece, off, budget)
            by_line.extend(one)
            for k, sp in enumerate(passes):
                if k == len(by_text):
                    by_text.append([])
                by_text[k].extend(sp)
        found = [m for m in [_merge_spans(by_line)] + [_merge_spans(sp) for sp in by_text] if m]
        if not found:
            break
        levels.extend(found)
        pieces = []
        seen = set()  # type: Set[Tuple[int, int]]
        for merged in found:
            for a, b in merged:
                head = 0 if text.startswith("${{", a) else (2 if text.startswith(("$(", "${"), a) else (1 if text[a] in "\"'`" else 0))
                if head and b - a > head + 1 and (a, b) not in seen:
                    seen.add((a, b))
                    pieces.append((a + head, text[a + head:b - 1]))
    return levels


def _blanked_lines(text: str, merged: List[List[int]]) -> List[str]:
    """The lines of `text` that hold one of the `merged` spans with a blank in it, each blank inside a span replaced by `_BLANK`. A span that runs
    over several lines gives one line."""
    pieces = []  # type: List[str]
    groups = []  # type: List[List[int]]      # [line start, line end] of the lines that changed
    pos = 0
    for a, b in merged:
        seg = text[a:b]
        if not _BLANK_RE.search(seg):
            continue
        pieces.append(text[pos:a])
        pieces.append(_BLANK_RE.sub(_BLANK, seg))
        pos = b
        if groups and a <= groups[-1][1]:
            if b > groups[-1][1]:
                end = text.find("\n", b)
                groups[-1][1] = len(text) if end < 0 else end
        else:
            lo = groups[-1][1] if groups else 0
            end = text.find("\n", b)
            groups.append([text.rfind("\n", lo, a) + 1, len(text) if end < 0 else end])
    if not groups:
        return []
    pieces.append(text[pos:])
    blanked = "".join(pieces)
    return [blanked[ls:le] for ls, le in groups]


def _blank_copy(text: str) -> str:
    """The lines of `text` that hold a span with a blank in it, with each such blank made one word (`_BLANK`), once per level of nesting; each line
    behind a separator line, so that nothing matches from one copy into the next. Empty when there is none."""
    if not _SPAN_RX["t"].search(text):
        return ""
    out = []  # type: List[str]
    seen = set()  # type: Set[str]
    for merged in _blank_levels(text):
        for line in _blanked_lines(text, merged):
            if line not in seen:
                seen.add(line)
                out.append(line)
    return "".join("\n\x00\n" + line for line in out)


_COMMENT_RX = re.compile(r"""[\\'"`$(){}#]""")
_COMMENT_STEPS = 20000       # delimiters looked at in one line; a line with more is left whole


def _comment_at(line: str) -> int:
    """The index of the `#` that starts a comment in one line of shell, or -1. One left-to-right pass over the line with a stack of what is open: `'...'`,
    `$'...'` (where a backslash escapes the next character, `\\'` included), `"..."`, `$(...)`, `${...}` (which holds `${{ ... }}`), a backtick pair.
    A `#` counts when it starts a word (after a blank or at the start of the line) and nothing is open. When the line ends with something still open,
    or a `#` stands inside a `$(...)` or `${...}`, or the line has too many delimiters, the answer is -1: the state is unclear, so nothing is stripped (D1)."""
    stack = []  # type: List[str]
    i = steps = 0
    while True:
        m = _COMMENT_RX.search(line, i)
        if m is None:
            return -1
        steps += 1
        if steps > _COMMENT_STEPS:
            return -1
        j = m.start()
        c = line[j]
        i = j + 1
        top = stack[-1] if stack else ""
        if top == "'":
            if c == "'":
                stack.pop()
        elif top == "$'":
            if c == "\\":
                i = j + 2
            elif c == "'":
                stack.pop()
        elif c == "\\":
            i = j + 2
        elif c == "`":
            k = line.find("`", j + 1)
            if k < 0:
                return -1
            i = k + 1
        elif top == '"':
            if c == '"':
                stack.pop()
            elif c == "$" and line.startswith(("$(", "${"), j):
                stack.append(line[j + 1])
                i = j + 2
        elif c == "'":
            stack.append("$'" if j and line[j - 1] == "$" else "'")
        elif c == '"':
            stack.append('"')
        elif c == "$":
            if line.startswith(("$(", "${"), j):
                stack.append(line[j + 1])
                i = j + 2
        elif c in "({":
            if top == c:
                stack.append(c)
        elif c in ")}":
            if top == {")": "(", "}": "{"}[c]:
                stack.pop()
        elif c == "#" and (j == 0 or (line[j - 1] in " \t" and not (j > 1 and line[j - 2] == "\\"))):      # `a\ #b` is one word
            return j if not stack else -1
    return -1


def strip_comments(text: str) -> str:
    """`text` with the comment at the end of each line cut off (`_comment_at`). A line whose quoting is unbalanced or unclear keeps its `#`: more text
    is read, never less."""
    out = []  # type: List[str]
    for line in text.split("\n"):
        k = _comment_at(line) if "#" in line else -1
        out.append(line[:k] if k >= 0 else line)
    return "\n".join(out)


@functools.lru_cache(maxsize=128)      # a script is normalised once per rule that reads it
def normalize_shell(text: str) -> str:
    """Shell text with the spellings that hide a command word removed: line continuations, `$(which terraform)`, `${X:-terraform}`, empty
    quote pairs, a backslash inside a word, a path before a deploy tool (also after `${DIR}` or `$(pwd)`), a package that runs a tool and its version
    (`npx aws-cdk@2 deploy`), quotes around a single word, and `TF=terraform` assignments (so `$TF apply` reads `terraform apply`). Then, behind a
    separator line, a copy of the lines with a quoted string, `$(...)`, `${...}`, `${{ ... }}` or escaped blank in them, in which the blanks inside
    that span are made one word (`_blank_copy`): the verb after `--context ${{ matrix.ctx }}` or `-c a="x y"` is then next to its flags. Matching aid
    only: the result is never executed or shown as the user's text.

    A NUL in the text is a separator (D6): bash drops it, so `terra\\0form` is `terraform`, and a script reader may take it as a blank, so
    `terraform\\0 apply` is two words. The text is read both ways, one after the other behind the separator line. A quote inside a word is dropped
    (`terra'form' apply`, `ap"ply"`), and `"$T"` after `T=terraform` is the tool."""
    if "\x00" in text:
        return normalize_shell(text.replace("\x00", " ")) + "\n\x00\n" + normalize_shell(text.replace("\x00", ""))
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    text = _WHICH_RE.sub(lambda m: _WHICH_TOOL_RE.search(m.group(0)).group(1), text)
    text = _DEFAULT_RE.sub(lambda m: m.group(1), text)
    text = _EMPTY_QUOTES_RE.sub("", text)
    for _ in range(3):                   # `t'e'r'r'a'form`: a quote pair that ends where the next begins takes a pass of its own
        text, n = _INWORD_QUOTES_RE.subn(lambda m: m.group(2) or m.group(4), text)
        if not n:
            break
    text = re.sub(r"(?<=\w)\\(?=\w)", "", text)
    text = _PATH_CMD_RE.sub(lambda m: "tofu" if m.group(1) == "opentofu" else m.group(1), text)
    text = _EXPANDED_PATH_RE.sub(lambda m: "tofu" if m.group(1) == "opentofu" else m.group(1), text)
    text = _PKG_RE.sub(lambda m: _PKG_TOOL[m.group(1)] if m.group(1) else m.group(2), text)
    text = _UNQUOTE_RE.sub(lambda m: m.group(2), text)
    assigns = {}  # type: Dict[str, str]
    for m in _ASSIGN_RE.finditer(text):
        if len(assigns) >= 16:        # bounded: each name costs a pass over the text
            break
        assigns.setdefault(m.group(1), m.group(2))
    for name, val in assigns.items():
        text = re.sub(r"\$\{%s\}|\$%s\b" % (re.escape(name), re.escape(name)), val, text)
    if assigns:
        text = _UNQUOTE_RE.sub(lambda m: m.group(2), text)      # `"$T"` is `"terraform"` now
    return text + _blank_copy(text)


def push_main_only(cfg: Any) -> bool:
    """True when a `push:` trigger can only be a push to main or master (no other branch, no tag, no ignore list)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    br = [str(b) for b in _as_list(cfg.get("branches"))]
    return bool(br) and all(b in ("main", "master") for b in br) and not _as_list(cfg.get("tags")) \
        and not _as_list(cfg.get("branches-ignore")) and not _as_list(cfg.get("tags-ignore"))


_IAC_IMAGE_REPOS = ("hashicorp/terraform", "hashicorp/tfc-agent", "amazon/aws-cli", "bitnami/kubectl", "alpine/helm", "dtzar/helm-kubectl",
                    "gruntwork/terragrunt", "alpine/terragrunt")
_IAC_IMAGE_WORD = re.compile(r"terraform|tofu|terragrunt|pulumi|kubectl|helm")


def docker_image(uses: str) -> Tuple[str, str, str]:
    """(repository, tag, digest) of a `docker://IMAGE[:tag][@sha256:...]` reference; empty strings for what is absent."""
    ref = uses[len("docker://"):] if uses.startswith("docker://") else uses
    name, _, digest = ref.partition("@")
    tag = ""
    if ":" in name.rsplit("/", 1)[-1]:
        name, _, tag = name.rpartition(":")
    return name, tag, digest


def docker_pinned(uses: str) -> bool:
    return re.match(r"^sha256:[0-9a-f]{64}$", docker_image(uses)[2], re.I) is not None


def docker_is_iac(uses: str) -> bool:
    repo = docker_image(uses)[0].lower()
    if any(repo == r or repo.endswith("/" + r) for r in _IAC_IMAGE_REPOS):
        return True
    return bool(_IAC_IMAGE_WORD.search(repo.rsplit("/", 1)[-1])) or repo.startswith("opentofu/") or "/opentofu/" in repo \
        or repo.startswith("pulumi/") or "/pulumi/" in repo


def _docker_tool(repo: str) -> str:
    last = repo.lower().rsplit("/", 1)[-1]
    for t in ("terraform", "tofu", "terragrunt", "pulumi", "kubectl", "helm"):
        if t in last:
            return t
    return "aws" if "aws-cli" in last else last


def docker_command(step: "Step") -> str:
    """The command a `uses: docker://IMAGE` step runs, as shell text: the entrypoint (or the tool the image is known for) followed by
    `with.args`. Empty for a step that is not a docker:// step."""
    if not step.uses.startswith("docker://"):
        return ""
    repo = docker_image(step.uses)[0]
    args = step.with_.get("args", "")
    args = " ".join(str(a) for a in args) if isinstance(args, list) else str(args or "")
    entry = str(step.with_.get("entrypoint", "") or "").strip()
    head = entry or (_docker_tool(repo) if docker_is_iac(step.uses) else "")
    return (head + " " + args).strip()


def docker_opaque_iac(step: "Step") -> bool:
    """A known IaC image with no args and no entrypoint: whatever it runs is invisible here, so it counts as deploy-capable."""
    if not (step.uses.startswith("docker://") and docker_is_iac(step.uses)):
        return False
    cmd = docker_command(step).strip()
    # no args at all, or args computed at run time (`${{ inputs.cmd }}`, `$ARGS`): the verb is not visible here
    return not cmd.replace(_docker_tool(docker_image(step.uses)[0]), "").strip() or "${{" in cmd or bool(re.search(r"\$\{?\w", cmd.split(None, 1)[-1] if " " in cmd else ""))


def step_command_text(step: "Step") -> str:
    """The shell text of a step as the infra rules read it: its `run:` plus the command of a docker:// step."""
    cmd = docker_command(step)
    return step.run + ("\n" + cmd if cmd else "")


# --- cloud credentials in a job (shared by ci_lint's infra rules and the doctor's deploy scan) ---------------------

# `secrets.NAME`, `secrets['NAME']`, a computed index (`secrets[format('AWS_{0}', 'KEY')]`: the name is not visible, so it may be any of these) and
# `toJSON(secrets)` (R-INFRA-6). Group 1 is the name when one is visible.
CRED_SECRETS = re.compile(
    r"secrets(?:\.|\s*\[\s*['\"])(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|ARM_CLIENT_SECRET|ARM_ACCESS_KEY|ARM_CLIENT_CERTIFICATE\w*|"
    r"GOOGLE_CREDENTIALS|GOOGLE_APPLICATION_CREDENTIALS|GCP_SA_KEY\w*|TF_TOKEN_\w+|TF_API_TOKEN|TFE_TOKEN|PULUMI_ACCESS_TOKEN|"
    r"DIGITALOCEAN_TOKEN|AZURE_CREDENTIALS|KUBE_CONFIG\w*|KUBECONFIG\w*)|secrets\s*\[(?!\s*['\"])|tojson\(\s*secrets\s*\)", re.I)
CRED_ACTIONS = ("aws-actions/configure-aws-credentials", "google-github-actions/auth", "azure/login")


def perm_level(permissions: Any, name: str) -> Optional[str]:
    """The level of one permission in a `permissions:` value (a scalar `read-all`/`write-all`, a mapping, or absent)."""
    if isinstance(permissions, str):
        return "write" if permissions == "write-all" else ("read" if permissions == "read-all" else None)
    if isinstance(permissions, dict):
        v = permissions.get(name)
        return str(v) if v is not None else None
    return None


def job_credentialed(wf: "Workflow", job: "Job") -> bool:
    """The job can obtain cloud credentials: `id-token: write`, a credential action, or a stored cloud key."""
    perms = job.permissions if job.permissions is not None else wf.permissions
    if perm_level(perms, "id-token") == "write":
        return True
    if any(s.uses.lower().startswith(a) for s in job.steps for a in CRED_ACTIONS):
        return True
    return bool(CRED_SECRETS.search(job.text))


_DEPLOY_USES = [
    "aws-actions/amazon-ecs-deploy-task-definition", "aws-actions/aws-cloudformation-github-deploy", "aws-actions/aws-codebuild-run-build",
    "azure/webapps-deploy", "azure/functions-action", "azure/k8s-deploy", "azure/arm-deploy", "azure/static-web-apps-deploy",
    "google-github-actions/deploy-cloudrun", "google-github-actions/deploy-appengine", "google-github-actions/deploy-cloud-functions",
    "superfly/flyctl-actions", "cloudflare/wrangler-action", "cloudflare/pages-action", "amondnet/vercel-action", "nwtgck/actions-netlify",
    "netlify/actions", "pulumi/actions", "hashicorp/terraform-github-actions", "hashicorp/tfc-workflows-github", "dflook/terraform-apply",
    "appleboy/ssh-action", "appleboy/scp-action", "easingthemes/ssh-deploy", "peaceiris/actions-gh-pages",
    "jamesives/github-pages-deploy-action", "actions/deploy-pages", "softprops/action-gh-release", "ncipollo/release-action",
    "googleapis/release-please-action", "google-github-actions/release-please-action", "changesets/action", "cycjimmy/semantic-release-action",
    "pypa/gh-action-pypi-publish", "js-devtools/npm-publish", "helm/chart-releaser-action", "digitalocean/app_action",
    "serverless/github-action", "sst/ion-action", "gruntwork-io/terragrunt-action", "dflook/terraform-destroy", "dflook/tofu-apply",
    "dawidd6/action-ansible-playbook", "railwayapp/", "render-oss/", "expo/expo-github-action", "aws-amplify/",
]


def deploy_labels(text: str) -> List[str]:
    """Deploy-class command labels found in shell text (normalised, lower-cased). A script the span pass will not read (`ScanLimit`) is one label,
    `UNREADABLE_LABEL`: deploy-capable, never none (D6)."""
    try:
        norm = re.sub(r"[ \t]+", " ", normalize_shell(text.lower()))
    except ScanLimit:
        return [UNREADABLE_LABEL]
    out = []  # type: List[str]
    for label, rx in DEPLOY_COMMANDS:
        if rx.search(norm) and label not in out:
            out.append(label)
    return out


def deploy_uses_label(step: Step) -> Optional[str]:
    if step.uses.startswith("docker://"):
        repo = docker_image(step.uses)[0]
        labs = deploy_labels(docker_command(step))
        if labs:
            return "uses docker://%s (%s)" % (repo, labs[0])
        if docker_opaque_iac(step):
            return "uses docker://%s (IaC image, command not visible)" % repo
        return None
    u = step.uses.lower().split("@", 1)[0]
    for prefix in _DEPLOY_USES:
        if u == prefix or (prefix.endswith("/") and u.startswith(prefix)):
            return "uses " + u
    if u == "docker/build-push-action":
        push = str(step.with_.get("push", "")).strip().lower()
        if push and push != "false":
            return "uses docker/build-push-action (push)"
    return None


def scan_text(text: str) -> List[str]:
    """Raw-text fallback for a workflow that did not parse: deploy labels from commands and `uses:`."""
    labels = deploy_labels(text)
    for m in re.finditer(r"(?m)^\s*-?\s*uses:\s*['\"]?([^\s'\"#]+)", text):
        fake = Step({"uses": m.group(1)}, 0)
        lab = deploy_uses_label(fake)
        if lab and lab not in labels:
            labels.append(lab)
    return labels


def branch_pattern_matches(pattern: str, ref: str) -> bool:
    """GitHub filter-pattern match: `*` stops at `/`, `**` does not, `?` one char, `!` negates (caller)."""
    rx = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                rx += ".*"
                i += 2
                continue
            rx += "[^/]*"
        elif c == "?":
            rx += "[^/]"
        elif c == "[":
            j = pattern.find("]", i)
            if j > i:
                rx += pattern[i:j + 1]
                i = j + 1
                continue
            rx += re.escape(c)
        else:
            rx += re.escape(c)
        i += 1
    return re.fullmatch(rx, ref) is not None
