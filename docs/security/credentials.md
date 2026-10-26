# Credentials (B2)

No mutating cloud, database or deploy credential, and no credential of yours for GitHub, may be reachable by the OS user the agent runs as. The agent can run arbitrary code as that user, so "reachable" means a process of that user could read it or ask something to hand it over.

`python3 .claude/scripts/doctor.py --credentials` looks for every route below. It reports names and paths, never values. Any error-level hit caps the effective level at **L1**, and the message says what to remove.

## What it looks for

| Route | Check ids | Notes |
|---|---|---|
| Environment variables | `cred.env.*` | `AWS_*` (except region, pager and similar), `GH_TOKEN`, `GITHUB_TOKEN` and friends, `GOOGLE_*`, `AZURE_*`, `ARM_*`, `KUBECONFIG`, deploy-host tokens (Vercel, Netlify, Fly, Cloudflare, Pulumi, Terraform Cloud, Supabase), `DOCKER_HOST`, registry and publish tokens (npm, PyPI, Cargo, RubyGems), `SSH_AUTH_SOCK`, `GIT_ASKPASS`, `GIT_CONFIG_COUNT`-style git config injection, `NETRC`, password-manager sessions, database URLs with credentials for a non-local host, and any value that matches a known secret format. Stripe test keys and local database URLs are allowed (dev and test values may live locally) |
| AWS | `cred.aws.profile`, `cred.aws.sso-cache` | Every profile with static keys, `credential_process`, SSO, a role, a web-identity file or a credential source; `AWS_SHARED_CREDENTIALS_FILE` and `AWS_CONFIG_FILE` are followed; SSO, CLI and `aws login` caches. A read-only profile is still a hit |
| GitHub CLI | `cred.gh.hosts` | `hosts.yml` logins, with the token in the file or in the OS keychain. A login that is only the bot is fine |
| git | `cred.git.config` (`cred.git.credential-helper`, `cred.git.extraheader`, `cred.git.url-credential`, `cred.git.ssh-command`), `cred.git.credentials-file`, `cred.git.submodules` | Credential helpers at system, global, local and worktree level, `http.*.extraheader` (what `actions/checkout` leaves behind), URL rewrites and remotes with embedded credentials, `core.sshCommand` with a key, `~/.git-credentials`, and the same inside submodule and per-worktree configs |
| netrc | `cred.netrc` | `~/.netrc`, `_netrc`, `$NETRC` |
| SSH | `cred.ssh.agent`, `cred.ssh.keys`, `cred.ssh.identity-files` | `SSH_AUTH_SOCK` set at all, agent sockets in known places (1Password, gpg-agent, launchd, systemd), `IdentityAgent`, private keys in `~/.ssh` (unencrypted is an error, passphrase-protected a warning) and keys named by `IdentityFile` outside it |
| Containers | `cred.docker.socket`, `cred.docker.config`, `cred.docker.contexts`, `cred.groups` | Docker, podman and containerd sockets (a socket is root on the machine), registry logins and credential helpers, remote docker contexts, membership of the docker, lxd, libvirt or disk groups |
| Kubernetes | `cred.kube` | Contexts in `~/.kube/config` and `$KUBECONFIG`, and the in-cluster service account token |
| Other clouds and hosts | `cred.files.*` | gcloud, Azure, Terraform Cloud, Vault, Fly, Vercel, Netlify, Wrangler, Railway, doctl, Pulumi, Firebase, Supabase, Stripe, Heroku, GitLab, Helm |
| Package registries and databases | `cred.files.*`, `cred.repo-files.*` | npm, Yarn, Bun, PyPI, pip, Poetry, Cargo, RubyGems, NuGet, Maven, Gradle, Composer; `.pgpass`, `.my.cnf`, `.pg_service.conf`. The same files lying in the repository are `cred.repo-files.*` |
| Secret stores | `cred.files.pass`, `cred.keyring` | `pass` stores, 1Password and Bitwarden CLI data, gpg private keys, a D-Bus session (secret service) |
| Startup files | `cred.shellrc` | Credentials exported from `.zshrc`, `.bashrc`, `.profile`, fish config, `BASH_ENV`, `.envrc` and `.env`: they reach every shell the agent starts even though the doctor's own environment looks clean |
| Shell history | `cred.history` | Secret-shaped text in the tail of shell and REPL history |
| Working tree | `cred.tree.*` | `.env.production*`, private keys, Terraform state and kubeconfigs, tracked or not, in the repository |
| Metadata endpoints | `cred.net.metadata` | 169.254.169.254 (cloud instance metadata), 169.254.170.2 (ECS task credentials) and 169.254.170.23 (EKS Pod Identity). Probed only with `--live`; without it the result is `unknown` unless the sandbox network settings deny 169.254.0.0/16 |
| Privilege | `cred.sudo` | Passwordless sudo |

With `--live` it also runs, from a plain terminal: `aws sts get-caller-identity` for every profile, `gh auth status`, `git credential fill` for github.com, and `ssh -T git@github.com`. Each passes only if the answer is nothing or the bot (`bot.login`).

## What it cannot see

Say these out loud to yourself before you trust a clean run:

- **Your running processes.** On Linux a process can read the environment of the same user's other processes; an IDE or terminal you left open may hold tokens the doctor never sees.
- **An unlocked keychain.** On macOS, `security find-internet-password` hands over the stored GitHub password for the logged-in user, with no helper configured. The Linux secret service is the same through D-Bus.
- **Browser sessions and cookies**, a cloud console you are signed into, an `ssh` connection you left open with agent forwarding.
- **Dashboard settings** on a git-integrated host (see [deploy-surface.md](deploy-surface.md)).

`cred.same-user` says so while `agent_isolation` is `none`. The answer is not a cleverer scan; it is to stop sharing the identity:

1. **Run the agent as a separate OS user** that has none of the above (no keychain, no shell history, no credentials), or
2. **run it in a credential-free container** (see `.claude/templates/security/devcontainer/devcontainer.json`), or
3. both, with the OS sandbox on top ([sandbox.md](sandbox.md)).

Then run the doctor **as that user, in that environment**, and set `agent_isolation` in `.claude/loop.config.md`. The doctor evaluates the environment it is started in: if you run it from your own shell and start the agent from another, it has checked the wrong machine.

## Devcontainers and Codespaces

VS Code and Codespaces forward the host's git credentials and SSH agent into a container by default. The template sets `remote.containers.copyGitConfig` to false, `dev.containers.gitCredentialHelperConfigLocation` to `none`, and blanks the credential variables in `remoteEnv`. The first two are partly **host-side** VS Code settings: check your user settings too. Do not mount `~/.ssh`, `~/.aws`, `~/.config/gh` or a docker socket, do not add the docker-in-docker feature, and do not forward an SSH agent. `devcontainer.lint` checks all of that in `.devcontainer/` files.

## The agent's own environment

The kit's driver strips `AWS_*`, `GOOGLE_*`, `AZURE_*`, `KUBECONFIG`, `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` and deploy tokens from the child environment (report section 8.3). Do the same for any other way you start the agent.

## Read-only cloud access is also a hit

`terraform plan`, `cdk diff` and `pulumi preview` run provider and program code, so a "read-only" credential used from the agent's machine is still code execution with that credential. Plans run in CI after a policy check, with a plan-only role reached through OIDC ([infra-human-gate.md](infra-human-gate.md)). A read-only IAM policy example for that role is in `.claude/templates/security/iam/plan-readonly.policy.json`.

## Fixing a hit

Remove or relocate the credential, then run the doctor again in the agent's own environment. If the hit is the bot's own credential (a credential helper that returns only the bot), prove it with `--live`: the helper passes when `git credential fill` returns the bot's login.
