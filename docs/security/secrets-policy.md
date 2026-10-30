# Secrets policy

One policy replaces the four contradictory statements the v1 kit had (report section 5.4).

## Where secrets may live

- **Locally**, environment variables and `.env.local` / `.env.test` may hold **development and test values only**: a local database URL, a Stripe test key, a throwaway token. Never a production value.
- **Deployed environments** use a secret manager. Provisioning a secret is a human act (H6).
- **Shared or production database credentials are never present locally.** A local port-forward defeats checking `DATABASE_URL` for a remote host, so tunnels and forwards (`ssh -L`, `kubectl port-forward`, `aws ssm start-session`) are denied to agents.

## Files and ignores

Append `${CLAUDE_PLUGIN_ROOT}/templates/security/gitignore.snippet` to your `.gitignore`. It ignores `.env*` except `.env.example`, key files (`*.pem`, `*.key`, `*.p12`, `id_rsa*`), `.netrc`, `.pypirc`, Terraform state and plans, and the kit's per-user files.

- `.worktreeinclude` lists only `.env.local` and `.env.test`.
- `.env.production*`, `*.pem`, `*.key` and cloud credential directories are never copied into a worktree. They are covered by the sandbox `denyRead` list, because a `Read(**/.env*)` deny rule alone is bypassed by `cat`.

## Never in artifacts

Secrets never appear in briefs, reports, evidence, events, pull request bodies, memory or primers. The events writer redacts known secret formats (`kitlib/redact.py`). What the redaction recognises (`kitlib/redact.py`):

- **Key names are identifiers, not words.** `DB_PASSWORD=`, `API_SECRET=`, `STRIPE_SECRET_KEY=`, `db_password=`, `apiSecret=`, `dbPassword:`, `clientsecret=` and `SECRETKEY=` all match. The suffix list is closed (`_key _id _value _secret _token _password _hash _salt _string _data _b64`), so `token_budget_per_item: 150000`, `input_tokens: 12` and `auth: required` stay readable.
- **Token shapes** are redacted wherever they appear: `ghp_`, `gho_`, `ghs_`, `github_pat_`, `xox[abprs]-`, `AKIA...`, `AIza...`, `sk_live_`, `rk_live_`, `npm_`, `sk-ant-`, `sk-proj-` / `sk-svcacct-` / `sk-admin-`, `glpat-`, `whsec_`, PyPI `pypi-AgEIcHlwaS5vcmc...` tokens and `AccountKey=`.
- **A placeholder excuses a value only as the whole value** (`changeme`, `<token>`, `example-password-1`). A placeholder-looking word inside a longer secret does not.

The secrets gate G11 (`loopctl/gates/g11_secrets.py`) applies the same rules to what an item adds. Unquoted values count in `.env*`, yaml, ini, cfg, conf, properties and toml files, and in `KEY=value` lines elsewhere (at least eight characters, with a letter and a digit). Findings that are specific to the gate: `g11.untracked-overflow` (blocks: more than 50 untracked files were not scanned), `g11.ignored-config` (an ignored `conftest.py`, `*.pth`, `sitecustomize.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `.coveragerc` or executable file in the item worktree, or a tracked file marked assume-unchanged or skip-worktree: it can run code or change what is tested without showing up in the diff) and `g11.ignored-incomplete` (warning: the ignored-file listing was too long to read). `.env`, `.env.*`, `.envrc` and dependency and cache directories are not scanned for this.

Run gitleaks or trufflehog on every loop commit and every evidence artifact, both as a pre-commit hook and as a required CI check; `secrets.scanner` warns when neither is configured, and the required-checks rule in [rulesets.md](rulesets.md) expects a `secrets-scan` check.

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

## Limits

The built-in secrets scan (G11) and the redaction the kit applies to events, reports and logs are heuristic readers. They catch honest mistakes and cheap evasions, and they fail closed where they can: text they cannot read, or that has more matches than a file should, is a blocking finding and not a pass. They are not complete, and a clean result is not evidence that no secret is present. Bounds are part of that: a quoted value is read up to 4096 characters, and past that the rest of the line is redacted whole; a `${NAME:-default}` reference is read when it is nested at most four deep and shorter than 1100 characters, and past that it is read as a literal; a text with more than 20000 matches is one finding. A value in a shape they do not know, built up from parts, or hidden behind an indirection (a command in a script, a value passed through a step output) goes through. The checks that hold are the ones outside the agent's reach: the external scanner in CI (`secrets_scan_cmd`, gitleaks) and the human apply gate. The gaps already known are listed under "Known gaps" in the changelog entry for the secrets and supply-chain gates (the 2.0.0-rc.1 entry of `CHANGELOG.md`; the fragment `changelog.d/fix-b-gates-secrets-supply.md` has them round by round).
