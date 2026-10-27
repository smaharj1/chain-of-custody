# Running an unknown repository's commands for the first time

On an existing repository the planner and the primer protocol tell the agent to run the execution-critical commands once (the test command, the run command, the ephemeral-database command) to prove them before writing them down. On a repository you have not vetted, those commands are unknown code. `.claude/scripts/probe_run.py` is the sandbox for that first run (CO-14, IMP-25).

## Use

```
python3 .claude/scripts/probe_run.py --repo . 
```

With no `--go` it **only prints**: the command list, where each command came from, and a short digest of the list. Nothing runs. The commands come from `verify_tests`, `verify_run` and `db_ephemeral` in `.claude/loop.config.md`, from fenced command blocks under "Run the App Locally", "Apply Migrations to the Durable Dev DB", "Seed Data" and "Reset the Local DB" in `.claude/context/local-dev.md`, and from any `--cmd` you add.

Read the list. If it is safe to run, tell the agent "go". The agent then runs:

```
python3 .claude/scripts/probe_run.py --repo . --image <an image with your toolchain> --go <digest>
```

The digest ties your "go" to the list you read: if the list changed, the digest no longer matches and nothing runs. `--image` has no default, because only you know which image has your toolchain.

## What the run does

Each command runs once in `docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges`, as your uid, with the repository mounted **read-only** at `/src` and copied into a tmpfs, no extra mounts, and **no environment passed through**: the docker client gets `PATH` and `HOME` only, so none of your credential variables enter the container. Output is truncated to the last lines and passed through the secret redactor. A server command (`verify_run`, "Run the App Locally") is expected not to exit: still running at the timeout counts as started, and the container is killed.

If docker is not available, the tool prints the commands and exits with status 3, and nothing runs on the host. Run them yourself somewhere with the network off.

The probe has no network, so a command that installs dependencies will fail unless they are already in the repository. That is the point: a command that needs the network to work is one you should look at before running.

## Exit codes

0 the plan was printed, or every command passed; 1 a command failed; 2 usage error or a digest mismatch; 3 docker unavailable.

## What this does not do

It does not make an untrusted repository safe to work in: it makes the first run of its commands observable and contained. The docker daemon is a root-equivalent resource, so run this on a machine where that is acceptable, never from inside the agent's own sandbox (which, correctly, has no docker socket).
