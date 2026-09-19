# What the tools return

Response shapes, default fields, pagination, and worked examples for
`jira-mini-mcp`. Installation and setup are in the
[README](../README.md); every setting is in
[configuration.md](configuration.md).

## Response format

Structured JSON with stable output schemas, and no second human-readable
rendering of the same result (the JSON is sent as text and as
`structuredContent`; see [Cheaper output](configuration.md#cheaper-output) to send it once).
Jira's rich text becomes Markdown inside the
corresponding string field. Timestamps normalize to UTC ISO-8601 with a `Z`.
Users are `account_id` and `display_name` only - no email, avatar, or `self` URL.
Known resources use compact shapes:

```text
issuetype  = {id, name, hierarchy_level}
status     = {id, name, category}
priority   = {id, name}
project    = {id, key, name}
components = [{id, name}, ...]
issue      = {key, summary?, status?, issuetype?}
issuelink  = {relationship, issue}
```

Absent and unrequested values are omitted rather than returned as `null`.
Explicitly requested unknown or `customfield_*` values are preserved as Jira
JSON. If Jira returns a malformed known resource but the rest is usable, the call
fails with the exact JSON paths and a sanitized partial result rather than
pretending the data was fine.

### Identifiers

`issue_key` is a key like `PROJ-123` (any case) or a numeric issue id, and
`attachment_id` is the numeric id `get_attachments` returns. Anything else,
including a value with `/`, `?`, `#`, or `..`, is refused before a request is sent.

### Fields

`search_issues` defaults to these seven fields:

```text
summary, status, issuetype, priority, assignee, updated, project
```

`get_issue` defaults to these sixteen fields:

```text
summary, description, issuetype, status, priority, assignee, reporter,
labels, components, created, updated, resolutiondate, issuelinks,
project, parent, subtasks
```

An explicit `fields` list replaces the default completely; the server adds no
hidden fields. `fields=[]` returns issue keys only.

### Resolved reference fields

Jira represents `watches` and `votes` as a link plus a count, never the actual
watcher or voter list. Naming either one in `get_issue`'s `fields` resolves it
to real data with one extra request per field:

```text
watches = {watch_count, is_watching, watchers: [{account_id, display_name}, ...]}
votes   = {vote_count, has_voted, voters: [{account_id, display_name}, ...]}
```

Neither is in the default field set, so this never costs an extra request
unless asked for by name. A failure on that extra request (for example, a 403
when watcher visibility is restricted) is a tool error naming the field - it
never falls back to Jira's raw `self`/count stub.

### Available transitions

Naming `transitions` in `get_issue`'s `fields` lists the moves available now,
with the status each leads to. It is resolved with one extra request and never
sent to Jira as a field:

```text
get_issue(issue_key="PROJ-123", fields=["transitions"])
-> {"key": "PROJ-123", "fields": {"transitions": [{"id": "21", "name": "In Progress", "status": {"id": "10001", "name": "In Development", "category": "indeterminate"}}, ...]}}
```

The `name` values are what `transition_issue` accepts in `to`.

### Custom field names

`customfield_10011` tells a reader nothing. When `fields` names any
`customfield_*`, `get_issue` also returns `field_names`, mapping each returned
custom field id to its display name:

```text
get_issue(issue_key="PROJ-123", fields=["customfield_10011"])
-> {"key": "PROJ-123", "fields": {"customfield_10011": 5}, "field_names": {"customfield_10011": "Story points"}}
```

Only custom fields that came back with a value are named, and the key is absent
when there are none. `search_issues` does not return names.

### Pagination

Large collections never pretend to be complete. `get_comments` and
`get_changelog` return `start_at`, an exact `total`, and `items` - more exist
when `start_at + len(items) < total`, and that sum is the next offset. Both
default to `order="desc"` (offset zero is the newest item) and to 20 items;
`limit=0` returns everything remaining from `start_at`, with no 100-item cap.
`get_comments` also takes `since`, applied before ordering and slicing, so
"what happened since the last release" does not mean loading a multi-year
discussion.

`search_issues` is the exception. The current
[Jira Cloud enhanced search API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
is cursor-based with no exact total; its count endpoint is approximate and the
old offset endpoint is being removed. So search returns `items` and
`next_page_token` only - pass the token back as `page_token`, and a null token
means the last page. `limit` is 1..100; zero is rejected with guidance rather
than silently treated as a default.

### Attachments

`get_attachments` returns metadata only. Only when a file matters does the agent
call `download_attachment`, which writes into an automatically managed
process-scoped temporary cache and returns a local path. No download directory to
configure, and the cache is removed at shutdown. Attachments over 100 MB are refused, and the error tells the agent to
ask the user to fetch the file.

## Examples

Prompts you might give an agent, and the calls the tools make possible. Each one
leans on a feature that a generic Jira tool does not have. Issue keys and
values are placeholders.

**"Summarize PROJ-123: status, owner, and what happened this week."**
Ask for only what the summary needs, and load only the recent discussion.

```text
get_issue(issue_key="PROJ-123", fields=["summary", "status", "assignee"])
get_comments(issue_key="PROJ-123", since="2026-09-14T00:00:00Z", order="asc", limit=0)
-> {"start_at": 0, "total": 4, "items": [...]}   # every comment since the timestamp
```

`fields` replaces the default set entirely, so nothing extra is fetched.
`since` is applied before ordering and slicing, so a multi-year discussion is
never loaded to find last week's replies.

**"Why did PROJ-123 go to Blocked, and who moved it?"**
The changelog is field-change history with an exact total, newest first.

```text
get_changelog(issue_key="PROJ-123", limit=5)
-> {"start_at": 0, "total": 37, "items": [...]}   # the five latest changes of 37
```

If those five do not reach the cause, `start_at=5` continues from there, or
`limit=0` returns everything remaining. Nothing is truncated without saying so.

**"Find my open bugs in PROJ, most recently updated first, and open the top three."**
Search is cursor-based and returns compact rows; the agent opens only what it
needs.

```text
search_issues(jql='project = PROJ AND type = Bug AND assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC', limit=10)
-> {"items": [...], "next_page_token": "eyJ..."}   # null token means the last page
get_issue(issue_key="PROJ-201")
```

**"Who is watching PROJ-123, and has anyone voted?"**
Jira only returns a link and a count for these. Naming them resolves them to
people.

```text
get_issue(issue_key="PROJ-123", fields=["watches", "votes"])
-> {"key": "PROJ-123", "fields": {"watches": {"watch_count": 2, "is_watching": false, "watchers": [...]}, "votes": {...}}}
```

**"There is a log attached to PROJ-123. What does it say?"**
Metadata first, the file only when it matters.

```text
get_attachments(issue_key="PROJ-123")
download_attachment(attachment_id="10042")
-> a local path in a managed temporary cache, removed at shutdown
```

**"The fix is merged. Move PROJ-123 to Done and say so."**
Transitions are matched by name, and a wrong name explains itself.

```text
transition_issue(issue_key="PROJ-123", to="Done", comment="Fixed in the linked PR")
```

If `Done` matches nothing, the error lists every transition available from the
issue's current status and the status each one reaches, so the agent corrects
itself without a separate lookup tool. `to` matches a transition name or a
status name, ignoring case; prefer the transition name.

**"Assign PROJ-123 to me and add the label `needs-review`, keeping the existing labels."**
`labels` is replaced wholesale, so the agent reads first, then writes the full
list.

```text
get_issue(issue_key="PROJ-123", fields=["labels"])
update_issue(issue_key="PROJ-123", fields={"assignee": "me", "labels": ["backend", "needs-review"]})
```

`assignee` accepts `"me"`, an account id, or a colleague's email or display name,
which is looked up among active users. An ambiguous name is an error listing the
candidates as `Name (accountId)`, never a guess; emails are matched but never
shown. `description` takes Markdown, which is converted to
Jira rich text.

**Same prompts, no write access.** With `READ_ONLY_MODE=true` the server
registers only the six read tools. An agent asked to comment or transition has
no such tool to call, rather than a tool that refuses.
