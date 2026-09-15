# Implementation guide

## Stack and structure

Use Python 3.12+, `uv`, `ruff`, `ty`, `pytest`, `mcp >=2,<3`, and `httpx2`.
Use MCPServer over stdio and `httpx2.AsyncClient` for HTTP. Do not introduce
FastMCP, FastAPI, aiohttp, requests, curl_cffi, or another framework/client
without a demonstrated requirement.

Target Jira Cloud REST API v3 only. Do not add Jira Server/Data Center endpoint
selection, PAT/Bearer authentication, or OAuth in the MVP.

Prefer a standard structure:

```text
src/jira_mini_mcp/
  __init__.py
  server.py
  jira.py
  auth.py
  models.py
  errors.py
tests/
```

Keep modules small and responsibilities explicit. Avoid abstraction layers until
they remove demonstrated duplication or improve testability.

## HTTP client and tool boundary

Create exactly one shared `httpx2.AsyncClient` per MCP process and close it at
shutdown. Reuse it across every Jira operation for connection pooling and
predictable lifecycle management. Do not instantiate an `AsyncClient` in a tool
handler.

```text
MCP tools -> JiraClient -> shared httpx2.AsyncClient -> Jira REST API
```

`JiraClient` must not depend on MCP internals; MCP tools are thin adapters.

## Configuration, authentication, and errors

Load exactly `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` from the
environment supplied by the MCP host. Keep them in one immutable configuration
object, validate all three at startup, and use Jira Cloud Basic authentication.
The base URL is configuration, not part of the authentication provider's return
value. Do not expose additional public settings for the attachment cache.

Translate Jira and HTTP failures into concise actionable MCP errors. Preserve
HTTP status, Jira error code, issue key, and operation where useful, but never
credential-bearing URLs or secrets. Distinguish authentication failures,
permission denials, not found, rate limits, validation errors, timeouts, Jira
server errors, and network errors. Handle rate-limit metadata explicitly when
available.

Raise expected validation and Jira failures as MCP tool errors, not successful
objects containing `error`. State what caused the error and how to correct it
when that is known. Do not include raw response bodies, request URLs, headers,
or exception representations until they have been sanitized. In particular,
`search_issues(limit=0)` must tell the caller that `limit` must be in `1..100`.

## Normalization and response shapes

Return typed structures so MCPServer can publish and validate output schemas.
Do not also construct a second Markdown rendering of the same result. Use
Markdown only for normalized rich-text values such as issue descriptions and
comment bodies.

Convert Jira Cloud ADF to Markdown with deterministic handling for paragraphs,
headings, emphasis, strong text, links, inline code, code blocks, lists,
blockquote, hard breaks, and mentions. For an unsupported node, retain its
recursively extractable text rather than failing the whole tool or silently
dropping content.

Normalize timestamps to UTC ISO-8601 with a `Z` suffix. Parse `since` only when
it includes `Z` or an explicit numeric offset. Sort comments and changelog
entries by `(created, id)` so equal timestamps remain deterministic.

Normalize known Jira fields into compact values. Keep explicitly requested
unknown and `customfield_*` values as Jira JSON except for recursive ADF and
timestamp normalization. Omit absent or unrequested fields instead of returning
`null`. Represent users as `account_id` plus `display_name`; remove email,
avatar, and `self` URL data.

## Pagination algorithms

For comments and changelog, public `start_at` indexes the logical collection
after filtering and in the requested order. `limit` is the number of logical
items to return, and zero means every remaining item. Their responses contain
only `start_at`, exact `total`, and `items`.

Search uses Jira Cloud v3 enhanced search with an opaque `page_token`, a
`limit` in `1..100`, and an `items`/`next_page_token` response. Map the token to
and from Jira's `nextPageToken` without interpretation. Do not use the older
offset search endpoint that is being removed, report approximate count as
exact, or scan the entire result set to emulate `start_at` and `total`.

Jira Cloud comments support ordering. For `since`, request newest-first, scan
until the first older comment makes further matches impossible, and compute the
exact filtered total before applying the public slice. Do not let the Jira page
size redefine public `limit`.

Jira Cloud changelog pages are oldest-first. Implement descending logical
offsets using the Jira-reported total and total-aware source ranges. Reversing a
single upstream page is insufficient because it would return the newest item of
the oldest page rather than the newest item in the history. Tests must exercise
multiple source pages in both directions.

## Attachment cache

Create one process-scoped temporary cache in the MCP lifespan and remove it on
normal shutdown. Store downloads at
`<cache>/<attachment_id>/<sanitized_filename>`. Resolve and validate paths,
including symlink/reparse-point escapes, before writing.

Stream to a `.part` file, remove it after failure or cancellation, and atomically
replace the completed destination. Do not perform blocking file I/O on the async
event loop; use an existing async facility or explicitly offload small blocking
filesystem operations without adding a dependency solely for this purpose.

## Performance and code conventions

Prioritize connection reuse, compact responses, early pagination termination,
minimal Jira fields, and only justified parallel requests. Avoid concurrency if
it compromises ordering, pagination, rate limiting, or error handling without a
measured benefit.

Use Python type hints, simple typed functions, dataclasses or ordinary
structures where sufficient, and async I/O. Use Pydantic only when validation
or schema generation materially simplifies the code. Avoid blocking network or
file I/O in async handlers. Code comments are English and explain why.
