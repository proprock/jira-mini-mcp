# Evals

This directory is where claims about the server get tested instead of asserted.

## Tool-selection eval

`README.md` argues that a small, clearly differentiated toolset makes an agent's
tool choice more reliable. This is where that claim gets tested.

The claim splits in two, and so does the test.

### The half that runs by default

`tests/test_tool_schema_hygiene.py` runs in the normal suite and checks the input
a model actually sees: every tool has a substantial description, no two
descriptions are near-duplicates, every parameter whose behavior cannot be
guessed from its name is explained in prose rather than left to the schema, and
each surprising rule — `limit=0` means unbounded, `fields` replaces a default,
`labels` replaces the whole list — is stated somewhere a model will read it.

Those are the properties a wrong tool choice usually traces back to, and they
cost nothing to check.

### The half that does not

`run_eval.py` puts the real tool definitions in front of a real model and scores
which tool it picks. That needs credentials and spends money, so it is not part
of `uv run pytest` and never will be.

```bash
uv run python evals/run_eval.py
```

It needs an Anthropic credential: `ANTHROPIC_API_KEY`, or a profile from
`ant auth login`. It does **not** need Jira credentials and never contacts Jira —
the scenarios stop at the choice of tool and its arguments.

Options:

```text
--model MODEL   default claude-opus-5
--only ID       run a single scenario
--json          machine-readable results
```

A full run is 16 requests. Expect roughly $0.20-0.50 on the default model; the
run prints its actual token usage, which is the number to trust. Exit status is
0 only when every scenario passes.

### The scenarios

`scenarios.json` holds them. Each prompt is phrased the way a developer would
actually put it and never names a tool, so the model has to choose from the
descriptions alone. The pairs that can be confused are covered on purpose:

| Confusion | Scenarios |
|---|---|
| comment vs. field update | `record-a-link`, `fix-the-title`, `take-the-ticket` |
| transition vs. field update | `start-work`, `label-it` |
| one call vs. two | `finish-with-a-note` (transition carries the comment) |
| discussion vs. history | `recent-discussion`, `field-history` |
| listing vs. fetching | `list-attachments`, `fetch-attachment` |
| defaults vs. explicit arguments | `whole-discussion` (`limit=0`), `discussion-since-an-event` (`since`) |

A scenario grades the tool name, then any arguments it names: exact values under
`arguments`, presence under `has_arguments`, absence under `lacks_arguments`.

`tool_choice` is left on its default rather than forcing a call. A model that
answers in prose instead of calling anything has made a selection error, and
forcing a call would hide it.

### Reading a failure

A failure is a signal about the tool descriptions at least as much as about the
model. When one appears, read the description of the tool it *did* choose and ask
what in that text made it look right, then read the description of the tool it
should have chosen and ask what was missing that would have distinguished them.
Fixing the prose is usually the correct response; adding a tool almost never is.

Two cautions. Sixteen scenarios is a small sample and the model is not
deterministic, so a single flipped result is noise, not a regression — rerun
before acting on one. And these prompts are unambiguous by construction; real
requests are not, so a perfect score means the descriptions are distinguishable,
not that the toolset is right.

## Structured-output savings eval

`DISABLE_STRUCTURED_OUTPUT` (see `PROJECT-CONTRACTS.md`'s Configuration
section) lets an operator stop a tool from duplicating its JSON as both
`content` and `structuredContent`. Extra 1 in `PLAN.agents.md` deliberately
did not claim that duplication costs anything — this eval is the
measurement, not an assumption. It never claims the savings *matter* to any
given host; it only reports how large they are.

```bash
uv run python evals/structured_output_savings.py
uv run python evals/structured_output_savings.py --requests 500
uv run python evals/structured_output_savings.py --json
```

It needs no credentials at all — unlike `run_eval.py`, it never calls a model
or Jira. A synthetic in-memory `JiraClient` stand-in (the same duck-typing
`tests/test_server.py` uses) feeds the real server's response-shaping code, so
what gets measured is the exact `CallToolResult` jira-mini-mcp sends, not a
guess about it.

A fixed mix of the nine tools, weighted like a realistic agent session
(`get_issue` and `search_issues` most common, `download_attachment` least),
is replayed `--requests` times (100 by default) with structured output on and
off, and the table reports each tool's byte savings and the overall total:

```text
  tool                  reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------------
  get_issue               30       79140       47490    40.0%
  search_issues           20      217220      142760    34.3%
  ...
  -----------------------------------------------------------
  TOTAL                  100      386286      247976    35.8%
```

The byte counts are each request's `CallToolResult` alone; the constant
JSON-RPC envelope (`jsonrpc`/`id`/`result`) is not included on either side,
since the setting does not change it. Synthetic ticket/comment/changelog
data is sized like a real one, not maximal — treat the percentages as
representative of a typical mix, not a worst case.
