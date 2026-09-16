# jira-mini-mcp

Minimal read-only Jira MCP server for coding agents. Keep the public surface
small, predictable, compact, and suitable for direct installation from GitHub.

## Always-applicable rules

- The MVP supports Jira Cloud REST API v3 only. Do not add Server/Data Center
  compatibility, Confluence, board, sprint, worklog, or administration
  features without a concrete agent use case.
- Read access is unrestricted within that scope. Write access is limited to
  exactly three tools: `add_comment`, `transition_issue`, and `update_issue`
  (PLAN.agents.md Phase 14). Every other write operation -- issue creation,
  issue links, attachment upload, comment or issue deletion, worklogs,
  administration -- needs a concrete agent use case and the user's explicit
  approval. A write tool is a public API change: it gets a contract in
  `PROJECT-CONTRACTS.md` before it is implemented.
- Annotate every write tool so `READ_ONLY_MODE` can withhold it:
  `readOnlyHint=False`, plus honest `destructiveHint` and `idempotentHint`
  values. The gate filters on that annotation and never on tool names, so
  operators can run the server with no state-changing tool registered.
- Keep MCP tools orthogonal. Do not add aliases or mirror Jira REST endpoints.
- Preserve published tool names, arguments, defaults, and response schemas
  unless a deliberate public API change is required.
- Normalize known Jira REST resources into the exact compact schemas in
  `PROJECT-CONTRACTS.md`; preserve Jira JSON only for unknown and
  `customfield_*` values. Never silently truncate a collection or disguise a
  failed request as an empty one.
- Comments and changelog responses use only `start_at`, exact `total`, and
  `items`; their pagination is defined over the logical filtered collection,
  not Jira's incidental upstream page order. Search follows Jira Cloud v3's
  cursor API and uses `items` plus `next_page_token`, without inventing an exact
  total.
- The mandatory part of configuration consists of exactly `JIRA_BASE_URL`,
  `JIRA_EMAIL`, and `JIRA_API_TOKEN` for the MVP. Do not add another required
  setup parameter casually. Optional parameters may be added only with the
  user's explicit approval.
- Never hardcode or log Jira URLs, credentials, authorization headers, OAuth
  secrets, cloud IDs, or attachment contents. Redact sensitive values in errors.
- Make tool errors actionable: state the cause and, when known, how the caller
  can correct it without exposing sensitive data.
- When a malformed known resource allows a useful partial issue result, keep
  the successfully normalized data and report the incomplete response as an
  error with exact JSON paths; never leak the malformed raw object.
- Use async I/O, type hints, small explicit modules, and the existing
  dependencies before considering a new abstraction or dependency.
- Keep the HTTP boundary mockable; default tests must not need a live Jira
  instance or real credentials.
- Derive HTTP mock shapes from read-only observations on a non-production Jira
  Cloud test site, then replace every tenant, user, issue, cursor, timestamp,
  and content value with synthetic data. Never commit raw captures.
- Before closing an implementation phase, review and report statement and
  branch coverage with missing lines; coverage does not replace behavioral
  assertions.
- Use short-lived, focused branches for substantial features. Do not develop a
  substantial feature directly on `master`.

## Read the applicable detail before changing behavior

- [PROJECT-CONTRACTS.md](PROJECT-CONTRACTS.md) — product goals, MCP tool
  contracts, pagination, response design, packaging, and release compatibility.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — stack, client lifecycle, module
  boundaries, authentication, errors, performance, and coding conventions.
- [QUALITY.md](QUALITY.md) — validation commands, test requirements, and the
  definition of done.
- [GIT-AND-RELEASES.md](GIT-AND-RELEASES.md) — branches, commits, pull
  requests, releases, and GitHub Actions.
- [AGENT-WORKFLOW.md](AGENT-WORKFLOW.md) — task routing, ownership,
  delegation, and development principles.

Read every linked document relevant to the files or public behavior you will
change. For a small mechanical documentation-only edit, this file and the
specific target document are sufficient.
