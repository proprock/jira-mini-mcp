# Security policy

## Supported versions

Only the latest release receives security fixes. The project is a small,
rolling 1.x line; upgrade to the newest version from
[PyPI](https://pypi.org/project/jira-mini-mcp/) or the
[Releases](https://github.com/proprock/jira-mini-mcp/releases) page.

## Reporting a vulnerability

Do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead:

**[Report a vulnerability](https://github.com/proprock/jira-mini-mcp/security/advisories/new)**

Please include the affected version, what you observed, the steps to reproduce
it against a non-production Jira site, and a suggested fix if you have one.
Never include a real API token, tenant URL, or customer data in a report;
redact them.

## What to expect

This is a small project with one maintainer, so timelines are best effort:

- an acknowledgment within about 7 days;
- an assessment and a plan once the report is understood;
- coordinated disclosure: the fix ships in a release before details are made
  public, and the reporter is credited in the advisory unless they prefer not
  to be.

There is no fixed patch deadline; severity decides the order of work.

## In scope

Problems in this server's own code, such as:

- a Jira URL, credential, authorization header, cloud ID, or raw response body
  reaching a log, a startup message, or a model-visible error;
- `READ_ONLY_MODE` failing to withhold a state-changing tool;
- a download escaping the temporary attachment cache, through a filename,
  symlink, or reparse point;
- a retry, redirect, or error path that applies a non-idempotent write twice;
- a malformed Jira response leaking its raw object into a tool result.

## Out of scope

- Vulnerabilities in Jira, Atlassian services, or third-party dependencies
  (report those upstream).
- An attacker who already holds the API token or controls the host that runs
  the server.
- What the token's own account is allowed to do; this server never widens or
  narrows Jira permissions.
- Prompt injection carried in Jira content. Ticket text, comments, and
  attachments are untrusted input to the agent that reads them. Treat them
  that way, and use `READ_ONLY_MODE` when an agent should not act on them.

## Running the server safely

- Use an API token from a dedicated account with the least Jira permission the
  job needs.
- Set `READ_ONLY_MODE=true` when the agent does not need to write; the three
  write tools are then never registered.
- Keep `JIRA_API_TOKEN` in the host's own configuration. Never commit it or a
  `.env` file, and rotate the token at once if it is exposed.
- Use a non-production Jira site for development and testing, and never commit
  raw Jira responses; see [CONTRIBUTING.md](CONTRIBUTING.md).
