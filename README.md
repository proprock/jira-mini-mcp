# jira-mini-mcp

A nine-tool Jira Cloud MCP server for coding agents.

[![CI](https://github.com/proprock/jira-mini-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/proprock/jira-mini-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/proprock/jira-mini-mcp)](https://github.com/proprock/jira-mini-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

General-purpose Atlassian MCP servers expose dozens to hundreds of tools. Every
one costs context before the agent does any useful work, and every near-duplicate
makes the agent's choice less certain. This server gives a coding agent the Jira
context it needs for a ticket, the three ways to answer back, and nothing else.

| Tool | Purpose |
|---|---|
| `search_issues` | Find issues with JQL |
| `get_issue` | One issue's core state and fields |
| `get_comments` | Recent or historical discussion, paginated |
| `get_attachments` | Attachment metadata |
| `download_attachment` | Fetch one attachment |
| `get_changelog` | Field-change history, paginated |
| `add_comment` | Post one Markdown comment |
| `transition_issue` | Move an issue through its workflow |
| `update_issue` | Set issue fields |

Six read, three write. `READ_ONLY_MODE=true` registers the six alone.

- [Install](#install)
- [Configure](#configure)
- [What the tools return](#what-the-tools-return)
- [Writing to Jira](#writing-to-jira)
- [Why so few tools](#why-so-few-tools)
- [Compared with the alternatives](#compared-with-the-alternatives)
- [Development](#development)
- [Release model](#release-model)

## Install

No installation step: run it straight from GitHub.

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

Pin a release when you want a fixed surface:

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp@v0.1.0 jira-mini-mcp
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Starting with the
next tagged release, the release workflow also publishes to PyPI, after which
`uvx jira-mini-mcp` will be enough.

## Configure

Three required values, and one optional switch:

| Variable | Required | Meaning |
|---|---|---|
| `JIRA_BASE_URL` | yes | Your site, e.g. `https://example.atlassian.net` |
| `JIRA_EMAIL` | yes | The email your API token belongs to |
| `JIRA_API_TOKEN` | yes | A [Jira Cloud API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `READ_ONLY_MODE` | no | `true`, `1`, `on` registers only the six read tools |

Authentication is Jira Cloud Basic auth with the email and token. Jira
Server/Data Center, PAT/Bearer, and OAuth are not supported. Configuration is
validated at startup, and an error names the missing setting without printing its
value or your Jira URL. An unrecognized `READ_ONLY_MODE` value stops startup
rather than quietly re-enabling the write tools.

Keep the token in the host's own configuration and never commit it. The token
carries its account's permissions: an account that cannot transition an issue
still cannot, whatever this server exposes.

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_EMAIL=you@example.com --env JIRA_API_TOKEN=your-token --transport stdio jira-mini -- uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

Put at least one other option between the last `--env` and the server name, as
above — the CLI otherwise reads the name as another `KEY=value` pair.

</details>

<details>
<summary><b>Claude Desktop</b></summary>

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jira-mini": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/proprock/jira-mini-mcp",
        "jira-mini-mcp"
      ],
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
codex mcp add jira-mini --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_EMAIL=you@example.com --env JIRA_API_TOKEN=your-token -- uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

</details>

<details>
<summary><b>Any other stdio host</b></summary>

Command `uvx`, arguments `--from`, the GitHub URL, `jira-mini-mcp`, and the three
environment variables. Add `READ_ONLY_MODE=true` to withhold the write tools.

</details>

## What the tools return

Structured JSON with stable output schemas, and no second human-readable
rendering of the same result. Jira's rich text becomes Markdown inside the
corresponding string field. Timestamps normalize to UTC ISO-8601 with a `Z`.
Users are `account_id` and `display_name` only — no email, avatar, or `self` URL.
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

### Pagination

Large collections never pretend to be complete. `get_comments` and
`get_changelog` return `start_at`, an exact `total`, and `items` — more exist
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
`next_page_token` only — pass the token back as `page_token`, and a null token
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
  of the status to reach, matched ignoring case. They differ in real workflows —
  a transition called `In Progress` can produce a status called `In Development`,
  and two differently named transitions can reach one status — so prefer the
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

## Why so few tools

A tool definition is a name, a description, an input schema, and often an output
contract. Depending on the client, all of it enters the model's context before
any work happens. A large toolset therefore spends context on capabilities the
current task will never use, and raises the chance of picking the wrong tool,
confusing similar ones, or passing bad parameters.

Nine compact schemas stay affordable for a whole session, leaving the context
budget for source code, issue descriptions, stack traces, and reasoning. The
design follows Anthropic's guidance for agent systems: keep toolsets small,
role-scoped, and clearly differentiated.

This is a claim, so the repository tests it. The offline half runs with the suite
and checks that every tool is described substantially, that no two descriptions
are near-duplicates, and that every parameter whose behavior cannot be guessed
from its name is explained in prose. The other half puts the real tool
definitions in front of a real model and scores which one it picks — see
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
| Tools | 9, always visible | A small default set with on-demand discovery | 98 |
| Writes | 3 tools | Yes, admin-gated by category | Yes |
| License | MIT | Apache 2.0 | MIT |

**The official server is the better choice** when you need breadth across
Atlassian products, OAuth rather than a stored token, Jira Service Management,
or organization-level controls such as permission groups, IP allowlisting, and
audit logs. It is Atlassian's own product, it tracks their APIs, and nothing here
competes with that.

**`mcp-atlassian` is the better choice** when you need Confluence alongside Jira,
Server/Data Center, or simply broader Jira coverage than nine tools.

**This server is the better choice** for one narrow case: a coding agent working
a Jira ticket, where the context every tool definition costs is worth more than
the coverage it buys.

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv run pytest --cov=jira_mini_mcp --cov-branch --cov-report=term-missing
```

`uv run ruff format .` applies formatting. The last command reports statement and
branch coverage with missing lines; it is reviewed before closing each phase, and
the project deliberately has no fail-under percentage until a meaningful baseline
exists.

The default suite is fully offline. HTTP mocks trace to observations against a
real Jira Cloud site, but raw responses are never committed: each fixture keeps
the observed structure while every tenant, account, issue, cursor, timestamp, and
content value is synthetic, and records its own provenance. Write endpoints are
exercised only against a disposable issue.

The tool-selection eval needs a model credential and spends money, so it is run
deliberately:

```bash
uv run python evals/run_eval.py
```

Architecture:

```text
agent -> stdio -> MCPServer -> JiraClient -> httpx2.AsyncClient -> Jira REST v3
```

One asynchronous HTTP client is reused for the process lifetime, with an explicit
timeout and bounded retries — a 429 is retried for any method, a 5xx or a dropped
connection only for methods that converge on replay, never a POST. `JiraClient`
knows nothing about MCP; the tools are thin adapters over it.

## Release model

Semantic versioning, Conventional Commits, short-lived branches, pull-request CI,
and tagged releases. [CHANGELOG.md](CHANGELOG.md) records what changed for
someone running the server. `v0.1.0` is a GitHub Release with CI-checked wheel and source
distributions attached. Starting with the next tag, the release workflow also
publishes to PyPI using GitHub Actions Trusted Publishing (OIDC), with no
long-lived credential to manage.

## License

[MIT](LICENSE). Not an official Atlassian product.
