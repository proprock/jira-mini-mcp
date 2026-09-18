# Git and releases

Use focused short-lived branches, for example:

```text
feat/comments-pagination
feat/project-foundation
fix/attachment-path-sanitization
refactor/jira-client
test/changelog-pagination
docs/github-install
ci/github-release
```

Rebase or otherwise update from `master` before integration when necessary.
Prefer squash merges unless individual commits have durable value, and delete
merged feature branches.

Use Conventional Commits (`feat`, `fix`, `refactor`, `test`, `docs`, `ci`,
`chore`, `perf`, `build`). Describe the result, use `perf` only for measured or
clearly justified performance work, and mark breaking changes only when public
contracts actually change.

Each pull request has one clear purpose. Before merge, verify formatting, lint,
types, tests, public schemas, and README/agent documentation when behavior
changes.

Use semantic versions and matching tags such as `v0.1.0`. CI runs on pull
requests and pushes to `master`, with `uv sync`, format check, lint, type check,
tests, and package build on the minimum supported and a current Python where
practical. Create distributable artifacts only through an intentional release
or tag after building and checking wheel and source distributions.

The initial `v0.1.0` release is a GitHub Release, not a PyPI publication. Build
the wheel and source distribution in CI, verify both in clean environments, and
attach those same checked artifacts to the release. Test direct `uvx` execution
from the repository and tag. Add PyPI Trusted Publishing only in a later,
deliberate publication task, and prefer it over a long-lived API token.

`.github/workflows/release.yml` automates this: on a pushed `v*` tag, or a
manual `workflow_dispatch` run (dispatched from a branch that already has
this workflow file, passing the target tag as its `tag` input — needed for a
tag pushed before this workflow existed), it builds the sdist and wheel,
verifies both install cleanly and pass MCP discovery, then publishes a GitHub
Release for that tag and attaches those exact files using the run's built-in
`GITHUB_TOKEN` — no manual download/attach step, no long-lived credential.

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
It is written for someone who installs and runs the server, not for someone
reading the diff, and the git history already records the diff.

Add the entry in the same pull request as the change it describes, under
`## [Unreleased]`. A changelog written at release time is written from memory.

**What earns an entry:** anything a user of the server can observe — a tool
added, removed, or renamed; a tool argument, default, or response schema
changed; a configuration value added or its accepted values changed; error
behavior a caller has to handle; and anything security-relevant, such as what
can reach a log or a model-visible message.

**What does not:** tests, fixtures, CI, refactors, dependency bumps,
documentation, and internal helpers. A release whose every change is invisible
from outside gets no entry, which is the correct outcome, not an oversight.

Use the Keep a Changelog categories — `Added`, `Changed`, `Deprecated`,
`Removed`, `Fixed`, `Security` — and name the tool or setting in the entry.
"`transition_issue` resolves a target status name as well as a transition name"
is an entry; "refactor the resolution helper" is not.

**Releasing:** rename `[Unreleased]` to the new version with its date, add a
fresh empty `[Unreleased]` above it, update the link definitions at the bottom,
and bump `version` in `pyproject.toml` to match the tag. Bump both `version`
fields in `server.json` (top level and the `pypi` package entry) to match as
well — the MCP Registry publish step in `release.yml` fails closed if the
package version it points at was never published. The Unreleased section
decides the version, strictly as `MAJOR.MINOR.PATCH`: a new tool, setting, or
other additive capability is a MINOR bump; a fix alone is a PATCH. Before
`1.0`, a breaking change to a published tool contract is also a MINOR bump,
and it says so in the entry rather than relying on the number.

A MAJOR bump, and creating or pushing the release tag itself, happens only on
the user's explicit instruction — never inferred, never bundled into a MINOR
or PATCH change on the assumption it's due.
