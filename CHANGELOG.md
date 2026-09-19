# Changelog

Notable changes to `jira-mini-mcp`, for people who install and run it. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
`GIT-AND-RELEASES.md` has the rules for keeping this file.

## [Unreleased]

### Fixed

- `issue_key` and `attachment_id` are checked before the request. A key
  containing `/`, `?`, `#`, or `..` is rejected with a clear error instead of
  sending a request to a different Jira resource, which for `update_issue` was a
  `PUT`. `attachment_id` must be numeric.

## [1.1.0] - 2026-09-19

### Added

- `JIRA_AUTH_METHOD`, an optional setting choosing `api_token` (the default,
  so existing configurations are unchanged) or `oauth`. With `oauth` the server
  authenticates through the user's own OAuth 2.0 (3LO) app, configured with
  `JIRA_OAUTH_CLIENT_ID` and `JIRA_OAUTH_CLIENT_SECRET` instead of
  `JIRA_EMAIL` and `JIRA_API_TOKEN`; an unrecognized value stops startup.
- `jira-mini-mcp login [--port N]` authorizes in the browser (PKCE, loopback
  callback on `http://localhost:8765/callback` by default) and stores the tokens
  in a user-only file; `jira-mini-mcp logout` deletes it. The server refreshes
  the access token itself, persists each rotated refresh token, and, until
  `login` has run, answers every tool call with an error saying to run it.
- A 401 in `oauth` mode tells the caller to run `jira-mini-mcp login` rather
  than to check `JIRA_EMAIL` and `JIRA_API_TOKEN`.

### Changed

- The `repr` of the loaded configuration no longer includes the email or API
  token.

## [1.0.0] - 2026-09-18

### Added

- `DISABLE_STRUCTURED_OUTPUT`, an optional setting naming tools,
  comma-separated, that return `content` only and skip
  `structuredContent`/`outputSchema`. Absent or empty changes nothing; an
  unrecognized tool name stops startup naming it and every valid tool name.
- `get_issue` resolves `watches` and `votes` to real data -- `{watch_count,
  is_watching, watchers}` and `{vote_count, has_voted, voters}` -- via one
  extra request per field when either name is explicitly requested in
  `fields`, instead of returning Jira's link-only `self`/count stub. Neither
  field joins the default field set, and a failure on the extra request is a
  tool error naming the field rather than a silent fallback.

## [0.9.3] - 2026-09-18

### Fixed

- Published README.md with the `mcp-name: io.github.proprock/jira-mini-mcp`
  marker the MCP Registry requires to verify PyPI package ownership; v0.9.2's
  registry publish failed because the marker was never in the released README.

## [0.9.2] - 2026-09-18

### Fixed

- Shortened `server.json`'s description below the MCP Registry's
  100-character limit; it had rejected the v0.9.1 registry publish while
  PyPI publishing succeeded.

## [0.9.1] - 2026-09-18

### Added

- The release workflow now also publishes each tagged release to the
  [MCP Registry](https://registry.modelcontextprotocol.io) as
  `io.github.proprock/jira-mini-mcp`, gated by the same GitHub Actions OIDC
  trusted publishing used for PyPI.

## [0.9.0] - 2026-09-17

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
- The release workflow now publishes each tagged release to PyPI over GitHub
  Actions trusted publishing (OIDC, no stored token), so `pip install
  jira-mini-mcp` and `uvx jira-mini-mcp` work starting with the next release.

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

[Unreleased]: https://github.com/proprock/jira-mini-mcp/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/proprock/jira-mini-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/proprock/jira-mini-mcp/compare/v0.9.3...v1.0.0
[0.9.3]: https://github.com/proprock/jira-mini-mcp/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/proprock/jira-mini-mcp/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/proprock/jira-mini-mcp/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/proprock/jira-mini-mcp/compare/v0.1.0...v0.9.0
[0.1.0]: https://github.com/proprock/jira-mini-mcp/releases/tag/v0.1.0
