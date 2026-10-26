---
name: launch-app
description: Start this project's app locally on its dedicated port, request one route, and stop it. Use when asked to run, preview or demo the app, or to see a change working.
---

# Launch the app

Fill every `<<...>>` from the repository before this file is installed (`/bootstrap` does it; a human reads it once, because a skill file
can carry hooks and only a human installs one). Delete this paragraph when you are done. Never write a command here that you have not
seen work: the commands must match `verify_dev` or `verify_run` in `.claude/loop.config.md` and the "Run the App Locally" section of
`.claude/context/local-dev.md`.

## Start

- Command: `<<the dev-server command, for example: pnpm run dev>>`
- Port: `<<the dedicated port, from verify_ports (not the framework's default)>>`. Check it is free first; a busy port means another run is
  alive, so stop and say so instead of picking another.
- Ready when: `http://127.0.0.1:<<port>><<ready path, for example />>` answers with a status below 400. Wait at most `<<seconds>>` seconds.
- Start it in its own process group so it can be stopped as a group, and write its output to a log file, not into the conversation.

## Check

Request `<<the route to show, for example /health>>` and report the status and the first lines of the body. For a UI route, use the
browser-preview tool if the session has one, otherwise report the HTTP result.

## Stop

Kill the whole process group you started, then confirm the port is free. A process left running after the check is a leak.

## Never

- Never run this against production, with production credentials, or with a real `.env`. Use the local environment only.
- Never run a build, migration or seed command from here; those belong to the verify stages and the migration notes in `local-dev.md`.
- Never leave the server running when you finish.
