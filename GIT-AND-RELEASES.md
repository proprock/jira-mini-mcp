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
