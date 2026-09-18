"""Measure what DISABLE_STRUCTURED_OUTPUT actually saves on the wire.

Deliberately outside the default test suite, but needs no credentials at
all -- unlike run_eval.py, it never calls a model or Jira. It drives the
real server's tool-registration and response-shaping code (create_server,
the `_dump_*` functions, the SDK's own structured-output handling) against
a synthetic in-memory JiraClient stand-in, the same duck-typing
tests/test_server.py uses. What it measures is the exact CallToolResult
jira-mini-mcp sends for each mode, not a guess about it.

    uv run python evals/structured_output_savings.py
    uv run python evals/structured_output_savings.py --requests 500
    uv run python evals/structured_output_savings.py --json

PLAN.agents.md's Extra 1 deliberately did not claim the duplicated
structuredContent payload costs anything -- "an available knob, not a
proven problem... do not use it to fix a performance issue that has not
been observed." This script is that measurement, run on demand; it reports
the savings, it does not decide whether they matter to any given host.

The reported byte counts are each request's `CallToolResult` alone (`content`
plus, when enabled, `structuredContent`), serialized the way the wire does:
`model_dump_json(by_alias=True, exclude_none=True)`. The constant JSON-RPC
envelope around it (`jsonrpc`, `id`, `result`) is not included on either
side, since DISABLE_STRUCTURED_OUTPUT does not change it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from mcp import Client
from mcp.server.mcpserver import MCPServer

from jira_mini_mcp.jira import JiraClient
from jira_mini_mcp.models import (
    Attachment,
    ChangelogChange,
    ChangelogEntry,
    Comment,
    DownloadResult,
    IssueDetail,
    IssueSummary,
    Page,
    SearchPage,
    Transition,
    TransitionResult,
    UpdateResult,
    User,
)
from jira_mini_mcp.server import AppContext, create_server

DEFAULT_TOTAL_REQUESTS = 100

_AGENT = User(account_id="5b10a2844c20165700ede21g", display_name="Dana Agent")
_REPORTER = User(account_id="6c21b3955d31276811fef32h", display_name="Sam Reporter")

_DESCRIPTION = (
    "## Summary\n\nThe nightly sync job fails intermittently when the upstream "
    "API returns a paginated response with more than 500 items. The retry "
    "logic assumes a single page and drops everything past the first one.\n\n"
    "## Steps to reproduce\n\n1. Trigger a full sync against a project with "
    "600+ issues\n2. Watch the job log for a `next_page_token` that is never "
    "followed\n\n## Expected\n\nEvery page is fetched before the job reports "
    "success."
)


def _issue_fields(summary: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "description": _DESCRIPTION,
        "issuetype": {"name": "Bug"},
        "status": {"name": "In Progress", "category": "indeterminate"},
        "priority": {"name": "High"},
        "assignee": _AGENT,
        "reporter": _REPORTER,
        "labels": ["backend", "sync", "needs-triage"],
        "components": ["Sync Service"],
        "created": "2026-09-01T10:15:00Z",
        "updated": "2026-09-17T14:22:00Z",
        "resolutiondate": None,
        "project": {"key": "SYN", "name": "Synthetic Project"},
        "parent": None,
    }


def _search_summary_fields(index: int) -> dict[str, Any]:
    return {
        "summary": f"Sync job drops page {index} of a paginated response",
        "status": {"name": "In Progress", "category": "indeterminate"},
        "issuetype": {"name": "Bug"},
        "priority": {"name": "High"},
        "assignee": _AGENT,
        "updated": "2026-09-17T14:22:00Z",
        "project": {"key": "SYN", "name": "Synthetic Project"},
    }


@dataclass
class FakeJiraClient:
    """Duck-types JiraClient's public surface with one canned result per
    call -- the same technique tests/test_server.py uses, so the response
    shape is exactly what the real adapter layer would produce."""

    search_issues_result: Any
    get_issue_result: Any
    get_comments_result: Any
    get_attachments_result: Any
    download_attachment_result: Any
    get_changelog_result: Any
    add_comment_result: Any
    transition_issue_result: Any
    update_issue_result: Any

    async def search_issues(
        self,
        jql: str,
        page_token: str | None = None,
        limit: int = 20,
        fields: list[str] | None = None,
    ) -> Any:
        return self.search_issues_result

    async def get_issue(self, issue_key: str, fields: list[str] | None = None) -> Any:
        return self.get_issue_result

    async def get_comments(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
        since: str | None = None,
    ) -> Any:
        return self.get_comments_result

    async def get_attachments(self, issue_key: str) -> Any:
        return self.get_attachments_result

    async def download_attachment(self, attachment_id: str) -> Any:
        return self.download_attachment_result

    async def get_changelog(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
    ) -> Any:
        return self.get_changelog_result

    async def add_comment(self, issue_key: str, body: str) -> Any:
        return self.add_comment_result

    async def transition_issue(self, issue_key: str, to: str, comment: str | None = None) -> Any:
        return self.transition_issue_result

    async def update_issue(self, issue_key: str, fields: dict[str, Any]) -> Any:
        return self.update_issue_result


def _fake_client() -> FakeJiraClient:
    """One canned result per tool, sized like a real ticket's worth of data."""
    return FakeJiraClient(
        search_issues_result=SearchPage(
            items=[
                IssueSummary(key=f"SYN-{100 + i}", fields=_search_summary_fields(i))
                for i in range(10)
            ],
            next_page_token="eyJvZmZzZXQiOjEwfQ==",
        ),
        get_issue_result=IssueDetail(
            key="SYN-142", fields=_issue_fields("Sync job drops pages past the first")
        ),
        get_comments_result=Page(
            start_at=0,
            total=5,
            items=[
                Comment(
                    id=str(144900 + i),
                    author=_AGENT if i % 2 == 0 else _REPORTER,
                    body=(
                        f"Reproduced on staging with a {600 + i * 10}-issue project; "
                        "the second page never gets requested."
                    ),
                    created=f"2026-09-{10 + i:02d}T09:{i:02d}:00Z",
                )
                for i in range(5)
            ],
        ),
        get_attachments_result=[
            Attachment(
                id=str(80001 + i),
                filename=name,
                mime_type=mime,
                size=size,
                author=_AGENT,
                created="2026-09-12T11:00:00Z",
            )
            for i, (name, mime, size) in enumerate(
                [
                    ("sync-job.log", "text/plain", 48213),
                    ("stack-trace.txt", "text/plain", 2114),
                    ("repro-screenshot.png", "image/png", 391022),
                ]
            )
        ],
        download_attachment_result=DownloadResult(
            attachment_id="80001",
            filename="sync-job.log",
            mime_type="text/plain",
            size=48213,
            local_path="/cache/80001/sync-job.log",
        ),
        get_changelog_result=Page(
            start_at=0,
            total=4,
            items=[
                ChangelogEntry(
                    id=str(20001 + i),
                    author=_AGENT,
                    created=f"2026-09-{5 + i:02d}T08:00:00Z",
                    changes=[
                        ChangelogChange(field="status", from_="To Do", to="In Progress"),
                    ]
                    if i == 0
                    else [
                        ChangelogChange(field="labels", from_=None, to="backend sync"),
                    ],
                )
                for i in range(4)
            ],
        ),
        add_comment_result=Comment(
            id="144916",
            author=_AGENT,
            body="Confirmed the fix handles the multi-page case; deploying to staging.",
            created="2026-09-17T20:08:19Z",
        ),
        transition_issue_result=TransitionResult(
            key="SYN-142",
            transition=Transition(
                id="21",
                name="Start Progress",
                status={"id": "10001", "name": "In Progress", "category": "indeterminate"},
            ),
        ),
        update_issue_result=UpdateResult(
            key="SYN-142", updated_fields=("labels", "assignee", "priority")
        ),
    )


@dataclass(frozen=True)
class _Scenario:
    """One typical request: a tool name, its arguments, and how often it
    shows up relative to the others in a realistic mix of agent traffic."""

    tool: str
    arguments: dict[str, Any]
    weight: int


SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario("get_issue", {"issue_key": "SYN-142"}, weight=30),
    _Scenario("search_issues", {"jql": "project = SYN AND status = 'In Progress'"}, weight=20),
    _Scenario("get_comments", {"issue_key": "SYN-142"}, weight=15),
    _Scenario("get_changelog", {"issue_key": "SYN-142"}, weight=10),
    _Scenario("get_attachments", {"issue_key": "SYN-142"}, weight=5),
    _Scenario(
        "add_comment",
        {"issue_key": "SYN-142", "body": "Confirmed the fix handles the multi-page case."},
        weight=8,
    ),
    _Scenario("transition_issue", {"issue_key": "SYN-142", "to": "Start Progress"}, weight=5),
    _Scenario(
        "update_issue",
        {"issue_key": "SYN-142", "fields": {"labels": ["backend", "sync"]}},
        weight=5,
    ),
    _Scenario("download_attachment", {"attachment_id": "80001"}, weight=2),
)


def _allocate(total_requests: int) -> dict[str, int]:
    """Split total_requests across SCENARIOS by weight (largest-remainder
    method), so counts sum to exactly total_requests for any --requests."""
    total_weight = sum(scenario.weight for scenario in SCENARIOS)
    shares = {
        scenario.tool: (scenario.weight * total_requests) / total_weight for scenario in SCENARIOS
    }
    counts = {tool: int(share) for tool, share in shares.items()}
    remainder = total_requests - sum(counts.values())
    by_fractional_part = sorted(
        shares.items(), key=lambda item: item[1] - int(item[1]), reverse=True
    )
    for tool, _ in by_fractional_part[:remainder]:
        counts[tool] += 1
    return counts


@dataclass
class ToolTally:
    requests: int = 0
    enabled_bytes: int = 0
    disabled_bytes: int = 0


async def _server_for(fake: FakeJiraClient, *, disabled: frozenset[str]) -> MCPServer[AppContext]:
    @asynccontextmanager
    async def fake_lifespan(_server: MCPServer[AppContext]):
        yield AppContext(jira_client=cast(JiraClient, fake))

    return create_server(
        lifespan=fake_lifespan, read_only_mode=False, disable_structured_output=disabled
    )


async def _call_once(server: MCPServer[AppContext], tool: str, arguments: dict[str, Any]) -> int:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


async def measure(total_requests: int) -> dict[str, ToolTally]:
    counts = _allocate(total_requests)
    tallies: dict[str, ToolTally] = {scenario.tool: ToolTally() for scenario in SCENARIOS}

    for scenario in SCENARIOS:
        count = counts[scenario.tool]
        tally = tallies[scenario.tool]
        for _ in range(count):
            fake = _fake_client()
            enabled_server = await _server_for(fake, disabled=frozenset())
            disabled_server = await _server_for(fake, disabled=frozenset({scenario.tool}))

            tally.requests += 1
            tally.enabled_bytes += await _call_once(
                enabled_server, scenario.tool, scenario.arguments
            )
            tally.disabled_bytes += await _call_once(
                disabled_server, scenario.tool, scenario.arguments
            )

    return tallies


def _savings_pct(enabled: int, disabled: int) -> float:
    if enabled == 0:
        return 0.0
    return (enabled - disabled) / enabled * 100


def report(tallies: dict[str, ToolTally]) -> None:
    width = max(len(tool) for tool in tallies)
    print()
    header = f"  {'tool':<{width}}  {'reqs':>5}  {'ON bytes':>10}  {'OFF bytes':>10}  {'saved':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    total = ToolTally()
    for tool, tally in tallies.items():
        if tally.requests == 0:
            continue
        pct = _savings_pct(tally.enabled_bytes, tally.disabled_bytes)
        print(
            f"  {tool:<{width}}  {tally.requests:>5}  {tally.enabled_bytes:>10}  "
            f"{tally.disabled_bytes:>10}  {pct:>6.1f}%"
        )
        total.requests += tally.requests
        total.enabled_bytes += tally.enabled_bytes
        total.disabled_bytes += tally.disabled_bytes

    print("  " + "-" * (len(header) - 2))
    total_pct = _savings_pct(total.enabled_bytes, total.disabled_bytes)
    print(
        f"  {'TOTAL':<{width}}  {total.requests:>5}  {total.enabled_bytes:>10}  "
        f"{total.disabled_bytes:>10}  {total_pct:>6.1f}%"
    )
    print(
        f"\n  Across {total.requests} simulated requests, disabling structured output for "
        f"every named tool saves {total_pct:.1f}% of the CallToolResult payload "
        f"({total.enabled_bytes - total.disabled_bytes} of {total.enabled_bytes} bytes).\n"
    )
    print(
        "  This is the response payload only -- the constant JSON-RPC envelope\n"
        "  (jsonrpc/id/result) is not counted on either side, and whether the\n"
        "  savings matter is a host/client decision, not something this script\n"
        "  claims to settle.\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=DEFAULT_TOTAL_REQUESTS)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if args.requests < len(SCENARIOS):
        raise SystemExit(f"--requests must be at least {len(SCENARIOS)} (one per tool)")

    tallies = asyncio.run(measure(args.requests))

    if args.json:
        print(
            json.dumps(
                {
                    "requests": args.requests,
                    "tools": {
                        tool: {
                            "requests": tally.requests,
                            "enabled_bytes": tally.enabled_bytes,
                            "disabled_bytes": tally.disabled_bytes,
                            "savings_pct": round(
                                _savings_pct(tally.enabled_bytes, tally.disabled_bytes), 2
                            ),
                        }
                        for tool, tally in tallies.items()
                    },
                },
                indent=2,
            )
        )
    else:
        report(tallies)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
