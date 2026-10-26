# Secrets policy

One policy replaces the four contradictory statements the v1 kit had (report section 5.4).

## Where secrets may live

- **Locally**, environment variables and `.env.local` / `.env.test` may hold **development and test values only**: a local database URL, a Stripe test key, a throwaway token. Never a production value.
- **Deployed environments** use a secret manager. Provisioning a secret is a human act (H6).
- **Shared or production database credentials are never present locally.** A local port-forward defeats checking `DATABASE_URL` for a remote host, so tunnels and forwards (`ssh -L`, `kubectl port-forward`, `aws ssm start-session`) are denied to agents.

## Files and ignores

Append `.claude/templates/security/gitignore.snippet` to your `.gitignore`. It ignores `.env*` except `.env.example`, key files (`*.pem`, `*.key`, `*.p12`, `id_rsa*`), `.netrc`, `.pypirc`, Terraform state and plans, and the kit's per-user files.

- `.worktreeinclude` lists only `.env.local` and `.env.test`.
- `.env.production*`, `*.pem`, `*.key` and cloud credential directories are never copied into a worktree. They are covered by the sandbox `denyRead` list, because a `Read(**/.env*)` deny rule alone is bypassed by `cat`.

## Never in artifacts

Secrets never appear in briefs, reports, evidence, events, pull request bodies, memory or primers. The events writer redacts known secret formats (`kitlib/redact.py`). Run gitleaks or trufflehog on every loop commit and every evidence artifact, both as a pre-commit hook and as a required CI check; `secrets.scanner` warns when neither is configured, and the required-checks rule in [rulesets.md](rulesets.md) expects a `secrets-scan` check.

## What the doctor checks

| Check id | Fails when |
|---|---|
| `secrets.tracked` | `.env*` (other than `.env.example`, `.env.sample`, `.env.template`), a private key, Terraform state or a credentials file is tracked. `.env.test` and `*.auto.tfvars` are warnings |
| `secrets.gitignore` | `.env`, `.env.local`, `.env.test`, `.env.production`, `*.pem` or `*.key` would not be ignored, or `.env.example` would be |
| `secrets.worktreeinclude` | `.worktreeinclude` lists anything besides `.env.local` and `.env.test` |
| `secrets.scanner` | no gitleaks or trufflehog in pre-commit or CI |
| `secrets.in-kit-files` | a secret-shaped string in `loop.config.md`, the context primers or `docs/features` |
| `cred.tree.*`, `cred.shellrc`, `cred.history` | production env files, keys and exported or typed credentials ([credentials.md](credentials.md)) |

## If a secret leaked

Rotate it first. Then remove it from the working tree, the index and history as your team does for any leak. The kit cannot un-leak a value; it only stops new ones.
