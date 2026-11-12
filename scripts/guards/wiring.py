#!/usr/bin/env python3
"""Emit the guard wiring from `hooks.manifest.json` (report §7.1, §7.2; IMP-13, IMP-16).

    wiring.py --print                 the plugin hooks.json content on stdout
    wiring.py --write                 write hooks/hooks.json, the templates under templates/settings/ and
                                      templates/security/managed-settings.example.json
    wiring.py --check [--project DIR] exit 1 if a generated file differs from what the manifest generates, a script that
                                      hooks.json runs is missing, or (with --project) the project's settings switch the
                                      guards off or wire kit scripts a second time (CI runs this)
    wiring.py --sandbox-snippet       the sandbox denyWrite / Edit-deny lines with the ABSOLUTE git-dir paths of this checkout, to paste
                                      into .claude/settings.local.json (writes nothing; needed in a linked worktree, where the git
                                      directory is outside the project directory, D1)

The manifest is the single source, and the plugin's `hooks/hooks.json` is the only wiring: every handler runs
`${CLAUDE_PLUGIN_ROOT}/scripts/...`. A project never wires the guards in its own settings. What a project's settings can
do is switch them off (`disableAllHooks`, or the plugin set to false in `enabledPlugins`); `project_problems` reports that.

Handler hygiene (A29, CO-32): quoted root variable, a `timeout` on every handler, no network, `onFailure: block` on every
blocking-capable event (the field's name and placement are unverified, V9/IMP-16, so every entry point also fails closed
itself), and a shell prologue that exits 2 when python3 is missing, because a missing interpreter would otherwise be an
exit 127 that the platform treats as a non-blocking error. A missing python3 at SessionStart prints a banner instead.
Every handler runs `python3 -B`: the plugin's folder is an installed copy, and a hook must not leave `__pycache__` in it.
`if:` filters are not used: the entry's `matcher` already limits which tools spawn python, and the Bash guard has to see
every Bash call, since a wrapped command cannot be told apart from a plain one without parsing it.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(HERE)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
from kitlib import kitpaths  # noqa: E402

REPO_ROOT = kitpaths.KIT_ROOT
MANIFEST_PATH = os.path.join(HERE, "hooks.manifest.json")
HOOKS_JSON = kitpaths.kit("hooks", "hooks.json")
TEMPLATE_DIR = kitpaths.kit("templates", "settings")
SANDBOX_TEMPLATE = kitpaths.kit("templates", "security", "sandbox.settings.json")
MANAGED_EXAMPLE = kitpaths.kit("templates", "security", "managed-settings.example.json")
# where the managed-settings example expects a read-only copy of the whole plugin folder (owned by a user the agent is not)
MANAGED_ROOT = "/opt/chain-of-custody"
MANAGED_DENY = ["Workflow", "mcp__*__run_script", "mcp__*deploy*"]

ROOT_PLUGIN = kitpaths.PLUGIN_ROOT_VAR
SCRIPTS_PLUGIN = "scripts"

NO_PYTHON_STDERR = "kit guards: python3 not found; failing closed (rule guards:no-python3)"
NO_PYTHON_BANNER = ("kit guards: python3 was not found, so the guards and the liveness canary did NOT load. Install python3 "
                    "(3.9 or newer); until then the effective level is L0 (rule guards:no-python3).")

# ---------------------------------------------------------------------------------------------------------------------
# the deny block (report §7.2). A typo guard that works even in bypass mode (P25).

# deploy-class MCP tools (IN-10). The same list is `templates/infra/mcp-deploy-deny.settings.json` and `templates/security/mcp-lockdown.settings.json`;
# tests/guards/test_sandbox_template.py keeps the three equal. (A constant, not a file read: the doctor imports this module in a plugin install too.)
MCP_DEPLOY_DENY = [
    "mcp__*__run_script", "mcp__*deploy*", "mcp__*apply*", "mcp__*destroy*", "mcp__*terminate*", "mcp__*provision*", "mcp__*rollout*", "mcp__*change_set*",
    "mcp__*__create_stack", "mcp__*__update_stack", "mcp__*__delete_stack",
]

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
    *MCP_DEPLOY_DENY, "Workflow",
]

# the rest of the H6 write set as Edit rules (D1): one list, `paths.SANDBOX_DENY_WRITE`, shared with the sandbox template and the doctor
from guards import paths as _paths  # noqa: E402

DENY_BASELINE += [r for r in (_paths.sandbox_edit_rule(e) for e in _paths.SANDBOX_DENY_WRITE) if r not in DENY_BASELINE]

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


def command_for(entry: Mapping[str, Any], root: str = ROOT_PLUGIN, scripts: str = SCRIPTS_PLUGIN) -> str:
    q = '"%s/%s' % (root, scripts)
    if entry.get("script"):
        target = '%s/%s"' % (q, entry["script"])
        banner = json.dumps({"hookSpecificOutput": {"hookEventName": entry["event"], "additionalContext": NO_PYTHON_BANNER}})
        return "if command -v python3 >/dev/null 2>&1; then exec python3 -B %s; fi; echo '%s'" % (target, banner)
    run = '%s/guards/run.py"' % q
    args = "%s --id %s" % (entry["event"], entry["id"])
    if entry.get("blocking"):
        return ("command -v python3 >/dev/null 2>&1 || { echo \"%s\" >&2; exit 2; }; exec python3 -B %s %s" % (NO_PYTHON_STDERR, run, args))
    if entry["event"] == "SessionStart":
        banner = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": NO_PYTHON_BANNER}})
        return "if command -v python3 >/dev/null 2>&1; then exec python3 -B %s %s; fi; echo '%s'" % (run, args, banner)
    return "command -v python3 >/dev/null 2>&1 && exec python3 -B %s %s; exit 0" % (run, args)


def handler_for(entry: Mapping[str, Any], root: str = ROOT_PLUGIN, scripts: str = SCRIPTS_PLUGIN) -> Dict[str, Any]:
    h = {"type": "command", "command": command_for(entry, root, scripts), "timeout": int(entry.get("timeout", 10))}  # type: Dict[str, Any]
    if entry.get("blocking"):
        h["onFailure"] = "block"
    if entry.get("async"):
        h["async"] = True
    return h


def build_hooks(manifest: Mapping[str, Any], root: str = ROOT_PLUGIN, scripts: str = SCRIPTS_PLUGIN) -> Dict[str, Any]:
    hooks = {}   # type: Dict[str, List[Dict[str, Any]]]
    for e in manifest["entries"]:
        group = {}   # type: Dict[str, Any]
        if e.get("matcher"):
            group["matcher"] = e["matcher"]
        group["hooks"] = [handler_for(e, root, scripts)]
        hooks.setdefault(e["event"], []).append(group)
    return hooks


def build_plugin_hooks(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """The plugin's hooks/hooks.json."""
    return {"hooks": build_hooks(manifest)}


def hook_scripts(manifest: Mapping[str, Any]) -> List[str]:
    """The kit scripts the hooks run, relative to scripts/."""
    out = ["guards/run.py"]
    for e in manifest["entries"]:
        if e.get("script") and e["script"] not in out:
            out.append(e["script"])
    return out


def _sandbox_block() -> Dict[str, Any]:
    with open(SANDBOX_TEMPLATE, "r", encoding="utf-8") as fh:
        return json.load(fh)["sandbox"]


def build_templates(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The §7.2 templates. Files under templates/settings/ are generated from this function. They carry no hooks: the
    guards come from the plugin, in a headless child as in an interactive session."""
    deny = list(DENY_BASELINE)
    sandbox = _sandbox_block()
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
    }
    return out


def build_managed(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """templates/security/managed-settings.example.json. `allowManagedHooksOnly` turns off every user, project and PLUGIN hook,
    so the managed file has to carry the whole guard wiring itself, pointed at a read-only copy of the kit, not just the canary."""
    return {
        "allowManagedHooksOnly": True,
        "enableAllProjectMcpServers": False,
        "permissions": {"disableBypassPermissionsMode": "disable", "deny": list(MANAGED_DENY)},
        "hooks": build_hooks(manifest, root=MANAGED_ROOT),
    }


def sandbox_snippet(cwd: str) -> Dict[str, Any]:
    """The settings fragment that denies sandboxed writes to the git directory's hooks, config, info and the session registry by absolute path, and, in a linked worktree, the `.git` pointer file.
    In a plain checkout the relative entries of the sandbox template already cover them; in a linked worktree `.git` is a file and only these do."""
    from kitlib import gitutil
    common = gitutil.common_dir(cwd)
    if not common:
        raise ValueError("not inside a git repository: %s" % cwd)
    entries = [os.path.join(common, "hooks"), os.path.join(common, "config"), os.path.join(common, "info"), os.path.join(common, "loopctl", "sessions")]
    ed = ["/" + e for e in entries]       # a permission rule spells an absolute path with a leading `//` [probe]
    deny = ["Edit(%s)" % ed[1]] + ["Edit(%s/**)" % e for e in ed[:1] + ed[2:]]
    # a linked worktree: `.git` is a pointer FILE inside the project directory; rewriting it moves every hook to a git directory the agent chose (R-A-4)
    top = gitutil.toplevel(cwd)
    pointer = os.path.join(top, ".git") if top else ""
    if pointer and os.path.isfile(pointer):
        entries.append(pointer)
        deny.append("Edit(/%s)" % pointer)
    return {"sandbox": {"filesystem": {"denyWrite": entries}}, "permissions": {"deny": deny}}


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def expected_files(manifest: Mapping[str, Any]) -> Dict[str, str]:
    files = {HOOKS_JSON: dumps(build_plugin_hooks(manifest))}
    for name, obj in build_templates(manifest).items():
        files[os.path.join(TEMPLATE_DIR, name)] = dumps(obj)
    files[MANAGED_EXAMPLE] = dumps(build_managed(manifest))
    return files


def kit_problems(manifest: Mapping[str, Any]) -> List[str]:
    """Generated files that differ from the manifest, and hook scripts that do not exist."""
    bad = []   # type: List[str]
    for path, text in expected_files(manifest).items():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cur = fh.read()
        except OSError:
            cur = None
        if cur != text:
            bad.append("%s %s" % (os.path.relpath(path, REPO_ROOT), "is missing" if cur is None else "differs from what the manifest generates"))
    for rel in hook_scripts(manifest):
        if not os.path.isfile(os.path.join(SCRIPTS_DIR, rel)):
            bad.append("scripts/%s is missing: a hook runs it" % rel)
    return bad


# -- a project's settings -----------------------------------------------------------------------------------------------

def plugin_name() -> str:
    try:
        with open(kitpaths.PLUGIN_JSON, "r", encoding="utf-8") as fh:
            name = json.load(fh).get("name")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError, AttributeError):
        pass
    return "chain-of-custody"


def plugin_keys(enabled: Any, name: Optional[str] = None) -> List[str]:
    """The `enabledPlugins` keys that name this kit: `<name>@<marketplace>` (or a bare `<name>`)."""
    name = name or plugin_name()
    if not isinstance(enabled, dict):
        return []
    return [k for k in enabled if isinstance(k, str) and (k == name or k.startswith(name + "@"))]


def hook_commands(data: Any) -> List[Tuple[str, str]]:
    """(event, command) for every command hook in a settings object."""
    out = []   # type: List[Tuple[str, str]]
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []):
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    out.append((str(event), h["command"]))
    return out


def kit_script_re(manifest: Mapping[str, Any]) -> "re.Pattern[str]":
    """A hook command that runs a kit hook script from some `scripts/` directory (a copy install's `.claude/scripts`, the plugin's)."""
    return re.compile(r"scripts/(?:%s)\b" % "|".join(re.escape(r) for r in hook_scripts(manifest)))


def settings_problems(data: Any, where: str, manifest: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """(kind, message) for each way one settings object switches the guards off or wires them a second time.
    kind: `disable-all-hooks` | `plugin-disabled` | `double-wired`."""
    if not isinstance(data, dict):
        return []
    out = []   # type: List[Tuple[str, str]]
    if data.get("disableAllHooks") is True:
        out.append(("disable-all-hooks", "%s sets disableAllHooks: no guard runs" % where))
    enabled = data.get("enabledPlugins")
    for k in plugin_keys(enabled):
        if enabled[k] is False:
            out.append(("plugin-disabled", "%s sets enabledPlugins %s to false: the kit and its guards are off" % (where, k)))
    rx = kit_script_re(manifest)
    wired = sorted({event for event, cmd in hook_commands(data) if rx.search(cmd)})
    if wired:
        out.append(("double-wired", "%s wires kit hook scripts itself (%s): the plugin's hooks/hooks.json is the only wiring; "
                                    "remove these entries" % (where, ", ".join(wired))))
    return out


def project_problems(project: str, manifest: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """settings_problems for the project's .claude/settings.json and .claude/settings.local.json."""
    out = []   # type: List[Tuple[str, str]]
    for name in ("settings.json", "settings.local.json"):
        path = os.path.join(project, ".claude", name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            out.append(("unreadable", "%s is unreadable: %s" % (path, exc)))
            continue
        out.extend(settings_problems(data, path, manifest))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--print", action="store_true", dest="do_print")
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--sandbox-snippet", action="store_true", dest="sandbox_snippet")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--project", default=None, help="with --check: also check this project's .claude/settings*.json")
    args = ap.parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.do_print:
        sys.stdout.write(dumps(build_plugin_hooks(manifest)))
        return 0
    if args.sandbox_snippet:
        try:
            sys.stdout.write(dumps(sandbox_snippet(os.getcwd())))
        except ValueError as exc:
            sys.stderr.write("wiring: %s\n" % exc)
            return 2
        return 0
    if args.write:
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        for path, text in expected_files(manifest).items():
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("wrote %s" % os.path.relpath(path, REPO_ROOT))
        return 0
    bad = kit_problems(manifest)
    if bad:
        sys.stderr.write("out of date (run `python3 %s --write`): %s\n" % (kitpaths.script("guards/wiring.py"), "; ".join(bad)))
    proj = [m for _, m in project_problems(os.path.abspath(args.project), manifest)] if args.project else []
    for m in proj:
        sys.stderr.write("wiring: %s\n" % m)
    if bad or proj:
        return 1
    print("wiring: hooks/hooks.json and %d templates match the manifest%s" % (len(expected_files(manifest)) - 1,
                                                                             "; the project's settings leave the guards on" if args.project else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
