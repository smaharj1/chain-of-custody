#!/usr/bin/env python3
"""Emit the guard wiring from `hooks.manifest.json` (report §7.1, §7.2; IMP-13, IMP-16).

    wiring.py --print                 the settings.v2.json content on stdout
    wiring.py --write                 write .claude/settings.v2.json and the templates under .claude/templates/settings/
    wiring.py --check                 exit 1 if either differs from what the manifest generates (CI runs this)
    wiring.py --plugin                the plugin hooks.json content (Phase 9 reuses build_hooks with its own root)

The manifest is the single source. `build_hooks` takes the root expression and the scripts directory so the same
manifest wires a copy install (`$CLAUDE_PROJECT_DIR/.claude/scripts`) and a plugin (`${CLAUDE_PLUGIN_ROOT}/scripts`).

Handler hygiene (A29, CO-32): quoted root variable, a `timeout` on every handler, no network, `onFailure: block` on every
blocking-capable event (the field's name and placement are unverified, V9/IMP-16, so every entry point also fails closed
itself), and a shell prologue that exits 2 when python3 is missing, because a missing interpreter would otherwise be an
exit 127 that the platform treats as a non-blocking error. A missing python3 at SessionStart prints a banner instead.
`if:` filters are not used: the entry's `matcher` already limits which tools spawn python, and the Bash guard has to see
every Bash call, since a wrapped command cannot be told apart from a plain one without parsing it.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))
MANIFEST_PATH = os.path.join(HERE, "hooks.manifest.json")
SETTINGS_V2 = os.path.join(REPO_ROOT, ".claude", "settings.v2.json")
TEMPLATE_DIR = os.path.join(REPO_ROOT, ".claude", "templates", "settings")
SANDBOX_TEMPLATE = os.path.join(REPO_ROOT, ".claude", "templates", "security", "sandbox.settings.json")

ROOT_COPY = "$CLAUDE_PROJECT_DIR"
SCRIPTS_COPY = ".claude/scripts"
ROOT_PLUGIN = "${CLAUDE_PLUGIN_ROOT}"
SCRIPTS_PLUGIN = "scripts"

NO_PYTHON_STDERR = "kit guards: python3 not found; failing closed (rule guards:no-python3)"
NO_PYTHON_BANNER = ("kit guards: python3 was not found, so the guards and the liveness canary did NOT load. Install python3 "
                    "(3.9 or newer); until then the effective level is L0 (rule guards:no-python3).")

# ---------------------------------------------------------------------------------------------------------------------
# the deny block (report §7.2). A typo guard that works even in bypass mode (P25).

DENY_BASELINE = [
    # infrastructure mutators (H4)
    "Bash(terraform apply:*)", "Bash(terraform destroy:*)", "Bash(terraform import:*)", "Bash(terraform state:*)", "Bash(terraform taint:*)",
    "Bash(tofu apply:*)", "Bash(tofu destroy:*)", "Bash(tofu import:*)", "Bash(tofu state:*)",
    "Bash(terragrunt apply:*)", "Bash(terragrunt destroy:*)", "Bash(terragrunt run-all apply:*)",
    "Bash(cdk deploy:*)", "Bash(cdk destroy:*)", "Bash(cdk bootstrap:*)", "Bash(cdktf deploy:*)", "Bash(cdktf destroy:*)",
    "Bash(pulumi up:*)", "Bash(pulumi destroy:*)", "Bash(pulumi refresh:*)", "Bash(kubectl apply:*)", "Bash(kubectl delete:*)",
    "Bash(kubectl replace:*)", "Bash(kubectl patch:*)", "Bash(helm install:*)", "Bash(helm upgrade:*)", "Bash(helm uninstall:*)",
    "Bash(sam deploy:*)", "Bash(serverless deploy:*)", "Bash(sls deploy:*)", "Bash(sst deploy:*)", "Bash(amplify publish:*)",
    "Bash(fly deploy:*)", "Bash(flyctl deploy:*)", "Bash(wrangler deploy:*)", "Bash(wrangler publish:*)", "Bash(vercel --prod:*)",
    "Bash(vercel deploy:*)", "Bash(netlify deploy:*)", "Bash(railway up:*)", "Bash(aws cloudformation deploy:*)",
    "Bash(aws cloudformation delete-stack:*)", "Bash(aws s3 rm:*)", "Bash(aws s3 sync:*)", "Bash(aws iam:*)",
    # privilege, containers, the Claude binary and the SDK (CS-2)
    "Bash(sudo:*)", "Bash(su:*)", "Bash(doas:*)", "Bash(docker run --privileged:*)", "Bash(docker push:*)", "Bash(claude:*)",
    "Bash(npx @anthropic-ai/claude-code:*)", "Bash(npx -y @anthropic-ai/claude-code:*)",
    # push to protected refs, force pushes, GitHub mutations
    "Bash(git push origin main:*)", "Bash(git push origin master:*)", "Bash(git push --force:*)",
    "Bash(git push -f:*)", "Bash(git push --delete:*)", "Bash(git push --mirror:*)", "Bash(git push --tags:*)", "Bash(git commit --no-verify:*)",
    "Bash(gh pr merge:*)", "Bash(gh release:*)", "Bash(gh secret:*)", "Bash(gh workflow run:*)", "Bash(gh repo edit:*)",
    # secrets files
    "Read(**/.env)", "Read(**/.env.local)", "Read(**/.env.test)", "Read(**/.env.development)", "Read(**/.env.production)",
    "Read(**/.env.production.*)", "Read(**/.env.staging)", "Read(**/.env.*.local)", "Read(~/.aws/**)", "Read(~/.ssh/**)", "Read(~/.config/gh/**)",
    # guardrail paths (H6)
    "Edit(.claude/settings.json)", "Edit(.claude/settings.local.json)", "Edit(.claude/hooks/**)", "Edit(.claude/scripts/**)",
    "Edit(.claude/agents/**)", "Edit(.claude/skills/**)", "Edit(.claude/commands/**)", "Edit(.github/workflows/**)", "Edit(.github/CODEOWNERS)",
    "Edit(.mcp.json)", "Edit(.claude-plugin/**)", "Edit(.git/hooks/**)", "Edit(.git/config)",
    # the MCP tool plane (§5.6) and Workflow (RC-17)
    "mcp__*__run_script", "mcp__*deploy*", "Workflow",
]

ALLOW_EXAMPLE = [
    "Read", "Grep", "Glob", "Edit", "Write",
    "Bash(npm run test:*)", "Bash(npm run lint:*)", "Bash(npm run typecheck:*)", "Bash(npm run build:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git branch --show-current:*)",
]
ASK_EXAMPLE = ["Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git merge:*)"]


# ---------------------------------------------------------------------------------------------------------------------

def load_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MANIFEST_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    check_manifest(data)
    return data


def check_manifest(data: Mapping[str, Any]) -> None:
    ids = set()
    for e in data.get("entries", []):
        if e.get("id") in ids:
            raise ValueError("duplicate manifest entry id %r" % e.get("id"))
        ids.add(e.get("id"))
        for k in ("id", "event"):
            if not e.get(k):
                raise ValueError("manifest entry without %s: %r" % (k, e))
        if not (e.get("guards") or e.get("script")):
            raise ValueError("manifest entry %s has neither guards nor script" % e["id"])


def command_for(entry: Mapping[str, Any], root: str = ROOT_COPY, scripts: str = SCRIPTS_COPY) -> str:
    q = '"%s/%s' % (root, scripts)
    if entry.get("script"):
        target = '%s/%s"' % (q, entry["script"])
        banner = json.dumps({"hookSpecificOutput": {"hookEventName": entry["event"], "additionalContext": NO_PYTHON_BANNER}})
        return "if command -v python3 >/dev/null 2>&1; then exec python3 %s; fi; echo '%s'" % (target, banner)
    run = '%s/guards/run.py"' % q
    args = "%s --id %s" % (entry["event"], entry["id"])
    if entry.get("blocking"):
        return ("command -v python3 >/dev/null 2>&1 || { echo \"%s\" >&2; exit 2; }; exec python3 %s %s" % (NO_PYTHON_STDERR, run, args))
    if entry["event"] == "SessionStart":
        banner = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": NO_PYTHON_BANNER}})
        return "if command -v python3 >/dev/null 2>&1; then exec python3 %s %s; fi; echo '%s'" % (run, args, banner)
    return "command -v python3 >/dev/null 2>&1 && exec python3 %s %s; exit 0" % (run, args)


def handler_for(entry: Mapping[str, Any], root: str = ROOT_COPY, scripts: str = SCRIPTS_COPY) -> Dict[str, Any]:
    h = {"type": "command", "command": command_for(entry, root, scripts), "timeout": int(entry.get("timeout", 10))}  # type: Dict[str, Any]
    if entry.get("blocking"):
        h["onFailure"] = "block"
    if entry.get("async"):
        h["async"] = True
    return h


def build_hooks(manifest: Mapping[str, Any], root: str = ROOT_COPY, scripts: str = SCRIPTS_COPY) -> Dict[str, Any]:
    hooks = {}   # type: Dict[str, List[Dict[str, Any]]]
    for e in manifest["entries"]:
        group = {}   # type: Dict[str, Any]
        if e.get("matcher"):
            group["matcher"] = e["matcher"]
        group["hooks"] = [handler_for(e, root, scripts)]
        hooks.setdefault(e["event"], []).append(group)
    return hooks


def build_settings(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """The staged `.claude/settings.v2.json`: the live file's shape (IMP-13) with the v2 hooks block. The project's
    permissions stay the project's (A28, CO-33): only the empty allow list the live file already has is carried over."""
    return {"permissions": {"allow": []}, "hooks": build_hooks(manifest)}


def build_plugin_hooks(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    return {"hooks": build_hooks(manifest, ROOT_PLUGIN, SCRIPTS_PLUGIN)}


def _sandbox_block() -> Dict[str, Any]:
    with open(SANDBOX_TEMPLATE, "r", encoding="utf-8") as fh:
        return json.load(fh)["sandbox"]


def build_templates(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The §7.2 templates. Files under .claude/templates/settings/ are generated from this function."""
    deny = list(DENY_BASELINE)
    sandbox = _sandbox_block()
    hooks = build_hooks(manifest)
    headless_allow = [a for a in ALLOW_EXAMPLE if not a.startswith("Bash(git ")] + ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
                                                                                      "Bash(git show:*)"]
    out = {}   # type: Dict[str, Dict[str, Any]]
    out["deny-baseline.json"] = {"permissions": {"deny": deny}}
    out["interactive.settings.json"] = {
        "permissions": {"allow": list(ALLOW_EXAMPLE), "ask": list(ASK_EXAMPLE), "deny": deny},
        "enableAllProjectMcpServers": False,
        "sandbox": copy.deepcopy(sandbox),
    }
    out["headless-child.settings.json"] = {
        "permissions": {"defaultMode": "dontAsk", "allow": headless_allow, "deny": deny},
        "enableAllProjectMcpServers": False,
        "sandbox": copy.deepcopy(sandbox),
        "env": {},
        "hooks": copy.deepcopy(hooks),
    }
    critic_deny = deny + ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash(npm install:*)", "Bash(npm i:*)", "Bash(npm ci:*)",
                          "Bash(pnpm install:*)", "Bash(yarn add:*)", "Bash(pip install:*)"]
    out["critic.settings.json"] = {
        "permissions": {"defaultMode": "dontAsk", "allow": ["Read", "Grep", "Glob", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
                                                            "Bash(git show:*)", "Bash(npm run test:*)", "Bash(npm run lint:*)",
                                                            "Bash(npm run typecheck:*)"],
                        "deny": critic_deny},
        "enableAllProjectMcpServers": False,
        "sandbox": copy.deepcopy(sandbox),
        "env": {},
        "hooks": copy.deepcopy(hooks),
    }
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def expected_files(manifest: Mapping[str, Any]) -> Dict[str, str]:
    files = {SETTINGS_V2: dumps(build_settings(manifest))}
    for name, obj in build_templates(manifest).items():
        files[os.path.join(TEMPLATE_DIR, name)] = dumps(obj)
    return files


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--print", action="store_true", dest="do_print")
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--plugin", action="store_true")
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.do_print:
        sys.stdout.write(dumps(build_settings(manifest)))
        return 0
    if args.plugin:
        sys.stdout.write(dumps(build_plugin_hooks(manifest)))
        return 0
    files = expected_files(manifest)
    if args.write:
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        for path, text in files.items():
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("wrote %s" % os.path.relpath(path, REPO_ROOT))
        return 0
    bad = []
    for path, text in files.items():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cur = fh.read()
        except OSError:
            cur = None
        if cur != text:
            bad.append(os.path.relpath(path, REPO_ROOT))
    if bad:
        sys.stderr.write("out of date (run `python3 .claude/scripts/guards/wiring.py --write`): %s\n" % ", ".join(bad))
        return 1
    print("wiring: %d generated files match the manifest" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
