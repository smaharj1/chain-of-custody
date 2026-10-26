# Release automation (H5)

Release-please, changesets, semantic-release and goreleaser all do the same thing from the kit's point of view: they turn a merge into a release. A release is H5. It is a human act at every autonomy level, and it never rides along with a merge the loop is allowed to make.

`/bootstrap` detects them (`python3 .claude/scripts/bootstrap_detect.py`, field `release_automation`). It looks for the tool's config files, its workflow (`googleapis/release-please-action`, `changesets/action`, `semantic-release`, `goreleaser-action`), and a dependency in `package.json`. It never runs any of them.

## The rule

- **Release pull requests are merged by a person.** The pull request that release-please or changesets opens ("chore: release 1.4.0", "Version Packages") is not auto-merged, not approved by the bot, and not merged by the loop, at any level. The kit has no code path that merges a pull request.
- **Tags and releases are human.** The tag ruleset (`tags-protection.solo.json` or `.team.json`, applied by `apply-boundary.sh`) lets only a person or the release team create a tag. A release workflow that runs on a tag then runs because a person pushed it.
- **A release workflow that publishes on a push or a merge is a deploy trigger.** If it runs `npm publish`, `gh release create`, a container push or a deploy on a push to `main`, list it under `deploy_triggers` in `loop.config.md` with `fires_on` and `gate`. `python3 .claude/scripts/doctor.py --deploy-surface` finds it and says what is missing. An ungated deploy on `main` caps the level at L4; one on a bot-pushable ref caps it at L1.
- **Do not turn on auto-merge for release pull requests.** Leave "Allow auto-merge" off for the repository, or at least never set it on these.

## What bootstrap does about it

1. Says in two sentences what it found and what it means.
2. Adds the tool's files to the config plan as `always_human_paths`, so a pull request that touches them is never treated as low risk: `release-please-config.json`, `.release-please-manifest.json`, `CHANGELOG.md` (release-please only), `.changeset/**`, `.releaserc*`, `release.config.*`, `.goreleaser.y*ml`, and any workflow whose name says release, publish or deploy.
3. Reminds you to attest the release workflow in `deploy_triggers`, if it publishes on a push or a merge.

## Per tool

| Tool | Opens | You | Watch for |
|---|---|---|---|
| release-please | A "release" pull request that bumps the version and the changelog | Review and merge it; the tag and the release follow | The action often runs with `GITHUB_TOKEN`; a release workflow on `release: published` is a deploy trigger |
| changesets | A "Version Packages" pull request | Review and merge it; the publish step follows | The `changesets/action` publish step uses an npm token: keep it in an Environment with required reviewers |
| semantic-release | Nothing: it releases directly from a merge to the release branch | Decide whether a release on every merge is acceptable; it is a deploy on `main` | It has no pull request to review, so gate the job behind an Environment or run it by `workflow_dispatch` |
| goreleaser | Nothing: it runs on a tag | Push the tag yourself | Tag protection decides who can |

A release workflow template is not shipped: they differ too much between projects. The infra templates (`.claude/templates/infra/`) show the shape of a human-gated job.

## Check it

```
python3 .claude/scripts/bootstrap_detect.py --json     release_automation.tools, .evidence, .suggested_always_human_paths
python3 .claude/scripts/doctor.py --deploy-surface     the deploy triggers it found and what you have attested
python3 .claude/scripts/ci_lint.py                     the workflows for the split-CI rules
```
