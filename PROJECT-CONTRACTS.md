# Project contracts

## Goals and scope

The server provides coding agents with compact, reliable Jira Cloud context. It
targets Jira Cloud REST API v3 only and is optimized for large issue histories,
inexpensive recent-activity retrieval, explicit pagination, low runtime
overhead, and direct installation from GitHub.

Non-goals are Jira Server/Data Center compatibility, OAuth, issue creation,
issue links, attachment upload, comment editing or deletion, worklogs, sprint or
board management, Jira administration, and Confluence integration.

## MCP tools

The server exposes exactly these tools:

```text
search_issues
get_issue
get_comments
get_attachments
download_attachment
get_changelog
add_comment
transition_issue
update_issue
```

Do not add a tool without a concrete agent use case. The first six read; the
last three write and are the entire write surface. `READ_ONLY_MODE` registers
the read tools alone.

## Common response conventions

Return canonical structured JSON. Do not duplicate the same result in a second
human-readable rendering. Convert Jira ADF description and comment bodies to
Markdown inside their JSON string fields. Normalize timestamps to UTC ISO-8601
with a `Z` suffix; accept `since` with any explicit ISO-8601 offset.

Represent a Jira user as:

```json
{"account_id": "...", "display_name": "..."}
```

Normalize known issue resources to these exact shapes:

```text
issuetype  = {id, name, hierarchy_level}
status     = {id, name, category}
priority   = {id, name}
project    = {id, key, name}
components = [{id, name}, ...]
```

`issuetype.hierarchy_level` comes from Jira's `hierarchyLevel`, and
`status.category` comes from `statusCategory.key`. The common issue-reference
shape is `{key, summary?, status?, issuetype?}`. Use it for `parent`, each item
in `subtasks`, and the `issue` value in an issue link; the optional `status`
and `issuetype` values use the compact shapes above. An issue link is
`{relationship, issue}`: for `inwardIssue`, take `relationship` from
`type.inward`; for `outwardIssue`, take it from `type.outward`, following
Jira's [issue-linking model](https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/).

Known normalized users and resources contain no additional Jira keys. In
particular, omit email addresses, `self`, avatar and icon URLs, descriptions,
scope data, nested `fields`, and issue IDs. Omit unrequested, absent, or `null`
optional values instead of emitting `null` or making follow-up requests.
Unknown and `customfield_*` values remain Jira JSON, except for the common ADF
and timestamp normalization rules.

Validate the expected scalar types and required identities of known resources:
`id` and `name`, plus `project.key` or `issue.key` where applicable. An issue
link must contain exactly one of `inwardIssue` and `outwardIssue`, together with
the corresponding relationship text. If Jira returns a malformed known
resource, normalize the rest best-effort, aggregate every problem with its
exact JSON path, and raise `JiraIncompleteResponseError`. Its JSON-ready partial
result is `{key, fields}` for `get_issue` or `{items, next_page_token}` for
search, and contains only successfully normalized, sanitized data. It never
contains the raw malformed object, URLs, icons, or credential-bearing data.

At the MCP boundary, this exception becomes `CallToolResult(isError=True)`.
The text explains the cause and incomplete result, then includes
`Partial result:` followed by compact JSON. A duplicate `structuredContent`
value is not required.

Tool failures are MCP tool errors, never successful responses containing an
`error` field. Whenever possible, state both the cause and how the caller can
correct it. Missing required configuration is a startup error.

### `search_issues`

Signature:

```text
search_issues(jql, page_token=None, limit=20, fields=None)
```

`limit` is in `1..100`; unlike comments and changelog, zero is invalid. The
validation error must tell the caller to set a positive value. Pass the opaque
`next_page_token` from one response as `page_token` in the next call; never
inspect, modify, or synthesize it.

The current Jira Cloud v3 enhanced search endpoint is cursor-based and does not
return an exact total. Its separate count endpoint is approximate, while the
older offset endpoint that returned exact `total` is being removed. Therefore
search returns exactly `items` and nullable `next_page_token`, with no
`start_at`, `total`, approximate count, or offset emulation. This is an explicit
upstream API limitation, not an accidental schema inconsistency.

When `fields` is omitted, use exactly:

```text
summary, status, issuetype, priority, assignee, updated, project
```

An explicit `fields` list replaces that default completely; the server adds no
implicit Jira fields. `fields=[]` is valid and returns issue keys only. Each
item has `key` plus a `fields` object containing only returned fields.

### `get_issue`

Signature:

```text
get_issue(issue_key, fields=None)
```

When `fields` is omitted, use exactly:

```text
summary, description, issuetype, status, priority, assignee, reporter,
labels, components, created, updated, resolutiondate, issuelinks,
project, parent, subtasks
```

An explicit list replaces the default, and `fields=[]` returns `key` with an
empty `fields` object. Jira Cloud treats both `fields=""` and `fields=-*` as an
unspecified field selection for this endpoint, so the client sends the sentinel
`fields=id`; the full field set otherwise returned varies by tenant and
permissions. Do not include complete comments, attachment bodies, or changelog
history; those belong to dedicated tools.

### `get_comments`

Signature:

```text
get_comments(issue_key, start_at=0, limit=20, order="desc", since=None)
```

`start_at` and `limit` are non-negative. `order` is `"desc"` or `"asc"` and
defaults to `"desc"`. `since` includes comments created at or after the given
timestamp. Define the logical collection in this order:

1. start with every comment;
2. filter by `since` when present;
3. sort by `(created, id)` in the requested direction;
4. return `[start_at:start_at + limit]`.

When `limit=0`, return every logical item from `start_at` onward. `total` is the
size of the complete collection when `since` is absent and the exact size of
the filtered collection when it is present. Use Jira's descending ordering and
stop once older comments cannot match, but do not let that upstream strategy
change the public semantics. Never cap a history at 100 comments.

A comment contains `id`, compact `author`, Markdown `body`, UTC `created`, and
optional UTC `updated`. Omit `updated` when it is absent or equal to `created`.
Include `updated_by` only when an edit was made by a different user. Do not
return `self`, visibility metadata, or the raw ADF document.

### `get_attachments` and `download_attachment`

`get_attachments(issue_key)` returns `{items}`, mirroring the collection-
wrapping convention `search_issues`/`get_comments`/`get_changelog` already use
(a bare top-level array cannot be an MCP `structuredContent` object). Each
item has metadata only: `id`, `filename`, `mime_type`, `size`, compact
`author`, and UTC `created`. Never embed binary or base64 attachment contents
or Jira download URLs.

`download_attachment(attachment_id)` writes to an automatically created
process-scoped temporary cache and returns `attachment_id`, `filename`,
`mime_type`, `size`, and `local_path`. Store the file at
`<cache>/<attachment_id>/<sanitized_filename>`, write through a temporary
`.part` file, atomically replace an existing completed file, and delete partial
files after failures or cancellation. Prevent path traversal and symlink or
reparse-point escape. Remove the cache during normal server shutdown.

### `get_changelog`

Signature:

```text
get_changelog(issue_key, start_at=0, limit=20, order="desc")
```

Apply the same logical offset, limit, `limit=0`, and `(created, id)` ordering
rules as comments. `start_at=0` means the newest change in descending mode and
the oldest in ascending mode, regardless of Jira's upstream oldest-first order.

A changelog entry contains `id`, compact `author`, UTC `created`, and `changes`.
Each change contains human-readable `field`, `from`, and `to`; include an
optional `field_id` for a custom field. Omit Jira's internal old/new value IDs.

## Write tools

Three tools change Jira state. They are annotated so `READ_ONLY_MODE` can
withhold them, and their annotations state what they really do: `add_comment`
only appends, `update_issue` overwrites but repeats into the same state, and
`transition_issue` overwrites and is not repeatable.

Write tools do not weaken any read rule. Their failures are MCP tool errors,
their messages name the cause and the correction, and neither a message nor a
result carries a Jira URL, credential, or raw response body.

### `add_comment`

Signature:

```text
add_comment(issue_key, body)
```

`body` is Markdown and is converted to Jira Cloud ADF. The supported set is the
one `get_comments` renders back: paragraphs, ATX headings, bullet and ordered
lists, blockquotes, fenced code blocks with an optional language, hard breaks,
and the strong, emphasis, inline-code, and link marks. Anything else — tables,
images, raw HTML, reference links, an unclosed delimiter — stays literal text
rather than being guessed at. An empty or whitespace-only body is a validation
error, not an empty comment.

The result is exactly the `Comment` shape `get_comments` returns: `id`, compact
`author`, Markdown `body`, UTC `created`, and optional `updated`/`updated_by`.
Jira reports `updated` equal to `created` on a new comment, so both optional
values are omitted. There is no tool to edit or delete a comment.

### `transition_issue`

Signature:

```text
transition_issue(issue_key, to, comment=None)
```

Jira accepts only a workflow-specific transition id, so `to` is resolved against
the issue's available transitions: by transition name first, then by target
status name, comparing case-insensitively after trimming surrounding space. Both
steps are necessary and the order matters. A transition's name routinely differs
from the status it produces — a transition named `In Progress` can lead to a
status named `In Development` — and two differently named transitions can reach
one status, in which case only the transition name distinguishes them.

A `to` that matches nothing is a validation error listing every available
transition and the status it leads to; that listing is the tool's discovery
channel, which is why there is no separate `get_transitions` tool. A `to` that
matches more than one transition is a validation error naming the candidates,
never a guess. If the issue offers no transition at all, the error says so
rather than reporting a missing match.

`comment` is Markdown, converted exactly as `add_comment` converts a body, and
travels in the same request as the move so the two cannot land apart.

The result separates the transition from its outcome, because conflating them is
the mistake the resolution rules exist to prevent:

```json
{
  "key": "ABC-123",
  "transition": {"id": "21", "name": "In Progress"},
  "status": {"id": "10001", "name": "In Development", "category": "indeterminate"}
}
```

`status` uses the compact status shape. The available transitions are read
immediately before the move, so a workflow change in between surfaces as Jira's
own error. A malformed transition in the list stops the move instead of
resolving against a partial list, which could report a missing match for a
transition that exists.

### `update_issue`

Signature:

```text
update_issue(issue_key, fields)
```

`fields` mirrors the read side: what `get_issue` returns can be written back.
Known fields take friendly values; unknown and `customfield_*` values pass
through as raw Jira JSON, exactly as they survive normalization on the way out.

```text
summary      text
description  Markdown, or null to clear
assignee     an account id, the literal "me", or null to unassign
labels       a list of strings, replacing the whole list
components   a list of names, replacing the whole list
priority     a name, or null
parent       an issue key, or null
duedate      YYYY-MM-DD, or null
```

`labels` and `components` replace their entire list; there is no add or remove
verb. A caller adding one value reads the issue first.

`assignee: "me"` resolves through the configured account's own identity, fetched
once per process, because an agent asked to take a ticket cannot know its own
account id.

An empty `fields` object is a validation error naming the requirement, not a
no-op request. `status` and `comment` are rejected with a message naming
`transition_issue` and `add_comment`; `attachment`, `issuelinks`, `worklog`,
`project`, `issuetype`, `key`, `id`, `created`, `updated`, and `resolutiondate`
are rejected as unsupported, with `issuelinks` pointing at `add_comment` as the
place to record a link or ask for one. Every rejection happens before any
request is sent.

The result is the issue key and the field names that were sent:

```json
{"key": "ABC-123", "updated_fields": ["labels", "summary"]}
```

The issue is not re-fetched. A caller that wants the resulting state calls
`get_issue`, which keeps the write to one request and avoids implying that the
returned values were read back from Jira.

## Pagination and responses

`get_comments` and `get_changelog` return only:

```text
start_at
total
items
```

`total` is exact for the logical collection. A caller can determine whether
more items exist with `start_at + len(items) < total`, and the next offset is
`start_at + len(items)`. Do not add redundant `max_results`, `is_last`, or
`next_start_at` fields. If `start_at >= total`, return an empty `items` list
without disguising an upstream failure as an empty result.

`search_issues` returns only:

```text
items
next_page_token
```

`next_page_token` is `null` when Jira reports the final page. It is the only
public cursor state; do not add a misleading `total` or expose Jira's internal
camelCase field name.

## Configuration

The MVP has exactly three required setup values:

```text
JIRA_BASE_URL
JIRA_EMAIL
JIRA_API_TOKEN
```

Use Jira Cloud Basic authentication with the email and API token. Do not expose
Bearer/PAT or OAuth configuration in the MVP.

`READ_ONLY_MODE` is an optional fourth value and the only one that is not a
credential. It restricts tool registration to the tools annotated
`readOnlyHint`, so an operator can withhold state-changing tools without
building a second server or maintaining a client-side allowlist:

```text
READ_ONLY_MODE
```

`true`, `1`, and `on` enable it; `false`, `0`, `off`, an empty value, and an
absent variable all leave it disabled, which registers every tool. Surrounding
whitespace is ignored and the comparison is case-insensitive. Any other value is
a startup `ConfigError` naming the variable, the value received, and the accepted
spellings — never a silent fallback, because ignoring a typo here would register
tools an operator believed they had withheld. The switch selects MCP tool
registration, not Jira access: it is parsed beside the three credentials rather
than inside `JiraConfig`, and `JiraClient` is unaware of it. It does not grant or
revoke any Jira permission; the API token's own account permissions still apply.
Enabling it writes one line to stderr at startup naming the mode, and never a
configured value.

## Packaging and compatibility

Use a standard `src` package layout, PEP 517/518/621 packaging, and a
`jira-mini-mcp = "jira_mini_mcp.server:main"` console entry point. Support:

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
uvx --from git+https://github.com/proprock/jira-mini-mcp@v0.1.0 jira-mini-mcp
```

Version `v0.1.0` is a GitHub Release with checked wheel and source-distribution
assets; it is not a PyPI release. Keep runtime behavior GitHub-agnostic so a
future `uvx jira-mini-mcp` publication remains possible. Before 1.0,
compatibility still matters because configurations and agent prompts may depend
on public schemas.
