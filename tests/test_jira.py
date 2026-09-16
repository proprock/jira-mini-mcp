"""Tests for jira_mini_mcp.jira: JiraClient core, search_issues, get_issue,
get_comments, get_attachments, download_attachment, get_changelog.

Fixtures under tests/fixtures/jira_*.json trace to live, read-only requests
against a Jira Cloud test site on 2026-09-16 (GET /rest/api/3/search/jql,
GET /rest/api/3/issue/{key}, GET /rest/api/3/issue/{key}/comment,
GET /rest/api/3/issue/{key}?fields=attachment,
GET /rest/api/3/attachment/{id}, and GET /rest/api/3/issue/{key}/changelog),
with every tenant/account/content value replaced by synthetic data. See each
fixture's `_provenance` note.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jira_mini_mcp import errors, jira
from jira_mini_mcp.auth import BasicTokenAuth
from jira_mini_mcp.jira import JiraClient
from jira_mini_mcp.models import (
    ChangelogChange,
    IssueDetail,
    IssueSummary,
    Page,
    SearchPage,
    User,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://synthetic-tenant.atlassian.net"

pytestmark = pytest.mark.anyio


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _json_response(status_code: int, body: Any) -> httpx2.Response:
    return httpx2.Response(status_code, json=body)


Handler = Callable[[httpx2.Request], httpx2.Response]


def _make_client(handler: Handler, cache_dir: Path = Path(".")) -> JiraClient:
    transport = httpx2.MockTransport(handler)
    http_client = httpx2.AsyncClient(transport=transport)
    auth = BasicTokenAuth("agent@example.com", "super-secret-token")
    return JiraClient(http_client, auth, BASE_URL, cache_dir)


def _recording_handler(response: httpx2.Response) -> tuple[Handler, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return response

    return handler, seen


class TestPrivateRequestHelper:
    async def test_attaches_basic_auth_header(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN")

        auth_header = seen[0].headers["Authorization"]
        assert auth_header.startswith("Basic ")
        decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode()
        assert decoded == "agent@example.com:super-secret-token"

    async def test_network_failure_raises_network_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused")

        client = _make_client(handler)
        with pytest.raises(errors.JiraNetworkError) as exc_info:
            await client.search_issues("project = SYN")
        assert exc_info.value.operation == "search_issues"

    async def test_timeout_raises_timeout_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out")

        client = _make_client(handler)
        with pytest.raises(errors.JiraTimeoutError) as exc_info:
            await client.search_issues("project = SYN")
        assert exc_info.value.operation == "search_issues"

    async def test_non_json_success_body_raises_server_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, text="<html>not json</html>")

        client = _make_client(handler)
        with pytest.raises(errors.JiraServerError):
            await client.search_issues("project = SYN")


class TestSearchIssues:
    async def test_uses_enhanced_v3_search_path(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN")

        assert seen[0].method == "GET"
        assert seen[0].url.path == "/rest/api/3/search/jql"

    async def test_default_limit_and_fields(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN")

        params = seen[0].url.params
        assert params["maxResults"] == "20"
        assert params["fields"] == ",".join(jira.SEARCH_DEFAULT_FIELDS)
        assert "nextPageToken" not in params

    async def test_explicit_fields_replace_default(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", fields=["summary", "status"])

        assert seen[0].url.params["fields"] == "summary,status"

    async def test_empty_fields_list_requests_no_fields(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", fields=[])

        assert seen[0].url.params["fields"] == ""

    @pytest.mark.parametrize("limit", [1, 100])
    async def test_limit_boundaries_accepted(self, limit: int) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", limit=limit)

        assert seen[0].url.params["maxResults"] == str(limit)

    @pytest.mark.parametrize("limit", [0, -1, 101])
    async def test_invalid_limit_raises_actionable_error_without_http_call(
        self, limit: int
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("no HTTP call should be made for an invalid limit")

        client = _make_client(handler)
        with pytest.raises(errors.JiraValidationError) as exc_info:
            await client.search_issues("project = SYN", limit=limit)
        message = str(exc_info.value)
        assert "1..100" in message
        assert "positive" in message

    async def test_page_token_passthrough_unchanged(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", page_token="opaque-cursor-value")

        assert seen[0].url.params["nextPageToken"] == "opaque-cursor-value"

    async def test_sequential_pages_pass_returned_token_to_next_call(self) -> None:
        page1 = _load("jira_search_page1.json")
        page2 = _load("jira_search_page2.json")
        responses = [_json_response(200, page1), _json_response(200, page2)]
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return responses.pop(0)

        client = _make_client(handler)

        first = await client.search_issues("project = SYN")
        assert first.next_page_token == "synthetic-cursor-page-2"
        assert "nextPageToken" not in seen[0].url.params

        second = await client.search_issues("project = SYN", page_token=first.next_page_token)
        assert second.next_page_token is None
        assert seen[1].url.params["nextPageToken"] == "synthetic-cursor-page-2"

    async def test_first_page_items_shape(self) -> None:
        page1 = _load("jira_search_page1.json")
        handler, _ = _recording_handler(_json_response(200, page1))
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert isinstance(result, SearchPage)
        assert len(result.items) == 2
        first_item = result.items[0]
        assert isinstance(first_item, IssueSummary)
        assert first_item.key == "SYN-201"
        assert first_item.fields["summary"] == "Search index falls behind after bulk import"
        assert first_item.fields["assignee"].account_id == "syn-acc-101"
        assert first_item.fields["issuetype"] == {"id": "10001", "name": "Bug"}
        assert first_item.fields["status"] == {
            "id": "3",
            "name": "In Progress",
            "category": "indeterminate",
        }
        assert first_item.fields["priority"] == {"id": "2", "name": "High"}
        assert first_item.fields["project"] == {
            "id": "10000",
            "key": "SYN",
            "name": "Synthetic Project",
        }
        assert "self" not in first_item.fields
        second_item = result.items[1]
        assert "assignee" not in second_item.fields  # null assignee is omitted, not null

    async def test_last_page_has_no_next_token(self) -> None:
        page2 = _load("jira_search_page2.json")
        handler, _ = _recording_handler(_json_response(200, page2))
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert result.next_page_token is None

    async def test_empty_result(self) -> None:
        empty = _load("jira_search_empty.json")
        handler, _ = _recording_handler(_json_response(200, empty))
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert result.items == []
        assert result.next_page_token is None

    async def test_malformed_payload_raises_instead_of_empty_result(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"issues": "not-a-list"}))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.search_issues("project = SYN")

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"issues": [], "nextPageToken": 123},
        ],
    )
    async def test_malformed_search_envelope_raises_server_error(self, body: Any) -> None:
        handler, _ = _recording_handler(_json_response(200, body))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.search_issues("project = SYN")

    @pytest.mark.parametrize(
        ("raw_issue", "problem"),
        [
            ("not-an-object", "$.issues[0]: expected an object"),
            (
                {"key": "SYN-1", "fields": "not-an-object"},
                "$.issues[0].fields: expected an object",
            ),
        ],
    )
    async def test_malformed_search_item_keeps_only_clean_partial_data(
        self, raw_issue: Any, problem: str
    ) -> None:
        handler, _ = _recording_handler(_json_response(200, {"issues": [raw_issue]}))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.search_issues("project = SYN")

        assert exc_info.value.problems == (problem,)

    async def test_null_search_fields_are_treated_as_absent(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"issues": [{"key": "SYN-1", "fields": None}]})
        )
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert result.items == [IssueSummary(key="SYN-1", fields={})]

    async def test_malformed_resource_raises_with_sanitized_partial_search_page(self) -> None:
        raw = {
            "issues": [
                {"key": "SYN-1", "fields": {"summary": "Valid"}},
                {
                    "key": "SYN-2",
                    "fields": {
                        "summary": "Partially valid",
                        "priority": {
                            "id": 2,
                            "name": "High",
                            "self": "https://synthetic-tenant.atlassian.net/priority/2",
                        },
                    },
                },
            ],
            "nextPageToken": "synthetic-next",
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.search_issues("project = SYN")

        assert exc_info.value.problems == (
            "$.issues[1].fields.priority.id: expected a non-empty string",
        )
        assert exc_info.value.partial_result == {
            "items": [
                {"key": "SYN-1", "fields": {"summary": "Valid"}},
                {"key": "SYN-2", "fields": {"summary": "Partially valid"}},
            ],
            "next_page_token": "synthetic-next",
        }
        assert "synthetic-tenant" not in repr(exc_info.value.partial_result)

    async def test_missing_issue_key_is_reported_without_losing_valid_items(self) -> None:
        raw = {
            "issues": [
                {"key": "SYN-1", "fields": {"summary": "Valid"}},
                {"id": "10002", "fields": {"summary": "No key"}},
            ]
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.search_issues("project = SYN")

        assert exc_info.value.problems == ("$.issues[1].key: expected a non-empty string",)
        assert exc_info.value.partial_result == {
            "items": [{"key": "SYN-1", "fields": {"summary": "Valid"}}],
            "next_page_token": None,
        }

    async def test_response_has_only_items_and_next_page_token_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(SearchPage)}
        assert field_names == {"items", "next_page_token"}

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
            (503, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.search_issues("project = SYN")


class TestGetIssue:
    async def test_uses_v3_issue_path(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1")

        assert seen[0].method == "GET"
        assert seen[0].url.path == "/rest/api/3/issue/SYN-1"

    async def test_default_fields(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1")

        assert seen[0].url.params["fields"] == ",".join(jira.ISSUE_DEFAULT_FIELDS)

    async def test_explicit_fields_replace_default(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1", fields=["summary"])

        assert seen[0].url.params["fields"] == "summary"

    async def test_empty_fields_list_uses_sentinel_not_upstream_default(self) -> None:
        # Jira Cloud v3's single-issue GET treats fields="" and fields=-* as
        # unspecified and returns a tenant- and permission-dependent full set.
        # The non-field issue id is a stable sentinel for an empty fields object.
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1", fields=[])

        sent = seen[0].url.params["fields"]
        assert sent != ""
        assert sent not in jira.ISSUE_DEFAULT_FIELDS

    async def test_empty_fields_list_returns_empty_fields_object(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        result = await client.get_issue("SYN-1", fields=[])

        assert result == IssueDetail(key="SYN-1", fields={})

    async def test_full_response_shape_and_normalization(self) -> None:
        fixture = _load("jira_issue_detail.json")
        handler, _ = _recording_handler(_json_response(200, fixture["raw"]))
        client = _make_client(handler)

        result = await client.get_issue("SYN-301")

        assert result.key == "SYN-301"
        assert result.fields["summary"] == fixture["expected_normalized_fields"]["summary"]
        assert result.fields["updated"] == fixture["expected_normalized_fields"]["updated"]
        assert result.fields["assignee"].account_id == "syn-acc-201"
        assert result.fields["status"] == {
            "id": "3",
            "name": "In Progress",
            "category": "indeterminate",
        }
        assert "resolutiondate" not in result.fields  # null is omitted, not emitted as null

    async def test_malformed_resource_raises_with_sanitized_partial_issue(self) -> None:
        raw = {
            "key": "SYN-1",
            "fields": {
                "summary": "Useful",
                "project": {
                    "id": "10000",
                    "name": "Missing key",
                    "self": "https://synthetic-tenant.atlassian.net/project/10000",
                },
            },
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_issue("SYN-1")

        assert exc_info.value.problems == ("$.fields.project.key: expected a non-empty string",)
        assert exc_info.value.partial_result == {
            "key": "SYN-1",
            "fields": {"summary": "Useful"},
        }
        assert "synthetic-tenant" not in repr(exc_info.value.partial_result)

    async def test_omits_comments_attachments_changelog_even_if_present_in_raw_envelope(
        self,
    ) -> None:
        raw = {
            "key": "SYN-1",
            "fields": {"summary": "Has embedded history"},
            "changelog": {"histories": [{"id": "1"}]},
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        result = await client.get_issue("SYN-1")

        assert result == IssueDetail(key="SYN-1", fields={"summary": "Has embedded history"})

    async def test_not_found_raises_with_issue_key(self) -> None:
        fixture = _load("jira_issue_not_found.json")
        handler, _ = _recording_handler(_json_response(404, fixture))
        client = _make_client(handler)

        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            await client.get_issue("SYN-404")

        assert exc_info.value.issue_key == "SYN-404"
        assert exc_info.value.status_code == 404

    async def test_malformed_payload_missing_key_raises_instead_of_crashing(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"fields": {"summary": "no key"}}))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_issue("SYN-1")

    async def test_non_object_issue_envelope_raises_server_error(self) -> None:
        handler, _ = _recording_handler(_json_response(200, []))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_issue("SYN-1")

    async def test_null_issue_fields_are_treated_as_absent(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": None}))
        client = _make_client(handler)

        assert await client.get_issue("SYN-1") == IssueDetail(key="SYN-1", fields={})

    async def test_non_object_issue_fields_raise_with_clean_partial_result(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"key": "SYN-1", "fields": "not-an-object"})
        )
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_issue("SYN-1")

        assert exc_info.value.problems == ("$.fields: expected an object",)
        assert exc_info.value.partial_result == {"key": "SYN-1", "fields": {}}

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.get_issue("SYN-1")


def _synthetic_comment(index: int, created: str, comment_id: str | None = None) -> dict[str, Any]:
    return {
        "id": comment_id or str(1000 + index),
        "author": {"accountId": f"syn-acc-{index}", "displayName": f"User {index}"},
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": f"Comment {index}"}]}
            ],
        },
        "created": created,
        "updated": created,
    }


def _comments_backend_handler(
    raw_comments: list[dict[str, Any]],
) -> tuple[Handler, list[httpx2.Request]]:
    """A stateful fake mirroring the live-observed comment endpoint: it honors
    startAt/maxResults/orderBy over the full synthetic collection, sorted by
    (created, id), so tests can exercise real multi-page pagination.
    """
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        params = request.url.params
        start_at = int(params["startAt"])
        max_results = int(params["maxResults"])
        order_by = params["orderBy"]
        ordered = sorted(
            raw_comments,
            key=lambda c: (c["created"], c["id"]),
            reverse=(order_by == "-created"),
        )
        page = ordered[start_at : start_at + max_results]
        return _json_response(
            200,
            {
                "startAt": start_at,
                "maxResults": max_results,
                "total": len(raw_comments),
                "comments": page,
            },
        )

    return handler, seen


class TestGetComments:
    async def test_uses_v3_comment_path(self) -> None:
        handler, seen = _recording_handler(
            _json_response(200, {"startAt": 0, "maxResults": 20, "total": 0, "comments": []})
        )
        client = _make_client(handler)

        await client.get_comments("SYN-1")

        assert seen[0].method == "GET"
        assert seen[0].url.path == "/rest/api/3/issue/SYN-1/comment"

    async def test_default_requests_newest_first(self) -> None:
        handler, seen = _recording_handler(
            _json_response(200, {"startAt": 0, "maxResults": 20, "total": 0, "comments": []})
        )
        client = _make_client(handler)

        await client.get_comments("SYN-1")

        params = seen[0].url.params
        assert params["orderBy"] == "-created"
        assert params["startAt"] == "0"
        assert params["maxResults"] == "20"

    async def test_full_response_shape_and_normalization(self) -> None:
        fixture = _load("jira_comments_page.json")
        handler, _ = _recording_handler(_json_response(200, fixture))
        client = _make_client(handler)

        result = await client.get_comments("SYN-301", limit=0, order="asc")

        assert isinstance(result, Page)
        assert result.start_at == 0
        assert result.total == 3
        assert [c.id for c in result.items] == ["71001", "71002", "71003"]

        unedited = result.items[0]
        assert unedited.author == User(account_id="syn-acc-301", display_name="Jordan Lee")
        assert unedited.body == "First triage pass looks reasonable."
        assert unedited.created == "2025-08-26T13:55:40Z"
        assert unedited.updated is None  # updated == created is omitted
        assert unedited.updated_by is None

        edited_by_other = result.items[1]
        assert edited_by_other.updated == "2025-08-27T15:20:05Z"
        assert edited_by_other.updated_by == User(
            account_id="syn-acc-303", display_name="Morgan Diaz"
        )

        edited_by_self = result.items[2]
        assert edited_by_self.updated == "2025-08-28T13:58:00Z"
        assert edited_by_self.updated_by is None  # same author as `author`: omitted

    async def test_no_self_or_raw_adf_on_any_item(self) -> None:
        fixture = _load("jira_comments_page.json")
        handler, _ = _recording_handler(_json_response(200, fixture))
        client = _make_client(handler)

        result = await client.get_comments("SYN-301")

        for comment in result.items:
            assert not hasattr(comment, "self")
            assert isinstance(comment.body, str)

    async def test_ascending_and_descending_orders(self) -> None:
        raw = [_synthetic_comment(i, f"2025-01-0{i}T00:00:00Z") for i in range(1, 6)]

        desc_handler, _ = _comments_backend_handler(raw)
        desc_result = await _make_client(desc_handler).get_comments("SYN-1", order="desc")
        assert [c.id for c in desc_result.items] == ["1005", "1004", "1003", "1002", "1001"]

        asc_handler, _ = _comments_backend_handler(raw)
        asc_result = await _make_client(asc_handler).get_comments("SYN-1", order="asc")
        assert [c.id for c in asc_result.items] == ["1001", "1002", "1003", "1004", "1005"]

    async def test_nonzero_start_at_offsets_the_logical_collection(self) -> None:
        raw = [_synthetic_comment(i, f"2025-01-0{i}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments("SYN-1", start_at=2, limit=2, order="asc")

        assert result.start_at == 2
        assert result.total == 5
        assert [c.id for c in result.items] == ["1003", "1004"]

    async def test_equal_timestamps_ordered_by_id(self) -> None:
        raw = [
            _synthetic_comment(1, "2025-01-01T00:00:00Z", comment_id="20"),
            _synthetic_comment(2, "2025-01-01T00:00:00Z", comment_id="10"),
            _synthetic_comment(3, "2025-01-01T00:00:00Z", comment_id="30"),
        ]
        handler, _ = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments("SYN-1", order="asc")

        assert [c.id for c in result.items] == ["10", "20", "30"]

    async def test_limit_zero_returns_every_remaining_item(self) -> None:
        raw = [_synthetic_comment(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments("SYN-1", start_at=1, limit=0, order="asc")

        assert result.total == 5
        assert [c.id for c in result.items] == ["1002", "1003", "1004", "1005"]

    async def test_reachable_beyond_100_comments_over_successive_calls(self) -> None:
        raw = [
            _synthetic_comment(i, f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z")
            for i in range(150)
        ]
        handler, seen = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments("SYN-1", start_at=0, limit=0, order="asc")

        assert result.total == 150
        assert len(result.items) == 150
        assert len(seen) >= 2  # jira.COMMENTS_PAGE_SIZE (100) forces a second call

    async def test_multi_page_fetch_with_shrunk_page_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira, "COMMENTS_PAGE_SIZE", 2)
        raw = [_synthetic_comment(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 6)]
        handler, seen = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments("SYN-1", limit=0, order="asc")

        assert [c.id for c in result.items] == ["1001", "1002", "1003", "1004", "1005"]
        assert len(seen) == 3  # 2 + 2 + 1

    async def test_since_filters_before_ordering_and_slicing(self) -> None:
        raw = [_synthetic_comment(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments(
            "SYN-1", since="2025-01-03T00:00:00Z", order="asc"
        )

        assert result.total == 3  # exact filtered total: comments 3, 4, 5
        assert [c.id for c in result.items] == ["1003", "1004", "1005"]

    async def test_since_early_stop_uses_fewer_calls_than_full_history_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira, "COMMENTS_PAGE_SIZE", 2)
        raw = [_synthetic_comment(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 11)]
        handler, seen = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments("SYN-1", since="2025-01-09T00:00:00Z")

        assert [c.id for c in result.items] == ["1010", "1009"]
        full_scan_calls = 5  # ceil(10 / page_size=2)
        assert len(seen) < full_scan_calls

    async def test_empty_comments(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"startAt": 0, "maxResults": 20, "total": 0, "comments": []})
        )
        client = _make_client(handler)

        result = await client.get_comments("SYN-1")

        assert result == Page(start_at=0, total=0, items=[])

    async def test_start_beyond_total_returns_empty_items_with_one_call(self) -> None:
        handler, seen = _recording_handler(
            _json_response(200, {"startAt": 50, "maxResults": 20, "total": 5, "comments": []})
        )
        client = _make_client(handler)

        result = await client.get_comments("SYN-1", start_at=50)

        assert result == Page(start_at=50, total=5, items=[])
        assert len(seen) == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"startAt": 0, "maxResults": 20, "comments": []},
            {"startAt": 0, "maxResults": 20, "total": "5", "comments": []},
            {"startAt": 0, "maxResults": 20, "total": 5, "comments": "not-a-list"},
            [],
        ],
    )
    async def test_malformed_envelope_raises_server_error(self, body: Any) -> None:
        handler, _ = _recording_handler(_json_response(200, body))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_comments("SYN-1")

    async def test_sort_key_drops_missing_or_malformed_created_without_crashing(self) -> None:
        raw = {
            "startAt": 0,
            "maxResults": 20,
            "total": 3,
            "comments": [
                _synthetic_comment(1, "2025-01-01T00:00:00Z"),
                {**_synthetic_comment(2, ""), "created": None},
                {**_synthetic_comment(3, ""), "created": "not-a-timestamp"},
            ],
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_comments("SYN-1", order="asc")

        assert len(exc_info.value.problems) == 2
        assert [c["id"] for c in exc_info.value.partial_result["items"]] == ["1001"]

    async def test_since_scan_skips_comment_with_malformed_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira, "COMMENTS_PAGE_SIZE", 2)
        raw = [
            _synthetic_comment(1, "2025-01-01T00:00:00Z"),
            {**_synthetic_comment(2, ""), "created": "not-a-timestamp"},
            _synthetic_comment(3, "2025-01-03T00:00:00Z"),
        ]
        handler, _ = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments(
            "SYN-1", since="2020-01-01T00:00:00Z", order="asc"
        )

        assert [c.id for c in result.items] == ["1001", "1003"]

    async def test_since_scan_skips_comment_with_missing_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira, "COMMENTS_PAGE_SIZE", 2)
        raw = [
            _synthetic_comment(1, "2025-01-01T00:00:00Z"),
            _synthetic_comment(2, ""),  # missing/empty created: falls through, no exception
            _synthetic_comment(3, "2025-01-03T00:00:00Z"),
        ]
        handler, _ = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments(
            "SYN-1", since="2020-01-01T00:00:00Z", order="asc"
        )

        assert [c.id for c in result.items] == ["1001", "1003"]

    async def test_sort_key_treats_non_dict_item_as_unsortable(self) -> None:
        raw = {
            "startAt": 0,
            "maxResults": 20,
            "total": 2,
            "comments": ["not-an-object", _synthetic_comment(1, "2025-01-01T00:00:00Z")],
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_comments("SYN-1", order="asc")

        assert [c["id"] for c in exc_info.value.partial_result["items"]] == ["1001"]

    async def test_since_matches_entire_history_reaches_natural_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira, "COMMENTS_PAGE_SIZE", 2)
        raw = [_synthetic_comment(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 5)]
        handler, seen = _comments_backend_handler(raw)

        result = await _make_client(handler).get_comments(
            "SYN-1", since="2000-01-01T00:00:00Z", order="asc"
        )

        assert [c.id for c in result.items] == ["1001", "1002", "1003", "1004"]
        assert len(seen) == 3  # 2 + 2, then one empty page confirms the natural end

    async def test_malformed_resource_raises_with_sanitized_partial_page(self) -> None:
        raw = {
            "startAt": 0,
            "maxResults": 20,
            "total": 2,
            "comments": [
                _synthetic_comment(1, "2025-01-01T00:00:00Z"),
                {"id": "1002", "author": None, "body": {}, "created": "2025-01-02T00:00:00Z"},
            ],
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_comments("SYN-1", order="asc")

        assert exc_info.value.problems == ("$.items[1].author: expected an object",)
        assert exc_info.value.partial_result["total"] == 2  # Jira's total is unaffected
        assert [c["id"] for c in exc_info.value.partial_result["items"]] == ["1001"]

    async def test_response_has_only_start_at_total_items_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(Page)}
        assert field_names == {"start_at", "total", "items"}

    async def test_not_found_raises_with_issue_key(self) -> None:
        fixture = _load("jira_issue_not_found.json")
        handler, _ = _recording_handler(_json_response(404, fixture))
        client = _make_client(handler)

        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            await client.get_comments("SYN-404")

        assert exc_info.value.issue_key == "SYN-404"
        assert exc_info.value.status_code == 404

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.get_comments("SYN-1")

    @pytest.mark.parametrize(
        ("kwargs", "message_fragment"),
        [
            ({"start_at": -1}, "non-negative"),
            ({"limit": -1}, "non-negative"),
            ({"order": "newest"}, "'asc' or 'desc'"),
            ({"since": "2025-01-01"}, "explicit offset"),
        ],
    )
    async def test_invalid_arguments_raise_without_http_call(
        self, kwargs: dict[str, Any], message_fragment: str
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("no HTTP call should be made for invalid arguments")

        client = _make_client(handler)
        with pytest.raises(errors.JiraValidationError) as exc_info:
            await client.get_comments("SYN-1", **kwargs)
        assert message_fragment in str(exc_info.value)


def _synthetic_changelog_entry(
    index: int, created: str, entry_id: str | None = None
) -> dict[str, Any]:
    return {
        "id": entry_id or str(50000 + index),
        "author": {"accountId": f"syn-acc-{index}", "displayName": f"User {index}"},
        "created": created,
        "items": [
            {
                "field": "status",
                "fieldtype": "jira",
                "fieldId": "status",
                "from": "10000",
                "fromString": "To Do",
                "to": "10001",
                "toString": f"Status {index}",
            }
        ],
    }


def _changelog_backend_handler(
    raw_entries: list[dict[str, Any]],
) -> tuple[Handler, list[httpx2.Request]]:
    """A stateful fake mirroring the live-observed changelog endpoint: always
    oldest-first (no orderBy support), startAt/maxResults index that fixed
    ascending collection directly, and -- observed live 2026-09-16 -- `total`
    reports the real collection size only while startAt does not exceed it;
    once startAt overshoots, Jira echoes back total == startAt instead of the
    real count.
    """
    seen: list[httpx2.Request] = []
    real_total = len(raw_entries)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        params = request.url.params
        start_at = int(params["startAt"])
        max_results = int(params["maxResults"])
        ordered = sorted(raw_entries, key=lambda e: (e["created"], e["id"]))
        page = ordered[start_at : start_at + max_results]
        reported_total = real_total if start_at <= real_total else start_at
        return _json_response(
            200,
            {
                "startAt": start_at,
                "maxResults": max_results,
                "total": reported_total,
                "isLast": start_at + len(page) >= real_total,
                "values": page,
            },
        )

    return handler, seen


class TestGetChangelog:
    async def test_uses_v3_changelog_path(self) -> None:
        handler, seen = _recording_handler(
            _json_response(
                200, {"startAt": 0, "maxResults": 1, "total": 0, "isLast": True, "values": []}
            )
        )
        client = _make_client(handler)

        await client.get_changelog("SYN-1")

        assert seen[0].method == "GET"
        assert seen[0].url.path == "/rest/api/3/issue/SYN-1/changelog"

    async def test_no_orderby_param_is_sent(self) -> None:
        handler, seen = _recording_handler(
            _json_response(
                200, {"startAt": 0, "maxResults": 1, "total": 0, "isLast": True, "values": []}
            )
        )
        client = _make_client(handler)

        await client.get_changelog("SYN-1")

        assert "orderBy" not in seen[0].url.params

    async def test_full_response_shape_and_normalization(self) -> None:
        fixture = _load("jira_changelog_page.json")
        handler, _ = _recording_handler(_json_response(200, fixture))
        client = _make_client(handler)

        result = await client.get_changelog("SYN-301", limit=0, order="asc")

        assert isinstance(result, Page)
        assert result.start_at == 0
        assert result.total == 3
        assert [e.id for e in result.items] == ["50101", "50102", "50103"]

        first = result.items[0]
        assert first.author == User(account_id="syn-acc-301", display_name="Jordan Lee")
        assert first.created == "2025-08-26T13:55:40Z"
        assert first.changes == [
            ChangelogChange(field="status", from_="To Do", to="In Progress", field_id=None)
        ]

        second = result.items[1]
        assert second.changes == [
            ChangelogChange(field="Attachment", from_=None, to="diagnostics.log", field_id=None),
            ChangelogChange(
                field="Sprint", from_="", to="Sprint 12 (8/27 - 9/10)", field_id="customfield_10020"
            ),
        ]

        third = result.items[2]
        assert third.changes == [
            ChangelogChange(
                field="Link", from_=None, to="This issue relates to SYN-9", field_id=None
            )
        ]

    async def test_default_order_is_newest_first(self) -> None:
        fixture = _load("jira_changelog_page.json")
        handler, _ = _recording_handler(_json_response(200, fixture))
        client = _make_client(handler)

        result = await client.get_changelog("SYN-301")

        assert [e.id for e in result.items] == ["50103", "50102", "50101"]

    async def test_ascending_and_descending_orders(self) -> None:
        raw = [_synthetic_changelog_entry(i, f"2025-01-0{i}T00:00:00Z") for i in range(1, 6)]

        desc_handler, _ = _changelog_backend_handler(raw)
        desc_result = await _make_client(desc_handler).get_changelog("SYN-1", order="desc")
        assert [e.id for e in desc_result.items] == ["50005", "50004", "50003", "50002", "50001"]

        asc_handler, _ = _changelog_backend_handler(raw)
        asc_result = await _make_client(asc_handler).get_changelog("SYN-1", order="asc")
        assert [e.id for e in asc_result.items] == ["50001", "50002", "50003", "50004", "50005"]

    async def test_nonzero_start_at_offsets_the_logical_collection(self) -> None:
        raw = [_synthetic_changelog_entry(i, f"2025-01-0{i}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=2, limit=2, order="asc"
        )

        assert result.start_at == 2
        assert result.total == 5
        assert [e.id for e in result.items] == ["50003", "50004"]

    async def test_nonzero_start_at_offsets_descending_collection(self) -> None:
        raw = [_synthetic_changelog_entry(i, f"2025-01-0{i}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=1, limit=2, order="desc"
        )

        assert result.start_at == 1
        assert result.total == 5
        assert [e.id for e in result.items] == ["50004", "50003"]

    async def test_equal_timestamps_ordered_by_id(self) -> None:
        raw = [
            _synthetic_changelog_entry(1, "2025-01-01T00:00:00Z", entry_id="20"),
            _synthetic_changelog_entry(2, "2025-01-01T00:00:00Z", entry_id="10"),
            _synthetic_changelog_entry(3, "2025-01-01T00:00:00Z", entry_id="30"),
        ]
        handler, _ = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog("SYN-1", order="asc")

        assert [e.id for e in result.items] == ["10", "20", "30"]

    async def test_limit_zero_returns_every_remaining_item(self) -> None:
        raw = [_synthetic_changelog_entry(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=1, limit=0, order="asc"
        )

        assert result.total == 5
        assert [e.id for e in result.items] == ["50002", "50003", "50004", "50005"]

    async def test_limit_zero_descending_returns_every_older_item(self) -> None:
        raw = [_synthetic_changelog_entry(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 6)]
        handler, _ = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=1, limit=0, order="desc"
        )

        assert result.total == 5
        assert [e.id for e in result.items] == ["50004", "50003", "50002", "50001"]

    async def test_reachable_beyond_100_entries_over_successive_calls(self) -> None:
        raw = [
            _synthetic_changelog_entry(i, f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z")
            for i in range(150)
        ]
        handler, seen = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=0, limit=0, order="asc"
        )

        assert result.total == 150
        assert len(result.items) == 150
        assert len(seen) >= 2  # jira.CHANGELOG_PAGE_SIZE (100) forces a second call

    async def test_multi_page_fetch_with_shrunk_page_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira, "CHANGELOG_PAGE_SIZE", 2)
        raw = [_synthetic_changelog_entry(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 6)]
        handler, seen = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog("SYN-1", limit=0, order="asc")

        assert [e.id for e in result.items] == ["50001", "50002", "50003", "50004", "50005"]
        assert len(seen) == 4  # 1 (discovery) + 2 + 2 + 1

    async def test_short_page_mid_range_stops_fetching_early(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A page shorter than requested, other than at the range boundary,
        stops the fetch loop immediately instead of retrying -- the same
        defensive stance `get_comments` takes for a collection that changed
        between calls.
        """
        monkeypatch.setattr(jira, "CHANGELOG_PAGE_SIZE", 2)
        entries = [_synthetic_changelog_entry(i, f"2025-01-0{i}T00:00:00Z") for i in range(1, 3)]

        def handler(request: httpx2.Request) -> httpx2.Response:
            start_at = int(request.url.params["startAt"])
            max_results = int(request.url.params["maxResults"])
            if start_at == 0 and max_results == 1:
                return _json_response(
                    200,
                    {
                        "startAt": 0,
                        "maxResults": 1,
                        "total": 5,
                        "isLast": False,
                        "values": entries[:1],
                    },
                )
            page = entries[start_at : start_at + max_results]
            return _json_response(
                200,
                {
                    "startAt": start_at,
                    "maxResults": max_results,
                    "total": 5,
                    "isLast": True,
                    "values": page,
                },
            )

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=0, limit=0, order="asc"
        )

        assert result.total == 5
        assert [e.id for e in result.items] == ["50001", "50002"]

    async def test_total_stays_correct_despite_upstream_corruption_beyond_real_total(
        self,
    ) -> None:
        raw = [_synthetic_changelog_entry(i, f"2025-01-{i:02d}T00:00:00Z") for i in range(1, 4)]
        handler, seen = _changelog_backend_handler(raw)

        result = await _make_client(handler).get_changelog(
            "SYN-1", start_at=0, limit=20, order="desc"
        )

        assert result.total == 3
        assert [e.id for e in result.items] == ["50003", "50002", "50001"]
        # confirms the discovery call never probes past the real total
        assert all(int(r.url.params["startAt"]) <= 3 for r in seen)

    async def test_empty_changelog(self) -> None:
        handler, _ = _recording_handler(
            _json_response(
                200, {"startAt": 0, "maxResults": 1, "total": 0, "isLast": True, "values": []}
            )
        )
        client = _make_client(handler)

        result = await client.get_changelog("SYN-1")

        assert result == Page(start_at=0, total=0, items=[])

    async def test_start_beyond_total_returns_empty_items_with_one_call(self) -> None:
        handler, seen = _recording_handler(
            _json_response(
                200, {"startAt": 0, "maxResults": 1, "total": 5, "isLast": False, "values": []}
            )
        )
        client = _make_client(handler)

        result = await client.get_changelog("SYN-1", start_at=50)

        assert result == Page(start_at=50, total=5, items=[])
        assert len(seen) == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"startAt": 0, "maxResults": 1, "values": []},
            {"startAt": 0, "maxResults": 1, "total": "5", "values": []},
            {"startAt": 0, "maxResults": 1, "total": 5, "values": "not-a-list"},
            [],
        ],
    )
    async def test_malformed_envelope_raises_server_error(self, body: Any) -> None:
        handler, _ = _recording_handler(_json_response(200, body))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_changelog("SYN-1")

    async def test_sort_key_drops_missing_or_malformed_created_without_crashing(self) -> None:
        raw = {
            "startAt": 0,
            "maxResults": 20,
            "total": 3,
            "isLast": True,
            "values": [
                _synthetic_changelog_entry(1, "2025-01-01T00:00:00Z"),
                {**_synthetic_changelog_entry(2, ""), "created": None},
                {**_synthetic_changelog_entry(3, ""), "created": "not-a-timestamp"},
            ],
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_changelog("SYN-1", order="asc")

        assert len(exc_info.value.problems) == 2
        assert [e["id"] for e in exc_info.value.partial_result["items"]] == ["50001"]

    async def test_sort_key_treats_non_dict_item_as_unsortable(self) -> None:
        raw = {
            "startAt": 0,
            "maxResults": 20,
            "total": 2,
            "isLast": True,
            "values": ["not-an-object", _synthetic_changelog_entry(1, "2025-01-01T00:00:00Z")],
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_changelog("SYN-1", order="asc")

        assert [e["id"] for e in exc_info.value.partial_result["items"]] == ["50001"]

    async def test_malformed_resource_raises_with_sanitized_partial_page(self) -> None:
        raw = {
            "startAt": 0,
            "maxResults": 20,
            "total": 2,
            "isLast": True,
            "values": [
                _synthetic_changelog_entry(1, "2025-01-01T00:00:00Z"),
                {"id": "50002", "author": None, "created": "2025-01-02T00:00:00Z", "items": []},
            ],
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_changelog("SYN-1", order="asc")

        assert exc_info.value.problems == ("$.items[1].author: expected an object",)
        assert exc_info.value.partial_result["total"] == 2
        assert [e["id"] for e in exc_info.value.partial_result["items"]] == ["50001"]

    async def test_not_found_raises_with_issue_key(self) -> None:
        fixture = _load("jira_issue_not_found.json")
        handler, _ = _recording_handler(_json_response(404, fixture))
        client = _make_client(handler)

        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            await client.get_changelog("SYN-404")

        assert exc_info.value.issue_key == "SYN-404"
        assert exc_info.value.status_code == 404

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.get_changelog("SYN-1")

    @pytest.mark.parametrize(
        ("kwargs", "message_fragment"),
        [
            ({"start_at": -1}, "non-negative"),
            ({"limit": -1}, "non-negative"),
            ({"order": "newest"}, "'asc' or 'desc'"),
        ],
    )
    async def test_invalid_arguments_raise_without_http_call(
        self, kwargs: dict[str, Any], message_fragment: str
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("no HTTP call should be made for invalid arguments")

        client = _make_client(handler)
        with pytest.raises(errors.JiraValidationError) as exc_info:
            await client.get_changelog("SYN-1", **kwargs)
        assert message_fragment in str(exc_info.value)


def _synthetic_attachment(attachment_id: str, filename: str = "diagnostics.log") -> dict[str, Any]:
    return {
        "self": f"{BASE_URL}/rest/api/3/attachment/{attachment_id}",
        "id": attachment_id,
        "filename": filename,
        "author": {
            "self": f"{BASE_URL}/rest/api/3/user?accountId=syn-acc-301",
            "accountId": "syn-acc-301",
            "displayName": "Jordan Lee",
            "active": True,
        },
        "created": "2025-08-26T09:55:40.906-0400",
        "size": 4096,
        "mimeType": "text/plain",
        "content": f"{BASE_URL}/rest/api/3/attachment/content/{attachment_id}",
        "thumbnail": f"{BASE_URL}/rest/api/3/attachment/thumbnail/{attachment_id}",
    }


class TestGetAttachments:
    async def test_returns_normalized_attachments_from_fixture(self) -> None:
        fixture = _load("jira_issue_attachments.json")
        handler, seen = _recording_handler(_json_response(200, fixture))
        client = _make_client(handler)

        items = await client.get_attachments("SYN-1")

        assert [a.id for a in items] == ["80001", "80002"]
        assert items[0].filename == "diagnostics.log"
        assert items[0].mime_type == "text/plain"
        assert items[0].size == 4096
        assert items[0].author == User(account_id="syn-acc-301", display_name="Jordan Lee")
        assert items[0].created == "2025-08-26T13:55:40Z"
        assert seen[0].url.params["fields"] == "attachment"

    async def test_sends_request_to_the_issue_endpoint(self) -> None:
        handler, seen = _recording_handler(
            _json_response(200, {"key": "SYN-1", "fields": {"attachment": []}})
        )
        client = _make_client(handler)

        await client.get_attachments("SYN-1")

        assert seen[0].url.path == "/rest/api/3/issue/SYN-1"

    async def test_issue_with_no_attachments_returns_empty_list(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"key": "SYN-1", "fields": {"attachment": []}})
        )
        client = _make_client(handler)

        assert await client.get_attachments("SYN-1") == []

    async def test_missing_attachment_key_returns_empty_list(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        assert await client.get_attachments("SYN-1") == []

    async def test_non_object_body_raises_server_error(self) -> None:
        handler, _ = _recording_handler(_json_response(200, ["not", "an", "object"]))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_attachments("SYN-1")

    async def test_non_object_fields_raises_server_error(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"key": "SYN-1", "fields": "not-an-object"})
        )
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_attachments("SYN-1")

    async def test_non_list_attachment_field_raises_server_error(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"key": "SYN-1", "fields": {"attachment": "not-a-list"}})
        )
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_attachments("SYN-1")

    async def test_malformed_attachment_dropped_with_sanitized_partial_result(self) -> None:
        raw = {
            "key": "SYN-1",
            "fields": {
                "attachment": [
                    _synthetic_attachment("80001"),
                    {**_synthetic_attachment("80002"), "author": None},
                ]
            },
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_attachments("SYN-1")

        assert exc_info.value.problems == ("$.fields.attachment[1].author: expected an object",)
        assert [a["id"] for a in exc_info.value.partial_result["items"]] == ["80001"]

    async def test_not_found_raises_with_issue_key(self) -> None:
        fixture = _load("jira_issue_not_found.json")
        handler, _ = _recording_handler(_json_response(404, fixture))
        client = _make_client(handler)

        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            await client.get_attachments("SYN-404")

        assert exc_info.value.issue_key == "SYN-404"

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.get_attachments("SYN-1")


def _attachment_download_handler(
    *,
    metadata: dict[str, Any] | None = None,
    metadata_status: int = 200,
    content_status: int = 200,
    content_bytes: bytes = b"synthetic attachment bytes",
    content_type: str = "text/plain",
    use_redirect: bool = True,
    signed_url: str = "https://media.example.invalid/signed/download",
) -> tuple[Handler, list[httpx2.Request]]:
    """Simulate GET .../attachment/{id} then GET .../attachment/content/{id}.

    `use_redirect` mirrors the live-observed behavior: the content URL
    returns 303 to a short-lived signed URL on a different host before the
    real 200 with bytes; `follow_redirects=True` is required to reach it.
    """
    if metadata is None:
        metadata = _synthetic_attachment("80001")
    attachment_id = str(metadata["id"])
    metadata_path = f"/rest/api/3/attachment/{attachment_id}"
    content_path = f"/rest/api/3/attachment/content/{attachment_id}"
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == metadata_path:
            return httpx2.Response(metadata_status, json=metadata)
        if request.url.path == content_path:
            if content_status != 200:
                return httpx2.Response(
                    content_status, json={"errorMessages": ["synthetic failure"], "errors": {}}
                )
            if use_redirect:
                return httpx2.Response(303, headers={"location": signed_url})
            return httpx2.Response(
                200, content=content_bytes, headers={"content-type": content_type}
            )
        if str(request.url) == signed_url:
            return httpx2.Response(
                200, content=content_bytes, headers={"content-type": content_type}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    return handler, seen


class _HangingAsyncStream(httpx2.AsyncByteStream):
    """Yields one chunk, signals `started`, then hangs until cancelled."""

    def __init__(self, first_chunk: bytes, started: asyncio.Event) -> None:
        self._first_chunk = first_chunk
        self._started = started

    async def __aiter__(self):  # type: ignore[override]
        self._started.set()
        yield self._first_chunk
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        return None


class TestDownloadAttachment:
    async def test_normal_download_writes_file_and_returns_result(self, tmp_path: Path) -> None:
        handler, _ = _attachment_download_handler()
        client = _make_client(handler, cache_dir=tmp_path)

        result = await client.download_attachment("80001")

        expected_path = tmp_path / "80001" / "diagnostics.log"
        assert result.attachment_id == "80001"
        assert result.filename == "diagnostics.log"
        assert result.mime_type == "text/plain"
        assert result.size == 4096
        assert result.local_path == str(expected_path)
        assert expected_path.read_bytes() == b"synthetic attachment bytes"
        assert not expected_path.with_name("diagnostics.log.part").exists()

    async def test_repeated_download_overwrites_atomically(self, tmp_path: Path) -> None:
        handler, _ = _attachment_download_handler(content_bytes=b"version one")
        await _make_client(handler, cache_dir=tmp_path).download_attachment("80001")

        handler2, _ = _attachment_download_handler(content_bytes=b"version two")
        result = await _make_client(handler2, cache_dir=tmp_path).download_attachment("80001")

        final_path = Path(result.local_path)
        assert final_path.read_bytes() == b"version two"
        assert not final_path.with_name(final_path.name + ".part").exists()

    async def test_same_filename_under_different_ids_do_not_clash(self, tmp_path: Path) -> None:
        handler1, _ = _attachment_download_handler(
            metadata=_synthetic_attachment("80001", filename="notes.txt"),
            content_bytes=b"first",
        )
        result1 = await _make_client(handler1, cache_dir=tmp_path).download_attachment("80001")

        handler2, _ = _attachment_download_handler(
            metadata=_synthetic_attachment("80002", filename="notes.txt"),
            content_bytes=b"second",
        )
        result2 = await _make_client(handler2, cache_dir=tmp_path).download_attachment("80002")

        assert result1.local_path != result2.local_path
        assert Path(result1.local_path).read_bytes() == b"first"
        assert Path(result2.local_path).read_bytes() == b"second"

    @pytest.mark.parametrize(
        ("raw_filename", "expected_basename"),
        [
            ("../../evil.txt", "evil.txt"),
            ("..\\..\\evil.txt", "evil.txt"),
            ("/etc/passwd", "passwd"),
            ("C:\\evil\\payload.exe", "payload.exe"),
            ("a/b/c/report.pdf", "report.pdf"),
        ],
    )
    async def test_traversal_filename_sanitized_to_basename(
        self, tmp_path: Path, raw_filename: str, expected_basename: str
    ) -> None:
        handler, _ = _attachment_download_handler(
            metadata=_synthetic_attachment("80001", filename=raw_filename)
        )
        client = _make_client(handler, cache_dir=tmp_path)

        result = await client.download_attachment("80001")

        final_path = Path(result.local_path)
        assert final_path.name == expected_basename
        assert final_path.parent == tmp_path / "80001"
        assert final_path.is_relative_to(tmp_path)

    @pytest.mark.parametrize("raw_filename", ["/", "\\", ".", "..", "../..", ".hidden", "..."])
    async def test_empty_or_hidden_filename_after_sanitization_raises(
        self, tmp_path: Path, raw_filename: str
    ) -> None:
        handler, _ = _attachment_download_handler(
            metadata=_synthetic_attachment("80001", filename=raw_filename)
        )
        client = _make_client(handler, cache_dir=tmp_path)

        with pytest.raises(errors.JiraValidationError):
            await client.download_attachment("80001")
        assert list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../etc", "a/b", "a\\b", ".", "..", ""])
    async def test_invalid_attachment_id_raises_without_http_call(
        self, tmp_path: Path, bad_id: str
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("no HTTP call should be made for an invalid attachment_id")

        client = _make_client(handler, cache_dir=tmp_path)
        with pytest.raises(errors.JiraValidationError):
            await client.download_attachment(bad_id)

    async def test_symlink_escape_is_rejected(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        outside = tmp_path_factory.mktemp("outside-cache-target")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        escape_link = cache_dir / "80001"
        try:
            escape_link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not permitted in this environment")

        handler, _ = _attachment_download_handler()
        client = _make_client(handler, cache_dir=cache_dir)

        with pytest.raises(errors.JiraValidationError):
            await client.download_attachment("80001")

        assert list(outside.iterdir()) == []

    async def test_not_found_attachment_raises(self, tmp_path: Path) -> None:
        handler, _ = _attachment_download_handler(metadata_status=404)
        client = _make_client(handler, cache_dir=tmp_path)

        with pytest.raises(errors.JiraNotFoundError):
            await client.download_attachment("80001")

    async def test_non_object_metadata_raises_server_error(self, tmp_path: Path) -> None:
        handler, _ = _recording_handler(_json_response(200, ["not", "an", "object"]))
        client = _make_client(handler, cache_dir=tmp_path)

        with pytest.raises(errors.JiraServerError):
            await client.download_attachment("80001")

    async def test_timeout_mid_download_raises_and_cleans_up_part_file(
        self, tmp_path: Path
    ) -> None:
        metadata = _synthetic_attachment("80001")

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/rest/api/3/attachment/80001":
                return httpx2.Response(200, json=metadata)
            raise httpx2.ReadTimeout("timed out")

        client = _make_client(handler, cache_dir=tmp_path)
        with pytest.raises(errors.JiraTimeoutError):
            await client.download_attachment("80001")

        assert list((tmp_path / "80001").glob("*")) == []

    async def test_error_status_from_content_url_raises_and_leaves_no_part_file(
        self, tmp_path: Path
    ) -> None:
        handler, _ = _attachment_download_handler(content_status=404)
        client = _make_client(handler, cache_dir=tmp_path)

        with pytest.raises(errors.JiraNotFoundError):
            await client.download_attachment("80001")

        assert list((tmp_path / "80001").glob("*.part")) == []

    async def test_network_failure_mid_download_raises_and_cleans_up_part_file(
        self, tmp_path: Path
    ) -> None:
        metadata = _synthetic_attachment("80001")

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/rest/api/3/attachment/80001":
                return httpx2.Response(200, json=metadata)
            raise httpx2.ConnectError("connection refused")

        client = _make_client(handler, cache_dir=tmp_path)
        with pytest.raises(errors.JiraNetworkError):
            await client.download_attachment("80001")

        assert list((tmp_path / "80001").glob("*")) == []

    async def test_cancellation_mid_download_leaves_no_part_file(self, tmp_path: Path) -> None:
        metadata = _synthetic_attachment("80001")
        started = asyncio.Event()

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/rest/api/3/attachment/80001":
                return httpx2.Response(200, json=metadata)
            return httpx2.Response(200, stream=_HangingAsyncStream(b"partial", started))

        client = _make_client(handler, cache_dir=tmp_path)
        task = asyncio.create_task(client.download_attachment("80001"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert list((tmp_path / "80001").glob("*")) == []

    @pytest.mark.parametrize(
        "override",
        [
            {"filename": None},
            {"filename": ""},
            {"mimeType": None},
            {"size": None},
            {"size": -1},
            {"size": "4096"},
            {"content": None},
            {"content": ""},
        ],
    )
    async def test_malformed_metadata_raises_without_partial_download(
        self, tmp_path: Path, override: dict[str, Any]
    ) -> None:
        metadata = {**_synthetic_attachment("80001"), **override}
        handler, _ = _attachment_download_handler(metadata=metadata)
        client = _make_client(handler, cache_dir=tmp_path)

        with pytest.raises(errors.JiraServerError):
            await client.download_attachment("80001")
        assert list(tmp_path.rglob("*")) == []

    async def test_metadata_id_as_integer_is_accepted(self, tmp_path: Path) -> None:
        metadata = {**_synthetic_attachment("80001"), "id": 80001}
        handler, _ = _attachment_download_handler(metadata=metadata)
        client = _make_client(handler, cache_dir=tmp_path)

        result = await client.download_attachment("80001")

        assert result.attachment_id == "80001"
