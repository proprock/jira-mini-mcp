"""Tests for jira_mini_mcp.server: MCP tool wiring for all six Jira tools.

These tests never touch HTTP -- Phases 4-7 already cover JiraClient/Jira
Cloud behavior against mocked HTTP. Here a FakeJiraClient stands in for
JiraClient so the tests isolate the adapter layer: argument defaults/
pass-through, response shaping into the exact PROJECT-CONTRACTS.md JSON,
and translation of JiraMiniError/JiraIncompleteResponseError into MCP tool
errors. Discovery, lifespan, and one stdio subprocess smoke test use the
MCP v2 in-process client.
"""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from jira_mini_mcp import errors
from jira_mini_mcp.auth import ConfigError
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
    User,
)
from jira_mini_mcp.server import AppContext, create_server

pytestmark = pytest.mark.anyio

TOOL_NAMES = (
    "search_issues",
    "get_issue",
    "get_comments",
    "get_attachments",
    "download_attachment",
    "get_changelog",
)


@dataclass
class _Call:
    name: str
    kwargs: dict[str, Any]


@dataclass
class FakeJiraClient:
    """Stands in for JiraClient. Each `*_result` is returned, or raised if
    it is a BaseException, and every call is recorded for assertion."""

    calls: list[_Call] = field(default_factory=list)
    search_issues_result: Any = field(
        default_factory=lambda: SearchPage(items=[], next_page_token=None)
    )
    get_issue_result: Any = field(default_factory=lambda: IssueDetail(key="X-1", fields={}))
    get_comments_result: Any = field(default_factory=lambda: Page(start_at=0, total=0, items=[]))
    get_attachments_result: Any = field(default_factory=list)
    download_attachment_result: Any = field(
        default_factory=lambda: DownloadResult(
            attachment_id="80001",
            filename="diagnostics.log",
            mime_type="text/plain",
            size=12,
            local_path="/cache/80001/diagnostics.log",
        )
    )
    get_changelog_result: Any = field(default_factory=lambda: Page(start_at=0, total=0, items=[]))

    def _resolve(self, name: str, kwargs: dict[str, Any], result: Any) -> Any:
        self.calls.append(_Call(name=name, kwargs=kwargs))
        if isinstance(result, BaseException):
            raise result
        return result

    async def search_issues(
        self,
        jql: str,
        page_token: str | None = None,
        limit: int = 20,
        fields: list[str] | None = None,
    ) -> SearchPage[IssueSummary]:
        return self._resolve(
            "search_issues",
            {"jql": jql, "page_token": page_token, "limit": limit, "fields": fields},
            self.search_issues_result,
        )

    async def get_issue(self, issue_key: str, fields: list[str] | None = None) -> IssueDetail:
        return self._resolve(
            "get_issue", {"issue_key": issue_key, "fields": fields}, self.get_issue_result
        )

    async def get_comments(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
        since: str | None = None,
    ) -> Page[Comment]:
        return self._resolve(
            "get_comments",
            {
                "issue_key": issue_key,
                "start_at": start_at,
                "limit": limit,
                "order": order,
                "since": since,
            },
            self.get_comments_result,
        )

    async def get_attachments(self, issue_key: str) -> list[Attachment]:
        return self._resolve(
            "get_attachments", {"issue_key": issue_key}, self.get_attachments_result
        )

    async def download_attachment(self, attachment_id: str) -> DownloadResult:
        return self._resolve(
            "download_attachment",
            {"attachment_id": attachment_id},
            self.download_attachment_result,
        )

    async def get_changelog(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
    ) -> Page[ChangelogEntry]:
        return self._resolve(
            "get_changelog",
            {"issue_key": issue_key, "start_at": start_at, "limit": limit, "order": order},
            self.get_changelog_result,
        )


def _server_with(fake: FakeJiraClient) -> MCPServer[AppContext]:
    @asynccontextmanager
    async def fake_lifespan(_server: MCPServer[AppContext]):
        # FakeJiraClient duck-types JiraClient's public surface; cast() tells
        # the type checker that on our behalf.
        yield AppContext(jira_client=cast(JiraClient, fake))

    return create_server(lifespan=fake_lifespan)


def _text_of(result: Any) -> str:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _content_json(result: Any) -> Any:
    return json.loads(_text_of(result))


def _description(tool: Any) -> str:
    assert tool.description is not None
    return tool.description


class TestToolDiscovery:
    async def test_discovers_exactly_six_read_only_idempotent_tools(self) -> None:
        async with Client(_server_with(FakeJiraClient())) as client:
            tools = (await client.list_tools()).tools

        assert [tool.name for tool in tools] == list(TOOL_NAMES)
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.idempotent_hint is True
            assert tool.description
            assert tool.output_schema is not None

    async def test_search_issues_schema_has_positive_limit_default_and_replaceable_fields(
        self,
    ) -> None:
        async with Client(_server_with(FakeJiraClient())) as client:
            tools = (await client.list_tools()).tools
        tool = next(t for t in tools if t.name == "search_issues")

        assert tool.input_schema["required"] == ["jql"]
        assert tool.input_schema["properties"]["limit"]["default"] == 20
        assert tool.input_schema["properties"]["page_token"]["default"] is None
        description = _description(tool)
        assert "1..100" in description
        assert "summary" in description and "project" in description

    async def test_get_issue_schema_documents_default_fields(self) -> None:
        async with Client(_server_with(FakeJiraClient())) as client:
            tools = (await client.list_tools()).tools
        tool = next(t for t in tools if t.name == "get_issue")

        assert tool.input_schema["required"] == ["issue_key"]
        description = _description(tool)
        assert "subtasks" in description
        assert "comments" in description  # documents the exclusion

    async def test_get_comments_and_get_changelog_document_pagination_and_limit_zero(
        self,
    ) -> None:
        async with Client(_server_with(FakeJiraClient())) as client:
            tools = (await client.list_tools()).tools
        by_name = {t.name: t for t in tools}

        for name in ("get_comments", "get_changelog"):
            tool = by_name[name]
            assert tool.input_schema["properties"]["order"]["default"] == "desc"
            assert tool.input_schema["properties"]["start_at"]["default"] == 0
            description = _description(tool)
            assert "limit=0" in description
            assert "newest" in description

        assert "since" in by_name["get_comments"].input_schema["properties"]
        assert "since" not in by_name["get_changelog"].input_schema["properties"]

    async def test_download_attachment_has_no_destination_argument(self) -> None:
        async with Client(_server_with(FakeJiraClient())) as client:
            tools = (await client.list_tools()).tools
        tool = next(t for t in tools if t.name == "download_attachment")

        assert set(tool.input_schema["properties"]) == {"attachment_id"}


class TestSearchIssues:
    async def test_defaults_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("search_issues", {"jql": "project = ABC"})

        assert fake.calls == [
            _Call(
                "search_issues",
                {"jql": "project = ABC", "page_token": None, "limit": 20, "fields": None},
            )
        ]

    async def test_explicit_arguments_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool(
                "search_issues",
                {"jql": "project = ABC", "page_token": "cursor-1", "limit": 5, "fields": []},
            )

        assert fake.calls[0].kwargs == {
            "jql": "project = ABC",
            "page_token": "cursor-1",
            "limit": 5,
            "fields": [],
        }

    async def test_response_shapes_items_and_next_page_token(self) -> None:
        fake = FakeJiraClient()
        fake.search_issues_result = SearchPage(
            items=[
                IssueSummary(
                    key="ABC-1",
                    fields={
                        "summary": "Fix bug",
                        "assignee": User(account_id="a1", display_name="Alice"),
                    },
                )
            ],
            next_page_token="cursor-2",
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("search_issues", {"jql": "project = ABC"})

        expected = {
            "items": [
                {
                    "key": "ABC-1",
                    "fields": {
                        "summary": "Fix bug",
                        "assignee": {"account_id": "a1", "display_name": "Alice"},
                    },
                }
            ],
            "next_page_token": "cursor-2",
        }
        assert not result.is_error
        assert result.structured_content == expected
        assert _content_json(result) == expected

    async def test_null_next_page_token_is_kept_not_omitted(self) -> None:
        fake = FakeJiraClient()
        fake.search_issues_result = SearchPage(items=[], next_page_token=None)
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("search_issues", {"jql": "project = ABC"})

        assert result.structured_content == {"items": [], "next_page_token": None}
        assert _content_json(result) == {"items": [], "next_page_token": None}


class TestGetIssue:
    async def test_defaults_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("get_issue", {"issue_key": "ABC-1"})

        assert fake.calls == [_Call("get_issue", {"issue_key": "ABC-1", "fields": None})]

    async def test_empty_fields_list_passes_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("get_issue", {"issue_key": "ABC-1", "fields": []})

        assert fake.calls[0].kwargs == {"issue_key": "ABC-1", "fields": []}

    async def test_response_shapes_key_and_fields(self) -> None:
        fake = FakeJiraClient()
        fake.get_issue_result = IssueDetail(
            key="ABC-1",
            fields={
                "summary": "Fix bug",
                "reporter": User(account_id="r1", display_name="Rae"),
                "project": {"id": "10000", "key": "ABC", "name": "Alphabet"},
                "subtasks": [
                    {
                        "key": "ABC-2",
                        "status": {"id": "1", "name": "To Do", "category": "new"},
                    }
                ],
            },
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_issue", {"issue_key": "ABC-1"})

        expected = {
            "key": "ABC-1",
            "fields": {
                "summary": "Fix bug",
                "reporter": {"account_id": "r1", "display_name": "Rae"},
                "project": {"id": "10000", "key": "ABC", "name": "Alphabet"},
                "subtasks": [
                    {
                        "key": "ABC-2",
                        "status": {"id": "1", "name": "To Do", "category": "new"},
                    }
                ],
            },
        }
        assert result.structured_content == expected
        assert _content_json(result) == expected


class TestGetComments:
    async def test_defaults_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("get_comments", {"issue_key": "ABC-1"})

        assert fake.calls == [
            _Call(
                "get_comments",
                {
                    "issue_key": "ABC-1",
                    "start_at": 0,
                    "limit": 20,
                    "order": "desc",
                    "since": None,
                },
            )
        ]

    async def test_explicit_arguments_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool(
                "get_comments",
                {
                    "issue_key": "ABC-1",
                    "start_at": 20,
                    "limit": 0,
                    "order": "asc",
                    "since": "2026-01-01T00:00:00Z",
                },
            )

        assert fake.calls[0].kwargs == {
            "issue_key": "ABC-1",
            "start_at": 20,
            "limit": 0,
            "order": "asc",
            "since": "2026-01-01T00:00:00Z",
        }

    async def test_response_shapes_start_at_total_items(self) -> None:
        fake = FakeJiraClient()
        fake.get_comments_result = Page(
            start_at=0,
            total=1,
            items=[
                Comment(
                    id="1",
                    author=User(account_id="a1", display_name="Alice"),
                    body="Looks good",
                    created="2026-01-01T00:00:00Z",
                )
            ],
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_comments", {"issue_key": "ABC-1"})

        expected = {
            "start_at": 0,
            "total": 1,
            "items": [
                {
                    "id": "1",
                    "author": {"account_id": "a1", "display_name": "Alice"},
                    "body": "Looks good",
                    "created": "2026-01-01T00:00:00Z",
                }
            ],
        }
        assert result.structured_content == expected
        assert _content_json(result) == expected

    async def test_absent_updated_is_omitted_present_updated_is_included(self) -> None:
        fake = FakeJiraClient()
        fake.get_comments_result = Page(
            start_at=0,
            total=1,
            items=[
                Comment(
                    id="1",
                    author=User(account_id="a1", display_name="Alice"),
                    body="Edited",
                    created="2026-01-01T00:00:00Z",
                    updated="2026-01-02T00:00:00Z",
                    updated_by=User(account_id="a2", display_name="Bob"),
                )
            ],
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_comments", {"issue_key": "ABC-1"})

        item = result.structured_content["items"][0]
        assert item["updated"] == "2026-01-02T00:00:00Z"
        assert item["updated_by"] == {"account_id": "a2", "display_name": "Bob"}

        fake.get_comments_result = Page(
            start_at=0,
            total=1,
            items=[
                Comment(
                    id="1",
                    author=User(account_id="a1", display_name="Alice"),
                    body="Original",
                    created="2026-01-01T00:00:00Z",
                )
            ],
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_comments", {"issue_key": "ABC-1"})

        item = result.structured_content["items"][0]
        assert "updated" not in item
        assert "updated_by" not in item


class TestGetAttachments:
    async def test_call_passes_issue_key(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("get_attachments", {"issue_key": "ABC-1"})

        assert fake.calls == [_Call("get_attachments", {"issue_key": "ABC-1"})]

    async def test_response_shapes_attachment_metadata_only(self) -> None:
        fake = FakeJiraClient()
        fake.get_attachments_result = [
            Attachment(
                id="80001",
                filename="diagnostics.log",
                mime_type="text/plain",
                size=12,
                author=User(account_id="a1", display_name="Alice"),
                created="2026-01-01T00:00:00Z",
            )
        ]
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_attachments", {"issue_key": "ABC-1"})

        expected = {
            "items": [
                {
                    "id": "80001",
                    "filename": "diagnostics.log",
                    "mime_type": "text/plain",
                    "size": 12,
                    "author": {"account_id": "a1", "display_name": "Alice"},
                    "created": "2026-01-01T00:00:00Z",
                }
            ]
        }
        assert result.structured_content == expected
        assert _content_json(result) == expected


class TestDownloadAttachment:
    async def test_call_passes_attachment_id(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("download_attachment", {"attachment_id": "80001"})

        assert fake.calls == [_Call("download_attachment", {"attachment_id": "80001"})]

    async def test_response_shapes_result_fields(self) -> None:
        fake = FakeJiraClient()
        fake.download_attachment_result = DownloadResult(
            attachment_id="80001",
            filename="diagnostics.log",
            mime_type="text/plain",
            size=12,
            local_path="/cache/80001/diagnostics.log",
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("download_attachment", {"attachment_id": "80001"})

        expected = {
            "attachment_id": "80001",
            "filename": "diagnostics.log",
            "mime_type": "text/plain",
            "size": 12,
            "local_path": "/cache/80001/diagnostics.log",
        }
        assert result.structured_content == expected
        assert _content_json(result) == expected


class TestGetChangelog:
    async def test_defaults_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool("get_changelog", {"issue_key": "ABC-1"})

        assert fake.calls == [
            _Call(
                "get_changelog",
                {"issue_key": "ABC-1", "start_at": 0, "limit": 20, "order": "desc"},
            )
        ]

    async def test_explicit_arguments_pass_through_unchanged(self) -> None:
        fake = FakeJiraClient()
        async with Client(_server_with(fake)) as client:
            await client.call_tool(
                "get_changelog",
                {"issue_key": "ABC-1", "start_at": 5, "limit": 0, "order": "asc"},
            )

        assert fake.calls[0].kwargs == {
            "issue_key": "ABC-1",
            "start_at": 5,
            "limit": 0,
            "order": "asc",
        }

    async def test_response_shapes_changes_and_keeps_null_from_and_to(self) -> None:
        fake = FakeJiraClient()
        fake.get_changelog_result = Page(
            start_at=0,
            total=1,
            items=[
                ChangelogEntry(
                    id="10001",
                    author=User(account_id="a1", display_name="Alice"),
                    created="2026-01-01T00:00:00Z",
                    changes=[
                        ChangelogChange(field="status", from_="To Do", to="In Progress"),
                        ChangelogChange(field="assignee", from_=None, to="Bob", field_id=None),
                    ],
                )
            ],
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_changelog", {"issue_key": "ABC-1"})

        expected = {
            "start_at": 0,
            "total": 1,
            "items": [
                {
                    "id": "10001",
                    "author": {"account_id": "a1", "display_name": "Alice"},
                    "created": "2026-01-01T00:00:00Z",
                    "changes": [
                        {"field": "status", "from": "To Do", "to": "In Progress"},
                        {"field": "assignee", "from": None, "to": "Bob"},
                    ],
                }
            ],
        }
        assert result.structured_content == expected
        assert _content_json(result) == expected

    async def test_custom_field_change_keeps_field_id(self) -> None:
        fake = FakeJiraClient()
        fake.get_changelog_result = Page(
            start_at=0,
            total=1,
            items=[
                ChangelogEntry(
                    id="10001",
                    author=User(account_id="a1", display_name="Alice"),
                    created="2026-01-01T00:00:00Z",
                    changes=[
                        ChangelogChange(
                            field="Sprint", from_=None, to="Sprint 12", field_id="customfield_10020"
                        )
                    ],
                )
            ],
        )
        async with Client(_server_with(fake)) as client:
            result = await client.call_tool("get_changelog", {"issue_key": "ABC-1"})

        change = result.structured_content["items"][0]["changes"][0]
        assert change == {
            "field": "Sprint",
            "from": None,
            "to": "Sprint 12",
            "field_id": "customfield_10020",
        }


_GENERIC_ERROR_CASES = [
    ("search_issues", {"jql": "project = ABC"}, "search_issues_result"),
    ("get_issue", {"issue_key": "ABC-1"}, "get_issue_result"),
    ("get_comments", {"issue_key": "ABC-1"}, "get_comments_result"),
    ("get_attachments", {"issue_key": "ABC-1"}, "get_attachments_result"),
    ("download_attachment", {"attachment_id": "80001"}, "download_attachment_result"),
    ("get_changelog", {"issue_key": "ABC-1"}, "get_changelog_result"),
]

_INCOMPLETE_ERROR_CASES = [
    (
        "search_issues",
        {"jql": "project = ABC"},
        "search_issues_result",
        {"items": [{"key": "ABC-1", "fields": {}}], "next_page_token": None},
    ),
    (
        "get_issue",
        {"issue_key": "ABC-1"},
        "get_issue_result",
        {"key": "ABC-1", "fields": {}},
    ),
    (
        "get_comments",
        {"issue_key": "ABC-1"},
        "get_comments_result",
        {"start_at": 0, "total": 0, "items": []},
    ),
    (
        "get_attachments",
        {"issue_key": "ABC-1"},
        "get_attachments_result",
        {"items": []},
    ),
    (
        "get_changelog",
        {"issue_key": "ABC-1"},
        "get_changelog_result",
        {
            "start_at": 0,
            "total": 1,
            "items": [
                {
                    "id": "10001",
                    "author": {"account_id": "a1", "display_name": "Alice"},
                    "created": "2026-01-01T00:00:00Z",
                    "changes": [
                        {"field": "status", "from_": "To Do", "to": "In Progress", "field_id": None}
                    ],
                }
            ],
        },
    ),
]


class TestErrorTranslation:
    @pytest.mark.parametrize("tool_name, arguments, result_attr", _GENERIC_ERROR_CASES)
    async def test_jira_mini_error_becomes_an_actionable_tool_error(
        self, tool_name: str, arguments: dict[str, Any], result_attr: str
    ) -> None:
        fake = FakeJiraClient()
        message = f"Jira resource was not found for operation '{tool_name}'."
        setattr(
            fake,
            result_attr,
            errors.JiraNotFoundError(message, operation=tool_name, status_code=404),
        )

        async with Client(_server_with(fake)) as client:
            result = await client.call_tool(tool_name, arguments)

        assert result.is_error
        text = _text_of(result)
        assert message in text
        assert "https://" not in text
        assert "super-secret-token" not in text

    @pytest.mark.parametrize("tool_name, arguments, result_attr, partial", _INCOMPLETE_ERROR_CASES)
    async def test_incomplete_response_becomes_error_result_with_partial_json(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result_attr: str,
        partial: dict[str, Any],
    ) -> None:
        fake = FakeJiraClient()
        problem = "$.fields.status: expected an object"
        setattr(
            fake,
            result_attr,
            errors.JiraIncompleteResponseError(
                f"Jira returned incomplete data for operation '{tool_name}': {problem}",
                problems=(problem,),
                partial_result=partial,
                operation=tool_name,
            ),
        )

        async with Client(_server_with(fake)) as client:
            result = await client.call_tool(tool_name, arguments)

        assert result.is_error
        text = _text_of(result)
        assert problem in text
        assert "Partial result:" in text

        partial_json_text = text.split("Partial result:", 1)[1].strip()
        expected_partial = json.loads(json.dumps(partial).replace('"from_"', '"from"'))
        assert json.loads(partial_json_text) == expected_partial


class TestLifespan:
    async def test_missing_configuration_stops_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_EMAIL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        server = create_server()
        with pytest.raises(ConfigError):
            async with Client(server):
                pass

    async def test_one_http_client_and_one_cache_dir_created_and_closed_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JIRA_BASE_URL", "https://synthetic-tenant.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "agent@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "super-secret-token")

        created_clients: list[httpx2.AsyncClient] = []
        original_client_init = httpx2.AsyncClient.__init__

        def spy_client_init(self: httpx2.AsyncClient, *args: Any, **kwargs: Any) -> None:
            original_client_init(self, *args, **kwargs)
            created_clients.append(self)

        monkeypatch.setattr(httpx2.AsyncClient, "__init__", spy_client_init)

        cache_dirs: list[Path] = []
        original_jira_client_init = JiraClient.__init__

        def spy_jira_client_init(
            self: JiraClient,
            client: httpx2.AsyncClient,
            auth: Any,
            base_url: str,
            cache_dir: Path,
        ) -> None:
            cache_dirs.append(cache_dir)
            original_jira_client_init(self, client, auth, base_url, cache_dir)

        monkeypatch.setattr(JiraClient, "__init__", spy_jira_client_init)

        rmtree_calls: list[Path] = []
        original_rmtree = shutil.rmtree

        def spy_rmtree(path: Any, *, ignore_errors: bool = False) -> None:
            rmtree_calls.append(Path(path))
            original_rmtree(path, ignore_errors=ignore_errors)

        monkeypatch.setattr(shutil, "rmtree", spy_rmtree)

        server = create_server()
        async with Client(server) as client:
            await client.list_tools()

        assert len(created_clients) == 1
        assert created_clients[0].is_closed
        assert len(cache_dirs) == 1
        assert rmtree_calls == [cache_dirs[0]]
        assert not cache_dirs[0].exists()


class TestStdioSubprocessSmoke:
    async def test_tools_are_discoverable_over_stdio(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "jira_mini_mcp.server"],
            env={
                "JIRA_BASE_URL": "https://synthetic-tenant.atlassian.net",
                "JIRA_EMAIL": "agent@example.com",
                "JIRA_API_TOKEN": "super-secret-token",
            },
        )

        async with Client(params) as client:
            tools = (await client.list_tools()).tools

        assert {tool.name for tool in tools} == set(TOOL_NAMES)
