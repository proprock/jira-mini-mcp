# Changelog

Notable changes to `jira-mini-mcp`, for people who install and run it. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
`GIT-AND-RELEASES.md` has the rules for keeping this file.

## [Unreleased]

### Added

- Three write tools, the first in the project: `add_comment` posts one Markdown
  comment, `transition_issue` moves an issue by transition or target status name,
  and `update_issue` sets issue fields. The server is no longer read-only.
- `READ_ONLY_MODE`, an optional setting. `true`, `1`, or `on` registers only the
  six read tools, so an operator can run the server without handing an agent the
  ability to change Jira. Absent, empty, `false`, `0`, or `off` registers all
  nine; any other value stops startup rather than quietly enabling writes.
- Rate-limited requests are retried automatically, honouring `Retry-After` while
  the wait is short and reporting the limit with its value when Jira asks for
  longer. Failed requests that may have been applied are never replayed.

### Changed

- The shared HTTP client now has an explicit timeout, so a stalled Jira cannot
  hold a tool call open for the library default.

### Security

- Error messages no longer carry links. Jira's own error text can include a URL,
  and a URL carries the tenant host.
- The entry point raises `httpx2`'s log level, which otherwise wrote the
  configured Jira host and the JQL of every search into whatever log the host
  process configured.

## [0.1.0] - 2026-09-16

### Added

- Six read-only tools for Jira Cloud REST API v3: `search_issues`, `get_issue`,
  `get_comments`, `get_attachments`, `download_attachment`, and `get_changelog`.
- Comment and changelog pagination over a logical collection — exact `total`,
  both sort directions, `limit=0` for everything remaining, and `since`
  filtering for comments — with no hidden cap on a long history.
- Compact normalized responses: rich text as Markdown, UTC timestamps, users as
  `account_id` and `display_name`, and exact shapes for known Jira resources.
- Attachment downloads into an automatically managed, process-scoped temporary
  cache, with no directory to configure.
- Configuration from exactly `JIRA_BASE_URL`, `JIRA_EMAIL`, and
  `JIRA_API_TOKEN`, validated at startup.
- Installation straight from GitHub with `uvx`, and a `jira-mini-mcp` console
  entry point serving over stdio.

[Unreleased]: https://github.com/proprock/jira-mini-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/proprock/jira-mini-mcp/releases/tag/v0.1.0
