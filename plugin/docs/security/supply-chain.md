# Dependencies and supply chain

A hallucinated, typosquatted or hijacked package is code execution at install time. The kit treats new dependencies as a gate and installs defensively (report section 5.5).

## New-dependency gate

Any added top-level dependency is listed at the checkpoint or in the pull request body. At L3 and above it needs human approval. The gate checks:

- the package exists in the registry;
- its age and download count;
- its name distance from popular packages (slopsquatting and typosquatting);
- that its licence is on the allowlist.

The gate itself is part of the verification gates (loopctl, later phase). Until then the reviewer does these checks and the pull request body lists new dependencies.

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

The doctor does not scan dependencies. It checks the things that make the scanner trustworthy: no registry or publish token reachable by the agent (`cred.env.registry`, `cred.files.*`), deploy-class publish commands inventoried in CI (`deploy.surface`), and workflows pinned and least-privilege (`ci_lint.py`: `ci.floating-action-tag` warns when an action is pinned to a tag rather than a commit SHA).
