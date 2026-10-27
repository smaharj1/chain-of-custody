# Dependencies and supply chain

A hallucinated, typosquatted or hijacked package is code execution at install time. The kit treats new dependencies as a gate and installs defensively (report section 5.5).

## New-dependency gate

Any added top-level dependency is listed at the checkpoint or in the pull request body. At L3 and above it needs human approval. The gate checks:

- the package exists in the registry;
- its age and download count;
- its name distance from popular packages (slopsquatting and typosquatting);
- that its licence is on the allowlist.

The gate is G9 (`loopctl/gates/g9_supply.py`, [../../.claude/spec/loopctl-gates.md](../../.claude/spec/loopctl-gates.md)) and runs under `loopctl`. Without loopctl the reviewer does these checks and the pull request body lists new dependencies.

G9 also looks at where a dependency comes from, not only whether it is new:

| Finding id | Severity | What it catches |
|---|---|---|
| `g9.source-changed` | error (warn when `ext.g9.allow` names the package) | an existing dependency whose spec moves to a git, tarball, `file:`, `link:`, `portal:`, `owner/repo`, PEP 508 direct-reference, `{ git\|path\|url\|registry = ... }` or Gemfile `git:` / `path:` / `source:` source. A plain version bump is not flagged |
| `g9.index-changed` | error | a changed package index or resolver setting: `--index-url`, `-i`, `--extra-index-url`, `--find-links`, `--trusted-host`, `--pre`, `--no-index` in a requirements file; `registry=` lines in `.npmrc`, `.yarnrc*`, `pip.conf`, `uv.toml`, `.cargo/config*`; `[tool.uv]` index keys and sources, `tool.poetry.source`, `tool.pdm.source`; Cargo `[patch]`, `[replace]` and `[source]`; Gemfile `source`; go.mod `replace`; npm `overrides` / `resolutions` / `pnpm.overrides` that point at a non-registry source |

An `--index-url` reached through `-r other.txt` is seen only when `other.txt` is itself a changed `requirements*.txt`.

## Install hygiene

- `npm ci --ignore-scripts` (and the equivalent for pnpm, yarn and bun) is the default. A package that needs an install script goes on an explicit allowlist.
- CI installs from a frozen lockfile.
- Registries are pinned in `.npmrc` or `pip.conf`. Do not put a token in either file; the doctor flags `cred.repo-files.npm` and `cred.files.pip` when you do.
- A lockfile major bump, a new `.terraform.lock.hcl` entry and any change to `package.json` scripts (`postinstall`, `prepare`, `pretest`) are H3 paths: a code owner reviews them.

## Scanner-backed review

`security-reviewer` does not claim to check CVEs it cannot look up. Vulnerability data comes from `npm audit`, `osv-scanner` or `pip-audit`, run by the loop or by CI; the reviewer interprets that output.

## Optional

An SBOM (syft) and build provenance attestations at release time (H5).

## What the doctor does here

The doctor does not scan dependencies. It checks the things that make the scanner trustworthy: no registry or publish token reachable by the agent (`cred.env.registry`, `cred.files.*`), deploy-class publish commands inventoried in CI (`deploy.surface`), and workflows pinned and least-privilege (`ci_lint.py`: `ci.floating-action-tag` warns when an action is pinned to a tag rather than a commit SHA, and `ci.docker-uses-unpinned` flags a `docker://` image without a digest; see [split-ci.md](split-ci.md)).
