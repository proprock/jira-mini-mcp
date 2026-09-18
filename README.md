<div align="center">

# jira-mini-mcp

A Jira Cloud MCP server for coding agents: 6 read tools, 9 with writes enabled.

[![CI](https://github.com/proprock/jira-mini-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/proprock/jira-mini-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/proprock/jira-mini-mcp)](https://github.com/proprock/jira-mini-mcp/releases)
[![PyPI Version](https://img.shields.io/pypi/v/jira-mini-mcp)](https://pypi.org/project/jira-mini-mcp/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[![Model Context Protocol compatible](https://img.shields.io/badge/Model_Context_Protocol-compatible-000000?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io)
[![MCP Registry: io.github.proprock/jira-mini-mcp](https://img.shields.io/badge/MCP_Registry-io.github.proprock%2Fjira--mini--mcp-000000?logo=modelcontextprotocol&logoColor=white)](server.json)
[![Auth: API token](https://img.shields.io/badge/Auth-API_token-2EBC4F)](#configure)

<img src="https://raw.githubusercontent.com/proprock/jira-mini-mcp/master/images/swiss-army-knife.jpg" alt="One job. One tool. Done right." width="760">

<i>One job. One tool. Done right.</i>

</div>

<!-- mcp-name: io.github.proprock/jira-mini-mcp -->

General-purpose Atlassian MCP servers expose dozens to hundreds of tools. Every
one costs context before the agent does any useful work, and every near-duplicate
makes the agent's choice less certain. This server gives a coding agent the Jira
context it needs for a ticket, the three ways to answer back, and nothing else.

- **6-9 tools, not 98** - every one earns its place in context, and the
  descriptions are tested so an agent picks the right one; see
  [why so few](#why-so-few-tools) and how it's [compared with the
  alternatives](#compared-with-the-alternatives).
- **A write surface of exactly three tools** - comment, transition, update.
  Set `READ_ONLY_MODE=true` and they never register, not even as a disabled
  entry the agent can see.
- **Compact, predictable output** - stable JSON schemas, Markdown for rich
  text, no `null` spam, no `self` URLs, emails, or avatars; see
  [what the tools return](#what-the-tools-return).
- **Up to ~40% less output on the wire** - turn off the duplicated
  `structuredContent` per tool with `DISABLE_STRUCTURED_OUTPUT`; the model still
  gets the same JSON. See [Cheaper output](#cheaper-output).
- **Nothing is silently cut short** - exact totals on comments and changelog,
  cursor paging on search, and `limit=0` to fetch the rest. A failed request is
  an error, never an empty list.
- **Errors an agent can act on** - a wrong transition name lists every valid
  transition and where it leads, so there is no separate discovery tool. Errors
  never contain your Jira URL, credentials, or raw response bodies.
- **On PyPI** - `uvx jira-mini-mcp` or `pip install jira-mini-mcp`, no repo
  clone or git URL required.

| Tool | Access | Purpose |
|---|---|---|
| `search_issues` | 🟢 read | Find issues with JQL |
| `get_issue` | 🟢 read | One issue's core state and fields |
| `get_comments` | 🟢 read | Recent or historical discussion, paginated |
| `get_attachments` | 🟢 read | Attachment metadata |
| `download_attachment` | 🟢 read | Fetch one attachment |
| `get_changelog` | 🟢 read | Field-change history, paginated |
| `add_comment` | 🔴 write | Post one Markdown comment |
| `transition_issue` | 🔴 write | Move an issue through its workflow |
| `update_issue` | 🔴 write | Set issue fields |

> [!TIP]
> Set `READ_ONLY_MODE=true` and only the six 🟢 read tools register - the
> three 🔴 write tools are withheld entirely, see [Configure](#configure).

- [Install](#install)
- [Configure](#configure)
- [Cheaper output: `DISABLE_STRUCTURED_OUTPUT`](#cheaper-output)
- [What the tools return](#what-the-tools-return)
- [Writing to Jira](#writing-to-jira)
- [Examples](#examples)
- [Why so few tools](#why-so-few-tools)
- [Compared with the alternatives](#compared-with-the-alternatives)
- [Contributing and security](#contributing-and-security)

## Install

```bash
uvx jira-mini-mcp
```

or

```bash
pip install jira-mini-mcp
```

Pin a version when you want a fixed surface: `uvx jira-mini-mcp==1.0.0`.

Running an unreleased commit straight from GitHub also works:

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or `pip`).

## Configure

Three required values, and two optional switches:

| Variable | Required | Meaning |
|---|---|---|
| `JIRA_BASE_URL` | yes | Your site, e.g. `https://example.atlassian.net` |
| `JIRA_EMAIL` | yes | The email your API token belongs to |
| `JIRA_API_TOKEN` | yes | A [Jira Cloud API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `READ_ONLY_MODE` | no | `true`, `1`, `on` registers only the six read tools |
| `DISABLE_STRUCTURED_OUTPUT` | no | Comma-separated tool names that return `content` only, skipping `structuredContent`; see [Cheaper output](#cheaper-output) |

Authentication is Jira Cloud Basic auth with the email and token. Jira
Server/Data Center, PAT/Bearer, and OAuth are not supported. Configuration is
validated at startup, and an error names the missing setting without printing its
value or your Jira URL. An unrecognized `READ_ONLY_MODE` value stops startup
rather than quietly re-enabling the write tools, and an unrecognized name in
`DISABLE_STRUCTURED_OUTPUT` stops startup naming it and every valid tool name.

Keep the token in the host's own configuration and never commit it. The token
carries its account's permissions: an account that cannot transition an issue
still cannot, whatever this server exposes.

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_EMAIL=you@example.com --env JIRA_API_TOKEN=your-token --transport stdio jira-mini -- uvx jira-mini-mcp
```

Put at least one other option between the last `--env` and the server name, as
above - the CLI otherwise reads the name as another `KEY=value` pair.

</details>

<details>
<summary><b>Claude Desktop</b></summary>

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jira-mini": {
      "command": "uvx",
      "args": ["jira-mini-mcp"],
      "env": {
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_EMAIL": "you@example.com",
        "JIRA_API_TOKEN": "your-token"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Codex CLI</b></summary>

```bash
codex mcp add jira-mini --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_EMAIL=you@example.com --env JIRA_API_TOKEN=your-token -- uvx jira-mini-mcp
```

</details>

<details>
<summary><b>Any other stdio host</b></summary>

Command `uvx`, argument `jira-mini-mcp`, and the three environment variables.
Add `READ_ONLY_MODE=true` to withhold the write tools.

</details>

## Cheaper output

By default every tool returns its result twice, as the MCP spec asks: the JSON
as text in `content`, and the same JSON as `structuredContent`, plus an
`outputSchema` advertised for each tool. A host that forwards both copies to the
model pays for the same data twice. `DISABLE_STRUCTURED_OUTPUT` names the tools
that should return `content` only:

```text
DISABLE_STRUCTURED_OUTPUT=search_issues,get_issue,get_comments,get_changelog
```

An empty or absent value changes nothing. A name that is not one of the nine
tools stops startup and lists the valid ones, so a typo never leaves you
believing a payload shrank when it did not.

**What it saves.** `evals/structured_output_savings.py` measures the exact result
this server sends in both modes. Across replayed agent sessions on synthetic
tickets it cuts the `CallToolResult` by about 35-40% (36.6% for a weighted mix of
all nine tools), and by 42-43% on real tickets from a non-production site. The
largest read tools gain the most: `get_comments` about 42%, `get_issue` about
41%, `search_issues` about 35%. Two caveats: these are bytes on the wire, not
billed tokens, and they only matter where the host actually sends both copies to
the model. Run the script for your own numbers; see
[`evals/README.md`](evals/README.md).

**Why it is safe for agent work.**

- `content` carries the *same* compact JSON, byte for byte, whether or not a tool
  is named. A model reads that text either way and sees the same fields,
  Markdown, timestamps, and pagination values.
- Nothing about the contract changes: arguments, defaults, pagination, and error
  behavior are identical. Errors were always text-only, with the sanitized
  partial result inline, so failure handling is unaffected.
- What you give up is the machine-checkable `outputSchema` and typed
  `structuredContent`, which only a programmatic client that validates or
  parses results in code makes use of. A coding agent that reads tool output
  as text does not.

Turn it off for the large read tools, where the saving is real. Leave it on for a
tool whose result a host or pipeline consumes as typed data.

## What the tools return

Structured JSON with stable output schemas, and no second human-readable
rendering of the same result (the JSON is sent as text and as
`structuredContent`; see [Cheaper output](#cheaper-output) to send it once).
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
configure, and the cache is removed at shutdown.

## Writing to Jira

Three tools, chosen so an agent can close the loop on a ticket it worked:

```text
add_comment(issue_key, body)
transition_issue(issue_key, to, comment=None)
update_issue(issue_key, fields)
```

Issue creation, links, attachment upload, worklogs, and deletion are out of
scope. Creation needs per-project, per-type required-field discovery and is a
feature in its own right; a link, or a request for one, fits in a comment.

Three things are worth knowing before an agent writes:

- **`transition_issue` takes a name, not an id.** A transition name or the name
  of the status to reach, matched ignoring case. They differ in real workflows -
  a transition called `In Progress` can produce a status called `In Development`,
  and two differently named transitions can reach one status - so prefer the
  transition name. When nothing matches, the error lists every available
  transition and where it leads. That listing is the discovery mechanism, which
  is why there is no separate `get_transitions` tool.
- **`update_issue` replaces `labels` and `components` wholesale.** There is no
  add or remove verb, so read the issue first if you mean to add one value. It
  takes the same values `get_issue` returns: `assignee` as an account id or the
  literal `"me"`, `description` as Markdown, `customfield_*` as raw Jira JSON.
  It refuses `status` and `comment`, naming the tool that does each.
- **Markdown is converted, not guessed at.** Headings, lists, fenced code,
  inline marks, and links become Jira rich text; anything outside that set stays
  literal rather than being reinterpreted.

Each write tool is annotated `readOnlyHint=false` with honest `destructiveHint`
and `idempotentHint` values, which is what `READ_ONLY_MODE` filters on.

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

`assignee` accepts `"me"`. `description` takes Markdown, which is converted to
Jira rich text.

**Same prompts, no write access.** With `READ_ONLY_MODE=true` the server
registers only the six read tools. An agent asked to comment or transition has
no such tool to call, rather than a tool that refuses.

## Why so few tools

A tool definition is a name, a description, an input schema, and often an output
contract. Depending on the client, all of it enters the model's context before
any work happens. A large toolset therefore spends context on capabilities the
current task will never use, and raises the chance of picking the wrong tool,
confusing similar ones, or passing bad parameters.

Six to nine compact schemas stay affordable for a whole session, leaving the context
budget for source code, issue descriptions, stack traces, and reasoning. The
design follows Anthropic's guidance for agent systems: keep toolsets small,
role-scoped, and clearly differentiated.

This is a claim, so the repository tests it. The offline half runs with the suite
and checks that every tool is described substantially, that no two descriptions
are near-duplicates, and that every parameter whose behavior cannot be guessed
from its name is explained in prose. The other half puts the real tool
definitions in front of a real model and scores which one it picks - see
[`evals/README.md`](evals/README.md).

The same principle shapes the responses. `get_issue` does not dump hundreds of
comments, the full changelog, attachment contents, or every custom field; large
resources are fetched only when asked for.

## Compared with the alternatives

|  | jira-mini-mcp | [Official Atlassian MCP](https://github.com/atlassian/atlassian-mcp-server) | [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian) |
|---|---|---|---|
| Scope | Jira only | Jira, Confluence, JSM, Bitbucket, Compass, Loom, and more | Jira and Confluence |
| Deployments | Cloud | Cloud | Cloud, Server/Data Center |
| Hosting | Local, stdio | Remote, Atlassian-hosted | Local (stdio, Docker) or HTTP |
| Auth | API token | OAuth 2.1 or API token | API token, PAT, or OAuth 2.0 |
| Tools | 6-9, always visible | A small default set with on-demand discovery | 98 |
| Writes | 3 tools | Yes, admin-gated by category | Yes |
| License | MIT | Apache 2.0 | MIT |

**The official server is the better choice** when you need breadth across
Atlassian products, OAuth rather than a stored token, Jira Service Management,
or organization-level controls such as permission groups, IP allowlisting, and
audit logs. It is Atlassian's own product, it tracks their APIs, and nothing here
competes with that.

**`mcp-atlassian` is the better choice** when you need Confluence alongside Jira,
Server/Data Center, or simply broader Jira coverage than six to nine tools.

**This server is the better choice** for one narrow case: a coding agent working
a Jira ticket, where the context every tool definition costs is worth more than
the coverage it buys.

## Contributing and security

Setup, checks, the test and eval commands, the branch and commit conventions,
and the release model are in [CONTRIBUTING.md](CONTRIBUTING.md). Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Changes
that affect someone running the server are recorded in
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). Not an official Atlassian product.
