# Quality and testing

Use `uv` for dependency and environment management. Typical validation is:

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv run pytest -o addopts="" --cov=jira_mini_mcp --cov-branch --cov-report=term-missing
```

`uv run pytest` is the fast behavioral suite. Tests marked `hygiene`
(documentation and tool-description contract checks) or `slow` (stdio
subprocess) are excluded from it by `addopts`; `-o addopts=""` runs everything,
which is what the coverage command and CI do.

`uv run prek run --all-files` runs the fast subset (file hygiene, format check,
lint, type check, version-file agreement) through the commit hooks; it does not
replace `pytest`. The `version-sync` hook runs `scripts/check_version_sync.py`
only when `pyproject.toml`, `server.json`, `uv.lock`, or `CHANGELOG.md` changes;
CI runs it on every push.

Use `uv run ruff format .` to format; do not manually fight the formatter. Run
all checks relevant to modified code and do not claim a check passed unless it
was executed.

The ordinary pytest run remains the fast correctness check. Before closing each
implementation phase, run the coverage command separately, review statement and
branch coverage plus the missing-line report, and record the result in the phase
or pull-request summary. Coverage is an assessment tool, not a substitute for
behavioral assertions. Do not introduce a repository-wide fail-under threshold
until a justified baseline exists; add tests for relevant uncovered behavior or
explain intentional exclusions.

## Test requirements

### Live Jira evidence and synthetic fixtures

Before writing or accepting HTTP mocks for a Jira endpoint, run the relevant
read-only requests against a dedicated non-production Jira Cloud test site. Base
mock shapes, field presence, nullability, ordering, pagination, headers, and
error mapping on responses observed from that server. Jira documentation is a
useful design source, but it is not sufficient evidence for a mock because it
may lag deployed behavior.

Live verification is opt-in and uses credentials supplied at runtime; default
tests remain fully offline. Never use a production tenant or store its
credentials/data. Do not commit raw captured responses, even from the test
tenant. Keep any temporary capture outside the repository, transform it into a
synthetic fixture, review it for sensitive data, and delete the capture.

Write endpoints cannot be observed read-only, so they follow a different form of
the same rule. Exercise them only against a disposable issue created for that
purpose by the site owner, and never against an issue holding real work. A
separate non-production site is not required and is not always desirable: a real
workflow -- many transitions, transition names that differ from their target
status names, two transitions reaching one status -- is evidence a default
sandbox cannot provide. Restoring the issue afterwards is optional when it is
disposable; the fixture provenance says what was changed.

Record the method, path, and request body you sent together
with the status and response body observed: a write mock has to assert the
request, not only the parsed result. Do not provoke write failures repeatedly
against the live service. Construct a 400 on an invalid transition, a 403 on a
missing permission, and every other failure shape from the closest safely
observed response plus documented status semantics, and mark those fixtures as
hand-authored rather than implying live provenance. Leave the test site in a
state a later observation can reuse.

Synthetic fixtures must preserve the observed structure and behavior while
replacing every tenant-specific or personal value, including base URLs, cloud
and account IDs, project/issue keys, names, emails, tokens/cursors, timestamps,
free text/ADF, filenames, attachment metadata, and custom-field contents. Add a
short fixture provenance note containing only the Jira Cloud endpoint, relevant
non-secret request options, observation date, behavior represented, and the fact
that all values were synthesized. Do not deliberately trigger unsafe or abusive
failure scenarios on the live service; construct those synthetic failures from
the closest safely observed response shape and documented status semantics.
When a positive case could not be observed, mark its synthetic fixture as
hand-authored rather than implying live provenance. In particular, the current
non-empty `components` unit fixture is hand-authored because the test site had
no issue with components assigned.

Mock the HTTP boundary. Cover authentication configuration, Jira error mapping,
pagination across more than one page, default newest-first comments, ascending
comments, `since` filtering and early stop, histories above 100 comments, empty
results, attachment filename sanitization, rate limits, malformed Jira payloads,
network failures, and timeouts. Integration tests must be opt-in and never use
stored production credentials or data.

For comment-related work, explicitly verify latest-first default ordering,
deterministic ordering, explicit pagination, retrieval past 100 comments,
`since` filtering and early termination, and absence of silent truncation.

For write-tool work, explicitly verify:

- the exact method, path, and JSON body sent, not only the parsed response;
- Markdown bodies converted to a valid ADF document;
- argument validation rejecting an empty or meaningless request before any HTTP
  call is made;
- a failed name-to-identifier resolution listing the valid alternatives instead
  of guessing one;
- that no retry, redirect, or error path can apply a non-idempotent write twice;
- that a rejected field names the tool that does support it;
- the full error mapping on a write path, including Jira's per-field 400 detail,
  with no URL, credential, email, or raw body in the message;
- that `READ_ONLY_MODE` withholds every write tool, asserted against a registry
  that actually mixes read-only and write annotations.

Contract tests must also verify:

- Jira Cloud REST API v3 paths and Basic authentication from exactly
  `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN`;
- `search_issues` defaults to `page_token=None`, `limit=20`, accepts `1..100`,
  passes Jira's opaque cursor unchanged between pages, and returns an actionable
  tool error for `limit=0` or another invalid value;
- omitted `fields`, explicit replacement fields, and `fields=[]` for both issue
  tools, with no implicit fields added;
- comment/changelog responses contain exactly `start_at`, exact `total`, and
  `items`, without `max_results`, `is_last`, or `next_start_at`;
- search responses contain exactly `items` and nullable `next_page_token`, never
  an approximate or synthesized `total`;
- comments apply `since` before ordering and slicing, and metadata describes the
  filtered collection;
- comments and changelog treat `limit=0` as unbounded from `start_at`;
- both sort directions across multiple Jira pages, including equal timestamps
  resolved by Jira `id`;
- newest-first changelog uses the end of Jira's oldest-first collection rather
  than merely reversing its first page;
- ADF-to-Markdown conversion, unsupported-node text fallback, and UTC timestamp
  normalization from multiple explicit offsets;
- absent fields are omitted, requested unknown/custom fields are retained, and
  compact user objects contain no email, avatar, or `self` URL;
- each known resource has the exact compact schema from
  `PROJECT-CONTRACTS.md`; extra keys such as `self`, URLs, icons, descriptions,
  scope, nested `fields`, and issue IDs are removed;
- `hierarchyLevel` maps to `hierarchy_level`, `statusCategory.key` maps to
  `category`, and parent, subtasks, and inward/outward issue links share the
  compact issue-reference shape;
- malformed known resources aggregate exact JSON-path problems in
  `JiraIncompleteResponseError` while its partial issue or search result keeps
  only successfully normalized, sanitized data;
- `get_issue(fields=[])` sends the verified `fields=id` sentinel and returns
  `{key, fields: {}}`; its test and comment must not assume a stable count for
  the tenant- and permission-dependent full field set;
- failures are MCP tool errors with cause and corrective guidance where known,
  while logs and model-visible errors contain no URL, credential, header, raw
  response body, or attachment content;
- attachment paths use the attachment identifier, cannot traverse through
  names or symlink/reparse points, replace atomically, remove `.part` files on
  failure/cancellation, and are cleaned up at normal server shutdown.

At the MCP boundary, use an in-process client to assert discovery advertises
exactly the published tools, including their descriptions, defaults, input
schemas, output schemas, and annotations -- read-only and idempotent for a read
tool, and honest `readOnlyHint`/`destructiveHint`/`idempotentHint` values for a
write tool, since `READ_ONLY_MODE` gates on them. Invoke every tool
through that boundary. Add one stdio subprocess smoke test and verify the shared
HTTP client and temporary cache each have one lifespan and close exactly once.

## Tool-selection eval

`tests/test_tool_schema_hygiene.py` runs with the normal suite and checks the
descriptions and schemas a model chooses from: each tool described substantially,
no two descriptions near-duplicates, every non-obvious parameter explained in
prose, and each surprising rule stated where a model will read it. A tool added
without considering those fails it.

`evals/run_eval.py` scores which tool a real model actually picks. It needs a
model credential and spends money, so it stays outside `uv run pytest` and is run
deliberately; see `evals/README.md`. Treat one flipped scenario as noise, and
treat a real failure as a question about the tool descriptions before it is a
question about the model.

## Definition of done

Implementation matches the request; public schemas remain intentional; relevant
edge cases are covered; formatting, linting, type checking, and tests pass; no
sensitive data was introduced; fixtures trace to sanitized test-site evidence;
coverage was reviewed and reported; and documentation changes with public
behavior.
