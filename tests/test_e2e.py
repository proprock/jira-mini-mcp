"""End-to-end flows through the real stack.

An MCP `Client` calls a real server whose lifespan hands out a real
`JiraClient` over `httpx2.MockTransport`. `test_server.py` isolates the
adapter with a fake client and `test_jira.py` isolates the client from the
adapter; these tests exist for what only shows when both are joined -- an
argument the adapter forgets to forward, a default that differs between the
layers, a tool result that no longer matches what Jira sent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from jira_mini_mcp.auth import BasicTokenAuth
from jira_mini_mcp.jira import JiraClient
from jira_mini_mcp.server import AppContext, create_server

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://synthetic-tenant.atlassian.net"
API = "/rest/api/3"

Handler = Callable[[httpx2.Request], httpx2.Response]


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _json(status: int, body: Any) -> httpx2.Response:
    return httpx2.Response(status, json=body)


@asynccontextmanager
async def _connected(handler: Handler, tmp_path: Path) -> AsyncIterator[Client]:
    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        auth = BasicTokenAuth("agent@example.com", "super-secret-token")
        try:
            yield AppContext(jira_client=JiraClient(http, auth, BASE_URL, tmp_path))
        finally:
            await http.aclose()

    server = create_server(
        lifespan=lifespan,
        read_only_mode=False,
        structured_output=True,
        disable_structured_output=frozenset(),
    )
    async with Client(server) as client:
        yield client


def _routes(
    routes: dict[tuple[str, str], httpx2.Response],
) -> tuple[Handler, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        try:
            return routes[(request.method, request.url.path)]
        except KeyError:
            raise AssertionError(f"unexpected request {request.method} {request.url}") from None

    return handler, seen


async def test_search_then_follow_the_cursor_then_read_an_issue(tmp_path: Path) -> None:
    page1, page2 = _load("jira_search_page1.json"), _load("jira_search_page2.json")
    issue = _load("jira_issue_detail.json")["raw"]
    pages = {None: page1, page1["nextPageToken"]: page2}
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == f"{API}/search/jql":
            return _json(200, pages[request.url.params.get("nextPageToken")])
        assert request.url.path == f"{API}/issue/SYN-301"
        return _json(200, issue)

    async with _connected(handler, tmp_path) as client:
        first = await client.call_tool("search_issues", {"jql": "project = SYN", "limit": 2})
        cursor = first.structured_content["next_page_token"]
        second = await client.call_tool(
            "search_issues", {"jql": "project = SYN", "limit": 2, "page_token": cursor}
        )
        detail = await client.call_tool("get_issue", {"issue_key": "SYN-301"})

    assert [item["key"] for item in first.structured_content["items"]] == ["SYN-201", "SYN-202"]
    assert cursor == page1["nextPageToken"]
    assert seen[1].url.params["nextPageToken"] == cursor
    assert [item["key"] for item in second.structured_content["items"]] == ["SYN-203"]
    assert second.structured_content["next_page_token"] is None
    assert detail.structured_content["key"] == "SYN-301"
    assert detail.structured_content["fields"]["summary"] == issue["fields"]["summary"]


def _comment_backend(comments: list[dict[str, Any]]) -> tuple[Handler, list[httpx2.Request]]:
    """Answer like Jira: honor `orderBy`, `startAt` and `maxResults`."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        params = request.url.params
        ordered = sorted(
            comments, key=lambda c: c["created"], reverse=params.get("orderBy") == "-created"
        )
        start, size = int(params.get("startAt", 0)), int(params.get("maxResults", 50))
        window = ordered[start : start + size]
        return _json(
            200,
            {"startAt": start, "maxResults": size, "total": len(comments), "comments": window},
        )

    return handler, seen


async def test_comments_keep_their_order_since_and_limit_end_to_end(tmp_path: Path) -> None:
    comments = _load("jira_comments_page.json")["comments"]
    handler, seen = _comment_backend(comments)

    async with _connected(handler, tmp_path) as client:
        result = await client.call_tool(
            "get_comments",
            {"issue_key": "SYN-1", "order": "asc", "since": "2025-08-27T00:00:00Z", "limit": 1},
        )

    # Three comments on Jira's side; `since` keeps the two from Aug 27 on,
    # `limit` returns the oldest of them, and `total` counts the filtered set.
    body = result.structured_content
    assert not result.is_error
    assert body["total"] == 2
    assert [c["created"] for c in body["items"]] == ["2025-08-27T13:55:40Z"]
    assert all(request.url.path == f"{API}/issue/SYN-1/comment" for request in seen)


async def test_transition_by_status_name_posts_one_transition_with_its_comment(
    tmp_path: Path,
) -> None:
    handler, seen = _routes(
        {
            ("GET", f"{API}/issue/SYN-1/transitions"): _json(
                200, _load("jira_issue_transitions.json")
            ),
            ("POST", f"{API}/issue/SYN-1/transitions"): httpx2.Response(204),
        }
    )

    async with _connected(handler, tmp_path) as client:
        result = await client.call_tool(
            "transition_issue", {"issue_key": "SYN-1", "to": "done", "comment": "Shipped."}
        )

    assert not result.is_error
    assert [request.method for request in seen] == ["GET", "POST"]
    posted = json.loads(seen[1].content)
    assert posted["transition"] == {"id": "31"}
    assert posted["update"]["comment"][0]["add"]["body"]["type"] == "doc"
    assert result.structured_content["key"] == "SYN-1"
    assert result.structured_content["transition"]["id"] == "31"


async def test_update_issue_resolves_me_and_sends_a_single_put(tmp_path: Path) -> None:
    handler, seen = _routes(
        {
            ("GET", f"{API}/myself"): _json(200, _load("jira_myself.json")),
            ("PUT", f"{API}/issue/SYN-1"): httpx2.Response(204),
        }
    )

    async with _connected(handler, tmp_path) as client:
        result = await client.call_tool(
            "update_issue", {"issue_key": "SYN-1", "fields": {"assignee": "me", "labels": ["a"]}}
        )

    assert not result.is_error
    put = next(request for request in seen if request.method == "PUT")
    fields = json.loads(put.content)["fields"]
    assert fields["assignee"] == {"accountId": "5b10a2844c20165700ede21g"}
    assert fields["labels"] == ["a"]
    assert result.structured_content["updated_fields"] == ["assignee", "labels"]


async def test_a_jira_rejection_reaches_the_agent_without_the_tenant_or_credentials(
    tmp_path: Path,
) -> None:
    handler, _ = _routes(
        {
            ("PUT", f"{API}/issue/SYN-1"): _json(
                400, _load("jira_write_error_bodies.json")["unknown_field"]
            )
        }
    )

    async with _connected(handler, tmp_path) as client:
        result = await client.call_tool(
            "update_issue", {"issue_key": "SYN-1", "fields": {"nosuchfield": "x"}}
        )

    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert result.is_error
    assert "nosuchfield" in text
    assert "synthetic-tenant" not in text
    assert "super-secret-token" not in text
