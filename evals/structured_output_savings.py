"""Measure what DISABLE_STRUCTURED_OUTPUT actually saves on the wire.

Deliberately outside the default test suite. By default it needs no credentials
at all -- unlike run_eval.py, it never calls a model or Jira. It drives the
real server's tool-registration and response-shaping code (create_server,
the `_dump_*` functions, the SDK's own structured-output handling) against
a synthetic in-memory JiraClient stand-in, the same duck-typing
tests/test_server.py uses. What it measures is the exact CallToolResult
jira-mini-mcp sends for each mode, not a guess about it.

Two ways to look at the same saving:

    # per tool: one weighted mix of the nine tools (the default)
    uv run python evals/structured_output_savings.py
    uv run python evals/structured_output_savings.py --requests 500

    # per task: replay a typical agent session from evals/session_profiles.json
    uv run python evals/structured_output_savings.py --mode profile
    uv run python evals/structured_output_savings.py --profile old_ticket --profile-size max

    # against real tickets (read-only; see the README for the safety rules)
    uv run python evals/structured_output_savings.py --live

Add --json to any of them for machine-readable output.

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
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
from mcp import Client
from mcp.server.mcpserver import MCPServer

from jira_mini_mcp.auth import ConfigError, load_config_from_env
from jira_mini_mcp.jira import HTTP_TIMEOUT, JiraClient
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
from jira_mini_mcp.server import AppContext, _build_jira_client, create_server

DEFAULT_TOTAL_REQUESTS = 100
REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = Path(__file__).with_name("session_profiles.json")
SIZES = ("min", "typical", "max")
LIVE_ISSUES_VAR = "EVAL_LIVE_ISSUES"
LIVE_SINCE_DAYS = 3

_AGENT = User(account_id="5b10a2844c20165700ede21g", display_name="Dana Agent")
_REPORTER = User(account_id="6c21b3955d31276811fef32h", display_name="Sam Reporter")

_FILLER = (
    "The nightly sync job fails intermittently when the upstream API returns "
    "a paginated response with more than 500 items. "
)


def _text(chars: int) -> str:
    """Deterministic filler of exactly `chars` characters."""
    if chars <= 0:
        return ""
    return (_FILLER * (chars // len(_FILLER) + 1))[:chars]


@dataclass(frozen=True)
class FixtureShape:
    """How big the synthetic ticket behind a run is. Each field is what a
    real ticket was observed to hold (see session_profiles.json)."""

    comments: int
    changelog_events: int
    attachments: int
    attachment_bytes: int
    description_chars: int
    comment_chars: int


_SHAPE_FIELDS = tuple(f.name for f in fields(FixtureShape))


def _issue_fields(summary: str, description_chars: int) -> dict[str, Any]:
    return {
        "summary": summary,
        "description": _text(description_chars),
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


def _window[T](items: list[T], start_at: int, limit: int) -> list[T]:
    """limit=0 means every remaining item, as in the real tools."""
    return items[start_at:] if limit == 0 else items[start_at : start_at + limit]


@dataclass
class FakeJiraClient:
    """Duck-types JiraClient's public surface -- the same technique
    tests/test_server.py uses, so the response shape is exactly what the
    real adapter layer would produce. Collections honor start_at and limit
    the way the real tools do, so a small default page of a long history
    is priced as a small page."""

    search_results: list[IssueSummary]
    get_issue_result: Any
    comments: list[Comment]
    attachments: list[Attachment]
    download_attachment_result: Any
    changelog: list[ChangelogEntry]
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
        return SearchPage(
            items=_window(self.search_results, 0, limit),
            next_page_token="eyJvZmZzZXQiOjIwfQ==",
        )

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
        return Page(
            start_at=start_at,
            total=len(self.comments),
            items=_window(self.comments, start_at, limit),
        )

    async def get_attachments(self, issue_key: str) -> Any:
        return self.attachments

    async def download_attachment(self, attachment_id: str) -> Any:
        return self.download_attachment_result

    async def get_changelog(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
    ) -> Any:
        return Page(
            start_at=start_at,
            total=len(self.changelog),
            items=_window(self.changelog, start_at, limit),
        )

    async def add_comment(self, issue_key: str, body: str) -> Any:
        return self.add_comment_result

    async def transition_issue(self, issue_key: str, to: str, comment: str | None = None) -> Any:
        return self.transition_issue_result

    async def update_issue(self, issue_key: str, fields: dict[str, Any]) -> Any:
        return self.update_issue_result


_MIME_TYPES = ("text/plain", "image/png", "application/pdf", "application/zip")


def _fake_client(shape: FixtureShape) -> FakeJiraClient:
    """One ticket, sized by `shape`."""
    return FakeJiraClient(
        search_results=[
            IssueSummary(key=f"SYN-{100 + i}", fields=_search_summary_fields(i)) for i in range(20)
        ],
        get_issue_result=IssueDetail(
            key="SYN-142",
            fields=_issue_fields("Sync job drops pages past the first", shape.description_chars),
        ),
        comments=[
            Comment(
                id=str(144900 + i),
                author=_AGENT if i % 2 == 0 else _REPORTER,
                body=_text(shape.comment_chars),
                created=f"2026-09-{1 + i % 28:02d}T09:{i % 60:02d}:00Z",
            )
            for i in range(shape.comments)
        ],
        attachments=[
            Attachment(
                id=str(80001 + i),
                filename=f"attachment-{i + 1}.dat",
                mime_type=_MIME_TYPES[i % len(_MIME_TYPES)],
                size=shape.attachment_bytes,
                author=_AGENT,
                created="2026-09-12T11:00:00Z",
            )
            for i in range(shape.attachments)
        ],
        download_attachment_result=DownloadResult(
            attachment_id="80001",
            filename="attachment-1.dat",
            mime_type="text/plain",
            size=shape.attachment_bytes,
            local_path="/cache/80001/attachment-1.dat",
        ),
        changelog=[
            ChangelogEntry(
                id=str(20001 + i),
                author=_AGENT,
                created=f"2026-09-{1 + i % 28:02d}T08:{i % 60:02d}:00Z",
                changes=[
                    ChangelogChange(field="status", from_="To Do", to="In Progress"),
                ]
                if i == 0
                else [
                    ChangelogChange(field="labels", from_=None, to="backend sync"),
                ],
            )
            for i in range(shape.changelog_events)
        ],
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

_TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {s.tool: s.arguments for s in SCENARIOS}


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


async def _server_for(fake: Any, *, disabled: frozenset[str]) -> MCPServer[AppContext]:
    @asynccontextmanager
    async def fake_lifespan(_server: MCPServer[AppContext]):
        yield AppContext(jira_client=cast(JiraClient, fake))

    # Structured output is switched on so `disabled` is what separates the two
    # modes being compared; the server's own default is off.
    return create_server(
        lifespan=fake_lifespan,
        read_only_mode=False,
        structured_output=True,
        disable_structured_output=disabled,
    )


async def _call_once(server: MCPServer[AppContext], tool: str, arguments: dict[str, Any]) -> int:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    if result.is_error:
        # The error text is deliberately not surfaced: with --live it could
        # carry tenant data.
        raise SystemExit(f"{tool} returned an error result; the measurement cannot continue.")
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


async def measure(total_requests: int, shape: FixtureShape) -> dict[str, ToolTally]:
    counts = _allocate(total_requests)
    tallies: dict[str, ToolTally] = {scenario.tool: ToolTally() for scenario in SCENARIOS}

    for scenario in SCENARIOS:
        count = counts[scenario.tool]
        tally = tallies[scenario.tool]
        for _ in range(count):
            fake = _fake_client(shape)
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


# --- session profiles -------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    tool: str
    count: int
    arguments: dict[str, Any]


def _pick(value: int | list[int], size: str) -> int:
    """A constant, a [lo, hi] range (typical = rounded-up midpoint), or an
    observed [lo, typical, hi] triple."""
    if isinstance(value, int):
        return value
    if len(value) == 3:
        return value[SIZES.index(size)]
    lo, hi = value
    return {"min": lo, "typical": (lo + hi + 1) // 2, "max": hi}[size]


def _check_number(path: str, value: Any) -> None:
    def is_count(item: Any) -> bool:
        return isinstance(item, int) and not isinstance(item, bool) and item >= 0

    if is_count(value):
        return
    if (
        isinstance(value, list)
        and len(value) in (2, 3)
        and all(is_count(item) for item in value)
        and value == sorted(value)
    ):
        return
    raise SystemExit(
        f"{PROFILES_PATH.name}: {path} must be a non-negative integer, [lo, hi], or "
        f"[lo, typical, hi] in ascending order; got {value!r}."
    )


def _check_shape(path: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != set(_SHAPE_FIELDS):
        raise SystemExit(
            f"{PROFILES_PATH.name}: {path} must hold exactly these keys: "
            f"{', '.join(_SHAPE_FIELDS)}."
        )
    for name, value in data.items():
        _check_number(f"{path}.{name}", value)
    return data


def _shape(data: dict[str, Any], size: str) -> FixtureShape:
    return FixtureShape(**{name: _pick(data[name], size) for name in _SHAPE_FIELDS})


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    data: dict[str, Any]
    calls: tuple[tuple[str, int | list[int], dict[str, Any]], ...]

    def shape(self, size: str) -> FixtureShape:
        return _shape(self.data, size)

    def calls_for(self, size: str) -> list[_Call]:
        resolved = [
            _Call(tool, _pick(count, size), {**_TOOL_ARGUMENTS[tool], **arguments})
            for tool, count, arguments in self.calls
        ]
        return [call for call in resolved if call.count > 0]


def load_profiles(path: Path = PROFILES_PATH) -> tuple[dict[str, Any], dict[str, Profile]]:
    """Return (defaults, profiles), validated so a typo names its own path."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    defaults = _check_shape("defaults", raw.get("defaults"))
    profiles: dict[str, Profile] = {}
    for profile_id, body in raw.get("profiles", {}).items():
        where = f"profiles.{profile_id}"
        data = _check_shape(f"{where}.data", body.get("data"))
        calls: list[tuple[str, int | list[int], dict[str, Any]]] = []
        for index, call in enumerate(body.get("calls", [])):
            call_path = f"{where}.calls[{index}]"
            tool = call.get("tool")
            if tool not in _TOOL_ARGUMENTS:
                raise SystemExit(
                    f"{PROFILES_PATH.name}: {call_path}.tool {tool!r} is not one of: "
                    f"{', '.join(_TOOL_ARGUMENTS)}."
                )
            _check_number(f"{call_path}.count", call.get("count"))
            calls.append((tool, call["count"], call.get("arguments", {})))
        if not calls:
            raise SystemExit(f"{PROFILES_PATH.name}: {where}.calls must not be empty.")
        profiles[profile_id] = Profile(
            profile_id, body.get("title", profile_id), data, tuple(calls)
        )
    if not profiles:
        raise SystemExit(f"{PROFILES_PATH.name}: no profiles defined.")
    return defaults, profiles


async def _price_calls(
    client: Any, calls: list[_Call], *, key: str | None = None, jql: str | None = None
) -> dict[str, ToolTally]:
    """Price each distinct call once and multiply by its count: repeated
    calls of one tool are treated as identical requests. With `key`, the
    issue-scoped calls target that ticket; with `jql`, search uses it."""
    tallies: dict[str, ToolTally] = {}
    for call in calls:
        arguments = dict(call.arguments)
        if key is not None and "issue_key" in arguments:
            arguments["issue_key"] = key
        if jql is not None and "jql" in arguments:
            arguments["jql"] = jql
        if key is not None and "since" in arguments:
            # A fixed date in the profile would go stale; a live "resume"
            # means the last few days.
            window_start = datetime.now(UTC) - timedelta(days=LIVE_SINCE_DAYS)
            arguments["since"] = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        enabled = await _call_once(
            await _server_for(client, disabled=frozenset()), call.tool, arguments
        )
        disabled = await _call_once(
            await _server_for(client, disabled=frozenset({call.tool})), call.tool, arguments
        )
        tally = tallies.setdefault(call.tool, ToolTally())
        tally.requests += call.count
        tally.enabled_bytes += enabled * call.count
        tally.disabled_bytes += disabled * call.count
    return tallies


async def measure_profile(profile: Profile, size: str) -> dict[str, ToolTally]:
    return await _price_calls(_fake_client(profile.shape(size)), profile.calls_for(size))


# --- live mode --------------------------------------------------------------

_LIVE_READS = frozenset(
    {"search_issues", "get_issue", "get_comments", "get_attachments", "get_changelog"}
)


class _LiveClient:
    """Reads go to the real Jira; everything that writes or saves a file
    (add_comment, transition_issue, update_issue, download_attachment) stays
    on the synthetic stand-in, so a live run cannot change or download
    anything."""

    def __init__(self, real: JiraClient, fake: FakeJiraClient) -> None:
        self._real = real
        self._fake = fake

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real if name in _LIVE_READS else self._fake, name)


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader; values already in the environment win, and
    nothing read is ever printed."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


@asynccontextmanager
async def _live_session(defaults: dict[str, Any]) -> AsyncIterator[tuple[list[str], _LiveClient]]:
    _load_dotenv(REPO_ROOT / ".env")
    keys = [key.strip() for key in os.environ.get(LIVE_ISSUES_VAR, "").split(",") if key.strip()]
    if not keys:
        raise SystemExit(
            f"--live needs {LIVE_ISSUES_VAR}: a comma-separated list of issue keys on the "
            "test site, set in the environment or in the gitignored .env file."
        )
    try:
        config = load_config_from_env()
    except ConfigError as exc:
        raise SystemExit(f"--live needs Jira credentials. {exc}") from None

    # httpx2 logs each request line, host included, at INFO.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    cache_dir = Path(tempfile.mkdtemp(prefix="jira-mini-mcp-eval-"))
    try:
        async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            real = _build_jira_client(config, http, cache_dir)
            fake = _fake_client(_shape(defaults, "typical"))
            yield keys, _LiveClient(real, fake)
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


async def measure_live(
    profiles: dict[str, Profile], selected: list[str], size: str, defaults: dict[str, Any]
) -> dict[str, dict[str, dict[str, ToolTally]]]:
    """profile id -> ticket alias -> tool -> tally. Tickets are only ever
    named ticket-1, ticket-2, ... in the result."""
    results: dict[str, dict[str, dict[str, ToolTally]]] = {}
    async with _live_session(defaults) as (keys, client):
        jql = f"key in ({', '.join(keys)}) ORDER BY updated DESC"
        for profile_id in selected:
            calls = profiles[profile_id].calls_for(size)
            results[profile_id] = {
                f"ticket-{number}": await _price_calls(client, calls, key=key, jql=jql)
                for number, key in enumerate(keys, start=1)
            }
    return results


# --- reporting --------------------------------------------------------------


def _savings_pct(enabled: int, disabled: int) -> float:
    if enabled == 0:
        return 0.0
    return (enabled - disabled) / enabled * 100


def _total(tallies: dict[str, ToolTally]) -> ToolTally:
    total = ToolTally()
    for tally in tallies.values():
        total.requests += tally.requests
        total.enabled_bytes += tally.enabled_bytes
        total.disabled_bytes += tally.disabled_bytes
    return total


def _print_table(tallies: dict[str, ToolTally], *, label: str = "tool") -> ToolTally:
    width = max(len(name) for name in tallies)
    width = max(width, len("TOTAL"), len(label))
    print()
    header = f"  {label:<{width}}  {'reqs':>5}  {'ON bytes':>10}  {'OFF bytes':>10}  {'saved':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for name, tally in tallies.items():
        if tally.requests == 0:
            continue
        pct = _savings_pct(tally.enabled_bytes, tally.disabled_bytes)
        print(
            f"  {name:<{width}}  {tally.requests:>5}  {tally.enabled_bytes:>10}  "
            f"{tally.disabled_bytes:>10}  {pct:>6.1f}%"
        )

    total = _total(tallies)
    print("  " + "-" * (len(header) - 2))
    total_pct = _savings_pct(total.enabled_bytes, total.disabled_bytes)
    print(
        f"  {'TOTAL':<{width}}  {total.requests:>5}  {total.enabled_bytes:>10}  "
        f"{total.disabled_bytes:>10}  {total_pct:>6.1f}%"
    )
    return total


_FOOTER = (
    "  This is the response payload only -- the constant JSON-RPC envelope\n"
    "  (jsonrpc/id/result) is not counted on either side, and whether the\n"
    "  savings matter is a host/client decision, not something this script\n"
    "  claims to settle.\n"
)


def _sentence(total: ToolTally, subject: str) -> str:
    return (
        f"\n  {subject}, disabling structured output for every named tool saves "
        f"{_savings_pct(total.enabled_bytes, total.disabled_bytes):.1f}% of the CallToolResult "
        f"payload ({total.enabled_bytes - total.disabled_bytes} of {total.enabled_bytes} bytes).\n"
    )


def report_tools(tallies: dict[str, ToolTally]) -> None:
    total = _print_table(tallies)
    print(_sentence(total, f"Across {total.requests} simulated requests"))
    print(_FOOTER)


def report_profile(profile: Profile, size: str, tallies: dict[str, ToolTally]) -> None:
    print(f"\n{profile.title}  [{size}]")
    total = _print_table(tallies)
    print(_sentence(total, f"Over this session's {total.requests} calls"))


def report_live(profile: Profile, size: str, per_ticket: dict[str, dict[str, ToolTally]]) -> None:
    print(f"\n{profile.title}  [{size}, live]")
    merged: dict[str, ToolTally] = {}
    for tallies in per_ticket.values():
        for tool, tally in tallies.items():
            target = merged.setdefault(tool, ToolTally())
            target.requests += tally.requests
            target.enabled_bytes += tally.enabled_bytes
            target.disabled_bytes += tally.disabled_bytes
    total = _print_table(merged)
    print(_sentence(total, f"Across {len(per_ticket)} live tickets"))
    _print_table({alias: _total(tallies) for alias, tallies in per_ticket.items()}, label="ticket")


def _tally_json(tally: ToolTally) -> dict[str, Any]:
    return {
        "requests": tally.requests,
        "enabled_bytes": tally.enabled_bytes,
        "disabled_bytes": tally.disabled_bytes,
        "savings_pct": round(_savings_pct(tally.enabled_bytes, tally.disabled_bytes), 2),
    }


def _tallies_json(tallies: dict[str, ToolTally]) -> dict[str, Any]:
    return {
        "tools": {tool: _tally_json(tally) for tool, tally in tallies.items()},
        "total": _tally_json(_total(tallies)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--mode", choices=("tools", "profile"), default="tools")
    parser.add_argument(
        "--requests",
        type=int,
        default=DEFAULT_TOTAL_REQUESTS,
        help="tools mode: total simulated requests",
    )
    parser.add_argument(
        "--profile",
        default="all",
        help="profile mode: a profile id from session_profiles.json, or 'all'",
    )
    parser.add_argument(
        "--profile-size",
        choices=SIZES,
        default="typical",
        help="which end of each range to use for ticket sizes and call counts",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=f"profile mode against the tickets in {LIVE_ISSUES_VAR}; reads only",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if args.live:
        args.mode = "profile"
    if args.mode == "tools" and args.requests < len(SCENARIOS):
        raise SystemExit(f"--requests must be at least {len(SCENARIOS)} (one per tool)")

    defaults, profiles = load_profiles()

    if args.mode == "tools":
        tallies = asyncio.run(measure(args.requests, _shape(defaults, args.profile_size)))
        if args.json:
            print(
                json.dumps(
                    {"mode": "tools", "requests": args.requests, **_tallies_json(tallies)},
                    indent=2,
                )
            )
        else:
            report_tools(tallies)
        return 0

    if args.profile != "all" and args.profile not in profiles:
        raise SystemExit(
            f"unknown profile {args.profile!r}; choose one of: {', '.join(profiles)}, or all."
        )
    selected = list(profiles) if args.profile == "all" else [args.profile]

    if args.live:
        live = asyncio.run(measure_live(profiles, selected, args.profile_size, defaults))
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": "live",
                        "size": args.profile_size,
                        "profiles": {
                            pid: {
                                alias: _tallies_json(tallies) for alias, tallies in tickets.items()
                            }
                            for pid, tickets in live.items()
                        },
                    },
                    indent=2,
                )
            )
        else:
            for profile_id in selected:
                report_live(profiles[profile_id], args.profile_size, live[profile_id])
            print(_FOOTER)
        return 0

    measured = {
        pid: asyncio.run(measure_profile(profiles[pid], args.profile_size)) for pid in selected
    }
    if args.json:
        print(
            json.dumps(
                {
                    "mode": "profile",
                    "size": args.profile_size,
                    "profiles": {pid: _tallies_json(tallies) for pid, tallies in measured.items()},
                },
                indent=2,
            )
        )
    else:
        for profile_id in selected:
            report_profile(profiles[profile_id], args.profile_size, measured[profile_id])
        print(_FOOTER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
