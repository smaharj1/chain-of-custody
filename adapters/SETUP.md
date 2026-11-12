# Using Chain of Custody with Codex or Cursor

Using Claude Code? Install the plugin instead; see the [README](../README.md). This page is for Codex and Cursor.

Codex and Cursor can't load a Claude Code plugin. So they get a **guidance-only copy**: the same modes, roles and
templates as plain instructions your agent reads. Nothing is enforced. Read "What you don't get" before you rely on it.

## Set it up

You need `git` and Python 3.9 or newer.

1. Clone the kit and check out a release tag. The tag matters, because the copy links to kit files at that tag.

   ```sh
   git clone https://github.com/smaharj1/chain-of-custody.git
   cd chain-of-custody
   git checkout v2.0.0   # or the release you want
   ```

2. Run the adapter against your project:

   ```sh
   python3 scripts/adapt.py --target /path/to/your/project              # Codex and Cursor
   python3 scripts/adapt.py --target /path/to/your/project --tool codex  # Codex only
   python3 scripts/adapt.py --target /path/to/your/project --tool cursor # Cursor only
   ```

3. Fill in `.chain-of-custody/project/PROJECT.md` (your stack, layout and commands), the `verify_*` commands in
   `.chain-of-custody/project/loop.config.md`, and the primers in `.chain-of-custody/project/context/`. Then commit the new
   files so your team gets them too.

## What you get

- `.chain-of-custody/`: the modes (`skills/`), the roles (`agents/`), the kit's protocols (`context/`), `templates/`, the
  loop spec (`spec/`), a `GUIDE.md` that ties them together, and a `VERSION` file.
- `.chain-of-custody/project/`: your project's own files, copied once from the starter files. `PROJECT.md` plays the part
  `CLAUDE.md` plays in Claude Code; where the kit's files say `CLAUDE.md`, they mean it. These files are yours to edit and
  are never overwritten.
- `.chain-of-custody/project-seed/`: this release's starter files, untouched, so you can compare yours with them.
- `AGENTS.md` (Codex) and `.cursor/rules/chain-of-custody.mdc` (Cursor, always on). They tell the agent to read
  `PROJECT.md` at session start, which mode file to read for each kind of work, which role files to use, and what does and
  does not work here.

You start a mode by naming it: "act as the PM and gather requirements for X", "act as the Architect for X". The agent reads
`.chain-of-custody/skills/<mode>/SKILL.md` and follows it. When you name no mode, it acts as the PM: it asks questions and
does not design or write code. The design docs, briefs and reports in `docs/features/<slug>/` are the hand-off between
steps, just as they are in Claude Code.

Only Markdown is rewritten. The other templates (CI workflows, policy files) are copied byte for byte, so they still name
`.claude/...` paths; adjust those when you adopt one. The Claude Code settings and hook templates are left out, since
nothing here reads them.

If you already have an `AGENTS.md` (or a rule with the same name), the adapter leaves it alone and prints the one line to add
to it by hand. `--force` replaces it and keeps your old copy as `AGENTS.md.pre-adapt`. The adapter never writes through a
symlink, not even with `--force`: if `AGENTS.md`, the `.mdc`, `.cursor/`, `.chain-of-custody/` or `project/` is a symlink,
it refuses, names the link and its target, and exits 1.

## Git and delivery

The seeded `loop.config.md` starts at `git_autonomy: none` and `delivery: local`: the agent asks you to create the branch,
commit and push. If you raise them, the rule in `GUIDE.md` is that an agent may push only its own feature branch, and only
when the config committed on the base branch says `git_autonomy: branch+commit+push` with `delivery: push-branch` or
`draft-pr`. It never pushes main or a protected branch, never force-pushes, and never merges. Nothing here checks this rule
or knows which branches are protected.

## What you don't get

All of this runs only in Claude Code, through the plugin:

- **Guards.** Nothing stops a push to main, an edit on a protected branch, or a change to a guardrail file.
- **Push-tier enforcement.** The `git_autonomy` and `delivery` values are instructions here, not limits.
- **Hooks.** No session-start checks, no worktree setup, no telemetry.
- **Subagent isolation.** Roles are instructions, not separate clean contexts. When one session writes code and then
  "reviews" it, that is not an independent review. Run the review in a fresh session if you want one.
- **Per-role tool limits.** A role file that says it is read-only, or lists the tools it may use, is a request the agent may
  ignore.
- **The `loopctl` driver.** No unattended loop. The Orchestrator mode is copied for reference only.
- **Gates.** Verify, design critique, code review and security review are steps the agent is asked to follow, not steps it
  can't skip.
- **Human-step enforcement.** The agent is told to stop and ask before a merge or an infrastructure apply. Nothing makes it.

Kit scripts (`verify.py`, `pr_prepare.py`, `loopctl`, `doctor.py`) are not copied. The copy names each one as
"`scripts/<name>` (Claude Code plugin only; link)". The link shows what the script does; it does not run here. `GUIDE.md` says
what to do instead: run the `verify_*` commands yourself, and deliver by hand.

For real enforcement, use your CI: required status checks and code review on every pull request, and branch protection on
main.

## Check the installed version

```sh
grep '^version:' .chain-of-custody/VERSION
head -1 AGENTS.md    # the adapter's marker line, with the version that wrote it
```

## Update

Check out the newer tag in your clone and run the same command again:

```sh
git fetch --tags && git checkout v2.1.0
python3 scripts/adapt.py --target /path/to/your/project
```

- `.chain-of-custody/` is regenerated from scratch, apart from `project/`.
- `.chain-of-custody/project/` is kept as it is (a starter file you deleted comes back). When a starter file changed in the
  new release, the adapter prints `seed for <file> changed since v<old>; compare with .chain-of-custody/project-seed/<file>`.
  It adds that copying it over is safe when you never edited yours. Merge what you want by hand.
- `AGENTS.md` and the `.mdc` are rewritten only while they still have the adapter's marker line. If you edited one and
  kept the marker, your text is saved as `AGENTS.md.prev` (or `chain-of-custody.mdc.prev`) before it is replaced. Delete
  the marker line to keep your own edits; after that the adapter leaves the file alone.
- Switching `--tool` leaves the other tool's file behind. The adapter prints a `stale` line for it; delete it, or re-run
  with `--tool both`.

Review the diff and commit.

## Remove

Delete these. Keep `.chain-of-custody/project/` first if you want your filled-in files.

- `.chain-of-custody/` (and `.chain-of-custody.adapt-tmp/`, if an interrupted run left it)
- `AGENTS.md` (or the lines you added to yours) and `.cursor/rules/chain-of-custody.mdc`
- any backups the adapter kept: `AGENTS.md.pre-adapt`, `AGENTS.md.prev`, `.cursor/rules/chain-of-custody.mdc.pre-adapt`,
  `.cursor/rules/chain-of-custody.mdc.prev`
