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
did not claim that duplication costs anything -- this eval is the
measurement, not an assumption. It never claims the savings *matter* to any
given host; it only reports how large they are.

```bash
# per tool: a weighted mix of the nine tools (default)
uv run python evals/structured_output_savings.py
uv run python evals/structured_output_savings.py --requests 500

# per task: replay a typical session from session_profiles.json
uv run python evals/structured_output_savings.py --mode profile
uv run python evals/structured_output_savings.py --profile old_ticket --profile-size max

# against real tickets on the test site (read-only, see below)
uv run python evals/structured_output_savings.py --live

# any of them, machine-readable
uv run python evals/structured_output_savings.py --mode profile --json
```

Without `--live` it needs no credentials at all -- unlike `run_eval.py`, it never
calls a model or Jira. A synthetic in-memory `JiraClient` stand-in (the same
duck-typing `tests/test_server.py` uses) feeds the real server's
response-shaping code, so what gets measured is the exact `CallToolResult`
jira-mini-mcp sends, not a guess about it. The stand-in honors `start_at` and
`limit` like the real tools, so the default 20-item page of a 170-event history
is priced as a 20-item page.

The byte counts are each request's `CallToolResult` alone; the constant
JSON-RPC envelope (`jsonrpc`/`id`/`result`) is not included on either side,
since the setting does not change it.

### Two views

**Tools mode** (`--mode tools`, the default) replays a fixed mix of the nine
tools, weighted like a realistic session (`get_issue` and `search_issues` most
common, `download_attachment` least), `--requests` times, and reports each
tool's saving. It answers "which tool should I turn this off for".

**Profile mode** (`--mode profile`) replays a whole task from
`session_profiles.json` and reports the saving for the session. It answers
"what does this cost on a typical job":

| Profile | Task |
|---|---|
| `fresh_ticket` (A) | read the ticket, list and download a file, comment, transition |
| `old_ticket` (B) | a long discussion and history: 2-3 comment pages, a changelog page, 1-2 downloads |
| `resume_work` (C) | `get_comments` with `since`, optional attachments and transition |
| `discovery` (D) | 1-3 searches, then 1-3 `get_issue` calls |

In a profile, the ticket is sized by the profile's `data` block
(`comments`, `changelog_events`, `attachments`, `attachment_bytes`,
`description_chars`, `comment_chars`) and each call has a `count`. A value is a
constant, a `[lo, hi]` range (typical = the rounded-up midpoint), or an observed
`[lo, typical, hi]` triple. `--profile-size min|typical|max` picks which end of
each range to use, so one run shows the spread instead of one number. Repeated
calls of one tool are priced as identical requests (the second comment page is
costed like the first), and `resume_work`'s `since` window size is
hand-authored -- it was not observed. `defaults` in the same file sizes the
ticket behind tools mode.

### Live mode

`--live` runs the profiles against real tickets. It is opt-in and read-only:

- Tickets come from `EVAL_LIVE_ISSUES` (comma-separated keys) and credentials
  from the usual `JIRA_*` variables, either in the environment or in the
  gitignored `.env`. Keys are real, so they are never stored in the repo.
- `get_issue`, `get_comments`, `get_attachments`, `get_changelog` and
  `search_issues` hit the real site. `add_comment`, `transition_issue`,
  `update_issue` and `download_attachment` stay on the synthetic stand-in, so a
  live run cannot write to Jira or save a file. `since` in a live run means the
  last 3 days.
- Output names tickets only `ticket-1`, `ticket-2`, ...; no key, host, name,
  or text is printed, `httpx2` request logging is silenced, and a tool error
  stops the run without echoing its message.
- Use the non-production test site only (`QUALITY.md`). Never commit or paste a
  live run's output without checking it for tenant data first.

### Observed ticket sizes

What the profiles are built from, observed 2026-09-18 on the non-production test
site (read-only; counts and sizes only). The population is every ticket of one
assignee: 365 found, 351 read successfully (14 failed and are excluded). "Old"
means 30 or more comments, which is 99 of the 351 (28%). Ranges, not means:
min .. **p10-median-p90** .. max.

| | All tickets (351) | Old, 30+ comments (99) |
|---|---|---|
| Description, chars | 0 .. **201-631-1601** .. 9857 | 30 .. **311-760-1989** .. 9857 |
| Comments per ticket | 0 .. **4-15-56** .. 253 | 30 .. **32-44-120** .. 253 |
| Comment length, chars | 0 .. **44-153-557** .. 31143 | 0 .. **49-166-632** .. 9245 |
| Attachments per ticket | 0 .. **0-4-22** .. 119 | 1 .. **4-15-45** .. 119 |
| Attachment size, bytes | 277 .. **11845-57622-191523** .. 206 MB | 295 .. **12303-58248-197294** .. 104 MB |
| Changelog events | 5 .. **23-64-177** .. 927 | 51 .. **86-148-370** .. 927 |

Comment length is over 9127 comments, attachment size over 3059 attachments.
Beyond the p90 the tails are heavy (p99 comment 2250 chars, p99 attachment
12.9 MB, max 206 MB), which is why the profiles use the p10-p90 band and not
the maximum.

Old tickets are longer than a guess of "30-50 comments, 20-40 events": the
median old ticket holds 44 comments and 148 events, the top decile 120 and 370.
The default page size (20) caps what one call returns, so a long history costs
a few pages, not the whole history, unless the agent asks for `limit=0`.
`get_issue` serialized to 0.7-11.5 KB (median 1.8 KB, p90 3.5 KB). 13% of
tickets have no attachment and 2% no comment. An earlier 44-ticket sample
(recent plus the 12 most-commented) put the median at 8 comments and 35 events
and the p90 attachment at 311 KB; the full population supersedes it.

### Results

The full, untrimmed output of the runs named in each heading. The synthetic
ones are reproducible; the live one depends on the test site's current state.

#### Tools mode, `--requests 100`, typical ticket

```text
  tool                  reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------------
  get_issue               30       90330       52920    41.4%
  search_issues           20      428620      280960    34.5%
  get_comments            15      158100       91575    42.1%
  get_changelog           10      113880       75300    33.9%
  get_attachments          5       11470        7365    35.8%
  add_comment              8        5256        3448    34.4%
  transition_issue         5        2745        1950    29.0%
  update_issue             5        1825        1385    24.1%
  download_attachment      2        1012         694    31.4%
  -----------------------------------------------------------
  TOTAL                  100      813238      515597    36.6%

  Across 100 simulated requests, disabling structured output for every named tool saves 36.6% of the CallToolResult payload (297641 of 813238 bytes).
```

#### Profile mode, typical

```text
A. Fresh ticket: read it, look at its files, report back  [typical]

  tool                  reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------------
  get_issue                1        3011        1764    41.4%
  get_comments             1       10540        6105    42.1%
  get_attachments          1        2294        1473    35.8%
  download_attachment      1         506         347    31.4%
  add_comment              1         657         431    34.4%
  transition_issue         1         549         390    29.0%
  -----------------------------------------------------------
  TOTAL                    6       17557       10510    40.1%

  Over this session's 6 calls, disabling structured output for every named tool saves 40.1% of the CallToolResult payload (7047 of 17557 bytes).

B. Old ticket: long discussion and history  [typical]

  tool                  reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------------
  get_issue                1        3271        1894    42.1%
  get_comments             3       43446       24978    42.5%
  get_attachments          1        8004        5010    37.4%
  download_attachment      2        1012         694    31.4%
  get_changelog            1       11390        7531    33.9%
  add_comment              1         657         431    34.4%
  transition_issue         1         549         390    29.0%
  -----------------------------------------------------------
  TOTAL                   10       68329       40928    40.1%

  Over this session's 10 calls, disabling structured output for every named tool saves 40.1% of the CallToolResult payload (27401 of 68329 bytes).

C. Resume after recent work: only what changed since last session  [typical]

  tool               reqs    ON bytes   OFF bytes    saved
  --------------------------------------------------------
  get_issue             1        3011        1764    41.4%
  get_comments          1        1648        1009    38.8%
  get_attachments       1        2294        1473    35.8%
  add_comment           1         657         431    34.4%
  transition_issue      1         549         390    29.0%
  --------------------------------------------------------
  TOTAL                 5        8159        5067    37.9%

  Over this session's 5 calls, disabling structured output for every named tool saves 37.9% of the CallToolResult payload (3092 of 8159 bytes).

D. Discovery: search, then open the candidates  [typical]

  tool            reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------
  search_issues      2       42862       28096    34.5%
  get_issue          2        6022        3528    41.4%
  -----------------------------------------------------
  TOTAL              4       48884       31624    35.3%

  Over this session's 4 calls, disabling structured output for every named tool saves 35.3% of the CallToolResult payload (17260 of 48884 bytes).
```

#### Profile totals across sizes

The `min`, `typical`, and `max` end of every range:

```text
  profile        size      calls  ON bytes  OFF bytes  saved
  -------------------------------------------------------------
  fresh_ticket   min           6      6225       4044   35.0%
  fresh_ticket   typical       6     17557      10510   40.1%
  fresh_ticket   max           6     48473      27334   43.6%

  old_ticket     min           8     37369      23587   36.9%
  old_ticket     typical      10     68329      40928   40.1%
  old_ticket     max          10    142425      79836   44.0%

  resume_work    min           3      3086       1987   35.6%
  resume_work    typical       5      8159       5067   37.9%
  resume_work    max           5     27075      15841   41.5%

  discovery      min           2     23582      15382   34.8%
  discovery      typical       4     48884      31624   35.3%
  discovery      max           6     79146      50346   36.4%
```

#### Live mode, typical, four test-site tickets

`ticket-1` and `ticket-2` are long-running tickets (over 100 comments and 600
events each); `ticket-3` and `ticket-4` are small. The saving is 35-47% across
all of them while the byte counts differ by more than 10x.

```text
A. Fresh ticket: read it, look at its files, report back  [typical, live]

  tool                  reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------------
  get_issue                4       25309       15631    38.2%
  get_comments             4       87555       46718    46.6%
  get_attachments          4       95922       58627    38.9%
  download_attachment      4        2024        1388    31.4%
  add_comment              4        2628        1724    34.4%
  transition_issue         4        2196        1560    29.0%
  -----------------------------------------------------------
  TOTAL                   24      215634      125648    41.7%

  Across 4 live tickets, disabling structured output for every named tool saves 41.7% of the CallToolResult payload (89986 of 215634 bytes).

  ticket     reqs    ON bytes   OFF bytes    saved
  ------------------------------------------------
  ticket-1      6      104450       60192    42.4%
  ticket-2      6       92765       53708    42.1%
  ticket-3      6       11364        7192    36.7%
  ticket-4      6        7055        4556    35.4%
  ------------------------------------------------
  TOTAL        24      215634      125648    41.7%

B. Old ticket: long discussion and history  [typical, live]

  tool                  reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------------
  get_issue                4       25309       15631    38.2%
  get_comments            12      262665      140154    46.6%
  get_attachments          4       95922       58627    38.9%
  download_attachment      8        4048        2776    31.4%
  get_changelog            4       67794       40112    40.8%
  add_comment              4        2628        1724    34.4%
  transition_issue         4        2196        1560    29.0%
  -----------------------------------------------------------
  TOTAL                   40      460562      260584    43.4%

  Across 4 live tickets, disabling structured output for every named tool saves 43.4% of the CallToolResult payload (199978 of 460562 bytes).

  ticket     reqs    ON bytes   OFF bytes    saved
  ------------------------------------------------
  ticket-1     10      218158      121735    44.2%
  ticket-2     10      199464      111733    44.0%
  ticket-3     10       30189       18889    37.4%
  ticket-4     10       12751        8227    35.5%
  ------------------------------------------------
  TOTAL        40      460562      260584    43.4%

C. Resume after recent work: only what changed since last session  [typical, live]

  tool               reqs    ON bytes   OFF bytes    saved
  --------------------------------------------------------
  get_issue             4       25309       15631    38.2%
  get_comments          4       56993       30180    47.0%
  get_attachments       4       95922       58627    38.9%
  add_comment           4        2628        1724    34.4%
  transition_issue      4        2196        1560    29.0%
  --------------------------------------------------------
  TOTAL                20      183048      107722    41.2%

  Across 4 live tickets, disabling structured output for every named tool saves 41.2% of the CallToolResult payload (75326 of 183048 bytes).

  ticket     reqs    ON bytes   OFF bytes    saved
  ------------------------------------------------
  ticket-1      5      101288       58400    42.3%
  ticket-2      5       67033       39812    40.6%
  ticket-3      5        8878        5703    35.8%
  ticket-4      5        5849        3807    34.9%
  ------------------------------------------------
  TOTAL        20      183048      107722    41.2%

D. Discovery: search, then open the candidates  [typical, live]

  tool            reqs    ON bytes   OFF bytes    saved
  -----------------------------------------------------
  search_issues      8       42552       28000    34.2%
  get_issue          8       50618       31262    38.2%
  -----------------------------------------------------
  TOTAL             16       93170       59262    36.4%

  Across 4 live tickets, disabling structured output for every named tool saves 36.4% of the CallToolResult payload (33908 of 93170 bytes).

  ticket     reqs    ON bytes   OFF bytes    saved
  ------------------------------------------------
  ticket-1      4       29494       18518    37.2%
  ticket-2      4       26150       16586    36.6%
  ticket-3      4       20730       13320    35.7%
  ticket-4      4       16796       10838    35.5%
  ------------------------------------------------
  TOTAL        16       93170       59262    36.4%
```

The saving is stable at roughly 35-45% of the payload wherever the bytes come
from; what changes with the task is how many bytes there are to save. Whether
that matters is a host/client decision, not something this script settles.
