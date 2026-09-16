# jira-mini-mcp

A small Jira Cloud REST API v3 MCP server designed specifically for coding agents such as Codex and Claude Code: broad read access, and a deliberately narrow write surface.

`jira-mini-mcp` intentionally exposes only a focused set of Jira capabilities instead of attempting to mirror the entire Jira REST API or the broader Atlassian product ecosystem.

The core idea is simple:

> Give the agent a small set of high-value, clearly differentiated tools and return only the context it actually needs.

This design follows Anthropic's tool-design guidance for agent systems: keep toolsets small, role-scoped, clearly differentiated, and focused on high-impact workflows. Anthropic's Claude Certified Architect materials also emphasize avoiding large tool registries and distributing capabilities into small, focused toolsets.

`jira-mini-mcp` applies that principle as a nine-tool Jira-specific interface.

## Why a small Jira MCP?

General-purpose Jira and Atlassian MCP servers often expose dozens or even hundreds of tools.

That sounds powerful, but it has significant costs for coding agents.

Every tool has:

- a name
- a description
- an input schema
- sometimes a complex output contract

Depending on the MCP client, these definitions may become part of the model context before the agent does any useful work.

A large toolset therefore consumes context even when most tools are irrelevant to the current task.

It also increases the probability of:

- choosing the wrong tool
- confusing similar tools
- supplying incorrect parameters
- making unnecessary tool calls
- retrieving excessively large responses
- spending tokens interpreting capabilities the agent will never use

The goal of `jira-mini-mcp` is not maximum Jira coverage.

The goal is maximum usefulness per tool.

## Nine tools

The server exposes only:

| Tool | Purpose |
|---|---|
| `search_issues` | Find Jira issues using JQL |
| `get_issue` | Get the core state and fields of one issue |
| `get_comments` | Retrieve recent or historical comments efficiently |
| `get_attachments` | List attachment metadata |
| `download_attachment` | Download one attachment |
| `get_changelog` | Retrieve issue history |
| `add_comment` | Post one Markdown comment |
| `transition_issue` | Move an issue through its workflow |
| `update_issue` | Set issue fields |

The first six read; the last three are the entire write surface.

This is enough for the primary coding-agent workflow:

```text
find issue
    |
    v
understand current state
    |
    +--> inspect recent discussion
    |
    +--> inspect attachments when relevant
    |
    +--> inspect history when relevant
    |
    v
work on code
    |
    v
report back
    |
    +--> comment with the result
    |
    +--> move the issue
    |
    +--> correct a field
```

The server does not expose unrelated Jira functionality just because the Jira API supports it.

## Designed for agents, not for API completeness

A conventional API wrapper tries to expose the underlying API as completely as possible.

An MCP server has a different consumer.

The consumer is an LLM agent with:

- a finite context window
- probabilistic tool selection
- a cost for every tool definition and result
- no benefit from knowing about functionality unrelated to its task

For that reason:

```text
REST API design:
    expose capabilities

jira-mini-mcp design:
    expose decisions an agent actually needs to make
```

The project intentionally trades API breadth for agent ergonomics.

## Why not expose the entire Jira API?

Jira supports a very large number of operations involving:

- users
- groups
- permissions
- projects
- project roles
- workflows
- workflow schemes
- screens
- fields
- field configurations
- dashboards
- filters
- worklogs
- votes
- watchers
- notifications
- issue security
- priorities
- resolutions
- components
- versions
- boards
- sprints
- administration
- and many more resources

Most of these have no value when a coding agent receives a task such as:

```text
Fix ABC-1234
```

Loading schemas for those capabilities gives the model more information, but usually not more useful information.

A smaller interface provides several advantages.

### Lower context overhead

Tool definitions consume tokens.

Nine compact schemas are inexpensive enough to remain available throughout an agent session without requiring a separate tool-discovery workflow.

The context budget can instead be used for:

- source code
- issue descriptions
- recent comments
- stack traces
- test results
- implementation reasoning

### Easier tool selection

Compare:

```text
get_issue
get_comments
get_attachments
```

with a server exposing many overlapping variants such as:

```text
get_issue
get_issue_details
get_issue_comments
get_issue_activity
get_issue_history
get_request_comments
get_service_desk_request
get_issue_remote_links
...
```

The first interface makes the intended action obvious.

This matters because tool selection is probabilistic.

Fewer overlapping choices reduce ambiguity.

### Smaller schemas

A tool designed specifically for an agent can expose only useful parameters.

For example:

```text
get_comments(
    issue_key,
    start_at=0,
    limit=20,
    order="desc",
    since=None
)
```

instead of exposing every query option supported by multiple underlying Jira endpoints.

The MCP interface is intentionally not a one-to-one representation of Jira REST.

### Better defaults

Agent-oriented defaults should reflect common agent workflows.

For example:

```text
get_comments("ABC-123")
```

returns the latest comments first.

The agent does not need to understand Jira pagination details just to learn what happened recently.

### Less response pollution

The same principle applies to outputs.

`get_issue` does not automatically dump:

- hundreds of comments
- complete changelog history
- attachment contents
- every Jira custom field
- unrelated metadata

Large resources are retrieved only when the agent asks for them.

This keeps useful information prominent and reduces token consumption.

### Predictable behavior

A small public interface is easier to:

- document
- test
- evaluate with agents
- keep backward compatible
- optimize
- secure

Adding every Jira endpoint would dramatically increase the number of interaction paths that need to work correctly.

## Why Jira only?

Combining multiple Atlassian products into one MCP server makes the tool-surface problem even larger.

A general Atlassian server may expose tools for:

```text
Jira
Jira Service Management
Confluence
Bitbucket
Compass
Atlassian administration
and other services
```

For an agent working on a Jira ticket, most of those definitions are irrelevant.

This creates two kinds of unnecessary complexity.

### Context complexity

The model must distinguish concepts such as:

```text
Jira issue
JSM request
Confluence page
Bitbucket pull request
Compass component
```

and select among tools belonging to different products.

That context competes directly with application code and task information.

### Implementation complexity

A multi-product server also requires more:

- dependencies
- authentication paths
- permission handling
- schemas
- error handling
- API version compatibility
- tests
- documentation

None of that improves the primary goal of `jira-mini-mcp`:

> Give a coding agent the Jira context required to work on a ticket.

Separate MCP servers provide a cleaner capability boundary:

```text
jira-mini-mcp
github-mcp
confluence-mcp
...
```

The MCP host can compose services when needed instead of forcing every individual server to become an integration platform.

## Context-efficient comments

Large Jira issues are a primary design case.

Comments support:

```text
pagination
ordering
latest-first retrieval
date filtering
```

For example:

```text
get_comments(
    issue_key="ABC-123",
    start_at=0,
    limit=20
)
```

returns the latest discussion.

An agent can also request:

```text
get_comments(
    issue_key="ABC-123",
    since="2026-09-01T12:00:00Z"
)
```

to retrieve only discussion after a known event, such as the last shipment or deployment.

`order="desc"` numbers comments from newest to oldest; `order="asc"` numbers them from oldest to newest. The server first applies `since`, then orders the resulting logical collection, and finally returns the slice `[start_at:start_at + limit]`. Equal timestamps are ordered by Jira comment ID.

`limit=0` means every remaining comment from `start_at`. The response `total` describes all comments when `since` is absent and only matching comments when it is present.

Jira Cloud does not filter comments by creation date directly, so the server retrieves comments newest-first and stops pagination as soon as older comments cannot match.

This avoids loading an entire multi-year discussion merely to answer:

```text
What changed since the last release?
```

## Explicit pagination

Potentially large collections never silently pretend to be complete.

`get_comments` and `get_changelog` return only:

```text
start_at
total
items
```

More items exist when `start_at + len(items) < total`; that sum is also the next `start_at`. Redundant `max_results`, `is_last`, and `next_start_at` fields are intentionally omitted.

`search_issues` is an intentional exception. The current [Jira Cloud v3 enhanced search API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/) is cursor-based and does not provide an exact total; its separate count endpoint is approximate, and the older offset endpoint is being removed. Search therefore accepts `page_token=None`, defaults to `limit=20`, and returns only:

```text
items
next_page_token
```

Pass `next_page_token` unchanged as `page_token` to retrieve the next page. A null token means the final page. Search accepts `limit` from 1 through 100; `limit=0` is rejected with guidance to use a positive value. The server does not present an approximate count as exact or scan the complete result set merely to emulate offset pagination.

Comments and changelog default to 20 and accept `limit=0` for an unbounded result from the selected offset.

Comments and changelog both default to `order="desc"`: `start_at=0` means the newest item. With `order="asc"`, offset zero means the oldest item. Jira Cloud returns changelog pages oldest-first internally, but the server maps offsets against the complete history so this upstream detail does not change the public ordering.

A server must never silently return the first 100 items from a 300-item history and make the agent believe it has seen everything.

## Attachments on demand

Attachment listing and downloading are separate operations.

```text
get_attachments
```

returns `{items}` of compact metadata -- the same collection-wrapping
convention `search_issues`, `get_comments`, and `get_changelog` already use,
since a bare top-level array cannot be an MCP `structuredContent` object.

Only when an attachment is relevant does the agent call:

```text
download_attachment
```

This avoids putting binary data or unnecessary attachment contents into the context.

Downloaded files are placed in an automatically managed process-scoped temporary cache. No download-directory configuration is required, and the cache is removed when the MCP server shuts down normally.

## A narrow write surface

Reading a ticket and never answering it leaves the loop open, so three tools close it: comment, move, correct a field. Nothing else writes.

The server intentionally does not include:

```text
create_issue
link_issues
add_attachment
delete_comment
log_work
```

Each was weighed against the same bar as every other tool: a concrete thing an agent needs while working a ticket. Issue creation needs per-project, per-type required-field discovery and is a feature in its own right. A link, or a request for one, fits in a comment.

Three details are worth knowing before an agent writes:

- `transition_issue` takes a transition name or a target status name, not an id. Those differ in real workflows -- a transition called `In Progress` can produce a status called `In Development` -- and two transitions can reach one status, so prefer the transition name. When nothing matches, the error lists every available transition and where it leads; that listing is the discovery mechanism, which is why there is no separate `get_transitions` tool.
- `update_issue` replaces `labels` and `components` wholesale. There is no add or remove verb, so read the issue first if you mean to add one value.
- Markdown in a comment or description is converted to Jira's rich text. Headings, lists, fenced code, inline marks, and links survive; anything else stays literal rather than being guessed at.

Every write tool is annotated `readOnlyHint=false`, with honest `destructiveHint` and `idempotentHint` values. Setting `READ_ONLY_MODE=true` registers the six read tools alone, so an operator can hand an agent this server without handing it the ability to change Jira. The Jira API token's own account permissions still apply on top of that.

## Stack

```text
Python 3.12+
uv
ruff
ty
pytest

mcp >=2,<3
httpx2
```

MCP transport:

```text
MCPServer
stdio
```

HTTP:

```text
httpx2.AsyncClient
```

A single asynchronous HTTP client is reused for the lifetime of the MCP process to provide:

- connection pooling
- keep-alive
- reduced TLS overhead

The expected performance bottleneck is Jira REST latency, not Python or MCP dispatch.

## Architecture

```text
Codex / Claude Code
        |
      stdio
        |
        v
   MCPServer
        |
        v
   JiraClient
        |
        v
httpx2.AsyncClient
        |
        v
Jira Cloud REST API v3
```

The Jira client is independent of MCP-specific code.

MCP tools are thin adapters around the Jira client.

## Installation

The project is packaged as a standard Python module and can be executed directly from GitHub.

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

A specific release can be pinned:

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp@v0.1.0 jira-mini-mcp
```

The package structure is kept compatible with later publication to PyPI.

After publication:

```bash
uvx jira-mini-mcp
```

will be sufficient.

## Configuration

The MVP requires exactly three values:

| Variable | Meaning |
|---|---|
| `JIRA_BASE_URL` | Jira Cloud site URL, such as `https://example.atlassian.net` |
| `JIRA_EMAIL` | Email address associated with the API token |
| `JIRA_API_TOKEN` | Jira Cloud API token |

The server uses Jira Cloud Basic authentication. Jira Server/Data Center, PAT/Bearer authentication, and OAuth are not supported by the MVP. Configuration is validated at startup, and errors identify the missing setting without printing its value or the configured Jira URL.

One optional value exists:

| Variable | Meaning |
|---|---|
| `READ_ONLY_MODE` | `true`, `1`, or `on` registers only the six read tools; `false`, `0`, `off`, empty, or absent registers all nine |

The comparison ignores case and surrounding space. Any other value stops startup with an error naming the variable and the accepted spellings, so a typo cannot quietly re-enable the write tools.

For Codex, register the stdio server with:

```shell
codex mcp add jira-mini --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_EMAIL=developer@example.com --env JIRA_API_TOKEN=replace-with-your-token -- uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

Use the equivalent stdio MCP configuration in another host: command `uvx`, arguments `--from`, the GitHub URL, and `jira-mini-mcp`, with the same three environment variables. Keep the token in the host's protected configuration and never commit it.

## Tool data conventions

Tool results are structured JSON with stable output schemas. Rich Jira Cloud ADF in issue descriptions and comments is converted to Markdown inside the corresponding string field; the server does not return a second duplicate rendering of the result.

Dates are normalized to UTC ISO-8601 with a `Z` suffix. Users are represented only by `account_id` and `display_name`. Known Jira resources use compact schemas:

```text
issuetype  = {id, name, hierarchy_level}
status     = {id, name, category}
priority   = {id, name}
project    = {id, key, name}
components = [{id, name}, ...]
issue      = {key, summary?, status?, issuetype?}
issuelink  = {relationship, issue}
```

The issue-reference shape is shared by `parent`, `subtasks`, and linked issues. `relationship` is the direction-appropriate inward or outward text from Jira. Extra Jira resource keys, including `self`, icons, avatars, descriptions, scope, and nested `fields`, are removed. Absent and unrequested values are omitted rather than returned as `null`.

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

An explicit `fields` list replaces the default completely; the server does not add hidden fields. `fields=[]` returns issue keys only. For `get_issue`, the client uses `fields=id` internally because Jira Cloud treats `fields=""` and `fields=-*` as an unspecified selection whose full result depends on the tenant and permissions. Each result contains `key` and a `fields` object. Explicitly requested unknown or `customfield_*` values are preserved as Jira JSON, apart from the common ADF and timestamp normalization.

If Jira returns a malformed known resource but other issue data is usable, the operation fails explicitly with the reasons and exact JSON paths, followed by a sanitized partial result. Valid independent fields and search items are preserved; the malformed raw object, URLs, icons, and credentials are never included.

## Development

Clone the repository and install dependencies:

```bash
uv sync
```

Run validation:

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv run pytest --cov=jira_mini_mcp --cov-branch --cov-report=term-missing
```

HTTP mocks are based on preliminary read-only observations from a dedicated
non-production Jira Cloud test site, not documentation alone. Raw server
responses are never committed: fixtures preserve the observed structure while
all tenant, user, issue, cursor, timestamp, and content values are replaced with
synthetic data. Live checks are opt-in; the default suite remains offline.

The final command reports statement and branch coverage with missing lines. Run
and review it before closing each implementation phase; the project does not use
an arbitrary fail-under percentage before establishing a meaningful baseline.

Format code:

```bash
uv run ruff format .
```

## Release model

The project uses:

- semantic versioning
- Conventional Commits
- short-lived feature branches
- pull-request CI
- tagged releases

The initial `v0.1.0` is a GitHub Release with CI-checked wheel and source-distribution assets. PyPI publication is a separate future step; when added, it should use GitHub Actions with PyPI Trusted Publishing rather than a long-lived credential.

## Design principle

`jira-mini-mcp` intentionally trades API coverage for agent ergonomics.

A successful MCP server does not need to expose everything its backend can do.

It needs to expose the smallest set of capabilities that lets an agent solve its intended tasks reliably.

For this project:

```text
fewer tools
+ clearer responsibilities
+ compact results
+ sensible defaults
+ explicit pagination
=
less context
less ambiguity
more reliable agent behavior
```

That is the central design constraint of `jira-mini-mcp`.
