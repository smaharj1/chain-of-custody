# changelog.d: per-topic changelog fragments

Each v2 build topic adds one fragment, `changelog.d/<topic>.md`, instead of editing
`CHANGELOG.md`, `README.md`, `GLOSSARY.md`, `docs/getting-started.md`, `docs/faq.md`,
`KIT_VERSION` or the SVGs (IMP-6, IMP-12). The release stage reads every fragment, writes the
single `## [2.0.0-rc.1]` section of `CHANGELOG.md`, folds the pointers into the prose files,
builds `docs/v2-pending-human.md` from the "Pending-human actions" sections, and then deletes the
fragments (this README stays).

`tools/check-refs.py` does not scan fragments: they name files of their own branch.

## Format

One file per topic, named after the topic branch without `v2/` (`00-foundations.md`,
`2-guards.md`, ...). Plain Markdown. Use exactly these level-2 headings, in this order. Write
"None." under a heading that has nothing to say, so the release stage can tell "nothing" from
"forgot".

```markdown
# <topic>: <one-line summary>

## Changes
What was added, changed or removed, as bullets a user could read. Name files and commands.

## Upgrade impact
What a v1.2.0 install must do or will notice when it takes this change. Breaking changes first.
Say "none" if copying the new files is enough.

## New config keys
One bullet per key: name, type, default, H6-protected or not, evidence-gated or not. The keys
themselves live in `.claude/scripts/kitlib/schema/<phase>.json`, not here.

## Pending-human actions
Things only a human can do (push, GitHub settings, live runs, credentials, attestations), each
with the exact command or file and why an agent could not do it. Becomes `docs/v2-pending-human.md`.

## Docs impact
Which prose files need which edits (README, GLOSSARY, getting-started, faq, SVGs), described
precisely enough for the release stage to make them without reading your diff.

## Decisions made
Judgement calls the design documents did not settle: what you decided and why.

## Conductor: pointers to add
One-line pointers the conductor must add to hot files you do not own, each as
`<file>: <exact line or place> -> <what to point at>`.
```

## Rules

- Keep it factual and short. No marketing, no emoji.
- Do not paste secrets, real credentials or real-project names. The two real installs are
  "Install A" and "Install B" only.
- The definition of done (IMP-24) requires the fragment to exist with all seven headings.
