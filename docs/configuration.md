# Configuration

Every setting `jira-mini-mcp` reads, all from the environment. The
[README](../README.md) has the quick start with an API token;
[oauth.md](oauth.md) walks through OAuth setup.

## Settings

| Variable | Required | Meaning |
|---|---|---|
| `JIRA_BASE_URL` | always | Your site, e.g. `https://example.atlassian.net` |
| `JIRA_AUTH_METHOD` | no | `api_token` (default) or `oauth`, case-insensitive |
| `JIRA_EMAIL` | with `api_token` | The email your API token belongs to |
| `JIRA_API_TOKEN` | with `api_token` | A [Jira Cloud API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRA_OAUTH_CLIENT_ID` | with `oauth` | Client ID of your OAuth 2.0 (3LO) app; see [oauth.md](oauth.md) |
| `JIRA_OAUTH_CLIENT_SECRET` | with `oauth` | Secret of that app |
| `READ_ONLY_MODE` | no | `true`, `1`, `on` registers only the six read tools |
| `DISABLE_STRUCTURED_OUTPUT` | no | Comma-separated tool names that return `content` only; see [Cheaper output](#cheaper-output) |

Configuration is validated at startup, and an error names the missing setting
without printing its value or your Jira URL. Settings of the method you did not
choose are ignored.

## Choosing an authentication method

**`api_token`** is Jira Cloud Basic auth with your email and an API token. It
takes one minute to set up and is the default, so an existing configuration
without `JIRA_AUTH_METHOD` keeps working unchanged.

**`oauth`** authorizes through your browser instead of storing a long-lived
token in the MCP host's configuration. Atlassian requires every OAuth client to
have a secret, so you register an OAuth 2.0 (3LO) app of your own once, then run
`jira-mini-mcp login`. The server refreshes the access token by itself; the
authorization lapses only after 90 days without use, or when you revoke it. Full
steps: [oauth.md](oauth.md).

Either way, the server acts with your account's Jira permissions: an account
that cannot transition an issue still cannot, whatever this server exposes.
Jira Server/Data Center and PAT/Bearer tokens are not supported.

## Withholding the write tools: `READ_ONLY_MODE`

`true`, `1`, and `on` register only the six read tools; the three write tools
are withheld entirely, not even listed as disabled. `false`, `0`, `off`, empty,
and absent register every tool. Matching ignores case and surrounding
whitespace. Any other value stops startup rather than quietly re-enabling the
write tools.

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
[`evals/README.md`](../evals/README.md).

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
