<div align="center">

# jira-mini-mcp

A Jira Cloud MCP server for coding agents: 6 read tools, 9 with writes enabled.

[![CI](https://github.com/proprock/jira-mini-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/proprock/jira-mini-mcp/actions/workflows/ci.yml)
[![M8ven Verified](https://m8ven.ai/badge/mcp/proprock/jira-mini-mcp?variant=verified)](https://m8ven.ai/mcp/proprock/jira-mini-mcp)
[![Release](https://img.shields.io/github/v/release/proprock/jira-mini-mcp)](https://github.com/proprock/jira-mini-mcp/releases)
[![PyPI Version](https://img.shields.io/pypi/v/jira-mini-mcp)](https://pypi.org/project/jira-mini-mcp/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[![Model Context Protocol compatible](https://img.shields.io/badge/Model_Context_Protocol-compatible-000000?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io)
[![MCP Registry: io.github.proprock/jira-mini-mcp](https://img.shields.io/badge/MCP_Registry-io.github.proprock%2Fjira--mini--mcp-000000?logo=modelcontextprotocol&logoColor=white)](server.json)
[![Auth: API token | OAuth 2.0](https://img.shields.io/badge/Auth-API_token_%7C_OAuth_2.0-2EBC4F)](#configure)

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
  [what the tools return](https://github.com/proprock/jira-mini-mcp/blob/master/docs/tools.md).
- **Roughly 40% less output on the wire** - each result is sent once, as JSON
  in `content`, rather than again as `structuredContent`; set
  `STRUCTURED_OUTPUT=true` if your host needs the typed copy. See
  [Cheaper output](https://github.com/proprock/jira-mini-mcp/blob/master/docs/configuration.md#cheaper-output).
- **Nothing is silently cut short** - exact totals on comments and changelog,
  cursor paging on search, and `limit=0` to fetch the rest. A failed request is
  an error, never an empty list.
- **Errors an agent can act on** - a wrong transition name lists every valid
  transition and where it leads, so there is no separate discovery tool. Errors
  never contain your Jira URL, credentials, or raw response bodies.
- **API token or OAuth** - one minute with an API token, or a browser login
  through your own OAuth 2.0 app with automatic refresh; see [Configure](#configure).
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
- [Writing to Jira](#writing-to-jira)
- [Why so few tools](#why-so-few-tools)
- [Compared with the alternatives](#compared-with-the-alternatives)
- [Contributing and security](#contributing-and-security)

More detail lives in [`docs/`](https://github.com/proprock/jira-mini-mcp/blob/master/docs): [configuration](https://github.com/proprock/jira-mini-mcp/blob/master/docs/configuration.md),
[OAuth setup](https://github.com/proprock/jira-mini-mcp/blob/master/docs/oauth.md), and [what the tools return, with
examples](https://github.com/proprock/jira-mini-mcp/blob/master/docs/tools.md).

## Install

```bash
uvx jira-mini-mcp
```

or

```bash
pip install jira-mini-mcp
```

Pin a version when you want a fixed surface: `uvx jira-mini-mcp==1.1.0`.

Running an unreleased commit straight from GitHub also works:

```bash
uvx --from git+https://github.com/proprock/jira-mini-mcp jira-mini-mcp
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or `pip`).

## Configure

With an API token, three values:

| Variable | Meaning |
|---|---|
| `JIRA_BASE_URL` | Your site, e.g. `https://example.atlassian.net` |
| `JIRA_EMAIL` | The email your API token belongs to |
| `JIRA_API_TOKEN` | A [Jira Cloud API token](https://id.atlassian.com/manage-profile/security/api-tokens) |

Add `READ_ONLY_MODE=true` to withhold the write tools. Every setting, including
`STRUCTURED_OUTPUT`, is in [configuration.md](https://github.com/proprock/jira-mini-mcp/blob/master/docs/configuration.md).

> [!NOTE]
> **Prefer OAuth to a stored token?** Set `JIRA_AUTH_METHOD=oauth`, register a
> free OAuth 2.0 (3LO) app, and run `jira-mini-mcp login` once to authorize in
> your browser. The server then refreshes its token by itself. Step by step:
> [oauth.md](https://github.com/proprock/jira-mini-mcp/blob/master/docs/oauth.md).

Configuration is validated at startup, and an error names the missing setting
without printing its value or your Jira URL. Keep the token in the host's own
configuration and never commit it. The server acts with your account's
permissions: an account that cannot transition an issue still cannot, whatever
this server exposes.

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
Add `READ_ONLY_MODE=true` to withhold the write tools. OAuth host examples are in
[oauth.md](https://github.com/proprock/jira-mini-mcp/blob/master/docs/oauth.md#2-configure-the-mcp-host).

</details>

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
  transition and where it leads. To see the options first, ask
  `get_issue(fields=["transitions"])`. That is why there is no separate
  `get_transitions` tool.
- **`update_issue` replaces `labels` and `components` wholesale.** There is no
  add or remove verb, so read the issue first if you mean to add one value. It
  takes the same values `get_issue` returns: `assignee` as an account id, an
  email, a display name, or the literal `"me"`, `description` as Markdown, `customfield_*` as raw Jira JSON.
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
| Auth | API token or OAuth 2.0 (own app) | OAuth 2.1 or API token | API token, PAT, or OAuth 2.0 (own app) |
| Tools | 6-9, always visible | A small default set with on-demand discovery | 98 |
| Writes | 3 tools | Yes, admin-gated by category | Yes |
| License | MIT | Apache 2.0 | MIT |

**The official server is the better choice** when you need breadth across
Atlassian products, OAuth without registering an app of your own, Jira Service Management,
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
