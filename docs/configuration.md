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
| `STRUCTURED_OUTPUT` | no | `true`, `1`, `on` also sends `structuredContent` and an `outputSchema`; off by default, see [Cheaper output](#cheaper-output) |
| `DISABLE_STRUCTURED_OUTPUT` | no | With `STRUCTURED_OUTPUT=true`, comma-separated tool names that still return `content` only |

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

By default every tool returns its result once: the JSON as text in `content`.
Setting `STRUCTURED_OUTPUT=true` (or `1`, `on`) also sends the same JSON as
`structuredContent` and advertises an `outputSchema` for each tool, as the MCP
spec allows. A host that forwards both copies to the model pays for the same
data twice.

**Why it is off.** Every tool returns a free-form `dict[str, Any]`, so the
`outputSchema` cannot describe a result's fields, and a coding agent reads the
text either way. The duplicate is the price of a schema that says almost
nothing. `evals/structured_output_savings.py` measures the exact result this
server sends in both modes. Across replayed agent sessions on synthetic tickets
the duplicate is about 35-40% of the `CallToolResult` (36.6% for a weighted mix
of all nine tools), and 42-43% on real tickets from a non-production site. The
largest read tools carry the most: `get_comments` about 42%, `get_issue` about
41%, `search_issues` about 35%. Two caveats: these are bytes on the wire, not
billed tokens, and they only matter where the host actually sends both copies to
the model. Run the script for your own numbers; see
[`evals/README.md`](../evals/README.md).

**When to turn it on.** A host or pipeline that consumes results as typed data
-- validating or parsing `structuredContent` in code -- needs it:

```text
STRUCTURED_OUTPUT=true
```

Anything else than `true`, `1`, `on`, `false`, `0`, `off`, or empty (matched
ignoring case) stops startup rather than quietly choosing a mode.

**Narrowing it.** With `STRUCTURED_OUTPUT=true`, `DISABLE_STRUCTURED_OUTPUT` names
the tools that should still return `content` only:

```text
STRUCTURED_OUTPUT=true
DISABLE_STRUCTURED_OUTPUT=search_issues,get_issue,get_comments,get_changelog
```

An empty or absent value changes nothing. A name that is not one of the nine
tools stops startup and lists the valid ones, so a typo never leaves you
believing a payload shrank when it did not. Without `STRUCTURED_OUTPUT=true` the
list has no effect, and startup says so on stderr.

**What stays the same.**

- `content` carries the *same* compact JSON, byte for byte, in every mode. A
  model reads that text and sees the same fields, Markdown, timestamps, and
  pagination values.
- Arguments, defaults, pagination, and error behavior are identical. Errors were
  always text-only, with the sanitized partial result inline.
- What you give up by default is the machine-checkable `outputSchema` and typed
  `structuredContent`, which only a programmatic client that validates or
  parses results in code makes use of.
