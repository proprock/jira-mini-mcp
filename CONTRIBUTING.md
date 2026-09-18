# Contributing

Thanks for helping. This server is deliberately small, so most of the work is
deciding what *not* to add. Read [Scope](#scope) before opening a pull request.

## Scope

- Jira Cloud REST API v3 only. No Server/Data Center, Confluence, boards,
  sprints, worklogs, or administration features without a concrete agent use
  case.
- Read access is broad. Write access is exactly three tools: `add_comment`,
  `transition_issue`, and `update_issue`. Anything else that changes Jira
  (issue creation, links, attachment upload, deletion) needs a concrete use case
  and maintainer approval first, so open an issue before writing code.
- A new or changed tool is a public API change. It gets a contract in
  [PROJECT-CONTRACTS.md](PROJECT-CONTRACTS.md) before it is implemented.
- Configuration is `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, plus the
  optional `READ_ONLY_MODE` and `DISABLE_STRUCTURED_OUTPUT`. A further setting
  needs maintainer approval.

Agent-assisted contributions follow the same rules; the repository's
[AGENTS.md](AGENTS.md) lists them and links the detail documents.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/proprock/jira-mini-mcp
cd jira-mini-mcp
uv sync
```

### Commit hooks

`.pre-commit-config.yaml` runs the fast checks on each commit: file hygiene,
`ruff format --check`, `ruff check`, and `ty check`. The Ruff and ty hooks call
`uv run`, so they use the versions locked in `uv.lock`, the same ones CI uses.
The runner is [prek](https://github.com/j178/prek), a fast Rust drop-in for
`pre-commit` that reads the same config. It is a dev dependency, so `uv sync`
already installed it:

```bash
uv run prek install
```

Run everything on demand with `uv run prek run --all-files`. The full test
suite is not a hook. Plain `pre-commit` also reads the config, but prek is the
supported runner.

## Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv run pytest -o addopts="" --cov=jira_mini_mcp --cov-branch --cov-report=term-missing
```

`uv run ruff format .` applies formatting. The last command reports statement
and branch coverage with missing lines; review it before closing a change. The
project deliberately has no fail-under percentage until a meaningful baseline
exists, and coverage never replaces behavioral assertions.

On macOS and Linux, `make check` runs the full sequence and stops on the first
failure; `make help` lists the targets. It is a convenience wrapper only.
Windows contributors run the commands above directly.

## Tests

The default suite is fully offline and needs no Jira instance or credentials.
[QUALITY.md](QUALITY.md) is the authority on what tests must cover; the
essentials:

- HTTP mocks trace to read-only observations of a **non-production** Jira Cloud
  site. Each fixture keeps the observed structure while every tenant, account,
  issue, cursor, timestamp, and content value is synthetic, and records its own
  provenance.
- Never commit a raw capture, a real token, or a real tenant URL, even from a
  test site.
- Write endpoints are exercised only against a disposable issue, and a write
  test asserts the exact request sent, not only the parsed result.

Two evals live outside `pytest` because they cost money or touch a live site.
Run them deliberately; see [evals/README.md](evals/README.md):

```bash
uv run python evals/run_eval.py                       # tool selection, calls a model
uv run python evals/structured_output_savings.py      # DISABLE_STRUCTURED_OUTPUT savings
```

## Architecture

```text
agent -> stdio -> MCPServer -> JiraClient -> httpx2.AsyncClient -> Jira REST v3
```

One asynchronous HTTP client is reused for the process lifetime, with an
explicit timeout and bounded retries: a 429 is retried for any method, a 5xx or
a dropped connection only for methods that converge on replay, never a POST.
`JiraClient` knows nothing about MCP; the tools are thin adapters over it. See
[IMPLEMENTATION.md](IMPLEMENTATION.md) for module boundaries and conventions.

## Branches, commits, and pull requests

- Use a short-lived branch per change, named like `feat/comments-pagination`,
  `fix/attachment-path-sanitization`, or `docs/github-install`. Do not develop
  directly on `master`.
- Write [Conventional Commits](https://www.conventionalcommits.org/): `feat`,
  `fix`, `refactor`, `test`, `docs`, `ci`, `chore`, `perf`, `build`. Describe the
  result, in the imperative mood.
- One pull request, one purpose. Before opening it, run the checks above and
  update the README and [PROJECT-CONTRACTS.md](PROJECT-CONTRACTS.md) when
  behavior changes.
- Pull requests are usually squash-merged.
- CI (format, lint, types, tests on Python 3.12 and 3.14, package build) must
  pass.

Full details: [GIT-AND-RELEASES.md](GIT-AND-RELEASES.md).

## Changelog

Record every externally observable change in [CHANGELOG.md](CHANGELOG.md) under
`## [Unreleased]`, in the same pull request that makes it. It is written for
someone who runs the server, not for someone reading the diff.

An entry is earned by a tool added, removed, or renamed; a changed argument,
default, or response schema; a configuration value added or changed; error
behavior a caller must handle; and anything security-relevant. Tests, fixtures,
CI, refactors, dependency bumps, and documentation do not earn one. Name the
tool or setting in the entry.

## Release model

Semantic versioning, Conventional Commits, short-lived branches, pull-request CI,
and tagged releases. Each tag is a GitHub Release with CI-checked wheel and
source distributions attached, and it is published to
[PyPI](https://pypi.org/project/jira-mini-mcp/) over GitHub Actions Trusted
Publishing (OIDC), so there is no long-lived credential to manage.

Releases are cut by the maintainer:

- The `[Unreleased]` section decides the version: a new tool, setting, or other
  additive capability is a MINOR bump; a fix alone is a PATCH.
- A release renames `[Unreleased]` to the new version and date, and bumps
  `version` in `pyproject.toml` and both version fields in `server.json` to
  match, then runs `uv lock`.
- Before pushing the tag, run `uv run python scripts/check_version_sync.py` to
  confirm all of those agree with the newest CHANGELOG heading.
- A MAJOR bump, and creating or pushing a release tag, happen only on the
  maintainer's explicit instruction.

Contributors do not bump versions or create tags.

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## License

By contributing you agree that your contribution is licensed under the
project's [MIT license](LICENSE).
